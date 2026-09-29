#!/usr/bin/env python3
"""Unit tests for the presence event spine (``presence_common`` + ``presence_import``).

Run: ``python -m unittest test_presence`` (or the repo's discover: ``-s seneschal/scripts -p "test_*.py"``).
Stdlib only; each test uses a throwaway temp dir so nothing touches the real ``state/``.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import presence_common as pc  # noqa: E402
from presence_import import import_events, status  # noqa: E402


def geofence(place, transition, ts_ms, off=-300, uuid=None):
    d = {"t": "geofence", "place": place, "transition": transition, "ts_ms": ts_ms, "offset_min": off}
    if uuid:
        d["uuid"] = uuid
    return json.dumps(d)


def activity(name, transition, ts_ms, off=-300, conf=None):
    d = {"t": "activity", "activity": name, "transition": transition, "ts_ms": ts_ms, "offset_min": off}
    if conf is not None:
        d["confidence"] = conf
    return json.dumps(d)


def sleep(state, ts_ms, off=-300):
    return json.dumps({"t": "sleep", "state": state, "ts_ms": ts_ms, "offset_min": off})


class PresenceSpineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="presence-test-")
        self.db = os.path.join(self.tmp, "presence.db")
        self.conn = pc.connect(self.db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def imp(self, lines):
        return import_events(self.conn, lines, state_dir=self.tmp)

    def ctx(self):
        with open(pc.context_path(self.tmp), encoding="utf-8") as fh:
            return json.load(fh)

    def test_geofence_enter_sets_place_and_writes_context(self):
        counts = self.imp([geofence("home", "enter", 1_000_000)])
        self.assertEqual(counts.get("geofence"), 1)
        self.assertEqual(status(self.conn)["events"], 1)
        ctx = self.ctx()
        self.assertEqual(ctx["at_place"], "home")
        self.assertFalse(ctx["asleep"])
        self.assertIsNotNone(ctx["since"]["place"])

    def test_exit_clears_place(self):
        self.imp([geofence("home", "enter", 1_000_000)])
        self.imp([geofence("home", "exit", 1_100_000)])
        self.assertIsNone(self.ctx()["at_place"])

    def test_latest_event_wins_regardless_of_arrival_order(self):
        # exit (later ts) arrives in the same batch as enter (earlier ts): event time, not arrival, decides.
        self.imp([geofence("home", "exit", 2_000_000), geofence("home", "enter", 1_000_000)])
        self.assertIsNone(self.ctx()["at_place"])

    def test_activity_and_sleep_context(self):
        self.imp([activity("in_vehicle", "enter", 3_000_000, conf=90), sleep("asleep", 3_100_000)])
        ctx = self.ctx()
        self.assertEqual(ctx["activity"], "in_vehicle")
        self.assertTrue(ctx["asleep"])
        self.imp([sleep("awake", 3_200_000)])
        self.assertFalse(self.ctx()["asleep"])

    def test_idempotent_reimport(self):
        batch = [geofence("home", "enter", 1_000_000), activity("still", "enter", 1_000_001)]
        self.imp(batch)
        self.imp(batch)  # same events again
        self.assertEqual(status(self.conn)["events"], 2)

    def test_explicit_uuid_is_the_identity(self):
        # same uuid, different ts -> still one row (uuid wins over the derived key)
        self.imp([geofence("home", "enter", 1, uuid="abc")])
        self.imp([geofence("home", "enter", 999, uuid="abc")])
        self.assertEqual(status(self.conn)["events"], 1)

    def test_unknown_type_skipped_and_bad_line_counted(self):
        counts = self.imp([json.dumps({"t": "weather", "temp": 50}), "{not json",
                           geofence("home", "enter", 5)])
        self.assertEqual(counts.get("skipped"), 1)
        self.assertEqual(counts.get("bad"), 1)
        self.assertEqual(counts.get("geofence"), 1)

    def test_bad_transition_is_counted_bad_not_stored(self):
        counts = self.imp([geofence("home", "sideways", 5)])
        self.assertEqual(counts.get("bad"), 1)
        self.assertEqual(status(self.conn)["events"], 0)

    def test_confidence_persisted(self):
        self.imp([activity("walking", "enter", 4_000_000, conf=75)])
        row = self.conn.execute("SELECT confidence FROM events WHERE kind='activity'").fetchone()
        self.assertEqual(row["confidence"], 75)

    def test_read_context_default_when_absent(self):
        c = pc.read_context(self.tmp)  # nothing written yet this test
        self.assertIsNone(c["at_place"])
        self.assertFalse(c["asleep"])

    def test_prune_old_events(self):
        now_ms = int(time.time() * 1000)
        self.imp([geofence("home", "enter", 1000),        # 1970 -> old, prunable
                  geofence("home", "enter", now_ms)])     # now  -> recent, kept
        self.assertEqual(status(self.conn)["events"], 2)
        self.assertEqual(pc.prune_events(self.conn, keep_days=30), 1)
        self.assertEqual(status(self.conn)["events"], 1)


def w_sleep(state, ts_ms, off=-300):
    """A sleep reading attributed to a wearable."""
    return json.dumps({"t": "sleep", "state": state, "ts_ms": ts_ms, "offset_min": off,
                       "source": "watch"})


MIN_MS = 60_000


class SourceProvenanceTest(unittest.TestCase):
    """Every event may name the device that observed it. Provenance only — the rollup still takes the
    newest reading of each kind, whatever device it came from."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="presence-source-")
        self.conn = pc.connect(os.path.join(self.tmp, "presence.db"))
        self.base = 1785000000000

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self, lines):
        return import_events(self.conn, lines, state_dir=self.tmp)["context"]

    def test_no_source_means_phone(self):
        c = self.ctx([sleep("asleep", self.base)])
        self.assertTrue(c["asleep"])
        self.assertEqual(c["sleep_source"], "phone")

    def test_the_context_names_the_device_behind_each_reading(self):
        c = self.ctx([w_sleep("awake", self.base),
                      activity("walking", "enter", self.base)])
        self.assertEqual(c["sleep_source"], "watch")
        self.assertEqual(c["activity_source"], "phone")

    def test_newest_reading_wins_whatever_its_source(self):
        """No device precedence: a newer phone reading beats an older wearable one."""
        c = self.ctx([w_sleep("awake", self.base), sleep("asleep", self.base + 5 * MIN_MS)])
        self.assertTrue(c["asleep"])
        self.assertEqual(c["sleep_source"], "phone")

    def test_no_reading_means_no_source(self):
        c = self.ctx([geofence("home", "enter", self.base)])
        self.assertIsNone(c["sleep_source"])
        self.assertIsNone(c["activity_source"])

    def test_same_millisecond_from_both_devices_keeps_both_rows(self):
        """The derived event id includes the source. Without it, whichever arrived second would
        silently overwrite the first — and which one that is depends on network timing."""
        self.ctx([sleep("asleep", self.base), w_sleep("asleep", self.base)])
        n = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(n, 2)

    def test_unknown_source_degrades_to_phone(self):
        c = self.ctx([json.dumps({"t": "sleep", "state": "asleep", "ts_ms": self.base,
                                  "offset_min": -300, "source": "toaster"})])
        self.assertEqual(c["sleep_source"], "phone")

    def test_source_is_case_insensitive(self):
        c = self.ctx([json.dumps({"t": "sleep", "state": "awake", "ts_ms": self.base,
                                  "offset_min": -300, "source": "WATCH"})])
        self.assertEqual(c["sleep_source"], "watch")


