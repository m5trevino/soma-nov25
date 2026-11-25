"""Python's half of the introbeaver: every number, hash and derived label.

The model reads the language. It quotes each instruction verbatim, names its
type, shows the trigger words it saw, writes the normalized form, the object
phrase and the unresolved pronouns. Everything below follows from that and is
computed here, exactly, from TB-004:

  counts        user_message_count, extracted_count, entry_count_by_type
  noise         signal_quality and noise_signature, from the USER segments
                (TB-004 step 4 defines a segment, NOISE_DEFINITIONS the classes)
  confidence    the five components and the formula (SCORING), truncated
                toward zero, never rounded up across a gate
  intent_key    16 hex of SHA-256 over ``verb_canon::sorted(tokens)``
  chronology    supersession and duplicate flags from intent_key
  labels        UNCERTAIN, QUARANTINED, downstream_action, status, skip_reason
  offsets       artifact char_start / char_end (entries get theirs from
                provenance, which has already run)

Interpretations, where TB-004 leaves a detail open. Each is one line of code
and listed here so none is hidden:

  * A word is a whitespace-separated token.
  * A trigger or noise token matches case-insensitively on word boundaries;
    ``?`` matches anywhere.
  * trigger_strength is chosen by the entry's instruction_type; when none of
    that type's listed rows matches, the weakest listed row of the type is used
    (CONSTRAINT 1.00, DIRECTIVE 0.90, PREFERENCE 0.50, QUESTION 0.60).
  * scope_clarity reads object_phrase: empty -> 0.40, only deictic words ->
    0.60, otherwise 1.00.
  * intent_key joins the sorted tokens with a single space; verb_canon is the
    first token of normalized_form that is not a polarity word, a stopword, or
    the ``query:`` prefix.
  * Supersession: within one intent_key, entries in an earlier message are
    SUPERSEDED by the first entry of the latest message. Inside that latest
    message, a later entry with the same polarity set is flagged DUPLICATE_OF.
  * An entry whose quote is under 3 or over 200 words scores anchor_integrity
    0.00 and is warned about; it is not removed, so entry ids stay sequential.
"""
from __future__ import annotations

import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from .hashes import sha256_hex
from .provenance import locate, normalize

TYPES = ("DIRECTIVE", "CONSTRAINT", "PREFERENCE", "QUESTION")
CEILING = Decimal("0.99")
UNCERTAIN_BELOW = Decimal("0.85")
QUARANTINE_BELOW = Decimal("0.50")
NO_SIGNAL_BELOW = Decimal("0.35")
_Q2 = Decimal("0.01")
_Q3 = Decimal("0.001")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")

# TB-004 TYPE_MAP, HEDGE, POLARITY, STOPWORDS, NOISE_DEFINITIONS, verbatim.
TYPE_MAP = {
    "CONSTRAINT": ("must not", "never", "do not", "don't", "avoid", "prohibit",
                   "prevent", "stop", "shall not", "cannot", "without"),
    "DIRECTIVE": ("must", "shall", "required", "require", "define", "create",
                  "build", "write", "implement", "delete", "remove", "fix",
                  "change", "add", "refactor", "use", "run", "return",
                  "rename", "move"),
    "PREFERENCE": ("prefer", "like", "should", "need", "maybe", "perhaps",
                   "consider", "optionally", "ideally", "would be nice"),
    "QUESTION": ("?", "what", "how", "why", "when", "where", "which", "who",
                 "should we", "can we", "could we", "do we"),
}
HEDGE = ("probably", "possibly", "might", "i think", "i guess", "sort of",
         "kind of")
POLARITY = frozenset({"not", "never", "no", "don't", "dont", "cannot", "without"})
STOPWORDS = frozenset({"a", "an", "the", "to", "of", "for", "in", "on", "with",
                       "we", "i", "you", "it", "should", "must", "please"})
NOISE = {
    "greeting": ("hi", "hello", "hey", "good morning", "good afternoon", "yo"),
    "apology": ("sorry", "my bad", "apologies", "i apologize"),
    "acknowledgment": ("ok", "okay", "thanks", "thank you", "got it",
                       "understood", "cool", "sounds good", "noted"),
    "closing": ("bye", "that's all", "done", "see you", "later", "cheers",
                "thanks all"),
    "filler": ("um", "uh", "like", "you know", "i mean", "basically",
               "actually"),
    "meta_question": ("does that make sense", "you know what i mean", "right?"),
}
DEICTIC = ("it", "this", "that", "them", "the same", "instead", "as before")
POLITE_PREFIXES = ("please", "could you", "can you", "would you",
                   "i want you to", "let's", "let us")

