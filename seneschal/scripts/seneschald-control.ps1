#Requires -Version 7
<#
.SYNOPSIS
    Control the resident seneschal presence daemon (the `seneschald` scheduled task).

.DESCRIPTION
    One entry point for stopping, starting, restarting, and updating seneschald.

      -Action Stop     Stop the scheduled task AND kill the running daemon (stopping the task alone does
                       NOT end the process), then clear the lock. Writes the deliberate-stop sentinel
                       (state/seneschald-stopped) so the auto-revive below never fights it.
      -Action Start    Start the scheduled task. Clears the deliberate-stop sentinel and the daemon's own
                       crash-loop give-up latch (a human starting it is the clean-slate signal).
      -Action Restart  Stop then Start (the old `reseneschald` behaviour). Default.
      -Action Update   Merge-detector: fetch the DEPLOY BRANCH ($DeployBranch below — `main`); if it
                       advanced, `git pull --ff-only`, then ask the *running* daemon to reload itself
                       gracefully (once its warm chat session is idle) via the control queue. No hard
                       kill, no lost conversation.

                       It also WATCHES THE DAEMON ITSELF and REVIVES it: a crashed presence.py that Task
                       Scheduler can no longer see (every graceful restart leaves a detached successor)
                       is restarted by starting the existing scheduled task, under a budget and a
                       settle-window check whose decision lives in seneschald_revive.py (CI-tested).
                       Alerts only when the revive did not take. See Invoke-PresenceRevive.

                       It also watches the CLAUDE ACCOUNT, because a login had no deploy path at all:
                       the warm `claude` session reads its auth once, at spawn, so `claude /login`
                       changed nothing until an unrelated merge happened to bounce the daemon. Update
                       compares ~/.claude.json's oauthAccount identity against the one presence.py
                       stamped into its lock at boot and, on a real change, asks for the SAME graceful
                       restart a merge does. It keys on the ACCOUNT, never on .credentials.json's
                       mtime, which every routine token refresh rewrites. See Invoke-CredentialCheck.

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

                       It also bounces the HEALTH LISTENER — a second, independent always-on task
                       (SCHEDULING.md §3) that otherwise has no restart hook at all, so a listener-code
                       PR would need a human to remember a manual restart every time (and a stale
                       process keeps answering with old code until someone does). Every successful pull
                       also drops a restart request into its OWN file (health-listener-control.json,
                       never the daemon's control-queue.json — two independent processes, two
                       independent requests); its background poll thread sees it and shuts itself down,
                       and its wrapper's respawn loop relaunches it with the freshly pulled code within
                       ~5s. See Request-HealthListenerRestart.

    Stop/Start/Restart touch the scheduled task + kill a PID, so they self-elevate to admin. Update only
    does git + drops control-queue entries, so it needs no elevation — that's what lets the lightweight
    `seneschald-update` scheduled task poll for merges without running as admin. (The health-listener
    bounce above is a plain file write for the same reason: no Stop-ScheduledTask/Stop-Process, which
    would need admin to touch a cross-session PID. The revive's Start-ScheduledTask of a limited-runlevel
    task the same user owns needs none either — and where a host refuses it, the revive fails loudly and
    degrades to alert-only.)

    This is the canonical, version-controlled control script. The local
    `~/Documents/powershell/restart-seneschald.ps1` (and the `reseneschald` alias) should delegate here:
        & "<repo>/seneschal/scripts/seneschald-control.ps1" -Action Restart

.NOTES
    A dot-source seam lets a test load this file's functions without running anything
    (`. seneschald-control.ps1`); the integration tests in test_seneschald_control.py drive the real
    script end to end.

    $DeployBranch IS THE ONE LINE THAT DECIDES WHAT DEPLOYS (main) -- every path in this file says the
    variable, never a literal, and Get-DaemonSyncArgs reads the cockpit opt-in sentinel
    (state/cockpit-enabled) to decide whether `uv sync` keeps the cockpit extras.

    THE BRANCH SELF-HEAL IS FAIL-CLOSED: it reclaims the deploy branch only when the parked branch
    provably carries nothing origin/$DeployBranch lacks; when it cannot tell, it does not touch anything.

    TWO INDEPENDENT HEALTH CHECKS EXIST, AND GREEN ON ONE SAYS NOTHING ABOUT THE OTHER: git/deploy health
    (state/seneschald-health.json, `last_ok` is the watch-the-watcher field, alert past
    $AlertAfterCycles, ~30 min) and daemon-alive health (state/presence-health.json, ~20 min) -- Update
    can log "up to date" for as long as you like over a dead brain.

    `last_alert` IS STAMPED ONLY ON A SEND THAT ACTUALLY LANDED -- a non-zero `python` exit does not throw
    in PowerShell, so an unchecked call would rate-limit its own retry away forever, which is exactly the
    silent-alerting bug this feature exists to kill.

    TWO SENTINELS VETO AN AUTO-REVIVE, both cleared only by a deliberate Start (or the daemon's own
    sustained-healthy self-heal): seneschald-stopped (a deliberate Stop and a crash are otherwise
    indistinguishable -- both remove the lock) and seneschald-crashloop (the daemon's OWN give-up latch;
    reviving into it would undo what it decided).

    A LOGIN DEPLOYS TOO: Invoke-CredentialCheck is a THIRD health check beside the other two, run ahead of
    every git early-return with its own try/catch. THE TRIGGER IS THE ACCOUNT IDENTITY, NEVER
    .credentials.json's MTIME. The key is (accountUuid, organizationUuid) from ~/.claude.json;
    organizationType/tier are deliberately excluded -- a plan upgrade is not a login -- and NO TOKEN IS
    EVER READ OR STAMPED. The boot stamp lives in presence.lock (one writer, dies with the process), never
    in the health file. It enqueues the SAME graceful request_control.py restart a merge does, and
    `credential_restart_for` caps that at ONE PER IDENTITY: if the daemon restarts (`started_at` moves)
    and is STILL wrong, it alerts instead of asking again. EVERY UNREADABLE SHAPE IS "TAKE NO ACTION."
    Full design: ../docs/seneschald-revive-spec.md.
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
# The deliberate-stop sentinel. A stop and a CRASH are otherwise indistinguishable: Stop-Seneschald kills
# the PID and removes the lock, and the crash path removes it too (presence.py's main_async finally ->
# release_lock), so Test-PresenceAlive reads $false either way. The auto-revive would therefore fight
# `-Action Stop` on the very next 10-minute cycle. Intent has to be WRITTEN, not inferred.
$StoppedFile = Join-Path $StateDir 'seneschald-stopped'
# The daemon's OWN self crash-loop guard (presence.py) drops this when it has booted too many times too
# fast and gives up relaunching. seneschald_revive.py refuses to auto-revive while it exists (same posture
# as seneschald-stopped), so a deliberate Start must clear it — a human intervening is exactly the "clean
# slate" signal — along with the boot-attempts window that fed the guard, so a fresh start isn't
# instantly re-tripped by the very boots that caused the give-up. presence.py also self-heals both after
# a sustained-healthy run.
$CrashloopFile = Join-Path $StateDir 'seneschald-crashloop'
$BootAttemptsFile = Join-Path $StateDir 'boot-attempts.json'
# Durable opt-in for the COCKPIT backend the daemon supervises. Its presence makes every deploy's
# `uv sync` keep the `cockpit` extras instead of pruning them — see Get-DaemonSyncArgs for the full why.
# Absent = the daemon's venv stays minimal (the default).
$CockpitOptInFile = Join-Path $StateDir 'cockpit-enabled'
# WHERE THE CLAUDE ACCOUNT IDENTITY LIVES — and deliberately NOT ~/.claude/.credentials.json. That file
# is rewritten on every routine OAuth token refresh (with nobody logging in), so keying off it would
# bounce the daemon on a timer forever. ~/.claude.json's `oauthAccount` names WHO is logged in, which
# changes only when the account does. CLAUDE_CONFIG_DIR relocates the CLI's config, so honour it when it
# actually holds the file. presence.py's claude_config_path() must resolve this the SAME two-step way:
# the guard is a comparison between what that stamped and what this reads, so two different files would
# make the comparison nonsense, not merely wrong.
$ClaudeConfigFile = if ($env:CLAUDE_CONFIG_DIR -and
                        (Test-Path (Join-Path $env:CLAUDE_CONFIG_DIR '.claude.json'))) {
    Join-Path $env:CLAUDE_CONFIG_DIR '.claude.json'
} else { Join-Path $HOME '.claude.json' }
$TaskName = 'seneschald'
$GitFlags = @('-c', 'core.fsmonitor=false')   # fsmonitor otherwise hangs in this repo

