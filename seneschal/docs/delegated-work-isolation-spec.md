# Delegated-work isolation — one worktree per job

**Status:** `PARTIAL(phases 1+3 BUILT)` — **Phase 1** is `jobs.py start --worktree` +
`seneschal/scripts/job_worktree.py` (`create` / `teardown` / `sweep`), pinned by
`test_job_worktree.py` and `test_jobs.py`'s `WorktreeJobTests`. **Phase 3 (the sweep)** is built —
but **not** by wiring `sweep`: it shipped as `seneschal/scripts/worktree_gc.py` +
`test_worktree_gc.py`, run from **Dream step 2**, with `sweep` left tested and unwired. §6 records why
the shipped sweeper is a strict superset rather than the function that was already written, and §10
is the measurement that forced it. **Phases 0 and 2 remain unbuilt**, and neither 1 nor 3 implies
them: nothing yet records `isolation` on every job (0), and nothing refuses a launch into the live
daemon's checkout (2). Two phase-1 deviations, both deliberate and both argued where they land:
`--worktree` takes **no branch name** (§5.1 — detached, the agent runs its own `checkout -b`), and one
line of phase 0's documentation fix was taken early — `--cwd` has a help string, because the new
flag's help would otherwise point at an undocumented one (§9.5).
**Scope:** `seneschal/scripts/jobs.py`, `seneschal/scripts/job_worktree.py`,
`seneschal/scripts/worktree_gc.py`, the `jobs.py` block of the daemon's grounding prompt, and
`seneschal/SKILL.md`'s job rule.
**Siblings:** `background-jobs-spec.md` (the parent — jobs exist because of it) and
`job-origin-routing-spec.md`. Read the parent first.

**The ask:** several delegated jobs collided in one shared checkout within a single afternoon. Two
recovered by hand; one wrote a commit into *another session's branch* before anyone noticed.

---

## 1. The record

Four delegated jobs, all launched with an empty `origin`, `wake: true`, a six-hour deadline, and
`--permission-mode bypassPermissions`:

| Job | `cwd` | Outcome |
|---|---|---|
| A — mint an archon | the shared **dev** checkout | exit 0, after a hand recovery |
| B — write a spec | the shared dev checkout | exit 0, after a hand recovery |
| C — bring an archon to standard | the shared dev checkout | exit 0, opened its PR, noticed nothing |
| D — implement a feature | **the live daemon's checkout** (no `--cwd`) | cancelled by the assistant seconds in |

A, B and C overlapped: all three held the same working tree for several minutes, two of them for
longer. Each was on a **different** branch. Every one exited `0`.

**What each one hit** (their own logs, paraphrased):

1. **B** — a concurrent session had staged its work in the shared tree, and B's `checkout -b` moved
   the branch under it. B restored that branch and its staged files, and did its own work in a
   throwaway worktree it then removed.
2. **A, the important one** — another session moved the checkout's HEAD mid-run. A's commit landed
   on *that session's* branch, and the push of A's own branch was a silent no-op, surfacing only
   later as `gh pr create` failing with "No commits between develop and <branch>". Recovery: reset
   the other session's branch, cherry-pick into a worktree cut from `origin/develop`, re-run the
   checks there, push, remove the worktree.
3. **C** — ran in the same tree across the same window and landed its PR. It is the session whose
   branch the other two were working around; its own log records no collision, which is the point
   (§2).
4. **D, the near-miss** — launched with **no `--cwd`**, so it ran in the live daemon's checkout, the
   one place the repo's rules forbid branching. It was caught and cancelled by hand. Nothing in code
   would have stopped it.

## 2. The property that makes this urgent is the silence

A commit landing on the wrong branch **succeeds**. A push of a branch that has no commits
**succeeds** — `Everything up-to-date`, exit 0. Neither is an error at the moment of damage. In case 2
the first symptom was `gh pr create` failing, minutes and one wrong-branch commit later, and the
damage was already written into a branch that belonged to a session that never knew.

C's clean log is the strongest evidence here: it was *one side of* the collision, and from inside it
nothing was wrong. A shared checkout does not tell either party that it is shared.

So the goal is **not** fewer collisions. It is: the failure becomes impossible, or it becomes loud.
Every design choice below is scored on that, and `git worktree add` scores well on both counts — it
removes the shared mutable HEAD entirely, and where it can still fail (a branch already checked out
elsewhere, a path that exists) it fails with a non-zero exit and a message, never with a no-op.

## 3. The mechanism is already the house pattern — this is plumbing, not invention

