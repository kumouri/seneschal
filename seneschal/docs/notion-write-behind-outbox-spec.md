# Notion write-behind outbox — design (durability)

**Status:** `PARTIAL(§11 steps 1-4, 6 and 7 BUILT — store, CLI, in-turn drain, §6.1 supersession, §6.2
retraction, dead-letter classes, the task_status op; the daemon's drain + backlog alarm (§7, §9 (a′)/(a‴))
land with the presence.py port)` — approved direction (owner sign-off) · **Owner:** the assistant ·
**Scope:** a durable local write queue for the assistant's **act-low Notion writes** (reminder acks,
med-intake rows, the register → Tasks status projection; run-log finalize in phase 2), plus the flush
mechanism that lands them. **Decisions locked (§10): flush = (a) opportunistic first; store = sqlite;
v1 scope = acks + med-logs; happy-path = belt-and-suspenders (direct MCP write + enqueue).** Companion:
`asyncio-daemon-design.md` (where the drainer lives), `../references/memory.md` (the local-first
durability patterns this generalizes), `../references/notion-rate-limits.md` (why writes rarely 429).

> ### ⟶ READ THIS BEFORE TELLING THE OWNER AN ACK LANDED
>
> **Nothing in this subsystem writes to Notion. `outbox_common.py` and `outbox.py` have no Notion
> client, no credential and no network call at all — by design, and both say so in their own module
> docstrings.** An enqueue makes a write **durable**; it does not make it **happen**. Every entry
> reaches Notion by exactly one route: **an LLM turn that holds the Notion MCP tools replays it and
> then marks it `done`.** There are two such turns and no third —
>
> 1. **A live turn draining in-turn** (`modes/chat.md` — option (a), preferred, cheapest, because it
>    is already holding the row id); and
> 2. **the daemon's gated backstop**, `presence.maybe_drain_outbox`, which spawns a fire-and-forget
>    headless `claude -p` only when its gates pass (§9 (a′)): the queue is non-empty, the oldest entry
>    is stale enough (longer while a live `/assistant` session has first refusal — §9 (a‴)), the spawn
>    cadence allows it, no warm turn or other headless child is running, the active store backend is
>    `notion`, and **Notion MCP is actually wired** (`active_mcp_configs`).
>
> **So an entry can sit `pending` and nothing is broken.** If neither flusher runs — most concretely,
> if Notion MCP is not configured on this host — the daemon spawns *nothing*, and the only thing that
> will ever say so is §7's backlog alarm at ~3 h. The entry is not lost; it is also not in Notion.
> **Those are different facts.**
>
> **The case that makes this bite: `scripts/ack.py` never writes Notion.** A Telegram 👍 and `ack.py`
> at the command line both dequeue and journal to this outbox — and **neither touches the ⏰ row.** On
> that path the outbox is not a backstop behind a direct write (§10 decision 4); it is the **only**
> route, and the Notion write is still owed by whichever turn drains next. The daemon's reaction line
> to the warm session says so explicitly, because a line asserting *"⏰ row marked Done"* is exactly
> what stops a warm session from doing the flush option (a) depends on.

> **Why the flush needs an owner.** (a) originally shipped without the two things meant to hold it
> up: the *drain* was a sentence in `modes/chat.md` — *"opportunistically drain any backlog at a
> natural point in a turn"* — that nothing else ever ran, and §7's backlog surfacing was never built,
> so the pending-depth metric §9 made the whole (a)→(b) escalation conditional on **had no sensor**. A
> queue in that state accumulates un-landed `ack_reminder` entries for a day or more, unremarked, while
> a 👍 on a reminder nudge reaches the local ledger and this queue and never touches the ⏰ row.
>
> The fix has three parts: **§6.1** (the supersession guard — a stale queued ack may never overwrite a
> newer one, enforced inside `pull`), **§7** (the backlog alarm + the `!status` line), and **§9(a′)**
> (the daemon owns the flush). Details in each section.

---

## Backend scope — this is a Notion-backend component

The outbox exists because **Notion is remote and rate-limited**: a write is a network call that can fail
after the assistant has already told the owner it happened. It is therefore enabled **only when the
active store backend is `notion`** (see `../store/config.json`, seeded from `config.example.json`, and
`../store/README.md`). The filesystem backends (`obsidian` / `markdown`) write locally and atomically —
there is no network to
outlive, so their act-low writes never route through the outbox; the direct local path in
`scripts/reminders_acks.py` (plus the store files themselves) is their durability story. The module and
file names stay Notion-specific (`outbox.py`, `state/notion-outbox.sqlite`) on purpose — honesty over
false generality; a future remote backend with the same failure shape would get its own enablement
decision, not a silent reuse.

---

## 1. Why — durability first

The assistant tells the owner something is *done, logged, marked off*. That claim is only true if the
underlying Notion write actually landed. Today it can silently not:

- **The warm session is volatile.** A chat ack (`Status = Done`, `Last Acknowledged = today`,
  `Consecutive Misses = 0`, untick `Ack`) is written by the LLM turn calling `notion-update-page`. If that
  call errors — Notion unreachable, a `collection_router_upstream_429`, the daemon's warm session lacking
  the connector, or the session winding down / the box rebooting mid-turn — the write is **lost**. The
  session is gone; nothing retries it.
- **This has already bitten us.** A live ops-watch entry in `state/carry-over.md` — *"Chat→Notion ack
  write-through gap"* — recorded **four chat-acks in a single day** that reached only `state/acks.json`
  and never flipped the ⏰ row. The fire-gate held (the owner wasn't re-nudged, because `acks.json` caught
  it), but Notion — the **system of record** — silently desynced. The next Brief/Wrap reads Notion, so a
  lost ack quietly corrupts "done today."
- **Same exposure for every act-low write:** med-intake rows (the recurring-medication auto-log), Run Log
  entries, reminder status flips. Each is a fire-and-forget MCP call with no retry.

