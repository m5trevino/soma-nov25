"""Clean table-based progress output for Stage 1 ingestion.

While a stage runs → one live line, rewritten in place with a spinning
character and elapsed seconds, frozen with a single newline when it ends.
After each chatset finishes → print header → divider → table → divider.
After all chatsets → summary.

Every session is shown by its short code (``SES-120``) and every stage by its
own (``DI-01``), so a line reads ``[SES-120][DI-01] DISTILLER``. A finished
stage also prints the wall-clock time it started, the duration it measured,
and an absolute ``file://`` link to its log, because a relative path cannot be
clicked.

No interleaved logs, no live dashboard.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from . import ascii_dividers
from . import short_codes


# ── Primitives ──────────────────────────────────────────────────────────

# ANSI styling for the dressed-up stage output. Always emitted so the
# launcher's terminal (which re-prints captured lines) still renders it.
_C_RESET = "\033[0m"
_C_BOLD = "\033[1m"
_C_DIM = "\033[2m"
_C_CYAN = "\033[36m"
_C_GREEN = "\033[32m"
_C_YELLOW = "\033[33m"
_C_RED = "\033[31m"
_C_BLUE = "\033[34m"
_C_MAGENTA = "\033[35m"
_C_WHITE = "\033[97m"
_C_ORANGE = "\033[38;5;208m"
_C_STEEL = "\033[38;5;245m"

_DIVIDER = ("■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□"
            "■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□"
            "■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■")

_STAGE_LABELS = {
    "distiller": "DISTILLER",
    "introbeaver": "INTRO-BEAVER",
    "planbeaver": "PLAN-BEAVER",
    "outrobeaver": "OUTRO-BEAVER",
}

# Step aliases: what the user sees
_STEP_LABELS = {
    "distiller":    ("ALPHA", "Receptor",      "DISTILLER"),
    "introbeaver":  ("BRAVO", "Classifier",    "INTRO-BEAVER"),
    "planbeaver":   ("CHARLIE", "Integrator",  "PLAN-BEAVER"),
    "outrobeaver":  ("EDWARD", "Retrograde",   "OUTRO-BEAVER"),
}

_STAGE_ORDER = ["distiller", "introbeaver", "planbeaver", "outrobeaver"]

_STAGE_DESCRIPTIONS = {
    "distiller": "Reminder of steps (paced for visibility)",
    "introbeaver": "Classifying intent and stack",
    "planbeaver": "Generating step-by-step plan",
    "outrobeaver": "Validating plan and intent",
}

# Paced step lines per stage, printed live while the one model call runs.
# The seconds beside them are pacing, not measurements (see the long comment
# above the first _DISTILLER_STEPS definition). THE LIVE COPY of each list
# is the one near StageTimer; keep both identical.
_STAGE_STEPS: dict[str, list[str]] = {
    "distiller": [
        "NFC-normalizing input chatset",
        "validating required fields (session_id, chatset_id, chatset_hash, raw_chat_set)",
        "extracting heredocs, metadata footers, and constraint blocks",
        "stripping noise: greetings, apologies, fillers, fences, assistant fluff",
        "checking for duplicate chatset hashes",
        "resolving chronology (last user statement = current goal)",
        "classifying signals (directives, constraints, preferences, questions, corrections, confirmations)",
        "matching intent across turns",
        "logging noise purges + stats",
        "computing overall signal quality",
        "applying gates (pass | skip | quarantine)",
        "writing canonical JSON output to Soma",
    ],
    "introbeaver": [
        "reading distiller output (directives, constraints, artifacts)",
        "extracting explicit language/framework mentions",
        "analyzing artifacts (heredocs, metadata, constraint blocks) for stack clues",
        "classifying intent (build, fix, refactor, ...)",
        "identifying project type (CLI, web, library, ...)",
        "detecting keywords (python, react, postgres, ...)",
        "resolving conflicts between explicit and implicit stack evidence",
        "computing confidence score for language/framework",
        "applying gates (pass | skip | quarantine)",
        "writing intent and stack inferences to Soma",
    ],
    "planbeaver": [
        "reading distiller and introbeaver outputs",
        "identifying primary directive and constraints",
        "breaking down task into actionable steps",
        "ordering steps chronologically (dependencies first)",
        "mapping steps to files, commands, and code changes",
        "validating plan against constraints (e.g. stdlib only)",
        "cross-referencing with existing codebase when available",
        "estimating effort for each step",
        "applying gates (pass | skip | quarantine)",
        "writing step-by-step plan to Soma",
    ],
    "outrobeaver": [
        "reading distiller, introbeaver, and planbeaver outputs",
        "verifying plan covers all directives and constraints",
        "checking for missing or incomplete steps",
        "ensuring chronology is respected (last user statement = final goal)",
        "validating stack inferences (language/framework)",
        "detecting contradictions or conflicts in the plan",
        "assessing completeness against the user's intent",
        "computing final signal quality score",
        "applying gates (pass | skip | quarantine)",
        "writing validation report and final verdict to Soma",
    ],
}

# THE LIVE COPY of the distiller step list (this definition wins at import).
# The full explanation of what these lines are, and are not, is in the long
# comment above the first definition near the top of this file. In short: the
# lines are real steps of the work for this chatset, printed one at a time
# WHILE the model call is running so the operator sees progress as it happens
# (not flooded after the call returns). The seconds beside them are pacing,
# not per-step measurements; the call's measured duration is in the result
# block. They are NOT fabricated output.
_DISTILLER_STEPS = _STAGE_STEPS["distiller"]


# ── ASCII line format ───────────────────────────────────────────────────
#
# One line per step, fixed width, ASCII only:
#   [SES-001] step description        ~~~ <DI-01> ~05s~
# Session and stage tags are bold cyan; filler and time are dim.
import re as _re

_LINE_WIDTH = 100
_ANSI_RE = _re.compile(r"\033\[[0-9;]*m")


def _visible_len(s: str) -> int:
    """Length of a string with ANSI escapes stripped."""
    return len(_ANSI_RE.sub("", s))


def _cells(s: str) -> int:
    """Width of a string on screen, in terminal cells.

    ``len`` counts ``⏳`` as one, but a terminal draws it two cells wide. A line
    cleared by character count is then one cell short and the last character of
    the old line survives on the next printed line.
    """
    import unicodedata
    total = 0
    for ch in _ANSI_RE.sub("", s):
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def _format_step_line(session_tag: str, step: str, stage_tag: str,
                      right: str, width: int = _LINE_WIDTH,
                      truncate: bool = True) -> str:
    """Format one step/status/log line.

    left  -> bold cyan [SES-001] + step text
    right -> bold cyan <DI-01> + right field (time, ERROR, log link)
    middle-> dim ~ filler so the right side always lands at the same column
    truncate=False lets long lines (errors, log links) overflow instead of
    being cut.
    """
    left = f"{_rail(session_tag, _C_CYAN)} {step}"
    right = f"{_rail(stage_tag, _C_MAGENTA)} {right}"
    gap = width - _visible_len(left) - _visible_len(right) - 1
    if gap < 0 and truncate:
        # Truncate the step text with an ellipsis to keep the right side.
        plain = _ANSI_RE.sub("", step)
        prefix_len = _visible_len(left) - len(plain)
        keep = width - _visible_len(right) - prefix_len - 3
        if keep > 0 and len(plain) > keep:
            step = plain[:keep] + "..."
        else:
            step = plain
        left = f"{_rail(session_tag, _C_CYAN)} {step}"
        gap = width - _visible_len(left) - _visible_len(right) - 1
    filler = f"{_C_STEEL}{'⟜' * max(0, gap)}{_C_RESET}"
    return f"{left}{filler}{right}"


def _rail(text: str, colour: str = _C_CYAN) -> str:
    """``┅┄⟦SES-006⟧┅┄``: a tag with a dim rail either side, the tag in colour."""
    return (f"{_C_DIM}┅┄{_C_RESET}{_C_BOLD}{colour}⟦{text}⟧{_C_RESET}"
            f"{_C_DIM}┅┄{_C_RESET}")


def _time_field(elapsed: float) -> str:
    return f"{_C_DIM}~{max(0, int(elapsed)):02d}s~{_C_RESET}"


_ART_COLOURS = (_C_CYAN, _C_MAGENTA, _C_ORANGE, _C_GREEN, _C_BLUE, _C_YELLOW)
_ART_STEP = [0]


def _print_divider() -> None:
    """Random ASCII art + random phrase, printed before a stage starts."""
    art, phrase = ascii_dividers.random_divider()
    with _io_lock:
        colour = _ART_COLOURS[_ART_STEP[0] % len(_ART_COLOURS)]
        _ART_STEP[0] += 1
        for line in art:
            print(f"  {_C_BOLD}{colour}{line}{_C_RESET}", flush=True)
        print(f"  {_C_DIM}{phrase}{_C_RESET}", flush=True)
        print(flush=True)

_COL_HEADERS = [
    "STAGE",
    "CHARS IN",
    "CHARS OUT",
    "STATUS",
    "SIGNALS",
    "TASKS",
    "OUTCOME",
    "MODEL",
]

# Fixed column widths matching the user's spec exactly
_COL_WIDTHS = [15, 12, 12, 22, 10, 10, 10, 34]


# ── Codes and log links ─────────────────────────────────────────────────
#
# The terminal shows the codes a human can quote: ``SES-120`` for a session
# and ``DI-01`` for a stage. The uuid is never printed, and a code that cannot
# be resolved degrades to a literal ``unknown`` rather than raising — display
# code must not be able to take a run down.

_UNKNOWN_CODE = "unknown"

#: Stage turn shown on screen. The pipeline runs one turn per stage today;
#: the code carries the number so a later multi-turn run needs no new format.
STAGE_TURN = 1


def _remember(session_id: str, code: str) -> str:
    """Record the id -> code pairing so printed text can be scrubbed."""
    if session_id:
        with _SEEN_LOCK:
            _SEEN[session_id] = code
    return code


def _session_code(session_id: str, code: str | None = None) -> str:
    """Short session code, or ``unknown`` when it cannot be resolved.

    ``code`` is the run's already-resolved value. When the caller passes it,
    no store lookup happens here: the display reads the same code the run
    resolved at selection instead of asking a store that may have changed
    since. Without it this module still resolves its own, which keeps direct
    callers (tests, one-off renders) working.

    Resolving a code also records it, so text built elsewhere with the raw id
    can be scrubbed at print time.
    """
    sid = str(session_id or "").strip()
    if code:
        return _remember(sid, code)
    try:
        resolved = short_codes.code_for(sid)
    except Exception:  # noqa: BLE001 - display must not raise
        return _UNKNOWN_CODE
    return _remember(sid, resolved)


def _stage_code(stage: str, turn: int = STAGE_TURN) -> str:
    """Short stage code, or ``unknown`` for a stage outside the map."""
    try:
        return short_codes.stage_code(stage, turn)
    except Exception:  # noqa: BLE001 - display must not raise
        return _UNKNOWN_CODE


def code_tag(stage: str, session_id: str, turn: int = STAGE_TURN,
             session_code: str | None = None) -> str:
    """``[SES-120][DI-01]`` for one stage execution."""
    return (f"[{_session_code(session_id, session_code)}]"
            f"[{_stage_code(stage, turn)}]")


#: Session ids seen on screen, mapped to their codes. The engine formats a
#: few of its own lines with the raw id; keeping the ids here lets every
#: printed string be scrubbed at the boundary, so the uuid cannot leak no
#: matter which caller built the text.
_SEEN: dict[str, str] = {}
_SEEN_LOCK = threading.Lock()


def _scrub(text: str) -> str:
    """Replace every known session id in ``text`` with its short code."""
    if not text or not _SEEN:
        return text
    with _SEEN_LOCK:
        pairs = sorted(_SEEN.items(), key=lambda kv: len(kv[0]), reverse=True)
    for sid, code in pairs:
        if sid in text:
            text = text.replace(sid, code)
    return text


_OUT_DIR: Path | None = None


def set_output_dir(path) -> None:
    """Tell the display where this run writes its logs.

    The engine owns its output directory and the logger writes the session
    sinks under it, so the engine sets it here once and every link printed
    afterwards points at the file the logger actually wrote.
    """
    global _OUT_DIR
    _OUT_DIR = Path(path) if path is not None else None


def _cli_out_dir() -> Path | None:
    """``--out`` from this process's own command line, if it was given.

    The engine's output directory reaches the logger but not this module, and
    the launcher and ``run_engine.py`` both pass it as ``--out``. Reading the
    argv already in hand avoids a filesystem search and cannot pick up a
    directory from an unrelated run.
    """
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--out" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if arg.startswith("--out="):
            return Path(arg.split("=", 1)[1])
    return None


def output_dir() -> Path:
    """Directory the stage logs live under.

    Resolution order: what the engine registered, then ``SOMA_OUT_DIR``, then
    this command's ``--out``, then the logger's own default of
    ``<cwd>/pipeline_out``. Whichever wins, it is an absolute path, which is
    what makes the link clickable.
    """
    if _OUT_DIR is not None:
        return _OUT_DIR
    override = os.environ.get("SOMA_OUT_DIR")
    if override:
        return Path(override)
    cli = _cli_out_dir()
    return cli if cli is not None else Path.cwd() / "pipeline_out"


def _log_name(stage: str, session_id: str, turn: int = STAGE_TURN) -> str:
    return f"{_session_code(session_id)}_{_stage_code(stage, turn)}.log"


def _log_candidates(stage: str, session_id: str, out_dir=None,
                    turn: int = STAGE_TURN) -> list[Path]:
    """Absolute paths the sink could occupy, most authoritative first.

    The engine owns its output directory and the logger writes there, while
    this module is only told about it through ``set_output_dir`` or
    ``SOMA_OUT_DIR``. Checking a short, fixed list lets a link point at the
    file that exists instead of at a guess, and the list is never widened by
    searching the filesystem.
    """
    name = _log_name(stage, session_id, turn)
    rel = Path("logs") / stage.lower() / name
    roots: list[Path] = []
    if out_dir is not None:
        roots.append(Path(out_dir))
    if _OUT_DIR is not None:
        roots.append(_OUT_DIR)
    if os.environ.get("SOMA_OUT_DIR"):
        roots.append(Path(os.environ["SOMA_OUT_DIR"]))
    cli = _cli_out_dir()
    if cli is not None:
        roots.append(cli)
    roots.append(Path.cwd() / "pipeline_out")
    roots.append(Path.cwd())
    return [(root / rel).resolve() for root in roots]


def _discover_log(stage: str, session_id: str,
                  turn: int = STAGE_TURN) -> Path | None:
    """Find the sink the logger wrote, two directory levels under CWD.

    The engine chooses its output directory and hands it to the logger, not
    to this module; until it does, the sink still exists on disk under that
    directory. Two levels is enough for ``<out>/logs/<stage>/`` and keeps the
    lookup bounded — this never walks a tree.
    """
    name = _log_name(stage, session_id, turn)
    tail = Path("logs") / stage.lower() / name
    for depth in ("*", "*/*"):
        try:
            hits = sorted(Path.cwd().glob(str(Path(depth) / tail)))
        except OSError:
            return None
        for hit in hits:
            if hit.is_file():
                return hit.resolve()
    return None


def log_path(stage: str, session_id: str, out_dir=None,
             turn: int = STAGE_TURN) -> Path:
    """The per-session per-stage log the logger writes for this execution.

    The returned path is always absolute. A candidate that exists wins, then
    a sink found under ``<cwd>/<something>/logs/<stage>/``, then the first
    candidate — so the link points at a real file whenever one is there.
    """
    candidates = _log_candidates(stage, session_id, out_dir, turn)
    for path in candidates:
        if path.exists():
            return path
    found = _discover_log(stage, session_id, turn)
    return found if found is not None else candidates[0]


def log_uri(stage: str, session_id: str, out_dir=None,
            turn: int = STAGE_TURN) -> str:
    """Clickable absolute ``file://`` URI for this stage's log.

    Built with ``Path.resolve().as_uri()`` rather than string concatenation,
    so the path is absolute and correctly escaped. A relative path is not
    clickable in any terminal that linkifies, which is the whole point.
    """
    return log_path(stage, session_id, out_dir, turn).resolve().as_uri()


# ── Helpers ─────────────────────────────────────────────────────────────

def _clean_model(model: str, gateway_map: dict | None = None) -> str:
    """Format model display name using gateway map when available.

    With gateway map: look up gateway for model_id, format as ``model_name-gateway``.
    Without gateway map (or model missing): fall back to uppercased name.

    Examples (with map):
        ``inclusionai/ling-3.0-flash`` (novita) → ``ling-3.0-flash-novita``
        ``nvidia/nemotron-3.5:free`` (openrouter) → ``nemotron-3.5-openrouter``
        ``gpt-4o`` (openai) → ``gpt-4o-openai``

    Examples (without map / fallback):
        ``inclusionai/ling-3.0-flash`` → ``LING-3.0-FLASH (INCLUSIONAI)``
    """
    if not model:
        return ""
    if gateway_map and model in gateway_map:
        gw = gateway_map[model]
        # Strip provider prefix (everything before /)
        name = model.split("/", 1)[1] if "/" in model else model
        # Strip tag at :
        name = name.split(":", 1)[0]
        return f"{name}-{gw}"
    # Fallback — uppercase model name with provider/tag
    parts = model.split("/", 1)
    provider = parts[0].upper() if len(parts) > 1 else None
    model_part = parts[1] if len(parts) > 1 else parts[0]
    mp = model_part.split(":", 1)
    name = mp[0].strip()
    tag = mp[1].strip() if len(mp) > 1 else None
    suffix = tag.upper() if tag else (provider if provider else None)
    if suffix:
        return f"{name.upper()} ({suffix})"
    return name.upper()


def _cell(text: str, width: int, align: str = "left") -> str:
    """Format one cell within its column."""
    text = str(text) if text is not None else ""
    if align == "right":
        return f"{text:>{width}}"
    return f"{text:<{width}}"


def _fmt_number(n) -> str:
    """Format an integer with commas for display."""
    if n is None or n == "":
        return ""
    try:
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)


def _top_line(widths: list[int]) -> str:
    """Top border: ╔══╦══╦═╗"""
    parts = []
    for i, w in enumerate(widths):
        sep = "╦" if i < len(widths) - 1 else "╗"
        parts.append("=" * w + sep)
    return "╔" + "".join(parts)


def _bottom_line(widths: list[int]) -> str:
    """Bottom border: ╚══╩══╩═╝"""
    parts = []
    for i, w in enumerate(widths):
        sep = "╩" if i < len(widths) - 1 else "╝"
        parts.append("=" * w + sep)
    return "╚" + "".join(parts)


def _hsep(widths: list[int]) -> str:
    """Header/body separator: ╠══╬══╬═╣"""
    parts = []
    for i, w in enumerate(widths):
        sep = "╬" if i < len(widths) - 1 else "╣"
        parts.append("=" * w + sep)
    return "╠" + "".join(parts)


def _row(cells: list[str], widths: list[int]) -> str:
    """One data row."""
    parts = []
    for i, (c, w) in enumerate(zip(cells, widths)):
        parts.append(_cell(c, w) + "║")
    return "║" + "".join(parts)


# ── Session Header ──────────────────────────────────────────────────────

def session_header(session_id: str, total_sets: int,
                   session_code: str | None = None) -> str:
    """Session header block.

    The line inside the frame is the session's short code. The uuid identifies
    the session to the store; it means nothing to the operator reading the
    screen and is far too long to scan.

    ``session_code`` is the value the run resolved at selection. Passing it
    means the header cannot disagree with the sink filenames or the debug
    events, which are given the same value.
    """
    code = _session_code(session_id, session_code)
    sets = f" {total_sets} CHAT SETS "
    c, m, r = _C_BOLD + _C_CYAN, _C_BOLD + _C_MAGENTA, _C_RESET
    lines = [
        "",
        f"{c}╒{'═' * len(code)}╕{r} {m}┏{'━' * len(sets)}┓{r}",
        f"{c}│{code}│{r} {m}┃{_C_WHITE}{sets}{m}┃{r}",
        f"{c}╘{'═' * len(code)}╛{r} {m}┗{'━' * len(sets)}┛{r}",
        "",
    ]
    return "\n".join(lines)


def chatset_count_line(number: int, total: int, chars: int) -> str:
    """``  Chatset 3/6 character count: 7,469`` with the numbers picked out."""
    return (f"  {_C_DIM}Chatset {number}/{total} character count:{_C_RESET} "
            f"{_C_BOLD}{_C_WHITE}{chars:,}{_C_RESET}")


def ingestion_banner(label: str, session_id: str, total_sets: int,
                     workers: int, session_code: str | None = None) -> str:
    """The stage banner and the one line that says what is about to run.

    ``    ┏◚◚◚◚◚◚◚◚◚┓`` / `` INGESTION`` / ``    ┗◛◛◛◛◛◛◛◛◛┛``, then
    ``┅┄⟦SES-006⟧┅┄ ⟜ ┅┄⟦6 total chatset(s)⟧┅┄ ⟜ ┅┄⟦Worker(s): 1⟧┅┄``.
    """
    code = _session_code(session_id, session_code)
    text = str(label or "").strip()
    o, r = _C_BOLD + _C_ORANGE, _C_RESET
    lines = [
        "",
        f"    {o}┏{'◚' * len(text)}┓{r}",
        f"     {_C_BOLD}{_C_WHITE}{text}{r}",
        f"    {o}┗{'◛' * len(text)}┛{r}",
        "",
        " " + f" {_C_STEEL}⟜{r} ".join([
            _rail(code, _C_CYAN),
            _rail(f"{total_sets} total chatset(s)", _C_GREEN),
            _rail(f"Worker(s): {workers}", _C_YELLOW)]),
    ]
    return "\n".join(lines)


# ── Chatset Table ───────────────────────────────────────────────────────

def _chars_out(sd: dict) -> str:
    """Characters the stage produced.

    The store keeps the verbatim response for the distiller, so its output
    size is measurable. Beavers only keep a token count, and tokens are not
    characters, so the cell stays blank rather than showing a number that
    means something else.
    """
    raw = sd.get("raw_response")
    if isinstance(raw, str) and raw:
        return _fmt_number(len(raw))
    out = sd.get("chars_out")
    if isinstance(out, (int, float)) and out:
        return _fmt_number(int(out))
    return ""


def _status_cell(sd: dict | None) -> str:
    """One unambiguous status word, including why a stage did not run."""
    if sd is None:
        return "NOT RUN"
    status = (sd.get("status") or "PENDING").upper()
    if status == "SKIPPED":
        reason = (sd.get("skip_reason") or "").strip()
        return f"SKIP {reason}" if reason else "SKIP"
    if status in ("FAILED", "BLOCKED"):
        errs = sd.get("errors")
        if isinstance(errs, str) and errs.strip():
            return f"{status} {errs.strip()[:14]}"
        return status
    if status == "DONE":
        verdict = str(sd.get("verdict") or "").strip()
        if verdict.lower() in ("true", "1", "yes"):
            # The stage ran and decided there was nothing to keep.
            return "DONE (NO SIGNAL)"
        return "DONE"
    return status


def _stage_counts(sd: dict | None) -> tuple[str, str, str]:
    """Signal / task / outcome counts from the stored output, if present."""
    if not sd:
        return "", "", ""
    parsed = sd.get("output")
    if not isinstance(parsed, dict):
        raw = sd.get("raw_response")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                import json as _json
                parsed = _json.loads(raw)
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        return "", "", ""

    body = unwrap_output(parsed)

    def _n(*keys: str) -> str:
        total = 0
        found = False
        for key in keys:
            val = body.get(key)
            if isinstance(val, list):
                total += len(val)
                found = True
        return str(total) if found else ""

    return (_n("directives", "preferences", "questions", "confirmations"),
            _n("tasks", "plan_items", "files"),
            _n("outcomes", "deliverables", "lessons"))


def _find_key(obj, key: str):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def chatset_table(set_num, total: int, stages: dict,
                  gateway_map: dict | None = None,
                  position: int | None = None) -> str:
    """Render the full table for one completed chatset.

    Every cell is either a measured character count or an explicit status.
    Tokens are deliberately absent: they vary by provider and do not describe
    how much text moved through the stage.

    ``stages``: dict keyed by stage name, values are checkpoint dicts
    (status, verdict, model, chars_in, raw_response, errors, ...).
    ``gateway_map``: optional model_id -> gateway lookup for _clean_model.
    """
    rows: list[list[str]] = []

    for sname in _STAGE_ORDER:
        sd = stages.get(sname)
        label = _STAGE_LABELS.get(sname, sname.upper())

        chars_in = ""
        if sd:
            ci = sd.get("chars_in")
            if isinstance(ci, (int, float)) and ci > 0:
                chars_in = _fmt_number(int(ci))

        signals, tasks, outcome = _stage_counts(sd)
        rows.append([
            label,
            chars_in,
            _chars_out(sd) if sd else "",
            _status_cell(sd),
            signals,
            tasks,
            outcome,
            _clean_model((sd or {}).get("model") or "", gateway_map),
        ])

    # Build lines using fixed column widths
    lines: list[str] = []

    # Three different numbers were in play: the set number inside the
    # session (49306), how many chatsets that session has (44), and how many
    # of them this run is doing (34). Reporting one as the other is what made
    # the progress line contradict the header.
    marker_text = f"CHAT SET {set_num}/{total}"
    if position is not None and position != set_num:
        marker_text += f"  ({position} of {total} this run)"
    marker_side = "█" * 7
    lines.append(f"\n{marker_side}   {marker_text}   {marker_side}")
    lines.append("")

    lines.append(_top_line(_COL_WIDTHS))
    lines.append(_row(_COL_HEADERS, _COL_WIDTHS))
    lines.append(_hsep(_COL_WIDTHS))
    for r in rows:
        lines.append(_row(r, _COL_WIDTHS))
    lines.append(_row([""] * len(_COL_HEADERS), _COL_WIDTHS))
    lines.append(_bottom_line(_COL_WIDTHS))
    lines.append("")

    return "\n".join(lines)


# ── Summary ─────────────────────────────────────────────────────────────

def session_summary(done: int, failed: int, skipped: int, total: int) -> str:
    """Final per-session tally.

    ``done`` counts chatsets the distiller processed, ``skipped`` counts
    chatsets it judged signal-free, ``failed`` counts chatsets that errored.
    The three add up to ``total``.
    """
    accounted = done + failed + skipped
    lines = [
        "",
        "  ┌─ SESSION COMPLETE " + "─" * 34,
        f"  │  chatsets        {total}",
        f"  │  distilled       {done}",
        f"  │  skipped         {skipped}   (distiller found no signal)",
        f"  │  failed          {failed}",
        f"  │  accounted       {accounted}/{total}"
        + ("" if accounted == total else "   ← MISMATCH"),
        "  └" + "─" * 52,
        "",
    ]
    return "\n".join(lines)


# ── Live stage line ─────────────────────────────────────────────────────
#
# Exactly one running stage owns the terminal line. The owner rewrites that
# line in place with a carriage return (spinner glyph + elapsed seconds) and
# freezes it with a single newline when the stage returns. Stage 1 runs on
# several worker threads, so extra spinners stay silent rather than fighting
# for the line, and ordinary output is queued until the line is free.

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_TICK = 0.1
_FROZEN_GLYPH = "✓"

_io_lock = threading.RLock()
_active_line: "_LiveLine | None" = None
_deferred: list[tuple[tuple, dict]] = []


class _LiveLine:
    """One spinner line, owned by at most one stage at a time."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._owns = False
        self._width = 0

    def open(self) -> None:
        global _active_line
        with _io_lock:
            self._owns = _active_line is None
            if self._owns:
                _active_line = self
        if not self._owns:
            return
        self._thread = threading.Thread(
            target=self._spin, daemon=True, name="stage-spinner")
        self._thread.start()

    def _spin(self) -> None:
        frames = _SPINNER_FRAMES
        idx = 0
        while True:
            self._draw(frames[idx % len(frames)])
            idx += 1
            if self._stop.wait(_SPINNER_TICK):
                return

    def _elapsed(self) -> int:
        return int(time.monotonic() - self._started)

    def _draw(self, glyph: str) -> None:
        body = f"  {glyph} {self.label}... {self._elapsed()}s"
        padding = " " * max(0, self._width - len(body))
        self._width = len(body)
        with _io_lock:
            sys.stdout.write("\r" + body + padding)
            sys.stdout.flush()

    def close(self) -> None:
        global _active_line
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if not self._owns:
            return
        # Freeze the final elapsed value on the same line, then end the line.
        self._draw(_FROZEN_GLYPH)
        with _io_lock:
            sys.stdout.write("\n")
            sys.stdout.flush()
            if _active_line is self:
                _active_line = None
        _flush_deferred()


