# Reminders & Nudges — policy (escalation, tone, exact times)

The single source of truth for **how** the assistant nudges the owner. The `reminders` subagent owns the
*mechanics* (read state, fire, write); this file owns the *behavior* — what fires when, how it escalates,
when it stops, and exactly how the "gentle rib" sounds. State lives in the store's **`reminders`** domain
(`../store/<backend>/schema.md`; on the Notion backend, the **⏰ Reminders** DB in `databases.md`).

**Field names.** This file names reminder fields by their display labels (`Status`, `Importance`,
`Nag Until Done`, `Last Acknowledged`, `Consecutive Misses`, `Due / Target`, …). Store-verb prose uses
the canonical keys and emoji-free values from the active backend's `schema.md` — `status: done` /
`finished`, `importance: critical`, `nag_until_done`, `last_acknowledged`, `consecutive_misses`. The
emoji-bearing labels below (`🚨 Critical`, `⭐ High`, …) are the Notion rendering of those canonical
values; the translation table lives in the Notion backend's `schema.md` (rendered per install from
`store/notion/schema.template.md`).

Tone is governed by the persona (`../../persona/persona.md`, else `persona.default.md`):
persistent-not-pushy on what matters, dry and factual on repeated small skips, **never shaming**. Nudges
support the owner's autonomy; they never moralize, guilt, or pile on.

**Prioritization advisor (pull vs push).** A **nudge** is a *push* surface — the Prioritization advisor
(`advisor-chain.md`) trims it to the **adaptive vital-few** (interrupting is attention-expensive), then
these mechanics fire the chosen items **staggered, one per buzz** — the gate picks *which* few, it never
merges them into one message. A **status digest** is a *pull* surface (the owner opened it): rank it, but
show everything.

## What a reminder is

Three `Type`s, one durable row each (never one row per day):

| Type | What it is | "Done" truth lives in |
|------|-----------|------------------------|
| **Recurring Habit** | water the plants, eat, stretch — repeats on a `Cadence` | the Reminders row itself |
| **Today Todo** | something the owner said they'd do today, not yet confirmed | the linked **Task** (`Related Task`) |
| **Deadline Watch** | a Goal/Task with a deadline approaching | the linked **Goal/Task** (`Related Goal`/`Related Task`) |

Today-Todo and Deadline-Watch **never copy content** — they carry a relation + `Due / Target`; the linked
record's `Status` is the source of truth for whether it's actually done.

**Every nudge traces to a row the owner created — the assistant never invents one.** In particular it
never prescribes sleep or bedtimes unprompted: the owner's hours are theirs by design (the
after-midnight day-boundary rule exists precisely because people keep late hours), and an unrequested
"go to bed" is a register error, not care.

## Exact per-reminder times + the daily seed (owner-local)

Reminders fire at **arbitrary per-reminder times**, not four fixed slots (the slots are retired — see
`../docs/reminder-exact-time-scheduling-spec.md`). Each reminder row carries a **`Times`** field — a
comma-list of `HH:MM` owner-local times (e.g. `08:00` or `08:00, 20:00`). When `Times` is empty the row
falls back to its **`Time Window`** default (kept as coarse sugar):

| `Time Window` (fallback) | Default time (owner-local) |
|--------------------------|----------------------------|
| `Morning` | 08:00 |
| `Midday` | 12:30 |
| `Evening` | 18:30 |
| `Bedtime` | 21:30 |
| `Anytime` | 09:00 |

These defaults are the **migration anchors** (each is an old fixed slot's time), so an un-migrated row
fires at exactly the minute it used to. File a habit at the time the owner actually *acts on it*, not a
default — e.g. **evening-only habits** (an evening stretch, a wind-down routine) belong at a bedtime
hour, not the morning; filing them in the morning just accrues phantom mid-day "overdue". (An
evening-only habit's morning → bedtime move graduated via an approved Dream learning — see
`autonomy-policy.md`.)

**How firing works — seed once, deliver by the minute.** The presence daemon runs a **once-per-local-day
seed pass** (`presence.maybe_seed_day`, spawned on the first tick of each new owner-local date — a
*date-rollover* trigger, **not** a fixed clock time, so a machine asleep through midnight still seeds on
wake, with no catch-up cliff that could skip the reset). The seed applies pending acks, runs the **daily
reset**, retires any completed One-off (below), and — for every reminder row due today — queues that
row's exact-time nudges into `state/reminders.json` via `reminders_seed.py` (primary time(s) + the re-fire
ladder below). The daemon's ~5 s delivery tick (`sentinel.check_reminders`) then fires each entry at its
due minute, through the usual quiet / curfew / ack / presence / staleness / catch-up-stagger gates. The
seed decides *what* and *when*; the tick owns delivery — the same enqueue/deliver split the four slots
used, with compute collapsed from four fixed wakes to one rollover wake.

**The seed speaks up rather than deciding.** It prints one `!` line on stderr per row it wants a human to
see — a `⭐ High`-or-above row seeded without the re-fire ladder (see the decoupling below), or a row due
for a **premise-review** question (*"is this still a thing?"* — never a verdict;
`../docs/reminder-premise-spec.md`: a reminder row has no field for *why* it exists, and this report-only
question is how a row that has outlived its reason gets noticed). The Reminders subagent copies those
lines into the Run Log verbatim and **never acts on a premise-review line itself** — it relays it to the
owner.

**Exact times are the earliest fire, not a guarantee of isolation.** The catch-up stagger (below) still
holds non-piercing nudges to ≥15 min apart, so several rows set close together drip out rather than
buzzing at once; piercing items (`Call Me` / `🚨`+ / `pierce_quiet` rows) fire exactly on time. Set the
minute you want, and know closely-spaced *non*-piercing nudges may be pushed later by that 15-min floor —
unless the owner acks, which advances the drip after a 2-min debounce (see "Catch-up stagger").

## States & transitions

`Status` ∈ `Pending`, `Reminded`, `Done`, `Skipped`, `Snoozed`, `Paused`, `Finished`. A **miss** is not a
status — it's `Consecutive Misses += 1` when a `Reminded`/`Pending` item rolls to the next cycle
un-acknowledged.

Per reconcile pass:

1. **Daily reset** — *the daily **seed** pass, once per owner-local day (first tick after local
   midnight); **daily** and **weekdays** cadences only.*
   **Apply any pending acks first** (step 3 — a tick made since the last run credits **yesterday**); an
   acked row is not a miss. Then, for each such habit whose `Cadence` applies today:
   - If yesterday's `Status` was `Reminded` or `Pending` (never acked) → `Consecutive Misses += 1` **first**.
   - Then set `Status = Pending`, clear `Reminded Today` (leave `Last Acknowledged` alone — it's the
     durable done-record the Wrap reads).
   - **Never zero `Consecutive Misses` here** — only an actual `Done` resets it. This is what powers
     "0 for 5 days."
2. **Seed/fire** — for each `Pending` row due today the seed enqueues its exact-time nudge(s) via
   `reminders_seed.py` (which stamps each with the row's ref, so a later ack can cancel any
   still-pending nudge for this row — step 3 / Delivery) and sets `Status = Reminded`, `Last Reminded =
   today`, `Reminded Today = true`. The ~5 s delivery tick then fires each at its due minute.
