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
      -Action Update   Merge-detector: fetch the DEPLOY BRANCH ($DeployBranch below — `main`); if it
                       advanced, `git pull --ff-only`, then ask the *running* daemon to reload itself
                       gracefully (once its warm chat session is idle) via the control queue. No hard
                       kill, no lost conversation.

                       It also SELF-HEALS and SPEAKS UP, because neither used to happen:
                       * Self-heal — a session that parks the live checkout on a feature branch used to
                         kill Path A until a human noticed (240 of 1329 cycles over one nine-day audit;
                         worst stretch 32.8 h). If the parked branch carries nothing origin/$DeployBranch
                         lacks AND the session registry says nobody is on it, Update reclaims the deploy
                         branch and carries on. It refuses (and says why) if the branch holds real
                         commits, if a live session claims it, or if the claim-check itself failed.
                       * Speak up — every cycle stamps state/seneschald-health.json, and a block lasting
                         past 3 cycles (~30 min) nudges the owner on Telegram. The old code logged each
                         failure to a file nobody reads and returned; 39.2 h of deploy-blind time went
                         unnoticed that way. The stamp is GUARANTEED now, not per-path: a catch-all turns
                         even an unexpected crash into a blocked heartbeat, because
                         run-seneschald-update-hidden.vbs launches this fire-and-forget (so the task's
                         exit code is always 0 and a crash would otherwise freeze the heartbeat silently
                         at its last 'ok').

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

# THE branch the live daemon tracks and auto-reloads from. Path A's whole design is that merging IS
# deploying, so this must be the branch merges land on — `main` here. Everything below says $DeployBranch
# rather than 'main' so this is a one-line change if the deploy branch ever moves.
$DeployBranch = 'main'

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

# --- Deploy health: make a blocked Path A audible ------------------------------------------------
# The log alone was never enough. Every failure path here is "log a line and return", and the log is a
# file nobody reads — over one nine-day stretch that hid 39.2 h of deploy-blind time across 240
# off-branch SKIPs and 15 failed pulls, including one 32.8 h block. The daemon keeps serving on its old
# code, so nothing *looks* broken; merges just quietly stop arriving. So: every cycle stamps
# state/seneschald-health.json, and a block that persists past $AlertAfterCycles nudges the owner on
# Telegram (their own channel, their own content — act-low), re-alerting at most every $ReAlertHours.
$HealthFile = Join-Path $StateDir 'seneschald-health.json'
$AlertAfterCycles = 3      # ~30 min at the 10-min task cadence — past a transient session checkout
$ReAlertHours = 6          # don't nag every cycle for a block the owner has already been told about

