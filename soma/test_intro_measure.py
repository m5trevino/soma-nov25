"""Tests for the introbeaver arithmetic that moved from the model to Python.

Expected values are worked by hand from TB-004 (SCORING, NOISE_DEFINITIONS,
TYPE_MAP, VERDICT_RULES) and from its EXEMPLAR input, not read back from the
code under test.
"""
import copy
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from machinetalk import intro_measure as im  # noqa: E402
from machinetalk.gate import GateResult  # noqa: E402

D = Decimal

# TB-004 EXEMPLAR input.
RAW = [
    {"role": "USER", "content": "Hey! Build a zero-dependency Python CLI. Never shell out to curl."},
    {"role": "AGENT", "content": "Understood. Drafting the spec now."},
    {"role": "USER", "content": "Actually, should we add a --json flag?"},
    {"role": "USER", "content": "ok thanks, that's all"},
]


def entry(i, msg, quote, kind, nf, obj, sentence=0):
    """What the model now sends: language only, no numbers, no hash."""
    return {"entry_id": f"E{i}", "source_msg_index": msg, "sentence_index": sentence,
            "verbatim_text": quote, "instruction_type": kind,
            "type_evidence": [], "normalized_form": nf, "derivation_rule": "R1",
            "object_phrase": obj, "deictic_dependencies": [], "flags": ["UNSIGNED"]}


def exemplar_output():
    return {"introbeaver_output": {
        "session_id": "s", "input_hash": "h", "signature": "EMPTY",
        "entries": [
            entry(1, 0, "Build a zero-dependency Python CLI.", "DIRECTIVE",
                  "build zero-dependency python cli", "zero-dependency Python CLI", 1),
            entry(2, 0, "Never shell out to curl.", "CONSTRAINT",
                  "never shell out to curl", "curl", 2),
            entry(3, 2, "should we add a --json flag?", "QUESTION",
                  "query: add --json flag", "--json flag")],
        "artifacts": [], "discarded": [], "warnings": ["SIGNATURE_ABSENT"], "errors": []}}


def gate_for(parsed):
    return GateResult(ok=True, stage="introbeaver", parsed=parsed)


class SegmentAndNoiseTests(unittest.TestCase):
    def test_segments_split_at_sentence_ends_and_newlines(self):
        self.assertEqual(im.segments("Hey! Build it. Never stop.\nok"),
                         ["Hey!", "Build it.", "Never stop.", "ok"])

    def test_exemplar_signal_quality_and_noise_signature(self):
        # 22 USER words; noise segments "Hey!" (1) and "ok thanks, that's all" (4):
        # (22 - 5) / 22 = .7727 -> .773
        m = im.noise_measure(RAW)
        self.assertEqual(m["total_words"], 22)
        self.assertEqual(m["signal_quality"], D("0.773"))
        self.assertEqual(m["noise_signature"], {
            "greeting": 1, "apology": 0, "acknowledgment": 1, "closing": 1,
            "filler": 0, "meta_question": 0, "noise_words": 5})

    def test_a_segment_that_fired_a_trigger_is_never_noise(self):
        # "like" is a filler word AND a PREFERENCE trigger: it fired, so it is signal.
        m = im.noise_measure([{"role": "USER", "content": "I like the blue one."}])
        self.assertEqual(m["noise_signature"]["filler"], 0)
        self.assertEqual(m["signal_quality"], D("1.000"))

    def test_agent_text_is_not_scored(self):
        m = im.noise_measure([{"role": "AGENT", "content": "hello hello hello"}])
        self.assertEqual(m["total_words"], 0)

    def test_no_user_words_gives_zero_not_a_crash(self):
        self.assertEqual(im.noise_measure([])["signal_quality"], D("0.000"))


