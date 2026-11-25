"""A failed-provenance skip is a failure; the bridge stores Python's numbers."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from machinetalk.gate import GateResult, is_done  # noqa: E402
from machinetalk.hashes import chatset_hash_for  # noqa: E402
from machinetalk.soma_bridge import SomaBridge  # noqa: E402
from store import Soma  # noqa: E402

RAW = [{"role": "USER", "content": "drop the full script."},
       {"role": "AGENT", "content": "Here is the code:\n```python\nprint('hi')\n```"}]
H = chatset_hash_for(RAW)


def call(reply, raw=None):
    """Run the distiller by tool call against a Soma seeded with ``raw``."""
    from tool_fake import scripted_engine, seeded_soma
    soma, chatset_id, chatset_hash, raw = seeded_soma(
        raw or RAW, session_id="s1", chatset_id="s1:2")
    ec = scripted_engine(content=json.dumps(reply), stage="distiller",
                         chatset_id=chatset_id, model_id="stub/model")
    return ec.call_stage("distiller", session_id="s1", chatset_id=chatset_id,
                         soma=soma, raw_chat_set=raw, expected_hash=chatset_hash)


def out(raw=None, **kw):
    base = {"chatset_hash": chatset_hash_for(raw or RAW), "skip": False, "skip_reason": None, "exfil_type": None,
            "directives": [], "constraints": [], "preferences": [], "questions": []}
    base.update(kw)
    return {"distiller_output": base}


class IsDoneTests(unittest.TestCase):
    def test_clean_skip_is_done(self):
        self.assertTrue(is_done(GateResult(ok=True, stage="distiller", skipped=True)))

    def test_skip_with_errors_is_not_done(self):
        g = GateResult(ok=False, stage="distiller", skipped=True, errors=["fabricated_quote"])
        self.assertFalse(is_done(g))

    def test_plain_pass_and_plain_fail(self):
        self.assertTrue(is_done(GateResult(ok=True, stage="distiller")))
        self.assertFalse(is_done(GateResult(ok=False, stage="distiller")))


class SkipBypassTests(unittest.TestCase):
    def test_skip_with_a_quote_from_the_wrong_turn_fails(self):
        # The text exists, but in turn 0, not turn 1: provenance rejects it.
        res = call(out(skip=True, skip_reason="NOISE_THRESHOLD", exfil_type="ARCHIVE",
                       directives=[{"text": "drop the full script.", "turn": 1}]))
        self.assertFalse(res.ok)
        self.assertEqual(res.record.status, "fail")

    def test_skip_with_an_invented_quote_fails(self):
        res = call(out(skip=True, skip_reason="NO_SIGNAL", exfil_type="ARCHIVE",
                       directives=[{"text": "never said this", "turn": 0}]))
        self.assertFalse(res.ok)
        self.assertIn("fabricated_quote", res.record.gate.reasons)

    def test_clean_skip_still_passes(self):
        res = call(out(skip=True, skip_reason="STATIC_ONLY", exfil_type="LESSON_EXTRACT"))
        self.assertTrue(res.ok)
        self.assertTrue(res.skipped)

    def test_good_output_gets_python_numbers_and_offsets(self):
        res = call(out(directives=[{"text": "drop the full script.", "turn": 0}]))
        self.assertTrue(res.ok)
        body = res.parsed["distiller_output"]
        want = sum(len(m["content"].encode("utf-8")) for m in RAW)
        self.assertEqual(body["bytes"]["original"], want)
        self.assertEqual(body["bytes"]["noise"] + body["bytes"]["signal"], want)
        d = body["directives"][0]
        self.assertEqual((d["char_start"], d["char_end"]), (0, len("drop the full script.")))


class BridgeTests(unittest.TestCase):
    def test_numbers_hash_and_offsets_land_in_the_graph(self):
        res = call(out(intent_anchor="drop the full script.",
                       directives=[{"text": "drop the full script.", "turn": 0}]))
        soma = Soma()
        counts = SomaBridge(soma).write_distilled("s1", "s1:2", 2, res.record.gate, "stub/model", 12.0)
        node = soma.get_node("distilled-s1:2").properties
        body = res.parsed["distiller_output"]
        self.assertEqual(node["chatset_hash"], H)
        self.assertEqual(node["intent"], "drop the full script.")
        self.assertEqual(node["bytes_original"], body["bytes"]["original"])
        self.assertEqual(node["bytes_signal"], body["bytes"]["signal"])
        self.assertEqual(node["bytes_noise"], body["bytes"]["noise"])
        self.assertEqual(node["bytes_distilled"], body["bytes"]["distilled"])
        self.assertEqual(node["signal_quality"], body["signal_quality"])
        self.assertIsNotNone(node["compaction_ratio"])
        self.assertEqual(counts.get("Directive"), 1)
        directive = soma.get_node("directive-s1:2-0").properties
        self.assertEqual((directive["turn"], directive["char_start"], directive["char_end"]),
                         (0, 0, len("drop the full script.")))
        noise = soma.get_node("noise-s1:2").properties
        self.assertEqual(noise["total_bytes_noise"], body["bytes"]["noise"])
        self.assertEqual(noise["total_bytes_signal"], body["bytes"]["signal"])


FULL_RAW = [
    {"role": "USER", "content": "build a pong game in python. no pip packages, stdlib only. "
                                "i prefer dark mode. how do i install it? "
                                "use go instead of python. ok that works."},
    {"role": "AGENT", "content": "Here you go:\ncat <<EOF > pong.go\npackage main\nEOF\nDone."},
]


def entry_obj(text, turn, status="ACTIVE", **kw):
    return {"text": text, "turn": turn, "status": status, "superseded_by": None,
            "duplicate_of": None, **kw}


class NothingDroppedTests(unittest.TestCase):
    """Every array and object in the TB-001 output contract reaches the graph."""

    def setUp(self):
        self.reply = out(
            FULL_RAW,
            directives=[entry_obj("build a pong game in python", 0, "SUPERSEDED", superseded_by=0),
                        entry_obj("use go instead of python", 0)],
            constraints=[entry_obj("no pip packages, stdlib only", 0)],
            preferences=[entry_obj("i prefer dark mode", 0)],
            questions=[entry_obj("how do i install it?", 0, "OPEN")],
            corrections=[entry_obj("use go instead of python", 0)],
            confirmations=[entry_obj("ok that works", 0, "CONFIRMED")],
            artifacts=[{"artifact_type": "HEREDOC", "turn": 1,
                        "text": "cat <<EOF > pong.go\npackage main\nEOF"}],
            stack_inferred={"language": "go", "framework": None, "confidence": 1.0,
                            "evidence": "EXPLICIT"},
            chronology_resolved=[{"topic": "language", "winner_turn": 0, "loser_turns": [0],
                                  "rule": "last_user_statement_wins"}])
        self.res = call(self.reply, FULL_RAW)
        self.soma = Soma()
        self.counts = SomaBridge(self.soma).write_distilled(
            "s1", "s1:2", 2, self.res.record.gate, "stub/model", 1.0)

    def test_it_passed_provenance(self):
        self.assertTrue(self.res.ok, self.res.record.gate.reasons)

    def test_every_entry_became_a_node(self):
        body = self.reply["distiller_output"]
        for key, node_type in (("directives", "Directive"), ("constraints", "Constraint"),
                               ("preferences", "Preference"), ("questions", "Question"),
                               ("corrections", "Correction"), ("confirmations", "Confirmation"),
                               ("artifacts", "Artifact")):
            self.assertEqual(self.counts.get(node_type), len(body[key]), key)
        self.assertEqual(self.counts.get("Stack"), 1)
        self.assertEqual(self.counts.get("Chronology"), 1)
        self.assertEqual(self.counts.get("NoiseSignature"), 1)

    def test_entry_fields_survive(self):
        d0 = self.soma.get_node("directive-s1:2-0").properties
        self.assertEqual((d0["status"], d0["superseded_by"]), ("SUPERSEDED", 0))
        c = self.soma.get_node("confirmation-s1:2-0").properties
        self.assertEqual(c["status"], "CONFIRMED")
        self.assertEqual(c["char_end"] - c["char_start"], len("ok that works"))

    def test_artifact_keeps_type_turn_and_offsets(self):
        a = self.soma.get_node("artifact-s1:2-0").properties
        self.assertEqual((a["artifact_type"], a["turn"]), ("HEREDOC", 1))
        self.assertEqual(a["char_end"] - a["char_start"], len(a["text"]))

    def test_stack_and_chronology_fields(self):
        s = self.soma.get_node("stack-s1:2").properties
        self.assertEqual((s["language"], s["evidence"]), ("go", "EXPLICIT"))
        ch = self.soma.get_node("chrono-s1:2-0").properties
        self.assertEqual((ch["winner_turn"], ch["loser_turns"], ch["rule"]),
                         (0, "[0]", "last_user_statement_wins"))

    def test_intent_is_the_first_active_directive(self):
        node = self.soma.get_node("distilled-s1:2").properties
        self.assertEqual(node["intent"], "use go instead of python")

    def test_signal_turns_are_stored(self):
        node = self.soma.get_node("distilled-s1:2").properties
        self.assertEqual(json.loads(node["turns_signal"]), [0, 1])


if __name__ == "__main__":
    unittest.main()
