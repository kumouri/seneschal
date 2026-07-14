#!/usr/bin/env python3
"""Tests for the quiet window (do-not-disturb) — the state helpers + the check_reminders delivery gate.

Stdlib ``unittest`` only (matches the Markdown-skill core; CI byte-compiles Python and this runs green
under ``python -m unittest``). Covers the 2026-07-08 fix: a "quiet the rest of the night" request must
DURABLY suppress ⭐-High-and-below nudges at the delivery chokepoint — surviving a rebuilt queue — while
Call Me + Critical-and-above still pierce; suppression is drop-not-defer (consumed, never delivered late).

Run:  python -m unittest seneschal.scripts.test_quiet   (or)   python test_quiet.py
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import sentinel as sn  # noqa: E402

NOW = datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc)  # 23:00 CT — the old late-night-buzz hour


def _z(dt):
    return dt.isoformat().replace("+00:00", "Z")


class QuietStateUnit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_absent_is_not_quiet(self):
        self.assertFalse(sn.is_quiet(self.dir, NOW))
        self.assertIsNone(sn.load_quiet(self.dir))

    def test_set_then_active_then_clear(self):
        sn.set_quiet(self.dir, NOW + timedelta(hours=8), reason="bed", set_by="chat")
        self.assertTrue(sn.is_quiet(self.dir, NOW))
        q = sn.load_quiet(self.dir)
        self.assertEqual(q["reason"], "bed")
        self.assertEqual(q["set_by"], "chat")
        self.assertTrue(sn.clear_quiet(self.dir))
        self.assertFalse(sn.is_quiet(self.dir, NOW))
        self.assertFalse(sn.clear_quiet(self.dir))  # idempotent — nothing left to remove

    def test_expired_window_reads_not_quiet(self):
        sn.set_quiet(self.dir, NOW - timedelta(minutes=1))  # already elapsed
        self.assertFalse(sn.is_quiet(self.dir, NOW))

    def test_malformed_until_fails_open(self):
        sn.save_json(os.path.join(self.dir, sn.QUIET_FILE), {"until": "not-a-date"})
        self.assertFalse(sn.is_quiet(self.dir, NOW))  # a broken file must never silence a real nudge


class PierceRuleUnit(unittest.TestCase):
    def test_plain_telegram_does_not_pierce(self):
        self.assertFalse(sn.entry_pierces_quiet({"channel": "telegram"}))
        self.assertFalse(sn.entry_pierces_quiet({}))  # default channel is telegram

    def test_call_and_escalate_and_flag_pierce(self):
        self.assertTrue(sn.entry_pierces_quiet({"channel": "call"}))
        self.assertTrue(sn.entry_pierces_quiet({"channel": "telegram", "escalate": True}))
        self.assertTrue(sn.entry_pierces_quiet({"channel": "telegram", "pierce_quiet": True}))


class CheckRemindersGate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig = sn.send_telegram
        self.sent = []
        sn.send_telegram = lambda text, env: (self.sent.append(text) or {"ok": True})

    def tearDown(self):
        sn.send_telegram = self._orig

    def _write(self, rows):
        sn.save_json(os.path.join(self.dir, "reminders.json"), rows)

    def _rows(self):
        return sn.load_json(os.path.join(self.dir, "reminders.json"), [])

    def test_quiet_suppresses_plain_but_pierces_critical_and_call(self):
        sn.set_quiet(self.dir, NOW + timedelta(hours=8))
        self._write([
            {"id": "alex", "text": "Check messages from Alex.", "due_at": _z(NOW), "channel": "telegram", "fired_at": None},
            {"id": "meds", "text": "Evening meds.", "due_at": _z(NOW), "channel": "telegram", "pierce_quiet": True, "fired_at": None},
            {"id": "ring", "text": "Take your meds.", "due_at": _z(NOW), "channel": "call", "fired_at": None},
        ])
        signals = sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        kinds = {s["id"]: s["kind"] for s in signals}
        self.assertEqual(kinds["alex"], "reminder_suppressed_quiet")
        self.assertEqual(kinds["meds"], "reminder_fired")
        self.assertEqual(kinds["ring"], "reminder_fired")
        rows = {r["id"]: r for r in self._rows()}
        self.assertTrue(rows["alex"].get("suppressed_at"))   # consumed…
        self.assertIsNone(rows["alex"].get("fired_at"))      # …but never delivered
        self.assertTrue(rows["meds"].get("fired_at"))
        self.assertNotIn("Alex", " ".join(self.sent))        # the low-stakes buzz never went out

    def test_suppressed_entry_is_not_redelivered_when_quiet_lifts(self):
        sn.set_quiet(self.dir, NOW + timedelta(hours=8))
        self._write([{"id": "alex", "text": "Check Alex.", "due_at": _z(NOW), "channel": "telegram", "fired_at": None}])
        sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")  # suppress during quiet
        sn.clear_quiet(self.dir)
        later = sn.check_reminders(self.dir, NOW + timedelta(hours=9), fire=True, telegram_env="x")
        self.assertEqual(later, [])                 # nothing to do — it was dropped, not deferred
        self.assertEqual(self.sent, [])             # no late avalanche

    def test_without_quiet_plain_fires_normally(self):
        self._write([{"id": "alex", "text": "Check Alex.", "due_at": _z(NOW), "channel": "telegram", "fired_at": None}])
        signals = sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        self.assertEqual(signals[0]["kind"], "reminder_fired")
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
