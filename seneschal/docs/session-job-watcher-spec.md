# Spec — the session that started the job, and how it learns the job ended

**Status:** `SPEC-ONLY` — nothing built, no behaviour changed. `jobs.py`, `presence.py` and every other
script are untouched by this spec.

**Scope (if built):** one new `jobs.py` subcommand (`wait`), one instruction in `seneschal/SKILL.md` /
`seneschal/modes/chat.md`, one manual probe script in `seneschal/scripts/`, and — optionally — one line
in the `/assistant` command file. No new dependency. No new process. **No change to the completion
push, ever.**

**Parent:** [`job-origin-routing-spec.md`](job-origin-routing-spec.md) — this closes the "Honest
limitation" paragraph at the end of its §3.3. Read that first.

**Machines this sits on and must not contradict:**
[`background-jobs-spec.md`](background-jobs-spec.md) §2 (*no silent path* — the invariant this may
only ever add to), [`job-origin-routing-spec.md`](job-origin-routing-spec.md) §1.1 + §3.3 (there is
no inbound channel to a non-daemon session; the delivery ladder),
[`session-coupling-spec.md`](session-coupling-spec.md) §1.1 + §2.3 (whether anyone is *waiting*; the
measured job-duration distribution this spec's cost table is built on), the probes of the `claude`
CLI's stdin control protocol behind the mid-turn interleave design (the evidence that closes
mechanism C), and the scripts' house rules (stdlib-first; `state/` writers fail open; never
truncate-write).

**The ask.** Desktop sessions now know to *check* their mail at orientation, but they stop checking
once they start a job. The owner asked for a durable watcher: either something that wakes the session
every so often to check the job's status, or the daemon waking the session back up *inside* Claude
Code — if that is even possible.

**The short answer, up front, because it changes what the rest of this document is for.** The
either/or resolves cleanly and neither branch is quite the shape it was guessed to be:

- **The daemon cannot wake a desktop session. Determined, not hedged** — four independent pieces of
  evidence in §4.3. That half is closed.
