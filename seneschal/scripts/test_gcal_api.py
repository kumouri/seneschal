#!/usr/bin/env python3
"""Tests for gcal_api.py's send-gate wiring — asserts a real (network-mocked) `create-event`/`delete-event` appends the right
`recipient_class` to `state/send-recipients.jsonl` (never the recipient itself), and that the site is
GATED: an owner-class send passes untouched, an unapproved third-party/unknown send is REFUSED (exit
`send_gate.EXIT_REFUSED`, no network call, the ledger row carries `gate.allowed == false`), and one
with an approved row in `state/pending-approvals.json` passes and spends it. The owner's addresses
come from a fixture identity (`_owner_fixture`), never code.

Also pins the bare-date window: `--start`/`--end YYYY-MM-DD` is the owner-local calendar day
(`tz_common.utc_offset_on`), and a missing `--env-file` is a handled JSON error, exit 1."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta, timezone, tzinfo
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import gcal_api  # noqa: E402
import _owner_fixture as fx  # noqa: E402
import send_gate  # noqa: E402
import send_recipients  # noqa: E402
import stateio  # noqa: E402
import tz_common  # noqa: E402


class RecipientClassInstrumentation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fx.use_fixture_owner(self)
        self.ledger = os.path.join(self.dir, send_recipients.FILENAME)
        env_patch = mock.patch.dict(os.environ, {"SENESCHAL_STATE_DIR": self.dir})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        req_patch = mock.patch.object(gcal_api.gc, "authorized_request", return_value={"id": "evt1"})
        req_patch.start()
        self.addCleanup(req_patch.stop)

    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["gcal_api.py"] + argv):
            return gcal_api.main()

    def test_create_event_with_no_attendees_records_owner(self):
        rc = self._run(["create-event", "--account", "personal", "--summary", "Focus block",
                         "--start", "2026-09-10T10:00:00-05:00", "--end", "2026-09-10T10:30:00-05:00"])
        self.assertEqual(rc, 0)
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], "gcal_create_event")
        self.assertEqual(rows[0]["recipient_class"], "owner")

    def test_create_event_with_third_party_attendee_records_third_party(self):
        body = json.dumps({
            "summary": "Coffee",
            "start": {"dateTime": "2026-09-10T15:00:00-05:00"},
            "end": {"dateTime": "2026-09-10T15:30:00-05:00"},
            "attendees": [{"email": "recruiter@acme.example"}],
        })
        rc = self._run(["create-event", "--account", "personal", "--json", body])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)  # the gate: an invite to a third party needs approval
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["recipient_class"], "third_party")

    def test_create_event_with_only_owner_attendee_records_owner(self):
        body = json.dumps({
            "summary": "Solo block",
            "start": {"dateTime": "2026-09-10T15:00:00-05:00"},
            "end": {"dateTime": "2026-09-10T15:30:00-05:00"},
            "attendees": [{"email": "owner@example.com"}],
        })
        rc = self._run(["create-event", "--account", "personal", "--json", body])
        self.assertEqual(rc, 0)
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["recipient_class"], "owner")

    def test_delete_event_always_records_unknown(self):
        rc = self._run(["delete-event", "--account", "personal", "--id", "evt1"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)  # the gate: gated on the event id
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["channel"], "gcal_delete_event")
        self.assertEqual(rows[0]["recipient_class"], "unknown")

    def test_reads_write_no_row(self):
        rc = self._run(["events", "--account", "personal"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.ledger))

    def test_row_never_contains_the_attendee_address(self):
        body = json.dumps({
            "summary": "Coffee",
            "start": {"dateTime": "2026-09-10T15:00:00-05:00"},
            "end": {"dateTime": "2026-09-10T15:30:00-05:00"},
            "attendees": [{"email": "recruiter@acme.example"}],
        })
        self._run(["create-event", "--account", "personal", "--json", body])
        with open(self.ledger, "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("recruiter@acme.example", raw)


class SendGate(RecipientClassInstrumentation):
    def _req(self):
        return mock.patch.object(gcal_api.gc, "authorized_request", return_value={"id": "evt1"})

    def test_attendee_less_event_is_owner_and_passes(self):
        with self._req() as req:
            rc = self._run(["create-event", "--account", "personal", "--summary", "Focus block",
                             "--start", "2026-09-10T10:00:00-05:00", "--end", "2026-09-10T10:30:00-05:00"])
        self.assertEqual(rc, 0)
        req.assert_called_once()

    def test_third_party_attendee_refused_then_passes_with_a_grant(self):
        body = json.dumps({"summary": "Coffee", "start": {"dateTime": "2026-09-10T15:00:00-05:00"},
                           "end": {"dateTime": "2026-09-10T15:30:00-05:00"},
                           "attendees": [{"email": "recruiter@acme.example"}]})
        with self._req() as req:
            rc = self._run(["create-event", "--account", "personal", "--json", body])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)
        req.assert_not_called()
        send_gate.grant("calendar", "recruiter@acme.example", why="the owner said invite them")
        with self._req() as req:
            rc = self._run(["create-event", "--account", "personal", "--json", body])
        self.assertEqual(rc, 0)
        req.assert_called_once()

    def test_delete_is_gated_on_the_event_id(self):
        with self._req() as req:
            rc = self._run(["delete-event", "--account", "personal", "--id", "evt1"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)
        req.assert_not_called()
        send_gate.grant("calendar", "evt1", why="the owner said delete it")
        with self._req() as req:
            rc = self._run(["delete-event", "--account", "personal", "--id", "evt1"])
        self.assertEqual(rc, 0)
        req.assert_called_once()


class _FakeOwnerZone(tzinfo):
    """A DST-aware stand-in for the owner's zone, keyed on the local calendar DATE: UTC-5 from the
    2nd Sunday of March to the day before the 1st Sunday of November (2026: 03-08 .. 10-31), else
    UTC-6. Deterministic on any runner — no tzdata, no machine-local clock."""

    def utcoffset(self, dt):
        d = dt.date()
        return timedelta(hours=-5 if date(2026, 3, 8) <= d < date(2026, 11, 1) else -6)

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "FakeOwner"


class BareDateWindowTest(unittest.TestCase):
    """`_window` on a bare `--start`/`--end YYYY-MM-DD` must resolve the OWNER-LOCAL calendar day,
    not a naive instant stamped UTC (the bug: a bare date read as UTC shifts the window by the
    owner's offset, so the evening's events land in "tomorrow"). The owner zone is pinned to a fake
    DST-aware zone; dates are pinned; no wall-clock reads."""

    def setUp(self):
        patcher = mock.patch.object(tz_common, "_zone", return_value=_FakeOwnerZone())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _args(self, start=None, end=None, days=7):
        return argparse.Namespace(start=start, end=end, days=days)

    def test_bare_dates_in_summer_resolve_to_the_owner_calendar_day(self):
        time_min, time_max = gcal_api._window(self._args(start="2026-07-10", end="2026-07-10"))
        self.assertEqual(time_min, "2026-07-10T05:00:00Z")
        self.assertEqual(time_max, "2026-07-11T04:59:59Z")

    def test_bare_dates_in_winter_resolve_to_the_owner_calendar_day(self):
        time_min, time_max = gcal_api._window(self._args(start="2026-01-10", end="2026-01-10"))
        self.assertEqual(time_min, "2026-01-10T06:00:00Z")
        self.assertEqual(time_max, "2026-01-11T05:59:59Z")

    def test_bare_dates_straddling_the_spring_transition(self):
        # 2026-03-07 is standard time; the transition day 2026-03-08 is daylight time for its
        # whole span (the offset is the date's, read at local noon — never split at 02:00).
        time_min, time_max = gcal_api._window(self._args(start="2026-03-07", end="2026-03-07"))
        self.assertEqual(time_min, "2026-03-07T06:00:00Z")
        self.assertEqual(time_max, "2026-03-08T05:59:59Z")
        time_min, time_max = gcal_api._window(self._args(start="2026-03-08", end="2026-03-08"))
        self.assertEqual(time_min, "2026-03-08T05:00:00Z")
        self.assertEqual(time_max, "2026-03-09T04:59:59Z")

    def test_bare_start_only_uses_days_from_the_owner_local_start(self):
        time_min, time_max = gcal_api._window(self._args(start="2026-07-10", days=1))
        self.assertEqual(time_min, "2026-07-10T05:00:00Z")
        self.assertEqual(time_max, "2026-07-11T05:00:00Z")

    def test_explicit_iso_datetime_passes_through_untouched(self):
        time_min, time_max = gcal_api._window(self._args(
            start="2026-07-10T15:00:00-05:00", end="2026-07-10T15:30:00-05:00"))
        self.assertEqual(time_min, "2026-07-10T20:00:00Z")
        self.assertEqual(time_max, "2026-07-10T20:30:00Z")

    def test_a_different_owner_zone_moves_the_window(self):
        with mock.patch.object(tz_common, "_zone", return_value=timezone(timedelta(hours=9, minutes=30))):
            time_min, time_max = gcal_api._window(self._args(start="2026-07-10", end="2026-07-10"))
        self.assertEqual(time_min, "2026-07-09T14:30:00Z")
        self.assertEqual(time_max, "2026-07-10T14:29:59Z")


class EnvFileFailureTest(unittest.TestCase):
    """A missing/unreadable `--env-file` must be a handled `{"ok": false}` (exit 1), not an
    unhandled traceback — `load_env` now runs inside `main()`'s try/except."""

    def test_missing_env_file_is_a_handled_json_error_not_a_traceback(self):
        missing = os.path.join(tempfile.mkdtemp(), "does-not-exist.env")
        argv = ["list-calendars", "--account", "personal", "--env-file", missing]
        with mock.patch.object(sys, "argv", ["gcal_api.py"] + argv):
            with mock.patch("builtins.print") as mock_print:
                rc = gcal_api.main()  # must not raise
        self.assertEqual(rc, 1)
        mock_print.assert_called_once()
        payload = json.loads(mock_print.call_args[0][0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "list-calendars")
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
