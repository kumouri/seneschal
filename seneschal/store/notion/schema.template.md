# Notion store — schema (template)

> **Placeholder ids — do not treat as real.** Every collection/page id below is a placeholder of the
> form `00000000-0000-0000-0000-0000000000NN`. This is the **tracked** copy; it always keeps the
> placeholders. `/setup-store` discovers the owner's real Notion ids once, at setup time, and renders
> them into a **gitignored** `schema.md` beside this file. Runtime code reads `schema.md`; this template
> is the shape it is rendered from. **Never fetch a DB schema at runtime** — trust `schema.md`.

This is the Notion backend's **domain map**: for each domain in the store contract it gives the Notion
collection/page id, the property list with Notion types, and a **translation table** from the
backend-neutral canonical option values to the exact Notion option strings.

**This file owns the emoji.** Canonical option values in skill prose are emoji-free, lowercase, and
hyphenated (`importance: critical`, `status: done`, `type: recurring-habit`). Notion's real option
strings often carry emoji and Title Case (`🚨 Critical`, `Done`, `Recurring Habit`). The verb layer
(`mapping.md`) speaks canonical; it looks up the exact Notion string **here** before writing. If an
option string is wrong or mis-cased, the Notion write fails — copy them exactly, emoji included.

**Fixed enums vs. owner-defined multi-selects.** The framework-branchable option sets
(status/priority/type/importance/cadence/time-window/valence) are **fixed** — identical, verbatim,
across all three backends. The multi-select fields (`category`, `tags`) are **owner-defined**: their
values are whatever the owner's own workspace uses. The slugs listed for them below are *illustrative*
(marked *(illustrative)*) — `/setup-store` reads the real options from the owner's databases and
records them here (adapt-and-record). Skills never branch on a specific category/tag value, so these
need not match across backends.

## Legend

`(title)` = the page-title property · `(date)` `(text)` `(number)` `(url)` `(checkbox)` `(person)` =
scalars · `(select: …)` = single-select, exact options · `(status: …)` = a Notion **status** property
(exact options) · `(multi: …)` = multi-select · `(rel → X)` = relation to collection X ·
`(last_edited_time)` = auto, never written.

> **Status-type vs. select quirk (verified).** Tasks / Projects / Flags / Goals `Status` are true Notion
> **status** properties. **Reminders `Status` and Run Log `Mode`/`Status` are plain `select`s**, not
> status properties. Both are set the same way via MCP (pass the exact option string), but the
> distinction matters if you ever inspect the raw schema — don't assume every "Status" is a status type.

---

## Key pages (non-database)

| Page | Placeholder id | Role |
|------|----------------|------|
| Personal Home | `00000000-0000-0000-0000-000000000009` | Top-level hub; parent of People, Tasks, Projects, Run Log. |
| Interstitial Journal | `00000000-0000-0000-0000-000000000010` | The working journal page — hosts the `journal` domain (date toggles + carry-over callout). Its tracking databases live one level down, under **IJData**. |
| IJData | `00000000-0000-0000-0000-000000000022` | Container page — the top child of the Interstitial Journal. **Parent of all the tracking databases** (incl. Reminders — the DB page `…0011` lives under it). Discover-or-create as a child of the Interstitial Journal (`…0010`). |
| Reminders DB page | `00000000-0000-0000-0000-000000000011` | The page that holds the Reminders database (its collection is `…0007`), under IJData. |
| Run Log DB page | `00000000-0000-0000-0000-000000000012` | The page that holds the assistant's Run Log database (its collection is `…0008`), under Personal Home. |
| Achievements Log | `00000000-0000-0000-0000-000000000014` | Append-only page: a running chronological list under `## <date>` headers (target of `store-append`). Distinct from the Achievements Tracker database `…0013`. |

> **Journal-activity signal (relied on by the reminder gate — preserve the shape).** The owner's daily
> writing lives on the Interstitial Journal page as **top-level date toggles**:
> `<details><summary><mention-date start="YYYY-MM-DD"/></summary>` with timestamped entries nested under
> each. **"The owner journaled today"** = a top-level date toggle whose `start` equals today (the owner's
> configured timezone) with **≥ 1 entry** under it. The `📌` **carry-over callout** at the very top is
> **the assistant's own** (written during the Brief) — *not* evidence the owner wrote.

---

## Core domains

### tasks — `collection://00000000-0000-0000-0000-000000000001`

