#!/usr/bin/env python3
"""Tests for `claude_accounts.py` and `jobs.py start --account <name>`.

An account is a Claude config dir; a job on it gets `CLAUDE_CONFIG_DIR` in its child env.

**The invariant most of this file pins: ONE JOB, ONE LOGIN, EVERY SPAWN.** A job's first attempt, its
`--retry` respawn, its `--worktree` spawn and §3.13's RESUME (`claude --resume <id>`) must all carry the
same config dir — a resume against another login cannot find the session it is resuming, and would
fail in a way that reads like the job's own fault. Each spawn path is driven through the real code
(`start_job`, `_spawn_attempt` via `reconcile`, the real `_run_shim`), with only the process faked.

**The second: NO FLAG, NO CHANGE.** A job started without `--account` carries no `account` block and
its child env is exactly what it was — including an inherited `CLAUDE_CONFIG_DIR`, left alone.

Run: python -m unittest seneschal.scripts.test_claude_accounts   (or)   python test_claude_accounts.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import claude_accounts as ca  # noqa: E402
import jobs  # noqa: E402
from test_jobs import NOW, _FakeChild, _Recorder, _RealGit, _runner  # noqa: E402 — shared seams

_REAL_DEFAULT_CONFIG_DIR = ca.default_config_dir  # captured before any test patches it


def _make_login(root: str, name: str = ".claude-2", logged_in: bool = True) -> str:
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    if logged_in:
        with open(os.path.join(path, ca.CREDENTIALS_FILE), "w", encoding="utf-8") as fh:
            fh.write("{}")
    return path


def _isolate_reference(case: unittest.TestCase, root: str) -> str:
    """Point "the default login" at an empty temp dir, so these tests never compare against the real
    `~/.claude` of whatever host runs them. Returns that dir."""
    ref = os.path.join(root, "default-login")
    os.makedirs(ref, exist_ok=True)
    patcher = mock.patch.object(ca, "default_config_dir", return_value=ref)
    patcher.start()
    case.addCleanup(patcher.stop)
    return ref


def _write_registry(state: str, mapping: dict) -> None:
    os.makedirs(state, exist_ok=True)
    with open(ca.registry_path(state), "w", encoding="utf-8") as fh:
        json.dump(mapping, fh)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state)
        self.login = _make_login(self.tmp.name)
        self.ref = _isolate_reference(self, self.tmp.name)

    def test_a_registered_logged_in_dir_resolves_to_its_absolute_path(self):
        _write_registry(self.state, {"default": None, "2": self.login})
        self.assertEqual(ca.resolve(self.state, "2"),
                         {"name": "2", "config_dir": os.path.abspath(self.login)})

    def test_null_and_the_default_name_mean_the_normal_login(self):
        _write_registry(self.state, {"default": None, "2": self.login})
        self.assertEqual(ca.resolve(self.state, "default"), {"name": "default", "config_dir": None})

    def test_default_resolves_even_with_no_registry_at_all(self):
        self.assertEqual(ca.resolve(self.state, "default")["config_dir"], None)

    def test_an_unknown_name_is_refused_naming_the_registered_ones_and_the_fix(self):
        _write_registry(self.state, {"default": None, "2": self.login})
        with self.assertRaises(ca.AccountError) as cm:
            ca.resolve(self.state, "3")
        msg = str(cm.exception)
        self.assertIn("unknown account '3'", msg)
        self.assertIn("registered: 2, default", msg)
        self.assertIn(ca.SETUP_DOC, msg)
        self.assertNotIn("\n", msg)  # one line

    def test_a_name_with_no_registry_file_is_refused(self):
        with self.assertRaises(ca.AccountError) as cm:
            ca.resolve(self.state, "2")
        self.assertIn("there is no", str(cm.exception))

    def test_a_dir_that_does_not_exist_is_refused(self):
        _write_registry(self.state, {"2": os.path.join(self.tmp.name, "nope")})
        with self.assertRaises(ca.AccountError) as cm:
            ca.resolve(self.state, "2")
        self.assertIn("does not exist", str(cm.exception))

    def test_a_dir_that_was_never_logged_in_is_refused(self):
        bare = _make_login(self.tmp.name, ".claude-3", logged_in=False)
        _write_registry(self.state, {"3": bare})
        with self.assertRaises(ca.AccountError) as cm:
            ca.resolve(self.state, "3")
        self.assertIn(ca.CREDENTIALS_FILE, str(cm.exception))
        self.assertIn("claude /login", str(cm.exception))

    def test_a_corrupt_registry_is_an_error_not_an_empty_registry(self):
        with open(ca.registry_path(self.state), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(ca.AccountError) as cm:
            ca.resolve(self.state, "2")
        self.assertIn("unreadable", str(cm.exception))

    def test_underscore_keys_are_notes_not_accounts(self):
        _write_registry(self.state, {"_note": "for the human", "2": self.login})
        self.assertEqual(sorted(ca.load_registry(self.state)), ["2"])

    # ---------------------------------------------------------------- the rails travel with the login

    def _ref_files(self, hooks=None, md=b"# the owner's rules\n"):
        with open(os.path.join(self.ref, ca.SETTINGS_FILE), "w", encoding="utf-8") as fh:
            json.dump({"hooks": hooks or {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "python merge_guard.py"}]}]}}, fh)
        with open(os.path.join(self.ref, ca.INSTRUCTIONS_FILE), "wb") as fh:
            fh.write(md)

    def test_an_account_missing_the_default_logins_hooks_is_refused(self):
        """A second config dir starts with no settings.json — a job there would run with no
        merge_guard, no send_gate_hook, no job_background_guard. Refused, naming the fix."""
        self._ref_files()
        _write_registry(self.state, {"2": self.login})
        with self.assertRaises(ca.AccountError) as cm:
            ca.resolve(self.state, "2")
        self.assertIn("same hooks", str(cm.exception))
        self.assertIn("step 2", str(cm.exception))

    def test_an_account_with_drifted_instructions_is_refused(self):
        self._ref_files()
        shutil.copy(os.path.join(self.ref, ca.SETTINGS_FILE), self.login)
        with open(os.path.join(self.login, ca.INSTRUCTIONS_FILE), "wb") as fh:
            fh.write(b"# an older copy\n")
        _write_registry(self.state, {"2": self.login})
        with self.assertRaises(ca.AccountError) as cm:
            ca.resolve(self.state, "2")
        self.assertIn(ca.INSTRUCTIONS_FILE, str(cm.exception))

    def test_an_account_carrying_the_same_rails_resolves(self):
        self._ref_files()
        for name in (ca.SETTINGS_FILE, ca.INSTRUCTIONS_FILE):
            shutil.copy(os.path.join(self.ref, name), self.login)
        _write_registry(self.state, {"2": self.login})
        self.assertEqual(ca.resolve(self.state, "2")["config_dir"], os.path.abspath(self.login))

    def test_the_reference_is_always_home_claude_never_the_inherited_config_dir(self):
        """A launcher already running on account 2 must not become account 2's own yardstick."""
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.login}):
            ref = _REAL_DEFAULT_CONFIG_DIR()
        self.assertEqual(os.path.basename(ref), ".claude")
        self.assertNotEqual(os.path.abspath(ref), os.path.abspath(self.login))

    def test_tilde_and_env_vars_are_expanded(self):
        with mock.patch.dict(os.environ, {"SENESCHAL_TEST_LOGIN_ROOT": self.tmp.name}):
            _write_registry(self.state, {"2": os.path.join("$SENESCHAL_TEST_LOGIN_ROOT", ".claude-2")})
            self.assertEqual(ca.resolve(self.state, "2")["config_dir"], os.path.abspath(self.login))


