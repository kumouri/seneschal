#!/usr/bin/env python3
"""Tests for reminders_seed.py — the exact-time whole-day reminder seeder that retired the four
fixed slots. Stdlib unittest; deterministic off a fixed aware ``now_local`` (no host tz db needed)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import reminders_seed as rs  # noqa: E402

CDT = timezone(timedelta(hours=-5))   # a UTC-5 owner zone in summer (DST; e.g. US Central)
CST = timezone(timedelta(hours=-6))   # the same owner zone in winter (standard time)
JUL15 = datetime(2026, 7, 15, 0, 5, tzinfo=CDT)   # just after local midnight → seeds for 2026-07-15


class WindowDefaults(unittest.TestCase):
    def test_map_is_the_migration_anchors(self):
        # These doubly serve as the run-time fallback AND the migration map, and MUST equal the old
        # fixed slot times (guards accidental drift between code and reminders-policy.md/databases.md).
        self.assertEqual(rs.WINDOW_DEFAULTS, {
            "Morning": "08:00", "Midday": "12:30", "Evening": "18:30",
            "Bedtime": "21:30", "Anytime": "09:00",
        })


class PrimaryTimes(unittest.TestCase):
    def test_single_explicit_time(self):
        self.assertEqual(rs.primary_times("08:00", None), [(8, 0)])

    def test_multiple_explicit_times_sorted_deduped(self):
        self.assertEqual(rs.primary_times("20:00, 08:00, 08:00", None), [(8, 0), (20, 0)])

    def test_empty_times_falls_back_to_window_default(self):
        self.assertEqual(rs.primary_times(None, "Evening"), [(18, 30)])
        self.assertEqual(rs.primary_times("", "Bedtime"), [(21, 30)])
        self.assertEqual(rs.primary_times("   ", "Anytime"), [(9, 0)])

    def test_explicit_times_win_over_window(self):
        self.assertEqual(rs.primary_times("07:15", "Bedtime"), [(7, 15)])

    def test_no_basis_returns_empty(self):
        self.assertEqual(rs.primary_times(None, None), [])
        self.assertEqual(rs.primary_times("", ""), [])
        self.assertEqual(rs.primary_times(None, "Nonsense"), [])

    def test_malformed_entries_ignored_not_fatal(self):
        self.assertEqual(rs.primary_times("08:00, garbage, 25:00, 09:61, 12:30", None),
                         [(8, 0), (12, 30)])


class IsRefire(unittest.TestCase):
    def test_high_and_above_refire(self):
        for imp in ("🛑 Super-Critical", "🚨 Critical", "⭐ High"):
            self.assertTrue(rs.is_refire(imp, False), imp)

    def test_low_and_notable_do_not(self):
        for imp in ("✨ Notable", "📌 Low", None, ""):
            self.assertFalse(rs.is_refire(imp, False), imp)

    def test_nag_forces_refire_regardless_of_importance(self):
        self.assertTrue(rs.is_refire("📌 Low", True))
        self.assertTrue(rs.is_refire(None, True))


class DayTimes(unittest.TestCase):
    def test_no_refire_is_just_primaries(self):
        self.assertEqual(rs.day_times([(8, 0)], refire=False), [(8, 0)])
        self.assertEqual(rs.day_times([(8, 0), (20, 0)], refire=False), [(8, 0), (20, 0)])

    def test_refire_every_90_through_end_of_day(self):
        got = rs.day_times([(8, 0)], refire=True)
        # 08:00 then +90 min until 23:59: 08:00 09:30 11:00 12:30 14:00 15:30 17:00 18:30 20:00 21:30 23:00
        self.assertEqual(got, [(8, 0), (9, 30), (11, 0), (12, 30), (14, 0), (15, 30),
                               (17, 0), (18, 30), (20, 0), (21, 30), (23, 0)])
        self.assertEqual(len(got), 11)

    def test_refire_near_end_of_day_makes_no_next_day_slot(self):
        # 23:00 + 90 = 00:30 next day → excluded; only the primary survives.
        self.assertEqual(rs.day_times([(23, 0)], refire=True), [(23, 0)])

    def test_refire_from_multiple_primaries_merges_distinct(self):
        got = rs.day_times([(8, 0), (8, 45)], refire=True)
        self.assertEqual(got, sorted(got))                 # sorted
        self.assertEqual(len(got), len(set(got)))          # deduped
        for t in ((8, 0), (8, 45), (23, 0), (23, 45)):
            self.assertIn(t, got)

    def test_refire_overlap_is_deduped(self):
        # 20:00 is 08:00 + 8*90, so a twice-daily 08:00/20:00 critical merges to the 08:00 series.
        self.assertEqual(rs.day_times([(8, 0), (20, 0)], refire=True), rs.day_times([(8, 0)], refire=True))


class SeedEntries(unittest.TestCase):
    def _row(self, **kw):
        base = {"reminder_id": "00000000-0000-0000-0000-000000000123", "text": "Morning meds.",
                "slug": "meds-am", "time_window": "Morning"}
        base.update(kw)
        return base

    def test_local_time_to_utc_and_id_format(self):
        entries = rs.seed_entries(self._row(), JUL15)          # 08:00 CDT, no re-fire (window only, no importance)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["id"], "rmd-2026-07-15-meds-am-0800")   # id uses LOCAL HHMM
        self.assertEqual(e["due_at"], "2026-07-15T13:00:00Z")      # 08:00 CDT == 13:00 UTC
        self.assertEqual(e["reminder_id"], "00000000-0000-0000-0000-000000000123")
        self.assertEqual(e["channel"], "telegram")
        self.assertIsNone(e["fired_at"])

    def test_ack_gate_is_absent_so_gate_applies(self):
        # Seed entries must NOT set ack_gate:false (that's a roll) — one ack should cancel the day's
        # remaining re-fires via the fire-time ack gate, which only skips entries with ack_gate:false.
        for e in rs.seed_entries(self._row(importance="🚨 Critical"), JUL15):
            self.assertNotIn("ack_gate", e)

    def test_critical_seeds_full_refire_schedule(self):
        entries = rs.seed_entries(self._row(importance="🚨 Critical"), JUL15)
        self.assertEqual(len(entries), 11)                     # the 90-min ladder
        ids = [e["id"] for e in entries]
        self.assertIn("rmd-2026-07-15-meds-am-0800", ids)
        self.assertIn("rmd-2026-07-15-meds-am-2300", ids)

    def test_pierce_quiet_flag_carried(self):
        e = rs.seed_entries(self._row(pierce_quiet=True), JUL15)[0]
        self.assertTrue(e["pierce_quiet"])
        e2 = rs.seed_entries(self._row(), JUL15)[0]
        self.assertNotIn("pierce_quiet", e2)

    def test_explicit_multi_times(self):
        entries = rs.seed_entries(self._row(times="08:00, 20:00", time_window=None), JUL15)
        self.assertEqual([e["due_at"] for e in entries],
                         ["2026-07-15T13:00:00Z", "2026-07-16T01:00:00Z"])  # 20:00 CDT == 01:00Z next day

    def test_no_time_basis_seeds_nothing(self):
        self.assertEqual(rs.seed_entries(self._row(time_window=None, times=None), JUL15), [])

    def test_dst_determinism(self):
        # Same 08:00 local wall time converts to a different UTC instant across the DST boundary,
        # purely from now_local's offset — no host tz db consulted.
        jan = datetime(2026, 1, 15, 0, 5, tzinfo=CST)
        summer = rs.seed_entries(self._row(), JUL15)[0]["due_at"]
        winter = rs.seed_entries(self._row(), jan)[0]["due_at"]
        self.assertEqual(summer, "2026-07-15T13:00:00Z")   # CDT −5
        self.assertEqual(winter, "2026-01-15T14:00:00Z")   # CST −6


class RefillSeed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.row = {"reminder_id": "abc123", "text": "Walk.", "slug": "walk", "times": "16:00"}

    def _queue(self):
        with open(os.path.join(self.dir, "reminders.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_writes_entries(self):
        added = rs.refill_seed(self.dir, self.row, now_local=JUL15)
        self.assertEqual(added, 1)
        q = self._queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["due_at"], "2026-07-15T21:00:00Z")   # 16:00 CDT

    def test_idempotent_second_run_adds_nothing(self):
        self.assertEqual(rs.refill_seed(self.dir, self.row, now_local=JUL15), 1)
        self.assertEqual(rs.refill_seed(self.dir, self.row, now_local=JUL15), 0)   # same ids → skip
        self.assertEqual(len(self._queue()), 1)

    def test_refill_appends_alongside_existing(self):
        # A pre-existing unrelated entry is preserved (seed only appends its own ids).
        import reminders_enqueue as re
        path = os.path.join(self.dir, "reminders.json")
        re.save_reminders(path, [{"id": "other", "text": "x", "due_at": "2026-07-15T10:00:00Z"}])
        added = rs.refill_seed(self.dir, self.row, now_local=JUL15)
        self.assertEqual(added, 1)
        ids = {e["id"] for e in self._queue()}
        self.assertIn("other", ids)
        self.assertIn("rmd-2026-07-15-walk-1600", ids)

    def test_no_time_basis_writes_nothing(self):
        added = rs.refill_seed(self.dir, {"reminder_id": "z", "text": "y"}, now_local=JUL15)
        self.assertEqual(added, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "reminders.json")))


if __name__ == "__main__":
    unittest.main()
