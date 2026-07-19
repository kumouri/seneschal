# Reminders & Nudges — policy (escalation, tone, exact times)

The single source of truth for **how** the assistant nudges the owner. The `reminders` subagent owns the
*mechanics* (read state, fire, write); this file owns the *behavior* — what fires when, how it escalates,
when it stops, and exactly how the "gentle rib" sounds. State lives in the **⏰ Reminders** Notion DB
(`databases.md`).

Tone is governed by the persona (`../../persona/persona.md (else persona.default.md)`): persistent-not-pushy on what matters, dry
and factual on repeated small skips, **never shaming**. Nudges support the owner's autonomy; they never
moralize, guilt, or pile on.

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

## Exact per-reminder times + the daily seed (owner-local)

Reminders fire at **arbitrary per-reminder times**, not four fixed slots (the slots were retired
2026-07-14 — see `../docs/reminder-exact-time-scheduling-spec.md`). Each ⏰ row carries a **`Times`**
field — a comma-list of `HH:MM` owner-local times (e.g. `08:00` or `08:00, 20:00`). When `Times` is empty
the row falls back to its **`Time Window`** default (kept as coarse sugar):

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
reset**, and — for every ⏰ row due today — queues that row's exact-time nudges into `state/reminders.json`
via `reminders_seed.py` (primary time(s) + the re-fire ladder below). The daemon's ~5 s delivery tick
(`sentinel.check_reminders`) then fires each entry at its due minute, through the usual quiet / ack /
presence / catch-up-stagger gates. The seed decides *what* and *when*; the tick owns delivery — the same
enqueue/deliver split the four slots used, with compute collapsed from four fixed wakes to one rollover wake.

**Exact times are the earliest fire, not a guarantee of isolation.** The catch-up stagger (below) still
holds non-piercing nudges to ≥15 min apart, so several rows set close together drip out rather than
buzzing at once; piercing items (`Call Me` / `🚨`+ / `pierce_quiet` rows) fire exactly on time. Set the
minute you want, and know closely-spaced *non*-piercing nudges may be pushed later by that 15-min floor.

## States & transitions

`Status` ∈ `Pending`, `Reminded`, `Done`, `Skipped`, `Snoozed`, `Paused`. A **miss** is not a status — it's
`Consecutive Misses += 1` when a `Reminded`/`Pending` item rolls to the next cycle un-acknowledged.

Per reconcile pass:

1. **Daily reset** — *the daily **seed** pass, once per owner-local day (first tick after local
   midnight); **Daily** and **Weekdays** cadences only.*
   **Apply any pending acks first** (step 3 — a tick made since the last run credits **yesterday**); an
   acked row is not a miss. Then, for each such habit whose `Cadence` applies today:
   - If yesterday's `Status` was `Reminded` or `Pending` (never acked) → `Consecutive Misses += 1` **first**.
   - Then set `Status = Pending`, clear `Reminded Today` (leave `Last Acknowledged` alone — it's the
     durable done-record the Wrap reads).
   - **Never zero `Consecutive Misses` here** — only an actual `Done` resets it. This is what powers
     "0 for 5 days."
2. **Seed/fire** — for each `Pending` row due today the seed enqueues its exact-time nudge(s) via
   `reminders_seed.py` (which stamps each with the ⏰ row's ref, so a later ack can cancel any
   still-pending nudge for this row — step 3 / Delivery) and sets `Status = Reminded`, `Last Reminded =
   today`, `Reminded Today = true`. The ~5 s delivery tick then fires each at its due minute.
