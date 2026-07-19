#!/usr/bin/env python3
"""The /doctor board — an end-to-end health check over every configured surface.

One row per surface (auth, model dials, each env-manifest entry, the store, the venv,
Ollama, the daemon, the session hooks, the persona/owner artifacts), each GREEN / YELLOW /
RED / SKIP with a one-line detail and a one-line remediation pointing at the chapter that
fixes it (e.g. ``-> /setup env:telegram``). The board is the deterministic half of
``/doctor``; the in-session half (MCP tool probes — Notion / calendar / Slack auth is
per-session and invisible to a subprocess) lives in ``.claude/commands/doctor.md`` and the
wizard's ``subagents/setup/chapters/verify.md``.

Contracts:
  * **Stdlib only**, tri-platform (win32 / linux / darwin), identity-neutral.
  * **No secret value ever appears in any output.** Env checks go through
    ``setup_env.check`` (names only); auth checks report *set / not set*; child-process
    output is never echoed into a detail line.
  * **No PowerShell spawns, ever.** Some Windows hosts wedge hard on ``powershell``/
    ``pwsh`` child processes (AMSI inspection stalls the spawn), so every Windows probe
    uses ``os.environ``, ``schtasks``, or plain ``cmd.exe`` — which also means the
    Windows auth check can only see *this shell's* environment (a ``setx`` from another
    window shows up in fresh shells only; the board says so rather than guessing).
  * Exit code = the number of RED rows (0 = nothing broken). ``--json`` for machines.
  * Optional features degrade, so their failures are YELLOW, never RED (Ollama down,
    venv absent, daemon not registered). RED is reserved for *configured-or-required
    things that are broken* (required env missing, an enabled feature's file gone, a
    model-dial config that would crash the warm spawn).

CLI::

    python seneschal/scripts/setup_doctor.py [--json] [--probe] [--send-test]
    python seneschal/scripts/setup_doctor.py --probe-model <id>     # one-off model probe
    python seneschal/scripts/setup_doctor.py --root D --state-dir D --manifest F  # tests

``--probe`` spawns ``claude -p "ok" --model <id> --max-turns 1`` per unique configured
dial (~120 s timeout each; ``ANTHROPIC_API_KEY`` scrubbed so the probe bills the
subscription, never the metered API). ``--send-test`` sends a REAL Telegram message —
the flag just does it; confirming with the owner first is the *caller's* job
(the /doctor command and the verify chapter both gate it).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import model_config  # noqa: E402
import setup_env  # noqa: E402
import setup_state  # noqa: E402

REPO_ROOT = HERE.parents[1]

GREEN, YELLOW, RED, SKIP = "GREEN", "YELLOW", "RED", "SKIP"
_ORDER = {GREEN: 0, SKIP: 0, YELLOW: 1, RED: 2}

# ASCII on purpose — Windows consoles with legacy codepages must render the board
# (same ruling as setup_state.GLYPHS).
GLYPHS = {GREEN: "[+]", YELLOW: "[!]", RED: "[x]", SKIP: "[ ]"}

DEFAULT_OLLAMA_URL = "http://localhost:11434"
HEALTH_FRESH_MINUTES = 30
PROBE_TIMEOUT = 120.0

# ---------------------------------------------------------------------------- seams
# Tiny wrappers around the only two effectful primitives, so tests monkeypatch these
# instead of the stdlib.


def _run(cmd: list[str], timeout: float = 30.0, env: dict | None = None) -> tuple[int | None, str, str]:
    """Run a child process, captured. Returns (rc, stdout, stderr); rc None = could not
    run at all (missing tool / timeout — stderr says which). Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env,
            encoding="utf-8", errors="replace",
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return None, "", f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return None, "", f"timeout after {timeout:.0f}s: {cmd[0]}"
    except OSError as exc:
        return None, "", f"cannot run {cmd[0]}: {exc}"


