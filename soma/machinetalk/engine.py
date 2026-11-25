"""Pipeline engine: runs the 4-stage machine-talk pipeline.

Run modes
---------
single session : --source <session_dir>  (or --source <proj> --session <id>)
project        : --source <proj_dir>     (all sessions under it)

Stage 1 (INGESTION) -- per ChatSet, parallel (1 worker = 1 ChatSet, workers
never cross sessions):
    distiller -> introbeaver -> planbeaver -> outrobeaver
    distiller SKIP -> skip vault, downstream stages for that ChatSet = SKIPPED

Stage 2-4 (MAPPING / CREATION / FINALIZING) -- run ONCE per build unit,
sequentially. A build unit is the session (single-session mode) or ALL
sessions merged chronologically (project mode). Chronological supremacy
inside nexus means the newest session's intent wins conflicts -- that is how
"N sessions -> 1 blueprint -> 1 codebase" happens.
    nexus -> spark -> falcon -> eagle -> drafter -> scout -> oc -> olc
    oc REWORK -> build BLOCKED (rework_order saved; operator drives --redo)

State: checkpoints/ (resume), artifacts/ (machine-readable chain),
stage1|stage2|... markdown (evidence), codebase/ (final files),
run_manifest.md (proof of every call).

Soma integration (NEW): Every successful stage writes corresponding nodes
and edges to the per-session Soma graph store, preserving
the node types and edge types the Soma schema defines.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .checkpoint import (CheckpointStore, DONE, FAILED, RUNNING, SKIPPED,
                         BLOCKED, PENDING)
from .client import EngineClient as MachineTalkClient
from .gate import is_done, is_hard_block
from .hashes import canonical_json, chatset_hash_for, sha256_hex
from .report import (Manifest, card_session_stage, write_artifact,
                     write_skip_vault)
from .source import ChatSet, Session
from .logger import StageLogger
from . import progress
from . import short_codes
from .engine_sessions import EngineSessions, learn as _learn_engine_session
from . import paths as _paths
from .trajectory import TrajectoryLog, build_record, new_run_id

# ── Stage-1 stages that drive the one-line spinner ───────────────────────
_LIVE_STAGES = ("distiller", "introbeaver", "planbeaver", "outrobeaver")

# Stage 1 handoff: each stage consumes the output of the one before it.
_PREVIOUS_STAGE = {
    "introbeaver": "distiller",
    "planbeaver": "introbeaver",
    "outrobeaver": "planbeaver",
}

# ── Session short codes: resolved once, at selection ─────────────────────
#
# A run has exactly one code per session. It is resolved where the session is
# selected and then handed to the logger, the debug event and the progress
# header, so a run cannot announce ``SES-120`` at the top and write
# ``SES-121`` into a stage sink because a second lookup ran after the store
# changed underneath it.

UNKNOWN_SESSION_CODE = "unknown"


def session_code_line(code: str) -> str:
    """The one line printed for a session before any stage starts.

    ``session SES-120  (2026-11-03 18:40)``

    The wall-clock stamp is the run's start, not the stage's: this line is
    the first thing a run writes, so it is what an operator later greps for
    when a log has to be matched to a run.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M")
    return f"session {code}  ({stamp})"


class SessionCodes:
    """One short code per session, resolved once for the life of a run.

    The selection point hands in the codes it minted (``pinned``); nothing
    downstream may re-derive them. A session missing from the map is resolved
    on first ask and remembered, so a direct ``Engine`` user still works, but
    a run that pins its codes performs exactly one store lookup per session
    and every later surface reads the same value.

    ``resolution_count`` counts the lookups this object performed, which is
    what lets a test prove resolution happened once rather than once per
    consumer. A code that cannot be resolved degrades to ``unknown``, the
    same way the logger and the progress display degrade: display must not
    take a run down.
    """

    def __init__(self, pinned: dict[str, str] | None = None,
                 resolve=None) -> None:
        self._by_session: dict[str, str] = dict(pinned or {})
        self._resolve = resolve or short_codes.code_for
        self._resolutions = 0
        self._error = ""
        self._lock = threading.Lock()

    def resolve(self, session_id: str) -> str:
        """The code for ``session_id``, minting it only if nothing pinned it."""
        sid = str(session_id or "").strip()
        if not sid:
            return UNKNOWN_SESSION_CODE
        with self._lock:
            code = self._by_session.get(sid)
            if code:
                return code
            self._resolutions += 1
            try:
                code = self._resolve(sid)
            except Exception as exc:  # noqa: BLE001 - a code never fails a run
                self._error = f"{type(exc).__name__}: {exc}"
                code = UNKNOWN_SESSION_CODE
            self._by_session[sid] = code
            return code

    def code(self, session_id: str) -> str:
        """The already-resolved code. Never mints, never looks anything up."""
        with self._lock:
            return self._by_session.get(str(session_id or "").strip(),
                                        UNKNOWN_SESSION_CODE)

    @property
    def resolution_count(self) -> int:
        with self._lock:
            return self._resolutions

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    def as_dict(self) -> dict[str, str]:
        with self._lock:
            return dict(self._by_session)


def _model_complaint(gate: GateResult) -> str:
    """The model's own words when it refused or complained, else empty.

    A model that cannot proceed answers with an error object instead of the
    stage output (for example INVALID_INPUT naming the keys it found missing).
    Show exactly what it said, not only the gate's "missing:..." summary.
    """
    parsed = getattr(gate, "parsed", None)
    if not isinstance(parsed, dict) or not parsed.get("error"):
        return ""
    missing = parsed.get("missing_fields")
    return (f"{parsed['error']}"
            + (f" · missing {missing}" if missing else "")
            + (" · halt" if parsed.get("halt") else ""))


def _soma_size(soma) -> tuple[int | None, int | None]:
    """(nodes, edges) now in the Soma, or (None, None) if it cannot be read.

    Counted from the store itself, so "wrote N nodes" is a measured difference
    between two counts, never a figure taken from a model's reply.
    """
    try:
        return int(soma.node_count()), int(soma.edge_count())
    except Exception:
        return None, None



def _verdict_label(gate) -> str:
    """Human label for a gate verdict.

    The distiller emits ``verdict: false`` to mean "do not skip". Printing
    the raw value next to STATUS DONE reads as a failed stage, so the label
    states the meaning instead of the encoding.
    """
    v = (gate.verdict or "").strip()
    if gate.skipped:
        reason = gate.skip_reason or v or "skipped"
        return f"SKIP · {reason}"
    if v.lower() in ("false", "", "0", "no"):
        return "proceed"
    if v.lower() in ("true", "1", "yes"):
        return f"skip · {gate.skip_reason or 'true'}"
    return v


# Everything after a stage, so a failure can stop the chain instead of
# letting later stages invent input.
_DOWNSTREAM = {
    "distiller": ("introbeaver", "planbeaver", "outrobeaver"),
    "introbeaver": ("planbeaver", "outrobeaver"),
    "planbeaver": ("outrobeaver",),
}

# Per-session SOMA databases: <repo>/somas/<session_id>/soma.db
# SOMA_SOMAS_DIR moves the whole folder, so runs of the same session that
# happen at the same time (a benchmark, one lane per model family) each get
# their own database instead of archiving each other's.
SOMAS_DIR = Path(__import__("os").environ.get("SOMA_SOMAS_DIR")
                 or Path(__file__).resolve().parents[2] / "somas")

# ── Dividers ──────────────────────────────────────────────────────────────
_DIVIDER = ("■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□"
            "■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□"
            "■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■■□■□■□■□■□■□■□■")

# -- Soma integration (NEW) ---------------------------------------------
import os as _os
from pathlib import Path as _Path
import json as _json
import threading as _threading
import time as _time_mod
try:
    from ..store import Soma
except ImportError:
    from store import Soma