class ChildEnvTests(unittest.TestCase):
    def test_an_account_sets_the_config_dir_and_still_scrubs_the_api_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-should-never-leak"}):
            env = jobs.child_env("j1", {"name": "2", "config_dir": "C:\\login-2"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "C:\\login-2")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["SENESCHAL_JOB_ID"], "j1")

    def test_the_default_account_REMOVES_an_inherited_config_dir(self):
        """Naming an account is a claim about which login the job uses — an inherited dir from a
        launcher running under account 2 must not silently contradict `--account default`."""
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "C:\\login-2"}):
            env = jobs.child_env("j1", {"name": "default", "config_dir": None})
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)

    def test_no_account_leaves_the_env_byte_for_byte_as_before(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "C:\\inherited",
                                          "ANTHROPIC_API_KEY": "sk-x"}):
            self.assertEqual(jobs.child_env("j1"), jobs.child_env("j1", None))
            self.assertEqual(jobs.child_env("j1")["CLAUDE_CONFIG_DIR"], "C:\\inherited")
            self.assertNotIn("ANTHROPIC_API_KEY", jobs.child_env("j1"))


class AccountJobTests(unittest.TestCase):
    """`--account` where it meets `jobs.py`: the record, every spawn path, and the visible surfaces."""

    ACCOUNT = {"name": "2", "config_dir": "C:\\Users\\example\\.claude-2"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state)

    def _start(self, calls, **kw):
        kw.setdefault("now", NOW)
        return jobs.start_job(self.state, "on account 2", ["claude", "-p", "brief"],
                              runner=_runner(calls=calls), **kw)

    def test_the_record_stamps_the_resolved_dir_and_the_shim_spawns_on_it(self):
        calls: list = []
        rec = self._start(calls, account=self.ACCOUNT)
        self.assertEqual(rec["account"], self.ACCOUNT)
        self.assertEqual(jobs.load_job(self.state, rec["id"])["account"], self.ACCOUNT)
        self.assertEqual(calls[0][1]["env"]["CLAUDE_CONFIG_DIR"], self.ACCOUNT["config_dir"])

    def test_without_the_flag_there_is_no_account_block_and_no_env_change(self):
        calls: list = []
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "C:\\inherited"}):
            rec = self._start(calls)
        self.assertNotIn("account", rec)
        self.assertEqual(calls[0][1]["env"]["CLAUDE_CONFIG_DIR"], "C:\\inherited")

    def test_the_real_shim_hands_the_config_dir_to_the_jobs_own_command(self):
        """The second spawn point: `_run_shim`'s Popen of the real argv — the process that IS the
        `claude -p` session. Only the grandchild is faked."""
        rec = self._start([], account=self.ACCOUNT)
        seen: list = []

        def fake_popen(argv, **kwargs):
            seen.append(kwargs["env"])
            return _FakeChild(0, "ok", kwargs.get("stdout"))
        with mock.patch.object(jobs.subprocess, "Popen", fake_popen), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}):
            jobs._run_shim(self.state, rec["id"], clock=lambda: NOW)
        self.assertEqual(seen[0]["CLAUDE_CONFIG_DIR"], self.ACCOUNT["config_dir"])
        self.assertNotIn("ANTHROPIC_API_KEY", seen[0])

    def test_a_retry_respawn_lands_on_the_same_login(self):
        calls: list = []
        rec = self._start(calls, account=self.ACCOUNT, retry=jobs.retry_config(2))
        rec.update({"status": jobs.RETRY_PENDING, "attempt": 2, "pid": None,
                    "next_attempt_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, rec)
        respawn: list = []
        jobs.reconcile(self.state, notify=_Recorder(), now=NOW + timedelta(seconds=1),
                       runner=_runner(calls=respawn))
        self.assertEqual(len(respawn), 1)
        self.assertEqual(respawn[0][1]["env"]["CLAUDE_CONFIG_DIR"], self.ACCOUNT["config_dir"])

    def test_a_worktree_job_carries_the_account(self):
        from test_job_worktree import _FakeGit
        calls: list = []
        roots = os.path.join(self.tmp.name, "seneschal-worktrees")
        rec = self._start(calls, account=self.ACCOUNT, worktree=True, worktree_root=roots,
                          wt_runner=_FakeGit())
        self.assertEqual(rec["cwd"], os.path.join(roots, rec["id"]))
        self.assertEqual(rec["account"], self.ACCOUNT)
        self.assertEqual(calls[0][1]["cwd"], rec["cwd"])
        self.assertEqual(calls[0][1]["env"]["CLAUDE_CONFIG_DIR"], self.ACCOUNT["config_dir"])

    def test_list_status_and_the_push_name_a_non_default_account(self):
        rec = self._start([], account=self.ACCOUNT)
        rec.update({"status": jobs.DONE, "exit_code": 0, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, rec)
        self.assertIn("Account: 2", jobs.notify_text(rec))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            jobs.main(["--state-dir", self.state, "list"])
            jobs.main(["--state-dir", self.state, "status", rec["id"]])
        text = out.getvalue()
        self.assertIn("account 2", text)
        self.assertIn(self.ACCOUNT["config_dir"], text)

    def test_the_default_account_is_not_shown(self):
        rec = self._start([], account={"name": "default", "config_dir": None})
        rec.update({"status": jobs.DONE, "exit_code": 0, "ended_at": jobs._stamp(NOW)})
        self.assertNotIn("Account", jobs.notify_text(rec))
        plain = dict(rec)
        del plain["account"]
        self.assertEqual(jobs.notify_text(rec), jobs.notify_text(plain))


class AccountCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state)
        _isolate_reference(self, self.tmp.name)

    def _run(self, *argv):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = jobs.main(["--state-dir", self.state, *argv])
        return code, out.getvalue(), err.getvalue()

    def _refused(self, *argv):
        def never(*a, **kw):  # a refusal that fails must fail the test, never spawn a real job
            raise AssertionError("start_job reached — the account was not refused")
        with mock.patch.object(jobs, "start_job", never):
            return self._run(*argv)

    def test_an_unknown_account_refuses_with_exit_2_and_writes_nothing(self):
        code, _, err = self._refused("start", "--title", "x", "--account", "2", "--",
                                     "python", "-c", "pass")
        self.assertEqual(code, 2)
        self.assertIn("refusing to start: unknown account '2'", err)
        self.assertEqual(jobs.list_jobs(self.state), [])

    def test_a_never_logged_in_dir_refuses_with_exit_2(self):
        bare = _make_login(self.tmp.name, ".claude-2", logged_in=False)
        _write_registry(self.state, {"2": bare})
        code, _, err = self._refused("start", "--title", "x", "--account", "2", "--",
                                     "python", "-c", "pass")
        self.assertEqual(code, 2)
        self.assertIn(ca.CREDENTIALS_FILE, err)
        self.assertEqual(jobs.list_jobs(self.state), [])

    def test_a_good_account_starts_and_reports_it(self):
        login = _make_login(self.tmp.name)
        _write_registry(self.state, {"2": login})
        real_start, seen = jobs.start_job, []

        def stubbed(*a, **kw):  # inject the fake spawner; everything else is the real path
            seen.append(kw.get("account"))
            kw["runner"] = _runner()
            return real_start(*a, **kw)

        with mock.patch.object(jobs, "start_job", stubbed):
            code, out, _ = self._run("start", "--title", "x", "--account", "2", "--no-agent",
                                     "--", "python", "-c", "pass")
        self.assertEqual(code, 0)
        self.assertEqual(seen, [{"name": "2", "config_dir": os.path.abspath(login)}])
        self.assertEqual(json.loads(out)["account"]["name"], "2")


class ResumeCarriesTheAccountTests(unittest.TestCase):
    """§3.13's RESUME, the case the brief named explicitly: `claude --resume <id>` must run against the
    SAME config dir the session was created in. Real git (the DETECT layer needs it), fake spawn."""

    ACCOUNT = {"name": "2", "config_dir": "C:\\Users\\example\\.claude-2"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        origin = os.path.join(self.tmp.name, "origin.git")
        self.work = os.path.join(self.tmp.name, "work")
        for args in (["git", "init", "-q", "--bare", origin],
                     ["git", "clone", "-q", origin, self.work],
                     ["git", "-C", self.work, "config", "user.email", "test@example.com"],
                     ["git", "-C", self.work, "config", "user.name", "Test"],
                     ["git", "-C", self.work, "commit", "-q", "--allow-empty", "-m", "init"],
                     ["git", "-C", self.work, "branch", "-m", "develop"],
                     ["git", "-C", self.work, "push", "-q", "origin", "HEAD:develop"]):
            subprocess.run(args, check=True, capture_output=True, text=True)

    def test_the_resume_respawn_runs_on_the_original_login(self):
        rec = jobs.start_job(self.state, "coding job", ["claude", "-p", "do the thing"],
                             cwd=self.work, runner=_runner(), now=NOW, account=self.ACCOUNT)
        self.assertIn("claude_session_id", rec)
        rec["worktree"] = {"path": self.work, "host": self.work}
        subprocess.run(["git", "-C", self.work, "checkout", "-q", "-b", f"feature/{rec['id']}"],
                       check=True, capture_output=True, text=True)
        with open(os.path.join(self.work, "half.md"), "w", encoding="utf-8") as fh:
            fh.write("half-written\n")
        subprocess.run(["git", "-C", self.work, "add", "half.md"], check=True,
                       capture_output=True, text=True)
        rec.update({"status": jobs.DONE, "exit_code": 0, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, rec)

        calls: list = []
        jobs.reconcile(self.state, notify=_Recorder(), now=NOW, completion_runner=_RealGit(),
                       runner=_runner(calls=calls))
        after = jobs.load_job(self.state, rec["id"])
        self.assertEqual(after["argv"][:3], ["claude", "--resume", rec["claude_session_id"]])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["env"]["CLAUDE_CONFIG_DIR"], self.ACCOUNT["config_dir"])
        self.assertEqual(after["account"], self.ACCOUNT)


if __name__ == "__main__":
    unittest.main()
