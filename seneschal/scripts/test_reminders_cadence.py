#!/usr/bin/env python3
"""Tests for :mod:`reminders_cadence` — the general cadence mechanism.

What each class is guarding, since a test name alone never says why:

``TheEnumerationIsGone``      the point of the change. An interval nobody ever added to the select
                             works, and every value that WAS in the select still means what it meant.
``TheEvenSpread``            Bresenham. The three sequences the requirement names, plus the
                             invariants (sums to the period, gaps differ by at most one) that make
                             "as evenly as possible" a property rather than three lucky examples.
``TheRateIsGridAnchored``    the decision. A rate self-corrects after a late ack; an interval does not.
                             These are the two halves of one decision and they are tested together
                             on purpose — the distinction IS the design.
``FractionalDaysAreRefused`` the refusal, at the boundary. A refusal that does not name the
                             working spelling is a refusal the owner has to guess their way out of.
``TheShapesThatArentIntervals`` Four shapes a bare number cannot express, none quietly dropped.
``LegacyRowsKeepFiring``     the migration's whole safety argument, asserted rather than claimed.
``TheMultiFireGateIsNeverNarrower`` the one live code path this change touches.
``TheAuditIsTheMigrationPreflight`` the artefact someone with store access runs before retyping it.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reminders_cadence as rc  # noqa: E402


#: Every option in the classic ⏰ ``Cadence`` select, spelled EXACTLY as a Notion select holds it.
#: The migration's safety argument is that this list parses.
LIVE_SELECT_OPTIONS = (
    "Daily", "Weekdays", "Every 2 days", "Every 3 days", "Every 4 days", "Every 5 days",
    "Weekly", "Multiple/day", "One-off",
)


class TheEnumerationIsGone(unittest.TestCase):
    """An arbitrary interval — the change's reason for existing."""

    def test_eleven_days_needs_nothing_added_anywhere(self):
        """11 was never in the select and never will need to be — no one-off additions."""
        cad = rc.parse("every 11 days")
        self.assertEqual(cad.shape, rc.INTERVAL)
        self.assertEqual(cad.n, 11)

    def test_an_arbitrary_interval_fires_on_the_right_day_and_not_before(self):
        ack = date(2026, 8, 31)
        for offset, want in ((10, False), (11, True), (12, True)):
            with self.subTest(offset=offset):
                self.assertIs(rc.is_due("every 11 days", today=ack + timedelta(days=offset),
                                        last_ack=ack), want)

    def test_a_span_of_intervals_nobody_enumerated(self):
        for n in (6, 7, 8, 9, 13, 21, 90, 365):
            with self.subTest(n=n):
                self.assertEqual(rc.parse("every %d days" % n).n, n)

    def test_the_canonical_spelling_round_trips(self):
        for text in ("Every 4 days", "2 per week", "daily", "One-off", "Weekdays", "Weekly",
                     "Multiple/day", "every 11 days", "3 per 10 days"):
            with self.subTest(text=text):
                self.assertEqual(rc.parse(rc.parse(text).canonical()), rc.parse(text))


