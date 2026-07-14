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
"2026-06-29T20:00:00Z") when it interprets "remind me at 3pm" in America/Chicago. This stays
timezone-dumb on purpose.

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
from datetime import datetime, timezone

import reminders_acks as ra  # durable ack ledger — the fire-time "already did it" gate (sibling, stdlib)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
DEFAULT_TELEGRAM_ENV = os.path.join(SCRIPT_DIR, "telegram.env")
EXIT_WORK = 10

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


def send_telegram(text: str, telegram_env: str) -> dict:
    """Push a message via the sibling telegram_send.py. Returns its parsed JSON result.
    Always returns a dict (never raises) so the daemon can act on {"ok": False} instead of crashing."""
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "telegram_send.py"),
             "--text", text, "--env-file", telegram_env],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "telegram_send.py timed out"}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": proc.stderr.strip() or "telegram_send.py produced no JSON"}


def poll_telegram(telegram_env: str, state_dir: str, commit: bool = True, timeout: int = 0) -> dict:
    """Fetch new inbound messages via telegram_poll.py. With commit=True, advance the offset
    (acknowledge them). timeout = long-poll seconds (0 = single fast call). Used by presence.py."""
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "telegram_poll.py"),
           "--env-file", telegram_env,
           "--offset-file", os.path.join(state_dir, "telegram-offset"),
           "--timeout", str(timeout)]
    if commit:
        cmd.append("--commit")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
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
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
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
    # Fire-time ack gate: load the durable ack ledger once and compute today's LOCAL (Chicago) date.
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
    args = p.parse_args()

    try:
        now = parse_iso(args.now) if args.now else datetime.now(timezone.utc)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"bad --now: {e}"}))
        return 1

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
            rc = subprocess.run(args.launch_cmd, shell=True, cwd=os.path.join(SCRIPT_DIR, "..", ".."))
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
