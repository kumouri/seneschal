#!/usr/bin/env python3
"""log_rotation.py — the one door every rotating log under `seneschal/state/logs/` (and, since it takes
no dependency on that directory, anywhere else under `state/`) goes through. Stdlib only.

## The trap this module exists around

A long-running daemon's logs grow without bound unless something rolls them, and the obvious fix —
`logging.handlers.RotatingFileHandler` — is wrong here for a reason that only shows up on Windows:
**a file cannot be renamed while any process holds an open handle to it, unless that handle was
opened with `FILE_SHARE_DELETE`** (POSIX has no such restriction, which is why this trap is invisible
in most tutorials and in every stdlib rotating handler's own test suite, which runs on Linux CI).
Nothing in this tree opens a log with that flag.

The worst case is a log a supervisor hands to a *spawned child*: the daemon opens the file, passes the
descriptor to the child (a web backend, say) as its stdout/stderr, then closes ITS OWN copy — so for
the entire life of that child (days to weeks), the CHILD is the sole holder of the handle, not the
daemon that would want to rotate it. A naive `os.rename`/`RotatingFileHandler` call from the daemon
while that child is alive raises `PermissionError: [WinError 32]`.

So this module has exactly two supported shapes, and conflating them is the bug:

1. **`roll_closed(path, ...)`** — for a log a *spawned child* holds open. The caller must already be
   about to kill that child (a supervision loop's normal respawn/bounce path) and must call this
   *after* the kill, before it spawns a replacement. A roll attempted while the old child might still
   be exiting can race and lose — that is reported, never raised (see below), and the very next
   respawn attempt (seconds later, on the next reconcile pass) tries again with the process by then
   actually gone. Wiring rotation into the supervisor's respawn branch rather than its own timer
   reuses a kill-then-open sequence the supervisor already performs for deploys and crash recovery,
   instead of inventing a second one.
2. **`RotatingAppendLog`** — for a log THIS process holds open itself (a daemon's `--log-file`,
   written via explicit `.write()` calls, never Python's `logging` module). Safe on Windows because
   the object renaming the file is the same object that just closed the only handle to it — there is
   no other holder to race against, except a human/editor that has the file open for reading, which
   is reported exactly like the child-process case: skipped this pass, retried next time. Unlike a
   startup-only "check the size once at boot" scheme, it rolls mid-run, which matters for a daemon
   that runs for weeks between restarts.

## What a failed roll does

**Never raises, never blocks, never takes the thing being logged down with it.** A `PermissionError`
mid-roll means: keep the old (still-growing) file, log one line saying so, and let the next scheduled
check try again. A rotation that requires a live subprocess to be perfectly timed and then crashes the
process it was hoping to help is a strictly worse outcome than a large file.

## Crash safety

The only step that changes what path holds the log's bytes is `os.replace(path, rolled_path)`, which
is atomic on both platforms for a same-directory rename: before it, `path` holds everything; after it,
`rolled_path` does and `path` does not exist. There is no observable window where the log is neither.
Compression happens strictly on the ALREADY-ROLLED file (write `<name>.gz.tmp`, `os.replace` it to
`<name>.gz`, then remove the plain copy) — a crash between those steps leaves the uncompressed roll on
disk, still fully readable, never a half-written `.gz` masquerading as done and never a moment with
zero copies of the data.

## Retention

`keep` follows the usual `0 = never prune` convention: `0` keeps every rolled generation forever, and
a positive integer is the number of most-recent generations kept, oldest deleted first. The default
(`DEFAULT_KEEP`) is a positive number: unlike `carry-over.md` or the other files `state_backup.py`
guards, a rotated log is diagnostic exhaust with no unique content once it has been read — losing an
old generation costs nothing. It is a judgment call, and tunable per caller.

## Naming: timestamp-suffixed, never numerically shifted

Rolls are named with a timestamp suffix, never shifted numerically (`.1`, `.2`, …). Shifting N existing
generations is N renames instead of one, and each of those renames is a fresh chance at the exact same
Windows open-handle trap this module exists around — for files that, by the time they'd be shifted, are
no longer even open. A timestamp suffix needs exactly one `os.replace` per roll, ever.

## No truncating writes

Every mutating step here is `os.replace` or `os.remove`, never a truncating `open(p, "w")` of a
`state/` file — the rule `memory_write.py` and `stateio.py` exist to enforce.

USAGE (manual/one-off, e.g. to force the first roll of an oversized file once its holder is closed):
  python log_rotation.py check --path seneschal/state/logs/presence.log
  python log_rotation.py roll  --path seneschal/state/logs/presence.log [--force] [--keep N] [--no-compress]
"""
from __future__ import annotations

