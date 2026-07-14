# `seneschal/state/` — local-first runtime state

The assistant's local working memory for the proactive (sentinel) loop. **Everything here except this
README and the `*.example.json` / `*.example.md` seed files is git-ignored** — it's regenerated at runtime
and machine-specific. Durable "system of record" memory still lives in the active store (Run Log,
carry-over); this directory is the cheap local cache the no-LLM sentinel and the modes read and write.

> **The memory logs live here now.** `run-log.md`, `carry-over.md`, and `context-digest.md` were split
> out of `../references/` (where they were tracked) into this gitignored directory so the daemon can run
> off `main` without a `git pull` conflicting on an append log. Their protocol/doc is
> `../references/memory.md`; each has a tracked `*.example.md` seed here. See that doc for the full story.

| File | Written by | What it holds |
|------|-----------|---------------|
| `reminders.json` | brain (creates) / presence (fires) | Ad-hoc reminders. List of objects — see `reminders.example.json`. |
| `acks.json` | `reminders_dequeue.py` (chat/slot ack path) | Durable **ack ledger** — `{normalized_reminder_id: "YYYY-MM-DD"}`, the LOCAL date (the owner's timezone) of the most recent ack per ⏰ row. The fire path (`check_reminders`) reads it to **suppress a due nudge already acked today** (see below). Fail-open: absent/broken → empty → never silences a real nudge. |
| `run-log.md` | modes (Dream/Wrap/Reminders/…) | Local mirror of the 🧭 Run Log (the active store is the record). Seed: `run-log.example.md`; protocol: `../references/memory.md`. |
| `carry-over.md` | modes | The assistant's running open-loops log (held approvals, follow-ups, flags). Seed: `carry-over.example.md`. |
| `context-digest.md` | Dream (overwrites) | Nightly condensed digest the morning Brief reads first. Regenerable cache. Seed: `context-digest.example.md`. |
| `reminders-id-cache.md` | Chat/Reminders (on create/retire) / Dream (reconcile) | Live **page-id table** for the ⏰ Reminders DB — lets a chat ack write `Status = Done` by id with no query. Split out of `../references/` (tracked) so runtime edits never jam the daemon's `pull --ff-only`. Seed: `reminders-id-cache.example.md`; protocol: the ack-by-cached-id notes in `../references/databases.md` (⏰ Reminders section). |
| `control-queue.json` | `request_control.py` / seneschald-update | Flagged daemon control requests (`restart` / `shutdown`), applied once warm sessions are idle. Seed: `control-queue.example.json`. |
| `telegram-offset` | `telegram_poll.py` | Last acknowledged Telegram `update_id + 1`. Delete to replay ~24h. |
| `discord-offset` | `discord_poll.py` | Last-seen Discord message id (snowflake). Delete to re-seed "from now." |
| `telegram-thread.json` | `presence.py` | Rolling chat history (last ~20 turns) for continuity across warm sessions — spans Telegram **and** Discord (one thread). |
| `presence.lock` | `presence.py` | Single-instance lock for the daemon: `{pid, started_at, heartbeat}`; stale if the heartbeat is old. |
| `presence-state.json` | `presence.py` | The daemon's own runtime state — chiefly the **action queue** of inbound messages consumed off the wire but not yet answered — saved after every step + on exit, reloaded on startup so a restart (e.g. `reseneschald` after a PR merge) never drops a message. `{pending: [{channel, text}], last_session_id, updated_at}`. |
| `pending-approvals.json` | brain | Held drafts awaiting approval (local mirror of the carry-over record). |
| `last-peek` | `presence.py` / `sentinel.py` | Timestamp of the last comms-peek (gates the peek cadence). |
| `slots.json` | `presence.py` | Per-slot last-fired **local** date (`{name: "YYYY-MM-DD"}`) for the daemon's internal Brief/Wrap/Dream/Journal + reminder-slot scheduler; fire-once-per-day guard. |
| `rolls.json` | `presence.py` (`reminders_roll.py`) | Last-refill **local** date (`{"refilled": "YYYY-MM-DD"}`) for standing every-N-hours reminder **rolls** — the daemon regenerates each roll's day of nudges once per local day (future-only, idempotent). See "standing rolls" below + `../references/reminders-policy.md`. |
| `quiet.json` | Chat mode (`quiet_set.py`) | Active **do-not-disturb window** (`{until, set_at, set_by, reason}`, UTC). While `now < until` the daemon **drops** every due nudge that doesn't pierce (⭐ High and below); `Call Me` + `🚨`/`🛑` Critical-and-above still fire. Absent = not quiet. `quiet_set.py --clear` lifts it. See `../references/reminders-policy.md` → "Quiet window." |
| `nudge-stagger.json` | `sentinel.py` (`check_reminders`) | Instant of the last **non-piercing** nudge fired (`{"last_nonpiercing_fire": ISO-UTC}`) — the **catch-up stagger** clock. A released backlog fires ≤ 1 non-piercing nudge per pass, ≥ 15 min apart, so it drips instead of walling; piercing items (`Call Me`/Critical/meds) skip it. Absent/broken → fire immediately (fail-open). See `../references/reminders-policy.md` → "Catch-up stagger." |
| `last-signal.json` | `sentinel.py` | Verdict of the last sentinel one-shot (debug / observability). |
| `metrics.jsonl` | modes (Trace/Observability advisor) | Append-only per-turn metrics the **Observability advisor** emits (one JSON object per line). Feeds Dream's weekly graduation rollup. Seed: `metrics.example.jsonl`; schema below; see `../references/advisor-chain.md`. |
| `router-log.jsonl` | `presence.py` (Router advisor, shadow) | Append-only per-inbound-message verdicts from the front-door **Router advisor** running in **shadow mode** (`scripts/router.py`, model `qwen3.5:4b`) — one JSON object per inbound chat message. **Log-only, zero behavior change** in phase 1; it's the accuracy evidence reviewed before phase 2 enables local handling. Seed: `router-log.example.jsonl`; schema below; setup: `../scripts/ROUTER_SETUP.md`. |
| `forgetting-events.jsonl` | Chat / Brief / Dream (the reasoning loop) | Append-only **forgetting-event log** (salience Phase 2): one line each time the assistant failed to recall/surface something *and there was a reaction* — the emotional-weight axis of `salience = frequency × weight`. Written organically, **never fished for**. Seed: `forgetting-events.example.jsonl`; schema below; policy: `../references/salience.md`. |
| `ablation-judgments.jsonl` | Chat (via `scripts/ablation_log.py`) | Append-only **memory-ablation A/B verdicts** (salience Phase 4c) — the human-judged **oracle** the cheap salience proxies calibrate against: the assistant answered a sampled recall turn twice (memory *with* vs *withheld*, blind where practical), the owner picked the better answer **and said why**. Seed: `ablation-judgments.example.jsonl`; schema below; protocol: `../references/salience.md`. |
| `rag-index.sqlite` | `scripts/rag_index.py` (Dream refresh) / `rag_query.py` (access counters) | Local **semantic RAG index** (Retrieval advisor, phase B): embedded chunks of the assistant's prose corpus (journal/notes/run-log) for semantic recall. Regenerable cache — rebuild with `rag_index.py --rebuild --local`. Also carries the **salience-learning** data (observe-only "what's safe to forget" experiment): `disposable`/`salience_cat`/`predicted_at` tags on chunks + the `salience_access` ledger — which is **measurement, not cache** (deliberately preserved across `--rebuild`; counters key on deterministic chunk ids). Schema below. Binary; no seed. Setup: `scripts/RAG_SETUP.md` + `scripts/SALIENCE_SETUP.md`. |
| `health.db` | `scripts/health_import.py` | Local **Samsung Health cache**: sleep sessions + stages, heart rate, stress, SpO₂, skin temperature, steps, meds, weight, parsed out of the export zip. Timestamps are naive-UTC + `tz_offset_min` (Samsung stores UTC, **not** wall clock — reading them as local shifts everything by the local UTC offset; see `../scripts/HEALTH_SETUP.md`). Regenerable cache; the export is cumulative, so delete and re-import any time. Binary; no seed. |
| `health-dashboard.html` | `scripts/health_dashboard.py` | Rendered **sleep & health dashboard** — self-contained HTML (inline CSS + generated SVG, no CDN), openable straight from disk. Centrepiece is the actigram: one row per night, every sleep session drawn where it actually happened. Regenerable from `health.db`. |
| `presence.db` | `scripts/presence_import.py` (via the listener's `/presence-ingest`) | Local **presence event store** (Phase 1): geofence enter/exit, activity transitions, sleep/wake — the *edge* log, consumed once-per-transition by the rules layer against a watermark. **Coordinates never land here** — the wire carries only place name + transition. Naive-UTC + `tz_offset_min`. Regenerable cache; binary, no seed. **Pruned to ~30 days nightly in Dream** (`presence_import.py --prune-days 30`). See `../scripts/presence_common.py`. |
| `presence-context.json` | `scripts/presence_import.py` (recomputed each ingest) | The derived **level** snapshot — `{at_place, activity, asleep, since, updated_at}` — answering "where / what / asleep is the owner *now*" so the daemon's suppression gate reads it cheaply. Regenerated from `presence.db` on every event; no seed. |
| `presence-feed.ndjson` | `scripts/health_listener.py` (`/presence-ingest`) | Verbatim archive of every presence batch the phone POSTs (sibling of `health-feed.ndjson`). Append-only; replayable into `presence.db`. |
| `presence-automations.json` | hand-edited (seed `presence-automations.example.json`) | **Phase 5** config — the owner's context-edge → Home Assistant automations for `scripts/presence_actions.py`. Each has an `on` trigger + a `call`; `approved:true` fires act-low, else draft-and-hold; `failsafe:true` stays ask-high always. **Inert until HA is stood up** (`ha.env`) and the daemon hook is enabled — see `../scripts/HA_SETUP.md`. |
| `telegram-inbox.json` | — | **Legacy/unused.** Old sentinel inbox-stash; the presence daemon consumes Telegram directly now. |
| `archive-people.json` | hand-edited (seed `archive-people.example.json`) | **Person registry** for the message archiver: person key → per-service identity (e.g. Telegram `user_id` + `export_dir`) + the owner's own ids (used to compute message direction). Read by `archive_common.load_people`. Schema below. |
| `archives/<person>/` | `telegram_ingest.py` / `archive_aggregate.py` | **Per-person cross-service conversation archive** — `raw/` (per-service source stores/pointers), `normalized/<service>.json`, a unified `media/`, and the rendered `conversation.{json,md,html}`. Private conversation content + bulk media; **no seed**. Layout below. Overridable via `ARCHIVE_OUT_DIR`. |

## `reminders.json` schema

```json
[
  {
    "id": "rmd-<date>-<slug>",        // stable id
    "text": "what to remind them",     // shown after "⏰ Reminder: "
    "due_at": "2026-06-29T19:45:00Z",  // UTC instant; brain converts the owner's local time → UTC
    "channel": "telegram",             // telegram (default) | call (phone) | discord
    "reminder_id": "<⏰ row page id>",  // OPTIONAL stable key back to the ⏰ Reminders row (see below)
    "member_reminder_ids": [],         // OPTIONAL; the ⏰ rows a *digest* covers — gated only when ALL are acked
    "ack_gate": true,                   // OPTIONAL; false = exempt from the fire-time ack gate (multi-fire rolls)
    "pierce_quiet": false,             // OPTIONAL; true = fire even during a quiet window (Critical+; see below)
    "require_place": "home",           // OPTIONAL; defer (hold, never drop) until the owner is at this geofenced place
    "created_at": "2026-06-29T14:02:00Z",
    "fired_at": null,                   // set to the fire time once delivered; null = pending
    "suppressed_at": null,              // set (instead of fired_at) if dropped by a quiet window; consumed, not delivered
    "acked_at": null                    // set (instead of fired_at) if dropped by the fire-time ack gate; consumed
  }
]
```

`reminder_id` is the stable key that lets an **ack cancel an obsolete re-nudge**. Because the daemon
fires by `due_at` and can't read acks in the active store, a nudge pre-staggered earlier in the day would
still fire after the owner has already acked it. So when the brain stamps a queue entry with the ⏰ row's
key (`reminders_enqueue.py --reminder-id <page id>`), an ack can then run
`reminders_dequeue.py --reminder-id <page id>` to drop every **un-fired** entry for that row (fired
history is left untouched; matching is dash-insensitive so a page id matches with or without dashes).
The field is optional and additive — the daemon ignores it; only the enqueue/dequeue pair use it.

Firing a reminder is **act-low** (it's the owner's own content), so the presence daemon delivers it
directly with no LLM, routing by `channel`: **telegram** → `telegram_send.py`, **call** → `push_call.py`
(Worker `/push-call`, the spoken line, no ⏰ prefix), **discord** → `discord_send.py`. An
unconfigured/unknown channel falls back to Telegram (never silently dropped). The brain only ever
*creates* reminders (when the owner says "remind me…") and writes the UTC `due_at` + `channel`.

**Fire-time ack gate (`acks.json` / `acked_at` / `member_reminder_ids` / `ack_gate`).** The daemon fires
by `due_at` and can't read the active store, so a nudge staggered *before* an ack — or a **soft-digest**
covering it that no per-id dequeue can reach — used to buzz for something already done. Now every ack
records the ⏰ row's key + today's local date to `acks.json` (the `reminders_dequeue.py` call the
chat/slot ack path already makes does this), and `check_reminders` consults it: a due entry whose
`reminder_id` (or, for a digest, **every** `member_reminder_ids`) is acked *today* is **dropped** —
stamped `acked_at` (consumed, never re-delivered) instead of `fired_at`. Only *today's* acks gate,
mirroring the daily reset. Same drop-not-defer contract as quiet. A same-day **multi-fire** entry (a roll)
sets `ack_gate: false` so one ack doesn't cancel the day's remaining pings. Fail-open: a missing/broken
ledger reads empty, so the gate can only *suppress a genuine ack*, never silence a real nudge. All four
fields are optional and additive.

**Quiet-window fields (`pierce_quiet` / `suppressed_at`).** When a `quiet.json` window is open, the daemon
gate (`check_reminders`) **drops** each due entry that doesn't pierce — stamping `suppressed_at` (so it's
consumed, never re-delivered when quiet lifts) instead of `fired_at`. An entry pierces if it's a `Call Me`
ring (`channel: call` / `escalate`) **or** the brain set `pierce_quiet: true` at enqueue
(`reminders_enqueue.py --pierce-quiet`) — which it does for `🚨 Critical` / `🛑 Super-Critical` items, where
it can read the ⏰ row's `Importance`. Both fields are optional and additive; a normal (non-quiet) fire path
ignores them.

**Place gate (`require_place`).** A nudge tagged with a place name (a phone geofence, e.g. `home`) is
**deferred** — held, never dropped — by `check_reminders` until `state/presence-context.json` says the
owner is at that place, then it fires (presence Phase 1). Enqueue with
`reminders_enqueue.py --require-place home`. The gate is **fail-open** — absent / stale / unavailable
presence *fires* it rather than holding forever — and a no-op for any entry without the field.

**Driving gate (`activity`, Phase 2).** While `presence-context.json` says `activity == in_vehicle`, the
same fail-open gate holds **non-piercing** nudges until the owner is stationary (a `Call Me` ring and
`🚨`/`🛑` Critical fire through) — no per-entry flag needed. Logic for all gates:
`../scripts/presence_rules.py` (`should_defer(entry, ctx, pierces)`) + `sentinel.py`
(`_presence_context_fresh` + the fire loop).

**The asleep gate (Phase 3) was retired.** The phone-only Sleep API classifies an idle phone as a sleeping
owner (observed: confidence 73–92 through a wide-awake afternoon), so the gate held daytime nudges behind
a false "asleep". The snapshot still carries `asleep` (the phone keeps sending sleep events; informational
only) but `presence_rules.py` ignores it — the rule returns only with a wearable-grade signal (watch heart
rate + wrist motion).

## Standing rolls (`reminders_roll.py`)

Most reminders fit the four daily slots, but some want a **custom intraday cadence** the slots can't
express — e.g. *check messages every 2 hours, 9am–11pm*. Those live as **rolls** in
`../scripts/reminders_roll.py` (a `ROLLS` config list keyed to a ⏰ row's `reminder_id`). The presence
daemon calls `refill_rolls` from its loop, gated **once per local day** via `rolls.json`, and regenerates
the roll's whole day (plus tomorrow, as a rolling horizon) directly into `reminders.json` — pure local
queue math, no Notion and no `claude` spawn, so it's free and safe to run every loop. It's **future-only**
(a late first run never back-fires past slots) and **idempotent** (stable ids `rmd-<date>-<prefix>-<HH>`,
so it never double-queues). `Dream` prunes fired entries nightly, keeping the queue bounded. Run
`python reminders_roll.py --dry-run` to preview. Behavior contract: `../references/reminders-policy.md`.

## `metrics.jsonl` schema

Append-only **JSON Lines** (one object per substantive turn) written by the **Observability advisor**
(the `out:` side of Trace). Local-first and gitignored like the rest of `state/`; it's a cheap analytics
cache, not a system of record — safe to lose or truncate. Dream reads a rolling window of it to propose
ask-high → act-low **graduations** with real evidence (`../references/autonomy-policy.md`).

```json
{
  "ts": "2026-07-06T13:31:00Z",        // turn finalize time (UTC)
  "mode": "Triage",                     // Chat | Brief | Wrap | Triage | Ask | Reminders | Watch | Dream
  "model": "opus",                      // model tier the turn ran on (opus | sonnet | haiku)
  "reads": 6,                           // Notion/calendar reads issued
  "rate_limited": 0,                    // count of 429s hit
  "drafts_held": [{"kind": "email"}],   // ask-high drafts held this turn (kind per pending-approvals)
  "actions": {"act_low": 3, "ask_high": 2},
  "correction": "approved_as_is",       // null (n/a) | approved_as_is | edited_before_approve | rejected
  "outcome": "Success",                 // Success | Partial | Failed
  "run_log": "<🧭 row id>"              // link back to the Run Log row for this turn
}
```

The **`correction`** field is the graduation signal: an action surfaced ask-high and repeatedly
`approved_as_is` (never `edited_before_approve`) is the evidence Dream's rollup needs to nominate it for
act-low. `edited_before_approve` means the owner changed the draft before approving; `rejected` means they
discarded it. `null` when the turn held nothing to approve.

## `router-log.jsonl` schema

Append-only **JSON Lines** (one object per inbound chat message) written by the front-door **Router
advisor** in `presence.py`, phase 1 (**shadow**). The router (`../scripts/router.py`) runs a small local
Ollama model (`qwen3.5:4b`) to classify each message *trivial-and-safe* vs *escalate* **before** the warm
Opus session spins — the same daemon-cheap-model shape as Watch. In phase 1 the verdict is **only logged**:
every message still escalates to the warm session, unchanged. It's a gitignored local analytics cache
(safe to lose/truncate); it's the accuracy evidence reviewed before phase 2 flips on local handling.

```json
{
  "ts": "2026-07-06T22:34:37Z",          // when classified (UTC)
  "channel": "telegram",                  // telegram | discord
  "text_preview": "took my meds",         // first ~80 chars of the inbound message
  "verdict": "trivial",                   // trivial | escalate
  "category": "ack",                      // ack | status | recall | other
  "confidence": 0.95,                     // 0.0-1.0; below ROUTER_CONF_THRESHOLD ⇒ escalate/other
  "model": "qwen3.5:4b"                   // the classifier model
}
```

**`verdict`/`category`:** `trivial` carries one of `ack` (a plain reminder/task acknowledgement),
`status` (a schedule/todo status question), or `recall` (simple factual recall). `escalate` always carries
`other` — anything drafting/outbound, ambiguous, multi-step, ask-high, or where the assistant's voice
carries the message. The router is **conservative**: Ollama unreachable, bad JSON, an unknown category, or
confidence below `ROUTER_CONF_THRESHOLD` (default 0.7) → `escalate`/`other`. Abstain ⇒ escalate. To read
the log, tail it or count verdicts by category (see `../scripts/ROUTER_SETUP.md`).

## `forgetting-events.jsonl` schema

Append-only **JSON Lines**, the rare-event counterpart to the bounded `salience_access` counter (events
are rare and their ordering/text matters, so this one IS an append log — same family as `metrics.jsonl`).
A "forgetting event" = *the assistant failed to recall or surface something, and there was a reaction.*
Logged by the reasoning loop when it happens organically; the assistant **never asks** "did that upset
you?" (policy: `../references/salience.md`). Gitignored analytics cache — safe to lose/truncate.

```json
{
  "ts": "2026-07-09T21:04:00Z",
  "subject": "the owner's birthday",         // what was forgotten (short)
  "salience_cat": "identity.core",           // the Phase-1 taxonomy (references/salience.md)
  "what_happened": "didn't mention it in the Brief",
  "reaction_text": "you seriously forgot my birthday?",  // their words if any (verbatim, short) — "" if none
  "sentiment": -0.8,                          // signed weight −1..+1 (magnitude = how much it mattered)
  "sentiment_source": "owner",                // owner | assistant_inferred | classifier
  "confidence": 0.9,
  "chunk_ref": "journal:2026-01-14",          // OPTIONAL link to the memory's doc id, if identifiable
  "recorded_by": "chat"                       // which surface caught it (chat | brief | wrap | dream | …)
}
```

**`sentiment_source`:** `owner` = the owner's stated/plain reaction (always wins) · `assistant_inferred` =
the warm Opus session's contextual read (the source of record otherwise) · `classifier` = the offline
`scripts/sentiment.py` cross-check Dream runs over `reaction_text` (`qwen3.5:4b`, local, abstains to
weight 0.0 when Ollama's down). Opus-vs-classifier divergence is a data-quality flag in the weekly
rollup, never an override. High-|weight| events are what **protect** rare-but-heavy memories from any
future prune recommendation (the θ_protect clause).

## `ablation-judgments.jsonl` schema

Append-only **JSON Lines**, written by `../scripts/ablation_log.py` (which validates + appends — a
malformed oracle label is worse than none, so it refuses bad records). Each line is one completed
**memory-ablation A/B**: sampled, offline/on-request, never live-behavior-changing. The weekly
`salience_rollup.py` folds verdicts into its report. Gitignored analytics cache.

```json
{
  "ts": "2026-07-12T02:10:00Z",
  "query": "when's my next dentist appointment?",             // the recall question A/B'd
  "subject": "appointment schedule recall",                   // short label for what was tested
  "chunk_refs": ["journal:2026-07-08"],                       // doc id(s) of the memory withheld
  "verdict": "with",                                          // with | without | tie
  "why": "the with-memory answer had the actual day and time",// the owner's reason — the labeled data
  "judged_by": "owner",
  "blind": true                                               // they didn't know which was which
}
```

**Verdict semantics:** `with` = the memory earned its keep (keep signal, and the `why` says *what
about it* mattered) · `without`/`tie` = disposability evidence for that memory/kind. This is the
ground-truth axis: access counts measure *demand*, sentiment measures *cost of forgetting*, the
ablation measures **whether the memory actually improved the answer**.

## `salience_access` schema (inside `rag-index.sqlite`)

The **access ledger** of the observe-only what's-safe-to-forget experiment
(`../references/salience.md`, mechanics `../scripts/SALIENCE_SETUP.md`). Written by
`rag_query.py::search()`: every hit scoring ≥ the access floor (default 0.55) upserts its chunk's row —
an aggregated counter (bounded, one row per touched chunk), not a per-touch event log. Analysis reads
pass `--no-record` so they never pollute the counters; a ledger write can never fail a recall.

```sql
salience_access (
    chunk_id     TEXT PRIMARY KEY,   -- chunks.id (deterministic doc:ref#idx — survives re-embeds)
    doc_id       TEXT NOT NULL,
    salience_cat TEXT,               -- taxonomy tag at last touch (references/salience.md)
    hit_count    INTEGER NOT NULL,   -- times returned in top-k above the floor
    first_hit_at TEXT,               -- ISO UTC
    last_hit_at  TEXT,
    max_score    REAL NOT NULL       -- best cosine ever seen (near-miss ≠ strong recall)
)
```

The companion columns on `chunks` — `disposable` (`0` normal · `1` predicted-disposable, still fully
retrievable · `2` approved-forgotten, the gated soft prune), `salience_cat`, `predicted_at` — are
migrated in place by `rag_common.connect()` (idempotent). Nothing writes `2` automatically.

## `telegram-thread.json` schema

Rolling chat history the presence daemon keeps for continuity *across* warm sessions (within a session,
the live `claude` process is the context). Capped to the last ~20 turns; seeded into the grounding of a
freshly-spawned session.

```json
[
  { "role": "owner",     "text": "what's on today?", "ts": "2026-06-29T18:20:00Z" },
  { "role": "assistant", "text": "Three things need you…", "ts": "2026-06-29T18:20:04Z" }
]
```

## `pending-approvals.json` schema

Local mirror of the held drafts in the carry-over record (the system of record). The brain writes these
when it drafts something ask-high; the owner approves/rejects via chat, Telegram, or a Notion comment, and
the brain flips `status` and executes. See `../references/memory.md` for the full loop.

```json
[
  {
    "id": "a3",                       // short, stable — so a one-word reply ("send a3") is unambiguous
    "kind": "email",                  // email | slack | calendar_response | notion_write
    "channelRef": "<thread/event id>",
    "summary": "Reply to Alex re: design feedback",
    "bodyPreview": "Hi Alex — thanks for the…",
    "created_at": "2026-06-29T18:20:00Z",
    "status": "pending"                // pending | sent | rejected | failed
  }
]
```

## `archive-people.json` schema + `archives/<person>/` layout

The **person registry** for the message archiver (`../scripts/archive_common.py`). `owner` is you (one
entry, shared across everyone — it's what lets the archiver compute message `direction`); each
`people.<key>.services.<svc>` carries that person's per-service ids. Seed: `archive-people.example.json`.

```json
{
  "schema": "seneschal.archive.people/1",
  "owner": {
    "display_name": "Owner",
    "telegram": { "user_id": "1000000001" }
  },
  "people": {
    "alex": {
      "display_name": "Alex",
      "aliases": ["their_handle", "ALEX"],
      "services": {
        "telegram": { "user_id": "2000000002", "export_dir": "C:/…/ChatExport_YYYY-MM-DD" }
      }
    }
  }
}
```

Each archived person's output tree (all gitignored; `ARCHIVE_OUT_DIR` overrides the root):

```
archives/<person>/
  raw/           per-service source stores/pointers (e.g. a telegram-source pointer)
  normalized/    <service>.json — e.g. telegram.json  (the shared seneschal.archive/1 record schema)
  media/         all attachments, deduped, stable-named <service>_<msgid>_<n>.<ext>
  conversation.json | conversation.md | conversation.html   (merged timeline, three renderings)
```

Each normalized message is one flat record: `{service, service_msg_id, person, thread_id, direction
(from_me|from_them|system), sender_name, ts_utc (int epoch UTC — the sort key), ts_iso, text, text_html?,
text_raw?, media[], price?, is_tip, is_locked, edited_ts_utc?, reply_to?, service_meta}`.
