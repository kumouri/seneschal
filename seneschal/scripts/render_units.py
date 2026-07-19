#!/usr/bin/env python3
"""Render the per-machine daemon assets the ``/setup daemon`` chapter installs.

The tracked launcher templates (``run-presence.cmd`` / ``run-presence.sh``) are
machine-neutral by contract — this tool renders **gitignored local copies** of them (plus the
platform's registration assets) into ``seneschal/state/setup/``, substituting the absolute
repo path and the model dials the wizard's ledger recorded. The tracked templates are never
modified; the setup dir is covered by the ``seneschal/state/*`` gitignore rule.

Per platform:

  * **win32** — ``run-presence.local.cmd`` (absolute paths; ledger model args) and
    ``register-tasks.ps1``: every admin step of the chapter in ONE script, so the owner
    approves it once and runs it under ONE elevation — Register-ScheduledTask ``seneschald``
    (at logon, S4U principal, restart 1 min x999, no time limit, IgnoreNew — mirroring
    SCHEDULING.md), ``seneschald-update`` (10-min repetition via ``wscript.exe`` +
    ``run-seneschald-update-hidden.vbs``, mirroring PATH_A_CUTOVER.md step 5), and, with
    ``--with-health-listener``, ``seneschal-health-listener``. The script writes per-task
    results to ``register-tasks.result.json`` for the chapter to read back. ``--user-level``
    renders the no-admin fallback (InteractiveToken principal — console window at logon,
    runs only while logged on). **This tool only renders the file** — launching it (the one
    ``Start-Process -Verb RunAs -Wait``) is the owner's/chapter's step, never Python's.
  * **linux** — ``run-presence.local.sh`` + ``run-seneschald-update.local.sh`` and the
    systemd **user** units ``seneschald.service`` (ExecStart = the local launcher,
    Restart=on-failure, ``EnvironmentFile=-%h/.config/seneschal/daemon.env`` — the token
    home the auth-models chapter established) + ``seneschald-update.service`` /
    ``seneschald-update.timer`` (OnUnitActiveSec=10min). Staged in the setup dir;
    ``--install`` copies them to ``~/.config/systemd/user/``.
  * **darwin** — the same two local scripts plus launchd agents
    ``com.seneschal.presence.plist`` (RunAtLoad + KeepAlive) and
    ``com.seneschal.update.plist`` (StartInterval 600), both exec'ing through a
    ``/bin/sh -c`` wrapper that sources ``~/.config/seneschal/daemon.env`` first.
    ``--install`` copies them to ``~/Library/LaunchAgents/``.

The rendered POSIX launcher itself also sources ``daemon.env`` (injected right after
``set -u``, *before* the template's ANTHROPIC_API_KEY scrub so billing safety still wins) —
launchd/cron paths don't get systemd's EnvironmentFile, so the launcher must self-serve.

Model substitution reads the wizard ledger's ``models`` block
(``setup_state.load()`` — ``{"watch": id|null, "slots": id|null, ...}``): ``watch`` replaces
the template's ``--watch-model`` value; ``slots`` adds a ``--slot-model`` line. Absent /
null -> the template defaults stand. Ids are normalized through ``model_config.canonical``
when they resolve, else passed through as given.

Contracts: stdlib only; identity-neutral; ``--dry-run`` (default) prints every asset and
writes NOTHING (including ``--install`` targets); ``--apply`` writes; **no PowerShell is
ever spawned from here** (AMSI wedges pwsh child processes on some Windows hosts — the
rendered .ps1 is for the OWNER to run).

CLI::

    python seneschal/scripts/render_units.py [--platform win32|linux|darwin] [--repo DIR]
        [--dry-run | --apply] [--user-level] [--with-health-listener] [--install]
        [--home DIR] [--state-file F]
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import model_config  # noqa: E402
import setup_state  # noqa: E402

REPO_ROOT = HERE.parents[1]
SETUP_DIR_REL = Path("seneschal") / "state" / "setup"

PLATFORMS = ("win32", "linux", "darwin")

# Registered names — MUST stay in step with setup_doctor.check_daemon's probes
# (schtasks seneschald / seneschald-update; systemd seneschald.service; launchd
# com.seneschal.*) and with SCHEDULING.md / PATH_A_CUTOVER.md.
TASK_DAEMON = "seneschald"
TASK_UPDATE = "seneschald-update"
TASK_HEALTH = "seneschal-health-listener"
UNIT_DAEMON = "seneschald.service"
UNIT_UPDATE_SERVICE = "seneschald-update.service"
UNIT_UPDATE_TIMER = "seneschald-update.timer"
LABEL_PRESENCE = "com.seneschal.presence"
LABEL_UPDATE = "com.seneschal.update"

DAEMON_ENV_POSIX = "$HOME/.config/seneschal/daemon.env"


class RenderError(Exception):
    """A condition that makes rendering impossible (missing template, bad platform)."""


def default_platform() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ps_quote(s: str) -> str:
    """A PowerShell single-quoted string literal (embedded quotes doubled)."""
    return "'" + str(s).replace("'", "''") + "'"


def load_models(repo: Path, state_file: Path | None = None) -> dict:
    """The ledger's models block, ids normalized where they resolve. Absent -> {}."""
    path = state_file or (repo / "seneschal" / "state" / "setup-state.json")
    models = setup_state.load(path).get("models") or {}
    out = {}
    for key in ("watch", "slots"):
        raw = models.get(key)
        if isinstance(raw, str) and raw.strip():
            out[key] = model_config.canonical(raw) or raw.strip()
    return out


