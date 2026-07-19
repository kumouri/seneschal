#!/usr/bin/env python3
"""Tests for cockpit/server/ws_hub.py — the browser-facing fan-out hub for GET /api/ws. Pure asyncio,
no FastAPI/websockets dependency needed (duck-typed against a fake with just an async .send_json), so
this runs unconditionally (unlike test_pipe_client.py / test_app.py) even on a bare-machine run.

Run: python -m unittest cockpit.server.test_ws_hub  (or, from the repo root with cockpit importable)
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server.ws_hub import BrowserHub  # noqa: E402


class _FakeBrowserWS:
    def __init__(self, block=False):
        self.received: list[dict] = []
        self._gate = asyncio.Event()
        if not block:
            self._gate.set()

    async def send_json(self, data):
        await self._gate.wait()
        self.received.append(data)

    def release(self):
        self._gate.set()


class BrowserHubFanout(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_reaches_every_connected_client(self):
        hub = BrowserHub()
        ws1, ws2 = _FakeBrowserWS(), _FakeBrowserWS()
        hub.connect(ws1)
        hub.connect(ws2)
        hub.broadcast({"type": "status", "session_up": True})
        await asyncio.sleep(0.05)
        self.assertEqual(ws1.received, [{"type": "status", "session_up": True}])
        self.assertEqual(ws2.received, [{"type": "status", "session_up": True}])
        self.assertEqual(hub.client_count, 2)

    async def test_disconnect_stops_further_delivery(self):
        hub = BrowserHub()
        ws1 = _FakeBrowserWS()
        cid = hub.connect(ws1)
        hub.disconnect(cid)
        self.assertEqual(hub.client_count, 0)
        hub.broadcast({"type": "x"})
        await asyncio.sleep(0.05)
        self.assertEqual(ws1.received, [])

    async def test_a_slow_client_never_blocks_broadcast_or_other_clients(self):
        hub = BrowserHub()
        slow = _FakeBrowserWS(block=True)   # its send_json() never returns until released
        fast = _FakeBrowserWS()
        hub.connect(slow)
        hub.connect(fast)
        hub.broadcast({"type": "chat.event", "kind": "turn_started"})  # must return immediately
        await asyncio.sleep(0.05)
        self.assertEqual(fast.received, [{"type": "chat.event", "kind": "turn_started"}])
        self.assertEqual(slow.received, [])  # still stuck — but that never blocked `fast`
        slow.release()
        await asyncio.sleep(0.05)
        self.assertEqual(slow.received, [{"type": "chat.event", "kind": "turn_started"}])

    async def test_bounded_queue_drops_oldest_with_counter_for_a_wedged_client(self):
        hub = BrowserHub()
        wedged = _FakeBrowserWS(block=True)
        cid = hub.connect(wedged)
        # Shrink this client's queue to make the bound easy to exercise deterministically (its writer
        # is permanently stuck in send_json(), so nothing ever drains it during this test).
        ws, _queue, task = hub._clients[cid]
        hub._clients[cid] = (ws, asyncio.Queue(maxsize=3), task)
        for i in range(5):
            hub.broadcast({"i": i})
        self.assertEqual(hub.dropped_count, 2)
        _ws, queue, _task = hub._clients[cid]
        remaining = [queue.get_nowait()["i"] for _ in range(3)]
        self.assertEqual(remaining, [2, 3, 4])


if __name__ == "__main__":
    unittest.main()
