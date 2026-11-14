#!/usr/bin/env python3
r"""service — the `seneschald` verbs, one CLI over the three platforms' service managers.

    seneschald install | uninstall | start | stop | restart | status | logs

Design: `seneschal/docs/service-supervision-spec.md` (§3 the verb table). One backend per OS, each
implementing the same seven verbs:

    verb       systemd (user unit)              launchd (agent)                 Windows (task + supervisor)
    install    render + copy the units;         render + copy the plists;       render; run register-tasks.ps1
               daemon-reload; enable --now;     bootstrap gui/<uid>             under ONE elevation (the only
               enable-linger (the one sudo)                                     UAC prompt), or unelevated for
                                                                                --user-level
    uninstall  disable --now; remove units      bootout; remove plists          schtasks /Delete (elevates once
                                                                                if refused)
    start      clear the stop/crash-loop        clear them; kickstart (or       clear them; schtasks /Run
               sentinels; reset-failed; start   bootstrap if not loaded)
    stop       stop sentinel; systemctl stop    stop sentinel; bootout (a       stop sentinel; graceful
                                                KeepAlive job respawns if       shutdown --now, wait 30 s for the
                                                merely killed)                  supervisor; schtasks /End; kill
                                                                                the VERIFIED supervisor and lock
                                                                                holder; withdraw the shutdown
    restart    the control queue (graceful; execv in place on POSIX, exit 75 under the Windows supervisor);
               --hard = stop, then start
    status     systemctl show                   launchctl print                 supervisor report + schtasks /Query
    logs       journalctl + presence.log        launchd-presence.log +          seneschald-child.log +
                                                presence.log                    presence.log

**Every command goes through one injectable runner** (`Runner`), so the tests drive each backend with a
fake that records argv and answers like the real tool, on any OS. The real runner never spawns a shell.

**Windows elevation.** Registering an S4U task needs admin, so `install` launches the rendered
`register-tasks.ps1` through `ShellExecuteExW` with the `runas` verb — one UAC prompt — and waits on the
process handle (no pipes; a detached elevated pwsh is not the piped child that AMSI wedges). When this
process is already elevated, or with `--user-level` (InteractiveToken, no admin), it runs it without the
verb. Every other verb runs unelevated and, when Task Scheduler refuses (a UAC-filtered token on a task
an admin registered), says so plainly and prints the exact elevated command — it never prompts on its
own and never fails silently (`EXIT_DENIED`).

**Stop never kills a pid it cannot prove.** The supervisor's pid is killed only when the supervisor's
own kernel lock is held AND its owner stamp names that pid (`single_instance.holder`); the daemon's
only when `presence.instance.lock` is held by it. A recycled pid can never be the target.

Stdlib only. Exit codes: 0 ok, 1 a step failed, 2 usage, 3 refused by the OS (elevation needed),
4 not installed.

Usage:
    python seneschal/scripts/service.py <verb> [--platform win32|linux|darwin] [--user-level]
        [--hard] [--lines N] [--json] [--print-only] [--repo DIR] [--state-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import render_units as ru  # noqa: E402
import request_control  # noqa: E402
import single_instance  # noqa: E402

REPO_ROOT = HERE.parents[1]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_DENIED = 3
EXIT_NOT_INSTALLED = 4

VERBS = ("install", "uninstall", "start", "stop", "restart", "status", "logs")

STOPPED_SENTINEL = "seneschald-stopped"
CRASHLOOP_SENTINEL = "seneschald-crashloop"
BOOT_ATTEMPTS = "boot-attempts.json"
SUPERVISOR_REPORT = "seneschald-supervisor.json"
SUPERVISOR_LOCK = "seneschald-supervisor.instance.lock"
CHILD_LOG = os.path.join("logs", "seneschald-child.log")
PRESENCE_LOG = "presence.log"
LAUNCHD_LOG = "launchd-presence.log"

#: The reason the Windows Stop queues its graceful shutdown under, and withdraws it by. Never change it
#: between the two calls of one Stop.
STOP_REASON = "seneschald stop"
STOP_GRACEFUL_WAIT_SEC = 30.0
#: A report whose heartbeat is older than this is not a running supervisor (it rewrites it every 30 s
#: while a child runs and before every backoff, so 180 s is its longest honest silence).
SUPERVISOR_FRESH_SEC = 180
SUPERVISOR_EXITED_STATES = ("stopped", "gave-up", "upgrading")
ELEVATION_WAIT_SEC = 600

DENIED_MARKERS = ("access is denied", "access denied", "0x80070005", "permission denied")


# ------------------------------------------------------------------------------------------ runner
@dataclass
class Result:
    rc: int | None          # None = the tool is missing or did not finish
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def denied(self) -> bool:
        text = (self.out + "\n" + self.err).lower()
        return self.rc not in (0, None) and any(m in text for m in DENIED_MARKERS)


class Runner:
    """Runs one command, never through a shell. Tests replace it with a fake."""

    CREATE_NO_WINDOW = 0x08000000

    def run(self, argv: list, timeout: float = 60.0) -> Result:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = self.CREATE_NO_WINDOW
        try:
            p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, errors="replace", **kwargs)
        except FileNotFoundError as e:
            return Result(None, "", str(e))
        except subprocess.TimeoutExpired:
            return Result(None, "", f"timed out after {timeout:g} s")
        except OSError as e:
            return Result(None, "", str(e))
        return Result(p.returncode, p.stdout or "", p.stderr or "")

    def run_elevated(self, exe: str, params: str, *, elevate: bool, hidden: bool = True,
                     timeout: float = ELEVATION_WAIT_SEC) -> Result:
        """Windows only: ShellExecuteExW (`runas` when `elevate`), wait on the process handle, return its
        exit code. The one place a UAC prompt can appear."""
        return shell_execute_wait(exe, params, elevate=elevate, hidden=hidden, timeout=timeout)


def shell_execute_wait(exe: str, params: str, *, elevate: bool, hidden: bool, timeout: float) -> Result:
    if os.name != "nt":
        return Result(None, "", "ShellExecuteEx is Windows-only")
    try:
        import ctypes
        from ctypes import wintypes

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                        ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                        ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
                        ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                        ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

        SEE_MASK_NOCLOSEPROCESS = 0x00000040
        SEE_MASK_NO_CONSOLE = 0x00008000
        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
        info.lpVerb = "runas" if elevate else "open"
        info.lpFile = exe
        info.lpParameters = params
        info.nShow = 0 if hidden else 1  # SW_HIDE / SW_SHOWNORMAL
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            err = ctypes.get_last_error()
            # 1223 = ERROR_CANCELLED: the owner said no at the UAC prompt.
            return Result(None, "", "the UAC prompt was declined" if err == 1223 else f"ShellExecuteEx error {err}")
        if not info.hProcess:
            return Result(None, "", "no process handle came back")
        try:
            if k32.WaitForSingleObject(info.hProcess, int(timeout * 1000)) != 0:
                return Result(None, "", f"still running after {timeout:g} s")
            code = wintypes.DWORD()
            k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
            return Result(int(code.value))
        finally:
            k32.CloseHandle(info.hProcess)
    except Exception as e:  # noqa: BLE001
        return Result(None, "", f"ShellExecuteEx failed: {e}")


def windows_elevated() -> bool | None:
    """True = this token is elevated (or UAC is off for an admin), False = a filtered/standard token,
    None = could not tell / not Windows."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = wintypes.HANDLE()
        if not a.OpenProcessToken(k.GetCurrentProcess(), 0x0008, ctypes.byref(h)):  # TOKEN_QUERY
            return None
        try:
            v, n = wintypes.DWORD(), wintypes.DWORD()
            if not a.GetTokenInformation(h, 20, ctypes.byref(v), 4, ctypes.byref(n)):  # TokenElevation
                return None
            return bool(v.value)
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------------------------------ shared state
def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = wintypes.DWORD()
                return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
            finally:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _age_sec(stamp) -> float | None:
    from datetime import datetime, timezone
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


