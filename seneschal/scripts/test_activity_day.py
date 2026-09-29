#!/usr/bin/env python3
"""Tests for ``activity_day.py`` — the after-midnight rule, cut at the owner's day boundary.

Every instant here is **frozen and explicit**, and the zone and identity are **patched**. That is not
style: the module answers "which day is it?", so a test that reads the wall clock (or the runner's
own timezone, or a real ``persona/identity.json`` on a dev box) agrees with itself on exactly one
calendar day in exactly one timezone.

The fixed zone is UTC-5 in "summer" and UTC-6 in "winter" (two ``timezone`` constants, no tzdata),
so the offset arithmetic is exercised in both directions without a real IANA zone.

Run:  python -m unittest test_activity_day
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import activity_day as ad  # noqa: E402
import clock  # noqa: E402
import tz_common  # noqa: E402

SUMMER = timezone(timedelta(hours=-5))
WINTER = timezone(timedelta(hours=-6))


class _Pinned(unittest.TestCase):
    zone = SUMMER
    identity: dict = {}

    def setUp(self):
        z = mock.patch.object(tz_common, "_zone", return_value=self.zone)
        i = mock.patch.object(clock, "load_identity", return_value=self.identity)
        z.start()
        i.start()
        self.addCleanup(z.stop)
        self.addCleanup(i.stop)


class TheCut(_Pinned):
    """00:00 up to the boundary (default 05:00) belongs to the previous day; the boundary opens the
    new one."""

    def test_before_the_cut_is_the_prior_day(self):
        self.assertEqual(ad.from_local(datetime(2026, 8, 12, 0, 1)), date(2026, 8, 11))
        self.assertEqual(ad.from_local(datetime(2026, 8, 12, 1, 29)), date(2026, 8, 11))
        self.assertEqual(ad.from_local(datetime(2026, 8, 12, 4, 59)), date(2026, 8, 11))

    def test_the_cut_itself_opens_the_new_day(self):
        self.assertEqual(ad.from_local(datetime(2026, 8, 12, 5, 0)), date(2026, 8, 12))
        self.assertEqual(ad.from_local(datetime(2026, 8, 12, 23, 59)), date(2026, 8, 12))

    def test_default_constant(self):
        self.assertEqual(ad.DEFAULT_DAY_CUT_HOUR, 5)
        self.assertEqual(ad.cut_hour(), 5)


class ConfiguredBoundary(_Pinned):
    identity = {"owner": {"dayBoundaryHour": 3}}

    def test_the_configured_hour_is_the_cut(self):
        self.assertEqual(ad.cut_hour(), 3)
        self.assertEqual(ad.from_local(datetime(2026, 8, 12, 2, 59)), date(2026, 8, 11))
        self.assertEqual(ad.from_local(datetime(2026, 8, 12, 3, 0)), date(2026, 8, 12))

    def test_an_explicit_cut_hour_still_wins(self):
        self.assertEqual(ad.from_local(datetime(2026, 8, 12, 4, 0), cut_hour=5), date(2026, 8, 11))

    def test_today_follows_the_configured_hour(self):
        # 03:30 local (UTC-5) = 08:30 UTC: past a 03:00 cut, so the calendar day itself.
        self.assertEqual(ad.today(datetime(2026, 8, 12, 8, 30, tzinfo=timezone.utc)), date(2026, 8, 12))


class FromAUtcInstant(_Pinned):
    """``due_at`` is UTC. The owner's wall clock first, then the cut — and the mapping matters BOTH
    ways."""

    def test_evening_nudge_belongs_to_its_own_calendar_day(self):
        # 2026-08-12 21:30 local (UTC-5)
        self.assertEqual(ad.from_instant("2026-08-13T02:30:00Z"), date(2026, 8, 12))

    def test_after_midnight_nudge_belongs_to_the_PREVIOUS_calendar_day(self):
        # 2026-08-12 00:30 local — the next calendar date, the same activity day as the 11th's evening
        self.assertEqual(ad.from_instant("2026-08-12T05:30:00Z"), date(2026, 8, 11))

    def test_the_offset_is_read_from_the_zone_not_assumed(self):
        # 2026-12-12 21:30 local under UTC-6
        with mock.patch.object(tz_common, "_zone", return_value=WINTER):
            self.assertEqual(ad.from_instant("2026-12-13T03:30:00Z"), date(2026, 12, 12))

    def test_accepts_the_shapes_the_queue_and_its_callers_write(self):
        for value in ("2026-08-13T02:30:00Z", "2026-08-13T02:30:00+00:00",
                      "2026-08-12T21:30:00-05:00", "2026-08-13T02:30:00",
                      datetime(2026, 8, 13, 2, 30, tzinfo=timezone.utc)):
            self.assertEqual(ad.from_instant(value), date(2026, 8, 12), value)

    def test_unreadable_is_None_and_None_is_not_now(self):
        """``None`` must not mean "now": a queue entry with no ``due_at`` would be dated to today —
        the very day whose nudges get deleted."""
        for value in (None, "", "   ", "not-a-date", 17, {}):
            self.assertIsNone(ad.from_instant(value), value)
            self.assertIsNone(ad.to_owner_local(value), value)


class Today(_Pinned):
    def test_an_after_midnight_report_belongs_to_the_evening_before(self):
        """01:29 local, reporting the previous evening's dinner: the previous day is the answer."""
        self.assertEqual(ad.today(datetime(2026, 8, 12, 6, 29, tzinfo=timezone.utc)), date(2026, 8, 11))

    def test_naive_now_is_taken_as_utc(self):
        self.assertEqual(ad.today(datetime(2026, 8, 12, 6, 29)), date(2026, 8, 11))

    def test_after_the_cut(self):
        # 2026-08-12 10:00 local
        self.assertEqual(ad.today(datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)), date(2026, 8, 12))


class OwnerNow(_Pinned):
    def test_returns_utc_offset_and_local_together(self):
        utc, offset, local = ad.owner_now(datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(utc, datetime(2026, 8, 12, 15, 0))
        self.assertEqual(offset, -300)
        self.assertEqual(local, datetime(2026, 8, 12, 10, 0))
        self.assertIsNone(utc.tzinfo)
        self.assertIsNone(local.tzinfo)


class ParseDay(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(ad.parse_day("2026-08-11"), date(2026, 8, 11))
        self.assertEqual(ad.parse_day(date(2026, 8, 11)), date(2026, 8, 11))
        self.assertEqual(ad.parse_day(datetime(2026, 8, 11, 22, 0)), date(2026, 8, 11))

    def test_junk_is_None(self):
        for value in (None, "", "11/08/2026", "2026-13-01", 20260811):
            self.assertIsNone(ad.parse_day(value), value)


if __name__ == "__main__":
    unittest.main()