def _read_template(repo: Path, name: str) -> str:
    path = repo / "seneschal" / "scripts" / name
    try:
        # Bytes, not read_text: universal-newline mode would silently fold the .cmd
        # template's CRLF to LF, and the rendered launcher must keep its native endings.
        return path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise RenderError(f"tracked template missing: {path} ({exc})")


# --------------------------------------------------------------------------- launchers


def render_launcher_cmd(repo: Path, models: dict, template: str | None = None) -> str:
    """run-presence.cmd -> run-presence.local.cmd: absolute paths + ledger model args.

    The template resolves everything off %~dp0 (its own folder). The local copy lives in
    seneschal/state/setup/, so every %~dp0 reference is pinned to the real script dir.
    """
    text = template if template is not None else _read_template(repo, "run-presence.cmd")
    repo_win = str(repo).replace("/", "\\").rstrip("\\")
    scripts_win = repo_win + "\\seneschal\\scripts\\"
    text = text.replace("%~dp0..\\..", repo_win).replace("%~dp0", scripts_win)
    text = _substitute_models(text, models, continuation="^")
    eol = "\r\n" if "\r\n" in text else "\n"
    banner = (
        f"REM >>> RENDERED LOCAL - generated {_stamp()} by render_units.py from the tracked{eol}"
        f"REM >>> template run-presence.cmd. Gitignored; edit knobs here freely; never commit{eol}"
        f"REM >>> this file. Re-render via /setup daemon (or render_units.py --apply).{eol}"
    )
    return _insert_after_first_line(text, banner)


def render_launcher_sh(repo: Path, models: dict, template: str | None = None) -> str:
    """run-presence.sh -> run-presence.local.sh: pinned SCRIPT_DIR, daemon.env sourcing,
    ledger model args."""
    text = template if template is not None else _read_template(repo, "run-presence.sh")
    repo_posix = str(repo).replace("\\", "/").rstrip("/")
    text = text.replace(
        'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)',
        f'SCRIPT_DIR="{repo_posix}/seneschal/scripts"'
        "   # pinned by render_units.py - this local copy lives in state/setup/, not scripts/",
    )
    env_block = (
        "\n"
        "# >>> daemon.env - the token home the setup wizard's auth-models chapter established.\n"
        "# >>> Sourced here so unattended runs (systemd/launchd/cron) see CLAUDE_CODE_OAUTH_TOKEN\n"
        "# >>> without polluting shell profiles. The API-key scrub below still runs AFTER this.\n"
        'DAEMON_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/seneschal/daemon.env"\n'
        'if [ -f "$DAEMON_ENV" ]; then\n'
        "  set -a\n"
        '  . "$DAEMON_ENV"\n'
        "  set +a\n"
        "fi\n"
    )
    marker = "\nset -u\n"
    if marker in text:
        text = text.replace(marker, "\nset -u\n" + env_block, 1)
    else:  # unexpected template drift: still source it, right after the shebang
        text = _insert_after_first_line(text, env_block)
    text = _substitute_models(text, models, continuation="\\")
    banner = (
        f"# >>> RENDERED LOCAL - generated {_stamp()} by render_units.py from the tracked\n"
        "# >>> template run-presence.sh. Gitignored; edit knobs here freely; never commit\n"
        "# >>> this file. Re-render via /setup daemon (or render_units.py --apply).\n"
    )
    return _insert_after_first_line(text, banner)