- **The watcher is the real answer, but it should not be a poller.** A poller wakes the session every
  X minutes to ask a question whose answer is almost always "no" — and every one of those wakeups is
  a billed model turn (§5's table: up to **72 turns** to observe one boolean). The same harness
  affordance that makes a poller possible also makes a **blocking waiter** possible, which costs
  **exactly one turn, at the right moment**. §4.2 is that mechanism and it is the recommendation.
- **And the honest framing, which partly argues this feature down:** the problem is small. Nothing is
  lost today — the Telegram push fires unconditionally and the owner is told (§2.2). What is lost is a
  *reader who already has the context*, in a session state whose frequency **nobody has measured**
  (§2.4). This is worth building because it is nearly free, **not because the gap is large**, and
  §7's phasing is arranged so that a cheap probe can kill it at a defined gate rather than after the
  wiring is in.

---

## 1. What is true today — read, not remembered

Read from the code and run at the CLI. Nothing under `seneschal/state/` was written. Symbol names, not
line numbers, are the durable address.

### 1.1 The ledger and the push

| Fact | Where |
|---|---|
| `TERMINAL` is `(done, failed, timed-out, ended-unknown, cancelled)`; `ACTIVE` is `(running, retry-pending)` | `jobs.py` |
| The completion push fires off the **record**, once, on every terminal state | `jobs.reconcile` — the guard is `status not in TERMINAL or notified_at` |
| `notified_at` is stamped **only when the send landed**; a failed send retries next tick | `jobs.reconcile` — *"leave `notified_at` unset: the retry IS the guarantee"* |
| A record is born `"notified_at": None` | `jobs.start_job` |
| `reconcile` is the one pass that does all of this; `presence.py`'s ~5 s scheduler tick calls it | `jobs.reconcile` |
| `load_job` requires only `id` and tolerates unknown keys | `jobs.load_job` |
| Default per-attempt deadline is **6 h** | `DEFAULT_DEADLINE_SEC` |

**So the floor is not merely present, it is load-bearing and free.** It needs no new code, and the
only way to lose it is for someone to deliberately make a delivery conditional on something else
working. Every proposal below is measured against that.

### 1.2 The mailbox, and the read moment that does not come around again

| Fact | Where |
|---|---|
| Rung 3's pull mailbox is `state/session-mail/<session_id>.jsonl` | `SESSION_MAIL_DIRNAME` |
| Retention is 14 days, one constant shared with `prune --days` — **but unread mail is never swept at any age** | `RETENTION_DAYS`, `prune_session_mail` |
| The address comes from `$CLAUDE_CODE_SESSION_ID` | `ORIGIN_ENV_VAR` |
| `jobs.py mail` is the read; it defaults `--session` to that env var and prints `no mail`, exit 0, when there is none | subparser + dispatch, `jobs.py` |
| **The read leaves a receipt** — `<session_id>.cursor.json`, a separate file so it cannot race the appender. `mail` shows unread and advances; `--all` shows everything; `--peek` shows without advancing; every read prints `N new, M total` | `session_mail_view`, `job-origin-routing-spec.md` §3.3 |
| The module says in its own comment why this is a pull and not a push | `SESSION_MAIL_DIRNAME`'s block |

And the sentence this whole spec exists to answer, from the parent
(`job-origin-routing-spec.md` §3.3):

> *"**Honest limitation, stated rather than papered over:** rung 3 is a pull, and a pull needs a
> reader. A session that closes before its next orientation never sees its mail."*

### 1.3 What `jobs.py` does NOT have — confirmed at the CLI, not by grep

```
$ python seneschal/scripts/jobs.py --help
  {start,list,status,cancel,analyze,reconcile,prune,mail,…}
```

**There is no `wait`. There is no `watch`. There is no blocking anything.** Every subcommand returns
immediately. `status` answers *"what is it now"*; nothing answers *"tell me when it changes"*. That
absence is the whole of the new code this spec proposes.

### 1.4 The session registry, and why it cannot help here

| Fact | Where |
|---|---|
| Hook entries are written on `SessionStart` / `UserPromptSubmit` / `Stop`, removed on `SessionEnd` | `session_stamp.py` |
| Every hook-stamped entry carries `pid: 0` **on purpose** | `HOOK_PID`, `session_stamp.py` |
| A `desktop` entry ages out of the gating TTL in **120 s**, out of the awareness listing in **1 h**, and is deleted at **24 h** | `sentinel.py` |
| Only `daemon` / `desktop` sources gate delivery | `GATING_SOURCES`, `sentinel.py` |

The registry knows **who is live**. It does not know how to reach any of them, and `pid: 0` means it
cannot even tell you which OS process to signal. It is an awareness surface, and this spec asks
nothing of it.

### 1.5 The harness affordance — probed

This is the one thing the parent spec could not have known, because it is a property of the Claude
Code harness rather than of this repo. A `Bash` call with `run_in_background: true` was given a
75-second Python sleeper:

| Observed | Evidence |
|---|---|
| The command **survives across turns** — it ran through four subsequent tool calls | its output file held `watcher start` at +25 s and `watcher exit` at +75 s |
| On exit, the session was **re-invoked with a `<task-notification>`** carrying the task id, the output-file path, `status: completed`, and **the exit code in the summary line** | the notification arrived in the conversation, unprompted |
| The child is a **descendant of the session's `claude` process**, not detached | the measured PID chain ran python ← shell ← … ← the session's own CLI process |
| The process is **gone after exit** — no zombie | a process lookup by its pid returned nothing |

Two consequences, and the second is the one the design turns on:

1. **The notification carries the exit code without anything reading the output file.** So a watcher's
   *exit code* is the cheapest possible signal, and the design should put the outcome there.
2. **The child is in the session's process tree.** It therefore cannot outlive the session as a
   detached orphan — which is the opposite of what `jobs.py` deliberately does for jobs
   (`DETACHED_PROCESS`, `background-jobs-spec.md` §3.1) and is **exactly right here**: a watcher
   whose only purpose is to wake a session has no business surviving that session.

**What this probe did NOT establish is in §8, and it is load-bearing.** The probing session was
continuously active throughout. Whether the re-invoke fires at a session that is **idle, awaiting a
human turn** — which is the entire case this feature exists for — was not tested and cannot be tested
from a non-interactive run.

---

## 2. The problem, sharpened

The brief states it correctly. Three sharpenings change what should be built.

### 2.1 It is not that the session stops checking. It is that "checking" only ever happened once.

Orientation is a **step in a command file**, executed when `/assistant` is invoked. `jobs.py mail` sits
in that block. There is no second orientation: a session's turn 2 does not re-read its own grounding, so
the mail read is not a habit that lapses — it is a one-shot that already fired, before the job it
would have been about even existed.

So *"they stop checking once they start the job"* is precisely inverted, and the inversion matters
for design: **they checked before they started it.** No amount of instruction-strengthening in the
orientation block can fix this, because the block runs at the wrong time. Whatever fixes it must
create a **new read moment**, and a read moment needs something to cause it.

### 2.2 Nothing is lost. Name what is.

The completion push fires unconditionally off the record (§1.1) and retries until it lands. **The owner
is told, every time, on every terminal state, including the ugly ones.** Any framing of this feature as
"the result goes missing" is false, and building against that framing is how the push ends up
conditional on the watcher — the one thing §9 forbids absolutely.

What is actually lost is narrower and worth stating exactly:

> **The session that started the job is the only reader that already knows why it was started.** The
> mail is addressed to it specifically — a ranked list of places to look, produced by an analyst that
> was deliberately given nothing but a log path and a goal line. Read by that session, it is a lead
> list. Read by a fresh session tomorrow, it is a lead list *plus* the reconstruction of everything
> the first session already knew — which is exactly the cost the analysis existed to save.

That is real, and it is bounded. It is not an outage.

### 2.3 The median job is a few minutes long, and this only ever bites the tail

From `session-coupling-spec.md` §2.3: the median job reaches a terminal state in a few minutes, the
p90 is around an hour, and the maximum runs many hours. **About a third ran ≥ 20 minutes.** For the
jobs whose origin session could be identified at all, the outcome arrived a median of well over an
hour after that session's last observed turn (§2.4 there).

Read this the way it cuts, in both directions:

- **Against the feature:** a few-minute job started mid-conversation is very often still the same
  conversation when it ends. The session is *right there*; it will look, or the owner will mention the
  push. For the median job there is nothing to fix.
- **For the feature:** the long tail is not exotic work, it is *the delegated build work* — the
  `--worktree` jobs, the CI watches, the analyses. Those are precisely the jobs where the origin
  session's context is worth most and where an hour-plus gap makes it least likely to still be there.

So the population is **the long tail of jobs, started from a desktop session, whose session is still
open when they end.** Which brings the last sharpening.

### 2.4 Three session states, and only one of them is fixable — its frequency is UNMEASURED

At the moment a job reaches a terminal state, its origin session is in exactly one of three states:

| | State | What today does | What any watcher can do |
|---|---|---|---|
| **(a)** | Actively working — mid-turn, or about to take another | It will see the mail at its next natural look; often it is still the same conversation (§2.3's median) | Marginal. The gain is timing, not existence |
| **(b)** | **Open, but idle, awaiting a human turn** | **Nothing. This is the gap.** No re-orientation, no push channel, no reader | **This is the whole addressable population** |
| **(c)** | Closed | Nothing, and nothing is possible — a watcher in the session's process tree (§1.5) dies with it | Nothing. Rung 4 already fired; that is the answer |

**Nobody has measured how often (b) obtains.** It is not in `state/jobs/` (which knows nothing about
session liveness at terminal time) and it is not in `state/sessions/` (whose `desktop` entries expire
on a 120 s gating TTL that answers a different question). It could be reconstructed by joining a job's
`ended_at` against its origin session's registry `last_seen`, and **§7's phase 2 is where that join
belongs** — but the `desktop` heartbeat's own TTL makes the answer noisy, which is why phase 2 proposes
a direct probe instead.

This is the number that decides whether the feature is worth its (small) cost, and this spec does not
have it. **Saying so is the point** — the alternative is an argument built on a frequency nobody
counted.

---

## 3. What a solution must do

Derived from the parents, not invented here. Any design that fails one of these is rejected on that
ground alone.

1. **No silent path — additively.** Every terminal state notifies exactly once
   (`background-jobs-spec.md` §2). A watcher may only ever **add** a delivery. It must never make the
   Telegram push conditional on the watcher working, and must never move it.
2. **Degrade to the floor.** Every failure of the watcher — not started, died, killed, timed out,
   harness feature absent — lands on today's behaviour, which is that the owner got the push.
3. **Respect the command-file boundary.** Anything that needs an owner's host-side edit (their own
   copy of a command file outside the repo) must be written out verbatim as a manual step (§6.3), the
   way `job-origin-routing-spec.md` §3.3.1 does — and the mechanism must not depend on it.
4. **No zombies.** State what kills the watcher in every case, including session-closed,
   machine-rebooted, job-cancelled, and job-already-terminal-when-the-watcher-starts.
5. **Token cost is a design input, not a footnote.** A design that burns dozens of model turns to
   observe one boolean is a bad design and the numbers must show it.
6. **Stdlib-first.** No new dependency.
7. **Idempotent and restart-safe**, per the standing `state/` rules — and the strongest form of that
   here is §4.2's: **the watcher writes nothing at all.**

---

## 4. The four mechanisms

### 4.1 A — in-session self-paced wake (`/loop` dynamic mode, `ScheduleWakeup`)

**What it is.** Claude Code can schedule its own next wakeup (delay clamped to [60, 3600] s) and be
re-invoked with the same prompt. It is real, available today, and needs zero new code in this repo —
the owner could type `/loop` at an `/assistant` session right now.

**Why it loses.**

- **Every tick is a real model turn.** Not a cheap one: this is an `/assistant` session, so a tick
  carries the persona, the grounding, the mode file and the conversation so far — on the order of a
  few tenths of a dollar per turn (`session-coupling-spec.md` §2.1, §2.6). §5's table works that
  through.
- **It polls a thing that notifies.** The job record has a terminal state and an owner that already
  pushes on it. Spending N model turns asking "has it finished yet" of a system built around
  "I will tell you when it finishes" is the wasteful shape, and **the `/loop` tool's own guidance says
  so** — do not schedule short-interval wakeups to poll for harness-tracked work, because when
  harness-tracked work finishes you are re-invoked automatically. That last clause is mechanism B.
- **The interval cannot be right.** The floor is 60 s and the ceiling is 3600 s, against a job
  distribution whose median is minutes and whose tail is hours (§2.3). Tune for the median and a
  6-hour job costs dozens of turns; tune for the tail and the median job waits an hour past its own
  end.
- **It is not durable in the sense the brief wants.** A scheduled wakeup belongs to the session. Close
  the window and it is gone — the same as B, but B did not cost anything on the way.

**What A is still good for, and it is worth keeping in view:** a job the owner specifically wants
babysat with *judgement* between looks — read the partial log, decide whether to cancel, adjust. That
is a different feature (supervision, not notification), it is the owner's to invoke by typing
`/loop`, and it needs nothing from this repo. **It is not the answer to "tell me when it's done."**

### 4.2 B — a blocking waiter that re-invokes on exit — **RECOMMENDED**

**The shape.** A new subcommand, `jobs.py wait <id>`, that blocks until the job reaches a terminal
state and then exits. The session launches it with `run_in_background: true` and returns to whatever
it was doing. It costs **zero turns while waiting** and wakes the session **exactly once, at the
moment the job ends** (§1.5, verified).

This is the notify shape rather than the poll shape, and it is the harness's own documented pattern
for it: for a one-shot "tell me when the build finishes", use Bash with `run_in_background` and a
command that exits when the condition is true — you get a single completion notification when it
exits.

**Tested hardest rather than assumed:**

| Question | Answer | Basis |
|---|---|---|
| Does the re-invoke actually fire? | **Yes** — a `<task-notification>` arrived unprompted, carrying task id, output path, status and exit code | §1.5 |
| Does it fire when the session is **idle awaiting a human**? | **UNVERIFIED — and this is the load-bearing unknown.** §8 names the experiment | not testable from a non-interactive run |
| If the user closes the session, is the child killed? Does it leave a zombie? | The child is a **descendant of the session's `claude` process** (measured PID chain), so it is not detached and cannot orphan the way a `jobs.py` job deliberately does. Whether the CLI reaps the tree on clean exit vs. leaves it to the OS is **UNVERIFIED** (§8) — which is why §4.2.2's hard deadline is not optional | §1.5 |
| What if the job outlives the session by hours? | Then the watcher is dead or timed out and **nothing happens** — which is rung 4, which is today, which is the owner got the push. State (c) of §2.4 | by design |
| Max practical block duration? | **UNVERIFIED.** Probed 75 s only. The `Monitor` tool caps at 1 h unless `persistent`; plain `Bash` background has no documented ceiling (§8) | — |

**Note what the fourth row means.** B's failure mode is not a wrong answer or a lost message — it is
*the absence of an extra delivery*. That is the cheapest failure mode available, and it is what makes
constraint 2 satisfiable by construction rather than by care.

#### 4.2.1 The one design decision that matters: the watcher writes nothing

`jobs.py wait` **reads `state/jobs/<id>.json` and nothing else. It does not write, does not stamp,
does not notify, does not append, does not touch `notified_at`, does not touch the mailbox.**

This is not tidiness, it is how constraint 1 is *proved* rather than promised. The parent spec had to
argue that routing may only add a delivery, and pin it with tests re-asserting "exactly once" with
routing on, off, and exploding. **A read-only watcher needs no such argument:** it cannot make the push
conditional because it touches nothing the push reads, and it cannot make the push fire twice because
it cannot fire anything. The regression test is *"no module named in the watcher's call graph writes to
`state/`"* — the same shape as `pending_checks.py`'s phase-0 test that nothing so much as mentions it,
and the same shape as a structural-unreachability assertion against `co_names`.

**Corollary, and it is a real cost accepted rather than hidden:** because the watcher writes nothing,
**there is no record that a watcher ever ran.** Nobody can later ask "how often did this fire, and did
it help?" from disk. That is the uncounted-metric trap pointed at this feature — and it is accepted
here for phase 1, because the alternative is a `state/` writer in a path whose whole value is that it
cannot break anything. §7's phase 2 measures the thing that actually matters (does the idle re-invoke
fire at all) by direct probe instead, which is cheaper *and* answers the question a counter never
could.

