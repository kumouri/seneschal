#!/usr/bin/env python3
"""Tests for carryover_region.py — the WRAP_SEED / GENERATED region mechanism.

No clock is frozen: unlike `loops.py`, this module never stamps a timestamp itself — a WRAP SEED's
"tonight, HH:MM" text is composed by the Wrap and handed in as the body, so there is nothing here
for a frozen clock to pin.

Stdlib unittest only.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import carryover_region as cor  # noqa: E402
import loops  # noqa: E402


class _TmpState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def seed(self, text: str = "# carry-over\n\nsome hand-written head.\n") -> str:
        path = cor.carry_over_path(self.state_dir)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return path


class WriteRegionCreatesMarkers(_TmpState):
    def test_inserts_at_top_when_absent(self):
        self.seed("# carry-over\n\nold content.\n")
        result = cor.write_region("WRAP_SEED", "tonight's snapshot", state_dir=self.state_dir)
        self.assertTrue(result["created"])
        text = open(result["path"], encoding="utf-8").read()
        self.assertTrue(text.startswith(cor.REGIONS["WRAP_SEED"].begin))
        self.assertIn("tonight's snapshot", text)
        self.assertIn("old content.", text)
        # old content must survive, in order, after the new region
        self.assertLess(text.index(cor.REGIONS["WRAP_SEED"].end), text.index("old content."))

    def test_refuses_missing_file(self):
        missing_dir = os.path.join(self.state_dir, "nope")
        os.makedirs(missing_dir)
        with self.assertRaises(cor.RegionError):
            cor.write_region("WRAP_SEED", "x", state_dir=missing_dir)


class WriteRegionReplacesInPlace(_TmpState):
    def test_second_write_replaces_first_and_leaves_head_untouched(self):
        self.seed("# carry-over\n\nhand-written head.\n")
        cor.write_region("WRAP_SEED", "night one", state_dir=self.state_dir)
        first = open(cor.carry_over_path(self.state_dir), encoding="utf-8").read()
        self.assertIn("night one", first)

        cor.write_region("WRAP_SEED", "night two", state_dir=self.state_dir)
        second = open(cor.carry_over_path(self.state_dir), encoding="utf-8").read()
        self.assertNotIn("night one", second)
        self.assertIn("night two", second)
        self.assertIn("hand-written head.", second)
        # exactly one opener/closer survives — no accumulation
        self.assertEqual(second.count(cor.REGIONS["WRAP_SEED"].begin), 1)
        self.assertEqual(second.count(cor.REGIONS["WRAP_SEED"].end), 1)

    def test_byte_identical_suffix_regression(self):
        """The growth failure's own shape: re-writing the SAME body twice must not grow the file —
        a re-render is a no-op in size, the same invariant loops.write_render's idempotent separator
        logic protects for the GENERATED region."""
        self.seed("# carry-over\n\nhead.\n")
        cor.write_region("WRAP_SEED", "same body every night", state_dir=self.state_dir)
        size_1 = os.path.getsize(cor.carry_over_path(self.state_dir))
        cor.write_region("WRAP_SEED", "same body every night", state_dir=self.state_dir)
        size_2 = os.path.getsize(cor.carry_over_path(self.state_dir))
        self.assertEqual(size_1, size_2)

    def test_does_not_grow_across_many_nights_of_different_bodies(self):
        self.seed("# carry-over\n\nhead.\n")
        for night in range(10):
            cor.write_region("WRAP_SEED", f"snapshot for night {night}", state_dir=self.state_dir)
        text = open(cor.carry_over_path(self.state_dir), encoding="utf-8").read()
        self.assertEqual(text.count(cor.REGIONS["WRAP_SEED"].begin), 1)
        self.assertIn("snapshot for night 9", text)
        self.assertNotIn("snapshot for night 0", text)


class Refusals(_TmpState):
    def test_refuses_unknown_region_name(self):
        self.seed()
        with self.assertRaises(cor.RegionError):
            cor.write_region("NOT_A_REGION", "x", state_dir=self.state_dir)

    def test_refuses_generated_region_names_the_real_owner(self):
        self.seed()
        with self.assertRaises(cor.RegionError) as ctx:
            cor.write_region("GENERATED", "x", state_dir=self.state_dir)
        self.assertIn("loops.py render --write", str(ctx.exception))

    def test_refuses_empty_body_without_allow_empty(self):
        self.seed()
        with self.assertRaises(cor.RegionError):
            cor.write_region("WRAP_SEED", "", state_dir=self.state_dir)

    def test_allow_empty_permits_an_empty_region(self):
        self.seed()
        result = cor.write_region("WRAP_SEED", "", state_dir=self.state_dir, allow_empty=True)
        text = open(result["path"], encoding="utf-8").read()
        self.assertIn(cor.REGIONS["WRAP_SEED"].begin, text)
        self.assertIn(cor.REGIONS["WRAP_SEED"].end, text)

    def test_refuses_duplicate_opener(self):
        self.seed(f"{cor.REGIONS['WRAP_SEED'].begin}\nx\n{cor.REGIONS['WRAP_SEED'].begin}\ny\n"
                  f"{cor.REGIONS['WRAP_SEED'].end}\n")
        with self.assertRaises(cor.RegionError):
            cor.write_region("WRAP_SEED", "z", state_dir=self.state_dir)

    def test_refuses_orphan_closer(self):
        self.seed(f"some text\n{cor.REGIONS['WRAP_SEED'].end}\nmore text\n")
        with self.assertRaises(cor.RegionError):
            cor.write_region("WRAP_SEED", "z", state_dir=self.state_dir)


class InteropWithGeneratedRegion(_TmpState):
    def test_wrap_seed_write_leaves_a_real_generated_region_untouched(self):
        """The two writers must coexist: writing WRAP_SEED must not disturb an existing GENERATED
        region loops.py already rendered, byte for byte."""
        seed_text = "# carry-over\n\nhead.\n\n" + loops.render_region(loops.empty_store()) + "\n"
        self.seed(seed_text)
        original = open(cor.carry_over_path(self.state_dir), encoding="utf-8").read()
        generated_before = original[original.index(loops.BEGIN_MARK):]

        cor.write_region("WRAP_SEED", "tonight", state_dir=self.state_dir)
        after = open(cor.carry_over_path(self.state_dir), encoding="utf-8").read()
        generated_after = after[after.index(loops.BEGIN_MARK):]
        self.assertEqual(generated_before, generated_after)
        self.assertIn("tonight", after)


class Check(_TmpState):
    def test_missing_file_is_not_a_finding(self):
        result = cor.check(os.path.join(self.state_dir, "nope"))
        self.assertFalse(result["exists"])
        self.assertEqual(result["malformed"], [])

    def test_absent_regions_are_reported_present_false_not_malformed(self):
        self.seed("# carry-over\n\nhead only, no regions.\n")
        result = cor.check(self.state_dir)
        self.assertFalse(result["regions"]["WRAP_SEED"]["present"])
        self.assertFalse(result["regions"]["GENERATED"]["present"])
        self.assertEqual(result["malformed"], [])

    def test_present_region_reported(self):
        self.seed("# carry-over\n\nhead.\n")
        cor.write_region("WRAP_SEED", "tonight", state_dir=self.state_dir)
        result = cor.check(self.state_dir)
        self.assertTrue(result["regions"]["WRAP_SEED"]["present"])

    def test_head_tripwire_fires_past_ceiling_never_below(self):
        self.seed("# head\n" + ("line\n" * 5))
        under = cor.check(self.state_dir, head_line_ceiling=10)
        self.assertFalse(under["head_tripwire"])
        over = cor.check(self.state_dir, head_line_ceiling=3)
        self.assertTrue(over["head_tripwire"])

    def test_malformed_region_reported_and_not_swallowed(self):
        self.seed(f"{cor.REGIONS['WRAP_SEED'].end}\nno opener\n")
        result = cor.check(self.state_dir)
        self.assertEqual(len(result["malformed"]), 1)

    def test_head_line_count_excludes_region_spans(self):
        self.seed("head1\nhead2\n")
        cor.write_region("WRAP_SEED", "a\nb\nc", state_dir=self.state_dir)
        result = cor.check(self.state_dir)
        # "head1"/"head2" count; so does the one blank separator line the first-ever insertion adds
        # between the new region and the pre-existing content — trivial, never re-added on a later
        # write (span is then found, not created, so the prefix/suffix branch preserves it as-is).
        # The WRAP_SEED span itself (markers + a/b/c) is excluded.
        self.assertEqual(result["head_lines"], 3)


class CLI(_TmpState):
    def _run_with_stdin(self, body, argv):
        import io

        old_stdin = sys.stdin
        sys.stdin = io.StringIO(body)
        try:
            return cor.main(argv)
        finally:
            sys.stdin = old_stdin

    def test_write_region_cli_round_trip(self):
        self.seed("# carry-over\n\nhead.\n")
        rc = self._run_with_stdin(
            "tonight via CLI",
            ["--state-dir", self.state_dir, "write-region", "--name", "WRAP_SEED"])
        self.assertEqual(rc, 0)
        text = open(cor.carry_over_path(self.state_dir), encoding="utf-8").read()
        self.assertIn("tonight via CLI", text)

    def test_write_region_cli_refuses_empty_stdin_without_flag(self):
        self.seed("# carry-over\n\nhead.\n")
        rc = self._run_with_stdin(
            "", ["--state-dir", self.state_dir, "write-region", "--name", "WRAP_SEED"])
        self.assertEqual(rc, 2)

    def test_check_cli_enforce_exit_code_on_structural_finding(self):
        self.seed(f"{cor.REGIONS['WRAP_SEED'].end}\nno opener\n")
        rc = cor.main(["--state-dir", self.state_dir, "check", "--enforce"])
        self.assertEqual(rc, 1)

    def test_check_cli_never_fails_on_head_tripwire_alone(self):
        self.seed("line\n" * 200)
        rc = cor.main(["--state-dir", self.state_dir, "check", "--enforce",
                      "--head-line-ceiling", "5"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
