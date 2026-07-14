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


if __name__ == "__main__":
    unittest.main()
