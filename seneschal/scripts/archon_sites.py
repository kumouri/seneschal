#!/usr/bin/env python3
"""Archon human-facing site supervision — the daemon-side module for presence.py's archon-sites
supervised task (see `seneschal/docs/cockpit-spec.md`'s Archons bullet). Stdlib only (the daemon is
stdlib-first, and this needs none of its sanctioned dependencies).

**What this is for.** An archon with a human-facing web UI (a job-search dashboard, a report viewer)
has no durable server of its own: a scheduled run might rebuild its static files, but actually
serving them over HTTP would otherwise be a manual step. This module gives the resident daemon
(`presence.py`) the pieces to discover which archons declare a UI, health-check it over HTTP, and
(re)spawn it when it's down or wedged — kept here, separate from the task loop itself, so it's
independently unit-testable with no real subprocess/socket (every side-effecting function below takes
an injectable seam: `spawn_site`'s `runner`, `site_healthy`'s own `urllib.request` call is easy to
monkeypatch in tests, `kill_pid`'s `os.kill`).

**Declaration — the archons themselves are the registry.** Per-archon self-declaration: each
UI-having archon ships a tracked `site.json` at its own root (`archons/<id>/site.json` — see
`seneschal/references/archons.md`'s "A UI-having archon also ships a tracked `site.json`").
`discover_sites` scans `<archons_dir>/*/site.json`; a newly-minted UI archon auto-registers with no
central file to edit and no archon name ever hard-coded here. Schema
(`"schema": "seneschal.archon-site/1"`, informational — not enforced):

    {
      "schema": "seneschal.archon-site/1",
      "id": "<id>",
      "enabled": true,
      "port": 9800,
      "health_path": "/",
      "cmd": "python",
      "args": ["tools/<ui_script>.py", "--serve", "--port", "9800"],
      "cwd": "."
    }

`cmd: "python"` is substituted with `sys.executable` (the daemon's own interpreter — same venv) at
spawn time; `cwd` is relative to the archon's own directory (`.` = the archon's root itself).

**Process model — detached survivors, reconcile-not-spawn.** A naive "spawn on daemon startup" would
orphan-and-duplicate on every merge-reload (the daemon's children don't die with it on Windows, and a
freshly-reloaded daemon would spawn a SECOND server that collides on the port). Instead:

  * `spawn_site` launches detached (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows,
    `start_new_session=True` elsewhere — mirroring presence.py's `_respawn_detached`), so a site
    survives a daemon reload.
  * Its PID persists to a gitignored `state/archon-sites.pids.json` (`load_pids`/`save_pids`), so a
    freshly-reloaded daemon ADOPTS what a prior incarnation spawned instead of starting a rival. The
    pids file is pure bookkeeping for `kill_pid` — health is decided over HTTP (`site_healthy`), never
    by whether a remembered PID still resolves to a live process.
  * The daemon task is a RECONCILE loop: each pass it re-discovers sites, health-checks each, and only
    (re)starts one that is actually down or wedged. A reload with healthy sites is a complete no-op —
    no bounce, no duplicate, no orphan. The daemon's own shutdown/reload path never kills these
    processes (they are not added to its headless-children set).
  * The cockpit's per-site Restart button (`POST /api/archons/{id}/restart`) enqueues a `restart-site`
    control; the daemon's control task hands it to this loop, which kills and respawns that one site on
    its next pass.

**Fail-open, tolerant throughout.** Every function here degrades to "nothing to do" rather than raising:
a missing `archons_dir`, a malformed/partial `site.json`, an `enabled: false` entry, a spawn failure, or
killing an already-dead PID are all silently absorbed. A flaky archon site must never take the daemon
(chat, reminders) down with it — the daemon task wraps the lot in its own try/except as well.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

SITE_DESCRIPTOR_FILENAME = "site.json"
PIDS_FILENAME = "archon-sites.pids.json"
LOG_FILENAME = "site.log"
DEFAULT_HEALTH_TIMEOUT_SEC = 2.0

# Windows detached-spawn flags — same values presence.py's `_respawn_detached` uses to relaunch itself
# across a graceful reload. Defined locally (not imported from presence.py) to avoid a circular import
# (presence.py imports this module) and to keep this module's only dependency on presence.py's
# conventions a documented one, not a code one.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def child_env() -> dict:
    """Env for the spawned site process: scrub ANTHROPIC_API_KEY, matching presence.py's `child_env`
    (duplicated here rather than imported — see the module docstring's note on avoiding a circular
    import). A static site server has no Claude billing surface of its own, but it's spawned from the
    same daemon process tree, so this keeps the invariant "nothing under presence.py's supervision ever
    inherits a stray API key" true without exception."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env


