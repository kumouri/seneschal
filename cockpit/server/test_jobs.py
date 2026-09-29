#!/usr/bin/env python3
"""Tests for cockpit/server/jobs.py — the read layer behind the cockpit's Jobs panel over
`state/jobs/` (seneschal/docs/background-jobs-spec.md). Pure stdlib, no FastAPI needed, so this runs
unconditionally (mirrors test_health.py / test_governor.py).

Fixtures are written as plain dicts rather than by calling `seneschal/scripts/jobs.py`, deliberately:
this reader is NOT allowed to import that module (cockpit-spec.md — the cockpit is its own
dependency world, and the backend must keep reading a state dir written by a daemon on a different
commit). Testing against a hand-written on-disk shape is what proves that independence, and it's how
the older/newer-record tolerance below gets exercised at all.

Run: python -m unittest cockpit.server.test_jobs
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import jobs  # noqa: E402

NOW = datetime(2026, 7, 29, 3, 0, 0, tzinfo=timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class JobsReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def _write(self, job_id: str, *, log: str | None = None, **fields) -> dict:
        d = self.state / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            "schema": "seneschal.job/1",
            "id": job_id,
            "title": f"job {job_id}",
            "argv": ["python", "-c", "pass"],
            "status": "running",
            "pid": 1234,
            "created_at": _stamp(NOW - timedelta(minutes=5)),
            "started_at": _stamp(NOW - timedelta(minutes=5)),
            "ended_at": None,
            "exit_code": None,
            "deadline_sec": 21600,
            "lease": False,
            "wake": False,
            "notify": {"channel": "telegram"},
            "notified_at": None,
            "log_path": str(d / f"{job_id}.log"),
        }
        rec.update(fields)
        (d / f"{job_id}.json").write_text(json.dumps(rec), encoding="utf-8")
        if log is not None:
            (d / f"{job_id}.log").write_text(log, encoding="utf-8")
        return rec

    # ---------------------------------------------------------------- availability

    def test_missing_store_is_unavailable_not_empty(self):
        """'I can't see jobs at all' and 'nothing has run' are different facts and the panel says so
        differently — so the payload must distinguish them."""
        out = jobs.read_jobs(self.state)
        self.assertFalse(out["available"])
        self.assertEqual(out["active"], [])

    def test_present_but_empty_store_is_available(self):
        (self.state / "jobs").mkdir()
        out = jobs.read_jobs(self.state)
        self.assertTrue(out["available"])
        self.assertEqual(out["active"], [])
        self.assertEqual(out["recent"], [])

    # ---------------------------------------------------------------- shaping

    def test_active_and_recent_are_split_and_ordered(self):
        self._write("20260729-020000-aaaa")
        self._write("20260729-020100-bbbb", status="done", exit_code=0,
                    ended_at=_stamp(NOW - timedelta(minutes=2)), notified_at=_stamp(NOW))
        self._write("20260729-020200-cccc", status="failed", exit_code=3,
                    ended_at=_stamp(NOW - timedelta(minutes=1)), notified_at=_stamp(NOW))
        out = jobs.read_jobs(self.state)
        self.assertEqual([j["id"] for j in out["active"]], ["20260729-020000-aaaa"])
        # newest finished first
        self.assertEqual([j["id"] for j in out["recent"]],
                         ["20260729-020200-cccc", "20260729-020100-bbbb"])
        self.assertEqual(out["active_count"], 1)
        self.assertEqual(out["counts"], {"running": 1, "done": 1, "failed": 1})

    def test_running_jobs_are_never_truncated_by_limit(self):
        """A fleet of running jobs must not push its own members out of view; `limit` bounds only the
        finished tail."""
        for i in range(5):
            self._write(f"20260729-0200{i:02d}-run{i}")
        for i in range(5):
            self._write(f"20260729-0300{i:02d}-fin{i}", status="done", exit_code=0,
                        ended_at=_stamp(NOW), notified_at=_stamp(NOW))
        out = jobs.read_jobs(self.state, limit=2)
        self.assertEqual(len(out["active"]), 5)
        self.assertEqual(len(out["recent"]), 2)

    def test_awaiting_push_counts_only_terminal_jobs_without_a_landed_ping(self):
        """The one alarming number: jobs.py stamps notified_at ONLY on a send that landed, so a
        terminal job without one means the daemon is failing to reach the owner. A running job with no
        notified_at is simply normal and must not inflate this."""
        self._write("20260729-020000-runn")                                     # running, no ping
        self._write("20260729-020100-told", status="done", exit_code=0,
                    ended_at=_stamp(NOW), notified_at=_stamp(NOW))              # owner told
        self._write("20260729-020200-quiet", status="failed", exit_code=1,
                    ended_at=_stamp(NOW), notified_at=None)                     # owner NOT told
        out = jobs.read_jobs(self.state)
        self.assertEqual(out["awaiting_push"], 1)

    def test_lease_held_reflects_running_lease_jobs_only(self):
        self._write("20260729-020000-lease", lease=True, status="done",
                    ended_at=_stamp(NOW), notified_at=_stamp(NOW))
        self.assertFalse(jobs.read_jobs(self.state)["lease_held"])
        self._write("20260729-020100-live", lease=True)
        self.assertTrue(jobs.read_jobs(self.state)["lease_held"])

    def test_duration_is_elapsed_for_running_and_total_for_finished(self):
        self._write("20260729-020000-live")
        self._write("20260729-020100-over", status="done", exit_code=0,
                    started_at=_stamp(NOW - timedelta(seconds=90)),
                    ended_at=_stamp(NOW - timedelta(seconds=30)), notified_at=_stamp(NOW))
        out = jobs.read_jobs(self.state, now=NOW)
        self.assertAlmostEqual(out["active"][0]["duration_sec"], 300.0, places=0)
        self.assertAlmostEqual(out["recent"][0]["duration_sec"], 60.0, places=0)

    # ---------------------------------------------------------------- tolerance

    def test_corrupt_and_stray_files_are_skipped_not_fatal(self):
        self._write("20260729-020000-good")
        d = self.state / "jobs"
        (d / "broken.json").write_text("{not json", encoding="utf-8")
        (d / "notajob.json").write_text('{"hello": "world"}', encoding="utf-8")
        (d / "stray.txt").write_text("ignore me", encoding="utf-8")
        out = jobs.read_jobs(self.state)
        self.assertTrue(out["available"])
        self.assertEqual(len(out["active"]), 1)

    def test_an_unknown_status_from_a_newer_daemon_passes_through(self):
        """The backend and the daemon can be on different commits; an unrecognized status must not be
        coerced into something wrong, nor blank the panel."""
        self._write("20260729-020000-weird", status="quarantined",
                    ended_at=_stamp(NOW), notified_at=_stamp(NOW))
        out = jobs.read_jobs(self.state)
        job = (out["active"] + out["recent"])[0]
        self.assertEqual(job["status"], "quarantined")
        self.assertFalse(job["is_running"])
        self.assertFalse(job["is_terminal"])
        # ...and an unknown status is NOT counted as an undelivered ping (is_terminal is False)
        self.assertEqual(out["awaiting_push"], 0)

    def test_a_record_missing_optional_fields_still_renders(self):
        d = self.state / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        (d / "20260729-020000-bare.json").write_text(
            json.dumps({"id": "20260729-020000-bare", "status": "running"}), encoding="utf-8")
        out = jobs.read_jobs(self.state)
        job = out["active"][0]
        self.assertEqual(job["title"], "20260729-020000-bare")  # falls back to the id
        self.assertIsNone(job["duration_sec"])                  # unreadable, omitted not faked
        self.assertEqual(job["log_tail"], [])

    def test_a_garbled_timestamp_yields_no_duration_rather_than_a_wrong_one(self):
        self._write("20260729-020000-bad", started_at="not-a-time", created_at="also-not")
        self.assertIsNone(jobs.read_jobs(self.state)["active"][0]["duration_sec"])

    # ---------------------------------------------------------------- logs

    def test_log_tail_returns_the_last_non_empty_lines(self):
        self._write("20260729-020000-logged", log="one\n\ntwo\nthree\n\n")
        out = jobs.read_jobs(self.state, tail_lines=2)
        self.assertEqual(out["active"][0]["log_tail"], ["two", "three"])

    def test_log_tail_of_a_missing_log_is_empty(self):
        self._write("20260729-020000-nolog")
        self.assertEqual(jobs.read_jobs(self.state)["active"][0]["log_tail"], [])

    def test_a_huge_log_is_tailed_not_slurped(self):
        big = "\n".join(f"line {i}" for i in range(200_000))
        self._write("20260729-020000-huge", log=big)
        tail = jobs.read_jobs(self.state, tail_lines=3)["active"][0]["log_tail"]
        self.assertEqual(len(tail), 3)
        self.assertEqual(tail[-1], "line 199999")

    # ---------------------------------------------------------------- single job / path safety

    def test_read_job_returns_a_longer_tail(self):
        self._write("20260729-020000-one", log="\n".join(f"l{i}" for i in range(100)))
        out = jobs.read_job(self.state, "20260729-020000-one", tail_lines=10)
        self.assertTrue(out["available"])
        self.assertEqual(len(out["job"]["log_tail"]), 10)

    def test_read_job_rejects_traversal_and_unknown_ids(self):
        """`job_id` arrives as a URL path segment, so this is a security boundary, not tidiness."""
        for bad in ("../../etc/passwd", "..", "a/b", "a\\b", "", "x" * 200, "with space", "a.json"):
            with self.subTest(bad=bad):
                out = jobs.read_job(self.state, bad)
                self.assertFalse(out["available"])
                self.assertIsNone(out["job"])
        self.assertFalse(jobs.read_job(self.state, "20260729-999999-nope")["available"])

    def test_limit_and_tail_params_survive_garbage(self):
        self._write("20260729-020000-ok", log="hello")
        for bad in (None, "many", -5, 0):
            with self.subTest(bad=bad):
                out = jobs.read_jobs(self.state, limit=bad, tail_lines=bad)
                self.assertTrue(out["available"])


if __name__ == "__main__":
    unittest.main()
