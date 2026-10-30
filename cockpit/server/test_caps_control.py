#!/usr/bin/env python3
"""Tests for cockpit/server/caps_control.py — the Jobs panel's caps control. Pure stdlib, no FastAPI needed, so this runs
unconditionally; the route-level tests (auth, CSRF, 400s) live in test_app.py.

**What this pins:** the panel writes through the SAME `save` in `seneschal/scripts/job_caps.py` the CLI uses (so
the two can never disagree about what is valid or where it lands); the live count is `jobs.py caps
--json`'s own number, degrading to `null` — never a guess — when it cannot be had; and loading the
writer by file path does not put `seneschal/scripts/` on `sys.path`, where its `jobs`/`governor` would
shadow this package's own.

Run: python -m unittest cockpit.server.test_caps_control
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import caps_control  # noqa: E402


class CapsControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        caps_control.clear_cache()
        self.addCleanup(caps_control.clear_cache)
        patcher = mock.patch.object(caps_control, "counter",
                                    lambda state_dir: {"global": 2, "uncounted": 1})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults_with_limits_and_the_live_count(self):
        out = caps_control.read_caps(self.state)
        self.assertTrue(out["available"])
        self.assertEqual((out["soft_per_session"], out["hard_global"], out["source"]),
                         (3, 5, "default"))
        self.assertEqual(out["limits"]["hard_ceiling"], 12)
        self.assertEqual((out["in_flight"], out["uncounted"], out["count_error"]), (2, 1, None))

    def test_save_uses_the_shared_writer_and_stamps_cockpit(self):
        out = caps_control.save_caps(self.state, 2, 7)
        self.assertEqual((out["soft_per_session"], out["hard_global"], out["updated_by"]),
                         (2, 7, "cockpit"))
        on_disk = json.loads((self.state / "job-caps.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["schema"], "seneschal.job-caps/1")
        # The CLI's own loader reads what the panel wrote — one store, one shape.
        self.assertEqual(caps_control._job_caps().load(str(self.state))["hard_global"], 7)

    def test_an_invalid_pair_raises_value_error_and_writes_nothing(self):
        for soft, hard in ((0, 5), (5, 4), (3, 13)):
            with self.subTest(soft=soft, hard=hard):
                with self.assertRaises(ValueError):
                    caps_control.save_caps(self.state, soft, hard)
                self.assertFalse((self.state / "job-caps.json").exists())

    def test_a_failed_count_is_null_with_a_reason_never_a_guess(self):
        def boom(state_dir):
            raise RuntimeError("jobs.py exploded")
        with mock.patch.object(caps_control, "counter", boom):
            caps_control.clear_cache()
            out = caps_control.read_caps(self.state)
        self.assertIsNone(out["in_flight"])
        self.assertIn("exploded", out["count_error"])
        self.assertEqual(out["hard_global"], 5)  # the caps still render

    def test_the_count_is_cached_briefly_and_a_save_invalidates_it(self):
        calls = []

        def counting(state_dir):
            calls.append(1)
            return {"global": len(calls), "uncounted": 0}
        with mock.patch.object(caps_control, "counter", counting):
            caps_control.clear_cache()
            caps_control.read_caps(self.state)
            caps_control.read_caps(self.state)
            self.assertEqual(len(calls), 1)
            caps_control.save_caps(self.state, 3, 6)
            self.assertEqual(len(calls), 2)

    def test_loading_the_writer_never_puts_the_scripts_dir_on_sys_path(self):
        before = list(sys.path)
        caps_control._job_caps()
        added = [p for p in sys.path if p not in before]
        self.assertNotIn(str(caps_control.SCRIPTS_DIR), added)

    def test_the_real_counter_runs_jobs_py_against_the_state_dir(self):
        """One real subprocess, so the CLI contract the panel depends on (`caps --json` and its
        `global` key) is exercised rather than assumed."""
        view = caps_control._default_counter(self.state)
        self.assertEqual(view["global"], 0)
        self.assertEqual(view["caps"]["hard_global"], 5)


if __name__ == "__main__":
    unittest.main()
