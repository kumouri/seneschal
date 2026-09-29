#!/usr/bin/env python3
"""The ONE home for the READ FIRST standing-safety items. Standard library only.

Spec: `../docs/read-first-retirement-spec.md`. The context digest's READ FIRST section needed a home
of its own before the digest could be retired; this store is that home.

## What this is

`state/standing-safety.json` — one gitignored JSON store, one owning script (this one). Every item is
something the owner has already been told, already decided, or explicitly closed, where **raising it
again is the harm**. Each item carries `id`, `text`, `source`, `added`, `retire_when`, and (once
retired) `retired_at` / `retired_reason`. Nothing is ever deleted — `retire` marks a row, it does not
remove it — so the store is also its own audit trail of what used to need saying and when it stopped.

**This module is the ONLY writer.** `presence.py` only ever calls `render()` (read-only); Dream
touches the store only through this CLI, never by hand-editing the JSON or by prose in
`modes/dream.md`. That is the same one-script-one-file shape the work-item register already holds:
nothing else writes it.

## `retire_when` — three shapes, and why a fourth is refused

Every item declares when it stops being read, in one of three ways, because "just leave it forever"
is how the digest's READ FIRST section grew without bound — nothing ever left:

* **A date** (`YYYY-MM-DD`) — self-expiring. `list --due` names it once that date has passed (the
  owner's activity day, `clock.local_today`); nothing retires it automatically, because auto-retiring
  a safety item on a date alone would be exactly the kind of silent behaviour change this store
  exists to avoid (Dream still has to look and decide).
* **`when <decision-id> lands`** — gated on a decision landing. Structurally this module cannot
  verify that on its own (a decision landing is a fact about somewhere else, not this store), so
  `list --due` never claims one of these is ready; the sentence itself is the pointer Dream checks
  by hand.
* **`standing`** — never auto-flagged. Permanently true until someone runs `retire` directly (the
  spec's class C — a standing rule about how to address or treat the owner).

A value that is none of the three is **refused at `add` time (exit 2)**, not stored — a malformed
`retire_when` is not a smaller problem than a missing one; it is the exact thing that lets items go
unclassified in the first place (the spec's §5: classification decided once, at write time, never
re-derived nightly).

## `render()` — verbatim, and why it carries no metadata

`render()` emits a `## READ FIRST — standing safety items (do NOT re-raise cold)` heading, then one
bullet per active item, verbatim. It carries **no `source`/`added`/`id` decoration** — those are this
module's bookkeeping, not the model's business, and the WORDING of a standing-safety line is
load-bearing (a phrase like "PAUSED BY CHOICE — this is OVER" does work a paraphrase would not);
appending metadata after it would be exactly the summarising that warns against. Returns `""` for no
store / an unreadable store / zero active items — fail-open, so the caller's own cap-and-truncate
logic is the only thing standing between a bad render and the grounding prompt.

## `import-digest` — one shot, and it is not run here

The READ FIRST items are the owner's own private content — nothing about them is seeded or committed.
`import-digest` parses a leftover `state/context-digest.md` (which does not exist in a fresh checkout
or in CI) into rows with `source="context-digest.md"` and `retire_when="standing"` (the safest default
for a bullet whose real class was never recorded — a human, or Dream, reclassifies it later with a
plain `retire`/`add` pair; guessing a date or a decision id from prose here would be exactly the
re-derivation the spec rejects). It refuses to run a second time (`--force` to override) so a stray
re-run cannot duplicate every item. **Run this on the host, by the owner or by Dream — never inside
a worktree job**, where the gitignored digest does not exist to read.

CLI:
    python standing_safety.py add "<text>" --source <where-this-came-from> --retire-when <date|"when <decision> lands"|standing>
    python standing_safety.py retire <id> --reason "<why>"
    python standing_safety.py list [--all] [--due]
    python standing_safety.py render
    python standing_safety.py import-digest [--digest <path>] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import date, datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import clock  # the owner's activity day (tz_common facade)  # noqa: E402
import stateio  # atomic JSON read/write  # noqa: E402

SCHEMA = "seneschal.standing-safety/1"
STORE_FILE = "standing-safety.json"
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

HEADING = "## READ FIRST — standing safety items (do NOT re-raise cold)"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DECISION_GATE_RE = re.compile(r"^when\s+\S.*\s+lands$", re.IGNORECASE)
STANDING = "standing"

RETIRE_WHEN_HELP = (
    "a date (YYYY-MM-DD), the literal phrase 'when <decision-id> lands', or the literal word "
    "'standing' for an item with no expiry")


def default_state_dir() -> str:
    return DEFAULT_STATE_DIR


def store_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, STORE_FILE)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _new_id(now: datetime | None = None) -> str:
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"ss-{when:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"


def classify_retire_when(value) -> str | None:
    """`"date"` / `"decision"` / `"standing"`, or `None` when `value` matches none of the three
    accepted shapes. Never raises."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    if v.lower() == STANDING:
        return "standing"
    if _DATE_RE.match(v):
        try:
            date.fromisoformat(v)
            return "date"
        except ValueError:
            return None
    if _DECISION_GATE_RE.match(v):
        return "decision"
    return None


