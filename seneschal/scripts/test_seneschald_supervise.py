#!/usr/bin/env python3
"""Tests for seneschald_supervise.py and presence.py's supervised branch
(seneschal/docs/service-supervision-spec.md §2-§3).

Everything is offline. The supervised child is a FAKE: a small stdlib script that follows a plan file
(one step per start: an exit code, lines to print, a file to touch, an exception to raise) and records
each start — its pid, parent pid, argv, cwd and the environment variables the contract cares about.
Real processes, so the exit codes, the pipe, the env and the parent pid are all the kernel's, not a
mock's. Sleeps are injected, so the backoff is asserted rather than waited out.

Pinned here, on every OS CI runs (ubuntu, macOS, Windows — the `service-supervision` job):
* the exit-code table (75 reload / 0 stay down / 69 lock held / anything else a crash), both as a pure
  classification and end-to-end through real children;
* a real child restarted on 75 without the supervisor's pid changing (in-process and as a real
  supervisor subprocess);
* the crash backoff, its reset, and the budget's exhaustion into the `seneschald-crashloop` sentinel;
* lock contention: a child exiting 69 makes the supervisor wait for the holder, then start;
* a reload storm: reloads spend no budget and no backoff, up to a ceiling past which they count;
* the `seneschald-stopped` check before every restart (and not before the first start);
* the env scrub, the child log (capture, an import-time traceback, size-capped rotation);
* the supervisor's own lock (a second supervisor exits 69), its report, and its self-upgrade;
* Windows: the kill-on-close job kills its member but no grandchild;
* POSIX: `os.execv` keeps the pid — the property the systemd and launchd units rely on for a reload;
* presence.py: the flag is read and removed, `child_env` drops it, the reload exit (75, lock), and the
  real entry point exiting 69 on a held lock only when supervised;
* `seneschald.args` parses with the daemon's real parser, and carries exactly run-presence.cmd's flags.

Run: python -m unittest test_seneschald_supervise
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import seneschald_supervise as ms  # noqa: E402
import single_instance as si  # noqa: E402

#: The base interpreter, never a venv launcher, so a fake child's parent IS the supervisor.
BASE_PY = getattr(sys, "_base_executable", None) or sys.executable

_FAKE_CHILD = textwrap.dedent(r'''
    import json, os, sys
    plan_path, record_path = os.environ["FAKE_PLAN"], os.environ["FAKE_RECORD"]
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    try:
        with open(record_path, encoding="utf-8") as f:
            runs = json.load(f)
    except FileNotFoundError:
        runs = []
    step = plan[min(len(runs), len(plan) - 1)]
    runs.append({"pid": os.getpid(), "ppid": os.getppid(), "argv": sys.argv[1:], "cwd": os.getcwd(),
                 "env": {k: os.environ.get(k) for k in
                         ("ANTHROPIC_API_KEY", "PYTHONUTF8", "PYTHONIOENCODING", "SENESCHALD_SUPERVISED")}})
    with open(record_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(runs, f)
    os.replace(record_path + ".tmp", record_path)
    for i in range(step.get("print_n", 0)):
        print(f"child line {i:05d} " + "x" * 40, flush=True)
    for line in step.get("print", []):
        print(line, flush=True)
    if step.get("touch"):
        open(step["touch"], "w").close()
    if step.get("hold_pipe_sec"):
        import subprocess
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%s)" % step["hold_pipe_sec"]],
                         stdout=sys.stdout, creationflags=0x08000000 if os.name == "nt" else 0)
    if step.get("raise"):
        raise RuntimeError(step["raise"])
    sys.exit(step.get("exit", 0))
''')


class Harness(unittest.TestCase):
    """A temp state dir, a fake child, a plan, and a Supervisor wired to all of it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.state)
        self.child = os.path.join(self.dir, "fake_child.py")
        with open(self.child, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_CHILD)
        self.plan_path = os.path.join(self.dir, "plan.json")
        self.record_path = os.path.join(self.dir, "runs.json")
        self.args_file = os.path.join(self.dir, "test.args")
        with open(self.args_file, "w", encoding="utf-8") as fh:
            fh.write("--alpha 1\n--path {repo}/x/y.txt\n")
        self.sleeps: list = []

    def plan(self, *steps):
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump(list(steps), fh)

    def runs(self) -> list:
        try:
            with open(self.record_path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return []

    def env(self, **extra) -> dict:
        env = dict(os.environ, FAKE_PLAN=self.plan_path, FAKE_RECORD=self.record_path)
        env.pop(ms.SUPERVISED_ENV, None)
        env.update(extra)
        return env

    def supervisor(self, **kw) -> ms.Supervisor:
        on_sleep = kw.pop("on_sleep", None)

        def sleep(sec):
            self.sleeps.append(sec)
            if on_sleep:
                on_sleep(sec)

        opts = dict(state_dir=self.state, script=self.child, args_file=self.args_file, python=BASE_PY,
                    cwd=self.dir, sleep=sleep, heartbeat_sec=5.0, base_env=self.env(),
                    source_hash_fn=lambda: "boot-hash")
        opts.update(kw)
        return ms.Supervisor(**opts)

    def report(self) -> dict:
        with open(ms.Paths(self.state).report, encoding="utf-8") as fh:
            return json.load(fh)

    def child_log(self) -> str:
        try:
            with open(ms.Paths(self.state).child_log, encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return ""


# ------------------------------------------------------------------------------------------ pure policy
class ExitCodeTable(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(ms.classify(75), ms.KIND_RELOAD)
        self.assertEqual(ms.classify(0), ms.KIND_STAY_DOWN)
        self.assertEqual(ms.classify(69), ms.KIND_LOCK_HELD)
        for crash in (1, 2, 3, 70, 74, 76, 255, -9, 3221225477, None):
            self.assertEqual(ms.classify(crash), ms.KIND_CRASH, crash)

    def test_reload_restarts_at_once_and_is_not_counted(self):
        p = ms.Policy()
        d = p.on_exit(75, uptime=5, now=100.0)
        self.assertEqual((d.action, d.delay), (ms.ACTION_RESTART, 0.0))
        self.assertEqual(p.budget_left(100.0), ms.CRASH_BUDGET)
        self.assertEqual(p.crash_count, 0)

    def test_stay_down_and_lock_held(self):
        p = ms.Policy()
        self.assertEqual(p.on_exit(0, 5, 1.0).action, ms.ACTION_STOP)
        self.assertEqual(p.on_exit(69, 5, 2.0).action, ms.ACTION_WAIT_LOCK)
        self.assertEqual(p.budget_left(2.0), ms.CRASH_BUDGET)


class Backoff(unittest.TestCase):
    def test_backoff_sequence_then_give_up(self):
        p = ms.Policy()
        delays = [p.on_exit(1, uptime=1, now=float(i)).delay for i in range(5)]
        self.assertEqual(delays, [2, 10, 30, 60, 120])
        self.assertEqual(p.on_exit(1, uptime=1, now=6.0).action, ms.ACTION_GIVE_UP)

    def test_last_step_repeats(self):
        p = ms.Policy(budget=100)
        delays = [p.on_exit(1, uptime=1, now=float(i)).delay for i in range(8)]
        self.assertEqual(delays[4:], [120, 120, 120, 120])

    def test_ten_minutes_of_uptime_resets_the_backoff(self):
        p = ms.Policy()
        p.on_exit(1, uptime=1, now=0.0)
        p.on_exit(1, uptime=1, now=1.0)
        d = p.on_exit(1, uptime=ms.BACKOFF_RESET_UPTIME_SEC, now=2.0)
        self.assertEqual(d.delay, 2)

    def test_crashes_outside_the_window_do_not_count(self):
        p = ms.Policy()
        for i in range(5):
            p.on_exit(1, uptime=1, now=float(i))
        later = 4.0 + ms.CRASH_WINDOW_SEC + 1
        self.assertEqual(p.on_exit(1, uptime=1, now=later).action, ms.ACTION_RESTART)

    def test_reload_storm_past_the_ceiling_counts_as_a_crash(self):
        p = ms.Policy(storm_max=3)
        for i in range(3):
            self.assertEqual(p.on_exit(75, 1, float(i)).delay, 0.0)
        d = p.on_exit(75, 1, 3.0)
        self.assertEqual((d.action, d.kind, d.delay), (ms.ACTION_RESTART, ms.KIND_CRASH, 2.0))
        self.assertIn("reload storm", d.reason)


# ------------------------------------------------------------------------------------------ args
class ArgsFile(unittest.TestCase):
    def test_flags_values_comments_and_repo_token(self):
        text = "﻿# comment\n\n--model claude-x\n--flag\n--p {repo}/seneschal/state/presence.log\n" \
               "--prompt Run this, verbatim: a b  c.\n"
        argv = ms.parse_args_text(text.lstrip("﻿"), repo_root=os.path.join("R", "root"))
        self.assertEqual(argv, ["--model", "claude-x", "--flag", "--p",
                                os.path.normpath(os.path.join("R", "root", "seneschal", "state", "presence.log")),
                                "--prompt", "Run this, verbatim: a b  c."])

    def test_a_bare_value_line_is_refused(self):
        with self.assertRaises(ms.ArgsFileError):
            ms.parse_args_text("--model\nclaude-x\n")

    def test_seneschald_args_carries_exactly_run_presence_cmds_flags(self):
        """Both launches exist (a hand run of the .cmd, the supervised task); this keeps their flag lists
        from drifting apart."""
        with open(os.path.join(SCRIPT_DIR, "run-presence.cmd"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        start = next(i for i, ln in enumerate(lines) if '"%~dp0presence.py"' in ln)
        cmd_argv = []
        for ln in lines[start + 1:]:
            s = ln.strip()
            if not s.startswith("--"):
                break
            s = s[:-1].rstrip() if s.endswith("^") else s
            flag, _, value = s.partition(" ")
            cmd_argv.append(flag)
            if value:
                value = value.strip().strip('"')
                if value.startswith("%~dp0"):
                    # The .cmd is batch syntax, so its backslashes ARE separators; on POSIX
                    # normpath treats "..\state\x" as one filename, so translate them first.
                    rel = value[len("%~dp0"):].replace("\\", os.sep)
                    value = os.path.normpath(os.path.join(SCRIPT_DIR, rel))
                cmd_argv.append(value)
        args = ms.read_args(ms.DEFAULT_ARGS_FILE)
        self.assertGreater(len(cmd_argv), 8)
        self.assertEqual(args, cmd_argv)

    def test_run_presence_cmd_supervise_mode_calls_the_supervisor(self):
        with open(os.path.join(SCRIPT_DIR, "run-presence.cmd"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('if /i "%~1"=="supervise" goto :supervise', text)
        self.assertIn('"%~dp0seneschald_supervise.py"', text)
        self.assertNotIn("SENESCHALD_SUPERVISED", text, "the supervisor sets it, never the .cmd")


class ChildEnv(unittest.TestCase):
    def test_scrub(self):
        env = ms.child_env({"ANTHROPIC_API_KEY": "sk-live", "PYTHONUTF8": "0", "KEEP": "1"})
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env[ms.SUPERVISED_ENV], "1")
        self.assertEqual(env["KEEP"], "1")


# ------------------------------------------------------------------------------------------ end to end
class EveryExitCode(Harness):
    def test_reload_reload_then_stay_down(self):
        """A real child restarted on 75 twice, the supervisor's pid never changing, no backoff."""
        self.plan({"exit": 75}, {"exit": 75}, {"exit": 0})
        rc = self.supervisor().run()
        self.assertEqual(rc, 0)
        runs = self.runs()
        self.assertEqual(len(runs), 3)
        self.assertEqual({r["ppid"] for r in runs}, {os.getpid()})
        self.assertEqual(len({r["pid"] for r in runs}), 3)
        self.assertEqual(self.sleeps, [])
        rep = self.report()
        self.assertEqual(rep["pid"], os.getpid())
        self.assertEqual(rep["restarts"], {"reload": 2, "crash": 0})
        self.assertEqual(rep["last_exit"]["kind"], ms.KIND_STAY_DOWN)
        self.assertEqual(rep["state"], "stopped")

    def test_args_reach_the_child_with_the_repo_token_expanded(self):
        self.plan({"exit": 0})
        self.supervisor().run()
        self.assertEqual(self.runs()[0]["argv"],
                         ["--alpha", "1", "--path", os.path.normpath(os.path.join(self.dir, "x", "y.txt"))])

    def test_any_other_code_is_a_crash_with_backoff(self):
        self.plan({"exit": 3}, {"exit": 0})
        self.assertEqual(self.supervisor().run(), 0)
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual(self.sleeps, [2.0])
        self.assertEqual(self.report()["restarts"]["crash"], 1)

    def test_unhandled_exception_is_a_crash_and_its_traceback_is_logged(self):
        self.plan({"raise": "boom at import"}, {"exit": 0})
        self.supervisor().run()
        self.assertEqual(self.sleeps, [2.0])
        log = self.child_log()
        self.assertIn("Traceback", log)
        self.assertIn("boom at import", log)
        self.assertIn("[supervisor] child exited 1", log)

    def test_crash_budget_exhaustion_writes_the_crashloop_sentinel(self):
        self.plan({"exit": 1})
        rc = self.supervisor().run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.runs()), ms.CRASH_BUDGET + 1)
        self.assertEqual(self.sleeps, [2, 10, 30, 60, 120])
        path = ms.Paths(self.state).crashloop
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            sentinel = json.load(fh)
        self.assertEqual(sentinel["by"], "supervisor")
        self.assertEqual(self.report()["state"], "gave-up")

    def test_a_launch_failure_is_a_crash(self):
        self.plan({"exit": 0})
        rc = self.supervisor(python=os.path.join(self.dir, "no-such-python.exe")).run()
        self.assertEqual(rc, 0)
        self.assertEqual(self.sleeps, [2, 10, 30, 60, 120])
        self.assertTrue(os.path.exists(ms.Paths(self.state).crashloop))

    def test_crashloop_sentinel_is_the_one_the_watchdog_reads(self):
        import seneschald_revive
        self.assertEqual(os.path.basename(ms.Paths(self.state).crashloop), seneschald_revive.CRASHLOOP_SENTINEL_NAME)
        self.assertEqual(os.path.basename(ms.Paths(self.state).stopped), seneschald_revive.STOPPED_SENTINEL_NAME)
        self.assertEqual(os.path.basename(ms.Paths(self.state).lock), seneschald_revive.SUPERVISOR_LOCK_NAME)


class LockContention(Harness):
    def test_69_waits_for_the_holder_then_starts(self):
        child_lock = si.InstanceLock(si.instance_lock_path(self.state))
        self.assertTrue(child_lock.acquire())
        self.plan({"exit": 69}, {"exit": 0})
        sup = self.supervisor(on_sleep=lambda sec: child_lock.release())
        try:
            rc = sup.run()
        finally:
            child_lock.release()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual(self.sleeps, [ms.LOCK_WAIT_POLL_SEC])
        self.assertEqual(self.report()["restarts"]["crash"], 0)

    def test_a_stop_while_waiting_starts_nothing(self):
        child_lock = si.InstanceLock(si.instance_lock_path(self.state))
        self.assertTrue(child_lock.acquire())
        self.plan({"exit": 69}, {"exit": 0})

        def stop(_sec):
            open(ms.Paths(self.state).stopped, "w").close()
        try:
            rc = self.supervisor(on_sleep=stop).run()
        finally:
            child_lock.release()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.runs()), 1)

    def test_second_supervisor_exits_69_before_any_side_effect(self):
        held = si.InstanceLock(ms.Paths(self.state).lock)
        self.assertTrue(held.acquire())
        self.plan({"exit": 0})
        try:
            rc = self.supervisor().run()
        finally:
            held.release()
        self.assertEqual(rc, ms.EXIT_LOCK_HELD)
        self.assertEqual(self.runs(), [])
        self.assertFalse(os.path.exists(ms.Paths(self.state).report))


class ReloadStorm(Harness):
    def test_a_storm_spends_no_budget_and_no_backoff(self):
        self.plan(*([{"exit": 75}] * 12), {"exit": 0})
        self.assertEqual(self.supervisor().run(), 0)
        self.assertEqual(len(self.runs()), 13)
        self.assertEqual(self.sleeps, [])
        rep = self.report()
        self.assertEqual(rep["restarts"], {"reload": 12, "crash": 0})
        self.assertEqual(rep["crash_budget"]["left"], ms.CRASH_BUDGET)

    def test_past_the_ceiling_reloads_count(self):
        self.plan(*([{"exit": 75}] * 5), {"exit": 0})
        self.supervisor(policy=ms.Policy(storm_max=3)).run()
        self.assertEqual(len(self.runs()), 6)
        self.assertEqual(self.sleeps, [2.0, 10.0])


class StoppedSentinel(Harness):
    def test_checked_before_a_restart(self):
        stopped = ms.Paths(self.state).stopped
        self.plan({"exit": 1, "touch": stopped}, {"exit": 0})
        self.assertEqual(self.supervisor().run(), 0)
        self.assertEqual(len(self.runs()), 1, "a Stop that races a crash restart must not resurrect it")
        self.assertEqual(self.report()["state"], "stopped")

    def test_checked_before_a_reload_restart_too(self):
        stopped = ms.Paths(self.state).stopped
        self.plan({"exit": 75, "touch": stopped}, {"exit": 0})
        self.supervisor().run()
        self.assertEqual(len(self.runs()), 1)

    def test_not_checked_before_the_first_start(self):
        open(ms.Paths(self.state).stopped, "w").close()
        self.plan({"exit": 0})
        self.supervisor().run()
        self.assertEqual(len(self.runs()), 1)


class EnvScrub(Harness):
    def test_child_sees_the_scrubbed_env_and_the_repo_cwd(self):
        self.plan({"exit": 0})
        sup = self.supervisor(base_env=self.env(ANTHROPIC_API_KEY="sk-must-not-reach", PYTHONUTF8="0"))
        sup.run()
        run = self.runs()[0]
        self.assertEqual(run["env"], {"ANTHROPIC_API_KEY": None, "PYTHONUTF8": "1",
                                      "PYTHONIOENCODING": "utf-8", "SENESCHALD_SUPERVISED": "1"})
        self.assertEqual(os.path.normcase(os.path.realpath(run["cwd"])),
                         os.path.normcase(os.path.realpath(self.dir)))

    def test_scrub_own_env(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk", ms.SUPERVISED_ENV: "1"}):
            ms.scrub_own_env()
            self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
            self.assertNotIn(ms.SUPERVISED_ENV, os.environ)
            self.assertEqual(os.environ["PYTHONUTF8"], "1")


class ChildLog(Harness):
    def test_output_is_captured(self):
        self.plan({"print": ["hello from the child"], "exit": 0})
        self.supervisor().run()
        self.assertIn("hello from the child", self.child_log())

    def test_a_grandchild_holding_the_pipe_never_hangs_the_supervisor(self):
        self.plan({"hold_pipe_sec": 8, "exit": 75}, {"exit": 0})
        started = time.monotonic()
        with mock.patch.object(ms, "PUMP_DRAIN_SEC", 0.5):
            rc = self.supervisor().run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.runs()), 2)
        self.assertLess(time.monotonic() - started, 7, "it must not wait for the grandchild to exit")
        self.assertIn("still held open", self.child_log())

    def test_size_capped_with_one_rotation(self):
        self.plan({"print_n": 600, "exit": 0})
        with mock.patch.object(ms, "CHILD_LOG_MAX_BYTES", 4000):
            self.supervisor().run()
        logs = os.path.join(self.state, "logs")
        names = sorted(os.listdir(logs))
        self.assertIn("seneschald-child.log", names)
        rolled = [n for n in names if n != "seneschald-child.log"]
        self.assertEqual(len(rolled), 1, names)
        self.assertLess(os.path.getsize(os.path.join(logs, "seneschald-child.log")), 4000 + 200)


class SelfUpgrade(Harness):
    def hashes(self, *values):
        it = iter(values)
        last = [values[-1]]
        return lambda: next(it, last[0])

    def test_changed_source_on_a_reload_hands_over(self):
        calls = []
        self.plan({"exit": 75}, {"exit": 0})
        sup = self.supervisor(source_hash_fn=self.hashes("a", "b"),
                              upgrade=lambda s: calls.append(s) or True)
        self.assertEqual(sup.run(), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.runs()), 1, "the successor supervisor starts the next child, not us")
        self.assertEqual(self.report()["state"], "upgrading")

    def test_unchanged_source_just_reloads(self):
        calls = []
        self.plan({"exit": 75}, {"exit": 0})
        self.supervisor(upgrade=lambda s: calls.append(s) or True).run()
        self.assertEqual(calls, [])
        self.assertEqual(len(self.runs()), 2)

    def test_a_crash_never_triggers_an_upgrade(self):
        calls = []
        self.plan({"exit": 1}, {"exit": 0})
        self.supervisor(source_hash_fn=self.hashes("a", "b"),
                        upgrade=lambda s: calls.append(s) or True).run()
        self.assertEqual(calls, [])

    def test_a_declined_upgrade_keeps_supervising(self):
        self.plan({"exit": 75}, {"exit": 0})
        self.supervisor(source_hash_fn=self.hashes("a", "b"), upgrade=lambda s: False).run()
        self.assertEqual(len(self.runs()), 2)

    def test_source_hash_covers_the_imported_modules(self):
        self.assertIsNotNone(ms.source_hash())
        for name in ms._HASHED_SOURCES:
            self.assertTrue(os.path.exists(os.path.join(SCRIPT_DIR, name)), name)

    @unittest.skipUnless(os.name == "nt", "the schtasks handoff is Windows-only")
    def test_windows_handoff_starts_the_helper_outside_any_window(self):
        sup = self.supervisor(task="seneschald")
        sup.report = {"source_sha256": "a"}
        with mock.patch.object(ms.subprocess, "Popen") as popen:
            self.assertTrue(sup.upgrade())
        argv = popen.call_args.args[0]
        self.assertIn("--upgrade-helper", argv)
        self.assertEqual(argv[argv.index("--task") + 1], "seneschald")
        self.assertIn(str(os.getpid()), argv)
        self.assertEqual(popen.call_args.kwargs["creationflags"], ms.CREATE_NO_WINDOW)

    @unittest.skipUnless(os.name == "nt", "the no-task refusal is the Windows path")
    def test_windows_hand_run_without_task_never_starts_the_task(self):
        sup = self.supervisor(task=None)
        with mock.patch.object(ms.subprocess, "Popen") as popen:
            self.assertFalse(sup.upgrade())
            self.assertFalse(sup.upgrade())
        popen.assert_not_called()
        self.assertTrue(sup.upgrade_noted)

    @unittest.skipIf(os.name == "nt", "POSIX re-executes in place")
    def test_posix_reexecs_in_place(self):
        sup = self.supervisor()
        with mock.patch.object(ms.os, "execv") as execv:
            sup.upgrade()
        exe, argv = execv.call_args.args
        self.assertEqual(exe, sys.executable)
        self.assertEqual(argv[1], os.path.abspath(ms.__file__))


