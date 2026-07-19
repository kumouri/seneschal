#!/usr/bin/env python3
"""The assistant's sentinel helpers + a no-LLM one-shot check. Standard library only.

**Role (since the presence daemon):** the always-on heartbeat is now `presence.py` (a resident process
that owns Telegram chat + reminders + the comms-peek). This module is kept as (a) the **shared helper
library** `presence.py` imports — `parse_iso`, `load_json`/`save_json`, `send_telegram`, `poll_telegram`,
`check_reminders` — and (b) a **manual one-shot**: run it by hand (or as a fallback scheduled task) to
fire any due reminders and optionally trigger a comms peek, without a resident daemon.

Telegram inbound is owned by `presence.py` (it consumes + commits the offset). This one-shot does **not**
touch Telegram, to avoid contending with the daemon over the update offset.

Times are compared as UTC instants; the brain writes reminder `due_at` as UTC ISO (e.g.
"2026-06-29T20:00:00Z") when it interprets "remind me at 3pm" in the owner's timezone. This stays
timezone-dumb on purpose (only the fire-time ack date consults the owner zone, via
reminders_acks.local_today → tz_common).

USAGE:
  python sentinel.py                                   # fire due reminders + report
  python sentinel.py --peek-interval-min 5 --launch-cmd "claude -p 'Run Watch mode'"  # + a comms peek
  python sentinel.py --no-fire-reminders               # report due reminders without sending
  python sentinel.py --now 2026-06-29T20:01:00Z        # override clock (testing)

Prints a one-line JSON verdict; also writes it to state/last-signal.json. Exit 0 on success (with or
without a peek due), 10 if a comms peek fired, 1 on error.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone

import reminders_acks as ra  # durable ack ledger — the fire-time "already did it" gate (sibling, stdlib)

try:  # owner-timezone rendering for session-registry stamps (guarded — presence.py house style;
    # already a transitive dependency via reminders_acks, so this can only fail on a broken install)
    import tz_common as _tz_common
except ImportError:  # pragma: no cover — a bare interpreter still stamps machine-local
    _tz_common = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
DEFAULT_TELEGRAM_ENV = os.path.join(SCRIPT_DIR, "telegram.env")
EXIT_WORK = 10

# Windows: spawn console children (the sibling helper CLIs, `claude`) with no console window of their
# own — a console-subsystem child of a console-less parent (the detached presence daemon imports these
# helpers) otherwise pops a fresh visible console on every send/poll. 0 off Windows (a no-op flag).
# Guarded by test_windowless_spawns.py: every subprocess spawn in the daemon tree must pass it.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TELEGRAM_MESSAGE_MAP = "telegram-message-map.json"  # message_id -> what the assistant sent (reaction context)
MESSAGE_MAP_CAP = 200  # newest N kept; a reaction to anything older reads as "an earlier message"
# Where inbound Telegram attachments land, relative to the state dir (state/inbox/). The daemon passes
# it as poll_telegram(download_dir=...); telegram_poll.py --prune-days sweeps it nightly (Dream).
TELEGRAM_INBOX_DIR = "inbox"

# Catch-up stagger. When a defer-release (the owner wakes / gets home / stops driving) or a plain backlog lets
# several nudges come due in one pass, firing them all at once is the "wall of N nudges at 2:12" we
# retired (reminders-stagger-not-batch — bunching overwhelms an Autistic+ADHD brain). So a NON-piercing
# nudge fires only when at least this long has elapsed since the last non-piercing fire (and at most one
# per pass); the rest stay pending and drip out one at a time on later ticks. Piercing items (Call Me /
# Critical / meds `pierce_quiet`) bypass the gate — they can't slide. Fresh nudges are already spaced
# 15-30 min apart at creation (the slots stagger due_at), so normal-day timing is untouched; this only
# reshapes a bunched-up *release*. Held = stamped with nothing → re-checked next loop (defer, never drop).
STAGGER_STATE_FILE = "nudge-stagger.json"
CATCHUP_STAGGER_SEC = 15 * 60


def parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 instant, accepting a trailing 'Z'. Returns an aware UTC datetime."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_last_nudge_fire(state_dir: str) -> datetime | None:
    """Instant we last delivered a NON-piercing nudge (the catch-up stagger clock). Absent/unparseable →
    None: fail-open, so the next non-piercing nudge fires immediately rather than hanging on a bad file."""
    data = load_json(os.path.join(state_dir, STAGGER_STATE_FILE), None)
    if isinstance(data, dict) and data.get("last_nonpiercing_fire"):
        try:
            return parse_iso(data["last_nonpiercing_fire"])
        except (ValueError, TypeError):
            return None
    return None


def _save_last_nudge_fire(state_dir: str, now: datetime) -> None:
    save_json(os.path.join(state_dir, STAGGER_STATE_FILE),
              {"last_nonpiercing_fire": now.isoformat().replace("+00:00", "Z")})


def _due_sort_key(r: dict) -> float:
    """Fire order = oldest-due first, so a released backlog drips out in the order it came due (and the
    single nudge that fires each pass is the most overdue one). Missing/bad due_at sorts last."""
    if not isinstance(r, dict) or not r.get("due_at"):
        return float("inf")
    try:
        return parse_iso(r["due_at"]).timestamp()
    except (ValueError, TypeError):
        return float("inf")


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- quiet window (do-not-disturb)
#
# A durable "hush the nudges" state, checked at the single delivery chokepoint (`check_reminders`).
# The owner asks in chat ("quiet till morning"); Chat mode writes state/quiet.json via quiet_set.py. The
# gate lives HERE at delivery — not at enqueue — on purpose: slots and rolls keep enqueuing whatever
# they compute, but nothing *fires* while quiet is active, so a rebuilt queue (the old bug: a "quiet
# tonight" sweep undone by the next slot re-deriving the same nudges) can't leak a buzz. Suppression is
# **drop, not defer** — a suppressed nudge is consumed (`suppressed_at` stamped), never delivered late,
# so waking up doesn't trigger an avalanche of everything slept through.
#
# The pierce set — what still comes through while quiet — is `Call Me` (a phone ring; channel=call or an
# escalating entry) plus Critical-and-above items (the brain marks those `pierce_quiet` at enqueue,
# where it can read the ⏰ row's Importance; the daemon stays Notion-dumb and just reads the flag).
QUIET_FILE = "quiet.json"


def load_quiet(state_dir: str) -> dict | None:
    """Return the quiet-state dict (``{"until": <utc iso>, ...}``), or None if absent/malformed."""
    data = load_json(os.path.join(state_dir, QUIET_FILE), None)
    return data if isinstance(data, dict) and data.get("until") else None


def is_quiet(state_dir: str, now: datetime) -> bool:
    """True while a quiet window is active (``now`` is before its ``until`` instant). A missing file,
    malformed state, or an ``until`` already in the past all read as *not quiet* (fail-open — a broken
    quiet file must never silence a genuine nudge)."""
    q = load_quiet(state_dir)
    if not q:
        return False
    try:
        return now < parse_iso(q["until"])
    except (ValueError, TypeError):
        return False


def entry_pierces_quiet(entry: dict) -> bool:
    """True if this queued reminder should fire even during a quiet window: a phone ring (`Call Me` —
    channel=call or an escalating call) or an item the brain marked Critical-and-above (`pierce_quiet`).
    Everything else (⭐ High and below) is dropped for the duration."""
    if not isinstance(entry, dict):
        return False
    channel = (entry.get("channel") or "telegram").lower()
    return channel == "call" or bool(entry.get("escalate")) or bool(entry.get("pierce_quiet"))


def _presence_context_fresh(ctx: dict, now: datetime, max_age_sec: int = 6 * 3600) -> bool:
    """True if ``state/presence-context.json`` is recent enough to gate a place-locked reminder on.
    Absent / stale / unparseable → False (**fail-open**: a place-gated nudge must never hang forever on a
    dead presence feed — if we can't trust the location, we fire rather than hold)."""
    if not isinstance(ctx, dict) or not ctx.get("updated_at"):
        return False
    for fmt in ("%Y-%m-%d %H:%M:%SZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            u = datetime.strptime(ctx["updated_at"], fmt).replace(tzinfo=timezone.utc)
            return (now - u).total_seconds() <= max_age_sec
        except ValueError:
            continue
    return False


def set_quiet(state_dir: str, until: datetime, reason: str = "", set_by: str = "chat") -> dict:
    """Open (or extend) a quiet window until ``until`` (an aware datetime). Returns the written state."""
    state = {
        "until": until.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "set_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "set_by": set_by,
        "reason": reason,
    }
    save_json(os.path.join(state_dir, QUIET_FILE), state)
    return state


def clear_quiet(state_dir: str) -> bool:
    """Lift any quiet window (delete state/quiet.json). Returns True if a file was removed."""
    path = os.path.join(state_dir, QUIET_FILE)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------- session registry (who's live, on what)
#
# Phase 2 of the live-session work (phase 1 was a single-file heartbeat): a **multi-session registry** —
# one small JSON entry per live session under `state/sessions/` — so several sessions can be visible at
# once (the daemon's warm Telegram/Discord chat, a desktop `/assistant`, an unrelated Claude Code build
# session stamped by the machine-wide `session_stamp.py` hook), each carrying a short `working_on` string
# it refreshes. Two consumers, two questions:
#   * The DELIVERY GATE (`session_is_live`) — "is a human actively engaged in an interactive /assistant
#     chat?" Only GATING_SOURCES (`daemon`/`desktop`) count, fresh within SESSION_TTL_SEC. While one is
#     live the daemon **defers** noise INTO the session rather than dropping it: a non-piercing due nudge
#     is HELD (never fired, never dropped) so it re-evaluates on the next ~5 s tick and fires naturally
#     once the session ages out, and the redundant Watch comms-peek is skipped. The pierce set is the
#     SAME as the quiet window — `Call Me` + Critical-and-above (`entry_pierces_quiet`) fire immediately
#     regardless — so the gates compose: both active = still no drops, piercing still pierces.
#   * AWARENESS (`list_live_sessions`) — "who's around, and what is each doing?" Every fresh entry, any
#     source, within SESSION_VISIBLE_TTL_SEC. A `build`/`scheduled` entry informs (don't run git surgery
#     on the tree another session is editing — the 2026-07-14 near-miss) but NEVER gates delivery: the
#     owner is coding, not conversing, so reminders still buzz them normally.
#
# Fail-open-SAFE, mirroring quiet: a missing OR malformed OR stale entry reads as *not live*, so the
# ABSENCE of the signal can never block a genuine reminder — the worst a broken registry does is let a
# nudge fire, never silence one. Entries are removed on clean exit (daemon wind-down / the hook's
# SessionEnd) and anything left behind by a crash self-prunes after SESSION_PRUNE_SEC.
SESSION_FILE = "seneschal-session.json"  # phase-1 legacy single file — still READ as a gate fallback so
                                         # a transition-era writer is honored; the registry gets written.
SESSIONS_DIR = "sessions"
SESSION_TTL_SEC = 120  # gate: an interactive entry older than this = no longer actively engaged
SESSION_VISIBLE_TTL_SEC = 3600  # awareness: entries older than this drop out of "who's live" listings
SESSION_PRUNE_SEC = 24 * 3600  # hygiene: entries older than this are deleted opportunistically on write
GATING_SOURCES = frozenset({"daemon", "desktop"})  # only interactive /assistant surfaces defer delivery


def _session_stamp(now: datetime | None) -> str:
    """An owner-local ISO-8601 stamp for a registry entry — the owner's configured timezone via
    ``tz_common`` when resolvable, the machine-local clock otherwise. The liveness math re-normalizes
    to UTC via ``parse_iso`` regardless, so an injected UTC ``now`` (tests) still compares correctly
    and the stored zone is cosmetic."""
    dt = now if now is not None else datetime.now(timezone.utc)
    if _tz_common is not None:
        try:
            return _tz_common.to_local(dt).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001 — a tz hiccup must never cost a heartbeat
            pass
    return dt.astimezone().isoformat(timespec="seconds")


def _sessions_dir(state_dir: str) -> str:
    return os.path.join(state_dir, SESSIONS_DIR)


def _session_id_for(source: str, session_id: str | None) -> str:
    """Normalize a registry id into a safe filename stem. Defaults to the *source* — the daemon and a
    desktop `/assistant` are effectively singletons, so `daemon`/`desktop` are stable ids that survive the
    per-invocation pid churn of a CLI stamp; the machine-wide hook passes the harness session uuid."""
    sid = (session_id or source or "session").strip() or "session"
    return "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in sid)[:80]


def load_session(state_dir: str) -> dict | None:
    """Return the phase-1 legacy single-file heartbeat (``{pid, source, started_at, last_seen}``), or
    None if absent/malformed. Kept as a read-only fallback for the transition window."""
    data = load_json(os.path.join(state_dir, SESSION_FILE), None)
    return data if isinstance(data, dict) and data.get("last_seen") else None


def load_sessions(state_dir: str) -> list:
    """Every registry entry (any freshness, malformed ones skipped), plus the legacy single file mapped
    into the same shape. Freshness filtering is the caller's job (`session_is_live` /
    `list_live_sessions`)."""
    out = []
    try:
        names = sorted(os.listdir(_sessions_dir(state_dir)))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        data = load_json(os.path.join(_sessions_dir(state_dir), name), None)
        if isinstance(data, dict) and data.get("last_seen"):
            out.append(data)
    legacy = load_session(state_dir)
    if legacy:
        out.append({"session_id": legacy.get("source", "legacy"), **legacy})
    return out


def write_session_heartbeat(state_dir: str, source: str, pid: int | None = None,
                            now: datetime | None = None, session_id: str | None = None,
                            working_on: str | None = None, cwd: str | None = None,
                            branch: str | None = None, phase: str | None = None) -> dict:
    """Create or refresh THIS session's registry entry (``state/sessions/<id>.json``). ``last_seen``
    advances every call; ``started_at`` is preserved across refreshes of the same entry (same pid +
    source — a respawn is a fresh window). Optional context (``working_on``/``cwd``/``branch``/``phase``)
    is stored when given and carried forward from the previous write when omitted, so a bare refresh
    never wipes what the session said it was doing. Opportunistically prunes crash-orphaned entries.
    Returns the written state."""
    if pid is None:
        pid = os.getpid()
    sid = _session_id_for(source, session_id)
    stamp = _session_stamp(now)
    path = os.path.join(_sessions_dir(state_dir), sid + ".json")
    prev = load_json(path, None)
    started_at = stamp
    carried = {}
    if isinstance(prev, dict):
        if prev.get("started_at") and prev.get("pid") == pid and prev.get("source") == source:
            started_at = prev["started_at"]
        carried = {k: prev[k] for k in ("working_on", "cwd", "branch", "phase") if prev.get(k) is not None}
    state = {"session_id": sid, "pid": pid, "source": source,
             "started_at": started_at, "last_seen": stamp}
    for key, val in (("working_on", working_on), ("cwd", cwd), ("branch", branch), ("phase", phase)):
        if val is not None:
            state[key] = val
        elif key in carried:
            state[key] = carried[key]
    save_json(path, state)
    prune_sessions(state_dir, now)
    return state


def clear_session_heartbeat(state_dir: str, source: str = "daemon",
                            session_id: str | None = None) -> bool:
    """Remove a session's registry entry (default: the daemon's) on wind-down / shutdown / session end.
    A daemon clear also sweeps the phase-1 legacy single file (the daemon was its owner) so a
    transition-era leftover can't wedge the gate. Returns True if anything was removed."""
    removed = False
    paths = [os.path.join(_sessions_dir(state_dir), _session_id_for(source, session_id) + ".json")]
    if source == "daemon":
        paths.append(os.path.join(state_dir, SESSION_FILE))
    for path in paths:
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            pass
    return removed


def prune_sessions(state_dir: str, now: datetime | None = None,
                   max_age_sec: int = SESSION_PRUNE_SEC) -> int:
    """Delete registry entries whose ``last_seen`` is older than ``max_age_sec`` (or unparseable — a
    malformed entry can't gate anyway, so pruning it is pure hygiene). Crash-orphaned sessions age out
    here; clean exits already removed themselves. Returns the number removed."""
    if now is None:
        now = datetime.now(timezone.utc)
    removed = 0
    try:
        names = os.listdir(_sessions_dir(state_dir))
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(_sessions_dir(state_dir), name)
        data = load_json(path, None)
        drop = True
        if isinstance(data, dict) and data.get("last_seen"):
            try:
                drop = (now - parse_iso(data["last_seen"])).total_seconds() > max_age_sec
            except (ValueError, TypeError):
                drop = True
        if drop:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def session_is_live(state_dir: str, now: datetime | None = None,
                    ttl_seconds: int = SESSION_TTL_SEC) -> bool:
    """True iff ANY interactive `/assistant` session (source in GATING_SOURCES — the daemon's warm chat or a
    desktop slash session) has an entry fresh within ``ttl_seconds``. Build/scheduled entries never gate.
    Fail-open-SAFE: missing / malformed / stale entries read as *not live*, so absence of the signal
    never blocks a reminder."""
    if now is None:
        now = datetime.now(timezone.utc)
    for s in load_sessions(state_dir):
        if s.get("source") not in GATING_SOURCES:
            continue
        try:
            last = parse_iso(s["last_seen"])
        except (ValueError, TypeError, KeyError):
            continue
        if (now - last).total_seconds() < ttl_seconds:
            return True
    return False


def list_live_sessions(state_dir: str, now: datetime | None = None,
                       ttl_seconds: int = SESSION_VISIBLE_TTL_SEC) -> list:
    """Every session entry fresh within ``ttl_seconds`` (default: the 1 h awareness window), any source,
    newest first — the "who's live and what is each doing" read for orientation, logs, and
    collision-avoidance. Purely informational; the delivery gate is `session_is_live`."""
    if now is None:
        now = datetime.now(timezone.utc)
    live = []
    for s in load_sessions(state_dir):
        try:
            last = parse_iso(s["last_seen"])
        except (ValueError, TypeError, KeyError):
            continue
        if (now - last).total_seconds() < ttl_seconds:
            live.append(s)
    live.sort(key=lambda s: s.get("last_seen", ""), reverse=True)
    return live


def branch_is_claimed(state_dir: str, branch: str, now: datetime | None = None,
                      ttl_seconds: int = SESSION_VISIBLE_TTL_SEC) -> bool:
    """True iff some session may still be working on ``branch`` — the guard `seneschald-control.ps1
    -Action Update` asks before reclaiming a live checkout that a session parked off the deploy branch.

    **Fail-CLOSED, on purpose — the inverse of `session_is_live`.** That gate answers "may I interrupt
    the owner?", so absence of signal must mean *fire the reminder*. This one answers "may I move a
    branch out from under someone?", so absence of signal must mean *don't touch it*: an unreadable or
    never-created registry returns True (treat as claimed). The cost of a false "claimed" is one skipped
    auto-heal plus a nudge the owner reads; the cost of a false "free" is yanking the tree from under
    live work.

    Any ``source`` counts (a `build` session is exactly the one likely to be sitting on a feature
    branch), and freshness uses the 1 h *awareness* window rather than the 120 s gating TTL — a session
    idle between prompts still owns its branch. `session_stamp.py` drops the entry on SessionEnd, so an
    ended session stops claiming immediately; a crashed one stops after the TTL lapses.
    """
    if not branch:
        return True
    if not os.path.isdir(_sessions_dir(state_dir)):
        return True  # no registry to consult → we cannot prove the branch is free
    if now is None:
        now = datetime.now(timezone.utc)
    for s in load_sessions(state_dir):
        if s.get("branch") != branch:
            continue
        try:
            last = parse_iso(s["last_seen"])
        except (ValueError, TypeError, KeyError):
            return True  # an entry ON this branch that we cannot date → assume it is live
        if (now - last).total_seconds() < ttl_seconds:
            return True
    return False


def send_telegram(text: str, telegram_env: str) -> dict:
    """Push a message via the sibling telegram_send.py. Returns its parsed JSON result.
    Always returns a dict (never raises) so the daemon can act on {"ok": False} instead of crashing."""
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "telegram_send.py"),
             "--text", text, "--env-file", telegram_env],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "telegram_send.py timed out"}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": proc.stderr.strip() or "telegram_send.py produced no JSON"}