| Canonical field | Notion property | Type | Notes |
|---|---|---|---|
| title | `Task name` | title | |
| status | `Status` | status | see table |
| due | `Due` | date | projects as `date:Due:start` — never a bare `Due` column |
| priority | `Priority` | select | see table (no emoji on Tasks) |
| completed | `Completed` | date | set when status→`done`; the real completion timestamp; query `date:Completed:start` |
| tags | `Tags` | multi | *(illustrative; owner-defined)* |
| summary | `Summary` | text | |
| project | `Project` | rel → projects `…0002` | |
| parent / subtasks | `Parent-task` / `Sub-tasks` | rel → self | |
| assignee | `Assignee` | person | |
| — | `Last Updated` | last_edited_time | auto; never write |

**status** — canonical → Notion: `not-started`→`Not Started` · `in-progress`→`In Progress` ·
`paused`→`Paused` · `done`→`Done` · `archived`→`Archived`.
**priority** — `low`→`Low` · `medium`→`Medium` · `high`→`High`. *(Tasks priority carries no emoji —
contrast Goals priority below.)*
**tags** *(illustrative; owner-defined)* — `mobile`→`Mobile` · `website`→`Website` · `improvement`→`Improvement`. New
options may be added by passing a new string.

### projects — `collection://00000000-0000-0000-0000-000000000002`

| Canonical field | Notion property | Type | Notes |
|---|---|---|---|
| title | `Project name` | title | |
| status | `Status` | status | see table |
| priority | `Priority` | select | see table (no emoji) |
| category | `Category` | multi | *(illustrative; owner-defined)* |
| summary | `Summary` | text | |
| dates | `Dates` | date | projects as `date:Dates:start` |
| tasks | `Tasks` | rel → tasks `…0001` | |
| owner | `Owner` | person | |
| blocked-by / blocking | `Blocked By` / `Is Blocking` | rel → self | |

**status** — `backlog`→`Backlog` · `planning`→`Planning` · `in-progress`→`In Progress` ·
`paused`→`Paused` · `done`→`Done` · `canceled`→`Canceled`.
**priority** — `low`→`Low` · `medium`→`Medium` · `high`→`High` · `critical`→`Critical`.
**category** *(illustrative; owner-defined)* — `home`→`Home` · `work`→`Work` · `hobby`→`Hobby`.

### flags — `collection://00000000-0000-0000-0000-000000000003`

| Canonical field | Notion property | Type | Notes |
|---|---|---|---|
| title | `Flag` | title | |
| date-flagged | `Date Flagged` | date | |
| type | `Type` | select | see table |
| importance | `Importance Level` | select | see table |
| status | `Status` | status | see table |
| context | `Context` | text | |
| why-it-matters | `Why It Matters` | text | |
| related-task / related-project | `Related Task` / `Related Project` | rel | |
| journal-link | `Journal Digest Link` | url | |
| — | `Resolution / Outcome` | text | Notion-only (no canonical field); journal-owned lifecycle |
| — | `Tags` | multi | Notion-only; empty by default |

**type** — `work`→`💼 Work` · `family`→`👨‍👩‍👧 Family` · `relationship`→`💜 Relationship` ·
`internal`→`🧠 Internal / Feelings` · `health`→`🩺 Health` · `finances`→`💰 Finances` ·
`home`→`🏠 Home / Life` · `hobby`→`🎨 Hobby / Creative` · `social`→`🤝 Friends / Social` · `other`→`Other`.
**importance** — `critical`→`🚨 Critical` · `high`→`⭐ High` · `notable`→`✨ Notable` ·
`reference`→`📌 Reference`.
**status** — `active`→`Active` · `carrying-over`→`Carrying Over` · `resolved`→`Resolved` ·
`archived`→`Archived`.

### people — `collection://00000000-0000-0000-0000-000000000004`

| Canonical field | Notion property | Type | Notes |
|---|---|---|---|
| title | `Name` | title | |
| aliases | `Nickname/Aliases` | text | |
| relationship | `Relationship` | select | free-form / workspace-defined — **no fixed option set**, so no translation table; pass the workspace's own strings |
| email | `Email` | email | |
| phone | `Phone` | phone | |
| birthday | `Birthday` | date | |
| notes | `Notes` | text | |

### goals — `collection://00000000-0000-0000-0000-000000000005`

