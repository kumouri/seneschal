#!/usr/bin/env python3
"""The durable transcript archive — step 1 of a full transcript view. Standard library only.

The retention posture it follows: an eviction must never be a destructive in-place rewrite of the
record it evicts from. The live window it mirrors is `cockpit_pipe.py`'s transcript ring.

## The problem, precisely

`cockpit_pipe.append_transcript_event` writes every warm-session `chat.event` to
`state/warm-transcript.jsonl`, and `_maybe_trim` **rewrites that file down to its retained tail** once
it grows past the cap. Older events are *deleted from disk*, not merely unread — no view, however
good, can retrieve them afterwards.

This module is the other half: the same events, appended to a **dated file that is never rewritten**.

## What this is NOT — and this is the part worth reading

**It is not a third copy of the conversation.** Several stores already hold overlapping slices of a
turn, and adding one that duplicates another would be the mistake, not the feature:

| Store | Holds | Retention |
|---|---|---|
| `turns.jsonl` (`turns.py`) | **what was SAID, both sides, verbatim and uncapped** | indefinite |
| `assertions.jsonl` (`mouth.py`) | what the **assistant** said, per send site, one side | 30 days |
| `warm-transcript.jsonl` (`cockpit_pipe.py`) | the **agent event stream** — turn boundaries, tool calls, model, cost, usage, duration | ~25 whole sessions, **destructively trimmed** |

`turns.jsonl` already answers *"what did we say to each other"* durably and losslessly. **Nothing here
duplicates it, and a view built on top of this must join to it rather than re-record it.** What this
archive holds is the third row's column: the **agent activity** around each turn — which tools ran with
what input, which model answered, what the turn cost and how long it took, and where turns began and
ended. That data exists nowhere else, is destroyed on a ~25-session horizon, and gets strictly more
valuable as `../docs/session-trace-spec.md`'s later phases widen those rows (uncapped tool input, then
tool results).

So: rows here are **byte-identical to the ring buffer's**, deliberately. A view parses one shape and
reads the archive for history and the ring for the live tail, with no schema to reconcile — and the
archive is a strict superset of the ring's history.

## Retention — keep everything, by decision

`RETENTION_DAYS = 0` is a **decision**, not a placeholder: keep everything, `prune` is a no-op at that
default, and **nothing in the tree calls it**, asserted by a test. Same sentinel and same posture as
`turns.prune`: wiring a sweep in here by copy-paste from `mouth.py`'s 30-day one would be a silent,
unrecoverable data loss. The reasoning: a complete chat log is the most personal thing in `state/`, and
`state/` is gitignored — for most of it there is no copy anywhere. The `prune` mechanism stays, tested
and uncalled, for a future owner decision that asks for one.

**The size question has a sensor.** Keep-everything is paired with a threshold — the owner wants to
hear when the archive passes ~200 MB — and that threshold is watched by `transcript_size_watch.py`,
run from Dream: a nightly directory-size sum that raises it **once** with the current size and the
measured growth rate, then goes quiet. It deletes nothing — a thermometer, not a thermostat. A
threshold nothing measures is an escalation gated on a metric nobody built, and that stays silent
through exactly the growth it was meant to catch.

## Crash safety — by having nothing to interrupt

There is **no roll step, no move, and no read-modify-write on the write path.** An event is appended to
the file its own timestamp names; the day file changes because the day changed, not because anything
rotated it. So a daemon killed at any instant cannot lose or duplicate an already-written line.

The one residual is a partial final line if the process dies mid-`write`, and it is bounded to exactly
that row rather than left to spread: `_append_lines` **heals a torn tail** before appending. Without
that, a truncated fragment (no trailing newline) would swallow the next event into one unparseable
line, so every crash would silently cost the first event after it — which a test caught on the first
run. Beyond that, every reader here skips unparseable lines, the established tolerance.

`fsync` is deliberately not called: this runs on the daemon's event loop inside the per-turn tee, and
the house pattern (`turns.py`, `mouth.py`) trades a durability window measured in milliseconds for not
putting a disk sync on the chat hot path.

**The fail-open contract, same as its two siblings:** `archive_event` returns a bool and **never
raises**. A failed append costs the row, never the turn. No caller wraps it in a try/except.

Day files are named by the **owner's local** date (`tz_common`: the configured owner zone, falling
back to machine-local). Each row keeps its own UTC `ts`, which stays authoritative.

USAGE:
  python transcript_archive.py days                  # which dates have been archived
  python transcript_archive.py tail --day 2026-08-09 --limit 20
  python transcript_archive.py stats
  python transcript_archive.py backfill              # idempotent; rescues the ring buffer's live tail
  python transcript_archive.py prune --days 400      # NOTHING calls this; retention is keep-everything
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone

import paths

try:  # the owner's configured zone names the day file; absent → machine-local
    import tz_common as _tz_common
except Exception:  # noqa: BLE001 — a missing helper must never cost the archive a row
    _tz_common = None

ARCHIVE_DIR = "transcripts"          # state/transcripts/YYYY-MM-DD.jsonl
RING_FILE = "warm-transcript.jsonl"  # cockpit_pipe.TRANSCRIPT_FILE — the capped live buffer we mirror

# Keep everything — a decision, not a placeholder (module docstring). `0` is the same "keep everything"
# sentinel `turns.RETENTION_DAYS` uses, so copying a nightly sweep in from `mouth.py` is a no-op
# rather than a deletion. The size threshold is watched by `transcript_size_watch.py`, not enforced
# here — nothing in this module deletes on a size, and adding that would be a thermostat where a
# thermometer was asked for.
RETENTION_DAYS = 0

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# `SENESCHAL_STATE_DIR`, else `seneschal/state` — resolved once at import (`paths.STATE_DIR`).
DEFAULT_STATE_DIR = paths.STATE_DIR

# `2026-08-09.jsonl` and nothing else. Used to enumerate the archive, so it doubles as the guard that
# keeps a stray file in the directory from being read as a day.
_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")

# The tee is called from the daemon's event loop AND from the warm session's blocking stdout reader
# on a worker thread, exactly like the ring
# buffer's own `_transcript_lock`. Same posture, separate lock: the archive append must not contend
# with — or be able to wedge — the ring append that backs the live pane.
_append_lock = threading.Lock()

# Paths this PROCESS has already appended to successfully. A completed append ends in a newline, so a
# tail we wrote ourselves cannot be torn and needs no re-checking — which takes the steady-state cost
# back to one open per event instead of two. It is deliberately per-process and never persisted: a
# torn tail can only be left by a process that DIED, and a dead process's cache dies with it, so the
# next boot starts empty and checks. (A second writer appending torn lines concurrently would defeat
# it, but only the daemon writes here, and the heal is best-effort by nature.)
_healed: set = set()


def default_state_dir() -> str:
    return DEFAULT_STATE_DIR


def archive_dir(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, ARCHIVE_DIR)


def archive_path(state_dir: str | None, day: str) -> str:
    return os.path.join(archive_dir(state_dir), f"{day}.jsonl")


def ring_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, RING_FILE)


# ------------------------------------------------------------------------------------ day routing

def _parse_ts(value) -> datetime | None:
    """Tolerant read of an event's `ts`. Anything unparseable reads as None and every caller treats
    that as "I cannot place this row in time" rather than guessing at a date."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def local_day(when: datetime | None = None) -> str:
    """The **owner's local** date as `YYYY-MM-DD` — which file an event is filed under.

    Read through `tz_common` (the configured owner zone, with its documented fallback to the
    machine-local clock), so the day files line up with the owner's calendar rather than the host's.

    **Nothing here decides anything on this value.** It picks a filename. Each row keeps its own UTC
    `ts`, which stays authoritative — so a reader must join on `ts`, and must not assume a day file
    holds exactly one UTC calendar day. If the configured zone changes, the archive is still complete
    and still correctly ordered; only the file boundaries move. That is why a zone drift is a filing
    quirk here rather than a bug, and why the tests inject their instants (and pin the zone) rather
    than trusting the wall clock."""
    instant = when or datetime.now(timezone.utc)
    if _tz_common is not None:
        return _tz_common.to_local(instant).strftime("%Y-%m-%d")
    return instant.astimezone().strftime("%Y-%m-%d")


