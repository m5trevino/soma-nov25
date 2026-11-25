"""The beavers read and write the Soma by tool call; Python assembles nothing."""
import json
from types import SimpleNamespace

from machinetalk.client import EngineClient, StageResult
from machinetalk.hashes import chatset_hash_for
from machinetalk.soma_bridge import SomaBridge
from store import Soma

RAW = [{"role": "USER", "content": "Build a CLI. Never shell out to curl."},
       {"role": "AGENT", "content": "On it."}]
H = chatset_hash_for(RAW)


def _call(i, name, args):
    return {"id": f"c{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _soma():
    soma = Soma(":memory:")
    cs = SimpleNamespace(chatset_id="S1:1", session_id="S1", set_number=1,
                         chars=40, raw_chat_set=[dict(m) for m in RAW],
                         chatset_hash="", provider="p", models=[])
    SomaBridge(soma).seed_session("S1", [cs])
    return soma


def _client(turns):
    """A model that answers with the scripted tool calls, one reply per turn."""
    ec = object.__new__(EngineClient)
    ec.sent = []
    script = list(turns)

    def fake(stage, messages, **kw):
        ec.sent.append((json.loads(json.dumps(messages)), kw))
        calls = script.pop(0) if len(script) > 1 else script[0]   # last turn repeats
        return StageResult("", model_id="m", tool_calls=calls)
    ec._call_with_retry = fake
    return ec


def _plan_output():
    return json.dumps({"planbeaver_output": {"plan_id": "P", "files": []}})


def test_beaver_reads_the_soma_and_its_write_becomes_a_node():
    soma = _soma()
    soma.create_node("StageOutput", {       # the node the engine already keeps
        "id": "stage:S1:1:introbeaver", "build": "S1:1", "stage": "introbeaver",
        "output": {"introbeaver_output": {"entries": []}}})
    ec = _client([
        [_call(1, "get_receptor_config", {"stage": "planbeaver"})],
        [_call(2, "get_chatset", {"chatset_id": "S1:1"}),
         _call(3, "query_nodes", {"node_type": "StageOutput", "chatset_id": "S1:1"})],
        [_call(4, "create_soma_node", {"node_type": "StageOutput",
                                       "stage": "planbeaver",
                                       "output": _plan_output()})]])
    res = ec.call_stage("planbeaver", session_id="S1", chatset_id="S1:1", soma=soma)
    # the request itself carried no payload: only the identity message and tools
    first_messages, kw = ec.sent[0]
    assert [m["role"] for m in first_messages] == ["user"]
    assert kw["tool_choice"] == "auto"
    assert {t["function"]["name"] for t in kw["tools"]} == {
        "get_receptor_config", "get_chatset", "query_nodes", "create_soma_node"}
    # what the tools returned came out of the Soma
    served = [m["content"] for m in ec.sent[2][0] if m["role"] == "tool"]
    assert json.loads(served[1])["raw_chat_set"] == RAW
    assert json.loads(served[1])["chatset_hash"] == H
    assert json.loads(served[2])[0]["stage"] == "introbeaver"
    # the write is a node in the Soma, linked to the chat set
    node = soma.get_node("stage:S1:1:planbeaver")
    assert node is not None and node.node_type == "StageOutput"
    assert node.properties["output"]["planbeaver_output"]["plan_id"] == "P"
    assert res.record.status == "pass"


def test_a_failed_gate_writes_nothing():
    soma = _soma()
    ec = _client([
        [_call(1, "get_receptor_config", {"stage": "planbeaver"}),
         _call(2, "get_chatset", {"chatset_id": "S1:1"})],
        [_call(3, "create_soma_node", {"node_type": "StageOutput",
                                       "stage": "planbeaver", "output": "{}"})]])
    res = ec.call_stage("planbeaver", session_id="S1", chatset_id="S1:1", soma=soma)
    assert not res.ok
    assert soma.get_node("stage:S1:1:planbeaver") is None


def test_chat_set_that_does_not_match_its_hash_is_refused_by_the_tool():
    soma = _soma()
    soma.delete_node("msg-S1:1-1")
    ec = _client([
        [_call(1, "get_receptor_config", {"stage": "planbeaver"}),
         _call(2, "get_chatset", {"chatset_id": "S1:1"})],
        [_call(3, "create_soma_node", {"node_type": "StageOutput",
                                       "stage": "planbeaver", "output": _plan_output()})],
    ] + [[_call(9, "get_chatset", {"chatset_id": "S1:1"})]] * 8)
    res = ec.call_stage("planbeaver", session_id="S1", chatset_id="S1:1", soma=soma)
    tool_msgs = [m["content"] for m in ec.sent[1][0] if m["role"] == "tool"]
    assert tool_msgs[1].startswith("error:") and "hash" in tool_msgs[1]
    assert not res.ok and soma.get_node("stage:S1:1:planbeaver") is None


def test_model_may_only_write_a_stageoutput_for_its_own_stage():
    soma = _soma()
    ec = _client([
        [_call(1, "get_receptor_config", {"stage": "planbeaver"}),
         _call(2, "get_chatset", {"chatset_id": "S1:1"})],
        [_call(3, "create_soma_node", {"node_type": "Directive",
                                       "stage": "planbeaver", "output": "{}"})],
    ] + [[_call(9, "get_chatset", {"chatset_id": "S1:1"})]] * 8)
    ec.call_stage("planbeaver", session_id="S1", chatset_id="S1:1", soma=soma)
    refusal = [m["content"] for m in ec.sent[2][0] if m["role"] == "tool"][-1]
    assert "may only write a StageOutput" in refusal


# ── build stages (nexus … olc) ─────────────────────────────────────────────

def _seed_two(soma):
    raws = [[{"role": "USER", "content": "first set"}],
            [{"role": "USER", "content": "second set"}]]
    css = [SimpleNamespace(chatset_id=f"S1:{i}", session_id="S1", set_number=i,
                           chars=9, raw_chat_set=[dict(m) for m in raw],
                           chatset_hash="", provider="p", models=[])
           for i, raw in enumerate(raws, 1)]
    SomaBridge(soma).seed_session("S1", css)
    return raws


def test_nexus_reads_the_merged_chat_set_of_the_build_from_the_soma():
    soma = Soma(":memory:")
    raws = _seed_two(soma)
    ec = _client([
        [_call(1, "get_receptor_config", {"stage": "nexus"}),
         _call(2, "get_chatset", {"chatset_id": "S1"})],
        [_call(3, "create_soma_node", {"node_type": "StageOutput", "stage": "nexus",
                                       "output": "{}"})]])
    ec.call_stage("nexus", session_id="S1", chatset_id="S1", soma=soma,
                  build_chatsets=["S1:1", "S1:2"])
    served = json.loads([m["content"] for m in ec.sent[1][0]
                         if m["role"] == "tool"][1])
    assert served["raw_chat_set"] == raws[0] + raws[1]
    assert served["chatset_hash"] == chatset_hash_for(raws[0] + raws[1])


def test_build_stage_queries_the_stage_outputs_the_build_already_has():
    soma = _soma()
    for nid, build, stage in (("stage:S1:nexus", "S1", "nexus"),
                              ("stage:S1:1:planbeaver", "S1:1", "planbeaver"),
                              ("stage:OTHER:nexus", "OTHER", "nexus")):
        soma.create_node("StageOutput", {"id": nid, "build": build, "stage": stage,
                                         "output": {"from": stage}})
    ec = _client([
        [_call(1, "get_receptor_config", {"stage": "spark"}),
         _call(2, "query_nodes", {"node_type": "StageOutput", "chatset_id": "S1"})],
        [_call(3, "create_soma_node", {"node_type": "StageOutput", "stage": "spark",
                                       "output": "{}"})]])
    ec.call_stage("spark", session_id="S1", chatset_id="S1", soma=soma,
                  build_chatsets=["S1:1"])
    served = [m["content"] for m in ec.sent[1][0] if m["role"] == "tool"]
    got = json.loads(served[1])
    assert {(g["stage"], g["chatset_id"]) for g in got} == {
        ("nexus", "S1"), ("planbeaver", "S1:1")}          # not OTHER's
    assert not any(c.startswith("error:") for c in served)  # the query counted as the read


def test_a_stage_can_have_its_own_tool_that_queries_a_data_store():
    soma = _soma()
    ec = _client([
        [_call(1, "get_receptor_config", {"stage": "scout"}),
         _call(2, "get_dna_contracts", {"chatset_id": "S1"})],
        [_call(3, "create_soma_node", {"node_type": "StageOutput", "stage": "scout",
                                       "output": "{}"})]])
    hooks = {"get_dna_contracts": ("DNA contracts.", lambda _a: '[{"contract_id": "DNA-001"}]')}
    ec.call_stage("scout", session_id="S1", chatset_id="S1", soma=soma, tool_hooks=hooks)
    names = {t["function"]["name"] for t in ec.sent[0][1]["tools"]}
    assert "get_dna_contracts" in names
    served = [m["content"] for m in ec.sent[1][0] if m["role"] == "tool"]
    assert served[1] == '[{"contract_id": "DNA-001"}]'


def test_build_stage_call_hands_the_engine_the_soma():
    """_run_build_stage used to call call_stage without soma=, which raised."""
    import inspect
    import machinetalk.engine as engine_mod
    src = inspect.getsource(engine_mod.Engine._run_build_stage)
    assert '"soma": self._soma' in src and "build_chatsets" in src


# ── a document that arrives encoded twice ──────────────────────────────────

def test_double_encoded_output_is_decoded_once_and_still_gated():
    from machinetalk.client import _undo_double_encoding as undo
    doc = {"drafter_output": {"files": [{"path": "a.py"}]}}
    assert undo(json.dumps(doc)) == doc                              # the whole document as a string
    assert undo({"drafter_output": json.dumps(doc["drafter_output"])}) == doc   # the value as a string
    assert undo(doc) == doc                                          # already right: untouched
    assert undo("not json") == "not json"                            # broken stays broken
    assert undo({"stage": "drafter", "drafter_output": json.dumps(doc["drafter_output"])}) == {
        "stage": "drafter", "drafter_output": doc["drafter_output"]}  # with a stray sibling key
    assert undo({"a": "text", "b": 1}) == {"a": "text", "b": 1}      # plain strings untouched
    assert undo({"drafter_output": "a plain string"}) == {"drafter_output": "a plain string"}


# ── a write that is not JSON is handed back, then accepted once it parses ──

def test_malformed_write_is_handed_back_and_the_rewrite_is_accepted(monkeypatch):
    monkeypatch.setattr("machinetalk.client.BEAVER_WRITE_RETRIES", 2)   # rewrites on: this tests the rewrite
    soma = _soma()
    ec = _client([
        [_call(1, "get_receptor_config", {"stage": "planbeaver"}),
         _call(2, "get_chatset", {"chatset_id": "S1:1"})],
        [_call(3, "create_soma_node", {"node_type": "StageOutput", "stage": "planbeaver",
                                       "output": '{"planbeaver_output": {"plan_id": \\"P'})],
        [_call(4, "create_soma_node", {"node_type": "StageOutput", "stage": "planbeaver",
                                       "output": _plan_output()})]])
    res = ec.call_stage("planbeaver", session_id="S1", chatset_id="S1:1", soma=soma)
    handed_back = [m["content"] for m in ec.sent[2][0] if m["role"] == "tool"][-1]
    assert handed_back.startswith("error:") and "gate rejected" in handed_back
    assert res.ok and soma.get_node("stage:S1:1:planbeaver") is not None


def test_malformed_write_goes_to_the_gate_after_the_retries():
    soma = _soma()
    bad = _call(3, "create_soma_node", {"node_type": "StageOutput", "stage": "planbeaver",
                                        "output": "{not json"})
    ec = _client([[_call(1, "get_receptor_config", {"stage": "planbeaver"}),
                   _call(2, "get_chatset", {"chatset_id": "S1:1"})],
                  [bad], [bad], [bad]])
    res = ec.call_stage("planbeaver", session_id="S1", chatset_id="S1:1", soma=soma)
    assert not res.ok and soma.get_node("stage:S1:1:planbeaver") is None
    assert res.record.gate.errors                       # the gate, not the tool, fails it


def test_gate_errors_are_handed_back_so_the_model_can_fix_the_missing_wrapper(monkeypatch):
    monkeypatch.setattr("machinetalk.client.BEAVER_WRITE_RETRIES", 2)   # rewrites on: this tests the rewrite
    soma = _soma()
    ec = _client([
        [_call(1, "get_receptor_config", {"stage": "planbeaver"}),
         _call(2, "get_chatset", {"chatset_id": "S1:1"})],
        [_call(3, "create_soma_node", {"node_type": "StageOutput", "stage": "planbeaver",
                                       "output": json.dumps({"plan_id": "P", "files": []})})],
        [_call(4, "create_soma_node", {"node_type": "StageOutput", "stage": "planbeaver",
                                       "output": _plan_output()})]])
    res = ec.call_stage("planbeaver", session_id="S1", chatset_id="S1:1", soma=soma)
    handed_back = [m["content"] for m in ec.sent[2][0] if m["role"] == "tool"][-1]
    assert "missing:planbeaver_output" in handed_back
    assert res.ok and soma.get_node("stage:S1:1:planbeaver") is not None


# ── a stage's own tool may take its own parameters ─────────────────────────

def test_a_hook_with_its_own_parameters_is_offered_and_called_with_them():
    soma = _soma()
    seen = {}

    def query(a):
        seen.update(a)
        return '[{"contract_id": "DNA-001"}]'

    schema = {"type": "object", "properties": {"query": {"type": "string"},
              "family": {"type": "string"}, "limit": {"type": "integer"}},
              "required": ["query"]}
    ec = _client([
        [_call(1, "get_receptor_config", {"stage": "scout"}),
         _call(2, "query_invariants", {"query": "parse config", "family": "FAILURE_MODES",
                                       "limit": 5})],
        [_call(3, "create_soma_node", {"node_type": "StageOutput", "stage": "scout",
                                       "output": "{}"})]])
    ec.call_stage("scout", session_id="S1", chatset_id="S1", soma=soma,
                  tool_hooks={"query_invariants": ("Query laws.", query, schema)})
    offered = {t["function"]["name"]: t["function"]["parameters"]
               for t in ec.sent[0][1]["tools"]}
    assert offered["query_invariants"] == schema
    assert seen == {"query": "parse config", "family": "FAILURE_MODES", "limit": 5}


def test_one_law_is_one_contract_even_when_the_corpus_holds_it_per_repo(monkeypatch):
    import machinetalk.invariants as inv
    import machinetalk.engine as engine_mod

    class FakeCorpus:
        def __init__(self, *_a, **_k): pass
        def search(self, text, **kw):
            return [{"statement": "must_validate_path", "family": "FAILURE_MODES",
                     "repo": "a", "scope": "x", "composite": 0.5},
                    {"statement": "must_validate_path", "family": "FAILURE_MODES",
                     "repo": "b", "scope": "x", "composite": 0.5},
                    {"statement": "must_close_file", "family": "FAILURE_MODES",
                     "repo": "a", "scope": "x", "composite": 0.4}]
        def close(self): pass
    monkeypatch.setattr(inv, "LawCorpus", FakeCorpus)
    ids = {}
    out = engine_mod.Engine._query_invariants(SimpleNamespace(), "x", ids=ids)
    assert [c["contract_id"] for c in out] == ["DNA-001", "DNA-002"]
    again = engine_mod.Engine._query_invariants(SimpleNamespace(), "x", ids=ids)
    assert [c["contract_id"] for c in again] == ["DNA-001", "DNA-002"]


# ── a misquote is handed back with the failing text, and the model fixes it ──

from tool_fake import tool_calls_for  # noqa: E402


def _distiller_output(chat_hash, quote):
    return json.dumps({"distiller_output": {
        "chatset_hash": chat_hash, "skip": False, "skip_reason": None, "exfil_type": None,
        "directives": [{"text": quote, "turn": 0, "status": "ACTIVE",
                        "superseded_by": None, "duplicate_of": None}],
        "constraints": [], "preferences": [], "questions": []}})


def test_a_misquote_is_handed_back_with_the_failing_text_and_the_fix_is_accepted(monkeypatch):
    monkeypatch.setattr("machinetalk.client.BEAVER_WRITE_RETRIES", 2)   # rewrites on: this tests the rewrite
    from tool_fake import scripted_engine, seeded_soma
    soma, cid, h, raw = seeded_soma([{"role": "USER", "content": "Build a CLI. Never shell out to curl."}])
    bad = tool_calls_for("distiller", _distiller_output(h, "Build me a command line tool"), cid)
    good = tool_calls_for("distiller", _distiller_output(h, "Build a CLI."), cid)
    ec = scripted_engine([bad, [good[2]]], chatset_id=cid)
    res = ec.call_stage("distiller", session_id="S1", chatset_id=cid, soma=soma,
                        raw_chat_set=raw, expected_hash=h)
    handed_back = [m["content"] for m in ec.sent[1][0] if m["role"] == "tool"][-1]
    assert "fabricated_quote" in handed_back and "Build me a command line tool" in handed_back
    assert res.ok and res.record.rejections == 1
    assert soma.get_node(f"stage:{cid}:distiller") is not None


def test_a_misquote_the_model_never_fixes_still_fails_on_provenance():
    from tool_fake import scripted_engine, seeded_soma
    soma, cid, h, raw = seeded_soma([{"role": "USER", "content": "Build a CLI."}])
    bad = tool_calls_for("distiller", _distiller_output(h, "something never said"), cid)
    res = scripted_engine([bad], chatset_id=cid).call_stage(
        "distiller", session_id="S1", chatset_id=cid, soma=soma, raw_chat_set=raw,
        expected_hash=h)
    assert not res.ok and any("fabricated_quote" in e for e in res.record.gate.errors)
    assert soma.get_node(f"stage:{cid}:distiller") is None