#### 4.2.2 The contract

```
jobs.py wait <id> [--poll-sec N] [--timeout-sec N] [--state-dir DIR]
```

| Aspect | Decision | Why |
|---|---|---|
| **The race — job already terminal at start** | Check the record **before the first sleep**, every time. A terminal job exits immediately with 0 | The race is the common case for a few-minute job (§2.3), not an edge case. A watcher that sleeps first is wrong for the median job |
| **Poll interval** | Default **5 s**, matching `presence.py`'s scheduler tick | The record only changes when `reconcile` writes it, so polling faster than the tick observes nothing. One small local-disk read per tick; negligible against a session that is idle anyway |
| **Hard deadline** | Default: **the job's own `deadline_sec` read off its record, plus a grace of a few ticks**. Never unbounded | The watcher's lifetime is *derived from its subject's*, so it structurally cannot outlive the thing it watches. `DEFAULT_DEADLINE_SEC` is 6 h, so the default default is ~6 h |
| **Record vanishes mid-wait** (pruned, deleted) | Exit **3** immediately. Never spin | `prune` drops terminal jobs whose push already landed, so a vanished record means the job ended *and* was reported. Spinning on an absent file is how a watcher becomes a zombie |
| **Exit codes** | **0** = a terminal state was reached · **2** = the watcher's own deadline elapsed, the job is still active · **3** = no such record / it vanished | The notification summary carries the exit code without anything reading the output file (§1.5), so this is the cheapest signal available |
| **Does exit 0 distinguish `done` from `failed`?** | **No — deliberately.** | An exit code that means "the job succeeded" makes the watcher a **second source of truth** about the outcome, and a second source of truth is a thing that can disagree with the first. `jobs.py status` is the answer to *what happened*; `wait` only answers *it is over*. A wrapper whose zero exit is not success is the failure this cut prevents, applied before it can bite |
| **What it prints** | **One line, self-sufficient**, on exit: job id, title, terminal status, wall-clock duration, and the literal next command (`python seneschal/scripts/jobs.py mail`) | The waking session's cheapest possible next step is to read this line and act. A line that requires a second lookup to interpret wastes the turn the watcher just bought |
| **Writes** | **None.** §4.2.1 | — |