def _insert_after_first_line(text: str, block: str) -> str:
    head, sep, rest = text.partition("\n")
    return head + sep + block + rest


def _substitute_models(text: str, models: dict, continuation: str) -> str:
    """Apply the ledger's watch/slots dials to a launcher's command block (EOL-preserving)."""
    import re

    eol = "\r\n" if "\r\n" in text else "\n"
    watch = models.get("watch")
    if watch:
        text = re.sub(r"(?m)^(  --watch-model )\S+", lambda m: m.group(1) + watch, text)
    slots = models.get("slots")
    if slots:
        text = re.sub(
            r"(?m)^  --watch-model ",
            f"  --slot-model {slots} {continuation}{eol}  --watch-model ",
            text,
            count=1,
        )
    return text


def render_update_sh(repo: Path) -> str:
    """The POSIX merge-detector — a deliberately small mirror of
    ``seneschald-control.ps1 -Action Update`` (ff-only pull + dep sync + graceful restart
    request). No self-heal/alerting arms here; the Windows control script stays the
    reference implementation and this one refuses loudly instead of guessing."""
    repo_posix = str(repo).replace("\\", "/").rstrip("/")
    return f"""#!/bin/sh
# >>> RENDERED LOCAL - generated {_stamp()} by render_units.py. Gitignored; never commit.
# The POSIX merge-detector: when origin/main advances, ff-only pull + `uv sync --frozen`,
# then ask the RUNNING daemon to reload gracefully via the control queue (no hard kill).
# A simplified sibling of seneschald-control.ps1 -Action Update (the reference flow):
# any surprise (off-branch checkout, failed pull/sync) logs + holds - it never forces.
set -u
REPO="{repo_posix}"
LOG="$REPO/seneschal/state/seneschald-update.log"
log() {{
  printf '[%s] %s\\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$1" >> "$LOG" 2>/dev/null || true
  printf -- '- %s\\n' "$1"
}}
cd "$REPO" || exit 0
GIT="git -c core.fsmonitor=false"
$GIT fetch origin main >/dev/null 2>&1 || {{ log "SKIP: fetch failed - holding on current code"; exit 0; }}
branch=$($GIT rev-parse --abbrev-ref HEAD 2>/dev/null) || exit 0
if [ "$branch" != "main" ]; then
  log "SKIP: checkout is on '$branch', not main - left alone"
  exit 0
fi
local_rev=$($GIT rev-parse '@' 2>/dev/null) || exit 0
remote_rev=$($GIT rev-parse origin/main 2>/dev/null) || exit 0
[ "$local_rev" = "$remote_rev" ] && exit 0
log "main advanced - pull --ff-only"
$GIT pull --ff-only origin main >/dev/null 2>&1 || {{ log "pull --ff-only FAILED - holding (run git status)"; exit 0; }}
if [ -f "$REPO/pyproject.toml" ] && command -v uv >/dev/null 2>&1; then
  uv sync --frozen >/dev/null 2>&1 || {{ log "uv sync --frozen FAILED - holding the restart"; exit 0; }}
fi
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
"$PY" "$REPO/seneschal/scripts/request_control.py" --action restart --reason "merge to main" || true
log "pulled + graceful restart requested"
"""


# --------------------------------------------------------------------------- windows


def _ps_register_daemon_task(name: str, launcher: str, user_level: bool) -> str:
    principal = (
        "New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited"
        if user_level
        else "New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited"
    )
    detail = (
        "registered (at logon, user-level InteractiveToken - console window visible, runs only while logged on) + started"
        if user_level
        else "registered (at logon, S4U session-0, restart 1 min x999, no time limit) + started"
    )
    return f"""try {{
    $action    = New-ScheduledTaskAction -Execute {ps_quote(launcher)}
    $trigger   = New-ScheduledTaskTrigger -AtLogOn
    $settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    $principal = {principal}
    Register-ScheduledTask -TaskName {ps_quote(name)} -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName {ps_quote(name)}
    Add-Result {ps_quote(name)} $true {ps_quote(detail)}
}} catch {{
    Add-Result {ps_quote(name)} $false "$_"
}}"""


