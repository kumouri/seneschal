#!/usr/bin/env python3
"""Cockpit supervision — the daemon-side module for presence.py's cockpit-backend supervised task (see
`seneschal/docs/cockpit-spec.md`). Stdlib only, like every other daemon module.

**What this is for.** The cockpit backend (`cockpit/server/app.py`) has no supervisor of its own:
running it means someone typing `uv run uvicorn ...` and leaving a window open. This module gives the
resident daemon the pieces to start it, keep it up, build its frontend, and bounce it onto merged code
— with the same reconcile-not-spawn discipline `archon_sites.py` uses for archon UIs.

**The three-worlds rule still holds.** The cockpit is its own dependency world (cockpit-spec.md
ruling 3): `fastapi`/`uvicorn` live in the `cockpit` extras group, which the daemon's own
`uv sync --frozen` deliberately never installs. So this module NEVER installs anything — it *detects*
(`deps_available`) and degrades. Having run `uv sync --extra cockpit` is itself the opt-in signal that
the cockpit is wanted on this machine; a bare venv just means the supervision task logs one line and
stops.

**Auth: the daemon must not hand out a 503 wall.** `cockpit/server` reads only `os.environ` — nothing
in it loads `cockpit.env` — so a naive spawn inherits no OIDC config, `auth.auth_mode()` falls through
to `"unconfigured"`, and every route but `/api/health` and `/api/auth/status` answers 503. `child_env`
therefore loads `cockpit/server/cockpit.env` when it exists (real OIDC always wins) and, only when it
doesn't, sets `COCKPIT_DEV_NO_AUTH=1` so the dashboard actually works. The exposure that buys is
bounded by what the cockpit already enforces twice over: it binds `127.0.0.1` and rejects any
non-loopback client, so reaching it already means code execution on this machine — and in `dev` mode
the `/api/ws` handshake additionally requires an allowlisted `Origin` (app.py).

**Deploy bounce without touching the shutdown path.** A merged cockpit change has to actually load.
Rather than teach presence.py's reload path (or `seneschald-control.ps1`'s Stop) to kill this process
— which would cover only *some* of the ways the daemon restarts — the spawn records the checkout's git
HEAD alongside the PID, and the reconcile loop bounces a backend whose recorded rev no longer matches
(`read_head_rev`). That covers every path uniformly: a `seneschald-update` graceful reload, a hard
restart, a break-glass force-pull, even a crash-restart — and it is decidable from two files on disk,
so it tests with no processes at all. `read_head_rev` reads `.git` directly and never shells out to
git: a checkout with fsmonitor configured can hang a `git` subprocess, and a supervisor that can hang
is not one.

**The frontend is built here, deliberately.** `cockpit/web/dist` does not exist in a fresh checkout,
and `app.py` mounts it at IMPORT time, so a build that finishes after uvicorn starts is not served
until it restarts. The task therefore spawns first (API + the daemon pipe come up in seconds), builds
after, and bounces once on success. `npm ci` + `npm run build` take minutes on a cold `node_modules`,
so the build is SPLIT: this module only *plans* it (`build_steps`, pure), and presence.py runs the
steps as async subprocesses it can abandon on shutdown. A worker thread would have been simpler and
wrong — `asyncio.run` waits for its default executor at exit, so a `subprocess.run` in a thread would
hold a graceful reload hostage for as long as npm ran. Every failure mode — npm absent, install
failure, build failure, timeout — degrades to "the cockpit serves the API, `/` 404s" and says so,
rather than blocking chat or reminders.

**A third bounce trigger is the backend's log growing oversized** (`log_rotation.py`) — the spawned
uvicorn child holds `state/logs/cockpit.log` open for its whole life, so a rename attempted while it's
alive races Windows's file-locking rule and can only succeed at a kill-then-respawn point this task
already performs for deploys and crash recovery. The roll itself belongs in the ONE respawn branch
all three triggers funnel through, right after `kill_pid`; a declined roll (the child hasn't fully died
yet) changes nothing about the respawn and is retried on the next pass — it never blocks and never
loses a line.

**Fail-open, tolerant throughout** — same posture as `archon_sites.py`: every function here degrades to
"nothing to do" rather than raising, and presence.py's task wraps the lot in its own try/except.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

# Reused from the archon-sites module rather than duplicated: same repo, same dependency world, no
# circular import (archon_sites doesn't know about this module). The cockpit's "duplicate, don't
# import" rule is about crossing INTO the cockpit's separate dependency world — it doesn't apply
# between two daemon-side siblings. The daemon's cockpit task calls `archon_sites.site_healthy` /
# `kill_pid` directly, so only the env scrubber is needed here.
from archon_sites import child_env as _scrubbed_env

DEFAULT_PORT = 8760              # cockpit/README.md's documented backend port
HEALTH_PATH = "/api/health"      # PUBLIC in every auth mode — a probe a 503 wall can't fool
PID_FILENAME = "cockpit-site.pid.json"
LOG_FILENAME = "cockpit.log"
COCKPIT_ENV_RELPATH = os.path.join("cockpit", "server", "cockpit.env")
NPM_TIMEOUT_SEC = 900            # a cold `npm ci` on this repo is minutes, not seconds

# Windows detached-spawn flags — same values presence.py's `_respawn_detached` and archon_sites.py use.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

# Frontend build inputs, relative to cockpit/web. Any of these being newer than the newest file in
# dist/ means the built UI is stale. Deliberately a small explicit list rather than "everything in
# web/" — node_modules alone would make the scan cost real wall time.
_BUILD_INPUTS = ("src", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json",
                 "index.html")


@dataclass(frozen=True)
class BuildStep:
    """One npm invocation the frontend build needs — `argv` ready to hand to a subprocess, plus the
    directory to run it in and a short name for the log line."""

    name: str      # "ci" | "build"
    argv: list
    cwd: str


def deps_available(find_spec=None) -> bool:
    """True if the `cockpit` extras are importable in THIS interpreter (the daemon runs from the same
    `.venv` it would spawn uvicorn from). Uses `find_spec` rather than a real import so a missing
    dependency costs nothing and imports no application code into the daemon; `find_spec` is injectable
    for tests. Any exception (a broken path entry, a half-installed package) reads as unavailable —
    this gates a spawn, so it fails closed."""
    probe = find_spec if find_spec is not None else importlib.util.find_spec
    try:
        return all(probe(mod) is not None for mod in ("fastapi", "uvicorn"))
    except Exception:  # noqa: BLE001 — a broken sys.path must read "no deps", never crash the daemon
        return False


def _git_dir(repo_root: str) -> str | None:
    """Resolve `<repo_root>/.git`, following the `gitdir: <path>` indirection a worktree/submodule
    checkout uses (the live daemon normally runs from a plain clone, but development happens in
    worktrees)."""
    path = os.path.join(repo_root, ".git")
    if os.path.isdir(path):
        return path
    try:
        with open(path, "r", encoding="utf-8") as fh:
            line = fh.read().strip()
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = line.split(":", 1)[1].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(repo_root, gitdir))
    return gitdir if os.path.isdir(gitdir) else None


def read_head_rev(repo_root: str) -> str | None:
    """The checkout's current commit sha, read straight out of `.git` — NEVER via a `git` subprocess
    (a subprocess can hang on fsmonitor, and a supervisor that can hang is not one). Follows `HEAD` ->
    `refs/heads/<branch>`, falling back to `packed-refs` for a branch whose loose ref has been packed
    away. Returns `None` for a detached HEAD it can't resolve, a missing/corrupt `.git`, or anything
    unexpected — and `None` is treated by the caller as "no deploy signal", never as "bounce it"."""
    gitdir = _git_dir(repo_root)
    if not gitdir:
        return None
    try:
        with open(os.path.join(gitdir, "HEAD"), "r", encoding="utf-8") as fh:
            head = fh.read().strip()
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not head.startswith("ref:"):
        return head or None  # detached HEAD — the sha is right there
    ref = head.split(":", 1)[1].strip()
    try:
        with open(os.path.join(gitdir, *ref.split("/")), "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        pass  # loose ref absent — it may be packed
    try:
        with open(os.path.join(gitdir, "packed-refs"), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith(("#", "^")):
                    continue
                sha, _, name = line.partition(" ")
                if name.strip() == ref:
                    return sha or None
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return None


def load_env(env_file: str | None) -> dict:
    """Parse a `KEY=VALUE` env file (the house shape every other script here uses — `#` comments,
    blank lines, optional surrounding quotes). A missing or unreadable file is an empty dict, never an
    error: no `cockpit.env` is the normal state until an OIDC app is provisioned."""
    values: dict = {}
    if not env_file:
        return values
    try:
        with open(env_file, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except (OSError, UnicodeDecodeError):
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def child_env(repo_root: str, state_dir: str, pipe_port: int, base_env: dict | None = None) -> dict:
    """Environment for the spawned uvicorn process.

    Four jobs, in order:

    1. **Scrub `ANTHROPIC_API_KEY`** — the standing invariant that nothing under presence.py's
       supervision inherits a stray API key.
    2. **Point it at THIS daemon's world** — `SENESCHAL_STATE_DIR` and `COCKPIT_PIPE_PORT`, so the
       backend's `PipeClient` finds the pipe (and its token, which it derives from the state dir) even
       when the daemon runs on a non-default `--cockpit-port`. The client already auto-connects on
       lifespan: it just has to be told where.
    3. **Overlay `cockpit/server/cockpit.env`** when it exists — real OIDC config always wins.
    4. **Fall back to `COCKPIT_DEV_NO_AUTH=1`** only when no OIDC client id ended up set, so an
       unprovisioned box gets a working dashboard instead of a 503 wall (see the module docstring).
    """
    env = dict(base_env) if base_env is not None else dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env["SENESCHAL_STATE_DIR"] = state_dir
    env["COCKPIT_PIPE_PORT"] = str(pipe_port)
    env.setdefault("PYTHONUTF8", "1")           # emoji in the transcript vs. Windows' default codepage
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.update(load_env(os.path.join(repo_root, COCKPIT_ENV_RELPATH)))
    if not env.get("COCKPIT_OIDC_CLIENT_ID"):
        env["COCKPIT_DEV_NO_AUTH"] = "1"
    return env


def web_dir(repo_root: str) -> str:
    return os.path.join(repo_root, "cockpit", "web")


def dist_dir(repo_root: str) -> str:
    return os.path.join(web_dir(repo_root), "dist")


def _newest_mtime(path: str) -> float:
    """Newest mtime at or under `path` (0.0 if it doesn't exist / can't be read). Tolerant of a file
    vanishing mid-walk — a concurrent build is exactly when this gets called."""
    try:
        if os.path.isfile(path):
            return os.path.getmtime(path)
    except OSError:
        return 0.0
    newest = 0.0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, name)))
            except OSError:
                continue
    return newest


def build_needed(repo_root: str) -> bool:
    """True if the built UI is missing or older than its sources. `index.html`'s absence counts as
    missing even when `dist/` exists — that's the file `app.py`'s `/` route serves, and a half-written
    dist from an interrupted build is worse than none."""
    dist = dist_dir(repo_root)
    if not os.path.isfile(os.path.join(dist, "index.html")):
        return True
    built = _newest_mtime(dist)
    if not built:
        return True
    web = web_dir(repo_root)
    return any(_newest_mtime(os.path.join(web, rel)) > built for rel in _BUILD_INPUTS)


def build_steps(repo_root: str, which=shutil.which) -> list:
    """Plan the frontend build: `npm ci` (ONLY when `node_modules` is absent — it's the slow one) then
    `npm run build`, both in `cockpit/web`. Returns `[]` when there's nothing to run or nothing to run
    it with (no `package.json`, npm not on PATH) — an empty plan is a normal, non-fatal outcome that
    leaves the cockpit serving the API with `/` 404ing, which `GET /api/health`'s `web_dist_present`
    already reports honestly.

    Deliberately PURE — it plans, it doesn't execute. The execution belongs in the daemon, which runs
    these as async subprocesses so a multi-minute `npm ci` can never block the event loop or stall the
    daemon's graceful reload (a `subprocess.run` in a worker thread would: `asyncio.run` waits for its
    default executor at shutdown). Keeping the plan pure is also what makes it testable without npm —
    `which` is injectable."""
    cwd = web_dir(repo_root)
    if not os.path.isfile(os.path.join(cwd, "package.json")):
        return []
    npm = which("npm")
    if not npm:
        return []
    steps = []
    if not os.path.isdir(os.path.join(cwd, "node_modules")):
        steps.append(BuildStep("ci", [npm, "ci"], cwd))
    steps.append(BuildStep("build", [npm, "run", "build"], cwd))
    return steps


def npm_available(repo_root: str, which=shutil.which) -> bool:
    """Whether a build is even possible here — used for the honest one-line startup log when it isn't."""
    return bool(which("npm")) and os.path.isfile(os.path.join(web_dir(repo_root), "package.json"))