| Canonical field | Notion property | Type | Notes |
|---|---|---|---|
| title | `Goal` | title | |
| status | `Status` | status | see table |
| category | `Category` | multi | see table *(illustrative)* |
| priority | `Priority` | select | see table (emoji) |
| target-date | `Target Date` | date | |
| target-value | `Target Value` | number | |
| current-value | `Current Value` | number | |
| unit | `Unit` | text | |
| — | `Specific` / `Measure` / `Achievable` / `Relevant To` / `Notes` | text | Notion-only |
| — | `Progress %` | number (percent 0–1) | Notion-only; often a formula — read, don't write |
| — | `Confidence` | select: `💪 High`, `🤔 Medium`, `😬 Low` | Notion-only |
| — | `Relevance Tags` | multi | Notion-only |
| — | `Start Date` / `Completed Date` | date | Notion-only |
| — | `Review Frequency` | select: `Daily`, `Weekly`, `Biweekly`, `Monthly`, `Quarterly` | Notion-only |
| — | `Source` | select: `Self`, `Journal Steward`, `Work` | Notion-only |
| — | `Related Projects` / `Related Tasks` | rel | Notion-only |
| — | `Origin Journal Digest` | url | Notion-only; provenance |

**status** *(canonical goals status — the tasks/projects lifecycle vocabulary plus goals-specific
`planning`; identical across all three backends, mapped 1:1 here)* — `not-started`→`Not Started` ·
`planning`→`Planning` · `in-progress`→`In Progress` · `paused`→`On Hold` · `done`→`Completed` ·
`archived`→`Abandoned`.
**priority** — `low`→`🟢 Low` · `medium`→`🟡 Medium` · `high`→`🔴 High`.
**category** *(illustrative; owner-defined)* — `work`→`💼 Work` · `health-self-care`→`🩺 Health & Self-Care` ·
`relationships`→`💜 Relationships` · `home-errands`→`🏠 Home & Errands` ·
`hobbies-tech`→`🔧 Hobbies & Tech` · `gaming`→`🎮 Gaming` · `finances`→`💵 Finances` ·
`personal-growth`→`🌱 Personal Growth`.

### reminders — `collection://00000000-0000-0000-0000-000000000007`

The assistant's own reminder tracker. Lives under the **IJData** page (`…0022`, a child of the
Interstitial Journal `…0010`); DB page id `…0011`. **`Status` and `Type`/`Importance`/`Cadence`/
`Time Window` are all `select`s** (not status properties); reminder `Status` options are
**emoji-free**, while `Importance` carries emoji.

| Canonical field | Notion property | Type | Notes |
|---|---|---|---|
| title | `Reminder` | title | |
| type | `Type` | select | see table |
| importance | `Importance` | select | see table (emoji) — drives nudge ordering |
| nag-until-done | `Nag Until Done` | checkbox | independent of importance |
| call-me | `Call Me` | checkbox | opts into phone-call escalation |
| status | `Status` | select | see table (emoji-free) |
| cadence | `Cadence` | select | see table |
| time-window | `Time Window` | select | see table |
| due | `Due / Target` | date | **name contains " / "** → projects as `date:Due / Target:start` |
| last-reminded | `Last Reminded` | date | `date:Last Reminded:start` |
| last-acknowledged | `Last Acknowledged` | date | `date:Last Acknowledged:start`; durable "done that day" record |
| consecutive-misses | `Consecutive Misses` | number | resets to 0 on any ack |
| reminded-today | `Reminded Today` | checkbox | intraday re-fire guard; cleared by daily reset |
| ack | `Ack` | checkbox | owner-facing one-shot input; consumed then unticked (never read as a record) |
| related-task / related-goal / related-flag | `Related Task` / `Related Goal` / `Related Flag` | rel | → tasks / goals / flags |
| notes | `Notes` | text | |

**type** — `recurring-habit`→`Recurring Habit` · `today-todo`→`Today Todo` ·
`deadline-watch`→`Deadline Watch`.
**importance** — `super-critical`→`🛑 Super-Critical` · `critical`→`🚨 Critical` · `high`→`⭐ High` ·
`notable`→`✨ Notable` · `low`→`📌 Low`.
**status** — `pending`→`Pending` · `reminded`→`Reminded` · `done`→`Done` · `finished`→`Finished` ·
`skipped`→`Skipped` · `snoozed`→`Snoozed` · `paused`→`Paused`. *(`done` = done-for-today, re-fires next
cycle; `finished` = retired, written only on an explicit "I'm finished".)*
**cadence** — `daily`→`Daily` · `weekdays`→`Weekdays` · `every-3-days`→`Every 3 days` ·
`every-5-days`→`Every 5 days` · `weekly`→`Weekly` · `multiple-per-day`→`Multiple/day` ·
`one-off`→`One-off`.
**time-window** — `morning`→`Morning` · `midday`→`Midday` · `evening`→`Evening` · `bedtime`→`Bedtime` ·
`anytime`→`Anytime`.

### run-log — `collection://00000000-0000-0000-0000-000000000008`

The **assistant's own** Run Log (DB page `…0012`, under Personal Home) — distinct from the journal's
Agent Run Log `…0016` (see journal-tier). `Mode` and `Status` are **selects** (emoji-free).

