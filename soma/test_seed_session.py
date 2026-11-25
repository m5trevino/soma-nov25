"""The whole session is seeded into the Soma before any stage runs.

The seed writes the session anchor and EVERY chat set with all its turns, and it
is where each chat set's hash is assigned. Stages read the chat set from the
Soma and are checked against that hash. Seeding twice is safe: a chat set already
seeded is left alone, so results an earlier run derived from it are not wiped.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import machinetalk.engine as engine_mod
from machinetalk.hashes import chatset_hash_for
from machinetalk.soma_bridge import SomaBridge
from store import Soma


def _cs(session, n, turns):
    raw = [{"role": r, "content": c} for r, c in turns]
    return SimpleNamespace(chatset_id=f"{session}:{n}", session_id=session, set_number=n,
                           chars=sum(len(c) for _r, c in turns), raw_chat_set=raw,
                           chatset_hash="stale-hash-from-the-file", provider="p", models=["m"])


def _session():
    return [_cs("S1", 1, [("USER", "alpha one"), ("AGENT", "alpha two")]),
            _cs("S1", 2, [("USER", "beta")]),
            _cs("S1", 3, [("USER", "gamma"), ("AGENT", "gamma reply"), ("USER", "gamma again")])]


def _db():
    soma = Soma(":memory:")
    soma._persist = lambda: None
    return soma, SomaBridge(soma)


def _nodes(soma, t):
    # a fresh Soma carries the template's demo nodes; count only this session's
    return [n for n in soma.query_by_type(t) if n.properties.get("session_id") == "S1"]


def test_every_chat_set_and_every_turn_is_seeded_up_front():
    soma, bridge = _db()
    sets = _session()
    res = bridge.seed_session("S1", sets)
    assert len(_nodes(soma, "ChatSet")) == 3
    assert len(_nodes(soma, "Message")) == 6                 # 2 + 1 + 3 turns
    anchor = soma.get_node("anchor-S1").properties
    assert anchor["total_sets"] == 3
    assert anchor["total_session_chars"] == sum(c.chars for c in sets)
    contains = [e for e in soma.get_edges("anchor-S1") if e.rel_type == "CONTAINS"]
    assert len(contains) == 3
    assert res["ChatSet"] == 3 and res["Message"] == 6 and res["already_seeded"] == 0


def test_the_hash_is_assigned_at_seed_time_and_stored_on_the_chat_set_node():
    soma, bridge = _db()
    sets = _session()
    res = bridge.seed_session("S1", sets)
    for cs in sets:
        want = chatset_hash_for(cs.raw_chat_set)
        assert soma.get_node(cs.chatset_id).properties["chatset_hash"] == want
        assert res["hashes"][cs.chatset_id] == want
        # and it is what the Soma's own turns hash to
        assert chatset_hash_for(bridge.read_chat_set(cs.chatset_id)) == want


def test_seeding_twice_leaves_a_seeded_chat_set_and_its_derived_nodes_alone():
    soma, bridge = _db()
    sets = _session()
    bridge.seed_session("S1", sets)
    soma.create_node("DistilledChatSet", {"id": "distilled-S1:2", "chat_set_id": "S1:2"})
    res = bridge.seed_session("S1", sets)
    assert res["ChatSet"] == 0 and res["already_seeded"] == 3
    assert soma.get_node("distilled-S1:2") is not None       # an earlier run's result survives


def test_a_chat_set_whose_text_changed_is_re_seeded_with_a_new_hash():
    soma, bridge = _db()
    sets = _session()
    bridge.seed_session("S1", sets)
    old = soma.get_node("S1:2").properties["chatset_hash"]
    sets[1].raw_chat_set[0]["content"] = "beta, edited"
    res = bridge.seed_session("S1", sets)
    assert res["ChatSet"] == 1 and res["already_seeded"] == 2
    assert soma.get_node("S1:2").properties["chatset_hash"] != old
    assert bridge.read_chat_set("S1:2")[0]["content"] == "beta, edited"


def test_clearing_results_keeps_the_seed():
    soma, bridge = _db()
    bridge.seed_session("S1", _session())
    soma.create_node("DistilledChatSet", {"id": "distilled-S1:1", "chat_set_id": "S1:1"})
    bridge.clear_chatset_results("S1:1")
    assert soma.get_node("distilled-S1:1") is None            # derived result gone
    assert soma.get_node("S1:1") is not None                  # seed stays
    assert len(bridge.read_chat_set("S1:1")) == 2


def test_engine_seeds_the_whole_session_and_takes_the_assigned_hashes():
    soma, bridge = _db()
    engine = SimpleNamespace(_bridge=bridge, _soma=soma, _soma_lock=threading.Lock())
    sets = _session()
    session = SimpleNamespace(session_id="S1", chatsets=sets)
    res = engine_mod.Engine._seed_session(engine, session)
    assert res["ChatSet"] == 3
    for cs in sets:                                           # the stale file hash was replaced
        assert cs.chatset_hash == soma.get_node(cs.chatset_id).properties["chatset_hash"]
        assert cs.chatset_hash != "stale-hash-from-the-file"


def test_a_chat_set_called_on_its_own_is_seeded_on_demand():
    soma, bridge = _db()
    engine = SimpleNamespace(_bridge=bridge, _soma=soma, _soma_lock=threading.Lock(),
                             _session_totals={})
    cs = _session()[0]
    engine_mod.Engine._ensure_seeded(engine, cs)
    assert soma.get_node("S1:1") is not None and soma.get_node("anchor-S1") is not None
    assert cs.chatset_hash == chatset_hash_for(cs.raw_chat_set)
