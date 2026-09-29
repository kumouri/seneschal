#!/usr/bin/env python3
"""Tests for `failures.py` — the `state/failures.jsonl` append helper + the `vitals.json` watchdog
reader.

Two things matter most here: `record()` can never raise (it is called from inside handlers that are
already degraded), and `watchdog_status()` never conflates a stale tick with a healthy one."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import failures  # noqa: E402
import stateio  # noqa: E402

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


class Record(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_appends_one_row(self):
        failures.record(self.dir, "some.site", "some_kind", "detail text", now=NOW)
        rows = list(stateio.iter_jsonl(os.path.join(self.dir, failures.FAILURES_FILENAME)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site"], "some.site")
        self.assertEqual(rows[0]["kind"], "some_kind")
        self.assertEqual(rows[0]["detail"], "detail text")
        self.assertEqual(rows[0]["at"], "2026-09-02T12:00:00Z")

    def test_never_raises_even_when_the_append_itself_fails(self):
        """The whole point: a caller inside an already-degraded except branch must never be handed a
        NEW exception by the thing meant to record the old one."""
        with mock.patch.object(stateio, "append_jsonl", side_effect=OSError("disk full")):
            failures.record(self.dir, "site", "kind", "detail")  # must not raise
        self.assertEqual(list(stateio.iter_jsonl(os.path.join(self.dir, failures.FAILURES_FILENAME))), [])

    def test_detail_is_truncated(self):
        failures.record(self.dir, "site", "kind", "x" * 1000, now=NOW)
        rows = list(stateio.iter_jsonl(os.path.join(self.dir, failures.FAILURES_FILENAME)))
        self.assertEqual(len(rows[0]["detail"]), 500)

    def test_tail_and_count_since(self):
        failures.record(self.dir, "a", "k1", now=NOW)
        failures.record(self.dir, "b", "k2", now=NOW + timedelta(minutes=1))
        failures.record(self.dir, "c", "k3", now=NOW + timedelta(minutes=2))
        self.assertEqual(len(failures.tail(self.dir, 50)), 3)
        self.assertEqual(len(failures.tail(self.dir, 2)), 2)
        since = (NOW + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        self.assertEqual(failures.count_since(self.dir, since), 2)

    def test_tail_on_a_missing_file_is_empty_not_a_raise(self):
        self.assertEqual(failures.tail(self.dir), [])
        self.assertEqual(failures.count_since(self.dir, "2026-01-01T00:00:00Z"), 0)


class WatchdogStatus(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write_vitals(self, **over):
        base = {"tick_at": NOW.isoformat().replace("+00:00", "Z"), "reminders_fired_today": 0,
                "reminders_failed_today": 0, "last_reminder_fired_at": None,
                "slots_failed_today": 0, "outbox_dead": 0, "warm_session": True}
        base.update(over)
        stateio.write_json_atomic(os.path.join(self.dir, failures.VITALS_FILENAME), base)

    def test_no_vitals_file_is_unknown_never_healthy(self):
        status = failures.watchdog_status(self.dir, now=NOW)
        self.assertFalse(status["tick_fresh"])
        self.assertIsNone(status["healthy"])

    def test_fresh_tick_healthy_work_reads_healthy(self):
        self._write_vitals()
        status = failures.watchdog_status(self.dir, now=NOW)
        self.assertTrue(status["tick_fresh"])
        self.assertTrue(status["healthy"])

    def test_fresh_tick_failing_work_reads_unhealthy(self):
        """This is the case the watchdog exists for: a fresh, ticking daemon whose work is
        actually broken must never read the same as a quiet healthy one."""
        self._write_vitals(reminders_failed_today=3)
        status = failures.watchdog_status(self.dir, now=NOW)
        self.assertTrue(status["tick_fresh"])
        self.assertFalse(status["healthy"])

    def test_a_dead_outbox_letter_alone_is_unhealthy(self):
        self._write_vitals(outbox_dead=1)
        status = failures.watchdog_status(self.dir, now=NOW)
        self.assertTrue(status["tick_fresh"])
        self.assertFalse(status["healthy"])

    def test_a_failed_slot_alone_is_unhealthy(self):
        self._write_vitals(slots_failed_today=2)
        status = failures.watchdog_status(self.dir, now=NOW)
        self.assertTrue(status["tick_fresh"])
        self.assertFalse(status["healthy"])

    def test_stale_tick_is_unknown_even_if_the_last_reading_was_healthy(self):
        """A daemon that died three hours ago, having last reported healthy, must not be reported as
        healthy now — that is exactly the failure mode of the bare {"pending": 0, "dead": 0} return
        the watchdog replaces, one level up."""
        self._write_vitals()
        status = failures.watchdog_status(self.dir, now=NOW + timedelta(hours=3))
        self.assertFalse(status["tick_fresh"])
        self.assertIsNone(status["healthy"])

    def test_unreadable_tick_at_is_unknown(self):
        self._write_vitals(tick_at="not a timestamp")
        status = failures.watchdog_status(self.dir, now=NOW)
        self.assertFalse(status["tick_fresh"])
        self.assertIsNone(status["healthy"])

    def test_corrupt_vitals_file_is_unknown_not_a_raise(self):
        with open(os.path.join(self.dir, failures.VITALS_FILENAME), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        status = failures.watchdog_status(self.dir, now=NOW)
        self.assertFalse(status["tick_fresh"])
        self.assertIsNone(status["healthy"])


if __name__ == "__main__":
    unittest.main()