The house rule (persona operating principle #5, "honest about state"; `modes/chat.md` rule 6) is *never
claim a write you didn't make*. Right now honoring it means the assistant has to **park the item in
carry-over by hand** and hope a later run replays it. That's a manual, lossy outbox. This spec makes it a
real one.

**The fix (one line):** every act-low Notion write is **journaled to a durable local store first** (it
survives a reboot), **then flushed to Notion** — idempotently, in order, retried until it lands.

**And here is the sentence to be careful with.** The assistant can truthfully say **"recorded"** the
instant it is journaled, because the journal guarantees the write **will not be lost**. It may **not**
say "done", "logged in Notion" or "marked off" on the strength of the journal alone, because the
journal does not perform the write — a flusher does (see the box at the top), and until one runs the
row in Notion is **unchanged**. "Durable" and "landed" are two states, and the outbox only ever puts an
entry in the first. This distinction is why *never claim a write you didn't make* survives the
introduction of a queue rather than being dissolved by it.

**Rate-limit relief is a side benefit, not the pitch.** A queue naturally serializes writes and lets us
back off on a 429 instead of dropping — but writes already route *around* the throttle we actually hit
(`collection_router_upstream_429` hits the SQL query path, not `notion-update-page`;
`notion-rate-limits.md`). So this is a **durability** project. Throttle behavior barely changes.

## 2. What it is

A small **write-behind queue** (`state/notion-outbox.*`). The assistant's act-low write path becomes two
steps:

1. **Journal** the *intent* of the write to the outbox (durable, atomic, local). — this is the guarantee.
2. **Flush**: replay the entry against Notion; mark it `done` only when Notion confirms.

Step 1 is the durability contract and is **identical regardless of how we flush**. Step 2 — *what triggers
the flush and how it talks to Notion* — is the open fork in §9. Framing it this way de-risks the decision:
**nothing is ever lost either way**; the fork only trades *flush latency* against *new surface area*.

## 3. Non-goals

- **Not for ask-high / outbound writes.** Email, Slack, calendar RSVPs already have a durability mechanism
  built for them — `pending-approvals.json` + the Notion carry-over — because they are **human-gated and
  single-shot**. You don't auto-retry a half-sent email; a double-send is worse than a miss. The outbox is
  **only for already-decided, act-low, idempotent Notion writes.** The two systems stay separate (§8).
- **Not a Notion read cache.** Reads never queue. This is write-side only.
- **Not a general job queue.** Closed, small set of Notion write intents (§5), not arbitrary work.
- **No new runtime dependency.** Stdlib only — `sqlite3` + (for option b) `urllib`. Same house style as
  the rest of `scripts/`; the sanctioned-dependency shortlist stays `websockets` + `tzdata`.
- **No billing-model change.** If the flush ever spawns `claude` (option b's headless variant), it's the
  subscription CLI with `ANTHROPIC_API_KEY` scrubbed, exactly like every other spawn.

## 4. What routes through the outbox (and what stays direct)

| Write | Route | Idempotency shape |
|---|---|---|
| **Reminder ack** — ⏰ row `Status`/`Last Acknowledged`/`Consecutive Misses`, untick `Ack` | **Outbox** | Naturally idempotent — same target state (`ack:<row>:<date>`) |
| **Med-intake row** — create a row in the owner's med-intake log collection | **Outbox** | **Create** — needs an enqueue-time key (§6) |
| **Run Log finalize** — the 🧭 row's status + counts + `Actions Summary`/`Carry-Over` at run end | **Outbox** (phase 2 — see OQ4) | Update by row id — idempotent |
| **Reminder status** — non-ack ⏰ flips the assistant makes act-low | **Outbox** | Update by row id — idempotent |
| **Tasks `Status` projection** — a register status change forwarded to its Notion Tasks row (`loops.py forward_task_status`, `register-notion-projection-spec.md`) | **Outbox** | Update by page id — one key per `(page, target status)` |
| Run Log **create + per-phase body appends** (the incremental trace) | **Direct** (best-effort) | Already mirrored to `state/run-log.md`; local trace survives regardless |
| Any **read** (`notion-fetch`, `query-data-sources`, `search`) | **Direct** | n/a |
| **Ask-high** sends (email/Slack/calendar) & flips (linked Task/Goal → Done, archive) | **Direct**, via the approval loop | n/a — human-gated, single-shot (§3) |

The ack path is the marquee case, and **it has two shapes that behave differently — do not read the
first and assume the second.**

- **The chat write-through** (`modes/chat.md`). An LLM turn is present, so it does the direct
  `notion-update-page` **and** enqueues: the belt-and-suspenders of §10 decision 4, where the outbox
  genuinely is a pure failure backstop.
- **The automated acks — a Telegram 👍, `scripts/ack.py` at the command line.** The **front door is
  `scripts/ack.py`**, which resolves the owner's words to ⏰ rows, journals via `outbox.py ack` and
  dequeues. **`ack.py` never writes Notion** — it says so in its own docstring — and neither does
  `reminders_dequeue.py`, and neither does `presence.ack_reminder_by_reaction`, which is just those
  calls. **On this path there is no direct write to back up, so the outbox is the only route to the ⏰
  row and the write is still owed.**

Both shapes already called `reminders_dequeue.py` (which records `acks.json`); each gains **one more
call** — enqueue an outbox entry — so the ack is now durable against Notion, not just against the
fire-gate. (See §8 for why both still fire.)

## 5. The store — schema & location

**Location:** `state/notion-outbox.sqlite` (gitignored, like every runtime file in `state/`). Recommend
**sqlite over JSONL** — precedent is `rag-index.sqlite`, and an outbox is exactly sqlite's job: per-entry
status transitions (`UPDATE … WHERE id`), a **`UNIQUE` idempotency-key index that gives dedup for free**,
indexed "what's pending" scans, and WAL-mode concurrency across the three accessors (the enqueuing LLM
turn, the drainer, the status CLI) without hand-rolling the `queue_lock` dance `acks.json` needs. JSONL is
the simpler-but-weaker fallback (see OQ2).

**One table, intent-level rows** (not raw API payloads — see the note below):

```sql
CREATE TABLE outbox (
    id               TEXT PRIMARY KEY,   -- enqueue-time uuid4; also the FIFO tiebreak with created_at
    idempotency_key  TEXT NOT NULL UNIQUE, -- dedup: a repeat enqueue is a silent no-op (see §6)
    op               TEXT NOT NULL,      -- ack_reminder | med_log | run_log_finalize | reminder_status
                                         --   | task_status
    target_kind      TEXT NOT NULL,      -- 'page' (update) | 'db' (create-in)
    target_id        TEXT NOT NULL,      -- ⏰/🧭 page id, or the collection id to create in
    payload          TEXT NOT NULL,      -- JSON: the *logical* fields for this op (below)
    status           TEXT NOT NULL,      -- pending | inflight | done | failed(=dead-letter)
    attempts         INTEGER NOT NULL DEFAULT 0,
    not_before       TEXT,               -- ISO-UTC backoff gate; NULL = eligible now
    created_at       TEXT NOT NULL,      -- ISO-UTC
    last_attempt_at  TEXT,
    last_error       TEXT,               -- trimmed message from the most recent failure
    notion_page_id   TEXT,               -- written back on success (esp. for creates); observability
    resolution       TEXT,               -- NULL = it WROTE. 'superseded' (§6.1) / 'retracted' (§6.2)
                                         --   = resolved WITHOUT a Notion write; never counts as landed
    revived_from     TEXT,               -- the previous created_at, kept when a revival resets it (§6)
    dead_letter_kind TEXT                -- caller_error_suspected | permanent | retries_exhausted |
                                         --   unclassified (§6)
);
CREATE INDEX outbox_ready ON outbox(status, not_before);
```

**Intent-level payloads, not MCP/REST payloads — this is deliberate.** The two candidate flushers speak
*different* Notion dialects: the hosted MCP's `notion-update-page` takes a simplified shape, while a raw
REST client takes canonical property objects. If we stored a pre-baked call, the store would be welded to
one flusher and couldn't survive the §9 fork — or a later switch between the two. So each row stores the
**logical intent**, and whichever flusher runs translates it at the edge. Examples:

```json
// op: ack_reminder   → idempotency_key "ack:<norm_row_id>:2026-07-14"
{ "reminder_id": "00000000-…", "status": "Done", "last_acknowledged": "2026-07-14",
  "consecutive_misses": 0, "untick_ack": true }

// op: med_log        → idempotency_key "medlog:<intent_uuid>"  (a create — key minted at enqueue)
{ "name": "Medication A", "dose_mg": 200, "taken_at": "2026-07-14T08:05:00-05:00", "set": "usual-am" }

// op: run_log_finalize → idempotency_key "runlog-final:<row_id>"
{ "run_log_id": "…", "run_status": "Success", "items_surfaced": 3, "actions_taken": 2,
  "drafts_held": 0, "actions_summary": "…", "carry_over": "…", "issues": "" }

// op: task_status    → idempotency_key "task_status:<norm_page_id>:<status>" (a PAGE update on the
//                      Tasks row; enqueued only by loops.py forward_task_status)
{ "status": "Done", "completed_date": "2026-07-14" }
```

A `'db'` target (a create) carries a Notion **data-source** id (a `collection://…` URL), never a
`database_id`: the flusher's `notion-create-pages` call uses `parent: {"data_source_id": target_id}`.
Passing a data-source id as a `database_id` parent 404s every time — the wrong object type, not a
missing target — so the drain prompt names the parent shape explicitly.

The three trailing columns are added by an idempotent, additive migration in `connect()`: an older store
upgrades in place and every existing row reads `NULL` (the ordinary path).

The closed `op` set keeps the flusher a small, testable dispatch — adding a write type is a new `op` +
its translation, nothing more.

## 6. Idempotency, ordering, retry, failure

**Idempotency — the crux, because a replay after a restart must not double-write.**

- **Updates (acks, status flips, run-log finalize) are naturally idempotent**: replaying "set this row to
  `Done`/these counts" converges to the same state. The idempotency key (`ack:<row>:<local_date>`,
  `runlog-final:<row_id>`) also means a *repeat enqueue* (the owner acks twice) collapses to one row via the
  `UNIQUE` constraint — the second enqueue is a no-op `INSERT OR IGNORE`.
- **Creates (med-intake rows) are the hard case** — Notion's create API isn't idempotent server-side, so a
  crash *after* Notion creates the row but *before* we mark the entry `done` would double-create on replay.
  Mitigation, in order of appetite (**OQ3**):
  1. **Minimal-window write-back (recommended default).** The drainer marks `done` and stores
     `notion_page_id` in the *same local transaction* immediately after the create returns, before touching
     the next entry. The crash window shrinks to a few milliseconds. A rare duplicate med row is low-harm
     and easy to spot. Ship this; revisit only if it ever bites.
  2. **Notion-side idempotency marker.** Stamp the `idempotency_key` into a text property on the created
     row; the drainer does a keyed `query` *before* create and skips if present. Fully exactly-once, but
     costs a read (the 429-prone SQL path) and a schema touch per create-target DB. Heavier than the risk
     warrants today.

**Revival.** Re-enqueueing a key whose entry is `failed` **or** `retracted` (§6.2) **revives** it: back to
`pending`, attempts and backoff cleared, the new payload stored — a fresh intent re-arms the key rather
than being swallowed by it. **A revival also resets `created_at` to the revival instant** (the old value
is kept in `revived_from`): `stats()`'s `oldest_pending_age_sec` — what §7's backlog alarm compares
against its ~3 h threshold — reads `created_at`, so a revived entry that inherited its dead
predecessor's timestamp would report a seconds-old intent as hours stale and fire a false nudge.
(`last_attempt_at` is still left from the previous incarnation; it is history, not an age input, but a
row reading `attempts: 0` beside an older `last_attempt_at` is a revived one.)

**Ordering.** The drainer is a **single consumer** processing **FIFO** (`ORDER BY created_at, id`). That
gives per-target order for free (two acks to one row apply oldest-first; run-log finalize lands after its
creates). We do **not** need global strict ordering — only *per-target* — and FIFO is a superset of that.

**Retry / backoff.** Per entry: exponential backoff with jitter, honoring `Retry-After` when present
(`not_before` gates the next attempt), `attempts` incremented and persisted **before** each try (poison-pill
safety, mirroring daemon invariant #2). After `MAX_ATTEMPTS` (propose **8**, ~capped at a few hours of
backoff) the entry goes **`failed` (dead-letter)** — it stays in the table, stops retrying, and **surfaces**
(§7). A dead-letter never blocks the queue: the drainer skips `failed`/`not_before`-future rows and keeps
draining the rest, so one poisoned entry can't wedge everything (unlike naive head-of-line FIFO).

**Conflict / failure classes.**
- *Transient* (429, 5xx, network, timeout): backoff + retry. Never dead-letter on these alone.
- *Permanent-shaped 4xx* (404, 400, 403, …): dead-letter immediately — retrying won't help; surface
  for a human. `last_error` records why.
- *Ambiguous* (create timed out — did it land?): treated as transient → retry, and covered by the create
  idempotency choice above. This is exactly why OQ3 matters.

**Not every 4xx is the TARGET's fault.** A 4xx is just as often proof the *caller* sent the wrong id or
shape, and the flush is done by a headless LLM turn expressly forbidden from querying Notion to tell the
two apart (the query path is the one that gets rate-limited). The data-source-id-as-`database_id`
parent (§5) is the concrete case: it 404s every single time, and a batch of creates dead-lettered that
way reads as an unfixable, permanent Notion-side condition when it is a code bug. So `mark_failed`
takes an optional `status_code` (`outbox_common.classify_dead_letter`, CLI `mark --dead-letter
--status-code <code>`): 400/404 classify as `caller_error_suspected` — a suspected code bug, not a
confirmed-permanent target state; any other code is `permanent`; an exhausted retry budget is
`retries_exhausted`; and an omitted status code stays `unclassified` rather than being silently
promoted to `permanent`. All of them still stop retrying (the "a dead-letter never wedges the queue"
invariant is untouched); only the framing differs — `outbox.py status` tags a caller-shaped 4xx
`⚠ LIKELY CALLER BUG` instead of blending it into the ordinary dead-letter list.

### 6.1 Supersession — the ordering hole FIFO does not close

FIFO makes two *queued* acks for one row converge. It says nothing about an ack that landed **outside the
queue** — and that is the ordinary case, because §8's belt-and-suspenders design has the turn write
directly to Notion and only *then* mark the entry done. If the direct write lands and the mark never runs
(session dies, turn errors, nobody drains), the queue keeps a **pending entry for a date that is no longer
the truth**.

The shape: pending `ack_reminder` entries dated yesterday while the ⏰ rows have since been acked by hand
for today. Replaying them in FIFO order writes yesterday's `Last Acknowledged` over today's — and that
field is what the re-fire and interval logic read, so the "repair" re-arms a reminder the owner already
answered. A drain that naively catches up is worse than no drain, which is why this guard ships with the
drainer.

**The rule.** Before any drainer is handed a dated-ack entry (`ack_reminder`, `reminder_status`), it is
compared against what is already known to be true for that row:

- the durable ack ledger `state/acks.json` (`reminders_acks` — normalized id → the row's most recent ack
  date), which both the chat and the reaction ack paths stamp; and
- this store's own **landed** entries for the same row (`outbox_common.latest_landed_ack`) — `done` with
  `resolution IS NULL`. The filter is the whitelist, not a blacklist: a `superseded` or `retracted`
  (§6.2) row never reached Notion, so counting its date as landed would be the same lie in miniature, and
  a future no-write resolution is excluded by default rather than by remembering to add it.

Strictly newer than the entry ⇒ the entry is **resolved without a write**: status `done`, `resolution =
'superseded'`, and a `last_error` naming the date that beat it, so the row answers "why did this never
reach Notion?" by itself. Equal dates replay (they converge, and letting them through keeps the guard from
swallowing a genuinely un-landed same-day ack). Creates (`med_log`) and `task_status` carry no ack date,
converge on nothing, and are never swept.