class ConfidenceTests(unittest.TestCase):
    def test_the_formula(self):
        # .40*.9 + .30*1 + .20*1 + .10*1 - 0 = .96
        c = {"trigger_strength": .9, "anchor_integrity": 1, "type_unanimity": 1,
             "scope_clarity": 1, "hedge_penalty": 0}
        self.assertEqual(im.confidence(c), D("0.96"))

    def test_ceiling_floor_and_truncation(self):
        top = {"trigger_strength": 1, "anchor_integrity": 1, "type_unanimity": 1,
               "scope_clarity": 1, "hedge_penalty": 0}
        self.assertEqual(im.confidence(top), D("0.99"))
        zero = {k: 0 for k in top} | {"hedge_penalty": 0.1}
        self.assertEqual(im.confidence(zero), D("0.00"))
        # .40*.6247 + .3 + .2 + .1 = .84988 -> truncates to .84, never rounds to .85
        c = dict(top, trigger_strength=0.6247)
        self.assertEqual(im.confidence(c), D("0.84"))

    def test_incomplete_components_are_undefined(self):
        self.assertIsNone(im.confidence({"trigger_strength": 0.9}))

    def test_trigger_strength_rows(self):
        ts = im.trigger_strength
        self.assertEqual(ts("CONSTRAINT", "Never shell out"), D("1.00"))
        self.assertEqual(ts("DIRECTIVE", "You must build it"), D("1.00"))
        self.assertEqual(ts("DIRECTIVE", "Build it"), D("0.90"))
        self.assertEqual(ts("DIRECTIVE", "please build it"), D("0.90"))
        self.assertEqual(ts("PREFERENCE", "it should be fast"), D("0.70"))
        self.assertEqual(ts("PREFERENCE", "I prefer tabs"), D("0.50"))
        self.assertEqual(ts("QUESTION", "what is it?"), D("0.60"))

    def test_an_unlisted_trigger_uses_the_weakest_row_of_its_type(self):
        self.assertEqual(im.trigger_strength("DIRECTIVE", "we need to add a flag"), D("0.90"))
        self.assertEqual(im.trigger_strength("CONSTRAINT", "avoid globals"), D("1.00"))

    def test_scope_clarity(self):
        self.assertEqual(im.scope_clarity("zero-dependency Python CLI"), D("1.00"))
        self.assertEqual(im.scope_clarity("it"), D("0.60"))
        self.assertEqual(im.scope_clarity("the same"), D("0.60"))
        self.assertEqual(im.scope_clarity(""), D("0.40"))
        self.assertEqual(im.scope_clarity(None), D("0.40"))

    def test_type_unanimity(self):
        self.assertEqual(im.type_unanimity(1), D("1.00"))
        self.assertEqual(im.type_unanimity(2), D("0.70"))
        self.assertEqual(im.type_unanimity(3), D("0.50"))
        self.assertIsNone(im.type_unanimity(0))

    def test_hedge_and_anchor_length(self):
        e = entry(1, 0, "I think we should build it", "DIRECTIVE", "build it", "it")
        c = im.components_for(e)
        self.assertEqual(c["hedge_penalty"], D("0.10"))
        short = entry(2, 0, "Build it", "DIRECTIVE", "build it", "it")
        self.assertEqual(im.components_for(short)["anchor_integrity"], D("0.00"))


class KeyAndChronologyTests(unittest.TestCase):
    def test_intent_key_is_16_hex_and_stable(self):
        k = im.intent_key("build zero-dependency python cli")
        self.assertRegex(k, r"^[0-9a-f]{16}$")
        self.assertEqual(k, im.intent_key("Build  Zero-Dependency Python CLI"))

    def test_polarity_does_not_change_the_key_but_is_recorded(self):
        self.assertEqual(im.intent_key("use sqlite"), im.intent_key("never use sqlite"))
        self.assertEqual(im.polarity_set("never use sqlite"), ["never"])
        self.assertEqual(im.polarity_set("use sqlite"), [])

    def test_different_content_gives_a_different_key(self):
        self.assertNotEqual(im.intent_key("use sqlite"), im.intent_key("use postgres"))

    def test_query_prefix_is_not_part_of_the_key(self):
        self.assertEqual(im.intent_key("query: add --json flag"), im.intent_key("add --json flag"))

    def test_a_later_message_supersedes_an_earlier_one(self):
        a = entry(1, 0, "Use SQLite for it.", "DIRECTIVE", "use sqlite", "sqlite")
        b = entry(2, 2, "Never use SQLite.", "CONSTRAINT", "never use sqlite", "sqlite")
        raw = [{"role": "USER", "content": "Use SQLite for it."},
               {"role": "AGENT", "content": "ok"},
               {"role": "USER", "content": "Never use SQLite."}]
        g = gate_for({"introbeaver_output": {"entries": [a, b], "artifacts": []}})
        im.apply(g, raw)
        out = g.parsed["introbeaver_output"]["entries"]
        self.assertEqual(out[0]["entry_status"], "SUPERSEDED")
        self.assertEqual(out[0]["superseded_by"], "E2")
        self.assertEqual(out[1]["entry_status"], "ACTIVE")
        self.assertEqual(out[1]["supersedes"], "E1")
        chrono = g.parsed["introbeaver_output"]["chronology_resolved"]
        self.assertEqual(len(chrono), 1)
        self.assertEqual((chrono[0]["winner"], chrono[0]["losers"], chrono[0]["rule"]),
                         ("E2", ["E1"], "last_user_statement_wins"))

    def test_a_repeat_inside_one_message_is_flagged_not_superseded(self):
        a = entry(1, 0, "Use SQLite.", "DIRECTIVE", "use sqlite", "sqlite", 0)
        b = entry(2, 0, "Use SQLite please.", "DIRECTIVE", "use sqlite", "sqlite", 1)
        raw = [{"role": "USER", "content": "Use SQLite. Use SQLite please."}]
        g = gate_for({"introbeaver_output": {"entries": [a, b], "artifacts": []}})
        im.apply(g, raw)
        out = g.parsed["introbeaver_output"]["entries"]
        self.assertNotIn("DUPLICATE_OF", out[0]["flags"])
        self.assertIn("DUPLICATE_OF", out[1]["flags"])
        self.assertEqual({e["entry_status"] for e in out}, {"ACTIVE"})