def render_register_ps1(
    repo: Path, user_level: bool = False, with_health_listener: bool = False
) -> str:
    """register-tasks.ps1 — ALL the chapter's Task Scheduler steps in one approve-once
    script. Task parameters mirror SCHEDULING.md (sections 2-3) and PATH_A_CUTOVER.md
    (step 5) exactly. Rendered for the OWNER to run; Python never launches it."""
    repo_win = str(repo).replace("/", "\\").rstrip("\\")
    setup_win = repo_win + "\\seneschal\\state\\setup"
    launcher = setup_win + "\\run-presence.local.cmd"
    vbs = repo_win + "\\seneschal\\scripts\\run-seneschald-update-hidden.vbs"
    health_cmd = repo_win + "\\seneschal\\scripts\\run-health-listener.cmd"
    result_json = setup_win + "\\register-tasks.result.json"

    if user_level:
        header_mode = (
            "# USER-LEVEL FALLBACK (no admin): tasks register with the InteractiveToken principal.\n"
            "# Honest caveats: a console window appears at logon (no session-0 hiding without S4U),\n"
            "# and the tasks run only while this user is logged on. Run this WITHOUT elevation:\n"
            "#   pwsh -NoProfile -ExecutionPolicy Bypass -File <this file>\n"
        )
    else:
        header_mode = (
            "# ALL admin steps in ONE script = ONE elevation. The setup wizard shows this file,\n"
            "# you approve it, then run it yourself in ONE elevated shot (the only UAC prompt):\n"
            "#   Start-Process pwsh -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File'," + ps_quote(setup_win + "\\register-tasks.ps1") + "\n"
        )

    blocks = [
        f"""# register-tasks.ps1 - RENDERED {_stamp()} by render_units.py. Gitignored; never commit.
# Registers the seneschal daemon layer with Windows Task Scheduler:
#   * {TASK_DAEMON}                 - the always-on presence daemon (at logon)
#   * {TASK_UPDATE}          - the 10-min merge detector (windowless via wscript + vbs)"""
        + (
            f"\n#   * {TASK_HEALTH}  - the phone health/presence feed listener"
            if with_health_listener
            else ""
        )
        + f"""
{header_mode}# Task parameters mirror seneschal/scripts/SCHEDULING.md + PATH_A_CUTOVER.md - the manual
# path there stays authoritative. Per-task results land in register-tasks.result.json.

$ErrorActionPreference = 'Stop'
$results = @()

function Add-Result {{
    param([string]$Task, [bool]$Ok, [string]$Detail)
    $script:results += [ordered]@{{ task = $Task; ok = $Ok; detail = $Detail }}
    if ($Ok) {{ Write-Host ('  ok    ' + $Task + ' - ' + $Detail) }}
    else {{ Write-Host ('  FAIL  ' + $Task + ' - ' + $Detail) }}
}}

# --- {TASK_DAEMON}: the always-on presence daemon ---------------------------------
{_ps_register_daemon_task(TASK_DAEMON, launcher, user_level)}""",
        f"""# --- {TASK_UPDATE}: poll origin/main every 10 min; merge = deploy ------------
# No admin needed by the task itself (git + a control-queue drop); registered here so one
# approved run covers everything. wscript keeps it windowless (a bare pwsh.exe task would
# flash a console every 10 minutes).
try {{
    $vbs     = {ps_quote(vbs)}
    $action  = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"{{0}}"' -f $vbs)
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Days 3650)
    Register-ScheduledTask -TaskName {ps_quote(TASK_UPDATE)} -Action $action -Trigger $trigger -Description 'Pull main into the seneschald checkout when a PR merges, then gracefully reload the daemon.' -Force | Out-Null
    Add-Result {ps_quote(TASK_UPDATE)} $true 'registered (every 10 min, windowless via wscript)'
}} catch {{
    Add-Result {ps_quote(TASK_UPDATE)} $false "$_"
}}""",
    ]
    if with_health_listener:
        blocks.append(
            f"""# --- {TASK_HEALTH}: the phone health/presence feed listener ------
# Needs the HEALTH_INGEST_TOKEN user env var (SCHEDULING.md section 3).
{_ps_register_daemon_task(TASK_HEALTH, health_cmd, user_level)}"""
        )
    blocks.append(
        f"""# --- results: the chapter reads this back -----------------------------------------
$summary = [ordered]@{{
    generated_at = (Get-Date -Format 's')
    user_level   = ${str(bool(user_level)).lower()}
    tasks        = $results
}}
$resultPath = {ps_quote(result_json)}
$summary | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resultPath -Encoding UTF8
Write-Host ('results written to ' + $resultPath)
if (@($results | Where-Object {{ -not $_.ok }}).Count -gt 0) {{ exit 1 }} else {{ exit 0 }}"""
    )
    return "\n\n".join(blocks) + "\n"


