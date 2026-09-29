#!/usr/bin/env python3
"""Tests for jobs.py (durable background jobs — detached survivors + the daemon-owned completion push),
watch_pr.py (the first consumer), and presence.py's wiring of both (the scheduler tick's reconcile, the
drainer's lease guard).

No real process spawns, no real network, no real clock: `start_job` takes an injectable `runner`,
`reconcile` takes `notify`/`wake`/`now`, `pid_alive` is monkeypatched, and `watch_pr` takes
`runner`/`sleep`/`clock`/`out`.

**The invariant most of this file exists to pin down: NO SILENT PATH.** Every way a job can end must
produce exactly one notification, including the ugly ones (killed shim, blown deadline, a daemon that
restarted mid-job). A regression that loses a ping is the entire bug this feature was built for.

**The second invariant, added with `--retry`: CONSERVATIVE CLASSIFICATION.** A retry that
fires on a genuinely broken command re-runs its damage N times, so `ClassifierTests` exists to hold the
line that **unrecognised output is terminal, first time** — the verbatim `API Error: 500` line retries,
a test failure does not, and the patterns this repo's own output contains (`notion-rate-limits.md`,
"test timed out", "429 tests") deliberately do not. Speed corroborates and never decides.

The retry lifecycle is exercised through the REAL shim (`jobs._run_shim`) with a fake child process, so
the classify → schedule → stamp path is covered end-to-end without spawning anything: only
`subprocess.Popen` is faked, and the log offsets, the per-attempt delimiter and the on-disk record are
all the real ones.

Run: python -m unittest discover -s seneschal/scripts -p "test_jobs.py"   (or)   python test_jobs.py
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import job_completion  # noqa: E402
import jobs  # noqa: E402
import loops  # noqa: E402 — `--loop <id>`
import presence as pr  # noqa: E402
import sentinel  # noqa: E402

try:  # the CI watcher lands in a later wave; its tests skip until then
    import watch_pr  # noqa: E402
except ImportError:  # pragma: no cover - depends on the wave
    watch_pr = None
from test_job_worktree import _FakeGit  # noqa: E402 — the git seam, defined once, next to the
                                        # tests that assert the command sequence itself

NOW = datetime(2026, 7, 28, 20, 0, 0, tzinfo=timezone.utc)

# The daemon-side wiring (the scheduler tick's reconcile, the wake enqueue) lands with the newer
# presence.py; the tests that drive it skip until those functions exist.
_PRESENCE_SKIP = "presence wiring lands in wave 26"

# The verbatim transient line `--retry` was built for — a job that died in 1 second with 161 bytes
# and nothing else in the log. It is a test fixture, not a paraphrase.
API_500 = ("API Error: 500 Internal server error. This is a server-side issue, usually temporary — "
           "try again in a moment.")


class _FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid


def _runner(pid=4321, calls=None):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        return _FakeProc(pid)
    return run


def _cwd_checking_runner(pid=4321, calls=None):
    """`_runner`, except it fails the way the REAL `subprocess.Popen` fails when `cwd` is gone.

    Every other runner in this file ignores `cwd` entirely, which is exactly why an
    `--analyze --worktree` bug was once invisible to the suite: the analysis job was handed a directory
    that had just been torn down and the fake spawned it happily. The message is the one the live
    record carried — `"spawn failed: [WinError 267] The directory name is invalid"`."""
    def run(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        cwd = kwargs.get("cwd")
        if cwd and not os.path.isdir(cwd):
            raise OSError(f"[WinError 267] The directory name is invalid: {cwd}")
        return _FakeProc(pid)
    return run


class _FakeChild:
    """The grandchild the shim runs: writes `text` to whatever handle it was given, then exits `code`.
    Faking exactly this and nothing else is what lets the retry tests drive the REAL `_run_shim`."""

    def __init__(self, code=0, text="", out=None):
        self.pid = 777
        self._code = code
        if text and hasattr(out, "write"):
            out.write(text + "\n")
            out.flush()

    def wait(self, timeout=None):
        if self._code == "timeout":
            raise __import__("subprocess").TimeoutExpired(cmd="x", timeout=timeout)
        return self._code

    def kill(self):
        pass


def _no_jitter():
    """`backoff_delay`'s `rand` seam pinned to the midpoint, so the jitter contributes exactly 0 and
    the schedule is assertable to the second. Jitter that can't be pinned can't be regression-tested."""
    return 0.5


class _Recorder:
    """A `notify` seam that records every push and can be told to fail (a Telegram outage)."""

    def __init__(self, landed=True):
        self.landed = landed
        self.sent: list = []

    def __call__(self, channel, text):
        self.sent.append((channel, text))
        return self.landed


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _start(self, title="a job", **kw):
        kw.setdefault("runner", _runner())
        kw.setdefault("now", NOW)
        return jobs.start_job(self.state, title, ["python", "-c", "pass"], **kw)

    # ---------------------------------------------------------------- spawn

    def test_start_writes_record_and_spawns_detached(self):
        calls: list = []
        rec = self._start(runner=_runner(calls=calls))
        self.assertEqual(rec["status"], jobs.RUNNING)
        self.assertEqual(rec["pid"], 4321)
        self.assertEqual(jobs.load_job(self.state, rec["id"])["title"], "a job")
        argv, kwargs = calls[0]
        # The shim, not the caller's argv — and --state-dir MUST precede the subcommand (a top-level
        # argparse argument after `__run` is a usage error, which is how the first smoke test failed).
        self.assertIn("__run", argv)
        self.assertLess(argv.index("--state-dir"), argv.index("__run"))
        self.assertEqual(argv[-1], rec["id"])
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"],
                             jobs._DETACHED_PROCESS | jobs._CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(kwargs["start_new_session"])
        # Asserted against the key LIST, never the dict itself — a failure here must never render
        # the full env (and whatever real secrets happen to sit in this host's shell) into the log.
        self.assertNotIn("ANTHROPIC_API_KEY", sorted(kwargs["env"].keys()))  # subscription billing rule

    def test_spawn_failure_is_a_terminal_notifiable_job_not_a_silent_noop(self):
        def boom(argv, **kwargs):
            raise OSError("no such executable")
        rec = self._start(runner=boom)
        self.assertEqual(rec["status"], jobs.FAILED)
        notify = _Recorder()
        self.assertEqual(len(jobs.reconcile(self.state, notify=notify, now=NOW)), 1)

    def test_child_env_scrubs_api_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-nope"}):
            self.assertNotIn("ANTHROPIC_API_KEY", sorted(jobs.child_env().keys()))

    # ---------------------------------------------------------------- terminal detection

    def test_running_job_with_live_pid_is_not_terminal(self):
        rec = self._start()
        with mock.patch.object(jobs, "pid_alive", return_value=True):
            self.assertIsNone(jobs.check_terminal(rec, NOW + timedelta(seconds=30)))

    def test_dead_pid_without_an_exit_stamp_is_ended_unknown(self):
        rec = self._start()
        with mock.patch.object(jobs, "pid_alive", return_value=False):
            changes = jobs.check_terminal(rec, NOW + timedelta(seconds=30))
        self.assertEqual(changes["status"], jobs.ENDED_UNKNOWN)

    def test_deadline_beats_a_still_live_pid(self):
        rec = self._start(deadline_sec=60)
        with mock.patch.object(jobs, "pid_alive", return_value=True):
            changes = jobs.check_terminal(rec, NOW + timedelta(seconds=61))
        self.assertEqual(changes["status"], jobs.TIMED_OUT)

    def test_a_running_record_with_no_pid_fails_after_the_startup_grace(self):
        """The spawn died between writing the record and getting a PID. Without this it would sit
        `running` until the 6 h deadline."""
        rec = self._start()
        rec["pid"] = None
        self.assertIsNone(jobs.check_terminal(rec, NOW + timedelta(seconds=5)))
        changes = jobs.check_terminal(rec, NOW + timedelta(seconds=jobs.STARTUP_GRACE_SEC + 1))
        self.assertEqual(changes["status"], jobs.ENDED_UNKNOWN)

    def test_a_garbled_ended_at_still_produces_a_push(self):
        """A cosmetic corruption must not make a job permanently silent: notify_text raising would
        be caught by reconcile's per-record guard and re-raise on every subsequent pass."""
        rec = self._start(title="garbled")
        rec.update({"status": jobs.DONE, "exit_code": 0, "ended_at": "not-a-timestamp"})
        jobs.save_job(self.state, rec)
        self.assertIn("garbled", jobs.notify_text(rec))
        notify = _Recorder()
        self.assertEqual(len(jobs.reconcile(self.state, notify=notify, now=NOW)), 1)

    def test_already_terminal_records_are_left_alone(self):
        rec = self._start()
        rec["status"] = jobs.DONE
        self.assertIsNone(jobs.check_terminal(rec, NOW + timedelta(days=9)))

    def test_pid_alive_is_conservative_when_it_cannot_answer(self):
        """A probe that can't answer must read ALIVE — a false "it ended" fires a premature, wrong
        ping. The deadline is what guarantees the job still eventually reports."""
        with mock.patch("os.name", "posix"), \
             mock.patch("os.kill", side_effect=PermissionError("EPERM")):
            self.assertTrue(jobs.pid_alive(1234))
        with mock.patch("os.name", "posix"), \
             mock.patch("os.kill", side_effect=ProcessLookupError()):
            self.assertFalse(jobs.pid_alive(1234))
        self.assertFalse(jobs.pid_alive(None))
        self.assertFalse(jobs.pid_alive("not-a-pid"))

    # ---------------------------------------------------------------- the no-silent-path invariant

    def test_every_terminal_path_notifies_exactly_once(self):
        cases = {
            jobs.DONE: {"exit_code": 0},
            jobs.FAILED: {"exit_code": 3},
            jobs.TIMED_OUT: {},
            jobs.ENDED_UNKNOWN: {},
            jobs.CANCELLED: {},
        }
        for status, extra in cases.items():
            with self.subTest(status=status):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                rec = jobs.start_job(tmp.name, f"job {status}", ["x"], runner=_runner(), now=NOW)
                rec.update({"status": status, "ended_at": jobs._stamp(NOW), **extra})
                jobs.save_job(tmp.name, rec)
                notify = _Recorder()
                first = jobs.reconcile(tmp.name, notify=notify, now=NOW)
                second = jobs.reconcile(tmp.name, notify=notify, now=NOW)
                self.assertEqual(len(first), 1, f"{status} did not notify")
                self.assertEqual(second, [], f"{status} notified twice")
                self.assertEqual(len(notify.sent), 1)

    def test_a_failed_send_does_not_stamp_notified_and_retries_next_tick(self):
        """Fail-CLOSED, deliberately — the one place in this module that isn't fail-open. A Telegram
        outage must not eat the ping."""
        rec = self._start()
        rec.update({"status": jobs.DONE, "exit_code": 0, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, rec)

        down = _Recorder(landed=False)
        self.assertEqual(jobs.reconcile(self.state, notify=down, now=NOW), [])
        self.assertIsNone(jobs.load_job(self.state, rec["id"])["notified_at"])

        back_up = _Recorder(landed=True)
        self.assertEqual(len(jobs.reconcile(self.state, notify=back_up, now=NOW)), 1)
        self.assertIsNotNone(jobs.load_job(self.state, rec["id"])["notified_at"])

    def test_a_daemon_restart_mid_job_still_lands_the_ping(self):
        """The whole point: the promise lives on disk, so a successor daemon that never saw the job
        start still reports it. Simulated by reconciling a record written by an 'earlier' run whose
        PID is now gone."""
        rec = self._start(title="survives a reload")
        notify = _Recorder()
        with mock.patch.object(jobs, "pid_alive", return_value=False):
            notified = jobs.reconcile(self.state, notify=notify, now=NOW + timedelta(minutes=5))
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0]["status"], jobs.ENDED_UNKNOWN)
        self.assertIn("survives a reload", notify.sent[0][1])

    def test_one_corrupt_record_does_not_stop_the_others(self):
        good = self._start(title="fine")
        good.update({"status": jobs.DONE, "exit_code": 0, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, good)
        with open(os.path.join(jobs.jobs_dir(self.state), "broken.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        notify = _Recorder()
        self.assertEqual(len(jobs.reconcile(self.state, notify=notify, now=NOW)), 1)

    def test_reads_are_tolerant_of_a_missing_store(self):
        empty = os.path.join(self.state, "nope")
        self.assertEqual(jobs.list_jobs(empty), [])
        self.assertIsNone(jobs.load_job(empty, "whatever"))
        self.assertFalse(jobs.lease_active(empty))
        self.assertEqual(jobs.reconcile(empty, notify=_Recorder(), now=NOW), [])

    # ---------------------------------------------------------------- wake

    def test_wake_fires_only_for_wake_jobs_and_only_after_the_push_landed(self):
        plain = self._start(title="plain")
        waker = self._start(title="waker", wake=True)
        for rec in (plain, waker):
            rec.update({"status": jobs.DONE, "exit_code": 0, "ended_at": jobs._stamp(NOW)})
            jobs.save_job(self.state, rec)
        woken: list = []
        jobs.reconcile(self.state, notify=_Recorder(), wake=woken.append, now=NOW)
        self.assertEqual(len(woken), 1)
        self.assertIn("waker", woken[0])

        # A push that never landed must not wake either — the wake is the bonus, not the guarantee.
        another = self._start(title="undelivered", wake=True)
        another.update({"status": jobs.DONE, "exit_code": 0, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, another)
        woken2: list = []
        jobs.reconcile(self.state, notify=_Recorder(landed=False), wake=woken2.append, now=NOW)
        self.assertEqual(woken2, [])

    def test_wake_text_does_not_impersonate_the_owner(self):
        rec = self._start(title="research run", wake=True)
        rec.update({"status": jobs.DONE, "exit_code": 0})
        text = jobs.wake_text(rec)
        self.assertIn("not a message from the owner", text)
        self.assertTrue(text.startswith("[job finished:"))

    def test_wake_text_carries_the_log_tail_so_no_second_lookup_is_needed(self):
        """The whole point of waking the session is that it can report WITHOUT re-opening
        the log — an `ended-unknown` job's own final words are exactly what's needed to say what
        happened, and they must travel with the wake, not just its path."""
        rec = self._start(title="mystery run", wake=True)
        with open(rec["log_path"], "w", encoding="utf-8") as fh:
            fh.write("step 1: ok\nstep 2: ok\nFATAL: the remote host vanished mid-transfer\n")
        rec.update({"status": jobs.ENDED_UNKNOWN, "ended_at": jobs._stamp(NOW)})
        text = jobs.wake_text(rec)
        self.assertIn("FATAL: the remote host vanished mid-transfer", text)
        self.assertIn("step 1: ok", text)

    def test_wake_text_tail_is_capped_and_says_so(self):
        rec = self._start(title="chatty run", wake=True)
        with open(rec["log_path"], "w", encoding="utf-8") as fh:
            fh.writelines(f"line {i:04d}: filler filler filler filler filler\n" for i in range(500))
        rec.update({"status": jobs.DONE, "exit_code": 0, "ended_at": jobs._stamp(NOW)})
        text = jobs.wake_text(rec)
        self.assertIn(f"capped at {jobs._WAKE_TAIL_CHARS} chars", text)
        tail_start = text.index("Log tail")
        self.assertLessEqual(len(text) - tail_start, jobs._WAKE_TAIL_CHARS + 200)
        # The most recent lines survive the cap, not the earliest ones.
        self.assertIn("line 0499", text)

    def test_wake_text_carries_duration_and_an_explicit_no_exit_code(self):
        rec = self._start(title="still-cooking wrapper", wake=True)
        rec.update({"status": jobs.ENDED_UNKNOWN, "exit_code": None,
                   "ended_at": jobs._stamp(NOW + timedelta(seconds=90))})
        text = jobs.wake_text(rec)
        self.assertIn("no exit code", text)
        self.assertIn("1m30s", text)

    def test_notify_text_is_honest_about_an_unreadable_outcome(self):
        rec = self._start(title="mystery")
        rec.update({"status": jobs.ENDED_UNKNOWN, "ended_at": jobs._stamp(NOW)})
        text = jobs.notify_text(rec)
        self.assertIn("can't tell you whether it finished", text)
        self.assertNotIn("✅", text)

    def test_notify_text_quotes_the_logs_last_line(self):
        rec = self._start(title="with a log")
        with open(rec["log_path"], "w", encoding="utf-8") as fh:
            fh.write("noise\nPR #214 CI GREEN — all 7 checks passed\n\n")
        rec.update({"status": jobs.DONE, "exit_code": 0, "ended_at": jobs._stamp(NOW)})
        self.assertIn("CI GREEN", jobs.notify_text(rec))

    # ---------------------------------------------------------------- lease

    def test_lease_holds_only_while_running_and_only_within_the_cap(self):
        rec = self._start(title="leased", lease=True)
        self.assertTrue(jobs.lease_active(self.state, NOW + timedelta(minutes=1)))
        # Past the cap the lease lapses — a wedged job can't pin the session forever.
        self.assertFalse(jobs.lease_active(
            self.state, NOW + timedelta(seconds=jobs.LEASE_MAX_SEC + 1)))
        # A terminal job never holds one.
        rec.update({"status": jobs.DONE, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, rec)
        self.assertFalse(jobs.lease_active(self.state, NOW + timedelta(minutes=1)))

    def test_a_non_lease_job_never_holds_the_session(self):
        self._start(title="detached, unleashed")
        self.assertFalse(jobs.lease_active(self.state, NOW + timedelta(minutes=1)))

    # ---------------------------------------------------------------- cancel & prune

    def test_cancel_kills_both_pids_and_still_notifies(self):
        rec = self._start(title="doomed")
        rec["child_pid"] = 9999
        jobs.save_job(self.state, rec)
        killed: list = []
        with mock.patch.object(jobs, "kill_pid", side_effect=killed.append):
            cancelled = jobs.cancel_job(self.state, rec["id"], now=NOW)
        self.assertEqual(cancelled["status"], jobs.CANCELLED)
        self.assertEqual(sorted(killed), sorted([4321, 9999]))  # shim AND its child
        self.assertEqual(len(jobs.reconcile(self.state, notify=_Recorder(), now=NOW)), 1)

    def test_prune_keeps_terminal_jobs_that_never_got_their_ping(self):
        unping = self._start(title="never reported")
        unping.update({"status": jobs.DONE, "ended_at": jobs._stamp(NOW), "notified_at": None})
        jobs.save_job(self.state, unping)
        told = self._start(title="already reported")
        told.update({"status": jobs.DONE, "ended_at": jobs._stamp(NOW),
                     "notified_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, told)
        removed = jobs.prune(self.state, days=14, now=NOW + timedelta(days=30))
        self.assertEqual(removed, 1)
        self.assertIsNotNone(jobs.load_job(self.state, unping["id"]))  # the ping outlives the GC
        self.assertIsNone(jobs.load_job(self.state, told["id"]))


class OriginTests(unittest.TestCase):
    """Phase 0 of `docs/job-origin-routing-spec.md`: every job carries a return address.

    The load-bearing property is the LAST one — with neither an id nor a goal, `origin` is `{}`, which
    is byte-for-byte what every record written before this feature carries. This feature may add context; it may
    never change what a caller that says nothing gets."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _start(self, **kw):
        kw.setdefault("runner", _runner())
        kw.setdefault("now", NOW)
        return jobs.start_job(self.state, "a job", ["python", "-c", "pass"], **kw)

    def test_auto_stamps_from_the_environment(self):
        """The primary path, and the reason it is primary: `jobs.py start` is typed by a model
        mid-turn, and a flag nobody passes is `metrics.jsonl` all over again."""
        origin = jobs.build_origin(self.state, env={jobs.ORIGIN_ENV_VAR: "sess-abc"})
        self.assertEqual(origin["session_id"], "sess-abc")
        self.assertEqual(origin["stamped_by"], "env")

    def test_the_flag_overrides_the_environment(self):
        origin = jobs.build_origin(self.state, session_id="explicit-one",
                                   env={jobs.ORIGIN_ENV_VAR: "sess-abc"})
        self.assertEqual(origin["session_id"], "explicit-one")
        self.assertEqual(origin["stamped_by"], "flag")

    def test_no_id_and_no_goal_degrades_to_todays_behaviour_exactly(self):
        self.assertEqual(jobs.build_origin(self.state, env={}), {})
        rec = self._start(origin=jobs.build_origin(self.state, env={}))
        self.assertEqual(rec["origin"], {})
        self.assertEqual(jobs.load_job(self.state, rec["id"])["origin"], {})

    def test_a_goal_with_no_session_is_stamped_none(self):
        """Context without an address is still worth keeping — and `stamped_by` says so honestly
        rather than implying a return path that doesn't exist."""
        origin = jobs.build_origin(self.state, goal="find out why the refresh writes zero docs",
                                   env={})
        self.assertNotIn("session_id", origin)
        self.assertEqual(origin["stamped_by"], "none")

    def test_an_unreadable_environment_costs_the_address_not_the_job(self):
        class Hostile:
            def get(self, *_a, **_kw):
                raise RuntimeError("no env for you")
        self.assertEqual(jobs.build_origin(self.state, env=Hostile()), {})

    def test_the_registry_enriches_but_never_identifies(self):
        """`source`/`cwd`/`branch` come from the registry when it has an entry for THIS id — but the
        id itself never does, because every hook-stamped entry carries `pid: 0` and `cwd`+`branch`
        is ambiguous across worktrees."""
        import sentinel
        sentinel.write_session_heartbeat(self.state, "build", pid=0, session_id="sess-abc",
                                         cwd=r"C:\repos\seneschal-dev", branch="feat/thing")
        origin = jobs.build_origin(self.state, env={jobs.ORIGIN_ENV_VAR: "sess-abc"})
        self.assertEqual(origin["source"], "build")
        self.assertEqual(origin["branch"], "feat/thing")
        # …and an id the registry has never heard of still produces a usable address.
        lonely = jobs.build_origin(self.state, env={jobs.ORIGIN_ENV_VAR: "sess-nobody-knows"})
        self.assertEqual(lonely["session_id"], "sess-nobody-knows")
        self.assertNotIn("source", lonely)

    def test_the_goal_is_capped_to_one_line_at_write_time(self):
        """A caller who pastes four paragraphs of hypothesis into --goal gets ONE LINE stored. The
        payload contract is structural, enforced once where it is written."""
        paragraphs = "why it broke\n\nI think the loop exits early\n" + ("x" * 500)
        rec = self._start(origin={"session_id": "s", "goal": paragraphs, "stamped_by": "flag"})
        goal = rec["origin"]["goal"]
        self.assertNotIn("\n", goal)
        self.assertLessEqual(len(goal), jobs.ORIGIN_GOAL_MAX)
        self.assertTrue(goal.startswith("why it broke"))

    def test_unknown_origin_keys_are_dropped_and_junk_is_empty(self):
        rec = self._start(origin={"session_id": "s", "cmd": "rm -rf /", "transcript": "…",
                                  "stamped_by": "flag"})
        self.assertEqual(set(rec["origin"]), {"session_id", "stamped_by"})
        self.assertEqual(jobs.normalize_origin("not-a-dict"), {})
        self.assertEqual(jobs.normalize_origin(None), {})

    def test_a_legacy_record_with_an_empty_origin_still_loads_pushes_and_prunes(self):
        """Every pre-feature record looks like this. `origin: {}` and a missing `origin` must stay
        indistinguishable to every reader."""
        for origin in ({}, None):
            with self.subTest(origin=origin):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                rec = jobs.start_job(tmp.name, "legacy", ["x"], runner=_runner(), now=NOW)
                rec.update({"status": jobs.DONE, "exit_code": 0, "ended_at": jobs._stamp(NOW)})
                if origin is None:
                    rec.pop("origin", None)
                else:
                    rec["origin"] = origin
                jobs.save_job(tmp.name, rec)
                self.assertIsNotNone(jobs.load_job(tmp.name, rec["id"]))
                self.assertEqual(jobs.origin_goal(rec), "")
                notify = _Recorder()
                self.assertEqual(len(jobs.reconcile(tmp.name, notify=notify, now=NOW)), 1)
                self.assertEqual(
                    jobs.prune(tmp.name, days=1, now=NOW + timedelta(days=30)), 1)

    def test_start_cli_stamps_from_the_environment_without_being_asked(self):
        """End to end through `main`, because the whole argument for auto-stamping is that nobody
        has to type anything — a unit test of `build_origin` alone would not prove the CLI calls it."""
        real_start = jobs.start_job

        def stubbed(*a, **kw):  # inject the fake spawner; everything else is the real path
            kw["runner"] = _runner()
            return real_start(*a, **kw)

        with mock.patch.dict(os.environ, {jobs.ORIGIN_ENV_VAR: "cli-session"}), \
             mock.patch.object(jobs, "start_job", stubbed), \
             mock.patch("sys.stdout"):
            self.assertEqual(jobs.main(["--state-dir", self.state, "start", "--title", "t",
                                        "--goal", "prove the stamp fires", "--", "python", "-c",
                                        "pass"]), 0)
        rec = jobs.list_jobs(self.state)[0]
        self.assertEqual(rec["origin"]["session_id"], "cli-session")
        self.assertEqual(rec["origin"]["goal"], "prove the stamp fires")
        self.assertEqual(rec["origin"]["stamped_by"], "env")


class LoopFlagTests(unittest.TestCase):
    """`--loop <id>` — one explicit signal, never
    inferred: starting a job that is doing register work IS the decision `loops.py start` records."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _add_loop_item(self, **over):
        kwargs = dict(text="do the thing", audience="owner",
                     terminal_state="it ships or the owner says drop it", whose_move="assistant",
                     next_action="do it", kind="enhancement", priority="normal")
        kwargs.update(over)
        return loops.add(self.state, **kwargs)

    def _cli_start(self, *extra):
        real_start = jobs.start_job

        def stubbed(*a, **kw):
            kw["runner"] = _runner()
            return real_start(*a, **kw)

        with mock.patch.object(jobs, "start_job", stubbed), mock.patch("sys.stdout"), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            code = jobs.main(["--state-dir", self.state, "start", "--title", "t",
                              *extra, "--", "python", "-c", "pass"])
        return code, err.getvalue()

    def test_loop_flag_starts_the_named_register_item(self):
        item = self._add_loop_item()
        code, _ = self._cli_start("--loop", item["id"])
        self.assertEqual(code, 0)
        got = loops.get(loops.load(self.state), item["id"])
        self.assertEqual(got["status"], "in_progress")

    def test_no_loop_flag_touches_no_register_state(self):
        code, _ = self._cli_start()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(loops.store_path(self.state)))

    def test_an_unknown_loop_id_is_reported_but_does_not_fail_the_job(self):
        code, err = self._cli_start("--loop", "loop-does-not-exist")
        self.assertEqual(code, 0)
        self.assertIn("loop-does-not-exist", err)

    def test_a_terminal_loop_item_is_reported_but_does_not_fail_the_job(self):
        item = self._add_loop_item()
        loops.resolve(self.state, item_id=item["id"], because="already shipped")
        code, err = self._cli_start("--loop", item["id"])
        self.assertEqual(code, 0)
        self.assertIn("did not start", err)
        self.assertEqual(loops.get(loops.load(self.state), item["id"])["status"], "done")


class WakeDefaultTests(unittest.TestCase):
    """The wake defaults ON for a job the owner asked for, so the completion report is a mechanism
    instead of a promise: a job that ends badly with `--wake` forgotten would otherwise go unexplained
    until the owner asks. `--wake` still works exactly as before; `--no-wake` is the explicit opt-out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _start_cli(self, extra_args, *, env=None):
        real_start = jobs.start_job

        def stubbed(*a, **kw):
            kw["runner"] = _runner()
            return real_start(*a, **kw)

        # Blank out the real session id this test itself is running under — otherwise a bare
        # `start_cli([])` would auto-stamp a live origin from THIS session's own environment,
        # rather than exercising the "nobody said anything" case it means to test.
        full_env = {jobs.ORIGIN_ENV_VAR: ""}
        full_env.update(env or {})
        with mock.patch.dict(os.environ, full_env, clear=False), \
             mock.patch.object(jobs, "start_job", stubbed), \
             mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = jobs.main(["--state-dir", self.state, "start", "--title", "t",
                              *extra_args, "--", "python", "-c", "pass"])
        return code, (jobs.list_jobs(self.state)[-1] if jobs.list_jobs(self.state) else None)

    # ---------------------------------------------------------- the pure predicate

    def test_default_wake_is_true_only_for_a_owner_request(self):
        self.assertTrue(jobs.default_wake({"request": {"by": "owner"}}))
        self.assertFalse(jobs.default_wake({"request": {"by": "assistant"}}))
        self.assertFalse(jobs.default_wake({"request": {"by": "unresolved"}}))
        self.assertFalse(jobs.default_wake({}))
        self.assertFalse(jobs.default_wake(None))
        self.assertFalse(jobs.default_wake("not-a-dict"))
        self.assertFalse(jobs.default_wake({"request": "not-a-dict-either"}))

    # ---------------------------------------------------------- end to end via the CLI

    def test_a_owner_origin_job_defaults_to_wake(self):
        code, rec = self._start_cli(["--goal", "prove the default", "--requested-by", "owner"])
        self.assertEqual(code, 0)
        self.assertEqual(rec["origin"]["request"]["by"], "owner")
        self.assertTrue(rec["wake"])

    def test_no_wake_overrides_the_owner_default(self):
        code, rec = self._start_cli(["--goal", "prove the override", "--requested-by", "owner",
                                     "--no-wake"])
        self.assertEqual(code, 0)
        self.assertEqual(rec["origin"]["request"]["by"], "owner")
        self.assertFalse(rec["wake"])

    def test_explicit_wake_still_works_on_a_assistant_job(self):
        """`--wake` is honoured as-is — nothing existing changes meaning."""
        code, rec = self._start_cli(["--goal", "prove the flag still works", "--requested-by",
                                     "assistant", "--reason-class", "merge-repair", "--wake"])
        self.assertEqual(code, 0)
        self.assertEqual(rec["origin"]["request"]["by"], "assistant")
        self.assertTrue(rec["wake"])

    def test_a_assistant_origin_job_does_not_default_to_wake(self):
        code, rec = self._start_cli(["--goal", "daemon-decided work", "--requested-by", "assistant",
                                     "--reason-class", "merge-repair"])
        self.assertEqual(code, 0)
        self.assertEqual(rec["origin"]["request"]["by"], "assistant")
        self.assertFalse(rec["wake"])

    def test_an_unresolved_origin_job_does_not_default_to_wake(self):
        """No goal, no session id, no --requested-by — `origin` is `{}` exactly like today, and a job
        with no address behind it is precisely the daemon-internal / Dream-step / scheduled-sweep
        case that must NOT start waking the warm session on its own cadence."""
        code, rec = self._start_cli([])
        self.assertEqual(code, 0)
        self.assertEqual(rec["origin"], {})
        self.assertFalse(rec["wake"])

    def test_wake_and_no_wake_together_are_refused(self):
        code, rec = self._start_cli(["--wake", "--no-wake"])
        self.assertEqual(code, 2)
        self.assertIsNone(rec)


class AnalysisRequestTests(unittest.TestCase):
    """Phase 2 — asking for a fresh pair of eyes. Every refusal here is a real answer, so each one is
    pinned; and the recursion guard is the structural half of "the analyst answers once"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _failed(self, goal="why the refresh writes zero docs", **kw):
        rec = jobs.start_job(self.state, "the work", ["x"], runner=_runner(), now=NOW,
                             origin=jobs.build_origin(self.state, session_id="s-1", goal=goal),
                             **kw)
        rec.update({"status": jobs.FAILED, "exit_code": 3, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, rec)
        return rec

    def test_an_analysis_job_carries_no_origin_and_one_is_refused_in_code(self):
        """§4.3's recursion guard — the `session_stamp.RECURSION_ENV` shape (never dream the
        dreamer). An analysis with no address cannot be routed, so the loop cannot close."""
        with self.assertRaises(ValueError):
            jobs.start_job(self.state, "analysis", ["x"], runner=_runner(), now=NOW,
                           analysis_for="some-job", origin={"session_id": "s-1"})
        sub = jobs.start_job(self.state, "analysis", ["x"], runner=_runner(), now=NOW,
                             analysis_for="some-job")
        self.assertEqual(sub["origin"], {})
        self.assertEqual(sub["analysis_for"], "some-job")

    def test_an_analysis_of_an_analysis_is_refused(self):
        sub = jobs.start_job(self.state, "analysis", ["x"], runner=_runner(), now=NOW,
                             analysis_for="some-job")
        sub.update({"status": jobs.FAILED, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, sub)
        with self.assertRaises(ValueError) as ctx:
            jobs.request_analysis(self.state, sub["id"], goal="g", runner=_runner())
        self.assertIn("IS an analysis", str(ctx.exception))

    def test_analysis_without_a_goal_is_refused(self):
        """An analysis with no goal line is an analyst guessing what "working"
        would have looked like — and the goal is half the input contract."""
        rec = self._failed(goal=None)
        with self.assertRaises(ValueError) as ctx:
            jobs.request_analysis(self.state, rec["id"], runner=_runner())
        self.assertIn("no goal line", str(ctx.exception))
        # …and the goal the JOB was started with satisfies it, without repeating yourself.
        started_with = self._failed()
        self.assertTrue(jobs.request_analysis(self.state, started_with["id"], runner=_runner()))

    def test_only_a_badly_ended_job_is_analysable(self):
        running = jobs.start_job(self.state, "still going", ["x"], runner=_runner(), now=NOW,
                                 origin=jobs.build_origin(self.state, session_id="s", goal="g"))
        with self.assertRaises(ValueError):
            jobs.request_analysis(self.state, running["id"], runner=_runner())
        running.update({"status": jobs.DONE, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, running)
        with self.assertRaises(ValueError):
            jobs.request_analysis(self.state, running["id"], runner=_runner())
        with self.assertRaises(ValueError):
            jobs.request_analysis(self.state, "no-such-job", goal="g", runner=_runner())

    def test_a_second_request_is_refused_rather_than_spending_again(self):
        rec = self._failed()
        first = jobs.request_analysis(self.state, rec["id"], runner=_runner())
        self.assertEqual(jobs.load_job(self.state, rec["id"])["analysis_job"], first["id"])
        with self.assertRaises(ValueError):
            jobs.request_analysis(self.state, rec["id"], runner=_runner())

    def test_the_analysis_job_runs_job_analysis_against_the_target(self):
        rec = self._failed()
        calls: list = []
        sub = jobs.request_analysis(self.state, rec["id"], runner=_runner(calls=calls))
        self.assertIn("job_analysis.py", " ".join(sub["argv"]))
        self.assertIn(rec["id"], sub["argv"])
        self.assertIn("why the refresh writes zero docs", sub["argv"])
        self.assertIn("--analyst", sub["argv"])
        # …and it is itself a normal job: detached, key-scrubbed, and it will push its own outcome.
        self.assertNotIn("ANTHROPIC_API_KEY", sorted(calls[0][1]["env"].keys()))

    def test_analyze_is_opt_in_and_fires_only_after_the_push_landed(self):
        opted_in = self._failed(analyze=True)
        plain = self._failed()
        notify = _Recorder()
        jobs.reconcile(self.state, notify=notify, now=NOW, runner=_runner())
        self.assertIsNotNone(jobs.load_job(self.state, opted_in["id"])["analysis_job"])
        self.assertIsNone(jobs.load_job(self.state, plain["id"]).get("analysis_job"))
        # Exactly one analysis, however many ticks run.
        jobs.reconcile(self.state, notify=notify, now=NOW, runner=_runner())
        self.assertEqual(sum(1 for r in jobs.list_jobs(self.state) if r.get("analysis_for")), 1)

    def test_a_successful_opted_in_job_is_never_analysed(self):
        rec = self._failed(analyze=True)
        rec.update({"status": jobs.DONE, "exit_code": 0})
        jobs.save_job(self.state, rec)
        jobs.reconcile(self.state, notify=_Recorder(), now=NOW, runner=_runner())
        self.assertIsNone(jobs.load_job(self.state, rec["id"]).get("analysis_job"))

    def test_a_refused_analysis_never_costs_the_completion_push(self):
        """The analysis is an ADDITION. A job that asked for one but can't have one (no goal) must
        still be reported exactly once — the parent spec's guarantee is not negotiable."""
        rec = self._failed(goal=None, analyze=True)
        notify = _Recorder()
        notified = jobs.reconcile(self.state, notify=notify, now=NOW, runner=_runner())
        self.assertEqual(len(notified), 1)
        self.assertEqual(len(notify.sent), 1)
        self.assertIsNone(jobs.load_job(self.state, rec["id"]).get("analysis_job"))

    def test_a_spawn_failure_in_the_analysis_never_costs_the_completion_push(self):
        def boom(argv, **kwargs):
            raise OSError("no python here")
        rec = self._failed(analyze=True)
        notify = _Recorder()
        notified = jobs.reconcile(self.state, notify=notify, now=NOW, runner=boom)
        self.assertEqual([r["id"] for r in notified], [rec["id"]])
        self.assertEqual(len(notify.sent), 1)

    def test_start_refuses_analyze_without_a_goal(self):
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(jobs.main(["--state-dir", self.state, "start", "--title", "t",
                                        "--analyze", "--", "python", "-c", "pass"]), 2)
        self.assertEqual(jobs.list_jobs(self.state), [])

    def test_an_unknown_analyst_is_refused_before_anything_is_spawned(self):
        """An unknown `--analyst` used to spawn a job that could only exit 2 on its own argparse,
        and then push that failure at the owner. A refusal must cost nothing."""
        rec = self._failed()
        calls: list = []
        with self.assertRaises(ValueError) as ctx:
            jobs.request_analysis(self.state, rec["id"], analyst="no-such-analyst",
                                  runner=_runner(calls=calls))
        self.assertIn("no-such-analyst", str(ctx.exception))
        self.assertEqual(calls, [])
        self.assertIsNone(jobs.load_job(self.state, rec["id"]).get("analysis_job"))

    def test_prune_takes_the_analysis_artifact_with_the_job(self):
        rec = self._failed()
        art = jobs.analysis_path(self.state, rec["id"])
        with open(art, "w", encoding="utf-8") as fh:
            fh.write("{}")
        rec["notified_at"] = jobs._stamp(NOW)
        jobs.save_job(self.state, rec)
        jobs.prune(self.state, days=1, now=NOW + timedelta(days=30))
        self.assertFalse(os.path.exists(art))


class AnalysisCwdTests(unittest.TestCase):
    """Where the ANALYSIS job runs — `jobs.analysis_cwd`, and the bug it exists for.

    A job started `--analyze --worktree` and cancelled had its analysis recorded
    `"spawn failed: [WinError 267] The directory name is invalid"`, with a `cwd` pointing at the
    private worktree. The analysis inherited the analysed job's `cwd`, and
    `release_worktree` tears that directory down at the terminal transition — BEFORE the push, which
    is before `request_analysis`. Teardown runs on every terminal state, so this was not a
    cancellation quirk: **`--analyze` combined with `--worktree` failed to spawn essentially
    always**, and the one case that worked was a worktree whose removal had been REFUSED. The safety
    net silently did not exist for exactly the jobs most likely to need it, and nothing looked broken
    because the completion push had already landed.

    So the first test below is the regression, and it needs `_cwd_checking_runner`: with the ordinary
    fake runner the whole failure is invisible, because nothing else in this file cares about `cwd`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.roots = tempfile.TemporaryDirectory()
        self.addCleanup(self.roots.cleanup)
        self.root = os.path.join(self.roots.name, "seneschal-worktrees")

    def _failed(self, *, worktree=False, analyze=False, goal="why the build died"):
        kw: dict = {}
        if worktree:
            kw = {"worktree": True, "worktree_root": self.root, "wt_runner": _FakeGit()}
        rec = jobs.start_job(self.state, "delegated build", ["claude", "-p", "brief"],
                             runner=_runner(), now=NOW, analyze=analyze,
                             origin=jobs.build_origin(self.state, session_id="s-1", goal=goal), **kw)
        rec.update({"status": jobs.FAILED, "exit_code": 1, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, rec)
        return rec

    # ---------------------------------------------------------------- the regression

    def test_an_analysis_spawns_after_its_targets_worktree_has_been_torn_down(self):
        """The whole bug, end to end through one reconcile pass: the worktree exists while the job
        does, is gone by the time the analyst is spawned, and the analyst spawns anyway."""
        rec = self._failed(worktree=True, analyze=True)
        self.assertTrue(os.path.isdir(rec["cwd"]))
        calls: list = []
        jobs.reconcile(self.state, notify=_Recorder(), now=NOW,
                       runner=_cwd_checking_runner(calls=calls), wt_runner=_FakeGit())
        self.assertFalse(os.path.exists(rec["cwd"]))     # teardown ran before the analysis did

        sub_id = jobs.load_job(self.state, rec["id"])["analysis_job"]
        self.assertIsNotNone(sub_id)
        sub = jobs.load_job(self.state, sub_id)
        # Pre-fix this is FAILED, carrying "spawn failed: [WinError 267] The directory name is invalid".
        self.assertEqual(sub["status"], jobs.RUNNING)
        self.assertIsNone(sub.get("error"))
        self.assertEqual(sub["cwd"], os.path.abspath(jobs.REPO_ROOT))
        self.assertNotEqual(sub["cwd"], rec["cwd"])
        self.assertEqual(calls[-1][1]["cwd"], sub["cwd"])   # the spawn's cwd, not just the record's

    def test_the_analysis_of_a_worktree_job_still_carries_no_origin(self):
        """§4.3's recursion guard is untouched by choosing a different directory to run in."""
        rec = self._failed(worktree=True)
        sub = jobs.request_analysis(self.state, rec["id"], runner=_cwd_checking_runner())
        self.assertEqual(sub["origin"], {})
        self.assertTrue(rec["origin"].get("session_id"))    # the TARGET had an address; the analysis doesn't
        self.assertEqual(sub["analysis_for"], rec["id"])

    def test_the_completion_push_is_still_exactly_once_whatever_the_analysis_does(self):
        """The parent spec's non-negotiable, re-asserted on the worktree path: the analysis is an
        ADDITION, so spawning, refusing and failing must all leave the push at exactly one."""
        def _boom(argv, **kwargs):
            raise OSError("no python here")

        for label, goal, runner in (("spawns", "why the build died", _cwd_checking_runner()),
                                    ("refuses", None, _cwd_checking_runner()),
                                    ("fails", "why the build died", _boom)):
            with self.subTest(label):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                self.state = tmp.name
                self.root = os.path.join(tmp.name, "seneschal-worktrees")
                rec = self._failed(worktree=True, analyze=True, goal=goal)
                notify = _Recorder()
                first = jobs.reconcile(self.state, notify=notify, now=NOW, runner=runner,
                                       wt_runner=_FakeGit())
                after = jobs.load_job(self.state, rec["id"])
                # Read the analysis's outcome off THIS pass: a later tick reaps its fake pid and
                # calls it `ended-unknown`, which says nothing about whether the spawn worked.
                if label == "refuses":
                    self.assertIsNone(after.get("analysis_job"))    # no goal line, so no analyst
                else:
                    sub = jobs.load_job(self.state, after["analysis_job"])
                    self.assertEqual(sub["status"],
                                     jobs.RUNNING if label == "spawns" else jobs.FAILED)

                second = jobs.reconcile(self.state, notify=notify, now=NOW, runner=runner,
                                        wt_runner=_FakeGit())
                self.assertEqual([r["id"] for r in first], [rec["id"]])
                self.assertNotIn(rec["id"], [r["id"] for r in second])
                self.assertTrue(after["notified_at"])

    def test_a_non_worktree_jobs_analysis_is_unaffected(self):
        """A plain job's `cwd` already IS the host repo, so choosing explicitly changes nothing for it."""
        rec = self._failed()
        self.assertEqual(rec["cwd"], jobs.REPO_ROOT)
        sub = jobs.request_analysis(self.state, rec["id"], runner=_cwd_checking_runner())
        self.assertEqual(sub["status"], jobs.RUNNING)
        self.assertEqual(sub["cwd"], os.path.abspath(rec["cwd"]))
        self.assertNotIn("worktree", sub)

    # ---------------------------------------------------------------- the ladder itself

    def test_the_host_repo_on_the_worktree_record_wins(self):
        """`worktree.host` is the checkout that owns the tree, and teardown leaves it alone."""
        self.assertEqual(jobs.analysis_cwd({"cwd": os.path.join(self.root, "gone"),
                                            "worktree": {"path": os.path.join(self.root, "gone"),
                                                         "host": self.roots.name}}),
                         os.path.abspath(self.roots.name))

    def test_a_host_that_no_longer_exists_falls_back_rather_than_failing(self):
        """A missing directory must never be the thing that costs the analysis."""
        missing = os.path.join(self.roots.name, "not-here")
        out = jobs.analysis_cwd({"worktree": {"path": missing, "host": missing}})
        self.assertEqual(out, os.path.abspath(jobs.REPO_ROOT))
        self.assertTrue(os.path.isdir(out))

    def test_the_floor_is_a_directory_that_is_guaranteed_to_exist(self):
        missing = os.path.join(self.roots.name, "not-here")
        with mock.patch.object(jobs, "REPO_ROOT", missing):
            out = jobs.analysis_cwd({"worktree": {"path": missing, "host": missing}})
        self.assertEqual(out, os.path.abspath(tempfile.gettempdir()))

    def test_it_never_returns_the_disposable_worktree_whatever_the_record_looks_like(self):
        """Every malformed shape lands on a real directory, and none of them lands on the tree that
        is scheduled for deletion — including the `{"requested": True, "path": None}` block a job
        whose worktree could not be created carries."""
        doomed = os.path.join(self.root, "20260813-140307-6530")
        os.makedirs(doomed, exist_ok=True)
        for rec in ({}, {"cwd": doomed}, {"cwd": doomed, "worktree": None},
                    {"cwd": doomed, "worktree": "a string"},
                    {"cwd": doomed, "worktree": {}},
                    {"cwd": doomed, "worktree": {"path": doomed}},
                    {"cwd": doomed, "worktree": {"path": doomed, "host": None}},
                    {"cwd": doomed, "worktree": {"path": doomed, "host": "   "}},
                    {"cwd": doomed, "worktree": {"path": doomed, "host": 17}},
                    {"worktree": {"requested": True, "path": None, "error": "fetch failed"}}):
            with self.subTest(repr(rec)):
                out = jobs.analysis_cwd(rec)
                self.assertTrue(os.path.isdir(out))
                self.assertNotEqual(out, doomed)


class AnalysisRoutingTests(unittest.TestCase):
    """Phase 3 — the §3.3 delivery ladder.

    **The first test in this class is the one that matters.** Routing may only ever ADD a delivery.
    A routing feature that can swallow a completion push would undo the whole parent spec, so the
    "exactly once on every terminal state" invariant is re-asserted here under routing-on,
    routing-off, and a route that fails — not merely assumed to still hold."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _analysis(self, *, origin=None, wake=False, status=jobs.DONE, staged=True,
                  title="the work"):
        """A finished analysis job plus the target it is about — the shape reconcile sees."""
        target = jobs.start_job(self.state, title, ["x"], runner=_runner(), now=NOW,
                                wake=wake, origin=origin)
        target.update({"status": jobs.FAILED, "exit_code": 3, "ended_at": jobs._stamp(NOW),
                       "notified_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, target)
        sub = jobs.start_job(self.state, f"analysis: {title}", ["y"], runner=_runner(), now=NOW,
                             analysis_for=target["id"])
        sub.update({"status": status, "exit_code": 0 if status == jobs.DONE else 4,
                    "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, sub)
        if staged:
            with open(jobs.analysis_path(self.state, target["id"]), "w", encoding="utf-8") as fh:
                fh.write('{"schema": "seneschal.job-leads/1", "leads": []}')
        return target, sub

    # ------------------------------------------------------------------ THE regression

    def test_every_terminal_path_still_notifies_exactly_once_with_routing_on_off_and_broken(self):
        """The load-bearing one. Same five terminal states as
        `test_every_terminal_path_notifies_exactly_once`, run three ways: with an analysis job whose
        route succeeds, with an ordinary job that routes nothing, and with a route that raises."""
        modes = {
            "routing-off": (None, None),
            "routing-on": ({"session_id": "s-1", "stamped_by": "flag"}, None),
            "route-explodes": ({"session_id": "s-1", "stamped_by": "flag"},
                               RuntimeError("mailbox on fire")),
        }
        for mode, (origin, boom) in modes.items():
            for status in jobs.TERMINAL:
                with self.subTest(mode=mode, status=status):
                    tmp = tempfile.TemporaryDirectory()
                    self.addCleanup(tmp.cleanup)
                    if origin:
                        # Written directly: a `start_job` here would leave a THIRD record that
                        # reconciles and notifies on its own, which would mask the thing under test.
                        jobs.save_job(tmp.name, {
                            "schema": jobs.SCHEMA, "id": "some-target", "title": "the work",
                            "argv": ["t"], "status": jobs.FAILED, "ended_at": jobs._stamp(NOW),
                            "notified_at": jobs._stamp(NOW), "origin": origin, "attempts": [],
                            "log_path": jobs.log_path(tmp.name, "some-target")})
                    rec = jobs.start_job(tmp.name, f"job {status}", ["x"], runner=_runner(),
                                         now=NOW,
                                         analysis_for="some-target" if origin else None)
                    rec.update({"status": status, "ended_at": jobs._stamp(NOW),
                                "exit_code": 0 if status == jobs.DONE else 3})
                    jobs.save_job(tmp.name, rec)
                    notify = _Recorder()
                    ctx = (mock.patch.object(jobs, "route_analysis", side_effect=boom)
                           if boom else mock.patch.object(jobs, "_utc_now", return_value=NOW))
                    with ctx:
                        first = jobs.reconcile(tmp.name, notify=notify, now=NOW, runner=_runner())
                        second = jobs.reconcile(tmp.name, notify=notify, now=NOW, runner=_runner())
                    self.assertEqual(len(first), 1, f"{status}/{mode} did not notify")
                    self.assertEqual(second, [], f"{status}/{mode} notified twice")
                    self.assertEqual(len(notify.sent), 1)

    def test_a_failing_route_never_unsets_notified_at(self):
        """Belt and braces on the same invariant from the other side: the push is stamped BEFORE the
        route runs, so an exploding route leaves a job that is done being reported, not one that
        will be reported again forever."""
        target, sub = self._analysis(origin={"session_id": "s-1"})
        with mock.patch.object(jobs, "route_analysis", side_effect=RuntimeError("nope")):
            jobs.reconcile(self.state, notify=_Recorder(), now=NOW, runner=_runner())
        self.assertIsNotNone(jobs.load_job(self.state, sub["id"])["notified_at"])

    # ------------------------------------------------------------------ the rungs

    def test_rung_2_wakes_the_warm_session_with_the_artifact_path(self):
        target, sub = self._analysis(origin={"session_id": "s-1", "source": "daemon"})
        woken: list = []
        jobs.reconcile(self.state, notify=_Recorder(), wake=woken.append, now=NOW,
                       runner=_runner())
        self.assertEqual(len(woken), 1)
        self.assertIn(jobs.analysis_path(self.state, target["id"]), woken[0])
        self.assertIn("not a diagnosis", woken[0])
        self.assertIn("not a message from the owner", woken[0])

    def test_a_wake_job_is_daemon_shaped_whatever_the_registry_called_it(self):
        """A job started from a warm turn is stamped `build` by the machine-wide hook — the warm
        session IS a Claude Code session and `session_stamp.py` does not exempt it. So `--wake`,
        which by definition means "read this back to me in voice", also earns rung 2."""
        target, sub = self._analysis(origin={"session_id": "s-1", "source": "build"}, wake=True)
        route = jobs.route_analysis(self.state, jobs.load_job(self.state, sub["id"]), now=NOW)
        self.assertEqual(route["rung"], 2)

    def test_rung_3_files_mail_for_a_session_that_cannot_be_pushed_at(self):
        target, sub = self._analysis(origin={"session_id": "sess-desk", "source": "desktop"})
        woken: list = []
        jobs.reconcile(self.state, notify=_Recorder(), wake=woken.append, now=NOW,
                       runner=_runner())
        self.assertEqual(woken, [])  # there is no channel to push at; a pull is the whole point
        mail = jobs.read_session_mail(self.state, "sess-desk")
        self.assertEqual(len(mail), 1)
        self.assertEqual(mail[0]["job_id"], target["id"])
        self.assertEqual(mail[0]["artifact"], jobs.analysis_path(self.state, target["id"]))
        self.assertIn("places to look", mail[0]["text"])

    def test_rung_4_is_silence_and_that_is_correct(self):
        """No address → nothing extra happens, because the Telegram push already fired. That floor
        needs no code and can only be lost by someone making delivery conditional."""
        target, sub = self._analysis(origin=None)
        notify = _Recorder()
        jobs.reconcile(self.state, notify=notify, now=NOW, runner=_runner())
        route = jobs.route_analysis(self.state, jobs.load_job(self.state, sub["id"]), now=NOW)
        self.assertEqual(route["rung"], 4)
        self.assertFalse(os.path.isdir(jobs.session_mail_dir(self.state)))
        self.assertEqual(len(notify.sent), 1)  # the analysis job's own push; the work's had landed

    def test_an_ordinary_job_routes_nothing(self):
        self.assertIsNone(jobs.route_analysis(self.state, {"id": "x"}, now=NOW))

    def test_a_failed_analysis_is_reported_honestly_rather_than_hidden(self):
        target, sub = self._analysis(origin={"session_id": "sess-desk", "source": "desktop"},
                                     status=jobs.FAILED, staged=False)
        jobs.reconcile(self.state, notify=_Recorder(), now=NOW, runner=_runner())
        mail = jobs.read_session_mail(self.state, "sess-desk")
        self.assertIsNone(mail[0]["artifact"])
        self.assertIn("did not produce a valid artifact", mail[0]["text"])

    def test_a_pruned_target_does_not_strand_the_route(self):
        target, sub = self._analysis(origin={"session_id": "s-1"})
        os.remove(jobs.job_path(self.state, target["id"]))
        route = jobs.route_analysis(self.state, jobs.load_job(self.state, sub["id"]), now=NOW)
        self.assertEqual(route["rung"], 4)

    # ------------------------------------------------------------------ the mailbox itself

    def test_mail_is_append_only_and_reads_oldest_first(self):
        for i in range(3):
            jobs.append_session_mail(self.state, "sess-x", {"at": jobs._stamp(NOW), "n": i})
        self.assertEqual([e["n"] for e in jobs.read_session_mail(self.state, "sess-x")], [0, 1, 2])
        self.assertEqual([e["n"] for e in jobs.read_session_mail(self.state, "sess-x", limit=2)],
                         [1, 2])

    def test_a_malformed_line_is_skipped_not_fatal(self):
        jobs.append_session_mail(self.state, "sess-x", {"at": jobs._stamp(NOW), "n": 1})
        with open(jobs.session_mail_path(self.state, "sess-x"), "a", encoding="utf-8") as fh:
            fh.write("{not json\n\n")
        self.assertEqual(len(jobs.read_session_mail(self.state, "sess-x")), 1)

    def test_an_unwritable_mailbox_costs_the_delivery_not_the_job(self):
        self.assertFalse(jobs.append_session_mail(self.state, "", {"at": "x"}))
        self.assertFalse(jobs.append_session_mail(self.state, "sess-x", "not a dict"))
        self.assertEqual(jobs.read_session_mail(self.state, "never-written"), [])

    def test_the_retention_is_one_constant_for_both_sweeps(self):
        """14 days, matching `prune --days`, changeable in one line."""
        self.assertEqual(jobs.RETENTION_DAYS, 14)
        self.assertEqual(inspect.signature(jobs.prune).parameters["days"].default,
                         jobs.RETENTION_DAYS)
        self.assertEqual(inspect.signature(jobs.prune_session_mail).parameters["days"].default,
                         jobs.RETENTION_DAYS)

    def test_the_mail_sweep_drops_old_entries_and_keeps_fresh_ones(self):
        old = jobs._stamp(NOW - timedelta(days=30))
        jobs.append_session_mail(self.state, "sess-x", {"at": old, "n": "stale"})
        jobs.append_session_mail(self.state, "sess-x", {"at": jobs._stamp(NOW), "n": "fresh"})
        # Age alone is no longer enough — the sweep drops READ entries, so read them first.
        jobs.session_mail_view(self.state, "sess-x", show_all=True, now=NOW)
        swept = jobs.prune_session_mail(self.state, now=NOW)
        self.assertEqual(swept["removed"], 1)
        self.assertEqual(swept["held_unread"], 0)
        remaining = jobs.read_session_mail(self.state, "sess-x")
        self.assertEqual([e["n"] for e in remaining], ["fresh"])

    def test_an_emptied_mailbox_is_removed_and_an_undatable_line_is_kept(self):
        old = jobs._stamp(NOW - timedelta(days=30))
        jobs.append_session_mail(self.state, "sess-gone", {"at": old})
        jobs.append_session_mail(self.state, "sess-keeps", {"at": old})
        jobs.append_session_mail(self.state, "sess-keeps", {"at": "who knows"})
        for session in ("sess-gone", "sess-keeps"):
            jobs.session_mail_view(self.state, session, show_all=True, now=NOW)
        jobs.prune_session_mail(self.state, now=NOW)
        self.assertFalse(os.path.exists(jobs.session_mail_path(self.state, "sess-gone")))
        # ...and the receipt goes with the mailbox it described.
        self.assertFalse(os.path.exists(jobs.session_mail_cursor_path(self.state, "sess-gone")))
        # Unread mail is the thing this is trying not to lose; a garbled stamp is cheap to keep.
        self.assertEqual(len(jobs.read_session_mail(self.state, "sess-keeps")), 1)

    def test_the_sweep_is_tolerant_of_a_missing_dir(self):
        self.assertEqual(jobs.prune_session_mail(os.path.join(self.state, "nope"))["removed"], 0)

    def test_mail_cli_reads_this_sessions_mailbox_from_the_environment(self):
        jobs.append_session_mail(self.state, "cli-session",
                                 {"at": jobs._stamp(NOW), "text": "leads for a thing"})
        with mock.patch.dict(os.environ, {jobs.ORIGIN_ENV_VAR: "cli-session"}), \
             mock.patch("sys.stdout"):
            self.assertEqual(jobs.main(["--state-dir", self.state, "mail"]), 0)
        with mock.patch.dict(os.environ, {jobs.ORIGIN_ENV_VAR: ""}, clear=False), \
             mock.patch("sys.stderr"):
            os.environ.pop(jobs.ORIGIN_ENV_VAR, None)
            self.assertEqual(jobs.main(["--state-dir", self.state, "mail"]), 2)


class MailReadReceiptTests(unittest.TestCase):
    """The read receipt. Before it, rung 3 delivered mail and could not answer *"did they read
    it?"* — it wrote nothing at all.

    **The invariant this class exists to pin: A READ NEVER FAILS BECAUSE A RECEIPT COULD NOT BE
    WRITTEN.** `read_session_mail`'s contract is that it must never be what breaks a session, and a
    confirmation is strictly less important than the mail it confirms. Every failure path here shows
    the mail and reports the failure; none withholds.

    **The second: `prune` may not eat unread mail.** It ran nightly, unattended, sweeping by age
    alone, in the one feature whose whole purpose is durable delivery.

    **The third: the receipt is a SEPARATE FILE.** Stamping `read_at` into the mailbox would mean a
    read-modify-write against a live appender, which can lose an entry — worse than the bug. Nothing
    here may grow a mailbox rewrite on the read path."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = tmp.name

    def _file(self, session, **fields):
        entry = {"at": jobs._stamp(NOW), **fields}
        self.assertTrue(jobs.append_session_mail(self.state, session, entry))
        return entry

    # ------------------------------------------------------------------ ids

    def test_append_stamps_a_stable_id_without_touching_the_callers_dict(self):
        entry = {"at": jobs._stamp(NOW), "text": "hello"}
        jobs.append_session_mail(self.state, "s", entry)
        self.assertNotIn("id", entry, "the caller's dict is copied, never mutated")
        [filed] = jobs.read_session_mail(self.state, "s")
        self.assertTrue(filed["id"].startswith("mail-"))
        self.assertEqual(jobs.mail_entry_id(filed), filed["id"])

    def test_an_id_the_caller_supplied_is_kept(self):
        jobs.append_session_mail(self.state, "s", {"at": jobs._stamp(NOW), "id": "mine"})
        self.assertEqual(jobs.read_session_mail(self.state, "s")[0]["id"], "mine")

    def test_a_pre_id_entry_gets_a_stable_synthesized_id(self):
        """Mail filed before ids existed is still REAL MAIL: it must be markable without rewriting
        the file it sits in."""
        legacy = {"at": "2026-08-20T16:16:58.652213+00:00", "kind": "handoff", "text": "HANDOFF..."}
        path = jobs.session_mail_path(self.state, "legacy")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(legacy) + "\n")
        [read_back] = jobs.read_session_mail(self.state, "legacy")
        self.assertNotIn("id", read_back)
        first = jobs.mail_entry_id(read_back)
        self.assertTrue(first.startswith("legacy-"))
        # Stable across reads — which is the ONLY property the cursor needs from it.
        self.assertEqual(first, jobs.mail_entry_id(jobs.read_session_mail(self.state, "legacy")[0]))
        view = jobs.session_mail_view(self.state, "legacy", now=NOW)
        self.assertEqual((view["unread"], view["shown"], view["advanced"]), (1, 1, True))
        # ...and it is not re-shown forever, nor deleted, nor rewritten.
        again = jobs.session_mail_view(self.state, "legacy", now=NOW)
        self.assertEqual((again["unread"], again["total"]), (0, 1))
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.read().strip()), legacy)

    # ------------------------------------------------------------------ the split

    def test_a_fresh_mailbox_is_entirely_unread(self):
        for i in range(3):
            self._file("s", n=i)
        view = jobs.session_mail_view(self.state, "s", peek=True)
        self.assertEqual((view["unread"], view["total"], view["shown"]), (3, 3, 3))
        self.assertEqual(jobs.read_mail_cursor(self.state, "s"), {})

    def test_an_absent_mailbox_reads_as_nothing_rather_than_an_error(self):
        view = jobs.session_mail_view(self.state, "never-written")
        self.assertEqual((view["total"], view["unread"], view["receipt"]), (0, 0, None))

    def test_reading_advances_the_cursor_and_the_next_read_shows_only_what_arrived_since(self):
        self._file("s", n=0)
        self._file("s", n=1)
        first = jobs.session_mail_view(self.state, "s", now=NOW)
        self.assertEqual(([e["n"] for e in first["entries"]], first["advanced"]), ([0, 1], True))
        second = jobs.session_mail_view(self.state, "s", now=NOW)
        self.assertEqual((second["entries"], second["unread"], second["total"]), ([], 0, 2))
        self._file("s", n=2)
        third = jobs.session_mail_view(self.state, "s", now=NOW)
        self.assertEqual(([e["n"] for e in third["entries"]], third["unread"], third["total"]),
                         ([2], 1, 3))

    def test_peek_shows_without_advancing(self):
        self._file("s", n=0)
        peeked = jobs.session_mail_view(self.state, "s", peek=True, now=NOW)
        self.assertEqual((peeked["shown"], peeked["advanced"], peeked["receipt"]), (1, False, None))
        self.assertEqual(jobs.read_mail_cursor(self.state, "s"), {})
        self.assertEqual(jobs.session_mail_view(self.state, "s", peek=True)["unread"], 1)

    def test_all_shows_read_mail_too(self):
        self._file("s", n=0)
        jobs.session_mail_view(self.state, "s", now=NOW)
        self.assertEqual(jobs.session_mail_view(self.state, "s", show_all=True, now=NOW)["shown"], 1)
        self.assertEqual(jobs.session_mail_view(self.state, "s", now=NOW)["shown"], 0)

    def test_the_unread_view_drains_oldest_first_so_a_limit_never_skips(self):
        for i in range(5):
            self._file("s", n=i)
        first = jobs.session_mail_view(self.state, "s", limit=2, now=NOW)
        self.assertEqual([e["n"] for e in first["entries"]], [0, 1])
        self.assertEqual((first["held_back"], first["advanced"]), (3, True))
        self.assertEqual([e["n"] for e in
                          jobs.session_mail_view(self.state, "s", limit=2, now=NOW)["entries"]],
                         [2, 3])

    def test_all_with_a_limit_that_would_hide_unread_mail_advances_nothing(self):
        """Marking mail read that the reader never saw is the same data loss with better manners."""
        for i in range(5):
            self._file("s", n=i)
        view = jobs.session_mail_view(self.state, "s", limit=2, show_all=True, now=NOW)
        self.assertEqual([e["n"] for e in view["entries"]], [3, 4])
        self.assertEqual((view["advanced"], view["receipt"], view["held_back"]), (False, None, 3))
        self.assertEqual(jobs.session_mail_view(self.state, "s", peek=True)["unread"], 5)

    def test_a_cursor_naming_an_entry_that_is_gone_falls_back_to_the_timestamp(self):
        old = {"at": jobs._stamp(NOW - timedelta(days=2)), "id": "a"}
        new = {"at": jobs._stamp(NOW), "id": "b"}
        cursor = {"read_through_id": "swept-away", "read_through_at": old["at"]}
        already, unread = jobs.split_session_mail([old, new], cursor)
        self.assertEqual(([e["id"] for e in already], [e["id"] for e in unread]), (["a"], ["b"]))

    def test_every_ambiguity_resolves_to_unread(self):
        entry = {"at": jobs._stamp(NOW), "id": "a"}
        for cursor in ({}, None, {"read_through_id": "nope"},
                       {"read_through_id": "nope", "read_through_at": "not a date"},
                       {"read_through_at": None}):
            with self.subTest(cursor=cursor):
                self.assertEqual(jobs.split_session_mail([entry], cursor), ([], [entry]))
        # An entry whose own stamp cannot be parsed is unread too, not silently swallowed.
        undatable = {"at": "who knows", "id": "z"}
        self.assertEqual(jobs.split_session_mail([undatable],
                                                 {"read_through_at": jobs._stamp(NOW)}),
                         ([], [undatable]))

    # ------------------------------------------------------------------ FAIL-OPEN

    def _block_the_cursor(self, session):
        """Make the cursor path unwritable in a way that works on Windows and POSIX alike: a
        NON-EMPTY DIRECTORY where the file belongs, so both the create and the `os.replace` fail."""
        blocked = jobs.session_mail_cursor_path(self.state, session)
        os.makedirs(blocked, exist_ok=True)
        with open(os.path.join(blocked, "occupied"), "w", encoding="utf-8") as fh:
            fh.write("x")
        return blocked

    def test_a_read_still_returns_the_mail_when_the_receipt_cannot_be_written(self):
        self._file("s", n=0, text="the thing that matters")
        self._block_the_cursor("s")
        view = jobs.session_mail_view(self.state, "s", now=NOW)
        self.assertEqual(view["shown"], 1, "the mail is returned exactly as it was before receipts")
        self.assertEqual(view["entries"][0]["text"], "the thing that matters")
        self.assertEqual((view["receipt"], view["advanced"]), (False, False))
        # And it stays unread rather than being quietly consumed by a write that never landed.
        self.assertEqual(jobs.session_mail_view(self.state, "s", peek=True)["unread"], 1)

    def test_the_receipt_writers_never_raise(self):
        self._file("s", n=0)
        self._block_the_cursor("s")
        self.assertFalse(jobs.write_mail_cursor(self.state, "s", {"read_through_id": "a"}))
        self.assertFalse(jobs.mark_session_mail_read(self.state, "s",
                                                     jobs.read_session_mail(self.state, "s")))
        self.assertFalse(jobs.mark_session_mail_read(self.state, "s", []))
        self.assertFalse(jobs.mark_session_mail_read(self.state, "", [{"at": "x"}]))
        self.assertFalse(jobs.write_mail_cursor(self.state, "s", "not a dict"))

    def test_a_failed_receipt_leaves_no_temp_file_behind(self):
        self._file("s", n=0)
        blocked = self._block_the_cursor("s")
        jobs.mark_session_mail_read(self.state, "s", jobs.read_session_mail(self.state, "s"))
        self.assertFalse(os.path.exists(blocked + ".tmp"))

    def test_a_corrupt_cursor_reads_as_nothing_read(self):
        self._file("s", n=0)
        with open(jobs.session_mail_cursor_path(self.state, "s"), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual(jobs.read_mail_cursor(self.state, "s"), {})
        self.assertEqual(jobs.session_mail_view(self.state, "s", peek=True)["unread"], 1)

    def test_the_cursor_is_a_separate_file_and_the_mailbox_is_never_rewritten_by_a_read(self):
        self._file("s", n=0)
        path = jobs.session_mail_path(self.state, "s")
        with open(path, "r", encoding="utf-8") as fh:
            before = fh.read()
        jobs.session_mail_view(self.state, "s", now=NOW)
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before, "a read must never rewrite a file an appender owns")
        cursor = jobs.read_mail_cursor(self.state, "s")
        self.assertEqual(cursor["schema"], jobs.MAIL_CURSOR_SCHEMA)
        self.assertEqual(cursor["read_at"], jobs._stamp(NOW))
        self.assertTrue(jobs.session_mail_cursor_path(self.state, "s").endswith(
            jobs.MAIL_CURSOR_SUFFIX))

    # ------------------------------------------------------------------ prune vs unread

    def test_the_sweep_refuses_to_eat_unread_mail_and_says_how_much_it_held(self):
        old = jobs._stamp(NOW - timedelta(days=30))
        jobs.append_session_mail(self.state, "s", {"at": old, "n": "read-and-old"})
        jobs.append_session_mail(self.state, "s", {"at": old, "n": "unread-and-old"})
        # Read only the first of the two.
        jobs.session_mail_view(self.state, "s", limit=1, now=NOW)
        swept = jobs.prune_session_mail(self.state, now=NOW)
        self.assertEqual((swept["removed"], swept["held_unread"]), (1, 1))
        self.assertEqual([e["n"] for e in jobs.read_session_mail(self.state, "s")],
                         ["unread-and-old"])
        # A second sweep changes nothing and keeps saying so — idempotent and restart-safe.
        self.assertEqual(jobs.prune_session_mail(self.state, now=NOW),
                         {"removed": 0, "held_unread": 1, "mailboxes": 1, "cursors_removed": 0})

    def test_the_sweep_reanchors_the_cursor_onto_what_survived(self):
        """The subtle one, and it is the original bug wearing the fix's clothes. The sweep can delete
        the very entry the cursor points at; a marker naming nothing falls back to comparing
        timestamps, and two entries filed in the same second are indistinguishable there — so a
        surviving UNREAD entry reads as read and the NEXT sweep eats it."""
        same_second = jobs._stamp(NOW - timedelta(days=30))
        jobs.append_session_mail(self.state, "s", {"at": same_second, "n": "read"})
        jobs.append_session_mail(self.state, "s", {"at": same_second, "n": "unread"})
        jobs.session_mail_view(self.state, "s", limit=1, now=NOW)
        jobs.prune_session_mail(self.state, now=NOW)
        cursor = jobs.read_mail_cursor(self.state, "s")
        self.assertIsNone(cursor["read_through_id"], "no read entry survived, so nothing is read")
        self.assertIsNone(cursor["read_through_at"])
        self.assertEqual(jobs.session_mail_view(self.state, "s", peek=True)["unread"], 1)
        for _ in range(3):
            jobs.prune_session_mail(self.state, now=NOW)
        self.assertEqual([e["n"] for e in jobs.read_session_mail(self.state, "s")], ["unread"])

    def test_a_surviving_read_entry_becomes_the_new_anchor(self):
        jobs.append_session_mail(self.state, "s",
                                 {"at": jobs._stamp(NOW - timedelta(days=30)), "n": "ancient"})
        jobs.append_session_mail(self.state, "s", {"at": jobs._stamp(NOW), "n": "recent"})
        jobs.session_mail_view(self.state, "s", show_all=True, now=NOW)
        jobs.prune_session_mail(self.state, now=NOW)
        [surviving] = jobs.read_session_mail(self.state, "s")
        self.assertEqual(jobs.read_mail_cursor(self.state, "s")["read_through_id"],
                         jobs.mail_entry_id(surviving))
        self.assertEqual(jobs.session_mail_view(self.state, "s", peek=True)["unread"], 0)

    def test_an_orphaned_cursor_is_swept(self):
        jobs.write_mail_cursor(self.state, "ghost", {"read_through_id": "x"})
        self.assertEqual(jobs.prune_session_mail(self.state, now=NOW)["cursors_removed"], 1)
        self.assertFalse(os.path.exists(jobs.session_mail_cursor_path(self.state, "ghost")))

    def test_a_line_that_is_not_even_json_is_never_treated_as_read(self):
        os.makedirs(jobs.session_mail_dir(self.state), exist_ok=True)
        with open(jobs.session_mail_path(self.state, "s"), "w", encoding="utf-8") as fh:
            fh.write("{not json\n")
        self.assertEqual(jobs.prune_session_mail(self.state, now=NOW)["removed"], 0)

    # ------------------------------------------------------------------ the renderer

    def test_an_entry_with_no_text_renders_as_more_than_one_bare_word(self):
        """A hand-filed entry with no `text` once printed as `handoff`, full stop, and had to be
        refiled by hand."""
        rendered = jobs.mail_summary({"at": "x", "kind": "handoff", "from": "assistant chat f3248325",
                                      "brief": r"C:\state\briefs\handoff-brief.md"})
        self.assertNotEqual(rendered, "handoff")
        self.assertIn("handoff", rendered)
        self.assertIn("assistant chat f3248325", rendered)
        self.assertIn("handoff-brief.md", rendered)

    def test_the_renderer_prefers_text_then_a_subject_line_then_the_fields(self):
        self.assertEqual(jobs.mail_summary({"kind": "handoff", "text": " hi "}), "hi")
        self.assertIn("ship it",
                      jobs.mail_summary({"kind": "handoff", "subject": "ship it", "job_id": "j1"}))
        self.assertIn("leads for a thing",
                      jobs.mail_summary({"kind": "job-analysis", "summary": "leads for a thing"}))

    def test_the_renderer_always_says_something_even_for_a_naked_entry(self):
        for entry in ({}, {"kind": "handoff"}, {"at": "x"}, {"kind": "x", "weird": {"a": 1}}):
            with self.subTest(entry=entry):
                rendered = jobs.mail_summary(entry)
                self.assertTrue(rendered.strip())
                self.assertGreater(len(rendered.split()), 1)
        self.assertIn("weird", jobs.mail_summary({"kind": "handoff", "weird": {"a": 1}}))

    def test_a_multi_line_summary_stays_on_one_line_when_it_is_built(self):
        rendered = jobs.mail_summary({"kind": "handoff", "subject": "one\ntwo\nthree"})
        self.assertNotIn("\n", rendered)

    # ------------------------------------------------------------------ the CLI

    def _run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = jobs.main(["--state-dir", self.state, "mail", "--session", "cli", *argv])
        return code, out.getvalue()

    def test_the_cli_distinguishes_no_mail_from_nothing_new(self):
        code, text = self._run()
        # Pinned by the watcher spec's §1.2 inventory: this exact string, exit 0.
        self.assertEqual((code, text.strip()), (0, "no mail"))
        self._file("cli", text="leads for a thing")
        code, text = self._run()
        self.assertIn("1 new, 1 total", text)
        self.assertIn("leads for a thing", text)
        code, text = self._run()
        self.assertIn("0 new, 1 total", text)
        self.assertIn("nothing new", text)
        self.assertNotIn("leads for a thing", text)
        self.assertIn("leads for a thing", self._run("--all")[1])

    def test_the_cli_peek_does_not_advance(self):
        self._file("cli", text="held")
        self.assertIn("1 new, 1 total", self._run("--peek")[1])
        self.assertIn("1 new, 1 total", self._run("--peek")[1])
        self.assertIn("1 new, 1 total", self._run()[1])
        self.assertIn("0 new, 1 total", self._run()[1])

    def test_the_cli_names_a_failed_receipt_instead_of_pretending(self):
        self._file("cli", text="still unread")
        self._block_the_cursor("cli")
        code, text = self._run()
        self.assertEqual(code, 0)
        self.assertIn("still unread", text)
        self.assertIn("receipt could not be written", text)

    def test_the_cli_says_when_a_limit_withheld_unread_mail(self):
        for i in range(3):
            self._file("cli", text=f"m{i}")
        self.assertIn("2 unread entr(ies) not shown", self._run("--limit", "1")[1])

    def test_the_cli_json_carries_the_counts_not_just_the_entries(self):
        self._file("cli", text="hello")
        payload = json.loads(self._run("--json", "--peek")[1])
        self.assertEqual((payload["unread"], payload["total"], payload["advanced"]),
                         (1, 1, False))
        self.assertEqual(payload["entries"][0]["text"], "hello")

    def test_prune_reports_the_hold_on_stdout(self):
        jobs.append_session_mail(self.state, "cli",
                                 {"at": jobs._stamp(NOW - timedelta(days=30)), "text": "old"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            jobs.main(["--state-dir", self.state, "prune"])
        self.assertIn("held 1 UNREAD past retention", out.getvalue())


class ClassifierTests(unittest.TestCase):
    """The crux. A too-eager classifier is how `--retry` does harm: a genuinely broken command would
    run N times and repeat its damage. So the default — **unrecognised output is terminal** — is
    asserted directly, and every pattern deliberately left OUT of `TRANSIENT_SIGNATURES` is pinned as
    terminal so a future "helpful" widening has to argue with a red test first."""

    def test_the_verbatim_api_500_line_is_transient(self):
        v = jobs.classify_failure(API_500, duration_sec=1.0, output_bytes=161)
        self.assertEqual(v["classification"], jobs.CLASS_TRANSIENT)
        self.assertEqual(v["signature"], "api-5xx")
        # 1 s / 161 bytes: the fast-fail corroborator lands, so this is the high-confidence shape.
        self.assertTrue(v["fast_fail"])
        self.assertEqual(v["confidence"], "high")

    def test_a_plain_test_failure_is_terminal_and_never_retried(self):
        for output in (
            "Ran 1489 tests in 41.2s\n\nFAILED (failures=1)",
            "FAILED test_foo.py::test_bar - AssertionError: 3 != 4",
            '  File "x.py", line 3\n    def f(:\n            ^\nSyntaxError: invalid syntax',
            "ModuleNotFoundError: No module named 'nope'",
            "error: pathspec 'develop' did not match any file(s) known to git",
            "",  # a non-zero exit that printed nothing at all
        ):
            with self.subTest(output=output[:40]):
                v = jobs.classify_failure(output, duration_sec=0.5, output_bytes=len(output))
                self.assertEqual(v["classification"], jobs.CLASS_TERMINAL)
                self.assertIsNone(v["signature"])
                self.assertEqual(v["confidence"], "none")

    def test_recognised_transient_signatures(self):
        cases = {
            'API Error: 529 {"type":"overloaded_error"}': "api-5xx",
            "API Error: 429 rate limit": "api-429",
            "Error: 503 Service Unavailable": "service-unavailable",
            "upstream connect error or disconnect/reset before headers": "upstream",
            "HTTP 502 from api.anthropic.com": "http-5xx",
            "<html><h1>502 Bad Gateway</h1>": "bad-gateway",
            "429 Too Many Requests": "too-many-requests",
            '{"type":"error","error":{"type":"rate_limit_error"}}': "rate-limit-error",
            "You have been rate limited; please slow down": "rate-limited",
            "ConnectionResetError: [WinError 10054] connection reset by peer": "conn-reset",
            "requests.exceptions.ConnectionError: ('Connection aborted.',)": "conn-aborted",
            "http.client.RemoteDisconnected: Remote end closed connection": "remote-disconnected",
            "TypeError: fetch failed": "fetch-failed",
            "urlopen error [Errno -3] Temporary failure in name resolution": "dns",
            "getaddrinfo ENOTFOUND api.anthropic.com": "dns",
            "net/http: TLS handshake timeout": "tls-handshake",
            "ssl.SSLError: [SSL: UNEXPECTED_EOF_WHILE_READING]": "tls-handshake",
            "Error: connect ETIMEDOUT 160.79.104.10:443": "transport-timeout",
            "socket timed out": "transport-timeout",
            "anthropic.APIConnectionError: Connection error.": "api-connection",
        }
        for output, signature in cases.items():
            with self.subTest(output=output[:44]):
                v = jobs.classify_failure(output)
                self.assertEqual(v["classification"], jobs.CLASS_TRANSIENT, output)
                self.assertEqual(v["signature"], signature)

    def test_the_deliberate_exclusions_stay_terminal(self):
        """Every one of these is either real output from this repo or the classic false positive. If a
        future pattern widening makes one of them retry, that widening is the bug."""
        for output in (
            "test timed out after 5s",                        # a slow test, not a transport timeout
            "TimeoutError: the fixture never became ready",
            "see seneschal/references/notion-rate-limits.md",     # our own filename, plural — must not hit
            "429 tests passed, 0 failed",                     # a bare number is not a status code
            "assert response.status == 500",                  # a test ASSERTING on a 5xx
            "ConnectionRefusedError: [Errno 111] Connection refused",  # a local service that is down
            "Internal server error rendered by the dev fixture",
            "500 Internal Server Error page snapshot updated",
        ):
            with self.subTest(output=output[:40]):
                self.assertIsNone(jobs.transient_signature(output), output)

    def test_speed_alone_never_earns_a_retry(self):
        """`fast_fail` is a corroborator, never a cause — a genuinely broken command also dies in a
        second (a syntax error is instant), so speed on its own must classify terminal."""
        v = jobs.classify_failure("SyntaxError: invalid syntax", duration_sec=0.2, output_bytes=27)
        self.assertTrue(v["fast_fail"])
        self.assertEqual(v["classification"], jobs.CLASS_TERMINAL)
        # And a signature WITHOUT the corroborator still retries, just at lower stated confidence.
        slow = jobs.classify_failure(API_500, duration_sec=900, output_bytes=200_000)
        self.assertEqual(slow["classification"], jobs.CLASS_TRANSIENT)
        self.assertFalse(slow["fast_fail"])
        self.assertEqual(slow["confidence"], "medium")

    def test_the_classifier_never_raises(self):
        for bad in (None, "", "\x00\xff binary-ish \udcff", "x" * 200_000):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertIn(jobs.classify_failure(bad)["classification"],
                              (jobs.CLASS_TRANSIENT, jobs.CLASS_TERMINAL))
        self.assertFalse(jobs.classify_failure(API_500, duration_sec="nope")["fast_fail"])


class RetryPolicyTests(unittest.TestCase):
    """`retry_config` / `parse_duration` / `backoff_delay` / `plan_retry` — all pure, so the decision
    the whole feature turns on is tested with no store, no clock and no process."""

    def test_retry_is_off_by_default_and_capped(self):
        self.assertIsNone(jobs.retry_config(0))
        self.assertIsNone(jobs.retry_config(None))
        self.assertIsNone(jobs.retry_config(-4))
        self.assertIsNone(jobs.retry_config("garbage"))
        self.assertEqual(jobs.retry_config(99)["max_retries"], jobs.RETRY_MAX_RETRIES_CAP)
        cfg = jobs.retry_config(3, backoff=True, window_sec=7200)
        self.assertEqual((cfg["max_retries"], cfg["backoff"], cfg["window_sec"]), (3, True, 7200))
        self.assertIsNone(jobs.retry_config(3, window_sec=0)["window_sec"])

    def test_parse_duration(self):
        for text, want in (("90", 90), ("90s", 90), ("45m", 2700), ("2h", 7200),
                           ("1d", 86400), ("1h30m", 5400), (" 2H ", 7200), (600, 600)):
            with self.subTest(text=text):
                self.assertEqual(jobs.parse_duration(text), want)
        for bad in ("", "soon", "2 weeks", "m", "1x", None, "1h30"):
            with self.subTest(bad=bad):
                self.assertRaises(ValueError, jobs.parse_duration, bad)

    def test_backoff_is_constant_without_the_flag_and_exponential_with_it(self):
        flat = jobs.retry_config(5)
        self.assertEqual([jobs.backoff_delay(flat, n, rand=_no_jitter) for n in (1, 2, 3)],
                         [30.0, 30.0, 30.0])
        exp = jobs.retry_config(5, backoff=True)
        self.assertEqual([jobs.backoff_delay(exp, n, rand=_no_jitter) for n in (1, 2, 3, 4)],
                         [30.0, 60.0, 120.0, 240.0])

    def test_backoff_is_capped_and_jittered_within_bounds(self):
        exp = jobs.retry_config(10, backoff=True)
        self.assertEqual(jobs.backoff_delay(exp, 10, rand=_no_jitter), float(jobs.RETRY_CAP_SEC))
        lo = jobs.backoff_delay(exp, 3, rand=lambda: 0.0)
        hi = jobs.backoff_delay(exp, 3, rand=lambda: 1.0)
        self.assertAlmostEqual(lo, 120.0 * (1 - jobs.RETRY_JITTER_FRAC))
        self.assertAlmostEqual(hi, 120.0 * (1 + jobs.RETRY_JITTER_FRAC))
        self.assertGreaterEqual(jobs.backoff_delay(exp, 1, rand=lambda: (_ for _ in ()).throw(
            RuntimeError("broken rand"))), 1.0)  # a broken seam must not strand the retry

    def _rec(self, **kw):
        rec = {"created_at": jobs._stamp(NOW), "attempt": 1,
               "retry": jobs.retry_config(3, backoff=True)}
        rec.update(kw)
        return rec

    def test_no_retry_when_it_was_never_asked_for(self):
        plan = jobs.plan_retry(self._rec(retry=None), status=jobs.FAILED, output=API_500, now=NOW)
        self.assertFalse(plan["retry"])
        self.assertEqual(plan["reason"], "retry-not-enabled")

    def test_the_verdict_is_reached_even_when_retry_is_off(self):
        """Classification happens BEFORE the retry-enabled check: the config decides what to do about
        a failure, never whether we bother to understand it. `retry` stays False either way."""
        transient = jobs.plan_retry(self._rec(retry=None), status=jobs.FAILED, output=API_500,
                                    now=NOW, duration_sec=1.0, output_bytes=161)
        self.assertEqual(transient["classification"], jobs.CLASS_TRANSIENT)
        self.assertEqual(transient["signature"], "api-5xx")
        self.assertEqual(transient["confidence"], "high")   # fast + tiny still corroborates
        self.assertTrue(transient["fast_fail"])
        self.assertFalse(transient["retry"])                # ...and still does not retry
        terminal = jobs.plan_retry(self._rec(retry=None), status=jobs.FAILED,
                                   output="FAILED (failures=1)", now=NOW)
        self.assertEqual(terminal["classification"], jobs.CLASS_TERMINAL)
        self.assertIsNone(terminal["signature"])
        # The non-failure outcomes keep the reason they always had, retry off or on.
        for status in (jobs.TIMED_OUT, jobs.ENDED_UNKNOWN, jobs.CANCELLED):
            with self.subTest(status=status):
                self.assertEqual(jobs.plan_retry(self._rec(retry=None), status=status,
                                                 output=API_500, now=NOW)["reason"],
                                 "retry-not-enabled")

    def test_success_is_not_a_retry(self):
        plan = jobs.plan_retry(self._rec(), status=jobs.DONE, output="all good", now=NOW)
        self.assertFalse(plan["retry"])
        self.assertEqual(plan["classification"], jobs.CLASS_SUCCESS)

    def test_timed_out_cancelled_and_unreadable_outcomes_are_never_retried(self):
        """Even with a transient signature sitting in the log. A timeout means the command was too
        slow — retrying makes that strictly worse — and the other two have no readable outcome to
        classify, so "transient" there would be a guess that re-runs invisible damage."""
        for status in (jobs.TIMED_OUT, jobs.ENDED_UNKNOWN, jobs.CANCELLED):
            with self.subTest(status=status):
                plan = jobs.plan_retry(self._rec(), status=status, output=API_500, now=NOW)
                self.assertFalse(plan["retry"])
                self.assertEqual(plan["reason"], f"non-retryable-outcome:{status}")

    def test_an_unrecognised_failure_is_terminal_first_time(self):
        plan = jobs.plan_retry(self._rec(), status=jobs.FAILED,
                               output="FAILED (failures=1)", now=NOW)
        self.assertFalse(plan["retry"])
        self.assertEqual(plan["reason"], "no-transient-signature")

    def test_a_transient_failure_schedules_the_next_attempt(self):
        plan = jobs.plan_retry(self._rec(), status=jobs.FAILED, output=API_500, now=NOW,
                               rand=_no_jitter)
        self.assertTrue(plan["retry"])
        self.assertEqual(plan["reason"], "transient-signature")
        self.assertEqual(plan["delay_sec"], 30.0)
        self.assertEqual(plan["next_attempt_at"], jobs._stamp(NOW + timedelta(seconds=30)))

    def test_the_retry_budget_runs_out(self):
        cfg = jobs.retry_config(2, backoff=True)
        # attempts 1 and 2 may still retry (2 extra attempts); attempt 3 is the last one there is.
        for attempt, expect in ((1, True), (2, True), (3, False)):
            with self.subTest(attempt=attempt):
                plan = jobs.plan_retry(self._rec(retry=cfg, attempt=attempt), status=jobs.FAILED,
                                       output=API_500, now=NOW, rand=_no_jitter)
                self.assertEqual(plan["retry"], expect)
        self.assertEqual(jobs.plan_retry(self._rec(retry=cfg, attempt=3), status=jobs.FAILED,
                                        output=API_500, now=NOW)["reason"], "retries-exhausted")

    def test_the_window_is_checked_against_the_SCHEDULED_time_not_just_now(self):
        """A 4-minute backoff that would land past a 2-minute window is a retry that must never be
        scheduled — otherwise the job sits pending until something else notices."""
        cfg = jobs.retry_config(9, backoff=True, window_sec=120)
        rec = self._rec(retry=cfg, attempt=4)          # backoff would be 8 × 30 s = 240 s
        plan = jobs.plan_retry(rec, status=jobs.FAILED, output=API_500, now=NOW, rand=_no_jitter)
        self.assertFalse(plan["retry"])
        self.assertEqual(plan["reason"], "retry-window-exhausted")
        # A short enough delay inside the same window is still fine.
        ok = jobs.plan_retry(self._rec(retry=jobs.retry_config(9, window_sec=120)),
                            status=jobs.FAILED, output=API_500, now=NOW, rand=_no_jitter)
        self.assertTrue(ok["retry"])

    def test_the_window_is_measured_from_creation_not_from_this_attempt(self):
        cfg = jobs.retry_config(9, window_sec=600)
        rec = self._rec(retry=cfg, started_at=jobs._stamp(NOW + timedelta(minutes=20)))
        self.assertEqual(jobs.retry_window_end(rec), NOW + timedelta(seconds=600))
        late = jobs.plan_retry(rec, status=jobs.FAILED, output=API_500,
                              now=NOW + timedelta(minutes=20), rand=_no_jitter)
        self.assertFalse(late["retry"])
        self.assertEqual(late["reason"], "retry-window-exhausted")

    def test_no_window_means_no_window(self):
        self.assertIsNone(jobs.retry_window_end(self._rec()))
        self.assertIsNone(jobs.retry_window_end(self._rec(retry=None)))
        self.assertIsNone(jobs.retry_window_end(self._rec(created_at="garbled")))


class RetryLifecycleTests(unittest.TestCase):
    """The retry loop end to end, through the REAL shim: classify → stamp `retry-pending` → the
    daemon's reconcile launches the due attempt → exactly one push at the real outcome.

    Only `subprocess.Popen` is faked (the grandchild), so the log offsets, the per-attempt delimiter,
    the on-disk record and the reconcile pass are all real."""

    # The timeline every multi-attempt test below runs on, with `--retry-backoff` and jitter pinned to
    # zero: each attempt takes 1 s, then waits 30 s, 60 s, 120 s … so the instants are exact.
    T1 = NOW
    T2 = NOW + timedelta(seconds=31)    # 1 s run + 30 s backoff
    T3 = NOW + timedelta(seconds=92)    # + 1 s run + 60 s backoff

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _start(self, title="retryable", retries=3, backoff=True, window_sec=None, **kw):
        return jobs.start_job(
            self.state, title, ["python", "-c", "pass"], runner=_runner(), now=NOW,
            retry=jobs.retry_config(retries, backoff=backoff, window_sec=window_sec), **kw)

    def _shim(self, job_id, text, code, at=NOW, dur=1.0):
        """Run the real shim in-process against a fake grandchild, on a pinned clock: the attempt
        starts at `at` and ends `dur` later, so both the measured duration (which feeds `fast_fail`)
        and the instant the next attempt is scheduled from are exact."""
        stamps = [at, at + timedelta(seconds=dur)]

        def clock():
            return stamps.pop(0) if len(stamps) > 1 else stamps[0]

        def fake_popen(argv, **kwargs):
            return _FakeChild(code, text, kwargs.get("stdout"))
        with mock.patch.object(jobs.subprocess, "Popen", fake_popen):
            return jobs._run_shim(self.state, job_id, rand=_no_jitter, clock=clock)

    def _rec(self, job_id):
        return jobs.load_job(self.state, job_id)

    # ---------------------------------------------------------------- off by default

    def test_retry_is_off_by_default_and_the_BEHAVIOUR_is_unchanged(self):
        """The safety property everything else rests on: a caller that never heard of `--retry` gets
        the exact behaviour it got before this feature existed — one attempt, terminal on the first
        failure, one push with no retry noise, and a log with no delimiter.

        What it DOES now get is the classification on the record (the test below). Recording is not
        retrying; none of the behaviour asserted here moved."""
        rec = jobs.start_job(self.state, "plain", ["x"], runner=_runner(), now=NOW)
        # No retry POLICY fields — a plain record must not imply a policy it doesn't have.
        for field in ("retry", "attempt", "next_attempt_at", "retry_outcome"):
            self.assertNotIn(field, rec)
        self._shim(rec["id"], API_500, 1)               # the transient line, retry NOT enabled
        after = self._rec(rec["id"])
        self.assertEqual(after["status"], jobs.FAILED)  # terminal on the first failure, as always
        self.assertEqual(len(after["attempts"]), 1)     # ...and it ran exactly once
        for field in ("retry", "attempt", "next_attempt_at", "retry_outcome"):
            self.assertNotIn(field, after)
        notify = _Recorder()
        self.assertEqual(len(jobs.reconcile(self.state, notify=notify, now=NOW)), 1)
        self.assertNotIn("attempt", notify.sent[0][1])   # no retry noise in the push
        # And the log carries no attempt delimiter — a plain job's log looks exactly as it used to.
        with open(after["log_path"], "r", encoding="utf-8") as fh:
            self.assertNotIn("===== attempt", fh.read())

    # ---------------------------------------------------------------- the grandchild's console

    def test_the_shims_child_gets_a_hidden_console_on_windows_and_no_flag_elsewhere(self):
        """The grandchild-console rule (jobs.py docstring): the shim is spawned
        DETACHED (no console), so a console-subsystem command it runs — `claude` is node.exe,
        `gradlew.bat` is cmd.exe — would be handed a fresh VISIBLE console by Windows, which with
        Windows Terminal as the default terminal is one new tab per job. The fix is on the SHIM's
        Popen (CREATE_NO_WINDOW is mutually exclusive with DETACHED_PROCESS, so it cannot ride the
        outer spawn), and it must not leak onto POSIX, where the kwarg is unknown to Popen."""
        seen: list = []

        def fake_popen(argv, **kwargs):
            seen.append((argv, kwargs))
            return _FakeChild(0, "fine", kwargs.get("stdout"))
        for os_name in ("nt", "posix"):
            seen.clear()
            rec = jobs.start_job(self.state, f"console-{os_name}", ["x"], runner=_runner(), now=NOW)
            with mock.patch.object(jobs.subprocess, "Popen", fake_popen), \
                 mock.patch.object(jobs.os, "name", os_name):
                jobs._run_shim(self.state, rec["id"], rand=_no_jitter, clock=lambda: NOW)
            argv, kwargs = seen[0]
            self.assertEqual(argv, ["x"])
            self.assertIs(kwargs["stdin"], jobs.subprocess.DEVNULL)   # unchanged by the flag
            if os_name == "nt":
                self.assertEqual(kwargs["creationflags"], jobs._CREATE_NO_WINDOW)
                self.assertEqual(jobs._CREATE_NO_WINDOW, 0x08000000)
                # ...and NOT the shim's own pair: DETACHED_PROCESS and CREATE_NO_WINDOW are exclusive.
                self.assertFalse(kwargs["creationflags"] & jobs._DETACHED_PROCESS)
            else:
                self.assertNotIn("creationflags", kwargs)
                self.assertNotIn("start_new_session", kwargs)

    # ------------------------------------------- always record the classification

    def test_a_non_retry_job_RECORDS_a_transient_failure_without_retrying_it(self):
        """The `state/metrics.jsonl` lesson, applied: the transient/terminal verdict is the
        diagnostic that says which jobs SHOULD have `--retry`, so collecting it only from jobs that
        already opted in would gather the data strictly after the decision it was meant to inform.

        So a plain job's failure is classified and written down — and still fails terminally, first
        time. Nothing retries because of it."""
        rec = jobs.start_job(self.state, "plain", ["x"], runner=_runner(), now=NOW)
        self._shim(rec["id"], API_500, 1)
        after = self._rec(rec["id"])

        self.assertEqual(after["status"], jobs.FAILED)     # did NOT retry
        self.assertEqual(len(after["attempts"]), 1)        # exactly one attempt ever ran
        att = after["attempts"][0]
        self.assertEqual(att["classification"], jobs.CLASS_TRANSIENT)
        self.assertEqual(att["signature"], "api-5xx")
        self.assertFalse(att["retry_enabled"])             # ...and retry was off at the time
        self.assertEqual(att["decision"], "retry-not-enabled")
        self.assertEqual((att["attempt"], att["outcome"], att["exit_code"]), (1, jobs.FAILED, 1))
        self.assertIsNone(att["next_attempt_at"])

        # The whole point: answerable from the records alone, with no log and no retry config.
        self.assertTrue(any(a["classification"] == jobs.CLASS_TRANSIENT and not a["retry_enabled"]
                            for r in jobs.list_jobs(self.state) for a in r.get("attempts") or []))

    def test_a_non_retry_job_records_an_ordinary_failure_as_terminal(self):
        """The other half — and the reason the field is worth reading. If everything came back
        `transient` the record would tell you nothing."""
        rec = jobs.start_job(self.state, "plain", ["x"], runner=_runner(), now=NOW)
        self._shim(rec["id"], "FAILED (failures=1)\nAssertionError: 3 != 4", 1)
        after = self._rec(rec["id"])
        self.assertEqual(after["status"], jobs.FAILED)
        att = after["attempts"][0]
        self.assertEqual(att["classification"], jobs.CLASS_TERMINAL)
        self.assertIsNone(att["signature"])
        self.assertEqual(att["confidence"], "none")
        self.assertFalse(att["retry_enabled"])

    def test_a_non_retry_job_that_succeeds_records_that_too(self):
        """One shape for every job, so a reader never has to ask why `attempts[]` is missing."""
        rec = jobs.start_job(self.state, "plain", ["x"], runner=_runner(), now=NOW)
        self._shim(rec["id"], "all good", 0)
        after = self._rec(rec["id"])
        self.assertEqual(after["status"], jobs.DONE)
        att = after["attempts"][0]
        self.assertEqual(att["classification"], jobs.CLASS_SUCCESS)
        self.assertEqual(att["decision"], "succeeded")
        self.assertFalse(att["retry_enabled"])
        # And the push is still the plain one — recording adds nothing to what the owner reads.
        notify = _Recorder()
        jobs.reconcile(self.state, notify=notify, now=NOW)
        self.assertNotIn("attempt", notify.sent[0][1])

    def test_a_retry_enabled_attempt_is_marked_as_such(self):
        """`retry_enabled` is per ATTEMPT and must be true where retry was actually on — otherwise
        "transient without retry" would over-count and the diagnostic would be worthless."""
        rec = self._start()
        self._shim(rec["id"], API_500, 1)
        att = self._rec(rec["id"])["attempts"][0]
        self.assertTrue(att["retry_enabled"])
        self.assertEqual(att["decision"], "transient-signature")
        self.assertFalse(any(a["classification"] == jobs.CLASS_TRANSIENT and not a["retry_enabled"]
                             for r in jobs.list_jobs(self.state) for a in r.get("attempts") or []))

    def test_the_classifier_is_not_forked_for_the_non_retry_path(self):
        """Both paths run the same `plan_retry`, so a signature added to `TRANSIENT_SIGNATURES` is
        seen by a plain job too. Pinned by monkeypatching the ONE classifier and watching a
        non-retry job's record change — if a second copy ever appears, this goes red."""
        rec = jobs.start_job(self.state, "plain", ["x"], runner=_runner(), now=NOW)
        with mock.patch.object(jobs, "transient_signature", lambda _text: "invented-signature"):
            self._shim(rec["id"], "something nobody has ever seen", 1)
        att = self._rec(rec["id"])["attempts"][0]
        self.assertEqual(att["signature"], "invented-signature")
        self.assertEqual(att["classification"], jobs.CLASS_TRANSIENT)
        self.assertEqual(self._rec(rec["id"])["status"], jobs.FAILED)  # still no retry

    # ---------------------------------------------------------------- the happy retry path

    def test_a_transient_failure_becomes_retry_pending_and_notifies_nothing(self):
        rec = self._start()
        self._shim(rec["id"], API_500, 1)
        after = self._rec(rec["id"])
        self.assertEqual(after["status"], jobs.RETRY_PENDING)
        self.assertEqual(after["attempt"], 2)
        self.assertEqual(after["next_attempt_at"], jobs._stamp(self.T2))
        self.assertIsNone(after["ended_at"])
        self.assertIsNone(after["pid"])  # the shim has exited; a stale PID would read as alive
        att = after["attempts"][0]
        self.assertEqual((att["attempt"], att["outcome"], att["exit_code"]), (1, jobs.FAILED, 1))
        self.assertEqual((att["classification"], att["signature"]),
                         (jobs.CLASS_TRANSIENT, "api-5xx"))
        self.assertEqual(att["decision"], "transient-signature")
        # A job mid-retry has NOT ended, so nothing may be pushed about it yet.
        notify = _Recorder()
        self.assertEqual(jobs.reconcile(self.state, notify=notify, now=NOW, runner=_runner()), [])
        self.assertEqual(notify.sent, [])

    def test_reconcile_waits_out_the_backoff_then_launches_the_attempt(self):
        rec = self._start()
        self._shim(rec["id"], API_500, 1)
        calls: list = []

        # Before it's due: nothing spawned, still pending.
        jobs.reconcile(self.state, notify=_Recorder(), now=self.T2 - timedelta(seconds=1),
                       runner=_runner(calls=calls))
        self.assertEqual(calls, [])
        self.assertEqual(self._rec(rec["id"])["status"], jobs.RETRY_PENDING)

        # Once due: the shim is respawned for attempt 2, with the same job id and the same detach flags.
        jobs.reconcile(self.state, notify=_Recorder(), now=self.T2, runner=_runner(calls=calls))
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv[-1], rec["id"])
        self.assertIn("__run", argv)
        self.assertNotIn("ANTHROPIC_API_KEY", sorted(kwargs["env"].keys()))
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"],
                             jobs._DETACHED_PROCESS | jobs._CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(kwargs["start_new_session"])
        relaunched = self._rec(rec["id"])
        self.assertEqual(relaunched["status"], jobs.RUNNING)
        self.assertEqual(relaunched["attempt"], 2)
        self.assertIsNone(relaunched["next_attempt_at"])

    def test_success_on_attempt_n_notifies_once_and_says_how_many_it_took(self):
        rec = self._start()
        self._shim(rec["id"], API_500, 1, at=self.T1)
        jobs.reconcile(self.state, notify=_Recorder(), now=self.T2, runner=_runner())
        self._shim(rec["id"], API_500, 1, at=self.T2)
        jobs.reconcile(self.state, notify=_Recorder(), now=self.T3, runner=_runner())
        self._shim(rec["id"], "finally worked", 0, at=self.T3)

        after = self._rec(rec["id"])
        self.assertEqual(after["status"], jobs.DONE)
        self.assertEqual(len(after["attempts"]), 3)
        self.assertEqual(after["retry_outcome"], "succeeded-after-retry")

        notify = _Recorder()
        first = jobs.reconcile(self.state, notify=notify, now=self.T3 + timedelta(minutes=1),
                              runner=_runner())
        second = jobs.reconcile(self.state, notify=notify, now=self.T3 + timedelta(minutes=1),
                               runner=_runner())
        self.assertEqual((len(first), second), (1, []))    # exactly ONE push for the whole job
        text = notify.sent[0][1]
        self.assertIn("✅", text)
        self.assertIn("Took 3 attempts of 4", text)       # not a clean first-try success
        self.assertIn("2 transient failures", text)
        self.assertIn("api-5xx", text)
        self.assertIn("finally worked", text)             # the log's last line, as always

    def test_each_attempt_is_classified_on_its_OWN_output(self):
        """The subtle one. Attempt 1's `API Error: 500` is still sitting in the shared log when
        attempt 2 fails for a real reason — reading the whole log would classify that as transient and
        retry a genuinely broken command. The recorded `log_offset` is what prevents it."""
        rec = self._start()
        self._shim(rec["id"], API_500, 1, at=self.T1)
        jobs.reconcile(self.state, notify=_Recorder(), now=self.T2, runner=_runner())
        self._shim(rec["id"], "AssertionError: 3 != 4", 1, at=self.T2)
        after = self._rec(rec["id"])
        self.assertEqual(after["status"], jobs.FAILED)     # terminal, despite attempt 1's 500 above
        self.assertEqual(after["retry_outcome"], "not-transient")
        self.assertEqual(after["attempts"][1]["classification"], jobs.CLASS_TERMINAL)
        self.assertGreater(after["attempts"][1]["log_offset"], after["attempts"][0]["log_offset"])
        self.assertEqual(len(jobs.reconcile(self.state, notify=_Recorder(),
                                            now=self.T3, runner=_runner())), 1)

    def test_a_stale_transient_line_EARLIER_IN_THE_SAME_ATTEMPT_stays_terminal(self):
        """A stale transient line early in ONE long attempt must not classify its ending.

        `log_offset` fixes the multi-attempt half (the test above) and does nothing for this one: ONE
        attempt can span hours and several phases, and `read_log_span`'s 64 KiB default meant "from
        byte 0" for any log under 64 KiB — which is nearly all of them. A job whose provisioning phase
        printed an ssh timeout near the start, recovered, ran for over an hour and died on something
        unrelated was recorded `transport-timeout` / `transient` — and §7's `--retry` re-runs a
        command off exactly that field.

        Retry is ENABLED here on purpose: with the bug this lands in `retry-pending` and re-runs, so
        the assertion is about behaviour and not only about a recorded string."""
        stale = "[remote] ssh: connect to host ssh.example.net port 28982: Connection timed out"
        # Comfortably past `_CLASSIFY_TAIL_BYTES`, so the stale line is above the failure end.
        filler = "\n".join(f"[remote] step {i:04d}: fine" for i in range(700))
        rec = self._start()
        self._shim(rec["id"], f"{stale}\n{filler}\nlaunch failed: remote payload exited 1", 1)
        after = self._rec(rec["id"])
        att = after["attempts"][0]
        self.assertEqual(att["classification"], jobs.CLASS_TERMINAL)
        self.assertIsNone(att["signature"])
        self.assertEqual(att["decision"], "no-transient-signature")
        self.assertEqual(after["status"], jobs.FAILED)          # terminal, first time — nothing re-ran
        self.assertEqual(after["retry_outcome"], "not-transient")

        # ...and the SAME line at the failure end still classifies transient. This fix is about WHERE
        # the classifier looks; widening or narrowing WHAT it recognises is a different change with a
        # different argument, and this pair is what keeps the two from being confused later.
        live = self._start(title="died on the transport")
        self._shim(live["id"], f"{filler}\n{stale}", 1)
        self.assertEqual(self._rec(live["id"])["attempts"][0]["signature"], "transport-timeout")
        self.assertEqual(self._rec(live["id"])["status"], jobs.RETRY_PENDING)

    def test_fast_fail_measures_the_ATTEMPT_not_the_classifier_window(self):
        """`FAST_FAIL_BYTES` means "this attempt printed almost nothing at all". Measured off the
        bounded window instead, it would be true of every long job whose tail happens to be short —
        an over-reporting corroborator on the one field that lifts stated confidence to `high`."""
        rec = self._start()
        big = "\n".join(f"line {i:05d} of a chatty but doomed run" for i in range(400))
        self._shim(rec["id"], f"{big}\n{API_500}", 1, dur=1.0)
        att = self._rec(rec["id"])["attempts"][0]
        self.assertEqual(att["signature"], "api-5xx")        # the tail is read
        self.assertFalse(att["fast_fail"])                   # ...but ~15 KB is not "printed nothing"
        self.assertEqual(att["confidence"], "medium")
        # The original fast-fail shape — 1 s, 161 bytes, one line — still reads `high`.
        tiny = self._start(title="the fast-fail shape")
        self._shim(tiny["id"], API_500, 1, dur=1.0)
        self.assertTrue(self._rec(tiny["id"])["attempts"][0]["fast_fail"])
        self.assertEqual(self._rec(tiny["id"])["attempts"][0]["confidence"], "high")

    def test_each_attempt_gets_its_own_delimited_block_in_the_log(self):
        rec = self._start()
        self._shim(rec["id"], API_500, 1, at=self.T1)
        jobs.reconcile(self.state, notify=_Recorder(), now=self.T2, runner=_runner())
        self._shim(rec["id"], "second go", 0, at=self.T2)
        with open(self._rec(rec["id"])["log_path"], "r", encoding="utf-8") as fh:
            body = fh.read()
        self.assertEqual(body.count("===== attempt "), 2)
        self.assertIn("===== attempt 1/4", body)
        self.assertIn("===== attempt 2/4", body)
        self.assertLess(body.index("attempt 1/4"), body.index("attempt 2/4"))  # appended, in order
        self.assertIn("second go", body)

    # ---------------------------------------------------------------- giving up

    def test_exhausting_the_retries_reports_as_FAILED_and_says_so(self):
        rec = self._start(retries=1)
        self._shim(rec["id"], API_500, 1, at=self.T1)
        self.assertEqual(self._rec(rec["id"])["status"], jobs.RETRY_PENDING)
        jobs.reconcile(self.state, notify=_Recorder(), now=self.T2, runner=_runner())
        self._shim(rec["id"], API_500, 1, at=self.T2)
        after = self._rec(rec["id"])
        self.assertEqual(after["status"], jobs.FAILED)     # a job out of retries is a FAILED job
        self.assertEqual(after["retry_outcome"], "exhausted")
        self.assertEqual(after["attempts"][-1]["decision"], "retries-exhausted")
        notify = _Recorder()
        self.assertEqual(len(jobs.reconcile(self.state, notify=notify, now=self.T3,
                                            runner=_runner())), 1)
        self.assertIn("❌", notify.sent[0][1])
        self.assertIn("Gave up after 2 attempts of 2", notify.sent[0][1])

    def test_the_retry_window_closing_mid_wait_ends_the_job_rather_than_stranding_it(self):
        """A job must never spend its life in `retry-pending`. If the window closes while it waits,
        reconcile calls it failed and pushes — the no-silent-path invariant applied to retry."""
        rec = self._start(retries=9, window_sec=600)
        self._shim(rec["id"], API_500, 1, at=self.T1)
        self.assertEqual(self._rec(rec["id"])["status"], jobs.RETRY_PENDING)
        calls: list = []
        notify = _Recorder()
        notified = jobs.reconcile(self.state, notify=notify, now=NOW + timedelta(minutes=11),
                                  runner=_runner(calls=calls))
        self.assertEqual(calls, [])                        # nothing relaunched past the window
        after = self._rec(rec["id"])
        self.assertEqual(after["status"], jobs.FAILED)
        self.assertEqual(after["retry_outcome"], "window-expired")
        self.assertEqual(len(notified), 1)
        # It ran only ONE attempt, and that is exactly the case a `n <= 1` shortcut would silence:
        # the owner must still be told the retries were abandoned rather than never wanted.
        self.assertIn("Stopped retrying after the 10m00s retry window (1 attempt)", notify.sent[0][1])
        self.assertEqual(jobs.reconcile(self.state, notify=notify,
                                        now=NOW + timedelta(minutes=12), runner=_runner()), [])

    def test_a_failed_respawn_ends_the_job_instead_of_leaving_it_pending_forever(self):
        rec = self._start()
        self._shim(rec["id"], API_500, 1, at=self.T1)

        def boom(argv, **kwargs):
            raise OSError("no such executable")
        notify = _Recorder()
        notified = jobs.reconcile(self.state, notify=notify, now=self.T2, runner=boom)
        after = self._rec(rec["id"])
        self.assertEqual(after["status"], jobs.FAILED)
        self.assertEqual(after["retry_outcome"], "spawn-failed")
        self.assertEqual(len(notified), 1)

    def test_a_timed_out_attempt_is_never_retried(self):
        rec = self._start()
        # The transient line AND a blown deadline: the signature is there and must still not retry.
        self._shim(rec["id"], API_500, "timeout", at=self.T1)
        after = self._rec(rec["id"])
        self.assertEqual(after["status"], jobs.TIMED_OUT)
        self.assertEqual(after["attempts"][0]["decision"], f"non-retryable-outcome:{jobs.TIMED_OUT}")
        self.assertEqual(len(jobs.reconcile(self.state, notify=_Recorder(), now=self.T2,
                                            runner=_runner())), 1)

    def test_cancel_wins_over_a_pending_retry(self):
        rec = self._start()
        self._shim(rec["id"], API_500, 1, at=self.T1)
        with mock.patch.object(jobs, "kill_pid"):
            jobs.cancel_job(self.state, rec["id"], now=NOW)
        self.assertEqual(self._rec(rec["id"])["status"], jobs.CANCELLED)
        calls: list = []
        notified = jobs.reconcile(self.state, notify=_Recorder(), now=self.T2,
                                  runner=_runner(calls=calls))
        self.assertEqual(calls, [])                        # a cancelled job is not relaunched
        self.assertEqual(len(notified), 1)

    # ---------------------------------------------------------------- durability

    def test_a_pending_retry_survives_a_daemon_restart(self):
        """The part most likely to be got wrong, and the reason the wait is a record rather than a
        `sleep`: the shim EXITS after scheduling, so there is no process holding the retry. A
        successor daemon that never saw the failure reads the same record off disk and launches it.

        Simulated the way a restart actually looks from `jobs.py`: nothing in memory, every input
        re-read from `state/jobs/`, and the record's own `pid` long gone."""
        rec = self._start()
        self._shim(rec["id"], API_500, 1, at=self.T1)
        pending = self._rec(rec["id"])
        self.assertEqual(pending["status"], jobs.RETRY_PENDING)

        # The successor's whole world is the store. Nothing is passed in but the state dir and a clock.
        calls: list = []
        with mock.patch.object(jobs, "pid_alive", return_value=False):   # the old shim is gone
            jobs.reconcile(self.state, notify=_Recorder(), now=NOW + timedelta(minutes=2),
                           runner=_runner(calls=calls))
        self.assertEqual(len(calls), 1, "a restarted daemon must launch the pending retry")
        self.assertEqual(calls[0][0][-1], rec["id"])
        relaunched = self._rec(rec["id"])
        self.assertEqual(relaunched["status"], jobs.RUNNING)
        self.assertEqual(relaunched["attempt"], 2)
        # And the completion push still lands from the successor, off the same record.
        self._shim(rec["id"], "done at last", 0, at=NOW + timedelta(minutes=2))
        notify = _Recorder()
        self.assertEqual(len(jobs.reconcile(self.state, notify=notify,
                                            now=NOW + timedelta(minutes=3), runner=_runner())), 1)
        self.assertIn("Took 2 attempts", notify.sent[0][1])

    def test_an_unreadable_next_attempt_at_costs_the_backoff_not_the_job(self):
        rec = self._start()
        self._shim(rec["id"], API_500, 1, at=self.T1)
        broken = self._rec(rec["id"])
        broken["next_attempt_at"] = "not-a-timestamp"
        jobs.save_job(self.state, broken)
        self.assertEqual(jobs.check_retry_due(broken, NOW), "due")
        calls: list = []
        jobs.reconcile(self.state, notify=_Recorder(), now=NOW, runner=_runner(calls=calls))
        self.assertEqual(len(calls), 1)

    def test_a_retry_pending_job_is_still_ACTIVE(self):
        """It hasn't finished, so `list --active`, the cockpit's `jobs_active` and the session lease
        must all still count it — a leased job whose session wound down mid-backoff would be exactly
        the bug the lease exists to prevent."""
        rec = self._start(lease=True)
        self._shim(rec["id"], API_500, 1, at=self.T1)
        self.assertEqual(self._rec(rec["id"])["status"], jobs.RETRY_PENDING)
        self.assertEqual([r["id"] for r in jobs.list_jobs(self.state, active_only=True)],
                         [rec["id"]])
        self.assertTrue(jobs.lease_active(self.state, NOW + timedelta(minutes=1)))
        # Still bounded from CREATION, so a retrying job can't renew its lease forever.
        self.assertFalse(jobs.lease_active(
            self.state, NOW + timedelta(seconds=jobs.LEASE_MAX_SEC + 1)))

    def test_a_retry_pending_job_is_never_pruned(self):
        rec = self._start()
        self._shim(rec["id"], API_500, 1, at=self.T1)
        self.assertEqual(jobs.prune(self.state, days=0, now=NOW + timedelta(days=90)), 0)
        self.assertIsNotNone(self._rec(rec["id"]))

    def test_the_deadline_is_per_attempt_and_the_window_bounds_the_job(self):
        rec = self._start(retries=9, window_sec=3600)
        self._shim(rec["id"], API_500, 1, at=self.T1)
        jobs.reconcile(self.state, notify=_Recorder(), now=self.T2, runner=_runner())
        running = self._rec(rec["id"])
        # `started_at` is re-stamped per attempt, so attempt 2 gets the FULL deadline, not what
        # attempt 1 left of it...
        self.assertEqual(running["started_at"], jobs._stamp(self.T2))
        with mock.patch.object(jobs, "pid_alive", return_value=True):
            self.assertIsNone(jobs.check_terminal(
                running, self.T2 + timedelta(seconds=running["deadline_sec"] - 1)))
        # ...while the whole-job bound stays pinned to creation.
        self.assertEqual(jobs.retry_window_end(running), NOW + timedelta(hours=1))


class LogContradictsExitCodeTests(unittest.TestCase):
    """A wrapper that exits 0 over a failed payload.

    The motivating job recorded `status: done`, `exit_code: 0` and pushed a ✅ — while three lines
    from the end of its own log said:

        launch failed: remote payload exited 1 on 48221510

    And because `--analyze` fires only on a bad ending, the fresh-eyes analysis the job had asked for
    was never filed. **Two guarantees lost in one silent path**, in the one module whose entire
    purpose is that no failure is silent.

    **The exit code was not lost in `jobs.py`.** The argv was a `wsl.exe … bash -c "bash
    /home/user/launch-remote-run.sh"` wrapper whose gate branch ends `exit 0` on purpose, on the WSL
    side, outside this repo. So the assertion here is NOT `failed` — this module has no
    standing to overrule a command's own exit contract on the strength of prose. It is
    `ended-unknown`: the honest name for two sources of truth that disagree, and the one that puts
    the job back inside `ANALYSABLE`.

    The whole risk of this check is a FALSE positive, so the negative cases below are the load-bearing
    half of the class, not the trimmings."""

    # A tail shaped like the real one (timestamps, the launcher's event lines, then the declaration).
    V7_TAIL = "\n".join([
        '[2026-08-20T17:38:26.393606+00:00] terminate_requested {"instance_id": "48221510"}',
        "[2026-08-20T17:38:27.980193+00:00] notified",
        '[2026-08-20T17:38:27.985083+00:00] launch_finished {"ok": false, '
        '"error": "RemoteFailure(\'remote payload exited 1 on 48221510\')"}',
        "",
        "launch failed: remote payload exited 1 on 48221510",
        "events: /home/user/remote-runs/run-20260820T155935Z-full/"
        "launcher-events.jsonl",
        "token ledger: settled run-20260820T155935Z-full at $0.0517",
    ])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _start(self, title="wrapper swallowed it", *, goal=None, **kw):
        origin = jobs.build_origin(self.state, session_id="sess-v7", goal=goal, env={}) if goal else None
        return jobs.start_job(self.state, title,
                              ["wsl.exe", "-d", "Ubuntu-24.04", "bash", "-c", "bash /home/x.sh"],
                              runner=_runner(), now=NOW, origin=origin, **kw)

    def _shim(self, job_id, text, code, dur=1.0):
        stamps = [NOW, NOW + timedelta(seconds=dur)]

        def clock():
            return stamps.pop(0) if len(stamps) > 1 else stamps[0]

        def fake_popen(argv, **kwargs):
            return _FakeChild(code, text, kwargs.get("stdout"))
        with mock.patch.object(jobs.subprocess, "Popen", fake_popen):
            return jobs._run_shim(self.state, job_id, clock=clock)

    def _rec(self, job_id):
        return jobs.load_job(self.state, job_id)

    # ------------------------------------------------------------------ the incident

    def test_a_wrapper_that_exits_0_over_a_failed_payload_is_NOT_recorded_done(self):
        """The payload exits non-zero, the wrapper's last statements succeed, so the shim observes
        exit 0 — and the job must not read as a success."""
        rec = self._start(analyze=True, goal="rerun the remote training job to completion")
        self._shim(rec["id"], self.V7_TAIL, 0)
        after = self._rec(rec["id"])

        self.assertNotEqual(after["status"], jobs.DONE)
        self.assertEqual(after["status"], jobs.ENDED_UNKNOWN)
        # The observed code is KEPT — it is the evidence, and half of what disagrees.
        self.assertEqual(after["exit_code"], 0)
        why = after["unknown_reason"]
        self.assertEqual(why["kind"], "log-contradicts-exit-code")
        self.assertEqual(why["exit_code"], 0)
        self.assertEqual(why["signature"], "declared-failure")
        self.assertEqual(why["line"], "launch failed: remote payload exited 1 on 48221510")
        # The attempt records the same outcome — a reader must not have to consult the log.
        self.assertEqual(after["attempts"][0]["outcome"], jobs.ENDED_UNKNOWN)

    def test_the_push_says_which_half_is_unreadable_and_ANALYZE_fires(self):
        """The second guarantee a false `done` loses: `--analyze` fires on a bad ending only, so a false
        `done` also silently cancelled the fresh eyes. Still exactly ONE push (spec §2)."""
        rec = self._start("THE REMOTE RUN (v7)", analyze=True, wake=True,
                          goal="rerun the remote training job to completion")
        self._shim(rec["id"], self.V7_TAIL, 0)

        notify, calls = _Recorder(), []
        notified = jobs.reconcile(self.state, notify=notify, wake=lambda _t: None,
                                  now=NOW + timedelta(minutes=99), runner=_runner(calls=calls))
        self.assertEqual(len(notified), 1)
        self.assertEqual(len(notify.sent), 1)
        text = notify.sent[0][1]
        self.assertNotIn("✅", text)
        self.assertIn("exited 0", text)
        self.assertIn("its own log declares a failure", text)
        # The declaration is quoted, and it is NOT the last line — which is why it gets its own part.
        self.assertIn("Its log says: launch failed: remote payload exited 1 on 48221510", text)
        self.assertIn("Last line: token ledger:", text)

        # --analyze fired, on a job that would have been `done` before this change.
        analysis_id = self._rec(rec["id"])["analysis_job"]
        self.assertTrue(analysis_id)
        self.assertEqual(jobs.load_job(self.state, analysis_id)["analysis_for"], rec["id"])

        # ...and no later pass ever pushes about THIS job again. (The analysis is its own record with
        # its own single push — the existing contract, and the thing "exactly once" is counted over.)
        jobs.reconcile(self.state, notify=notify, wake=lambda _t: None,
                       now=NOW + timedelta(minutes=100), runner=_runner())
        self.assertEqual(sum(1 for _c, t in notify.sent if '"THE REMOTE RUN (v7)"' in t), 1)

    def test_the_wake_line_never_carries_an_unqualified_exit_0(self):
        """`wake_text` reads `exit_code`, and a woken session reads the code before the status word.
        The `CANCEL_OWNED_FIELDS` argument — "the same lie in a smaller font" — pointing here."""
        rec = self._start(wake=True)
        self._shim(rec["id"], self.V7_TAIL, 0)
        line = jobs.wake_text(self._rec(rec["id"]))
        self.assertIn("exit 0 — which its own log contradicts", line)
        self.assertIn("ended-unknown", line)

    # ------------------------------------------------------------------ the false-positive half

    def test_a_job_that_merely_MENTIONS_failure_still_finishes(self):
        """Every one of these is a job that really did succeed. If a future widening downgrades one,
        that widening is the bug — this check may only ever be reached by a program declaring its OWN
        outcome, and the window does most of that work."""
        for tail in (
            "Ran 1489 tests in 41.2s\n\nOK",
            "3 tests failed, 1 passed\nretried both, all green\nDone.",
            "retry 1 failed: connection reset\nrecovered on retry 2\nBUILD SUCCESS",
            "Failures: 0\nErrors: 0\nBUILD SUCCESS",
            "failed: 0\nfinished",
            "Failed: none\nfinished",
            "  failed: a sub-step nobody minds\nwrapper finished cleanly",
            "see docs/why-the-build-failed.md for the history\nOK",
            "child exited with exit code 1, which we handled\nall done",
            "npm ERR! code ELIFECYCLE\nrecovered; publishing anyway\ndone",
            "Traceback (most recent call last):\n  ...\ncaught, continuing\nOK",
            '{"ok": false, "probe": "staging"} — that is the PROBE, not us\nall checks passed',
        ):
            with self.subTest(tail=tail.splitlines()[0][:44]):
                rec = self._start(title=tail.splitlines()[0][:30])
                self._shim(rec["id"], tail, 0)
                after = self._rec(rec["id"])
                self.assertEqual(after["status"], jobs.DONE, tail)
                self.assertNotIn("unknown_reason", after)
                self.assertEqual(jobs.notify_text(after)[:1], "✅")

    def test_the_declaration_must_be_in_THIS_attempt_and_near_its_END(self):
        """Two bounds, one test. A failure a job printed and then recovered from is buried under its
        own later output; and a previous ATTEMPT's declaration lives below this attempt's offset."""
        # (a) declared 400 lines before the end of the same attempt — recovered from, not the ending.
        buried = "\n".join(["launch failed: the first host went away"]
                           + [f"[remote] step {i:04d}: fine" for i in range(400)] + ["all done"])
        rec = self._start(title="recovered")
        self._shim(rec["id"], buried, 0)
        self.assertEqual(self._rec(rec["id"])["status"], jobs.DONE)

        # (b) attempt 1 declares a failure and retries; attempt 2 succeeds cleanly.
        retried = jobs.start_job(self.state, "retried", ["x"], runner=_runner(), now=NOW,
                                 retry=jobs.retry_config(2))
        self._shim(retried["id"], f"launch failed: it died\n{API_500}", 1)
        self.assertEqual(self._rec(retried["id"])["status"], jobs.RETRY_PENDING)
        jobs.reconcile(self.state, notify=_Recorder(), now=NOW + timedelta(minutes=5),
                       runner=_runner())
        self._shim(retried["id"], "all good", 0)
        self.assertEqual(self._rec(retried["id"])["status"], jobs.DONE)

    def test_it_only_ever_moves_done_and_only_downward(self):
        """It may not touch an attempt that already reported a failure of its own (there is nothing
        to correct), it may not produce `failed` (that would overrule the command's exit contract),
        and it must not make a retry-enabled job retry anything new."""
        failed = self._start(title="already honest")
        self._shim(failed["id"], self.V7_TAIL, 1)
        self.assertEqual(self._rec(failed["id"])["status"], jobs.FAILED)   # unchanged, exit 1
        self.assertNotIn("unknown_reason", self._rec(failed["id"]))

        # Retry is enabled AND the log carries a transient signature; `ended-unknown` is still never
        # retried (§7.2), so this change re-runs nothing that did not re-run before.
        retryable = jobs.start_job(self.state, "retryable", ["x"], runner=_runner(), now=NOW,
                                   retry=jobs.retry_config(3))
        self._shim(retryable["id"], f"{API_500}\nlaunch failed: and then this", 0)
        after = self._rec(retryable["id"])
        self.assertEqual(after["status"], jobs.ENDED_UNKNOWN)
        self.assertEqual(after["attempts"][0]["decision"], "non-retryable-outcome:ended-unknown")
        self.assertEqual(len(after["attempts"]), 1)

    def test_an_unreadable_log_can_never_manufacture_a_verdict(self):
        """Fail-open, like every other read here: no evidence is not evidence of failure."""
        self.assertEqual(jobs.log_tail_lines(os.path.join(self.state, "nope.log")), [])
        self.assertIsNone(jobs.done_contradiction([]))
        for bad in (None, 0, ["ok", None, 3], [b"launch failed: bytes"]):
            with self.subTest(bad=repr(bad)[:24]):
                self.assertIsNone(jobs.done_contradiction(bad))
        rec = self._start(title="log gone")
        with mock.patch.object(jobs, "log_tail_lines", side_effect=OSError("boom")):
            # Even a reader that RAISES must not strand the attempt without an outcome.
            with self.assertRaises(OSError):
                jobs.log_tail_lines("x")
        self._shim(rec["id"], "quiet success", 0)
        self.assertEqual(self._rec(rec["id"])["status"], jobs.DONE)

    def test_a_partial_first_line_is_discarded_rather_than_matched(self):
        """The bounded read can land mid-line, and a fragment's first character is not the start of a
        line. Matching it would be a false refusal manufactured by where the seek happened to land."""
        path = os.path.join(self.state, "frag.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x" * 40 + "we have not failed: we are iterating\nstill going\ndone\n")
        lines = jobs.log_tail_lines(path, 0, count=5, limit=30)
        self.assertNotIn("failed", " ".join(lines))     # the fragment never becomes a line
        self.assertEqual(lines, ["still going", "done"])
        self.assertIsNone(jobs.done_contradiction(lines))

    def test_notify_text_never_raises_on_a_malformed_unknown_reason(self):
        """`notify_text` runs inside reconcile's per-record try, so a raise here means the job never
        notifies AT ALL — on this pass and every later one (`worktree_note`'s argument, same shape)."""
        for why in (None, "", 3, [], {"kind": "something-else"},
                    {"kind": "log-contradicts-exit-code"},
                    {"kind": "log-contradicts-exit-code", "line": None},
                    {"kind": "log-contradicts-exit-code", "line": ["a", "b"]}):
            with self.subTest(why=repr(why)[:30]):
                rec = {"id": "x", "title": "t", "status": jobs.ENDED_UNKNOWN, "ended_at": None,
                       "unknown_reason": why, "log_path": "", "exit_code": 0}
                self.assertIn("can't tell", jobs.notify_text(rec))
                self.assertIsInstance(jobs.wake_text(rec), str)


class ReconcileStaleSnapshotRaceTests(unittest.TestCase):
    """Jobs that finished for real — a genuine exit code, a populated `attempts[]`, a clean complete
    log with no crash and no traceback — were clobbered a moment later by `reconcile`'s own stale,
    pre-completion snapshot. `list_jobs()`
    reads every record once at the TOP of a tick; `reconcile` then processes them one at a time, and
    `job_completion.evaluate`'s git/`gh` calls for OTHER already-terminal records earlier in that
    same pass can eat enough real wall-clock time for THIS record's own shim to finish in the gap.
    `check_terminal`'s "pid gone" verdict is computed off the stale snapshot (still `running`, no
    `exit_code`, `attempts: []`) — the shim's PID really is gone by then, correctly, because it
    finished — so a blind `rec.update(changes); save_job(...)` overwrote the real completion with an
    unreadable one. The exact §3.10 lost-update shape `honour_cancel` already fixed for a cancel
    racing the shim, never extended to the ordinary finish.

    Reproduced deterministically here by patching `list_jobs` to return the stale snapshot while the
    real file on disk already carries the shim's genuine completion — no sleeping, no timing luck."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _start(self, **kw):
        kw.setdefault("runner", _runner())
        kw.setdefault("now", NOW)
        return jobs.start_job(self.state, "wait-and-merge", ["pwsh", "-File", "x.ps1"], **kw)

    def test_a_completion_written_after_the_snapshot_is_not_clobbered(self):
        rec = self._start()
        stale = dict(rec)  # `list_jobs()`'s snapshot from the top of the tick — still `running`

        finished = dict(rec)
        finished.update({"status": jobs.DONE, "exit_code": 0, "ended_at": jobs._stamp(NOW),
                         "attempts": [{"attempt": 1, "outcome": jobs.DONE, "exit_code": 0}]})
        jobs.save_job(self.state, finished)  # the shim's own real write, landed in the gap

        notify = _Recorder()
        with mock.patch.object(jobs, "list_jobs", return_value=[stale]), \
             mock.patch.object(jobs, "pid_alive", return_value=False):
            notified = jobs.reconcile(self.state, notify=notify, now=NOW + timedelta(minutes=5))

        after = jobs.load_job(self.state, rec["id"])
        self.assertEqual(after["status"], jobs.DONE)
        self.assertEqual(after["exit_code"], 0)
        self.assertEqual(after["attempts"], finished["attempts"])
        self.assertEqual(len(notified), 1)
        self.assertIn("✅", notify.sent[0][1])

    def test_a_retry_pending_write_in_the_gap_is_not_clobbered_either(self):
        """Same race, a different real destination: the shim decided to retry and exited, leaving a
        `retry-pending` record with `pid` cleared. A stale snapshot's pid-gone verdict must not
        overwrite that with `ended-unknown` either — retry-pending is real forward progress too."""
        rec = self._start(retry=jobs.retry_config(2))
        stale = dict(rec)

        pending = dict(rec)
        pending.update({"status": jobs.RETRY_PENDING, "pid": None, "attempt": 2,
                        "next_attempt_at": jobs._stamp(NOW + timedelta(minutes=1))})
        jobs.save_job(self.state, pending)

        with mock.patch.object(jobs, "list_jobs", return_value=[stale]), \
             mock.patch.object(jobs, "pid_alive", return_value=False):
            jobs.reconcile(self.state, notify=_Recorder(), now=NOW + timedelta(seconds=5))

        after = jobs.load_job(self.state, rec["id"])
        self.assertEqual(after["status"], jobs.RETRY_PENDING)

    def test_a_genuinely_dead_shim_still_reports_ended_unknown(self):
        """The fix must not paper over a REAL failure to report: when nothing ever landed a fresher
        write, reconcile still marks the job `ended-unknown` exactly as before."""
        rec = self._start()
        notify = _Recorder()
        with mock.patch.object(jobs, "pid_alive", return_value=False):
            notified = jobs.reconcile(self.state, notify=notify, now=NOW + timedelta(minutes=5))
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0]["status"], jobs.ENDED_UNKNOWN)
        self.assertEqual(jobs.load_job(self.state, rec["id"])["status"], jobs.ENDED_UNKNOWN)


class EndedUnknownWordingTests(unittest.TestCase):
    """`ended-unknown`'s "PID gone with no exit stamp" half is actually two different stories —
    "never reported a PID, nothing ran" vs "a real process WAS running and vanished" — and both once
    rendered as an identical bare "no exit code" in both the completion push and the
    `--wake` synthetic inbound. That is indistinguishable from a job that dies in milliseconds with
    an empty log, so a benign ending wore the alarming one's clothes. `check_terminal` now tags
    `unknown_reason.kind`; `notify_text`/`wake_text` read it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _start(self, **kw):
        kw.setdefault("runner", _runner())
        kw.setdefault("now", NOW)
        return jobs.start_job(self.state, "spawned but vanished", ["x"], **kw)

    def test_check_terminal_tags_never_started_when_no_pid_was_ever_recorded(self):
        rec = self._start()
        rec["pid"] = None
        changes = jobs.check_terminal(rec, NOW + timedelta(seconds=jobs.STARTUP_GRACE_SEC + 1))
        self.assertEqual(changes["status"], jobs.ENDED_UNKNOWN)
        self.assertEqual(changes["unknown_reason"]["kind"], "never-started")

    def test_check_terminal_tags_pid_gone_when_a_real_pid_disappeared(self):
        rec = self._start()
        with mock.patch.object(jobs, "pid_alive", return_value=False):
            changes = jobs.check_terminal(rec, NOW + timedelta(seconds=30))
        self.assertEqual(changes["status"], jobs.ENDED_UNKNOWN)
        self.assertEqual(changes["unknown_reason"]["kind"], "pid-gone-no-stamp")

    def test_notify_text_distinguishes_never_started_from_pid_gone(self):
        never = {"id": "n", "title": "never", "status": jobs.ENDED_UNKNOWN, "ended_at": None,
                 "exit_code": None, "log_path": "",
                 "unknown_reason": {"kind": "never-started"}}
        gone = {"id": "g", "title": "gone", "status": jobs.ENDED_UNKNOWN, "ended_at": None,
                "exit_code": None, "log_path": "",
                "unknown_reason": {"kind": "pid-gone-no-stamp"}}
        never_text = jobs.notify_text(never)
        gone_text = jobs.notify_text(gone)
        self.assertIn("never actually ran", never_text)
        self.assertNotIn("can't tell you whether it finished", never_text)
        self.assertIn("can't tell you whether it finished", gone_text)
        self.assertNotIn("never actually ran", gone_text)

    def test_notify_text_with_no_unknown_reason_reads_as_pid_gone_unchanged(self):
        """An older record, written before this distinction existed, carries no `unknown_reason` at
        all — it must keep reading exactly as it always did, not silently start claiming
        `never-started`."""
        rec = {"id": "o", "title": "old record", "status": jobs.ENDED_UNKNOWN, "ended_at": None,
               "exit_code": None, "log_path": ""}
        text = jobs.notify_text(rec)
        self.assertIn("can't tell you whether it finished", text)

    def test_wake_text_says_never_started_instead_of_a_bare_no_exit_code(self):
        rec = {"id": "n", "title": "never", "status": jobs.ENDED_UNKNOWN, "ended_at": jobs._stamp(NOW),
               "exit_code": None, "log_path": "", "unknown_reason": {"kind": "never-started"}}
        text = jobs.wake_text(rec)
        self.assertIn("never started", text)
        self.assertNotIn(", no exit code", text)

    def test_wake_text_pid_gone_still_says_no_exit_code(self):
        rec = {"id": "g", "title": "gone", "status": jobs.ENDED_UNKNOWN, "ended_at": jobs._stamp(NOW),
               "exit_code": None, "log_path": "", "unknown_reason": {"kind": "pid-gone-no-stamp"}}
        text = jobs.wake_text(rec)
        self.assertIn(", no exit code", text)


@unittest.skipIf(watch_pr is None, "watch_pr lands in wave 28")
class WatchPrTests(unittest.TestCase):
    """`classify` is pure, so the CI verdict is tested without gh, network, or a clock."""

    @staticmethod
    def _check(name, status="COMPLETED", conclusion="SUCCESS"):
        return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}

    def test_all_green(self):
        v = watch_pr.classify([self._check("a"), self._check("b")])
        self.assertTrue(v["done"])
        self.assertEqual((v["passed"], v["failed"]), (2, 0))
        self.assertIn("GREEN", watch_pr.summary_line("214", v))

    def test_pending_is_never_green_and_never_terminal(self):
        v = watch_pr.classify([self._check("a"), self._check("b", status="IN_PROGRESS")])
        self.assertFalse(v["done"])
        self.assertEqual(v["pending"], 1)
        self.assertIn("still pending", watch_pr.summary_line("214", v))

    def test_a_failure_is_named_in_the_summary(self):
        v = watch_pr.classify([self._check("cockpit-server", conclusion="FAILURE")])
        self.assertEqual(v["failed"], 1)
        self.assertIn("cockpit-server", watch_pr.summary_line("214", v))
        self.assertIn("RED", watch_pr.summary_line("214", v))

    def test_skipped_and_neutral_are_not_failures(self):
        v = watch_pr.classify([self._check("android", conclusion="SKIPPED"),
                               self._check("x", conclusion="NEUTRAL")])
        self.assertEqual(v["failed"], 0)
        self.assertTrue(v["done"])

    def test_an_unknown_conclusion_reads_as_a_failure_not_a_pass(self):
        v = watch_pr.classify([self._check("weird", conclusion="SOMETHING_NEW")])
        self.assertEqual(v["failed"], 1)

    def test_legacy_status_contexts(self):
        v = watch_pr.classify([{"__typename": "StatusContext", "context": "legacy", "state": "PENDING"}])
        self.assertEqual(v["pending"], 1)
        v = watch_pr.classify([{"__typename": "StatusContext", "context": "legacy", "state": "FAILURE"}])
        self.assertEqual(v["failed"], 1)

    def test_an_empty_rollup_is_never_reported_as_green(self):
        """The bug the very first dogfood run hit: a PR opened seconds ago has no checks attached
        yet, which is indistinguishable from a repo with no CI — and it was reported ✅ in 1s."""
        v = watch_pr.classify([])
        self.assertTrue(v["empty"])
        line = watch_pr.summary_line("214", v)
        self.assertIn("NO CI checks", line)
        self.assertNotIn("GREEN", line)

    def test_an_empty_rollup_keeps_polling_through_the_grace_then_exits_2(self):
        polls = []

        def run(cmd, **kwargs):
            polls.append(1)
            return mock.Mock(returncode=0, stdout=json.dumps({"statusCheckRollup": []}), stderr="")

        clock = iter([0, 0, 30, 30, 400, 400, 400])
        rc = watch_pr.watch("1", runner=run, sleep=lambda _: None, clock=lambda: next(clock),
                            empty_grace_sec=300, deadline_sec=10_000, out=lambda *_: None)
        self.assertEqual(rc, 2)              # no verdict — emphatically not 0
        self.assertGreater(len(polls), 1)    # it waited rather than declaring victory instantly

    def test_checks_that_appear_after_the_grace_starts_are_still_judged(self):
        payloads = [{"statusCheckRollup": []},
                    {"statusCheckRollup": [self._check("a", status="QUEUED")]},
                    {"statusCheckRollup": [self._check("a")]}]

        def run(cmd, **kwargs):
            return mock.Mock(returncode=0, stdout=json.dumps(payloads.pop(0) if payloads
                                                             else {"statusCheckRollup": []}), stderr="")

        rc = watch_pr.watch("1", runner=run, sleep=lambda _: None, out=lambda *_: None)
        self.assertEqual(rc, 0)

    def test_exit_codes(self):
        def gh(payload, rc=0):
            def run(cmd, **kwargs):
                return mock.Mock(returncode=rc, stdout=json.dumps(payload), stderr="")
            return run
        out: list = []
        green = gh({"statusCheckRollup": [self._check("a")]})
        self.assertEqual(watch_pr.watch("1", runner=green, out=out.append), 0)
        red = gh({"statusCheckRollup": [self._check("a", conclusion="FAILURE")]})
        self.assertEqual(watch_pr.watch("1", runner=red, out=out.append), 1)
        stuck = gh({"statusCheckRollup": [self._check("a", status="QUEUED")]})
        self.assertEqual(watch_pr.watch("1", runner=stuck, out=out.append, once=True), 2)
        self.assertEqual(watch_pr.watch("1", runner=gh({}, rc=1), out=out.append), 3)

    def test_deadline_gives_up_with_exit_2(self):
        clock = iter([0, 0, 10_000, 10_000])
        def run(cmd, **kwargs):
            return mock.Mock(returncode=0, stdout=json.dumps(
                {"statusCheckRollup": [self._check("a", status="QUEUED")]}), stderr="")
        rc = watch_pr.watch("1", runner=run, sleep=lambda _: None,
                            clock=lambda: next(clock), deadline_sec=60, out=lambda *_: None)
        self.assertEqual(rc, 2)


class PresenceWiringTests(unittest.TestCase):
    """presence.py's two touchpoints: the scheduler tick's reconcile (+ the async wake enqueue) and
    the drainer's lease guard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = argparse.Namespace(state_dir=self.tmp.name, no_jobs=False, router_mode="off",
                                       telegram_env="", discord_env=None, stub_send=True)
        self.logs: list = []

    def _state(self):
        state = pr.DaemonState()
        state.pending = []
        return state

    @unittest.skipUnless(hasattr(pr, "_reconcile_jobs"), _PRESENCE_SKIP)
    def test_reconcile_is_skipped_when_disabled(self):
        self.args.no_jobs = True
        with mock.patch.object(jobs, "reconcile") as rec:
            asyncio.run(pr._reconcile_jobs(self._state(), self.args, self.logs.append))
        rec.assert_not_called()

    @unittest.skipUnless(hasattr(pr, "_reconcile_jobs"), _PRESENCE_SKIP)
    def test_a_broken_store_never_breaks_the_tick(self):
        with mock.patch.object(jobs, "reconcile", side_effect=RuntimeError("store on fire")):
            asyncio.run(pr._reconcile_jobs(self._state(), self.args, self.logs.append))
        self.assertTrue(any("job reconcile failed" in m for m in self.logs))

    @unittest.skipUnless(hasattr(pr, "_reconcile_jobs"), _PRESENCE_SKIP)
    def test_wake_jobs_are_enqueued_into_the_action_queue(self):
        done = {"id": "j1", "title": "waker", "status": jobs.DONE, "wake": True,
                "exit_code": 0, "log_path": "x.log", "notify": {"channel": "telegram"}}
        plain = dict(done, id="j2", title="quiet", wake=False)
        state = self._state()
        with mock.patch.object(jobs, "reconcile", return_value=[done, plain]):
            asyncio.run(pr._reconcile_jobs(state, self.args, self.logs.append))
        self.assertEqual(len(state.pending), 1)
        channel, text, _attempts, _topic = state.pending[0]
        self.assertEqual(channel, "telegram")
        self.assertIn("waker", text)

    @unittest.skipUnless(hasattr(pr, "_reconcile_jobs"), _PRESENCE_SKIP)
    def test_reconcile_refreshes_the_cached_active_count_for_the_status_frame(self):
        """`jobs_active` rides the cockpit pipe's status frame, which is a hot path (every turn
        start/end + every queue change) — so it's fed from this tick-refreshed cache rather than
        re-read from disk per push."""
        state = self._state()
        self.assertEqual(state.jobs_active, 0)
        # Real timestamps, not the frozen NOW: `_reconcile_jobs` runs against the wall clock, and a
        # job stamped last year would (correctly) blow its deadline and stop being active.
        jobs.start_job(self.tmp.name, "one", ["x"], runner=_runner())
        jobs.start_job(self.tmp.name, "two", ["x"], runner=_runner(pid=4322))
        with mock.patch.object(jobs, "pid_alive", return_value=True):
            asyncio.run(pr._reconcile_jobs(state, self.args, self.logs.append))
        self.assertEqual(state.jobs_active, 2)

    @unittest.skipUnless(hasattr(pr, "_reconcile_jobs"), _PRESENCE_SKIP)
    def test_status_snapshot_carries_jobs_active_without_touching_disk(self):
        state = self._state()
        state.jobs_active = 3
        args = argparse.Namespace(state_dir=self.tmp.name, model=None)
        with mock.patch.object(jobs, "list_jobs", side_effect=AssertionError("must not read disk")):
            snap = pr._status_snapshot(state, args)
        self.assertEqual(snap["jobs_active"], 3)

    def test_lease_defers_the_wind_down_but_a_pending_control_still_wins(self):
        jobs.start_job(self.tmp.name, "leased", ["x"], lease=True, runner=_runner(), now=NOW)
        soon = NOW + timedelta(minutes=1)  # an explicit clock: the default one would have the lease
                                           # long lapsed, which is correct behaviour but not the
                                           # thing under test here
        self.assertTrue(jobs.lease_active(self.tmp.name, soon))
        # The drainer's guard is `not control_pending and lease_active` — assert the composed
        # condition directly, since a lease that could outrank a graceful restart would silently
        # block a Path-A deploy.
        for control_pending, expect_hold in ((False, True), (True, False)):
            with self.subTest(control_pending=control_pending):
                held = (not control_pending) and jobs.lease_active(self.tmp.name, soon)
                self.assertEqual(held, expect_hold)

class CancelAttributionTests(unittest.TestCase):
    """docs/cancel-attribution-spec.md §8.

    THE ONE NON-NEGOTIABLE this whole class defends: the push stays unconditional. Nothing here may
    add a branch to "does it fire?" — the fix is tone, not suppression. So every test below asserts
    the push COUNT as well as the wording.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name

    def _cancelled(self, **fields):
        rec = {"id": "20260806-000000-aaaa", "title": "transcribe", "status": jobs.CANCELLED,
               "started_at": jobs._stamp(NOW), "ended_at": jobs._stamp(NOW),
               "argv": ["python", "-c", "pass"], "notified_at": None}
        rec.update(fields)
        return rec

    def _push_once(self, rec):
        """Drive the REAL reconcile, so the push count is the thing actually asserted, not a proxy."""
        jobs.save_job(self.state, rec)
        rec_ = _Recorder()
        jobs.reconcile(self.state, notify=rec_, now=NOW + timedelta(seconds=1))
        return rec_.sent

    # ---- case 1: an assistant surface, and the two sources must agree ----

    def test_daemon_and_desktop_produce_THE_SAME_short_string_when_assistant_decided_it(self):
        """Daemon and desktop must agree — and an assistant-decided cancel (no `cancel_request`
        asserting `owner`) does not get "That was me": it gets the short §12 wording, and NEITHER
        source gets a wake alongside it (see CancelWakeSuppressionTests)."""
        heads = []
        for i, source in enumerate(("daemon", "desktop")):
            sent = self._push_once(self._cancelled(
                id="20260806-000000-a%03d" % i,
                cancelled_by={"session_id": "s1", "source": source}))
            self.assertEqual(len(sent), 1, source)
            heads.append(sent[0][1].splitlines()[0])
        self.assertEqual(heads[0], heads[1])
        self.assertIn("cancelled by the assistant", heads[0])
        self.assertNotIn("That was me", heads[0])

    def test_an_assistant_surface_cancel_the_owner_asked_for_KEEPS_that_was_me(self):
        """§12's carve-out: when `cancel_request.by == "owner"` — the owner asked for this cancel, a
        picker tap or a message — the push is unchanged from before this feature: the fuller rung-1
        confirmation, not the short "cancelled by the assistant" acknowledgement. Suppressing THIS
        push would hide the one cancel the owner is actually waiting to hear back on."""
        sent = self._push_once(self._cancelled(
            cancelled_by={"session_id": "s1", "source": "daemon"},
            cancel_request={"by": "owner"}))
        self.assertEqual(len(sent), 1)
        head = sent[0][1].splitlines()[0]
        self.assertIn("That was me", head)
        self.assertNotIn("cancelled by the assistant", head)

    def test_a_build_session_does_NOT_speak_in_the_first_person(self):
        """The "I" is the surfaces the owner is PRESENT at. A session the assistant dispatched is
        not one, so it names its checkout instead."""
        sent = self._push_once(self._cancelled(
            cancelled_by={"session_id": "s1", "source": "build",
                          "cwd": "C:\\Users\\user\\workspace\\repos\\seneschal-dev",
                          "branch": "feat/x"}))
        self.assertEqual(len(sent), 1)
        head = sent[0][1].splitlines()[0]
        self.assertNotIn("That was me", head)
        self.assertIn("seneschal-dev @ feat/x", head)

    # ---- case 2: no attribution == today, byte for byte ----

    def test_an_unattributed_cancel_is_BYTE_IDENTICAL_to_todays_wording(self):
        """Every existing record lands here, and so does every caller that stamps nothing. Unknown
        attribution degrades to today's wording, NEVER to a guess — the push must never say "that was
        me" about something it cannot show was."""
        sent = self._push_once(self._cancelled())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1].splitlines()[0], '\U0001f6d1 "transcribe" cancelled after 0s.')

    # ---- case 3: THE LOAD-BEARING ONE ----

    def test_malformed_attribution_never_raises_out_of_notify_text(self):
        """**This is the test that matters.** `notify_text` runs INSIDE `reconcile`'s per-record try.
        A raise there is caught, logged, and `notified_at` is never stamped — so the next tick
        retries, raises again, and the job NEVER NOTIFIES AT ALL. A cosmetic feature would have
        silently converted the no-silent-path guarantee into a permanent silent path for that record.
        `--why` widens that surface by one more field of operator-typed input, which is why the
        malformed reasons belong in the same test."""
        cases = (
            ("str", {"cancelled_by": "me"}),
            ("none", {"cancelled_by": None}),
            ("list", {"cancelled_by": ["me"]}),
            ("shape", {"cancelled_by": {"nope": 1}}),
            ("nosid", {"cancelled_by": {"source": "daemon"}}),
            ("rdict", {"cancel_reason": {"why": "x"}}),
            ("rlist", {"cancel_reason": ["x"]}),
            ("rint", {"cancel_reason": 7}),
        )
        for i, (label, fields) in enumerate(cases):
            with self.subTest(label):
                sent = self._push_once(self._cancelled(id="20260806-000000-b%03d" % i, **fields))
                self.assertEqual(len(sent), 1, label)
                # every one is unattributable or unreadable -> rung 3, today's wording
                self.assertIn("cancelled after", sent[0][1].splitlines()[0], label)

    # ---- --why, made falsifiable ----

    def test_an_empty_why_writes_no_key_and_renders_identically(self):
        """An absent reason degrades to EXACTLY the flat copy, never to a dangling ": " clause."""
        for empty in ("", "   ", "\n\t ", "\u200b"):
            with self.subTest(repr(empty)):
                self.assertIsNone(jobs.normalise_why(empty))
        baseline = self._push_once(self._cancelled())[0][1]
        with_empty = self._push_once(
            self._cancelled(id="20260806-000000-cccc", cancel_reason=None))[0][1]
        self.assertEqual(baseline.splitlines()[0], with_empty.splitlines()[0])

    def test_a_huge_why_is_bounded_and_ellipsised(self):
        out = jobs.normalise_why("x" * 5000)
        self.assertEqual(len(out), jobs._WHY_CHARS)
        self.assertTrue(out.endswith("\u2026"))

    def test_a_multiline_why_renders_as_ONE_line_and_adds_no_parts(self):
        """The assertion that actually catches a regression: `notify_text` joins its parts with a
        newline, so an embedded newline would make the reason's tail indistinguishable from a
        retry_note or a "Last line:" part. A bare string compare would look fine."""
        why = jobs.normalise_why("wrong\nbranch\r\nentirely\ttabbed")
        self.assertEqual(why, "wrong branch entirely tabbed")
        plain = self._push_once(self._cancelled())[0][1]
        withwhy = self._push_once(
            self._cancelled(id="20260806-000000-dddd", cancel_reason=why))[0][1]
        self.assertEqual(len(withwhy.splitlines()), len(plain.splitlines()))

    def test_terminal_punctuation_is_added_but_never_doubled(self):
        head = jobs._cancelled_head(self._cancelled(cancel_reason="wrong branch"), "t", "5s")
        self.assertTrue(head.endswith("wrong branch."))
        for already in ("wrong branch.", "really?", "stop!", "and so on\u2026"):
            h = jobs._cancelled_head(self._cancelled(cancel_reason=already), "t", "5s")
            self.assertTrue(h.endswith(already), already)

    def test_a_non_string_why_is_rejected_outright_never_coerced(self):
        """`str(x)` of a stray object is how "<object at 0x...>" ends up in a push."""
        for junk in (object(), 7, ["a"], {"a": 1}, None):
            self.assertIsNone(jobs.normalise_why(junk))

    # ---- the writer ----

    def test_cancel_job_stamps_cancelled_by_from_the_env(self):
        rec = jobs.start_job(self.state, "doomed", ["python", "-c", "pass"],
                             runner=_runner(), now=NOW)
        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            out = jobs.cancel_job(self.state, rec["id"], now=NOW, why="wrong branch",
                                  env={jobs.ORIGIN_ENV_VAR: "sess-9",
                                       jobs.SOURCE_ENV_VAR: "daemon"})
        self.assertEqual(out["cancelled_by"]["session_id"], "sess-9")
        self.assertEqual(out["cancelled_by"]["source"], "daemon")
        self.assertEqual(out["cancelled_by"]["stamped_by"], "env")
        self.assertEqual(out["cancel_reason"], "wrong branch")

    def test_cancel_still_happens_when_the_resolver_raises(self):
        """Losing attribution is cosmetic. Failing to cancel is real."""
        rec = jobs.start_job(self.state, "doomed", ["python", "-c", "pass"],
                             runner=_runner(), now=NOW)
        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            with mock.patch.object(jobs, "build_cancelled_by", side_effect=RuntimeError("boom")):
                out = jobs.cancel_job(self.state, rec["id"], now=NOW, why="x")
        self.assertEqual(out["status"], jobs.CANCELLED)
        self.assertNotIn("cancelled_by", out)

    def test_cancelling_an_ALREADY_TERMINAL_job_writes_neither_field(self):
        """There is no un-notified terminal transition left to describe, so a --why writes nothing and
        pushes nothing. Correct, and pinned so nobody "fixes" it."""
        rec = jobs.start_job(self.state, "done already", ["python", "-c", "pass"],
                             runner=_runner(), now=NOW)
        rec.update({"status": jobs.DONE, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, rec)
        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            out = jobs.cancel_job(self.state, rec["id"], now=NOW, why="too late",
                                  env={jobs.ORIGIN_ENV_VAR: "sess-9"})
        self.assertEqual(out["status"], jobs.DONE)
        self.assertNotIn("cancelled_by", out)
        self.assertNotIn("cancel_reason", out)
        self.assertNotIn("cancel_request", out)

    def test_cancel_job_stamps_cancel_request_from_the_flag(self):
        """§12: `cancel_request` says whose IDEA the cancel was, built with `build_request` — the same
        mechanism `start` uses for `origin.request` — pointed at the cancelling session."""
        rec = jobs.start_job(self.state, "doomed", ["python", "-c", "pass"],
                             runner=_runner(), now=NOW)
        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            out = jobs.cancel_job(self.state, rec["id"], now=NOW, why="x",
                                  env={jobs.ORIGIN_ENV_VAR: "sess-9", jobs.SOURCE_ENV_VAR: "daemon"},
                                  requested_by="owner")
        self.assertEqual(out["cancel_request"]["by"], "owner")
        self.assertEqual(out["cancel_request"]["resolved_by"], "flag")

    def test_cancel_job_without_a_requested_by_flag_resolves_via_the_turn_pointer(self):
        """No `--requested-by` and no open turn pointer -> `unresolved`, same default `start` gets —
        never guessed toward either real answer."""
        rec = jobs.start_job(self.state, "doomed", ["python", "-c", "pass"],
                             runner=_runner(), now=NOW)
        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            out = jobs.cancel_job(self.state, rec["id"], now=NOW,
                                  env={jobs.ORIGIN_ENV_VAR: "sess-9", jobs.SOURCE_ENV_VAR: "daemon"})
        self.assertEqual(out["cancel_request"]["by"], "unresolved")

    def test_cancel_request_resolver_raising_costs_only_the_field(self):
        rec = jobs.start_job(self.state, "doomed", ["python", "-c", "pass"],
                             runner=_runner(), now=NOW)
        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            with mock.patch.object(jobs, "build_request", side_effect=RuntimeError("boom")):
                out = jobs.cancel_job(self.state, rec["id"], now=NOW, why="x",
                                      env={jobs.ORIGIN_ENV_VAR: "sess-9"})
        self.assertEqual(out["status"], jobs.CANCELLED)
        self.assertEqual(out["cancel_reason"], "x")
        self.assertNotIn("cancel_request", out)


class CancelWakeSuppressionTests(unittest.TestCase):
    """docs/cancel-attribution-spec.md §12 — don't "read it properly" on jobs the assistant itself
    stopped. Jobs cancelled on purpose and explained to the owner in the same turn used to wake the
    warm session again to "read the log and say plainly what happened" — telling the owner something
    they had already been told.

    Three branches:
    (a) an assistant surface cancelled it on its own initiative -> ONE short line, no wake at all.
    (b) anything not cancelled (or cancelled but not by an assistant surface) -> unchanged.
    (c) `cancel_request.by == "owner"` -> unchanged (the owner asked; a confirmation is right).
    Plus the legacy/unattributed fallback: `wake_text` gains one clause instead of guessing."""

    def _rec(self, **fields):
        rec = {"id": "20260914-000000-aaaa", "title": "design review", "status": jobs.CANCELLED,
              "started_at": jobs._stamp(NOW), "ended_at": jobs._stamp(NOW),
              "argv": ["python", "-c", "pass"], "notified_at": None}
        rec.update(fields)
        return rec

    # ---- cancel_wake_suppressed itself ----

    def test_suppressed_for_an_assistant_surface_cancel_the_assistant_decided_itself(self):
        self.assertTrue(jobs.cancel_wake_suppressed(self._rec(
            cancelled_by={"session_id": "s1", "source": "daemon"})))
        self.assertTrue(jobs.cancel_wake_suppressed(self._rec(
            cancelled_by={"session_id": "s1", "source": "desktop"},
            cancel_request={"by": "assistant"})))
        self.assertTrue(jobs.cancel_wake_suppressed(self._rec(
            cancelled_by={"session_id": "s1", "source": "daemon"},
            cancel_request={"by": "unresolved"})))

    def test_NOT_suppressed_when_the_owner_asked_for_the_cancel(self):
        self.assertFalse(jobs.cancel_wake_suppressed(self._rec(
            cancelled_by={"session_id": "s1", "source": "daemon"},
            cancel_request={"by": "owner"})))

    def test_NOT_suppressed_for_a_build_delegated_session(self):
        """Rung 2 — something the assistant DISPATCHED, not the assistant speaking. The owner may
        not have been watching it; a cancel from one is news, not a repeat."""
        self.assertFalse(jobs.cancel_wake_suppressed(self._rec(
            cancelled_by={"session_id": "s1", "source": "build"})))

    def test_NOT_suppressed_with_no_session_attribution_at_all(self):
        """Rung 3 — genuinely unknown. Falls back to `wake_text`'s appended clause instead."""
        self.assertFalse(jobs.cancel_wake_suppressed(self._rec()))
        self.assertFalse(jobs.cancel_wake_suppressed(self._rec(cancelled_by={"source": "daemon"})))

    def test_NOT_suppressed_for_a_non_cancelled_job(self):
        """(b) — a job that died keeps today's behaviour, unconditionally. Never a branch on
        `cancel_wake_suppressed` for a status other than `cancelled`."""
        for status in (jobs.DONE, jobs.FAILED, jobs.TIMED_OUT, jobs.ENDED_UNKNOWN):
            self.assertFalse(jobs.cancel_wake_suppressed(self._rec(
                status=status, cancelled_by={"session_id": "s1", "source": "daemon"})), status)

    def test_malformed_rec_never_raises(self):
        for junk in (None, "x", 7, [], {}):
            self.assertFalse(jobs.cancel_wake_suppressed(junk))

    # ---- notify_text: one short line, leak note survives, nothing else does ----

    def test_notify_text_is_one_line_when_suppressed(self):
        rec = self._rec(cancelled_by={"session_id": "s1", "source": "daemon"}, wake=True,
                        log_path="C:\\x\\job.log")
        text = jobs.notify_text(rec)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertNotIn("Reading it properly now.", text)
        self.assertNotIn("Log:", text)

    def test_notify_text_keeps_the_worktree_leak_note_even_when_suppressed(self):
        """The one exception: a leak is actionable information about THIS record, not a restatement
        of why the job was cancelled, so dropping it would silently hide it."""
        rec = self._rec(cancelled_by={"session_id": "s1", "source": "daemon"},
                        worktree_leaked=True, worktree={"path": "C:\\x\\wt", "removed": False})
        text = jobs.notify_text(rec)
        self.assertGreater(len(text.splitlines()), 1)

    def test_notify_text_unchanged_when_the_owner_requested_the_cancel(self):
        rec = self._rec(cancelled_by={"session_id": "s1", "source": "daemon"},
                        cancel_request={"by": "owner"}, wake=True)
        text = jobs.notify_text(rec)
        self.assertIn("Reading it properly now.", text)

    # ---- wake_text: the legacy fallback clause ----

    def test_wake_text_gains_the_dedupe_clause_when_attribution_is_unknown(self):
        rec = self._rec(wake=True, argv=["python", "-c", "pass"])
        text = jobs.wake_text(rec)
        self.assertIn("say NOTHING (no message)", text)

    def test_wake_text_carries_no_dedupe_clause_for_a_build_session_cancel(self):
        """Rung 2's attribution IS known — the shim just isn't guessing whether it was already
        explained, because rung 2 usually wasn't."""
        rec = self._rec(wake=True, cancelled_by={"session_id": "s1", "source": "build"})
        text = jobs.wake_text(rec)
        self.assertNotIn("say NOTHING", text)

    def test_wake_text_carries_no_dedupe_clause_on_a_non_cancelled_job(self):
        rec = self._rec(status=jobs.FAILED, wake=True, exit_code=1)
        text = jobs.wake_text(rec)
        self.assertNotIn("say NOTHING", text)

    # ---- the end-to-end wake enqueue, via presence.job_wakes ----

    @unittest.skipUnless(hasattr(pr, "job_wakes"), _PRESENCE_SKIP)
    def test_job_wakes_excludes_a_suppressed_cancel(self):
        suppressed = self._rec(id="a", title="suppressed one", wake=True,
                               cancelled_by={"session_id": "s1", "source": "daemon"})
        kept = self._rec(id="b", title="kept one", wake=True, status=jobs.FAILED, exit_code=1)
        wakes = pr.job_wakes([suppressed, kept])
        self.assertEqual(len(wakes), 1)
        self.assertIn("kept one", wakes[0][1])
        self.assertNotIn("suppressed one", wakes[0][1])

    @unittest.skipUnless(hasattr(pr, "job_wakes"), _PRESENCE_SKIP)
    def test_job_wakes_keeps_an_owner_requested_cancel(self):
        rec = self._rec(wake=True, cancelled_by={"session_id": "s1", "source": "daemon"},
                        cancel_request={"by": "owner"})
        wakes = pr.job_wakes([rec])
        self.assertEqual(len(wakes), 1)

    @unittest.skipUnless(hasattr(pr, "job_wakes"), _PRESENCE_SKIP)
    def test_job_wakes_skips_records_with_no_wake_flag_regardless(self):
        rec = self._rec(wake=False, cancelled_by={"session_id": "s1", "source": "daemon"},
                        cancel_request={"by": "owner"})
        self.assertEqual(pr.job_wakes([rec]), [])


class CancelIsTerminalTests(unittest.TestCase):
    """A cancel is terminal and unoverwritable — spec §3.10, the lost-race cancel.

    **What actually happened.** Of several jobs cancelled together, most recorded `cancelled` with no
    exit code; **one (call it e302) recorded `failed`, exit 1**, with `cancelled_by` gone entirely,
    and that is the story that went out as its completion push. `cancel_job` killed the
    child FIRST and saved LAST — but the kill is exactly what returns the shim from `proc.wait()`, so
    killing first handed the shim a head start on the same unlocked file. Both writers are
    load-modify-save; the ending was whatever the later save said. The shim's own re-read guard was a
    TOCTOU: it re-reads *before* classifying the attempt and writes after, so a cancel landing inside
    that window was simply overwritten. Duration didn't predict the outcome, which is what made it
    look like a race and not a threshold — it was one.

    **Both interleavings are exercised deterministically here** (`_shim_racing`'s `when=`), by driving
    the REAL shim and choosing where in its own sequence the cancel's write lands. Nothing sleeps and
    nothing depends on scheduling: a test for a race that can only be provoked by timing is a test
    that passes by luck, which is the state this bug shipped in."""

    CANCEL_AT = NOW + timedelta(seconds=169)   # 2m49s in, like the one that lost

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _start(self, **kw):
        kw.setdefault("runner", _runner())
        kw.setdefault("now", NOW)
        return jobs.start_job(self.state, "doomed", ["python", "-c", "pass"], **kw)

    def _cancel(self, job_id):
        """A real `cancel_job`, minus the only part a test may not do: signalling a process. The env
        is the one a daemon-side cancel carries, so the push assertions read the real rung."""
        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            return jobs.cancel_job(self.state, job_id, now=self.CANCEL_AT, why="wrong branch",
                                   env={jobs.ORIGIN_ENV_VAR: "sess-c",
                                        jobs.SOURCE_ENV_VAR: "daemon"})

    def _shim_racing(self, job_id, *, when):
        """Run the real shim against a child that gets killed, with the cancel's save landing at a
        CHOSEN point in the shim's sequence.

        * `before-reread` — the cancel's write lands while the shim is still inside `proc.wait()`.
          That is the ordinary path once the claim goes down before the kill, because the kill is what
          `wait` returns from; the shim's re-read then finds it.
        * `after-reread`  — the cancel's write lands between the shim's re-read and the shim's own
          save. **This is e302.** No re-read guard can see it; only a rule applied at write time can.
        """
        fired: list = []

        def cancel_once():
            if not fired:
                fired.append(self._cancel(job_id))

        class _KilledChild(_FakeChild):
            def wait(self, timeout=None):
                if when == "before-reread":
                    cancel_once()
                return 1               # what a killed process leaves behind (taskkill /F on Windows)

        def fake_popen(argv, **kwargs):
            return _KilledChild(1, "half a line of real work", kwargs.get("stdout"))

        real_span = jobs.read_log_span

        def racing_span(*a, **kw):
            # The shim calls this AFTER its re-read and BEFORE its save — the exact lost window.
            if when == "after-reread":
                cancel_once()
            return real_span(*a, **kw)

        stamps = [NOW, self.CANCEL_AT]
        with mock.patch.object(jobs.subprocess, "Popen", fake_popen), \
             mock.patch.object(jobs, "read_log_span", racing_span):
            jobs._run_shim(self.state, job_id, rand=_no_jitter,
                           clock=lambda: stamps.pop(0) if len(stamps) > 1 else stamps[0])
        self.assertTrue(fired, f"the test's own {when} interleaving never fired")
        return jobs.load_job(self.state, job_id)

    # ---------------------------------------------------------------- the race itself

    def test_a_cancel_wins_whichever_write_lands_last(self):
        for when in ("before-reread", "after-reread"):
            with self.subTest(when=when):
                rec = self._start()
                out = self._shim_racing(rec["id"], when=when)
                self.assertEqual(out["status"], jobs.CANCELLED,
                                 "the shim relabelled a cancelled job — this is the e302 bug")
                self.assertIsNone(out["exit_code"],
                                  "a killed process's exit code is not the job's outcome")
                self.assertEqual(out["cancelled_at"], jobs._stamp(self.CANCEL_AT))
                self.assertEqual(out["ended_at"], jobs._stamp(self.CANCEL_AT))
                # Attribution survives too. e302 lost it wholesale, which is why its push could not
                # even reach rung 3's wording, let alone say "that was me".
                self.assertEqual(out["cancelled_by"]["session_id"], "sess-c")
                self.assertEqual(out["cancel_reason"], "wrong branch")

    def test_the_claim_is_on_disk_BEFORE_anything_is_killed(self):
        """The mechanism, not just its effect. Killing first is what made the cancel raceable at all:
        the kill is the event that wakes the shim, so any writing left until after it is a head start
        handed to the other writer."""
        rec = self._start()
        seen: list = []

        def spy(pid):
            seen.append((pid, (jobs.load_job(self.state, rec["id"]) or {}).get("status")))
        with mock.patch.object(jobs, "kill_pid", spy):
            jobs.cancel_job(self.state, rec["id"], now=self.CANCEL_AT)
        self.assertTrue(seen, "nothing was killed at all")
        for pid, status_at_kill in seen:
            self.assertEqual(status_at_kill, jobs.CANCELLED,
                             f"the record still said {status_at_kill!r} when pid {pid} was killed")

    def test_the_losing_writers_attempt_is_kept_as_evidence(self):
        """Deliberate: `honour_cancel` restores the fields a cancel OWNS and writes the rest through.
        The shim really did observe exit 1, and `attempts[]` is where an observed exit code belongs —
        it just doesn't get to name the ending."""
        rec = self._start()
        out = self._shim_racing(rec["id"], when="after-reread")
        self.assertEqual(len(out["attempts"]), 1)
        self.assertEqual(out["attempts"][0]["exit_code"], 1)
        self.assertEqual(out["attempts"][0]["outcome"], jobs.FAILED)
        self.assertEqual(out["status"], jobs.CANCELLED)

    def test_no_later_write_may_relabel_a_cancelled_job(self):
        """The guard on its own, against every status a later writer could carry — including `done`,
        which is the reverse race (the job genuinely finished in the window the cancel was claiming
        it). The rule: once a cancel is claimed, the ending is the cancel's."""
        for status in (jobs.DONE, jobs.FAILED, jobs.TIMED_OUT, jobs.ENDED_UNKNOWN,
                       jobs.RUNNING, jobs.RETRY_PENDING):
            with self.subTest(status=status):
                rec = self._start()
                self._cancel(rec["id"])
                stale = jobs.load_job(self.state, rec["id"])
                stale.update({"status": status, "exit_code": 0,
                              "ended_at": jobs._stamp(NOW + timedelta(hours=1))})
                stale.pop("cancelled_by", None)
                stale.pop("cancelled_at", None)
                stale.pop("cancel_reason", None)
                jobs.save_job(self.state, stale)
                out = jobs.load_job(self.state, rec["id"])
                self.assertEqual(out["status"], jobs.CANCELLED)
                self.assertIsNone(out["exit_code"])
                self.assertEqual(out["ended_at"], jobs._stamp(self.CANCEL_AT))
                self.assertEqual(out["cancelled_at"], jobs._stamp(self.CANCEL_AT))
                self.assertEqual(out["cancelled_by"]["session_id"], "sess-c")
                self.assertEqual(out["cancel_reason"], "wrong branch")

    def test_the_guard_still_lets_every_legitimate_later_write_through(self):
        """It must not turn a cancelled record read-only: `reconcile` stamps `notified_at` on it,
        `release_worktree` records the teardown, and both run AFTER the cancel by design. A guard that
        blocked those would cost the completion push — strictly worse than the bug."""
        rec = self._start()
        self._cancel(rec["id"])
        out = jobs.load_job(self.state, rec["id"])
        out.update({"notified_at": jobs._stamp(NOW), "worktree_leaked": True,
                    "worktree": {"path": "/x", "removed": False}})
        jobs.save_job(self.state, out)
        back = jobs.load_job(self.state, rec["id"])
        self.assertEqual(back["notified_at"], jobs._stamp(NOW))
        self.assertTrue(back["worktree_leaked"])
        self.assertEqual(back["status"], jobs.CANCELLED)

    # ---------------------------------------------------------------- what the record then buys

    def test_the_completion_push_says_cancelled_not_failed(self):
        """The user-visible half of the bug. e302's push told the owner a job had failed with exit 1
        when it had just been stopped on purpose."""
        rec = self._start()
        self._shim_racing(rec["id"], when="after-reread")
        notify = _Recorder()
        sent = jobs.reconcile(self.state, notify=notify, now=self.CANCEL_AT, runner=_runner())
        self.assertEqual(len(sent), 1)
        text = notify.sent[0][1]
        # rung 1, no `cancel_request.by == "owner"` -> the short §12 wording, not
        # "That was me" — but still unmistakably a CANCEL, which is the bug this test guards.
        self.assertIn("cancelled by the assistant", text)
        self.assertIn("wrong branch", text)
        self.assertNotIn("failed", text)
        self.assertNotIn("exit 1", text)

    def test_a_cancelled_job_is_never_analysed(self):
        """A cancel is not a failure to investigate. The analyst is handed a log and a goal line
        describing work that was DELIBERATELY abandoned and has no way to know that, so it spends a
        real one-shot on an incident that never happened — which is exactly what clean cancels with
        `--analyze` used to do."""
        rec = self._start(analyze=True,
                          origin=jobs.build_origin(self.state, session_id="s", goal="ship it"))
        self._shim_racing(rec["id"], when="after-reread")
        notify = _Recorder()
        jobs.reconcile(self.state, notify=notify, now=self.CANCEL_AT, runner=_runner())
        after = jobs.load_job(self.state, rec["id"])
        self.assertEqual(after["status"], jobs.CANCELLED)
        self.assertIsNone(after.get("analysis_job"))
        self.assertEqual([r for r in jobs.list_jobs(self.state) if r.get("analysis_for")], [])
        # The push still landed — losing the analysis may never cost the notification.
        self.assertEqual(len(notify.sent), 1)
        # And the manual door is shut with the same sentence, on purpose.
        with self.assertRaises(ValueError):
            jobs.request_analysis(self.state, rec["id"], goal="ship it", runner=_runner())
        self.assertNotIn(jobs.CANCELLED, jobs.ANALYSABLE)


class AssistantSurfaceNonDemotionTests(unittest.TestCase):
    """docs/cancel-attribution-spec.md §9.4 — without this rule there is no rung 1 for desktop, and
    daemon/desktop agreement is unimplementable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_build_stamp_does_not_demote_a_desktop_entry(self):
        """The machine-wide hook fires on UserPromptSubmit and Stop — twice a turn — so without this
        it would overwrite the desktop marker within seconds of the chat setting it, every turn."""
        import sentinel
        sentinel.write_session_heartbeat(self.tmp.name, "desktop", pid=1, session_id="s1")
        out = sentinel.write_session_heartbeat(self.tmp.name, "build", pid=1, session_id="s1")
        self.assertEqual(out["source"], "desktop")

    def test_a_build_entry_is_not_promoted(self):
        import sentinel
        sentinel.write_session_heartbeat(self.tmp.name, "build", pid=1, session_id="s2")
        out = sentinel.write_session_heartbeat(self.tmp.name, "build", pid=1, session_id="s2")
        self.assertEqual(out["source"], "build")

    def test_a_non_build_source_still_overwrites_normally(self):
        """The rule is narrow: only a `build` stamp is refused. Anything else is an honest update."""
        import sentinel
        sentinel.write_session_heartbeat(self.tmp.name, "desktop", pid=1, session_id="s3")
        out = sentinel.write_session_heartbeat(self.tmp.name, "daemon", pid=1, session_id="s3")
        self.assertEqual(out["source"], "daemon")


class WorktreeJobTests(unittest.TestCase):
    """`--worktree` where it meets `jobs.py` — docs/delegated-work-isolation-spec.md phase 1.

    The command sequence itself is `test_job_worktree.py`'s (fetch before add, `origin/develop` never
    bare, `remove` never `--force`). What this class holds is the wiring, and every test is one of the
    shared-checkout failures said in `jobs.py`'s own terms:

      * the job actually RUNS in the worktree (a flag that records a path and spawns elsewhere would
        be worse than nothing);
      * **the ledger stays the daemon's**, which is the hazard this change ADDS rather than removes:
        `DEFAULT_STATE_DIR` and `REPO_ROOT` are the same constant, so a job whose cwd is a worktree is
        one careless default away from writing its record into a directory scheduled for deletion,
        which no daemon reconciles — a brand-new silent path in the module whose whole invariant is
        that there are none (§5.4);
      * a worktree that cannot be created **fails the job** instead of quietly running it in the
        shared tree;
      * a leak is named in the push, and **nothing about cleanup can cost the push** — the guarantee
        the module exists for.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.roots = tempfile.TemporaryDirectory()
        self.addCleanup(self.roots.cleanup)
        self.root = os.path.join(self.roots.name, "seneschal-worktrees")

    def _start(self, calls=None, **kw):
        kw.setdefault("runner", _runner(calls=calls))
        kw.setdefault("now", NOW)
        return jobs.start_job(self.state, "delegated build", ["claude", "-p", "brief"], **kw)

    def _wt(self, calls=None, git=None, **kw):
        return self._start(calls=calls, worktree=True, worktree_root=self.root,
                           wt_runner=git or _FakeGit(), **kw)

    # ---------------------------------------------------------------- the isolation itself

    def test_the_job_runs_in_the_worktree_not_the_launching_checkout(self):
        calls: list = []
        rec = self._wt(calls=calls)
        path = os.path.join(self.root, rec["id"])
        self.assertEqual(rec["cwd"], path)
        self.assertEqual(rec["worktree"]["path"], path)
        self.assertEqual(rec["worktree"]["base"], "origin/develop")
        self.assertEqual(calls[0][1]["cwd"], path)          # the spawn's cwd, not just the record's
        self.assertNotEqual(rec["cwd"], jobs.REPO_ROOT)

    def test_the_state_dir_stays_the_daemons_and_never_follows_the_cwd(self):
        """§5.4's added hazard, pinned. A record written into the worktree's own `seneschal/state/jobs/`
        would be a ledger no daemon reconciles, in a directory scheduled for deletion."""
        calls: list = []
        rec = self._wt(calls=calls)
        argv = calls[0][0]
        self.assertLess(argv.index("--state-dir"), argv.index("__run"))
        self.assertEqual(argv[argv.index("--state-dir") + 1], self.state)
        self.assertTrue(rec["log_path"].startswith(self.state))
        self.assertFalse(rec["log_path"].startswith(rec["worktree"]["path"]))

    def test_without_the_flag_nothing_changes_and_no_git_runs(self):
        """Additive, in the strict sense: no `worktree` key, the old cwd, and not one git call."""
        git = _FakeGit()
        rec = self._start(wt_runner=git)
        self.assertNotIn("worktree", rec)
        self.assertEqual(rec["cwd"], jobs.REPO_ROOT)
        self.assertEqual(git.calls, [])

    def test_an_explicit_cwd_is_untouched_by_this_change(self):
        rec = self._start(cwd=self.roots.name)
        self.assertEqual(rec["cwd"], os.path.abspath(self.roots.name))
        self.assertNotIn("worktree", rec)

    # ---------------------------------------------------------------- setup failure

    def test_a_failed_worktree_add_fails_the_job_and_spawns_nothing(self):
        """The one that matters most: the alternative to failing here is a delegated agent editing
        and committing in a checkout it was never meant to touch — with a zero exit."""
        calls: list = []
        git = _FakeGit(fail={"worktree add": (128, "fatal: 'x' already exists")})
        rec = self._wt(calls=calls, git=git)
        self.assertEqual(rec["status"], jobs.FAILED)
        self.assertIn("worktree setup failed", rec["error"])
        self.assertEqual(calls, [])                          # nothing was spawned, anywhere
        self.assertIsNone(rec["worktree"]["path"])

    def test_a_failed_setup_still_notifies_exactly_once(self):
        """NO SILENT PATH survives the new failure mode: a job that never started is still a job that
        was promised a report."""
        git = _FakeGit(fail={"fetch": (128, "fatal: unable to access origin")})
        self._wt(git=git)
        notify = _Recorder()
        self.assertEqual(len(jobs.reconcile(self.state, notify=notify, now=NOW)), 1)
        self.assertEqual(len(jobs.reconcile(self.state, notify=notify, now=NOW)), 0)
        self.assertIn("failed", notify.sent[0][1])

    # ---------------------------------------------------------------- teardown

    def _finish(self, rec, status=jobs.DONE):
        rec.update({"status": status, "exit_code": 0, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, rec)
        return rec

    def test_a_finished_job_gives_its_worktree_back(self):
        rec = self._finish(self._wt())
        git = _FakeGit()
        notify = _Recorder()
        jobs.reconcile(self.state, notify=notify, now=NOW, wt_runner=git)
        out = jobs.load_job(self.state, rec["id"])
        self.assertTrue(out["worktree"]["removed"])
        self.assertNotIn("worktree_leaked", out)
        self.assertIn("worktree remove", git.verbs())
        self.assertNotIn("I kept its worktree", notify.sent[0][1])

    def test_a_refused_removal_is_recorded_and_named_in_the_push(self):
        """"kept `…` — it has uncommitted work" is the whole design: 6.7 MB is cheap, and a job that
        ended badly is exactly when its tree is most likely to still hold something."""
        rec = self._finish(self._wt(), status=jobs.FAILED)
        git = _FakeGit(fail={"worktree remove": (1, "fatal: contains modified or untracked files")})
        notify = _Recorder()
        jobs.reconcile(self.state, notify=notify, now=NOW, wt_runner=git)
        out = jobs.load_job(self.state, rec["id"])
        self.assertTrue(out["worktree_leaked"])
        self.assertIn("modified or untracked", out["worktree"]["leak_reason"])
        text = notify.sent[0][1]
        self.assertIn("I kept its worktree", text)
        self.assertIn(out["worktree"]["path"], text)

    def test_teardown_runs_once_however_many_ticks_pass(self):
        """A Telegram outage means reconcile re-reads the same terminal record every tick. A second
        `remove` against a reclaimed path would report a fresh 'leak' for a directory that is gone."""
        rec = self._finish(self._wt())
        git = _FakeGit()
        notify = _Recorder(landed=False)
        for _ in range(3):
            jobs.reconcile(self.state, notify=notify, now=NOW, wt_runner=git)
        self.assertEqual(git.verbs().count("worktree remove"), 1)
        self.assertEqual(len(notify.sent), 3)                # the push kept retrying, as it must

    def test_a_teardown_that_raises_never_costs_the_completion_push(self):
        """Cleanup may not become a way for a job to go silent — the standing rule, and phase 3's
        named regression test."""
        rec = self._finish(self._wt())
        notify = _Recorder()
        with mock.patch.object(jobs.job_worktree, "teardown",
                               side_effect=RuntimeError("git exploded")):
            sent = jobs.reconcile(self.state, notify=notify, now=NOW)
        self.assertEqual(len(sent), 1)
        out = jobs.load_job(self.state, rec["id"])
        self.assertTrue(out["worktree_leaked"])              # loud, not swallowed

    def test_a_cancelled_job_reclaims_its_worktree_on_the_next_tick(self):
        """One of the four paths nobody plans for. A cancel seconds in leaves a clean tree, and
        cleanliness is git's judgment, not ours."""
        rec = self._wt()
        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            jobs.cancel_job(self.state, rec["id"], now=NOW)
        git = _FakeGit()
        jobs.reconcile(self.state, notify=_Recorder(), now=NOW, wt_runner=git)
        self.assertTrue(jobs.load_job(self.state, rec["id"])["worktree"]["removed"])

    def test_a_retrying_job_keeps_its_worktree_until_the_real_outcome(self):
        """`retry-pending` is not terminal, so nothing is torn down under an attempt that is about to
        be re-run in that very directory."""
        rec = self._wt(retry=jobs.retry_config(2))
        rec.update({"status": jobs.RETRY_PENDING, "attempt": 2,
                    "next_attempt_at": jobs._stamp(NOW + timedelta(hours=1))})
        jobs.save_job(self.state, rec)
        git = _FakeGit()
        jobs.reconcile(self.state, notify=_Recorder(), now=NOW, wt_runner=git)
        self.assertIsNone(git.find("worktree remove"))
        self.assertNotIn("torn_down_at", jobs.load_job(self.state, rec["id"])["worktree"])

    # ---------------------------------------------------------------- the note, and its shapes

    def test_the_leak_note_is_silent_on_every_healthy_job(self):
        self.assertEqual(jobs.worktree_note({"id": "j"}), "")
        self.assertEqual(jobs.worktree_note({"worktree": {"path": "/x", "removed": True}}), "")

    def test_the_leak_note_survives_every_malformed_shape(self):
        """`notify_text` runs inside reconcile's per-record try: a raise here means the job never
        notifies at all, on this pass and every later one."""
        for rec in ({"worktree_leaked": True},
                    {"worktree_leaked": True, "worktree": None},
                    {"worktree_leaked": True, "worktree": "a string"},
                    {"worktree_leaked": True, "worktree": {}},
                    {"worktree_leaked": True, "worktree": {"path": "   "}},
                    {"worktree_leaked": True, "worktree": {"path": 17}},
                    "not a record at all"):
            self.assertEqual(jobs.worktree_note(rec), "")
        note = jobs.worktree_note({"worktree_leaked": True,
                                   "worktree": {"path": "/tmp/x", "leak_reason": ["not", "a", "str"]}})
        self.assertIn("/tmp/x", note)                        # still names the path
        self.assertNotIn("\n", note)

    def test_the_cli_refuses_worktree_and_cwd_together(self):
        """Two contradictory instructions about where a job runs is exactly how one ends up somewhere
        nobody intended."""
        code = jobs.main(["--state-dir", self.state, "start", "--title", "x", "--worktree",
                          "--cwd", self.roots.name, "--", "echo", "hi"])
        self.assertEqual(code, 2)
        self.assertEqual(jobs.list_jobs(self.state), [])

    def test_both_flags_are_documented_where_the_model_can_see_them(self):
        """`--cwd` shipped as a bare `add_argument` with NO help string while every neighbouring flag
        carried two to six lines — so the session that launched a job into the live daemon's checkout
        had nothing to read (§9.5). A new flag with the same gap would be the same bug with a newer
        date on it, and this asserts `start --help` actually says what each one does."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            jobs.main(["start", "--help"])
        text = buf.getvalue()
        self.assertIn("--worktree", text)
        self.assertIn("origin/develop", text)          # --worktree says what it cuts from
        self.assertIn("--force", text)                 # …and what it refuses to do on the way out
        self.assertIn("--cwd", text)
        self.assertIn("live one", text)                # --cwd finally says what its default IS


class JobPrDraftResolutionTests(unittest.TestCase):
    """A PR opened by a still-running job is a draft until that job finishes — `job_pr_draft.py`.
    This class holds the `jobs.py` side of the wiring:
    resolving the draft at a job's terminal transition, in its own guard beside `release_worktree`,
    and the completion-push note. `test_job_pr_draft.py` covers the module's own functions in
    isolation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _job(self, job_id="job-1", status=jobs.DONE, exit_code=0):
        rec = {"id": job_id, "title": "delegated build", "status": status, "exit_code": exit_code,
              "ended_at": jobs._stamp(NOW), "started_at": jobs._stamp(NOW), "argv": ["claude"]}
        jobs.save_job(self.state, rec)
        return rec

    def _draft(self, job_id="job-1", pr=860, repo="kumouri/seneschal", branch="feat/x"):
        ok = jobs.job_pr_draft.draft_and_record(
            self.state, repo=repo, pr=pr, branch=branch, job_id=job_id,
            runner=lambda argv: (0, "", ""))
        self.assertTrue(ok)

    def test_a_job_with_no_drafted_pr_is_unaffected(self):
        rec = self._job()
        notify = _Recorder()
        jobs.reconcile(self.state, notify=notify, now=NOW)
        out = jobs.load_job(self.state, rec["id"])
        self.assertNotIn("pr_draft", out)
        self.assertNotIn("ready", notify.sent[0][1])
        self.assertNotIn("draft", notify.sent[0][1])

    def test_a_successful_job_marks_its_drafted_pr_ready(self):
        rec = self._job(status=jobs.DONE, exit_code=0)
        self._draft(job_id=rec["id"])
        notify = _Recorder()
        run = lambda argv: (0, "", "")
        jobs.reconcile(self.state, notify=notify, now=NOW, pr_draft_gh_runner=run)
        out = jobs.load_job(self.state, rec["id"])
        self.assertEqual(out["pr_draft"],
                         [{"repo": "kumouri/seneschal", "pr": 860, "action": "readied"}])
        self.assertIn("is ready for review", notify.sent[0][1])
        self.assertEqual(jobs.job_pr_draft.entries_for_job(self.state, rec["id"]), [])

    def test_a_failed_job_leaves_its_drafted_pr_a_draft(self):
        rec = self._job(status=jobs.FAILED, exit_code=1)
        self._draft(job_id=rec["id"])
        notify = _Recorder()
        run = lambda argv: self.fail("gh should never be called on the leave-draft path")
        jobs.reconcile(self.state, notify=notify, now=NOW, pr_draft_gh_runner=run)
        out = jobs.load_job(self.state, rec["id"])
        self.assertEqual(out["pr_draft"],
                         [{"repo": "kumouri/seneschal", "pr": 860, "action": "left-draft"}])
        self.assertIn("stays a draft", notify.sent[0][1])

    def test_a_cancelled_job_leaves_its_drafted_pr_a_draft(self):
        rec = self._job(status=jobs.CANCELLED, exit_code=None)
        self._draft(job_id=rec["id"])
        notify = _Recorder()
        jobs.reconcile(self.state, notify=notify, now=NOW,
                       pr_draft_gh_runner=lambda argv: (0, "", ""))
        out = jobs.load_job(self.state, rec["id"])
        self.assertEqual(out["pr_draft"][0]["action"], "left-draft")

    def test_a_gh_failure_reports_ready_failed_and_keeps_the_entry(self):
        rec = self._job(status=jobs.DONE, exit_code=0)
        self._draft(job_id=rec["id"])
        notify = _Recorder()
        run = lambda argv: (1, "", "rate limited")
        jobs.reconcile(self.state, notify=notify, now=NOW, pr_draft_gh_runner=run)
        out = jobs.load_job(self.state, rec["id"])
        self.assertEqual(out["pr_draft"][0]["action"], "ready-failed")
        text = notify.sent[0][1]
        self.assertIn("couldn't reach gh", text)
        self.assertIn("gh pr ready 860", text)
        # left standing for a later attempt — never silently dropped
        self.assertEqual(len(jobs.job_pr_draft.entries_for_job(self.state, rec["id"])), 1)

    def test_resolution_runs_once_however_many_ticks_pass(self):
        rec = self._job(status=jobs.DONE, exit_code=0)
        self._draft(job_id=rec["id"])
        calls = []

        def run(argv):
            calls.append(argv)
            return 0, "", ""
        notify = _Recorder()
        for _ in range(3):
            jobs.reconcile(self.state, notify=notify, now=NOW, pr_draft_gh_runner=run)
        self.assertEqual(len(calls), 1)   # `gh pr ready` fired once, not once per tick
        self.assertEqual(len(notify.sent), 1)

    def test_a_raising_resolution_never_costs_the_completion_push(self):
        """Cleanup may not become a way for a job to go silent — the same standing rule
        `test_a_teardown_that_raises_never_costs_the_completion_push` pins for `release_worktree`."""
        rec = self._job(status=jobs.DONE, exit_code=0)
        self._draft(job_id=rec["id"])
        notify = _Recorder()
        with mock.patch.object(jobs.job_pr_draft, "resolve_for_job",
                               side_effect=RuntimeError("boom")):
            sent = jobs.reconcile(self.state, notify=notify, now=NOW)
        self.assertEqual(len(sent), 1)

    # ---------------------------------------------------------------- pr_draft_note, in isolation

    def test_the_note_is_silent_on_every_job_that_never_drafted_a_pr(self):
        self.assertEqual(jobs.pr_draft_note({"id": "j"}), "")
        self.assertEqual(jobs.pr_draft_note({"pr_draft": []}), "")
        self.assertEqual(jobs.pr_draft_note({"pr_draft": None}), "")

    def test_the_note_survives_every_malformed_shape(self):
        for rec in ({"pr_draft": "not a list"},
                    {"pr_draft": [None, "junk", {}]},
                    {"pr_draft": [{"repo": "r", "pr": None, "action": "readied"}]},
                    "not a record at all"):
            self.assertEqual(jobs.pr_draft_note(rec), "")

    def test_the_note_names_the_repo_and_pr_for_each_action(self):
        note = jobs.pr_draft_note({"pr_draft": [
            {"repo": "kumouri/seneschal", "pr": 860, "action": "readied"}]})
        self.assertIn("kumouri/seneschal #860", note)
        self.assertIn("ready for review", note)


class LiveLogTests(unittest.TestCase):
    """**A running job's log must be readable WHILE it runs, and a killed job must keep what it
    produced**.

    Two jobs cancelled in one evening — one `pwsh`, one `claude -p`, no shared child — both left a
    **0-byte** log. The suspicion was the cancel path. It was not: `cancel` demonstrably does not
    touch a byte that has already been written (`test_cancel_keeps_what_the_job_already_wrote`). What
    was happening is that nothing had been written, because C stdio block-buffers stdout when it is a
    file, so a plain `python …` job held its entire output in its own address space until it exited.
    On Windows nothing can be recovered from that: `os.kill` is `TerminateProcess`, so there is no
    signal a doomed process can flush on. Hence the fix is `child_env`'s `PYTHONUNBUFFERED` — stop the
    buffering, rather than try to rescue it afterwards.

    **These are the file's only tests that spawn real processes, deliberately.** Every other test here
    fakes `Popen`, which is right for everything about the record — but buffering is precisely the
    thing that happens *inside* the child, past every seam this module has. A test that fakes the
    child cannot observe it, and would have gone on passing through the whole incident. So the
    liveness pair runs `sys.executable` for real (no network, no fixed sleeps — it polls, so a loaded
    CI box makes it slower and never redder) and the cheap seam assertions below pin the same property
    where it can be checked deterministically."""

    MARKER = "TOP-marker"
    # The child prints, then holds. Long enough that a buffering regression cannot pass by finishing
    # early; the test never waits for it, it is always cancelled.
    HOLD_SEC = 90
    POLL_LIMIT_SEC = 30

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        # Tolerant teardown: on Windows a just-killed shim can still hold the log handle for a
        # moment, and losing the race to delete a temp directory must not turn a passing test red.
        self.addCleanup(self._drop_tmp)

    def _drop_tmp(self):
        with contextlib.suppress(OSError):
            self.tmp.cleanup()

    def _start_talker(self):
        rec = jobs.start_job(
            self.state, "a job that talks then holds",
            [sys.executable, "-c",
             f"import time; print({self.MARKER!r}); time.sleep({self.HOLD_SEC})"])
        self.addCleanup(self._stop, rec)  # never leave one of these running
        return rec

    def _stop(self, rec):
        """Cancel, and on Windows wait for the shim to actually be gone so the temp directory is
        free — an open handle is what blocks a delete there, and nowhere else.

        **Deliberately not waiting on POSIX**, where it would cost the full timeout every run for
        nothing: the shim is detached but still this process's child, so nobody ever `wait()`s it and
        it sits as a **zombie** — which `os.kill(pid, 0)` reports as alive. (That is also where the
        `ResourceWarning: subprocess N is still running` in this class's output comes from. It is
        true and it is the detach model working as designed — `jobs.py` never waits on a job, which
        is the entire point — so it is left visible rather than filtered away.)"""
        jobs.cancel_job(self.state, rec["id"])
        if os.name != "nt":
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and jobs.pid_alive(rec.get("pid")):
            time.sleep(0.1)

    def _await_log(self, rec):
        """Poll the log until the marker lands, up to POLL_LIMIT_SEC. Returns what was read.

        Polled rather than slept: a fixed wait is a flake on a loaded CI box, and the property under
        test is "it arrives before the child exits", not "it arrives within N milliseconds"."""
        deadline = time.monotonic() + self.POLL_LIMIT_SEC
        while time.monotonic() < deadline:
            try:
                with open(rec["log_path"], "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            except OSError:
                body = ""
            if self.MARKER in body:
                return body
            time.sleep(0.25)
        return ""

    def test_the_log_is_readable_while_the_job_is_still_running(self):
        rec = self._start_talker()
        body = self._await_log(rec)
        self.assertIn(self.MARKER, body,
                      "a running job's log stayed empty — `jobs.py status` on a healthy long job is "
                      "then indistinguishable from a wedged one, which is the whole defect")
        # Still running: this is the mid-run read, not an at-exit one.
        self.assertEqual(jobs.load_job(self.state, rec["id"])["status"], jobs.RUNNING)

    def test_cancel_keeps_what_the_job_already_wrote(self):
        rec = self._start_talker()
        before = self._await_log(rec)
        self.assertIn(self.MARKER, before)
        jobs.cancel_job(self.state, rec["id"])
        with open(rec["log_path"], "r", encoding="utf-8", errors="replace") as fh:
            after = fh.read()
        self.assertIn(self.MARKER, after,
                      "cancel ate output the job had already produced — `--analyze` exists for jobs "
                      "that ended badly, and a timeout IS a kill")
        self.assertEqual(jobs.load_job(self.state, rec["id"])["status"], jobs.CANCELLED)

    # ---------------------------------------------------------------- the seam, checked cheaply

    def test_child_env_unbuffers_python_children(self):
        self.assertEqual(jobs.child_env()["PYTHONUNBUFFERED"], "1")

    def test_the_unbuffered_env_reaches_both_the_shim_and_the_real_command(self):
        """Both spawns, because they are two different `Popen` calls: `_spawn_attempt` launches the
        shim and `_run_shim` launches the job's own argv. Losing it on either one loses the log."""
        calls: list = []
        rec = jobs.start_job(self.state, "x", ["python", "-c", "pass"],
                             runner=_runner(calls=calls), now=NOW)
        self.assertEqual(calls[0][1]["env"]["PYTHONUNBUFFERED"], "1")

        seen: list = []

        def fake_popen(argv, **kwargs):
            seen.append(kwargs)
            return _FakeChild(0, "", kwargs.get("stdout"))
        with mock.patch.object(jobs.subprocess, "Popen", fake_popen):
            jobs._run_shim(self.state, rec["id"])
        self.assertEqual(seen[0]["env"]["PYTHONUNBUFFERED"], "1")
        # and the older invariant still holds — checked against the KEY LIST, never the dict itself
        self.assertNotIn("ANTHROPIC_API_KEY", sorted(seen[0]["env"].keys()))

    def test_child_env_stamps_assistant_job_id_when_given(self):
        self.assertEqual(jobs.child_env("20260906-211234-268c")["SENESCHAL_JOB_ID"],
                         "20260906-211234-268c")

    def test_child_env_omits_assistant_job_id_when_not_given(self):
        # child_env() copies the real os.environ, so inside a jobs.py job — where SENESCHAL_JOB_ID is
        # already set on this very process — the un-patched assertion fails, and unittest renders the
        # WHOLE env dict (real API keys included) into the failure message. Pop it inside a
        # patch.dict scope (restored on exit either way) so this passes the same in and out of a job,
        # and assert against the key list, never the dict, so a genuine regression can't leak values.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SENESCHAL_JOB_ID", None)
            env = jobs.child_env()
        self.assertNotIn("SENESCHAL_JOB_ID", sorted(env.keys()))

    def test_assistant_job_id_reaches_both_the_shim_and_the_real_command(self):
        """§3.14: `job_background_guard.py`'s whole scoping signal is this env var, so losing it on
        either spawn point would silently widen the hook back to "fires on nothing" for that half of
        a job's own process tree."""
        calls: list = []
        rec = jobs.start_job(self.state, "x", ["python", "-c", "pass"],
                             runner=_runner(calls=calls), now=NOW)
        self.assertEqual(calls[0][1]["env"]["SENESCHAL_JOB_ID"], rec["id"])

        seen: list = []

        def fake_popen(argv, **kwargs):
            seen.append(kwargs)
            return _FakeChild(0, "", kwargs.get("stdout"))
        with mock.patch.object(jobs.subprocess, "Popen", fake_popen):
            jobs._run_shim(self.state, rec["id"])
        self.assertEqual(seen[0]["env"]["SENESCHAL_JOB_ID"], rec["id"])

    def test_an_unwritable_log_is_recorded_rather_than_left_to_be_explained(self):
        """The one case where an empty log is not the child's doing. It used to be silent, and
        silently identical to "the child hasn't printed yet" — so the next reader of a 0-byte log had
        no way to tell those apart. The job still runs; only its output is lost."""
        rec = jobs.start_job(self.state, "x", ["python", "-c", "pass"],
                             runner=_runner(), now=NOW)
        rec["log_path"] = os.path.join(self.state, "no", "such", "dir", "x.log")
        jobs.save_job(self.state, rec)
        with mock.patch.object(jobs.subprocess, "Popen",
                               lambda argv, **kw: _FakeChild(0, "", kw.get("stdout"))):
            jobs._run_shim(self.state, rec["id"])
        after = jobs.load_job(self.state, rec["id"])
        self.assertIn("log unwritable", after.get("log_error", ""))
        self.assertEqual(after["status"], jobs.DONE)  # an unwritable log never costs the work


class KillTreeTests(unittest.TestCase):
    """**`kill_pid` reaps the tree, because the record's two PIDs are not the whole tree.**

    Measured: a job cancelled at 10 s had its grandchild still running — and still appending to the
    job's log — 15 s after the record said `cancelled`, and still going three minutes later. A
    `pwsh -File build.ps1` job is `pwsh` → `gradlew.bat` → `java`, and only the first of those is on
    the record. The visible symptoms all looked like other bugs: a `--worktree` teardown refused with
    `Permission denied` (a live grandchild holding the directory), a terminal record whose log kept
    growing under `--analyze`, and a "cancelled" job still doing its work.

    The guard matters as much as the reap: **never signal our own process group**, or `killpg`
    takes this process down with the job."""

    def test_windows_kills_the_whole_tree(self):
        calls: list = []
        with mock.patch.object(jobs.os, "name", "nt"):
            jobs.kill_pid(4242, runner=lambda argv, **kw: calls.append(argv))
        self.assertEqual(calls, [["taskkill", "/PID", "4242", "/T", "/F"]])

    def test_a_broken_taskkill_still_kills_the_process_it_knows_about(self):
        """Losing the descendants is bad; losing the cancel entirely is worse."""
        killed: list = []
        with mock.patch.object(jobs.os, "name", "nt"), \
             mock.patch.object(jobs.os, "kill", lambda pid, sig: killed.append(pid)):
            jobs.kill_pid(4242, runner=mock.Mock(side_effect=OSError("no taskkill")))
        self.assertEqual(killed, [4242])

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_posix_kills_the_process_group(self):
        killed: list = []
        with mock.patch.object(jobs.os, "getpgid", lambda pid: 500 if pid else 999), \
             mock.patch.object(jobs.os, "killpg", lambda pgid, sig: killed.append(pgid)):
            jobs.kill_pid(4242)
        self.assertEqual(killed, [500])

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_posix_refuses_to_kill_its_own_group(self):
        """The shim is spawned `start_new_session=True` so it leads its own group — but a child that
        somehow never got one shares OURS, and killpg on that is suicide, not a cancel."""
        killed, grouped = [], []
        with mock.patch.object(jobs.os, "getpgid", lambda pid: 999), \
             mock.patch.object(jobs.os, "killpg", lambda pgid, sig: grouped.append(pgid)), \
             mock.patch.object(jobs.os, "kill", lambda pid, sig: killed.append(pid)):
            jobs.kill_pid(4242)
        self.assertEqual(grouped, [])
        self.assertEqual(killed, [4242])

    def test_still_tolerant_of_nothing_dead_and_junk(self):
        """The contract that has always held: every failure swallowed, never raised. A kill that
        throws out of `reconcile` would cost the completion push."""
        jobs.kill_pid(None)
        jobs.kill_pid(0)
        jobs.kill_pid("not a pid")
        with mock.patch.object(jobs.os, "name", "nt"):
            jobs.kill_pid(999999, runner=mock.Mock(side_effect=Exception("boom")))


class SpawnPreflightTests(unittest.TestCase):
    """**Two jobs died within three seconds of spawning, and neither failure was in
    `jobs.py` or in the job.** In both cases the script path in the argv had already been mangled by
    the shell that launched `jobs.py start`, and in both cases the named file did not exist, so both
    were detectable before the spawn.

    The two verbatim argvs are the point of this class; everything else here is the other half of the
    argument — **FAIL-OPEN ON UNKNOWN, FAIL-CLOSED ON KNOWN-BAD**. A false refusal that blocks a
    legitimate job would be worse than the bug being fixed, so the fail-open cases are pinned as
    hard as the refusals are."""

    # The pwsh job's shape: launched from Git Bash, which ate every backslash, so pwsh received a
    # drive letter with no separator and exited 64.
    PWSH_MANGLED = "C:UsersuserAppDataLocalTempseneschal-scratchrun-ha-spec.ps1"
    # The transcribe job's shape: MSYS path translation rewrote a POSIX-looking argument on its way
    # to a native `.exe`, prefixing Git Bash's own install root, and the bash inside WSL exited 127.
    WSL_MANGLED = ("C:/Program Files/Git/mnt/c/Users/user/workspace/repos/seneschal/seneschal/scripts/"
                   "transcribe.sh")
    WSL_INTENDED = "/mnt/c/Users/user/workspace/repos/seneschal/seneschal/scripts/transcribe.sh"

    @staticmethod
    def _existing(*paths):
        """The `exists` seam: a fixed set of paths that exist, separator-insensitively. No real
        files, so this reads identically on the daemon's Windows box and on CI."""
        known = {str(p).replace("\\", "/") for p in paths}
        return lambda p: str(p).replace("\\", "/") in known

    # ---------------------------------------------------------------- the two real failures

    def test_the_backslash_eaten_pwsh_path_is_refused(self):
        argv = ["pwsh", "-NoProfile", "-File", self.PWSH_MANGLED]
        why = jobs.preflight_refusal(argv, exists=self._existing())
        self.assertIsNotNone(why)
        self.assertIn("argv[3]", why)                      # WHICH argument
        self.assertIn(self.PWSH_MANGLED, why)              # and the path that was checked
        # The cause, named, so the reader doesn't have to know this class of bug exists.
        self.assertIn("drive letter has no separator", why)
        self.assertIn("Git Bash", why)
        self.assertIn("--no-preflight", why)

    def test_the_msys_mangled_wsl_path_is_refused_and_says_msys(self):
        argv = ["wsl.exe", "-d", "Ubuntu-24.04", "bash", self.WSL_MANGLED, "sample.wav"]
        why = jobs.preflight_refusal(argv, exists=self._existing())
        self.assertIsNotNone(why)
        self.assertIn("argv[4]", why)
        self.assertIn(self.WSL_MANGLED, why)
        # The whole reason this signature is detected separately: `exit 127` on a path that LOOKS
        # plausible is otherwise baffling, and the next reader should not re-derive it.
        self.assertIn("MSYS", why)
        self.assertIn("Git Bash", why)

    # ---------------------------------------------------------------- the healthy shapes

    def test_a_healthy_pwsh_file_passes(self):
        script = "C:/Users/user/AppData/Local/Temp/seneschal-scratch/run-ha-spec.ps1"
        self.assertIsNone(jobs.preflight_refusal(["pwsh", "-NoProfile", "-File", script],
                                                 exists=self._existing(script)))

    def test_a_healthy_wsl_bash_mnt_path_passes(self):
        """`/mnt/c/x` is how an ordinary Windows path is spelled inside WSL, so the check translates
        it back rather than declaring every WSL job broken."""
        argv = ["wsl.exe", "-d", "Ubuntu-24.04", "bash", self.WSL_INTENDED, "sample.wav"]
        exists = self._existing("C:/Users/user/workspace/repos/seneschal/seneschal/scripts/transcribe.sh")
        self.assertIsNone(jobs.preflight_refusal(argv, exists=exists))

    def test_a_missing_wsl_script_is_still_caught_after_translation(self):
        argv = ["wsl.exe", "-d", "Ubuntu-24.04", "bash", self.WSL_INTENDED]
        why = jobs.preflight_refusal(argv, exists=self._existing())
        self.assertIsNotNone(why)
        self.assertIn("C:/Users/user/workspace", why)          # the RESOLVED path is reported
        self.assertIn("genuinely missing", why)                 # no mangling signature to blame

    def test_a_healthy_python_script_passes_and_a_missing_one_does_not(self):
        script = "C:/repo/seneschal/scripts/watch_pr.py"
        self.assertIsNone(jobs.preflight_refusal(["python", "-u", script, "214"],
                                                 exists=self._existing(script)))
        self.assertIsNotNone(jobs.preflight_refusal(["python", "-u", script, "214"],
                                                    exists=self._existing()))

    # ---------------------------------------------------------------- fail-open on unknown

    def test_an_argv_with_no_recognisable_script_path_is_left_alone(self):
        """The common case by a wide margin. Nothing here is definitionally a path to an existing
        file, so nothing is checked — including the shapes whose next argument is a PROGRAM."""
        nothing_exists = self._existing()
        for argv in (["git", "status"],
                     ["npm", "ci"],
                     ["claude", "-p", "implement phase 1"],
                     ["bash", "-c", "echo /nope/not/a/path.sh"],
                     ["bash", "-lc", "pytest -q"],
                     ["python", "-m", "unittest", "discover"],
                     ["python", "-c", "print(1)"],
                     ["pwsh", "-Command", "Get-ChildItem C:\\nope"],
                     ["wsl.exe", "--shutdown"],
                     ["wsl.exe", "--some-future-flag", "bash", "/mnt/c/nope.sh"]):
            self.assertIsNone(jobs.preflight_refusal(argv, exists=nothing_exists), argv)

    def test_a_relative_path_is_left_alone_because_cwd_decides_it(self):
        """What a relative path resolves against is the job's `cwd` — and a `--worktree` job's `cwd`
        does not exist yet when this runs. Untestable is not missing."""
        self.assertIsNone(jobs.preflight_refusal(["bash", "scripts/build.sh"],
                                                 exists=self._existing()))
        self.assertIsNone(jobs.preflight_refusal(["python", "seneschal/scripts/watch_pr.py", "214"],
                                                 exists=self._existing()))

    def test_a_posix_path_that_is_not_under_mnt_is_left_alone(self):
        """`/home/user/x.sh` lives in the WSL filesystem and `/c/Users/…` is Git Bash's own spelling
        — a Windows process can stat neither. That is unknown, and unknown must never cost a
        legitimate job. **A shell's script argument is read as a POSIX path even without a `wsl.exe`
        prefix**, which is why a bare `bash` gets the same treatment."""
        self.assertIsNone(jobs.preflight_refusal(["wsl.exe", "-d", "Ubuntu-24.04", "bash",
                                                  "/home/user/x.sh"], exists=self._existing()))
        self.assertIsNone(jobs.preflight_refusal(["bash", "/c/Users/user/x.sh"],
                                                 exists=self._existing()))
        self.assertIsNone(jobs.preflight_refusal(["sh", "/home/user/x.sh"],
                                                 exists=self._existing()))

    def test_the_preflight_never_raises(self):
        """It runs immediately before every spawn, so a raise here would block every job — the
        module-wide fail-open posture, in the one place it would hurt most."""
        exploding = mock.Mock(side_effect=OSError("no filesystem today"))
        for argv in (None, [], ["pwsh", "-File"], [None], [object()], "not a list",
                     ["wsl.exe"], ["bash", "--"], ["python", "--"]):
            self.assertIsNone(jobs.preflight_refusal(argv, exists=exploding), argv)
        self.assertIsNone(jobs.preflight_refusal(["pwsh", "-File", "C:/x.ps1"], exists=exploding))

    # ---------------------------------------------------------------- the CLI

    def test_a_refusal_writes_no_job_record_and_spawns_nothing(self):
        """A refused job must not appear in the ledger as failed — it never started. Exit 2, matching
        the `--analyze`-without-`--goal` and `--worktree`-with-`--cwd` refusals beside it."""
        started: list = []

        def never(*a, **kw):
            started.append(a)
            raise AssertionError("the preflight let a mangled argv through to the spawn")

        with tempfile.TemporaryDirectory() as state:
            with mock.patch.object(jobs, "start_job", never), mock.patch("sys.stderr"):
                code = jobs.main(["--state-dir", state, "start", "--title", "ha spec", "--",
                                  "pwsh", "-NoProfile", "-File", self.PWSH_MANGLED])
            self.assertEqual(code, 2)
            self.assertEqual(jobs.list_jobs(state), [])
            self.assertEqual(started, [])

    def test_no_preflight_bypasses_the_check(self):
        """A genuinely unusual argv must stay launchable — the escape hatch is for a preflight that
        is wrong, not for a path that is."""
        real_start = jobs.start_job

        def stubbed(*a, **kw):
            kw["runner"] = _runner()
            return real_start(*a, **kw)

        with tempfile.TemporaryDirectory() as state:
            with mock.patch.object(jobs, "start_job", stubbed), mock.patch("sys.stdout"):
                code = jobs.main(["--state-dir", state, "start", "--title", "ha spec",
                                  "--no-preflight", "--",
                                  "pwsh", "-NoProfile", "-File", self.PWSH_MANGLED])
            self.assertEqual(code, 0)
            self.assertEqual(len(jobs.list_jobs(state)), 1)

    def test_a_healthy_job_still_starts_through_the_cli(self):
        """The preflight is inert for everything it does not recognise, asserted end to end — a guard
        that quietly blocked ordinary jobs would be the worse bug."""
        real_start = jobs.start_job

        def stubbed(*a, **kw):
            kw["runner"] = _runner()
            return real_start(*a, **kw)

        with tempfile.TemporaryDirectory() as state:
            with mock.patch.object(jobs, "start_job", stubbed), mock.patch("sys.stdout"):
                code = jobs.main(["--state-dir", state, "start", "--title", "ci",
                                  "--", "git", "status"])
            self.assertEqual(code, 0)
            self.assertEqual(len(jobs.list_jobs(state)), 1)


class PreflightNamespaceTests(unittest.TestCase):
    """**The preflight once checked the path in the wrong namespace, and so passed a job that could
    not possibly find its script.**

    A long measurement job died at **exit 127 in under a second** on
    `/bin/bash: C:/Users/…/scratchpad/probe_longleash.sh: No such file or directory` — with the script
    sitting exactly there. Bare `bash` resolves to `C:\\WINDOWS\\system32\\bash.exe` on this host,
    which is **WSL's** bash, and WSL reaches the Windows filesystem only under `/mnt/`. The preflight
    asked *Windows* whether the file existed while the command was about to ask *WSL*.

    The refusals here are one half; **the fail-open cases are the other half and are pinned just as
    hard**, because a Git-for-Windows `bash.exe` reads `C:/…` perfectly well and refusing those jobs
    would be worse than the bug. Every test drives the seams — `exists`, `which`, `is_windows` — so
    the verdict is the same on the daemon's Windows box and on CI's Linux, and **nothing here spawns
    anything**."""

    # The failing argv's shape, as recorded on the job.
    FAILING_ARGV = ["bash", "C:/Users/user/AppData/Local/Temp/claude/"
                            "00000000-0000-0000-0000-00000000a001/scratchpad/probe_longleash.sh"]
    WSL_FORM = ("/mnt/c/Users/user/AppData/Local/Temp/claude/"
                "00000000-0000-0000-0000-00000000a001/scratchpad/probe_longleash.sh")

    # The two `bash.exe`s that matter, and the whole distinction between them is where they live.
    WSL_BASH = r"C:\WINDOWS\system32\bash.exe"
    GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"

    @staticmethod
    def _which(resolved):
        return lambda token: resolved

    @staticmethod
    def _everything_exists(_path):
        """The point of the whole class: **existence is not the question.** Saying yes to every probe
        isolates the namespace verdict from the missing-file one — a check that only fired when the
        file was also absent would be the old bug with extra words."""
        return True

    # ---------------------------------------------------------------- the measured failure

    def test_the_real_failing_argv_is_refused(self):
        why = jobs.preflight_refusal(self.FAILING_ARGV, exists=self._everything_exists,
                                     which=self._which(self.WSL_BASH), is_windows=True)
        self.assertIsNotNone(why)
        self.assertIn("argv[1]", why)                      # WHICH argument
        self.assertIn("WSL", why)                          # the interpreter's namespace
        self.assertIn(self.WSL_BASH, why)                  # ...and the evidence for it
        self.assertIn(self.FAILING_ARGV[1], why)           # the path as given
        self.assertIn(self.WSL_FORM, why)                  # the path it would have to be
        self.assertIn("pwsh -NoProfile -File", why)        # fix 1: run it in the Windows namespace
        self.assertIn("--no-preflight", why)
        # It must not read as a missing file — that wording is what sent the reader to the script
        # instead of to the interpreter, and it is the actual damage this fixes.
        self.assertIn("Nothing is missing", why)
        self.assertNotIn("genuinely missing", why)

    def test_the_refusal_does_not_rewrite_the_argv(self):
        """A caller's command is not ours to edit — a job that runs something slightly different from
        what was asked is the failure class the preflight exists to prevent. So the refusal SAYS so,
        and the argv it was handed comes back untouched."""
        argv = list(self.FAILING_ARGV)
        why = jobs.preflight_refusal(argv, exists=self._everything_exists,
                                     which=self._which(self.WSL_BASH), is_windows=True)
        self.assertIn("NOT rewritten", why)
        self.assertEqual(argv, self.FAILING_ARGV)

    # ---------------------------------------------------------------- the mirror image

    def test_a_wsl_path_handed_to_a_windows_interpreter_is_refused(self):
        """`/mnt/c` is not the C: drive to a native Windows process — it is a directory called
        `\\mnt\\c` on whatever drive happens to be current. Same bug, other direction."""
        for argv, idx in ((["pwsh", "-NoProfile", "-File", "/mnt/c/tmp/run.ps1"], 3),
                          (["python", "-u", "/mnt/c/tmp/run.py"], 2)):
            why = jobs.preflight_refusal(argv, exists=self._everything_exists,
                                         which=self._which(None), is_windows=True)
            self.assertIsNotNone(why, argv)
            self.assertIn(f"argv[{idx}]", why)
            self.assertIn("WINDOWS namespace", why)
            self.assertIn("/mnt/c/tmp/run.", why)          # as given
            self.assertIn("C:/tmp/run.", why)              # would need
            self.assertIn("--no-preflight", why)

    # ---------------------------------------------------------------- what must still be allowed

    def test_a_mnt_path_under_bash_is_allowed(self):
        """The correct spelling for a WSL bash, and the one the failing job should have used."""
        argv = ["bash", self.WSL_FORM]
        self.assertIsNone(jobs.preflight_refusal(argv, exists=self._everything_exists,
                                                 which=self._which(self.WSL_BASH), is_windows=True))

    def test_a_windows_path_under_pwsh_file_is_allowed(self):
        argv = ["pwsh", "-NoProfile", "-File", "C:/Users/user/scratch/run-ha-spec.ps1"]
        self.assertIsNone(jobs.preflight_refusal(argv, exists=self._everything_exists,
                                                 which=self._which(None), is_windows=True))

    def test_a_git_for_windows_bash_reads_windows_paths_and_is_not_refused(self):
        """**The false refusal this had to avoid.** Git Bash accepts `C:/…`, so the same argv that is
        broken under WSL bash is a working job under that one — which is why the verdict is taken from
        the interpreter that actually resolves, and never from the shape of the argv alone."""
        self.assertIsNone(jobs.preflight_refusal(self.FAILING_ARGV, exists=self._everything_exists,
                                                 which=self._which(self.GIT_BASH), is_windows=True))

    def test_an_unrecognisable_interpreter_fails_open(self):
        """Three ways of not being able to tell, and all three allow the spawn: a `bash` that is not
        on PATH at all, a `which` that raises, and a non-Windows host, where the host's own shell IS
        the namespace and there is no second one to be mismatched against."""
        def exploding(_token):
            raise OSError("no PATH today")

        for kwargs in ({"which": self._which(None), "is_windows": True},
                       {"which": exploding, "is_windows": True},
                       {"which": self._which("/usr/bin/bash"), "is_windows": True},
                       {"which": self._which(self.WSL_BASH), "is_windows": False}):
            self.assertIsNone(jobs.preflight_refusal(self.FAILING_ARGV,
                                                     exists=self._everything_exists, **kwargs),
                              kwargs)

    def test_the_namespace_check_never_claims_an_untranslatable_path(self):
        """A path that is neither drive-rooted nor `/mnt/<drive>/…` has no counterpart to name, so
        there is nothing to prove and nothing is refused — `/home/user/x.sh` under WSL is simply the
        distro's own filesystem, and a relative path is still the job's `cwd` to decide."""
        for argv in (["bash", "/home/user/x.sh"],
                     ["bash", "/c/Users/user/x.sh"],
                     ["bash", "scripts/build.sh"],
                     ["pwsh", "-File", "C:/tmp/x.ps1"]):
            self.assertIsNone(jobs.preflight_refusal(argv, exists=self._everything_exists,
                                                     which=self._which(self.WSL_BASH),
                                                     is_windows=True), argv)

    def test_the_msys_signature_still_wins_over_the_namespace_message(self):
        """The MSYS-mangled argv is a Windows-shaped string that was never meant to be one, so its
        own message — the one that stops the next reader re-deriving `exit 127` — outranks a namespace
        complaint about the shape it accidentally has."""
        mangled = ("C:/Program Files/Git/mnt/c/Users/user/workspace/repos/seneschal/seneschal/scripts/"
                   "transcribe.sh")
        why = jobs.preflight_refusal(["wsl.exe", "-d", "Ubuntu-24.04", "bash", mangled],
                                     exists=SpawnPreflightTests._existing(),
                                     which=self._which(self.WSL_BASH), is_windows=True)
        self.assertIsNotNone(why)
        self.assertIn("MSYS", why)

    # ---------------------------------------------------------------- the classifier itself

    def test_the_namespace_is_read_off_where_the_interpreter_lives(self):
        """System32 is the evidence, and it is the only evidence: `bash.exe` there is WSL's launcher.
        Anything else resolves to UNKNOWN rather than to Windows, because "not WSL" is not a proof."""
        cases = ((r"C:\WINDOWS\system32\bash.exe", jobs.NS_WSL),
                 (r"C:\WINDOWS\Sysnative\bash.exe", jobs.NS_WSL),
                 (r"C:\WINDOWS\system32\bash.EXE", jobs.NS_WSL),   # what which() really returns here
                 (r"C:\Program Files\Git\bin\bash.exe", jobs.NS_UNKNOWN),
                 ("/usr/bin/bash", jobs.NS_UNKNOWN),
                 (None, jobs.NS_UNKNOWN))
        for resolved, expected in cases:
            ns, _ = jobs._command_namespace("bash", which=self._which(resolved), is_windows=True)
            self.assertEqual(ns, expected, resolved)
        # `wsl.exe` needs no lookup at all — it is WSL by construction, whatever it is asked to run.
        self.assertEqual(jobs._script_path_args(["wsl.exe", "-d", "U", "bash", "/mnt/c/x.sh"],
                                                which=self._which(None), is_windows=True)[0][3],
                         jobs.NS_WSL)

    def test_the_two_translations_are_inverses(self):
        self.assertEqual(jobs._windows_to_mnt("C:/Users/user/x.sh"), "/mnt/c/Users/user/x.sh")
        self.assertEqual(jobs._windows_to_mnt(r"C:\Users\user\x.sh"), "/mnt/c/Users/user/x.sh")
        self.assertEqual(jobs._mnt_to_windows("/mnt/c/Users/user/x.sh"), "C:/Users/user/x.sh")
        # Neither may claim a path that is not its own shape — that is what keeps the refusal narrow.
        self.assertIsNone(jobs._windows_to_mnt("C:Usersuserx.sh"))   # the eaten-backslash mangling
        self.assertIsNone(jobs._windows_to_mnt("/home/user/x.sh"))
        self.assertIsNone(jobs._mnt_to_windows("/mnt"))
        self.assertIsNone(jobs._mnt_to_windows("relative/x.sh"))

    def test_the_namespace_check_never_raises(self):
        """It runs immediately before every spawn, so a raise here would block every job."""
        exploding = mock.Mock(side_effect=OSError("no filesystem today"))
        for argv in (None, [], ["bash"], ["bash", "--"], [None], [object()], "not a list",
                     ["wsl.exe"], ["pwsh", "-File"], self.FAILING_ARGV):
            self.assertIsNone(jobs._namespace_refusal(0, argv, jobs.NS_UNKNOWN, ""), argv)
        for argv in (None, [], ["bash", "C:/x.sh"], ["pwsh", "-File", "/mnt/c/x.ps1"]):
            jobs.preflight_refusal(argv, exists=exploding,
                                   which=mock.Mock(side_effect=OSError("nope")), is_windows=True)

    # ---------------------------------------------------------------- the CLI

    def test_the_cli_refuses_the_real_argv_and_spawns_nothing(self):
        """End to end, through the seams the CLI itself resolves at call time: exit 2, no record, no
        spawn. A refused job never started, so it must not read as a failed one."""
        def never(*a, **kw):
            raise AssertionError("the preflight let a wrong-namespace argv through to the spawn")

        with tempfile.TemporaryDirectory() as state:
            with mock.patch.object(jobs, "start_job", never), \
                    mock.patch.object(jobs, "_default_which", self._which(self.WSL_BASH)), \
                    mock.patch.object(jobs, "_HOST_IS_WINDOWS", True), \
                    mock.patch("sys.stderr"):
                code = jobs.main(["--state-dir", state, "start", "--title", "probe", "--"]
                                 + self.FAILING_ARGV)
            self.assertEqual(code, 2)
            self.assertEqual(jobs.list_jobs(state), [])

    def test_no_preflight_still_bypasses_the_namespace_check(self):
        """Unchanged, and deliberately so: the escape hatch is for a preflight that is wrong, never
        for a path that is."""
        real_start = jobs.start_job

        def stubbed(*a, **kw):
            kw["runner"] = _runner()
            return real_start(*a, **kw)

        with tempfile.TemporaryDirectory() as state:
            with mock.patch.object(jobs, "start_job", stubbed), \
                    mock.patch.object(jobs, "_default_which", self._which(self.WSL_BASH)), \
                    mock.patch.object(jobs, "_HOST_IS_WINDOWS", True), \
                    mock.patch("sys.stdout"):
                code = jobs.main(["--state-dir", state, "start", "--title", "probe",
                                  "--no-preflight", "--"] + self.FAILING_ARGV)
            self.assertEqual(code, 0)
            self.assertEqual(len(jobs.list_jobs(state)), 1)


class ConcurrencyCapTests(unittest.TestCase):
    """The concurrency caps — **3 soft per warm session, 5 hard globally**, as a gate that can
    actually refuse rather than a sentence in a prompt.

    The soft 3 keeps headroom for the owner's desktop sessions; the hard 5 is the ceiling. At 5, an
    urgent job gets a ranked list of the running jobs to cancel and replace — the owner decides.

    The fixture is the shape that motivated it: five jobs concurrent from ONE warm session.

    Three things are pinned here and each is a separate failure if it goes:
      * the two caps are **separate predicates** over different populations (per-origin vs global);
      * the soft cap **yields to one named flag** and the hard cap yields to nothing;
      * the refusal at 5 is **ranked and actionable**, because a refusal that only says no hands the
        reconstruction work back to the owner, which is the cost the whole feature exists to
        remove."""

    SESSION = "00000000-0000-0000-0000-00000000a002"
    OTHER = "00000000-0000-0000-0000-00000000a003"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(jobs.jobs_dir(self.state), exist_ok=True)
        # Every count in this class runs against records whose PID is a stub, so the liveness probe
        # is pinned rather than left to whatever pid 4321 happens to be on the box running CI.
        alive = mock.patch.object(jobs, "pid_alive", return_value=True)
        alive.start()
        self.addCleanup(alive.stop)

    # ------------------------------------------------------------------ fixtures

    def _rec(self, jid, *, session=None, status=None, by="owner", reason=None, wake=False,
             lease=False, age_sec=60, title=None, goal=None, pid=4321):
        """One on-disk record, written the way `start_job` writes one. Ages are expressed as seconds
        BEFORE `NOW`, so the youngest-first tiebreak is stated in the fixture rather than inferred."""
        started = NOW - timedelta(seconds=age_sec)
        origin = {}
        if session:
            origin["session_id"] = session
            origin["stamped_by"] = "env"
        if goal:
            origin["goal"] = goal
        if by:
            # `by=None` writes NO request block (every pre-phase-5 record); `by="unresolved"` writes the literal floor `normalize_request` produces when
            # nothing resolves. Both must land in the same rank band, and they reach it by different
            # routes — the dict's `"unresolved"` entry and the dict's DEFAULT — so both are fixtures.
            req = {"by": by, "resolved_by": "none" if by == "unresolved" else "turn-pointer"}
            if by != "unresolved":
                req["turn_id"] = "t" + jid[-4:]
            if by == "unresolved":
                req["why"] = "no turn pointer was open"
            if by == "assistant" and reason:
                req["reason_class"] = reason
            origin["request"] = req
        rec = {"schema": jobs.SCHEMA, "id": jid, "title": title or f"job {jid}",
               "argv": ["python", "-c", "pass"], "cwd": self.state,
               "log_path": jobs.log_path(self.state, jid),
               "status": status or jobs.RUNNING, "pid": pid, "child_pid": None,
               "created_at": jobs._stamp(started), "started_at": jobs._stamp(started),
               "ended_at": None, "exit_code": None, "deadline_sec": jobs.DEFAULT_DEADLINE_SEC,
               "lease": lease, "wake": wake, "notify": {"channel": "telegram"},
               "notified_at": None, "origin": jobs.normalize_origin(origin), "attempts": []}
        jobs.save_job(self.state, rec)
        return rec

    def _fill(self, n, *, session=None, prefix="f", **kw):
        return [self._rec(f"2026090{i}-000000-{prefix}{i:03d}", session=session,
                          age_sec=60 * (i + 1), **kw) for i in range(n)]

    # ------------------------------------------------------------------ the two predicates, apart

    def test_hard_cap_is_global_and_refuses_only_at_five(self):
        """The 5 counts **every** origin. Four is fine; the fifth start is the one that is refused,
        so the cap admits exactly five in flight and no more."""
        self._fill(4, session=self.SESSION)
        self.assertIsNone(jobs.hard_cap_refusal(self.state, NOW))
        self._rec("20260901-000000-fifth", session=self.OTHER)
        why = jobs.hard_cap_refusal(self.state, NOW)
        self.assertIsNotNone(why)
        self.assertIn("hard cap is 5", why)

    def test_hard_cap_counts_across_sessions_not_within_one(self):
        """Three from one session and two from another is FIVE — the global cap has nothing to do with
        whose session the work came out of. This is the half that a per-session-only count would
        miss — two sessions at 3+3 is six in flight."""
        self._fill(3, session=self.SESSION, prefix="a")
        self.assertIsNone(jobs.hard_cap_refusal(self.state, NOW))
        self._fill(2, session=self.OTHER, prefix="b")
        self.assertIsNotNone(jobs.hard_cap_refusal(self.state, NOW))

    def test_soft_cap_is_per_session_and_refuses_only_at_three(self):
        self._fill(2, session=self.SESSION)
        self.assertIsNone(jobs.soft_cap_refusal(self.state, self.SESSION, NOW))
        self._rec("20260901-000000-third", session=self.SESSION)
        why = jobs.soft_cap_refusal(self.state, self.SESSION, NOW)
        self.assertIsNotNone(why)
        self.assertIn("soft cap is 3", why)

    def test_another_sessions_jobs_never_trip_my_soft_cap(self):
        """The 3 reserves headroom for the owner's desktop sessions, so it is scoped by `origin.session_id` —
        the field that already distinguishes the two caps. Four jobs from someone else's session leave
        mine at zero."""
        self._fill(4, session=self.OTHER)
        self.assertIsNone(jobs.soft_cap_refusal(self.state, self.SESSION, NOW))
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["session"], 0)
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["global"], 4)

    def test_an_origin_less_job_counts_globally_and_against_no_session(self):
        """A cron shim, a hand-run command and an `--analyze` job (origin-less in code) are all real
        concurrency and none of them is competing for a desktop slot."""
        self._fill(3, session=None, by=None)
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["global"], 3)
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["session"], 0)
        self.assertIsNone(jobs.soft_cap_refusal(self.state, self.SESSION, NOW))

    def test_a_caller_with_no_session_id_is_never_held_by_the_soft_cap(self):
        self._fill(4, session=None, by=None)
        self.assertIsNone(jobs.soft_cap_refusal(self.state, None, NOW))
        self.assertIsNone(jobs.soft_cap_refusal(self.state, "", NOW))
        # Jobs that DO carry a session must not hold an unaddressed caller either.
        self._fill(4, session=self.SESSION, prefix="s")
        self.assertIsNone(jobs.soft_cap_refusal(self.state, None, NOW))

    def test_concurrency_zeroes_the_session_count_for_a_caller_with_no_session_id(self):
        """**Where the "never held" property actually lives.** `soft_cap_refusal`'s early return and
        this ternary's `else 0` mutually mask: with both in place, deleting either alone changes no
        answer over any (population, caller) pair, so neither is killable through the refusal. Asserted on `concurrency` directly, which is the line that decides it — the refusal's
        guard is readability on top."""
        self._fill(4, session=self.SESSION)
        self._fill(2, session=None, by=None, prefix="n")
        for caller in (None, "", "   "):
            counts = jobs.concurrency(self.state, caller, NOW)
            self.assertEqual(counts["session"], 0, caller)
            self.assertEqual(counts["global"], 6, caller)

    # ------------------------------------------------------------------ what counts as in flight

    def test_a_stale_running_record_whose_pid_is_gone_does_not_count(self):
        """**The gate reads liveness, not the status field.** `list_jobs(active_only=True)` reads
        `status`, which the daemon's reconcile writes — so a job whose shim died at 03:00 still reads
        `running` on disk until a tick notices. Counting those would let a dead job refuse live work
        for reasons its owner cannot see, which is how a cap gets deleted rather than obeyed."""
        self._fill(5, session=self.SESSION)
        self.assertIsNotNone(jobs.hard_cap_refusal(self.state, NOW))
        with mock.patch.object(jobs, "pid_alive", return_value=False):
            self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["global"], 0)
            self.assertIsNone(jobs.hard_cap_refusal(self.state, NOW))

    def test_a_running_record_past_its_deadline_does_not_count(self):
        self._fill(4, session=self.SESSION)
        self._rec("20260901-000000-old", session=self.SESSION,
                  age_sec=jobs.DEFAULT_DEADLINE_SEC + 60)
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["global"], 4)
        self.assertIsNone(jobs.hard_cap_refusal(self.state, NOW))

    def test_a_live_retry_pending_counts_and_an_expired_one_does_not(self):
        """`retry-pending` is ACTIVE, not finished (§7) — a job in its backoff is still in flight and
        still owns a slot. One whose window has closed is `failed` at the next reconcile and owns
        nothing."""
        self._fill(4, session=self.SESSION)
        pending = self._rec("20260901-000000-rtry", session=self.SESSION,
                            status=jobs.RETRY_PENDING, age_sec=30)
        pending["retry"] = jobs.retry_config(2, window_sec=3600)
        pending["attempt"] = 2
        pending["next_attempt_at"] = jobs._stamp(NOW + timedelta(seconds=30))
        jobs.save_job(self.state, pending)
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["global"], 5)
        self.assertIsNotNone(jobs.hard_cap_refusal(self.state, NOW))

        pending["retry"] = jobs.retry_config(2, window_sec=1)
        jobs.save_job(self.state, pending)
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["global"], 4)
        self.assertIsNone(jobs.hard_cap_refusal(self.state, NOW))

    def test_a_terminal_job_never_counts(self):
        for i, status in enumerate(jobs.TERMINAL):
            rec = self._rec(f"20260901-000000-t{i:03d}", session=self.SESSION, status=status)
            rec["ended_at"] = jobs._stamp(NOW)
            jobs.save_job(self.state, rec)
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["global"], 0)

    def test_counting_writes_nothing(self):
        """`start` must not transition another job's record as a side effect of counting itself in.
        `check_terminal` / `check_retry_due` are both side-effect-free and are used as verdicts only."""
        self._fill(3, session=self.SESSION)
        before = {p: os.stat(os.path.join(jobs.jobs_dir(self.state), p)).st_size
                  for p in os.listdir(jobs.jobs_dir(self.state))}
        with mock.patch.object(jobs, "pid_alive", return_value=False):
            jobs.concurrency(self.state, self.SESSION, NOW)
            jobs.hard_cap_refusal(self.state, NOW)
        after = {p: os.stat(os.path.join(jobs.jobs_dir(self.state), p)).st_size
                 for p in os.listdir(jobs.jobs_dir(self.state))}
        self.assertEqual(before, after)
        for rec in jobs.list_jobs(self.state):
            self.assertEqual(rec["status"], jobs.RUNNING)

    # ------------------------------------------------------------------ the ranking

    def _ranked_ids(self, recs):
        return [r["id"] for r in jobs.rank_kill_order(recs, NOW)]

    def test_retry_pending_outranks_everything_because_nothing_is_running(self):
        """Key 1. A job in its backoff has no child process at all, so cancelling it discards no
        in-flight work — the cheapest thing to take, on evidence rather than taste. It outranks even a
        `floated-idea` violation, which is the next-strongest signal there is."""
        violation = self._rec("20260901-000000-viol", session=self.SESSION, by="assistant",
                              reason=jobs.REQUEST_REASON_VIOLATION, age_sec=30)
        pending = self._rec("20260901-000000-pend", session=self.SESSION, by="owner",
                            status=jobs.RETRY_PENDING, age_sec=9000)
        self.assertEqual(self._ranked_ids([violation, pending]),
                         [pending["id"], violation["id"]])

    def test_a_floated_idea_violation_outranks_every_ordinary_job(self):
        """Key 2. `floated-idea` is the module's own word for a job that should not exist — the
        owner was thinking out loud and it became a brief. Taking it back costs nothing anyone asked for."""
        mine = self._rec("20260901-000000-asst", session=self.SESSION, by="assistant",
                         reason="spec-phase", age_sec=30)
        violation = self._rec("20260901-000000-viol", session=self.SESSION, by="assistant",
                              reason=jobs.REQUEST_REASON_VIOLATION, age_sec=9000)
        self.assertEqual(self._ranked_ids([mine, violation]), [violation["id"], mine["id"]])

    def test_who_asked_orders_assistant_then_unresolved_then_owner(self):
        """Key 3, and the honest middle. Rung 3 is a POSITIVE claim that the assistant started it
        itself, so nobody is waiting on the answer. `unresolved` sits between the two because it
        MIGHT be the owner's — safer to take than one known to be the assistant's, less safe than one
        known to be the owner's."""
        owners = self._rec("20260901-000000-ownr", session=self.SESSION, by="owner", age_sec=60)
        mine = self._rec("20260901-000000-asst", session=self.SESSION, by="assistant", age_sec=60)
        unk = self._rec("20260901-000000-unkn", session=self.SESSION, by=None, age_sec=60)
        self.assertEqual(self._ranked_ids([owners, unk, mine]),
                         [mine["id"], unk["id"], owners["id"]])
        # **Both spellings of "unresolved", because they take different routes to the same band.** A
        # record with no `request` block at all (anything predating phase 5)
        # arrives via the lookup's DEFAULT; a record `normalize_request` could not resolve carries the
        # literal `"unresolved"` and arrives via the entry. Testing only the first left the entry
        # unexercised, which a mutation run caught.
        floor = self._rec("20260901-000000-flor", session=self.SESSION, by="unresolved", age_sec=60)
        self.assertEqual(jobs.job_request(floor)["by"], "unresolved")
        self.assertEqual(self._ranked_ids([owners, floor, mine]),
                         [mine["id"], floor["id"], owners["id"]])
        self.assertEqual(jobs.kill_rank(floor, NOW), jobs.kill_rank(unk, NOW))

    def test_a_waited_on_job_sinks_but_only_inside_its_requester_band(self):
        """Key 4, and its position is the argument. `--wake`'s own contract is that the completion push
        fires either way, so cancelling a waited-on job costs the re-entry and never the report — which
        makes "who asked" the stronger question and "who is listening" the tiebreak underneath it. So an
        `assistant` job with a waiter still outranks an `owner` job with none."""
        assistant_waited = self._rec("20260901-000000-aswk", session=self.SESSION, by="assistant",
                                     wake=True, age_sec=60)
        owner_quiet = self._rec("20260901-000000-owqt", session=self.SESSION, by="owner",
                                age_sec=60)
        self.assertEqual(self._ranked_ids([owner_quiet, assistant_waited]),
                         [assistant_waited["id"], owner_quiet["id"]])

        quiet = self._rec("20260901-000000-asqt", session=self.SESSION, by="assistant", age_sec=60)
        leased = self._rec("20260901-000000-asls", session=self.SESSION, by="assistant",
                           lease=True, age_sec=60)
        self.assertEqual(self._ranked_ids([leased, quiet]), [quiet["id"], leased["id"]])

    def test_youngest_first_breaks_the_last_tie(self):
        """Key 5, and it is LAST on purpose: least work discarded is the right tiebreak and the wrong
        headline. A job three hours in has three hours to lose."""
        old = self._rec("20260901-000000-oldy", session=self.SESSION, age_sec=3 * 3600)
        mid = self._rec("20260901-000000-midy", session=self.SESSION, age_sec=600)
        young = self._rec("20260901-000000-yung", session=self.SESSION, age_sec=30)
        self.assertEqual(self._ranked_ids([old, young, mid]),
                         [young["id"], mid["id"], old["id"]])

    def test_the_ranking_never_reorders_on_title_or_goal(self):
        """Titles and goals are PRINTED, never ranked on: a keyword list deciding that `fix` outranks
        `feat` is an invented score, and inventing one is the thing this ranking refuses to do."""
        a = self._rec("20260901-000000-aaaa", session=self.SESSION, age_sec=60,
                      title="fix(critical): production is on fire", goal="urgent, drop everything")
        b = self._rec("20260901-000000-bbbb", session=self.SESSION, age_sec=61,
                      title="chore: tidy a comment", goal="cosmetic")
        self.assertEqual(self._ranked_ids([a, b]), [a["id"], b["id"]])   # age alone decided
        a["title"], a["goal"] = b["title"], b.get("goal")
        self.assertEqual(self._ranked_ids([a, b]), [a["id"], b["id"]])   # and still does

    def test_kill_signals_name_every_key_that_fired(self):
        rec = self._rec("20260901-000000-sigs", session=self.SESSION, by="assistant",
                        reason=jobs.REQUEST_REASON_VIOLATION, status=jobs.RETRY_PENDING,
                        wake=True, lease=True, age_sec=125)
        signals = " | ".join(jobs.kill_signals(rec, NOW))
        self.assertIn("nothing is running", signals)
        self.assertIn(jobs.REQUEST_REASON_VIOLATION, signals)
        self.assertIn("the assistant started this itself", signals)
        self.assertIn("--wake + --lease", signals)
        self.assertIn("2m05s", signals)

    def test_kill_signals_always_answer_who_asked_even_when_unresolved(self):
        """An absent attribution is a fact the owner would weigh, so the row says so rather than going
        quiet — `A WRONG ATTRIBUTION IS WORSE THAN NO ATTRIBUTION`, and a silent one is worse still."""
        rec = self._rec("20260901-000000-nreq", session=self.SESSION, by=None)
        self.assertIn("unresolved", " | ".join(jobs.kill_signals(rec, NOW)))

    # ------------------------------------------------------------------ the refusal, as the owner reads it

    def test_the_hard_refusal_lists_every_running_job_ranked_with_a_cancel_line(self):
        """The ask: present the running jobs in order of recommendation to kill and replace. So: all
        five, each with the signals that placed it and the one command that acts on it. A refusal the
        owner has to translate into a job id themselves is the same cost in a smaller font."""
        recs = self._fill(5, session=self.SESSION)
        why = jobs.hard_cap_refusal(self.state, NOW)
        for rec in recs:
            self.assertIn(rec["id"], why)
            self.assertIn(f"cancel: python jobs.py cancel {rec['id']}", why)
        self.assertEqual(len(re.findall(r"^  \d+\. ", why, re.M)), 5)
        # The provenance line: the owner can audit the order rather than take it on trust.
        self.assertIn("Ranked on:", why)
        for signal in ("retry-pending", "reason_class", "origin.request.by", "--wake/--lease", "age"):
            self.assertIn(signal, why)
        self.assertIn("never ranked on", why)
        self.assertIn("no record was written", why)

    def test_the_hard_refusal_rows_are_in_rank_order(self):
        recs = [self._rec("20260901-000000-r1ow", session=self.SESSION, by="owner", age_sec=60),
                self._rec("20260901-000000-r2as", session=self.SESSION, by="assistant", age_sec=60),
                self._rec("20260901-000000-r3vi", session=self.SESSION, by="assistant",
                          reason=jobs.REQUEST_REASON_VIOLATION, age_sec=60),
                self._rec("20260901-000000-r4pd", session=self.SESSION,
                          status=jobs.RETRY_PENDING, age_sec=60),
                self._rec("20260901-000000-r5un", session=self.SESSION, by=None, age_sec=60)]
        why = jobs.hard_cap_refusal(self.state, NOW)
        order = [m for m in re.findall(r"^  \d+\. (\S+)", why, re.M)]
        self.assertEqual(order, self._ranked_ids(recs))
        self.assertEqual(order[0], "20260901-000000-r4pd")   # retry-pending first
        self.assertEqual(order[-1], "20260901-000000-r1ow")  # the owner's, untouched, last

    def test_the_soft_refusal_names_the_flag_and_shows_only_this_session(self):
        """The soft refusal is a headroom message, not an alarm: it says whose concurrency is being
        spent, names the one flag that gets past it, and ranks only THIS session's jobs — the other
        session's are not this one's to trade."""
        mine = self._fill(3, session=self.SESSION, prefix="m")
        theirs = self._fill(1, session=self.OTHER, prefix="o")
        why = jobs.soft_cap_refusal(self.state, self.SESSION, NOW)
        self.assertIn("--over-soft-cap", why)
        self.assertIn("HEADROOM, NOT MACHINE LOAD", why)
        self.assertIn("Global: 4 of 5", why)
        for rec in mine:
            self.assertIn(rec["id"], why)
        self.assertNotIn(theirs[0]["id"], why)

    def test_a_refusal_never_raises_on_a_malformed_record(self):
        """The ranking runs inside a refusal, so one garbled record must not cost the whole list."""
        self._fill(4, session=self.SESSION)
        broken = {"schema": jobs.SCHEMA, "id": "20260901-000000-brok", "status": jobs.RUNNING,
                  "pid": 4321, "origin": "not-a-dict", "started_at": "not-a-date",
                  "title": None, "wake": None}
        jobs.save_json(jobs.job_path(self.state, broken["id"]), broken)
        why = jobs.hard_cap_refusal(self.state, NOW)
        self.assertIsNotNone(why)
        self.assertIn("20260901-000000-brok", why)

    # ------------------------------------------------------------------ the CLI, end to end

    def _cli(self, *extra, session=None):
        real_start = jobs.start_job

        def stubbed(*a, **kw):
            kw["runner"] = _runner()
            return real_start(*a, **kw)

        env = {jobs.ORIGIN_ENV_VAR: session} if session else {}
        out, err = io.StringIO(), io.StringIO()
        # The CLI path takes no `now`, and the fixtures are aged against NOW — without a frozen clock
        # every one of them reads as hours past its 6 h deadline and the caps see an empty tree.
        with mock.patch.object(jobs, "start_job", stubbed), \
                mock.patch.object(jobs, "_utc_now", return_value=NOW), \
                mock.patch.dict(os.environ, env, clear=not session), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = jobs.main(["--state-dir", self.state, "start", "--title", "new",
                              *extra, "--", "git", "status"])
        return code, out.getvalue(), err.getvalue()

    def test_the_cli_refuses_at_the_hard_cap_and_writes_no_record(self):
        """Exit 2 and **nothing on disk** — `preflight_refusal`'s posture, for its reason: a refused
        job never started, so it must not read as failed."""
        self._fill(5, session=self.SESSION)
        code, _, err = self._cli(session=self.SESSION)
        self.assertEqual(code, 2)
        self.assertIn("refusing to start:", err)
        self.assertIn("hard cap is 5", err)
        self.assertEqual(len(jobs.list_jobs(self.state)), 5)

    def test_the_cli_refuses_at_the_soft_cap_and_writes_no_record(self):
        self._fill(3, session=self.SESSION)
        code, _, err = self._cli(session=self.SESSION)
        self.assertEqual(code, 2)
        self.assertIn("soft cap is 3", err)
        self.assertEqual(len(jobs.list_jobs(self.state)), 3)

    def test_over_soft_cap_gets_past_the_three_and_stamps_the_overrun(self):
        """A fourth job can be the RIGHT call (an urgent data-loss fix, say), so the soft cap has to
        yield to judgement. One flag, and the overrun is recorded, so how
        often it is used is a measurement rather than a memory."""
        self._fill(3, session=self.SESSION)
        code, out, _ = self._cli("--over-soft-cap", session=self.SESSION)
        self.assertEqual(code, 0)
        self.assertEqual(len(jobs.list_jobs(self.state)), 4)
        note = json.loads(out)["over_soft_cap"]
        self.assertEqual(note["cap"], jobs.SOFT_CAP_PER_ORIGIN)
        self.assertEqual(note["session_active"], 3)
        self.assertEqual(note["global_active"], 3)
        self.assertTrue(note["at"].endswith("Z"))

    def test_over_soft_cap_below_the_cap_records_nothing(self):
        """Absent rather than false, the same posture as `retry`/`analyze`/`worktree`: a job started at
        one-of-three overrode nothing, and a record must never imply a policy it doesn't have. That is
        what keeps the field countable — every one on disk is a real overrun."""
        self._fill(1, session=self.SESSION)
        code, out, _ = self._cli("--over-soft-cap", session=self.SESSION)
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)["over_soft_cap"])
        started = [r for r in jobs.list_jobs(self.state) if r["title"] == "new"]
        self.assertNotIn("over_soft_cap", started[0])

    def test_over_soft_cap_does_not_get_past_the_hard_cap(self):
        """The one thing that makes the 5 a different cap from the 3. Five in flight from two sessions,
        only two of them mine — the soft cap is nowhere near — and it still refuses."""
        self._fill(2, session=self.SESSION, prefix="m")
        self._fill(3, session=self.OTHER, prefix="o")
        code, _, err = self._cli("--over-soft-cap", session=self.SESSION)
        self.assertEqual(code, 2)
        self.assertIn("hard cap is 5", err)
        self.assertIn("no flag for this one", err)

    def test_the_hard_refusal_wins_when_both_caps_are_hit(self):
        """The owner gets the one with no way out, not the one that could have been flagged past."""
        self._fill(5, session=self.SESSION)
        code, _, err = self._cli(session=self.SESSION)
        self.assertEqual(code, 2)
        self.assertIn("hard cap is 5", err)
        self.assertNotIn("soft cap is 3", err)

    def test_an_ordinary_job_still_starts_under_both_caps(self):
        """The guard that quietly blocks ordinary work is the worse bug — pinned as hard as the
        refusals are."""
        self._fill(2, session=self.SESSION)
        code, out, _ = self._cli(session=self.SESSION)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], jobs.RUNNING)
        self.assertEqual(len(jobs.list_jobs(self.state)), 3)

    def test_the_preflight_is_answered_before_the_caps(self):
        """A mangled argv is wrong whatever the load is, and telling the owner to cancel a live job to make
        room for one that would die in a second is worse advice than none. So the broken command is
        reported first even at five in flight."""
        self._fill(5, session=self.SESSION)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {jobs.ORIGIN_ENV_VAR: self.SESSION}), \
                mock.patch.object(jobs, "_utc_now", return_value=NOW), \
                contextlib.redirect_stderr(err):
            code = jobs.main(["--state-dir", self.state, "start", "--title", "new", "--",
                              "pwsh", "-NoProfile", "-File",
                              "C:UsersuserAppDataLocalTempseneschal-scratchrun.ps1"])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err.getvalue())
        self.assertNotIn("hard cap", err.getvalue())

    # ------------------------------------------------------------------ what the caps must NOT block

    def test_start_job_itself_is_not_capped(self):
        """**The caps live at the CLI, not in `start_job`** — `preflight_refusal`'s placement and its
        reason. `request_analysis` and the retry respawn both build their argv in code and neither may
        be blockable: an analysis eaten by a cap is §3.11's failure exactly, the fresh eyes a job asked
        for, silently cancelled."""
        self._fill(5, session=self.SESSION)
        # The clock is frozen, not merely passed as `now=`: a cap check added inside `start_job` would
        # read `_utc_now()` and see five fixtures long past their deadline, so the test would pass for
        # the wrong reason and could never catch the thing it exists to forbid.
        with mock.patch.object(jobs, "_utc_now", return_value=NOW):
            self.assertIsNotNone(jobs.hard_cap_refusal(self.state))   # the cap IS being hit
            rec = jobs.start_job(self.state, "in-code", ["python", "-c", "pass"],
                                 runner=_runner(), now=NOW)
        self.assertEqual(rec["status"], jobs.RUNNING)

    def test_an_analysis_still_spawns_at_the_hard_cap(self):
        self._fill(5, session=self.SESSION)
        failed = self._rec("20260901-000000-fail", session=self.SESSION, status=jobs.FAILED,
                           goal="what the work was for")
        failed["ended_at"] = jobs._stamp(NOW)
        jobs.save_job(self.state, failed)
        with mock.patch.object(jobs, "_utc_now", return_value=NOW):
            self.assertIsNotNone(jobs.hard_cap_refusal(self.state))   # the cap IS being hit
            sub = jobs.request_analysis(self.state, failed["id"], runner=_runner(), now=NOW)
        self.assertEqual(sub["status"], jobs.RUNNING)
        self.assertEqual(sub["analysis_for"], failed["id"])

    def test_the_flag_is_documented_in_the_start_help(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            jobs.main(["start", "--help"])
        text = buf.getvalue()
        self.assertIn("--over-soft-cap", text)
        self.assertIn("HEADROOM", text)


class AgentOnlyCapTests(unittest.TestCase):
    """Only jobs that make agent calls (local, subscription, or API) count against the job cap —
    the cap exists to meter model spend and headroom, not CPU.

    Pinned: a non-agent job counts toward NEITHER cap and is not held by them; a `claude` job counts;
    the explicit `--agent`/`--no-agent` stamp beats the heuristic both ways; an unrecognised shape counts
    (fail closed); the refusal ranks counted jobs only and says how many are running uncounted."""

    SESSION = ConcurrencyCapTests.SESSION
    CLAUDE = ["claude", "--session-id", "00000000-0000-0000-0000-00000000a004", "-p",
              "--permission-mode", "bypassPermissions", "do the thing"]
    CLAUDE_EXE = [r"C:\Users\user\.local\bin\claude.exe", "-p", "do the thing"]
    BACKUP_RUN = [r"C:\Users\user\workspace\repos\seneschal\.venv\Scripts\python.exe",
                 r"C:\Users\user\workspace\repos\seneschal\seneschal\scripts\state_backup.py", "run"]
    GRADLE = [r"C:\Users\user\workspace\repos\seneschal\phone\android\gradlew.bat",
              ":app:installDebug", "--no-daemon", "-q"]
    WSL_TRANSCRIBE = ["wsl.exe", "-d", "Ubuntu-24.04", "bash",
                      "/mnt/c/Users/user/workspace/repos/seneschal/seneschal/state/transcribe/live/run_x"]

    setUp = ConcurrencyCapTests.setUp

    def _rec(self, jid, *, argv, agent=None, session=None, **kw):
        rec = ConcurrencyCapTests._rec(self, jid, session=session or self.SESSION, **kw)
        rec["argv"] = list(argv)
        if agent is not None:
            rec["agent"] = agent
        jobs.save_job(self.state, rec)
        return rec

    def _fill(self, n, *, argv, prefix, agent=None, session=None):
        return [self._rec(f"2026090{i}-000000-{prefix}{i:03d}", argv=argv, agent=agent,
                          session=session, age_sec=60 * (i + 1)) for i in range(n)]

    # ------------------------------------------------------------------ the classifier

    def test_claude_is_an_agent_job_by_either_spelling(self):
        for argv in (self.CLAUDE, self.CLAUDE_EXE):
            self.assertEqual(jobs.job_agent_class({"argv": argv})[0], jobs.AGENT, argv)
            self.assertTrue(jobs.counts_toward_cap({"argv": argv}))

    def test_known_model_calling_scripts_are_agent_jobs(self):
        for script in ("fable_delegate.py", "job_analysis.py", "router.py"):
            rec = {"argv": ["python", f"seneschal/scripts/{script}", "run"]}
            self.assertEqual(jobs.job_agent_class(rec)[0], jobs.AGENT, script)

    def test_known_pure_compute_shapes_are_non_agent(self):
        for argv in (self.BACKUP_RUN, self.GRADLE, ["python", "-m", "unittest", "discover"],
                     ["uv", "run", "python", "seneschal/scripts/state_backup.py", "run"],
                     ["ffmpeg", "-i", "a.mp4", "a.wav"], ["yt-dlp", "https://example.com/v"]):
            self.assertEqual(jobs.job_agent_class({"argv": argv})[0], jobs.NON_AGENT, argv)
            self.assertFalse(jobs.counts_toward_cap({"argv": argv}))

    def test_an_unrecognised_shape_is_unknown_and_counts(self):
        """Fail closed: a wrapper hides what it runs, so an opaque job is never cleared as free."""
        for argv in (self.WSL_TRANSCRIBE, ["pwsh", "-NoProfile", "-File", r"C:\tmp\x.ps1"],
                     ["cmd.exe", "/c", r"C:\tmp\run.cmd"], ["python", "-c", "pass"],
                     ["python", r"C:\tmp\mystery.py"], ["git", "status"]):
            self.assertEqual(jobs.job_agent_class({"argv": argv})[0], jobs.AGENT_UNKNOWN, argv)
            self.assertTrue(jobs.counts_toward_cap({"argv": argv}))
        for broken in ({}, {"argv": []}, {"argv": "claude"}, "not-a-dict", None):
            self.assertEqual(jobs.job_agent_class(broken)[0], jobs.AGENT_UNKNOWN, broken)
            self.assertTrue(jobs.counts_toward_cap(broken))

    def test_a_path_under_a_claude_directory_is_not_agent_evidence(self):
        """Basenames only: a pure-compute script inside `.claude/worktrees/` stays non-agent."""
        argv = ["python", r"C:\repo\.claude\worktrees\x\seneschal\scripts\state_backup.py", "run"]
        self.assertEqual(jobs.job_agent_class({"argv": argv})[0], jobs.NON_AGENT)

    def test_the_explicit_stamp_overrides_the_heuristic_both_ways(self):
        self.assertEqual(jobs.job_agent_class({"argv": self.CLAUDE, "agent": False})[0],
                         jobs.NON_AGENT)
        self.assertEqual(jobs.job_agent_class({"argv": self.GRADLE, "agent": True})[0], jobs.AGENT)
        self.assertEqual(jobs.job_agent_class({"argv": self.WSL_TRANSCRIBE, "agent": False})[0],
                         jobs.NON_AGENT)

    # ------------------------------------------------------------------ the counts

    def test_non_agent_jobs_count_toward_neither_cap(self):
        self._fill(6, argv=self.BACKUP_RUN, prefix="bak")
        counts = jobs.concurrency(self.state, self.SESSION, NOW)
        self.assertEqual((counts["global"], counts["session"]), (0, 0))
        self.assertEqual(len(counts["uncounted"]), 6)
        self.assertIsNone(jobs.hard_cap_refusal(self.state, NOW))
        self.assertIsNone(jobs.soft_cap_refusal(self.state, self.SESSION, NOW))

    def test_claude_jobs_count_toward_both_caps(self):
        self._fill(3, argv=self.CLAUDE, prefix="cla")
        self.assertIsNotNone(jobs.soft_cap_refusal(self.state, self.SESSION, NOW))
        self._fill(2, argv=self.CLAUDE_EXE, prefix="exe")
        self.assertIsNotNone(jobs.hard_cap_refusal(self.state, NOW))

    def test_the_stamp_moves_a_job_in_and_out_of_the_count(self):
        self._fill(5, argv=self.CLAUDE, prefix="nop", agent=False)
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["global"], 0)
        self._fill(5, argv=self.GRADLE, prefix="yes", agent=True)
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["global"], 5)
        self.assertIsNotNone(jobs.hard_cap_refusal(self.state, NOW))

    def test_unknown_shapes_count(self):
        self._fill(5, argv=self.WSL_TRANSCRIBE, prefix="wsl")
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["global"], 5)
        self.assertIsNotNone(jobs.hard_cap_refusal(self.state, NOW))

    def test_the_refusal_lists_only_counted_jobs_and_names_the_uncounted(self):
        agents = self._fill(5, argv=self.CLAUDE, prefix="cla")
        others = self._fill(2, argv=self.BACKUP_RUN, prefix="bak")
        why = jobs.hard_cap_refusal(self.state, NOW)
        self.assertIn("5 agent jobs", why)
        for rec in agents:
            self.assertIn(rec["id"], why)
        for rec in others:
            self.assertNotIn(rec["id"], why)
        self.assertEqual(len(re.findall(r"^  \d+\. ", why, re.M)), 5)
        self.assertIn("NOT counted", why)
        self.assertIn("2 non-agent jobs", why)

    def test_the_refusal_is_silent_about_uncounted_jobs_when_there_are_none(self):
        self._fill(5, argv=self.CLAUDE, prefix="cla")
        self.assertNotIn("NOT counted", jobs.hard_cap_refusal(self.state, NOW))

    # ------------------------------------------------------------------ the CLI

    def _cli(self, *extra, argv):
        real_start = jobs.start_job

        def stubbed(*a, **kw):
            kw["runner"] = _runner()
            return real_start(*a, **kw)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(jobs, "start_job", stubbed), \
                mock.patch.object(jobs, "_utc_now", return_value=NOW), \
                mock.patch.dict(os.environ, {jobs.ORIGIN_ENV_VAR: self.SESSION}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = jobs.main(["--state-dir", self.state, "start", "--title", "new", "--no-preflight",
                              *extra, "--", *argv])
        return code, out.getvalue(), err.getvalue()

    def _new(self):
        return [r for r in jobs.list_jobs(self.state) if r["title"] == "new"]

    def test_a_non_agent_job_starts_at_the_hard_cap(self):
        self._fill(5, argv=self.CLAUDE, prefix="cla")
        code, out, err = self._cli(argv=self.GRADLE)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["agent_class"], jobs.NON_AGENT)
        self.assertIsNone(json.loads(out)["over_soft_cap"])

    def test_a_claude_job_is_refused_at_the_hard_cap(self):
        self._fill(5, argv=self.CLAUDE, prefix="cla")
        code, _, err = self._cli(argv=self.CLAUDE)
        self.assertEqual(code, 2)
        self.assertIn("hard cap is 5", err)
        self.assertEqual(self._new(), [])

    def test_no_agent_on_the_cli_stamps_the_record_and_skips_the_caps(self):
        self._fill(5, argv=self.CLAUDE, prefix="cla")
        code, _, err = self._cli("--no-agent", argv=self.WSL_TRANSCRIBE)
        self.assertEqual(code, 0, err)
        self.assertIs(self._new()[0]["agent"], False)
        # ...and it stays out of the count once it is on disk.
        self.assertEqual(jobs.concurrency(self.state, self.SESSION, NOW)["global"], 5)

    def test_agent_on_the_cli_stamps_the_record(self):
        code, out, err = self._cli("--agent", argv=self.GRADLE)
        self.assertEqual(code, 0, err)
        self.assertIs(self._new()[0]["agent"], True)
        self.assertEqual(json.loads(out)["agent_class"], jobs.AGENT)

    def test_agent_on_the_cli_is_capped_even_for_a_pure_compute_shape(self):
        self._fill(5, argv=self.CLAUDE, prefix="cla")
        code, _, err = self._cli("--agent", argv=self.GRADLE)
        self.assertEqual(code, 2)
        self.assertIn("hard cap is 5", err)
        self.assertEqual(self._new(), [])

    def test_neither_flag_writes_no_stamp(self):
        code, _, _ = self._cli(argv=self.CLAUDE)
        self.assertEqual(code, 0)
        self.assertNotIn("agent", self._new()[0])

    def test_agent_and_no_agent_together_are_refused(self):
        code, _, err = self._cli("--agent", "--no-agent", argv=self.CLAUDE)
        self.assertEqual(code, 2)
        self.assertIn("contradict", err)
        self.assertEqual(self._new(), [])

    def test_the_flags_are_documented_in_the_start_help(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            jobs.main(["start", "--help"])
        self.assertIn("--no-agent", buf.getvalue())
        self.assertIn("--agent", buf.getvalue())


class _RealGit:
    """The real `job_completion` seam over actual `subprocess.run` — §3.13's DETECT/RESCUE need real
    git behaviour (`test_job_completion.py`'s own docstring has the full argument); this integration
    suite only needs a repo real enough that `is_repo`/`diff --quiet` answer honestly, and reuses that
    same real-subprocess seam rather than inventing a second fake with its own chance to be wrong."""

    def run(self, args, cwd=None, check=False):
        return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


class JobCompletionGuaranteeTests(unittest.TestCase):
    """§3.13, wired into `jobs.reconcile` — `test_job_completion.py` covers DETECT/RESUME-decision/
    RESCUE in isolation; this class is the integration seam: does `reconcile` actually call into them
    at the right point, with the right record mutations, and does `start_job` actually inject a
    session id up front. No real `claude` process runs anywhere here — a resume's respawn is verified
    through the same fake `runner` every other retry/worktree test in this file uses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")
        self.addCleanup(self.tmp.cleanup)
        self.origin = os.path.join(self.tmp.name, "origin.git")
        self.work = os.path.join(self.tmp.name, "work")
        for args in (["git", "init", "-q", "--bare", self.origin],
                    ["git", "clone", "-q", self.origin, self.work]):
            subprocess.run(args, check=True, capture_output=True, text=True)
        for args in (["git", "-C", self.work, "config", "user.email", "test@example.com"],
                    ["git", "-C", self.work, "config", "user.name", "Test"],
                    ["git", "-C", self.work, "commit", "-q", "--allow-empty", "-m", "init"],
                    ["git", "-C", self.work, "branch", "-m", "develop"],
                    ["git", "-C", self.work, "push", "-q", "origin", "HEAD:develop"]):
            subprocess.run(args, check=True, capture_output=True, text=True)
        self.git = _RealGit()

    def _worktree_job(self, *, title="coding job", **kw):
        kw.setdefault("runner", _runner())
        kw.setdefault("now", NOW)
        rec = jobs.start_job(self.state, title, ["claude", "-p", "do the thing"],
                             cwd=self.work, **kw)
        # A real `--worktree` job's record carries a `worktree` block; this fixture uses a plain
        # `--cwd` instead (no real `git worktree add`), so the scope gate needs the same signal
        # supplied directly — `is_scoped_cwd` treats either shape identically by design.
        rec["worktree"] = {"path": self.work, "host": self.work}
        jobs.save_job(self.state, rec)
        # The agent's own first move, per its brief (`job_worktree.create`'s detached HEAD is exactly
        # what a real one gets) — `rescue()` refuses to commit directly on `develop`, so a job fixture
        # that never leaves `develop` would make every rescue test fail for a reason unrelated to what
        # it's checking.
        subprocess.run(["git", "-C", self.work, "checkout", "-q", "-b", f"feature/{rec['id']}"],
                       check=True, capture_output=True, text=True)
        return rec

    def _mark_ended(self, rec, *, status=jobs.DONE, exit_code=0):
        rec.update({"status": status, "exit_code": exit_code, "ended_at": jobs._stamp(NOW)})
        jobs.save_job(self.state, rec)
        return rec

    # ---------------------------------------------------------------- start_job session-id injection

    def test_start_job_injects_a_session_id_for_a_bare_claude_dash_p(self):
        rec = jobs.start_job(self.state, "x", ["claude", "-p", "do it"],
                             runner=_runner(), now=NOW)
        self.assertIn("claude_session_id", rec)
        self.assertIn("--session-id", rec["argv"])
        self.assertIn(rec["claude_session_id"], rec["argv"])

    def test_start_job_does_not_touch_a_non_claude_argv(self):
        rec = jobs.start_job(self.state, "x", ["python", "-c", "pass"], runner=_runner(), now=NOW)
        self.assertNotIn("claude_session_id", rec)
        self.assertEqual(rec["argv"], ["python", "-c", "pass"])

    # ---------------------------------------------------------------- the happy path is unaffected

    def test_a_clean_scoped_job_notifies_normally_with_an_empty_completion_record(self):
        rec = self._worktree_job()
        self._mark_ended(rec)
        notify = _Recorder()
        notified = jobs.reconcile(self.state, notify=notify, now=NOW,
                                  completion_runner=self.git)
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0]["completion"]["reasons"], [])
        self.assertIn("Finished", notify.sent[0][1])

    def test_a_job_in_the_shared_repo_root_is_never_rescued_even_when_dirty(self):
        """The false-positive guard, at the integration seam this time: a job with NO worktree and no
        `--cwd` different from the shared root must never have unrelated dirty state in that shared
        checkout blamed on it. `jobs.REPO_ROOT` is patched to `self.work` for the duration — the point
        being tested is "cwd == the shared root", and the real `REPO_ROOT` is this very checkout,
        which a test must never write into."""
        with mock.patch.object(jobs, "REPO_ROOT", self.work):
            rec = jobs.start_job(self.state, "watcher", ["python", "-c", "pass"],
                                 runner=_runner(), now=NOW)   # no cwd ⇒ defaults to (patched) REPO_ROOT
            self.assertEqual(rec["cwd"], self.work)
            with open(os.path.join(self.work, "unrelated.txt"), "w", encoding="utf-8") as fh:
                fh.write("someone else's mess")
            subprocess.run(["git", "-C", self.work, "add", "unrelated.txt"], check=True,
                           capture_output=True, text=True)
            self._mark_ended(rec)
            notify = _Recorder()
            notified = jobs.reconcile(self.state, notify=notify, now=NOW, completion_runner=self.git)
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0]["status"], jobs.DONE)
        self.assertEqual(notified[0]["completion"]["skipped"], "shared-repo-root")
        self.assertIn("Finished", notify.sent[0][1])

    # ---------------------------------------------------------------- RESCUE

    def test_a_dirty_worktree_job_with_no_session_id_is_rescued_and_reported_failed(self):
        rec = self._worktree_job(title="api client")
        del rec["claude_session_id"]
        with open(os.path.join(self.work, "api_client.py"), "w", encoding="utf-8") as fh:
            fh.write("# 954 lines, never committed\n")
        subprocess.run(["git", "-C", self.work, "add", "api_client.py"], check=True,
                       capture_output=True, text=True)
        self._mark_ended(rec)
        notify = _Recorder()
        notified = jobs.reconcile(self.state, notify=notify, now=NOW, completion_runner=self.git)
        self.assertEqual(len(notified), 1)
        out = notified[0]
        self.assertEqual(out["status"], jobs.FAILED)
        self.assertTrue(out["completion"]["rescue"]["pushed"])
        self.assertEqual(out["completion"]["rescue"]["branch"], f"rescue/{rec['id']}")
        text = notify.sent[0][1]
        self.assertIn("never actually finished", text)
        self.assertIn(f"rescue/{rec['id']}", text)
        self.assertIn("nothing was lost", text)
        # The commit genuinely landed on origin under the rescue name.
        show = subprocess.run(["git", "--git-dir", self.origin, "log", "-1", "--name-only",
                               f"refs/heads/rescue/{rec['id']}"],
                              capture_output=True, text=True)
        self.assertIn("api_client.py", show.stdout)

    # ---------------------------------------------------------------- RESUME

    def test_a_dirty_worktree_job_with_a_session_id_is_resumed_not_notified(self):
        rec = self._worktree_job(title="grounding slice")
        with open(os.path.join(self.work, "notes.md"), "w", encoding="utf-8") as fh:
            fh.write("half-written\n")
        subprocess.run(["git", "-C", self.work, "add", "notes.md"], check=True,
                       capture_output=True, text=True)
        self._mark_ended(rec)
        calls: list = []
        notify = _Recorder()
        notified = jobs.reconcile(self.state, notify=notify, now=NOW,
                                  completion_runner=self.git, runner=_runner(calls=calls))
        self.assertEqual(notified, [])            # not notified THIS pass — it was resumed instead
        self.assertEqual(notify.sent, [])
        self.assertEqual(len(calls), 1)            # the resume respawn, through the real shim spawn
        after = jobs.load_job(self.state, rec["id"])
        self.assertEqual(after["status"], jobs.RUNNING)
        self.assertEqual(len(after["completion"]["resume_attempts"]), 1)
        self.assertEqual(after["original_argv"], ["claude", "--session-id",
                                                  rec["claude_session_id"], "-p", "do the thing"])
        self.assertEqual(after["argv"][:3], ["claude", "--resume", rec["claude_session_id"]])

    def test_resume_is_bounded_and_falls_through_to_rescue(self):
        rec = self._worktree_job(title="stubborn job")
        with open(os.path.join(self.work, "stuck.txt"), "w", encoding="utf-8") as fh:
            fh.write("still not landed\n")
        subprocess.run(["git", "-C", self.work, "add", "stuck.txt"], check=True,
                       capture_output=True, text=True)
        rec["completion"] = {"resume_attempts": [{"at": "x", "argv": []}]
                             * job_completion.MAX_RESUME_ATTEMPTS}
        self._mark_ended(rec)
        notify = _Recorder()
        notified = jobs.reconcile(self.state, notify=notify, now=NOW, completion_runner=self.git)
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0]["status"], jobs.FAILED)
        self.assertTrue(notified[0]["completion"]["rescue"]["pushed"])

    def test_a_resume_carries_the_original_permission_mode_forward(self):
        """The pre-fix resume dropped `--permission-mode
        bypassPermissions`, so the resumed session's `git push` was refused with no human present.
        `_worktree_job` doesn't pass `--permission-mode` itself, so this pins the carry at the
        integration seam directly."""
        rec = jobs.start_job(self.state, "coding job",
                             ["claude", "-p", "--permission-mode", "bypassPermissions", "do it"],
                             cwd=self.work, runner=_runner(), now=NOW)
        rec["worktree"] = {"path": self.work, "host": self.work}
        jobs.save_job(self.state, rec)
        subprocess.run(["git", "-C", self.work, "checkout", "-q", "-b", f"feature/{rec['id']}"],
                       check=True, capture_output=True, text=True)
        with open(os.path.join(self.work, "notes.md"), "w", encoding="utf-8") as fh:
            fh.write("half-written\n")
        subprocess.run(["git", "-C", self.work, "add", "notes.md"], check=True,
                       capture_output=True, text=True)
        self._mark_ended(rec)
        notify = _Recorder()
        jobs.reconcile(self.state, notify=notify, now=NOW, completion_runner=self.git,
                       runner=_runner())
        after = jobs.load_job(self.state, rec["id"])
        self.assertIn("--permission-mode", after["argv"])
        self.assertIn("bypassPermissions", after["argv"])
        self.assertIsNone(jobs.resume_permission_mismatch(after))


class ResumePermissionMismatchTests(unittest.TestCase):
    """`jobs.resume_permission_mismatch` — a one-line visibility check for a FUTURE regression of the
    resume bug (a resume that silently reverted to the CLI's default permission mode). Fed a record shaped by hand, the same way the worktree-leak note above is tested directly
    rather than only through a full `reconcile` pass."""

    def test_no_note_when_the_job_never_resumed(self):
        self.assertIsNone(jobs.resume_permission_mismatch({"id": "j"}))
        self.assertIsNone(jobs.resume_permission_mismatch({"completion": {"resume_attempts": []}}))

    def test_no_note_when_every_resume_carried_the_same_mode(self):
        rec = {
            "original_argv": ["claude", "-p", "--permission-mode", "bypassPermissions", "do it"],
            "completion": {"resume_attempts": [
                {"argv": ["claude", "--resume", "sid", "--permission-mode",
                          "bypassPermissions", "-p", "continue"]},
            ]},
        }
        self.assertIsNone(jobs.resume_permission_mismatch(rec))

    def test_names_the_mismatch_when_a_resume_dropped_the_mode(self):
        """The exact shape a regression of the pre-fix bug would produce: the resumed argv carries no
        `--permission-mode` at all, so it silently reverts to the CLI's default."""
        rec = {
            "original_argv": ["claude", "-p", "--permission-mode", "bypassPermissions", "do it"],
            "completion": {"resume_attempts": [
                {"argv": ["claude", "--resume", "sid", "-p", "continue"]},
            ]},
        }
        note = jobs.resume_permission_mismatch(rec)
        self.assertIsNotNone(note)
        self.assertIn("bypassPermissions", note)
        self.assertIn("default", note)

    def test_falls_back_to_argv_when_original_argv_is_absent(self):
        rec = {
            "argv": ["claude", "-p", "--permission-mode", "acceptEdits", "do it"],
            "completion": {"resume_attempts": [
                {"argv": ["claude", "--resume", "sid", "-p", "continue"]},
            ]},
        }
        note = jobs.resume_permission_mismatch(rec)
        self.assertIn("acceptEdits", note)

    def test_the_note_appears_in_notify_text(self):
        rec = {
            "id": "j1", "title": "coding job", "status": jobs.FAILED, "exit_code": 1,
            "created_at": jobs._stamp(NOW), "ended_at": jobs._stamp(NOW),
            "original_argv": ["claude", "-p", "--permission-mode", "bypassPermissions", "do it"],
            "completion": {"reasons": [], "resume_attempts": [
                {"argv": ["claude", "--resume", "sid", "-p", "continue"]},
            ]},
        }
        self.assertIn("bypassPermissions", jobs.notify_text(rec))


if __name__ == "__main__":
    unittest.main()