class ApplyExemplarTests(unittest.TestCase):
    def setUp(self):
        self.gate = gate_for(exemplar_output())
        self.m = im.apply(self.gate, RAW)
        self.b = self.gate.parsed["introbeaver_output"]
        self.e = self.b["entries"]

    def test_counts(self):
        self.assertEqual((self.b["user_message_count"], self.b["extracted_count"]), (3, 3))
        self.assertEqual(self.b["entry_count_by_type"],
                         {"DIRECTIVE": 1, "CONSTRAINT": 1, "PREFERENCE": 0, "QUESTION": 1})
        self.assertEqual(sum(self.b["entry_count_by_type"].values()), self.b["extracted_count"])

    def test_noise(self):
        self.assertEqual(self.b["signal_quality"], 0.773)
        self.assertEqual(self.b["noise_signature"]["noise_words"], 5)

    def test_confidence_matches_the_exemplar_where_the_spec_is_consistent(self):
        self.assertEqual(self.e[0]["confidence"], 0.96)
        self.assertEqual(self.e[1]["confidence"], 0.99)

    def test_the_question_scores_lower_than_the_exemplar_by_the_literal_type_map(self):
        # The exemplar shows .78 (2 classes fired). By TYPE_MAP, "add" is also a
        # DIRECTIVE token, so 3 classes fire: unanimity .50, so
        # .40*.60 + .30*1 + .20*.50 + .10*1 - 0 = .74.
        self.assertEqual(self.e[2]["confidence"], 0.74)
        self.assertEqual(self.e[2]["confidence_components"]["type_unanimity"], 0.5)

    def test_labels_that_follow_from_confidence_and_the_question(self):
        self.assertEqual([x["downstream_action"] for x in self.e],
                         ["BLOCKED_PENDING_ANSWER", "BLOCKED_PENDING_ANSWER", "ANSWER_REQUIRED"])
        self.assertEqual(self.e[0]["flags"], ["UNSIGNED"])
        self.assertEqual(self.e[2]["flags"], ["UNSIGNED", "UNCERTAIN"])
        self.assertEqual({x["entry_status"] for x in self.e}, {"ACTIVE"})

    def test_status_follows_verdict_rule_3(self):
        self.assertEqual((self.b["status"], self.b["block_reason"], self.b["skip_reason"]),
                         ("QUARANTINE", "UNRESOLVED_QUESTION", None))
        self.assertEqual(self.gate.verdict, "QUARANTINE")

    def test_every_entry_has_a_real_hex_key(self):
        for x in self.e:
            self.assertRegex(x["intent_key"], r"^[0-9a-f]{16}$")

    def test_apply_is_idempotent(self):
        once = copy.deepcopy(self.gate.parsed)
        im.apply(self.gate, RAW)
        self.assertEqual(self.gate.parsed, once)