class UpgradeHelper(Harness):
    def exited_pid(self) -> int:
        p = subprocess.Popen([BASE_PY, "-c", "pass"])
        p.wait()
        return p.pid

    def test_runs_the_task_and_confirms_a_new_supervisor(self):
        ran, held = [], iter([False, True])
        rc = ms.upgrade_helper("seneschald", self.state, "seneschald", [self.exited_pid()],
                               run=lambda argv: ran.append(argv) or 0, held=lambda: next(held),
                               sleep=lambda s: None)
        self.assertEqual(rc, 0)
        self.assertEqual(ran, [["schtasks", "/Run", "/TN", "seneschald"]] * 2)
        self.assertIn("holds its lock", self.child_log())

    def test_gives_up_after_bounded_tries(self):
        ran = []
        rc = ms.upgrade_helper("seneschald", self.state, "seneschald", [self.exited_pid()],
                               run=lambda argv: ran.append(argv) or 1, held=lambda: False,
                               sleep=lambda s: None)
        self.assertEqual(rc, 1)
        self.assertEqual(len(ran), ms.UPGRADE_CONFIRM_TRIES)
        self.assertIn("watchdog", self.child_log())


class RealSupervisorProcess(Harness):
    """The CLI, as a real process: its pid is the parent of every child it starts."""

    def test_cli_supervises_across_a_reload(self):
        self.plan({"exit": 75}, {"exit": 0})
        proc = subprocess.run(
            [BASE_PY, os.path.join(SCRIPT_DIR, "seneschald_supervise.py"), "--state-dir", self.state,
             "--script", self.child, "--args-file", self.args_file, "--python", BASE_PY],
            env=self.env(ANTHROPIC_API_KEY="sk-x"), capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        runs = self.runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(len({r["ppid"] for r in runs}), 1, "the supervisor's pid never changed")
        self.assertEqual(self.report()["pid"], runs[0]["ppid"])

    def test_print_args(self):
        proc = subprocess.run([BASE_PY, os.path.join(SCRIPT_DIR, "seneschald_supervise.py"), "--print-args"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ms.read_args(ms.DEFAULT_ARGS_FILE))


@unittest.skipUnless(os.name == "nt", "Job Objects are Windows-only")
class KillOnCloseJobM5(unittest.TestCase):
    """Closing the job kills its member, and silent breakaway leaves the member's own child alive — so
    the cockpit and archon sites outlive a supervisor exactly as they outlive a reload."""

    def test_job_kills_the_member_but_no_grandchild(self):
        job = ms.KillOnCloseJob.create()
        self.assertIsNotNone(job)
        code = ("import subprocess, sys, time\n"
                "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
                " creationflags=0x08000000)\n"
                "print(gc.pid, flush=True)\n"
                "time.sleep(60)\n")
        child = subprocess.Popen([BASE_PY, "-c", code], stdout=subprocess.PIPE, text=True,
                                 creationflags=ms.CREATE_NO_WINDOW)
        grandchild = None
        try:
            self.assertTrue(job.assign(child.pid))
            grandchild = int(child.stdout.readline().strip())
            job.close()
            child.wait(timeout=10)  # raises if the job did not kill it
            self.assertTrue(_alive(grandchild), "silent breakaway: the grandchild must survive")
        finally:
            job.close()
            if child.poll() is None:
                child.kill()
            child.stdout.close()
            if grandchild:
                subprocess.run(["taskkill", "/PID", str(grandchild), "/F"], capture_output=True,
                               creationflags=ms.CREATE_NO_WINDOW)


def _alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


# ------------------------------------------------------------------------------------------ presence.py
class PresenceSupervisedBranch(unittest.TestCase):
    """presence.py's side of the contract, gated on SENESCHALD_SUPERVISED (spec §2)."""

    @classmethod
    def setUpClass(cls):
        import presence  # heavy import — once
        cls.presence = presence

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_flag_is_read_and_removed(self):
        env = {self.presence.SUPERVISED_ENV: "1", "OTHER": "x"}
        self.assertTrue(self.presence.take_supervised_flag(env))
        self.assertNotIn(self.presence.SUPERVISED_ENV, env)
        self.assertFalse(self.presence.take_supervised_flag(env))
        self.assertFalse(self.presence.take_supervised_flag({self.presence.SUPERVISED_ENV: "0"}))

    def test_child_env_drops_the_flag(self):
        with mock.patch.dict(os.environ, {self.presence.SUPERVISED_ENV: "1", "ANTHROPIC_API_KEY": "sk"}):
            env = self.presence.child_env()
        self.assertNotIn(self.presence.SUPERVISED_ENV, env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_reload_mode(self):
        rm = self.presence.reload_mode
        self.assertEqual(rm(True, "nt"), "exit")
        self.assertEqual(rm(False, "nt"), "respawn")
        self.assertEqual(rm(True, "posix"), "respawn", "POSIX keeps execv under any supervisor")
        self.assertEqual(rm(False, "posix"), "respawn")

    def test_exit_codes_match_the_supervisor(self):
        self.assertEqual(self.presence.EXIT_RELOAD, ms.EXIT_RELOAD)
        self.assertEqual(self.presence.EXIT_LOCK_HELD, ms.EXIT_LOCK_HELD)
        self.assertEqual(self.presence.SUPERVISED_ENV, ms.SUPERVISED_ENV)

    def test_reload_exit_hands_the_lock_over(self):
        lock = si.InstanceLock(si.instance_lock_path(self.dir))
        self.assertTrue(lock.acquire())
        logs = []
        rc = self.presence._reload_exit(logs.append, self.dir, None, instance_lock=lock)
        self.assertEqual(rc, self.presence.EXIT_RELOAD)
        self.assertFalse(lock.held)
        self.assertFalse(si.is_held(si.instance_lock_path(self.dir)))

    def test_reload_that_would_loop_stays_down(self):
        now = self.presence.datetime.now(self.presence.timezone.utc)
        stamps = [now] * (self.presence.CRASHLOOP_MAX_BOOTS)
        self.presence.save_json(self.presence.boot_attempts_path(self.dir),
                                [self.presence._iso_z(s) for s in stamps])
        lock = si.InstanceLock(si.instance_lock_path(self.dir))
        self.assertTrue(lock.acquire())
        try:
            with mock.patch.object(self.presence, "send_telegram", return_value={"ok": False}):
                rc = self.presence._reload_exit(lambda m: None, self.dir, None, instance_lock=lock)
        finally:
            lock.release()
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.presence.crashloop_sentinel_path(self.dir)))

    def _loser(self, supervised: bool):
        state = tempfile.mkdtemp()
        log_path = os.path.join(state, "presence.log")
        holder = si.InstanceLock(si.instance_lock_path(state))
        self.assertTrue(holder.acquire())
        try:
            env = dict(os.environ, PYTHONUTF8="1")
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop(ms.SUPERVISED_ENV, None)
            if supervised:
                env[ms.SUPERVISED_ENV] = "1"
            proc = subprocess.run(
                [sys.executable, os.path.join(SCRIPT_DIR, "presence.py"), "--state-dir", state,
                 "--log-file", log_path, "--instance-lock-wait-sec", "0", "--stub-brain", "--stub-send",
                 "--no-cockpit"],
                capture_output=True, text=True, timeout=120, env=env)
        finally:
            holder.release()
        with open(log_path, encoding="utf-8") as fh:
            return proc.returncode, fh.read()

    def test_real_loser_exits_69_only_when_supervised(self):
        rc, log = self._loser(supervised=True)
        self.assertEqual(rc, ms.EXIT_LOCK_HELD, log)
        self.assertIn("(supervised)", log)
        rc, log = self._loser(supervised=False)
        self.assertEqual(rc, 0, log)
        self.assertNotIn("supervised", log)


# ------------------------------------------------------------------------------------------ seneschal's rows
class Services(unittest.TestCase):
    """Seneschal ships one SERVICES row, `seneschald`, whose files are the names the watchdog and the
    control script already read."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = tmp.name

    def test_seneschald_row(self):
        sup = ms.Supervisor(state_dir=self.state)
        self.assertEqual(sup.name, "seneschald")
        self.assertEqual(sup.script, ms.DEFAULT_SCRIPT)
        self.assertEqual(sup.args_file, ms.DEFAULT_ARGS_FILE)
        self.assertEqual(sup.child_lock, si.instance_lock_path(sup.paths.state_dir))
        self.assertEqual(sup.policy.budget, ms.CRASH_BUDGET)

    def test_file_names(self):
        p = ms.Paths("S")
        self.assertEqual(os.path.basename(p.lock), "seneschald-supervisor.instance.lock")
        self.assertEqual(os.path.basename(p.report), "seneschald-supervisor.json")
        self.assertEqual(os.path.basename(p.child_log), "seneschald-child.log")
        self.assertEqual(os.path.basename(p.stopped), "seneschald-stopped")
        self.assertEqual(os.path.basename(p.crashloop), "seneschald-crashloop")

    def test_the_child_lock_is_the_one_presence_takes(self):
        self.assertEqual(ms.SERVICES["seneschald"].child_lock, si.INSTANCE_LOCK_NAME)

    def test_an_unknown_name_keeps_the_daemons_script_under_its_own_files(self):
        sup = ms.Supervisor(state_dir=self.state, name="scratch")
        self.assertEqual(sup.script, ms.DEFAULT_SCRIPT)
        self.assertTrue(sup.paths.report.endswith("scratch-supervisor.json"))

    def test_seneschald_args_parse_with_the_daemons_real_parser(self):
        import presence
        args = presence.build_parser().parse_args(ms.read_args(ms.DEFAULT_ARGS_FILE))
        self.assertEqual(args.poll_timeout, 25)
        self.assertTrue(args.log_file.endswith(os.path.join("seneschal", "state", "presence.log")))
        self.assertTrue(os.path.isabs(args.log_file))


@unittest.skipIf(os.name == "nt", "os.execv on Windows is spawn-and-exit — the reason the supervisor exists")
class PosixReloadKeepsThePid(unittest.TestCase):
    """The systemd and launchd units encode the reload contract by NOT seeing it: presence.py re-executes
    in place, so the unit's main pid never exits. This pins the kernel half of that claim with a real
    process: a script that `os.execv`s itself reports the same pid before and after, and its parent sees
    exactly one child exit, with the second image's code."""

    def test_execv_keeps_the_pid_and_the_parent_sees_one_exit(self):
        code = textwrap.dedent("""
            import os, sys
            if len(sys.argv) == 1:
                print("before", os.getpid(), flush=True)
                os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), "again"])
            print("after", os.getpid(), flush=True)
        """)
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "reexec.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(code)
            proc = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = dict(ln.split() for ln in proc.stdout.splitlines() if ln.strip())
        self.assertEqual(lines["before"], lines["after"], "a reload by execv must never change the pid")


if __name__ == "__main__":
    unittest.main()
