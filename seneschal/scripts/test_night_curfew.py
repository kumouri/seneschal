#!/usr/bin/env python3
"""Tests for the **night curfew** + the **staleness cutoff** — the two lateness gates in
``sentinel.check_reminders``.

The failure they exist to prevent: a nudge due 23:30 delivered at **04:24 local** — the tail of a queue,
not a new nudge. A live `/assistant` session correctly deferred everything from 18:30 to 22:08; an
unacked ⏰ row re-enqueues at every later slot, so 26 entries were waiting when the hold lifted; the
catch-up stagger drains at 15 min/nudge = 4/hour; 26 ÷ 4 = 6 h 30 m, and 22:08 + 6 h 30 m = 04:24. **The
04:24 delivery is arithmetically guaranteed the moment the hold lifts**, unless something in the fire
path asks whether a nudge is still worth sending. `NightIncidentReplay` below is that queue, replayed.

Stdlib ``unittest`` only.

**Every instant in this file is explicit UTC, and the owner zone and identity are PINNED** (a fixed
UTC-5, ``OWNER``; identity ``{}`` → the default 01:00-07:00 window and 05:00 day boundary). The gate is a
wall-clock reading in the OWNER's zone, so a test that asked the runner what time (or zone) it is would
be testing the runner. The DST cases use a real IANA zone and skip when the tz database is absent.

Run:  python -m unittest seneschal.scripts.test_night_curfew   (or)   python test_night_curfew.py
"""
import os
import sys
import shutil
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import clock  # noqa: E402
import identity_common  # noqa: E402
import sentinel as sn  # noqa: E402
import tz_common  # noqa: E402

# A summer UTC-5 owner zone: local = UTC - 5h.
OFFSET = timedelta(hours=-5)
OWNER = timezone(OFFSET)