_message_map_lock = threading.Lock()


def load_message_map(state_dir: str) -> dict:
    """`{message_id(str): {kind, text, reminder_id?, sent_at}}` — what the assistant last sent, so an
    inbound reaction can say *what it reacted to*. Telegram's reaction update carries only a message_id,
    never the message; without this a 👍 is context-free. Fail-open: absent/broken reads empty, which
    downgrades a reaction to "an earlier message" — never an error."""
    data = load_json(os.path.join(state_dir, TELEGRAM_MESSAGE_MAP), {})
    if not isinstance(data, dict):
        return {}
    sent = data.get("messages")
    return sent if isinstance(sent, dict) else {}


def record_sent_message(state_dir: str, result: dict, kind: str, text: str,
                        reminder_id: str | None = None, now: datetime | None = None) -> None:
    """Remember an outbound Telegram message by its `message_id` so a later reaction to it has meaning.

    Called on the paths a reaction plausibly lands on: a reminder nudge (which carries the ⏰ row key, so
    a 👍 can ack it) and the assistant's chat replies (so a 👍 on a question reads as "yes"). Best-effort
    by design — the map is context, not truth; a lost entry costs a reaction its quote, nothing more, so
    this never raises into a send path.

    Bounded to MESSAGE_MAP_CAP newest. The lock matters: the reminder tick and the chat drainer both
    reach here from worker threads in the one daemon process, and an unguarded read-modify-write would
    lose entries (and collide on save_json's shared .tmp path)."""
    mid = (result or {}).get("message_id")
    if not (result or {}).get("ok") or mid is None:
        return
    try:
        stamp = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
        entry = {"kind": kind, "text": text, "sent_at": stamp}
        if reminder_id:
            entry["reminder_id"] = reminder_id
        path = os.path.join(state_dir, TELEGRAM_MESSAGE_MAP)
        with _message_map_lock:
            data = load_json(path, {})
            if not isinstance(data, dict) or not isinstance(data.get("messages"), dict):
                data = {"schema": "seneschal.telegram.message-map/1", "messages": {}}
            data["messages"][str(mid)] = entry
            if len(data["messages"]) > MESSAGE_MAP_CAP:
                keep = sorted(data["messages"].items(),
                              key=lambda kv: kv[1].get("sent_at") or "")[-MESSAGE_MAP_CAP:]
                data["messages"] = dict(keep)
            save_json(path, data)
    except Exception:  # noqa: BLE001 — context is a nicety; a nudge/reply must never fail over it
        pass


