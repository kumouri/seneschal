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
"2026-06-29T20:00:00Z") when it interprets "remind me at 3pm" in the owner's timezone. Scheduling stays
timezone-dumb on purpose — `due_at` comparison is pure UTC arithmetic — with exactly two owner-zone
readings: the fire-time ack date (reminders_acks.local_today → tz_common) and the **night curfew**
(`in_night_curfew`), which asks what the CLOCK ON THE OWNER'S WALL reads, a question UTC cannot answer.
It converts through `clock.to_local` (tz_common: the configured owner zone, DST-correct; machine-local
when unconfigured) and reads its window from `owner.nightCurfew` in persona/identity.json.

USAGE:
  python sentinel.py                                   # fire due reminders + report
  python sentinel.py --peek-interval-min 5 --launch-cmd "claude -p 'Run Watch mode'"  # + a comms peek
  python sentinel.py --no-fire-reminders               # report due reminders without sending
  python sentinel.py --now 2026-06-29T20:01:00Z        # override clock (testing)

Prints a one-line JSON verdict; also writes it to state/last-signal.json. Exit 0 on success (with or
without a peek due), 10 if a comms peek fired, 1 on error.

This module is a helper library and one-shot, and it is also THE SINGLE DELIVERY CHOKEPOINT via
`check_reminders`. Invariants worth knowing before an edit:

THE GATE ORDER IS THE INVARIANT, AND IT IS THE PART THAT IS EASY TO BREAK LATER:

    acked -> quiet -> CURFEW -> presence -> live-session -> STALENESS -> stagger

Four DROP (acked/quiet/curfew/staleness — stamp the entry, consumed, never re-delivered), three DEFER
(presence/live-session/stagger — the entry stays pending and re-checks next tick). The POSITIONS are
load-bearing: curfew sits under quiet so an EXPLICIT request keeps naming itself when both apply, and
staleness sits AFTER the live-session defer — a nudge held three hours by a live session SHOULD die
there — while being UNREACHABLE from a presence hold, which `continue`s above it. Moving either is a
behaviour change, not a refactor.

WHY CURFEW AND STALENESS EXIST: THE STAGGER IS A RATE LIMIT, NOT A LATENESS BOUND. A live session can
hold everything for hours (defer, never drop); an unacked Reminders row re-enqueues at every later slot;
and 26 waiting nudges at 4/hour is 6 h 30 m of drain — so the tail of a released backlog lands at 04:00
by arithmetic, with every gate behaving correctly. Nothing in the path asked whether a nudge was still
worth sending; the curfew ("is NOW a reasonable hour?") and the staleness cutoff ("is this still about
NOW?") are that question.

AN ACK ADVANCES THE STAGGER, AFTER A 2-MIN DEBOUNCE. `reminders_dequeue` stamps `last_ack_at` into
`nudge-stagger.json` on every ack; the stagger gate releases its 15-min hold once that ack is NEWER than
the last non-piercing fire and `ACK_ADVANCE_DEBOUNCE_SEC` old. It releases the STAGGER ONLY — it is the
last gate, `acked` is the first, and nothing between them reads the stamp. A missing or unparseable
`last_ack_at` means NO advance — the one value in that file that fails SAFE rather than open.

THE CURFEW PREDICATE IS A CONJUNCTION — `now` inside the owner's night window AND `due_at` before that
window occurrence started. Gating on `due_at` alone misses the actual case (due 22:50, released 01:20);
dropping the second half would retire late slots the owner seeds ON PURPOSE. The window is owner config
(`owner.nightCurfew`, default 01:00-07:00 owner-local); the default starts at 01:00 rather than 23:00
because plenty of owners are up past 23:00 and still want those nudges — that stretch is covered by the
staleness cutoff, not by nothing. `from datetime import time` WOULD SHADOW THE `time` MODULE this file
sleeps on — hence `clock_time`.

PRESENCE-HELD AND STAGGER-HELD TIME ARE SUBTRACTED FROM LATENESS. `presence_rules.should_defer` has TWO
rules and only the place one carries a per-entry marker, so a nudge held through a >2 h DRIVE would
otherwise arrive at the cutoff with nothing on it to exempt it and be consumed, straight through the
presence gate's NO DROPS contract. So the defer branch accumulates (`presence_deferred_since` ->
`presence_held_sec`) and the stagger stamps `stagger_deferred_since` the first tick it holds a row;
lateness measures TIME THE OWNER COULD HAVE ACTED ON IT. The curfew is deliberately NOT exempted the
same way: whether 2 AM is a reasonable hour does not depend on why we are late.

THE DRAIN ORDER IS IMPORTANCE FIRST on a `due_at` tie (`_due_sort_key`, `reminder_importance`), so a
High row can't queue behind a Low one seeded earlier into the same slot; and every staleness
suppression is also a `reminder-suppressions.jsonl` row (`reminder_suppressions.py`) the EOD Wrap
reads — a silent kill with no durable trace is the failure, not a side effect of one.

`send_telegram`'s 60 s SUBPROCESS TIMEOUT IS `ambiguous: true` — the child was demonstrably still
running with the request very probably on the wire. A delivery attempt that comes back `ambiguous` is
HELD (`ambiguous_send_at`), never re-fired blind: under-sending is recoverable, double-sending is not.
An UNPARSEABLE stdout is deliberately NOT flagged — that is a child that died abnormally, usually before
sending. Real send failures (and ambiguous ones) are also `failures.jsonl` rows (`failures.py`).