def _utc(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _loc(y, mo, d, h, mi=0, s=0):
    """An owner wall-clock reading (UTC-5), as the UTC instant it names."""
    return _utc(y, mo, d, h, mi, s) - OFFSET


def _z(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _pin(testcase, zone=OWNER, identity=None):
    """Pin the owner zone and identity for one test (undone by addCleanup)."""
    ident = {} if identity is None else identity
    for p in (mock.patch.object(tz_common, "_zone", return_value=zone),
              mock.patch.object(clock, "load_identity", return_value=ident),
              mock.patch.object(identity_common, "load_identity", return_value=ident)):
        p.start()
        testcase.addCleanup(p.stop)


class _Pinned(unittest.TestCase):
    def setUp(self):
        _pin(self)


class CurfewPredicateUnit(_Pinned):
    """`in_night_curfew` as a pure predicate — no state dir, no delivery, no clock."""

    def test_leaked_evening_nudge_inside_the_window(self):
        # Due 22:00, firing 02:30 → the whole reason the gate exists.
        self.assertTrue(sn.in_night_curfew(_loc(2026, 8, 13, 2, 30), _loc(2026, 8, 12, 22, 0)))

    def test_nudge_genuinely_due_inside_the_window_is_untouched(self):
        # Due 01:30, firing 01:31. An owner may seed late slots on purpose; retiring them is not the gate.
        self.assertFalse(sn.in_night_curfew(_loc(2026, 8, 13, 1, 31), _loc(2026, 8, 13, 1, 30)))

    def test_before_the_window_starts(self):
        # Due 23:30, firing 23:45 — the 23:00-01:00 stretch the default window deliberately leaves open.
        self.assertFalse(sn.in_night_curfew(_loc(2026, 8, 12, 23, 45), _loc(2026, 8, 12, 23, 30)))

    def test_start_boundary_is_inclusive(self):
        due = _loc(2026, 8, 12, 22, 0)
        self.assertFalse(sn.in_night_curfew(_loc(2026, 8, 13, 0, 59, 59), due))
        self.assertTrue(sn.in_night_curfew(_loc(2026, 8, 13, 1, 0, 0), due))

    def test_end_boundary_is_exclusive(self):
        due = _loc(2026, 8, 12, 22, 0)
        self.assertTrue(sn.in_night_curfew(_loc(2026, 8, 13, 6, 59, 59), due))
        self.assertFalse(sn.in_night_curfew(_loc(2026, 8, 13, 7, 0, 0), due))

    def test_the_0424_delivery(self):
        # Due 23:30, would have been delivered 04:24.
        self.assertTrue(sn.in_night_curfew(_loc(2026, 8, 13, 4, 24), _loc(2026, 8, 12, 23, 30)))

    def test_no_grace_period(self):
        # Due 00:58, firing 01:03 — five minutes late across the boundary is still consumed. Deliberate.
        self.assertTrue(sn.in_night_curfew(_loc(2026, 8, 13, 1, 3), _loc(2026, 8, 13, 0, 58)))

    def test_daytime_is_never_curfewed(self):
        self.assertFalse(sn.in_night_curfew(_loc(2026, 8, 13, 14, 0), _loc(2026, 8, 13, 9, 0)))


class CurfewWindowIsOwnerConfig(unittest.TestCase):
    """The window is `owner.nightCurfew` in persona/identity.json — never a constant in code."""

    def test_default_window_is_0100_to_0700(self):
        _pin(self)
        self.assertEqual(sn.curfew_window(), (time(1, 0), time(7, 0)))
        self.assertEqual((sn.CURFEW_START_LOCAL, sn.CURFEW_END_LOCAL), (time(1, 0), time(7, 0)))

    def test_a_configured_window_is_honoured(self):
        _pin(self, identity={"owner": {"nightCurfew": {"start": "00:00", "end": "06:00"}}})
        self.assertEqual(sn.curfew_window(), (time(0, 0), time(6, 0)))
        due = _loc(2026, 8, 12, 22, 0)
        self.assertTrue(sn.in_night_curfew(_loc(2026, 8, 13, 0, 30), due))
        self.assertFalse(sn.in_night_curfew(_loc(2026, 8, 13, 6, 30), due))

    def test_an_unusable_value_falls_back_to_the_default(self):
        for bad in ({"start": "25:00", "end": "07:00"}, {"start": "1am"}, "01:00-07:00", None):
            _pin(self, identity={"owner": {"nightCurfew": bad}})
            self.assertEqual(sn.curfew_window(), (time(1, 0), time(7, 0)), bad)

    def test_a_window_that_wraps_midnight(self):
        # 23:00-07:00: past midnight the occurrence began at 23:00 the PREVIOUS local date.
        window = (time(23, 0), time(7, 0))
        _pin(self)
        self.assertTrue(sn.in_night_curfew(_loc(2026, 8, 12, 23, 30), _loc(2026, 8, 12, 22, 0), window))
        self.assertTrue(sn.in_night_curfew(_loc(2026, 8, 13, 3, 0), _loc(2026, 8, 12, 22, 0), window))
        # Due inside the occurrence (23:10) is untouched, before or after midnight.
        self.assertFalse(sn.in_night_curfew(_loc(2026, 8, 13, 3, 0), _loc(2026, 8, 12, 23, 10), window))
        # Outside the window entirely.
        self.assertFalse(sn.in_night_curfew(_loc(2026, 8, 13, 7, 0), _loc(2026, 8, 12, 22, 0), window))
        self.assertFalse(sn.in_night_curfew(_loc(2026, 8, 12, 22, 59), _loc(2026, 8, 12, 20, 0), window))

    def test_start_equal_to_end_disables_the_curfew(self):
        _pin(self)
        window = (time(3, 0), time(3, 0))
        self.assertFalse(sn.in_night_curfew(_loc(2026, 8, 13, 3, 0), _loc(2026, 8, 12, 22, 0), window))


def _resolvable_dst_zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Berlin")
    except Exception:  # noqa: BLE001 — no tz database (e.g. Windows without the tzdata venv)
        return None


_DST_ZONE = _resolvable_dst_zone()


@unittest.skipIf(_DST_ZONE is None, "no IANA tz database available for a DST zone")
class CurfewIsDstCorrect(unittest.TestCase):
    """The window is an owner WALL-CLOCK reading, not a frozen UTC offset.

    Each pair below is two UTC instants 24 h apart that straddle a DST transition, chosen so that the
    same `05:30Z` lands on OPPOSITE sides of the 07:00 curfew end. A frozen-offset implementation gets
    exactly one of each pair wrong, whichever offset it froze. (The zone is a real DST zone chosen only
    for its transitions: UTC+1 standard, UTC+2 summer.)
    """

    def setUp(self):
        _pin(self, zone=_DST_ZONE)

    def test_spring_forward_2026_03_29(self):
        # Summer time starts 2026-03-29 01:00Z (02:00 -> 03:00 local).
        due = _utc(2026, 3, 27, 20, 0)  # well before either window
        # 2026-03-28 05:30Z is 06:30 standard time -> INSIDE the window.
        self.assertTrue(sn.in_night_curfew(_utc(2026, 3, 28, 5, 30), due))
        # 2026-03-29 05:30Z is 07:30 summer time (clocks sprang forward) -> the window has ENDED.
        self.assertFalse(sn.in_night_curfew(_utc(2026, 3, 29, 5, 30), due))

    def test_fall_back_2026_10_25(self):
        # Summer time ends 2026-10-25 01:00Z (03:00 -> 02:00 local).
        due = _utc(2026, 10, 23, 20, 0)
        # 2026-10-24 05:30Z is 07:30 summer time -> the window has ENDED.
        self.assertFalse(sn.in_night_curfew(_utc(2026, 10, 24, 5, 30), due))
        # 2026-10-25 05:30Z is 06:30 standard time (clocks fell back) -> back INSIDE the window.
        self.assertTrue(sn.in_night_curfew(_utc(2026, 10, 25, 5, 30), due))


class LatenessUnit(_Pinned):
    """`entry_lateness_sec` measures time the owner could have ACTED on it — presence-hold time
    subtracted."""

    def test_raw_lateness_with_no_hold(self):
        now, due = _loc(2026, 8, 13, 14, 0), _loc(2026, 8, 13, 12, 0)
        self.assertEqual(sn.entry_lateness_sec({}, due, now), 7200)

    def test_presence_hold_is_netted_out(self):
        now, due = _loc(2026, 8, 13, 14, 0), _loc(2026, 8, 13, 9, 0)  # 5 h raw
        self.assertEqual(sn.entry_lateness_sec({"presence_held_sec": 4 * 3600}, due, now), 3600)

    def test_garbage_hold_reads_as_zero(self):
        now, due = _loc(2026, 8, 13, 14, 0), _loc(2026, 8, 13, 12, 0)
        for junk in ("lots", None, -5, {"a": 1}, [1]):
            self.assertEqual(sn.entry_lateness_sec({"presence_held_sec": junk}, due, now), 7200)


class _GateHarness(unittest.TestCase):
    """Shared fixture: a temp state dir with delivery mocked to succeed, zone + identity pinned."""

    def setUp(self):
        _pin(self)
        self.dir = tempfile.mkdtemp(prefix="curfew-test-")
        self.delivered = []
        self._orig = sn._deliver_reminder
        sn._deliver_reminder = lambda ch, raw, *a, **k: (
            self.delivered.append(raw) or ({"ok": True}, "telegram"))

    def tearDown(self):
        sn._deliver_reminder = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rows):
        sn.save_json(os.path.join(self.dir, "reminders.json"), rows)

    def rows(self):
        return {r["id"]: r for r in sn.load_json(os.path.join(self.dir, "reminders.json"), [])}

    def run_at(self, now):
        return sn.check_reminders(self.dir, now, fire=True, telegram_env="x")

    def kinds(self, signals):
        return {s["id"]: s["kind"] for s in signals if "id" in s}


class CurfewGateEndToEnd(_GateHarness):
    def test_leaked_evening_nudge_is_consumed(self):
        self.write([{"id": "shower", "text": "Shower.", "due_at": _z(_loc(2026, 8, 12, 22, 0)),
                     "channel": "telegram", "fired_at": None}])
        signals = self.run_at(_loc(2026, 8, 13, 2, 30))
        self.assertEqual(self.kinds(signals)["shower"], "reminder_suppressed_curfew")
        self.assertTrue(self.rows()["shower"]["suppressed_at"])   # consumed…
        self.assertIsNone(self.rows()["shower"]["fired_at"])      # …never delivered
        self.assertEqual(self.delivered, [])

    def test_2330_firing_2345_is_delivered(self):
        # The 23:00-01:00 stretch the default window deliberately leaves open.
        self.write([{"id": "shower", "text": "Shower.", "due_at": _z(_loc(2026, 8, 12, 23, 30)),
                     "channel": "telegram", "fired_at": None}])
        signals = self.run_at(_loc(2026, 8, 12, 23, 45))
        self.assertEqual(self.kinds(signals)["shower"], "reminder_fired")
        self.assertEqual(self.delivered, ["Shower."])

    def test_due_inside_the_window_is_delivered(self):
        self.write([{"id": "late-slot", "text": "The 1:30 check.", "due_at": _z(_loc(2026, 8, 13, 1, 30)),
                     "channel": "telegram", "fired_at": None}])
        signals = self.run_at(_loc(2026, 8, 13, 1, 31))
        self.assertEqual(self.kinds(signals)["late-slot"], "reminder_fired")

    def test_piercing_entries_are_exempt(self):
        # Critical and Super-Critical still come through at 3 AM — that is the whole point of the set.
        self.write([
            {"id": "critical", "text": "Critical item.", "due_at": _z(_loc(2026, 8, 12, 22, 0)),
             "channel": "telegram", "pierce_quiet": True, "fired_at": None},
            {"id": "ring", "text": "Check the stove.", "due_at": _z(_loc(2026, 8, 12, 22, 0)),
             "channel": "call", "fired_at": None},
            {"id": "esc", "text": "Wake up.", "due_at": _z(_loc(2026, 8, 12, 22, 0)),
             "channel": "telegram", "escalate": True, "fired_at": None},
        ])
        kinds = self.kinds(self.run_at(_loc(2026, 8, 13, 3, 0)))
        self.assertEqual(set(kinds.values()), {"reminder_fired"})

    def test_0700_does_not_curfew_a_staleness_exempt_entry(self):
        # The end boundary, end-to-end. Needs a staleness-exempt entry to be reachable at all: an item
        # due before 01:00 is >=6 h late by 07:00, so staleness would eat it and "delivered" would be
        # unassertable. A presence hold is the exemption that exists — 6 h of it nets this to 30 min.
        self.write([{"id": "held", "text": "Walk.", "due_at": _z(_loc(2026, 8, 13, 0, 30)),
                     "channel": "telegram", "fired_at": None, "presence_held_sec": 6 * 3600}])
        signals = self.run_at(_loc(2026, 8, 13, 7, 0, 0))
        self.assertEqual(self.kinds(signals)["held"], "reminder_fired")

    def test_0659_still_curfews(self):
        self.write([{"id": "held", "text": "Walk.", "due_at": _z(_loc(2026, 8, 13, 0, 30)),
                     "channel": "telegram", "fired_at": None, "presence_held_sec": 6 * 3600}])
        signals = self.run_at(_loc(2026, 8, 13, 6, 59, 59))
        self.assertEqual(self.kinds(signals)["held"], "reminder_suppressed_curfew")

    def test_quiet_wins_when_both_apply(self):
        # Quiet is the EXPLICIT request and must keep naming itself; curfew is the standing default
        # underneath it. Only one signal is emitted per entry, and it has to be the quiet one.
        now = _loc(2026, 8, 13, 2, 30)
        sn.set_quiet(self.dir, now + timedelta(hours=6))
        self.write([{"id": "shower", "text": "Shower.", "due_at": _z(_loc(2026, 8, 12, 22, 0)),
                     "channel": "telegram", "fired_at": None}])
        signals = self.run_at(now)
        self.assertEqual([s["kind"] for s in signals], ["reminder_suppressed_quiet"])


class StalenessGateEndToEnd(_GateHarness):
    NOW = _loc(2026, 8, 13, 14, 0)  # 14:00 local — daylight, so only the staleness gate can bite

    def _nudge(self, rid, late, **extra):
        return {"id": rid, "text": rid, "due_at": _z(self.NOW - late),
                "channel": "telegram", "fired_at": None, **extra}

    def test_just_inside_the_cutoff_fires(self):
        self.write([self._nudge("fresh", timedelta(hours=1, minutes=59))])
        self.assertEqual(self.kinds(self.run_at(self.NOW))["fresh"], "reminder_fired")

    def test_exactly_two_hours_fires(self):
        # The comparison is strictly `>`.
        self.write([self._nudge("edge", timedelta(hours=2))])
        self.assertEqual(self.kinds(self.run_at(self.NOW))["edge"], "reminder_fired")

    def test_past_the_cutoff_is_consumed(self):
        self.write([self._nudge("stale", timedelta(hours=2, minutes=1))])
        signals = self.run_at(self.NOW)
        self.assertEqual(self.kinds(signals)["stale"], "reminder_suppressed_stale")
        self.assertTrue(self.rows()["stale"]["suppressed_at"])
        self.assertIsNone(self.rows()["stale"]["fired_at"])
        self.assertEqual(self.delivered, [])
        self.assertEqual([s["late_sec"] for s in signals if s["kind"] == "reminder_suppressed_stale"],
                         [2 * 3600 + 60])

    def test_a_stale_suppression_is_a_durable_ledger_row(self):
        import reminder_suppressions
        self.write([self._nudge("stale", timedelta(hours=3))])
        self.run_at(self.NOW)
        rows = reminder_suppressions.tail(self.dir, 10)
        self.assertEqual([r["id"] for r in rows], ["stale"])

    def test_piercing_entries_are_exempt(self):
        self.write([
            self._nudge("critical", timedelta(hours=5), pierce_quiet=True),
            self._nudge("ring", timedelta(hours=5), channel="call"),
        ])
        self.assertEqual(set(self.kinds(self.run_at(self.NOW)).values()), {"reminder_fired"})

    def test_presence_held_entry_five_hours_late_still_fires(self):
        # A `require_place` nudge that waited 4 h 30 m to get home: 5 h raw, 30 min actionable.
        self.write([self._nudge("walk", timedelta(hours=5), require_place="home",
                                presence_held_sec=int(4.5 * 3600))])
        self.assertEqual(self.kinds(self.run_at(self.NOW))["walk"], "reminder_fired")

    def test_driving_held_entry_five_hours_late_still_fires(self):
        # The hole a `require_place` check alone would have left: the DRIVING rule carries no per-entry
        # marker, so this entry is indistinguishable from an ignored one except by its accumulator.
        self.write([self._nudge("drive", timedelta(hours=5), presence_held_sec=int(4.5 * 3600))])
        self.assertEqual(self.kinds(self.run_at(self.NOW))["drive"], "reminder_fired")

    def test_stale_beats_stagger(self):
        # Order matters: a stale entry must be CONSUMED, not merely held behind the drip — otherwise it
        # keeps its place in the queue and lands later still.
        sn.save_json(os.path.join(self.dir, "nudge-stagger.json"),
                     {"last_nonpiercing_fire": _z(self.NOW - timedelta(minutes=1))})
        self.write([self._nudge("stale", timedelta(hours=3))])
        self.assertEqual(self.kinds(self.run_at(self.NOW))["stale"], "reminder_suppressed_stale")


class PresenceHoldAccumulator(_GateHarness):
    """The presence gate stamps a bookkeeping accumulator. This AMENDS its "stamp NOTHING" defer
    contract, deliberately: the contract exists so a deferred entry stays PENDING rather than being
    consumed, and a counter does not consume it."""

    def ctx(self, now, **kw):
        base = {"at_place": None, "activity": None, "asleep": False, "since": {},
                "updated_at": (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%SZ")}
        base.update(kw)
        sn.save_json(os.path.join(self.dir, "presence-context.json"), base)

    def test_defer_stamps_since_and_leaves_the_entry_pending(self):
        t0 = _loc(2026, 8, 13, 12, 0)
        self.ctx(t0, activity="in_vehicle")
        self.write([{"id": "walk", "text": "Walk.", "due_at": _z(t0), "channel": "telegram",
                     "fired_at": None}])
        self.assertEqual(self.kinds(self.run_at(t0))["walk"], "reminder_deferred_presence")
        row = self.rows()["walk"]
        self.assertTrue(row["presence_deferred_since"])
        self.assertIsNone(row["fired_at"])            # still pending — the contract that matters
        self.assertNotIn("suppressed_at", row)
        self.assertNotIn("acked_at", row)

    def test_repeated_defers_do_not_restart_the_clock(self):
        t0 = _loc(2026, 8, 13, 12, 0)
        self.ctx(t0, activity="in_vehicle")
        self.write([{"id": "walk", "text": "Walk.", "due_at": _z(t0), "channel": "telegram",
                     "fired_at": None}])
        self.run_at(t0)
        stamped = self.rows()["walk"]["presence_deferred_since"]
        self.ctx(t0 + timedelta(hours=1), activity="in_vehicle")
        self.run_at(t0 + timedelta(hours=1))
        self.assertEqual(self.rows()["walk"]["presence_deferred_since"], stamped)

    def test_release_folds_the_segment_and_exempts_the_lateness(self):
        # Held by the driving rule for 3 h — longer than MAX_LATENESS_SEC — then released. NO DROPS.
        t0 = _loc(2026, 8, 13, 10, 0)
        release = t0 + timedelta(hours=3)
        self.ctx(t0, activity="in_vehicle")
        self.write([{"id": "walk", "text": "Walk.", "due_at": _z(t0), "channel": "telegram",
                     "fired_at": None}])
        self.run_at(t0)
        self.ctx(release, activity="still")
        signals = self.run_at(release)
        self.assertEqual(self.kinds(signals)["walk"], "reminder_fired")
        self.assertEqual(self.delivered, ["Walk."])

    def test_a_second_hold_accumulates_rather_than_replacing(self):
        # Two separate holds (drive, park, drive again) must SUM. Delivery is stubbed to fail so the
        # entry stays pending across all four passes and can be held a second time.
        sn._deliver_reminder = lambda *a, **k: ({"ok": False, "error": "stubbed"}, "telegram")
        t0 = _loc(2026, 8, 13, 8, 0)
        self.write([{"id": "walk", "text": "Walk.", "due_at": _z(t0), "channel": "telegram",
                     "fired_at": None}])
        self.ctx(t0, activity="in_vehicle")
        self.run_at(t0)                                                    # hold 1 opens
        self.ctx(t0 + timedelta(hours=2), activity="still")
        self.run_at(t0 + timedelta(hours=2))                               # hold 1 closes: +2 h
        self.assertEqual(self.rows()["walk"]["presence_held_sec"], 2 * 3600)
        self.assertNotIn("presence_deferred_since", self.rows()["walk"])

        self.ctx(t0 + timedelta(hours=3), activity="in_vehicle")
        self.run_at(t0 + timedelta(hours=3))                               # hold 2 opens
        self.ctx(t0 + timedelta(hours=4), activity="still")
        self.run_at(t0 + timedelta(hours=4))                               # hold 2 closes: +1 h
        self.assertEqual(self.rows()["walk"]["presence_held_sec"], 3 * 3600)
        # 4 h raw lateness minus 3 h held = 1 h actionable, so the staleness gate must NOT have eaten it.
        self.assertIsNone(self.rows()["walk"]["fired_at"])                 # the stub refused the send…
        self.assertNotIn("suppressed_at", self.rows()["walk"])             # …but nothing consumed it

    def test_garbled_since_stamp_costs_the_exemption_not_the_nudge(self):
        t0 = _loc(2026, 8, 13, 12, 0)
        self.write([{"id": "walk", "text": "Walk.", "due_at": _z(t0), "channel": "telegram",
                     "fired_at": None, "presence_deferred_since": "not-a-date"}])
        self.assertEqual(self.kinds(self.run_at(t0))["walk"], "reminder_fired")
        self.assertEqual(self.rows()["walk"]["presence_held_sec"], 0.0)


class NightIncidentReplay(_GateHarness):
    """**The regression test: replay the 04:24 night and assert it never happens.**

    26 non-piercing entries, all released at 22:08 when a live-session hold lifted, drained through the
    real 15-minute catch-up stagger. Without the lateness gates the queue takes 6 h 30 m to drip and the
    tail lands at 04:24. The assertion is not "fewer nudges" — it is that **nothing lands after 01:00**.
    """

    # An unacked ⏰ row re-enqueues at EVERY later slot, which is why entries arrive faster than 4/hour
    # can clear them. Six rows, 26 entries in total.
    SLOTS = {
        "shower": ["19:00", "20:00", "20:30", "21:30", "22:00", "23:00", "23:30"],
        "water-plants": ["19:30", "21:00", "22:30"],
        "tidy-front": ["19:15", "20:45", "22:15"],
        "tidy-back": ["19:45", "21:15", "22:45"],
        "dishes": ["19:20", "20:20", "21:20", "22:20", "23:20"],
        "laundry": ["19:50", "20:50", "21:50", "22:50", "23:45"],
    }
    RELEASE = _loc(2026, 8, 12, 22, 8)   # 22:08 local, when the live-session hold lifted
    CURFEW_OPENS = _loc(2026, 8, 13, 1, 0)  # the instant the window opens, that same night
    END = _loc(2026, 8, 13, 8, 0)

    def _queue(self):
        rows = []
        for slug, times in self.SLOTS.items():
            for hhmm in times:
                h, m = (int(x) for x in hhmm.split(":"))
                rows.append({"id": f"rmd-2026-08-12-{slug}-{h:02d}{m:02d}",
                             "text": f"{slug} ({hhmm}).",
                             "due_at": _z(_loc(2026, 8, 12, h, m)),
                             "channel": "telegram", "fired_at": None, "local_due": hhmm})
        return rows

    def test_the_queue_is_26(self):
        self.assertEqual(len(self._queue()), 26)

    def _drain(self):
        """Run the real fire path every 5 minutes from the release to 08:00. Returns
        ``(fires, suppressions)``, each mapping entry id -> the UTC instant it resolved at.

        Compared as absolute instants, never as times-of-day: this run crosses midnight, so a
        `.time()` comparison would read 22:08 as "after 01:00" and quietly invert the assertion.
        Tick granularity is 5 min — the daemon's real beat is ~5 s, but the 15-minute stagger makes
        anything finer behave identically, and 5 min lands exactly on 01:00 and 07:00."""
        fires, suppressions = {}, {}
        t = self.RELEASE
        while t <= self.END:
            for s in self.run_at(t):
                if s["kind"] == "reminder_fired":
                    fires[s["id"]] = t
                elif s["kind"].startswith("reminder_suppressed_"):
                    suppressions[s["id"]] = (s["kind"], t)
            t += timedelta(minutes=5)
        return fires, suppressions

    def test_nothing_lands_after_0100(self):
        self.write(self._queue())
        fires, _ = self._drain()

        self.assertTrue(fires, "the drip must still deliver the fresh end of the backlog")
        latest_id, latest = max(fires.items(), key=lambda kv: kv[1])
        # THE ASSERTION: no delivery once the window opens. 04:24 is unreachable, and so is 01:00 itself.
        self.assertLess(latest, self.CURFEW_OPENS,
                        f"{latest_id} landed at {latest + OFFSET:%H:%M} local — the incident is back")

        # And nothing is left pending: every entry either fired or was consumed by a named gate.
        self.assertEqual([r["id"] for r in self.rows().values()
                          if not r.get("fired_at") and not r.get("suppressed_at")], [])

    def test_the_2330_shower_nudge_never_goes_out(self):
        # rmd-2026-08-12-shower-2330 is the one that would land at 04:24. It dies at 01:03 to the
        # CURFEW, when it is only 1 h 33 m past due — under MAX_LATENESS_SEC, so the staleness cutoff
        # alone would NOT have caught it: this single entry is why both gates exist, curfew first.
        self.write(self._queue())
        fires, suppressions = self._drain()
        self.assertNotIn("rmd-2026-08-12-shower-2330", fires)
        kind, when = suppressions["rmd-2026-08-12-shower-2330"]
        self.assertEqual(kind, "reminder_suppressed_curfew")
        self.assertEqual((when + OFFSET).strftime("%H:%M"), "01:03")

    def test_each_gate_takes_the_half_of_the_backlog_it_is_for(self):
        # The division of labour. Staleness nets out stagger wait (`entry_lateness_sec` subtracts
        # `_stagger_held_sec`), so it only ever catches what was ALREADY >2h late the FIRST tick it's
        # checked (here: the release instant, 22:08) — nothing goes newly stale from congestion alone —
        # and the curfew is the backstop for everything that merely waited, including 23:00-01:00.
        self.write(self._queue())
        _, suppressions = self._drain()
        stale = {k: t for k, (kind, t) in suppressions.items() if kind == "reminder_suppressed_stale"}
        curfewed = {k: t for k, (kind, t) in suppressions.items() if kind == "reminder_suppressed_curfew"}

        self.assertTrue(stale and curfewed, "both gates must be doing work in this replay")
        # Only the two lateness gates ever fire here: no acks, no quiet window in this fixture.
        self.assertLessEqual({kind for kind, _ in suppressions.values()},
                             {"reminder_suppressed_stale", "reminder_suppressed_curfew"})
        for rid, t in stale.items():
            self.assertLess(t, self.CURFEW_OPENS, rid)
        self.assertTrue(all(t == self.RELEASE for t in stale.values()),
                        "staleness only catches what was ALREADY stale the instant the backlog was "
                        "first checked — never something that went stale mid-drip")
        # The curfew only ever RESOLVES entries once the window opens, and only entries due before it.
        for rid, t in curfewed.items():
            self.assertGreaterEqual(t, self.CURFEW_OPENS, rid)
            self.assertLess(sn.parse_iso(self.rows()[rid]["due_at"]), self.CURFEW_OPENS, rid)
        self.assertTrue(any(self.CURFEW_OPENS - timedelta(hours=2)
                            <= sn.parse_iso(self.rows()[rid]["due_at"]) < self.CURFEW_OPENS
                            for rid in curfewed),
                        "an entry DUE in 23:00-01:00 must still be caught — by the curfew, not staleness")

    def test_a_piercing_entry_in_the_same_backlog_still_gets_through(self):
        # NO DROPS for the pierce set, even in this queue.
        rows = self._queue()
        rows.append({"id": "rmd-2026-08-12-critical-2200", "text": "Critical item.",
                     "due_at": _z(_loc(2026, 8, 12, 22, 0)), "channel": "telegram",
                     "pierce_quiet": True, "fired_at": None})
        self.write(rows)
        self.run_at(self.RELEASE)
        self.assertTrue(self.rows()["rmd-2026-08-12-critical-2200"]["fired_at"])


if __name__ == "__main__":
    unittest.main()
