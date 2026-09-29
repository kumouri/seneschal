# Substance or silence — the turn that has nothing new to say

**Status:** `PARTIAL(the written rule BUILT — seneschal/modes/chat.md rule 13; turn_suppression.py
BUILT — side map, verdict seam, audit log + CLI; the landed predicate
reminders_acks.reaction_ack_fully_landed and the drainer's third outcome in presence.py BUILT;
phase 3 is a week of measurement)` — the predicate reads the Notion outbox, so on a filesystem backend
nothing ever qualifies and every reaction relay still runs a turn: the designed fail-open direction.

**The owner's request:** don't reply to every message just because one arrived. A turn whose entire
content is a restatement of something the owner already has is a buzz with no fact in it.

**The mechanism, in three phases:**

* **Phase 1 — the predicate.** `reminders_acks.reaction_ack_fully_landed`: has **every** store write a
  reaction ack on this reminder owes already landed? Composes the store-landed arm of the existing
  acked-today check (§4.2) with a conjunction over any companion writes the same ack enqueued (§4.3).
  Pure, read-only, fail-open. Lands with the reminders-ack work, after this module.
* **Phase 2 — `turn_suppression.py` plus the drainer's third outcome**: popped, not spent, not
  delivered. The side map is §6's option 2 (a bounded dict on the daemon state, keyed by the queued
  text, not persisted); the bookkeeping is §6's table, line for line; and **every suppression appends a
  row to `state/suppressed-turns.jsonl` carrying the withheld line verbatim**, because a suppression
  nobody can count is how a silent-drop defect ships. The module ships now
  (`seneschal/scripts/turn_suppression.py`, tests `seneschal/scripts/test_turn_suppression.py`); the
  drainer outcome arrives with the daemon wiring.
* **Phase 3 — a week of counting** once phase 2 is live (§6.1).

**One deviation from §6's table, stated rather than discovered.** The table asks for *one*
suppressed-turn event on the cockpit tee; the code emits a `turn_started`/`turn_done` **pair**. The
reader is why: `cockpit/web/src/chatEvents.ts` attaches any event that is not `turn_started` to the
**newest turn in its list**, so a lone suppressed frame would graft itself onto an unrelated turn. The
pair is the protocol as it exists, needs no web change, and renders the suppression as what it is — a
turn that produced no output, with the reason in `reply_preview`.

**What does NOT change:** nothing about what is written (§5), no reminder can be silenced by this door
(§5, and it is structural — reminders fire from `sentinel.check_reminders`), the approval gate is
untouched, and `seneschal/modes/chat.md` rule 13 stands as written. **No new rule is added anywhere.**

---

## 1. The trigger, reconstructed from the daemon's own records

The pattern that motivates this is common and easy to reproduce: the owner reacts 👍 to several
reminder nudges within half a minute. Each reaction is relayed as its own queued line. Reconstructed
from the daemon's logs, the interleave log, the outbox store and the assertions log (not from the
conversation):

| Offset | What happened |
|---|---|
| T+0 s | 👍 #1 on reminder A — ack recorded locally; outbox entry created |
| T+~30 s | **turn 1 starts** on ack #1 |
| T+21 s / T+22 s | 👍 #2 (reminder B) and 👍 #3 (reminder C) — both land **mid-turn** |
| T+~135 s | **all three ack writes → `done`**, first attempt |
| T+~160 s | turn 1 closes and its reply is delivered — **this one is the substance**: it reports the writes |
| T+~163 s | **turn 2 begins** on ack #2 — its own write landed **~27 s ago** |
| T+~187 s | **turn 3 begins** on ack #3 — its own write landed **~51 s ago** |

**Three replies in under a minute.** One turn did all the work; the other two spent a warm-session turn
each to say it had already been done.

**The rule takes this from three to one, not to zero — and one is right.** Turn 1's reply is the
report that the writes landed. If a successful write were silent, a *failed* one would be the only
thing that ever spoke, and the whole reason a 👍 is trustworthy is that failure is audible against a
background of confirmations. §7 makes that a hard constraint rather than a preference.

### 1.1 The interleave classifier already had the fact

A mid-turn interleave classifier observing the same stretch (in observe-only mode) labelled both
redundant acks **`steer`** — *"this belongs in the turn already running"* — at high confidence, within
seconds of arrival and more than a minute and a half before turn 1 ended. This is not a claim that live
interleave should ship; it is a claim that **two independent layers already had the fact, at different
times, and neither could act on it.**

---

## 2. The two candidate fixes

**(A) Don't wake a turn for an ack that has already landed.** Cheap, deterministic, and it removes the
buzz rather than relying on a model choosing silence.

