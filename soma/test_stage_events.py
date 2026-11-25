"""Tests for the live stage status: real values, shown at the moment they happen.

The point of these events is that the operator watches the work as it happens
instead of reading a dump after the call. So the tests check order against the
model call itself, and check that each value is the real one.
"""
import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from machinetalk import engine as eng  # noqa: E402
from machinetalk import progress  # noqa: E402
from machinetalk.client import EngineClient  # noqa: E402
from machinetalk.gate import GateResult  # noqa: E402
from machinetalk.hashes import chatset_hash_for  # noqa: E402

RAW = [{"role": "USER", "content": "Hey! Build a zero-dependency Python CLI. Never shell out to curl."},
       {"role": "AGENT", "content": "Understood. Drafting the spec now."},
       {"role": "USER", "content": "Actually, should we add a --json flag?"},
       {"role": "USER", "content": "ok thanks, that's all"}]
H = chatset_hash_for(RAW)
BODY = "stub instruction body " * 10          # a known length


def entry(i, m, s, q, k, nf, obj):
    return {"entry_id": f"TB004-s-{i:04d}", "source_msg_index": m, "sentence_index": s,
            "verbatim_text": q, "instruction_type": k, "type_evidence": [],
            "normalized_form": nf, "derivation_rule": "R1", "object_phrase": obj,
            "deictic_dependencies": [], "flags": ["UNSIGNED"]}


GOOD = {"introbeaver_output": {
    "session_id": "s", "timestamp_utc": "t", "input_hash": H, "signature": "EMPTY",
    "entries": [entry(1, 0, 1, "Build a zero-dependency Python CLI.", "DIRECTIVE",
                      "build zero-dependency python cli", "zero-dependency Python CLI"),
                entry(2, 0, 2, "Never shell out to curl.", "CONSTRAINT",
                      "never shell out to curl", "curl"),
                entry(3, 2, 0, "should we add a --json flag?", "QUESTION",
                      "query: add --json flag", "--json flag")],
    "artifacts": [], "discarded": [], "warnings": [], "errors": []}}

COMPLAINT = {"error": "INVALID_INPUT", "missing_fields": ["raw_chat_set"], "halt": True}


def seeded():
    """A Soma seeded with the chat set; the stage's instruction row replaced
    with a known body so the test can recognise its length and version."""
    from tool_fake import seeded_soma, set_instruction
    soma, chatset_id, chatset_hash, _raw = seeded_soma(RAW, session_id="s",
                                                       chatset_id="cs")
    set_instruction(soma, "introbeaver", BODY)
    soma.conn.execute("UPDATE stage_instructions SET tb_number='tb-004', version=3 "
                      "WHERE stage_name='introbeaver'")
    soma.conn.commit()
    return soma, chatset_hash


def make_client(reply, log, chatset_id="cs"):
    from tool_fake import tool_calls_for
    c = EngineClient.__new__(EngineClient)

    def fake_call(*a, **k):
        log.append("CALL")                       # the moment the model is called
        return types.SimpleNamespace(
            tool_calls=tool_calls_for("introbeaver", json.dumps(reply), chatset_id),
            content="", model_id="stub-model", prompt_tokens=4528,
            completion_tokens=33, total_tokens=4561, cache_hit_tokens=0,
            cache_miss_tokens=0, chars_in=1, latency_ms=9921,
            engine_session="SES-007", engine_stage="IB-72", log_link="")
    c._call_with_retry = fake_call
    return c


def run(reply, emit, log):
    soma, chatset_hash = seeded()
    return make_client(reply, log).call_stage(
        "introbeaver", session_id="s", chatset_id="cs", soma=soma,
        raw_chat_set=RAW, expected_hash=chatset_hash, emit=emit)


