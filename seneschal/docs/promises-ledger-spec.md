# The promises ledger — commitments the record holds, not the turn

**Status:** `PARTIAL(phase 0 BUILT — promises.py + state/promises.jsonl; phases 1-3 unbuilt)` — phase
0 is the store and the tool (`seneschal/scripts/promises.py`, covered by
`seneschal/scripts/test_promises.py`), and it is deliberately the smallest thing this spec can start
with: **nothing reads it yet.** No grounding change, no `/assistant` prompt change, no mode-prompt
instruction to append, no daemon task, no Dream sweep, no nag. Phases 1+ (§4) are designed, not built,
and are the owner's to approve.

**Parent pattern:** `background-jobs-spec.md` — *"the record, not the turn, holds the promise"* is the
sentence `jobs.py` already proved once, for one kind of promise. Siblings whose shape this spec copies
(measure before proposing, phase 0 changes no behaviour, name what it will not do): `mouth-spec.md`.

---

## 1. The founding failures

1. **The job cap leaves the assistant no way to promise at all.** `jobs.py` is the only promise
   mechanism in this tree. When its per-session soft cap (`background-jobs-spec.md`) refuses a new
   job, an owed follow-up has nowhere to live — a hand improvises a store Tasks row as a stand-in. It
   works *because* a hand improvised, which is exactly the failure a ledger exists to remove: reaching
   for the stand-in requires someone to remember that door exists and walk through it by hand, at the
   moment the one mechanized door is full.
2. **The standing prompt rule is the same contract, in prose, for one promise type.** The chat
   grounding already says: never say *"I'll let you know when it's done"* or *"I'll circle back"*
   unless a job id exists, because otherwise nothing anywhere will wake to keep that promise
   (`seneschal/modes/chat.md` rule 10). **A rule that only covers promises which happen to spawn a
   process is the gap this step closes** — correct for a narrower problem than the one the assistant
   actually has, and the measurement below (§2) puts a number on how much narrower.

`background-jobs-spec.md`'s reconcile/notify path is the one promise type that already works end to
end, and this spec's whole job is to say precisely which of its properties generalise (a record, owned
outside the turn, that survives the hand that made it) and which are process-specific (a PID, a log
file, a terminal exit code — none of which a promise like "I'll check that tomorrow" has).

---

## 2. What was measured before this was designed

The design was sized against a month of real conversation history from a production deployment: every
assistant-side, human-origin turn in `state/turns.jsonl` (the rows `turns.py` classifies as
`origin == "human"`), with `state/assertions.jsonl` over the same window.

### 2.1 How many promise-shaped statements does an assistant actually make?

The turns were scanned, case-folded, for `"i'll "`, `"i will "`, `"i'm going to"`, `"circle back"`,
`"let you know"`, `"keep an eye"`. Roughly a quarter of assistant turns matched at least one, and
`"i'll "` alone accounted for almost all of the hits — the other phrases were near-absent.

**A raw hit is not a promise, and the gap between them is the whole finding.** A stratified sample of
40 hits (evenly spaced across the whole window, not one week of it) was read in context and
hand-classified against the bar in §2.3:

| Class | n / 40 | What it looks like |
|---|---:|---|
| **Genuine, trackable promise** | **12 (30%)** | *"I'll push you the outcome either way"* (job-backed); *"I'll come back to you with it when it lands — the job holds that promise"*; *"I'll clean them up when you're not mid-thought"* (a chore, no job) |
| **Conditional offer — not yet a promise** | **16 (40%)** | *"say the word and I'll open a job for it"*; *"if you tell me which mornings, I'll write them"* — contingent on a reply that had not yet arrived |
| **Not a promise at all** | **12 (30%)** | a figure of speech; a job's own log text quoted back (*"I'll pick this up automatically"* — not the assistant's speech act); a reaction to something; a report of an existing automatic mechanism (*"I'll reload myself onto it shortly"*) |

At that 30% rate the deployment was making **several genuine promises a day** — a real, non-trivial
rate. Naively regexing "I'll" overcounts by roughly 3.3×; the count that matters for sizing a ledger
is the 30%, not the 100%.

