# Exact-time reminders — retire the 4-slot model — design

**Status:** approved direction — the owner signed off on the shape (2026-07-14); since **implemented**
(`presence.maybe_seed_day` + `reminders_seed.py`; reminders-policy.md is the living model) · **Owner:** the assistant · **Scope:**
`seneschal/references/reminders-policy.md`, `seneschal/references/databases.md` (⏰ schema), the Reminders
subagent (`subagents/reminders/SKILL.md`), `seneschal/scripts/presence.py` (`SLOTS` + a new seed trigger),
and one new brain step folded into Wrap/Dream. Companion: [asyncio-daemon-design.md](asyncio-daemon-design.md),
[reminders-policy.md](../references/reminders-policy.md).

---

## TL;DR — the reframing that makes this small

**The daemon already fires at any exact minute.** Since the asyncio port, `check_reminders` runs on a
~5 s scheduler tick and delivers every queued entry the moment `due_at <= now` — quiet / ack / presence /
stagger gates and all. There is no 4-slot granularity in *delivery*; a nudge with `due_at =
2026-07-14T13:17:00Z` fires at 13:17, full stop.

What is stuck at four times a day is **compute**: the brain (`claude -p` running the Reminders subagent)
only wakes at **08:00 / 12:30 / 18:30 / 21:30** to read the ⏰ tracker, run the state machine, and *enqueue*
nudges. And the ⏰ schema only lets a habit pick one of **five coarse buckets** (`Time Window` ∈
Morning / Midday / Evening / Bedtime / Anytime), which map to those four wakes. So "remind me at 09:15"
can't be said as a habit today — only as a one-off ad-hoc enqueue.

**This change is therefore ~90 % schema + brain-scheduling, ~10 % daemon.** We (1) give each ⏰ row an
explicit **time-of-day** (and a small list for multiple/day), (2) replace the four reminder *compute*
slots with **one once-per-day "seed the day" pass** that reads every row and enqueues its exact-time
nudges for the whole day (re-fires included), and (3) let the already-exact-time delivery queue do the
rest. We do **not** build the deferred sleep-until-next-due timer wheel — the 5 s tick already delivers
exact-minute precision (asyncio-daemon-design.md → "Explicitly deferred").

---

## Why

- **The 4 slots are an artifact of the original Claude-scheduled-actions design**, where the only way to
  "wake up" was a cron entry, so reminders were bucketed to a handful of crons. The presence daemon
  removed that constraint two rewrites ago; the schema never caught up.
- **Real times are what the owner actually wants.** "Meds at 8, standup nudge at 09:25, walk at 16:00,
  teeth at 22:30" is the natural expression. Bucketing them into four windows forces every morning thing
  to pile onto 08:00 (then the stagger rule frantically spreads them back out 15–30 min apart — solving a
  problem the bucketing created).
- **Fewer brain spawns, not more.** Counter-intuitively, retiring the four `reminders-*` `claude -p`
  slots in favor of **one** daily seed pass *reduces* scheduled Opus/Sonnet spawns from 4 → 1. The
  per-minute firing stays LLM-free in the daemon, exactly as now.
- **Catch-up gets simpler and safer.** Seeding the whole day ahead means a reminder whose minute passed
  while the machine slept is already sitting in `reminders.json` with a past `due_at`, so it fires on wake
  through the existing queue + catch-up-stagger path — no per-slot `classify_slots` "too late, skip"
  cliff that could silently drop a window.

## Non-goals

- **No timer wheel / sleep-until-next-due.** Stays deferred (asyncio-daemon-design.md). The 5 s tick is
  the firing clock; exact-time here means *per-reminder configurable*, not *sub-second*.
- **No change to the delivery gates.** Quiet window (drop-not-defer + pierce set), fire-time ack gate,
  presence defer (NO DROPS), catch-up stagger, channel routing/fallback, `Call Me` escalation — all
  unchanged. They already operate on `due_at`, not on slots.
- **No change to the act-low / ask-high gate.** Seeding, firing, reset, and tracker writes stay act-low;
  flipping a linked Task/Goal stays ask-high.
- **No removal of standing rolls.** The every-N-hours roll (`reminders_roll.py` — e.g. a standing
  check-messages ping) is already an
  LLM-free whole-day seed keyed to a row; it stays as-is and becomes the *prototype* the general seed
  pass follows.
- **No flag-day.** Additive schema + a dual-run window (below); nothing regresses at merge.

---

## What the four slots actually do today (the entangled part)

Retiring the slots means every duty currently *bound* to a slot needs an explicit new home. Enumerated
so nothing is lost:

