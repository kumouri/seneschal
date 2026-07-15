#!/usr/bin/env python3
"""Open, extend, lift, or report the assistant's *quiet window* (do-not-disturb for nudges).

When the owner asks in chat to hush the nudges ("quiet till morning", "no nudges for two hours"), Chat mode
runs this to write a durable window into ``seneschal/state/quiet.json``. The always-on ``presence.py`` daemon
checks it at the single delivery chokepoint (``sentinel.check_reminders``): while the window is open it
**drops** every due nudge that doesn't pierce — ⭐ High and below are consumed, never delivered late — so
a rebuilt queue (a slot re-deriving the same nudges, a standing roll re-seeding) can't leak a buzz. What
still comes through: **Call Me** (a phone ring) and **Critical-and-above** items (the brain marks those
``--pierce-quiet`` at enqueue). See ``seneschal/references/reminders-policy.md`` → "Quiet window".

Setting a quiet window is **act-low** — the assistant suppressing its own pushes, reversible, bounded.

Stdlib only. Times stored as UTC ISO-8601 ``Z``. "Local" means the owner's timezone via ``tz_common``
(the configured identity zone when resolvable, else the daemon machine's wall clock).

Usage:
  python quiet_set.py --until-morning                 # hush until the next 08:00 local (Morning slot)
  python quiet_set.py --minutes 120 --reason "nap"    # hush for two hours
  python quiet_set.py --until-local 07:30             # hush until the next 07:30 local
  python quiet_set.py --until 2026-07-09T12:00:00Z    # hush until an explicit UTC instant
  python quiet_set.py --status                        # report the current window (JSON)
  python quiet_set.py --clear                         # lift it now ("you can nudge me again")
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import tz_common
from sentinel import DEFAULT_STATE_DIR, clear_quiet, load_quiet, parse_iso, set_quiet


def _next_local(hour: int, minute: int, now_local: datetime) -> datetime:
    """The next future wall-clock ``hour:minute`` in ``now_local``'s local zone (today, else tomorrow).
    Returns an aware local datetime; ``set_quiet`` converts it to UTC."""
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_local:
        target += timedelta(days=1)
    return target


def main() -> int:
    p = argparse.ArgumentParser(description="Open/lift/report the assistant's quiet (do-not-disturb) window.")
    when = p.add_mutually_exclusive_group()
    when.add_argument("--until", help="explicit UTC ISO-8601 instant (e.g. 2026-07-09T12:00:00Z)")
    when.add_argument("--minutes", type=int, help="hush for N minutes from now")
    when.add_argument("--until-local", metavar="HH:MM", help="hush until the next local wall-clock HH:MM")
    when.add_argument("--until-morning", action="store_true",
                      help="hush until the next 08:00 local (the Morning reminders slot)")
    p.add_argument("--reason", default="", help="free-text note (e.g. 'nap', 'bed early')")
    p.add_argument("--set-by", default="chat", help="who set it (chat | manual); default chat")
    p.add_argument("--clear", action="store_true", help="lift any active window now")
    p.add_argument("--status", action="store_true", help="report the current window and exit")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="dir holding quiet.json")
    args = p.parse_args()

    now_utc = datetime.now(timezone.utc)

    if args.status:
        q = load_quiet(args.state_dir)
        if not q:
            print(json.dumps({"ok": True, "quiet": False}))
            return 0
        try:
            active = now_utc < parse_iso(q["until"])
        except (ValueError, TypeError):
            active = False
        print(json.dumps({"ok": True, "quiet": active, **q}))
        return 0

    if args.clear:
        removed = clear_quiet(args.state_dir)
        print(json.dumps({"ok": True, "cleared": removed}))
        return 0

    # Resolve the target instant from whichever --when option was given. "Local" for the
    # --until-local/--until-morning math is the OWNER's clock (tz_common): "hush until 08:00"
    # means the owner's 08:00, machine-local only as the unconfigured fallback.
    now_local = tz_common.local_now()
    if args.until:
        try:
            until = parse_iso(args.until)
        except ValueError as e:
            print(json.dumps({"ok": False, "error": f"bad --until: {e}"}))
            return 2
    elif args.minutes is not None:
        if args.minutes <= 0:
            print(json.dumps({"ok": False, "error": "--minutes must be positive"}))
            return 2
        until = now_utc + timedelta(minutes=args.minutes)
    elif args.until_local:
        try:
            hh, mm = (int(x) for x in args.until_local.split(":", 1))
        except ValueError:
            print(json.dumps({"ok": False, "error": "bad --until-local, want HH:MM"}))
            return 2
        until = _next_local(hh, mm, now_local)
    elif args.until_morning:
        until = _next_local(8, 0, now_local)
    else:
        print(json.dumps({"ok": False, "error": "give one of --until/--minutes/--until-local/"
                                                 "--until-morning, or --clear/--status"}))
        return 2

    state = set_quiet(args.state_dir, until, reason=args.reason, set_by=args.set_by)
    print(json.dumps({"ok": True, "quiet": True, **state}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