Both improvising agents reached for `git worktree` and it worked. Reading the repo says something
stronger: **a transient worktree off `origin/<base>` is already the documented convention here, for
exactly this hazard.**

| Where | What it does |
|---|---|
| `seneschal/modes/dream.md` (the proposed-learnings PR step) | `worktree add -b <branch> <tmp> origin/<base>` → write the change → commit → push → `gh pr create` → `worktree remove`, every git command prefixed `-c core.fsmonitor=false`, so the daemon's checkout is never `git checkout`-ed off its deploy branch. Runs nightly. |
| Archon promotion tools | The same recipe in code, for the same reason: never touch the live checkout. |

So the delegated-job path is the **one** writer that didn't adopt a pattern the repo already runs
every night. **What is wrong is the default, not the mechanism.** That is why this spec proposes no
new concept — only moving an existing one down a layer, from "the agent remembers" to "`jobs.py` does
it".

### 3.1 Branch discipline was already correct, and it did not help

All three colliding jobs used distinct branch names. Nobody made a naming mistake. **The unit of
collision is the checkout, not the branch** — which is also the answer to §5.8's question about
`sentinel.branch_is_claimed`.

---

## 4. What was true in the code before this spec

| Fact | Where |
|---|---|
| `start_job` takes `cwd` and stores `os.path.abspath(cwd or REPO_ROOT)` | `jobs.py` `start_job` |
| `REPO_ROOT` is derived from **`jobs.py`'s own location** (`SCRIPT_DIR/../..`) | `jobs.py` |
| …so the default cwd is *whichever checkout the launching process loaded `jobs.py` from* — for the daemon, always the live checkout | the cause of case D |
| `DEFAULT_STATE_DIR` is derived from the **same** location | `jobs.py` |
| The shim and the retry respawn both re-read `rec["cwd"]` | `jobs.py` |
| `--cwd` existed as a bare `add_argument` with **no help string** | `jobs.py` argparse |
| `--cwd` appeared **nowhere** in the daemon's grounding prompt, `seneschal/SKILL.md`, or `.claude/commands/assistant.md` | grep |
| There is exactly **one** production caller of `start_job`: the CLI | `jobs.py` `main` |
| Nothing in `jobs.py` read, validated or recorded anything about the working tree | grep for `git`/`worktree`/`branch` |
| `sentinel.branch_is_claimed` matches on `s.get("branch")` only — branch name, never checkout | `sentinel.py` |
| `session_stamp.py` stamps each session's own `cwd` **and** `branch` | `session_stamp.py` |
| The harness's nested `.claude/worktrees/` is hidden from `git status` only by `.git/info/exclude` — an **untracked, per-clone** file | `git check-ignore -v` |
| `rag_projects.find_repos` treats any `.git`-bearing dir under a scanned root as a project; `exclude_names` is **exact-name**, not glob | `rag_projects.py` |
| Worktrees had accumulated against the dev checkout for weeks, and `git worktree prune --dry-run -v` over them reported **nothing** | see §5.6 |
| A fresh worktree of this repo is a few MB of tracked files; the dev checkout's `.venv` is *not* copied | `du`, `git ls-files` |

---

## 5. Design

### 5.1 Where delegated work runs: one worktree per job, cut from `origin/develop`

**The argument, rather than the assumption.** The failure in §1 is not concurrency; it is *shared
mutable state* — one HEAD, one index, one working tree, N writers, no lock and no notification. Three
candidate fixes:

- **A lock** (one delegated job at a time in a checkout). Removes the collision and removes the
  parallelism with it — three jobs of twenty-odd minutes each serialise into over an hour. It also
  does nothing about case D, which was a *wrong tree*, not a busy one.
- **A checkout per job (clone).** Correct and expensive: a full clone plus its own object store, per
  job, for work that lives minutes.
- **A worktree per job.** Private HEAD, index and working tree; shared object store; a few MB and one
  `git worktree add`. It is the smallest thing that removes the shared mutable state, and it is
  already the house pattern (§3).

**Cut from `origin/develop`, explicitly, always.** Two reasons. `develop` can be an ambiguous ref (a
checkout with two remotes carries one on each), so the spelling matters; and a worktree cut from *the
host checkout's HEAD* inherits whatever branch that tree happened to be parked on, which is how a
fresh worktree ends up many commits stale. The wrapper therefore runs
`git -c core.fsmonitor=false fetch origin develop` first.

