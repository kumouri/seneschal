#!/usr/bin/env python3
"""Close a Dream proposal, notice when one was applied but never closed, and retire one that has
aged out.

**The gap this exists to shut.** `seneschal/references/proposed-learnings.md` is the held queue:
Dream raises a proposal, the owner rules on it, someone applies the fix. Closing the row is otherwise
a thing a person has to *remember* — and it only survives when whoever applied the fix happens to
already be editing that file. From a chat turn, "apply the fix" and "edit a tracked markdown file"
are two unrelated actions with nothing linking them.

So the row stays `[ ]`, Dream re-raises it the next night, and the next — weeks of a nightly
recommendation to do work that was already done. To the owner it looks like the fixes aren't
happening. They are; the ledger never says so.

**A second gap: closing a row never shrinks the file.** Ticking `[ ]` to `[x]` and appending a note
records neither *when* the row closed nor *what happened* (applied vs. declined), and nothing moves a
closed row anywhere — so the file Dream reads and writes nightly only ever grows. This module has
four parts:

* ``close`` makes applying-and-closing **one command**, and stamps a structured **disposition**
  (`applied`/`declined` — the file's own vocabulary, nothing invented) and a **closed-at** date on the
  row, because a later retirement pass cannot tell "fixed" from "turned down" or "how old" by parsing
  English.
* ``restamp`` backfills that same structured marker onto a row that was closed **before** this stamp
  existed — a one-time migration primitive, not an everyday command; it refuses on anything it cannot
  say with certainty (an open row, an already-stamped row, an ambiguous same-date collision).
* ``retire`` moves a closed, **stamped**, sufficiently-aged row out of `proposed-learnings.md` and into
  `proposed-learnings-archive.md` — a sibling file, never loaded at grounding, so Dream's nightly
  read/write stops paying for a decision made months ago. **Move, never delete**, matching this
  repo's convention everywhere a record's usefulness fades (salience's soft-prune, for one). Dry-run
  by default; only a row carrying the structured stamp is ever touched — an unstamped legacy row is
  left exactly alone rather than guessed at. **Declined rows are never auto-retired**
  (`--include-declined` to override): the file's own flow keeps a decline around specifically so the
  same thing isn't re-proposed, and the archive is deliberately outside what Dream re-reads before
  writing a new proposal — retiring a declined row by default would quietly reopen the door it exists
  to keep shut.
* ``audit`` greps each open item's ``Applies to:`` target for the change it asks for, so the gap is
  **visible within a day** when the CLI isn't used.

The audit is a **heuristic and says so**. It cannot know a fix landed; it can only notice that a file
a proposal names has changed since the proposal was raised, which is a reason to *look*, not a verdict.
It reports, it never edits — an auto-closer working off a guess would replace a visible backlog with
an invisible one, which is strictly worse than what it is fixing. ``retire`` is not that kind of guess:
it moves a row only once a human (via `close --declined`/`close`, or a one-time `restamp`) has already
recorded the disposition in writing, and only once an explicit age floor has cleared.

CLI:
    python learnings.py close 2026-07-17 --note "applied in <commit>"
    python learnings.py close 2026-07-18 --declined --note "no clean fix path; see row"
    python learnings.py restamp 2026-07-14 --closed-at 2026-07-15 --disposition applied
    python learnings.py retire                 # dry run — what WOULD move
    python learnings.py retire --apply          # actually move it
    python learnings.py audit    # open items whose Applies-to file has since changed
    python learnings.py list
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import clock  # noqa: E402 — owner-local "today", the test seam every date-dependent module here uses
import memory_write as mw  # noqa: E402 — atomic write; this file is tracked, but truncating it on a
                           # UnicodeEncodeError would still lose the queue between commits.

DEFAULT_PATH = os.path.normpath(
    os.path.join(SCRIPT_DIR, "..", "references", "proposed-learnings.md"))
DEFAULT_ARCHIVE_PATH = os.path.normpath(
    os.path.join(SCRIPT_DIR, "..", "references", "proposed-learnings-archive.md"))

_ARCHIVE_HEADER = """# Retired learnings (the archive)

Closed rows retired out of `proposed-learnings.md` by `learnings.py retire`, once their disposition
and age (30+ days since close, by default) both clear the bar. Nothing here is deleted — a row is
*moved*, so a later "did we already try this" question stays answerable without growing the file
Dream reads and writes every night (`../scripts/learnings.py`). This file is never loaded at
grounding; read it by hand or grep it when the question comes up.

