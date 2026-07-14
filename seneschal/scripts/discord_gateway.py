#!/usr/bin/env python3
"""Discord **gateway websocket** inbound for the presence daemon — push, not poll.

The gateway is a persistent connection with its own lifecycle (HELLO → IDENTIFY → heartbeats every
~41 s → MESSAGE_CREATE events → RESUME after a drop), which is exactly why the original stdlib-only
build used REST polling instead (see DISCORD_SETUP.md history). The asyncio daemon (phase 1) supplies
the concurrency, and the repo's one sanctioned dependency — ``websockets``, from the uv-managed venv —
supplies the client. Design: seneschal/docs/asyncio-daemon-design.md.

Layering rules:
  * This module is **import-safe without websockets** — every protocol/parsing function below is pure
    stdlib and unit-testable everywhere (CI py_compiles and tests it with no deps installed). Only
    ``run_gateway`` needs the real library; callers check ``gateway_available()`` first and fall back
    to REST polling when it's False (a degraded daemon beats a dead one).
  * It knows nothing about presence.py's DaemonState. The caller hands in primitives: a stop event,
    an async ``enqueue(messages)`` callback, an async ``catchup()`` callback (the REST ``after=``
    backfill), and ``log``.
  * **Catch-up on every (re)connect.** The gateway's resume window alone cannot guarantee "a restart
    never drops a message," so each (re)connect first runs the REST backfill against the stored
    snowflake offset — the same cursor this module advances as live events arrive, keeping the two
    paths coherent.
  * Same filters as discord_poll.py: watched channel only, no bots (no self-echo), author allowlist.

Credentials come from the same ``discord.env`` as the REST path (DISCORD_SETUP.md). Message Content
is a privileged intent — it must be toggled on in the Dev Portal or every message arrives empty
(close code 4014 means the intent toggle is missing entirely).
"""
from __future__ import annotations

import asyncio
import json
import os
import time

try:  # the one sanctioned dependency (pyproject.toml); absent on a stale interpreter → REST fallback
    import websockets
except ImportError:  # pragma: no cover - exercised via gateway_available() in tests
    websockets = None

from discord_poll import DEFAULT_OFFSET_FILE, load_env, read_offset, write_offset  # noqa: F401

GATEWAY_URL = "wss://gateway.discord.gg/"
GATEWAY_QUERY = "?v=10&encoding=json"

# GUILD_MESSAGES (1<<9) + DIRECT_MESSAGES (1<<12) + MESSAGE_CONTENT (1<<15, privileged — the Dev
# Portal toggle DISCORD_SETUP.md step 2 already requires for the REST path).
INTENTS = (1 << 9) | (1 << 12) | (1 << 15)

# Gateway opcodes we speak.
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Close codes that re-connecting will never fix — bail out to the REST fallback instead of hammering.
FATAL_CLOSE_CODES = {
    4004: "authentication failed (bad DISCORD_BOT_TOKEN)",
    4010: "invalid shard",
    4011: "sharding required",
    4012: "invalid API version",
    4013: "invalid intents",
    4014: "disallowed intents — enable Message Content Intent in the Dev Portal (Bot page)",
}

MAX_BACKOFF_SEC = 60.0


# --------------------------------------------------------------------- pure protocol helpers (no ws)

def build_identify(token: str) -> dict:
    return {"op": OP_IDENTIFY, "d": {
        "token": token,
        "intents": INTENTS,
        "properties": {"os": os.name, "browser": "seneschal-presence", "device": "seneschal-presence"},
    }}


def build_resume(token: str, session_id: str, seq) -> dict:
    return {"op": OP_RESUME, "d": {"token": token, "session_id": session_id, "seq": seq}}


def build_heartbeat(seq) -> dict:
    return {"op": OP_HEARTBEAT, "d": seq}


def next_backoff(prev: float) -> float:
    """Exponential, capped — a flapping gateway must not turn into a tight reconnect loop."""
    return min(max(prev, 1.0) * 2, MAX_BACKOFF_SEC)


class GatewaySession:
    """Resume bookkeeping across (re)connects: last dispatch seq, session id, and the resume URL the
    READY event designates. Survives connection objects; reset() drops back to a fresh IDENTIFY."""

    def __init__(self):
        self.seq = None
        self.session_id = None
        self.resume_url = None

    def can_resume(self) -> bool:
        return self.session_id is not None and self.seq is not None

    def connect_url(self) -> str:
        base = self.resume_url if (self.can_resume() and self.resume_url) else GATEWAY_URL
        return base.rstrip("/") + "/" + GATEWAY_QUERY if not base.endswith("/") else base + GATEWAY_QUERY

    def record(self, event: dict) -> None:
        """Track seq on every dispatch; capture session/resume-url from READY."""
        if event.get("s") is not None:
            self.seq = event["s"]
        if event.get("op") == OP_DISPATCH and event.get("t") == "READY":
            d = event.get("d") or {}
            self.session_id = d.get("session_id")
            self.resume_url = d.get("resume_gateway_url")

    def reset(self) -> None:
        self.seq = None
        self.session_id = None
        self.resume_url = None


def extract_message(event: dict, channel_id: str, allowed_user_ids: set) -> dict | None:
    """A MESSAGE_CREATE dispatch → the poll-shaped message dict, or None if filtered. Filters mirror
    discord_poll.py exactly: watched channel only, never bots (no self-echo loops), and — when an
    allowlist is configured — only listed author ids."""
    if event.get("op") != OP_DISPATCH or event.get("t") != "MESSAGE_CREATE":
        return None
    m = event.get("d") or {}
    if str(m.get("channel_id", "")) != str(channel_id):
        return None
    author = m.get("author", {}) or {}
    if author.get("bot"):
        return None
    author_id = str(author.get("id", ""))
    if allowed_user_ids and author_id not in allowed_user_ids:
        return None
    return {
        "id": m.get("id"),
        "channel_id": str(channel_id),
        "author": author.get("username") or author.get("global_name"),
        "author_id": author_id,
        "text": m.get("content", ""),
        "ts": m.get("timestamp"),
    }


