# The ack system — ownership, and who flushes the outbox

**Status:** `SPEC-ONLY(redesign declined)` — nothing built; a full redesign was offered and is declined
here with reasons (§7) · **Owner:** the assistant · **Scope:** who owns flushing
`state/notion-outbox.sqlite`, and — asked, weighed, and **declined** — whether the notification /
acknowledgement system as a whole should be redesigned. Carries the **full component map** (§3) either
way. Companions: `notion-write-behind-outbox-spec.md` (the store and the flush fork; this spec does not
restate it), `../scripts/reminders_acks.py` (the ledger and the second-sender gate),
`../scripts/ack.py` (the typed-phrase front door), `../references/reminders-policy.md` (the fire-time
policy).

## Backend scope

Everything about *flushing* here is **Notion-backend only**, like the outbox itself
(`notion-write-behind-outbox-spec.md` → "Backend scope"). On the filesystem backends (`obsidian` /
`markdown`) an ack is a local, atomic store write made in the same turn — there is nothing pending and
no second owner to disagree with. The component map (§3), the fire path and the ack ledger apply to
every backend.

> **The event that prompted this.** A warm chat turn ran `ack.py` for two reminders. `ack.py` is
> stdlib and has no MCP, so it cannot write the store and leaves every entry it journals `pending` **by
> design**. The turn then told the owner the entries were *"queued and safe — the daemon lands them on
> its next tend cycle."* **That is false while the session is live**: the daemon's backstop stands aside
> for a live session (§3.1), so the turn that journalled the entries owns flushing them. The same
> misreading had recurred before, each time reported as a drain bug that did not exist.
>
> **The central finding is that it was, again, not a drain defect — and not a harm either.** The
> entries landed a few minutes after they were journalled (§2.3), both second-sender gates were closed
> for the whole window (§4), and nothing was at risk. What failed was **the sentence**, repeatedly,
> against several written rules. §6 recommends fixing the sentence, in code, and **not** touching the
> drain — with the measurement that would reverse that call stated in §6.4.

---

## 1. The question, and why the obvious answer is wrong

