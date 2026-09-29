# Tomorrow's Lead — a first-class "prioritize tomorrow" marker

**Status:** `PARTIAL(§6 decided, all six option A; phases 0-2 BUILT in tomorrow_marker.py and the
chat/brief/wrap/dream mode files, with the Dream prune wired; the daemon's morning-slot injection of the
brief line and the Wrap/lead picker callback dispatch BUILT in presence.py)` — the store, the CLI
(`mark` / `close` / `roll` / `drop` / `list` / `brief-line` / `render-for-wrap` / `prune` / `wrap-ask`
/ `wrap-apply` / `lead-ask` / `lead-apply` / `reconcile`) and its tests ship; the modes already call
it. The scheduled Brief receives the rendered line in its prompt (`presence._brief_context`; a
chat-invoked Brief calls `brief-line` directly) and a picker tap is routed back into `apply_wrap_grid` /
`apply_lead_answer` (`presence._tomorrow_wrap_clause` / `_tomorrow_lead_clause`).

**The request:** a way for the owner to mark things to prioritize the next day — a tag they can put on
a Task/Reminder (or a `<tomorrow>` chat tag) that the Brief leads with and the Wrap rolls forward or
closes. The motivating stopgap is instructive: a handful of goals for the next day, parked as throwaway
Tasks rows because nothing else could hold "the plan for tomorrow, in this order".

Read with `seneschal/references/reminders-policy.md` (the reminders tracker this sits beside without
joining), `seneschal/references/databases.md` (the Tasks + Reminders schemas this adds one property to),
`seneschal/modes/brief.md` + `seneschal/modes/wrap.md` (where it wires in, alongside
`subagents/morning-briefing/SKILL.md` + `subagents/eod-wrap/SKILL.md`), `seneschal/references/memory.md`
(§1 leans on its carry-over rules directly), and `seneschal/references/autonomy-policy.md` (the gate
every door in §2 sits under).

---

## 1. The problem, and why carry-over is the wrong home

What exists without this is `carry-over.md` — and it is the wrong home on its own terms.
`seneschal/references/memory.md` states carry-over's rule directly: **rebuild from current state each
run (resolved items drop off; new ones appear).** Carry-over is memory of what the assistant is holding
*for* the owner — held drafts, unanswered invites, promised follow-ups — rebuilt fresh every run and
pruned the instant something resolves. It has no concept of "in this order," no concept of "these are
the ones that matter tomorrow specifically," and nothing in it survives a rebuild by design. A goal the
owner names for tomorrow is not a loop the assistant is holding; it is a **plan for one day**, in **the
owner's order**, that needs to still be there tomorrow morning exactly as they left it and needs someone
to ask what happened to it tomorrow evening — the opposite of "rebuilt each run."

The throwaway-Tasks stopgap shows the shape of the gap: Tasks has no ordering field, no "this is for
tomorrow, specifically" flag, and nothing that leads the Brief with them. A `Priority` of
`Low`/`Medium`/`High` and a `Due` field that means "due," not "the plan for the day," is not a fix.

## 2. The three entry doors — all act-low, all silent on capture

Marking something is not a decision that needs approval; it is the owner telling the assistant what
their own day looks like. Every door below writes act-low, the same class as updating a tracker row as
part of a pipeline the assistant owns (`seneschal/references/autonomy-policy.md`).

### 2.1 (a) — a `<tomorrow>…</tomorrow>` chat tag

A capture tag alongside `<todo>` (a commitment) — `seneschal/modes/chat.md` rule 14. `<todo>` routes to
Reminders / Tasks; `<tomorrow>` routes to **only** the durable store in §3 — it does not create or
require a Task or Reminder row. That is deliberate: the mark is about *ordering tomorrow's Brief*, not
about creating a new obligation-bearing record. If the owner also wants the thing tracked as a
commitment, they type `<todo>` for that — the two tags compose
(`<tomorrow><todo>Ship the spec</todo></tomorrow>` both marks and tracks), but neither implies the other.

