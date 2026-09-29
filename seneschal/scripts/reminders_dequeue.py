#!/usr/bin/env python3
"""Cancel still-queued reminder nudges that an ack has made obsolete.

Companion to ``reminders_enqueue.py``. The presence daemon (``sentinel.check_reminders``) delivers
queued entries in ``seneschal/state/reminders.json`` purely by ``due_at`` and **cannot read store acks**,
so a nudge that was pre-staggered earlier in the day still fires even after the owner has acked the
underlying ⏰ Reminders row. When an ack lands — chat write-through, an ``Ack`` tick applied by a slot
run, or a linked Task/Goal flipping Done — call this to remove the matching **un-fired** entries so the
obsolete re-nudge never fires. See ``seneschal/references/reminders-policy.md`` and ``../state/README.md``.

Only entries with ``fired_at`` still null are removed — an already-delivered nudge is history and is
left untouched (we never rewrite what already went out). Matching is by ``reminder_id`` (the stable ⏰
row key — ideally the store row's page id; normalized so an id matches with or without dashes) and/or by
the entry ``id`` directly. Idempotent: cancelling an already-absent key is a no-op success.

**A ``--reminder-id`` cancel is scoped to ONE ACTIVITY DAY.** An unscoped cancel (drop *every* un-fired
entry for the row) is fine for most of the day and destructive between midnight and the owner's day
boundary: an ack reported at 01:30 for the *previous* evening's item belongs to that previous activity
day, and an unscoped cancel would also eat the row's nudges queued for *this* evening. So an ack removes
only the nudges belonging to **its own activity day**: each un-fired entry's ``due_at`` is read on the
owner's wall clock and mapped through the same ``activity_day`` rule, so a nudge due 00:30 local on the
*following* calendar date still belongs to the current activity day and still goes, while the next
evening's survives.

The day is ``--activity-day`` if given, else ``--ack-date``, else *now* through that rule. Two
consequences worth stating rather than discovering: an entry whose ``due_at`` is missing or unreadable
is **kept** (never delete on a date you couldn't read — the durable ack ledger below suppresses it at
fire time anyway), and a **stale** un-fired entry from an earlier day is not swept up by today's ack
(that is the same fire-time gate's job). **``--id`` stays exact and unscoped**: an explicitly named
entry is removed whatever day it is due.

**Also records the ack** (``reminders_acks.record_ack``): every ``--reminder-id`` is stamped acked on
today's local date in ``state/acks.json``, the durable ledger the fire path (``sentinel.check_reminders``)
now consults. This is what closes the gap the plain dequeue couldn't: a nudge staggered *before* the ack —
or a **soft-digest** covering it that no per-id dequeue can reach — is suppressed at fire time because the
ack is on durable record, not just in the volatile warm session. Pass ``--no-ack-record`` to cancel a
nudge WITHOUT recording an ack (a rare non-ack cancel), or ``--ack-date`` to backfill a specific date.

**And stamps the ack INSTANT.** The same non-``--no-ack-record`` call writes ``last_ack_at`` (UTC ISO)
into ``state/nudge-stagger.json`` via ``reminders_acks.record_ack_instant`` — the **ack-advance** signal.
``sentinel``'s catch-up stagger releases its 15-min hold once an ack has landed after the last
non-piercing fire and ``ACK_ADVANCE_DEBOUNCE_SEC`` (2 min) has passed since that ack, so the next
still-pending nudge follows the owner's 👍 promptly. The debounce absorbs a burst: three 👍s within
seconds are three overwrites of one field, and the gate measures from the last. This never lowers any
other gate — ``acked`` runs first, so a row acked in that same burst is consumed, never fired.

Stdlib only. Prints a JSON summary of what was removed (and acked).

Usage:
  python reminders_dequeue.py --reminder-id 00000000-0000-0000-0000-000000000001
  python reminders_dequeue.py --reminder-id cats-am --reminder-id breakfast
  python reminders_dequeue.py --id rmd-2026-07-03-morning-cats
  python reminders_dequeue.py --reminder-id cats-am --dry-run   # show what would go, write nothing
  python reminders_dequeue.py --reminder-id cats-am --activity-day 2026-08-11   # scope a backfill

Invariants:

* THE LEDGER DATE AND THE ACTIVITY DAY ARE DIFFERENT QUESTIONS: ``ra.local_today`` is the calendar date
  the fire-time gate compares against and must stay that; the removal scope here is the after-midnight
  rule (``activity_day.py``), and the two disagree exactly across the midnight-to-boundary window —
  which is why ``ack.py`` passes ``--activity-day`` explicitly rather than letting it fall back.
* ``--id`` is EXACT AND UNSCOPED — naming an entry outright is already the instruction scoping
  approximates.
* An entry whose ``due_at`` can't be read is KEPT, since this function deletes and nothing recovers a
  deleted nudge (the fire-time gate covers the redundant buzz, which is the cheap error).
* ``cancel(day=...)`` IS KEYWORD-ONLY WITH NO DEFAULT, so the unscoped call can no longer be spelled.
* A bulk "clear my day" leaves Critical-and-above queued and never takes a "Call Me" row — but this
  function cancels exactly the ids it is handed, so which rows go is the CALLER'S judgment. A DEQUEUE
  DELETES; A QUIET WINDOW SUPPRESSES — a deleted entry reaches no gate, so it cannot pierce the way a
  quiet-suppressed entry can. See ``../references/reminders-policy.md``.
"""
import argparse
import json
import os
import sys