def poll_telegram(telegram_env: str, state_dir: str, commit: bool = True, timeout: int = 0,
                  download_dir: str | None = None) -> dict:
    """Fetch new inbound messages via telegram_poll.py. With commit=True, advance the offset
    (acknowledge them). timeout = long-poll seconds (0 = single fast call). With download_dir, inbound
    attachments are fetched there (--download-dir) and each message carries an `attachment` record;
    without it they are described but not downloaded. Used by presence.py."""
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "telegram_poll.py"),
           "--env-file", telegram_env,
           "--offset-file", os.path.join(state_dir, "telegram-offset"),
           "--timeout", str(timeout)]
    if commit:
        cmd.append("--commit")
    if download_dir:
        cmd += ["--download-dir", download_dir]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30,
                              creationflags=NO_WINDOW)
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": proc.stderr.strip() or "telegram_poll.py produced no JSON"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "telegram_poll.py timed out"}


def send_call(text: str, call_env: str, escalate: bool = False,
              interval_sec: int | None = None, max_attempts: int | None = None) -> dict:
    """Place a phone call via the sibling push_call.py (Worker /push-call). Returns its parsed JSON.
    With escalate=True the Worker keeps calling back until the owner presses a digit (retry loop is
    Worker-side; push_call.py just kicks it off and returns an escalationId)."""
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "push_call.py"),
           "--text", text, "--env-file", call_env]
    if escalate:
        cmd.append("--escalate")
        if interval_sec is not None:
            cmd += ["--interval-sec", str(interval_sec)]
        if max_attempts is not None:
            cmd += ["--max-attempts", str(max_attempts)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=60, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "push_call.py timed out"}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": proc.stderr.strip() or "push_call.py produced no JSON"}