`for_date` is computed the same way every other "today" in this repo is — the owner's configured
timezone (`tz_common`) and the after-midnight day-boundary cut — **plus one day**: a `<tomorrow>` typed
at 1am Tuesday, while the day-boundary rule still treats "today" as Monday, means "the day I'm about to
wake up into" — Monday's tomorrow, not Tuesday's. `order` is the sentence order the owner typed several
tags in, appended after whatever is already queued for that `for_date`.

### 2.2 (b) — a `Tomorrow` flag on Tasks and Reminders

**Property: `tomorrow` (boolean) on both Tasks and Reminders — on the Notion backend, a `Tomorrow`
checkbox.** Default unset — the same shape as `Nag Until Done` and `Call Me`, not a new select, because
this is one more independent flag on a row, not a new state machine. The owner ticks it on a row they
are already looking at, without going through chat at all.

**It is a one-shot input, consumed on reconciliation — exactly like `Ack`.** The next Brief or Wrap
gather that reads Tasks/Reminders (§4.1/§4.2, both already read these every run) also filters
`tomorrow = true`, upserts an entry into the durable store keyed on the row id (so re-running
reconciliation never duplicates it), and **unticks the flag** the same pass — the reminders `Ack` rule
(a stale tick must not auto-complete a later cycle) applies verbatim. A row ticked mid-morning is
picked up by that evening's Wrap; nothing polls the store outside the reads the Brief and Wrap already
do.

**Ordering from this door is append-only, not authored.** A checkbox carries no position, so an item
arriving through it is appended after whatever the store already holds for that `for_date`, in the
order the reconciling read encountered the rows. Reordering is a chat follow-up or the Wrap picker
(§2.3), not a new field.

### 2.3 (c) — a Telegram picker at Wrap time, "what's tomorrow's lead?"

The Wrap (§4.2) already gathers tomorrow's due Tasks and near-due deadline watches. It offers them back
as a multi-select picker (`seneschal/scripts/telegram_ask.py`, `--multi`) rather than asking the owner
to retype anything the store already knows: *"Anything here should lead tomorrow?"*, each candidate an
option. Selections append to the store for tomorrow's `for_date`, `source: "wrap_picker"`. This door
**promotes what the assistant already knows about**; it does not accept freeform text (§6.6).

## 3. The one durable store

`state/tomorrow.json` (gitignored runtime state), read and written through one script —
`seneschal/scripts/tomorrow_marker.py` — the same one-writer shape `seneschal/references/memory.md`
cites for `open-loops.json` (`seneschal/scripts/loops.py`), so nothing else touches the file directly.

```json
{
  "schema": "seneschal.tomorrow-marker/1",
  "items": [
    {
      "id": "tmw-20260102-1",
      "text": "Ship the datapath spec",
      "for_date": "2026-01-02",
      "order": 1,
      "source": "chat_tag",
      "linked_kind": null,
      "linked_page_id": null,
      "status": "open",
      "created_at": "2026-01-01T21:07:00Z",
      "resolved_at": null
    }
  ]
}
```

- `source` ∈ `chat_tag` | `notion_task` | `notion_reminder` | `wrap_picker` — where the mark came from,
  for audit only; nothing branches on it except reconciliation's dedupe key.
- `linked_kind`/`linked_page_id` — set when the mark rides a Task or Reminder row (door b, and a
  composed `<tomorrow><todo>`); `null` for a bare chat-tag mark with nothing backing it.
- `status` ∈ `open` | `rolled` | `done` | `dropped` — `open` and `rolled` are both "still active"; the
  distinction is only whether the Wrap has already carried it forward once.
- Writes go through the atomic state-write primitive (`stateio`), never a bare `open(path, "w")` —
  `check_state_writes.py --enforce` blocks that shape in CI, and a small, frequently-touched file is as
  exposed to truncation and concurrent-write races as a large one.

