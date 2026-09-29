# The ⏰ cadence mechanism — an expression, not an enumeration — design

**Status:** `PARTIAL(phase 1 BUILT — the grammar, the due-test and the wiring; phase 2, the Notion
select → text conversion, is the owner's host-side action and is unbuilt by design)` — phase 1 is
complete and shippable alone: it changes nothing in the store and every existing row keeps firing. ·
**Owner:** the assistant · **Scope:** `../scripts/reminders_cadence.py`, `../scripts/reminders_acks.py`,
`../references/reminders-policy.md`, `../references/databases.md`, `../../subagents/reminders/SKILL.md`.
Live behaviour is [reminders-policy.md](../references/reminders-policy.md) → *"Cadence — when a row is
'due today'"*. Companions: [reminder-exact-time-scheduling-spec.md](reminder-exact-time-scheduling-spec.md)
(the `Times`/seed layer this sits above), [databases.md](../references/databases.md) (the ⏰ schema).

---

## 1. Why this exists

The ⏰ `Cadence` field was a closed list, and its **interval family** (`Every 2 days`, `Every 3 days`,
…) was the only part of it that ever grew. Each growth cost a schema edit (a new select option) plus two
documentation edits — **to express a single integer**. A vocabulary that needs a code review to learn
the number 4 is the problem, and it is a problem the second time it happens, not the tenth.

The request that commissioned this had two halves, and they are **different shapes**:

- *"Support any number of days"* — an **interval**: an offset from the last time it was done.
- *"Twice per week, alternating three days then four"* — a **rate**: a count per period, spread evenly.

A design that only does offsets cannot express the second. §4 is about that.

## 2. The shape of the problem

The classic `Cadence` options are not one kind of thing:

| Option | What question it answers |
|---|---|
| `Daily`, `Weekdays` | a **calendar** predicate — which days of the week |
| `Every 2/3/4/5 days` | an **offset from the last ack** |
| `Weekly` | *both*, depending on whether `Notes` names a weekday |
| `Multiple/day` | **intraday** — `Times` and `reminders_roll.py` own it |
| `One-off` | a **target date** with a lookahead |

**A bare number cannot express four of those five rows.** So the answer is not "make `Cadence` a
float". It is a small grammar whose terms are **shapes**, two of which carry a magnitude.

### 2.1 Where the due-test lived, which is the part that surprised

Before this change **no Python computed cadence due-ness**. `reminders_seed.py` handled the
*time-of-day* math for a row already judged due; the judgment itself — `today ≥ Last Acknowledged +
N` — was **prose in `reminders-policy.md` that the seed run's model executed**. The only Python that
inspected a `Cadence` value at all was a one-line substring test for `multiple` in
`reminders_acks.id_cache_titles`.

That is the reason to build this as a module rather than more prose: `reminders_seed.py`'s own
docstring already argues that time math belongs in code, not in the model, and the same argument covers
the date math one layer up.

## 3. The grammar

Case- and spacing-insensitive; every classic select option is a valid term.

```
cadence  := daily | weekdays | weekly | multiple | one-off | interval | rate
interval := "every" N "days"                    N a positive whole number
rate     := N ("per" | "/" | "x") period        N a positive whole number
period   := "week" | "fortnight" | "month" | "year" | D ["days"]
```

`daily` also accepts `every day` / `every 1 day`; `weekdays` accepts `mon-fri`; `one-off` accepts
`once`. `every 7 days`, `weekly` and `1 per 7 days` are **three different cadences and are
deliberately not collapsed** — see §4.

**Refusals**, each naming the spelling that works (§5):

* a **fractional** interval — `every 3.5 days` → *"write it as a rate: `2 per 7 days`"*
* a rate **denser than daily** — `3 per day`, `8 per 7 days` → *"use `multiple/day` and set `Times`"*
* `every 0 days`, a fractional event count, an unrecognised period
* anything else → the shape list

## 4. Decision — an interval is ack-relative; a rate is grid-anchored

The fork is decided **both ways, by shape**, because the two shapes mean different things.

**`every N days` is ACK-RELATIVE.** Due when `today ≥ Last Acknowledged + N`. The offset *is* the
semantic — *"four days after you last did it"* — so a late ack must push the next one back. This is the
behaviour interval rows always had; changing it would silently re-schedule existing rows.

**`N per D days` is GRID-ANCHORED.** The event days are a fixed lattice
(`RATE_EPOCH + floor(j·D/N)`, j = 0, 1, 2, …), and the next due date is the first lattice point
**strictly after** `Last Acknowledged`. The **count** is the semantic, and only a fixed grid keeps the
count true: under ack-relative gaps, acking three days late turns *"twice a week"* into *"twice per
nine days"* — the rate becomes whatever the slippage made it, which destroys the one property a rate
exists to pin down. A late ack rejoins the lattice instead of dragging it.

### 4.1 Why this also disposes of the state question

An ack-relative rate would have required the row to **remember its cycle position** — new durable state
that has to survive a Dream reconcile, a migration, and a hand-edit in the store.

