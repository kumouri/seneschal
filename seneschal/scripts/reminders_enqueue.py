#!/usr/bin/env python3
"""Enqueue a reminder into seneschal/state/reminders.json for the presence daemon to deliver.

This is the bridge between the assistant's brain (the Reminders mode, which runs the recurring/
importance/nag/rib state machine against the Notion ⏰ Reminders DB) and the always-on
``presence.py`` daemon, which actually delivers due reminders over Telegram. The brain decides
*what* to say and *when*; this script drops a one-shot entry into the local queue; the daemon
fires it (``due_at <= now``) and stamps ``fired_at``. See ``seneschal/state/README.md`` for the
schema and ``seneschal/references/reminders-policy.md`` for the behavior.

Stdlib only. Times are UTC ISO-8601 with a trailing ``Z`` (the daemon never converts zones).

Usage:
  python reminders_enqueue.py --text "Morning meds — required (vitamin D, …)"
  python reminders_enqueue.py --text-file nudge.txt --id rmd-2026-06-29-morning
  python reminders_enqueue.py --text "…" --due-at 2026-06-29T18:30:00Z --channel telegram
  python reminders_enqueue.py --text "Cats' breakfast." --reminder-id 00000000-0000-0000-0000-000000000001
  python reminders_enqueue.py --text "Take your meds." --channel call   # rings the owner's phone
  python reminders_enqueue.py --text "…" --dry-run        # build the entry, write nothing

Channels the presence daemon can deliver: telegram (default), call (phone, via the Worker /push-call),
discord. A `call` entry's text is *spoken*, so phrase it for the ear; telegram/discord get an ⏰ prefix.
An unconfigured channel falls back to Telegram at fire time (never silently dropped).
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import reminders_acks as ra  # queue_lock — serialize reminders.json writers (sibling, stdlib)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:32] or "reminder")


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


def main() -> int:
    p = argparse.ArgumentParser(description="Enqueue a reminder for the presence daemon to deliver.")
    p.add_argument("--text", help="reminder text (shown after '⏰ Reminder: ')")
    p.add_argument("--text-file", help="read the reminder text from a file")
    p.add_argument("--id", help="stable id; default rmd-<utc-date>-<slug>. Reused id is skipped (idempotent).")
    p.add_argument("--reminder-id", help="stable key linking this nudge back to its ⏰ Reminders row "
                   "(ideally the Notion row page id) so an ack can cancel it via reminders_dequeue.py "
                   "AND the fire path can suppress it once acked (state/acks.json).")
    p.add_argument("--member-reminder-id", action="append", default=[], metavar="KEY",
                   help="(digests) a ⏰ row key this combined nudge covers (repeatable). The fire-time ack "
                        "gate drops the whole digest only once EVERY member is acked today — so a soft "
                        "status digest can't buzz for a set of things the owner already finished.")
    p.add_argument("--no-ack-gate", action="store_true",
                   help="exempt this nudge from the fire-time ack gate (for a same-day multi-fire nudge "
                        "that should keep firing after one ack, like a standing roll).")
    p.add_argument("--require-place", metavar="NAME",
                   help="(presence) hold this nudge until the owner is at the named place (a geofence, e.g. "
                        "'home'), then fire it — deferred, never dropped. No-op unless the presence feed "
                        "is reporting location; stale/absent presence fires it. See presence_rules.py.")
    p.add_argument("--due-at", help="UTC ISO-8601 instant (e.g. 2026-06-29T18:30:00Z); default now")
    p.add_argument("--channel", default="telegram", choices=["telegram", "call", "discord"],
                   help="delivery channel: telegram (default) | call (phone) | discord")
    p.add_argument("--pierce-quiet", action="store_true",
                   help="fire this nudge even during a do-not-disturb window (quiet.json). Set it for "
                        "Critical-and-above items (🚨/🛑); a Call Me/escalating call pierces on its own.")
    p.add_argument("--escalate", action="store_true",
                   help="(call channel) keep calling back until the owner presses a digit; the daemon passes "
                        "--escalate to push_call.py so the Worker runs the retry-until-ack loop.")
    p.add_argument("--interval-sec", type=int, default=None, help="seconds between escalation callbacks (only with --escalate)")
    p.add_argument("--max-attempts", type=int, default=None, help="max escalation calls before giving up (only with --escalate)")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="dir holding reminders.json")
    p.add_argument("--dry-run", action="store_true", help="build the entry and print it; write nothing")
    args = p.parse_args()

    text = args.text
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
    if not text:
        print(json.dumps({"ok": False, "error": "no --text or --text-file"}))
        return 2

    now = _now_z()
    due_at = args.due_at or now
    rid = args.id or f"rmd-{now[:10]}-{_slug(text)}"
    entry = {
        "id": rid,
        "text": text,
        "due_at": due_at,
        "channel": args.channel,
        "created_at": now,
        "fired_at": None,
    }
    if args.reminder_id:
        entry["reminder_id"] = args.reminder_id
    if args.member_reminder_id:
        entry["member_reminder_ids"] = args.member_reminder_id
    if args.no_ack_gate:
        entry["ack_gate"] = False
    if args.pierce_quiet:
        entry["pierce_quiet"] = True
    if args.require_place:
        entry["require_place"] = args.require_place
    if args.escalate:
        entry["escalate"] = True
        if args.interval_sec is not None:
            entry["interval_sec"] = args.interval_sec
        if args.max_attempts is not None:
            entry["max_attempts"] = args.max_attempts

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "entry": entry}))
        return 0

    path = os.path.join(args.state_dir, "reminders.json")
    # Load-modify-save under the cross-process queue lock — the daemon's check_reminders (worker
    # thread) or a sibling enqueue (parallel slot run) may be rewriting the file right now, and an
    # unlocked race drops whichever write loses.
    with ra.queue_lock(args.state_dir):
        reminders = load_reminders(path)
        if any(isinstance(r, dict) and r.get("id") == rid for r in reminders):
            print(json.dumps({"ok": True, "enqueued": False, "skipped": "duplicate id", "id": rid}))
            return 0
        reminders.append(entry)
        save_reminders(path, reminders)
    print(json.dumps({"ok": True, "enqueued": True, "id": rid, "due_at": due_at, "path": path}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