# --------------------------------------------------------------------------- store I/O

def _load(state_dir: str | None = None) -> dict:
    """`{"schema", "items": [...]}`. Absent, unreadable, or malformed reads as an EMPTY store —
    fail-open like every reader in this tree; a broken store must not crash a `render()` call on the
    cold-grounding path."""
    path = store_path(state_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"schema": SCHEMA, "items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"schema": SCHEMA, "items": []}
    return {"schema": data.get("schema", SCHEMA),
            "items": [it for it in data["items"] if isinstance(it, dict)]}


def _save(state_dir: str | None, data: dict) -> None:
    stateio.write_json_atomic(store_path(state_dir), data)


# --------------------------------------------------------------------------- writing

def add(state_dir: str | None = None, *, text=None, source=None, retire_when=None,
        now: datetime | None = None, item_id: str | None = None) -> tuple[bool, str]:
    """Append one item. Returns `(True, id)` on success or `(False, "<reason>")` on refusal — a
    malformed request is refused, never silently coerced (`retire_when`'s whole point is that an
    unclassified item is worse than a missing one)."""
    text = (text or "").strip()
    source = (source or "").strip()
    if not text:
        return False, "a standing-safety item needs TEXT — the instruction itself, verbatim"
    if not source:
        return False, "a standing-safety item needs --source — where this came from"
    kind = classify_retire_when(retire_when)
    if kind is None:
        return False, f"--retire-when {retire_when!r} is not one of the accepted shapes: {RETIRE_WHEN_HELP}"
    data = _load(state_dir)
    new_id = item_id or _new_id(now)
    data["items"].append({
        "id": new_id,
        "text": text,
        "source": source,
        "added": _stamp(now),
        "retire_when": retire_when.strip(),
        "retired_at": None,
        "retired_reason": None,
    })
    _save(state_dir, data)
    return True, new_id


def retire(state_dir: str | None = None, *, item_id=None, reason=None,
           now: datetime | None = None) -> tuple[bool, str]:
    """Mark an item retired. Never removes the row — the store is its own audit trail. Refuses
    (never guesses) an unknown id or a re-retirement of an already-retired row."""
    item_id = (item_id or "").strip()
    reason = (reason or "").strip()
    if not item_id:
        return False, "retire needs an item id — `list` prints them"
    if not reason:
        return False, "retire needs --reason — why this item no longer needs saying"
    data = _load(state_dir)
    for item in data["items"]:
        if item.get("id") == item_id:
            if item.get("retired_at"):
                return False, f"{item_id} was already retired at {item['retired_at']}"
            item["retired_at"] = _stamp(now)
            item["retired_reason"] = reason
            _save(state_dir, data)
            return True, item_id
    return False, f"no item {item_id!r} in {store_path(state_dir)} — `list --all` shows every id"


# --------------------------------------------------------------------------- reading

def list_items(state_dir: str | None = None, *, include_retired: bool = False) -> list:
    """Items oldest-first. Active only unless `include_retired`."""
    items = _load(state_dir)["items"]
    if include_retired:
        return list(items)
    return [it for it in items if not it.get("retired_at")]


def due_items(state_dir: str | None = None, now: datetime | None = None) -> list:
    """Active items whose `retire_when` is a DATE on or before the owner's current activity day
    (`clock.local_today` — the owner's timezone, cut at `owner.dayBoundaryHour`). Never includes a
    decision-gated or standing item — this module cannot verify a decision landed, and a standing
    item is never due by construction (module docstring)."""
    today = clock.local_today(now=now)
    out = []
    for item in list_items(state_dir):
        if classify_retire_when(item.get("retire_when")) != "date":
            continue
        try:
            if date.fromisoformat(item["retire_when"]) <= today:
                out.append(item)
        except (KeyError, ValueError):
            continue
    return out


def has_active_items(state_dir: str | None = None) -> bool:
    return bool(list_items(state_dir))


def render(state_dir: str | None = None) -> str:
    """The block `presence.py` injects verbatim, or `""` when there is nothing to inject (no store,
    an unreadable store, or zero active items). Never raises."""
    try:
        items = list_items(state_dir)
    except Exception:  # noqa: BLE001 — a broken store must render empty, not crash grounding
        return ""
    if not items:
        return ""
    lines = [HEADING]
    for item in items:
        text = (item.get("text") or "").strip()
        if text:
            lines.append(f"- {text}")
    if len(lines) < 2:  # a heading with nothing under it is not a standing-safety block
        return ""
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- import-digest (one-shot)

#: Matches any markdown heading containing "READ FIRST" (with or without a leading emoji) and the
#: next heading at the same or shallower depth that ends the section. Kept here rather than imported
#: so this module never depends on `presence.py` (which depends on THIS module; the other way would
#: be a cycle).
_DIGEST_HEADING_RE = re.compile(r"^(#{1,6})\s+.*READ FIRST", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})\s")
_BULLET_RE = re.compile(r"^-\s+")

