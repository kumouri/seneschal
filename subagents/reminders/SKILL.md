---
name: reminders
description: >-
  The assistant's reminders & nudges. Tracks the things the owner wants to be reminded of — recurring
  habits (water the plants, stretch), today's unconfirmed todos, and goals/tasks nearing a deadline —
  expects a response, remembers what went unacknowledged, re-fires important things until confirmed
  done, and gently ribs repeated low-stakes skips. Use for "remind me to…", "did I do X", "what's still
  open", or the scheduled intraday runs. Delegated to by the seneschal orchestrator (Reminders mode).
compatibility: Requires a configured seneschal store (run /setup-store). The Notion backend additionally requires the Notion MCP (mcp__*__notion-*). Enqueues nudges via seneschal/scripts/reminders_enqueue.py → state/reminders.json; the presence daemon delivers them over Telegram/Discord — or rings the owner (call channel) for `Call Me` items (act-low, to the owner's own channels). Reads the assistant's persona + references.
---

# Reminders & Nudges (Seneschal · Reminders mode)

Keep the owner's reminders honest in the persona's voice: brief, persistent on what matters, dry on the
small repeated misses, **never shaming**. State lives in the **Reminders** domain of the store; behavior
(escalation, tone, slots) is governed by `../../seneschal/references/reminders-policy.md` — follow it
exactly. Reads and Reminders tracker writes are **act-low**; flipping a *linked* Task/Goal to Done is
**ask-high**.

The **scheduled** runs (`seneschal-reminders-morning` / `-midday` / `-evening` / `-bedtime`, e.g. 08:00 /
12:30 / 18:30 / 21:30 local — only Morning runs the daily reset) are where the push + Run Log write
attach. Each task's prompt inlines the full state machine so it runs unattended. Invoked
**interactively** ("remind me to…", "did I do X"), do the read + reconcile and propose writes, but only
write the tracker (act-low) and never the linked Task/Goal.

## Read first

**Store access.** Read `../../seneschal/store/config.json` for the active backend, then that backend's
`../../seneschal/store/<backend>/schema.md` (the Reminders domain's fields + canonical option values) and
`../../seneschal/store/<backend>/mapping.md` (how each verb executes — on Notion, the by-cached-id ack
flow + the fetch-vs-query throttle rule this skill leans on). Speak the six **store verbs** (`store-query`
/ `store-get` / `store-create` / `store-update` / `store-append` / `store-search`) plus domain nouns and
canonical, **emoji-free** option values (`status: done`, `importance: critical`); the mapping resolves
them to the backend's tools. **Never fetch schemas at runtime.**

- `../../persona/persona.md` (else `../../persona/persona.default.md`) — voice (incl. the
  reminder/ribbing tone) + `../../persona/owner-profile.md` (if present).
- `../../seneschal/references/reminders-policy.md` — slots, state machine, importance rule, rib
  threshold, tone ladder.
- `../../seneschal/references/databases.md` — the Reminders domain + Tasks/Goals/Flags map (Notion
  backend); the Notion query projection gotcha lives in `../../seneschal/store/notion/mapping.md`.
- `../../seneschal/references/autonomy-policy.md` — act-low vs ask-high (and the Reminders graduation
  entry).
- `../../seneschal/references/comms-mapping.md` — reminder delivery (Telegram via the presence daemon) +
  ack channels.
- `../../seneschal/references/memory.md` — how to leave a trace (Run Log + carry-over protocol). Live
  files: `../../seneschal/state/run-log.md` + `../../seneschal/state/carry-over.md` (gitignored; the
  active store is the record).

## Steps

**Phase 0 — Orient.** Determine "today" + the current **slot** in the owner's configured timezone
(after-midnight counts as the prior day). If invoked without a slot, infer from the clock (or treat as a
Morning-style full pass if asked "what's still open"). **If the Reminders domain isn't provisioned yet,
say so and stop** — there's nothing to track until it exists (see `databases.md` / `store/<backend>/schema.md`).

**Phase 1 — Read state (one parallel batch):**
- **`store-query` Reminders** rows that could fire: status in (pending, reminded, snoozed) and (cadence
  applies today **or** due/target ≤ today). Read the `ack` + `last_acknowledged` fields to catch acks
  since the last run. Also pull the `related_task` / `related_goal` relation refs — you'll join them
  locally below. (On the Notion backend the due filter is the `date:Due / Target:start` projection and
  relations come back as JSON URL arrays — see `../../seneschal/store/notion/mapping.md`.)
- **Linked Tasks/Goals — `store-get` by ref (capped), NOT another `store-query`.** The linked `status`
  is the real "done" truth (don't trust a stale Reminders row over its source). Collect the linked refs
  from the Reminders query's `related_task` / `related_goal` relations, then read each linked row with
  **`store-get` by ref**, **capped to ~3 concurrent**. **Why get-by-ref, not a broad query:** on the
  Notion backend the query (`notion-query-data-sources`) is the SQL path the `collection_router_upstream_429`
  throttle actually hits, while `store-get` (`notion-fetch`) routes around it and capped concurrency keeps
  it under the classic ~3 req/s burst too (verified in production — `store/notion/mapping.md` →
  Throughput). A reminder set usually has only a handful of linked rows; if a set has **none**, zero
  reads. *(This corrects the earlier "one broad query per DB" guidance, which cut call count for
  burst-throttle #1 but pushed more load onto the upstream throttle #2 we actually keep hitting.)*
- Last Run Log carry-over (so important open items aren't dropped).

**Phase 2 — Reconcile** (per `reminders-policy.md`):
- **Apply acks FIRST — before any reset.** An ack arrives three ways: the one-tap `ack` flag set, the row
  already written through by chat (`status: done` + `last_acknowledged` set), or the linked record now
  done. Apply via **`store-update` — `status: done` for *every* type** (Recurring Habit, Today Todo,
  Deadline Watch): `done` = done *for today*, still an **active** reminder that re-fires on its next due
  cycle (a one-time item comes back tomorrow while still in-window). Set `status: finished` (retire —
  drops out of the active-reminders filter, stops re-firing) **only** on the owner's explicit "I'm
  finished with X"; a plain ack is never `finished`. Both set `last_acknowledged: today`,
  `consecutive_misses: 0`, then **reset `ack`** — it's a one-shot input, consumed on read, so a stale
  flag can't auto-complete a later cycle. At the **Morning** slot, an ack made since the last run belongs
  to **yesterday** (`last_acknowledged: yesterday`) so the reset below doesn't count it as a miss. The
  **durable** done-record is `status`/`last_acknowledged`, never the one-tap flag — the EOD Wrap counts
  "done today" off `last_acknowledged = today`. (On the Notion backend, resolve the row **by its cached
  page id** — the ack write routes around the throttle the lookup query hits; worked end-to-end in
  `../../seneschal/store/notion/mapping.md` → "the reminders ack".)
  **Then cancel the obsolete re-nudge — once per acked row:** run
  `python ../../seneschal/scripts/reminders_dequeue.py --reminder-id <the row's ref/id>` — it drops
  every still-un-fired `state/reminders.json` entry for the row **and records the ack to
  `seneschal/state/acks.json`**, the durable ack ledger the daemon re-reads at fire time (so a
  pre-staggered nudge, or a soft-digest baked earlier, for an already-acked row never buzzes). This
  dequeue is **daemon-local and backend-independent** — it never touches the store. Act-low; see
  `reminders-policy.md` → "Fire-time ack gate."
- **Journal-presence-gated habits (auto-satisfy from journal activity).** For any Recurring Habit whose
  `notes` contains `[gate: journal-presence]` (the canonical case: **Write in the journal**), before
  treating it as a fire candidate, `store-get` the Interstitial Journal page and check for a **top-level
  date toggle for today** (the owner's configured timezone) with ≥1 timestamped entry (ignore the
  assistant's own carry-over callout). If the owner journaled today → **auto-satisfy like an ack**
  (`status: done`, `last_acknowledged: today`, `consecutive_misses: 0`) and **drop it from this slot's
  fires** — never nudge a thing already done today. Only nudge (Evening slot) once they've had
  `quiet_days` consecutive quiet days (default `1`). Run at **Evening** (nudge-or-satisfy) + **Bedtime**
  (satisfy-only backstop). See `reminders-policy.md` → "Journal-presence gate."
- **Morning slot only:** run the **daily reset** for `Daily`/`Weekdays` habits — increment
  `consecutive_misses` for yesterday's unacked (a row just acked above is **not** a miss), then set them
  `status: pending` + clear `reminded_today` (leave `last_acknowledged` alone — it's the Wrap's
  evidence). Never zero misses here. Interval/`Weekly`/`One-off` rows aren't reset daily — flip them to
  `status: pending` when their date rule says due.
- **Compute this slot's fires:** `pending` items matching this slot's time window / due, plus the
  **re-fire** of any unacked item that is `importance: super-critical`/`critical`/`high` **OR**
  `nag_until_done: true` (regardless of window), plus re-surfaced `snoozed` items.
- **Rib candidates:** `importance: low`/`notable` habits **with `nag_until_done: false`** and
  `consecutive_misses >= 3` (one dry line, once/day). Nag/important items are re-fired, never ribbed.

**Phase 3 — Deliver (act-low):**
- Compose **one short nudge per reminder** in the persona's voice — never bundle several items into one
  message (bundling is retired; see `reminders-policy.md` → "One reminder per nudge"). Append at most one
  rib line if warranted. Honor the tone ladder; no walls of text.
- **Enqueue one entry per reminder for the daemon to deliver over Telegram** — when a slot has more than
  one item, enqueue each as its own entry with `--due-at` **spaced 15–30 min apart**, ordered by
  `importance`, each with its own stable `--id`, **stamped with the Reminders row's ref/id** so a later
  ack can cancel it (the fire-time ack gate needs it):
  `python ../../seneschal/scripts/reminders_enqueue.py --text "<one item>"
  --id rmd-<YYYY-MM-DD>-<slot>-<item> --reminder-id <the row's ref/id>`
  (writes `../../seneschal/state/reminders.json`; the resident `presence.py` daemon fires it via Telegram
  within ~one poll cycle and stamps `fired_at`; **Dream** prunes fired entries). The per-item `--id` makes
  re-runs idempotent. End each text with the v1 ack hint: *"tick Ack in Notion or tell me."* If the
  daemon isn't running the entry simply waits in the queue; surface the nudge in chat too when
  interactive.
- **Quiet-window pierce.** If the item you're enqueuing is `importance: critical` or `super-critical`,
  add `--pierce-quiet` so it still fires when the owner has a do-not-disturb window open (`quiet.json`);
  `high` and below are intentionally **dropped** for the duration (not deferred). `Call Me`
  call-entries pierce on their own. See `reminders-policy.md` → "Quiet window."
- **`Call Me` items → also ring the owner.** For any fired reminder with `call_me: true`, enqueue a
  **second**, call-channel entry a few minutes out, phrased for the ear (no ⏰ prefix — it's spoken):
  `python ../../seneschal/scripts/reminders_enqueue.py --text "It's your assistant. <the one thing>."
  --channel call --id rmd-<YYYY-MM-DD>-<slot>-call --due-at <due+~3min, UTC>`. Rare + opt-in; the daemon
  speaks it via the Worker `/push-call` (and falls back to Telegram if calls aren't configured). See the
  `Call Me` escalation in `reminders-policy.md`.
- **Write the tracker** (act-low): `store-update` fired items `status: reminded`, `last_reminded: today`,
  `reminded_today: true`; persist any reset/miss/ack changes from Phase 2.

**Phase 4 — Trace.** Write a Run Log row (`Mode = Reminders`, counts: items surfaced / acked / ribbed,
`Status`) and carry forward still-open important items (mirror to `../../seneschal/state/run-log.md` +
`../../seneschal/state/carry-over.md`).

## Guardrails

- **Never auto-flip a linked Task/Goal to Done** — that's **ask-high**; propose it ("mark *Ship the
  quarterly report* done too?"). The Reminders tracker itself is act-low.
- **Rib is low-importance only**, ≤ one line per reminder per day, factual ("0 for 5 days"), never
  shaming. Critical/High items are re-fired, never ribbed.
- **Persistent ≠ pushy.** Important items re-fire with the *same* calm tone; persistence is in frequency,
  not sharpness.
- **Cite, don't invent.** Every item traces to a Reminders row / linked Task / Goal.
- **Reversible writes only** without asking. Times + date logic in the owner's configured timezone.
- **Signal over noise** — hand the owner three real things, not ten.