A TELEGRAM NUDGE CAN LAND IN ITS OWN TOPIC (the optional `telegram_topics` module). `_deliver_reminder`
takes `pierces` — `entry_pierces_quiet`'s ANSWER, PASSED DOWN, NEVER RECOMPUTED — and
`reminder_topic_purpose` collapses the main chat to None, i.e. NO FLAG AT ALL, so a main-chat nudge's
argv is byte-identical to the pre-topics one. The import is lazy and total: a `telegram_topics` that
cannot be reached (or is not installed) costs the topic, never the nudge. The `reminder_fired` signal
carries `message_thread_id` read off the SEND RESULT, not off the purpose asked for. Discord and `call`
are untouched.

See also: ../references/reminders-policy.md -> "Night curfew + staleness cutoff", "Catch-up stagger"
and "Delivery".
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
# `time` (the module) is imported above and used for sleeps; the datetime class of the same name is
# aliased rather than imported bare, because shadowing it silently breaks load_json's retry backoff.
from datetime import datetime, timedelta, timezone
from datetime import time as clock_time

import clock  # the owner's wall clock (a facade over tz_common) — the curfew's one zone conversion
import failures  # a durable row for a send failure that used to vanish silently
import identity_common  # owner.nightCurfew — the curfew window is owner config, never a constant
import reminder_importance  # canonical importance levels — the drain order's tie-break
import reminder_suppressions  # the staleness-suppression ledger the EOD Wrap reads (sibling, stdlib)
import reminders_acks as ra  # durable ack ledger — the fire-time "already did it" gate (sibling, stdlib)

try:  # the assertions log (what the assistant has actually said) — lands in a later port; optional
    import mouth as _mouth
except ImportError:  # pragma: no cover — absent until then; a fire simply isn't recorded there
    _mouth = None

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
# Critical `pierce_quiet`) bypass the gate — they can't slide. Fresh nudges are already spaced
# 15-30 min apart at creation (the slots stagger due_at), so normal-day timing is untouched; this only
# reshapes a bunched-up *release*. Held = re-checked next loop (defer, never drop).
#
# **THE STAGGER IS A RATE LIMIT, NOT A LATENESS BOUND.** 15 min/nudge is 4/hour, so a long hold (a live
# session, a presence defer) followed by a release can take hours to drain — the tail of that queue lands
# in the small hours by arithmetic. The night curfew and the staleness cutoff below are the missing
# question in its two forms: "is NOW a reasonable hour?" and "is this still about NOW?".
STAGGER_STATE_FILE = ra.STAGGER_STATE_FILE  # "nudge-stagger.json" — shared with the ack path
CATCHUP_STAGGER_SEC = 15 * 60

# **An ack ADVANCES the stagger, after a debounce.** The 15-min hold exists to space a *release* the
# owner has not caught up on; an ack IS the owner catching up, so making the next pending nudge wait out
# the rest of the window is the rate limit punishing the behaviour it wants. `reminders_dequeue` (every
# ack route ends there) stamps `last_ack_at` into the same file as the drip clock, and the gate below also
# releases when that ack is NEWER than the last non-piercing fire AND at least this many seconds old. The
# debounce is for a burst: three delivered nudges 👍'd within seconds — WRONG is firing the second one
# (already acked) next; RIGHT is realising all three are acked and sending only the next still-pending
# one. The `acked` gate (first in the order) makes the acked rows drop; the debounce makes sure the
# dequeue for the burst's last 👍 has landed before the advance fires anything. Every ack overwrites the
# stamp, so a burst measures from its LAST ack; a fire re-stamps `last_nonpiercing_fire` past the ack, so
# one burst opens the window ONCE. It releases the STAGGER only: quiet / curfew / presence / live-session /
# staleness all sit ahead of it and are not consulted about acks at all.
ACK_ADVANCE_DEBOUNCE_SEC = 2 * 60

# ------------------------------------------------------------------ Night curfew + staleness cutoff
# **Night curfew (owner config `owner.nightCurfew`, default 01:00-07:00 owner-local).** A NON-piercing
# nudge that LEAKED into the night is CONSUMED, not delivered — drop-not-defer, stamped `suppressed_at`
# exactly like the quiet gate, so the EOD wrap and Consecutive Misses count it identically and it can
# never re-fire at breakfast. The window is read through `identity_common.night_curfew_window` (a start
# after the end wraps midnight; start == end disables the curfew). These are only the defaults, kept as
# names for readers and tests.
CURFEW_START_LOCAL = clock_time(*identity_common.DEFAULT_NIGHT_CURFEW[0])
CURFEW_END_LOCAL = clock_time(*identity_common.DEFAULT_NIGHT_CURFEW[1])

# **Staleness cutoff.** A NON-piercing nudge more than this far past due has stopped being a reminder and
# become an interruption, whatever hour it is. Same drop-not-defer stamping. NO grace period, and no grace
# on the curfew either: due-before-curfew + firing-inside-curfew = consumed even 5 minutes late. That
# aggressiveness is deliberate and is the knob to soften first.
MAX_LATENESS_SEC = 2 * 3600


def parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 instant, accepting a trailing 'Z'. Returns an aware UTC datetime.

    Raises ``TypeError`` on a non-string argument (e.g. a JSON object/array read back from a cache
    file the warm session clobbered) instead of the raw ``AttributeError`` a ``str`` method would
    throw. Every caller that parses a cache value already guards ``(ValueError, TypeError)`` and fails
    open, so a garbled ``last-peek`` / ``last_nonpiercing_fire`` degrades to "treat as due" rather than
    crashing the scheduler task and taking the whole daemon down."""
    if not isinstance(s, str):
        raise TypeError(f"parse_iso expected an ISO 8601 string, got {type(s).__name__}")
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_stagger_instant(state_dir: str, field: str) -> datetime | None:
    """One ISO instant out of `nudge-stagger.json`, or None when absent/unparseable. Both readers below
    want None on a bad value; what None MEANS differs per field and is documented on each."""
    data = ra.load_stagger_state(state_dir)
    if data.get(field):
        try:
            return parse_iso(data[field])
        except (ValueError, TypeError):
            return None
    return None


def _load_last_nudge_fire(state_dir: str) -> datetime | None:
    """Instant we last delivered a NON-piercing nudge (the catch-up stagger clock). Absent/unparseable →
    None: fail-open, so the next non-piercing nudge fires immediately rather than hanging on a bad file."""
    return _load_stagger_instant(state_dir, "last_nonpiercing_fire")


def _load_last_ack_at(state_dir: str) -> datetime | None:
    """Instant of the owner's most recent ack (`reminders_dequeue` stamps it). Absent/unparseable →
    None, which the gate reads as NO ADVANCE — fail-safe in the opposite direction from the fire clock,
    because the advance is a privilege the ack earns and a garbled stamp has earned nothing; the ordinary
    15-min drip still applies, so the cost of a bad value is the old behaviour, never a wall."""
    return _load_stagger_instant(state_dir, "last_ack_at")


def _ack_advances_stagger(now: datetime, last_nudge_fire: datetime, last_ack_at: datetime | None) -> bool:
    """The ack-advance predicate: a hold inside `CATCHUP_STAGGER_SEC` is released iff an ack landed
    AFTER the last non-piercing fire and is at least `ACK_ADVANCE_DEBOUNCE_SEC` old (so a batch of 👍s
    has finished landing and the `acked` gate has already consumed everything acked). An ack older than
    the last fire is one this window has already spent — it advances nothing."""
    if last_ack_at is None or last_ack_at <= last_nudge_fire:
        return False
    return (now - last_ack_at).total_seconds() >= ACK_ADVANCE_DEBOUNCE_SEC


def _save_last_nudge_fire(state_dir: str, now: datetime) -> None:
    # MERGE (keeps `last_ack_at` standing) — `reminders_acks.save_stagger_state` is tmp + os.replace.
    ra.save_stagger_state(state_dir, last_nonpiercing_fire=now.isoformat().replace("+00:00", "Z"))


# Catch-up-drain order, when several rows share a `due_at` (a morning batch, a released backlog):
# importance decides who goes first, never raw seed/insertion order alone — a High Today Todo seeded
# deep into a large batch otherwise drains one row per CATCHUP_STAGGER_SEC and can go stale before its
# turn ever comes. `{canonical key: rank}` (0 = most important), the order
# `references/reminders-policy.md` defines; an entry's `importance` may be ANY backend's spelling (the
# Notion emoji label or the canonical key) and is folded through `reminder_importance.normalize`.
IMPORTANCE_RANK = dict(reminder_importance.RANK)
# Missing/unrecognized importance — an ad-hoc `reminders_enqueue.py` nudge, a standing roll — is
# NEUTRAL: the same rank as `notable`, so it can neither jump a High row nor get starved behind an
# explicit Low one just for lacking the field.
DEFAULT_IMPORTANCE_RANK = reminder_importance.DEFAULT_RANK


def _importance_rank(r: dict) -> int:
    return reminder_importance.rank((r or {}).get("importance"))


def _due_sort_key(r: dict) -> tuple[float, int]:
    """Fire order = oldest-due first, ties broken by importance (`IMPORTANCE_RANK`) so a High row
    never queues behind a Low one just because it was seeded later in the same slot. Missing/bad
    due_at sorts last. `sorted()` is stable, so two entries tied on BOTH keys keep their original —
    i.e. seed — order, which is the fallback `references/reminders-policy.md` asks for."""
    if not isinstance(r, dict) or not r.get("due_at"):
        return (float("inf"), DEFAULT_IMPORTANCE_RANK)
    try:
        return (parse_iso(r["due_at"]).timestamp(), _importance_rank(r))
    except (ValueError, TypeError):
        return (float("inf"), DEFAULT_IMPORTANCE_RANK)


# save_json's atomic-replace retry budget: ~0.6s total (0.1 + 0.2 + 0.3) across 4 attempts. Sized to
# outlast a reader that just opens, reads and closes a small JSON file, without stalling the ~5s
# scheduler beat that is save_json's most frequent caller.
_REPLACE_ATTEMPTS = 4
_REPLACE_BACKOFF_SEC = 0.1


def load_json(path: str, default):
    """A concurrent writer (e.g. the machine-wide session_stamp.py hook) can hold this exact path open
    for write at the instant we open it for read; on Windows that raises PermissionError where POSIX
    would just allow it, and an unhandled one kills the daemon's scheduler task. One short retry clears
    the transient collision; if it's still locked after that, treat it the same as absent/corrupt rather
    than propagate and crash the caller."""
    for attempt in range(2):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return default
        except PermissionError:
            if attempt == 0:
                time.sleep(0.05)
                continue
            return default
    return default


def save_json(path: str, data) -> None:
    """The write-side twin of load_json's retry, and for the same Windows reason. `os.replace` onto a
    destination another process currently holds OPEN — seneschald-control.ps1's liveness check reading
    `presence.lock` on its cycle, the cockpit's readers, a sibling script — fails with
    PermissionError/WinError 5 where POSIX would rename straight through, and an unhandled one takes the
    daemon down until it is restarted by hand.

    Retry with a short backoff, then let it raise — a persistent failure is a real one (disk full, a
    genuinely stuck handle) and must not be swallowed into a silently-not-saved file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SEC * (attempt + 1))


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