class TheEvenSpread(unittest.TestCase):
    """Bresenham / Euclidean-rhythm distribution — the named algorithm, not an invention."""

    def test_two_per_week_alternates_three_then_four(self):
        """"Twice per week" means alternating 3 days then 4 days — an even spread."""
        self.assertEqual(rc.gaps(2, 7), [3, 4])

    def test_three_per_week_is_two_two_three(self):
        self.assertEqual(rc.gaps(3, 7), [2, 2, 3])

    def test_three_per_ten_days_is_three_three_four(self):
        self.assertEqual(rc.gaps(3, 10), [3, 3, 4])

    def test_a_rate_cycles_forever_rather_than_running_out(self):
        """3,4,3,4 — the *repeating* sequence, walked on the real lattice rather than read off
        :func:`gaps`. A distribution that is right for one period and wrong for the next is not one."""
        cur, seen = rc.RATE_EPOCH, []
        for _ in range(6):
            nxt = rc.next_rate_day(2, 7, cur)
            seen.append((nxt - cur).days)
            cur = nxt
        self.assertEqual(seen, [3, 4, 3, 4, 3, 4])

    def test_every_spread_sums_to_its_period_and_is_flat(self):
        """The two properties that make it *even*: the gaps tile the period exactly, and no gap is
        more than one day longer than any other. Checked over a range rather than three cases."""
        for d in range(1, 40):
            for n in range(1, d + 1):
                with self.subTest(n=n, d=d):
                    g = rc.gaps(n, d)
                    self.assertEqual(sum(g), d)
                    self.assertEqual(len(g), n)
                    self.assertLessEqual(max(g) - min(g), 1)

    def test_the_lattice_and_the_gap_sequence_are_the_same_object(self):
        """:func:`gaps` documents what :func:`next_rate_day` does; a drift between them would make
        the docs a lie while every individual test still passed."""
        for n, d in ((2, 7), (3, 7), (3, 10), (5, 8), (4, 30)):
            with self.subTest(n=n, d=d):
                cur, walked = rc.RATE_EPOCH, []
                for _ in range(n):
                    nxt = rc.next_rate_day(n, d, cur)
                    walked.append((nxt - cur).days)
                    cur = nxt
                self.assertEqual(walked, rc.gaps(n, d))

    def test_two_per_week_lands_on_monday_and_thursday(self):
        """The visible consequence of anchoring the lattice on a Monday, and the reason the epoch is
        frozen: moving it re-phases every live rate row."""
        cur, days = rc.RATE_EPOCH, {rc.RATE_EPOCH.strftime("%a")}
        for _ in range(10):
            cur = rc.next_rate_day(2, 7, cur)
            days.add(cur.strftime("%a"))
        self.assertEqual(days, {"Mon", "Thu"})

    def test_three_per_week_lands_on_monday_wednesday_friday(self):
        cur, days = rc.RATE_EPOCH, {rc.RATE_EPOCH.strftime("%a")}
        for _ in range(12):
            cur = rc.next_rate_day(3, 7, cur)
            days.add(cur.strftime("%a"))
        self.assertEqual(days, {"Mon", "Wed", "Fri"})

    def test_rate_spellings_all_reach_the_same_cadence(self):
        want = rc.Cadence(rc.RATE, n=2, d=7)
        for text in ("2 per week", "2 per 7", "2 per 7 days", "2/week", "2 x 7 days", "2 PER WEEK"):
            with self.subTest(text=text):
                self.assertEqual(rc.parse(text), want)


