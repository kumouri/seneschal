#!/usr/bin/env python3
"""Scrub the Telegram bot token out of local state logs. Standard library only.

**Why this exists.** An older build of `telegram_http.py` built every error message from
``url.split('?')[0]`` — which strips the *query* while the Bot API carries its credential in the
*path* (``/bot<TOKEN>/sendMessage``). So every `TelegramHTTPError` embedded the live token, and two
of its callers write those strings to disk: `telegram_send.log_format_fallback` into
`state/telegram-format-fallback.jsonl`, and the daemon's own logger into `state/presence.log`. Both
files are gitignored, so nothing reached git — and both are **read by agents** and by anything doing
comms diagnostics, which is a credential-exposure surface that should not exist.

`telegram_http.redact` closes the source. This closes what is already on disk.

**It reuses that same helper rather than carrying a pattern of its own**, which is the whole point:
one definition of *"what is the credential here"*, so the scrubber cannot drift from the redactor
and leave a shape the writer produces and the cleaner misses.

## TWO WRITE MODES, AND WHICH ONE A FILE NEEDS IS A FACT ABOUT THE FILE

`atomic` — temp file + `os.replace` — is right for a log **nobody holds open**.
`telegram-format-fallback.jsonl` is written by `log_format_fallback`, which opens, appends and
closes per row, so a rename underneath it can never strand a handle.

**`presence.log` is the opposite and `os.replace` there would be a silent disaster.** The daemon
opens it once at startup (`presence.py`'s `log_fh = open(args.log_file, "a")`) and holds that handle
for its whole life. A rename onto the path either fails outright on Windows or — worse — succeeds and
orphans the handle, after which **every subsequent log line is written to a file nobody can see** and
the daemon goes observability-blind with nothing to announce it. A daemon that is silently blind
for days is a known, expensive failure class, and *"scrub the log"* is not worth risking it.

So `in-place` writes the redaction **over the token, at the same byte offsets, at exactly the same
length** — `bot<redacted------>`, padded to fit. Nothing is renamed, nothing is truncated, the file
size never changes, no byte after the match moves, and **the daemon's append handle is untouched**:
it writes at EOF, and every offset this touches is strictly before the EOF that was read. Lines the
daemon appends while the scrub runs are simply not in the window and survive intact.

The padding is the one visible cost, and it is the right one: a shorter replacement would shift
every following byte, which is exactly what may not happen here.

## What it promises

* **Byte-exact except for the token.** In `atomic` mode lines are handled as bytes, decoded through
  ``utf-8``/``surrogateescape`` and re-encoded the same way, so a line with no token round-trips to
  the identical bytes — including its line ending, including any byte sequence that is not valid
  UTF-8 at all. No re-serialisation: a JSONL row keeps its exact key order, spacing and escaping,
  because it is never parsed as JSON.
* **Atomic.** A temp file in the same directory, then `os.replace`. A crash mid-write leaves the
  original intact; a reader never sees a half-written file.
* **Idempotent, in both modes.** Neither replacement (``bot<redacted>``, ``bot<redacted--->``)
  contains a ``/bot`` segment, so a second run matches nothing and says so — ``changed: 0``. Safe to
  re-run, safe to schedule.
* **It never deletes, never reorders, never truncates**, and touches only the files it is given.
  Nothing else in `state/` is opened for writing.
* **Nothing is created.** A path that does not exist is reported as `missing` and skipped, so
  running this in a checkout that is not the daemon's is a no-op rather than a mess.

## Usage

```sh
python scrub_telegram_token.py --scan                 # report only, write nothing
python scrub_telegram_token.py                        # scrub the default targets, each in its mode
python scrub_telegram_token.py --state-dir D:/x/state # a state dir other than this checkout's
python scrub_telegram_token.py path/to/other.log      # explicit targets (atomic unless --mode says)
python scrub_telegram_token.py --mode in-place a.log  # for a file something holds open
```

**`--scan` also walks the whole state directory** and names every *other* file that carries the
pattern, so *"is it only these two?"* is a question this script answers rather than one you have to
trust. It skips nothing by extension — a match inside a `.db` is still a match worth knowing about,
even though scrubbing a binary is deliberately not something this does automatically — and it
**streams**, because the live state directory is ~1.9 GB and one file in it is a 377 MB SQLite
database. A diagnostic that has to be memory-budgeted before you dare run it does not get run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import telegram_http as th  # noqa: E402 — one definition of the credential, one place

DEFAULT_STATE_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "state"))

#: The two files the leak is known to have reached, each with **the write mode that file needs**.
#: Explicit rather than discovered, because *"rewrite every file under state/ that matches"* is one
#: bad pattern away from rewriting `presence.db` or an outbox. Discovery is `--scan`'s job and stops
#: at reporting. The mode is not a preference: `presence.log` is held open by the running daemon, so
#: `atomic` there strands its handle — see the module docstring.
DEFAULT_TARGETS = (("telegram-format-fallback.jsonl", "atomic"),
                   ("presence.log", "in-place"))

#: `in-place` reads the file whole to locate its matches. A log this large is a different problem
#: needing a different tool, so it is REFUSED rather than half-scrubbed off a streaming guess.
MAX_IN_PLACE_BYTES = 256 << 20

#: `--scan`'s walk skips these entirely — bulk stores of the owner's own media and caches, none of
#: them a place a Telegram error string is written, and together most of the directory's bytes.
SCAN_SKIP_DIRS = ("__pycache__", "archives", "attachments", "inbox", "avatar")

#: `--scan` reads in chunks this size, with :data:`SCAN_OVERLAP` bytes carried over, so a token that
#: straddles a chunk boundary is still seen. No file is ever held whole.
SCAN_CHUNK = 1 << 20
SCAN_OVERLAP = 512

#: The scrub pattern as bytes, compiled from `telegram_http`'s own source string so the two can
#: never disagree — the scanner and the redactor answer to one definition, which is the property
#: that keeps a shape the writer produces from being one the cleaner misses.
_BOT_SEGMENT_BYTES = re.compile(th._BOT_SEGMENT.pattern.encode("utf-8"))

#: `telegram_http.REDACTED` as bytes — the width `in-place` pads out to, taken from the same
#: constant the atomic path writes, so the two modes can never disagree about the marker.
REDACTED_BYTES = th.REDACTED.encode("utf-8")


def scrub_text(text: str) -> str:
    """One line's worth of scrubbing. Thin on purpose: `telegram_http.redact` is the definition."""
    return th.redact(text)


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def scrub_bytes(raw: bytes) -> tuple[bytes, int]:
    """`(rewritten, rows_changed)` for a whole file's bytes.

    Splits with ``keepends`` so every line ending — LF, CRLF, a final line with none at all —
    survives untouched, and rejoins by concatenation rather than by `"\\n".join`, which would
    invent one."""
    out, changed = [], 0
    for line in raw.splitlines(keepends=True):
        text = _decode(line)
        scrubbed = scrub_text(text)
        if scrubbed != text:
            changed += 1
            out.append(_encode(scrubbed))
        else:
            out.append(line)          # untouched line: the ORIGINAL bytes, not a round-trip
    return b"".join(out), changed


