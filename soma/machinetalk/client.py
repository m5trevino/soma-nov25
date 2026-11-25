"""TurnKey client — the only place that talks to models.

Dendrites and Soma call this with a stage name and messages.
They do not pick a model. They do not know the engine URL.
TurnKey SDK does rotation, retry, gateway, and the HTTP call.
"""
from __future__ import annotations

import contextlib
import http.client
import inspect
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Stage 1 extracts. There is exactly one right answer to "which sentence is
# the directive", so sampling a distribution over answers is wrong: the same
# prompt against the same model returned 0, 7 and 0 verified entries on three
# consecutive calls. Later stages compose plans and journals, where some
# latitude is legitimate.
_EXTRACTIVE_STAGES = frozenset({
    "distiller", "introbeaver", "planbeaver", "outrobeaver",
    "scout", "oc", "olc",
})
DEFAULT_EXTRACTIVE_TEMPERATURE = 0.0


# ── live status helpers ─────────────────────────────────────────────────
# These feed the ``emit`` callback of ``EngineClient.call_stage``. Each event
# is printed at the moment the step happens (not batched after the call), and
# every value is read from that step: instruction size from the database row,
# input size from the assembled messages, tokens and latency from the reply,
# the gate verdict from validate(), anchors from provenance. Nothing is
# estimated. Same idea as the distiller's step lines in progress.py, which
# show real work live; see the comment above ``_DISTILLER_STEPS`` there.

def _tok(n: Any) -> str:
    """A token count, or an em dash when the provider reported none."""
    return f"{n:,}" if isinstance(n, int) and n else "—"


def _say_gate(say: Any, gate: Any, is_done: Any) -> None:
    """Report the gate's verdict, and the model's own complaint if it made one."""
    if gate.skipped and not gate.errors:
        word = f"SKIP · {gate.skip_reason or gate.verdict or 'skipped'}"
    elif is_done(gate):
        word = "PASS" + (f" · verdict {gate.verdict}" if gate.verdict else "")
    else:
        word = "FAIL · " + ("; ".join(gate.errors[:2]) or gate.verdict or "unknown")
    say("gate", word)
    parsed = gate.parsed
    if isinstance(parsed, dict) and parsed.get("error"):
        missing = parsed.get("missing_fields")
        say("model said",
            f"{parsed['error']}"
            + (f" · missing {missing}" if missing else "")
            + (" · halt" if parsed.get("halt") else ""))


_STAGE_MAP = {
    "receptor": "distiller",
    "distiller": "distiller",
    "classifier": "introbeaver",
    "introbeaver": "introbeaver",
    "integrator": "planbeaver",
    "planbeaver": "planbeaver",
    "retrograde": "outrobeaver",
    "outrobeaver": "outrobeaver",
    "nexus": "nexus",
    "spark": "spark",
    "falcon": "falcon",
    "drafter": "drafter",
    "eagle": "eagle",
    "scout": "scout",
    "axon_hillock": "oc",
    "oc": "oc",
    "myelinator": "olc",
    "olc": "olc",
    "terminal": "terminal",
    "saltatory_gate": "saltatory_gate",
}


