"""Tests for the outrobeaver arithmetic that moved from the model to Python.

Every expected number below is worked by hand from the TB-006 formulas, not read
back from the code under test.
"""
import copy
import hashlib
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from machinetalk import outro_measure as om  # noqa: E402
from machinetalk.gate import GateResult  # noqa: E402

D = Decimal
RAW = [{"role": "USER", "content": "build a CLI tool in Python"},
       {"role": "AGENT", "content": "Standard flow: walk the directory."}]


def fp(i, kind):
    return {"turn_index": i, "type": kind, "description": "x"}


def drift(sev, i=1):
    return {"turn_index": i, "expected": "a", "actual": "b", "severity": sev}


class HashTests(unittest.TestCase):
    def test_empty_content_is_the_sha256_of_nothing(self):
        # TB-006 exemplar: e3b0c44298fc1c14 is the start of SHA-256("").
        self.assertEqual(om.content_hash(""), "e3b0c44298fc1c14")
        self.assertEqual(om.content_hash(None), "e3b0c44298fc1c14")

    def test_hash_is_of_the_content_only(self):
        want = hashlib.sha256(b"build a CLI tool in Python").hexdigest()[:16]
        self.assertEqual(om.content_hash("build a CLI tool in Python"), want)
        # same content under another role gives the same hash
        self.assertEqual(om.content_hash(RAW[0]["content"]),
                         om.content_hash(RAW[0]["content"]))

    def test_verify_passes_correct_hashes(self):
        journal = [{"turn_index": i, "content_hash": om.content_hash(t["content"])}
                   for i, t in enumerate(RAW)]
        self.assertEqual(om.verify_hashes(journal, RAW), [])

    def test_verify_reports_a_wrong_hash_with_the_right_one(self):
        journal = [{"turn_index": 0, "content_hash": "a8f2b9c1d4e6f8a2"},
                   {"turn_index": 1, "content_hash": om.content_hash(RAW[1]["content"])}]
        bad = om.verify_hashes(journal, RAW)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["turn_index"], 0)
        self.assertEqual(bad[0]["claimed"], "a8f2b9c1d4e6f8a2")
        self.assertEqual(bad[0]["expected"], om.content_hash(RAW[0]["content"]))

    def test_verify_reports_an_index_outside_the_chat_set(self):
        bad = om.verify_hashes([{"turn_index": 9, "content_hash": "x"}], RAW)
        self.assertEqual(bad[0]["expected"], None)


class TurnScoreTests(unittest.TestCase):
    def test_formulas(self):
        # user = .40*1.0 + .30*.8 + .30*1.0 = .94 ; agent = 1 - 0 ; mutual = .5
        s = om.turn_scores({"specificity": 1.0, "constraint_density": 0.8,
                            "reference_resolution": 1.0, "fluff_ratio": 0.0,
                            "confirmation_ratio": 0.5})
        self.assertEqual(s["user_clarity"], D("0.94"))
        self.assertEqual(s["agent_clarity"], D("1.00"))
        self.assertEqual(s["mutual_understanding"], D("0.50"))

    def test_a_missing_component_is_undefined_not_invented(self):
        self.assertIsNone(om.turn_scores({"specificity": 1.0}))
        self.assertIsNone(om.turn_scores({"specificity": "n/a", "constraint_density": 0,
                                          "reference_resolution": 0, "fluff_ratio": 0,
                                          "confirmation_ratio": 0}))

    def test_ratios_are_clamped_to_the_unit_interval(self):
        s = om.turn_scores({"specificity": 2.0, "constraint_density": 2.0,
                            "reference_resolution": 2.0, "fluff_ratio": 1.5,
                            "confirmation_ratio": 3.0})
        self.assertEqual(s["user_clarity"], D("1.00"))
        self.assertEqual(s["agent_clarity"], D("0.00"))
        self.assertEqual(s["mutual_understanding"], D("1.00"))