**Detached, not on a new branch.** `git worktree add --detach <path> origin/develop`. The delegated
agent then runs its own `git checkout -b feat/whatever` inside its private tree, exactly as its brief
already tells it to, with **no change to how briefs are written** and no wrapper-created branch to
clean up afterwards. This is the property that makes adoption free.

### 5.2 Where the worktree lives — constraints from the code, one answer

Not a matter of taste; separate pieces of the repo constrain it.

1. **Sibling-path resolution.** Any tool that guesses a sibling repo as `REPO_ROOT.parent / "<name>"`
   is *wrong* inside a nested `.claude/worktrees/<name>`. Any placement whose ancestor chain doesn't
   pass through the parent `repos/` directory re-breaks every sibling-repo guess.
2. **`.git/info/exclude`.** The nested `.claude/worktrees/` location is invisible to `git status` only
   because of an **untracked, per-clone** file. A re-clone does not recreate it, so that invisibility
   is one re-clone from gone.
3. **The project-state RAG corpus.** `rag_projects.find_repos` indexes any `.git`-bearing directory
   under a scanned root as a project, and `exclude_names` matches **exact directory names**. Worktrees
   named per job can't be excluded by name; worktrees under one fixed-name parent can, in one line of
   `state/project-roots.json`.

**⇒ `<repos>/seneschal-worktrees/<job-id>/`**, i.e. `REPO_ROOT.parent / "seneschal-worktrees" / job_id`
— derived, never hardcoded. `repos` stays in the ancestor chain (1), no exclude file is needed (2), and
`"seneschal-worktrees"` is one entry in `exclude_names` (3). Overridable by `SENESCHAL_WORKTREE_ROOT`.