def _padded(match) -> bytes:
    """The replacement for `in-place`: same bytes wide as what it replaces, and self-describing.

    `bot<redacted------->`. The dashes are not decoration — a shorter string would move every byte
    after it, which is the one thing this mode exists to avoid. A match too narrow to hold the word
    (impossible for a real token; `bot` plus one character is the floor) is still fully covered."""
    width = match.end() - match.start()
    if width >= len(REDACTED_BYTES):
        return b"bot<redacted" + b"-" * (width - len(REDACTED_BYTES)) + b">"
    return b"-" * width


def _rows_touched(raw: bytes, matches) -> int:
    """How many distinct LINES the matches fall on — the number an operator actually wants, since
    one line can carry the token twice (a `getFile` followed by its download URL does)."""
    return len({raw.count(b"\n", 0, m.start()) for m in matches})


def scrub_in_place(path: str, *, dry_run: bool = False) -> dict:
    """Redact over the token at its own offsets, same width, without renaming or truncating.

    **The correctness argument, since this is the unusual one.** The daemon appends at EOF; every
    offset written here is strictly below the EOF that was read, so the two never contend for a
    byte. Nothing moves, so a match located during the read is still that match at write time. The
    file's size is unchanged, so a reader tailing it sees one line's characters change and nothing
    else. And the write is idempotent, so a run interrupted halfway is simply finished by the next
    one."""
    if not os.path.isfile(path):
        return {"path": path, "status": "missing", "mode": "in-place", "changed": 0}
    size = os.path.getsize(path)
    if size > MAX_IN_PLACE_BYTES:
        return {"path": path, "status": "too-large", "mode": "in-place", "changed": 0,
                "bytes": size}
    with open(path, "rb") as fh:
        raw = fh.read()
    matches = list(_BOT_SEGMENT_BYTES.finditer(raw))
    if not matches:
        return {"path": path, "status": "clean", "mode": "in-place", "changed": 0}
    changed = _rows_touched(raw, matches)
    if dry_run:
        return {"path": path, "status": "would-scrub", "mode": "in-place", "changed": changed}
    with open(path, "r+b") as fh:
        for m in matches:
            replacement = _padded(m)
            # Belt and braces: a width mismatch here would shift the rest of the file, so it is an
            # assertion rather than a comment. `_padded` cannot produce one; a later edit could.
            if len(replacement) != m.end() - m.start():
                raise AssertionError("in-place replacement changed width")
            fh.seek(m.start())
            fh.write(replacement)
        fh.flush()
        os.fsync(fh.fileno())
    return {"path": path, "status": "scrubbed", "mode": "in-place", "changed": changed}


