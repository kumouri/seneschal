#!/usr/bin/env python3
"""Tests for reminders_roll.py — the standing every-N-hours reminder roll refill.

Stdlib ``unittest`` only. Deterministic off any host clock: every test passes an explicit
timezone-aware ``now_local`` (fixed -05:00, i.e. CDT), so the UTC due instants are computed from that
offset rather than the machine's tz. Run:  python -m unittest seneschal.scripts.test_reminders_roll
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import reminders_roll as rr  # noqa: E402
import presence as pr  # noqa: E402

CDT = timezone(timedelta(hours=-5))
ROLL = {
    "id_prefix": "alex",
    "text": "Check messages from Alex.",
    "reminder_id": "00000000-0000-0000-0000-000000000042",
    "start_hour": 9,
    "end_hour": 23,
    "interval_hours": 2,
    "channel": "telegram",
}


class RollEntries(unittest.TestCase):
    def test_future_only_skips_past_slots_today(self):
        now = datetime(2026, 7, 7, 18, 30, tzinfo=CDT)  # 6:30pm CDT
        entries = rr.roll_entries(ROLL, now, horizon_days=1)
        # today's 9/11/13/15/17 are past; only 19/21/23 remain.
        self.assertEqual([e["id"] for e in entries],
                         ["rmd-2026-07-07-alex-19", "rmd-2026-07-07-alex-21", "rmd-2026-07-07-alex-23"])

    def test_due_at_is_utc_z_from_local_offset(self):
        now = datetime(2026, 7, 7, 18, 30, tzinfo=CDT)
        by_id = {e["id"]: e for e in rr.roll_entries(ROLL, now, horizon_days=1)}
        # 19:00 CDT (-05:00) == 00:00Z the next day.
        self.assertEqual(by_id["rmd-2026-07-07-alex-19"]["due_at"], "2026-07-08T00:00:00Z")
        self.assertEqual(by_id["rmd-2026-07-07-alex-23"]["due_at"], "2026-07-08T04:00:00Z")

    def test_two_day_horizon_seeds_full_next_day(self):
        now = datetime(2026, 7, 7, 18, 30, tzinfo=CDT)
        entries = rr.roll_entries(ROLL, now, horizon_days=2)
        tomorrow = [e for e in entries if e["id"].startswith("rmd-2026-07-08-")]
        # tomorrow gets the full 9..23 step-2 roll: 8 slots.
        self.assertEqual([e["id"][-2:] for e in tomorrow],
                         ["09", "11", "13", "15", "17", "19", "21", "23"])

    def test_reminder_id_and_channel_carried(self):
        now = datetime(2026, 7, 7, 8, 0, tzinfo=CDT)
        e = rr.roll_entries(ROLL, now, horizon_days=1)[0]
        self.assertEqual(e["reminder_id"], ROLL["reminder_id"])
        self.assertEqual(e["channel"], "telegram")


class RefillRolls(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.now = datetime(2026, 7, 7, 18, 30, tzinfo=CDT)

    def test_adds_then_idempotent(self):
        added = rr.refill_rolls(self.dir, now_local=self.now, log=lambda *_: None, rolls=[ROLL])
        self.assertEqual(added, 3 + 8)  # today future (3) + tomorrow full (8)
        again = rr.refill_rolls(self.dir, now_local=self.now, log=lambda *_: None, rolls=[ROLL])
        self.assertEqual(again, 0)  # same ids already queued → nothing added

    def test_written_entries_have_queue_schema(self):
        rr.refill_rolls(self.dir, now_local=self.now, log=lambda *_: None, rolls=[ROLL])
        with open(os.path.join(self.dir, "reminders.json"), encoding="utf-8") as fh:
            queue = json.load(fh)
        e = queue[0]
        self.assertEqual(set(e), {"id", "text", "due_at", "channel", "created_at", "fired_at",
                                  "reminder_id", "ack_gate"})
        self.assertIsNone(e["fired_at"])
        self.assertIs(e["ack_gate"], False)  # rolls opt out of the fire-time ack gate (multi-fire/day)

    def test_preserves_unrelated_existing_entries(self):
        rr.save_reminders(os.path.join(self.dir, "reminders.json"),
                          [{"id": "rmd-keepme", "text": "x", "due_at": "2026-07-07T00:00:00Z",
                            "channel": "telegram", "created_at": "2026-07-07T00:00:00Z", "fired_at": None}])
        rr.refill_rolls(self.dir, now_local=self.now, log=lambda *_: None, rolls=[ROLL])
        with open(os.path.join(self.dir, "reminders.json"), encoding="utf-8") as fh:
            ids = [r["id"] for r in json.load(fh)]
        self.assertIn("rmd-keepme", ids)


class MaybeRefillGate(unittest.TestCase):
    def test_stamps_once_per_local_day(self):
        d = tempfile.mkdtemp()
        now = datetime(2026, 7, 7, 18, 30, tzinfo=CDT)
        pr.maybe_refill_rolls(d, lambda *_: None, now_local=now)
        with open(os.path.join(d, "rolls.json"), encoding="utf-8") as fh:
            stamp = json.load(fh)
        self.assertEqual(stamp["refilled"], "2026-07-07")
        # second call same day is a no-op (stamp already set) — must not raise.
        pr.maybe_refill_rolls(d, lambda *_: None, now_local=now)


if __name__ == "__main__":
    unittest.main()