**Where it lives is the design decision.** The sweep runs inside **`outbox.py pull`** (and on its own as
`outbox.py resolve`), not in the drain loop's prose — so a drainer *cannot be handed* a stale entry, by
any drainer, including the in-turn one `modes/chat.md` documents. "The drainer should check first" is an
instruction; this is a guard.

**Fail-open in exactly one direction.** A missing or corrupt `acks.json` contributes nothing, so every
entry replays exactly as it would have before this existed. The ledger can only ever **stop a backwards
write** — it can never block a legitimate one. (The store's own doctrine is still fail-*closed*: nothing
here drops an entry or gives up on one.)

`latest_landed_ack` is also what the reaction-relay predicate (`reminders_acks.reaction_ack_fully_landed`)
reads to decide whether a 👍's write has actually happened — the one arm allowed to license silence,
because it counts only rows that **wrote**.

### 6.2 Retraction — the verb for "correctly cancelled"

**The gap.** The outbox had two terminal states and neither could say *this write must not happen*.
Marking such an entry `done` claims a Notion write that never occurred — the exact silent-success lie
this whole store exists to prevent. Dead-lettering it is honest about the absence but wrong about the
cause: `failed` means *permanent failure, come look*, and §7's sensor nudges the owner about it **once a
day, forever**. A standing false alarm is how a real dead-letter gets ignored, so the wrong resting place
doesn't just mislabel one row — it degrades the alarm for every future one.