class TheRateIsGridAnchored(unittest.TestCase):
    """THE DECISION. An interval is ack-relative; a rate is grid-anchored. Both halves, together."""

    def test_a_late_ack_pushes_an_interval_back(self):
        """"Every 4 days" means *four days after you last did it* — the offset is the semantic, so
        slipping three days slips the next one three days."""
        on_time = rc.next_due("every 4 days", date(2026, 9, 1))
        late = rc.next_due("every 4 days", date(2026, 9, 4))
        self.assertEqual(on_time, date(2026, 9, 5))
        self.assertEqual(late, date(2026, 9, 8))
        self.assertEqual((late - on_time).days, 3)  # the whole slip, carried forward

    def test_a_late_ack_does_not_push_a_rate_back(self):
        """"Twice a week" means *twice in every week* — the count is the semantic, so a late ack
        rejoins the lattice instead of dragging it. Without this, three days of slippage quietly
        turns 2-per-7 into 2-per-10 and the rate is whatever the owner's week happened to be."""
        # 2026-08-31 is a Monday and a lattice point; the next is Thursday 09-03.
        self.assertEqual(rc.next_due("2 per week", date(2026, 8, 31)), date(2026, 9, 3))
        # Acked two days late (Wednesday) — still Thursday. The schedule did not move.
        self.assertEqual(rc.next_due("2 per week", date(2026, 9, 2)), date(2026, 9, 3))

    def test_a_rate_needs_no_stored_cycle_position(self):
        """The state argument, made executable: the next due date is a pure function of (cadence,
        last ack). Nothing is remembered between calls, so there is no cycle position to persist,
        migrate, or reconcile in Dream."""
        for ack in (date(2026, 8, 31), date(2026, 9, 2), date(2027, 1, 14)):
            with self.subTest(ack=ack):
                self.assertEqual(rc.next_due("2 per week", ack), rc.next_due("2 per week", ack))

    def test_the_lattice_is_stable_arbitrarily_far_from_its_epoch(self):
        """The O(1) ceil-division must be right for dates far after — and before — the anchor,
        where Python's floor division on negatives is the easy thing to get wrong."""
        for ack in (date(2020, 3, 1), date(2023, 12, 31), date(2024, 1, 1), date(2031, 7, 4)):
            with self.subTest(ack=ack):
                nxt = rc.next_rate_day(2, 7, ack)
                self.assertGreater(nxt, ack)
                self.assertLessEqual((nxt - ack).days, 4)
                self.assertIn(nxt.strftime("%a"), ("Mon", "Thu"))

    def test_a_rate_row_with_no_ack_is_due_now(self):
        """Same fail-open convention intervals have had since 2026-06-28."""
        self.assertTrue(rc.is_due("2 per week", today=date(2026, 9, 2), last_ack=None))

    def test_a_rate_is_not_due_between_lattice_points(self):
        ack = date(2026, 8, 31)  # Monday, a lattice point
        self.assertFalse(rc.is_due("2 per week", today=date(2026, 9, 1), last_ack=ack))
        self.assertFalse(rc.is_due("2 per week", today=date(2026, 9, 2), last_ack=ack))
        self.assertTrue(rc.is_due("2 per week", today=date(2026, 9, 3), last_ack=ack))

    def test_weekly_and_every_seven_days_and_one_per_week_are_three_different_things(self):
        """Three spellings of "about once a week" that are deliberately NOT collapsed: `weekly` may
        be pinned to a weekday by `Notes`, `every 7 days` drifts with the owner's acks, `1 per 7 days` does
        not. Collapsing them would silently change live rows."""
        self.assertEqual(rc.parse("weekly").shape, rc.WEEKLY)
        self.assertEqual(rc.parse("every 7 days"), rc.Cadence(rc.INTERVAL, n=7))
        self.assertEqual(rc.parse("1 per 7 days"), rc.Cadence(rc.RATE, n=1, d=7))
        late = date(2026, 9, 2)  # a Wednesday, two days after a Monday lattice point
        self.assertEqual(rc.next_due("every 7 days", late), date(2026, 9, 9))
        self.assertEqual(rc.next_due("1 per 7 days", late), date(2026, 9, 7))  # back on Monday