**(B) Let a chat turn produce no outbound message.** General — it covers every duplicate-relay class,
not just acks — but it spends a turn and it depends on judgement.

They are not exclusive. (A) without (B) leaves other classes buzzing; (B) without (A) still spends a
turn and still depends on judgement.

---

## 3. (B) — can a chat turn produce no message? **Not naively: trying wedges the daemon.**

The warm session returns an empty model turn to the drainer as `""`, not `None`. `None` is the
dead-session branch; `""` is not, so without a dedicated guard it falls straight through to
`deliver_reply(channel, "", …)`. From there:

1. `deliver_reply` → `sentinel.send_telegram("")` → `telegram_send.py`, which refuses an empty body
   before it touches the network (`{"ok": false, "error": "no --text or --text-file provided"}`,
   exit 2).
2. `ok:false` is indistinguishable, one layer up, from a transient Telegram outage. `deliver_reply`
   retries once, then returns `False`.
3. The drainer's undelivered branch does the right thing for an outage and the wrong thing here: it
   **rolls the attempt counter back** — deliberately, so a Telegram outage never dead-letters a good
   message — keeps the message at the head of the durable queue, ends the warm session (not
   resumable), and sleeps.
4. Because the counter is rolled back, **`MAX_TURN_ATTEMPTS` is unreachable.** The dead-letter that
   bounds every other poison message cannot fire.

Driven against the real drainer with a warm-session double returning `""` and a send double returning
exactly what `telegram_send.py` returns for an empty body: **2 warm-session spawns and 4 send attempts
in 12 seconds, the message still queued at `attempts: 0`.** Each cycle is a fresh cold-grounded session
and a full turn. Left alone it never stops.

There is also **no sentinel convention** — no reply string means *"deliver nothing"*. `deliver_reply`
has three shapes: stub-send (test harness only), cockpit (always "delivered"), and a real send.

**So the obvious way to reach for (B) is worse than the bug it would fix.** Any (B) has to be a
*drainer* change: a turn outcome that is neither delivered nor undelivered — pop the queue, don't send,
don't respawn the session. That is the same class of change as (A) §6, and it should be built **on top
of** (A) rather than instead of it, because it is the more expensive half (a turn is spent either way)
and the less certain one (a model decides). The empty-reply guard in
`reply-marker-forcing-function-spec.md` §9 is exactly that outcome, applied to genuinely empty output.

**Consequence for the prose rule, and it is the load-bearing one:** a written instruction to *"say
nothing"* is an instruction to produce an empty reply, which — without that guard — is an instruction
to wedge the daemon. `seneschal/modes/chat.md` rule 13 therefore states the principle, names the
never-silent list, and **explicitly forbids the empty reply**, pointing here.

---

## 4. (A) — can the daemon know? **Not at relay time. At DRAIN time, yes.**

### 4.1 Relay time: no

The tempting framing is *"the relay woke a turn for an ack whose store write was ALREADY resolved."*
That is true of the turn but not of the relay. In §1, ack #2 was relayed ~2 minutes before its own
outbox entry landed, inside a turn that had not started when it was relayed. **Nothing was knowable at
relay time**, and no enqueue-time check could have suppressed it.

The reactions were not one poll batch either — they arrived as separate updates, some while turn 1 was
already in flight — so there is no batch-level de-duplication available. Each is correct to enqueue.

### 4.2 Drain time: yes — the acked-today check, read by source

An acked-today predicate over two local sources already gates the Watch send path, and it **reports
which source knew**:

* `ledger` — `state/acks.json`, stamped **by the daemon's own local half** on dequeue. Positive within
  milliseconds of the 👍.
* the store-landed arm — the outbox's latest *landed* ack, counting **only `done`, non-superseded
  entries**, i.e. rows that actually **wrote to the store**.