def _drafted_files(soma: Any, lock: Any, build: str) -> list[dict]:
    """The files the drafter wrote, read from its StageOutput in the Soma."""
    from .gate import _find
    if soma is None:
        return []
    with (lock if lock is not None else contextlib.nullcontext()):
        node = soma.get_node(f"stage:{build}:drafter")
    out = node.properties.get("output") if node is not None else None
    files = _find(out, "files") if isinstance(out, (dict, list)) else None
    return [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []


def _int_metric(value: Any) -> int:
    """Coerce a provider-reported counter to int, or 0 when it is unusable.

    Providers differ: some omit the cache keys, some send null. Telemetry
    must never be the reason a call fails, so anything unreadable is 0.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _messages_chars(messages: Any) -> int:
    """Total characters of the outgoing message contents.

    Measured from the messages themselves, before the request is sent, so a
    provider that reports nothing still leaves a size on the record. Only a
    count is kept; the text itself never goes onto the usage record.
    """
    if not isinstance(messages, list):
        return 0
    total = 0
    for msg in messages:
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                total += len(content)
    return total


@dataclass
class CallRecord:
    stage: str = ""
    session_id: str = ""
    chatset_id: str = ""
    model: str = ""
    status: str = ""
    attempt: int = 0
    attempts: list = field(default_factory=list)
    gate: Any = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    chars_in: int = 0
    latency_ms: float = 0.0
    error: str = ""
    # What the engine says it recorded: its session, this call's stage id
    # (DI-193) and the dashboard link. Empty when the engine returns none.
    engine_session: str = ""
    engine_stage: str = ""
    log_link: str = ""
    raw_response: str = ""
    sys_prompt: str = ""
    user_msg: str = ""
    # The whole tool conversation, one entry per turn, and how many writes the
    # gate sent back. See machinetalk/trajectory.py.
    transcript: list = field(default_factory=list)
    turns: list = field(default_factory=list)
    rejections: int = 0
    provider_served: str = ""
    upstream_provider: str = ""


class StageResult:
    """What a dendrite gets back. No model selection happened on their side."""

    def __init__(
        self,
        content: str,
        model_id: str = "unknown",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: float = 0.0,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        chars_in: int = 0,
        engine_session: str = "",
        engine_stage: str = "",
        log_link: str = "",
        tool_calls: list | None = None,
        provider: str = "",
        upstream_provider: str = "",
    ) -> None:
        # The gateway the engine says answered, and, behind an aggregator such
        # as OpenRouter, the company that actually served the call.
        self.provider = provider
        self.upstream_provider = upstream_provider
        # The tool calls the model made in this reply, as the provider sent
        # them. Empty when the model answered in text.
        self.tool_calls = tool_calls or []
        self.engine_session = engine_session
        self.engine_stage = engine_stage
        self.log_link = log_link
        self.content = content
        self.model_id = model_id
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.latency_ms = latency_ms
        self.cache_hit_tokens = cache_hit_tokens
        self.cache_miss_tokens = cache_miss_tokens
        self.chars_in = chars_in


class ProfileError(RuntimeError):
    """The deck file is absent, unreadable, or not a profile.

    A run whose deck is wrong cannot be interpreted afterwards, so this is
    raised rather than defaulted: there is no correct deck to guess.
    """


def describe_deck(profile_path: str | Path) -> dict:
    """Describe the deck a profile loads, read-only.

    Returns ``{"path", "name", "models", "stages", "stages_pinned"}``.
    ``models`` is the number of cards the profile puts in play and ``stages``
    names them, so a log line says which deck produced a run instead of only
    which file was named.

    Raises ProfileError if the path is unset, missing, unreadable, not JSON,
    or not a JSON object. Nothing is written and no model is selected here.
    """
    if not profile_path:
        raise ProfileError(
            "TurnKey profile path is not set: no deck can be described")

    path = Path(profile_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProfileError(f"TurnKey profile not found: {path}") from exc
    except OSError as exc:
        raise ProfileError(
            f"TurnKey profile is unreadable: {path}: {exc}") from exc

    try:
        profile = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileError(
            f"TurnKey profile is not valid JSON: {path}: {exc}") from exc

    if not isinstance(profile, dict):
        raise ProfileError(
            f"TurnKey profile root is not a JSON object: {path}")

    deck = profile.get("deck")
    if not isinstance(deck, list):
        # Peacock-era profiles list their cards under "attempts".
        deck = profile.get("attempts")
    if not isinstance(deck, list):
        raise ProfileError(
            f"TurnKey profile has no 'deck' or 'attempts' list: {path}")

    raw_stages = profile.get("stages")
    stages: list[str] = []
    pinned = 0
    if isinstance(raw_stages, list):
        for entry in raw_stages:
            if not isinstance(entry, dict):
                continue
            name = entry.get("stageName") or entry.get("stage") or ""
            if name:
                stages.append(str(name))
            if entry.get("pinnedModels"):
                pinned += 1
    elif isinstance(raw_stages, dict):
        for name, entry in raw_stages.items():
            stages.append(str(name))
            if isinstance(entry, dict) and entry.get("pinnedModels"):
                pinned += 1

    return {
        "path": str(path),
        "name": str(profile.get("name") or ""),
        "models": len(deck),
        "stages": stages,
        "stages_pinned": pinned,
    }


class PinUnavailable(RuntimeError):
    """A stage is pinned to a model the deck does not hold.

    Raised instead of serving the stage from some other model: a pin that
    quietly falls back to the rest of the deck makes a one-model run
    meaningless. Never retried, because retrying cannot make the model appear.
    """


def pins_from_profile(profile_path: str | Path) -> dict[str, list[str]]:
    """``{stage: [model ids]}`` for every stage a profile pins. Unpinned stages are absent."""
    try:
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = profile.get("stages") if isinstance(profile, dict) else None
    entries = (raw.items() if isinstance(raw, dict)
               else [(e.get("stageName") or e.get("stage"), e)
                     for e in (raw or []) if isinstance(e, dict)])
    pins: dict[str, list[str]] = {}
    for name, entry in entries:
        models = [m for m in ((entry or {}).get("pinnedModels") or []) if m]
        if name and models:
            pins[str(name)] = models
    return pins


class TurnKeyClient:
    """Middleware. Takes a stage name. Hands it to TurnKey. Returns the text."""

    def __init__(self, profile_path: str) -> None:
        self._profile_path = profile_path
        # Describe the deck before the SDK loads it: the line must exist even
        # if the deck turns out to be unusable, and a bad path is fatal here
        # with the path in the message rather than deep inside the SDK.
        self._deck = describe_deck(profile_path)
        print(f"deck: {self._deck['path']}  models={self._deck['models']}  "
              f"stages_pinned={self._deck['stages_pinned']}", flush=True)
        self._sdk = self._load_sdk(profile_path)
        self._pea_param = self._pea_param_name(self._sdk.run_stage)
        self._pea_kw = self._pea_param is not None
        self._stage_id_kw = "stage_id" in inspect.signature(self._sdk.run_stage).parameters
        # A pin is a pin. By default a pinned stage is served by its pinned
        # model or not at all: the SDK's own retry loop would rotate to the
        # rest of the deck once the circuit breaker shut the model down, so it
        # is told not to retry, and the caller retries on the same model.
        # SOMA_STRICT_PINS=0 restores the SDK's fallback to the whole deck.
        self._pins = pins_from_profile(profile_path)
        self._strict_pins = os.environ.get("SOMA_STRICT_PINS", "1") != "0"
        if self._strict_pins and self._pins:
            config = getattr(self._sdk, "config", None)
            if config is not None:
                config.max_retries = 0
            print(f"pins: strict · "
                  + " · ".join(f"{s}={'/'.join(m)}" for s, m in
                               sorted(self._pins.items())[:6])
                  + (" · …" if len(self._pins) > 6 else ""), flush=True)

    _broken: set = set()

    def mark_broken(self, gateway: str, model: str) -> None:
        """A card that cannot work (wrong provider): it stays shut down."""
        if "_broken" not in self.__dict__:
            self._broken = set()
        self._broken.add(f"{gateway}:{model}")

    def revive_deck(self) -> int:
        """Lift the circuit breaker's shutdowns that were only transient.

        The SDK shuts a model down after three consecutive failures and never
        tries it again, so one bad minute (a rate limit, an outage) retires the
        whole deck for the rest of the process. Cards recorded as broken stay
        shut. Returns how many cards are usable again.
        """
        rotator = self._sdk.rotator
        shut = getattr(rotator, "_shutdown_models", None)
        if shut is None:
            return 0
        broken = set(self.__dict__.get("_broken", ()))
        before = len(shut)
        shut.clear()
        for per_gateway in getattr(rotator, "_failures", {}).values():
            for model_id in per_gateway:
                per_gateway[model_id] = 0
        shut.update(broken)
        return max(0, before - len(broken & shut))

    def pinned_models(self, stage: str) -> list[str]:
        """The models a stage is held to, or [] when it is not strictly pinned."""
        if not getattr(self, "_strict_pins", False):
            return []
        return list(getattr(self, "_pins", {}).get(_STAGE_MAP.get(stage, stage), []))

    def _hold_pin(self, stage: str, pins: list[str]) -> None:
        """Make sure the next call is served by a pinned model, or refuse it."""
        rotator = self._sdk.rotator
        if rotator.peek_model(stage).get("model_id") in pins:
            return
        # The circuit breaker shut the pinned model down after repeated
        # failures. For a pinned stage that is a transient fault to wait out,
        # not a reason to use another model, so the model is brought back.
        for gateway, model in list(getattr(rotator, "_flat", [])):
            if model.get("id") in pins:
                rotator.report_success(gateway, model["id"])
        if rotator.peek_model(stage).get("model_id") not in pins:
            raise PinUnavailable(
                f"stage {stage!r} is pinned to {pins} but the deck does not "
                f"hold it. Add it to the profile's deck or unpin the stage.")

    @staticmethod
    def _pea_param_name(fn) -> str | None:
        """The keyword the installed SDK's run_stage takes for the session id.

        The SDK names it ``session_id`` and turns it into the engine's
        ``pea_session``. It has no ``**kwargs``, so looking only for a
        ``pea_session`` parameter found nothing and the id was never sent:
        the engine then made a new session for every call.
        """
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return "pea_session"
        for name in ("pea_session", "session_id"):
            if name in params:
                return name
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return "pea_session"
        return None

    @staticmethod
    def _supports_pea_session(fn) -> bool:
        """True when the installed SDK accepts a session id."""
        return TurnKeyClient._pea_param_name(fn) is not None

    @staticmethod
    def _load_sdk(profile_path: str):
        try:
            from turnkey.profile_integration import initialize_sdk_with_profile
        except ImportError as exc:
            raise RuntimeError(
                "TurnKey SDK is not importable. It is the only middleware "
                "for model calls. Install or path it before running."
            ) from exc
        return initialize_sdk_with_profile(profile_path)

    def report_stage(self, kind: str, **fields: Any) -> dict | None:
        """Report a stage event to the engine's log. None when the SDK cannot."""
        fn = getattr(self._sdk, "report_stage", None)
        return fn(kind, **fields) if fn else None

    def call(self, stage: str, messages: list[dict[str, str]],
             pea_session: str | None = None,
             temperature: float | None = None,
             tools: list[dict] | None = None,
             tool_choice: str | dict | None = None,
             stage_id: str | None = None) -> StageResult:
        mapped = _STAGE_MAP.get(stage, stage)
        pins = self.pinned_models(mapped)
        if pins:
            self._hold_pin(mapped, pins)
        # Size is measured before the request leaves, so it is available even
        # when the provider reports no usage block at all.
        chars_in = _messages_chars(messages)
        start = time.monotonic()
        pea_param = getattr(self, "_pea_param", "pea_session")
        kwargs = {pea_param: pea_session} if self._pea_kw and pea_param else {}
        # run_stage only sends temperature when it is not None, so passing it
        # unconditionally is safe and makes unset the explicit default.
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if stage_id and getattr(self, "_stage_id_kw", False):
            kwargs["stage_id"] = stage_id
        raw = self._sdk.run_stage(mapped, messages, **kwargs)
        elapsed = (time.monotonic() - start) * 1000

        choices = raw.get("choices", []) if isinstance(raw, dict) else []
        content = ""
        tool_calls: list = []
        if choices:
            message = choices[0].get("message", {}) or {}
            content = message.get("content", "") or ""
            tool_calls = message.get("tool_calls") or []

        usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        return StageResult(
            content=content,
            model_id=raw.get("model", "unknown") if isinstance(raw, dict) else "unknown",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            latency_ms=elapsed,
            # DeepSeek and Qwen report prefix-cache accounting here; other
            # providers omit it. Absent or unreadable is 0, never a raise.
            cache_hit_tokens=_int_metric(usage.get("prompt_cache_hit_tokens", 0)),
            cache_miss_tokens=_int_metric(usage.get("prompt_cache_miss_tokens", 0)),
            chars_in=chars_in,
            # The engine returns these beside the answer. Absent is "", never
            # a raise: a run must not fail because a link did not come back.
            engine_session=str(raw.get("session") or "") if isinstance(raw, dict) else "",
            engine_stage=str(raw.get("stage") or "") if isinstance(raw, dict) else "",
            log_link=str(raw.get("log_link") or "") if isinstance(raw, dict) else "",
            tool_calls=tool_calls,
            provider=str(raw.get("provider") or "") if isinstance(raw, dict) else "",
            upstream_provider=(str(raw.get("upstream_provider") or "")
                               if isinstance(raw, dict) else ""),
        )

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> StageResult:
        stage = kwargs.get("stage") or kwargs.get("model") or "distiller"
        return self.call(stage, messages)


MAX_TOOL_TURNS = 8
# How many times a rejected write is handed back before the stage fails on it.
MAX_WRITE_RETRIES = int(os.environ.get("SOMA_WRITE_RETRIES", "2"))
# The four beaver stages read once and write once: no rewrite is handed back.
# Set this above 0 to let the gate send a beaver write back to the model.
BEAVER_WRITE_RETRIES = int(os.environ.get("SOMA_BEAVER_WRITE_RETRIES", "0"))
# A reply with no tool call, after the stage has read its inputs, is handed back
# this many times. The prose is never taken as the output: only a tool call is.
PROSE_NUDGES = int(os.environ.get("SOMA_PROSE_NUDGES", "1"))
# A running stage tells the engine's log it is alive this often, even inside one
# slow model call. A stage that stops pinging shows as stalled, so a killed run
# never stays "running".
HEARTBEAT_S = float(os.environ.get("SOMA_HEARTBEAT_S", "20"))
# Registering a stage with the engine log is retried: a stage that never registers
# is invisible in the stage view.
START_TRIES = int(os.environ.get("SOMA_START_TRIES", "3"))
START_BACKOFF_S = float(os.environ.get("SOMA_START_BACKOFF_S", "0.5"))


def _undo_double_encoding(out: Any) -> Any:
    """Decode a JSON document that arrived encoded twice.

    A provider carries a tool argument as JSON, and some models then put the
    whole document inside that argument as a string, or put it in a
    top-level key as a string: ``"{...}"`` or ``{"x_output": "{...}"}``. That
    is a transport artifact, not content: decode it once and hand the document
    to the gate, which still judges what is in it. Anything else is returned
    unchanged, so a genuinely broken document still fails the gate.
    """
    def as_object(text: str):
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    if isinstance(out, str):
        return as_object(out) or out
    if isinstance(out, dict):
        return {k: (as_object(v) or v) if isinstance(v, str) else v
                for k, v in out.items()}
    return out

# The beavers' tools. Every one of them reads or writes the Soma itself: the
# instructions row, the chat set's Message nodes, the nodes earlier stages
# left behind, and the stage's own output node. Nothing is assembled for the
# model; it asks for what its instructions say it needs.
BEAVER_STAGES = frozenset({"distiller", "introbeaver", "planbeaver",
                           "outrobeaver"})
BUILD_STAGES = frozenset({"nexus", "spark", "falcon", "eagle", "drafter",
                          "scout", "oc", "olc"})
SOMA_BEAVER_TOOLS = [
    {"type": "function", "function": {
        "name": "get_receptor_config",
        "description": "Read this stage's instructions from the Soma.",
        "parameters": {"type": "object",
                       "properties": {"stage": {"type": "string"}},
                       "required": ["stage"]}}},
    {"type": "function", "function": {
        "name": "get_chatset",
        "description": "Read a chat set from the Soma: its session id, hash "
                       "and raw_chat_set (every turn, in order). For a build "
                       "id, every chat set of the build, merged in order.",
        "parameters": {"type": "object",
                       "properties": {"chatset_id": {"type": "string"}},
                       "required": ["chatset_id"]}}},
    {"type": "function", "function": {
        "name": "query_nodes",
        "description": "Query nodes already in the Soma for a chat set. "
                       "StageOutput: what earlier stages wrote, each with its "
                       "stage name and chat set (a build id returns the "
                       "build's own stage outputs and those of its chat "
                       "sets).",
        "parameters": {"type": "object",
                       "properties": {
                           "node_type": {"type": "string",
                                         "enum": ["StageOutput"]},
                           "chatset_id": {"type": "string"}},
                       "required": ["node_type", "chatset_id"]}}},
    {"type": "function", "function": {
        "name": "create_soma_node",
        "description": "Write this stage's output into the Soma as a "
                       "StageOutput node. Python validates it before it is "
                       "accepted.",
        "parameters": {"type": "object",
                       "properties": {
                           "node_type": {"type": "string",
                                         "enum": ["StageOutput"]},
                           "stage": {"type": "string"},
                           # A string: with no declared properties a model
                           # flattens an object argument and drops what is
                           # nested in it. A malformed string is caught by the
                           # write tool, which hands the parse error back.
                           "output": {
                               "type": "string",
                               "description": "The complete output as one "
                                              "valid JSON document, exactly "
                                              "as the stage instructions "
                                              "define it, every field "
                                              "filled in."}},
                       "required": ["node_type", "stage", "output"]}}},
]
_READ_INSTRUCTIONS = ("get_receptor_config",)
_READ_INPUT = ("get_chatset",)
_WRITE = ("create_soma_node",)


class EngineClient:
    """Adapter so the engine can call TurnKey with the old call_stage shape.

    TurnKey still picks the model. The engine never sees a model id until
    the response comes back.
    """

    def __init__(self, profile_path: str) -> None:
        self._tk = TurnKeyClient(profile_path)
        self.timeout = 180.0

    def model_for(self, stage: str) -> str:
        """Name the model the next call will use.

        This used to return the literal string "turnkey", which appeared in
        every progress line and summary table as though it were a model. The
        rotator can say which card it is about to serve without consuming it.
        """
        return self.route_for(stage).get("model_id") or "unknown"

    def route_for(self, stage: str) -> dict:
        """Model id and provider for the next call, without consuming it.

        The provider is the gateway the card belongs to (openrouter, merge,
        xkiro, ollama, novita). Two models with the same id can behave
        differently behind different gateways, so a telemetry block that
        names only the model is ambiguous when something needs explaining.
        """
        try:
            info = self._tk._sdk.rotator.peek_model(stage)
        except Exception:
            # A profile with no deck is a configuration error the call itself
            # will surface; the display should not crash first.
            return {"model_id": "unknown", "provider": "unknown"}
        return {
            "model_id": info.get("model_id") or "unknown",
            "provider": info.get("gateway") or "unknown",
        }

    # A DNS failure cannot be fixed by waiting: the host does not resolve at
    # all, so every retry fails the same way. Give it one quick second chance
    # and then surface it instead of burning the whole retry budget.
    _sleep = staticmethod(time.sleep)
    _REVIVE_COOLDOWN = 30.0

    def _after_failure(self, stage: str, text: str) -> None:
        """Keep the deck usable across a bad minute without hiding a bad card."""
        m = re.search(r"asked provider '([^']+)' for model '([^']+)'", text)
        if m and "PROVIDER MISMATCH" in text:
            mark = getattr(self._tk, "mark_broken", None)
            if mark:
                mark(m.group(1), m.group(2))         # a wrong provider is not transient
        elif "All models have been shut down" in text:
            revive = getattr(self._tk, "revive_deck", None)
            if revive and revive() > 0:
                print(f"[deck] every card was shut down; lifted the transient ones "
                      f"after {self._REVIVE_COOLDOWN:.0f}s", file=sys.stderr, flush=True)
                self._sleep(self._REVIVE_COOLDOWN)

    def _pin_wait(self, stage: str, attempt: int, attempts: int, error: str = "") -> None:
        """Wait before retrying a strictly pinned stage.

        An unpinned stage recovers by rotating to the next card, so it never
        waits. A pinned stage has no other card: the only recovery is time on
        the same model (a cold endpoint, a rate limit), so it backs off. A rate
        limit (HTTP 429) is waited out for longer than a dropped connection,
        because a per-minute quota only resets after the minute is up.
        """
        pinned = getattr(self._tk, "pinned_models", None)
        if attempt + 1 < attempts and pinned and pinned(stage):
            if "429" in error:
                self._sleep(min(15.0 * (attempt + 1) + 5.0, 75.0))
            else:
                self._sleep(min(2.0 * (attempt + 1), 20.0))

    _UNRECOVERABLE = ("Name or service not known",
                      "nodename nor servname",
                      "Temporary failure in name resolution")

    def _call_with_retry(self, stage: str, messages: list[dict[str, str]],
                         attempts: int = 10,
                         pea_session: str | None = None,
                         temperature: float | None = None,
                         **tool_kwargs: Any) -> Any:
        """Retry transport failures that escape the TurnKey SDK.

        The SDK converts URLError/TimeoutError to SDKError and retries those,
        but ``http.client.RemoteDisconnected`` is a ConnectionError/BadStatusLine
        and is not a URLError, so it propagates out of ``run_stage`` and kills
        the chatset. Retrying here also advances the SDK's deck rotator, so a
        bad model is skipped instead of being hit again.

        Failures that cannot improve with time (DNS resolution) abort after the
        first retry, because retrying them only multiplies the wait by the
        attempt count.
        """
        last: Exception | None = None
        dns_failures = 0
        for attempt in range(attempts):
            # Name the model this attempt will use. Without it a retry storm
            # reads as one model failing repeatedly, when the deck is actually
            # rotating behind it and every card is being burned in turn.
            model = self.model_for(stage)
            try:
                return self._tk.call(stage, messages, pea_session=pea_session,
                                     temperature=temperature, **tool_kwargs)
            except (TypeError, PinUnavailable):
                # A signature mismatch is a code bug, and a missing pinned
                # model will not appear: retrying either only burns the deck.
                raise
            except (ConnectionError, TimeoutError, http.client.HTTPException) as exc:
                last = exc
                text = f"{type(exc).__name__}: {exc}"
                print(f"\n[retry] {stage} attempt {attempt + 1}/{attempts} "
                      f"model={model} failed: {text}", file=sys.stderr, flush=True)
                self._pin_wait(stage, attempt, attempts, text)
                if any(tok in text for tok in self._UNRECOVERABLE):
                    dns_failures += 1
                    if dns_failures >= 2:
                        print(f"[retry] {stage}: DNS failure will not recover, "
                              f"stopping after {attempt + 1} attempt(s)",
                              file=sys.stderr, flush=True)
                        raise
            except Exception as exc:
                # SDKError and anything else — also retry to rotate the deck
                last = exc
                text = f"{type(exc).__name__}: {exc}"
                print(f"\n[retry] {stage} attempt {attempt + 1}/{attempts} "
                      f"model={model} failed: {text}", file=sys.stderr, flush=True)
                self._after_failure(stage, text)
                self._pin_wait(stage, attempt, attempts, text)
                if any(tok in text for tok in self._UNRECOVERABLE):
                    dns_failures += 1
                    if dns_failures >= 2:
                        print(f"[retry] {stage}: DNS failure will not recover, "
                              f"stopping after {attempt + 1} attempt(s)",
                              file=sys.stderr, flush=True)
                        raise
        assert last is not None
        raise last

    def _run_tool_loop(self, stage: str, chatset_id: str, instructions: str,
                       row: dict, read_data: Any, *, tools: list,
                       query_is_input: bool = False,
                       hooks: dict | None = None,
                       check: Any = None,
                       pea_session: str | None,
                       temperature: float | None, say: Any,
                       stage_id: str | None = None, report: Any = None):
        """Drive one stage as tool calls. Returns (result, written, read).

        ``result`` totals tokens and latency over every turn; its content is
        what the write tool carried. ``written`` is False when the model
        never wrote (text instead of a tool call, bad tool use, or too many
        turns), and then ``result.error`` says why. ``read`` records which
        reads the model actually made, and ``read["served"]`` holds the text
        the data reads returned. ``read_data(name, args)`` answers every
        read other than the instructions.
        """
        # The only message is the stage's identity. It carries no instruction
        # and no history; the model has to fetch those by tool call.
        messages: list[dict] = [{
            "role": "user",
            "content": f"stage: {stage}\nchatset_id: {chatset_id}"}]
        read: dict = {"instructions": False, "input": False, "served": [],
                      "missing": [], "messages": messages, "turns": [],
                      "rejected": 0}
        rejected = 0
        total = StageResult("")
        total.error = ""
        total.chars_in = _messages_chars(messages)
        beaver = stage in BEAVER_STAGES
        nudges = 0
        max_rewrites = BEAVER_WRITE_RETRIES if beaver else MAX_WRITE_RETRIES
        for turn in range(MAX_TOOL_TURNS):
            # A beaver reads its instructions and its input once. After that the
            # only tool it is offered is the write, so it cannot read again and
            # loop: its next call is its one write.
            turn_tools = tools
            if beaver and read["instructions"] and read["input"]:
                turn_tools = [t for t in tools if t["function"]["name"] in _WRITE]
            # Every turn of a stage is one engine session. The engine makes the
            # id on the first request that carries none, so the next turn sends
            # back the one it made; without this each turn became its own session.
            r = self._call_with_retry(stage, messages, pea_session=pea_session,
                                      temperature=temperature,
                                      tools=turn_tools, tool_choice="auto",
                                      **({"stage_id": stage_id} if stage_id else {}))
            for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                      "latency_ms", "cache_hit_tokens", "cache_miss_tokens"):
                setattr(total, k, getattr(total, k) + (getattr(r, k) or 0))
            total.model_id = r.model_id
            total.provider = getattr(r, "provider", "") or total.provider
            total.upstream_provider = (getattr(r, "upstream_provider", "")
                                       or total.upstream_provider)
            read["turns"].append({
                "turn": turn, "model": r.model_id,
                "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
                "latency_ms": round(r.latency_ms or 0.0, 1),
                "tool_calls": [(c.get("function") or {}).get("name", "")
                               for c in (r.tool_calls or [])]})
            total.engine_session = r.engine_session or total.engine_session
            total.engine_stage = r.engine_stage or total.engine_stage
            pea_session = pea_session or r.engine_session or None
            stage_id = stage_id or r.engine_stage or None
            if report:
                # per-attempt numbers, measured here, for the engine's retry ledger
                report("turn", turn=turn + 1, model=r.model_id,
                       latency_ms=round(r.latency_ms or 0.0, 1),
                       prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens,
                       engine_session=r.engine_session, engine_stage=r.engine_stage)
            total.log_link = r.log_link or total.log_link
            if not r.tool_calls:
                if nudges < PROSE_NUDGES:
                    # The model answered in text. Its reply is kept in the history
                    # but is not the output; it is told so, once, in the same words
                    # every other refusal here uses. If it has not read both its
                    # instructions and its input, it is told to read them first:
                    # a document written without reading the chat set is invented.
                    nudges += 1
                    read["rejected"] = rejected + nudges
                    say("write", "no tool call: the reply was text, handed back")
                    if report:
                        report("reject", errors=["prose_instead_of_tool_call"])
                    messages.append({"role": "assistant", "content": r.content or ""})
                    if read["instructions"] and read["input"]:
                        again = "Call create_soma_node now."
                    else:
                        again = ("Read the instructions and the input first, each by "
                                 "tool call, then call create_soma_node.")
                    messages.append({"role": "user", "content":
                        "error: this run takes its output only as tool calls. Your "
                        "reply had no tool call and was discarded. " + again})
                    continue
                total.content = r.content or ""
                total.error = "prose_instead_of_tool_call"
                return total, False, read
            messages.append({"role": "assistant", "content": r.content or None,
                             "tool_calls": r.tool_calls})
            for call in r.tool_calls:
                fn = (call.get("function") or {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except (ValueError, TypeError):
                    args = None
                reply = None
                if name in _READ_INSTRUCTIONS:
                    if (args or {}).get("stage", stage) != stage:
                        reply = f"error: this run is stage {stage!r}"
                    else:
                        read["instructions"] = True
                        reply = instructions
                        total.chars_in += len(instructions)
                        say("instructions",
                            f"model called {name}: "
                            f"stage_instructions[{stage}] · {row.get('tb_number') or '—'} "
                            f"v{row.get('version', '—')} · {len(instructions):,} bytes")
                elif (name in _READ_INPUT or name == "query_nodes"
                      or name in (hooks or {})):
                    if name in (hooks or {}):
                        try:
                            reply = hooks[name][1](args or {})
                        except Exception as exc:     # a failed tool answers, it never crashes the stage
                            reply = f"error: {type(exc).__name__}: {exc}"
                    else:
                        reply = read_data(name, args or {})
                    read["served"].append(reply)
                    if reply.startswith("error: MISSING_SOMA_DATA"):
                        read["missing"].append(reply[len("error: "):])
                    if ((name in _READ_INPUT or query_is_input)
                            and not reply.startswith("error:")):
                        read["input"] = True
                    total.chars_in += len(reply)
                    say("input", f"model called {name}: {len(reply):,} chars")
                elif name in _WRITE:
                    if not (read["instructions"] and read["input"]):
                        reply = ("error: read the instructions and the input "
                                 "before writing")
                    elif (name == "create_soma_node"
                          and ((args or {}).get("node_type") != "StageOutput"
                               or (args or {}).get("stage", stage) != stage)):
                        reply = (f"error: this run may only write a "
                                 f"StageOutput node for stage {stage!r}")
                    else:
                        out = (args or {}).get("output") if args is not None else None
                        if out is None:
                            # Unreadable arguments are handed to the gate as
                            # they are, so it reports the real parse failure.
                            content = raw_args if isinstance(raw_args, str) else ""
                        else:
                            out = _undo_double_encoding(out)
                            content = out if isinstance(out, str) else json.dumps(
                                out, ensure_ascii=False)
                        # Python's gate runs on the write itself. What it
                        # rejects is handed back with its reasons so the model
                        # can write the document again; after the retries the
                        # text goes on as it is and the stage fails on it.
                        if rejected < max_rewrites:
                            if args is None:
                                problems = ["the tool arguments are not valid JSON"]
                            else:
                                problems = check(content) if check else []
                            if problems:
                                rejected += 1
                                read["rejected"] = rejected + nudges
                                if report:
                                    report("reject", errors=problems[:3])
                                say("write", "rejected by the gate: "
                                    + "; ".join(problems[:2]))
                                messages.append({
                                    "role": "tool", "tool_call_id": call.get("id", ""),
                                    "name": name,
                                    "content": "error: the gate rejected this "
                                               "output: " + "; ".join(problems[:4])
                                               + ". Write the whole document "
                                               "again, fixed."})
                                continue
                        total.content = content
                        say("write", f"model called {name}: {len(content):,} chars")
                        return total, True, read
                else:
                    reply = f"error: unknown tool {name!r}"
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "name": name, "content": reply})
        total.error = f"no_write_output_after_{MAX_TOOL_TURNS}_turns"
        return total, False, read

    def _soma_reader(self, soma: Any, lock: Any, session_id: str,
                     chatset_id: str, stage: str = "",
                     build_chatsets: list | None = None):
        """The read side of the beavers' tools, answered from the Soma store."""
        import contextlib
        from .hashes import canonical_json, chatset_hash_for
        from .soma_bridge import SomaBridge
        guard = lock if lock is not None else contextlib.nullcontext()

        def read_data(name: str, args: dict) -> str:
            cid = args.get("chatset_id") or chatset_id
            if cid != chatset_id:
                return f"error: this run is chat set {chatset_id!r}"
            with guard:
                if name == "get_chatset" and build_chatsets:
                    # A build is every chat set of its sessions, merged in
                    # order and deduplicated by hash, each read from the Soma
                    # and checked against its own stored hash.
                    merged: list = []
                    seen: set = set()
                    bridge = SomaBridge(soma)
                    for csid in build_chatsets:
                        node = soma.get_node(csid)
                        want = (node.properties.get("chatset_hash") if node else "") or ""
                        turns = bridge.read_chat_set(csid)
                        if not want or chatset_hash_for(turns) != want:
                            return (f"error: MISSING_SOMA_DATA: chat set {csid} is "
                                    f"missing from the Soma or does not match its hash")
                        if want in seen:
                            continue
                        seen.add(want)
                        merged.extend(turns)
                    return canonical_json({
                        "session_id": session_id, "chatset_id": cid,
                        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                       time.gmtime()),
                        "chatset_hash": chatset_hash_for(merged),
                        "raw_chat_set": merged})
                if name == "get_chatset":
                    node = soma.get_node(cid)
                    want = (node.properties.get("chatset_hash") if node else "") or ""
                    if not want:
                        return f"error: MISSING_SOMA_DATA: chat set {cid} is not in the Soma"
                    turns = SomaBridge(soma).read_chat_set(cid)
                    if chatset_hash_for(turns) != want:
                        return (f"error: MISSING_SOMA_DATA: chat set {cid} in the "
                                f"Soma does not match its stored hash")
                    return canonical_json({
                        "session_id": session_id, "chatset_id": cid,
                        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                       time.gmtime()),
                        "chatset_hash": want, "raw_chat_set": turns})
                node_type = args.get("node_type")
                if node_type != "StageOutput":
                    return f"error: node_type {node_type!r} is not readable"
                found = []
                for n in soma.query_by_type(node_type):
                    if node_type == "StageOutput":
                        # the engine's convention: build == the chat set id.
                        # A build id also reaches the stage outputs of the
                        # chat sets it is made of.
                        owner = n.properties.get("build")
                        if owner != cid and owner not in (build_chatsets or ()):
                            continue
                        found.append({"stage": n.properties.get("stage"),
                                      "chatset_id": owner,
                                      "output": n.properties.get("output")})
                return canonical_json(found)
        return read_data

    def _report(self, kind: str, **fields: Any) -> dict | None:
        """Send one stage event to the engine's log. Logging only: never raises."""
        tk = getattr(self, "_tk", None)
        fn = getattr(tk, "report_stage", None)
        if fn is None:
            return None
        tries = START_TRIES if kind == "start" else 1
        for attempt in range(tries):
            try:
                got = fn(kind, **fields)
            except Exception:
                got = None
            if got is not None or kind != "start":
                return got
            if attempt + 1 < tries:
                time.sleep(START_BACKOFF_S * (attempt + 1))
        print(f"[stage log] could not register stage {fields.get('stage')!r} with the "
              f"engine log after {tries} tries; it will register from its first reply",
              file=sys.stderr, flush=True)
        return None

    def call_stage(self, stage: str, *, session_id: str, chatset_id: str,
                   expected_hash: str | None = None,
                   temperature: float | None = None,
                   soma: Any = None,
                   raw_chat_set: list[dict] | None = None,
                   session_code: str | None = None,
                   emit: Any = None,
                   soma_lock: Any = None,
                   build_chatsets: list | None = None,
                   tool_hooks: dict | None = None) -> Any:
        """Run one stage and tell the engine's log what happened to it.

        The engine's log can only see that HTTP calls came back. This reports
        the stage's own life: it started (the engine hands out the session and
        stage id, used by every turn), each reply, each write the gate sent
        back, and the gate's verdict. The verdict is the only thing that says
        pass or fail; the engine's SUCCESS on a call never does.
        """
        started = time.monotonic()
        ids = self._report("start", stage=stage, session=session_code,
                           chatset_id=chatset_id) or {}
        sid, stage_id = ids.get("session"), ids.get("stage_id")
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(HEARTBEAT_S):
                report("ping")

        def adopt(session: Any, engine_stage: Any) -> None:
            """The start report failed: register the stage under the ids the engine gave its reply."""
            nonlocal sid, stage_id
            if not (sid and stage_id) and session and engine_stage:
                sid, stage_id = session, engine_stage
                threading.Thread(target=beat, daemon=True).start()

        def report(kind: str, **fields: Any) -> None:
            adopt(fields.pop("engine_session", None), fields.pop("engine_stage", None))
            if sid and stage_id:
                self._report(kind, session=sid, stage_id=stage_id, stage=stage, **fields)

        if sid and stage_id:
            threading.Thread(target=beat, daemon=True).start()

        try:
            res = self._call_stage(
                stage, session_id=session_id, chatset_id=chatset_id,
                expected_hash=expected_hash, temperature=temperature, soma=soma,
                raw_chat_set=raw_chat_set, session_code=sid or session_code,
                emit=emit, soma_lock=soma_lock, build_chatsets=build_chatsets,
                tool_hooks=tool_hooks, stage_id=stage_id, report=report)
        except Exception as exc:
            stop.set()
            report("end", status="FAIL", errors=[f"{type(exc).__name__}: {exc}"[:300]],
                   seconds=round(time.monotonic() - started, 1))
            raise
        finally:
            stop.set()
        rec, gate = res.record, res.record.gate
        adopt(getattr(rec, "engine_session", None), getattr(rec, "engine_stage", None))
        if res.ok and getattr(gate, "skipped", False):
            status = "SKIP"
        else:
            status = "PASS" if res.ok else "FAIL"
        rejections = int(getattr(rec, "rejections", 0) or 0)
        report("end", status=status, verdict=str(getattr(gate, "verdict", "") or ""),
               errors=list(getattr(gate, "errors", []) or [])[:6],
               warnings=[str(w)[:200] for w in (getattr(gate, "warnings", None) or [])][:6],
               model=rec.model,
               turns=len(rec.turns or []), rejections=rejections,
               writes=rejections + (1 if rec.raw_response else 0),
               tokens=int(rec.total_tokens or 0),
               seconds=round(time.monotonic() - started, 1))
        return res

    def _call_stage(self, stage: str, *, session_id: str, chatset_id: str,
                   expected_hash: str | None = None,
                   temperature: float | None = None,
                   soma: Any = None,
                   raw_chat_set: list[dict] | None = None,
                   session_code: str | None = None,
                   emit: Any = None,
                   soma_lock: Any = None,
                   build_chatsets: list | None = None,
                   tool_hooks: dict | None = None,
                   stage_id: str | None = None,
                   report: Any = None) -> Any:
        """Call one stage and verify its output before it is trusted.

        ``session_code`` is the id the engine generated for this session (None
        on its first call, so the engine generates one); see ``engine_sessions``.

        ``emit(label, text)``, when given, is called at each real step, at the
        moment it happens, so the operator watches the work live instead of
        reading a dump after the call. Every value in ``text`` is read from
        the step that just ran; nothing is estimated. A fault in ``emit`` is
        swallowed, because a display problem must never fail a stage.

        Instructions come from the ``stage_instructions`` row for this stage,
        never from the markdown files on disk. A stage is told its own job and
        nothing about the other stages, so it has no pipeline to reason about.

        When ``raw_chat_set`` is supplied (distiller and introbeaver), every
        emitted entry must match the source text at the offsets it claims.
        A provenance failure fails the stage: the model's claim is not
        allowed into the graph unverified.
        """
        from .gate import (GateResult, summarize, validate, is_done,
                           is_hard_block)
        from .provenance import (verify_distiller, verify_introbeaver,
                                 verify_oc_slots)

        # The ENGINE generates the session id; the pipeline does not make one
        # up. ``session_code`` is the id the engine already generated for this
        # session, or None on the session's first call.
        #   * None  -> nothing is sent, which tells the engine "generate one";
        #              its reply names it (engine_session) and the caller keeps it.
        #   * a code -> sent as pea_session, which tells the engine the id is
        #              already assigned, so it does not generate another.
        # Never fall back to the chat-session name or to a local counter such
        # as SES-007: sending those made every re-run of the same chat session
        # land in one engine session. See engine_sessions.py and
        # session-stage-linkage.md.
        pea_session = session_code or None

        def say(label: str, text: str) -> None:
            """Show one real event now. Never lets a display fault fail the stage."""
            if emit is None:
                return
            try:
                emit(label, text)
            except Exception:
                pass

        # Nothing is handed to the model: no prompt, no payload, no chunk. It is
        # given tools and has to call them. It reads its instructions and its
        # input from the Soma and writes its output there. If the Soma cannot
        # supply what the stage needs, the stage halts with MISSING_SOMA_DATA.
        if stage not in BEAVER_STAGES | BUILD_STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        if soma is None:
            raise RuntimeError(
                f"MISSING_SOMA_DATA: stage {stage!r} was called without a Soma")
        instruction = soma.get_stage_instruction(stage)
        if instruction is None:
            raise RuntimeError(
                f"MISSING_SOMA_DATA: stage {stage!r} has no row in "
                f"stage_instructions. Instructions are read from the database, "
                f"not from disk; build the store from soma_template.sql first.")
        body = instruction["body"]
        # A hook is (description, function) or (description, function,
        # parameters); without parameters it takes just the chat set id.
        default_params = {"type": "object",
                          "properties": {"chatset_id": {"type": "string"}},
                          "required": ["chatset_id"]}
        tools = list(SOMA_BEAVER_TOOLS) + [
            {"type": "function", "function": {
                "name": hook, "description": spec[0],
                "parameters": spec[2] if len(spec) > 2 else default_params}}
            for hook, spec in (tool_hooks or {}).items()]
        read_data = self._soma_reader(soma, soma_lock, session_id,
                                      chatset_id, stage, build_chatsets)
        if temperature is None:
            temperature = (DEFAULT_EXTRACTIVE_TEMPERATURE
                           if stage in _EXTRACTIVE_STAGES else None)
        say("request",
            f"sent · tools {', '.join(t['function']['name'] for t in tools)} · "
            f"tool_choice required · temperature "
            f"{temperature if temperature is not None else 'model default'} · "
            f"waiting for the model")
        def check(content: str) -> list:
            """What is wrong with a write, in words the model can act on.

            The gate's own errors, and then provenance: every quote must be an
            exact substring of the message it names. A misquote handed back
            with the failing text is one the model can fix by copying from the
            chat set it read; found only after the loop it can only fail.
            """
            if stage == "distiller" and raw_chat_set is not None:
                # v3.0.2: the validator is the single source of truth. The old
                # gate demands a legacy skip field this contract deliberately
                # removed, so it is not run here.
                from . import distiller_validator as dv
                ok, err, detail, doc, retryable = dv.validate_output(
                    content, chatset_id, expected_hash, raw_chat_set)
                if not ok:
                    return [f"{err}: {detail}" if detail else err]
                return []
            g = validate(stage, content, expected_hash=expected_hash)
            if not is_done(g):
                return list(g.errors)
            if raw_chat_set is not None and g.parsed and not g.skipped:
                verify = {"distiller": verify_distiller,
                          "introbeaver": verify_introbeaver}.get(stage)
                if verify is not None:
                    prov = verify(g.parsed, raw_chat_set)
                    if not prov.ok:
                        return [str(i) for i in prov.issues[:6]]
            if stage == "oc" and g.parsed and g.verdict.upper() in ("APPROVE", "CONDITIONAL"):
                prov = verify_oc_slots(g.parsed, _drafted_files(soma, soma_lock, session_id))
                if not prov.ok:
                    return [str(i) for i in prov.issues[:6]]
            return []

        result, written, read = self._run_tool_loop(
            stage, chatset_id, body, instruction, read_data, tools=tools,
            query_is_input=stage in BUILD_STAGES,
            hooks=tool_hooks, check=check,
            pea_session=pea_session, temperature=temperature, say=say,
            stage_id=stage_id, report=report)
        sys_prompt = body if read["instructions"] else ""
        user_msg = "\n".join(read["served"])
        say("response",
            f"{result.model_id} replied · {len(result.content or ''):,} chars written · "
            f"tokens in {_tok(result.prompt_tokens)} / out {_tok(result.completion_tokens)} · "
            f"{result.latency_ms / 1000:.1f}s · engine "
            f"{result.engine_session or '—'}/{result.engine_stage or '—'}")
        _v32_validated = False
        if read["missing"]:
            # The Soma could not supply what the stage reads. Whatever the
            # model wrote on top of that is not trusted: the stage halts.
            gate = GateResult(False, stage, errors=[
                "MISSING_SOMA_DATA: " + "; ".join(
                    m.split("MISSING_SOMA_DATA: ", 1)[-1] for m in read["missing"])])
        elif written:
            if stage == "distiller" and raw_chat_set is not None:
                from . import distiller_validator as dv
                ok, err, detail, doc, retryable = dv.validate_output(
                    result.content, chatset_id, expected_hash, raw_chat_set)
                if ok:
                    _v32_validated = True
                    skip, reason, exfil = dv.derive_verdict(doc)
                    gate = GateResult(
                        True, stage, parsed=doc,
                        skipped=skip, skip_reason=reason or "",
                        exfil_type=exfil or "",
                        verdict="skip" if skip else "PASS")
                    gate.accomplishment_summary = summarize(gate)
                else:
                    gate = GateResult(False, stage, errors=[
                        f"{err}: {detail}" if detail else err])
                    gate.accomplishment_summary = summarize(gate)
            else:
                gate = validate(stage, result.content, expected_hash=expected_hash)
        else:
            gate = GateResult(False, stage, errors=[result.error])
        _say_gate(say, gate, is_done)

        # ── provenance: every function slot is a cut of the drafter's files ──
        if stage == "oc" and gate.parsed and is_done(gate) \
                and gate.verdict.upper() in ("APPROVE", "CONDITIONAL"):
            prov = verify_oc_slots(gate.parsed, _drafted_files(soma, soma_lock, session_id))
            gate.provenance = prov.summary()
            say("anchors", f"{prov.verified}/{prov.checked} function slots found in the draft"
                + ("" if prov.ok else " · REJECTED: " + "; ".join(str(i) for i in prov.issues[:2])))
            if not prov.ok:
                gate.errors.extend(str(i) for i in prov.issues)
                gate.ok = False

        # ── provenance: the claim must exist in the source ──────────────
        # Distiller: the v3.0.2 validator already re-segments and requires exact
        # text == segment, so the legacy verify_distiller pass does not run.
        if raw_chat_set is not None and gate.parsed and is_done(gate):
            if stage == "distiller" and _v32_validated:
                prov = None
            elif stage == "distiller":
                prov = verify_distiller(gate.parsed, raw_chat_set)
            elif stage == "introbeaver":
                prov = verify_introbeaver(gate.parsed, raw_chat_set)
            else:
                prov = None
            if prov is not None:
                say("anchors",
                    f"{prov.verified}/{prov.checked} quotes found in the transcript "
                    f"(offsets located by Python)"
                    + (f" · {len(prov.ambiguous)} repeated" if prov.ambiguous else "")
                    + ("" if prov.ok else " · REJECTED: " + "; ".join(
                        str(i) for i in prov.issues[:2])))
            if prov is not None and not prov.ok:
                gate.errors.extend(str(i) for i in prov.issues)
                gate.ok = False
                gate.provenance = prov.summary()
            if prov is not None:
                # The counts must describe the entries that survived the
                # check. The model's arrays still contain the rejected ones,
                # so counting what the model wrote would overstate the stage.
                gate.accomplishment_summary = summarize(gate, prov.accepted)
            if stage == "distiller" and prov is not None and prov.ok:
                # The arithmetic is Python's: bytes, ratios, noise counts and
                # the two numeric skip rules overwrite anything the model sent.
                from . import measure
                m = measure.apply(gate, prov.accepted, raw_chat_set)
                b = (m.get("bytes") or {}) if isinstance(m, dict) else {}
                say("python",
                    f"measured: {b.get('original', '—')} bytes in, "
                    f"{b.get('signal', '—')} signal, {b.get('noise', '—')} noise · "
                    f"signal quality {m.get('signal_quality')} · "
                    + (f"SKIP {m.get('skip_reason')}" if m.get("skip") else "proceed"))
            if stage == "introbeaver" and prov is not None and prov.ok:
                # Counts and confidence are arithmetic on what the model quoted
                # (TB-004 SCORING); Python's values overwrite the model's.
                from . import intro_measure
                n_warn = len(gate.warnings)
                m = intro_measure.apply(gate, raw_chat_set)
                body = (gate.parsed or {}).get("introbeaver_output") or {}
                confs = [e.get("confidence") for e in body.get("entries") or []
                         if isinstance(e, dict)]
                say("python",
                    f"computed: {m.get('counts', {}).get('extracted_count', '—')} entries "
                    f"{m.get('counts', {}).get('entry_count_by_type', {})} · "
                    f"signal quality {m.get('signal_quality', '—')} · "
                    f"confidence {confs} · status {m.get('status', '—')} · "
                    f"{len(gate.warnings) - n_warn} correction(s) to the model's reply")
            if stage == "outrobeaver":
                # Hashes, per-turn scores and session health are Python's too
                # (TB-006 SCORING); the model's values are overwritten.
                from . import outro_measure
                n_warn = len(gate.warnings)
                m = outro_measure.apply(gate, raw_chat_set)
                if m.get("applied"):
                    say("python",
                        f"computed: health {m.get('session_health')} (score {m.get('score')}"
                        f"{', override: ' + m['override'] if m.get('override') else ''}) · "
                        f"{m.get('journal_entries', '—')} journal hashes · "
                        f"{len(gate.warnings) - n_warn} correction(s) to the model's reply")
                else:
                    say("python", f"health not computed: {m.get('why')}")

        # ── the model's write lands in the Soma ──────────────────────────
        # Same node the engine's _save_artifact keeps for every stage
        # (id ``stage:<chatset>:<stage>``), so there is one node per stage and
        # a re-run replaces it. What is stored is what survived the gate, the
        # provenance check and Python's own measurements, not the model's raw
        # text. The engine adds the HANDOFF_TO edge to the previous stage.
        if is_done(gate) and gate.parsed is not None:
            import contextlib
            out_id = f"stage:{chatset_id}:{stage}"
            if stage == "distiller" and _v32_validated:
                from . import distiller_validator as dv
                with (soma_lock if soma_lock is not None else contextlib.nullcontext()):
                    dv.write_stage_output(soma, chatset_id, stage, gate.parsed, expected_hash)
                say("soma", f"StageOutput node written: {out_id}")
            else:
                with (soma_lock if soma_lock is not None else contextlib.nullcontext()):
                    soma.create_node("StageOutput", {
                        "id": out_id, "build": chatset_id, "stage": stage,
                        # same wrap as the engine's _save_artifact
                        "output": (gate.parsed if isinstance(gate.parsed, (dict, list))
                                   else {"value": str(gate.parsed)}),
                        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     time.gmtime())})
                say("soma", f"StageOutput node written: {out_id}")

        rec = CallRecord(
            stage=stage,
            session_id=session_id,
            chatset_id=chatset_id,
            model=result.model_id,
            status="pass" if is_done(gate) else ("blocked" if is_hard_block(gate) else "fail"),
            attempt=1,
            attempts=[{"attempt": 1, "model": result.model_id}],
            gate=gate,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            cache_hit_tokens=result.cache_hit_tokens,
            cache_miss_tokens=result.cache_miss_tokens,
            chars_in=result.chars_in,
            latency_ms=result.latency_ms,
            engine_session=result.engine_session,
            engine_stage=result.engine_stage,
            log_link=result.log_link,
            raw_response=result.content,
            sys_prompt=sys_prompt,
            user_msg=user_msg,
            transcript=read["messages"],
            turns=read["turns"],
            rejections=read["rejected"],
            provider_served=result.provider,
            upstream_provider=result.upstream_provider,
        )

        class _Res:
            pass
        res = _Res()
        res.ok = is_done(gate)
        res.record = rec
        res.parsed = gate.parsed
        res.skipped = gate.skipped
        res.verdict = gate.verdict
        return res