**Retention.** `open` and `rolled` items are **never pruned by age** — the reminders policy's
auto-retire argument applies even more directly here: an item silently dropped for being old is work
the owner still owes, dropped with nobody asking. Only `done`/`dropped` items age out, and only after
the Wrap's picker put them there — Dream prunes entries whose `resolved_at` is more than 3 days old
(short, because these are day-scoped). `seneschal/modes/dream.md` step 2 calls
`python scripts/tomorrow_marker.py prune --keep-days 3` alongside its other state housekeeping; the
prune is fail-open by construction — a missing or unreadable `state/tomorrow.json` loads as an empty
store, so the call can never cost the Dream run.

**Reconciliation without a runtime schema fetch.** The flag's name and type are baked into the store
schema once, the same way `Nag Until Done` is — the Brief and Wrap query it by name in the batch that
already reads Tasks/Reminders (never fetch a schema at runtime). Nothing here adds a new store read; it
widens an existing filter.

## 4. Brief / Wrap / Reminders wiring

### 4.1 Brief — leads with the marked items, in the owner's order

A section, **"🎯 Tomorrow's Lead,"** sourced from the marker script's `brief-line` (the same pattern
`seneschal/scripts/owi_unknowns.py brief-line` uses for the unassigned-work line): items where
`for_date` = today and `status ∈ (open, rolled)`, in stored `order`, rendered by code and printed
verbatim — never re-derived or re-ordered by the run itself. For the scheduled run, `presence.py`'s
morning slot injects the rendered text into the run's prompt, as it does for the unassigned-work line
(pending daemon wiring); a chat-invoked Brief calls the script directly. **This section sits above
"Needs You,"** not folded into it — it is not a subset of what needs a decision today, it is the day's
own stated plan. Empty → the section is omitted, never printed as "nothing marked"
(`seneschal/references/briefing.md`).

An item surfaced here that also appears in the day's Tasks/Flags read is de-duped into this section
only — it is the most action-relevant section while it is active.

### 4.2 Wrap — closes, rolls, or drops each marked item, one picker, never a list

For every item with `for_date` = today and `status ∈ (open, rolled)`, the Wrap sends **one grid
picker** (`tomorrow_marker.py wrap-ask`, over `telegram_ask.py ask --grid`), one row per item, choices
`Close (done) | Roll to tomorrow | Drop` — never a prose list asking the owner to type three decisions
in one message, the same argument `seneschal/docs/picker-state-marking-spec.md` makes for merge
approvals. Resolving:

- **Close** → `status = done`, `resolved_at = now`. If the item is `linked_kind = task` or `reminder`,
  this returns a *proposal* to flip the linked row done — ask-high, exactly as flipping a linked
  Task/Goal already is (`seneschal/references/reminders-policy.md` → "Act-low vs ask-high") — never an
  automatic write.
- **Roll to tomorrow** → `status = rolled`, `for_date` advances one day, `order` unchanged (it still
  leads, having already earned the spot once).
- **Drop** → `status = dropped`, `resolved_at = now`. Costs nothing — no post-mortem, no "you didn't
  finish X" framing.

**No silent auto-roll** (§6.3): the Wrap always asks, which also means no separate escalation is needed
for an item rolled many nights running — this domain asks every evening by construction, so a
chronically-rolled item is visible for free.

The same Wrap run offers the §2.3 "what's tomorrow's lead?" picker for the *next* day
(`tomorrow_marker.py lead-ask`). Both pickers resolve through the daemon's callback dispatch — the Wrap
run never writes `state/tomorrow.json` itself (pending daemon wiring).

### 4.3 Reminders — a marked today-todo nudges exactly as it always did

