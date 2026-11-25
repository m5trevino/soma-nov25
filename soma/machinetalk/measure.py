"""Python's half of the distiller: the arithmetic.

The model reads the language and quotes what matters. Everything that is a
count is done here, exactly, from the chat set as it was hashed and from the
entries that provenance accepted:

  * bytes: original, signal, noise, distilled (UTF-8 lengths, never characters)
  * the noise signature: how many noise segments fall in each class
  * signal_quality and compaction_ratio
  * the two skip rules that are pure arithmetic, NO_SIGNAL and NOISE_THRESHOLD

The definitions come from TB-001 (``SCORING``, ``NOISE_DEFINITIONS``,
``VERDICT_RULES`` 3 and 4, ``SKIP_REASON_MAP``, ``EXFIL_TYPE_MAP``). Nothing
here invents a rule. The semantic skip reasons (STATIC_ONLY, ALREADY_RESOLVED,
OUT_OF_DOMAIN, DUPLICATE_HASH) need judgment and stay with the model.

Signal is the bytes covered by the entries provenance accepted. Everything
else in the chat set is noise, so ``noise + signal == original`` holds by
construction (SUBTRACTION_LAW).
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .provenance import normalize

NOISE_THRESHOLD = Decimal("0.300")

# TB-001 NOISE_DEFINITIONS, one pattern per class. Word classes match whole
# words, case-insensitively; sign-off emoji match anywhere.
_W = lambda alts: re.compile(r"(?<![\w'])(?:%s)(?![\w'])" % alts, re.IGNORECASE)  # noqa: E731
NOISE_PATTERNS: dict[str, re.Pattern] = {
    "greeting": _W(r"hi|hello|hey|good (?:morning|afternoon)|what's up|how are you"),
    "apology": _W(r"my bad|sorry|i was wrong|i apologize|i messed up|i was trippin"),
    "hedge": _W(r"i think|maybe|perhaps|i guess|kind of|sort of"),
    "filler": _W(r"um|uh|like|you know|basically|literally|honestly"),
    "assistant_fluff": _W(r"here is|let me|i will|as requested|as you can see|happy to"),
    "prose_wrapper": _W(r"here is the|let me explain|what i'm doing is"),
    "sign_off": re.compile(
        r"(?<![\w'])(?:4sho|claro que si|locc|cheers|that's all)(?![\w'])|[💀🎯🔥]",
        re.IGNORECASE),
}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_EMOJI_ONLY = re.compile(
    r"[\s‍️☀-➿⬀-⯿\U0001f000-\U0001faff]+")
_SEGMENT = re.compile(r"[^.!?\n]+[.!?]?|[.!?\n]")
CLASSES = ("greeting", "apology", "hedge", "filler", "repetition",
           "assistant_fluff", "prose_wrapper", "visual_feedback", "sign_off")
SIGNAL_ARRAYS = ("directives", "constraints", "preferences", "questions")
_ARCHIVE = "ARCHIVE"


def _ratio(num: int, den: int) -> Decimal:
    """3 decimal places, half-up; a zero-byte chat set scores 0.000."""
    if den == 0:
        return Decimal("0.000")
    return (Decimal(num) / Decimal(den)).quantize(Decimal("0.001"), ROUND_HALF_UP)


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def measure(raw_chat_set: list[dict], accepted: list[dict]) -> dict[str, Any]:
    """The numbers for one chat set.

    ``accepted`` is ``ProvenanceReport.accepted``: entries whose quote was found
    in the turn they cite, with ``turn``, ``char_start`` and ``char_end`` set by
    Python (codepoint offsets into the normalized turn, half-open).
    """
    turns = [normalize((m or {}).get("content") or "") for m in raw_chat_set or []]
    spans: dict[int, list[tuple[int, int]]] = {}
    unique: dict[tuple[int, int, int], str] = {}
    for e in accepted or []:
        t, s, en = e.get("turn"), e.get("char_start"), e.get("char_end")
        if not (isinstance(t, int) and 0 <= t < len(turns)
                and isinstance(s, int) and isinstance(en, int) and en > s):
            continue
        spans.setdefault(t, []).append((s, en))
        unique[(t, s, en)] = turns[t][s:en]

    original = signal = 0
    noise_turns: list[int] = []
    signal_turns: list[int] = []
    counts = {c: 0 for c in CLASSES}
    seen_segments: set[str] = set()

    for i, text in enumerate(turns):
        original += _bytes(text)
        merged = _merge(spans.get(i, []))
        signal += sum(_bytes(text[s:e]) for s, e in merged)
        (signal_turns if merged else noise_turns).append(i)
        for m in _SEGMENT.finditer(text):
            seg = m.group(0)
            body = seg.strip(" \t\r\n.!?")
            if not body:
                continue
            s0, e0 = m.start(), m.end()
            if any(s < e0 and e > s0 for s, e in merged):
                continue  # overlaps a quoted entry: signal, not noise
            key = " ".join(body.casefold().split())
            if key in seen_segments:
                counts["repetition"] += 1
            seen_segments.add(key)
            for cls, pat in NOISE_PATTERNS.items():
                if pat.search(body):
                    counts[cls] += 1
            if _ANSI.search(seg) or _EMOJI_ONLY.fullmatch(body):
                counts["visual_feedback"] += 1

    noise = original - signal
    distilled = sum(_bytes(t) for t in unique.values())
    counts["total_bytes_noise"] = noise
    return {
        "bytes": {"original": original, "noise": noise, "signal": signal,
                  "distilled": distilled},
        "signal_quality": _ratio(original - noise, original),
        "compaction_ratio": _ratio(distilled, original),
        "turns": {"signal": signal_turns, "noise": noise_turns},
        "noise_signature": counts,
    }


def skip_decision(body: dict, accepted: list[dict], m: dict) -> dict[str, Any]:
    """Apply the two arithmetic skip rules; keep the model's judgment otherwise.

    VERDICT_RULES order: 3 (no signal) then 4 (noise threshold). The noise
    threshold is a rule for a chat set the model called noise. When the model
    returned skip false (or none) and provenance accepted at least one entry,
    that explicit non-skip stands whatever the ratio is. When neither rule
    fires, a skip the model gave for one of those two reasons is withdrawn,
    because the arithmetic does not support it. A skip for a semantic reason is
    left exactly as the model wrote it.
    """
    have = {k: 0 for k in SIGNAL_ARRAYS}
    for e in accepted or []:
        key = str(e.get("entry_id") or "").split("[", 1)[0]
        if key in have:
            have[key] += 1
    if not any(have.values()):
        return {"skip": True, "skip_reason": "NO_SIGNAL", "exfil_type": _ARCHIVE,
                "decided_by": "python"}
    if not body.get("skip"):
        return {"skip": False, "skip_reason": None, "exfil_type": None,
                "decided_by": "model"}
    if m["signal_quality"] < NOISE_THRESHOLD:
        return {"skip": True, "skip_reason": "NOISE_THRESHOLD", "exfil_type": _ARCHIVE,
                "decided_by": "python"}
    reason = body.get("skip_reason")
    if body.get("skip") and reason not in ("NO_SIGNAL", "NOISE_THRESHOLD"):
        return {"skip": True, "skip_reason": reason,
                "exfil_type": body.get("exfil_type"), "decided_by": "model"}
    return {"skip": False, "skip_reason": None, "exfil_type": None,
            "decided_by": "python"}


def apply(gate: Any, accepted: list[dict], raw_chat_set: list[dict]) -> dict[str, Any]:
    """Write Python's numbers and skip decision over whatever the model sent.

    Edits ``gate.parsed`` in place and brings the gate result in line, so every
    later reader (routing, checkpoint, Soma, display) sees the same values.
    Returns the measurement for logging.
    """
    parsed = gate.parsed if isinstance(gate.parsed, dict) else {}
    body = parsed.get("distiller_output") if isinstance(
        parsed.get("distiller_output"), dict) else parsed
    m = measure(raw_chat_set, accepted)
    d = skip_decision(body, accepted, m)

    # Python's offsets go back into the entries the model wrote, so every later
    # reader sees exact positions instead of a field the model no longer sends.
    for e in accepted or []:
        mt = re.match(r"(\w+)\[(\d+)\]$", str(e.get("entry_id") or ""))
        lst = body.get(mt.group(1)) if mt else None
        if mt and isinstance(lst, list) and int(mt.group(2)) < len(lst) \
                and isinstance(lst[int(mt.group(2))], dict):
            lst[int(mt.group(2))]["char_start"] = e.get("char_start")
            lst[int(mt.group(2))]["char_end"] = e.get("char_end")

    # intent_anchor is a rule, not a judgment (TB-001 SCORING): the first ACTIVE
    # directive, else the first ACTIVE constraint, else null. Filled here when
    # the model left it empty.
    if not body.get("intent_anchor"):
        anchor = None
        for key in ("directives", "constraints"):
            for item in body.get(key) or []:
                if isinstance(item, dict) and item.get("text") \
                        and item.get("status", "ACTIVE") == "ACTIVE":
                    anchor = item["text"]
                    break
            if anchor:
                break
        body["intent_anchor"] = anchor

    body["bytes"] = m["bytes"]
    body["signal_quality"] = float(m["signal_quality"])
    body["compaction_ratio"] = float(m["compaction_ratio"])
    body["turns"] = m["turns"]
    body["noise_signature"] = m["noise_signature"]
    body["skip"] = d["skip"]
    body["skip_reason"] = d["skip_reason"]
    body["exfil_type"] = d["exfil_type"]

    gate.skipped = bool(d["skip"])
    gate.verdict = "true" if d["skip"] else "false"
    gate.skip_reason = d["skip_reason"] or ""
    gate.exfil_type = d["exfil_type"] or ""
    gate.signal_quality = str(m["signal_quality"])
    return {**m, **d}