import activity_day as ad  # the after-midnight rule — ONE definition, shared with every day-scoped caller
import reminders_acks as ra  # durable ack ledger — recorded here so the fire path can gate acked nudges

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))


def _norm(key) -> str:
    """Normalize a reminder key for comparison: lowercase, drop dashes/whitespace. Lets a page id
    match whether or not it carries dashes. Non-strings normalize to '' (never match)."""
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


def entry_day(entry: dict):
    """The activity day an entry's ``due_at`` belongs to — ``None`` when it has none we can read.

    Owner wall clock first, then the day-boundary cut, both from :mod:`activity_day`. ``None`` is a
    "don't know", and the one caller treats it as *keep*."""
    if not isinstance(entry, dict):
        return None
    return ad.from_instant(entry.get("due_at"))


def cancel(reminders: list, reminder_ids=(), entry_ids=(), *, day):
    """Return (kept, removed) for the activity day ``day`` (a ``date`` or ``YYYY-MM-DD``).

    Removed: every **un-fired** entry that either

    * carries a ``reminder_id`` matching one of ``reminder_ids`` (normalized) **and** is due on
      ``day`` — the ack cancels that day's obsolete nudges and nothing else; or
    * has an ``id`` in ``entry_ids`` — **exact and unscoped**, because naming an entry outright is
      already the specific instruction that scoping exists to approximate.

    Fired entries are always kept (history, never rewritten), and so is anything whose ``due_at``
    can't be read: this function *deletes*, so an unreadable date has to mean "leave it alone".

    ``day`` is keyword-only and has **no default** deliberately: the bug it fixes was invisible for
    hours a night, and a caller that can forget to pass it is a caller that will.
    """
    scope = ad.parse_day(day)
    if scope is None:
        raise ValueError(f"cancel(day=...) needs a date or YYYY-MM-DD, got {day!r}")
    want_rid = {_norm(r) for r in reminder_ids if _norm(r)}
    want_eid = set(entry_ids)
    kept, removed = [], []
    for r in reminders:
        if not isinstance(r, dict) or r.get("fired_at") is not None:
            kept.append(r)
            continue
        rid = _norm(r.get("reminder_id"))
        obsolete = (
            r.get("id") in want_eid
            or (rid != "" and rid in want_rid and entry_day(r) == scope)
        )
        (removed if obsolete else kept).append(r)
    return kept, removed


