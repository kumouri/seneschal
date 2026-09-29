# Databases & Pages — the assistant's Notion map

> **Placeholder IDs.** Every collection/page id in this file is a placeholder of the form
> `00000000-0000-0000-0000-0000000000NN`. The store setup flow replaces them with your workspace's real
> Notion ids in a **local (gitignored) copy** of this file — the tracked copy always keeps the
> placeholders.

The Notion databases the assistant's **own** modes (Brief, Wrap, Triage, Ask) read and write directly.
**Do not fetch these schemas at runtime** — use what's here.

> **This is the Notion backend's registry.** Skills speak the backend-neutral store verbs
> (`../store/README.md`) and canonical, emoji-free option values; each backend's **domain map** is
> `../store/<backend>/schema.md` (Notion: `../store/notion/schema.template.md`, rendered to a
> gitignored `schema.md` by `/setup-store`; filesystem backends: `../store/obsidian/schema.md`,
> `../store/markdown/schema.md`). The Notion-only mechanics below — `notion-*` tools, emoji option
> strings, `collection://…` ids, `date:X:start` projections, the outbox — apply **on the Notion backend
> only**; the verb → tool translation is `../store/notion/mapping.md`. A field added here (e.g.
> `Tomorrow`) must be added to every backend's schema too.

> **Canonical full map:** every journal database (Achievements, Mood, Goals,
> Run Log, …) lives in `../../subagents/journal-steward/daily-journal-steward/references/databases.md`.
> That file is the source of truth for journal-owned databases; this file only restates the handful the
> assistant touches outside the journal pipeline, plus the assistant's own Run Log. If a schema here
> ever conflicts with the canonical map, the canonical map wins — sync them.

Notation matches the canonical map: `(title)`, `(date)`, `(text)`, `(number)`, `(url)`,
`(select: …)`, `(status: …)`, `(multi: …)`, `(rel → X)`. For HOW to set each type via MCP, see the
journal-steward's `notion-mcp-mapping.md`.

## Key pages

| Page | ID | Role |
|------|----|------|
| Personal Home | `00000000-0000-0000-0000-000000000009` | Top-level hub; parent of People, Tasks, Projects. |
| Interstitial Journal | `00000000-0000-0000-0000-000000000010` | Working journal page. Its top **carry-over callout** is a prime source for the morning Brief. Its tracking databases live one level down, under **IJData**. |
| IJData | `00000000-0000-0000-0000-000000000022` | Container page — the top child of the Interstitial Journal. **Parent of all the tracking databases** (incl. ⏰ Reminders), moved off the Journal page itself. |

> **Journal-activity signal (for the journal-presence reminder gate).** The owner's daily writing lives
> on the **Interstitial Journal** page as **top-level date toggles** — `<details><summary><mention-date
> start="YYYY-MM-DD"/></summary>` with timestamped entries nested under each. **"The owner journaled
> today"** = a top-level date toggle whose `start` equals today (the owner's configured timezone) with
> **≥ 1 entry** under it. The `📌` **carry-over callout** at the very top is **the assistant's own**
> (written during the Brief) — *not* evidence the owner wrote. Consumed by the journal-presence gate in
> `reminders-policy.md`.
>
> **The `as of …` a `notion-fetch` prints is NOT a read time — it is the last-edited time of the newest
> block still surviving on the page.** So a rarely-touched page reads *old* on a **cold** fetch (weeks
> out, on a page nobody has edited), a fresh write shows up within seconds, and **deleting that write
> moves the stamp backwards** — which a clock cannot do. There is no page cache: every fetch is live.
> Never subtract it from `now` and call the result staleness; that inverts the journal gate in both
> directions (`reminders-policy.md` → "Journal-presence gate"). A true monotonic last-edited does
> exist, on `notion-search`'s per-result `timestamp`.

## Tasks — `collection://00000000-0000-0000-0000-000000000001`
- `Task name` **(title)**
- `Status` (status: `Not Started`, `In Progress`, `Paused`, `Done`, `Archived`)
- `Due` (date) · `Priority` (select: `Low`, `Medium`, `High`)
- `Completed` (date) — **set when a task is marked `Done`** (records *when*; the real completion timestamp).
  Project/query as `date:Completed:start`.
