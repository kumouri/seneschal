# Spec — coupled turns, and the evidence that outlives the session

**Status:** `PARTIAL(phase 0 BUILT)` — `seneschal/scripts/pending_checks.py` +
`state/pending-checks.jsonl`, and **still zero behaviour change: nothing reads it and nothing is
delivered.** Phases 1-3 are designed and **deliberately unbuilt**, per the §8.5 decision (*phase 0
only, then look*) and §6.5's gate of ≥ 2 weeks of rows.

**Shape, copied deliberately from the sibling designs on claims and speech:** lead with a failure read
out of the record, measure before proposing, phase 0 changes no behaviour, name what it will not do,
and keep a section for where the reading contradicted the brief.

**Machines this sits on and must not contradict:**
`seneschal/scripts/presence.py` (the warm session's lifecycle and its wind-down),
`seneschal/scripts/jobs.py` + `state/jobs/` (detached work and the exactly-once completion push),
`seneschal/scripts/job_analysis.py` + `job-origin-routing-spec.md` §3.3 (the delivery ladder —
**this spec builds no new delivery**), the assertions log `state/assertions.jsonl` (what the assistant
has said — the Mouth record, a later component here), the *confidently-wrong* design (claims that
outrun their look — the sibling this one is the *time axis* of), and the harness's durable memory
directory (`~/.claude/projects/<project>/memory/`, which is **not in this repo** — §7.1).

**The ask.** Late in a long, productive evening, the owner observed over Telegram that the session
was about ninety minutes and twenty turns old, quoted the running cost gauge, concluded that a long
session is better and worth it, and asked for it to be specced.

**Their three numbers were exact** (§2.1). **Their conclusion is the one thing in this document that
gets pushed back on**, and §5 is that argument: the variable that paid that evening was not length, it
was **coupling**, and the two come apart in a way that costs money when they do.

Every figure below came from a single read-only snapshot of the live daemon's `state/`, taken with
scratch scripts outside the repo; nothing under `seneschal/state/` was written. One snapshot,
deliberately: these stores grow while a spec is being written, and figures gathered across several
reads would not add up. Owner-specific counts are summarised, not reproduced.

---

## 1. The exhibit

During that evening the assistant told the owner three times that **a tool they had asked for did not
exist**. It had been built together days earlier. It existed, tracked and committed, in the repo the
session was sitting in.

What matters here is the shape of what happened next, read out of `state/warm-transcript.jsonl`'s
`tool_uses` field:

| Step | What ran |
|---|---|
| **T+0** | A recursive `grep` across every repo in the workspace, **launched in the background.** Its output goes to a task file under the harness temp dir, `…/<session_id>/tasks/<id>.output` — **a path keyed by the session id** |
| T+5 min | That file read once, `\| head -20`. Inconclusive; the session moved on |
| **T+38 min** | `Edit` on a durable **memory** file — **an invented explanation for the miss written into durable memory as fact.** Elegant, one moving part, and wrong |
| **T+71 min** | **The same task file read again.** It refutes the explanation |
| T+71 min | `ls` + `git ls-files` confirming the real answer |
| T+72 min | `Edit` on the same memory file — the correction |

> **About seventy minutes** from launch to the read that refuted it. **Over half an hour** during which
> the wrong conclusion sat in durable memory with its own check still outstanding on disk, unread.

**The only reason that correction happened is that the session was still alive to receive it, and
nothing about that was designed.** The refuting read sits inside a turn that started seconds earlier in
reply to a message about something else entirely. **Nothing pointed at that file, nothing was owed,
nothing would have complained. A turn happened to be running and somebody happened to look.**

And it was not comfortable. The evening's longest quiet stretch reached **nearly 90 % of the 20-minute
idle wind-down** while the check was already outstanding and the wrong conclusion was minutes from
being written. A couple more minutes of quiet in the wrong place and the memory file would still say
the wrong thing.

### 1.1 The real asymmetry — not durability, but whether anyone is waiting

The obvious reading is that background work is *unrecorded*. **That is wrong, and the first pass of
this spec got it wrong** (§9.6). The launch, the two reads and both memory edits are all durably
archived: `state/transcripts/` keeps everything, and the day file holds hundreds of rows carrying
`tool_uses`. The evidence was on disk the whole time.

Compare what the two mechanisms actually record:

| | a `jobs.py` job | a command backgrounded inside a turn |
|---|---|---|
| Where the record lives | `state/jobs/<id>.json` | one `tool_uses` entry among hundreds in the current archive day-file, which nothing reads |
| Lifecycle | `created_at` → `started_at` → `ended_at`, `exit_code`, `status` | **none.** There is no "running", no terminal state, nothing to poll |
| Who is told when it ends | the **daemon**, unconditionally, exactly once, on every terminal state, retried until it lands | **nobody.** The output file is written and sits there |
| Return address | `origin.session_id` + `--goal`, and the §3.3 ladder | the temp path's `<session_id>` — an address with no delivery attached to it |
| If the session dies | irrelevant; the push is the daemon's | the result is orphaned in a temp directory named after a session that no longer exists |

> ### The difference is not durability. It is that one of them has **someone waiting for it** and the other is only *a thing that was typed*. A job has a terminal state and an owner; a backgrounded command has an output file and a hope.

That asymmetry is the whole spec. §2.3 measures the good half; §3 is the bad half.

**One thing here is genuinely gone, and it shaped §6.3.** The memory file was **overwritten in place**
twice, so its `modified` time holds only the last write. The *wrong* version's text survives nowhere.
What it said is recoverable only because the corrected version chose to quote it — a courtesy, not a
mechanism.

---

## 2. What is true today — measured

### 2.1 The owner's three numbers are exact, and naming what they measure matters

`state/metrics.jsonl`, `mode: Chat` on every row. The session's age, its metered turn count and the
cost figure the owner quoted all match the metrics rows exactly — the cost was the `session_cost_usd`
stamped minutes before they quoted it. They were reading a live gauge and they read it right.

**But the conversation was not ninety minutes long.** From `turns.jsonl`, the evening's conversation
ran roughly twice that. The ninety-minute window is one *cost run* inside a longer evening:
`session_cost_usd` resets when the CLI process does, and it had reset several times that evening
before the run they quoted. Context grew about fourfold across it.

None of that makes them wrong. It matters because §5's rule is about *the work referring to itself*,
and the self-referring stretch of the evening was closer to three hours than to ninety minutes.

**n = 1, and here is what would falsify it.** One evening, one shape of work, one person, no control.
The finding in §5 is refuted if: a comparably coupled evening produces no catch; or a run of
*uncoupled* turns in one long session performs as well per dollar as the same asks split across fresh
sessions; or the same catch happens in a **short** session, which would mean length was never the
mechanism. **None of those has been run.** This document proposes instrumentation partly so that they
can be.

### 2.2 How long a warm session actually lives

Three deaths, from `presence.py`'s own grounding: a warm session *winds down after ~20 min idle, dies
on any turn error, and is replaced on every code merge.* The first is a code constant — `--idle-min`
**default 20.0**, floored at `MIN_IDLE_MIN = 10.0`. The third is not rare on an actively developed
install: merges land on `develop` several times a day.

`state/session-starts.jsonl` over several days of respawn decisions:

| Respawn reason | share |
|---|---|
| `idle_winddown` | the overwhelming majority |
| `undelivered_reply` | a few percent |
| `daemon_shutdown` (incl. a merge reload) | **none recorded** |

**Do not read that table as "how warm sessions die."** The decision row is written by the daemon on
the *next* spawn, so a death that takes the daemon with it — which is exactly what a merge reload is —
has no surviving writer to record it. Dozens of merges landed in that window and the log shows zero
reload respawns. **It is a census of the deaths the survivor could see.** Reloads are a large share of
real session deaths; that and this table are not in conflict, they are different populations, and this
one is the biased sample.

**A `session_id` is a lineage, not a process.** The `opened` rows in that window carry only a handful
of distinct ids, one of them accounting for most rows. So a per-`session_id` span in `metrics.jsonl`
measures a resumed *lineage* — several CLI processes wearing one id — and this document does not quote
one as a lifetime. **Incidental, and flagged rather than fixed here:** the session-start log holds
several times more `opened` rows than decision rows, which does not match the documented
one-row-per-spawn pairing. The reading that fits is that `system/init` — which the opened-hook hangs
off — is emitted **per turn** on a resumed session, not per spawn. It is one CLI check away from
confirmed and **out of scope here**; recorded so it is not lost.

**What actually ends a long coupled session, in order:** the 20-minute idle timer, a merge, a turn
error — and then, a long way behind, the resource ceilings. That evening peaked around a fifth of the
context window, against a resume gate that refuses at 80 % fill. Cost and context were not what would
have ended it.

### 2.3 The delivery half is already solved, and this spec must not rebuild it

`state/jobs/`, every record with both a start and an end: the median job reaches a terminal state in a
few minutes, but the distribution has a long tail — **about a third ran ≥ 20 minutes, past the idle
wind-down, and around a tenth ran longer than an hour.** So a seventy-minute latency is not exotic: it
lands at about the **p90** for work this system already runs routinely, and it is more than three
times the wind-down of the session that launched it.

Every one of those pushes the owner exactly once on every terminal state, stamped only after the send
landed, retried until it does — and `job-origin-routing-spec.md` §3.3 adds a ladder of *additional*
deliveries on top: `--wake` back into a live warm session, `state/session-mail/<session_id>.jsonl` for
a session nothing can be pushed at, silence when there is no address, because the push already fired.

> **The delivery problem is solved. This spec adds no channel, no push, and no retry.** Its entire
> subject is that the thing delivered is a *bare result* rather than *a challenge to a specific claim*.

### 2.4 The orphan rate — small, and the wrong statistic to design against

Taking "the origin session was still there" as *it served another metered turn after the job ended*,
over the jobs carrying an `origin.session_id`: **roughly one in twenty was orphaned**, and the rate
barely moves whether the window is 20 minutes, an hour, or ever. (Origin ids that appear nowhere in
`metrics.jsonl` — desktop or build sessions the governor never metered — are excluded rather than
guessed at.)

**Two reasons this is a floor and not the number.** First, because `session_id` survives a resume
(§2.2), a lineage that wound down and came back hours later still counts as *alive* — the test is
generous in exactly the direction that under-reports orphaning. Second, and more to the point, **it
can only see evidence that went through `jobs.py`.** The §1 search did not, so the §1 incident is not
in this rate at all, and neither is any other backgrounded command, because none of them are recorded
anywhere (§1.1).

For the orphans it can see, the outcome arrived hours after the origin session's last observed turn.

### 2.5 `state/assertions.jsonl` — read first, and here is what it does not cover

**It is a record of speech, not of claims.** `text` is what the owner actually received, post-shaping:
the log answers "what does the owner believe", and they believe the delivered wording. A row is one
delivered message. Nothing in the schema distinguishes a claim from a greeting, nothing marks a claim
as checkable, and nothing points forward in time.

Three fields look like they might already carry this, and none of them does:

| Field | Populated | What it is actually for |
|---|---|---|
| `item_ids` | **never** (as measured) | The **Mouth queue** item ids delivered in a row — dedup for an at-least-once drain. The queue is unbuilt; these are always empty in the Mouth's phase 0 |
| `superseded` | **never** (as measured) | Items **replaced within the pending set** by a newer one with the same `supersede_key` before delivery. A *dispatch* record, not a "this turned out to be wrong" record |
| `turn_id` | only on a warm turn's reply, once the confidently-wrong design's phase 0 landed it | A correlator, with a derived `scope` beside it. §7.2's composition is unchanged: `turn_id` is not the pairing this register makes |

So the honest answer to *"is this extending `assertions.jsonl` rather than inventing a register?"* is
**yes for the substrate and no for the fields.** It is the right family — same primitive, same
fail-open contract, same writer discipline that separates a store written in code from one that only a
prompt was asked to write (and which therefore never appeared). But `superseded[]` must **not** be
overloaded to mean "refuted": it already means something specific and load-bearing to a component that
is about to be built, and a field with two meanings is a field with none.

### 2.6 The countervailing cost the brief asked for — it is not there

The brief asks for honesty in both directions and names the cost of a long session as *"per-turn price
rises with context length."* **Measured, it does not.** Over every session with ≥ 8 metered turns, the
median per-turn `cost_usd` in the second half of the session was **the same as the first half, to
within a percent**, and about half the sessions got *cheaper* per turn in their second half. Tokens per
turn rise by roughly a quarter and the dollar bill does not move, because the growth is almost entirely
cache reads, billed at **0.1×** (Anthropic's published ratios). Within one session the pattern is
stark: early turns that load context cost several times more than later turns that carry far more of
it. **What costs money is a turn that does work — tool calls, long output — not a turn that carries
history.**

Bucketing every turn by context size shows a U-shape rather than a slope — turn *type* leaking in:
early turns load context and late ones in very long sessions tend to be heavy synthesis. **No cost
model is read out of it.** The defensible statement is the within-session one: *no measured per-turn
cost penalty for context.*

**So the honest countervailing cost is not dollars.** It is (a) the token bill, which is real even
when cheap and is what eventually reaches the context ceiling; (b) the session resume gate, which
refuses at 80 % fill; and (c) the thing this document is actually about — **a long session accumulates
outstanding checks, and every one of them is a thing that gets forgotten if the session ends before it
lands.** Length does not make a turn dearer. It makes the session hold more unfinished business.

---

## 3. The three-layer failure, in the order they have to be fixed

**Layer 1 — orphaned evidence.** `jobs.py` guarantees delivery to *the owner*; nothing guarantees it
to *whoever asked the question*. A command backgrounded inside a turn has exactly one consumer — the
session — and the session is the most volatile thing in the system (§2.2). Its output lands in a temp
path keyed by session id, with no lifecycle and nothing polling it (§1.1). The mitigation already
exists and is one sentence of policy in `presence.py`'s grounding: *anything long-running goes through
a JOB — never a bare background spawn.* **It is in front of every warm turn, and the §1 search did not
follow it.** A prompt-side rule with no code path is the exact failure the confidently-wrong design
measured at a survival time of days — and it is the unflattering half of the incident, so it is said
first. (`background-jobs-spec.md` §3.14's `PreToolUse` guard is the later, code-side answer inside
delegated jobs.)

**Layer 2 — no link from the evidence back to the claim.** This is the load-bearing one, and layer 1
does not fix it. Even had the search been a job, its completion push would have read *"Job 'search the
repos tree' finished — exit 0. Log: …"*. Nothing recorded that it was **checking an assertion already
made**, so the result arrives as news rather than as a contradiction, and reconciling it against a
claim made an hour and a dozen turns earlier is left to whoever reads the push — an act of memory,
performed by the component whose memory is volatile. The file *was* read twice; between those two
reads the conclusion it bore on was written down as fact, and nothing connected the two.

**Layer 3 — the conclusion hardened while its check was outstanding.** The explanation was written to
durable memory at maximum confidence and minimum evidence, and a memory file is not an inert note: it
is the strongest form of prompt-side instruction available short of the persona. A wrong one is loaded
into future sessions as fact. Worse, it was **overwritten in place**, so the erroneous version left no
trace of having existed — the both-sides-gone failure where a claim can be neither confirmed, refuted,
nor undone.

> **This is the confidently-wrong design on the time axis.** That design's defect is a claim whose
> look was narrower than it sounded. This one's is a claim whose look **had not finished yet** — and
> the two share a remedy shape, which is why §4 decides the way it does.

**The razor applies to the diagnosis and not to the incident.** When the first explanation is elegant
and self-flattering, suspect it: the true one here was that nobody looked in the obvious place. The
pull to build an elaborate register is the same pull. **The unflattering reading is: a rule that
already exists was not followed, and a file was overwritten instead of appended to.** §4 has to clear
that bar before it earns anything more.

---

## 4. The design question, and the decision

> **Should a conclusion with an outstanding check be marked provisional until the check lands — and
> what mechanically un-marks it?**

### 4.1 The decision

**Yes, and the mark must be a row written by a code path, not a discipline.** Specifically:

1. **The pairing is recorded at launch, not at landing** — because at landing there is nobody left who
   remembers what was being checked.
2. **The evidence is delivered as a challenge to a named claim**, quoting the claim back, over the
   delivery ladder that already exists. **No new channel.**
3. **A durable write made while a check is outstanding records the outstanding check beside it**, so a
   later reader can see the claim was never confirmed — even if nobody ever came back.
4. **Nothing blocks.** No claim waits for a check, no write waits for a check, no reply is gated.
   Fail-open throughout, the assertions-log contract: a failed append costs the row, never the message
   and never the turn.

Point 3 is the cheap half of the write barrier and it is the one that survives the session dying. A
*true* barrier — refusing the durable write until the check resolves — is **rejected** in §7.

### 4.2 Why not the alternatives

**"Just use `jobs.py` for everything"** (layer 1 alone). Necessary, insufficient, and already written
down. It fixes delivery-to-the-owner, which was never broken, and leaves layer 2 exactly as it is. It
is also the rule that did not fire, and adding emphasis to a sentence that already exists is a remedy
with a record of repeated failure.

**A confidence score on the claim.** Rejected: a specific number generated without measurement is the
defect wearing a badge. *Pending* / *not pending* is a fact about the world, checkable on disk. *72 %
confident* is not.

**A model in the loop reconciling results against prior claims.** Rejected: cost, latency, and
re-centralising judgment in a component that must fail open — the standing objection to a supervisor
model. The join proposed here is a **key match**, not an inference.

**Reusing `superseded[]`.** Rejected, §2.5. It means something else to a component about to be built.

**Detecting the contradiction by reading the owner's replies.** Already measured and already refused:
that join scored around **20 % precision and under 50 % recall**. It is not an instrument.

---

## 5. The finding, stated as a rule — and it is not "long sessions are better"

**The evening's variable was coupling, not length.** Almost every turn in the long stretch depended on
the previous one having been wrong: the claim, the search, the invented explanation, the refutation,
the memory rewrite, the principle named out of it, the write-up that followed. A fresh session at any
point in that chain would have started from the wrong belief, because the wrong belief was the durable
artifact and the doubt was not.

**Twenty *unrelated* asks gain nothing from one session.** They pay a token bill for history none of
them reads, they accumulate outstanding checks nobody will reconcile, and they walk toward a context
ceiling for no return. Nothing measured argues otherwise, and §2.6 is why the argument has to be made
on those terms rather than on price: **a long session is not measurably dearer per turn, so "it costs
more" is not the reason to end one. Having nothing left to refer back to is.**

> ### **Stay while the work still refers to itself.**
>
> **Stay** when a later turn will read an earlier one's result — a hypothesis under test, a search
> outstanding, a claim that might be wrong, a build whose outcome changes the next move.
> **Leave** when the next ask does not read the last one's answer.
> **The tell is not the clock. It is whether anything in flight would be lost by starting over** —
> and §4's register is what makes that question answerable by looking rather than by remembering.

**Honest in both directions, because the brief asked for that and the measurement obliges:**

- The cost of staying is **not** per-turn price (§2.6, no penalty measured). It is the token bill, the
  80 %-fill resume gate, and the outstanding checks.
- The cost of leaving is **everything in flight**, and today that cost is invisible: nothing records
  that anything was in flight (§1.1).
- **The short-session mitigation is the same machinery, and this is the point.** What makes a short
  session safe is not staying longer — an owner cannot always give ninety minutes, let alone three
  hours, and that is not a thing to require of anyone. It is that the correction must find them when
  the session that erred is gone. §4's register plus the existing ladder is that, and it is why this
  document is not a recommendation to sit at the keyboard.

---

## 6. The proposal

### 6.1 Phase 0 — record the pairing, and nothing else

**When a session states a conclusion that a running job could refute, write one row pairing them.**
`state/pending-checks.jsonl`, append-only, `seneschal.pending-check/1`, one row at launch:

- the **job id** (the evidence), and its `--goal` line, which `jobs.py` already requires
- the **claim**, as text — what would be wrong if the check comes back the other way
- **where the claim went**: the `assertions.jsonl` row (by `at` + exact text — the only join available
  when this was written, and still the only one for a claim made outside a warm turn's reply, which is
  the only place `turn_id` is written; wiring the two is §7.2 and phase 1's), and any durable artifact
  the claim was written into, by path
- the **origin session id**, which `jobs.py` already stamps from `$CLAUDE_CODE_SESSION_ID`
- the **falsifier**: one line saying what result would refute the claim, written *before* the answer
  is known

**The evidence must be a job id, and that is deliberate rather than restrictive.** A pending check
whose evidence is a background command has nothing to key on — no id, no terminal state, nothing to
reconcile against (§1.1) — so requiring the id makes the grounding's existing job rule *structural*
instead of advisory: the row cannot be written for work that was not run as a job. That is the same
move `job_analysis.py`'s schema makes, where "point, don't diagnose" is enforced as **a shape a
diagnosis cannot fit into** rather than as a rule anyone has to remember. It also means the register
inherits `jobs.py`'s lifecycle, its exactly-once push and its return address for free, which is most
of why §6.2 is a wording change rather than a mechanism.

**Zero behaviour change. Nothing reads it.** Its job is to answer the questions this document could
not: how often is a claim outstanding when a session ends; how long do checks actually run against
their session's life; how many are never reconciled by anyone.

**Why a new file rather than a field on `assertions.jsonl`.** A pending check is not speech and is
frequently not spoken at all — the strongest case for it is a belief acted on silently. It also has
its own lifecycle (open → resolved → orphaned) and a different retention question, and the assertions
row is deliberately one-delivered-message-shaped. It stays in the same family — same primitive, same
`NEVER raises` contract, same `state/` write discipline (append-only JSONL, never truncate-written).

**The honest deviation, named rather than hidden.** "Is this claim refutable by that job?" is a
judgment, so the writer is a CLI (`pending_checks.py record`) invoked from a turn, which is
prompt-side — exactly what the confidently-wrong design warns about. **The mitigation is that the row
is not the only detector:** `jobs.py --goal` is already mandatory and already in code, so a phase-1
sweep can count completed jobs whose goal is check-shaped and that carry no pending-check row, and
report the gap. **A disk-side check on a prompt-side contract.**

**As built — three things this section did not specify, each recorded rather than folded in
silently.** The row is exactly the five things above; these are how they are enforced, and the full
per-field contract lives in `pending_checks.py`'s module docstring.

1. **`refusal()` is a separate predicate, and the refusal follows `reminders_acks`' line: fail-open
   on unknown, fail-closed on known-bad.** A job id naming no record in a jobs directory that
   *could* be read is refused with the reason on stderr and `exit 2` — that is a fabricated id, or
   work that was never a job. A jobs directory that is **absent** (a fresh checkout, a `--state-dir`
   pointed elsewhere) is *unknown*, so the row is written and flagged `job_verified: false` rather
   than lost. Losing the pairing because this module could not see a directory would be it deciding
   its own blind spot is the caller's fault.
2. **`falsifier_blind` measures "written before the answer is known" instead of asserting it.** Code
   cannot see intent, but it can see the clock: a row written when the job was already terminal
   records `false`. **Flagged, never refused** — refusing would lose the pairing for a job that
   finished in the seconds before the turn got round to writing it, and losing the measurement is
   worse than an honest caveat about it. `stats()['falsifier_not_blind']` counts them.
3. **`resolve` ships in phase 0, because §8.4's retention needs it.** The retention decision is
   *unresolved kept indefinitely, resolved pruned at 90 days*, and with no way to record a resolution
   that decision would be prose with nothing implementing it — a shape this repo has paid for before
   (an outbox drain with no owner, `notion-write-behind-outbox-spec.md` §7). It is still zero
   behaviour change: a resolution is an **append** for the same `check_id`, never a rewrite — the
   shape of §8.2 — and nothing reads the state. `orphaned` is the lifecycle's third state and
   **nothing writes it**; §6.4 derives it.

**Not wired into Dream, deliberately.** Nothing in this file can reach 90 days for the first three
months after install, so the sweep lands with phase 1 rather than as a nightly call nothing exercises.

### 6.2 Phase 1 — deliver it as a challenge, over the ladder that exists

When a job with a pending-check row reaches a terminal state, `jobs.notify_text` — which already
composes the push — additionally names **the claim it bears on**, and the §3.3 ladder routes it
unchanged:

> *Job "confirm whether the watcher exists" finished — exit 0. **This was checking: "the tool does not
> exist" (said 20:07, written into a memory file). Read the result before trusting that.***

Three invariants, inherited rather than invented:

- **Routing may only ADD a delivery** (`job-origin-routing-spec.md` §3.3). The completion push stays
  unconditional, exactly once, on every terminal state. This changes *wording*, never *whether*.
- **A raise here must not cost the push.** `notify_text` runs inside reconcile's per-record `try`, so
  a malformed pending-check row would silence the job's only notification. Every malformed shape gets
  a test, per the jobs module's standing rule.
- **Rung 3 already covers the dead session.** `state/session-mail/<session_id>.jsonl` is the pull
  mailbox for a session nothing can be pushed at; a challenge files there like any other mail, and the
  owner's Telegram push has already fired regardless. **Nothing new is needed for the orphan case.**

### 6.3 Phase 2 — the cheap write barrier

**A durable write made while a check is outstanding records the check beside it.** For a memory file
that is one line in the body — *"Pending: job `<id>`, launched `<time>`, checking whether `<claim>`"*
— removed by the write that resolves it. A later reader sees, without any machinery, that the claim
was never confirmed.

**And a correction is an append, not an overwrite.** In §1 the wrong version is unrecoverable because
the file was rewritten in place (§1.1). The journaling design already decided this exact question in a
neighbouring domain: *a correction is **append-only supersession**, never an edit or a revert.* §8.2
extends it to memory files.

### 6.4 Phase 3 — the sweep

Dream reports: checks still open past their job's terminal state; checks whose origin session never
came back; durable artifacts still carrying a pending line whose job finished days ago. **A report,
not an enforcement** — a thermometer, not a thermostat.

### 6.5 Phases

| Phase | What | Gated on |
|---|---|---|
| **0** | `state/pending-checks.jsonl` + `pending_checks.py record`. **Zero behaviour change** — **BUILT** | nothing |
| **1** | `notify_text` names the claim; the §3.3 ladder carries it | ≥ 2 weeks of phase-0 rows, and §8.1 |
| **2** | The pending line in a durable write; bitemporal correction | §8.2 |
| **3** | The Dream sweep | phase 1 |

**No phase past 0 should be built until that data exists.** This document is n = 1 and says so twice.

---

## 7. Deliberately not built

- **A new delivery channel, push, retry or mailbox.** All three rungs exist and work. §2.3.
- **A hard write barrier that refuses a durable write until its check resolves.** It would block the
  common case (most claims have no outstanding check) on the machinery for the rare one, it fails
  closed in a system whose whole posture is fail-open, and a barrier that can wedge a memory write is
  worse than a wrong memory. The cheap version records; it does not gate.
- **Blocking or delaying a reply on a pending check.** Fail-open always.
- **A model reconciling results against prior claims.** §4.2.
- **Overloading `assertions.jsonl`'s `superseded[]`.** §2.5.
- **Detecting the contradiction from the owner's replies.** Measured at around 20 % precision. §4.2.
- **A confidence score.** §4.2.
- **Retro-fitting pending checks onto history.** They were never recorded (§1.1); inventing them
  would be this document's own failure mode.
- **Any change to what a turn checks, or to how long a session lives.** Not one tool call is added,
  and `--idle-min` is untouched. The subject is what happens to a check already in flight.
- **Fixing the `opened`-per-turn observation** (§2.2). Flagged, out of scope, one CLI check from
  confirmed.

### 7.1 Two boundaries this spec cannot cross, named here rather than discovered later

**A desktop session's orientation is only as good as its command file.** Anything that makes a desktop
`/assistant` session read its pending checks at orientation is an edit to the command file that session
loads (`.claude/commands/assistant.md` here; an owner's own host-side copy, if they keep one, is
outside any PR — the same wall `job-origin-routing-spec.md` §3.3.1 names). Until that edit is made, a
desktop session behaves exactly as it does today — which is rung 4, which is the owner still getting
the push.

**Durable memory is not in this repo either.** The files in the harness's memory directory are written
by the harness's memory tool, outside version control. §6.3's pending line and §8.2's bitemporal
correction are therefore **conventions in prose**, enforceable only by the same prompt-side contract
this document is otherwise sceptical of. **That is a real weakness and it is not pretended otherwise.**
The mitigation is that the *machine-readable* half — the pending-check row naming the artifact by path
— lives in `state/` where a sweep can reach it, so a memory file carrying a stale pending line is
detectable from outside even though the line itself is written by hand.

### 7.2 Where this composes with the confidently-wrong design

Same family, different axis, and they should not be built to collide:

- That design's **phase 0** (`turn_id` on the assertion row) is what would let a pending check point
  at a claim **by key** instead of by `at` + exact text. This spec does not duplicate it and does not
  require it; it degrades to the text join that design measured as exact within the joinable window.
- That design's **phase 2** (the derived scope clause) answers *"how wide was the look?"*. This one
  answers *"has the look finished?"*. A claim can fail either way and the clauses do not overlap.
- **Neither may grow a second capture layer.** `assertions.jsonl`, `turns.jsonl`,
  `warm-transcript.jsonl` and `state/jobs/` already hold four sides of this. Phase 0 here adds one
  file that records a *relationship* nothing else can express — not a fifth copy of the conversation.

---

## 8. Decisions

**All five questions were decided by the owner together, the same evening the spec was written.** Four
took the recorded default (`rec`); **8.2 was decided with an addition** that changes the mechanism
rather than merely confirming it. Each subsection keeps its original reasoning and carries the
decision at the top.

### 8.1 What counts as a claim worth pairing? — **decided: rec**

**The default: a claim the session is about to *act on*, where a running job could come back the other
way.** Not every statement, not every job — the pairing is worth writing exactly when a wrong answer
would send the next turns somewhere wrong, which is §1's shape. A looser bar produces a register nobody
reads; a tighter one misses the case where the claim seemed obviously right, which is precisely when
to check. **Phase 0's data should set this, not a guess** — the whole reason phase 0 exists.

### 8.2 Does append-only correction extend to durable memory? — **decided: YES, AND BITEMPORALLY**

**The addition is the mechanism, and it is stronger than the default.** The default was journal-style
append-only — a correction is a new entry, never an edit. The decision is instead for a **bitemporal
model**: every record carries `valid_from`, `valid_to` (NULL = still current) and an optional
`supersedes` pointer, so a query answers **what is true now** while the superseded record stays
**fully retrievable and flagged, never deleted**.

Applied to durable memory this is a strictly better fit than plain append-only, for the reason §1
demonstrated: an append-only memory file preserves the wrong claim but leaves a reader to work out
which version is live. Bitemporal makes *currency* a queryable property rather than a reading-order
convention — the corrected claim is current, the wrong one is retrievable and marked superseded, and
the link between them is explicit rather than implied by adjacency.

**Consequence for the memory files specifically:** in §1 the wrong diagnosis was **overwritten**, so
the wrong version is gone and only this document records that it was ever made. Under this decision
that overwrite is the defect, not the tidying. Implementation is not specified here — whether memory
files grow a front-matter validity interval, or the pairing register carries the supersession edge, is
a phase question — but the *semantics* are fixed: **supersede, never overwrite; superseded stays
readable.**

*(Original reasoning, retained:)* The journaling design already decided it for journal entries: *a
correction is a new entry, never an edit or a revert.* The same failure happened one directory over, in
a memory file, and the wrong version is gone. The argument that won there (a rewrite destroys the
evidence that the claim was ever made) is the same argument, now backed by an instance. It is the
owner's call because it makes memory files grow rather than stay tidy, and the owner is the one who
reads them.

### 8.3 Should the assistant be *told at session start* what it left outstanding? — **decided: rec**

The pending register makes it possible: a new session could open with *"three checks were outstanding
when the last session ended."* **The default: yes, on the daemon surface, after phase 1 has data** — it
is the piece that makes a *short* session safe, which §5 says is the real prize. Two things argue for
waiting: it adds tokens to every cold start whether or not anything is pending, and the desktop half
depends on the command file (§7.1). It would also want to be the same orientation read the session
mailbox already uses rather than a second one.

### 8.4 Retention — **decided: rec**

Four numbers already live in this family — `assertions.jsonl` 30 days, `turns.jsonl` **never**, an
offer ledger 400 days, `session-mail` swept nightly. **The default: keep an *unresolved* check
indefinitely and prune a *resolved* one at 90 days.** An unresolved check is the artifact that says
something was never finished, and pruning it is deleting exactly the finding. But this is a fifth
reading of a question this repo keeps re-answering, and unifying them would be a better outcome than a
fifth number.

### 8.5 Is this worth building at all? — **decided: rec**

**The measured orphan rate is about one in twenty (§2.4), and the §1 incident is not in it** — because
the mechanism that failed leaves no trace, so the rate is a floor of unknown tightness. That is an
honest reason to instrument (phase 0) and a poor reason to build phases 1-3 at once. **The default is
phase 0 only, then look**, and the pull the other way deserves naming plainly: the thing this catches
is a wrong belief hardening into durable memory, which is expensive out of proportion to its frequency
because a memory file is loaded into future sessions as fact. **Rare and expensive is exactly the
profile that justifies cheap instrumentation and does not yet justify machinery.**

**Resolved rather than open:** *is the fix to tell the assistant to be more careful about outstanding
checks?* **No.** The grounding already says *anything long-running goes through a JOB — never a bare
background spawn*, in front of every warm turn, and in §1 it did not fire. That class of remedy has a
measured survival time of days across repeated attempts. It is a settled experiment, and this document
exists because it was run again.

---

## 9. Where the reading contradicted the brief

The brief asks for this section. It was right about the shape of the failure and wrong or incomplete
about five things, three of which changed a design decision — and the sixth entry is this document's
own, because it committed the failure it describes while describing it.

1. **The countervailing cost the brief names does not exist in the data.** *"Per-turn price rises with
   context length"* — measured over every multi-turn session, the second half costs the same per turn
   as the first, and about half got cheaper (§2.6). The token bill rises and is almost all cache reads
   at 0.1×. This changed §5: the argument against twenty unrelated asks in one session cannot be made on
   price, so it is made on outstanding checks and the context ceiling instead.
2. **The brief's two timings were both slightly long, and both are now exactly sourced** from
   `warm-transcript.jsonl`'s `tool_uses` (§1). Neither error changes anything, and both are corrected
   because a document arguing about evidence does not get to round.
3. **The brief's framing of layer 1 — "a backgrounded command has no delivery guarantee" — is right,
   but "orphaned evidence" understates and misplaces the defect.** The evidence was not lost. It was
   written to a session-keyed temp file, durably archived in `state/transcripts/`, and **read twice**.
   What it lacked was a lifecycle, a terminal state and an owner — the things that make a `jobs.py`
   record something *waited for* rather than something typed (§1.1). This rewrote §1.1 and §3 layer 1,
   and it is why §6 records a **pairing** rather than proposing a capture layer for background output.
4. **`assertions.jsonl` has the right shape and the wrong fields.** The substrate: yes. The fields:
   `item_ids` and `superseded` are **empty on every row** and are both reserved for the unbuilt Mouth
   dispatcher, with `superseded` meaning something specific and different (§2.5). Reusing it would have
   looked like reuse and been a collision.
5. **The owner's three numbers are exact.** Age, metered turns and cost all land (§2.1). What is *not*
   exact is the framing: the conversation was about twice as long, and the ninety-minute window is one
   cost run inside it.
6. **This document's own instrument failure, kept in because it is the cheapest possible
   demonstration of §3.** The first draft of §1.1 stated, in the register of a measurement, that
   *"`warm-transcript.jsonl` holds three event kinds and no tool-call events anywhere in the file"* —
   and concluded that a backgrounded command is recorded nowhere. **`tool_uses` is a FIELD on
   `assistant_output` rows, not an event `kind`.** It is present on most rows, it holds the tool name
   and a truncated input, and it contained the entire timeline in §1 — which the second pass then read
   straight off it. The draft had counted `kind` values and reported the absence as a finding: *a null
   result is not a finding until you know the search could have found it.* **The draft conclusion was
   strictly more dramatic than the truth**, which is the tell the razor names, and the corrected version
   is a better spec: the defect is a missing lifecycle, not missing data.

**Incidental, found on the way, and not fixed here:** `state/session-starts.jsonl`'s `opened` rows do
not match the documented one-row-per-spawn pairing (§2.2). And the respawn-reason table cannot see
reload deaths at all, because the daemon that would write the row is the thing that died — so its
`idle_winddown` majority must not be quoted as the death distribution.

**One correction to this document rather than to the brief:** §1's exhibit is the vivid part, but the
load-bearing evidence is §2.3 — **about a third of jobs already outrun the 20-minute wind-down and
around a tenth run past an hour.** The §1 latency was not a freak. It was an ordinary one, landing on
the one class of work that nothing waits for. If only one section survives review, it should be that
one.

## Router entry

**Router status:** **PHASE 0 BUILT** (phases 1-3 designed, deliberately unbuilt — the §8.5 decision is
*phase 0 only, then look*). **What it decided:** Evidence that outlives the session that asked for it.
**The defect is not lost data — it is a missing lifecycle:** a `jobs.py` job has a terminal state and
an owner that pushes on it; a command backgrounded inside a turn has an output file and nobody waiting.
Pairs a claim with the check that could refute it, over the **existing** §3.3 ladder. **About a third
of jobs already outrun the 20-min wind-down** (§2.3), and §2.6 kills the obvious objection: **no
measured per-turn cost penalty for context**.
