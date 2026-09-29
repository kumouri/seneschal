# The Mouth — one voice, one clock, one assertions log

**Status:** `PARTIAL(phases 0-1 module BUILT — mouth.py's assertions log, outbound queue, staleness
prefix and drain; recording at each §2.1 send site and the presence.py drain wiring not yet landed;
phases 2-3 unbuilt)` — `seneschal/scripts/mouth.py` ships the whole phase-0/phase-1 surface
(`record_assertion` / `read_assertions` / `prune` / `origin_hand`, and `enqueue` / `read_outbound` /
`pending` / `staleness_prefix` / `drain`), covered by `seneschal/scripts/test_mouth.py`, and Dream
already prunes the log (`seneschal/modes/dream.md`). What is **not** here yet is the daemon side: the
send sites of §2.1 do not call `record_assertion`, and `presence.py`'s scheduler tick does not drain
the queue. Until that wiring lands the Mouth is an *available* door, not the only one.
**Parent pattern:** `background-jobs-spec.md` — the record, not the turn, holds the promise.

**Why this exists.** An always-on assistant speaks through many producers — reminders, job
completions, alerts, digests, turn replies. Nothing orders them, checks their age, or shapes a burst.
The failure that motivates this spec is not a wrong message; it is a *burst of stale, correct
messages arriving out of order in an alarmed tone*, each about something the owner was already told
minutes earlier in the same thread. Nothing contradicts anything, and it still reads as a committee
rather than as one assistant: **stale truths arriving out of order in the wrong tone is what a
committee sounds like.**

---

## 1. The thesis, in one paragraph

The assistant runs as several concurrent, stateless activations that die constantly. "One assistant"
cannot be a property of those processes and does not need to be. It needs exactly two things: durable
state, and **a single point of speech** — one ordered, staleness-aware channel per surface through
which everything the owner hears arrives, plus a log of what has actually been said to them. The first
is largely solved (run-log, journal, RAG, Dream, mini-dream). The second is what this spec builds.

The log matters as much as the ordering. **No hand can otherwise answer "what does the owner
currently believe, based on what we have told them?"** — which is the question you must answer before
speaking as the same person. `state/assertions.jsonl` is that answer, and every later identity
mechanism (the promises ledger, uniform orientation, freshness-before-speak) reads it.

---

## 2. The shape of the problem

### 2.1 How many mouths are there

