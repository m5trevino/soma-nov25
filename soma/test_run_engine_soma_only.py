"""Test --soma-only: first-session, first-chat-set, distiller-only, Soma-write.

Tests the new `_soma_only_run` path in run_engine.py.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import pytest

# Ensure local soma path is importable
SOMA_DIR = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _add_soma_path():
    if str(SOMA_DIR) not in os.sys.path:
        os.sys.path.insert(0, str(SOMA_DIR))


# ---------------------------------------------------------------------------
# Mock LLM response for distiller (no real LLM call)
# ---------------------------------------------------------------------------

RecMock = mock.MagicMock()
RecMock.gate = mock.MagicMock(
    verdict="PASS",
    skipped=False,
    parsed={"distiller_output": {"skip": False}},
    errors=[],
    reasons=[],
)
RecMock.model = "mock-model"
RecMock.latency_ms = 42.0

ResMock = mock.MagicMock(
    ok=True, record=RecMock, skipped=False, verdict="PASS", parsed=RecMock.gate.parsed
)

# What the distiller call was given, for the tests to inspect.
CALLS: list = []


@pytest.fixture(autouse=True)
def _mock_engine_client():
    """Patch EngineClient so no model is called; record what the stage is given."""
    import machinetalk.client as client_mod

    class FakeEngineClient:
        def __init__(self, *a, **kw):
            pass

        def call_stage(self, stage, **kw):
            CALLS.append((stage, kw))
            return ResMock

    import run_engine as run_engine_mod        # it holds its own reference to the class

    CALLS.clear()
    orig, orig_in_run_engine = client_mod.EngineClient, run_engine_mod.EngineClient
    client_mod.EngineClient = run_engine_mod.EngineClient = FakeEngineClient
    yield
    client_mod.EngineClient, run_engine_mod.EngineClient = orig, orig_in_run_engine


@pytest.fixture()
def test_session_dir(tmp_path: Path) -> Path:
    """Create a minimal test session with exactly 2 chat sets."""
    sd = tmp_path / "test-session-001"
    sd.mkdir()
    (sd / "session.json").write_text(
        json.dumps(
            {
                "session_id": "test-session-001",
                "total_sets": 2,
                "provider": "test",
                "models_used": {"mock-model": 2},
            }
        )
    )
    (sd / "000001.json").write_text(
        json.dumps(
            {
                "chat_set_id": "cs-001",
                "session_id": "test-session-001",
                "set_number": 1,
                "user_text": "Hello, create a test file.",
                "assistant_text": "Sure! I will create a test file.",
                "model": "mock-model",
                "provider": "test",
            }
        )
    )
    (sd / "000002.json").write_text(
        json.dumps(
            {
                "chat_set_id": "cs-002",
                "session_id": "test-session-001",
                "set_number": 2,
                "user_text": "Now add a function.",
                "assistant_text": "Done!",
                "model": "mock-model",
                "provider": "test",
            }
        )
    )
    return sd


@pytest.fixture()
def test_queue(tmp_path: Path, test_session_dir: Path) -> Path:
    """Create a queue JSON pointing to the test session."""
    base = tmp_path / "sessions"
    base.mkdir()
    dst = base / test_session_dir.name
    dst.mkdir()
    for f in test_session_dir.iterdir():
        if f.is_file():
            shutil.copy2(f, dst / f.name)
    qp = tmp_path / "queue.json"
    qp.write_text(json.dumps({"selected": [str(dst)], "count": 1}))
    return qp


@pytest.fixture()
def test_profile(tmp_path: Path) -> Path:
    """Copy the real profile for EngineClient."""
    here = Path(__file__).resolve().parent
    src = here / "turn-key-profiles" / "soma-current.json"
    if not src.exists():
        # The profile dirs are gitignored. Use the newest real profile.
        found = sorted((here.parent / "turn-key-profile").glob("*.json"),
                       key=lambda f: f.stat().st_mtime)
        if not found:
            pytest.skip("no TurnKey profile available (turn-key-profile/ is empty)")
        src = found[-1]
    dst = tmp_path / "profile.json"
    dst.write_text(src.read_text())
    return dst


@pytest.fixture()
def clean_out(tmp_path: Path) -> Path:
    """Empty output directory."""
    out = tmp_path / "soma_out"
    out.mkdir()
    return out


def _node_types(soma_file: Path) -> dict:
    """node_type -> [node ids] read straight from the SQLite file."""
    import sqlite3
    con = sqlite3.connect(str(soma_file))
    out: dict = {}
    for nid, ntype in con.execute("SELECT node_id, node_type FROM nodes"):
        out.setdefault(ntype, []).append(nid)
    return out, con


class TestSomaOnlyFirstChatSet:
    """--soma-only seeds ONE chat set into a blind Soma and runs the distiller
    on it, which is handed nothing but the Soma."""

    def _run(self, **kw):
        from run_engine import _soma_only_run
        return _soma_only_run(argparse.Namespace(debug=None, **kw))

    def test_soma_only_source_flag(self, test_session_dir, test_profile, clean_out):
        rc = self._run(source=str(test_session_dir), queue=None,
                       profile=str(test_profile), out=str(clean_out))
        assert rc == 0
        soma_file = clean_out / "soma.db"
        assert soma_file.exists()
        types, con = _node_types(soma_file)

        # the seeded skeleton and the distiller's log, nothing downstream
        assert "SessionAnchor" in types and "ReceptorLog" in types
        assert len(types["ChatSet"]) == 1               # only the first chat set
        assert len(types["Message"]) >= 2
        downstream = {"IntentNode", "TaskNode", "OutcomeNode", "StageOutput",
                      "FileNode", "InvariantQueryNode", "VerifiedNode"}
        assert not downstream & set(types), (
            "soma-only must not run the beavers or stages 2-4")
        edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert edges >= 2

    def test_the_distiller_is_handed_nothing_but_the_soma(
            self, test_session_dir, test_profile, clean_out):
        self._run(source=str(test_session_dir), queue=None,
                  profile=str(test_profile), out=str(clean_out))
        assert len(CALLS) == 1
        stage, kw = CALLS[0]
        assert stage == "distiller"
        assert not {"payload", "chunk", "contracts"} & set(kw)
        # what it reads is in the Soma it was given
        soma = kw["soma"]
        cs = soma.get_node(kw["chatset_id"])
        assert cs is not None and cs.properties["chatset_hash"] == kw["expected_hash"]
        assert kw["raw_chat_set"]

    def test_soma_only_queue_flag(self, test_session_dir, test_profile, clean_out,
                                  test_queue):
        rc = self._run(source=None, queue=str(test_queue),
                       profile=str(test_profile), out=str(clean_out))
        assert rc == 0
        types, _con = _node_types(clean_out / "soma.db")
        assert {"SessionAnchor", "ChatSet", "ReceptorLog"} <= set(types)

    def test_a_soma_left_by_an_earlier_run_is_set_aside_not_deleted(
            self, test_session_dir, test_profile, clean_out):
        kw = dict(source=str(test_session_dir), queue=None,
                  profile=str(test_profile), out=str(clean_out))
        self._run(**kw)
        self._run(**kw)
        backups = list(clean_out.glob("soma.db.bak-*"))
        assert len(backups) == 1 and (clean_out / "soma.db").exists()
        types, _con = _node_types(clean_out / "soma.db")
        assert len(types["ChatSet"]) == 1               # the new run started blind
