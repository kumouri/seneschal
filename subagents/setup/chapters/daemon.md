# Chapter: daemon

The always-on layer — the resident presence daemon, its merge-is-deploy updater, and the
machine-wide session hooks, registered platform-natively (Task Scheduler / systemd user
units / launchd agents). Four movements: common groundwork, the per-OS registration, the
hooks, verification. The design rule that shapes everything here: **at most ONE elevation,
and the wizard never elevates itself** — on Windows the single admin step is a rendered
script the owner reads, approves, and launches in one `-Verb RunAs` shot; on Linux the one
`sudo` is `loginctl enable-linger`; on macOS there is none.

On entry: `mark daemon in-progress`, and check the ledger's auth-models summary — if the
subscription token is absent (`token absent`), say plainly that the daemon will start but
its **warm session can't spawn unattended** (Telegram goes quiet); offer a jump to
`/setup auth-models` or continue eyes-open.

## 1 — Common groundwork (every platform)

**1a. The venv** (confirm-to-run, like every install):

```
uv sync --frozen
```

— add `--extra cockpit` instead when the ledger's `features.cockpit` is true (one sync
covers both). Then check: `.venv`'s python imports `websockets` (the doctor's venv row
logic). No `uv` / sync fails → say what degrades (Discord gateway falls back to REST
polling; cockpit backend can't run) and continue — the venv never gates the daemon.

**1b. Render the local assets.** Dry-run first, show what would be written, then apply on
confirm:

```
python seneschal/scripts/render_units.py --platform <ledger platform> --dry-run
python seneschal/scripts/render_units.py --platform <ledger platform> --apply
```

This renders gitignored locals into `seneschal/state/setup/` from the tracked templates —
the launcher (`run-presence.local.cmd` / `.sh`, absolute paths pinned, the ledger's
`models` watch/slots dials substituted) plus the platform's registration assets. Tracked
templates are never touched. Flags to pass through when they apply:
`--with-health-listener` (Windows, if the owner runs the phone health/presence feed —
ask only if they mentioned it or `HEALTH_SETUP.md` artifacts exist), `--user-level`
(the Windows no-admin fallback, step 2's decline path). Show the printed asset list.

## 2 — Windows: one script, one elevation

The rendered `seneschal/state/setup/register-tasks.ps1` holds **every** admin step:
`seneschald` (at logon, S4U principal — session 0, no console window, restart 1 min ×999,
no time limit, single-instance), `seneschald-update` (every 10 min, windowless via
`wscript.exe` + `run-seneschald-update-hidden.vbs`), and optionally
`seneschal-health-listener`. The parameters mirror `seneschal/scripts/SCHEDULING.md`
exactly — that doc stays the authoritative manual path.

1. **SHOW the script** (read it out of `seneschal/state/setup/register-tasks.ps1`) and say
   what it registers, in one line per task. Never elevate anything the owner hasn't seen.
2. Explain the single UAC prompt: one elevated `pwsh` run registers all of it, writes
   per-task results to `register-tasks.result.json`, and exits. Give the line **verbatim to
   paste in their own terminal** (or run it yourself only on their explicit confirm):

   ```
   Start-Process pwsh -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','<repo>\seneschal\state\setup\register-tasks.ps1'
   ```

3. **Read back** `seneschal/state/setup/register-tasks.result.json` and report each task's
   `ok`/`detail`. A failed task → show its detail, offer a retry or the manual path
   (`SCHEDULING.md`), and mark accordingly — never claim a registration the result file
   doesn't show.
4. **Elevation declined** → the honest fallback: re-render with `--user-level`
   (InteractiveToken principal, registers without admin) and state its caveats plainly — a
   console window appears at logon (no session-0 hiding without S4U), and the tasks run
   **only while this user is logged on**. The owner runs it unelevated:
   `pwsh -NoProfile -ExecutionPolicy Bypass -File <repo>\seneschal\state\setup\register-tasks.ps1`.
   Declining both is fine: `mark daemon declined --summary "no scheduled tasks; SCHEDULING.md is the by-hand path"`.

## 3 — Linux: user units + the one sudo

1. Install the staged units (confirmed):

   ```
   python seneschal/scripts/render_units.py --platform linux --apply --install
   ```

   — copies `seneschald.service` + `seneschald-update.service`/`.timer` to
   `~/.config/systemd/user/`. The service's `EnvironmentFile=-%h/.config/seneschal/daemon.env`
   (and the launcher's own sourcing) is how the unattended daemon gets the auth-models
   chapter's token.

2. Enable (confirmed):

   ```
   systemctl --user daemon-reload && systemctl --user enable --now seneschald.service seneschald-update.timer
   ```

3. **The one sudo — lingering.** Explain before asking: user units normally live and die
   with the login session; lingering makes them start at boot and survive logout — which is
   the whole point of an always-on daemon. Confirmed:

   ```
   sudo loginctl enable-linger $USER
   ```

   Declined → honest note in the summary: the daemon runs only while logged in.

## 4 — macOS: launchd agents, zero elevation

1. Install the staged plists (confirmed): the same `--apply --install` line with
   `--platform darwin` — copies `com.seneschal.presence.plist` (RunAtLoad + KeepAlive) and
   `com.seneschal.update.plist` (every 600 s) to `~/Library/LaunchAgents/`. Both wrap the
   launcher in `/bin/sh -c` sourcing `~/.config/seneschal/daemon.env` first.
2. Bootstrap each agent (confirmed, one per command — no sudo anywhere):

   ```
   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.seneschal.presence.plist
   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.seneschal.update.plist
   ```

   (Undo later with `launchctl bootout gui/$UID/<label>`.)

## 5 — Session hooks (every platform)

The machine-wide session registry hook (`SCHEDULING.md` §5): `session_stamp.py` on all four
session events in the **user's** `~/.claude/settings.json` — never this repo's settings.
Diff first, always:

```
python seneschal/scripts/settings_merge.py --dry-run
```

Show the diff, explain in one line what it buys (every Claude Code session on the box lands
in the session registry, so the daemon defers nudges into live chats; SessionEnd also feeds
the mini-dream distiller), then on confirm:

```
python seneschal/scripts/settings_merge.py --apply
```

It appends, never removes; a second run is a no-op; it backs the file up first and refuses
a corrupt settings.json outright. If it reports a `session_stamp.py` hook pointing at a
**different checkout**, surface that verbatim and leave it — `--force-path` repoints it
here, only on the owner's explicit say-so.

## 6 — Verify

Run what the platform can actually show, and report only what happened:

- **Registered + started:** Windows `schtasks /query /tn seneschald` (and
  `/tn seneschald-update`); Linux `systemctl --user is-active seneschald.service`; macOS
  `launchctl print gui/$UID/com.seneschal.presence | head -5`.
- **Alive:** `seneschal/state/presence.lock` appears within ~a minute of the start and its
  pid is live (the daemon's own single-instance lock).
- **Deploy heartbeat:** `seneschal/state/seneschald-health.json` gets its first stamp on
  the updater's first cycle — up to ~10 min out; say when to expect it rather than waiting.
- Offer **`/doctor`** — its daemon + hooks rows check exactly these things and give fix
  pointers.

## Close

```
python seneschal/scripts/setup_state.py mark daemon done --artifacts seneschal/state/setup/register-tasks.ps1 --summary "<one line: what registered + what was declined>"
```

(Artifacts per platform: the ps1 + `run-presence.local.cmd` on Windows;
`run-presence.local.sh` on POSIX — record what was actually rendered. Partial outcomes are
fine: a `done` with "user-level tasks, linger declined" beats a false green.)

Name the marquee failure this chapter just prevented: an unregistered or session-bound
daemon dies silently with the login — reminders stop firing, Telegram goes quiet, merges
stop deploying, and nothing *looks* broken until the owner notices days of silence. A
registered task with restart-on-failure, a 10-minute updater, and a fresh health stamp make
that silence impossible to miss.
