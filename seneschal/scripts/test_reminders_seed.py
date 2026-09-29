#!/usr/bin/env python3
"""Tests for reminders_seed.py — the exact-time whole-day reminder seeder that retired the four
fixed slots. Stdlib unittest; deterministic off a fixed aware ``now_local`` (no host tz db needed)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

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
    """`Nag Until Done` ALONE drives the ladder. It used to be `importance >= High` **OR** the checkbox,
    so the box could only ever ADD laddering — while the schema described the two fields as
    independent. They are now: importance = how loud + whether it pierces; nag = whether it chases."""

    def test_nag_alone_ladders(self):
        self.assertTrue(rs.is_refire(True))

    def test_no_nag_no_ladder(self):
        self.assertFalse(rs.is_refire(False))

    def test_importance_is_not_an_input(self):
        # The predicate takes ONE argument on purpose: a second one is how the OR grows back.
        with self.assertRaises(TypeError):
            rs.is_refire("🚨 Critical", False)


class LadderGap(unittest.TestCase):
    """The one foot-gun the nag-alone rule creates, and its mitigation: a NEW row at High or above with
    the box unticked silently would not chase. It still seeds exactly as asked — this only makes it
    loud. Importance compares in ANY backend's spelling (Notion emoji labels and canonical keys)."""

    def test_important_without_nag_is_a_gap(self):
        for imp in ("🛑 Super-Critical", "🚨 Critical", "⭐ High", "super-critical", "critical", "high"):
            self.assertTrue(rs.ladder_gap(imp, False), imp)

    def test_important_with_nag_is_not(self):
        for imp in ("🛑 Super-Critical", "🚨 Critical", "⭐ High", "high"):
            self.assertFalse(rs.ladder_gap(imp, True), imp)

    def test_low_stakes_rows_are_never_a_gap(self):
        # Firing once and stopping is the whole design for these — a line here would be noise every day.
        for imp in ("✨ Notable", "📌 Low", "notable", "low", None, "", "Nonsense"):
            self.assertFalse(rs.ladder_gap(imp, False), imp)
            self.assertFalse(rs.ladder_gap(imp, True), imp)


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
        base = {"reminder_id": "00000000-0000-0000-0000-000000000123", "text": "Water the plants.",
                "slug": "plants-am", "time_window": "Morning"}
        base.update(kw)
        return base

    def test_local_time_to_utc_and_id_format(self):
        entries = rs.seed_entries(self._row(), JUL15)          # 08:00 CDT, no re-fire (window only, no importance)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["id"], "rmd-2026-07-15-plants-am-0800")   # id uses LOCAL HHMM
        self.assertEqual(e["due_at"], "2026-07-15T13:00:00Z")      # 08:00 CDT == 13:00 UTC
        self.assertEqual(e["reminder_id"], "00000000-0000-0000-0000-000000000123")
        self.assertEqual(e["channel"], "telegram")
        self.assertIsNone(e["fired_at"])

    def test_ack_gate_is_absent_so_gate_applies(self):
        # Seed entries must NOT set ack_gate:false (that's a roll) — one ack should cancel the day's
        # remaining re-fires via the fire-time ack gate, which only skips entries with ack_gate:false.
        for e in rs.seed_entries(self._row(nag=True), JUL15):
            self.assertNotIn("ack_gate", e)

    def test_nag_seeds_full_refire_schedule(self):
        entries = rs.seed_entries(self._row(importance="🚨 Critical", nag=True), JUL15)
        self.assertEqual(len(entries), 11)                     # the 90-min ladder
        ids = [e["id"] for e in entries]
        self.assertIn("rmd-2026-07-15-plants-am-0800", ids)
        self.assertIn("rmd-2026-07-15-plants-am-2300", ids)

    def test_high_without_nag_does_not_ladder(self):
        # Under the older OR rule this row seeded 11 entries off its importance alone.
        entries = rs.seed_entries(self._row(importance="⭐ High"), JUL15)
        self.assertEqual([e["id"] for e in entries], ["rmd-2026-07-15-plants-am-0800"])

    def test_notable_with_nag_does_ladder(self):
        # The other half: the checkbox is authoritative in BOTH directions now, not just additive.
        entries = rs.seed_entries(self._row(importance="✨ Notable", nag=True), JUL15)
        self.assertEqual(len(entries), 11)

    def test_importance_is_carried_verbatim_onto_every_entry(self):
        # Read by sentinel._due_sort_key to break a same-due_at tie; any backend's spelling rides.
        for imp in ("⭐ High", "high"):
            for e in rs.seed_entries(self._row(importance=imp, nag=True), JUL15):
                self.assertEqual(e["importance"], imp)
        self.assertNotIn("importance", rs.seed_entries(self._row(), JUL15)[0])

    def test_pierce_quiet_flag_carried(self):
        e = rs.seed_entries(self._row(pierce_quiet=True), JUL15)[0]
        self.assertTrue(e["pierce_quiet"])
        e2 = rs.seed_entries(self._row(), JUL15)[0]
        self.assertNotIn("pierce_quiet", e2)

    def test_pierce_is_untouched_by_the_ladder_rule(self):
        """Pierce is a DIFFERENT mechanism (`sentinel.entry_pierces_quiet` reads this flag): it is set
        by the caller for Critical-and-above and carried verbatim, whether or not the row ladders."""
        import sentinel

        high = rs.seed_entries(self._row(importance="⭐ High", pierce_quiet=True), JUL15)
        self.assertEqual(len(high), 1)                                          # no ladder…
        self.assertTrue(all(sentinel.entry_pierces_quiet(e) for e in high))     # …still pierces

        nagging = rs.seed_entries(self._row(importance="✨ Notable", nag=True, pierce_quiet=True), JUL15)
        self.assertEqual(len(nagging), 11)                                      # ladders…
        self.assertTrue(all(sentinel.entry_pierces_quiet(e) for e in nagging))

        # And laddering never GRANTS pierce: a nagging row without the flag carries none.
        for e in rs.seed_entries(self._row(importance="📌 Low", nag=True), JUL15):
            self.assertNotIn("pierce_quiet", e)
            self.assertFalse(sentinel.entry_pierces_quiet(e))

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