#### 4.2.3 What kills it, in every case (constraint 4)

| Case | What happens | Result |
|---|---|---|
| Job reaches any terminal state | `wait` sees it on the next poll, prints, exits 0 | The session is re-invoked. **The feature working** |
| Job was already terminal when `wait` started | Pre-sleep check catches it, exits 0 immediately | Re-invoked at once. No spurious delay |
| Job cancelled | `cancelled` **is** terminal and is sticky at write time (`background-jobs-spec.md` §3.10), so `wait` exits 0 | Correct by inheritance — nothing special needed |
| Job retries (`retry-pending`) | Not terminal (`ACTIVE`), so `wait` keeps waiting through the backoff | Correct: one wake at the real outcome, matching the push's own rule |
| Session closed | The watcher is in the session's process tree (§1.5) and dies with it — or, if the CLI does not reap it, its own deadline (§4.2.2) ends it | Either way bounded. **UNVERIFIED which** (§8) — the deadline is what makes that not matter |
| Machine rebooted | Process gone. The record survives on disk; the daemon's next `reconcile` pushes as always | Floor intact. Nothing to clean up |
| Watcher's deadline elapses first | Exits 2. The job is still running and still owned by the daemon | The session learns the watch lapsed, not that the job failed |
| Record pruned mid-wait | Exits 3 | See §4.2.2 |
| `wait` itself crashes | The `<task-notification>` reports a non-zero exit; nothing else changes | Floor |