function Get-SeneschaldHealth {
    if (Test-Path $HealthFile) {
        try { return Get-Content $HealthFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
    }
    return $null
}

function Set-SeneschaldHealth {
    # Returns the merged health record. Never throws: a health-write failure must not break a deploy.
    param([string]$Status, [string]$Reason, [string]$Detail, [string]$Branch, [string]$Head)
    $prev = Get-SeneschaldHealth
    $now = (Get-Date -Format 's')
    $h = [ordered]@{
        status = $Status; reason = $Reason; detail = $Detail; branch = $Branch; head = $Head
        consecutive_blocked = 0
        blocked_since = $null
        last_ok = if ($prev) { $prev.last_ok } else { $null }
        last_alert = if ($prev) { $prev.last_alert } else { $null }
        updated_at = $now
    }
    if ($Status -eq 'ok') {
        $h.last_ok = $now
    } else {
        $h.consecutive_blocked = 1 + $(if ($prev -and $prev.status -ne 'ok') { [int]$prev.consecutive_blocked } else { 0 })
        $h.blocked_since = if ($prev -and $prev.status -ne 'ok' -and $prev.blocked_since) { $prev.blocked_since } else { $now }
    }
    try { ($h | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $HealthFile -Encoding UTF8 } catch {}
    return $h
}

function Send-SeneschaldAlert {
    # Best-effort Telegram nudge, rate-limited. Silent no-op when telegram.env is absent (a fresh clone)
    # — an un-configured channel must never break the update cycle.
    param($Health, $Log)
    if (-not $Health -or $Health.status -eq 'ok') { return }
    if ([int]$Health.consecutive_blocked -lt $AlertAfterCycles) { return }
    if ($Health.last_alert) {
        try {
            if (((Get-Date) - [datetime]$Health.last_alert).TotalHours -lt $ReAlertHours) { return }
        } catch {}
    }
    $envFile = Join-Path $PSScriptRoot 'telegram.env'
    if (-not (Test-Path $envFile)) { & $Log 'ALERT suppressed: no telegram.env'; return }
    $mins = 0
    try { $mins = [int]((Get-Date) - [datetime]$Health.blocked_since).TotalMinutes } catch {}
    $text = "Heads up - I've stopped auto-reloading.`n`n" +
            "$($Health.detail)`n`n" +
            "Blocked $mins min ($($Health.consecutive_blocked) cycles). Merges to $DeployBranch aren't reaching me " +
            "until it's cleared - I'm still running, just on older code."
    # Only record last_alert on a send that ACTUALLY landed. A non-zero exit from python does NOT throw
    # in PowerShell, so an unchecked call here would log a success it never made and then rate-limit the
    # retry away for $ReAlertHours — an alerting path that fails silently, which is the exact bug this
    # whole feature exists to kill. On failure: say so, leave last_alert alone, retry next cycle.
    $sendOut = ''
    try { $sendOut = "$(& python (Join-Path $PSScriptRoot 'telegram_send.py') --text $text --env-file $envFile 2>&1)" }
    catch { $sendOut = "$_"; $global:LASTEXITCODE = 1 }
    if ($LASTEXITCODE -eq 0 -and $sendOut -notmatch '"ok"\s*:\s*false') {
        & $Log "ALERTED the owner on Telegram (blocked $mins min)"
        $Health.last_alert = (Get-Date -Format 's')
        try { ($Health | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $HealthFile -Encoding UTF8 } catch {}
    } else {
        & $Log "alert FAILED (telegram_send exit $LASTEXITCODE) — NOT rate-limiting; retries next cycle. $($sendOut -replace '\s+', ' ')"
    }
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
        & git @GitFlags fetch origin $DeployBranch 2>&1 | Out-Null
        # rev-parse ECHOES the literal ref (non-empty!) and exits non-zero when it can't resolve, so gate
        # on the EXIT CODE, not on emptiness. An unresolvable origin/$DeployBranch (fetch never succeeded,
        # no cached ref) otherwise poisons every check below: merge-base fails, $noUniqueWork reads FALSE,
        # and the off-branch arm then blames the parked branch for "commits origin/$DeployBranch lacks"
        # that it does not have. Turn a fetch/remote problem into one honest, actionable stamp instead.
        # No `| Select-Object -First 1` here: piping a native command into a -First selector calls
        # StopUpstreamCommands, which races the git process's own exit and can leave $LASTEXITCODE
        # non-zero even on success — a false 'fetch-failed'. rev-parse of a single ref is one line, so
        # capture it directly and read $LASTEXITCODE straight off the native call.
        $remote = "$(& git @GitFlags rev-parse "origin/$DeployBranch" 2>$null)"
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($remote)) {
            $h = Set-SeneschaldHealth -Status 'blocked' -Reason 'fetch-failed' -Branch $DeployBranch `
                -Detail "Could not resolve origin/$DeployBranch (is the network / remote reachable?). I can't tell whether a merge landed, so I'm holding on the current code." -Head '(unknown)'
            & $log "SKIP: origin/$DeployBranch did not resolve — fetch/remote problem. Holding."
            Send-SeneschaldAlert -Health $h -Log $log
            return
        }
        $remote = $remote.Trim()

        # Detached HEAD: an external tool (GitKraken, a stray checkout) can leave the checkout detached,
        # which used to make this skip forever. If the detached commit is on the deploy branch's line (an
        # ancestor of origin/$DeployBranch — no divergent local work), re-attach at the SAME commit (no tree
        # move) and continue; the normal ff-pull below then advances it. If it has commits NOT on it,
        # skip loudly rather than force-moving unknown work.
        & git @GitFlags symbolic-ref -q HEAD 1>$null 2>$null
        if ($LASTEXITCODE -ne 0) {
            & git @GitFlags merge-base --is-ancestor HEAD "origin/$DeployBranch" 2>$null
            if ($LASTEXITCODE -eq 0) {
                & $log "HEAD was detached (on $DeployBranch's line) — re-attaching to $DeployBranch"
                & git @GitFlags checkout -B $DeployBranch 2>&1 | Out-Null   # reset the branch to HEAD; no tree move
                if ($LASTEXITCODE -ne 0) {
                    # Re-attach failed (dirty tree?). Without this stamp the function would fall through
                    # with HEAD still detached and could mis-stamp 'ok' below — so hold loudly instead.
                    $h = Set-SeneschaldHealth -Status 'blocked' -Reason 'reattach-failed' -Branch '(detached)' `
                        -Detail "HEAD was detached on $DeployBranch's line but re-attaching to $DeployBranch failed (dirty tree?). Needs a hand." -Head $remote
                    & $log "BLOCKED: re-attach to $DeployBranch failed from a detached HEAD."
                    Send-SeneschaldAlert -Health $h -Log $log
                    return
                }
            } else {
                $h = Set-SeneschaldHealth -Status 'blocked' -Reason 'detached-divergent' -Branch '(detached)' `
                    -Detail "HEAD is detached with commits origin/$DeployBranch does not have. I will not auto-move unknown work." -Head $remote
                & $log "SKIP: HEAD detached with commits not on origin/$DeployBranch — not auto-moving. Investigate."
                Send-SeneschaldAlert -Health $h -Log $log
                return
            }
        } else {
            $branch = (& git @GitFlags rev-parse --abbrev-ref HEAD).Trim()
            if ($branch -ne $DeployBranch) {
                # A session parked the live checkout on a feature branch — by far the most common way Path
                # A dies (240 of 1329 cycles in the audit above; worst stretch 32.8 h). Apply the SAME
                # reasoning the detached-HEAD arm above already uses: if the branch carries nothing
                # origin/$DeployBranch lacks, the tree is identical and reclaiming it costs nobody anything.
                # The extra guard the detached case doesn't need: a *named* branch may have a live session
                # sitting on it, so ask the session registry first (fail-CLOSED — "can't tell" = "hands
                # off"). Between them: an abandoned park self-heals, a live one is left alone and alerts.
                & git @GitFlags merge-base --is-ancestor HEAD "origin/$DeployBranch" 2>$null
                $noUniqueWork = ($LASTEXITCODE -eq 0)

                # Three states, not two. "claimed" and "couldn't check" both block the reclaim, but they
                # are different facts and must not be reported as the same one — saying "a live session is
                # on it" when the truth is "the check crashed" sends the owner hunting a session that
                # doesn't exist, and hides a broken check behind a plausible excuse.
                $claimOut = ''; $claimExit = 1
                try {
                    $claimOut = "$(& python (Join-Path $PSScriptRoot 'sentinel.py') `
                        --state-dir $StateDir --branch-claimed $branch 2>$null | Select-Object -Last 1)".Trim()
                    $claimExit = $LASTEXITCODE
                } catch { $claimExit = 1 }
                $claimState = if ($claimExit -eq 0 -and $claimOut -in 'free', 'claimed') { $claimOut } else { 'unknown' }

                if ($noUniqueWork -and $claimState -eq 'free') {
                    & $log "on '$branch' (nothing origin/$DeployBranch lacks; no live session claims it) — reclaiming $DeployBranch"
                    & git @GitFlags checkout $DeployBranch 2>&1 | Out-Null
                    if ($LASTEXITCODE -ne 0) {
                        $h = Set-SeneschaldHealth -Status 'blocked' -Reason 'checkout-failed' -Branch $branch `
                            -Detail "On '$branch' and the reclaim of $DeployBranch failed (dirty tree?). Needs a hand." -Head $remote
                        & $log "BLOCKED: reclaim of $DeployBranch failed from '$branch'."
                        Send-SeneschaldAlert -Health $h -Log $log
                        return
                    }
                } else {
                    $why = if (-not $noUniqueWork) { "it has commits origin/$DeployBranch lacks" }
                           elseif ($claimState -eq 'claimed') { 'a live session is working on it' }
                           else { "I couldn't check the session registry — that check is itself broken" }
                    $reason = if ($claimState -eq 'unknown' -and $noUniqueWork) { 'claim-check-failed' } else { 'off-deploy-branch' }
                    $h = Set-SeneschaldHealth -Status 'blocked' -Reason $reason -Branch $branch `
                        -Detail "The live checkout is on '$branch', not $DeployBranch, and I left it alone because $why." -Head $remote
                    & $log "SKIP: on '$branch', not $DeployBranch ($why) — left alone."
                    Send-SeneschaldAlert -Health $h -Log $log
                    return
                }
            }
        }

        $local = (& git @GitFlags rev-parse '@').Trim()
        if ($local -eq $remote) {
            # Up to date — but a prior cycle may have pulled and then failed the dep sync. Retry the
            # sync until it succeeds, then fire the restart that pull deferred.
            if (Test-Path $pendingRestart) {
                if (Sync-DaemonDeps $log) {
                    Remove-Item $pendingRestart -Force -ErrorAction SilentlyContinue
                    python (Join-Path $RepoRoot 'seneschal\scripts\request_control.py') --action restart --reason 'deferred: deps synced'
                    Set-SeneschaldHealth -Status 'ok' -Detail 'deps recovered; deferred restart requested' `
                        -Branch $DeployBranch -Head $local | Out-Null
                    & $log 'deps recovered — requested the deferred graceful restart'
                } else {
                    $h = Set-SeneschaldHealth -Status 'blocked' -Reason 'dep-sync-failed' -Branch $DeployBranch `
                        -Detail 'Code is pulled but `uv sync --frozen` keeps failing, so I am holding the restart rather than reload onto a half-updated env.' -Head $local
                    Send-SeneschaldAlert -Health $h -Log $log
                }
                return
            }
            Set-SeneschaldHealth -Status 'ok' -Detail 'up to date' -Branch $DeployBranch -Head $local | Out-Null
            & $log "up to date ($($local.Substring(0,8)))"; return
        }

        & $log "$DeployBranch advanced ($($local.Substring(0,8)) -> $($remote.Substring(0,8))) — pull --ff-only"
        $pullOut = (& git @GitFlags pull --ff-only origin $DeployBranch 2>&1) -join ' '
        if ($LASTEXITCODE -ne 0) {
            # The other silent killer (15 cycles in the same audit): a tracked file left dirty at a path
            # the incoming merge touches aborts the ff-pull. Any tracked file that runtime appends to
            # (e.g. an archon's delegation ledger) arms this the moment a PR touches the same path.
            $h = Set-SeneschaldHealth -Status 'blocked' -Reason 'pull-failed' -Branch $DeployBranch `
                -Detail "$DeployBranch advanced but `pull --ff-only` failed - usually a tracked file left dirty at a path the merge touches. Run ``git status`` in the live checkout." -Head $remote
            & $log "pull --ff-only FAILED; leaving the daemon on its current code. git: $pullOut"
            Send-SeneschaldAlert -Health $h -Log $log
            return
        }

        # Deps BEFORE the reload: never restart the daemon onto code whose lockfile didn't sync.
        # (The running daemon keeps its old in-memory code, which matches the old env — safe to wait.)
        if (-not (Sync-DaemonDeps $log)) {
            New-Item -ItemType File -Path $pendingRestart -Force | Out-Null
            $h = Set-SeneschaldHealth -Status 'blocked' -Reason 'dep-sync-failed' -Branch $DeployBranch `
                -Detail 'Pulled the new code, but `uv sync --frozen` failed - holding the restart so I never reload onto a half-updated env. Retrying every cycle.' -Head $remote
            & $log 'restart deferred until the dep sync succeeds (retries every Update cycle)'
            Send-SeneschaldAlert -Health $h -Log $log
            return
        }
        Remove-Item $pendingRestart -Force -ErrorAction SilentlyContinue

        # Ask the running daemon to reload itself once its warm session is idle (no hard kill).
        python (Join-Path $RepoRoot 'seneschal\scripts\request_control.py') --action restart --reason "merge to $DeployBranch"
        Set-SeneschaldHealth -Status 'ok' -Detail "pulled $($remote.Substring(0,8)); graceful restart requested" `
            -Branch $DeployBranch -Head $remote | Out-Null
        & $log 'requested graceful restart (applies once the warm session is idle)'
    }
    catch {
        # Belt-and-braces guarantee: EVERY cycle stamps health. The per-outcome stamps above cover every
        # KNOWN path, but an unexpected terminating error (a git call returning nothing so a later .Trim()
        # throws, a transient file-system error, a mid-run tree change, ...) would otherwise skip the
        # stamp entirely and FREEZE seneschald-health.json at its last — usually 'ok' — value. And because
        # run-seneschald-update-hidden.vbs launches this fire-and-forget (Run cmd, 0, False), the scheduled
        # task's LastTaskResult is ALWAYS 0, so a crash here is invisible. That silent-green freeze — the
        # heartbeat stuck at ok while merges quietly stop arriving — is the exact failure this whole
        # health feature exists to kill, so any escape lands here as a blocked heartbeat: consecutive_blocked
        # keeps climbing cycle over cycle and the existing 3-cycle Telegram alert arms. A one-off transient
        # self-clears (the next cycle stamps 'ok' and resets the counter); only a persistent crash nags.
        try {
            $msg = "$($_.Exception.Message)" -replace '\s+', ' '
            $h = Set-SeneschaldHealth -Status 'blocked' -Reason 'update-error' -Branch '(unknown)' `
                -Detail "The Update cycle hit an unexpected error before it could finish: $msg" -Head '(unknown)'
            & $log "ERROR: Update crashed before it could finish — $msg"
            Send-SeneschaldAlert -Health $h -Log $log
        } catch { }
    }
    finally { Pop-Location }
}

switch ($Action) {
    'Stop' { Stop-Seneschald }
    'Start' { Start-Seneschald }
    'Restart' { Restart-Seneschald }
    'Update' { Update-Seneschald }
}
