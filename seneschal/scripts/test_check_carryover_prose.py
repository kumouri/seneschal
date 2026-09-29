#!/usr/bin/env python3
"""Tests for check_carryover_prose.py — pure logic (find_raw_writes / TOOL_NAME_RE), plus a live
scan against the real tree so a regression in the actual prose is caught, not just in a fixture.

Stdlib unittest only.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import check_carryover_prose as ccp  # noqa: E402


class FindRawWrites(unittest.TestCase):
    def test_matches_the_old_shape(self):
        text = "run `python ../../seneschal/scripts/memory_write.py write ../../seneschal/state/carry-over.md`"
        self.assertEqual(len(ccp.find_raw_writes(text)), 1)

    def test_matches_reversed_order_on_one_line(self):
        text = "against carry-over.md, pipe the draft into memory_write.py write"
        self.assertEqual(len(ccp.find_raw_writes(text)), 1)

    def test_does_not_match_across_two_lines(self):
        text = "memory_write.py write is the general primitive.\nSee state/carry-over.md's own row."
        self.assertEqual(ccp.find_raw_writes(text), [])

    def test_does_not_match_carryover_region_py_write_region(self):
        text = "pipe the body into carryover_region.py write-region --name WRAP_SEED for carry-over.md"
        self.assertEqual(ccp.find_raw_writes(text), [])

    def test_does_not_match_memory_write_append_or_prepend(self):
        text = "memory_write.py append ../state/carry-over.md < entry.md"
        self.assertEqual(ccp.find_raw_writes(text), [])
        text2 = "memory_write.py prepend ../state/carry-over.md < newest.md"
        self.assertEqual(ccp.find_raw_writes(text2), [])

    def test_does_not_match_an_unrelated_write_verb(self):
        text = "memory_write.py write ../state/run-log.md < entry.md"
        self.assertEqual(ccp.find_raw_writes(text), [])


class ScanAgainstAFixtureTree(unittest.TestCase):
    def _make_tree(self, files: dict) -> str:
        root = tempfile.mkdtemp()
        for rel, content in files.items():
            path = os.path.join(root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        return root

    def test_missing_tool_name_is_flagged(self):
        root = self._make_tree({
            "subagents/eod-wrap/SKILL.md": "seed carry-over the old way, no tool named here.",
            "seneschal/modes/wrap.md": "delegates to eod-wrap.",
            "seneschal/references/memory.md": "protocol doc, no raw write.",
            "seneschal/state/README.md": "contract doc, no raw write.",
        })
        result = ccp.scan(root)
        self.assertIn("subagents/eod-wrap/SKILL.md", result["missing_tool_name"])
        self.assertEqual(result["raw_writes"], [])

    def test_tool_named_and_clean_is_no_finding(self):
        root = self._make_tree({
            "subagents/eod-wrap/SKILL.md": "run carryover_region.py write-region --name WRAP_SEED.",
            "seneschal/modes/wrap.md": "delegates to eod-wrap, which calls carryover_region.py.",
            "seneschal/references/memory.md": "protocol doc, no raw write.",
            "seneschal/state/README.md": "contract doc, no raw write.",
        })
        result = ccp.scan(root)
        self.assertEqual(result["missing_tool_name"], [])
        self.assertEqual(result["raw_writes"], [])

    def test_raw_write_in_a_named_file_is_flagged(self):
        root = self._make_tree({
            "subagents/eod-wrap/SKILL.md": "uses carryover_region.py normally.",
            "seneschal/modes/wrap.md": "delegates to eod-wrap.",
            "seneschal/references/memory.md": "python memory_write.py write ../state/carry-over.md < x.md",
            "seneschal/state/README.md": "contract doc, no raw write.",
        })
        result = ccp.scan(root)
        self.assertEqual(len(result["raw_writes"]), 1)
        self.assertEqual(result["raw_writes"][0]["file"], "seneschal/references/memory.md")

    def test_missing_files_are_reported_not_crashed_on(self):
        root = tempfile.mkdtemp()
        result = ccp.scan(root)
        self.assertEqual(len(result["files_missing"]), len(ccp.NAMED_FILES))
        self.assertEqual(result["raw_writes"], [])


class ScanAgainstTheRealTree(unittest.TestCase):
    """The live check — this is what CI actually runs, and it must be clean after this PR's own doc
    updates, since ccp.scan uses the SAME real files this PR edits."""

    def test_the_real_tree_has_no_raw_writes_and_names_the_tool(self):
        result = ccp.scan(ccp.REPO_ROOT)
        self.assertEqual(result["files_missing"], [])
        self.assertEqual(result["raw_writes"], [], result["raw_writes"])
        self.assertEqual(result["missing_tool_name"], [], result["missing_tool_name"])


class CLI(unittest.TestCase):
    def test_report_only_exits_zero_even_with_findings(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "subagents", "eod-wrap"))
        with open(os.path.join(root, "subagents", "eod-wrap", "SKILL.md"), "w") as fh:
            fh.write("no tool named")
        rc = ccp.main(["--root", root])
        self.assertEqual(rc, 0)

    def test_enforce_exits_one_on_a_finding(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "subagents", "eod-wrap"))
        with open(os.path.join(root, "subagents", "eod-wrap", "SKILL.md"), "w") as fh:
            fh.write("no tool named")
        rc = ccp.main(["--root", root, "--enforce"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
