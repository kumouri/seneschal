# Session continuity — using the prior session id on both sides of the resume gate

**Status:** `PARTIAL(phase 0)` — phase 0 BUILT: `thread_tail` sends `THREAD_CAP` turns rather than 6,
the refusal *class* is logged to `state/session-starts.jsonl` on every spawn, and `CONTEXT_WINDOW_TOKENS`
is `1_000_000` rather than `200_000` (§3.2.1). The session-id resume gate itself is built in
`scripts/presence.py`. **Phases 1-4 designed, deliberately unbuilt**; phase 1 stays gated on §6's two
weeks of refusal-class data even though §8.1 records that the owner **likes** the 2 h → 8 h raise in
principle and chose to wait for the data.

> **A lesson about this very header, kept because it generalises.** An earlier version of this line
> read "SPEC ONLY — nothing built" while its own §6 table marked phase 0 as built. The header was
> authored from the phases still to come and never re-read against the row beneath it. A status line
> is corrected against the **code** (`scripts/presence.py`), not against the router that summarises it.

**Parent:** the warm-session lifetime research (its Step 2a shipped the resume gate this document is
about; Step 1 built the context measurement; Step 3 is the wind-down question).
**Siblings it composes with:** the chat-turn indexing work's phase 0 (`state/turns.jsonl` carries
`session_id`, which is what makes scope B affordable), `spend-levers-spec.md` phase 0 (`turn_id` joins
the ledger to the metrics row), `mouth-spec.md` phase 0.

**The ask.** The owner wanted something that actually *uses* the prior session id from the old warm
session. Four options were put to them; they chose **1 and 2** — a better handoff when resume is
refused, **and** widening the gate so it is refused less — and decided that the other two (the id as a
join key across the logs, and a durable conversation thread across respawns) are already covered by
what the sibling specs shipped.

---

## 1. The thesis

Step 2a built a gate that decides whether the next warm session resumes the last one. It is a good
gate and its refusals are, individually, correct. But **the gate is the only thing that has ever read
`last_session`**, so a refusal discards the id entirely — and a refusal was, when this was written, the
*majority* outcome. When it refuses, the assistant re-grounds from six lines of thread tail and the
conversation restarts.

Two halves, and they are not alternatives:

- **Scope A — refuse less, where refusing is not actually buying anything.** Two of the four refusal
  classes look soft under measurement.
- **Scope B — when it still refuses, land softly.** The id is a handle on everything the ended session
  said. Six lines is not the best available answer; it is just the oldest one.

**Scope B is the load-bearing half.** A gate can always be widened later and narrowed again; a refusal
path that throws away context is a permanent tax on every refusal that remains, including the ones
that should never be widened (`turn_error`, `undelivered_reply`).

---

## 2. What was true when this was designed — measured, not remembered

Read off a live daemon's `state/presence.log` and `state/presence-state.json` before phase 0 landed.
The numbers are illustrative of the mechanism; §8.1 records how they moved afterwards.

### 2.1 The gate refused more often than it accepted

| | count |
|---|---:|
| `• resuming warm session …` | **27** |
| `• cold warm-session start …` | **35** |

**56% of warm-session spawns were cold.** Every one of those had a `last_session` record on disk.

### 2.2 Why the 35 refused

Grouped from the log's own reason strings:

| Refusal | count | Soft? |
|---|---:|---|
| `last session is Nm old (limit 120m)` | **15** | **Yes — scope A.** The session was clean and intact; only the clock disqualified it. |
| `last context ~N (…%) at/over the 80% wind-down mark` | **7+** | **Partly — scope A.** Observed at 98.7%, 101.0%, 102.8%, 113.2% and **171.3%** of the window. |
| `last session ended with undelivered_reply (not resumable)` | 6 | **No.** Deliberate, and §3.3 argues it stays. |
| `last session ended with turn_error (not resumable)` | 2 | **No.** Deliberate. |

**The largest class is the clock.** Fifteen refusals of a session that had done nothing wrong.

### 2.3 The cold path already had 20 turns and sent 6

```python
THREAD_CAP = 20                      # presence.py — "rolling turns kept for cross-session continuity"
lines = [... for t in thread[-6:]]   # presence.py — thread_tail(), before phase 0
```

`state/telegram-thread.json` retains **20** turns. `thread_tail` — the only reader, and the sole
continuity input to a cold `GROUNDING` — took the last **six**. **Fourteen turns were stored on the
explicit rationale of "cross-session continuity" and then dropped at the one moment continuity is
needed.** No decision chose 6; the two constants simply disagreed, and nothing had ever compared them.

