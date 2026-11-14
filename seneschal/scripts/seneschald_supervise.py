#!/usr/bin/env python3
r"""seneschald_supervise — the long-lived process that supervises `presence.py` across reloads and crashes.

Design: `seneschal/docs/service-supervision-spec.md` (a port of the supervisor the private assistant
this framework was distilled from runs). The short of it: Task Scheduler supervises only the FIRST
process of a task instance, its restart-on-failure does not fire on a non-zero exit code at all (it
covers a failure to LAUNCH, nothing more), and every graceful reload used to leave a detached successor
nobody watched. So on Windows the `seneschald` task's process becomes this supervisor, for life, and the
daemon is its child:

    Task Scheduler \seneschald (S4U; or InteractiveToken + pythonw.exe for the no-admin fallback)
      └─ .venv\Scripts\python.exe seneschal\scripts\seneschald_supervise.py --task seneschald  ← for life
           └─ [Job: KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK]
                └─ .venv\Scripts\python.exe seneschal\scripts\presence.py <seneschald.args>  (CREATE_NO_WINDOW)

On Linux and macOS systemd / launchd ARE the supervisor (their unit settings encode the same policy, see
`render_units.py`); this file still runs there by hand (`run-presence.sh` is unchanged) and its POSIX
paths are tested in CI, but no rendered unit starts it.

**The exit-code contract** (`classify`, spec §2). The child runs with `SENESCHALD_SUPERVISED=1`, and only
then does `presence.py` speak it:

    75  EX_TEMPFAIL     reload me      restart at once; not counted; no backoff; nothing else killed
     0  —               stay down      exit 0 too, so the task instance ends (a `shutdown`, or the
                                       daemon's own crash-loop guard)
    69  EX_UNAVAILABLE  lock held      another daemon holds presence.instance.lock: wait for it (poll
                                       5 s), then start — how a supervisor adopts over a daemon a hand
                                       run or the legacy launcher left running
    *   —               crash          backoff 2 s, 10 s, 30 s, 60 s, then 120 s; reset after 10 min of
                                       uptime; MORE than 5 crashes in 15 min writes `seneschald-crashloop`
                                       (the daemon's own give-up sentinel, which the watchdog already
                                       stands down on) and exits 0

**`seneschald-stopped` is checked before every restart**, so a Stop that races a crash restart can never
resurrect the daemon. It is NOT checked before the first start: a boot or logon trigger starts the daemon
even with a stale sentinel (the daemon clears it in `write_lock`), and so does this.

**One kill-on-close job holding only the daemon's launcher** (`KillOnCloseJob`). `SILENT_BREAKAWAY_OK`
means nothing the daemon spawns lands in it, so the cockpit, archon sites, detached jobs and `claude`
sessions keep exactly the lifetimes they have today, while the supervisor's own death — a
`schtasks /End`, a crash, a `taskkill` — takes the launcher with it and, through the venv launcher's own
job, its interpreter. The supervisor therefore can never orphan a daemon.

**Its own kernel lock** (`state/seneschald-supervisor.instance.lock`, `single_instance.InstanceLock`): a
second supervisor exits 69 on one line, before any side effect.

**Its own report** (`state/seneschald-supervisor.json`, written atomically through `stateio`): pid,
`started_at`, a heartbeat, the child pid, the last exit and why, the crash budget, restart counts, and the
sha256 of its own source and of the args file at boot. `presence.lock` remains the daemon's report.

**The child log** (`state/logs/seneschald-child.log`): the child's stdout and stderr, piped through this
process into a `log_rotation.RotatingAppendLog` capped at `CHILD_LOG_MAX_BYTES` with one rotated
generation, plus one line per supervisor decision. It catches what the daemon's own `--log-file` cannot —
an import-time `SyntaxError` or a traceback before that log exists, which in session 0 went nowhere.

**The env scrub** (`child_env`): `ANTHROPIC_API_KEY` removed (subscription billing — what
`run-presence.cmd` does, here in tested Python), `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`, cwd = the
repo root. `presence.child_env` stays the second layer, and `presence.py` pops `SENESCHALD_SUPERVISED`
from its own environment at boot so nothing it spawns ever inherits it.

**`seneschald.args`** is the daemon's argument list, one flag per line, read at EVERY child start — so a
knob change deploys on the next graceful reload instead of needing a hard restart. The tracked template
sits beside this file; `/setup daemon` renders a local copy with the owner's model dials into
`seneschal/state/setup/seneschald.args` and the task passes it with `--args-file`. An unreadable file
reuses the last good list (or, at the very first start, the daemon's own defaults) and says so in the
child log; it never stops supervision.

**Self-upgrade.** On every child exit 75 the boot hash of this file (and of the three repo modules it
imports) is compared with what is on disk. When they differ:

* **Windows, with `--task NAME`**: start a one-shot helper (`--upgrade-helper`, `CREATE_NO_WINDOW`,
  outside the job) that waits for this process and its launcher to exit and then runs
  `schtasks /Run /TN NAME` (the task relaunches in well under a second under `IgnoreNew`), retrying while
  no supervisor holds the lock; then exit 0. If the helper fails, the watchdog's backstop starts the task
  within a cycle.
* **POSIX**: `os.execv` this same file — same PID, the lock is re-taken by the new image.
* **Windows without `--task`** (a hand run from a terminal): log the change once and keep supervising on
  the old code. A hand run must never start the scheduled task behind its operator's back.

**More than one service.** `--name` picks a row of `SERVICES`: the supervised script, its args file, the
instance lock its child holds (what an exit 69 waits on) and whether the crash budget may give up. Every
file a supervisor owns is named after it (`Paths`), so two supervisors on one host never share a lock, a
report, a log or a sentinel. Seneschal ships one row, `seneschald`.

**Stdlib only** — it must be able to run before `uv sync` has ever succeeded. It holds no network and no
state beyond its report, lock and log.

Usage:
    python seneschald_supervise.py [--task seneschald] [--state-dir DIR] [--args-file FILE]
    python seneschald_supervise.py [--name NAME] --print-args   # the args file, resolved, one per line
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:  # the repo modules below, whatever the caller's cwd
    sys.path.insert(0, HERE)

import log_rotation  # noqa: E402
import single_instance  # noqa: E402
import stateio  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DEFAULT_STATE_DIR = os.path.join(REPO_ROOT, "seneschal", "state")
DEFAULT_ARGS_FILE = os.path.join(HERE, "seneschald.args")
DEFAULT_SCRIPT = os.path.join(HERE, "presence.py")
DEFAULT_NAME = "seneschald"


@dataclass(frozen=True)
class Service:
    """One supervised service: its script and args file (both beside this file), the
    instance lock its child holds in the state dir (what an exit 69 waits on), and whether the crash
    budget may give up."""
    name: str
    script: str
    args_file: str
    child_lock: str
    give_up: bool = True


SERVICES = {
    "seneschald": Service("seneschald", "presence.py", "seneschald.args", "presence.instance.lock", True),
}


def service_for(name: str) -> Service:
    """The row for `name`. A name with no row gets the daemon's script, args and lock (how a hand-run or
    test supervisor with a custom name always behaved), under its own file names."""
    known = SERVICES.get(name)
    if known is not None:
        return known
    base = SERVICES[DEFAULT_NAME]
    return Service(name, base.script, base.args_file, base.child_lock, base.give_up)

# ------------------------------------------------------------------------------------------ the contract
EXIT_STAY_DOWN = 0
EXIT_LOCK_HELD = 69   # EX_UNAVAILABLE
EXIT_RELOAD = 75      # EX_TEMPFAIL

KIND_RELOAD = "reload"
KIND_STAY_DOWN = "stay-down"
KIND_LOCK_HELD = "lock-held"
KIND_CRASH = "crash"

#: Crash backoff, in order; the last value repeats. Spec §2.
BACKOFF_SEC = (2, 10, 30, 60, 120)
#: A child that stayed up this long has proved itself: the backoff starts over from the first step.
BACKOFF_RESET_UPTIME_SEC = 10 * 60
#: MORE than this many crashes inside the window → give up (crash-loop sentinel, exit 0).
CRASH_BUDGET = 5
CRASH_WINDOW_SEC = 15 * 60
#: A belt the spec did not name: a child that exits 75 this many times inside the window is not
#: reloading, it is spinning (a reload that fails before `write_lock` never reaches the daemon's own
#: boot guard). Past it, a 75 is counted as a crash, so backoff and the budget apply. No real reload
#: cadence comes near it: one per merge, and the daemon's own guard trips at 4 boots in 5 min.
RELOAD_STORM_MAX = 30
RELOAD_STORM_WINDOW_SEC = 10 * 60

#: While another daemon holds the lock (exit 69), how often to look again.
LOCK_WAIT_POLL_SEC = 5.0
#: How often the report's heartbeat is rewritten while a child runs.
HEARTBEAT_SEC = 30.0
#: How long a hand-run Ctrl+C waits for the child before killing it.
TERMINATE_WAIT_SEC = 10.0
#: After the child exits, how long its output pump gets to drain the last lines before moving on.
PUMP_DRAIN_SEC = 5.0

CHILD_LOG_MAX_BYTES = 10 * 1024 * 1024
CHILD_LOG_KEEP = 1  # size-capped with one rotation

SUPERVISED_ENV = "SENESCHALD_SUPERVISED"
#: Removed from the child's environment, always. Billing safety (run-presence.cmd's own scrub).
SCRUBBED_ENV = ("ANTHROPIC_API_KEY",)
FORCED_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

#: The repo modules this file imports. Their bytes are part of the "has the supervisor changed" hash,
#: because a change to any of them is a change to what this running process is executing.
_HASHED_SOURCES = ("seneschald_supervise.py", "single_instance.py", "stateio.py", "log_rotation.py")

# Windows process-creation flags (subprocess exposes some only on Windows, so spelled out here).
CREATE_NO_WINDOW = 0x08000000

UPGRADE_PARENT_WAIT_SEC = 15.0
UPGRADE_CONFIRM_TRIES = 3
UPGRADE_CONFIRM_SEC = 5.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------------------------------------------------------------------------ names
@dataclass(frozen=True)
class Paths:
    """Every file a supervisor named `name` owns, under one state dir. `seneschald` yields exactly the
    names the watchdog and the control script read (`seneschald-stopped`, `seneschald-crashloop`); a
    second supervised service would get its own set by name."""
    state_dir: str
    name: str = DEFAULT_NAME

    @property
    def lock(self) -> str:
        return os.path.join(self.state_dir, f"{self.name}-supervisor.instance.lock")

    @property
    def report(self) -> str:
        return os.path.join(self.state_dir, f"{self.name}-supervisor.json")

    @property
    def child_log(self) -> str:
        return os.path.join(self.state_dir, "logs", f"{self.name}-child.log")

    @property
    def stopped(self) -> str:
        return os.path.join(self.state_dir, f"{self.name}-stopped")

    @property
    def crashloop(self) -> str:
        return os.path.join(self.state_dir, f"{self.name}-crashloop")


# ------------------------------------------------------------------------------------------ pure policy
def classify(code) -> str:
    """The exit-code contract (spec §2). Anything that is not 75, 0 or 69 — a `return 1`, an unhandled
    exception, an access violation, a negative POSIX signal code, None — is a crash."""
    if code == EXIT_RELOAD:
        return KIND_RELOAD
    if code == EXIT_STAY_DOWN:
        return KIND_STAY_DOWN
    if code == EXIT_LOCK_HELD:
        return KIND_LOCK_HELD
    return KIND_CRASH


ACTION_RESTART = "restart"
ACTION_WAIT_LOCK = "wait-lock"
ACTION_STOP = "stop"
ACTION_GIVE_UP = "give-up"


@dataclass
class Decision:
    action: str
    kind: str
    delay: float = 0.0
    reason: str = ""


class Policy:
    """What to do after a child exits. Pure: the caller supplies the clock (a monotonic `now`), so every
    rule here is tested without sleeping. Holds the crash and reload history of one supervisor's life.

    `budget=None` never gives up: every crash restarts with backoff, which stays at its last step (the
    policy a service with nothing to hand a give-up to would want)."""

    def __init__(self, *, backoff=BACKOFF_SEC, reset_uptime=BACKOFF_RESET_UPTIME_SEC,
                 budget=CRASH_BUDGET, window=CRASH_WINDOW_SEC,
                 storm_max=RELOAD_STORM_MAX, storm_window=RELOAD_STORM_WINDOW_SEC):
        self.backoff = tuple(backoff)
        self.reset_uptime = reset_uptime
        self.budget = budget
        self.window = window
        self.storm_max = storm_max
        self.storm_window = storm_window
        self.crashes: list[float] = []
        self.reloads: list[float] = []
        self.consecutive = 0     # crashes since the last long-enough run: indexes the backoff
        self.reload_count = 0    # lifetime totals, for the report
        self.crash_count = 0

    def budget_left(self, now: float) -> int | None:
        if self.budget is None:
            return None
        recent = [t for t in self.crashes if now - t < self.window]
        return max(0, self.budget - len(recent))

    def on_exit(self, code, uptime: float, now: float) -> Decision:
        kind = classify(code)
        if uptime >= self.reset_uptime:
            self.consecutive = 0  # it proved itself; whatever comes next starts the backoff over
        if kind == KIND_STAY_DOWN:
            return Decision(ACTION_STOP, kind, reason="the daemon exited 0: stay down")
        if kind == KIND_LOCK_HELD:
            return Decision(ACTION_WAIT_LOCK, kind,
                            reason="another daemon holds the single-instance lock: waiting for it")
        if kind == KIND_RELOAD:
            self.reloads = [t for t in self.reloads if now - t < self.storm_window] + [now]
            if len(self.reloads) <= self.storm_max:
                self.reload_count += 1
                return Decision(ACTION_RESTART, kind, 0.0, "graceful reload: restarting at once")
            kind = KIND_CRASH
            storm = (f"reload storm: {len(self.reloads)} reloads in "
                     f"{self.storm_window / 60:g} min, counted as a crash")
        else:
            storm = ""
        # A crash (or a reload storm, counted as one).
        self.crash_count += 1
        self.crashes = [t for t in self.crashes if now - t < self.window] + [now]
        if self.budget is not None and len(self.crashes) > self.budget:
            return Decision(ACTION_GIVE_UP, kind, reason=(
                f"{len(self.crashes)} crashes in {self.window / 60:g} min (budget {self.budget}): "
                "giving up"))
        delay = float(self.backoff[min(self.consecutive, len(self.backoff) - 1)])
        self.consecutive += 1
        why = storm or f"crash (exit {code})"
        left = self.budget_left(now)
        budget = "no crash budget: it never gives up" if left is None else f"{left} left in budget"
        return Decision(ACTION_RESTART, kind, delay, f"{why}: restarting in {delay:g} s ({budget})")


# ------------------------------------------------------------------------------------------ args + env
class ArgsFileError(ValueError):
    pass


REPO_TOKEN = "{repo}"


def parse_args_text(text: str, repo_root: str = REPO_ROOT) -> list:
    """`seneschald.args` → argv. One flag per line; a flag's value rides on the same line after the first
    run of whitespace, VERBATIM (so `--watch-prompt Run Watch mode …` keeps its spaces and needs no
    quoting). Blank lines and `#` comments are skipped. A line that does not start with `-` is refused:
    a value on its own line is how a flag silently loses its argument. A value that STARTS with `{repo}`
    becomes an absolute, normalised path under `repo_root` — the `.cmd` passed absolute paths, and a
    relative one would break the moment any child ran from another directory."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("-"):
            raise ArgsFileError(f"line {n}: {line[:40]!r} is not a flag (a value belongs on its flag's line)")
        parts = line.split(None, 1)
        out.append(parts[0])
        if len(parts) == 2:
            value = parts[1].strip()
            if value.startswith(REPO_TOKEN):
                value = os.path.normpath(os.path.join(repo_root, value[len(REPO_TOKEN):].lstrip("/\\")))
            out.append(value)
    return out