def curfew_window() -> tuple:
    """The owner's night-curfew window as ``(start, end)`` ``datetime.time`` values, read fresh from
    ``owner.nightCurfew`` (``identity_common.night_curfew_window``; default 01:00-07:00). Never raises —
    ``load_identity`` falls back to defaults on any problem. ``check_reminders`` reads it once per pass."""
    (sh, sm), (eh, em) = identity_common.night_curfew_window(identity_common.load_identity())
    return clock_time(sh, sm), clock_time(eh, em)


def in_night_curfew(now: datetime, due: datetime, window: tuple | None = None) -> bool:
    """True if a nudge due at ``due`` would be firing at ``now`` **inside the night curfew, having
    leaked in from earlier** — i.e. it must be consumed rather than delivered. Pure predicate given
    ``window``; the caller owns the piercing exemption and the stamping.

    Two conditions, and BOTH are load-bearing:

    1. **``now`` is inside the window** (``start <= local < end``). Gating on ``due`` alone would miss
       the actual failure mode: an item due 22:50 that the stagger only releases at 01:20. What
       matters is when the buzz *lands*, not when it was scheduled.
    2. **``due`` is BEFORE that window occurrence started.** This gate consumes what leaked in from
       the evening; it must NOT touch a nudge genuinely scheduled *inside* the small hours.

    Both readings are the owner's wall clock (``clock.to_local`` → ``tz_common``: the configured owner
    zone, DST-correct; machine-local when unconfigured), never a frozen offset. ``window`` is
    ``(start, end)`` as ``datetime.time`` (default: :func:`curfew_window`, the owner's config). A window
    whose start is after its end wraps midnight (e.g. 23:00-07:00): the occurrence containing ``now``
    then began at ``start`` on the previous local date when ``now`` is past midnight. ``start == end``
    disables the curfew."""
    start, end = window if window is not None else curfew_window()
    if start == end:
        return False
    local_now = clock.to_local(now)
    t = local_now.time()
    if start < end:
        if not (start <= t < end):
            return False
        window_start = datetime.combine(local_now.date(), start)
    else:  # wraps midnight
        if t >= start:
            window_start = datetime.combine(local_now.date(), start)
        elif t < end:
            window_start = datetime.combine(local_now.date() - timedelta(days=1), start)
        else:
            return False
    return clock.to_local(due) < window_start


def _stagger_held_sec(entry: dict, now: datetime) -> float:
    """How long this entry has been waiting its turn behind the catch-up stagger's one-per-
    ``CATCHUP_STAGGER_SEC`` drip, right now — or 0.0 if it isn't (this pass) and never has been.

    Unlike ``presence_held_sec`` this is never folded into a running total on release: the wait is
    continuous from the first tick the stagger gate actually holds the row (``stagger_deferred_since``,
    stamped in ``_check_reminders_locked``) until it fires, so this always reads the length of the
    CURRENT segment rather than a historical sum. That is also why it can never retroactively erase
    lateness the row already had *before* it started waiting — see ``entry_lateness_sec``."""
    since = entry.get("stagger_deferred_since")
    if not since:
        return 0.0
    try:
        return max(0.0, (now - parse_iso(since)).total_seconds())
    except (ValueError, TypeError):
        return 0.0


