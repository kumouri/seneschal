#!/usr/bin/env python3
"""Tests for `brief_prestage.py` — the Brief's pre-stage cache (Dream step 1b's snapshot).

Round trip, staleness, corruption and the CLI's own exit codes — the four things the Brief's Phase 1
actually depends on: a fresh write reads back byte-identical, a stale one refuses rather than being
handed to the Brief as current, a corrupt file refuses rather than raising, and `status` never fails.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import brief_prestage  # noqa: E402


class WriteReadRoundTrip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_write_then_read_returns_the_same_payload(self):
        payload = {"tasks": [{"name": "Renew passport", "id": "abc123"}], "flags": [], "projects": []}
        brief_prestage.write(payload, state_dir=self.dir)
        self.assertEqual(brief_prestage.read(state_dir=self.dir), payload)

    def test_write_stamps_fetched_at_utc(self):
        now = datetime(2026, 9, 13, 4, 0, 0, tzinfo=timezone.utc)
        record = brief_prestage.write({"tasks": []}, state_dir=self.dir, now=now)
        self.assertEqual(record["fetched_at"], "2026-09-13T04:00:00Z")

    def test_write_is_atomic_replace_not_in_place(self):
        brief_prestage.write({"tasks": [1]}, state_dir=self.dir)
        brief_prestage.write({"tasks": [2]}, state_dir=self.dir)
        path = os.path.join(self.dir, brief_prestage.FILENAME)
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["payload"], {"tasks": [2]})

    def test_read_respects_seneschal_state_dir_env_over_explicit_arg(self):
        from unittest import mock
        env_dir = tempfile.mkdtemp()
        brief_prestage.write({"tasks": []}, state_dir=env_dir)
        with mock.patch.dict(os.environ, {"SENESCHAL_STATE_DIR": env_dir}):
            # explicit arg points at self.dir (empty), but the env var must win
            payload = brief_prestage.read(state_dir=self.dir)
        self.assertEqual(payload, {"tasks": []})


class Staleness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_fresh_payload_reads_fine(self):
        now = datetime(2026, 9, 13, 4, 0, 0, tzinfo=timezone.utc)
        brief_prestage.write({"tasks": []}, state_dir=self.dir, now=now)
        later = now + timedelta(hours=8)
        self.assertEqual(brief_prestage.read(state_dir=self.dir, now=later), {"tasks": []})

    def test_payload_past_the_default_window_raises_stale(self):
        now = datetime(2026, 9, 13, 4, 0, 0, tzinfo=timezone.utc)
        brief_prestage.write({"tasks": []}, state_dir=self.dir, now=now)
        later = now + timedelta(hours=brief_prestage.DEFAULT_MAX_AGE_HOURS + 0.1)
        with self.assertRaises(brief_prestage.Stale):
            brief_prestage.read(state_dir=self.dir, now=later)

    def test_custom_max_age_is_honored(self):
        now = datetime(2026, 9, 13, 4, 0, 0, tzinfo=timezone.utc)
        brief_prestage.write({"tasks": []}, state_dir=self.dir, now=now)
        later = now + timedelta(hours=2)
        with self.assertRaises(brief_prestage.Stale):
            brief_prestage.read(state_dir=self.dir, now=later, max_age_hours=1.0)

    def test_stale_reason_names_the_age_and_the_window(self):
        now = datetime(2026, 9, 13, 4, 0, 0, tzinfo=timezone.utc)
        brief_prestage.write({"tasks": []}, state_dir=self.dir, now=now)
        later = now + timedelta(hours=13)
        with self.assertRaises(brief_prestage.Stale) as ctx:
            brief_prestage.read(state_dir=self.dir, now=later)
        self.assertIn("13.0h", ctx.exception.reason)
        self.assertIn("12.0h", ctx.exception.reason)


class Unreadable(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_missing_file_raises_unreadable(self):
        with self.assertRaises(brief_prestage.Unreadable):
            brief_prestage.read(state_dir=self.dir)

    def test_corrupt_json_raises_unreadable_not_a_crash(self):
        path = os.path.join(self.dir, brief_prestage.FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        with self.assertRaises(brief_prestage.Unreadable):
            brief_prestage.read(state_dir=self.dir)

    def test_wrong_shape_raises_unreadable(self):
        path = os.path.join(self.dir, brief_prestage.FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tasks": []}, fh)  # no "fetched_at"/"payload" envelope
        with self.assertRaises(brief_prestage.Unreadable):
            brief_prestage.read(state_dir=self.dir)

    def test_unparseable_fetched_at_raises_unreadable(self):
        path = os.path.join(self.dir, brief_prestage.FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": brief_prestage.SCHEMA, "fetched_at": "not-a-date", "payload": {}}, fh)
        with self.assertRaises(brief_prestage.Unreadable):
            brief_prestage.read(state_dir=self.dir)


class StatusNeverFails(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_absent_store(self):
        self.assertEqual(brief_prestage.status(state_dir=self.dir), {"exists": False})

    def test_fresh_store(self):
        now = datetime(2026, 9, 13, 4, 0, 0, tzinfo=timezone.utc)
        brief_prestage.write({"tasks": []}, state_dir=self.dir, now=now)
        st = brief_prestage.status(state_dir=self.dir, now=now + timedelta(hours=1))
        self.assertTrue(st["exists"])
        self.assertTrue(st["readable"])
        self.assertTrue(st["fresh"])
        self.assertEqual(st["age_hours"], 1.0)

    def test_corrupt_store_reports_unreadable_rather_than_raising(self):
        path = os.path.join(self.dir, brief_prestage.FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json at all")
        st = brief_prestage.status(state_dir=self.dir)
        self.assertTrue(st["exists"])
        self.assertFalse(st["readable"])

    def test_stale_store_reports_fresh_false(self):
        now = datetime(2026, 9, 13, 4, 0, 0, tzinfo=timezone.utc)
        brief_prestage.write({"tasks": []}, state_dir=self.dir, now=now)
        later = now + timedelta(hours=brief_prestage.DEFAULT_MAX_AGE_HOURS + 1)
        st = brief_prestage.status(state_dir=self.dir, now=later)
        self.assertFalse(st["fresh"])


class CLI(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _run(self, args, stdin_text=""):
        old_stdin = sys.stdin
        try:
            import io
            sys.stdin = io.StringIO(stdin_text)
            return brief_prestage._main(["--state-dir", self.dir] + args)
        finally:
            sys.stdin = old_stdin

    def test_write_then_read_round_trip_through_the_cli(self):
        rc = self._run(["write"], stdin_text=json.dumps({"tasks": [{"name": "x"}]}))
        self.assertEqual(rc, 0)
        rc = self._run(["read"])
        self.assertEqual(rc, 0)

    def test_write_rejects_invalid_json_on_stdin(self):
        rc = self._run(["write"], stdin_text="{not json")
        self.assertEqual(rc, 2)

    def test_read_exits_3_when_nothing_written_yet(self):
        rc = self._run(["read"])
        self.assertEqual(rc, brief_prestage.EXIT_READ_REFUSED)

    def test_read_exits_3_on_corrupt_file(self):
        path = os.path.join(self.dir, brief_prestage.FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        rc = self._run(["read"])
        self.assertEqual(rc, brief_prestage.EXIT_READ_REFUSED)

    def test_status_always_exits_0(self):
        rc = self._run(["status"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