| Speaker | Where | Can it be queued? |
|---|---|---|
| `sentinel.send_telegram` — the shared helper most producers call | `sentinel.py` | yes |
| `sentinel.send_discord` | `sentinel.py` | yes |
| `sentinel.send_call` (the `Call Me` path, Worker-side escalation) | `sentinel.py` | yes, with care (§5.4) |
| `sentinel._deliver_reminder` — routes a due reminder to its channel | `sentinel.py` | yes |
| `presence.deliver_reply` — conversational turn replies, retry-aware, **returns whether it landed** | `presence.py` | **no — §3.1** |
| the daemon's crash-loop alert | `presence.py` | no (the daemon is dying) |
| the governor/alert push | `presence.py` | yes |
| the wake line (a `--wake` job's result read back in voice) | `presence.py` | it *is* a reply |
| `jobs.reconcile(notify=…)` — every terminal job, exactly once | `jobs.py`; presence passes `deliver_reply` | yes |
| `seneschald-control.ps1` deploy-health + presence alerts | `seneschald-control.ps1` | **no — §5.1** |
| break-glass rung notifications | `cockpit/breakglass/supervisor.py` | **no — §5.1** |
| an archon's own digest / pings | the archon's tools | yes |

**A dozen producers, no coordinator.** Nothing orders them, checks their age, supersedes them, or
shapes a burst.

### 2.2 What already exists and is good

- The **file-queue pattern**, twice: `state/control-queue.json` (`request_control.py`, drained by
  the daemon) and `state/cockpit-inbox.jsonl`. Producers keep working while the consumer is down and
  it drains later. The Mouth is the third instance of a pattern the repo has already proven.
- **`jobs.py`'s delivery discipline**: `notify(channel, text) -> bool` must report whether the send
  *landed*, and `notified_at` is stamped only then. Fail-**closed**, deliberately — a Telegram outage
  retries next tick rather than eating the ping. The Mouth inherits this verbatim.
- **`presence.deliver_reply`'s honesty**: it reports whether the send landed, because a reply that is
  fired and forgotten lets a silently-dropped reply count as a delivered turn.
- **`state/telegram-message-map.json`** — message_id → what the assistant last sent. A *partial*
  assertions log already, for one surface, kept for a different purpose (reaction context). §3.3 says
  why it isn't enough and why it isn't the right thing to extend.

### 2.3 What does not exist

- Any ordering guarantee across producers.
- Any notion of *when the fact became true* as distinct from *when the message was sent*.
- Any supersede, dedupe, or burst shaping.
- A record of what has been said to the owner that a hand reads before speaking. `mouth.py` now
  provides the log; nothing writes it at the send sites or reads it at orientation yet.

---

## 3. Design

### 3.1 Two doors, one log

This is the load-bearing structural decision.

**Door A — the queue.** All *unsolicited* speech: reminders, job completions, alerts, digests,
deploy-health nudges, archon pushes. Asynchronous by nature; nobody is waiting on the return value.
These enqueue and the daemon drains them.

**Door B — direct.** *Turn replies.* A reply to something the owner just said must land inside the
turn, and `deliver_reply`'s boolean return feeds the poison-pill guard that stops an undeliverable
message replaying forever. Routing it through an async queue would break both. Replies send directly.

**Both doors append to `state/assertions.jsonl`.** The log is the identity artifact; the queue is only
the discipline applied to one door. Splitting the doors but not the log is what makes this cheap: no
existing conversational behaviour changes at all.

Consequence, stated plainly: the ordering guarantee is **total within pushes, per surface** and
**total within the assertions log**, but a reply may interleave with a push. That is correct — a reply
is by construction the freshest thing in the system, and it is what a person does when interrupted
mid-sentence by their own pager.

### 3.2 The queue item — `seneschal.outbound/1`

`state/outbound.jsonl`, append-only, one JSON object per line.

```json
{
  "schema": "seneschal.outbound/1",
  "id": "20260101-201512-a3f1",
  "surface": "telegram",
  "kind": "job",
  "text": "Job \"research pass\" finished — exit 0.",
  "origin_hand": {"source": "daemon", "session_id": "…", "cwd": "…"},
  "observed_at": "2026-01-01T19:35:02Z",
  "queued_at":   "2026-01-01T19:52:11Z",
  "supersede_key": null,
  "tone": "normal",
  "pierce": false,
  "expires_at": null
}
```

- `surface` ∈ `telegram | discord | call | cockpit`.
- `kind` ∈ `reminder | job | alert | nudge | digest | archon`. Drives the shaping rules in §3.4, and
  it is the field that keeps the reminder carve-out honest.
- **`observed_at` is when the fact became true, not when the message was built.** For a job that is
  `ended_at`, not `notified_at`. This single field is what makes staleness detectable, and getting it
  wrong makes the whole feature a no-op — see the worked example in §6.
- `origin_hand` is the same shape the job-origin spec stamps, and for the same reason. `source` comes
  from `CLAUDE_CODE_SESSION_ID` where there is one; a warm turn reads `build`, not `daemon` (§9.4).
- `tone` ∈ `normal | alert | critical`. Advisory to the shaper; `critical` implies `pierce`.
- `pierce: true` bypasses burst shaping and any deferral, exactly like the existing `Call Me` +
  Critical pierce set (`presence_rules.py`, the session-registry quiet window).

**Append-only jsonl, not a JSON list, and this is deliberate.** `control-queue.json` is
load→append→save, which is a lost-update race the moment there are two writers. The Mouth has a
dozen writers by definition. A single sub-4 KB line append is the one multi-writer primitive that is
safe on one machine without a lock.

### 3.3 The assertions log — `seneschal.assertion/1`

`state/assertions.jsonl`, append-only:

```json
{
  "schema": "seneschal.assertion/1",
  "at": "2026-01-01T19:52:14Z",
  "surface": "telegram",
  "door": "queue",
  "kind": "job",
  "origin_hand": {"source": "daemon", "session_id": "…"},
  "text": "…as delivered, after annotation and shaping…",
  "item_ids": ["20260101-201512-a3f1"],
  "superseded": [],
  "delivered": true
}
```

`text` is **what the owner actually received**, post-annotation and post-digest — not the producer's
draft. The log answers "what does the owner believe", and they believe the delivered wording.

**Why not extend `telegram-message-map.json`:** it is keyed by Telegram `message_id`, exists to give an
inbound reaction its context, is capped and swept for that purpose, and covers exactly one surface. A
cross-surface, cross-door, cross-producer record of what has been said is a different artifact with a
different retention rule. Reusing it would couple identity to Telegram's id space — and the map would
still miss every Discord push, every call, and every desktop turn.

**Retention:** one `RETENTION_DAYS` constant (30, matching `session-distillations.jsonl` and the
job-origin mailbox), pruned in Dream (`mouth.py prune --days 30`). Compaction into the RAG index is
deliberately **not** part of this spec — §8.

### 3.4 What the dispatcher does — four mechanical rules, no model

This is the point on which the design lives or dies: **none of this needs an LLM.** A supervisor model
reviewing every outbound would add cost, latency, and re-centralize cognition in exactly the component
whose failure posture must be fail-open.

1. **FIFO per surface.** One clock. The owner never gets B-then-A when A-then-B happened.
2. **Staleness annotation.** If `now - observed_at > STALE_AFTER` (default **10 min**), the delivered
   text is prefixed with an as-of stamp in the assistant's voice — *"(from 14:42)"* — rather than
   presented as news. If `expires_at` has passed, the item is dropped and recorded in assertions as
   `delivered: false` with a reason. **Nothing vanishes unrecorded.**
3. **Supersede.** Within the pending set, a newer item with the same `supersede_key` replaces the
   older; the older is listed in the survivor's `superseded[]`. Use for genuinely re-stated facts —
   a deploy-health alert re-firing, a job's retry-pending → failed transition.
4. **Burst shaping.** ≥ `BURST_N` (**3**) shapeable items for one surface inside `BURST_WINDOW`
   (**60 s**) collapse into one digest. `pierce` / `critical` items never collapse. **Reminders never
   collapse — see §3.5.**

### 3.5 The reminder carve-out — a standing decision that burst shaping must not break

It is tempting to read burst shaping as a *generalization* of the reminders-stagger rule. **It inverts
it.** The reminders policy (`seneschal/references/reminders-policy.md`) is: **one reminder per push
nudge, always; multi-item slots stagger apart.** Only a *pull* ("what's open?") may be a list. So:

- `kind: "reminder"` is **never** merged into a digest, never merged with another reminder, and never
  reordered relative to its own stagger. The dispatcher passes it through.
- Burst shaping applies to `job | alert | nudge | archon` only.

Named here rather than left to implementation because a shaper that merged two reminders would look
like a feature and be a regression against a decision the reminders policy already makes.

---

## 4. Ordering, delivery, and failure

### 4.1 Where the drain runs

`presence.py`'s scheduler tick (~5 s), immediately **after** job reconciliation — so a job that just
went terminal enqueues and drains in the same pass, and the push is no later than a direct send would
be. When the daemon is down, items accumulate and drain on the next boot, annotated stale. That is the
whole point of a file queue: **the daemon dying costs the assistant its cadence, not its memory or its
voice.** (This call site is part of the pending daemon wiring.)

### 4.2 At-least-once, deduped by the assertions log

A cursor plus a crash means a possible double-send. For owner-bound speech a duplicate is noise and a
drop can be a missed critical reminder, so the choice is the same one `jobs.py` already made:
**fail-closed, at-least-once.** The dispatcher never rewrites the queue file (you cannot safely
rewrite a jsonl line); terminal state is recorded by appending a `state` row for the same id — the
last word for an id wins — and delivered ids are recorded in assertions. Exactly-once in practice, no
rewrite, and a mid-drain crash costs at most a repeat.

### 4.3 Fail-open on everything that is not delivery

A malformed queue line is skipped and logged, never fatal. An unreadable assertions log downgrades a
hand to plain behaviour (speak without the tail) — it must never block speech. The one deliberate
fail-**closed** point is the landed-send stamp, inherited from `jobs.py`.

---

## 5. The exceptions, and why they stay exceptions

### 5.1 The bypass mouths keep bypassing

`seneschald-control.ps1` and `cockpit/breakglass/supervisor.py` exist **to speak when the daemon is
dead.** Routing them through a daemon-drained queue would make the one message that matters most —
*"the daemon is down"* — depend on the thing that is down. They keep sending directly. They append to
assertions **best-effort** (via the `mouth.py record` CLI, or a lazy guarded import), and a failed
append costs the append, never the alert.

This is the same reasoning that made break-glass a third, deliberately stdlib-only process: **the
component that reports a failure must not share a dependency with the thing that failed.**

### 5.2 Replies

Covered in §3.1. Door B, direct, unchanged, logged.

### 5.3 The daemon's own crash-loop alert

Fires as the daemon is deciding not to respawn itself. Direct, for §5.1's reason.

### 5.4 Calls

A `Call Me` is `pierce` by definition and never shapes or digests. It queues only so that it is
*logged* and *ordered* with respect to other speech; the escalation loop stays Worker-side, untouched.

---

## 6. Worked example — a stale burst, run through the Mouth

Five job-terminal pushes — four failed delegations and one budget-governor refusal — delivered over
four minutes about facts observed ten to fifteen minutes earlier, each in alert tone, after the
assistant had already explained all five in-thread.

With the Mouth: five items, `kind: "job"`, `surface: "telegram"`, `observed_at` = each job's
`ended_at`, arriving inside one 60 s window. Rule 4 collapses them to one digest; rule 2 sees every
`observed_at` older than 10 minutes and stamps it. The owner receives **one** message, once:

> (from 14:35–14:45) Four delegation attempts and one budget refusal have logged their completion
> pushes. All five were the failures I walked you through; nothing new.

Five alert-toned interruptions become one honest line. **Note what did the work: not intelligence —
`observed_at` and a 60-second window.**

And note what the Mouth would *not* have fixed: the failures themselves, or the tone ladder in
`cancel-attribution-spec.md`. That spec decides how a single push should sound; the Mouth decides how
many pushes there are and in what order. The two compose.

---

## 7. Phases — each shippable alone

### Phase 0 — the assertions log, and nothing else — module BUILT, send-site wiring pending

Add `mouth.record_assertion()`; call it from every send site in §2.1 **after a landed send**,
including both doors and both bypass mouths. **Zero behaviour change** — no queue, no shaping, no
reordering.

Why first: it is the artifact every later step reads, it is unable to break anything, and it
**measures the problem before the fix** — how often bursts actually occur, how stale delivery actually
is, whether supersede has any real work to do.

It is written **in code, at the send sites**, not asked for in a prompt. A prompt-side-only logging
contract is the shape that reliably produces zero rows; code at the call site is the shape that
doesn't.

#### 7.0.1 What ships, and where each producer records

`seneschal/scripts/mouth.py` (stdlib): `record_assertion()`, `read_assertions()`, `prune()`,
`origin_hand()`, and a `record` / `tail` / `prune` CLI. Coverage: `seneschal/scripts/test_mouth.py`.

The intended recording point for each §2.1 producer, for the wiring still to land:

| §2.1 producer | Recorded at |
|---|---|
| `sentinel.send_telegram` / `send_discord` / `send_call` / `_deliver_reminder` | the reminder check, on the landed branch — the one frame that knows the `kind` *and* has the landed-check for all three channels |
| `presence.deliver_reply` (Door B: replies, the wake line, the backlog ack, the dead-letter notice, the spawn-fallback alert) | `deliver_reply`, both the helper branch and the cockpit branch |
| `jobs.reconcile(notify=…)` | the daemon's job-notify wrapper, which passes `kind="job"` into `deliver_reply` — that wrapper *is* jobs' mouth |
| the crash-loop alert | the crash-loop guard |
| the governor alert | the governor's per-turn meter |
| `seneschald-control.ps1` deploy-health + presence alerts (bypass) | the `mouth.py record` CLI |
| break-glass rung notifications (bypass) | the supervisor's Telegram helper; the `mouth` import is lazy + guarded so the rescuer keeps no dependency on what it may have to report as broken |
| archon pushes | the archon's own send helper |

Decisions the module encodes, which the design had left implicit:

- **Every phase-0 row is `door: "direct"`**, because there is no queue yet. The field is written
  anyway so a phase-1 reader never has to special-case rows written before the queue existed.
- **`kind` gains `reply`, `question` and `unknown`.** §3.2's list types a *queue item*, and the queue
  is Door A only; the log spans both doors, so Door B needs a name. Vocabularies are validated
  **loosely** — an unrecognised value is recorded verbatim, never rejected, because a producer that
  speaks must not be blocked by this module's opinion about labels.
- **`origin_hand` carries `speaker` alongside `source`.** `source` reads `build` for a warm turn
  (§9.4); `speaker` is the call site's own label for itself (`daemon` / `sentinel` / `watchdog` /
  `breakglass` / an archon), which is the field to trust. `source` is registry enrichment, and its
  lookup is doubly guarded so it can never cost the row.