The rule *"the warm session drains its own writes in the same turn"* is written in several places —
`../modes/chat.md` rule 5 (which carries a dedicated paragraph on the backstop's gates),
`notion-write-behind-outbox-spec.md`, and the outbox CLI's own help. **Readers still got it
backwards**, including after the paragraph written specifically to stop it.

That shape has a known answer in this repo: when a prose rule was already in place and prevented
nothing, the fix is a **structural refusal**, not another sentence. `jobs.py`'s preflight refusal is
the pattern — the guard evaluates, the caller obeys — and `job_analysis.py` makes the same move by
giving its schema no field a diagnosis could fit into.

So the framing question is: **what makes the ownership structural rather than remembered?**

The tempting answer is *"move the flush"* — drop the live-session gate, shorten the staleness window,
add per-op urgency, or flush on turn completion. **§2 measures that family of answers to nothing.** The
flush is not late and has never lost a write. What is wrong is what the assistant *says about* the
flush, and no change to the flush's schedule makes a false sentence about it true.

## 2. What was measured — the drain is healthy, and this is the number that decides the spec

Read-only, from a reference install's live outbox: **303 entries** over five weeks.

### 2.1 Nothing has ever been lost, stalled, or written backwards

| Reading | Value |
|---|---|
| entries by status | `done` **303** · `pending` 0 · `inflight` 0 · `failed` **0** |
| entries by resolution | ordinary (wrote to the store) **303** · `superseded` **0** |

**Zero dead-letters in 303 entries**, and zero supersessions — the stale-ack guard has never had to
fire. The fail-closed doctrine held completely.

### 2.2 The daemon taking ownership of the backstop is the whole improvement

Latency = `last_attempt_at − created_at` for the ordinary `done` entries carrying both stamps.

| Cohort | n | median | p90 | max |
|---|---|---|---|---|
| **before the daemon backstop** | 114 | **10.5 h** | 49.0 h | 200.6 h |
| **after the daemon backstop** | 183 | **134 s** | 21.5 min | **31.7 min** |

**The worst case since the daemon took the backstop is 31.7 minutes**, against a 3 h staleness alarm
that has never had anything to fire on. A median of hours is what an unowned flush looks like; a median
of about two minutes is what the current design produces.

### 2.3 The prompting event, to the second

| t | Event |
|---|---|
| 0 s | `ack.py` journals the entries. `state/acks.json` is stamped in the same call (§4.1). |
| 0 s | The turn asserts the daemon will land them. **False** — the session is live. |
| 214 s | All entries marked `done`, one attempt each, by the in-turn drain. |

**Elapsed: 214 seconds**, against a same-day median of 224 s for other entries — **typical, not an
outage.** The daemon's own drain had not been needed for days, because the in-turn drain keeps getting
there first.

**This is the number every proposal in §6.1 has to beat, and none of them do.**

## 3. The map — every component, every ack door, every gate

Read from source rather than recalled.

### 3.1 The pipeline, end to end

| Stage | Where | What it owns |
|---|---|---|
| the ⏰ row | the store (`../references/databases.md`) | the **system of record**: `Status`, `Last Acknowledged`, `Consecutive Misses`, `Ack`, `Cadence`, `Nag Until Done` |
| seeding | `presence.maybe_seed_day` → `reminders_seed.py` → `reminders_enqueue.py` | appends the day's due nudges to `state/reminders.json` |
| the queue | `state/reminders.json`, lock `reminders_acks.queue_lock` | pending nudges; reconciled nightly by Dream step 2 (`reminders_reconcile.py`) |
| firing | `sentinel.check_reminders` → `_check_reminders_locked` | the seven ordered gates (§3.3) |
| delivery | `telegram_send.py` / `push_call.py` / `discord_send.py` | one prose chokepoint per surface |
| ack intake | four doors (§3.2) | resolve a phrase or reaction → a ⏰ row id |
| durable ack | `reminders_acks.record_ack` → `state/acks.json` | **fail-OPEN** — "was this acked today?" |
| queue cleanup | `reminders_dequeue.py` | drop the row's un-fired nudges, **scoped to one activity day** |
| durable store write | `outbox.py ack` → `state/notion-outbox.sqlite` (Notion only) | **fail-CLOSED** — "has this landed in the store yet?" |
| the flush | in-turn (prompt-side) **first**; the daemon's outbox backstop second | replay the payload → the store write, then `mark` |
| second-sender gate | `reminders_acks.watch_escalation_blocked` | stop a Watch peek chasing an already-acked row |
| observability | the daemon's backlog nudge · `outbox.py status` · `!status` · cockpit | a 3 h staleness push, dead-letters on sight |

The daemon's backstop **stands aside while an interactive session is live** (`sentinel.session_is_live`)
and otherwise drains an entry once it is 20 minutes stale. Both halves matter to §4–§6.

### 3.2 The ack doors — four, and one field with no reader

| # | Door | Entry point | Writes |
|---|---|---|---|
| 1 | typed phrase (alias) | `ack.py` stage 1 → the owner's `references/reminder-aliases.json` (template: `../references/reminder-aliases.example.json`) | outbox ack (Notion) → dequeue + ledger |
| 2 | typed phrase (live title) | `ack.py` stage 2 → `reminders_live.py` (Notion only; degrades elsewhere) | the same apply path — one write path |
| 3 | 👍 reaction | `presence.ack_reminder_by_reaction` | dequeue + ledger, then `outbox.py ack` on Notion |
| 4 | the chat turn itself | `../modes/chat.md` rule 5 | the direct store write **and** the enqueue (belt-and-suspenders) |
| — | **the owner ticks `Ack` in the store** | **nothing** | — |

**There is no fifth door.** `Ack` is a *written* field only; no code path reads the checkbox back.
`../references/reminders-policy.md` states the design intent — "acked today" is answered by `Last
Acknowledged` + `Status`, never by scanning for ticked boxes — so this is deliberate, not a gap. It is
listed because a redesign that "unifies the ack doors" would otherwise silently invent a poller for a
field whose whole point is that nobody polls it.

### 3.3 The fire path — seven gates, `sentinel._check_reminders_locked`

Order is the invariant: **`acked → quiet → CURFEW → presence → live-session → STALENESS → stagger`**.
Four **DROP** (consume the entry, stamp it, never re-deliver); three **DEFER** (stamp nothing that
changes pending-ness, re-check next tick).

| # | Gate | Kind | Predicate |
|---|---|---|---|
| 1 | acked-today | DROP | `reminders_acks.entry_acked(entry, acks, today_local)` |
| 2 | quiet window | DROP | `is_quiet` ∧ ¬`entry_pierces_quiet` |
| 3 | night curfew | DROP | `in_night_curfew(now, due)` — **a conjunction**; the window is `owner.nightCurfew` in `persona/identity.json` (default 01:00–07:00 owner-local) |
| 4 | presence | DEFER | `presence_rules.should_defer` — accumulates `presence_held_sec` |
| 5 | live session | DEFER | `sentinel.session_is_live` |
| 6 | staleness | DROP | lateness **minus** `presence_held_sec`, against `MAX_LATENESS_SEC` (2 h) |
| 7 | catch-up stagger | DEFER | one non-piercing fire per window; an ack advances the drip after a short debounce |

Two positions are load-bearing and are behaviour, not layout: **curfew under quiet** (so an explicit
quiet request keeps naming itself when both apply) and **staleness after the live-session defer** (so a
session-held nudge dies there) **while being unreachable from a presence hold** (gate 4 `continue`s
above it, which is what preserves NO DROPS on presence-deferred nudges).

### 3.4 The outbox — the primitives that matter to §5

| Primitive (`outbox_common.py`) | Property |
|---|---|
| `enqueue` | idempotent on `idempotency_key` (`UNIQUE`); a `failed` key **revives** |
| `claim_ready` | `UPDATE … WHERE id=? AND status=pending`, `rowcount == 1` → **the claim is atomic** |
| `mark_done` | `UPDATE … WHERE id=?` — **no status guard** (§5.2) |
| `reclaim_stale` | an `inflight` claim older than the reclaim window returns to `pending` |
| `prune_done` | drops `done` entries past retention; owned by Dream step 2 (`../modes/dream.md`) |

### 3.5 Unowned lifecycles — the defect class, and its owners here

*"A documented housekeeping step with no owning script"* is a recurring defect class: a step described
in a mode's prose, with nothing that actually runs it, reads as done while it silently isn't. The
instances that bear on the ack system, and who owns each in this tree:

| Lifecycle | Owner |
|---|---|
| the outbox **flush** | the turn (§6), with the daemon backstop behind it |
| the `reminders.json` **reconcile** | Dream step 2 — `reminders_reconcile.py` |
| the outbox **prune** | Dream step 2 — `outbox.py prune --days 14` |

The prune is not housekeeping for its own sake: the outbox arm of the second-sender gate (§4.2) scans
`done` entries on the Watch send path, so an unpruned table makes that scan grow without bound.

## 4. The harm, checked rather than assumed — and it is smaller than it looks

Is an un-landed ack invisible to the Watch peek's second-sender gate, since that gate can read the
outbox? **It reads the outbox as one of two arms, disjunctively — and the other arm is positive at
t = 0.**

### 4.1 The ledger is stamped synchronously, inside the same `ack.py` call

`ack.py` → `reminders_dequeue.py --reminder-id … --ack-date … --activity-day …` →
`reminders_acks.record_ack(state_dir, rid, ack_date)` → `save_acks` (build-then-`os.replace`). This is
in-process and completes before the ack returns.

### 4.2 Both gates read that ledger, and neither depends on the outbox alone

```
reminders_acks.reminder_acked_today            # the shared predicate
  ├─ _ledger_ack_date   → state/acks.json
  └─ _notion_ack_date   → the outbox's latest landed ack (Notion only; no opinion elsewhere)
  acked = True iff EITHER source reads today's owner-local date
```

Disagreement resolves to acked: both sources are written only by a real ack, so a positive is evidence
and a silence is not.

- **Gate A — fire time.** `sentinel` → `reminders_acks.entry_acked(...)`. Reads `acks.json` **only**.
  Never touches the outbox. Closed at t = 0.
- **Gate B — the Watch second sender.** `watch_escalation_blocked` → `reminder_acked_today` → **both**
  arms. The ledger arm reads today. Closed at t = 0.

### 4.3 The window a pending entry opens

**Zero seconds, on both gates.** The outbox arm of gate B is **redundant** here by construction: it is
the honest local stand-in for reading the store from a stdlib process. It adds a reading; it is not the
reading.

The second-sender incident that motivated gate B was a **title-resolution** failure, not an outbox
latency one: the gate scored a real escalation below its bar because the denominator was the whole
nudge *sentence* rather than the row's *title* (`reminders_acks.py`'s docstring). Fixing the flush
would not have prevented it, and a pending outbox does not reopen it.

