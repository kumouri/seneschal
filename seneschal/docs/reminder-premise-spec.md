# A reminder's premise: what it is, who checks it, and what happens when nobody can

**Status:** `PARTIAL(Phase 0 BUILT — reminder_premise.py, pure; Phase 0.5 BUILT — reminder_premise_track.py
wires review_due into the live seed path via reminders_seed.py --consecutive-misses, report-only, using
§10.1's recommended thresholds and the seed's existing stderr/seed-log channel; Phase 1's store
properties, the Wrap/Brief digest line and the owner's write-back answer unbuilt, gated on §10.2/§10.3)`
· **Owner:** the assistant · **Scope:** `../scripts/reminder_premise.py`,
`../scripts/reminder_premise_track.py`, `../scripts/reminders_seed.py`,
`../references/reminders-policy.md`, `../references/databases.md`.

---

## 1. The problem

A ⏰ row encodes *what to do* and *how often* (`Cadence`), *when* (`Times` / `Time Window`), *how loud*
(`Importance`), and *whether it chases* (`Nag Until Done`) — see `../references/reminders-policy.md`. It
encodes nothing about **why it exists**. When the reason evaporates, the row keeps firing at full
strength, because nothing in the system has anywhere to notice. The only thing that closes that gap is
the owner being asked directly, and without a mechanism that ask is incidental rather than something
the system produces.