class HealthTests(unittest.TestCase):
    def test_one_major_drift_alone_is_not_red(self):
        # .35*.833 + .35*(1-.35) + .30*(1-0) = .29155 + .2275 + .30 = .81905 -> .819
        h = om.health(D("0.833"), [drift("MAJOR")], [], 2)
        self.assertEqual(h["score"], D("0.819"))
        self.assertEqual(h["session_health"], "YELLOW")
        self.assertIsNone(h["override"])

    def test_green_boundary_is_inclusive(self):
        # clarity 1, no drift, 1 friction in 2 turns: .35 + .35 + .30*.5 = .850
        h = om.health(D("1"), [], [fp(0, "REPETITION")], 2)
        self.assertEqual(h["score"], D("0.850"))
        self.assertEqual(h["session_health"], "GREEN")

    def test_yellow_boundary_is_inclusive(self):
        # clarity 0, no drift, 1 friction in 2 turns: 0 + .35 + .15 = .500
        h = om.health(D("0"), [], [fp(0, "REPETITION")], 2)
        self.assertEqual(h["score"], D("0.500"))
        self.assertEqual(h["session_health"], "YELLOW")

    def test_below_half_is_red(self):
        # clarity 0, 2 friction in 3 turns: 0 + .35 + .30*(1/3) = .45
        h = om.health(D("0"), [], [fp(0, "REPETITION"), fp(1, "IGNORED_TOOL")], 3)
        self.assertEqual(h["score"], D("0.450"))
        self.assertEqual(h["session_health"], "RED")
        self.assertIsNone(h["override"])

    def test_drift_penalty_is_capped_at_one(self):
        h = om.health(D("1"), [drift("MAJOR"), drift("MAJOR"), drift("MAJOR")], [], 4)
        self.assertEqual(h["drift_penalty"], D("1.000"))

    def test_severity_weights(self):
        h = om.health(D("1"), [drift("MINOR"), drift("MODERATE")], [], 4)
        self.assertEqual(h["drift_penalty"], D("0.200"))      # .05 + .15

    def test_friction_penalty_is_a_ratio_at_most_one(self):
        h = om.health(D("1"), [], [fp(0, "REPETITION")] * 5, 2)
        self.assertEqual(h["friction_penalty"], D("1.000"))

    def test_critical_drift_forces_red_even_with_a_high_score(self):
        h = om.health(D("1"), [drift("CRITICAL")], [], 100)
        self.assertEqual(h["session_health"], "RED")
        self.assertEqual(h["override"], om.OVERRIDE_CRITICAL)

    def test_context_reset_then_repeated_work_forces_red(self):
        h = om.health(D("1"), [], [fp(2, "CONTEXT_LOSS"), fp(4, "REPETITION")], 50)
        self.assertEqual(h["session_health"], "RED")
        self.assertEqual(h["override"], om.OVERRIDE_CONTEXT_RESET)

    def test_repetition_before_the_reset_is_not_an_override(self):
        h = om.health(D("1"), [], [fp(1, "REPETITION"), fp(3, "CONTEXT_LOSS")], 50)
        self.assertIsNone(h["override"])
        self.assertEqual(h["session_health"], "GREEN")

    def test_unresolved_question_forces_red_only_when_given(self):
        self.assertIsNone(om.health(D("1"), [], [], 4)["override"])
        h = om.health(D("1"), [], [], 4, ["what port?"])
        self.assertEqual(h["override"], om.OVERRIDE_QUESTION)
        self.assertEqual(h["session_health"], "RED")

    def test_unknown_severity_is_reported_and_not_weighted(self):
        h = om.health(D("1"), [drift("HUGE")], [], 4)
        self.assertEqual(h["drift_penalty"], D("0.000"))
        self.assertEqual(h["unknown_severities"], ["HUGE"])

    def test_no_early_turn_rule_exists(self):
        # A drift at an early turn is weighted like any other; there is no
        # "first half of the chat is GREEN" shortcut in TB-006.
        early = om.health(D("0"), [drift("MAJOR", 0)], [], 10)
        late = om.health(D("0"), [drift("MAJOR", 9)], [], 10)
        self.assertEqual(early["score"], late["score"])