def read_args(path: str, repo_root: str = REPO_ROOT) -> list:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return parse_args_text(fh.read(), repo_root)


def child_env(base: dict | None = None) -> dict:
    """The child's environment: the scrub `run-presence.cmd` used to do, plus the supervised marker."""
    env = dict(os.environ if base is None else base)
    for key in SCRUBBED_ENV:
        env.pop(key, None)
    env.update(FORCED_ENV)
    env[SUPERVISED_ENV] = "1"
    return env


def default_python(repo_root: str = REPO_ROOT) -> str:
    """The uv venv interpreter when it exists (it carries the cockpit extras), else this interpreter."""
    venv = (os.path.join(repo_root, ".venv", "Scripts", "python.exe") if os.name == "nt"
            else os.path.join(repo_root, ".venv", "bin", "python"))
    return venv if os.path.exists(venv) else sys.executable


def sha256_of(paths) -> str | None:
    h = hashlib.sha256()
    try:
        for p in paths:
            with open(p, "rb") as fh:
                h.update(os.path.basename(p).encode() + b"\0" + fh.read() + b"\0")
    except OSError:
        return None
    return h.hexdigest()


def source_hash() -> str | None:
    return sha256_of(os.path.join(HERE, name) for name in _HASHED_SOURCES)


# ------------------------------------------------------------------------------------------ Windows job
class KillOnCloseJob:
    """A Job Object with KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK, held for this process's whole life.

    Only the daemon's launcher is ever assigned. Silent breakaway keeps everything the daemon spawns out
    of it; kill-on-close means the handle closing — this process exiting by any route — terminates the
    launcher, whose own job then takes the interpreter (measured on Windows 10: the venv launcher holds its interpreter in its own job). `create()` returns None off
    Windows or when the kernel refuses; the caller logs that and supervises without it."""

    JobObjectExtendedLimitInformation = 9
    KILL_ON_JOB_CLOSE = 0x00002000
    SILENT_BREAKAWAY_OK = 0x00001000
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    def __init__(self, handle, k32):
        self._handle = handle
        self._k32 = k32

    @classmethod
    def create(cls) -> "KillOnCloseJob | None":
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class EXTENDED(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = ctypes.c_void_p
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                    wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return None
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = cls.KILL_ON_JOB_CLOSE | cls.SILENT_BREAKAWAY_OK
            if not k32.SetInformationJobObject(job, cls.JobObjectExtendedLimitInformation,
                                               ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(job)
                return None
            return cls(job, k32)
        except Exception:  # noqa: BLE001 — no job is a logged degradation, never a refusal to supervise
            return None

    def assign(self, pid: int) -> bool:
        try:
            h = self._k32.OpenProcess(self.PROCESS_SET_QUOTA | self.PROCESS_TERMINATE, False, int(pid))
            if not h:
                return False
            try:
                return bool(self._k32.AssignProcessToJobObject(self._handle, h))
            finally:
                self._k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        """Closing the last handle kills every process in the job. Only tests call this; in production
        the handle closes when the process ends, however it ends."""
        handle, self._handle = self._handle, None
        if handle:
            try:
                self._k32.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------------------------------ the supervisor
class Supervisor:
    """One supervisor's life: take the lock, start the child, act on each exit until told to stop.

    Every side effect is a method or an injected callable, so tests drive it with fake children and a
    fake clock. `run()` returns the supervisor's own exit code."""

    def __init__(self, *, state_dir: str = DEFAULT_STATE_DIR, name: str = DEFAULT_NAME,
                 script: str | None = None, args_file: str | None = None,
                 python: str | None = None, cwd: str = REPO_ROOT, task: str | None = None,
                 child_lock: str | None = None, policy: Policy | None = None,
                 sleep=time.sleep, monotonic=time.monotonic, lock_poll_sec: float = LOCK_WAIT_POLL_SEC,
                 heartbeat_sec: float = HEARTBEAT_SEC, base_env: dict | None = None,
                 use_job: bool = True, upgrade=None, source_hash_fn=source_hash,
                 cli_argv: list | None = None):
        self.service = service_for(name)
        self.paths = Paths(state_dir, name)
        self.name = name
        self.script = os.path.abspath(script or os.path.join(HERE, self.service.script))
        self.args_file = args_file or os.path.join(HERE, self.service.args_file)
        self.python = python or default_python()
        self.cwd = cwd
        self.task = task
        self.child_lock = child_lock or os.path.join(state_dir, self.service.child_lock)
        self.policy = policy or Policy(budget=CRASH_BUDGET if self.service.give_up else None)
        self.sleep = sleep
        self.monotonic = monotonic
        self.lock_poll_sec = lock_poll_sec
        self.heartbeat_sec = heartbeat_sec
        self.base_env = base_env
        self.use_job = use_job
        self._upgrade = upgrade          # test seam: replaces the real self-upgrade
        self._source_hash_fn = source_hash_fn
        self.cli_argv = list(sys.argv[1:] if cli_argv is None else cli_argv)  # for the POSIX re-exec
        self.lock = single_instance.InstanceLock(self.paths.lock)
        self.job: KillOnCloseJob | None = None
        self.log_fh: log_rotation.RotatingAppendLog | None = None
        self.proc: subprocess.Popen | None = None
        self.last_args: list | None = None
        self.upgrade_noted = False
        self.report: dict = {}

    # --------------------------------------------------------------------------------- logging + report
    def say(self, msg: str) -> None:
        """One supervisor decision line, into the child log (and stdout for a hand run)."""
        line = f"[{_now_iso()}] [supervisor] {msg}"
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass
        if self.log_fh is not None:
            self.log_fh.write(line + "\n")

    def write_report(self, **fields) -> None:
        self.report.update(fields)
        self.report["heartbeat"] = _now_iso()
        self.report["crash_budget"] = {
            "max": self.policy.budget, "window_sec": self.policy.window,
            "left": self.policy.budget_left(self.monotonic())}
        self.report["restarts"] = {"reload": self.policy.reload_count, "crash": self.policy.crash_count}
        try:
            stateio.write_json_atomic(self.paths.report, self.report)
        except Exception:  # noqa: BLE001 — the report is a report; it must never stop supervision
            pass

    # --------------------------------------------------------------------------------- the child
    def child_argv(self) -> list:
        try:
            args = read_args(self.args_file, self.cwd)
            self.last_args = args
        except (OSError, ArgsFileError) as e:
            if self.last_args is not None:
                self.say(f"! {os.path.basename(self.args_file)} unreadable ({e}); reusing the last good args")
                args = self.last_args
            else:
                self.say(f"! {os.path.basename(self.args_file)} unreadable ({e}); starting with the "
                         "daemon's own defaults")
                args = []
        return [self.python, self.script] + list(args)

    def spawn(self) -> subprocess.Popen:
        argv = self.child_argv()
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        proc = subprocess.Popen(argv, cwd=self.cwd, env=child_env(self.base_env),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, close_fds=True, **kwargs)
        if self.job is not None and not self.job.assign(proc.pid):
            self.say(f"! could not place pid {proc.pid} in the kill-on-close job; a supervisor death "
                     "would orphan it until the watchdog's next cycle")
        pump = threading.Thread(target=self._pump, args=(proc,), name="child-log", daemon=True)
        pump.start()
        proc.seneschald_pump = pump
        return proc

    def finish_pump(self, proc) -> None:
        """After the child exits, give the pump `PUMP_DRAIN_SEC` to copy the last lines. If EOF has not
        come by then, something the child started still holds the pipe's write end — leave that pump
        reading in the background (it costs a thread until that writer goes) rather than close the pipe
        under a live writer, which would turn its next print into a broken-pipe error."""
        pump = getattr(proc, "seneschald_pump", None)
        if pump is None:
            return
        pump.join(timeout=PUMP_DRAIN_SEC)
        if pump.is_alive():
            self.say("! the child's output pipe is still held open by a process it started; "
                     "leaving it drained in the background")

    def _pump(self, proc) -> None:
        """Copy the child's merged stdout/stderr into the child log, line by line, until EOF — which is
        the moment every writer is gone, so the pump is the one place that may close the read end."""
        try:
            for raw in iter(proc.stdout.readline, b""):
                if self.log_fh is not None:
                    self.log_fh.write(raw.decode("utf-8", errors="replace").rstrip("\r\n") + "\n")
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass

    def wait_child(self, proc) -> int:
        """Block until the child exits, rewriting the heartbeat while it runs."""
        while True:
            try:
                return proc.wait(timeout=self.heartbeat_sec)
            except subprocess.TimeoutExpired:
                self.write_report()

    # --------------------------------------------------------------------------------- waits
    def stopped(self) -> bool:
        return os.path.exists(self.paths.stopped)

    def wait_for_lock(self) -> bool:
        """Exit 69: another daemon holds the instance lock. Poll until it is free. False = a deliberate
        stop arrived while waiting, so do not start anything."""
        while single_instance.is_held(self.child_lock):
            if self.stopped():
                return False
            self.write_report(state="waiting-lock")
            self.sleep(self.lock_poll_sec)
        return True

    # --------------------------------------------------------------------------------- give up
    def give_up(self, reason: str) -> None:
        """The same sentinel the daemon's own crash-loop guard writes, in the same shape, so there is one
        give-up state and one alert path (the watchdog's), never two."""
        now = datetime.now(timezone.utc)
        stamps = [now.isoformat().replace("+00:00", "Z")]
        try:
            stateio.write_json_atomic(self.paths.crashloop, {
                "tripped_at": stamps[0],
                "boots_in_window": len(self.policy.crashes),
                "window_min": self.policy.window / 60,
                "boots": stamps,
                "by": "supervisor",
                "reason": reason,
            })
        except Exception as e:  # noqa: BLE001
            self.say(f"! could not write {os.path.basename(self.paths.crashloop)}: {e}")
        self.say(f"GIVING UP — {reason}. Wrote {os.path.basename(self.paths.crashloop)}; the watchdog "
                 "stands down and alerts. `seneschald start` (or Start-Seneschald) clears it.")

    # --------------------------------------------------------------------------------- self-upgrade
    def upgrade_pending(self) -> bool:
        current = self._source_hash_fn()
        boot = self.report.get("source_sha256")
        return bool(current and boot and current != boot)

    def reexec_argv(self) -> list:
        """POSIX self-upgrade: this same file, with the flags this supervisor was started with."""
        return [sys.executable, os.path.abspath(__file__)] + list(self.cli_argv)

    def helper_argv(self) -> list:
        """The one-shot upgrade helper's command line: wait for this process, then for its parent (the
        venv launcher when the task runs us), then `schtasks /Run` this task."""
        return [sys.executable, os.path.abspath(__file__), "--upgrade-helper", "--task", str(self.task),
                "--state-dir", self.paths.state_dir, "--name", self.name,
                "--wait-pid", str(os.getpid()), "--wait-pid", str(os.getppid())]

    def upgrade(self) -> bool:
        """Hand over to the changed code. True = this process should now exit 0 (a successor is coming).
        False = keep supervising on the old code."""
        if self._upgrade is not None:
            return bool(self._upgrade(self))
        if os.name != "nt":
            self.say("supervisor source changed — re-executing in place (same pid)")
            self.lock.release()
            if self.log_fh is not None:
                self.log_fh.close()
            argv = self.reexec_argv()
            os.execv(argv[0], argv)
            return True  # not reached
        if not self.task:
            if not self.upgrade_noted:
                self.upgrade_noted = True
                self.say("supervisor source changed on disk; no --task was given (a hand run), so it keeps "
                         "running the old code until it is restarted by hand")
            return False
        argv = self.helper_argv()
        try:
            subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            self.say(f"! supervisor source changed but the upgrade helper would not start ({e}); "
                     "keeping the old code")
            return False
        self.say(f"supervisor source changed — handing over: the helper will run schtasks /Run "
                 f"/TN {self.task} once this process has exited")
        return True

    # --------------------------------------------------------------------------------- the loop
    def run(self) -> int:
        if not self.lock.acquire():
            print(f"[{_now_iso()}] [supervisor] {self.name} supervisor already running — "
                  f"{self.lock.holder_note()}; exiting.", flush=True)
            return EXIT_LOCK_HELD
        try:
            return self._run_locked()
        finally:
            if self.log_fh is not None:
                self.log_fh.close()
            self.lock.release()

    def _run_locked(self) -> int:
        self.log_fh = log_rotation.RotatingAppendLog(
            self.paths.child_log, max_bytes=CHILD_LOG_MAX_BYTES, keep=CHILD_LOG_KEEP, compress=False)
        if self.use_job:
            self.job = KillOnCloseJob.create()
            if self.job is None and os.name == "nt":
                self.say("! no kill-on-close job (the kernel refused); supervising without one")
        self.report = {
            "name": self.name, "pid": os.getpid(), "started_at": _now_iso(),
            "source_sha256": self._source_hash_fn(),
            "args_sha256": sha256_of([self.args_file]),
            "task": self.task, "job": self.job is not None,
            "child_pid": None, "child_started_at": None, "last_exit": None, "state": "starting",
        }
        self.write_report()
        self.say(f"supervisor up (pid {os.getpid()}), supervising {os.path.basename(self.script)}")
        first = True
        while True:
            if not first and self.stopped():
                self.say(f"{os.path.basename(self.paths.stopped)} present — not restarting; exiting 0")
                self.write_report(state="stopped", child_pid=None)
                return EXIT_STAY_DOWN
            first = False
            started = self.monotonic()
            try:
                self.proc = self.spawn()
            except OSError as e:
                # A launch failure is a crash like any other: backoff and the budget apply.
                self.say(f"! could not start the child: {e}")
                code = None
            else:
                self.say(f"started {os.path.basename(self.script)} (pid {self.proc.pid})")
                self.write_report(state="running", child_pid=self.proc.pid, child_started_at=_now_iso(),
                                  child_args_sha256=sha256_of([self.args_file]))
                try:
                    code = self.wait_child(self.proc)
                except KeyboardInterrupt:
                    return self._interrupted()
                self.finish_pump(self.proc)
            uptime = self.monotonic() - started
            decision = self.policy.on_exit(code, uptime, self.monotonic())
            self.say(f"child exited {code} after {uptime:.0f} s — {decision.kind}: {decision.reason}")
            self.write_report(child_pid=None, last_exit={
                "code": code, "kind": decision.kind, "at": _now_iso(), "uptime_sec": round(uptime, 1),
                "action": decision.action, "reason": decision.reason})
            if decision.kind == KIND_RELOAD and decision.action == ACTION_RESTART and self.upgrade_pending():
                if self.upgrade():
                    self.write_report(state="upgrading")
                    return EXIT_STAY_DOWN
            if decision.action == ACTION_STOP:
                self.write_report(state="stopped")
                return EXIT_STAY_DOWN
            if decision.action == ACTION_GIVE_UP:
                self.give_up(decision.reason)
                self.write_report(state="gave-up")
                return EXIT_STAY_DOWN
            if decision.action == ACTION_WAIT_LOCK:
                self.wait_for_lock()  # False = stopped while waiting; the loop top sees it and exits
                continue
            if decision.delay:
                self.write_report(state="backoff")
                self.sleep(decision.delay)

    def _interrupted(self) -> int:
        """Ctrl+C on a hand run. The child has its own hidden console, so it never saw the signal."""
        self.say("interrupted — stopping the child")
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=TERMINATE_WAIT_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.write_report(state="stopped", child_pid=None)
        return EXIT_STAY_DOWN


# ------------------------------------------------------------------------------------------ upgrade helper
def _wait_pid_exit(pid: int, timeout_sec: float) -> None:
    """Block until `pid` has exited or `timeout_sec` passes. A pid we cannot open is treated as gone."""
    if os.name != "nt":
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.2)
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not h:
            return
        try:
            k32.WaitForSingleObject(h, int(timeout_sec * 1000))
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return


def upgrade_helper(task: str, state_dir: str, name: str, wait_pids: list, *,
                   run=None, held=None, sleep=time.sleep) -> int:
    """The one-shot that relaunches the task once the old supervisor is gone. The first
    pid is the supervisor itself (waited on fully); the rest — its venv launcher, or cmd.exe for a run
    that was not the task's — are given `UPGRADE_PARENT_WAIT_SEC`, because `IgnoreNew` drops a `/Run`
    while the old instance is still running. Then `schtasks /Run`, confirmed by a supervisor lock holder
    appearing, retried a bounded number of times."""
    paths = Paths(state_dir, name)
    run = run or (lambda argv: subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW
                                              if os.name == "nt" else 0).returncode)
    held = held or (lambda: single_instance.is_held(paths.lock))
    log = log_rotation.RotatingAppendLog(paths.child_log, max_bytes=CHILD_LOG_MAX_BYTES,
                                         keep=CHILD_LOG_KEEP, compress=False)
    try:
        for i, pid in enumerate(wait_pids):
            _wait_pid_exit(int(pid), 24 * 3600 if i == 0 else UPGRADE_PARENT_WAIT_SEC)
        for attempt in range(1, UPGRADE_CONFIRM_TRIES + 1):
            rc = run(["schtasks", "/Run", "/TN", task])
            sleep(UPGRADE_CONFIRM_SEC)
            if held():
                log.write(f"[{_now_iso()}] [upgrade-helper] schtasks /Run /TN {task}: the new supervisor "
                          f"holds its lock (attempt {attempt})\n")
                return 0
            log.write(f"[{_now_iso()}] [upgrade-helper] schtasks /Run /TN {task} returned {rc}; no "
                      f"supervisor yet (attempt {attempt}/{UPGRADE_CONFIRM_TRIES})\n")
        log.write(f"[{_now_iso()}] [upgrade-helper] gave up; the watchdog's revive is the backstop\n")
        return 1
    finally:
        log.close()


