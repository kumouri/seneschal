#!/usr/bin/env python3
"""**A Telegram topic is a CONVERSATION, not a mailbox** — the module half: the inbound payload
carries the thread, and the reply goes back into the thread it came from
(`../docs/telegram-capability-map.md` §2.1).

A tap is a `callback_query` resolved by `message_id`, so pickers never need to know which thread they
came from. Ordinary conversation has no such escape hatch: a message typed *inside* a topic must be
answered inside it, or "topics" only means "a place pickers get filed".

**THE ONE FAILURE THAT IS WORSE THAN NO FEATURE: one thread's history leaking into another.** The
daemon's per-topic continuity cache and its routing of a reply back into its thread are daemon wiring
and are tested with the daemon; what is covered here is what `telegram_poll.py` and `telegram_send.py`
themselves promise — the id surfaced absent-not-null, the thread riding every chunk of a send, and a
topics-off send byte-identical to a pre-topics one.

Stdlib `unittest`, temp dirs only; nothing here touches real state or the network.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import telegram_poll as tp  # noqa: E402
import telegram_send as ts  # noqa: E402

PRS = 11539      # a "Pull requests" topic id


def _source(name: str) -> str:
    """One module's source, for the handful of assertions that are about a CALL SITE existing rather
    than about behaviour — a wiring line that can be deleted with every unit test still green."""
    with io.open(os.path.join(SCRIPT_DIR, name), encoding="utf-8") as fh:
        return fh.read()


class TheInboundPayloadCarriesTheThread(unittest.TestCase):
    """`telegram_poll` surfaces both fields Bot API 9.3 documents for private chats, each
    absent-not-null when there is no topic."""

    def test_the_id_is_extracted(self):
        self.assertEqual(tp.extract_thread_id({"message_thread_id": PRS}), PRS)

    def test_absent_is_none_and_zero_is_not_a_thread(self):
        self.assertIsNone(tp.extract_thread_id({}))
        self.assertIsNone(tp.extract_thread_id({"message_thread_id": 0}))
        self.assertIsNone(tp.extract_thread_id({"message_thread_id": True}))
        self.assertIsNone(tp.extract_thread_id({"message_thread_id": "11539"}))

    def test_the_payload_carries_both_fields(self):
        """Asserted through the real assembly rather than by re-implementing it — the block in
        `main` is the thing that could be dropped."""
        src = _source("telegram_poll.py")
        self.assertIn('item["message_thread_id"] = thread_id', src)
        self.assertIn('if msg.get("is_topic_message") is True:', src)
        self.assertIn('item["is_topic_message"] = True', src)

    def test_is_topic_message_is_not_what_routes(self):
        """A boolean cannot name a thread. Two fields that must agree are two fields that can
        disagree, so exactly one of them decides — and it is the one carrying the id."""
        self.assertIsNone(tp.extract_thread_id({"is_topic_message": True}))


class _RecordingApi:
    """A `telegram_send.api_call` stand-in that records every payload it is handed."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, c, method, params):
        self.calls.append((method, dict(params)))
        if self.fail is not None:
            raise self.fail
        return {"ok": True, "result": {"message_id": 900 + len(self.calls)}}

    def params(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


def _cfg():
    return {"token": "t", "chat_id": "123", "api_base": "https://example.invalid",
            "parse_mode": "", "format": "plain"}


class TheReplyGoesBackToItsThread(unittest.TestCase):
    """Outbound. `telegram_send` composes the thread into the SAME `params()` builder every chunk and
    every fallback rung already goes through — a second path is a second place to forget it."""

    def test_the_thread_rides_the_send(self):
        api = _RecordingApi()
        ts.send_text(_cfg(), "checks are green.", api=api, message_thread_id=PRS)
        self.assertEqual(api.params()[0]["message_thread_id"], PRS)

    def test_every_chunk_of_a_long_reply_carries_it(self):
        """A reply that lost its thread halfway through would arrive split across two
        conversations — which reads as the assistant being incoherent, not as a routing bug."""
        api = _RecordingApi()
        ts.send_text(_cfg(), "x" * 9000, api=api, message_thread_id=PRS)
        sends = api.params()
        self.assertGreater(len(sends), 1)
        self.assertTrue(all(p["message_thread_id"] == PRS for p in sends), sends)

    def test_topics_absent_is_byte_identical_to_a_pre_topics_send(self):
        """Stated as an EQUALITY between two whole payloads, not as `assertNotIn`. The weaker
        assertion passes on a payload carrying `message_thread_id: None`, and on one that grew some
        other key — and "with the toggle off, nothing changed" is the acceptance bar this whole
        feature is held to."""
        before, after = _RecordingApi(), _RecordingApi()
        ts.send_text(_cfg(), "what's on today?", api=before)
        ts.send_text(_cfg(), "what's on today?", api=after, message_thread_id=None)
        self.assertEqual(before.calls, after.calls)
        self.assertNotIn("message_thread_id", after.params()[0])


class AutoNamingIsNotBuilt(unittest.TestCase):
    """`ForumTopic.is_name_implicit` — *"the owner types, a thread appears, the assistant names it"* —
    is explicitly a later step, not this one. Asserted so that a future reader does not mistake this
    build for having shipped it."""

    def test_nothing_calls_edit_or_delete_forum_topic(self):
        for name in ("presence.py", "telegram_topics.py", "telegram_send.py", "telegram_poll.py"):
            src = _source(name)
            for method in ("editForumTopic", "deleteForumTopic", "closeForumTopic",
                           "reopenForumTopic"):
                self.assertNotIn(f'"{method}"', src, f"{name} calls {method}")

    def test_nothing_reads_is_name_implicit(self):
        for name in ("presence.py", "telegram_topics.py", "telegram_poll.py"):
            self.assertNotIn('get("is_name_implicit")', _source(name))


if __name__ == "__main__":
    unittest.main()