- `Last Updated` (last_edited_time) — auto; no manual upkeep.
- `Tags` (multi: `Mobile`, `Website`, `Improvement`) · `Summary` (text)
- `Project` (rel → Projects) · `Parent-task`/`Sub-tasks` (rel → self) · `Assignee` (person)
- `Tomorrow` (checkbox) — canonical field `tomorrow` (`../docs/tomorrow-marker-spec.md`): a checkbox,
  matching `Nag Until Done`/`Call Me`'s boolean-flag convention on ⏰ Reminders below. A **one-shot
  input, consumed on reconciliation, exactly like ⏰'s `Ack`**: the next Brief or Wrap gather that reads
  Tasks also checks `Tomorrow = true`, upserts an entry into `../state/tomorrow.json` keyed on the row's
  id (`../scripts/tomorrow_marker.py reconcile`), and **unticks the box** the same pass. Ticking it is
  act-low, silent — it does not itself change `Priority`/`Due`, only what the next morning's Brief
  leads with and what the Wrap asks about. **If the property is missing or the query on it fails,
  reconciliation fails OPEN** — it never blocks the Brief or Wrap, it just folds in nothing that pass.

**Briefing reads:** open tasks with a due date on/before today and `Status` not in (`Done`,`Archived`)
→ "due/overdue today".

> **SQL projection gotcha (verified):** in `notion-query-data-sources`, a **date** property projects as
> three columns — `date:Due:start`, `date:Due:end`, `date:Due:is_datetime` — **not** a column named
> `Due`. Querying `"Due"` errors with *no such column*. Filter/sort on **`"date:Due:start"`** (ISO
> string, comparable directly). Status/select/title columns use their plain names (`"Task name"`,
> `"Status"`, `"Priority"`). Example:
> `SELECT "Task name","Status","date:Due:start" FROM "collection://00000000-0000-0000-0000-000000000001"
> WHERE "Status" NOT IN ('Done','Archived') AND "date:Due:start" IS NOT NULL AND "date:Due:start" <=
> '2026-06-28' ORDER BY "date:Due:start"`. (A long-lived Tasks DB accumulates a large undated backlog —
> the date filter is what keeps the brief to genuinely due/overdue items.)
>
> **More projection notes (verified):** `status`/`select`/`title` use plain names + exact option
> strings. A **multi-select** projects as a **JSON-array string** (e.g. Goals `Category` →
> `["🔧 Hobbies & Tech"]`) — match with `LIKE '%…%'` or parse client-side. **relation** columns
> project as a JSON array of page URLs. Every row also carries `id` and `url` — use `url` for citations.

## Projects — `collection://00000000-0000-0000-0000-000000000002`
- `Project name` **(title)**
- `Status` (status: `Backlog`, `Planning`, `In Progress`, `Paused`, `Done`, `Canceled`)
- `Priority` (select: `Low`, `Medium`, `High`, `Critical`)
- `Category` (multi: `Home`, `Work`, `Hobby`) · `Summary` (text) · `Dates` (date)
- `Tasks` (rel → Tasks) · `Owner` (person) · `Blocked By`/`Is Blocking` (rel → self)

**Briefing reads:** `Status = In Progress` (optionally `Planning`) → "active projects".

## ⭐ Important Flags — `collection://00000000-0000-0000-0000-000000000003`
- `Flag` **(title)** · `Date Flagged` (date)
- `Type` (select: `💼 Work`, `👨‍👩‍👧 Family`, `💜 Relationship`, `🧠 Internal / Feelings`, `🩺 Health`,
  `💰 Finances`, `🏠 Home / Life`, `🎨 Hobby / Creative`, `🤝 Friends / Social`, `Other`)
- `Importance Level` (select: `🚨 Critical`, `⭐ High`, `✨ Notable`, `📌 Reference`)
- `Status` (status: `Active`, `Carrying Over`, `Resolved`, `Archived`)
- `Context` (text) · `Why It Matters` (text) · `Related Task`/`Related Project` (rel) · `Journal Digest Link` (url)

**Briefing reads:** `Status` in (`Active`,`Carrying Over`) → surface in the brief, ordered by
`Importance Level`. (The journal-steward owns the flag *lifecycle*; the assistant only reads them for
briefs.)

## People — `collection://00000000-0000-0000-0000-000000000004`
- `Name` **(title)** · `Nickname`/`Aliases` (text) · `Relationship` (select) · `Email`/`Phone` ·
  `Birthday` (date) · `Notes` (text). Use to resolve who an email/Slack sender is, and for birthday nudges.

## 🎯 Goals — `collection://00000000-0000-0000-0000-000000000005`
The assistant reads Goals for briefs; Goal writes are **ask-high** (full schema in the canonical map).
Useful `Category` values group the owner's life areas (Work / Health & Self-Care / Hobbies & Tech /
Finances / …).

## ⏰ Reminders — `collection://00000000-0000-0000-0000-000000000007`

