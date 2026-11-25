"""Python's half of the outrobeaver: hashes, scores and session health.

The model reads the language: it names drift events (a quoted directive against
the artifact state), labels friction points, and summarises what was built. The
numbers are Python's, computed here exactly from the chat set and from the
counted things the model reported:

  * content_hash for every journal entry: the first 16 hex of the SHA-256 of
    that turn's content, which is the "hash law" of TB-006 (same content, same
    hash; the exemplar's ``e3b0c44298fc1c14`` is SHA-256 of the empty string)
  * the per-turn scores from their components (TB-006 ``SCORING``)
  * session_clarity_score, drift_penalty, friction_rate and friction_penalty
  * health_score, the GREEN / YELLOW / RED band, and the override rules

The definitions come from TB-006 (``SCORING``, ``FRICTION_RULES``, ``drift
severity weights``, the ``override`` block). Nothing here invents a rule. Two
inputs have no source in the outrobeaver's own output and are left alone:

  * "any unresolved QUESTION at session end" is applied only when the caller
    passes ``unresolved_questions``; it is not guessed from the model's prose.
  * an error status (CORRUPT_INPUT, GHOST_SESSION) says "session_health
    recomputed with penalty" but gives no penalty, so Python does not
    recompute those; the model's envelope is left as it was.

The component ratios (specificity, fluff_ratio and the rest) are still counted
by the model. Python recomputes everything that is arithmetic on them.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .hashes import sha256_hex

SEVERITY_WEIGHT = {
    "MINOR": Decimal("0.05"),
    "MODERATE": Decimal("0.15"),
    "MAJOR": Decimal("0.35"),
    "CRITICAL": Decimal("1.00"),
}
GREEN_AT = Decimal("0.850")
YELLOW_AT = Decimal("0.500")

# The override texts are the rule lines of TB-006, verbatim.
OVERRIDE_CRITICAL = "any CRITICAL drift event"
OVERRIDE_CONTEXT_RESET = "any context reset followed by repeated work"
OVERRIDE_QUESTION = "any unresolved QUESTION at session end"

_Q2 = Decimal("0.01")
_Q3 = Decimal("0.001")
_ZERO = Decimal("0")
_ONE = Decimal("1")


def _d(value: Any) -> Decimal | None:
    """A model number as a Decimal, or None when it is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _clamp(x: Decimal) -> Decimal:
    return max(_ZERO, min(_ONE, x))


def _q(x: Decimal, places: Decimal) -> Decimal:
    return x.quantize(places, rounding=ROUND_HALF_UP)


# ── hashes ───────────────────────────────────────────────────────────────

def content_hash(content: Any) -> str:
    """16 hex that identify the content, not who said it."""
    return sha256_hex("" if content is None else str(content))[:16]


def verify_hashes(journal: list[dict], raw_chat_set: list[dict]) -> list[dict]:
    """Every journal entry whose content_hash is not the hash of its turn.

    ``turn_index`` is an index into the raw chat set (INDEX_LAW). A missing or
    out-of-range index is reported too, since there is nothing to verify it
    against. Returns one record per problem: turn_index, claimed, expected.
    """
    bad: list[dict] = []
    for e in journal or []:
        if not isinstance(e, dict):
            continue
        ti = e.get("turn_index")
        claimed = e.get("content_hash")
        if not isinstance(ti, int) or isinstance(ti, bool) \
                or not 0 <= ti < len(raw_chat_set):
            bad.append({"turn_index": ti, "claimed": claimed, "expected": None})
            continue
        want = content_hash(raw_chat_set[ti].get("content"))
        if claimed != want:
            bad.append({"turn_index": ti, "claimed": claimed, "expected": want})
    return bad


# ── scores ───────────────────────────────────────────────────────────────

