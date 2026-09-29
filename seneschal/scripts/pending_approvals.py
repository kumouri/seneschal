#!/usr/bin/env python3
"""`state/pending-approvals.json` — the ONE draft-and-hold ledger, one writer. Stdlib only.

Email triage and Slack triage both hold ask-high drafts here under one `a<N>` id space (so a
one-word reply — `send a7` — is unambiguous across channels), and the outbound send gate
(`send_gate.py`) reads approvals from the same file. Because several writers and a reader share it,
its shape is a **contract**: a `schema` version field and a documented row shape
(`../state/README.md`), writer and reader held to the same version.

## Why a module, not prose

When a triage skill described the append in prose, the model turn did the id arithmetic itself by
reading the file. That is tolerable with one writer; with two it is two independent computations of
the same `a<N>` sequence, which collide the first time they race or disagree. This module is the one
writer every skill calls instead — never hand-append, never compute an id by reading the file.

## The migration: tolerant read, versioned write

An older file (anything written before the `schema` field existed) is a **bare JSON array**. `load()`
accepts that legacy shape unconditionally (wraps it as `{"schema": SCHEMA, "approvals": <array>}` in
memory) and also accepts the `{"schema", "approvals": [...]}` object shape; `save()` always stamps
the CURRENT `SCHEMA` value regardless of what it read, so one round trip through this module is the
whole migration — no separate script, no downtime.

**A file that parses but is neither shape raises `CorruptStore`, and a file that fails to parse also
raises `CorruptStore` — load() never treats corruption as "no drafts."** Silently falling back to an
empty store would mean the next `add()`/`resolve()` atomically writes that empty-plus-one-entry list
over whatever real (but malformed) content was there. A cache may fail open; the owner's pending
sends may not.

## `next_id` scans rather than counts

There is no separate sequence counter, because one would need seeding to match every id already
written, and a wrong seed silently collides with a real entry. Scanning every existing `a<N>` id and
returning one past the highest is the same answer with nothing to get wrong, at the cost of an O(n)
scan over a ledger that is never large.

## Verbs

`add(fields)` stamps `id`/`created_at`/`status: "pending"` onto caller-supplied content fields
(`kind`, `channelRef`, `body`, ...) and appends — refuses if the caller tries to set any of those
three itself, so two callers can never mint the same id by hand. `list_entries()` reads, optionally
filtered by `status`/`kind`. `resolve(id, status)` flips an existing entry's status (+ optional extra
fields, e.g. an error string on a failed send) — raises `NotFound` rather than create a phantom row
for a typo'd id.

Atomicity: `save()` goes through `stateio.write_json_atomic` (build-then-`os.replace`).

CLI:
    python pending_approvals.py add                         # entry fields (no id/created_at/status) JSON on stdin
    python pending_approvals.py list [--status S] [--kind K]
    python pending_approvals.py resolve <id> --status S [--fields '{"error": "..."}']
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import paths  # noqa: E402
import stateio  # noqa: E402

FILENAME = "pending-approvals.json"
SCHEMA = "seneschal.pending-approvals/1"

#: Fields `add()` stamps itself — a caller supplying any of these is refused rather than silently
#: overridden, so a bug that tries to mint its own id fails loudly instead of colliding quietly.
_STAMPED_FIELDS = {"id", "created_at", "status"}

_ID_RE = re.compile(r"^a(\d+)$", re.IGNORECASE)

#: `resolve()`'s exit code when the id doesn't exist — a distinct, documented refusal code rather
#: than a bare 1.
EXIT_NOT_FOUND = 3


class CorruptStore(ValueError):
    """The store file exists but is not valid JSON, or is JSON of a shape this module does not
    recognize (neither a bare list nor an object carrying an `approvals` list). Raised rather than
    treated as empty — see the module docstring's migration section for why."""


class NotFound(KeyError):
    """`resolve()` was asked to update an id that isn't in the store."""


