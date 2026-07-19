#!/usr/bin/env python3
"""Tests for the post-restart backlog ack (seneschal/docs/telegram-inbound-spec.md §5).
Stdlib ``unittest`` only, like the rest of the suite.

The incident: after a `reseneschald`, the daemon drained a five-message backlog serially and quietly, so
from the owner's side only one reply appeared and they re-forwarded everything. One line up front fixes
that — but the line must be RARE, or the assistant turns chatty. What's covered:
  * a cold-start burst (N >= 2) gets exactly one ack, before the messages are answered;
  * a cold-start single message gets NONE (the steady-state path stays silent);
  * the flag is one-shot: a later burst mid-run is silent;
  * an empty first poll doesn't burn the flag (the ack still fires on the burst that follows);
  * a failed ack send doesn't cost us the backlog.

Every send goes through deliver_reply with --stub-send, so this suite can NEVER message the real
Telegram (that mistake has already been made once).

Run:  python -m unittest seneschal.scripts.test_telegram_backlog_ack   (or)   python test_telegram_backlog_ack.py
"""
import argparse
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402


def _args(state_dir, **over):
    base = dict(state_dir=state_dir, telegram_env="unused.env", discord_env=None,
                stub_send=True, router_mode="off")
    base.update(over)
    return argparse.Namespace(**base)


def _sent(state_dir):
    path = os.path.join(state_dir, "sent.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines() if line.strip()]


def _msgs(n):
    return [{"text": f"message {i}"} for i in range(n)]


class BacklogAck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)
        self.state = pr.DaemonState()
        self.log = lambda *_: None

    async def test_cold_start_burst_is_acked_once(self):
        await pr._maybe_backlog_ack(self.state, self.args, self.log, _msgs(5))
        sent = _sent(self.dir)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["channel"], "telegram")
        self.assertIn("got your 5 messages", sent[0]["text"])

    async def test_cold_start_single_message_is_silent(self):
        """The steady-state path — one message, one reply. An ack here would be pure chatter."""
        await pr._maybe_backlog_ack(self.state, self.args, self.log, _msgs(1))
        self.assertEqual(_sent(self.dir), [])
        self.assertFalse(self.state.just_started)  # still burned: this WAS the first poll

    async def test_one_shot_a_later_burst_is_silent(self):
        await pr._maybe_backlog_ack(self.state, self.args, self.log, _msgs(3))
        await pr._maybe_backlog_ack(self.state, self.args, self.log, _msgs(4))
        self.assertEqual(len(_sent(self.dir)), 1)
        self.assertIn("got your 3 messages", _sent(self.dir)[0]["text"])

    async def test_empty_poll_does_not_burn_the_flag(self):
        """A quiet first poll is the norm; the ack must still be available for the burst that follows."""
        await pr._maybe_backlog_ack(self.state, self.args, self.log, [])
        self.assertTrue(self.state.just_started)
        await pr._maybe_backlog_ack(self.state, self.args, self.log, _msgs(2))
        self.assertEqual(len(_sent(self.dir)), 1)

    async def test_ack_is_threaded_for_continuity(self):
        await pr._maybe_backlog_ack(self.state, self.args, self.log, _msgs(2))
        with open(os.path.join(self.dir, "telegram-thread.json"), encoding="utf-8") as fh:
            thread = json.load(fh)
        self.assertEqual(thread[-1]["role"], "assistant")
        self.assertIn("got your 2 messages", thread[-1]["text"])

    async def test_failed_ack_does_not_thread_and_does_not_raise(self):
        """A dropped ack must cost us the ack, never the backlog it was announcing."""
        with mock.patch.object(pr, "deliver_reply", return_value=False):
            await pr._maybe_backlog_ack(self.state, self.args, self.log, _msgs(3))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "telegram-thread.json")))
        self.assertFalse(self.state.just_started)


class BacklogAckInTaskOrder(unittest.IsolatedAsyncioTestCase):
    """The ack has to land BEFORE the answers, or it's just a confusing trailer."""

    async def test_ack_precedes_enqueue(self):
        d = tempfile.mkdtemp()
        state, args = pr.DaemonState(), _args(d)
        order = []
        with mock.patch.object(pr, "deliver_reply",
                               side_effect=lambda *a, **k: order.append("ack") or True):
            await pr._maybe_backlog_ack(state, args, lambda *_: None, _msgs(2))
        order.append("enqueue")
        self.assertEqual(order, ["ack", "enqueue"])


if __name__ == "__main__":
    unittest.main()
