#!/usr/bin/env python3
"""Integration tests for `seneschald-control.ps1 -Action Update` — the Path A merge-detector / self-heal.

**Why this drives the real PowerShell script instead of unit-testing a Python function.** The Update
logic lives entirely in `seneschald-control.ps1`; the branch check, self-heal, claim guard, and — the thing
under test here — the *health stamp* are PowerShell, not Python. The one guarantee that matters is
behavioural: **every Update cycle must stamp `state/seneschald-health.json`** (blocked when it cannot get onto
the deploy branch) so `consecutive_blocked` climbs and the existing 3-cycle Telegram alert arms. The only
faithful way to assert that is to run the script and read the file it writes.

**Why it is Windows-gated.** `seneschald-control.ps1` is a Windows control script (`#Requires -Version 7`,
Windows scheduled tasks, `WindowsPrincipal`, and backslash-separated `Join-Path` literals that do not
resolve on POSIX). It only ever runs on the owner's Windows host, so the test runs there — where the
Python suite is run locally — and skips cleanly everywhere else (e.g. the Linux CI runner) rather than
reporting a false failure. Each case builds a throwaway git repo, fakes `origin/main` with `update-ref`
(no push → no network, no Windows long-path pack error), and invokes the *real* script.

Regression anchor: 2026-07-17, the live checkout sat off the deploy branch for ~13 min while the deploy
alarm stayed silent-green. The health stamp is what turns that into an audible block; these tests pin it.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PWSH = shutil.which("pwsh")
GIT = shutil.which("git")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _pwsh_responsive() -> bool:
    """Findable is not enough: a host-wide AMSI wedge can make every pwsh launch hang forever,
    which would hang the whole suite (observed 2026-07-18). One cached probe with a hard timeout —
    unresponsive pwsh degrades these integration tests to a skip, exactly like an absent one."""
    if not (sys.platform == "win32" and PWSH):
        return False
    try:
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", "1"],
            capture_output=True, timeout=10,
        ).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


PWSH_OK = _pwsh_responsive()


@unittest.skipUnless(
    PWSH_OK and GIT,
    "seneschald-control.ps1 is a Windows PowerShell script; this integration test needs win32 + a "
    "RESPONSIVE pwsh (an AMSI-wedged one hangs every launch) + git",
)
class SeneschaldControlUpdateTest(unittest.TestCase):
    """Drive `seneschald-control.ps1 -Action Update` through the paths that must stamp health."""

    # ------------------------------------------------------------------ helpers
    def _git(self, repo: str, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=repo, check=True, capture_output=True, text=True,
        )

    def _make_repo(self, *, with_origin_ref: bool = True, checkout_branch: str | None = None,
                   make_sessions_dir: bool = False) -> str:
        """A throwaway repo laid out like the real one: <root>/seneschal/scripts + <root>/seneschal/state,
        with the real scripts copied in so sentinel's claim check is exercised for real. `origin/main` is
        faked via update-ref so `git fetch` failing (no remote) is irrelevant — the ref still resolves."""
        repo = tempfile.mkdtemp(prefix="seneschald_test_")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        scripts_dst = os.path.join(repo, "seneschal", "scripts")
        state_dst = os.path.join(repo, "seneschal", "state")
        shutil.copytree(SCRIPTS_DIR, scripts_dst)
        os.makedirs(state_dst, exist_ok=True)
        # Never let a real secret ride a copy into a throwaway repo: with no telegram.env,
        # Send-SeneschaldAlert is a logged no-op, so a 3-cycle test can never fire a real Telegram
        # message at the owner.
        for envf in glob.glob(os.path.join(scripts_dst, "*.env")):
            os.remove(envf)
        with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as f:
            f.write("seed\n")
        self._git(repo, "init", "-b", "main")
        self._git(repo, "config", "user.email", "test@example.invalid")
        self._git(repo, "config", "user.name", "seneschald-test")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-m", "seed")
        if with_origin_ref:
            self._git(repo, "update-ref", "refs/remotes/origin/main", "main")
        if make_sessions_dir:
            os.makedirs(os.path.join(state_dst, "sessions"), exist_ok=True)
        if checkout_branch:
            self._git(repo, "checkout", "-b", checkout_branch)
        return repo

    def _run_update(self, repo: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        script = os.path.join(repo, "seneschal", "scripts", "seneschald-control.ps1")
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Action", "Update"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=180,
        )

    def _health(self, repo: str) -> dict:
        with open(os.path.join(repo, "seneschal", "state", "seneschald-health.json"), encoding="utf-8") as f:
            return json.load(f)

    def _current_branch(self, repo: str) -> str:
        out = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo, check=True, capture_output=True, text=True,
        )
        return out.stdout.strip()

    def _log(self, repo: str) -> str:
        p = os.path.join(repo, "seneschal", "state", "seneschald-update.log")
        if not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8") as f:
            return f.read()

    # -------------------------------------------------------------------- tests
    def test_off_branch_stamps_blocked_and_increments(self):
        """The 2026-07-17 shape: parked on a feature branch it must not reclaim. Every cycle must stamp a
        BLOCKED heartbeat and `consecutive_blocked` must climb — that is what arms the alert."""
        repo = self._make_repo(checkout_branch="feature/parked")  # no unique work; no session registry

        self._run_update(repo)
        h1 = self._health(repo)
        self.assertEqual(h1["status"], "blocked")
        self.assertEqual(h1["branch"], "feature/parked")
        self.assertEqual(int(h1["consecutive_blocked"]), 1)
        self.assertTrue(h1["blocked_since"], "blocked_since must be set on the first blocked cycle")
        # Reason depends only on whether the claim check could run: a live claim (fail-closed) reads
        # 'off-deploy-branch'; a claim check that could not run reads 'claim-check-failed'. Both are blocked.
        self.assertIn(h1["reason"], ("off-deploy-branch", "claim-check-failed"))

        self._run_update(repo)
        h2 = self._health(repo)
        self.assertEqual(h2["status"], "blocked")
        self.assertEqual(int(h2["consecutive_blocked"]), 2, "consecutive_blocked must climb across cycles")
        self.assertEqual(h2["blocked_since"], h1["blocked_since"], "blocked_since must carry forward")
        # The parked branch must be left exactly where it was — self-heal refused, tree untouched.
        self.assertEqual(self._current_branch(repo), "feature/parked")

    def test_fetch_unresolvable_reports_fetch_failed(self):
        """A fetch/remote problem (origin/main won't resolve) must be one honest 'fetch-failed' stamp —
        not a false 'off-deploy-branch' that blames the parked branch for commits it does not have."""
        repo = self._make_repo(with_origin_ref=False)  # nothing named origin/main resolves
        self._run_update(repo)
        h = self._health(repo)
        self.assertEqual(h["status"], "blocked")
        self.assertEqual(h["reason"], "fetch-failed")

    def test_on_deploy_branch_up_to_date_stamps_ok(self):
        """Happy path stays green: on main and up to date → status ok, counter reset, last_ok stamped."""
        repo = self._make_repo()  # on main, origin/main == HEAD
        self._run_update(repo)
        h = self._health(repo)
        self.assertEqual(h["status"], "ok")
        self.assertEqual(int(h["consecutive_blocked"]), 0)
        self.assertTrue(h["last_ok"], "last_ok must advance on a healthy cycle")

    def test_self_heal_reclaims_deploy_branch_when_free(self):
        """Parked on an abandoned branch (no unique work) with the registry present and no one on it →
        reclaim main and stamp ok. This is the self-heal working, the counterpart to the refusal above."""
        repo = self._make_repo(checkout_branch="feature/abandoned", make_sessions_dir=True)
        self._run_update(repo)
        self.assertEqual(self._current_branch(repo), "main", "an unclaimed, work-free park must be reclaimed")
        self.assertEqual(self._health(repo)["status"], "ok")

    def test_three_blocked_cycles_arm_the_alert(self):
        """The whole point of the stamp: after AlertAfterCycles (3) blocked cycles the alert path must
        engage. With no telegram.env it logs 'ALERT suppressed: no telegram.env' — that line prints only
        AFTER Send-SeneschaldAlert clears the `consecutive_blocked >= 3` gate, so it proves the alert armed."""
        repo = self._make_repo(checkout_branch="feature/parked")
        for _ in range(3):
            self._run_update(repo)
        self.assertEqual(int(self._health(repo)["consecutive_blocked"]), 3)
        self.assertIn("ALERT suppressed: no telegram.env", self._log(repo),
                      "the 3-cycle alert gate must open (suppressed only because the throwaway repo has no env)")

    def test_unexpected_error_still_stamps_blocked(self):
        """Belt-and-braces: an unexpected terminating error must NOT leave health frozen. Run with git off
        PATH so the very first `& git` throws; the catch-all must convert it to a blocked 'update-error'
        stamp rather than a silent exit that freezes seneschald-health.json at its last (ok) value."""
        repo = self._make_repo()
        # pwsh still needs to launch, so keep its own directory on PATH; git is simply absent from it.
        self._run_update(repo, extra_env={"PATH": os.path.dirname(PWSH)})
        h = self._health(repo)
        self.assertEqual(h["status"], "blocked")
        self.assertEqual(h["reason"], "update-error")


if __name__ == "__main__":
    unittest.main()