def day_for_event(event, now: datetime | None = None) -> str:
    """Which dated file this event belongs in — from the event's OWN `ts` when it has one, falling back
    to now.

    Reading the event's stamp rather than the clock is what makes the live append and the backfill
    agree: an event stamped at 23:59:59 files under that day even if the append (or a backfill days
    later) happens afterwards. If they disagreed, the backfill could not be idempotent."""
    ts = _parse_ts(event.get("ts")) if isinstance(event, dict) else None
    return local_day(ts or now)


# ------------------------------------------------------------------------------------------ write

def _tail_is_torn(path: str) -> bool:
    """True when the file exists, is non-empty, and does NOT end in a newline — i.e. its last line was
    cut off mid-write by a process that died.

    Cheap (one open, one seek, one byte) and worth every bit of it: see `_append_lines`."""
    try:
        with open(path, "rb") as fh:
            if fh.seek(0, os.SEEK_END) == 0:
                return False
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:
        return False


def _append_lines(path: str, lines) -> None:
    """Append complete lines, **healing a torn tail first**. The one write primitive in this module.

    **Why the heal exists, and it is not theoretical — a test caught it.** Without it, a line truncated
    by a kill costs *two* events, not one: the torn fragment has no trailing newline, so the next
    append concatenates onto it and the two become a single unparseable line that every reader skips.
    A crash would therefore silently eat the first event after every restart. One byte closes the
    fragment off, leaving the damage at exactly the row that was in flight.

    Writing that lone newline is itself an append, so dying between it and the payload is harmless and
    a re-run is safe — the file still only ever grows, and there is still no read-modify-write for an
    interruption to land in the middle of."""
    with _append_lock:
        prefix = "" if path in _healed or not _tail_is_torn(path) else "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(prefix + "".join(lines))
        _healed.add(path)  # only on success — a failed write leaves the tail's state unknown