def entry_lateness_sec(entry: dict, due: datetime, now: datetime) -> float:
    """How late this nudge is **in time the owner could actually have acted on it** — raw lateness
    minus any accumulated presence-hold time (``presence_held_sec``; see the presence gate in
    ``_check_reminders_locked``) and minus any time it has spent waiting its turn behind the catch-up
    stagger (``_stagger_held_sec``).

    Subtracting the presence hold is what keeps the staleness cutoff off a collision course with the
    presence gate's **NO DROPS** contract. A ``require_place`` check alone would NOT have been enough:
    ``presence_rules.should_defer`` has two rules, and only the place one carries a per-entry marker.
    The **driving** rule holds any non-piercing nudge while ``activity == in_vehicle`` and leaves nothing
    on the entry, so a nudge held through a >2 h drive would arrive at the cutoff hours late with no way
    to tell it apart from one that was simply ignored. Measuring *actionable* lateness covers both rules
    uniformly, with no special case for either.

    **Subtracting the stagger wait is the same idea applied to the drip itself.** A nudge stuck behind
    several others in the same due-time slot is not being ignored — it is mechanically unable to fire
    any faster than one-per-``CATCHUP_STAGGER_SEC``. It does NOT make a row immune to staleness: the
    segment only opens the first tick the stagger gate actually holds the row, so any lateness it already
    carried at that instant is still counted in full ("stale beats stagger"). Only the *incremental*
    wait from that point on stops compounding."""
    held = entry.get("presence_held_sec")
    held = float(held) if isinstance(held, (int, float)) and held > 0 else 0.0
    held += _stagger_held_sec(entry, now)
    return (now - due).total_seconds() - held


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
# The interactive surfaces the owner is present at — never demoted to `build` by a hook stamp (see
# write_session_heartbeat). Kept in step with `jobs.ASSISTANT_SURFACES`, and deliberately duplicated
# rather than imported: `jobs` imports `sentinel` (deferred, inside a function) and an import back the
# other way at module level would be a cycle. Two names, one meaning.
ASSISTANT_SURFACES = frozenset({"daemon", "desktop"})


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
    # An assistant surface is NEVER demoted to `build` by a later hook stamp
    # (docs/cancel-attribution-spec.md). The machine-wide session_stamp.py hook fires on
    # UserPromptSubmit and Stop — twice a turn — so without this rule it would overwrite an
    # `/assistant` desktop chat's own marker within seconds of the chat setting it, every turn, and
    # `desktop` would be indistinguishable from a delegated build session. A session that ran
    # `/assistant` IS an assistant surface for as long as it lives (the entry is deleted on SessionEnd).
    # Narrow on purpose: only a `build` stamp is refused; any other source is an honest update.
    if (isinstance(prev, dict) and source == "build"
            and prev.get("source") in ASSISTANT_SURFACES):
        source = prev["source"]
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


def send_telegram(text: str, telegram_env: str, message_thread_id=None, topic=None,
                  state_dir: str | None = None) -> dict:
    """Push a message via the sibling telegram_send.py. Returns its parsed JSON result.
    Always returns a dict (never raises) so the daemon can act on {"ok": False} instead of crashing.

    **`message_thread_id` is the private-chat TOPIC to send into, and it defaults to OFF.** `None` adds
    no argument at all, so the argv this builds — and therefore the Bot API payload it produces — is
    byte-identical to the one it built before this parameter existed. A caller replying into a thread
    passes one.

    **`topic` NAMES A PURPOSE INSTEAD OF A THREAD** (how reminder nudges can get their own channel).
    This function reaches Telegram by RUNNING `telegram_send.py`, so it holds no token and no chat id and
    cannot resolve a thread id itself; `--topic` is that resolution on the far side of the subprocess,
    fail-open in every direction (a `telegram_send.py` without topic support is a later port's concern —
    `reminder_topic_purpose` only ever asks for a topic when the optional `telegram_topics` module is
    importable). `None` adds no argument.

    **`state_dir` RIDES ONLY WHEN `topic` DOES, AND THAT IS DELIBERATE.** The state dir is what makes a
    purpose resolvable (the topic map lives there), and adding it unconditionally would change the argv
    of every send in the tree — including the main-chat nudges, whose claim is that they are
    byte-identical to before this existed. In a test it names the temp dir, keeping topic state out of
    the live file.

    **A TIMEOUT HERE IS `ambiguous`.** When the 60 s ceiling fires, the child was still alive with the
    request very probably on the wire — Telegram may have accepted it, and the reason we cannot say is
    that we killed the process that knew. `telegram_send` classifies its own failures and can put
    `ambiguous: true` in its result JSON, but a timeout produces no JSON at all, so this dict must carry
    the flag itself or the failure reaches a retry loop with no phase on it and gets re-sent blind.

    **The residual, named rather than papered over:** an UNPARSEABLE stdout (below) is deliberately
    NOT flagged. That is a child that died abnormally, most often before it sent anything — a missing
    env file, an import error — and flagging it would permanently retire the retry for every ordinary
    misconfiguration. The timeout is flagged because the child was demonstrably still running."""
    argv = [sys.executable, os.path.join(SCRIPT_DIR, "telegram_send.py"),
            "--text", text, "--env-file", telegram_env]
    if message_thread_id is not None:
        # ABSENT, not an empty value: the assertion the tests make is an equality between two whole
        # argv lists, which only holds if there is no extra flag to ignore.
        argv += ["--message-thread-id", str(int(message_thread_id))]
    if topic is not None:
        argv += ["--topic", str(topic)]
        if state_dir:
            argv += ["--state-dir", state_dir]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "ambiguous": True, "error": "telegram_send.py timed out",
                "note": ("the request was still in flight when we killed it, so Telegram may "
                         "already have delivered it — do NOT automatically re-send this text")}
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


