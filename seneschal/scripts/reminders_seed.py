#!/usr/bin/env python3
"""Seed a whole day of **exact-time** reminder nudges from an ⏰ Reminders row.

This is the general case of ``reminders_roll.py``: where a *roll* is a fixed every-N-hours cadence
baked into a ``ROLLS`` config, a *seed* takes any ⏰ row's own **explicit fire time(s)** and queues
that row's nudges for the whole local day. It's the mechanism that retired the four fixed reminder
slots (Morning/Midday/Evening/Bedtime) in favour of arbitrary per-reminder times — see
``seneschal/docs/reminder-exact-time-scheduling-spec.md`` and ``seneschal/references/reminders-policy.md``.

The presence daemon's **once-per-local-day seed pass** (``presence.maybe_seed_day``, triggered on the
first tick of each new owner-local date) spawns the Reminders subagent in SEED mode. That brain run does
the parts that need the store — apply pending acks, run the daily reset, decide which rows are due today
— then calls **this script once per due row** to enqueue that row's exact-time nudges. The daemon's
~5 s delivery tick (``sentinel.check_reminders``) then fires each entry at its due minute, applying
every gate (quiet / ack / presence / catch-up stagger) exactly as for any queued nudge.

**Why the time math lives here, not in the LLM:** parsing ``"08:00, 20:00"``, the ``Time Window`` →
default-time fallback, and the 90-minute re-fire cadence for important/nag items must be exact and
DST-correct. Like ``roll_entries`` it computes off ``now_local``'s fixed UTC offset, so it is
deterministic and unit-testable on any host (CI runs on UTC).

Behaviour:
  * **Primary times** = the row's ``Times`` (comma-list of ``HH:MM`` local), else the ``Time Window``
    default (Morning 08:00 / Midday 12:30 / Evening 18:30 / Bedtime 21:30 / Anytime 09:00). A row with
    neither has no time basis and seeds nothing.
  * **Re-fire** (importance ≥ ⭐ High **or** ``Nag Until Done``) adds a nudge every 90 min after each
    primary through the end of the local day. Re-fire entries keep the fire-time **ack gate ON** (the
    default), so the first ack cancels the day's remaining re-fires — the opposite of a *roll*, which
    opts out so one ack can't hush the rest. (Contrast ``reminders_roll.py``.)
  * **Idempotent + whole-day.** Stable ids ``rmd-<local-date>-<slug>-<HHMM>``; re-running skips ids
    already queued (fired or not). Seeds *all* of today's times, including any already past at seed
    time (a late seed after a slept-through morning) — the delivery layer's catch-up stagger + ack gate
    handle late/again firing, exactly as for a released backlog.

Stdlib only. Times in ``reminders.json`` are UTC ISO-8601 with a trailing ``Z``. See
``seneschal/state/README.md`` for the entry schema.

Usage (one row per call — the SEED pass loops over due rows):
  python reminders_seed.py --reminder-id <page id> --slug meds-am --text "Morning meds." \
      --time-window Morning --importance "🚨 Critical" --pierce-quiet
  python reminders_seed.py --reminder-id <id> --slug teeth-pm --text "Brush teeth." \
      --times "22:30" --importance "📌 Low" --nag
  python reminders_seed.py --reminder-id <id> --slug walk --text "Walk." --times "16:00" --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import reminders_acks as ra  # queue_lock — serialize reminders.json writers (sibling, stdlib)
from reminders_enqueue import _now_z, _slug, load_reminders, save_reminders

# Owner-timezone plumbing (guarded, presence.py house style): the CLI's default `now_local` follows
# the owner's configured zone via tz_common; without it (bare interpreter), machine-local — the same
# fallback every sibling has. Callers that pass an explicit aware now_local are untouched.
try:
    import tz_common as _tz_common
except ImportError:  # pragma: no cover — behave exactly like a machine-local install
    _tz_common = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

# `Time Window` → default fire time when a row leaves `Times` empty. These are the migration anchors
# (each is the old fixed slot's time) AND the run-time fallback, so an un-migrated row fires at exactly
# the minute it used to. Keep in lockstep with references/reminders-policy.md + databases.md.
WINDOW_DEFAULTS = {
    "Morning": "08:00",
    "Midday": "12:30",
    "Evening": "18:30",
    "Bedtime": "21:30",
    "Anytime": "09:00",
}

# Re-fire cadence for unacked important/nag items: every 90 minutes from
# each primary time through the end of the local day, until acked (the ack gate cancels the rest).
REFIRE_INTERVAL_MIN = 90
REFIRE_END = "23:59"  # last local minute a re-fire may land (stays on today's date)
# Importance tiers that re-fire until Done (mirrors reminders-policy.md). `Nag Until Done` re-fires too.
REFIRE_IMPORTANCE = {"🛑 Super-Critical", "🚨 Critical", "⭐ High"}


def _local_now() -> datetime:
    """The owner's current local time (tz_common when importable, machine-local otherwise) — the
    default `now_local` for the CLI/refill paths, so the seeded *local day* follows the owner's
    calendar (rule 5), not the machine's."""
    if _tz_common is not None:
        return _tz_common.local_now()
    return datetime.now().astimezone()