**Of the 12 genuine promises, only 2 named their own job id in the text.** A few more were plausibly
job-backed from context but did not say so. That is founding failure #2 reproduced in the data: even on
a day nothing goes wrong, most real commitments are not visibly carried by anything durable — whether
one turns out to be job-backed is a fact about phrasing, not about mechanism.

**Whether a promise was kept was deliberately not estimated.** A chronological/text-match instrument
for "was this reconciled later" has been measured elsewhere as scoring about one genuine hit in dozens
of flagged candidates. Building a second, unvalidated instrument here and reporting a rate from it
would be exactly the failure this spec exists to prevent. One case was hand-checked in depth instead —
a chore promise followed forward through the next several hours of replies — and nothing mentioned it
again: not done, not pending. **That is the honest state of the evidence: nobody reading the
transcript can tell whether that promise was kept, dropped, or never surfaced again** — the gap this
ledger exists to close, demonstrated rather than assumed.

### 2.2 What already tracks a commitment, and where they disagree

| Store | What kind of commitment | Who reconciles it | What happens when it is dropped |
|---|---|---|---|
| `state/jobs/*.json` (`jobs.py`) | A background process's own attempt, from launch to terminal state | The daemon's `jobs.reconcile`, every ~5 s tick | **Never silently** — every terminal state pushes exactly once, retried until it lands. But a job is an *attempt*, not the *work* — a failed job still leaves the underlying promise open with nothing tracking that fact |
| ⏰ Reminders (the store) | A scheduled behavioural cue (habit, one-off, todo-typed) | The reminder fire-time engine + the ack ledger | An acked reminder just stops firing — there is no "did the thing actually get done" escalation once the nag itself is silenced |
| Tasks (the store) | The owner's own task list, with a whose-move field | Nobody, structurally | Becomes an invisible pile of open items with no status vocabulary for "abandoned" |
| `state/open-loops.json` (`loops.py`) | A tracked work item with a **required** `terminal_state` | Whoever calls `resolve`/`drop`/`hold` | A `drop` requires `--because` — the reason IS the record; but nothing writes to it from a conversational "I'll" either — it is a register for named work items, not a promise-capture surface |
| `state/carry-over.md` | Freeform prose of what's open, rebuilt each Dream run | The assistant, by hand, at Dream | No real status vocabulary, so a dropped item there is indistinguishable from one that was never written down |
| `state/run-log.md` | A record of what a run **did** | Nobody — it is retrospective | Answers "what happened", not "what is still owed" |
| `state/assertions.jsonl` (the Mouth) | What the assistant **said** to the owner, verbatim, across every surface | Nobody | Answers "what does the owner currently believe" — a different question from "what is the assistant still on the hook for". A promise sits inside an assertion's `text` exactly as invisibly as inside a raw turn |