**There is no case in which a watcher outlives its job, and no case in which it outlives its session
by more than its own deadline.** That is the whole of constraint 4.

### 4.3 C — daemon-side injection into a desktop Claude Code session — **NOT POSSIBLE**

Whether this was possible was an open question in the ask. It is not, and the evidence is strong
enough to close it rather than hedge it.

**1. There is no queue, no drainer and no socket.** The daemon's inbound path appends to the daemon's
*own* durable action queue and wakes its *own* drainer. `jobs.py` records this in the module itself: a
desktop `/assistant` or a hook-stamped `build` session *"has no queue, no drainer and no socket. They
can only READ."*

**2. The daemon does not own the desktop session's stdin, and stdin is the only control surface.**
The CLI *does* accept a rich control protocol on stdin — probing it confirmed `control_request` frames
with `interrupt`, `set_model`, `set_permission_mode`, `can_use_tool`, `hook_callback` and
`mcp_message` subtypes, with a `control_response` back in under 0.1 s. **That is exactly why C
fails:** the daemon can do all of this to the warm session *because it spawned it and holds the pipe*.
A desktop session's stdin is the owner's terminal or app window. There is no file descriptor for the
daemon to write to, and there is no listener on the other side to open one.

**3. Even with the pipe, a written message does not reach the model mid-turn.** Probed: a
`{"type":"user"}` line written several seconds into a running turn was *accepted*, and the turn **ran
to completion** before the CLI consumed it. So "injection" is not something the CLI offers even to a
process that owns the pipe — the best available primitive is *interrupt, then start the next turn*.
That is not a thing an external process can do to a session it did not spawn.

**4. Hooks are callbacks the session triggers, not an inbound channel.** Every hook event —
`PreToolUse`, `SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`, `PreCompact`,
`InstructionsLoaded` — **is fired by the session's own lifecycle.** There is no event an external
process can raise. A hook is a thing the harness calls *out* to; it is not a door in.

#### 4.3.1 C′ — the near-miss worth naming, and why it also loses

There is one hook that is genuinely more than a passive stamp, and honesty requires naming it rather
than lumping it in above: **`Stop` can block.** A `Stop` hook that returns a block decision prevents
the session from going idle and hands it a reason to continue. That *is* a re-invoke surface, and it
is the closest thing in the harness to what the ask was reaching for.

**It still does not solve this**, for a reason that has nothing to do with whether it works:

- **It fires when a turn ends, not when a job ends.** A session that starts a job at turn N and then
  goes idle has already had its `Stop` hook fire — at the end of turn N, before the job finished. It
  will not fire again until turn N+1 ends, which requires the owner to type, which is the gap. **C′
  covers state (a) of §2.4 and misses state (b) entirely** — it fixes the case that barely needs
  fixing.
- **It is host-side** — installed in the user's `~/.claude/settings.json`, not shipped by this repo
  (constraint 3).
- **It would change the posture of an already-wired hook.** `session_stamp.py` is on `Stop` and its
  contract is explicit: it *must print nothing and must always exit 0 — a broken courtesy signal must
  never break a session*. Adding a second `Stop` hook that deliberately blocks puts a thing that can
  *hold a session open* next to a thing that is fail-silent by contract, on the event that fires most
  often.

**Verdict: C is closed, C′ is a worse B.** If a future harness version grows an external-event hook,
this section is the place to re-open it — and the re-open would be a transport swap onto the same
`jobs.py wait` contract, not a redesign.

### 4.4 D — do nothing new. The null hypothesis, given a real hearing.

**What D already delivers, at zero cost:**

- Every terminal state pushes the owner exactly once, retried until it lands (§1.1). Including
  `ended-unknown`, including `cancelled`, including the ones nobody wants to read.
- The analysis artifact is filed to disk unconditionally, first, before any delivery is attempted
  (`job-origin-routing-spec.md` §3.3 rung 1). `jobs.py status <id>` prints its path forever.
- The mail sits in the mailbox for 14 days (`RETENTION_DAYS`) — and **indefinitely while it is
  unread**. A session that *does* re-orient — a fresh `/assistant` tomorrow morning under the same
  session id, or any session the owner points at it — reads it, and leaves a receipt saying so.
- **It cannot break**, because there is nothing to break.

**D's real case, stated at its strongest:** the thing this feature buys is a *timing improvement on a
context advantage*, in a session state whose frequency is unmeasured (§2.4), for the long-running
third of jobs (§2.3), and only when the session happens to still be open. Multiply those three
unmeasured-or-partial factors together and the expected value is genuinely modest. **If `jobs.py wait`
cost a week and a new dependency, D would win outright and this spec would recommend it.**

**Why D loses anyway:** B costs roughly forty lines of stdlib polling in a module that already owns
the ledger, writes nothing, adds no dependency, has a failure mode of "nothing extra happens", and
consumes **zero** model turns. Against that, even a modest expected value clears the bar. The argument
for B is an argument from *cost*, not from *severity* — and it is important that the record says so,
because a future reader who inherits "we built a watcher" will otherwise assume the gap was big.

### 4.5 Hybrids

**B with A as a fallback when B is unavailable — rejected.** It is the obvious hybrid and it is wrong
twice over. First, "B unavailable" means the harness affordance is missing, which is a condition the
session cannot reliably detect — so the fallback would fire on a guess. Second, the fallback's cost is
paid in exactly the situation where we know least: burning up to 72 turns (§5) because we could not
tell whether a notification would have arrived. **Constraint 2 says degrade to the floor, and the
floor is D.** A watcher that cannot be armed should do nothing, loudly enough that the session says so
in one clause and moves on.

