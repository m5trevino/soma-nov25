"""The two loops, the blind start, and halting on data the Soma does not hold."""
from pathlib import Path
from types import SimpleNamespace

import pytest

import machinetalk.engine as engine_mod
from store import Soma
from tool_fake import seeded_soma

SOMA_DIR = Path(__file__).resolve().parent


def _engine(soma, build_chatsets=("S1:1",)):
    """The slice of an Engine the presence and blind-start checks use."""
    import threading
    eng = SimpleNamespace(_soma=soma, _soma_lock=threading.Lock(),
                          _build_chatsets=list(build_chatsets),
                          _SEED_NODE_TYPES=engine_mod.Engine._SEED_NODE_TYPES,
                          _BUILD_NEEDS=engine_mod.Engine._BUILD_NEEDS)
    eng._load_artifact = lambda build, stage: engine_mod.Engine._load_artifact(
        eng, build, stage)
    eng._stage_node_id = lambda build, stage: engine_mod.Engine._stage_node_id(
        eng, build, stage)
    return eng


def _stage_output(soma, build, stage, output=None):
    soma.create_node("StageOutput", {
        "id": f"stage:{build}:{stage}", "build": build, "stage": stage,
        "output": output or {f"{stage}_output": {"ok": True}}})


# ── there is no run that does both loops ───────────────────────────────────

def test_run_needs_a_phase():
    with pytest.raises(ValueError, match="phase"):
        engine_mod.run([], out_dir=Path("/tmp/x"), client=None)
    with pytest.raises(ValueError, match="phase"):
        engine_mod.run([], out_dir=Path("/tmp/x"), client=None, phase="everything")


def test_run_engine_refuses_to_start_without_a_phase(monkeypatch, capsys):
    import run_engine
    monkeypatch.setattr("sys.argv", ["run_engine.py", "--source", "/nonexistent",
                                     "--profile", "/nonexistent.json"])
    monkeypatch.setattr(run_engine, "_ensure_engine_auth", lambda p: p)
    assert run_engine.main() == 1
    assert "--phase" in capsys.readouterr().out


# ── blind start ────────────────────────────────────────────────────────────

def test_a_new_soma_holds_no_data_rows_at_all():
    soma = Soma(":memory:")
    assert soma.node_count() == 0 and soma.edge_count() == 0
    assert len(soma.list_stage_instructions()) == 18        # instructions only: tb-001 to tb-018


def test_a_seeded_soma_passes_the_blind_start_check():
    soma, *_ = seeded_soma()
    engine_mod.Engine._assert_blind_start(_engine(soma))     # does not raise
    types = {n[0] for n in soma.conn.execute("SELECT DISTINCT node_type FROM nodes")}
    assert types <= {"SessionAnchor", "ChatSet", "Message"}


@pytest.mark.parametrize("node_type", ["StageOutput", "DistilledChatSet", "Directive"])
def test_a_soma_that_already_holds_derived_nodes_is_refused(node_type):
    soma, *_ = seeded_soma()
    soma.create_node(node_type, {"id": "left-over", "session_id": "S1"})
    with pytest.raises(RuntimeError, match="blind start violated"):
        engine_mod.Engine._assert_blind_start(_engine(soma))


# ── a build stage whose input is not in the Soma does not run ──────────────

def test_spark_without_nexus_in_the_soma_is_missing_data():
    soma, *_ = seeded_soma()
    eng = _engine(soma)
    assert engine_mod.Engine._missing_soma_data(eng, "S1", "spark") == ["StageOutput nexus"]
    _stage_output(soma, "S1", "nexus")
    assert engine_mod.Engine._missing_soma_data(eng, "S1", "spark") == []


def test_every_build_stage_names_what_it_reads():
    soma, *_ = seeded_soma()
    eng = _engine(soma)
    need = lambda stage: engine_mod.Engine._missing_soma_data(eng, "S1", stage)
    assert need("falcon") == ["StageOutput spark"]
    assert need("eagle") == ["StageOutput falcon"]
    assert need("scout") == ["StageOutput drafter"]
    assert need("oc") == ["StageOutput drafter", "StageOutput scout"]
    assert need("olc") == ["StageOutput oc", "StageOutput scout", "StageOutput drafter"]
    assert need("drafter") == ["StageOutput planbeaver (no chat set has one)"]
    assert need("nexus") == []                               # the chat set is seeded


def test_drafter_needs_a_planbeaver_output_from_a_chat_set_of_the_build():
    soma, *_ = seeded_soma()
    eng = _engine(soma)
    _stage_output(soma, "S1:1", "planbeaver")
    assert engine_mod.Engine._missing_soma_data(eng, "S1", "drafter") == []


def test_nexus_needs_every_chat_set_of_the_build_in_the_soma():
    soma, *_ = seeded_soma()
    eng = _engine(soma, build_chatsets=("S1:1", "S1:2"))
    assert engine_mod.Engine._missing_soma_data(eng, "S1", "nexus") == ["ChatSet S1:2"]