**The pattern across every row but the first**: only `jobs.py` treats a commitment as a first-class,
durably-owned record with an unconditional reconciliation guarantee. Everything else either has no
reconciler at all or reconciles a different axis entirely (a reminder reconciles *"did the owner
acknowledge the nudge"*, never *"did the assistant do what it said it would"*). The promises ledger is
not another place to write things down — it is the first place built specifically to answer the one
question none of the others can: *is the thing the assistant said it would do still owed, and by whom.*

### 2.3 What is a promise, precisely? — the definition, tested against real lines

The hard part is the definition, not the file format. The bar, in one sentence, borrowed from
`loops.py`'s own membership test applied to speech instead of to a work item: **a promise is a
first-person, unconditional statement committing the assistant to a nameable future action, whose
completion is a discrete event a later turn could check.** "Can you name the event that would close
this?" is `loops.py`'s `terminal_state` refusal, generalized — if no turn could ever say "yes, that
happened" or "no, it didn't", it is not a promise this store can resolve, whatever else it is.

Four exclusions, each earned by a real line from §2.1's sample:

1. **An offer contingent on the owner's reply is not yet a promise.** *"Say the word and I'll open a
   job for it"* — 16 of the 40 sampled hits. If the owner says yes, **that reply** is what should be
   recorded, not the offer that preceded it; an unaccepted offer belongs nowhere.
2. **A quotation of someone else's words is not the assistant's speech act.** A job's own log text
   quoted back inside a reply contains an "I'll" that is not a commitment. A regex over raw text cannot
   tell the two apart; a reader in context can, which is why phase 0 records what a caller *decided*
   to record rather than trying to detect a promise mechanically.
3. **A standing behavioural disposition with no discrete completion event is not this store's
   business.** *"I'll stop phrasing it like that"* — first-person, about the future, and **with no
   moment a later turn could point at and say "resolved."** It is an ongoing habit change, the opposite
   of `terminal_state`.
4. **A report of an already-automatic mechanism is not a new commitment.** *"I'll reload myself onto
   it shortly"*, describing the daemon's scheduled self-update, states a fact about the system.

A belief (*"I think X"*) and a bare question carry no commitment either.

**What this bar deliberately does NOT try to do: classify automatically.** Phase 0 accepts whatever
text a caller hands it — it validates that `text` is non-blank and nothing more. The definition above
is prose a future caller (a mode prompt, a grounding rule) applies at the point a promise is made. A
classifier that is "mostly right" about what counts as a promise is a classifier that is confidently
wrong about the rest, in the one store whose entire value is that a caller can trust what it holds.

---

## 3. Design — phase 0, and what it deliberately does not do

### 3.1 The record — `seneschal.promise/1`

`state/promises.jsonl`, append-only, one JSON object per line — the same primitive as `mouth.py` and
`turns.py`, for the same reason (a sub-4 KB line append is the one multi-writer-safe operation
available with no lock), via the shared `stateio` jsonl helpers rather than a bespoke reimplementation.

```json
{
  "schema": "seneschal.promise/1",
  "id": "20260101-220512-a3f1",
  "promised_at": "2026-01-01T22:05:12Z",
  "origin_hand": {"speaker": "daemon", "session_id": "…", "source": "build", "cwd": "…"},
  "text": "I'll open a PR for the config fix",
  "due": null,
  "job_id": null,
  "status": "open"
}
```

A resolution is a **second, partial row for the same `id`** — never a rewrite of the first:

```json
{"schema": "seneschal.promise/1", "id": "20260101-220512-a3f1", "status": "kept",
 "resolved_at": "2026-01-02T14:02:00Z", "reason": "the fix PR merged"}
```

- **`text` is verbatim and uncapped**, for the same reason `turns.jsonl`'s is: a ledger that
  paraphrased what was promised is only as trustworthy as the paraphrase.
- **`origin_hand` reuses `mouth.origin_hand()`** rather than inventing a second notion of who is
  speaking — it carries the same `speaker`-is-the-field-to-trust / `source`-reads-`build`-for-a-warm-
  turn caveat `mouth-spec.md` §9.4 establishes. `promises.py` never re-derives identity.
- **`job_id` is a reference, never a duplicate.** If a promise is carried by a `jobs.py` job, this
  field points at it; the promise's own `status` still exists independently, because a job's terminal
  state answers "did the process finish", not "was the underlying commitment kept" — those can diverge.
- **`status` is `open | kept | dropped`, and it is the one vocabulary this module validates
  strictly**, unlike `mouth.py`'s loosely-validated `kind`/`surface`. A `resolve` call naming anything
  else is refused outright, writing nothing — a typo'd status would otherwise leave a promise silently
  un-resolvable.
- **Unknown fields are tolerated, never rejected**, so a later phase can add a field without breaking
  a reader of rows written before it existed.

### 3.2 The module and its CLI

`seneschal/scripts/promises.py`, stdlib only: `record_promise()` / `list_open()` / `list_all()` /
`resolve()`, plus a `record` / `list-open` / `resolve` CLI with `--json`. `record_promise` and
`resolve` **never raise** — the same contract `mouth.record_assertion` and `turns.record_turn` are
built to: a failed append costs the row, never the reply or the decision that made the promise true,
and no caller wraps either in a try/except.

**Coverage: `seneschal/scripts/test_promises.py`** across the record's fail-open contract (a squatted
path, an unreadable store, a malformed line all degrade to an empty read or a `None`/`False` return,
never a raise), the fold-on-`id` semantics `resolve` depends on (a resolution is additive, the
original `text` survives, and the newest row for an id always wins), the strict `status` refusal, and
the CLI's `--json` round trip.

