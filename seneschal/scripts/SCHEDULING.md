# Scheduling — local-first (no cloud queue)

How the assistant wakes itself, all on the owner's machine. The scheduled tasks **run the orchestrator
directly**, and a resident **`presence.py` daemon** drives the real-time proactive loop (warm Telegram
chat + reminders + comms-peek). No hosted queue, no cloud service in the loop.

> **Prereqs for unattended runs:** the Notion + Calendar (+ Gmail/Slack) MCP servers connected, **Proton
> Mail Bridge running** (for email), `telegram.env` filled in (`TELEGRAM_SETUP.md`), and a **subscription
> auth token** for the daemon (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`; see below). Pre-approve
> tools once per task (below) so headless runs don't stall on a permission prompt. Slot times are the
> **machine-local wall clock** — set them for the owner's configured timezone.

## The two kinds of schedule

### 1. Heavyweight runs — owned by the presence daemon

**The daemon runs these itself.** `presence.py` has a built-in slot scheduler (`SLOTS` +
`maybe_run_slots`) that fires the Brief / Wrap / Dream / Daily-Journal runs **and** the four reminder
slots on their local times, spawning a fresh headless `claude -p` per run (same mechanism as the
comms-peek). No separate Task Scheduler entries are needed — one always-on `\seneschald` owns the whole
cadence. Times live in `SLOTS` in `presence.py`; each fires at most once per local day, and a slot missed
while the machine was asleep fires late on the next loop **if** still within `--slot-catchup-min` (default
180 min), else it's skipped for the day (so a 06:30 brief never fires at 11 pm). Disable with `--no-slots`;
pick a model with `--slot-model`. The daily fired-state is `state/slots.json`.

> **Needs Notion.** These headless runs (especially the reminder slots, which read the ⏰ DB) require the
> daemon to have Notion access — see `NOTION_MCP_SETUP.md`. Without it, slot runs launch but can't reach
> Notion, so DB-driven reminders won't enqueue.

The slot times + prompts (edit in `presence.py`):

| Slot (SLOTS name) | When (local) | What it runs |
|-------------------|--------------|--------------|
| `daily-journal` | 05:00 | Daily Journal steward |
| `morning-brief` | 06:30 | Brief (chat + Telegram + Proton email + Run Log) |
| `reminders-morning` | 08:00 | Reminders slot (daily reset + enqueue) |
| `reminders-midday` | 12:30 | Reminders slot |
| `reminders-evening` | 18:30 | Reminders slot |
| `eod-wrap` | 21:07 | Wrap |
| `reminders-bedtime` | 21:30 | Reminders slot |
| `dream` | 22:00 | Dream consolidation |

**Standing reminder rolls.** Alongside the four reminder slots, the daemon also refills any **standing
every-N-hours roll** (e.g. an every-2h *check messages from Alex* poll, 9am–11pm) **once per local
day**, straight from its loop via `reminders_roll.py` — pure local queue math (no Notion, no `claude`
spawn), future-only and idempotent, guarded by `state/rolls.json`. Rolls are configured in
`reminders_roll.py` (`ROLLS`); behavior is in `../references/reminders-policy.md`. This is what makes a
custom intraday cadence hands-off instead of hand-enqueued each day.

**Legacy (optional).** You *can* still run any of these as a standalone Claude Code **Desktop Scheduled
Task** instead (local, fresh session, local file + MCP access, one catch-up run on wake) whose prompt
invokes the orchestrator in one mode — but with the daemon owning them, that's redundant:

| Task | When (local) | Prompt |
|------|--------------|--------|
| `seneschal-morning-brief` | ~6:30 AM daily | "Run the morning **Brief** (`seneschal/SKILL.md`). Deliver in chat + push highlights to Telegram + email via Proton + write the Run Log." |
| `seneschal-eod-wrap` | ~9:07 PM daily | "Run the **Wrap** (`seneschal/SKILL.md`)." |
| `seneschal-dream` | nightly, after Wrap (e.g. ~9:30 PM) | "Run the **Dream** consolidation (`seneschal/SKILL.md`): rebuild `state/context-digest.md` and refresh reminders." |
| `seneschal-daily-journal` | 5:00 AM daily | "Run the **Daily Journal** steward (`seneschal/SKILL.md`)." |

### 2. The presence daemon — always-on service

`presence.py` runs resident (assuming an always-on host) and is event-driven, so it's ~free while idle.
It owns Telegram chat (a warm `claude` session), fires reminders, and runs the comms-peek.

**Don't put the python command in the Task Scheduler "Arguments" field** — its quoting/line-continuation
rules will mangle a multi-flag command (you'll get a Python `exit code 2` = argparse rejected the args).
Instead, point the task at the wrapper script **`run-presence.cmd`**, which holds the knobs in a normal
file and sets the billing-safe environment:

```
Program/script:  %USERPROFILE%\workspace\repos\seneschal\seneschal\scripts\run-presence.cmd
(Arguments:      — leave empty —)
Start in:        %USERPROFILE%\workspace\repos\seneschal
Trigger:         At log on
Settings:        "If the task is already running … Do not start a new instance"
                 "If the task fails, restart every 1 minute"
```

(The daemon also self-guards with a single-instance lock, so an accidental second launch just exits.)
Edit the model / idle / peek knobs inside `run-presence.cmd`. To run it by hand for a smoke test, just
double-click it or run `seneschal\scripts\run-presence.cmd` in a terminal.

**Subscription billing (important).** The warm session is the `claude` **CLI**, which bills against the
Claude subscription — *not* the metered API. One-time:

1. `claude setup-token` (interactive; needs Pro/Max) → prints a long-lived token.
2. `setx CLAUDE_CODE_OAUTH_TOKEN <token>` so the service picks it up from your user environment.
3. **Make sure `ANTHROPIC_API_KEY` is not a persistent env var.** `run-presence.cmd` clears it for the
   daemon and `presence.py` scrubs it from the child `claude` env too — but if it's set globally, a
   *manual* `claude` run would still bill the API, so consider removing it (`setx ANTHROPIC_API_KEY ""`).

`--permission-mode bypassPermissions` (the default) keeps headless turns from blocking — the assistant's
own act-low/ask-high gate is the real safety. The peek uses `--watch-prompt` (a plain prompt, no shell
quoting; `--watch-model` picks a cheap model); set `--peek-interval-min 0` to disable it. `sentinel.py`
is no longer scheduled — it stays a manual one-shot / fallback for firing reminders without the daemon.

### 3. The health + presence feed listener — always-on service

`health_listener.py` receives the phone's **health** feed (`/health-ingest`) **and** its **presence** feed
(`/presence-ingest`) over the LAN/tailnet and imports them into `state/health.db` / `state/presence.db`. It
must be up whenever the phone might POST, so it runs as its **own** always-on scheduled task —
**independent of the presence daemon**, so a `seneschald` reload never drops the listener (and vice-versa).

Point a Task Scheduler entry at the wrapper **`run-health-listener.cmd`** (same shape as
`run-presence.cmd`), or register it in one shot. **Run this in an elevated PowerShell** (task creation
needs admin), and give it an **`S4U` principal** (`LogonType S4U` = "run whether logged on or not," no
stored password) so it runs in **session 0 with no console window**, exactly like the `\seneschald` task.
Without the S4U principal it registers as `InteractiveToken` and pops a persistent console window at logon:

```powershell
$a = New-ScheduledTaskAction -Execute "$env:USERPROFILE\workspace\repos\seneschal\seneschal\scripts\run-health-listener.cmd"
$t = New-ScheduledTaskTrigger -AtLogOn
$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 `
       -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName "seneschal-health-listener" -Action $a -Trigger $t -Principal $p -Settings $s -Force
```

| Setting | Value |
|---------|-------|
| Program/script | `…\seneschal\scripts\run-health-listener.cmd` |
| Trigger | At log on |
| Run as | Whether logged on or not — **`S4U`** (session 0, **no window**, like `\seneschald`) |
| If already running | Do not start a new instance (`IgnoreNew`) |
| On failure | Restart every 1 minute |
| Time limit | none (it's a long-running server) |

**Token (one-time).** The wrapper binds all interfaces (`--host 0.0.0.0`) and therefore **requires** a
bearer token, read from the `HEALTH_INGEST_TOKEN` user env var. Set it to the SAME value configured in the
companion phone app that POSTs the feeds:

```
setx HEALTH_INGEST_TOKEN <token>
```

The wrapper loops (restarts the listener on exit) and the task restarts it on failure — belt and
suspenders. Smoke-test by double-clicking `run-health-listener.cmd`; `GET http://<desktop>:8765/health`
returns a health check.

> **IP note.** The phone POSTs to the desktop IP configured in the app (a LAN `192.168.x`, or a Tailscale
> `100.x` once the phone is on the tailnet). If that desktop IP changes, update the app's config and
> reinstall. A DHCP reservation (or Tailscale on the phone) keeps it stable.

### 4. Archon scheduled loops — an optional pattern (tripwire + digest)

When an Archon (see `../references/archons.md`) owns a recurring watch-style job, keep the standing
cadence **LLM-free**: schedule the Archon's own stdlib tools (no `claude` spawn, no subscription spend)
and reserve full delegations for on-demand work through the assistant's gate. The shape that works well is
a pair of tasks:

- an **hourly tripwire** — fetch + score a watchlist, diff against a rolling seen-ledger, and push only
  genuinely **new** high-scoring items (capped per cycle, quiet-window aware); everything else waits
- a **daily digest** — roll the day's cycles into one Markdown summary (new items, near-misses, flagged
  items with reasons, gone-since-yesterday) delivered by email/Telegram

Register each the same way as the health listener (S4U principal, elevated PowerShell), pointing at a
small wrapper `.cmd` for the archon's tool, e.g.:

```powershell
$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
       -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$a1 = New-ScheduledTaskAction -Execute "<repo>\archons\<id>\tools\run-tripwire.cmd"
$t1 = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date.AddMinutes(5)) `
        -RepetitionInterval (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "seneschal-<archon>-tripwire" -Action $a1 -Trigger $t1 -Principal $p -Settings $s -Force
```

Useful conventions: keep thresholds/caps as flags on the tool (edit the wrapper); support a pause
sentinel file (create `paused` in the tool's output dir to skip cycles without unscheduling); and run the
first-ever cycle with a `--no-notify` flag so it seeds the seen-ledger without re-alerting already-known
items. The loop is the tripwire, not the writer — anything that drafts for the outside world stays an
on-demand, gated delegation.

### 5. Session registry hooks — machine-wide (manual setup)

The **session registry** (`state/sessions/` — see `../references/reminders-policy.md` → "Live-session
defer") learns about *every* Claude Code session on the box through a machine-wide hook:
`session_stamp.py` writes/refreshes an awareness-only `build` entry on session events, and on
**SessionEnd** it also fire-and-forgets the `mini_dream.py` distiller (→
`state/session-distillations.jsonl`). The daemon and a desktop `/assistant` session register
themselves separately (`sentinel.write_session_heartbeat` / `session_heartbeat.py`) — the hook covers
everything else.

Wire it in **your user-level `~/.claude/settings.json`** — *not* this repo's `.claude/settings.json*`
(a repo-shipped hook would impose it on every install and double-fire beside the user copy; personal
hook config never ships). All **four events** point at the same script, absolute-pathed into the live
checkout so any project's session lands entries in the shared state dir (replace `$REPO` with your
checkout path, e.g. `C:/Users/you/workspace/seneschal`):

```json
{
  "env": { "PYTHONUTF8": "1" },
  "hooks": {
    "SessionStart":     [ { "hooks": [ { "type": "command", "command": "python $REPO/seneschal/scripts/session_stamp.py", "timeout": 10 } ] } ],
    "UserPromptSubmit": [ { "hooks": [ { "type": "command", "command": "python $REPO/seneschal/scripts/session_stamp.py", "timeout": 10 } ] } ],
    "Stop":             [ { "hooks": [ { "type": "command", "command": "python $REPO/seneschal/scripts/session_stamp.py", "timeout": 10 } ] } ],
    "SessionEnd":       [ { "hooks": [ { "type": "command", "command": "python $REPO/seneschal/scripts/session_stamp.py", "timeout": 10 } ] } ]
  }
}
```

Notes: the `timeout: 10` keeps a wedged git/filesystem from ever stalling a session (the script itself
is fail-silent and always exits 0); the `PYTHONUTF8=1` env entry stops Windows' legacy console codepage
from tripping Python over emoji/UTF-8 transcript content. The hook prints nothing by contract
(SessionStart/UserPromptSubmit stdout would be injected into the session's context). The setup wizard
will automate this registration later — this is the manual path.

## Pre-approving tools (one-time)

For each Desktop Scheduled Task, **Run now** once and approve the Notion / Calendar / Proton / Telegram
(/ Slack / Gmail) tools it uses, so future unattended runs don't pause. Re-do this if you add a tool to a
mode. (`bypassPermissions` covers the presence daemon's own turns.)

## Cadence & cost knobs

- **Warm-session idle:** `--idle-min` (default 20) — how long a conversation stays warm before winding
  down. Longer = more "always ready"; shorter = leaner.
- **Comms-peek frequency:** `--peek-interval-min` (5 = chosen default; raise it to spend less). Keep the
  Watch peek on a **cheap model** (set it in `--watch-cmd`); the warm chat uses `--model`.
- **Asleep machine:** Desktop tasks do one catch-up run on wake; the daemon resumes when the machine
  does; Telegram holds inbound ~24h, and reminders due while asleep fire on the next loop.
- An optional phone/SMS front door (a call-screener Worker, deployed separately — see
  `push-call.env.example`) can stay live independently on its own hosting for 24/7 coverage.
