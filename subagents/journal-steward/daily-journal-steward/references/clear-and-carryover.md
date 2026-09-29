# Clear the Journal + Build Tomorrow's Carry-Over (Critical Path #2)

After the day is captured (the Agent Run Log row exists with the verbatim entries in its page body —
Phase 1), reset the Interstitial Journal (`00000000-0000-0000-0000-000000000010`) for the next journal
day and build the carry-over callout at the top. **Never clear a day whose capture doesn't exist yet.**

## Journal-day boundary

The owner may journal after midnight, so **treat late-night-after-midnight entries as part of the prior
day.** Group entries under a top-level **date toggle** whose title is the journal day they belong to. If
multiple date toggles exist, **process only the most recent/active one** during the run (unless asked
otherwise).

> **Preserve the date-toggle shape.** Other skills read "has the owner journaled today" as *a top-level
> date toggle for today with ≥ 1 entry* (see `databases.md` §1) — the reset below must leave the page in
> exactly that structure.

## Clearing steps

1. The processed day's entries are now preserved verbatim in the run-log capture — they can be removed
   from the Interstitial Journal.
2. Keep only **carry-over notes** relevant to the next journal day. *Relevant* is not a judgement
   call — it is defined by **The daily rebuild** below, which builds tomorrow's callout from current
   sources rather than editing yesterday's list.
3. Add a fresh **top-level date toggle** for the next journal day; new entries (including after-midnight
   ones) go under it.

## The carry-over callout

A single callout at the very top of the Interstitial Journal: `<callout icon="📌">`. Inside, group items
into collapsible `<details>` toggles by category so it isn't overwhelming.

- Each toggle's summary line: **category emoji + bolded name + item count**, e.g. `💼 **Work** (2)`.
- Standard groups (use whichever apply; **skip empty ones**):
  - `💼 Work`
  - `💜 Relationships`
  - `🏠 Home & Errands`
  - `🩺 Health & Self-Care`
  - `💵 Finances`
  - `🔧 Hobbies & Tech Projects`
  - `🎮 Gaming`
- Items that don't fit go under a catch-all or an ad-hoc group.
- Standalone one-off notes (closing notes, single reminders) can sit **outside** the toggles at the
  bottom of the callout.
- A `🎯 Goals` group is added when a goal review is due / deadline within 7 days / status changed / new
  goal today (see `goals.md`).

### Shape (matches the live journal)

```markdown
<callout icon="📌">
**Carry-over — <Weekday Month D, YYYY>:**
<details>
<summary>🏠 **Home & Errands** (1)</summary>
	- 💡 water the plants before the weekend
</details>
<details>
<summary>💼 **Work** (2)</summary>
	- ⚠️ item…
	- 💡 item…
</details>
</callout>
```

## The daily rebuild — the callout is REWRITTEN, not appended

The callout is **not a historical record, so it is not append-only: it is rewritten completely every
run.** Carry-over that is only ever added to fills with lines that are no longer true, that were
retired weeks ago, or that are already done — and a working surface the owner has to wade through is
one they stop reading.

**Do not read yesterday's callout and add to it.** Build today's from today's sources, then re-admit a
yesterday item only if it survives the tests below. The default for every line is **drop**; carrying
requires a reason the run can name. That inversion is the whole mechanism — "remove what's stale" is a
diligence rule and diligence rules decay, whereas "keep only what you can justify" cannot silently
accrete.

*Why this needs saying.* `../../../../seneschal/references/memory.md` has always carried this rule for
**the assistant's own** carry-over — *rebuild from current state each run (resolved items drop off)* —
and the same section declares the journal's carry-over **distinct** from it, so the journal never
inherited it. Meanwhile the only lifecycle rule in this file pointed the other way (*"Every Active or
Carrying Over flag MUST appear in the next day's callout"*) — a rule about flags that is easily read as
a rule about everything. Left alone, the callout grows to dozens of items across every toggle, plus
multi-week narrative paragraphs, with wrong and long-retired lines riding along. (The assistant's own
`state/carry-over.md` is a different object with its own writers — see
`../../../../seneschal/docs/carry-over-region-spec.md`; nothing in this file writes to it.)

### Audience — the carry-over is written FOR THE OWNER

The callout is **the owner's** working surface, not the assistant's. **An item that exists for the
assistant's benefit does not belong in it, however true and however recent.** Bookkeeping notes,
records of what the assistant changed, internal indexes and "I updated X" confirmations are all in that
class.

**Audience is the first gate; freshness is the second.** The drop criteria below ask *is this still
true?* — this asks *is this theirs?* A line can pass every drop criterion, accurate and same-day, and
still not belong. Run audience first: it needs no lookup, and it removes items the drop criteria would
have kept.

**The test — answerable without a judgement call:**

> **Who is worse off if this line is missing tomorrow?**

If the answer is **the owner**, it is carry-over. If the answer is **the assistant** — the run loses
its place, or the next run has to re-derive something — it is not carry-over, and it still has
somewhere to go. **Route it:**

- **The owner's** — they act on it, decide on it, wait on it, or are affected by it → **the carry-over
  callout.**
- **A record of work done** — what a run changed, and when → the Run Log `Carry-Over Context`
  (`run-log.md`), or the record it describes: the row, the page, the ledger it is a receipt for.
- **The assistant's own** — a lesson, an unfinished thought, a generalisation that hasn't earned a rule
  yet, a thread that would leave the next run *worse off* if it were lost → **the assistant's own
  space**, below.
- **Nowhere** — what genuinely nobody needs: a confirmation of something that is now simply true, a
  note whose entire content is legible in the artifact it describes. **Nowhere is where the useless
  goes — it is not the default for anything assistant-facing.**

**Routing, not deletion.** *"Not the carry-over" never means "destroy it."* This section decides
**which surface an item belongs on**, and every branch above except the last one keeps the item. Read
the header as *audience*, never as a licence to discard: an item is thrown away only when the answer to
*who is worse off tomorrow* is genuinely **nobody**.

#### The assistant's own space — the drawer that is the assistant's

Routing with two assistant-facing destinations that are **both records of work**, and then a bin,
leaves a note that is neither the owner's nor a receipt — a lesson worth carrying, a half-formed
generalisation, a thread worth not losing — with nowhere to land, and tells the run to discard it.
The fix is not a looser rule; it is a drawer.

That drawer is a **store page the owner has set aside for the assistant's own use** (a scratch pad for
working notes, half-formed ideas and threads worth keeping between runs), if they have given it one;
without one, a witness-only dated note (`../../../../seneschal/scripts/notes.py`) is the local
equivalent. Nothing that lands there needs to be useful to the owner, and nothing there is written
*at* them.

