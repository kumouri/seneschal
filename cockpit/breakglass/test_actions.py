#!/usr/bin/env python3
"""Tests for cockpit/breakglass/actions.py — the `restart` and `force-pull` break-glass actions
(cockpit-spec.md "Break-glass"). Everything runs against a `RecordingRunner` — NOTHING here ever kills
a real process, deletes a real file, or shells out to a real `git`/`uv`/`cmd`; the whole point of the
`Runner` seam is that these tests can assert the exact command sequence without touching the machine.

Run: python -m unittest cockpit.breakglass.test_actions
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.breakglass.actions import BreakglassConfig, RecordingRunner, do_force_pull, do_restart  # noqa: E402


class ActionsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name) / "repo"
        self.state_dir = Path(self._tmp.name) / "state"
        self.repo_root.mkdir(parents=True)
        self.state_dir.mkdir(parents=True)
        self.run_presence_cmd = self.repo_root / "seneschal" / "scripts" / "run-presence.cmd"
        self.cfg = BreakglassConfig(
            repo_root=self.repo_root, state_dir=self.state_dir,
            run_presence_cmd=self.run_presence_cmd, deploy_branch="develop",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _write_lock(self, pid: int) -> None:
        with open(self.cfg.lock_path, "w", encoding="utf-8") as fh:
            json.dump({"pid": pid, "started_at": "2026-07-18T00:00:00Z"}, fh)


class DoRestartTests(ActionsTestCase):
    def test_exact_command_sequence_with_a_live_pid(self):
        self._write_lock(4242)
        runner = RecordingRunner()
        result = do_restart(self.cfg, runner)
        self.assertTrue(result["ok"])
        self.assertEqual(runner.calls, [
            ("kill_pid", 4242),
            ("remove_file", self.cfg.lock_path),
            ("spawn_detached", ["cmd", "/c", str(self.run_presence_cmd)], self.repo_root),
        ])

    def test_missing_lock_file_still_clears_and_relaunches(self):
        runner = RecordingRunner()
        result = do_restart(self.cfg, runner)
        self.assertEqual(runner.calls, [
            ("remove_file", self.cfg.lock_path),
            ("spawn_detached", ["cmd", "/c", str(self.run_presence_cmd)], self.repo_root),
        ])
        self.assertIn("no pid in presence.lock", result["steps"][0])

    def test_lock_with_no_pid_field_is_tolerant(self):
        with open(self.cfg.lock_path, "w", encoding="utf-8") as fh:
            json.dump({"started_at": "now"}, fh)
        runner = RecordingRunner()
        do_restart(self.cfg, runner)
        self.assertNotIn(("kill_pid", None), runner.calls)

    def test_corrupt_lock_file_is_tolerant(self):
        self.cfg.lock_path.write_text("not json", encoding="utf-8")
        runner = RecordingRunner()
        result = do_restart(self.cfg, runner)
        self.assertTrue(result["ok"])
        self.assertEqual(runner.calls[0][0], "remove_file")


class DoForcePullTests(ActionsTestCase):
    def test_exact_command_sequence(self):
        self._write_lock(99)
        runner = RecordingRunner()
        result = do_force_pull(self.cfg, runner)
        self.assertTrue(result["ok"])
        self.assertEqual(runner.calls, [
            ("run", ["git", "-c", "core.fsmonitor=false", "fetch", "origin"], self.repo_root),
            ("run", ["git", "-c", "core.fsmonitor=false", "reset", "--hard", "origin/develop"], self.repo_root),
            ("run", ["uv", "sync", "--frozen"], self.repo_root),
            ("kill_pid", 99),
            ("remove_file", self.cfg.lock_path),
            ("spawn_detached", ["cmd", "/c", str(self.run_presence_cmd)], self.repo_root),
        ])

    def test_uses_configured_deploy_branch(self):
        cfg = BreakglassConfig(repo_root=self.repo_root, state_dir=self.state_dir,
                               run_presence_cmd=self.run_presence_cmd, deploy_branch="master")
        runner = RecordingRunner()
        do_force_pull(cfg, runner)
        reset_call = runner.calls[1]
        self.assertIn("origin/master", reset_call[1])

    def test_uv_sync_failure_is_best_effort_and_does_not_block_restart(self):
        runner = RecordingRunner(run_returncode=1)
        result = do_force_pull(self.cfg, runner)
        # uv sync failing (returncode 1) still lets the restart steps run afterward.
        step_names = [c[0] for c in runner.calls]
        self.assertEqual(step_names, ["run", "run", "run", "remove_file", "spawn_detached"])
        self.assertIn("best-effort", result["steps"][2])

    def test_fetch_failure_marks_not_ok_but_still_restarts(self):
        runner = RecordingRunner(run_returncode=1)
        result = do_force_pull(self.cfg, runner)
        self.assertFalse(result["ok"])
        # the restart steps still ran (see the recorded calls above) — force-pull never leaves the
        # daemon down just because the pull itself failed.
        self.assertTrue(any(c[0] == "spawn_detached" for c in runner.calls))


if __name__ == "__main__":
    unittest.main()
