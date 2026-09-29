#!/usr/bin/env python3
"""Tests for router.py's fallback-rate check: because every router arm is fail-safe, a degraded
classifier (Ollama up but slower than the timeout) silently produces nothing but 0.0-confidence
fallback verdicts, and nothing else reports it. This covers `is_fallback_verdict`,
`read_verdict_rows`, `fallback_rate`, `check_fallback_rate` and the `check-fallback-rate` CLI
subcommand. No test touches the wall clock; logs are written to a per-test temp dir.

Stdlib ``unittest`` only. Run:  python -m unittest test_router_fallback_rate
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import router  # noqa: E402


def _row(confidence, **extra):
    row = {"verdict": "escalate", "confidence": confidence}
    row.update(extra)
    return row


class IsFallbackVerdict(unittest.TestCase):
    def test_exact_zero_confidence_is_a_fallback(self):
        self.assertTrue(router.is_fallback_verdict({"confidence": 0.0}))

    def test_a_real_low_but_nonzero_confidence_is_not_a_fallback(self):
        self.assertFalse(router.is_fallback_verdict({"confidence": 0.05}))

    def test_a_confident_verdict_is_not_a_fallback(self):
        self.assertFalse(router.is_fallback_verdict({"confidence": 0.95}))

    def test_a_non_dict_row_is_not_a_fallback_and_does_not_raise(self):
        self.assertFalse(router.is_fallback_verdict(["not", "a", "dict"]))
        self.assertFalse(router.is_fallback_verdict(None))

    def test_a_missing_confidence_is_not_a_fallback(self):
        self.assertFalse(router.is_fallback_verdict({"verdict": "escalate"}))


class ReadVerdictRows(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "router-log.jsonl")

    def _write(self, rows):
        with open(self.path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_absent_file_reads_empty(self):
        self.assertEqual(router.read_verdict_rows(os.path.join(self.dir, "nope.jsonl")), [])

    def test_blank_and_malformed_lines_are_skipped(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"confidence": 0.9}\n')
            fh.write("\n")
            fh.write("not json\n")
            fh.write('{"confidence": 0.1}\n')
        rows = router.read_verdict_rows(self.path)
        self.assertEqual(len(rows), 2)

    def test_a_resolution_row_with_no_confidence_key_is_dropped(self):
        self._write([{"confidence": 0.9, "arm": "triage"}, {"kind": "interleave.resolved"}])
        rows = router.read_verdict_rows(self.path)
        self.assertEqual(len(rows), 1)

    def test_filter_field_narrows_to_one_arm(self):
        self._write([
            {"confidence": 0.9, "arm": "triage"},
            {"confidence": 0.0, "arm": "fable"},
            {"confidence": 0.2, "arm": "triage"},
        ])
        rows = router.read_verdict_rows(self.path, filter_field="arm", filter_value="triage")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["arm"] == "triage" for r in rows))


class FallbackRate(unittest.TestCase):
    def test_zero_rows_abstains(self):
        self.assertIsNone(router.fallback_rate([])["rate"])

    def test_computes_the_fraction(self):
        rows = [_row(0.0), _row(0.0), _row(0.9), _row(0.8)]
        result = router.fallback_rate(rows, window=None)
        self.assertEqual(result, {"rows": 4, "fallbacks": 2, "rate": 0.5})

    def test_window_keeps_only_the_trailing_n(self):
        # 10 healthy rows, then 4 fallbacks — a window of 4 must see only the degraded tail.
        rows = [_row(0.9)] * 10 + [_row(0.0)] * 4
        result = router.fallback_rate(rows, window=4)
        self.assertEqual(result, {"rows": 4, "fallbacks": 4, "rate": 1.0})

    def test_a_falsy_window_means_the_whole_list(self):
        rows = [_row(0.0)] * 3 + [_row(0.9)] * 27
        result = router.fallback_rate(rows, window=0)
        self.assertEqual(result["rows"], 30)


class CheckFallbackRate(unittest.TestCase):
    def test_abstains_below_min_rows(self):
        rows = [_row(0.0)] * 5  # all fallback, but too few to judge
        result = router.check_fallback_rate(rows, min_rows=10)
        self.assertFalse(result["alert"])
        self.assertIsNone(result["rate"])
        self.assertIn("insufficient data", result["reason"])

    def test_a_long_blind_streak_alerts(self):
        # One real verdict, then 18 consecutive fallbacks — a classifier silently timing out for hours.
        rows = [_row(0.95)] + [_row(0.0)] * 18
        result = router.check_fallback_rate(rows, window=20, threshold=0.5, min_rows=10)
        self.assertTrue(result["alert"])
        self.assertGreaterEqual(result["rate"], 0.5)

    def test_ordinary_mixed_operation_does_not_alert(self):
        rows = ([_row(0.9), _row(0.85), _row(0.0), _row(0.7), _row(0.92)] * 4)  # 20% fallback
        result = router.check_fallback_rate(rows, window=20, threshold=0.5, min_rows=10)
        self.assertFalse(result["alert"])

    def test_exactly_at_threshold_alerts(self):
        rows = [_row(0.0)] * 10 + [_row(0.9)] * 10
        result = router.check_fallback_rate(rows, window=20, threshold=0.5, min_rows=10)
        self.assertTrue(result["alert"])

    def test_never_raises_on_an_empty_list(self):
        result = router.check_fallback_rate([])
        self.assertFalse(result["alert"])


class Cli(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "router-log.jsonl")
        self.out = io.StringIO()
        patcher = mock.patch("sys.stdout", self.out)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, rows):
        with open(self.path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_prints_the_verdict_as_json(self):
        self._write([{"confidence": 0.9, "arm": "triage"}] * 15)
        router._main(["router.py", "check-fallback-rate", "--log", self.path])
        result = json.loads(self.out.getvalue())
        self.assertEqual(set(result), {"alert", "rate", "rows", "fallbacks", "threshold", "reason"})
        self.assertFalse(result["alert"])

    def test_alert_exits_nonzero(self):
        self._write([{"confidence": 0.0, "arm": "triage"}] * 15)
        rc = router._main(["router.py", "check-fallback-rate", "--log", self.path,
                           "--min-rows", "10"])
        self.assertEqual(rc, 1)

    def test_healthy_log_exits_zero(self):
        self._write([{"confidence": 0.9, "arm": "triage"}] * 15)
        rc = router._main(["router.py", "check-fallback-rate", "--log", self.path,
                           "--min-rows", "10"])
        self.assertEqual(rc, 0)

    def test_filter_flags_reach_the_reader(self):
        self._write([{"confidence": 0.0, "arm": "fable"}] * 15
                     + [{"confidence": 0.9, "arm": "triage"}] * 15)
        rc = router._main(["router.py", "check-fallback-rate", "--log", self.path,
                           "--filter-field", "arm", "--filter-value", "triage",
                           "--min-rows", "10"])
        self.assertEqual(rc, 0, "the fable-arm fallbacks must not leak into the triage-only check")


if __name__ == "__main__":
    unittest.main()
