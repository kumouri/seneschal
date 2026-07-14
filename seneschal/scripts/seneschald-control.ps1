#Requires -Version 7
<#
.SYNOPSIS
    Control the resident seneschal presence daemon (the `seneschald` scheduled task).

.DESCRIPTION
    One entry point for stopping, starting, restarting, and updating seneschald.

      -Action Stop     Stop the scheduled task AND kill the running daemon (stopping the task alone does
                       NOT end the process), then clear the lock.
      -Action Start    Start the scheduled task.
      -Action Restart  Stop then Start (the old `reseneschald` behaviour). Default.
      -Action Update   Merge-detector: fetch origin/main; if it advanced, `git pull --ff-only`, then
                       ask the *running* daemon to reload itself gracefully (once its warm chat session is
                       idle) via the control queue. No hard kill, no lost conversation.

    Stop/Start/Restart touch the scheduled task + kill a PID, so they self-elevate to admin. Update only
    does git + drops a control-queue entry, so it needs no elevation — that's what lets the lightweight
    `seneschald-update` scheduled task poll for merges without running as admin.

    This is the canonical, version-controlled control script. The local
    `~/Documents/powershell/restart-seneschald.ps1` (and the `reseneschald` alias) should delegate here:
        & "<repo>/seneschal/scripts/seneschald-control.ps1" -Action Restart
#>
[CmdletBinding()]
param(
    [ValidateSet('Stop', 'Start', 'Restart', 'Update')]
    [string]$Action = 'Restart'
)

$ErrorActionPreference = 'Stop'

# Repo root = two levels up from this script (seneschal/scripts/ -> repo root). Robust wherever the repo lives.
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$StateDir = Join-Path $RepoRoot 'seneschal\state'
$LockFile = Join-Path $StateDir 'presence.lock'
$TaskName = 'seneschald'
$GitFlags = @('-c', 'core.fsmonitor=false')   # fsmonitor otherwise hangs in this repo

function Test-Admin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Actions that touch the task/PID need admin; Update does not. Self-elevate only when required.
if ($Action -in 'Stop', 'Start', 'Restart' -and -not (Test-Admin)) {
    Start-Process pwsh "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action $Action" -Verb RunAs
    exit
}

function Stop-Seneschald {
    Write-Host '- stopping seneschald...'
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch { Write-Warning "Stop-ScheduledTask: $_" }
    # Stopping the scheduled task does NOT end the already-running daemon process — kill the PID from the lock.
    if (Test-Path $LockFile) {
        $daemonPid = $null
        try { $daemonPid = (Get-Content $LockFile -Raw | ConvertFrom-Json).pid } catch { Write-Warning "lock parse: $_" }
        if ($daemonPid) {
            try { Stop-Process -Id $daemonPid -Force -ErrorAction Stop } catch { Write-Warning "Stop-Process ${daemonPid}: $_" }
        }
        Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
    }
}

function Start-Seneschald {
    Write-Host '- starting seneschald...'
    Start-ScheduledTask -TaskName $TaskName
}

function Restart-Seneschald {
    Stop-Seneschald
    Start-Seneschald
}

function Sync-DaemonDeps {
    # Bring .venv in step with uv.lock before a reload. Returns $true when it's safe to restart onto
    # the pulled code: synced OK, or there's nothing to sync (no pyproject — e.g. a revert), or uv
    # itself is missing (don't brick deploys: the launcher falls back to system python and
    # dependency-needing code degrades gracefully — see seneschal/docs/asyncio-daemon-design.md).
    # Returns $false ONLY when a real sync attempt failed — the caller then holds the restart so the
    # running daemon (old in-memory code, matching old deps) stays up, and retries next cycle.
    param($log)
    if (-not (Test-Path (Join-Path $RepoRoot 'pyproject.toml'))) { return $true }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        & $log 'WARN: uv not found — skipping dependency sync (install uv to enable the daemon venv).'
        return $true
    }
    $out = & uv sync --frozen 2>&1
    if ($LASTEXITCODE -ne 0) {
        $tail = ($out | Select-Object -Last 3) -join ' | '
        & $log "DEPS FAILED: uv sync --frozen exited $LASTEXITCODE — holding the restart. $tail"
        return $false
    }
    & $log 'deps synced (uv sync --frozen)'
    return $true
}