3. **Ack done** — three equivalent sources: the owner ticks `Ack` in Notion; they tell the assistant in
   chat (Chat writes it through immediately — the same fields below, not just the checkbox); or the linked
   Task/Goal is now `Done`. Apply — **`Status = Done` for *every* `Type`** (Recurring Habit, Today Todo,
   and Deadline Watch alike): `Done` means "done *for today*" — the item stays an **active** reminder and
   comes back on its next due cycle (a one-time item re-fires tomorrow while still within its window). Also
   set `Last Acknowledged = today`, `Consecutive Misses = 0`, then **untick `Ack`**. **`Finished` is a
   separate, explicit-only terminal value — never written on a plain ack.** The assistant writes `Finished`
   (retiring the reminder — it drops out of the owner's *active*-reminders filter and stops re-firing)
   *only* when the owner **explicitly** says they're finished / done-for-good with the item; "done" /
   "did it" / an `Ack` tick all mean `Done`, not `Finished`. (An owner-side Notion `Ack` automation should
   write `Done` on a tick for every `Type` too, so the assistant's direct field-write matches it.)
   **Then cancel the obsolete re-nudge.** The daemon fires queued nudges by `due_at` and can't read acks,
   so a still-pending, pre-staggered nudge for this same row would fire anyway (e.g. an item acked at
   08:15, but its staggered 08:45 nudge still buzzes). Immediately run
   `scripts/reminders_dequeue.py --reminder-id <this row's page id>` to drop every **un-fired**
   `state/reminders.json` entry for the row (fired history is left alone). For this to work the enqueue
   must have stamped the entry with `--reminder-id <page id>` (see the **Seed/fire** step and
   `../state/README.md`). **That same call also records the ack** to `state/acks.json` (the durable ack
   ledger), which is what stops a nudge the dequeue *can't* reach — a **soft-digest** covering this row —
   from buzzing: the fire path (`check_reminders`) suppresses any un-fired entry whose row (or every member
   of a digest) is acked today (see "Fire-time ack gate" below). Run the dequeue **once per acked row** so
   every ack — chat write-through, an `Ack` tick you reconcile, or a linked flip — lands on the ledger.
   This is **act-low** — removing a redundant nudge to the owner. The checkbox is a **one-shot input,
   consumed on read** — never the durable record (a tick left standing would auto-complete the next cycle).
   Durable proof-of-done for a day is `Last Acknowledged = that day` (+ `Status = Done` until the next
   reset); **the EOD Wrap counts "done today" from `Last Acknowledged`, never from `Ack`.** For a **Today
   Todo**, also *propose* flipping the `Related Task → Done` (**ask-high** — see below).
4. **Snooze / "later"** → `Status = Snoozed` and enqueue one fresh nudge **+30 min** out (an ad-hoc
   `reminders_enqueue.py` at `now+30m` carrying the row's `reminder_id`). Becomes a miss only if still
   `Snoozed` at the next daily reset.
5. **Skip / "not today"** → `Status = Skipped`, `Last Acknowledged = today`. An honest skip is **not** a
   miss and does **not** reset the streak (they answered; they just chose no).
6. **Paused** — manual off-switch; never fires, never accrues misses, until un-paused.

## Importance, the Nag-Until-Done flag, and re-firing (core behavior)

Two fields drive escalation, and they're **independent**:

- **`Importance`** (top → bottom): `🛑 Super-Critical` > `🚨 Critical` > `⭐ High` > `✨ Notable` > `📌 Low`.
  Drives nudge **ordering** and rib eligibility.
- **`Nag Until Done`** (checkbox): forces re-firing **regardless of importance** — for the things the
  owner wants chased even though they're low-stakes (e.g. water the plants).

**An item RE-FIRES until `Done` when** `Importance` ∈ (`🛑 Super-Critical`, `🚨 Critical`, `⭐ High`) **OR**
`Nag Until Done = true`. The seed queues it at its primary time **plus every 90 minutes** through the end
of the local day; the **fire-time ack gate** cancels the day's remaining re-fires the moment the owner
acks (one ack → the rest go silent), and it re-seeds the next day until confirmed. **Persistence is in
frequency, not sharpness:** the tone stays the same calm "still open" line, never escalating into
nagging-as-pressure.

**Everything else** — `✨ Notable` / `📌 Low` **with `Nag Until Done = false`** — **fires once per cycle,
then stops.** No same-day re-nagging; misses accumulate silently in `Consecutive Misses`, which feeds the rib.

### Phone-call escalation — the `Call Me` flag

For the rare **can't-miss** reminder (a flight, a court date), a silent Telegram line isn't enough.
`Call Me` (a checkbox on the Reminders row, **independent of `Importance`** — like `Nag Until Done`) opts
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

- **Daily** — every day. **Weekdays** — Mon–Fri only.
- **Every 3 days / Every 5 days** — due when `today ≥ Last Acknowledged + N` (empty `Last Acknowledged` ⇒
  due now).
- **Weekly** — if `Notes` names a weekday (e.g. "Fridays"), due that weekday; otherwise due when
  `today ≥ Last Acknowledged + 7` (empty ⇒ due now).
- **One-off / Deadline Watch** — due while not `Finished` and `Due / Target` is today, overdue, or within
  ~3 days. (A plain ack writes `Done` = *done-for-today* only, so the row re-fires the next day while
  still in-window — it goes silent for good only once the owner says they're `Finished`.)
- **Multiple/day** — fires at each `HH:MM` in its `Times` (or a **standing roll** for every-N-hours; see
  below).

The **daily reset** (in the seed pass) only re-`Pending`s **Daily** and **Weekdays** habits. Interval /
Weekly / One-off rows aren't reset daily — they flip to `Pending` when their date rule above says due.

### Days off — hold work Deadline-Watches to the digest

On a **day off**, a **work** Deadline-Watch does **not** fire as a push nudge — it appears **only in the
Brief / EOD-Wrap digest** as an FYI, and resumes normal push nudging on the next work day. (Graduated at
the owner's request — see `autonomy-policy.md`. Prior to this it was decided ad hoc each pass; the arc
played out cleanly across several holiday/weekend spans — e.g. work deliverable Deadline-Watches held to
the digest over a holiday weekend — before being codified.)

- **"Day off" signal:** the day is off if it's a **weekend** *or* a **calendar-marked PTO / holiday** *or*
  **the owner says so** in chat. (Weekend is the default; a calendar PTO/holiday block or an explicit "I'm
  off today" both count.) When unsure whether a given day is off, treat it as a work day (fire normally).
- **"Work" Deadline-Watch:** a `Deadline Watch` row whose linked record is a **💼 Work** Project/Task/Flag,
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

1. **Fetch the Interstitial Journal page** (`00000000-0000-0000-0000-000000000010`, baked in
   `databases.md`) and look for a **top-level date toggle** whose `<mention-date start="…"/>` equals
   **today** (the owner's configured timezone) with **≥ 1 timestamped entry** under it. That toggle is the
   owner's own writing; **ignore the assistant's carry-over `callout`** at the top of the page — the
   assistant edits that during the Brief, so it is *not* evidence the owner journaled.
2. **Journal activity today → auto-satisfy (act-low).** Treat it exactly like an ack: `Status = Done`,
   `Last Acknowledged = today`, `Consecutive Misses = 0`. **Suppress the nudge entirely** — this is the
   "automatically tick off that I've been using it" behavior; never nudge the owner to do a thing they've
   already done today. (No `Ack` checkbox to untick — the evidence is the journal page.)
3. **No journal activity today → nudge, but only past the grace window.** Nudge only once they've gone
   **`quiet_days` consecutive days** with no journal entry (default `1` ⇒ nudge this evening if nothing
   landed today). The nudge is one gentle evening line; it fires once per day (`✨ Notable`,
   `Nag Until Done = false` ⇒ no same-day re-nag).

Run the check in the **Wrap** run (nudge-or-satisfy) and again in **Dream** as a **satisfy-only
backstop** (auto-tick if they journaled after the Wrap check; no bedtime nudge). That's one page fetch in
each of those two runs — within the read budget (`notion-rate-limits.md`); **don't** fan it out further.

This generalizes: any habit whose "done" is provable from a durable source (a page edit, a linked record)
can carry a `[gate: …]` marker and be auto-satisfied the same way instead of nagged. (Graduated at the
owner's request — they asked the assistant to read the journal and "automatically tick off that I've been
using it.")

## The gentle rib (low-importance, non-nag only)

When a **`📌 Low` or `✨ Notable` Recurring Habit with `Nag Until Done = false`** reaches **`Consecutive
Misses >= 3`**, the assistant adds **one** dry, data-backed line to the next nudge. Rules:

- **Low-importance, non-nag only.** Super-Critical/Critical/High — and anything flagged `Nag Until Done` —
  are never ribbed; they're re-fired instead.
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
| Read reminders, compute due/misses, run the daily reset, set Reminders-DB `Status`/counters/`Reminded Today` | **act-low** — a tracker the assistant owns (same class as journal trackers); reversible, inbound, bounded, audited |
| Enqueue the nudge to `state/reminders.json` for the daemon to deliver over Telegram (+ Notion) | **act-low** — pushing the owner their own content = inbound (the brief push already graduated) |
| Add a rib line | **act-low** — content in a push to the owner, bounded by the threshold above |
| Set a Reminders row `Status = Done` from an ack (any `Type` — done-for-today) | **act-low** |
| Set a Reminders row `Status = Finished` (retire it) on the owner's **explicit** "I'm finished with X" | **act-low** |
| Open/lift a **quiet window** (`quiet_set.py`) on the owner's request — suppress their own non-piercing nudges | **act-low** — reversible, bounded, self-directed (Call Me + Critical+ still pierce) |
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

**One reminder per nudge — the bundling pattern is retired (the owner's call).** A single nudge listing
several things either overwhelms (no clear one thing to start on) or gets half-finished (one or two get
done and the rest falls off). So a **queued nudge always carries exactly one reminder** — there is no
combined/digest nudge anymore, and there are **no "may combine" exceptions** (no bundling of a status
list, no bundling of "tightly coupled steps"; if two steps truly belong together, that's one reminder row,
not two rows merged at fire time). When more than one item lands together, enqueue them as **separate**
entries, each with its own stable `--id` so re-runs stay idempotent. Under exact times the seed queues
each at the row's own minute; where several genuinely share a minute, the delivery **catch-up stagger**
spreads them ≥15 min apart (oldest-due first) so they drip rather than wall — the seed need not
hand-stagger `due_at`. This clustering is heaviest first thing in the morning, where Today-Todos,
Deadline-Watch, and habits all land together.

This governs the **push** path (queued nudges the assistant initiates). It does **not** restrict a **pull**
answer: when the owner themselves asks "what's still open?", replying with a ranked list in chat is
answering their question, not enqueuing a nudge — that stays allowed (see the pull-vs-push note at the top
of this file).

**A status digest carries its members' ids.** When you *do* combine (a status digest that isn't a
do-this-now list), enqueue it with a `--member-reminder-id <page id>` for **each** row it mentions
(`reminders_enqueue.py`, repeatable). That's what lets the fire-time ack gate (below) drop the whole
digest once the owner has acked *everything* in it — without those member ids, a digest can only ever fire
blind, which was a real production bug (a soft-digest re-nudged five already-acked items). One-per-nudge
do-this-now items don't need this — their single `--reminder-id` already gates them.

## Fire-time ack gate (durable, not session-memory)

The daemon delivers queued nudges by `due_at` and **can't read Notion** (its only Notion path is the
hosted OAuth MCP, reachable from `claude`, not from the stdlib daemon). So a nudge staggered *before* an
ack — or a soft-digest baked at 08:00 covering it — used to buzz for something already done, because the
warm session's knowledge of the ack lived only in volatile context, never where the fire path could see
it. The fix: every ack records the ⏰ row's key + **today's local date** to `state/acks.json` (the
`reminders_dequeue.py` call the ack step already makes does this), and `check_reminders` consults that
ledger at fire time:

- A due entry whose `reminder_id` is acked **today** is **dropped** — stamped `acked_at` (consumed, never
  re-delivered), exactly like a quiet-suppressed one. A **digest** (`member_reminder_ids`) drops only once
  **every** member is acked today.
- Only *today's* acks gate (yesterday's don't), so tomorrow's re-fire after the daily reset is unaffected.
- **Multi-fire rolls opt out** (`ack_gate: false`): one "checked messages" ack must not cancel the rest of
  the day's pings. A roll is hushed for the day via quiet or an explicit dequeue, not a single ack.
- **Fail-open.** A missing or malformed ledger reads empty, so the gate can only ever *suppress a genuine
  ack* — it can never silence a real nudge. The ledger is durable local state (survives the session
  winding down / a reboot), which is the whole point: acks gate delivery from disk, not from memory.

## Quiet window (do-not-disturb)

When the owner asks to hush the nudges for a while ("quiet till morning", "no nudges tonight"), that
request must be **durable and honored no matter what rebuilds the queue** — the old failure was a chat
"quiet tonight" that the next reconcile silently undid by re-deriving the same nudges from the open ⏰ rows (and
a standing roll re-seeding itself), so buzzes kept landing all night.

- **One state file, one gate.** Chat mode writes `state/quiet.json` (`scripts/quiet_set.py`); the presence
  daemon checks it at the **single delivery chokepoint** (`sentinel.check_reminders`). Slots and rolls go
  on enqueuing whatever they compute — nothing *fires* while the window is open. Gating at delivery (not
  enqueue) is what makes it robust against a rebuilt queue.
- **Drop, not defer.** A suppressed nudge is **consumed** (`suppressed_at` stamped), never delivered when
  the window lifts — so waking up doesn't trigger an avalanche of everything slept through.
- **What still pierces:** `Call Me` items (the phone ring — channel `call` / an escalating call) **and**
  `🚨 Critical` / `🛑 Super-Critical` items. `⭐ High` and below are dropped for the duration. The daemon is
  Notion-dumb: it reads a per-entry `pierce_quiet` flag, which the **brain sets at enqueue** (`--pierce-quiet`)
  for Critical-and-above, where it can see the ⏰ row's `Importance`. A call entry pierces on its own.
- **Setting it is act-low** (the assistant suppressing its own pushes to the owner; reversible, bounded).
  `quiet_set.py --until-morning` = next 08:00 local (the morning anchor); `--minutes N` / `--until-local
  HH:MM` for other spans; `--clear` lifts it ("you can nudge me again"). Chat mode rule 7
  (`seneschal/SKILL.md`) wires it up.

## Live-session defer (don't buzz into a live conversation)

While a human is **actively engaged in an interactive `/assistant` chat** — the daemon's warm
Telegram/Discord session or a desktop slash session — a due non-piercing nudge shouldn't buzz into the
middle of the conversation. The **session registry** (`state/sessions/`, one small JSON entry per live
session) is how the daemon knows:

- **Defer INTO the session, never drop.** At the same delivery chokepoint (`sentinel.check_reminders`),
  a non-piercing due nudge is **held** (stamped nothing → re-checked each ~5 s tick) while
  `sentinel.session_is_live` is true, and fires naturally once the session ages out of its 120 s TTL.
  The redundant Watch comms-peek is skipped too (without consuming its cadence).
- **Only interactive sources gate.** `daemon` (the warm chat, self-registered every turn) and `desktop`
  (a slash session, self-registered via `scripts/session_heartbeat.py`) defer delivery. `build` /
  `scheduled` entries — every other Claude Code session, stamped by the machine-wide
  `scripts/session_stamp.py` hook — are **awareness-only** ("who's live, on what branch"): the owner is
  coding, not conversing, so reminders still buzz normally.
- **The pierce set is the same as quiet's.** `Call Me` + Critical-and-above fire immediately regardless;
  the gates compose (quiet's drop wins first, piercing pierces both).
- **Fail-open.** A missing / malformed / stale registry reads *not live* — the absence of the signal can
  never block a genuine reminder.

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
  never drop), so a released backlog drips out oldest-due first at ~15-min spacing instead of walling up.
- **Piercing items skip the gate entirely.** `Call Me` / `🚨 Critical` (`pierce_quiet`) fire the
  moment they're due — a released backlog never delays a can't-slide item, and piercing fires neither
  consume nor reset the drip clock (separate lane).
- **Normal days are mostly untouched.** The seed queues each row at its own exact minute, so nudges set
  ≥15 min apart clear the gate trivially; this reshapes a *bunched release* and also auto-spreads any rows
  that happen to share a minute (they drip 15 min apart, oldest-due first). Fail-open: a missing/broken
  `nudge-stagger.json` fires immediately. Mechanics live in `sentinel._check_reminders_locked`.

## Standing rolls — custom every-N-hours cadences

Neither a `Times` list nor the seed cleanly expresses *"every 2 hours, 9am–11pm"* — an intraday cadence
some reminders want (the canonical case: a check-messages poll on a service whose notifications are
unreliable). Rather than have the assistant hand-enqueue that roll each day, it's a **standing roll**: a
config entry in `scripts/reminders_roll.py` (`ROLLS`, keyed to the ⏰ row's `reminder_id`) that the
presence daemon **regenerates once per local day** on its own — pure local queue math, no store read and
no `claude` spawn.

- **Future-only, idempotent, rolling.** Each refill seeds today's still-upcoming slots **and** all of
  tomorrow's (so a late-waking machine is still covered), skipping any slot whose instant has passed and
  any id already queued (stable ids `rmd-<date>-<prefix>-<HH>`). It never back-fires a past slot and
  never double-queues. `state/rolls.json` holds the once-per-day guard; `Dream` prunes fired entries.
- **Acks still cancel — but don't ack-gate.** Every roll nudge carries the row's `reminder_id`, so a chat
  ack's `reminders_dequeue.py --reminder-id <id>` drops the remaining un-fired nudges for the day exactly
  like any other reminder (the row re-seeds tomorrow). But a roll is **multi-fire**, so its entries set
  `ack_gate: false` — the fire-time ack gate ignores them, so acking one ping doesn't silently suppress
  the rest of the day's (a dequeue is the explicit "stop for today"; a single ack isn't).
- **Act-low.** A roll is the assistant pushing its own recurring content to the owner on a fixed cadence —
  same class as any enqueue. Adding/adjusting a roll (interval, window) is a config edit; the cadence
  itself is set with the owner. To add one: a `ROLLS` dict + the ⏰ row (`Cadence = Multiple/day`,
  `Anytime`).

## Resolving relative dates ("tomorrow", "in the morning")

When the owner uses a relative day-word — *"tomorrow," "in the morning," "first thing"* — resolve it
against the **wake-day**, not a naïve calendar+1:

- **Post-midnight sessions** (~00:00–05:00 local, before they've slept): a relative day-word binds to the
  **upcoming wake-day = the current local calendar date**, *not* date+1. If they say "remind me tomorrow
  morning" at 02:30, that's **today's** date (the day they're about to wake into), the same way the
  timezone rule already counts after-midnight activity as the prior day.
- **Normal daytime sessions:** "tomorrow" means the next calendar day, as expected.
- **Explicit anchors always win.** A named weekday or date ("Wednesday," "the 1st") is honored exactly as
  stated and is *not* reinterpreted — that's why a "Wednesday morning" bill stays on Wednesday even in a
  post-midnight chat.

This governs how `reminders_enqueue` computes `due_at` and how the Reminders mode sets `Due / Target`.
(Graduated via approved Dream learning — see `autonomy-policy.md`.)

## Acknowledgment channel (v1 → v2)

- **v1 (now):** the owner acks by the one-tap `ack` affordance in the store **or** telling the assistant
  in a chat session (terminal `/assistant` or Telegram). The seed + each reconcile run reads that state
  first, so any ack before the next run is honored. The nudge ends with *"tick Ack or tell me."* **A chat ack is only
  honored if the Chat session writes it through to the store** — the Reminders row is the durable record;
  an ack that stays in the (volatile, reboot-clearing) warm session is lost and the next reconcile re-fires it.
  Telegram Chat runs headless, so the presence daemon must be wired with the store's read/write MCP for
  this to land — resolved store-config-driven (`store/config.json`; a legacy auto-detect of
  `scripts/notion-mcp.json` on the Notion backend), see `scripts/NOTION_MCP_SETUP.md`. Filesystem backends
  need no MCP. (Chat write-through is `seneschal/SKILL.md` Chat mode rule 5.)
  **The one-tap `ack` is consumed, not kept:** the next reconciling run applies it (`status: done`,
  `last_acknowledged: today`, misses zeroed) and **resets `ack`**. So "were things finished today?" is
  answered by `last_acknowledged: today` + `status`, never by scanning for set flags — seeing all `ack`
  flags cleared in the evening is the system working, not evidence nothing got done. Chat write-through
  likewise sets the fields directly and leaves `ack` alone (it's the owner's affordance, not the agents').
- **A 👍 on a nudge is an ack (2026-07-16).** Reacting 👍 to a Telegram nudge runs the **same** path a
  typed ack does — `reminders_dequeue.py` (cancel the obsolete re-nudges + record the durable local ack)
  and, **on the notion backend only** (the write-behind outbox is Notion-specific —
  `store/notion/mapping.md`, Outbox section), an `outbox.py ack` journaling the `status: done` /
  `last_acknowledged: today` write for the next LLM turn to flush; filesystem backends leave the store row
  to the warm session's `store-update`. No typing, no session needed: the daemon does it directly
  (act-low, the owner's own content), and tells the warm session it's already done so it doesn't re-ack.
  **Narrow on purpose — it only fires for a 👍 (or whatever the owner maps to `ack` in
  `state/telegram-reactions.json`) on a *tracked nudge* that went out *today, local*.** A reminder row
  resets daily and an ack stamps today's date, so honoring a 👍 on yesterday's nudge would mark **today**
  Done — for meds, exactly the wrong outcome. Anything outside that (a 👍 on a chat reply, an older
  nudge, an untracked message) is handed to the warm session as context instead, and it decides.
  Mechanics: `../docs/telegram-inbound-spec.md` §3.4c; the emoji vocabulary is the owner's to edit.
- **v2 (later, designed-for):** the warm Telegram session already parses a reply like `done` / `skip` /
  `later <which>` and writes it back to the **same** Reminders fields (v1 already does the write-through
  above); v2 layers on richer reply grammar and ack-gated `Call Me` escalation — no schema or
  state-machine change.

## Guardrails

- **Signal over noise.** A nudge is short — the few things that actually need the owner, never a
  wall. Prefer three real items to ten.
- **One reminder per nudge; stagger 15–30 min apart.** Never enqueue more than one item in a single nudge
  (see Delivery — bundling is retired, no exceptions) — it overwhelms or gets half-done. A ranked list is
  only ever a **pull** reply to the owner asking "what's open?", never a queued push.
- **Reversible writes only** without asking; never touch a linked Task/Goal status unprompted.
- **One rib max per reminder per day**, low-importance non-nag only, factual.
- Times and date logic in **the owner's configured timezone**; after-midnight counts as the prior day.
- Every substantive seed/reconcile run leaves a trace (Run Log `Mode = Reminders` + carry-over).