**A missed reminder and a pointless reminder are indistinguishable from the inside.** Both look like:
fired, not acked, misses +1. The escalation ladder (`../references/reminders-policy.md` → "Importance,
the Nag-Until-Done flag, and re-firing") then treats the pointless one *harder*, because escalating on an
unacked streak is exactly what the ladder is for.

## 2. The two shapes this spec is answerable against

**Shape 1 — the world changed quietly.** A high-importance daily row with a nag ladder and a
quiet-window pierce existed because the owner was covering a task for someone else. Its misses climbed
to four, five, six. The carry-over kept reading it as an open item. In fact the other person had quietly
taken the task back days earlier and simply stopped asking. The row was measuring nothing, with a stack
of queued nudges still behind it, until the owner mentioned it in passing and it was retired the same
evening. **Nothing here was watching for the world to change.**

**Shape 2 — the premise was killed deliberately, by a change in this repo.** A low-importance daily row
asked the owner to grab a screenshot of the plan-usage panel. It had never once been acked. It existed
only because the assistant could not read that panel — which stopped being true the day a
usage-telemetry instrument shipped. It kept firing for two more days, until the owner said it could be
retired. **The premise did not merely die; a change made here killed it, and nothing connected the two.**

## 3. Why this matters more than two dead rows

⏰ is the same surface that carries the owner's `super-critical` rows. Every never-acked row on it trains
the owner to skim past that surface. The cost is not the noise itself — it is the erosion of the one
channel that also carries the things that must not be missed. A fix aimed only at "reduce noise" would
miss this; the target is **trust in the channel**.

## 4. What a premise is, and is not

A **premise** is a short, human-readable statement of the condition under which a ⏰ row's cadence still
makes sense — *why does this row exist*, distinct from every other field on it:

| Field | Answers |
|---|---|
| `Cadence` | How often |
| `Times` / `Time Window` | When |
| `Importance` | How loud, and whether it pierces quiet |
| `Nag Until Done` | Whether it chases until acked |
| **`Premise` (proposed)** | **Why it exists at all** |

Most recurring habits have no premise worth writing down — "brush teeth" needs no justification and
nothing here changes that. A premise is worth recording when a row exists *because of* a fact external
to the habit itself: an injury, a missing capability, a season, someone else's situation. Both shapes in
§2 are exactly this, and **neither row had one written down** — that absence is part of what this spec
has to fix, not a precondition it can assume away.

**A premise is never evaluated by code.** Nothing here reads a `Premise` string and decides it is false.
The only judge of whether a premise still holds is the owner; the only thing a machine can do is notice
that a row has gone unacknowledged long enough to be worth asking about, and hand the owner the question
with whatever reason is on file. See §8.

## 5. Representation on a ⏰ row (proposed, not yet added)

Two additive store properties, in the same spirit as `Times` and the `Cadence`-expression change
(`reminder-cadence-mechanism-spec.md`) — a row with neither present behaves exactly as it does today, so
there is no migration and no flag day:

- **`Premise` (text, optional).** A sentence naming the condition that makes the row worth having —
  *"the owner is covering this while X is away,"* *"the assistant cannot read the usage panel."* Free
  text, exactly like `Notes`; never parsed, never evaluated, only ever displayed back inside the review
  question (§6).
- **`Premise Last Reviewed At Misses` (number, optional).** The row's `Consecutive Misses` at the moment
  a premise-review question was last asked about it. Not a date — the re-arm rule in §6 needs the
  miss-count it fired at, not merely that it fired once. Cleared whenever the row is next acked (`Done`
  or `Finished`), exactly like `Reminded Today` is cleared at the daily reset, so a fresh streak after a
  real ack can trigger a fresh, independent question.

**Neither property is added to the schema by this spec.** Like the `Cadence` → text conversion, adding
them is a one-time store edit; it is deferred until §10's questions are answered, so nothing here can be
half-wired against a schema nobody has agreed to.

## 6. Who evaluates it, and when

**Triggering does not require a `Premise` value.** Shape 1 had no premise written down anywhere, and it
is exactly the row this spec exists to catch — gating the trigger on the field's presence would miss it
a second time. The trigger is `Consecutive Misses` alone, a field every ⏰ row already carries:

```
review_due(consecutive_misses, importance, last_reviewed_at_misses) -> bool
```

`../scripts/reminder_premise.py` (`review_due`, `threshold_for`) implements this: due once `Consecutive
Misses` reaches a threshold — lower for `critical` / `super-critical` rows than for everything else
(Notion's emoji labels normalize to these keys), per §3's argument that the more dangerous surface should
be questioned sooner — and due again only once misses climb a further threshold past whatever the
"already asked at" marker last recorded.

**The question is asked, never answered, by a scheduled run.** A row that crosses the threshold is
surfaced **once** as *"is this still a thing?"*, with its history attached (`format_review_question` —
the miss count, and the `Premise` text if one is on file). It is never delivered as a standalone push
nudge — see §8.

## 7. What the owner's answer does

- **"No, retire it."** Already covered by existing policy, not a new gate: `Status = Finished` on the
  owner's explicit "I'm finished with X" is the standing act-low rule in
  `../references/reminders-policy.md` → "Act-low vs ask-high". The review question is simply a new
  *route* to that explicit statement, not a new kind of write.
- **"Yes, still needed."** Nothing changes on the row except the "already asked at" marker being stamped
  to the current `Consecutive Misses`, so the same streak does not ask again until it climbs a further
  threshold past that point. Without a re-arm rule keyed on the miss count (not a date, and not "until
  next ack"), a row that is asked once, confirmed, and then genuinely never acked again would only ever
  be asked about once.
- **No answer at all.** The row keeps firing exactly as it does today. Silence never retires anything;
  see §8.

## 8. What this is NOT (binding on this design)

- **Not auto-retirement.** A row that retired itself on N misses would have retired Shape 1's row while
  it was still genuinely needed and the owner was merely unable to do it for a while. Miss count alone
  cannot distinguish *pointless* from *blocked* — only the owner can.
- **Not more nudging.** The review question is not a new push channel; it rides an existing scheduled
  digest, asked at most once per streak, never escalated, never repeated mid-cycle.
- **Not a truth-oracle.** The assistant never judges whether a premise is dead. `review_due` decides only
  *whether to ask*; the answer is never inferred, defaulted, or guessed.

## 9. What happens when it cannot be evaluated (fail-open, named in both directions)

This is not one fork but two, and they fail in **opposite** directions on purpose
(`reminder_premise.review_due`'s docstring states both):

- **A malformed or missing `Consecutive Misses`** (the field that says a row is overdue at all) fails
  toward **not asking**. Bad data must never manufacture an overdue streak that isn't real — the same
  direction `reminders_cadence.py` takes when a cadence cannot be parsed, aimed at a different question
  (there: is it due *at all*; here: is it due for *review*).
- **A malformed or missing "already asked at" marker** fails toward **asking again**. Treating an
  unreadable "already asked" marker as licence to stay silent would recreate the exact bug this spec
  exists to fix — a row nobody is watching. The cost of guessing wrong here is one redundant question;
  the cost of guessing the other way is silence.
- **In neither direction can a failure here touch delivery.** `review_due` has no path back into
  `Status`, `Nag Until Done`, the seed's enqueue, or the fire-time gates — it returns a bare bool. A bug
  in this layer can cost at most one missed or one extra "is this still a thing?" line; it cannot
  silence or duplicate the underlying reminder.

## 10. Decisions for the owner — options, with a recommendation

Retirement is a destructive act on the owner's tracker, and every fork below either touches that path or
sets the pace at which the owner is asked to use it.

### 10.1 The miss-count thresholds

| Option | Note |
|---|---|
| A. A single threshold for every row (6) | Simple; matches Shape 2's lifetime miss count. Leaves critical rows waiting as long as low ones, which §3 argues against. |
| **B. A lower threshold for critical-and-above (4 critical / 6 default)** | Matches Shape 1 — it was already worth asking at 4. Costs a second constant. |
| C. Per-row, settable in `Notes` | Maximum flexibility, no shared vocabulary, and a new parsing surface for a number that rarely needs to differ. |

**Recommendation: B.** `reminder_premise.py` ships this shape (`DEFAULT_THRESHOLD = 6`,
`CRITICAL_THRESHOLD = 4`); either number moves with a one-line change.

### 10.2 Which run carries the question — open

| Option | Note |
|---|---|
| A. Wrap only | One daily check; a row that crosses the threshold mid-morning waits until evening. |
| B. Brief only | Front-loads it; misses the day's own new streak-crossings until tomorrow. |
| **C. Both, guarded by the re-arm rule so only one ever asks** | Whichever runs first catches it; the other sees the marker already current and asks nothing. No extra store read — both already read the ⏰ collection. |

**Recommendation: C.**

### 10.3 The channel — open

| Option | Note |
|---|---|
| **A. A line in the existing Wrap/Brief digest** | No new send path; consistent with holding non-urgent items to the digest rather than pushing them standalone. |
| B. A tappable Telegram picker, like an approval ask | Interruptive, and costlier to build (a new picker kind, a new settle path) for a question that is not time-critical. |

**Recommendation: A, with a named risk.** Both shapes in §2 were actually resolved by direct
conversation, not by a digest line that might be skimmed past — the same failure could reappear one
level up if digest questions go unanswered. If measurement later shows that, escalating to B for rows
that go unanswered N times is the natural next step, not a redesign.

### 10.4 The deliberate-premise-kill link — open, no recommendation

Shape 2 was not the world changing — it was *this repository* shipping the change that made a row
pointless, with nothing connecting the two events. Should shipping the replacement name the row it
obsoletes?

| Option | Note |
|---|---|
| A. Leave it to the miss-count review (§6) | No new mechanism; Shape 2 shows the gap is measured in days, not months. |
| B. A convention: a change that makes a ⏰ row's premise false names that row in its description, and something greps for it | Closes the gap to zero, at the cost of an authoring discipline nothing enforces today. |

**No recommendation** — this is a question about how future changes are written up, not a
reminders-mechanism question.

## 11. Phase plan

**Phase 0 — BUILT.** `../scripts/reminder_premise.py` (`threshold_for`, `review_due`,
`format_review_question`) + `test_reminder_premise.py`. Pure, stdlib, zero I/O — importing it changes
nothing.

**Phase 0.5 — BUILT, an interim wiring that decides nothing new.** The narrowest shape that puts the
module on the live reminder path without pre-empting §10. `../scripts/reminder_premise_track.py` adds
the one thing `review_due` needs that Phase 1 would get from the store — the "already asked at" marker —
as a **local** file instead (`state/reminder-premise-reviews.json`, one int per reminder id, written
atomically through `stateio`), since neither proposed property exists in the schema yet.
`reminders_seed.py` gains `--consecutive-misses` (mirroring `--importance`); when a row crosses its
threshold and hasn't been asked since, the seed prints the question as one `!` line on stderr and a
`premise_review_question` field in `state/seed-log.jsonl` — the **same** channel the seed's `no_ladder`
warning already uses (the Reminders subagent copies the seed's `!` lines into the Run Log verbatim), not
a new one, so §10.3 is untouched. The thresholds are the module's own defaults (§10.1's recommendation
B), so §10.1 is untouched too. Omit `--consecutive-misses` and the whole path is a no-op.

**What Phase 0.5 is NOT:** it never writes `Premise`, never asks inside the Wrap or the Brief, and the
owner's answer (if any) lands exactly as it always has — a human reading the Run Log and, on "no",
setting `Status = Finished`. `Premise` text is never attached, because the field doesn't exist to read
from. The Reminders subagent relays a premise-review line; it never acts on one itself.

**Phase 1 — gated on §10.2/§10.3.** The two store properties (§5), a Wrap/Brief digest line (replacing
Phase 0.5's stderr/seed-log surfacing), and the write path for "yes, still needed" (stamping the real
marker property) / "no, retire it" (the existing act-low `Status = Finished`, reused rather than
reinvented).

**Not in this spec, and not proposed:** anything that writes `Status` off silence, a re-fire ladder for
the review question itself, or a channel beyond §10.3's two options.

## Related

- `../references/reminders-policy.md` — where the Phase 1 wiring lands; already describes the Phase 0.5
  `!` line.
- `../references/databases.md` — the ⏰ Reminders schema; carries a pointer to this spec, not the
  properties themselves (§5).
- `reminder-cadence-mechanism-spec.md` — the other ⏰ property change deferred to the owner, and the
  fail-open polarity §9 mirrors.

## Router entry

**Status:** PARTIAL — the pure decision module (Phase 0) is BUILT and is called against live rows (Phase
0.5: `reminder_premise_track.py` + `reminders_seed.py --consecutive-misses`), report-only via the seed's
existing stderr/seed-log channel and a local "already asked" store — nothing new decided. Phase 1 (the
store properties, a Wrap/Brief digest question, the owner's write-back answer) and the
deliberate-premise-kill link are unbuilt, gated on §10.2/§10.3.

**What it decides:** **A ⏰ row has no concept of WHY it exists, so a reason can die with nothing
noticing** — either because the world changed quietly, or because a change in this repo made the row
pointless. Proposes an optional `Premise` text field plus a miss-count-triggered, once-per-streak "is
this still a thing?" question — never an automatic retirement, never a second nudging channel. Names the
fail-open asymmetry explicitly: a bad "is this row overdue" reading must never manufacture a question,
but a bad "did we already ask" reading must never manufacture silence.