- **A `--stub-send` reply records nothing.** The offline harness reached nobody, and `delivered: true`
  would be a lie in the one file that exists to be trusted.
- **Email is not recorded.** It is not one of the Mouth's four surfaces.
- **Retention is wired**, per §3.3: `RETENTION_DAYS = 30`, `mouth.py prune --days 30`, run by Dream.
  A row with a missing or unparseable `at` is **kept** — this GC must never be the thing that loses a
  record of something the assistant said.

### Phase 1 — the queue and a dumb dispatcher — module BUILT, drain wiring pending

`mouth.enqueue()` / `mouth.drain()`; the drain wired into the scheduler tick; producers migrated from
`sentinel.send_telegram` to `mouth.enqueue`. **FIFO and staleness annotation only** — no supersede, no
digest. Reminders and calls pass straight through (`PASSTHROUGH_KINDS`). The migration is mechanical
and can land producer by producer; an unmigrated producer keeps working exactly as it does now.

What ships: `state/outbound.jsonl` + `enqueue` / `read_outbound` / `pending` / `staleness_prefix` /
`drain`, with `_append_line` shared with the assertions log since both are jsonl for the same reason.
*Terminal state is recorded by appending a `state` row for the same id*, never by rewriting the file.

**The first producer to migrate should be the safest thing to be wrong about** — an unsolicited,
at-most-once-per-boot nudge: it belongs in the queue by definition, it is not a reminder (so §3.5 is
not in play), and it is not a reply (so the poison-pill guard is not involved). On a healthy boot
nothing changes for the owner; when Telegram is down, the nudge **waits and goes out when the network
recovers** instead of being logged once and lost — the entire argument for the door, made concrete in
one message. Tests for a migrated producer should assert the *property* (the owner gets it once),
end-to-end through enqueue-then-drain, rather than the mechanism.

