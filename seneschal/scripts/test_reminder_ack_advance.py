#!/usr/bin/env python3
"""Tests for the **ack-advance** of the catch-up stagger (``sentinel._ack_advances_stagger``).

An ack advances the stagger after a 2-min debounce (``sentinel.ACK_ADVANCE_DEBOUNCE_SEC``), so the next
pending nudge follows the owner's 👍 instead of waiting out the 15-min window. ``reminders_dequeue``
(where every ack route ends) stamps ``last_ack_at`` into ``nudge-stagger.json`` via
``reminders_acks.record_ack_instant``; the stagger gate — and ONLY the stagger gate — reads it.

Stdlib ``unittest`` only. Delivery is mocked to succeed; every instant is explicit UTC and the owner zone
+ identity are pinned (a fixed UTC-5), so no case depends on the runner's clock or zone.

Run:  python -m unittest seneschal.scripts.test_reminder_ack_advance
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clock  # noqa: E402
import identity_common  # noqa: E402
import reminders_acks as ra  # noqa: E402
import reminders_dequeue  # noqa: E402
import sentinel  # noqa: E402
import tz_common  # noqa: E402


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class _StaggerHarness(unittest.TestCase):
    """Delivery is mocked to succeed so we can watch fired_at / held signals directly, and `self.now`
    is the clock `check_reminders` sees."""

    def setUp(self):
        for p in (mock.patch.object(tz_common, "_zone", return_value=timezone(timedelta(hours=-5))),
                  mock.patch.object(clock, "load_identity", return_value={}),
                  mock.patch.object(identity_common, "load_identity", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp(prefix="ack-advance-test-")
        self.now = datetime(2026, 7, 13, 19, 12, 0, tzinfo=timezone.utc)  # mid-afternoon owner-local
        self._orig_deliver = sentinel._deliver_reminder
        sentinel._deliver_reminder = lambda *a, **k: ({"ok": True}, "telegram")  # noqa: E731

    def tearDown(self):
        sentinel._deliver_reminder = self._orig_deliver
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, obj):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def saved(self):
        with open(os.path.join(self.tmp, "reminders.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _nudge(self, rid, minutes_overdue, **extra):
        due = _iso(self.now - timedelta(minutes=minutes_overdue))
        return {"id": rid, "text": rid, "due_at": due, "channel": "telegram", "fired_at": None, **extra}

    def _run(self):
        return sentinel.check_reminders(self.tmp, self.now, fire=True, telegram_env="/no/such.env")

    def _kinds(self, signals, kind):
        return [s["id"] for s in signals if s["kind"] == kind]


class AckAdvancesStaggerTest(_StaggerHarness):

    def _stagger(self, fire_ago_sec, ack_ago_sec=None, raw_ack=None):
        state = {"last_nonpiercing_fire": _iso(self.now - timedelta(seconds=fire_ago_sec))}
        if ack_ago_sec is not None:
            state["last_ack_at"] = _iso(self.now - timedelta(seconds=ack_ago_sec))
        if raw_ack is not None:
            state["last_ack_at"] = raw_ack
        self.write("nudge-stagger.json", state)

    def _at(self, when):
        self.now = when
        return self._run()

    def test_single_ack_advances_after_debounce(self):
        # Fired 5 min ago (inside the 15-min hold); the owner acked 2 min ago → the hold is released.
        self._stagger(fire_ago_sec=300, ack_ago_sec=sentinel.ACK_ADVANCE_DEBOUNCE_SEC)
        self.write("reminders.json", [self._nudge("next", 20)])
        self.assertEqual(self._kinds(self._run(), "reminder_fired"), ["next"])

    def test_ack_inside_debounce_still_holds(self):
        # Same, but the ack is only 30 s old → still held (the batch may not have finished landing).
        self._stagger(fire_ago_sec=300, ack_ago_sec=30)
        self.write("reminders.json", [self._nudge("next", 20)])
        self.assertEqual(self._kinds(self._run(), "reminder_stagger_held"), ["next"])

    def test_ack_older_than_last_fire_does_nothing(self):
        # An ack from BEFORE the last fire is one that window already spent → ordinary 15-min hold.
        self._stagger(fire_ago_sec=300, ack_ago_sec=600)
        self.write("reminders.json", [self._nudge("next", 20)])
        self.assertEqual(self._kinds(self._run(), "reminder_stagger_held"), ["next"])

    def test_missing_or_garbled_ack_fails_safe(self):
        # No last_ack_at → no advance. An unparseable one → no advance. Both keep the plain hold.
        for raw in (None, "not-a-time", 42, {"x": 1}):
            with self.subTest(raw=raw):
                self._stagger(fire_ago_sec=300, raw_ack=raw)
                self.write("reminders.json", [self._nudge("next", 20)])
                self.assertEqual(self._kinds(self._run(), "reminder_stagger_held"), ["next"])

    def test_advance_fires_one_and_restamps_the_clock(self):
        # Two pending; the advance releases exactly ONE (one-per-pass is untouched), and the fire
        # re-stamps last_nonpiercing_fire past the ack — so the same ack cannot open the window twice —
        # while leaving last_ack_at standing (merge, not overwrite).
        self._stagger(fire_ago_sec=300, ack_ago_sec=sentinel.ACK_ADVANCE_DEBOUNCE_SEC)
        self.write("reminders.json", [self._nudge("first", 20), self._nudge("second", 10)])
        signals = self._run()
        self.assertEqual(self._kinds(signals, "reminder_fired"), ["first"])
        self.assertEqual(self._kinds(signals, "reminder_stagger_held"), ["second"])
        with open(os.path.join(self.tmp, "nudge-stagger.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual(sentinel.parse_iso(state["last_nonpiercing_fire"]), self.now)
        self.assertIn("last_ack_at", state)
        # Next tick, 5 s later: the ack is now OLDER than the fire → "second" waits the ordinary window.
        self.assertEqual(self._kinds(self._at(self.now + timedelta(seconds=5)),
                                     "reminder_stagger_held"), ["second"])

    def test_batch_ack_debounce_measures_from_the_last_ack(self):
        # Acks at t, t+3 s, t+8 s (each overwrites the stamp, as reminders_dequeue does) → nothing fires
        # before t+8 s+120 s; one fires after.
        t = self.now
        self.write("reminders.json", [self._nudge("next", 20)])
        for off in (0, 3, 8):
            ra.record_ack_instant(self.tmp, t + timedelta(seconds=off))
            ra.save_stagger_state(self.tmp, last_nonpiercing_fire=_iso(t - timedelta(minutes=5)))
        last = t + timedelta(seconds=8)
        for when in (t + timedelta(seconds=10), last + timedelta(seconds=119)):
            with self.subTest(when=when):
                self.assertEqual(self._kinds(self._at(when), "reminder_stagger_held"), ["next"])
        self.assertEqual(self._kinds(self._at(last + timedelta(seconds=120)), "reminder_fired"), ["next"])

    def test_three_acked_in_a_burst_one_pending(self):
        # Three nudges delivered, the owner was busy, then 👍'd all three within seconds. Each 👍 goes
        # through reminders_dequeue.main. The only thing that fires, ~2 min after the LAST ack — not
        # 15 min after the last fire — is the genuinely-next item, never an already-acked re-nudge.
        t = self.now
        fire_at = _iso(t - timedelta(minutes=3))
        delivered = [
            {**self._nudge("plants", 40, reminder_id="water-plants"), "fired_at": fire_at},
            {**self._nudge("stretch", 30, reminder_id="morning-stretch"), "fired_at": fire_at},
            {**self._nudge("ticket", 20, reminder_id="ticket-review"), "fired_at": fire_at},
        ]
        # The re-nudge ladder for the delivered rows, still queued, plus the one genuinely-next item.
        pending = [
            self._nudge("stretch-again", 5, reminder_id="morning-stretch"),
            self._nudge("ticket-again", 4, reminder_id="ticket-review"),
            self._nudge("breakfast", 2, reminder_id="eat-breakfast"),
        ]
        self.write("reminders.json", delivered + pending)
        self.write("nudge-stagger.json", {"last_nonpiercing_fire": fire_at})
        today = ra.local_today(t)
        for off, rid in ((0, "water-plants"), (3, "morning-stretch"), (8, "ticket-review")):
            with mock.patch.object(ra, "record_ack_instant",
                                   lambda sd, now=None, _o=off: ra.save_stagger_state(
                                       sd, last_ack_at=_iso(t + timedelta(seconds=_o)))):
                argv = ["--state-dir", self.tmp, "--reminder-id", rid, "--ack-date", today,
                        "--activity-day", today]
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(reminders_dequeue.main(argv), 0)
        # 30 s after the last 👍: still inside the debounce → nothing fires, nothing already-acked fires.
        signals = self._at(t + timedelta(seconds=38))
        self.assertEqual(self._kinds(signals, "reminder_fired"), [])
        # 2 min after the last 👍 (≈ 5 min since the last fire, far under 15): exactly "breakfast".
        signals = self._at(t + timedelta(seconds=8 + sentinel.ACK_ADVANCE_DEBOUNCE_SEC))
        self.assertEqual(self._kinds(signals, "reminder_fired"), ["breakfast"])
        # Never the acked rows — their queued re-nudges are gone (dequeued) or consumed by the acked gate.
        fired_ids = {r["id"] for r in self.saved() if r.get("fired_at")}
        self.assertNotIn("stretch-again", fired_ids)
        self.assertNotIn("ticket-again", fired_ids)
        self.assertEqual(fired_ids, {"plants", "stretch", "ticket", "breakfast"})


if __name__ == "__main__":
    unittest.main()
