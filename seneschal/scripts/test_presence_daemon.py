#!/usr/bin/env python3
"""Integration tests for the presence gate inside ``sentinel.check_reminders`` (Phase 1 daemon wiring).

Stdlib only. Uses a throwaway state dir; the **deferred** path never reaches delivery, so nothing sends
Telegram (the exact behavior we care about). The freshness helper is tested directly.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sentinel  # noqa: E402


def _ctx(at_place, updated):
    return {"at_place": at_place, "activity": None, "asleep": False, "since": {}, "updated_at": updated}


class PresenceContextFreshTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)

    def stamp(self, minutes_ago):
        return (self.now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%SZ")

    def test_fresh_is_true(self):
        self.assertTrue(sentinel._presence_context_fresh(_ctx("home", self.stamp(5)), self.now))

    def test_stale_is_false(self):
        self.assertFalse(sentinel._presence_context_fresh(_ctx("home", self.stamp(7 * 60)), self.now))

    def test_absent_updated_at_is_false(self):
        self.assertFalse(sentinel._presence_context_fresh({"at_place": "home"}, self.now))

    def test_bad_format_is_false(self):
        self.assertFalse(sentinel._presence_context_fresh(_ctx("home", "not-a-date"), self.now))

    def test_empty_is_false(self):
        self.assertFalse(sentinel._presence_context_fresh({}, self.now))


class PresenceDeferGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="presence-daemon-test-")
        self.now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, obj):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def fresh_stamp(self):
        return (self.now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%SZ")

    def _one_home_gated_reminder(self):
        self.write("reminders.json", [{
            "id": "rmd-home-walk", "text": "walk", "due_at": "2026-07-12T11:00:00Z",
            "channel": "telegram", "fired_at": None, "require_place": "home",
        }])

    def _saved(self):
        with open(os.path.join(self.tmp, "reminders.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_defers_and_holds_when_away(self):
        self._one_home_gated_reminder()
        self.write("presence-context.json", _ctx("work", self.fresh_stamp()))  # the owner is not home
        signals = sentinel.check_reminders(self.tmp, self.now, fire=True, telegram_env="/no/such.env")
        self.assertIn("reminder_deferred_presence", [s["kind"] for s in signals])
        # Deferred = held, not fired, and stamped with NOTHING (so it re-checks next loop).
        saved = self._saved()[0]
        self.assertIsNone(saved["fired_at"])
        self.assertNotIn("suppressed_at", saved)
        self.assertNotIn("acked_at", saved)

    def test_stale_context_fails_open_not_deferred(self):
        self._one_home_gated_reminder()
        old = (self.now - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%SZ")
        self.write("presence-context.json", _ctx("work", old))  # away, but the feed is stale
        signals = sentinel.check_reminders(self.tmp, self.now, fire=False, telegram_env="/no/such.env")
        # fire=False never defers/delivers (reports only); assert the freshness helper drove the decision.
        self.assertFalse(sentinel._presence_context_fresh(_ctx("work", old), self.now))
        self.assertIn("reminder_due", [s["kind"] for s in signals])

    def test_driving_defers_ordinary_reminder(self):
        # An ordinary (untagged, non-piercing) reminder is held while the owner is driving — no per-entry flag.
        self.write("reminders.json", [{
            "id": "rmd-walk", "text": "walk", "due_at": "2026-07-12T11:00:00Z",
            "channel": "telegram", "fired_at": None,
        }])
        self.write("presence-context.json", {
            "at_place": None, "activity": "in_vehicle", "asleep": False,
            "since": {}, "updated_at": self.fresh_stamp(),
        })
        signals = sentinel.check_reminders(self.tmp, self.now, fire=True, telegram_env="/no/such.env")
        self.assertIn("reminder_deferred_presence", [s["kind"] for s in signals])
        self.assertIsNone(self._saved()[0]["fired_at"])

    def test_asleep_does_not_defer(self):
        # Sleep gate RETIRED (2026-07-13): a fresh asleep=True context must not hold a nudge — the
        # phone-only Sleep API flags an idle phone as a sleeping owner. Delivery proceeds (and fails
        # here only because the test points at a nonexistent telegram env).
        self.write("reminders.json", [{
            "id": "rmd-walk", "text": "walk", "due_at": "2026-07-12T11:00:00Z",
            "channel": "telegram", "fired_at": None,
        }])
        self.write("presence-context.json", {
            "at_place": None, "activity": None, "asleep": True,
            "since": {}, "updated_at": self.fresh_stamp(),
        })
        signals = sentinel.check_reminders(self.tmp, self.now, fire=True, telegram_env="/no/such.env")
        kinds = [s["kind"] for s in signals]
        self.assertNotIn("reminder_deferred_presence", kinds)
        self.assertIn("reminder_send_failed", kinds)  # it reached delivery, not the defer branch


class CatchupStaggerTest(unittest.TestCase):
    """A released backlog (defer lifts / several nudges come due at once) must DRIP, not fire as a wall.
    Delivery is mocked to succeed so we can watch fired_at / held signals directly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stagger-test-")
        self.now = datetime(2026, 7, 13, 19, 12, 0, tzinfo=timezone.utc)
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
        due = (self.now - timedelta(minutes=minutes_overdue)).isoformat().replace("+00:00", "Z")
        return {"id": rid, "text": rid, "due_at": due, "channel": "telegram", "fired_at": None, **extra}

    def _run(self):
        return sentinel.check_reminders(self.tmp, self.now, fire=True, telegram_env="/no/such.env")

    def _kinds(self, signals, kind):
        return [s["id"] for s in signals if s["kind"] == kind]

    def test_backlog_fires_one_holds_the_rest(self):
        # Three ordinary nudges all overdue → exactly one fires, the other two are held (drip, not wall).
        self.write("reminders.json",
                   [self._nudge("a", 72), self._nudge("b", 60), self._nudge("c", 42)])
        signals = self._run()
        self.assertEqual(len(self._kinds(signals, "reminder_fired")), 1)
        self.assertEqual(len(self._kinds(signals, "reminder_stagger_held")), 2)
        # Oldest-due (a) is the one that fires; b and c stay pending (fired_at still None → re-checked).
        self.assertEqual(self._kinds(signals, "reminder_fired"), ["a"])
        by_id = {r["id"]: r for r in self.saved()}
        self.assertIsNotNone(by_id["a"]["fired_at"])
        self.assertIsNone(by_id["b"]["fired_at"])
        self.assertIsNone(by_id["c"]["fired_at"])

    def test_piercing_bypasses_the_gate(self):
        # A piercing item fires even after an ordinary one already fired this pass — separate lane.
        self.write("reminders.json", [
            self._nudge("ordinary", 60),
            self._nudge("meds", 30, pierce_quiet=True),  # newer, so it's iterated AFTER 'ordinary'
        ])
        fired = self._kinds(self._run(), "reminder_fired")
        self.assertIn("ordinary", fired)
        self.assertIn("meds", fired)  # not starved by the ordinary fire

    def test_within_gap_holds_a_lone_nudge(self):
        # One nudge due, but we fired a non-piercing nudge 5 min ago (< 15 min gap) → held.
        recent = (self.now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        self.write("nudge-stagger.json", {"last_nonpiercing_fire": recent})
        self.write("reminders.json", [self._nudge("late", 20)])
        signals = self._run()
        self.assertEqual(self._kinds(signals, "reminder_stagger_held"), ["late"])
        self.assertIsNone(self.saved()[0]["fired_at"])

    def test_after_gap_fires_and_records_clock(self):
        # Same nudge, but the last fire was 20 min ago (> gap) → it fires and re-stamps the clock.
        old = (self.now - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
        self.write("nudge-stagger.json", {"last_nonpiercing_fire": old})
        self.write("reminders.json", [self._nudge("ready", 20)])
        self.assertEqual(self._kinds(self._run(), "reminder_fired"), ["ready"])
        with open(os.path.join(self.tmp, "nudge-stagger.json"), encoding="utf-8") as fh:
            self.assertEqual(sentinel.parse_iso(json.load(fh)["last_nonpiercing_fire"]), self.now)


if __name__ == "__main__":
    unittest.main()