def main(argv=None) -> int:
    """``argv=None`` reads ``sys.argv`` exactly as before; passing a list is how an in-process caller
    (``ack.py``) reuses this whole path — the queue lock, the cancel and the ack record — instead of
    reimplementing it or shelling out. Same shape as ``outbox.main``."""
    p = argparse.ArgumentParser(description="Cancel un-fired queued nudges an ack has made obsolete.")
    p.add_argument("--reminder-id", action="append", default=[], metavar="KEY",
                   help="stable ⏰ row key to cancel (repeatable); matches entries' reminder_id, "
                        "dash-insensitive. Ideally the store row's page id.")
    p.add_argument("--id", action="append", default=[], dest="entry_id", metavar="ENTRY_ID",
                   help="exact queue-entry id to cancel (repeatable).")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="dir holding reminders.json")
    p.add_argument("--ack-date", default=None, metavar="YYYY-MM-DD",
                   help="local ack date to record (default: today in the owner's timezone). Backfill "
                        "only. Also scopes which day's nudges a --reminder-id cancels, unless "
                        "--activity-day says otherwise.")
    p.add_argument("--activity-day", default=None, metavar="YYYY-MM-DD",
                   help="the activity day whose un-fired nudges a --reminder-id cancels (default: "
                        "--ack-date, else now through the after-midnight rule). --id is never scoped.")
    p.add_argument("--no-ack-record", action="store_true",
                   help="cancel the nudge(s) WITHOUT recording an ack in acks.json (a rare non-ack cancel)")
    p.add_argument("--dry-run", action="store_true", help="report what would be removed; write nothing")
    args = p.parse_args(argv)

    if not args.reminder_id and not args.entry_id:
        print(json.dumps({"ok": False, "error": "give at least one --reminder-id or --id"}))
        return 2

    # The ack's own activity day — what "the nudges this ack makes obsolete" actually means. The
    # ledger date (--ack-date / ra.local_today) is a CALENDAR date and is a different question; the
    # two diverge between midnight and the owner's day boundary.
    given = args.activity_day or args.ack_date
    day = ad.parse_day(given) if given else ad.today()
    if day is None:
        print(json.dumps({"ok": False,
                          "error": f"--activity-day/--ack-date must be YYYY-MM-DD, got {given!r}"}))
        return 2

    path = os.path.join(args.state_dir, "reminders.json")
    if args.dry_run:
        reminders = load_reminders(path)
        kept, removed = cancel(reminders, args.reminder_id, args.entry_id, day=day)
        removed_ids = [r.get("id") for r in removed if isinstance(r, dict)]
        would_ack = [] if args.no_ack_record else list(args.reminder_id)
        print(json.dumps({"ok": True, "dry_run": True, "would_remove": len(removed),
                          "ids": removed_ids, "would_ack": would_ack,
                          "activity_day": day.isoformat()}))
        return 0

    # Load-modify-save under the cross-process queue lock: the daemon's check_reminders can be
    # mid load→deliver→save in a worker thread right now, and racing it unlocked either resurrects
    # what we remove here or erases its fresh fired_at stamps (double buzz).
    with ra.queue_lock(args.state_dir):
        reminders = load_reminders(path)
        kept, removed = cancel(reminders, args.reminder_id, args.entry_id, day=day)
        removed_ids = [r.get("id") for r in removed if isinstance(r, dict)]
        if removed:
            save_reminders(path, kept)
    # Record each --reminder-id as acked today so the fire path suppresses any OTHER un-fired nudge for
    # it (a staggered single or a soft-digest member) even though this dequeue removed none of them. The
    # ack is the durable fact; dequeue only trims what happens to be queued right now. Done regardless of
    # how many entries were removed (an ack can land after the individual nudge already fired).
    acked = []
    ack_at = None
    if not args.no_ack_record:
        for rid in args.reminder_id:
            rec = ra.record_ack(args.state_dir, rid, args.ack_date)
            if rec:
                acked.append(rec["key"])
        # Ack-advance: stamp WHEN this ack landed into the stagger clock's file, so the fire path can
        # release its 15-min hold ~2 min after the owner's last ack instead of waiting it out. Stamped
        # here because every ack route ends in this function; a --no-ack-record cancel is not an ack and
        # does not advance anything.
        ack_at = ra.record_ack_instant(args.state_dir)
    # ``activity_day`` is additive: it names the day the removal was scoped to, so a caller reading this
    # output can see WHY a nudge it expected to go is still queued.
    print(json.dumps({"ok": True, "removed": len(removed), "ids": removed_ids,
                      "acked": acked, "ack_at": ack_at, "activity_day": day.isoformat(),
                      "path": path}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