**B for notification, A for supervision — accepted, but it is not this feature.** §4.1's last
paragraph. The owner can type `/loop`; nothing in this repo needs to change for it, and nothing here
should try to automate it.

---

## 5. The decision table

Job durations are §2.3's distribution. Turn cost is taken as a few tenths of a dollar per warm-session
turn (`session-coupling-spec.md` §2.1, §2.6) — *a warm-session figure used here as an
order-of-magnitude anchor for a desktop `/assistant` turn, which is the same model with a comparable
context. It is a proxy, and it is labelled one.*

| | **A** — `/loop` poller | **B** — `jobs.py wait` | **C** — daemon injection | **D** — do nothing |
|---|---|---|---|---|
| **Turns burned — 5-min job** | ≥ 1 (60 s floor ⇒ ~1–5 ticks) | **1** | — | **0** |
| **Turns burned — 1-hour job** | ~1 at 60-min ticks; **12** at 5-min ticks | **1** | — | **0** |
| **Turns burned — 6-hour job** | **6** at 60-min ticks; **72** at 5-min ticks | **1** | — | **0** |
| **≈ cost, 6-hour job** | **a couple of dollars to tens of dollars** | **one turn's worth** | — | **$0** |
| **Latency to notice** | ½ the tick interval on average (30 s – 30 min) | **≤ poll interval, ~5 s** | — | until the next `/assistant` orientation, or never |
| **Survives session close** | No | No (and correctly so — §1.5) | — | **N/A — the push is the daemon's** |
| **Survives reboot** | No | No | — | **Yes — the record and the push are on disk** |
| **New code required** | **None** | ~40 lines in `jobs.py` + tests | — | None |
| **Host-side edit required** | None | **None for the mechanism** (§6.3) | Would need one, and it still would not work | None |
| **Failure mode** | Silent over-spend; wrong interval either way | **Nothing extra happens ⇒ D** | Does not exist | It is the floor |
| **Verdict** | Loses on cost and shape | ✅ **Recommended** | ❌ Not possible (§4.3) | The floor B degrades to |

**Read the 6-hour row across.** A burns between six and seventy-two model turns to learn one boolean;
B burns one, later, and knows it sooner. That is the argument, and no amount of interval tuning
changes its direction — because the right interval for a distribution with a minutes-long median and
an hours-long maximum does not exist.

---

## 6. What is in this repo's gift, and what is not

Constraint 3, made explicit, because the parent spec paid for this distinction.

### 6.1 In this repo's gift — the whole mechanism

| Change | File | Phase |
|---|---|---|
| `jobs.py wait <id>` — the subcommand, its exit codes, its deadline, its race check | `seneschal/scripts/jobs.py` | 1 |
| Its unit tests, incl. the already-terminal race, the vanished record, the deadline, and *nothing writes to `state/`* | `seneschal/scripts/test_jobs.py` | 1 |
| The manual idle-re-invoke probe — a new `watch_probe` script, **a manual live probe, not a test, not in CI** | `seneschal/scripts/` | 2 |
| The instruction telling a session that starts a long job to arm the watcher | `seneschal/SKILL.md` / `seneschal/modes/chat.md` (beside the existing job rule) | 3 |
| The router line and the setup note | `seneschal/docs/CLAUDE.md` and the scripts docs | with each |

**The important line here: the arming instruction is IN-REPO.** It belongs where jobs are started —
the Chat-mode rule that already tells the assistant to use `jobs.py` for anything long-running — not
in the orientation block. That is what makes B cheaper than the parent's rung 3: **rung 3 needed an
orientation read because its read moment was orientation; B creates its own read moment, so it needs
none.**

### 6.2 Not needed for B

