#!/usr/bin/env python3
"""The owning script for Dream's "reconcile `state/reminders.json`" step (``dream_steps`` step ``2``).

**Why it exists.** ``modes/dream.md`` step 2 used to say *"reconcile `state/reminders.json` (drop
fired/expired…)"* in prose, with no script behind it — and nothing else ever garbage-collects the
queue: the daily seed (``presence.maybe_seed_day`` → ``reminders_seed.py``) only *appends* each day's
due nudges, ``sentinel.check_reminders`` only stamps ``fired_at`` on delivery, ``reminders_enqueue.py``
adds and ``reminders_dequeue.py`` cancels by ack. None of them ever removes a row, so a prose step
nobody owns lets the queue grow by hundreds of stale entries across days before anyone notices. A
prose instruction with no owner silently does not happen; this script is the owner.

**The rule.** Under ``reminders_acks.queue_lock`` — the same cross-process lock
``reminders_enqueue.py`` / ``reminders_dequeue.py`` / the daemon's fire path share, never a new one —
drop every entry whose ``due_at`` falls on an **owner-local activity day** (``activity_day.from_instant``:
the owner's timezone via ``tz_common``, cut at ``owner.dayBoundaryHour``) strictly before today.
Today's and every future entry are kept untouched. Fired and un-fired stale entries go alike: this is
the one place fired history is collected.

**An entry whose ``due_at`` is absent or unparseable is KEPT, deliberately.** This function's only
power is deletion, so an unreadable date has to mean "leave it alone" — the same rule
``reminders_dequeue.cancel`` follows, for the same reason: dropping on a parse failure is how this file
gets emptied by the tool meant to protect it, and nothing recovers a wrongly deleted nudge.

Self-stamps a ``dream_steps`` row (step ``"2"``) on every real run — a quiet night included — so a night
this doesn't run is visible on disk, per ``dream_steps.py``'s "a skip must cost something" rule.
``--dry-run`` stamps nothing.

Written atomically via ``memory_write.write_text``, and only when something was dropped — never
``open(path, "w")`` over the live queue.

Stdlib only.

Usage:
    python reminders_reconcile.py
    python reminders_reconcile.py --dry-run
    python reminders_reconcile.py --state-dir ../state
"""
from __future__ import annotations

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import activity_day as ad  # noqa: E402 — the after-midnight rule, one definition
import dream_steps as ds  # noqa: E402 — the Dream step ledger this script stamps
import memory_write as mw  # noqa: E402 — atomic, empty-refusing writer
import reminders_acks as ra  # noqa: E402 — queue_lock: the one shared lock on reminders.json

DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

#: ``dream_steps.py``'s step id for this reconcile. Registered (with this file as owner) in its STEPS.
DREAM_STEP = "2"


def load_reminders(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_reminders(path: str, data: list) -> None:
    mw.write_text(path, json.dumps(data, indent=2))


def reconcile(reminders: list, *, today) -> tuple[list, list]:
    """``(kept, dropped)`` for the activity date ``today`` (a ``date``; keyword-only and required, so
    no caller can reach the clock by accident).

    Dropped: every entry whose ``due_at`` reads as an activity day strictly before ``today``. Kept:
    everything else — including a non-dict row (nothing here can judge it, so it survives) and any
    entry whose ``due_at`` can't be read: ``activity_day.from_instant`` returning ``None`` is a
    "don't know", and "don't know" means "keep" for a function whose only power is deletion."""
    kept, dropped = [], []
    for r in reminders:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        day = ad.from_instant(r.get("due_at"))
        if day is not None and day < today:
            dropped.append(r)
        else:
            kept.append(r)
    return kept, dropped


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Reconcile state/reminders.json: drop entries due before today's activity day.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="dir holding reminders.json")
    p.add_argument("--dry-run", action="store_true", help="report what would be dropped; write nothing")
    args = p.parse_args(argv)

    path = os.path.join(args.state_dir, "reminders.json")
    today = ad.today()

    if args.dry_run:
        reminders = load_reminders(path)
        kept, dropped = reconcile(reminders, today=today)
        print(json.dumps({
            "ok": True, "dry_run": True, "before": len(reminders),
            "kept": len(kept), "dropped": len(dropped), "today": today.isoformat(),
        }))
        return 0

    with ra.queue_lock(args.state_dir):
        reminders = load_reminders(path)
        kept, dropped = reconcile(reminders, today=today)
        if dropped:
            save_reminders(path, kept)

    # Stamped whether or not anything dropped: "ran and found nothing to do" must look different on
    # disk from "did not run at all" — the whole point of dream_steps.py.
    ds.record(args.state_dir, DREAM_STEP)
    print(json.dumps({
        "ok": True, "before": len(reminders), "kept": len(kept), "dropped": len(dropped),
        "today": today.isoformat(), "path": path,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
