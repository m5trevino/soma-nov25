"""TurnKey SDK main module.

Slice: TB-TK-007 (SDK Main Class) — implements the main SDK class.
Previous slices established the package structure, types, profile loader, rotator, 
and stage pinning.

Hard gates:
- Must integrate with existing profile structure
- Must maintain all existing functionality
- Must load profile at init
- Must initialize rotator with deck and stages
- Must provide run_stage() interface

Enhancements (TB-TK-011, TB-TK-017, TB-TK-024):
- Normalize ``deck`` entries (card objects ``{"modelId": ...}`` → model-id strings)
  so profiles like the Dendritic turn-key profiles work without modification.
- Normalize ``stages`` (list of ``{"stageName": ..., "pinnedModels": ...,
  "useDeck": ...}`` → dict keyed by stage name) for the ``DeckRotator``.
- Load ``timeout``, ``max_retries`` and ``engine_url`` from the profile's
  ``pipeline`` section when present, falling back to sensible defaults.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Dict, List

from .loader import load_profile
from .gateway_rotator import GatewayDeckRotator
from .types import SDKConfig, SDKError, Profile, NormalizedResponse, StageError


#: Default engine URL (matches the Peacock engine convention).
_DEFAULT_ENGINE_URL: str = "http://engine.save-aichats.com:3099"
#: Default per-call timeout in seconds.
_DEFAULT_TIMEOUT: int = 180
#: Default maximum retry attempts.
_DEFAULT_MAX_RETRIES: int = 10


# Models that cannot serve a chat completion regardless of what the catalog
# says: embedding, reranking, compression, transcription and vision utilities.
# They appear "active" in /v1/models because that endpoint lists everything the
# gateway can bill for, not everything that answers a chat request. Leaving
# them in the deck means spending a call on a hard 400 every lap.
_NON_CHAT = re.compile(
    r"(embed|rerank|scaledown/|classify|moderation|whisper|tts|ocr|"
    r"dall-e|flux|stable-diffusion|image-|video-)",
    re.IGNORECASE,
)


# Gateway names the SDK invents when a profile card carries no provider. They
# are placeholders, not real gateways, and must never be sent to the engine.
_PLACEHOLDER_PROVIDERS = frozenset({"", "unknown", "default"})


def _provider_required_message(model: str, stage: Optional[str],
                               given: Optional[str]) -> str:
    """Say what is wrong and exactly how to add the provider."""
    where = f" (stage '{stage}')" if stage else ""
    shown = "nothing" if given is None else f"'{given}'"
    return (
        f"PROVIDER MISSING for model '{model}'{where}: got {shown}.\n"
        "Every engine call must send the model and the provider as two "
        "separate fields, e.g. {\"model\": \"" + model + "\", \"provider\": "
        "\"openrouter\"}. The engine is not allowed to choose the gateway.\n"
        "How to fix it:\n"
        "  1. In the TurnKey profile JSON, give this model's deck card a "
        "\"provider\":\n"
        "       {\"modelId\": \"" + model + "\", \"provider\": "
        "\"openrouter\"}   (use openrouter, novita, xkiro, ... - the gateway "
        "that should serve it)\n"
        "  2. A deck written as bare model names has no provider at all; use "
        "cards with modelId + provider.\n"
        "  3. Calling the SDK directly: pass provider=..., e.g. "
        "sdk._call_engine(model, messages, provider=\"openrouter\")."
    )


def is_chat_model(model_id: str) -> bool:
    """Whether a model id can answer a /v1/chat/completions request."""
    return bool(model_id) and not _NON_CHAT.search(model_id)


def _card_from_models_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Map one ``GET /v1/models`` entry onto the card shape ``sync`` reads.

    ``/v1/models`` names things the engine's own way (``gateway``,
    ``tools_supported``, ``context_window``, flat ``*_price_1m``). The ``id``
    and ``gateway`` pair is the engine's registry key, which is exactly what
    a chat request names as ``model`` and ``provider``.
    """
    gateway = entry.get("gateway") or "unknown"
    pricing: dict[str, Any] = {}
    if entry.get("input_price_1m") is not None:
        pricing = {
            "input_1m": entry.get("input_price_1m"),
            "output_1m": entry.get("output_price_1m"),
            "cache_read_1m": entry.get("cache_read_price_1m"),
            "known": True,
        }
    return {
        "modelId": entry.get("id"),
        "modelName": entry.get("display_name") or entry.get("id"),
        "provider": gateway,
        "meta": {"provider": gateway},
        "pricing": pricing,
        "classification": {"free_kind": entry.get("free_kind")},
        "limits": {"context": entry.get("context_window"),
                   "max_output": entry.get("max_completion_tokens")},
        "capabilities": {"text": True,
                         "tools": bool(entry.get("tools_supported")),
                         "image": bool(entry.get("vision")),
                         "reasoning": bool(entry.get("reasoning"))},
    }