Marking changes **ordering and resolution**, never **urgency mechanics**. A linked item that is also a
live Reminders row keeps whatever `Importance`/`Nag Until Done`/`Call Me` it already had. A marked
today-todo still gets its one ordinary nudge, same as any other; the mark adds nothing to the
escalation ladder and **pierces nothing new** — it does not become Critical, does not gain a re-fire
ladder, and does not pierce a quiet window it wouldn't already pierce. A bare chat-tag mark with
`linked_kind: null` has no Reminders row at all and therefore **never nudges** — see §5.

## 5. What this is NOT

- **Not a priority-field rewrite.** Tasks' `Priority` and Reminders' `Importance` are untouched;
  marking something "Tomorrow" says nothing about how loud it is.
- **Not a reminder-ladder change.** `Nag Until Done` and `Call Me` are independent of this marker, as
  they are of each other. This adds another independent axis, not another OR clause on the same ones.
- **Not a way to raise the approval gate.** Every write in §2–§4 is act-low; a write that was ask-high
  before marking (flipping a linked Task/Goal done) is still ask-high after.
- **Not a second reminders database.** A bare `<tomorrow>` mark never enqueues a push nudge — it only
  shapes what the Brief prints and what the Wrap asks about. Chasing the owner is what `<todo>` and the
  reminders tracker already do.
- **Not carry-over**, for §1's reason: carry-over rebuilds every run and prunes on resolution; this
  store holds a small, explicitly-ordered plan for one day at a time, and a resolved item's fate is
  always a picker's answer, never a silent drop.

## 6. Decisions — none of them the marker itself

All six were decided by the owner in one grid picker, every row option A (the marked recommendation).
The rejected alternatives are kept below so the reasoning survives.

1. **The property's name and type.** **Decided (A): `Tomorrow`, a checkbox** — matches
   `Nag Until Done`/`Call Me`'s boolean-flag convention on the same two databases, no new vocabulary.
   Rejected: `Tomorrow's Lead` (more self-describing, longer to read in a property list). Rejected: a
   `select` with `Lead`/`Backup` values — a rank the store's own `order` field already carries, so a
   second, easily-out-of-sync copy of the same information.
2. **Does a mark expire, or persist until closed?** **Decided (A): persist — never auto-expire.** An
   unresolved item stays `open` and keeps leading the Brief every morning until the Wrap picker closes,
   rolls, or drops it — a thing dropped without anyone asking is worse than a thing that nags by staying
   visible. Rejected: auto-expire after one unresolved day — quieter, but exactly the silent drop §5
   rules out.
3. **Does the Wrap always ask, or auto-roll silently past some threshold?** **Decided (A): always ask**,
   via the per-item grid every evening. Rejected: auto-roll silently and only picker after N consecutive
   rolls — saves a tap on an uncontested night, at the cost of a lead item drifting for days with no
   visible trace.
4. **Ordering when the checkbox door adds several items on the same day.** **Decided (A): append in
   read order** (§2.2) — simplest, and reordering is cheap through the other two doors. Rejected:
   prompt for a rank when a second item lands via checkbox — adds a picker to the one door meant to be
   lowest-friction.
5. **A cap on how many items can lead at once.** **Decided (A): no hard cap** — a six-item "lead" stops
   reading as a lead and the owner will prune it; a cap enforced in code adds a refusal to a low-stakes
   marker. Rejected: a soft cap of 3 enforced by the Wrap picker.
6. **Freeform text on the Wrap's "what's tomorrow's lead?" picker.** **Decided (A): no** — new
   priorities are captured through the chat tag or the checkbox during the day; the Wrap ask only
   promotes candidates it already has (§2.3), because Telegram's inline keyboard has no text-entry
   affordance and parsing a chat reply as freeform capture reopens the "is this text data or an
   instruction" question for one narrow case. Rejected: read a reply to the Wrap's message as freeform
   text — closes the gap, at the cost of a new inbound-parsing surface.

## 7. Phase plan

**Phase 0 — the store, the tag, the property. BUILT.**