# SCORING: (trigger tokens that select the row, strength), per type.
_STRENGTH = {
    "CONSTRAINT": [(("never", "must not", "shall not", "prohibit"), Decimal("1.00"))],
    "DIRECTIVE": [(("must", "shall", "required"), Decimal("1.00")),
                  ("IMPERATIVE", Decimal("0.90"))],
    "PREFERENCE": [(("should", "need", "ought"), Decimal("0.70")),
                   (("prefer", "like", "maybe", "perhaps", "consider"), Decimal("0.50"))],
    "QUESTION": [("INTERROGATIVE", Decimal("0.60"))],
}


def _d(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _has(token: str, text: str) -> bool:
    """Whole-word, case-insensitive; the question mark matches anywhere."""
    if token == "?":
        return "?" in text
    return re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", text,
                     re.IGNORECASE) is not None


def _words(text: str) -> int:
    return len((text or "").split())


# ── segments, noise, signal ──────────────────────────────────────────────

def segments(text: str) -> list[str]:
    """TB-004 step 4: split at [.!?] followed by whitespace or end, and at newline."""
    out: list[str] = []
    for line in normalize(text).split("\n"):
        for part in re.split(r"(?<=[.!?])\s+", line):
            part = part.strip()
            if part:
                out.append(part)
    return out


def classes_fired(text: str) -> list[str]:
    return [cls for cls, toks in TYPE_MAP.items() if any(_has(t, text) for t in toks)]


def noise_measure(raw_chat_set: list[dict]) -> dict[str, Any]:
    """signal_quality and noise_signature over the USER segments."""
    sig = {k: 0 for k in NOISE}
    total = noise_words = 0
    for turn in raw_chat_set or []:
        if str((turn or {}).get("role") or "").upper() != "USER":
            continue
        for seg in segments((turn or {}).get("content") or ""):
            n = _words(seg)
            total += n
            if classes_fired(seg):
                continue                      # a trigger fired: not noise
            hit = [k for k, toks in NOISE.items() if any(_has(t, seg) for t in toks)]
            if hit:
                for k in hit:
                    sig[k] += 1
                noise_words += n
    quality = (Decimal(total - noise_words) / Decimal(total)) if total else Decimal(0)
    sig["noise_words"] = noise_words
    return {"signal_quality": quality.quantize(_Q3, rounding=ROUND_HALF_UP),
            "noise_signature": sig, "total_words": total}


# ── confidence ───────────────────────────────────────────────────────────

def _imperative(text: str) -> bool:
    """Clause-initial imperative verb, after stripping politeness prefixes."""
    t = text.strip().lower()
    for p in POLITE_PREFIXES:
        if t.startswith(p + " "):
            t = t[len(p) + 1:].lstrip()
    first = re.match(r"[\w'-]+", t)
    return bool(first) and first.group(0) in TYPE_MAP["DIRECTIVE"][3:]  # verbs, not modals


def trigger_strength(kind: str, text: str) -> Decimal | None:
    rows = _STRENGTH.get(kind)
    if not rows:
        return None
    for key, strength in rows:
        if key == "IMPERATIVE":
            if _imperative(text):
                return strength
        elif key == "INTERROGATIVE":
            if "?" in text or any(_has(t, text) for t in TYPE_MAP["QUESTION"]):
                return strength
        elif any(_has(t, text) for t in key):
            return strength
    return min(s for _, s in rows)            # weakest listed row for the type


def scope_clarity(object_phrase: Any) -> Decimal:
    obj = (object_phrase or "").strip().lower() if isinstance(object_phrase, str) else ""
    if not obj:
        return Decimal("0.40")
    rest = obj
    for tok in sorted(DEICTIC, key=len, reverse=True):
        rest = re.sub(r"(?<!\w)" + re.escape(tok) + r"(?!\w)", " ", rest)
    return Decimal("0.60") if not re.sub(r"[\W_]+", "", rest) else Decimal("1.00")


def type_unanimity(n_classes: int) -> Decimal | None:
    if n_classes <= 0:
        return None
    return Decimal("1.00") if n_classes == 1 else (
        Decimal("0.70") if n_classes == 2 else Decimal("0.50"))