### 4.4 So what *was* the harm?

1. **The store — the system of record — was stale for 214 seconds.** The ⏰ rows read not-acked.
   Anything reading the store in that window (a Brief, a Wrap, the owner's own eyes) would have seen
   the wrong thing. Nothing did.
2. **The assistant asserted a false mechanism claim.** This is the real harm, and it is not measured in
   seconds: it damages the thing that makes an assistant usable — that its account of its own machinery
   can be trusted without verification.
3. **Nothing was lost.** The entries were durable from t = 0. The fail-closed guarantee held.

**The spec must not inflate (1) to justify itself.** A 214-second staleness window on an act-low write
is inside the design's stated tolerance and faster than the measured p90.

## 5. Is the claim the real serializer? — and what the live-session gate actually protects

### 5.1 Yes, at the entry level

`outbox_common.claim_ready`:

```python
cur = conn.execute(
    "UPDATE outbox SET status=?, attempts=attempts+1, last_attempt_at=? WHERE id=? AND status=?",
    (INFLIGHT, now_s, r["id"], PENDING),
)
if cur.rowcount == 1:
    claimed.append(get(conn, r["id"]))
```

The `AND status=PENDING` guard plus the `rowcount` check means **two concurrent drainers cannot both be
handed the same entry** — sqlite in WAL mode with a busy timeout serializes the write. So **the
live-session gate is NOT protecting entry-level double-flush; the claim already does that.**

### 5.2 What it *is* protecting — three things, and only the third is unique to it

1. **The single-headless-store-child rule.** The daemon's drain shares its busy checks with the Watch
   peek and the scheduled runs. That is a store-serialization concern, not an outbox one, and it
   survives any change to the live-session gate.
2. **A converging duplicate update.** Under belt-and-suspenders the turn does its own direct store
   write and *then* marks the entry. A drainer claiming the entry in that gap replays the same write.
   For an update op this **converges** — same row, same fields, same values. For a create op the turn
   only *enqueues* (it makes no direct create), so the create path is single-writer.
3. **`mark_done` is not claim-aware.** It updates `WHERE id=?` with **no status guard**. A turn holding
   an id it enqueued can mark `done` an entry another drainer has claimed and is mid-write on. The
   consequence is bounded — a converging duplicate update, and an entry recorded `done` a beat before
   the second writer finishes — but it is the one hazard the claim does *not* cover.

**Verdict: the live-session gate is mostly belt-and-braces over a claim that already serializes, plus a
small real guard over (3).** That argues for leaving it alone, not for removing it: it costs nothing
when the in-turn drain works, and removing it buys a latency improvement §2 measures at zero.

### 5.3 The asymmetry, verified in code rather than quoted from prose

Standing aside costs a **delayed** write; racing costs a **double** write.

- **Delayed is bounded and observable.** `enqueue` is the durability guarantee; `mark_retry` backs off;
  `mark_failed` dead-letters without wedging the queue; the backlog nudge escalates at 3 h.
- **Double is bounded for updates and structurally prevented for creates.** Updates converge. Creates
  dedupe **at enqueue** on an intent key that answers *"is this the same thing?"* rather than *"is this
  a replay of this enqueue?"* — so two ack doors hitting one row on one day produce one created row.

So the asymmetry holds — *because of the claim and the keys*, not because of the live-session gate.

## 6. The narrow question — options weighed, one recommended

### 6.1 The four options, each measured against §2.3's 214 seconds

| | Option | Would it have helped? | Verdict |
|---|---|---|---|
| (a) | the in-turn drain **moves into code** — the daemon flushes on turn completion | The turn ended at about the same moment. **Roughly a wash**, and it costs a headless spawn per ack-bearing turn | **No**, on cost |
| (b) | drop the live-session stand-aside, rely on claim serialization | The daemon still would not have fired: its drain needs **20 min** staleness. **Zero effect.** Also re-opens §5.2(3) | **No** |
| (c) | shorten the staleness window | To help it would need to be **< 3.5 min** — below any sane margin over the daemon's check interval, spawning a store child after every ack. This is the gate that keeps a fresh queue from forking a child every tick | **No** |
| (d) | per-op urgency (acks faster than run-log finalize) | Same arithmetic as (c) for the ops that matter, and the slow class it would de-prioritise barely exists | **No** |

**None of them would have landed the entries appreciably sooner than the in-turn drain did.** Stating
that is the point of running the check.

### 6.2 RECOMMENDED — (e) the tool result carries the ownership, in code

**Do not move the flush. Move the fact.**

The failure is that a turn reads `{"ok": true, "created": true, "id": "…"}` and then narrates the
*mechanism* from memory — and memory has been wrong every time. The fix is to **put the true sentence
where the turn is already looking, and compute it in code so it cannot be misremembered.**

Phase 1 — additive, small, reversible by deleting two fields:

1. **`ack.py` and `outbox.py ack|enqueue` gain two fields**:
   - `flush_owner` — `"this_turn"` or `"daemon_backstop"`, computed by **calling
     `sentinel.session_is_live` directly**, the same predicate the daemon's drain gates on. One
     expression, two readers — never a second copy that can drift.
   - `flush_note` — a fixed string, not free text. When `flush_owner == "this_turn"`: *"PENDING. The
     daemon stands aside while this session is live — YOU own the flush: replay via the store, then
     `outbox.py mark --id <id> --done`."*
2. **An `--assert-drained` verb on `outbox.py`** printing the entries this session enqueued that are
   still `pending`. Cheap, no network, and it makes *"did I finish?"* a question with an answer.
3. **`../modes/chat.md` rule 5 loses its backstop paragraph and gains one line**: *"read `flush_owner`
   on every ack result; if it says `this_turn`, you are not done."* The prose shrinks because the fact
   moved into the tool.

**Why this is structural and not another sentence.** Every misreading was the same move — a turn
reasoning *about* the drain's gates from a half-remembered rule instead of reading the state.
`flush_owner` removes the reasoning step: the gate's own predicate has already been evaluated and its
answer is in the JSON.

**What it does not do**, stated plainly: it does not make the turn drain. It makes the turn *unable to
say it drained without checking*. That is a smaller claim than "fixed", and it is the accurate one.

### 6.3 What evidence would change this

Stated in advance, so it is a prediction rather than a rationalisation:

- **Any dead-letter appears**, or any entry is genuinely lost → the flush *is* the problem; revisit (a).
- **Max land latency exceeds the 3 h alarm** — the alarm built for this ever fires → the backstop is not
  backstopping; (a) becomes right.
- **A false mechanism claim recurs *after* `flush_owner` ships** → the structural move failed where the
  prose failed, and the answer is (a): remove the prompt-side duty entirely and pay the spawn cost.
- **A create op double-creates once** → §5.2(2)'s reasoning is wrong and the live-session gate is
  load-bearing after all.

## 7. Should the whole system be redesigned? — asked, and declined

The owner offered a full redesign on the grounds that the system had been bolted together over time.
**That is permission, not an instruction, and this spec declines it — for now, and with a route back.**

### 7.1 The case against, in three measurements

1. **The mechanism is not failing.** §2.1: zero lost, zero dead-lettered, zero superseded. §2.2: worst
   case 31.7 min against a 3 h alarm. A redesign of a subsystem with a clean measured record is a
   rewrite in search of a defect.
2. **The defect is in a different layer.** §4.3: the harm window was **zero seconds** on both gates. The
   thing that broke was an assertion, and §6.2 is a small change to the assertion's inputs. Rebuilding
   the queue, the fire path and the ledger would not touch it.
3. **"Bolted together" describes the history, not the current shape.** Each gate in §3.3 was added for
   a specific failure and every one is still doing its job. That is *accreted*, which looks the same
   from outside and is not the same thing. A clean sheet inherits all seven constraints (§9) and has to
   re-earn them.

### 7.2 The case *for*, honestly stated

- **The outbox and `acks.json` answer overlapping questions with opposite failure doctrines**
  (`notion-write-behind-outbox-spec.md` §8 defends this, and the defence is sound) — but §4.2 shows the
  Watch gate now consults **both**, so the separation that spec calls load-bearing has been partly
  re-joined at the read side.
- **`Ack` is a write-only field** (§3.2). Deliberate, but a clean sheet would either wire it or delete
  it rather than leave a checkbox the owner can tick to no effect.

Neither is urgent. Both are recorded so the next reader does not have to rediscover them.

### 7.3 What would re-open it

The one question this spec cannot answer from inside: *is the seven-gate fire path the right
decomposition at all, or is it seven special cases of two rules?* §3.3 shows four DROP and three
DEFER; whether that is the real structure or a coincidence of accretion is a question for a fresh
architectural read. **If that read says the decomposition is wrong, re-open this.** Until then §6.2 is
the change that fits the measured defect.

## 8. The design decision on staleness

**Q. Is a few minutes of store staleness on an act-low ack acceptable as the design target?**
Everything in §6 rests on *yes*: if a ⏰ row reading not-acked for even three minutes were wrong,
option (a) would become correct despite its spawn cost. **Decided: yes** — so option (a) is off the
table on latency grounds and survives only through §6.3's separate trigger (a recurrence of the false
claim), which is a question about discipline, not about the window.

