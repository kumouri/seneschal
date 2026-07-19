#!/usr/bin/env python3
"""The seneschald cockpit pipe protocol — presence.py's SIXTH supervised task (cockpit-spec.md "The daemon
pipe"; design: seneschal/docs/asyncio-daemon-design.md). A localhost-only WebSocket server that lets the
cockpit backend (`cockpit/server/`) send chat into the SAME warm-session queue Telegram/Discord use,
and stream the warm session's turns back out as a live transcript.

Layering rules (mirrors discord_gateway.py's split):
  * This module is **import-safe without `websockets`** — every protocol/ring-buffer/token/inbox
    function below is pure stdlib and unit-testable everywhere (CI py_compiles and tests it with no
    deps installed). `PipeHub` itself needs no live socket either: it is duck-typed against whatever
    `ws` object it is handed (async `.recv()`/`.send()`/`.close()`), so its whole lifecycle — auth,
    single-client replace, frame handling, bounded-queue drop-with-counter — is unit-testable with a
    plain fake. Only `run_pipe_server` (which actually binds a listening socket) needs the real
    library; callers check `pipe_available()` first and degrade (no cockpit pipe) rather than dying —
    a hard requirement here, mirroring the Discord gateway's fallback discipline.
  * It knows nothing about presence.py's DaemonState. presence.py hands in three callbacks
    (`on_chat_send`, `on_control_restart`, `on_status_get`) and a `log` — this module never imports
    presence.py.
  * **Single-client discipline (spec).** The daemon serves exactly ONE authenticated pipe client — the
    cockpit backend, which multiplexes to N browser tabs on its own side. A newer authenticated
    connection replaces (closes) the older; there is no other client bookkeeping.
  * **Fail-open, always.** Every public entry point here that presence.py calls from its hot paths
    (`append_transcript_event`, `broadcast`/`broadcast_threadsafe`, `drain_inbox`) is safe to call
    from a try/except that swallows failures — a wedged/absent cockpit must never touch the chat loop
    or reminder firing. Bounded outbound queues + drop-with-counter (never block) is how `PipeHub`
    keeps a slow/dead client from backpressuring the daemon.

cockpit/server/ deliberately does NOT import this module (its own dependency world — cockpit-spec.md
ruling 3); it duplicates the small set of frame-type constants it needs. Keep the two in sync by hand
if the protocol changes.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
from datetime import datetime, timezone

try:  # a sanctioned daemon dependency (pyproject.toml); absent on a stale interpreter → no pipe
    import websockets
except ImportError:  # pragma: no cover - exercised via pipe_available() in tests
    websockets = None

# --------------------------------------------------------------------------------------- constants

DEFAULT_PORT = 8471  # localhost-only; override via presence.py's --cockpit-port / cockpit.env

PIPE_TOKEN_FILE = "cockpit-pipe-token"          # plain text, gitignored; auto-generated on first run
TRANSCRIPT_FILE = "warm-transcript.jsonl"        # ring buffer backing GET /api/transcript backfill
TRANSCRIPT_CAP = 2000                            # rewritten-tail cap (see _maybe_trim)
INBOX_FILE = "cockpit-inbox.jsonl"               # fallback queue when the pipe is down
INBOX_SEEN_FILE = "cockpit-inbox-seen.json"      # small dedupe ledger for the fallback drain
INBOX_SEEN_CAP = 500

# Frame `type` values (the wire protocol). cockpit/server duplicates these — keep both in sync.
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


def pipe_available() -> bool:
    """True when the websockets library is importable (the uv venv is live) — the daemon's cockpit
    pipe task checks this and degrades (no pipe) rather than dying when it's False."""
    return websockets is not None


# ------------------------------------------------------------------------------------- tiny json I/O
# Deliberately NOT imported from sentinel.py — this module stays a single, dependency-free file any
# consumer (presence.py, tests, a future direct pipe client) can read top-to-bottom.

def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------------------- pipe token