### 3.3 What phase 0 deliberately does not do

- **Nothing reads `state/promises.jsonl` yet.** No caller in `presence.py`, no mode prompt, no
  grounding line, no Dream step, no daemon task. This is the artifact every later phase reads, landed
  first, exactly where it cannot break anything.
- **No retention or pruning.** A promises ledger is a store of discrete, bounded events (one row per
  commitment, one more on resolution) rather than a per-message log; its growth rate is nowhere near
  `assertions.jsonl`'s, and inventing a retention rule before there is a real volume figure would be
  guessing a threshold ahead of the instrument. Retention stays undecided until phase 1 gives it
  something to measure against.
- **No classifier, no automatic detection of what counts as a promise** — §2.3.
- **No connection to `jobs.py`, `loops.py`, Reminders, or Tasks.** `job_id` is a field a caller may
  fill in; nothing here reads a job's terminal state, writes to `open-loops.json`, or touches the
  store. Wiring those together is a judgment call §2.2's table informs, not one this phase makes.

---

## 4. Phases — each shippable alone, only the first built

| Phase | What | Gated on |
|---|---|---|
| **0 — BUILT** | The store + the tool, as above. Zero behaviour change | — |
| 1 | Grounding + the `/assistant` command + mode prompts instruct: on saying "I'll X" (per §2.3's bar), call `promises.py record`. Orientation reads open promises | phase 0's real volume figure, and the owner's decision on whether the write-time contract is enforced **in code** — prompt-only logging contracts have a track record of producing zero rows |
| 2 | Dream sweeps promises stale past some threshold into a nag, in the Wrap/Brief digest — never a second interrupting channel | phase 1's real data on how often a promise goes stale unresolved |
| 3 | Jobs become one promise type that carries a process: `jobs.py start` optionally records a promise with `job_id` set, and the terminal push also offers to resolve it | phase 1, and a decision on whether that coupling is automatic or a caller's explicit choice |

---

## 5. Rails phase 0 honours

- **Writes go through `stateio`'s append helper**, never an in-place `open(path, "w")` —
  `check_state_writes.py --enforce` blocks CI on the latter.
- **stdlib only.** No import beyond `mouth`, `paths`, `stateio` (all sibling stdlib modules) and the
  standard library.
- **Fail-open on every path that is not the append itself.**

---

## 6. What could not be verified

- **Whether any of the sampled genuine promises were actually kept.** §2.1 explains why no second
  reconciliation instrument was built to answer it.
- **The true false-negative rate of the six named phrases.** A promise phrased without any of them
  (*"consider it done tomorrow"*, *"you'll have it by then"*) is invisible to the measurement. The 30%
  figure is a precision estimate over the phrases named, not a recall estimate over every way the
  assistant might promise something.
- **Whether founding failure #1's stand-in is typical or a one-off.** The surface a workaround takes
  is not regex-able the way an "I'll" phrase is.

---

## 7. Open questions for the owner

1. **Is the phase-1 write-time contract enforced in code, or left to the prompt?** `mouth.py` and
   `turns.py` both had to live at the code call site because prompt-only logging yields zero rows. But
   there is no single call site for "the assistant just said 'I'll X'" the way there is for a landed
   Telegram send — it would have to fire from inside the warm session's own turn. The lean: start
   prompt-side (grounding + mode instruction), measure, and move to a code-side forcing function only
   if the measurement shows the zero-rows pattern repeating.
2. **What does the Dream nag sound like, and how stale is stale?** Left open rather than guessing a
   number with no data behind it.
3. **Should `job_id`-backed promises resolve automatically off the job's terminal state, or does that
   coupling need an explicit decision at record time?** Phase 3 names the question; it does not answer
   it.