**The case.** A report about breakfast auto-acks the lunch reminder; the ack is caught and pulled from
the queue before it flushes — correctly, with **zero attempts** — and a dead-letter was the only resting
place the store offered.

**The state.** A third `resolution` on the existing `done` status — not a fifth status, because the row
*is* resolved and the queue must not wedge on it:

- `resolution = 'retracted'`, `last_error = "retracted: <reason>"`.
- **The reason is required and must be non-empty** (`ValueError` otherwise). A retraction with no stated
  reason is indistinguishable from a write someone quietly dropped, and being the *honest* resting place
  is the entire value of the state.
- **A landed write cannot be retracted.** `done` with `resolution IS NULL` means Notion has it;
  relabelling it here would make the store disagree with the system of record. Refused loudly.
- A `pending` / `inflight` / `failed` entry can be. Retracting a dead-letter is the migration path for
  every row already mis-parked as one.
- `stats()` reports `retracted` separately (a **subset of `done`**, never counted as landed), so a
  cancellation stays visible without inflating the landed-write count, and §7's nudge stops firing.

**The regression it must not introduce, and the reason this needed code rather than a convention.**
Idempotency keys here are **day-scoped** — `ack:<row>:<date>`. If a retracted key stayed inert, the
retraction would silently swallow the *real* ack of the same row later the same day. So a retracted entry
**revives on a fresh enqueue**, exactly as a dead-letter does (§6 "Revival"). **A retraction cancels that
intent, not that key forever.** A caller reporting on an enqueue must therefore read `revived` as well as
`created`: `created: false, revived: true` is a fresh write sitting `pending`, not a same-day no-op.

