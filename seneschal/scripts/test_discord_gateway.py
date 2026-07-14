#!/usr/bin/env python3
"""Tests for the Discord gateway module's pure protocol/parsing layer (phase 2 — see
seneschal/docs/asyncio-daemon-design.md). Stdlib ``unittest`` only.

Everything tested here is import-safe without the ``websockets`` dependency — that's a design rule
(CI and any stale interpreter must still py_compile + test this module), and the first test pins it.
The live connection loop (run_gateway/_session_loop) is exercised against the real gateway only in
prod; its logic is kept thin around these pure helpers on purpose.

Run:  python -m unittest seneschal.scripts.test_discord_gateway   (or)   python test_discord_gateway.py
"""
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import discord_gateway as gw  # noqa: E402

CHANNEL = "123456789"


def _msg_event(text="hi assistant", channel=CHANNEL, author_id="42", bot=False, seq=7, mid="111"):
    return {"op": gw.OP_DISPATCH, "t": "MESSAGE_CREATE", "s": seq,
            "d": {"id": mid, "channel_id": channel, "content": text,
                  "timestamp": "2026-07-11T12:00:00Z",
                  "author": {"id": author_id, "username": "owner", "bot": bot}}}


class ImportSafety(unittest.TestCase):
    def test_module_imports_and_reports_availability_without_websockets(self):
        # gateway_available() must answer (not raise) whether or not the dep is installed —
        # presence.discord_task branches on it for the REST fallback.
        self.assertIn(gw.gateway_available(), (True, False))

    def test_intents_include_message_content(self):
        self.assertTrue(gw.INTENTS & (1 << 15), "MESSAGE_CONTENT intent missing — inbound would be empty")
        self.assertTrue(gw.INTENTS & (1 << 9), "GUILD_MESSAGES intent missing")


class PayloadBuilders(unittest.TestCase):
    def test_identify_shape(self):
        p = gw.build_identify("tok-123")
        self.assertEqual(p["op"], gw.OP_IDENTIFY)
        self.assertEqual(p["d"]["token"], "tok-123")
        self.assertEqual(p["d"]["intents"], gw.INTENTS)
        self.assertIn("properties", p["d"])

    def test_resume_shape(self):
        p = gw.build_resume("tok", "sess-9", 41)
        self.assertEqual(p["op"], gw.OP_RESUME)
        self.assertEqual(p["d"], {"token": "tok", "session_id": "sess-9", "seq": 41})

    def test_heartbeat_carries_seq(self):
        self.assertEqual(gw.build_heartbeat(99), {"op": gw.OP_HEARTBEAT, "d": 99})
        self.assertEqual(gw.build_heartbeat(None)["d"], None)

    def test_backoff_doubles_and_caps(self):
        self.assertEqual(gw.next_backoff(1.0), 2.0)
        self.assertEqual(gw.next_backoff(40.0), gw.MAX_BACKOFF_SEC)
        self.assertEqual(gw.next_backoff(0.0), 2.0)  # floor — never a zero-delay hot loop


class SessionBookkeeping(unittest.TestCase):
    def test_ready_captures_resume_state(self):
        s = gw.GatewaySession()
        self.assertFalse(s.can_resume())
        s.record({"op": gw.OP_DISPATCH, "t": "READY", "s": 1,
                  "d": {"session_id": "abc", "resume_gateway_url": "wss://resume.example"}})
        self.assertTrue(s.can_resume())
        self.assertEqual(s.session_id, "abc")
        self.assertIn("resume.example", s.connect_url())
        self.assertIn("v=10", s.connect_url())

    def test_seq_tracks_dispatches_and_reset_falls_back_to_identify(self):
        s = gw.GatewaySession()
        s.record(_msg_event(seq=5))
        self.assertEqual(s.seq, 5)
        s.record({"op": gw.OP_HEARTBEAT_ACK, "s": None})  # non-dispatch: seq untouched
        self.assertEqual(s.seq, 5)
        s.reset()
        self.assertFalse(s.can_resume())
        self.assertIn(gw.GATEWAY_URL, s.connect_url())


class MessageExtraction(unittest.TestCase):
    def test_extracts_poll_shaped_message(self):
        m = gw.extract_message(_msg_event(), CHANNEL, set())
        self.assertEqual(m["text"], "hi assistant")
        self.assertEqual(m["author"], "owner")
        self.assertEqual(m["channel_id"], CHANNEL)
        self.assertEqual(m["id"], "111")

    def test_filters_mirror_the_rest_poll(self):
        self.assertIsNone(gw.extract_message(_msg_event(channel="999"), CHANNEL, set()))      # other channel
        self.assertIsNone(gw.extract_message(_msg_event(bot=True), CHANNEL, set()))           # no self-echo
        self.assertIsNone(gw.extract_message(_msg_event(author_id="66"), CHANNEL, {"42"}))    # allowlist
        self.assertIsNotNone(gw.extract_message(_msg_event(author_id="42"), CHANNEL, {"42"}))
        self.assertIsNone(gw.extract_message({"op": gw.OP_HEARTBEAT_ACK}, CHANNEL, set()))    # not a dispatch


class OffsetCursor(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "discord-offset")

    def test_advances_forward_only(self):
        gw.advance_offset(self.path, "100")
        self.assertEqual(gw.read_offset(self.path), "100")
        gw.advance_offset(self.path, "250")
        self.assertEqual(gw.read_offset(self.path), "250")
        gw.advance_offset(self.path, "180")  # older snowflake must never rewind the cursor
        self.assertEqual(gw.read_offset(self.path), "250")

    def test_garbage_id_is_ignored(self):
        gw.advance_offset(self.path, "100")
        gw.advance_offset(self.path, "not-a-snowflake")
        gw.advance_offset(self.path, None)
        self.assertEqual(gw.read_offset(self.path), "100")


if __name__ == "__main__":
    unittest.main()
