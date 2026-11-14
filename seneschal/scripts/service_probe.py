#!/usr/bin/env python3
r"""service_probe — the LIVE per-host check of the Windows service shape (maintainer-run, never in CI).

docs/service-supervision-spec.md §7. The unit tests prove the supervisor, the verbs and the rendered
task on every OS; what only a real host can answer is how Task Scheduler and UAC treat them. This probe
registers a THROWAWAY task (`zz-seneschal-svc-probe-<mode>`, no triggers — started only by the probe)
whose action is the real `seneschald_supervise.py` supervising a FAKE daemon (it holds the real
`presence.instance.lock` in a probe-only state dir, exits 75 when asked to reload, and records its pid,
session and console on every start). Then, from the token the probe runs under:

  1. register — S4U needs admin: from a filtered token this is the ONE UAC prompt (ShellExecuteEx runas);
  2. start     — `schtasks /Run` from this token: allowed, or refused (the UAC-on answer for `start`);
  3. verify    — one supervisor, one daemon; the supervisor verified by its kernel lock; the daemon's
                 session (0 for S4U; the owner's for InteractiveToken) and whether it has a console;
  4. reload    — the daemon exits 75: restarted by the SAME supervisor, within 10 s, task still running;
  5. crash     — the daemon is killed: restarted by the same supervisor after its 2 s backoff;
  6. stop      — `service.py`'s Windows Stop aimed at the probe task: nothing left running;
  7. unregister (always, in a `finally`; elevated once more if this token may not delete it).

With `--as-limited` (on an elevated host only), steps 2, 5's kill and 6's `schtasks /End` are ALSO tried
from a child process holding a SAFER "normal user" token — the Administrators group deny-only, the shape
of a UAC-filtered token — and its answers are recorded beside the elevated ones. That is an
APPROXIMATION of a UAC-on host, stated as one: the real UAC-on run is this probe started from a
non-elevated shell on a host with UAC prompting enabled.

`--mode user` is the no-admin fallback (an InteractiveToken task running the supervisor under
`pythonw.exe`): no elevation at all, and step 3 measures whether anything shows a window in the owner's
session — the session-1 measurement the fallback needed before it shipped.

Prints one JSON document; exit 0 only when every required step passed.

    python seneschal/scripts/service_probe.py --mode s4u [--as-limited] [--keep-state]
    python seneschal/scripts/service_probe.py --mode user
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import service as svc  # noqa: E402
import single_instance as si  # noqa: E402

REPO = HERE.parents[1]
TASK_PREFIX = "zz-seneschal-svc-probe-"

_FAKE_DAEMON = r'''
import ctypes, json, os, sys, time
state = os.environ["PROBE_STATE"]
sys.path.insert(0, os.environ["PROBE_SCRIPTS"])
import single_instance as si
k32 = ctypes.WinDLL("kernel32")
sess = ctypes.c_ulong()
k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sess))
k32.GetConsoleWindow.restype = ctypes.c_void_p
lock = si.InstanceLock(si.instance_lock_path(state))
won = lock.acquire(wait_sec=20)
with open(os.path.join(state, "probe-child.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"pid": os.getpid(), "ppid": os.getppid(), "session": sess.value,
                         "console_window": bool(k32.GetConsoleWindow()), "won_lock": won,
                         "supervised": os.environ.get("SENESCHALD_SUPERVISED"),
                         "exe": sys.executable, "at": time.time()}) + "\n")
if not won:
    sys.exit(69)
while True:
    if os.path.exists(os.path.join(state, "probe-reload")):
        os.remove(os.path.join(state, "probe-reload"))
        lock.release()
        sys.exit(75)
    time.sleep(0.2)
'''


def task_xml(*, user: str, logon: str, command: str, arguments: str, workdir: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>seneschal service probe - throwaway, removed by the probe</Description></RegistrationInfo>
  <Triggers />
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>{logon}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def uac_policy() -> dict:
    import winreg
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System") as k:
            for name in ("EnableLUA", "ConsentPromptBehaviorAdmin", "PromptOnSecureDesktop"):
                try:
                    out[name] = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    out[name] = None
    except OSError:
        pass
    return out


def run_limited(argv: list, timeout: float = 60.0) -> dict:
    """Run argv under a SAFER normal-user token (Administrators deny-only — the UAC-filtered shape),
    windowless, and return {rc, error}. Only meaningful from an elevated token."""
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    advapi.SaferCreateLevel.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    advapi.SaferComputeTokenFromLevel.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE),
                                                  wintypes.DWORD, ctypes.c_void_p]
    advapi.SaferCloseLevel.argtypes = [wintypes.HANDLE]
    advapi.CreateProcessAsUserW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                            ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
                                            wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                                            ctypes.POINTER(PROCESS_INFORMATION)]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    SAFER_SCOPEID_USER, SAFER_LEVELID_NORMALUSER, SAFER_LEVEL_OPEN = 2, 0x20000, 1
    level, token = wintypes.HANDLE(), wintypes.HANDLE()
    if not advapi.SaferCreateLevel(SAFER_SCOPEID_USER, SAFER_LEVELID_NORMALUSER, SAFER_LEVEL_OPEN,
                                   ctypes.byref(level), None):
        return {"rc": None, "error": f"SaferCreateLevel {ctypes.get_last_error()}"}
    try:
        if not advapi.SaferComputeTokenFromLevel(level, None, ctypes.byref(token), 0, None):
            return {"rc": None, "error": f"SaferComputeTokenFromLevel {ctypes.get_last_error()}"}
        si_ = STARTUPINFOW()
        si_.cb = ctypes.sizeof(si_)
        pi = PROCESS_INFORMATION()
        cmdline = ctypes.create_unicode_buffer(subprocess_list2cmdline(argv))
        CREATE_NO_WINDOW = 0x08000000
        if not advapi.CreateProcessAsUserW(token, None, cmdline, None, None, False, CREATE_NO_WINDOW, None,
                                           None, ctypes.byref(si_), ctypes.byref(pi)):
            return {"rc": None, "error": f"CreateProcessAsUser {ctypes.get_last_error()}"}
        try:
            if k32.WaitForSingleObject(pi.hProcess, int(timeout * 1000)) != 0:
                return {"rc": None, "error": "timed out"}
            code = wintypes.DWORD()
            k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
            return {"rc": int(code.value), "error": None}
        finally:
            k32.CloseHandle(pi.hThread)
            k32.CloseHandle(pi.hProcess)
    finally:
        if token:
            k32.CloseHandle(token)
        advapi.SaferCloseLevel(level)


def subprocess_list2cmdline(argv):
    import subprocess
    return subprocess.list2cmdline([str(a) for a in argv])


class Probe:
    def __init__(self, mode: str, as_limited: bool, keep_state: bool):
        self.mode = mode
        self.as_limited = as_limited
        self.keep_state = keep_state
        self.task = TASK_PREFIX + mode
        self.runner = svc.Runner()
        self.elevated = bool(svc.windows_elevated())
        self.workdir = Path(tempfile.mkdtemp(prefix="seneschal-svc-probe-"))
        self.out = {"mode": mode, "task": self.task, "elevated_token": self.elevated, "uac": uac_policy(),
                    "steps": {}, "limited_token": {} if as_limited else None}
        self.registered = False

    def step(self, name, ok, **detail):
        self.out["steps"][name] = {"ok": bool(ok), **detail}
        return ok

    def children(self) -> list:
        try:
            with open(self.workdir / "probe-child.jsonl", encoding="utf-8") as fh:
                return [json.loads(ln) for ln in fh if ln.strip()]
        except OSError:
            return []

    def wait(self, pred, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = pred()
            if v:
                return v
            time.sleep(0.25)
        return pred()

    def sup_pid(self):
        return si.holder(str(self.workdir / svc.SUPERVISOR_LOCK)).get("pid") if si.is_held(
            str(self.workdir / svc.SUPERVISOR_LOCK)) else None

    def daemon_pid(self):
        h = si.holder(si.instance_lock_path(str(self.workdir)))
        return h["pid"] if h["held"] else None

    def schtasks(self, *args):
        return self.runner.run(["schtasks", *args], timeout=60)

    def run(self) -> int:
        try:
            return self._run()
        finally:
            self.cleanup()

    def _run(self) -> int:
        child = self.workdir / "fake_daemon.py"
        child.write_text(_FAKE_DAEMON, encoding="utf-8")
        args_file = self.workdir / "empty.args"
        args_file.write_text("", encoding="utf-8")
        windowless = self.mode == "user"
        python = svc.ru.supervisor_python(REPO, windowless=windowless)
        if not Path(python).exists():
            base = Path(sys.executable).with_name("pythonw.exe" if windowless else "python.exe")
            python = str(base)
        # Task Scheduler cannot set environment variables, so a two-line launcher sets the fake daemon's
        # PROBE_* and then runs the REAL supervisor in-process (runpy): the supervisor's pid is this
        # launcher's pid, exactly as the real task's supervisor is the task's process.
        child_python = str(Path(python).with_name("python.exe"))  # the real install's child: python.exe
        launcher = self.workdir / "launch.py"
        launcher.write_text(
            "import os, runpy, sys\n"
            f"os.environ['PROBE_STATE'] = {str(self.workdir)!r}\n"
            f"os.environ['PROBE_SCRIPTS'] = {str(HERE)!r}\n"
            f"sys.argv = [{str(HERE / 'seneschald_supervise.py')!r}] + sys.argv[1:]\n"
            f"runpy.run_path({str(HERE / 'seneschald_supervise.py')!r}, run_name='__main__')\n",
            encoding="utf-8")
        arguments = (f'"{launcher}" --task {self.task} --state-dir "{self.workdir}" --script "{child}" '
                     f'--args-file "{args_file}" --python "{child_python}"')
        user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
        xml = self.workdir / "task.xml"
        xml.write_text(task_xml(user=user, logon="InteractiveToken" if windowless else "S4U",
                                command=python, arguments=arguments, workdir=str(REPO)), encoding="utf-16")

        # 1. register
        create = ["/Create", "/TN", self.task, "/XML", str(xml), "/F"]
        r = self.schtasks(*create)
        how = "this token"
        if r.denied and not windowless:
            how = "elevated (one UAC prompt)"
            r = self.runner.run_elevated("schtasks.exe", subprocess_list2cmdline(create), elevate=True)
        self.registered = r.ok
        if not self.step("register", r.ok, how=how, rc=r.rc, err=(r.err or r.out).strip()[:300]):
            return 1

        # 2. start, from this token (and the limited one)
        if self.as_limited:
            self.out["limited_token"]["schtasks_run"] = run_limited(["schtasks", "/Run", "/TN", self.task])
            time.sleep(2)
        r = self.schtasks("/Run", "/TN", self.task)
        self.step("start", r.ok or self.sup_pid() is not None, rc=r.rc, denied=r.denied,
                  err=(r.err or r.out).strip()[:300])

        # 3. verify
        first = self.wait(lambda: self.children() and self.daemon_pid(), 30)
        sup = self.wait(self.sup_pid, 30)
        kids = self.children()
        if not self.step("verify", bool(first and sup and kids), supervisor_pid=sup, daemon=kids[-1] if kids else None,
                         daemon_session=kids[-1]["session"] if kids else None,
                         daemon_console_window=kids[-1]["console_window"] if kids else None,
                         supervisor_image=_image(sup) if sup else None):
            return 1

        # 4. reload: exit 75 -> same supervisor, new daemon
        before = self.daemon_pid()
        t0 = time.monotonic()
        (self.workdir / "probe-reload").write_text("", encoding="utf-8")
        new = self.wait(lambda: (p := self.daemon_pid()) and p != before and p, 15)
        self.step("reload", bool(new) and self.sup_pid() == sup, seconds=round(time.monotonic() - t0, 2),
                  supervisor_unchanged=self.sup_pid() == sup, task_status=self.task_status())

        # 5. crash: kill the daemon -> same supervisor, new daemon after the backoff
        before = self.daemon_pid()
        if self.as_limited and before:
            self.out["limited_token"]["kill_daemon"] = run_limited(["taskkill", "/PID", str(before), "/F"])
            time.sleep(1)
        before = self.daemon_pid()
        t0 = time.monotonic()
        killed = svc._default_kill(before) if before else False
        new = self.wait(lambda: (p := self.daemon_pid()) and p != before and p, 20)
        self.step("crash", bool(killed and new) and self.sup_pid() == sup, seconds=round(time.monotonic() - t0, 2),
                  supervisor_unchanged=self.sup_pid() == sup)

        # 6. stop, through service.py's Windows Stop aimed at the probe task
        if self.as_limited:
            self.out["limited_token"]["schtasks_end"] = run_limited(["schtasks", "/End", "/TN", self.task])
            time.sleep(2)
            self.out["limited_token"]["supervisor_survived_limited_end"] = self.sup_pid() is not None
        lines = []
        ctx = svc.Ctx(platform="win32", repo=REPO, state_dir=self.workdir, home=Path.home(), out=lines.append)
        old = svc.STOP_GRACEFUL_WAIT_SEC
        svc.STOP_GRACEFUL_WAIT_SEC = 2.0  # the fake daemon does not read the control queue
        try:
            rc = svc.WindowsBackend(ctx, task=self.task).stop(argparse.Namespace())
        finally:
            svc.STOP_GRACEFUL_WAIT_SEC = old
        gone = self.wait(lambda: self.sup_pid() is None and self.daemon_pid() is None, 15)
        self.step("stop", rc == 0 and bool(gone), rc=rc, log=lines[-6:])
        return 0 if all(s["ok"] for s in self.out["steps"].values()) else 1

    def task_status(self):
        r = self.schtasks("/Query", "/TN", self.task, "/FO", "LIST")
        for ln in r.out.splitlines():
            if ln.strip().startswith("Status:"):
                return ln.split(":", 1)[1].strip()
        return None

    def cleanup(self):
        if self.sup_pid():
            svc._default_kill(self.sup_pid())
        if self.daemon_pid():
            svc._default_kill(self.daemon_pid())
        if self.registered:
            r = self.schtasks("/Delete", "/TN", self.task, "/F")
            if r.denied:
                r = self.runner.run_elevated("schtasks.exe", f"/Delete /TN {self.task} /F", elevate=True)
            self.out["unregistered"] = r.ok
        if not self.keep_state:
            shutil.rmtree(self.workdir, ignore_errors=True)
        else:
            self.out["state_dir"] = str(self.workdir)


def _image(pid):
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        return os.path.basename(buf.value) if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
    finally:
        k32.CloseHandle(h)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Live probe of the Windows service shape (throwaway task).")
    p.add_argument("--mode", choices=("s4u", "user"), required=True)
    p.add_argument("--as-limited", action="store_true",
                   help="also try start/kill/end from a SAFER normal-user token (elevated hosts only)")
    p.add_argument("--keep-state", action="store_true", help="keep the probe's temp state dir")
    a = p.parse_args(argv)
    if os.name != "nt":
        print(json.dumps({"skipped": "Windows only — systemd and launchd are covered by CI (service-supervision)"}))
        return 0
    probe = Probe(a.mode, a.as_limited, a.keep_state)
    rc = probe.run()
    print(json.dumps(probe.out, indent=2, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