class DueSuffix(unittest.TestCase):
    """An early/overdue One-off nudge names its own due date so it reads as a heads-up, never as "do
    it now" — see :func:`reminders_seed.due_suffix`."""

    def test_no_due_target_adds_nothing(self):
        self.assertEqual(rs.due_suffix(None, date(2026, 9, 11)), "")

    def test_fire_day_equal_to_due_day_adds_nothing(self):
        self.assertEqual(rs.due_suffix(date(2026, 9, 14), date(2026, 9, 14)), "")

    def test_an_early_fire_names_the_due_date(self):
        self.assertEqual(rs.due_suffix(date(2026, 9, 14), date(2026, 9, 11)), " — due Mon 09-14.")

    def test_an_overdue_fire_also_names_the_due_date(self):
        self.assertEqual(rs.due_suffix(date(2026, 9, 14), date(2026, 9, 16)), " — due Mon 09-14.")

    def test_seed_entries_appends_the_suffix_when_seeded_ahead_of_due_day(self):
        # A Today Todo due Monday, seeded on the Friday before.
        row = {"reminder_id": "x", "text": "Renew the passport.", "times": "08:00",
               "due_target": date(2026, 9, 14)}
        friday = datetime(2026, 9, 11, 0, 5, tzinfo=CDT)
        entries = rs.seed_entries(row, friday)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "Renew the passport. — due Mon 09-14.")

    def test_seed_entries_omits_the_suffix_on_its_own_due_day(self):
        row = {"reminder_id": "x", "text": "Renew the passport.", "times": "08:00",
               "due_target": date(2026, 9, 14)}
        monday = datetime(2026, 9, 14, 0, 5, tzinfo=CDT)
        self.assertEqual(rs.seed_entries(row, monday)[0]["text"], "Renew the passport.")

    def test_seed_entries_omits_the_suffix_when_no_due_target_given(self):
        # A Recurring Habit (or any row without a due_target) is unaffected.
        row = {"reminder_id": "x", "text": "Water the plants.", "time_window": "Morning"}
        self.assertEqual(rs.seed_entries(row, JUL15)[0]["text"], "Water the plants.")


