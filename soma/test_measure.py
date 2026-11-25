"""Tests for the distiller arithmetic that moved from the model to Python."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from machinetalk import measure  # noqa: E402
from machinetalk.gate import GateResult  # noqa: E402
from machinetalk.provenance import verify_distiller  # noqa: E402


def entry(key, i, text, turn):
    return {"entry_id": f"{key}[{i}]", "text": text, "turn": turn}


def accepted_for(raw, body):
    return verify_distiller({"distiller_output": body}, raw).accepted


class MeasureTests(unittest.TestCase):
    RAW = [{"role": "USER", "content": "hey! use pygame, please. 🔥"},
           {"role": "AGENT", "content": "Sure, let me do that."}]

    def test_bytes_are_utf8_not_characters(self):
        m = measure.measure(self.RAW, [])
        want = sum(len(t["content"].encode("utf-8")) for t in self.RAW)
        self.assertEqual(m["bytes"]["original"], want)
        self.assertGreater(want, sum(len(t["content"]) for t in self.RAW))  # the emoji is 4 bytes

    def test_noise_plus_signal_is_original_always(self):
        body = {"directives": [entry("directives", 0, "use pygame", 0)]}
        m = measure.measure(self.RAW, accepted_for(self.RAW, body))
        b = m["bytes"]
        self.assertEqual(b["noise"] + b["signal"], b["original"])
        self.assertEqual(b["signal"], len("use pygame"))
        self.assertEqual(b["distilled"], len("use pygame"))

    def test_overlapping_entries_do_not_double_count_signal(self):
        raw = [{"role": "USER", "content": "use pygame for the game"}]
        body = {"directives": [entry("directives", 0, "use pygame for", 0)],
                "constraints": [entry("constraints", 0, "pygame for the game", 0)]}
        m = measure.measure(raw, accepted_for(raw, body))
        self.assertEqual(m["bytes"]["signal"], len("use pygame for the game"))
        self.assertLessEqual(m["bytes"]["signal"], m["bytes"]["original"])

    def test_ratios_round_half_up_to_three_places(self):
        self.assertEqual(measure._ratio(1, 8), Decimal("0.125"))
        self.assertEqual(measure._ratio(1, 3), Decimal("0.333"))
        self.assertEqual(measure._ratio(2, 3), Decimal("0.667"))
        self.assertEqual(measure._ratio(5, 0), Decimal("0.000"))  # zero-byte guard

    def test_noise_classes_counted_per_segment(self):
        raw = [{"role": "USER", "content": "hey. sorry about that. maybe later. 4sho"}]
        m = measure.measure(raw, [])["noise_signature"]
        self.assertEqual(m["greeting"], 1)
        self.assertEqual(m["apology"], 1)
        self.assertEqual(m["hedge"], 1)
        self.assertEqual(m["sign_off"], 1)

    def test_word_boundaries(self):
        raw = [{"role": "USER", "content": "this is a thing."}]  # 'hi' inside words must not match
        self.assertEqual(measure.measure(raw, [])["noise_signature"]["greeting"], 0)

    def test_repetition_counts_later_identical_segments(self):
        raw = [{"role": "USER", "content": "ok go. ok go. ok go."}]
        self.assertEqual(measure.measure(raw, [])["noise_signature"]["repetition"], 2)

    def test_empty_chat_set(self):
        m = measure.measure([], [])
        self.assertEqual(m["bytes"]["original"], 0)
        self.assertEqual(m["signal_quality"], Decimal("0.000"))

    def test_ansi_and_emoji_only_are_visual_feedback(self):
        raw = [{"role": "AGENT", "content": "\x1b[32mdone\x1b[0m. 🔥🔥"}]
        self.assertGreaterEqual(measure.measure(raw, [])["noise_signature"]["visual_feedback"], 1)


class SkipTests(unittest.TestCase):
    RAW = [{"role": "USER", "content": "drop the full script."},
           {"role": "AGENT", "content": "x = 1\n" * 300}]

    def gate(self, body):
        return GateResult(ok=True, stage="distiller", parsed={"distiller_output": body})

    def run_apply(self, body):
        g = self.gate(body)
        acc = verify_distiller(g.parsed, self.RAW).accepted
        info = measure.apply(g, acc, self.RAW)
        return g, info

    def test_no_signal_skips_and_archives(self):
        g, _ = self.run_apply({"skip": False, "directives": []})
        b = g.parsed["distiller_output"]
        self.assertTrue(g.skipped)
        self.assertEqual((b["skip_reason"], b["exfil_type"]), ("NO_SIGNAL", "ARCHIVE"))
        self.assertEqual(g.verdict, "true")

    def test_noise_threshold_still_applies_when_the_model_called_it_noise(self):
        # 21 signal bytes against ~1,800 bytes of code: far below 0.300.
        g, info = self.run_apply(
            {"skip": True, "skip_reason": "NOISE_THRESHOLD", "exfil_type": "ARCHIVE",
             "directives": [entry("directives", 0, "drop the full script", 0)]})
        self.assertLess(info["signal_quality"], Decimal("0.300"))
        self.assertEqual(g.parsed["distiller_output"]["skip_reason"], "NOISE_THRESHOLD")
        self.assertTrue(g.skipped)

    def test_an_explicit_non_skip_with_a_directive_wins_over_a_low_signal_ratio(self):
        g, info = self.run_apply(
            {"skip": False, "directives": [entry("directives", 0, "drop the full script", 0)]})
        self.assertLess(info["signal_quality"], Decimal("0.300"))   # the ratio is low, as before
        b = g.parsed["distiller_output"]
        self.assertFalse(g.skipped)
        self.assertFalse(b["skip"])
        self.assertIsNone(b["skip_reason"])

    def test_a_missing_skip_field_with_a_directive_is_also_a_non_skip(self):
        g, _ = self.run_apply({"directives": [entry("directives", 0, "drop the full script", 0)]})
        self.assertFalse(g.skipped)

    def test_a_non_skip_with_nothing_accepted_is_still_no_signal(self):
        g, _ = self.run_apply({"skip": False, "directives": [entry("directives", 0, "not in the text", 0)]})
        self.assertTrue(g.skipped)
        self.assertEqual(g.parsed["distiller_output"]["skip_reason"], "NO_SIGNAL")

    def test_model_noise_threshold_withdrawn_when_arithmetic_disagrees(self):
        raw = [{"role": "USER", "content": "use pygame"}]
        g = GateResult(ok=True, stage="distiller", parsed={"distiller_output": {
            "skip": True, "skip_reason": "NOISE_THRESHOLD", "exfil_type": "ARCHIVE",
            "directives": [entry("directives", 0, "use pygame", 0)]}})
        acc = verify_distiller(g.parsed, raw).accepted
        measure.apply(g, acc, raw)
        self.assertFalse(g.skipped)
        self.assertIsNone(g.parsed["distiller_output"]["skip_reason"])

    def test_semantic_skip_is_left_to_the_model(self):
        raw = [{"role": "USER", "content": "use pygame"}]
        g = GateResult(ok=True, stage="distiller", parsed={"distiller_output": {
            "skip": True, "skip_reason": "ALREADY_RESOLVED", "exfil_type": "ARCHIVE",
            "directives": [entry("directives", 0, "use pygame", 0)]}})
        acc = verify_distiller(g.parsed, raw).accepted
        info = measure.apply(g, acc, raw)
        self.assertTrue(g.skipped)
        self.assertEqual(g.parsed["distiller_output"]["skip_reason"], "ALREADY_RESOLVED")
        self.assertEqual(info["decided_by"], "model")

    def test_python_numbers_overwrite_the_models(self):
        g, _ = self.run_apply({"skip": False, "signal_quality": 0.99, "bytes": {"original": 7},
                               "directives": [entry("directives", 0, "drop the full script", 0)]})
        b = g.parsed["distiller_output"]
        self.assertNotEqual(b["signal_quality"], 0.99)
        self.assertNotEqual(b["bytes"]["original"], 7)
        self.assertEqual(g.signal_quality, str(Decimal(str(b["signal_quality"])).quantize(Decimal("0.001"))))


if __name__ == "__main__":
    unittest.main()
