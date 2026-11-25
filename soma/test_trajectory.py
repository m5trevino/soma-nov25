"""Every stage call is kept whole: the conversation, the turns, the verdict."""
import hashlib
import json
from types import SimpleNamespace

import machinetalk.engine as engine_mod
from machinetalk.gate import is_done
from machinetalk.trajectory import (TrajectoryLog, compact_messages, new_run_id)
from tool_fake import (scripted_engine, seeded_soma, set_instruction,
                       tool_calls_for)

BODY = "INSTRUCTION BODY UNIQUE-3c9e"


def _stage_call(content="{}", turns=None):
    soma, cid, h, raw = seeded_soma()
    set_instruction(soma, "planbeaver", BODY)
    ec = scripted_engine(turns, content=content, stage="planbeaver", chatset_id=cid)
    res = ec.call_stage("planbeaver", session_id="S1", chatset_id=cid, soma=soma)
    return soma, cid, res


def _log(tmp_path, soma, cid, res, run_id="RUN-T"):
    fake = SimpleNamespace(
        traj=TrajectoryLog(tmp_path / "trajectories.jsonl"), run_id=run_id,
        phase="extract", _soma=soma, _print=lambda *_a: None,
        client=SimpleNamespace(model_for=lambda stage: "my-7b"))
    engine_mod.Engine._log_trajectory(
        fake, stage="planbeaver", session_id="S1", chatset_id=cid,
        rec=res.record, gate=res.record.gate, chatset_hash="HASH")
    return fake.traj.read()


OK = json.dumps({"planbeaver_output": {"status": "READY"}})


def test_the_whole_tool_conversation_is_kept(tmp_path):
    soma, cid, res = _stage_call(content=OK)
    (r,) = _log(tmp_path, soma, cid, res)
    assert r["run_id"] == "RUN-T" and r["stage"] == "planbeaver" and r["phase"] == "extract"
    assert r["model_requested"] == "my-7b" and r["model_served"] == "m"
    roles = [m["role"] for m in r["messages"]]
    assert roles[0] == "user" and "assistant" in roles and "tool" in roles
    # the model's own calls and write are stored whole
    calls = [c["function"]["name"] for m in r["messages"]
             for c in m.get("tool_calls", [])]
    assert calls == ["get_receptor_config", "get_chatset", "create_soma_node"]
    # one entry per turn, with its own tokens and the tools it called
    assert [t["tool_calls"] for t in r["turns"]] == [
        ["get_receptor_config", "get_chatset", "create_soma_node"]]
    assert r["status"] == "pass" and r["write_rejections"] == 0


def test_big_tool_results_are_kept_as_references_that_match_the_soma(tmp_path):
    soma, cid, res = _stage_call(content=OK)
    (r,) = _log(tmp_path, soma, cid, res)
    tool = {m["name"]: m["content"] for m in r["messages"] if m["role"] == "tool"}
    ref = tool["get_receptor_config"]
    assert ref["chars"] == len(BODY)
    assert ref["sha256"] == hashlib.sha256(BODY.encode()).hexdigest()
    assert r["instruction"]["sha256"] == ref["sha256"]       # the same text, named by hash
    assert isinstance(tool["get_chatset"], dict) and tool["get_chatset"]["chars"] > 0
    assert BODY not in json.dumps(r)                         # the text itself is not copied


def test_full_keeps_the_text():
    msgs = [{"role": "tool", "name": "get_receptor_config", "content": BODY}]
    assert compact_messages(msgs, full=True)[0]["content"] == BODY
    assert compact_messages(msgs)[0]["content"]["chars"] == len(BODY)


def test_an_error_reply_is_kept_as_text_because_it_is_the_evidence():
    msgs = [{"role": "tool", "name": "get_chatset",
             "content": "error: MISSING_SOMA_DATA: chat set X is not in the Soma"}]
    assert compact_messages(msgs)[0]["content"].startswith("error: MISSING_SOMA_DATA")


def test_a_write_the_gate_sent_back_is_counted(tmp_path, monkeypatch):
    monkeypatch.setattr("machinetalk.client.BEAVER_WRITE_RETRIES", 2)   # rewrites on: this tests the rewrite
    bad = tool_calls_for("planbeaver", "{}")
    soma, cid, res = _stage_call(turns=[bad, bad, bad])
    (r,) = _log(tmp_path, soma, cid, res)
    assert r["write_rejections"] == 2                        # MAX_WRITE_RETRIES, then it goes on
    assert len(r["turns"]) == 3


def test_a_passing_stage_is_marked_pass(tmp_path):
    soma, cid, res = _stage_call(content=OK)
    (r,) = _log(tmp_path, soma, cid, res)
    assert r["status"] == "pass" and r["errors"] == [] and is_done(res.record.gate)
    assert json.loads(r["output"])["planbeaver_output"]["status"] == "READY"


def test_the_log_is_append_only_and_survives_a_torn_last_line(tmp_path):
    log = TrajectoryLog(tmp_path / "t.jsonl")
    log.append({"a": 1})
    log.append({"a": 2})
    with open(log.path, "a") as f:
        f.write('{"a": 3')                                   # a killed run's half line
    assert [r["a"] for r in log.read()] == [1, 2]


def test_a_stage_that_raised_still_leaves_a_line(tmp_path):
    fake = SimpleNamespace(traj=TrajectoryLog(tmp_path / "t.jsonl"), run_id="R", phase="extract")
    engine_mod.Engine._log_trajectory_error(
        fake, stage="distiller", session_id="S1", chatset_id="S1:1",
        detail="RuntimeError: MISSING_SOMA_DATA: chat set S1:1 is not seeded")
    (r,) = fake.traj.read()
    assert r["status"] == "error" and "MISSING_SOMA_DATA" in r["errors"][0]


def test_run_ids_sort_by_time_and_do_not_collide():
    ids = {new_run_id() for _ in range(200)}
    assert len(ids) == 200 and all(i.startswith("RUN-") for i in ids)