class LiveTicker:
    """One terminal line, redrawn in place while a call is in flight.

    Owns exactly one line. Other output must clear() first, or its
    carriage-return redraws will land on top of the printed text.

    Disabled when stdout is not a terminal: a pipe has no carriage return,
    so redrawing there would emit one line per tick.
    """

    def __init__(self, text_fn, interval: float = 0.5) -> None:
        self._text_fn = text_fn
        self._interval = interval
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._width = 0
        self._held = False
        # In-place redraw needs a real terminal with a working carriage
        # return. Launcher-spawned terminal windows report a tty but some
        # don't honor \r, which turns every tick into a new scroll line.
        # TERM=dumb or a missing TERM also means "no cursor control".
        term = os.environ.get("TERM", "")
        self._enabled = sys.stdout.isatty() and term not in ("", "dumb")
        # Non-tty fallback: print the elapsed line at widening marks instead
        # of every tick, so a piped log stays one line per mark, not per 0.5s.
        self._last_mark = 0

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="stage-ticker")
        self._thread.start()

    def _run(self) -> None:
        started = time.monotonic()
        if self._enabled:
            while not self._stop.wait(self._interval):
                self._draw()
            return
        # Fallback: widening-interval elapsed prints (5s, 15s, 30s, ...),
        # then one line per minute. One line per mark, never per tick.
        marks = (5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
        for mark in marks:
            if self._stop.wait(max(0.0, mark - (time.monotonic() - started))):
                return
            self._print_mark()
        while not self._stop.wait(60.0):
            self._print_mark()

    def _print_mark(self) -> None:
        try:
            text = self._text_fn()
        except Exception:
            return
        with _io_lock:
            print(text, flush=True)

    def _draw(self) -> None:
        try:
            text = self._text_fn()
        except Exception:
            return
        with _io_lock:
            if self._held:
                return
            pad = " " * max(0, self._width - _cells(text))
            self._width = max(self._width, _cells(text))
            sys.stdout.write("\r" + text + pad)
            sys.stdout.flush()

    def clear(self) -> bool:
        """Erase the line so a normal print can use it."""
        if not self._enabled:
            return False
        with _io_lock:
            if self._held:
                return False
            self._held = True
            sys.stdout.write("\r" + " " * self._width + "\r")
            sys.stdout.flush()
            return True

    def redraw(self) -> None:
        with _io_lock:
            self._held = False
        self._draw()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._enabled and self._width:
            with _io_lock:
                sys.stdout.write("\r" + " " * self._width + "\r")
                sys.stdout.flush()
            self._width = 0


# The ticker currently owning the bottom line, if any.
_ticker: "LiveTicker | None" = None


def _scrub_args(args: tuple) -> tuple:
    """Apply the session-id scrub to string arguments of a print call."""
    return tuple(_scrub(a) if isinstance(a, str) else a for a in args)


def print_live(*args, **kwargs) -> None:
    """Print without destroying the live line underneath."""
    held = _ticker.clear() if _ticker is not None else False
    with _io_lock:
        print(*_scrub_args(args), **kwargs)
        sys.stdout.flush()
    if held and _ticker is not None:
        _ticker.redraw()


def _flush_deferred() -> None:
    """Print output queued while a spinner owned the line."""
    while True:
        with _io_lock:
            if _active_line is not None or not _deferred:
                return
            args, kwargs = _deferred.pop(0)
            print(*_scrub_args(args), **kwargs)


def emit(*args, **kwargs) -> None:
    """Print normally, preserving whatever live line is on screen."""
    with _io_lock:
        if _active_line is not None:
            _deferred.append((args, kwargs))
            return
    print_live(*args, **kwargs)


def spin(label: str, fn, *args, **kwargs):
    """Run ``fn`` behind a one-line spinner showing elapsed seconds."""
    line = _LiveLine(label)
    line.open()
    try:
        return fn(*args, **kwargs)
    finally:
        line.close()


# ── Stage timing ────────────────────────────────────────────────────────
#
# The distiller keeps its ~30 s step display: it is the one call long enough
# that a blank wait looks like a hang.
#
# Every later stage is one line. Before, a stage printed three unrelated
# pieces under two different formats:
#
#   ✓ introbeaver trevino.war.room CS49306 turnkey... 13s      <- spinner, "✓" even on failure
#   -[INTROBEAVER]- [FAILED] missing:introbeaver_output ...     <- result
#   chars in   3,296 / plan   0 file(s) ...                      <- detail block
#
# The spinner froze on ✓ regardless of outcome, so a failed stage looked
# successful, and the model was printed as the literal word "turnkey"
# because EngineClient.model_for() returns a placeholder. Anything long
# enough to be worth watching gets its own live line with a real model.

_STAGE_START: dict[str, float] = {}
_STAGE_WALL_START: dict[str, str] = {}
_STAGE_TIMES: dict[str, list[tuple[float, str, str]]] = {}
_STAGE_ORDER = ["distiller", "introbeaver", "planbeaver", "outrobeaver"]

# ── The distiller's step lines: what they are, and what they are not ───────
#
# READ THIS BEFORE CALLING ANY OF THIS "FAKE" OR "FABRICATED".
#
# Each line below names a real piece of the work done for THIS chatset in THIS
# call. The call to the model takes seconds to minutes. Rather than print
# nothing until it returns and then flood the screen with every line at once,
# the steps are printed one at a time WHILE the call is in flight
# (StageTimer._print_steps). The operator watches the work move as it
# happens. That is the design, and it is the model for the other stages: show
# the real steps live, never batch them after the fact.
#
# What is measured and what is not:
#   * The step NAMES and their ORDER follow the distiller's job (the PROCESSING
#     PROTOCOL in soma/machine-talk/tb-001-distiller.v2.md). They are a
#     paraphrase of it, not invented.
#   * The seconds beside each step are PACING, not a per-step measurement. The
#     pipeline cannot see the model finish "step 3 of 11" inside one request,
#     so the lines are spaced evenly (about 2.7 s apart, first 30 s only). Do
#     not read them as timings. The call's real duration is the `duration`
#     field in the result block (telemetry_result), and THAT one is measured.
#   * Most steps are done by the model inside the call (hash echo, signatures,
#     duplicates, chronology, classifying, the verdict). Three are Python's, not
#     the model's: "stripping noise", "logging noise purges + stats" and
#     "computing overall signal quality". tb-001 says noise is "measured
#     outside you", and soma/machinetalk/measure.py computes it right after the
#     reply. They stay in the list because that work still happens for this
#     chatset in this call's pipeline.
#
# This list is defined twice in this file. The later definition (near
# StageTimer) is the one in effect at import time. Keep both identical.
_DISTILLER_STEPS = _STAGE_STEPS["distiller"]


def _timestamp() -> str:
    return time.strftime("%H:%M:%S")


def stage_clock_start(stage: str) -> None:
    """Record when a stage's model call began.

    Two clocks are kept for the same instant. The monotonic one measures the
    duration; the wall clock one is what the operator prints, because
    ``31.4s`` says nothing about when the stage ran.
    """
    _STAGE_START[stage] = time.monotonic()
    _STAGE_WALL_START[stage] = time.strftime("%Y-%m-%d %H:%M:%S")


def stage_wall_start(stage: str) -> str:
    """Wall-clock time this stage's call began, or ``—`` if it never did."""
    return _STAGE_WALL_START.get(stage, "—")


def stage_clock_done(stage: str, status: str) -> float:
    """Record when a stage finished; return elapsed seconds."""
    started = _STAGE_START.pop(stage, None)
    elapsed = time.monotonic() - started if started is not None else 0.0
    _STAGE_TIMES.setdefault(stage, []).append((elapsed, status, _timestamp()))
    return elapsed


def stage_clock_reset() -> None:
    _STAGE_START.clear()
    _STAGE_WALL_START.clear()
    _STAGE_TIMES.clear()


def _stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage.upper())