The CLI is `outbox.py retract --id <id|prefix> --reason "<why>"`; the id may be the 8-character prefix
`status` prints, and an ambiguous prefix is refused rather than guessed.

## 7. Observability — seeing a stuck or failed entry

> **The gap that mattered most.** Originally only the first bullet existed, and it is a command someone
> has to think to run: nothing pushed, nothing was displayed, nothing counted — while §9 made the whole
> (a)→(b) escalation conditional on *"if the pending-depth metric ever shows real lag"*, a gate wired to
> a sensor that did not exist.

- **`python scripts/outbox.py status`** — counts by status, oldest-pending age, and the full dead-letter
  list with `op`/`target_id`/`last_error`/`attempts` (a caller-shaped 4xx tagged `⚠ LIKELY CALLER BUG`,
  §6). The one command to answer "is anything stuck?" A **retracted** count (§6.2) is appended to `done`
  only when there is one — the ordinary line must not grow a permanent `0 retracted` nobody reads.
- **A Telegram nudge when it stops draining** (`presence.maybe_nudge_outbox_backlog`): once per local
  day, through the **Mouth** with `supersede_key="outbox-backlog"`, when the oldest un-landed entry passes
  ~3 h (`OUTBOX_NUDGE_AGE_SEC`) **or** any dead-letter exists. Through the Mouth for the staleness prefix
  and the supersede key — a backlog stuck for a week must produce one current nudge, not seven stacked
  ones, because a nagging alarm is one that gets muted. `observed_at` is when the oldest entry was
  *enqueued*, not now, so a day-old backlog reads a day old. A dead-letter is nudged on sight regardless
  of age — it has **stopped** retrying, so waiting for it to age is waiting for nothing to happen — and
  each one gets its own named line (op + short id + error, capped with a `+N more` tail), because an
  aggregate "1 dead-letter" tells the owner *that* something needs attention and nothing about *what*.
  **This is why §6.2 exists**: a write cancelled on purpose has no honest home in `failed`, and parking
  one there converts this alarm into a daily false positive.
- **On the status surfaces.** `outbox_pending` / `outbox_dead` / `outbox_oldest_sec` ride the daemon's
  status snapshot (cache-fed on a ~60 s cadence, so the hot path does no disk I/O), which puts them in
  **`!status`** — answerable with no warm session, which is the point when the warm session is what
  stopped draining. The line renders **only when there is a backlog**: a permanently-visible "0 pending"
  is a gauge you learn to stop reading.
- **The observer fails open, the queue stays fail-closed.** The daemon's backlog reading treats any
  store failure as an empty queue — a broken store costs the reading, never the tick — but marks it
  `read_failed: True` (and records a `failures.jsonl` row), so a watchdog can tell a broken store from a
  genuinely quiet one. Nothing on the observer side ever drops an entry.
- The table is inspectable with any sqlite tool; `last_error`/`attempts`/`notion_page_id`/`resolution`/
  `dead_letter_kind` make each entry's history legible, including *why* an entry finished without a write.
- **Still not built, deliberately:** a per-drain-pass `state/metrics.jsonl` line, a carry-over ops-watch
  entry, and a morning-Brief listing. The alarm above is the load-bearing one — it *finds* the owner —
  and the other three are trend/reporting nice-to-haves. Named here so their absence stays a decision
  rather than a silence.

## 8. Relationship to `acks.json` / `reminders.json`

This **generalizes** the durability pattern `acks.json` pioneered — but does **not** absorb it.

- **`acks.json` is the fire-time gate, and must stay exactly what it is.** It answers *"was this row acked
  today?"* and is read by the **stdlib daemon** (`check_reminders`) to suppress an obsolete nudge. It is
  deliberately **Notion-independent and fail-open** (a broken ledger fires the nudge — a redundant buzz
  beats a missed Critical). The outbox answers a **different** question — *"has this ack been persisted to
  Notion yet?"* — and has the **opposite** failure doctrine (fail-*closed*: keep retrying until Notion
  confirms). Folding them would force one file to serve two contradictory safety models. Keep them
  separate.