**What this decision does NOT license.** It is a target for an **act-low** write on the ⏰ path, where
both second-sender gates are already closed at t = 0 by the synchronous `acks.json` stamp (§4). It is
not a general licence for store staleness on any other surface, and not a floor to design *toward* —
the measured median is about two minutes and nothing here may make that worse.

## 9. The failure ledger — every gate, and where it lands under §6.2

Every existing gate encodes a failure it was built to stop. A clean sheet that silently drops one is a
regression wearing better architecture.

| Failure | Gate it bought | Under §6.2 |
|---|---|---|
| **A late-evening nudge delivered in the small hours** — a held backlog drained at the stagger's rate for hours after the hold lifted. *The stagger is a rate limit, not a lateness bound.* | the staleness cutoff (gate 6), positioned **after** the live-session defer; the night curfew (gate 3) | **Untouched.** §6.2 changes no gate, no position, no threshold. |
| **A peek pushed "due 2 hours ago" minutes after a correct ack** | `watch_escalation_blocked` (gate B), title-sourced from the id cache | **Untouched**, and §4.3 shows it was never outbox-dependent. |
| **An after-midnight ack deleted that evening's nudges** — an unscoped cancel | `reminders_dequeue.py`'s activity-day-scoped cancel | **Untouched.** |
| **A 👍 wrote less than typing did** | the reaction door runs the same calls as the typed door | **Untouched.** §6.2 adds fields to the result; it does not change what is written. |
| **NO DROPS on presence-deferred nudges** | gate 4 accumulates `presence_held_sec`; gate 6 nets it out; gate 4 `continue`s above gate 6 | **Untouched.** |
| **Un-landed acks sitting for most of a day** | the daemon backstop + the 3 h alarm | **Untouched** — and §2.2 measures it as the fix that worked. |
| **A stale queued ack would have written `Last Acknowledged` backwards** | supersession, swept inside `pull` | **Untouched.** |

