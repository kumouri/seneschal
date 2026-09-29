#!/usr/bin/env python3
"""Tests for `check_grounding_dates.py` — `../docs/cleanroom-remediation-spec.md` §3 Phase 5.

No git fixtures needed: this module reads the tree, not a diff. A throwaway `context-budget.json`
plus throwaway tier A/B files stand in for the real ones.

Run:  python -m unittest seneschal.scripts.test_check_grounding_dates
"""
import io
import json
import os
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
import sys  # noqa: E402
sys.path.insert(0, SCRIPT_DIR)

import check_grounding_dates as cgd  # noqa: E402


def write(root: str, files: dict):
    for rel, body in files.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(body)


def budget_json(tiered: dict) -> str:
    budgets = {name: {"tier": tier, "max_bytes": 1000, "raises": []} for name, tier in tiered.items()}
    return json.dumps({"budgets": budgets}, indent=2)


class TierPathsTests(unittest.TestCase):
    def test_only_tier_a_and_b_are_selected(self):
        root = tempfile.mkdtemp()
        path = os.path.join(root, "budget.json")
        write(root, {"budget.json": budget_json({"a.md": "A", "b.md": "B", "c.md": "C"})})
        self.assertEqual(cgd.tier_ab_paths(path), ["a.md", "b.md"])

    def test_symbol_artifacts_are_skipped(self):
        root = tempfile.mkdtemp()
        path = os.path.join(root, "budget.json")
        write(root, {"budget.json": budget_json({"presence.py::GROUNDING": "A", "a.md": "A"})})
        self.assertEqual(cgd.tier_ab_paths(path), ["a.md"])

    def test_missing_or_corrupt_file_yields_no_paths(self):
        self.assertEqual(cgd.tier_ab_paths("/no/such/file.json"), [])


class ScanTests(unittest.TestCase):
    def _root_with(self, content: str):
        root = tempfile.mkdtemp()
        write(root, {
            "seneschal/context-budget.json": budget_json({"grounded.md": "A"}),
            "grounded.md": content,
        })
        return root

    def test_bare_date_is_flagged(self):
        root = self._root_with("The owner ruled this on 2026-08-31, verbatim.\n")
        result = cgd.scan(root)
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["date"], "2026-08-31")

    def test_ruling_id_is_exempt(self):
        root = self._root_with("See ruling `2026-08-31-master-untagged` for the reasoning.\n")
        result = cgd.scan(root)
        self.assertEqual(result["findings"], [])

    def test_date_at_end_of_sentence_is_flagged(self):
        root = self._root_with("This shipped on 2026-08-31.\n")
        result = cgd.scan(root)
        self.assertEqual(len(result["findings"]), 1)

    def test_multiple_dates_all_reported(self):
        root = self._root_with("From 2026-08-01 to 2026-08-31, two bare dates.\n")
        result = cgd.scan(root)
        self.assertEqual(len(result["findings"]), 2)

    def test_only_tier_ab_files_are_scanned(self):
        root = tempfile.mkdtemp()
        write(root, {
            "seneschal/context-budget.json": budget_json({"grounded.md": "A"}),
            "grounded.md": "No dates here.\n",
            "unscoped.md": "The owner ruled this on 2026-08-31.\n",
        })
        result = cgd.scan(root)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["scanned"], 1)

    def test_has_no_enforce_flag(self):
        # Structural guarantee, not a CLI smoke test: this lint must never gain a way to block CI.
        parser_dests = []
        import argparse
        p = argparse.ArgumentParser()
        # Reconstruct main()'s parser surface by inspecting the module's own main() argument names.
        self.assertNotIn("enforce", cgd.main.__code__.co_names)


class LiveCorpusTests(unittest.TestCase):
    """Runs against the real tree — never asserts zero findings (this lint ships red on purpose
    until Phase 6), only that it runs and returns a well-shaped result."""

    def test_runs_clean_against_the_real_tree(self):
        result = cgd.scan(REPO_ROOT)
        self.assertIsInstance(result["findings"], list)
        self.assertGreaterEqual(result["scanned"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