@dataclass
class Ctx:
    """Everything a verb needs to know about the install, plus the injected effects."""
    platform: str
    repo: Path
    state_dir: Path
    home: Path
    runner: Runner = field(default_factory=Runner)
    out: object = None                      # a callable(str); print by default
    sleep: object = time.sleep
    monotonic: object = time.monotonic
    uid: int | None = None
    user: str | None = None
    kill: object = None                     # callable(pid) -> bool
    alive: object = pid_alive               # callable(pid) -> bool
    holder: object = single_instance.holder  # callable(path) -> {"held", "pid"}
    elevated: object = windows_elevated     # callable() -> bool | None

    def say(self, msg: str) -> None:
        (self.out or print)(msg)

    def path(self, name: str) -> str:
        return str(self.state_dir / name)


def _default_kill(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
        return True
    except OSError:
        return False


def write_stop_sentinel(ctx: Ctx) -> None:
    """Intent first: a stop that dies half-way still reads as deliberate (the watchdog's rule 1 and the
    supervisor's before-every-restart check both read it)."""
    try:
        os.makedirs(ctx.state_dir, exist_ok=True)
        with open(ctx.path(STOPPED_SENTINEL), "a", encoding="utf-8"):
            pass
    except OSError as e:
        ctx.say(f"! could not write {STOPPED_SENTINEL}: {e}")


def clear_for_start(ctx: Ctx) -> None:
    """A human start is the clean slate: the stop intent, the crash-loop give-up (the daemon's or the
    supervisor's) and the boot window that fed it all go — what Start-Seneschald does."""
    for name in (STOPPED_SENTINEL, CRASHLOOP_SENTINEL, BOOT_ATTEMPTS):
        try:
            os.remove(ctx.path(name))
        except FileNotFoundError:
            pass
        except OSError as e:
            ctx.say(f"! could not remove {name}: {e}")


def graceful_restart(ctx: Ctx, now: bool) -> int:
    request_control.enqueue_control(str(ctx.state_dir), "restart", "seneschald restart", defer=not now)
    ctx.say("- graceful restart queued (applies once the warm session is idle"
            + ("; --now skips that wait" if now else "") + ")")
    return EXIT_OK


def supervisor_summary(ctx: Ctx) -> dict:
    """The supervisor's report, reduced: state running | down | none, plus the verified pid."""
    rep = _read_json(ctx.path(SUPERVISOR_REPORT))
    if not isinstance(rep, dict):
        return {"state": "none"}
    age = _age_sec(rep.get("heartbeat"))
    running = age is not None and age <= SUPERVISOR_FRESH_SEC and rep.get("state") not in SUPERVISOR_EXITED_STATES
    restarts = rep.get("restarts") if isinstance(rep.get("restarts"), dict) else {}
    budget = rep.get("crash_budget") if isinstance(rep.get("crash_budget"), dict) else {}
    return {
        "state": "running" if running else "down",
        "pid": rep.get("pid"), "child_pid": rep.get("child_pid"), "reported_state": rep.get("state"),
        "heartbeat_age_sec": None if age is None else round(age),
        "reloads": restarts.get("reload"), "crash_restarts": restarts.get("crash"),
        "crash_restarts_left": budget.get("left"), "last_exit": rep.get("last_exit"),
        "verified_pid": verified_supervisor_pid(ctx),
    }


def verified_supervisor_pid(ctx: Ctx) -> int | None:
    """The supervisor's pid, ONLY when its own kernel lock is held and the lock's owner stamp names the
    pid its report names. Anything else — no report, no holder, a mismatch — is None."""
    rep = _read_json(ctx.path(SUPERVISOR_REPORT))
    pid = rep.get("pid") if isinstance(rep, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    h = ctx.holder(ctx.path(SUPERVISOR_LOCK))
    return pid if h.get("held") and h.get("pid") == pid else None


def daemon_lock_holder(ctx: Ctx) -> int | None:
    h = ctx.holder(ctx.path(single_instance.INSTANCE_LOCK_NAME))
    pid = h.get("pid")
    return pid if h.get("held") and isinstance(pid, int) else None


def wait_pid_exit(ctx: Ctx, pid: int, timeout: float) -> bool:
    deadline = ctx.monotonic() + timeout
    while ctx.alive(pid):
        if ctx.monotonic() >= deadline:
            return False
        ctx.sleep(0.5)
    return True


def tail(path: str, lines: int) -> list:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


def daemon_lines(ctx: Ctx) -> list:
    """Platform-neutral facts every status shows."""
    pid = daemon_lock_holder(ctx)
    lines = [f"daemon: {'running (pid ' + str(pid) + ')' if pid else 'not running (nobody holds presence.instance.lock)'}"]
    if os.path.exists(ctx.path(STOPPED_SENTINEL)):
        lines.append(f"  {STOPPED_SENTINEL} present: deliberately stopped")
    if os.path.exists(ctx.path(CRASHLOOP_SENTINEL)):
        lines.append(f"  {CRASHLOOP_SENTINEL} present: the crash budget gave up; `seneschald start` clears it")
    return lines


def render_assets(ctx: Ctx, *, install: bool, user_level: bool = False) -> int:
    argv = ["render_units.py", "--platform", ctx.platform, "--repo", str(ctx.repo), "--home", str(ctx.home),
            "--apply"]
    if install:
        argv.append("--install")
    if user_level:
        argv.append("--user-level")
    rc = ru._main(argv)
    if rc != 0:
        ctx.say(f"! render_units.py exited {rc}; nothing was registered")
    return rc


# ------------------------------------------------------------------------------------------ systemd
class SystemdBackend:
    unit = ru.UNIT_DAEMON
    timer = ru.UNIT_UPDATE_TIMER

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def sysctl(self, *args, timeout=60.0) -> Result:
        return self.ctx.runner.run(["systemctl", "--user", *args], timeout=timeout)

    def install(self, a) -> int:
        c = self.ctx
        if render_assets(c, install=True) != 0:
            return EXIT_FAILED
        for step in (("daemon-reload",), ("enable", "--now", self.unit, self.timer)):
            r = self.sysctl(*step)
            if not r.ok:
                c.say(f"! systemctl --user {' '.join(step)} failed (rc {r.rc}): {r.err.strip()}")
                return EXIT_FAILED
        c.say(f"- {self.unit} and {self.timer} enabled and started")
        return self.linger()

    def linger(self) -> int:
        """User units outlive logout and start at boot only with linger. Try it unprivileged (polkit
        often allows it for oneself); otherwise print the ONE sudo command and leave it to the owner."""
        c = self.ctx
        user = c.user or os.environ.get("USER") or ""
        r = c.runner.run(["loginctl", "show-user", user, "-p", "Linger"], timeout=30)
        if r.ok and "Linger=yes" in r.out:
            c.say("- linger already enabled (the daemon starts at boot and survives logout)")
            return EXIT_OK
        r = c.runner.run(["loginctl", "enable-linger", user], timeout=30)
        if r.ok:
            c.say("- linger enabled")
            return EXIT_OK
        c.say(f"- linger is off, and enabling it needs root. Run this once (the one sudo):\n"
              f"    sudo loginctl enable-linger {user or '$USER'}\n"
              "  Until then the daemon runs only while you are logged in.")
        return EXIT_OK

    def uninstall(self, a) -> int:
        c = self.ctx
        self.sysctl("disable", "--now", self.unit, self.timer)
        removed = []
        for name in (ru.UNIT_DAEMON, ru.UNIT_UPDATE_SERVICE, ru.UNIT_UPDATE_TIMER):
            path = c.home / ".config" / "systemd" / "user" / name
            try:
                path.unlink()
                removed.append(name)
            except FileNotFoundError:
                pass
        self.sysctl("daemon-reload")
        c.say(f"- disabled; removed {', '.join(removed) or 'no unit files (none were installed)'}")
        return EXIT_OK

    def start(self, a) -> int:
        clear_for_start(self.ctx)
        # A human start lifts systemd's start-limit give-up too (the crash budget's systemd half).
        self.sysctl("reset-failed", self.unit)
        r = self.sysctl("start", self.unit)
        if not r.ok:
            self.ctx.say(f"! systemctl --user start {self.unit} failed (rc {r.rc}): {r.err.strip()}")
            return EXIT_NOT_INSTALLED if "not found" in r.err.lower() else EXIT_FAILED
        self.ctx.say(f"- {self.unit} started")
        return EXIT_OK

    def stop(self, a) -> int:
        write_stop_sentinel(self.ctx)
        r = self.sysctl("stop", self.unit)
        if not r.ok:
            self.ctx.say(f"! systemctl --user stop {self.unit} failed (rc {r.rc}): {r.err.strip()}")
            return EXIT_FAILED
        self.ctx.say(f"- {self.unit} stopped ({STOPPED_SENTINEL} written)")
        return EXIT_OK

    def status(self, a) -> dict:
        r = self.sysctl("show", self.unit, "-p", "ActiveState,SubState,MainPID,NRestarts,ExecMainStatus,Result")
        props = dict(ln.split("=", 1) for ln in r.out.splitlines() if "=" in ln) if r.ok else {}
        return {"manager": "systemd", "unit": self.unit, "available": r.rc is not None, "properties": props}

    def logs(self, a) -> list:
        r = self.ctx.runner.run(["journalctl", "--user", "-u", self.unit, "-n", str(a.lines), "--no-pager"])
        return [f"== journalctl --user -u {self.unit} =="] + (r.out.splitlines() if r.ok else [f"(unavailable: {r.err.strip()})"])


# ------------------------------------------------------------------------------------------ launchd
class LaunchdBackend:
    label = ru.LABEL_PRESENCE
    labels = (ru.LABEL_PRESENCE, ru.LABEL_UPDATE)

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    @property
    def domain(self) -> str:
        uid = self.ctx.uid if self.ctx.uid is not None else (os.getuid() if hasattr(os, "getuid") else 0)
        return f"gui/{uid}"

    def plist(self, label: str) -> Path:
        return self.ctx.home / "Library" / "LaunchAgents" / f"{label}.plist"

    def lctl(self, *args, timeout=60.0) -> Result:
        return self.ctx.runner.run(["launchctl", *args], timeout=timeout)

    def loaded(self, label: str) -> bool:
        return self.lctl("print", f"{self.domain}/{label}").ok

    def install(self, a) -> int:
        c = self.ctx
        if render_assets(c, install=True) != 0:
            return EXIT_FAILED
        for label in self.labels:
            if self.loaded(label):  # bootout first, so a re-install picks up the new plist
                self.lctl("bootout", f"{self.domain}/{label}")
            r = self.lctl("bootstrap", self.domain, str(self.plist(label)))
            if not r.ok:
                c.say(f"! launchctl bootstrap {label} failed (rc {r.rc}): {r.err.strip()}")
                return EXIT_FAILED
        c.say(f"- {', '.join(self.labels)} bootstrapped into {self.domain}")
        return EXIT_OK

    def uninstall(self, a) -> int:
        c = self.ctx
        removed = []
        for label in self.labels:
            self.lctl("bootout", f"{self.domain}/{label}")
            try:
                self.plist(label).unlink()
                removed.append(label)
            except FileNotFoundError:
                pass
        c.say(f"- booted out; removed {', '.join(removed) or 'no plists (none were installed)'}")
        return EXIT_OK

    def start(self, a) -> int:
        clear_for_start(self.ctx)
        if self.loaded(self.label):
            r = self.lctl("kickstart", f"{self.domain}/{self.label}")
        elif self.plist(self.label).exists():
            # `stop` booted it out (a KeepAlive job respawns if merely killed), so start bootstraps.
            r = self.lctl("bootstrap", self.domain, str(self.plist(self.label)))
        else:
            self.ctx.say(f"! {self.plist(self.label)} is not installed; run `seneschald install`")
            return EXIT_NOT_INSTALLED
        if not r.ok:
            self.ctx.say(f"! launchctl could not start {self.label} (rc {r.rc}): {r.err.strip()}")
            return EXIT_FAILED
        self.ctx.say(f"- {self.label} started")
        return EXIT_OK

    def stop(self, a) -> int:
        write_stop_sentinel(self.ctx)
        if not self.loaded(self.label):
            self.ctx.say(f"- {self.label} was not loaded ({STOPPED_SENTINEL} written)")
            return EXIT_OK
        r = self.lctl("bootout", f"{self.domain}/{self.label}")
        if not r.ok:
            self.ctx.say(f"! launchctl bootout {self.label} failed (rc {r.rc}): {r.err.strip()}")
            return EXIT_FAILED
        self.ctx.say(f"- {self.label} booted out ({STOPPED_SENTINEL} written)")
        return EXIT_OK

    def status(self, a) -> dict:
        r = self.lctl("print", f"{self.domain}/{self.label}")
        props = {}
        for ln in r.out.splitlines() if r.ok else []:
            key, sep, val = ln.strip().partition(" = ")
            if sep and key in ("state", "pid", "last exit code", "runs", "last terminating signal"):
                props[key] = val
        return {"manager": "launchd", "label": self.label, "loaded": r.ok, "properties": props}

    def logs(self, a) -> list:
        path = self.ctx.path(LAUNCHD_LOG)
        return [f"== {path} =="] + (tail(path, a.lines) or ["(empty or missing)"])


# ------------------------------------------------------------------------------------------ windows
class WindowsBackend:
    task = ru.TASK_DAEMON

    def __init__(self, ctx: Ctx, task: str | None = None):
        self.ctx = ctx
        if task:
            self.task = task  # service_probe.py aims the same verbs at its throwaway task

    def schtasks(self, *args, timeout=60.0) -> Result:
        return self.ctx.runner.run(["schtasks", *args], timeout=timeout)

    def denied(self, verb: str, r: Result, elevated_cmd: str) -> int:
        self.ctx.say(f"! Task Scheduler refused `{verb}` (rc {r.rc}): {(r.err or r.out).strip()}\n"
                     "  This token is not allowed to drive the task (UAC keeps a non-elevated shell from\n"
                     "  managing a task an administrator registered). Run this from an elevated shell:\n"
                     f"    {elevated_cmd}")
        return EXIT_DENIED

    def install(self, a) -> int:
        c = self.ctx
        if render_assets(c, install=False, user_level=a.user_level) != 0:
            return EXIT_FAILED
        ps1 = c.repo / ru.SETUP_DIR_REL / "register-tasks.ps1"
        result = c.repo / ru.SETUP_DIR_REL / "register-tasks.result.json"
        shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        params = f'-NoProfile -ExecutionPolicy Bypass -File "{ps1}"'
        elevate = not a.user_level and not c.elevated()
        if a.print_only:
            c.say(("Run this once from an elevated shell (the one UAC prompt):\n" if not a.user_level
                   else "Run this (no elevation needed):\n")
                  + f"    {shell} {params}")
            return EXIT_OK
        try:
            result.unlink()
        except FileNotFoundError:
            pass
        if elevate:
            c.say("- registering the tasks: Windows will ask once for administrator approval (UAC)")
        r = c.runner.run_elevated(shell, params, elevate=elevate)
        report = _read_json(str(result))
        if isinstance(report, dict):
            for row in report.get("tasks") or []:
                c.say(f"  {'ok  ' if row.get('ok') else 'FAIL'}  {row.get('task')} - {row.get('detail')}")
        if r.rc is None:
            c.say(f"! register-tasks.ps1 did not run: {r.err}")
            return EXIT_DENIED if "declined" in r.err else EXIT_FAILED
        if r.rc != 0 or not isinstance(report, dict):
            c.say(f"! register-tasks.ps1 exited {r.rc}" + ("" if isinstance(report, dict) else " and wrote no result file"))
            return EXIT_FAILED
        return EXIT_OK

    def uninstall(self, a) -> int:
        c = self.ctx
        failed = []
        for name in (ru.TASK_DAEMON, ru.TASK_UPDATE):
            r = self.schtasks("/Delete", "/TN", name, "/F")
            if r.ok:
                c.say(f"- deleted task {name}")
            elif r.denied:
                failed.append(name)
            elif "cannot find" in (r.err + r.out).lower():
                c.say(f"- task {name} was not registered")
            else:
                c.say(f"! schtasks /Delete {name} failed (rc {r.rc}): {(r.err or r.out).strip()}")
                return EXIT_FAILED
        if failed:
            # An S4U task an admin registered needs admin to delete: ONE elevated retry for all of them.
            cmd = " & ".join(f"schtasks /Delete /TN {n} /F" for n in failed)
            c.say("- deleting needs administrator approval: Windows will ask once (UAC)")
            r = c.runner.run_elevated("cmd.exe", f"/c {cmd}", elevate=True)
            if not r.ok:
                c.say(f"! the elevated delete did not complete: {r.err or r.rc}")
                return EXIT_DENIED if r.rc is None else EXIT_FAILED
            c.say(f"- deleted {', '.join(failed)} (elevated)")
        return EXIT_OK

    def start(self, a) -> int:
        clear_for_start(self.ctx)
        r = self.schtasks("/Run", "/TN", self.task)
        if r.ok:
            self.ctx.say(f"- task {self.task} started")
            return EXIT_OK
        if r.denied:
            return self.denied("start", r, f"schtasks /Run /TN {self.task}")
        if "cannot find" in (r.err + r.out).lower():
            self.ctx.say(f"! task {self.task} is not registered; run `seneschald install`")
            return EXIT_NOT_INSTALLED
        self.ctx.say(f"! schtasks /Run failed (rc {r.rc}): {(r.err or r.out).strip()}")
        return EXIT_FAILED

    def stop(self, a) -> int:
        """docs/service-supervision-spec.md §3 — the supervised Stop. Graceful first, so the daemon saves
        its state and ends its warm session the way a reload does; the hard path only for what is left."""
        c = self.ctx
        kill = c.kill or _default_kill
        write_stop_sentinel(c)
        sup = verified_supervisor_pid(c)
        queued = False
        if sup:
            request_control.enqueue_control(str(c.state_dir), "shutdown", STOP_REASON, defer=False)
            queued = request_control.queued(str(c.state_dir), "shutdown", STOP_REASON)
            if not queued:
                c.say("- another shutdown is already queued; waiting on it instead")
            if wait_pid_exit(c, sup, STOP_GRACEFUL_WAIT_SEC):
                c.say("- the daemon shut down cleanly and its supervisor exited")
            else:
                c.say(f"! no clean exit within {STOP_GRACEFUL_WAIT_SEC:g} s; stopping it hard")
        r = self.schtasks("/End", "/TN", self.task)
        denied = r.denied
        ended = verified_supervisor_pid(c)
        if r.ok and ended:
            # /End returns before the process is gone; give it a moment so the belt below only acts on
            # what /End really left (the live probe saw this on an InteractiveToken task).
            wait_pid_exit(c, ended, 5)
        # Whatever is still alive — a supervisor schtasks could not end (denied, or a hand run outside
        # the task), a daemon outside the task's tree — goes by its VERIFIED pid. Plain kill, never a
        # tree kill: the cockpit and archon sites keep their own lifetimes.
        left = verified_supervisor_pid(c)
        if left:
            c.say(f"- stopping supervisor pid {left}" + (" (schtasks /End was refused)" if denied else ""))
            kill(left)
            wait_pid_exit(c, left, 10)
        dpid = daemon_lock_holder(c)
        if dpid:
            c.say(f"- stopping daemon pid {dpid}")
            kill(dpid)
            wait_pid_exit(c, dpid, 10)
        try:
            os.remove(c.path("presence.lock"))
        except OSError:
            pass
        if queued and request_control.withdraw_control(str(c.state_dir), "shutdown", STOP_REASON):
            c.say("- withdrew Stop's own queued shutdown (nobody consumed it)")
        still = verified_supervisor_pid(c) or daemon_lock_holder(c)
        if still:
            c.say(f"! pid {still} is still running")
            return self.denied("stop", r, f"schtasks /End /TN {self.task}") if denied else EXIT_FAILED
        c.say(f"- stopped ({STOPPED_SENTINEL} written)")
        return EXIT_OK

    def status(self, a) -> dict:
        r = self.schtasks("/Query", "/TN", self.task, "/FO", "LIST", "/V")
        props = {}
        for ln in r.out.splitlines() if r.ok else []:
            key, sep, val = ln.partition(":")
            if sep and key.strip() in ("Status", "Last Run Time", "Last Result", "Logon Mode", "Run As User",
                                       "Task To Run"):
                props[key.strip()] = val.strip()
        return {"manager": "task-scheduler", "task": self.task, "registered": r.ok, "properties": props,
                "supervisor": supervisor_summary(self.ctx)}

    def logs(self, a) -> list:
        path = self.ctx.path(CHILD_LOG)
        return [f"== {path} =="] + (tail(path, a.lines) or ["(empty or missing)"])


BACKENDS = {"linux": SystemdBackend, "darwin": LaunchdBackend, "win32": WindowsBackend}


# ------------------------------------------------------------------------------------------ verbs
def do_status(ctx: Ctx, backend, a) -> int:
    st = backend.status(a)
    st["daemon_pid"] = daemon_lock_holder(ctx)
    st["stopped"] = os.path.exists(ctx.path(STOPPED_SENTINEL))
    st["crashloop"] = os.path.exists(ctx.path(CRASHLOOP_SENTINEL))
    if a.json:
        ctx.say(json.dumps(st, indent=2, default=str))
        return EXIT_OK
    ctx.say(f"{st['manager']}:")
    for k, v in (st.get("properties") or {}).items():
        ctx.say(f"  {k}: {v}")
    sup = st.get("supervisor")
    if sup is not None:
        if sup["state"] == "none":
            ctx.say("supervisor: no report (the task does not run seneschald_supervise.py, or it never started)")
        else:
            ctx.say(f"supervisor: {sup['state']} (pid {sup.get('pid')}; {sup.get('reloads')} reload(s), "
                    f"{sup.get('crash_restarts')} crash restart(s), {sup.get('crash_restarts_left')} left in the "
                    f"15-min budget)")
    for line in daemon_lines(ctx):
        ctx.say(line)
    return EXIT_OK


def do_logs(ctx: Ctx, backend, a) -> int:
    for line in backend.logs(a):
        ctx.say(line)
    path = ctx.path(PRESENCE_LOG)
    ctx.say(f"== {path} ==")
    for line in tail(path, a.lines) or ["(empty or missing)"]:
        ctx.say(line)
    return EXIT_OK


def run_verb(ctx: Ctx, a) -> int:
    backend = BACKENDS[ctx.platform](ctx)
    if a.verb == "status":
        return do_status(ctx, backend, a)
    if a.verb == "logs":
        return do_logs(ctx, backend, a)
    if a.verb == "restart":
        if not a.hard:
            return graceful_restart(ctx, now=a.now)
        rc = backend.stop(a)
        return rc if rc != EXIT_OK else backend.start(a)
    return getattr(backend, a.verb)(a)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seneschald", description="Install and drive the seneschal daemon as a "
                                "service (systemd / launchd / Windows Task Scheduler + supervisor).")
    p.add_argument("verb", choices=VERBS)
    p.add_argument("--platform", choices=ru.PLATFORMS, default=ru.default_platform())
    p.add_argument("--repo", default=None, help="the checkout (default: this one)")
    p.add_argument("--state-dir", default=None, help="default: <repo>/seneschal/state")
    p.add_argument("--home", default=None, help="home dir override (tests)")
    p.add_argument("--user-level", action="store_true",
                   help="Windows install: the no-admin fallback (pythonw supervisor, runs while logged on)")
    p.add_argument("--print-only", action="store_true",
                   help="Windows install: render, then print the one elevated command instead of running it")
    p.add_argument("--hard", action="store_true", help="restart: stop, then start (for a wedged daemon)")
    p.add_argument("--now", action="store_true", help="graceful restart: skip the chat-idle wait")
    p.add_argument("--lines", type=int, default=40, help="logs: lines per log (default 40)")
    p.add_argument("--json", action="store_true", help="status: machine-readable")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    repo = Path(a.repo).resolve() if a.repo else REPO_ROOT
    ctx = Ctx(platform=a.platform, repo=repo,
              state_dir=Path(a.state_dir) if a.state_dir else repo / "seneschal" / "state",
              home=Path(a.home) if a.home else Path.home())
    if a.platform != ru.default_platform() and a.verb not in ("status", "logs"):
        ctx.say(f"! --platform {a.platform} does not match this machine ({ru.default_platform()}); "
                "only status/logs make sense cross-platform")
        return EXIT_USAGE
    return run_verb(ctx, a)


if __name__ == "__main__":
    sys.exit(main())