def _path(state_dir: str | None = None) -> str:
    return os.path.join(paths.state_dir(state_dir), FILENAME)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(state_dir: str | None = None) -> dict:
    """The store as `{"schema": ..., "approvals": [...]}`, tolerating the legacy bare-array shape.

    Missing file → a fresh empty store (nothing has ever been held). A file that exists but fails to
    parse, or parses to something that is neither a bare list nor an object with an `approvals`
    list, raises `CorruptStore` — never silently treated as empty (module docstring)."""
    path = _path(state_dir)
    if not os.path.exists(path):
        return {"schema": SCHEMA, "approvals": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CorruptStore(f"{FILENAME} exists but is not valid JSON: {exc}") from None
    if isinstance(raw, list):
        return {"schema": SCHEMA, "approvals": raw}  # legacy, un-versioned shape — tolerated
    if isinstance(raw, dict) and isinstance(raw.get("approvals"), list):
        doc = dict(raw)
        doc["schema"] = raw.get("schema") or SCHEMA
        return doc
    raise CorruptStore(
        f"{FILENAME} is valid JSON but neither a bare list nor an object with an 'approvals' "
        f"list — refusing to guess its shape rather than silently treating it as empty")


def save(doc: dict, *, state_dir: str | None = None) -> None:
    """Atomically replace the store. Always stamps the CURRENT `SCHEMA` — a writer that read a
    legacy un-versioned (or older-versioned) file still writes the current version out, which is
    the whole migration: one round trip through this module upgrades the file."""
    doc = dict(doc)
    doc["schema"] = SCHEMA
    stateio.write_json_atomic(_path(state_dir), doc)


def next_id(doc: dict) -> str:
    """One past the highest existing `a<N>` id in `doc` — see the module docstring for why this
    scans rather than tracks a separate counter."""
    highest = 0
    for entry in doc.get("approvals", []):
        match = _ID_RE.match(str(entry.get("id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"a{highest + 1}"


def list_entries(*, status: str | None = None, kind: str | None = None,
                 state_dir: str | None = None) -> list[dict]:
    """Every entry, optionally filtered by `status` and/or `kind`. Read-only; raises `CorruptStore`
    the same as `load()` if the file can't be trusted."""
    rows = load(state_dir).get("approvals", [])
    if status is not None:
        rows = [r for r in rows if r.get("status") == status]
    if kind is not None:
        rows = [r for r in rows if r.get("kind") == kind]
    return rows


def find(entry_id: str, *, state_dir: str | None = None) -> dict | None:
    """The entry with this id, or `None`. Case-insensitive (`a7` == `A7`) — matching the approval
    grammar's own tolerance (`send a7` / `send A7`)."""
    wanted = str(entry_id).strip().lower()
    for entry in load(state_dir).get("approvals", []):
        if str(entry.get("id", "")).strip().lower() == wanted:
            return entry
    return None


def add(fields: dict, *, state_dir: str | None = None, now: datetime | None = None) -> dict:
    """Append a new pending-approval entry. `fields` supplies every content field the kind needs
    (`kind`, `channelRef`, `body`, `to`, `summary`, `bodyPreview`, `sources`, `critique_note`,
    `thread_seen_ts`, ...) — never `id`/`created_at`/`status`, which this function stamps itself.

    Returns the stamped entry (the same dict that was appended, so a caller can echo the new id
    straight back into the triage summary or a push without a second read)."""
    collision = _STAMPED_FIELDS & fields.keys()
    if collision:
        raise ValueError(
            f"add() stamps {sorted(_STAMPED_FIELDS)} itself — caller must not set "
            f"{sorted(collision)}")
    if not fields.get("kind"):
        raise ValueError("add() requires a non-empty 'kind'")
    doc = load(state_dir)
    entry = dict(fields)
    entry["id"] = next_id(doc)
    entry["created_at"] = _stamp(now)
    entry["status"] = "pending"
    doc.setdefault("approvals", []).append(entry)
    save(doc, state_dir=state_dir)
    return entry


def resolve(entry_id: str, status: str, *, fields: dict | None = None,
            state_dir: str | None = None) -> dict:
    """Set an existing entry's `status` (`sent` / `rejected` / `failed` / `approved`, ...) and merge
    any additional fields (e.g. an `error` string on a failed send). Never mints a new entry —
    raises `NotFound` for an unknown id so a typo can't silently create a phantom row."""
    doc = load(state_dir)
    wanted = str(entry_id).strip().lower()
    for entry in doc.get("approvals", []):
        if str(entry.get("id", "")).strip().lower() == wanted:
            entry["status"] = status
            if fields:
                entry.update(fields)
            save(doc, state_dir=state_dir)
            return entry
    raise NotFound(f"no pending-approvals entry with id {entry_id!r}")


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="state/pending-approvals.json — the one draft-and-hold ledger. add / list / resolve.")
    ap.add_argument("--state-dir", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("add", help="entry fields (no id/created_at/status) as JSON on stdin")

    ls = sub.add_parser("list", help="print matching entries as a JSON array")
    ls.add_argument("--status", default=None)
    ls.add_argument("--kind", default=None)

    rs = sub.add_parser("resolve", help="set an existing entry's status (+ optional extra fields)")
    rs.add_argument("id")
    rs.add_argument("--status", required=True)
    rs.add_argument("--fields", default=None, help="extra fields to merge, as a JSON object")

    args = ap.parse_args(argv)

    if args.cmd == "add":
        raw = sys.stdin.read()
        try:
            fields = json.loads(raw) if raw.strip() else {}
        except ValueError as exc:
            print(f"pending_approvals: stdin is not valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(fields, dict):
            print("pending_approvals: stdin must be a JSON object", file=sys.stderr)
            return 2
        try:
            entry = add(fields, state_dir=args.state_dir)
        except ValueError as exc:  # CorruptStore is a ValueError too
            print(f"pending_approvals: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(entry, ensure_ascii=False))
        return 0

    if args.cmd == "list":
        try:
            rows = list_entries(status=args.status, kind=args.kind, state_dir=args.state_dir)
        except CorruptStore as exc:
            print(f"pending_approvals: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(rows, ensure_ascii=False))
        return 0

    # resolve
    extra_fields = None
    if args.fields:
        try:
            extra_fields = json.loads(args.fields)
        except ValueError as exc:
            print(f"pending_approvals: --fields is not valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(extra_fields, dict):
            print("pending_approvals: --fields must be a JSON object", file=sys.stderr)
            return 2
    try:
        entry = resolve(args.id, args.status, fields=extra_fields, state_dir=args.state_dir)
    except NotFound as exc:
        print(f"pending_approvals: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except CorruptStore as exc:
        print(f"pending_approvals: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(entry, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
