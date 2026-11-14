#!/usr/bin/env python3
"""Enqueue a control request for the resident presence daemon — graceful restart / shutdown.

Generalizes request_restart.py. Instead of a bare restart sentinel, this appends a **flagged** control
entry to state/control-queue.json. The daemon applies it only once its warm chat session has wound down
and the action queue is empty (so a merge-triggered restart never interrupts an in-progress conversation)
— unless --now is passed. The `action` field is extensible: `restart` re-execs the daemon (reloading code
pulled from main); `shutdown` exits cleanly (pair with `seneschald-control.ps1 -Action Stop` to keep the
scheduled task down — a bare exit would be relaunched).

Usage:
  python request_control.py --action restart [--reason "..."] [--now] [STATE_DIR]
  python request_control.py --action shutdown [--reason "..."]
  python request_control.py --action shutdown --withdraw --reason "..." [STATE_DIR]

`--withdraw` takes back what one caller queued: it removes the entries of that action whose reason is
EXACTLY the one given, and nothing else. A Stop that queued a graceful `shutdown --now` and then had to
kill the daemon uses it, because a shutdown nobody consumed would sit at the head of the queue and make
the next start exit at once (docs/service-supervision-spec.md §3).

seneschald-control.ps1 calls through here for every graceful restart it asks for: a merge landing on
the deploy branch, a deferred restart once a failed dep sync recovers, and the login-deploys credential
check (`Invoke-CredentialCheck`), which enqueues the SAME restart a merge does — capped by its own
`credential_restart_for` loop guard at one restart per account identity.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sentinel import DEFAULT_STATE_DIR, load_json, save_json

# Must match presence.CONTROL_QUEUE.
CONTROL_QUEUE = "control-queue.json"


def enqueue_control(state_dir: str, action: str, reason: str | None = None, defer: bool = True) -> str:
    """Append a control entry to state/control-queue.json (deduped by action) and return the path."""
    import os
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, CONTROL_QUEUE)
    items = load_json(path, [])
    if not isinstance(items, list):
        items = []
    # Dedupe: don't stack identical pending actions (many merges in a row → still one restart).
    if any(isinstance(i, dict) and i.get("action") == action for i in items):
        return path
    items.append({
        "action": action,
        "defer_until_idle": bool(defer),
        "reason": reason or "",
        "requested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    save_json(path, items)
    return path


def withdraw_control(state_dir: str, action: str, reason: str) -> int:
    """Remove the queued `action` entries whose reason is exactly `reason`. Returns how many went.
    Another caller's entry (any other reason) is left alone; a missing or unreadable queue is 0."""
    import os
    path = os.path.join(state_dir, CONTROL_QUEUE)
    items = load_json(path, [])
    if not isinstance(items, list) or not items:
        return 0
    keep = [i for i in items
            if not (isinstance(i, dict) and i.get("action") == action and (i.get("reason") or "") == reason)]
    removed = len(items) - len(keep)
    if removed:
        save_json(path, keep)
    return removed


def queued(state_dir: str, action: str, reason: str) -> bool:
    """Is an `action` entry with exactly this reason waiting in the queue?"""
    import os
    items = load_json(os.path.join(state_dir, CONTROL_QUEUE), [])
    return isinstance(items, list) and any(
        isinstance(i, dict) and i.get("action") == action and (i.get("reason") or "") == reason for i in items)


def main() -> int:
    p = argparse.ArgumentParser(description="Enqueue a daemon control request (restart/shutdown).")
    p.add_argument("--action", required=True, choices=["restart", "shutdown"])
    p.add_argument("--reason", default=None)
    p.add_argument("--now", action="store_true",
                   help="apply ASAP (defer_until_idle=false) — may interrupt a live chat")
    p.add_argument("--withdraw", action="store_true",
                   help="remove the queued entries of --action whose reason is exactly --reason")
    p.add_argument("state_dir", nargs="?", default=DEFAULT_STATE_DIR)
    args = p.parse_args()
    if args.withdraw:
        if not args.reason:
            p.error("--withdraw needs --reason (it only ever takes back one caller's own entry)")
        n = withdraw_control(args.state_dir, args.action, args.reason)
        print(f"control '{args.action}' withdrawn: {n} entr{'y' if n == 1 else 'ies'}")
        return 0
    path = enqueue_control(args.state_dir, args.action, args.reason, defer=not args.now)
    print(f"control '{args.action}' queued: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
