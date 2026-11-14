# Service supervision — the same restart policy on Linux, macOS and Windows

**Status:** `PARTIAL(everything in §2-§6 is built and tested, and §7's CI matrix runs it on all three OSes; the live Windows probe ran on a host where admins elevate without a prompt, so the run from a non-elevated shell on a UAC-prompting host is still owed, §7.3)`

**Scope:** what restarts `presence.py` after a crash on each OS, how a graceful reload (every merge to
`main`) stays inside that supervision, the `seneschald` verbs, and the install shape `/setup daemon`
renders. It ports the supervisor the private assistant this framework was distilled from runs on
Windows, and extends `render_units.py` / `setup_doctor.py` rather than replacing them.

Code: `seneschal/scripts/seneschald_supervise.py` (the Windows supervisor), `single_instance.py` (the
kernel-held instance lock), `presence.py` (the supervised branch), `service.py` + the `seneschald` /
`seneschald.cmd` shims (the verbs), `render_units.py` (unit settings), `seneschald_revive.py` (rule 0),
`setup_doctor.py` (the supervisor in the daemon row), `service_probe.py` (the live host probe).

---

## 0. The answer, short

| | What supervises the daemon | Reload | Crash |
|---|---|---|---|
| **Linux** | a systemd **user** unit, linger on | in-place `os.execv`: same pid, systemd sees nothing | `Restart=on-failure`, `RestartSec=5`, give up past 5 starts in 10 min |
| **macOS** | a LaunchAgent in `gui/$UID` | in-place `os.execv`: same pid, launchd sees nothing | `KeepAlive = {SuccessfulExit: false, Crashed: true}`, 10 s throttle |
| **Windows** | `seneschald_supervise.py`, which **is** the `seneschald` task's process for life (S4U, no password) | the daemon exits **75**; the supervisor restarts it at once, outside its budget | backoff 2/10/30/60/120 s; more than 5 crashes in 15 min writes `seneschald-crashloop` and stays down |

Exit **0** means stay down everywhere. Exit **69** means another daemon holds the instance lock (the
Windows supervisor waits for it; unsupervised the daemon exits 0 and stays down, as before).

---

## 1. Why Windows needs its own supervisor

Two facts about Task Scheduler, both measured upstream and re-measured here (§7.2):

1. **It supervises only the first process of a task instance.** A graceful reload used to spawn a
   detached successor (`_respawn_detached`) and exit 0, so after the first reload of the day nothing
   supervised the daemon at all.
2. **Its restart-on-failure does not fire on a non-zero exit code.** It covers a failure to *launch*.
   Every "exit non-zero so the task restarts us" comment the daemon carried described a mechanism that
   does not exist; until this, a crashed daemon came back only through the ~10-minute watchdog.

`os.execv` cannot fix it on Windows: there it is spawn-and-exit, a new pid, which is the reason
`_respawn_detached` exists. So the reload has to stay inside a process that never exits: the supervisor.
Every Windows *service* host (NSSM, WinSW, shawl, pywin32, …) would have to log on as the owner with a
**stored password**; an S4U task needs none, and gives the same session-0, no-window result.

On POSIX none of this applies: `os.execv` keeps the pid, so systemd and launchd never see a reload,
and their own restart settings (§4) are the supervisor.

---

## 2. The reload contract

The Windows supervisor sets `SENESCHALD_SUPERVISED=1` in the child's environment. `presence.py` reads
and removes it at boot (`take_supervised_flag`) and changes behaviour **only** when it was set; it is
also scrubbed from every child the daemon starts (`child_env`), so a nested `presence.py` never thinks
it is supervised.

| `presence.py` exits | Meaning | Windows supervisor | systemd / launchd |
|---|---|---|---|
| **75** `EX_TEMPFAIL` | graceful reload, supervised on Windows only (`reload_mode`, `_reload_exit`) | restart **at once**, not counted; up to 30 reloads in 10 min, past which a 75 counts as a crash | never sent: POSIX reloads by `execv` |
| **0** | stay down: a `shutdown`, or the daemon's own crash-loop guard | exit 0 too; the task instance ends | not restarted (`on-failure` / `SuccessfulExit: false`) |
| **69** `EX_UNAVAILABLE` | another daemon holds `presence.instance.lock` (supervised only; unsupervised it is 0) | poll the lock every 5 s, then start | — |
| anything else | a crash: `return 1`, an unhandled exception, a kill | backoff 2, 10, 30, 60, then 120 s; reset after 10 min up; the 6th crash in 15 min writes `seneschald-crashloop` (`"by": "supervisor"`) and exits 0 | restarted after 5 s / 10 s throttle |