Naming the worktree after the **job id** rather than the branch is deliberate: the job id already sorts
by start time (`jobs.py`'s `YYYYmmdd-HHMMSS-xxxx`), it is what the record and the log are named after,
and it is knowable *before* the agent picks a branch.

### 5.3 Who creates and destroys it: `jobs.py`, and only `jobs.py`

**Convention is what existed, and it failed three times in one afternoon.** Reading the code narrows
the choice further: there is exactly **one** production caller of `start_job` — the CLI — so *every*
job launch is a command line typed by a model mid-turn. A flag is therefore exactly as forgettable as
a convention. The model that forgot `--cwd` (case D) is the same model that will forget `--worktree`,
and an `origin` sitting empty on consecutive records (`job-origin-routing-spec.md` §2(b)) is another
instance of the same lesson.

**So: the flag creates the isolation, and the *default* is what must be made safe.** Those are
separable, and the phasing in §6 does them in that order.

- `--worktree` → `job_worktree.create(job_id)` before the spawn; `rec["cwd"]` becomes the worktree,
  and `rec["worktree"]` records it. Teardown per §5.6.
- **No module owns the git calls twice.** `seneschal/scripts/job_worktree.py` is a stdlib module with
  an injected `Runner` seam so CI never shells out to a real `git`.

**Not the delegated agent, by convention.** Beyond §1: an agent that creates its own worktree does it
*after* it has already started reading and editing in the shared tree, which is where B's
`checkout -b` did its damage. The isolation has to exist before the agent's first command.

### 5.4 What it costs

A `git worktree add` checks out **tracked files only** — a few MB, well under a second. It does
**not** copy the expensive things, and that cuts both ways:

| Not in a fresh worktree | Consequence |
|---|---|
| `.venv` | a job needing non-stdlib deps must provision or opt out (below) |
| `node_modules` | `cockpit/web` / `phone` jobs need `npm ci` |
| `seneschal/state/` runtime data | only `*.example.*` are tracked — **a feature, see below** |
| `*.env` secrets | gitignored, so a delegated job cannot accidentally message the owner for real |
| large gitignored archon outputs | not copied, not walked, not a cost |

**The dominant case needs no provisioning at all.** The two checks every build brief requires —
`git ls-files '*.py' | xargs python -m py_compile` and
`python -m unittest discover -s seneschal/scripts -p "test_*.py"` — are **stdlib-only**, and were run
in a bare, unprovisioned worktree to verify this spec.

**What genuinely needs more:** the `cockpit/server` suite (`uv sync --extra cockpit --group test`) and
anything touching `cockpit/web`. That is a real cost and it is **not** solved here — §8.3 is the open
question, with `UV_PROJECT_ENVIRONMENT` (share one venv across worktrees) as the candidate to spike,
and "cockpit-touching jobs declare shared" as the zero-work fallback.

**The state-dir consequence is the subtle one.** `DEFAULT_STATE_DIR` and `REPO_ROOT` derive from the
same location, so a delegated agent that runs `python seneschal/scripts/jobs.py start …` *from inside
its worktree* would write a job record into the **worktree's** `seneschal/state/jobs/` — a ledger no
daemon reconciles, in a directory scheduled for deletion. That is a brand-new silent path, in the
module whose whole invariant is that there are none. **The wrapper must pass `--state-dir`
explicitly** (already a top-level argument), and phase 1 pins that with a test. This hazard exists
only because worktrees are being introduced; it is the one thing this change *adds* rather than
removes, and it is cheap to close.

### 5.5 The refusal in code for the live checkout

The live checkout is the daemon's. A delegated job that branches or commits there is the strictly
worse version of §1 — it can block the updater's `pull --ff-only`, leaving the daemon deploy-blind for
as long as nobody notices (`seneschal/scripts/PATH_A_CUTOVER.md`).

**How to identify it without hardcoding a path:** the daemon's checkout is the one whose
`<cwd>/seneschal/state/presence.lock` holds a live PID with a fresh heartbeat. `jobs.py` already
imports `sentinel` and already has a conservative `pid_alive`. No constant, no config, correct on a
rebuilt box.

**Hard error, with one named override.** A blanket refusal is wrong: some tools stage files into the
tree the **cockpit reads**, which is the daemon's, so they must run there. The rule is therefore:

| Launch | Result |
|---|---|
| resolved cwd is the live daemon checkout, no flag | **refused**, exit 3, message naming `--worktree` and `--cwd-shared` |
| `--worktree` | isolated; the question never arises |
| `--cwd-shared <path>` | allowed, and recorded as `isolation.mode = "shared-declared"` |

`--cwd-shared` is deliberately not `--force`: the flag's *name* is the assertion ("this job must run in
a shared tree and will not touch git"), the same way `--retry` is the caller's assertion that re-running
is safe (parent spec §7.3). And because it is recorded, "which jobs ran in a shared tree, and were they
right to?" is a `jq` over `state/jobs/` rather than an archaeology exercise.

The shared **dev** checkout is the same hazard with a lower blast radius — §6 warns first and refuses
later, on phase 0's data.

### 5.6 Cleanup, including the paths nobody plans for

**`git worktree prune` is not the reclaim tool, and this is the single most load-bearing correction in
this spec.** Checked against a dev checkout with weeks of accumulated worktrees: `git worktree prune
--dry-run -v` reports nothing. `prune` drops *administrative records whose directory is already gone*.
A leaked directory — which is precisely what a killed job leaves — is invisible to it, forever.
Reclaiming one is `git worktree remove`.

So the sweep is:

1. **Owner: the nightly Dream pass**, alongside the `--prune-days` sweeps it already runs. It is the
   same layer that already owns "no job goes silent", and it is already scheduled. A new scheduled
   task for this would be a second thing to notice being dead.
2. **Reclaim, don't guess.** For each directory under the worktree root: find its job record by id.
   Remove it only when the record is **terminal and its completion push has landed** — the identical
   condition `prune --days` already uses, for the identical reason (the GC must not be what makes a job
   go silent), plus an age floor.
3. **`git worktree remove`, never `--force`.** Plain `remove` **refuses** when the tree is dirty. That
   refusal is the feature: a one-file tool that wrote and committed exactly one file can afford
   `--force`; a general job wrapper cannot. (§10.2 corrects what this refusal does *not* cover.)
4. **A refused removal is a leak, and a leak is loud.** It is left on disk, recorded on the job
   (`worktree_leaked: true` + why), and named in the completion push: *"kept
   `…/seneschal-worktrees/<id>` — it has uncommitted work."* **A leaked directory is a few MB; lost
   work is not recoverable.** `jobs.py list`/`status` show leaks, and the sweep reports a running count
   so a pile-up is visible rather than merely present.
5. **`git worktree prune` still runs**, once, at the end of the sweep — its actual job is tidying admin
   records for directories a human deleted by hand, and it is the correct tool for exactly that.

**The four paths nobody plans for**, each answered by machinery that already exists:

- **The job fails** → terminal state → §5.6.2, normally with unpushed work, so normally a loud leak.
- **The job times out** → `timed-out` is terminal (parent §2); same path.
- **The job is cancelled** → `jobs.py cancel` already kills the shim and its child; teardown follows the
  same clean-or-leak test. A cancel seconds in (case D) leaves a clean worktree and reclaims it.
- **The daemon restarts mid-run** → nothing is held in memory. The path is in the record; the successor
  daemon re-reads it off disk. This is the *same* mechanism that already makes the completion push and
  the pending retry survive a restart (parent §3.3, §7.5), which is why it needs no new argument.

### 5.7 What the dev checkout is for after this

**It is the human's tree, and delegated work leaves it entirely.** It holds in-flight uncommitted work
that exists nowhere else, and it is the one checkout that is *provisioned* (a real `.venv`; secrets
deliberately not). Everything §1 describes is a machine writing into a human's scratch space.

It can also be the **host** for job worktrees — its `.git` is then the object store, which is why it
must keep existing and stay fetched (§8.1). The convention becomes three rows:

| Path | Whose | Rule |
|---|---|---|
| the live checkout | the daemon | never branch, never dirty |
| the dev checkout | **a human** | the owner's own sessions and in-flight work. **Delegated jobs do not run here.** |
| `<repos>/seneschal-worktrees/<job-id>` | **one delegated job** | created off `origin/develop`, private, disposable |

### 5.8 The claim guard is not warranted here — and the record says why

`sentinel.branch_is_claimed` is fail-closed, already consulted by `seneschald-control.ps1`'s
self-heal, and **could not have prevented any of the three collisions.** It matches on
`s.get("branch")` — a branch *name*. At the moment A ran `checkout -b <its branch>`, that branch did
not exist and no session claimed it; the guard would have answered "free", correctly, and the damage
happened afterwards when a *different* session moved the shared tree's HEAD. The guard answers "may I
move this branch out from under someone?" The question here is "am I sharing a working tree?" —
§3.1's point, in code.

**Recommendation: do not add it to the job path.** Worktree isolation makes it unnecessary (a private
HEAD cannot be moved by anyone else), and a redundant guard is its own cost. It stays exactly where it
earns its keep.

**And isolation makes the registry itself more accurate, for free.** `session_stamp.py` stamps each
session's own `cwd` and `branch`; one-tree-one-branch removes the ambiguity of several live registry
entries reading the same `<checkout> @ <branch>`. `job-origin-routing-spec.md` §3.1's conclusion —
identity comes from `CLAUDE_CODE_SESSION_ID`, never from `cwd`+`branch` — is unchanged and should stay
unchanged; this only makes the *enrichment* honest.

---

## 6. Phasing — smallest useful first, merge on green

**Phase 0 — make the default visible. No behaviour change.**
Record `isolation` on every job: `{"mode": "shared", "cwd": …, "worktree": null,
"is_live_daemon_checkout": bool, "concurrent_jobs_same_cwd": [ids]}`. Give `--cwd` a help string. Add
two sentences to the daemon's grounding prompt and `SKILL.md`'s job rule saying where a delegated job
runs and that git work needs its own tree. Nothing refuses; nothing changes.
*Why first:* it is the parent spec's §7.4.1 argument again — **collect the data before the decision it
informs.** Phase 2's refusal will break some caller; phase 0 is how we learn which, instead of finding
out in production. It also closes case D by *documentation*, since the model that launched into the
daemon's checkout had nothing to read about `--cwd` (§4). Worth landing alone even if nothing follows.

**Phase 1 — `--worktree`. BUILT.**
`seneschal/scripts/job_worktree.py`: `create(job_id)` / `teardown(rec)` / `sweep(state_dir, days)` over
an injected `Runner` (no real `git` in CI), and `jobs.py --worktree`. Detached at `origin/develop` after
an explicit fetch (§5.1), under `<repos>/seneschal-worktrees/<job-id>` (§5.2), `--state-dir` passed
explicitly (§5.4), clean-or-loud-leak teardown (§5.6). Tests pin: the fetch happens before the add;
`remove` is never `--force`; a dirty worktree leaks and says so; the state dir is the daemon's, not the
worktree's.

*As built,* plus what the writing found:

- **A failed setup FAILS THE JOB** — terminal, notifiable, nothing spawned. The spec said the fetch
  comes first and did not say what a *failed* fetch does; letting it warn and carry on would cut from
  a stale base, which is the silent failure the fetch exists to prevent, so it is fatal.
- **`--worktree` and `--cwd` are refused together** (exit 2). One says "make me a tree", the other
  "use this one"; ranking them would be a guess about where a job runs, which is the class of failure
  §1 is made of.
- **Teardown is idempotent** (`worktree.torn_down_at`). `reconcile` re-reads un-notified terminal
  records on every tick, and a Telegram outage means many — a second `remove` against a reclaimed path
  would report a fresh "leak" for a directory that is simply gone.
- **`sweep` treats a directory with no record as an `orphan` and does not remove it.** "Reclaim, don't
  guess" (§5.6.2) has no reading under which a missing record proves a job is over. It is counted, and
  phase 3's leak counter is where that count becomes visible.
- **The leak reason is collapsed to one line before it reaches the push**, `normalise_why`'s rule for
  `notify_text`'s newline-joined parts (`cancel-attribution-spec.md` §4.3). A twelve-line git error
  would otherwise be indistinguishable from the next part of the message.
- **Still undiscoverable where it matters**, and that is phase 0's to fix: `--worktree` appears in
  `jobs.py start --help` and not in the grounding prompt's job block. §5.3's own argument says a flag
  is exactly as forgettable as a convention. The grounding prompt runs close to its byte budget
  (the context-budget byte ratchet), so that sentence needs a trim elsewhere or a raise — a phase-0 decision.

**Phase 2 — the refusals.**
The live-daemon-checkout refusal + `--cwd-shared` (§5.5), sized against phase 0's data, with the known
shared-tree callers updated in the same PR. A warning (not yet a refusal) for a shared *dev* checkout.

**Phase 3 — the sweep. BUILT, as `worktree_gc.py`.** The dev-checkout flip stays with phase 2, unbuilt,
still gated on phase 0's data. *The regression test that matters here* — a job whose worktree cannot be
removed still reaches a terminal state and still notifies exactly once — shipped in phase 1
(`WorktreeJobTests.test_a_teardown_that_raises_never_costs_the_completion_push`) and is untouched:
**this phase changed nothing in `jobs.py` and nothing in `teardown`.**

*As built, and why it is not the `sweep` this section planned* (the measurement is §10):

- **Not `jobs.py prune --worktrees`, and `sweep` was left unwired.** Reclaiming is not the same
  operation as dropping a record: it deletes a **checkout**, so it wants a dry run by default, an
  age floor in hours, protected paths, and a per-directory verdict to report. Folding that into
  `prune`, whose other sweeps are `os.remove` on regenerable files, would have hidden it behind a
  flag on a command nobody reads the output of. It is its own script, in the same Dream step.
- **The shipped sweeper is a strict superset of `sweep`, which is why `sweep` may not be revived.**
  It keeps the terminal-and-notified condition verbatim, and adds the two things §5.6 assumed
  `git worktree remove` would cover and it does not: an **empty husk** is reclaimed (`sweep` calls
  every record-less directory an `orphan` and removes none — on a real pile that is most of it),
  and a clean worktree holding **unpushed commits** is refused (`remove` permits it, §10.2).
- **`--force` still appears nowhere**, asserted against the source *and* against every argv handed
  to the runner. The husk path is `os.rmdir`, never `shutil.rmtree`: `rmdir` refuses a non-empty
  directory, so the OS re-proves the emptiness at the instant of deletion and the check cannot go
  stale between the proof and the act.
- **An error is a refusal**, and two errors refuse the whole run rather than one directory — an
  unreadable `git worktree list`, and an **absent job ledger**. The second is the one worth naming:
  without records a *running* job's tree reads as an orphan, and a running job's tree is usually
  clean and fully pushed, i.e. it satisfies proof 2. A `--state-dir` one level too deep produces
  exactly that, silently (§10.3).
- **Held is not refused.** A running job and a young directory are the expected nightly state;
  reporting them would ping the owner every night about nothing. Only removals and refusals speak.

**The interaction phase 1 found is now answered, and the answer cost nothing:** `jobs.prune --days`
drops a notified terminal job's record at 14 days while `sweep` identifies a directory *by* its
record, so a leak outliving its record became an unreclaimable `orphan` — a race, since both numbers
were `RETENTION_DAYS`. **The husk proof dissolves it.** A record-less directory that is empty is
removed on the strength of its own emptiness, no record consulted; a record-less directory that is
*not* empty was never safe to remove on a record's say-so either. So neither retention number had to
move, and §8.4 is answered by construction rather than by picking a number. The age floor that
replaced it is **6 hours**, and it is a floor rather than a retention: nothing is removed *because*
it is old, only because it is provable, and the age just buys confidence that nothing is still
writing there.

---

## 7. Out of scope

- **CI changes.** None. The suites are unchanged and run in a bare worktree (§5.4).
- **The daemon's reload path.** Untouched. `seneschald-update` / the self-heal keep their current
  behaviour exactly; worktree admin records in a checkout do not affect `pull --ff-only`.
- **How the dev checkout is cloned or provisioned**, beyond the convention table (§5.7).
- **The harness's own `.claude/worktrees/`.** Not ours to move. Phase 0 records it like any other cwd;
  §5.2's placement argument applies only to worktrees *this* spec creates.
- **Provisioning.** §8.3 — an open question, not a v1 feature.
- **Any change to the completion-push guarantee.** Same standing rule as
  `job-origin-routing-spec.md` §3.3: this may only add a delivery, never make one conditional.

## 8. Open questions for the owner

1. **Which repo hosts the worktrees?** Recommended: whichever checkout `jobs.py` is running from — no
   new path constant, works on a rebuilt box. Cost: when the *daemon* starts a job, the admin records
   land in the live checkout's `.git/worktrees/`. That is not a dirty tracked file and does not move
   HEAD, but it is administrative writing in the deploy checkout, whose rule is deliberately strict.
   The alternative (pin the host to the dev checkout) is cleaner but needs a configured path and a dev
   checkout that always exists.
2. **Does the shared *dev* checkout ever hard-refuse, or stay a warning forever?** A refusal means the
   owner cannot fire a quick job there without a flag. Recommended: decide on phase 0's data, not now.
3. **Provisioning for jobs that need more than stdlib** (§5.4): share one venv across worktrees via
   `UV_PROJECT_ENVIRONMENT` (needs a spike — unverified), `uv sync` per worktree (a venv and a link
   step per job), or "cockpit-touching jobs run `--cwd-shared`" (zero work, keeps one class of job
   colliding). Recommended: fallback now, spike later.
4. **Leak retention.** Answered by construction in phase 3 (§6): the husk proof removes the race, and
   the 6-hour floor is a confidence margin, not a retention.

## 9. Where reading the code and the logs contradicted the brief

1. **`git worktree prune` does not reclaim leaked worktrees.** It reads as though it is the reclaim
   tool. It is not: it only drops admin records whose directory is **already gone** — verified with
   `prune --dry-run -v` reporting nothing over weeks of accumulated worktrees. The leak the brief
   correctly worries about is exactly the leak `prune` cannot touch; the tool is `git worktree
   remove`, and its refusal-when-dirty is what implements "never delete uncommitted work silently".
   §5.6.
2. **The mechanism is more proven than the brief claimed.** It isn't only that two improvising agents
   reached for `git worktree`; it is already the documented house pattern, in code and in a nightly
   instruction (Dream), for the *same* hazard. The delegated-job path is the one writer that didn't
   adopt it. §3.
3. **Branch discipline was already correct.** All three colliding jobs were on distinct branches. The
   unit of collision is the **checkout**. That is also why `sentinel.branch_is_claimed` could not have
   prevented case 2: the branch did not exist and was unclaimed at `checkout -b` time. §3.1, §5.8.
4. **The no-`--cwd` default has a cause worth naming.** `REPO_ROOT` is derived from `jobs.py`'s own
   file location and shares it with `DEFAULT_STATE_DIR`. So it is not a missing default — it is one
   location serving two purposes, **correct for the state dir and wrong for the working tree**. That
   framing is what produces §5.4's warning about a worktree-local ledger, a silent path the naive fix
   would introduce. §4, §5.4.
5. **`--cwd` was undocumented everywhere the model can see.** A bare `add_argument` with no help string
   while every neighbouring flag carried several lines, and absent from the grounding prompt,
   `seneschal/SKILL.md` and `.claude/commands/assistant.md`. The model that launched into the daemon's
   checkout had nothing to read. That makes a one-line phase-0 doc fix worth more than its size. §4,
   §6.
6. **Placement is constrained by existing code the brief did not have.** Sibling-repo resolution
   breaking inside a nested worktree, `.git/info/exclude` (the nested location is invisible only
   because of an untracked per-clone file a re-clone would not recreate), and `rag_projects.find_repos`
   (exact-name exclusion, so worktrees need one fixed-name parent or they enter the project corpus).
   Together they pick the directory. §5.2.
7. **"Who creates it" is a narrower choice than it looks.** There is exactly one production caller of
   `start_job` — the CLI — so every launch is a command line typed by a model. A flag is therefore as
   forgettable as a convention, and the thing that must be made safe is the **default**, not the flag.
   That splits the brief's single question into two phases. §5.3, §6.
8. **A blanket live-checkout refusal would break a real caller.** Tools that stage into the tree the
   cockpit reads must run in the daemon's checkout. The refusal needs a named, recorded override
   (`--cwd-shared`) rather than being unconditional. §5.5.

---

## 10. The pile, measured — and the three things it corrected

Some days after phase 1, read off disk before a line of phase 3 was written: a handful of worktrees
still registered under `<repos>/seneschal-worktrees/` and more directories present than registrations.
Every branch of the shipped decision tree was exercised by that one pile, which is why it is the
fixture the tests are modelled on rather than an invented one.

### 10.1 "Leaked" is not one state — and `sweep` could reclaim almost none of it

Most of the directories had a job record marked `worktree_leaked: true`. But they split three ways,
and only the middle group is what §5.6 pictured:

| What was on disk | What `sweep` would do | What is actually true |
|---|---|---|
| **Empty husk** — no `.git`, unregistered, zero entries (the most common) | nothing (`orphan` — no record match by path) | nothing at risk at all |
| **Registered, clean, pushed** | remove — correct | correct |
| **Half-finished removal** — no `.git`, but top-level entries remain (up to thousands of files, one carrying a `.venv`) | nothing | **nothing here is provable**; refuse and say so |

So the shape of a real leak is mostly *directories with nothing in them*, and the sweep as designed
reclaims exactly none of those — it identifies a directory by its record and treats a record-less one
as an `orphan`. That is right as a rule about **guessing**, and wrong as a rule about **emptiness**:
an empty directory needs no record to prove it holds nothing. That single distinction is the
difference between a sweeper that clears the pile and one that clears one directory of many.

### 10.2 `git worktree remove` does not check unpushed commits — and one tree proved it

One registered worktree had `status --porcelain` **empty** and `rev-list --count HEAD --not --remotes`
= **1**: a commit that existed in no other ref in the repository.

§5.6.3's original text said plain `remove` refuses "when the tree is dirty or has unpushed commits".
**The first half is right and the second is not** — git's check is `is_worktree_dirty`, tracked and
untracked files only. So the refusal phase 1 correctly relies on would not have fired here, and a
sweeper trusting it would have deleted the one directory on the pile that actually held something.
This is the correction that makes `worktree_gc.py` a different program rather than a scheduling
change: the proof that matters is not available from the tool doing the removing.

### 10.3 Two bugs the sweeper's own first run found, both silent

Both were caught by running it against the real pile rather than by reasoning about it, and both are
now tests.

1. **The derived root was wrong when run from inside a worktree.**
   `job_worktree.worktree_root()` is `dirname(REPO_ROOT)/seneschal-worktrees`, correct for `jobs.py` in
   the daemon's checkout and wrong for a sweeper run from `<repos>/seneschal-worktrees/<job-id>`, where
   it yields `<repos>/seneschal-worktrees/seneschal-worktrees`. That does not exist, so the first run
   reported a clean night over a directory that was never the pile — **a no-op that looks exactly like
   success.** Fixed in `worktree_gc.resolve_root`, not in `job_worktree`, whose derivation is right for
   its own callers.
2. **A `--state-dir` one level too deep disabled the running-job cross-check, silently.** Records
   live at `<state>/jobs/<id>.json`, so passing the `jobs/` directory made `load_job` return `None`
   for every job — indistinguishable from a job that never existed. A **running** job's worktree
   then read as an orphan; it was clean and fully pushed, so only the age floor stood between a live
   agent and its checkout. Now: an absent ledger refuses the entire run, and a registered worktree
   with no record is refused outright — two rules, because one load-bearing rule is one edit from
   being none.

## Router entry

**Status:** PHASES 1 + 3 BUILT.

**What it decides:** `jobs.py --worktree`. The unit of collision is the checkout, not the branch.
**Phase 3 is the sweeper for what a teardown that correctly refuses `--force` leaves behind — and it
did NOT wire the `sweep()` phase 1 shipped for it** (that one stays unwired; reviving it puts the
weaker sweeper in front of the stronger). §10 is why: on a real pile, **"leaked" was three states, not
one** — mostly EMPTY husks `sweep` reclaims none of, since it identifies a directory by its record and
an empty one needs no record to prove it holds nothing. And **plain `git worktree remove` does not
refuse unpushed commits** — it checks dirtiness only, and a tree holding a commit that existed in no
other ref would have been deleted by a sweeper trusting it. Removal is opt-in per directory: two
proofs, everything else left and REPORTED, an error is a refusal.
