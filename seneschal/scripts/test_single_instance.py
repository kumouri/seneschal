#!/usr/bin/env python3
"""Tests for single_instance.py — the kernel-held lock that decides which presence.py is THE daemon
(and which seneschald_supervise.py is THE supervisor).

What is pinned here:

* exactly one winner when several processes start within milliseconds of each other — real separate
  processes, because that is what the kernel lock is for and an in-process test would not prove it;
* a start that lands DURING a re-exec handoff: the outgoing daemon holds the lock, a successor and a
  revived copy both wait, the holder releases, and exactly one of the two comes up;
* a holder that dies frees the lock with nothing to reap (the property a supervisor's crash restart
  depends on: a killed daemon's successor never waits out a stale heartbeat);
* a loser exits quietly on one line and reaches NONE of presence.py's side effects (run against the
  real presence.py entry point);
* `_respawn_detached` gives the lock back BEFORE it spawns, so the successor can win it;
* the lock file is never deleted; release is idempotent.

Stdlib unittest only. Run: python -m unittest test_single_instance
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import single_instance as si  # noqa: E402

# A child that waits for a shared "go" file (so every child attempts at the same instant), tries the
# lock, holds it for `hold` seconds if it won, and prints one JSON line.
_CHILD = textwrap.dedent("""
    import json, os, sys, time
    sys.path.insert(0, {script_dir!r})
    import single_instance as si
    lock_path, go_path, wait_sec, hold = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
    deadline = time.monotonic() + 30
    while not os.path.exists(go_path) and time.monotonic() < deadline:
        time.sleep(0.001)
    lk = si.InstanceLock(lock_path)
    won = lk.acquire(wait_sec=wait_sec, poll_sec=0.05)
    print(json.dumps({{"pid": os.getpid(), "won": won}}), flush=True)
    if won:
        time.sleep(hold)
        lk.release()
""").format(script_dir=SCRIPT_DIR)


def _spawn(lock_path, go_path, wait_sec, hold):
    return subprocess.Popen([sys.executable, "-c", _CHILD, lock_path, go_path, str(wait_sec), str(hold)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _result(proc, timeout=60):
    out, err = proc.communicate(timeout=timeout)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines:
        raise AssertionError(f"child printed nothing; stderr={err!r}")
    return json.loads(lines[-1])


class InstanceLockUnit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = si.instance_lock_path(self.dir)

    def test_second_claim_fails_while_first_holds(self):
        a, b = si.InstanceLock(self.path), si.InstanceLock(self.path)
        try:
            self.assertTrue(a.acquire())
            self.assertFalse(b.acquire())
            self.assertTrue(si.is_held(self.path))
        finally:
            a.release()
            b.release()
        self.assertFalse(si.is_held(self.path))

    def test_reacquire_is_true_and_never_drops_the_claim(self):
        a = si.InstanceLock(self.path)
        try:
            self.assertTrue(a.acquire())
            self.assertTrue(a.acquire())  # re-entrant call must not release-then-retry
            self.assertTrue(si.is_held(self.path))
        finally:
            a.release()

    def test_release_is_idempotent_and_never_deletes_the_file(self):
        a = si.InstanceLock(self.path)
        self.assertTrue(a.acquire())
        a.release()
        a.release()
        self.assertTrue(os.path.exists(self.path), "the lock file is a handle to lock — never deleted")
        self.assertFalse(a.held)

    def test_loser_can_name_the_holder(self):
        a, b = si.InstanceLock(self.path), si.InstanceLock(self.path)
        try:
            self.assertTrue(a.acquire())
            self.assertFalse(b.acquire())
            self.assertIn(str(os.getpid()), b.holder_note())
            self.assertEqual(si.holder(self.path), {"held": True, "pid": os.getpid()})
        finally:
            a.release()
        self.assertEqual(si.holder(self.path), {"held": False, "pid": None})

    def test_waiter_takes_over_when_holder_releases(self):
        # The in-process shape of a re-exec handoff: the waiter is already waiting when the holder lets go.
        a, b = si.InstanceLock(self.path), si.InstanceLock(self.path)
        self.assertTrue(a.acquire())
        got = {}
        t = threading.Thread(target=lambda: got.setdefault("won", b.acquire(wait_sec=10, poll_sec=0.02)))
        t.start()
        time.sleep(0.3)
        self.assertNotIn("won", got)  # still waiting — the holder has it
        a.release()
        t.join(timeout=15)
        try:
            self.assertTrue(got.get("won"))
        finally:
            b.release()

    def test_wait_gives_up_after_wait_sec(self):
        a, b = si.InstanceLock(self.path), si.InstanceLock(self.path)
        try:
            self.assertTrue(a.acquire())
            t0 = time.monotonic()
            self.assertFalse(b.acquire(wait_sec=0.3, poll_sec=0.05))
            self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        finally:
            a.release()


class RaceAcrossProcesses(unittest.TestCase):
    """Real processes, real kernel lock."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = si.instance_lock_path(self.dir)
        self.go = os.path.join(self.dir, "go")

    def test_starts_within_milliseconds_exactly_one_wins(self):
        procs = [_spawn(self.path, self.go, 0, 2.0) for _ in range(4)]
        time.sleep(1.5)  # let every child reach its spin on the go file
        with open(self.go, "w", encoding="utf-8") as fh:
            fh.write("go")
        results = [_result(p) for p in procs]
        winners = [r for r in results if r["won"]]
        self.assertEqual(len(winners), 1, f"exactly one daemon may come up, got {results}")

    def test_start_during_reexec_handoff_exactly_one_comes_up(self):
        # The handoff race: the outgoing daemon (this test) holds the lock through its wind-down; the
        # watchdog's revived copy AND the re-exec'd successor both start meanwhile and wait. When the
        # outgoing daemon hands the lock over, exactly one of them may come up; the other must give up.
        holder = si.InstanceLock(self.path)
        self.assertTrue(holder.acquire())
        with open(self.go, "w", encoding="utf-8") as fh:
            fh.write("go")
        revived = _spawn(self.path, self.go, 4.0, 6.0)
        successor = _spawn(self.path, self.go, 4.0, 6.0)
        time.sleep(1.0)
        self.assertIsNone(revived.poll())
        self.assertIsNone(successor.poll())  # both waiting, neither through
        holder.release()  # the handoff
        results = [_result(revived), _result(successor)]
        self.assertEqual(sum(1 for r in results if r["won"]), 1, f"got {results}")

    def test_holder_dying_frees_the_lock(self):
        # No stale-lock rule to get wrong: the OS drops the claim when the holder's process ends.
        with open(self.go, "w", encoding="utf-8") as fh:
            fh.write("go")
        p = _spawn(self.path, self.go, 0, 30.0)
        deadline = time.monotonic() + 20
        while not si.is_held(self.path) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(si.is_held(self.path))
        p.kill()
        p.communicate(timeout=20)
        deadline = time.monotonic() + 10
        while si.is_held(self.path) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(si.is_held(self.path))