@dataclass(frozen=True)
class SiteSpec:
    """One archon's declared human-facing site, resolved from its `site.json`."""

    id: str
    port: int
    health_path: str
    cmd: str
    args: list = field(default_factory=list)
    archon_dir: str = ""   # absolute path to archons/<id> (site.json's own directory)
    cwd_rel: str = "."     # the subprocess's working dir, relative to archon_dir


def _site_descriptor_path(archon_dir: str) -> str:
    return os.path.join(archon_dir, SITE_DESCRIPTOR_FILENAME)


def discover_sites(archons_dir: str) -> list:
    """Scan ``<archons_dir>/*/site.json`` for enabled site descriptors. Tolerant throughout: a missing
    `archons_dir`, an archon directory with no `site.json`, a malformed/partial descriptor, or
    `enabled: false` is silently skipped — never raises. Returns a list of `SiteSpec`, sorted by archon
    id for a deterministic reconcile pass (and deterministic test assertions)."""
    specs: list = []
    try:
        names = sorted(os.listdir(archons_dir))
    except OSError:
        return specs
    for name in names:
        archon_dir = os.path.join(archons_dir, name)
        path = _site_descriptor_path(archon_dir)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if not data.get("enabled", True):
            continue

        port = data.get("port")
        cmd = data.get("cmd")
        args = data.get("args")
        if (not isinstance(port, int) or isinstance(port, bool)
                or not isinstance(cmd, str) or not cmd.strip()
                or not isinstance(args, list)):
            continue  # malformed — skip rather than raise; a bad site.json costs its own site only

        health_path = data.get("health_path", "/")
        if not isinstance(health_path, str) or not health_path.strip():
            health_path = "/"
        elif not health_path.startswith("/"):
            health_path = f"/{health_path}"  # same leading-slash normalization site_healthy applies

        cwd_rel = data.get("cwd", ".")
        if not isinstance(cwd_rel, str) or not cwd_rel.strip():
            cwd_rel = "."

        archon_id = data.get("id")
        if not isinstance(archon_id, str) or not archon_id.strip():
            archon_id = name

        specs.append(SiteSpec(
            id=archon_id,
            port=port,
            health_path=health_path,
            cmd=cmd,
            args=[str(a) for a in args],
            archon_dir=os.path.abspath(archon_dir),
            cwd_rel=cwd_rel,
        ))
    return specs


def _close_quietly(resp) -> None:
    """Close a urllib response. Best-effort and silent — `site_healthy`'s contract is that no failure
    mode escapes it, so a broken close must not become an exception (nor flip a healthy verdict, which
    is why callers close only AFTER deciding)."""
    try:
        resp.close()
    except Exception:  # noqa: BLE001 — closing is advisory; a health probe must never raise
        pass