def reminder_topic_purpose(pierces: bool):
    """Which `telegram_topics` purpose a nudge belongs to, or **`None` meaning "name no topic at
    all"** — the main chat, with the argv byte-identical to the one this file built before topics
    existed.

    A thin wrapper over the OPTIONAL `telegram_topics.reminder_topic`, and the wrapper is the fail-open
    layer: that module is imported HERE, lazily, so an install without it (or an import that fails)
    costs the topic and never the nudge.

    **The main-chat purpose COLLAPSES TO `None` RATHER THAN BEING PASSED THROUGH.** It would resolve to
    the main chat anyway, but passing it would still put two extra flags on the argv — so the claim
    *"a main-chat nudge sends exactly what it sent before"* would be an argument about a downstream
    branch instead of an equality between two argv lists. Here it is the latter, and a test asserts
    it."""
    try:
        import telegram_topics as tt  # noqa: PLC0415 — lazy + optional: an import may never cost a nudge
        purpose = tt.reminder_topic(bool(pierces))
        return None if purpose == tt.TOPIC_MAIN_CHAT else purpose
    except Exception:  # noqa: BLE001 — no topic this time; the nudge goes to the main chat
        return None


def _deliver_reminder(channel: str, raw: str, telegram_env: str,
                      call_env: str | None, discord_env: str | None,
                      escalate: bool = False, interval_sec: int | None = None,
                      max_attempts: int | None = None, pierces: bool = False,
                      state_dir: str | None = None):
    """Route a due reminder to its channel. Returns (result_dict, effective_channel).

    A `call` speaks the raw line (no ⏰ prefix — TTS would read the emoji aloud); Telegram/Discord get
    the ⏰-prefixed text. An escalating `call` keeps ringing until the owner presses a digit (Worker-side).
    Any channel that isn't configured (or is unknown) falls back to Telegram so a critical nudge is
    never silently dropped.

    **`pierces` PICKS THE TELEGRAM TOPIC AND NOTHING ELSE.** It is :func:`entry_pierces_quiet`'s answer
    for this entry, passed down rather than recomputed here — the gate order already asked the question,
    and a second definition of *piercing* is a second thing that can drift from the gates it is named
    after. It changes no channel, no wording, no ordering and nothing about the call or Discord branches
    (Telegram-only by scope)."""
    if channel == "call" and call_env:
        return send_call(raw, call_env, escalate=escalate,
                         interval_sec=interval_sec, max_attempts=max_attempts), "call"
    if channel == "discord" and discord_env:
        return send_discord(f"⏰ Reminder: {raw}", discord_env), "discord"
    purpose = reminder_topic_purpose(pierces)
    if purpose is None:
        # The two-argument call, byte-for-byte what this line has always been. It is also why the
        # `send_telegram` test doubles that take exactly two arguments keep working on this path.
        return send_telegram(f"⏰ Reminder: {raw}", telegram_env), "telegram"
    return send_telegram(f"⏰ Reminder: {raw}", telegram_env, topic=purpose,
                         state_dir=state_dir), "telegram"