This is the cheapest finding in the document and it is worth stating plainly: **the single highest
value/effort change available here is a one-line constant.**

### 2.4 The sizes involved

| | bytes |
|---|---:|
| `GROUNDING` (cold spawn pays this) | 9,935 |
| `RESUME_PREAMBLE` (a resume pays this) | 422 |
| A thread-tail line, measured median | ~180 |

So the cold handoff spent ~9.9 kB re-explaining who the assistant is and ~1.1 kB on what was actually
being discussed. **The ratio is upside down**, and the context-budget check (`context-budget.json`,
`scripts/check_context_budget.py`) measures the first number while nothing measures the second.

### 2.5 What the record already contains

```json
"last_session": {"id": "00000000-…", "model": "claude-opus-5", "context_tokens": 106318,
                 "turns_served": 3, "ended_at": 1785974686.35, "reason": "idle_winddown"}
```

The live record at time of writing was itself a good exhibit: a **clean** death, **recent**, and
106,318 tokens — which the context ceiling, at the old window, refused. Three turns served. The gate was
about to throw away a three-turn conversation because those three turns were expensive.

---

## 3. Scope A — refuse less, where refusing buys nothing

### 3.1 The clock (15 refusals) — the limit is defensible, its *shape* is not

The 2-hour limit (`RESUME_MAX_AGE_SEC`) has a real argument behind it, recorded beside the constant in
`scripts/presence.py`: past that, only the clock is re-injected per turn, so everything else the session
believes — carry-over, reminder state, what the store said — has gone stale. That is correct and it is
why this spec does **not** propose simply raising the number.

**The proposal is to make staleness a thing the session is TOLD about rather than a thing it is
protected from.** A resume already re-injects the clock. Extend `RESUME_PREAMBLE` to also name the gap
and what it invalidates:

> *(Resuming after 4h 12m. Re-check anything time-sensitive before relying on it: carry-over,
> reminder state, and anything the store told you are all from before the gap.)*

With that sentence, the age limit protects against a genuinely different thing — a conversation so old
that continuing it is *socially* wrong, not factually wrong — and can be set on that basis. **Proposed:
`RESUME_MAX_AGE_SEC` 2 h → 8 h**, which would have converted most of the 15.

**The honest counter, recorded rather than hidden:** this trades a correctness rail for a prompt-side
instruction, and this repo's standing precedent (`state/metrics.jsonl`) is that prompt-side contracts
are worth less than they look. The mitigation is that the failure is *visible and cheap* — the
assistant says something stale, the owner corrects it — whereas the failure the current limit causes is
invisible: a dropped conversation reads as normal.

### 3.2 The context ceiling (7+ refusals) — the gate is right and the *measurement* is wrong

Refusing at 98.7% is correct. Refusing at **171.3%** should be impossible, and the fact that it is
recorded at all says the number is not what it claims to be.

`context_tokens` is the Step-1 **estimate**, derived by dividing a turn's summed usage by `num_turns`
(the warm-session lifetime research notes the raw sum overstates by up to 41×). A figure that reads 171%
of a 200k window is not a context measurement; it is an artefact.

