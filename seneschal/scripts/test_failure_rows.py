#!/usr/bin/env python3
"""The `failures.jsonl` breadcrumb lands ON THE FAILURE PATH, not merely "the code didn't crash".

One class per instrumented site. `failures.record` is called from inside an already-degraded
``except`` branch, so the only way to know a site is wired is to force that branch and read the row
back. Sites covered here:

  * `jobs.cancel_job` — a `save_job` claim write that raises (the cancel itself must still land)

The same shape extends to the other fail-open sites as their modules land (the sentinel's reminder
send, the daemon's outbox backlog read, the Telegram picker store): add one class per site.

No live network, no live store, no live `claude`. Stdlib ``unittest`` only.

Run:  python -m unittest discover -s seneschal/scripts -p "test_failure_rows.py"
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import failures  # noqa: E402
import jobs  # noqa: E402
import stateio  # noqa: E402

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _rows(state_dir):
    return list(stateio.iter_jsonl(os.path.join(state_dir, failures.FAILURES_FILENAME)))


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
