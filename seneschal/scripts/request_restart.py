#!/usr/bin/env python3
"""Ask the resident presence daemon to restart itself — the SAFE way to do a "reseneschald" from chat.

Running seneschald-control.ps1 (or otherwise killing the daemon) from inside the assistant's warm session kills the
daemon MID-TURN: its reply never lands, and because the message is only popped from the durable queue on
delivery, it is re-queued and replayed on every boot — a self-kill loop (see presence.py MAX_TURN_ATTEMPTS
and the 2026-07-03 incident). Instead this enqueues a graceful **restart control** (via request_control.py)
that the daemon honors only *after* it has delivered the current reply AND its warm session has wound down
(so it never interrupts a live conversation), then re-execs itself with code reloaded from disk — no
dropped message.

Thin wrapper kept for back-compat: the chat grounding tells the assistant to run this. It now writes to
state/control-queue.json (action=restart, defer_until_idle=true).

Usage:  python request_restart.py [STATE_DIR]
"""
from __future__ import annotations

import sys

from request_control import enqueue_control
from sentinel import DEFAULT_STATE_DIR


def main() -> int:
    state_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_STATE_DIR
    path = enqueue_control(state_dir, "restart", reason="reseneschald (chat)", defer=True)
    print(f"restart requested (queued, graceful): {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
