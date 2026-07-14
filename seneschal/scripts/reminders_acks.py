#!/usr/bin/env python3
"""Durable local **ack ledger** — the fire-time gate that stops the daemon re-nudging acked reminders.

**Why this exists.** The presence daemon (``sentinel.check_reminders``) delivers queued nudges purely by
``due_at`` and **can't read Notion**: its only Notion path is the hosted OAuth MCP, reachable from the
``claude`` CLI, not from this stdlib daemon. So a nudge staggered earlier in the day — most painfully a
combined **soft-digest** baked by the 08:00 slot — still fires after the owner has acked the underlying item,
because the fire path had no way to see the ack. ``reminders_dequeue.py`` pulls an obsolete *single* nudge
by id, but it can't reach inside a multi-item digest (one entry, one text), and it only helps when the
chat/slot path remembers to call it.

This closes the gap with a **durable local ledger** (``state/acks.json``): every ack records the ⏰ row's
key + the local date it landed, and ``check_reminders`` consults it at **fire time**. It's durable
(survives the warm session winding down / a reboot — it's a file, not the volatile session), Notion-
independent, and **fail-open**: any read error means *fire the nudge* (a redundant buzz beats a missed
Critical one). This is the "gate delivery on durable state, not session memory" fix.

``acks.json`` maps ``{ "<normalized reminder_id>": "YYYY-MM-DD" }`` — the **local** (owner-timezone,
via ``tz_common``) date of the most recent ack for that ⏰ row. Only *today's* acks gate; yesterday's don't (so tomorrow's re-fire
is unaffected), which mirrors the daily reset. Matching is dash-insensitive so a Notion page id matches
with or without dashes, exactly like ``reminders_dequeue``.

A queue entry opts **out** of the gate with ``"ack_gate": false`` — set by same-day-recurring **rolls**
(``reminders_roll.py``), where one "checked messages" ack must not cancel the rest of the day's pings. A
combined digest carries ``"member_reminder_ids": [...]``; it's suppressed only once **every** member is
acked today. Stdlib only.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime

import tz_common

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
ACKS_FILE = "acks.json"
QUEUE_LOCK_FILE = "reminders.json.lock"


@contextlib.contextmanager
def queue_lock(state_dir: str, timeout: float = 90.0, stale_sec: float = 120.0):
    """Cross-process advisory mutex for ``reminders.json`` load-modify-save sections.

    Every writer rewrites the whole file (load → mutate → atomic replace), and since the asyncio daemon
    they can genuinely overlap: the scheduler's ``check_reminders`` runs in a worker thread WHILE a chat
    turn's ``reminders_dequeue.py`` (or a slot's ``reminders_enqueue.py``) runs as a separate process.
    Last-writer-wins there can erase a fresh ``fired_at`` (→ double buzz, violating the one-nudge-per-push
    policy) or resurrect a dequeued entry. This serializes the four writers (sentinel.check_reminders,
    reminders_dequeue, reminders_enqueue, reminders_roll.refill_rolls); lives here because this module is
    the leaf they all already reach.

    Semantics, chosen deliberately:
      * plain lockfile (O_CREAT|O_EXCL) — the only cross-process primitive that's stdlib on Windows;
      * a holder that crashed is STOLEN after ``stale_sec`` (mtime age) so a dead process can't wedge
        the queue (120 s > the longest legitimate hold: a burst of sends at 60 s subprocess timeout);
      * **fail-open** past ``timeout``: proceed unlocked. A stuck lock degrades to the pre-lock race —
        a possible redundant buzz — which beats reminders falling silent. Same doctrine as the ledger.
    """
    path = os.path.join(state_dir, QUEUE_LOCK_FILE)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            os.makedirs(state_dir, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - os.path.getmtime(path) > stale_sec:
                    os.remove(path)  # crashed holder — steal and retry immediately
                    continue
            if time.monotonic() >= deadline:
                break  # fail-open (see docstring)
            time.sleep(0.05)
        except OSError:
            break  # can't even create the lock (odd perms/fs) — fail-open, never silence nudges
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                os.remove(path)


def norm_key(key) -> str:
    """Normalize a reminder key for comparison: lowercase, drop dashes/whitespace — so a Notion page id
    matches whether or not it carries dashes. Non-strings normalize to '' (never match). Mirrors
    ``reminders_dequeue._norm`` so the ledger and the dequeue path agree on identity."""
    if not isinstance(key, str):
        return ""
    return "".join(key.split()).replace("-", "").lower()


def local_today(now: datetime | None = None) -> str:
    """The current **owner-local** date as ``YYYY-MM-DD``, via ``tz_common``.

    Accepts an aware UTC instant (as ``check_reminders`` holds) — naive is taken as UTC — and converts
    to the owner's timezone (the configured identity zone when resolvable, else machine-local, which is
    exactly the old behavior). With no argument, reads the clock now. Gating on the *local* date (not
    UTC) matches the daily reset and rule 5 (date logic on local time)."""
    if now is None:
        return tz_common.local_today()
    return tz_common.to_local(now).strftime("%Y-%m-%d")


def acks_path(state_dir: str) -> str:
    return os.path.join(state_dir, ACKS_FILE)


def load_acks(state_dir: str) -> dict:
    """Load the ack ledger (``{normalized_reminder_id: 'YYYY-MM-DD'}``). A missing or malformed file
    reads as an empty ledger — fail-open, so a broken ledger never *suppresses* a genuine nudge."""
    data = load_json(acks_path(state_dir), {})
    return data if isinstance(data, dict) else {}


def save_acks(state_dir: str, acks: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    tmp = acks_path(state_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(acks, fh, indent=2, sort_keys=True)
    os.replace(tmp, acks_path(state_dir))


def record_ack(state_dir: str, reminder_id: str, date_str: str | None = None) -> dict | None:
    """Stamp ``reminder_id`` as acked on ``date_str`` (default: local today). Returns the written
    ``{key, date}`` record, or ``None`` if the id normalizes to empty (nothing to record). Overwrites
    any prior date for that key — we only ever care about the *most recent* ack."""
    key = norm_key(reminder_id)
    if not key:
        return None
    date_str = date_str or local_today()
    acks = load_acks(state_dir)
    acks[key] = date_str
    save_acks(state_dir, acks)
    return {"key": key, "date": date_str}


def prune_acks(state_dir: str, keep_date: str) -> int:
    """Drop ledger rows older than ``keep_date`` (string compare works for ISO dates). Returns how many
    were removed. Optional housekeeping for Dream — the gate ignores stale rows anyway, this just keeps
    the file small."""
    acks = load_acks(state_dir)
    kept = {k: d for k, d in acks.items() if isinstance(d, str) and d >= keep_date}
    removed = len(acks) - len(kept)
    if removed:
        save_acks(state_dir, kept)
    return removed


def entry_acked(entry: dict, acks: dict, today_str: str) -> bool:
    """Pure fire-time gate predicate: should this queued entry be **suppressed as already-acked today**?

    - ``ack_gate: false`` (multi-fire rolls) → never gated.
    - Collect the entry's reminder keys: ``reminder_id`` plus any ``member_reminder_ids`` (a digest).
    - No keys → not gated (an un-keyed nudge fires as before; we never make delivery *worse*).
    - Gated iff **every** collected key is acked on ``today_str`` in ``acks`` — so a plain single-row
      nudge drops once its row is acked, and a digest drops only once *all* its members are.

    Pure and total (never raises) so ``check_reminders`` can call it in a tight loop and callers stay
    fail-open."""
    if not isinstance(entry, dict) or entry.get("ack_gate") is False:
        return False
    keys = []
    rid = entry.get("reminder_id")
    if rid:
        keys.append(rid)
    members = entry.get("member_reminder_ids")
    if isinstance(members, list):
        keys.extend(m for m in members if m)
    norm = [norm_key(k) for k in keys]
    norm = [k for k in norm if k]
    if not norm:
        return False
    return all(acks.get(k) == today_str for k in norm)


def entry_acked_today(state_dir: str, entry: dict, now: datetime | None = None) -> bool:
    """Convenience wrapper: load the ledger and evaluate :func:`entry_acked` for ``now`` (local today).
    ``check_reminders`` uses the pure form directly (loading the ledger once per pass); this is for
    one-off callers/tests."""
    try:
        return entry_acked(entry, load_acks(state_dir), local_today(now))
    except Exception:  # noqa: BLE001 — fail-open: a ledger error must never suppress a real nudge
        return False


# Local json loader (kept self-contained so this module has no import cycle with sentinel).
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
