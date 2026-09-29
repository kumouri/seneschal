#!/usr/bin/env python3
"""health_listener.py's restart-on-merge hook.

`health_listener.py` runs as its own always-on scheduled task, so the daemon's graceful restart never
reaches it: without this hook, a listener-code PR needs a human to remember a manual restart, and a
stale process keeps answering with old code (a route added by the PR 404s) until someone does.
`seneschald-control.ps1`'s Update path now writes `state/health-listener-control.json` on every pull;
the listener's poll thread acts on it.

These tests cover the pure decision functions only (`read_restart_request` / `restart_requested_since`)
with frozen, explicit datetimes — never the wall clock, and never the background thread's own
`time.sleep` loop, which is a thin imperative wrapper around the same decision. The writer side is
covered end to end by `test_seneschald_control.py`.

Run: python -m unittest discover -s seneschal/scripts -p "test_health_listener_restart_control.py"
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import health_listener  # noqa: E402


class ReadRestartRequestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="health_listener_restart_")
        self.path = os.path.join(self.tmp, "health-listener-control.json")

    def _write(self, doc) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def test_missing_file_is_none(self):
        self.assertIsNone(health_listener.read_restart_request(self.path))

    def test_malformed_json_is_none_not_raised(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertIsNone(health_listener.read_restart_request(self.path))

    def test_non_dict_json_is_none(self):
        self._write(["restart"])
        self.assertIsNone(health_listener.read_restart_request(self.path))

    def test_wrong_action_is_none(self):
        self._write({"action": "shutdown", "requested_at": "2026-01-15T12:00:00Z"})
        self.assertIsNone(health_listener.read_restart_request(self.path))

    def test_missing_requested_at_is_none(self):
        self._write({"action": "restart"})
        self.assertIsNone(health_listener.read_restart_request(self.path))

    def test_unparseable_requested_at_is_none(self):
        self._write({"action": "restart", "requested_at": "not-a-date"})
        self.assertIsNone(health_listener.read_restart_request(self.path))

    def test_z_suffixed_restart_request_parses_as_utc(self):
        self._write({"action": "restart", "requested_at": "2026-01-15T12:00:00Z"})
        dt = health_listener.read_restart_request(self.path)
        self.assertEqual(dt, datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc))

    def test_offset_requested_at_normalizes_to_utc(self):
        self._write({"action": "restart", "requested_at": "2026-01-15T07:00:00-05:00"})
        dt = health_listener.read_restart_request(self.path)
        self.assertEqual(dt, datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc))


class RestartRequestedSinceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="health_listener_restart_")
        self.path = os.path.join(self.tmp, "health-listener-control.json")
        self.started_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

    def _write(self, requested_at: str) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"action": "restart", "requested_at": requested_at}, fh)

    def test_no_file_means_no_restart(self):
        self.assertFalse(health_listener.restart_requested_since(self.started_at, self.path))

    def test_request_after_start_triggers_a_restart(self):
        self._write(
            (self.started_at + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertTrue(health_listener.restart_requested_since(self.started_at, self.path))

    def test_request_before_start_is_stale_and_ignored(self):
        # THE regression this class exists to pin: a restart already applied (this process's own
        # start is later than the request that caused it) must never re-fire on the next poll, or
        # every boot would immediately shut itself down again in a tight loop.
        self._write(
            (self.started_at - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertFalse(health_listener.restart_requested_since(self.started_at, self.path))

    def test_request_at_exactly_start_time_is_not_after_and_is_ignored(self):
        self._write(self.started_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertFalse(health_listener.restart_requested_since(self.started_at, self.path))

    def test_a_stale_request_stays_ignored_across_many_polls(self):
        # Simulates the real shape: the file is never cleared, so the same stale entry is read on
        # every ~5s poll for the rest of this process's life and must never flip to True.
        self._write(
            (self.started_at - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for _ in range(5):
            self.assertFalse(health_listener.restart_requested_since(self.started_at, self.path))


if __name__ == "__main__":
    unittest.main()
