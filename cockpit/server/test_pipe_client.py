#!/usr/bin/env python3
"""Tests for cockpit/server/pipe_client.py — the outbound WebSocket client to the daemon's cockpit
pipe. Requires the `cockpit` uv extras group (websockets is a BASE project dependency, always synced —
see root pyproject.toml). SKIPPED, not errored, when it isn't installed, matching test_app.py's pattern
so a bare-machine run of the root stdlib suite never even looks here.

The connect/auth/relay flow is exercised against a fake `websockets` module (an async-context-manager
stand-in for `websockets.connect`) rather than a real socket or a live daemon — mirrors
seneschal/scripts/test_cockpit_pipe.py's duck-typed-fake approach on the daemon side.

Run:
  uv sync --extra cockpit --group test
  uv run python -m unittest discover -s cockpit/server -p "test_*.py"
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import websockets  # noqa: F401

    from cockpit.server import pipe_client as pc

    _WEBSOCKETS_AVAILABLE = True
except ImportError:
    _WEBSOCKETS_AVAILABLE = False


async def _noop(_frame):
    return None


@unittest.skipUnless(_WEBSOCKETS_AVAILABLE, "websockets not installed (uv sync --extra cockpit)")
class TokenAndAvailability(unittest.TestCase):
    def test_pipe_available_is_a_bool(self):
        self.assertIn(pc.pipe_available(), (True, False))

    def test_read_token_missing_file_is_none(self):
        self.assertIsNone(pc.read_token(Path(tempfile.mkdtemp()) / "nope"))

    def test_read_token_reads_and_strips(self):
        d = Path(tempfile.mkdtemp())
        p = d / "tok"
        p.write_text("  abc123  \n", encoding="utf-8")
        self.assertEqual(pc.read_token(p), "abc123")


@unittest.skipUnless(_WEBSOCKETS_AVAILABLE, "websockets not installed (uv sync --extra cockpit)")
class SendQueueBackpressure(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_queue_drops_oldest_with_counter(self):
        client = pc.PipeClient("127.0.0.1", 8471, Path("unused"), on_frame=_noop)
        client._queue = asyncio.Queue(maxsize=3)
        results = [client.send({"i": i}) for i in range(5)]
        self.assertEqual(results, [True, True, True, False, False])
        self.assertEqual(client.dropped_count, 2)
        self.assertEqual(client._queue.qsize(), 3)
        remaining = [client._queue.get_nowait()["i"] for _ in range(3)]
        self.assertEqual(remaining, [2, 3, 4])  # the two oldest were dropped, never the newest


if _WEBSOCKETS_AVAILABLE:
    class _FakeClientWS:
        """Duck-typed stand-in for a websockets client connection: one fixed auth-ack answer to the
        first `.recv()`, then an async-iterable of further inbound frames that hangs (simulating an
        open, idle connection) once exhausted — ended only by the caller cancelling/closing, never by
        this fake raising StopAsyncIteration."""

        def __init__(self, auth_ack: str, inbound_after_auth=None):
            self.auth_ack = auth_ack
            self.inbound = list(inbound_after_auth or [])
            self.sent: list[str] = []

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            return self.auth_ack

        def __aiter__(self):
            return self._aiter()

        async def _aiter(self):
            for msg in self.inbound:
                yield msg
            await asyncio.Event().wait()

    class _FakeConnCM:
        def __init__(self, ws):
            self.ws = ws

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, *exc):
            return False

    class _FakeWebsocketsModule:
        def __init__(self, ws):
            self._ws = ws
            self.urls: list[str] = []

        def connect(self, url, **_kwargs):
            self.urls.append(url)
            return _FakeConnCM(self._ws)


@unittest.skipUnless(_WEBSOCKETS_AVAILABLE, "websockets not installed (uv sync --extra cockpit)")
class PipeClientConnectFlow(unittest.IsolatedAsyncioTestCase):
    async def test_connects_authenticates_and_relays_frames(self):
        d = Path(tempfile.mkdtemp())
        token_path = d / "cockpit-pipe-token"
        token_path.write_text("secret", encoding="utf-8")

        status_event = {"type": "status", "session_up": True}
        ws = _FakeClientWS(auth_ack=json.dumps({"type": "auth.ok"}),
                          inbound_after_auth=[json.dumps(status_event)])
        fake_module = _FakeWebsocketsModule(ws)

        received: list[dict] = []

        async def on_frame(frame):
            received.append(frame)

        connect_calls = {"n": 0}

        def on_connect():
            connect_calls["n"] += 1

        orig = pc.websockets
        pc.websockets = fake_module
        try:
            client = pc.PipeClient("127.0.0.1", 8471, token_path, on_frame=on_frame,
                                   on_connect=on_connect)
            client.start()
            async with asyncio.timeout(5.0):
                while not client.connected:
                    await asyncio.sleep(0.01)
            async with asyncio.timeout(5.0):
                while not received:
                    await asyncio.sleep(0.01)
            self.assertEqual(received, [status_event])
            self.assertEqual(json.loads(ws.sent[0]), {"type": "auth", "token": "secret"})
            self.assertEqual(connect_calls["n"], 1)
            self.assertIn("ws://127.0.0.1:8471", fake_module.urls)

            client.send({"type": "chat.send", "id": "c1", "text": "hi"})
            async with asyncio.timeout(5.0):
                while len(ws.sent) < 2:
                    await asyncio.sleep(0.01)
            self.assertEqual(json.loads(ws.sent[1]), {"type": "chat.send", "id": "c1", "text": "hi"})

            await client.stop()
            self.assertFalse(client.connected)
        finally:
            pc.websockets = orig

    async def test_missing_token_never_attempts_to_connect(self):
        d = Path(tempfile.mkdtemp())
        token_path = d / "missing-token"
        ws = _FakeClientWS(auth_ack=json.dumps({"type": "auth.ok"}))
        fake_module = _FakeWebsocketsModule(ws)

        orig = pc.websockets
        pc.websockets = fake_module
        try:
            client = pc.PipeClient("127.0.0.1", 8471, token_path, on_frame=_noop)
            client.start()
            await asyncio.sleep(0.05)
            self.assertFalse(client.connected)
            self.assertEqual(fake_module.urls, [])
            await client.stop()
        finally:
            pc.websockets = orig

    async def test_auth_rejected_never_marks_connected(self):
        d = Path(tempfile.mkdtemp())
        token_path = d / "cockpit-pipe-token"
        token_path.write_text("secret", encoding="utf-8")
        ws = _FakeClientWS(auth_ack=json.dumps({"type": "auth.error", "reason": "bad token"}))
        fake_module = _FakeWebsocketsModule(ws)

        orig = pc.websockets
        pc.websockets = fake_module
        try:
            client = pc.PipeClient("127.0.0.1", 8471, token_path, on_frame=_noop)
            client.start()
            await asyncio.sleep(0.1)
            self.assertFalse(client.connected)
            self.assertIn("auth rejected", client.last_error or "")
            await client.stop()
        finally:
            pc.websockets = orig

    async def test_disconnect_callback_fires_on_stop(self):
        d = Path(tempfile.mkdtemp())
        token_path = d / "cockpit-pipe-token"
        token_path.write_text("secret", encoding="utf-8")
        ws = _FakeClientWS(auth_ack=json.dumps({"type": "auth.ok"}))
        fake_module = _FakeWebsocketsModule(ws)

        disconnects = {"n": 0}

        def on_disconnect():
            disconnects["n"] += 1

        orig = pc.websockets
        pc.websockets = fake_module
        try:
            client = pc.PipeClient("127.0.0.1", 8471, token_path, on_frame=_noop,
                                   on_disconnect=on_disconnect)
            client.start()
            async with asyncio.timeout(5.0):
                while not client.connected:
                    await asyncio.sleep(0.01)
            await client.stop()
            self.assertEqual(disconnects["n"], 1)
        finally:
            pc.websockets = orig


if __name__ == "__main__":
    unittest.main()