| Duty (today) | Bound to | Notes |
|---|---|---|
| **Daily reset** (re-`Pending` Daily/Weekdays habits, `Consecutive Misses += 1` for yesterday's unacked, clear `Reminded Today`, apply pending acks first) | **Morning slot only**, once per owner-local day | The single most load-bearing slot duty. |
| **Enqueue a window's habits** (a `Pending` row whose `Time Window` matches this slot) | each slot | This is the "firing" the schema change is really about. |
| **Re-fire unacked important** (`Importance ≥ ⭐ High` **or** `Nag Until Done`) until `Done` | **every** slot | The frequency-not-sharpness persistence. |
| **Re-surface Snoozed** ("later" → re-fire next slot today) | next slot | |
| **Journal-presence gate** (fetch journal page → auto-satisfy or nudge) | Evening (nudge-or-satisfy) + Bedtime (satisfy-only) | Needs a *late-day* Notion read — can't be predicted at dawn. |
| **Linked-task truth refresh** (a Deadline-Watch whose linked Task went `Done` midday should stop) | implicit each slot re-read | Partly covered by the ack gate today. |
| **Rib** (`📌`/`✨` non-nag, `misses ≥ 3`, one dry line) | fire time | Pure function of `Consecutive Misses`; independent of *when*. |
| **Tracker + Run Log writes** | each slot | Act-low. |

Everything below re-homes each of these.

---

## The new model

### 1. Schema — an explicit time, with `Time Window` kept as optional sugar

Add one property to the ⏰ Reminders DB and **keep** `Time Window`:

- **`Times`** *(text)* — a comma-separated list of `HH:MM` **owner-local** times, e.g.
  `08:00` or `08:00, 20:00`. Empty ⇒ fall back to the `Time Window` default (below). This is a plain text
  field (not a select/multi-select) so a small set of arbitrary minutes is expressible without pre-seeding
  option values; the seed pass parses/validates it. One-off / Deadline-Watch rows may leave it empty and
  are timed off `Due / Target` + a default hour (open question Q3).
- **`Time Window`** *(select — unchanged values)* — **kept as coarse sugar.** When `Times` is empty, the
  window maps to a concrete default time. This is what makes migration zero-regression and preserves the
  "file a habit in the window the owner acts on it" ergonomic (PM hygiene → Bedtime, etc.).

**Recommendation:** keep the presets rather than fully replacing them (Q1). They cost one fallback lookup,
carry the entire existing corpus through untouched, and remain a nice shorthand ("just make it an evening
thing") when an exact minute doesn't matter.

Default map (also the migration map — §Migration):

| `Time Window` | Default time (owner-local) | = today's slot |
|---|---|---|
| Morning | **08:00** | reminders-morning |
| Midday | **12:30** | reminders-midday |
| Evening | **18:30** | reminders-evening |
| Bedtime | **21:30** | reminders-bedtime |
| Anytime | **09:00** | (first Pending slot today) |

`Anytime`'s old semantics ("fire in whichever slot it's still Pending") has no single-time equivalent;
09:00 gives an important Anytime item the whole day to re-fire (decided — §Decisions Q3).

### 2. Compute — one "seed the day" pass replaces the four reminder slots

A single daily brain pass (the Reminders subagent in a new **Seed** sub-mode) does, once per owner-local
day:

1. **Apply pending acks**, then **run the daily reset** (verbatim the current Morning-slot reset —
   `Daily`/`Weekdays` re-`Pending`, misses for yesterday's unacked, clear `Reminded Today`). This is the
   daily reset's new home (§3).
2. **Read every fire-candidate ⏰ row** (same query as today's Phase 1) and, for each, compute its
   **due instants for the whole local day**:
   - `Times` present ⇒ one instant per listed `HH:MM`.
   - `Times` empty ⇒ the `Time Window` default time.
   - `Deadline Watch` / `One-off` ⇒ default hour on the `Due / Target` day while in-window.
   - **Re-fire items** (`Importance ≥ ⭐ High` **or** `Nag Until Done`) ⇒ the primary instant **plus** a
     **re-fire schedule** through the rest of the day (a small set of later instants — Q4).
3. **Enqueue each instant** as a `reminders.json` entry with the exact `due_at` (UTC), the row's
   `reminder_id`, and (for Critical-and-above) `--pierce-quiet`, using stable idempotent ids
   `rmd-<local-date>-<rowslug>-<HH><MM>`. Future-only and idempotent — **exactly the `reminders_roll.py`
   contract, generalized to all rows.**
4. **Write the tracker** (`Status = Reminded`, `Last Reminded = today`, `Reminded Today = true` for what
   it seeded — set early is fine, the durable done-record is `Last Acknowledged`) + the **Run Log** entry.

Then the daemon's existing per-minute `check_reminders` fires each seeded entry at its `due_at`, applying
every gate exactly as now.

**Why one pass, not per-reminder brain wakes:** the brain's job is to *decide + enqueue*; the daemon's job
is to *deliver by due_at*. Spawning a `claude -p` at each reminder's minute would re-introduce the cost the
enqueue/deliver split exists to avoid. The rolls already prove a whole-day LLM-free seed works.

### 3. The daily reset's new home — a once-per-local-day seed trigger (not a fixed HH:MM)

Retiring the Morning slot orphans the reset. **Decided (2026-07-14):** trigger the seed pass on the
**first scheduler tick of each new owner-local date**, guarded by a local-date marker — **not** a
fixed-time `SLOTS` entry. *(As built, the marker rides the existing `slots.json` under the
`reminders-seed` name via the slot reap lifecycle, rather than a separate `reminders-seed.json` — same
semantics, one less state file.)*

Rationale — this is strictly safer than a fixed time:

- A fixed `00:05` (or `05:00`) slot is subject to `classify_slots`' `--slot-catchup-min` (180 min) skip:
  machine asleep 00:00–09:00 ⇒ the seed is >3 h late ⇒ **skipped for the day** ⇒ no reset, no nudges. A
  catastrophic silent regression.
- A date-rollover trigger fires the seed on the **first tick after midnight *or* first tick after wake**,
  whichever comes first — the reset can never be skipped. It runs at ~00:05 on an always-on box (the
  normal case) and "first thing on wake" otherwise.

Mechanically it's a sibling of `maybe_refill_rolls`: the scheduler tick calls `maybe_seed_day(state_dir)`
which no-ops if the marker == `local_today()`, else spawns the seed `claude -p` (and, on
its clean exit, stamps the marker — reusing the `slot_children` reap-on-exit-0 discipline so a crashed
seed retries rather than silently vanishing).

### 4. Re-fire — a seeded, ack-gated multi-entry (no new mechanism)

"Re-fire unacked important until Done, at every slot" becomes: the seed pass enqueues the item at its
primary time **plus a re-fire every 90 minutes** from that time through the end of the local day (so an
08:00 Critical re-nudges 09:30, 11:00, 12:30, … until acked), all carrying the row's `reminder_id` and
**`ack_gate` left ON (default `true`)**. Then:

- The **fire-time ack gate** (`acks.json`) drops every remaining seeded re-fire the instant the row is
  acked today — this is *already built* and durable. One ack ⇒ the rest go silent. Exactly the behavior we
  want, for free.
- A chat ack already runs `reminders_dequeue.py --reminder-id <id>`, which **both** removes the un-fired
  re-fires from the queue **and** records the ack — so acked re-fires vanish immediately, and any the
  dequeue can't reach are caught at fire time.
- Tomorrow's seed re-evaluates and re-seeds (with `Consecutive Misses += 1` if it went unacked all day).

Note the deliberate contrast with **rolls**, which set `ack_gate: false` (one "checked messages" must
*not* cancel the day's other pings). Re-fires are the opposite — one ack *should* cancel the rest — so they keep
`ack_gate: true`. The two cases are already distinguished by exactly this flag.

### 5. Snooze — an ad-hoc enqueue at now + interval

"Later" → `Status = Snoozed` and **enqueue one fresh entry** at `now + snooze_interval` (**+30 min**)
with the row's `reminder_id`. No slot needed — it's a single exact-time enqueue, which the queue
already supports. Becomes a miss only if still `Snoozed` at the next daily reset (unchanged).

### 6. The late-day residue — journal-presence + linked-task truth (folded into existing wakes)

Two duties genuinely need a *late-day store read* and so can't be fully pre-seeded at dawn:

- **Journal-presence gate** — "did the owner journal today?" is only answerable in the evening.
- **Linked-task truth refresh** — a Deadline-Watch whose linked Task was completed midday.

**Recommendation:** fold both into the **already-existing** brain wakes rather than adding a slot:

- **Wrap (21:07)** gains a small step: for each `[gate: journal-presence]` habit, fetch the journal page
  → auto-satisfy (`Status = Done`, `Last Acknowledged = today`, misses 0) **or**, past the grace window,
  enqueue the one evening nudge. Same one page-fetch as today's Evening check. Also refresh linked-Task
  status for active Deadline-Watches and record an ack (via the ledger) for any now-`Done`, so their
  seeded re-fires stop.
- **Dream (22:00)** gains the **satisfy-only backstop** (auto-tick if the owner journaled after the Wrap
  check; never a bedtime nudge) — one page-fetch, no new spawn.

This dissolves the journal gate's Evening+Bedtime binding into wakes that already run and already read
the store at those hours. Net new recurring brain spawns from this whole change: **+1 seed, −4 reminder
slots = −3.**

---

## What must still hold under exact-time firing

Each survives because it already lives at the **delivery** layer (`check_reminders`), which this change
does not touch — it only changes what `due_at`s land in the queue and who puts them there.

| Guarantee | How it survives |
|---|---|
| **Catch-up stagger** (drip, don't wall) | Unchanged. `nudge-stagger.json` + `CATCHUP_STAGGER_SEC` still gate non-piercing fires to ≤1/pass, ≥15 min apart, oldest-due first. **Improved:** seeding-ahead means a slept-through backlog is real queue entries with past `due_at`, which is exactly the case the stagger was built for. Fresh same-minute seeds (e.g. two 08:00 items) *rely* on the stagger to space them: the seed queues each at its exact minute and the delivery stagger drips same-minute collisions ≥15 min apart, oldest-due first — so no enqueue-time hand-staggering is needed. |
| **Presence-gate defer** (asleep/driving/away — NO DROPS) | Unchanged. `require_place` / driving defer operate on any queued entry by `due_at`; seeded entries defer and re-fire on the edge, fail-open, never dropped. |
| **Quiet window** (drop-not-defer + pierce set) | Unchanged. A quiet window set at any hour still drops non-piercing seeded entries whose minute falls inside it (`suppressed_at`), and `Call Me` / Critical-and-above (`pierce_quiet`, set by the seed for those rows) still pierce. |
| **Standing rolls** (every-N-hours) | Unchanged and *kept* — the seed pass and `refill_rolls` both write the same queue with idempotent ids; they don't collide (different id prefixes). Rolls stay `ack_gate:false`; general seeds default `ack_gate:true`. |
| **Fire-time ack gate** | Unchanged, and **now more load-bearing** — it's the mechanism that stops seeded re-fires once acked. Only *today's* acks gate; the next day's re-seed is unaffected. |
| **Reset-once-per-day** | Preserved by the date-rollover seed trigger (§3) — moved off "Morning slot," never skipped. |

---

## Where each slot duty moves (summary)

| Duty | New home |
|---|---|
| Daily reset | Once-per-local-day **seed pass** (date-rollover trigger) |
| Enqueue a window's habits | Seed pass, at each row's explicit `Times` / window-default |
| Re-fire unacked important | Seed pass enqueues a per-day re-fire schedule; **ack gate** stops it on ack |
| Re-surface Snoozed | Ad-hoc enqueue at `now + snooze_interval` from the chat/reconcile path |
| Journal-presence gate | **Wrap** (nudge-or-satisfy) + **Dream** (satisfy-only backstop) |
| Linked-task truth refresh | **Wrap** (refresh + ledger-ack the now-`Done`) |
| Rib | Fire-time / seed-time text, unchanged (pure fn of misses) |
| Tracker + Run Log writes | Seed pass (+ Wrap for the residue) |

---

## Migration — zero-regression, no manual re-entry

1. **Additive schema.** Add `Times` (text) to the ⏰ tracker; keep `Time Window`. Existing rows keep working
   immediately because the seed pass falls back to `Time Window` → default time when `Times` is empty. So
   **the seed pass is correct on day one with `Times` entirely blank** — migration is optional polish, not
   a prerequisite.
2. **Optional backfill (one-time, act-low).** A small pass sets each existing row's `Times` from its
   `Time Window` default (the §1 table), so the corpus reads explicitly rather than via fallback. Rows
   whose ideal time differs from the bucket default (e.g. a 09:25 standup that was filed as "Morning")
   get hand-tuned by the owner afterward — that's the *point* of the feature. This is a data edit, reversible,
   surfaced for review, not a schema risk.
3. **Reference/doc sync (same change).** `reminders-policy.md` (retire "The four daily slots" section →
   "The daily seed + exact times"; move the reset description; update Cadence/`Multiple/day`),
   `databases.md` (add `Times`, annotate `Time Window` as sugar), `subagents/reminders/SKILL.md` (Seed
   sub-mode; Wrap/Dream residue steps), `SCHEDULING.md` (`SLOTS` table: −4 reminder slots, +seed trigger),
   `state/README.md` (`reminders-seed.json`; note the seed alongside rolls). Docs-stay-in-sync is a hard
   rule; the PR that changes behavior fixes the docs in the same commit series.

Nothing regresses: an un-backfilled row fires at its old window-default minute; a backfilled row fires at
the same minute, now stated explicitly; only a hand-tuned row moves — and only because the owner moved it.

---

## Rollout + testing (merge-on-green, no flag-day)

**Phased, each phase merges only on green CI, deploys via Path A's graceful reload:**

- **Phase 1 — plumbing, dark.** Add `Times` to the schema doc + the seed computation (row → list of
  `due_at`) as a pure, unit-tested function. Add `maybe_seed_day` + the `reminders-seed.json` guard to the
  scheduler, **behind `--seed-day` (default off)**. The four `reminders-*` slots still run. No behavior
  change. Ships dark like the Discord gateway did.
- **Phase 2 — dual-run proof.** Enable the seed pass **alongside** the four slots on the host. Because the
  seed and the slots both enqueue with **stable idempotent ids** and both are future-only, a row already
  seeded for the day is skipped by the slot (duplicate-id no-op) and vice-versa — so dual-run cannot
  double-fire. Watch a few days: the seed should be enqueuing everything the slots would, at the same
  minutes. Verify on-host (`reminders.json`, `presence.log`).
- **Phase 3 — retire the slots.** Remove the four `reminders-*` entries from `SLOTS`; move the
  journal-presence + linked-task steps into Wrap/Dream; flip `--seed-day` on by default. The seed pass is
  now the sole reminder-compute path. Update all docs in this same PR.
- **Rollback** = revert the merge; the next `seneschald-update` cycle rolls the daemon back automatically.

**Daemon-reload safety (mid-flight merge).** The seed is **idempotent + once-per-day-guarded**, and every
`reminders.json` writer takes the cross-process `queue_lock`. So a reload that lands mid-seed: the marker
is stamped only on the seed's clean exit; a half-done seed re-runs after reload and idempotent ids prevent
double-queueing; the lock prevents a torn write. No dropped or double-fired reminder across a reload — the
same invariant the roll refill already satisfies.

**`test_*.py` coverage to add** (stdlib unittest, CI-green before merge):

- `seed_due_instants(row, now_local)` → correct `due_at` list for: single `Times`, multi `Times`,
  empty `Times` + each `Time Window` default, Deadline-Watch/One-off off `Due / Target`, DST-boundary day
  (deterministic off a fixed offset, like `roll_entries`).
- **Reset-in-seed:** `Daily`/`Weekdays` re-`Pending`; miss increment for yesterday's unacked; acked-row
  not-a-miss; interval/Weekly/One-off untouched by the reset.
- **Re-fire seeding + ack-gate suppression:** an important row seeds N entries; after `record_ack`, all
  remaining are gated (`entry_acked` true); a `Nag Until Done` low-importance row seeds re-fires too.
- **Idempotency / dual-run:** re-running the seed adds nothing; seed + slot for the same row don't
  double-queue; `maybe_seed_day` no-ops when the marker == today.
- **Catch-up:** a seeded entry with a past `due_at` fires on the next tick; the stagger still drips a
  bunched same-minute batch.
- **Migration map:** `Time Window` → default-time table is exactly the numbers in §1 (guards accidental
  drift between doc and code).
- **Trigger safety:** date-rollover fires the seed after a simulated long sleep (no `slot_catchup_min`
  skip); a fixed-time regression test would *fail* — encode why we chose rollover.

---

## Decisions (owner sign-off — 2026-07-14)

All seven open questions are ruled; this is the design's decision record. Implementation follows these.

| # | Question | Ruling |
|---|----------|--------|
| Q1 | Keep `Time Window` or replace it? | **Keep** as optional sugar — an empty `Times` falls back to the window's default time (zero-regression migration). |
| Q2 | Default times per window | **08:00 / 12:30 / 18:30 / 21:30** (today's slot times) — the fallback + migration anchors. |
| Q3 | `Anytime` + empty-`Times` deadline hour | **09:00** for both. |
| Q4 | Re-fire cadence for unacked important | **Every 90 minutes** from the primary time through end of local day, ack cancels the remainder (an 08:00 Critical re-nudges 09:30, 11:00, 12:30, … until acked). |
| Q5 | Snooze default interval | **+30 minutes.** |
| Q6 | Seed trigger time | **Date-rollover** — the first daemon tick of each new owner-local date (not a fixed HH:MM), so the reset can never hit the catch-up skip cliff. |
| Q7 | Per-time override richness | **Start simple** — a comma-list of `HH:MM` in one `Times` text field; add structured per-time options only if a real reminder needs it. |

---

*The owner signed off on this shape 2026-07-14 (decisions above); it has since shipped through the
phased, merge-on-green rollout.*