def site_healthy(port: int, health_path: str = "/", timeout: float = DEFAULT_HEALTH_TIMEOUT_SEC) -> bool:
    """True if ``127.0.0.1:<port><health_path>`` answers with ANY HTTP response — 2xx/3xx/4xx alike,
    since any status at all means something is actually serving there (matches
    cockpit/server/archons.py's `_probe`, same reasoning). Connection-refused or a timed-out request
    (a wedged process that accepts the TCP connection but never answers) both read as unhealthy. Never
    raises — every failure mode collapses to False.

    Both outcomes close the response. That matters most on the error arm: an `HTTPError` IS the response
    (it inherits `urllib.response.addinfourl`, itself a `tempfile._TemporaryFileWrapper`), so leaving it
    unclosed holds its connection until the cycle collector happens to reach it — and since a site
    answering 4xx/5xx counts as healthy here, the reconcile loop re-probes it on every pass, for the
    life of the daemon. The success arm is closed for the same reason, rather than relying on CPython
    refcounting to do it the instant the value is discarded."""
    path = health_path if health_path.startswith("/") else f"/{health_path}"
    url = f"http://127.0.0.1:{port}{path}"
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        _close_quietly(e)
        return True  # any HTTP status, even 4xx/5xx, means the server IS answering
    except Exception:  # noqa: BLE001 — a health probe is advisory; any other failure just reads "down"
        return False
    _close_quietly(resp)
    return True


def spawn_site(spec: SiteSpec, log, runner=subprocess.Popen):
    """Launch `spec`'s server process, detached so it survives a daemon reload (see the module
    docstring's "detached survivors" section). Returns the new PID, or `None` if the spawn itself
    raised (logged, never propagated — the caller's reconcile loop is fail-open by design).

    `runner` is injectable (tests pass a stub instead of really spawning a process) — signature-
    compatible with `subprocess.Popen`: called as `runner(argv, **kwargs)` and expected to expose
    `.pid`."""
    exe = sys.executable if spec.cmd == "python" else spec.cmd
    argv = [exe] + list(spec.args)
    cwd = os.path.normpath(os.path.join(spec.archon_dir, spec.cwd_rel))
    logs_dir = os.path.join(spec.archon_dir, "state", "logs")

    kwargs: dict = {"cwd": cwd, "env": child_env()}
    log_fh = None
    try:
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
        log(f"! archon-site {spec.id}: spawn failed: {e}")
        return None
    finally:
        # Close the PARENT's copy once the child has inherited its own — a site is respawned every time
        # the reconcile loop finds it down or wedged, for the life of the daemon, so holding the handle
        # open would leak one per respawn. Safe on both Windows (`close_fds=True`; Popen duplicates the
        # handle for the child) and POSIX. Mirrors cockpit_site.spawn_backend.
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
    return proc.pid


def pids_path(state_dir: str) -> str:
    return os.path.join(state_dir, PIDS_FILENAME)


def load_pids(state_dir: str) -> dict:
    """``{archon_id: pid}`` — the PID a prior daemon incarnation spawned for each site, so a
    freshly-reloaded daemon adopts rather than duplicates. Tolerant: a missing/corrupt file, or a
    non-dict/non-int entry, reads as absent rather than raising. Pure bookkeeping for `kill_pid` — site
    HEALTH is always decided over HTTP (`site_healthy`), never by whether this PID still resolves to a
    live process."""
    try:
        with open(pids_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)}


def save_pids(state_dir: str, pids: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = pids_path(state_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pids, fh, indent=2)
    os.replace(tmp, path)


def kill_pid(pid) -> None:
    """Best-effort kill of `pid` — tolerant of `pid` being `None`/falsy and of an already-dead pid (no
    such process, access denied, or the PID having since been reused by an unrelated process): every
    failure is swallowed, never raised. Deliberately non-blocking (no wait-then-escalate poll loop):
    this is called directly from the daemon's event loop, never off-loaded to a worker thread, so a
    synchronous multi-second wait here would stall reminders/chat for everyone. On Windows,
    `os.kill(pid, SIGTERM)` already hard-terminates (Python maps SIGTERM to `TerminateProcess` there) —
    correct for the simple static-file HTTP servers this module manages, which have no shutdown state
    worth flushing. On POSIX, SIGTERM is immediately followed by SIGKILL (no wait) for the same
    non-blocking reason — belt and suspenders, since nothing here depends on a graceful shutdown
    succeeding."""
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass  # already dead / no such process / access denied — nothing more to do
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
