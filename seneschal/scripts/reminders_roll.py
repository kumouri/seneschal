#!/usr/bin/env python3
"""Standing every-N-hours reminder *rolls* — the hands-off refill for cadences the four daily
slots can't express.

The morning/midday/evening/bedtime slots (``reminders-policy.md``) cover the owner's habits, but some
reminders want a **custom intraday cadence** — e.g. "check messages every 2 hours, 9am–11pm." Rather
than have the assistant hand-enqueue those each day, this module regenerates a roll's whole day of nudges
**once per local day**, deterministically and for free (pure local queue math — no Notion, no ``claude``
spawn). The presence daemon calls :func:`refill_rolls` from its loop (gated once/day via ``rolls.json``);
it can also be run by hand for a dry-run.

Design notes:
  * **Future-only.** A refill only enqueues slots whose due instant is still ahead, so a late first
    run of the day (machine asleep through the morning) never back-fires a burst of past nudges.
  * **Idempotent.** Every slot has a stable id ``rmd-<local-date>-<prefix>-<HH>``; re-running skips ids
    already in ``reminders.json`` (fired or not), so it's safe to call every loop.
  * **Rolling horizon.** Each run covers today + tomorrow, so even if the daemon isn't up early the
    next morning the day is already seeded. ``Dream`` prunes fired entries nightly, so the queue stays
    bounded.
  * **DST.** due instants are computed off ``now_local``'s current UTC offset (a fixed offset, so the
    function is deterministic and testable off any host clock — CI runs on UTC). The only imperfection
    is the single day a DST transition lands *between* now and a next-day slot, where a nudge can be an
    hour off — negligible for a "check messages" ping, and it self-corrects the following day.

Stdlib only. Times in ``reminders.json`` are UTC ISO-8601 with a trailing ``Z`` (the daemon never
converts zones). See ``seneschal/state/README.md`` for the entry schema and ``reminders-policy.md`` for
behavior.

Usage:
  python reminders_roll.py --dry-run           # show what today+tomorrow would add, write nothing
  python reminders_roll.py                      # refill the queue now (what the daemon calls)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import reminders_acks as ra  # queue_lock — serialize reminders.json writers (sibling, stdlib)
from reminders_enqueue import _now_z, load_reminders, save_reminders

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

# The standing rolls. Each is a custom intraday cadence keyed to a ⏰ Reminders row (reminder_id =
# the Notion page id, so an ack's reminders_dequeue can still cancel a queued nudge). Add a dict here
# to give another reminder an every-N-hours roll.
ROLLS = [
    {
        "id_prefix": "alex",
        "text": "Check messages from Alex.",
        "reminder_id": "00000000-0000-0000-0000-000000000042",
        "start_hour": 9,     # first nudge, local hour (inclusive)
        "end_hour": 23,      # last nudge, local hour (inclusive)
        "interval_hours": 2,
        "channel": "telegram",
    },
]


def roll_entries(roll: dict, now_local: datetime, horizon_days: int = 2) -> list:
    """Future nudge entries for one roll across ``horizon_days`` local dates starting today.

    Emits only slots whose due instant is strictly after ``now_local`` (future-only). Deterministic
    given ``now_local`` — it applies that moment's fixed UTC offset rather than consulting the host
    tz db — so it's unit-testable on any machine. ``now_local`` must be timezone-aware."""
    offset = now_local.utcoffset() or timedelta(0)
    local_tz = timezone(offset)
    now_utc = now_local.astimezone(timezone.utc)
    start = roll["start_hour"]
    end = roll["end_hour"]
    step = roll.get("interval_hours", 2)
    prefix = roll["id_prefix"]
    text = roll["text"]
    channel = roll.get("channel", "telegram")
    reminder_id = roll["reminder_id"]
    pierce_quiet = bool(roll.get("pierce_quiet"))  # a Critical roll may set this to fire through quiet

    out = []
    base = now_local.date()
    for d in range(horizon_days):
        day = base + timedelta(days=d)
        for hour in range(start, end + 1, step):
            due_utc = datetime(day.year, day.month, day.day, hour, 0, tzinfo=local_tz).astimezone(timezone.utc)
            if due_utc <= now_utc:
                continue  # future-only: never back-fire a past slot
            out.append({
                "id": f"rmd-{day.isoformat()}-{prefix}-{hour:02d}",
                "text": text,
                "due_at": due_utc.isoformat().replace("+00:00", "Z"),
                "channel": channel,
                "reminder_id": reminder_id,
                "pierce_quiet": pierce_quiet,
            })
    return out


def refill_rolls(state_dir: str, now_local: datetime | None = None, log=print,
                 rolls: list | None = None) -> int:
    """Append any missing future roll nudges to ``reminders.json``. Idempotent (skips ids already
    present). Returns the number of entries added; writes only if something was added."""
    if now_local is None:
        now_local = datetime.now().astimezone()
    rolls = ROLLS if rolls is None else rolls

    path = os.path.join(state_dir, "reminders.json")
    # Load-modify-save under the cross-process queue lock (see reminders_acks.queue_lock) — the
    # daemon's check_reminders or a slot's enqueue may be rewriting the file concurrently.
    with ra.queue_lock(state_dir):
        queue = load_reminders(path)
        have = {r.get("id") for r in queue if isinstance(r, dict)}

        added = 0
        for roll in rolls:
            for e in roll_entries(roll, now_local):
                if e["id"] in have:
                    continue
                entry = {
                    "id": e["id"],
                    "text": e["text"],
                    "due_at": e["due_at"],
                    "channel": e["channel"],
                    "created_at": _now_z(),
                    "fired_at": None,
                    "reminder_id": e["reminder_id"],
                    # Rolls are same-day *multi-fire* (e.g. "check messages every 2h") — one ack must NOT
                    # cancel the rest of the day. Opt out of the fire-time ack gate so acking once still
                    # leaves the later pings standing (a roll is hushed for the day via quiet or a
                    # dequeue, not an ack).
                    "ack_gate": False,
                }
                if e.get("pierce_quiet"):
                    entry["pierce_quiet"] = True
                queue.append(entry)
                have.add(e["id"])
                added += 1
        if added:
            save_reminders(path, queue)
            log(f"• reminder rolls refilled (+{added} nudges queued)")
    return added


def main() -> int:
    p = argparse.ArgumentParser(description="Refill standing every-N-hours reminder rolls into the queue.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="dir holding reminders.json")
    p.add_argument("--dry-run", action="store_true", help="print what today+tomorrow would add; write nothing")
    args = p.parse_args()

    now_local = datetime.now().astimezone()
    if args.dry_run:
        entries = [e for roll in ROLLS for e in roll_entries(roll, now_local)]
        print(json.dumps({"ok": True, "dry_run": True, "count": len(entries), "would_add": entries}, indent=2))
        return 0
    added = refill_rolls(args.state_dir, now_local=now_local)
    print(json.dumps({"ok": True, "added": added}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
