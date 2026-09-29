#!/usr/bin/env python3
"""Ad-hoc, witness-only notes — one dated file per owner-local activity day. Stdlib only.

A witness-only note has no closing event, by design: ask *"can you name the event that would close
this?"* and the honest answer is no. Forcing it through a work-item register built around that
question is the wrong fix, and appending it to the bottom of `state/carry-over.md` is worse — those
ad-hoc dated asides accumulate there, never revisited and never pruned, and bloat the file every mode
reads at orientation. This module gives them their own home: one file per activity day,
`state/notes/YYYY-MM-DD.md`, appended never rebuilt, pruned by age — the same shape
`mini_dream.py --prune-days 30` already uses for `session-distillations.jsonl`.

## What belongs here, and what doesn't

Ask the closing-event question first. Yes → it is a work item (a carry-over open loop, a reminder),
not this. No, by design — an emotional-hold note, a "do NOT problem-solve, do NOT raise cold" entry,
a status aside meant to be found later by grep or recall rather than re-surfaced every night —
belongs here instead.

## Day boundary

The owner's timezone, cut at the owner's day boundary (`owner.dayBoundaryHour`, default 05:00) —
`activity_day.today()`, the same after-midnight rule every other writer in this tree uses. A note
written at 01:30 local belongs to the PRIOR day's file, not a fresh one two hours after the day
started on the calendar. Bullet times are the owner's local wall clock.

## Writes

`add` builds the whole bullet — and the file's one-line header, the first time — as one string and
hands it to `memory_write.append_text`. **Never `open(path, "w")`** — the truncating-write failure
`memory_write.py` exists to prevent. Appending to an absent file creates it, so there is no separate
"write the header, then append the bullet" step for a crash to land between.

## Retention

`prune --days N` (default 90) deletes a dated file whose OWN FILENAME is older than the cutoff.
Ninety is not a measured optimum — these notes are hand-triggered and sparse, not a per-session log
like `mini_dream.py`'s 30-day one, and the whole point of the store is that a note is still findable
next season — but it is tunable per-call with no code change.

CLI:
    python notes.py add "<text>" [--source chat|watch|job] [--date YYYY-MM-DD]
    python notes.py list [--days N]
    python notes.py prune [--days N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import activity_day  # noqa: E402 — the one after-midnight day definition, never re-derived here
import memory_write  # noqa: E402

DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
NOTES_SUBDIR = "notes"

#: The `--source` vocabulary the brief names. Not open-ended: an unrecognized source is a typo, not a
#: new kind of caller, and refusing it here is cheaper than a silently-uncategorized note.
SOURCE_CHOICES = ("chat", "watch", "job")

#: See the module docstring's "Retention" section for why 90 and not 30.
DEFAULT_PRUNE_DAYS = 90

#: The default read window — today's + yesterday's notes, read-only (what an end-of-day Wrap wants
#: in front of it).
DEFAULT_LIST_DAYS = 2

_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


class NotesError(Exception):
    """A refusal. Carries a real sentence the CLI prints verbatim."""


def _dir(state_dir: str | None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, NOTES_SUBDIR)


def note_path(day: date, state_dir: str | None = None) -> str:
    return os.path.join(_dir(state_dir), f"{day.isoformat()}.md")


def _resolve_day(date_str: str | None, now) -> date:
    if date_str:
        try:
            return date.fromisoformat(date_str)
        except ValueError:
            raise NotesError(f"--date {date_str!r} is not YYYY-MM-DD.")
    return activity_day.today(now)


def _header(day: date) -> str:
    return f"# Notes — {day.isoformat()}\n\n"


def _bullet(text: str, source: str | None, now) -> str:
    _, _, local = activity_day.owner_now(now)
    stamp = local.strftime("%I:%M %p").lstrip("0")
    suffix = f"  _(via {source})_" if source else ""
    return f"- **{stamp}** — {text}{suffix}\n"


def add(text: str, *, state_dir: str | None = None, source: str | None = None,
       date_str: str | None = None, now=None) -> dict:
    """Append one dated bullet. Refuses an empty/whitespace-only `text` — a note nobody wrote
    anything into is a caller bug, not a real observation — and an unrecognized `source`. Creates
    `state/notes/<day>.md` with a one-line header on the first write for that day; every write after
    that is a pure append, never a rebuild."""
    text = (text or "").strip()
    if not text:
        raise NotesError("REFUSED to log an empty note. Say what happened.")
    if source is not None and source not in SOURCE_CHOICES:
        raise NotesError(f"--source must be one of {', '.join(SOURCE_CHOICES)}, not {source!r}.")
    day = _resolve_day(date_str, now)
    path = note_path(day, state_dir)
    existed = os.path.exists(path)
    bullet = _bullet(text, source, now)
    payload = bullet if existed else _header(day) + bullet
    # `memory_write.append_text` takes a sidecar lock BEFORE it makes the parent directory, so the
    # first note of this checkout's life needs `state/notes/` to already exist for the LOCK file to
    # land in — unlike `carry-over.md`'s flat `state/`, this subdirectory is new.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    memory_write.append_text(path, payload)
    return {"path": path, "date": day.isoformat(), "created": not existed}


def list_notes(state_dir: str | None = None, *, days: int = DEFAULT_LIST_DAYS, now=None) -> list:
    """`days` activity days ending on today, OLDEST FIRST. Each entry is `{"date", "path", "exists",
    "text"}` — a day with no file is REPORTED, not skipped, so a caller can tell "nothing happened"
    from "didn't look"."""
    if days < 1:
        raise NotesError("--days must be at least 1.")
    today = activity_day.today(now)
    out = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        path = note_path(day, state_dir)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            out.append({"date": day.isoformat(), "path": path, "exists": True, "text": text})
        else:
            out.append({"date": day.isoformat(), "path": path, "exists": False, "text": None})
    return out


def render_list(entries: list) -> str:
    parts = []
    for e in entries:
        if e["exists"]:
            parts.append(e["text"].rstrip("\n"))
        else:
            parts.append(f"# Notes — {e['date']}\n\n(none)")
    return "\n\n".join(parts)


def prune(state_dir: str | None = None, *, days: int = DEFAULT_PRUNE_DAYS, now=None,
         dry_run: bool = False) -> dict:
    """Delete a dated notes file whose OWN FILENAME date is more than `days` days before today's
    activity day. Matches only the `YYYY-MM-DD.md` shape `add` writes — a stray file in the same
    directory (a README, a hand-made rescue copy) is never touched, the same discipline
    `state_backup.py`'s rotation holds for its own `.backup-*` suffix."""
    directory = _dir(state_dir)
    today = activity_day.today(now)
    cutoff = today - timedelta(days=days)
    removed, kept, errors = [], [], []
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    for name in sorted(names):
        m = _DAY_RE.match(name)
        if not m:
            continue
        try:
            day = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if day < cutoff:
            if dry_run:
                removed.append(name)
                continue
            try:
                os.remove(os.path.join(directory, name))
                removed.append(name)
            except OSError as exc:
                errors.append(f"{name}: {exc}")
        else:
            kept.append(name)
    return {"removed": removed, "kept": len(kept), "errors": errors, "cutoff": cutoff.isoformat()}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Ad-hoc dated notes — witness-only content with no closing event, kept out "
                    "of carry-over on purpose.")
    p.add_argument("--state-dir", default=None, help="override seneschal/state/ (tests MUST pass this)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="append one dated bullet")
    a.add_argument("text")
    a.add_argument("--source", choices=SOURCE_CHOICES, default=None)
    a.add_argument("--date", dest="date_str", default=None,
                   help="YYYY-MM-DD; default is today's owner-local activity day")

    ls = sub.add_parser("list", help="read-only: the last N activity days' notes")
    ls.add_argument("--days", type=int, default=DEFAULT_LIST_DAYS)
    ls.add_argument("--json", action="store_true")

    pr = sub.add_parser("prune", help="delete dated files older than N days")
    pr.add_argument("--days", type=int, default=DEFAULT_PRUNE_DAYS)
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    sd = args.state_dir

    try:
        if args.cmd == "add":
            result = add(args.text, state_dir=sd, source=args.source, date_str=args.date_str)
            print(json.dumps({"ok": True, **result}, ensure_ascii=False))
            return 0
        if args.cmd == "list":
            entries = list_notes(sd, days=args.days)
            if args.json:
                print(json.dumps(entries, ensure_ascii=False, indent=2))
            else:
                print(render_list(entries))
            return 0
        result = prune(sd, days=args.days, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            verb = "would remove" if args.dry_run else "removed"
            print(f"notes prune: {verb} {len(result['removed'])}, kept {result['kept']} "
                 f"(cutoff {result['cutoff']})")
            for name in result["removed"]:
                print(f"  {name}")
            for err in result["errors"]:
                print(f"  ! {err}", file=sys.stderr)
        return 0
    except NotesError as exc:
        print(f"notes: refused — {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "refused": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