**Proposal: do not widen this rail. Fix its input first, then decide.** Concretely — record the *last
turn's* `context_tokens` rather than a session aggregate, and have `resume_decision` refuse to act on
a value above 100% of the window (treating it as unmeasured, i.e. the existing *"no context
measurement"* refusal) rather than treating it as a very large true value. Then re-measure for a
fortnight and set the ceiling on real numbers.

**This deliberately does not make resume happen more often in the short term.** It makes the ceiling
mean something, which is the precondition for moving it. Same discipline as the governor's metering
fix: the number was wrong before the budget was wrong.

#### 3.2.1 Correction — the numerator was only half of it

The analysis above blames the **numerator** (`context_tokens` is a lossy estimate) and that part
stands. But it takes *"a 200k window"* as given, and it was not: the CLI exposes no context flag and
`settings.json` no context key, so the window is whatever the model gives, and `claude-opus-5` — the
warm dial at the time — carries **1M as both default and maximum**, behind no beta header.
`CONTEXT_WINDOW_TOKENS` had been `200_000` since Step 1, where it is honestly labelled a stated
assumption made when nothing measured context. Step 1 then built the measurement, and it settles it —
867 turns / 99 warm sessions:

| | median | p90 | max |
|---|---|---|---|
| per-turn context | 128,141 | 184,284 | 350,525 |
| per-session peak | 95,602 | 197,600 | 350,525 |

**57 turns run past 200k and 0 past 400k.** A 200k envelope with silent compaction would leave a wall
just under 200k; there is a smooth tail instead.

So the refusal at **171.3%** was not purely an artefact — it was an ordinary ~34%-full session
measured with a ruler five times too short. Both errors were real and they compounded: the estimate
overstates, *and* it was divided by a fifth of the window.

**What this changes for the phases.** The constant moved to `1_000_000` in the same change, which is
why phase 0 ships it (see §6). At the real window, **31 of 99 sessions peaked at or above the old 160k
line and 0 of 99 reach the new 800k one** — so the ceiling now refuses approximately nothing, and
§2.2's `context_too_full` class should go quiet. That is the *point*, not a loss: the ceiling was
suppressing about a third of all resumes, including the Path A reloads (`scripts/PATH_A_CUTOVER.md`)
that §4.1 treats as the highest-value case. **Phase 2 is not cancelled, it is de-urgentised** — the
numerator is still a proxy, and a ceiling that binds on a bad number is still wrong; it just no longer
binds on anything, so fixing it can wait for honest volume rather than racing it.

### 3.3 What stays refused, and why widening these would be a mistake

`turn_error` (2) and `undelivered_reply` (6) stay. Both mean the previous session ended in a state
where **what the assistant believes it said and what the owner actually received have diverged** — the
poison-pill guard exists for exactly this. Resuming into that divergence would carry the false belief
forward and make it harder to detect, not easier. They are 8 of 35; they are also the 8 where a clean
restart is the *point*.

---

## 4. Scope B — when it refuses, land softly

### 4.1 Phase 0: raise the tail, change nothing else

`thread_tail`'s `[-6:]` becomes `[-THREAD_CAP:]`. One line. The data is already retained, already
persisted, already the right shape, and already justified in a comment as being for this purpose.

Expected cost: ~14 × ~180 B ≈ **2.5 kB** of additional cold-start context, against a `GROUNDING` that
already spends 9.9 kB introducing the assistant to itself. **This is the whole of phase 0**, and it is
worth shipping alone.

### 4.2 Phase 1: a real handoff, built from the id

The ended session's id is a handle on three artifacts that did not exist when Step 2a was written:

| Artifact | What it can contribute |
|---|---|
| `state/turns.jsonl` | the verbatim conversation, both sides, filtered by `session_id` |
| `state/session-distillations.jsonl` | the mini-dream's per-session distillate (`scripts/mini_dream.py`) |
| `state/assertions.jsonl` | what the assistant actually told the owner, in the wording received |

**Proposal:** on a refused resume, `GROUNDING`'s `{thread}` slot is filled by a **handoff block** built
from `turns.jsonl` for that `session_id` — the last N turns *of that specific conversation*, not the
last N of the rolling cross-channel thread — plus, when one exists, the session's distillate as a
one-paragraph header.

**Why this beats the thread tail even after phase 0.** `telegram-thread.json` is a *rolling, single-
channel, capped* buffer; `turns.jsonl` is the actual conversation, addressable by session, uncapped,
and carries `origin` so the job notices and reaction markers that make up ~19% of the assistant's turns
can be excluded from a handoff where they are pure noise.

**Bounded, and the bound is the point.** A handoff that grows without limit re-creates the problem the
resume ceiling exists for. Proposed: a byte budget for the block, declared in
`seneschal/context-budget.json` alongside `GROUNDING` and `RESUME_PREAMBLE`, so it is measured by the
context-budget check rather than by intention.

### 4.3 Phase 2 (speculative, and flagged as such): a distilled handoff

If phase 1's block is routinely truncated by its budget, the next move is a model-written summary of
the ended session rather than a slice of it. **Not proposed now**, because: it costs a model call on a
latency-sensitive path, `mini_dream` already writes a distillate at SessionEnd that phase 1 can read
for free, and building the expensive version before measuring the free one is the wrong order.

---

## 5. What this will not do

- **Not a durable conversation identity across respawns.** Decided out of scope by the owner (option 4).
- **Not a join layer over the logs.** Decided out of scope (option 3); `turn_id` and `session_id` already
  make it possible ad hoc.
- **Not compaction.** The CLI exposes no compaction control (the warm-session lifetime research
  establishes this); nothing here pretends otherwise.
- **Not a change to when a session ENDS.** Wind-down policy is Step 3's question and waits on the data
  `spend-levers-spec.md` phase 0 collects.
- **Not resuming a `turn_error` or `undelivered_reply` session.** §3.3.

---

## 6. Phases

| Phase | What lands | Behaviour change | Gate |
|---|---|---|---|
| **0** ✅ **BUILT** | `thread_tail` sends `THREAD_CAP` turns, not 6; the refusal *class* is logged on every spawn to `state/session-starts.jsonl`, so §2.2's table becomes a query instead of a grep; **and `CONTEXT_WINDOW_TOKENS` 200_000 → 1_000_000** (§3.2.1 — found while building the instrument, and it is what the instrument would have spent two weeks mis-measuring) | Cold starts carry ~2.5 kB more of what was actually said; the context ceiling stops refusing ~a third of resumes | Full suite green; the constant disagreement documented |
| **1** | The `RESUME_PREAMBLE` staleness sentence, then `RESUME_MAX_AGE_SEC` 2 h → 8 h | Fewer refusals; resumed sessions are told the gap | Two weeks of phase 0's refusal-class data |
| **2** | Fix `context_tokens` to a last-turn measurement; treat >100% as unmeasured | The ceiling starts meaning something. Possibly *more* refusals at first | A fortnight of honest numbers before the ceiling is moved. **De-urgentised by §3.2.1** — with the window corrected the ceiling refuses ~nothing, so this is now about making a dormant rail honest rather than unblocking resume |
| **3** | The `turns.jsonl`-built handoff block, budgeted in `context-budget.json` | A refused resume carries the real conversation | Phase 0 shipped; a measured budget |
| **4** | *(speculative)* distilled handoff | — | Only if phase 3's block is routinely truncated |

Phase 0 changes no policy and is worth landing by itself. Phases 1 and 2 are deliberately **not**
bundled: 1 widens the gate, 2 may narrow it, and merging them would make the resulting change in
resume rate uninterpretable.

---

## 7. Open, and the owner's to decide

1. **The 8-hour figure (§3.1) is a guess.** It converts most of the observed 15, and there is no
   measurement behind the specific number. A different bound — or keeping 2 h and accepting that
   class as a permanent cost — is a one-constant change.
2. **Does the staleness sentence belong in `RESUME_PREAMBLE` at all?** It is prompt-side, and this
   repo's precedent is unkind to prompt-side contracts. The alternative is refusing to resume across a
   gap at all, i.e. today.
3. **Phase 2 may make things worse before better** — treating >100% as unmeasured converts some
   current resumes into refusals. Worth it for a rail that means something, but it is a real
   short-term regression in the number the owner would see.
4. **Whether phase 3's handoff should exclude `origin: "system"` turns** (job notices, reaction
   markers) or include them for explaining the assistant's own replies. The chat-turn indexing work
   decided they are *indexed*; whether they belong in a *handoff* is a different question with a
   different answer available.

