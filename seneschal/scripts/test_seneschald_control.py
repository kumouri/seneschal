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

Regression anchor: a live checkout that sat off the deploy branch while the deploy alarm stayed
silent-green. The health stamp is what turns that into an audible block; these tests pin it.

**Nothing here can reach a real daemon or scheduled task.** Every throwaway repo carries its own state
dir with the deliberate-stop sentinel (`seneschald-stopped`) already dropped, so the presence-down arm's
auto-revive is always vetoed (`deliberately-stopped`) and never calls `Start-ScheduledTask` on the
host's real `seneschald` task. `request_control.py` resolves its default state dir from its own (copied)
location, so a queued restart lands in the throwaway repo too.
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
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tmproot  # noqa: E402  — rmtree that clears git's read-only objects (see below)

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
        # Not `shutil.rmtree(..., ignore_errors=True)`: git writes its objects read-only, which that
        # silently skips on Windows, so every `seneschald_test_*/.git` used to leak into Temp.
        # `tmproot.rmtree` clears the bit and retries.
        self.addCleanup(tmproot.rmtree, repo)
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
        self._veto_revive(repo)
        return repo

    def _veto_revive(self, repo: str) -> None:
        """Drop the deliberate-stop sentinel (untracked: the state dir is written AFTER the seed commit
        in `_make_repo`, and after the clone in `_make_repo_with_real_remote`). With it present,
        seneschald_revive.py answers `deliberately-stopped`, so a test with no live lock can never start
        the host's real `seneschald` scheduled task."""
        state = os.path.join(repo, "seneschal", "state")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "seneschald-stopped"), "w", encoding="utf-8"):
            pass

    def _make_repo_with_real_remote(self) -> tuple[str, str]:
        """Like `_make_repo`, but `origin` is a REAL second repo, so `git pull --ff-only origin main`
        actually runs a fetch+merge instead of being a same-commit no-op.

        Every other test's `origin/main` is faked in place via `update-ref` (see `_make_repo`'s own
        docstring), which means local always reads up to date and the real "branch advanced -> pull
        succeeds -> request the restarts" path in Update-Seneschald would never execute in this suite.
        That is exactly the path `Request-HealthListenerRestart` lives on, so it needs a fixture that can
        actually exercise a pull.

        `origin` is BARE (`git init --bare`) rather than a normal repo: a non-bare repo's `main`
        checkout refuses an incoming push by default (`receive.denyCurrentBranch`), which is exactly
        what "make origin advance" needs to do below. A bare repo has no checkout to protect.
        """
        origin = tempfile.mkdtemp(prefix="seneschald_origin_")
        self.addCleanup(tmproot.rmtree, origin)
        self._git(origin, "init", "--bare", "-b", "main")

        repo = tempfile.mkdtemp(prefix="seneschald_test_")
        self.addCleanup(tmproot.rmtree, repo)
        # Seed origin's history through a throwaway clone — a bare repo has no working tree to
        # write `git add`/`commit` against directly.
        seed = tempfile.mkdtemp(prefix="seneschald_seed_")
        self.addCleanup(tmproot.rmtree, seed)
        subprocess.run(["git", "-c", "core.fsmonitor=false", "clone", origin, seed],
                       check=True, capture_output=True, text=True)
        self._git(seed, "config", "user.email", "test@example.invalid")
        self._git(seed, "config", "user.name", "seneschald-test")
        with open(os.path.join(seed, "README.md"), "w", encoding="utf-8") as f:
            f.write("seed\n")
        scripts_dst = os.path.join(seed, "seneschal", "scripts")
        shutil.copytree(SCRIPTS_DIR, scripts_dst)
        for envf in glob.glob(os.path.join(scripts_dst, "*.env")):
            os.remove(envf)
        self._git(seed, "add", "-A")
        self._git(seed, "commit", "-m", "seed + add seneschal/scripts")
        self._git(seed, "push", "origin", "main")

        subprocess.run(["git", "-c", "core.fsmonitor=false", "clone", origin, repo],
                       check=True, capture_output=True, text=True)
        self._git(repo, "config", "user.email", "test@example.invalid")
        self._git(repo, "config", "user.name", "seneschald-test")
        self._veto_revive(repo)

        # Now make origin advance without `repo` knowing yet — the shape a real merge leaves behind.
        # Through `seed` again, never through `repo`: `repo` must stay exactly at its clone point so
        # Update-Seneschald's own `git pull --ff-only` is what advances it, not a second local commit.
        with open(os.path.join(seed, "README.md"), "w", encoding="utf-8") as f:
            f.write("seed\nmerged change\n")
        self._git(seed, "add", "-A")
        self._git(seed, "commit", "-m", "a merge landed on main")
        self._git(seed, "push", "origin", "main")
        return repo, origin

    def _health_listener_control(self, repo: str) -> dict | None:
        path = os.path.join(repo, "seneschal", "state", "health-listener-control.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)

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

    def _fake_claude_config(self, repo: str, account: str = "acct-1", org: str = "org-1",
                            email: str = "owner@example.invalid") -> dict:
        """A CLAUDE_CONFIG_DIR the script will resolve instead of the real ~/.claude.json. Without this
        the credential check would read the host's actual account and the test would be
        non-deterministic (its result would depend on who is logged in when it runs)."""
        home = os.path.join(repo, "fake-home")
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, ".claude.json"), "w", encoding="utf-8") as f:
            json.dump({"oauthAccount": {"accountUuid": account, "organizationUuid": org,
                                        "emailAddress": email}}, f)
        return {"CLAUDE_CONFIG_DIR": home}

    def _fake_lock(self, repo: str, account: str | None = "acct-1", org: str = "org-1",
                   email: str = "owner@example.invalid") -> None:
        """presence.lock with a FRESH heartbeat, as write_lock leaves it — so Update reads the daemon as
        alive and never enters the revive arm at all. account=None models a daemon that booted without
        the identity stamp."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lock = {"pid": 4242, "started_at": now, "heartbeat": now}
        if account is not None:
            lock["claude_identity"] = {"account_uuid": account, "organization_uuid": org,
                                       "email": email}
        with open(os.path.join(repo, "seneschal", "state", "presence.lock"), "w", encoding="utf-8") as f:
            json.dump(lock, f)

    def _control_queue(self, repo: str) -> list:
        path = os.path.join(repo, "seneschal", "state", "control-queue.json")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _presence_health(self, repo: str) -> dict:
        with open(os.path.join(repo, "seneschal", "state", "presence-health.json"), encoding="utf-8-sig") as f:
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

    def test_real_pull_requests_both_the_daemon_and_health_listener_restart(self):
        """The actual "a merge landed" path: a real `git pull --ff-only` against a real remote must
        request the daemon's own graceful restart AND drop a restart request into
        `health-listener-control.json` — the health listener has no other restart hook, so without it a
        listener-code PR needs a human to remember one by hand."""
        repo, _origin = self._make_repo_with_real_remote()

        result = self._run_update(repo)

        self.assertEqual(result.returncode, 0, result.stderr)
        h = self._health(repo)
        self.assertEqual(h["status"], "ok")
        self.assertIn("requested graceful restart", self._log(repo))
        # The pull actually landed the new commit.
        with open(os.path.join(repo, "README.md"), encoding="utf-8") as f:
            self.assertIn("merged change", f.read())
        # The daemon's own restart request still fires.
        queue = self._control_queue(repo)
        self.assertEqual([i["action"] for i in queue], ["restart"])
        self.assertEqual(queue[0]["reason"], "merge to main")
        # The health-listener restart request, in its OWN file — never the daemon's queue.
        hl = self._health_listener_control(repo)
        self.assertIsNotNone(hl, "health-listener-control.json must be written on a real pull")
        self.assertEqual(hl["action"], "restart")
        self.assertEqual(hl["reason"], "merge to main")
        self.assertRegex(hl["requested_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

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
        # Anchored on the log's `] ` prefix: the presence-down arm logs its own
        # "PRESENCE ALERT suppressed: ..." line, which must not satisfy this assertion by substring.
        self.assertRegex(self._log(repo), r"\] ALERT suppressed: no telegram\.env",
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

    # --------------------------------------------- the presence watchdog (seneschald-revive-spec.md)

    def test_dead_daemon_is_stamped_down_and_a_deliberate_stop_vetoes_the_revive(self):
        """No lock = no daemon. The presence arm must stamp presence-health.json DOWN, consult the revive
        predicate, and — with the deliberate-stop sentinel present — refuse to revive, saying why. The
        git phase must still run and stamp the deploy health as usual."""
        repo = self._make_repo()
        self._run_update(repo)
        ph = self._presence_health(repo)
        self.assertEqual(ph["status"], "down")
        self.assertEqual(int(ph["consecutive_down"]), 1)
        self.assertRegex(ph["updated_at"], r"Z$", "presence-health stamps must be UTC with an explicit Z")
        self.assertIn("revive: SKIPPED — deliberately stopped", self._log(repo))
        self.assertEqual(self._health(repo)["status"], "ok", "a dead daemon must not block the deploy path")

    def test_live_daemon_reads_alive(self):
        repo = self._make_repo()
        self._fake_lock(repo)
        self._run_update(repo)
        self.assertEqual(self._presence_health(repo)["status"], "ok")
        self.assertIn("presence: alive", self._log(repo))

    def test_health_stamps_are_utc_with_an_explicit_z(self):
        """A bare `Get-Date -Format 's'` writes machine-local time with no offset marker, which every
        reader parses as UTC — ages come out wrong by the full UTC offset."""
        repo = self._make_repo()
        self._run_update(repo)
        h = self._health(repo)
        for field in ("updated_at", "last_ok"):
            self.assertRegex(h[field], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", field)

    # ------------------------------------------------------------------ the cockpit deploy opt-in

    def _sync_args(self, repo: str) -> str:
        script = os.path.join(repo, "seneschal", "scripts", "seneschald-control.ps1")
        out = subprocess.run(
            [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             f". '{script}'; (Get-DaemonSyncArgs) -join ' '"],
            cwd=repo, capture_output=True, text=True, timeout=60,
        )
        return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""

    def test_deploy_sync_prunes_the_cockpit_extras_unless_opted_in(self):
        """`uv sync --frozen` UNINSTALLS extras it wasn't asked for, so without a durable opt-in the
        cockpit's fastapi/uvicorn survive only until the next merge. `state/cockpit-enabled` is that
        opt-in. Dot-sourcing must also load the functions WITHOUT running an action (the seam)."""
        repo = self._make_repo()
        self.assertEqual(self._sync_args(repo), "sync --frozen")
        with open(os.path.join(repo, "seneschal", "state", "cockpit-enabled"), "w", encoding="utf-8"):
            pass
        self.assertEqual(self._sync_args(repo), "sync --frozen --extra cockpit")
        self.assertFalse(os.path.exists(os.path.join(repo, "seneschal", "state", "seneschald-health.json")),
                         "dot-sourcing must not run Update")

    # ------------------------------------------------- the credential guard (seneschald-revive-spec.md §9)
    #
    # These assert the WIRING — that Update-Seneschald actually calls the check, that it runs ahead of
    # the git phase, and that the health fields survive the Set-SeneschaldHealth rebuild later in the same
    # cycle — by reading the files a real cycle leaves behind.

    def test_credential_change_requests_the_same_graceful_restart_a_merge_does(self):
        """A login the daemon has not picked up must queue a graceful restart — the deploy path's own
        mechanism, not a second one, and not a hard kill."""
        repo = self._make_repo()
        self._fake_lock(repo, account="acct-1", org="org-1", email="old@example.invalid")
        env = self._fake_claude_config(repo, account="acct-2", org="org-2", email="new@example.invalid")

        self._run_update(repo, extra_env=env)

        queue = self._control_queue(repo)
        self.assertEqual([i["action"] for i in queue], ["restart"])
        self.assertTrue(queue[0]["defer_until_idle"], "it must WAIT for the warm session, like a merge does")
        h = self._health(repo)
        self.assertEqual(h["credential_state"], "restart-requested")
        self.assertEqual(h["credential_restart_for"], "acct-2/org-2")
        # The git phase stamps ok AFTER the credential check in the same cycle; the credential fields
        # have to survive that rebuild or the loop guard forgets what it asked for every 10 minutes.
        self.assertEqual(h["status"], "ok")
        self.assertIn("account changed", self._log(repo))

    def test_same_identity_requests_nothing_however_recently_the_file_was_written(self):
        """THE anti-mtime case. ~/.claude/.credentials.json is rewritten by every routine OAuth refresh,
        so an mtime trigger would bounce the daemon on a timer forever. Same account, freshly written
        config, nothing happens."""
        repo = self._make_repo()
        self._fake_lock(repo, account="acct-1", org="org-1")
        env = self._fake_claude_config(repo, account="acct-1", org="org-1")

        self._run_update(repo, extra_env=env)

        self.assertEqual(self._control_queue(repo), [])
        self.assertEqual(self._health(repo)["credential_state"], "ok")

    def test_no_daemon_stamp_takes_no_action(self):
        """A daemon that booted without the identity stamp leaves nothing to compare. That is 'I could
        not tell', which must never be read as 'the account changed'."""
        repo = self._make_repo()
        self._fake_lock(repo, account=None)
        env = self._fake_claude_config(repo, account="acct-2", org="org-2")

        self._run_update(repo, extra_env=env)

        self.assertEqual(self._control_queue(repo), [])
        self.assertEqual(self._health(repo)["credential_state"], "unstamped")

    def test_unreadable_config_takes_no_action(self):
        """An unreadable config is not evidence of anything. No restart, no crash, and the cycle still
        completes its normal git work."""
        repo = self._make_repo()
        self._fake_lock(repo, account="acct-1", org="org-1")
        home = os.path.join(repo, "fake-home")
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, ".claude.json"), "w", encoding="utf-8") as f:
            f.write("{ half-written")

        self._run_update(repo, extra_env={"CLAUDE_CONFIG_DIR": home})

        self.assertEqual(self._control_queue(repo), [])
        h = self._health(repo)
        self.assertEqual(h["credential_state"], "unreadable")
        self.assertEqual(h["status"], "ok", "an unreadable config must not block the deploy path")

    def test_it_does_not_ask_twice_for_the_same_change(self):
        """The loop guard. The restart waits for the warm session to go idle, so the mismatch survives
        the next cycle by design — asking again every 10 minutes would be a restart loop."""
        repo = self._make_repo()
        self._fake_lock(repo, account="acct-1", org="org-1")
        env = self._fake_claude_config(repo, account="acct-2", org="org-2")

        self._run_update(repo, extra_env=env)
        self._run_update(repo, extra_env=env)

        # One entry either way (request_control dedupes), so assert on the guard's own bookkeeping too.
        self.assertEqual([i["action"] for i in self._control_queue(repo)], ["restart"])
        h = self._health(repo)
        self.assertEqual(h["credential_state"], "restart-pending")
        self.assertEqual(int(h["credential_stuck_cycles"]), 1)


if __name__ == "__main__":
    unittest.main()