- **They become siblings written at the same instant.** The ack path already records `acks.json` (gate);
  it now **also** enqueues an outbox entry (durable Notion write). Two cheap local writes, two jobs:

  | | `acks.json` | outbox |
  |---|---|---|
  | Question | acked today? | landed in Notion yet? |
  | Reader | stdlib daemon (fire gate) | the flusher |
  | On error | fail-**open** (fire anyway) | fail-**closed** (retry to completion) |
  | Lifetime | today only (daily reset) | until Notion confirms |

- **`reminders.json` is orthogonal** — it's the *nudge schedule* (when to buzz), not a Notion-write log.
  Untouched. The existing `reminders_dequeue.py` (cancel an obsolete nudge) and the new outbox enqueue are
  two effects of the same ack, aimed at two different queues.

**Belt-and-suspenders on the happy path (recommended — OQ5).** The ack turn keeps doing its **direct MCP
write** *and* enqueues. On the happy path the direct write lands in-turn (zero added latency) and the
drainer finds the entry already-satisfied → marks it `done` with no second write (idempotent). Only when
the direct write *fails* does the outbox actually earn its keep. This ships with **no behavior change on
the happy path** — the outbox is a pure backstop that activates on failure — which is the safest possible
rollout. (Pure write-behind — enqueue-only, drain does the sole write — is cleaner but changes ack latency
and timing; not worth it for acks.)

## 9. THE FORK — how the flush happens

> **DECIDED (the owner): (a) opportunistic flush first.** (b) stays a fast-follow if the
> pending-depth metric ever shows lag — the store/schema below are designed so adding it is purely
> additive (a drainer that reads the same table). The fork write-up is kept for that future call.
>
> **RESOLVED as (a) + (a′), not (b).** When lag arrived, the metric that was supposed to notice it had
> never been built, so the escalation gate never fired. What shipped is §9(a′) below: the daemon owns
> the flush, using the Notion MCP config it *already* threads into every spawn — so (a) stays first and
> preferred, and the endgame (b) still costs a new Notion integration token, a hand-written REST write
> client and a second property mapping, none of which were needed. **(b) remains the owner's call;
> nothing here forecloses it.**

Both options share §2–§8 verbatim: same store, same schema, same idempotency, same durability guarantee.
They differ only in **what triggers the flush** and **how the flusher talks to Notion**. Because the
journal-first step is the guarantee, **neither option can lose a write** — this is purely latency vs.
new surface.

### (a) Opportunistic flush — drain inside LLM turns (via MCP)

The outbox is drained by **the warm session / the next scheduled run**: at the top and tail of a turn,
the assistant runs a short drain — read pending entries, replay each via the **MCP tools it already has**
(`notion-update-page`, `notion-create-pages`), mark results. No new task, no new client, no new
credential.

- **+** Zero new surface. Reuses the exact Notion auth (OAuth MCP) and write path that exists today. The
  translation from intent→MCP is code the assistant already runs. Smallest, safest thing that works.
- **+** Naturally correct billing/identity — it's the assistant acting, in-session.
- **−** **Flush latency is coupled to activity.** If the box reboots while Notion is down and then *nothing
  happens* — no chat, no scheduled run — the entry sits (durably) until the next turn. In practice the
  daemon's scheduled slots (Brief/Wrap/Dream + 4 reminder slots) guarantee a drain at least a few times a
  day, and any chat message triggers one. So "stuck for hours" is possible only in a genuinely idle window
  with Notion also down — rare, and *still not lost*.
- **−** Drain work rides on the (more expensive) Opus turn, though it's cheap (a few writes).

### (b) Dedicated REST-drainer task — a supervised asyncio task draining on a cadence

A new `outbox_task` in the presence daemon (alongside `telegram_task`/`scheduler_task` etc.) drains on a
short cadence (say ~30–60 s) **independent of any chat activity**, writing to Notion through a **new
stdlib `notion_rest.py` client** (`urllib`) authenticated by a **Notion internal-integration token**.

- **+** **Tightest durability→flush latency**, always, regardless of chat. A reboot-with-Notion-down
  recovers on its own within a minute of Notion returning — no turn required.
- **+** Flush is **free** (no `claude` spawn) and never competes with a chat turn for the Opus session.
- **+** Writes route around the `collection_router_upstream_429` throttle, so a bare REST writer is
  well-behaved (`notion-rate-limits.md`).
- **−** **Real new surface:** a second Notion auth path (a new **integration token** + gitignored env,
  separate from the OAuth MCP), a hand-written REST write client + its intent→REST-property translation,
  and a new supervised task that must honor the daemon invariants (durable queue, poison-pill/dead-letter,
  graceful-restart quiesce). More to build, test, and secure.
- **−** Two write paths to keep in sync (the LLM's MCP writes elsewhere + the daemon's REST writes here)
  can drift in subtle property-mapping ways.
- **Variant (b′): task spawns a headless `claude` flush** instead of a REST client — cadence-independent
  like (b) but reuses the MCP auth like (a), at the cost of a **subscription turn per drain** (heavier,
  and wasteful when the queue is usually empty). Mentioned for completeness; I don't recommend it.
  *(This is what shipped, gated — see (a′) below. The "wasteful when the queue is usually empty"
  objection is real; the gate is what answers it.)*

### My read (yours to overrule)

**Ship (a) first; keep (b) on the table as a fast-follow.** (a) delivers the entire durability guarantee
with near-zero new surface, and its only weakness — flush latency in a rare idle+outage window — is
bounded by the scheduled slots and, crucially, **loses nothing**. The store/schema/§8 wiring are designed
so moving to (b) later is *purely additive* (add a drainer that reads the same table) — we don't have to
pick the endgame now to start being durable. If the pending-depth metric (§7) ever shows real lag, that's
the signal to add (b). **But this is a genuine fork and it's yours** — if you'd rather pay the surface cost
now for cadence-independent flushing, we build (b) from the start.

### (a′) The daemon owns the flush — (a) still goes first

