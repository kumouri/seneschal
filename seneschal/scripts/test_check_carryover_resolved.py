#!/usr/bin/env python3
"""Tests for the carry-over resolved-item guard (check_carryover_resolved.py).

The load-bearing case is the motivating shape: a "waiting on the owner" summary line naming a PR,
and — hundreds of lines lower in the same file — a struck-through RESOLVED row for that same PR.
Every other test exists to keep the check from becoming either blind (misses a real recurrence) or
a wall (flags the resolved row's own prose, or an unrelated PR discussed nearby).

Stdlib unittest only.
"""
from __future__ import annotations

import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import check_carryover_resolved as ccr  # noqa: E402


def pr_findings(text):
    return [(f.pr, f.resolved_line, f.stale_line) for f in ccr.scan(text)]


class DetectsTheRealIncident(unittest.TestCase):
    def test_the_motivating_shape_exactly(self):
        """A summary clause near the top vs. a resolved row ~900 lines lower, compressed."""
        lines = ["# carry-over"] * 5
        lines.append("**Waiting on the owner:** PR #123 green/unmerged + two open questions.")
        lines += ["filler"] * 900
        lines.append("| ~~PR #123~~ | **RESOLVED.** #122 and #123 were both CLOSED and "
                     "superseded by #124 (abc1234) ... Nothing waits here. |")
        text = "\n".join(lines)
        findings = pr_findings(text)
        self.assertEqual(len(findings), 1)
        pr, resolved_line, stale_line = findings[0]
        self.assertEqual(pr, "123")
        self.assertEqual(stale_line, 6)
        self.assertEqual(resolved_line, len(lines))

    def test_bare_hash_number_without_PR_prefix(self):
        text = ("Waiting on the owner: #123 still needs a decision.\n"
                "| ~~#123~~ | **RESOLVED.** done. |\n")
        findings = pr_findings(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], "123")


class DoesNotFalsePositive(unittest.TestCase):
    def test_clean_file_with_no_resolved_rows(self):
        text = "**Waiting on the owner:** PR #123 green/unmerged.\nStill open, nothing else here.\n"
        self.assertEqual(pr_findings(text), [])

    def test_resolved_rows_own_prose_does_not_self_flag(self):
        """The declaration row legitimately repeats the number outside the struck span
        (`#122 and #123 were both CLOSED`) — that is not a stale mention of itself."""
        text = ("| ~~PR #123~~ | **RESOLVED.** #122 and #123 were both CLOSED and "
                "superseded by #124. |\n")
        self.assertEqual(pr_findings(text), [])

    def test_a_different_unstruck_pr_nearby_is_not_flagged(self):
        """#124 supersedes #123 but was never struck through, so it is not "resolved" and mentioning
        it elsewhere is not a finding."""
        text = ("**Waiting on the owner:** PR #124 still needs a merge.\n"
                "| ~~PR #123~~ | **RESOLVED.** superseded by #124. |\n")
        self.assertEqual(pr_findings(text), [])

    def test_a_second_struck_through_mention_of_the_same_pr_is_not_stale(self):
        text = ("Some note: ~~PR #123~~ closed.\n"
                "| ~~PR #123~~ | **RESOLVED.** done. |\n")
        self.assertEqual(pr_findings(text), [])

    def test_strikethrough_without_the_word_resolved_is_not_a_declaration(self):
        """A struck-through mention with no RESOLVED marker isn't confidently a resolution — e.g. a
        formatting accident or an item struck for some other reason entirely."""
        text = "~~PR #123~~ some other reason\nPR #123 still open somewhere else\n"
        self.assertEqual(pr_findings(text), [])

    def test_no_file_present_reports_clean_exit(self):
        """Bootstrap-on-absence (`../references/memory.md`) — a missing carry-over.md is normal, not
        an error."""
        self.assertEqual(ccr.main(["/nonexistent/path/carry-over.md"]), 0)


class MultipleResolutions(unittest.TestCase):
    def test_two_independent_resolved_items_each_catch_their_own_stale_mention(self):
        text = ("Waiting on the owner: PR #100 and PR #200 both still open.\n"
                "| ~~PR #100~~ | **RESOLVED.** done. |\n"
                "| ~~PR #200~~ | **RESOLVED.** done. |\n")
        findings = pr_findings(text)
        self.assertEqual({f[0] for f in findings}, {"100", "200"})
        self.assertEqual(len(findings), 2)


class CLI(unittest.TestCase):
    def test_enforce_flag_exits_nonzero_on_a_finding(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write("PR #123 still open.\n| ~~PR #123~~ | **RESOLVED.** done. |\n")
            path = fh.name
        try:
            self.assertEqual(ccr.main([path]), 0)             # report-only default
            self.assertEqual(ccr.main([path, "--enforce"]), 1)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
