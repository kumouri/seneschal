#!/usr/bin/env python3
"""Cancel still-queued reminder nudges that an ack has made obsolete.

Companion to ``reminders_enqueue.py``. The presence daemon (``sentinel.check_reminders``) delivers
queued entries in ``seneschal/state/reminders.json`` purely by ``due_at`` and **cannot read Notion acks**,
so a nudge that was pre-staggered earlier in the day still fires even after the owner has acked the
underlying ⏰ Reminders row. When an ack lands — chat write-through, an ``Ack`` tick applied by a slot
run, or a linked Task/Goal flipping Done — call this to remove the matching **un-fired** entries so the
obsolete re-nudge never fires. See ``seneschal/references/reminders-policy.md`` and ``../state/README.md``.

Only entries with ``fired_at`` still null are removed — an already-delivered nudge is history and is
left untouched (we never rewrite what already went out). Matching is by ``reminder_id`` (the stable ⏰
row key — ideally the Notion row page id; normalized so an id matches with or without dashes) and/or by
the entry ``id`` directly. Idempotent: cancelling an already-absent key is a no-op success.

**Also records the ack** (``reminders_acks.record_ack``): every ``--reminder-id`` is stamped acked on
today's local date in ``state/acks.json``, the durable ledger the fire path (``sentinel.check_reminders``)
now consults. This is what closes the gap the plain dequeue couldn't: a nudge staggered *before* the ack —
or a **soft-digest** covering it that no per-id dequeue can reach — is suppressed at fire time because the
ack is on durable record, not just in the volatile warm session. Pass ``--no-ack-record`` to cancel a
nudge WITHOUT recording an ack (a rare non-ack cancel), or ``--ack-date`` to backfill a specific date.

Stdlib only. Prints a JSON summary of what was removed (and acked).

Usage:
  python reminders_dequeue.py --reminder-id 00000000-0000-0000-0000-000000000001
  python reminders_dequeue.py --reminder-id cats-am --reminder-id breakfast
  python reminders_dequeue.py --id rmd-2026-07-03-morning-cats
  python reminders_dequeue.py --reminder-id cats-am --dry-run   # show what would go, write nothing
"""
import argparse
import json
import os
import sys

import reminders_acks as ra  # durable ack ledger — recorded here so the fire path can gate acked nudges

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))


def _norm(key) -> str:
    """Normalize a reminder key for comparison: lowercase, drop dashes/whitespace. Lets a Notion page
    id match whether or not it carries dashes. Non-strings normalize to '' (never match)."""
    if not isinstance(key, str):
        return ""
    return "".join(key.split()).replace("-", "").lower()


def load_reminders(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_reminders(path: str, data: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def cancel(reminders: list, reminder_ids=(), entry_ids=()):
    """Return (kept, removed): drop every un-fired entry whose ``reminder_id`` matches one of
    ``reminder_ids`` (normalized) or whose ``id`` matches one of ``entry_ids`` (exact). Fired entries
    are always kept."""
    want_rid = {_norm(r) for r in reminder_ids if _norm(r)}
    want_eid = set(entry_ids)
    kept, removed = [], []
    for r in reminders:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        obsolete = (
            r.get("fired_at") is None
            and (_norm(r.get("reminder_id")) in want_rid and _norm(r.get("reminder_id")) != ""
                 or r.get("id") in want_eid)
        )
        (removed if obsolete else kept).append(r)
    return kept, removed


def main() -> int:
    p = argparse.ArgumentParser(description="Cancel un-fired queued nudges an ack has made obsolete.")
    p.add_argument("--reminder-id", action="append", default=[], metavar="KEY",
                   help="stable ⏰ row key to cancel (repeatable); matches entries' reminder_id, "
                        "dash-insensitive. Ideally the Notion row page id.")
    p.add_argument("--id", action="append", default=[], dest="entry_id", metavar="ENTRY_ID",
                   help="exact queue-entry id to cancel (repeatable).")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="dir holding reminders.json")
    p.add_argument("--ack-date", default=None, metavar="YYYY-MM-DD",
                   help="local ack date to record (default: today in the owner's timezone). Backfill only.")
    p.add_argument("--no-ack-record", action="store_true",
                   help="cancel the nudge(s) WITHOUT recording an ack in acks.json (a rare non-ack cancel)")
    p.add_argument("--dry-run", action="store_true", help="report what would be removed; write nothing")
    args = p.parse_args()

    if not args.reminder_id and not args.entry_id:
        print(json.dumps({"ok": False, "error": "give at least one --reminder-id or --id"}))
        return 2

    path = os.path.join(args.state_dir, "reminders.json")
    if args.dry_run:
        reminders = load_reminders(path)
        kept, removed = cancel(reminders, args.reminder_id, args.entry_id)
        removed_ids = [r.get("id") for r in removed if isinstance(r, dict)]
        would_ack = [] if args.no_ack_record else list(args.reminder_id)
        print(json.dumps({"ok": True, "dry_run": True, "would_remove": len(removed),
                          "ids": removed_ids, "would_ack": would_ack}))
        return 0

    # Load-modify-save under the cross-process queue lock: the daemon's check_reminders can be
    # mid load→deliver→save in a worker thread right now, and racing it unlocked either resurrects
    # what we remove here or erases its fresh fired_at stamps (double buzz).
    with ra.queue_lock(args.state_dir):
        reminders = load_reminders(path)
        kept, removed = cancel(reminders, args.reminder_id, args.entry_id)
        removed_ids = [r.get("id") for r in removed if isinstance(r, dict)]
        if removed:
            save_reminders(path, kept)
    # Record each --reminder-id as acked today so the fire path suppresses any OTHER un-fired nudge for
    # it (a staggered single or a soft-digest member) even though this dequeue removed none of them. The
    # ack is the durable fact; dequeue only trims what happens to be queued right now. Done regardless of
    # how many entries were removed (an ack can land after the individual nudge already fired).
    acked = []
    if not args.no_ack_record:
        for rid in args.reminder_id:
            rec = ra.record_ack(args.state_dir, rid, args.ack_date)
            if rec:
                acked.append(rec["key"])
    print(json.dumps({"ok": True, "removed": len(removed), "ids": removed_ids,
                      "acked": acked, "path": path}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