def _verdict_glyph(status: str) -> tuple[str, str]:
    """Glyph and colour for a finished stage. Never ✓ for a failure."""
    head = status.upper()
    if head.startswith(("DONE", "PASS")):
        return "✓", _C_GREEN
    if head.startswith(("SKIP", "BLOCK")):
        return "⊘", _C_YELLOW
    return "✗", _C_RED


# ── Per-stage telemetry ────────────────────────────────────────────────
#
# One block per stage execution, printed immediately before the call and
# completed after it returns. Every value is measured: the timestamps come
# from the clock, the character counts from the strings, the tokens from the
# provider's usage block. Nothing here is estimated.
#
# The block exists so a run can be read after the fact without guessing what
# happened: which model served the call, whether it was the one announced,
# how much text went in and came out, and what the gate decided.

_TELEMETRY_WIDTH = 74


def _rule(char: str = "─") -> str:
    return "  " + _C_DIM + char * _TELEMETRY_WIDTH + _C_RESET


def _field(label: str, value: str, colour: str = "") -> str:
    colour = colour or _C_RESET
    return f"  {_C_DIM}{label:<12}{_C_RESET}{colour}{value}{_C_RESET}"


# One number per stage execution, rising for good (see short_codes.next_stage_run).
# The header takes it when the stage opens and the timer reads it back, so every
# line of that execution shows the same code.
_PENDING_RUNS: dict = {}
_PENDING_LOCK = threading.Lock()