- `seneschal/scripts/tomorrow_marker.py` (stdlib): `mark()`, `close()`/`roll()`/`drop()`, `for_day()`,
  `brief_line()`, `render_for_wrap()`, `prune()`; atomic writes through `stateio`.
- `state/tomorrow.json` as gitignored runtime state.
- The `<tomorrow>` tag in `seneschal/modes/chat.md` rule 14, beside `<todo>`.
- The `tomorrow` flag on Tasks and Reminders (on Notion, a one-time additive checkbox — no migration).
- Tests (`seneschal/scripts/test_tomorrow_marker.py`): mark / close / roll / drop; append ordering; the
  never-auto-expire rule (an `open` item survives a prune pass untouched); a bare chat-tag mark
  producing `linked_kind: null` and no Reminders-side effect.

**Phase 1 — Brief/Wrap wiring. Mode side BUILT; daemon side pending.**

- The "🎯 Tomorrow's Lead" section in `seneschal/modes/brief.md` and `seneschal/references/briefing.md`,
  sourced from `brief-line`. *Pending:* the scheduled morning run receiving the rendered line through
  `presence.py`'s morning slot, the way the unassigned-work line is.
- The per-item Close/Roll/Drop grid and the "what's tomorrow's lead?" multi-select in
  `seneschal/modes/wrap.md`, via `wrap-ask` / `lead-ask`. *Pending:* the daemon's callback dispatch
  routing a tap into `apply_wrap_grid` / `apply_lead_answer`.
- A linked today-todo still nudges unchanged through the reminders machinery — no code change.
- Tests: brief-line rendering, empty vs non-empty; each of the three grid resolutions; a linked-Task
  Close producing an ask-high proposal, never a direct flip; a bare mark never touching
  `state/reminders.json`. The daemon-side wiring brings its own tests.

**Phase 2 — store reconciliation. BUILT.**

- The Brief's and the Wrap's gathers each add one `tomorrow = true` filter against Tasks and
  Reminders, call `tomorrow_marker.py reconcile`, which upserts keyed on the row id, and untick the flag
  on the same pass. The model turn runs the live store query and hands the rows over; the script never
  queries the store itself.
- Tests: idempotent upsert (reconciling twice does not duplicate); the untick list; a stale or
  unreadable property failing open (never blocks the Brief or Wrap).

**Retention wiring — Dream. BUILT.** `main()`'s `prune` subcommand (`--keep-days`, default
`PRUNE_AFTER_DAYS`), called from `seneschal/modes/dream.md` step 2. Tests re-prove the never-auto-expire
invariant through the CLI path, not just the bare function — an `open`/`rolled` item survives, only an
aged `done`/`dropped` row goes; a missing/unreadable store is a no-op, never a failed run.

## Related

- `seneschal/references/reminders-policy.md` — the tracker this sits beside without joining; its
  auto-retire and `Ack`-consumption rules are §3/§4.2's direct precedent.
- `seneschal/references/databases.md` — the Notion backend's schema registry, where the `Tomorrow`
  property belongs (the registry row itself is not added yet; the modes already fail open when the
  field is absent).
- `seneschal/references/memory.md` — §1's argument that carry-over is the wrong home.
- `seneschal/modes/chat.md` rule 14 — the capture tags.
- `seneschal/docs/picker-state-marking-spec.md` — why a picker, never a prose list, for a per-item ask.

## Router entry

`tomorrow-marker-spec.md` — **PARTIAL(§6 decided; phases 0-2 built in the module and the mode files;
the daemon's morning-slot injection and picker callback dispatch pending)** — a first-class "prioritize
tomorrow" marker: three act-low capture doors (a `<tomorrow>` chat tag, a `Tomorrow` flag on
Tasks/Reminders, a Wrap-time Telegram picker) into one durable `state/` file the Brief leads with and
the Wrap resolves (close/roll/drop) per item, one grid picker, never a prose list; explicitly not
carry-over, not a priority-field rewrite, not a reminder-ladder change.