class SeedLogAndAudit(unittest.TestCase):
    """The silent-skip detector. The daily reset is prompt-side (the subagent, against the store) and no
    stdlib script can perform it; `reminders.json` is a live queue pruned as entries fire, so by Wrap
    the evidence of a skipped row is gone. What code CAN do is notice that a row which seeded every day
    for a fortnight did not seed today — which is exactly the shape a selective miss takes."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _log(self, day, rid, slug=""):
        with open(os.path.join(self.d, rs.SEED_LOG_FILE), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"day": day, "reminder_id": rid, "slug": slug, "added": 2}) + "\n")

    def test_a_row_that_stops_seeding_is_reported(self):
        for day in ("2026-07-17", "2026-07-18", "2026-07-19"):
            self._log(day, "row-evening", "evening-stretch")
            self._log(day, "row-plants", "water-plants")
        self._log("2026-07-20", "row-plants", "water-plants")   # plants seeded; stretch did not
        rep = rs.audit_day(self.d, "2026-07-20")
        self.assertTrue(rep["available"])
        self.assertEqual([m["reminder_id"] for m in rep["missing"]], ["row-evening"])
        self.assertEqual(rep["missing"][0]["slug"], "evening-stretch")

    def test_a_row_seeded_today_is_not_reported(self):
        for day in ("2026-07-17", "2026-07-18", "2026-07-19", "2026-07-20"):
            self._log(day, "row-evening")
        self.assertEqual(rs.audit_day(self.d, "2026-07-20")["missing"], [])

    def test_a_brand_new_row_is_not_reported_as_missing(self):
        # min_days exists so a row with almost no history can't cry wolf — and so a genuinely retired
        # row stops being reported once it ages out of the window.
        self._log("2026-07-19", "row-new")
        self.assertEqual(rs.audit_day(self.d, "2026-07-20")["missing"], [])

    def test_no_log_at_all_is_unavailable_not_a_crash(self):
        rep = rs.audit_day(self.d, "2026-07-20")
        self.assertFalse(rep["available"])
        self.assertEqual(rep["missing"], [])

    def test_a_corrupt_line_costs_one_row(self):
        self._log("2026-07-17", "row-evening")
        with open(os.path.join(self.d, rs.SEED_LOG_FILE), "a", encoding="utf-8") as fh:
            fh.write("{ not json\n")
        self._log("2026-07-18", "row-evening")
        self._log("2026-07-19", "row-evening")
        self.assertEqual([m["reminder_id"] for m in rs.audit_day(self.d, "2026-07-20")["missing"]],
                         ["row-evening"])

    def test_the_seed_writes_its_own_log(self):
        """Producer coverage — the risk was never a buggy writer, it was a writer nothing calls."""
        row = {"reminder_id": "row-evening", "slug": "evening-stretch", "times": "20:00"}
        rs.refill_seed(self.d, row, datetime(2026, 7, 20, 8, 0, tzinfo=CDT))
        with open(os.path.join(self.d, rs.SEED_LOG_FILE), encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["reminder_id"], "row-evening")
        self.assertEqual(rec["day"], "2026-07-20")

    def test_a_broken_seed_log_never_costs_the_seed(self):
        # The house rule for state/ writers. A directory squatting on the path makes the append
        # impossible; the nudges must still be queued.
        os.makedirs(os.path.join(self.d, rs.SEED_LOG_FILE))
        row = {"reminder_id": "row-evening", "slug": "evening-stretch", "times": "20:00"}
        added = rs.refill_seed(self.d, row, datetime(2026, 7, 20, 8, 0, tzinfo=CDT))
        self.assertGreater(added, 0, "the seed must survive its own bookkeeping failing")


class NoLadderIsRecorded(unittest.TestCase):
    """The ladder-gap mitigation, disk side: "why didn't that Critical chase me?" has to be answerable
    from the seed log, because `reminders.json` is pruned as entries fire."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _rec(self):
        with open(os.path.join(self.d, rs.SEED_LOG_FILE), encoding="utf-8") as fh:
            return json.loads(fh.readline())

    def test_important_row_without_nag_is_flagged(self):
        rs.refill_seed(self.d, {"reminder_id": "row-x", "slug": "ship-it", "times": "09:00",
                                "importance": "🚨 Critical"}, JUL15)
        rec = self._rec()
        self.assertTrue(rec["no_ladder"])
        self.assertEqual(rec["importance"], "🚨 Critical")
        self.assertEqual(rec["added"], 1)          # seeded as asked — the flag refuses nothing

    def test_a_nagging_row_is_not_flagged(self):
        rs.refill_seed(self.d, {"reminder_id": "row-x", "slug": "ship-it", "times": "09:00",
                                "importance": "🚨 Critical", "nag": True}, JUL15)
        self.assertNotIn("no_ladder", self._rec())

    def test_a_low_stakes_row_is_not_flagged(self):
        rs.refill_seed(self.d, {"reminder_id": "row-y", "slug": "water-plants", "times": "09:00",
                                "importance": "📌 Low"}, JUL15)
        self.assertNotIn("no_ladder", self._rec())

    def test_audit_day_still_reads_a_flagged_record(self):
        # The extra keys must not disturb the silent-skip detector that shares this log.
        rs.refill_seed(self.d, {"reminder_id": "row-x", "slug": "ship-it", "times": "09:00",
                                "importance": "🚨 Critical"}, JUL15)
        rep = rs.audit_day(self.d, "2026-07-15")
        self.assertTrue(rep["available"])
        self.assertEqual(rep["seeded_today"], 1)


