#!/usr/bin/env python3
"""`state/failures.jsonl` — a durable row for a failure that used to vanish silently.

A foundation primitive, built on `stateio.append_jsonl`. It is meant to be called from inside an
`except` branch that is already degraded — a reminder send that came back not-ok, a store read that
raised, a corrupt ledger being quarantined — where the old behaviour was to swallow the error and
carry on with a fail-open default that looks exactly like health. Wiring a call site to it is a
deliberate, per-site decision: this module existing does not widen any handler's scope.

## `record()` MUST NEVER RAISE

Every call site is already inside a broad handler that exists so a broken store can never take down
the caller. If `record()` could raise, calling it would convert a survivable failure into a crash at
exactly the moment the system is already degraded — strictly worse than the silence it replaces. So
every exception inside `record()` is swallowed: a failed append costs the row, never the caller's own
recovery. `dream_steps.record` follows the same contract, for the same reason.

`tail()`/`count_since()` are the reader half. `watchdog_status()` is the small reader for the daemon's
`vitals.json` once-a-minute snapshot (when the daemon writes one): it answers whether the tick is
fresh, and if it is, whether the work it reports is healthy — never conflating a STALE tick (daemon
down, mid-reload, file unreadable — "unknown") with a FRESH one reporting failures ("unhealthy").
That distinction is the whole point: a backlog read that fails open to `{"pending": 0, "dead": 0}`
reads exactly like a healthy queue whether the daemon is running fine or the store is broken.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import stateio

FAILURES_FILENAME = "failures.jsonl"
VITALS_FILENAME = "vitals.json"

#: A vitals tick older than this (or, degenerately, from the future by more than this) is STALE — the
#: watchdog answers "unknown", never "healthy". Three missed writes at the ~60 s snapshot cadence.
VITALS_STALE_SEC = 180


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def record(state_dir: str, site: str, kind: str, detail: str = "", *, now: datetime | None = None) -> None:
    """Append one failure row: ``{at, site, kind, detail}``. NEVER RAISES.

    Call this from inside an `except` branch that is already degraded — a raise here must cost only
    the row, never the caller's own fail-open recovery. `detail` is truncated (500 chars): this is a
    breadcrumb for a human, not a second copy of a traceback."""
    try:
        path = os.path.join(state_dir, FAILURES_FILENAME)
        row = {"at": _stamp(now), "site": str(site), "kind": str(kind), "detail": str(detail)[:500]}
        stateio.append_jsonl(path, row)
    except Exception:  # noqa: BLE001 — this call exists to survive an already-broken caller
        pass


def tail(state_dir: str, limit: int = 50) -> list:
    """The newest `limit` failure rows — fail-open to `[]` on an absent or unreadable log."""
    return stateio.tail_jsonl(os.path.join(state_dir, FAILURES_FILENAME), limit)


def count_since(state_dir: str, since_iso: str) -> int:
    """How many rows landed at or after `since_iso` (a string compare against the same ISO stamp
    format every row is written with — sortable lexically, like every other timestamp in this
    tree)."""
    path = os.path.join(state_dir, FAILURES_FILENAME)
    return sum(1 for row in stateio.iter_jsonl(path) if str(row.get("at", "")) >= str(since_iso))


def read_vitals(state_dir: str) -> dict | None:
    """The daemon's own most recent `vitals.json` snapshot, or `None` if it has never been written or
    cannot be read — never a raise, and never a guessed value standing in for a missing one."""
    path = os.path.join(state_dir, VITALS_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_iso(raw) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def watchdog_status(state_dir: str, now: datetime | None = None) -> dict:
    """Read `vitals.json` and answer: is the tick fresh, and if it is, is the work it reports healthy?

    Returns ``{"tick_fresh": bool, "healthy": bool | None, "reason": str, "vitals": dict | None}``.
    `healthy` is `None` whenever `tick_fresh` is `False` — a stale or missing tick means "we don't
    know", which must never be reported as either "healthy" or "unhealthy". A fresh tick is unhealthy
    the moment it reports any of the three failure signals the snapshot carries: a failed reminder
    send today, a failed slot today, or a non-empty outbox dead-letter count."""
    now = now or datetime.now(timezone.utc)
    vitals = read_vitals(state_dir)
    if vitals is None:
        return {"tick_fresh": False, "healthy": None, "reason": "no vitals.json", "vitals": None}
    tick_at = _parse_iso(vitals.get("tick_at"))
    if tick_at is None:
        return {"tick_fresh": False, "healthy": None, "reason": "tick_at unreadable", "vitals": vitals}
    age = abs((now - tick_at).total_seconds())
    if age > VITALS_STALE_SEC:
        return {"tick_fresh": False, "healthy": None, "reason": f"tick is {age:.0f}s old",
                "vitals": vitals}
    failing = bool(vitals.get("reminders_failed_today") or vitals.get("slots_failed_today")
                   or vitals.get("outbox_dead"))
    return {"tick_fresh": True, "healthy": not failing,
            "reason": "failure signal in this tick" if failing else "ok", "vitals": vitals}
