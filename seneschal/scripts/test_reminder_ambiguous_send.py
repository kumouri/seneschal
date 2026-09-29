#!/usr/bin/env python3
"""Tests for the reminder loop's handling of an AMBIGUOUS send (`sentinel.check_reminders`).

The defect: `telegram_send` marks a failure `{"ambiguous": true}` precisely so callers do not retry
it — Telegram may already have delivered the request, and a blind retry is the duplicate, not a
recovery. A reminder loop that tests only `res.get("ok")` leaves an ambiguously-sent row exactly as due
as it was before the attempt, so the very next tick fires it again, blind — on the channel that carries
the owner's most important reminders.

The fix: an ambiguous result is HELD, not re-armed — stamped `ambiguous_send_at` (skipped by the
loop's own due-check exactly like `fired_at`/`suppressed_at`/`acked_at`) and reported as
`reminder_send_ambiguous`, never `reminder_fired` or `reminder_send_failed`.

Both a real failure and an ambiguous one are also `failures.jsonl` rows (`failures.record` at the
send site), so a nudge that should have fired and didn't leaves a durable trace.

Stdlib ``unittest`` only. No wall-clock reads — every instant is an explicit UTC literal, and the owner
zone + identity are pinned (a fixed UTC-5), following ``test_night_curfew.py``'s ``_GateHarness``.

Run:  python -m unittest seneschal.scripts.test_reminder_ambiguous_send
"""
import os
import sys
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import clock  # noqa: E402
import failures  # noqa: E402
import identity_common  # noqa: E402
import sentinel as sn  # noqa: E402
import tz_common  # noqa: E402


def _utc(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _z(dt):
    return dt.isoformat().replace("+00:00", "Z")


class _GateHarness(unittest.TestCase):
    """Shared fixture: a temp state dir, delivery result stubbed per-test."""

    def setUp(self):
        for p in (mock.patch.object(tz_common, "_zone", return_value=timezone(timedelta(hours=-5))),
                  mock.patch.object(clock, "load_identity", return_value={}),
                  mock.patch.object(identity_common, "load_identity", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        self.dir = tempfile.mkdtemp(prefix="ambiguous-send-test-")
        self._orig = sn._deliver_reminder

    def tearDown(self):
        sn._deliver_reminder = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def stub(self, result):
        sn._deliver_reminder = lambda ch, raw, *a, **k: (result, "telegram")

    def write(self, rows):
        sn.save_json(os.path.join(self.dir, "reminders.json"), rows)

    def rows(self):
        return {r["id"]: r for r in sn.load_json(os.path.join(self.dir, "reminders.json"), [])}

    def run_at(self, now):
        return sn.check_reminders(self.dir, now, fire=True, telegram_env="x")

    def kinds(self, signals):
        return {s["id"]: s["kind"] for s in signals if "id" in s}

    def nudge(self, rid="critical", due=None):
        return {"id": rid, "text": "Renew the passport.", "due_at": _z(due or _utc(2026, 9, 18, 12, 0)),
                "channel": "telegram", "fired_at": None}


class AmbiguousSendIsHeldNotReArmed(_GateHarness):
    def test_ambiguous_result_is_not_reported_as_fired(self):
        self.stub({"ok": False, "ambiguous": True, "error": "telegram_send.py timed out"})
        self.write([self.nudge()])
        signals = self.run_at(_utc(2026, 9, 18, 12, 0))
        self.assertEqual(self.kinds(signals)["critical"], "reminder_send_ambiguous")

    def test_ambiguous_result_stamps_the_row_and_leaves_fired_at_unset(self):
        self.stub({"ok": False, "ambiguous": True, "error": "timed out"})
        self.write([self.nudge()])
        self.run_at(_utc(2026, 9, 18, 12, 0))
        row = self.rows()["critical"]
        self.assertTrue(row["ambiguous_send_at"])
        self.assertIsNone(row["fired_at"])
        self.assertNotIn("suppressed_at", row)

    def test_the_very_next_tick_does_not_blind_retry(self):
        """THE REGRESSION TEST. Before the fix, nothing distinguished an ambiguous failure from a
        transient one, so the row stayed due and the next tick fired it again — duplicating a
        message Telegram may already have delivered."""
        calls = []
        sn._deliver_reminder = lambda ch, raw, *a, **k: (
            calls.append(raw) or ({"ok": False, "ambiguous": True, "error": "timed out"}, "telegram"))
        self.write([self.nudge()])
        self.run_at(_utc(2026, 9, 18, 12, 0))
        self.run_at(_utc(2026, 9, 18, 12, 5))
        self.run_at(_utc(2026, 9, 18, 13, 0))
        self.assertEqual(len(calls), 1, "an ambiguous send must be attempted exactly once, ever")

    def test_an_ambiguous_send_is_a_failure_row(self):
        self.stub({"ok": False, "ambiguous": True, "error": "timed out"})
        self.write([self.nudge()])
        self.run_at(_utc(2026, 9, 18, 12, 0))
        rows = failures.tail(self.dir, 10)
        self.assertEqual([(r["site"], r["kind"]) for r in rows],
                         [("sentinel.check_reminders", "reminder_send_ambiguous")])
        self.assertIn("id=critical", rows[0]["detail"])

    def test_a_non_ambiguous_failure_still_retries_next_tick(self):
        """The other half of the fix: a PLAIN failure (nothing delivered, provably) must keep its
        existing behaviour — left due, retried next tick — so the ambiguous check does not
        accidentally widen into holding every failure."""
        calls = []
        sn._deliver_reminder = lambda ch, raw, *a, **k: (
            calls.append(raw) or ({"ok": False, "error": "connection refused"}, "telegram"))
        self.write([self.nudge()])
        signals1 = self.run_at(_utc(2026, 9, 18, 12, 0))
        self.assertEqual(self.kinds(signals1)["critical"], "reminder_send_failed")
        row = self.rows()["critical"]
        self.assertIsNone(row.get("fired_at"))
        self.assertNotIn("ambiguous_send_at", row)
        signals2 = self.run_at(_utc(2026, 9, 18, 12, 5))
        self.assertEqual(self.kinds(signals2)["critical"], "reminder_send_failed")
        self.assertEqual(len(calls), 2, "a provably-failed send must still be retried")

    def test_a_successful_send_still_fires_normally(self):
        """Guard against the fix over-widening: `ok: True` must be completely unaffected."""
        self.stub({"ok": True})
        self.write([self.nudge()])
        signals = self.run_at(_utc(2026, 9, 18, 12, 0))
        self.assertEqual(self.kinds(signals)["critical"], "reminder_fired")
        row = self.rows()["critical"]
        self.assertTrue(row["fired_at"])
        self.assertNotIn("ambiguous_send_at", row)

    def test_already_ambiguous_row_is_skipped_on_reload_even_if_due_again(self):
        """A row already stamped `ambiguous_send_at` (e.g. hand-restored from a backup, or a second
        process racing the same file) must be skipped exactly like a fired/suppressed/acked one —
        the top-of-loop due-check is the second half of the fix."""
        row = self.nudge()
        row["ambiguous_send_at"] = _z(_utc(2026, 9, 18, 11, 0))
        self.write([row])
        calls = []
        sn._deliver_reminder = lambda ch, raw, *a, **k: (calls.append(raw) or ({"ok": True}, "telegram"))
        signals = self.run_at(_utc(2026, 9, 18, 12, 0))
        self.assertEqual(signals, [])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