A duty on `presence.py`'s existing **scheduler tick** (`_tend_outbox`), not a new supervised task: this is
the same "did the thing we started actually finish?" work as the slot reap and the job reconcile, and it
belongs beside them. **Notion backend only** — on a filesystem backend the tick does not open the store,
sweep, alarm or spawn. Two halves, deliberately unequal:

1. **The sweep — pure local, no network, no spawn.** §6.1's supersession pass, run on every check that
   finds a backlog. It costs a sqlite open and retires entries that must never be written. When the stuck
   rows have already been acked by hand for a later date, this half alone clears the queue and the
   expensive half never runs.
2. **The flush — a gated, fire-and-forget `claude -p`.** Replays what is left through the **Notion MCP
   config the daemon already threads into every spawn** (`--store-mcp` / the store-config-driven
   resolution). No new credential, no REST client, no second property mapping to drift from the first.
   The child's environment scrubs `ANTHROPIC_API_KEY` like every other spawn.

**The gates are the design.** Each one answers a specific objection:

| Gate | Default | Why |
|---|---|---|
| oldest un-landed entry older than `--outbox-stale-min` | 20 min | **Keeps (a) first.** A warm or scheduled turn that drains as designed means this never fires at all. |
| …or, while a `/assistant` session is live, older than `--outbox-live-stale-min` | 60 min | A live session gets first refusal — a **delay, not a veto** (§9 (a‴)). |
| `--outbox-drain-interval-min` since the last spawn | 30 min | A wedged queue must not fork a `claude` every tick forever. Stamped *before* the spawn, so a failing launch still burns its slot. |
| no warm turn mid-flight, no other headless child | — | The same single-headless-Notion-child rule the peek and the slots obey. |
| the active store backend is `notion` | — | The outbox is a Notion-backend component (see "Backend scope"). |
| Notion MCP actually wired | — | Without it the child cannot write and would burn a turn marking nothing. |
| the queue is non-empty | — | An empty queue costs one cached sqlite read per minute and spawns nothing. This is the answer to (b′)'s "wasteful" objection. |

**Rate limits.** The replay is `notion-update-page` **by page id** — the write path that routes around the
collection router (`../references/notion-rate-limits.md`) — and the drain prompt explicitly forbids
searching or querying for the row, because `target_id` *is* the row and the query path is the throttled
one. The prompt also names the create parent shape (§5) and requires the real HTTP status on every
`--dead-letter` (§6). One serialized child, `--limit 25`, on the cheap watch model when set: this is a
replay of already-decided writes, not a judgment.

**Off switches.** `--no-outbox-drain` keeps the sweep and the alarm but never spawns (pure option (a));
`--no-outbox` disables all three, which is for tests and offline runs. Entries stay durable under both —
they just go unflushed.

#### (a″) A pending queue inside a live turn is not a broken drain

**Both stale gates are doing their job when a live turn sees its own entries `pending`.** A live session
watching its own fresh entries sit `pending` is the daemon standing aside for it, as designed; it is easy
to misread as `_tend_outbox` failing, and "rescuing" the queue by hand-flushing it or filing a bug against
the backstop is the wrong response. **The fix for a pending queue inside a live turn is to finish the
write** — never to wait for the backstop. The only signal worth escalating on is the backstop's own
sensor: the ~3 h `OUTBOX_NUDGE_AGE_SEC` push, which fires regardless of session state. The symptom is
indistinguishable from a real stall **without reading `presence.log` for launch times**, which is why
this is recorded here rather than only in `modes/chat.md`'s short form.

#### (a‴) The `session_is_live` gate is a DELAY, not a veto

(a″) is right that a live turn's own pending entries are the daemon standing aside correctly — and wrong
if it concludes that putting the whole burden on the live session is therefore sound. As originally
built, `session_is_live` was a **veto**: while any `/assistant` session was live the daemon stood down on
every tick. The failure shape: one ack sits `pending` for half a day while a warm session is live across
most of that window; `acks.json` holds the ack the whole time, so nothing is lost locally, but Notion
disagrees with it for all those hours. The session the daemon stood down for never drains, and the only
thing that closes the loop is the once-a-day nudge — to the owner, who then has to hand it back.

**Why the deferral is not a coin flip.** The two halves are **asymmetric**: the daemon's stand-down is
automatic and fires on every tick without fail, while the session's pickup is discretionary and
unprompted — and a session only drains what it knows about. So the arrangement is structurally biased
toward nobody draining, and gets **more** biased the longer a session stays live.

**The veto was also defending against a race the store already closes.** `outbox_common.claim_ready`
claims each row with an atomic `UPDATE … SET status=INFLIGHT … WHERE id=? AND status=PENDING` and takes
only the rows where `rowcount == 1` — two concurrent drainers are safe, only one wins each row. So the
cost of an overlap is a **wasted duplicate pass**, never a double write. The veto gave up a guaranteed
drainer to avoid a collision that cannot happen.

**The decision.** `session_is_live` is a **delay**. A live `/assistant` session still gets first refusal
— option (a) stays first — but once the oldest un-landed entry is stale past the **longer** live-session
bound, the daemon drains anyway, live session or not. The nudge is demoted to the last resort it was
always meant to be. `OUTBOX_LIVE_SESSION_STALE_SEC` (`--outbox-live-stale-min`) defaults to **60 min** —
a **residual default**, flagged for the owner's veto like §10's list: 3× the plain bound gives a draining
session ample room (a session that is actually draining lands its writes in minutes), while resolving
well inside the ~3 h alarm so that alarm stays the last resort. No new lock was needed.

**Rejected alternative, recorded so it is not re-proposed as new:** surfacing the backlog count in the
warm session's turn context so the session cannot fail to notice. It fixes the *ignorance* and leaves the
*deadlock* — the daemon still stands down, and the fix only works when the session then acts. A
reasonable companion change; not a substitute.

## 10. Decisions & residual defaults

**Decided by the owner:**