# --------------------------------------------------------------------------- linux


def render_systemd_units(repo: Path) -> dict[str, str]:
    repo_posix = str(repo).replace("\\", "/").rstrip("/")
    setup_posix = repo_posix + "/seneschal/state/setup"
    banner = f"# RENDERED {_stamp()} by render_units.py - gitignored local; re-render via /setup daemon.\n"
    daemon = f"""{banner}[Unit]
Description=Seneschal presence daemon (warm chat, reminders, comms-peek)
After=network-online.target

[Service]
Type=simple
# The token home the setup wizard's auth-models chapter established ('-' = ok when absent).
EnvironmentFile=-%h/.config/seneschal/daemon.env
ExecStart={setup_posix}/run-presence.local.sh
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
"""
    update_service = f"""{banner}[Unit]
Description=Seneschal merge detector - ff-pull the deploy branch, then graceful daemon reload

[Service]
Type=oneshot
EnvironmentFile=-%h/.config/seneschal/daemon.env
ExecStart={setup_posix}/run-seneschald-update.local.sh
"""
    update_timer = f"""{banner}[Unit]
Description=Run {UNIT_UPDATE_SERVICE} every 10 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
Unit={UNIT_UPDATE_SERVICE}

[Install]
WantedBy=timers.target
"""
    return {
        UNIT_DAEMON: daemon,
        UNIT_UPDATE_SERVICE: update_service,
        UNIT_UPDATE_TIMER: update_timer,
    }


# --------------------------------------------------------------------------- macos


