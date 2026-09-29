---
name: reminders
description: >-
  The assistant's reminders & nudges. Tracks the things the owner wants to be reminded of — recurring
  habits (water the plants, stretch), today's unconfirmed todos, and goals/tasks nearing a deadline —
  expects a response, remembers what went unacknowledged, re-fires `nag_until_done` items until
  confirmed, and gently ribs repeated low-stakes skips. Use for "remind me to…", "did I do X", "what's
  still open", or the scheduled intraday runs. Delegated to by the seneschal orchestrator (Reminders
  mode).
compatibility: Requires a configured seneschal store (run /setup-store). The Notion backend additionally requires the Notion MCP (mcp__*__notion-*). Enqueues nudges via seneschal/scripts/reminders_enqueue.py → state/reminders.json; the presence daemon delivers them over Telegram/Discord — or rings the owner (call channel) for `Call Me` items (act-low, to the owner's own channels).
---

# Reminders & Nudges (Seneschal · Reminders mode)

Keep the owner's reminders honest in the persona's voice: brief, persistent on what matters, dry on the
small repeated misses, **never shaming**. State lives in the **Reminders** domain of the store; behavior
(escalation, tone, exact times) is governed by `../../seneschal/references/reminders-policy.md` — follow it
exactly. Reads and Reminders tracker writes are **act-low**; flipping a *linked* Task/Goal to Done is
**ask-high**.

The **scheduled** run is the once-per-day **seed pass** (`presence.maybe_seed_day`, spawned on the first
tick of each new owner-local date) — it runs the daily reset and enqueues the whole day's exact-time
nudges (the Seed steps below); the push + Run Log write attach there. The **Wrap** + **Dream** runs carry
the journal-presence gate. Each prompt inlines the state machine so it runs unattended. Invoked
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
- `../../seneschal/references/reminders-policy.md` — exact times + the daily seed, state machine,
  importance rule, rib threshold, tone ladder.
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

**Phase 0 — Orient.** Determine "today" in the owner's configured timezone — after-midnight counts as
the prior day, and the cut is the owner's **day-boundary hour** (`owner.dayBoundaryHour` in
`persona/identity.json`, default 05:00; `../../seneschal/scripts/activity_day.py` is the one definition,
and `reminders_dequeue.py` takes it as `--activity-day`) — and which run this is: the daily **seed**
(the once-per-day whole-day enqueue + reset), the **Wrap/Dream** journal-presence check, or an
**interactive** ask ("remind me to…", "what's still open"). **If the Reminders domain isn't provisioned
yet, say so and stop** — there's nothing to track until it exists (see `databases.md` /
`store/<backend>/schema.md`).

**Phase 1 — Read state (one parallel batch):**
- **`store-query` Reminders** rows that could fire: status in (pending, reminded, snoozed) and (cadence
  applies today **or** due/target ≤ today). Read the `ack` + `last_acknowledged` fields to catch acks
  since the last run. Also pull the `related_task` / `related_goal` relation refs — you'll join them
  locally below. (On the Notion backend the due filter is the `date:Due / Target:start` projection and
  relations come back as JSON URL arrays — see `../../seneschal/store/notion/mapping.md`.)