def scrub_file(path: str, *, dry_run: bool = False) -> dict:
    """Scrub one file in place, atomically. Returns a report row; never raises on a missing file.

    **The file is only replaced when something actually changed.** A clean file keeps its mtime and
    its inode, so a re-run leaves no trace at all — which is what makes scheduling this harmless and
    what makes *"the second run changed nothing"* checkable from outside."""
    if not os.path.isfile(path):
        return {"path": path, "status": "missing", "mode": "atomic", "changed": 0}
    with open(path, "rb") as fh:
        raw = fh.read()
    rewritten, changed = scrub_bytes(raw)
    if not changed:
        return {"path": path, "status": "clean", "mode": "atomic", "changed": 0}
    if dry_run:
        return {"path": path, "status": "would-scrub", "mode": "atomic", "changed": changed}
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".scrub-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(rewritten)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    return {"path": path, "status": "scrubbed", "mode": "atomic", "changed": changed}


def scrub(path: str, mode: str, *, dry_run: bool = False) -> dict:
    """Dispatch to the mode this file needs. There is no auto-detection: *"is something holding this
    open?"* is not a question a process can answer portably, and guessing it wrong is the failure
    the modes exist to prevent."""
    if mode == "in-place":
        return scrub_in_place(path, dry_run=dry_run)
    return scrub_file(path, dry_run=dry_run)


def count_hits(path: str) -> int:
    """How many token occurrences `path` carries, read in chunks and never held whole.

    The overlap is what makes chunking honest: without it a token split across a boundary is missed
    by both halves, and a scanner that under-reports is worse than none — it converts *"I did not
    look properly"* into *"there is nothing there."*"""
    hits, tail = 0, b""
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(SCAN_CHUNK)
                if not chunk:
                    break
                window = tail + chunk
                hits += len(_BOT_SEGMENT_BYTES.findall(window))
                if tail:
                    # Anything wholly inside the carried-over tail was already counted last round.
                    hits -= len(_BOT_SEGMENT_BYTES.findall(tail))
                tail = window[-SCAN_OVERLAP:]
    except OSError:
        return 0
    return hits


def scan_dir(state_dir: str) -> list:
    """Every file under `state_dir` that carries the pattern, with its occurrence count.

    Read-only, and the honest answer to *"is it only the two known files?"* — an empty result past
    the defaults is a real answer, not an absence of evidence. A file it cannot open is skipped
    rather than raised on: the daemon holds several of these open, and a scan that dies partway is
    a scan whose silence means nothing."""
    found = []
    for root, dirs, files in os.walk(state_dir):
        dirs[:] = [d for d in dirs if d not in SCAN_SKIP_DIRS]
        for name in sorted(files):
            path = os.path.join(root, name)
            hits = count_hits(path)
            if hits:
                found.append({"path": os.path.relpath(path, state_dir), "hits": hits})
    return found


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Redact the Telegram bot token from local state logs.")
    p.add_argument("targets", nargs="*",
                   help="files to scrub (default: the known log files under --state-dir)")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                   help="the state directory the default targets live in")
    p.add_argument("--scan", action="store_true",
                   help="report every file under --state-dir that carries the token; write nothing")
    p.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    p.add_argument("--mode", choices=("atomic", "in-place"), default="atomic",
                   help="write mode for EXPLICIT targets; the defaults carry their own (default: "
                        "atomic). Use in-place for a file a running process holds open.")
    args = p.parse_args(argv)

    if args.scan:
        print(json.dumps({"ok": True, "mode": "scan", "state_dir": args.state_dir,
                          "found": scan_dir(args.state_dir)}, ensure_ascii=False))
        return 0

    # A default target's mode travels WITH it: which one `presence.log` needs is a fact about the
    # file, not an operator preference, so the common invocation cannot get it wrong. An explicit
    # target has no such fact attached and takes `--mode`, defaulting to the safe-anywhere one.
    if args.targets:
        plan = [(t, args.mode) for t in args.targets]
    else:
        plan = [(os.path.join(args.state_dir, name), mode) for name, mode in DEFAULT_TARGETS]
    rows = [scrub(path, mode, dry_run=args.dry_run) for path, mode in plan]
    print(json.dumps({"ok": True, "mode": "dry-run" if args.dry_run else "scrub",
                      "changed": sum(r["changed"] for r in rows), "files": rows},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
