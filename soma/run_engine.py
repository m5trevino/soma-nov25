#!/usr/bin/env python3
"""Run the MachineTalk engine with the live debug dashboard.

Usage (two loops, run one after the other):
    python3 run_engine.py --phase extract     --queue /path/to/soma_queue.json --profile /path/to/profile.json
    python3 run_engine.py --phase crystallize --queue /path/to/soma_queue.json --profile /path/to/profile.json
    python3 run_engine.py --soma-only --source /path/to/session --profile /path/to/profile.json

--phase extract: the extraction loop. Starts from a blind Soma (SessionAnchor,
  ChatSet and Message nodes only) and runs the four beavers.
--phase crystallize: the generation loop. Runs nexus through drafter, then
  AxonHillock -> TerminalPlayer -> SaltatoryGate -> CodePublisher, then scout
  through olc on the Soma that extract filled. Nothing else calls these stages.

--soma-only: Process ONLY the first session's first chat set (distiller only),
  write nodes into the Soma, then exit. Does NOT call classifier/integrator/retrograde.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from machinetalk import paths as soma_paths  # noqa: E402

if soma_paths.turnkey_path():
    sys.path.insert(0, soma_paths.turnkey_path())

from machinetalk.client import EngineClient
from machinetalk.engine import run, select_sessions, session_code_line
from machinetalk.source import load_source
from machinetalk.debug_dashboard_skeu import start_dashboard
from machinetalk.short_codes import code_for


def _log_output_path(output_path: str) -> None:
    """Append the output file path to the Hetzner output log.

    Only writes when the HETZNER_OUTPUT_LOG env var is set and non-empty.
    Each line is: <ISO timestamp> <output_path>
    """
    log_path = os.environ.get("HETZNER_OUTPUT_LOG", "").strip()
    if not log_path:
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{timestamp} {output_path}\n"
    try:
        with open(log_path, "a") as f:
            f.write(line)
    except OSError:
        # Fail silently — logging must not break the engine
        pass


def load_queue(queue_path: str) -> list[Path]:
    """Load session paths from a Soma queue JSON file."""
    qp = Path(queue_path)
    if not qp.exists():
        print(f"Queue file not found: {qp}", flush=True)
        return []
    with open(qp) as f:
        data = json.load(f)
    ids = data.get("selected", [])
    paths = []
    for sid in ids:
        # Exact path first: a consolidated session lives under
        # lineage/consolidated/<key> and must not be shadowed by a same-named
        # source session under the base root.
        exact = Path(sid)
        if exact.is_dir():
            paths.append(exact)
            continue
        # Try resolving the session_id to a session directory.
        # Format could be just the dir name or full session_id from dev_sessions.
        base = soma_paths.sessions_dir()
        dir_name = sid.split("/")[-1]
        candidate = base / dir_name
        if candidate.exists():
            paths.append(candidate)
        elif (base / sid).exists():
            paths.append(base / sid)
    return paths


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", default=None, help="session dir or project dir")
    p.add_argument("--queue", default=None, help="queue JSON file with selected session paths")
    p.add_argument("--profile", required=False, default=None,
                   help="TurnKey profile JSON (defaults to the profile in the last queue, "
                        "then the newest turn-key-profile/*.json)")
    p.add_argument("--out", default="/tmp/soma_out", help="output dir")
    p.add_argument("--debug", default="/tmp/soma_debug", help="debug JSONL dir")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--port", type=int, default=9876)
    p.add_argument("--soma-only", action="store_true",
                   help="Process only the first session's first chat set (distiller), write Soma, exit")
    p.add_argument("--max-chatsets", type=int, default=None,
                   help="Maximum number of chatsets to process (0 or None = all)")
    p.add_argument("--step-mode", action="store_true",
                   help="Pause after each stage, waiting for Enter to continue")
    p.add_argument("--stage", default=None,
                   help="Run only this stage: distiller, introbeaver, planbeaver, outrobeaver")
    p.add_argument("--granular", action="store_true",
                   help="Pause before every individual step: dashboard, each beaver, each build stage, codebase, manifest")
    p.add_argument("--from", dest="from_stage", default=None,
                   help="Resume the build at this stage (e.g. drafter); "
                        "earlier stages must already be DONE in --out")
    p.add_argument("--phase", choices=("extract", "crystallize"), default=None,
                   help="extract: the extraction loop (the four beavers, on a "
                        "blind-start Soma). crystallize: the generation loop "
                        "(nexus, spark, falcon, eagle, drafter, AxonHillock, "
                        "TerminalPlayer, SaltatoryGate, CodePublisher, scout, "
                        "oc, olc on the Soma extract filled). "
                        "Required unless --soma-only.")
    p.add_argument("--no-dashboard", action="store_true",
                   help="Do not start the live debug dashboard (headless and "
                        "parallel runs).")
    p.add_argument("--run-id", default=None,
                   help="Name this run in the trajectory log (default: generated). "
                        "A harness passes one id for both phases of a run.")
    p.add_argument("--force", action="store_true",
                   help="Re-run stages even when the checkpoint says DONE")
    p.add_argument("--only", action="append", default=None, metavar="SESSION:SET",
                   help="Run only this chatset (repeatable), e.g. --only 250718-1380:1")
    args = p.parse_args()

    if not args.profile:
        args.profile = _default_profile()
    if not args.profile:
        print("Error: --profile is required (no profile found)", flush=True)
        return 1

    args.profile = _ensure_engine_auth(args.profile)

    if args.soma_only:
        return _soma_only_run(args)
    if not args.phase:
        print("Error: --phase extract|crystallize is required. The two loops "
              "are separate: extract fills the Soma, crystallize builds from it.",
              flush=True)
        return 1

    if args.queue:
        sessions_list = []
        for spath in load_queue(args.queue):
            sessions_list.extend(load_source(spath))
        print(f"Loaded {len(sessions_list)} sessions from queue: {args.queue}", flush=True)
    elif args.source:
        sessions_list = load_source(Path(args.source))
        print(f"Loaded {len(sessions_list)} sessions from --source", flush=True)
    else:
        print("Error: provide --source or --queue", flush=True)
        return 1

    only = None
    if args.only:
        only = []
        for spec in args.only:
            sid, sep, num = spec.rpartition(":")
            if not sep or not sid or not num.isdigit():
                print(f"Error: --only expects SESSION:SET, got {spec!r}", flush=True)
                return 1
            only.append((sid, int(num)))

    # ── Selection: one resolution per session, announced before any stage ──
    #
    # This is the run's only session-code lookup. The mapping is handed to
    # run(), which pins it into the Engine so the logger, the debug events and
    # the progress headers all read these values instead of resolving again.
    # Printing here rather than deeper means the first thing a run writes is
    # the code an operator will later grep for.
    session_codes = {}
    for session_id in select_sessions(sessions_list, only):
        session_codes[session_id] = code_for(session_id)
        print(session_code_line(session_codes[session_id]), flush=True)

    client = EngineClient(args.profile)

    # Dashboard prompt is handled by the parent launcher (run_sessions.py)
    # when --granular is used through the launcher flow.

    if not args.no_dashboard:
        threading.Thread(
            target=start_dashboard,
            args=(args.debug,),
            kwargs={"port": args.port, "auto_open": False},
            daemon=True,
        ).start()
        print(f"Dashboard: http://127.0.0.1:{args.port}")

    ret = run(
        sessions_list,
        out_dir=Path(args.out),
        client=client,
        workers=args.workers,
        debug_dir=args.debug,
        max_chatsets=args.max_chatsets,
        step_mode=args.step_mode,
        stage=args.stage,
        granular=args.granular,
        force=args.force,
        only=only,
        from_stage=args.from_stage,
        phase=args.phase,
        run_id=args.run_id,
        session_codes=session_codes,
        announced=True,
    )
    _log_output_path(args.out)
    return ret


def _ensure_engine_auth(profile_path: str) -> str:
    """Read the engine URL and key from the environment, never from the profile.

    Profiles are portable configuration: they carry a deck and pipeline
    settings, and nothing secret. The engine URL and key come from
    ``ENGINE_URL`` and ``ENGINE_KEY`` at run time, so no profile file, commit,
    or shared copy ever contains a credential.

    Returns the profile path unchanged when the environment supplies what is
    needed. Prints a single warning when the key is absent, because otherwise
    the failure surfaces later as 401s that look like model or gateway faults.

    The TurnKey SDK loads every ``.env`` it can find from its own import
    (``turnkey/sdk.py:_load_dotenv``), and that import happens inside
    ``EngineClient.__init__`` -- after this function used to run. Checking the
    raw environment here therefore warned about a key that was about to be
    loaded, and the run then worked. Load first, then report what is actually
    there.
    """
    try:
        from turnkey.sdk import _load_dotenv
        _load_dotenv()
    except Exception:
        # An older SDK without the helper: fall through and report honestly on
        # whatever the environment already holds.
        pass
    if not os.environ.get("ENGINE_KEY"):
        print("  [warn] ENGINE_KEY is not set; inference calls will return 401",
              flush=True)
    return profile_path


def _engine_url_from_env() -> str:
    return os.environ.get("ENGINE_URL") or os.environ.get("PEACOCK_ENGINE_URL") or ""


def _engine_url_from_siblings(path: Path) -> str:
    for sibling in sorted(path.parent.glob("*.json")):
        if sibling == path:
            continue
        try:
            doc = json.loads(sibling.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        cfg = doc.get("pipeline") if isinstance(doc, dict) else None
        if not isinstance(cfg, dict):
            continue
        url = cfg.get("engineUrl") or cfg.get("engine_url")
        if url:
            return str(url)
    return ""


def _engine_key_from_siblings(path: Path) -> str:
    for sibling in sorted(path.parent.glob("*.json")):
        if sibling == path:
            continue
        try:
            doc = json.loads(sibling.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        cfg = doc.get("pipeline") if isinstance(doc, dict) else None
        if not isinstance(cfg, dict):
            continue
        key = cfg.get("engineKey") or cfg.get("engine_key")
        if key:
            return str(key)
    return ""


def _default_profile() -> str | None:
    root = Path(__file__).resolve().parent.parent
    queue = root / "data" / "soma_queue.json"
    if queue.exists():
        try:
            prof = json.loads(queue.read_text()).get("profile")
            if prof and Path(prof).is_file():
                return str(prof)
        except (OSError, json.JSONDecodeError):
            pass
    profiles = sorted((root / "turn-key-profile").glob("*.json"))
    return str(profiles[-1]) if profiles else None


# ---------------------------------------------------------------------------
# --soma-only: first-session, first-chat-set, distiller-only, Soma-write
# ---------------------------------------------------------------------------

def _soma_only_run(args) -> int:
    """Run distiller on the first chat set of the first session, write Soma, exit.

    Hard gates:
      - Processes exactly one chat set.
      - Processes exactly one session.
      - Does NOT call classifier (introbeaver), integrator (planbeaver), or retrograde (outrobeaver).
      - Does NOT call stages 2-4 (nexus, spark, falcon, drafter, AxonHillock,
        TerminalPlayer, SaltatoryGate, CodePublisher, scout, oc, olc).
    """
    from machinetalk.soma_bridge import SomaBridge
    import time as _time

    # Resolve session path
    session_path = None
    if args.queue:
        qp = Path(args.queue)
        if not qp.exists():
            print(f"Queue file not found: {qp}", flush=True)
            return 1
        with open(qp) as f:
            data = json.load(f)
        sel = data.get("selected", [])
        if not sel:
            print("Queue has no selected sessions.", flush=True)
            return 1

        base = soma_paths.sessions_dir()
        sid = sel[0]
        dir_name = sid.split("/")[-1]
        session_path = base / dir_name
        if not session_path.exists():
            session_path = Path(sid) if Path(sid).exists() else Path(dir_name)
    elif args.source:
        session_path = Path(args.source)
        if not session_path.exists():
            base = soma_paths.sessions_dir()
            session_path = base / session_path.name
    else:
        print("Error: provide --source or --queue", flush=True)
        return 1

    if not session_path.exists():
        print(f"Session directory not found: {session_path}", flush=True)
        return 1

    # Load the first session and its first chat set
    sessions = load_source(session_path)
    if not sessions:
        print(f"No sessions loaded from {session_path}", flush=True)
        return 1

    first_session = sessions[0]
    if not first_session.chatsets:
        print(f"Session {first_session.session_id} has no chat sets.", flush=True)
        return 1

    first_cs = first_session.chatsets[0]
    # Same selection rule as a full run: resolve once, print before the stage.
    # This path has no Engine, so the code is resolved here and used here; it
    # is not re-derived later because nothing later needs it.
    session_code = code_for(first_session.session_id)
    print(session_code_line(session_code), flush=True)
    print(f"SOMA-ONLY: session={session_code} set={first_cs.set_number} "
          f"hash={first_cs.chatset_hash[:16]}...", flush=True)

    # A real SQLite file, started blind: a Soma left by an earlier run is
    # renamed aside (never deleted), so only the seeded skeleton is in it.
    soma_file = Path(args.out) / "soma.db"
    soma_file.parent.mkdir(parents=True, exist_ok=True)
    if soma_file.exists():
        soma_file.rename(soma_file.with_name(
            soma_file.name + ".bak-" + _time.strftime("%Y%m%d-%H%M%S")))
    soma_path = str(soma_file)
    from store import Soma as SomaStore

    soma = SomaStore(soma_path)
    bridge = SomaBridge(soma)

    # Seed the Soma with the session's skeleton only: the SessionAnchor, the
    # ChatSet and its Message nodes. The hash the Soma assigns is the one the
    # stage is checked against.
    seeded = bridge.seed_session(
        first_session.session_id, [first_cs], project_name="Dendritic")
    soma._persist()
    chatset_hash = seeded["hashes"][first_cs.chatset_id]

    # Run distiller ONLY (no beavers, no stages 2-4). It is handed nothing: it
    # reads its instructions and the chat set from the Soma by tool call.
    client = EngineClient(args.profile)

    print(f"Chatset characters {first_cs.chars}", flush=True)

    res = client.call_stage(
        "distiller",
        session_id=first_session.session_id,
        chatset_id=first_cs.chatset_id,
        expected_hash=chatset_hash,
        soma=soma,
        raw_chat_set=bridge.read_chat_set(first_cs.chatset_id),
    )

    rec = res.record
    gate = rec.gate

    # Write ReceptorLog + edges for distiller result
    bridge.write_receptor(
        first_session.session_id,
        first_cs.chatset_id,
        first_cs.set_number,
        gate,
        rec.model,
        rec.latency_ms,
    )
    soma._persist()

    # Print results
    print(f"\nSoma: {soma_path}", flush=True)
    print(f"Nodes: {soma.node_count()}", flush=True)
    rows = soma._conn.execute("SELECT node_type, node_id FROM nodes ORDER BY node_type, node_id").fetchall()
    node_types = {}
    for row in rows:
        node_types.setdefault(row[0], []).append(row[1])
    for t in sorted(node_types):
        print(f"  {t}: {len(node_types[t])}", flush=True)
    print(f"Edges: {soma.edge_count()}", flush=True)
    print(f"Distiller verdict: {gate.verdict if gate else 'N/A'}", flush=True)
    print("SOMA-ONLY: DONE", flush=True)
    _log_output_path(soma_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