def components_for(entry: dict) -> dict[str, Decimal] | None:
    """The five components from the quote and its labels, or None."""
    text = normalize(entry.get("verbatim_text") or "")
    kind = str(entry.get("instruction_type") or "").strip().upper()
    ts = trigger_strength(kind, text)
    tu = type_unanimity(len(classes_fired(text)))
    if ts is None or tu is None:
        return None
    n = _words(text)
    return {
        "trigger_strength": ts,
        "anchor_integrity": Decimal("1.00") if 3 <= n <= 200 else Decimal("0.00"),
        "type_unanimity": tu,
        "scope_clarity": scope_clarity(entry.get("object_phrase")),
        "hedge_penalty": Decimal("0.10") if any(_has(h, text) for h in HEDGE)
        else Decimal("0.00"),
    }


def confidence(components: dict) -> Decimal | None:
    """TB-004 SCORING; None when a component is missing or not a number."""
    c = {k: _d((components or {}).get(k)) for k in (
        "trigger_strength", "anchor_integrity", "type_unanimity",
        "scope_clarity", "hedge_penalty")}
    if any(v is None for v in c.values()):
        return None
    raw = (Decimal("0.40") * c["trigger_strength"] + Decimal("0.30") * c["anchor_integrity"]
           + Decimal("0.20") * c["type_unanimity"] + Decimal("0.10") * c["scope_clarity"]
           - c["hedge_penalty"])
    return min(CEILING, max(Decimal("0"), raw)).quantize(_Q2, rounding=ROUND_DOWN)


# ── counts, key, chronology ──────────────────────────────────────────────

def user_message_count(raw_chat_set: list[dict]) -> int:
    return sum(1 for t in raw_chat_set or []
               if str((t or {}).get("role") or "").upper() == "USER")


def tally(entries: list[dict]) -> dict[str, int]:
    out = {k: 0 for k in TYPES}
    for e in entries or []:
        kind = str(e.get("instruction_type") or "").strip().upper() \
            if isinstance(e, dict) else ""
        if kind in out:
            out[kind] += 1
    return out


def _tokens(normalized_form: str) -> list[str]:
    nf = (normalized_form or "").lower().strip()
    if nf.startswith("query:"):
        nf = nf[len("query:"):]
    toks = [t.strip(".,;:!?\"'()[]") for t in nf.split()]
    return [t for t in toks if t]


def polarity_set(normalized_form: str) -> list[str]:
    return sorted({t for t in _tokens(normalized_form) if t in POLARITY})


def intent_key(normalized_form: str) -> str | None:
    """16 hex: sha256(verb_canon + "::" + sorted(tokens - STOPWORDS - POLARITY))."""
    toks = _tokens(normalized_form)
    if not toks:
        return None
    verb = next((t for t in toks if t not in POLARITY and t not in STOPWORDS), toks[0])
    rest = sorted(set(toks) - STOPWORDS - POLARITY)
    return sha256_hex(verb + "::" + " ".join(rest))[:16]


def _chronology(entries: list[dict]) -> list[dict]:
    """Set entry_status / supersedes / superseded_by and the DUPLICATE_OF flag."""
    for e in entries:
        e["entry_status"], e["supersedes"], e["superseded_by"] = "ACTIVE", None, None
        e["flags"] = [f for f in (e.get("flags") or []) if f != "DUPLICATE_OF"]
    groups: dict[str, list[dict]] = {}
    for e in entries:
        if e.get("intent_key") and isinstance(e.get("source_msg_index"), int):
            groups.setdefault(e["intent_key"], []).append(e)
    resolved: list[dict] = []
    for key, g in groups.items():
        g.sort(key=lambda e: (e["source_msg_index"], e.get("sentence_index") or 0))
        latest = g[-1]["source_msg_index"]
        newest = [e for e in g if e["source_msg_index"] == latest]
        older = [e for e in g if e["source_msg_index"] < latest]
        winner = newest[0]
        for later in newest[1:]:
            if polarity_set(later.get("normalized_form")) == polarity_set(
                    winner.get("normalized_form")) and "DUPLICATE_OF" not in later["flags"]:
                later["flags"].append("DUPLICATE_OF")
        if older:
            for e in older:
                e["entry_status"], e["superseded_by"] = "SUPERSEDED", winner.get("entry_id")
            winner["supersedes"] = older[-1].get("entry_id")
            resolved.append({"intent_key": key, "winner": winner.get("entry_id"),
                             "losers": [e.get("entry_id") for e in older],
                             "rule": "last_user_statement_wins"})
    return resolved


# ── applying it to a gate result ─────────────────────────────────────────