---

## 8. Decisions recorded, and what the data said

### 8.1 The owner likes the 2 h → 8 h change — and the gate still holds

The owner's decision on §7.1: wait, but record that they like the 2 h → 8 h change.

So §7.1 is answered in direction but not in timing. Phase 1's `RESUME_MAX_AGE_SEC` raise has approval
**in principle**; the two-week gate in §6 is **not** waived, and the phase does not land early on the
strength of a partial window.

**Where the data stood when that was decided** (read from `state/session-starts.jsonl`):

| | |
|---|---:|
| spawn decisions recorded | **125** |
| resumed | **97 (78%)** |
| refused `too_old` | **21** |
| refused `not_resumable` (undelivered reply) | **7** |
| refused `context_too_full` | **0** |
| sessions ended by `idle_winddown` | **118 of 125 (94%)** |
| window covered | **9.0 days** — the gate wants 14 |

Three things this already establishes, and one it does not:

- **The cold-start rate fell from the 56% this spec was written against to 22%.** §3.2.1's window
  correction did that, alone, before phase 1 was written.
- **`context_too_full` is now a dead class** — zero refusals against the 31-of-99 that motivated
  §3.2. Phase 2 is about making a dormant rail honest, exactly as its gate says, and nothing more.
- **Idle wind-down is 94% of session deaths.** Not Path-A reloads, not the ceiling. Anything aimed at
  making sessions longer should aim there first; this spec's scope is what happens *after* a death,
  which is a different lever from how often one occurs.