def model_output():
    """What the model sent for the real golden-test chatset 2: wrong hashes and
    a health of RED/0.443 that its own components do not support."""
    return {"outrobeaver_output": {
        "session_id": "s", "timestamp_utc": "t",
        "journal_entries": [
            {"entry_id": "e1", "turn_index": 0, "role": "USER",
             "content_hash": "a8f2b9c1d4e6f8a2", "tool_calls": [], "state_delta": "NONE"},
            {"entry_id": "e2", "turn_index": 1, "role": "AGENT",
             "content_hash": "b7d3c8e2f9a1b4d5", "tool_calls": [], "state_delta": "NONE"}],
        "clarity_and_communication_analysis": {
            "session_clarity_score": 0.833,
            "turn_scores": [
                {"turn_index": 0, "user_clarity": 0.95, "agent_clarity": 0.9,
                 "mutual_understanding": 0.5,
                 "components": {"specificity": 1.0, "constraint_density": 0.8,
                                "reference_resolution": 1.0, "fluff_ratio": 0.0,
                                "confirmation_ratio": 0.5}},
                {"turn_index": 1, "user_clarity": 0.0, "agent_clarity": 0.85,
                 "mutual_understanding": 0.5,
                 "components": {"specificity": 0.0, "constraint_density": 0.0,
                                "reference_resolution": 0.0, "fluff_ratio": 0.15,
                                "confirmation_ratio": 0.5}}],
            "drift_events": [drift("MAJOR")],
            "friction_points": [], "friction_rate": 0.0},
        "summary": {"built": "none", "blocked": "x", "carries_forward": [],
                    "session_health": "RED"},
        "health_components": {"clarity": 0.833, "drift_penalty": 0.35,
                              "friction_penalty": 0.0, "score": 0.443,
                              "override": "MAJOR drift event: primary artifacts missing"},
        "status": "OK"}}