# health_listener.py's OWN restart-request file — deliberately NOT $StateDir/control-queue.json. The
# listener runs as its own always-on scheduled task, independent of seneschald by design (SCHEDULING.md
# §3: a seneschald reload must never drop the listener, and vice-versa), so sharing a queue would let a
# request meant for one process bounce the other. Consumed by health_listener.py's own
# `watch_restart_control` background thread, which polls this file and calls `server.shutdown()` — the
# wrapper's `run-health-listener.cmd` respawn loop then re-execs python with whatever code just landed.
# Written unconditionally on every successful pull, same trigger as the daemon's own graceful restart
# request, so a listener-code PR never needs a human to remember a manual restart.
$HealthListenerControlFile = Join-Path $StateDir 'health-listener-control.json'

# THE branch the live daemon tracks and auto-reloads from. Path A's whole design is that merging IS
# deploying, so this must be the branch merges land on — `main` here. Everything below says $DeployBranch
# rather than 'main' so this is a one-line change if the deploy branch ever moves.
$DeployBranch = 'main'

function Test-Admin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Dot-source seam. `. seneschald-control.ps1` loads the functions WITHOUT running anything — which is what
# makes this file testable. Without it, sourcing would fall straight through to the elevation gate below
# and, because $Action defaults to 'Restart', try to UAC-elevate and exit before a single test ran.
# $MyInvocation.InvocationName is '.' only when dot-sourced.
$script:SeneschaldDotSourced = ($MyInvocation.InvocationName -eq '.')

# Actions that touch the task/PID need admin; Update does not. Self-elevate only when required.
if (-not $script:SeneschaldDotSourced -and
    $Action -in 'Stop', 'Start', 'Restart' -and -not (Test-Admin)) {
    Start-Process pwsh "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action $Action" -Verb RunAs
    exit
}

function Stop-Seneschald {
    Write-Host '- stopping seneschald...'
    # Record the INTENT before doing anything else, so a stop that dies half-way still reads as
    # deliberate. Cheap to have a stale sentinel (Start-Seneschald and a healthy daemon both clear it);
    # expensive to lack one (the watchdog would revive a daemon the owner just deliberately stopped).
    try {
        New-Item -ItemType File -Path $StoppedFile -Force | Out-Null
    } catch { Write-Warning "stopped-sentinel write: $_" }
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
    # An explicit start revokes the deliberate-stop intent — otherwise the sentinel would outlive the
    # stop it described and suppress every future auto-revive. (presence.py clears it on a healthy
    # start too, so a sentinel can never strand the watchdog even if this path is bypassed.)
    Remove-Item $StoppedFile -Force -ErrorAction SilentlyContinue
    # Same clean-slate reasoning for the self crash-loop guard: a human deliberately starting the daemon
    # is intervening, so lift the give-up sentinel AND reset the boot-attempts window that tripped it —
    # otherwise the guard would immediately re-trip on the very boots that caused the give-up, and a
    # human trying to recover couldn't. (The daemon self-heals both after a sustained-healthy run, so
    # this only front-loads what would eventually happen anyway.)
    Remove-Item $CrashloopFile -Force -ErrorAction SilentlyContinue
    Remove-Item $BootAttemptsFile -Force -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName
}

function Restart-Seneschald {
    Stop-Seneschald
    Start-Seneschald
}

function Get-DaemonSyncArgs {
    # The argument list Sync-DaemonDeps hands to uv. Split out as its own function ONLY so it is
    # testable without spawning uv — same seam philosophy as seneschald_revive.py, which exists so the
    # revive/don't-revive decision can be tested apart from the process it drives.
    #
    # Why the sentinel: `uv sync` makes the environment MATCH the requested set, so it doesn't just
    # skip the cockpit extras — a plain `--frozen` actively UNINSTALLS fastapi/uvicorn if they're
    # present. That makes enabling the cockpit survive exactly until the next merged PR (on Path A,
    # merging IS deploying). `state/cockpit-enabled` is the durable opt-in: create it and every deploy
    # keeps the extras; delete it and the next deploy prunes them back out. The daemon still installs
    # nothing the owner has not explicitly asked for — the sentinel just makes "asked for it" persist.
    #
    # --frozen is unconditional: a deploy must never re-resolve the lockfile behind our back.
    $syncArgs = @('sync', '--frozen')
    if (Test-Path $CockpitOptInFile) { $syncArgs += @('--extra', 'cockpit') }
    return $syncArgs
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
    $syncArgs = Get-DaemonSyncArgs
    $shown = 'uv ' + ($syncArgs -join ' ')
    $out = & uv @syncArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        $tail = ($out | Select-Object -Last 3) -join ' | '
        & $log "DEPS FAILED: $shown exited $LASTEXITCODE — holding the restart. $tail"
        return $false
    }
    & $log "deps synced ($shown)"
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

# --- Presence-alive health: this check is INDEPENDENT of the git/deploy health above --------------
# seneschald-update staying green only ever means "the code is current" — it says nothing about whether
# presence.py itself is still running. A daemon killed by an unhandled task exception can sit dead while
# seneschald-update keeps logging "up to date" every 10 min, because the git-health checks above never
# look at the daemon process at all. Worse, that crash's exit(1) has nobody to catch it: Task Scheduler's
# RestartOnFailure only supervises the ORIGINAL process it launched, and the very first graceful restart
# (routine — every merge to $DeployBranch triggers one) replaces it with a fully DETACHED successor
# (presence.py's _respawn_detached — detaching is deliberate) that Task Scheduler can no longer see. So
# after that first restart, a crash is permanently silent until a human notices. This check closes that
# gap: sample every cycle, try a revive (Invoke-PresenceRevive), and alert past a threshold only when the
# revive did not take — never nag.
$PresenceHealthFile = Join-Path $StateDir 'presence-health.json'
$PresenceStaleSec = 180        # heartbeat ticks every ~5s (scheduler_task); 3 min is generous slack
$ReviveSettleSec = 90          # a revived daemon must stay up (heartbeat advancing) at least this long
                               # before a revive counts as a real recovery. A crash-looping daemon comes
                               # up, writes ONE heartbeat, and dies within a second; scoring that as
                               # success reset the revival budget every cycle so the give-up latch never
                               # fired. The judgement lives in seneschald_revive.py --confirm-sustained
                               # (CI-tested).
