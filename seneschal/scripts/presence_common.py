#!/usr/bin/env python3
"""Shared plumbing for the assistant's presence feed: the event-store schema, connect(), and the derived
current-context snapshot.

The phone (``PresenceSyncWorker``) POSTs discrete presence *events* — geofence enter/exit, activity
transitions, sleep/wake — as NDJSON to the desktop listener over the tailnet (see ``import_events`` in
``presence_import.py``). **Coordinates never leave the phone:** the wire carries only ``{place,
transition}``, an activity label, or a sleep state. Events land in ``state/presence.db``, and a tiny
derived snapshot ``state/presence-context.json`` answers "where / what / asleep is the owner *right now*" so the
daemon can read it cheaply every loop.

Two shapes, mirroring the health pipeline's split (and the same local-first, stdlib-only posture):

  * ``events`` (this DB)        — the **edge** log: "the owner just arrived / just woke". Consumed by
                                  ``presence_rules`` against a watermark, so a rule fires once per
                                  transition and survives a restart.
  * ``presence-context.json``   — the **level** snapshot: "the owner is home / driving / asleep now".
                                  Recomputed on every import; read by the suppression gate.

Times are naive-UTC ISO-8601 strings plus an integer ``tz_offset_min``, so any consumer renders local
without guessing (same contract as ``health_common``).
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

DEFAULT_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")
DEFAULT_DB = os.path.join(DEFAULT_STATE_DIR, "presence.db")
CONTEXT_NAME = "presence-context.json"

_ISO = "%Y-%m-%d %H:%M:%S"

#: Event kinds we accept on the wire. Unknown kinds are skipped, never fatal (forward-compat).
KINDS = ("geofence", "activity", "sleep")


def epoch_ms_to_utc(ms: int) -> datetime:
    """Epoch milliseconds → naive-UTC datetime (the phone sends absolute epoch-ms, unambiguous UTC)."""
    return datetime(1970, 1, 1) + timedelta(milliseconds=int(ms))


def iso(dt: datetime) -> str:
    """Naive-UTC datetime → ISO-8601 string (no offset; the row carries ``tz_offset_min`` separately)."""
    return dt.strftime(_ISO)


def now_iso() -> str:
    """Current UTC as an ISO-8601 string with a trailing ``Z``."""
    return datetime.now(timezone.utc).strftime(_ISO) + "Z"


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,   -- explicit uuid, else derived kind:label:transition:ts_ms
    kind          TEXT NOT NULL,      -- geofence | activity | sleep
    label         TEXT NOT NULL,      -- place name | activity name | sleep state (asleep|awake)
    transition    TEXT NOT NULL,      -- enter | exit
    ts_utc        TEXT NOT NULL,      -- naive-UTC ISO-8601
    ts_ms         INTEGER NOT NULL,   -- raw epoch ms (ordering + rule watermark)
    tz_offset_min INTEGER NOT NULL,
    confidence    INTEGER,            -- 0..100 where the phone provides it, else NULL
    ingested_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_ts      ON events(ts_ms);
CREATE INDEX IF NOT EXISTS ix_events_kind_ts ON events(kind, ts_ms);
"""


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Open (creating if needed) the presence event store and ensure the schema exists."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def context_path(state_dir: str = DEFAULT_STATE_DIR) -> str:
    return os.path.join(state_dir, CONTEXT_NAME)


def recompute_context(conn: sqlite3.Connection) -> dict:
    """Derive the current level-state snapshot from the event log.

    * ``at_place``  — the place of the most recent geofence event, iff it was an *enter* (else None).
    * ``activity``  — the label of the most recent activity *enter* (still / walking / in_vehicle / ...).
    * ``asleep``    — True iff the most recent sleep event is ``asleep``. **Informational only** since
                      2026-07-13: the reminder gate (``presence_rules``) no longer reads it — the
                      phone-only Sleep API flags an idle phone as a sleeping owner.

    "Most recent" is by ``ts_ms`` (event time), not arrival order — a batch that delivers an enter and a
    later exit together still resolves correctly.
    """
    def latest(kind: str, extra: str = "") -> sqlite3.Row | None:
        return conn.execute(
            f"SELECT label, transition, ts_utc, ts_ms FROM events WHERE kind=? {extra} "
            "ORDER BY ts_ms DESC LIMIT 1", (kind,)).fetchone()

    g = latest("geofence")
    a = latest("activity", "AND transition='enter'")
    s = latest("sleep")
    return {
        "at_place": g["label"] if g and g["transition"] == "enter" else None,
        "activity": a["label"] if a else None,
        "asleep": bool(s and s["label"] == "asleep"),
        "since": {
            "place": g["ts_utc"] if g else None,
            "activity": a["ts_utc"] if a else None,
            "sleep": s["ts_utc"] if s else None,
        },
        "updated_at": now_iso(),
    }


def write_context(ctx: dict, state_dir: str = DEFAULT_STATE_DIR) -> str:
    """Atomically write ``presence-context.json`` (temp + replace, so a reader never sees half a file)."""
    os.makedirs(state_dir, exist_ok=True)
    path = context_path(state_dir)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".presence-context.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ctx, fh, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def read_context(state_dir: str = DEFAULT_STATE_DIR) -> dict:
    """Read the current context snapshot, or a neutral one if it doesn't exist / is unreadable yet."""
    try:
        with open(context_path(state_dir), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"at_place": None, "activity": None, "asleep": False, "since": {}, "updated_at": None}


def prune_events(conn: sqlite3.Connection, keep_days: int = 30) -> int:
    """Delete events older than ``keep_days`` (retention — run nightly in Dream). Returns rows removed.
    The current context derives from the *latest* events, so pruning old history never changes it."""
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=keep_days)).timestamp() * 1000)
    n = conn.execute("DELETE FROM events WHERE ts_ms < ?", (cutoff_ms,)).rowcount
    conn.commit()
    return n