def _http_get(url: str, timeout: float = 3.0) -> str:
    """GET a URL, return the body text. Raises on any failure (callers catch broadly)."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _pid_alive(pid: int) -> bool:
    """Is a PID alive? POSIX: signal 0. Windows: OpenProcess via ctypes — NEVER
    os.kill(pid, 0) there (on Windows that *terminates* the process)."""
    if pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------- helpers


def _row(rid: str, status: str, detail: str, fix: str | None = None, notes: list[str] | None = None) -> dict:
    row = {"id": rid, "status": status, "detail": detail, "fix": fix or ""}
    if notes:
        row["notes"] = notes
    return row


def _worst(a: str, b: str) -> str:
    return a if _ORDER[a] >= _ORDER[b] else b


def _parse_iso(ts) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _minutes_old(ts) -> float | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def _load_json(path: Path):
    """(data, err): data None when absent; err set when present-but-unparseable."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"not valid JSON ({exc})"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, None


def _resolve_path(root: Path, p: str) -> Path:
    """Store-config path resolution — mirror of presence._resolve_store_path: ~ expanded,
    relative taken against the repo root, forward slashes fine on Windows."""
    q = os.path.expanduser(p)
    if not os.path.isabs(q):
        q = os.path.join(str(root), q)
    return Path(os.path.normpath(q))


def _dep_map(entry: dict) -> dict[str, str]:
    """The entry's dependency ids -> why (string deps carry no why)."""
    out: dict[str, str] = {}
    for dep in entry.get("depends", []) or []:
        if isinstance(dep, str):
            out[dep] = ""
        elif isinstance(dep, dict) and dep.get("id"):
            out[dep["id"]] = dep.get("why", "")
    return out


def _chapter_status(ctx, chapter: str) -> str:
    return ctx.ledger.get("chapters", {}).get(chapter, {}).get("status", "pending")


def build_ctx(root=None, state_dir=None, manifest=None, home=None, platform=None, environ=None):
    """All the paths + facts the checks read, overridable for tests."""
    root = Path(root) if root else REPO_ROOT
    state_dir = Path(state_dir) if state_dir else root / "seneschal" / "state"
    manifest_path = Path(manifest) if manifest else root / "seneschal" / "setup" / "env-manifest.json"
    manifest_error = None
    try:
        manifest_data = setup_env.load_manifest(manifest_path)
    except setup_env.SetupEnvError as exc:
        manifest_data = {"entries": []}
        manifest_error = str(exc)
    return SimpleNamespace(
        root=root,
        state_dir=state_dir,
        manifest=manifest_data,
        manifest_error=manifest_error,
        ledger=setup_state.load(state_dir / "setup-state.json"),
        home=Path(home) if home else Path.home(),
        platform=platform or sys.platform,
        environ=environ if environ is not None else os.environ,
    )


# ---------------------------------------------------------------------------- checks


