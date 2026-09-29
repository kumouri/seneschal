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
default-time fallback, and the 90-minute re-fire cadence for ``Nag Until Done`` items must be exact and
DST-correct. Like ``roll_entries`` it computes off ``now_local``'s fixed UTC offset, so it is
deterministic and unit-testable on any host (CI runs on UTC).

Behaviour:
  * **Primary times** = the row's ``Times`` (comma-list of ``HH:MM`` local), else the ``Time Window``
    default (Morning 08:00 / Midday 12:30 / Evening 18:30 / Bedtime 21:30 / Anytime 09:00). A row with
    neither has no time basis and seeds nothing.
  * **Re-fire** (``Nag Until Done`` **alone** — see ``is_refire``) adds a nudge every 90 min after each
    primary through the end of the local day. Re-fire entries keep the fire-time **ack gate ON** (the
    default), so the first ack cancels the day's remaining re-fires — the opposite of a *roll*, which
    opts out so one ack can't hush the rest. (Contrast ``reminders_roll.py``.)
  * **``Nag Until Done`` is the ladder, alone.** ``Importance`` still drives ordering, rib eligibility
    and the quiet-window pierce, but it no longer implies laddering. A High-and-above row seeded without
    the box gets one ``!`` line on stderr and a ``no_ladder`` flag in the seed log (``ladder_gap``) —
    the choice is visible, never refused.
  * **Idempotent + whole-day.** Stable ids ``rmd-<local-date>-<slug>-<HHMM>``; re-running skips ids
    already queued (fired or not). Seeds *all* of today's times, including any already past at seed
    time (a late seed after a slept-through morning) — the delivery layer's catch-up stagger + ack gate
    handle late/again firing, exactly as for a released backlog.
  * **A One-off's ``--due-target`` names its own due date in the text** (:func:`due_suffix`): when a
    fire's local day differs from ``Due / Target`` (a Deadline Watch's ~3-day lookahead, or any Type's
    overdue re-fire), the text gets `` — due <Weekday MM-DD>.`` appended, so an early or overdue surface
    reads as a heads-up rather than "do it now." Omitting ``--due-target`` adds nothing.
  * **``--importance`` rides onto every entry**, so ``sentinel._due_sort_key`` can break a same-minute
    tie by importance (any backend's spelling — ``reminder_importance``) instead of seed-call order.
  * **``--consecutive-misses`` feeds the premise-review question** (``../docs/reminder-premise-spec.md``):
    once a row's ``Consecutive Misses`` clears its threshold and hasn't been asked about since (tracked
    locally — ``reminder_premise_track.py``), one ``!`` line on stderr and a ``premise_review_question``
    flag in the seed log, same convention as ``no_ladder``. Omitting the flag is a no-op.
  * **Every seeded row is logged** to ``state/seed-log.jsonl`` (:func:`_note_seeded`), and
    ``--audit-day`` reports rows that seeded on most recent days but NOT today — the silent-skip
    detector (:func:`audit_day`).

Stdlib only. Times in ``reminders.json`` are UTC ISO-8601 with a trailing ``Z``. See
``seneschal/state/README.md`` for the entry schema.

All reminders.json writers — enqueue, dequeue, roll, reconcile and this seed script — share the SAME
``reminders_acks.queue_lock``, never a second one.

Usage (one row per call — the SEED pass loops over due rows):
  python reminders_seed.py --reminder-id <page id> --slug plants-am --text "Water the plants." \
      --time-window Morning --importance critical --pierce-quiet --nag
  python reminders_seed.py --reminder-id <id> --slug teeth-pm --text "Brush teeth." \
      --times "22:30" --importance low --nag
  python reminders_seed.py --reminder-id <id> --slug walk --text "Walk." --times "16:00" --dry-run
  python reminders_seed.py --reminder-id <id> --slug renew --text "Renew the passport." \
      --times "08:00" --due-target 2026-09-14 --dry-run   # early/overdue fires say "— due Mon 09-14."
  python reminders_seed.py --audit-day                   # rows that seeded recently but not today
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import memory_write as mw  # the seed log is an append; see _note_seeded
import reminder_importance as imp  # canonical importance levels — any backend's spelling
import reminder_premise_track as rpt  # the premise-review question — see check_premise_review
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

# Re-fire cadence for unacked `Nag Until Done` items: every 90 minutes from each primary time through
# the end of the local day, until acked (the ack gate cancels the rest).
REFIRE_INTERVAL_MIN = 90
REFIRE_END = "23:59"  # last local minute a re-fire may land (stays on today's date)
# **NOT a re-fire predicate.** The ladder is `Nag Until Done` ALONE; this is the lowest canonical
# importance (`reminder_importance`) at which a row seeding WITHOUT a ladder is worth one visible line
# rather than silence — the whole mitigation for that rule's one foot-gun (`ladder_gap` + the seed
# log's `no_ladder`).
LADDER_NOTICE_MIN_IMPORTANCE = "high"


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


def is_refire(nag: bool) -> bool:
    """True if this row re-fires until acked — **``Nag Until Done`` alone**.

    The older rule was ``importance ≥ High OR Nag Until Done``, which made the checkbox able only to
    ADD laddering and never to remove it, while the schema called the two fields "independent". They
    now are: **``Importance`` = how loud, and whether it pierces a quiet window; ``Nag Until Done`` =
    whether it chases.** An install migrating from the older rule ticks the box on its High-and-above
    rows (``references/databases.md``).

    **Importance is deliberately not a parameter.** A High row with the box unticked no longer
    ladders, which is the intended semantics and a foot-gun for whoever adds the next row, so the
    warning lives in ``ladder_gap`` where it can be *seen* — not in a second input to this predicate
    that would quietly re-create the OR."""
    return bool(nag)


def ladder_gap(importance: str | None, nag: bool) -> bool:
    """True for a High-and-above row seeding **without** the 90-minute ladder (no ``Nag Until Done``).

    Not a refusal and not a default — the row seeds exactly as asked. It buys one line in the seed log
    and one on stderr, so "important but not chased" is a visible choice in the Run Log rather than a
    silent one. Importance is compared in any backend's spelling (``reminder_importance.at_least``)."""
    return not bool(nag) and imp.at_least(importance, LADDER_NOTICE_MIN_IMPORTANCE)


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


def due_suffix(due_target: date | None, fire_day: date) -> str:
    """``" — due <Weekday> <MM-DD>."`` appended to a nudge when this fire lands on a day other than
    its own ``Due / Target`` — early (a Deadline Watch's lookahead, or a Today Todo seeded ahead of
    its day) or overdue alike. Empty string when there's nothing to add (no ``due_target``, or the
    fire day IS the due day).

    **Why this exists.** A Today Todo due Monday that fires Friday morning with no date in the text
    reads as "do it now" rather than a heads-up. Fixing *when* it fires
    (``reminders_cadence.ONE_OFF_LOOKAHEAD_DAYS`` / ``lookahead_days``) doesn't fix *what it says* —
    a Deadline Watch still fires up to three days early by design, and an overdue re-fire of either
    Type needs the same disambiguation. So any early or overdue surface names its actual due date."""
    if due_target is None or fire_day == due_target:
        return ""
    return " — due %s %02d-%02d." % (due_target.strftime("%a"), due_target.month, due_target.day)


def seed_entries(row: dict, now_local: datetime) -> list[dict]:
    """The ``reminders.json`` entries for one ⏰ row's whole local day.

    ``row`` keys: ``reminder_id`` (the ⏰ page id — carried so an ack can dequeue/gate the day's
    nudges), ``text`` (the nudge line), optional ``times`` / ``time_window`` / ``importance`` /
    ``nag`` / ``channel`` / ``pierce_quiet`` / ``slug`` / ``due_target`` (a :class:`date` — a
    One-off's ``Due / Target``; see :func:`due_suffix`). Deterministic given ``now_local`` (uses that
    moment's fixed UTC offset, not the host tz db) so it is testable on any machine. ``now_local``
    must be timezone-aware. Returns ``[]`` if the row has no time basis."""
    primaries = primary_times(row.get("times"), row.get("time_window"))
    if not primaries:
        return []
    refire = is_refire(bool(row.get("nag")))
    slug = _slug(str(row.get("slug") or row.get("reminder_id") or row.get("text") or "reminder"))
    reminder_id = row.get("reminder_id")
    text = (row.get("text") or "") + due_suffix(row.get("due_target"), now_local.date())
    channel = row.get("channel") or "telegram"
    pierce = bool(row.get("pierce_quiet"))
    importance = row.get("importance") or None

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
        if importance:
            # Read by `sentinel._due_sort_key` to break same-`due_at` ties (super-critical > critical
            # > high > notable > low) — without this a High row seeded into a shared slot can't be told
            # apart from a Low one and just queues in call order. No-op for a row with no importance.
            entry["importance"] = importance
        out.append(entry)
    return out


def check_premise_review(state_dir: str, row: dict, *, mark: bool = True) -> str | None:
    """Is this row due for a "is this still a thing?" premise-review question right now
    (``../docs/reminder-premise-spec.md``)? Reads ``row["consecutive_misses"]`` — absent (``None``)
    for every caller that doesn't pass ``--consecutive-misses``, so this is a no-op until the seed
    pass supplies it. See ``reminder_premise_track.check_premise_review`` for the fail-open rules and
    what ``mark`` does; ``row["text"]``/``row["slug"]``/``row["reminder_id"]`` (in that order) name
    the row in the question, the same fallback order ``seed_entries`` uses for the entry slug."""
    misses = row.get("consecutive_misses")
    if misses is None:
        return None
    title = row.get("text") or row.get("slug") or str(row.get("reminder_id") or "reminder")
    return rpt.check_premise_review(state_dir, row.get("reminder_id"), misses,
                                    row.get("importance"), title, mark=mark)


def refill_seed(state_dir: str, row: dict, now_local: datetime | None = None) -> int:
    """Append this row's missing day entries to ``reminders.json`` (idempotent — skips ids already
    present, fired or not). Returns the number added; writes only if something was added. Held under
    the cross-process queue lock (the daemon's ``check_reminders`` / a sibling enqueue may be
    rewriting the file concurrently).

    Also checks (and, on a due row, marks) the premise-review question once, via
    ``check_premise_review`` — stashed onto ``row["_premise_review_question"]`` so ``main()`` can
    print the same stderr line the check itself decided, rather than recomputing it and risking a
    second call finding the store already marked. Then logs the row to the seed log."""
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
    try:
        row["_premise_review_question"] = check_premise_review(state_dir, row, mark=True)
    except Exception:  # noqa: BLE001 — bookkeeping only, never the seed itself
        row["_premise_review_question"] = None
    _note_seeded(state_dir, row, now_local, added)
    return added


SEED_LOG_FILE = "seed-log.jsonl"


def _note_seeded(state_dir: str, row: dict, now_local: datetime, added: int) -> bool:
    """One line per row seeded per day — the record ``audit_day`` compares against.

    **Why a log and not the queue:** ``reminders.json`` is a live queue, pruned as entries fire, so by
    the time anyone asks "did the evening reminder get seeded today?" the evidence is gone. A handful of
    daily rows left in a stale state can be silently skipped by the seed, with nothing on disk able to
    answer the question — until a human happens to notice at Wrap.

    Fail-open and silent, the house rule for ``state/`` writers: a failed append costs the row, never the
    seed. A reminder that fires without being logged is far better than a log that stops a reminder."""
    try:
        rid = row.get("reminder_id") or row.get("slug") or row.get("text")
        if not rid:
            return False
        rec = {"day": now_local.date().isoformat(), "reminder_id": str(rid),
               "slug": str(row.get("slug") or ""), "added": int(added),
               "at": now_local.isoformat(timespec="seconds")}
        if ladder_gap(row.get("importance"), bool(row.get("nag"))):
            # The durable half of the ladder-gap mitigation: `reminders.json` is pruned as entries
            # fire, so by the time anyone asks "why didn't that Critical chase me?" only this log can
            # answer. `audit_day` ignores the key; it is for the human reading back.
            rec["no_ladder"] = True
            rec["importance"] = str(row.get("importance") or "")
        premise_review = row.get("_premise_review_question")
        if premise_review:
            # Same shape as `no_ladder` above — a durable trace of a question already surfaced on
            # stderr (see `_warn_premise_review`), not a second source of it.
            rec["premise_review_due"] = True
            rec["premise_review_question"] = premise_review
        mw.append_text(os.path.join(state_dir, SEED_LOG_FILE),
                       json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001 — never the seed
        return False


def audit_day(state_dir: str, today: str, window_days: int = 14, min_days: int = 3) -> dict:
    """Which rows seeded on most of the last ``window_days`` but **not today**?

    The daily reset itself is prompt-side — the Reminders subagent does it against the store — so code
    cannot *perform* the reset. What code can do is notice that a row which has seeded every day for a
    fortnight did not seed today, which is exactly the shape a selective miss takes: the seed reports
    success in its Run Log and some rows never moved.

    Deliberately report-only, and deliberately a *comparison against this row's own history* rather
    than against a list of what should exist — a list would need the store, and a stale one would raise
    false alarms forever. ``min_days`` keeps a genuinely new or genuinely retired row from being
    reported as missing."""
    path = os.path.join(state_dir, SEED_LOG_FILE)
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(obj, dict) and obj.get("day") and obj.get("reminder_id"):
                    rows.append(obj)
    except OSError:
        return {"available": False, "today": today, "missing": [], "seeded_today": 0}

    days = sorted({r["day"] for r in rows if r["day"] < today}, reverse=True)[:window_days]
    prior = set(days)
    seen_before: dict = {}
    seeded_today = set()
    for r in rows:
        if r["day"] == today:
            seeded_today.add(r["reminder_id"])
        elif r["day"] in prior:
            seen_before.setdefault(r["reminder_id"], set()).add(r["day"])

    missing = [{"reminder_id": rid, "slug": next((r.get("slug") for r in rows
                                                  if r["reminder_id"] == rid and r.get("slug")), ""),
                "seeded_on_days": len(ds)}
               for rid, ds in sorted(seen_before.items())
               if len(ds) >= min_days and rid not in seeded_today]
    return {"available": True, "today": today, "window_days": len(days),
            "seeded_today": len(seeded_today), "missing": missing}


def _warn_no_ladder(args, gap: bool) -> None:
    """One ``!`` line on stderr for an important row seeded without the ladder — same shape as
    ``--audit-day``'s, so the Reminders subagent's "put any ``!`` lines in the Run Log verbatim" rule
    already covers it. Never fatal, never non-zero: the row seeded correctly."""
    if gap:
        print(f"  ! {args.slug or args.reminder_id} is {args.importance} but has no `Nag Until Done` — "
              "seeded at its primary time(s) only, NO 90-minute re-fire ladder (the ladder is "
              "`Nag Until Done` alone). Tick the box on the ⏰ row if it should chase.", file=sys.stderr)


def _warn_premise_review(question: str | None) -> None:
    """One ``!`` line on stderr when ``check_premise_review`` finds this row due for a "is this still
    a thing?" question (``../docs/reminder-premise-spec.md``) — same convention as ``_warn_no_ladder``,
    the seed's existing channel for "worth a line in the Run Log, not worth a gate." Never fatal."""
    if question:
        print(f"  ! {question}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(description="Seed one ⏰ row's exact-time nudges for the whole local day.")
    p.add_argument("--audit-day", action="store_true",
                   help="report rows that seeded on most recent days but NOT today, then exit "
                        "(the silent-skip detector; report-only, always exit 0)")
    p.add_argument("--window-days", type=int, default=14)
    p.add_argument("--min-days", type=int, default=3)
    p.add_argument("--reminder-id", default=None, help="the ⏰ Reminders row page id (carried so an ack "
                   "can dequeue/gate the day's nudges via reminders_dequeue.py / the fire-time ack gate)")
    p.add_argument("--text", default=None, help="the nudge text (shown after '⏰ Reminder: ')")
    p.add_argument("--slug", default=None, help="short stable+unique token for the entry ids "
                   "(rmd-<date>-<slug>-<HHMM>); default derives from reminder-id/text")
    p.add_argument("--times", default=None, help="comma-list of HH:MM local fire times, e.g. '08:00, 20:00'")
    p.add_argument("--time-window", default=None, choices=list(WINDOW_DEFAULTS),
                   help="coarse fallback used only when --times is empty (maps to a default time)")
    p.add_argument("--due-target", default=None, help="One-off only: the row's Due / Target, "
                   "YYYY-MM-DD. When this fire lands on a different local day, ' — due <Weekday "
                   "MM-DD>.' is appended to --text so an early/overdue surface reads as a heads-up, "
                   "never as do-it-now (see due_suffix)")
    p.add_argument("--importance", default=None, help="⏰ importance — a canonical key (super-critical "
                   "/ critical / high / notable / low) or the backend's label for one. Drives drain "
                   "order and the no-ladder notice; it does NOT drive the re-fire ladder (that is --nag "
                   "alone), but pass it anyway so a High-and-above row seeding without --nag says so")
    p.add_argument("--consecutive-misses", type=int, default=None, help="⏰ Consecutive Misses value, "
                   "read fresh off the store like --importance. Feeds the premise-review question "
                   "(../docs/reminder-premise-spec.md) — omit it and this is a no-op")
    p.add_argument("--nag", action="store_true", help="Nag Until Done — THE re-fire ladder predicate: "
                   "every 90 min through end of local day, whatever the importance")
    p.add_argument("--channel", default="telegram", choices=["telegram", "call", "discord"])
    p.add_argument("--pierce-quiet", action="store_true",
                   help="fire through a quiet window (set for 🚨 Critical / 🛑 Super-Critical rows)")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="dir holding reminders.json")
    p.add_argument("--dry-run", action="store_true", help="print the entries; write nothing")
    args = p.parse_args()

    if args.audit_day:
        # Report-only, always exit 0. This is a detector, not a gate: making it fail the seed would
        # mean a false positive could stop a Critical nudge from being queued, which is worse than the
        # silence it exists to break.
        rep = audit_day(args.state_dir, _local_now().date().isoformat(),
                        window_days=args.window_days, min_days=args.min_days)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        for m in rep.get("missing", []):
            print(f"  ! {m['slug'] or m['reminder_id']} seeded on {m['seeded_on_days']} recent "
                  f"days but NOT today", file=sys.stderr)
        return 0
    if not args.reminder_id or not args.text:
        p.error("--reminder-id and --text are required (except with --audit-day)")

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
        "due_target": date.fromisoformat(args.due_target) if args.due_target else None,
        "consecutive_misses": args.consecutive_misses,
    }
    now_local = _local_now()
    gap = ladder_gap(args.importance, args.nag)
    if args.dry_run:
        entries = seed_entries(row, now_local)
        # mark=False: a dry run must never stamp the review store (no write of any kind).
        try:
            premise_review = check_premise_review(args.state_dir, row, mark=False)
        except Exception:  # noqa: BLE001 — a dry-run preview must never itself fail
            premise_review = None
        print(json.dumps({"ok": True, "dry_run": True, "count": len(entries), "no_ladder": gap,
                          "premise_review_question": premise_review, "entries": entries}, indent=2))
        _warn_no_ladder(args, gap)
        _warn_premise_review(premise_review)
        return 0
    added = refill_seed(args.state_dir, row, now_local=now_local)
    premise_review = row.get("_premise_review_question")
    print(json.dumps({"ok": True, "reminder_id": args.reminder_id, "added": added, "no_ladder": gap,
                      "premise_review_question": premise_review}))
    _warn_no_ladder(args, gap)
    _warn_premise_review(premise_review)
    return 0


if __name__ == "__main__":
    sys.exit(main())
