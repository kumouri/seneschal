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


def main() -> int:
    p = argparse.ArgumentParser(description="Enqueue a daemon control request (restart/shutdown).")
    p.add_argument("--action", required=True, choices=["restart", "shutdown"])
    p.add_argument("--reason", default=None)
    p.add_argument("--now", action="store_true",
                   help="apply ASAP (defer_until_idle=false) — may interrupt a live chat")
    p.add_argument("state_dir", nargs="?", default=DEFAULT_STATE_DIR)
    args = p.parse_args()
    path = enqueue_control(args.state_dir, args.action, args.reason, defer=not args.now)
    print(f"control '{args.action}' queued: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