- **What it does not establish is phase 1.** Nine days is not fourteen, and reading a partial window
  as the answer is the specific error the gate exists to prevent. Re-read once the window reaches
  fourteen days.

### 8.2 The observed cost of the 1 M window: intra-session conflation

Recorded because it is the other half of the trade-off §3.2.1 made, and it came from the only
instrument that can see it — the owner, in daily use. It is qualitative, it is theirs, and it is not
in any log.

The owner's report, paraphrased as the requirement it implies: continuity got noticeably better in the
small things; there was **also** a slight increase in the assistant mixing up similar things from early
and late in the same day; the assistant recovers once it is pointed out; and **the increased continuity
is worth more than the mixing costs.**

**The verdict is explicit: the continuity is worth the conflation.** The bigger window is not on
probation.

But the failure mode is real and it is *specific* — **not** general drift. It is **similar items from
different points in the same session being merged into one**, and it presents as confident recall
rather than as uncertainty, which is what makes it cost the owner a correction rather than prompting a
question. Three instances in a single session, all the same shape:

1. Two different documents of the same kind, describing superficially similar events, conflated into
   one — **twice**, the second time *after* the first had been corrected.
2. A piece of equipment described from an earlier turn's reading rather than the owner's later
   correction.
3. A person's age read off a stored summary when the same note carried the birth date needed to
   derive the right (different) answer.

Note what is common to all three: **the correct information was present and was not consulted.** This
is a retrieval failure, not a storage one — and the reason a longer window does not fix it by itself.
More context in the window converts some *look-ups* into *presence*, which helps; it also puts more
similar items in reach of each other, which is the cost being described.

**Consequence for this spec:** a handoff block (phase 3) that carries *more* prior conversation
inherits this failure mode rather than escaping it, and a phase-3 design should be read with that in
mind. **Consequence beyond it:** the mitigations live elsewhere — disambiguation work against
confidently-wrong recall, and the pairing register in `session-coupling-spec.md` — and this entry
exists so that the 1 M window's cost is written down beside its benefit, rather than being
rediscovered as a novel complaint later.

### 8.3 The diagnosis in §8.2 had already been made, twice, and was re-derived anyway

Added shortly after §8.2 was written, at the owner's prompting — they pointed out that the transcripts
would show the problem had been correctly diagnosed several times already — and verified against
`state/turns.jsonl` rather than from memory, which is the only honest way to check a claim of this
shape.

The owner was right. Days earlier the assistant had said, in substance: *it is not a storage problem,
it is a **recall** problem* — an enormous amount is written down and none of it is in hand until it is
deliberately opened; a warehouse-sized filing cabinet with no pockets. Minutes later the analogy was
recorded, with the note that its third clause — *decides whether to consult* — is the load-bearing
one. It was stated again the following day. §8.2 then derived the same conclusion from scratch and
presented it as a finding.

**This sharpens the diagnosis, and the sharpening is the reason this subsection exists.**

The conclusion is not merely on disk. It is a **one-line entry in the memory index that loads every
session**: *the symptom is RETRIEVAL, not storage.* So the failure in §8.2's three instances cannot be
only *did not go and look* — for this one, the line was already in context. It is at least partly
**a recognition failure**: the summary was read, and was not recognised as the answer to the question
being asked. That is the same move as reading an age off a note whose next clause is the birth date.

The distinction matters for anything built to fix it. A retrieval failure is fixed by fetching more,
which is what a bigger window and a phase-3 handoff both do. **A recognition failure is not** — it is
fixed by making the relevant line *announce itself* at the moment of need, which is a different
mechanism and is not delivered by carrying more text.

**One method note, worth more than it looks.** The prior statement was nearly missed on search: the
query used this document's phrasing (*"retrieval, not storage"*) and the real hit was worded *"not a
storage problem, it's a recall problem."* It surfaced only through the **owner's** word — `warehouse`.
When searching the record for whether something has been said before, search the owner's vocabulary,
not the current turn's. The record is written in the words that were used at the time, and the whole
reason for searching is that those words are not the ones now in hand. This is the same defect
`scripts/rag_common.py`'s FTS5 arm was added for (the lexical arm beside the semantic one), arriving
from the other direction.

## Router entry

**Router status:** PARTIAL — PHASE 0 BUILT. **What it decided:** Why the resume gate refused 56% of the
time, and the `CONTEXT_WINDOW_TOKENS` correction (§3.2.1).