def send_discord(text: str, discord_env: str) -> dict:
    """Push a message via the sibling discord_send.py. Returns its parsed JSON result.
    Always returns a dict (never raises) so the daemon can act on {"ok": False} instead of crashing."""
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "discord_send.py"),
             "--text", text, "--env-file", discord_env],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "discord_send.py timed out"}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": proc.stderr.strip() or "discord_send.py produced no JSON"}


def poll_discord(discord_env: str, state_dir: str, commit: bool = True) -> dict:
    """Fetch new inbound Discord messages via discord_poll.py (REST after-cursor). With commit=True,
    advance the stored message-id offset. Used by presence.py."""
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "discord_poll.py"),
           "--env-file", discord_env,
           "--offset-file", os.path.join(state_dir, "discord-offset")]
    if commit:
        cmd.append("--commit")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45,
                              creationflags=NO_WINDOW)
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": proc.stderr.strip() or "discord_poll.py produced no JSON"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "discord_poll.py timed out"}


def _deliver_reminder(channel: str, raw: str, telegram_env: str,
                      call_env: str | None, discord_env: str | None,
                      escalate: bool = False, interval_sec: int | None = None,
                      max_attempts: int | None = None):
    """Route a due reminder to its channel. Returns (result_dict, effective_channel).

    A `call` speaks the raw line (no ⏰ prefix — TTS would read the emoji aloud); Telegram/Discord get
    the ⏰-prefixed text. An escalating `call` keeps ringing until the owner presses a digit (Worker-side).
    Any channel that isn't configured (or is unknown) falls back to Telegram so a critical nudge is
    never silently dropped."""
    if channel == "call" and call_env:
        return send_call(raw, call_env, escalate=escalate,
                         interval_sec=interval_sec, max_attempts=max_attempts), "call"
    if channel == "discord" and discord_env:
        return send_discord(f"⏰ Reminder: {raw}", discord_env), "discord"
    return send_telegram(f"⏰ Reminder: {raw}", telegram_env), "telegram"