**Seven gates in, seven gates out.** That is the strongest single argument for §6.2 over a redesign: a
change that touches none of them cannot regress any of them.

## 10. The residuals — stated on their own lines

> **RESIDUAL: after §6.2 ships, the actual replay-and-mark is still prompt-side.** A turn that reads
> `flush_owner: "this_turn"` and then does not drain leaves the entry pending exactly as today. §6.2
> makes the *claim* structural; it does **not** make the *flush* structural. The backstop (20 min
> staleness, 3 h alarm) remains the only thing that catches a turn which ignores the field.

> **RESIDUAL: `mark_done` remains claim-unaware** (§5.2(3)). Adding the status guard is a one-line
> change with a real regression risk — a turn legitimately marking its own reclaimed entry would start
> failing — so it is deliberately left for a phase 2 that has a test first.

> **RESIDUAL: `flush_owner` is computed at enqueue time.** A session that ages out of its TTL between
> the enqueue and the end of the turn will have been told `this_turn` when the daemon has since become
> the owner. Harmless in the direction it errs (the turn drains something the daemon would also have
> drained, and the claim serializes it), and the reverse direction cannot occur.

## 11. Fixed constraints

None of the following is changed, proposed for change, or worked around: the 20-min drain staleness ·
the night-curfew window (owner config) · **NO DROPS** on presence-deferred nudges · Done-vs-Finished ·
stagger-don't-batch · the act-low / ask-high line. Every write in §6.2's scope is already act-low and
**nothing here becomes newly autonomous**. `ack.py` does **not** learn to write the store.