Ordered as retired, oldest retirement batch first. Each entry keeps its full original text verbatim,
preceded by a one-line marker recording which section it was retired from and when.
"""

# A pending item. The date is the key: Dream raises at most one proposal per subject per day, and the
# title drifts as recurrences are appended, so matching on the title would rot.
_ITEM_RE = re.compile(r"^- \[( |x)\] (\d{4}-\d{2}-\d{2}) — (.+)$", re.M)

#: The structured marker `close()`/`restamp()` write — everything `retire()` is willing to trust for
#: an age and a disposition. Deliberately narrow: a free-text "CLOSED 2026-09-03 — the owner ruled..."
#: written by hand before this existed does NOT match, and `retire()` must leave that row alone rather
#: than guess at a date buried in prose.
_CLOSE_STAMP_RE = re.compile(r"CLOSED (\d{4}-\d{2}-\d{2}) \((applied|declined)\)")

#: Only these three headings are ever searched for a retirable row. Everything above `## Pending`
#: (the format template, the worked example) is `- [ ]` and untouched on principle — `retire()` never
#: even looks there — and everything is scoped per-section so a row is removed from the section it is
#: actually sitting in, never the wrong one.
_SECTION_RE = re.compile(r"^## (Pending|Applied|Declined)[ \t]*$", re.M)

_DISPOSITIONS = ("applied", "declined")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _pending_section(text: str) -> str:
    """Everything from `## Pending` to end of file — deliberately not scoped tighter than that.

    `close`/`restamp`/`list`/`audit` all need to find a row **by date** regardless of which of the
    three lifecycle headings (`## Pending`, `## Applied`, `## Declined`) it currently sits under —
    rows hand-filed under `## Applied` are normal, and a date lookup that only saw `## Pending` would
    silently fail to find them. Above `## Pending` is
    only the format template and the worked example, both of which are `- [ ]` lines and neither of
    which is a real row — counting those was how a naive grep once reported three open proposals when
    there was one. `retire()` alone needs to know *which* section a row is in (so it can remove it
    from the right place); it uses `_section_bounds` instead."""
    parts = text.split("## Pending", 1)
    return parts[1] if len(parts) == 2 else ""


def _section_bounds(text: str) -> dict[str, tuple[int, int]]:
    """`{heading: (start, end)}` for `Pending`/`Applied`/`Declined` — each body runs from just after
    its `## Heading` line to the start of the next tracked heading, or end of file."""
    matches = list(_SECTION_RE.finditer(text))
    bounds: dict[str, tuple[int, int]] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        bounds[m.group(1)] = (start, end)
    return bounds


def _item_blocks(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """`[(block_start, block_end), ...]` for every top-level item in `text[start:end]`.

    Each block runs to the **next item's start** (or `end`), so it carries its own trailing blank
    line and nothing about the surrounding formatting shifts when a block is later removed."""
    starts = [m.start() for m in _ITEM_RE.finditer(text, start, end)]
    return [(s, starts[i + 1] if i + 1 < len(starts) else end) for i, s in enumerate(starts)]


def list_items(path: str = DEFAULT_PATH, include_done: bool = False) -> list:
    try:
        body = _pending_section(_read(path))
    except OSError:
        return []
    out = []
    for done, item_date, title in _ITEM_RE.findall(body):
        if done == "x" and not include_done:
            continue
        out.append({"date": item_date, "title": title.strip(), "done": done == "x"})
    return out


def close(date_str: str, note: str, path: str = DEFAULT_PATH, disposition: str = "applied",
          now: str | None = None) -> dict:
    """Tick the row dated `date_str`, stamp it `CLOSED <today> (<disposition>)`, and append `note`.

    Refuses on an unknown date and on an already-closed one rather than doing nothing quietly: a
    close that silently no-ops is the same failure class as the reset that silently no-opped, and the
    caller — often a script applying a fix — needs to know its bookkeeping didn't land.

    `disposition` must be `"applied"` or `"declined"` — the file's own two-way vocabulary
    ("## The flow"), nothing invented. The closed-at date defaults to the owner-local activity day
    (`clock.local_today`); `now` is the test seam (an ISO `YYYY-MM-DD` string), matching every other
    date-dependent module in this tree."""
    if disposition not in _DISPOSITIONS:
        return {"ok": False, "reason": f"disposition must be one of {_DISPOSITIONS}, got {disposition!r}"}
    text = _read(path)
    body = _pending_section(text)
    if not body:
        return {"ok": False, "reason": "no '## Pending' section in the file"}

    matches = [m for m in _ITEM_RE.finditer(body) if m.group(2) == date_str]
    if not matches:
        return {"ok": False, "reason": f"no proposal dated {date_str}"}
    if len(matches) > 1:
        return {"ok": False, "reason": f"{len(matches)} proposals dated {date_str} — close by hand"}
    m = matches[0]
    if m.group(1) == "x":
        return {"ok": False, "reason": f"the {date_str} proposal is already closed"}

    old_line = m.group(0)
    if text.count(old_line) != 1:
        return {"ok": False,
                "reason": "the matched row's title line is not unique in the file — refusing to "
                          "guess which occurrence to edit"}

    closed_at = now or clock.local_today().isoformat()
    new_line = f"- [x] {date_str} — {m.group(3).strip()} — CLOSED {closed_at} ({disposition})"
    if note:
        new_line += f": {note.strip()}"
    mw.write_text(path, text.replace(old_line, new_line, 1))

    after = _read(path)
    if new_line not in after or old_line in after:
        return {"ok": False,
                "reason": "post-write verification failed — the file may be inconsistent; check it "
                          "by hand before trusting it"}
    return {"ok": True, "date": date_str, "title": m.group(3).strip()[:70],
            "closed_at": closed_at, "disposition": disposition}


def restamp(date_str: str, closed_at: str, disposition: str, path: str = DEFAULT_PATH,
            match: str | None = None) -> dict:
    """Backfill the structured `CLOSED <closed_at> (<disposition>)` marker onto a row that was
    already closed before this stamp existed.

    A one-time migration primitive, not an everyday command: `close()` is what closes a row and
    always stamps it going forward, so `restamp` exists only to give a *historical* row the same
    structure `retire()` requires, without touching anything else about it (the title, the body, and
    every "Recurrence" paragraph are untouched — this only appends to the title line).

    Refuses rather than guesses on every ambiguity: an unknown date, a same-date collision (narrow it
    with `match`, a substring of the title), a row that is still **open** (restamp records history
    for a decision already made — it is not a second `close`), and a row that already carries the
    stamp (no double-stamping)."""
    if disposition not in _DISPOSITIONS:
        return {"ok": False, "reason": f"disposition must be one of {_DISPOSITIONS}, got {disposition!r}"}
    text = _read(path)
    body = _pending_section(text)
    if not body:
        return {"ok": False, "reason": "no '## Pending' section in the file"}

    matches = [m for m in _ITEM_RE.finditer(body) if m.group(2) == date_str]
    if match:
        matches = [m for m in matches if match in m.group(3)]
    if not matches:
        reason = f"no proposal dated {date_str}"
        if match:
            reason += f" matching {match!r}"
        return {"ok": False, "reason": reason}
    if len(matches) > 1:
        return {"ok": False,
                "reason": f"{len(matches)} proposals dated {date_str} match — narrow with --match"}
    m = matches[0]
    if m.group(1) != "x":
        return {"ok": False,
                "reason": f"the {date_str} proposal is still open — restamp only records a "
                          f"disposition for a row already closed; use close() for that"}
    old_line = m.group(0)
    if _CLOSE_STAMP_RE.search(old_line):
        return {"ok": False, "reason": f"the {date_str} proposal is already stamped"}
    if text.count(old_line) != 1:
        return {"ok": False,
                "reason": "the matched row's title line is not unique in the file — refusing to "
                          "guess which occurrence to edit"}

    new_line = f"{old_line} — CLOSED {closed_at} ({disposition})"
    mw.write_text(path, text.replace(old_line, new_line, 1))

    after = _read(path)
    if after.count(new_line) != 1:
        return {"ok": False,
                "reason": "post-write verification failed — the file may be inconsistent; check it "
                          "by hand before trusting it"}
    return {"ok": True, "date": date_str, "title": m.group(3).strip()[:70],
            "closed_at": closed_at, "disposition": disposition}


def retire(path: str = DEFAULT_PATH, archive_path: str = DEFAULT_ARCHIVE_PATH,
           min_age_days: int = 30, apply: bool = False, now: str | None = None,
           include_declined: bool = False) -> dict:
    """Move every closed row stamped `CLOSED <date> (<disposition>)` at least `min_age_days` old out
    of `path` and into `archive_path` — dry-run unless `apply=True`.

    **Only the structured stamp is ever trusted.** An open row, or a historical closed row nobody has
    `restamp`-ed yet, is left exactly alone — this is a deliberate refusal to infer an age or a
    disposition from free-text prose. A file edited by hand over months is not uniform, and a wrong
    guess here would be a quieter version of the mistake `memory_write.py` exists to stop: silent
    damage to a file nobody is watching.

    **A declined row is never retired by default** (`include_declined=True` to override): the file's
    own flow keeps a decline around specifically so Dream doesn't re-propose the same thing, and the
    archive is deliberately outside what Dream re-reads before writing a new proposal.

    **Archive-then-shrink, in that order.** A crash between the two writes leaves a duplicate in the
    archive (recoverable by hand) rather than a row deleted from `path` and landed nowhere
    (unrecoverable short of `git log`)."""
    today = date.fromisoformat(now) if now else clock.local_today()
    try:
        text = _read(path)
    except OSError:
        return {"ok": False, "reason": f"no file at {path}"}

    bounds = _section_bounds(text)
    if not bounds:
        return {"ok": False,
                "reason": "no '## Pending' / '## Applied' / '## Declined' headings found"}

    plan = []
    for section, (s, e) in bounds.items():
        for b_start, b_end in _item_blocks(text, s, e):
            block = text[b_start:b_end]
            m = _ITEM_RE.match(block)
            if not m or m.group(1) != "x":
                continue  # never touch an open row
            stamp = _CLOSE_STAMP_RE.search(block)
            if not stamp:
                continue  # unstamped legacy row — leave it, never guess
            disposition = stamp.group(2)
            if disposition == "declined" and not include_declined:
                continue
            closed_at = date.fromisoformat(stamp.group(1))
            age_days = (today - closed_at).days
            if age_days < min_age_days:
                continue
            plan.append({
                "section": section, "start": b_start, "end": b_end,
                "date": m.group(2), "title": m.group(3).strip()[:70],
                "closed_at": stamp.group(1), "disposition": disposition, "age_days": age_days,
            })

    plan.sort(key=lambda p: p["start"])  # stable source order, regardless of dict iteration order

    if not plan or not apply:
        return {"ok": True, "apply": apply, "retired": plan, "path": path,
                "archive_path": archive_path}

    stamp_today = today.isoformat()
    archive_addition = "\n" + "".join(
        f"\n<!-- retired {stamp_today} from ## {p['section']} -->\n{text[p['start']:p['end']]}"
        for p in plan)
    if not os.path.exists(archive_path) or os.path.getsize(archive_path) == 0:
        mw.write_text(archive_path, _ARCHIVE_HEADER)
    mw.append_text(archive_path, archive_addition)

    removed_ranges = sorted((p["start"], p["end"]) for p in plan)
    parts, cursor = [], 0
    for s, e in removed_ranges:
        parts.append(text[cursor:s])
        cursor = e
    parts.append(text[cursor:])
    mw.write_text(path, "".join(parts))

    after_main = _read(path)
    after_archive = _read(archive_path)
    problems = []
    for p in plan:
        block_text = text[p["start"]:p["end"]]
        if block_text in after_main:
            problems.append(f"{p['date']} ({p['section']}) still present in {path}")
        if block_text not in after_archive:
            problems.append(f"{p['date']} ({p['section']}) missing from {archive_path}")
    if problems:
        return {"ok": False, "reason": "post-retire verification failed: " + "; ".join(problems),
                "retired": plan}
    return {"ok": True, "apply": True, "retired": plan, "path": path, "archive_path": archive_path}


def _touched_since(repo_root: str, rel: str, since: str) -> str:
    """The last commit touching `rel` on or after `since`, or "" — the audit's only real signal."""
    try:
        out = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "log", "-1", f"--since={since}",
             "--format=%h %ad %s", "--date=short", "--", rel],
            cwd=repo_root, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def audit(path: str = DEFAULT_PATH, repo_root: str | None = None) -> dict:
    """Open proposals whose named target has changed since they were raised.

    **A reason to look, not a verdict.** A commit touching the file could be the fix, or could be
    something else entirely — so this reports and never edits. The shape it is for: the named
    script changed two days after the proposal was raised, and nothing noticed for weeks."""
    repo_root = repo_root or os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
    try:
        text = _read(path)
    except OSError:
        return {"available": False, "suspects": [], "open": 0}
    body = _pending_section(text)

    suspects, n_open = [], 0
    for m in _ITEM_RE.finditer(body):
        if m.group(1) == "x":
            continue
        n_open += 1
        item_date, title = m.group(2), m.group(3).strip()
        chunk = body[m.end():m.end() + 4000]
        applies = re.search(r"Applies to:\s*(.+)", chunk)
        if not applies:
            continue
        for rel in re.findall(r"[\w./-]+\.(?:py|md|ps1|json)", applies.group(1)):
            rel = rel.strip("`.,;")
            if not os.path.exists(os.path.join(repo_root, rel)):
                continue
            hit = _touched_since(repo_root, rel, item_date)
            if hit:
                suspects.append({"date": item_date, "title": title[:70], "file": rel, "commit": hit[:90]})
    return {"available": True, "open": n_open, "suspects": suspects}


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("close", help="tick a proposal and record how it was applied")
    c.add_argument("date")
    c.add_argument("--note", default="", help="how it was applied (commit, PR, one line of why)")
    c.add_argument("--declined", action="store_true",
                   help="record this as a decline, not an applied fix")
    c.add_argument("--path", default=DEFAULT_PATH)
    c.add_argument("--now", default=None, help=argparse.SUPPRESS)  # test/backfill seam

    r = sub.add_parser("restamp",
                        help="backfill the structured CLOSED marker onto a row closed before it existed")
    r.add_argument("date")
    r.add_argument("--closed-at", required=True, help="YYYY-MM-DD the row was actually closed")
    r.add_argument("--disposition", choices=list(_DISPOSITIONS), default="applied")
    r.add_argument("--match", default=None,
                   help="substring of the title, to disambiguate a shared date")
    r.add_argument("--path", default=DEFAULT_PATH)

    t = sub.add_parser("retire",
                        help="move closed, aged-out rows into the archive file (dry-run unless --apply)")
    t.add_argument("--path", default=DEFAULT_PATH)
    t.add_argument("--archive-path", default=DEFAULT_ARCHIVE_PATH)
    t.add_argument("--min-age-days", type=int, default=30)
    t.add_argument("--apply", action="store_true")
    t.add_argument("--include-declined", action="store_true")
    t.add_argument("--now", default=None, help=argparse.SUPPRESS)  # test seam

    a = sub.add_parser("audit", help="open items whose named file changed since they were raised")
    a.add_argument("--path", default=DEFAULT_PATH)

    l = sub.add_parser("list", help="what is still open")
    l.add_argument("--path", default=DEFAULT_PATH)

    args = ap.parse_args(argv)

    if args.cmd == "close":
        disposition = "declined" if args.declined else "applied"
        res = close(args.date, args.note, args.path, disposition=disposition, now=args.now)
        if not res["ok"]:
            print(f"could not close {args.date}: {res['reason']}", file=sys.stderr)
            return 1
        print(f"closed {res['date']} ({res['disposition']}) — {res['title']}")
        return 0

    if args.cmd == "restamp":
        res = restamp(args.date, args.closed_at, args.disposition, args.path, match=args.match)
        if not res["ok"]:
            print(f"could not restamp {args.date}: {res['reason']}", file=sys.stderr)
            return 1
        print(f"stamped {res['date']} — CLOSED {res['closed_at']} ({res['disposition']})")
        return 0

    if args.cmd == "retire":
        res = retire(args.path, args.archive_path, args.min_age_days, args.apply, args.now,
                      include_declined=args.include_declined)
        if not res["ok"]:
            print(f"retire failed: {res['reason']}", file=sys.stderr)
            return 1
        verb = "retired" if args.apply else "would retire"
        suffix = "" if args.apply else " (dry run; pass --apply)"
        print(f"{verb} {len(res['retired'])} row(s){suffix}")
        for p in res["retired"]:
            print(f"  {p['date']}  ({p['section']}, {p['disposition']}, closed {p['closed_at']}, "
                  f"{p['age_days']}d old)  {p['title']}")
        return 0

    if args.cmd == "list":
        items = list_items(args.path)
        print(f"{len(items)} open")
        for it in items:
            print(f"  {it['date']}  {it['title'][:72]}")
        return 0

    rep = audit(args.path)
    if not rep["available"]:
        print("no proposals file")
        return 0
    print(f"{rep['open']} open; {len(rep['suspects'])} may already be applied")
    for s in rep["suspects"]:
        print(f"  ? {s['date']} {s['title']}")
        print(f"      {s['file']} changed: {s['commit']}")
    # Always 0 — a heuristic must not be able to fail a nightly run.
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
