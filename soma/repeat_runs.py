#!/usr/bin/env python3
"""Run the pipeline many times on one model and report what it did.

Each job is one run of one session in its own lane: its own Soma, output and
log directory, so runs never share state. Both loops run for a job, extract and
then crystallize (crystallize is skipped when extract fails). When every job is
done the trajectory logs are aggregated into a pass table per model and stage,
and the tasks that sometimes pass and sometimes fail are listed.

    python3 repeat_runs.py --profile profile.json --source SESSION_DIR [--source ...] \
        --runs 5 --parallel 4 [--root DIR] [--max-chatsets N] [--phases extract]

The profile should pin every stage to the model under test (--require-pins
refuses to start otherwise), so the numbers are about one model.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from machinetalk import results  # noqa: E402
from machinetalk.client import pins_from_profile  # noqa: E402
from machinetalk.trajectory import new_run_id  # noqa: E402

PHASES = ("extract", "crystallize")
ALL_STAGES = results.STAGE_ORDER


def build_jobs(sources: list[str], runs: int, root: Path) -> list[dict]:
    """One job per (run, session). A lane is never reused."""
    jobs = []
    for r in range(1, runs + 1):
        for src in sources:
            lane = root / f"run-{r:03d}" / Path(src).name
            jobs.append({"run": r, "source": src, "lane": lane,
                         "run_id": f"{new_run_id()}-r{r:03d}"})
    return jobs


def phase_command(job: dict, phase: str, args, port: int) -> list[str]:
    cmd = [sys.executable, str(HERE / "run_engine.py"),
           "--phase", phase, "--source", job["source"], "--profile", args.profile,
           "--workers", str(args.workers), "--out", str(job["lane"] / "out"),
           "--debug", str(job["lane"] / "debug"), "--port", str(port),
           "--no-dashboard", "--run-id", job["run_id"]]
    if phase == "extract" and args.max_chatsets:
        cmd += ["--max-chatsets", str(args.max_chatsets)]
    return cmd


def lane_env(job: dict, args) -> dict:
    env = dict(os.environ)
    env["SOMA_SOMAS_DIR"] = str(job["lane"] / "somas")     # this lane's own Soma
    env["PYTHONPATH"] = os.pathsep.join(
        [str(HERE.parent / "turn-key"), str(HERE), env.get("PYTHONPATH", "")]).strip(os.pathsep)
    if args.engine_url:
        env["ENGINE_URL"] = args.engine_url
    return env


def classify(exit_code: int, log_text: str) -> str:
    """How a phase ended: ``ok``, ``gate_failures`` or ``crashed``.

    The engine exits 1 when any stage failed its gate, which is the normal
    outcome of testing a model and not a fault in the run. A crash is an
    exception, or a log that never reached the end of the phase.
    """
    if exit_code == 0:
        return "ok"
    if "Traceback (most recent call last)" in log_text:
        return "crashed"
    return "gate_failures" if exit_code == 1 else "crashed"


def run_job(job: dict, args, port: int, runner=subprocess.run) -> dict:
    """Run a job's phases in order.

    A phase that ended with gate failures does not stop the next one: the
    build stages are measured on whatever extraction produced, and each stage
    that cannot run says why (MISSING_SOMA_DATA). Only a crash stops a job, or
    ``--stop-on-failures`` for a strict run.
    """
    lane = job["lane"]
    if lane.exists():
        raise FileExistsError(f"{lane} already exists: a lane is never reused")
    lane.mkdir(parents=True)
    t0 = time.monotonic()
    codes: dict[str, int] = {}
    states: dict[str, str] = {}
    log_path = lane / "run.log"
    with open(log_path, "w") as log:
        for phase in args.phases:
            r = runner(phase_command(job, phase, args, port), env=lane_env(job, args),
                       stdout=log, stderr=subprocess.STDOUT, cwd=str(HERE),
                       timeout=args.timeout)
            log.flush()
            codes[phase] = r.returncode
            states[phase] = classify(r.returncode, log_path.read_text(errors="replace"))
            if states[phase] == "crashed" or (
                    states[phase] == "gate_failures" and getattr(args, "stop_on_failures", False)):
                break
    crashed = any(v == "crashed" for v in states.values())
    return {"run": job["run"], "source": Path(job["source"]).name,
            "run_id": job["run_id"], "exit": codes, "state": states,
            "ok": not crashed and len(states) == len(args.phases),
            "gate_failures": any(v == "gate_failures" for v in states.values()),
            "seconds": round(time.monotonic() - t0, 1)}


def check_pins(profile: str, require: bool) -> tuple[bool, str]:
    pins = pins_from_profile(profile)
    unpinned = [s for s in ALL_STAGES if s not in pins]
    models = sorted({m for v in pins.values() for m in v})
    if not pins:
        msg = "no stage is pinned: the deck will rotate, so results are not about one model"
    elif unpinned:
        msg = (f"pinned to {', '.join(models)}; NOT pinned: {', '.join(unpinned)}")
    else:
        msg = f"every stage pinned to {', '.join(models)}"
    return (not require) or (not unpinned and bool(pins)), msg


def report(root: Path, outcomes: list[dict]) -> str:
    stages, headers = results.load(sorted(root.glob("run-*/*/out/trajectories.jsonl")))
    rows = results.pass_table(stages)
    boundary = results.boundary_tasks(stages)
    summaries = results.run_summaries(stages, headers)
    crashed = [o for o in outcomes if not o["ok"]]
    text = [f"RESULTS  {len(outcomes)} job(s): {len(outcomes) - len(crashed)} ran to the end "
            f"({sum(1 for o in outcomes if o.get('gate_failures'))} with gate failures), "
            f"{len(crashed)} crashed",
            "", results.render(rows), ""]
    built = sum(1 for s in summaries if s["built"])
    text.append(f"builds that reached a passing olc: {built} of {len(summaries)} run(s)")
    text.append(f"boundary tasks (sometimes pass, sometimes fail): {len(boundary)}")
    for t in boundary[:15]:
        text.append(f"  {t['stage']:<12} {t['session_id']}  {t['chatset_id']}  "
                    f"{t['pass_rate'] * 100:.0f}% of {t['runs']}")
    (root / "results.json").write_text(json.dumps(
        {"outcomes": outcomes, "pass_table": rows, "boundary_tasks": boundary,
         "runs": summaries}, indent=2))
    (root / "report.txt").write_text("\n".join(text) + "\n")
    return "\n".join(text)


def main(argv=None, runner=subprocess.run) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--source", action="append", default=[], metavar="SESSION_DIR")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=1, help="jobs at once")
    ap.add_argument("--workers", type=int, default=1, help="chat sets at once inside a job")
    ap.add_argument("--root", default=None, help="where lanes and the report go")
    ap.add_argument("--max-chatsets", type=int, default=None,
                    help="extract only the first N chat sets of each session")
    ap.add_argument("--phases", default="extract,crystallize")
    ap.add_argument("--engine-url", default=None)
    ap.add_argument("--timeout", type=int, default=36000, help="seconds per phase")
    ap.add_argument("--port-base", type=int, default=9900)
    ap.add_argument("--stop-on-failures", action="store_true",
                    help="do not start crystallize when extraction had gate failures")
    ap.add_argument("--require-pins", action="store_true",
                    help="refuse to start unless every stage is pinned")
    args = ap.parse_args(argv)
    args.phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    bad = [p for p in args.phases if p not in PHASES]
    if bad or not args.phases:
        print(f"--phases must be some of {PHASES}", file=sys.stderr)
        return 2
    if not args.source:
        print("give at least one --source SESSION_DIR", file=sys.stderr)
        return 2
    ok, msg = check_pins(args.profile, args.require_pins)
    print(f"pins: {msg}")
    if not ok:
        print("refusing to start: --require-pins and a stage is not pinned", file=sys.stderr)
        return 2

    root = Path(args.root) if args.root else HERE / "runs" / time.strftime("%Y%m%d-%H%M%S")
    jobs = build_jobs(args.source, args.runs, root)
    print(f"{len(jobs)} job(s) → {root}  (parallel {args.parallel})", flush=True)
    outcomes: list[dict] = []

    def one(idx_job):
        idx, job = idx_job
        try:
            out = run_job(job, args, args.port_base + idx, runner=runner)
        except Exception as exc:                       # a lane that could not start
            out = {"run": job["run"], "source": Path(job["source"]).name,
                   "run_id": job["run_id"], "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        label = ("CRASHED" if not out["ok"]
                 else "ok, gate failures" if out.get("gate_failures") else "ok")
        print(f"  run {out['run']:03d}  {out['source']:<40} {label}  {out.get('seconds', '')}s",
              flush=True)
        return out

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        outcomes = list(pool.map(one, enumerate(jobs)))
    print()
    print(report(root, outcomes))
    return 0 if all(o["ok"] for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