def _plist(label: str, program: str, schedule_xml: str) -> str:
    """One launchd agent: a /bin/sh -c wrapper that sources daemon.env then execs."""
    wrapper = (
        f'set -a; [ -f "{DAEMON_ENV_POSIX}" ] && . "{DAEMON_ENV_POSIX}"; set +a; '
        f'exec "{program}"'
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<!-- RENDERED {_stamp()} by render_units.py - gitignored local; re-render via /setup daemon. -->
<dict>
    <key>Label</key>
    <string>{xml_escape(label)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>{xml_escape(wrapper)}</string>
    </array>
{schedule_xml}</dict>
</plist>
"""


def render_launchd_plists(repo: Path) -> dict[str, str]:
    repo_posix = str(repo).replace("\\", "/").rstrip("/")
    setup_posix = repo_posix + "/seneschal/state/setup"
    log_path = xml_escape(repo_posix + "/seneschal/state/launchd-presence.log")
    presence_schedule = f"""    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
"""
    update_schedule = """    <key>RunAtLoad</key>
    <false/>
    <key>StartInterval</key>
    <integer>600</integer>
"""
    return {
        f"{LABEL_PRESENCE}.plist": _plist(
            LABEL_PRESENCE, setup_posix + "/run-presence.local.sh", presence_schedule
        ),
        f"{LABEL_UPDATE}.plist": _plist(
            LABEL_UPDATE, setup_posix + "/run-seneschald-update.local.sh", update_schedule
        ),
    }


# --------------------------------------------------------------------------- plan


def build_plan(
    platform: str,
    repo: Path,
    models: dict,
    user_level: bool = False,
    with_health_listener: bool = False,
) -> list[dict]:
    """Every asset to render: [{name, text, executable, install_to|None}] (install_to is
    the ~-relative copy target ``--install`` uses; None = stays in the setup dir)."""
    if platform not in PLATFORMS:
        raise RenderError(f"unknown platform {platform!r} (choose from: {', '.join(PLATFORMS)})")
    plan: list[dict] = []
    if platform == "win32":
        plan.append({"name": "run-presence.local.cmd",
                     "text": render_launcher_cmd(repo, models),
                     "executable": False, "install_to": None})
        plan.append({"name": "register-tasks.ps1",
                     "text": render_register_ps1(repo, user_level, with_health_listener),
                     "executable": False, "install_to": None})
        return plan
    plan.append({"name": "run-presence.local.sh",
                 "text": render_launcher_sh(repo, models),
                 "executable": True, "install_to": None})
    plan.append({"name": "run-seneschald-update.local.sh",
                 "text": render_update_sh(repo),
                 "executable": True, "install_to": None})
    if platform == "linux":
        for name, text in render_systemd_units(repo).items():
            plan.append({"name": name, "text": text, "executable": False,
                         "install_to": f".config/systemd/user/{name}"})
    else:
        for name, text in render_launchd_plists(repo).items():
            plan.append({"name": name, "text": text, "executable": False,
                         "install_to": f"Library/LaunchAgents/{name}"})
    return plan


def enable_commands(platform: str, home: Path | None = None) -> list[str]:
    """The post-install activation commands the chapter shows (never runs unconfirmed)."""
    if platform == "linux":
        return [
            "systemctl --user daemon-reload",
            f"systemctl --user enable --now {UNIT_DAEMON} {UNIT_UPDATE_TIMER}",
            "sudo loginctl enable-linger $USER   # the ONE sudo: user units outlive logout / start at boot",
        ]
    if platform == "darwin":
        agents = str((home or Path.home()) / "Library" / "LaunchAgents").replace("\\", "/")
        return [
            f"launchctl bootstrap gui/$UID {agents}/{LABEL_PRESENCE}.plist",
            f"launchctl bootstrap gui/$UID {agents}/{LABEL_UPDATE}.plist",
        ]
    return []  # win32: register-tasks.ps1 is the activation


def _write(path: Path, text: str, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    if executable and os.name == "posix":
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Render the per-machine daemon assets (launcher locals + platform "
                    "registration files) into seneschal/state/setup/. Dry-run by default."
    )
    p.add_argument("--platform", choices=PLATFORMS, default=default_platform(),
                   help="target platform (default: this machine's)")
    p.add_argument("--repo", default=None, help="absolute repo root (default: this checkout)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print every asset, write nothing (default)")
    mode.add_argument("--apply", action="store_true", help="write the assets")
    p.add_argument("--user-level", action="store_true",
                   help="win32: render the no-admin InteractiveToken fallback variant")
    p.add_argument("--with-health-listener", action="store_true",
                   help="win32: also register the seneschal-health-listener task")
    p.add_argument("--install", action="store_true",
                   help="POSIX: also copy staged units/plists to the real user paths "
                        "(respects --dry-run)")
    p.add_argument("--home", default=None, help="home dir override for --install (tests)")
    p.add_argument("--state-file", default=None, help="wizard-ledger path override (tests)")
    args = p.parse_args(argv[1:])

    repo = Path(args.repo).resolve() if args.repo else REPO_ROOT
    home = Path(args.home) if args.home else Path.home()
    dry = not args.apply

    try:
        models = load_models(repo, Path(args.state_file) if args.state_file else None)
        plan = build_plan(args.platform, repo, models,
                          user_level=args.user_level,
                          with_health_listener=args.with_health_listener)
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    setup_dir = repo / SETUP_DIR_REL
    if models:
        dials = ", ".join(f"{k}={v}" for k, v in sorted(models.items()))
        print(f"model dials from the wizard ledger: {dials}")
    else:
        print("no model dials in the wizard ledger - template defaults stand")

    for item in plan:
        target = setup_dir / item["name"]
        if dry:
            print(f"\n--- would write {target} ---")
            print(item["text"], end="" if item["text"].endswith("\n") else "\n")
        else:
            _write(target, item["text"], item["executable"])
            print(f"wrote {target}")

    if args.install and args.platform != "win32":
        for item in plan:
            if not item["install_to"]:
                continue
            dest = home / item["install_to"]
            if dry:
                print(f"--- would install {setup_dir / item['name']} -> {dest}")
            else:
                _write(dest, item["text"], item["executable"])
                print(f"installed {dest}")

    cmds = enable_commands(args.platform, home)
    if cmds:
        print("\nactivation commands (the chapter confirms each before running):")
        for c in cmds:
            print(f"  {c}")
    if dry:
        print("\n(dry run - nothing written. Re-run with --apply to write.)")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