def build_env() -> dict:
    """Environment for the npm build steps: the daemon's own, with ANTHROPIC_API_KEY scrubbed — the
    same invariant `spawn_backend` holds, applied to the build's process tree too."""
    return _scrubbed_env()


def spawn_backend(repo_root: str, state_dir: str, port: int, pipe_port: int, log,
                  runner=subprocess.Popen):
    """Launch `uvicorn cockpit.server.app:app` on `127.0.0.1:<port>`, detached so it survives a daemon
    reload (the reconcile loop adopts it via the PID file rather than starting a rival). Returns the new
    PID, or `None` if the spawn itself raised — logged, never propagated.

    Runs as `<this interpreter> -m uvicorn` rather than `uv run uvicorn`: same venv, one less process in
    the tree, and no chance of uv re-resolving the environment underneath the daemon. No `--reload` —
    that's a dev-loop convenience whose file watcher has no business in a supervised process."""
    argv = [sys.executable, "-m", "uvicorn", "cockpit.server.app:app",
            "--host", "127.0.0.1", "--port", str(port)]
    kwargs: dict = {"cwd": repo_root, "env": child_env(repo_root, state_dir, pipe_port)}
    log_fh = None
    try:
        logs_dir = os.path.join(state_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        log_fh = open(os.path.join(logs_dir, LOG_FILENAME), "a", encoding="utf-8")
        kwargs["stdout"] = log_fh
        kwargs["stderr"] = log_fh
    except OSError:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL

    if os.name == "nt":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True

    try:
        proc = runner(argv, **kwargs)
    except OSError as e:
        log(f"! cockpit: spawn failed: {e}")
        return None
    finally:
        # Close the PARENT's copy once the child has inherited its own — this backend is respawned on
        # every merge for the life of the daemon, so holding the handle open would leak one per deploy.
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
    return proc.pid


def pid_path(state_dir: str) -> str:
    return os.path.join(state_dir, PID_FILENAME)


def load_record(state_dir: str) -> dict:
    """`{"pid": int, "rev": str|None, "port": int}` — what a prior daemon incarnation spawned, so a
    reloaded daemon ADOPTS it instead of starting a rival (the cockpit pipe serves exactly one client;
    two backends would fight over it). Tolerant: a missing/corrupt file, or a non-dict, reads as empty.
    Health is always decided over HTTP, never by whether this PID still resolves."""
    try:
        with open(pid_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return {}
    rev = data.get("rev")
    port = data.get("port")
    return {
        "pid": pid,
        "rev": rev if isinstance(rev, str) else None,
        "port": port if isinstance(port, int) and not isinstance(port, bool) else None,
    }


def save_record(state_dir: str, pid: int, rev: str | None, port: int) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = pid_path(state_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"pid": pid, "rev": rev, "port": port}, fh, indent=2)
    os.replace(tmp, path)


def clear_record(state_dir: str) -> None:
    try:
        os.remove(pid_path(state_dir))
    except OSError:
        pass