def _run_code(stage: str) -> str:
    try:
        return short_codes.next_stage_run(stage)
    except Exception:  # noqa: BLE001 - display must not raise
        return _stage_code(stage)


def announce_run(stage: str, session: str, chatset) -> str:
    """Mint the run code for one stage execution and hold it for its timer."""
    code = _run_code(stage)
    with _PENDING_LOCK:
        _PENDING_RUNS[(str(session), str(chatset), stage)] = code
    return code


def take_run(stage: str, session: str, chatset) -> str:
    """The code the header announced for this execution, else a fresh one."""
    with _PENDING_LOCK:
        code = _PENDING_RUNS.pop((str(session), str(chatset), stage), None)
    return code or _run_code(stage)


_EVENT_COLOURS = {
    "request": _C_STEEL, "instructions": _C_CYAN, "input": _C_CYAN,
    "write": _C_YELLOW, "response": _C_GREEN, "gate": _C_WHITE,
    "anchors": _C_BLUE, "python": _C_MAGENTA, "soma": _C_ORANGE,
    "session": _C_STEEL,
}
_VERDICT_COLOURS = (("PASS", _C_GREEN), ("FAIL", _C_RED), ("QUARANTINE", _C_YELLOW),
                    ("SKIP", _C_YELLOW), ("proceed", _C_GREEN), ("halt", _C_RED))