3. **Ack done** — three equivalent sources: the owner sets the one-tap `Ack` in the store; they tell the
   assistant in chat (Chat writes it through immediately — the same fields below, not just the flag); or
   the linked Task/Goal is now `Done`. Apply — **`status: done` for *every* `Type`** (Recurring Habit,
   Today Todo, and Deadline Watch alike): `done` means "done *for today*" — the item stays an **active**
   reminder and comes back on its next due cycle (a one-time item re-fires tomorrow while still within its
   window). Also set `Last Acknowledged = today`, `Consecutive Misses = 0`, then **reset `Ack`**.
   **`finished` is a separate terminal value — never written on a plain ack.** The assistant writes
   `finished` (retiring the reminder — it drops out of the owner's *active*-reminders filter and stops
   re-firing) *only* when the owner **explicitly** says they're finished / done-for-good with the item;
   "done" / "did it" / an `Ack` tick all mean `done`, not `finished`. (Exactly one other thing writes
   `finished`, and it is not an ack path: the seed's **One-off auto-retire** below, for a one-off already
   acked on or after its due date whose due date has since passed.) (An owner-side Notion `Ack`
   automation, on the Notion backend, should write `Done` on a tick for every `Type` too, so the
   assistant's direct field-write matches it.)
   **Then cancel the obsolete re-nudge.** The daemon fires queued nudges by `due_at` and can't read acks,
   so a still-pending, pre-staggered nudge for this same row would fire anyway (e.g. an item acked at
   08:15, but its staggered 08:45 nudge still buzzes). Immediately run
   `scripts/reminders_dequeue.py --reminder-id <this row's ref>` to drop **this activity day's**
   un-fired `state/reminders.json` entries for the row (fired history is left alone). For this to work
   the enqueue must have stamped the entry with `--reminder-id <ref>` (see the **Seed/fire** step and
   `../state/README.md`).
   **The day scoping is load-bearing between midnight and the day-boundary hour.** A cancel that took
   *every* un-fired entry for the row, with no dates involved, is harmless all day and destructive at
   01:30 — when the day's own nudges have already fired (kept, as history) and the ones still un-fired
   are **tomorrow's**: an after-midnight ack would silently delete the coming evening's nudges. So an
   entry is removed only if its `due_at`, read in the owner's timezone and put through the same
   after-midnight rule (before `owner.dayBoundaryHour`, default 05:00, the activity day is still
   yesterday), lands on the ack's activity day — a nudge due 00:30 local still belongs to the evening
   before it and still goes. The day is `--activity-day` if given, else `--ack-date`, else now through
   that rule; **`--id` is exact and never scoped**; and an entry whose `due_at` can't be read is kept,
   because nothing recovers a deleted nudge. The JSON reports the `activity_day` it used.
   **That same call also records the ack** to `state/acks.json` (the durable ack ledger), which is what
   stops a nudge the dequeue *can't* reach — a **soft-digest** covering this row — from buzzing: the fire
   path (`check_reminders`) suppresses any un-fired entry whose row (or every member of a digest) is acked
   today (see "Fire-time ack gate" below). Run the dequeue **once per acked row** so every ack — chat
   write-through, an `Ack` tick you reconcile, or a linked flip — lands on the ledger.

   **One call does the whole local half of an ack — `scripts/ack.py`.** Give it the owner's words
   (`python scripts/ack.py "evening stretch" "watered the plants"`) and it resolves each phrase to a
   reminder row, runs the dequeue, and — **on the Notion backend** — journals the ack through
   `outbox.py ack` (the write-behind outbox is Notion-only; `store/notion/mapping.md`). It exists because
   nothing else maps the owner's phrasings to a row: the ref was hand-resolved and then fanned out into
   several calls per row. **It refuses rather than guesses** — an ambiguous phrase exits non-zero naming
   the candidates, and so does one whose row isn't in the id cache. That default is not caution for its
   own sake: an ack that lands on the *wrong* one of two similar rows (a required vs. an optional
   variant of the same habit) is a false record that the next Wrap has to walk back. The owner's
   vocabulary is tracked in `references/reminder-aliases.json` (gitignored; seeded from
   `reminder-aliases.example.json`), and the row refs stay in the gitignored
   `../state/reminders-id-cache.md`. **Paired morning/evening rows resolve off the local clock** against
   a per-pair cutover, and a phrase that names the *other* side than the clock chose is refused rather
   than written either way. `ack.py` **writes nothing to the store itself** — the direct `store-update`
   (`status: done`, `last_acknowledged: today`, `consecutive_misses: 0`) is still the turn's job, and on
   the Notion backend the outbox is the backstop. Which component owns which half of an ack (the local
   ledger, the store write, the outbox) is surveyed in `../docs/ack-system-ownership-spec.md`.

   **Stage 2 — the rows nobody wrote an alias for.** The alias table is exactly right for **habits**:
   *"watered the plants"* is the same words for months. It is exactly wrong for **todos** — a todo lives
   for a few days and retires, so hand-authoring an alias for one costs more than the hand lookup it
   replaces and is permanently behind. So on a stage-1 **miss** (and only then — an alias *ambiguity*
   stays a refusal) `ack.py` matches the phrase against the **titles of the reminder rows that are active
   right now**, read live through one query (`scripts/reminders_live.py` — **Notion backend only**: a
   subscription-billed `claude -p` one-shot where the model copies the table out and decides nothing; on
   a filesystem backend it declines without spawning anything, so a stage-1 miss simply refuses).
   **Stage 2's bar is higher than stage 1's, not lower**, because inference is more dangerous than
   curated vocabulary: the row's title must account for **every distinctive word the owner said**, only
   `Status`-active rows are candidates (a `finished` match would resurrect a dead todo), and a
   **near-tie is a refusal naming both** — the required-vs-optional wrong ack above, refused by
   construction. A title match is labelled as one (`matched_by: "title"`, `by_title` in the JSON, the row
   printed in full), because an inferred ack that looks identical to a curated one is unreviewable. Add a
   **habit** phrasing it didn't know to `reminder-aliases.json`; a todo's wording is not worth tracking,
   which is the whole reason stage 2 exists.

   This is **act-low** — removing a redundant nudge to the owner. The `Ack` flag is a **one-shot input,
   consumed on read** — never the durable record (a flag left standing would auto-complete the next
   cycle). Durable proof-of-done for a day is `Last Acknowledged = that day` (+ `status: done` until the
   next reset); **the EOD Wrap counts "done today" from `Last Acknowledged`, never from `Ack`.** For a
   **Today Todo**, also *propose* flipping the `Related Task → Done` (**ask-high** — see below).
4. **Snooze / "later"** → `Status = Snoozed` and enqueue one fresh nudge **+30 min** out (an ad-hoc
   `reminders_enqueue.py` at `now+30m` carrying the row's `reminder_id`). Becomes a miss only if still
   `Snoozed` at the next daily reset.
5. **Skip / "not today"** → `Status = Skipped`, `Last Acknowledged = today`. An honest skip is **not** a
   miss and does **not** reset the streak (they answered; they just chose no).
6. **Paused** — manual off-switch; never fires, never accrues misses, until un-paused.

### Auto-retire a completed One-off (seed pass)

A `One-off` row the owner acked lands at `status: done`, and `done` means *done for today* — so the row
stays **active**. Once its `Due / Target` slips into the past it therefore reads as **overdue** at every
subsequent seed and re-fires indefinitely, escalating as Critical if it carries that importance. The
failure this closes: a one-off bill, paid and acked on its due date, re-surfaced as a Critical item
six days after the money moved. Nobody re-pays a bill. A one-off past its due date with evidence it was
acked is finished in practice; the tracker just had no rule saying so.

So, at the **daily seed pass, before enqueuing any nudges**, retire a row when **all four** hold:

1. `Cadence = one-off` *(the rule keys on `Cadence`, not on `Type`)*
2. `status: done`
3. `Last Acknowledged` is **set** *and* **`>= Due / Target`**
4. `Due / Target` is **strictly before** today's owner-local date

→ write `status: finished`. **Leave `Last Acknowledged` and `Consecutive Misses` untouched** (the first
is the Wrap's evidence, the second is history), and enqueue **no** nudges for that row.

**Condition 3 is the safety rail and is not optional.** It is positive evidence the owner actually did
the thing — the ack landed on or after the day it was due. Without it the rule would retire rows sitting
at `done` for unrelated reasons (a stale write, a partial run) and **silently drop work the owner still
owes**. Auto-finishing something they haven't done is *strictly worse* than the bug being fixed: the bug
is noisy and visible; this failure would be silent.

**Nothing else is in scope.** Any other `Cadence` (a habit re-firing after `done` is the entire *point*
of `done`), any other `Status` (`Pending` / `Reminded` / `Snoozed` / `Paused` / `Skipped`), a row with
**no** `Last Acknowledged` (unacked means genuinely still owed), and a row due **today** (strictly-past
only) are all left exactly as they are. A **linked** Task/Goal is never flipped — that stays
**ask-high**, always.

**Log every auto-retirement** in the seed's Run Log entry — row title, `Due / Target`, `Last
Acknowledged`, one line each. A silent retirement is unauditable, and "no silent caps" applies here too:
if this rule is ever wrong about a row, the Run Log is the only place that will show it.

## Importance, the Nag-Until-Done flag, and re-firing (core behavior)

Two fields drive escalation, and they're **genuinely independent**: one answers *how loud*, the other
*does it chase*.

- **`Importance`** (top → bottom): `🛑 Super-Critical` > `🚨 Critical` > `⭐ High` > `✨ Notable` > `📌 Low`
  (canonical `super-critical` … `low`). Drives nudge **ordering**, **rib eligibility**, and the
  quiet-window **pierce** (Critical-and-above). **How loud it is, and whether it gets through a quiet
  window.**
- **`Nag Until Done`** (flag): **the re-fire ladder, and nothing else drives it** — for anything the owner
  wants chased, low-stakes (water the plants) or not. **Whether it chases.**

**An item RE-FIRES until `done` when `nag_until_done = true` — and only then.** `Importance` does not
imply it. The seed queues a nagging row at its primary time **plus every 90 minutes** through the end of
the local day; the **fire-time ack gate** cancels the day's remaining re-fires the moment the owner acks
(one ack → the rest go silent), and it re-seeds the next day until confirmed. **Persistence is in
frequency, not sharpness:** the tone stays the same calm "still open" line, never escalating into
nagging-as-pressure.

**Everything else** — any row with **`nag_until_done = false`**, whatever its `Importance` — **fires once
per cycle, then stops.** No same-day re-nagging; misses accumulate silently in `Consecutive Misses`,
which feeds the rib (rib eligibility is still `📌 Low` / `✨ Notable` only — see "The gentle rib").

### Why the two are decoupled (and the upgrade migration)

**The older rule** re-fired an item when `Importance` ∈ (Super-Critical, Critical, High) **OR**
`Nag Until Done = true`. One field therefore answered two unrelated questions — *how loud* and *does it
chase* — and there was no way to say "this is important, tell me once": the flag could only ever *add*
laddering to a row, never take it away.

**Upgrading an install from the older rule: migrate first, on purpose.** Before the decoupled rule goes
live, set `nag_until_done = true` on every active row at `importance` ∈ (super-critical, critical, high)
that has it unset, so the change lands **behavior-preserving**: every row that laddered the day before
ladders the day after, for a reason now written on the row itself.

**The one live foot-gun, and what watches it.** A **new** row created at `⭐ High` or above with the flag
unset will **not** chase. That is the intended semantics and it is quiet, so the seed says it out loud:
`reminders_seed.py` prints one `!` line on stderr per such row and writes `"no_ladder": true` (with the
importance) into `state/seed-log.jsonl` — `ladder_gap()`. The Reminders subagent copies the seed's `!`
lines into the Run Log verbatim, so the choice shows up where a human reads it, and the log outlives
`reminders.json` (a live queue, pruned as entries fire). It **refuses nothing and defaults nothing**:
the row seeds exactly as configured.

**Pierce did not change.** What gets through a quiet window / the night curfew is still `Call Me` plus
Critical-and-above, marked `pierce_quiet` at enqueue and read by `sentinel.entry_pierces_quiet` — a
different mechanism from the ladder, still keyed on `Importance`. The pierce set clears quiet, the
curfew, the live-session defer, the staleness cutoff and the stagger. It does **not** clear a
`require_place` presence hold — see "Presence gate".

### Phone-call escalation — the `Call Me` flag

For the rare **can't-miss** reminder (a flight, a court date), a silent Telegram line isn't enough.
`Call Me` (a flag on the Reminders row, **independent of `Importance`** — like `Nag Until Done`) opts
the item into a **phone call**: when it's due, the assistant *rings the owner's own phone* and speaks one
short, calm line (persona register: *"It's your assistant. Quick reminder: …"*). It escalates the
**channel**, not the re-fire cadence — the calm, persistent-not-pushy rule still holds.

- **How it's wired (v1).** The brain enqueues **two** entries for a `Call Me` item: the normal Telegram
  nudge at due time, **plus** a `channel: call` entry due a few minutes later (`reminders_enqueue.py
  --channel call`, `--id <id>-call`). Because the local daemon can't yet read acks, this is a
  belt-and-suspenders ring, not an ack-gated one — acceptable because `Call Me` is opt-in and rare. The
  `-call` id makes it fire **at most once per seeded instant** (idempotent). A `call` entry's `text` is **spoken**,
  so phrase it for the ear (no ⏰ prefix — TTS would read the emoji aloud).
- **v2 (designed-for).** Once the ack read-back path exists (see "Acknowledgment channel" below), the call
  becomes **ack-gated**: place it only if the Telegram nudge went unacked for N minutes. Same fields, no
  state-machine change.
- **Cost + safety.** A call is metered (Twilio) and intrusive, so it's reserved for `Call Me` items;
  everything else stays on the free Telegram/Discord path. Calling *the owner's own number* with their
  *own* reminder is **act-low** (`autonomy-policy.md`); calling any third party stays ask-high.

### Call-until-answered — the "alarm you can't sleep through"

When the owner asks in chat to be **called until they answer** (*"call me at 7am and keep calling until I
pick up"* — a hard alarm), enqueue an **escalating** call rather than a single ring:
`reminders_enqueue.py --channel call --escalate --due-at <UTC>` (optionally `--interval-sec` /
`--max-attempts`). Also enqueue the usual Telegram nudge at the same time as a backup. This is **act-low**
(the owner's own phone, their own explicit request).

- **How it works.** At due time the daemon fires the entry with `push_call.py --escalate`, which tells the
  Worker to start a `CallEscalation` loop: it rings, speaks the line + *"press 1 to let me know you got
  it,"* and **re-calls every 2 minutes (default) up to 15 attempts** until they press a digit or the cap
  is hit. The retry loop runs **server-side in the Worker** (a Durable Object alarm), so it keeps trying
  even if the desktop/daemon is asleep — the daemon only kicks it off.
- **Stop = a keypress.** Pressing any digit (1) acks and cancels the remaining callbacks; merely answering
  (or voicemail) does **not** stop it — that's the point of an alarm. After the cap it gives up quietly
  (the Telegram nudge still stands).
- **Reserve it.** Escalation is the most intrusive channel — for genuine can't-miss/wake-up cases only;
  ordinary `Call Me` stays a single ring. Defaults + the ack route live in `phone/` (see
  `comms-mapping.md`).

### Cadence — when a row is "due today"

**`Cadence` is an EXPRESSION, not a list of options.** It is parsed by `../scripts/reminders_cadence.py`,
which is the authority — **never do this arithmetic by hand, and never add a value to a list to make one
work**:

```
python ../scripts/reminders_cadence.py --due "<Cadence>" --last-ack <Last Acknowledged> \
    [--notes "<Notes>"] [--due-target <Due / Target>] [--lookahead-days <N>]
# -> {"cadence": …, "due": true|false, "next_due": "YYYY-MM-DD"|null}
```

**For a `one-off`, `--lookahead-days` is TYPE-GATED — the caller passes it, the script never guesses.**
`Type = Today Todo` → `--lookahead-days 0`. `Type = Deadline Watch` → omit it (default
`ONE_OFF_LOOKAHEAD_DAYS` = 3). See the split below.

Seven **shapes**. The first five are the values the cadence field has always held; the sixth is the
general form that replaced the enumerated intervals, and the seventh is new.

- **`daily`** — every day. **`weekdays`** — Mon–Fri only.
- **`multiple/day`** — fires at each `HH:MM` in its `Times` (or a **standing roll** for every-N-hours;
  see below).
- **`weekly`** — if `Notes` names a weekday (e.g. "Fridays"), due that weekday; otherwise due when
  `today ≥ Last Acknowledged + 7` (empty ⇒ due now).
- **`one-off`** — due while not `finished` and `Due / Target` is today or overdue, **plus** an
  early-surface window before it that depends on the row's `Type` (a Today Todo for a Monday
  appointment must not fire the Friday before just because every one-off shared one lookahead):
  - **`Type = Deadline Watch`** — due within **~3 days** of `Due / Target` too (`--lookahead-days`
    omitted, default `ONE_OFF_LOOKAHEAD_DAYS`). A deadline is worth surfacing a few days early.
  - **`Type = Today Todo`** — due only on `Due / Target` itself or after (`--lookahead-days 0`). An
    appointment/commitment for a specific day surfaced days early is a false alarm — it would fire every
    day from the lookahead's start through the day itself, not just once.

  (A plain ack writes `done` = *done-for-today* only, so the row re-fires the next day while still
  in-window.) A `one-off` goes silent for good in one of two ways: the owner says they're `finished`,
  or the seed's **One-off auto-retire** fires — acked on/after its due date, and that date now past (see
  "Auto-retire a completed One-off (seed pass)" above). **Whenever a one-off fires on a day other than
  its own `Due / Target`** — a Deadline Watch's early surface, or either Type's overdue re-fire — the
  nudge text names the due date (`reminders_seed.due_suffix`, e.g. *"Renew the passport. — due Mon
  09-14."*), so it reads as a heads-up, never as "do it now." See "Delivery" below.
- **`every N days` — an INTERVAL, and it is ACK-RELATIVE.** Due when `today ≥ Last Acknowledged + N`
  (empty `Last Acknowledged` ⇒ due now). **N is any positive whole number** — `every 11 days` works today
  and needs nothing added anywhere. This subsumes the old `Every 3 days` / `Every 5 days` options, which
  still parse verbatim and still mean exactly what they meant.
- **`N per D days` — a RATE, and it is GRID-ANCHORED.** *"Twice a week."* The D days are divided into N
  as-even-as-possible whole-day gaps (Bresenham: `2 per week` → 3, 4, 3, 4; `3 per week` → 2, 2, 3;
  `3 per 10 days` → 3, 3, 4), and the next due date is the first point of that fixed lattice **strictly
  after** `Last Acknowledged`. Also spelled `N/week`, `N x week`, `N per 7`.

**Why an interval drifts and a rate does not — a design rule, not an implementation detail.**
`every 4 days` means *four days after you last did it*: the offset **is** the meaning, so acking three
days late moves the next one three days later. `2 per week` means *twice in every week*: the **count** is
the meaning, so a late ack rejoins the lattice rather than dragging it — otherwise a slipped week quietly
redefines "twice a week" as "twice per nine days". The lattice is anchored on Monday 2024-01-01, so
`2 per week` lands on **Mon/Thu** and `3 per week` on **Mon/Wed/Fri**. A rate therefore stores **no
cycle position** — the anchor and `Last Acknowledged` determine it — so a rate row needs **no new
field** and there is nothing for Dream to reconcile.

**Fractional days are REFUSED, loudly.** `every 3.5 days` cannot fire: the due test is a date comparison
and the seed runs once a day. The parser exits 2 and names the spelling that works — `2 per 7 days`, the
same cadence on whole days. **That rate spelling wins** for anything expressible both ways. A fraction
denser than daily is pointed at `multiple/day` + `Times` instead. **Never round one to a whole number**:
a value silently floored to a day is exactly the quiet wrongness this file exists to prevent.

**A cadence the parser cannot read FAILS OPEN — treat the row as due today, and tell the owner what it
says and that it did not parse.** A reminder that cannot be scheduled is better delivered too often than
never, the same direction as "empty `Last Acknowledged` ⇒ due now". Never drop the row.

**On the Notion backend, `Cadence` is still a `select`, and that is fine.** All of its options are valid
expressions, so nothing has to change there for any of the above to work — a row reading
`Every 5 days` behaves identically. Converting `Cadence` to a **text** property is what makes arbitrary
expressions *typeable*; it is optional and the owner's to do. Runbook, and what the conversion costs:
`../docs/reminder-cadence-mechanism-spec.md` → "Phase 2 — the Notion property". (The filesystem
backends store `cadence` as free text already.)

The **daily reset** (in the seed pass) only re-`Pending`s **`daily`** and **`weekdays`** habits.
Interval / rate / `weekly` / `one-off` rows aren't reset daily — they flip to `Pending` when the parser
above says they are due. The one exception: a `one-off` that matches all four **auto-retire** conditions
is written `finished` instead of flipping to `Pending`, and seeds nothing.

### Days off — hold work Deadline-Watches to the digest

On a **day off**, a **work** Deadline-Watch does **not** fire as a push nudge — it appears **only in the
Brief / EOD-Wrap digest** as an FYI, and resumes normal push nudging on the next work day. (Graduated at
the owner's request — see `autonomy-policy.md`. Prior to this it was decided ad hoc each pass; the arc
played out cleanly across several holiday/weekend spans — e.g. work deliverable Deadline-Watches held to
the digest over a holiday weekend — before being codified.)

- **"Day off" signal:** the day is off if it's a **weekend** *or* a **calendar-marked PTO / holiday** *or*
  **the owner says so** in chat. (Weekend is the default; a calendar PTO/holiday block or an explicit "I'm
  off today" both count.) When unsure whether a given day is off, treat it as a work day (fire normally).
- **"Work" Deadline-Watch:** a `Deadline Watch` row whose linked record is a **Work** Project/Task/Flag,
  or whose subject is plainly a work deliverable (e.g. "ship the quarterly report"). Non-work
  Deadline-Watches (personal deadlines, hobby deadlines) are unaffected.
- **What still pierces:** `🚨 Critical` / `🛑 Super-Critical` importance **and** `Call Me` items push
  normally even on a day off — criticality is the pierce, not a time threshold. Only `⭐ High` and below
  work Deadline-Watches are held to the digest.
- **Never a drop.** The deadline itself is unchanged; this only suppresses the *standalone nudge* on days
  off, never whether the item is tracked or surfaced. It reappears in the Brief/Wrap the whole time and
  fires on cadence again the next work day.

## Journal-presence gate (auto-satisfy from journal activity)

Some habits are **satisfied by evidence, not by an ack** — the clearest case is **Write in the
journal**. An owner who journals throughout the day makes a blind daily "did you journal?" nudge pure
noise. Instead this row is **journal-presence-gated**: flagged in its `Notes` with
`[gate: journal-presence]` (plus an optional `quiet_days: N`, default `1`).

The seed does **not** queue journal-presence-gated habits (their fire is conditional on a late-day
evidence check the dawn seed can't do). Instead the **Wrap** run **checks for today's journal activity**
rather than nudging cold:

1. **Read the journal** and look for today's entries. On the Notion backend: fetch the Interstitial
   Journal page (`00000000-0000-0000-0000-000000000010`, baked in `databases.md`) and look for a
   **top-level date toggle** whose `<mention-date start="…"/>` equals **today** (the owner's configured
   timezone, after-midnight rule applied) with **≥ 1 timestamped entry** under it (a `startTime` date
   mention, or a bold time at the head of the entry). That toggle is the owner's own writing; **ignore
   the assistant's carry-over `callout`** at the top of the page — the assistant edits that during the
   Brief, so it is *not* evidence the owner journaled, and any `<details>` blocks inside it are decoys
   for a hand-rolled scan. On a filesystem backend, the same test runs over today's journal note.
   **Three verdicts, not two:** a read that failed, a toggle that can't be located, or content present
   but unreadable is **indeterminate** — it licenses **neither** the satisfy nor the nudge.
2. **Journal activity today → auto-satisfy (act-low).** Treat it exactly like an ack: `status: done`,
   `Last Acknowledged = today`, `Consecutive Misses = 0`. **Suppress the nudge entirely** — this is the
   "automatically tick off that I've been using it" behavior; never nudge the owner to do a thing they've
   already done today. (No `Ack` flag to reset — the evidence is the journal itself.)
3. **No journal activity today → nudge, but only past the grace window.** Nudge only once they've gone
   **`quiet_days` consecutive days** with no journal entry (default `1` ⇒ nudge this evening if nothing
   landed today). The nudge is one gentle evening line; it fires once per day (`✨ Notable`,
   `nag_until_done = false` ⇒ no same-day re-nag).

Run the check in the **Wrap** run (nudge-or-satisfy) and again in **Dream** as a **satisfy-only
backstop** (auto-tick if they journaled after the Wrap check; no bedtime nudge). That's one page read in
each of those two runs — within the Notion read budget (`../store/notion/mapping.md` → Throughput);
**don't** fan it out further. The Dream backstop is a real backstop: the read is **live**, so a late
evening read genuinely sees an entry made half an hour earlier.

**On the Notion backend, never compute an age from the fetch's `as of` stamp — decide on content.** That
stamp is the **last-edited time of the newest block still surviving on the page**: a property of
content, not of the read. There is no page cache. Treating it as a read-freshness age inverts the gate in
**both** directions:

- **Quiet day.** The newest surviving edit is the Brief's own early-morning carry-over rewrite, so the
  check scores the read `stale` **precisely when it is most conclusive**, and every night resolves
  indeterminate → no action.
- **The harmful direction.** The owner journals at 14:00, the stamp becomes 14:00, and the evening Wrap
  calls its own positive evidence seven hours stale — **refusing to satisfy a row it can prove, then
  nagging the owner about a thing they did.**

The stamp demonstrably is not a read clock: deleting a block moves it *backwards*, to the prior edit's
exact millisecond, and a clock cannot go backwards. Report it as provenance and subtract it from
nothing. (If a true monotonic last-edited is ever needed, `notion-search`'s per-result `timestamp` does
not revert when a block is deleted. Nothing here needs one.)

This generalizes: any habit whose "done" is provable from a durable source (a page edit, a linked record)
can carry a `[gate: …]` marker and be auto-satisfied the same way instead of nagged. (Graduated at the
owner's request — they asked the assistant to read the journal and "automatically tick off that I've been
using it.")

## The gentle rib (low-importance, non-nag only)

When a **`📌 Low` or `✨ Notable` Recurring Habit with `nag_until_done = false`** reaches **`Consecutive
Misses >= 3`**, the assistant adds **one** dry, data-backed line to the next nudge. Rules:

- **Low-importance, non-nag only.** Super-Critical/Critical/High — and anything flagged `Nag Until Done` —
  are never ribbed. A `Nag Until Done` row is re-fired instead; an important row without the flag is
  simply carried (still surfaced by ordering and by the Wrap's Slipped list), because importance alone
  no longer ladders.
- **At most one rib per reminder per day**, one line, factual.
- **Cite the number** — the rib is data ("0 for 5 days"), not a feeling ("you keep failing").
- **Never shaming, never moralizing, never stacked.** One dry observation, then drop it.

Tone ladder (lowest → highest pressure), with example lines:

| Situation | Register | Example |
|-----------|----------|---------|
| First nudge, anything | neutral, brief | `Plants?` · `Lunch — you flagged it for today.` |
| Important, still open later/next day | persistent, *same* calm tone | `Still open: renew the passport. Carrying since Jun 20.` |
| Low-stakes habit, `misses >= 3` | one dry rib, then drop | `Water the plants — 0 for 5 days. Just noting it.` |
| Low-stakes habit, long streak | dry, a touch wry, still kind | `You and the plants — a real saga. Day 7.` |

Never: `Why haven't you done this?`, `You always skip this`, anything that reads as disappointment.

## Act-low vs ask-high

| Action | Gate |
|--------|------|
| Read reminders, compute due/misses, run the daily reset, set reminder `Status`/counters/`Reminded Today` | **act-low** — a tracker the assistant owns (same class as journal trackers); reversible, inbound, bounded, audited |
| Enqueue the nudge to `state/reminders.json` for the daemon to deliver over Telegram | **act-low** — pushing the owner their own content = inbound (the brief push already graduated) |
| Add a rib line | **act-low** — content in a push to the owner, bounded by the threshold above |
| Set a reminder `status: done` from an ack (any `Type` — done-for-today) | **act-low** |
| Set a reminder `status: finished` (retire it) on the owner's **explicit** "I'm finished with X" | **act-low** |
| **Auto-retire a completed `one-off`** at the seed → `status: finished` (all four conditions above) | **act-low** — same tracker the assistant owns, reversible (flip it back to `done`), and **logged in the Run Log** so a wrong retirement is visible |
| Open/lift a **quiet window** (`quiet_set.py`) on the owner's request — suppress their own non-piercing nudges | **act-low** — reversible, bounded, self-directed (Call Me + Critical+ still pierce) |
| **Bulk-dequeue** a day's ordinary nudges on the owner's "clear my day" (`⭐ High` and below) | **act-low** — same class as the quiet window. But it is **irreversible** (a deleted nudge doesn't come back), so `🚨`+ stays queued unless the owner says otherwise and `Call Me` is never in scope — see "'Clear my day' — a dequeue DELETES…" |
| Flip a **linked Task/Goal** to `Done` | **ask-high** — modifies primary Tasks/Goals beyond a routine tracker |
| Create a persistent **Task** from "remind me to…" | **ask-high** |

See the graduation-log entry in `autonomy-policy.md`.

## Delivery — via the presence daemon over Telegram

Reminders never send SMS directly. The seed (and any ad-hoc enqueue) **queues one nudge per reminder** —
never a combined message (see "One reminder per nudge" below) — into
`seneschal/state/reminders.json` (via `reminders_seed.py` / `scripts/reminders_enqueue.py`); the always-on
**presence daemon**
(`scripts/presence.py`) fires due entries over **Telegram** within a poll cycle and stamps `fired_at`;
**Dream** prunes fired entries nightly (`comms-mapping.md`). The brain (this state machine) decides *what*
and *when*; the daemon owns delivery. If the daemon is down, entries wait in the queue and fire when it's
back. Each entry carries a `channel` the daemon routes on — **telegram** (default), **call** (phone, via
the Worker `/push-call`; `Call Me` items), or **discord** — falling back to Telegram if the requested
channel isn't configured, so a nudge is never silently dropped. Telegram remains the default path.

**A Telegram nudge lands in its own TOPIC, not in the conversation.** A nudge arrives on a schedule the
owner did not pick the moment of, so it lands in the middle of whatever they were saying — the
live-session defer below holds one *while a session is live*, but that is a delay, not a place to put
it. So a Telegram nudge is sent into the **Reminders** private-chat topic
(`telegram_topics.TOPIC_REMINDERS`; `scripts/telegram_send.py --topic`). **Telegram only** — the `call`
and `discord` branches are untouched and there is deliberately no Discord equivalent waiting to be
switched on.

- **🚨 Critical / 🛑 Super-Critical and `Call Me` nudges GO INTO THE TOPIC TOO.** On the Telegram
  clients this was built against, a message in a topic is visible in the topic **and** in the main chat,
  while a main-chat message is visible only in the main chat — so the topic is the *wider* audience, and
  keeping the piercing ones in the main chat would be the *less* visible option. Routing a pierce to the
  topic does not narrow who sees it; it widens it. **That premise is an observation of the real client,
  not a Bot API guarantee**, and it is recorded at the one constant that holds it,
  `telegram_topics.REMINDERS_PIERCING_TO_MAIN_CHAT`, because the cost of the visibility model being
  wrong is a missed can't-miss reminder — that line is what a future Telegram change should send someone
  back to. Nothing about *which* nudges pierce changed here: the pierce set is still defined by what it
  defeats (a quiet window, the night curfew, a day-off hold, the live-session defer), and only where they
  land moved.
- **A topic may only ever change WHERE a nudge lands, never WHETHER.** Private-chat topics are gated on a
  @BotFather Mini App toggle no code change can flip, so with them off every nudge goes exactly where it
  went before, with a byte-identical payload. Topics off, an unreachable `getMe`, a corrupt state file, a
  creation that failed, a stored thread id Telegram now refuses: each one falls back to the main chat and
  the nudge still sends.
- **Acking from inside the topic works unchanged**, and that is the load-bearing part rather than a
  footnote. A 👍 resolves by `message_id` (`telegram-message-map.json`), which says nothing about
  threads; a tap resolves by the question id in its `callback_data`; and a typed *"done"* arrives on the
  same chat id, through the same allowlist and the same offset. The daemon additionally records each
  topic-routed nudge into that thread's continuity cache, so a bare *"done"* typed under it is read
  against the nudge it answers rather than against an empty conversation.

**One reminder per nudge — the bundling pattern is retired (the owner's call).** A single nudge listing
several things either overwhelms (no clear one thing to start on) or gets half-finished (one or two get
done and the rest falls off). So a **queued nudge always carries exactly one reminder** — there is no
combined/digest nudge anymore, and there are **no "may combine" exceptions** (no bundling of a status
list, no bundling of "tightly coupled steps"; if two steps truly belong together, that's one reminder row,
not two rows merged at fire time). When more than one item lands together, enqueue them as **separate**
entries, each with its own stable `--id` so re-runs stay idempotent. Under exact times the seed queues
each at the row's own minute; where several genuinely share a minute, the delivery **catch-up stagger**
spreads them ≥15 min apart (Importance first, then oldest-due) so they drip rather than wall — the seed
need not hand-stagger `due_at`. This clustering is heaviest first thing in the morning, where
Today-Todos, Deadline-Watch, and habits all land together.

This governs the **push** path (queued nudges the assistant initiates). It does **not** restrict a **pull**
answer: when the owner themselves asks "what's still open?", replying with a ranked list in chat is
answering their question, not enqueuing a nudge — that stays allowed (see the pull-vs-push note at the top
of this file).

**An early or overdue `one-off` nudge names its own due date.** A Deadline Watch fires by design up to
~3 days before `Due / Target`; a Today Todo re-fires after it if still unacked. In both cases the bare
habit-style line ("Renew the passport.") reads as "do it now." So `reminders_seed.py`, given
`--due-target`, appends `" — due <Weekday MM-DD>."` whenever the fire's local day differs from
`Due / Target` — *"Renew the passport. — due Mon 09-14. tick Ack or tell me."* — and adds nothing when
firing on the due day itself. This is mechanical (the script computes it from the fire day), not
something composed by hand in the `--text` the seed pass writes.

**A status digest carries its members' ids.** When you *do* combine (a status digest that isn't a
do-this-now list), enqueue it with a `--member-reminder-id <ref>` for **each** row it mentions
(`reminders_enqueue.py`, repeatable). That's what lets the fire-time ack gate (below) drop the whole
digest once the owner has acked *everything* in it — without those member ids, a digest can only ever fire
blind, which was a real production bug (a soft-digest re-nudged five already-acked items). One-per-nudge
do-this-now items don't need this — their single `--reminder-id` already gates them.

## Fire-time ack gate (durable, not session-memory)

The daemon delivers queued nudges by `due_at` and **can't read the store** (on the Notion backend its
only Notion path is the hosted OAuth MCP, reachable from `claude`, not from the stdlib daemon). So a nudge
staggered *before* an ack — or a soft-digest baked at 08:00 covering it — used to buzz for something
already done, because the warm session's knowledge of the ack lived only in volatile context, never where
the fire path could see it. The fix: every ack records the row's key + **today's local date** to
`state/acks.json` (the `reminders_dequeue.py` call the ack step already makes does this), and
`check_reminders` consults that ledger at fire time:

- A due entry whose `reminder_id` is acked **today** is **dropped** — stamped `acked_at` (consumed, never
  re-delivered), exactly like a quiet-suppressed one. A **digest** (`member_reminder_ids`) drops only once
  **every** member is acked today.
- Only *today's* acks gate (yesterday's don't), so tomorrow's re-fire after the daily reset is unaffected.
- **Multi-fire rolls opt out** (`ack_gate: false`): one "checked messages" ack must not cancel the rest of
  the day's pings. A roll is hushed for the day via quiet or an explicit dequeue, not a single ack.
- **Fail-open.** A missing or malformed ledger reads empty, so the gate can only ever *suppress a genuine
  ack* — it can never silence a real nudge. The ledger is durable local state (survives the session
  winding down / a reboot), which is the whole point: acks gate delivery from disk, not from memory.

## The second sender (the Watch-peek ack gate)

Everything above gates **the queue**. It does not gate anything else that can reach the owner — and a
second sender exists. The failure this closes: the owner acked a Super-Critical row; the ack landed in
the store, in `state/acks.json`, and (on the Notion backend) in the outbox, and the queue dequeued its
staggered re-nudges. The queue fired nothing. Forty minutes later the **headless Watch comms peek**
pushed *"… due 2 hours ago …"* — it had gone outside its email/Slack/calendar lane into the reminders
domain, matched `Importance` + `Nag Until Done`, and sent **without reading `Last Acknowledged`,
`Status`, or the ledger**. Two senders, one gate, and no record anywhere that the second one had spoken.

- **Watch must never escalate a reminder row** (`../modes/watch.md`) — reminders have an owning path;
  escalate to Reminders mode instead of pushing.
- **And that is enforced in code, not by this sentence.** `telegram_send.py` refuses (exit 3) a send
  from a Watch surface that *chases* a row acked today. The daemon stamps the peek child
  `SENESCHAL_SESSION_SOURCE=watch` (`presence.child_env("watch")`); the judgment is
  `reminders_acks.watch_escalation_blocked`, which reads **two** independent local sources: this ledger,
  and — on the Notion backend — the row's `Last Acknowledged` as the outbox has actually **landed** it
  (`outbox_common.latest_landed_ack`). Since a Watch push carries no reminder id, the row is identified
  by **title**, and only a message that reads as a *chase* can be blocked, so an escalation that merely
  mentions the topic still goes out.
- **Where that title comes from — best coverage wins.** `reminders.json` stores no title — it stores the
  sentence the assistant sends, template and all — and matching against all of it dilutes the score with
  tokens (`day`, `ack`, the store name) that sit on **every** nudge in the queue, so a genuine chase can
  score under the threshold and send. The title source is therefore, best-coverage-wins:
  1. **`state/reminders-id-cache.md`** — the row's real name in the store, which a stdlib process can
     read without the MCP the daemon doesn't have. A `Cadence = multiple/day` row there is treated as
     `ack_gate: false`, so this source can widen *identification* without widening *what gets gated*;
  2. the queue's own `text` **minus the nudge template** (the `⏰ Reminder:` prefix, the trailing ack
     hint, the `— it's due today (day N).` clause); and
  3. `telegram-message-map.json`'s record of what was actually sent, stripped the same way.

  **The 0.6 threshold is not the knob.** The defect was the denominator; lowering the threshold would
  buy suppression of real escalations to paper over it.
- **Multi-fire rolls (`ack_gate: false`) are exempt here too**, exactly as they are at fire time.
- **Fail-open on unknown, fail-closed on acked:** only a *positive* ack-today reading suppresses.
  Unreadable ledger, absent outbox, unknown title, any error at all → the push goes out. A genuinely
  missed Super-Critical item costs incomparably more than a duplicate nudge.
- **Every verdict is recorded** in `state/watch-gate.jsonl`, blocked and allowed alike — the second half
  of the defect was that a Watch push left no trace at all.

### Dedupe

The ack gate above stops the peek re-chasing a row **the owner has acted on**. It does nothing about a
peek re-chasing a fact **nobody has acted on at all** — e.g. one bank alert escalated four times in
sixteen hours about a thing already resolved before the second push fired; the owner's *"I already fixed
that"* changed nothing, because it went into prose in `state/carry-over.md`, which the Watch path has
never read. Adding a per-instance entry to `references/watch-suppressions.json` is exactly the wrong
fix: a suppression list is for standing instructions, not for one fact.

- **What it asks.** *"Has this peek already escalated this exact fact, recently?"* — not an event
  (acked/not-acked) and not a standing instruction (suppress/don't), a THIRD question:
  `reminders_acks.fact_key(text)` normalizes an escalation down to its identifying nouns (account/
  last-4, sender, subject), stripping everything that is guaranteed to change on every repeat of the
  same fact — the emoji/label prefix, every time-of-day and elapsed-duration mention, every currency
  figure, and the "as of"/"since"/"ago"/"overdue" phrasing that frames them.
- **No new store.** `reminders_acks.watch_duplicate_blocked` reads `state/watch-gate.jsonl` — the same
  ledger the ack gate already writes — for a prior row with the same `fact_key` that was **not itself
  blocked** (a push that never reached the owner can't be the thing this one repeats), inside a rolling
  window (`WATCH_DEDUPE_WINDOW_HOURS`, 24h by default).
- **Ships REPORT-ONLY first** (instrument before gate). Every armed send always computes and logs the
  verdict (`dedupe: {fact_key, would_block, duplicate_of, enforced}` on every `watch-gate.jsonl` row), but
  a duplicate only actually blocks the send once `telegram_send.WATCH_DEDUPE_ENFORCE` is set. Flipping it
  is then reading a few days of real `would_block` rows, not a rebuild.
- **Never a second suppression list.** The key is derived from the message text on every call, never
  hand-maintained, and it only ever matches an **identical** fact. It never inspects the 🚨/🛑/`Call
  Me` markers the suppression list's own carve-out exists for — a genuinely NEW critical alert about a
  DIFFERENT fact is structurally outside its reach, because the marker was never what it looks at.
- **The identity comes from the SOURCE when there is one.** "Identical fact" keyed on the prose alone
  fails in practice: the peek re-words every pass, so one alert family mints dozens of distinct
  `fact_key`s in a week and re-escalates after the owner has said it was handled. So an escalation that
  carries `--source-sender` (and `--source-subject`) is keyed by
  `reminders_acks.escalation_fact_key` — sender + normalized subject + account token, e.g.
  `source:alerts@bank.example|notice overdraft|x1234` — which every rewording of that email shares.
  Dedupe matches a prior row on EITHER that key or the prose key, so older rows still count. A
  non-source escalation (Slack, calendar) keys on the prose exactly as before. The row carries both
  (`fact_key` = the gated identity, `fact_key_text` = the prose key) plus `source_sender`/
  `source_subject`, so the ledger says which identity every verdict was judged on.

### Runtime ack

Dedupe stops the peek repeating **itself**. It does nothing when the OWNER is the one who said a fact is
handled — their *"I already fixed that"* goes into carry-over prose the Watch path has never read, and
the next push still fires.

- **A store, `state/watch-acks.json`, and a module, `scripts/watch_ack.py`** — deliberately not folded
  into `reminders_acks.py`: dedupe is a predicate over an existing ledger, this is a new WRITE surface the
  warm session uses. `python scripts/watch_ack.py ack "<the owner's words>" [--key <fact_key>]
  [--for 7d] [--source chat|reaction]` writes `{fact_key, text_as_said, acked_at, expires_at, source}`,
  keyed on the SAME identity dedupe uses (`reminders_acks.escalation_fact_key` — the SOURCE key for an
  email-backed escalation, the prose key otherwise), default expiry 7 days.
- **An ack COVERS THE FAMILY.** Recorded against a source key, it suppresses every later escalation whose
  `--source-sender`/`--source-subject` resolve to the same key — every rewording of one email — for the
  ack's span. Keyed on prose alone, every rewording was its own key and an ack on one covered none of the
  others, so the owner's exasperated *"I know, stop"* matched nothing. The gate checks BOTH of the
  outgoing escalation's keys, so an ack recorded on a prose key keeps blocking exactly what it blocked.
- **Resolution never guesses.** The owner's words are matched (`reminders_acks.title_coverage`, same
  0.6-class bar) against the pool of facts the peek actually escalated in the last 7 days
  (`watch_ack.candidate_facts`, reading `watch-gate.jsonl`'s own un-blocked rows — no new ledger). An
  unmatched or ambiguous phrase refuses (exit 3) and names the candidates, rather than acking the wrong
  fact or a fact that was never escalated. `--key <fact_key>` bypasses matching entirely — the door for a
  caller (the warm chat session) that already resolved the referent from conversation context, e.g. a
  purely deictic *"I fixed that"* naming no distinctive vocabulary of its own. **`--replied-to "<the
  quoted alert>"` is the door for a swipe-reply** — the turn arrives as `(replying to: "<the alert>")
  <the owner's words>`, the quote IS the alert, and it resolves to that alert's own ledger key (the source
  key when it has one) by text prefix, never by matching the owner's words. **A chat turn answering an
  alert the owner replied to MUST call `watch_ack` with that alert's key** — the words alone will usually
  refuse, correctly, and a carry-over note is NOT where the watcher looks.
- **The gate ENFORCES, unconditionally — no report-only phase, no env var.** `watch_ack.ack_blocks`
  computes the OUTGOING escalation's `fact_key` and checks it against the store; a match blocks the send
  (`reason: "watch-acked:<acked_at>"`), logged to the same `watch-gate.jsonl` row shape
  (`watch_ack: {blocked, acked_at}`) so it stays measurable even though it isn't optional. The
  instrument-first doctrine governs an unproven automatic judgment (dedupe); an ack is the owner's
  explicit instruction, so it never needs to earn trust before it can act.
- **Never inspects 🚨/🛑/`Call Me` either**, same reasoning as dedupe: an acked fact is suppressed even
  carrying a Critical marker (the owner acked THAT exact fact), and a genuinely new critical alert about a
  DIFFERENT fact carries a different `fact_key` and is structurally unreachable here.
- **Chat wiring** (`../modes/chat.md`): a Watch-topic ack in the owner's words runs `watch_ack.py ack` in
  the same turn; the assistant reports what it matched or that it refused, and never treats
  `carry-over.md` prose as the record — the exact substitution that lets this gap happen.

### Thread reconciliation

Dedupe stops the peek repeating **itself**; the runtime ack stops it when the OWNER says a fact is
handled. Neither helps when the SOURCE itself later resolves the fact and nobody tells the assistant in
words — the peek reads mail one message at a time and never reconciles a thread, which is how an
overdraft notice can outlive the same sender's later *"your balance is restored"*: the second message
existed, and nothing ever read it.

- **What it asks.** *"Did the SOURCE later say this is resolved?"* — `watch_reconcile.classify` takes the
  escalation's `sender`/`subject`/`text`/`received_at` and fetches the sender's later messages within a
  window (`window_hours`, 48h by default) through the same stdlib doors the peek's own channels already
  use — Gmail (`gmail_api.py`), then Proton (`proton_read.py`), tried in that order. **Never a new door and
  never the peek's own MCP tools** — this runs as a plain subprocess, not a Claude turn.
- **Three verdicts.** **SUPERSEDED** — a later message from the same sender names the same fact
  (`reminders_acks.fact_key` identity, the same normalizer dedupe uses) and carries a resolution signal
  (`references/watch-resolution-signals.json` — a tracked, growable list: "no longer", "resolved",
  "restored", "paid", "cleared", "is now positive", …). **STILL_OPEN** — a later message exists but none
  both name the same fact and resolve it (an unrelated later message, or a status update that doesn't fix
  anything). **UNKNOWN** — no later message at all, or the door couldn't be asked (no credentials
  configured, the bridge down, a raise anywhere) — **fail-open**: the escalation sends, exactly as
  before.
- **What the peek must supply.** Reconciliation only runs when the peek passes `--source-sender` (ideally
  `--source-subject` too) and `--source-received-at` to `telegram_send.py` alongside `--text` — fields the
  peek already has from reading the email it is escalating (`../modes/watch.md` → "Thread reconciliation
  needs the source fields"). The peek passes all three for an email-sourced escalation, and none of them
  for a Slack/calendar finding, which has no thread to reconcile against. Omitting them reads UNKNOWN
  without even touching a door — the correct, unchanged behavior for a non-email finding.
- **Ships REPORT-ONLY first**, the same shape as dedupe. Every armed send always computes and logs the
  verdict (`reconcile: {verdict, would_block, matched, enforced}` on every `watch-gate.jsonl` row), but a
  SUPERSEDED verdict only actually blocks the send once `telegram_send.WATCH_RECONCILE_ENFORCE` is set.
- **Never a second suppression list**, same discipline as dedupe and the runtime ack: identity is derived
  from the message text and the source thread every call, never hand-maintained, and it only ever matches
  an **identical** fact. It never inspects the 🚨/🛑/`Call Me` markers — a superseded fact is suppressed
  even carrying a Critical marker (the fact is resolved), and a genuinely new critical alert about a
  DIFFERENT fact carries a different `fact_key` and is structurally outside its reach.

## Quiet window (do-not-disturb)

When the owner asks to hush the nudges for a while ("quiet till morning", "no nudges tonight"), that
request must be **durable and honored no matter what rebuilds the queue** — the old failure was a chat
"quiet tonight" that the next reconcile silently undid by re-deriving the same nudges from the open
reminder rows (and a standing roll re-seeding itself), so buzzes kept landing all night.

- **One state file, one gate.** Chat mode writes `state/quiet.json` (`scripts/quiet_set.py`); the presence
  daemon checks it at the **single delivery chokepoint** (`sentinel.check_reminders`). The seed and rolls
  go on enqueuing whatever they compute — nothing *fires* while the window is open. Gating at delivery
  (not enqueue) is what makes it robust against a rebuilt queue.
- **Drop, not defer.** A suppressed nudge is **consumed** (`suppressed_at` stamped), never delivered when
  the window lifts — so waking up doesn't trigger an avalanche of everything slept through.
- **What still pierces:** `Call Me` items (the phone ring — channel `call` / an escalating call) **and**
  `🚨 Critical` / `🛑 Super-Critical` items. `⭐ High` and below are dropped for the duration. The daemon is
  store-blind: it reads a per-entry `pierce_quiet` flag, which the **brain sets at enqueue**
  (`--pierce-quiet`) for Critical-and-above, where it can see the row's `Importance`. A call entry pierces
  on its own.
- **Setting it is act-low** (the assistant suppressing its own pushes to the owner; reversible, bounded).
  `quiet_set.py --until-morning` = next 08:00 owner-local (the morning anchor); `--minutes N` /
  `--until-local HH:MM` for other spans; `--clear` lifts it ("you can nudge me again"). The Chat mode
  quiet rule (`../modes/chat.md`) wires it up.
- **A complaint counts as a request.** *"stop nudging me"* / *"it's 2 AM"* / *"I'm going to bed"* is the
  same instruction phrased as exasperation, and answering it conversationally without writing the file
  is a known failure (an "Understood, stopping" followed by eleven more nudges). The quiet request must be
  written before it is answered (`../modes/chat.md`).
- **The night curfew is a backstop under this, not a replacement for it** — see "Night curfew +
  staleness cutoff" below. Quiet is explicit, bounded and can be set at any hour; the curfew is standing,
  fixed to an owner-local small-hours window, and only consumes what leaked in from earlier. When both
  apply the **quiet** signal is the one emitted, so the explicit request keeps naming itself.
- **A quiet window is not a "clear my day", and the difference is the mechanism** — quiet *suppresses at
  fire time*, so the pierce set still runs; a dequeue *deletes*, so nothing is left to pierce. See
  **"'Clear my day' — a dequeue DELETES…"** below.

## Night curfew + staleness cutoff (is this still worth sending?)

The gates before these two all ask *may* we send (acked? quiet? is the owner driving? are they
mid-conversation?) and *how fast* (the stagger). **None of them asks whether the nudge is still worth
sending at all** — an evening nudge due at 23:30 would be exactly as fireable at 04:24 as at 23:31. These
two are that missing question.

**The failure that motivated them.** A live `/assistant` session correctly deferred every non-piercing
nudge through a long evening — held, never dropped, exactly as designed. Meanwhile an unacked nagging row
**re-enqueues on its ladder**, so seven entries for one row plus repeats of three others were waiting
when the hold lifted: **26 entries**. The catch-up stagger drains at one per 15 min = 4/hour. 26 ÷ 4 =
**6 h 30 m**, so a release shortly after 22:00 put the tail at **04:24**. The night tail was
arithmetically guaranteed the moment the hold lifted. The stagger is a *rate limit*, not a *lateness
bound*, and that is the difference the two rules below supply.

- **Night curfew — an owner-local small-hours window (`owner.nightCurfew` in
  `persona/identity.json`, 01:00–07:00 as shipped; `sentinel.in_night_curfew`).** A **non-piercing** nudge that **leaked into** the window is
  **consumed**: `suppressed_at` stamped, `reminder_suppressed_curfew` emitted. Same drop-not-defer
  contract and the same stamping as the quiet window, so the EOD wrap and `Consecutive Misses` count both
  identically.
  - **The predicate is a conjunction, and both halves matter.** The **fire instant** must be inside the
    window **and** the entry's `due_at` must be **before that window occurrence started**. So it consumes
    what leaked in from the evening, and it does **not** touch a nudge genuinely *scheduled* inside the
    small hours — a row the owner set for 01:30 on purpose is not what the curfew is for.
  - Gating on `now`, not on `due_at` alone, is what catches the real case: an item due 22:50 that the
    stagger only releases at 01:20. **No grace period** — due-before-curfew plus firing-inside-curfew is
    consumed even five minutes late. That aggressiveness is deliberate; it's the first knob to soften if
    too much goes missing.
  - **Why the window opens at 01:00, not 23:00:** plenty of owners are up past 23:00 and still want
    those nudges. **That stretch is not left to nothing** — the staleness cutoff governs it, and in the
    replayed incident it is what eats the 23:00–01:00 drips (3 h+ past due by the time the drip reached
    them).
  - **DST-correct, not a frozen offset.** Readings go through the owner's timezone (`tz_common`), never a
    fixed UTC offset. The default window lies entirely after midnight, so the occurrence containing
    `now` begins on `now`'s own local date; an owner who configures a window whose start is after its
    end (e.g. `23:00`–`07:00`) gets a wrapping window whose after-midnight half began the previous local
    date. `start` equal to `end` disables the curfew; an unusable value falls back to the default.
- **Staleness cutoff — `MAX_LATENESS_SEC`, 2 h.** A **non-piercing** nudge more than two hours past due
  has stopped being a reminder and become an interruption, at any hour. Consumed the same way:
  `suppressed_at` + `reminder_suppressed_stale`. Comparison is strictly `>`, so exactly two hours still
  fires.
- **What still pierces — both gates, the same set as quiet and the live-session defer.** `Call Me` rings
  and `🚨 Critical` / `🛑 Super-Critical` (`pierce_quiet`) come through at 3 AM and five hours late. That
  is the entire point of the pierce set (`entry_pierces_quiet`, shared, never duplicated).
- **Presence-held time is exempt from the staleness cutoff — and a `require_place` check would NOT have
  achieved that.** `presence_rules.should_defer` has **two** rules: the place rule carries a per-entry
  marker, and the **driving** rule carries nothing at all (it holds any non-piercing nudge while
  `activity == in_vehicle`). A nudge held through a >2 h drive would therefore reach the cutoff hours late
  with nothing on it to tell it apart from one that was simply ignored — and get consumed, straight
  through the presence gate's **NO DROPS** guarantee. So instead the fire path **accumulates** held time
  on the entry (`presence_deferred_since` opened at defer, folded into `presence_held_sec` on release) and
  **subtracts it** from the lateness computation. Lateness then measures *time the owner could have acted
  on it*, which is what the gate is actually for, and it covers both presence rules uniformly with no
  special case.
  - **This amends the presence gate's "stamp NOTHING" defer contract, deliberately.** That contract exists
    so a deferred entry stays **pending** rather than being consumed; an accumulator does not consume it.
    `fired_at` / `suppressed_at` / `acked_at` are still untouched, so the entry re-checks on the next tick
    exactly as before.
  - **The curfew is deliberately *not* exempted this way.** It asks "is 2 AM a reasonable moment to buzz
    the owner?", and the answer doesn't depend on why we're late.
- **Catch-up-stagger wait is exempt from the staleness cutoff too.** The failure: a `⭐ High` Today Todo
  was seeded into a large morning batch every day for a week and suppressed as stale every single time —
  queued deep in a 10–14-row batch with **no Importance-aware drain order at all**, drained one row per
  15 min, so its raw due-vs-now gap crossed the 2 h cutoff before its turn ever came, and nothing reached
  anywhere the owner or the EOD Wrap reads. Same shape as the presence fix, same reason: `sentinel`
  stamps `stagger_deferred_since` the first tick the stagger gate actually holds a row, and
  `entry_lateness_sec` subtracts the running `now - stagger_deferred_since` segment exactly like
  `presence_held_sec`. **It does not make the drip a hiding place** — a row already stale the very first
  tick it reaches the stagger gate carries an empty (just-opened) segment and still dies right there
  ("stale beats stagger"); only the wait that accrues *after* that instant stops compounding.
- **The drain order is Importance first, then seed order (same fix).** Ties on `due_at` used to break in
  whatever order the seed pass happened to call `reminders_seed.py`, which carries no relationship to
  `Importance` at all — a `⭐ High` row could and did queue behind `📌 Low` ones seeded earlier for the same
  minute. `sentinel._due_sort_key` sorts `(due_at, IMPORTANCE_RANK)` — Super-Critical > Critical > High >
  Notable > Low, an unset/unrecognized value ranking neutrally at Notable — with `sorted()`'s stability
  keeping seed order as the final tiebreak. `reminders_seed.seed_entries` carries the row's `importance`
  onto the queued entry (omitted when the row has none) so there is something to sort on;
  `reminders_enqueue.py --importance` does the same for an ad-hoc nudge.
- **Every staleness suppression is also a durable ledger row.** `reminder_suppressions.record` (sibling
  stdlib module, same never-raises contract as `failures.record`) appends one line per
  `reminder_suppressed_stale` signal to `state/reminder-suppressions.jsonl` — `presence.log`'s
  per-suppression line (below) is a daemon-internal log nobody reads day to day. The EOD Wrap folds
  `reminder_suppressions.count_today` into its Slipped section (`../../subagents/eod-wrap/SKILL.md`), so a
  run of silent kills is visible the first evening, not invisible for a week.
- **Gate order, which is the part that is easy to break later:**
  `acked → quiet → **curfew** → presence → live-session → **staleness** → stagger`. Curfew sits
  immediately under quiet because quiet is the *explicit request* and must keep naming itself when both
  apply, while curfew is the standing default underneath it. Staleness sits **after** the live-session
  defer — a nudge held three hours by a live session *should* die there, that being the exact 04:24
  mechanism — and is **unreachable from a presence hold**, which `continue`s above it. Moving either is a
  behavior change, not a refactor. The stagger, last, is the **only** gate an ack can release (the
  ack-advance, "Catch-up stagger" below) — its position after every drop and defer is what makes that
  safe, since a row the acked gate consumed never reaches it.
- **Both leave a trace.** `presence.py` logs one line per suppressed entry to `presence.log`
  (`log_reminder_suppressions`); a staleness suppression additionally writes the
  `state/reminder-suppressions.jsonl` row above — `presence.log` is forensic replay, the ledger is what a
  Wrap or a human actually reads. Mechanics: `sentinel.in_night_curfew` / `entry_lateness_sec` /
  `_stagger_held_sec` / `_due_sort_key` / `_check_reminders_locked`; tests:
  `scripts/test_night_curfew.py` (a replay of the whole 26-entry night asserting the 04:24 delivery never
  happens), `scripts/test_reminder_stagger_priority.py` (the deep-batch case), and
  `scripts/test_reminder_suppressions.py` (the ledger).

## "Clear my day" — a dequeue DELETES, so it spares Critical+ and never touches `Call Me`

**The rule:** "clear my day" leaves Critical-and-above rows queued unless the owner explicitly says to
clear critical too — and it never touches `Call Me`, which owners use as alarms (to be woken, or for the
one thing that has to reach them).

So when the owner asks the assistant to clear / cancel / wipe the rest of a day's nudges:

| Row | What a "clear my day" does to it |
|---|---|
| `⭐ High` and below | **dequeued** — this is the thing the owner is actually asking for |
| `🚨 Critical` / `🛑 Super-Critical` | **left queued.** The existing fire-time gates decide (quiet, curfew, staleness, presence) — they already know Critical pierces |
| `Call Me` | **never dequeued, on any instruction — including an explicit "clear critical too"** |

- **Critical+ clears only on an explicit instruction** — *"clear critical too"*, *"critical+ as well"*. A
  plain "clear my day", however emphatic, is not one. Naming a *specific* Critical row ("kill the
  passport one") does clear that row: that is naming an item, not clearing a class.
- **`Call Me` is categorically out of scope.** A cleared `Call Me` is a **missed alarm**, which is a
  different kind of loss from a missed nudge, and no phrasing of "clear my day" buys it. If the owner
  wants a particular alarm off, they will say so about *that alarm*.
- **If what the owner wants is the phone to stop, that is `scripts/quiet_set.py`, not a wider dequeue.**
  Quiet is the mechanism that expresses "I don't want to hear anything except what genuinely can't wait",
  because it lets `Call Me` and Critical-and-above through at fire time. Dequeue what they asked to be rid
  of; open a quiet window for the rest of the noise.
- **Say what you left queued.** A clear that silently spares four rows reads as a clear that missed them.
  One line naming what stayed — "left the Critical ladder and the Call Me" — is the whole audit trail.

### The mechanism distinction, which is the actual content of the rule

**A quiet window SUPPRESSES at fire time; a dequeue DELETES.** Same felt intent, opposite mechanism:

| | Quiet window (`scripts/quiet_set.py`) | Dequeue (`scripts/reminders_dequeue.py`) |
|---|---|---|
| When it acts | at delivery, per entry | immediately, on the queue |
| What survives | the entry, until it is consumed or pierces | nothing — the entry is gone from `../state/reminders.json` |
| Does Critical pierce? | **yes** — `entry_pierces_quiet` reads the entry's `pierce_quiet` | **no. There is nothing left to pierce.** |

The pierce set (`Call Me` + Critical-and-above) is a property of the **fire path**. Every gate this file
describes — quiet, curfew, staleness, the live-session defer, the stagger — consults it *when the entry
comes due*. A dequeue runs **before** all of that and removes the entry, so the pierce set is never
consulted at all. Piercing is not something a row carries with it into having been deleted.

**The failure that bought the rule.** An owner asked to clear the rest of their day; the assistant ran a
non-ack cancel over **every** un-fired entry — dozens of nudges — which swept a Critical evening ladder
along with everything else. The quiet window opened at the same time *would* have let Critical through
at fire time — but the dequeue had already **deleted the entries**, so no gate ever saw them. **"Hush my
phone" and "clear my day" behaved identically, and the whole point is that they must not.**

**Nothing in the tree enforces this.** `scripts/reminders_dequeue.py` has no bulk switch and no
importance filter — it cancels the `--reminder-id`s / `--id`s it is handed, which is correct for its job
(an ack cancels one row's obsolete nudges). A bulk clear is a thing a **session** does when asked, so the
selection is the turn's judgment and this section is the only thing standing between the owner and a
sweep that deletes their alarms. **Read each row's `Importance` and `Call Me` before you build the id
list.**

## Presence gate — NO DROPS, and the one place piercing does NOT pierce

*The presence gate sits third in the delivery order (`acked → quiet → curfew → **presence** →
live-session → staleness → stagger`). Its rules live in `scripts/presence_rules.py` (pure, no I/O),
wired into `sentinel._check_reminders_locked`.*

**Two rules, and both DEFER — nothing here ever drops.** A gated nudge fires **late, not never**; only
quiet, the curfew, the staleness cutoff and an ack consume an entry.

| Rule | Holds | Piercing item? |
|---|---|---|
| **`require_place: "<name>"`** (phase 1) — hold until `context['at_place']` equals it, then fire on arrival | any entry carrying the field | **HELD TOO — see below** |
| **driving** (phase 2) — hold while `context['activity'] == 'in_vehicle'`, release when stationary | non-piercing entries only | fires through |

**⚠️ `require_place` is the single exception to the pierce set, and it is deliberate.** `presence_gate`
applies it *to any entry carrying the field, piercing or not* — the entry was tagged place-locked, so that
is honored. So a `Call Me` ring or a `🛑 Super-Critical` item enqueued with `--require-place home` **is
held** while the owner is elsewhere, however loudly it would otherwise pierce. Every other gate's
"piercing always gets through" wording is true; **this one is not**, and the reason is that a place-lock
is a statement about *where the reminder makes sense*, not about how urgent it is — ringing about the
thing in the kitchen while the owner is on the highway is not the can't-miss delivery the pierce set
exists to guarantee. If an item must reach the owner anywhere, **do not give it `require_place`**; that
field is the opt-out from piercing, not a hint.

- **`require_place` is set at enqueue time** (`reminders_enqueue.py --require-place`). The driving rule
  needs no per-entry flag — it applies to every non-piercing nudge.
- **FAIL-OPEN on the signal, per path.** The gate runs only when the context is **fresh**:
  `sentinel._presence_context_fresh` requires `state/presence-context.json` to carry a parseable
  `updated_at` **within 6 h**; absent, stale or unparseable → `False` → **fire**. A dead presence feed can
  only let a nudge through, never hang one forever. (Contrast the merge guard, which fails closed — the
  asymmetry is the point: here the cost of failing closed is a silent reminder.)
- **The `asleep` rule (phase 3) is RETIRED — do not re-add it from the phone alone.** The phone-only Sleep
  API cannot tell a sleeping owner from an idle phone: it has classified "asleep" at high confidence
  straight through a weekday afternoon and held daytime nudges. The context snapshot still *carries*
  `asleep` (the phone keeps sending sleep events) but the gate no longer reads it — **informational
  only**. Even where an event names a wearable as its observing device (`source`, surfaced as
  `sleep_source` in `presence-context.json` — provenance, not precedence), **the gate still does not read
  `asleep`** — reviving it is a reminder-behavior change, unbuilt, and the owner's to call.
- **Held time is netted out of the staleness cutoff**, which is what preserves NO DROPS: the entry gets a
  bookkeeping `presence_deferred_since` stamp on hold and a `presence_held_sec` accumulator on release,
  and `entry_lateness_sec` subtracts it. A presence-held nudge also `continue`s *before* the staleness
  gate, so it can never be eaten there. **The curfew is deliberately NOT exempted this way** — it asks
  "is 2 AM a reasonable moment to buzz the owner?", and the answer does not depend on why we are late.

## Live-session defer (don't buzz into a live conversation)

While a human is **actively engaged in an interactive `/assistant` chat** — the daemon's warm
Telegram/Discord session or a desktop slash session — a due non-piercing nudge shouldn't buzz into the
middle of the conversation. The **session registry** (`state/sessions/`, one small JSON entry per live
session) is how the daemon knows:

- **Defer INTO the session, never drop (by this gate).** At the same delivery chokepoint
  (`sentinel.check_reminders`), a non-piercing due nudge is **held** (stamped nothing → re-checked each
  ~5 s tick) while `sentinel.session_is_live` is true, and fires naturally once the session ages out of
  its 120 s TTL. The redundant Watch comms-peek is skipped too (without consuming its cadence).
- **Only interactive sources gate.** `daemon` (the warm chat, self-registered every turn) and `desktop`
  (a slash session, self-registered via `scripts/session_heartbeat.py`) defer delivery. `build` /
  `scheduled` entries — every other Claude Code session, stamped by the machine-wide
  `scripts/session_stamp.py` hook — are **awareness-only** ("who's live, on what branch"): the owner is
  coding, not conversing, so reminders still buzz normally.
- **The pierce set is the same as quiet's.** `Call Me` + Critical-and-above fire immediately regardless
  (`entry_pierces_quiet`, shared with quiet, not duplicated). A can't-slide item never waits on a
  conversation.
- **Composes with quiet + the other gates.** Piercing clears all of them **except a `require_place`
  hold**, which holds a piercing item too (see "Presence gate" above — the one documented exception): a
  quiet window already drops the non-piercing entry (consumed) before the live-session gate is reached,
  so the live gate only bites when it's *not* quiet.
- **This gate defers, but a long hold does not guarantee eventual delivery.** Holding here drops nothing
  *itself* — but the **staleness cutoff** sits immediately after it, so a non-piercing nudge held past two
  hours by a long conversation is consumed there rather than dripping out in the small hours. That is
  deliberate and is the direct fix for the 04:24 failure: a multi-hour session hold was what built the
  26-entry queue. Contrast the **presence** gate, whose held time is explicitly netted out so it keeps its
  NO DROPS guarantee. See "Night curfew + staleness cutoff".
- **Fail-open.** A missing / malformed / stale registry reads *not live* — the absence of the signal can
  never block a genuine reminder. Mechanics: `sentinel.session_is_live` + the fire loop in
  `sentinel._check_reminders_locked`; `presence.py` (`maybe_peek` skip). State schema:
  `../state/README.md`.

## Catch-up stagger (a released backlog must drip, not wall)

The quiet window *drops* what was slept through, but the **presence gate** (`presence_rules.py`) *holds* —
it defers nudges while the owner is driving / away from a required place, then releases them (no drops).
Without a governor, that release fires **every held nudge in one `check_reminders` pass** — a "wall of 4
nudges" the moment the flag clears (observed in production: a presence flag held four staggered nudges and
delivered them all the instant it lifted). That's the exact bunching the stagger rule exists to prevent.

- **One non-piercing nudge per pass, spaced ≥ `CATCHUP_STAGGER_SEC` (15 min).** At the single delivery
  chokepoint, a **non-piercing** nudge fires only if we haven't already fired one this pass **and** at least
  15 min has elapsed since the last non-piercing fire (`state/nudge-stagger.json` holds that instant,
  durable across ticks/restarts). The rest stay **pending** (stamped nothing → re-checked next loop — defer,
  never drop), so a released backlog drips out Importance-first, then oldest-due, at ~15-min spacing
  instead of walling up.
- **Piercing items skip the gate entirely.** `Call Me` / `🚨 Critical` (`pierce_quiet`) fire the
  moment they're due — a released backlog never delays a can't-slide item, and piercing fires neither
  consume nor reset the drip clock (separate lane).
- **Normal days are mostly untouched.** The seed queues each row at its own exact minute, so nudges set
  ≥15 min apart clear the gate trivially; this reshapes a *bunched release* and also auto-spreads any rows
  that happen to share a minute (they drip 15 min apart). Fail-open: a missing/broken
  `nudge-stagger.json` fires immediately. Mechanics live in `sentinel._check_reminders_locked`.
- **An ack ADVANCES the stagger, after a 2-min debounce.** The 15-min hold spaces a release the owner
  hasn't caught up on; an ack *is* the owner catching up, so waiting out the rest of the window is the
  rate limit punishing the behavior it wants. Every ack route — chat write-through, a Telegram 👍, a
  reconciled `Ack` — ends in `reminders_dequeue.py`, which also stamps `last_ack_at` (UTC ISO) into
  `state/nudge-stagger.json` beside the drip clock. The stagger gate then **also** releases its hold when
  that ack is *newer* than the last non-piercing fire **and** at least `ACK_ADVANCE_DEBOUNCE_SEC`
  (**120 s**) old. The debounce is there so the owner can ack several at once: say three nudges were
  delivered, the owner was busy, then 👍'd all three within seconds. **Wrong:** fire the second, then the
  third (both already acked), then the next pending one. **Right:** realise all three are acked and send
  only the next still-pending, not-yet-delivered, un-acked entry, ~2 min after the last 👍 rather than 15
  min after the last fire. What makes that hold: the `acked` gate runs **first** and consumes the acked
  rows (the dequeue already removed their queued re-nudges), and the debounce makes sure the dequeue for
  the last 👍 has landed before the advance fires anything. Each ack overwrites the stamp, so a burst
  measures from its **last** ack; a fire re-stamps `last_nonpiercing_fire` past the ack, so one burst
  opens the window **once**, not a floodgate — the nudge after that waits the ordinary 15 min or the next
  ack. Still one nudge per pass. **It releases the stagger only:** quiet / curfew / presence /
  live-session / staleness all sit ahead of it and never read the stamp, so an ack cannot pull a nudge
  through a curfew or a presence hold; piercing items were never held. A missing or unparseable
  `last_ack_at` means **no advance** (fail-safe — the advance is earned by an ack, and a garbled stamp has
  earned nothing; the ordinary drip still applies). `--no-ack-record` and `--dry-run` dequeues stamp
  nothing: they are not acks.
- **It is a RATE LIMIT, not a LATENESS BOUND.** 15 min/nudge is 4/hour, so a 26-entry release takes
  **6 h 30 m** to drain and the tail lands wherever that arithmetic puts it — 04:24, in the failure
  above. The stagger has no opinion about *when* the drip ends; it only spaces it. The night curfew and
  the staleness cutoff are the two rules that bound the tail — and they run **before** this gate, so a
  nudge that was *already* no longer worth sending (stale before it ever reached the drip) is consumed
  rather than keeping its place in the queue. See "Night curfew + staleness cutoff".
- **But the drip itself must not MANUFACTURE staleness.** The curfew/staleness pair bounds a *released,
  already-stale* backlog; it does nothing for a same-minute batch where every row is fresh at seed time
  and the rate limit alone pushes a deep-queued row past `MAX_LATENESS_SEC` before its turn comes. Two
  fixes: `entry_lateness_sec` nets out time spent waiting in THIS gate (`_stagger_held_sec`, opened at
  `stagger_deferred_since`) the same way it nets out a presence hold, so merely waiting a rate-limited
  turn no longer compounds into staleness (a row already stale before it started waiting still dies — the
  segment opens empty); and the drain order is **Importance first, then seed order**
  (`sentinel._due_sort_key`), so a `⭐ High` row can no longer sit behind a `📌 Low` one just because the
  seed pass happened to call `reminders_seed.py` for it later in the loop. See "Night curfew + staleness
  cutoff" above for both.

## Standing rolls — custom every-N-hours cadences

Neither a `Times` list nor the seed cleanly expresses *"every 2 hours, 9am–11pm"* — an intraday cadence
some reminders want (the canonical case: a check-messages poll on a service whose notifications are
unreliable). Rather than have the assistant hand-enqueue that roll each day, it's a **standing roll**: a
config entry in `scripts/reminders_roll.py` (`ROLLS`, keyed to the reminder row's `reminder_id`) that the
presence daemon **regenerates once per local day** on its own — pure local queue math, no store read and
no `claude` spawn.

- **Future-only, idempotent, rolling.** Each refill seeds today's still-upcoming slots **and** all of
  tomorrow's (so a late-waking machine is still covered), skipping any slot whose instant has passed and
  any id already queued (stable ids `rmd-<date>-<prefix>-<HH>`). It never back-fires a past slot and
  never double-queues. `state/rolls.json` holds the once-per-day guard; `Dream` prunes fired entries.
- **Acks still cancel — but don't ack-gate.** Every roll nudge carries the row's `reminder_id`, so a chat
  ack's `reminders_dequeue.py --reminder-id <id>` drops the remaining un-fired nudges for the day exactly
  like any other reminder (the row re-seeds tomorrow) — **that activity day's**, so the slots this refill
  has already staged for *tomorrow* survive it, which is the dequeue's day scoping doing its job here. But
  a roll is **multi-fire**, so its entries set `ack_gate: false` — the fire-time ack gate ignores them, so
  acking one ping doesn't silently suppress the rest of the day's (a dequeue is the explicit "stop for
  today"; a single ack isn't).
- **Act-low.** A roll is the assistant pushing its own recurring content to the owner on a fixed cadence —
  same class as any enqueue. Adding/adjusting a roll (interval, window) is a config edit; the cadence
  itself is set with the owner. To add one: a `ROLLS` dict + the reminder row (`Cadence = multiple/day`,
  `Anytime`).

## Resolving relative dates ("tomorrow", "in the morning")

When the owner uses a relative day-word — *"tomorrow," "in the morning," "first thing"* — resolve it
against the **wake-day**, not a naïve calendar+1:

- **Post-midnight sessions** (between midnight and the day-boundary hour, `owner.dayBoundaryHour`,
  default 05:00 — before they've slept): a relative day-word binds to the **upcoming wake-day = the
  current local calendar date**, *not* date+1. If they say "remind me tomorrow morning" at 02:30, that's
  **today's** date (the day they're about to wake into), the same way the timezone rule already counts
  after-midnight activity as the prior day.
- **Normal daytime sessions:** "tomorrow" means the next calendar day, as expected.
- **Explicit anchors always win.** A named weekday or date ("Wednesday," "the 1st") is honored exactly as
  stated and is *not* reinterpreted — that's why a "Wednesday morning" bill stays on Wednesday even in a
  post-midnight chat.

This governs how `reminders_enqueue` computes `due_at` and how the Reminders mode sets `Due / Target`.
(Graduated via approved Dream learning — see `autonomy-policy.md`.)

## Acknowledgment channel (v1 → v2)

- **v1 (now):** the owner acks by the one-tap `ack` affordance in the store **or** telling the assistant
  in a chat session (terminal `/assistant` or Telegram). The seed + each reconcile run reads that state
  first, so any ack before the next run is honored. The nudge ends with *"tick Ack or tell me."* **A chat
  ack is only honored if the Chat session writes it through to the store** — the Reminders row is the
  durable record; an ack that stays in the (volatile, reboot-clearing) warm session is lost and the next
  reconcile re-fires it. Telegram Chat runs headless, so the presence daemon must be wired with the store's
  read/write MCP for this to land — resolved store-config-driven (`store/config.json`; a legacy
  auto-detect of `scripts/notion-mcp.json` on the Notion backend), see `scripts/NOTION_MCP_SETUP.md`.
  Filesystem backends need no MCP. (Chat write-through is the Chat mode ack rule, `../modes/chat.md`.)
  **The one-tap `ack` is consumed, not kept:** the next reconciling run applies it (`status: done`,
  `last_acknowledged: today`, misses zeroed) and **resets `ack`**. So "were things finished today?" is
  answered by `last_acknowledged: today` + `status`, never by scanning for set flags — seeing all `ack`
  flags cleared in the evening is the system working, not evidence nothing got done. Chat write-through
  likewise sets the fields directly and leaves `ack` alone (it's the owner's affordance, not the agents').
- **A 👍 on a nudge is an ack.** Reacting 👍 to a Telegram nudge runs the **same** local path a typed ack
  does — `reminders_dequeue.py` (cancel the obsolete re-nudges + record the durable local ack) and,
  **on the notion backend only** (the write-behind outbox is Notion-specific — `store/notion/mapping.md`,
  Outbox section), an `outbox.py ack` journaling the `status: done` / `last_acknowledged: today` write for
  the next LLM turn to flush; filesystem backends leave the store row to the warm session's
  `store-update`. No typing, no session needed: the daemon does it directly (act-low, the owner's own
  content).
  **The row itself is written LATER, and the threaded line says so.** None of the daemon's calls touches
  the store. A line that claimed *"row marked Done"* would be false and — worse — would make the warm
  session stand down, so the write would have no owner in the one turn holding the row ref, and
  consecutive 👍 acks would never reach the store at all. So the line names what is still owed and asks
  the turn to write it, and on the Notion backend the daemon runs a gated backstop drain if the turn
  doesn't (`../docs/notion-write-behind-outbox-spec.md` §9). *If you are reconciling and a 👍-acked row
  looks untouched, check `python scripts/outbox.py status` before concluding the ack never happened — a
  pending entry means it did.*
  **Narrow on purpose — it only fires for a 👍 (or whatever the owner maps to `ack` in
  `state/telegram-reactions.json`) on a *tracked nudge* that went out *today, local*.** A reminder row
  resets daily and an ack stamps today's date, so honoring a 👍 on yesterday's nudge would mark **today**
  done — for a daily can't-miss item, exactly the wrong outcome. Anything outside that (a 👍 on a chat
  reply, an older nudge, an untracked message) is handed to the warm session as context instead, and it
  decides. Mechanics: `../docs/telegram-inbound-spec.md` §3.4c; the emoji vocabulary is the owner's to
  edit.
- **v2 (later, designed-for):** the warm Telegram session already parses a reply like `done` / `skip` /
  `later <which>` and writes it back to the **same** Reminders fields (v1 already does the write-through
  above); v2 layers on richer reply grammar and ack-gated `Call Me` escalation — no schema or
  state-machine change.

## Guardrails

- **Signal over noise.** A nudge is short — the few things that actually need the owner, never a
  wall. Prefer three real items to ten.
- **Every nudge traces to a row the owner created.** Never invent one; never prescribe sleep unprompted.
- **One reminder per nudge; stagger 15–30 min apart.** Never enqueue more than one item in a single nudge
  (see Delivery — bundling is retired, no exceptions) — it overwhelms or gets half-done. A ranked list is
  only ever a **pull** reply to the owner asking "what's open?", never a queued push.
- **Reversible writes only** without asking; never touch a linked Task/Goal status unprompted.
- **A dequeue deletes.** Never bulk-clear Critical+ without an explicit instruction; never clear `Call Me`.
- **One rib max per reminder per day**, low-importance non-nag only, factual.
- Times and date logic in **the owner's configured timezone**; after-midnight (before the day-boundary
  hour) counts as the prior day.
- Every substantive seed/reconcile run leaves a trace (Run Log `Mode = Reminders` + carry-over).