class AtomicSoma:
    """Soma with atomic persistence (write to .tmp, then rename)."""

    def __init__(self, path: str):
        self._path = _Path(path)
        self._soma = Soma(str(self._path))
        self._lock = _threading.Lock()

    def __getattr__(self, name):
        return getattr(self._soma, name)

    @property
    def path(self) -> _Path:
        """On-disk location of the persisted SOMA database."""
        return self._path

    def _persist(self) -> None:
        """Atomic write: dump to .tmp, then rename."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.suffix.lower() != ".json":
                # A SQLite file is the database itself: it commits as it
                # writes, so there is no snapshot to dump over it.
                self._soma._conn.commit()
                return
            # Handle both old in-memory Soma (_nodes dict) and new SQLite-backed Soma
            if hasattr(self._soma, "_nodes"):
                nodes = {
                    nid: {"type": n.node_type, "properties": n.properties}
                    for nid, n in self._soma._nodes.items()
                }
                edges = [
                    {"source": e.source, "rel_type": e.rel_type,
                     "target": e.target, "id": getattr(e, "id", "")}
                    for e in self._soma._edges
                ]
            else:
                # SQLite-backed Soma — query the database
                rows = self._soma._conn.execute(
                    "SELECT node_id, node_type, properties FROM nodes ORDER BY node_id"
                ).fetchall()
                nodes = {row[0]: {"type": row[1], "properties": json.loads(row[2])} for row in rows}
                edge_rows = self._soma._conn.execute(
                    "SELECT edge_id, source, rel_type, target FROM edges ORDER BY edge_id"
                ).fetchall()
                edges = [{"id": r[0], "source": r[1], "rel_type": r[2], "target": r[3]} for r in edge_rows]
            data = {"nodes": nodes, "edges": edges}
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                _json.dump(data, f, indent=2)
            _os.rename(tmp_path, self._path)


from .soma_bridge import SomaBridge

GROUP_OF = {
    "distiller": "stage1", "introbeaver": "stage1", "planbeaver": "stage1",
    "outrobeaver": "stage1", "nexus": "stage2", "spark": "stage2",
    "falcon": "stage2", "eagle": "stage3", "drafter": "stage3",
    "scout": "stage3", "oc": "stage4", "olc": "stage4",
}


# ---------------------------------------------------------------------------
# Scout text report -> structured (best-effort, Python side)
# ---------------------------------------------------------------------------

def parse_scout_report(raw: str, verdict: str) -> dict:
    violations = []
    blocks = re.split(r"^\[(PASS|FAIL|WARN|SKIP)\]\s+(\S+)[—-]\s*(.*)$",
                      raw, flags=re.M)
    # blocks: [pre, kind, id, title, body, kind, id, title, body, ...]
    for i in range(1, len(blocks) - 3, 4):
        kind, cid, title, body = blocks[i], blocks[i + 1], blocks[i + 2], blocks[i + 3]
        sev_m = re.search(r"Severity:\s*(\w+)", body)
        type_m = re.search(r"Type:\s*(\w+)", body)
        msg_m = re.search(r"Message:\s*(.+)", body)
        violations.append({
            "contract_id": cid.strip(),
            "status": kind.lower(),
            "severity": (sev_m.group(1).lower() if sev_m else
                         ("high" if kind == "FAIL" else "info")),
            "type": (type_m.group(1).lower() if type_m else ""),
            "message": (msg_m.group(1).strip() if msg_m else title.strip()),
        })
    criticals = [v for v in violations
                 if v["severity"] == "critical" and v["status"] == "fail"]
    fails = [v for v in violations if v["status"] == "fail"]
    total = len(violations)
    health = 0.0 if (criticals or verdict == "REWORK") else (
        0.9 if not fails else max(0.5, 1 - 0.15 * len(fails)))
    return {
        "analysis_id": f"SCOUT-{verdict}",
        "scout_verdict": verdict,
        "violations": violations,
        "blockers": [v["contract_id"] for v in criticals],
        "aggregate_health": round(health, 2),
    }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, *, out_dir: Path, client: MachineTalkClient,
                 workers: int = 4, quiet: bool = False,
                 force: bool = False,
                 gateway_map: dict | None = None,
                 debug_dir: str | None = None,
                 max_chatsets: int | None = None,
                 step_mode: bool = False,
                 soma_path: Path | None = None,
                 session_codes: dict[str, str] | None = None,
                 announced: bool = False) -> None:
        self.out = Path(out_dir)
        self.client = client
        self.workers = max(1, workers)
        self.force = force
        self.max_chatsets = max_chatsets
        self.step_mode = step_mode
        self.granular = False
        self.gateway_map = gateway_map
        self.ck = CheckpointStore(self.out, layout="chunk")
        # Every stage call is kept whole, one line each, under the run's id.
        self.run_id = ""
        self.phase = ""
        self.traj = TrajectoryLog(self.out / "trajectories.jsonl")
        self.manifest = Manifest(self.out)
        self.art_dir = self.out / "artifacts"
        # The run's session codes. The selection point may pin them; a direct
        # Engine user resolves lazily on first ask. Either way there is one
        # holder, so the logger, the debug event and the progress header all
        # read a value that was decided once.
        self.session_codes = SessionCodes(pinned=session_codes)
        # The id the ENGINE generated for each chat session in this run. The
        # local code above is for display and file names only; it is never sent
        # to the engine. A session's first call sends no id, the engine
        # generates one, and every later call for that session sends it.
        self._engine_sessions = EngineSessions()
        self._announced: set[str] = set()
        self._announce_lock = threading.Lock()
        if announced:
            # The caller printed the lines already (run_engine.py does, so the
            # code is on screen before anything else can write).
            self.mark_announced(self.session_codes.as_dict())
        self.log = StageLogger(cwd=Path.cwd(), out_dir=self.out, quiet=quiet,
                               session_codes=self.session_codes)
        # The display builds a file:// link to the sink the logger writes, so
        # it has to be told the same directory the logger was given. Without
        # this the link fell back to <cwd>/pipeline_out and pointed at a file
        # that was never written when --out was left at its default.
        progress.set_output_dir(self.out)
        self._print = (lambda *a: None) if quiet else progress.emit
        self._debug_dir = Path(debug_dir) if debug_dir else None
        if self._debug_dir:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
        # One identifier for this pipeline run. Every PEA file this run
        # PEA directories are named by the engine (start time + session id).
        # Nothing here needs to track a run id, so none is kept.
        self._manifest_lock = threading.Lock()

        # Soma graph store (per-session by default)
        soma_file = Path(soma_path) if soma_path else self.out / "soma.json"
        self._soma_file = soma_file
        self._soma_bytes_before = (
            soma_file.stat().st_size if soma_file.exists() else None)
        self._soma = AtomicSoma(str(soma_file))
        self._bridge = SomaBridge(self._soma)
        self._soma_lock = threading.Lock()
        self._print_lock = threading.Lock()

    # -- logging helpers (NEW) ---------------------------------------------

    def mark_announced(self, session_ids) -> None:
        """Record that these sessions' codes were printed by the caller.

        ``run_engine.py`` prints the line at selection, before the Engine
        exists. Telling the Engine what it printed keeps stage 1 from printing
        the same line a second time: one announcement per session per run.
        """
        with self._announce_lock:
            for sid in session_ids:
                self._announced.add(str(sid or "").strip())

    def announce_session(self, session_id: str) -> str:
        """Resolve this session's code and print it before any stage runs.

        Returns the code so the caller can hand the same value to the header
        that follows. Every surface afterwards reads ``session_codes``, so a
        single resolution is what the whole run shows.

        A session is announced once per run. Stage 1 asks again before it
        prints its header, and a line repeated with the same code reads as two
        runs.
        """
        code = self.session_codes.resolve(session_id)
        key = str(session_id or "").strip()
        with self._announce_lock:
            if key in self._announced:
                return code
            self._announced.add(key)
        self._print(session_code_line(code))
        return code

    def _log_summary(self, stage: str, session: str, counts: dict[str, int],
                    chatset: int | None = None) -> None:
        """Print per-task summary from SomaBridge return dict."""
        if not counts:
            return
        parts = [f"{k}={v}" for k, v in counts.items() if v > 0]
        if not parts:
            return
        # Suppress output for Stage 1 — we print clean tables, not interleaved logs
        if stage in ("distiller", "introbeaver", "planbeaver", "outrobeaver"):
            return
        detail = "  → " + ", ".join(parts)
        self._print(detail)
        self.log.info(stage, f"session={session}" +
                      (f" chatset={chatset}" if chatset else "") +
                      f" | {', '.join(parts)}")

    def _with_spinner(self, label: str, fn, *args,
                      live_stage: str = "", **kwargs):
        """Run ``fn`` behind the one-line stage progress printer.

        Only the four Stage-1 beaver stages own the live line. Other stages
        keep printing normally so their timeout warnings stay visible.
        """
        if live_stage in _LIVE_STAGES:
            return progress.spin(label, fn, *args, **kwargs)
        return fn(*args, **kwargs)

    def _timeout_watcher(self, label: str, timeout: float,
                          stop: threading.Event) -> None:
        """Background thread that prints warnings at 50/75/90% of timeout."""
        thresholds = [
            (0.50, "[⚠ SLOW]"),
            (0.75, "[⚠ SLOW]"),
            (0.90, "[⚠ TIMEOUT IMMINENT]"),
        ]
        for pct, prefix in thresholds:
            wait = timeout * pct
            if stop.wait(timeout=wait):
                return
            if not stop.is_set():
                elapsed = time.monotonic() - self._watch_start
                remaining = timeout - elapsed
                self._print(f"  {prefix} {label} elapsed={elapsed:.0f}s/{timeout:.0f}s "
                            f"remaining={remaining:.0f}s")

    def _print_dashboard(self, sessions: list[Session]) -> None:
        """Print compact status board for all sessions and their stages."""
        from .checkpoint import STAGE1, STAGE234
        # Symbols
        SYM = {"DONE": "✓", "SKIPPED": "≋", "RUNNING": "→RUN", "BLOCKED": "⊘",
               "FAILED": "✗", "PENDING": "-"}
        lines = []
        lines.append("  " + "═" * 70)
        # Header
        all_stages = STAGE1 + STAGE234
        hdr = "  SESSION        " + "".join(f"{s[:6]:<7}" for s in all_stages)
        lines.append(hdr)
        lines.append("  " + "─" * 70)
        # Per session rows
        for s in sessions:
            sid = s.session_id
            data = self.ck.load(sid)
            cs_rows = data.get("chatsets", {})
            row = f"  {sid:<14} "
            # Stage 1: aggregate across chatsets
            for stg in STAGE1:
                statuses = [cs["stages"].get(stg, {}).get("status", "PENDING")
                            for cs in cs_rows.values()]
                if any(st == "RUNNING" for st in statuses):
                    sym = SYM["RUNNING"]
                elif any(st == "FAILED" for st in statuses):
                    sym = SYM["FAILED"]
                elif any(st == "BLOCKED" for st in statuses):
                    sym = SYM["BLOCKED"]
                elif all(st in ("DONE", "SKIPPED") for st in statuses):
                    sym = SYM["DONE"]
                elif any(st == "SKIPPED" for st in statuses):
                    sym = SYM["SKIPPED"]
                else:
                    sym = SYM["PENDING"]
                row += f"{sym:<7}"
            # Stage 2-4: per session
            for stg in STAGE234:
                st = data.get("session_stages", {}).get(stg, {}).get("status", "PENDING")
                row += f"{SYM.get(st, '?'):<7}"
            lines.append(row)
        lines.append("  " + "═" * 70)
        for line in lines:
            self._print(line)

    # -- artifact helpers ----------------------------------------------------

    # -- stage handoff: Soma nodes -------------------------------------------
    #
    # Stages hand off through the graph, not through JSON files on disk. A
    # file handoff left the pipeline's real state outside the store, so the
    # only record of what introbeaver actually consumed was a directory that
    # could be overwritten, deleted, or read by the next run without any
    # provenance link to the node that produced it.
    #
    # Every stage writes one ``StageOutput`` node carrying its parsed output,
    # plus an edge from the node it consumed. Reading the upstream output is
    # then a graph query with the same guarantees as everything else in Soma.

    def _stage_node_id(self, build: str, stage: str) -> str:
        return f"stage:{build}:{stage}"

    def _save_artifact(self, build: str, stage: str, obj) -> str:
        """Persist one stage's output as a Soma node; return its node id."""
        node_id = self._stage_node_id(build, stage)
        output = obj if isinstance(obj, (dict, list)) else {"value": str(obj)}
        props = {
            "id": node_id,
            "build": build,
            "stage": stage,
            "output": output,
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with self._soma_lock:
            self._soma.create_node("StageOutput", props)
            # Link the chain so the newest node is traceable to its input.
            # create_edge always inserts, so a re-run would stack a second
            # identical edge; the graph should show one handoff per pair.
            previous = _PREVIOUS_STAGE.get(stage)
            if previous:
                src = self._stage_node_id(build, previous)
                if self._soma.get_node(src) is not None:
                    existing = self._soma.get_edges(src)
                    if not any(e.rel_type == "HANDOFF_TO"
                               and e.target == node_id for e in existing):
                        self._soma.create_edge(src, "HANDOFF_TO", node_id)
            self._soma._persist()
        return node_id

    def _load_artifact(self, build: str, stage: str):
        """Read one stage's output node, or None when it has not run."""
        node = self._soma.get_node(self._stage_node_id(build, stage))
        if node is None:
            return None
        out = node.properties.get("output")
        return out if isinstance(out, (dict, list)) else None

    # -- stage 1: one chatset, all 4 beaver stages in order ------------------

    # The only node types a Soma may hold when the extraction loop begins.
    _SEED_NODE_TYPES = frozenset({"SessionAnchor", "ChatSet", "Message"})

    def _assert_blind_start(self) -> None:
        """Halt unless the Soma holds just the seeded skeleton.

        The extraction loop is what fills the graph. A Soma that already
        carries anything a stage derives (a distilled set, a stage output, a
        demo row) would let a stage read data it did not produce, so the loop
        refuses to start on it.
        """
        with self._soma_lock:
            found = {n[0] for n in self._soma._conn.execute(
                "SELECT DISTINCT node_type FROM nodes").fetchall()}
        extra = sorted(found - self._SEED_NODE_TYPES)
        if extra:
            raise RuntimeError(
                "blind start violated: the Soma already holds " + ", ".join(extra)
                + ". Only SessionAnchor, ChatSet and Message nodes may exist "
                "before extraction. Start from a fresh Soma.")

    def _seed_session(self, session: Session) -> dict:
        """Seed the whole session into the Soma and take the hashes it assigned.

        Every chat set in the session is written, with all its turns, and its
        hash is assigned and stored on its ChatSet node. The in-memory chat
        sets then carry the Soma-assigned hash, so there is one hash per chat
        set and it is the one in the Soma.
        """
        with self._soma_lock:
            res = self._bridge.seed_session(
                session.session_id, session.chatsets, project_name="Dendritic")
            self._soma._persist()
        for cs in session.chatsets:
            cs.chatset_hash = res["hashes"][cs.chatset_id]
        return res

    def _ensure_seeded(self, cs: ChatSet) -> None:
        """Seed this chat set if the Soma does not hold it yet (caller holds the lock)."""
        sid = cs.session_id
        if self._soma.get_node(f"anchor-{sid}") is None:
            totals = getattr(self, "_session_totals", {}).get(sid, (1, cs.chars))
            self._bridge.write_session_anchor(
                sid, project_name="Dendritic",
                total_sets=totals[0], total_chars=totals[1])
        cs.chatset_hash = self._bridge.seed_chatset(sid, cs)["hash"]

    def _chat_set_from_soma(self, cs: ChatSet) -> list[dict]:
        """The chat set as the Soma holds it, checked against its hash.

        The chat set is written into the Soma as Message nodes when the
        session starts. Stages are given that copy, not the in-memory one
        loaded from the file. If what the Soma holds does not hash to the
        chat set's stored hash, it was not written or was changed since, and
        sending it to a model would be sending text the hash does not cover.
        """
        node = self._soma.get_node(cs.chatset_id)
        want = (node.properties.get("chatset_hash") if node else "") or ""
        if not want:
            raise RuntimeError(
                f"MISSING_SOMA_DATA: chat set {cs.chatset_id} is not seeded in the "
                "Soma (no hash was assigned). The session has to be seeded "
                "before a stage reads it.")
        turns = self._bridge.read_chat_set(cs.chatset_id)
        got = chatset_hash_for(turns)
        if got != want:
            raise RuntimeError(
                f"MISSING_SOMA_DATA: chat set {cs.chatset_id} in the Soma does not match its hash "
                f"(read {len(turns)} turns, hash {got[:12]}... != assigned at seed "
                f"{want[:12]}...). The turns were changed after the session was seeded.")
        return turns

    def _log_trajectory(self, *, stage: str, session_id: str, chatset_id: str,
                        rec, gate, chatset_hash: str = "") -> None:
        """Keep this stage call's whole conversation. Never fails the stage."""
        try:
            self.traj.append(build_record(
                run_id=self.run_id, phase=self.phase, stage=stage,
                session_id=session_id, chatset_id=chatset_id, rec=rec, gate=gate,
                is_done=is_done(gate), chatset_hash=chatset_hash,
                instruction=self._soma.get_stage_instruction(stage),
                model_requested=self.client.model_for(stage),
                full=_os.environ.get("SOMA_TRAJECTORY_FULL") == "1"))
        except Exception as exc:        # logging must never be the reason a stage fails
            self._print(f"  [trajectory] not written: {type(exc).__name__}: {exc}")

    def _report_halt(self, stage: str, build: str, reason: str) -> None:
        """Tell the engine's log that a stage failed without ever calling a model."""
        report = getattr(getattr(self, "client", None), "_report", None)
        if report is None:
            return
        try:
            with self._engine_sessions.scope(build) as engine_code:
                ids = report("start", stage=stage, session=engine_code, chatset_id=build,
                             run_id=getattr(self, "run_id", "")) or {}
                _learn_engine_session(self._engine_sessions, build, engine_code,
                                      ids.get("session"), self._print)
            if ids.get("session") and ids.get("stage_id"):
                report("end", session=ids["session"], stage_id=ids["stage_id"], stage=stage,
                       status="FAIL", errors=[reason[:300]], turns=0, rejections=0,
                       writes=0, seconds=0)
        except Exception:
            pass

    def _log_trajectory_error(self, *, stage: str, session_id: str,
                              chatset_id: str, detail: str) -> None:
        """A stage that raised before it produced a record still leaves a line."""
        try:
            self.traj.append({"schema": "trajectory/1", "run_id": self.run_id,
                              "phase": self.phase, "stage": stage,
                              "session_id": session_id, "chatset_id": chatset_id,
                              "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "status": "error", "errors": [detail]})
        except Exception:
            pass

    def _run_chatset_stage(self, cs: ChatSet, stage: str,
                           worker: int) -> bool:
        """Run one stage for one chatset; checkpoint + artifact + card.

        Returns True when the stage is DONE (or cleanly SKIPPED).
        """
        sid, n = cs.session_id, cs.set_number
        # Resolved at selection, pinned for the run. This is a read from the
        # run's own map, not a second lookup: the logger, the debug event and
        # the progress header below all take this same value.
        ses_code = self.session_codes.resolve(sid)
        cur = self.ck.cs_stage(sid, n, stage)
        if cur["status"] == DONE:
            if self.force:
                self._print(f"  [W{worker}] {n}:{stage} forced re-run (was DONE)")
            else:
                self._print(f"  … [W{worker}] {n}:{stage} already DONE (resume skip)")
                self.log.info(stage, f"{sid} CS{n} already DONE (resume)")
                return True
        if cur["status"] == SKIPPED:
            if not self.force:
                return True

        self.ck.cs_mark(sid, n, stage, RUNNING)
        # Resolve model and provider before the call so the telemetry block
        # can name what is about to run, not only what answered.
        route = self.client.route_for(stage)
        model = route.get("model_id", "unknown")
        provider = route.get("provider", "unknown")
        self.log.start(stage, session=sid, chatset=n, model=model,
                       detail=f"chars={cs.chars} hash={cs.chatset_hash[:16]}…")

        # Telemetry opens before the call: a silent wait is indistinguishable
        # from a hang, and an operator watching a two-minute stage needs the
        # start time and the model id on screen.
        # The size reported is the chat set the stage reads from the Soma.
        chars_in = cs.chars

        self._print(progress.telemetry_header(
            stage, sid, cs.source_chunk_id or n, chars_in,
            model, provider, session_code=ses_code))

        t0 = time.monotonic()
        spinner_label = f"{stage} {sid} CS{cs.source_chunk_id or n} {model}"

        # One line per stage, owned entirely by the timer: it starts the
        # clock, shows the live state, and closes with the real outcome.
        # The old spinner is deliberately not started here — two owners drew
        # two lines per stage and the spinner froze on "✓" even for failures.
        timer = progress.StageTimer(stage, sid, cs.source_chunk_id or n,
                                    cs.chars, model=model,
                                    model_fn=lambda: self.client.model_for(stage),
                                    out_dir=self.out,
                                    session_code=ses_code)
        timer.start()
        self._stage_timer = timer

        # Real status, printed as each step happens (see the comment above
        # progress._DISTILLER_STEPS). ``call_stage`` calls this at the moment it
        # reads the instructions, reads the input, sends the request, gets the
        # reply, runs the gate, checks the anchors and measures; every value
        # is read from that step, nothing is estimated or batched.
        def _emit(label: str, text: str) -> None:
            self._print(progress.stage_event(stage, sid, label, text,
                                             session_code=ses_code))

        try:
            # None on this session's first call: the engine generates the
            # id and the reply names it. After that, send it every time.
            with self._engine_sessions.scope(sid) as engine_code:
                res = self.client.call_stage(
                    stage, session_id=sid, chatset_id=cs.chatset_id,
                    expected_hash=cs.chatset_hash if stage in
                    ("distiller", "introbeaver") else None,
                    soma=self._soma, soma_lock=self._soma_lock,
                    raw_chat_set=self._chat_set_from_soma(cs),
                    session_code=engine_code,
                    emit=_emit,
                )
                _learn_engine_session(
                    self._engine_sessions, sid, engine_code,
                    res.record.engine_session,
                    lambda text: _emit("session", text))
        except BaseException:
            timer.finish("CRASHED")
            raise
        rec, gate = res.record, res.record.gate or GateResult(skipped=False, verdict="UNKNOWN", reasons=["Stage exhausted"], errors=[], parsed=None)

        # The timer closes here, after the call returns and before any output
        # is written, so the reported duration is the model call itself.
        if is_done(gate):
            _stage_status = "SKIPPED" if gate.skipped else "DONE"
        elif is_hard_block(gate):
            _stage_status = f"BLOCKED {gate.verdict or ''}".strip()
        else:
            _stage_status = f"FAILED {gate.first_reason() or ''}".strip()
        timer.finish(_stage_status)

        # Close the telemetry block with measured values. chars_out is the raw
        # response length, which is what actually left the provider; the gate
        # may reject some of it, and that is what `verdict` and `detail` say.
        chars_out = len(rec.raw_response or "")
        self._print(progress.telemetry_result(
            stage,
            elapsed=(time.monotonic() - t0),
            chars_in=chars_in,
            chars_out=chars_out,
            prompt_tokens=rec.prompt_tokens,
            completion_tokens=rec.completion_tokens,
            total_tokens=rec.total_tokens,
            model=rec.model or model,
            provider=provider,
            verdict=_verdict_label(gate),
            status=_stage_status,
            detail=(gate.first_reason() if not is_done(gate) else ""),
            session=sid,
            out_dir=self.out,
            engine_session=rec.engine_session,
            engine_stage=rec.engine_stage,
            log_link=rec.log_link,
            complaint=_model_complaint(gate),
        ))

        latency = (time.monotonic() - t0) * 1000
        with self._manifest_lock:
            self.manifest.add(rec, gate)
        self._log_trajectory(stage=stage, session_id=sid, chatset_id=cs.chatset_id,
                             rec=rec, gate=gate, chatset_hash=cs.chatset_hash)
        p = write_artifact(self.out, sid, stage, n, rec, gate,
                           group=GROUP_OF[stage])
        
        # ── Debug event writing ─────────────────────────
        if self._debug_dir:
            try:
                from .debug_dashboard_skeu import write_event
                import traceback

                status = rec.status
                status_map = {"pass": "done", "fail": "failed", "blocked": "failed", "": "running"}

                # Convert non-serializable values to string (safe fallback)
                def safe(val):
                    try:
                        json.dumps(val)
                        return val
                    except (TypeError, ValueError):
                        return str(val)[:500]

                ev = {
                    "stage": stage,
                    "status": status_map.get(status, status),
                    "session_id": sid,
                    "session_code": ses_code,
                    "stage_code": short_codes.stage_code(stage, n),
                    "engine_session": rec.engine_session,
                    "engine_stage": rec.engine_stage,
                    "log_link": rec.log_link,
                    "chatset_number": n,
                    "chatset_id": cs.chatset_id,
                    "chars_in": cs.chars,
                    "model": rec.model or "unknown",
                    "tokens": rec.total_tokens,
                    "prompt_tokens": rec.prompt_tokens,
                    "completion_tokens": rec.completion_tokens,
                    "cache_hit_tokens": rec.cache_hit_tokens,
                    "cache_miss_tokens": rec.cache_miss_tokens,
                    "latency_ms": round(rec.latency_ms, 1),
                    "attempts": safe(rec.attempts if rec.attempts else [{"attempt": 1, "model": rec.model or "unknown"}]),
                    "errors": rec.error or (gate.errors if gate.errors else gate.reasons) if not is_done(gate) else "",
                    "verdict": gate.verdict or "",
                    "raw_response": rec.raw_response or "",
                    "sys_prompt": rec.sys_prompt or "",
                    "user_msg": rec.user_msg or "",
                    "output": safe(gate.parsed) if gate.parsed else {},
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                write_event(self._debug_dir, ev)
            except Exception as e:
                traceback.print_exc()
                # Route through _print so this never writes to stdout while a
                # stage spinner owns the live line (progress.emit defers it).
                self._print(f"WARNING: debug event write failed: {e}")
        # The engine's own ids for this call, so a local log line can be
        # matched to the engine's record. "engine=none" says plainly that the
        # engine returned no session, stage or link.
        ref = (f" engine={rec.engine_session}/{rec.engine_stage} {rec.log_link}"
               if (rec.engine_session or rec.engine_stage or rec.log_link)
               else " engine=none")
        if is_done(gate):
            _parsed = gate.parsed if isinstance(gate.parsed, dict) else {}
            _dout = _parsed.get("distiller_output") if isinstance(
                _parsed.get("distiller_output"), dict) else _parsed
            self.ck.cs_mark(sid, n, stage, DONE,
                            verdict=gate.verdict or None,
                            model=rec.model, tokens=rec.total_tokens,
                            chars_in=cs.chars,
                            skip_reason=(_dout or {}).get("skip_reason")
                            if stage == "distiller" else None,
                            signal_quality=(_dout or {}).get("signal_quality")
                            if stage == "distiller" else None,
                            latency_ms=round(rec.latency_ms, 1),
                            artifact=str(p.relative_to(self.out)),
                            raw_response=rec.raw_response,
                            sys_prompt=rec.sys_prompt,
                            user_msg=rec.user_msg,
                            errors=rec.error or (gate.errors if gate.errors else gate.reasons) if not is_done(gate) else "",)
            if gate.skipped and stage == "distiller":
                write_skip_vault(self.out, sid, n, gate, rec,
                                 transcript_head=cs.transcript())
                self.ck.cs_downstream_skipped(sid, n, "distiller")
            if gate.skipped:
                self.log.skip(stage, session=sid, chatset=n,
                              detail=f"chars_in={cs.chars} ms={latency:.0f} model={rec.model}{ref}")

            else:
                verdict_label = ("" if gate.verdict.lower() in ("false", "", "0", "no")
                                 else f" verdict={gate.verdict}")
                self.log.done(stage, session=sid, chatset=n,
                               detail=f"chars_in={cs.chars} ms={latency:.0f} model={rec.model}{verdict_label}{ref}")

            if gate.parsed is not None:
                self._save_artifact(cs.chatset_id, stage, gate.parsed)
            # ---- Soma graph writes: the graph nodes built from the stage output ----
            _n0, _e0 = _soma_size(self._soma)
            with self._soma_lock:
                if stage == "distiller":
                    self._bridge.write_receptor(
                        sid, cs.chatset_id, n, gate, rec.model, latency)
                    dcounts = self._bridge.write_distilled(
                        sid, cs.chatset_id, n, gate, rec.model, latency)
                    self._log_summary(stage, sid, dcounts, chatset=n)
                elif stage in ("introbeaver", "planbeaver", "outrobeaver"):
                    try:
                        counts = self._bridge.write_intro_plan_outrobeaver(
                            sid, cs.chatset_id, stage, gate, rec.model, latency)
                        self._log_summary(stage, sid, counts, chatset=n)
                    except Exception as exc:
                        # A graph write failure must not be reported as a
                        # successful stage. Surface it and fail the chatset.
                        self._print(f"  [{stage.upper()}] GRAPH WRITE FAILED: "
                                    f"{type(exc).__name__}: {exc}")
                        raise
                self._soma._persist()
            # What this stage just wrote to the Soma, counted from the store
            # before and after (not from the model's reply).
            _n1, _e1 = _soma_size(self._soma)
            if _n0 is not None and _n1 is not None:
                _emit("soma", f"wrote {_n1 - _n0} node(s), {_e1 - _e0} edge(s) "
                              f"· graph now {_n1} nodes, {_e1} edges")
            if stage == "distiller":
                self._print(progress.distiller_summary(
                    self._soma.path, cs.chars, gate.parsed))
            else:
                detail = progress.stage_detail(stage, cs.chars, gate.parsed)
                if detail:
                    self._print(detail)
            return True
        if is_hard_block(gate):
            self.ck.cs_mark(sid, n, stage, BLOCKED,
                            verdict=gate.verdict, model=rec.model,
                            artifact=str(p.relative_to(self.out)))
            self.log.block(stage, session=sid, chatset=n,
                           detail=f"verdict={gate.verdict} models={len(rec.attempts)}{ref}")

        else:
            self.ck.cs_mark(sid, n, stage, FAILED, model=rec.model,
                            errors=gate.reasons[:300],
                            artifact=str(p.relative_to(self.out)))
            self.log.fail(stage, session=sid, chatset=n,
                          detail=f"models={len(rec.attempts)} errors={gate.reasons[:80]}{ref}")

        return False

    def run_chatset(self, cs: ChatSet, worker: int = 0) -> dict:
        """All of stage 1 for one chatset, in order. Returns per-stage dict."""
        sid, n = cs.session_id, cs.set_number
        self.ck.ensure_chatset(sid, n, cs.chatset_id, cs.chatset_hash, cs.chars)

        # Print current chatset character count before each stage
        with self._print_lock:
            self._print(
                f"  [{n}] {cs.chars:,} character(s) in this chatset")

        # ---- Soma: the session was seeded up front (run_stage1). If this
        # chat set is not in the Soma, seed it now; either way, clear what an
        # earlier run derived from it so this run starts clean. ----
        with self._soma_lock:
            self._ensure_seeded(cs)
            self._bridge.clear_chatset_results(cs.chatset_id)
            self._soma._persist()

        result: dict[str, str] = {}

        # Each beaver reads the raw ChatSet directly. No cross-dependencies.
        _all_stages = ("distiller", "introbeaver", "planbeaver", "outrobeaver")
        # ``--stage`` narrows the run to one stage. Without it, all four run.
        # The old default was "distiller" alone, which silently skipped every
        # downstream beaver while the tables reported them as NOT RUN.
        _run_stages = (getattr(self, "_stage", None) or _all_stages)
        _run_stages = (_run_stages,) if isinstance(_run_stages, str) else _run_stages

        if self.granular:
            self._print("\n  [Step: write SessionAnchor+ChatSet to Soma]")
            self._print("  Enter to run, Ctrl+C to skip")
            try:
                input()
            except EOFError:
                pass

        for stage in _all_stages:
            if stage not in _run_stages:
                continue
            if self.granular:
                self._print(f"\n  [Step: {stage.upper()}]")
                self._print("  Enter to run, Ctrl+C to skip")
                try:
                    answer = input().strip().lower()
                    if answer == "n" or answer == "no":
                        self._print(f"  Skipping {stage}.")
                        result[stage] = SKIPPED
                        continue
                except EOFError:
                    pass
                pass  # no input, will proceed
            # A beaver is handed nothing. It reads its instructions, the chat
            # set and whatever earlier stages wrote from the Soma by tool call,
            # and writes its output there.
            try:
                ok = self._run_chatset_stage(cs, stage, worker)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                self._log_trajectory_error(stage=stage, session_id=sid,
                                           chatset_id=cs.chatset_id, detail=detail)
                self.ck.cs_mark(sid, n, stage, FAILED,
                                model=None, chars_in=cs.chars, errors=detail)
                self.log.fail(stage, session=sid, chatset=n, detail=detail)
                # A provider error carries its own explanation and the fix;
                # cutting it to 120 characters hides exactly that.
                if "PROVIDER MISSING" in detail or "PROVIDER MISMATCH" in detail:
                    shown = "\n      " + detail.replace("\n", "\n      ")
                else:
                    shown = detail[:120]
                self._print(f"  -[{stage.upper()}]- [FAILED:TRANSPORT] "
                            f"{type(exc).__name__} chars_in={cs.chars:,}  "
                            f"{shown}")
                result[stage] = FAILED
                ok = False
            else:
                result[stage] = self.ck.cs_stage(sid, n, stage)["status"]
            # ── skip policy ────────────────────────────────────────
            # A distiller SKIP means the stage decided this turn carries no
            # signal. Everything downstream extracts from what the distiller
            # produced, so there is nothing to extract. Halt the chain here.
            #
            # A skip is a successful outcome, so it returns ok=True and a
            # guard written as "not ok" never fires. Under --force the
            # checkpoint's SKIPPED marker is also ignored, which let
            # introbeaver run on an empty upstream and report DONE.
            #
            # The gate object belongs to the stage runner and is not in scope
            # here; the checkpoint carries the same decision.
            if stage == "distiller":
                _d = self.ck.cs_stage(sid, n, "distiller")
                # A distiller skip is recorded as status DONE with skip_reason
                # set, because stopping extraction is a successful decision,
                # not a failure. Checking only for status SKIPPED therefore
                # never matches a real skip; the skip_reason is the marker.
                _skipped = (_d.get("status") == SKIPPED
                            or (_d.get("status") == DONE
                                and bool(_d.get("skip_reason"))))
                if _skipped:
                    reason = _d.get("skip_reason") or "SKIPPED"
                    for s in _DOWNSTREAM.get("distiller", ()):
                        if s in _all_stages and s in _run_stages:
                            self.ck.cs_mark(sid, n, s, SKIPPED,
                                            errors=f"upstream distiller "
                                                   f"skipped ({reason})")
                            result[s] = SKIPPED
                            self._print(
                                f"  ⊘ {s.upper()} skipped: distiller skipped "
                                f"({reason})")
                    return result

            # Stage 1 is a chain: a stage with no upstream has nothing to
            # work on. A failed distiller used to let introbeaver run on the
            # raw conversation anyway and report DONE, which reads as a healthy
            # chatset that had no signal.
            if not ok:
                downstream = _DOWNSTREAM.get(stage, ())
                for s in downstream:
                    if s in _all_stages and s in _run_stages:
                        self.ck.cs_mark(sid, n, s, SKIPPED,
                                        errors=f"upstream {stage} did not "
                                               f"complete")
                        result[s] = SKIPPED
                        self._print(
                            f"  ⊘ {s.upper()} skipped: upstream {stage} "
                            f"did not complete")
                return result

        return result

    # -- stage 1 across chatsets (parallel, workers never cross sessions) ----

    def run_stage1(self, sessions: list[Session],
                   only: tuple[str, int] | list[tuple[str, int]] | None = None) -> bool:
        """Run stage1 for all chatsets, or only the selected (sid, set) pairs."""
        # Determine which stages to run: stage="distiller" means only distiller
        stages_to_run = ("distiller", "introbeaver", "planbeaver", "outrobeaver")
        selected = (getattr(self, "_stage", None)
                    or getattr(self, "_selected_stage", None))
        if selected:
            stages_to_run = (selected,)

        # Normalise ``only`` to a set of (session_id, set_number) tuples.
        wanted: set[tuple[str, int]] | None = None
        if only:
            if (isinstance(only, tuple) and len(only) == 2
                    and isinstance(only[0], str)):
                wanted = {only}
            else:
                wanted = set(only)

        jobs = []
        for s in sessions:
            for cs in s.chatsets:
                if wanted is not None and (cs.session_id, cs.set_number) not in wanted:
                    continue
                if not self.force:
                    last = stages_to_run[-1]
                    if self.ck.cs_stage(cs.session_id, cs.set_number,
                                        last)["status"] in (DONE, SKIPPED):
                        continue  # fully done, resume
                jobs.append(cs)

        # Accurate per-session totals for the SessionAnchor (not per chatset).
        self._session_totals = {
            s.session_id: (len(s.chatsets), sum(c.chars for c in s.chatsets))
            for s in sessions
        }

        # Apply max_chatsets limit
        if self.max_chatsets is not None and self.max_chatsets > 0 and self.max_chatsets < len(jobs):
            jobs = jobs[:self.max_chatsets]
        if not jobs:
            if self.force:
                self._print("  Nothing to do.")
            else:
                self._print("  Stage 1: nothing to do (all DONE or SKIPPED).")
            return True

        # Group jobs by session for header output
        session_jobs: dict[str, list[ChatSet]] = {}
        for cs in jobs:
            session_jobs.setdefault(cs.session_id, []).append(cs)

        # Seed every session that has work into the Soma, whole, before any
        # stage runs. The hash of each chat set is assigned here, and stages
        # read the chat set from the Soma from this point on.
        by_id = {s.session_id: s for s in sessions}
        # Blind start: before the first stage, the Soma holds only the seeded
        # skeleton. Checked on every session's seed, before any stage runs.
        for sid in sorted(session_jobs):
            res = self._seed_session(by_id[sid])
            if getattr(self, "_blind_start", False):
                self._assert_blind_start()
            after = (self._soma_file.stat().st_size
                     if self._soma_file.exists() else None)
            if self._soma_bytes_before is None:
                state = "NEW (file did not exist, created this run)"
            else:
                state = f"REUSED (existing file, {self._soma_bytes_before:,} bytes before run)"
            ok = "written" if after is not None else "NOT WRITTEN, file missing after seed"
            counts = {}
            for tbl in ("stage_instructions", "nodes", "edges"):
                try:
                    counts[tbl] = self._soma._conn.execute(
                        f"SELECT count(*) FROM {tbl}").fetchone()[0]
                except Exception as exc:
                    counts[tbl] = f"ERROR {exc}"
            self._print(
                f"  Soma DB: {state}\n"
                f"           path {self._soma_file}\n"
                f"           rows: stage_instructions={counts['stage_instructions']} "
                f"nodes={counts['nodes']} edges={counts['edges']}\n"
                f"           now {after if after is None else format(after, ',')} bytes, {ok}; "
                f"selected {len(by_id[sid].chatsets)} chat set(s) / "
                f"{sum(c.chars for c in by_id[sid].chatsets):,} chars, "
                f"to run {len(session_jobs[sid])} / "
                f"{sum(c.chars for c in session_jobs[sid]):,} chars, "
                f"{len(by_id[sid].chatsets) - len(session_jobs[sid])} already DONE/SKIPPED")
            self._print(
                f"  seeded session {sid} into the Soma: {len(by_id[sid].chatsets)} "
                f"chat set(s), {res['Message']} turn(s) written, "
                f"{res['already_seeded']} already seeded, hashes assigned")

        # Print total session character count before the first chatset
        total_chars = sum(cs.chars for cs in jobs)
        self._print(
            f"  Total session: {total_chars:,} character(s) across "
            f"{len(jobs)} chatset(s)")

        # Print session headers. The code is the run's resolved value, and the
        # one-line announcement above it is what an operator writes down when
        # the run starts.
        for sid in sorted(session_jobs.keys()):
            s_jobs = session_jobs[sid]
            cs0 = s_jobs[0]
            total_sets = cs0.total_sets or len(s_jobs)
            ses_code = self.announce_session(sid)
            self._print(progress.session_header(sid, total_sets,
                                                session_code=ses_code))

        progress.stage_clock_reset()
        label = "INGESTION (FORCE)" if self.force else "INGESTION"
        # Print session name, total chatsets, current index before first stage
        session_id = session_jobs.get(
            next(iter(session_jobs)), [None])[0].session_id if session_jobs else "unknown"
        total_sets = len(jobs)
        self._print(progress.ingestion_banner(label, session_id, total_sets,
                                              self.workers))

        # Print individual chatset character counts before starting ingestion
        for i, cs in enumerate(jobs, 1):
            self._print(progress.chatset_count_line(i, total_sets, cs.chars))

        ok = True
        counts = {"done": 0, "failed": 0, "skipped": 0}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {}
            for i, cs in enumerate(jobs, 1):
                f = pool.submit(self.run_chatset, cs, i)
                futs[f] = cs
            completed = 0
            # How many chatsets this run is doing, which is not the same as
            # how many the session contains.
            run_total = len(jobs)
            run_position: dict[str, int] = {}
            for f in as_completed(futs):
                cs = futs[f]
                completed += 1
                run_position[cs.chatset_id] = completed
                self._print(f"Chatset {completed}/{len(jobs)} "
                            f"({cs.source_chunk_id or cs.set_number}) finished. "
                            f"Total characters: {cs.chars:,}")
                try:
                    r = f.result()
                except Exception as e:
                    ok = False
                    self._print(f"  ✗ chatset {cs.chatset_id} crashed: {e}")
                    continue
                # Count the chatset by the stages that actually ran. A bounded
                # run (``--stage distiller``) leaves the other stages out of the
                # result entirely, which must not count as a failure.
                ran = [s for s in ("distiller", "introbeaver", "planbeaver",
                                   "outrobeaver") if s in r]
                r_d = r.get("distiller", "")
                distiller_skipped = (
                    self.ck.cs_stage(cs.session_id, cs.set_number,
                                     "distiller").get("skip_reason") is not None
                    or self.ck.cs_stage(cs.session_id, cs.set_number,
                                        "distiller").get("verdict") in ("true", True))
                if r_d == SKIPPED or distiller_skipped:
                    counts["skipped"] += 1
                elif any(r.get(s) in (FAILED, BLOCKED) for s in ran):
                    counts["failed"] += 1
                    ok = False
                elif ran and all(r.get(s) == DONE for s in ran):
                    counts["done"] += 1
                else:
                    counts["failed"] += 1
                    ok = False

                # ── Debug event writing ─────────────────────────
                if self._debug_dir:
                    try:
                        from .debug_dashboard_skeu import write_event
                        # Read checkpoint data for this chatset
                        ck_data = {}
                        for sn in ("distiller", "introbeaver", "planbeaver", "outrobeaver"):
                            sd = self.ck.cs_stage(sid, n, sn)
                            if sd.get("status") not in ("PENDING", "RUNNING"):
                                ck_data[sn] = sd
                        # Build debug event
                        ev = {
                            "stage": "chatset",
                            "status": ("done" if all(v.get("status") == DONE
                                                     for v in ck_data.values())
                                       and ck_data else "running"),
                            "session_id": sid,
                            # Read from the run's map, never resolved here:
                            # this event and the per-stage events must carry
                            # one code for the session.
                            "session_code": self.session_codes.code(cs.session_id),
                            "chatset_number": n,
                            "chatset_id": cs.chatset_id,
                            "chars_in": cs.chars,
                            "chatset_hash": cs.chatset_hash[:12] + "…",
                            "stages": ck_data,
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        }
                        write_event(self._debug_dir, ev)
                    except Exception:
                        pass  # debug failure doesn't break pipeline

                # ── Print clean table after chatset completes ─
                sid, n = cs.session_id, cs.set_number
                stages_data: dict[str, Any] = {}
                for sn in ("distiller", "introbeaver", "planbeaver", "outrobeaver"):
                    sd = self.ck.cs_stage(sid, n, sn)
                    if sd.get("status") not in ("PENDING", "RUNNING"):
                        stages_data[sn] = sd
                self._print(progress.chatset_table(
                    n, cs.total_sets or len(cs.chatsets), stages_data,
                    gateway_map=self.gateway_map,
                    position=run_position.get(cs.chatset_id)))
                self._print(_DIVIDER)
                self._print()
        # Print overall summary, then where the wall-clock time actually went.
        self._print(progress.session_summary(
            counts["done"], counts["failed"], counts["skipped"], len(jobs)))
        for line in progress.stage_ledger():
            self._print(line)
        # The console summary above never reaches the master log (that file is
        # written by StageLogger, not stdout), so record the same tally there.
        self.log.summary(session=session_id, done=counts["done"],
                         failed=counts["failed"],
                         skipped=counts["skipped"], total=len(jobs))
        return ok

    # -- stages 2-4: one build unit (session or merged project) --------------

    def _merged_raw(self, sessions: list[Session]) -> list[dict]:
        """All chatsets of the build unit, chronological, deduped by hash."""
        seen: set[str] = set()
        merged: list[dict] = []
        for s in sorted(sessions, key=lambda x: x.sort_key):
            for cs in s.chatsets:
                if cs.chatset_hash in seen:
                    continue
                seen.add(cs.chatset_hash)
                merged.extend(cs.raw_chat_set)
        return merged

    def _run_build_stage_safely(self, build: str, stage: str, **kwargs) -> bool:
        """``_run_build_stage``, where a transport failure fails the stage, not the run.

        A stage that cannot get an answer (the model's endpoint is down, a rate
        limit outlasted every retry) is recorded as failed with the reason and
        the build halts there, the same as a stage that failed its gate. The
        process keeps its trajectory log and its Soma instead of dying.
        """
        try:
            return self._run_build_stage(build, stage, **kwargs)
        except Exception as exc:
            detail = f"TRANSPORT: {type(exc).__name__}: {exc}"
            self.ck.ss_mark(build, stage, FAILED, errors=detail[:300])
            self._log_trajectory_error(stage=stage, session_id=build,
                                       chatset_id=build, detail=detail[:500])
            self._print(f"  -[{stage.upper()}]- [FAILED] {detail[:140]}")
            return False

    def _run_build_stage(self, build: str, stage: str, *,
                         tool_hooks: dict | None = None,
                         expected_hash: str | None = None) -> bool:
        cur = self.ck.ss_stage(build, stage)
        if cur["status"] in (DONE, SKIPPED):
            self.log.info(stage, f"{build} already {cur['status']} (resume)")
            return True
        self.ck.ss_mark(build, stage, RUNNING)
        model = self.client.model_for(stage)
        self.log.start(stage, session=build, model=model,
                       detail=f"build_unit={build}")
        t0 = time.monotonic()
        # ---- Spinner + timeout watcher (NEW) ----
        spinner_label = f"{stage} {build} {model}"
        self._watch_start = t0
        stop_event = threading.Event()
        watcher = threading.Thread(
            target=self._timeout_watcher,
            args=(spinner_label, self.client.timeout, stop_event),
            daemon=True,
        )
        watcher.start()
        # A build stage is handed nothing. The model reads its instructions
        # and what earlier stages wrote from the Soma by tool call, and writes
        # its output there. ``tool_hooks`` adds the stage's own tools (a data
        # store the model queries, such as the invariants corpus).
        soma_kwargs = {
            "soma": self._soma, "soma_lock": self._soma_lock,
            "build_chatsets": getattr(self, "_build_chatsets", None),
            "tool_hooks": tool_hooks}
        try:
            # Same rule as the per-chatset stages: the build unit's first call
            # sends no id (the engine generates one); later calls send it.
            with self._engine_sessions.scope(build) as engine_code:
                res = self._with_spinner(
                    spinner_label,
                    self.client.call_stage,
                    stage, session_id=build, chatset_id=build,
                    expected_hash=expected_hash,
                    session_code=engine_code,
                    live_stage=stage,
                    **soma_kwargs,
                )
                _learn_engine_session(
                    self._engine_sessions, build, engine_code,
                    res.record.engine_session, self._print)
        finally:
            stop_event.set()
            watcher.join(timeout=0.5)
        rec, gate = res.record, res.record.gate or GateResult(skipped=False, verdict="UNKNOWN", reasons=["Stage exhausted"], errors=[], parsed=None)
        latency = (time.monotonic() - t0) * 1000
        with self._manifest_lock:
            self.manifest.add(rec, gate)
        self._log_trajectory(stage=stage, session_id=build, chatset_id=build,
                             rec=rec, gate=gate)
        p = write_artifact(self.out, build, stage, None, rec, gate,
                           group=GROUP_OF[stage])
        if is_done(gate):
            self.ck.ss_mark(build, stage, DONE, verdict=gate.verdict or None,
                            model=rec.model, tokens=rec.total_tokens,
                            latency_ms=round(rec.latency_ms, 1),
                            artifact=str(p.relative_to(self.out)))
            if gate.parsed is not None:
                self._save_artifact(build, stage, gate.parsed)
            self.log.done(stage, session=build,
                          detail=f"chars_out={len(rec.raw_response or '')} ms={latency:.0f} "
                                 f"model={rec.model} verdict={gate.verdict}")
            # ---- Soma graph writes: the graph nodes built from the stage output ----
            self._print(f"  -[{stage.upper()}]- [DONE] chars_in={rec.chars_in:,} "
                        f"out={len(rec.raw_response or ''):,} "
                        f"{latency / 1000:.1f}s  model={rec.model or 'N/A'}")
            with self._soma_lock:
                if stage == "nexus":
                    counts = self._bridge.write_nexus(build, gate, rec.model, latency)
                elif stage == "spark":
                    counts = self._bridge.write_spark(build, gate, rec.model, latency)
                elif stage == "falcon":
                    counts = self._bridge.write_falcon(build, gate, rec.model, latency)
                elif stage == "drafter":
                    counts = self._bridge.write_drafter(build, gate, rec.model, latency)
                elif stage == "scout":
                    counts = self._bridge.write_scout(build, gate, rec.model, latency)
                elif stage == "oc":
                    counts = self._bridge.write_oc(build, gate, rec.model, latency)
                elif stage == "olc":
                    counts = self._bridge.write_olc(build, gate, rec.model, latency)
                    t_counts = self._bridge.write_terminal(build, rec.model, latency)
                    counts.update(t_counts)
                    g_counts = self._bridge.write_gate(build)
                    counts.update(g_counts)
                else:
                    counts = {}
                self._soma._persist()
            self._log_summary(stage, build, counts)
            return True
        if is_hard_block(gate):
            self.ck.ss_mark(build, stage, BLOCKED, verdict=gate.verdict,
                            model=rec.model,
                            artifact=str(p.relative_to(self.out)))
            self.log.block(stage, session=build,
                           detail=f"verdict={gate.verdict} models={len(rec.attempts)}")
            self._print(f"  -[{stage.upper()}]- [BLOCKED] verdict={gate.verdict or 'BLOCKED'} "
                        f"{latency / 1000:.1f}s  model={rec.model or 'N/A'}")
        else:
            self.ck.ss_mark(build, stage, FAILED, model=rec.model,
                            errors=gate.reasons[:300],
                            artifact=str(p.relative_to(self.out)))
            self.log.fail(stage, session=build,
                          detail=f"models={len(rec.attempts)} errors={gate.reasons[:80]}")
            self._print(f"  -[{stage.upper()}]- [FAILED] "
                        f"{gate.first_reason()} "
                        f"{latency / 1000:.1f}s  model={rec.model or 'N/A'}")
        return False

    # What each build stage reads from the Soma, so a stage whose input is not
    # there halts instead of running on nothing.
    _BUILD_NEEDS = {
        "spark": ("nexus",), "falcon": ("spark",), "eagle": ("falcon",),
        "scout": ("drafter",), "oc": ("drafter", "scout"),
        "olc": ("oc", "scout", "drafter"),
    }

    def _missing_soma_data(self, build: str, stage: str) -> list[str]:
        """What ``stage`` reads that the Soma does not hold. Empty: it can run."""
        missing = []
        if stage == "nexus":
            for csid in getattr(self, "_build_chatsets", None) or []:
                if self._soma.get_node(csid) is None:
                    missing.append(f"ChatSet {csid}")
        elif stage == "drafter":
            if not any(self._load_artifact(csid, "planbeaver")
                       for csid in getattr(self, "_build_chatsets", None) or []):
                missing.append("StageOutput planbeaver (no chat set has one)")
        for needed in self._BUILD_NEEDS.get(stage, ()):
            if self._load_artifact(build, needed) is None:
                missing.append(f"StageOutput {needed}")
        return missing

    def _scout_tools(self, files: list[dict], code: str) -> dict:
        """The tools scout is given for the invariants corpus.

        Both answer from the corpus and share one id table, so a law keeps its
        DNA id however many times it is asked for.
        """
        dna_ids: dict = {}

        def query_invariants(a: dict) -> str:
            rows = self._query_invariants(
                str(a.get("query") or ""), family=a.get("family"),
                min_composite=float(a.get("min_composite") or 0.0),
                limit=a.get("limit") or 20, ids=dna_ids)
            self._print(f"  scout query_invariants "
                        f"family={a.get('family') or 'any'} "
                        f"{str(a.get('query') or '')[:50]!r}: "
                        f"{len(rows)} law(s)")
            return canonical_json(rows)

        def get_dna_contracts(_a: dict) -> str:
            rows = self._query_invariants(code, limit=60, ids=dna_ids)
            return canonical_json(rows) if rows else self._scout_contracts(files, code)

        return {
            "query_invariants": (
                "Query the invariants corpus (611,000 laws) for testable "
                "statements that apply to something in the drafted code. Ask "
                "per function or concern, with a family, and keep the limit "
                "small: a targeted query is fast and on topic. Returns "
                "contracts with their DNA ids.",
                query_invariants,
                {"type": "object", "properties": {
                    "query": {"type": "string",
                              "description": "Words describing the concern, "
                                             "e.g. a function name and what it does."},
                    "family": {"type": "string",
                               "enum": list(self.INVARIANT_FAMILIES),
                               "description": "Restrict to one family."},
                    "limit": {"type": "integer",
                              "description": "How many laws, 1-100 (default 20)."}},
                 "required": ["query"]}),
            "get_dna_contracts": (
                "The corpus laws that best match the drafted code as a whole, "
                "a broad starting set. Use query_invariants to go deeper on a "
                "function or family.",
                get_dna_contracts),
        }

    # The families the corpus holds, with their size, so a stage asking for
    # laws can name one. Filtering by family is what keeps a query fast and on
    # topic (the cheatsheet's first rule).
    INVARIANT_FAMILIES = (
        "DATA_INVARIANTS", "API_CONTRACTS", "STATE_TRANSITIONS",
        "FAILURE_MODES", "OBSERVABILITY", "DEPLOYMENT", "SECURITY", "TESTING",
        "DISTRIBUTED_SYSTEMS")

    def _query_invariants(self, text: str, *, family: str | None = None,
                          min_composite: float = 0.0, limit: int = 20,
                          ids: dict | None = None) -> list[dict]:
        """Laws from the corpus for ``text``, shaped as scout's dna_contracts.

        ``ids`` maps a statement to its ``DNA-NNN`` id and is shared by every
        query in one scout run, so asking twice never gives one law two ids
        and two laws never share one.
        """
        from .invariants import LawCorpus
        ids = ids if ids is not None else {}
        corpus = LawCorpus(getattr(self, "law_db",
                                  _paths.invariants_db()))
        try:
            rows = corpus.search(text, limit=max(1, min(int(limit), 100)),
                                 min_confidence=0.5, min_score=min_composite,
                                 family=family or None, max_terms=24)
        finally:
            corpus.close()
        out = []
        seen: set = set()
        for law in rows:
            stmt = law.get("statement") or ""
            # the same law is stored once per repo it was found in; one
            # statement is one contract
            if not stmt or stmt in seen:
                continue
            seen.add(stmt)
            cid = ids.setdefault(stmt, f"DNA-{len(ids) + 1:03d}")
            out.append({"contract_id": cid, "statement": stmt,
                        "family": law.get("family") or "GENERAL",
                        "origin": (f"{law.get('repo')}:{law.get('scope')}"
                                   if law.get("repo") else "truth_corpus"),
                        "composite": law.get("composite")})
        return out

    def _scout_contracts(self, files: list[dict], code: str) -> str:
        """Build the ``dna_contracts`` block scout expects.

        Contracts come from the collapsed truth corpus, ranked by BM25 against
        the artifact's own text, so they describe what this code claims to do
        rather than a fixed list of four generic rules.

        Falls back to the local registry only when the corpus is unavailable,
        and always emits the ``[{contract_id, statement, family, origin}]``
        shape TB-012 declares.
        """
        from .invariants import LawCorpus

        wanted = int(getattr(self, "scout_contracts", 60) or 60)
        corpus = LawCorpus(getattr(self, "law_db",
                                  _paths.invariants_db()))
        contracts: list[dict] = []
        used_corpus = corpus.available

        if used_corpus:
            for law in corpus.search(code, limit=wanted):
                stmt = law.get("statement") or ""
                if not stmt:
                    continue
                contracts.append({
                    "contract_id": f"DNA-{len(contracts) + 1:03d}",
                    "statement": stmt,
                    "family": law.get("family") or "GENERAL",
                    "origin": (f"{law.get('repo')}:{law.get('scope')}"
                               if law.get("repo") else "truth_corpus"),
                    "composite": law.get("composite"),
                })
            corpus.close()

        if not contracts:
            from .invariants import InvariantRegistry
            exts = [Path(f.get("path", "")).suffix for f in files if f.get("path")]
            for i, r in enumerate(InvariantRegistry().query(exts), 1):
                contracts.append({
                    "contract_id": f"DNA-{i:03d}",
                    "statement": r.get("statement", ""),
                    "family": str(r.get("family", "GENERAL")).upper(),
                    "origin": f"local:{r.get('rule_id', '')}",
                })

        self._print(f"  scout contracts: {len(contracts)} "
                    f"({'truth corpus' if used_corpus else 'local fallback'})")
        return canonical_json(contracts)

    def run_stages_2_4(self, build: str, sessions: list[Session],
                       from_stage: str | None = None,
                       step_mode: bool = False,
                       granular: bool = False) -> bool:
        if granular:
            self._print("\n  [Step: Stages 2-4 build. Enter to run, Ctrl+C to skip]")
            try: input()
            except EOFError: pass
        merged_raw = self._merged_raw(sessions)
        h = chatset_hash_for(merged_raw)
        # The build's chat sets, in the order _merged_raw reads them. The
        # model's get_chatset(build) merges these from the Soma.
        self._build_chatsets = [
            cs.chatset_id
            for s in sorted(sessions, key=lambda x: x.sort_key)
            for cs in s.chatsets]
        ctx = {"session_id": build, "chatset_id": build, "chatset_hash": h}
        skip_to = 0
        order = ["nexus", "spark", "falcon", "eagle", "drafter", "scout",
                 "oc", "olc"]
        if from_stage:
            if from_stage not in order:
                raise ValueError(f"--from: unknown build stage {from_stage!r}")
            skip_to = order.index(from_stage)

        stages = order[skip_to:]
        failed: list[str] = []
        for stage in stages:
            if self.granular:
                self._print(f"\n  [Step: {stage.upper()} build stage]")
                self._print("  Enter to run, Ctrl+C to skip")
                try:
                    answer = input().strip().lower()
                    if answer == "n" or answer == "no":
                        self._print(f"  Skipping {stage}.")
                        st_status = SKIPPED
                    else:
                        st_status = None
                except EOFError:
                    st_status = None
                if st_status == SKIPPED:
                    self.ck.ss_stage(build, stage, st_status)
                    self._print(f"  {stage.upper()} [SKIPPED]")
                    continue
            self._print(f"\n=== {stage.upper()} ({build}) ===")
            # The stage reads everything from the Soma. If the Soma does not
            # hold what it needs, the stage does not run: MISSING_SOMA_DATA.
            missing = self._missing_soma_data(build, stage)
            if missing:
                reason = "MISSING_SOMA_DATA: " + ", ".join(missing)
                self.ck.ss_mark(build, stage, FAILED, errors=reason)
                self._print(f"  -[{stage.upper()}]- [FAILED] {reason}")
                # It never called a model, but it is a failed stage: it goes on the
                # pipeline's record and on the engine's log like any other failure.
                self._log_trajectory_error(stage=stage, session_id=build,
                                           chatset_id=build, detail=reason)
                self._report_halt(stage, build, reason)
                failed.append(stage)
                continue            # the stages after it still run; each checks its own inputs
            tool_hooks = None
            if stage == "scout":
                # scout reads the drafted code from the Soma (drafter's output
                # node) and queries the invariants corpus with its own tools.
                draft = self._load_artifact(build, "drafter") or {}
                files = ((draft.get("drafter_output") or draft.get("draft_artifact") or {})
                         .get("files", []))
                code = "\n\n".join(
                    f"### {f.get('path')}\n{f.get('content', '')}" for f in files)
                tool_hooks = self._scout_tools(files, code)
            ok = self._run_build_stage_safely(
                build, stage, tool_hooks=tool_hooks,
                expected_hash=h if stage in ("nexus", "spark", "falcon", "eagle") else None)
            if stage == "oc" and ok:
                oc = self._load_artifact(build, "oc") or {}
                v = ((oc.get("oc_output") or oc.get("oc_decision") or {})
                     .get("verdict", ""))
                if v == "REWORK":
                    self.ck.ss_mark(build, "oc", BLOCKED, verdict="REWORK")
                    self._print("  ⊘ OC verdict REWORK -- build BLOCKED."
                                " See rework_order; --redo drafter when"
                                " ready.")
                    return False
            if stage == "olc" and ok:
                if self.granular:
                    self._print("\n  [Step: write codebase. Enter to run, Ctrl+C to skip]")
                    try: input()
                    except EOFError: pass
                self._write_codebase(build, lambda st: self._load_artifact(build, st))
            # ---- Dashboard board (NEW) ----
            self._print_dashboard(sessions)
            # stop on any non-DONE above
            st = self.ck.ss_stage(build, stage)["status"]
            if st not in (DONE, SKIPPED):
                self._print(f"  {stage} {st}: the build goes on with the stages after it;"
                            f" one that needs {stage}'s output will say so")
                failed.append(stage)
                continue
            if self.step_mode:
                print(f"\n  [{stage.upper()} done. Press Enter for next stage...]")
                input()
        if failed:
            self._print(f"\n  build finished with failed stage(s): {', '.join(failed)}")
        return not failed

    def _write_codebase(self, build: str, upstream) -> None:
        target = self.out / "codebase" / build
        olc = upstream("olc")
        files = []
        if olc:
            files = (olc.get("olc_output", {}) or {}).get("hardened_files", [])
        if not files:
            draft = upstream("drafter") or {}
            files = ((draft.get("drafter_output") or draft.get("draft_artifact") or {})
                         .get("files", []))
        for f in files:
            path = str(f.get("path", "")).lstrip("/")
            if not path or ".." in path:
                continue
            dest = target / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f.get("content", ""))
        self._print(f"  ▸ codebase written: {target} "
                    f"({len(files)} file(s), "
                    f"{'hardened' if olc else 'draft'})")
        # ---- Soma: CodePublisher writes VerifiedNodes (NEW) ----
        with self._soma_lock:
            self._bridge.write_publisher(build, len(files))
            self._soma._persist()


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------

def restrict_to_chatsets(sessions: list, only) -> list:
    """The sessions, each cut down to the chat sets ``only`` names.

    ``only`` is (session_id, set_number) or a list of them. A session that
    ``only`` does not name is left whole. Naming a chat set the session does
    not have is an error, never an empty build.
    """
    import dataclasses
    pairs = ([only] if (isinstance(only, tuple) and len(only) == 2
                        and isinstance(only[0], str)) else list(only))
    out = []
    for s in sessions:
        wanted = {n for sid, n in pairs if sid == s.session_id}
        if not wanted:
            out.append(s)
            continue
        kept = [cs for cs in s.chatsets if cs.set_number in wanted]
        missing = wanted - {cs.set_number for cs in kept}
        if missing:
            raise ValueError(f"{s.session_id} has no chat set(s) {sorted(missing)}")
        out.append(dataclasses.replace(s, chatsets=kept))
    return out


def _pause(prompt: str = "\n  Press Enter to continue...") -> None:
    """Wait for Enter, but only when a human is attached."""
    if not sys.stdin.isatty():
        return
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        pass


def select_sessions(sessions: list[Session],
                    only: tuple[str, int] | list[tuple[str, int]] | None = None
                    ) -> list[str]:
    """Session ids this run will touch, in a stable order.

    ``--only`` narrows the run to the sessions it names; without it the run
    touches the sessions it was given. Resolving codes only for these keeps a
    number from being minted for a session that never runs.
    """
    if only:
        pairs = ([only] if (isinstance(only, tuple) and len(only) == 2
                            and isinstance(only[0], str)) else list(only))
        wanted = {str(p[0]) for p in pairs}
        # Only sessions this run actually holds. A name in --only that the
        # source did not yield selects nothing, and a code minted for it would
        # be a permanent number spent on a run that never touched it.
        return sorted(s.session_id for s in sessions if s.session_id in wanted)
    return sorted(s.session_id for s in sessions)


def resolve_session_codes(sessions: list[Session],
                          only: tuple[str, int] | list[tuple[str, int]] | None = None
                          ) -> dict[str, str]:
    """One short code per selected session, resolved at selection.

    ``run_engine.py`` calls this before it prints anything and passes the
    mapping to ``run``. This is the fallback for a caller that enters through
    ``run`` directly: it resolves at exactly the same point (selection, before
    ``Engine`` exists), so a run still performs one lookup per session and
    never resolves again inside a stage.

    A session whose code cannot be resolved is left out; ``SessionCodes``
    fills it with ``unknown`` on first ask, the same degradation the logger
    and the display already use.
    """
    codes: dict[str, str] = {}
    for sid in select_sessions(sessions, only):
        try:
            codes[sid] = short_codes.code_for(sid)
        except Exception:  # noqa: BLE001 - a code never stops a run
            continue
    return codes


def run(sessions: list[Session], *, out_dir: Path,
        client: MachineTask if False else MachineTalkClient,
        workers: int = 4, from_stage: str | None = None,
        only: tuple[str, int] | list[tuple[str, int]] | None = None,
        quiet: bool = False,
        force: bool = False,
        gateway_map: dict | None = None,
        debug_dir: str | None = None,
        max_chatsets: int | None = None,
        step_mode: bool = False,
        stage: str | None = None,
        granular: bool = False,
        session_codes: dict[str, str] | None = None,
        announced: bool = False,
        phase: str | None = None,
        run_id: str | None = None) -> int:
    """Run one phase of the pipeline.

    ``phase="extract"`` is the extraction loop and the only caller of the
    four beaver stages. It starts blind: a new Soma holding just the
    SessionAnchor, the ChatSets and their Messages, which the loop then fills.

    ``phase="crystallize"`` is the generation loop and the only caller of
    nexus through olc. It works on the Soma the extraction loop filled, and
    halts with MISSING_SOMA_DATA if what a stage reads is not in it.

    There is no phase that runs both: a caller that wants both runs extract
    and then crystallize.
    """
    if phase not in ("extract", "crystallize"):
        raise ValueError(
            "run() needs phase='extract' or phase='crystallize'; "
            f"got {phase!r}")
    # One SOMA database per chat session. If --only targets a single
    # session, use that session's database even when the queue has more.
    target_session = None
    if only:
        pairs = ([only] if (isinstance(only, tuple) and len(only) == 2
                            and isinstance(only[0], str)) else list(only))
        sids = {p[0] for p in pairs}
        if len(sids) == 1:
            target_session = next(iter(sids))
    if target_session:
        soma_file = SOMAS_DIR / target_session / "soma.db"
    elif len(sessions) == 1:
        soma_file = SOMAS_DIR / sessions[0].session_id / "soma.db"
    else:
        soma_file = Path(out_dir) / "soma.db"

    # Every run starts from a new Soma database (SQLite, built from
    # soma_template.sql) and a clean checkpoint, so a test is a whole run
    # and not a resume of the last one. The old files are renamed to
    # .bak-<time>, never deleted. SOMA_RESUME=1 keeps them and resumes.
    blind_start = phase == "extract" and not _os.environ.get("SOMA_RESUME")
    if blind_start:
        stamp = _time_mod.strftime("%Y%m%d-%H%M%S")
        moved = []
        cks = CheckpointStore(out_dir, layout="chunk")
        targets = [soma_file] + [cks._path(s.session_id) for s in sessions]
        for t in targets:
            if t.exists():
                t.rename(t.with_name(t.name + f".bak-{stamp}"))
                moved.append(t.name)
        if not quiet:
            progress.emit(
                "  Fresh run: archived " + (", ".join(moved) + f" (.bak-{stamp})"
                                              if moved else "nothing (no old Soma DB or checkpoint)"))

    # Selection: one resolution per session, before the Engine exists and
    # before any stage can print. The codes are pinned into the run so the
    # logger, the debug event and the progress header read this mapping
    # rather than resolving again. A caller that already holds the codes (a
    # launcher that printed them) may pass them in; that is not a lookup.
    if session_codes is None:
        session_codes = resolve_session_codes(sessions, only)

    eng = Engine(out_dir=out_dir, client=client, workers=workers,
                 quiet=quiet, force=force, gateway_map=gateway_map,
                 debug_dir=debug_dir, max_chatsets=max_chatsets,
                 step_mode=step_mode, soma_path=soma_file,
                 session_codes=session_codes, announced=announced)
    if not announced and not quiet:
        # Announce at selection, before the first stage. A caller that already
        # printed the lines passes announced=True and nothing is repeated.
        for sid in session_codes:
            eng.announce_session(sid)
    if stage:
        # Accept a comma-separated list so a strict isolation run can execute
        # a prefix of the pipeline (e.g. "distiller,introbeaver") without
        # bringing the later stages along.
        _stage_names = [s.strip() for s in str(stage).split(",") if s.strip()]
        eng._stage = (_stage_names[0] if len(_stage_names) == 1
                      else tuple(_stage_names))
    eng.granular = granular
    eng._blind_start = blind_start
    eng.run_id = run_id or new_run_id()
    eng.phase = phase
    eng.traj.append({
        "schema": "trajectory/1", "type": "run", "run_id": eng.run_id,
        "phase": phase, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sessions": [x.session_id for x in sessions],
        "profile": getattr(getattr(client, "_tk", None), "_profile_path", ""),
        "pins": getattr(getattr(client, "_tk", None), "_pins", {}),
        "workers": workers})
    if not quiet:
        print(f"  run id: {eng.run_id}  ({phase})", flush=True)
    if len(sessions) == 1:
        build = sessions[0].session_id
    else:
        # A readable concatenation of 18 session ids exceeds NAME_MAX and
        # turns every checkpoint stat into ENAMETOOLONG. Keep it stable but
        # short.
        names = "|".join(s.session_id for s in sessions)
        build = "project-" + hashlib.sha1(names.encode("utf-8")).hexdigest()[:16]

    if phase == "extract":
        # --- the extraction loop: stage 1 only -----------------------------
        if from_stage in (None, "distiller"):
            if not eng.run_stage1(sessions, only=only):
                return 1
        # Only the old count flag stops after extraction. A named chat set goes on
        # into the build, so there is nothing to wait for.
        if not quiet and max_chatsets is not None and max_chatsets > 0:
            print("\n  [Bounded run: one chatset.]")
            _pause()
        ok = True
    else:
        # --- the crystallize loop: nexus through olc only ------------------
        if max_chatsets is not None and max_chatsets > 0:
            print("  --max-chatsets belongs to the extraction loop. To build from "
                  "chosen chat sets, name them with --only.", flush=True)
            return 1
        if only:
            # The build covers exactly the chat sets named: one chat set runs
            # through all twelve stages, not just the four beavers.
            sessions = restrict_to_chatsets(sessions, only)
        if not Path(soma_file).exists():
            print(f"  MISSING_SOMA_DATA: no Soma at {soma_file}. Run the "
                  f"extraction loop first.", flush=True)
            return 1
        if step_mode:
            _pause("\n  [Press Enter to continue to stages 2-4...]")
        build_from = from_stage if from_stage in (
            "nexus", "spark", "falcon", "eagle", "drafter", "scout", "oc",
            "olc") else None
        ok = eng.run_stages_2_4(build, sessions, from_stage=build_from,
                                granular=granular)

    # final manifest
    if granular:
        eng._print("\n  [Step: manifest. Enter to run, Ctrl+C to skip]")
        try: input()
        except EOFError: pass
    header = {
        "build": build,
        "sessions": ", ".join(s.session_id for s in sessions),
        "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workers": workers,
    }
    mp = eng.manifest.render(header)
    eng._print(f"\nManifest: {mp}")
    return 0 if ok else 1