## 12. Phases

**Phase 1 — the claim becomes structural.** `flush_owner` + `flush_note` on the enqueue surfaces,
computed from `sentinel.session_is_live`; `outbox.py --assert-drained`; `../modes/chat.md` rule 5
trimmed to one line. Additive, no gate touched, reversible by deleting two fields.

**What phase 1 would prove:** whether a false mechanism claim can recur when the true one is in the tool
result. The measurement is simply *does it recur* — and §6.3 pre-commits to what a recurrence means.

**Phase 2 — gated on phase 1's answer alone.** Either the `mark_done` status guard with a test (§10), or
option (a) in full if the discipline is measured unbuyable per §6.3.

**No flag day.** Phase 1 changes no behaviour on any existing path; a turn that ignores the new fields
behaves exactly as today.

## Router entry

**Status:** SPEC ONLY — nothing built; redesign offered and declined.

**What it decides:** Who owns flushing the outbox (Notion backend), plus the ack system's full map
(§3: four ack doors — and the owner's `Ack` checkbox, which **has no reader at all** — and the seven
fire gates, four DROP / three DEFER). **The measurement is the spec**: zero entries lost or
dead-lettered; median land latency fell from hours to about two minutes once the daemon owned the
backstop, so none of the four candidate flush changes would have helped. **The harm window is zero
seconds**, because `acks.json` is stamped synchronously and both second-sender gates read it. The
defect is **the sentence** — a repeated false mechanism claim — so the fix is `flush_owner` computed in
code into the tool result, with §6.3 pre-committing to what a recurrence would mean. **The claim is the
serializer** (`WHERE status=PENDING`); the live-session gate is belt-and-braces over one real hazard:
**`mark_done` has no status guard**.