**Deliberately still absent, so phase 2 has to add them on purpose:** there is no digest function,
and a test asserts that five queued reminders produce five pushes rather than one digest.

### Phase 2 — supersede and burst shaping

Gated on phase 0's data. Includes the §3.5 reminder carve-out and the digest wording, which needs the
owner's eye (§10).

### Phase 3 — orientation reads the tail

The assertions tail (last N, or last 2 h) joins `presence.py`'s `GROUNDING` and the orientation bundle
every surface reads, including the `/assistant` command's orientation. Until that lands, a session
speaks without the tail, which is plain behaviour today.

Then, and only then, the drafting freshness rule generalizes: **an outbound drafted more than a few
minutes ago re-checks the assertions tail before sending**, and a changed fact gets the correction
voice — *"earlier I said X; that's changed"* — rather than a silent contradiction.

---

## 8. Deliberately not built

- **Contradiction detection.** Semantic contradiction between two hands is undecidable in general and
  unaffordable to approximate per-message. Ordering + awareness + graceful correction is the whole
  offer. A local-model advisory pass, shadow-first and router-shaped, is a *later* option gated on the
  assertions log showing contradictions actually happening.
- **Any model in the dispatch path.** §3.4.
- **A message bus, an actor framework, mailbox-per-hand.** Mailbox-per-hand fragments the mouth, which
  is the opposite of the goal.
