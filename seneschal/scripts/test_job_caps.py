#!/usr/bin/env python3
"""Tests for `job_caps.py` and `jobs.py`'s reading of it — the cap VALUES are the owner's to set.

What is pinned, each a separate failure if it goes:
  * **validate rejects, never clamps** — a stored 50 that read back as 12 would be a setting that lies;
  * **a bad or missing file is the defaults, never "no cap"**, and says so once, not on every check;
  * **`jobs.py` reads the stored values fresh at every check** — a value saved from the cockpit applies to
    the very next `start`, with no restart and no cache;
  * **the two caps stay distinct**: the soft one still yields to `--over-soft-cap`, the hard one still
    has no flag, whatever their numbers are;
  * **lowering a cap below the live count cancels nothing** — it only refuses the next start;
  * **the write is atomic** (`stateio`), and a rejected pair writes nothing.

Run: python -m unittest seneschal.scripts.test_job_caps   (or)   python test_job_caps.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import job_caps  # noqa: E402
import jobs  # noqa: E402
from test_jobs import NOW  # noqa: E402


class ValidateTests(unittest.TestCase):
    def test_the_defaults_validate(self):
        self.assertEqual(job_caps.validate(3, 5), (3, 5))

    def test_soft_equal_to_hard_is_allowed(self):
        self.assertEqual(job_caps.validate(4, 4), (4, 4))

    def test_rejections_never_clamp(self):
        for soft, hard, words in ((0, 5, "at least 1"), (4, 3, "at least the soft cap"),
                                  (3, job_caps.HARD_CEILING + 1, "at most"),
                                  ("3", 5, "whole number"), (3, 5.0, "whole number"),
                                  (True, 5, "whole number"), (None, 5, "whole number")):
            with self.subTest(soft=soft, hard=hard):
                with self.assertRaises(job_caps.CapsError) as cm:
                    job_caps.validate(soft, hard)
                self.assertIn(words, str(cm.exception))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name
        job_caps._warned.clear()
        self.addCleanup(job_caps._warned.clear)

    def _write_raw(self, text):
        with open(job_caps.caps_path(self.state), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_file_is_the_defaults_silently(self):
        err = io.StringIO()
        caps = job_caps.load(self.state, stream=err)
        self.assertEqual((caps["soft_per_session"], caps["hard_global"], caps["source"]),
                         (3, 5, "default"))
        self.assertEqual(err.getvalue(), "")

    def test_save_then_load_round_trips_and_stamps_who(self):
        saved = job_caps.save(self.state, 2, 7, updated_by="cockpit")
        loaded = job_caps.load(self.state)
        self.assertEqual(saved, loaded)
        self.assertEqual((loaded["soft_per_session"], loaded["hard_global"]), (2, 7))
        self.assertEqual(loaded["updated_by"], "cockpit")
        self.assertEqual(loaded["source"], "file")
        with open(job_caps.caps_path(self.state), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["schema"], job_caps.SCHEMA)

    def test_save_goes_through_the_atomic_writer(self):
        with mock.patch.object(job_caps.stateio, "write_json_atomic") as w:
            job_caps.save(self.state, 3, 5, updated_by="cli")
        self.assertEqual(w.call_args.args[0], job_caps.caps_path(self.state))

    def test_a_rejected_pair_writes_nothing(self):
        job_caps.save(self.state, 3, 5, updated_by="cli")
        with self.assertRaises(job_caps.CapsError):
            job_caps.save(self.state, 6, 4, updated_by="cockpit")
        self.assertEqual(job_caps.load(self.state)["hard_global"], 5)

    def test_an_unknown_updater_is_refused(self):
        with self.assertRaises(job_caps.CapsError):
            job_caps.save(self.state, 3, 5, updated_by="assistant")
        self.assertFalse(os.path.exists(job_caps.caps_path(self.state)))

    def test_corrupt_and_invalid_files_fall_back_to_the_defaults_and_warn_once(self):
        for text in ("{not json", "[3, 5]", json.dumps({"soft_per_session": 9, "hard_global": 4})):
            with self.subTest(text=text):
                job_caps._warned.clear()
                self._write_raw(text)
                err = io.StringIO()
                first = job_caps.load(self.state, stream=err)
                second = job_caps.load(self.state, stream=err)
                self.assertEqual((first["soft_per_session"], first["hard_global"]), (3, 5))
                self.assertEqual(first["source"], "default")
                self.assertIn("error", first)
                self.assertEqual(first, second)
                self.assertEqual(err.getvalue().count("job caps:"), 1)  # once, not per check
                self.assertIn("using the defaults", err.getvalue())


class JobsReadsTheStoreTests(unittest.TestCase):
    """The gate in `jobs.py`, driven by stored values rather than its old constants."""

    SESSION = "sess-a"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name
        os.makedirs(jobs.jobs_dir(self.state), exist_ok=True)
        alive = mock.patch.object(jobs, "pid_alive", return_value=True)
        alive.start()
        self.addCleanup(alive.stop)
        job_caps._warned.clear()
        self.addCleanup(job_caps._warned.clear)

    def _running(self, n, session=SESSION, at=NOW):
        for i in range(n):
            jid = f"20260728-19{i:02d}00-{session[-1]}{i:03d}"
            started = jobs._stamp(at - timedelta(seconds=60 + i))
            jobs.save_job(self.state, {
                "schema": jobs.SCHEMA, "id": jid, "title": f"job {i}",
                "argv": ["claude", "-p", "x"], "cwd": self.state,
                "log_path": jobs.log_path(self.state, jid), "status": jobs.RUNNING,
                "pid": 4321, "child_pid": None, "created_at": started, "started_at": started,
                "ended_at": None, "exit_code": None, "deadline_sec": 3600,
                "origin": {"session_id": session}, "attempts": []})

    def test_the_stored_hard_cap_replaces_the_constant(self):
        self._running(5, session="sess-b")
        self.assertIsNotNone(jobs.hard_cap_refusal(self.state, now=NOW))   # default 5: full
        job_caps.save(self.state, 3, 6, updated_by="cockpit")
        self.assertIsNone(jobs.hard_cap_refusal(self.state, now=NOW))      # read fresh: room now

    def test_the_hard_refusal_names_the_owners_setting_and_still_offers_no_flag(self):
        self._running(4, session="sess-b")
        job_caps.save(self.state, 2, 4, updated_by="cockpit")
        msg = jobs.hard_cap_refusal(self.state, now=NOW)
        self.assertIn("the hard cap is 4", msg)
        self.assertIn("the owner's setting, via cockpit", msg)
        self.assertNotIn("the shipped default", msg)   # never misattributes a changed number
        self.assertIn("There is no flag for this one", msg)

    def test_the_stored_soft_cap_replaces_the_constant_and_still_yields_to_the_flag(self):
        self._running(2)
        self.assertIsNone(jobs.soft_cap_refusal(self.state, self.SESSION, now=NOW))
        job_caps.save(self.state, 2, 5, updated_by="cockpit")
        msg = jobs.soft_cap_refusal(self.state, self.SESSION, now=NOW)
        self.assertIn("the soft cap is 2", msg)
        self.assertIn("--over-soft-cap", msg)
        self.assertIn("Global: 2 of 5", msg)
        self.assertEqual(jobs.over_soft_cap_note(self.state, self.SESSION, now=NOW)["cap"], 2)

    def test_lowering_below_the_live_count_cancels_nothing(self):
        self._running(4, session="sess-b")
        before = {r["id"]: r["status"] for r in jobs.list_jobs(self.state)}
        job_caps.save(self.state, 1, 2, updated_by="cockpit")
        self.assertIsNotNone(jobs.hard_cap_refusal(self.state, now=NOW))  # new starts refused...
        after = {r["id"]: r["status"] for r in jobs.list_jobs(self.state)}
        self.assertEqual(before, after)                                    # ...nothing touched
        self.assertTrue(all(s == jobs.RUNNING for s in after.values()))

    def test_an_invalid_store_enforces_the_defaults_never_no_cap(self):
        self._running(5, session="sess-b")
        with open(job_caps.caps_path(self.state), "w", encoding="utf-8") as fh:
            json.dump({"soft_per_session": 3, "hard_global": 99}, fh)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIn("the hard cap is 5", jobs.hard_cap_refusal(self.state, now=NOW))

    def test_the_start_cli_refuses_at_the_stored_hard_cap(self):
        # The CLI counts against the REAL clock, so these records are started just now.
        self._running(2, session="sess-b", at=jobs._utc_now())
        job_caps.save(self.state, 1, 2, updated_by="cockpit")
        err = io.StringIO()

        def never(*a, **kw):  # a refusal that fails must fail the test, never spawn a real job
            raise AssertionError("start_job reached — the cap did not refuse")
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(jobs, "start_job", never):
            code = jobs.main(["--state-dir", self.state, "start", "--title", "x",
                              "--origin-session", "sess-c", "--", "claude", "-p", "x"])
        self.assertEqual(code, 2)
        self.assertIn("the hard cap is 2", err.getvalue())
        self.assertEqual(len(jobs.list_jobs(self.state)), 2)  # nothing written for the refusal

    def test_there_is_still_no_hard_cap_override_flag(self):
        """The rule moved from "the number is fixed" to "the number is the owner's"; it did NOT move to "a
        job can talk past it". `start` has exactly one cap flag, and it is the soft one."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            jobs.main(["start", "--help"])
        flags = [w for w in out.getvalue().split() if w.startswith("--") and "cap" in w]
        self.assertEqual(sorted(set(flags)), ["--over-soft-cap"])


class CapsCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name
        job_caps._warned.clear()
        self.addCleanup(job_caps._warned.clear)

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = jobs.main(["--state-dir", self.state, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_show_defaults(self):
        code, out, _ = self._run("caps", "--session", "")
        self.assertEqual(code, 0)
        self.assertIn("soft 3 per session · hard 5 total (defaults)", out)

    def test_set_one_side_keeps_the_other_and_is_the_same_writer(self):
        code, out, _ = self._run("caps", "set", "--hard", "8")
        self.assertEqual(code, 0)
        caps = job_caps.load(self.state)
        self.assertEqual((caps["soft_per_session"], caps["hard_global"], caps["updated_by"]),
                         (3, 8, "cli"))

    def test_set_rejects_with_exit_2_and_writes_nothing(self):
        code, _, err = self._run("caps", "set", "--soft", "6", "--hard", "4")
        self.assertEqual(code, 2)
        self.assertIn("refusing", err)
        self.assertFalse(os.path.exists(job_caps.caps_path(self.state)))

    def test_set_with_nothing_to_set_is_refused(self):
        self.assertEqual(self._run("caps", "set")[0], 2)

    def test_json_view_carries_caps_limits_and_live_counts(self):
        code, out, _ = self._run("caps", "--json", "--session", "")
        view = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(view["caps"]["hard_global"], 5)
        self.assertEqual(view["limits"]["hard_ceiling"], job_caps.HARD_CEILING)
        self.assertEqual((view["global"], view["session"], view["uncounted"]), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