def turn_scores(components: dict) -> dict[str, Decimal] | None:
    """The three per-turn scores from one turn's components, or None.

    user_clarity   = 0.40*specificity + 0.30*constraint_density + 0.30*reference_resolution
    agent_clarity  = 1.00 - fluff_ratio
    mutual_underst = confirmation_ratio

    None when a needed component is missing or not a number (SCORE_UNDEFINED):
    the score is not invented.
    """
    c = {k: _d((components or {}).get(k)) for k in (
        "specificity", "constraint_density", "reference_resolution",
        "fluff_ratio", "confirmation_ratio")}
    if any(v is None for v in c.values()):
        return None
    user = (Decimal("0.40") * _clamp(c["specificity"])
            + Decimal("0.30") * _clamp(c["constraint_density"])
            + Decimal("0.30") * _clamp(c["reference_resolution"]))
    return {
        "user_clarity": _q(_clamp(user), _Q2),
        "agent_clarity": _q(_clamp(_ONE - _clamp(c["fluff_ratio"])), _Q2),
        "mutual_understanding": _q(_clamp(c["confirmation_ratio"]), _Q2),
    }


def health(clarity: Decimal, drift_events: list[dict],
           friction_points: list[dict], total_turns: int,
           unresolved_questions: list[str] | None = None) -> dict[str, Any]:
    """health_score, its band, and the override that forced RED, if any.

    drift_penalty    = min(1, sum(severity_weight))
    friction_penalty = friction_points / max(1, total_turns)   (a ratio, at most 1)
    health_score     = 0.35*clarity + 0.35*(1 - drift_penalty) + 0.30*(1 - friction_penalty)
    GREEN >= 0.850, YELLOW >= 0.500, RED below; the band is read from the
    reported 3dp score so the number and the label can never disagree.
    """
    unknown: list[str] = []
    drift_sum = _ZERO
    for ev in drift_events or []:
        sev = str((ev or {}).get("severity") or "").upper()
        if sev in SEVERITY_WEIGHT:
            drift_sum += SEVERITY_WEIGHT[sev]
        else:
            unknown.append(sev or "?")
    drift_penalty = min(_ONE, drift_sum)
    rate = Decimal(len(friction_points or [])) / Decimal(max(1, total_turns))
    friction_penalty = min(_ONE, rate)

    score = _q(Decimal("0.35") * clarity
               + Decimal("0.35") * (_ONE - drift_penalty)
               + Decimal("0.30") * (_ONE - friction_penalty), _Q3)
    band = "GREEN" if score >= GREEN_AT else ("YELLOW" if score >= YELLOW_AT else "RED")

    override = None
    sevs = {str((e or {}).get("severity") or "").upper() for e in drift_events or []}
    losses = [f.get("turn_index") for f in friction_points or []
              if isinstance(f, dict) and f.get("type") == "CONTEXT_LOSS"
              and isinstance(f.get("turn_index"), int)]
    repeats = [f.get("turn_index") for f in friction_points or []
               if isinstance(f, dict) and f.get("type") == "REPETITION"
               and isinstance(f.get("turn_index"), int)]
    if unresolved_questions:
        override = OVERRIDE_QUESTION
    elif "CRITICAL" in sevs:
        override = OVERRIDE_CRITICAL
    elif any(r > l for l in losses for r in repeats):
        override = OVERRIDE_CONTEXT_RESET
    if override:
        band = "RED"

    return {
        "clarity": _q(clarity, _Q3),
        "drift_penalty": _q(drift_penalty, _Q3),
        "friction_penalty": _q(friction_penalty, _Q3),
        "friction_rate": _q(friction_penalty, _Q3),
        "score": score,
        "session_health": band,
        "override": override,
        "unknown_severities": unknown,
    }


# ── applying it to a gate result ─────────────────────────────────────────

