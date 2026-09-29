#!/usr/bin/env python3
"""Atomic `state/` write primitives, plus the append/tail/prune jsonl family. Zero intra-repo
imports.

A foundation primitive: the JSON-shaped sibling of `memory_write.py` (which owns the text memory
files and their line-ending rules). New `state/` writers should build on one of the two rather than
hand-rolling their own write.

## Never `open(path, "w")` a `state/` file

It truncates the target BEFORE writing, so any failure after that point — an encoding error, a
disk-full, a kill — leaves nothing, and `state/` is gitignored, so for most of it there is no copy
anywhere. Build the new content in a sibling temp file, flush + fsync it, then `os.replace` it over
the target. `write_text_atomic` and `write_json_atomic` below are that primitive.

**The Windows `PermissionError` retry on `os.replace` is the load-bearing part of both.**
`os.replace` onto a destination another process holds open (a log tail, `seneschald-control.ps1`'s
liveness check, a sibling reader) fails with `PermissionError`/`WinError 5` on Windows, where POSIX
renames straight through. A daemon that hits it on a heartbeat write without a retry simply dies.
The loop here is `memory_write.write_text`'s, ported exactly rather than reinventing the backoff.

## `write_text_atomic` refuses an empty payload over a target that holds bytes

Unless `allow_empty=True`. This is a SEPARATE failure from the truncation one above: a write that
goes *through* the correct atomic helper, called correctly, but is handed an **empty payload** from a
producer that already failed upstream, atomically replaces a full file with zero bytes and exits 0.
**Atomicity stops a PARTIAL write; it does not stop a COMPLETE write of NOTHING.**
`write_json_atomic` never needs `allow_empty`: a JSON-encoded `{}`/`[]`/`null` is never an empty
*string*, so the refusal never fires on legitimate empty JSON.

**Zero bytes only — this is deliberately not a "suspicious shrink" heuristic.** A size-ratio rule
fires on a legitimate prune (`prune_jsonl` below routinely halves a file) and gets switched off,
taking the narrow guard with it.

## The `append_jsonl` / `iter_jsonl` / `tail_jsonl` / `prune_jsonl` family

The record/append/tail/prune shape every append-only ledger under `state/` shares. `append_jsonl` is
the lock-free single-line-append primitive they all rely on — a sub-4 KB line append is atomic on
both platforms without a lock, which is what makes it safe with several writers. `prune_jsonl` is
build-then-`os.replace`, never in-place, same as `write_text_atomic`.
"""
from __future__ import annotations

import json
import os
import time

_REPLACE_ATTEMPTS = 4
_REPLACE_BACKOFF_SEC = 0.05


class EmptyWriteRefused(ValueError):
    """`write_text_atomic` was asked to replace a file that holds bytes with nothing, and would
    not. Its own type rather than a bare `ValueError` so a caller that genuinely drains a file can
    catch exactly this and nothing else. It exists because an
    empty payload reaching a *correct* atomic write still destroys the file, and the only signal
    available at that moment is that the payload is empty and the target is not."""


def _size_on_disk(path: str) -> int:
    """The target's current size in bytes — 0 if it does not exist or cannot be stat'd. Unreadable
    is deliberately treated as "nothing to lose": this guard exists to stop a destructive write, and
    refusing one because `os.stat` failed would turn a durability check into an availability bug."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def write_text_atomic(path: str, text: str, *, allow_empty: bool = False,
                       encoding: str = "utf-8") -> None:
    """Replace `path`'s contents with `text`, atomically. The target is untouched until the very
    last step, so every failure mode short of a successful `os.replace` leaves the previous content
    intact.

    Refuses an empty `text` over a target that already holds bytes (raises `EmptyWriteRefused`)
    unless `allow_empty=True` — see the module docstring's empty-payload section. Creating a new empty
    file, or re-emptying an already-empty one, is never refused: only replacing HELD bytes with
    nothing is."""
    if not text and not allow_empty:
        held = _size_on_disk(path)
        if held:
            raise EmptyWriteRefused(
                f"REFUSED to write 0 bytes over {path}, which currently holds {held:,} bytes. "
                f"The file was NOT modified. An empty payload is almost always a producer that "
                f"failed upstream. Pass allow_empty=True if you really do mean to empty it.")
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, f".{os.path.basename(path)}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except UnicodeEncodeError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows only: another process holds the destination open. Ported from
            # memory_write.write_text's retry — do not reinvent the backoff.
            if attempt == _REPLACE_ATTEMPTS - 1:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            time.sleep(_REPLACE_BACKOFF_SEC * (attempt + 1))


def write_json_atomic(path: str, data, *, indent: int = 2) -> None:
    """Encode `data` as JSON and replace `path` with it, atomically (`write_text_atomic`
    underneath). Never needs `allow_empty`: JSON legitimately encodes `{}`/`[]`/`null` to a
    non-empty string, so the empty-payload refusal above only ever fires on the failure it exists
    for."""
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    write_text_atomic(path, text, allow_empty=True)


def append_jsonl(path: str, row: dict) -> None:
    """Append one JSON object as a line.

    Does not lock — a single sub-4 KB line append is atomic on both platforms without one, the same
    reasoning every append-only ledger in this tree already relies on for several writers. A caller
    with genuine same-process concurrent-writer risk (several threads, not several processes) wants
    its own lock beside this call, not a change to this function."""
    line = json.dumps(row, ensure_ascii=False) + "\n"
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def iter_jsonl(path: str):
    """Yield each line of `path` as a parsed dict, oldest first.

    Malformed lines are skipped silently — this is a reader, and a reader's job is never to explain
    a writer's bug. An absent or unreadable file yields nothing (fail-open, matching every reader of
    these logs in this tree)."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def tail_jsonl(path: str, limit: int) -> list:
    """The newest `limit` rows of `path`. `limit == 0` returns `[]`; `limit < 0` returns
    everything, oldest first — the shape every existing `tail`/`read_*` function in this family
    already uses."""
    rows = list(iter_jsonl(path))
    if limit < 0:
        return rows
    return rows[-limit:] if limit else []


def prune_jsonl(path: str, keep) -> int:
    """Rewrite `path`, keeping only rows for which `keep(row)` is true. Returns how many rows were
    dropped.

    Build-then-`os.replace`, never in-place — a crash mid-prune leaves the old file intact. A
    malformed line is always KEPT (unreadable is never treated as expired, matching every prune in
    this tree) and a `keep` predicate that raises on a row also keeps that row rather than losing
    it — a prune must never be the reason a record disappears."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    survivors, dropped = [], 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            survivors.append(line if line.endswith("\n") else line + "\n")
            continue
        try:
            keeps = not isinstance(row, dict) or keep(row)
        except Exception:  # noqa: BLE001 — a raising predicate must not cost the row
            keeps = True
        if keeps:
            survivors.append(line if line.endswith("\n") else line + "\n")
        else:
            dropped += 1
    if not dropped:
        return 0
    # Pruning to zero surviving rows is a legitimate result (every row expired), not the destructive
    # empty-write case write_text_atomic's default guards against.
    write_text_atomic(path, "".join(survivors), allow_empty=True)
    return dropped