def _event_text(label: str, text: str) -> str:
    """Colour the words that carry a verdict; leave every other word alone."""
    for word, colour in _VERDICT_COLOURS:
        text = text.replace(word, f"{_C_BOLD}{colour}{word}{_C_RESET}")
    return text


def stage_event(stage: str, session: str, label: str, text: str,
                session_code: str | None = None) -> str:
    """One real event line, printed at the moment the thing happens.

    ``HH:MM:SS ▸ [SES-007][IB-01] anchors      2/2 verified ...``

    The time is the moment of printing and every value in ``text`` is read
    from the call that just happened: nothing here is estimated.
    """
    tag = code_tag(stage, session, session_code=session_code)
    colour = _EVENT_COLOURS.get(label.strip(), _C_WHITE)
    return (f"  {_C_DIM}{_timestamp()}{_C_RESET} {colour}▸{_C_RESET} "
            f"{_C_DIM}{tag}{_C_RESET} {_C_BOLD}{colour}{label:<13}{_C_RESET}"
            f"{_event_text(label, text)}")


def session_link(log_link: str) -> str:
    """The engine's page for the whole session, from a stage's own link.

    The engine writes ``…/v1/soma/?session=SES-007&slug=…&stage=IB-72``. The
    session page is the same address without the slug and stage.
    """
    if not log_link or "?" not in log_link:
        return ""
    from urllib.parse import parse_qs, urlsplit
    parts = urlsplit(log_link)
    ses = (parse_qs(parts.query).get("session") or [""])[0]
    if not ses:
        return ""
    return f"{parts.scheme}://{parts.netloc}{parts.path}?session={ses}"


