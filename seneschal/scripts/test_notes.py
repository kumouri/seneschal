#!/usr/bin/env python3
"""Tests for `notes.py` — the ad-hoc, witness-only notes file.

Every instant here is frozen and explicit, and the owner's zone (UTC-5) and identity (default day
boundary) are patched — the same posture `test_activity_day.py` holds itself to: this module answers
"which file does this note belong in", so a test that reads the wall clock or the runner's own
timezone agrees with itself on exactly one calendar day in exactly one timezone.

Run:  python -m unittest test_notes
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import clock  # noqa: E402
import notes  # noqa: E402
import tz_common  # noqa: E402

OWNER_ZONE = timezone(timedelta(hours=-5))


class _TmpState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = self._tmp.name
        z = mock.patch.object(tz_common, "_zone", return_value=OWNER_ZONE)
        i = mock.patch.object(clock, "load_identity", return_value={})
        z.start()
        i.start()
        self.addCleanup(z.stop)
        self.addCleanup(i.stop)

    def tearDown(self):
        self._tmp.cleanup()


class Add(_TmpState):
    def test_creates_the_file_with_a_header_on_first_write(self):
        result = notes.add("fed the cats early", state_dir=self.state_dir,
                            now=datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc))
        self.assertTrue(result["created"])
        text = open(result["path"], encoding="utf-8").read()
        self.assertTrue(text.startswith("# Notes"))
        self.assertIn("fed the cats early", text)

    def test_second_call_the_same_day_appends_never_rewrites_the_header(self):
        now = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)
        notes.add("first thing", state_dir=self.state_dir, now=now)
        result = notes.add("second thing", state_dir=self.state_dir, now=now)
        self.assertFalse(result["created"])
        text = open(result["path"], encoding="utf-8").read()
        self.assertEqual(text.count("# Notes"), 1)
        self.assertIn("first thing", text)
        self.assertIn("second thing", text)
        # order preserved -- an append, never a rebuild
        self.assertLess(text.index("first thing"), text.index("second thing"))

    def test_after_midnight_note_lands_in_the_prior_days_file(self):
        # 2026-08-12 00:30 local (UTC-5) -- activity_day's own after-midnight fixture
        # (test_activity_day.py: FromAUtcInstant.
        #  test_after_midnight_nudge_belongs_to_the_PREVIOUS_calendar_day)
        now = datetime(2026, 8, 12, 5, 30, tzinfo=timezone.utc)
        result = notes.add("still up", state_dir=self.state_dir, now=now)
        self.assertEqual(result["date"], "2026-08-11")
        self.assertTrue(os.path.exists(notes.note_path(date(2026, 8, 11), self.state_dir)))
        self.assertFalse(os.path.exists(notes.note_path(date(2026, 8, 12), self.state_dir)))

    def test_the_bullet_time_is_the_owner_local_wall_clock(self):
        # 20:00Z under UTC-5 is 3:00 PM local; no zone label is baked into the note.
        result = notes.add("afternoon aside", state_dir=self.state_dir,
                           now=datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc))
        text = open(result["path"], encoding="utf-8").read()
        self.assertIn("- **3:00 PM** — afternoon aside", text)

    def test_refuses_empty_text(self):
        with self.assertRaises(notes.NotesError):
            notes.add("   ", state_dir=self.state_dir)

    def test_refuses_unknown_source(self):
        with self.assertRaises(notes.NotesError):
            notes.add("x", state_dir=self.state_dir, source="telegram")

    def test_source_is_recorded_on_the_bullet(self):
        result = notes.add("x", state_dir=self.state_dir, source="watch",
                            now=datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc))
        text = open(result["path"], encoding="utf-8").read()
        self.assertIn("watch", text)

    def test_explicit_date_overrides_the_clock(self):
        result = notes.add("backfilled", state_dir=self.state_dir, date_str="2026-01-01",
                            now=datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(result["date"], "2026-01-01")

    def test_refuses_a_malformed_explicit_date(self):
        with self.assertRaises(notes.NotesError):
            notes.add("x", state_dir=self.state_dir, date_str="not-a-date")


class ListNotes(_TmpState):
    def test_missing_days_are_reported_not_skipped(self):
        entries = notes.list_notes(self.state_dir, days=2,
                                   now=datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(len(entries), 2)
        self.assertFalse(entries[0]["exists"])
        self.assertFalse(entries[1]["exists"])

    def test_reads_todays_and_yesterdays_notes_oldest_first(self):
        now = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)
        yesterday = datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc)
        notes.add("yesterday's thing", state_dir=self.state_dir, now=yesterday)
        notes.add("today's thing", state_dir=self.state_dir, now=now)
        entries = notes.list_notes(self.state_dir, days=2, now=now)
        self.assertEqual([e["date"] for e in entries], ["2026-09-17", "2026-09-18"])
        self.assertIn("yesterday's thing", entries[0]["text"])
        self.assertIn("today's thing", entries[1]["text"])

    def test_refuses_a_non_positive_window(self):
        with self.assertRaises(notes.NotesError):
            notes.list_notes(self.state_dir, days=0)


class Prune(_TmpState):
    def _seed(self, day_str: str, text: str = "x\n") -> str:
        path = os.path.join(self.state_dir, notes.NOTES_SUBDIR, f"{day_str}.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_boundary_keeps_exactly_days_old_drops_one_older(self):
        now = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)  # activity day 2026-09-18
        kept_path = self._seed("2026-06-20")     # exactly 90 days before 2026-09-18
        dropped_path = self._seed("2026-06-19")  # 91 days before
        result = notes.prune(self.state_dir, days=90, now=now)
        self.assertIn("2026-06-19.md", result["removed"])
        self.assertNotIn("2026-06-20.md", result["removed"])
        self.assertTrue(os.path.exists(kept_path))
        self.assertFalse(os.path.exists(dropped_path))

    def test_dry_run_touches_nothing(self):
        now = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)
        path = self._seed("2020-01-01")
        result = notes.prune(self.state_dir, days=90, now=now, dry_run=True)
        self.assertIn("2020-01-01.md", result["removed"])
        self.assertTrue(os.path.exists(path))

    def test_ignores_a_file_that_does_not_match_the_dated_shape(self):
        stray = os.path.join(self.state_dir, notes.NOTES_SUBDIR, "README.md")
        os.makedirs(os.path.dirname(stray), exist_ok=True)
        with open(stray, "w", encoding="utf-8") as fh:
            fh.write("not a dated note\n")
        result = notes.prune(self.state_dir, days=1,
                             now=datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(result["removed"], [])
        self.assertTrue(os.path.exists(stray))


class CLI(_TmpState):
    def test_add_cli_round_trip(self):
        rc = notes.main(["--state-dir", self.state_dir, "add", "cli note", "--date", "2026-09-18"])
        self.assertEqual(rc, 0)
        text = open(notes.note_path(date(2026, 9, 18), self.state_dir), encoding="utf-8").read()
        self.assertIn("cli note", text)

    def test_add_cli_refuses_empty_text(self):
        rc = notes.main(["--state-dir", self.state_dir, "add", "   "])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
