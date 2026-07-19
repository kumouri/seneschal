# Notion write-behind outbox — design (durability)

**Status:** approved direction (owner sign-off) · **Owner:** the assistant · **Scope:** a new durable
local write queue for the assistant's **act-low Notion writes** (reminder acks, med-intake rows; run-log
finalize in phase 2), plus the flush mechanism that lands them. **Decisions locked (§10): flush = (a)
opportunistic first; store = sqlite; v1 scope = acks + med-logs; happy-path = belt-and-suspenders (direct
MCP write + enqueue).** Companion: `asyncio-daemon-design.md` (where a phase-2 drainer task would live),
`../references/memory.md` (the local-first durability patterns this generalizes),
`../references/notion-rate-limits.md` (why writes rarely 429).

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

The house rule (persona operating principle #5, "honest about state"; `SKILL.md` Chat step 6) is *never
claim a write you didn't make*. Right now honoring it means the assistant has to **park the item in
carry-over by hand** and hope a later run replays it. That's a manual, lossy outbox. This spec makes it a
real one.

**The fix (one line):** every act-low Notion write is **journaled to a durable local store first** (it
survives a reboot), **then flushed to Notion** — idempotently, in order, retried until it lands. The
assistant can truthfully say "recorded" the instant it's journaled, because the journal *guarantees* it
will land.

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
| Run Log **create + per-phase body appends** (the incremental trace) | **Direct** (best-effort) | Already mirrored to `state/run-log.md`; local trace survives regardless |
| Any **read** (`notion-fetch`, `query-data-sources`, `search`) | **Direct** | n/a |
| **Ask-high** sends (email/Slack/calendar) & flips (linked Task/Goal → Done, archive) | **Direct**, via the approval loop | n/a — human-gated, single-shot (§3) |

The ack path is the marquee case. Where it plugs in today: chat write-through (`SKILL.md` Chat step 5) and
the slot ack path both already call `reminders_dequeue.py` (which records `acks.json`). That same moment
gains **one more call** — enqueue an outbox entry — so the ack is now durable against Notion, not just
against the fire-gate. (See §8 for why both still fire.)

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
    target_kind      TEXT NOT NULL,      -- 'page' (update) | 'db' (create-in)
    target_id        TEXT NOT NULL,      -- ⏰/🧭 page id, or the collection id to create in
    payload          TEXT NOT NULL,      -- JSON: the *logical* fields for this op (below)
    status           TEXT NOT NULL,      -- pending | inflight | done | failed(=dead-letter)
    attempts         INTEGER NOT NULL DEFAULT 0,
    not_before       TEXT,               -- ISO-UTC backoff gate; NULL = eligible now
    created_at       TEXT NOT NULL,      -- ISO-UTC
    last_attempt_at  TEXT,
    last_error       TEXT,               -- trimmed message from the most recent failure
    notion_page_id   TEXT                -- written back on success (esp. for creates); observability
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
```

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
- *Permanent* (404 target gone, 400 bad property, 403): dead-letter immediately — retrying won't help;
  surface for a human. `last_error` records why.
- *Ambiguous* (create timed out — did it land?): treated as transient → retry, and covered by the create
  idempotency choice above. This is exactly why OQ3 matters.

## 7. Observability — seeing a stuck or failed entry

- **`python scripts/outbox.py status`** — counts by status, oldest-pending age, and the full dead-letter
  list with `op`/`target_id`/`last_error`/`attempts`. The one command to answer "is anything stuck?"
- **Dead-letters surface where held approvals already do:** an ops-watch line in `state/carry-over.md` and,
  for anything older than a threshold, a Telegram push and a line in the morning **Brief**. A lost ack
  should *find* the owner, not wait to be discovered — the write-through-gap incident was found by luck.
- **A metrics line** per drain pass to `state/metrics.jsonl` (flushed / failed / pending-depth) so Dream's
  weekly rollup can watch the trend — a rising pending-depth is an early warning that Notion is unhappy.
- The table is inspectable with any sqlite tool; `last_error`/`attempts`/`notion_page_id` make each entry's
  history legible.

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

### My read (yours to overrule)

**Ship (a) first; keep (b) on the table as a fast-follow.** (a) delivers the entire durability guarantee
with near-zero new surface, and its only weakness — flush latency in a rare idle+outage window — is
bounded by the scheduled slots and, crucially, **loses nothing**. The store/schema/§8 wiring are designed
so moving to (b) later is *purely additive* (add a drainer that reads the same table) — we don't have to
pick the endgame now to start being durable. If the pending-depth metric (§7) ever shows real lag, that's
the signal to add (b). **But this is a genuine fork and it's yours** — if you'd rather pay the surface cost
now for cadence-independent flushing, we build (b) from the start.

## 10. Decisions & residual defaults

**Decided by the owner:**

1. **Flush fork (§9)** — **(a) opportunistic first.** (b) is a fast-follow gated on the pending-depth
   metric.
2. **Store format (§5)** — **sqlite** (`UNIQUE` dedup + per-entry status updates + WAL concurrency).
3. **v1 enqueue scope (§4)** — **acks + med-logs only.** Run-log finalize is phase 2 (its local mirror
   `state/run-log.md` already makes its Notion copy the least-exposed write).
4. **Happy-path write mode (§8)** — **belt-and-suspenders:** keep the direct MCP write **and** enqueue, so
   there's zero happy-path behavior change and the outbox is a pure failure backstop.

**Residual defaults — the assistant's call unless the owner vetoes** (each has a low-stakes proposed
value; flagged here so nothing is silently assumed):

5. **Create idempotency (§6)** — **minimal-window write-back** (mark `done` + store `notion_page_id` in the
   same local txn right after create). Accepts a millisecond double-create risk on med rows over a
   per-create read + schema touch.
6. **Retention** — Dream prunes `done` entries older than **14 days**; dead-letters persist until resolved.
7. **Dead-letter surfacing (§7)** — carry-over ops-watch immediately; Telegram push once an entry is stuck
   **> 30 min**; always listed in the next Brief.
8. **`MAX_ATTEMPTS` / backoff ceiling (§6)** — **8** attempts (~a few hours of backoff) before dead-letter.

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
4. ▶ **Wire the enqueue producers + the (a) in-turn drain routine** — belt-and-suspenders ack flow in
   `SKILL.md` (Chat step 5), the med-log path via `outbox.py medlog`, and the drain
   (`pull --json` → replay via MCP → `mark`) land with the producer-wiring PR (which also gates the
   daemon paths on the `notion` backend). (Option (b) `notion_rest.py` + `outbox_task` remains the
   deferred fast-follow.)
5. Backfill nothing; the outbox starts empty and the ack path fills it going forward. The manual "park it
   in carry-over" step is retired for outbox-covered writes.

Each step merged only on green CI and deploys via Path A's graceful reload.