def archive_event(state_dir: str | None = None, event=None, now: datetime | None = None) -> bool:
    """Append one event, verbatim, to its day's file. Returns True iff it hit disk.

    **This never raises** (the fail-open contract). It is called from `cockpit_pipe.
    append_transcript_event`, which sits inside the daemon's per-turn tee — a full disk, a bad
    permission or an unserializable value costs the row and nothing else.

    The line is byte-identical to the ring buffer's, on purpose: same `json.dumps(..., ensure_ascii=
    False)`, same shape, so one parser reads both and the backfill can dedupe on the raw line."""
    if not isinstance(event, dict):
        return False
    try:
        line = json.dumps(event, ensure_ascii=False) + "\n"
        path = archive_path(state_dir, day_for_event(event, now))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _append_lines(path, [line])
        return True
    except Exception:  # noqa: BLE001 — see the docstring: the row is never worth the turn
        return False


# ------------------------------------------------------------------------------------------ read

def available_days(state_dir: str | None = None) -> list:
    """Every archived date, oldest first. Fail-open: an absent directory reads as no days.

    Nothing in step 1 calls this except the CLI, the backfill and the tests — the view is step 2. It
    exists now for the same reason `turns.read_turns` did in its phase 0: a write-only file nobody can
    inspect by hand cannot be verified, and an archive whose contents are unreadable is indistinguish-
    able from one that is silently empty."""
    try:
        names = os.listdir(archive_dir(state_dir))
    except OSError:
        return []
    return sorted(m.group(1) for m in (_DAY_FILE_RE.match(n) for n in names) if m)


def read_day(state_dir: str | None = None, day: str = "", limit: int | None = None) -> list:
    """One day's events, oldest first, malformed lines skipped. `limit` keeps the **newest** N.

    Tolerant by design — a partial final line from a process killed mid-write is skipped rather than
    raised on, which is what makes the append-only write path safe without an fsync."""
    rows: list = []
    try:
        with open(archive_path(state_dir, day), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    if limit is not None and limit >= 0:
        rows = rows[-limit:] if limit else []
    return rows


def stats(state_dir: str | None = None) -> dict:
    """Size and span of the archive, rows parsed and counted by kind. Fail-open: an absent archive
    reads as zero, never an error.

    **This is the rich read, and it parses every line** — `transcript_size_watch.measure` deliberately
    does not call it: the nightly size check needs `st_size` and nothing else, and at 200 MB the
    difference is minutes."""
    days = available_days(state_dir)
    rows = total_bytes = 0
    by_kind: dict = {}
    for day in days:
        try:
            total_bytes += os.path.getsize(archive_path(state_dir, day))
        except OSError:
            pass
        for row in read_day(state_dir, day):
            rows += 1
            kind = row.get("kind") or "unknown"
            by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "rows": rows,
        "bytes": total_bytes,
        "by_kind": by_kind,
        "retention_days": RETENTION_DAYS,
        # Keep-everything is a decision. `retention_days` is 0 either way, so this is the field that
        # says whether the 0 is a decision or a placeholder.
        "retention_decided": True,
    }


# --------------------------------------------------------------------------------------- backfill

