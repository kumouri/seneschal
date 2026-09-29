#!/usr/bin/env python3
"""The `InstructionsLoaded` logger — grounding-restructure-spec.md §6.2, phase 0. Stdlib only.

## What this is for

The restructure's second failure mechanism (§6.1) is **a sub-router that never loads**: the turn never
touches that directory, so the fact it holds is simply absent from context. Nothing goes red. The
assistant is quietly wrong, indistinguishable from an ordinary model miss — the same shape as a data
feed that silently reads zero and is taken for a true zero.

Claude Code fires an `InstructionsLoaded` hook when a `CLAUDE.md` or `.claude/rules/*.md` file enters
context, carrying `file_path` and `load_reason` ∈ {`session_start`, `nested_traversal`,
`path_glob_match`, `include`, `compact`}. **That turns mechanism 2 from invisible into logged, in code,
at zero model cost.** After a week of rows, questions that are unanswerable today become arithmetic:

- which sub-routers **never** load (candidates for folding back into the root);
- what fraction of turns load any sub-router at all — §3.3's hole, measured rather than argued;
- whether `load_reason: "include"` ever appears, which means an `@path` import crept in (§3.2's trap).

A prompt-side-only telemetry contract is the warning here: one that asks the model to log produces
no rows, and has to be re-landed in code. This is the artifact every later step reads, landed first,
where it cannot break anything.

## The split, and why this file is tracked while its registration is not

Hooks are registered in the user's `~/.claude/settings.json`, which is **outside version control** and
absolute-pathed to the live checkout — the same constraint `/assistant` and `session_stamp.py` live
under. **No PR can install this hook, and the tree cannot tell whether it is live**: a PR can review and
test the logger; it cannot make the hook fire. So the *registration* is a host-side, opt-in step; the
**logger itself is ordinary tracked code**, and belongs here where it can be read, tested and reviewed.
`settings_merge.py --guard instructions-loaded` writes this block (dry-run first, append-only; it is
observability, not a guard, so `--guard all` does not include it):

    {"hooks": {"InstructionsLoaded": [{"hooks": [{"type": "command",
       "command": "python <repo>/seneschal/scripts/instructions_loaded.py",
       "timeout": 10}]}]}}

## Two deliberate limits, stated rather than discovered

1. **It records LOADING, not USING.** A sub-router that loads and is then ignored looks identical here
   to one that loads and works. §6.2 says so; this file does not pretend otherwise.
2. **It only logs files under this repo's own root.** The hook is registered globally — it fires for
   every Claude Code session on the machine — but a row about some unrelated project's `CLAUDE.md` is
   noise in the one log that exists to answer questions about *this* tree's routing. Everything else
   is dropped silently. `--all-roots` opts out of the filter.

## The one invariant

**This must never cost a session.** It is on the critical path of loading instructions, so every
failure — unparseable stdin, a full disk, a squatted path, an exotic payload — exits 0 having written
nothing. It never raises, never blocks, and never prints to stdout (a hook's stdout can reach the
model). Same contract as `mouth.record_assertion`, for the same reason.

Writes `state/instructions-loaded.jsonl` (append-only; one row per load).

USAGE (normally invoked by the hook, with the payload on stdin):
  python instructions_loaded.py                # read one hook payload from stdin, append a row
  python instructions_loaded.py tail --limit 20
  python instructions_loaded.py report         # what loads, what never loads, and `include` sightings
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

SCHEMA = "seneschal.instructions-loaded/1"
LOG_FILE = "instructions-loaded.jsonl"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
# The tree this log is about: `seneschal/scripts/..` -> `seneschal/` -> the repo root.
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))

# Documented values; an unknown one is recorded verbatim rather than rejected, because a payload this
# module does not recognise is exactly the thing worth having a row about.
LOAD_REASONS = ("session_start", "nested_traversal", "path_glob_match", "include", "compact")


def log_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, LOG_FILE)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def under_repo_root(file_path: str, root: str | None = None) -> bool:
    """Is this a load of one of *this repo's* instruction files?

    The hook is registered globally, so it also fires for every other project on the machine. Those
    rows would be noise in a log whose entire purpose is answering which of THIS tree's sub-routers
    load. Fail-open: a path that cannot be resolved counts as ours, since a dropped row is the only
    thing this module can get badly wrong."""
    root = os.path.normpath(root or REPO_ROOT)
    try:
        resolved = os.path.normpath(os.path.abspath(file_path))
    except (OSError, ValueError, TypeError):
        return True
    return os.path.normcase(resolved).startswith(os.path.normcase(root) + os.sep)


def build_row(payload: dict, root: str | None = None, all_roots: bool = False) -> dict | None:
    """Payload -> row, or None if this load is not ours to record. Pure, so it is testable without a
    filesystem or a hook.

    `all_roots` bypasses the root filter entirely. It is a separate flag rather than a magic `root`
    value because "match everything" has no correct spelling as a path prefix — `os.sep` looks like it
    would work and, on Windows, silently matches nothing at all."""
    if not isinstance(payload, dict):
        return None
    file_path = payload.get("file_path") or ""
    if not isinstance(file_path, str) or not file_path.strip():
        return None
    if not all_roots and not under_repo_root(file_path, root):
        return None
    row = {
        "schema": SCHEMA,
        "at": _stamp(),
        "file_path": file_path,
        "load_reason": str(payload.get("load_reason") or "unknown"),
        "session_id": payload.get("session_id") or None,
        "cwd": payload.get("cwd") or None,
    }
    try:  # a path relative to the repo root is what every consumer actually wants to group by
        row["rel_path"] = os.path.relpath(os.path.abspath(file_path),
                                          os.path.normpath(root or REPO_ROOT)).replace("\\", "/")
    except (OSError, ValueError):
        pass
    return row


def record(payload: dict, state_dir: str | None = None, root: str | None = None,
           all_roots: bool = False) -> bool:
    """Append one row. Returns True iff a row hit disk. **Never raises** — see the module docstring."""
    try:
        row = build_row(payload, root, all_roots)
        if row is None:
            return False
        state_dir = state_dir or DEFAULT_STATE_DIR
        os.makedirs(state_dir, exist_ok=True)
        with open(log_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001 — a row is never worth a session
        return False


def read_rows(state_dir: str | None = None, limit: int | None = None) -> list:
    rows = []
    try:
        with open(log_path(state_dir), encoding="utf-8") as fh:
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


def report(state_dir: str | None = None, known_paths=None) -> dict:
    """The three questions §6.2 exists to answer.

    `known_paths` is the set of instruction files that *should* be loading — pass the sub-router set,
    and `never_loaded` becomes the fold-back candidate list. Without it the report still answers the
    other two."""
    rows = read_rows(state_dir)
    by_path: dict = {}
    by_reason: dict = {}
    sessions: set = set()
    for row in rows:
        key = row.get("rel_path") or row.get("file_path") or "unknown"
        by_path[key] = by_path.get(key, 0) + 1
        reason = row.get("load_reason") or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + 1
        if row.get("session_id"):
            sessions.add(row["session_id"])
    out = {
        "rows": len(rows),
        "sessions": len(sessions),
        "by_path": dict(sorted(by_path.items(), key=lambda kv: -kv[1])),
        "by_reason": by_reason,
        # §3.2's trap: an `@path` import crept in. Should always be zero.
        "include_sightings": by_reason.get("include", 0),
    }
    if known_paths:
        out["never_loaded"] = sorted(set(known_paths) - set(by_path))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="InstructionsLoaded logger (grounding-restructure-spec.md §6.2).")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    p.add_argument("--all-roots", action="store_true",
                   help="record loads outside this repo's tree too (default: drop them as noise)")
    sub = p.add_subparsers(dest="cmd")

    tail = sub.add_parser("tail", help="print the newest rows")
    tail.add_argument("--limit", type=int, default=20)
    sub.add_parser("report", help="what loads, what never loads, and `include` sightings")

    args = p.parse_args(argv)

    if args.cmd == "tail":
        for row in read_rows(args.state_dir, limit=args.limit):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.cmd == "report":
        print(json.dumps(report(args.state_dir), ensure_ascii=False, indent=2))
        return 0

    # No subcommand: hook mode. Read one payload from stdin and append. Exit 0 whatever happens —
    # this runs while instructions are being loaded, and must never be what breaks a session.
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        return 0
    record(payload, args.state_dir, all_roots=args.all_roots)
    return 0


if __name__ == "__main__":
    sys.exit(main())