**The grid needs none.** The anchor plus `Last Acknowledged` determine where in the cycle a row sits,
so `next_due` is a pure function of *(cadence, last ack)*. **A rate row adds no ⏰ property, and there is
nothing for Dream to reconcile.** That is a large part of the decision's justification: the
ack-relative branch would have bought drift-tolerance, which a rate does not want, at the price of the
only new state in the design.

The predicted cost — *"twice a week collapses toward fixed days"* — is real and, on inspection, a
**feature**. With the anchor on Monday 2024-01-01, `2 per week` lands on **Mon/Thu** and `3 per week` on
**Mon/Wed/Fri** — the answer a human would have written down. `RATE_EPOCH` is therefore frozen: moving
it re-phases every live rate row.

The spread is **Bresenham / Euclidean rhythm**, named rather than invented — `gap_j = floor((j+1)·D/N)
− floor(j·D/N)`: `2 per week` → 3,4,3,4; `3 per week` → 2,2,3; `3 per 10 days` → 3,3,4.

### 4.2 Why not cron

**Cron expresses an absolute wall-clock schedule and cannot say "four days after you last did it."**
`0 8 */4 * *` is not `Every 4 days`: it fires on days 1, 5, 9, …, 29 of each month and then
**short-cycles at the month boundary** (the 29th → the 1st is two or three days, not four), and it takes
no notice of `Last Acknowledged` at all. Asked what happens to a row acked three days late, cron's
answer is *"nothing — it fires on the same calendar days regardless"*, which is precisely the behaviour
the interval family exists to avoid.

Cron **could** express `Weekdays` and `Weekly`-on-a-named-day. It cannot express the interval family,
cannot express a rate's even spread (a three-day day-of-month step gives ten three-day gaps and then a
one-day one, every month), and it brings a five-field syntax and a timezone/DST surface to a field whose
whole vocabulary is seven shapes. **Rejected on the semantics, not on taste.**

## 5. Decision — fractional days are refused, loudly, and told the right spelling

The due test is a **date** comparison, and the seed pass runs **once per owner-local day**. There is no
wake that could deliver a half-day offset, and `Multiple/day` + `Times` already owns intraday firing. So
`every 3.5 days` **must not silently become 3** — a value quietly floored to something the owner did not
write is the worst kind of scheduler bug, because nothing about it looks wrong.

It raises, exits 2, and the message names the working spelling:

```
$ python seneschal/scripts/reminders_cadence.py --parse "every 3.5 days"
! 'every 3.5 days': a fractional interval cannot fire — the due test is a date comparison and the
  seed runs once a day. Write it as a rate: '2 per 7 days'.
```

**Which spelling wins, and why it is not merely the implementable one.** `3.5` days is exactly
`2 per 7`. The rate spelling wins because it is the one that is **true**: it says what is meant (two per
week) without asserting a half-day offset the system cannot honour, and it lands on whole days at a
stable time-of-day.

The suggestion is computed, not enumerated: one event per `p/q` days is `q` events per `p` days, so
`Fraction("3.5") = 7/2` → `2 per 7 days`. Parsed from the **text**, never a float —
`Fraction(0.1)` is `3602879701896397/36028797018963968` and its `limit_denominator` is noise. Where the
fraction implies **more than one event per day** (`every 0.5 days`), no sane rate says it and the message
points at `multiple/day` + `Times` instead.

`every 3.0 days` **is** `every 3 days`. The refusal is about a fraction that cannot land on a day, not
about a decimal point.

### 5.1 And an unparseable cadence FAILS OPEN

A row whose `Cadence` will not parse is **due today, and reported** — never silently dropped. A reminder
that cannot be scheduled is better delivered too often than never; it matches the existing *"empty
`Last Acknowledged` ⇒ due now"* convention; and a typo that nags is a typo that gets fixed, where a typo
that goes quiet is not. This is the same polarity as every other reminder gate in the tree: **fail-open
on unknown.**

## 6. Phase 1 — what shipped, and why it needs no store change

**Every classic select option is already a well-formed expression in this grammar, and parses to
exactly the behaviour it always had.** `Daily`, `Weekdays`, `Every 2/3/4/5 days`, `Weekly`,
`Multiple/day`, `One-off` — asserted in `test_reminders_cadence.py` against the list spelled exactly as
the Notion select holds it.

That is the whole migration argument, and it is why **there is no window in which the store holds old
values and the code is new in a way that misbehaves**: the old values *are* valid input. An existing
`Every 4 days` row acked on day 0 still fires on day 4. **No flag day, and nothing to roll back.**

Shipped in phase 1:

* `../scripts/reminders_cadence.py` — the grammar, the Bresenham spread, the due-test, the CLI
  (`--parse`, `--gaps`, `--due`, `--audit`).
* `reminders_acks.py` delegates its one cadence read to `is_multi_fire`, which is **contractually never
  narrower** than the substring test it replaced (an unparseable value falls back to exactly that
  substring). A narrower predicate would newly *gate* multi-fire rolls that the fire-time ack gate has
  always exempted — a silent-suppression bug. Because no input distinguishes the two behaviourally, the
  wiring is asserted **structurally**: `reminders_acks` no longer spells a cadence value at all.