class SourceMigrationTest(unittest.TestCase):
    """The `source` column has to be ADDED to a live database — `CREATE TABLE IF NOT EXISTS` does
    nothing to an existing one, and a real presence.db can hold thousands of events."""

    OLD_SCHEMA = (
        "CREATE TABLE IF NOT EXISTS events ("
        " event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,"
        " transition TEXT NOT NULL, ts_utc TEXT NOT NULL, ts_ms INTEGER NOT NULL,"
        " tz_offset_min INTEGER NOT NULL, confidence INTEGER, ingested_at TEXT NOT NULL);"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="presence-migrate-")
        self.db = os.path.join(self.tmp, "presence.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _old_db_with_rows(self, n=3):
        import sqlite3
        c = sqlite3.connect(self.db)
        c.executescript(self.OLD_SCHEMA)
        for i in range(n):
            c.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                      ("e%d" % i, "sleep", "asleep", "enter", "2026-07-01T00:00:00",
                       1785000000000 + i, -300, None, "2026-07-01T00:00:00Z"))
        c.commit()
        c.close()

    def test_column_is_added_and_existing_rows_default_to_phone(self):
        self._old_db_with_rows()
        conn = pc.connect(self.db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
            self.assertIn("source", cols)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 3)
            sources = {r[0] for r in conn.execute("SELECT DISTINCT source FROM events")}
            self.assertEqual(sources, {"phone"})
        finally:
            conn.close()

    def test_migration_is_idempotent(self):
        self._old_db_with_rows()
        pc.connect(self.db).close()
        conn = pc.connect(self.db)          # must not raise "duplicate column name"
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 3)
        finally:
            conn.close()

    def test_a_fresh_db_already_has_the_column(self):
        conn = pc.connect(self.db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
            self.assertIn("source", cols)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
