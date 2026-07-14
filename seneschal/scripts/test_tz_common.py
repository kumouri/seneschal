#!/usr/bin/env python3
"""Tests for tz_common — the owner-timezone fallback ladder + the deterministic offset specs.

Must pass BOTH with tzdata installed (the uv venv) and without (a bare Windows interpreter where
``ZoneInfo`` resolves nothing), so anything that needs a real IANA zone either mocks
``zoneinfo.ZoneInfo`` (the same seam ``test_presence_grounding`` uses) or is skip-guarded on
actual resolvability. The machine-local fallback path itself is exercised unconditionally with
an unresolvable key.

Run:  python -m unittest seneschal.scripts.test_tz_common   (or)   python test_tz_common.py
"""
import io
import os
import re
import sys
import unittest
import unittest.mock
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone, tzinfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import tz_common as tzc  # noqa: E402

WINTER = 1770159199  # 2026-02-03T22:53:19Z — US-Central is CST (−360)
SUMMER = 1783000000  # 2026-07-02T08:26:40Z — US-Central is CDT (−300)


def _adelaide_resolves() -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Australia/Adelaide")
        return True
    except Exception:  # noqa: BLE001
        return False


def _identity(tz_name):
    return {"owner": {"timezone": tz_name}}


class LadderStep1NullConfig(unittest.TestCase):
    """No timezone configured → machine-local, silently (the normal fresh-install state)."""

    def setUp(self):
        tzc._reset()
        self.addCleanup(tzc._reset)

    def test_machine_local_and_silent(self):
        err = io.StringIO()
        with unittest.mock.patch.object(tzc, "load_identity", return_value={"owner": {}}), \
                redirect_stderr(err):
            now = tzc.local_now()
            machine = datetime.now().astimezone()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.utcoffset(), machine.utcoffset())
        self.assertEqual(err.getvalue(), "")  # null config is normal — no warning

    def test_offset_minutes_matches_machine(self):
        with unittest.mock.patch.object(tzc, "load_identity", return_value={}):
            machine_min = int(datetime.now().astimezone().utcoffset().total_seconds() // 60)
            self.assertEqual(tzc.offset_minutes(datetime.now(timezone.utc)), machine_min)


class LadderStep2Resolvable(unittest.TestCase):
    """Configured AND resolvable → the configured zone wins over the machine clock."""

    def setUp(self):
        tzc._reset()
        self.addCleanup(tzc._reset)

    def test_mocked_half_hour_zone(self):
        # A +9:30 zone (Adelaide standard time) via the ZoneInfo seam — runs without tzdata.
        fixed = timezone(timedelta(hours=9, minutes=30))
        with unittest.mock.patch.object(tzc, "load_identity",
                                        return_value=_identity("Australia/Adelaide")), \
                unittest.mock.patch("zoneinfo.ZoneInfo", lambda key: fixed):
            self.assertEqual(tzc.offset_minutes(WINTER), 570)  # half-hour offsets survive intact
            self.assertEqual(tzc.local_now().utcoffset(), timedelta(hours=9, minutes=30))

    @unittest.skipUnless(_adelaide_resolves(), "needs tzdata (run inside the uv venv)")
    def test_real_adelaide_dst_pair(self):
        # Southern hemisphere: DST in January (+10:30), standard in July (+9:30).
        with unittest.mock.patch.object(tzc, "load_identity",
                                        return_value=_identity("Australia/Adelaide")):
            jan = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
            jul = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
            self.assertEqual(tzc.offset_minutes(jan), 630)
            tzc._reset()  # offset_minutes re-resolves per cache; keep both reads honest
        with unittest.mock.patch.object(tzc, "load_identity",
                                        return_value=_identity("Australia/Adelaide")):
            self.assertEqual(tzc.offset_minutes(jul), 570)


class LadderStep3Unresolvable(unittest.TestCase):
    """Configured but unresolvable → machine-local + exactly ONE stderr warning per process."""

    def setUp(self):
        tzc._reset()
        self.addCleanup(tzc._reset)

    def test_falls_back_and_warns_once(self):
        err = io.StringIO()
        with unittest.mock.patch.object(tzc, "load_identity",
                                        return_value=_identity("Not/AZone")), \
                redirect_stderr(err):
            first = tzc.local_now()
            second = tzc.local_today()  # second call — must not warn again
        self.assertEqual(first.utcoffset(), datetime.now().astimezone().utcoffset())
        self.assertRegex(second, r"^\d{4}-\d{2}-\d{2}$")
        warnings = [ln for ln in err.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("Not/AZone", warnings[0])
        self.assertIn("machine-local", warnings[0])


class StampAndConversions(unittest.TestCase):
    def setUp(self):
        tzc._reset()
        self.addCleanup(tzc._reset)

    def test_local_stamp_format_matches_presence_contract(self):
        # 'Friday 2026-07-03 12:30 <zone name>' — weekday word, ISO date, HH:MM. (%Z text varies
        # by platform/zone source, so only its presence-or-absence is left free.)
        stamp = tzc.local_stamp()
        self.assertRegex(stamp, r"^[A-Z][a-z]+day \d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_to_local_treats_naive_as_utc(self):
        fixed = timezone(timedelta(hours=-6))
        with unittest.mock.patch.object(tzc, "load_identity",
                                        return_value=_identity("America/Chicago")), \
                unittest.mock.patch("zoneinfo.ZoneInfo", lambda key: fixed):
            local = tzc.to_local(datetime(2026, 2, 3, 22, 53, 19))  # naive == UTC
            self.assertEqual((local.hour, local.minute), (16, 53))
            self.assertEqual(local.utcoffset(), timedelta(hours=-6))

    def test_owner_tz_is_a_tzinfo(self):
        self.assertIsNotNone(tzc.owner_tz())
        self.assertIsNotNone(datetime.now(tzc.owner_tz()).utcoffset())


class CentralFormula(unittest.TestCase):
    """The retained US-Central rule pins both DST transitions — with or without tzdata, the
    IANA path and the hand-rolled fallback must agree here (they do for 2007+)."""

    def test_winter_summer(self):
        self.assertEqual(tzc.us_central_offset_minutes(datetime(2025, 1, 24, 12)), -360)
        self.assertEqual(tzc.us_central_offset_minutes(datetime(2025, 7, 24, 12)), -300)

    def test_spring_forward_boundary(self):
        # DST begins 02:00 local on the 2nd Sunday of March 2026 = Mar 8 = 08:00 UTC.
        self.assertEqual(tzc.us_central_offset_minutes(datetime(2026, 3, 8, 7, 59)), -360)
        self.assertEqual(tzc.us_central_offset_minutes(datetime(2026, 3, 8, 8, 0)), -300)

    def test_fall_back_boundary(self):
        # DST ends 02:00 local on the 1st Sunday of November 2025 = Nov 2. 02:00 there is CDT
        # (UTC−5), so the transition instant is 07:00 UTC — NOT 08:00, the legacy formulas' bug
        # (spring-forward's 02:00 is CST, hence its 08:00 UTC). IANA and the fixed fallback agree.
        self.assertEqual(tzc.us_central_offset_minutes(datetime(2025, 11, 2, 6, 59)), -300)
        self.assertEqual(tzc.us_central_offset_minutes(datetime(2025, 11, 2, 7, 0)), -360)

    def test_aware_input_accepted(self):
        aware = datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(tzc.us_central_offset_minutes(aware), -300)


class ResolveOffsetSpecs(unittest.TestCase):
    """The archive_common CLI spec strings survive verbatim."""

    def setUp(self):
        tzc._reset()
        self.addCleanup(tzc._reset)

    def test_auto_and_empty_follow_owner_tz(self):
        for spec in (None, "", "auto"):
            self.assertEqual(tzc.resolve_offset(WINTER, spec), tzc.offset_minutes(WINTER))

    def test_explicit_chicago_is_deterministic_everywhere(self):
        self.assertEqual(tzc.resolve_offset(WINTER, "chicago"), -360)
        self.assertEqual(tzc.resolve_offset(WINTER, "America/Chicago"), -360)
        self.assertEqual(tzc.resolve_offset(SUMMER, "America/Chicago"), -300)

    def test_utc_any_case(self):
        self.assertEqual(tzc.resolve_offset(WINTER, "UTC"), 0)
        self.assertEqual(tzc.resolve_offset(WINTER, "utc"), 0)

    def test_fixed_minutes(self):
        self.assertEqual(tzc.resolve_offset(0, "-120"), -120)
        self.assertEqual(tzc.resolve_offset(0, "90"), 90)

    def test_unknown_spec_reads_as_utc(self):
        self.assertEqual(tzc.resolve_offset(WINTER, "definitely not a zone"), 0)

    def test_offset_minutes_across_us_central_dst_boundary(self):
        # offset_minutes itself (the owner path) crosses a DST edge correctly when the owner
        # zone is DST-aware — proven via the Chicago spec against epoch instants that straddle
        # the 2026 spring-forward (08:00 UTC on Mar 8).
        before = int(datetime(2026, 3, 8, 7, 59, tzinfo=timezone.utc).timestamp())
        after = int(datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(tzc.resolve_offset(before, "America/Chicago"), -360)
        self.assertEqual(tzc.resolve_offset(after, "America/Chicago"), -300)


class OffsetMinutesOwnerDst(unittest.TestCase):
    """offset_minutes (the owner-tz path, not a spec string) across a DST boundary."""

    def setUp(self):
        tzc._reset()
        self.addCleanup(tzc._reset)

    def test_dst_edge_with_dst_aware_zone(self):
        # A minimal DST-aware tzinfo standing in for a resolvable IANA zone: −360 before the
        # 2026-03-08 08:00Z edge, −300 after — so no tzdata is needed for this pin. fromutc
        # snapshots the per-instant offset into the returned datetime's tzinfo (dt's value is
        # naive-UTC there), which sidesteps the wall-clock-vs-UTC ambiguity of utcoffset(dt).
        edge_naive = datetime(2026, 3, 8, 8, 0)  # the 2026 US spring-forward instant, UTC

        class FakeCentral(tzinfo):
            def __init__(self, fixed=None):
                self._fixed = fixed

            def utcoffset(self, dt):
                if self._fixed is not None:
                    return self._fixed
                return timedelta(minutes=-300 if dt.replace(tzinfo=None) >= edge_naive else -360)

            def dst(self, dt):
                return timedelta(0)

            def tzname(self, dt):
                return "FakeCentral"

            def fromutc(self, dt):
                off = self.utcoffset(dt)
                return (dt + off).replace(tzinfo=FakeCentral(off))

        with unittest.mock.patch.object(tzc, "load_identity",
                                        return_value=_identity("America/Chicago")), \
                unittest.mock.patch("zoneinfo.ZoneInfo", lambda key: FakeCentral()):
            edge_epoch = int(edge_naive.replace(tzinfo=timezone.utc).timestamp())
            self.assertEqual(tzc.offset_minutes(edge_epoch - 60), -360)
            self.assertEqual(tzc.offset_minutes(edge_epoch), -300)

    @unittest.skipUnless(_adelaide_resolves(), "needs tzdata (run inside the uv venv)")
    def test_real_zone_dst_edge(self):
        with unittest.mock.patch.object(tzc, "load_identity",
                                        return_value=_identity("America/Chicago")):
            before = int(datetime(2026, 3, 8, 7, 59, tzinfo=timezone.utc).timestamp())
            after = int(datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc).timestamp())
            self.assertEqual(tzc.offset_minutes(before), -360)
            self.assertEqual(tzc.offset_minutes(after), -300)


if __name__ == "__main__":
    unittest.main()