# ------------------------------------------------------------------------------------------ CLI
def scrub_own_env() -> None:
    """The supervisor's own process gets the same scrub as its child, so nothing it ever runs bills the
    metered API either."""
    for key in SCRUBBED_ENV:
        os.environ.pop(key, None)
    os.environ.update(FORCED_ENV)
    os.environ.pop(SUPERVISED_ENV, None)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Supervise presence.py (or another SERVICES row) across "
                                            "reloads and crashes (seneschal/docs/service-supervision-spec.md).")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    p.add_argument("--name", default=DEFAULT_NAME,
                   help="the service (a SERVICES row: seneschald); also names the lock, report, child "
                        "log and sentinels (default seneschald)")
    p.add_argument("--script", default=None,
                   help="the supervised script (default: the service's; presence.py for seneschald)")
    p.add_argument("--args-file", default=None,
                   help="one flag per line (default: the service's; seneschald.args for seneschald)")
    p.add_argument("--python", default=None, help="the child's interpreter (default: the repo .venv's)")
    p.add_argument("--task", default=None,
                   help="the scheduled task this supervisor IS; enables the Windows self-upgrade handoff")
    p.add_argument("--print-args", action="store_true", help="print the resolved child argv and exit")
    p.add_argument("--upgrade-helper", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--wait-pid", action="append", default=[], help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    a = build_parser().parse_args(argv)
    if a.upgrade_helper:
        if not a.task:
            return 2
        return upgrade_helper(a.task, a.state_dir, a.name, a.wait_pid)
    if a.print_args:
        for item in read_args(a.args_file or os.path.join(HERE, service_for(a.name).args_file)):
            print(item)
        return 0
    scrub_own_env()
    try:
        os.chdir(REPO_ROOT)
    except OSError:
        pass
    sup = Supervisor(state_dir=a.state_dir, name=a.name, script=a.script, args_file=a.args_file,
                     python=a.python, task=a.task, cli_argv=argv)
    try:
        return sup.run()
    except KeyboardInterrupt:
        return EXIT_STAY_DOWN


if __name__ == "__main__":
    sys.exit(main())