def pipe_token_path(state_dir: str) -> str:
    return os.path.join(state_dir, PIPE_TOKEN_FILE)


def read_pipe_token(state_dir: str) -> str | None:
    try:
        with open(pipe_token_path(state_dir), "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
            return tok or None
    except OSError:
        return None


def ensure_pipe_token(state_dir: str) -> str:
    """Read the daemon's pipe auth token, generating (and persisting) a fresh random one on first run.
    Plain text (not JSON) — the one artifact the daemon and any pipe client (cockpit/server, tests)
    must agree on byte-for-byte, so there's nothing to parse wrong."""
    tok = read_pipe_token(state_dir)
    if tok:
        return tok
    tok = secrets.token_hex(32)
    os.makedirs(state_dir, exist_ok=True)
    path = pipe_token_path(state_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(tok)
    os.replace(tmp, path)
    return tok


# ---------------------------------------------------------------------------------- frame protocol

def encode_frame(frame: dict) -> str:
    return json.dumps(frame, ensure_ascii=False)


def decode_frame(raw) -> dict | None:
    """Tolerant frame parse: anything that isn't a JSON object with a string `type` is discarded, never
    raised — a malformed frame from a flaky client must never crash the pipe."""
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        return None
    return obj


def auth_frame(token: str) -> dict:
    return {"type": TYPE_AUTH, "token": token}


def auth_ok_frame() -> dict:
    return {"type": TYPE_AUTH_OK}


def auth_error_frame(reason: str) -> dict:
    return {"type": TYPE_AUTH_ERROR, "reason": reason}


def chat_ack_frame(msg_id, **extra) -> dict:
    return {"type": TYPE_CHAT_ACK, "id": msg_id, **extra}


def control_ack_frame(action: str = "restart", **extra) -> dict:
    return {"type": TYPE_CONTROL_ACK, "action": action, **extra}


def status_frame(**fields) -> dict:
    return {"type": TYPE_STATUS, **fields}


def chat_event(kind: str, **fields) -> dict:
    """One digestible transcript event — a turn start/output/tool-use/turn-done marker. `kind` in use:
    turn_started | assistant_output | tool_use | turn_done. None-valued fields are dropped so frames
    stay small; ts is always stamped here so every producer gets it for free."""
    ev = {"type": TYPE_CHAT_EVENT, "kind": kind,
          "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    for k, v in fields.items():
        if v is not None:
            ev[k] = v
    return ev


def _preview(value, cap: int = 200) -> str:
    try:
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(value)
    s = s.strip()
    return s if len(s) <= cap else s[:cap].rstrip() + "…"


def build_chat_event_from_stream(ev: dict, source: str, model: str | None = None):
    """Convert one parsed claude-CLI stream-json line (the warm session's stdout) into a digestible
    chat.event, or None to skip it entirely — deliberately conservative: unknown/uninteresting event
    types are dropped rather than guessed at, so this never ships a firehose of raw tokens. Pure and
    side-effect-free, so it's unit-testable with plain dicts (no live `claude` process needed).

    `assistant` events (one per content block the CLI emits) become `assistant_output` (text) and/or
    carry `tool_uses` (name + a short input preview) when the block is a tool call. The terminal
    `result` event becomes `turn_done`, carrying whatever usage/cost/duration fields the CLI supplied
    (all optional — this only forwards what's present, never invents fields)."""
    if not isinstance(ev, dict):
        return None
    t = ev.get("type")
    if t == "assistant":
        blocks = (ev.get("message") or {}).get("content")
        if not isinstance(blocks, list):
            return None
        text_parts, tools = [], []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text"):
                text_parts.append(str(b["text"]))
            elif b.get("type") == "tool_use":
                tools.append({"name": b.get("name"), "input_preview": _preview(b.get("input"))})
        if not text_parts and not tools:
            return None
        return chat_event("assistant_output", source=source, model=model,
                          text=_preview("\n".join(text_parts), 4000) if text_parts else None,
                          tool_uses=tools or None)
    if t == "result":
        return chat_event("turn_done", source=source, model=model,
                          is_error=bool(ev.get("is_error")),
                          reply_preview=_preview(ev.get("result") or "", 400) or None,
                          duration_ms=ev.get("duration_ms"), num_turns=ev.get("num_turns"),
                          total_cost_usd=ev.get("total_cost_usd"), usage=ev.get("usage"))
    return None


# --------------------------------------------------------------------------------- transcript ring buffer

_transcript_lock = threading.Lock()  # append can come from a worker thread (the warm session's stdout
                                     # reader, via broadcast_threadsafe's sibling call site in
                                     # presence.py) as well as the event loop; guards interleaved writes.


def transcript_path(state_dir: str) -> str:
    return os.path.join(state_dir, TRANSCRIPT_FILE)


def _maybe_trim(path: str, slack: int = 200) -> None:
    """Rewrite the file to its last TRANSCRIPT_CAP lines once it has grown TRANSCRIPT_CAP + slack past
    the cap — amortizes the rewrite cost instead of paying it on every single append."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    if len(lines) <= TRANSCRIPT_CAP + slack:
        return
    tail = lines[-TRANSCRIPT_CAP:]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(tail)
    os.replace(tmp, path)


def append_transcript_event(state_dir: str, event: dict) -> None:
    """Append one digestible event to the ring buffer. Callers (presence.py) wrap this in a broad
    try/except — a tee failure (disk full, bad permissions) must never break a chat turn."""
    path = transcript_path(state_dir)
    os.makedirs(state_dir, exist_ok=True)
    with _transcript_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        _maybe_trim(path)


def read_transcript_tail(state_dir: str, limit: int = 200) -> list:
    """Tolerant tail read for backfill: a corrupt line is skipped and doesn't count against the limit
    (every line is parsed first, then the last N valid ones are kept — mirrors cockpit's own
    read_oneiroi pattern). Oldest-first (chronological), newest last."""
    limit = max(1, min(int(limit or 200), TRANSCRIPT_CAP))
    try:
        with open(transcript_path(state_dir), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-limit:]


# -------------------------------------------------------------------------------- inbox fallback queue

_inbox_lock = threading.Lock()


def inbox_path(state_dir: str) -> str:
    return os.path.join(state_dir, INBOX_FILE)


def append_inbox(state_dir: str, item: dict) -> None:
    """The cockpit backend's fallback write when the pipe is down: one {"id","text","ts"} JSON line.
    (cockpit/server duplicates this tiny function rather than importing it — ruling 3.) The daemon's
    scheduler tick drains it (see drain_inbox) into the same chat queue as Telegram/Discord — degraded
    to ~5s latency, never lossy."""
    os.makedirs(state_dir, exist_ok=True)
    with _inbox_lock:
        with open(inbox_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def _load_seen_ids(state_dir: str) -> list:
    data = _load_json(os.path.join(state_dir, INBOX_SEEN_FILE), None)
    ids = data.get("ids") if isinstance(data, dict) else None
    return list(ids) if isinstance(ids, list) else []


def _save_seen_ids(state_dir: str, ids: list) -> None:
    _save_json(os.path.join(state_dir, INBOX_SEEN_FILE), {"ids": ids[-INBOX_SEEN_CAP:]})


def drain_inbox(state_dir: str) -> list:
    """Read + clear the fallback inbox file, deduping by `id` against a small persisted seen-ledger (a
    retried backend write while the daemon happened to be mid-drain must not double-enqueue). Tolerant
    of corrupt lines and a missing file. Returns fresh {"id","text","ts"} items, file order (oldest
    first) — callers enqueue them exactly like a Telegram/Discord batch."""
    path = inbox_path(state_dir)
    with _inbox_lock:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return []
        try:
            with open(path, "w", encoding="utf-8"):
                pass  # truncate now that we've read everything there is — never re-read what we return
        except OSError:
            pass
    items = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and (obj.get("text") or "").strip():
            items.append(obj)
    if not items:
        return []
    seen = _load_seen_ids(state_dir)
    seen_set = set(seen)
    fresh = []
    changed = False
    for it in items:
        iid = str(it.get("id") or "").strip()
        if iid and iid in seen_set:
            continue
        fresh.append(it)
        if iid:
            seen.append(iid)
            seen_set.add(iid)
            changed = True
    if changed:
        _save_seen_ids(state_dir, seen)
    return fresh


# ------------------------------------------------------------------------------------------ PipeHub

class PipeHub:
    """The daemon's single-pipe-client connection manager (cockpit-spec.md "Fan-out discipline").

    Duck-typed against `ws` — needs only an async `.recv()`, async `.send(str)`, and async
    `.close(code=, reason=)` — so its entire lifecycle (auth, single-client replace, frame handling,
    bounded-queue drop-with-counter) is unit-testable with a plain fake, no real `websockets` socket
    required. Only the caller that actually binds a listening socket (`run_pipe_server`, presence.py's
    cockpit_task) needs the real dependency.

    A newer authenticated connection always replaces (closes) whatever was connected before — the ONE
    client is the cockpit backend, which fans out to N browser tabs on its own side; this hub never
    grows bookkeeping beyond "the current client."
    """

    SEND_QUEUE_MAXSIZE = 200
    AUTH_TIMEOUT_SEC = 10.0

    def __init__(self, token: str, on_chat_send, on_control_restart, on_status_get, log=None):
        self.token = token
        self.on_chat_send = on_chat_send              # async (msg_id, text, frame) -> None
        self.on_control_restart = on_control_restart    # async () -> None
        self.on_status_get = on_status_get              # () -> dict (sync; keep it cheap)
        self.log = log or (lambda *_: None)
        self.client = None
        self._send_queue: asyncio.Queue | None = None
        self._writer_task = None
        self.dropped_count = 0

    async def handle_connection(self, ws) -> None:
        """The full lifecycle of one incoming socket: authenticate-or-close, adopt as the sole active
        client, then read frames until it disconnects. Never raises past here — a single bad connection
        must not take the pipe (or the daemon) down with it."""
        try:
            raw = await asyncio.wait_for(self._recv(ws), timeout=self.AUTH_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            await self._safe_close(ws, 4001, "auth timeout")
            return
        frame = decode_frame(raw)
        if not frame or frame.get("type") != TYPE_AUTH or frame.get("token") != self.token:
            await self._safe_send_now(ws, encode_frame(auth_error_frame("unauthorized")))
            await self._safe_close(ws, 4001, "unauthorized")
            return
        await self._adopt(ws)
        if not await self._safe_send_now(ws, encode_frame(auth_ok_frame())):
            await self._release(ws)
            return
        try:
            while True:
                raw = await self._recv(ws)
                if raw is None:
                    break
                frame = decode_frame(raw)
                if frame is not None:
                    await self._handle_frame(ws, frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a dropped/broken connection must not crash the pipe
            self.log(f"! cockpit pipe: connection error: {e}")
        finally:
            await self._release(ws)

    @staticmethod
    async def _recv(ws):
        """One inbound message, or None at end-of-stream/on error. A single seam so a test fake can be
        a plain asyncio.Queue-backed stub instead of a real socket."""
        try:
            return await ws.recv()
        except Exception:
            return None

    async def _handle_frame(self, ws, frame: dict) -> None:
        t = frame.get("type")
        if t == TYPE_CHAT_SEND:
            text = frame.get("text")
            msg_id = frame.get("id")
            if isinstance(text, str) and text.strip():
                try:
                    await self.on_chat_send(msg_id, text, frame)
                except Exception as e:  # noqa: BLE001 — a bad enqueue must not drop the connection
                    self.log(f"! cockpit chat.send handling failed: {e}")
            await self._safe_send_now(ws, encode_frame(chat_ack_frame(msg_id)))
        elif t == TYPE_CONTROL_RESTART:
            ok = True
            try:
                await self.on_control_restart()
            except Exception as e:  # noqa: BLE001
                self.log(f"! cockpit control.restart failed: {e}")
                ok = False
            await self._safe_send_now(ws, encode_frame(control_ack_frame("restart", ok=ok)))
        elif t == TYPE_STATUS_GET:
            try:
                snap = self.on_status_get() or {}
            except Exception as e:  # noqa: BLE001
                self.log(f"! cockpit status.get failed: {e}")
                snap = {}
            await self._safe_send_now(ws, encode_frame(status_frame(**snap)))
        else:
            self.log(f"! cockpit pipe: unknown frame type {t!r} — ignored")

    async def _adopt(self, ws) -> None:
        old = self.client
        self.client = ws
        self._send_queue = asyncio.Queue(maxsize=self.SEND_QUEUE_MAXSIZE)
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()
        self._writer_task = asyncio.ensure_future(self._writer_loop(ws, self._send_queue))
        if old is not None and old is not ws:
            await self._safe_close(old, 4000, "replaced by newer cockpit connection")

    async def _release(self, ws) -> None:
        if self.client is ws:
            self.client = None
            if self._writer_task is not None:
                self._writer_task.cancel()
            self._writer_task = None
            self._send_queue = None
        await self._safe_close(ws, 1000, "bye")

    async def _writer_loop(self, ws, queue: asyncio.Queue) -> None:
        try:
            while True:
                raw = await queue.get()
                if not await self._safe_send_now(ws, raw):
                    return  # connection's gone — handle_connection's finally cleans the rest up
        except asyncio.CancelledError:
            return

    async def _safe_send_now(self, ws, raw: str) -> bool:
        try:
            await asyncio.wait_for(ws.send(raw), timeout=5.0)
            return True
        except Exception:
            return False

    @staticmethod
    async def _safe_close(ws, code: int, reason: str) -> None:
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            pass

    def broadcast(self, frame: dict) -> None:
        """Best-effort, NEVER blocks the caller: enqueue onto the bounded outbound queue for the writer
        loop to actually send. A full queue (slow/wedged client) drops the OLDEST entry and bumps
        `dropped_count` instead of growing unbounded or blocking — the fail-open contract every
        cockpit-pipe failure mode must honor. A no-op (frame silently discarded) when nobody's
        connected — the ring buffer is what backfills a reconnect, not this queue."""
        q = self._send_queue
        if q is None:
            return
        raw = encode_frame(frame)
        try:
            q.put_nowait(raw)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dropped_count += 1
            try:
                q.put_nowait(raw)
            except asyncio.QueueFull:
                pass

    def broadcast_threadsafe(self, frame: dict, loop) -> None:
        """Cross-thread-safe variant for callers running inside asyncio.to_thread (the warm session's
        blocking stdout reader lives on a worker thread) — schedules `broadcast` back onto the event
        loop instead of touching asyncio primitives from the wrong thread. Silently swallows a dead/
        closing loop — this is a tee, never a required delivery path."""
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self.broadcast, frame)
        except Exception:  # noqa: BLE001
            pass


async def run_pipe_server(hub: PipeHub, host: str, port: int, stop, log) -> None:
    """Bind the localhost-only pipe server and hold it until `stop` is set. Guarded by
    pipe_available(): if the websockets dependency is missing (a stale venv that predates `uv sync`),
    this logs loudly and returns immediately — the daemon degrades (no cockpit pipe this run), never
    dies, mirroring discord_task's REST-fallback discipline."""
    if not pipe_available():
        log("! cockpit pipe: websockets unavailable — pipe disabled (uv sync needed)")
        return
    try:
        async with websockets.serve(hub.handle_connection, host, port):
            log(f"• cockpit pipe listening on {host}:{port}")
            await stop.wait()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — a bind failure (port in use, etc.) must not kill the daemon
        log(f"! cockpit pipe server error: {e}")