- **CRDTs.** Permanently. They merge concurrent structural edits under partition; there is one machine
  and no partition, and the conflicts that matter are semantic. A CRDT can merge two edits to
  `carry-over.md`; it cannot merge *"the PR is green"* with *"the PR is red"*.
- **Global write locks on `state/`.**
- **Any change to the completion-push guarantee.** Every terminal job still notifies exactly once,
  retried until it lands. The Mouth changes the *shape and timing* of that notification, never whether
  it happens.
- **RAG compaction of assertions**, cross-machine addressing, a cockpit surface for the queue, and the
  promises ledger — the last is its own spec (`promises-ledger-spec.md`).

---

## 9. Where reading the code contradicted the obvious design

1. **Speech is not one door.** A turn reply is synchronous, its landed-boolean feeds the poison-pill
   guard, and queueing it would break the guard and the turn. Two doors, one log (§3.1) — and this is
   what makes the change cheap rather than a rewrite of the conversational path.
2. **Burst shaping is not a generalization of the reminders-stagger rule. That rule says the
   opposite** (§3.5). Generalizing it as written would have broken a standing decision while claiming
   to honour it.
3. **The daemon is not "already the single mouth for most of one surface."** There are a dozen
   producers, and two of them *must* stay independent (§5.1).
4. **`origin_hand.source` reads `build` for a warm turn**, not `daemon` — the warm session is a Claude
   Code session and `session_stamp.py` exempts nobody. Hence `speaker` (§7.0.1).

---

## 10. Open questions for the owner

1. **Staleness threshold and wording.** 10 minutes is a guess. And *"(from 14:42)"* is one phrasing
   for an as-of stamp — it should sound like the persona, not like a log line.
2. **Digest shape.** §3.5 pins reminders. Does the same *never batch* instinct extend to job
   completions, or is one collapsed line the improvement it looks like?
3. **Should a superseded item ever be visible?** A single line naming what was dropped may beat
   silence — but it is one more thing to read.
4. **`STALE_AFTER` vs quiet hours.** A push held overnight by a deferral is stale by construction come
   morning. Annotate it, or is a deferred nudge exempt because the owner knows it was held?

## Router entry

**Router status:** phases 0-1 module BUILT (`mouth.py`); send-site recording and the daemon drain
wiring pending; phases 2-3 unbuilt.

**What it decides:** One ordered, staleness-aware channel per surface; `state/assertions.jsonl` as the
record of what the owner was actually told.
