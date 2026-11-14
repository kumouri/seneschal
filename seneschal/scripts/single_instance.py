#!/usr/bin/env python3
r"""The single-instance rail: an exclusive lock the OPERATING SYSTEM holds, not a heartbeat.

Ported from the private assistant this framework was distilled from, where it closed an incident of
exactly the shape below. `presence.py`'s single-instance check used to be a *heartbeat age* read out of
`state/presence.lock`: load the JSON, compare `heartbeat` against `stale_sec`, and run if it looked
dead. Three things are wrong with that shape:

1. **It is advisory.** A fresh heartbeat is a *claim* a process wrote, not a thing the kernel enforces.
   Check-then-write is TOCTOU: two starts milliseconds apart both read "no live lock" and both proceed.
2. **A heartbeat goes stale while the daemon is very much alive.** The task that beats it is cancelled
   at the START of a graceful wind-down, and closing a wedged warm session can then take minutes. The
   daemon still owns Telegram intake and the warm session the whole time, but to a newcomer (and to the
   watchdog) the lock reads dead 90 s in. A heartbeat also stays FRESH for up to 90 s after a hard
   kill, which would make a supervisor's crash restart wait out the stale window.
3. **Releasing it is a path delete.** `os.remove(lock_path)` removes whatever is there, including a
   lock a DIFFERENT live process wrote a moment ago.

Put together, a watchdog revive that lands during a slow wind-down boots a second daemon, and two
daemons each answer every message.

So this module does not measure anything. It opens a file and takes a **non-blocking exclusive
byte-range lock** on it (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) and holds it open for the
process's whole life. The guarantees that follow are the ones the heartbeat could not give:

* **Atomic.** There is no window between "is it free" and "it is mine" — the kernel answers both at
  once. Two starts in the same millisecond cannot both win.
* **Never stale.** The lock lives on the open handle, so it says "this process exists", not "this
  process wrote a timestamp recently". A daemon spending ten minutes in a wind-down still holds it,
  and a killed one holds it not at all.
* **Self-releasing, and only by its owner.** The OS drops it when the holder exits — cleanly, by crash,
  by `taskkill`, by power cut. One process's release can never free another's claim, because the claim
  is the handle and not the file.

**The lock file is never deleted.** It is only ever a handle to lock; its bytes matter to nobody but a
log line. `release()` unlocks and closes and leaves the file exactly where it is.

**`acquire(wait_sec=…)` is how the re-exec handoff stays covered.** An unsupervised graceful reload
releases immediately before it spawns its successor (Windows) or re-executes in place (POSIX), and the
successor waits, so the handoff is a few milliseconds of contention rather than a race. A waiter that
never wins exits quietly — exactly one daemon survives whatever mix of launchers fired.

**Why the lock byte is at `LOCK_OFFSET`, not byte 0.** Windows byte-range locks are MANDATORY: a locked
region cannot be read by anyone else. Owner bytes (pid + when) are written at offset 0 so a loser can
read who holds it and say so in its one log line; the lock itself is taken on a single byte far past
EOF, which Windows allows and which no reader ever touches. On POSIX `flock` is whole-file and
advisory, so the offset is irrelevant there and costs nothing.

**Who takes which lock** (`seneschal/docs/service-supervision-spec.md`):

* `presence.py` takes `presence.instance.lock` at the top of `main()`, before any side effect.
* `seneschald_supervise.py` takes `seneschald-supervisor.instance.lock`; a second supervisor exits 69.
* `seneschald_revive.py`'s rule 0 probes the supervisor's lock with `is_held` and stands down while a
  live supervisor owns the daemon.

**Stdlib only** (`msvcrt` / `fcntl` ship with CPython). Nothing here raises on an ordinary failure: an
unreadable owner stamp degrades to "unknown holder", never to a crash on the daemon's own startup path.

The advisory `state/presence.lock` is UNCHANGED and still written and beaten exactly as before —
`seneschald-control.ps1`, `seneschald_revive.py`, `setup_doctor.py` and the cockpit all read it. This
module is the gate; that file is still the report.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

#: The lock's own file, beside the advisory `presence.lock` it deliberately does NOT replace.
INSTANCE_LOCK_NAME = "presence.instance.lock"

#: The byte we lock. Far past any plausible owner stamp, for the Windows mandatory-lock reason in the
#: module docstring: the region we lock must be one nobody ever reads.
LOCK_OFFSET = 1 << 30

#: How long a would-be daemon waits for the lock by default. Long enough to cover a graceful re-exec
#: handoff (the predecessor releases and exits in milliseconds) without a stranger hanging about.
DEFAULT_WAIT_SEC = 60.0

#: Gap between acquisition attempts while waiting.
POLL_SEC = 0.25


def instance_lock_path(state_dir: str) -> str:
    return os.path.join(state_dir, INSTANCE_LOCK_NAME)


def _lock_fd(fd: int) -> bool:
    """Take the exclusive lock on `fd`, non-blocking. True iff we got it."""
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            # LK_NBLCK: fail immediately rather than blocking, so the WAIT is ours to schedule (and
            # therefore bounded and loggable) rather than the kernel's to hold us in indefinitely.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False  # held by someone else (EACCES/EAGAIN/EDEADLK all mean the same thing here)


def _unlock_fd(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # closing the fd drops the lock regardless; this is tidiness, not the mechanism


class InstanceLock:
    """One process's exclusive claim on being THE daemon.

    Use it once, near the top of `main()`, before any side effect:

        lock = InstanceLock(instance_lock_path(state_dir))
        if not lock.acquire(wait_sec=60):
            log(f"presence already running ({lock.holder_note()}) — exiting.")
            return 0

    `release()` is idempotent, so the graceful-reload path can release before spawning its successor
    and a `finally` can release again without caring which ran first."""

    def __init__(self, path: str):
        self.path = path
        self._fd: int | None = None
        self._holder: dict | None = None  # whoever held it when our last acquire failed

    # ----------------------------------------------------------------- acquire / release

    def acquire(self, *, wait_sec: float = 0.0, poll_sec: float = POLL_SEC) -> bool:
        """Take the lock, retrying until `wait_sec` elapses. True iff we hold it afterwards.

        `wait_sec <= 0` is a single attempt. Already holding it is True without touching anything —
        re-entrancy must never be able to drop a claim we already have."""
        if self._fd is not None:
            return True
        deadline = time.monotonic() + max(0.0, wait_sec)
        while True:
            if self._try_once():
                return True
            if time.monotonic() >= deadline:
                self._holder = self._read_owner()  # for the caller's one log line
                return False
            time.sleep(max(0.01, poll_sec))

    def _try_once(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        except OSError:
            pass
        try:
            # O_CREAT, never O_TRUNC: truncation would be a write to a file another live process may be
            # holding, and nothing here needs the old bytes gone.
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return False
        if not _lock_fd(fd):
            os.close(fd)
            return False
        self._fd = fd
        self._write_owner()
        return True

    def release(self) -> None:
        """Unlock and close. Idempotent, never raises, and NEVER deletes the file (see the docstring)."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        _unlock_fd(fd)
        try:
            os.close(fd)
        except OSError:
            pass

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    # ----------------------------------------------------------------- owner stamp (diagnostics only)

    def _write_owner(self) -> None:
        """Stamp pid + acquisition time at offset 0, for a loser's log line and for a human reading the
        file. Nothing DECIDES on these bytes — the lock is the decision — so every failure here is
        swallowed: a daemon must never fail to start over a diagnostic."""
        payload = json.dumps({
            "pid": os.getpid(),
            "acquired_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, payload.encode("utf-8") + b"\n")
        except (OSError, TypeError):
            pass

    def _read_owner(self) -> dict | None:
        """Who holds it, read WITHOUT taking the lock. Offset 0 is outside the locked region on purpose,
        so this works on Windows while the lock is held. None = couldn't tell."""
        try:
            with open(self.path, "rb") as fh:
                raw = fh.read(512).split(b"\n", 1)[0]
            owner = json.loads(raw.decode("utf-8"))
            return owner if isinstance(owner, dict) else None
        except Exception:  # noqa: BLE001 — missing, locked, mid-write, malformed: all "couldn't tell"
            return None

    def holder_note(self) -> str:
        """One human clause naming the holder, for the loser's single log line. Never the empty string:
        "an unreadable holder" is still an answer, and a silent exit is the failure mode this whole
        module exists to make impossible."""
        owner = self._holder if self._holder is not None else self._read_owner()
        if not owner:
            return "another instance holds the lock"
        pid = owner.get("pid")
        since = owner.get("acquired_at")
        if pid and since:
            return f"pid {pid} has held the lock since {since}"
        if pid:
            return f"pid {pid} holds the lock"
        return "another instance holds the lock"


# --------------------------------------------------------------------------------------------- probes


def is_held(path: str) -> bool:
    """Does a LIVE process hold this lock right now?

    The honest liveness probe the heartbeat never was, for a watcher that must not launch a second
    daemon or second-guess a live supervisor (`seneschald_revive.decide`'s rule 0). Implemented by trying to take the lock and immediately
    giving it back: success proves nobody held it, failure proves somebody does. The window in which
    this probe itself holds the lock is microseconds and a real daemon start would simply wait through
    it, which is why `acquire`'s default wait exists.

    **Fails CLOSED** — any error reads as "held", i.e. "do not start another one". A probe that cannot
    tell must never be the reason a second brain comes up."""
    try:
        probe = InstanceLock(path)
        if probe.acquire():
            probe.release()
            return False
        return True
    except Exception:  # noqa: BLE001
        return True


def holder(path: str) -> dict:
    """`{"held": bool, "pid": int | None}` — whether a live process holds the lock and, if so, the pid
    its owner stamp names. For `service.py`'s Windows Stop, which kills a supervisor pid only when the
    supervisor's own lock is held by that very pid (a recycled pid can then never be the target). `held` fails CLOSED exactly like `is_held`; `pid` is None whenever
    it is not held or the stamp can't be read (the caller then kills nothing it can't prove is a stray)."""
    held = is_held(path)
    pid = None
    if held:
        owner = InstanceLock(path)._read_owner()
        raw = owner.get("pid") if isinstance(owner, dict) else None
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            pid = raw
    return {"held": held, "pid": pid}
