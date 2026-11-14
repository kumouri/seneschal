#!/usr/bin/env python3
"""Tests for service.py — the `seneschald` verbs over systemd, launchd and Windows Task Scheduler.

Each backend is driven through a FAKE runner that records every argv and answers the way the real tool
does, so all three platforms' verb mappings run on every OS CI uses (docs/service-supervision-spec.md §3).
What a fake cannot prove — that a real supervisor and a real daemon are found by their kernel locks and
stopped by verified pid — runs against REAL processes in `RealStop`, on whatever OS runs the suite.

Pinned:
* install: render, then the platform's activation (systemd daemon-reload + enable --now + linger,
  launchd bootstrap of both agents, Windows register-tasks.ps1 under ONE elevation — none for
  --user-level or an already-elevated token, none at all for --print-only; a declined prompt is
  EXIT_DENIED);
* start clears the stop / crash-loop sentinels and the boot window (and systemd's start limit);
* stop writes the stop sentinel first; on Windows it is the graceful supervised Stop: shutdown --now,
  wait, schtasks /End, kill only VERIFIED pids, withdraw its own queued shutdown;
* a refusal from Task Scheduler (the UAC-filtered token's answer) is EXIT_DENIED with the exact elevated
  command, never silence; uninstall retries a refused delete under one elevation;
* restart is the graceful control-queue request; --hard is stop + start;
* status / logs read the managers and the supervisor's report.

Run: python -m unittest test_service
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
from pathlib import Path
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import request_control  # noqa: E402
import service as svc  # noqa: E402
import single_instance as si  # noqa: E402

BASE_PY = getattr(sys, "_base_executable", None) or sys.executable


class FakeRunner:
    """Answers by argv prefix; records every call. `elevated_calls` records ShellExecuteEx launches."""

    def __init__(self, answers=None, on_elevated=None):
        self.answers = list(answers or [])   # [(prefix tuple, Result)]
        self.calls: list = []
        self.elevated_calls: list = []
        self.on_elevated = on_elevated

    def run(self, argv, timeout=60.0):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        for prefix, result in self.answers:
            if tuple(argv[:len(prefix)]) == tuple(prefix):
                return result
        return svc.Result(0, "", "")

    def run_elevated(self, exe, params, *, elevate, hidden=True, timeout=600):
        self.elevated_calls.append((exe, params, elevate))
        if self.on_elevated:
            return self.on_elevated(exe, params, elevate)
        return svc.Result(0)

    def called(self, *prefix) -> bool:
        return any(tuple(c[:len(prefix)]) == prefix for c in self.calls)


def args(verb, **kw):
    ns = svc.build_parser().parse_args([verb])
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class Base(unittest.TestCase):
    platform = "linux"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.state = self.repo / "seneschal" / "state"
        self.state.mkdir(parents=True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.lines: list = []
        self.kills: list = []
        self.holders: dict = {}   # lock basename -> {"held", "pid"}
        self.alive_pids: set = set()
        self.runner = FakeRunner()
        self.render_calls: list = []
        patcher = mock.patch.object(svc.ru, "_main", side_effect=self._fake_render)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_render(self, argv):
        self.render_calls.append(argv)
        return 0

    def ctx(self, **kw) -> svc.Ctx:
        opts = dict(platform=self.platform, repo=self.repo, state_dir=self.state, home=self.home,
                    runner=self.runner, out=self.lines.append, sleep=lambda s: None,
                    monotonic=self._clock, uid=501, user="owner",
                    kill=lambda pid: self.kills.append(pid) or True,
                    alive=lambda pid: pid in self.alive_pids,
                    holder=lambda path: self.holders.get(os.path.basename(path), {"held": False, "pid": None}),
                    elevated=lambda: False)
        opts.update(kw)
        return svc.Ctx(**opts)

    _t = 0.0

    def _clock(self):
        self._t += 1.0
        return self._t

    def touch(self, name):
        (self.state / name).write_text("", encoding="utf-8")

    def text(self) -> str:
        return "\n".join(self.lines)


# ------------------------------------------------------------------------------------------ shared
class SharedVerbs(Base):
    def test_start_clears_the_sentinels_and_the_boot_window(self):
        for name in (svc.STOPPED_SENTINEL, svc.CRASHLOOP_SENTINEL, svc.BOOT_ATTEMPTS):
            self.touch(name)
        svc.clear_for_start(self.ctx())
        for name in (svc.STOPPED_SENTINEL, svc.CRASHLOOP_SENTINEL, svc.BOOT_ATTEMPTS):
            self.assertFalse((self.state / name).exists(), name)

    def test_graceful_restart_is_the_control_queue(self):
        rc = svc.run_verb(self.ctx(), args("restart"))
        self.assertEqual(rc, svc.EXIT_OK)
        q = json.loads((self.state / "control-queue.json").read_text(encoding="utf-8"))
        self.assertEqual([(i["action"], i["defer_until_idle"]) for i in q], [("restart", True)])
        self.assertEqual(self.runner.calls, [], "a graceful restart never touches the service manager")

    def test_restart_now_skips_the_idle_wait(self):
        svc.run_verb(self.ctx(), args("restart", now=True))
        q = json.loads((self.state / "control-queue.json").read_text(encoding="utf-8"))
        self.assertFalse(q[0]["defer_until_idle"])

    def test_hard_restart_is_stop_then_start(self):
        svc.run_verb(self.ctx(), args("restart", hard=True))
        verbs = [c[2] for c in self.runner.calls if c[:2] == ["systemctl", "--user"]]
        self.assertEqual(verbs[0], "stop")
        self.assertEqual(verbs[-1], "start")
        self.assertFalse((self.state / svc.STOPPED_SENTINEL).exists(), "start lifts the stop it follows")

    def test_logs_tail_the_daemon_log(self):
        (self.state / svc.PRESENCE_LOG).write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
        svc.run_verb(self.ctx(), args("logs", lines=3))
        self.assertIn("line 99", self.text())
        self.assertNotIn("line 96", self.text())

    def test_verified_supervisor_pid_needs_the_lock_holder_to_match(self):
        (self.state / svc.SUPERVISOR_REPORT).write_text(json.dumps({"pid": 4242}), encoding="utf-8")
        self.assertIsNone(svc.verified_supervisor_pid(self.ctx()), "no holder")
        self.holders[svc.SUPERVISOR_LOCK] = {"held": True, "pid": 999}
        self.assertIsNone(svc.verified_supervisor_pid(self.ctx()), "a recycled pid in the report")
        self.holders[svc.SUPERVISOR_LOCK] = {"held": True, "pid": 4242}
        self.assertEqual(svc.verified_supervisor_pid(self.ctx()), 4242)

    def test_cross_platform_verbs_are_refused(self):
        other = "linux" if svc.ru.default_platform() != "linux" else "darwin"
        with mock.patch("builtins.print"):
            self.assertEqual(svc.main(["stop", "--platform", other, "--state-dir", str(self.state)]),
                             svc.EXIT_USAGE)


# ------------------------------------------------------------------------------------------ systemd
class Systemd(Base):
    platform = "linux"

    def test_install_renders_enables_and_lingers(self):
        self.runner.answers = [(("loginctl", "show-user"), svc.Result(0, "Linger=no\n"))]
        rc = svc.run_verb(self.ctx(), args("install"))
        self.assertEqual(rc, svc.EXIT_OK)
        self.assertIn("--install", self.render_calls[0])
        self.assertIn("--apply", self.render_calls[0])
        self.assertTrue(self.runner.called("systemctl", "--user", "daemon-reload"))
        self.assertTrue(self.runner.called("systemctl", "--user", "enable", "--now", "seneschald.service",
                                           "seneschald-update.timer"))
        self.assertTrue(self.runner.called("loginctl", "enable-linger", "owner"))

    def test_linger_needing_root_prints_the_one_sudo(self):
        self.runner.answers = [(("loginctl", "show-user"), svc.Result(0, "Linger=no\n")),
                               (("loginctl", "enable-linger"), svc.Result(1, "", "Access denied"))]
        self.assertEqual(svc.run_verb(self.ctx(), args("install")), svc.EXIT_OK)
        self.assertIn("sudo loginctl enable-linger owner", self.text())

    def test_linger_already_on_is_left_alone(self):
        self.runner.answers = [(("loginctl", "show-user"), svc.Result(0, "Linger=yes\n"))]
        svc.run_verb(self.ctx(), args("install"))
        self.assertFalse(self.runner.called("loginctl", "enable-linger"))

    def test_failed_enable_is_a_failure(self):
        self.runner.answers = [(("systemctl", "--user", "enable"), svc.Result(1, "", "Unit not found"))]
        self.assertEqual(svc.run_verb(self.ctx(), args("install")), svc.EXIT_FAILED)

    def test_start_resets_the_start_limit_then_starts(self):
        self.touch(svc.CRASHLOOP_SENTINEL)
        self.assertEqual(svc.run_verb(self.ctx(), args("start")), svc.EXIT_OK)
        calls = [c[2:] for c in self.runner.calls]
        self.assertEqual(calls, [["reset-failed", "seneschald.service"], ["start", "seneschald.service"]])
        self.assertFalse((self.state / svc.CRASHLOOP_SENTINEL).exists())

    def test_stop_writes_the_sentinel_then_stops(self):
        self.assertEqual(svc.run_verb(self.ctx(), args("stop")), svc.EXIT_OK)
        self.assertTrue((self.state / svc.STOPPED_SENTINEL).exists())
        self.assertEqual(self.runner.calls, [["systemctl", "--user", "stop", "seneschald.service"]])

    def test_uninstall_disables_and_removes_the_units(self):
        units = self.home / ".config" / "systemd" / "user"
        units.mkdir(parents=True)
        for name in ("seneschald.service", "seneschald-update.service", "seneschald-update.timer"):
            (units / name).write_text("x", encoding="utf-8")
        self.assertEqual(svc.run_verb(self.ctx(), args("uninstall")), svc.EXIT_OK)
        self.assertTrue(self.runner.called("systemctl", "--user", "disable", "--now"))
        self.assertEqual(list(units.iterdir()), [])

    def test_status_reads_systemctl_show(self):
        self.runner.answers = [(("systemctl", "--user", "show"),
                                svc.Result(0, "ActiveState=active\nMainPID=321\nNRestarts=2\n"))]
        svc.run_verb(self.ctx(), args("status", json=True))
        st = json.loads(self.text())
        self.assertEqual(st["properties"]["NRestarts"], "2")
        self.assertEqual(st["manager"], "systemd")


# ------------------------------------------------------------------------------------------ launchd
class Launchd(Base):
    platform = "darwin"

    def plist(self):
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        p = agents / "com.seneschal.presence.plist"
        p.write_text("x", encoding="utf-8")
        return p

    def test_install_bootstraps_both_agents_into_the_gui_domain(self):
        self.runner.answers = [(("launchctl", "print"), svc.Result(113, "", "Could not find service"))]
        self.assertEqual(svc.run_verb(self.ctx(), args("install")), svc.EXIT_OK)
        boots = [c for c in self.runner.calls if c[1] == "bootstrap"]
        self.assertEqual([c[2] for c in boots], ["gui/501", "gui/501"])
        self.assertTrue(boots[0][3].endswith("com.seneschal.presence.plist"))
        self.assertTrue(boots[1][3].endswith("com.seneschal.update.plist"))
        self.assertFalse(any(c[1] == "bootout" for c in self.runner.calls))

    def test_reinstall_boots_out_first(self):
        svc.run_verb(self.ctx(), args("install"))
        self.assertTrue(self.runner.called("launchctl", "bootout", "gui/501/com.seneschal.presence"))

    def test_stop_boots_out_because_keepalive_would_respawn_a_kill(self):
        self.assertEqual(svc.run_verb(self.ctx(), args("stop")), svc.EXIT_OK)
        self.assertTrue((self.state / svc.STOPPED_SENTINEL).exists())
        self.assertTrue(self.runner.called("launchctl", "bootout", "gui/501/com.seneschal.presence"))

    def test_start_kickstarts_a_loaded_agent(self):
        svc.run_verb(self.ctx(), args("start"))
        self.assertTrue(self.runner.called("launchctl", "kickstart", "gui/501/com.seneschal.presence"))

    def test_start_after_stop_bootstraps(self):
        p = self.plist()
        self.runner.answers = [(("launchctl", "print"), svc.Result(113))]
        self.assertEqual(svc.run_verb(self.ctx(), args("start")), svc.EXIT_OK)
        self.assertTrue(self.runner.called("launchctl", "bootstrap", "gui/501", str(p)))

    def test_start_without_a_plist_is_not_installed(self):
        self.runner.answers = [(("launchctl", "print"), svc.Result(113))]
        self.assertEqual(svc.run_verb(self.ctx(), args("start")), svc.EXIT_NOT_INSTALLED)

    def test_status_parses_launchctl_print(self):
        self.runner.answers = [(("launchctl", "print"), svc.Result(0, "\tstate = running\n\tpid = 77\n"
                                                                      "\truns = 3\n\tlast exit code = 1\n"))]
        svc.run_verb(self.ctx(), args("status", json=True))
        st = json.loads(self.text())
        self.assertEqual(st["properties"], {"state": "running", "pid": "77", "runs": "3", "last exit code": "1"})


# ------------------------------------------------------------------------------------------ windows
DENIED = svc.Result(1, "", "ERROR: Access is denied.")


class Windows(Base):
    platform = "win32"

    def report(self, pid=4242, **extra):
        rep = {"pid": pid, "heartbeat": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "state": "running",
               "restarts": {"reload": 1, "crash": 0}, "crash_budget": {"left": 5}}
        rep.update(extra)
        (self.state / svc.SUPERVISOR_REPORT).write_text(json.dumps(rep), encoding="utf-8")

    def supervised(self, sup=4242, daemon=5151):
        self.report(sup)
        self.holders[svc.SUPERVISOR_LOCK] = {"held": True, "pid": sup}
        self.holders[si.INSTANCE_LOCK_NAME] = {"held": True, "pid": daemon}
        self.alive_pids |= {sup, daemon}

    # ---- install / uninstall
    def write_result(self, ok=True):
        def on_elevated(exe, params, elevate):
            d = self.repo / "seneschal" / "state" / "setup"
            d.mkdir(parents=True, exist_ok=True)
            (d / "register-tasks.result.json").write_text(json.dumps(
                {"tasks": [{"task": "seneschald", "ok": ok, "detail": "registered"}]}), encoding="utf-8")
            return svc.Result(0 if ok else 1)
        return on_elevated

    def test_install_runs_the_register_script_under_one_elevation(self):
        self.runner.on_elevated = self.write_result()
        self.assertEqual(svc.run_verb(self.ctx(), args("install")), svc.EXIT_OK)
        self.assertEqual(len(self.runner.elevated_calls), 1)
        exe, params, elevate = self.runner.elevated_calls[0]
        self.assertTrue(elevate, "a filtered token gets exactly one UAC prompt")
        self.assertIn("register-tasks.ps1", params)
        self.assertNotIn("--install", self.render_calls[0])
        self.assertRegex(self.text(), r"ok\s+seneschald - registered")

    def test_install_from_an_elevated_shell_does_not_prompt(self):
        self.runner.on_elevated = self.write_result()
        svc.run_verb(self.ctx(elevated=lambda: True), args("install"))
        self.assertFalse(self.runner.elevated_calls[0][2])

    def test_user_level_install_never_elevates(self):
        self.runner.on_elevated = self.write_result()
        svc.run_verb(self.ctx(), args("install", user_level=True))
        self.assertFalse(self.runner.elevated_calls[0][2])
        self.assertIn("--user-level", self.render_calls[0])

    def test_print_only_runs_nothing(self):
        self.assertEqual(svc.run_verb(self.ctx(), args("install", print_only=True)), svc.EXIT_OK)
        self.assertEqual(self.runner.elevated_calls, [])
        self.assertIn("register-tasks.ps1", self.text())

    def test_a_declined_prompt_is_denied(self):
        self.runner.on_elevated = lambda *a: svc.Result(None, "", "the UAC prompt was declined")
        self.assertEqual(svc.run_verb(self.ctx(), args("install")), svc.EXIT_DENIED)

    def test_a_failed_registration_is_a_failure(self):
        self.runner.on_elevated = self.write_result(ok=False)
        self.assertEqual(svc.run_verb(self.ctx(), args("install")), svc.EXIT_FAILED)
        self.assertIn("FAIL", self.text())

    def test_uninstall_retries_a_refused_delete_once_elevated(self):
        self.runner.answers = [(("schtasks", "/Delete"), DENIED)]
        self.assertEqual(svc.run_verb(self.ctx(), args("uninstall")), svc.EXIT_OK)
        self.assertEqual(len(self.runner.elevated_calls), 1)
        exe, params, elevate = self.runner.elevated_calls[0]
        self.assertTrue(elevate)
        self.assertIn("/TN seneschald /F", params)
        self.assertIn("/TN seneschald-update /F", params)

    # ---- start
    def test_start_runs_the_task(self):
        self.touch(svc.STOPPED_SENTINEL)
        self.assertEqual(svc.run_verb(self.ctx(), args("start")), svc.EXIT_OK)
        self.assertEqual(self.runner.calls, [["schtasks", "/Run", "/TN", "seneschald"]])
        self.assertFalse((self.state / svc.STOPPED_SENTINEL).exists())

    def test_a_refused_start_is_loud_with_the_elevated_command(self):
        """The UAC-on answer: a filtered token may not run an admin-registered task. It must say so and
        name the command, never fail quietly, and never prompt on its own."""
        self.runner.answers = [(("schtasks", "/Run"), DENIED)]
        self.assertEqual(svc.run_verb(self.ctx(), args("start")), svc.EXIT_DENIED)
        self.assertIn("schtasks /Run /TN seneschald", self.text())
        self.assertIn("elevated shell", self.text())
        self.assertEqual(self.runner.elevated_calls, [])

    def test_start_of_an_unregistered_task(self):
        self.runner.answers = [(("schtasks", "/Run"), svc.Result(1, "", "ERROR: The system cannot find the file specified."))]
        self.assertEqual(svc.run_verb(self.ctx(), args("start")), svc.EXIT_NOT_INSTALLED)

    # ---- stop
    def test_supervised_stop_is_graceful_first(self):
        self.supervised()

        def consume(pid):
            # The daemon reads the queued shutdown, exits 0; its supervisor exits 0 behind it.
            if request_control.queued(str(self.state), "shutdown", svc.STOP_REASON):
                request_control.withdraw_control(str(self.state), "shutdown", svc.STOP_REASON)  # consumed
                self.alive_pids.clear()
                self.holders.clear()
            return pid in self.alive_pids

        self.assertEqual(svc.run_verb(self.ctx(alive=consume), args("stop")), svc.EXIT_OK)
        self.assertTrue((self.state / svc.STOPPED_SENTINEL).exists())
        self.assertEqual(self.kills, [], "a clean exit leaves nothing to kill")
        self.assertTrue(self.runner.called("schtasks", "/End", "/TN", "seneschald"))
        self.assertIn("shut down cleanly", self.text())

    def test_a_shutdown_that_never_lands_takes_the_hard_path_and_is_withdrawn(self):
        self.supervised()

        def kill(pid):
            self.kills.append(pid)
            self.alive_pids.discard(pid)
            if pid == 4242:
                self.holders.pop(svc.SUPERVISOR_LOCK, None)
            if pid == 5151:
                self.holders.pop(si.INSTANCE_LOCK_NAME, None)
            return True

        self.assertEqual(svc.run_verb(self.ctx(kill=kill), args("stop")), svc.EXIT_OK)
        self.assertEqual(self.kills, [4242, 5151])
        self.assertFalse(request_control.queued(str(self.state), "shutdown", svc.STOP_REASON),
                         "the next start must not read Stop's leftover shutdown and exit at once")

    def test_another_callers_shutdown_is_never_withdrawn(self):
        self.supervised()
        request_control.enqueue_control(str(self.state), "shutdown", "someone else")
        svc.run_verb(self.ctx(), args("stop"))
        self.assertTrue(request_control.queued(str(self.state), "shutdown", "someone else"))

    def test_unsupervised_stop_queues_nothing_and_kills_the_lock_holder(self):
        self.holders[si.INSTANCE_LOCK_NAME] = {"held": True, "pid": 5151}
        self.alive_pids.add(5151)

        def kill(pid):
            self.kills.append(pid)
            self.holders.clear()
            return True

        self.assertEqual(svc.run_verb(self.ctx(kill=kill), args("stop")), svc.EXIT_OK)
        self.assertFalse((self.state / "control-queue.json").exists())
        self.assertEqual(self.kills, [5151])

    def test_an_unverified_supervisor_pid_is_never_killed(self):
        self.report(4242)
        self.holders[svc.SUPERVISOR_LOCK] = {"held": True, "pid": 999}  # the report's pid was recycled
        svc.run_verb(self.ctx(), args("stop"))
        self.assertNotIn(4242, self.kills)
        self.assertFalse((self.state / "control-queue.json").exists(), "no graceful path for a stranger")

    def test_a_refused_end_with_a_survivor_is_denied(self):
        self.supervised()
        self.runner.answers = [(("schtasks", "/End"), DENIED)]
        rc = svc.run_verb(self.ctx(kill=lambda pid: False), args("stop"))
        self.assertEqual(rc, svc.EXIT_DENIED)
        self.assertIn("schtasks /End /TN seneschald", self.text())

    def test_stop_removes_the_advisory_lock(self):
        self.touch("presence.lock")
        svc.run_verb(self.ctx(), args("stop"))
        self.assertFalse((self.state / "presence.lock").exists())

    # ---- status
    def test_status_reads_the_task_and_the_supervisor(self):
        self.supervised()
        self.runner.answers = [(("schtasks", "/Query"), svc.Result(0, "Status:        Running\nLast Result:   0\n"))]
        svc.run_verb(self.ctx(), args("status", json=True))
        st = json.loads(self.text())
        self.assertEqual(st["properties"]["Status"], "Running")
        self.assertEqual(st["supervisor"]["state"], "running")
        self.assertEqual(st["supervisor"]["verified_pid"], 4242)
        self.assertEqual(st["daemon_pid"], 5151)

    def test_a_stale_report_is_down(self):
        self.report(4242, heartbeat="2020-01-01T00:00:00Z")
        st = svc.supervisor_summary(self.ctx())
        self.assertEqual(st["state"], "down")

    def test_logs_tail_the_child_log(self):
        (self.state / "logs").mkdir()
        (self.state / svc.CHILD_LOG).write_text("[supervisor] child exited 75\n", encoding="utf-8")
        svc.run_verb(self.ctx(), args("logs"))
        self.assertIn("child exited 75", self.text())


# ------------------------------------------------------------------------------------------ real processes
_LOCK_HOLDING_CHILD = textwrap.dedent(r'''
    import os, sys, time
    sys.path.insert(0, os.environ["SI_DIR"])
    import single_instance as si
    lock = si.InstanceLock(si.instance_lock_path(os.environ["STATE_DIR"]))
    if not lock.acquire(wait_sec=10):
        sys.exit(69)
    time.sleep(120)
''')


class RealStop(unittest.TestCase):
    """A real supervisor (seneschald_supervise.py, hand-run outside any task) supervising a real child
    that holds presence.instance.lock — then the Windows Stop, with the REAL kernel-lock holder probe and
    real kills. Task Scheduler itself is faked as "no such task" (a hand run is not the task's), so this
    proves the part a fake cannot: the pids Stop kills are found by their kernel locks, and nothing is
    left running. Runs on every OS: off Windows there is no job object, so the daemon is killed by its
    own verified pid rather than through the supervisor's job."""

    def test_stop_ends_a_real_supervisor_and_its_daemon(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        state = root / "state"
        state.mkdir()
        child = root / "child.py"
        child.write_text(_LOCK_HOLDING_CHILD, encoding="utf-8")
        args_file = root / "empty.args"
        args_file.write_text("", encoding="utf-8")
        env = dict(os.environ, SI_DIR=SCRIPT_DIR, STATE_DIR=str(state))
        env.pop("SENESCHALD_SUPERVISED", None)
        sup = subprocess.Popen([BASE_PY, os.path.join(SCRIPT_DIR, "seneschald_supervise.py"),
                                "--state-dir", str(state), "--script", str(child), "--args-file", str(args_file),
                                "--python", BASE_PY],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=0x08000000 if os.name == "nt" else 0)
        self.addCleanup(lambda: sup.poll() is None and sup.kill())
        daemon_lock = si.instance_lock_path(str(state))
        deadline = time.monotonic() + 30
        while not si.is_held(daemon_lock) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(si.is_held(daemon_lock), "the child never took the daemon's lock")
        daemon_pid = si.holder(daemon_lock)["pid"]

        lines: list = []
        runner = FakeRunner([(("schtasks",), svc.Result(1, "", "ERROR: The system cannot find the file specified."))])
        ctx = svc.Ctx(platform="win32", repo=root, state_dir=state, home=root, runner=runner, out=lines.append)
        self.assertEqual(svc.verified_supervisor_pid(ctx), sup.pid)
        with mock.patch.object(svc, "STOP_GRACEFUL_WAIT_SEC", 1.0):
            rc = svc.WindowsBackend(ctx).stop(args("stop"))
        self.assertEqual(rc, svc.EXIT_OK, "\n".join(lines))
        sup.wait(timeout=20)
        deadline = time.monotonic() + 20
        while svc.pid_alive(daemon_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(svc.pid_alive(daemon_pid), "the daemon must not outlive Stop")
        self.assertFalse(si.is_held(daemon_lock))
        self.assertFalse(si.is_held(str(state / svc.SUPERVISOR_LOCK)))
        self.assertTrue((state / svc.STOPPED_SENTINEL).exists())
        self.assertFalse(request_control.queued(str(state), "shutdown", svc.STOP_REASON))


class EntryShims(unittest.TestCase):
    def test_both_shims_call_service_py(self):
        for name in ("seneschald", "seneschald.cmd"):
            with open(os.path.join(SCRIPT_DIR, name), encoding="utf-8") as fh:
                self.assertIn("service.py", fh.read(), name)


if __name__ == "__main__":
    unittest.main()
