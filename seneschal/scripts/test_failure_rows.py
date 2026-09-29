#!/usr/bin/env python3
"""The `failures.jsonl` breadcrumb lands ON THE FAILURE PATH, not merely "the code didn't crash".

One class per instrumented site. `failures.record` is called from inside an already-degraded
``except`` branch, so the only way to know a site is wired is to force that branch and read the row
back. Sites covered here:

  * `sentinel.check_reminders` — a reminder send that comes back not-ok (not a Python raise: the
    send itself failed, so the row is written beside the `reminder_send_failed` signal)
  * `jobs.cancel_job` — a `save_job` claim write that raises (the cancel itself must still land)

The same shape extends to the other fail-open sites as their modules land (the daemon's outbox
backlog read, the Telegram picker store): add one class per site.

No live network, no live store, no live `claude`. Stdlib ``unittest`` only.

Run:  python -m unittest discover -s seneschal/scripts -p "test_failure_rows.py"
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import clock  # noqa: E402
import failures  # noqa: E402
import identity_common  # noqa: E402
import jobs  # noqa: E402
import sentinel as sn  # noqa: E402
import stateio  # noqa: E402
import tz_common  # noqa: E402

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _rows(state_dir):
    return list(stateio.iter_jsonl(os.path.join(state_dir, failures.FAILURES_FILENAME)))


class SentinelReminderSendFailed(unittest.TestCase):
    """`sentinel.check_reminders`'s `reminder_send_failed` signal is also a failure row."""

    def setUp(self):
        # Pin the owner zone + identity so the curfew/staleness gates can't depend on the runner's zone.
        for p in (mock.patch.object(tz_common, "_zone", return_value=timezone(timedelta(hours=-5))),
                  mock.patch.object(clock, "load_identity", return_value={}),
                  mock.patch.object(identity_common, "load_identity", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self._orig = sn._deliver_reminder
        sn._deliver_reminder = lambda ch, raw, *a, **k: (
            {"ok": False, "error": "telegram 500"}, "telegram")
        self.addCleanup(setattr, sn, "_deliver_reminder", self._orig)

    def _queue(self, rid):
        sn.save_json(os.path.join(self.dir, "reminders.json"),
                     [{"id": rid, "text": "Water the plants.",
                       "due_at": NOW.isoformat().replace("+00:00", "Z")}])

    def test_a_failed_send_writes_a_failure_row(self):
        self._queue("r1")
        signals = sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        self.assertIn("reminder_send_failed", {s.get("kind") for s in signals})
        rows = _rows(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site"], "sentinel.check_reminders")
        self.assertEqual(rows[0]["kind"], "reminder_send_failed")
        self.assertIn("r1", rows[0]["detail"])

    def test_a_successful_send_writes_no_row(self):
        sn._deliver_reminder = lambda ch, raw, *a, **k: ({"ok": True}, "telegram")
        self._queue("r2")
        sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        self.assertEqual(_rows(self.dir), [])


class JobsCancelSaveFailed(unittest.TestCase):
    """`jobs.cancel_job` swallowing a `save_job` failure on its claim write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name

    def _start(self):
        return jobs.start_job(self.state, "doomed", ["python", "-c", "pass"],
                              runner=lambda argv, **k: mock.Mock(pid=1234), now=NOW)

    def test_a_save_job_raise_on_the_claim_still_cancels_and_writes_a_row(self):
        rec = self._start()
        real_save = jobs.save_job
        calls = {"n": 0}

        def flaky(state_dir, r):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_save(state_dir, r)

        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            with mock.patch.object(jobs, "save_job", side_effect=flaky):
                out = jobs.cancel_job(self.state, rec["id"], now=NOW, why="test")
        # The cancel still lands (the post-kill re-assert is the second, successful save) — losing the
        # claim's OWN write must never cost the cancel itself.
        self.assertEqual(out["status"], jobs.CANCELLED)
        rows = _rows(self.state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site"], "jobs.cancel_job")
        self.assertEqual(rows[0]["kind"], "cancel_claim_save_failed")
        self.assertIn(rec["id"], rows[0]["detail"])

    def test_an_ordinary_cancel_writes_no_row(self):
        rec = self._start()
        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            jobs.cancel_job(self.state, rec["id"], now=NOW, why="test")
        self.assertEqual(_rows(self.state), [])


if __name__ == "__main__":
    unittest.main()
