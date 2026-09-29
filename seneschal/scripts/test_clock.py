#!/usr/bin/env python3
"""Tests for `clock.py` — the owner-wall-clock facade over `tz_common`: `to_local`'s naive owner
reading, a DST edge in both directions, `local_today`'s configured cut hour, and `parse_iso`'s
None/garbage/Z/offset contract.

Nothing here depends on the runner's own timezone or on tzdata being installed: every test injects
its instant, patches the zone at `tz_common._zone` (the one seam every owner-tz helper reads), and
patches the identity `clock` reads its day boundary from. A DST edge is modelled with a small
two-offset tzinfo rather than a real IANA zone; one real-zone test is skip-guarded on tzdata.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone, tzinfo
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import clock  # noqa: E402
import tz_common  # noqa: E402

MINUS_6 = timezone(timedelta(hours=-6))
PLUS_930 = timezone(timedelta(hours=9, minutes=30))


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class EdgeZone(tzinfo):
    """`before` until the UTC instant `edge`, `after` from it on — a DST transition, deterministic."""

    def __init__(self, edge_utc: datetime, before: timedelta, after: timedelta, fixed=None):
        self._edge = edge_utc.replace(tzinfo=None)
        self._before, self._after, self._fixed = before, after, fixed

    def utcoffset(self, dt):
        if self._fixed is not None:
            return self._fixed
        return self._after if (dt.replace(tzinfo=None) - self._before) >= self._edge else self._before

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "EdgeZone"

    def fromutc(self, dt):
        naive = dt.replace(tzinfo=None)
        off = self._after if naive >= self._edge else self._before
        return (naive + off).replace(tzinfo=EdgeZone(self._edge, self._before, self._after, off))


class _Pinned(unittest.TestCase):
    """Pin the zone and the identity for every test in a subclass."""

    zone: tzinfo = MINUS_6
    identity: dict = {}

    def setUp(self):
        z = mock.patch.object(tz_common, "_zone", return_value=self.zone)
        i = mock.patch.object(clock, "load_identity", return_value=self.identity)
        z.start()
        i.start()
        self.addCleanup(z.stop)
        self.addCleanup(i.stop)


class ToLocal(_Pinned):
    def test_returns_the_owner_wall_clock_naive(self):
        got = clock.to_local(_utc(2026, 1, 15, 10, 59))
        self.assertEqual(got, datetime(2026, 1, 15, 4, 59))
        self.assertIsNone(got.tzinfo)

    def test_naive_instant_is_assumed_utc(self):
        self.assertEqual(clock.to_local(datetime(2026, 1, 15, 10, 59)), datetime(2026, 1, 15, 4, 59))

    def test_a_half_hour_zone_survives_intact(self):
        with mock.patch.object(tz_common, "_zone", return_value=PLUS_930):
            self.assertEqual(clock.to_local(_utc(2026, 1, 15, 10, 0)), datetime(2026, 1, 15, 19, 30))

    def test_now_local_takes_an_injected_instant(self):
        self.assertEqual(clock.now_local(_utc(2026, 7, 15, 12, 0)), datetime(2026, 7, 15, 6, 0))

    def test_unconfigured_zone_falls_back_to_the_machine_clock(self):
        with mock.patch.object(tz_common, "_zone", return_value=None):
            instant = _utc(2026, 7, 15, 12, 0)
            self.assertEqual(clock.to_local(instant), instant.astimezone().replace(tzinfo=None))


class DstEdge(_Pinned):
    """A spring-forward edge at 08:00 UTC (−6 → −5) and a fall-back edge at 07:00 UTC (−5 → −6)."""

    def test_spring_forward(self):
        edge = _utc(2026, 3, 8, 8, 0)
        z = EdgeZone(edge, timedelta(hours=-6), timedelta(hours=-5))
        with mock.patch.object(tz_common, "_zone", return_value=z):
            self.assertEqual(clock.to_local(edge - timedelta(minutes=1)), datetime(2026, 3, 8, 1, 59))
            self.assertEqual(clock.to_local(edge), datetime(2026, 3, 8, 3, 0))

    def test_fall_back(self):
        edge = _utc(2026, 11, 1, 7, 0)
        z = EdgeZone(edge, timedelta(hours=-5), timedelta(hours=-6))
        with mock.patch.object(tz_common, "_zone", return_value=z):
            self.assertEqual(clock.to_local(edge - timedelta(minutes=1)), datetime(2026, 11, 1, 1, 59))
            self.assertEqual(clock.to_local(edge), datetime(2026, 11, 1, 1, 0))


def _berlin_resolves() -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Europe/Berlin")
        return True
    except Exception:  # noqa: BLE001
        return False


class RealZone(unittest.TestCase):
    @unittest.skipUnless(_berlin_resolves(), "needs tzdata (run inside the uv venv)")
    def test_a_real_iana_zone_crosses_its_dst_edge(self):
        from zoneinfo import ZoneInfo
        with mock.patch.object(tz_common, "_zone", return_value=ZoneInfo("Europe/Berlin")):
            # EU spring-forward 2026: 2026-03-29 01:00 UTC (+1 → +2).
            self.assertEqual(clock.to_local(_utc(2026, 3, 29, 0, 59)), datetime(2026, 3, 29, 1, 59))
            self.assertEqual(clock.to_local(_utc(2026, 3, 29, 1, 0)), datetime(2026, 3, 29, 3, 0))


class LocalToday(_Pinned):
    def test_just_before_the_default_cut_is_the_prior_day(self):
        # 04:59 local (UTC-6) -> 10:59 UTC
        self.assertEqual(clock.local_today(now=_utc(2026, 1, 15, 10, 59)), date(2026, 1, 14))

    def test_at_the_default_cut_is_the_current_day(self):
        self.assertEqual(clock.local_today(now=_utc(2026, 1, 15, 11, 0)), date(2026, 1, 15))

    def test_an_explicit_cut_hour_moves_the_boundary(self):
        # cut_hour=0: only exact midnight moves the day, so 04:59 local belongs to TODAY.
        self.assertEqual(clock.local_today(cut_hour=0, now=_utc(2026, 1, 15, 10, 59)),
                         date(2026, 1, 15))

    def test_the_configured_boundary_is_honoured(self):
        with mock.patch.object(clock, "load_identity", return_value={"owner": {"dayBoundaryHour": 3}}):
            self.assertEqual(clock.configured_cut_hour(), 3)
            # 03:30 local is past a 03:00 cut -> today; it would be yesterday under the default 5.
            self.assertEqual(clock.local_today(now=_utc(2026, 1, 15, 9, 30)), date(2026, 1, 15))

    def test_an_unusable_boundary_falls_back_to_the_default(self):
        for bad in (None, 13, -1, "soon", True, 4.5):
            with mock.patch.object(clock, "load_identity",
                                   return_value={"owner": {"dayBoundaryHour": bad}}):
                self.assertEqual(clock.configured_cut_hour(), 5, bad)

    def test_default_cut_hour_matches_the_documented_default(self):
        self.assertEqual(clock.DEFAULT_CUT_HOUR, 5)
        self.assertEqual(clock.configured_cut_hour(), 5)  # nothing configured


class ParseIso(unittest.TestCase):
    def test_none_is_none(self):
        self.assertIsNone(clock.parse_iso(None))

    def test_none_strict_raises(self):
        with self.assertRaises(ValueError):
            clock.parse_iso(None, strict=True)

    def test_empty_string_is_none(self):
        self.assertIsNone(clock.parse_iso(""))

    def test_garbage_is_none(self):
        self.assertIsNone(clock.parse_iso("not a date"))

    def test_garbage_strict_raises(self):
        with self.assertRaises(ValueError):
            clock.parse_iso("not a date", strict=True)

    def test_a_non_string_is_none(self):
        self.assertIsNone(clock.parse_iso(12345))

    def test_a_non_string_strict_raises(self):
        with self.assertRaises(ValueError):
            clock.parse_iso(12345, strict=True)

    def test_trailing_z(self):
        self.assertEqual(clock.parse_iso("2026-01-15T10:59:00Z"), _utc(2026, 1, 15, 10, 59, 0))

    def test_explicit_utc_offset(self):
        self.assertEqual(clock.parse_iso("2026-01-15T10:59:00+00:00"), _utc(2026, 1, 15, 10, 59, 0))

    def test_a_non_utc_offset_converts_to_utc(self):
        self.assertEqual(clock.parse_iso("2026-01-15T04:59:00-06:00"), _utc(2026, 1, 15, 10, 59, 0))

    def test_naive_is_assumed_utc(self):
        self.assertEqual(clock.parse_iso("2026-01-15T10:59:00"), _utc(2026, 1, 15, 10, 59, 0))

    def test_result_is_always_aware(self):
        self.assertIsNotNone(clock.parse_iso("2026-01-15T10:59:00Z").tzinfo)


class NowUtc(unittest.TestCase):
    def test_is_naive_and_close_to_the_real_clock(self):
        got = clock.now_utc()
        self.assertIsNone(got.tzinfo)
        real = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertLess(abs((real - got).total_seconds()), 5)


if __name__ == "__main__":
    unittest.main()
