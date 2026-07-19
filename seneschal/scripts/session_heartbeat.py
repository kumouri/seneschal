#!/usr/bin/env python3
"""Write / clear / report entries in the assistant's **session registry** (``state/sessions/``).

Phase 2 of the live-session work: one small JSON entry per live session so the resident ``presence.py``
daemon knows when an interactive ``/assistant`` chat is engaged (and **defers** noise into it — holds, never
drops, non-piercing nudges; skips the redundant Watch comms-peek; ``Call Me`` + Critical-and-above still
pierce), and so any session can see **who else is live and what each is doing** (the ``working_on``
string). Only ``daemon``/``desktop`` sources gate delivery; ``build``/``scheduled`` entries are
awareness-only. See ``seneschal/references/reminders-policy.md`` → "Live-session defer."

The **daemon's warm Telegram/Discord session** writes its entry directly (``sentinel.
write_session_heartbeat``, refreshed around every turn, cleared on wind-down) — the reliable path. This
CLI is for a **desktop ``/assistant``** session to register itself: a slash command is just a prompt, so
there's no session-exit hook — the refresh is best-effort and a closed/forgotten session simply ages out
of the TTL (``SESSION_TTL_SEC``, 120 s). The machine-wide ``session_stamp.py`` hook covers every *other*
Claude Code session automatically. Writing an entry is **act-low** — the assistant telling itself "a
human's engaged, hold the buzzes."

Stdlib only. ``last_seen`` / ``started_at`` are stored as owner-local ISO-8601 (via ``tz_common`` when
resolvable); the liveness math re-normalizes to UTC, so the stored zone is cosmetic.

Usage:
  python session_heartbeat.py --source desktop --working-on "phase-2 design chat"   # create/refresh
  python session_heartbeat.py --status              # list live entries + the gate verdict
  python session_heartbeat.py --clear               # remove this session's entry (session ended)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from sentinel import (
    DEFAULT_STATE_DIR,
    SESSION_TTL_SEC,
    SESSION_VISIBLE_TTL_SEC,
    clear_session_heartbeat,
    list_live_sessions,
    session_is_live,
    write_session_heartbeat,
)


def main() -> int:
    p = argparse.ArgumentParser(description="Write/clear/report the assistant's session-registry entries.")
    p.add_argument("--source", default="desktop", choices=["desktop", "daemon", "build", "scheduled"],
                   help="what kind of session this is: desktop (an /assistant slash session; default), "
                        "daemon (the resident warm chat), build (an unrelated Claude Code session), "
                        "or scheduled (a Brief/Wrap/Dream/Journal run). Only desktop/daemon gate nudges.")
    p.add_argument("--session-id", default=None,
                   help="stable id for this session's registry entry (default: the source name — fine "
                        "for the singleton daemon/desktop surfaces; pass an explicit id to keep several "
                        "same-source sessions distinct)")
    p.add_argument("--working-on", default=None,
                   help="a few words on what this session is doing right now (shown to other sessions; "
                        "carried forward on refresh when omitted)")
    p.add_argument("--phase", default=None, choices=["active", "idle"],
                   help="optionally mark the entry active (turn in flight) or idle")
    p.add_argument("--clear", action="store_true", help="remove this session's entry (session ended)")
    p.add_argument("--status", action="store_true", help="report live entries + gate verdict and exit")
    p.add_argument("--ttl-seconds", type=int, default=SESSION_TTL_SEC,
                   help=f"gate liveness window for --status (default {SESSION_TTL_SEC})")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="dir holding the sessions/ registry")
    args = p.parse_args()

    if args.status:
        now = datetime.now(timezone.utc)
        live = session_is_live(args.state_dir, now, args.ttl_seconds)
        sessions = list_live_sessions(args.state_dir, now, SESSION_VISIBLE_TTL_SEC)
        print(json.dumps({"ok": True, "live": live, "sessions": sessions}))
        return 0

    if args.clear:
        removed = clear_session_heartbeat(args.state_dir, args.source, args.session_id)
        print(json.dumps({"ok": True, "cleared": removed}))
        return 0

    state = write_session_heartbeat(args.state_dir, args.source, session_id=args.session_id,
                                    working_on=args.working_on, phase=args.phase)
    print(json.dumps({"ok": True, **state}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
