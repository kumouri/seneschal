"""Outbound WebSocket client: the cockpit backend -> the daemon's cockpit pipe
(seneschal/scripts/cockpit_pipe.py; seneschal/docs/cockpit-spec.md "The daemon pipe").

Its own dependency world (cockpit-spec.md ruling 3) — this module duplicates the small set of
frame-type constants and the plain-text token-file convention it needs rather than importing
seneschal/scripts. Keep both copies in sync by hand if the wire protocol changes.

The daemon serves exactly ONE pipe client; this IS that client. `PipeClient` holds the connection,
reconnects with backoff, and relays every frame the daemon sends to `on_frame`. Sending back to the
daemon (`send()`) is a synchronous, NEVER-blocking enqueue onto a bounded outbound queue — a stalled or
absent daemon connection must never backpressure a request handler; the queue drops the oldest frame
(with a counter) when full, mirroring the daemon-side PipeHub's own fail-open discipline.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable, Optional

try:  # optional at import time — pipe_available() gates whether PipeClient can ever actually connect
    import websockets
except ImportError:  # pragma: no cover - exercised via pipe_available() in tests
    websockets = None

# Frame `type` values — duplicated from seneschal/scripts/cockpit_pipe.py (ruling 3: no cross-import).
TYPE_AUTH = "auth"
TYPE_AUTH_OK = "auth.ok"
TYPE_AUTH_ERROR = "auth.error"
TYPE_CHAT_SEND = "chat.send"
TYPE_CHAT_ACK = "chat.ack"
TYPE_CHAT_EVENT = "chat.event"
TYPE_STATUS = "status"
TYPE_STATUS_GET = "status.get"
TYPE_CONTROL_RESTART = "control.restart"
TYPE_CONTROL_ACK = "control.ack"

DEFAULT_PORT = 8471
MAX_BACKOFF_SEC = 30.0
SEND_QUEUE_MAXSIZE = 200


def pipe_available() -> bool:
    """True when the websockets library is importable. If False, PipeClient.start() is a permanent
    no-op (never crashes) — the backend just reports the pipe as perpetually down and every write
    falls back to the inbox file queue."""
    return websockets is not None


def read_token(token_path: Path) -> Optional[str]:
    """The daemon's pipe auth token — a plain text file (cockpit_pipe.ensure_pipe_token writes it).
    None if it doesn't exist yet (the daemon hasn't started, or hasn't been restarted since this PR)."""
    try:
        tok = token_path.read_text(encoding="utf-8").strip()
        return tok or None
    except OSError:
        return None


class PipeClient:
    """Holds (and reconnects) the ONE outbound connection to the daemon's cockpit pipe.

    `on_frame(frame)` (async) is awaited for every frame the daemon sends. `on_connect`/`on_disconnect`
    (sync, optional) fire on each transition so the caller can flip its own "pipe up/down" bookkeeping
    (e.g. broadcasting an honest status frame to browser tabs) without polling `.connected`.
    """

    def __init__(self, host: str, port: int, token_path: Path,
                on_frame: Callable[[dict], Awaitable[None]],
                on_connect: Optional[Callable[[], None]] = None,
                on_disconnect: Optional[Callable[[], None]] = None):
        self.host = host
        self.port = port
        self.token_path = token_path
        self.on_frame = on_frame
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=SEND_QUEUE_MAXSIZE)
        self._connected = False
        self._stop_event: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self.dropped_count = 0
        self.last_error: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        """Kick off the reconnect loop as a background task. A no-op if already started, or if
        `websockets` isn't importable (logged via `last_error`, never raised)."""
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        if not pipe_available():
            self.last_error = "websockets not installed"
            return
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._connected = False

    def send(self, frame: dict) -> bool:
        """Best-effort, NEVER blocks: enqueue for the writer to actually send. A full queue (daemon
        connection wedged/absent) drops the OLDEST entry and bumps `dropped_count` rather than growing
        unbounded or blocking the caller (an HTTP/WS request handler)."""
        try:
            self._queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dropped_count += 1
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass
            return False

    async def _run(self) -> None:
        backoff = 1.0
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            token = read_token(self.token_path)
            if not token:
                self.last_error = "no pipe token file yet (daemon not started?)"
                await self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SEC)
                continue
            try:
                async with websockets.connect(
                        f"ws://{self.host}:{self.port}", open_timeout=5) as ws:
                    await ws.send(json.dumps({"type": TYPE_AUTH, "token": token}))
                    ack_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    ack = json.loads(ack_raw)
                    if ack.get("type") != TYPE_AUTH_OK:
                        self.last_error = f"auth rejected: {ack}"
                        await self._sleep(backoff)
                        backoff = min(backoff * 2, MAX_BACKOFF_SEC)
                        continue
                    self._connected = True
                    self.last_error = None
                    backoff = 1.0
                    if self.on_connect:
                        self.on_connect()
                    writer = asyncio.ensure_future(self._writer(ws))
                    try:
                        async for raw in ws:
                            try:
                                frame = json.loads(raw)
                            except (ValueError, TypeError):
                                continue
                            if not isinstance(frame, dict):
                                continue
                            try:
                                await self.on_frame(frame)
                            except Exception as e:  # noqa: BLE001 — a bad handler must not drop the pipe
                                self.last_error = f"on_frame handler error: {e}"
                    finally:
                        writer.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — any connect/network failure: log + backoff + retry
                self.last_error = str(e)
            finally:
                if self._connected:
                    self._connected = False
                    if self.on_disconnect:
                        self.on_disconnect()
            if not self._stop_event.is_set():
                await self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SEC)

    async def _writer(self, ws) -> None:
        try:
            while True:
                frame = await self._queue.get()
                await ws.send(json.dumps(frame))
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            self.last_error = f"writer error: {e}"

    async def _sleep(self, seconds: float) -> None:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
