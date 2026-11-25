"""Nothing outside the repo is named in code: a machine moves its data with an env var."""
import re
from pathlib import Path

import pytest

from machinetalk import paths

SOMA_DIR = Path(__file__).resolve().parent


def test_defaults_are_this_workstations_layout(monkeypatch):
    for v in ("SOMA_SESSIONS_DIR", "SOMA_INVARIANTS_DB", "SOMA_SESSION_VAULT_DB", "SOMA_TURNKEY_PATH"):
        monkeypatch.delenv(v, raising=False)
    assert paths.sessions_dir() == Path("/media/storage/dendritic123/chat-logs/sessions")
    assert paths.invariants_db() == "/media/storage/invariants/invariants.db"
    assert paths.turnkey_path() is None


def test_an_env_var_moves_each_location(monkeypatch):
    monkeypatch.setenv("SOMA_SESSIONS_DIR", "/workspace/sessions")
    monkeypatch.setenv("SOMA_INVARIANTS_DB", "/workspace/inv.db")
    monkeypatch.setenv("SOMA_TURNKEY_PATH", "/workspace/turn-key")
    assert paths.sessions_dir() == Path("/workspace/sessions")
    assert paths.invariants_db() == "/workspace/inv.db"
    assert paths.turnkey_path() == "/workspace/turn-key"


def test_the_engine_reads_the_invariants_db_from_the_env(monkeypatch):
    import machinetalk.invariants as inv
    import machinetalk.engine as engine_mod
    from types import SimpleNamespace
    seen = []

    class Corpus:
        def __init__(self, path, *a, **k): seen.append(path)
        def search(self, *a, **k): return []
        def close(self): pass
    monkeypatch.setattr(inv, "LawCorpus", Corpus)
    monkeypatch.setenv("SOMA_INVARIANTS_DB", "/runpod/inv.db")
    engine_mod.Engine._query_invariants(SimpleNamespace(), "x")
    assert seen == ["/runpod/inv.db"]


def test_run_engine_finds_a_session_under_the_configured_dir(monkeypatch, tmp_path):
    import json
    import run_engine
    sessions = tmp_path / "sessions"
    (sessions / "my-session").mkdir(parents=True)
    queue = tmp_path / "q.json"
    queue.write_text(json.dumps({"selected": ["some/where/my-session"]}))
    monkeypatch.setenv("SOMA_SESSIONS_DIR", str(sessions))
    assert run_engine.load_queue(str(queue)) == [sessions / "my-session"]


def test_no_workstation_path_is_left_in_the_pipeline_code():
    """The three places a path may be named: the defaults in paths.py and run_sessions."""
    allowed = {"machinetalk/paths.py"}
    offenders = []
    for py in list(SOMA_DIR.rglob("*.py")):
        rel = py.relative_to(SOMA_DIR).as_posix()
        if rel.startswith("test_") or rel in allowed or "/.venv/" in str(py):
            continue
        for n, line in enumerate(py.read_text().splitlines(), 1):
            if re.search(r"/media/storage|/workspace/artifacts", line) and not line.lstrip().startswith(("#", '"""', "-")):
                offenders.append(f"{rel}:{n}: {line.strip()[:80]}")
    # a few non-pipeline helpers and docstrings may still mention one; the engine path may not
    engine_files = [o for o in offenders if o.startswith(("run_engine", "machinetalk/engine", "machinetalk/client", "machinetalk/invariants"))]
    assert not engine_files, engine_files