class FractionalDaysAreRefused(unittest.TestCase):
    """The refusal, at the boundary. The seed runs once a day, so a half-day offset has no wake to fire it."""

    def test_three_and_a_half_days_is_refused_and_told_the_rate_spelling(self):
        """`3.5` days IS expressible as `2 per 7`, and that spelling wins — it is the one that is
        true. A refusal that does not say so is a dead end."""
        with self.assertRaises(rc.CadenceError) as ctx:
            rc.parse("every 3.5 days")
        self.assertIn("2 per 7 days", str(ctx.exception))

    def test_the_suggested_rate_is_the_same_cadence_it_refused(self):
        """The suggestion has to be *correct*, not merely present: one event per 3.5 days is exactly
        two events per 7 days, and the mean gap proves it."""
        cad = rc.parse("2 per 7 days")
        self.assertEqual(sum(rc.gaps(cad.n, cad.d)) / cad.n, 3.5)

    def test_other_fractions_get_their_own_correct_rate(self):
        for text, want in (("every 2.5 days", "2 per 5 days"), ("every 1.5 days", "2 per 3 days"),
                           ("every 3.25 days", "4 per 13 days")):
            with self.subTest(text=text):
                with self.assertRaises(rc.CadenceError) as ctx:
                    rc.parse(text)
                self.assertIn(want, str(ctx.exception))

    def test_a_sub_day_fraction_is_pointed_at_multiple_per_day_not_at_a_rate(self):
        """`every 0.5 days` is twice a day. "2 per 1 days" would be a nonsense suggestion, so the
        message names the mechanism that actually owns intraday firing."""
        with self.assertRaises(rc.CadenceError) as ctx:
            rc.parse("every 0.5 days")
        self.assertIn("multiple/day", str(ctx.exception))
        self.assertIn("`Times`", str(ctx.exception))

    def test_an_integral_float_is_just_an_integer(self):
        """The refusal is about a fraction that cannot land on a day, not about a decimal point."""
        self.assertEqual(rc.parse("every 3.0 days"), rc.Cadence(rc.INTERVAL, n=3))
        self.assertEqual(rc.parse("every 11.00 days"), rc.Cadence(rc.INTERVAL, n=11))

    def test_the_decimal_is_read_exactly_not_through_a_float(self):
        """`Fraction(0.1)` is 3602879701896397/36028797018963968; `Fraction("0.1")` is 1/10. Parsing
        the TEXT is what keeps the suggested rate from being binary noise."""
        with self.assertRaises(rc.CadenceError) as ctx:
            rc.parse("every 3.3 days")
        self.assertIn("10 per 33 days", str(ctx.exception))

    def test_zero_and_negative_intervals_are_refused(self):
        for text in ("every 0 days", "every 0.0 days"):
            with self.subTest(text=text):
                with self.assertRaises(rc.CadenceError):
                    rc.parse(text)

    def test_a_rate_denser_than_daily_is_refused_by_name(self):
        for text in ("3 per day", "8 per 7 days", "2 per 1 days"):
            with self.subTest(text=text):
                with self.assertRaises(rc.CadenceError) as ctx:
                    rc.parse(text)
                self.assertIn("multiple/day", str(ctx.exception))

    def test_a_fractional_event_count_is_refused(self):
        with self.assertRaises(rc.CadenceError):
            rc.parse("1.5 per week")

    def test_a_rate_that_is_exactly_daily_is_daily(self):
        self.assertEqual(rc.parse("7 per week").shape, rc.DAILY)


class TheShapesThatArentIntervals(unittest.TestCase):
    """A bare number cannot express these four, and none of them was quietly dropped."""

    def test_daily_is_due_every_day_including_weekends(self):
        self.assertEqual(rc.parse("Daily").shape, rc.DAILY)
        for day in range(7):
            with self.subTest(weekday=day):
                self.assertTrue(rc.is_due("Daily", today=date(2026, 8, 31) + timedelta(days=day)))

    def test_weekdays_is_monday_to_friday_only(self):
        # 2026-08-31 is a Monday.
        want = [True, True, True, True, True, False, False]
        got = [rc.is_due("Weekdays", today=date(2026, 8, 31) + timedelta(days=i)) for i in range(7)]
        self.assertEqual(got, want)

    def test_multiple_per_day_is_always_due_and_times_owns_the_rest(self):
        """The shape says *when in the day*, which `Times` and `reminders_roll.py` own — so the
        date-level answer is always yes."""
        self.assertEqual(rc.parse("Multiple/day").shape, rc.MULTIPLE)
        self.assertTrue(rc.is_due("Multiple/day", today=date(2026, 9, 5), last_ack=date(2026, 9, 5)))

    def test_one_off_surfaces_within_the_lookahead_and_stays_due_when_overdue(self):
        target = date(2026, 9, 10)
        for today, want in ((date(2026, 9, 6), False), (date(2026, 9, 7), True),
                            (date(2026, 9, 10), True), (date(2026, 9, 30), True)):
            with self.subTest(today=today):
                self.assertIs(rc.is_due("One-off", today=today, due_target=target), want)

    def test_a_one_off_with_no_target_is_due_rather_than_silent(self):
        self.assertTrue(rc.is_due("One-off", today=date(2026, 9, 6), due_target=None))