1. **Flush fork (§9)** — **(a) opportunistic first.** (b) is a fast-follow gated on the pending-depth
   metric.
2. **Store format (§5)** — **sqlite** (`UNIQUE` dedup + per-entry status updates + WAL concurrency).
3. **v1 enqueue scope (§4)** — **acks + med-logs only.** Run-log finalize is phase 2 (its local mirror
   `state/run-log.md` already makes its Notion copy the least-exposed write).
4. **Happy-path write mode (§8)** — **belt-and-suspenders:** keep the direct MCP write **and** enqueue, so
   there's zero happy-path behavior change and the outbox is a pure failure backstop.
   **This holds only where a direct write exists — i.e. inside an LLM turn.** On the automated ack
   paths (👍 / `ack.py`) there is no LLM turn and therefore no direct write, so the outbox is the
   *primary* route, not a backstop, and the ⏰ row stays untouched until a flusher runs (§4). Reading
   this decision as unconditional is exactly how a flush ends up with no owner.

**Residual defaults — the assistant's call unless the owner vetoes** (each has a low-stakes proposed
value; flagged here so nothing is silently assumed):

5. **Create idempotency (§6)** — **minimal-window write-back** (mark `done` + store `notion_page_id` in the
   same local txn right after create). Accepts a millisecond double-create risk on med rows over a
   per-create read + schema touch.
6. **Retention** — Dream prunes `done` entries older than **14 days** (`modes/dream.md` →
   `outbox.py prune`); dead-letters persist until resolved. The caller is load-bearing, not cosmetic:
   `latest_landed_ack` scans every `done` row, on the stated premise that `prune` bounds the table.
7. **Dead-letter surfacing (§7)** — ~~carry-over ops-watch immediately; Telegram push once an entry is
   stuck **> 30 min**; always listed in the next Brief.~~ **Superseded by §7 as built.** One of the three
   shipped, at a different threshold: the Telegram push is `maybe_nudge_outbox_backlog` at
   **`OUTBOX_NUDGE_AGE_SEC` = 3 h**, not 30 min, **plus a dead-letter nudged on sight regardless of
   age**. The carry-over ops-watch entry and the Brief listing are **deliberately not built** — §7's last
   bullet gives the reason. §7 is the authority; this line is history.
8. **`MAX_ATTEMPTS` / backoff ceiling (§6)** — **8** attempts (~a few hours of backoff) before dead-letter.
9. **Live-session drain bound (§9 (a‴))** — **60 min** (`--outbox-live-stale-min`), 3× the plain 20-min
   stale bound.

## 11. Rollout — status

Stdlib, TDD, merge-on-green, merge-commit only, `-c core.fsmonitor=false` on every git call, branch off
`develop`.

1. ✅ **`outbox_common.py`** — the sqlite store: `connect`/migrate, idempotent `enqueue` (`UNIQUE` key,
   revive-on-dead-letter), `claim_ready` (FIFO + attempt-count + stale-inflight reclaim), `mark_done`/
   `mark_retry`/`mark_failed`, `next_backoff` (equal jitter), `stats`, `prune_done`. Unit-tested.
2. ✅ **`outbox.py`** — one CLI, subcommands `ack` / `medlog` / `enqueue` / `pull` / `mark` / `status` /
   `prune` (the sketch's separate `outbox_enqueue`/`outbox_status` collapsed into one clean entry point).
   CLI-tested; full suite green.
3. ✅ **Docs** — schema/lifecycle in `state/README.md`, the intent→MCP mapping + FIFO/idempotency/
   dead-letter rules in `store/notion/mapping.md` ("Outbox — durable act-low writes"), and the backend
   scope above.
4. ✅ **Wire the enqueue producers + the (a) in-turn drain routine** — belt-and-suspenders ack flow in
   `modes/chat.md`, the automated ack front door `scripts/ack.py` (Notion-gated), the med-log path via
   `outbox.py medlog`, and the drain (`pull --json` → replay via MCP → `mark`). (Option (b)
   `notion_rest.py` + `outbox_task` remains the deferred fast-follow.)
5. Backfill nothing; the outbox starts empty and the ack path fills it going forward. The manual "park it
   in carry-over" step is retired for outbox-covered writes.
6. ✅ **Give the flush an owner** — **§6.1 supersession** (`outbox_common.superseding_date` /
   `resolve_superseded` / `mark_superseded` / `latest_landed_ack`, the `resolution` column + its
   idempotent migration, swept inside `outbox.py pull`, verb `outbox.py resolve`); **§6's dead-letter
   classes** (`classify_dead_letter`, `mark --dead-letter --status-code`, the `dead_letter_kind` column);
   **revival resets `created_at`** (`revived_from`). ▶ **§9(a′)/(a‴) the daemon drain** (`presence
   ._tend_outbox` / `outbox_backlog` / `outbox_drain_due` / `maybe_drain_outbox`, Notion-gated, flags
   `--no-outbox` / `--no-outbox-drain` / `--outbox-stale-min` / `--outbox-live-stale-min` /
   `--outbox-drain-interval-min`) and **§7 the alarm + the gauge** (`maybe_nudge_outbox_backlog` through
   the Mouth, `outbox_*` on the status snapshot → `!status`) land with the presence.py port; their tests
   are in `test_outbox_drain.py` and activate as the hooks exist.
7. ✅ **§6.2 retraction, the third terminal state** — `outbox_common.RETRACTED` + `mark_retracted`
   (reason required, a landed write refused), the revive-on-retracted rule in `enqueue`, a `retracted`
   count on `stats`, and the `outbox.py retract` verb (short-id prefix, ambiguity refused).
8. ✅ **The `task_status` op** — the register → Notion Tasks projection's write path (`loops.py
   forward_task_status`, `register-notion-projection-spec.md`); rides the generic `pull`/`mark` loop.

Each step merged only on green CI and deploys via Path A's graceful reload.