The assistant's **own** tracker for things the owner wants reminding of (recurring habits, today's
unconfirmed todos, deadlines approaching). **Provisioned + seeded at setup** with an initial set of rows
(recurring habits, deadline watches, one-offs). Lives under the **IJData** page
(`00000000-0000-0000-0000-000000000022`, a child of the **Interstitial Journal**
`00000000-0000-0000-0000-000000000010` under Personal Home); database page id
`00000000-0000-0000-0000-000000000011`. Behavior lives in `reminders-policy.md`.

> **Ack by cached id — skip the query.** Every row's stable **page id** lives in the live cache table
> **`../state/reminders-id-cache.md`** (gitignored, runtime-updated; a tracked example ships at
> `../state/reminders-id-cache.example.md`). When the owner acks a reminder in chat, match their phrase
> to that table and write `Status = Done` directly by id (`notion-update-page`) — **do not** run a
> `notion-query-data-sources` lookup first. The SQL query path is what the `collection_router_upstream_429`
> throttle hits (`notion-rate-limits.md`); writes-by-id sidestep it entirely. Fall back to a single
> query only on a cache miss / stale id, then refresh the cache.

- `Reminder` **(title)** — e.g. `Water the plants (AM)`, `Eat lunch`, `Ship the quarterly report`
- `Type` (select: `Recurring Habit`, `Today Todo`, `Deadline Watch`)
- `Importance` (select: `🛑 Super-Critical`, `🚨 Critical`, `⭐ High`, `✨ Notable`, `📌 Low`) — drives nudge
  ordering, rib eligibility and the quiet-window pierce (Critical-and-above). **Not** the re-fire ladder.
- `Nag Until Done` (checkbox; canonical `nag_until_done`) — **independent of importance**, literally:
  this box **alone** decides whether a row re-fires until `Done` (the 90-min ladder), whatever its
  `Importance` — so a low-stakes item (water the plants) can nag, and a High item need not. It used
  to be `Importance ≥ ⭐ High` **OR** this box; an install migrating from that rule ticks the box on its
  active High-and-above rows first so the change is behaviour-preserving. See `reminders-policy.md`.
- `Call Me` (checkbox) — **independent of importance** (like `Nag Until Done`); opts this reminder into
  **phone-call escalation**. When it's due, the assistant also *rings the owner's phone* with a spoken
  line (the presence daemon routes it `push_call.py` → Worker `/push-call`). For the rare can't-miss
  item; see `reminders-policy.md`.