- **Linked Tasks/Goals — `store-get` by ref (capped), NOT another `store-query`.** The linked `status`
  is the real "done" truth (don't trust a stale Reminders row over its source). Read each ref pulled
  above with **`store-get` by ref**, **capped to ~3 concurrent**. A reminder set usually has only a
  handful of linked rows; if a set has **none**, zero reads. Why get-by-ref and not a broad query — the
  two throttles on the Notion backend, and why this corrects the older "one broad query per DB"
  guidance: `../../seneschal/store/notion/mapping.md` → Throughput, which names this exact case.
- Last Run Log carry-over (so important open items aren't dropped).

**Phase 2 — Reconcile** (per `reminders-policy.md`):
- **Apply acks FIRST — before any reset.** An ack arrives three ways: the one-tap `ack` flag set, the row
  already written through by chat (`status: done` + `last_acknowledged` set), or the linked record now
  done. Apply via **`store-update` — `status: done` for *every* type** (Recurring Habit, Today Todo,
  Deadline Watch): `done` = done *for today*, still an **active** reminder that re-fires on its next due
  cycle (a one-time item comes back tomorrow while still in-window). Set `status: finished` (retire —
  drops out of the active-reminders filter, stops re-firing) **only** on the owner's explicit "I'm
  finished with X"; a plain ack is never `finished`. (The seed's One-off auto-retire below is the one
  other writer of `finished`, and it is not an ack path.) Both set `last_acknowledged: today`,
  `consecutive_misses: 0`, then **reset `ack`** — it's a one-shot input, consumed on read, so a stale
  flag can't auto-complete a later cycle. In the **seed** pass, an ack made since the last run belongs
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
- **An ack in the owner's own words goes through the front door, `../../seneschal/scripts/ack.py`**
  (interactive runs, and any chat that relays one): `python ../../seneschal/scripts/ack.py "<their
  phrase>" ["<another>"]` resolves the words to Reminders rows, journals the ack (on the Notion backend,
  through the outbox — `outbox.py ack`), and dequeues that activity day's un-fired nudges for the row.
  It **refuses** an ambiguous phrase (exit 3) rather than guessing — add the phrasing to
  `../../seneschal/references/reminder-aliases.json` that same turn. **It never writes the store
  itself** (stdlib, no MCP): read its JSON, take the `reminder_id`, do the `store-update` above, and on
  the Notion backend `outbox.py mark --id <id> --done --notion-page-id <row id>` — finishing that write
  is THIS turn's job, not a later one's. A same-day 👍 on a nudge already ran all of this.
- **Journal-presence-gated habits (auto-satisfy from journal activity).** For any Recurring Habit whose
  `notes` contains `[gate: journal-presence]` (the canonical case: **Write in the journal**), before
  treating it as a fire candidate, `store-get` the Interstitial Journal page and check for a **top-level
  date toggle for today** (the owner's configured timezone) with ≥1 timestamped entry (ignore the
  assistant's own carry-over callout). If the owner journaled today → **auto-satisfy like an ack**
  (`status: done`, `last_acknowledged: today`, `consecutive_misses: 0`) and **don't nudge** — never nudge
  a thing already done today. Only nudge once they've had `quiet_days` consecutive quiet days (default
  `1`). **The daily seed skips these gated habits** (their fire is conditional on this late check); run
  it in the **Wrap** run (nudge-or-satisfy) + **Dream** (satisfy-only backstop). Full gate, incl. what
  the toggle must look like: `reminders-policy.md` → "Journal-presence gate."
  - **Never compute an age from the fetch's "as of" stamp** — it is the last-edited time of the newest
    block *still surviving on the page*, i.e. content, not a read time, and the read is live: there is
    no cache. Subtracting it inverts the gate in **both** directions (`reminders-policy.md` →
    "Journal-presence gate").
- **Seed pass only — auto-retire completed One-offs FIRST, before anything is enqueued.** For each
  Reminders row where **all four** hold — `cadence: one-off`, `status: done`, `last_acknowledged` set
  **and** `>= due`, and `due` **strictly before** today's owner-local date — `store-update`
  it to `status: finished`, leave `last_acknowledged` + `consecutive_misses` **untouched**, and enqueue
  **nothing** for that row. **Condition 3 is the safety rail and is not optional** — it is the positive
  evidence the owner did the thing; without it the rule silently `finished`es work they still owe, which
  is *worse* than the re-firing it fixes (that failure is noisy, this one is silent). **Nothing else is
  in scope** — never a **linked** Task/Goal (ask-high, always), any other cadence or status, an unacked
  row, or a row due **today**. **Log every retirement in the Run Log** — title, `due`,
  `last_acknowledged`, one line each. The full rule: `reminders-policy.md` → "Auto-retire a completed
  One-off (seed pass)."
- **Seed pass only:** run the **daily reset** for `Daily`/`Weekdays` habits — increment
  `consecutive_misses` for yesterday's unacked (a row just acked above is **not** a miss), then set them
  `status: pending` + clear `reminded_today` (leave `last_acknowledged` alone — it's the Wrap's
  evidence). Never zero misses here. Interval / rate / `Weekly` / `One-off` rows aren't reset daily —
  flip them to `status: pending` when they are due. **`cadence` is an EXPRESSION and the due-test is a
  SCRIPT, not arithmetic you do in prose** — `python ../../seneschal/scripts/reminders_cadence.py --due
  "<cadence>" --last-ack <date> [--notes …] [--due-target …] [--lookahead-days …]` (act-low, local, no
  network) answers `due` + `next_due` for every shape, including `every 11 days` and rates like
  `2 per week`. **A cadence it cannot parse FAILS OPEN — treat the row as due and tell the owner it did
  not parse; never drop it.**
  - **A `One-off` row's early-surface window is gated by `type` — pass `--lookahead-days` yourself,
    never let the default apply blind.** `type: today-todo` → `--lookahead-days 0` (an appointment is
    not due until its own day — a Monday appointment must never fire the Friday before). `type:
    deadline-watch` → omit the flag (the default ~3-day early surface is right for a deadline). Applying
    the Deadline Watch lookahead to every `One-off` regardless of `type` makes a Today Todo fire days
    early. `reminders-policy.md` → "Cadence — when a row is 'due today'".
- **Seed pass only — verify the reset landed, don't just report it.** After the reset and the per-row
  seeding, run `python ../../seneschal/scripts/reminders_seed.py --audit-day --reminder-id x --text x`
  (**argparse requires those two even for the audit**; act-low, local, no network) and put any `!` lines
  in the Run Log verbatim — a reported reset that did not persist is indistinguishable from a real one
  unless something on disk disagrees. **Never auto-mark an unacked row done to clear a flag** — that is
  the hazard being fixed, not the fix. The silent skip this detector exists for (several Daily rows
  quietly never seeded, one of them critical), and why it compares each row against its own seeding
  history rather than the store: `../../seneschal/scripts/reminders_seed.py`'s module docstring.
- **Compute the day's fires (seed):** every `pending` row due today, at its `times` (or the `time_window`
  default). Rows with **`nag_until_done: true`** — that flag **alone**, never off `importance` — also get
  the **90-min re-fire ladder** through end of day (Phase 3). `snoozed` items re-surface `+30 min` out.
