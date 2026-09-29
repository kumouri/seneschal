#!/usr/bin/env python3
"""Atomic, UTF-8-safe writes for the assistant's memory files and state JSON.

**Why this exists, precisely.** The memory files under `state/` (`carry-over.md`,
`context-digest.md`, `run-log.md`, ...) are gitignored, so for most of them there is **no copy
anywhere else**. The classic way to lose one outright is a script that writes it with
`open(path, "w")` — which truncates the target *first* — and then raises (a `UnicodeEncodeError` on a
non-ASCII character under Windows's non-UTF-8 default encoding is the usual culprit). The truncate
has already happened; the content never arrives; the file is empty and there is nothing to restore
it from.

This is a **total-loss** failure mode and it is distinct from the lost-*append* race that
`append_text` below addresses: that one drops a single entry under concurrency, this empties the
entire file on one bad write, and it can hit any memory file or any `state/*.json` the daemon
rewrites.

**The rule, and it is not negotiable for anything under `state/`:** never write with a bare
truncating `open(path, "w")`. Build the new content in a sibling temp file with an explicit
`encoding="utf-8"`, flush and fsync it, then `os.replace` it over the target. A crash, a disk-full,
or an encoding error then leaves the **old** file standing, which is always better than an empty one.
That includes the Windows `PermissionError` retry: `os.replace` onto a path another process holds
open fails on Windows where POSIX renames straight through.

This module is `stateio.write_json_atomic`'s text-shaped twin, plus the append path, plus a CLI so a
**skill** can call it instead of improvising a one-liner. That last part matters: memory files are
mostly written by prompt-side code, and a durability rule that lives only in a prompt is a rule that
does not run. `state_backup.py` is the second line of defence, so a total loss that does get past
this costs a day rather than everything.

## Atomicity says nothing about an EMPTY payload

A write can go through this module's CLI correctly and still destroy a file: pipe it an **empty
stdin** because the step that was supposed to produce the content already failed and the shell ran
the next statement anyway. The write is atomic, the temp file is fsynced, `os.replace` lands — and
zero bytes go faithfully over the whole file with exit 0.

**Atomicity prevents a PARTIAL write. It does not prevent a COMPLETE WRITE OF NOTHING** — and from
the outside the second is indistinguishable from a successful save: no exception, no leftover temp
file, nothing to notice. So `write_text` **refuses** an empty payload when the target exists and
holds bytes: nothing is written, and the error names the target's current size and the flag that
overrides it. `--allow-empty` (CLI) / `allow_empty=True` (library) is that flag, because deliberately
emptying a state file is a real operation (a queue whose every entry has been handled), and a guard
that breaks the legitimate caller is a guard that gets removed.

**Zero bytes only. There is deliberately no "suspicious shrink" heuristic.** Zero-over-content is not
a judgment call; *"it got 90% smaller"* is. A size-ratio rule would fire on a legitimate prune — a
reconcile that drops half of `reminders.json`'s spent rows — and a check that calls the correct
operation a bug is a check somebody switches off.

**`append` and `prepend` are NOT guarded, because they cannot lose content this way.** An empty
append writes no bytes to a file it never truncates; an empty prepend writes `"" + old`, which is
`old`. Both are no-ops, not deletions. What an empty payload there *does* still mean is the same
failed producer — so the **CLI** warns on stderr and exits 0, while the library functions stay silent:
their callers are the fail-open `state/` writers, and a warning printed from the reminder path costs
more than it buys.

## Line endings are preserved, and that is not a nicety

`state/` can be **mixed**: one memory file CRLF, its neighbour LF, both written by the same code
paths (hand edits on Windows, tools on either platform). A `prepend_text` that read the old content
in universal-newline mode (`\r\n` → `\n`) and wrote it back with `newline=""` (no translation) would
convert every ending in a CRLF file to LF — a whole-file rewrite in which the one line actually added
is invisible in the diff. A durability fix that makes every diff unreadable is not a fix, so the
target's existing endings are detected from its bytes and matched. A file with no line ending yet —
new, or empty — is written **verbatim**, because the honest answer to "what did this file use?" is
"nothing yet", and guessing is how mixing starts.

CLI:
    python memory_write.py write <path>            # content on stdin, atomic replace
    python memory_write.py append <path>           # content on stdin, concurrency-safe append
    python memory_write.py prepend <path>          # content on stdin, lock-guarded read-modify-write
    python memory_write.py write <path> --allow-empty   # ...and I really do mean to empty it

`write` REFUSES empty stdin when <path> already holds bytes: nothing is written and it exits **2**.
`append`/`prepend` warn on empty stdin and exit 0.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

_REPLACE_ATTEMPTS = 4
_REPLACE_BACKOFF_SEC = 0.05
_LOCK_TIMEOUT_SEC = 5.0
_LOCK_POLL_SEC = 0.02

#: How much of the target to read when sniffing its line endings. Bounded so a write never pays to
#: read an arbitrarily large file: the biggest text file `state/` rewrites wholesale is typically
#: `run-log.md`, well under 1 MiB even after months of use, and on anything larger a prefix this size
#: is still overwhelmingly decisive.
_SNIFF_LIMIT = 1 << 20

_BOM = b"\xef\xbb\xbf"
_SPLIT_NEWLINES = re.compile(r"\r\n|\r|\n")


class EmptyWriteRefused(ValueError):
    """`write_text` was asked to replace a file that holds bytes with nothing, and would not.

    Its own type rather than a bare `ValueError` so a caller that genuinely drains a file can catch
    exactly this and nothing else. It exists because an empty
    payload reaching a *correct* atomic write still destroys the file, and the only signal available
    at that moment is that the payload is empty and the target is not."""


def _size_on_disk(path: str) -> int:
    """The target's current size in bytes — 0 if it does not exist or cannot be stat'd.

    Unreadable is deliberately treated as "nothing to lose". This guard exists to stop a destructive
    write; refusing one because `os.stat` failed would turn a durability check into an availability
    bug, and `state/`'s writers are fail-open by house rule."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def detect_newline(path: str, default: str | None = None) -> str | None:
    """The target's dominant line ending — `"\\r\\n"`, `"\\n"`, or `default` if it has none.

    Reads **bytes**, deliberately: the whole point is to see the terminators that text mode exists to
    hide. A file that does not exist, cannot be read, or holds no line ending at all is not guessed
    at — it returns `default` and the caller writes what it was given, unchanged."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read(_SNIFF_LIMIT)
    except OSError:
        return default
    crlf = blob.count(b"\r\n")
    lf = blob.count(b"\n") - crlf
    if not crlf and not lf:
        return default
    return "\r\n" if crlf >= lf else "\n"


def _has_bom(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(3) == _BOM
    except OSError:
        return False


def _retarget_newlines(text: str, newline: str | None) -> str:
    """Rewrite every line ending in `text` to `newline`. `None` means leave it exactly as given."""
    if newline is None:
        return text
    return _SPLIT_NEWLINES.sub(newline, text)


def write_text(path: str, text: str, newline: str | None = "preserve",
               allow_empty: bool = False) -> None:
    """Replace `path`'s contents with `text`, atomically and as UTF-8.

    The target is untouched until the very last step, so **every** failure mode short of a successful
    replace leaves the previous content intact. That is the whole point: a stale `carry-over.md` is a
    recoverable annoyance, an empty one is unrecoverable.

    **An empty `text` over a target that holds bytes raises `EmptyWriteRefused` and writes nothing** —
    the failure where the atomic path works perfectly and the payload is the bug. Pass
    `allow_empty=True` for the one shape that means it: deliberately draining a file. Creating a new
    empty file, or re-emptying an already-empty one, is not destruction and is never refused.

    `newline` defaults to `"preserve"`: match whatever the target already uses (see
    :func:`detect_newline`), and write `text` byte-for-byte when the target has no ending to match.
    Pass `"\\n"` or `"\\r\\n"` to force one, or `None` to translate nothing. A UTF-8 BOM already on
    the target is kept — dropping it would silently change how another reader decodes the file."""
    if not text and not allow_empty:
        # FIRST, before the newline sniff and long before the temp file: a refusal must not have
        # touched anything, including a sibling `.tmp` that would look like an interrupted write.
        held = _size_on_disk(path)
        if held:
            raise EmptyWriteRefused(
                f"REFUSED to write 0 bytes over {path}, which currently holds {held:,} bytes. "
                f"The file was NOT modified. An empty payload is almost always a producer that "
                f"failed upstream (a staging step raised and the shell ran the next statement "
                f"anyway). If you really do mean to empty it, pass --allow-empty (CLI) or "
                f"allow_empty=True (write_text).")
    if newline == "preserve":
        newline = detect_newline(path)
    text = _retarget_newlines(text, newline)
    encoding = "utf-8-sig" if _has_bom(path) else "utf-8"
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, f".{os.path.basename(path)}.tmp")
    # encoding is explicit: the classic total loss is a UnicodeEncodeError under Windows's non-UTF-8
    # default, AFTER open(w) has already truncated the real file. `newline=""` here means "write the
    # string's own terminators" -- the retargeting above has already put the right ones in.
    try:
        with open(tmp, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except UnicodeEncodeError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        # "surrogates not allowed" is the usual shape here. A lone surrogate reaches here when
        # an emoji was pasted into a source literal as its UTF-16 pair instead of written as an
        # escape. `path` is untouched -- say so, because the whole question in the first minute after
        # this fires is whether the file survived.
        if "surrogate" in str(e):
            raise UnicodeEncodeError(
                e.encoding, e.object, e.start, e.end,
                f"{e.reason} -- a lone surrogate in the text (an emoji pasted as a UTF-16 pair "
                f"rather than written as \\U0001XXXX?). {path} was NOT modified.") from None
        raise
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows only: another process holds the destination open. Same retry `stateio` uses.
            if attempt == _REPLACE_ATTEMPTS - 1:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            time.sleep(_REPLACE_BACKOFF_SEC * (attempt + 1))


class _FileLock:
    """A crude cross-process lock: an exclusively-created sidecar file.

    Deliberately crude. `prepend_text` is the one operation that genuinely cannot be made atomic by a
    syscall — it has to read the old content — so it needs mutual exclusion, and a stdlib-only,
    Windows-and-POSIX lock with no dependency is worth more here than a sophisticated one. A stale
    lock (a crashed holder) is broken after `_LOCK_TIMEOUT_SEC` rather than deadlocking forever:
    losing the race is recoverable, hanging the daemon is not."""

    def __init__(self, path: str):
        self.path = path + ".lock"

    def __enter__(self):
        deadline = time.monotonic() + _LOCK_TIMEOUT_SEC
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            # PermissionError is NOT a different failure here, it is the same one wearing a Windows
            # coat: a file another thread is deleting enters a "delete pending" state, and an open
            # against it returns ERROR_ACCESS_DENIED rather than "already exists". Catching only
            # FileExistsError lost 1 append in 24 under contention — the exact defect this lock is
            # here to prevent, reintroduced by the lock itself.
            except (FileExistsError, PermissionError):
                if time.monotonic() >= deadline:
                    # Break it. A held-forever lock would be a worse failure than a lost race.
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    deadline = time.monotonic() + _LOCK_TIMEOUT_SEC
                time.sleep(_LOCK_POLL_SEC)

    def __exit__(self, *exc):
        try:
            os.unlink(self.path)
        except OSError:
            pass
        return False


def append_text(path: str, text: str) -> None:
    """Append `text` without losing a concurrent writer's entry.

    A whole-file read-modify-write of the `run-log.md` mirror loses an entry whenever a concurrent
    session rewrites the file between this run's read and its write. The store is the system of
    record, so nothing is truly lost, but the local mirror is the cheap pre-read every mode leans on at
    orientation, and a Dream digest that reads it under-reports the day.

    **The textbook answer is `O_APPEND`, and on Windows it is wrong — which is the platform this
    runs on.** POSIX makes `O_APPEND` move the offset to the end *as part of the write syscall*, so
    appends interleave rather than clobber. The Windows CRT emulates the flag by seeking to the end
    and then writing, which is two operations with a gap: two writers can both seek to the same end
    and one overwrites the other. Measured, not assumed — two dozen concurrent appends through the
    plain `"a"` mode on Windows land only about half their entries. So this takes the same lock
    `prepend_text` does.

    Slower than a bare append, and correct on every platform the daemon runs on. If this ever needs to
    be fast, the fix is a per-process queue, **not** removing the lock.

    Appending is the one operation that can *mix* a file's line endings rather than convert them —
    twenty CRLF lines followed by one LF line — so it matches the target's existing ending too."""
    with _FileLock(path):
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        text = _retarget_newlines(text, detect_newline(path))
        # Plain "utf-8", never "utf-8-sig". Measured, because the obvious reason is wrong: the sig
        # codec does NOT plant a second BOM mid-file (CPython emits one only when the file is empty).
        # What it does do is give a BRAND-NEW file a BOM, so an append that happens to be the first
        # would decide the encoding of a file nobody chose one for. `write_text` preserves a BOM that
        # is already there; this must not invent one.
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())


