"""Test helpers: a seeded Soma and a scripted model that works it by tool call.

A stage reads its instructions and its input from the Soma and writes its
output there. So a test seeds a real Soma first (the SessionAnchor, the
ChatSet and its Messages, written by the same seeder the engine uses) and then
runs the stage. The only thing faked is the model: it is a script of the tool
calls a model would make, answered by the real tool loop against the real Soma.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from machinetalk.client import EngineClient, StageResult
from machinetalk.hashes import chatset_hash_for
from machinetalk.soma_bridge import SomaBridge
from store import Soma

RAW = [{"role": "USER", "content": "Build a CLI. Never shell out to curl."},
       {"role": "AGENT", "content": "On it."}]


def seeded_soma(raw=None, session_id="S1", chatset_id="S1:1"):
    """A Soma holding just the seeded skeleton for one chat set.

    Returns ``(soma, chatset_id, chatset_hash, raw)``. The hash is the one the
    seeder assigned, which is what a stage is checked against.
    """
    raw = [dict(m) for m in (raw if raw is not None else RAW)]
    soma = Soma(":memory:")
    cs = SimpleNamespace(chatset_id=chatset_id, session_id=session_id,
                         set_number=1, chars=sum(len(m["content"]) for m in raw),
                         raw_chat_set=[dict(m) for m in raw], chatset_hash="",
                         provider="p", models=[])
    SomaBridge(soma).seed_session(session_id, [cs])
    return soma, chatset_id, soma.get_node(chatset_id).properties["chatset_hash"], raw


def set_instruction(soma, stage, body):
    """Replace a stage's instruction row, so a test can recognise its text."""
    soma.conn.execute("UPDATE stage_instructions SET body=? WHERE stage_name=?",
                      (body, stage))
    soma.conn.commit()


def _call(i, name, args):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def tool_calls_for(stage, content, chatset_id="S1:1"):
    """The three calls a model makes: read instructions, read input, write."""
    try:
        output = json.loads(content)
    except (ValueError, TypeError):
        output = content
    return [_call(1, "get_receptor_config", {"stage": stage}),
            _call(2, "get_chatset", {"chatset_id": chatset_id}),
            _call(3, "create_soma_node",
                  {"node_type": "StageOutput", "stage": stage,
                   "output": output if isinstance(output, str) else json.dumps(output)})]


def tool_reply(raw: dict, stage: str, chatset_id: str = "S1:1") -> dict:
    """A raw SDK reply whose text message becomes the three tool calls."""
    msg = (raw.get("choices") or [{}])[0].get("message", {})
    out = dict(raw)
    out["choices"] = [{"message": {
        "content": None,
        "tool_calls": tool_calls_for(stage, msg.get("content", ""), chatset_id)}}]
    return out


def scripted_engine(turns=None, *, content=None, stage=None, chatset_id="S1:1",
                    model_id="m", **result_fields):
    """An EngineClient whose model makes the scripted tool calls.

    Give ``turns`` (a list of tool-call lists, the last one repeating) or give
    ``content`` and ``stage`` for the usual three calls ending in a write of
    that content. ``engine.sent`` records every request that was sent.
    """
    if turns is None:
        turns = [tool_calls_for(stage, content, chatset_id)]
    ec = object.__new__(EngineClient)
    ec.timeout = 180.0
    ec.sent = []
    script = list(turns)

    def fake(stage_name, messages, **kw):
        ec.sent.append((json.loads(json.dumps(messages)), kw))
        calls = script.pop(0) if len(script) > 1 else script[0]
        return StageResult("", model_id=model_id, tool_calls=calls, **result_fields)
    ec._call_with_retry = fake
    return ec