def check_reminders(state_dir: str, now: datetime, fire: bool, telegram_env: str,
                    call_env: str | None = None, discord_env: str | None = None) -> list:
    """Fire any due, unfired reminders (act-low). Routes each to its `channel`
    (telegram | call | discord; default telegram). Returns signal dicts describing what happened.

    **THE GATE ORDER, which is the part that is easy to break later:**

        acked -> quiet -> CURFEW -> presence -> live-session -> STALENESS -> stagger

    Four of those DROP (consume the entry, stamping it) and three DEFER (stamp nothing that changes its
    pending-ness, re-check next tick): acked/quiet/curfew/staleness drop; presence/live-session/stagger
    defer. Curfew sits under quiet so an explicit request still names itself, and staleness sits after
    the live-session defer (so a session-held nudge dies there) but is unreachable from a presence hold
    (so a presence-held one never can). Moving either one is a behaviour change, not a refactor.

    **A delivery attempt that comes back `ambiguous` is ALSO consumed, not left due.**
    `_deliver_reminder`/`send_telegram` set `res["ambiguous"] = True` precisely when the request may
    already have reached the owner — a killed-mid-flight subprocess, a rate-limit (429) that outran its
    own retry ceiling — and a loop checking only `res.get("ok")` would leave that row exactly as due as
    before and fire it again, blind, next tick. It is stamped `ambiguous_send_at` (skipped at the top of
    the loop like `fired_at`/`suppressed_at`/`acked_at`) and reported as `reminder_send_ambiguous`, never
    `reminder_fired` or `reminder_send_failed` — a human has to look, because this loop cannot tell
    "delivered" from "lost" any better than `telegram_send` could.

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
    # Ack-advance: the owner's most recent ack instant, read once per pass. Only consulted when the
    # stagger would otherwise hold, and only ever to RELEASE the stagger — never any gate above it.
    last_ack_at = _load_last_ack_at(state_dir)
    # The owner's night-curfew window (owner.nightCurfew), read once per pass.
    curfew = curfew_window()
    fired_nonpiercing = False
    signals, changed = [], False
    for r in sorted(reminders, key=_due_sort_key):
        if not isinstance(r, dict):
            continue
        if (r.get("fired_at") or r.get("suppressed_at") or r.get("acked_at")
                or r.get("ambiguous_send_at") or not r.get("due_at")):
            continue
        try:
            due = parse_iso(r["due_at"])
        except (ValueError, TypeError):
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
        # Night curfew: a non-piercing nudge that LEAKED into the owner's night window is consumed, not
        # delivered. Drop-not-defer with the same `suppressed_at` stamping as quiet, so downstream counting
        # (EOD wrap, Consecutive Misses) treats the two identically. Piercing entries are exempt —
        # Critical and Super-Critical still come through at 3 AM, which is the entire point of the pierce
        # set. Placed AFTER quiet (the explicit request keeps naming itself when both apply) and BEFORE
        # presence; NOT exempted by presence-hold time the way staleness is — whether 2 AM is a
        # reasonable moment to buzz does not depend on why we're late.
        if fire and not pierces and in_night_curfew(now, due, curfew):
            r["suppressed_at"] = now.isoformat().replace("+00:00", "Z")
            changed = True
            signals.append({"kind": "reminder_suppressed_curfew", "id": r.get("id"),
                            "reminder_id": r.get("reminder_id"), "text": r.get("text"),
                            "due_at": r.get("due_at")})
            continue
        # Presence defer: hold — never drop — a nudge until presence conditions clear (Phase 1 place-gate;
        # Phase 2 driving). Fail-open: only defer when the context is FRESH; absent/stale/unavailable
        # presence fires. Piercing items (Call Me / Critical) are never held by the driving rule.
        presence_holds = (fire and _presence_defer is not None
                          and _presence_context_fresh(presence_ctx, now)
                          and _presence_defer(r, presence_ctx, pierces))
        if presence_holds:
            # AMENDS the old "stamp NOTHING" defer contract, deliberately: that contract exists so a
            # deferred entry stays PENDING rather than being consumed, and an accumulator does not
            # consume it. `fired_at`/`suppressed_at`/`acked_at` are still untouched, so the entry
            # re-checks next loop exactly as before — the only new bytes are a bookkeeping stamp the
            # staleness cutoff below needs in order to not eat presence-held nudges.
            if not r.get("presence_deferred_since"):
                r["presence_deferred_since"] = now.isoformat().replace("+00:00", "Z")
                changed = True
            signals.append({"kind": "reminder_deferred_presence", "id": r.get("id"),
                            "require_place": r.get("require_place"),
                            "activity": presence_ctx.get("activity"), "text": r.get("text")})
            continue
        # Released from a presence hold: close the open segment into the accumulator. Runs for BOTH
        # presence rules (place and driving) because it keys off the stamp, not off `require_place` —
        # the driving rule has no per-entry marker to key off. A re-hold later just opens a new segment.
        if fire and r.get("presence_deferred_since"):
            try:
                held_sec = (now - parse_iso(r["presence_deferred_since"])).total_seconds()
            except (ValueError, TypeError):
                held_sec = 0.0  # a garbled stamp costs the exemption, never the nudge
            prior = r.get("presence_held_sec")
            prior = float(prior) if isinstance(prior, (int, float)) and prior > 0 else 0.0
            r["presence_held_sec"] = round(prior + max(0.0, held_sec), 3)
            r.pop("presence_deferred_since", None)
            changed = True
        # Live-session defer: HOLD a non-piercing nudge (stamp NOTHING → re-checked next loop, defer-not-
        # drop) while an interactive /assistant session is live, so it doesn't buzz into a live
        # conversation; it fires naturally once the session ages out of the TTL. Piercing items (Call Me /
        # Critical) are never held. Composes with quiet: a quiet window would already have dropped this
        # non-piercing entry above, so this only bites when NOT quiet — piercing still pierces both gates.
        if fire and session_live and not pierces:
            signals.append({"kind": "reminder_deferred_session", "id": r.get("id"), "text": r.get("text")})
            continue
        # Staleness cutoff: a non-piercing nudge more than MAX_LATENESS_SEC past due has stopped being a
        # reminder. Consumed, same stamping as quiet/curfew. Piercing entries are exempt, and so is any
        # time the presence gate held it OR the catch-up stagger has — entry_lateness_sec subtracts both
        # `presence_held_sec` and the running `stagger_deferred_since` segment the gate below maintains.
        #
        # Placed AFTER the live-session defer and BEFORE the stagger, and the order is the fix: a nudge
        # held three hours by a live session SHOULD die here, while a nudge held by presence never
        # reaches this gate at all (it `continue`d above), and one released from a presence hold arrives
        # with its held time already netted out. A nudge merely waiting its turn in the drip must not go
        # stale for that alone — but an entry ALREADY stale the first tick it reaches the stagger gate
        # still dies right here (the segment only opens then): the drip is never a hiding place.
        if fire and not pierces:
            late_sec = entry_lateness_sec(r, due, now)
            if late_sec > MAX_LATENESS_SEC:
                r["suppressed_at"] = now.isoformat().replace("+00:00", "Z")
                changed = True
                stale_signal = {"kind": "reminder_suppressed_stale", "id": r.get("id"),
                                "reminder_id": r.get("reminder_id"), "text": r.get("text"),
                                "due_at": r.get("due_at"), "late_sec": int(late_sec),
                                "presence_held_sec": int(r.get("presence_held_sec") or 0),
                                "stagger_held_sec": int(_stagger_held_sec(r, now))}
                signals.append(stale_signal)
                # A silent kill IS the failure, not a side effect of one — `presence.log` is a
                # daemon-internal log nobody reads day to day. Durable + fail-open: a broken ledger
                # costs the breadcrumb, never this pass. The EOD Wrap reads
                # `reminder_suppressions.count_today`.
                reminder_suppressions.record(state_dir, stale_signal, now=now)
                continue
        # Catch-up stagger gate: a released backlog must drip, not wall. A non-piercing nudge is HELD
        # (re-checked next loop, defer-not-drop) if we already fired one this pass, or if the last
        # non-piercing fire was under CATCHUP_STAGGER_SEC ago — UNLESS an ack newer than that fire has
        # aged past ACK_ADVANCE_DEBOUNCE_SEC (`_ack_advances_stagger`), in which case the window is
        # released early: the owner has caught up, so the next pending nudge follows the ack, not the
        # clock. The one-per-pass half is untouched by acks. Piercing items skip the gate.
        #
        # `stagger_deferred_since` is stamped the first tick this predicate holds a row (never reset
        # while it keeps holding) — the segment `entry_lateness_sec`/`_stagger_held_sec` read above so
        # the staleness gate stops counting this wait as lateness once it starts. Cleared once the row
        # is no longer held (about to fire) — bookkeeping only.
        stagger_holds = (fire and not pierces
                         and (fired_nonpiercing
                              or (last_nudge_fire is not None
                                  and (now - last_nudge_fire).total_seconds() < CATCHUP_STAGGER_SEC
                                  and not _ack_advances_stagger(now, last_nudge_fire, last_ack_at))))
        if stagger_holds:
            if not r.get("stagger_deferred_since"):
                r["stagger_deferred_since"] = now.isoformat().replace("+00:00", "Z")
                changed = True
            signals.append({"kind": "reminder_stagger_held", "id": r.get("id"), "text": r.get("text")})
            continue
        if r.pop("stagger_deferred_since", None) is not None:
            changed = True
        # Due now. Reminders are act-low (the owner's own content) — the daemon delivers directly.
        if fire:
            raw = r.get("text", "(no text)")
            channel = (r.get("channel") or "telegram").lower()
            res, used = _deliver_reminder(
                channel, raw, telegram_env, call_env, discord_env,
                escalate=bool(r.get("escalate")),
                interval_sec=r.get("interval_sec"), max_attempts=r.get("max_attempts"),
                # `pierces` was computed at the top of this iteration by the SAME predicate the quiet,
                # curfew, session and staleness gates all read, so the topic split and the pierce set
                # cannot disagree about one entry.
                pierces=pierces, state_dir=state_dir)
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
                # The assertions log (optional `mouth` module, a later port): record what the owner was
                # actually told, in the wording they got it in. Recorded HERE rather than inside the
                # send helpers because this is the frame that knows the `kind` and has already confirmed
                # the send landed. `record_assertion` never raises; absent module = no record.
                if _mouth is not None:
                    _mouth.record_assertion(
                        state_dir, surface=used, kind="reminder", speaker="sentinel",
                        text=raw if used == "call" else f"⏰ Reminder: {raw}", now=now)
                # A non-piercing fire opens the stagger window (and spends this pass's one slot); piercing
                # items are a separate lane — they neither consume nor reset the drip clock.
                if not pierces:
                    fired_nonpiercing = True
                    last_nudge_fire = now
                    _save_last_nudge_fire(state_dir, now)
                fired = {"kind": "reminder_fired", "id": r.get("id"), "channel": used, "text": raw}
                # **WHICH THREAD IT LANDED IN, WHEN IT LANDED IN ONE** — read off the send result rather
                # than off the purpose we asked for, because the two differ on exactly the cases that
                # matter (topics off, a failed detect, a stale id that fell back). Additive and
                # absent-not-null, so a main-chat fire emits the signal it always did.
                thread = res.get("message_thread_id")
                if isinstance(thread, int) and not isinstance(thread, bool) and thread > 0:
                    fired["message_thread_id"] = thread
                signals.append(fired)
            elif res.get("ambiguous"):
                # The send helpers classify a failure `ambiguous` precisely when the request may already
                # have been delivered (it went out and the answer was lost, a 429 outran its own retry
                # ceiling, or the subprocess timed out with the child still on the wire). HOLD, never
                # re-arm: stamp it so it is never retried automatically — under-sending is recoverable;
                # double-sending is not — and surface it distinctly so a human can check whether it
                # actually landed.
                r["ambiguous_send_at"] = now.isoformat().replace("+00:00", "Z")
                changed = True
                failures.record(state_dir, "sentinel.check_reminders", "reminder_send_ambiguous",
                                detail=f"id={r.get('id')} channel={used} error={res.get('error')}", now=now)
                signals.append({"kind": "reminder_send_ambiguous", "id": r.get("id"), "channel": used,
                                "error": res.get("error")})
            else:
                # A nudge that was supposed to fire and didn't — the send itself failed, not a Python
                # exception, so `failures.record` is called here rather than from an except handler.
                # Never raises; a failed append costs the row, never the signal below it.
                failures.record(state_dir, "sentinel.check_reminders", "reminder_send_failed",
                                detail=f"id={r.get('id')} channel={used} error={res.get('error')}", now=now)
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