The `/assistant` command file (`.claude/commands/assistant.md`, or an owner's own host-side copy) does
not need to change for B. The watcher's exit line names the next command, and the re-invoke is the read
moment.

### 6.3 The optional command-file strengthening — verbatim, copy-pasteable

Two things belong here anyway.

**(a) The parent spec's orientation read.** `job-origin-routing-spec.md` §3.3.1 specifies the
orientation step that reads the mailbox. It should be in whichever command file an owner's desktop
sessions load, **regardless of anything in this document**, because it fixes the case B cannot: a
*new* session, tomorrow, under the same session id.

**(b) A belt-and-braces instruction at the top of any turn that mentions a job.** Optional, additive,
and strictly a strengthening — append after the orientation step:

```markdown
6. If a background job is (or might be) outstanding for this session, **arm a watcher rather than
   promising to check back**: launch `python seneschal/scripts/jobs.py wait <id>` as a BACKGROUND Bash
   command and carry on. It costs no turns while it waits, and it wakes this session once, when the
   job actually ends — at which point read its one-line output and run `python
   seneschal/scripts/jobs.py mail`. If the watcher cannot be armed, say so in one clause and move on:
   the owner gets the completion push from the daemon either way, so a missing watcher costs a
   convenience, never the result. Never make a promise to "check back later" that nothing is holding —
   arm the watcher or say plainly that nobody is watching.
```

**The last sentence is the one that matters**, and it is the standing lesson of
`background-jobs-spec.md` §1: from a warm session that winds down on idle, "I'll watch it / I'll
circle back" is hollow — nothing wakes it. A watcher makes that promise keepable. Where it cannot be
armed, the honest move is to withdraw the promise, not to make it anyway.

---

## 7. Phasing — smallest useful first, merge on green

**Phase 1 — `jobs.py wait`. Nothing calls it.**
The subcommand, its exit codes, its derived deadline, the pre-sleep race check, the vanished-record
exit, and tests including the structural *"the watcher writes nothing"* assertion (§4.2.1). ~40 lines
plus tests, stdlib, no new dependency, no behaviour change anywhere.

*Why this is worth merging even if nothing after it is ever built:* it is immediately useful to a
**human**. `jobs.py wait <id> && jobs.py status <id>` is a thing the owner can type in a terminal; so
is chaining a watcher into a script. It is also the single missing verb in a module that already
answers every other question about a job's life. **It stands on its own merits with the harness
question entirely unresolved** — which is exactly the property phase 1 should have, because phase 2
might come back "no."

**Phase 2 — settle the load-bearing unknown, before anything depends on it.**
A new `watch_probe` script: a **manual live probe, not a test and not in CI**, that the owner runs once
in a real interactive `/assistant` session. It arms a background sleeper, says to stop typing, and
reports whether the re-invoke landed at an idle session and how long it took. Three minutes of the
owner's time.

*Why before phase 3, and not after:* this is `background-jobs-spec.md` §7.4.1's argument, applied one
feature later — **collect the data before the decision it informs.** If the re-invoke does not fire at
an idle session, then §2.4's state (b) is unreachable, **B is dead, and the answer is D** — and finding
that out costs three minutes rather than a merged instruction that quietly does nothing. The probe is
also version-pinned: this is undocumented harness behaviour, and it needs a re-run when the CLI moves.

**Phase 3 — wire it. Gated on phase 2 coming back yes.**
One instruction in `seneschal/modes/chat.md` beside the existing job rule (§6.1), and the §6.3(b)
command-file line if the owner wants it. The regression test that matters here is the parent's,
re-asserted: **the completion push still fires exactly once on every terminal state, with a watcher
armed, without one, and with one that crashes.** Arming is by phrase (§11.2).

**Phase 4 — not proposed, recorded so it is not reinvented.** A cockpit surface showing which jobs
have a live watcher would be the natural next thing, and it is the wrong next thing: §4.2.1 chose to
write nothing, so there is no data for a panel to read. Building the panel means reversing that
choice, which is a separate decision with a separate argument.

---

## 8. What could not be verified

Non-negotiable section. Everything here was **not run and not read**, and is named rather than
smoothed over.

1. **Whether the background-exit re-invoke fires at a session that is IDLE, awaiting a human turn.**
   This is the single load-bearing unknown and the entire feature rests on it. The re-invoke was
   verified across turns while the session was continuously active (§1.5); the idle case cannot be
   tested from a non-interactive run. **The experiment that settles it:** in a real interactive
   `/assistant` session, launch `python -c "import time;time.sleep(180)"` with
   `run_in_background: true`, send no further input, and observe whether the session produces output
   on its own at +180 s. That is phase 2, and it takes three minutes.
2. **Whether the desktop app surface exposes `Bash(run_in_background)` with the same re-invoke.** The
   probe ran on the same host at the same CLI version, which is strong circumstantial evidence, but it
   did not exercise the interactive desktop surface itself. Folded into the phase-2 probe.
3. **Whether the CLI reaps background children on clean exit, or leaves them to the OS.** The
   process-tree *relationship* (PID chain, §1.5) was verified, and the child was gone after its own
   exit. Closing a session with a live child was not. §4.2.2's derived hard deadline is what makes this
   not matter, and it is in the design for that reason.
4. **The maximum practical block duration for a background Bash task.** Probed 75 s. Not probed: 6 h.
   The `Monitor` tool documents a 1 h cap unless `persistent`, which is suggestive but is a *different
   tool*. If plain background Bash has a comparable ceiling, a 6-hour job's watcher dies at that
   ceiling and exits — degrading to D, which is safe, but it would mean B covers the p90 and not the
   max. Worth establishing in phase 2.
5. **How often §2.4's state (b) actually obtains.** Not measured and not measurable from `state/jobs/`
   alone. It is the number that would tell us the feature's real value.
6. **The mid-turn interleave probes were read only in part.** The sections §4.3 relies on were read in
   full; later sections were not. If they contain a rejected alternative bearing on daemon-to-session
   delivery, it was not seen.
7. **`presence.py` was not read in full.** Its grounding region was read by grep, and the claim
   depended on — the daemon's inbound path feeds only the warm session — is asserted identically by
   `job-origin-routing-spec.md` §1.1 and by `jobs.py`'s own comment.
8. **`jobs.py wait` does not exist and no code was written.** Every line count, exit code and default
   in §4.2.2 is a **proposal**, not a measurement. The ~40-line estimate is a judgement from reading
   the neighbouring functions, not from writing it.

---

## 9. What is NOT in scope

- **No change to the completion push, ever.** §3.1. If a future change wants "don't buzz the owner
  when the session got it", that is a separate decision with a separate argument — and doing it by
  accident is how the no-silent-path guarantee dies.
- **No new delivery channel.** This spec adds a *read moment*, not a rung. The §3.3 ladder is
  untouched.
- **No push to a desktop session.** §4.3. Closed, with evidence.
- **No watcher for anything that is not a `jobs.py` job.** A backgrounded command has no id, no
  terminal state and nothing to poll (`session-coupling-spec.md` §1.1). Requiring a job id is the same
  structural move `pending_checks.py` makes, for the same reason.
- **No supervision.** The watcher does not read the log, does not judge, does not cancel, does not
  retry. It answers *"is it over"* and nothing else. Judgement between looks is A, and it is the
  owner's to invoke.
- **No cockpit surface.** §7 phase 4.
- **No state written.** §4.2.1, with its cost named.
- **No cross-machine addressing.** One box, one state dir.

---

## 10. Open questions (as first posed)

Three genuine forks. Each changes the design, and none is answerable by reading. §11 records how each
was decided; this section stays as written, because a spec that edits away its own uncertainty after
the fact teaches the next reader nothing.

1. **If phase 2's probe says the idle re-invoke does NOT fire — accept D, or fall back to A?**
   The recommendation is **accept D and stop**: §5 shows A costing up to tens of dollars to observe one
   boolean, and the floor already tells the owner. But that is a judgement about whether the
   context-preservation in §2.2 is worth real money, and that judgement is the owner's. **It decides
   whether phase 3 exists at all.**

2. **Arm a watcher automatically for every long job started from a desktop session, or opt-in per
   job?** The parent spec made analysis *opt-in* explicitly because it costs money. **A watcher does
   not** — zero turns while waiting, one turn at the end you would want anyway. That argument points at
   automatic. What points the other way is that automatic means every desktop job spawns a background
   process, and *"every"* is how a small cost becomes a habit nobody audits. Recommendation:
   **automatic for jobs whose deadline exceeds some threshold, opt-in below it** — but the threshold is
   a number that governs the owner's machine, and it is theirs to pick.

3. **When the watcher wakes the session, should the owner be told?** The session comes back to life,
   reads its mail, and acts — possibly while the owner is doing something else entirely. **Silent**
   means they may never learn the session acted on a job they were already pushed about. **Loud** means
   a second notification for the same job, which is the duplicate-buzz shape
   `cancel-attribution-spec.md` exists to soften. The *no silent path* posture and the *don't buzz
   twice* posture point in opposite directions, and which one governs depends on whether a woken
   session reads as *the assistant doing something* or as *the assistant finishing something already
   known about*.

---

## 11. Decisions

All three of §10 were decided by the owner.

### 11.1 If phase 2's probe fails, accept D and stop

If the manual probe shows the background re-invoke does **not** fire at a session idle awaiting a
human, the feature ends there. **Phase 3 does not exist** and the polling fallback is not built. §5's
arithmetic stands as the reason — up to 72 billed turns to observe one boolean on a 6-hour job — and
the floor (the Telegram completion push) already tells the owner.

**So phase 2 is a real gate, not a formality.** It is the cheapest place this can die and it is
authorised to kill it.

### 11.2 Opt-in by PHRASE, not by deadline threshold

**The owner's reason is the design input:** a watcher is wanted mostly when they are stepping away —
overnight, or away from the computer for a while. When they are at the machine they can simply go
back to the session and nudge it. So the opt-in is a phrase — *"watch that job"* or similar.

**This supersedes §10's "automatic above some deadline threshold" recommendation, and it is better.**
A threshold *guesses* whether the owner is present; a phrase is the owner *stating* it. Job duration
was never the variable — **presence was** — and no number could have inferred it. **Do not invent a
threshold**, and do not add "automatic for long jobs" later as a convenience: it re-introduces the
guess this decision removed.

**The consequence that must be designed in, not bolted on: the watcher has to be armable
RETROACTIVELY against an already-running job.** The phrase frequently arrives *after* `start` —
*"actually, watch that one"* — because the decision to step away is unrelated to when the job began. A
watcher that can only be armed at `jobs.py start` fails the majority of the case this decision exists
to serve. Arming and starting are therefore **separate operations**, and the arming one takes a job id
that is already live.

### 11.3 Silent on the wake, loud on the EFFECT, quiet-hours deferred

§10 had no recommendation it trusted, because *no silent path* and *don't buzz twice* pointed opposite
ways. **They only appeared to conflict because they attach to different events.**

- **Nothing is sent because a session woke up.** A wake is plumbing, and the completion push already
  covered *"the job ended."*
- **A notification fires when the woken session CHANGES STATE the owner would care about** — a commit,
  a PR, an outbound message. That is a different fact from *"the job ended,"* and it is the one the
  floor never covered.
- **If it lands in the quiet window it defers into the morning brief** rather than buzzing. A code
  commit is not worth waking the owner for; the content being right does not make 3 AM the right
  delivery.

**Why §10's deadlock broke, and it was 11.2 that broke it.** §10 assumed watchers would be common, so
"don't buzz twice" was protecting a frequent case: the owner sitting at the machine, pinged
redundantly. Opt-in-by-phrase makes the **away / asleep** case the *only* case a watcher ever runs in.
So the scenario the silent argument protected is now rare by construction, and the scenario the loud
argument protected — asleep while a session commits something — is the whole of it.

**The invariant is untouched.** This still only ever ADDS a delivery. The Telegram completion push
fires exactly as it does today, unconditionally, and nothing here may gate it, move it, or make it
depend on a watcher having worked.

## Router entry

**Status:** SPEC ONLY.

**What it decides:** Who reads rung 3's mail once the session that started the job goes idle. **The
daemon CANNOT wake a desktop session — closed with evidence** (§4.3), **and a poller is the wrong
watcher**: `/loop` burns up to 72 turns on one boolean where a blocking `jobs.py wait` costs **one, at
the right moment**. It **writes nothing**, which is how "may only ADD a delivery" is proved not
promised — and it **argues its own value down** (nothing is lost today; the median job is minutes long;
the one fixable state UNMEASURED).