- `Status` (select: `Pending`, `Reminded`, `Done`, `Finished`, `Skipped`, `Snoozed`, `Paused`) — **two
  distinct "acked" values, split by intent, not by `Type`:** **`Done` = done *for today*** (every `Type`,
  habits and one-time items alike) — the item stays an **active** reminder and re-fires on its next due
  cycle; **`Finished` = retired** — the terminal value that drops the row out of the owner's
  *active*-reminders filter and stops it re-firing. A plain ack (chat, slot, or `Ack` tick) always
  writes `Done`. Only two things write `Finished`: the owner explicitly saying they're *finished* with
  the item, and the seed's **One-off auto-retire** — a `Cadence = One-off` row at `Done` whose `Last
  Acknowledged` is set and `>= Due / Target`, with `Due / Target` already past (`reminders-policy.md`
  → "Auto-retire a completed One-off"). Both count as "done today" for the Wrap.
- `Cadence` — **an EXPRESSION, parsed by `../scripts/reminders_cadence.py`; not a closed list.**
  Seven shapes: `daily`, `weekdays`, `every N days` (**any** positive whole N), `N per D days`
  (a rate — `2 per week`), `weekly`, `multiple/day`, `one-off`. Interval / rate / `weekly` /
  `one-off` rows go due by date, not the daily reset; **an interval is ack-relative and a rate is
  grid-anchored**, fractional days are refused, and an unparseable value fails open (due today,
  reported). The whole rule, with the reasoning: `reminders-policy.md` → "Cadence — when a row is
  'due today'". A `One-off` that was acked on/after its due date is **auto-retired to `Finished`**
  at the seed once that date is past, so a completed one-time task can't resurrect.

  **On Notion the property may still be a `select`, and every stock option (`Daily`, `Weekdays`,
  `Every 2/3/4/5 days`, `Weekly`, `Multiple/day`, `One-off`) is a valid expression that parses to
  exactly what it has always meant** — so existing rows need no migration and no flag day. What a
  select cannot hold is a value nobody added to it: each new interval would otherwise cost a Notion
  edit plus doc edits to express one integer. **Converting `Cadence` to a `text` property is what makes
  an arbitrary expression typeable**; it is optional, it changes no behaviour on its own, and it is the
  owner's to do — runbook and costs in `../docs/reminder-cadence-mechanism-spec.md`. Filesystem
  backends store the expression as plain text already.
- `Times` (text) — **the reminder's exact fire time(s):** a comma-list of `HH:MM` owner-local times
  (e.g. `08:00` or `08:00, 20:00`), enqueued for the whole day by the daily **seed**
  (`../scripts/reminders_seed.py`). Empty ⇒ fall back to the `Time Window` default below. *(Additive
  property — until a row has `Times` it uses `Time Window`, so migration is zero-regression. See
  `reminders-policy.md` → "Exact per-reminder times + the daily seed.")*
- `Time Window` (select: `Morning`, `Midday`, `Evening`, `Bedtime`, `Anytime`) — **coarse fallback
  sugar**, used only when `Times` is empty: maps to a default time (Morning 08:00 / Midday 12:30 /
  Evening 18:30 / Bedtime 21:30 / Anytime 09:00). Kept for the "just make it an evening thing" shorthand.
- `Due / Target` (date) — for `Today Todo` (today) / `Deadline Watch` (the deadline).
- `Last Reminded` (date) · `Last Acknowledged` (date) — interval/Weekly cadences compute "due" off
  `Last Acknowledged`. **`Last Acknowledged` doubles as the durable "done that day" record** — it survives
  the daily reset and `Ack` consumption; the EOD Wrap counts "done today" from
  `"date:Last Acknowledged:start" = today` (+ `Status IN (Done, Finished)` — both are acked-done: `Done` is
  done-for-today, `Finished` is a retired item acked on its way out; `Status = Skipped` with today's date
  is an honest skip, not a done).
- `Consecutive Misses` (number) — increments per unacked cycle; **resets to 0 only on an ack** (`Done` or
  `Finished`).
- `Reminded Today` (checkbox) — intraday re-fire guard; cleared by the daily reset.
- `Ack` (checkbox) — **owner-facing** one-tap acknowledgment (the v1 two-way affordance). **One-shot
  input, not a record:** the next reconciling run (any reminder run, or chat) consumes it — `Status =
  Done`, `Last Acknowledged = today`, `Consecutive Misses = 0` — then **unticks it** so a stale tick
  can't auto-complete a later cycle. Agents write the fields directly and never treat an unticked `Ack`
  as "not done"; read `Last Acknowledged`/`Status` instead (`reminders-policy.md`).
- `Tomorrow` (checkbox; canonical `tomorrow`) — `../docs/tomorrow-marker-spec.md`: the same additive
  shape and the same one-shot/consumed-and-unticked contract `Ack` above already has. Independent of
  `Nag Until Done`/`Call Me`/`Importance` — a marked row keeps whatever nudge mechanics it already had;
  this box only feeds `../state/tomorrow.json` via `../scripts/tomorrow_marker.py reconcile`, never the
  reminder ladder. A missing/unreadable property fails OPEN, same as on Tasks above.
- `Related Task` (rel → Tasks) · `Related Goal` (rel → Goals) ·
  `Related Flag` (rel → Important Flags) · `Notes` (text — Weekly rows may name a weekday here)

**Today-Todo / Deadline-Watch never copy content** — they carry a relation + `Due / Target`; the linked
record's `Status` is the source of truth for "done." **Date projection gotcha applies:** filter/sort on
`"date:Due / Target:start"` and `"date:Last Reminded:start"`, not the bare names (see the Tasks note above).

**A row has no field for WHY it exists, and that is a named open gap, not an oversight.**
`../docs/reminder-premise-spec.md` proposes an optional `Premise` (text) + `Premise Last Reviewed At
Misses` (number) pair — neither is part of the provisioned schema yet; it is deferred to that spec's
own open questions, the same way the `Cadence`-to-text conversion above is deferred to
`reminder-cadence-mechanism-spec.md`.

---

## 🧭 Run Log — `collection://00000000-0000-0000-0000-000000000008`

A **dedicated** database (not reuse of the journal's Agent Run Log), so assistant runs stay cleanly
separate from journal runs. Lives under **Personal Home** (page
`00000000-0000-0000-0000-000000000012`). The assistant's across-run memory; written incrementally per
run (see `memory.md`; the local mirror is `../state/run-log.md`).

- `Date` **(title)** — run label, e.g. `2026-06-28 Brief`
- `Run Date` (date) — project/query as `date:Run Date:start`
- `Mode` (select: `Brief`, `Wrap`, `Triage`, `Ask`, `Reminders`, `Dream`, `Chat`, `Forge`, `Archive`) —
  provision all nine; an older database missing one gets the option added at runtime as that mode
  first logs.
- `Status` (select: `Success`, `Partial`, `Failed`)
- `Items Surfaced` (number) · `Actions Taken` (number) · `Drafts Held` (number)
- `Actions Summary` (text) · `Carry-Over Context` (text) · `Issues / Uncertainties` (text)
- **Page body** = incremental phase log.
