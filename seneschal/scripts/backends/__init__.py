#!/usr/bin/env python3
"""The `Backend` contract (`seneschal/docs/pluggable-backend-spec.md` §3.1) — the shape `WarmSession` and
`StubWarmSession` already shared informally before this package existed (the stub's existence was the
strongest evidence that a Backend abstraction was already latent). This package names that shape
explicitly and holds every implementation:

  * `claude_cli.py`  — `WarmSession`/`StubWarmSession`, moved here unchanged in behavior from
    `presence.py`. The reference implementation; `presence.py` imports both names back from here so every
    existing caller (`presence.WarmSession`, many test modules) is unaffected by the move.
  * `codex_cli.py`   — `CodexWarmSession`, the second implementation (the OpenAI Codex CLI on a ChatGPT
    subscription), selected by the daemon's backend dial.

## The contract (duck-typed, not an enforced ABC — matching the pre-existing style)

Every backend session exposes:

    start() -> None
    send(text, on_event=None, cold_retry_text=None) -> str | None
    close() -> None
    age_sec() -> float | None

    session_id: str | None
    resumed: bool
    resume_failed: bool
    timed_out: bool
    turns_served: int
    last_usage: dict | None
    last_num_turns: int | None          # None where the backend has no such concept (e.g. codex-cli)
    context_tokens: int | None
    session_cost_usd: float | None      # None where the backend reports no per-call dollar cost
    last_turn_cost_usd: float | None
    cost_model: "subscription" | "metered_api"
    fallback_alerted: bool
    _on_opened: callable | None         # set by presence.py's make_session() after construction

`presence.py` never introspects a backend beyond `getattr(session, "...", default)` reads and the
four methods above, which is what makes a third implementation a `make_session()` branch instead of a
rewrite.

## Safety is a gate on which backends may reach outbound tools at all (spec §3.1, §1.4)

`send_gate_hook.py`'s `PreToolUse` interception only covers the Claude Code CLI's own hook surface.
Codex's equivalent MCP-tool coverage is unverified, so **`codex_cli.py` sidesteps the question rather
than resolving it**: it contains no code path that can add an MCP server to a codex spawn at all
(unlike `WarmSession.start()`'s per-invocation `--mcp-config`, Codex reads MCP servers from a STANDING
file, `~/.codex/config.toml`, that only a host-side step — `CODEX_SETUP.md` — ever writes, and that doc
grants the store's MCP only, never an outbound-send server). So a codex-backed assistant has the store
+ shell + file tools because the host config grants them, and no MCP send tool to call in the first
place, by construction — a stricter, simpler guarantee than trusting an unverified hook. `send_gate.py`'s
code-level gate is unaffected either way: it lives inside the outbound SCRIPTS themselves, reachable
from any backend's shell tool exactly as today, so an outbound script call is gated identically on
every backend.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))            # seneschal/scripts/backends
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "..", ".."))  # repo root, 3 hops up

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: no visible console per grandchild
                                                         # spawn (same rationale as sentinel.NO_WINDOW)


class _TurnWatchdog:
    """The per-turn idle-gap deadline (`../../docs/hung-turn-deadline-spec.md` P1): kill the child
    when it has produced NOTHING for `gap_sec`. Moved here verbatim from `presence.py` (unchanged
    behavior) because both `claude_cli.WarmSession` and `codex_cli.CodexWarmSession` need the same
    "is this child still producing output?" watchdog around their own subprocess reads — it operates
    only on a Popen's `.stdout`/`.kill()`, which is backend-agnostic by construction. See
    `presence.py`'s own (pre-move) docstring for the full "why a watchdog and not queue.get(timeout=)"
    reasoning; nothing about that reasoning changed with the move."""

    def __init__(self, proc, gap_sec: float, log=None):
        self._proc = proc
        self._gap = gap_sec
        self._log = log
        self._wake = threading.Event()   # set by cancel() — ends the wait immediately
        self._last = time.monotonic()    # last sign of life; written by beat(), read by the thread
        self._thread = None
        self.fired = False               # the watchdog killed the child (read after the loop ends)

    def start(self) -> "_TurnWatchdog":
        if not self._gap or self._gap <= 0 or self._proc is None:
            return self
        try:
            self._thread = threading.Thread(target=self._run, name="warm-turn-watchdog", daemon=True)
            self._thread.start()
        except Exception:  # noqa: BLE001 — no watchdog is strictly better than no turn
            self._thread = None
        return self

    def beat(self) -> None:
        """Called from the stdout reader for every line. Kept to one store — this is the hot path."""
        self._last = time.monotonic()

    def cancel(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        try:
            while not self._wake.is_set():
                remaining = self._gap - (time.monotonic() - self._last)
                if remaining <= 0:
                    # Re-check rather than fire straight off the expired wait: a beat can land between
                    # the two, and killing a child that just spoke would be the one way this feature
                    # could cost a healthy turn.
                    if time.monotonic() - self._last >= self._gap:
                        self._fire()
                        return
                    continue
                self._wake.wait(remaining)
        except Exception:  # noqa: BLE001 — see the class docstring: never at the turn's expense
            pass

    def _fire(self) -> None:
        self.fired = True
        try:
            if self._log:
                self._log(f"! warm turn produced no output for {self._gap:.0f}s — killing the session so "
                          "the turn ends (idle-gap deadline, docs/hung-turn-deadline-spec.md)")
        except Exception:  # noqa: BLE001
            pass
        try:
            self._proc.kill()  # closes stdout → the read loop ends → the turn returns None
        except Exception:  # noqa: BLE001
            pass