Before every **restart** (not the first start) the supervisor checks `seneschald-stopped`, so a Stop
that races a crash restart can never resurrect the daemon. The crash-loop sentinel is the same file
the daemon's own guard writes, so the watchdog has one give-up state and one alert path.

**The instance lock moved from a heartbeat to the kernel.** `presence.py`'s single-instance gate used to
read `presence.lock`'s heartbeat age. A heartbeat stays *fresh* for 90 s after a hard kill — which would
have made every crash restart wait out the stale window, or worse, exit 69 into an immediate retry —
and goes *stale* while a live daemon winds down. The gate is now `single_instance.InstanceLock` on
`presence.instance.lock`, taken at the top of `main()` before any side effect, held on an open handle
for the process's life and released by the OS when the process dies. `--instance-lock-wait-sec`
(default 60) covers the unsupervised reload's handoff, which releases immediately before it spawns or
re-execs. `presence.lock` is still written and beaten: it is the report the watchdog, the doctor and the
cockpit read.

**The supervisor's own upgrade.** On every exit 75 it compares the hash of its own source (and the
three modules it imports) with what is on disk. Changed: on Windows with `--task seneschald` (what the
rendered task passes) it starts a windowless one-shot helper that waits for it to exit and runs
`schtasks /Run /TN seneschald`, then exits 0; on POSIX it re-executes in place; on a Windows hand run
with no `--task` it logs once and keeps the old code.

---

## 3. The verbs

`seneschald <verb>` (`seneschal/scripts/seneschald` on POSIX, `seneschald.cmd` on Windows, both
`service.py`):

| Verb | systemd (user) | launchd (agent) | Windows (task + supervisor) |
|---|---|---|---|
| install | render + copy the units; `daemon-reload`; `enable --now`; linger (tried unprivileged, else the one `sudo loginctl enable-linger` is printed) | render + copy the plists; `bootstrap gui/$UID` (booted out first on a re-install) | render; run `register-tasks.ps1` through `ShellExecuteEx` `runas` — **the one UAC prompt** — or unelevated for `--user-level` or an already-elevated shell; `--print-only` prints the command instead |
| uninstall | `disable --now`; remove the units | `bootout`; remove the plists | `schtasks /Delete`; a refused delete is retried once, elevated |
| start | clear `seneschald-stopped`, `seneschald-crashloop`, `boot-attempts.json`; `reset-failed`; `start` | clear them; `kickstart`, or `bootstrap` if `stop` booted it out | clear them; `schtasks /Run` |
| stop | write `seneschald-stopped`; `systemctl --user stop` | write it; `bootout` (a KeepAlive job respawns if merely killed) | below |
| restart | the control queue (`request_control.py --action restart`): graceful, idle-gated (`--now` skips the gate); `--hard` = stop, then start ||||
| status | `systemctl --user show` | `launchctl print` | the supervisor's report + `schtasks /Query` |
| logs | `journalctl --user -u seneschald` + `presence.log` | `launchd-presence.log` + `presence.log` | `seneschal/state/logs/seneschald-child.log` + `presence.log` |

**The Windows Stop** (`WindowsBackend.stop`):

1. Write `seneschald-stopped` first, so a stop that dies half-way still reads as deliberate.
2. **Under a verified supervisor**, queue `shutdown --now` (reason `seneschald stop`) and wait up to
   30 s for the supervisor to exit: the daemon saves its state and ends its warm session the way a
   reload does, exits 0, and the supervisor stays down. `--now` skips only the chat-idle gate; an
   in-flight slot still holds it, and then the hard path runs.
3. `schtasks /End` — which kills the supervisor and, through its kill-on-close job, the daemon.
4. Whatever is left is killed **by verified pid only**: the supervisor when its kernel lock is held by
   the pid its report names, the daemon when `presence.instance.lock` is held by it. A recycled pid is
   never a target. Plain kills, never a tree kill: the cockpit and archon sites keep their lifetimes.
5. Withdraw the shutdown it queued (`request_control.py --withdraw`, matched by its exact reason), so
   the next start's daemon does not read a leftover shutdown and exit at once.

A refusal from Task Scheduler (§7.3) is `EXIT_DENIED` (3) with the exact elevated command — the verbs
never prompt on their own except `install`/`uninstall`, and never fail silently.
`seneschald-control.ps1 -Action Stop` writes the sentinel and hands over to `service.py stop`; its
legacy steps run only if python cannot.

---

## 4. The unit settings

**systemd** (`seneschald.service`, rendered): `Restart=on-failure`, `RestartSec=5` (it was 60: a minute
of nothing after every crash), `StartLimitIntervalSec=600` + `StartLimitBurst=5` in `[Unit]`, and the
default `KillMode=control-group`, which acts only on a stop or a crash because a reload is not an exit.
The start limit counts every start, manual ones included; `seneschald start` runs `reset-failed` first.
Exiting 75 under systemd would have been worse than useless: the control-group kill would take the
cockpit and the warm session down on every reload.