class TheOneOffLookaheadIsTypeGated(unittest.TestCase):
    """The bug: a Today Todo due Monday 09-14 fired Friday morning — three days early, reading as
    "do it now." `ONE_OFF_LOOKAHEAD_DAYS` (3) is right for a Deadline
    Watch and wrong for a Today Todo appointment. The fix is a `lookahead_days` parameter the SEED-side
    caller sets from the row's `Type` — 0 for Today Todo, the default for Deadline Watch — and this
    module never learns what a "Type" is."""

    TARGET = date(2026, 9, 14)  # Monday

    def test_reproduces_the_bug_at_the_default_lookahead(self):
        """The exact repro from the report: unqualified, a Friday-fired Monday one-off is 'due'."""
        self.assertTrue(rc.is_due("One-off", today=date(2026, 9, 11), due_target=self.TARGET))

    def test_today_todo_lookahead_zero_is_not_due_until_its_own_day(self):
        for today, want in ((date(2026, 9, 11), False), (date(2026, 9, 12), False),
                            (date(2026, 9, 13), False), (date(2026, 9, 14), True)):
            with self.subTest(today=today):
                self.assertIs(rc.is_due("One-off", today=today, due_target=self.TARGET,
                                        lookahead_days=0), want)

    def test_today_todo_lookahead_zero_still_reads_overdue_as_due(self):
        self.assertTrue(rc.is_due("One-off", today=date(2026, 9, 15), due_target=self.TARGET,
                                  lookahead_days=0))

    def test_deadline_watch_default_lookahead_is_unchanged(self):
        """No `lookahead_days` passed — every existing Deadline Watch caller keeps firing from
        09-11 (3 days out), exactly as before this change."""
        for today, want in ((date(2026, 9, 10), False), (date(2026, 9, 11), True),
                            (date(2026, 9, 14), True)):
            with self.subTest(today=today):
                self.assertIs(rc.is_due("One-off", today=today, due_target=self.TARGET), want)

    def test_next_due_also_takes_the_lookahead(self):
        self.assertEqual(rc.next_due("One-off", None, due_target=self.TARGET, lookahead_days=0),
                         self.TARGET)
        self.assertEqual(rc.next_due("One-off", None, due_target=self.TARGET),
                         self.TARGET - timedelta(days=3))

    def test_a_today_todo_with_no_due_target_still_fails_open(self):
        """A Today Todo with an empty `Due / Target` is still due — the fail-open convention doesn't
        depend on the lookahead at all."""
        self.assertTrue(rc.is_due("One-off", today=date(2026, 9, 11), due_target=None,
                                  lookahead_days=0))

    def test_cli_lookahead_days_flag_reproduces_the_fix(self):
        code, out, _ = self._cli_due(["--due", "One-off", "--today", "2026-09-11",
                                      "--due-target", "2026-09-14", "--lookahead-days", "0"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["due"])

    def test_cli_omitting_lookahead_days_keeps_the_default(self):
        code, out, _ = self._cli_due(["--due", "One-off", "--today", "2026-09-11",
                                      "--due-target", "2026-09-14"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["due"])

    def _cli_due(self, argv):
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            code = rc.main(argv)
            return code, sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    def test_weekly_pinned_by_notes_fires_on_that_weekday(self):
        """The existing rule: *if `Notes` names a weekday (e.g. "Fridays"), due that weekday*."""
        friday, thursday = date(2026, 9, 4), date(2026, 9, 3)
        self.assertTrue(rc.is_due("Weekly", today=friday, notes="Fridays — bins out"))
        self.assertFalse(rc.is_due("Weekly", today=thursday, notes="Fridays — bins out"))

    def test_weekly_pinned_by_notes_fires_on_every_weekday_name(self):
        """Regression for the 2026-09-18 bug: `notes_weekday()`'s regex was built as
        `name[:3] + "(s|days?)?"`, which only reconstructs the full name for monday/friday/sunday
        ("mon"+"day", "fri"+"day", "sun"+"day"). tuesday/wednesday/thursday/saturday all have extra
        letters between the 3-letter prefix and "day" ("tue"+"s"+"day", "wed"+"nes"+"day",
        "thu"+"rs"+"day", "sat"+"ur"+"day"), so those four never matched at all -- a Weekly reminder
        named for one of them silently never fired on the day named. Covers all seven, including the
        three that already worked, as a regression floor."""
        cases = [
            ("Mondays", date(2026, 9, 7), date(2026, 9, 6)),
            ("Tuesdays", date(2026, 9, 1), date(2026, 8, 31)),
            ("Wednesdays", date(2026, 9, 2), date(2026, 9, 1)),
            ("Thursdays", date(2026, 9, 3), date(2026, 9, 2)),
            ("Fridays", date(2026, 9, 4), date(2026, 9, 3)),
            ("Saturdays", date(2026, 9, 5), date(2026, 9, 4)),
            ("Sundays", date(2026, 9, 6), date(2026, 9, 5)),
        ]
        for notes, on_day, off_day in cases:
            with self.subTest(notes=notes):
                self.assertTrue(rc.is_due("Weekly", today=on_day, notes=notes))
                self.assertFalse(rc.is_due("Weekly", today=off_day, notes=notes))

    def test_notes_weekday_recognizes_three_letter_abbreviations(self):
        abbrevs = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        for abbrev, weekday in abbrevs.items():
            with self.subTest(abbrev=abbrev):
                self.assertEqual(rc.notes_weekday(abbrev), weekday)

    def test_weekly_with_no_weekday_in_notes_falls_back_to_seven_days(self):
        ack = date(2026, 8, 31)
        self.assertFalse(rc.is_due("Weekly", today=ack + timedelta(days=6), last_ack=ack, notes=""))
        self.assertTrue(rc.is_due("Weekly", today=ack + timedelta(days=7), last_ack=ack, notes=""))

    def test_weekly_with_two_weekdays_in_notes_is_ambiguous_and_falls_back(self):
        """Two named days is not a pin. Falling back to +7 is the safe direction — it keeps firing;
        picking one of the two would silently invent a schedule the owner never set."""
        self.assertIsNone(rc.notes_weekday("Mondays and Fridays"))
        ack = date(2026, 8, 31)
        self.assertTrue(rc.is_due("Weekly", today=ack + timedelta(days=7), last_ack=ack,
                                  notes="Mondays and Fridays"))

    def test_no_shape_was_dropped(self):
        """Seven shapes, and every live select option maps into one of them."""
        self.assertEqual(len(rc.SHAPES), 7)
        self.assertEqual({rc.parse(o).shape for o in LIVE_SELECT_OPTIONS},
                         {rc.DAILY, rc.WEEKDAYS, rc.INTERVAL, rc.WEEKLY, rc.MULTIPLE, rc.ONE_OFF})


class LegacyRowsKeepFiring(unittest.TestCase):
    """The migration's safety argument, asserted rather than claimed: the store holds the old strings
    for as long as the owner likes, and the new code reads every one of them correctly."""

    def test_every_live_select_option_parses(self):
        for option in LIVE_SELECT_OPTIONS:
            with self.subTest(option=option):
                self.assertIsNotNone(rc.try_parse(option))

    def test_an_unmigrated_every_4_days_row_still_fires_on_day_four(self):
        """A row like `Descale the kettle`, `Every 4 days`, acked 2026-08-31, with the store still
        holding the select value verbatim."""
        ack = date(2026, 8, 31)
        self.assertFalse(rc.is_due("Every 4 days", today=date(2026, 9, 3), last_ack=ack))
        self.assertTrue(rc.is_due("Every 4 days", today=date(2026, 9, 4), last_ack=ack))

    def test_title_case_and_lower_case_agree(self):
        for option in LIVE_SELECT_OPTIONS:
            with self.subTest(option=option):
                self.assertEqual(rc.parse(option), rc.parse(option.lower()))

    def test_a_value_pasted_out_of_a_rich_text_store_still_parses(self):
        """Non-breaking spaces and the dash family survive a copy-paste; a cadence should too."""
        for text in ("Every 4 days", "One–off", "  Weekly  ", "Every 4 days."):
            with self.subTest(text=text):
                self.assertIsNotNone(rc.try_parse(text))

    def test_an_unparseable_cadence_is_due_rather_than_silent(self):
        """The fail-open decision. A typo that nags gets fixed; a typo that goes quiet does not."""
        self.assertIsNone(rc.try_parse("evrey 4 days"))
        self.assertTrue(rc.is_due("evrey 4 days", today=date(2026, 9, 3), last_ack=date(2026, 9, 3)))

    def test_the_placeholder_dash_in_a_fixture_is_not_a_cadence_and_does_not_raise(self):
        """`test_ack.py`'s id-cache fixture writes `—` where this repo does not know a row's real
        cadence. Nothing may crash on it."""
        self.assertIsNone(rc.try_parse("—"))
        self.assertFalse(rc.is_multi_fire("—"))


class TheMultiFireGateIsNeverNarrower(unittest.TestCase):
    """The one live code path this change rewires: `reminders_acks.id_cache_titles`'s `multi_fire`.

    A narrower predicate here would newly *gate* rolls the fire-time ack gate has always exempted —
    a silent-suppression bug, which is why the fallback to the old substring test is deliberate."""

    LEGACY_TOKEN = "multiple"

    def _legacy(self, text):
        return self.LEGACY_TOKEN in text.lower()

    def test_it_agrees_with_the_substring_test_it_replaced(self):
        for text in ("Multiple/day", "multiple/day", "Multiple/day (roll)", "Every 4 days", "Daily",
                     "Weekly", "One-off", "—", "", "multiple per day", "MULTIPLE/DAY"):
            with self.subTest(text=text):
                self.assertIs(rc.is_multi_fire(text), self._legacy(text))

    def test_a_non_string_is_not_multi_fire(self):
        for value in (None, 4, [], {}):
            with self.subTest(value=value):
                self.assertFalse(rc.is_multi_fire(value))

    def test_reminders_acks_holds_no_cadence_vocabulary_of_its_own(self):
        """**Structural, and it has to be.** `is_multi_fire` is contractually never narrower than the
        substring test it replaced, so no input can distinguish the two behaviourally — a
        behavioural test here passes just as well on the un-wired code. The property that actually
        changed is that `reminders_acks` no longer spells a cadence value at all."""
        import reminders_acks as ra

        src = io.open(ra.__file__, encoding="utf-8").read()
        self.assertIs(ra._MULTI_FIRE_CADENCE, rc.is_multi_fire)
        self.assertIn("import reminders_cadence", src)
        self.assertNotIn('"multiple"', src)
        self.assertNotIn("in cadence.lower()", src)

    def test_reminders_acks_reads_the_cadence_column_through_it(self):
        """Wired, not merely available — the id cache's `Cadence` column must reach this module."""
        import reminders_acks as ra

        with tempfile.TemporaryDirectory() as tmp:
            with io.open(os.path.join(tmp, ra.ID_CACHE_FILE), "w", encoding="utf-8") as fh:
                fh.write(
                    "| Reminder | Page id | Type | Window | Cadence | Importance |\n"
                    "|---|---|---|---|---|---|\n"
                    "| Check messages | `000000000000000000000000000000e1` | Habit | Anytime "
                    "| Multiple/day | Low |\n"
                    "| Descale the kettle | `000000000000000000000000000000e2` | Habit | Morning "
                    "| Every 4 days | Low |\n"
                    "| Take a walk | `000000000000000000000000000000e3` | Habit | Anytime "
                    "| 2 per week | Low |\n")
            rows = ra.id_cache_titles(tmp)
        by_title = {r["title"]: r["multi_fire"] for r in rows.values()}
        self.assertTrue(by_title["Check messages"])
        self.assertFalse(by_title["Descale the kettle"])
        self.assertFalse(by_title["Take a walk"])  # a rate is a once-a-day row, and must be gated


class TheAuditIsTheMigrationPreflight(unittest.TestCase):
    """The migration pre-flight: something a person with store access runs BEFORE retyping it."""

    def test_a_clean_sheet_reports_ok(self):
        result = rc.audit([(o, o) for o in LIVE_SELECT_OPTIONS])
        self.assertTrue(result["ok"])
        self.assertEqual(result["parsed"], len(LIVE_SELECT_OPTIONS))
        self.assertEqual(result["failed"], 0)

    def test_one_bad_row_fails_the_sheet_and_names_itself(self):
        result = rc.audit([("Descale the kettle", "Every 4 days"), ("Typo row", "evrey 4 days")])
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], 1)
        bad = [r for r in result["rows"] if not r["ok"]]
        self.assertEqual(bad[0]["label"], "Typo row")
        self.assertIn("not a cadence", bad[0]["error"])

    def test_it_reads_the_live_id_cache_table_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with io.open(os.path.join(tmp, rc.ID_CACHE_FILE), "w", encoding="utf-8") as fh:
                fh.write(
                    "Some prose above the table.\n\n"
                    "| Reminder | Page id | Type | Window | Cadence | Importance |\n"
                    "|---|---|---|---|---|---|\n"
                    "| Descale the kettle | `00000000-0000-0000-0000-0000000000f1` | Habit | Morning "
                    "| Every 4 days | 📌 Low |\n"
                    "| Water the fern | `00000000-0000-0000-0000-0000000000f2` | Habit | Midday "
                    "| Every 5 days | 🛑 Super-Critical |\n")
            rows = rc.id_cache_cadences(tmp)
        self.assertEqual(rows, [("Descale the kettle", "Every 4 days"), ("Water the fern", "Every 5 days")])

    def test_an_absent_cache_is_empty_not_an_exception(self):
        self.assertEqual(rc.id_cache_cadences(os.path.join(tempfile.gettempdir(), "no-such-dir")),
                         [])

    def test_the_cli_audit_exits_nonzero_when_a_row_would_not_parse(self):
        self.assertEqual(self._cli_audit("Every 4 days\n2 per week\n"), 0)
        self.assertEqual(self._cli_audit("Every 4 days\nevrey 4 days\n"), 1)

    def _cli_audit(self, text):
        old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
        sys.stdin, sys.stdout, sys.stderr = io.StringIO(text), io.StringIO(), io.StringIO()
        try:
            return rc.main(["--audit", "--from-stdin"])
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err