def apply(gate: Any, raw_chat_set: list[dict],
          unresolved_questions: list[str] | None = None) -> dict[str, Any]:
    """Write Python's hashes, scores and health over whatever the model sent.

    Edits ``gate.parsed`` in place and brings the gate's verdict and warnings
    in line, so every later reader (routing, checkpoint, Soma, display) sees
    the same values. Returns the measurement for logging.
    """
    parsed = gate.parsed if isinstance(gate.parsed, dict) else {}
    body = parsed.get("outrobeaver_output")
    if not isinstance(body, dict):
        return {"applied": False, "why": "no outrobeaver_output"}
    if body.get("status", "OK") != "OK":
        return {"applied": False, "why": f"status {body.get('status')}"}

    result: dict[str, Any] = {"applied": True}

    # hashes: Python's replace the model's
    journal = body.get("journal_entries") or []
    bad = verify_hashes(journal, raw_chat_set)
    for e in journal:
        ti = e.get("turn_index") if isinstance(e, dict) else None
        if isinstance(ti, int) and not isinstance(ti, bool) and 0 <= ti < len(raw_chat_set):
            e["content_hash"] = content_hash(raw_chat_set[ti].get("content"))
    result["journal_entries"] = len(journal)
    result["turns"] = len(raw_chat_set)
    if len(journal) != len(raw_chat_set):
        gate.warnings.append(
            f"journal_entries={len(journal)} but chat set has {len(raw_chat_set)} turns")
    wrong = [b for b in bad if b["claimed"] is not None]    # a missing hash is not a wrong one
    result["hash_corrections"] = len(wrong)
    if wrong:
        gate.warnings.append(f"content_hash corrected on {len(wrong)} entr"
                             f"{'y' if len(wrong) == 1 else 'ies'}")

    # per-turn scores from components; the session mean from those
    ca = body.setdefault("clarity_and_communication_analysis", {})
    rows = ca.get("turn_scores") or []
    users, agents, mutuals, undefined = [], [], [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        s = turn_scores(row.get("components") or {})
        if s is None:
            undefined.append(row.get("turn_index"))
            continue
        row["user_clarity"] = float(s["user_clarity"])
        row["agent_clarity"] = float(s["agent_clarity"])
        row["mutual_understanding"] = float(s["mutual_understanding"])
        users.append(s["user_clarity"])
        agents.append(s["agent_clarity"])
        mutuals.append(s["mutual_understanding"])
    if undefined:
        gate.warnings.append(f"SCORE_UNDEFINED on turns {undefined}: left as the model sent")
    if not users:
        result.update(applied=False, why="no scorable turns")
        gate.warnings.append("health not computed: no turn had complete components")
        return result

    n = Decimal(len(users))
    clarity = (sum(users) / n + sum(agents) / n + sum(mutuals) / n) / Decimal(3)
    h = health(clarity, ca.get("drift_events") or [], ca.get("friction_points") or [],
               len(raw_chat_set), unresolved_questions)
    if h["unknown_severities"]:
        gate.warnings.append(f"unknown drift severity {h['unknown_severities']}: not weighted")

    ca["session_clarity_score"] = float(h["clarity"])
    ca["friction_rate"] = float(h["friction_rate"])
    # REPETITION points counted by the role of the turn each one cites
    def _role(ti):
        ok = isinstance(ti, int) and not isinstance(ti, bool) and 0 <= ti < len(raw_chat_set)
        return str((raw_chat_set[ti] or {}).get("role") or "").upper() if ok else ""
    reps = [_role(f.get("turn_index")) for f in ca.get("friction_points") or []
            if isinstance(f, dict) and f.get("type") == "REPETITION"]
    ca["user_repetition_count"] = reps.count("USER")
    ca["agent_repetition_count"] = reps.count("AGENT")
    body.setdefault("summary", {})["session_health"] = h["session_health"]
    body["health_components"] = {
        "clarity": float(h["clarity"]),
        "drift_penalty": float(h["drift_penalty"]),
        "friction_penalty": float(h["friction_penalty"]),
        "score": float(h["score"]),
        "override": h["override"],
    }

    # the gate's own view follows Python's verdict
    gate.warnings[:] = [w for w in gate.warnings if w != "session_health_RED"]
    if h["session_health"] == "RED":
        gate.warnings.append("session_health_RED")
    gate.verdict = h["session_health"]

    result.update(session_health=h["session_health"], score=str(h["score"]),
                  override=h["override"])
    return result
