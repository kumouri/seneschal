"""Browser-facing fan-out hub for `GET /api/ws` — broadcasts frames relayed from the daemon pipe (or
synthesized locally, e.g. an honest "pipe is down" status) to every connected browser tab.

Each connection gets its own bounded outbound queue (drop-oldest + counter) so one slow tab can never
backpressure another tab, the daemon pipe, or a request handler — the same fail-open discipline as
seneschal/scripts/cockpit_pipe.py's PipeHub on the daemon side. `ws` is duck-typed (needs only an async
`.send_json(dict)`), matching both FastAPI's WebSocket and the test doubles in test_ws_hub.py.
"""
from __future__ import annotations

import asyncio
from typing import Any

SEND_QUEUE_MAXSIZE = 200


class BrowserHub:
    def __init__(self) -> None:
        self._clients: dict[int, tuple[Any, "asyncio.Queue[dict]", "asyncio.Task[None]"]] = {}
        self._next_id = 0
        self.dropped_count = 0

    def connect(self, ws: Any) -> int:
        """Register a newly-accepted browser connection; returns an id for later `disconnect`."""
        cid = self._next_id
        self._next_id += 1
        queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=SEND_QUEUE_MAXSIZE)
        task = asyncio.ensure_future(self._writer(ws, queue))
        self._clients[cid] = (ws, queue, task)
        return cid

    def disconnect(self, cid: int) -> None:
        entry = self._clients.pop(cid, None)
        if entry is not None:
            entry[2].cancel()

    def broadcast(self, frame: dict) -> None:
        """Best-effort, NEVER blocks: enqueue onto every connected client's bounded queue. A full queue
        (a stalled tab) drops the OLDEST frame and bumps `dropped_count` instead of blocking or growing
        unbounded — one dead tab must never affect the others."""
        for _, (_, queue, _) in list(self._clients.items()):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self.dropped_count += 1
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    pass

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def _writer(self, ws: Any, queue: "asyncio.Queue[dict]") -> None:
        try:
            while True:
                frame = await queue.get()
                await ws.send_json(frame)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — a dead/broken tab's writer must not affect anything else
            return
