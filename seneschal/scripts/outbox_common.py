#!/usr/bin/env python3
"""Durable **write-behind outbox** for the assistant's act-low Notion writes — the sqlite store + its
primitives.

**Why this exists.** The assistant tells the owner something is *done / logged / marked off*. That's only
true if the underlying Notion write actually landed — and today it can silently not: a chat ack
(``Status = Done`` on the ⏰ row) is written by the warm session calling ``notion-update-page``, and if
that call errors (Notion unreachable, a ``collection_router_upstream_429``, the session winding down, the
box rebooting mid-turn) the write is **lost**. A live ops-watch once caught four such acks that reached
``state/acks.json`` but never flipped Notion — the **system of record** silently desynced.

**Backend scope: this is a Notion-backend component.** The filesystem store backends (obsidian /
markdown) write locally and atomically, so their act-low writes never route through here — the direct
path is their durability story. See the spec's "Backend scope" section.

This module is the fix's durable core: an act-low write is **journaled to this local store first** (it
survives a reboot), **then flushed to Notion** — idempotently, in order, retried until it lands. The
flush itself is done by the warm/scheduled LLM turn via the MCP tools (option (a) — see
``../docs/notion-write-behind-outbox-spec.md``); this module never talks to Notion, it only holds and
sequences the work.

**Doctrine (mirrors ``reminders_acks``):** the store is the durable fact; enqueue is idempotent (a repeat
is a silent no-op via the ``UNIQUE`` key); the drainer is single-consumer + FIFO so per-target order is
free; a transient failure backs off, a permanent one or an exhausted retry budget **dead-letters** (never
wedges the queue). Unlike the ack *ledger* (which is fail-**open** — a broken ledger fires the nudge), the
outbox is fail-**closed**: an entry keeps retrying until Notion confirms.

Stdlib only (``sqlite3``). WAL mode + a busy timeout let the three accessors — the enqueuing LLM turn, the
drain loop, the status CLI — share the file without the hand-rolled lock ``acks.json`` needs.

Schema, idempotency, retry, and the (a)/(b) fork: ``../docs/notion-write-behind-outbox-spec.md``.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
DB_FILE = "notion-outbox.sqlite"

# --- status values (the entry lifecycle) ---
PENDING = "pending"      # eligible to flush (once not_before has passed)
INFLIGHT = "inflight"    # claimed by a drainer; reclaimed to PENDING if the drainer dies (stale)
DONE = "done"            # Notion confirmed; pruned after a retention window
FAILED = "failed"        # dead-letter — retries exhausted or a permanent error; surfaced, never auto-dropped
STATUSES = (PENDING, INFLIGHT, DONE, FAILED)

# --- the closed set of write intents this outbox carries (§4 of the spec) ---
OPS = ("ack_reminder", "med_log", "run_log_finalize", "reminder_status")
TARGET_KINDS = ("page", "db")  # 'page' = update an existing row; 'db' = create a row in a collection

# --- retry policy (§6 of the spec; overridable per call for tests) ---
MAX_ATTEMPTS = 8            # attempts before dead-letter (~a few hours of backoff)
BACKOFF_BASE_SEC = 5.0      # first-failure base delay
BACKOFF_CAP_SEC = 3600.0    # never wait more than an hour between tries
STALE_INFLIGHT_SEC = 300.0  # a claim older than this is assumed dead → reclaimed to PENDING
ERROR_MAX_CHARS = 500       # last_error is trimmed to keep the row small


# --------------------------------------------------------------------------- time helpers
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Serialize an instant to the store's canonical UTC form (``YYYY-MM-DDTHH:MM:SSZ``)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    """Parse the store's canonical UTC form back to an aware datetime. Tolerant of a trailing ``Z`` or a
    ``+00:00`` offset so hand-written/backfilled values still load."""
    if not isinstance(s, str):
        raise ValueError(f"not an ISO string: {s!r}")
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _trim(err) -> str | None:
    if err is None:
        return None
    s = str(err)
    return s[:ERROR_MAX_CHARS]


# --------------------------------------------------------------------------- key helpers
def _norm(key) -> str:
    """Normalize a Notion id/key for identity: lowercase, drop dashes/whitespace — so a page id matches
    with or without dashes. Mirrors ``reminders_acks.norm_key`` so the outbox and the ack ledger agree."""
    if not isinstance(key, str):
        return ""
    return "".join(key.split()).replace("-", "").lower()


def ack_key(reminder_id: str, local_date: str) -> str:
    """Idempotency key for a reminder ack: one ack per ⏰ row per local day → a repeat is a no-op."""
    return f"ack:{_norm(reminder_id)}:{local_date}"


def reminder_status_key(reminder_id: str, local_date: str) -> str:
    """Idempotency key for a non-ack ⏰ status flip (same converging-update shape as an ack)."""
    return f"rstatus:{_norm(reminder_id)}:{local_date}"


def runlog_final_key(run_log_id: str) -> str:
    """Idempotency key for finalizing a 🧭 Run Log row (an update by id — naturally idempotent)."""
    return f"runlog-final:{_norm(run_log_id)}"


def medlog_key(intent: str) -> str:
    """Idempotency key for a med-intake row (a *create*). ``intent`` must be unique per intended row —
    the caller mints a uuid per dose — so the key dedups replays of the *same* enqueue, not distinct
    doses. See the create-idempotency discussion (§6 of the spec)."""
    return f"medlog:{intent}"


def new_intent() -> str:
    """A fresh unique token for a create's idempotency key (one per intended row)."""
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- store
def db_path(state_dir: str) -> str:
    return os.path.join(state_dir, DB_FILE)