class PremiseReviewIsWired(unittest.TestCase):
    """`reminder_premise.review_due` is actually called against a live row via
    `reminders_seed.check_premise_review`/`refill_seed`. Omitting `--consecutive-misses` must change
    nothing."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _rec(self):
        with open(os.path.join(self.d, rs.SEED_LOG_FILE), encoding="utf-8") as fh:
            return json.loads(fh.readline())

    def test_omitting_consecutive_misses_is_a_no_op(self):
        row = {"reminder_id": "row-x", "slug": "walk", "times": "09:00", "importance": "✨ Notable"}
        self.assertIsNone(rs.check_premise_review(self.d, row))
        rs.refill_seed(self.d, row, JUL15)
        self.assertNotIn("premise_review_due", self._rec())

    def test_below_threshold_is_not_flagged(self):
        row = {"reminder_id": "row-x", "slug": "walk", "times": "09:00", "importance": "✨ Notable",
               "consecutive_misses": 2}
        rs.refill_seed(self.d, row, JUL15)
        self.assertNotIn("premise_review_due", self._rec())

    def test_at_threshold_is_flagged_and_logged(self):
        from reminder_premise import DEFAULT_THRESHOLD

        row = {"reminder_id": "row-x", "slug": "grab-screenshot", "times": "09:00",
               "importance": "📌 Low", "consecutive_misses": DEFAULT_THRESHOLD}
        rs.refill_seed(self.d, row, JUL15)
        rec = self._rec()
        self.assertTrue(rec["premise_review_due"])
        self.assertIn("grab-screenshot", rec["premise_review_question"])
        self.assertIn(str(DEFAULT_THRESHOLD), rec["premise_review_question"])

    def test_seeding_the_row_marks_the_local_store(self):
        from reminder_premise import DEFAULT_THRESHOLD
        import reminder_premise_track as rpt

        row = {"reminder_id": "row-x", "slug": "walk", "times": "09:00", "importance": "✨ Notable",
               "consecutive_misses": DEFAULT_THRESHOLD}
        rs.refill_seed(self.d, row, JUL15)
        self.assertEqual(rpt.last_reviewed_at_misses(self.d, "row-x"), DEFAULT_THRESHOLD)

    def test_a_row_asked_yesterday_is_not_re_asked_one_miss_later(self):
        from reminder_premise import DEFAULT_THRESHOLD
        import reminder_premise_track as rpt

        rpt.mark_reviewed(self.d, "row-x", DEFAULT_THRESHOLD)
        row = {"reminder_id": "row-x", "slug": "walk", "times": "09:00", "importance": "✨ Notable",
               "consecutive_misses": DEFAULT_THRESHOLD + 1}
        rs.refill_seed(self.d, row, JUL15)
        self.assertNotIn("premise_review_due", self._rec())

    def test_a_broken_review_store_never_costs_the_seed(self):
        # Same house rule as the seed log itself: bookkeeping never gets to block a nudge.
        import reminder_premise_track as rpt

        os.makedirs(os.path.join(self.d, rpt.STORE_FILE))
        row = {"reminder_id": "row-x", "slug": "walk", "times": "09:00", "importance": "✨ Notable",
               "consecutive_misses": 99}
        self.assertGreater(rs.refill_seed(self.d, row, JUL15), 0)

    def test_dry_run_never_marks_the_store(self):
        from reminder_premise import DEFAULT_THRESHOLD
        import reminder_premise_track as rpt

        row = {"reminder_id": "row-x", "slug": "walk", "times": "09:00", "importance": "✨ Notable",
               "consecutive_misses": DEFAULT_THRESHOLD}
        self.assertIsNotNone(rs.check_premise_review(self.d, row, mark=False))
        self.assertIsNone(rpt.last_reviewed_at_misses(self.d, "row-x"))


if __name__ == "__main__":
    unittest.main()