def telemetry_header(stage: str, session: str, chatset, chars_in: int,
                     model: str, provider: str,
                     session_code: str | None = None) -> str:
    """The opening block for one stage execution, before the call.

    The heading carries both codes, and the session field shows the code
    rather than the uuid it was resolved from.

    ``session_code`` is the run's resolved value; without it this header
    would ask the store a second time and could print a different code than
    the one the log sink is named after.
    """
    label = _stage_label(stage)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    ses = _session_code(session, session_code)
    run = announce_run(stage, session, chatset)
    tag = code_tag(stage, session, session_code=session_code)
    plain = f"{tag} {label}"
    inner = max(30, len(plain) + 8)
    pad = " " * (2 + (inner + 2 - len(plain)) // 2)
    frame = _C_BOLD + _C_ORANGE
    lines = [
        "",
        f"  {frame}▛{'▝' * inner} ▜{_C_RESET}",
        f"{pad}{_C_BOLD}{_C_CYAN}{tag}{_C_RESET} {_C_BOLD}{_C_WHITE}{label}{_C_RESET}",
        f"  {frame}▙ {'▖' * inner}▟{_C_RESET}",
        _rule("═"),
        _field("start", started, _C_STEEL),
        _field("session", ses, _C_CYAN),
        _field("stage", run, _C_MAGENTA + _C_BOLD),
        _field("chatset", f"CS{chatset}", _C_WHITE),
        _field("chars in", f"{chars_in:,}", _C_WHITE),
        _field("model", model or "unknown", _C_GREEN),
        _field("provider", provider or "unknown", _C_YELLOW),
        _rule("═"),
    ]
    return "\n".join(lines)


def telemetry_result(stage: str, *, elapsed: float, chars_in: int,
                     chars_out: int, prompt_tokens: int,
                     completion_tokens: int, total_tokens: int,
                     model: str, provider: str, status: str,
                     verdict: str = "", detail: str = "",
                     session: str = "", out_dir=None,
                     engine_session: str = "", engine_stage: str = "",
                     log_link: str = "", complaint: str = "") -> str:
    """The closing block for one stage execution, after it returns.

    Tokens are reported exactly as the provider returned them. A zero means
    the provider did not report usage, which is shown as ``—`` rather than
    ``0`` so an absent measurement is not mistaken for an empty one.

    ``started`` and ``duration`` are separate labels on purpose: the first is
    a wall-clock time, the second is measured elapsed seconds, and one cannot
    be recovered from the other when several chatsets run in parallel.
    """
    glyph, colour = _verdict_glyph(status)

    def tok(n: int) -> str:
        return f"{n:,}" if n else "—"

    ratio = ""
    if chars_in and chars_out:
        ratio = f"  {_C_DIM}({chars_out / chars_in:.2f}x){_C_RESET}"

    # Reasoning models bill their thinking as completion tokens and then
    # strip it from the message. A reply of 112 characters can carry 1,485
    # completion tokens, which looks like a contradiction until it is
    # labelled; without the note the operator reads it as a counting bug.
    hidden = ""
    if completion_tokens and chars_out:
        per_tok = chars_out / completion_tokens
        if per_tok < 1.5:
            hidden = (f"  {_C_DIM}(reply shorter than token count — "
                      f"thinking tokens are billed, not shown){_C_RESET}")

    lines = [
        _rule(),
        _field("started", stage_wall_start(stage)),
        _field("finished", time.strftime("%Y-%m-%d %H:%M:%S")),
        _field("duration", f"{elapsed:.1f}s"),
        _field("chars out", f"{chars_out:,}{ratio}"),
        _field("tokens", f"prompt {tok(prompt_tokens)}  "
                         f"completion {tok(completion_tokens)}  "
                         f"total {tok(total_tokens)}{hidden}"),
        _field("model", model or "unknown"),
        _field("provider", provider or "unknown"),
        _field("verdict", verdict or "—"),
        _field("status", status, colour),
    ]
    if detail:
        lines.append(_field("detail", detail[:96]))
    if complaint:
        # What the model itself said when it refused or complained.
        lines.append(_field("model said", complaint[:160], _C_YELLOW))
    if engine_session or engine_stage:
        lines.append(_field("engine id", f"{engine_session or '—'} / {engine_stage or '—'}"
                                         f"  {_C_DIM}(the engine's numbering, not the local one){_C_RESET}"))
    if log_link:
        lines.append(_field("stage log", log_link))
        sess = session_link(log_link)
        if sess:
            lines.append(_field("session log", sess))
    if session:
        # The link is the absolute file:// URI, never a relative path: a
        # terminal can only make an absolute URI clickable.
        lines.append(_field("local log", f"{_log_name(stage, session)}  "
                                         f"{log_uri(stage, session, out_dir)}"))
    lines.append(_rule("═"))
    lines.append("")
    return "\n".join(lines)


class StageTimer:
    """One live line per stage, ending in that stage's real outcome.

    For the distiller this drives the step display as well, so the operator
    sees the run progress inside the one call that lasts minutes. Every other
    stage gets a spinner that names the stage and the model actually serving
    it, then a single frozen line carrying the true result.
    """

    def __init__(self, stage: str, session: str, chatset, chars: int,
                 model: str = "", model_fn=None, out_dir=None,
                 session_code: str | None = None) -> None:
        self.stage = stage
        self.label = _stage_label(stage)
        self.session = session
        # The uuid is kept for the log link and nothing else; every line
        # this timer writes shows the resolved code. A code handed in is the
        # one used: this timer never asks the store when the run has already
        # decided.
        self.ses_code = _session_code(session, session_code)
        self.stg_code = _stage_code(stage)
        self.run_code = take_run(stage, session, chatset)
        self.tag = f"[{self.ses_code}][{self.stg_code}]"
        self.out_dir = out_dir
        self.chatset = chatset
        self.chars = chars
        self.model = model or ""
        # Optional callable returning the model the rotator would serve now.
        # Retries advance the deck, so a name captured at start goes stale.
        self.model_fn = model_fn
        self.attempt = 1
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._started = 0.0
        # Own wall-clock start, so several workers running the same stage in
        # parallel cannot overwrite each other's clock.
        self._wall_started = "—"
        self._steps = _STAGE_STEPS.get(stage, [])

    def start(self) -> None:
        self._started = time.monotonic()
        self._wall_started = time.strftime("%Y-%m-%d %H:%M:%S")
        stage_clock_start(self.stage)

        # Random art + phrase before every stage, including the first.
        _print_divider()

        # Header line: stage label, description, model, stage tag.
        model = self.model
        if self.model_fn is not None:
            try:
                model = self.model_fn() or model
            except Exception:
                pass
        header = (f"{_C_BOLD}{self.label}{_C_RESET}: "
                  f"{_STAGE_DESCRIPTIONS.get(self.stage, 'running')}"
                  + (f" ({model})" if model else ""))
        print_live(_format_step_line(self.ses_code, header,
                                     self.run_code, _time_field(0.0)))

        # The ticker owns the bottom line for every stage. Without it a
        # multi-minute call is indistinguishable from a hang: the step fill
        # stops after 30 s and then nothing moves.
        global _ticker
        _ticker = LiveTicker(self._line, interval=0.5)
        _ticker.start()

        if self._steps:
            self._thread = threading.Thread(
                target=self._print_steps, daemon=True,
                name=f"steps-{self.stage}")
            self._thread.start()
            return

        # Stages without a step list: the ticker is the whole story.
        self._thread = threading.Thread(
            target=self._spin, daemon=True, name=f"spin-{self.stage}")
        self._thread.start()

    def _line(self) -> str:
        """Live bottom line, rendered fresh on every tick.

        The model is re-read each tick rather than captured at start. A retry
        storm rotates the deck, so a frozen name shows one model "failing
        repeatedly" while five cards are actually being burned in turn.
        """
        elapsed = time.monotonic() - self._started
        model = self.model
        if self.model_fn is not None:
            try:
                model = self.model_fn() or model
            except Exception:
                pass
        tail = f" · {model}" if model else ""
        tries = f"  {_C_DIM}(attempt {self.attempt}){_C_RESET}" if self.attempt > 1 else ""
        return (f"  {_C_DIM}…{_C_RESET} {_C_BOLD}{_C_CYAN}{self.tag}{_C_RESET} "
                f"{_C_BOLD}{_C_WHITE}{self.label}{_C_RESET}{_C_DIM}{tail}{_C_RESET} "
                f"{_C_YELLOW}[⏳ {elapsed:.1f}s elapsed]{_C_RESET}{tries}")

    def _spin(self) -> None:
        """Keep the ticker alive when no step fill is running.

        The ticker redraws itself (in place on a terminal, as widening-mark
        lines otherwise), so this loop only needs to notice the stop.
        """
        self._stop.wait()

    def _print_steps(self) -> None:
        """The paced step fill for the one call that runs for minutes.

        These steps name real work done for THIS chatset in THIS call; they
        are not independently observable from here, so they pace evenly
        rather than claiming measured durations.

        NOT FABRICATED. Each step is real work for this chatset, printed here
        one at a time while the model call is in flight, instead of all at
        once after it returns. Only the seconds are pacing; the real duration
        is reported in the result block. See the comment above
        ``_STAGE_STEPS`` for which steps the model does and which are
        Python's (measure.py). Do not "fix" this by removing the live
        printing, and do not batch the lines after the call: showing the
        work as it happens is the point.

        On failure the loop stops early and prints the error line; all
        remaining steps are still printed afterwards so the operator sees
        the full list (pacing only, per the locked design).
        """
        steps = self._steps
        n = len(steps)
        per = max(1.5, 30.0 / n)
        failed = False

        for i, step in enumerate(steps, start=1):
            if self._stop.wait(per):
                failed = getattr(self, "_failed", False)
                break
            elapsed = time.monotonic() - self._started
            if failed:
                mark, colour = "✗", _C_RED
            else:
                mark, colour = "✓", _C_GREEN
            # print_live clears the ticker, prints, then redraws it below, so
            # the step line lands above the live timer instead of on top of it.
            print_live(_format_step_line(
                self.ses_code,
                f"{colour}{mark}{_C_RESET} [{i:02d}/{n}] {step}",
                self.run_code, _time_field(elapsed)))

        if failed:
            reason = getattr(self, "_failure_reason", "unknown error")
            elapsed = time.monotonic() - self._started
            print_live(_format_step_line(
                self.ses_code,
                f"{_C_RED}✗ FAILED: {reason}{_C_RESET}",
                self.run_code,
                f"{_C_DIM}~{_C_RED}ERROR{_C_RESET}{_C_DIM}~{_C_RESET}"))

        # Steps are done but the model may not have answered. The ticker
        # keeps counting, so only a coarse reminder is added.
        last = 0.0
        while not self._stop.wait(1.0):
            elapsed = time.monotonic() - self._started
            if elapsed - last < 60:
                continue
            last = elapsed
            print_live(_format_step_line(
                self.ses_code,
                f"{_C_YELLOW}…{_C_RESET} {self.label} still "
                f"waiting on the model ({elapsed:.0f}s)",
                self.run_code, _time_field(elapsed)))

    def finish(self, status: str, detail: str = "") -> float:
        """Stop the live line and print the stage's real result."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

        # The ticker holds the bottom line; it must go before the result
        # prints or the result lands on top of a half-drawn timer.
        global _ticker
        if _ticker is not None:
            _ticker.stop()
            _ticker = None

        elapsed = stage_clock_done(self.stage, status)
        # The timer's own start, not the shared per-stage entry: with several
        # workers on one stage, the shared entry belongs to whichever started
        # most recently.
        started_at = self._wall_started

        if sys.stdout.isatty():
            with _io_lock:
                sys.stdout.write("\r" + " " * 100 + "\r")
                sys.stdout.flush()

        # Status line and log link in the same ASCII frame as the steps.
        # The log URI is absolute file:// on its own line: a wrapped path is
        # hard to click, and a relative one cannot be clicked at all.
        glyph, colour = _verdict_glyph(status)
        self._failed = "FAILED" in status or "QUARANTINE" in status
        self._failure_reason = detail or status
        tail = f"  {_C_DIM}{detail}{_C_RESET}" if detail else ""
        with _io_lock:
            print(_format_step_line(
                self.ses_code,
                f"{colour}{glyph}{_C_RESET} Status: {colour}{status}{_C_RESET}"
                f"{_C_DIM} start {started_at}  duration {elapsed:>6.1f}s{_C_RESET}"
                f"{tail}",
                self.run_code, _time_field(elapsed)))
            print(_format_step_line(
                self.ses_code,
                f"{_C_DIM}Log:{_C_RESET} {log_uri(self.stage, self.session, self.out_dir)}",
                self.run_code, "", truncate=False))
        return elapsed


def stage_ledger() -> list[str]:
    """Per-stage totals for the end-of-run block."""
    if not _STAGE_TIMES:
        return []
    rows = []
    total = 0.0
    for stage in _STAGE_ORDER:
        entries = _STAGE_TIMES.get(stage)
        if not entries:
            continue
        secs = sum(e[0] for e in entries)
        total += secs
        worst = max(e[0] for e in entries)
        counts: dict[str, int] = {}
        for _, status, _ts in entries:
            key = status.split("(")[0].strip().lower() or status
            counts[key] = counts.get(key, 0) + 1
        mix = "  ".join(f"{k}×{v}" for k, v in sorted(counts.items()))
        rows.append(
            f"  {_stage_label(stage):<14}{len(entries):>4} call   "
            f"{secs:>8.0f}s   {secs / len(entries):>6.0f}s avg   "
            f"{worst:>6.0f}s max   {mix}")

    out = ["", "  " + "─" * 78,
           f"  {'STAGE':<14}{'':>4}       {'TOTAL':>8}   {'MEAN':>10}   "
           f"{'WORST':>10}   OUTCOMES",
           "  " + "─" * 78]
    out.extend(rows)
    out.append("  " + "─" * 78)
    out.append(f"  {'WALL CLOCK':<14}{total:>28.0f}s")
    out.append("  " + "─" * 78)
    return out


def print_stage_ledger() -> None:
    for line in stage_ledger():
        print(line, flush=True)


# ── Distiller result summary ────────────────────────────────────────────

def _fmt_bytes(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def unwrap_output(parsed):
    """Return a stage's own output object from a stored stage_output.

    The model returns ``{"_meta": {...}, "<stage>_output": {...}}``. Every
    field of interest lives inside that wrapper, so reading the envelope
    reports zeros. Payloads that are already flat pass through unchanged.

    The wrapper key differs per stage, so every ``*_output`` key is checked
    rather than only the distiller's.
    """
    if not isinstance(parsed, dict):
        return {}
    for key in ("distiller_output", "introbeaver_output",
                "planbeaver_output", "outrobeaver_output"):
        inner = parsed.get(key)
        if isinstance(inner, dict):
            return inner
    return parsed


def _count_of(stage_output: dict, key: str) -> int:
    v = stage_output.get(key)
    if isinstance(v, (list, tuple)):
        return len(v)
    return int(v) if isinstance(v, int) else 0


def distiller_summary(soma_path, chars_in, parsed) -> str:
    """One block stating what the distiller actually decided.

    Every number here is either a character count taken from the source or a
    value read out of the model's own output object. Nothing is estimated.
    """
    stage_output = unwrap_output(parsed)

    skip = bool(stage_output.get("skip"))
    skip_reason = str(stage_output.get("skip_reason") or "").strip()
    intent = str(stage_output.get("intent_anchor") or stage_output.get("intent") or "").strip()
    sigq = stage_output.get("signal_quality")

    counts = {k: _count_of(stage_output, k) for k in (
        "directives", "constraints", "preferences", "questions",
        "corrections", "confirmations", "artifacts")}

    if skip:
        decision = f"{_C_YELLOW}SKIPPED{_C_RESET}"
        if skip_reason:
            decision += f" {_C_DIM}({skip_reason}){_C_RESET}"
    else:
        decision = f"{_C_GREEN}PROCESSED{_C_RESET}"

    parts = [
        f"  {_C_BOLD}decision{_C_RESET}      {decision}",
    ]
    if isinstance(chars_in, int) and chars_in >= 0:
        parts.append(f"  {_C_BOLD}chars in{_C_RESET}     {chars_in:,}")
    if isinstance(sigq, (int, float)):
        parts.append(f"  {_C_BOLD}signal qual{_C_RESET}  {sigq:.2f}")
    if intent:
        parts.append(f"  {_C_BOLD}intent{_C_RESET}       {intent[:96]}")

    found = "  ".join(f"{k}={v}" for k, v in counts.items() if v)
    if found:
        parts.append(f"  {_C_BOLD}extracted{_C_RESET}    {found}")
    else:
        parts.append(f"  {_C_BOLD}extracted{_C_RESET}    {_C_DIM}nothing "
                     f"(all signal lists empty){_C_RESET}")

    parts.append(f"  {_C_BOLD}soma{_C_RESET}         {soma_path}")
    return "\n".join(parts)


# ── Per-stage result detail ─────────────────────────────────────────────
#
# One block per stage as it completes, so the CLI shows what the stage
# actually produced instead of only START/SKIP/FAIL lines. Every value
# printed is a field from that stage's own output contract.

def _pct(value) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def introbeaver_detail(chars_in, parsed) -> str:
    """TB-004: entry counts, signal quality, noise, artifacts, verdict."""
    body = unwrap_output(parsed)
    by_type = body.get("entry_count_by_type") or {}
    noise = body.get("noise_signature") or {}
    entries = body.get("entries") or []
    artifacts = body.get("artifacts") or []
    chron = body.get("chronology_resolved") or []
    discarded = body.get("discarded") or []

    parts = [f"  {_C_BOLD}chars in{_C_RESET}    {chars_in:,}"]
    parts.append(f"  {_C_BOLD}entries{_C_RESET}     {body.get('extracted_count', len(entries))}"
                 f"  " + "  ".join(f"{k}={v}" for k, v in by_type.items() if v))
    parts.append(f"  {_C_BOLD}signal{_C_RESET}      {_pct(body.get('signal_quality'))}"
                 f"   {_C_DIM}user msgs {body.get('user_message_count', '—')}"
                 f"  noise words {noise.get('noise_words', 0)}{_C_RESET}")

    noise_bits = [f"{k} {v}" for k, v in noise.items()
                  if k != "noise_words" and v]
    if noise_bits:
        parts.append(f"  {_C_BOLD}noise{_C_RESET}       {', '.join(noise_bits)}")

    if artifacts:
        kinds = {}
        for a in artifacts:
            kinds[a.get("artifact_type", "?")] = kinds.get(a.get("artifact_type", "?"), 0) + 1
        parts.append(f"  {_C_BOLD}artifacts{_C_RESET}   {len(artifacts)}  ("
                     + ", ".join(f"{k} {v}" for k, v in kinds.items()) + ")")

    if chron:
        superseded = sum(len(c.get("losers") or []) for c in chron)
        parts.append(f"  {_C_BOLD}chronology{_C_RESET}  {len(chron)} intent group(s), "
                     f"{superseded} superseded")

    if discarded:
        reasons = {}
        for d in discarded:
            r = d.get("reason", "?")
            reasons[r] = reasons.get(r, 0) + 1
        parts.append(f"  {_C_BOLD}discarded{_C_RESET}   {len(discarded)}  ("
                     + ", ".join(f"{k} {v}" for k, v in reasons.items()) + ")")

    status = body.get("status") or "—"
    colour = _C_GREEN if status == "PASS" else _C_YELLOW
    line = f"  {_C_BOLD}status{_C_RESET}      {colour}{status}{_C_RESET}"
    if body.get("block_reason"):
        line += f"   {_C_DIM}{body['block_reason']}{_C_RESET}"
    if body.get("skip_reason"):
        line += f"   {_C_DIM}{body['skip_reason']}{_C_RESET}"
    parts.append(line)
    return "\n".join(parts)


def planbeaver_detail(chars_in, parsed) -> str:
    """TB-005: files, functions, line budget, contracts, execution order."""
    body = unwrap_output(parsed)
    files = body.get("files") or []
    budget = body.get("budget") or {}
    sequence = body.get("execution_sequence") or []
    contracts = body.get("data_contracts") or []

    functions = sum(len(f.get("functions") or []) for f in files)
    lines = body.get("estimated_lines_total")
    if lines is None:
        lines = sum(int(f.get("estimated_lines") or 0) for f in files)

    parts = [f"  {_C_BOLD}chars in{_C_RESET}    {chars_in:,}"]
    parts.append(f"  {_C_BOLD}plan{_C_RESET}        {len(files)} file(s)   "
                 f"{functions} function(s)   ~{lines:,} line(s)")
    if budget:
        parts.append(f"  {_C_BOLD}budget{_C_RESET}      "
                     + "  ".join(f"{k}={v}" for k, v in budget.items()))
    if contracts:
        parts.append(f"  {_C_BOLD}contracts{_C_RESET}   {len(contracts)}")
    if sequence:
        head = " → ".join(str(s) for s in sequence[:6])
        more = "" if len(sequence) <= 6 else f" → … (+{len(sequence) - 6})"
        parts.append(f"  {_C_BOLD}sequence{_C_RESET}    {head}{more}")
    if body.get("cycle"):
        parts.append(f"  {_C_BOLD}cycle{_C_RESET}       {_C_YELLOW}{body['cycle']}{_C_RESET}")
    parts.append(f"  {_C_BOLD}status{_C_RESET}      {body.get('status') or '—'}")
    return "\n".join(parts)


def outrobeaver_detail(chars_in, parsed) -> str:
    """TB-006: journal size, clarity, drift, friction, session health."""
    body = unwrap_output(parsed)
    journal = body.get("journal_entries") or []
    analysis = body.get("clarity_and_communication_analysis") or {}
    summary = body.get("summary") or {}
    comp = body.get("health_components") or {}
    drift = analysis.get("drift_events") or []
    friction = analysis.get("friction_points") or []

    parts = [f"  {_C_BOLD}chars in{_C_RESET}    {chars_in:,}"]
    parts.append(f"  {_C_BOLD}journal{_C_RESET}     {len(journal)} entr(ies)")

    clarity = analysis.get("session_clarity_score")
    if clarity is not None:
        parts.append(f"  {_C_BOLD}clarity{_C_RESET}     {_pct(clarity)}   "
                     f"{_C_DIM}user reps {analysis.get('user_repetition_count', 0)}"
                     f"  agent reps {analysis.get('agent_repetition_count', 0)}"
                     f"  friction {_pct(analysis.get('friction_rate'))}{_C_RESET}")

    if drift:
        worst = max((d.get("severity", "MINOR") for d in drift),
                    key=lambda s: ["MINOR", "MODERATE", "MAJOR", "CRITICAL"].index(s)
                    if s in ["MINOR", "MODERATE", "MAJOR", "CRITICAL"] else 0)
        parts.append(f"  {_C_BOLD}drift{_C_RESET}       {len(drift)} event(s)  worst {worst}")
    if friction:
        kinds = {}
        for f in friction:
            k = f.get("type", "?")
            kinds[k] = kinds.get(k, 0) + 1
        parts.append(f"  {_C_BOLD}friction{_C_RESET}    "
                     + ", ".join(f"{k} {v}" for k, v in kinds.items()))

    if comp:
        parts.append(f"  {_C_BOLD}health{_C_RESET}      score {_pct(comp.get('score'))}   "
                     f"{_C_DIM}clarity {_pct(comp.get('clarity'))}"
                     f"  drift −{_pct(comp.get('drift_penalty'))}"
                     f"  friction −{_pct(comp.get('friction_penalty'))}{_C_RESET}")

    if summary:
        health = summary.get("session_health", "—")
        colour = (_C_GREEN if health == "GREEN"
                  else _C_YELLOW if health == "YELLOW" else _C_RED
                  if "_C_RED" in globals() else _C_YELLOW)
        parts.append(f"  {_C_BOLD}session{_C_RESET}     {colour}{health}{_C_RESET}")
        if summary.get("built"):
            parts.append(f"  {_C_BOLD}built{_C_RESET}       {str(summary['built'])[:70]}")
        if summary.get("blocked"):
            parts.append(f"  {_C_BOLD}blocked{_C_RESET}     {str(summary['blocked'])[:70]}")

    parts.append(f"  {_C_BOLD}status{_C_RESET}      {body.get('status') or '—'}")
    return "\n".join(parts)


STAGE_DETAIL = {
    "introbeaver": introbeaver_detail,
    "planbeaver": planbeaver_detail,
    "outrobeaver": outrobeaver_detail,
}


def stage_detail(stage: str, chars_in, parsed) -> str:
    """Render the detail block for a stage, or an empty string."""
    fn = STAGE_DETAIL.get(stage)
    if fn is None or not isinstance(parsed, dict):
        return ""
    try:
        return fn(chars_in, parsed)
    except Exception:
        return ""