$PresenceAlertAfterCycles = 2  # ~20 min — a dead brain is more urgent than stale code
$PresenceReAlertHours = 2      # re-nag sooner than the deploy alert; this one nobody else will notice

function Write-MouthAssertion {
    # The Mouth (seneschal/docs/mouth-spec.md): record what the WATCHDOG told the owner into
    # state/assertions.jsonl, so the one log that answers "what does the owner currently believe" isn't
    # missing the messages that arrive precisely when the assistant itself can't speak.
    #
    # This script is a BYPASS MOUTH and stays one: it exists to talk when the daemon is dead, so it
    # keeps sending directly and only appends here BEST-EFFORT. Every failure is swallowed — a failed
    # append costs the append, never the alert. (Same reason it shells out to python rather than
    # sharing anything with the daemon: the component that reports a failure must not share a
    # dependency with the thing that failed.)
    param([string]$Text, [string]$Kind = 'alert')
    try {
        & python (Join-Path $PSScriptRoot 'mouth.py') --state-dir $StateDir record `
            --surface telegram --kind $Kind --speaker watchdog --text $Text 2>&1 | Out-Null
    } catch { }
}

function ConvertFrom-IsoUtc {
    # Normalize one of OUR health/lock timestamps (always WRITTEN as UTC with an explicit Z, see
    # Set-PresenceHealth) into a true UTC [datetime]. Returns $null on anything unparseable.
    #
    # Why this exists — a presence alert that once read "down ~-290 min". These fields reach us by two
    # different routes with two different TYPES, and that is the whole trap:
    #
    #   * fresh in-memory (this cycle set it)  -> [string] "2026-01-01T23:34:38Z"
    #   * round-tripped through the health file -> [datetime], because ConvertFrom-Json AUTO-PARSES an
    #     ISO-8601 string into a DateTime with Kind=Utc.
    #
    # Subtracting that Kind=Utc value from a local (Get-Date) does NOT normalize zones — .NET just
    # subtracts tick values, so the result is off by the full UTC offset (that is the negative number).
    # The same defect ran the other way in the re-alert throttles, inflating every gap by the offset so
    # a 2h/6h throttle expired in minutes and the watchdog could re-nag far more often than intended.
    #
    # (A bare `[datetime]$x` cast on a *string* really does convert to Local correctly — but the value
    # actually flowing through these call sites is usually the DESERIALIZED DateTime, which the cast
    # leaves untouched. Testing the wrong one of the two shapes is how the first fix shipped broken.)
    #
    # Always compare the result against (Get-Date).ToUniversalTime(), never (Get-Date).
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [datetime]) {
        # Kind=Utc -> itself; Kind=Local -> converted; Kind=Unspecified -> treated as local, which is
        # .NET's own default and the best available guess for a stamp we didn't write.
        return $Value.ToUniversalTime()
    }
    try {
        return [datetime]::Parse(
            [string]$Value, [cultureinfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor
            [System.Globalization.DateTimeStyles]::AssumeUniversal)
    } catch { return $null }
}

function ConvertTo-IsoUtcString {
    # The inverse of ConvertFrom-IsoUtc, and the other half of the same defence. Anything that came back
    # through ConvertFrom-Json is a [datetime], and letting PowerShell re-serialize that writes
    # "...T23:00:00.0000000Z" — a different string than the one we stored, drifting the on-disk format
    # away from what seneschald_revive.py and every other reader expect. Normalize to the ONE canonical
    # shape (second precision, explicit Z) on every write.
    param($Value)
    if ($null -eq $Value -or $Value -eq '') { return $null }
    $dt = ConvertFrom-IsoUtc $Value
    if (-not $dt) { return $null }
    return $dt.ToString('yyyy-MM-ddTHH:mm:ssZ')
}

function Test-PresenceAlive {
    if (-not (Test-Path $LockFile)) { return $false }
    try {
        $lock = Get-Content $LockFile -Raw -Encoding UTF8 | ConvertFrom-Json
        # ConvertFrom-IsoUtc, NOT [datetime]::Parse. ConvertFrom-Json has already turned the heartbeat
        # string into a Kind=Utc [datetime]; passing that to Parse() stringifies it through the current
        # culture and re-parses it as Kind=Unspecified, after which .ToUniversalTime() treats it as LOCAL
        # and shifts it into the future. A 30-min-stale heartbeat would compute a NEGATIVE age and read
        # as ALIVE — so a daemon that hung while leaving its lock behind would never be detected, alerted
        # on, or revived. (Only the lock-ABSENT path would work: a crash removes the lock.)
        $hb = ConvertFrom-IsoUtc $lock.heartbeat
        if (-not $hb) { return $false }
        return ((Get-Date).ToUniversalTime() - $hb).TotalSeconds -lt $PresenceStaleSec
    } catch {
        return $false  # unreadable/malformed lock (e.g. mid-write) reads as "can't confirm alive" — a
                        # single cycle of that self-corrects next pass; only sustained absence alerts.
    }
}

function Confirm-PresenceSustained {
    <#
      True only when presence.py has been ticking CONTINUOUSLY for >= $ReviveSettleSec — the honest
      "the revive actually took" signal, as opposed to Test-PresenceAlive's "there is a fresh heartbeat
      right now", which a crash-looping daemon satisfies for the one second it lives each boot.

      The judgement lives in seneschald_revive.py (--confirm-sustained), CI-tested, so this file stays a
      thin caller — same LAST-line-of-stdout-as-JSON contract as --decide / sentinel.py --branch-claimed,
      and the same FAIL-CLOSED default: if we can't confirm it's sustained, treat it as not-recovered.
    #>
    try {
        $out = "$(& python (Join-Path $PSScriptRoot 'seneschald_revive.py') `
                    --state-dir $StateDir --confirm-sustained `
                    --min-uptime-sec $ReviveSettleSec --stale-sec $PresenceStaleSec 2>$null |
                    Select-Object -Last 1)".Trim()
        if ($LASTEXITCODE -eq 0 -and $out) { return [bool]($out | ConvertFrom-Json).sustained }
    } catch {}
    return $false
}

function Set-PresenceHealth {
    param([bool]$Alive)
    $prev = if (Test-Path $PresenceHealthFile) { try { Get-Content $PresenceHealthFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $null } } else { $null }
    # UTC, explicit Z. `Get-Date -Format 's'` writes machine-LOCAL wall-clock time with no offset marker,
    # so every age computed off these fields would be inflated by the UTC offset — a fresh heartbeat
    # read as hours stale. Every reader of these fields goes through ConvertFrom-IsoUtc (which handles
    # both the string and the deserialized-[datetime] shape) and compares in UTC; do not reintroduce a
    # bare cast.
    $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $h = [ordered]@{
        status = if ($Alive) { 'ok' } else { 'down' }
        consecutive_down = 0
        down_since = $null
        last_alive = if ($Alive) { $now } elseif ($prev) { $prev.last_alive } else { $null }
        last_alert = if ($prev) { $prev.last_alert } else { $null }
        # Auto-revive bookkeeping. These MUST carry forward explicitly, exactly like last_alert above:
        # this function rebuilds the record from scratch every cycle, so a field that isn't restored from
        # $prev is silently dropped every 10 minutes. A revival counter that quietly reset would never
        # reach the give-up threshold, and the crash-loop guard would look present while doing nothing.
        # seneschald_revive.py owns the VALUES (see Invoke-PresenceRevive); this only persists them.
        revivals = if ($prev -and $null -ne $prev.revivals) { [int]$prev.revivals } else { 0 }
        revival_window_start = if ($prev) { ConvertTo-IsoUtcString $prev.revival_window_start } else { $null }
        revival_gave_up = if ($prev -and $null -ne $prev.revival_gave_up) { [bool]$prev.revival_gave_up } else { $false }
        updated_at = $now
    }
    if (-not $Alive) {
        $h.consecutive_down = 1 + $(if ($prev -and $prev.status -eq 'down') { [int]$prev.consecutive_down } else { 0 })
        $h.down_since = if ($prev -and $prev.status -eq 'down' -and $prev.down_since) { $prev.down_since } else { $now }
    }
    try { ($h | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $PresenceHealthFile -Encoding UTF8 } catch {}
    return $h
}

function Send-PresenceAlert {
    # -Revived: the daemon DID come back, but this is a repeat inside the crash-loop window. Different
    # message, and it deliberately bypasses the consecutive-down threshold below — a self-healed cycle
    # only ever reaches consecutive_down = 1, so the normal gate would swallow the very alert that says
    # "it keeps falling over". Flapping is the thing worth telling the owner about; a single clean
    # recovery is not.
    param($Health, $Log, [switch]$Revived)
    if (-not $Health -or $Health.status -eq 'ok') { return }
    if (-not $Revived -and [int]$Health.consecutive_down -lt $PresenceAlertAfterCycles) { return }
    if ($Health.last_alert) {
        $lastAlert = ConvertFrom-IsoUtc $Health.last_alert
        if ($lastAlert -and ((Get-Date).ToUniversalTime() - $lastAlert).TotalHours -lt $PresenceReAlertHours) { return }
    }
    $envFile = Join-Path $PSScriptRoot 'telegram.env'
    if (-not (Test-Path $envFile)) { & $Log 'PRESENCE ALERT suppressed: no telegram.env'; return }
    $mins = 0
    $downSince = ConvertFrom-IsoUtc $Health.down_since
    if ($downSince) { $mins = [int]((Get-Date).ToUniversalTime() - $downSince).TotalMinutes }
    $text = if ($Revived) {
        "This is the seneschald watchdog, not the assistant. presence.py died and I restarted it - it's " +
        "back up. Telling you because that's more than once in the last hour, so something is making it " +
        "fall over repeatedly. Worth a look at seneschal/state/presence.log."
    } else {
        "This is the seneschald watchdog, not the assistant. presence.py looks dead: no live lock " +
        "heartbeat for ~$mins min ($($Health.consecutive_down) checks). Telegram/Discord/reminders are " +
        "unanswered until someone runs seneschald-control.ps1 -Action Restart."
    }
    # Same not-rate-limited-on-failure shape as Send-SeneschaldAlert, for the same reason: a silent send
    # failure here would rate-limit away the only alert this outage gets.
    $sendOut = ''
    try { $sendOut = "$(& python (Join-Path $PSScriptRoot 'telegram_send.py') --text $text --env-file $envFile 2>&1)" }
    catch { $sendOut = "$_"; $global:LASTEXITCODE = 1 }
    if ($LASTEXITCODE -eq 0 -and $sendOut -notmatch '"ok"\s*:\s*false') {
        & $Log "PRESENCE ALERTED the owner on Telegram (down ~$mins min)"
        Write-MouthAssertion -Text $text
        $Health.last_alert = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')  # UTC — see Set-PresenceHealth
        try { ($Health | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $PresenceHealthFile -Encoding UTF8 } catch {}
    } else {
        & $Log "presence alert FAILED (telegram_send exit $LASTEXITCODE) — NOT rate-limiting; retries next cycle. $($sendOut -replace '\s+', ' ')"
    }
}

function Save-PresenceRevivalState {
    # Persist ONLY the three revival fields seneschald_revive.py computed, leaving the rest of the record
    # alone. Best-effort by design: losing a counter write costs at most one extra revival attempt,
    # whereas throwing here would escape into the unprotected pre-try region (see Invoke-PresenceRevive).
    param($NextState)
    if (-not $NextState) { return }
    try {
        $rec = if (Test-Path $PresenceHealthFile) {
            Get-Content $PresenceHealthFile -Raw -Encoding UTF8 | ConvertFrom-Json
        } else { $null }
        if (-not $rec) { return }
        $rec.revivals             = [int]$NextState.revivals
        $rec.revival_window_start = ConvertTo-IsoUtcString $NextState.revival_window_start
        $rec.revival_gave_up      = [bool]$NextState.revival_gave_up
        ($rec | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $PresenceHealthFile -Encoding UTF8
    } catch {}
}

function Invoke-PresenceRevive {
    <#
      Try to bring a dead daemon back. Returns $true ONLY when a revive was attempted AND the daemon was
      observed healthy afterwards — the caller uses that to decide whether the owner still needs alerting.

      Why the daemon needs reviving at all: after the first graceful restart of any day, presence.py's
      _respawn_detached leaves a DETACHED successor that Task Scheduler cannot see, so its
      RestartCount can never fire for a later crash (see the presence-alive note above). This closes that
      gap on the 10-minute update cadence.

      The DECISION is not made here. seneschald_revive.py owns it, because process-restart logic with a
      spend-relevant budget belongs somewhere CI can prove it. Same shape as the sentinel.py
      --branch-claimed consult below: take the LAST stdout line, and FAIL CLOSED — an unclear verdict
      means do not revive.
    #>
    param($Log)

    $verdict = $null
    try {
        $out = "$(& python (Join-Path $PSScriptRoot 'seneschald_revive.py') `
                    --state-dir $StateDir --decide 2>$null | Select-Object -Last 1)".Trim()
        if ($LASTEXITCODE -eq 0 -and $out) { $verdict = $out | ConvertFrom-Json }
    } catch { $verdict = $null }

    if (-not $verdict) {
        & $Log 'revive: SKIPPED — seneschald_revive.py gave no usable verdict (failing closed).'
        return $false
    }

    # Persist whatever the predicate computed even when it says no: that is how the give-up latches.
    Save-PresenceRevivalState -NextState $verdict.next_state

    if (-not $verdict.revive) {
        switch ($verdict.reason) {
            'deliberately-stopped' { & $Log 'revive: SKIPPED — deliberately stopped (seneschald-stopped sentinel present).' }
            'crash-looping'        { & $Log 'revive: SKIPPED — the daemon''s own crash-loop guard gave up (seneschald-crashloop sentinel present). It already alerted the owner; clear it with -Action Start once the crash is fixed.' }
            'budget-exhausted'     { & $Log "revive: GIVING UP — $($verdict.next_state.revivals) revivals inside the window produced no healthy daemon. No more attempts until it recovers or you intervene." }
            'already-gave-up'      { & $Log 'revive: SKIPPED — already gave up this window.' }
            default                { & $Log "revive: SKIPPED — $($verdict.reason)." }
        }
        return $false
    }

    # Start the EXISTING scheduled task rather than spawning presence.py ourselves. run-presence.cmd is
    # not a thin wrapper: it clears ANTHROPIC_API_KEY to force SUBSCRIPTION billing, sets PYTHONUTF8,
    # prefers the uv venv interpreter, and carries every daemon flag. Hand-rolling that risks silently
    # flipping the daemon onto metered API billing. MultipleInstances=IgnoreNew makes a redundant start a
    # no-op rather than a second brain.
    & $Log "revive: attempting (attempt $($verdict.next_state.revivals) in this window)..."
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    } catch {
        # A UAC-enabled host (or a task registered under a different principal) may refuse this. Fail
        # LOUDLY and fall through to the alert — a watchdog that quietly stops watching is worse than one
        # that never existed.
        & $Log "revive: FAILED to start the task — $_. Falling back to alert-only."
        return $false
    }

    # Verify the revive actually TOOK — not merely that the daemon twitched. Reporting a success we
    # didn't achieve is the same class of bug as the alert that once said "down ~-290 min", and it is
    # exactly what lets a crash-loop stay "revived" for hours: the daemon boots, writes one heartbeat,
    # and dies within a second, and a check that returns success on that first heartbeat resets the
    # revival budget every cycle, so the give-up latch never fires. So: wait for it to boot, then require
    # it to survive a full settle window before counting it recovered.
    $booted = $false
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Seconds 2
        if (Test-PresenceAlive) { $booted = $true; break }
    }
    if (-not $booted) {
        & $Log 'revive: task started but no heartbeat within ~20s — treating as failed.'
        return $false
    }
    & $Log "revive: booted — confirming it survives ${ReviveSettleSec}s before counting it recovered..."
    Start-Sleep -Seconds $ReviveSettleSec
    if (Confirm-PresenceSustained) {
        & $Log "revive: SUCCEEDED — daemon has been up >= ${ReviveSettleSec}s."
        # Observed SUSTAINED-healthy: clear the counter so an unrelated future crash gets a full budget.
        try {
            $reset = "$(& python (Join-Path $PSScriptRoot 'seneschald_revive.py') `
                          --state-dir $StateDir --record-healthy 2>$null | Select-Object -Last 1)".Trim()
            if ($LASTEXITCODE -eq 0 -and $reset) { Save-PresenceRevivalState -NextState ($reset | ConvertFrom-Json) }
        } catch {}
        return $true
    }
    & $Log "revive: FAILED — daemon came up but did not survive ${ReviveSettleSec}s (crash-loop). Revival budget stands, so repeated failures reach the give-up latch and alert the owner."
    return $false
}

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
    # UTC, explicit Z — see the matching comment in Set-PresenceHealth above for why.
    $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $h = [ordered]@{
        status = $Status; reason = $Reason; detail = $Detail; branch = $Branch; head = $Head
        consecutive_blocked = 0
        blocked_since = $null
        last_ok = if ($prev) { ConvertTo-IsoUtcString $prev.last_ok } else { $null }
        last_alert = if ($prev) { ConvertTo-IsoUtcString $prev.last_alert } else { $null }
        # Credential guard (seneschald-revive-spec.md §9). SAME PREV-CARRY TRAP as the revival fields in
        # Set-PresenceHealth: this record is rebuilt from scratch every cycle, so a field without
        # explicit carry logic is silently dropped every 10 minutes. Here that is not a lost counter but
        # a RESTART LOOP — credential_restart_for is the only memory of what we already asked for, and a
        # guard that forgets it re-asks every cycle, forever.
        credential_state = if ($prev) { $prev.credential_state } else { $null }
        credential_detail = if ($prev) { $prev.credential_detail } else { $null }
        credential_restart_for = if ($prev) { $prev.credential_restart_for } else { $null }
        credential_restart_at = if ($prev) { ConvertTo-IsoUtcString $prev.credential_restart_at } else { $null }
        credential_stuck_cycles = if ($prev -and $null -ne $prev.credential_stuck_cycles) { [int]$prev.credential_stuck_cycles } else { 0 }
        credential_last_alert = if ($prev) { ConvertTo-IsoUtcString $prev.credential_last_alert } else { $null }
        updated_at = $now
    }
    if ($Status -eq 'ok') {
        $h.last_ok = $now
    } else {
        $h.consecutive_blocked = 1 + $(if ($prev -and $prev.status -ne 'ok') { [int]$prev.consecutive_blocked } else { 0 })
        $h.blocked_since = if ($prev -and $prev.status -ne 'ok' -and $prev.blocked_since) { ConvertTo-IsoUtcString $prev.blocked_since } else { $now }
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
        $lastAlert = ConvertFrom-IsoUtc $Health.last_alert
        if ($lastAlert -and ((Get-Date).ToUniversalTime() - $lastAlert).TotalHours -lt $ReAlertHours) { return }
    }
    $envFile = Join-Path $PSScriptRoot 'telegram.env'
    if (-not (Test-Path $envFile)) { & $Log 'ALERT suppressed: no telegram.env'; return }
    $mins = 0
    $blockedSince = ConvertFrom-IsoUtc $Health.blocked_since
    if ($blockedSince) { $mins = [int]((Get-Date).ToUniversalTime() - $blockedSince).TotalMinutes }
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
        Write-MouthAssertion -Text $text
        $Health.last_alert = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')  # UTC — see Set-PresenceHealth
        try { ($Health | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $HealthFile -Encoding UTF8 } catch {}
    } else {
        & $Log "alert FAILED (telegram_send exit $LASTEXITCODE) — NOT rate-limiting; retries next cycle. $($sendOut -replace '\s+', ' ')"
    }
}