def prepend_text(path: str, text: str) -> None:
    """Put `text` at the top of `path` — newest-first, the shape `run-log.md` uses.

    Read-modify-write, so it takes the lock. Everything else about it is `write_text`: the old file
    stands until the atomic replace lands.

    The read is universal-newline (so `old` comes back with `\\n` whatever the file used) and
    `write_text` then retargets the whole result to the ending the file *still* has on disk — it has
    not been touched yet. So a CRLF file round-trips as CRLF — without this, the read would translate
    and the write would not translate back, silently converting a CRLF `carry-over.md` to LF end to
    end."""
    with _FileLock(path):
        newline = detect_newline(path)
        try:
            # "utf-8-sig" strips a leading BOM if there is one; `write_text` puts it back from its
            # own sniff of the same file. Reading it as content would double it.
            with open(path, "r", encoding="utf-8-sig") as fh:
                old = fh.read()
        except FileNotFoundError:
            old = ""
        except UnicodeDecodeError:
            # Refuse rather than write a file whose old content we could not read: prepending to
            # something we misread would destroy it just as thoroughly as the truncate did.
            raise
        write_text(path, text + old, newline=newline)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["write", "append", "prepend"])
    ap.add_argument("path")
    ap.add_argument("--allow-empty", action="store_true",
                    help="let `write` replace a file that holds bytes with zero bytes. Meaningless "
                         "for `append`/`prepend`, which cannot lose content to an empty payload.")
    a = ap.parse_args(argv)
    text = sys.stdin.read()
    if a.mode == "write":
        try:
            write_text(a.path, text, allow_empty=a.allow_empty)
        except EmptyWriteRefused as exc:
            # Exit 2: a well-formed request whose SHAPE is refused (the usual argparse-style code). A traceback would say as much, less usefully, to a shell that
            # is already mid-failure -- and the one question in that moment is whether the file
            # survived, so the message leads with the answer.
            print(f"memory_write: {exc}", file=sys.stderr)
            return 2
        return 0
    # Not a refusal: an empty append/prepend changes nothing, by construction. But it means the same
    # failed producer the guard above catches, and a silent exit 0 is exactly how that goes unnoticed
    # until somebody wonders why the entry never showed up.
    if not text:
        print(f"memory_write: warning -- empty stdin, so `{a.mode} {a.path}` changed nothing. "
              f"Did the step that produces the content fail?", file=sys.stderr)
    {"append": append_text, "prepend": prepend_text}[a.mode](a.path, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