class VerdictRuleTests(unittest.TestCase):
    def run_apply(self, raw, entries=None):
        g = gate_for({"introbeaver_output": {"entries": entries or [], "artifacts": []}})
        im.apply(g, raw)
        return g.parsed["introbeaver_output"], g

    def test_rule_1_no_user_messages(self):
        b, _ = self.run_apply([{"role": "AGENT", "content": "hello"}])
        self.assertEqual((b["status"], b["skip_reason"]), ("SKIP", "NO_USER_MESSAGES"))

    def test_rule_4_no_entries_and_low_signal(self):
        # all noise: signal_quality 0.000 < .35
        b, _ = self.run_apply([{"role": "USER", "content": "ok thanks"}])
        self.assertEqual((b["status"], b["skip_reason"]), ("SKIP", "NO_SIGNAL"))

    def test_rule_5_no_entries_but_signal(self):
        # plain prose, no trigger and no noise: signal 1.000 >= .35
        b, _ = self.run_apply([{"role": "USER", "content": "The sky was grey today."}])
        self.assertEqual((b["status"], b["skip_reason"]), ("SKIP", "NO_TRIGGER_MATCH"))

    def test_rule_6_pass(self):
        e = [entry(1, 0, "Build a small tool.", "DIRECTIVE", "build small tool", "small tool")]
        b, g = self.run_apply([{"role": "USER", "content": "Build a small tool."}], e)
        self.assertEqual((b["status"], b["block_reason"], b["skip_reason"]), ("PASS", None, None))
        self.assertEqual(b["entries"][0]["downstream_action"], "PROCEED")

    def test_low_confidence_entry_is_quarantined_and_uncertain(self):
        # a 2-word quote scores anchor_integrity 0: .36 + 0 + .2 + .1 = .66 -> UNCERTAIN,
        # then hedge: "might" -> .56; still >= .50, so only UNCERTAIN + HUMAN_REVIEW
        e = [entry(1, 0, "might build", "DIRECTIVE", "might build", "")]
        b, g = self.run_apply([{"role": "USER", "content": "might build"}], e)
        x = b["entries"][0]
        self.assertEqual(x["confidence"], 0.0 + float(D("0.40") * D("0.90") + D("0.20") * 1
                                                      + D("0.10") * D("0.40") - D("0.10")))
        self.assertIn("UNCERTAIN", x["flags"])
        self.assertTrue(any("anchor_integrity 0.00" in w for w in g.warnings))


class ArtifactAndSafetyTests(unittest.TestCase):
    def test_artifact_offsets_are_found_by_python(self):
        raw = [{"role": "USER", "content": "see\n- must a\n- never b\nend"}]
        art = {"artifact_type": "CONSTRAINT_BLOCK", "source_msg_index": 0,
               "verbatim_text": "- must a\n- never b"}
        g = gate_for({"introbeaver_output": {"entries": [], "artifacts": [art]}})
        im.apply(g, raw)
        a = g.parsed["introbeaver_output"]["artifacts"][0]
        self.assertEqual((a["char_start"], a["char_end"]), (4, 22))

    def test_an_error_object_without_entries_is_left_alone(self):
        g = gate_for({"error": "INVALID_INPUT", "halt": True})
        before = copy.deepcopy(g.parsed)
        self.assertFalse(im.apply(g, RAW)["applied"])
        self.assertEqual(g.parsed, before)

    def test_the_models_wrong_numbers_are_overwritten_and_reported(self):
        out = exemplar_output()
        b = out["introbeaver_output"]
        b.update(user_message_count=99, extracted_count=7, signal_quality=0.1,
                 status="PASS")
        b["entries"][0].update(confidence=0.5, intent_key="a1b2c3d4e5f6g7h8")
        g = gate_for(out)
        im.apply(g, RAW)
        self.assertEqual(g.parsed["introbeaver_output"]["user_message_count"], 3)
        self.assertEqual(g.parsed["introbeaver_output"]["entries"][0]["confidence"], 0.96)
        self.assertRegex(g.parsed["introbeaver_output"]["entries"][0]["intent_key"],
                         r"^[0-9a-f]{16}$")
        text = " | ".join(g.warnings)
        self.assertIn("user_message_count 99 -> 3", text)
        self.assertIn("status 'PASS' -> 'QUARANTINE'", text)


if __name__ == "__main__":
    unittest.main()
