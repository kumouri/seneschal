#!/usr/bin/env python3
"""Tests for `reminder_suppressions.py` — the durable ledger a `reminder_suppressed_stale` signal
writes to, so a silent staleness kill is no longer invisible outside `presence.log` (repeated silent
suppressions must reach the owner and the EOD Wrap). Stdlib ``unittest`` only.

Run:  python -m unittest seneschal.scripts.test_reminder_suppressions   (or)   python test_reminder_suppressions.py
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
import reminder_suppressions as rs  # noqa: E402
import tz_common  # noqa: E402

SUMMER = timezone(timedelta(hours=-5))  # a fixed UTC-5 owner zone (no tzdata needed)


def _z(dt):
    return dt.isoformat().replace("+00:00", "Z")


class RecordAndRead(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rsupp-test-")
        # Pin the owner zone + identity (default day boundary 05:00): the activity-day test must
        # not depend on the runner's own timezone or a real persona/identity.json.
        for p in (mock.patch.object(tz_common, "_zone", return_value=SUMMER),
                  mock.patch.object(clock, "load_identity", return_value={})):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_record_then_tail_round_trips_the_signal_fields(self):
        now = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
        signal = {"kind": "reminder_suppressed_stale", "id": "rmd-2026-09-14-renew-passport-0800",
                  "reminder_id": "00000000-0000-0000-0000-000000000077", "text": "Renew the passport.",
                  "due_at": "2026-09-14T13:00:00Z", "late_sec": 7260, "presence_held_sec": 0,
                  "stagger_held_sec": 5460}
        rs.record(self.dir, signal, now=now)
        rows = rs.tail(self.dir, 10)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["at"], _z(now))
        self.assertEqual(row["id"], "rmd-2026-09-14-renew-passport-0800")
        self.assertEqual(row["reminder_id"], "00000000-0000-0000-0000-000000000077")
        self.assertEqual(row["text"], "Renew the passport.")
        self.assertEqual(row["late_sec"], 7260)
        self.assertEqual(row["stagger_held_sec"], 5460)

    def test_record_never_raises_on_a_broken_state_dir(self):
        # A file where a directory should be — os.makedirs inside append_jsonl must fail, and record()
        # must swallow it, exactly like failures.record.
        blocked = os.path.join(self.dir, "blocked")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        try:
            rs.record(os.path.join(blocked, "nested"), {"id": "x"})
        except Exception as e:  # noqa: BLE001
            self.fail(f"record() raised: {e!r}")

    def test_count_since_is_a_lexical_iso_compare(self):
        rs.record(self.dir, {"id": "a"}, now=datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc))
        rs.record(self.dir, {"id": "b"}, now=datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(rs.count_since(self.dir, "2026-09-15T00:00:00Z"), 1)
        self.assertEqual(rs.count_since(self.dir, "2026-09-01T00:00:00Z"), 2)

    def test_count_today_uses_the_activity_day_cut(self):
        # 01:29 owner-local (06:29Z at UTC-5) on 2026-09-15 is still 2026-09-14's activity day.
        late_night = datetime(2026, 9, 15, 6, 29, tzinfo=timezone.utc)
        rs.record(self.dir, {"id": "late"}, now=late_night)
        self.assertEqual(rs.count_today(self.dir, now=late_night), 1)
        # A genuinely-next-day read (mid-morning 09-15) must NOT count that 06:29Z row.
        next_morning = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(rs.count_today(self.dir, now=next_morning), 0)

    def test_absent_log_reads_as_empty_not_an_error(self):
        self.assertEqual(rs.tail(self.dir), [])
        self.assertEqual(rs.count_since(self.dir, "2026-01-01T00:00:00Z"), 0)
        self.assertEqual(rs.count_today(self.dir, now=datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)), 0)


if __name__ == "__main__":
    unittest.main()
