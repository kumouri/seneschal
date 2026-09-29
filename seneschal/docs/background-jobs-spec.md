# Durable background jobs — "I'll tell you when it's done," made real

**Status:** `PARTIAL(BUILT + §3.7-§3.14)` — the detached ledger, shim and completion push · §7
transient-failure retry · §3.8 live logs + the tree kill · §3.9 the spawn preflight, made
namespace-aware in §3.9.1 · §3.10 a cancel is unoverwritable · §3.11 exit 0 vs. the log · §7.2.1 the
classifier reads the failure end · §3.12 the concurrency caps, counting agent jobs only · §3.13 the
job-completion guarantee (detect/resume/rescue) · §3.14 the preventive rail
(`job_background_guard.py`). · **Scope:** `seneschal/scripts/jobs.py`,
`seneschal/scripts/job_completion.py`, `seneschal/scripts/job_background_guard.py`, the PR CI watcher
(§4), and `presence.py`'s two touchpoints (the scheduler tick's reconcile, the drainer's lease guard).

**The problem.** When the assistant says it spawned something, the work must not die when the warm
session dies (and if it would, the session must stay alive until it's done), and the notification
when it finishes must actually be fired and sent to Telegram. Saying "I'll tell you when I finish"
from a warm session, then having the work die or the promise go unkept, is the failure this spec
exists to remove.

---

## 1. The two failures, precisely

**(a) The work dies.** Anything spawned from inside a warm-session turn is a child of the `claude` CLI
process the daemon holds. That process is killed on the 20-minute idle wind-down
(`presence.drainer_task`), on any turn error, and on every merge reload — and reloads are a large share
of all session deaths. So the work stops half-done.

**(b) The promise dies.** The notification lived in the turn that made it. Once the session wound down,
nothing anywhere held "buzz the owner when X finishes" — so even work that *survived* went unreported
until the owner asked. From a warm session that winds down on idle, "I'll watch it / I'll circle back"
is hollow — nothing wakes it.

**Why it had to be code, not prompt.** `state/metrics.jsonl` is the precedent: an observability contract
specified prompt-side only, which produced **zero rows in a month**. A promise the assistant has to
*remember* to keep is not a mechanism. The completion push is therefore fired by the daemon off a
durable ledger; the prompt-side rule (Chat rule 10, the grounding block) only steers the assistant
toward *using* it, and the feature works whether or not it does.

## 2. The invariant

> **No silent path.** Every job reaches a terminal state, and every terminal state notifies exactly
> once — including the ugly ones.

| Terminal state | Cause | Detected by |
|---|---|---|
| `done` / `failed` | the command exited | the shim's exit stamp (the normal path) |
| `timed-out` | `deadline_sec` elapsed | the shim itself; re-checked by `reconcile` as a backstop |
| `ended-unknown` | the PID is gone with no stamp (shim killed, machine crash) — **or the exit code says 0 while the attempt's own parting lines declare a failure (§3.11)** | `reconcile`'s liveness probe · the shim's log/exit cross-check |
| `cancelled` | `jobs.py cancel` | explicit — and **sticky**: no later write may take it back (§3.10) |

There is one **non**-terminal state besides `running`: `retry-pending`, added with §7. It notifies
nothing (that is what "not terminal" buys) and it is not the end of anything — a job can never *finish*
in it, because both ways out are covered: the attempt comes due and runs, or the window/budget closes
and `reconcile` calls it `failed` and pushes.

`ended-unknown` exists because the honest answer to "did it finish?" is sometimes *"I can't tell."*
Guessing success there would be a cheerful lie — the push says so plainly instead. **§3.11 widened what
reaches it**, and only in that direction: a `done` whose own log declares a failure is a disagreement
between the two things that can be known about an outcome, not a success. (Why a finished job could
also be mislabeled `ended-unknown`, and the fix: `jobs-ended-unknown-spec.md`.)

**And "exactly once" is not the whole invariant — the other word is *honestly*.** §3.11 is the shape
where the count held and the content did not, which is the worse half: a missing ping gets re-asked, a
wrong one does not.

`test_jobs.py::test_every_terminal_path_notifies_exactly_once` is the regression pin for all five.

## 3. Design

### 3.1 Detached survivors

`start_job` spawns with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` (Windows) /
`start_new_session=True` (POSIX) — the same model the daemon uses to relaunch itself. The child leaves
the CLI's process tree, so none of the three session deaths can reach it.

### 3.2 The shim, and why there is one

`start_job` doesn't spawn the caller's argv directly. It spawns `jobs.py __run <id>`, which runs the
real command as *its* child, waits, and stamps the exit code. Without it there is no portable way to
learn an exit code from a detached process after the fact — and "green vs red" is precisely what the
first consumer needs to say. The shim also enforces the deadline locally, so a job stays bounded even
while the daemon is down.

Two consequences, both handled:

- **Cancel has a tree to kill.** The record carries `pid` (shim) and `child_pid`; `cancel` kills both,
  tolerantly. On Windows a terminated shim runs no cleanup handler, so its child would otherwise orphan.
  **Each of those two kills reaps that PID's whole TREE** — see §3.8, because the two PIDs on the
  record turned out not to be the whole tree.
- **The grandchild's output must be redirected explicitly.** Inheritance silently loses it: `subprocess`
  only builds the `handle_list` that survives `close_fds=True` when handles are passed explicitly, so an
  un-redirected grandchild of a `DETACHED_PROCESS` parent writes to nothing. That produced a 0-byte log
  in the first smoke test — and the log is what the push's `Last line:` and every `--wake` read depend
  on.

### 3.3 The ledger and the reconcile

One record per job at `state/jobs/<id>.json` (+ its `.log`), mirroring `state/sessions/<id>.json`. Ids
are `YYYYmmdd-HHMMSS-xxxx`, so sorting the directory sorts by start time.

`presence.scheduler_task`'s ~5 s tick calls `jobs.reconcile` (in a thread — the Telegram send does
network I/O with a retry). Notification is driven off the **record**, not off the pass that detected
the end, which is what makes a restart mid-job harmless: the successor daemon reads the same
un-notified terminal record off disk and sends.

**Interim push path.** Until the daemon's tick calls `reconcile` with its own delivery callback,
`python seneschal/scripts/jobs.py reconcile --send [--telegram-env PATH]` runs the same pass and
pushes through `telegram_send.py` (`jobs.telegram_notify`; discord/cockpit channels fall back to
Telegram and say so). Schedule it every minute or so. It passes no wake callback, so a `--wake`
job gets its push but not its wake on this path; a plain `reconcile` only prints.

**The one fail-closed thing in the module.** `notified_at` is stamped *only* when the send actually
landed. A Telegram outage retries next tick rather than eating the ping — the write-behind outbox's
posture, deliberately not `metrics.jsonl`'s fire-and-forget. Everything else here is fail-open, per
`sentinel.load_json`'s rule: a corrupt gitignored state cache must never crash the daemon.

**§3.3.1 — but `notified_at` does not ENFORCE "exactly once", and never could.** It lives on the job
record, and the job record has more than one writer. `cancel_job` claims it, spends up to 60 s in two
`taskkill /T /F` calls, then re-asserts its stale in-memory copy — and the ~5 s reconcile tick can land
inside that window, push, stamp, and have the stamp erased underneath it: two byte-identical pushes
seconds apart, and **one** `notified_at` on the record. In practice this hit only cancels, and a
meaningful fraction of them. Two fixes, neither sufficient alone: `reassert_cancel` merges instead of
clobbering (§3.10's shape, at the writer), and **`job_push_ledger.py` gates the send on
`(job_id, outcome)`** from a file nothing else writes. **It dedupes; it never suppresses** — every
degradation (unreadable entry, unwritable directory, wedged lock, a claim whose holder died) resolves to
*send*, because a job that finished unreported is worse than one reported twice, and a prevented
duplicate is logged and counted rather than silent.

**§3.3.2 — and an AMBIGUOUS send is notified-with-a-caveat, not a failure to retry.** The `notify`
callback may return `{"ok", "ambiguous"}` instead of a bool (a bool still means exactly what it meant).
The Telegram send path classifies a send failure positionally — pre-delivery vs the request being on
the wire — and refuses to retry the latter; a bool boundary that threw that away would let `reconcile`
read `False` as licence to send again next tick, the same blind retry one layer up. An ambiguous push
stamps `notified_at` **and** `notify_delivery: "ambiguous"`, so the record says what could be proved
rather than claiming delivery. A **provably** pre-delivery failure still retries, unchanged — that
polarity is the guarantee and may not flip.

### 3.4 Liveness is conservative on purpose

`pid_alive` never uses `os.kill(pid, 0)` on Windows — CPython maps `os.kill` there to
`TerminateProcess`, so the conventional POSIX liveness probe would **kill the job it is asking about**.
It uses `OpenProcess`/`GetExitCodeProcess` via ctypes, and any probe it cannot answer reads as *alive*,
since a wrong "it ended" fires a premature false ping. That is safe only because `deadline_sec` always
has a real default (6 h): **the deadline, not the probe, is what guarantees every job reports.** PID
reuse is covered the same way.

### 3.5 `--wake` — the hybrid (default-on for the owner's own asks)

The bare push always fires; that is the guarantee, and it costs nothing (no LLM). `--wake`
*additionally* enqueues a synthetic inbound into the same durable action queue Telegram/Discord/cockpit
use, so the warm session spins up and reports the result in voice. If the wake fails the owner still
got the push. The wake line is a bracketed system line (`[job finished: …]`) matching the
`[attachment: …]` / reaction-line convention, and it says outright that it is not the owner speaking —
the grounding prompt frames inbound as the owner's words, so it must not pretend to be.

The daemon's job reconcile does the wake enqueue off `reconcile`'s **return value** rather than its
`wake` seam, because the enqueue is async and reconcile runs in a worker thread.

**`--wake` used to be opt-in, and that was the bug.** A job could end `ended-unknown` with no `--wake`
on it: the bare push landed, but nothing handed the job back to a session to read, so nothing was said
about it until the owner asked, hours later. "The model will remember to pass `--wake`" is exactly the
prose-countermeasure shape that does not hold, so the fix is not a reminder, it's a default that no
longer depends on anyone remembering: **`jobs.py start` defaults `--wake` ON whenever the job resolves
to `origin.request.by == "owner"`** (`jobs.default_wake`) — a job the owner asked for, whether by a
chat message or a Telegram tap. `--wake` still works exactly as it always has, and `--no-wake` is the
explicit opt-out for an owner-origin job that would otherwise be noisy (a long watch, a routine
re-run). An `assistant`-requested or `unresolved`-origin job — a Dream step, a scheduled sweep,
anything daemon-internal — keeps the default of **no** wake: this is about a request the owner made
getting a guaranteed report, not about the daemon waking itself on its own cadence.

`wake_text` also carries the job's **duration** and a bounded **tail of its log** (`_WAKE_TAIL_LINES` =
20 lines, hard-capped at `_WAKE_TAIL_CHARS` = 4000 rendered chars) — the woken session must be able to
report the outcome, including an `ended-unknown`/`failed` job's own final words, without a second
lookup. The exit code line is explicit when there isn't one (`", no exit code"`) rather than silently
omitted.

**A `cancelled` job's wake is not unconditional** (`cancel-attribution-spec.md` §12 owns the full
design; this is the pointer). `presence.job_wakes` — the wake-enqueue list pulled out into its own
pure, testable function — drops a job entirely via `jobs.cancel_wake_suppressed` when an assistant
surface (the warm daemon session or a desktop `/assistant`) cancelled it on its own initiative: the
turn that cancelled it already told the owner why, and the wake existed to make sure *some* session
tells them, which already happened. The bare push is unaffected — `notify_text` still fires exactly
once, just with a shorter head line for this case — and a cancel the owner asked for, or one from a
delegated/build session, still wakes exactly as before.

### 3.6 The session lease

For work that genuinely cannot detach. A `--lease` job makes `jobs.lease_active` true, and
`drainer_task` defers its idle wind-down while it is.

Bounded in three independent ways, because a session held open forever is its own bug:

1. `LEASE_MAX_SEC` (30 min) — past it the lease lapses and the session winds down normally. The **job**
   keeps running and still notifies: a lease governs the session, never the job.
2. **A pending control always wins.** `not state.control_pending` is checked *first*. Otherwise a long
   job would silently block a merge deploy, and merging-is-deploying is the invariant the whole reload
   path rests on.
3. Fail-open: any error reading the store → no lease.

The probe runs only at the moment the drainer would otherwise wind down, so it costs one small directory
read per wind-down decision, not one per tick.

### 3.7 Retention

`jobs.py prune --days 14`, nightly in Dream alongside the other `--prune-days` sweeps. It drops terminal
jobs **whose push already landed**, with their logs. A terminal job that never got its ping is never
pruned — the GC must not be what makes a job go silent.

### 3.8 The log is live, and the kill takes the tree

Cancelled jobs — a `pwsh -File …` and a `claude -p` — each left a **0-byte** log, while a job that ran
to completion in the same window left a readable one. No shared child, so no single program's buffering
could be the story. Two separate defects came out of it, and the first is much the more important
because it was never about cancelling at all.

**The log was never buffered by `jobs.py`, and `cancel` never ate a byte.** Both were checked rather
than assumed. The shim hands the log file's descriptor straight to the child, so anything the child
actually writes is on disk the instant it writes it; and a cancelled job's already-written output was
measured intact, before and after, to the byte. What was actually happening is that **stdout to a file
is block-buffered by C stdio**, so the child was holding its output in its own address space. Measured
against a throwaway state dir:

| child | log at 12 s | at exit | kept through a cancel |
|---|---|---|---|
| `python -c "print(…); sleep(40)"` | **0 bytes** | complete | **nothing** |
| the same under `python -u` | first line present | complete | everything written so far |
| `pwsh -NoProfile -File …` | first line present | complete | everything written so far |

So *"a cancel loses the log"* was the visible face of **"a running job's log is empty until it
finishes"** — and that is the finding that matters, because it means `jobs.py status` on a healthy long
job was byte-identical to one that was wedged, and `--analyze` was handed an empty file in precisely the
case it exists for (**a timeout is a kill**). Note the shape of the misdirection: a completed small
Python job that "proved" logging worked had simply flushed everything at exit, which looks exactly like
a log that had been live all along.

**The fix is to stop the buffering, not to rescue it afterwards, because afterwards is not available.**
On Windows `os.kill` is `TerminateProcess`; there is no signal a doomed process can flush on, and the
detached child has no console for a `CTRL_BREAK_EVENT` either. So `child_env()` sets
`PYTHONUNBUFFERED=1`, which covers every Python child — the PR watcher, `job_analysis.py`, the shim
itself, most of what runs here. **The residual limit is real and is not papered over: a non-Python
child that block-buffers its own stdout still writes nothing until it flushes, and nothing outside that
process can change it.** `pwsh` happens to flush as it goes; a future job whose child does not will look
empty again, and the answer will be to make *that* child unbuffer.

**The second defect: the kill reached two PIDs, and a job is not two PIDs.** A job is very often
`pwsh` → `gradlew.bat` → `java`; the record carries the shim and its immediate child, and everything
below that survived. Measured: a job cancelled at 10 s had its grandchild still executing — and still
**appending to the log of a record that already said `cancelled`** — 15 s later, and still going minutes
on. Three symptoms that read as unrelated bugs, all of them this:

- a "cancelled" job still burning CPU and still doing whatever it was told to do;
- a `--worktree` teardown refused with `Permission denied` and recorded as a `worktree_leaked`, for a
  tree that only leaked because a live grandchild was sitting in it;
- a terminal record whose log keeps growing, so the push's `Last line:` and `--analyze`'s input are
  both read out from under a writer nobody stopped.

`kill_pid` therefore reaps the tree: `taskkill /PID <pid> /T /F` on Windows, `killpg` on POSIX with an
explicit refusal to signal **our own** process group (the shim is spawned `start_new_session=True`, so
it leads its own; a child that somehow never got one would otherwise take the daemon down with the
job). Still SIGTERM rather than SIGKILL on POSIX, still no wait-then-escalate loop, and still every
failure swallowed — a kill that raises out of `reconcile` would cost the completion push. The PID-reuse
caveat the module already accepted widens with this: a stale PID used to cost one wrong process and now
costs one wrong tree. That stays acceptable for the same reason it always was — this only runs against a
record we still believe is `running`.

**One remaining path is now recorded rather than silent.** If the shim cannot open the log at all, the
real command's output goes to `DEVNULL` — a total, permanent loss that used to look identical from the
outside to "the child hasn't printed yet". It now stamps `log_error` on the record. The job still runs:
an unwritable log is worth the output, never the work.

**What is deliberately NOT changed.** No grace period before the kill (there is nothing on Windows for
a grace period to buy), no flush-before-kill (the buffer is not ours to flush), and no change to the
push: a cancelled job still notifies exactly once, as every terminal state does.

### 3.9 The spawn preflight — a mangled argv is refused, not spawned

Two jobs died within seconds of spawning, and **neither failure was in `jobs.py` or in the job**. In
both cases the script path *in the argv* had already been mangled by the shell that launched
`jobs.py start`:

| what was asked for | what the command actually received | exit |
|---|---|---|
| `pwsh -NoProfile -File C:\…\run-spec.ps1`, launched from Git Bash | `C:Users…Temp…run-spec.ps1` — bash ate every backslash | **64** |
| `wsl.exe -d <distro> bash /mnt/c/…/transcribe.sh`, launched from Git Bash | `C:/Program Files/Git/mnt/c/…/transcribe.sh` — MSYS rewrote a POSIX-looking argument bound for a native `.exe` and prefixed its own install root | **127** |

Both spawned *successfully*, ran for under three seconds, and spent a completion push on a failure the
owner then had to read and re-derive. **Both were detectable before the spawn, because in both cases the
file the command was told to run did not exist.**

**The fix is not another rule.** A "shell work goes in a script file" rule exists precisely because of
this class of failure — Windows-path quoting is the single most common way a shell command fails at the
parser. That rule is prose, it is addressed to the operator, and it prevented neither of these. So the
guard is a **shape** the mangled argv cannot fit through, the same move `job_analysis.py`'s schema makes
for *"point, don't diagnose"*.

`preflight_refusal(argv, exists=…)` inspects the argv about to be launched and refuses — **nothing
spawned, no job record written, exit 2** — when an argument that is *definitionally* the path of a script
the command requires names nothing on disk. A refused job never started, so it must not turn up in the
ledger as a `failed` one.

**FAIL-OPEN ON UNKNOWN, FAIL-CLOSED ON KNOWN-BAD**, the line `pending_checks._jobs_dir_readable`
(guarding `pending_checks.refusal`) and the watch ack gate already hold. Only three shapes are inspected:

- `pwsh` / `powershell` … **`-File <path>`** (or `-f`)
- `bash` / `sh` **`<path>`**, including after a `wsl.exe [-d <distro>]` prefix. A shell's script
  argument is read as a **POSIX** path either way, so `/mnt/c/x` is translated back to `C:/x` for the
  check, and any other POSIX-absolute path (`/c/Users/…`, `/home/<user>/x.sh`) is untestable rather
  than missing. **That is a claim about the SPELLING and says nothing about which filesystem the
  interpreter can reach — the two were collapsed into one here, and §3.9.1 is what that cost**
- `python` / `python3` **`<path>.py`**

Everything else is left alone, and the exclusions are as load-bearing as the inclusions: **`bash -c` and
`python -m` are skipped** because the argument after them is a program, not a path; **a relative path is
skipped** because what it resolves against is the job's `cwd`, and a `--worktree` job's `cwd` does not
exist yet when this runs; a path inside the WSL distro's own filesystem (`/home/<user>/x.sh`) is skipped
because a Windows process cannot stat it — *untestable is not missing*. **A false refusal that blocks a
legitimate job would be worse than the bug being fixed.**

The **MSYS signature is named specifically** — an argument containing `/mnt/` that is *not* at the start
of the string — because `exit 127` on a path that reads as plausible is otherwise baffling, and the next
person to hit it should not have to re-derive Git Bash's involvement. The same goes for a drive letter
with no separator after it: the refusal says a shell ate the backslashes, rather than leaving a reader to
notice that `C:Users…` is one character short of correct.

`--no-preflight` is the escape hatch, and its help text says what it is for: **the case where the
preflight is wrong** (an unusual argv it misreads), never the case where the path is. It runs at the CLI
rather than inside `start_job`, because the CLI is the only caller whose argv came through a shell —
`request_analysis` and the retry respawn both build theirs in code, and neither should be blockable by
this. `exists` is the sole filesystem contact and is injectable, so the tests need no real files.

#### 3.9.1 …and it tests in the namespace the command will run in

**The check was validating in the wrong namespace, so it passed a job that could not possibly find its
script.** A long measurement job launched by a desktop session died at **exit 127 in under a second**:

```
/bin/bash: C:/.../scratchpad/probe.sh: No such file or directory
```

argv: `["bash", "C:/.../scratchpad/probe.sh"]`. **The script was not missing** — `Test-Path` on that
exact path from Windows returns `True`. On a Windows host with WSL installed, **bare `bash` can resolve
to `C:\WINDOWS\system32\bash.exe`, which is WSL's bash**, and WSL reaches the Windows filesystem only
under `/mnt/`. §3.9's preflight asked *Windows* whether the file was there while the command was about
to ask *WSL*, so it returned "fine" for the one shape it most needed to catch.

**That is the worst available shape of bug.** The job reports `failed` with an error naming a real file
as missing, so the reader goes and looks at the file — and finds it. The interpreter is never
suspected. And the blindness was structural, not incidental: §3.9's own bullet already said *"a shell's
script argument is read as a **POSIX** path either way"*, which is true about the SPELLING and says
nothing about **which filesystem the interpreter can reach**. The two questions had been collapsed into
one.

**So the namespace is a separate verdict, decided from EVIDENCE.** `bash.exe` under `System32` is WSL's
launcher; a Git-for-Windows `bash.exe` is not, and Git Bash reads `C:/…` perfectly well — getting that
distinction wrong in the other direction would refuse *working* jobs, which §3.9 rules is worse than the
bug. Hence:

| argv shape | namespace | how it is known |
|---|---|---|
| `wsl.exe …` | **WSL** | by construction, whatever follows the launcher |
| bare `bash` / `sh`, Windows host | **WSL** *only* if it resolves into `System32` (`Sysnative`/`SysWOW64` too) | `shutil.which` — **the same PATH lookup the spawn is about to do** |
| bare `bash` / `sh` resolving anywhere else, or not resolving at all | UNKNOWN | Git-for-Windows, MSYS, busybox: `C:/…` is fine for them |
| `pwsh` / `powershell` / `python` / a `.ps1`, `.py`, `.bat`, `.cmd`, `.exe` token, not behind `wsl.exe`, on a Windows host | **Windows** | a native Windows process by definition |
| anything else, and **every command on a non-Windows host** | UNKNOWN | on a POSIX host the host's own shell *is* the namespace — there is no second one to be mismatched against |

A candidate path is then translated into the other namespace and the mismatch refused **in both
directions** — `C:/x` or `C:\x` ⇄ `/mnt/c/x`. The mirror image is real: `/mnt/c/…` handed to a native
Windows process is not the C: drive, it is a directory called `\mnt\c` on whatever drive happens to be
current.

**UNKNOWN refuses nothing**, and that is the load-bearing half. So is the shape of the path: only a
drive-*rooted* `C:[\\/]…` counts as a Windows path here, because the separator-less `C:Users…` is §3.9's
eaten-backslash mangling and its message is better; and the MSYS signature still outranks both, because
that string is Windows-shaped without ever having been meant to be.

**The argv is NEVER rewritten.** Rewriting a caller's command is a surprise, and a job that runs
something slightly different from what was asked is a failure class of its own. The refusal names the
interpreter's namespace *with its evidence*, the path as given, the path it would have to be, and both
fixes (`pwsh -NoProfile -File <windows path>`, or the `/mnt/c/…` form) — and then stops. Same exit 2,
same "nothing spawned, no record", same `--no-preflight`, all unchanged.

Both namespace seams (`which`, `is_windows`) default to `None` and resolve at **call** time
(`_default_which`, `_HOST_IS_WINDOWS`) rather than being bound into the signature, because `main` calls
`preflight_refusal` with no arguments and the CLI path has to be pinnable by a test too.

### 3.10 A cancel is terminal and unoverwritable

**What was observed.** Of several jobs cancelled with `jobs.py cancel <id>` in one evening, most
recorded `cancelled` with no exit code. One recorded **`failed`, exit 1**, with `cancelled_by` absent
from the record entirely, and *that* is the story the completion push told the owner.

**The mechanism.** `cancel_job` and the shim are two independent writers of one file, and neither takes
a lock — every writer in this module is load-modify-save. `cancel_job` killed the child, killed the
shim, stamped `cancelled` in memory, resolved attribution, and saved **last**. But the kill of the child
is precisely the event that returns the shim from `proc.wait()`. So killing first handed the shim a head
start on the same record, and the outcome was decided by whichever `save_job` landed later.

The shim's existing "cancel wins" re-read was not the safety net it looked like. It re-reads *before*
classifying the attempt (`read_log_span` + `plan_retry`, up to 64 KiB of log) and writes after, so it is
a TOCTOU with a window wide enough to lose — and the losing write does not merely change `status`, it
writes back a record loaded *before* the cancel, wiping `cancelled_by` and `cancel_reason` with it.
**Duration did not predict which cancel lost**, which is exactly the signature of a race rather than a
threshold.

**Why it is more than cosmetic.** The ledger stops being able to tell a deliberate stop from a job that
died on its own, and every later reading — a report to the owner, a Dream summary, a retrospective —
inherits the wrong story from a record that looks authoritative. And the push is simply false: the owner
stopped it, and was told it failed.

**The fix, in two halves, because neither alone is sufficient.**

1. **`cancel_job` claims the record on disk BEFORE it kills.** The child cannot die before the claim
   lands, so the shim's re-read always finds it. This closes the ordinary path and does it by removing
   the head start rather than by racing faster. The claim's own write is guarded and re-asserted after
   the kill: an unwritable record must not cost the kill either, and attribution stays inside its own
   `try` exactly as §4.2 of `cancel-attribution-spec.md` requires.
2. **`honour_cancel`, inside `save_job`.** `cancelled` is a **sticky** status: once it is on disk, no
   later write may relabel the job `done`, `failed`, `timed-out` or `ended-unknown`. It lives at the one
   chokepoint every writer already goes through, so the guarantee holds **regardless of write
   ordering** — including the interleavings half 1 does not cover (a job that genuinely finished in the
   same instant, a `reconcile` backstop already in flight).

Only the fields a cancel *owns* are protected — `status`, `ended_at`, `exit_code`, `cancelled_at`,
`cancelled_by`, `cancel_reason`. Everything else in a later write goes through untouched, deliberately:
`reconcile` still stamps `notified_at` on a cancelled record and `release_worktree` still records the
teardown, and a guard that blocked those would cost the completion push — strictly worse than the bug.
The losing writer's `attempts[]` entry is **kept**, as the honest record that the shim did observe
exit 1. Top-level `exit_code` is not, because `wake_text` reads it and "status cancelled, exit 1" is the
same lie in a smaller font.

**A rejected alternative: a lock.** A lockfile or a mutex around the record would also close it, and
would add a new failure mode (a stale lock wedging the daemon's tick) to a module whose entire posture
is fail-open. A precedence rule cannot wedge anything.

**`cancelled_at`, and why the field exists.** `ended_at` is stamped for every terminal state and means
*the job stopped*; `cancelled_at` means *somebody asked for this*, which only a cancel has. Without it,
telling a deliberate stop from a spontaneous one meant parsing `cancelled_by` — which rung 3
legitimately omits, so an unattributed cancel was indistinguishable from a job that ended by itself.
Both are stamped from the same instant.

**`--analyze` does not fire for a cancel.** `jobs.ANALYSABLE` is `(failed, ended-unknown)`. An analysis
of a cancelled job hands the analyst a log and a goal line describing work that was *deliberately
abandoned*, with no way to know that — so it spends a real model one-shot investigating an incident that
never happened and files leads about it. The session that cancelled the job already knows why. This
governs the manual `jobs.py analyze <id>` too, deliberately: an operator who wants fresh eyes on a
cancel has the log path.

**Pinned by** `test_jobs.py::CancelIsTerminalTests`, which drives the **real** shim against a killed
child and places the cancel's write at a *chosen* point in the shim's own sequence — so both
interleavings run deterministically, with nothing sleeping and nothing depending on scheduling. A test
for a race that can only be provoked by timing is a test that passes by luck, which is the state this
bug shipped in.

### 3.11 The exit code says 0 and the log says otherwise

**What was observed.** A long remote-training job was recorded `status: done`, `exit_code: 0`, and the
owner was pushed **`✅ Finished`**. Its own log ends:

```
[<timestamp>] payload_finished {"exit_code": 1}
[<timestamp>] launch_finished {"ok": false, "error": "RemoteFailure('remote payload exited 1 on <host>')"}

launch failed: remote payload exited 1 on <host>
events: …/launcher-events.jsonl
```

**Two guarantees went at once.** The push was false; and because `--analyze` fires only on an ending in
`ANALYSABLE`, the fresh-eyes analysis the job had explicitly asked for (`analyze: true` was on that
record) was never filed. §2's invariant is that every terminal state notifies *honestly*, and this is
the shape where "exactly once" holds and "honestly" does not — which is worse than a missing ping,
because a wrong answer is not re-asked.

**Where the code was lost, precisely — and it is not here.** The argv was
`wsl.exe -d <distro> bash -c "bash <wrapper>.sh"`, and the chain reads, end to end: an on-box gate
printed `-> FAIL` and exited non-zero, which broke the payload's `&&` chain, so the remote payload
exited 1; the launcher reported `launch failed: …` and exited non-zero; and then the wrapper script — on
the WSL side, **outside this repo** — reached

```bash
if grep -q -- "-> FAIL" "$LOG"; then
  notify "gate FAILED … a REAL verdict …"
  exit 0
fi
```

and took that branch. `wsl.exe`, `bash -c` and the shim each propagated their child's status correctly;
**nothing in `jobs.py` dropped anything.** The wrapper's author considered a gate FAIL a legitimate
verdict, so **that `exit 0` was deliberate**. It was nonetheless wrong there — the payload also failed
to complete, and the FAIL branch masked that — but fixing it is an edit to a file this repo cannot
reach.

**So `failed` is not a verdict this module is entitled to reach.** Calling it `failed` would mean
overruling a command's own stated exit contract on the strength of prose in its log, and the
`grep -q -- "-> FAIL"` branch above is a live example of a program that means its zero. What `jobs.py`
*can* say is that **its two sources of truth disagree**, and there is already a state for exactly that:
`ended-unknown` — *"an unreadable outcome is reported as unreadable"* (§2). It is not a fourth state, it
costs no extra push, and it is in `ANALYSABLE`, so the missing half comes back with it.

**The check.** For an attempt that exited **0**, the last few non-blank lines **of that attempt's own
span** are matched against `jobs.FAILURE_ASSERTIONS`. A match records
`unknown_reason: {kind: "log-contradicts-exit-code", exit_code, signature, line}` and the status becomes
`ended-unknown`. The observed `exit_code: 0` is **kept** — it is the evidence, and half of what
disagrees.

**The whole risk is a FALSE positive, and the WINDOW does more of the work than the pattern.** Three
bounds, each of which is a documented exclusion rather than a tuning knob:

1. **Exit code 0 only.** A non-zero exit already reports honestly; there is nothing to correct.
2. **The last five non-blank lines of THIS attempt.** Five because the shape being caught is a wrapper
   that declares the failure and then echoes a line or two of cleanup on its way out (the observed
   declaration was third-from-last of five). A step that failed and was recovered from has pages of
   output after it; a previous *attempt's* declaration is below this attempt's `log_offset`.
3. **A failure assertion at column 0**, with at most one non-numeric word before it. Deliberately
   excluded: a mention anywhere but line-initial (`3 tests failed, 1 passed`,
   `see docs/why-the-build-failed.md`); an **indented** assertion, because indentation is how a wrapper
   reports a *sub*-step and a failed sub-step is compatible with a successful job; a zero or `none`
   count (`Failures: 0`); bare `error` / `Traceback`, which every verbose build prints while succeeding;
   `exit code 1` in prose, which is what a working supervisor says about its child; and
   **`"ok": false`** — considered and **rejected** though it appears in the observed log, because it is
   a field-level claim and a job whose last output is a health summary or an API body legitimately
   carries one about something other than itself. The line-initial assertion already catches this
   case, so the second pattern would buy nothing and spend precision this list has to keep.

**It can only ever move `done` → `ended-unknown`.** Never the reverse, never to `failed`, never on an
attempt that already reported a failure, and — since `ended-unknown` is not retryable (§7.2) —
**nothing new re-runs because of it**, `--retry` on or off. An unreadable log yields `[]` and no
verdict: no evidence is not evidence of failure.

**The push names which half is unreadable**, because the generic `ended-unknown` wording ("not cleanly
enough for me to read an exit code") would be simply wrong here — the code was perfectly readable and
said 0. It reads *"exited 0 …, but its own log declares a failure — so I genuinely can't tell you
whether it finished"*, quotes the declaring line as its own part (it is usually **not** the last line),
and `wake_text` never carries a bare `, exit 0` on such a record — the `CANCEL_OWNED_FIELDS` argument,
*"the same lie in a smaller font"*, pointing here.

**Pinned by** `test_jobs.py::LogContradictsExitCodeTests`, whose false-positive half — a dozen real
successful-job tails that must stay `done` — is the load-bearing part, not the trimmings.

### 3.12 The concurrency caps — 3 soft per session, 5 hard globally

**The rule.** No more than 3 concurrent jobs out of one warm session; no more than 5 in total. The 3 is
a soft cap, so the owner keeps room for their own desktop sessions; the 5 is the hard cap. At 5, with an
urgent job waiting, the owner is shown the currently running jobs in recommended kill order and
decides.

**Both caps have bitten.** A sweep over `started_at`..`ended_at` across the ledger shows nights where
a single warm session alone ran five concurrent jobs, and nights where two sessions together exceeded
the global five — so neither cap is hypothetical.

**Why it is code.** A rule in a grounding prompt is a rule the next turn may simply not apply. And the
caps would otherwise have to be duplicated into **two** prompts: `presence.py`'s `GROUNDING` and the
`/assistant` command. A gate in `jobs.py` binds whichever prompt typed the command. Nothing was added to
either prompt, on purpose: the refusal arrives in under a second with nothing spawned and says
everything a prompt could have, and a sentence in `GROUNDING` would be the second source of truth that
goes stale first.

#### The two caps are different kinds of limit

| | soft | hard |
|---|---|---|
| number | 3 | 5 |
| population | agent jobs from one `origin.session_id` | agent jobs from every origin (below) |
| what it protects | **the owner's headroom** — the concurrency they want left for their own desktop sessions | the ceiling |
| override | `--over-soft-cap`, stamped on the record | **none, and none should be added** |

The 3 is a claim about what the owner wants available, not about what the machine can carry, and
overrunning it can be right: an urgent data-loss fix as a fourth job is the correct call. So it yields
to judgement. The 5 does not yield at all — an escape hatch there would make the two caps one cap with
two spellings.

**Both are refusals, not warnings — stop-with-override, decided and closed.** **A later edit that moves
this toward warn-and-continue is reversing a decision, not tidying an implementation detail.**

`jobs.py` runs both caps *before* the record is written and returns **exit 2 with nothing spawned** —
`hard_cap_refusal()` first, then `soft_cap_refusal()` unless `--over-soft-cap` was passed;
`over_soft_cap_note()` stamps the record only when the cap was genuinely overrun.

A cap that printed a warning and started the job anyway would be a prose rule with a compile step:
nothing about it can refuse. The difference between them is what gets past, not whether anything is
stopped. `--over-soft-cap` is one flag; the overrun is recorded (`rec["over_soft_cap"]`, **absent
rather than false** when the flag was passed below the cap, like `retry`/`analyze`/`worktree`), so how
often the soft cap is overrun is a measurement rather than a memory.

#### The refusal at 5 is the feature, not the cap

A gate that only says no hands the reconstruction work back to the owner, and **deciding is the
expensive part, not typing**. So the refusal prints the in-flight set ranked by recommended kill order:
rank, id, age, title, the signals that placed it, its goal line, and the `jobs.py cancel` command that
acts on that row. The owner picks. **Nothing here ever cancels anything.**

**The ranking is a lexicographic sort, not a score.** A weighted score invents magnitudes ("a violation
is worth 30, a wake waiter −15") that nobody can defend and that silently trade one signal against
another. A tuple claims only an *order*, which is the only thing argued. Five keys, most-killable first,
every one read off a field already on the record:

1. **`status == retry-pending`** — nothing is running. A job in its backoff has no child process at all
   (§7), so cancelling it discards no in-flight work *by construction*. Cheapest on evidence, not taste.
2. **`reason_class == floated-idea`** — the module's own word for a violation, not a category
   (`job-origin-routing-spec.md` §3.5.7): the owner was thinking out loud and it became a brief. A job
   that should not exist is next.
3. **Who asked** — `assistant` > `unresolved` > `owner`. Rung 3 is a *positive* claim that the
   assistant started it itself. `unresolved` sits between them honestly: it **might** be the
   assistant's.
4. **Is a session waiting on this result** — `--wake` / `--lease`. Below "who asked", and the reason is
   checkable rather than felt: **`--wake`'s own contract is that the push fires either way** (its help
   text; §2's table — `cancelled` notifies like every terminal state), so cancelling a waited-on job
   costs the re-entry, never the report.
5. **Youngest first** — least work discarded. Last, so it only breaks ties inside a band.

**Deliberately not keys: the title and the goal line.** They are prose, and a keyword list deciding that
`fix` outranks `feat` is exactly the invented score this avoids. They are **printed** on every row,
because they are what makes the list decidable by the one reader who can weigh them.

**Honest about homogeneous data:** when every in-flight job is owner-asked, `--wake`, and running, keys
1–4 all tie and **age alone decides the order**. The ranking is right and the data was homogeneous; a
richer order is available when the in-flight set is mixed.

#### What counts as in flight

**Not `list_jobs(active_only=True)`, and the difference is the gate's whole reliability.** `active_only`
reads the *status field*, which the reconcile pass writes; a job whose shim died at 03:00 reads `running`
on disk until a tick notices. Counting those would let a dead job refuse live work for reasons its owner
cannot see — which is how a cap gets deleted rather than obeyed. So `in_flight()` drops a record when the
module can already **prove** it is over, using the same two side-effect-free predicates `reconcile` uses
for the same decision: `check_terminal` (deadline elapsed / no PID past the startup grace / PID gone) and
`check_retry_due` returning `expired`. **Nothing is written** — `start` must not transition another job's
record as a side effect of counting itself in.

**Polarity, named:** proof-of-over drops, everything else counts. `pid_alive` is conservative, so the
count errs toward *full* — the same direction `reconcile` errs and the opposite of most of this module,
which is right for a cap whose refusal is one flag or one `cancel` away. Residual, said out loud: a
recycled PID number counts as its old job.

#### Only jobs that make agent calls count

Only jobs that include agent calls (local, subscription, or API) count against the caps. Counting every
in-flight job would let a transcription, a download, a gradle build or a print watcher spend a slot the
owner's desktop sessions need — and the headroom both caps exist to protect is **agent** concurrency.

**Both caps, both populations.** `concurrency()` takes the live set from `in_flight()` and keeps only the
records `counts_toward_cap()` accepts; the global and per-session numbers are both taken over that
filtered set, and a refusal ranks only counted jobs. The non-agent remainder is returned as `uncounted`
and the refusal says how many are running — they are not hidden, just not kill candidates. A NEW job that
is itself non-agent is checked against neither cap at the CLI: starting it adds nothing to either count.

**Classification (`job_agent_class`) is read off the record as launched**, in this order:

1. **The explicit stamp.** `start --agent` / `--no-agent` writes `agent: true|false` on the record
   (**absent** when neither was passed, like `over_soft_cap`), and it wins both ways — the caller knows
   what the heuristic is only guessing.
2. **Agent evidence** — `claude` (or `ollama`/`codex`/`gemini`) as the command, or a script known to call
   a model (`fable_delegate.py`, `job_analysis.py`, the RAG/router scripts). In practice most jobs are
   `claude`/`claude.exe`.
3. **Non-agent evidence** — a command or repo script known to make no model call (`gradlew`, `ffmpeg`,
   `yt-dlp`, `whisper`, the PR watcher, `python -m unittest`/`pytest`, …). Then, only as a label, a
   basename naming a model provider reads `agent`.
4. **Everything else is `unknown`, and unknown COUNTS — fail closed.** A wrapper (`pwsh -File x.ps1`,
   `bash x.sh`, `wsl … bash …`, `cmd /c`, `python -c`) hides what it runs. Treating an opaque job as free
   is how an agent job slips past the cap unseen, so an over-count is the cheaper error: it costs one
   `--no-agent` on the next start.

#### Where the gate lives, and what it must never block

**At the CLI, not in `start_job`** — `preflight_refusal`'s placement and its reason. `request_analysis`
and the retry respawn both build their argv in code, and neither may be blockable: **an analysis eaten by
a cap is §3.11's failure exactly** — the fresh eyes a job asked for, silently cancelled — and a retry
respawn is not a new job at all, its record already exists and was already counted when it started.

Order within the CLI: **preflight first, caps second.** A mangled argv is wrong whatever the load is, and
telling the owner to cancel a live job to make room for one that would die in a second is worse advice
than none. Both exit **2** with **no record written**: a refused job never started, so it must not read
as failed.

**Pinned by** `test_jobs.py::ConcurrencyCapTests` — the two caps as separate predicates over different
populations, the override path and its record stamp, the ranked refusal's order and its cancel lines, the
stale-record exclusions, and the two things that must stay unblockable; and by
`test_jobs.py::AgentOnlyCapTests` — a non-agent job counts toward neither cap, a `claude` job does, the
explicit flag overrides the heuristic both ways, and an unknown shape counts.

### 3.13 The job-completion guarantee — detect, resume, rescue

**"Terminal" was never the same claim as "the work landed."** A job's own `claude -p` agent can launch a
verification step with `run_in_background` and then end its turn to wait for a completion notification
— but `claude -p` is a single-prompt process, so it dies the instant the turn ends, and that
notification can never arrive. The job still exits 0. Observed shapes: a PR opened but the agent's own
report never reached the log; a commit made, never pushed, no PR; hundreds of lines staged and never
committed. Each was recovered by a human noticing. Noticing is not a mechanism — §1's exact argument
about `state/metrics.jsonl`, one layer up.

**The fix is not "keep the process alive."** `claude -p` is meant for a single prompt, and nothing here
holds a `-p` process open past its turn. Instead, `jobs.reconcile`'s existing tick — already the
mechanism for exactly-once notification — notices what the process left behind, before it is allowed to
report `done` as a plain success. A mechanism OUTSIDE the turn, never a rule inside a brief — a prose
instruction to "commit before you finish" is precisely the shape that does not hold.

**Scoped to a job-specific working directory, deliberately.** The default `cwd` for a job with neither
`--worktree` nor `--cwd` is the daemon's own live checkout (`REPO_ROOT`) — shared by every such job.
Running DETECT there unconditionally would attribute whatever happened to be dirty in that shared tree,
at that exact instant, to whichever unrelated job (say, a PR watcher) happened to finish next. So the
guarantee only ever runs for a job that has its own `--worktree`, or an explicit `--cwd` different from
the shared root (`job_completion.is_scoped_cwd`) — the failures above are all the `--worktree` shape
recommended for delegated coding work, so this excludes nothing the feature exists for.

**Layer 1 — DETECT** (`job_completion.detect_incomplete`). Before a job that just reached `done` or
`ended-unknown` is allowed to notify as such, its cwd is inspected for: staged-but-uncommitted changes,
unstaged changes to tracked files, commits on the current branch never pushed to `origin` (checked via
`git ls-remote`, never a possibly-stale local remote-tracking ref), and a fully-pushed branch with no
open PR (`gh pr list`, behind its own fail-open seam). **Every condition is additive evidence, never a
default suspicion** — a job that legitimately produced no changes is COMPLETE, not incomplete, and an
unreadable git/`gh` read skips that one condition rather than flagging on ambiguous evidence.
**"Unpushed" means unreachable from every remote-tracking ref, detached or not**
(`remote_refs_containing_head` — the plumbing form of `git branch -r --contains HEAD`), never merely
"ahead of `origin/develop`": a job run `--cwd` in a worktree detached at a commit that was already a
remote branch's tip, exiting 0 having touched nothing, was otherwise flagged `unpushed-commits` off the
ahead-of-base count alone — and RESCUE then pushed `rescue/<id>` at a commit origin already had and
marked it FAILED. RESCUE now asks the same question before its push and declines (`already_remote` on
the outcome) rather than duplicate a remote commit under a second name.

**Layer 2 — RESUME** (`job_completion.can_resume` / `resume_argv` / `build_resume_prompt`). A bare
single-prompt `claude -p` invocation gets a `--session-id <uuid>` injected up front, at `start_job` time
(`job_completion.prepare_argv`) — the only way to have an id to `--resume` later, since there is no way
to learn one from outside a process once it has already started (`--session-id` sets a session up
front, `--resume` continues it, both compose with `-p`). An incomplete job with a captured id is handed
back to that exact session — `claude --resume <id> <carried flags...> -p "<prompt>"` — via
`jobs.reconcile`'s existing `_spawn_attempt` (a resume is spawned exactly like a fresh attempt, so
detached survival, the deadline and the kill machinery all apply unmodified). **The carried flags
(`job_completion._carried_flags`):** `--permission-mode`, `--model`, `--allowedTools`/`--disallowedTools`,
`--dangerously-skip-permissions`, `--mcp-config`, `--add-dir`, `--settings`, `--append-system-prompt` —
each checked present in `claude --help` before being added — read from `rec["original_argv"]` if present
else `rec["argv"]`, so a second resume still sees the true original launch rather than the first
resume's own already-carried argv. Without them a resumed session silently reverts to the CLI's default
permission mode: a job launched under `--permission-mode bypassPermissions` whose resume dropped that
flag had `git push`/`gh`/`python` all refused with no human present to approve them, and failed with its
work stranded committed-but-unpushed. `jobs.resume_permission_mismatch` names it, in the completion push
and in `list`/`status`, if a resumed attempt's `--permission-mode` ever again diverges from the original
launch's. The prompt is a **module constant** (`RESUME_PROMPT_TEMPLATE`), never caller-supplied text —
it says exactly what was found and what to do (commit, push, open the PR, never background anything),
because the agent being resumed is the one that just made that exact mistake. Bounded at
`MAX_RESUME_ATTEMPTS` (2); a job with no captured session id, or one already at the bound, cannot be
resumed and falls straight to RESCUE; a cancelled job is never resumed (in practice this never arises,
since the guarantee only ever runs for a success-like status a cancel can't be).

**Layer 3 — RESCUE** (`job_completion.rescue`). Resume exhausted or never possible: commit whatever is
staged/dirty and push `HEAD` to `origin` under `rescue/<job-id>` — a NEW ref name, never the branch's
own, so a plain (non-`--force`) push refuses outright rather than silently winning a race against
history already sitting there. **Never a PR from a rescue branch** — inventing a merge is not what this
exists to do. **Never on `develop`/`master` in a shared checkout** — a job that ran directly in the
daemon's own live tree (no `--worktree`) on the protected branch is refused rescue outright rather than
auto-committing onto the branch the daemon deploys from; the rule for that checkout is "don't leave
tracked files dirty here," and an automated commit there would be the guarantee violating the rule it
exists to protect everywhere else. The job is then reported `FAILED`, naming the rescue branch — or, if
rescue itself could not run, saying why.

**What the completion push says.** A rescued job's head line replaces the ordinary `done`/`failed`
wording entirely (`jobs.notify_text`) — "exited 0 but never actually finished" rather than a bare
"failed (exit 0)," which read alone is a contradiction, not an answer; a job that needed resuming but
eventually landed keeps its ordinary "✅ Finished" head line and gains one added line naming how many
extra turns it took. `rec["completion"]` carries `checked_at`, `reasons`, `resume_attempts` and (on
rescue) the outcome, on every job the guarantee evaluated — including the ordinary case, so "did DETECT
run for this job" is answerable from the record alone, not just from what the push happened to say.

**Honesty about what is tested, since a fake that proves nothing is worse than an honest gap.** DETECT
and RESCUE are exercised in `test_job_completion.py` against **real temporary git repositories** — this
module's design leans on non-obvious real git behaviour (`git worktree remove` succeeds even when the
worktree's branch holds committed-but-unpushed commits, because those commits stay reachable via the
branch ref in the shared object store — verified against real git), and a canned `CompletedProcess`
would need to already know the answer this suite exists to pin down. `gh pr list` is the one exception,
behind its own injectable seam, always faked (no network/auth in CI). **RESUME cannot be exercised end
to end** — spawning a real `claude` process from a test is not feasible here — so only its DECISION
logic is covered (bounding, prompt/argv construction, `prepare_argv`'s injection); the actual respawn
rides `jobs.reconcile`'s existing, separately-tested `_spawn_attempt` path, unmodified by this feature,
and is verified in `test_jobs.py`'s `JobCompletionGuaranteeTests` with a faked spawn.

**Named residuals.** A job that both opts into `--retry` and needs a resume gets `_run_shim`'s retry
bookkeeping re-evaluated on the resumed attempt (its `retry` config is still on the record) — two
independently-designed safety nets composing rather than one subordinating the other, not entangled
further given how rare a retry-configured coding job is. `_run_shim`'s per-attempt numbering and its log
delimiter are both gated on `retry` config, so a resumed attempt on a plain job still stamps
`"attempt": 1` in every `attempts[]` entry and appends to the log with no separator from the prior run's
output — `completion.resume_attempts`, not `attempts[]`, is the authoritative count. Full argument:
`job_completion.py`'s own docstring.

### 3.14 The preventive rail — `job_background_guard.py`

**§3.13 is reactive: it cleans up after a job backgrounds a step and ends its turn before the result
can land.** The obvious alternative — "add an explicit no-background-waiting rule to the standard job
brief" — is exactly the prose-inside-the-brief shape `job_completion.py`'s docstring already argues
against for this same failure. A sentence in a brief is not a mechanism.

**So §3.14 is the other half: a `PreToolUse` hook (`job_background_guard.py`) that refuses the one Bash
call that causes the failure, before it ever runs**, rather than diagnosing the wreckage afterward. It
fires only when its own process carries `SENESCHAL_JOB_ID` — an env var `jobs.child_env` stamps on both
the shim (`_spawn_attempt`) and the job's real command (`_run_shim`), so it reaches every Bash tool call
the job's own `claude -p` session makes, and is absent — a complete no-op — on every ordinary
interactive session, where `run_in_background` is genuinely safe (there is a next turn to receive the
notification on).

**Neither layer retires the other.** The hook cannot help a job launched before it existed on the host
(installing it is a `~/.claude/settings.json` entry, `../scripts/JOB_BACKGROUND_GUARD_SETUP.md`, the
same as every other guard hook), or one whose brief finds some other way to strand work with no
backgrounded Bash call involved at all. §3.13's DETECT/RESUME/RESCUE has no opinion about *why* work was
left uncommitted and keeps running regardless. Two independently verifiable safety nets, the same
posture §3.13's own residuals section already accepts for retry-vs-resume composing.

Tests: `test_job_background_guard.py` (the hook's own decision logic, fail-open contract, and import
weight) + `test_jobs.py` (`child_env`'s `SENESCHAL_JOB_ID` stamp, both call sites).

## 4. First consumer — a PR CI watcher

A PR watcher polls `gh pr view <n> --json statusCheckRollup` (the same call Dream's merge step uses) to
a terminal verdict, printing a summary line that the push quotes back. Exit **0** all green · **1** some
check failed · **2** still pending at the deadline · **3** the PR couldn't be read.

```bash
python seneschal/scripts/jobs.py start --title "CI: PR #214" --wake -- \
  python seneschal/scripts/watch_pr.py 214 --repo <owner>/<repo>
```

`--repo` is required: a PR number is not an identity, and the watcher never infers a repository.

**It never merges.** The standing rule — never merge a red or pending PR, no self-waiving — is
untouched; this only watches and reports. Pending is never green: unknown fails closed, the same rule
Dream's merge discipline uses.

## 5. What this does NOT do

- **It doesn't make the warm session long-lived.** Per-turn cost scales linearly with context, so the
  warm session stays short-lived. This gets the durability without the context bill.
- **It doesn't bypass the approval gate.** A job is act-low to *start*; whatever it produces still rides
  the gate if it's outbound or destructive.
- **It doesn't schedule, queue or throttle.** §3.12's caps REFUSE at the ceiling; they do not hold a job
  back and start it later. A queue would need an owner for "when does the held job run, and who is told
  if it never does," which is the whole of §1 again one layer up — so the answer is a refusal the owner
  can act on in one command, not a wait they cannot see.
- **It doesn't supervise the daemon.** That's the daemon's own revive path + the `seneschald-update`
  health checks.
- **Its cockpit surface is read-only** — the Jobs panel (§8).

## 7. Transient-failure retry

**The failure.** The model API throws intermittent `500 Internal server error` for hours at a time. A
detached job that called `claude -p` died **one second in**, with a few hundred bytes in its log and
nothing else:

```
API Error: 500 Internal server error. This is a server-side issue, usually temporary — try again in a moment.
```

Nothing was built; the whole job was lost to a blip. The design goal: absorb the random API failures.
The desktop app retries up to 10 times; this runner retried zero. That gap is squarely in this module's
remit — §1's argument is that a promise the assistant makes should be kept **in code**, and a job that
evaporates on a transient 500 breaks that promise exactly as surely as one that never notifies.

### 7.1 The flags — opt-in, off by default

| Flag | Effect |
|---|---|
| `--retry N` | up to N **extra** attempts, capped at `RETRY_MAX_RETRIES_CAP` (10, matching the desktop app) |
| `--retry-backoff` | exponential + jitter: 30 s doubling, capped at 10 min, ±25%. Without it, a constant 30 s |
| `--retry-window <dur>` | stop retrying this long after the job was **created**, whatever N says (`90s` / `45m` / `2h` / `1h30m`) |

No `--retry` ⇒ **the pre-retry *behaviour*, exactly**: no `retry` block on the record, no delimiter in
the log, one attempt, one push, terminal on the first failure. That is deliberate and load-bearing:
every existing caller (the PR watcher, scheduled drains, bench runs) is untouched, so this change can
only affect work that explicitly asked for it.

What a non-retry job *does* get is the **record** — see §7.4.1. Recording is not retrying.

`--deadline-sec` bounds **each attempt** (each gets the full deadline, since `started_at` is re-stamped
per attempt); `--retry-window` is the only whole-job wall-clock bound. `LEASE_MAX_SEC` is measured from
`created_at` for the same reason — otherwise a retrying job would renew its 30-minute session lease
indefinitely.

### 7.2 The classifier — the crux

**A blanket "exit != 0 ⇒ retry" is wrong and does not ship.** A genuinely broken command would run N
times and repeat its damage N times. Retry fires *only* on a recognised transient signature in that
attempt's captured output, from one named, documented, easily-extended constant —
`jobs.TRANSIENT_SIGNATURES`:

- **the model API:** `API Error: 5xx` / `API Error: 429`, the verbatim "server-side issue, usually
  temporary" line, `overloaded_error`, `rate_limit_error` / "rate limited", `HTTP 5xx`, `502 Bad
  Gateway`, `503 Service Unavailable`, gateway timeout, `429 Too Many Requests`
- **the transport layer:** connection reset / aborted, `RemoteDisconnected` / socket hang up, `fetch
  failed`, DNS (`ENOTFOUND` / `EAI_AGAIN` / "temporary failure in name resolution"), TLS handshake
  failures, `ETIMEDOUT` and socket/read/connect timeouts, `APIConnectionError`

> **Anything not on that list is TERMINAL, first time, no retry.** A test failure, a syntax error, a
> non-zero exit with unrecognised output, an exit that printed nothing — all final, all pushed
> immediately, exactly as before.

So are the three outcomes that aren't a readable failure: **`timed-out`** (the command was too slow;
retrying makes that strictly worse), **`ended-unknown`** and **`cancelled`** (no readable outcome to
classify — "transient" there would be a guess that re-runs damage we cannot even see). A transient
signature sitting in the log does not change that, and there is a test for each.

Every pattern carries its own context, because the obvious loose versions match this repo's own output:
a bare `\b429\b` matches "429 tests passed", a bare `timeout` matches "test timed out after 5s", a bare
`rate.?limit` matches `references/notion-rate-limits.md`, and `assert response.status == 500` is a test
asserting on a 5xx, not suffering one. `test_the_deliberate_exclusions_stay_terminal` pins all of them,
so a future widening has to argue with a red test first. Deliberately excluded with reasons in the
constant's own comment: `connection refused` (usually a local service that is genuinely down), bare
`timeout`, bare `internal server error`.

**Speed corroborates; it never decides.** The real failure was 1 s and a few hundred bytes, so an
attempt that dies inside `FAST_FAIL_SEC` (15 s) with under `FAST_FAIL_BYTES` (4 KB) of output is
recorded as `fast_fail: true` and lifts `confidence` from `medium` to `high` — but a fast failure with
no signature classifies **terminal**, because a broken command also fails fast (a syntax error is
instant).

**Each attempt is classified on its OWN output.** The attempt records the log byte offset it began at,
and `read_log_span` reads from there. Without that, attempt 1's `API Error: 500` would still be sitting
in the shared log when attempt 2 failed for a real reason, and a genuinely broken command would be
retried off a stale signature. That is `test_each_attempt_is_classified_on_its_OWN_output`.

#### 7.2.1 …and from the FAILURE END of that attempt, not from its first byte

The offset above closes the *multi-attempt* half of "stale signature" and does nothing for the other
half, which is the one that actually fired: **one attempt can span hours and several phases.**
`read_log_span` takes the tail of a span — but only once the span exceeds its `limit`, and with that
limit at 64 KiB almost no attempt does, so in practice the classifier read from byte 0.

The observed case: a ~30 KB log, therefore read whole, whose single transient match sat about 1 KB in —

```
[remote] ssh: connect to host <host> port <port>: Connection timed out
```

— a provisioning-phase timeout the job **recovered from and then ran for well over an hour more**. It
died on a later step, with nothing transient anywhere near the end. The record read
`classification: transient`, `signature: transport-timeout`, `confidence: medium`. That is not
cosmetic: §7's `--retry` decides whether to re-run a command off exactly that field, so a stale match
is a re-run of something that did not fail transiently at all.

**The fix is WHERE, not WHAT.** The classifier reads `_CLASSIFY_TAIL_BYTES` (8 KiB — `log_tail_line`'s
window, this module's existing definition of "the end of the log") with the attempt's own `log_offset`
still the floor. `TRANSIENT_SIGNATURES` is untouched; narrowing the window can only ever make FEWER
things transient, which is this section's posture in the first place. A signature more than 8 KiB above
the end of a failing attempt is now missed and classifies **terminal**, which is the answer §7.2 already
gives to everything it does not recognise.

**`fast_fail` moved with it, in the opposite direction.** `FAST_FAIL_BYTES` asks whether the attempt
printed almost nothing *at all*; measured off the bounded window it would have become "the last 4 KB I
bothered to read", true of every long job. It is measured over the whole attempt span (`log_span_bytes`)
so the corroborator keeps meaning what it says.

**The residual limit, named rather than papered over: the bound is BYTES, not time.** A quiet attempt
whose entire output is under 8 KiB is still classified on all of it, however many hours it spans. There
is no per-phase marker in a job's log to anchor on, and inventing one would mean parsing every
producer's prose. Pinned by `test_a_stale_transient_line_EARLIER_IN_THE_SAME_ATTEMPT_stays_terminal`,
which asserts both halves: the stale line is terminal, and **the same line at the failure end still
retries.**

### 7.3 Idempotency — the quiet part, said out loud

**`--retry` is the caller's assertion that the command is safe to run again from scratch.** A retry
re-runs the whole argv. `jobs.py` cannot know whether attempt 1 got far enough to create a branch, open
a PR or send a mail before it died, and it does not try to — there is no general solution here and
inventing a half one would be worse than naming the limit. The limit is named in `jobs.py start --help`,
in `--retry`'s own help string, in the module docstring, and here.

This is safe to leave unsolved *because it is opt-in*: nothing retries unless asked, so nothing
existing can be duplicated. Use `--retry` for idempotent or trivially re-doable work — a model one-shot,
a poll, a read-only build — and not for anything that mutates the world on its way through.

### 7.4 The record, the log, the push

**The record is the durable thing** — a reader must be able to reconstruct what happened without the
log. Each attempt appends to `attempts[]`: `attempt`, `started_at`, `ended_at`, `duration_sec`,
`exit_code`, `outcome`, `retry_enabled`, `classification`, `signature`, `fast_fail`, `confidence`, the
`decision` (`transient-signature` / `no-transient-signature` / `retry-not-enabled` /
`retries-exhausted` / `retry-window-exhausted` / `non-retryable-outcome:<status>`), `log_offset`, and
`next_attempt_at`. The retry BOOKKEEPING is the part that stays opt-in, at the top level: `retry` (the
config), `attempt` (which one is current), `next_attempt_at` while pending, and `retry_outcome`
(`succeeded-after-retry` / `exhausted` / `window-expired` / `not-transient` / `spawn-failed`) are
written only for a job that asked for `--retry`, so a plain record never implies a policy it doesn't
have.

#### 7.4.1 The classification is recorded whether or not retry is on

`attempts[]` is written for **every** job — length 1 for a plain one — and every terminal failure
carries its transient/terminal verdict plus `retry_enabled: false`. (The first cut wrote `attempts[]`
only for retry-enabled jobs, to keep plain records byte-for-byte unchanged; that was reversed.)

The argument is §1's, turned on this feature: the classification is exactly the diagnostic that tells
you **which jobs should have `--retry` in the first place**, and gathering it only from jobs that
already opted in collects the data strictly *after* the decision it was supposed to inform. That is the
`state/metrics.jsonl` shape — a contract specified only where it was already being honoured, which
produced zero rows in a month. Not reproduced here.

**It changes nothing about what jobs do.** A job without `--retry` still fails terminally, the first
time, on any outcome; `plan_retry` classifies *before* it checks the config and then returns
`retry: false, reason: "retry-not-enabled"` regardless of the verdict. There is still exactly one
classifier — the shim's non-retry path was merged into the retry one rather than forked, and
`test_the_classifier_is_not_forked_for_the_non_retry_path` goes red if a second copy ever appears.

So the question that motivated it is answerable from `state/jobs/` alone:

```bash
jq -r 'select(.attempts[]? | .classification == "transient" and .retry_enabled == false) | .id' \
   seneschal/state/jobs/*.json
```

`jobs.py status <id>` prints the same thing per attempt. `cockpit/server/jobs.py` is unaffected — it
never read `attempts[]` (§7.6), so an extra key on disk changes nothing there.

**The log stays readable**: every attempt of a retry-enabled job is prefixed
`===== attempt 2/4 · <ISO> =====`, so N attempts are distinguishable rather than blindly appended into
one stream — and a plain job's log gets no delimiter at all, keeping the shape it always had.

**One push, at the terminal outcome, naming the attempt count.** `retry-pending` isn't terminal, so a
retrying job pushes nothing; the single push says *"Took 3 attempts of 4 — I absorbed 2 transient
failures getting there. [api-5xx]"*, so a job that succeeded on try 4 never reads as a clean first-try
success. A job that runs out of retries reports as **`failed`** and says *"Gave up after 4 attempts"*;
one whose window closes says so, **including when only one attempt ran** (a backoff that would land
outside the window is refused, and that decision is reported rather than hidden). The duration quoted
for a retried job is the whole job's, not the last attempt's. `--wake`'s synthetic inbound carries the
count too, so the woken session reports honestly instead of describing try 4 as the only one.

### 7.5 Why a pending retry survives a restart

**The wait is a record, not a `sleep`.** This is the part most likely to be got wrong, and it is
deliberately built out of the same two pieces §3.3 already relies on:

1. The shim **classifies and schedules, then exits** — it stamps `retry-pending` + `next_attempt_at` and
   dies. No process is holding the backoff, so nothing is there for a wind-down, a turn error or a merge
   reload to kill.
2. `reconcile` — the daemon's ~5 s tick — **launches the attempt that has come due**, via the same
   `_spawn_attempt` that `start_job` uses (shared on purpose: a second copy of the detach flags, the
   API-key scrub and the log-handle dance is exactly where the two would drift, and the safety argument
   is that a retried attempt is spawned *identically* to a first one).

So a daemon that restarts mid-backoff loses nothing: the successor reads the same record off disk and
carries on, which is the identical mechanism that already makes the completion push survive a restart.
`test_a_pending_retry_survives_a_daemon_restart` drives exactly that — nothing in memory, every input
re-read from `state/jobs/`, the old shim's PID gone.

Three loose ends closed for the same "no silent path" reason: a **failed respawn** ends the job as
`failed` (rather than leaving it pending forever), an **unreadable `next_attempt_at`** reads as *due* (a
garbled timestamp costs the backoff, never the job), and a `retry-pending` job is **never pruned** and
still counts as **active** for `list --active`, the lease guard and the cockpit's `jobs_active`.

### 7.6 What §7 deliberately does not do

- **No general idempotency protection.** See §7.3 — named, not solved.
- **No retry for `timed-out` / `ended-unknown` / `cancelled`.** See §7.2.
- **No retry default anywhere.** Not for the PR watcher, not for scheduled drains, not for bench runs.
  Whether a caller opts in is a per-caller decision, and each is a one-line change when someone wants to
  make it.
- **No cockpit surface.** `cockpit/server/jobs.py` is a deliberately independent reader of the on-disk
  shape (`cockpit-spec.md`) and tolerates the new status as an unknown one, so nothing 500s — but a
  `retry-pending` job currently sorts into the panel's "recently finished" list rather than its active
  one, and the `attempts` array isn't surfaced. That is the obvious follow-up, on the cockpit's side of
  the boundary.

## 8. Follow-ups

- **Cockpit Jobs panel — shipped:** `GET /api/jobs` + `GET /api/jobs/{job_id}` over `state/jobs/`
  (degrading to `available: false`), a read-only `JobsPanel` that alarms only on `awaiting_push`, and
  `jobs_active` on `_status_snapshot` so the count rides the existing pipe `status` frame with no
  protocol change. Spec: `cockpit-spec.md` → "Jobs panel".
- **Cockpit surface for retry** — see §7.6: `retry-pending` should sort as active, and the `attempts`
  array (with each attempt's classification and signature) is the interesting thing to show on a job
  that took four goes. Cockpit-side change; nothing in `jobs.py` blocks it. Since §7.4.1 every job
  carries `attempts[]`, so the panel would have something to show on a plain job too — namely "this died
  on an API 500 with no retry policy."
- **Read the transient-without-retry data back.** §7.4.1 collects it; once a few weeks of `state/jobs/`
  have accumulated, the `jq` in §7.4.1 says which callers should opt into `--retry` — that is the
  decision the recording exists to inform, and leaving it collected-but-unread would be the same mistake
  one step later.
- **Still open:** the live daemon's restart-mid-job adoption path is unit-tested but not yet exercised
  against a real daemon reload with a job in flight. `--wake` and `--lease` are likewise tested but not
  yet live-exercised. The retry loop *has* been smoke-tested end to end against real detached processes
  (three real attempts, two transient failures absorbed, one push naming the count) — but not against a
  live daemon's own tick.

## Router entry

**Status:** PARTIAL(BUILT + §3.7-§3.14).

**What it decides:** Detached jobs the daemon pushes on every terminal state; the opt-in transient retry
and why unrecognised output is terminal first time. **§3.8: "a cancel loses the log" was really "you
can't watch a running job"**. **§3.9: jobs died in seconds off a script path the LAUNCHING SHELL had
mangled — a prose rule was already in place and prevented neither, so the guard is a shape, not
another rule**. **§3.10: a cancel came back `failed`, exit 1 — the kill is what wakes the shim, so
killing before saving handed the other writer of the same unlocked record a head start. Claim before
kill, plus a sticky status at write time; and `cancelled` is not `ANALYSABLE`**. **§3.11: a job pushed
✅ with `launch failed:` three lines from the end of its own log, and the `--analyze` it asked for was
silently lost with it — "exactly once" held, "honestly" did not. The code was lost in a wrapper outside
this repo that means its `exit 0`, so `failed` is not this module's to say: `ended-unknown`, and the
WINDOW (exit 0 · this attempt's last 5 lines · column 0) is the guard, not the pattern. §7.2.1: the
attempt offset closes only the multi-attempt half — ONE attempt spans hours, so a log was classified
off a signature near its start that the job had long outrun. WHERE, not WHAT**. **§3.9.1: the
preflight checked the path FROM WINDOWS while the command was about to ask WSL, so a job died at exit
127 in one second on a script that WAS THERE. The namespace is evidence (System32 bash is WSL's; a
Git-for-Windows one reads `C:/…` fine), UNKNOWN refuses nothing, and the argv is never rewritten**.
**§3.12: 3 SOFT per warm session, 5 HARD globally, as a gate that can REFUSE. The 3 is the owner's
desktop headroom, so it yields to one flag (`--over-soft-cap`, stamped on the record); the 5 yields to
nothing. THE REFUSAL AT 5 IS THE FEATURE: the in-flight set RANKED BY KILL ORDER, each row's signals and
its `cancel` line — a lexicographic sort, never a score, titles/goals printed but never ranked on. ONLY
AGENT JOBS COUNT (`--agent`/`--no-agent` stamp first, then the argv; an unrecognised shape counts —
fail closed)**. **§3.13: "terminal" was never "landed" — a job's own `claude -p` agent backgrounded a
check and ended its turn to wait for a result that process death made unreachable. DETECT inspects a
job-specific cwd (never the shared `REPO_ROOT` — that would blame one job for another's mess) for
uncommitted/unpushed/PR-less work; RESUME hands it back to its own `--session-id`-captured session with
a fixed prompt, bounded; RESCUE — exhausted or impossible — commits onto `rescue/<job-id>` and pushes,
never forced, never a PR from it, never onto `develop`/`master`. DETECT/RESCUE tested against real git;
RESUME's respawn is not (spawning real `claude` isn't feasible in a test), only its bounded decision
logic is**. **§3.14: a prose rule in the job brief is the shape that fails, so instead a `PreToolUse`
hook refuses the backgrounded Bash call itself, scoped to `SENESCHAL_JOB_ID` (`jobs.child_env` stamps it
on the job's real command), a no-op in every ordinary session where backgrounding is genuinely safe; it
complements §3.13 rather than replacing it**.