class TheCliSpeaksJson(unittest.TestCase):
    """The mode calls this rather than doing date arithmetic in prose, so its output is a contract."""

    def _run(self, argv):
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            code = rc.main(argv)
            return code, sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    def test_parse_prints_the_shape_and_magnitude(self):
        code, out, _ = self._run(["--parse", "every 11 days"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["shape"], rc.INTERVAL)
        self.assertEqual(json.loads(out)["n"], 11)

    def test_due_answers_with_the_next_date(self):
        code, out, _ = self._run(["--due", "2 per week", "--last-ack", "2026-08-31",
                                  "--today", "2026-09-02"])
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertFalse(body["due"])
        self.assertEqual(body["next_due"], "2026-09-03")

    def test_gaps_prints_the_sequence(self):
        code, out, _ = self._run(["--gaps", "2 per week"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["gaps"], [3, 4])

    def test_gaps_refuses_a_non_rate_rather_than_inventing_one(self):
        code, _, err = self._run(["--gaps", "every 4 days"])
        self.assertEqual(code, 2)
        self.assertIn("not a rate", err)

    def test_a_refused_cadence_exits_two_and_prints_the_working_spelling(self):
        code, _, err = self._run(["--parse", "every 3.5 days"])
        self.assertEqual(code, 2)
        self.assertIn("2 per 7 days", err)


if __name__ == "__main__":
    unittest.main()
