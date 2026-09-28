#!/usr/bin/env python3
"""Tests for the /setup wizard's worktree guard (setup_checkout.py) and its enforcement
in render_units.py --apply.

Builds real throwaway git repos (``git init`` + ``git worktree add``) in a temp dir, so
the detection is exercised against git itself, not a mock. Stdlib ``unittest`` only.
Run:  python -m unittest seneschal.scripts.test_setup_checkout
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import render_units as ru  # noqa: E402
import setup_checkout as sc  # noqa: E402
import setup_state as ss  # noqa: E402

REAL_REPO = Path(SCRIPT_DIR).resolve().parents[1]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "init.defaultBranch=main", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


@unittest.skipUnless(shutil.which("git"), "git not on PATH")
class Detect(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.plain = base / "plain"
        self.plain.mkdir()
        self.main = base / "main"
        self.main.mkdir()
        _git(self.main, "init")
        _git(self.main, "commit", "--allow-empty", "-m", "root")
        self.wt = self.main / ".claude" / "worktrees" / "setup-x"
        _git(self.main, "worktree", "add", "-b", "claude/setup-x", str(self.wt))

    def cli(self, root: Path, cmd: str) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sc._main(["setup_checkout.py", "--root", str(root), cmd])
        return rc, json.loads(out.getvalue()), err.getvalue()

    def test_not_a_git_checkout_is_not_a_worktree(self):
        v = sc.detect(self.plain)
        self.assertFalse(v["git"])
        self.assertFalse(v["worktree"])

    def test_main_checkout_is_not_a_worktree(self):
        v = sc.detect(self.main)
        self.assertTrue(v["git"])
        self.assertFalse(v["worktree"])
        self.assertEqual(Path(v["main_checkout"]), self.main)

    def test_linked_worktree_detected_with_its_main_checkout(self):
        v = sc.detect(self.wt)
        self.assertTrue(v["worktree"])
        self.assertEqual(Path(v["path"]), self.wt)
        self.assertEqual(Path(v["main_checkout"]), self.main)
        self.assertIn(str(self.wt), sc.refusal(v))
        self.assertIn("use this worktree", sc.refusal(v))

    def test_check_records_an_unaccepted_verdict_with_the_message(self):
        rc, out, _ = self.cli(self.wt, "check")
        self.assertEqual(rc, 0)
        self.assertTrue(out["worktree"])
        self.assertFalse(out["accepted"])
        self.assertIn("Run /setup from the main checkout", out["message"])
        rec = ss.load(self.wt / "seneschal" / "state" / "setup-state.json")["checkout"]
        self.assertEqual(rec["worktree"], True)
        self.assertFalse(rec["accepted"])

    def test_require_refuses_until_accepted_then_passes(self):
        rc, _, err = self.cli(self.wt, "require")
        self.assertEqual(rc, sc.EXIT_UNACCEPTED_WORKTREE)
        self.assertIn("git worktree", err)
        rc, out, _ = self.cli(self.wt, "accept")
        self.assertEqual(rc, 0)
        self.assertTrue(out["accepted"])
        rc, _, _ = self.cli(self.wt, "require")
        self.assertEqual(rc, 0)
        # a later plain `check` keeps the acceptance (sticky per path)
        rc, out, _ = self.cli(self.wt, "check")
        self.assertTrue(out["accepted"])

    def test_acceptance_is_bound_to_the_accepted_path(self):
        state = ss.fresh()
        sc.record(state, sc.detect(self.wt), accept=True)
        moved = dict(sc.detect(self.wt), path=str(self.main / "elsewhere"))
        self.assertFalse(sc.accepted(state, moved))

    def test_main_checkout_always_passes_require(self):
        rc, out, _ = self.cli(self.main, "require")
        self.assertEqual(rc, 0)
        self.assertTrue(out["accepted"])

    def test_render_units_apply_refuses_an_unaccepted_worktree(self):
        scripts = self.wt / "seneschal" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("run-presence.cmd", "run-presence.sh"):
            shutil.copy(REAL_REPO / "seneschal" / "scripts" / name, scripts / name)
        argv = ["render_units.py", "--repo", str(self.wt), "--platform", "linux"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = ru._main(argv + ["--apply"])
        self.assertEqual(rc, sc.EXIT_UNACCEPTED_WORKTREE)
        self.assertFalse((self.wt / "seneschal" / "state" / "setup").exists())
        # dry-run still renders (with a warning), so the owner can see what WOULD be pinned
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ru._main(argv + ["--dry-run"]), 0)
        self.assertIn("warning:", out.getvalue())
        # once accepted, --apply writes
        with contextlib.redirect_stdout(io.StringIO()):
            sc._main(["setup_checkout.py", "--root", str(self.wt), "accept"])
            self.assertEqual(ru._main(argv + ["--apply"]), 0)
        self.assertTrue((self.wt / "seneschal" / "state" / "setup").is_dir())


if __name__ == "__main__":
    unittest.main()