# --- Credential health: a login is a deploy with no deploy path ----------------------------------
# A code merge deploys itself (fetch -> pull -> uv sync -> graceful restart). A CREDENTIAL change had
# NO path at all: the warm `claude` CLI session reads its auth when it spawns, so `claude /login` did
# nothing until something else happened to bounce the daemon. A swap that "works" only because an
# unrelated merge restarted the daemon minutes later is coincidence, not mechanism. Log in on a quiet
# evening with nothing merging and the daemon runs the OLD token indefinitely, whose only symptom is
# "why am I still hitting the old account's limit" — which reads like a BILLING problem, so the search
# starts in exactly the wrong place.
#
# THE PREDICATE IS THE ACCOUNT IDENTITY, NOT A FILE MTIME. See $ClaudeConfigFile above for why the
# obvious trigger (.credentials.json's mtime) is wrong. Rationale: seneschal/docs/seneschald-revive-spec.md §9.
$CredentialStuckAlertCycles = 6   # ~60 min at the 10-min cadence. A requested restart legitimately
                                  # waits for the warm session to go idle, so "pending" is NORMAL for
                                  # a while and must not nag; an hour of it is worth saying.
$CredentialReAlertHours = 6       # same throttle as the deploy alert - the owner has already been told

function Get-ClaudeIdentity {
    <#
      The account identity currently ON DISK, as {key; email} - or $null meaning "I could not tell",
      which every caller MUST treat as *take no action*. An unreadable file is never evidence that the
      account changed.

      THE KEY IS (accountUuid, organizationUuid) AND NOTHING ELSE. The other oauthAccount fields were
      each considered and each rejected:
        * emailAddress - derived from the account and independently mutable. Renaming an email must not
          bounce the daemon. Carried alongside for the log line and the nudge, never compared.
        * organizationType / organizationRateLimitTier - a plan upgrade or a server-side tier re-bucket
          changes these while the credential is untouched. A plan change is not a login.
      Both halves of the key are real events: a different accountUuid is a different person, and a
      different organizationUuid is a different workspace to bill and rate-limit against.
    #>
    param([string]$Path)
    $file = if ($Path) { $Path } else { $ClaudeConfigFile }
    if (-not $file -or -not (Test-Path -LiteralPath $file)) { return $null }
    $cfg = $null
    try { $cfg = Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
    if ($null -eq $cfg) { return $null }
    $prop = $cfg.PSObject.Properties['oauthAccount']
    if (-not $prop -or $prop.Value -isnot [pscustomobject]) { return $null }
    $acct = $prop.Value
    if ([string]::IsNullOrWhiteSpace([string]$acct.accountUuid)) { return $null }
    return [pscustomobject]@{
        key   = '{0}/{1}' -f [string]$acct.accountUuid, [string]$acct.organizationUuid
        email = [string]$acct.emailAddress
    }
}

function Get-DaemonAuthStamp {
    <#
      The identity the RUNNING daemon actually started with, as {key; email; started_at} - or $null.

      It is read from presence.lock, not from a file of our own, because that lock IS the record of the
      running process's facts (pid, started_at, heartbeat) and an auth identity is exactly one of those.
      Two properties fall out for free and both are load-bearing:
        * it dies with the process, so a stale identity claim can never outlive the daemon that made it;
        * presence.py is its ONLY writer, so this script never has to read-modify-write a file the
          daemon is also rebuilding - which is how a two-writer seneschald-health.json would quietly
          drop the stamp mid-cycle.
      $null (no lock, malformed lock, or a daemon that booted without the stamp) means "nothing to
      compare", never "the account changed".

      started_at goes through ConvertTo-IsoUtcString for the same reason every other timestamp here
      does: ConvertFrom-Json has already turned it into a [datetime], and comparing that against a
      stored string would never match. Second precision truncates presence.py's microseconds, so two
      boots inside the same second read as one - vanishingly unlikely, and it fails toward "not
      restarted yet" (wait) rather than toward a loop.
    #>
    if (-not (Test-Path -LiteralPath $LockFile)) { return $null }
    $lock = $null
    try { $lock = Get-Content -LiteralPath $LockFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
    if ($null -eq $lock) { return $null }
    $prop = $lock.PSObject.Properties['claude_identity']
    if (-not $prop -or $prop.Value -isnot [pscustomobject]) { return $null }
    $ident = $prop.Value
    if ([string]::IsNullOrWhiteSpace([string]$ident.account_uuid)) { return $null }
    return [pscustomobject]@{
        key        = '{0}/{1}' -f [string]$ident.account_uuid, [string]$ident.organization_uuid
        email      = [string]$ident.email
        started_at = ConvertTo-IsoUtcString $lock.started_at
    }
}

function Save-CredentialState {
    # Persist ONLY the credential fields, leaving the rest of the health record alone - the same
    # narrow-write shape as Save-PresenceRevivalState.
    #
    # Unlike that one it CREATES the record when none exists, and that is load-bearing rather than
    # tidy: this check runs ahead of the cycle's Set-SeneschaldHealth, so on a fresh host there is no
    # file yet. If a missing file made this a no-op, credential_restart_for would never persist, the
    # next cycle would see no memory of the restart it already asked for, and it would enqueue again
    # every cycle forever. The loop guard IS this write.
    param([string]$State, [string]$Detail, [string]$RestartFor, [string]$RestartAt, [int]$StuckCycles)
    try {
        $rec = [ordered]@{}
        $prev = Get-SeneschaldHealth
        if ($prev) { foreach ($prop in $prev.PSObject.Properties) { $rec[$prop.Name] = $prop.Value } }
        $rec['credential_state'] = $State
        $rec['credential_detail'] = $Detail
        $rec['credential_restart_for'] = if ([string]::IsNullOrEmpty($RestartFor)) { $null } else { $RestartFor }
        $rec['credential_restart_at'] = if ([string]::IsNullOrEmpty($RestartAt)) { $null } else { $RestartAt }
        $rec['credential_stuck_cycles'] = $StuckCycles
        if (-not $rec.Contains('credential_last_alert')) { $rec['credential_last_alert'] = $null }
        # Re-normalize every timestamp on the way out. Copying $prev forward means copying values
        # ConvertFrom-Json already turned into [datetime]s, and re-serializing those writes
        # "...T23:00:00.0000000Z" - a different string than the one we stored, drifting the on-disk
        # format away from what the cockpit's readers.py and this script's own ConvertFrom-IsoUtc
        # callers expect. Same defence as Save-PresenceRevivalState's.
        foreach ($ts in @('last_ok', 'last_alert', 'blocked_since', 'updated_at',
                          'credential_restart_at', 'credential_last_alert')) {
            if ($rec.Contains($ts) -and $rec[$ts]) { $rec[$ts] = ConvertTo-IsoUtcString $rec[$ts] }
        }
        ($rec | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $HealthFile -Encoding UTF8
    } catch {}
}

function Send-CredentialAlert {
    # The third alerter in this file, alongside Send-SeneschaldAlert (deploy) and Send-PresenceAlert
    # (daemon-alive), and it carries their whole discipline: silent no-op with no telegram.env, and
    # the last-alert stamp written ONLY on a send that actually landed, so a silent failure retries
    # next cycle instead of rate-limiting away the only warning this gets.
    #
    # It needs its own counter and throttle rather than riding consecutive_blocked, because the git
    # phase re-stamps status='ok' later in the same cycle and would reset a shared counter to 0 every
    # pass - the alert would never reach its threshold.
    param($Health, $Log)
    if (-not $Health) { return }
    $state = [string]$Health.credential_state
    if ($state -ne 'restart-ineffective' -and $state -ne 'restart-pending') { return }
    # A pending restart is the NORMAL state while the warm session finishes a turn. Only one that has
    # been pending about an hour is worth interrupting the owner for; an ineffective one is worth it
    # immediately, because that one will never fix itself.
    if ($state -eq 'restart-pending' -and
        [int]$Health.credential_stuck_cycles -lt $CredentialStuckAlertCycles) { return }
    if ($Health.credential_last_alert) {
        $lastAlert = ConvertFrom-IsoUtc $Health.credential_last_alert
        if ($lastAlert -and ((Get-Date).ToUniversalTime() - $lastAlert).TotalHours -lt $CredentialReAlertHours) { return }
    }
    $envFile = Join-Path $PSScriptRoot 'telegram.env'
    if (-not (Test-Path $envFile)) { & $Log 'CREDENTIAL ALERT suppressed: no telegram.env'; return }
    # No stray backticks in these strings beyond the deliberate newlines: in a double-quoted PowerShell
    # string the backtick is the escape character, so one in a word silently eats the next letter.
    $text = if ($state -eq 'restart-ineffective') {
        "Heads up - I am still running on the old Claude account.`n`n" +
        "$($Health.credential_detail)`n`n" +
        "I asked myself for a graceful restart, I came back, and I am still on the old one - so I have " +
        "stopped asking rather than loop. Running seneschald-control.ps1 -Action Restart (reseneschald) forces it."
    } else {
        "Heads up - I am still on the previous Claude account.`n`n" +
        "$($Health.credential_detail)`n`n" +
        "I asked myself for a graceful restart so I would pick it up, but it has not applied yet - it " +
        "waits for my warm session to go idle. If that looks stuck, reseneschald forces it."
    }
    $sendOut = ''
    try { $sendOut = "$(& python (Join-Path $PSScriptRoot 'telegram_send.py') --text $text --env-file $envFile 2>&1)" }
    catch { $sendOut = "$_"; $global:LASTEXITCODE = 1 }
    if ($LASTEXITCODE -eq 0 -and $sendOut -notmatch '"ok"\s*:\s*false') {
        & $Log "CREDENTIAL ALERTED the owner on Telegram ($state)"
        Write-MouthAssertion -Text $text
        try {
            $rec = Get-SeneschaldHealth
            if ($rec) {
                $stampedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
                Save-CredentialState -State ([string]$rec.credential_state) `
                    -Detail ([string]$rec.credential_detail) `
                    -RestartFor ([string]$rec.credential_restart_for) `
                    -RestartAt (ConvertTo-IsoUtcString $rec.credential_restart_at) `
                    -StuckCycles ([int]$rec.credential_stuck_cycles)
                $rec = Get-SeneschaldHealth
                $rec.credential_last_alert = $stampedAt
                ($rec | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $HealthFile -Encoding UTF8
                $Health.credential_last_alert = $stampedAt
            }
        } catch {}
    } else {
        & $Log "credential alert FAILED (telegram_send exit $LASTEXITCODE) - NOT rate-limiting; retries next cycle. $($sendOut -replace '\s+', ' ')"
    }
}

function Invoke-CredentialCheck {
    <#
      Compare the account identity on disk against the one the running daemon booted with, and on a
      real change ask for the SAME graceful restart a merge asks for. Never a second restart mechanism,
      never a hard kill.

      FAIL SAFE MEANS DO NOTHING. A missing / unreadable / oauthAccount-less config and an unstamped
      daemon are "I could not tell", which must never be read as "the account changed" — each is a fact
      we record rather than a silence.

      THE LOOP GUARD is credential_restart_for: at most one restart is ever enqueued per identity. If
      the daemon then RESTARTS (its started_at moves) and is still on the old account, the swap did not
      take - that is the alert case, and we stop asking. If it has not restarted yet, the request is
      simply still queued behind a busy warm session, which is normal and stays quiet for an hour.
    #>
    param($Log)

    $onDisk = Get-ClaudeIdentity
    if (-not $onDisk) {
        Save-CredentialState -State 'unreadable' -StuckCycles 0 `
            -Detail 'Could not read a Claude account identity from ~/.claude.json (missing, unreadable, or no oauthAccount). Taking no action - an unreadable file is not an account change.'
        & $Log 'credentials: no readable account identity on disk - taking no action.'
        return
    }

    $stamp = Get-DaemonAuthStamp
    if (-not $stamp) {
        Save-CredentialState -State 'unstamped' -StuckCycles 0 `
            -Detail "Nothing to compare: the running daemon left no account stamp (it is down, or it booted without one). Its next boot stamps one. Disk says $($onDisk.email)."
        & $Log 'credentials: the daemon left no account stamp - nothing to compare.'
        return
    }

    if ($onDisk.key -eq $stamp.key) {
        $prev = Get-SeneschaldHealth
        if ($prev -and $prev.credential_restart_for) {
            & $Log "credentials: the daemon is now on the current account ($($onDisk.email)) - clearing the pending credential restart."
        }
        Save-CredentialState -State 'ok' -RestartFor '' -RestartAt '' -StuckCycles 0 `
            -Detail "Daemon and disk agree ($($onDisk.email))."
        return
    }

    # --- MISMATCH: the daemon is running an account that is no longer the logged-in one.
    $prev = Get-SeneschaldHealth
    $alreadyAsked = ($prev -and [string]$prev.credential_restart_for -eq $onDisk.key)
    if ($alreadyAsked) {
        # ConvertTo-IsoUtcString, not a bare cast: this value round-tripped through ConvertFrom-Json
        # and is a [datetime] by now, which would never string-compare equal to the stamp.
        $askedAt = ConvertTo-IsoUtcString $prev.credential_restart_at
        $restarted = ($stamp.started_at -and $askedAt -and $stamp.started_at -ne $askedAt)
        $stuck = 1 + $(if ($null -ne $prev.credential_stuck_cycles) { [int]$prev.credential_stuck_cycles } else { 0 })
        if ($restarted) {
            & $Log "credentials: the daemon RESTARTED and is STILL on $($stamp.email) while the disk says $($onDisk.email) - the swap did not take. Not asking again."
            Save-CredentialState -State 'restart-ineffective' -RestartFor $onDisk.key -RestartAt $askedAt -StuckCycles $stuck `
                -Detail "You are logged in as $($onDisk.email); I restarted and came back still running as $($stamp.email)."
        } else {
            & $Log "credentials: restart already requested for $($onDisk.email); the daemon has not restarted yet (warm session busy?) - waiting ($stuck cycles)."
            Save-CredentialState -State 'restart-pending' -RestartFor $onDisk.key -RestartAt $askedAt -StuckCycles $stuck `
                -Detail "You are logged in as $($onDisk.email); I am still running as $($stamp.email)."
        }
        Send-CredentialAlert -Health (Get-SeneschaldHealth) -Log $Log
        return
    }

    & $Log "credentials: account changed ($($stamp.email) -> $($onDisk.email)) - requesting a graceful restart so I pick it up."
    $enqueueOut = ''
    try {
        $enqueueOut = "$(& python (Join-Path $RepoRoot 'seneschal\scripts\request_control.py') --action restart --reason 'claude account changed' 2>&1)"
    } catch { $enqueueOut = "$_"; $global:LASTEXITCODE = 1 }
    if ($LASTEXITCODE -ne 0) {
        # Record NOTHING about having asked, so the next cycle retries. Same discipline as last_alert:
        # a non-zero python exit does not throw in PowerShell, and booking a request we never made
        # would latch the loop guard shut over a restart that never happened.
        & $Log "credentials: could not enqueue the restart (request_control exit $LASTEXITCODE) - not recording it; retrying next cycle. $($enqueueOut -replace '\s+', ' ')"
        Save-CredentialState -State 'enqueue-failed' -RestartFor '' -RestartAt '' -StuckCycles 0 `
            -Detail "You are logged in as $($onDisk.email) but I could not queue my own restart to pick it up."
        return
    }
    Save-CredentialState -State 'restart-requested' -RestartFor $onDisk.key -RestartAt $stamp.started_at -StuckCycles 0 `
        -Detail "You logged in as $($onDisk.email); I am running as $($stamp.email), so I asked for a graceful restart."
}

function Request-HealthListenerRestart {
    <#
      Ask health_listener.py to restart itself on its own ~5s poll cycle. NOT the daemon's
      request_control.py path (that queue has a different consumer with different semantics —
      warm-session-idle deferral makes no sense for a stateless per-request listener) and NOT a
      Stop-ScheduledTask/Stop-Process kill (that would need elevation for a cross-session PID, which
      would break Update's whole reason for being an unprivileged scheduled task). Just a plain file
      write health_listener.py's own background thread polls — no admin, no PID, no cross-session
      call at all.

      Never throws: a failed write costs one restart request, not the deploy cycle. The next
      successful cycle (or the next merge) writes another.
    #>
    param([string]$Reason, $Log)
    try {
        $doc = [ordered]@{
            action = 'restart'
            reason = $Reason
            requested_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        }
        ($doc | ConvertTo-Json) | Set-Content -LiteralPath $HealthListenerControlFile -Encoding UTF8
        if ($Log) { & $Log 'health-listener: restart requested (applies within ~5s of its own poll)' }
    } catch {
        if ($Log) { & $Log "health-listener: could not request a restart - $_" }
    }
}

function Update-Seneschald {
    # Graceful, no admin. Runs from the seneschald-update scheduled task, which DISCARDS console output — so
    # every outcome is logged to state/seneschald-update.log too, or a silent skip/failure is invisible (a
    # detached HEAD once made this no-op for ~20 min with nobody the wiser).
    $logFile = Join-Path $StateDir 'seneschald-update.log'
    # Dropped when a pull landed but the dep sync failed: the restart is deferred (never onto a
    # half-updated env) and re-attempted here every cycle until the sync succeeds.
    $pendingRestart = Join-Path $StateDir 'pending-restart'
    $log = {
        param($m)
        # Machine-local on purpose: this log is read by a human at the machine. Every stamp that code
        # READS (the health files, the lock) is UTC with an explicit Z instead — see Set-PresenceHealth.
        $line = '[{0}] {1}' -f (Get-Date -Format 's'), $m
        Write-Host "- $m"
        try { Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8 } catch {}
    }

    # Presence-alive check runs first and unconditionally — independent of every git early-return
    # below, so a dead daemon gets flagged on schedule no matter what state the checkout is in.
    #
    # NOTE the try/catch below is not decorative. This whole block sits OUTSIDE the try at the top of the
    # git phase, and $ErrorActionPreference is 'Stop' globally — so a terminating error here would escape
    # the catch-all at the bottom of this function, skip the health stamp, and (because the VBS launcher
    # is fire-and-forget, so the task result is always 0) freeze seneschald-health.json silently at its
    # last 'ok'. That silent-failure mode is the exact thing this health feature exists to kill.
    $presenceAlive = Test-PresenceAlive
    $presenceHealth = Set-PresenceHealth -Alive $presenceAlive
    if ($presenceAlive) {
        & $log 'presence: alive'
        # Healthy sample clears any revival counter, so an unrelated crash weeks later gets a full budget.
        # But only a SUSTAINED sample (up >= settle window) may clear it: a crash-loop twitch that happens
        # to be mid-heartbeat at check time must not silently forgive an active crisis — the same failure
        # the revive-side settle check closes. A genuinely healthy daemon has been up far longer than the
        # window, so Confirm-PresenceSustained is trivially true for it; at worst the reset waits one cycle.
        try {
            if (($presenceHealth.revivals -gt 0 -or $presenceHealth.revival_gave_up) -and (Confirm-PresenceSustained)) {
                $reset = "$(& python (Join-Path $PSScriptRoot 'seneschald_revive.py') `
                              --state-dir $StateDir --record-healthy 2>$null | Select-Object -Last 1)".Trim()
                if ($LASTEXITCODE -eq 0 -and $reset) {
                    Save-PresenceRevivalState -NextState ($reset | ConvertFrom-Json)
                    & $log 'presence: healthy again — revival counter reset'
                }
            }
        } catch { & $log "presence: revival-counter reset failed (non-fatal) — $_" }
    } else {
        & $log "presence: DOWN (consecutive checks: $($presenceHealth.consecutive_down))"
        # Revive FIRST, alert only if that didn't work. Fixing it before the owner is told is the point;
        # the alert is what's left when we couldn't.
        $revived = $false
        try { $revived = Invoke-PresenceRevive -Log $log }
        catch { & $log "revive: ERRORED (non-fatal, alerting instead) — $_" }
        if ($revived) {
            # A clean one-off self-heal is logged, not sent. Telegram is reserved for a repeat inside the
            # window, a failed revive, or give-up — quiet when it's working, loud when it isn't. Re-read
            # the record so the count reflects what the revive just persisted.
            $post = try { Get-Content $PresenceHealthFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $null }
            if ($post -and [int]$post.revivals -ge 2) {
                Send-PresenceAlert -Health $presenceHealth -Log $log -Revived
            }
        } else {
            Send-PresenceAlert -Health $presenceHealth -Log $log
        }
    }

    # Credential check: also ahead of every git early-return, and for the same reason the presence
    # check is. Whether the owner logged into a different Claude account has nothing to do with the
    # state of the checkout, so a blocked pull must not be able to swallow it.
    #
    # Its OWN try/catch is mandatory, not decorative: this block sits outside the git phase's try and
    # $ErrorActionPreference is 'Stop', so a terminating error here would escape the catch-all at the
    # bottom, skip the health stamp, and freeze seneschald-health.json silently at its last 'ok' - the
    # exact silent-green failure the health feature exists to kill.
    try { Invoke-CredentialCheck -Log $log }
    catch { & $log "credentials: check ERRORED (non-fatal, taking no action) - $_" }

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
                    Request-HealthListenerRestart -Reason 'deferred: deps synced' -Log $log
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
        # And the health listener too — a SEPARATE, unconditional request every pull, since it has no
        # warm session to wait on and no other restart hook.
        Request-HealthListenerRestart -Reason "merge to $DeployBranch" -Log $log
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

# Dispatch — skipped when dot-sourced (see the seam near the top), so tests can load the functions
# without executing an action.
if (-not $script:SeneschaldDotSourced) {
    switch ($Action) {
        'Stop' { Stop-Seneschald }
        'Start' { Start-Seneschald }
        'Restart' { Restart-Seneschald }
        'Update' { Update-Seneschald }
    }
}