- **Rib candidates:** `importance: low`/`notable` habits **with `nag_until_done: false`** and
  `consecutive_misses >= 3` (one dry line, once/day). Nag and important items are never ribbed.

**Phase 3 — Deliver (act-low):**
- **Seed run — enqueue the whole day per row via `reminders_seed.py`.** For each due Reminders row, one
  call queues that row's exact-time nudge(s) for the day (primary time(s) from `times`, else the
  `time_window` default) **plus** the 90-min re-fire ladder for a `nag_until_done` row (`--nag`):
  `python ../../seneschal/scripts/reminders_seed.py --reminder-id <the row's ref/id> --slug <short-stable>
  --text "<one line>" --times "08:00, 20:00" [--time-window Morning] [--importance "🚨 Critical"] [--nag]
  [--pierce-quiet] [--due-target <due>] [--consecutive-misses <consecutive_misses>]`. It writes
  `../../seneschal/state/reminders.json` idempotently and the daemon fires each at its due minute (queue
  shape + `fired_at` + Dream's prune: `../../seneschal/state/README.md`). **Copy its stderr `!` lines
  into the Run Log verbatim** — a high-importance row seeded without `--nag` says so there, and so does a
  row due for a **premise-review question** (`../../seneschal/docs/reminder-premise-spec.md`, computed by
  `../../seneschal/scripts/reminder_premise.py` — "is this still a thing?", never a verdict; just relay
  the line, never act on it yourself). Compose **one short line per reminder** in the persona's voice —
  never bundle (`reminders-policy.md` → "One reminder per nudge"); append at most one rib line if
  warranted. Honor the tone ladder; no walls of text. End each `--text` with the v1 ack hint: *"tick Ack
  or tell me."*
  - **Always pass `--due-target` for a `One-off` row (Today Todo or Deadline Watch).** When this seed's
    fire lands on a day other than the row's own `due` — the Deadline Watch lookahead firing
    early, or either type re-firing overdue — the script itself appends `" — due <Weekday MM-DD>."` to
    the text (`reminders_seed.due_suffix`), so *"Renew the passport. tick Ack…"* fired ahead of time
    reads *"Renew the passport. — due Mon 09-14. tick Ack…"* — a heads-up, not a do-it-now. Firing on
    the due day itself adds nothing. Don't hand-compose this suffix in the `--text` you write; the
    script computes it from the fire day so it's never wrong.
- **Interactive / ad-hoc / snooze — `reminders_enqueue.py` for a single exact-time nudge.** For a one-off
  "remind me at 3pm", a **snooze** (`--due-at` = `now + 30 min`), or a `Call Me` ring, enqueue a single
  entry with its `--due-at` (UTC) + `--reminder-id <the row's ref/id>` + its own stable `--id`; surface
  the nudge in chat too when interactive. If the daemon isn't running the entry simply waits in the queue.
- **Quiet-window pierce.** Add `--pierce-quiet` for `importance: critical` / `super-critical` so the
  entry still fires through an open do-not-disturb window (`quiet.json`); `high` and below are
  **dropped** for the duration, not deferred. `Call Me` entries pierce on their own.
  `reminders-policy.md` → "Quiet window."
- **`Call Me` items → also ring the owner.** For any fired reminder with `call_me: true`, enqueue a
  **second**, call-channel entry a few minutes out, phrased for the ear (no ⏰ prefix — it's spoken):
  `python ../../seneschal/scripts/reminders_enqueue.py --text "It's your assistant. <the one thing>."
  --channel call --id rmd-<YYYY-MM-DD>-<slug>-call --due-at <due+~3min, UTC>`. Rare + opt-in. The
  wiring, the idempotent `-call` id and the Telegram fallback when calls aren't configured:
  `reminders-policy.md` → "Phone-call escalation — the `Call Me` flag."
- **Write the tracker** (act-low): `store-update` fired items `status: reminded`, `last_reminded: today`,
  `reminded_today: true`; persist any reset/miss/ack changes from Phase 2.

**Phase 4 — Trace.** Write a Run Log row (`Mode = Reminders`, counts: items surfaced / acked / ribbed,
`Status`), **list every One-off auto-retirement** from Phase 2 (fields as listed there — a silent
retirement is unauditable), and carry forward still-open important items (mirror to the two live files
named under "Read first").

## Guardrails

- **Never auto-flip a linked Task/Goal to Done** — **ask-high**; propose it ("mark *Ship the quarterly
  report* done too?"). The tracker itself is act-low.
- **Rib is low-importance non-nag only** (candidates above), ≤ one line per day, factual ("0 for 5
  days"), never shaming.
- **Persistent ≠ pushy.** `nag_until_done` items re-fire in the *same* calm tone; persistence is
  frequency, not sharpness.
- **Cite, don't invent.** Every item traces to a Reminders row / linked Task / Goal.
- **Reversible writes only** without asking. Times + date logic in the owner's configured timezone.
- **Signal over noise** — three real things, not ten. Full list: `reminders-policy.md` → "Guardrails."
