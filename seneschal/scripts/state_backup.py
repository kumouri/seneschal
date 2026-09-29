#!/usr/bin/env python3
"""Nightly rotating backups for the `state/` files nothing else can rebuild. Stdlib only.

## Why this exists

`state/` is gitignored, so for most of this directory the standing answer to "what if it's lost?" is
"it's gone". `memory_write.py` and `stateio.py` stop the common way a file gets destroyed (a
truncating rewrite that fails halfway); this module is the second line of defence, so the next total
loss — by whatever route — costs a day rather than everything.

**A habit is not coverage.** Hand-made copies ("backup saved" on the nights someone remembers) leave
one irreplaceable file protected and its neighbour thirty days stale, with nothing having decided
that. So this runs as a Dream step (`dream_steps.py` step `2g`), and a night it does not run shows up
as a stale ledger row instead of silence. The file naming (`<stem>.backup-YYYYMMDD-HHMM<ext>`) is the
natural hand-made shape, so any copies already on disk simply join the rotation.

## What gets rotated, and the rule that decides it

**A file is rotated iff it is (a) not cheaply derivable from the store, git, or a re-run, AND (b)
rewritten WHOLESALE rather than appended to.**

Half (b) is the part worth arguing. The failure this answers is a *truncating rewrite* — `open(w)`
that empties the target and then fails. That cannot happen to an append-only ledger (`metrics.jsonl`,
`session-distillations.jsonl`, `governor-ledger.jsonl`, ...): those are opened `"a"`, and their worst
case is one torn line at the tail. Copying megabytes of append-only history every night to guard
against a failure mode its writer does not have would buy nothing and would make the rotation too
expensive to keep.

Known gaps, named rather than assumed — none of these is covered here and each has a reason:

* **`health.db`** — can be large, and mostly re-imports from its source export. A nightly byte copy
  is the wrong tool; a sqlite `.backup` of any hand-entered rows on a weekly cadence would be the
  right one, as a separate change with its own size argument.
* **Append-only ledgers** (`*.jsonl`) — irreplaceable but never rewritten wholesale; see above.
* **`presence.db`, `rag-index.sqlite`, `projects.jsonl`, `health-dashboard.html`** — regenerable
  caches, and `state/README.md` says so for each.

## What it will not do

* **It never opens the source for writing, and never decodes it.** A backup is a byte copy
  (`shutil.copyfile` to a sibling temp, then `os.replace`), so line endings, a BOM and any encoding
  survive exactly. A "backup" that re-encoded its input would be a second copy of the bug it guards.
* **It never backs up a backup** (`*.backup-*` is skipped), and never deletes anything that is not a
  backup it made — the prune matches the full `<stem>.backup-YYYYMMDD-HHMM<ext>` shape and nothing else.
* **One file's failure never stops the rest.** Each is independent and errors are collected, because
  a sweep that aborts on the first unreadable file protects the alphabetically-early half of a list.

Usage:
  python state_backup.py                       # rotate, default state dir, keep 7
  python state_backup.py --keep 14 --json
  python state_backup.py --dry-run             # say what it would do, touch nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

#: How many copies of each file to keep. Seven, and the reasoning is a latency argument rather than a
#: storage one: the whole set is small, so depth is nearly free, but every copy past the point where
#: somebody would have NOTICED a loss buys correlation and not coverage. A lost memory file is
#: usually noticed within one Dream cycle; a week is comfortably past that and still short enough
#: that the directory stays readable by eye. Raise it with --keep if a loss is ever found late; that
#: is the evidence that would change the number.
DEFAULT_KEEP = 7

#: The rotation set. See the module docstring for the rule that admits a file. A file missing from
#: disk is skipped silently — not every install has every one of these.
FILES = (
    "carry-over.md",           # the open-loops log; hand- and mode-written, rewritten whole
    "run-log.md",              # the store is the record, but re-deriving the local mirror is hours
    "context-digest.md",       # RETIRED (nothing writes it); kept while an upgraded install still has
                               # one for `standing_safety.py import-digest` to migrate from
    "standing-safety.json",    # the owner's hand-curated READ FIRST items — one home, no other copy
    "open-loops.json",         # the work-item register; loops.py rewrites it whole on every verb
    "reminders.json",          # wholesale-rewritten by every reconcile
    "reminders-id-cache.md",   # ⏰ page ids; rebuilding means re-querying the store row by row
    "acks.json",               # which nudges were already acked today — losing it re-fires them
    "project-roots.json",      # hand-edited config, no seed beyond the example
    "archive-people.json",     # hand-edited person registry for the message archiver
    "model-config.json",       # the model dials; rewritten whole by the cockpit
    "governor-config.json",    # hand-tuned budgets and quotas
)

#: `<stem>.backup-YYYYMMDD-HHMM<ext>` — the shape already on disk, kept byte-compatible on purpose so
#: the copies made by hand before this existed are part of the rotation rather than orphans beside it.
_STAMP = "%Y%m%d-%H%M"


def backup_name(filename: str, when: datetime) -> str:
    stem, ext = os.path.splitext(filename)
    return f"{stem}.backup-{when.strftime(_STAMP)}{ext}"


def _backup_re(filename: str) -> re.Pattern:
    stem, ext = os.path.splitext(filename)
    return re.compile(rf"^{re.escape(stem)}\.backup-\d{{8}}-\d{{4}}{re.escape(ext)}$")


def existing_backups(state_dir: str, filename: str) -> list[str]:
    """Newest first. Sorted by the NAME's stamp, not by mtime: a copy is what its filename says it
    is, and a `git checkout` or a file-manager touch must not reorder the rotation."""
    pat = _backup_re(filename)
    try:
        names = [n for n in os.listdir(state_dir) if pat.match(n)]
    except OSError:
        return []
    return sorted(names, reverse=True)


def _digest(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def rotate_one(state_dir: str, filename: str, *, keep: int = DEFAULT_KEEP,
               now: datetime | None = None, dry_run: bool = False) -> dict:
    """Back up one file and prune its old copies. Returns a row describing what happened.

    Never raises for an ordinary problem — a missing source, an unreadable file, a failed unlink are
    all reported in the row. The caller is a nightly sweep, and a sweep that throws stops sweeping."""
    now = now or datetime.now()
    src = os.path.join(state_dir, filename)
    row: dict = {"file": filename, "action": None, "kept": 0, "pruned": [], "error": None}

    if not os.path.isfile(src):
        row["action"] = "absent"
        return row

    prior = existing_backups(state_dir, filename)
    # Unchanged since the newest copy? Then a second identical copy costs a rotation slot and buys
    # nothing -- it would push a DIFFERENT day's content off the end of the window.
    if prior:
        newest = os.path.join(state_dir, prior[0])
        if os.path.getsize(newest) == os.path.getsize(src) and _digest(newest) == _digest(src):
            row["action"] = "unchanged"
            row["kept"] = len(prior)
            return row

    dest = os.path.join(state_dir, backup_name(filename, now))
    if os.path.abspath(dest) == os.path.abspath(src):        # paranoia: never overwrite the source
        row["error"] = "backup name collided with the source"
        return row

    if dry_run:
        row["action"] = "would-copy"
        row["dest"] = os.path.basename(dest)
    else:
        tmp = dest + ".part"
        try:
            # A BYTE copy, and `copyfile` never opens the SOURCE for writing. Nothing here decodes
            # the file, so CRLF/LF, a BOM and any encoding come through untouched.
            shutil.copyfile(src, tmp)
            os.replace(tmp, dest)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            row["error"] = f"{type(exc).__name__}: {exc}"
            return row
        row["action"] = "copied"
        row["dest"] = os.path.basename(dest)

    survivors = existing_backups(state_dir, filename)
    if not dry_run and os.path.basename(dest) not in survivors:   # pragma: no cover - listdir lag
        survivors = sorted(survivors + [os.path.basename(dest)], reverse=True)
    for stale in survivors[max(keep, 1):]:
        if dry_run:
            row["pruned"].append(stale)
            continue
        try:
            os.unlink(os.path.join(state_dir, stale))
            row["pruned"].append(stale)
        except OSError as exc:
            row["error"] = f"prune {stale}: {exc}"
    row["kept"] = min(len(survivors), max(keep, 1))
    return row


def rotate(state_dir: str = DEFAULT_STATE_DIR, *, keep: int = DEFAULT_KEEP,
           now: datetime | None = None, dry_run: bool = False,
           files: tuple = FILES) -> list[dict]:
    now = now or datetime.now()
    return [rotate_one(state_dir, f, keep=keep, now=now, dry_run=dry_run) for f in files]


def summary_line(rows: list[dict]) -> str:
    copied = sum(1 for r in rows if r["action"] in ("copied", "would-copy"))
    unchanged = sum(1 for r in rows if r["action"] == "unchanged")
    absent = sum(1 for r in rows if r["action"] == "absent")
    pruned = sum(len(r["pruned"]) for r in rows)
    errors = [r for r in rows if r["error"]]
    line = (f"state backup: {copied} copied · {unchanged} unchanged · {absent} absent "
            f"· {pruned} pruned")
    if errors:
        line += f" · {len(errors)} ERROR" + ("S" if len(errors) > 1 else "")
    return line


def _stamp_dream_step(state_dir: str, step: str = "2g") -> None:
    """Record that Dream's backup step actually ran (`dream_steps.py`).

    Lazy import, swallowed whole — the same contract every `dream_steps` owner uses, for
    the same reason: bookkeeping that cannot import must never stop the work it was measuring. This
    is what makes a *missed* night visible instead of silent, which is the whole difference between
    this and the hand-made copies it replaces."""
    try:
        import dream_steps
        dream_steps.record(state_dir, step)
    except Exception:  # noqa: BLE001 — see the docstring
        pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Rotate backups of the irreplaceable state/ files.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    p.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"copies per file (default {DEFAULT_KEEP})")
    p.add_argument("--dry-run", action="store_true", help="report only; touch nothing")
    p.add_argument("--json", action="store_true", help="machine-readable rows on stdout")
    a = p.parse_args(argv)

    rows = rotate(a.state_dir, keep=a.keep, dry_run=a.dry_run)
    if not a.dry_run:
        # Stamp even when a file errored: the step DID run, and `dream_steps` measures whether the
        # sweep happened, not whether every file in it was readable. Conflating the two would let one
        # locked file mark the whole night as never-attempted.
        _stamp_dream_step(a.state_dir)
    if a.json:
        print(json.dumps({"rows": rows, "summary": summary_line(rows)}, ensure_ascii=False))
    else:
        print(summary_line(rows))
        for r in rows:
            if r["error"]:
                print(f"  ! {r['file']}: {r['error']}", file=sys.stderr)
            elif r["action"] in ("copied", "would-copy"):
                extra = f" (pruned {len(r['pruned'])})" if r["pruned"] else ""
                print(f"  {r['file']} → {r['dest']}{extra}")
    # A failure to back up is never a failure of the run that called us: Dream must not abort its
    # remaining steps because one file was locked. Report loudly, exit 0.
    return 0


if __name__ == "__main__":
    sys.exit(main())
