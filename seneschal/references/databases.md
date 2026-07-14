# Databases & Pages — the assistant's Notion map

> **Placeholder IDs.** Every collection/page id in this file is a placeholder of the form
> `00000000-0000-0000-0000-0000000000NN`. The store setup flow replaces them with your workspace's real
> Notion ids in a **local (gitignored) copy** of this file — the tracked copy always keeps the
> placeholders.

The Notion databases the assistant's **own** modes (Brief, Wrap, Triage, Ask) read and write directly.
**Do not fetch these schemas at runtime** — use what's here.

> **Canonical full map:** every journal database (Journal Transcripts, Achievements, Mood, Goals,
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
| Interstitial Journal | `00000000-0000-0000-0000-000000000010` | Working journal page. Its top **carry-over callout** is a prime source for the morning Brief. |

> **Journal-activity signal (for the journal-presence reminder gate).** The owner's daily writing lives
> on the **Interstitial Journal** page as **top-level date toggles** — `<details><summary><mention-date
> start="YYYY-MM-DD"/></summary>` with timestamped entries nested under each. **"The owner journaled
> today"** = a top-level date toggle whose `start` equals today (the owner's configured timezone) with
> **≥ 1 entry** under it. The `📌` **carry-over callout** at the very top is **the assistant's own**
> (written during the Brief) — *not* evidence the owner wrote. Consumed by the journal-presence gate in
> `reminders-policy.md`.

## Tasks — `collection://00000000-0000-0000-0000-000000000001`
- `Task name` **(title)**
- `Status` (status: `Not Started`, `In Progress`, `Paused`, `Done`, `Archived`)
- `Due` (date) · `Priority` (select: `Low`, `Medium`, `High`)
- `Completed` (date) — **set when a task is marked `Done`** (records *when*; the real completion timestamp).
  Project/query as `date:Completed:start`.
- `Last Updated` (last_edited_time) — auto; no manual upkeep.
- `Tags` (multi: `Mobile`, `Website`, `Improvement`) · `Summary` (text)
- `Project` (rel → Projects) · `Parent-task`/`Sub-tasks` (rel → self) · `Assignee` (person)

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
(recurring habits, deadline watches, one-offs). Lives on the **Interstitial Journal** page
(`00000000-0000-0000-0000-000000000010`, under Personal Home); database page id
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
  ordering + rib eligibility.
- `Nag Until Done` (checkbox) — **independent of importance**; forces re-firing until `Done` even for
  low-stakes items (e.g. water the plants). An item re-fires when `Importance` ∈ (Super-Critical, Critical,
  High) **OR** `Nag Until Done = true`. See `reminders-policy.md`.
- `Call Me` (checkbox) — **independent of importance** (like `Nag Until Done`); opts this reminder into
  **phone-call escalation**. When it's due, the assistant also *rings the owner's phone* with a spoken
  line (the presence daemon routes it `push_call.py` → Worker `/push-call`). For the rare can't-miss
  item; see `reminders-policy.md`.
- `Status` (select: `Pending`, `Reminded`, `Done`, `Finished`, `Skipped`, `Snoozed`, `Paused`) — **two
  distinct "acked" values, split by intent, not by `Type`:** **`Done` = done *for today*** (every `Type`,
  habits and one-time items alike) — the item stays an **active** reminder and re-fires on its next due
  cycle; **`Finished` = retired** — the explicit-only terminal value that drops the row out of the
  owner's *active*-reminders filter and stops it re-firing. A plain ack (chat, slot, or `Ack` tick)
  always writes `Done`; `Finished` is written **only** when the owner explicitly says they're *finished*
  with the item. Both count as "done today" for the Wrap.
- `Cadence` (select: `Daily`, `Weekdays`, `Every 3 days`, `Every 5 days`, `Weekly`, `Multiple/day`,
  `One-off`) — interval/Weekly/One-off rows go due by date, not the daily reset (`reminders-policy.md`).
- `Time Window` (select: `Morning`, `Midday`, `Evening`, `Bedtime`, `Anytime`) — maps a habit to a slot.
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
  input, not a record:** the next reconciling run (any reminder slot, or chat) consumes it — `Status =
  Done`, `Last Acknowledged = today`, `Consecutive Misses = 0` — then **unticks it** so a stale tick
  can't auto-complete a later cycle. Agents write the fields directly and never treat an unticked `Ack`
  as "not done"; read `Last Acknowledged`/`Status` instead (`reminders-policy.md`).
- `Related Task` (rel → Tasks) · `Related Goal` (rel → Goals) ·
  `Related Flag` (rel → Important Flags) · `Notes` (text — Weekly rows may name a weekday here)

**Today-Todo / Deadline-Watch never copy content** — they carry a relation + `Due / Target`; the linked
record's `Status` is the source of truth for "done." **Date projection gotcha applies:** filter/sort on
`"date:Due / Target:start"` and `"date:Last Reminded:start"`, not the bare names (see the Tasks note above).

---

## 🧭 Run Log — `collection://00000000-0000-0000-0000-000000000008`

A **dedicated** database (not reuse of the journal's Agent Run Log), so assistant runs stay cleanly
separate from journal runs. Lives under **Personal Home** (page
`00000000-0000-0000-0000-000000000012`). The assistant's across-run memory; written incrementally per
run (see `memory.md`; the local mirror is `../state/run-log.md`).

- `Date` **(title)** — run label, e.g. `2026-06-28 Brief`
- `Run Date` (date) — project/query as `date:Run Date:start`
- `Mode` (select: `Brief`, `Wrap`, `Triage`, `Ask`, `Reminders`) — add the `Reminders` option at runtime.
- `Status` (select: `Success`, `Partial`, `Failed`)
- `Items Surfaced` (number) · `Actions Taken` (number) · `Drafts Held` (number)
- `Actions Summary` (text) · `Carry-Over Context` (text) · `Issues / Uncertainties` (text)
- **Page body** = incremental phase log.
