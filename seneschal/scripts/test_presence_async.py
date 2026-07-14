#!/usr/bin/env python3
"""Tests for the presence daemon's asyncio reactive core (the phase-1 port — see
seneschal/docs/asyncio-daemon-design.md). Stdlib ``unittest`` only, like the rest of the suite.

What's covered here is the drainer's turn lifecycle — the part of the old loop where every branch
encodes a production incident — plus control quiesce and the offline end-to-end harness:
  * a delivered turn pops the durable queue, appends continuity, stamps activity;
  * a transient send failure rolls the poison-pill counter back, keeps the message queued, and drops
    the warm session (no phantom replies);
  * a message past MAX_TURN_ATTEMPTS is dead-lettered with a heads-up, not replayed forever;
  * a defer-until-idle control holds while chat is busy and applies once idle;
  * the full daemon (main()) runs a fake inbox through the stub brain offline — with --stub-send, so
    this suite can NEVER message the real Telegram (that mistake has already been made once).

Run:  python -m unittest seneschal.scripts.test_presence_async   (or)   python test_presence_async.py
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402


def _args(state_dir, **over):
    """A minimal args namespace for exercising the tasks directly."""
    base = dict(state_dir=state_dir, telegram_env="unused.env", call_env=None, discord_env=None,
                no_discord=True, no_discord_gateway=False, stub_brain=True, stub_send=True,
                fake_inbox=None, router_mode="off",
                no_reminders=True, no_peek=True, no_slots=True, max_iterations=0,
                poll_timeout=1, discord_poll_sec=0.05, tick_sec=0.05, idle_min=0.0,
                model=None, slot_model=None, slot_catchup_min=180, claude_bin="claude",
                notion_mcp=None, permission_mode="bypassPermissions",
                peek_interval_min=0, watch_prompt=None, watch_cmd=None, watch_model=None)
    base.update(over)
    return argparse.Namespace(**base)


def _sent(state_dir):
    path = os.path.join(state_dir, "sent.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines() if line.strip()]


async def _run_drainer_until(state, args, cond, timeout=5.0):
    """Run drainer_task until cond() holds (checked every 10 ms), then wind it down."""
    task = asyncio.ensure_future(
        pr.drainer_task(state, args, lambda *_: None, lambda: pr.StubWarmSession(), 600.0))
    try:
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.01)
    finally:
        state.stop.set()
        state.pending_event.set()  # wake it if parked
        await asyncio.wait_for(task, timeout=5.0)


class DrainerLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)

    async def test_delivered_turn_pops_and_records(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "hey assistant", 0)]
        state.pending_event.set()
        await _run_drainer_until(state, self.args, lambda: not state.pending)
        sent = _sent(self.dir)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["channel"], "telegram")
        self.assertIn("stub reply", sent[0]["text"])
        # Durable queue drained on disk too, and continuity recorded the assistant's reply.
        st = pr.load_daemon_state(self.dir)
        self.assertEqual(st["pending"], [])
        thread = pr.load_json(pr.thread_path(self.dir), [])
        self.assertEqual([t["role"] for t in thread], ["assistant"])

    async def test_transient_send_failure_keeps_message_and_rolls_back_attempts(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "flaky wire", 0)]
        state.pending_event.set()
        calls = {"n": 0}
        orig = pr.deliver_reply

        def failing_deliver(channel, reply, args, log, retries=1):
            calls["n"] += 1
            return False

        pr.deliver_reply = failing_deliver
        try:
            await _run_drainer_until(state, self.args, lambda: calls["n"] >= 1 and state.session is None)
        finally:
            pr.deliver_reply = orig
        # Still queued (durably), attempts rolled back to 0 (transient outage ≠ poison), session dropped
        # so the retry re-grounds, and nothing was recorded as sent.
        self.assertEqual(state.pending, [("telegram", "flaky wire", 0)])
        st = pr.load_daemon_state(self.dir)
        self.assertEqual([(i["channel"], i["text"], i["attempts"]) for i in st["pending"]],
                         [("telegram", "flaky wire", 0)])
        thread = pr.load_json(pr.thread_path(self.dir), [])
        self.assertEqual(thread, [])

    async def test_poison_pill_dead_letters_with_heads_up(self):
        state = pr.DaemonState()
        state.pending = [("discord", "kills the daemon", pr.MAX_TURN_ATTEMPTS)]
        state.pending_event.set()
        await _run_drainer_until(state, self.args, lambda: not state.pending)
        sent = _sent(self.dir)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["channel"], "discord")
        self.assertIn("set it aside", sent[0]["text"])
        self.assertIn("kills the daemon", sent[0]["text"])
        self.assertEqual(pr.load_daemon_state(self.dir)["pending"], [])

    async def test_dead_session_apologizes_and_still_pops(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "hello?", 0)]
        state.pending_event.set()

        class DeadSession(pr.StubWarmSession):
            def send(self, text):
                return None  # session died mid-turn

        args = self.args
        task = asyncio.ensure_future(
            pr.drainer_task(state, args, lambda *_: None, lambda: DeadSession(), 600.0))
        try:
            async with asyncio.timeout(5.0):
                while state.pending:
                    await asyncio.sleep(0.01)
        finally:
            state.stop.set()
            state.pending_event.set()
            await asyncio.wait_for(task, timeout=5.0)
        sent = _sent(self.dir)
        self.assertEqual(len(sent), 1)
        self.assertIn("hit a snag", sent[0]["text"])
        self.assertIsNone(state.session)  # dead session was dropped, not reused


class ControlQuiesce(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # Control task only runs outside stub/fake modes (like the old loop's gating).
        self.args = _args(self.dir, stub_brain=False, fake_inbox=None)

    async def test_control_defers_while_busy_then_applies_when_idle(self):
        state = pr.DaemonState()
        state.session = pr.StubWarmSession()   # live session +
        state.pending = [("telegram", "mid-turn", 1)]  # queued work = busy
        pr.save_json(pr.control_queue_path(self.dir),
                     [{"action": "restart", "defer_until_idle": True, "reason": "merge to main"}])
        task = asyncio.ensure_future(pr.control_task(state, self.args, lambda *_: None))
        try:
            async with asyncio.timeout(5.0):
                while not state.control_pending:
                    await asyncio.sleep(0.01)
                # Deferred, not applied. Now the chat goes idle → it must apply and wind us down.
                self.assertIsNone(state.pending_action)
                state.session = None
                state.pending = []
                await asyncio.wait_for(task, timeout=5.0)
        finally:
            state.stop.set()
            if not task.done():
                await asyncio.wait_for(task, timeout=5.0)
        self.assertEqual(state.pending_action, "restart")
        self.assertTrue(state.stop.is_set())
        self.assertEqual(pr.load_control_queue(self.dir), [])  # consumed

    async def test_legacy_restart_sentinel_applies_when_idle(self):
        state = pr.DaemonState()
        with open(pr.restart_request_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("restart requested\n")
        task = asyncio.ensure_future(pr.control_task(state, self.args, lambda *_: None))
        await asyncio.wait_for(task, timeout=5.0)
        self.assertEqual(state.pending_action, "restart")
        self.assertFalse(pr.restart_requested(self.dir))  # sentinel cleared


class OfflineEndToEnd(unittest.TestCase):
    """The whole daemon, offline: fake inbox → stub brain → stub send. No network, no claude, and —
    because of --stub-send — no possibility of a real Telegram message escaping a test run."""

    def test_fake_inbox_drains_through_stub_brain(self):
        d = tempfile.mkdtemp()
        inbox = os.path.join(d, "inbox.json")
        # Two early batches then empty ones: gives the drainer time to finish before iterations run out.
        # (Fake-inbox entries are telegram_poll-shaped dicts, exactly like the real wire.)
        pr.save_json(inbox, [[{"text": "good morning"}], [{"text": "what's on today?"}],
                             [], [], [], [], [], []])
        argv = ["presence.py",
                "--state-dir", d, "--fake-inbox", inbox, "--stub-brain", "--stub-send",
                "--max-iterations", "8", "--no-peek", "--no-slots", "--no-reminders",
                "--router-mode", "off", "--idle-min", "0"]
        old_argv = sys.argv
        sys.argv = argv
        try:
            rc = pr.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(rc, 0)
        sent = _sent(d)
        self.assertEqual(len(sent), 2, f"expected both replies delivered, got {sent}")
        self.assertTrue(all(s["channel"] == "telegram" for s in sent))
        self.assertEqual(pr.load_daemon_state(d)["pending"], [])   # nothing left behind
        self.assertFalse(os.path.exists(pr.lock_path(d)))          # lock released on exit
        # Both sides of both exchanges recorded. Don't assert strict interleaving — the reactive core
        # legitimately allows message 2 to be read off the wire while turn 1 is still running, so
        # owner,owner,assistant,assistant is as correct as strict alternation.
        thread = pr.load_json(pr.thread_path(d), [])
        roles = [t["role"] for t in thread]
        self.assertEqual(sorted(roles), ["assistant", "assistant", "owner", "owner"])


if __name__ == "__main__":
    unittest.main()