* The two reference files describe the mechanism; the Reminders subagent calls the CLI instead of doing
  date arithmetic in prose.

The filesystem backends store `cadence` as free text already, so on `obsidian` / `markdown` any
expression is typeable today; phase 2 is a Notion-only concern.

## 7. Phase 2 — the Notion property (the owner's, not a change set's)

Nothing below is done by this change, and **nothing below is required for phase 1 to be complete.** It
is a one-time edit in the owner's Notion workspace, which holds the live ⏰ rows.

### 7.1 Pre-flight — prove every live row parses first

On the daemon's checkout (the only one whose `state/` holds the live id cache):

```
python seneschal/scripts/reminders_cadence.py --audit
```

It reads every row's `Cadence` out of `state/reminders-id-cache.md`, parses each, prints one `!` line
per failure and a JSON summary, and **exits 1 if any row fails**. Expect
`{"total": N, "parsed": N, "failed": 0, "ok": true}`. Anything else is a row to look at *before*
touching the property, not after.

Without the daemon's checkout, the same check runs on a pasted list:

```
python seneschal/scripts/reminders_cadence.py --audit --from-stdin < cadences.txt
```

The id cache is gitignored and can be stale, so **a clean audit is evidence, not proof**; the real
guarantee is §6 — the classic options are the only values the select can hold.

### 7.2 The conversion

In Notion, on the ⏰ Reminders database: change the `Cadence` property's type from **Select** to
**Text**. Notion keeps each row's option *name* as its text, so **there is no data migration** — the
strings survive verbatim and every one of them still parses.

### 7.3 What it costs, stated rather than discovered

* **The pick-list goes.** Entering a cadence becomes typing one. That is the point (an arbitrary
  interval becomes typeable), and it is also the whole downside: a typo is now possible where it was
  not. Mitigated by §5.1 — an unparseable cadence nags rather than going quiet — and by `--parse` as a
  one-command check.
* **The option colours go**, and any Notion **view that filters or groups on `Cadence` as a select**
  will need its filter rewritten as a text condition. Check the views before converting.
* **Reversible with a caveat.** Converting back to Select re-creates options from the distinct text
  values present at that moment — so a row set to `every 11 days` becomes an option named
  `every 11 days`. Nothing is lost; the tidiness is.
* **No code change accompanies it**, in either direction. That is the test of whether phase 1 was built
  right.

### 7.4 Behaviour in the in-between window

There is no in-between window with a behaviour of its own. Before the conversion, after it, and during
it, the parser reads whatever string the property holds, and every string it can hold is valid. **Old
rows keep firing throughout** — satisfied by making the old vocabulary a subset of the new one rather
than by sequencing a cutover.

## 8. Non-goals

* **No sub-day cadence.** `multiple/day` + `Times` + `reminders_roll.py` own intraday firing, and §5
  refuses anything that would encroach.
* **No second field.** *"Two fields — a number and a unit"* was considered and rejected: two fields
  that must agree are two fields that can disagree, and a `Cadence` + `Cadence Detail` pair would need a
  rule for `Every N days` with an empty detail. One expression has no such state.
* **No natural-language front end.** `ack.py` owns *"what did the owner mean"*; this is a grammar.
* **Nothing fires differently for an existing row.** Every behaviour change here is reachable only by
  writing a cadence nobody could write before.

## 9. What this leaves open

* **Whether the owner converts the property at all.** The pick-list may be preferable and
  `every 11 days` never needed. Phase 1 is worth having either way: it removes the prose arithmetic,
  makes the due-test testable, and means the *next* interval costs nothing.
* **`Weekly` vs `1 per 7 days`.** Both are about weekly and they behave differently on a late ack (§4).
  Unifying them would be a decision, not a refactor — it would move live rows.
* **A cadence the Brief/Wrap could *show*.** `next_due` exists and nothing renders it yet.

## Router entry

**Status:** PARTIAL (phase 1 — the grammar, due-test and wiring — BUILT; phase 2, the Notion select →
text conversion, is the owner's host-side action).

**What it decides:** **⏰ `Cadence` is an EXPRESSION, not a list of options** —
`../scripts/reminders_cadence.py`. The interval family was the only part of the list that grew, and each
growth cost a schema edit plus doc edits to express one integer. **Two decisions, both explicit.** ①
**An interval is ACK-RELATIVE; a rate is GRID-ANCHORED** — `every N days` means *N days after you last
did it*; `N per D days` means *N times in every D days*, so the count is the semantic and only a fixed
lattice keeps it true. That choice also removes the new state the alternative required: a rate row adds
no ⏰ property and Dream has nothing to reconcile. The spread is Bresenham / Euclidean rhythm, anchored
on Monday 2024-01-01. ② **Fractional days are REFUSED, loudly, and told the working spelling**:
`every 3.5 days` exits 2 naming `2 per 7 days`. **Cron is rejected on the semantics.** **An unparseable
cadence FAILS OPEN.** **Phase 1 needs no store change and has no flag day**: every classic option is
already a well-formed expression; §7 is the runbook + `--audit` pre-flight for the optional Notion
property conversion.