def _aggregator_gateways() -> set[str]:
    """Gateways whose responses name the upstream provider, not themselves.

    OpenRouter does: a call asked of ``openrouter`` is answered with
    ``"provider": "Fireworks"``. The provider check must not reject those.
    ``SOMA_AGGREGATOR_GATEWAYS`` (comma separated) adds more.
    """
    extra = os.environ.get("SOMA_AGGREGATOR_GATEWAYS", "")
    return {"openrouter"} | {g.strip().lower() for g in extra.split(",") if g.strip()}


def _normalize_gateway_models(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize profile models into gateway structure for GatewayDeckRotator.
    
    Converts the deck structure into the expected gateway format with:
    - gateway: provider name
    - models: list of model dictionaries with 'id' and 'name'
    
    Args:
        profile: The loaded profile dictionary
        
    Returns:
        A list of gateway configurations for the GatewayDeckRotator
    """
    # Get the deck from the profile
    deck = profile.get("deck", [])
    
    if not isinstance(deck, list):
        return []
    
    # Group models by provider/gateway
    gateway_groups: dict[str, list[dict[str, str]]] = {}
    skipped_utility: list[str] = []
    
    for item in deck:
        if isinstance(item, dict):
            # Extract model ID and provider
            model_id = item.get("modelId") or item.get("modelName")
            provider = item.get("provider", "unknown")
            
            if model_id and not is_chat_model(model_id):
                # Utility model: skip it rather than burn a call on a 400.
                skipped_utility.append(model_id)
            elif model_id:
                # Create model entry
                model_entry = {
                    "id": model_id,
                    "name": model_id  # Using model_id as name for simplicity
                }
                
                # Group by provider
                if provider not in gateway_groups:
                    gateway_groups[provider] = []
                gateway_groups[provider].append(model_entry)
    
    # Convert to the expected format for GatewayDeckRotator
    result = []
    for gateway_name, models in gateway_groups.items():
        result.append({
            "gateway": gateway_name,
            "models": models
        })
    
    return result


def _normalize_deck(deck: List[Any]) -> List[str]:
    """Normalize a profile ``deck`` into a list of model-id strings.

    Accepts either a list of plain model-id strings or a list of card
    objects of the form ``{"uid": ..., "modelId": ..., "modelName": ...,
    "provider": ...}`` (the Dendritic turn-key profile format).  Card
    objects are mapped to their ``modelId`` (falling back to ``modelName``).

    Args:
        deck: The raw ``deck`` value from the profile.

    Returns:
        A list of model-id strings.
    """
    if not isinstance(deck, list):
        return []
    normalized: List[str] = []
    for card in deck:
        if isinstance(card, str):
            normalized.append(card)
        elif isinstance(card, dict):
            model_id = card.get("modelId") or card.get("modelName") or ""
            if isinstance(model_id, str) and model_id:
                normalized.append(model_id)
    return normalized


def _normalize_stages(stages: Any) -> Dict[str, Any]:
    """Normalize a profile ``stages`` value into a stage-keyed dict.

    Accepts either a dict (already keyed by stage name) or a list of
    stage-config objects of the form ``{"stageName": ..., "pinnedModels":
    [...], "useDeck": bool}`` (the Dendritic turn-key profile format).

    Args:
        stages: The raw ``stages`` value from the profile.

    Returns:
        A dict keyed by stage name, each value a config dict with
        ``pinnedModels`` and ``useDeck``.
    """
    if isinstance(stages, dict):
        return stages
    if not isinstance(stages, list):
        return {}
    out: Dict[str, Any] = {}
    for entry in stages:
        if not isinstance(entry, dict):
            continue
        name = entry.get("stageName") or entry.get("name") or entry.get("stage")
        if not isinstance(name, str) or not name:
            continue
        out[name] = {
            "pinnedModels": entry.get("pinnedModels", []) or [],
            "useDeck": bool(entry.get("useDeck", True)),
        }
    return out


def _load_dotenv() -> None:
    """Load every .env found, nearest first, without stopping at the first.

    An unrelated ~/.env used to win purely because it was found first, leaving
    ENGINE_KEY unset and every call returning 401. All candidate files are now
    merged and no file aborts the search. Existing environment values still
    win, so an explicit export overrides any file.
    """
    candidates: list[Path] = []
    try:
        candidates.append(Path.cwd() / ".env")
    except OSError:
        pass
    for base in (Path(__file__).resolve(), Path.cwd()):
        for parent in list(base.parents)[:6]:
            candidates.append(parent / ".env")

    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(),
                                      value.strip().strip('"').strip("'"))
        except OSError:
            continue


_load_dotenv()


class SDK:
    """Main SDK class for TurnKey.

    Orchestrates profile loading, model rotation, and HTTP calls to the
    engine service.

    Args:
        config: An ``SDKConfig`` instance.  When ``None`` a default config
            is built from the profile's ``pipeline`` section (falling back
            to sensible defaults: 180s timeout, 10 retries, the Peacock
            engine URL).
        profile_path: Path to the profile file. When ``None`` the default
            convention ``peacock_profile.json`` in the current working directory
            is used.

    Example:
        >>> sdk = SDK(SDKConfig(engine_url="http://localhost:3099"))
        >>> raw = sdk._call_engine("gpt-4", messages=[{"role": "user", "content": "hello"}], provider="openrouter")
        >>> isinstance(raw, dict)
        True
    """

    def __init__(self, config: Optional[SDKConfig] = None,
                 profile_path: Optional[str] = None) -> None:
        self.profile_path = profile_path
        self._call_count: int = 0  # tracks how many engine calls have been made

        # Load profile at init (raises on missing/invalid profiles —
        # TB-TK-024 fail-fast).
        self.profile = load_profile(profile_path)

        # --- configuration (TB-TK-011) -----------------------------------
        # Prefer an explicit config; otherwise derive from the profile's
        # ``pipeline`` section, then fall back to defaults.
        pipeline_cfg = self.profile.get("pipeline") or {}
        if not isinstance(pipeline_cfg, dict):
            pipeline_cfg = {}
        profile_timeout = pipeline_cfg.get("timeout", _DEFAULT_TIMEOUT)
        profile_max_retries = pipeline_cfg.get("maxRetries",
                                               pipeline_cfg.get("max_retries",
                                                                _DEFAULT_MAX_RETRIES))
        profile_engine_url = (pipeline_cfg.get("engineUrl")
                              or pipeline_cfg.get("engine_url")
                              or os.environ.get("ENGINE_URL")
                              or _DEFAULT_ENGINE_URL)
        # Engine key for inference routes. The engine rejects unauthenticated
        # chat/completions calls; every other route stays open. The profile is
        # authoritative, and ENGINE_KEY is honoured so a freshly built profile
        # does not fail with 401s.
        self._engine_key = str(pipeline_cfg.get("engineKey")
                               or pipeline_cfg.get("engine_key")
                               or os.environ.get("ENGINE_KEY")
                               or "")

        # Stage configs carry pinnedModels / useDeck. They were normalized by
        # _normalize_stages but never stored, so pinning a model in a profile
        # had no effect: every stage drew from the deck regardless.
        self.stages = _normalize_stages(self.profile.get("stages"))

        if config is not None:
            self.config = config
        else:
            self.config = SDKConfig(
                engine_url=profile_engine_url,
                timeout=profile_timeout,
                max_retries=profile_max_retries,
            )

        # --- normalized gateway models (TB-TK-017, TB-TK-024) -----------
        # Use gateway-based rotation for fair distribution
        gateway_models = _normalize_gateway_models(self.profile)
        
        if not gateway_models:
            # Try to fall back to the deck directly if no gateways found
            deck = self.profile.get("deck", [])
            if isinstance(deck, list):
                # Extract model IDs from deck items and wrap as a single
                # "default" gateway so the GatewayDeckRotator doesn't choke.
                fallback_models = []
                for item in deck:
                    if isinstance(item, str):
                        fallback_models.append(item)
                    elif isinstance(item, dict) and "modelId" in item:
                        fallback_models.append(item["modelId"])
                gateway_models = [{
                    "gateway": "default",
                    "models": [{"id": m, "name": m} for m in fallback_models],
                }]
        
        if not gateway_models:
            raise SDKError(
                "Profile must contain at least one model",
                error_code="INVALID_PROFILE",
            )

        # Initialize rotator with gateway models
        self.rotator = GatewayDeckRotator(gateway_models)
        _pins = {
            name: cfg.get("pinnedModels") or []
            for name, cfg in (self.stages or {}).items()
        }
        self.rotator.set_stage_pins(_pins)

    # ------------------------------------------------------------------ engine

    def _auth_headers(self) -> dict[str, str]:
        """Request headers for the engine, including the key when configured."""
        headers = {"Content-Type": "application/json"}
        key = getattr(self, "_engine_key", "")
        if key:
            headers["X-Peacock-Key"] = key
        return headers

    def _call_engine(
        self,
        model: str,
        messages: list[dict[str, str]],
        timeout: Optional[int] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[str | dict[str, Any]] = None,
        temperature: Optional[float] = None,
        pea_session: Optional[str] = None,
        stage: Optional[str] = None,
        stage_id: Optional[str] = None,
        *,
        provider: Optional[str] = None,
    ) -> dict[str, Any]:
        """Make an HTTP POST to the engine's ``/v1/chat/completions`` endpoint.

        ``model`` and ``provider`` are sent as two separate fields on every
        call. The engine does not get to choose the gateway: a model id that
        several gateways offer was being served by whichever the engine
        preferred, not the one the profile named. ``provider`` is required;
        omitting it, or an engine reply from a different provider, fails the
        call instead of falling back to the engine's choice.

        Builds a conformant ``chat.completions`` JSON payload, serialises it,
        POSTs to ``{engine_url}/v1/chat/completions``, and returns the parsed
        JSON response as a plain ``dict``.

        **Error handling**

        | Condition | Raised |
        |---|---|
        | Connection refused / DNS failure | ``SDKError(error_code="CONNECTION_ERROR")`` |
        | Timeout (exceeds *timeout*) | ``SDKError(error_code="TIMEOUT")`` |
        | Non-2xx HTTP status | ``SDKError(error_code="HTTP_<status>")`` |
        | Non-JSON response body | ``SDKError(error_code="PARSE_ERROR")`` |

        Args:
            model: Model identifier (drawn from the deck or pinned stage).
            messages: List of message dicts with ``role`` and ``content`` keys.
            timeout: Per-call timeout in seconds.  Falls back to
                ``self.config.timeout`` when ``None``.
            tools: Optional list of tool definitions to pass through.
            tool_choice: Optional tool-choice directive to pass through.
            temperature: Optional sampling temperature to pass through.

        Returns:
            The raw JSON response from the engine as a ``dict``.

        Raises:
            SDKError: On any network, HTTP, or parsing failure.
            ValueError: If *messages* is empty.
        """
        # --- input guard --------------------------------------------------
        if not messages:
            raise ValueError("messages must contain at least one entry")
        if (not isinstance(provider, str)
                or provider.strip().lower() in _PLACEHOLDER_PROVIDERS):
            raise SDKError(
                _provider_required_message(model, stage, provider),
                error_code="PROVIDER_REQUIRED",
            )
        provider = provider.strip()

        # --- build payload ------------------------------------------------
        payload: dict[str, Any] = {
            "model": model,
            "provider": provider,
            "messages": messages,
            "stream": False,
        }

        # Attach optional fields only when provided.
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if temperature is not None:
            payload["temperature"] = temperature
        if pea_session:
            payload["pea_session"] = pea_session
        if stage:
            payload["stage"] = stage
        if stage_id:
            # the engine numbers every request; this keeps one stage one number
            payload["soma_stage_id"] = stage_id

        body = json.dumps(payload).encode("utf-8")
        url = f"{self.config.engine_url}/v1/chat/completions"
        effective_timeout = timeout if timeout is not None else self.config.timeout

        # --- request ------------------------------------------------------
        req = urllib.request.Request(
            url,
            data=body,
            headers=self._auth_headers(),
            method="POST",
        )

        self._call_count += 1

        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                raw_bytes = resp.read()
                try:
                    data = json.loads(raw_bytes.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise SDKError(
                        f"Engine returned non-JSON response: {exc}",
                        error_code="PARSE_ERROR",
                    ) from exc
            # The engine says which gateway actually served the call. If it
            # names a different one than we asked for, the call did not go
            # where the profile said it should. Fail it; do not accept it.
            if isinstance(data, dict):
                served = data.get("provider") or data.get("gateway")
                if (isinstance(served, str) and served.strip()
                        and served.strip().lower() != provider.lower()
                        and provider.lower() in _aggregator_gateways()):
                    # An aggregator (OpenRouter) names the company that served
                    # the call in ``provider``: Fireworks, Mistral, Nvidia. That
                    # is routing inside the gateway we asked for, not a call
                    # that went somewhere else. Keep the answer, and keep the
                    # name so it can be logged.
                    data.setdefault("upstream_provider", served.strip())
                    served = None
                if isinstance(served, str) and served.strip() \
                        and served.strip().lower() != provider.lower():
                    raise SDKError(
                        f"PROVIDER MISMATCH: asked provider '{provider}' for "
                        f"model '{model}' but the engine served it from "
                        f"'{served}'.\nThe call was rejected, not accepted.\n"
                        "How to fix it: check the engine honors the "
                        "\"provider\" request field, and that provider "
                        f"'{provider}' actually offers '{model}' (see the "
                        "catalog); otherwise change this card's provider in "
                        "the profile to one that does.",
                        error_code="PROVIDER_MISMATCH",
                    )
            return data

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SDKError(
                f"HTTP {exc.code}: {detail}",
                error_code=f"HTTP_{exc.code}",
            ) from exc

        except urllib.error.URLError as exc:
            reason_str = str(exc.reason)
            if isinstance(exc.reason, urllib.error.HTTPError):
                # Re-wrapped HTTPError inside URLError.
                http_err: urllib.error.HTTPError = exc.reason
                raise SDKError(
                    f"HTTP {http_err.code}: {http_err.read().decode()}",
                    error_code=f"HTTP_{http_err.code}",
                ) from exc
            if "timed out" in reason_str.lower():
                raise SDKError(
                    f"Connection timed out after {effective_timeout}s",
                    error_code="TIMEOUT",
                ) from exc
            raise SDKError(
                f"Connection error: {reason_str}",
                error_code="CONNECTION_ERROR",
            ) from exc

        except TimeoutError as exc:
            raise SDKError(
                f"Request timed out after {effective_timeout}s",
                error_code="TIMEOUT",
            ) from exc

    # ------------------------------------------------------------------ stage execution

    def report_stage(self, kind: str, **fields: Any) -> Optional[dict[str, Any]]:
        """Tell the engine what a stage is doing, so its log can say so.

        ``start`` registers the stage and returns ``{"session", "stage_id"}``,
        which the engine hands out. ``turn``, ``reject`` and ``end`` report
        progress against that pair. This is logging: it never raises and never
        fails a stage. A failure is printed once and returns None.
        """
        path = "start" if kind == "start" else "event"
        body = json.dumps({"kind": kind, **fields}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.config.engine_url}/v1/soma/stage/{path}",
            data=body, headers=self._auth_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # logging must not break a stage
            if not getattr(self, "_report_warned", False):
                self._report_warned = True
                import sys
                print(f"[stage log] could not report {kind} to the engine: {exc}",
                      file=sys.stderr, flush=True)
            return None

    def run_stage(self, stage_name: str,
                  messages: list[dict[str, str]],
                  session_id: Optional[str] = None,
                  timeout: Optional[int] = None,
                  tools: Optional[list[dict[str, Any]]] = None,
                  tool_choice: Optional[str | dict[str, Any]] = None,
                  temperature: Optional[float] = None,
                  stage_id: Optional[str] = None) -> dict[str, Any]:
        """Run a specific stage using the configured model from the deck.

        This method:
        1. Gets the appropriate model for the stage from the rotator
        2. Makes an engine call with that model
        3. Retries on transient failures (connection errors, timeouts, 5xx),
           rotating models on each attempt
        4. Returns the response

        Args:
            stage_name: The name of the stage to run
            messages: List of message dicts with ``role`` and ``content`` keys.
            timeout: Per-call timeout in seconds.  Falls back to
                ``self.config.timeout`` when ``None``.
            tools: Optional list of tool definitions to pass through.
            tool_choice: Optional tool-choice directive to pass through.
            temperature: Optional sampling temperature to pass through.
            pea_session: Optional PEA session name — injected into the engine
                request so the engine writes forensic PEA files under a named
                session directory.

        Returns:
            The raw JSON response from the engine as a ``dict``.

        Raises:
            StageError: When all retries are exhausted. The exception contains
                the full retry history, every model that was attempted, and
                the root-cause exception.
            ValueError: If *messages* is empty.
        """
        # Get the appropriate model for this stage
        model_info = self.rotator.next_model(stage_name)
        model = model_info["model_id"]
        gateway = model_info["gateway"]

        # Execute the engine call with the determined model
        # Implement retry logic with model rotation
        max_retries = self.config.max_retries
        last_error = None

        # Keep track of attempted models for StageError reporting
        retry_history: list[dict[str, str]] = []
        failed_models: set[str] = set()
        failed_models.add(model)

        for attempt in range(max_retries + 1):
            try:
                result = self._call_engine(model, messages, timeout,
                                           tools, tool_choice, temperature,
                                           pea_session=session_id,
                                           stage=stage_name,
                                           stage_id=stage_id,
                                           provider=gateway)
                # Success — report it and reset this model's failure counter.
                # Preserve the engine's provider when it supplies one; otherwise
                # name the gateway that actually served this successful attempt,
                # not the route originally peeked before a retry rotated the deck.
                if isinstance(result, dict):
                    result.setdefault("provider", gateway)
                self.rotator.report_success(gateway, model)
                return result
            except SDKError as e:
                # Report failure to the rotator's circuit breaker
                self.rotator.report_failure(gateway, model)

                # Check if we should retry based on error type
                should_retry = (
                    e.error_code in ("CONNECTION_ERROR", "TIMEOUT") or 
                    (e.error_code and e.error_code.startswith("HTTP_5"))
                )
                
                if not should_retry or attempt >= max_retries:
                    # Should not retry — build StageError with full diagnostic info
                    failed_models.add(model)
                    retry_history.append({
                        "model": model,
                        "error": e.error_code or "UNKNOWN",
                        "message": str(e),
                    })
                    raise StageError(
                        stage=stage_name,
                        message=(str(e) if e.error_code in
                                 ("PROVIDER_REQUIRED", "PROVIDER_MISMATCH")
                                 else f"All retries exhausted for stage '{stage_name}'"),
                        retry_history=retry_history,
                        failed_models=failed_models,
                        # The provider errors already carry the whole
                        # explanation in the message; repeating them as the
                        # root cause printed every line twice.
                        root_cause=(None if e.error_code in
                                    ("PROVIDER_REQUIRED", "PROVIDER_MISMATCH")
                                    else e),
                        error_code=e.error_code or "STAGE_FAILURE",
                        details={
                            "attempt_number": attempt + 1,
                            "max_retries": max_retries,
                        },
                    ) from e
                
                # Rotate to next model in deck and try again
                failed_models.add(model)
                retry_history.append({
                    "model": model,
                    "error": e.error_code or "UNKNOWN",
                    "message": str(e),
                })
                model_info = self.rotator.next_model(stage_name)
                model = model_info["model_id"]
                gateway = model_info["gateway"]
                last_error = e
        
        # Defensive fallback — should never be reached due to the raise above.
        raise StageError(
            stage=stage_name,
            message=f"Unknown error in retry loop for stage '{stage_name}'",
            retry_history=retry_history,
            failed_models=failed_models,
            root_cause=last_error,
            error_code="STAGE_FAILURE",
        )

    def _normalize_response(self, raw_response: Dict[str, Any], model: str, duration_ms: float) -> NormalizedResponse:
        """Normalize raw engine response to canonical format.
        
        Args:
            raw_response: Raw JSON response from the engine
            model: The model that was used
            duration_ms: Duration of the call in milliseconds
            
        Returns:
            NormalizedResponse object with content, model, usage, and latency
        """
        # Extract content from response
        content = ""
        if "choices" in raw_response and raw_response["choices"]:
            # Handle choices format (standard OpenAI format)
            content = raw_response["choices"][0]["message"].get("content", "")
        elif "content" in raw_response:
            # Handle direct content format
            content = raw_response.get("content", "")
            
        # Extract usage data
        usage = raw_response.get("usage", {})
        
        # Create normalized response with all data preserved
        return NormalizedResponse(
            content=content,
            model=model,
            usage=usage,
            duration_ms=duration_ms
        )

    # ------------------------------------------------------------------ introspection

    @property
    def call_count(self) -> int:
        """Return the total number of engine calls made by this SDK instance."""
        return self._call_count

    @property
    def profile_info(self) -> dict[str, Any]:
        """Return basic information about the loaded profile."""
        return {
            "gateway_info": self.rotator.get_gateway_info(),
            "profile_path": self.profile_path
        }

    # ------------------------------------------------------------------ engine

    def sync(self) -> dict[str, Any]:
        """Sync models from the engine endpoint and update the deck.

        Fetches the current model list from the engine's /v1/models
        endpoint, normalizes it into gateway format, and replaces the current
        deck with the fresh data. Stage assignments and pipeline settings are
        preserved.

        This is the primary way TurnKey keeps its model deck up to date. It
        works independently — no freshner tool required.

        Returns:
            A summary dict with:
            - ``total_models``: int — new total model count
            - ``gateways``: list[str] — list of provider keys
            - ``model_count_by_gateway``: dict[str, int] — models per provider

        Raises:
            SDKError: On network failure or invalid response.
        """
        url = f"{self.config.engine_url}/v1/models"
        try:
            req = urllib.request.Request(
                url,
                headers={"Content-Type": "application/json"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SDKError(
                f"Engine returned HTTP {exc.code}: {detail}",
                error_code=f"HTTP_{exc.code}",
            ) from exc
        except urllib.error.URLError as exc:
            reason_str = str(exc.reason)
            raise SDKError(
                f"Connection error during sync: {reason_str}",
                error_code="CONNECTION_ERROR",
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SDKError(
                f"Engine returned invalid JSON: {exc}",
                error_code="PARSE_ERROR",
            ) from exc

        if not isinstance(data, list):
            # Engine returns {providers: [...], models: [...]}
            if isinstance(data, dict) and "models" in data:
                data = data["models"]
            else:
                raise SDKError(
                    "Engine response must be a JSON array of model objects",
                    error_code="PARSE_ERROR",
                )

        # /v1/models entries carry ``gateway`` where a card carries
        # ``provider``; map them so the rest of this method is unchanged.
        data = [_card_from_models_entry(c)
                if isinstance(c, dict) and "gateway" in c and "provider" not in c
                else c for c in data]

        # Normalize cards into gateway structure AND preserve all model data
        # Convert engine response (raw fields) to flat format (freshner style)
        gateway_groups: dict[str, list[dict[str, str]]] = {}
        self._normalized_cards = []  # Store full card data for profile builder
        
        for card in data:
            if not isinstance(card, dict):
                continue
            model_id = card.get("modelId") or card.get("modelName") or card.get("id") or card.get("name", "")
            provider = card.get("provider", "unknown")
            if not model_id:
                continue
            if provider not in gateway_groups:
                gateway_groups[provider] = []
            gateway_groups[provider].append({
                "id": model_id,
                "name": model_id,
            })

            # Extract full card data for profile builder
            raw = card.get("engine") or {}
            limits = card.get("limits") or {}
            pricing = card.get("pricing") or {}
            classification = card.get("classification") or {}
            meta = card.get("meta") or {}
            raw_meta = meta.get("raw") or {}

            model_name = card.get("modelName") or card.get("name") or ""
            provider_label = meta.get("provider", provider) or provider

            # Calculate pricing - engine returns cents or per_1m_tokens
            if "input" in pricing and "output" in pricing:
                # Engine returns cents per token (e.g., 0.8 = $0.80)
                input_1m = pricing.get("input")
                output_1m = pricing.get("output")
                cache_read_1m = pricing.get("cache_read")
                known = True
            elif "input_1m" in pricing:
                # Freshner format
                input_1m = pricing.get("input_1m")
                output_1m = pricing.get("output_1m")
                cache_read_1m = pricing.get("cache_read_1m")
                known = pricing.get("known", True)
            else:
                input_1m = None
                output_1m = None
                cache_read_1m = None
                known = False

            # Calculate free flag
            free_kind = classification.get("free_kind") or "unknown"
            free = known and input_1m == 0 and output_1m == 0 and free_kind != "cloud"
            free_label = "FREE" if free else None

            # Normalize capabilities
            caps = card.get("capabilities") or {}
            if not isinstance(caps, dict):
                caps = {}
            capabilities = {
                "text": caps.get("text", True),
                "tools": caps.get("tools", False),
                "image": caps.get("image", False),
                "reasoning": caps.get("reasoning", False),
            }

            self._normalized_cards.append({
                "modelId": model_id,
                "modelName": model_name,
                "provider": provider,
                "providerLabel": provider_label,
                "pricing": {
                    "input_1m": input_1m,
                    "output_1m": output_1m,
                    "cache_read_1m": cache_read_1m,
                    "known": known,
                    "free_kind": free_kind,
                },
                "free": free,
                "free_label": free_label,
                "context": limits.get("context"),
                "max_output": limits.get("max_output"),
                "capabilities": capabilities,
            })

        if not gateway_groups:
            raise SDKError(
                "Engine returned no valid models",
                error_code="INVALID_RESPONSE",
            )

        gateway_models = [
            {"gateway": gw, "models": models}
            for gw, models in gateway_groups.items()
        ]

        # Rebuild the rotator
        self.rotator = GatewayDeckRotator(gateway_models)

        # Preserve existing stage pins
        _pins = {
            name: cfg.get("pinnedModels") or []
            for name, cfg in (self.stages or {}).items()
        }
        self.rotator.set_stage_pins(_pins)

        # Build summary
        info = self.rotator.get_gateway_info()
        return {
            "total_models": self.rotator.total_models,
            "gateways": list(gateway_groups.keys()),
            "model_count_by_gateway": {
                gw: len(models)
                for gw, models in gateway_groups.items()
            },
        }

    # ------------------------------------------------------------------ freshner

    def load_freshner_config(self, filepath: str) -> None:
        """Load a freshner-generated model list and update the deck.

        A freshner config is a JSON file containing an array of model cards
        in the flat format (modelId, provider, pricing, capabilities, etc.).
        This method reads that file, extracts the model cards, restructures
        them into gateway format, and replaces the current deck with them.

        This does NOT change stages or pipeline settings — it only replaces
        the model deck. Stage assignments and pinning are preserved.

        Args:
            filepath: Path to the freshner JSON list file.

        Raises:
            SDKError: If the file cannot be read or has invalid format.
        """
        try:
            raw = Path(filepath).read_text(encoding="utf-8")
            cards = json.loads(raw)
        except OSError as exc:
            raise SDKError(
                f"Cannot read freshner config file: {filepath}: {exc}",
                error_code="FILE_READ_ERROR",
            ) from exc
        except json.JSONDecodeError as exc:
            raise SDKError(
                f"Invalid JSON in freshner config file: {filepath}: {exc}",
                error_code="PARSE_ERROR",
            ) from exc

        if not isinstance(cards, list):
            raise SDKError(
                "Freshner config must be a JSON array of model cards",
                error_code="INVALID_PROFILE",
            )

        # Group cards by provider (gateway)
        gateway_groups: dict[str, list[dict[str, str]]] = {}
        for card in cards:
            if not isinstance(card, dict):
                continue
            model_id = card.get("modelId") or card.get("modelName", "")
            provider = card.get("provider", "unknown")
            if not model_id:
                continue
            if provider not in gateway_groups:
                gateway_groups[provider] = []
            gateway_groups[provider].append({
                "id": model_id,
                "name": model_id,
            })

        if not gateway_groups:
            raise SDKError(
                "Freshner config contains no valid model cards",
                error_code="INVALID_PROFILE",
            )

        # Rebuild the rotator with the new gateway structure
        gateway_models = [
            {"gateway": gw, "models": models}
            for gw, models in gateway_groups.items()
        ]
        self.rotator = GatewayDeckRotator(gateway_models)

        # Preserve existing stage pins
        _pins = {
            name: cfg.get("pinnedModels") or []
            for name, cfg in (self.stages or {}).items()
        }
        self.rotator.set_stage_pins(_pins)
