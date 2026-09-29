#!/usr/bin/env python3
"""Tests for WarmSession's spawn-fallback (presence.py) — the safety net below the v3 model dials.

`resolve_warm_model` only checks that a warm_model dial is in the capability RANK, NOT that the model
is actually spawnable. So a dial pointed at a not-yet-available tier (e.g. a freshly-ranked
`claude-opus-5` the CLI won't serve) would spawn a warm session whose every turn dies — darkening the
chat with no net. WarmSession now self-heals: if the FIRST turn on the configured model fails before the
session ever delivers a good reply, it re-spawns once on a known-good floor (`fallback_model`, the
launcher's --model / claude-opus-4-8) and retries the same turn. Only the first turn is guarded — a
mid-conversation death is a genuine session drop, not a bad model.

Stdlib ``unittest`` only; no live `claude`. subprocess.Popen is replaced with fake processes whose
stdout is a canned stream-json event list, so we drive start()/send()/close() with no real spawn. Run:
  python -m unittest seneschal.scripts.test_presence_warm_fallback
"""
import json
import os
import sys
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402


# A clean init + successful result — one delivered turn.
GOOD = [{"type": "system", "subtype": "init", "session_id": "s1", "apiKeySource": "none"},
        {"type": "result", "is_error": False, "result": "hello"}]
# stdout closes with no result event at all — how a process that died on a bad --model looks from here.
DEAD_NO_RESULT = []
# an explicit error result — the other shape a rejected model can take (is_error → None upstream).
ERROR_RESULT = [{"type": "result", "is_error": True, "result": "model not found: claude-opus-5"}]


class _FakeStdin:
    def __init__(self):
        self.closed = False
        self.written = []

    def write(self, s):
        self.written.append(s)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeProc:
    """Stand-in for subprocess.Popen: stdout is a fixed iterator of stream-json lines for one turn."""

    def __init__(self, events, exit_code=None):
        self.stdin = _FakeStdin()
        self.stdout = iter([json.dumps(ev) + "\n" for ev in events])
        self._exit = exit_code
        self.terminated = False

    def poll(self):
        return self._exit

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True


def _session(model, fallback):
    return pr.WarmSession("claude", model, "bypassPermissions", lambda *_a, **_k: None,
                          fallback_model=fallback)


class WarmSessionSpawnFallback(unittest.TestCase):

    def test_first_turn_dead_stream_falls_back_and_retries(self):
        bad, good = _FakeProc(DEAD_NO_RESULT), _FakeProc(GOOD)
        ws = _session("claude-opus-5", "claude-opus-4-8")
        with mock.patch.object(pr.subprocess, "Popen", side_effect=[bad, good]):
            ws.start()
            reply = ws.send("hi")
        self.assertEqual(reply, "hello")
        self.assertTrue(ws.did_fallback)
        self.assertEqual(ws.model, "claude-opus-4-8")   # swapped to the floor
        self.assertTrue(ws._delivered_any)
        self.assertTrue(bad.stdin.closed)               # old proc closed before respawn

    def test_first_turn_error_result_also_falls_back(self):
        bad, good = _FakeProc(ERROR_RESULT), _FakeProc(GOOD)
        ws = _session("claude-opus-5", "claude-opus-4-8")
        with mock.patch.object(pr.subprocess, "Popen", side_effect=[bad, good]):
            ws.start()
            reply = ws.send("hi")
        self.assertEqual(reply, "hello")
        self.assertTrue(ws.did_fallback)

    def test_good_first_turn_never_falls_back(self):
        good = _FakeProc(GOOD)
        ws = _session("claude-opus-5", "claude-opus-4-8")
        # Only ONE proc queued: a stray fallback attempt would StopIteration on Popen and fail loudly.
        with mock.patch.object(pr.subprocess, "Popen", side_effect=[good]):
            ws.start()
            reply = ws.send("hi")
        self.assertEqual(reply, "hello")
        self.assertFalse(ws.did_fallback)
        self.assertEqual(ws.model, "claude-opus-5")     # untouched
        self.assertTrue(ws._delivered_any)

    def test_no_fallback_when_model_equals_floor(self):
        # Already on the floor → nothing safer to try; a failure just returns None (normal drop).
        bad = _FakeProc(DEAD_NO_RESULT)
        ws = _session("claude-opus-4-8", "claude-opus-4-8")
        with mock.patch.object(pr.subprocess, "Popen", side_effect=[bad]):
            ws.start()
            reply = ws.send("hi")
        self.assertIsNone(reply)
        self.assertFalse(ws.did_fallback)

    def test_no_fallback_when_floor_is_none(self):
        bad = _FakeProc(DEAD_NO_RESULT)
        ws = _session("claude-opus-5", None)
        with mock.patch.object(pr.subprocess, "Popen", side_effect=[bad]):
            ws.start()
            reply = ws.send("hi")
        self.assertIsNone(reply)
        self.assertFalse(ws.did_fallback)

    def test_fallback_spent_only_once_even_if_floor_also_fails(self):
        bad1, bad2 = _FakeProc(DEAD_NO_RESULT), _FakeProc(DEAD_NO_RESULT)
        ws = _session("claude-opus-5", "claude-opus-4-8")
        with mock.patch.object(pr.subprocess, "Popen", side_effect=[bad1, bad2]):
            ws.start()
            reply1 = ws.send("hi")
            self.assertIsNone(reply1)
            self.assertTrue(ws.did_fallback)
            # A later turn must NOT try to fall back again (guard). No new Popen ⇒ no StopIteration.
            reply2 = ws.send("still there?")
        self.assertIsNone(reply2)

    def test_no_fallback_after_a_delivered_turn(self):
        # Good turn, THEN the stream is exhausted on the next turn (a real mid-convo drop). Must not
        # fall back: _delivered_any latches. One proc queued ⇒ a fallback would StopIteration.
        good_then_dead = _FakeProc(GOOD)
        ws = _session("claude-opus-5", "claude-opus-4-8")
        with mock.patch.object(pr.subprocess, "Popen", side_effect=[good_then_dead]):
            ws.start()
            self.assertEqual(ws.send("hi"), "hello")
            reply2 = ws.send("and again")     # stdout iterator now exhausted → None
        self.assertIsNone(reply2)
        self.assertFalse(ws.did_fallback)

    def test_can_fallback_predicate(self):
        ws = _session("claude-opus-5", "claude-opus-4-8")
        self.assertTrue(ws._can_fallback())
        ws.did_fallback = True
        self.assertFalse(ws._can_fallback())            # spent
        ws2 = _session("claude-opus-4-8", "claude-opus-4-8")
        self.assertFalse(ws2._can_fallback())           # equal to floor
        ws3 = _session("claude-opus-5", None)
        self.assertFalse(ws3._can_fallback())           # no floor


class MakeSessionFloor(unittest.TestCase):
    """The floor default constant is the launcher's --model (claude-opus-4-8)."""

    def test_default_floor_constant(self):
        self.assertEqual(pr.DEFAULT_WARM_FALLBACK_MODEL, "claude-opus-4-8")


if __name__ == "__main__":
    unittest.main()