def gate_for(parsed):
    return GateResult(ok=True, stage="outrobeaver", parsed=parsed,
                      verdict="RED", warnings=["session_health_RED"])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.gate = gate_for(model_output())
        self.m = om.apply(self.gate, RAW)
        self.body = self.gate.parsed["outrobeaver_output"]

    def test_hashes_are_python_s(self):
        got = [e["content_hash"] for e in self.body["journal_entries"]]
        self.assertEqual(got, [om.content_hash(t["content"]) for t in RAW])
        self.assertEqual(self.m["hash_corrections"], 2)
        self.assertTrue(any("content_hash corrected on 2" in w for w in self.gate.warnings))

    def test_turn_scores_are_recomputed_from_components(self):
        ts = self.body["clarity_and_communication_analysis"]["turn_scores"]
        self.assertEqual((ts[0]["user_clarity"], ts[0]["agent_clarity"],
                          ts[0]["mutual_understanding"]), (0.94, 1.0, 0.5))
        self.assertEqual((ts[1]["user_clarity"], ts[1]["agent_clarity"],
                          ts[1]["mutual_understanding"]), (0.0, 0.85, 0.5))

    def test_health_follows_the_formula_not_the_model(self):
        # clarity = ((.94+0)/2 + (1+.85)/2 + (.5+.5)/2) / 3 = 1.895/3 = .631667 -> .632
        # score   = .35*.631667 + .35*.65 + .30 = .748583 -> .749   (not 0.443)
        hc = self.body["health_components"]
        self.assertEqual(self.body["clarity_and_communication_analysis"]
                         ["session_clarity_score"], 0.632)
        self.assertEqual(hc["score"], 0.749)
        self.assertEqual(hc["drift_penalty"], 0.35)
        self.assertEqual(self.body["summary"]["session_health"], "YELLOW")
        self.assertEqual(self.m["session_health"], "YELLOW")

    def test_a_made_up_override_is_replaced(self):
        # "MAJOR drift event ..." is not one of the three TB-006 override rules.
        self.assertIsNone(self.body["health_components"]["override"])

    def test_gate_view_follows_python_s_verdict(self):
        self.assertEqual(self.gate.verdict, "YELLOW")
        self.assertNotIn("session_health_RED", self.gate.warnings)

    def test_red_is_kept_when_python_computes_red(self):
        out = model_output()
        out["outrobeaver_output"]["clarity_and_communication_analysis"]["drift_events"] = [
            drift("CRITICAL")]
        g = gate_for(out)
        om.apply(g, RAW)
        self.assertEqual(g.verdict, "RED")
        self.assertIn("session_health_RED", g.warnings)
        self.assertEqual(g.parsed["outrobeaver_output"]["health_components"]["override"],
                         om.OVERRIDE_CRITICAL)

    def test_apply_is_idempotent(self):
        once = copy.deepcopy(self.gate.parsed)
        om.apply(self.gate, RAW)
        self.assertEqual(self.gate.parsed, once)

    def test_a_missing_hash_is_filled_without_a_correction_warning(self):
        out = model_output()
        for e in out["outrobeaver_output"]["journal_entries"]:
            del e["content_hash"]
        g = gate_for(out)
        m = om.apply(g, RAW)
        got = [e["content_hash"] for e in g.parsed["outrobeaver_output"]["journal_entries"]]
        self.assertEqual(got, [om.content_hash(t["content"]) for t in RAW])
        self.assertEqual(m["hash_corrections"], 0)
        self.assertFalse(any("content_hash corrected" in w for w in g.warnings))

    def test_output_with_no_model_scores_at_all_is_fully_filled(self):
        # What the new TB-006 asks of the model: components only, no scores.
        out = model_output()
        b = out["outrobeaver_output"]
        for e in b["journal_entries"]:
            e.pop("content_hash")
        ca = b["clarity_and_communication_analysis"]
        for k in ("session_clarity_score", "friction_rate"):
            ca.pop(k)
        for row in ca["turn_scores"]:
            for k in ("user_clarity", "agent_clarity", "mutual_understanding"):
                row.pop(k)
        b["summary"].pop("session_health")
        b.pop("health_components")
        g = gate_for(out)
        om.apply(g, RAW)
        b = g.parsed["outrobeaver_output"]
        ca = b["clarity_and_communication_analysis"]
        self.assertEqual(b["health_components"]["score"], 0.749)
        self.assertEqual(b["summary"]["session_health"], "YELLOW")
        self.assertEqual(ca["session_clarity_score"], 0.632)
        self.assertEqual(ca["turn_scores"][0]["user_clarity"], 0.94)
        self.assertEqual(ca["friction_rate"], 0.0)

    def test_repetition_counts_follow_the_role_of_the_cited_turn(self):
        out = model_output()
        ca = out["outrobeaver_output"]["clarity_and_communication_analysis"]
        ca["friction_points"] = [fp(0, "REPETITION"), fp(1, "REPETITION"),
                                 fp(1, "IGNORED_TOOL"), fp(0, "REPETITION")]
        g = gate_for(out)
        om.apply(g, RAW)
        got = g.parsed["outrobeaver_output"]["clarity_and_communication_analysis"]
        self.assertEqual((got["user_repetition_count"], got["agent_repetition_count"]), (2, 1))

    def test_error_status_is_left_alone(self):
        out = model_output()
        out["outrobeaver_output"]["status"] = "CORRUPT_INPUT"
        before = copy.deepcopy(out)
        g = gate_for(out)
        m = om.apply(g, RAW)
        self.assertFalse(m["applied"])
        self.assertEqual(g.parsed, before)

    def test_turns_without_components_are_reported_not_invented(self):
        out = model_output()
        ca = out["outrobeaver_output"]["clarity_and_communication_analysis"]
        ca["turn_scores"][1]["components"] = {}
        g = gate_for(out)
        om.apply(g, RAW)
        self.assertTrue(any("SCORE_UNDEFINED on turns [1]" in w for w in g.warnings))
        # the unscorable turn keeps the model's numbers
        self.assertEqual(ca["turn_scores"][1]["agent_clarity"], 0.85)

    def test_journal_length_mismatch_is_a_warning(self):
        out = model_output()
        out["outrobeaver_output"]["journal_entries"].pop()
        g = gate_for(out)
        om.apply(g, RAW)
        self.assertTrue(any("journal_entries=1 but chat set has 2" in w for w in g.warnings))


if __name__ == "__main__":
    unittest.main()