def backfill_from_ring(state_dir: str | None = None, now: datetime | None = None) -> int:
    """Copy whatever is still in `warm-transcript.jsonl` into the archive. Returns rows appended.

    **Why this exists.** Everything that happened before this module was deployed lives only in the
    ring buffer, and the next trim deletes it. One pass at daemon boot rescues the ~25 sessions that
    are still there; without it, the archive starts empty and that history is lost for good — which is
    the exact loss the whole change is about.

    **Idempotent**, because it runs on every boot and the daemon reloads on every merge. Dedupe is on
    the **raw line**: `archive_event` and `append_transcript_event` serialize identically, so a row
    already archived compares equal byte-for-byte. Two byte-identical events would collapse to one —
    they carry a microsecond `ts`, so that is theoretical, and collapsing indistinguishable rows is the
    safe direction anyway.

    A line whose `ts` will not parse rides with its predecessor (the same instinct as
    `cockpit_pipe._session_aware_tail`, where a corrupt line stays with its neighbours instead of
    splitting them) rather than being discarded — a row that cannot say when it happened is still a row
    that happened. Fail-open throughout: an unreadable ring, an unwritable archive, anything at all
    returns 0 rather than raising into the daemon's startup."""
    try:
        with open(ring_path(state_dir), encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh]
    except OSError:
        return 0

    # Group into (day, [raw_line]) in file order, carrying the last known day forward.
    buckets: dict = {}
    order: list = []
    fallback = local_day(now)
    for raw in lines:
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            event = None
        if isinstance(event, dict) and _parse_ts(event.get("ts")) is not None:
            day = day_for_event(event, now)
            fallback = day
        else:
            day = fallback
        if day not in buckets:
            buckets[day] = []
            order.append(day)
        buckets[day].append(raw)

    appended = 0
    for day in order:
        path = archive_path(state_dir, day)
        try:
            with open(path, encoding="utf-8") as fh:
                seen = {ln.strip() for ln in fh if ln.strip()}
        except OSError:
            seen = set()
        fresh = [raw for raw in buckets[day] if raw not in seen]
        if not fresh:
            continue
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _append_lines(path, [raw + "\n" for raw in fresh])
        except OSError:
            continue  # this day's rescue failed; the others are independent and still worth trying
        appended += len(fresh)
    return appended


# ------------------------------------------------------------------------------------------ prune

def prune(state_dir: str | None = None, days: int = RETENTION_DAYS,
          now: datetime | None = None) -> list:
    """Delete whole day files older than `days`. Returns the days dropped, oldest first.

    **NOTHING CALLS THIS**, and that is the design, not an oversight: retention here is
    keep-everything by decision (module docstring), `days` defaults to `RETENTION_DAYS = 0`, and 0
    keeps everything. The mechanism stays tested and uncalled so that a *different* future decision
    has something to reach for; under the current one the archive only grows, and
    `transcript_size_watch.py` is what reports how fast.

    Whole files, never a partial rewrite — an eviction must not rewrite in place, and dropping a day whole means the archive is never left
    holding a truncated day that reads as a quiet one."""
    if not days or days <= 0:
        return []
    cutoff = local_day((now or datetime.now(timezone.utc)) - timedelta(days=days))
    dropped = []
    for day in available_days(state_dir):
        if day >= cutoff:
            continue
        try:
            os.remove(archive_path(state_dir, day))
        except OSError:
            continue
        dropped.append(day)
    return dropped


# ---------------------------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="The durable transcript archive (step 1 of the view).")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("days", help="list the archived dates")

    tail = sub.add_parser("tail", help="print one day's newest events")
    tail.add_argument("--day", required=True)
    tail.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats", help="size, span and rows by kind (parses every line)")
    sub.add_parser("backfill", help="rescue the ring buffer's live tail (idempotent)")

    pr = sub.add_parser("prune", help="drop whole days older than --days (NOTHING calls this)")
    pr.add_argument("--days", type=int, default=RETENTION_DAYS)

    args = p.parse_args(argv)

    if args.cmd == "days":
        for day in available_days(args.state_dir):
            print(day)
        return 0
    if args.cmd == "tail":
        for row in read_day(args.state_dir, args.day, limit=args.limit):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.cmd == "stats":
        print(json.dumps(stats(args.state_dir), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "backfill":
        print(json.dumps({"ok": True, "appended": backfill_from_ring(args.state_dir)}))
        return 0
    dropped = prune(args.state_dir, days=args.days)
    print(json.dumps({"ok": True, "dropped": dropped}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