class RespawnHandoff(unittest.TestCase):
    """presence._respawn_detached: the lock is handed over BEFORE the spawn."""

    def setUp(self):
        import presence  # noqa: F401 — heavy import, only for this class
        self.presence = presence
        self.dir = tempfile.mkdtemp()

    def test_lock_free_at_spawn_time(self):
        lock = si.InstanceLock(si.instance_lock_path(self.dir))
        self.assertTrue(lock.acquire())
        seen = {}

        def fake_spawn(*a, **k):
            seen["held_at_spawn"] = si.is_held(si.instance_lock_path(self.dir))
            return mock.MagicMock()

        logs = []
        with mock.patch.object(self.presence.subprocess, "Popen", side_effect=fake_spawn), \
                mock.patch.object(self.presence.os, "execv", side_effect=lambda *a: fake_spawn()):
            self.presence._respawn_detached(logs.append, self.dir, None, instance_lock=lock)
        self.assertIn("held_at_spawn", seen, f"no spawn happened; log={logs}")
        self.assertFalse(seen["held_at_spawn"], "the successor must be able to take the lock")
        self.assertFalse(lock.held)

    def test_a_refused_respawn_keeps_the_lock_until_exit(self):
        """A respawn the crash-loop guard declines never gives up a claim it still holds while it exits."""
        now = self.presence.datetime.now(self.presence.timezone.utc)
        self.presence.save_json(self.presence.boot_attempts_path(self.dir),
                                [now.isoformat().replace("+00:00", "Z")] * self.presence.CRASHLOOP_MAX_BOOTS)
        lock = si.InstanceLock(si.instance_lock_path(self.dir))
        self.assertTrue(lock.acquire())
        try:
            with mock.patch.object(self.presence, "send_telegram", return_value={"ok": False}), \
                    mock.patch.object(self.presence.subprocess, "Popen") as popen, \
                    mock.patch.object(self.presence.os, "execv") as execv:
                ok = self.presence._respawn_detached(lambda m: None, self.dir, None, instance_lock=lock)
            self.assertFalse(ok)
            popen.assert_not_called()
            execv.assert_not_called()
            self.assertTrue(lock.held)
        finally:
            lock.release()


class PresenceLoserReachesNothing(unittest.TestCase):
    """The real presence.py entry point, started while another process holds the lock."""

    def test_loser_exits_on_one_line_before_any_side_effect(self):
        state = tempfile.mkdtemp()
        log_path = os.path.join(state, "presence.log")
        holder = si.InstanceLock(si.instance_lock_path(state))
        self.assertTrue(holder.acquire())
        try:
            env = dict(os.environ, PYTHONUTF8="1")
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("SENESCHALD_SUPERVISED", None)
            proc = subprocess.run(
                [sys.executable, os.path.join(SCRIPT_DIR, "presence.py"), "--state-dir", state,
                 "--log-file", log_path, "--instance-lock-wait-sec", "0", "--stub-brain", "--stub-send",
                 "--no-cockpit"],
                capture_output=True, text=True, timeout=120, env=env)
        finally:
            holder.release()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(log_path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, f"a loser logs exactly one line, got {lines}")
        self.assertIn("presence already running", lines[0])
        # Nothing a daemon does: no advisory lock, no daemon state, no boot record.
        for name in ("presence.lock", "boot-attempts.json", "presence-state.json"):
            self.assertFalse(os.path.exists(os.path.join(state, name)), name)


if __name__ == "__main__":
    unittest.main()