**It is not a third log, and no run is required to feed it.** There is no nightly "append to the
drawer" step, no expected shape, no minimum. A destination that *must* be filled every run is a
bookkeeping chore, and would recreate precisely the burden this section removes — **the drawer exists
when there is something to put in it**. Write to it when a run has something it would not want to
lose; write nothing on the runs that don't.

#### The boundary — this is about the reader, not about mentioning the assistant's machinery

The rule bites on **who the item is for**, not on what it is about. The assistant's machinery appears
in the callout constantly and belongs there.

| Whose it is | Example | Where it goes |
|---|---|---|
| **The owner's** — they act, decide, or are affected | A reminder the assistant has **paused** and will not fire · a background job the owner is **waiting on** the answer to · a PR sitting green **needing their tap** · a draft held at the approval gate | **Keep.** Every one of these is about the assistant's machinery, and every one leaves *the owner* worse off if it silently vanishes. |
| **The assistant's** — bookkeeping, an internal index, a receipt for finished work | "Created 3 rows and filled in their Notes" · "Rebuilt the carry-over" · "Appended today's capture link to 4 pages" · "Updated the Run Log" | **Not carry-over** — which is not the same as *gone*. Run Log or the row it describes if it is a record of work; the assistant's own space if the next run would be worse off without it; nowhere only if nobody needs it. |

So the question is never *does this mention the assistant* — it is *does the owner have a move to
make, a wait to be aware of, or a fact they would act on differently without it*. **"I updated X" is
the shape to watch:** it is a receipt for work already finished, addressed to the worker.

### Drop criteria

**The second gate.** These apply to what survived *Audience*, above; they ask whether an item is still
true, not whether it is the owner's.

Drop the item when the run can point at the fact that retires it. **No fact, no drop** — that case is
*The unverifiable item*, below.

| Drop it when | The fact you must be able to point at |
|---|---|
| **Resolved** | The thing it asked for happened — task `done`, message sent, appointment attended, decision made, thing bought. |
| **Superseded** | A later fact replaces it. If the supersession is load-bearing, it becomes a corrected line rather than a deletion — see *Corrections*. |
| **Contradicted** | A later fact makes the line false as written. Also usually a correction, not a silent drop. |
| **Retired by instruction** | The owner said stop, drop it, or never raise it again. **A retired item is never re-admitted** — a later run that stumbles on an old mention of it in the journal must not resurrect it. |
| **Overtaken** | The person, project, employer, or context the item hangs off is gone. |
| **Already rendered by its own surface** | The item is fully carried by a store record whose view already shows it — a flag, task, goal or deadline. The callout points at those; it does not re-type them. |
| **Not an item** | A carry-over line is *one open loop*: something to do, watch, or decide. Multi-week narrative is a digest and belongs in the day's capture (the Run Log page body, `run-log.md`), not in the callout. |

**Tripwire, not a quota:** if the rebuilt callout runs much past ~15 items, the rebuild probably didn't
happen. Re-read it against this table before writing it.

### The unverifiable item — bounded holding, then a picker

The hard class is the item the run can neither confirm live nor confirm dead from its own sources. This
is what builds the pile: not obviously dead, so it was kept; kept, so it was kept again tomorrow.

- Put it in a **`❓ Needs a ruling`** group and **stamp the date it entered holding** — `held since
  Aug 28`. A holding group without stamps is append-only with an emoji on it.
