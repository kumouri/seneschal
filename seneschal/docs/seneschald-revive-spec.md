# seneschald auto-revive — making a crashed daemon come back on its own

**Status:** `BUILT` — the watchdog side (`seneschald_revive.py` + `seneschald-control.ps1`, §1-§7 and the
watchdog halves of §8/§9) and the daemon side (`presence.py`: the self crash-loop guard of §8 —
`record_boot_attempt` / `trip_crashloop_guard` / `maybe_clear_crashloop_on_sustained` — and §9's
boot-time `claude_identity` stamp in `presence.lock`). A lock with no stamp still reads `unstamped` and
takes no action, the designed fail-safe.
Implementation: `seneschal/scripts/seneschald_revive.py` (the decision predicate) +
`seneschald-control.ps1`'s `Invoke-PresenceRevive` / `Save-PresenceRevivalState` /
`Confirm-PresenceSustained` (the thin caller) + `Invoke-CredentialCheck` and its helpers (§9). Tests:
`test_seneschald_revive.py` (the predicate) and `test_seneschald_control.py` (the real script, end to
end, Windows + pwsh only).
**Scope:** `seneschald-update`'s presence check gains the ability to revive, not just alert — and, per
§9, the update cycle also notices that the owner logged into a **different Claude account** and asks
for the same graceful restart a merge does. A login has no deploy path otherwise.

> **Building it found a second, unrelated bug class.** A liveness check that did
> `[datetime]::Parse($lock.heartbeat, …)` after `ConvertFrom-Json` had already turned that string into
> a `Kind=Utc` DateTime would stringify it through the current culture and re-parse it as
> `Kind=Unspecified`, so `.ToUniversalTime()` shifted it into the future. A 30-minute-stale heartbeat
> computes a **negative** age and reads as ALIVE. Only the lock-*absent* path works in that shape — a
> crash removes the lock — while a daemon that *hung* holding its lock would never be detected, alerted
> on, or revived. Every timestamp read in `seneschald-control.ps1` now goes through
> `ConvertFrom-IsoUtc`, and every timestamp it writes is UTC with an explicit `Z`.

---

## 1. The problem

A daemon whose scheduler task crashes (an unhandled task exception — a Windows atomic-replace race is
the classic trigger) stays dead until a human restarts it. `seneschald-update` notices within 10 minutes
and can alert — it just has no way to do anything about it.

### 1a. Nothing supervises the daemon after its first graceful restart