def parse_hhmm(s: str) -> tuple[int, int]:
    """'HH:MM' → (hour, minute). Raises ValueError on anything unparseable."""
    h, m = s.strip().split(":")
    return int(h), int(m)


def primary_times(times: str | None, time_window: str | None) -> list[tuple[int, int]]:
    """The row's explicit ``HH:MM`` primary fire times (deduped, sorted), or the ``Time Window``
    default if ``Times`` is empty/absent. Empty list if the row has neither basis (the caller skips
    it — a row with no time never seeds). Malformed items in the list are ignored, not fatal."""
    out: list[tuple[int, int]] = []
    if times:
        for part in times.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                h, m = parse_hhmm(part)
            except ValueError:
                continue
            if 0 <= h < 24 and 0 <= m < 60:
                out.append((h, m))
    if not out and time_window:
        default = WINDOW_DEFAULTS.get(time_window.strip())
        if default:
            out.append(parse_hhmm(default))
    return sorted(set(out))


def is_refire(importance: str | None, nag: bool) -> bool:
    """True if this row re-fires until acked: importance ≥ ⭐ High **or** ``Nag Until Done``."""
    return bool(nag) or (importance or "").strip() in REFIRE_IMPORTANCE


def day_times(primaries: list[tuple[int, int]], refire: bool,
              interval_min: int = REFIRE_INTERVAL_MIN, end: str = REFIRE_END) -> list[tuple[int, int]]:
    """All ``(hour, minute)`` fire times for the local day: the primaries, plus — when ``refire`` —
    every ``interval_min`` after each primary through ``end`` (same local day). Deduped + sorted, so
    re-fires from multiple primaries (a twice-daily critical firing off both 08:00 and 20:00) merge
    cleanly and never double-up on a shared minute."""
    fires = set(primaries)
    if refire and primaries:
        end_h, end_m = parse_hhmm(end)
        end_total = end_h * 60 + end_m
        for (h, m) in primaries:
            total = h * 60 + m + interval_min
            while total <= end_total:
                fires.add((total // 60, total % 60))
                total += interval_min
    return sorted(fires)


def seed_entries(row: dict, now_local: datetime) -> list[dict]:
    """The ``reminders.json`` entries for one ⏰ row's whole local day.

    ``row`` keys: ``reminder_id`` (the ⏰ page id — carried so an ack can dequeue/gate the day's
    nudges), ``text`` (the nudge line), optional ``times`` / ``time_window`` / ``importance`` /
    ``nag`` / ``channel`` / ``pierce_quiet`` / ``slug``. Deterministic given ``now_local`` (uses that
    moment's fixed UTC offset, not the host tz db) so it is testable on any machine. ``now_local``
    must be timezone-aware. Returns ``[]`` if the row has no time basis."""
    primaries = primary_times(row.get("times"), row.get("time_window"))
    if not primaries:
        return []
    refire = is_refire(row.get("importance"), bool(row.get("nag")))
    slug = _slug(str(row.get("slug") or row.get("reminder_id") or row.get("text") or "reminder"))
    reminder_id = row.get("reminder_id")
    text = row.get("text") or ""
    channel = row.get("channel") or "telegram"
    pierce = bool(row.get("pierce_quiet"))

    offset = now_local.utcoffset() or timedelta(0)
    local_tz = timezone(offset)
    day = now_local.date()

    out = []
    for (h, m) in day_times(primaries, refire):
        due_utc = datetime(day.year, day.month, day.day, h, m, tzinfo=local_tz).astimezone(timezone.utc)
        entry = {
            "id": f"rmd-{day.isoformat()}-{slug}-{h:02d}{m:02d}",
            "text": text,
            "due_at": due_utc.isoformat().replace("+00:00", "Z"),
            "channel": channel,
            "created_at": _now_z(),
            "fired_at": None,
        }
        if reminder_id:
            entry["reminder_id"] = reminder_id
        # NB: no `ack_gate` key → the fire-time ack gate applies (default). One ack cancels the day's
        # remaining re-fires for this row. (A *roll* sets ack_gate=false for the opposite behaviour.)
        if pierce:
            entry["pierce_quiet"] = True
        out.append(entry)
    return out


def refill_seed(state_dir: str, row: dict, now_local: datetime | None = None) -> int:
    """Append this row's missing day entries to ``reminders.json`` (idempotent — skips ids already
    present, fired or not). Returns the number added; writes only if something was added. Held under
    the cross-process queue lock (the daemon's ``check_reminders`` / a sibling enqueue may be
    rewriting the file concurrently)."""
    if now_local is None:
        now_local = _local_now()
    entries = seed_entries(row, now_local)
    if not entries:
        return 0
    path = os.path.join(state_dir, "reminders.json")
    with ra.queue_lock(state_dir):
        queue = load_reminders(path)
        have = {r.get("id") for r in queue if isinstance(r, dict)}
        added = 0
        for e in entries:
            if e["id"] in have:
                continue
            queue.append(e)
            have.add(e["id"])
            added += 1
        if added:
            save_reminders(path, queue)
    return added


def main() -> int:
    p = argparse.ArgumentParser(description="Seed one ⏰ row's exact-time nudges for the whole local day.")
    p.add_argument("--reminder-id", required=True, help="the ⏰ Reminders row page id (carried so an ack "
                   "can dequeue/gate the day's nudges via reminders_dequeue.py / the fire-time ack gate)")
    p.add_argument("--text", required=True, help="the nudge text (shown after '⏰ Reminder: ')")
    p.add_argument("--slug", default=None, help="short stable+unique token for the entry ids "
                   "(rmd-<date>-<slug>-<HHMM>); default derives from reminder-id/text")
    p.add_argument("--times", default=None, help="comma-list of HH:MM local fire times, e.g. '08:00, 20:00'")
    p.add_argument("--time-window", default=None, choices=list(WINDOW_DEFAULTS),
                   help="coarse fallback used only when --times is empty (maps to a default time)")
    p.add_argument("--importance", default=None, help="⏰ Importance value (drives re-fire eligibility)")
    p.add_argument("--nag", action="store_true", help="Nag Until Done — re-fire regardless of importance")
    p.add_argument("--channel", default="telegram", choices=["telegram", "call", "discord"])
    p.add_argument("--pierce-quiet", action="store_true",
                   help="fire through a quiet window (set for 🚨 Critical / 🛑 Super-Critical rows)")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="dir holding reminders.json")
    p.add_argument("--dry-run", action="store_true", help="print the entries; write nothing")
    args = p.parse_args()

    row = {
        "reminder_id": args.reminder_id,
        "text": args.text,
        "slug": args.slug,
        "times": args.times,
        "time_window": args.time_window,
        "importance": args.importance,
        "nag": args.nag,
        "channel": args.channel,
        "pierce_quiet": args.pierce_quiet,
    }
    now_local = _local_now()
    if args.dry_run:
        entries = seed_entries(row, now_local)
        print(json.dumps({"ok": True, "dry_run": True, "count": len(entries), "entries": entries}, indent=2))
        return 0
    added = refill_seed(args.state_dir, row, now_local=now_local)
    print(json.dumps({"ok": True, "reminder_id": args.reminder_id, "added": added}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