def check_auth(ctx) -> dict:
    """1 — the daemon's subscription token + ANTHROPIC_API_KEY hygiene. Presence only."""
    token = bool(str(ctx.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")).strip())
    if ctx.platform == "win32":
        # os.environ is all we can read without a PowerShell spawn (see module docstring):
        # a user-level `setx` from another window only shows in shells opened after it.
        if token:
            status, detail = GREEN, "CLAUDE_CODE_OAUTH_TOKEN present in this shell's environment"
        else:
            status = YELLOW
            detail = ("CLAUDE_CODE_OAUTH_TOKEN not visible in this shell (a setx elsewhere only shows "
                      "in fresh shells) - unattended daemon runs need it")
    else:
        env_file = ctx.home / ".config" / "seneschal" / "daemon.env"
        try:
            text = env_file.read_text(encoding="utf-8")
        except OSError:
            text = None
        has_line = bool(text) and any(
            line.startswith("CLAUDE_CODE_OAUTH_TOKEN=") and len(line) > len("CLAUDE_CODE_OAUTH_TOKEN=")
            for line in text.splitlines()
        )
        if has_line:
            status, detail = GREEN, "~/.config/seneschal/daemon.env present with CLAUDE_CODE_OAUTH_TOKEN set"
            # Mode bits only mean something on a real POSIX filesystem (tests may force a
            # POSIX ctx.platform on Windows, where chmod is a no-op).
            if os.name == "posix":
                try:
                    mode = os.stat(env_file).st_mode & 0o777
                    if mode & 0o077:
                        status = YELLOW
                        detail += f" but mode is {oct(mode)[-3:]} (chmod 600 it)"
                except OSError:
                    pass
        elif token:
            status = YELLOW
            detail = ("token in this shell's environment only - the daemon sources "
                      "~/.config/seneschal/daemon.env, which is absent/empty")
        else:
            status = YELLOW
            detail = "~/.config/seneschal/daemon.env absent - unattended daemon runs need the token"
    if str(ctx.environ.get("ANTHROPIC_API_KEY", "")).strip():
        status = _worst(status, YELLOW)
        detail += "; ANTHROPIC_API_KEY is set - bare `claude` runs bill the METERED API, not the subscription"
    elif ctx.platform != "win32" and os.name == "posix":
        # POSIX persistence check: profile files that would re-export it (names only).
        profiles = [".bashrc", ".zshrc", ".profile", ".bash_profile"]
        hits = []
        for name in profiles:
            try:
                if "ANTHROPIC_API_KEY" in (ctx.home / name).read_text(encoding="utf-8", errors="replace"):
                    hits.append(name)
            except OSError:
                continue
        if hits:
            status = _worst(status, YELLOW)
            detail += (f"; ANTHROPIC_API_KEY appears in {', '.join(hits)} - "
                       "bare `claude` runs bill the METERED API")
    return _row("auth", status, detail, None if status == GREEN else "-> /setup auth-models")


def probe_model(model_id: str, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """One live `claude -p "ok" --model <id> --max-turns 1` probe (subscription-billed:
    ANTHROPIC_API_KEY is scrubbed from the child env). Returns (ok, secret-safe detail)."""
    exe = _which("claude")
    if not exe:
        return False, "claude CLI not found on PATH"
    cmd = [exe, "-p", "ok", "--model", model_id, "--max-turns", "1"]
    if exe.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/c"] + cmd  # CreateProcess can't exec a .cmd shim directly
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    rc, out, err = _run(cmd, timeout=timeout, env=env)
    if rc == 0:
        return True, "ok"
    if rc is None:
        return False, err  # "timeout after Ns" / "not found" — already secret-safe
    tail = (err or out).strip().splitlines()
    return False, (tail[-1][:160] if tail else f"exit {rc}")


def check_models(ctx, probe: bool = False) -> dict:
    """2 — the two model dials: parse + validate; optionally live-probe each unique id."""
    path = ctx.state_dir / model_config.CONFIG_FILE
    fix = "-> /setup auth-models"
    if not path.is_file():
        return _row("models", YELLOW, "dials unset - daemon falls back to --model", fix)
    data, err = _load_json(path)
    if data is None:
        return _row("models", RED, f"state/{model_config.CONFIG_FILE} {err or 'unreadable'}", fix)
    warm, ceiling = data.get("warm_model"), data.get("max_routable_model")
    ok, verr = model_config.validate_pair(warm, ceiling)
    if not ok:
        return _row("models", RED, f"invalid dials: {verr}", fix)
    warm_c, ceiling_c = model_config.canonical(warm), model_config.canonical(ceiling)
    detail = f"warm={warm_c} ceiling={ceiling_c}"
    if not probe:
        return _row("models", GREEN, detail)
    failures = []
    probed = []
    for mid in dict.fromkeys([warm_c, ceiling_c]):  # unique, order kept
        p_ok, p_detail = probe_model(mid)
        probed.append(mid)
        if not p_ok:
            failures.append(f"{mid}: {p_detail}")
    if failures:
        return _row("models", RED, f"{detail}; probe FAILED - {'; '.join(failures)}", fix)
    return _row("models", GREEN, f"{detail}; probed ok: {', '.join(probed)}")


def check_env(ctx) -> list[dict]:
    """3 — manifest-driven env surfaces (names only, never values)."""
    rows: list[dict] = []
    if ctx.manifest_error:
        return [_row("env", RED, f"env manifest unreadable: {ctx.manifest_error}", "fix seneschal/setup/env-manifest.json")]
    for entry in ctx.manifest.get("entries", []):
        if entry.get("handled_by"):
            continue  # owned by another chapter's row (cockpit / mcp), not the env walker's
        eid = entry.get("id", "?")
        rid = f"env:{eid}"
        fix = f"-> /setup {rid}"
        fname = Path(entry.get("path", eid)).name
        try:
            res = setup_env.check(entry, ctx.root)
        except Exception as exc:  # a broken entry shouldn't sink the whole board
            rows.append(_row(rid, RED, f"check failed: {exc}", fix))
            continue
        status_l = _chapter_status(ctx, rid)
        if res["ok"]:
            rows.append(_row(rid, GREEN, f"{fname} present, required vars set"))
        elif status_l == "declined":
            rows.append(_row(rid, SKIP, f"declined in setup ({fname} not expected)", fix))
        elif not res["exists"]:
            if entry.get("required"):
                rows.append(_row(rid, RED, f"{fname} missing (required core channel)", fix))
            elif status_l == "done":
                rows.append(_row(rid, RED, f"{fname} missing but setup marked {rid} done", fix))
            else:
                rows.append(_row(rid, SKIP, f"not configured (optional; {fname} absent)", fix))
        else:
            rows.append(_row(rid, RED, f"{fname} incomplete - missing: {', '.join(res['missing'])}", fix))
    return rows


def check_store(ctx) -> dict:
    """4 — the store config + (for Notion) MCP wiring evidence. Tool AUTH is per-session,
    so file-level GREEN still gets re-verified in-session by /doctor."""
    cfg_path = ctx.root / "seneschal" / "store" / "config.json"
    if not cfg_path.is_file():
        return _row("store", YELLOW, "no store configured - assistant runs storeless", "-> /setup store")
    cfg, err = _load_json(cfg_path)
    if cfg is None:
        return _row("store", RED, f"store/config.json {err or 'unreadable'}", "-> /setup store")
    active = cfg.get("active")
    backends = cfg.get("backends") if isinstance(cfg.get("backends"), dict) else {}
    if not isinstance(active, str) or not active:
        return _row("store", RED, "store/config.json names no active backend", "-> /setup store")
    if active != "notion":
        return _row("store", GREEN, f"{active} backend (filesystem - no MCP needed)")
    backend = backends.get("notion") if isinstance(backends.get("notion"), dict) else {}
    mcp_rel = backend.get("mcp_config")
    mcp_ok = bool(mcp_rel) and _resolve_path(ctx.root, str(mcp_rel)).is_file()
    hint = False
    try:  # user-scope registration hint (Path A): the Claude Code user config names notion
        hint = "notion" in (ctx.home / ".claude.json").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        pass
    if mcp_ok or hint:
        via = "mcp config present" if mcp_ok else "user-scope registration hint"
        return _row("store", GREEN, f"notion backend ({via}; tool auth is per-session - /doctor re-probes)")
    return _row("store", YELLOW, "notion backend but no MCP wiring found - verify in-session via /doctor",
                "-> /setup mcp:notion")


def check_venv(ctx) -> dict:
    """5 — the uv venv (websockets; + fastapi/uvicorn + the built web dist when the
    cockpit feature is enabled). Everything here degrades, so worst case is YELLOW."""
    cockpit_on = bool(ctx.ledger.get("features", {}).get("cockpit"))
    vpy = ctx.root / ".venv" / ("Scripts/python.exe" if ctx.platform == "win32" else "bin/python")
    if not vpy.is_file():
        detail = "no .venv - Discord gateway degrades to REST polling; owner-tz math uses the machine clock"
        if cockpit_on:
            detail += "; cockpit backend cannot run"
        return _row("venv", YELLOW, detail, "run: uv sync" + (" --extra cockpit" if cockpit_on else ""))
    problems = []
    rc, _, _ = _run([str(vpy), "-c", "import websockets"], timeout=30.0)
    if rc != 0:
        problems.append("websockets not importable (Discord gateway degrades to REST polling) - run: uv sync")
    if cockpit_on:
        rc2, _, _ = _run([str(vpy), "-c", "import fastapi, uvicorn"], timeout=30.0)
        if rc2 != 0:
            problems.append("cockpit enabled but fastapi/uvicorn missing - run: uv sync --extra cockpit")
        if not (ctx.root / "cockpit" / "web" / "dist" / "index.html").is_file():
            problems.append("cockpit web dist missing - run: cd cockpit/web && npm ci && npm run build")
    if problems:
        return _row("venv", YELLOW, "; ".join(problems), "-> /setup cockpit" if cockpit_on else "run: uv sync")
    detail = ".venv ok (websockets importable)"
    if cockpit_on:
        detail += "; cockpit extras + web dist present"
    return _row("venv", GREEN, detail)


def check_ollama(ctx) -> dict:
    """6 — only when an enabled feature depends on Ollama. Down is YELLOW, never RED:
    every dependent feature degrades safely (the manifest's depends[].why says how)."""
    enabled = []
    for entry in ctx.manifest.get("entries", []):
        deps = _dep_map(entry)
        if "ollama" in deps and _chapter_status(ctx, f"env:{entry['id']}") == "done":
            enabled.append((entry, deps["ollama"]))
    if not enabled:
        return _row("ollama", SKIP, "no Ollama-dependent features enabled")
    ids = ", ".join(e["id"] for e, _ in enabled)
    url = str(ctx.ledger.get("deps", {}).get("ollama", {}).get("url") or "").strip()
    if not url:
        for entry, _ in enabled:  # an env-file override, when one exists
            target = ctx.root / entry["path"]
            if target.is_file():
                try:
                    url = setup_env.parse_env_text(target.read_text(encoding="utf-8")).get("OLLAMA_URL", "").strip()
                except OSError:
                    url = ""
                if url:
                    break
    url = url or DEFAULT_OLLAMA_URL
    notes = [f"{e['id']}: {why}" for e, why in enabled if why]
    try:
        body = _http_get(url.rstrip("/") + "/api/tags", timeout=3.0)
        models = json.loads(body).get("models", [])
        names = [m.get("name", "") for m in models if isinstance(m, dict)]
    except Exception:
        return _row("ollama", YELLOW,
                    f"Ollama unreachable at {url} - enabled features degrade safely ({ids})",
                    "start Ollama (see the enabled features' setup docs)", notes=notes)
    required: list[str] = []
    for entry, _ in enabled:
        for m in entry.get("ollama_models", []) or []:
            if m not in required:
                required.append(m)
    missing = [m for m in required
               if not any(n == m or n.split(":", 1)[0] == m for n in names)]
    if missing:
        return _row("ollama", YELLOW,
                    f"reachable at {url} but models not pulled: {', '.join(missing)}",
                    f"ollama pull {missing[0]}", notes=notes)
    have = ", ".join(required) if required else "none required"
    return _row("ollama", GREEN, f"reachable at {url}; models present: {have}")


def check_daemon(ctx) -> dict:
    """7 — is the daemon registered / running / freshly stamped? Platform-appropriate,
    best-effort, never RED (the daemon is start-by-hand until its chapter lands)."""
    fix = "-> /setup daemon"
    registered: bool | None = None
    reg_note = ""
    if ctx.platform == "win32":
        rc1, _, _ = _run(["schtasks", "/query", "/tn", "seneschald"], timeout=15.0)
        rc2, _, _ = _run(["schtasks", "/query", "/tn", "seneschald-update"], timeout=15.0)
        if rc1 is not None:
            registered = rc1 == 0
            if registered and rc2 != 0:
                reg_note = "task seneschald registered but seneschald-update missing (merges won't auto-deploy)"
            elif registered:
                reg_note = "tasks seneschald + seneschald-update registered"
            else:
                reg_note = "scheduled task seneschald not registered"
    elif ctx.platform == "darwin":
        rc, out, _ = _run(["launchctl", "list"], timeout=15.0)
        if rc is not None:
            # The daemon chapter's agent label is com.seneschal.presence (render_units.py);
            # also accept a hand-rolled "seneschald" label.
            registered = "com.seneschal." in out or "seneschald" in out
            reg_note = "launchd agent com.seneschal.presence " + ("loaded" if registered else "not loaded")
    else:
        rc, out, _ = _run(["systemctl", "--user", "is-active", "seneschald.service"], timeout=15.0)
        if rc is not None:
            registered = rc == 0
            reg_note = "seneschald.service " + ("active" if registered else "not active")

    lock_pid, lock_state = None, "absent"  # absent | alive | dead | unreadable
    lock, lerr = _load_json(ctx.state_dir / "presence.lock")
    if lerr:
        lock_state = "unreadable"
    elif lock is not None:
        lock_pid = lock.get("pid")
        lock_state = "alive" if isinstance(lock_pid, int) and _pid_alive(lock_pid) else "dead"

    health, herr = _load_json(ctx.state_dir / "seneschald-health.json")
    health_fresh: bool | None = None  # None = no stamp to judge
    age = None
    if health is not None:
        age = _minutes_old(health.get("updated_at"))
        health_fresh = age is not None and age < HEALTH_FRESH_MINUTES

    parts = [p for p in (reg_note,) if p]
    if lock_state == "alive":
        parts.append(f"daemon running (pid {lock_pid})")
    elif lock_state == "dead":
        parts.append(f"stale presence.lock (pid {lock_pid} not running)")
    elif lock_state == "unreadable":
        parts.append("presence.lock unreadable")
    if health_fresh is True:
        parts.append("health stamp fresh")
    elif health_fresh is False:
        parts.append(f"health stamp stale ({age:.0f} min old)" if age is not None else "health stamp unreadable")

    if lock_state == "alive":
        status = GREEN if health_fresh is not False else YELLOW
    elif lock_state in ("dead", "unreadable"):
        status = YELLOW
    elif registered is True:
        status = YELLOW
        parts.append("registered but not running (no live presence.lock)")
    elif registered is False:
        status = YELLOW
        parts.append("not running")
    else:
        if health_fresh is None and lock_state == "absent":
            return _row("daemon", SKIP, "no daemon evidence (service tooling unavailable)", fix)
        status = YELLOW
    return _row("daemon", status, "; ".join(parts) or "no daemon evidence",
                None if status == GREEN else fix)


def check_hooks(ctx) -> dict:
    """8 — the machine-wide session_stamp.py hook in the USER's ~/.claude/settings.json
    (string search only — no JSON surgery on a file this tool doesn't own)."""
    path = ctx.home / ".claude" / "settings.json"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _row("hooks", YELLOW, "no ~/.claude/settings.json - session_stamp.py hook not installed",
                    "-> /setup daemon")
    if "session_stamp.py" in text:
        return _row("hooks", GREEN, "session_stamp.py hook wired in ~/.claude/settings.json")
    return _row("hooks", YELLOW, "session_stamp.py hook not found in ~/.claude/settings.json",
                "-> /setup daemon")


def check_artifacts(ctx) -> list[dict]:
    """9 — persona + owner artifacts. Optional-but-recommended: absent persona means the
    default-Claude persona is active, which is a fine way to run — the row says so."""
    rows = []
    identity = (ctx.root / "persona" / "identity.json").is_file()
    persona = (ctx.root / "persona" / "persona.md").is_file()
    if identity and persona:
        rows.append(_row("persona", GREEN, "custom persona built (identity.json + persona.md)"))
    else:
        missing = [n for n, ok in (("identity.json", identity), ("persona.md", persona)) if not ok]
        rows.append(_row("persona", YELLOW,
                         f"no custom persona ({', '.join(missing)} absent) - the default-Claude persona is active, which is fine",
                         "-> /setup persona"))
    if (ctx.root / "persona" / "owner-profile.md").is_file():
        rows.append(_row("owner-profile", GREEN, "owner-profile.md present"))
    else:
        rows.append(_row("owner-profile", YELLOW,
                         "owner-profile.md absent - the assistant knows nothing about its owner yet",
                         "-> /setup owner-interview"))
    return rows


def run_send_test(ctx) -> dict:
    """10 — --send-test: one REAL Telegram message. The flag just does it; the caller
    (the /doctor command / verify chapter) owns confirming with the owner first."""
    env_file = ctx.root / "seneschal" / "scripts" / "telegram.env"
    if not env_file.is_file():
        return _row("send-test", RED, "telegram.env missing - cannot send", "-> /setup env:telegram")
    script = ctx.root / "seneschal" / "scripts" / "telegram_send.py"
    if not script.is_file():
        script = HERE / "telegram_send.py"
    rc, out, _ = _run(
        [sys.executable, str(script), "--env-file", str(env_file),
         "--text", "Seneschal doctor: test message - your channel works."],
        timeout=30.0,
    )
    if rc == 0 and '"sent": true' in out:
        return _row("send-test", GREEN, "test message sent - check your Telegram")
    # Child output is never echoed (an API error could theoretically embed request
    # details); the entry's own verify command is the place to debug interactively.
    detail = "send failed" + (f" (exit {rc})" if rc is not None else " (could not run telegram_send.py)")
    return _row("send-test", RED, detail + " - run the verify in TELEGRAM_SETUP.md for details",
                "-> /setup env:telegram")


# ---------------------------------------------------------------------------- board


def run_checks(ctx, probe: bool = False, send_test: bool = False) -> list[dict]:
    rows = [check_auth(ctx), check_models(ctx, probe=probe)]
    rows.extend(check_env(ctx))
    rows.append(check_store(ctx))
    rows.append(check_venv(ctx))
    rows.append(check_ollama(ctx))
    rows.append(check_daemon(ctx))
    rows.append(check_hooks(ctx))
    rows.extend(check_artifacts(ctx))
    if send_test:
        rows.append(run_send_test(ctx))
    return rows


def summarize(rows: list[dict]) -> dict:
    counts = {GREEN: 0, YELLOW: 0, RED: 0, SKIP: 0}
    for r in rows:
        counts[r["status"]] += 1
    return {"green": counts[GREEN], "yellow": counts[YELLOW], "red": counts[RED], "skip": counts[SKIP]}


def summary_line(rows: list[dict]) -> str:
    s = summarize(rows)
    line = f"{s['green']} green, {s['yellow']} yellow, {s['red']} red"
    if s["skip"]:
        line += f" ({s['skip']} skipped)"
    return line


def board_lines(rows: list[dict]) -> list[str]:
    width = max(len(r["id"]) for r in rows) if rows else 0
    lines = []
    for r in rows:
        lines.append(f"{GLYPHS[r['status']]} {r['id']:<{width}}  {r['detail']}")
        if r.get("fix") and r["status"] != GREEN:
            lines.append(" " * (4 + width + 2) + f"fix: {r['fix']}")
    lines.append(summary_line(rows))
    return lines


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="The /doctor board - end-to-end health checks, GREEN/YELLOW/RED per surface.")
    p.add_argument("--json", action="store_true", help="machine output (rows + summary) instead of the board")
    p.add_argument("--probe", action="store_true",
                   help="live-probe each configured model dial via `claude -p` (~120s timeout each)")
    p.add_argument("--probe-model", metavar="ID",
                   help="one-off: probe a single model id and exit (0 ok / 1 fail); for the auth-models chapter")
    p.add_argument("--send-test", action="store_true",
                   help="send a REAL Telegram test message (caller confirms with the owner first)")
    p.add_argument("--root", default=None, help="repo root override (tests)")
    p.add_argument("--state-dir", default=None, help="state dir override (tests; default <root>/seneschal/state)")
    p.add_argument("--manifest", default=None, help="env manifest override (tests)")
    p.add_argument("--platform", default=None, help="platform override: win32/linux/darwin (tests)")
    p.add_argument("--home", default=None, help="home dir override (tests)")
    args = p.parse_args(argv[1:])

    if args.probe_model:
        ok, detail = probe_model(args.probe_model)
        print(json.dumps({"model": args.probe_model, "ok": ok, "detail": detail}))
        return 0 if ok else 1

    ctx = build_ctx(root=args.root, state_dir=args.state_dir, manifest=args.manifest,
                    home=args.home, platform=args.platform, environ=None)
    rows = run_checks(ctx, probe=args.probe, send_test=args.send_test)
    if args.json:
        print(json.dumps({
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "platform": ctx.platform,
            "rows": rows,
            "summary": summarize(rows),
        }, indent=2))
    else:
        for line in board_lines(rows):
            print(line)
    return summarize(rows)["red"]


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
