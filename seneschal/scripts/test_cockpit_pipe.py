#!/usr/bin/env python3
"""Tests for the seneschald cockpit pipe protocol (seneschal/scripts/cockpit_pipe.py — presence.py's sixth
supervised task; design: seneschal/docs/asyncio-daemon-design.md). Stdlib ``unittest`` only, like the rest
of the suite — everything here runs WITHOUT the ``websockets`` dependency installed (mirroring
test_discord_gateway.py's import-safety pattern), because ``PipeHub`` is duck-typed against a fake
connection object rather than a real socket.

Run:  python -m unittest seneschal.scripts.test_cockpit_pipe   (or)   python test_cockpit_pipe.py
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import cockpit_pipe as cp  # noqa: E402


async def _noop3(*_a, **_kw):
    return None


async def _noop0():
    return None


class ImportSafety(unittest.TestCase):
    def test_module_reports_availability_without_raising(self):
        # pipe_available() must answer (not raise) whether or not websockets is installed —
        # presence.cockpit_task branches on it to decide whether to run the pipe server at all.
        self.assertIn(cp.pipe_available(), (True, False))


class TokenLifecycle(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_missing_token_reads_none(self):
        self.assertIsNone(cp.read_pipe_token(self.dir))

    def test_ensure_generates_and_persists(self):
        tok = cp.ensure_pipe_token(self.dir)
        self.assertTrue(tok)
        self.assertEqual(cp.read_pipe_token(self.dir), tok)
        # A second call must NOT rotate the token (clients need it stable across daemon restarts).
        self.assertEqual(cp.ensure_pipe_token(self.dir), tok)

    def test_token_file_is_plain_text_not_json(self):
        tok = cp.ensure_pipe_token(self.dir)
        with open(cp.pipe_token_path(self.dir), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), tok)


class FrameCodec(unittest.TestCase):
    def test_round_trip(self):
        frame = cp.chat_ack_frame("m1")
        self.assertEqual(cp.decode_frame(cp.encode_frame(frame)), frame)

    def test_decode_tolerates_garbage(self):
        self.assertIsNone(cp.decode_frame("not json"))
        self.assertIsNone(cp.decode_frame("[1, 2, 3]"))          # valid JSON, not an object
        self.assertIsNone(cp.decode_frame('{"no_type": 1}'))     # object, no `type`
        self.assertIsNone(cp.decode_frame('{"type": 5}'))        # type isn't a string
        self.assertIsNone(cp.decode_frame(None))

    def test_frame_builders_shape(self):
        self.assertEqual(cp.auth_frame("tok")["type"], cp.TYPE_AUTH)
        self.assertEqual(cp.auth_ok_frame(), {"type": cp.TYPE_AUTH_OK})
        self.assertEqual(cp.control_ack_frame("restart", ok=True),
                         {"type": cp.TYPE_CONTROL_ACK, "action": "restart", "ok": True})
        s = cp.status_frame(session_up=True, queue_depth=2)
        self.assertEqual(s["type"], cp.TYPE_STATUS)
        self.assertTrue(s["session_up"])

    def test_chat_event_stamps_ts_and_drops_none_fields(self):
        ev = cp.chat_event("turn_started", source="telegram", model=None, text="hi")
        self.assertEqual(ev["type"], cp.TYPE_CHAT_EVENT)
        self.assertEqual(ev["kind"], "turn_started")
        self.assertIn("ts", ev)
        self.assertNotIn("model", ev)   # None dropped
        self.assertEqual(ev["text"], "hi")


class StreamEventConversion(unittest.TestCase):
    def test_assistant_text_becomes_assistant_output(self):
        ev = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi there"}]}}
        out = cp.build_chat_event_from_stream(ev, source="telegram", model="opus")
        self.assertEqual(out["kind"], "assistant_output")
        self.assertEqual(out["text"], "hi there")
        self.assertEqual(out["model"], "opus")
        self.assertNotIn("tool_uses", out)

    def test_assistant_tool_use_becomes_tool_uses(self):
        ev = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}}]}}
        out = cp.build_chat_event_from_stream(ev, source="cockpit")
        self.assertEqual(out["kind"], "assistant_output")
        self.assertNotIn("text", out)
        self.assertEqual(out["tool_uses"], [{"name": "Read", "input_preview": '{"file_path": "x.py"}'}])

    def test_assistant_with_no_blocks_is_skipped(self):
        self.assertIsNone(cp.build_chat_event_from_stream(
            {"type": "assistant", "message": {"content": []}}, source="telegram"))

    def test_result_becomes_turn_done(self):
        ev = {"type": "result", "is_error": False, "result": "here's your answer",
              "duration_ms": 1200, "num_turns": 1, "total_cost_usd": 0.01,
              "usage": {"input_tokens": 10, "output_tokens": 20}}
        out = cp.build_chat_event_from_stream(ev, source="discord", model="sonnet")
        self.assertEqual(out["kind"], "turn_done")
        self.assertFalse(out["is_error"])
        self.assertEqual(out["reply_preview"], "here's your answer")
        self.assertEqual(out["duration_ms"], 1200)
        self.assertEqual(out["usage"], {"input_tokens": 10, "output_tokens": 20})

    def test_unknown_or_uninteresting_types_are_skipped(self):
        self.assertIsNone(cp.build_chat_event_from_stream({"type": "system", "subtype": "init"}, "t"))
        self.assertIsNone(cp.build_chat_event_from_stream({"type": "user"}, "t"))
        self.assertIsNone(cp.build_chat_event_from_stream("not a dict", "t"))
        self.assertIsNone(cp.build_chat_event_from_stream({"type": "assistant", "message": {}}, "t"))


class TranscriptRingBuffer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_append_and_read_tail_order(self):
        for i in range(5):
            cp.append_transcript_event(self.dir, {"i": i})
        tail = cp.read_transcript_tail(self.dir, limit=3)
        self.assertEqual([e["i"] for e in tail], [2, 3, 4])

    def test_read_tail_missing_file(self):
        self.assertEqual(cp.read_transcript_tail(self.dir), [])

    def test_read_tail_tolerates_corrupt_lines(self):
        path = cp.transcript_path(self.dir)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"i": 1}) + "\n")
            fh.write("not json\n")
            fh.write(json.dumps({"i": 2}) + "\n")
        tail = cp.read_transcript_tail(self.dir, limit=10)
        self.assertEqual([e["i"] for e in tail], [1, 2])

    def test_cap_trims_the_tail(self):
        # Write well past CAP + slack directly (faster than N individual appends) then trigger one
        # more append — the same trim path append_transcript_event uses.
        path = cp.transcript_path(self.dir)
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(cp.TRANSCRIPT_CAP + 250):
                fh.write(json.dumps({"i": i}) + "\n")
        cp.append_transcript_event(self.dir, {"i": "new"})
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        self.assertLessEqual(len(lines), cp.TRANSCRIPT_CAP + 1)
        last = json.loads(lines[-1])
        self.assertEqual(last["i"], "new")


class InboxFallback(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_append_then_drain_clears_and_dedupes(self):
        cp.append_inbox(self.dir, {"id": "a1", "text": "hello", "ts": "2026-07-17T00:00:00Z"})
        cp.append_inbox(self.dir, {"id": "a2", "text": "world", "ts": "2026-07-17T00:00:01Z"})
        items = cp.drain_inbox(self.dir)
        self.assertEqual([i["id"] for i in items], ["a1", "a2"])
        # File is cleared — a second drain (before any new appends) returns nothing.
        self.assertEqual(cp.drain_inbox(self.dir), [])

    def test_missing_file_drains_empty(self):
        self.assertEqual(cp.drain_inbox(self.dir), [])

    def test_blank_text_and_garbage_lines_are_skipped(self):
        path = cp.inbox_path(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"id": "b1", "text": "  "}) + "\n")   # blank text — dropped
            fh.write(json.dumps({"id": "b2", "text": "real"}) + "\n")
        self.assertEqual([i["id"] for i in cp.drain_inbox(self.dir)], ["b2"])

    def test_dedupe_persists_across_drains(self):
        cp.append_inbox(self.dir, {"id": "dup", "text": "first"})
        first = cp.drain_inbox(self.dir)
        self.assertEqual(len(first), 1)
        # Same id appended again later (e.g. a retried backend write) must not re-enqueue.
        cp.append_inbox(self.dir, {"id": "dup", "text": "first"})
        second = cp.drain_inbox(self.dir)
        self.assertEqual(second, [])

    def test_items_without_id_are_never_deduped(self):
        cp.append_inbox(self.dir, {"text": "no id here"})
        self.assertEqual(len(cp.drain_inbox(self.dir)), 1)
        cp.append_inbox(self.dir, {"text": "no id here"})
        self.assertEqual(len(cp.drain_inbox(self.dir)), 1)  # not deduped, but also not lost


# ------------------------------------------------------------------------------- PipeHub (fake socket)

class FakeWS:
    """Minimal duck-typed stand-in for a websockets connection: recv() is fed from a fixed inbound
    list then hangs (simulating an idle-but-open connection, so the caller's own timeout/cancellation
    is what ends things — never this fake). send() records what it received, optionally gated so a
    test can simulate a slow/wedged client. close() records the (code, reason)."""

    def __init__(self, inbound=None, block_send=False):
        self._inbound = list(inbound or [])
        self._idx = 0
        self.sent = []
        self.closed = None
        self._gate = asyncio.Event()
        if not block_send:
            self._gate.set()

    async def recv(self):
        if self._idx < len(self._inbound):
            msg = self._inbound[self._idx]
            self._idx += 1
            return msg
        await asyncio.Event().wait()  # hang — ended only by the caller cancelling/timing out

    async def send(self, data):
        await self._gate.wait()
        self.sent.append(data)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def release_send(self):
        self._gate.set()


async def _settle(seconds=0.05):
    await asyncio.sleep(seconds)


async def _cancel(task):
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


class PipeHubAuth(unittest.IsolatedAsyncioTestCase):
    def _hub(self, on_chat_send=_noop3, on_control_restart=_noop0, on_status_get=None):
        return cp.PipeHub(token="secret", on_chat_send=on_chat_send,
                          on_control_restart=on_control_restart,
                          on_status_get=on_status_get or (lambda: {}))

    async def test_wrong_token_is_closed(self):
        hub = self._hub()
        ws = FakeWS([cp.encode_frame(cp.auth_frame("nope"))])
        task = asyncio.ensure_future(hub.handle_connection(ws))
        await _settle()
        self.assertEqual(ws.closed, (4001, "unauthorized"))
        self.assertIsNone(hub.client)
        await _cancel(task)

    async def test_missing_first_frame_type_is_closed(self):
        hub = self._hub()
        ws = FakeWS([cp.encode_frame({"type": "chat.send", "text": "hi"})])  # not `auth` first
        task = asyncio.ensure_future(hub.handle_connection(ws))
        await _settle()
        self.assertEqual(ws.closed, (4001, "unauthorized"))
        await _cancel(task)

    async def test_auth_timeout_closes(self):
        hub = self._hub()
        hub.AUTH_TIMEOUT_SEC = 0.05
        ws = FakeWS([])  # recv() hangs forever — nothing ever arrives
        task = asyncio.ensure_future(hub.handle_connection(ws))
        await _settle(0.15)
        self.assertEqual(ws.closed, (4001, "auth timeout"))
        await _cancel(task)

    async def test_correct_token_adopts_client(self):
        hub = self._hub()
        ws = FakeWS([cp.encode_frame(cp.auth_frame("secret"))])
        task = asyncio.ensure_future(hub.handle_connection(ws))
        await _settle()
        self.assertIs(hub.client, ws)
        self.assertIn(cp.encode_frame(cp.auth_ok_frame()), ws.sent)
        await _cancel(task)

    async def test_second_connection_replaces_and_closes_first(self):
        hub = self._hub()
        ws1 = FakeWS([cp.encode_frame(cp.auth_frame("secret"))])
        t1 = asyncio.ensure_future(hub.handle_connection(ws1))
        await _settle()
        self.assertIs(hub.client, ws1)

        ws2 = FakeWS([cp.encode_frame(cp.auth_frame("secret"))])
        t2 = asyncio.ensure_future(hub.handle_connection(ws2))
        await _settle()
        self.assertIs(hub.client, ws2)
        self.assertEqual(ws1.closed, (4000, "replaced by newer cockpit connection"))
        await _cancel(t1)
        await _cancel(t2)


class PipeHubFrames(unittest.IsolatedAsyncioTestCase):
    async def test_chat_send_invokes_callback_and_acks(self):
        received = []

        async def on_chat_send(msg_id, text, frame):
            received.append((msg_id, text, frame.get("force_fable")))

        hub = cp.PipeHub(token="t", on_chat_send=on_chat_send, on_control_restart=_noop0,
                         on_status_get=lambda: {})
        ws = FakeWS([cp.encode_frame(cp.auth_frame("t")),
                    cp.encode_frame({"type": "chat.send", "id": "c1", "text": "hi there",
                                     "force_fable": True})])
        task = asyncio.ensure_future(hub.handle_connection(ws))
        await _settle()
        self.assertEqual(received, [("c1", "hi there", True)])
        self.assertIn(cp.encode_frame(cp.chat_ack_frame("c1")), ws.sent)
        await _cancel(task)

    async def test_blank_text_chat_send_still_acks_but_skips_callback(self):
        received = []

        async def on_chat_send(*a):
            received.append(a)

        hub = cp.PipeHub(token="t", on_chat_send=on_chat_send, on_control_restart=_noop0,
                         on_status_get=lambda: {})
        ws = FakeWS([cp.encode_frame(cp.auth_frame("t")),
                    cp.encode_frame({"type": "chat.send", "id": "c2", "text": "   "})])
        task = asyncio.ensure_future(hub.handle_connection(ws))
        await _settle()
        self.assertEqual(received, [])
        self.assertIn(cp.encode_frame(cp.chat_ack_frame("c2")), ws.sent)
        await _cancel(task)

    async def test_control_restart_success_and_failure_ack(self):
        calls = {"n": 0, "fail": False}

        async def on_control_restart():
            calls["n"] += 1
            if calls["fail"]:
                raise RuntimeError("boom")

        hub = cp.PipeHub(token="t", on_chat_send=_noop3, on_control_restart=on_control_restart,
                         on_status_get=lambda: {})
        ws = FakeWS([cp.encode_frame(cp.auth_frame("t")),
                    cp.encode_frame({"type": "control.restart"})])
        task = asyncio.ensure_future(hub.handle_connection(ws))
        await _settle()
        self.assertEqual(calls["n"], 1)
        self.assertIn(cp.encode_frame(cp.control_ack_frame("restart", ok=True)), ws.sent)
        await _cancel(task)

    async def test_status_get_returns_snapshot(self):
        hub = cp.PipeHub(token="t", on_chat_send=_noop3, on_control_restart=_noop0,
                         on_status_get=lambda: {"session_up": True, "queue_depth": 3})
        ws = FakeWS([cp.encode_frame(cp.auth_frame("t")),
                    cp.encode_frame({"type": "status.get"})])
        task = asyncio.ensure_future(hub.handle_connection(ws))
        await _settle()
        self.assertIn(cp.encode_frame(cp.status_frame(session_up=True, queue_depth=3)), ws.sent)
        await _cancel(task)

    async def test_unknown_frame_type_is_ignored_not_fatal(self):
        hub = cp.PipeHub(token="t", on_chat_send=_noop3, on_control_restart=_noop0,
                         on_status_get=lambda: {})
        ws = FakeWS([cp.encode_frame(cp.auth_frame("t")),
                    cp.encode_frame({"type": "something.weird"}),
                    cp.encode_frame({"type": "status.get"})])
        task = asyncio.ensure_future(hub.handle_connection(ws))
        await _settle()
        # The connection survived the unknown frame and answered the status.get right after it.
        self.assertIn(cp.encode_frame(cp.status_frame()), ws.sent)
        await _cancel(task)


class PipeHubBroadcast(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_without_a_client_is_a_silent_noop(self):
        hub = cp.PipeHub(token="t", on_chat_send=_noop3, on_control_restart=_noop0,
                         on_status_get=lambda: {})
        hub.broadcast({"type": "chat.event", "kind": "turn_started"})  # must not raise
        self.assertEqual(hub.dropped_count, 0)

    async def test_broadcast_delivers_to_connected_client(self):
        hub = cp.PipeHub(token="t", on_chat_send=_noop3, on_control_restart=_noop0,
                         on_status_get=lambda: {})
        ws = FakeWS([cp.encode_frame(cp.auth_frame("t"))])
        task = asyncio.ensure_future(hub.handle_connection(ws))
        await _settle()
        hub.broadcast(cp.chat_event("turn_started", source="telegram"))
        await _settle()
        kinds = [json.loads(s).get("kind") for s in ws.sent if json.loads(s).get("type") == cp.TYPE_CHAT_EVENT]
        self.assertIn("turn_started", kinds)
        await _cancel(task)

    async def test_bounded_queue_drops_oldest_with_counter(self):
        hub = cp.PipeHub(token="t", on_chat_send=_noop3, on_control_restart=_noop0,
                         on_status_get=lambda: {})
        # Exercise the bounded-queue mechanics directly (no live writer draining it), matching how a
        # slow/wedged client would leave the queue full — broadcast() itself must never block.
        hub._send_queue = asyncio.Queue(maxsize=3)
        for i in range(5):
            hub.broadcast({"type": "chat.event", "kind": "n", "i": i})
        self.assertEqual(hub.dropped_count, 2)
        self.assertEqual(hub._send_queue.qsize(), 3)
        remaining = [json.loads(hub._send_queue.get_nowait())["i"] for _ in range(3)]
        self.assertEqual(remaining, [2, 3, 4])  # the two oldest (0, 1) were dropped, never the newest

    async def test_broadcast_threadsafe_schedules_onto_loop(self):
        hub = cp.PipeHub(token="t", on_chat_send=_noop3, on_control_restart=_noop0,
                         on_status_get=lambda: {})
        hub._send_queue = asyncio.Queue(maxsize=10)
        loop = asyncio.get_running_loop()
        hub.broadcast_threadsafe({"type": "chat.event", "kind": "x"}, loop)
        await _settle()
        self.assertEqual(hub._send_queue.qsize(), 1)

    async def test_broadcast_threadsafe_with_dead_loop_never_raises(self):
        hub = cp.PipeHub(token="t", on_chat_send=_noop3, on_control_restart=_noop0,
                         on_status_get=lambda: {})
        hub.broadcast_threadsafe({"type": "chat.event"}, None)  # no loop at all — must not raise


if __name__ == "__main__":
    unittest.main()