**Only the second arm answers the question this fix asks**, and the distinction is the whole design:
*has the reminder row's store write for today* **landed**? Using a bare `acked` would suppress the very
first relay — the ledger arm is positive the instant the 👍 arrives, before the one turn that has to
run. The ledger arm is right for the fire gate it was written for (*"don't nudge about a thing just
acked"*) and wrong for this one (*"has the write the owner is owed happened"*).

Against §1: at the start of turn 2 the store-landed arm had been positive for ~27 s; at turn 3, ~51 s.
Both redundant turns suppressed; turn 1 untouched.

### 4.3 Companion writes are part of the answer, not a footnote

A reaction ack can enqueue more than one store write — the ack itself plus any standing companion rows
the reminder is configured to log. A turn suppressed because the *ack* landed while a companion write
sits `pending` would suppress the turn that was supposed to flush it. That they usually land together is
a fact about one morning, not a guarantee.

**So the suppression predicate is: every outbox entry this reaction created is `done`.** The idempotency
keys are deterministic and already in the store; nothing new has to be written to compute this.
**Anything short of all-`done` relays the turn** — the fail-open direction §7 demands.

### 4.4 There is already a door that does exactly this

**A wrist/watch ack path is (A), shipped, for a different door**: it resolves a watch tap to the latest
ackable nudge, runs the *same* reaction-ack write, and relays **nothing** — a wrist tap produces no line
for the warm session to read, so there is nobody to tell; the outcome goes to the log and the rows to
the outbox, which the daemon drains on its own.

That reframes the Telegram path: **the reaction relay is not there to answer the owner. It is there to
get the store write done sooner than the outbox backstop would.** The synthesized reaction line is an
instruction to the turn — *"the reminder row's store write has NOT happened yet: please write it
through now"* — and once it **has** happened, the relay has no remaining purpose. The buzz is a side
effect of a flush request, not a reply the owner asked for.

On a filesystem backend, which writes directly rather than through the outbox, the ack write lands
before the relay is enqueued; the same predicate simply reads it as already landed.

---

## 5. What this does NOT touch

* **Nothing about what is written.** The reminder row, any companion rows, the outbox mark,
  `acks.json`, the run log — unchanged and still owed. A suppressed relay is a turn not spent, never a
  write not made.
* **Not the reminder mouth.** Reminders fire from `sentinel.check_reminders`, a different Mouth door
  from the chat drainer (`mouth-spec.md` §3.1). **This mechanism is structurally incapable of silencing
  a `🚨 Critical`, a `🛑 Super-Critical` or a `Call Me`** — it can only decline to spend a chat turn on
  a reaction line the daemon itself synthesized. That is not a promise; it is which code path the
  change lives in.
* **Not the approval gate.** A suppressed turn never had anything held in it: the only lines eligible
  are reaction lines whose ack half has already fully landed.

---

## 6. Why it isn't small, and where it goes

The drainer's queue entry holds `(channel, text, attempts)`. **It does not know the reminder id** — the
inbound-line builder computes it three layers away, and the synthesized line deliberately does not carry
it. So a drain-time check needs the id to travel from the relay to the drain, and every route for that
is a change to the chat path:

**Option 1 — widen the queue entry to a 4-tuple.** Touches the queue-entry helper, the persisted daemon
state schema, the edit/replace paths, both channel tasks, the cockpit call site, and the drainer's
requeue-on-failure. Survives a restart. Every one of those sites indexes the tuple **by position**, and
several test doubles construct it by hand.

**Option 2 — a bounded side map on the daemon state, keyed by the queued text.** Populated where the
inbound line is enqueued, read in the drainer, evicted oldest-first (`turn_suppression.remember` /
`take`, capped at `REACTION_ACK_CAP`). **No durable schema change, no tuple change, no call-site
churn.** It does not survive a restart — acceptable in exactly one direction: a lost entry means the
turn is relayed, which is plain behaviour. **Chosen.** The map is consumed on every drain, suppressed or
not, so a delivery failure requeues with no mapping and the retry relays — a delivery failure is not
evidence a write landed.

Either way the drainer gains a **third turn outcome** — popped, not spent, not delivered — and that
outcome has to answer for the bookkeeping a delivered turn does:

| What a delivered turn does | What a suppressed one must do | Why |
|---|---|---|
| append the owner's line to the thread tail | already done at enqueue — leave it | an owner line with no assistant reply is honest |
| record the owner's side of the turn (`turns.py`) | **must still fire** | `turns.jsonl` is the record of what the owner said; a 👍 that produced no turn still happened |
| record the assistant's side | nothing | nothing was said |
| `mouth.record_assertion` | nothing | the owner was told nothing; a row here would be the lie that file exists to prevent |
| cockpit `turn_started` / `turn_done` tee | **a suppressed-turn event** (a pair — see the deviation above) | otherwise the trace panel shows a message that vanished |
| consume the arrival marker | consume it | else it attaches to an unrelated later turn |
| interleave open/close turn | skip both | no turn window opened |
| attempt counter / dead-letter | untouched | never a failure |

**That is a real change to the daemon's chat path, so it is specced line by line and not half-built**
— this is the process that wakes the assistant, and bookkeeping one branch out of step is the most
common way a drainer change goes wrong.

### 6.1 Phases

* **Phase 1 — the predicate.** `reminders_acks.reaction_ack_fully_landed(state_dir, reminder_id, now)`,
  plus tests. Pure, stdlib. It returns a **dict, not a bool** — `landed` is the field a gate branches
  on, and the rest (`why`, the ack sources, the companion-write detail) is what the audit row carries,
  because a suppression whose reason cannot be read afterwards is one nobody can dispute. It belongs
  beside the two arms it composes. *Not yet in this tree.*
* **Phase 2 — the side map + the suppressed-turn outcome** (§6 option 2, the table above). This is the
  phase that changes what the owner experiences. It ships with a daemon-log line **and a durable audit
  trail** (`state/suppressed-turns.jsonl`, the withheld line verbatim, a `turn_suppression.py count`
  CLI) — a suppression nobody can see is the failure mode of every gate, and a log line alone does not
  survive log rotation. *`turn_suppression.py` BUILT; the drainer outcome pending.*
* **Phase 3 — measure, then decide whether (B) is still wanted.** Count suppressions and
  relays-because-not-yet-landed for a week. If phase 2 takes the ack class to near zero, (B)'s
  remaining classes may not be worth a drainer change at all.

---

## 7. The never-silent list, as encoded

The constraint set, in the order it binds. Anything not on it may be suppressed; anything on it speaks,
whatever else is true of the turn.

1. **A FAILED or PARTIAL write ALWAYS speaks.** *"I couldn't reach the store"* is never silent. This is
   the single most important property: an ack is worth something only because a failure is audible. In
   the mechanism this is not a rule the model follows — it is §4.3's conjunction: an entry that is not
   `done` **is** the not-yet-landed case, and the turn relays.
2. **`🚨 Critical` / `🛑 Super-Critical` / `Call Me` never go quiet.** §5: they do not come out of this
   door at all.
3. **A question to the owner, an approval ask, and a job-completion report all still speak.** None is a
   synthesized reaction line, so none is eligible.
4. **Silence is only ever permitted for a turn whose ENTIRE content is a restatement of something
   already delivered. If any part is new, the turn speaks.** Chat turns are strictly serialized, one
   queue entry per turn, so "the entire turn" and "this one synthesized line" are the same object —
   which is why the unit of suppression is the queue entry and never a sentence inside a reply.
5. **No change to what is WRITTEN.** §5.

**The polarity is the whole safety argument.** The harm is a suppressed message the owner needed, not
an extra one they didn't. So every path fails toward SENDING: no side-map entry, an unreadable outbox,
an id that will not normalize, a missing predicate, a raise anywhere, a restart that loses the map —
all relay. There is exactly one way to reach silence, and it requires an affirmative reading from a
store that only records a row after it has actually written. There is deliberately ONE caller of the
suppression verdict; a second one is how a mechanism scoped to one queue entry becomes a general mute.

---

## 8. Open questions for the owner

* **Q1 — is turn 1 wanted?** The rule reduces the pattern from three buzzes to one, deliberately (§1).
  Making a clean 👍 *fully* silent when everything lands is a different and larger decision — it makes a
  successful write indistinguishable from one that never happened, and §7.1 is written against it.
* **Q2 — the other relay classes.** Picker taps and job-finished lines are the remaining candidates.
  Job reports are explicitly wanted. A picker tap that arrives after the turn already acted on it is the
  same shape as this bug and is **not** covered by (A); it needs (B) or its own predicate. **The design
  honours this by omission, and the drainer wiring must assert it** — a test pinning that a button tap
  never reaches the side map, so folding one in later is a deliberate act rather than a silent
  widening of an open question.

---

## 9. The written rule

`seneschal/modes/chat.md` rule 13 — the principle, the trigger, the never-silent list, and the explicit
prohibition on the empty reply (§3). The rule is the half that survives a Dream; the mechanism is the
half that makes it hold. Another written rule would not: prose corrections to this kind of habit have a
poor track record of binding, so the mechanism is the whole of the change and the prose stands as it
is.

## 10. Where a suppression is visible

In the order a human would look: the daemon log (one line naming the reminder and the verdict),
`state/suppressed-turns.jsonl` (the durable row, with the withheld line verbatim and the full verdict
object; `!private` writes a tombstone instead of the text), `python turn_suppression.py count --days 14`,
and the cockpit trace panel (a turn with no output whose `reply_preview` says why).

## Router entry

**Status:** the written rule (chat.md rule 13) and `turn_suppression.py` BUILT; the landed predicate
and the drainer's third outcome pending; phase 3 unbuilt.

**What it decides:** The turn that has nothing new to say. The relay cannot know a reaction's store
write landed — the fact only exists at DRAIN time — and an empty reply is not silence (it wedges the
daemon), so silence is the daemon's to build: a side map carrying the reminder id to the drainer, a
third outcome (popped, not spent, not delivered), only for a daemon-synthesized reaction line whose
every owed write has landed, every path failing toward sending, and every suppression audited verbatim.
