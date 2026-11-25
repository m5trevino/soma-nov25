"""Tests for the session rule: the engine generates the id, the pipeline reuses it.

  * a session's first call sends NO session code, so the engine generates one;
  * every later call for that session sends the engine's code, that session only;
  * a new session (or a new run) starts again with no code and gets its own;
  * parallel workers never open two sessions for one chat session;
  * the local SES-007 counter and the chat-session name are never sent.
"""
import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from machinetalk.client import EngineClient  # noqa: E402
from machinetalk.engine_sessions import EngineSessions, learn  # noqa: E402
from machinetalk.hashes import chatset_hash_for  # noqa: E402

RAW = [{"role": "USER", "content": "Build a small CLI tool."}]
H = chatset_hash_for(RAW)


class FakeEngine:
    """Behaves like the engine: no pea_session -> generate; given -> keep."""

    def __init__(self):
        self.counter = 320
        self.sent = []                       # what each call carried
        self.lock = threading.Lock()

    def call(self, pea_session):
        with self.lock:
            self.sent.append(pea_session)
            if pea_session:
                return pea_session           # already assigned: no new one
            self.counter += 1
            return f"SES-{self.counter}"


class RuleTests(unittest.TestCase):
    def run_session(self, sessions, engine, key, calls):
        """One chat session: `calls` calls, following exactly the pipeline's loop."""
        got = []
        for _ in range(calls):
            with sessions.scope(key) as code:
                reply = engine.call(code)
                learn(sessions, key, code, reply, lambda t: None)
                got.append(reply)
        return got

    def test_first_call_sends_nothing_then_the_engines_code_every_time(self):
        e, s = FakeEngine(), EngineSessions()
        got = self.run_session(s, e, "chat-A", 5)
        self.assertEqual(e.sent, [None, "SES-321", "SES-321", "SES-321", "SES-321"])
        self.assertEqual(set(got), {"SES-321"})                  # one session

    def test_a_new_chat_session_gets_its_own(self):
        e, s = FakeEngine(), EngineSessions()
        a = self.run_session(s, e, "chat-A", 3)
        b = self.run_session(s, e, "chat-B", 3)
        self.assertEqual(set(a), {"SES-321"})
        self.assertEqual(set(b), {"SES-322"})
        self.assertEqual(e.sent[3], None)                        # B's first call sent nothing

    def test_a_second_run_of_the_same_chat_session_does_not_merge(self):
        e = FakeEngine()
        run1 = self.run_session(EngineSessions(), e, "chat-A", 4)     # run 1
        run2 = self.run_session(EngineSessions(), e, "chat-A", 4)     # run 2: fresh store
        self.assertEqual(set(run1), {"SES-321"})
        self.assertEqual(set(run2), {"SES-322"})
        self.assertNotEqual(set(run1), set(run2))

    def test_a_code_is_never_replaced_within_a_session(self):
        s = EngineSessions()
        self.assertEqual(s.remember("k", None, "SES-5"), "SES-5")
        self.assertIsNone(s.remember("k", None, "SES-9"))        # a later reply cannot change it
        self.assertEqual(s.get("k"), "SES-5")

    def test_a_continuation_call_teaches_nothing(self):
        s = EngineSessions()
        self.assertIsNone(s.remember("k", "SES-5", "SES-5"))
        self.assertIsNone(s.get("k"))

    def test_only_an_engine_shaped_code_is_accepted(self):
        s = EngineSessions()
        for bad in (None, "", "SES-007x", "synthesized_012456", "abc", "SES-"):
            self.assertIsNone(s.remember("k", None, bad), bad)
        self.assertIsNone(s.get("k"))

    def test_a_failed_first_call_leaves_the_next_call_to_ask_again(self):
        s = EngineSessions()
        with self.assertRaises(RuntimeError):
            with s.scope("k") as code:
                self.assertIsNone(code)
                raise RuntimeError("connection reset")
        with s.scope("k") as code:                               # lock was released
            self.assertIsNone(code)

    def test_a_missing_reply_is_reported(self):
        s, said = EngineSessions(), []
        learn(s, "chat-A", None, None, said.append)
        self.assertTrue(any("returned no session id" in x for x in said))

    def test_learning_is_announced_once(self):
        s, said = EngineSessions(), []
        learn(s, "chat-A", None, "SES-321", said.append)
        learn(s, "chat-A", "SES-321", "SES-321", said.append)
        self.assertEqual(len(said), 1)
        self.assertIn("SES-321", said[0])


class ParallelTests(unittest.TestCase):
    def test_parallel_workers_open_one_session_not_one_each(self):
        e, s = FakeEngine(), EngineSessions()
        start = threading.Barrier(6)

        def worker():
            start.wait()
            with s.scope("chat-A") as code:
                time.sleep(0.05 if code is None else 0)          # first call is slow
                reply = e.call(code)
                learn(s, "chat-A", code, reply, lambda t: None)

        ts = [threading.Thread(target=worker) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(e.sent.count(None), 1)                  # only ONE call asked for a session
        self.assertEqual(e.counter, 321)                         # the engine generated exactly one
        self.assertEqual(sorted(x for x in e.sent if x), ["SES-321"] * 5)


class ClientSendsTheRightThingTests(unittest.TestCase):
    """call_stage must pass the engine's code or nothing, never the local code
    or the chat session's name."""

    chatset_id = "synthesized_012456:1"

    def setUp(self):
        # rewrites on, so the stage takes several turns and reuses its session
        import unittest.mock
        p = unittest.mock.patch("machinetalk.client.BEAVER_WRITE_RETRIES", 2)
        p.start()
        self.addCleanup(p.stop)

    def make(self, seen):
        c = EngineClient.__new__(EngineClient)

        def fake(stage, messages, pea_session=None, temperature=None, **kw):
            seen.append(pea_session)
            from tool_fake import tool_calls_for
            return types.SimpleNamespace(
                tool_calls=tool_calls_for(stage, json.dumps({"x": 1}), self.chatset_id),
                content="", model_id="m", prompt_tokens=1,
                completion_tokens=1, total_tokens=2, cache_hit_tokens=0,
                cache_miss_tokens=0, chars_in=1, latency_ms=1,
                engine_session="SES-321", engine_stage="DI-1", log_link="")
        c._call_with_retry = fake
        return c

    def call(self, client, **kw):
        # the stage reads its chat set from a Soma that was seeded first
        from tool_fake import seeded_soma
        soma, self.chatset_id, chatset_hash, _raw = seeded_soma(
            session_id="synthesized_012456", chatset_id="synthesized_012456:1")
        return client.call_stage("distiller", session_id="synthesized_012456",
                                 chatset_id=self.chatset_id, soma=soma,
                                 expected_hash=chatset_hash, **kw)

    def test_first_call_sends_none_not_the_chat_session_name(self):
        seen = []
        self.call(self.make(seen))
        # the first request asks the engine for a session; every later turn of
        # the same stage sends back the one it made, so a stage is one session
        self.assertIsNone(seen[0])
        self.assertEqual(set(seen[1:]), {"SES-321"})

    def test_a_known_engine_code_is_sent(self):
        seen = []
        self.call(self.make(seen), session_code="SES-321")
        self.assertEqual(set(seen), {"SES-321"})

    def test_the_reply_carries_the_code_the_caller_keeps(self):
        res = self.call(self.make([]))
        self.assertEqual(res.record.engine_session, "SES-321")


if __name__ == "__main__":
    unittest.main()
