# Job fan-out — whether a delegated job may run several agents, and how the panel would show it

**Status:** `SPEC-ONLY(closed)` — nothing built. · **Scope:** the brief-side guidance next to
`jobs.py`'s caller-facing rules and `background-jobs-spec.md`; and, for the panel,
`cockpit/server/jobs.py` + the cockpit's Jobs web panel (a later component). **This document AMENDS
`jobs-panel-depth-spec.md` §7** (see §0 and that file's note).

**The ask.** Right after `jobs-panel-depth-spec.md` recommended *against* a phases/agents tree, the
owner pushed back: any time they ask for something to be built they would want the agent breakdown,
and if nothing in the pipeline can run several agents at once, that is worth fixing.

Both halves of that are right. **The capability is present and has been all along** (§2). **It had been
used in almost no delegated job** (M3) — and the one time it was, it worked (§3).

---

## 0. Why the prior spec was wrong, stated plainly

`jobs-panel-depth-spec.md` §7.4 argued: a job is one opaque subprocess, so the tree would render one
phase containing one agent on every recent job — less information than the log tail already shows.
Every one of its numbers was correct. **The inference is circular.**

It measured a population in which nothing fans out, and concluded nothing would ever be there to show.
But the reason nothing fans out is not that the capability is missing. It is that **the briefs the
assistant writes never ask for it** — and the one brief that did ask got nine concurrent agents, a
several-fold speedup on agent time and a wide PR (§3). The spec measured the consequence of its own
author's habit and reported it as a property of the system.

The same file gets a second thing wrong by over-reach. §6 concluded per-job tokens are *"D, and NOT via
`metrics.jsonl`"*. The `metrics.jsonl` half is exactly right and is not disturbed here. The general
claim that tokens are unsourceable is **false for the population the tree is actually for**: a delegated
`claude` job's own CLI transcript carries `usage` on every assistant message (M15), broken down per
agent, already on disk, written by nobody who had to remember to.

---

## 1. The measurements this rests on

Read-only scans of the local Claude Code project store (`~/.claude/projects`, structure only) and the
daemon's `state/jobs/`. The counts were taken on one install and are not reproduced here; what is
recorded is the **structural finding** each one established, which holds on any install with the same
CLI behaviour.

| # | Measurement | Finding |
|---|---|---|
| M1 | Project directories named after a job worktree path | one per `--worktree` job that ran long enough to write a transcript |
| M2 | `tool_use` calls across those transcripts | dominated by Bash/Edit/Read/Write; **`Agent` almost never; `Workflow` never** |
| M3 | Transcripts containing an `Agent` call | **one**, the fan-out job of §3 |
| M4 | Job records in `state/jobs/` | a large minority ran in a private worktree |
| M5 | Worktree jobs whose path, encoded the way Claude Code names a project directory, **is** an existing project directory | **all** that ran more than a few seconds; the only misses were children that died before writing a transcript |
| M6 | `argv[0]` across worktree jobs | a shell wrapper or `claude`, **nothing else** — every private-worktree job is an agent job |
| M7 | Non-worktree jobs | most share `cwd` = the repo root, whose project directory holds thousands of sessions (the owner's own and the daemon's) |
| M8 | A delegated job's own session | `entrypoint: sdk-cli`, `permissionMode: bypassPermissions`; its tool list includes **Agent, TaskCreate/TaskUpdate/TaskOutput/TaskStop, Workflow, ToolSearch** |
| M9 | The scratch launcher scripts that start `claude` | **none carries `--allowedTools`, `--agents`, `--model` or `--output-format`** — the child gets the default tool set |
| M10 | The fan-out job's `Agent` calls | 9, every one `subagent_type: general-purpose`, `spawnDepth: 1`, **no model override, no `run_in_background` argument** |
| M11 | Its agent timings | all nine spawned within a few minutes; **peak concurrency 9**; agent-time sum several times the wall-clock window |
| M12 | Its subagents' own tool calls | hundreds, overwhelmingly Bash and Edit |
| M13 | Distinct in-tree files edited **by the 9 subagents**, in one shared worktree | dozens, and **0 pairs of subagents edited a shared file** |
| M14 | The PR that job opened | one docs-only PR, dozens of files, merged |
| M15 | Token usage recoverable from that job's transcripts | parent: `usage` on **every** assistant message; subagents likewise, per agent |
| M16 | Sessions anywhere on the machine with a `subagents/` directory | many; interactive sessions fan out routinely and widely |
| M17 | Fan-out inside the shared repo-root project directory | headless sessions only (`entrypoint: sdk-cli`), and almost all spawned exactly one agent |
| M18 | Distinct in-tree files edited per correlated worktree job | median a handful; a long tail past ten |
| M19 | Duration of `done` worktree jobs | a large share ran ≥ 20 min **and** touched ≥ 4 in-tree files |
| M20 | `CLAUDE_CODE_SESSION_ID` inside a running job | **equals that job's OWN transcript session id, not the launching session's.** The CLI **sets** the variable, overwriting what `child_env()` passed in |
| M21 | `claude --session-id <uuid>` | exists: *"Use a specific session ID for the conversation (must be a valid UUID)"* |

**The one line M2 and M16 make together:** the `Agent` tool is not exotic. Interactive sessions fan out
routinely. Delegated jobs — the work nobody is watching, i.e. exactly the work a panel exists for —
almost never do.

## 2. Capability audit — what a delegated `claude -p` run can actually do today

**Agent: present and unrestricted.** A delegated job has `Agent` in its tool list (M8) and has *used* it
(M3). Nothing gates it: the launchers pass no `--allowedTools` and no `--agents` (M9), and a default
`~/.claude/settings.json` declares no `disallowedTools`. Under `--permission-mode bypassPermissions` a
subagent inherits the same bypass, so a fan-out job's agents write files without prompting — which is
what made §3 possible and is also the risk §4.3 bounds.

**Task\*: present, and not a fan-out mechanism.** Two different families share the prefix.
`TaskCreate`/`TaskUpdate` take `subject`/`description`/`activeForm` and `taskId`/`status` — that is the
to-do list: a *plan*, one agent. `TaskOutput`/`TaskStop` take a `task_id` and manage work already
backgrounded. **Neither creates a second agent**, so Task\* calls are not evidence of fan-out and must
not be counted as such.

**Workflow: present in the tool list, self-gated, and never used** (M2, M8). Its own contract requires
explicit user opt-in — the keyword, a session setting, or a direct instruction — before it may be
called. For a delegated job **the brief is the only place that opt-in can come from**, which makes
Workflow a strictly brief-side capability.

**What this means for §5:** all three levers live in the brief. There is no code in this repo that
enables, disables, sizes or shapes a delegated job's fan-out, and adding some would be inventing a
control where a sentence already suffices.

## 3. The one job that fanned out

**What it was.** A documentation-accuracy pass over every doc surface, code-first. Launched by a build
session with `--worktree` and `analyze: true`; ended `done`, exit 0, after about an hour. `argv[0]` was
`claude`.

**Why it fanned out.** Its brief contains, in a section headed *Method*, the sentence
**"Fan out with subagents — one per group"**, followed by three constraints: each does code-first
verification, each returns a proposed diff plus what it could not verify, and **no subagent pushes,
branches or opens a PR**. That is the entire causal story. **Fan-out has happened exactly once, and
exactly once has a brief asked for it.** n = 1 is not a proof, but it is the only evidence either way,
and it points one direction.

**What ran.** Nine `Agent` calls (M10), each in its own assistant message, spawned in quick succession,
partitioned by documentation surface: root CLAUDE.md + README · SKILL.md router + mode bodies ·
`seneschal/references` · the scripts docs · SETUP docs + state README · docs router + spec status lines
· CI + test inventory · cockpit/archons/phone · subagent SKILL.md files.

**They ran concurrently.** Each `Agent` call's `tool_result` came back within about a second while the
agent kept working, and the nine execution spans overlap: **peak concurrency 9**, the agent-time sum
compressed several-fold into the wall-clock window (M11). None was given a model override.

**Was the result good? Yes, measurably.** The nine agents edited dozens of distinct in-tree files **with
zero collisions between them** (M13), sharing one worktree with no locking of any kind — the brief's
surface partition was the only thing keeping them apart, and it held. The job opened one docs-only PR,
which merged (M14). For scale: the median worktree job touches a handful of files (M18).

**The one caveat, and it matters for §6.** Reading only the parent transcript, that job appears to have
edited **4** files. The rest are in the subagent transcripts. **A reader that stops at the parent
under-reports a fan-out job by an order of magnitude** — which is both a hazard and the clearest single
argument that the panel should read the subagent files rather than infer anything.

## 4. Which job shapes would genuinely benefit

"Anytime I ask you to build something" is close but not exact, and the difference is worth having:
**what predicts benefit is not "build" — it is a work-list of independent surfaces, or an independent
check.** Real job shapes, profiled from their own transcripts.

### 4.1 Would have fanned out well

| Shape | Why |
|---|---|
| **Retire a subsystem reversibly** — a code core + a wide doc sweep + an independent frontend panel | The core constant and its cockpit mirror are **coupled** and must stay serial. But the frontend panel is a self-contained surface with its own build and test, and a ten-file doc sweep is precisely §3's partition. **Serial core, 2–3 agents around it.** |
| **A one-path fix with a large documentation tail** | The fix itself is a few calls and cannot be split. The doc/reference files can. |
| **A measurement spec** — twenty-odd independent read-only measurements | The clearest case, and the irony is instructive: `jobs-panel-depth-spec.md`'s M-table is a list of questions with no dependencies between them, each answered by its own scan. **This is the pure fan-out shape** — no write conflicts are even possible — and the spec that resulted argued fan-out would show nothing. |
| **A fix whose correctness is contestable** (e.g. which delivery failures prove non-delivery) | The valuable second agent here is not a parallel editor but **a verifier told to refute the fix**. |
| **Several independent cuts over one fixed corpus** | Measurement passes over a fixed corpus have no ordering constraint at all. |

### 4.2 Would NOT have benefited — and these are the honest half

| Shape | Why not |
|---|---|
| **Rebase one PR onto another** | Dozens of Bash calls, zero file edits, zero Reads. Every step depends on the previous one's result. A second agent has nothing to do, and a second agent touching the same index is a corrupted rebase. |
| **A byte-exact in-place redaction** of a leaked secret | Most "edited files" are scratch scripts; only a few tracked files change. One careful serial act. |
| **Build a new module** → its test → wire it into `presence.py` → docs | The chain is the work; only the small doc tail is separable, and the coordination would cost more than it saves. |
| Compute sweeps, CI pollers, a RAG index catch-up | Not agent jobs at all — `python`/`wsl` compute and pollers, no model call in them. |

### 4.3 The bound the evidence actually supports

**A large share of `done` worktree jobs ran ≥ 20 min AND touched ≥ 4 in-tree files** (M19). That is the
candidate population, not the beneficiary population — §4.2 shows serial jobs inside it. It is an
upper bound and this document does not claim more.

Two safety rules the one live example earns, rather than asserts:

1. **Partition by surface, in the brief, and no subagent pushes, branches or opens a PR.** Nine agents
   editing dozens of files in one worktree collided zero times (M13) because the brief assigned
   disjoint surfaces. Nothing in the tooling enforces that; the partition *is* the mechanism.
2. **Fan-out lives inside the worktree the job already has.** `jobs.py --worktree` isolates a job from
   other jobs; it does not isolate an agent from its siblings. The unit of collision is still the
   checkout (`delegated-work-isolation-spec.md` §3.1), and a fan-out job puts N writers in one — so the
   disjointness has to come from the brief, or from `Agent`'s own `isolation: "worktree"` for a job
   whose agents genuinely cannot be partitioned.

## 5. What has to change — the honest answer is "the brief", and mostly nothing else

**Nothing in this repo has to change for a delegated job to fan out.** The capability is present (§2),
unrestricted (M9), and demonstrated (§3). A brief that says *"fan out with subagents — one per group"*
gets nine agents. That is a legitimate finding and it is cheaper than any code, so it is stated first
and it is the recommendation.

What is left is **where the guidance lives**. A rule that only exists one hop away is a rule that does
not run: you look up evidence when you want to argue with a rule, but you obey a rule you were never
told to go and find.

So the question is not *what to write* but *what loads when a brief is being written*. Three candidates:

1. **The `jobs.py` entry of the scripts-level agent guide** (the file that loads on any turn touching
   `seneschal/scripts/`). ✅ **Recommended.** That is where `jobs.py start` is invoked from and where
   every launcher script is written, and it is the natural home of `jobs.py`'s other caller-facing rules
   (`--retry` is the caller's assertion; `--worktree` is per-job isolation). One or two sentences: *a job
   whose work is a list of independent surfaces should say so in its brief and assign one agent per
   surface, disjoint, with no subagent pushing or opening a PR* — plus the pointer to this file.
2. **`background-jobs-spec.md`.** The evidence, the measurement, and the §4 shape table belong there or
   here — not in the router. **Rule in the router, evidence one hop out.** This document is that hop;
   the jobs spec gets a pointer, not a copy.
3. **The `/assistant` command or `modes/chat.md`.** ❌ **Refused.** `modes/chat.md` is one of the largest
   mode bodies and is loaded on nearly every turn; a fan-out sentence there is paid for by every
   conversation that never starts a job.

**What is explicitly NOT recommended: a `--fanout` flag, a required-agents count, or any jobs.py-side
enforcement.** A field a caller must remember to populate is not a mechanism (`metrics.jsonl` stayed
empty; `origin` stayed `{}` on every record until it was auto-stamped). The inverse holds too — **a
mechanism that can only be a judgment should not be given a flag**, because a flag with a default
decides for every job whether its work decomposes, and §4.2 is four shapes where the answer is no.
Fan-out is a property of the *work*, and the only thing that knows the work is the brief.

## 6. The instrumentation question — read it, don't emit it

This is the original question, re-asked under the new premise: **if jobs fan out, what would the Jobs
panel need to show the breakdown?**

`jobs-panel-depth-spec.md` §7.3 designed a sidecar `progress.jsonl` emit protocol, then §7.4 rejected
it on two grounds: nothing would populate it, and populating it would be a prompt-side contract of the
kind that has repeatedly failed. **The second ground survives the new premise. The first does not. And
both are moot, because the breakdown already exists on disk.**

### 6.1 It is already written, by the CLI, for free

Claude Code writes one directory per session, and a fan-out session gets a `subagents/` subdirectory
inside it:

```
~/.claude/projects/<encoded-cwd>/
    <session-uuid>.jsonl                          the parent conversation
    <session-uuid>/subagents/agent-<id>.meta.json {agentType, description, toolUseId, spawnDepth}
    <session-uuid>/subagents/agent-<id>.jsonl     that agent's full conversation
```

Everything an agent tree shows is derivable from those files, and **nothing in the job has to
cooperate**:

| Panel element | Derived from | Verified on the §3 job |
|---|---|---|
| Agent row | one `agent-*.meta.json` | 9 rows |
| Agent label | `meta.description` | *"docs router + spec STATUS lines"*, etc. |
| Agent kind | `meta.agentType` | `general-purpose` ×9 |
| Nesting | `meta.spawnDepth` | all 1 — a flat fan-out, drawn flat |
| Which parent call spawned it | `meta.toolUseId` → the parent's `Agent` `tool_use` id | exact join, 9/9 |
| **Time** per agent | first → last `timestamp` in the agent's `.jsonl` | present on every agent |
| **Model** per agent | `message.model` on its assistant records | present on every agent |
| **Tokens** per agent | `message.usage` per assistant record | present on every agent |
| Aggregate meter | sum of the above + the parent's own `usage` | "N agents · M output tokens" (M15) |
| Concurrency | overlapping spans | peak 9 (M11) |
| Tool counts per agent | `tool_use` blocks in its `.jsonl` | present (M12) |

**There are no phases.** `spawnDepth` is a tree, not a timeline, and nothing in the CLI's record names a
phase. So the honest rendering is **an agent list with a concurrency-aware time axis, not a phases
tree** — a smaller thing than the mock-up and the *true* thing. Inventing a phase layer by grouping on
spawn time would be exactly the decoratively-wrong inference `jobs-panel-depth-spec.md` §8.2 already
forbids.

### 6.2 The crux: correlating a job to its transcript

`child_env()` is `os.environ.copy()` minus `ANTHROPIC_API_KEY` plus `PYTHONUNBUFFERED`, and **the
obvious join does not work**: `CLAUDE_CODE_SESSION_ID` is *set* by the CLI, not read from it — measured
on a delegated job, where the inherited value (the launcher's) was overwritten by the child's own
(M20). `child_env()` does now stamp `SENESCHAL_JOB_ID` (for the background guard,
`background-jobs-spec.md` §3.14), but that is no join either, for a different reason: the CLI records
no environment in the transcript, so an id passed that way is invisible at the reading end.

**But no environment variable is needed, because `--worktree` already spells the job id into the path**,
and Claude Code names a project directory after the cwd (every path separator and `:` becomes `-`):

```
cwd  <repos>/seneschal-worktrees/<job-id>
dir  <encoded repos path>-seneschal-worktrees-<job-id>
```

Measured: every worktree job that ran more than a few seconds resolves to an existing project directory
this way; the only misses died before writing anything (M5). And it is not a lucky sample: **every
private-worktree job is a shell wrapper or `claude` and nothing else** (M6), i.e. the worktree flag is
already a near-exact marker for *"this job is an agent run"*.

**The residual, stated rather than smoothed:** a non-worktree `claude` job lands in the shared repo-root
project directory alongside thousands of other sessions (M7), including the owner's own and the
daemon's warm session. There is no id to join on there. Two ways out, neither taken here:

- **Filter that directory by mtime to the job's `started_at`…`ended_at` window, then match the
  transcript's first user message against the job's `-p` argument.** The first user record in a headless
  run **is** the brief verbatim (verified byte-identical to the argv element), so this is an exact join,
  not a fuzzy one. It costs a windowed directory scan.
- **`--session-id <uuid>`** (M21): jobs.py mints a uuid, records it, passes it on the child argv. Exact
  and cheap — but jobs.py only owns `argv` when `argv[0]` is `claude`, and a common shape is a shell
  wrapper whose `claude` line jobs.py must not rewrite (`preflight_refusal`'s standing rule: **it never
  rewrites the argv**). For the wrapper shape this degenerates into a per-script contract again — the
  thing §5 refuses.

**Recommendation: correlate by worktree path, and let a non-worktree job simply have no breakdown.**
That is not a gap being papered over: fan-out is an agent behaviour, every agent job that lasted more
than a few seconds got a worktree, and the invariant below makes the absence render as nothing at all
rather than as an empty tree.

### 6.3 The invariant, unchanged from the prior spec and now cheaper to honour

> **A job with no `subagents/` directory renders exactly as it does today.** No *Agents* header, no
> empty list, no `0 agents` chip, no placeholder. Absence is not a state worth drawing.

That is nearly every correlated job today, and every `wsl`/`python`/`uv` job forever (§4.2 — no model
call, so no agent and no tokens, and never any). The difference from §7.3 is that honouring it now costs
one `os.path.isdir`, not a cooperating writer.

### 6.4 Two rules a reader must be built with, and one it must not break

- **Render structure and `meta.description`. Never a prompt body, never a result body, never
  conversation text.** A subagent transcript holds everything that agent read, and this framework's
  standing rule is that inbound mail text is data and never becomes anything replayed to a model or a
  person unmarked. The panel needs an agent's *label*, its model, its clock and its counts — none of
  which is content. `description` is the parent model's own words and is a one-line label; cap it in the
  reader and render it as text, escaped, exactly as §7.3 already specified.
- **`~/.claude/projects` is a new filesystem root for a backend that today reads only `state/`.** It is
  outside the repo, it is not this repo's to guarantee, and it can be absent or relocated. It must be
  configurable, and unreadable ⇒ `available: false` per `cockpit/CLAUDE.md`'s tolerant-reader rule —
  never a 500, never a partial tree that looks like a small job.
- **The per-poll cost is a directory `stat` plus, on expand only, a bounded read.** The list poll runs
  every few seconds; a fan-out job's subagent files run to megabytes, so the tree is a
  **detail-endpoint** payload (`GET /api/jobs/{job_id}`, which the prior spec §4 found had no caller),
  never a list-row one.

### 6.5 What survives of the sidecar design

Nothing needs to be built, but §7.3's *reader* rules were right and transfer wholesale: bounded read,
skip an unparseable line without charging it against the limit, type-check every field to `null` rather
than passing it through, truncate every string in the reader, derive counts from what is present rather
than from a total anything asserts. A torn final line is now *more* likely, not less — a killed job's
subagent file is being appended to at the moment of the kill.

**The emit protocol is not merely unnecessary; it would be worse.** A self-reported phase is a claim; a
transcript is a record. `metrics.jsonl` is the precedent and it is exact: a prompt-side contract that
produced zero rows, while the CLI's own transcripts recorded every one of those turns faithfully the
whole time.

## 7. Phases, cheapest first

| Phase | What | Size | Recommendation |
|---|---|---|---|
| **1** | **Two sentences in the scripts-level guide's `jobs.py` entry**, plus a pointer to this file, plus the router row. Nothing else. | a few hundred bytes of prose | **Do this.** It is the whole lever (§5). It changes no code, ships in a docs PR, and is testable the honest way — by whether the next multi-surface job's brief asks. |
| **2** | **The reader.** `cockpit/server/jobs.py` gains an `agents` block on the **detail** payload only, sourced from `<projects>/<encoded worktree cwd>/<session>/subagents/`; the Jobs panel's expanded row renders an agent list — label, kind, model, elapsed, tokens, tool count — with §6.3's absence invariant. | **Estimated** ~150 lines of implementation + ~120 of test, across `cockpit/server/jobs.py`, its test, and the web panel. Estimate, not a measurement | **Build after phase 1 has produced a second fan-out job.** One example is enough to design against and not enough to tune against. |
| **2a** | The prior spec's **phase 2** — give the existing `GET /api/jobs/{job_id}` detail endpoint a caller in the web panel. | ~20 lines | **Prerequisite for phase 2** and worth doing regardless; it is where the agent block lands. |
| **3** | A concurrency-aware time axis (overlapping agent spans, a peak-concurrency figure) and the aggregate meter. | Estimated ~60 lines on top of phase 2 | **Only if the owner asks after using phase 2.** It is the part that is prettiest and least load-bearing. |
| — | Correlating **non-worktree** jobs by mtime window + first-user-message equality (§6.2). | — | **Not now.** Zero known fan-out jobs are affected; build it when one is. |
| — | An emit protocol, a `progress.jsonl`, log sentinels. | — | **Rejected**, §6.5 and `jobs-panel-depth-spec.md` §7.2. |
| — | A `--fanout` flag or any jobs.py-side enforcement. | — | **Rejected**, §5. |

**Why phase 1 is not "just a doc change and therefore free to skip".** It is the only phase that changes
what happens. Phase 2 renders a tree that is empty on nearly every job unless phase 1 works first —
which is the prior spec's own argument, correctly aimed this time.

## 8. Non-goals

- **Any write affordance in the panel.** Unchanged from `jobs-panel-depth-spec.md` §8.2: no start, no
  cancel, no retry, no clear, no "re-run this agent".
- **A phases layer.** There is no phase concept in the CLI record (§6.1) and grouping by spawn time to
  manufacture one is the decoratively-wrong inference already forbidden.
- **Changing what a job IS.** No sub-job registry, no parent/child job graph, no phase state machine in
  the record. The tree is read from files the CLI already writes.
- **Rendering any conversation content — prompt, result, or tool output — from a subagent transcript.**
  §6.4. Counts, labels, models and clocks only.
- **Making fan-out automatic, default, or enforced.** §5. The shapes in §4.2 would be made worse by it,
  and one of them (the rebase) would be made *unsafe*.
- **Per-job cost reporting as a product.** §6.1 shows tokens are recoverable; that is a fact about the
  data, not a proposal for a spend column. The prior spec's §6 recommendation against a token column
  stands on its own reasoning and this document does not reopen it.
- **Retro-fitting a breakdown for the jobs that did not fan out.** There is nothing to read.

## 9. Where this document is uncertain

- **n = 1.** Every claim about what fan-out does to a delegated job's *quality* rests on one job. The
  speedup is a real measurement of that job; it is not a forecast. The candidate population (M19) is a
  shape count, not a prediction that those jobs would have been better.
- **The counterfactual is unmeasured.** Whether the §3 job would have produced a worse PR run serially is
  unknown and unknowable from these records.
- **The nine subagents' zero collisions (M13) may be partly luck.** The brief partitioned by surface and
  the partition held, but nine unsynchronised writers in one checkout is a race that did not happen to
  fire. **`Agent`'s `isolation: "worktree"` option exists and was not used, and it is untested inside a
  delegated job.**
- **Phase 2's ~150/120-line sizing is an estimate**, from the field count in §6.1 and from `_normalize`
  being the single shaping point in `cockpit/server/jobs.py`. Nothing has been implemented.
- **The subagent transcript layout is an observed CLI implementation detail, not a documented contract.**
  It could move. §6.3's absence invariant is what keeps that from being a breakage — a reader that finds
  nothing draws nothing — but a phase-2 reader must be written version-tolerantly and must not assume
  `meta.json`'s key set.
- **Whether `Workflow` is usable in a delegated job is untested.** It is in the tool list (M8) and its
  contract says a brief-level instruction is a valid opt-in, but no job has ever called it. §5's
  recommendation does not depend on the answer.
- **The non-worktree residual (M7) is unbounded, not zero.** Only worktree-named project directories were
  scanned, so "`Agent` almost never" is a statement about **worktree** jobs. A non-worktree `claude -p`
  job could have fanned out invisibly among the shared sessions; M17 makes that unlikely (headless
  fan-outs there almost all spawned exactly one agent) but does not close it.

## 10. Summary

| Question | Answer |
|---|---|
| Can a delegated job run multiple agents? | **Yes.** `Agent` is present and unrestricted under `bypassPermissions`; `Workflow` is present and self-gated; `Task*` is present and is not a fan-out mechanism. §2 |
| Has it? | **Once** (M3), in the one job whose brief asked for it. |
| Did it work? | **Yes** — 9 concurrent agents, a several-fold speedup on agent time, dozens of files edited with zero collisions, a PR that merged. §3 |
| What has to change? | **The brief, and where the sentence telling the assistant to write that brief lives.** No code. §5 |
| Can the panel read the breakdown instead of being fed it? | **Yes, entirely** — agent, kind, model, time, tokens and tool counts are all in `<session>/subagents/`, and the job→transcript join is the `--worktree` path, exact among jobs that ran. §6 |

## 11. Cost — an unbounded-width fan-out has nothing gating it

> **The one cost reading behind this section is in doubt.** It was a single live read of the plan
> meters, and a same-week reset of those meters contradicted it; neither observation was ever
> re-measured. **Do not cite a price from it.** What survives, because it does not depend on the
> numbers at all, is the structural gap below. **Before citing any cost:** re-measure — one bounded
> fan-out, width declared in the brief, meters read before and after.

**What happened.** A brief told a job to fan out with the `Agent` tool "by lens" and to *choose the
lenses itself* — unbounded width, on the most expensive model tier, with every agent reading a whole
repo export. The run was caught live and cancelled well before it finished. Nothing in that sentence
capped the lens count; the width that ran was decided implicitly, never declared or bounded anywhere
the tooling could see.

**The reading, structurally.** The meters suggested the **5-hour session window**, not either weekly
allowance, is the binding wall for a wide fan-out: a shape that never approaches a weekly ceiling can
still exhaust the short window in under an hour. That direction — the short clock binds, not the long
one — is the same one ordinary turn spend shows, and it is the part worth keeping even while the
magnitude is unconfirmed.

**The structural point, not a detail: `jobs.py` counts jobs, not fan-out.** The concurrency caps
(`background-jobs-spec.md` §3.12, 3 soft / 5 hard) gate how many *jobs* run at once. They have no
visibility into how many `Agent` calls happen **inside** one job. §2 already established why: `Agent`
is present and unrestricted in a delegated run, and no launcher passes `--allowedTools` or `--agents`
to narrow it. A wide fan-out inside a single job is therefore invisible to every existing cap, soft or
hard — not a gap in how tightly those caps are set, but a population the caps were never built to see.

**Mitigations identified here; none of them built.**

1. **Cap the lens count explicitly in the brief** that requests a fan-out, rather than leaving "choose
   the lenses yourself" open-ended.
2. **Run lenses as separate sequential jobs** instead of one wide fan-out — trades wall-clock time for
   keeping each job inside the session window on its own.
3. **Run the fan-out on a cheaper model** for lens work that does not need the top tier's specific
   strength, so the same width costs a smaller share of any meter.

**Open question for the owner — raised here, not answered.** Should `jobs.py` gate on a *declared*
fan-out width, given that it structurally cannot observe the *actual* width today (§2, §6.2)? No gate
is built in this document; per §5's refusal to add jobs.py-side enforcement without an owner decision,
it is named as open.

## Router entry

**Status:** SPEC ONLY — nothing built. AMENDS `jobs-panel-depth-spec.md` §7, which it supersedes.

**What it decides:** Whether a delegated job may run several agents, and how the panel would show it.
**The capability was never missing: `Agent` is present and unrestricted in a delegated `claude -p` run**
(no launcher passes `--allowedTools`/`--agents`) **and was used once — in the one job whose brief said
"fan out with subagents".** It ran **9 concurrent agents**, edited dozens of files **with ZERO
collisions between them**, and merged a wide PR — so the prior spec's *"one agent, every time"* measured
its own author's habit and reported it as a property of the system. **Four corrections to the obvious
reading.** The lever is **the BRIEF; nothing in this repo has to change** — and a `--fanout` flag is
refused in BOTH directions: a field a caller forgets is not a mechanism, *and* a flag with a default
decides for every job whether its work decomposes, which §4.2 names shapes it would get wrong. **No emit
protocol is needed because the CLI already writes the breakdown** —
`<projects>/<encoded-cwd>/<session>/subagents/agent-*.{meta.json,jsonl}` yields agent, kind, model,
elapsed, tokens and tool counts with zero cooperation from the job. **`child_env()` is NOT the join and
no env var can be** (not even the `SENESCHAL_JOB_ID` it now stamps): `CLAUDE_CODE_SESSION_ID` is *set* by the CLI over whatever was passed in
(measured), and a transcript records no environment — the join is the **`--worktree` path**, exact
among jobs that ran. **Reading only the PARENT transcript under-reports a fan-out job tenfold.** And
**there are no phases** — `spawnDepth` is a tree, not a timeline — so grouping by spawn time to
manufacture one is the decorative wrongness §8.2 forbids. Phase 1 is **two sentences next to `jobs.py`'s
caller-facing rules**, the only phase that changes anything; the reader waits for a second real fan-out
job. **§11:** an unbounded-width fan-out ("choose the lenses yourself") has nothing gating it — the
concurrency caps (`background-jobs-spec.md` §3.12) count *jobs*, not agents inside a job. The one cost
reading is in doubt, so re-measure before citing it; three mitigations named, none built; whether
`jobs.py` should gate on a *declared* width is left open.
