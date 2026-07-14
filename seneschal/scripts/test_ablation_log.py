#!/usr/bin/env python3
"""Tests for ablation_log.py + the rollup's ablation section — salience Phase 4(c).

Stdlib ``unittest`` only. The oracle log is ground truth, so the contract inverts the usual
graceful-degradation shape: **the writer refuses malformed records** (a bad label is worse than
none), while the reader stays tolerant. Also covers: the rollup renders the oracle section even
under evidence-window abstention (human judgment is meaningful from #1), and omits it when empty.

Run:  python -m unittest seneschal.scripts.test_ablation_log   (or)   python test_ablation_log.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import ablation_log as al      # noqa: E402
import salience_rollup as sr   # noqa: E402


class AppendUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "ablation-judgments.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def _lines(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_appends_wellformed_lines(self):
        al.append_judgment(query="when's therapy?", subject="therapy recall", verdict="with",
                           why="had the real date", chunk_refs=["journal:2026-07-08"],
                           log_path=self.log)
        al.append_judgment(query="old 3pm move?", verdict="tie", why="didn't need it",
                           log_path=self.log)
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["verdict"], "with")
        self.assertEqual(lines[0]["chunk_refs"], ["journal:2026-07-08"])
        self.assertTrue(lines[0]["blind"])
        self.assertEqual(lines[1]["subject"], "old 3pm move?")  # defaults to the query
        self.assertTrue(all("ts" in l and l["judged_by"] == "owner" for l in lines))

    def test_refuses_bad_records(self):
        for kwargs in (
            dict(query="q", verdict="better", why="w"),   # unknown verdict
            dict(query="q", verdict="with", why="  "),    # empty why — the label IS the data
            dict(query="", verdict="with", why="w"),      # empty query
        ):
            with self.assertRaises(ValueError):
                al.append_judgment(log_path=self.log, **kwargs)
        self.assertFalse(os.path.exists(self.log))  # nothing partial was written

    def test_cli_roundtrip_and_refusal_exit_codes(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):  # keep the suite's output quiet
            rc_ok = al.main(["--query", "q", "--verdict", "without", "--why", "generic was fine",
                             "--log", self.log, "--not-blind"])
            rc_bad = al.main(["--query", "q", "--verdict", "with", "--why", "  ",
                              "--log", self.log])
        self.assertEqual(rc_ok, 0)
        self.assertFalse(self._lines()[0]["blind"])
        self.assertEqual(rc_bad, 2)
        self.assertEqual(len(self._lines()), 1)


class RollupSectionUnit(unittest.TestCase):
    def test_summary_counts_and_none_when_empty(self):
        judgments = [
            {"subject": "a", "verdict": "with", "why": "had the date", "blind": True},
            {"subject": "b", "verdict": "tie", "why": "same", "blind": True},
            {"subject": "c", "verdict": "without", "why": "generic fine", "blind": False},
            {"subject": "junk", "verdict": "maybe", "why": "?"},   # unknown verdict skipped
        ]
        s = sr.ablation_summary(judgments)
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["counts"], {"with": 1, "without": 1, "tie": 1})
        self.assertIsNone(sr.ablation_summary([]))
        self.assertIsNone(sr.ablation_summary([{"verdict": "junk"}]))

    def test_report_shows_oracle_even_under_abstention(self):
        summary = sr.ablation_summary(
            [{"subject": "therapy recall", "verdict": "with", "why": "real date", "blind": True}])
        report = sr.render_report({}, set(), [], (False, 0, 0), ablation=summary)
        self.assertIn("Insufficient data", report)          # counters abstain…
        self.assertIn("Ablation A/B oracle", report)        # …the oracle still shows
        self.assertIn("therapy recall", report)

    def test_report_omits_section_when_no_judgments(self):
        report = sr.render_report({}, set(), [], (False, 0, 0), ablation=None)
        self.assertNotIn("Ablation A/B oracle", report)


if __name__ == "__main__":
    unittest.main()
