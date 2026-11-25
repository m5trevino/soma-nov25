"""Results: what many runs of one model say about each stage.

Reads the trajectory logs the engine writes (``trajectories.jsonl``) and
answers the questions a many-runs experiment exists to answer: how often does
each stage pass the gate on this model, how it fails when it does not, what it
costs, and which tasks sit on the boundary (sometimes pass, sometimes fail),
which is where a model has something left to learn.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

STAGE_ORDER = ["distiller", "introbeaver", "planbeaver", "outrobeaver",
               "nexus", "spark", "falcon", "eagle", "drafter", "scout", "oc", "olc"]


def load(paths: Iterable[str | Path]) -> tuple[list[dict], list[dict]]:
    """``(stage_records, run_headers)`` from one or more trajectory files."""
    stages: list[dict] = []
    runs: list[dict] = []
    for path in paths:
        p = Path(path)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue                # a torn last line from a killed run
            (runs if rec.get("type") == "run" else stages).append(rec)
    return stages, runs


def error_kind(errors: list[str]) -> str:
    """The first error, cut down to its kind so failures can be counted.

    ``fabricated_quote (not in transcript: '...')`` and ``json_parse:Expecting
    value@3995`` are one kind each, whatever the text and offset.
    """
    if not errors:
        return ""
    first = str(errors[0])
    first = re.sub(r"^[A-Z0-9][\w.:-]*?-\d{4}: ", "", first)      # a TB004-...-0001: prefix
    first = re.sub(r"^\w+\[\d+\]: ", "", first)                  # a directives[2]: prefix
    m = re.match(r"([A-Za-z_]+(?::[A-Za-z_]+)?)", first)
    return m.group(1) if m else first[:40]


def pass_table(stages: list[dict]) -> list[dict]:
    """One row per (model, stage): calls, outcomes, rates, cost and top failures."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in stages:
        groups[(r.get("model_served") or r.get("model_requested") or "?",
                r.get("stage", "?"))].append(r)
    rows = []
    for (model, stage), recs in groups.items():
        n = len(recs)
        c = Counter(r.get("status") for r in recs)
        decided = c["pass"] + c["fail"] + c["error"]            # a skip is a decision, not an attempt
        fails = Counter(error_kind(r.get("errors") or []) for r in recs
                        if r.get("status") in ("fail", "error"))
        toks = [r.get("tokens", {}).get("total") or 0 for r in recs]
        lat = [r.get("latency_ms") or 0.0 for r in recs]
        rows.append({
            "model": model, "stage": stage, "calls": n,
            "pass": c["pass"], "skip": c["skip"], "fail": c["fail"], "error": c["error"],
            "pass_rate": round(c["pass"] / decided, 3) if decided else None,
            "rewrites": sum(r.get("write_rejections") or 0 for r in recs),
            "avg_tokens": round(sum(toks) / n) if n else 0,
            "avg_seconds": round(sum(lat) / n / 1000, 1) if n else 0.0,
            "top_failures": fails.most_common(3),
        })
    rows.sort(key=lambda r: (r["model"], STAGE_ORDER.index(r["stage"])
                             if r["stage"] in STAGE_ORDER else 99))
    return rows


def boundary_tasks(stages: list[dict], min_runs: int = 2) -> list[dict]:
    """Tasks that passed in some runs and failed in others.

    A task is one stage on one chat set. Always passing is mastered, always
    failing is out of reach; the ones in between are where a model can still
    improve, so they are the useful ones to train and evaluate on.
    """
    by_task: dict[tuple, list[str]] = defaultdict(list)
    for r in stages:
        if r.get("status") in ("pass", "fail", "error"):
            by_task[(r.get("session_id"), r.get("chatset_id"), r.get("stage"))].append(
                "pass" if r["status"] == "pass" else "fail")
    out = []
    for (session, chatset, stage), outcomes in by_task.items():
        if len(outcomes) >= min_runs and 0 < outcomes.count("pass") < len(outcomes):
            out.append({"session_id": session, "chatset_id": chatset, "stage": stage,
                        "runs": len(outcomes),
                        "pass_rate": round(outcomes.count("pass") / len(outcomes), 3)})
    out.sort(key=lambda t: (t["stage"], t["session_id"] or "", t["chatset_id"] or ""))
    return out


def run_summaries(stages: list[dict], runs: list[dict]) -> list[dict]:
    """Per run: which phases ran, and how far the build got."""
    by_run: dict[str, list[dict]] = defaultdict(list)
    for r in stages:
        by_run[r.get("run_id", "?")].append(r)
    headers: dict[str, list[dict]] = defaultdict(list)
    for h in runs:
        headers[h.get("run_id", "?")].append(h)
    out = []
    for run_id in sorted(set(by_run) | set(headers)):
        recs = by_run.get(run_id, [])
        olc = [r for r in recs if r.get("stage") == "olc"]
        out.append({
            "run_id": run_id,
            "phases": sorted({h.get("phase") for h in headers.get(run_id, [])}),
            "stage_calls": len(recs),
            "failures": sum(1 for r in recs if r.get("status") in ("fail", "error")),
            "built": bool(olc and olc[-1].get("status") == "pass"),
        })
    return out


def render(rows: list[dict]) -> str:
    """The pass table as text."""
    if not rows:
        return "no stage calls recorded"
    head = (f"{'model':<28} {'stage':<12} {'calls':>5} {'pass':>5} {'skip':>5} "
            f"{'fail':>5} {'err':>4} {'rate':>6} {'rewr':>5} {'tok':>7} {'sec':>6}  top failures")
    lines = [head, "-" * len(head)]
    for r in rows:
        rate = "  n/a" if r["pass_rate"] is None else f"{r['pass_rate'] * 100:5.1f}%"
        tops = ", ".join(f"{k or '?'}×{v}" for k, v in r["top_failures"])
        lines.append(f"{r['model'][:28]:<28} {r['stage']:<12} {r['calls']:>5} {r['pass']:>5} "
                     f"{r['skip']:>5} {r['fail']:>5} {r['error']:>4} {rate:>6} "
                     f"{r['rewrites']:>5} {r['avg_tokens']:>7} {r['avg_seconds']:>6}  {tops}")
    return "\n".join(lines)