def check_reminders(state_dir: str, now: datetime, fire: bool, telegram_env: str,
                    call_env: str | None = None, discord_env: str | None = None) -> list:
    """Fire any due, unfired reminders (act-low). Routes each to its `channel`
    (telegram | call | discord; default telegram). Returns signal dicts describing what happened.

    The whole load→deliver→save section holds the cross-process queue lock (ra.queue_lock): since the
    asyncio daemon, this runs in a worker thread WHILE a chat turn's reminders_dequeue.py (or a slot's
    reminders_enqueue.py) may rewrite the same file, and last-writer-wins would erase a fresh fired_at
    (→ double buzz) or resurrect a dequeued nudge. Short timeout — if the lock is somehow wedged,
    firing anyway (fail-open) beats a silent reminder."""
    with ra.queue_lock(state_dir, timeout=10.0):
        return _check_reminders_locked(state_dir, now, fire, telegram_env, call_env, discord_env)


def _check_reminders_locked(state_dir: str, now: datetime, fire: bool, telegram_env: str,
                            call_env: str | None = None, discord_env: str | None = None) -> list:
    path = os.path.join(state_dir, "reminders.json")
    reminders = load_json(path, [])
    if not isinstance(reminders, list):
        return [{"kind": "reminders_error", "detail": "reminders.json is not a list"}]

    quiet = is_quiet(state_dir, now)
    # Live-session gate: while an interactive /assistant chat (the daemon's warm session or a desktop
    # slash session) is engaged, DEFER non-piercing nudges INTO it — held, never dropped — so a buzz
    # doesn't land mid-conversation; it re-checks each tick and fires once the session ages out of TTL.
    # Piercing items (Call Me / Critical) still fire immediately. Read once per pass; fail-open
    # (absent/stale → not live → normal firing) so the signal can only ever hold a nudge briefly, never
    # silence one.
    session_live = session_is_live(state_dir, now)
    # Fire-time ack gate: load the durable ack ledger once and compute today's LOCAL (owner-tz) date.
    # An entry whose ⏰ row (or every member of a digest) was acked today is consumed here instead of
    # delivered — so a nudge staggered before the ack (or baked into a soft-digest the per-id dequeue
    # can't reach) never buzzes for something already done. Fail-open: a missing/broken ledger reads
    # empty, so it can only *suppress* a genuine ack, never silence a real nudge.
    acks = ra.load_acks(state_dir)
    today_local = ra.local_today(now)
    # Presence gate (Phase 1): a place-gated nudge (require_place) is DEFERRED — held, never dropped —
    # until the presence feed says the owner is at that place. Loaded lazily + fail-open, so a presence-subsystem
    # import/read problem can never break reminder firing.
    try:
        from presence_common import read_context as _read_ctx
        from presence_rules import should_defer as _presence_defer
        presence_ctx = _read_ctx(state_dir)
    except Exception:
        _presence_defer, presence_ctx = None, {}
    # Catch-up stagger state: the last non-piercing fire instant (durable, cross-tick) and a per-pass
    # one-fire flag. Together they turn a bunched release into a drip. Iterate oldest-due first so the one
    # nudge that fires each pass is the most overdue, and a backlog drains in the order it came due.
    last_nudge_fire = _load_last_nudge_fire(state_dir)
    fired_nonpiercing = False
    signals, changed = [], False
    for r in sorted(reminders, key=_due_sort_key):
        if r.get("fired_at") or r.get("suppressed_at") or r.get("acked_at") or not r.get("due_at"):
            continue
        try:
            due = parse_iso(r["due_at"])
        except ValueError:
            signals.append({"kind": "reminder_error", "id": r.get("id"), "detail": f"bad due_at {r.get('due_at')!r}"})
            continue
        if due > now:
            continue
        pierces = entry_pierces_quiet(r)
        # Already acked today (chat write-through / slot reconcile stamped the ledger): consume it as
        # done, don't deliver. Checked before quiet so the signal names the truest reason. Drop-not-defer,
        # exactly like quiet — an acked nudge is never re-delivered later. Only when we'd actually fire.
        if fire and ra.entry_acked(r, acks, today_local):
            r["acked_at"] = now.isoformat().replace("+00:00", "Z")
            changed = True
            signals.append({"kind": "reminder_suppressed_ack", "id": r.get("id"),
                            "reminder_id": r.get("reminder_id"), "text": r.get("text")})
            continue
        # Quiet window (do-not-disturb): drop a due nudge that doesn't pierce (⭐ High and below), so it
        # never buzzes tonight AND never re-fires when quiet lifts — it's consumed, not deferred. Call Me
        # + Critical-and-above still come through (entry_pierces_quiet). Only when we'd actually deliver.
        if fire and quiet and not pierces:
            r["suppressed_at"] = now.isoformat().replace("+00:00", "Z")
            changed = True
            signals.append({"kind": "reminder_suppressed_quiet", "id": r.get("id"), "text": r.get("text")})
            continue
        # Presence defer: hold — never drop — a nudge until presence conditions clear (Phase 1 place-gate;
        # Phase 2 driving). Stamp NOTHING, so it stays pending and re-checks next loop (fires late, never
        # never). Fail-open: only defer when the context is FRESH; absent/stale/unavailable presence fires.
        # Piercing items (Call Me / Critical) are never held by the driving rule.
        if (fire and _presence_defer is not None
                and _presence_context_fresh(presence_ctx, now)
                and _presence_defer(r, presence_ctx, pierces)):
            signals.append({"kind": "reminder_deferred_presence", "id": r.get("id"),
                            "require_place": r.get("require_place"),
                            "activity": presence_ctx.get("activity"), "text": r.get("text")})
            continue
        # Live-session defer: HOLD a non-piercing nudge (stamp NOTHING → re-checked next loop, defer-not-
        # drop) while an interactive /assistant session is live, so it doesn't buzz into a live
        # conversation; it fires naturally once the session ages out of the TTL. Piercing items (Call Me /
        # Critical) are never held. Composes with quiet: a quiet window would already have dropped this
        # non-piercing entry above, so this only bites when NOT quiet — piercing still pierces both gates.
        if fire and session_live and not pierces:
            signals.append({"kind": "reminder_deferred_session", "id": r.get("id"), "text": r.get("text")})
            continue
        # Catch-up stagger gate: a released backlog must drip, not wall. A non-piercing nudge is HELD
        # (stamped nothing → re-checked next loop, defer-not-drop) if we already fired one this pass, or if
        # the last non-piercing fire was under CATCHUP_STAGGER_SEC ago. Piercing items skip the gate.
        if (fire and not pierces
                and (fired_nonpiercing
                     or (last_nudge_fire is not None
                         and (now - last_nudge_fire).total_seconds() < CATCHUP_STAGGER_SEC))):
            signals.append({"kind": "reminder_stagger_held", "id": r.get("id"), "text": r.get("text")})
            continue
        # Due now. Reminders are act-low (the owner's own content) — the daemon delivers directly.
        if fire:
            raw = r.get("text", "(no text)")
            channel = (r.get("channel") or "telegram").lower()
            res, used = _deliver_reminder(
                channel, raw, telegram_env, call_env, discord_env,
                escalate=bool(r.get("escalate")),
                interval_sec=r.get("interval_sec"), max_attempts=r.get("max_attempts"))
            if channel != "telegram" and used == "telegram":
                signals.append({"kind": "reminder_channel_fallback", "id": r.get("id"), "requested": channel})
            if res.get("ok"):
                r["fired_at"] = now.isoformat().replace("+00:00", "Z")
                changed = True
                # Remember which Telegram message THIS nudge was, keyed to its ⏰ row, so a 👍 on it can
                # ack the right thing. Telegram/nudges only — a call has no message to react to.
                if used == "telegram":
                    record_sent_message(state_dir, res, "nudge", f"⏰ Reminder: {raw}",
                                        reminder_id=r.get("reminder_id"), now=now)
                # A non-piercing fire opens the stagger window (and spends this pass's one slot); piercing
                # items are a separate lane — they neither consume nor reset the drip clock.
                if not pierces:
                    fired_nonpiercing = True
                    last_nudge_fire = now
                    _save_last_nudge_fire(state_dir, now)
                signals.append({"kind": "reminder_fired", "id": r.get("id"), "channel": used, "text": raw})
            else:
                signals.append({"kind": "reminder_send_failed", "id": r.get("id"), "channel": used, "error": res.get("error")})
        else:
            signals.append({"kind": "reminder_due", "id": r.get("id"), "text": r.get("text")})
    if changed:
        save_json(path, reminders)
    return signals