- **Bound: 3 journal days** — the same 3 the flag nudge already uses, so this file carries one number
  and not two. On day 3 the item may not sit another night: it is either resolved by evidence, or it is
  **asked**.
- **Ask with a picker, one question per run.** `../../../../seneschal/scripts/telegram_ask.py ask`,
  highest cost-if-wrong first — a decision goes out tappable, one question per message. The options are
  normally *Still open · Done, drop it · Retire it, don't raise this again*; a "retire" answer makes the
  item **Retired by instruction** above, permanently. A backlog drains one ask per night — that is the
  intended rate, not a queue to flush.
- **Hard cap: 5 items.** If holding would exceed five, that is evidence the rebuild is failing, not a
  reason to grow the group. Keep the five with the highest cost-if-wrong, **name the rest in the Run Log
  `Carry-Over Context`** (`run-log.md`) and drop them from the callout. They stay durable and the next
  run reads them; they just stop occupying the owner's attention.

### Never dropped silently — the narrow carve-out

Three classes where a wrong drop and a wrong keep do **not** cost the same. For these, the default when
unsure flips from *drop* to *ask*:

1. **Standing safety items** — anything where the owner not seeing the line is itself the harm.
2. **Anything the owner explicitly asked to be recorded** — "keep this for tomorrow", "don't forget
   this", `⭐` / `🚩` / `📌`. If they put it there, only they take it out. (Same triggers as
   `important-flags.md`; most of these should *be* flags, and a flag is governed by its row.)
3. **Appointment-bound material** — something to raise at an appointment or meeting that hasn't
   happened yet. It retires when the appointment happens or the topic is raised, and not before.

**The carve-out changes the default when unsure. It does not make an item immortal, and it is not a
fourth bucket for "important stuff."** A carve-out item still drops the moment it is *resolved*, and a
carve-out item that goes unverifiable goes into bounded holding and out to a picker like anything else
— it is only never dropped **on a guess**. Read it any wider than these three classes and the carve-out
is the append-only rule again, wearing a safety label.

**And a carve-out item is not exempt from being wrong.** A class-1 line can be false — telling any
reader that something is paused and must not be chased, days after it was in fact resolved. Protected
from silent deletion is not protected from correction.

### Corrections — when the fix is itself the content

Most superseded lines simply go. But when the supersession is **load-bearing**, the replacement carries
the correction, so a future reader cannot re-derive the wrong version from an older copy.

**The test: would a session that read only the old line take an action the owner would have to undo?**
For a stale "paused, don't chase it" line, yes — it would tell them to stop chasing something already
won. For a stale errand, no; just drop it.

When the answer is yes:

- Write the current fact **and** mark what it replaces, on one line — e.g.
  `⚠️ <current fact> — supersedes the earlier "<short quote of the wrong line>" note`.
- It rides **one** rebuild and then drops like anything else. The correction is durable in that day's
  capture and in the Run Log `Carry-Over Context`, so the record isn't left holding only the wrong
  version.
- Deleting a load-bearing wrong line and saying nothing is **not** sufficient: the wrong version
  survives in archived captures, and the next reader is free to reconstruct it and believe it.

**This file carries the rule and the mechanism, never the material.** The callout's content is the
owner's and stays in the store.

## Important Flags in the carry-over (REQUIRED)

Every **Active** or **Carrying Over** flag MUST appear in the next day's callout until it's `Resolved` or
`Archived`. (Full lifecycle in `important-flags.md`.) Visual treatment:

- Group flag lines by **Type**; lead each line with the **type emoji + importance level**, then the
  description, then a `[flag](url)` link.
- Order within the callout: **Critical → High → Notable → Reference.**
- If several flags share a Type, sub-group them under a Type header.
- If a flag has been carrying **3+ days** unresolved, add a gentle nudge, e.g. `— carrying since Apr 27`.

Examples:
```markdown
- 🚨 💼 CRITICAL Work — short flag description — [flag](flag-url)
- ⭐ 👨‍👩‍👧 High Family — short flag description — [flag](flag-url)
- ✨ 🧠 Notable Internal/Feelings — short flag description — [flag](flag-url)
- 📌 🏠 Reference Home/Life — short flag description — [flag](flag-url)
```

These flag lines can live in a `⭐ Flags` group or be folded into the matching category toggle — but they
must be present and ordered by importance.

### This obligation belongs to flags and to nothing else

A flag persists because a **record** in the Important Flags domain says `Active` or `Carrying Over` — a
lifecycle the owner can see and change, and one `important-flags.md` already terminates (`Resolved` /
`Archived` → remove from the callout). So the MUST above is a **rendering obligation over a table**:
the callout shows today's query result, and the store decides what is in it. It is not a licence to
retain.

An ordinary carry-over item has no record and no `Status`, so **nothing authoritative says it is still
open** — it appears tomorrow only if tomorrow's rebuild re-derives it from current sources (*The daily
rebuild*, above). Reading this flag MUST as a general retention rule is precisely how everything else
accretes; if an item feels important enough to deserve that guarantee, the answer is to **make it a
flag**, not to pin it to the callout.
