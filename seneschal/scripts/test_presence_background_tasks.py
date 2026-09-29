#!/usr/bin/env python3
"""Chat replies must not run one turn behind after a harness background-task notification.

The warm session is one long-lived `claude -p --input-format stream-json` process. A Bash call that
outran its 120 s timeout could be MOVED TO THE BACKGROUND by the harness; when it finished between
turns the CLI queued a `task_notification` turn, and that turn's `result` was the first one the daemon
read after writing the owner's next message — so the owner's message was answered one result late.

The fix is an env rail on the warm session's spawn — `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`
(`presence.WARM_SESSION_ENV`), verified in the daemon's exact spawn shape — plus a tripwire in the
read loop that logs any `task_notification` that reaches the process anyway. These tests pin:

* the overlay's content and its SCOPE (warm session only — `child_env` for one-shots is untouched);
* that `WarmSession.start()` actually hands the overlay to Popen;
* the tripwire, replayed against the exact harness event order a live probe recorded (notification
  → init → the notification's answer → result), so the failure shape is on record in a test that
  runs without a real `claude`.

Stdlib ``unittest`` only; ``subprocess.Popen`` is stubbed, nothing spawns.
"""
import json
import os
import sys
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402

FLAG = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"


class _FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, s):
        self.written.append(s)

    def flush(self):
        pass

    def close(self):
        pass


class _FakeProc:
    def __init__(self, events):
        self.stdin = _FakeStdin()
        self.stdout = iter([json.dumps(ev) + "\n" for ev in events])

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _session(log):
    return pr.WarmSession("claude", "claude-opus-5", "bypassPermissions", log)


class WarmSessionEnvTest(unittest.TestCase):
    def test_overlay_disables_background_tasks(self):
        self.assertEqual(pr.WARM_SESSION_ENV, {FLAG: "1"})

    def test_warm_session_env_is_child_env_plus_overlay(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-should-be-scrubbed"}, clear=False):
            env = pr.warm_session_env()
        self.assertEqual(env[FLAG], "1")
        self.assertEqual(env["SENESCHAL_SESSION_SOURCE"], "daemon")   # still THE warm session's marker
        self.assertNotIn("ANTHROPIC_API_KEY", env)                # billing safety untouched

    def test_scope_one_shots_and_jobs_keep_background_tasks(self):
        # A one-shot -p run (peek, slots) has no next turn for a queued notification to steal, and jobs'
        # own rails live in jobs.py — so the overlay is NOT part of child_env for any other source.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(FLAG, None)
            for source in ("scheduled", "watch"):
                self.assertNotIn(FLAG, pr.child_env(source), source)

    def test_start_hands_the_overlay_to_popen(self):
        captured = {}

        class _Popen:
            def __init__(self, cmd, **kw):
                captured["cmd"] = cmd
                captured["env"] = kw.get("env")

        with mock.patch.object(pr.subprocess, "Popen", _Popen):
            _session(lambda *_a, **_k: None).start()
        self.assertEqual(captured["env"][FLAG], "1")
        self.assertEqual(captured["env"]["SENESCHAL_SESSION_SOURCE"], "daemon")
        self.assertIn("stream-json", captured["cmd"])  # same spawn shape the probe verified against


class TaskNotificationTripwireTest(unittest.TestCase):
    """Replays the harness event order the live probe recorded for the FAILURE shape (background tasks
    enabled): an idle process emits the notification, then runs a turn on it, and that turn's result
    is what the daemon reads first after writing the owner's message."""

    # Exactly what the probe saw on stdout after the second stdin write (fields trimmed): the queued
    # notification, the notification-turn's init, its answer, its result — the owner's answer would be the
    # result after this one.
    SKEWED = [
        {"type": "system", "subtype": "background_tasks_changed", "tasks": []},
        {"type": "system", "subtype": "task_updated", "task_id": "b2m46b8p8",
         "patch": {"status": "completed"}},
        {"type": "system", "subtype": "task_notification", "task_id": "b2m46b8p8",
         "tool_use_id": "toolu_0156", "status": "completed", "output_file": "…/tasks/b2m46b8p8.output"},
        {"type": "system", "subtype": "init", "session_id": "s1", "apiKeySource": "none"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "*(answering a background search finishing — nothing new for you)*"}]}},
        {"type": "result", "is_error": False, "num_turns": 1,
         "result": "*(answering a background search finishing — nothing new for you)*",
         "usage": {"input_tokens": 4, "cache_read_input_tokens": 196_268}},
    ]
    CLEAN = [
        {"type": "system", "subtype": "init", "session_id": "s1", "apiKeySource": "none"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "PONG"}]}},
        {"type": "result", "is_error": False, "num_turns": 1, "result": "PONG",
         "usage": {"input_tokens": 4, "cache_read_input_tokens": 100}},
    ]

    def _run(self, events):
        lines = []
        s = _session(lambda msg, *_a, **_k: lines.append(msg))
        s.proc = _FakeProc(events)
        s.turn_gap_sec = 5.0  # the watchdog never fires here; keep it short regardless
        reply = s.send("Explain that last paragraph.")
        return s, reply, lines

    def test_notification_ahead_of_the_owners_turn_trips_the_wire(self):
        s, reply, lines = self._run(self.SKEWED)
        # The daemon's contract is unchanged — first result wins — which is exactly why the env rail
        # (not this loop) is the fix. What this pins is that the shape can no longer pass silently.
        self.assertEqual(reply, "*(answering a background search finishing — nothing new for you)*")
        self.assertEqual(s.harness_notifications, 1)
        hits = [ln for ln in lines if "task_notification" in ln and "WARM_SESSION_ENV" in ln]
        self.assertEqual(len(hits), 1, lines)
        self.assertIn("b2m46b8p8", hits[0])
        self.assertIn("one turn behind", hits[0])

    def test_clean_turn_stays_silent(self):
        s, reply, lines = self._run(self.CLEAN)
        self.assertEqual(reply, "PONG")
        self.assertEqual(s.harness_notifications, 0)
        self.assertFalse([ln for ln in lines if "task_notification" in ln], lines)

    def test_counter_is_per_process(self):
        s, _reply, _lines = self._run(self.SKEWED)
        self.assertEqual(s.harness_notifications, 1)
        with mock.patch.object(pr.subprocess, "Popen", lambda cmd, **kw: _FakeProc(self.CLEAN)):
            s.start()
        self.assertEqual(s.harness_notifications, 0)


if __name__ == "__main__":
    unittest.main()
