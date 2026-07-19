"""The two break-glass actions (cockpit-spec.md "Break-glass"): `restart` and `force-pull`. Both end in
the SAME hard restart — kill the daemon by its `presence.lock` PID, clear the lock, relaunch via
`run-presence.cmd` — mirroring `seneschal/scripts/seneschald-control.ps1 -Action Restart`'s semantics in Python
(no PowerShell / Task Scheduler / admin dependency here, so the supervisor works even if the scheduled
task itself is wedged). `force-pull` additionally force-syncs the live checkout to
`origin/<deploy_branch>` before that restart — the recovery for a pull that can't fast-forward
(`seneschald-update`'s one class of unrecoverable failure, cockpit-spec.md "Break-glass").

Every real side effect (kill a PID, delete a file, spawn a detached process, run `git`/`uv`) goes
through a `Runner`, so tests inject a `RecordingRunner` and assert the EXACT command sequence without
ever touching a real process, a real git repo, or a real `presence.lock` file. **This module must never
execute anything for real during `python -m unittest`** — only `supervisor.py`'s `main()` ever
constructs a `DefaultRunner`.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Protocol


@dataclass
class BreakglassConfig:
    repo_root: Path
    state_dir: Path
    run_presence_cmd: Path
    lock_filename: str = "presence.lock"
    deploy_branch: str = "main"

    @property
    def lock_path(self) -> Path:
        return Path(self.state_dir) / self.lock_filename


@dataclass
class RunResult:
    args: List[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def kill_pid(self, pid: int) -> bool: ...

    def remove_file(self, path: Path) -> None: ...

    def spawn_detached(self, args: List[str], cwd: Optional[Path] = None) -> None: ...

    def run(self, args: List[str], cwd: Optional[Path] = None) -> RunResult: ...


class DefaultRunner:
    """The real thing. Only ever constructed by `supervisor.py`'s `main()` — never by a test."""

    def kill_pid(self, pid: int) -> bool:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except (OSError, ProcessLookupError):
            return False

    def remove_file(self, path: Path) -> None:
        try:
            Path(path).unlink()
        except OSError:
            pass

    def spawn_detached(self, args: List[str], cwd: Optional[Path] = None) -> None:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(  # noqa: S603 — args are fixed by BreakglassConfig, never user input
            args, cwd=str(cwd) if cwd else None, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **kwargs,
        )

    def run(self, args: List[str], cwd: Optional[Path] = None) -> RunResult:
        proc = subprocess.run(  # noqa: S603
            args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=120,
        )
        return RunResult(args=list(args), returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


class RecordingRunner:
    """Test double — records every call as a tuple in `self.calls`, executes NOTHING for real. This is
    what `test_actions.py` asserts the exact command sequence against."""

    def __init__(self, kill_ok: bool = True, run_returncode: int = 0):
        self.calls: list[tuple] = []
        self._kill_ok = kill_ok
        self._run_returncode = run_returncode

    def kill_pid(self, pid: int) -> bool:
        self.calls.append(("kill_pid", pid))
        return self._kill_ok

    def remove_file(self, path: Path) -> None:
        self.calls.append(("remove_file", Path(path)))

    def spawn_detached(self, args: List[str], cwd: Optional[Path] = None) -> None:
        self.calls.append(("spawn_detached", list(args), Path(cwd) if cwd else None))

    def run(self, args: List[str], cwd: Optional[Path] = None) -> RunResult:
        self.calls.append(("run", list(args), Path(cwd) if cwd else None))
        return RunResult(args=list(args), returncode=self._run_returncode)


def _read_lock_pid(lock_path: Path) -> Optional[int]:
    try:
        with open(lock_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    pid = data.get("pid") if isinstance(data, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def do_restart(cfg: BreakglassConfig, runner: Runner) -> dict:
    """Kill the daemon by its `presence.lock` PID, clear the lock, relaunch via `run-presence.cmd` —
    `seneschald-control.ps1 -Action Restart`'s semantics, without any Task Scheduler / admin dependency."""
    steps: List[str] = []
    pid = _read_lock_pid(cfg.lock_path)
    if pid is not None:
        runner.kill_pid(pid)
        steps.append(f"killed pid {pid}")
    else:
        steps.append("no pid in presence.lock (already stopped?)")
    runner.remove_file(cfg.lock_path)
    steps.append("cleared presence.lock")
    runner.spawn_detached(["cmd", "/c", str(cfg.run_presence_cmd)], cwd=cfg.repo_root)
    steps.append(f"relaunched {cfg.run_presence_cmd.name}")
    return {"ok": True, "action": "restart", "steps": steps}


def do_force_pull(cfg: BreakglassConfig, runner: Runner) -> dict:
    """`git fetch origin` + `git reset --hard origin/<deploy_branch>` + `uv sync --frozen`
    (best-effort — its outcome is reported but never blocks the restart) + the same hard restart as
    `do_restart`. Destructive ONLY to uncommitted tracked changes in the live checkout — `state/` is
    gitignored and survives (cockpit-spec.md "Break-glass": this is exactly the failure it exists to
    clear, the same one `seneschald-update`'s `pull-failed` reason flags)."""
    steps: List[str] = []
    fetch = runner.run(["git", "-c", "core.fsmonitor=false", "fetch", "origin"], cwd=cfg.repo_root)
    steps.append(f"git fetch origin -> {fetch.returncode}")
    reset = runner.run(
        ["git", "-c", "core.fsmonitor=false", "reset", "--hard", f"origin/{cfg.deploy_branch}"],
        cwd=cfg.repo_root,
    )
    steps.append(f"git reset --hard origin/{cfg.deploy_branch} -> {reset.returncode}")
    sync = runner.run(["uv", "sync", "--frozen"], cwd=cfg.repo_root)
    steps.append(f"uv sync --frozen -> {sync.returncode} (best-effort)")

    restart = do_restart(cfg, runner)
    steps.extend(restart["steps"])
    return {"ok": fetch.returncode == 0 and reset.returncode == 0, "action": "force-pull", "steps": steps}
