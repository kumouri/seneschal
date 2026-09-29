# Log rotation for `seneschal/state/logs/` (and the two siblings found beside it)

**Status:** `BUILT` — the helper (`seneschal/scripts/log_rotation.py`), its wiring into
`cockpit_site.py` / `presence.py`'s cockpit-supervision task, and the migration of `presence.log`'s own
rotation onto it. An already-large `cockpit.log` on an existing install is rolled by the first
reconcile pass after the change deploys — see "What ships already-large" below.

## The problem, measured

`seneschal/state/logs/cockpit.log` was a single unrotated file (`cockpit_site.py` `spawn_backend`, the
combined stdout/stderr of the supervised cockpit backend). On a long-running install it reached
**several hundred MB** unrotated over about five weeks, still growing. It was found incidentally by a
sibling research question on whether any `seneschal/state/` store should move off sqlite/JSON, which
correctly set it aside there as a log-rotation gap rather than a database question.

## Who actually opens the file — the finding that shapes everything else

Exactly one process at a time: the spawned `uvicorn` child. `spawn_backend` opens
`seneschal/state/logs/cockpit.log` in append mode, hands that descriptor to the child as its
stdout/stderr, then closes ITS OWN copy — `seneschal/state/README.md`'s entry for this file already
says so. For the entire life of that child (days to weeks between deploys/crashes), **the daemon
itself holds no handle to the file it would want to rotate.** Nothing else opens it: no cockpit UI
panel reads it, no health check tails it (verified — `cockpit/server` never reads it, and the only
reference to the path outside `cockpit_site.py`/`presence.py`/`archon_sites.py` is the human-facing
"check this file" line in a Telegram nudge).

**This is the Windows trap, spelled out.** A file cannot be renamed while *any* process holds an open
handle to it unless that handle was opened with `FILE_SHARE_DELETE` — POSIX has no such restriction,
which is why `logging.handlers.RotatingFileHandler` (whose own test suite runs on Linux CI) looks fine
and is not. Nothing in this tree opens a log with that flag, so a naive rename attempted by the daemon
while the uvicorn child is alive raises `PermissionError: [WinError 32]`. This is not a multi-process
race in the usual sense — there is exactly one writer — but that one writer is a *different process*
than the one that would do the rotating, for the writer's entire lifetime, which is the same hazard
wearing a simpler shape.

`archon_sites.py`'s per-archon `site.log` is written the identical way (same
open-then-hand-to-child-then-close-own-copy pattern) and carries the identical trap. It is smaller in
practice (archons are delegation-on-demand, not always running) and is **not adopted here** — see
"What this change does not do."

## The design: never rename a file while its holder might still have it open

Two supported shapes, and conflating them is the bug this spec exists to prevent:

1. **A log a spawned child holds open (`cockpit.log`, and `site.log` if adopted later).** Rotation can
   only happen at a kill-then-respawn point, because that is the only moment the file is guaranteed to
   have no holder. The daemon's cockpit-supervision task (`cockpit_app_task` in `presence.py`) already
   performs exactly this sequence on every deploy bounce and every crash recovery — this spec adds a
   **third** trigger (log oversized) to the existing "kill it, then reconcile respawns it" loop, and
   puts the actual roll attempt (`log_rotation.roll_closed`) in the one place all three triggers
   already funnel through: immediately after `archon_sites.kill_pid`, before
   `cockpit_site.spawn_backend` reopens the path. No new timer, no new supervision loop — the rotation
   rides the bounce machinery that was already there for deploys.

   A roll attempted right after `kill_pid` can still race: `kill_pid` is deliberately non-blocking
   (`archon_sites.py`'s own docstring — `TerminateProcess` on Windows, no wait-for-exit, because a
   blocking wait in the daemon's event loop would stall reminders/chat for everyone), so the child's
   handle may not be released the instant the rename is attempted. That is fine: a declined roll
   changes nothing about the respawn (the backend still comes back up on the old, still-oversized
   file), and the very next reconcile pass — `COCKPIT_APP_INTERVAL`, 20 s later — sees the file is
   still over the trigger and tries again, by which point the previous process is reliably gone. It
   self-heals within one or two passes; it never blocks, and it never loses a line, because until the
   rename actually succeeds the log is exactly the file it always was.

2. **A log this process holds open itself (`presence.log`, via `presence.py --log-file`, written with
   explicit `.write()` calls, never Python's `logging` module).** Safe on Windows because the object
   doing the renaming is the same object that just closed the only handle to the file — check size,
   close, roll, reopen, all under one lock (`log_rotation.RotatingAppendLog`). There is no other
   holder to race against except a stray human/editor that has the file open for reading, handled the
   same way: declined, logged once, retried on the next write that crosses the trigger.

Both shapes go through the same primitive, `log_rotation.roll_closed`, which:

- Checks the size trigger (skippable with `force=True`, the manual escape hatch below).
- Renames the file to `<stem>-<YYYYmmdd-HHMMSS><ext>` via `os.replace` — **atomic** on both platforms
  for a same-directory rename, so there is no observable moment where the log is neither the old file
  nor the new one. Timestamp-suffixed rather than numerically shifted (`.1`, `.2`, …): shifting N
  existing files on every roll is N renames instead of one, each one a fresh chance to hit the same
  Windows trap, for files that are no longer even open.
- Optionally gzips the rolled file (write `<name>.gz.tmp`, `os.replace` it to `<name>.gz`, then remove
  the plain copy) — a crash between those two steps leaves the uncompressed roll fully intact, never a
  half-written `.gz` masquerading as done.
- Prunes old generations beyond `keep`.
- **Never raises.** A `PermissionError` (or any `OSError`) mid-rename is reported as a declined roll,
  not an error — the caller keeps appending to the untouched original and the next scheduled check
  tries again. A rotation that crashes the thing it was supposed to help is a strictly worse outcome
  than a large file, which is the whole reason this problem sat unrotated for weeks instead of being
  "fixed" by a naive handler that occasionally takes the cockpit down.

`check_state_writes.py --enforce` stays green against this module by construction, not by an allowlist
entry: every mutation is `os.replace`/`os.remove`, never a truncating `open(p, "w")`, so there is
nothing for that check to flag.

## What else is unrotated — named, not fixed here

- **`seneschal/state/presence.log`** — explicitly in scope. It already had a weaker, ad-hoc rotation
  (`presence.py main()`: check size once at process startup, rename the one prior copy aside as `.1`,
  uncompressed, no further check for the rest of the run). That is almost certainly why it never grew
  as large as `cockpit.log` — the daemon restarts often enough (every merge is a deploy) that the
  startup check kept firing — but a daemon that stayed up for weeks without a restart would still grow
  it unbounded, the same failure shape at a slower clock. **It is migrated onto
  `log_rotation.RotatingAppendLog`**, which checks on every write rather than only at startup, keeps
  `DEFAULT_KEEP` (8) gzipped generations instead of one plain one, and is a same-process,
  no-subprocess-trap case — the safer of the two shapes above — so wiring it here doubles as the
  mechanism's second real consumer.
- **`seneschal/state/seneschald-update.log`** — written by `seneschald-control.ps1` via `Add-Content`
  (open-write-close per line, not a held-open handle), so it carries none of this spec's Windows trap —
  but it is **PowerShell**, and this module is Python, so it cannot call `log_rotation.py` directly
  without shelling out to `python` on every log line, which is worse than the problem it would solve.
  **Named as a gap, not fixed here**: a `.ps1`-native rotation (a ~10-line `Rotate-LogIfOversized`
  helper mirroring `roll_closed`'s rename-then-gzip-then-prune shape) is a small, independent
  follow-up.
- **`archon_sites.py`'s per-archon `site.log`** — same subprocess-holds-the-handle shape as
  `cockpit.log`, structurally the same fix (`log_rotation.roll_closed` in the archon-site supervision
  task's kill-then-respawn branch). Not adopted here because no archon site log has been observed
  anywhere near `cockpit.log`'s size (delegation-on-demand archons are not continuously running).
- **Per-watcher and one-off logs** are small, bounded by their own producers' cadence, and not adopted
  here for the same reason.

Adopting any of the above is a follow-up change, deliberately not sprawled into this one.

## Open decisions — defaults chosen and justified below

**Trigger: size vs time vs both.** Left open by the owner ("by size and/or date"). **Default shipped:
size only**, `DEFAULT_MAX_BYTES = 25 MiB`. Reasoning: the measured problem is bytes on disk, not age —
`cockpit.log` took weeks to become a problem, but a burst of errors could do it in a day, and a
time-based trigger would let that burst sit unrotated until the calendar caught up. For the
subprocess-held case (shape 1 above), a time trigger buys nothing over a size trigger anyway: both can
only actually roll at a kill-then-respawn point, so "rotate daily" would still mean "bounce the cockpit
daily," which is a cost with no upside over "bounce it when the log is actually large." 25 MiB is a
guess bounded by the observed growth rate (hundreds of MiB over five weeks, bursty): it rotates
roughly every few days in steady state — frequent enough that no single rolled generation is painful
to grep, infrequent enough not to bounce the backend needlessly. It is one constant
(`log_rotation.DEFAULT_MAX_BYTES`, overridable per call site) and cheap to retune once real rotation
cadence is observed.

**Retention: how many rolls, and whether one is ever deleted.** Left open. **Default shipped:
`DEFAULT_KEEP = 8`** rolled generations before the oldest is deleted (`keep=0` means never delete, this
tree's existing convention for "keep everything" — `turns.py`, `mouth.py`). Reasoning, given the
state-loss incident classes recorded in [state-durability-spec.md](state-durability-spec.md): those
incidents are all about files with **no other copy anywhere** — `carry-over.md`, `context-digest.md` —
where losing the file is losing information that exists nowhere else. A rotated log is different in
kind: it is diagnostic exhaust (uvicorn startup errors, daemon heartbeat lines), regenerable in the
sense that losing an old generation costs nothing beyond "you can't grep that week anymore," never a
fact about the world that existed only there. So this spec does not treat log retention as needing the
same never-delete posture as `carry-over.md`. If the owner decides otherwise, the fix is `keep=0` at
each call site — a one-line change, not a redesign.

**Compression.** Shipped ON by default (`compress=True`) — gzip is stdlib, costs one pass over the
rolled file's bytes (never the live one, so it can never stall a write), and typically returns ~90% of
the disk back on a text log. A failed compression (disk full mid-gzip) leaves the plain rolled file in
place and reports the plain name — never a half-written `.gz` and never data loss.

## What ships already-large

An install that ran before this change may already carry a very large `cockpit.log`. It is not rolled
by any migration step. What happens instead, in order of what actually runs:

1. **Automatic, no command needed.** Once the change deploys (on Path A a merge is a deploy), the
   cockpit-supervision task's next reconcile pass (within `COCKPIT_APP_INTERVAL` = 20 s of the backend
   next being health-checked) sees `cockpit.log` is over `COCKPIT_LOG_MAX_BYTES` and bounces the
   cockpit to rotate it — the same size-triggered path described above. This costs one ordinary
   cockpit restart (the same interruption a deploy or a crash-recovery already causes), not a special
   maintenance step.
2. **Manual, if the owner wants it sooner or wants to watch it happen**: stop the cockpit backend first
   (bounce it via a merge, a daemon restart, or a manual kill of the recorded PID in
   `seneschal/state/cockpit-site.pid.json`), then run
   `python seneschal/scripts/log_rotation.py roll --path seneschal/state/logs/cockpit.log --force`.
   This rolls the file to a timestamped, gzipped sibling immediately; the next boot of the cockpit
   recreates a fresh `cockpit.log`. If the backend is still running when this is invoked, the roll
   declines and prints why (`PermissionError`) rather than doing anything destructive.

## Test coverage

`seneschal/scripts/test_log_rotation.py` — the module in isolation: the size boundary (`>` not `≥`),
a successful roll losing no bytes, a `PermissionError` mid-rename declining without touching the
original (the Windows-trap simulation, since this suite spawns no real child process), retention
pruning oldest-first, `keep=0` meaning never-prune, compression preserving content byte-for-byte, a
failed compression leaving the plain roll readable, and the CLI's `check`/`roll`/`--force` paths.

`seneschal/scripts/test_cockpit_site.py` — the wiring: an oversized log bounces an otherwise-healthy
backend (mirroring the existing stale-UI-build bounce test), the roll actually happens before the
respawn opens a fresh file, and — the one that matters most — **a roll that cannot happen (mocked to
decline) still lets the respawn proceed**, so a stuck rename can never mean a stuck cockpit.

## What this change does not do

- Does not run a one-off migration over an existing oversized `cockpit.log` (above).
- Does not adopt the helper into `archon_sites.py`'s `site.log` or `seneschald-update.log` (named as
  gaps).
- Does not change `check_state_writes.py`'s allowlist (nothing here needed an exemption).
- Does not add a new daemon task or timer — rotation for the subprocess-held case rides the existing
  cockpit-supervision reconcile loop; rotation for the self-held case rides the existing `--log-file`
  writer.

## Router entry

**Router status:** **BUILT**. **What it decided:** a different problem from
[state-durability-spec.md](state-durability-spec.md)'s backup rotation (that one guards irreplaceable
files against a bad in-place write; this one guards an ever-growing log against never being rotated at
all). **The finding that shapes the design: on Windows a file cannot be renamed while any process holds
it open without `FILE_SHARE_DELETE`**, and `cockpit.log`'s sole holder for its whole life is the
spawned uvicorn child, not the daemon that would want to rotate it — the `RotatingFileHandler` fix that
"obviously" works is wrong here for a reason invisible on Linux CI. So `roll_closed` only fires from the
cockpit-supervision task's existing kill-then-respawn point (a new size trigger beside the
deploy-bounce and stale-UI-build ones, never its own timer), and `RotatingAppendLog` handles the other
shape — a log the process holds open itself (`presence.log`). **Never raises**: a declined roll keeps
the caller appending to the untouched original and retries next pass. Rolls are timestamp-suffixed and
gzipped via write-temp-then-`os.replace`. Two open decisions, defaults chosen and justified: retention
(`keep=8`) and the trigger (size only, 25 MiB). Named gaps, not fixed: `archon_sites.py`'s per-archon
`site.log` and `seneschald-update.log`.