def advance_offset(offset_file: str, message_id) -> None:
    """Move the shared snowflake cursor forward (never backward) so the REST catch-up path and the
    gateway agree on what's been seen. Snowflakes grow over time → numeric max."""
    if message_id is None:
        return
    try:
        current = read_offset(offset_file)
        if current is None or int(message_id) > int(current):
            write_offset(offset_file, str(message_id))
    except (TypeError, ValueError, OSError):
        pass  # a bad id/write must never kill the gateway; the catch-up poll self-heals the cursor


def gateway_available() -> bool:
    """True when the websockets library is importable (the uv venv is live)."""
    return websockets is not None


# ------------------------------------------------------------------------------- the resident task

async def run_gateway(env: dict, offset_file: str, stop, enqueue, catchup, log) -> bool:
    """Hold the gateway connection until ``stop`` is set. Returns False only for a fatal,
    reconnect-won't-fix condition (caller then falls back to REST polling for the daemon's lifetime).

    ``enqueue(messages)`` (async) receives poll-shaped message dicts; ``catchup()`` (async) runs the
    REST ``after=`` backfill and is awaited on EVERY (re)connect, before live events flow."""
    if not gateway_available():
        return False
    token = env.get("DISCORD_BOT_TOKEN")
    channel_id = env.get("DISCORD_CHANNEL_ID")
    if not token or not channel_id:
        log("! discord gateway: missing DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID")
        return False
    allowed = {x.strip() for x in (env.get("DISCORD_ALLOWED_USER_IDS", "") or "").split(",") if x.strip()}

    sess = GatewaySession()
    backoff = 1.0
    while not stop.is_set():
        try:
            # Backfill anything missed while disconnected BEFORE going live — the stored snowflake
            # cursor makes this exact, and it's what preserves "a restart never drops a message."
            await catchup()
            async with websockets.connect(
                    sess.connect_url(), ping_interval=None, max_size=2 ** 22) as ws:
                ok = await _session_loop(ws, sess, token, channel_id, allowed,
                                         offset_file, stop, enqueue, log)
                if not ok:
                    return False  # fatal — caller falls back to REST
            backoff = 1.0  # a session that got as far as running resets the penalty
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — network flap, DNS, TLS, abnormal close: retry
            code = getattr(getattr(e, "rcvd", None), "code", None)
            if code in FATAL_CLOSE_CODES:
                log(f"! discord gateway: fatal close {code}: {FATAL_CLOSE_CODES[code]} — REST fallback")
                return False
            if not stop.is_set():
                log(f"! discord gateway: connection lost ({type(e).__name__}: {e}) — "
                    f"reconnecting in {backoff:.0f}s")
                await _sleep_unless_stop(stop, backoff)
                backoff = next_backoff(backoff)
    return True


async def _session_loop(ws, sess: GatewaySession, token: str, channel_id: str, allowed: set,
                        offset_file: str, stop, enqueue, log) -> bool:
    """One connected session: HELLO → IDENTIFY/RESUME → heartbeats + dispatches until the connection
    drops (raises, caller reconnects), stop is set (returns True), or a fatal close (returns False)."""
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
    if hello.get("op") != OP_HELLO:
        raise RuntimeError(f"expected HELLO, got op {hello.get('op')}")
    interval = (hello.get("d") or {}).get("heartbeat_interval", 41250) / 1000.0

    if sess.can_resume():
        await ws.send(json.dumps(build_resume(token, sess.session_id, sess.seq)))
        log("• discord gateway: resuming session")
    else:
        await ws.send(json.dumps(build_identify(token)))
        log("• discord gateway: connected (identify)")

    last_ack = time.monotonic()
    next_beat = time.monotonic() + interval * 0.5  # first beat early (jitter per the docs' spirit)

    while not stop.is_set():
        timeout = max(0.1, min(next_beat - time.monotonic(), 5.0))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            raw = None

        now = time.monotonic()
        if raw is not None:
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            op = event.get("op")
            sess.record(event)
            if op == OP_HEARTBEAT_ACK:
                last_ack = now
            elif op == OP_HEARTBEAT:  # server asked for an immediate beat
                await ws.send(json.dumps(build_heartbeat(sess.seq)))
            elif op == OP_RECONNECT:
                log("• discord gateway: server requested reconnect (resume)")
                raise ConnectionResetError("gateway RECONNECT")
            elif op == OP_INVALID_SESSION:
                resumable = bool(event.get("d"))
                if not resumable:
                    sess.reset()
                log(f"• discord gateway: invalid session (resumable={resumable}) — reconnecting")
                await _sleep_unless_stop(stop, 2.0)
                raise ConnectionResetError("gateway INVALID_SESSION")
            elif op == OP_DISPATCH:
                msg = extract_message(event, channel_id, allowed)
                if msg is not None:
                    text = (msg.get("text") or "").strip()
                    advance_offset(offset_file, msg.get("id"))  # keep the REST cursor coherent
                    if text:
                        await enqueue([msg])

        if now >= next_beat:
            if now - last_ack > interval * 2:
                # Two beats with no ACK = zombie connection (half-open TCP). Force a reconnect.
                log("• discord gateway: heartbeat ACK overdue — reconnecting")
                raise ConnectionResetError("gateway heartbeat timeout")
            await ws.send(json.dumps(build_heartbeat(sess.seq)))
            next_beat = now + interval
    return True


async def _sleep_unless_stop(stop, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