class LiveEventTests(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.events = []

        def emit(label, text):
            self.log.append(label)
            self.events.append((label, text))
        self.res = run(GOOD, emit, self.log)

    def text(self, label):
        return next(t for l, t in self.events if l == label)

    def test_every_real_step_is_reported_in_order(self):
        self.assertEqual([l for l, _ in self.events],
                         ["request", "instructions", "input", "write", "response",
                          "gate", "anchors", "python", "soma"])

    def test_events_are_live_not_batched_after_the_call(self):
        # request is shown BEFORE the model is called; the tool calls the model
        # makes (read_instructions, read_input, write_output) and the response
        # are shown AFTER it.
        i = self.log.index("CALL")
        self.assertEqual(self.log[:i], ["request"])
        self.assertEqual(self.log[i + 1:i + 6],
                         ["instructions", "input", "write", "response", "gate"])

    def test_instruction_line_carries_the_real_row(self):
        t = self.text("instructions")
        self.assertIn("stage_instructions[introbeaver]", t)
        self.assertIn("tb-004 v3", t)
        self.assertIn(f"{len(BODY):,} bytes", t)

    def test_input_line_names_what_was_read_and_its_size(self):
        t = self.text("input")
        self.assertIn("model called get_chatset", t)
        served = self.res.record.user_msg          # what the Soma returned to the model
        self.assertIn(f"{len(served):,} chars", t)
        self.assertEqual(json.loads(served)["raw_chat_set"], RAW)

    def test_response_line_is_the_replys_real_numbers(self):
        t = self.text("response")
        self.assertIn("stub-model replied", t)
        self.assertIn(f"{len(json.dumps(GOOD)):,} chars", t)
        self.assertIn("tokens in 4,528 / out 33", t)
        self.assertIn("9.9s", t)
        self.assertIn("SES-007/IB-72", t)

    def test_anchors_and_python_lines(self):
        self.assertIn("3/3 quotes found in the transcript", self.text("anchors"))
        py = self.text("python")
        self.assertIn("3 entries", py)
        self.assertIn("signal quality 0.773", py)
        self.assertIn("[0.96, 0.99, 0.74]", py)

    def test_the_call_result_is_unchanged_by_emitting(self):
        self.assertTrue(self.res.ok)


class RobustnessTests(unittest.TestCase):
    def test_a_broken_display_never_fails_the_stage(self):
        def boom(label, text):
            raise RuntimeError("terminal exploded")
        res = run(GOOD, boom, [])
        self.assertTrue(res.ok)

    def test_no_emit_at_all_still_works(self):
        self.assertTrue(run(GOOD, None, []).ok)

    def test_a_model_complaint_is_shown_in_its_own_words(self):
        events = []
        res = run(COMPLAINT, lambda l, t: events.append((l, t)), [])
        self.assertFalse(res.ok)
        d = dict(events)
        self.assertIn("FAIL", d["gate"])
        self.assertIn("INVALID_INPUT", d["model said"])
        self.assertIn("raw_chat_set", d["model said"])
        self.assertNotIn("anchors", d)            # no claim about steps that did not run
        self.assertNotIn("python", d)


class DisplayTests(unittest.TestCase):
    def test_stage_event_line(self):
        s = progress.stage_event("introbeaver", "s", "anchors", "3/3 ok", session_code="SES-007")
        self.assertIn("[SES-007]", s)
        self.assertIn("anchors", s)
        self.assertIn("3/3 ok", s)

    def test_session_link_comes_from_the_stage_link(self):
        link = "https://engine.save-aichats.com/v1/soma/?session=SES-007&slug=introbeaver&stage=IB-72"
        self.assertEqual(progress.session_link(link),
                         "https://engine.save-aichats.com/v1/soma/?session=SES-007")
        self.assertEqual(progress.session_link(""), "")
        self.assertEqual(progress.session_link("https://x/y"), "")

    def test_result_block_shows_ids_links_and_the_models_words(self):
        link = "https://engine.save-aichats.com/v1/soma/?session=SES-007&slug=introbeaver&stage=IB-72"
        out = progress.telemetry_result(
            "introbeaver", elapsed=9.9, chars_in=1, chars_out=84, prompt_tokens=1,
            completion_tokens=2, total_tokens=3, model="m", provider="p",
            status="FAILED", session="s", engine_session="SES-007", engine_stage="IB-72",
            log_link=link, complaint="INVALID_INPUT · missing ['raw_chat_set'] · halt")
        import re
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)          # drop colour codes
        for needle in ("engine id   SES-007 / IB-72", f"stage log   {link}",
                       "session log https://engine.save-aichats.com/v1/soma/?session=SES-007\n",
                       "model said  INVALID_INPUT", "local log   SES-"):
            self.assertIn(needle, plain)

    def test_result_block_is_unchanged_when_nothing_extra_is_given(self):
        out = progress.telemetry_result(
            "distiller", elapsed=1.0, chars_in=1, chars_out=1, prompt_tokens=1,
            completion_tokens=1, total_tokens=2, model="m", provider="p", status="DONE")
        self.assertNotIn("engine id", out)
        self.assertNotIn("model said", out)


class EngineHelperTests(unittest.TestCase):
    def test_model_complaint(self):
        g = GateResult(ok=False, stage="introbeaver", parsed=COMPLAINT)
        self.assertEqual(eng._model_complaint(g), "INVALID_INPUT · missing ['raw_chat_set'] · halt")
        self.assertEqual(eng._model_complaint(GateResult(ok=True, stage="x", parsed={"a": 1})), "")
        self.assertEqual(eng._model_complaint(GateResult(ok=True, stage="x", parsed=None)), "")

    def test_soma_size_is_read_from_the_store(self):
        s = types.SimpleNamespace(node_count=lambda: 50, edge_count=lambda: 48)
        self.assertEqual(eng._soma_size(s), (50, 48))

    def test_soma_size_failure_is_none_not_a_made_up_number(self):
        def boom():
            raise OSError("closed")
        s = types.SimpleNamespace(node_count=boom, edge_count=boom)
        self.assertEqual(eng._soma_size(s), (None, None))


if __name__ == "__main__":
    unittest.main()