import argparse
import gzip
import os
import sys
import threading
import time

DEFAULT_MAX_BYTES = 25 * 1024 * 1024  # 25 MiB — big enough to hold weeks of a daemon log
DEFAULT_KEEP = 8                      # generations kept before the oldest is deleted; 0 = keep all


class RollOutcome:
    """`rolled` is the only field a caller needs to branch on. `rolled_path` is the pre-compression
    name (even when compression then renamed it again to `.gz`) so a log line can name what happened
    without caring whether compression succeeded. `reason` is set only when `rolled` is False AND the
    file was actually over the trigger — i.e. a real decline, not "nothing to do"."""
    __slots__ = ("rolled", "rolled_path", "reason")

    def __init__(self, rolled: bool, rolled_path: str | None = None, reason: str | None = None):
        self.rolled = rolled
        self.rolled_path = rolled_path
        self.reason = reason


def needs_rotation(path: str, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """True if `path` exists and is over `max_bytes`. A missing/unreadable file needs no rotation —
    this gates a rename attempt, so it fails toward "nothing to do", never toward raising."""
    try:
        return os.path.getsize(path) > max_bytes
    except OSError:
        return False


def _rolled_name(path: str, now: float | None = None) -> str:
    """`<stem>-<YYYYmmdd-HHMMSS><ext>`, disambiguated with a trailing `-N` if that exact second is
    already taken (two rolls of the same file within one second — a test, or a very unlucky pair of
    reconcile passes). Timestamp-suffixed rather than numerically shifted (`.1`, `.2`, ...): shifting
    N existing files on every roll means N renames instead of one, which is N more chances to hit the
    exact Windows trap this module exists to avoid, for files that are no longer even open."""
    stem, ext = os.path.splitext(path)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    candidate = f"{stem}-{ts}{ext}"
    n = 1
    while os.path.exists(candidate) or os.path.exists(candidate + ".gz"):
        candidate = f"{stem}-{ts}-{n}{ext}"
        n += 1
    return candidate


def _generations(log_dir: str, base_name: str) -> list:
    """Every already-rolled generation of `base_name` (plain or `.gz`) in `log_dir`, oldest first —
    the timestamp suffix sorts lexicographically because it's a fixed-width `YYYYmmdd-HHMMSS`."""
    stem, ext = os.path.splitext(base_name)
    prefix = stem + "-"
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []
    out = []
    for name in names:
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        if rest.endswith(ext + ".gz") or rest.endswith(ext):
            out.append(name)
    out.sort()
    return out


def _prune(log_dir: str, base_name: str, keep: int) -> None:
    if keep <= 0:  # 0 = never prune
        return
    names = _generations(log_dir, base_name)
    for name in names[:-keep] if keep < len(names) else []:
        try:
            os.remove(os.path.join(log_dir, name))
        except OSError:
            pass  # a generation that resists deletion is not this pass's problem


def _compress(rolled_path: str) -> str:
    """Gzip `rolled_path` in place via write-temp-then-`os.replace` (never a truncating write to
    either the source or a half-named destination). Returns the path callers should report — the
    `.gz` name on success, the untouched plain name on any failure, because a failed compression must
    never cost the data: the plain rolled file is still there and still fully readable."""
    gz_tmp = rolled_path + ".gz.tmp"
    gz_final = rolled_path + ".gz"
    try:
        with open(rolled_path, "rb") as src, gzip.open(gz_tmp, "wb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(chunk)
        os.replace(gz_tmp, gz_final)
    except OSError:
        try:
            os.remove(gz_tmp)
        except OSError:
            pass
        return rolled_path
    try:
        os.remove(rolled_path)
    except OSError:
        pass  # the .gz landed; a stray plain copy beside it costs disk, not correctness
    return gz_final


def roll_closed(path: str, *, max_bytes: int = DEFAULT_MAX_BYTES, keep: int = DEFAULT_KEEP,
                compress: bool = True, force: bool = False, now: float | None = None) -> RollOutcome:
    """Roll `path` aside, assuming the caller holds no open handle to it right now (or has just
    closed one). Never raises.

    `force=True` skips the size check (the manual one-off command, or a test) but still declines
    gracefully — same as the size-triggered path — if the file doesn't exist or is still held open by
    someone else.

    **On Windows, a rename raises `PermissionError` if ANY process still has the file open** (the
    module docstring's trap). That is caught here and reported as a declined roll, not an error: the
    original file is untouched and still being appended to by whoever holds it, and the next
    scheduled check (an interval-based supervisor loop, or a person running this again) gets another
    try. This is deliberate, not a missing retry loop — see the module docstring for why blocking to
    wait out the holder here would be worse."""
    if not force and not needs_rotation(path, max_bytes):
        return RollOutcome(False)
    rolled = _rolled_name(path, now=now)
    try:
        os.replace(path, rolled)
    except OSError as e:
        return RollOutcome(False, reason=f"{e.__class__.__name__}: {e}")
    reported = _compress(rolled) if compress else rolled
    _prune(os.path.dirname(path) or ".", os.path.basename(path), keep)
    return RollOutcome(True, rolled_path=reported)


class RotatingAppendLog:
    """A single-process-owned, append-mode text log that rolls itself when it exceeds `max_bytes`.

    Use this ONLY for a log this process opens and writes to directly (e.g. via explicit `.write()`
    calls, the way a daemon's `--log-file` does — never Python's `logging` module, which this
    tree does not use for daemon logs). Do NOT use it for a log a *spawned child* holds open via
    redirected stdout/stderr (a supervised web backend, say) — that handle belongs to the
    child, not to this object, and only the child's death makes a rename safe; call `roll_closed`
    directly from the supervisor's own respawn path instead.

    Safe on Windows because this object is the ONLY holder of the handle it renames: check size,
    close its own handle, roll, reopen — all under one lock, so by the time `os.replace` runs, nobody
    (other than a stray external reader) still has the file open. `write()` never raises: a failed
    write, roll, or reopen costs the line, matching every other daemon logger's fail-open contract."""

    def __init__(self, path: str, *, max_bytes: int = DEFAULT_MAX_BYTES, keep: int = DEFAULT_KEEP,
                compress: bool = True, on_event=None):
        self._path = path
        self._max_bytes = max_bytes
        self._keep = keep
        self._compress = compress
        self._on_event = on_event or (lambda _msg: None)
        self._lock = threading.Lock()
        self._fh = self._open()

    def _open(self):
        try:
            d = os.path.dirname(self._path)
            if d:
                os.makedirs(d, exist_ok=True)
            return open(self._path, "a", encoding="utf-8")
        except OSError as e:
            self._on_event(f"! log rotation: could not open {self._path}: {e}")
            return None

    def write(self, line: str) -> None:
        with self._lock:
            self._maybe_roll()
            if self._fh is None:
                self._fh = self._open()
            if self._fh is None:
                return
            try:
                self._fh.write(line)
                self._fh.flush()
            except (OSError, ValueError):  # ValueError: write to a closed handle
                pass

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    def _maybe_roll(self) -> None:
        if self._fh is not None:
            try:
                size = os.fstat(self._fh.fileno()).st_size
            except (OSError, ValueError):
                return
            if size <= self._max_bytes:
                return
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        elif not needs_rotation(self._path, self._max_bytes):
            return
        outcome = roll_closed(self._path, max_bytes=self._max_bytes, keep=self._keep,
                              compress=self._compress, force=True)
        if outcome.rolled:
            self._on_event(f"log rotation: rolled {self._path} -> "
                           f"{os.path.basename(outcome.rolled_path)}")
        elif outcome.reason:
            self._on_event(f"! log rotation: could not roll {self._path}: {outcome.reason}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="report whether a log is over its size trigger; exits 0/1")
    c.add_argument("--path", required=True)
    c.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)

    r = sub.add_parser("roll", help="roll a log aside now, assuming nothing still has it open")
    r.add_argument("--path", required=True)
    r.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    r.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    r.add_argument("--no-compress", action="store_true")
    r.add_argument("--force", action="store_true", help="roll even if under the size trigger")

    args = p.parse_args(argv)
    if args.cmd == "check":
        over = needs_rotation(args.path, args.max_bytes)
        size = os.path.getsize(args.path) if os.path.exists(args.path) else 0
        print(f"{args.path}: {size} bytes, trigger {args.max_bytes} bytes — "
              f"{'OVER' if over else 'under'}")
        return 0 if not over else 1
    outcome = roll_closed(args.path, max_bytes=args.max_bytes, keep=args.keep,
                          compress=not args.no_compress, force=args.force)
    if outcome.rolled:
        print(f"rolled {args.path} -> {outcome.rolled_path}")
        return 0
    if outcome.reason:
        print(f"could not roll {args.path}: {outcome.reason}", file=sys.stderr)
        return 1
    print(f"{args.path}: not over the size trigger, nothing to do (use --force to roll anyway)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