DEFAULT_DIGEST_FILE = "context-digest.md"


def _digest_section(digest_path: str) -> list:
    with open(digest_path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    depth, section = None, []
    for line in lines:
        if depth is None:
            m = _DIGEST_HEADING_RE.match(line)
            if m:
                depth = len(m.group(1))
            continue
        end = _HEADING_RE.match(line)
        if end and len(end.group(1)) <= depth:
            break
        section.append(line)
    return section


def _bullets_of(section_lines: list) -> list:
    """Fold a markdown bullet list into one text per top-level `- ` item — a wrapped/indented
    continuation line joins the bullet above it, matching how multi-line digest bullets are
    written."""
    items, current = [], None
    for line in section_lines:
        if _BULLET_RE.match(line):
            if current is not None:
                items.append(current.strip())
            current = _BULLET_RE.sub("", line, count=1)
        elif current is not None and line.strip():
            current += " " + line.strip()
    if current is not None:
        items.append(current.strip())
    return [i for i in items if i]


def import_digest(state_dir: str | None = None, *, digest_path: str | None = None,
                   force: bool = False, now: datetime | None = None) -> tuple[bool, str, int]:
    """One-shot: fold a leftover digest's READ FIRST bullets into the store as `source="context-digest.md"`
    / `retire_when="standing"` rows. Refuses (`(False, ..., 0)`) when the store already holds ANY
    item (active or retired) unless `force=True` — a second run must not duplicate every bullet."""
    path = digest_path or os.path.join(state_dir or DEFAULT_STATE_DIR, DEFAULT_DIGEST_FILE)
    existing = _load(state_dir)["items"]
    if existing and not force:
        return False, (f"the store already holds {len(existing)} item(s) — import-digest is one-shot. "
                        "Pass --force if you really mean to import again."), 0
    try:
        section = _digest_section(path)
    except OSError as exc:
        return False, f"could not read {path}: {exc}", 0
    bullets = _bullets_of(section)
    if not bullets:
        return False, f"no READ FIRST bullets found in {path}", 0
    data = _load(state_dir)
    added = 0
    for text in bullets:
        data["items"].append({
            "id": _new_id(now),
            "text": text,
            "source": DEFAULT_DIGEST_FILE,
            "added": _stamp(now),
            "retire_when": STANDING,
            "retired_at": None,
            "retired_reason": None,
        })
        added += 1
    _save(state_dir, data)
    return True, f"imported {added} item(s) from {path}", added


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="The one home for the READ FIRST standing-safety items.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add a standing-safety item")
    a.add_argument("text")
    a.add_argument("--source", required=True, help="where this came from — a decision reference, a memory file, the owner's own words")
    a.add_argument("--retire-when", required=True, help=RETIRE_WHEN_HELP)

    r = sub.add_parser("retire", help="retire an item by id (never deletes it)")
    r.add_argument("item_id")
    r.add_argument("--reason", required=True)

    ls = sub.add_parser("list", help="print items, newest bookkeeping first is NOT guaranteed — oldest-first")
    ls.add_argument("--all", action="store_true", help="include retired items")
    ls.add_argument("--due", action="store_true",
                     help="only DATE-shaped items whose date has passed (never decision-gated or standing)")

    sub.add_parser("render", help="print exactly what presence.py would inject")

    imp = sub.add_parser("import-digest", help="ONE-SHOT: fold a leftover digest's READ FIRST bullets in")
    imp.add_argument("--digest", default=None, help="defaults to <state-dir>/context-digest.md")
    imp.add_argument("--force", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "add":
        ok, result = add(args.state_dir, text=args.text, source=args.source,
                          retire_when=args.retire_when)
        if not ok:
            print(f"standing_safety: refused — {result}", file=sys.stderr)
            print(json.dumps({"ok": False, "refused": result}, ensure_ascii=False))
            return 2
        print(json.dumps({"ok": True, "id": result}, ensure_ascii=False))
        return 0

    if args.cmd == "retire":
        ok, result = retire(args.state_dir, item_id=args.item_id, reason=args.reason)
        if not ok:
            print(f"standing_safety: {result}", file=sys.stderr)
        print(json.dumps({"ok": ok, "id": args.item_id if ok else None}, ensure_ascii=False))
        return 0 if ok else 1

    if args.cmd == "list":
        items = due_items(args.state_dir) if args.due else list_items(
            args.state_dir, include_retired=args.all)
        for item in items:
            print(json.dumps(item, ensure_ascii=False))
        return 0

    if args.cmd == "render":
        block = render(args.state_dir)
        if block:
            print(block, end="")
        return 0

    ok, message, count = import_digest(args.state_dir, digest_path=args.digest, force=args.force)
    print(f"standing_safety: {message}", file=sys.stderr)
    print(json.dumps({"ok": ok, "imported": count}, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