| Canonical field | Notion property | Type | Notes |
|---|---|---|---|
| date | `Date` | title | run label, e.g. `2026-06-28 Brief` |
| run-date | `Run Date` | date | `date:Run Date:start` |
| mode | `Mode` | select | see table |
| status | `Status` | select | see table |
| items-surfaced | `Items Surfaced` | number | |
| actions-taken | `Actions Taken` | number | |
| drafts-held | `Drafts Held` | number | |
| actions-summary | `Actions Summary` | text | |
| carry-over-context | `Carry-Over Context` | text | |
| issues | `Issues / Uncertainties` | text | |
| (body) | page body | blocks | incremental phase log — target of `store-append` |

**mode** — `brief`→`Brief` · `wrap`→`Wrap` · `triage`→`Triage` · `ask`→`Ask` · `reminders`→`Reminders`.
**status** — `success`→`Success` · `partial`→`Partial` · `failed`→`Failed`.

---

## Journal-tier domains

### achievements — `collection://00000000-0000-0000-0000-000000000013`

The Achievements Tracker (one row per achievement). **Naming mismatch (see report):** the canonical
`achievements` domain names its select field `category`, but Notion's property is **`Tier`** — a
size-tier, not a topical category. `category` maps to `Tier`.

| Canonical field | Notion property | Type | Notes |
|---|---|---|---|
| title | `Achievement` | title | |
| date | `Date` | date | |
| category | `Tier` | select | see table |
| — | `Related Task` / `Related Project` | rel | Notion-only |
| — | `Notes` | text | Notion-only |
| — | `Journal Digest Link` | url | Notion-only; provenance |

**category → Tier** *(illustrative; size-tiers, owner-defined)* — `small`→`🥉 Small` ·
`medium`→`🥈 Medium` · `large`→`🥇 Large`. When unsure, pick the lower tier.

### mood — `collection://00000000-0000-0000-0000-000000000015`

The Feelings & Mood Log.

| Canonical field | Notion property | Type | Notes |
|---|---|---|---|
| title | `Feeling` | title | brief description |
| date | `Date` | date | |
| emotions | `Emotions` | multi | extensible; add options as needed |
| valence | `Valence` | select | see table |
| context | `Context` | text | |
| — | `Journal Digest Link` | url | Notion-only; provenance |

**emotions** *(emoji-free; open set)* — `Happy`, `Excited`, `Proud`, `Calm`, `Anxious`, `Frustrated`,
`Sad`, `Fatigued`, `Motivated`, `Overwhelmed`.
**valence** — `positive`→`Positive` · `neutral`→`Neutral` · `negative`→`Negative` · `mixed`→`Mixed`.

### journal — page `00000000-0000-0000-0000-000000000010` (not a database)

Daily entries are timestamped bullets under **date toggles** on the Interstitial Journal page, plus a
top **carry-over callout**. No option translation table — it is page-body content, reached via
`store-append` / `store-get` (see `mapping.md` for the block shapes). Journal-presence signal = a
today date-toggle with ≥ 1 entry (see the Key-pages note).

### goal-measurements — `collection://00000000-0000-0000-0000-000000000006` *(auxiliary)*

**Not one of the store contract's canonical domains** — a journal-tier tracker companion to `goals`
(its placeholder id `…0006` is reserved in the numbering). Documented here for completeness.

- `Measurement` **(title)** · `Goal` (rel → goals `…0005`, **limit 1**) · `Date` (date)
- `Value` / `Previous Value` / `Change` / `% of Target` (number) · `Unit` (text) · `Context` (text)
- `Journal Digest Link` (url — provenance)

### journal agent run log — `collection://00000000-0000-0000-0000-000000000016` *(auxiliary)*

**Separate from the canonical `run-log` (`…0008`).** The journal steward's own memory-across-runs DB,
with a different schema. Documented for completeness; the canonical `run-log` domain does **not** map here.

- `Date` **(title)** · `Run Date` (date) · `Status` (select: `Success`, `Partial`, `Failed`)
- `Tasks Created` / `Tasks Updated` / `Tasks Completed` / `Achievements Logged` / `Mood Entries` (number)
- `Themes` (multi: `Work`, `Health`, `Hobbies`, `Relationships`, `Errands`, `Gaming`, `Self-Care`,
  `Productivity`)
- `Actions Summary` / `Carry-Over Context` / `Issues / Uncertainties` / `Retrospective` (text)
- **Page body** = the day's verbatim journal entries + incremental phase log. **No Goals count field
  exists** — fold goal counts into Actions Summary text; don't set non-existent properties.