**launchd** (`com.seneschal.presence.plist`): `RunAtLoad`, and `KeepAlive = {SuccessfulExit: false,
Crashed: true}` — restart a non-zero exit or a crash signal, stay down on 0 — with the default 10 s
`ThrottleInterval`. It is an agent, not a daemon, because only an agent reaches the login keychain
where the `claude` CLI keeps its credentials. Whether an agent runs before any GUI login is not relied on.

**Windows** (`register-tasks.ps1`'s `seneschald` block):

```
Task Scheduler \seneschald   (S4U, owner, Limited; at startup + at logon; IgnoreNew; no time limit;
                              battery flags off)
  └─ .venv\Scripts\python.exe seneschal\scripts\seneschald_supervise.py --task seneschald
                              --args-file seneschal\state\setup\seneschald.args     ← for life
       └─ [Job: KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK]
            └─ .venv\Scripts\python.exe seneschal\scripts\presence.py <args>  (CREATE_NO_WINDOW)
```

- **The task runs the supervisor directly**, not through `run-presence.cmd`: `schtasks /End` kills only
  the task's own process, so a `cmd.exe` in front would orphan the supervisor.
- **One kill-on-close job, holding only the daemon's launcher.** Silent breakaway keeps everything the
  daemon spawns out of it, so the cockpit, archon sites, detached jobs and `claude` sessions keep the
  lifetimes they have across a reload, while the supervisor's own death takes the daemon with it.
- **The arguments live in `seneschald.args`** (tracked template beside the supervisor; `/setup daemon`
  renders a local copy with the owner's model dials), read at every child start, so a knob change
  deploys on the next graceful reload. Paths use a `{repo}` token the supervisor expands.
- `RestartCount 999` stays, honestly commented: it covers a failure to launch (a missing venv), nothing
  more. The env scrub (`ANTHROPIC_API_KEY` removed, UTF-8 forced, cwd the repo root) moved from the
  `.cmd` into the supervisor's tested Python; `presence.child_env` is still the second layer.
- The supervisor's own files: `state/seneschald-supervisor.instance.lock` (a second supervisor exits
  69), `state/seneschald-supervisor.json` (pid, heartbeat every 30 s, child pid, last exit, crash
  budget, restart counts, source hashes) and `state/logs/seneschald-child.log` (the child's stdout and
  stderr, 10 MiB with one rotation — it catches an import-time traceback the daemon's own log never sees).

`run-presence.cmd` keeps its default launch for a hand run; `run-presence.cmd supervise` runs the
supervisor instead. `run-presence.sh` is unchanged: on POSIX the unit is the supervisor.

---

## 5. Who reads what

- **The watchdog** (`seneschald_revive.py`, rule 0): while a live process holds
  `seneschald-supervisor.instance.lock`, stand down — the supervisor owns the restarts. Nothing
  supervises the supervisor, so when nobody holds it every other rule applies exactly as before and the
  `seneschald-update` revive is its backstop (a `schtasks /Run` that relaunches the supervisor).
- **The doctor** (`setup_doctor.check_daemon`): names a running supervisor and its crash restarts;
  says "supervisor not running" for a stale report; and turns a registered Windows task with a live
  daemon but no supervisor report YELLOW — an install from before this, which re-running
  `/setup daemon` (or `seneschald install`) moves onto the supervisor.
- **`seneschald status`**: the manager's view plus the supervisor's report and the instance-lock holder.

---

## 6. Windows without admin

`render_units.py --user-level` / `seneschald install --user-level` registers an **InteractiveToken**
task at logon whose action is the venv's **`pythonw.exe`** running the same supervisor. No admin, and
no window: `pythonw` has no console and the daemon child is started with `CREATE_NO_WINDOW` (measured,
§7.2). The honest cost: it runs only while the owner is logged on — no supervision before logon, none
after logoff — and a boot trigger is not available without admin.

---

## 7. Testing, per platform

### 7.1 CI

The `service-supervision` job runs the supervision suites on **ubuntu, macOS and Windows**
(`test_single_instance`, `test_seneschald_supervise`, `test_service`, `test_seneschald_revive`,
`test_render_units`, `test_control_queue`, `test_seneschald_control_stop`):

- every exit code through real child processes; a real child restarted on 75 without the supervisor's
  pid changing; backoff, budget and the crash-loop sentinel; lock contention (69); the reload storm;
  the stop sentinel; the env scrub; the child log; the supervisor's own lock, report and self-upgrade;
- **Windows**: the kill-on-close job kills its member and leaves its member's child alive; the
  control script's Stop hand-over (pwsh);
- **Linux / macOS**: `os.execv` keeps the pid (a real process re-executing itself) — the kernel half of
  the systemd/launchd reload claim; `systemd-analyze verify` on the rendered unit and `plutil -lint` on
  the rendered plists;
- **every OS**: all three backends' verbs through a fake runner, and the Windows Stop against a REAL
  hand-run supervisor and a real lock-holding daemon (`RealStop`), killed only by verified pid.

### 7.2 The live probe (`service_probe.py`)

Maintainer-run, never in CI: registers a throwaway task (`zz-seneschal-svc-probe-<mode>`, no
triggers) whose action is the real supervisor supervising a fake daemon that holds the real instance
lock, then starts, verifies, reloads, crashes and stops it, and always unregisters.

Measured on the development host (Windows 10 Pro 19045; UAC enabled but set to elevate administrators
without prompting; the probe ran from an elevated shell):

| | `--mode s4u` | `--mode user` (§6) |
|---|---|---|
| register | from the elevated token, no password | from the token, no elevation |
| daemon's session / console window | 0 / none | 1 (the owner's) / none; supervisor image `pythonw.exe` |
| reload (exit 75) | restarted in 0.27 s, same supervisor, task still Running | 0.52 s, same supervisor |
| crash (daemon killed) | restarted in 2.35 s (the 2 s backoff), same supervisor | 2.35 s |
| `seneschald`-style Stop | nothing left running | nothing left running (`/End` returned before the supervisor was gone; Stop now waits up to 5 s for it before its kill belt) |

### 7.3 The UAC-on case

A stranger's Windows host has UAC prompting on, and a non-elevated shell holds a **filtered** token.
What is built for it: `install` asks for elevation exactly once (`ShellExecuteEx runas`); every other
verb runs unelevated and reports a Task Scheduler refusal as `EXIT_DENIED` with the elevated command;
the watchdog's revive already fails loudly to alert-only when `Start-ScheduledTask` is refused.

What was measured: `service_probe.py --as-limited` repeats start, the daemon kill and `schtasks /End`
from a child holding a **SAFER normal-user token** (the Administrators group deny-only — the shape of a
filtered token). On the development host that token **was allowed** to `schtasks /Run` and
`schtasks /End` the owner's S4U task (and `/End` took the supervisor and the daemon down), and **was
refused** a `taskkill` of the session-0 daemon. That is why Stop leads with `schtasks /End` and treats
pid kills as the belt.

**What is still owed:** that token is an approximation, not UAC. The real check is
`python seneschal/scripts/service_probe.py --mode s4u` started from a **non-elevated** shell on a host
with UAC prompting enabled (one UAC prompt at register, then every step from the filtered token). Until
that run is recorded here, the UAC-on behaviour above is designed and approximated, not observed.

---

## 8. Where this departs from the design it ports

1. **No restart marker.** Upstream the reload re-stamps a "handing-off" marker for a watchdog rule
   that stands down mid-handoff. Seneschal's watchdog has no such rule, and under the supervisor rule 0
   already covers the gap; the unsupervised reload keeps its existing behaviour.
2. **One supervised service.** `SERVICES` has only `seneschald`; the health listener keeps its own
   `run-health-listener.cmd` task (moving it needs its own reload exit and instance lock first).
3. **The control script's Stop delegates to Python** instead of carrying its own supervised branch:
   this repo has no PowerShell test framework, so the decision lives where `unittest` proves it.
4. **The Update cycle does not record the supervisor's report** into `seneschald-health.json`; the
   doctor and `seneschald status` read it directly.
5. **The instance lock replaced the heartbeat gate** in the same change (upstream it landed first, on
   its own), because the 69 contract and a 2-second crash restart both need it.

---

## 9. Moving an existing install

A Windows install registered before this runs `run-presence.local.cmd` unsupervised; the doctor says so.
Re-running `/setup daemon` or `seneschald install` re-registers `seneschald` (`-Force`) onto the
supervisor with the same principal. Linux and macOS installs pick up the new unit settings the same way
(the re-render plus `daemon-reload` / a re-bootstrap). Nothing else needs to move: the state files, the
control queue and the watchdog are unchanged.

---

## 10. Known limits

- **The supervisor itself is supervised only by the watchdog** (rule 0's backstop, every ~10 min).
- **The task executes the venv's interpreter directly.** A missing venv is a launch failure — the one
  case Task Scheduler's restart-on-failure covers — and every Update's `uv sync` recreates it.
- **The no-admin fallback runs only while the owner is logged on** (§6).
- **The UAC-prompting run of the probe is owed** (§7.3).
