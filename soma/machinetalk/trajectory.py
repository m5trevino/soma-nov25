"""Trajectories: the whole tool conversation of every stage call, on disk.

A stage call is a conversation: the model reads its instructions, reads its
input, and writes, sometimes after the gate sent a write back. That
conversation is the evidence for what a model did and it is the training data
for what a model should do, so it is kept, one JSON line per stage call, with
the run it belongs to, the model that answered, the tokens each turn cost and
the verdict the gate gave.

Instructions and chat sets are large and already live in the Soma under a
hash, so by default a tool result that carries one is stored as a reference
(its name, its length and its SHA-256) and not as text. The model's own tool
calls and writes are stored in full. ``full=True`` keeps everything.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = "trajectory/1"

# Tool results that are re-derivable from the Soma by name and hash.
_REFERENCED = ("get_receptor_config", "get_chatset", "query_nodes")


def new_run_id() -> str:
    """``RUN-20261119-041203-a3f9c01e``: sortable, and unique across lanes.

    Four random bytes: two runs started in the same second (parallel lanes, two
    harnesses) must not share an id, and two bytes collide too often for that.
    """
    return time.strftime("RUN-%Y%m%d-%H%M%S-", time.gmtime()) + secrets.token_hex(4)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compact_messages(messages: list[dict], *, full: bool = False) -> list[dict]:
    """The conversation as stored: big tool results become references."""
    out = []
    for m in messages:
        m = dict(m)
        if (not full and m.get("role") == "tool" and m.get("name") in _REFERENCED
                and isinstance(m.get("content"), str)
                and not m["content"].startswith("error:")):
            body = m["content"]
            m["content"] = {"ref": m["name"], "chars": len(body), "sha256": _sha(body)}
        out.append(m)
    return out


def build_record(*, run_id: str, phase: str, stage: str, session_id: str,
                 chatset_id: str, rec: Any, gate: Any, is_done: bool,
                 chatset_hash: str = "", instruction: dict | None = None,
                 model_requested: str = "", full: bool = False) -> dict:
    """One stage call as a trajectory record."""
    if gate is not None and getattr(gate, "skipped", False) and not getattr(gate, "errors", []):
        status = "skip"
    elif is_done:
        status = "pass"
    else:
        status = "fail"
    errors = list(getattr(gate, "errors", []) or [])
    body = (instruction or {}).get("body") or ""
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phase": phase,
        "stage": stage,
        "session_id": session_id,
        "chatset_id": chatset_id,
        "chatset_hash": chatset_hash,
        "status": status,
        "verdict": getattr(gate, "verdict", "") or "",
        "errors": errors,
        "warnings": [str(w)[:200] for w in (getattr(gate, "warnings", None) or [])][:8],
        "model_requested": model_requested,
        "model_served": rec.model,
        "provider_served": getattr(rec, "provider_served", ""),
        "upstream_provider": getattr(rec, "upstream_provider", ""),
        "tokens": {"prompt": rec.prompt_tokens, "completion": rec.completion_tokens,
                   "total": rec.total_tokens, "cache_hit": rec.cache_hit_tokens},
        "latency_ms": round(rec.latency_ms, 1),
        "turns": rec.turns,
        "write_rejections": rec.rejections,
        "instruction": {"tb": (instruction or {}).get("tb_number"),
                        "version": (instruction or {}).get("version"),
                        "chars": len(body), "sha256": _sha(body) if body else ""},
        "engine": {"session": rec.engine_session, "stage": rec.engine_stage,
                   "link": rec.log_link},
        "messages": compact_messages(rec.transcript or [], full=full),
        "output": rec.raw_response,
    }


class TrajectoryLog:
    """Append-only JSONL. Safe to call from several worker threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue            # a torn last line from a killed run
        return out
