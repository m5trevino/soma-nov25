#!/usr/bin/env python3
"""Session batch launcher for SOMA pipeline.

Ties together the session picker (web UI), pipeline execution, and
dashboard progress tracking in one workflow.

Usage:
    python run_sessions.py                  # Interactive picker → run pipeline
    python run_sessions.py --queue FILE     # Use existing queue or export file
    python run_sessions.py --progress       # Just show progress, don't run
    python run_sessions.py --dashboard      # Start dashboard only
    python run_sessions.py --all            # Process ALL sessions on disk

Pipeline execution:
    The engine is invoked via soma/run_engine.py with --queue support.
    Requires a TurnKey profile (auto-detected from turn-key-profiles/).

Dashboard:
    Runs on port 9877. Two tabs:
      - ORBS: per-chatset stage cards (existing view)
      - SESSIONS: batch progress queue (new view)

    Watch progress at: http://127.0.0.1:9877

Session Picker Integration (NEW):
    1. ./soma-session-picker/start.sh     # Starts picker (port 5174)
    2. Pick + export favorites in browser
    3. python run_sessions.py --queue data/soma_queue.json
    The queue file contains session IDs that resolve to on-disk paths.
    Full export JSON (with metadata, anchors, topics) is also supported.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import threading
import urllib.request
import webbrowser
from pathlib import Path


SOMA_DIR = Path(__file__).parent
DATA_DIR = SOMA_DIR / "data"
DEBUG_DIR = "/tmp/soma_debug"
QUEUE_FILE = str(DATA_DIR / "soma_queue.json")
QUEUE_FILE_ALT = str(DATA_DIR / "queue.json")
LAUNCH_CONFIG = SOMA_DIR / "data" / "soma_launch.json"
BASE_PATH_FILE = DATA_DIR / ".base_path"
PORT = 9877
SOMA_PKG = SOMA_DIR / "soma"


def _sessions_dir() -> Path:
    """Where the chat-log sessions live. SOMA_SESSIONS_DIR moves it (RunPod, a laptop)."""
    return Path(os.environ.get("SOMA_SESSIONS_DIR")
                or "/media/storage/dendritic123/chat-logs/sessions")
SOMA_RUNNER = SOMA_PKG / "run_engine.py"

# The banner lives beside the engine so the CLI and the board render the same
# art from one source.
if str(SOMA_PKG) not in sys.path:
    sys.path.insert(0, str(SOMA_PKG))
try:
    from banner import print_banner
except ImportError:  # a missing banner must never stop a run
    def print_banner(color: bool | None = None) -> None:
        return None

def ensure_debug_dir():
    """Create the debug directory."""
    Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)


def find_profile() -> str | None:
    """Auto-detect TurnKey profile from turn-key-profiles/."""
    for candidate in [
        SOMA_DIR / "soma" / "turn-key-profiles" / "soma-current.json",
        SOMA_DIR / "soma" / "turn-key-profiles" / "*profile*.json",
    ]:
        if candidate.exists():
            return str(candidate)
        patterns = list(SOMA_DIR.glob(str(candidate)))
        if patterns:
            return str(patterns[0])
    for candidate in [
        SOMA_DIR / "soma-current.json",
        SOMA_DIR / "*profile*.json",
    ]:
        if candidate.exists():
            return str(candidate)
        patterns = list(SOMA_DIR.glob(str(candidate)))
        if patterns:
            return str(patterns[0])
    return None


def load_queue(path: str) -> tuple[list[str], list[dict]] | None:
    """Load session queue from JSON file.

    Supports three formats:
      1. Queue format:       {"selected": ["id1", "id2"], "count": 2, ...}
      2. Full export JSON:   [{...session objects with metadata...}]
      3. Plain string array: ["id1", "id2"]

    Returns (session_ids, full_objects) where:
      - session_ids: list of session identifiers to resolve
      - full_objects: list of full session dicts (for metadata, anchors, etc.)
        — empty if the file was in queue format
    """
    qpath = Path(path)
    if not qpath.exists():
        print(f"  Queue file not found: {path}")
        return None
    try:
        data = json.loads(qpath.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  Failed to read queue: {e}")
        return None

    if not data:
        print(f"  Queue is empty: {path}")
        return None

    # Format 1: Full export JSON — array of session objects
    if isinstance(data, list) and len(data) > 0:
        first = data[0]
        if isinstance(first, dict) and "session_id" in first:
            ids = [s.get("session_id", "") for s in data if isinstance(s, dict)]
            return (ids, data)

    # Format 2: Queue format — {"selected": [...]}
    if isinstance(data, dict):
        ids = data.get("selected", [])
        if isinstance(ids, list) and len(ids) > 0:
            return (ids, [])

    # Format 3: Plain array of strings
    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], str):
        return (data, [])

    print(f"  Unknown queue format in: {path}")
    return None


def resolve_sessions_from_ids(session_ids: list[str]) -> list[str]:
    """Resolve session IDs (from the picker) to on-disk session paths.

    The session picker writes session_ids like:
      "26-01-12-peacock.project.timeline.revealed"

    These resolve to <sessions dir>/<dir_name> (SOMA_SESSIONS_DIR)
    where dir_name = session_id.split("/")[-1].
    """
    vault = _sessions_dir()
    resolved = []
    for sid in session_ids:
        if not sid:
            continue
        # Exact path first: a consolidated session (lineage/consolidated/<key>)
        # must not be shadowed by a same-named source session under the vault.
        exact = Path(sid)
        if exact.is_dir():
            resolved.append(str(exact))
            continue
        dir_name = sid.split("/")[-1]
        candidate = vault / dir_name
        if candidate.exists():
            resolved.append(str(candidate))
        elif (vault / sid).exists():
            resolved.append(str(vault / sid))
        else:
            d = Path(sid)
            if d.exists():
                resolved.append(str(d))
            else:
                print(f"    \u26a0 Session not found on disk: {sid}")
    return resolved


def load_session_info(path: str) -> dict:
    """Load session metadata from session.json."""
    sj = Path(path) / "session.json"
    if sj.exists():
        try:
            return json.loads(sj.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def interactive_pick():
    """Run the curses session picker and save queue."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOMA_DIR / "soma") + ":" + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "soma.machinetalk.session_picker"],
        cwd=str(SOMA_DIR),
        env=env,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  Session picker error:\n{result.stderr}")
        return None, []

    paths = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line.startswith("  /"):
            paths.append(line[2:])

    if not paths:
        print("  No sessions selected.")
        return None, []

    queue_data = {
        "selected": paths,
        "count": len(paths),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    qpath = Path(QUEUE_FILE)
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text(json.dumps(queue_data, indent=2))
    print(f"\n  Saved {len(paths)} sessions to {QUEUE_FILE}")
    return paths, []


def run_pipeline(paths: list[str], full_objects: list[dict] | None = None,
                 max_chatsets: int | None = None,
                 profile: str | None = None,
                 workers: int | None = None,
                 step_mode: bool = False,
                 stage: str | None = None,
                 granular: bool = False,
                 force: bool = False,
                 only: list[tuple[str, int]] | None = None,
                 resume: bool = False):
    """Run the SOMA pipeline on the selected sessions.

    Uses soma/run_engine.py with --queue for native queue consumption.
    This is the canonical runner — it supports debug dashboard, checkpointing,
    and the full MachineTalk pipeline stages.

    Parameters
    ----------
    profile : str | None
        Explicit profile path. Falls back to find_profile() if None.
    workers : int | None
        Number of parallel workers. Defaults to 2 if not specified.
    """
    # Step mode pauses after every build stage. It is opt-in: running from a
    # terminal no longer forces it, because an unattended run that stops on a
    # prompt stalls with no one there to press Enter.
    ensure_debug_dir()

    if profile is None:
        profile = find_profile()
    if not profile:
        print("  ERROR: No TurnKey profile found!")
        print("  Place a profile JSON in soma/turn-key-profiles/")
        print("  Or use: python run_sessions.py --profile /path/to/profile.json")
        return

    print_banner()

    print(f"\n  Profile: {profile}")
    print(f"  Sessions: {len(paths)}")
    print(f"  Debug dir: {DEBUG_DIR}")
    print(f"  Dashboard: http://127.0.0.1:{PORT}")
    if max_chatsets is not None:
        print(f"  Max chatsets: {max_chatsets}")

    # Show metadata summary if we have full export objects
    if full_objects:
        def _labels(value) -> list[str]:
            """Export metadata is either a string, a list, or a list of
            {name, confidence} objects depending on the extractor version."""
            if not value:
                return []
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                label = value.get("name") or value.get("label") or ""
                return [str(label)] if label else []
            if isinstance(value, (list, tuple)):
                out: list[str] = []
                for item in value:
                    out.extend(_labels(item))
                return out
            return [str(value)]

        domains = set()
        sentiments = set()
        for obj in full_objects:
            md = obj.get("metadata", {})
            if isinstance(md, dict):
                domains.update(_labels(md.get("domain")))
                sentiments.update(_labels(md.get("sentiment")))
        if domains:
            print(f"  Domains:   {', '.join(sorted(domains))}")
        if sentiments:
            print(f"  Sentiments: {', '.join(sorted(sentiments))}")
    print()

    # Start dashboard
    if granular:
        print("\n  [Step: start dashboard. Enter to run, Ctrl+C to skip]")
        try: input()
        except EOFError: pass
    dashboard_proc = start_dashboard()

    # Write queue file for dashboard compatibility
    queue_data = {
        "selected": paths,
        "count": len(paths),
        "running": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profile": profile,
    }
    qpath = Path(QUEUE_FILE)
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text(json.dumps(queue_data, indent=2))

    # Also write to legacy queue location for dashboard compatibility
    legacy_qpath = Path(QUEUE_FILE_ALT)
    legacy_qpath.parent.mkdir(parents=True, exist_ok=True)
    legacy_qpath.write_text(json.dumps(queue_data, indent=2))

    # Write full export JSON if we have metadata from the picker
    if full_objects:
        export_path = Path(str(QUEUE_FILE).replace("soma_queue.json", "soma_export.json"))
        export_path.write_text(json.dumps(full_objects, indent=2))

    # Run the SOMA engine via the canonical runner with --queue
    cmd = [
        sys.executable, str(SOMA_RUNNER),
        "--queue", str(qpath),
        "--profile", profile,
        "--debug", DEBUG_DIR,
        "--out", str(SOMA_DIR / "soma" / "output"),
        "--port", str(PORT),
        "--workers", str(workers if workers is not None else 2),
    ]
    # Two loops, never one process doing both. The extraction loop runs the
    # four beavers on a blind-start Soma; the crystallize loop runs nexus
    # through olc on what extraction wrote. Flags that select chat sets or
    # beaver stages belong to extraction.
    stage1_stages = ("distiller", "introbeaver", "planbeaver", "outrobeaver")
    # --max-chatsets is the old count flag and stays extraction-only. Naming chat
    # sets with --only runs them through every stage: the beavers, then the build
    # on exactly those chat sets.
    bounded = max_chatsets is not None and max_chatsets > 0
    if stage and stage not in stage1_stages:
        phases = ["crystallize"]
    elif stage or bounded:
        phases = ["extract"]
    else:
        phases = ["extract", "crystallize"]
    if step_mode:
        cmd.append("--step-mode")
    if granular:
        cmd.append("--granular")
    extract_flags: list[str] = []
    if max_chatsets is not None and max_chatsets > 0:
        extract_flags.extend(["--max-chatsets", str(max_chatsets)])
    if stage in stage1_stages:
        extract_flags.extend(["--stage", stage])
    if force:
        extract_flags.append("--force")
    crystallize_flags = ["--from", stage] if (stage and stage not in stage1_stages) else []
    only_flags: list[str] = []
    for spec in (only or []):
        # Accept either a "session:set" string from the CLI or an already
        # split (session, set) tuple from the interactive picker.
        if isinstance(spec, str):
            sid, _, setnum = spec.rpartition(":")
            if not sid or not setnum.isdigit():
                print(f"  Error: --only expects SESSION:SET, got {spec!r}")
                return
        else:
            sid, setnum = spec
        only_flags.extend(["--only", f"{sid}:{setnum}"])
    extract_flags.extend(only_flags)
    crystallize_flags.extend(only_flags)

    exit_code = None
    try:
        # Inherit stdio so stage output (and granular prompts) stream live
        # instead of being buffered until the subprocess exits.
        for phase in phases:
            print(f"\n  ── {phase.upper()} ──", flush=True)
            phase_cmd = cmd + ["--phase", phase] + (
                extract_flags if phase == "extract" else crystallize_flags)
            phase_env = dict(os.environ)
            if resume and phase == "extract":
                # keep the Soma and the checkpoint that were reset for this resume;
                # without this the extraction archives them and starts blind
                phase_env["SOMA_RESUME"] = "1"
            result = subprocess.run(
                phase_cmd,
                cwd=str(SOMA_DIR),
                env=phase_env,
                timeout=36000,
            )
            exit_code = result.returncode
            if result.returncode != 0:
                break   # crystallize never runs on a failed extraction

        if result.returncode == 0:
            print(f"\n  \u2713 Pipeline complete")
        else:
            print(f"\n  \u2717 Pipeline failed (exit {result.returncode})")
    except subprocess.TimeoutExpired:
        print("\n  \u2717 Pipeline timed out (10h limit)")
    except KeyboardInterrupt:
        print("\n  Pipeline interrupted.")
        queue_data["running"] = False
        queue_data["interrupted"] = True
        queue_data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        qpath.write_text(json.dumps(queue_data, indent=2))
        if dashboard_proc:
            dashboard_proc.terminate()
        return

    queue_data["running"] = False
    queue_data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    queue_data["exit_code"] = exit_code
    qpath.write_text(json.dumps(queue_data, indent=2))

    print(f"\n  Dashboard: http://127.0.0.1:{PORT}")
    print(f"  Press Ctrl+C to stop the dashboard.")


def start_dashboard():
    """Start the debug dashboard server as a background process."""
    ensure_debug_dir()

    cmd = [
        sys.executable, str(SOMA_DIR / "run_dashboard.py"),
        "--no-open",
        "--port", str(PORT),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(SOMA_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    return proc


def show_progress():
    """Show current pipeline progress from queue and checkpoint data."""
    result = load_queue(QUEUE_FILE)
    if result is None:
        result = load_queue(QUEUE_FILE_ALT)
    if result is None:
        print("  No queue file found. Run with --queue or use the picker.")
        return

    session_ids, full_objects = result
    paths = resolve_sessions_from_ids(session_ids)

    print(f"\n  Session Queue: {len(paths)} sessions\n")

    # Check progress from dashboard /progress endpoint
    try:
        import urllib.request
        resp = urllib.request.urlopen(f'http://127.0.0.1:{PORT}/progress', timeout=3)
        data = json.loads(resp.read())
        sessions = data.get("sessions", [])
        pct = data.get("overall_pct", 0)

        total = data.get("total", len(sessions))
        total_sets = data.get("total_sets", 0)
        done_sets = data.get("done_sets", 0)

        print(f"  Sessions: {total}  |  Chat Sets: {total_sets}  |  Done: {done_sets}  |  {pct:.1f}%")
        print()

        for s in sessions:
            status = s.get("status", "?")
            sid = (s.get("id") or "?")[:50]
            sets = f"{s.get('sets_done', 0)}/{s.get('sets_total', 0)}"
            bar = ""
            p = s.get("progress", 0)
            bar_len = 20
            filled = int(bar_len * p / 100)
            bar = "\u2588" * filled + "\u25ca" * (bar_len - filled)
            status_icon = {"done": "\u2713", "running": "\u2192", "queued": "\u25cb", "failed": "\u2717"}.get(status, "?")
            print(f"  {status_icon} [{bar}] {pct:>5.0f}%  {sid}")
        return
    except Exception:
        pass

    # Fallback: check checkpoints on disk
    done_count = 0
    running_count = 0
    pending_count = 0

    for path in paths:
        sid = Path(path).name
        found = False
        for sd in SOMA_PKG.glob("*/"):
            cp = sd / "checkpoints" / f"{sid}.chunks.checkpoint.json"
            if not cp.exists():
                cp = sd / "checkpoints" / f"{sid}.checkpoint.json"
            if cp.exists():
                found = True
                try:
                    cp_data = json.loads(cp.read_text())
                    cs = cp_data.get("chatsets", {})
                    done_sets = sum(
                        1 for c in cs.values()
                        if all(
                            st.get("status") in ("DONE", "SKIPPED")
                            for st in c.get("stages", {}).values()
                        )
                        for _ in [1]
                    )
                    total_sets = len(cs)
                    if done_sets == total_sets and total_sets > 0:
                        done_count += 1
                    elif total_sets > 0:
                        running_count += 1
                    else:
                        pending_count += 1
                except Exception:
                    pending_count += 1
                break

        if not found:
            pending_count += 1

    total = done_count + running_count + pending_count
    pct = (done_count / total * 100) if total > 0 else 0

    print(f"  Done:   {done_count}")
    print(f"  Running: {running_count}")
    print(f"  Pending: {pending_count}")
    print(f"  Overall: {pct:.1f}% complete\n")

    print(f"  Sessions:")
    for i, path in enumerate(paths[:20]):
        sid = Path(path).name
        meta = load_session_info(path)
        title = meta.get("title", sid)
        sets = meta.get("total_sets", "?")
        print(f"    [{i+1}] {title} ({sets} sets)")
    if len(paths) > 20:
        print(f"    ... and {len(paths) - 20} more")


def start_dashboard_only():
    """Start the debug dashboard server only."""
    ensure_debug_dir()
    proc = start_dashboard()
    print(f"\n  Dashboard running at http://127.0.0.1:{PORT}")
    print("  Press Ctrl+C to stop.\n")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


def start_picker() -> subprocess.Popen | None:
    """Launch the session picker server, wait until reachable, open browser.

    Runs soma-session-picker/start.sh in the background, polls
    http://localhost:5174 until it returns 200 (or 30 s), prints the URL,
    opens the browser, and returns the process handle.
    """
    picker_script = Path(__file__).parent / "soma-session-picker" / "start.sh"
    if not picker_script.exists():
        print(f"  ERROR: Picker script not found at {picker_script}")
        return None

    env = os.environ.copy()
    env["SESSIONS_PATH"] = str(_sessions_dir())
    env["QUEUE_OUTPUT_PATH"] = str(Path(__file__).parent / QUEUE_FILE)

    print(f"\n  Launching session picker from {picker_script}")
    proc = subprocess.Popen(
        ["bash", str(picker_script)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    url = "http://localhost:5174"
    deadline = time.monotonic() + 30
    print(f"  Waiting for picker at {url}...")
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            if resp.getcode() == 200:
                print(f"  Picker is ready at {url}")
                webbrowser.open(url)
                print(f"  Process PID: {proc.pid}")
                return proc
        except Exception:
            pass
        time.sleep(0.5)

    print(f"  ERROR: Picker did not become ready within 30 s")
    proc.terminate()
    return None


def prompt_for_json_path() -> str:
    """Prompt the user for a JSON file path after the picker is ready.

    Validates that the file exists and contains valid JSON.
    Re-prompts on invalid input, missing file, or empty input.
    """
    while True:
        path = input("  Enter the path to the exported JSON file: ").strip()
        if not path:
            print("  Empty path. Please enter a valid JSON file path.")
            continue
        qpath = Path(path)
        if not qpath.exists():
            print(f"  File not found: {path}")
            continue
        try:
            json.loads(qpath.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Invalid JSON ({e}). Please enter a valid file.")
            continue
        return path


# ── Base Path Management ─────────────────────────────────────────────

def _load_base_path() -> str | None:
    """Load the project base path from data/.base_path.

    Returns None if the file does not exist or is empty.
    """
    if not BASE_PATH_FILE.exists():
        return None
    try:
        path = BASE_PATH_FILE.read_text().strip()
        if path:
            return path
    except OSError:
        pass
    return None


def _save_base_path(path: str) -> None:
    """Persist the project base path to data/.base_path."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BASE_PATH_FILE.write_text(path + "\n")


def _prompt_and_save_base_path() -> str:
    """On first run, prompt the user for the project base path.

    Default suggestion is the directory containing run_sessions.py.
    Accepts the default (Enter) or a custom path.
    """
    default = str(SOMA_DIR)
    print()
    print(f"  Project base path not configured.")
    print(f"  Default: {default}")
    while True:
        raw = input(f"  Base path [{default}]: ").strip()
        if not raw:
            path = default
            break
        candidate = Path(raw)
        if not candidate.exists():
            print(f"  Path does not exist: {raw}")
            continue
        path = str(candidate.resolve())
        break
    _save_base_path(path)
    print(f"  → Base path saved: {path}")
    return path


def _get_base_dir() -> Path:
    """Resolve the project base directory.

    Reads from data/.base_path if available; otherwise falls back
    to SOMA_DIR (directory of run_sessions.py).
    """
    bp = _load_base_path()
    if bp:
        return Path(bp).resolve()
    return SOMA_DIR


# ── Launcher Helpers ─────────────────────────────────────────────────

def _get_last_state() -> dict:
    """Load last-used values from data/.last_* files."""
    state = {}
    for key in ("profile", "queue", "workers"):
        p = DATA_DIR / f".last_{key}"
        if p.exists():
            state[key] = p.read_text().strip()
    return state


def _save_state(state: dict) -> None:
    """Persist last-used values to data/.last_* files."""
    for key in ("profile", "queue", "workers"):
        if key in state:
            p = DATA_DIR / f".last_{key}"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(state[key] + "\n")


def _profile_model_count(path: Path) -> str:
    """Best-effort count of deck cards in a profile JSON, for the picker list."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return "?"
    if not isinstance(data, dict):
        return "?"
    deck = data.get("deck") or data.get("attempts")
    if isinstance(deck, list):
        return f"{len(deck)} models"
    pinned = data.get("pinnedModels") or data.get("pinned_models")
    if isinstance(pinned, dict):
        return f"{sum(len(v) for v in pinned.values() if isinstance(v, list))} pinned"
    return "0 models"


def _discover_profiles() -> list[dict]:
    """Discover TurnKey profile JSON files from <base>/turn-key/.

    Returns list of {"path", "name", "detail"} dicts sorted by name, where
    detail carries the model count for the picker list.
    """
    profiles = []
    seen = set()
    profile_dir = _get_base_dir() / "turn-key"
    if profile_dir.exists():
        for p in sorted(profile_dir.glob("*.json")):
            if p.name not in seen and p.suffix == ".json":
                seen.add(p.name)
                profiles.append({"path": str(p), "name": p.name,
                                 "detail": _profile_model_count(p)})
    # Also check legacy location
    legacy_dir = SOMA_DIR / "soma" / "turn-key-profiles"
    if legacy_dir.exists():
        for p in sorted(legacy_dir.glob("*profile*.json")):
            if p.name not in seen and p.suffix == ".json":
                seen.add(p.name)
                profiles.append({"path": str(p), "name": p.name})
    return profiles


def _launch_profile_builder() -> None:
    """Launch the profile builder server and block until it exits.

    Starts the Solid launcher, opens the browser, and waits for
    the server process to terminate.
    """
    builder_script = Path("/home/flintx/soma-nov/turn-key/profile_builder/solid/launch.sh")
    if not builder_script.exists():
        print(f"  ERROR: Profile builder not found at {builder_script}")
        return

    print(f"\n  Launching profile builder from {builder_script}")
    proc = subprocess.Popen(
        ["bash", str(builder_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    url = "http://127.0.0.1:5173"
    deadline = time.monotonic() + 30
    print(f"  Waiting for builder at {url}...")
    ready = False
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            if resp.getcode() == 200:
                print(f"  Builder is ready at {url}")
                webbrowser.open(url)
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ready:
        print(f"  Builder did not become ready within 30 s")
        proc.terminate()
        proc.wait()

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n  Profile builder interrupted.")
        proc.terminate()


def _select_profile(state: dict) -> str:
    """Step 1: Interactive profile selection with last-used highlighting."""
    profiles = _discover_profiles()
    if not profiles:
        print("\n  No TurnKey profiles found.")
        answer = input("  Open the profile builder? [y/N]: ").strip().lower()
        if answer in ("y", "yes"):
            _launch_profile_builder()
            profiles = _discover_profiles()
            if not profiles:
                print("\n  No profiles found after builder closed. Exiting.")
                sys.exit(1)
        else:
            print("\n  No profiles. Exiting.")
            sys.exit(1)

    last = state.get("profile")
    last_idx = next(
        (i for i, p in enumerate(profiles) if p["path"] == last),
        None,
    )

    print("\n  ═══════════════════════════════════════════════════════")
    print("  Step 1/4 — TurnKey Profile")
    print("  ═══════════════════════════════════════════════════════")
    print()
    print("  Profiles:")
    for i, p in enumerate(profiles, 1):
        marker = " (last used)" if last_idx is not None and i == (last_idx + 1) else ""
        detail = f"  ·  {p['detail']}" if p.get("detail") else ""
        print(f"    [{i}] {p['name']}{detail}{marker}")
    print(f"    [n] New path…")
    print()

    while True:
        raw = input(f"  Select profile [{last_idx + 1 if last_idx is not None else 'n'}]: ").strip()
        if not raw:
            if last_idx is not None:
                path = profiles[last_idx]["path"]
                print(f"  → {profiles[last_idx]['name']}")
                return path
            print("  Please enter a selection.")
            continue
        if raw.lower() == "n":
            path = input("  Profile path: ").strip()
            if not path:
                print("  Empty path. Please enter a valid path or a number.")
                continue
            return path
        try:
            num = int(raw)
            if 1 <= num <= len(profiles):
                path = profiles[num - 1]["path"]
                print(f"  → {profiles[num - 1]['name']}")
                return path
        except ValueError:
            pass
        print(f"  Invalid. Enter 1-{len(profiles)}, 'n' for a new path.")


def _queue_session_count(path: Path) -> str:
    """Best-effort count of sessions in a queue/export JSON, for the picker."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return "?"
    if isinstance(data, list):
        return f"{len(data)} sessions"
    if isinstance(data, dict):
        sel = data.get("selected")
        if isinstance(sel, list):
            return f"{len(sel)} sessions"
        return "0 sessions"
    return "?"


def _select_queue(state: dict) -> tuple[str, list[dict] | None]:
    """Step 2: Interactive queue/session selection.

    Reads *.json from <base>/sessions/.
    Returns (queue_file_path, full_objects | None).
    """
    last_queue = state.get("queue")
    queue_dir = _get_base_dir() / "sessions"
    queue_files = sorted(queue_dir.glob("*.json")) if queue_dir.exists() else []

    print("\n  ═══════════════════════════════════════════════════════")
    print("  Step 2/4 — Sessions / Queue")
    print("  ═══════════════════════════════════════════════════════")
    print()

    if queue_files:
        print("  Available queues:")
        for i, qf in enumerate(queue_files, 1):
            marker = " (last used)" if last_queue and qf.name == Path(last_queue).name else ""
            print(f"    [{i}] {qf.name}  ·  {_queue_session_count(qf)}{marker}")
    else:
        print("  No session JSONs found in sessions/.")

    print(f"    [p] Open web session picker…")
    print(f"    [s] Skip (will use defaults)")
    print()

    # ── Picker loop: re-scan after picker closes ───────────────────────
    while True:
        if not queue_files:
            answer = input("  Open the web session picker? [y/N]: ").strip().lower()
            if answer in ("y", "yes"):
                picker_env = os.environ.copy()
                picker_env["SESSIONS_PATH"] = "/media/storage/dendritic123/chat-logs/sessions"
                picker_env["QUEUE_OUTPUT_PATH"] = str(SOMA_DIR / QUEUE_FILE)
                picker_script = SOMA_DIR / "soma-session-picker" / "start.sh"
                subprocess.run(["bash", str(picker_script)], cwd=str(picker_script.parent), env=picker_env)
                queue_files = sorted(queue_dir.glob("*.json")) if queue_dir.exists() else []
                if not queue_files:
                    print("  No session JSONs after picker. Returning.")
                    return "skip", None
                print("  Session JSONs found after picker.")
            else:
                return "skip", None

        raw = input(f"  Select queue [{last_queue if last_queue else 'p'}]: ").strip()

        # Reuse last queue
        if not raw and last_queue and Path(last_queue).exists():
            name = Path(last_queue).name
            print(f"  → {name}")
            result = load_queue(last_queue)
            full_objs = result[1] if result else []
            return last_queue, full_objs if full_objs else None

        # Numbered selection
        if not raw and queue_files:
            print("  Please enter a number, 'p', or 's'.")
            continue
        try:
            num = int(raw)
            if 1 <= num <= len(queue_files):
                qf = queue_files[num - 1]
                name = qf.name
                print(f"  → {name}")
                result = load_queue(str(qf))
                full_objs = result[1] if result else []
                return str(qf), full_objs if full_objs else None
        except ValueError:
            pass

        if raw.lower() == "p":
            return "web_picker", None
        if raw.lower() == "s":
            return "skip", None

        print(f"  Invalid. Enter 1-{len(queue_files)}, 'p' for picker, 's' to skip.")


def _select_workers(state: dict) -> int:
    """Step 3: Worker count selection."""
    last_workers = state.get("workers")
    default = int(last_workers) if last_workers and last_workers.isdigit() else 2

    print("\n  ═══════════════════════════════════════════════════════")
    print("  Step 3/4 — Workers")
    print("  ═══════════════════════════════════════════════════════")
    print()
    print(f"  How many parallel workers? (default: {default})")

    while True:
        raw = input(f"  Workers [{default}]: ").strip()
        if not raw:
            print(f"  → {default} workers")
            return default
        try:
            n = int(raw)
            if n < 1:
                print("  Please enter a number >= 1.")
                continue
            print(f"  → {n} workers")
            return n
        except ValueError:
            print("  Invalid. Enter a number >= 1.")


def run_launcher(granular: bool = False, force: bool = False) -> None:
    """4-step interactive launcher.

    Step 1 — Pick TurnKey profile
    Step 2 — Pick sessions / queue
    Step 3 — Pick worker count
    Step 4 — Summary + 3-2-1 countdown → run_pipeline()
    """
    state = _get_last_state()

    if granular:
        print("\n  [Step: profile selection]")
        print("  Enter to continue, Ctrl+C to skip")
        try: input()
        except EOFError: pass

    # Step 1: Profile
    profile = _select_profile(state)
    state["profile"] = profile

    if granular:
        print("\n  [Step: queue/session selection]")
        print("  Enter to continue, Ctrl+C to skip")
        try: input()
        except EOFError: pass

    # Step 2: Sessions / Queue
    queue_file, full_objects = _select_queue(state)
    state["queue"] = queue_file

    # Collect sessions early (needed for summary)
    session_ids = []
    paths = []
    if queue_file == "web_picker":
        run_web_picker_and_pipeline(QUEUE_FILE, granular=granular, force=force)
        return
    if queue_file != "skip":
        result = load_queue(queue_file)
        if result is None:
            print("  ERROR: Cannot load queue.")
            return
        session_ids, _ = result
        paths = resolve_sessions_from_ids(session_ids)
    else:
        paths = []

    # Step 3: Workers
    if granular:
        print("\n  [Step: workers]")
        print("  Enter to continue, Ctrl+C to skip")
        try: input()
        except EOFError: pass
    workers = _select_workers(state)
    state["workers"] = str(workers)

    _save_state(state)
    if not paths:
        print("  No sessions to run. Provide a queue file or use the session picker.")
        return
    launch_sessions(paths, full_objects if full_objects else [], profile=profile,
                    workers=workers, granular=granular, force=force)


# ── CLI Session Picker ───────────────────────────────────────────────

def _parse_selection_input(user_input: str, session_count: int) -> list[int] | None:
    """Parse user selection into a list of 1-indexed session numbers.

    Accepted formats:
        "all"       → all sessions
        "1,3,5"     → sessions 1, 3, 5
        "1-10"      → sessions 1 through 10
        "1"         → session 1 (single number)

    Returns None if the input is empty, malformed, or selects out-of-range numbers.
    """
    raw = user_input.strip()
    if not raw:
        return None

    if raw.lower() == "all":
        return list(range(1, session_count + 1))

    if "-" in raw and "," not in raw:
        # Range, e.g. "1-10"
        parts = raw.split("-")
        if len(parts) != 2:
            return None
        try:
            start = int(parts[0].strip())
            end = int(parts[1].strip())
        except ValueError:
            return None
        if start > end:
            return None
        nums = list(range(start, end + 1))
    else:
        # Comma-separated or single number, e.g. "1,3,5" or "1"
        parts = [p.strip() for p in raw.split(",")]
        try:
            nums = [int(p) for p in parts if p]
        except ValueError:
            return None

    # Validate all numbers are within [1, session_count]
    if not nums or any(n < 1 or n > session_count for n in nums):
        return None
    return nums


def load_and_pick_sessions(queue_path: str) -> list[str] | None:
    """Load sessions from a JSON queue, let the user pick which to run.

    Loads the JSON from *queue_path*, prints the session list with numbers,
    prompts for a selection, parses the input, and prints the final selection.

    Parameters
    ----------
    queue_path : str
        Path to a queue JSON (format: {"selected": ["/path/...", ...], ...})
        or full export JSON array.

    Returns
    -------
    list[str] | None
        Selected session IDs (from the JSON), or None on invalid input.
    """
    print("\n  ═══════════════════════════════════════════════════════")
    print(f"  CLI Session Picker — {queue_path}")
    print("  ═══════════════════════════════════════════════════════\n")

    result = load_queue(queue_path)
    if result is None:
        return None

    session_ids, full_objects = result
    all_paths = resolve_sessions_from_ids(session_ids)

    if not all_paths:
        print("  No sessions found on disk.")
        return None

    session_count = len(all_paths)
    print(f"  Total sessions loaded: {session_count}\n")
    print("  Sessions:")

    # Build display entries with metadata
    names = []
    sets_info = []
    for p in all_paths:
        meta = load_session_info(p)
        title = meta.get("title") or meta.get("name") or ""
        if not title:
            title = Path(p).name
        names.append(title)
        idx = all_paths.index(p)
        sets_info.append(full_objects[idx].get("total_sets", "?") if full_objects else "?")

    for i, (name, total_sets) in enumerate(zip(names, sets_info), start=1):
        display_name = name[:60] + "..." if len(name) > 60 else name
        sets_str = f" ({total_sets} sets)" if total_sets else ""
        print(f"    [{i:>3}] {display_name}{sets_str}")

    print()

    # Interactive selection loop
    while True:
        user_input = input("  Enter numbers (1,3,5), a range (1-10), or 'all': ").strip()
        nums = _parse_selection_input(user_input, session_count)
        if nums is None:
            print("  Invalid input. Enter numbers (1,3,5), a range (1-10), or 'all'.\n")
            continue
        break

    selected_ids = [session_ids[i - 1] for i in nums]

    print(f"\n  ✓ {len(selected_ids)} session(s) selected:")
    for sid in selected_ids:
        print(f"    - {sid}")
    print()

    return selected_ids


def run_web_picker_and_pipeline(queue_path: str, granular: bool = False,
                                force: bool = False):
    """Launch the web session picker, let user pick sessions, then run pipeline."""
    import os

    picker_script = Path(__file__).parent / "soma-session-picker" / "start.sh"
    if not picker_script.exists():
        print(f"  ERROR: Web picker not found at {picker_script}")
        return

    if granular:
        print("\n  [Step: web session picker. Enter to run, Ctrl+C to skip]")
        print("  Close the browser or press Ctrl+C when done picking.")
        try: input()
        except EOFError: pass

    print(f"\n  ═══════════════════════════════════════════════════════")
    print(f"  Starting Web Session Picker...")
    print(f"  ═══════════════════════════════════════════════════════")
    print(f"\n  Frontend: http://localhost:5174")
    print(f"  Backend:  http://localhost:8001")
    print(f"\n  1. Pick your favorite sessions in the browser")
    print(f"  2. Click 'Export N Favorites'")
    print(f"  3. Close the browser or press Ctrl+C when done")
    print(f"\n  Selections will be saved to: {queue_path}\n")

    # Launch the web picker (blocking — waits for Ctrl+C or completion)
    picker_env = os.environ.copy()
    picker_env["SESSIONS_PATH"] = "/media/storage/dendritic123/chat-logs/sessions"
    picker_env["QUEUE_OUTPUT_PATH"] = queue_path

    try:
        subprocess.run(
            [str(picker_script)],
            cwd=str(picker_script.parent),
            env=picker_env,
        )
    except KeyboardInterrupt:
        print("\n  Picker interrupted.")
        return

    # After picker exits, check if a queue file was written
    if not Path(queue_path).exists():
        print(f"\n  No queue file written at {queue_path}")
        print("  Make sure you clicked 'Export N Favorites' in the picker.")
        return

    # Load the queue
    result = load_queue(queue_path)
    if result is None:
        return
    session_ids, full_objects = result
    paths = resolve_sessions_from_ids(session_ids)

    if not paths:
        print(f"\n  No sessions resolved from {queue_path}")
        return

    print(f"\n  \u2713 {len(paths)} sessions selected from web picker")
    print(f"  Queue: {queue_path}")

    launch_sessions(paths, full_objects if full_objects else [],
                    granular=granular, force=force)


def poll_for_launch_config(timeout=None) -> dict:
    """Block until soma_launch.json exists, then read and return it.

    Polls every 0.5 seconds. If timeout is None, polls indefinitely.
    Returns the parsed JSON dict.
    Exits with error on malformed JSON.
    """
    print(f"\n  Waiting for launch config: {LAUNCH_CONFIG}")
    deadline = None
    if timeout is not None:
        deadline = time.monotonic() + timeout

    while True:
        if deadline and time.monotonic() > deadline:
            print(f"  ERROR: Timed out waiting for {LAUNCH_CONFIG}")
            sys.exit(1)
        if LAUNCH_CONFIG.exists():
            break
        time.sleep(0.5)

    try:
        data = json.loads(LAUNCH_CONFIG.read_text())
    except json.JSONDecodeError as e:
        print(f"  ERROR: Malformed launch config at {LAUNCH_CONFIG}: {e}")
        sys.exit(1)
    except OSError as e:
        print(f"  ERROR: Cannot read launch config at {LAUNCH_CONFIG}: {e}")
        sys.exit(1)

    if not isinstance(data, dict):
        print(f"  ERROR: Launch config must be a JSON object")
        sys.exit(1)
    return data


def select_sessions_from_config(config: dict) -> list[str]:
    """Load a queue file and return the mode-filtered subset of session paths.

    Config keys:
      - json_path: path to a queue/export JSON (relative to project root)
      - mode:      "all", "one", or "few"
      - count:     number of sessions for "few" mode (max 3)
    """
    queue_path = config.get("json_path")
    if not queue_path:
        print("  ERROR: Launch config missing 'json_path'")
        sys.exit(1)

    mode = config.get("mode", "all")
    if mode not in ("all", "one", "few"):
        print(f"  ERROR: Unknown launch mode '{mode}' (must be all/one/few)")
        sys.exit(1)

    count = config.get("count", 1)
    if not isinstance(count, int) or count < 1:
        count = 1

    # Load the queue
    result = load_queue(queue_path)
    if result is None:
        print(f"  ERROR: Cannot load queue from {queue_path}")
        sys.exit(1)

    session_ids, full_objects = result
    all_paths = resolve_sessions_from_ids(session_ids)

    if not all_paths:
        print("  ERROR: No sessions resolved from queue")
        sys.exit(1)

    # Filter by mode
    if mode == "all":
        paths = all_paths
    elif mode == "one":
        paths = [all_paths[0]]
    elif mode == "few":
        n = min(count, 3, len(all_paths))
        paths = all_paths[:n]
    else:
        paths = all_paths

    return paths, full_objects if full_objects else []


def _chatset_numbers(path: str) -> list[int]:
    """The chat set numbers of one session on disk."""
    try:
        sys.path.insert(0, str(SOMA_PKG))
        from machinetalk.source import load_session
        return [cs.set_number for cs in load_session(Path(path)).chatsets]
    except Exception:
        return []


SOMA_OUT = SOMA_DIR / "soma" / "output"       # where run_pipeline sends --out


def _somas_dir() -> Path:
    """Where the engine keeps each session's Soma (SOMA_SOMAS_DIR moves it)."""
    return Path(os.environ.get("SOMA_SOMAS_DIR") or SOMA_DIR / "somas")


def _prompt_chatsets(numbers: list[int]) -> list[int] | None:
    """All the session's chat sets (None), the first N of them, or the ones named.

    Whatever is chosen runs through every stage, the four beavers and then the
    build. The default is all of them. ``set 2`` (or ``set 2,3``) runs just those
    chat sets: the first chat set of a session is often too short to carry any
    signal, so it is not always the right one to test with.
    """
    if len(numbers) < 2:
        return None
    nums = sorted(numbers)
    print(f"\n  This session has {len(nums)} chat sets (numbers {nums[0]}-{nums[-1]}). "
          f"Every chat set you run goes through all twelve stages.")
    while True:
        raw = input(f"  How many chat sets? [a]ll {len(nums)} (default), a number for the first N, "
                    f"or 'set 2' for just that one: ").strip().lower()
        if raw in ("", "a", "all"):
            return None
        if raw.startswith("set"):
            picked = [p.strip() for p in raw[3:].replace(" ", ",").split(",") if p.strip()]
            if picked and all(p.isdigit() for p in picked):
                chosen = sorted({int(p) for p in picked})
                if all(n in nums for n in chosen):
                    return chosen
                print(f"  This session has chat sets {nums[0]}-{nums[-1]} only.")
                continue
        elif raw.isdigit() and int(raw) >= 1:
            n = int(raw)
            return None if n >= len(nums) else nums[:n]
        print(f"  Enter 'a' for all, a number from 1 to {len(nums)}, or 'set N' for one chat set.")


def _pick_build_stage(status) -> str | None:
    """Choose which build stage to start from. The default is the first one not done."""
    from machinetalk import run_status as rs
    default = next((i for i, s in enumerate(rs.BUILD, 1)
                    if status.build_status(s) != "DONE"), 1)
    print("\n  Start from which build stage? It and every stage after it run again.")
    for i, stage in enumerate(rs.BUILD, 1):
        st = status.build_status(stage)
        print(f"    [{i}] {stage:<8} ({rs._WORD[st]})")
    while True:
        raw = input(f"  Stage [{default}] (or q): ").strip().lower()
        if raw in ("q", "quit"):
            return None
        raw = raw or str(default)
        if raw.isdigit() and 1 <= int(raw) <= len(rs.BUILD):
            return rs.BUILD[int(raw) - 1]
        print(f"  Enter 1-{len(rs.BUILD)}.")


def choose_start(path: str, ask: bool = True) -> dict | None:
    """Show what this session's last run left behind and ask where to start.

    Returns ``{"mode": "all" | "resume" | "from", "stage": str | None, "status": ...}``,
    or None when the person quits. A session that never ran is not asked anything:
    there is only one way to start it. ``ask=False`` always starts all over.
    """
    sys.path.insert(0, str(SOMA_PKG))
    from machinetalk import run_status as rs
    sid = Path(path).name
    status = rs.read_status(SOMA_OUT, sid, somas_dir=_somas_dir(),
                            total_sets=_chatset_numbers(path))
    print()
    print(rs.render(status))
    opts = rs.start_options(status)
    if len(opts) == 1 or not ask:
        if rs.nothing_done(status):
            print("\n  Starting from the beginning: every stage.")
        elif len(opts) == 1:
            print("\n  The earlier run cannot be resumed, so this starts all over "
                  "(the old Soma and checkpoint are kept as .bak-<time>).")
        return {"mode": "all", "stage": None, "status": status,
                "chatsets": None if not ask else _prompt_chatsets(_chatset_numbers(path))}
    print("\n  Where do you want to start?")
    for i, opt in enumerate(opts, 1):
        print(f"    [{i}] {opt.label}" + ("   (default)" if i == 1 else ""))
        print(f"        {opt.detail}")
    print("    [q] Quit")
    print("  (The four beaver stages run as a chain per chat set, so they are redone per "
          "chat set or all together, not one stage at a time.)")
    while True:
        raw = input("  Choose [1]: ").strip().lower()
        if raw in ("q", "quit"):
            return None
        raw = raw or "1"
        if raw.isdigit() and 1 <= int(raw) <= len(opts):
            break
        print(f"  Enter 1-{len(opts)}, or q.")
    opt = opts[int(raw) - 1]
    stage = None
    if opt.key == "from":
        stage = _pick_build_stage(status)
        if stage is None:
            return None
    chatsets = _prompt_chatsets(_chatset_numbers(path)) if opt.key == "all" else None
    return {"mode": opt.key, "stage": stage, "status": status, "chatsets": chatsets}


def _apply_start(choice: dict) -> None:
    """Make the checkpoint and Soma ready for the start that was chosen."""
    from machinetalk import run_status as rs
    status = choice["status"]
    if choice["mode"] == "resume":
        done = rs.prepare_resume(SOMA_OUT, status)
    elif choice["mode"] == "from":
        done = rs.prepare_from(SOMA_OUT, status, choice["stage"])
    else:
        return
    for line in done:
        print(f"  reset: {line}")


def launch_sessions(paths: list[str], full_objects: list[dict] | None = None, *,
                    profile: str | None = None, workers: int | None = None,
                    granular: bool = False, force: bool = False, ask: bool = True) -> None:
    """Run each selected session in full, one after the other.

    Each session is shown first (which stages are done) and the person says where
    to start. Nothing here limits a run to a few chat sets or to one stage: a run
    is every chat set through all four beavers, then the whole build.
    """
    for i, path in enumerate(paths, start=1):
        sid = Path(path).name
        print(f"\n  ═══════════════════════════════════════════════════════")
        print(f"  Session {i} of {len(paths)}")
        print(f"  ═══════════════════════════════════════════════════════")
        choice = choose_start(path, ask=ask)
        if choice is None:
            print("  Aborted.")
            return
        mode, stage = choice["mode"], choice["stage"]
        nums = choice.get("chatsets")
        only = [(sid, n) for n in nums] if nums else None
        if ask:
            what = {"all": "start all over: every stage of every chat set",
                    "resume": "resume: redo the unfinished chat sets, then the whole build",
                    "from": f"start from build stage {stage}"}[mode]
            print(f"\n  Profile:  {os.path.basename(profile) if profile else '(default)'}")
            print(f"  Session:  {sid}")
            print(f"  Start:    {what}")
            if only:
                print(f"  Chat sets: {len(only)} (set {', '.join(str(n) for _, n in only)}), "
                      f"each through all twelve stages")
            else:
                print("  Chat sets: all")
            if workers is not None:
                print(f"  Workers:  {workers}")
            print(f"  Dashboard: http://127.0.0.1:{PORT}\n")
            if input("  Launch? [Y/n]: ").strip().lower() in ("n", "no"):
                print("  Aborted.")
                return
            print("\n  Launching...")
            for n in (3, 2, 1):
                print(f"  {n}…")
                time.sleep(0.5)
        _apply_start(choice)
        run_pipeline([path], full_objects or [], profile=profile, workers=workers,
                     granular=granular, force=force,
                     stage=stage if mode == "from" else None,
                     resume=(mode == "resume"), only=only)


def main():
    parser = argparse.ArgumentParser(
        description="SOMA Session Batch Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_sessions.py                    # Launch web picker → run pipeline
  python run_sessions.py --queue q.json     # Use existing JSON, skip picker
  python run_sessions.py --progress         # Show queue progress
  python run_sessions.py --dashboard        # Start dashboard only
  python run_sessions.py --all              # Process ALL sessions on disk
  python run_sessions.py --cli-picker       # CLI prompt to pick from JSON
  python run_sessions.py --profile PROF     # Specify profile explicitly
        """,
    )
    parser.add_argument("--queue", "-q", default=QUEUE_FILE,
                        help="Queue or export JSON file (default: data/soma_queue.json)")
    parser.add_argument("--run-only", action="store_true",
                        help="Skip picker, use --queue directly")
    parser.add_argument("--progress", action="store_true",
                        help="Show progress only, don't run")
    parser.add_argument("--dashboard", action="store_true",
                        help="Start dashboard only")
    parser.add_argument("--all", action="store_true",
                        help="Process all sessions on disk")
    parser.add_argument("--cli-picker", action="store_true",
                        help="Interactively pick sessions from queue JSON via CLI")
    parser.add_argument("--port", type=int, default=PORT,
                        help="Dashboard port (default: 9877)")
    parser.add_argument("--profile", type=str, default=None,
                        help="TurnKey profile path")
    parser.add_argument("--step-mode", action="store_true",
                        help="Pause after each stage, waiting for Enter to continue")
    parser.add_argument("--stage", default=None,
                        help="Run only one stage: distiller, introbeaver, planbeaver, outrobeaver")
    parser.add_argument("--granular", action="store_true",
                        help="Pause before every single step: profile, queue, sessions, chatsets, workers, "
                             "dashboard, each beaver, each build stage, codebase, manifest")
    parser.add_argument("--force", action="store_true",
                        help="Re-run stages even when the checkpoint says DONE")
    parser.add_argument("--only", action="append", default=None,
                        metavar="SESSION:SET",
                        help="Run a single chatset, e.g. --only trevino.war.room:6 "
                             "(repeatable). Pairs with --stage to inspect one "
                             "stage of one chatset.")
    parser.add_argument("--max-chatsets", type=int, default=None,
                        help="Stop after this many chatsets")
    parser.add_argument("--workers", type=int, default=None,
                        help="Parallel chatset workers (default 2)")

    args = parser.parse_args()

    if args.dashboard:
        start_dashboard_only()
        return

    if args.progress:
        show_progress()
        return

    # --only: inspect one chatset directly, skipping every interactive prompt.
    # This is the path used to verify a single stage's telemetry before
    # running the rest of the pipeline.
    if args.only:
        result = load_queue(args.queue)
        if result is None:
            return
        session_ids, full_objects = result
        paths = resolve_sessions_from_ids(session_ids)
        if not paths:
            print("  No sessions resolved on disk.")
            return
        run_pipeline(
            paths, full_objects if full_objects else [],
            max_chatsets=args.max_chatsets,
            workers=args.workers,
            granular=args.granular,
            force=args.force,
            profile=args.profile,
            only=args.only,
            stage=args.stage,
        )
        return

    # --run-only: skip picker, use queue directly
    if args.run_only:
        result = load_queue(args.queue)
        if result is None:
            return
        session_ids, full_objects = result
        paths = resolve_sessions_from_ids(session_ids)
        if not paths:
            print("  No sessions resolved on disk.")
            return
        launch_sessions(paths, full_objects if full_objects else [], profile=args.profile,
                        workers=args.workers, granular=args.granular, force=args.force)
        return

    # --all: scan all sessions, skip picker
    if args.all:
        from soma.machinetalk.session_picker import scan_sessions
        sessions = scan_sessions()
        paths = [s.path for s in sessions]
        if not paths:
            print("  No sessions found!")
            return
        queue_data = {
            "selected": paths,
            "count": len(paths),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        qpath = Path(QUEUE_FILE)
        qpath.parent.mkdir(parents=True, exist_ok=True)
        qpath.write_text(json.dumps(queue_data, indent=2))
        print(f"  Queued {len(paths)} sessions for processing")
        launch_sessions(paths, [], profile=args.profile, workers=args.workers,
                        granular=args.granular, force=args.force, ask=False)
        return

    # --cli-picker: interactive CLI selection from queue JSON
    if args.cli_picker:
        selected_ids = load_and_pick_sessions(args.queue)
        if selected_ids is None:
            return
        paths = resolve_sessions_from_ids(selected_ids)
        if not paths:
            print("  No sessions resolved on disk.")
            return
        # We need full_objects for metadata — reload with load_queue
        result = load_queue(args.queue)
        _, full_objects = result if result else (None, [])
        launch_sessions(paths, full_objects if full_objects else [], profile=args.profile,
                        workers=args.workers, granular=args.granular, force=args.force)
        return

    # DEFAULT: run the 4-step interactive launcher
    run_launcher(granular=args.granular, force=args.force)


if __name__ == "__main__":
    main()