def apply(gate: Any, raw_chat_set: list[dict]) -> dict[str, Any]:
    """Write Python's numbers, hashes and labels over whatever the model sent.

    Edits ``gate.parsed`` in place and records what it changed as warnings, so
    a model that miscounts is visible without failing the stage.
    """
    parsed = gate.parsed if isinstance(gate.parsed, dict) else {}
    body = parsed.get("introbeaver_output")
    if not isinstance(body, dict):
        body = parsed if "entries" in parsed else None
    if not isinstance(body, dict) or not isinstance(body.get("entries"), list):
        return {"applied": False, "why": "no entries[]"}

    entries = [e for e in body["entries"] if isinstance(e, dict)]
    notes: list[str] = []
    short: list[str] = []
    undefined: list[str] = []

    # per-entry: components, confidence, key
    for e in entries:
        eid = e.get("entry_id")
        comps = components_for(e)
        c = confidence(comps) if comps else None
        if comps is None or c is None:
            undefined.append(str(eid))
            e["confidence"] = None
            e.pop("confidence_components", None)
        else:
            e["confidence_components"] = {k: float(v) for k, v in comps.items()}
            e["confidence"] = float(c)
            if comps["anchor_integrity"] == 0:
                short.append(str(eid))
        e["intent_key"] = intent_key(e.get("normalized_form"))

    # counts and noise
    counts = {"user_message_count": user_message_count(raw_chat_set),
              "extracted_count": len(entries),
              "entry_count_by_type": tally(entries)}
    nm = noise_measure(raw_chat_set)
    for key, value in counts.items():
        if body.get(key) is not None and body.get(key) != value:   # absent is expected
            notes.append(f"{key} {body.get(key)!r} -> {value!r}")
        body[key] = value
    body["signal_quality"] = float(nm["signal_quality"])
    body["noise_signature"] = nm["noise_signature"]

    # chronology, then the labels that depend on confidence
    body["chronology_resolved"] = _chronology(entries)
    any_question = any(str(e.get("instruction_type") or "").upper() == "QUESTION"
                       for e in entries)
    for e in entries:
        c = _d(e.get("confidence"))
        flags = [f for f in (e.get("flags") or []) if f != "UNCERTAIN"]
        if c is None or c < UNCERTAIN_BELOW:
            flags.append("UNCERTAIN")
        e["flags"] = flags
        if c is not None and c < QUARANTINE_BELOW and e["entry_status"] == "ACTIVE":
            e["entry_status"] = "QUARANTINED"
        if any_question:
            e["downstream_action"] = ("ANSWER_REQUIRED"
                                      if str(e.get("instruction_type")).upper() == "QUESTION"
                                      else "BLOCKED_PENDING_ANSWER")
        else:
            e["downstream_action"] = "HUMAN_REVIEW" if "UNCERTAIN" in flags else "PROCEED"

    # artifacts: Python finds the offsets
    for a in body.get("artifacts") or []:
        if isinstance(a, dict) and isinstance(a.get("source_msg_index"), int):
            i = a["source_msg_index"]
            if 0 <= i < len(raw_chat_set):
                span = locate(a.get("verbatim_text") or "",
                              (raw_chat_set[i] or {}).get("content") or "")
                if span:
                    a["char_start"], a["char_end"] = span

    # verdict rules 1, 3, 4, 5 (rule 2 is an error object, handled above)
    if counts["user_message_count"] == 0:
        status, block, skip = "SKIP", None, "NO_USER_MESSAGES"
    elif any_question:
        status, block, skip = "QUARANTINE", "UNRESOLVED_QUESTION", None
    elif not entries:
        status, block = "SKIP", None
        skip = "NO_SIGNAL" if nm["signal_quality"] < NO_SIGNAL_BELOW else "NO_TRIGGER_MATCH"
    else:
        status, block, skip = "PASS", None, None
    if body.get("status") is not None and body.get("status") != status:
        notes.append(f"status {body.get('status')!r} -> {status!r}")
    body["status"], body["block_reason"], body["skip_reason"] = status, block, skip
    gate.verdict = status

    if notes:
        gate.warnings.append("introbeaver numbers corrected: " + "; ".join(notes))
    if undefined:
        gate.warnings.append(f"confidence undefined on {undefined}: no trigger class matched")
    if short:
        gate.warnings.append(f"quote under 3 or over 200 words on {short}: anchor_integrity 0.00")

    return {"applied": True, "counts": counts, "signal_quality": str(nm["signal_quality"]),
            "status": status, "chronology": len(body["chronology_resolved"]),
            "undefined": len(undefined)}
