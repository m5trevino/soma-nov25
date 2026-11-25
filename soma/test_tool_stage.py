"""A stage is a tool call: nothing is handed to the model, prose is rejected."""
import json

from machinetalk.client import SOMA_BEAVER_TOOLS, StageResult
from tool_fake import (scripted_engine, seeded_soma, set_instruction,
                       tool_calls_for)

BODY = "INSTRUCTION BODY UNIQUE-7d41"
TRANSCRIPT = "UNIQUE-TRANSCRIPT-LINE-9a02"


def _run(ec, soma, chatset_id="S1:1", chatset_hash=None):
    return ec.call_stage("distiller", session_id="S1", chatset_id=chatset_id,
                         soma=soma, expected_hash=chatset_hash)


def _seeded():
    soma, cid, h, raw = seeded_soma([{"role": "USER", "content": TRANSCRIPT}])
    set_instruction(soma, "distiller", BODY)
    return soma, cid, h


def test_request_carries_tools_required_and_none_of_the_stage_text():
    soma, cid, _h = _seeded()
    ec = scripted_engine(content="{}", stage="distiller")
    _run(ec, soma)
    messages, kw = ec.sent[0]
    assert kw["tools"] == SOMA_BEAVER_TOOLS and kw["tool_choice"] == "auto"
    assert [m["role"] for m in messages] == ["user"]
    sent = json.dumps(messages)
    assert BODY not in sent and TRANSCRIPT not in sent        # neither is in the request


def test_what_the_model_reads_comes_out_of_the_soma(monkeypatch):
    monkeypatch.setattr("machinetalk.client.BEAVER_WRITE_RETRIES", 2)   # the rejected "{}" gives the second request
    soma, cid, h = _seeded()
    ec = scripted_engine(content="{}", stage="distiller")
    res = _run(ec, soma)
    served = [m["content"] for m in ec.sent[1][0] if m["role"] == "tool"]
    assert served[0] == BODY                                   # the instructions row
    assert json.loads(served[1])["raw_chat_set"][0]["content"] == TRANSCRIPT
    assert json.loads(served[1])["chatset_hash"] == h
    assert res.record.sys_prompt == BODY


def test_prose_instead_of_a_tool_call_fails_the_stage():
    soma, _cid, _h = _seeded()
    ec = scripted_engine([[]])           # a reply with no tool calls
    ec._call_with_retry = lambda stage, messages, **kw: StageResult(
        "Sure! Here is the JSON: {}")
    res = _run(ec, soma)
    assert not res.ok
    assert "prose_instead_of_tool_call" in res.record.gate.errors


def test_write_before_reading_is_refused_then_accepted_after_reads():
    soma, _cid, _h = _seeded()
    only_write = [tool_calls_for("distiller", "{}")[2]]
    ec = scripted_engine([only_write, tool_calls_for("distiller", "{}")])
    _run(ec, soma)
    refusal = ec.sent[1][0][-1]
    assert refusal["role"] == "tool" and "before writing" in refusal["content"]


def test_model_that_never_writes_is_failed_not_looped_forever():
    soma, _cid, _h = _seeded()
    reads = [tool_calls_for("distiller", "{}")[0]]
    res = _run(scripted_engine([reads]), soma)
    assert not res.ok
    assert res.record.gate.errors[0].startswith("no_write_output")


def test_a_chat_set_the_soma_does_not_hold_halts_with_missing_soma_data():
    soma, _cid, _h = _seeded()
    ec = scripted_engine(content="{}", stage="distiller", chatset_id="NOT-THERE")
    res = _run(ec, soma, chatset_id="NOT-THERE")
    assert not res.ok
    assert res.record.gate.errors[0].startswith("MISSING_SOMA_DATA")