def test_a_stage_called_without_a_soma_halts_naming_the_missing_data():
    from machinetalk.client import EngineClient
    ec = object.__new__(EngineClient)
    with pytest.raises(RuntimeError, match="MISSING_SOMA_DATA"):
        ec.call_stage("distiller", session_id="S1", chatset_id="S1:1", soma=None)


# ── the legacy input path is gone ──────────────────────────────────────────

def test_call_stage_no_longer_takes_a_payload_chunk_or_contracts():
    import inspect
    from machinetalk.client import EngineClient
    params = set(inspect.signature(EngineClient.call_stage).parameters)
    assert not {"payload", "chunk", "contracts"} & params
    assert "soma" in params


# ── the launcher runs the loops as two phases, in order ────────────────────

def _launch(monkeypatch, tmp_path, returncodes=None, **kw):
    """Run run_sessions.run_pipeline with the engine stubbed; return the commands."""
    import sys
    sys.path.insert(0, str(SOMA_DIR.parent))
    import run_sessions
    cmds = []
    codes = list(returncodes or [0, 0])

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        return SimpleNamespace(returncode=codes.pop(0) if codes else 0)

    monkeypatch.setattr(run_sessions.subprocess, "run", fake_run)
    monkeypatch.setattr(run_sessions, "start_dashboard", lambda: None)
    monkeypatch.setattr(run_sessions, "print_banner", lambda: None)
    monkeypatch.setattr(run_sessions, "QUEUE_FILE", str(tmp_path / "q.json"))
    monkeypatch.setattr(run_sessions, "QUEUE_FILE_ALT", str(tmp_path / "q2.json"))
    monkeypatch.setattr(run_sessions, "DEBUG_DIR", str(tmp_path / "dbg"))
    run_sessions.run_pipeline(["/x/s1"], profile="p.json", **kw)
    return [(c[c.index("--phase") + 1], c) for c in cmds]


def test_a_normal_run_is_extract_then_crystallize(monkeypatch, tmp_path):
    runs = _launch(monkeypatch, tmp_path)
    assert [p for p, _ in runs] == ["extract", "crystallize"]


def test_crystallize_never_runs_after_a_failed_extraction(monkeypatch, tmp_path):
    runs = _launch(monkeypatch, tmp_path, returncodes=[1, 0])
    assert [p for p, _ in runs] == ["extract"]


def test_the_old_count_flag_stays_extraction_only(monkeypatch, tmp_path):
    assert [p for p, _ in _launch(monkeypatch, tmp_path, max_chatsets=1)] == ["extract"]


def test_a_named_chat_set_goes_through_the_beavers_and_then_the_build(monkeypatch, tmp_path):
    runs = _launch(monkeypatch, tmp_path, only=[("s1", 2)])
    assert [p for p, _ in runs] == ["extract", "crystallize"]      # all twelve stages, in order
    for _phase, cmd in runs:
        assert cmd[cmd.index("--only") + 1] == "s1:2"               # both phases work on that chat set


def test_a_beaver_stage_is_extraction_and_a_build_stage_is_crystallize(monkeypatch, tmp_path):
    (phase, cmd), = _launch(monkeypatch, tmp_path, stage="planbeaver")
    assert phase == "extract" and cmd[cmd.index("--stage") + 1] == "planbeaver"
    (phase, cmd), = _launch(monkeypatch, tmp_path, stage="scout")
    assert phase == "crystallize" and cmd[cmd.index("--from") + 1] == "scout"
    assert "--stage" not in cmd and "--only" not in cmd


def test_chat_set_flags_belong_to_extraction_only(monkeypatch, tmp_path):
    runs = _launch(monkeypatch, tmp_path, force=True)
    by_phase = dict(runs)
    assert "--force" in by_phase["extract"] and "--force" not in by_phase["crystallize"]


# ── a transport failure in a build stage fails the stage, not the run ───────

def test_a_build_stage_that_cannot_get_an_answer_is_recorded_and_the_build_halts(tmp_path):
    from machinetalk.checkpoint import FAILED
    from machinetalk.trajectory import TrajectoryLog
    marks, printed = [], []

    def boom(build, stage, **kw):
        raise RuntimeError("All retries exhausted for stage 'nexus' | HTTP_429")
    eng = SimpleNamespace(
        _run_build_stage=boom,
        ck=SimpleNamespace(ss_mark=lambda *a, **k: marks.append((a, k))),
        traj=TrajectoryLog(tmp_path / "t.jsonl"), run_id="R", phase="crystallize",
        _print=printed.append)
    eng._log_trajectory_error = lambda **kw: engine_mod.Engine._log_trajectory_error(eng, **kw)
    ok = engine_mod.Engine._run_build_stage_safely(eng, "B", "nexus")
    assert ok is False                                       # the build halts at this stage
    (args, _k), = marks
    assert args[1:3] == ("nexus", FAILED) and "TRANSPORT" in _k["errors"]
    (rec,) = eng.traj.read()
    assert rec["status"] == "error" and "HTTP_429" in rec["errors"][0]
    assert any("[FAILED]" in p for p in printed)             # and it says so on screen