def main() -> int:
    p = argparse.ArgumentParser(description="Sentinel one-shot: fire due reminders + optional comms peek.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    p.add_argument("--telegram-env", default=DEFAULT_TELEGRAM_ENV)
    p.add_argument("--call-env", default=None, help="push-call.env — enables reminders with channel=call")
    p.add_argument("--discord-env", default=None, help="discord.env — enables reminders with channel=discord")
    p.add_argument("--launch-cmd", help="shell command to run the comms peek (e.g. a headless Watch run)")
    p.add_argument("--peek-interval-min", type=int, default=0,
                   help="run a comms peek (Watch) at most every N minutes (0 = never)")
    p.add_argument("--no-fire-reminders", action="store_true", help="report due reminders but don't send them")
    p.add_argument("--now", help="override current time (ISO 8601 UTC) for testing")
    p.add_argument("--branch-claimed", metavar="BRANCH",
                   help="query only: print 'claimed' if any live session is working on BRANCH, else "
                        "'free', then exit. Fail-CLOSED (prints 'claimed' when it cannot tell). Used by "
                        "seneschald-control.ps1 -Action Update before reclaiming a parked checkout.")
    args = p.parse_args()

    try:
        now = parse_iso(args.now) if args.now else datetime.now(timezone.utc)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"bad --now: {e}"}))
        return 1

    # Query-only mode: answer and exit — never fires reminders or a peek as a side effect.
    if args.branch_claimed:
        try:
            claimed = branch_is_claimed(args.state_dir, args.branch_claimed, now)
        except Exception:
            claimed = True  # fail-CLOSED: an unexpected read error must not license moving the branch
        print("claimed" if claimed else "free")
        return 0

    os.makedirs(args.state_dir, exist_ok=True)
    signals = []
    brain_work = False

    signals += check_reminders(args.state_dir, now, not args.no_fire_reminders, args.telegram_env,
                               call_env=args.call_env, discord_env=args.discord_env)

    # Comms peek cadence: run the (cheap) Watch pass at most every N minutes.
    peek_path = os.path.join(args.state_dir, "last-peek")
    peek_due = False
    if args.peek_interval_min > 0:
        last_peek = load_json(peek_path, None)
        try:
            elapsed_ok = last_peek is None or (now - parse_iso(last_peek)).total_seconds() >= args.peek_interval_min * 60
        except (ValueError, TypeError):
            elapsed_ok = True
        if elapsed_ok:
            peek_due = True
            brain_work = True
            signals.append({"kind": "comms_peek_due"})

    launched = None
    if brain_work and args.launch_cmd:
        try:
            rc = subprocess.run(args.launch_cmd, shell=True, cwd=os.path.join(SCRIPT_DIR, "..", ".."),
                                creationflags=NO_WINDOW)
            launched = {"ran": True, "returncode": rc.returncode}
        except Exception as e:  # noqa: BLE001
            launched = {"ran": False, "error": str(e)}
    # Record the peek time once it's due, so the cadence holds even if no launch-cmd is wired yet.
    if peek_due:
        save_json(peek_path, now.isoformat().replace("+00:00", "Z"))

    verdict = {
        "ok": True,
        "checked_at": now.isoformat().replace("+00:00", "Z"),
        "brain_work": brain_work,
        "signals": signals,
        "launched": launched,
    }
    save_json(os.path.join(args.state_dir, "last-signal.json"), verdict)
    print(json.dumps(verdict))
    return EXIT_WORK if brain_work else 0


if __name__ == "__main__":
    sys.exit(main())
