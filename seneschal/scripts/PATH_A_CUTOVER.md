# Path A cutover — move the live daemon onto `main`

One-time host runbook for the "daemon runs off `main`, auto-pulls merges, runtime logs live in
gitignored `state/`" change (Path A). Do this **once**, on the host machine, **after the Path A PR is
merged to `main`**. After it, every future merge reloads the daemon automatically — no manual steps.

## Why a manual cutover is needed

Before Path A the daemon ran from this working tree on a feature branch, with `run-log.md` /
`carry-over.md` / `context-digest.md` **tracked** under `references/`. Path A deletes those tracked files
(their content moves to gitignored `state/`) and puts the daemon on `main`. A naive `git pull` would
**delete the live copies**, so we back them up and restore them into `state/` first.

## Prerequisites

- The Path A PR is **merged to `main`** on `origin`.
- Run from the daemon's repo, e.g. `%USERPROFILE%\workspace\repos\seneschal` (adjust if it lives
  elsewhere).
- If a filesystem-monitor tool (e.g. GitKraken's fsmonitor) hangs git in your repo, use
  `-c core.fsmonitor=false` on **every** git command, as the snippets below do.

## Steps (PowerShell)

```powershell
$repo = "$env:USERPROFILE\workspace\repos\seneschal"
Set-Location $repo

# 1. Stop the daemon — INLINE, not via seneschald-control.ps1. That script is added by Path A and does NOT
#    exist on the pre-cutover branch, so calling it here fails ("not recognized") and leaves the old
#    daemon running. Stop the task + kill the lock PID + clear the lock directly (works on any branch):
try { Stop-ScheduledTask -TaskName seneschald -ErrorAction Stop } catch { Write-Warning "Stop-ScheduledTask: $_" }
$lock = "$repo\seneschal\state\presence.lock"
if (Test-Path $lock) {
    $daemonPid = (Get-Content $lock -Raw | ConvertFrom-Json).pid
    if ($daemonPid) { try { Stop-Process -Id $daemonPid -Force -ErrorAction Stop } catch { Write-Warning "kill ${daemonPid}: $_" } }
    Remove-Item $lock -Force -ErrorAction SilentlyContinue
}

# 2. Preserve the live memory content into state/ (gitignored, so the pull won't touch it).
#    (Only copy files that still exist on the current pre-Path-A branch.)
foreach ($f in 'run-log','carry-over','context-digest') {
    $src = "$repo\seneschal\references\$f.md"
    if (Test-Path $src) { Copy-Item $src "$repo\seneschal\state\$f.md" -Force }
}

# 3. Move the checkout onto main. -f discards the now-deleted tracked references/*.md (already backed
#    up in step 2); state/*.md are gitignored so they survive.
#    Use `-B main origin/main`, NOT a bare `checkout main`: it works even when `main` does not yet exist
#    as a local branch, and it disambiguates when more than one remote carries a `main` (a bare checkout
#    of a not-yet-local branch dies with "matched multiple remote tracking branches" in that case).
git -c core.fsmonitor=false fetch origin
git -c core.fsmonitor=false checkout -f -B main origin/main
git -c core.fsmonitor=false pull --ff-only origin main

# 4. Point the local restart-seneschald.ps1 (and the `reseneschald` alias) at the version-controlled
#    control script, so a manual restart uses the new Stop/Start/Restart path:
@"
# reseneschald → delegate to the repo's version-controlled control script.
& "$repo\seneschal\scripts\seneschald-control.ps1" -Action Restart
"@ | Set-Content "$HOME\Documents\powershell\restart-seneschald.ps1" -Encoding UTF8

# 5. Register the merge-detector: seneschald-update polls origin/main every 10 min and, when it advances,
#    ff-pulls + asks the daemon to reload gracefully (unprivileged — no admin needed).
#    Launch via wscript.exe + run-seneschald-update-hidden.vbs (windowless) — NOT pwsh.exe directly, which
#    flashes a console window every 10 min and steals focus (disrupts typing / full-screen games).
$act = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument "`"$repo\seneschal\scripts\run-seneschald-update-hidden.vbs`""
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Days 3650)  # ~indefinite; [TimeSpan]::MaxValue serializes to P99999999D and Task Scheduler rejects it as out of range
Register-ScheduledTask -TaskName "seneschald-update" -Action $act -Trigger $trg `
    -Description "Pull main into the seneschald checkout when a PR merges, then gracefully reload the daemon." -Force

# 6. Point the Dream scheduled-task trigger at the (Path A) Dream step in SKILL.md instead of the old
#    "commit the references/ memory files" text, so it delegates rather than duplicating specifics:
$dream = "$HOME\.claude\scheduled-tasks\seneschal-dream\SKILL.md"
@"
---
name: seneschal-dream
description: Have the assistant consolidate the day and learn from it
---

Run the Dream consolidation (seneschal/SKILL.md): rebuild state/context-digest.md, refresh reminders, propose learnings.

Then follow the Dream PR step in seneschal/SKILL.md: open a PR **only if a tracked source file changed** —
built in a transient worktree off main — and **merge it on green** (`gh pr merge --merge` once every CI
check passes; red or pending CI holds the merge and gets surfaced to the owner). This commit + PR + merge
is an explicitly requested action for this task — perform it when the step calls for it. The gitignored
memory logs are not committed. Report the PR URL + merge outcome if one was opened.
"@ | Set-Content $dream -Encoding UTF8

# 7. Start the daemon again.
& "$repo\seneschal\scripts\seneschald-control.ps1" -Action Start
```

## Verify

```powershell
git -c core.fsmonitor=false -C $repo rev-parse --abbrev-ref HEAD   # → main
Get-ChildItem "$repo\seneschal\state\*.md"                         # run-log.md / carry-over.md / context-digest.md present
Get-ScheduledTask seneschald, seneschald-update                    # both Ready/Running
```

Then send the assistant a Telegram message — it should reply as usual, and acks should still persist to
the active store (e.g. Notion).

## How it behaves afterward

- **A PR merges to `main`** → within ~10 min `seneschald-update` ff-pulls it and enqueues a graceful
  `restart` control. The daemon applies it **once its warm chat session is idle** (1-min quiet window when
  a restart is pending), re-execs, and comes back on the merged code. No conversation is interrupted.
- **The updater self-heals and speaks up.** Every Update cycle stamps `state/seneschald-health.json`
  (`status` / `reason` / `consecutive_blocked` / `last_ok` — `last_ok` is the watch-the-watcher field).
  If a session parked the live checkout on a feature branch, the updater reclaims `main` automatically
  **only when** the parked branch has no commits `origin/main` lacks AND the session registry says no
  live session claims it (`sentinel.py --branch-claimed`, fail-closed — "can't tell" means "hands off").
  A block persisting past 3 cycles (~30 min) sends the owner a Telegram nudge, re-alerting at most every
  6 h; an unexpected crash still stamps a blocked heartbeat via the catch-all, so the health file can
  never freeze silently at `ok`.
- **Manual hard restart** (rare) → `reseneschald` / `seneschald-control.ps1 -Action Restart`.
- **Runtime memory** (`state/run-log.md`, `carry-over.md`, `context-digest.md`) is gitignored and never
  conflicts on a pull; the active store (Run Log + carry-over) stays the system of record. See
  `../references/memory.md`.

## Rollback

If something misbehaves: `Unregister-ScheduledTask seneschald-update -Confirm:$false`, `git -c
core.fsmonitor=false checkout <old feature branch>`, restore the local `restart-seneschald.ps1`, and
`seneschald-control.ps1 -Action Restart`. The state/*.md files are harmless to leave in place.