def connect(state_dir: str = DEFAULT_STATE_DIR) -> sqlite3.Connection:
    """Open (creating + migrating idempotently) the outbox DB. WAL + a busy timeout make the three
    accessors coexist without a lockfile. Safe to call repeatedly."""
    os.makedirs(state_dir, exist_ok=True)
    conn = sqlite3.connect(db_path(state_dir), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbox (
            id               TEXT PRIMARY KEY,
            idempotency_key  TEXT NOT NULL UNIQUE,
            op               TEXT NOT NULL,
            target_kind      TEXT NOT NULL,
            target_id        TEXT NOT NULL,
            payload          TEXT NOT NULL,
            status           TEXT NOT NULL,
            attempts         INTEGER NOT NULL DEFAULT 0,
            not_before       TEXT,
            created_at       TEXT NOT NULL,
            last_attempt_at  TEXT,
            last_error       TEXT,
            notion_page_id   TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(status, not_before)")
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["payload"] = json.loads(d["payload"])
    except (TypeError, ValueError, json.JSONDecodeError):
        pass  # leave the raw text if it somehow isn't JSON — never blow up a read
    return d


def get(conn: sqlite3.Connection, entry_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM outbox WHERE id=?", (entry_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_by_key(conn: sqlite3.Connection, idempotency_key: str) -> dict | None:
    row = conn.execute("SELECT * FROM outbox WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    return _row_to_dict(row) if row else None


def enqueue(conn: sqlite3.Connection, op: str, target_kind: str, target_id: str, payload: dict,
            idempotency_key: str, now: datetime | None = None) -> dict:
    """Journal a write intent. **This is the durability guarantee** — once it returns ``ok``, the write
    *will* be flushed (idempotently, retried) even across a reboot.

    Idempotent by ``idempotency_key``:
      * key absent           → insert a fresh PENDING entry (``created: True``).
      * key present, live     → no-op; return the existing entry (``created: False``).
      * key present, DONE     → no-op; return it (already landed — nothing to do).
      * key present, FAILED   → **revive** it (reset to PENDING, attempts 0, clear backoff/error) so a
                                fresh intent re-arms a dead-letter (``revived: True``).

    Raises ``ValueError`` on an unknown ``op``/``target_kind`` — a typo must fail loudly, not queue junk.
    """
    if op not in OPS:
        raise ValueError(f"unknown op {op!r}; expected one of {OPS}")
    if target_kind not in TARGET_KINDS:
        raise ValueError(f"unknown target_kind {target_kind!r}; expected one of {TARGET_KINDS}")
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    now = now or _utcnow()
    payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)

    existing = conn.execute(
        "SELECT id, status FROM outbox WHERE idempotency_key=?", (idempotency_key,)
    ).fetchone()
    if existing is not None:
        if existing["status"] == FAILED:
            conn.execute(
                "UPDATE outbox SET status=?, attempts=0, not_before=NULL, last_error=NULL, "
                "payload=? WHERE id=?",
                (PENDING, payload_json, existing["id"]),
            )
            conn.commit()
            return {"ok": True, "id": existing["id"], "created": False, "revived": True,
                    "idempotency_key": idempotency_key}
        return {"ok": True, "id": existing["id"], "created": False, "revived": False,
                "idempotency_key": idempotency_key}

    entry_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO outbox (id, idempotency_key, op, target_kind, target_id, payload, status, "
        "attempts, created_at) VALUES (?,?,?,?,?,?,?,0,?)",
        (entry_id, idempotency_key, op, target_kind, target_id, payload_json, PENDING, iso(now)),
    )
    conn.commit()
    return {"ok": True, "id": entry_id, "created": True, "revived": False,
            "idempotency_key": idempotency_key}


def reclaim_stale(conn: sqlite3.Connection, now: datetime | None = None,
                  stale_sec: float = STALE_INFLIGHT_SEC) -> int:
    """Return INFLIGHT entries whose claim is older than ``stale_sec`` back to PENDING — a drainer that
    died mid-flush must not strand its claim forever. Returns how many were reclaimed."""
    now = now or _utcnow()
    cutoff = iso(now - timedelta(seconds=stale_sec))
    cur = conn.execute(
        "UPDATE outbox SET status=? WHERE status=? AND (last_attempt_at IS NULL OR last_attempt_at < ?)",
        (PENDING, INFLIGHT, cutoff),
    )
    conn.commit()
    return cur.rowcount


def claim_ready(conn: sqlite3.Connection, now: datetime | None = None, limit: int = 50,
                stale_sec: float = STALE_INFLIGHT_SEC) -> list[dict]:
    """Atomically claim up to ``limit`` flushable entries for a drainer, oldest first (FIFO → per-target
    order for free). Each claim flips PENDING→INFLIGHT, **increments ``attempts`` and stamps
    ``last_attempt_at`` before the risky Notion write** (poison-pill safety — a write that keeps killing
    the turn burns its budget and dead-letters). Stale INFLIGHT entries are reclaimed first. The
    ``WHERE status=PENDING`` guard on the claim makes two concurrent drainers safe — only one wins each
    row."""
    now = now or _utcnow()
    reclaim_stale(conn, now, stale_sec)
    now_s = iso(now)
    rows = conn.execute(
        "SELECT id FROM outbox WHERE status=? AND (not_before IS NULL OR not_before<=?) "
        "ORDER BY created_at, id LIMIT ?",
        (PENDING, now_s, limit),
    ).fetchall()
    claimed = []
    for r in rows:
        cur = conn.execute(
            "UPDATE outbox SET status=?, attempts=attempts+1, last_attempt_at=? WHERE id=? AND status=?",
            (INFLIGHT, now_s, r["id"], PENDING),
        )
        if cur.rowcount == 1:
            claimed.append(get(conn, r["id"]))
    conn.commit()
    return claimed


def mark_done(conn: sqlite3.Connection, entry_id: str, notion_page_id: str | None = None,
              now: datetime | None = None) -> dict | None:
    """Mark an entry landed. For a *create*, pass the new ``notion_page_id`` — it's stored in the **same
    local transaction** as the status flip, shrinking the crash-after-create window to milliseconds
    (the accepted create-idempotency tradeoff, §6)."""
    now = now or _utcnow()
    cur = conn.execute(
        "UPDATE outbox SET status=?, notion_page_id=COALESCE(?, notion_page_id), last_error=NULL, "
        "last_attempt_at=? WHERE id=?",
        (DONE, notion_page_id, iso(now), entry_id),
    )
    conn.commit()
    return get(conn, entry_id) if cur.rowcount else None


def next_backoff(attempts: int, base: float = BACKOFF_BASE_SEC, cap: float = BACKOFF_CAP_SEC,
                 retry_after=None, rand=None) -> float:
    """Delay (seconds) before the next attempt. Honors a server ``Retry-After`` when given; otherwise
    exponential (``base * 2**(attempts-1)``, capped) with **equal jitter** (half fixed + half random) so
    a fleet of retries doesn't thundering-herd. ``rand`` is injectable for deterministic tests."""
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    exp = base * (2 ** max(0, attempts - 1))
    delay = min(cap, exp)
    r = rand() if rand is not None else random.random()
    return delay / 2 + r * (delay / 2)


def mark_retry(conn: sqlite3.Connection, entry_id: str, error, now: datetime | None = None,
               max_attempts: int = MAX_ATTEMPTS, base: float = BACKOFF_BASE_SEC,
               cap: float = BACKOFF_CAP_SEC, retry_after=None, rand=None) -> dict | None:
    """Record a **transient** failure: back off and re-arm (INFLIGHT→PENDING with a future
    ``not_before``), or **dead-letter** if the attempt budget is spent. ``attempts`` was already
    incremented at claim time, so it reflects tries-so-far."""
    now = now or _utcnow()
    row = conn.execute("SELECT attempts FROM outbox WHERE id=?", (entry_id,)).fetchone()
    if row is None:
        return None
    if row["attempts"] >= max_attempts:
        return mark_failed(conn, entry_id, f"retry budget exhausted ({max_attempts}); last: {error}", now)
    delay = next_backoff(row["attempts"], base, cap, retry_after, rand)
    nb = iso(now + timedelta(seconds=delay))
    conn.execute(
        "UPDATE outbox SET status=?, not_before=?, last_error=?, last_attempt_at=? WHERE id=?",
        (PENDING, nb, _trim(error), iso(now), entry_id),
    )
    conn.commit()
    return get(conn, entry_id)


def mark_failed(conn: sqlite3.Connection, entry_id: str, error, now: datetime | None = None) -> dict | None:
    """Dead-letter an entry (a **permanent** error, or retries exhausted). It stops retrying, stays in the
    table, and is surfaced (§7) — never silently dropped."""
    now = now or _utcnow()
    cur = conn.execute(
        "UPDATE outbox SET status=?, last_error=?, last_attempt_at=? WHERE id=?",
        (FAILED, _trim(error), iso(now), entry_id),
    )
    conn.commit()
    return get(conn, entry_id) if cur.rowcount else None


def stats(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Observability snapshot (§7): counts by status, the age of the oldest un-landed entry, and the full
    dead-letter list. The one call behind ``outbox.py status``."""
    now = now or _utcnow()
    counts = {s: 0 for s in STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) AS c FROM outbox GROUP BY status"):
        counts[r["status"]] = r["c"]
    oldest = conn.execute(
        "SELECT MIN(created_at) AS m FROM outbox WHERE status IN (?, ?)", (PENDING, INFLIGHT)
    ).fetchone()["m"]
    oldest_age = (now - parse_iso(oldest)).total_seconds() if oldest else None
    dead = [get(conn, r["id"]) for r in
            conn.execute("SELECT id FROM outbox WHERE status=? ORDER BY created_at", (FAILED,))]
    return {
        "counts": counts,
        "backlog": counts[PENDING] + counts[INFLIGHT],
        "oldest_pending_age_sec": oldest_age,
        "dead_letters": dead,
    }


def prune_done(conn: sqlite3.Connection, older_than_days: int = 14, now: datetime | None = None) -> int:
    """Delete DONE entries created more than ``older_than_days`` ago (Dream housekeeping). Dead-letters
    are **never** pruned — they persist until a human resolves them. Returns rows removed."""
    now = now or _utcnow()
    cutoff = iso(now - timedelta(days=older_than_days))
    cur = conn.execute("DELETE FROM outbox WHERE status=? AND created_at < ?", (DONE, cutoff))
    conn.commit()
    return cur.rowcount
