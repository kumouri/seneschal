#!/usr/bin/env python3
"""Machine-wide Claude Code hook → stamp THIS session into the assistant's session registry.

Wired globally in the user's ``~/.claude/settings.json`` (SessionStart / UserPromptSubmit / Stop /
SessionEnd → ``python <repo>/seneschal/scripts/session_stamp.py``; see ``SCHEDULING.md`` → "Session
registry hooks"), so **every** Claude Code session on the box — an ``/assistant`` chat, an unrelated
build session, the daemon's own spawned runs — is visible in ``state/sessions/`` with what it's working
on (cwd + git branch). That upgrades "don't nudge into a live chat" into "don't collide with the session
editing the main tree" (the 2026-07-14 near-miss).

Hook entries are written with ``source="build"`` — **awareness-only**: they inform other sessions and
the daemon's logs, but never gate reminder delivery (only ``daemon``/``desktop`` sources defer nudges —
see the "session registry" section of ``sentinel.py``). An ``/assistant`` desktop session additionally
self-stamps a ``desktop`` entry via ``session_heartbeat.py``; the two coexist by design.

On **SessionEnd** this hook also fire-and-forgets the **mini-dream** distiller (phase 2b,
``mini_dream.py``): the ended session's transcript is distilled into
``state/session-distillations.jsonl`` — the shared cross-instance memory every assistant surface reads
at orientation. Detached spawn, recursion-guarded, salience-gated inside the distiller.

Hook contract (Claude Code): the event arrives as JSON on stdin (``hook_event_name``, ``session_id``,
``cwd``, …). This script MUST print nothing (SessionStart/UserPromptSubmit stdout is injected into the
session's context) and MUST always exit 0 (a broken courtesy signal must never break a session) — so
everything is wrapped fail-silent. The registry lives in the state dir resolved **relative to this
script** (``../state``), so the absolute settings.json path into the daemon's live checkout lands
entries in the shared state dir no matter which project the session runs in. Stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
# Windows: run our git sub-call with a HIDDEN console. This hook fires on every session event, sometimes
# from a console-less parent (a headless `claude` the daemon spawned), where a console child like git would
# otherwise flash a visible window. CREATE_NO_WINDOW hides it; captured stdio is unaffected. 0 (no-op) off
# Windows. (The mini-dream spawn below stays DETACHED_PROCESS — it must SURVIVE the session ending, not
# merely hide.)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HOOK_PID = 0  # hook processes are one-shot (a fresh pid per event) — a fixed marker pid keeps
              # `started_at` stable across the events of one session instead of resetting every stamp

# Registry writes for each event. SessionEnd removes the entry; anything unlisted is ignored.
_WRITE_EVENTS = {"SessionStart": "idle", "UserPromptSubmit": "active", "Stop": "idle"}


def _git_branch(cwd: str) -> str | None:
    """The checked-out branch of ``cwd``'s repo, or None (not a repo / git absent / slow). Always runs
    with fsmonitor off — a Git GUI's fsmonitor integration can hang git in some repos — and a hard
    timeout, because a hook must never stall a session on a git quirk."""
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=3, creationflags=_NO_WINDOW)
        return (proc.stdout or "").strip() or None
    except Exception:  # noqa: BLE001 — fail-silent by contract
        return None


def maybe_spawn_mini_dream(event: dict, state_dir: str = STATE_DIR) -> bool:
    """On SessionEnd, fire-and-forget the mini-dream distiller (phase 2b) over the ended session's
    transcript — detached, so the hook returns instantly and the session teardown never waits on a
    distill (which may itself run a headless LLM). Skipped when RECURSION_ENV is set (we ARE a
    distiller's child — never dream the dreamer) or when the event carries no usable transcript.
    Returns whether a distiller was spawned (for tests)."""
    import mini_dream  # sibling import, deferred like sentinel

    if os.environ.get(mini_dream.RECURSION_ENV):
        return False
    transcript = event.get("transcript_path")
    sid = event.get("session_id")
    if not (transcript and sid and os.path.exists(transcript)):
        return False
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    subprocess.Popen(
        [sys.executable, os.path.join(SCRIPT_DIR, "mini_dream.py"),
         "--transcript", transcript, "--session-id", sid,
         "--cwd", event.get("cwd") or "", "--state-dir", state_dir],
        **kwargs)
    return True


def handle_event(event: dict, state_dir: str = STATE_DIR) -> str:
    """Apply one hook event to the registry. Returns the action taken (``written`` / ``cleared`` /
    ``ignored``) — for tests and for nothing else; the hook entrypoint discards it."""
    import sentinel  # sibling import, deferred so an import-time sentinel problem can't crash the hook

    name = event.get("hook_event_name") or ""
    sid = event.get("session_id") or None
    if not sid:
        return "ignored"
    if name == "SessionEnd":
        sentinel.clear_session_heartbeat(state_dir, source="build", session_id=sid)
        try:
            maybe_spawn_mini_dream(event, state_dir)
        except Exception:  # noqa: BLE001 — the distill is a bonus; the registry clear is the contract
            pass
        return "cleared"
    if name not in _WRITE_EVENTS:
        return "ignored"
    cwd = event.get("cwd") or os.getcwd()
    branch = _git_branch(cwd)
    label = os.path.basename(os.path.normpath(cwd)) or cwd
    sentinel.write_session_heartbeat(
        state_dir, "build", pid=HOOK_PID, session_id=sid,
        working_on=(f"{label} @ {branch}" if branch else label),
        cwd=cwd, branch=branch, phase=_WRITE_EVENTS[name])
    return "written"


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if SCRIPT_DIR not in sys.path:
            sys.path.insert(0, SCRIPT_DIR)
        handle_event(event)
    except Exception:  # noqa: BLE001 — never block or noise a session; the registry is a courtesy
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
