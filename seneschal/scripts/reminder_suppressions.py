#!/usr/bin/env python3
"""`state/reminder-suppressions.jsonl` — a durable row for every reminder the staleness cutoff
CONSUMED without delivering it, because the suppression itself was the failure and nothing said so.

**Why this exists.** A High-importance Today Todo seeded deep into a large morning batch drains one
row per `sentinel.CATCHUP_STAGGER_SEC`, so its raw due-vs-now gap can cross `sentinel.MAX_LATENESS_SEC`
before its turn ever comes — and be suppressed as stale, day after day. `presence.py` writes one line to
`presence.log` for a suppression, but that is a daemon-internal log nobody reads day to day (it exists
for forensic replay), so repeated silent kills were invisible to the owner and to the EOD Wrap alike.
`sentinel.check_reminders` calls `record()` at the exact site that stamps `suppressed_at` for a
`reminder_suppressed_stale` signal; the EOD Wrap reads `count_today()` into its Slipped section
(`../../subagents/eod-wrap/SKILL.md`).

`record()` NEVER RAISES — same contract as `failures.record`: this is called from inside the fire-path
loop, and a logging problem must cost only the row, never the pass. Built on `stateio.append_jsonl`.

Deliberately its own file rather than another `failures.py` call site: a staleness suppression is a
distinct kind of event (a delivery outcome, not an exception path) with its own reader (the Wrap's
daily count) that `failures.tail`/`count_since` were never shaped for.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import activity_day
import stateio

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
FILENAME = "reminder-suppressions.jsonl"


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def record(state_dir: str, signal: dict, *, now: datetime | None = None) -> None:
    """Append one row from a `reminder_suppressed_stale` signal (see `sentinel.check_reminders`).
    NEVER RAISES. `signal` is the exact dict already built for the return-value/log side, so this is
    a projection of it, never a second computation of the same facts."""
    try:
        row = {
            "at": _stamp(now),
            "id": signal.get("id"),
            "reminder_id": signal.get("reminder_id"),
            "text": str(signal.get("text") or "")[:200],
            "due_at": signal.get("due_at"),
            "late_sec": signal.get("late_sec"),
            "presence_held_sec": signal.get("presence_held_sec"),
            "stagger_held_sec": signal.get("stagger_held_sec"),
        }
        stateio.append_jsonl(os.path.join(state_dir, FILENAME), row)
    except Exception:  # noqa: BLE001 — a logging problem must never cost the fire pass
        pass


def tail(state_dir: str, limit: int = 50) -> list:
    """The newest `limit` suppression rows — fail-open to `[]` on an absent/unreadable log."""
    return stateio.tail_jsonl(os.path.join(state_dir, FILENAME), limit)


def count_since(state_dir: str, since_iso: str) -> int:
    """How many rows landed at or after `since_iso` (string-sortable ISO, like every other timestamp
    in this tree). Fail-open to 0 on an absent/unreadable log."""
    return sum(1 for r in stateio.iter_jsonl(os.path.join(state_dir, FILENAME))
               if isinstance(r.get("at"), str) and r["at"] >= since_iso)


def count_today(state_dir: str, now: datetime | None = None) -> int:
    """How many suppressions landed on **today's activity day** (`activity_day.py` — after-midnight
    still counts as the prior day, cut at `owner.dayBoundaryHour`) — the EOD Wrap's own "N reminders
    went stale silently today" count."""
    now = now or datetime.now(timezone.utc)
    today = activity_day.today(now)
    n = 0
    for r in stateio.iter_jsonl(os.path.join(state_dir, FILENAME)):
        day = activity_day.from_instant(r.get("at"))
        if day == today:
            n += 1
    return n


def main() -> int:
    p = argparse.ArgumentParser(description="Read state/reminder-suppressions.jsonl (the staleness-"
                                             "suppression ledger the EOD Wrap folds into Slipped).")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("count-today", help="how many suppressions landed on today's activity day")
    tp = sub.add_parser("tail", help="print the newest N rows")
    tp.add_argument("--limit", type=int, default=20)
    args = p.parse_args()

    if args.cmd == "count-today":
        print(json.dumps({"ok": True, "count": count_today(args.state_dir)}))
    elif args.cmd == "tail":
        print(json.dumps({"ok": True, "rows": tail(args.state_dir, args.limit)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
