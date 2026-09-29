#!/usr/bin/env python3
"""seneschald auto-revive — the DECISION half of "should the watchdog restart a dead daemon right now."

Background: `seneschald-control.ps1` runs every ~10 minutes as an unprivileged scheduled task and already
knows how to detect a dead `presence.py` (see `Test-PresenceAlive`). Today it only alerts. Teaching it to
*revive* means putting process-restart logic — which must never fight a deliberate `-Action Stop`, and
which manages a spend-relevant retry budget — somewhere it can be proven correct. There is no PowerShell
test infrastructure in this repo (no Pester, CI never runs a `.ps1`), so the decision lives here, in
stdlib Python under `python -m unittest`, and `seneschald-control.ps1` becomes a thin caller: it runs this
module, reads the LAST line of stdout as JSON, and acts on the verdict — exactly the existing shape
`seneschald-control.ps1` already uses for `sentinel.py --branch-claimed`. Full background:
`seneschal/docs/seneschald-revive-spec.md`.

This module never touches `presence-health.json`. It only *reads* it (best-effort) to decide, and hands
back a `next_state` for the caller to persist — see `Save-PresenceRevivalState` in `seneschald-control.ps1`,
which writes back exactly the three `next_state` fields and leaves the rest of the record alone.

CLI:
    python seneschald_revive.py --state-dir <dir> --decide           # the revive/don't-revive verdict
    python seneschald_revive.py --state-dir <dir> --record-healthy   # the reset after an observed-healthy check

Both modes print exactly one line of JSON — the last line of stdout — and exit 0. A non-zero exit is
reserved for the TOOL failing (a bad --now, an argument error), never for "the answer is don't revive";
"don't revive" is itself a normal, successful verdict.

Stdlib only. Python 3.11+.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# Budget: at most this many revival attempts inside a rolling window before giving up and staying given
# up (spec §3.5 / §4). Named constants so the numbers live in exactly one place and tests can reference
# them instead of hardcoding copies that could silently drift out of sync.
MAX_REVIVALS = 3
REVIVAL_WINDOW_MIN = 60

STOPPED_SENTINEL_NAME = "seneschald-stopped"
# The daemon's OWN self crash-loop guard (presence.py) drops this when it detects it has booted too
# many times too fast and gives up relaunching itself. The watchdog must treat it exactly like
# seneschald-stopped — do NOT auto-revive while it's present — so the two mechanisms don't fight: the daemon
# deliberately stopped burning into the same crash, and a revive here would immediately undo that. A
# human clears it (seneschald-control.ps1 Start-Seneschald) or the daemon self-heals it after a sustained-
# healthy run. See presence.py's "self crash-loop guard" section + seneschal/docs/seneschald-revive-spec.md.
CRASHLOOP_SENTINEL_NAME = "seneschald-crashloop"
HEALTH_FILE_NAME = "presence-health.json"
LOCK_FILE_NAME = "presence.lock"

# The revive-confirmation settle window: a daemon we just restarted must stay up (heartbeat advancing)
# at least this long before the watchdog counts the revive as a real recovery. seneschald-control.ps1 passes
# its live value in via --min-uptime-sec ($ReviveSettleSec); this is the fallback default. See
# seneschald-revive-spec.md §"Confirming a revive actually took".
DEFAULT_MIN_UPTIME_SEC = 90
# Heartbeat-staleness threshold, mirroring seneschald-control.ps1 $PresenceStaleSec (heartbeat ticks ~5s).
# The .ps1 passes its live value in via --stale-sec; this is only the fallback.
DEFAULT_STALE_SEC = 180


# --------------------------------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------------------------------

def _parse_iso(value) -> datetime:
    """Parse an ISO-8601 instant defensively: accept a trailing 'Z', accept an explicit '+00:00'
    offset, and treat a naive (offset-less) stamp as UTC rather than raising or letting it slip through
    as naive.

    This mirrors ``sentinel.py``'s ``parse_iso`` deliberately (same repo convention, same shape) rather
    than importing it: this module has to stay stdlib-only and self-contained so it keeps working
    regardless of unrelated changes elsewhere in ``seneschal/scripts``.

    Comparing a naive datetime against an aware one raises ``TypeError`` in Python — and a UTC/local
    mixup is exactly the bug that once produced a real misreport (a presence alert that read "down
    ~-290 min", see ``seneschald-control.ps1``'s ``ConvertFrom-IsoUtc`` comment). Every timestamp that reaches a comparison in this module goes through here first, and the
    result is always aware UTC, so that class of bug cannot recur on this path.

    Raises ValueError/TypeError on genuine garbage — every caller here treats that as fail-closed
    'undetermined', never lets it propagate.
    """
    if not isinstance(value, str):
        raise TypeError(f"timestamp must be a string, got {type(value).__name__}")
    s = value.strip()
    if not s:
        raise ValueError("empty timestamp")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    """Serialize an aware UTC datetime to the ONE canonical on-disk form: second precision, explicit 'Z'.

    Truncated to seconds deliberately. ``datetime.isoformat()`` emits microseconds, but the PowerShell
    side writes (and normalizes back to) ``yyyy-MM-ddTHH:mm:ssZ`` — see ``ConvertTo-IsoUtcString`` in
    ``seneschald-control.ps1``. Both sides parse either shape, so a mismatch here is harmless *today*; it is
    normalized anyway because a field that round-trips between two processes in two different formats is
    exactly the kind of near-miss that makes a future reader distrust the data — or write a comparison
    against a literal string that works on one path and not the other.
    """
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------------------------------
# Reading presence-health.json
# --------------------------------------------------------------------------------------------------

def _fresh_record() -> dict:
    """The record a never-revived (or unrecoverably confused) daemon starts from."""
    return {"revivals": 0, "revival_window_start": None, "revival_gave_up": False}


def _load_health(path: str) -> tuple[dict, bool]:
    """Read + normalize the revival bookkeeping out of presence-health.json.

    Returns ``(record, ok)`` where ``record`` always has python-native types (``revival_window_start``
    is a ``datetime`` or ``None``, never a raw string) and ``ok`` is False for anything other than a
    clean read or a genuinely missing file.

    A MISSING file is not an error (spec §3.5/rule 2) — a watchdog that has never revived this daemon
    before has nothing to read yet, and that must not be indistinguishable from a broken read. Every
    OTHER failure mode — unparseable JSON, a non-dict top level, a field of the wrong type, a timestamp
    that won't parse, a permissions error mid-read — comes back as ``ok=False`` so the caller can fail
    closed (rule 5). We deliberately do NOT try to salvage individual fields out of a partially-bad
    record: a health file that lied about one field is not a record we can trust for the others either,
    and "reset to fresh, don't revive this cycle" is a safe combination (a corrupt record already means
    we have no evidence of an active crash loop worth protecting).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return _fresh_record(), True
    except OSError:
        return _fresh_record(), False  # unreadable for any other reason (permissions, mid-write lock, …)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _fresh_record(), False

    if not isinstance(data, dict):
        return _fresh_record(), False

    revivals = data.get("revivals", 0)
    if not isinstance(revivals, int) or isinstance(revivals, bool) or revivals < 0:
        return _fresh_record(), False

    gave_up = data.get("revival_gave_up", False)
    if not isinstance(gave_up, bool):
        return _fresh_record(), False

    window_raw = data.get("revival_window_start")
    window_start = None
    if window_raw is not None:
        try:
            window_start = _parse_iso(window_raw)
        except (ValueError, TypeError):
            return _fresh_record(), False

    return {"revivals": revivals, "revival_window_start": window_start, "revival_gave_up": gave_up}, True


# --------------------------------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------------------------------

def _result(revive: bool, reason: str, record: dict | None) -> dict:
    """Build the verdict. ``record=None`` emits ``next_state: null``, which tells the caller to persist
    NOTHING — see ``_undetermined`` for why that distinction matters."""
    if record is None:
        return {"revive": revive, "reason": reason, "next_state": None}
    window_start = record["revival_window_start"]
    return {
        "revive": revive,
        "reason": reason,
        "next_state": {
            "revivals": record["revivals"],
            "revival_window_start": _iso(window_start) if window_start is not None else None,
            "revival_gave_up": record["revival_gave_up"],
        },
    }


def _undetermined() -> dict:
    """Fail-closed verdict that also declines to WRITE anything.

    Emitting a zeroed next_state here would be actively harmful, not merely useless: the health file is
    rewritten by ``Set-PresenceHealth`` every cycle, so a read can lose a race and come back unreadable
    for reasons that have nothing to do with the daemon's actual state. If that transient made us hand
    back fresh zeros, the caller would persist them and silently reset the crash-loop counter — handing a
    genuinely crash-looping daemon a brand-new budget every time a read happened to race, so the give-up
    gate might never fire. "I don't know" must mean "change nothing", never "assume the best".
    (``Save-PresenceRevivalState`` no-ops on a null next_state, which is the other half of this contract.)
    """
    return _result(False, "undetermined", None)


def decide(state_dir: str, now: datetime) -> dict:
    """Should the caller revive the daemon right now, and what should it persist afterward?

    Every branch below is one of the five numbered rules in seneschald-revive-spec.md §4/§3.4/§3.5, in the
    same order. The whole body runs inside one try/except: this predicate is consulted by an
    unprivileged scheduled task running under PowerShell's ``$ErrorActionPreference = 'Stop'``, and the
    ``.ps1`` side deliberately keeps the revive call wrapped in its own try/catch too (spec §3.3) — but
    belt AND braces, this side must never be the thing that raises. "Unclear" always resolves to "do not
    revive": a watchdog that guesses wrong by reviving is worse than one that guesses wrong by staying
    quiet, because the former can mask a real, unrecoverable failure behind a false "it's handled".
    """
    try:
        sentinel_path = os.path.join(state_dir, STOPPED_SENTINEL_NAME)
        crashloop_path = os.path.join(state_dir, CRASHLOOP_SENTINEL_NAME)
        health_path = os.path.join(state_dir, HEALTH_FILE_NAME)
        record, ok = _load_health(health_path)

        # Rule 1 — the deliberate-stop sentinel wins over EVERYTHING, including an available budget.
        # `Stop-Seneschald` writes this file precisely so that intent (a human said stop) is never
        # reconstructed by inference from the daemon's absence — inference cannot work here because a
        # deliberate stop and a crash converge on identical observable state (spec §3.4). Without this
        # check, a revive would fight `-Action Stop` on the very next 10-minute cycle. We still echo
        # whatever revival bookkeeping we could read (or the fresh default, if we couldn't) rather than
        # touching it — a deliberate stop says nothing about whether the crash-loop counter should move.
        if os.path.exists(sentinel_path):
            return _result(False, "deliberately-stopped", record)

        # Rule 1b — the daemon's OWN crash-loop guard gave up. Same posture as the deliberate-stop
        # sentinel: it wins over the budget and we don't touch the revival bookkeeping. presence.py's
        # self crash-loop guard already decided that relaunching is pointless right now (it kept booting
        # into the same crash), so reviving would just re-arm the loop it deliberately broke. Stand down;
        # a human (Start-Seneschald) or a sustained-healthy run clears the sentinel. Checked before the
        # give-up latch / budget so the watchdog never spends a revival attempt against a daemon that has
        # already told it to stop.
        if os.path.exists(crashloop_path):
            return _result(False, "crash-looping", record)

        # Rule 5 (checked here because it covers the read above) — an unreadable/corrupt health file
        # must never be treated as "fresh". Treating it as fresh would silently forgive an active crash
        # loop (reset the counter an attacker — or just bad luck — could keep tripping forever); refusing
        # to revive is the only safe default when we can't tell what state we're actually in.
        if not ok:
            return _undetermined()

        # Rule 3 — the give-up latch. Checked BEFORE any window-roll logic below, deliberately: a
        # give-up must survive an arbitrarily stale window, or the window simply rolling over on the
        # next cycle would silently un-stick it and the "stay given-up until observed healthy or a human
        # intervenes" contract (spec §4) would be a lie. Only --record-healthy or manual intervention
        # clears this field.
        if record["revival_gave_up"]:
            return _result(False, "already-gave-up", record)

        # Rule 4 — the rolling budget.
        window_start = record["revival_window_start"]
        revivals = record["revivals"]
        if window_start is None or (now - window_start) >= timedelta(minutes=REVIVAL_WINDOW_MIN):
            # No window yet, or it's expired: start a fresh one before counting. ">=" so a check landing
            # exactly on the boundary counts as rolled (spec §4's own inequality) rather than needing one
            # more cycle to clear — an off-by-one here would silently double the effective window.
            window_start = now
            revivals = 0

        if revivals >= MAX_REVIVALS:
            # Budget spent inside a still-live window: give up now and LATCH it (revival_gave_up=True)
            # so next cycle hits rule 3 immediately instead of re-deriving the same conclusion — and,
            # more importantly, so the window simply rolling over an hour from now does NOT quietly grant
            # a fresh budget to a daemon that is still crash-looping. Only an observed-healthy check
            # (--record-healthy) or a human clears this.
            exhausted = {"revivals": revivals, "revival_window_start": window_start, "revival_gave_up": True}
            return _result(False, "budget-exhausted", exhausted)

        granted = {"revivals": revivals + 1, "revival_window_start": window_start, "revival_gave_up": False}
        return _result(True, "ok", granted)
    except Exception:
        # Fail CLOSED, unconditionally. Whatever the failure mode — a permissions error mid-read, a
        # field of a type nothing above anticipated, disk trouble — the answer is the same as a garbage
        # file: don't revive, and don't write anything either (see _undetermined).
        return _undetermined()


def record_healthy() -> dict:
    """The reset the caller persists after observing the daemon alive post-revive (or any other
    "we just confirmed it's healthy" moment). Mirrors ``presence.py``'s ``archon_sites_task``, which
    clears its own in-memory respawn counter the same way on an observed-healthy check — the watchdog
    and the daemon are separate processes with no shared memory, so ``presence-health.json`` (via this
    module's output) is the closest thing to that shared state this side has.

    Printed BARE — not wrapped in the ``{"revive": ..., "next_state": ...}`` envelope ``--decide`` uses —
    because the caller (``Save-PresenceRevivalState`` in ``seneschald-control.ps1``) feeds this straight in
    as the next_state payload with no unwrapping step.
    """
    return _fresh_record()


# --------------------------------------------------------------------------------------------------
# Confirming a revive actually took (the settle-window check)
# --------------------------------------------------------------------------------------------------

def _load_lock(state_dir: str) -> dict | None:
    """Best-effort read of presence.lock → dict, else None (absent / unreadable / garbled / non-dict).
    Deliberately never raises: a watchdog must not crash on a half-written lock caught mid-write."""
    try:
        with open(os.path.join(state_dir, LOCK_FILE_NAME), "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def confirm_sustained(lock: dict | None, now: datetime, stale_sec: float,
                      min_uptime_sec: float) -> dict:
    """Has the daemon described by ``lock`` been alive *continuously* for at least ``min_uptime_sec``?

    This is the honest "the revive took" signal, replacing the old "saw one heartbeat within ~24s" check
    that a fast crash-loop passed every cycle: a daemon that boots, writes one heartbeat, and dies
    within a second was scored a successful revive — which reset the revival budget so the give-up latch
    never fired, and it could stay down for many hours, "revived" every 10 minutes.

    BOTH conditions are required:
      * **ticking now** — the heartbeat is fresh (age < ``stale_sec``). A dead process stops touching the
        lock, so its heartbeat eventually goes stale.
      * **sustained** — the *observed uptime* (``heartbeat - started_at``, i.e. how long it actually kept
        ticking) is >= ``min_uptime_sec``.

    Why observed-uptime and NOT ``now - started_at``: the stale window (180s) is longer than the settle
    window (90s), so a heartbeat frozen at boot+1s still reads "fresh" 90s later — ``now - started_at``
    would wrongly pass. ``heartbeat - started_at`` is immune: a daemon that died a second after booting
    reports ~1s of uptime no matter how long we then wait. That is exactly the crash-loop shape.

    Fail-closed: any unreadable/garbled/absent lock returns not-sustained, never raises.
    Returns ``{"sustained": bool, "reason": str, "uptime_sec": float|None, "hb_age_sec": float|None}``."""
    try:
        if not isinstance(lock, dict):
            return {"sustained": False, "reason": "no-lock", "uptime_sec": None, "hb_age_sec": None}
        hb_raw = lock.get("heartbeat")
        start_raw = lock.get("started_at")
        if not hb_raw or not start_raw:
            return {"sustained": False, "reason": "lock-missing-fields", "uptime_sec": None, "hb_age_sec": None}
        hb = _parse_iso(hb_raw)
        started = _parse_iso(start_raw)
        hb_age = (now - hb).total_seconds()
        uptime = (hb - started).total_seconds()
        if hb_age >= stale_sec:
            return {"sustained": False, "reason": "heartbeat-stale", "uptime_sec": uptime, "hb_age_sec": hb_age}
        if uptime < min_uptime_sec:
            return {"sustained": False, "reason": "uptime-too-short", "uptime_sec": uptime, "hb_age_sec": hb_age}
        return {"sustained": True, "reason": "ok", "uptime_sec": uptime, "hb_age_sec": hb_age}
    except (ValueError, TypeError):
        return {"sustained": False, "reason": "lock-unreadable", "uptime_sec": None, "hb_age_sec": None}


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="seneschald auto-revive: should seneschald-control.ps1 restart a dead presence.py right now?")
    p.add_argument("--state-dir", required=True, help="seneschal/state — where seneschald-stopped and "
                   "presence-health.json live")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--decide", action="store_true",
                       help="print the revive/don't-revive verdict + next_state as one line of JSON")
    mode.add_argument("--record-healthy", action="store_true",
                       help="print the revival-counter reset for an observed-healthy daemon")
    mode.add_argument("--confirm-sustained", action="store_true",
                       help="print whether presence.lock shows a daemon that has stayed up >= "
                            "--min-uptime-sec (the settle-window check the .ps1 gates revive-success on)")
    p.add_argument("--min-uptime-sec", type=float, default=DEFAULT_MIN_UPTIME_SEC,
                    help="settle window for --confirm-sustained, in seconds (default %(default)s)")
    p.add_argument("--stale-sec", type=float, default=DEFAULT_STALE_SEC,
                    help="heartbeat-staleness threshold for --confirm-sustained, in seconds "
                         "(default %(default)s; mirrors seneschald-control.ps1 $PresenceStaleSec)")
    p.add_argument("--now", default=None,
                    help="override current time (ISO-8601 UTC) — tests only; defaults to real UTC now")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    if args.now:
        try:
            now = _parse_iso(args.now)
        except (ValueError, TypeError) as e:
            # A bad --now is the TOOL failing, not a decision — non-zero, distinct shape, so the caller
            # (which only ever tries to ConvertFrom-Json the last line on exit 0) doesn't mistake this
            # for a real verdict.
            print(json.dumps({"ok": False, "error": f"bad --now: {e}"}))
            return 1
    else:
        now = datetime.now(timezone.utc)

    if args.record_healthy:
        result = record_healthy()
    elif args.confirm_sustained:
        result = confirm_sustained(_load_lock(args.state_dir), now, args.stale_sec, args.min_uptime_sec)
    else:
        result = decide(args.state_dir, now)

    # Exactly one line, and nothing else on stdout in this process — the caller reads the LAST line of
    # stdout as the verdict, matching the existing sentinel.py --branch-claimed convention.
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