function Update-Seneschald {
    # Graceful, no admin. Runs from the seneschald-update scheduled task, which DISCARDS console output — so
    # every outcome is logged to state/seneschald-update.log too, or a silent skip/failure is invisible (as
    # happened 2026-07-06: a detached HEAD made this no-op for ~20 min with nobody the wiser).
    $logFile = Join-Path $StateDir 'seneschald-update.log'
    # Dropped when a pull landed but the dep sync failed: the restart is deferred (never onto a
    # half-updated env) and re-attempted here every cycle until the sync succeeds.
    $pendingRestart = Join-Path $StateDir 'pending-restart'
    $log = {
        param($m)
        $line = '[{0}] {1}' -f (Get-Date -Format 's'), $m
        Write-Host "- $m"
        try { Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8 } catch {}
    }
    Push-Location $RepoRoot
    try {
        & git @GitFlags fetch origin main 2>&1 | Out-Null
        $remote = (& git @GitFlags rev-parse 'origin/main').Trim()

        # Detached HEAD: an external tool (GitKraken, a stray checkout) can leave the checkout detached,
        # which used to make this skip forever. If the detached commit is on main's line (an ancestor of
        # origin/main — no divergent local work), re-attach to main at the SAME commit (no tree move)
        # and continue; the normal ff-pull below then advances it. If it has commits NOT on origin/main,
        # skip loudly rather than force-moving unknown work.
        & git @GitFlags symbolic-ref -q HEAD 1>$null 2>$null
        if ($LASTEXITCODE -ne 0) {
            & git @GitFlags merge-base --is-ancestor HEAD origin/main 2>$null
            if ($LASTEXITCODE -eq 0) {
                & $log 'HEAD was detached (on main line) — re-attaching to main'
                & git @GitFlags checkout -B main 2>&1 | Out-Null   # reset main to HEAD; no tree move
            } else {
                & $log 'SKIP: HEAD detached with commits not on origin/main — not auto-moving. Investigate.'
                return
            }
        } else {
            $branch = (& git @GitFlags rev-parse --abbrev-ref HEAD).Trim()
            if ($branch -ne 'main') { & $log "SKIP: on '$branch', not main (Path A expects main)."; return }
        }

        $local = (& git @GitFlags rev-parse '@').Trim()
        if ($local -eq $remote) {
            # Up to date — but a prior cycle may have pulled and then failed the dep sync. Retry the
            # sync until it succeeds, then fire the restart that pull deferred.
            if (Test-Path $pendingRestart) {
                if (Sync-DaemonDeps $log) {
                    Remove-Item $pendingRestart -Force -ErrorAction SilentlyContinue
                    python (Join-Path $RepoRoot 'seneschal\scripts\request_control.py') --action restart --reason 'deferred: deps synced'
                    & $log 'deps recovered — requested the deferred graceful restart'
                }
                return
            }
            & $log "up to date ($($local.Substring(0,8)))"; return
        }

        & $log "main advanced ($($local.Substring(0,8)) -> $($remote.Substring(0,8))) — pull --ff-only"
        & git @GitFlags pull --ff-only origin main 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { & $log 'pull --ff-only FAILED; leaving the daemon on its current code.'; return }

        # Deps BEFORE the reload: never restart the daemon onto code whose lockfile didn't sync.
        # (The running daemon keeps its old in-memory code, which matches the old env — safe to wait.)
        if (-not (Sync-DaemonDeps $log)) {
            New-Item -ItemType File -Path $pendingRestart -Force | Out-Null
            & $log 'restart deferred until the dep sync succeeds (retries every Update cycle)'
            return
        }
        Remove-Item $pendingRestart -Force -ErrorAction SilentlyContinue

        # Ask the running daemon to reload itself once its warm session is idle (no hard kill).
        python (Join-Path $RepoRoot 'seneschal\scripts\request_control.py') --action restart --reason 'merge to main'
        & $log 'requested graceful restart (applies once the warm session is idle)'
    }
    finally { Pop-Location }
}

switch ($Action) {
    'Stop' { Stop-Seneschald }
    'Start' { Start-Seneschald }
    'Restart' { Restart-Seneschald }
    'Update' { Update-Seneschald }
}