`presence.py`'s `_supervise` exits **1** on a crashed task — "loud exit → the scheduled task's
restart-on-failure relaunches us". The `seneschald` task does carry `RestartCount=3 /
RestartInterval=PT1M`. But on a **graceful** restart — which happens on *every* merge to the deploy
branch — `main()` calls `_respawn_detached()` and returns **0**. The task's own process ends
*successfully*, and the live daemon is a detached orphan Task Scheduler can no longer see.

So `RestartCount` only ever protects the original process. After the first merge of any given day, the
"loud exit" contract is fiction: a crash exits 1 into nobody's hands.

An alert-only watchdog stops there because `-Action Start` touches the scheduled task + a PID and
self-elevates via UAC, which an unprivileged task can't do unattended. This spec's job is to clear that
specific blocker (§3.2).

### 1b. Detaching is deliberate and stays

`_respawn_detached` exists because an in-place restart of the process image can kill the daemon without
a successor. Detaching is the fix for that, not a bug to undo. See §6(a) for why un-detaching to regain
supervision is the wrong trade.

---

## 2. Where it hooks in

`Update-Seneschald`'s presence block in `seneschald-control.ps1`. It runs first and unconditionally,
ahead of every git early-return, so it sees a dead daemon regardless of the checkout's state. The
presence-down arm is the only place the script knows the daemon is dead.

Cadence is the `seneschald-update` task's ~10 minutes, so a revive retries at 10-minute granularity.

---

## 3. Constraints (all load-bearing)

### 3.1 Reuse the scheduled task; do not hand-roll the spawn

`run-presence.cmd` is not a thin wrapper. It **clears `ANTHROPIC_API_KEY`** to force subscription
billing, sets `PYTHONUTF8`/`PYTHONIOENCODING`, prefers the uv venv interpreter over system Python, and
carries every daemon flag (`--model`, `--idle-min`, `--peek-interval-min`, `--call-env`, `--log-file`,
the Watch model and prompt).

A hand-rolled detached spawn duplicates all of it. Getting the key-scrub wrong would silently flip the
daemon from **subscription** to **metered API billing** — a failure with a real invoice attached and no
obvious symptom. That risk alone settles the mechanism: **revive by starting the existing task.**

`MultipleInstances=IgnoreNew` on the task is the double-start backstop: if a daemon somehow is running,
a redundant start is a no-op rather than a second brain.

### 3.2 UAC — the task start is primary; a refusal degrades loudly

- A user starting their own limited-runlevel task does not normally need elevation, and the `-Action
  Start` self-elevation gate keys off the **top-level `$Action`**, which on this path is `Update`.
  Calling `Start-ScheduledTask` directly never trips it.
- **A UAC-enabled host (or a task registered under a different principal) may still refuse it.** When
  that happens the revive **fails loudly and degrades to alert-only** — never silently does nothing. A
  watchdog that quietly stops watching is worse than one that never existed.
- Any non-elevated fallback that spawns the daemon directly **must** solve the billing-safety
  duplication in §3.1 properly, in the framework rather than per deployment.
- **Validate the path on a UAC-enabled host** before relying on it there: a green result on a host with
  UAC off is silent about the case that matters.

### 3.3 Wrap the revive in its own try/catch

The presence block sits **outside** the git phase's `try`, and `$ErrorActionPreference = 'Stop'` is
global. A terminating error in revive code there would escape the catch-all, skip the health stamp, and
— because the VBS launcher is fire-and-forget, so the task result is always 0 — freeze
`seneschald-health.json` silently at its last `ok`.

That is precisely the silent-failure mode the health feature exists to kill. The revive call (and the
credential check, §9) each carry their own `try`/`catch`.

### 3.4 The deliberate-stop sentinel is a hard prerequisite

**Inference cannot work here**, because a deliberate stop and a crash converge on identical observable
state:

| | leaves the lock file |
|---|---|
| `Stop-Seneschald` | `Stop-Process -Force`, then explicitly `Remove-Item $LockFile` |
| crash | `main_async`'s `finally` calls `release_lock` |

`Test-PresenceAlive` returns `$false` identically for absent, stale, and malformed. Neither
`seneschald-health.json` nor `presence-health.json` nor `control-queue.json` records intent — the queue
entry is popped *before* the shutdown is applied, so nothing survives to read.

So intent is **written, not inferred**: `Stop-Seneschald` writes `state/seneschald-stopped` before it
does anything else; `Start-Seneschald` clears it; the daemon clears it on a healthy start as a self-heal,
so a stale sentinel can never suppress revival forever.

Without this, the revive fights `-Action Stop` on the very next 10-minute cycle. This is not an
enhancement to the feature; it is a precondition for it being safe to build at all.

### 3.5 Persist the crash-loop counter

`archon_sites_task`'s respawn budget is the right shape to mirror:

- count **revival attempts that did not yield a healthy service**, not crashes
- do **not** burn budget when the launch itself fails
- reset on observed health
- log the giving-up line **once**, not every pass

One thing must differ. Its counter is **in-memory** — fine for a resident daemon, useless for a watchdog
that is a fresh PowerShell process every 10 minutes. The counter persists in `presence-health.json`.

**Trap:** `Set-PresenceHealth` rebuilds the record from scratch each cycle. Any field without explicit
prev-carry logic — the shape `last_alert` uses — is **silently dropped every 10 minutes**, so a
naively-added counter would sit at 1 forever and the give-up gate would never fire.

Adding fields is otherwise safe: this file has **no consumer anywhere outside the script** (the
cockpit's `readers.py` reads `seneschald-health.json`, not this one).

Fields: `revivals`, `revival_window_start`, `revival_gave_up`. A fourth, `last_revival`, was considered
and dropped: nothing reads it — every decision comes from the counter, the window and the latch — and
each extra field is one more that must be threaded through the prev-carry logic or be silently dropped
every cycle. Revival times are already in `seneschald-update.log`.

---

## 4. Behaviour

**Revive at `consecutive_down = 1`** — i.e. as soon as the daemon is confirmed dead, ≤ ~10 minutes. That
sits *ahead* of `$PresenceAlertAfterCycles = 2`, so the hard-down alert only fires when revival failed,
gave up, or was suppressed. Fixing it before the owner is told is the point.

**Budget:** 3 revivals per rolling 60 minutes (`MAX_REVIVALS` / `REVIVAL_WINDOW_MIN`), then give up and
stay given-up until the daemon is observed healthy or a human intervenes.

**Verify inline that the revive TOOK — sustained liveness, not a twitch.** After starting the task, wait
for the daemon to boot, then require it to **stay up a full settle window (`$ReviveSettleSec = 90s`)
before counting the revive a success.** A revive that reports success it didn't achieve is the same class
of bug as the negative-age alert — and it is exactly what turns a crash-loop into a many-hour silent
outage: the daemon boots, writes **one** heartbeat, and dies within a second; a check that returns
success on that first heartbeat calls `--record-healthy`, which **resets the revival budget every
10-minute cycle**, so the give-up latch never fires and the loud "still dying" alert (gated on
`revivals ≥ 2`) never triggers.

The honest signal is not "is there a fresh heartbeat" but "has it been *ticking* for ≥ the settle
window." Because the stale threshold (`$PresenceStaleSec = 180s`) is longer than the settle window, a
heartbeat frozen at boot+1s still reads "fresh" 90s later — so `now - started_at` would wrongly pass. The
check uses the **observed uptime** (`heartbeat - started_at`, how long it actually kept ticking)
instead: a daemon that died a second after booting reports ~1s of uptime no matter how long we wait.
This judgement lives in `seneschald_revive.py --confirm-sustained` (pure, CI-tested —
`confirm_sustained()`); `Confirm-PresenceSustained` is a thin fail-closed caller, gating **both** the
post-revive success check and the start-of-cycle counter reset. A daemon that comes up but can't survive
the window counts as a **failed** revive, so the budget burns down to the give-up latch and the owner is
alerted, instead of a silent loop.

**Suppressed** whenever `state/seneschald-stopped` exists — logged plainly (`revive: SKIPPED —
deliberately stopped`) so the log never looks like a watchdog asleep at the wheel.

**Notification policy:** a clean one-off self-heal is **logged, not sent**. Telegram fires on a second
revival inside the window, on a failed revival, or on give-up. Quiet when it's working, loud when it
isn't. The not-rate-limited-on-send-failure discipline applies unchanged: `last_alert` is stamped only
on a send that actually landed.

---

## 5. Testability

**The decision goes in Python; the `.ps1` stays a thin caller.** Putting process-restart logic — which
must never fight a deliberate stop, and which manages a spend-relevant budget — into PowerShell alone
would leave it where the unit suite cannot reach it. `seneschald_revive.py` answers *should I revive, and
if not why not* from (sentinels, counter, window), and `seneschald-control.ps1` parses the verdict and
acts on it.

This mirrors an existing precedent exactly: `seneschald-control.ps1` already consults
`sentinel.py --branch-claimed` for a decision it refuses to make itself, reading the last line of stdout
and failing **closed** when the answer is unclear. Same shape, same fail-closed default — an unclear
verdict means *don't revive*, and an unreadable health file means *write nothing* (a transient read
race must never hand a crash-looping daemon a fresh budget).

The PowerShell side is covered end to end by `test_seneschald_control.py`, which runs the real script
against throwaway repos (Windows + a responsive pwsh; it skips elsewhere). Every throwaway state dir
carries the deliberate-stop sentinel, so no test can start the host's real scheduled task.

---

## 6. Alternatives considered

**(a) Don't detach on restart, so Task Scheduler keeps supervision.** Rejected. `RestartCount` is a
*failure* budget; routine restarts (every merge) would consume it, so the three lives would be spent on
healthy deploys and unavailable for an actual crash. It also reopens the failure `_respawn_detached` was
written to fix.

**(b) A real service wrapper (NSSM, WinSW, or a native service).** Genuine supervision with restart
policy, and the honest long-term answer — especially for a framework shipping to arbitrary hosts, where
"depend on Windows Task Scheduler" is a portability constraint anyway. Not chosen *for now*: a new
external dependency and a rebuild of the launch path, for a problem an existing, already-scheduled
component can cover. Worth revisiting if the watchdog proves insufficient. (On Linux and macOS the
`/setup daemon` chapter's systemd / launchd units already supervise with `Restart=`/`KeepAlive`.)

**(c) Battery gating.** A task carrying `DisallowStartIfOnBatteries = True` and `StopIfGoingOnBatteries
= True` would silently defeat a revive on battery power. Inert on a desktop with no battery; on a laptop
host, flip both off.

---

## 7. Open questions

1. **Give-up reset.** After the budget is exhausted, what un-sticks it besides a human? As built: an
   observed-*sustained*-healthy daemon clears it, so a genuine recovery self-resets — but a daemon that
   is crashing every 20 minutes stays given-up and keeps alerting rather than flapping forever.
2. **Interaction with `pending-restart`.** If a dep-sync failure has deferred a restart and the daemon
   *also* dies, is reviving onto a known-stale venv right? Probably yes — a running daemon on old deps
   beats no daemon — but it should be a deliberate decision, logged as such, not an accident of
   ordering.
3. **Watchdog or supervisor?** §6(b) may be the better answer for some hosts; re-open the question
   rather than inherit the answer.

---

## 8. The daemon's OWN crash-loop guard

**Watchdog side BUILT** (`seneschald_revive.decide`'s `crash-looping` rule and `Start-Seneschald`'s
clean-slate clear); **daemon side lands with the daemon-core port** (`presence.py`'s "self crash-loop
guard": `record_boot_attempt` / `is_crashloop` / `trip_crashloop_guard` / `clear_crashloop_state` /
`maybe_clear_crashloop_on_sustained`, the startup check in `main()`, the pre-spawn check in
`_respawn_detached`, and the self-heal in `scheduler_task`). Tests: `CrashloopSentinelUnit` in
`test_seneschald_revive.py`.

**The gap this closes.** Everything above is the *watchdog* — a separate PowerShell process that samples
every ~10 min. Even with the settle-window fix, that leaves two things the watchdog alone can't cover:

1. **The daemon has no self-awareness of its own crash loop.** When it keeps booting into the same crash
   (a corrupt state cache is the classic trigger), each relaunch (Task Scheduler `RestartOnFailure` at
   ~1-min intervals, or the watchdog itself) just burns straight back into it. The daemon should notice
   *"I keep dying"* and stop, loudly, rather than thrash.
2. **`_respawn_detached` respawns with no loop guard.** The graceful-reload path (every merge) spawns a
   detached successor and exits. Nothing there asks whether we've been respawning in a tight loop.

**Mechanism.** Every real boot funnels through `write_lock`, which stamps `state/boot-attempts.json`
(pruned to a rolling `CRASHLOOP_WINDOW_MIN` = 5 min window) — a *single* recording point, so one count
catches every relaunch shape (Task-Scheduler-restart loop, watchdog-revive loop, graceful-respawn loop)
without double-counting any. If the window holds more than `CRASHLOOP_MAX_BOOTS` = 3 boots, the guard:

- writes `state/seneschald-crashloop` (the give-up sentinel),
- pushes **one** loud Telegram alert (deduped on the sentinel's existence — a repeat boot that re-trips
  the same episode must not re-spam), and
- **exits `0` without respawning** — exit `0`, not `1`, deliberately: a loud `exit(1)` would invite Task
  Scheduler's `RestartOnFailure` to relaunch us straight back into the crash.

The check runs in **two places**: the **primary** is `main()` right after `write_lock` (every boot
funnels through here, so it breaks all three loop shapes); the **secondary** is `_respawn_detached`,
which *projects* the window (prune + the imminent boot) before spawning and refuses if that would exceed
the budget. Both are **fail-open**: a bug in the guard must never take a healthy daemon down, so any
error just proceeds to run/respawn. And `boot-attempts.json` is read defensively to the bone — a garbage
file reads as "no prior boots", never an exception, because *a corrupt state cache crashing the daemon
is the exact failure this guard exists to prevent*, and it must never become the source of one.

**How the two mechanisms cooperate (don't fight).** `seneschald_revive.decide`'s **Rule 1b** treats the
`seneschald-crashloop` sentinel exactly like `seneschald-stopped` — it wins over the revival budget and
the watchdog returns `crash-looping` (no revive, revival bookkeeping untouched). So the daemon gives up
loudly and the watchdog stands down, rather than reviving it straight back into the loop the daemon just
deliberately broke. **Division of labour by timescale:** this guard catches **fast** loops
(sub-settle-window deaths, >3 boots in 5 min); the watchdog's revival budget (§4, 3 per 60 min) still
covers **slow** ones (survives the 90 s settle window but dies within the hour). No overlap.

**Lifting it.** The sentinel + boot window are cleared two ways, both mapping to "the past attempts are
forgiven": a **sustained-healthy run** self-heals it (`maybe_clear_crashloop_on_sustained`, once uptime
≥ `CRASHLOOP_SETTLE_SEC` = 90 s — mirrors the watchdog's `Confirm-PresenceSustained` bar), and a
**deliberate `Start-Seneschald`** clears both up front (a human intervening is the clean-slate signal —
and this is *load-bearing*: without resetting the boot window, the guard would instantly re-trip on the
very boots that caused the give-up and a human trying to recover couldn't). The watchdog's
`Invoke-PresenceRevive` uses `Start-ScheduledTask` directly, *not* `Start-Seneschald`, so a watchdog
revive correctly does **not** reset the window — only a human does.

---

## 9. A LOGIN is a deploy with no deploy path

**Watchdog side BUILT** (`seneschald-control.ps1`'s `Invoke-CredentialCheck` / `Get-ClaudeIdentity` /
`Get-DaemonAuthStamp` / `Save-CredentialState` / `Send-CredentialAlert`; the wiring end to end in
`test_seneschald_control.py`). **Daemon side lands with the daemon-core port** (`presence.py`'s
`read_claude_identity` / `claude_config_path` + the stamp in `write_lock`, restored by `beat_lock`).

**The gap.** Everything above §8 is about the daemon dying. This is about the daemon living, correctly,
on the wrong credentials. A **code** merge has a deploy path — `seneschald-update` fetches, pulls,
`uv sync`s and enqueues the graceful restart. A **credential** change has none. Nothing watches the
Claude CLI's auth, and the warm `claude` session reads it once, at spawn. So `claude /login` does nothing
until something *else* happens to bounce the daemon.

**The failure this prevents is the nasty kind.** A swap that seems to work because an unrelated merge
restarted the daemon minutes later is coincidence, not mechanism. Log in on a quiet evening with nothing
merging and the daemon runs the OLD token indefinitely. The only symptom is *"why am I still hitting the
old account's limit"* — which reads like a **billing** problem, not a stale process, so the search
starts in entirely the wrong place.

### 9.1 The predicate: account identity, NOT the credential file's mtime

The obvious trigger is the mtime of `~/.claude/.credentials.json`, and it is **wrong**. That file is
rewritten on **every routine OAuth token refresh**, with nobody logging in. An mtime trigger would bounce
the daemon on a timer, forever.

The trigger is `~/.claude.json`'s **`oauthAccount`**. A change of identity is a real event; a token
refresh is not.

**The key is `(accountUuid, organizationUuid)` and nothing else.** The other fields were each considered
and each rejected, and the exclusions are as load-bearing as the inclusions:

| field | in the key? | why |
|---|---|---|
| `accountUuid` | **yes** | a different account is a different person, a different subscription, a different limit |
| `organizationUuid` | **yes** | a different workspace to bill and rate-limit against |
| `emailAddress` | no — carried, never compared | derived from the account and independently renameable. Recorded so the log line and the nudge can say *which* account in words |
| `organizationType` | no | a plan upgrade moves it while the credential is untouched. **A plan change is not a login** |
| `organizationRateLimitTier` | no | worse: it can be re-bucketed server-side, so it would restart the daemon over somebody else's metadata |

`accountCreatedAt`, `profileFetchedAt`, `displayName` and the rest are not read. **No token, access key
or refresh token is read, copied or stamped anywhere** — `~/.claude/.credentials.json` is never opened.
The daemon-side reader's test must assert its returned dict is exactly the three stamped keys, so
widening it fails CI rather than leaking quietly.

### 9.2 Where the stamp lives, and why it is NOT seneschald-health.json

The daemon stamps the identity it booted with into **`presence.lock`** (`write_lock`), beside `pid`,
`started_at` and `heartbeat`, as `claude_identity: {account_uuid, organization_uuid, email}`.

The natural-seeming home was `seneschald-health.json`, since Update already stamps it every cycle. It is
the wrong one: **two writers on a rebuilt-from-scratch record.** `Set-SeneschaldHealth` reads `$prev`,
rebuilds, and writes — an Update cycle takes seconds to minutes (git, `uv sync`, and on the revive path
a 90 s settle sleep). A daemon booting inside that window would write its stamp into a record Update
then overwrote from a `$prev` read before it. The stamp would vanish, the guard would read *"nothing to
compare"*, and it would look present while doing nothing — §3.5's trap, one layer up.

The lock avoids it by construction and earns two properties for free:

* **Single writer.** `presence.py` writes it; the watchdog only reads. There is no read-modify-write
  race to lose.
* **It dies with the process.** A stale identity claim cannot outlive the daemon that made it. When the
  daemon is dead there is nothing to compare and nothing to do — which is *correct*, because the revive
  path (§4) starts a fresh process that reads the current credentials anyway.

`seneschald-health.json` still records the **outcome** of each check (§9.4), which is what Update owns.

**One trap on the lock side.** `beat_lock` rebuilds the record from `load_json(..., {})`, which falls
back to `{}` on an unreadable read (a real Windows collision — see `sentinel.save_json`). A heartbeat
that rebuilt the lock without the identity would silently un-stamp a *running* daemon. So the boot
identity is held in a module-level global and restored on every beat. It is deliberately **not
re-read** per beat: the stamp must keep meaning *what the daemon started with*, and re-reading would
quietly turn it into *what is on disk right now* — the guard would answer its own question and could
never fire.

`claude_config_path()` (daemon) and `$ClaudeConfigFile` (watchdog) must resolve the file the same way:
`$CLAUDE_CONFIG_DIR/.claude.json` when that exists, else `~/.claude.json`. The guard compares what one
wrote against what the other read, so two different files would make the comparison nonsense rather than
merely wrong.

### 9.3 Behaviour

`Invoke-CredentialCheck` runs in `Update-Seneschald` immediately after the presence check and **ahead
of every git early-return**, for the same reason the presence check does: whether the owner logged into
a different account has nothing to do with the state of the checkout, so a blocked pull must not
swallow it. It carries **its own `try`/`catch`** — §3.3 applies verbatim.

On a real change it enqueues **the same graceful restart a merge enqueues** (`request_control.py
--action restart`, `defer_until_idle`). Not a second mechanism, not a hard kill, not an interrupted
conversation.

**Fail safe means do nothing.** An unreadable file must never be read as *the account changed*; each
no-action outcome is recorded rather than passed over in silence:

| `credential_state` | when | action |
|---|---|---|
| `ok` | disk and daemon agree | none; clears any pending guard |
| `unreadable` | `~/.claude.json` missing, unreadable, no `oauthAccount`, or no `accountUuid` | **none** |
| `unstamped` | no lock, malformed lock, or a daemon that booted without the stamp | **none** |
| `restart-requested` | first sighting of a mismatch | enqueue one graceful restart |
| `restart-pending` | mismatch, already asked, daemon has not restarted yet | wait; quiet for ~1 h |
| `restart-ineffective` | mismatch, already asked, daemon **has** restarted since | **alert; never ask again** |
| `enqueue-failed` | `request_control.py` exited non-zero | record nothing, retry next cycle |

### 9.4 The loop guard, and why "has it restarted" is not a cycle count

`credential_restart_for` holds the identity key a restart was enqueued for; at most one restart is ever
enqueued per identity. A *different* identity is a different event and gets its own attempt, so logging
into a third account while one is stuck still works.

Distinguishing *the restart has not applied yet* from *the restart applied and did not help* matters,
because a graceful restart legitimately waits for the warm session to go idle — a mismatch surviving
the next cycle is **normal**, and alerting on a cycle count alone would nag mid-conversation. So the
guard compares the lock's `started_at` against the value it recorded when it asked
(`credential_restart_at`): if the daemon has not restarted, it is simply still queued; if it *has*
restarted and the mismatch survived, the swap did not take and no further restart will fix it.

That second case alerts **immediately** — it will never fix itself. The first stays quiet until
`$CredentialStuckAlertCycles` (6, ≈ 1 h). Both throttle at `$CredentialReAlertHours` (6 h), matching
the deploy alert.

Two disciplines this file already treats as load-bearing are reused verbatim, both because **a non-zero
`python` exit does not throw in PowerShell**:

* `credential_last_alert` is stamped only on a send that actually **landed** — otherwise a silent
  failure would rate-limit away the only warning the outage gets.
* `credential_restart_for` is recorded only on an enqueue that actually **succeeded** — booking a
  request we never made would latch the loop guard shut over a restart that never happened.

`Send-CredentialAlert` needs its **own** counter and throttle rather than riding `consecutive_blocked`:
the git phase re-stamps `status = ok` later in the same cycle, which would reset a shared counter to 0
every pass and the alert would never reach its threshold. That makes it the third alerter in the file,
alongside the deploy and daemon-alive ones, carrying their whole discipline.

### 9.5 Known limits, stated rather than discovered

* **The stamp is boot-time, and the warm session can respawn later.** If a respawned warm session picks
  up new credentials on its own, the boot stamp is pessimistic and the guard asks for one restart that
  was not strictly needed. The cost is bounded at exactly one graceful restart — the restart re-stamps,
  and the mismatch clears.
* **Second-precision comparison.** `started_at` is truncated to seconds, so two boots inside the same
  second read as one. Vanishingly unlikely, and it fails toward *not restarted yet* (wait) rather than
  toward a loop.
* **The loop guard is only as durable as its write.** If `Save-CredentialState`'s write to
  `seneschald-health.json` fails, the next cycle has no memory of the request. That is the same trust
  this script already places in that file for `last_alert` and `consecutive_blocked`; a persistently
  unwritable state dir breaks far more than this.
* **Path resolution is duplicated in two languages** (§9.2). They must stay identical; tests pin both
  halves.
