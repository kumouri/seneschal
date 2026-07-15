# obsidian/schema.md — the Obsidian backend's domain map

**What this file is.** The domain map for the **Obsidian** store backend: which folder/note holds each
domain, the exact fields each record carries, their **fixed canonical order**, and the canonical
(emoji-free) option values. It is the filesystem analogue of `notion/schema.template.md`. Read this with
`../README.md` (the three-step indirection + the six store verbs) and `mapping.md` (HOW each verb becomes a
Read/Write/Edit/Glob/Grep call). **Never re-derive vault conventions by exploring the vault** — trust this
file; setup-time discovery by `/setup-store` is the only sanctioned exception (README §"Never fetch schemas
at runtime").

The data is **Markdown on disk**, read and written with Claude Code's native tools — **no MCP, no plugin
required**. Dataview/Tasks-plugin conventions are honored so a human's own queries work too, but seneschal
queries the notes itself with Grep/Glob.

## Where records live

One **note per record** under `<vault_path>/<subfolder>/` (from `store/config.json`;
default `subfolder: "Seneschal"`). One folder per domain:

| Folder | Domain | Filename rule |
|---|---|---|
| `Tasks/` | tasks | `<title>.md` |
| `Projects/` | projects | `<title>.md` |
| `Goals/` | goals | `<title>.md` |
| `Flags/` | flags | `<title>.md` |
| `Reminders/` | reminders | `<title>.md` |
| `People/` | people | `<title>.md` |
| `Journal/` | journal | `YYYY-MM-DD.md` daily notes **+** `Carry-Over.md` |
| `Briefs/` | brief-mode output | `YYYY-MM-DD.md` |
| `Runs/` | run-log | `YYYY-MM-DD <mode>.md` (Dream prunes > 90 days) |
| `Achievements/` | achievements | `<title>.md` |
| `Mood/` | mood | `<title>.md` |

**`ref` = the note's vault-relative path**, e.g. `Seneschal/Tasks/Ship the spec.md`. This *is* the record
identity — it replaces Notion's id cache entirely. Every verb that takes a `ref` takes one of these paths.

## Conventions (the contract every domain obeys)

- **YAML frontmatter is the single source of truth** for a record's fields. The note body is narrative /
  log / capture only — never the authoritative value of a field.
- **Fixed canonical field order.** Each domain below lists its fields in one deterministic order. A
  `store-create` writes them in that order; a `store-update` edits them **in place**. Because the order is
  fixed, fields that change together sit on **adjacent lines**, so one multi-line `Edit` sets them in a
  single pass (this is what makes `store-update` deterministic — see `mapping.md`).
- **Dates** are ISO `YYYY-MM-DD` (no time unless a field is explicitly a timestamp). **Booleans** are
  `true` / `false`. Empty/unknown → omit the key or leave it blank; never invent a value.
- **Option values go in verbatim** — the canonical, **emoji-free**, lowercase-hyphenated strings from the
  tables below (`status: in-progress`, `importance: super-critical`). No emoji, no title-casing.
- **Relations are quoted wikilinks in frontmatter**, pointing at the target note **subfolder-relative,
  without extension**: `project: "[[Projects/Ship the spec]]"`. A list relation is a YAML list of such
  wikilinks. To turn a relation into a `ref` for another verb: prepend `<subfolder>/` and append `.md`
  (`[[Projects/Ship the spec]]` → `Seneschal/Projects/Ship the spec.md`).
- **Filename = title** with the illegal set `\ / : * ? " < > | # ^ [ ]` stripped, trimmed to **≤ 80 chars**;
  a collision appends ` (2)`, ` (3)`, … **Never rename a note to track a title change** — the title lives in
  `title:`; the filename is a stable-enough handle, and renaming would break every inbound wikilink and every
  cached `ref`. (Date-keyed domains — journal, briefs, runs — are named by date/mode, not title.)
- **Capture candidates are not records.** A Tasks-plugin checkbox line in a daily note
  (`- [ ] call the vet 📅 2026-07-15`) is a **capture candidate** the journal/wrap mode *offers to promote*
  into a real `Tasks/` note — **never silently**, and the checkbox is **never** the record's state source.
  The `Tasks/<note>.md` frontmatter `status:` is the only truth for whether a task is done.
- **Journal-presence signal** = `Journal/<today>.md` exists **and** has ≥ 1 timestamped bullet in its body
  (a `Glob` for the file + a `Read`/`Grep` for a `- HH:MM` line). That is the evidence the owner journaled
  today (consumed by the reminders journal-presence gate); the assistant's own `Carry-Over.md` edits are
  **not** evidence.
- **`ack: true` is the mobile one-tap affordance.** On a phone the owner flips a reminder's `ack` to `true`
  in the note; a reconciling run **consumes and resets it** (see the reminders worked example in
  `mapping.md`). It is a one-shot input, never a durable done-record.
- Exclude sync-conflict copies (`*conflict*`, `*sync-conflict*`) from every read — see `mapping.md`
  §Hazards.

---

## tasks — `Tasks/`

| # | field | type | canonical values / notes |
|---|---|---|---|
| 1 | `title` | title | the task name |
| 2 | `status` | select | `not-started` · `in-progress` · `paused` · `done` · `archived` |
| 3 | `due` | date | ISO |
| 4 | `priority` | select | `low` · `medium` · `high` |
| 5 | `completed` | date | set when `status: done` (records *when*) |
| 6 | `tags` | list | free-text list |
| 7 | `summary` | text | one-line description |
| 8 | `project` | rel → projects | wikilink |
| 9 | `parent` | rel → tasks | wikilink (this task's parent) |
| 10 | `subtasks` | list rel → tasks | wikilinks |
| 11 | `assignee` | text | name |

```markdown
---
title: Ship the spec
status: in-progress
due: 2026-07-18
priority: high
completed:
tags: [writing, docs]
summary: Finish and circulate the store-layer spec.
project: "[[Projects/Ship the spec]]"
parent:
subtasks:
  - "[[Tasks/Draft the mapping doc]]"
assignee: Alex
---

Notes, progress log, and any narrative live in the body.
```

## projects — `Projects/`

| # | field | type | canonical values / notes |
|---|---|---|---|
| 1 | `title` | title | the project name |
| 2 | `status` | select | `backlog` · `planning` · `in-progress` · `paused` · `done` · `canceled` |
| 3 | `priority` | select | `low` · `medium` · `high` · `critical` |
| 4 | `category` | list | free-text list |
| 5 | `summary` | text | one-line description |
| 6 | `dates` | date | key project date (start/target) |
| 7 | `tasks` | list rel → tasks | wikilinks |
| 8 | `owner` | text | name |
| 9 | `blocked-by` | list rel → projects | wikilinks |
| 10 | `blocking` | list rel → projects | wikilinks |

```markdown
---
title: Ship the spec
status: in-progress
priority: high
category: [product]
summary: Land the pluggable store layer end to end.
dates: 2026-07-20
tasks:
  - "[[Tasks/Ship the spec]]"
owner: Sam
blocked-by:
blocking:
---

Milestones, progress log, dependencies, reflections go in the body.
```

## goals — `Goals/`

| # | field | type | canonical values / notes |
|---|---|---|---|
| 1 | `title` | title | the goal name |
| 2 | `status` | select | `not-started` · `planning` · `in-progress` · `paused` · `done` · `archived` |
| 3 | `category` | list | free-text list |
| 4 | `priority` | select | `low` · `medium` · `high` |
| 5 | `target-date` | date | ISO |
| 6 | `target-value` | number | the number to reach |
| 7 | `current-value` | number | latest measured value |
| 8 | `unit` | text | e.g. `lb`, `books`, `%` |

> **Note (goals.status).** Canonical goals status is `not-started` · `planning` · `in-progress` ·
> `paused` · `done` · `archived` — the tasks/projects lifecycle vocabulary plus a goals-specific
> `planning`. All three backends use these same values; the Notion registry's translation table maps
> them 1:1 onto its Goals database's option strings (`paused`→`On Hold`, `done`→`Completed`,
> `archived`→`Abandoned`).

```markdown
---
title: Read 24 books this year
status: in-progress
category: [personal]
priority: medium
target-date: 2026-12-31
target-value: 24
current-value: 11
unit: books
---

Progress log + measurements go in the body.
```

## flags — `Flags/`

| # | field | type | canonical values / notes |
|---|---|---|---|
| 1 | `title` | title | short flag name |
| 2 | `date-flagged` | date | ISO |
| 3 | `type` | select | `work` · `family` · `relationship` · `internal` · `health` · `finances` · `home` · `hobby` · `social` · `other` |
| 4 | `importance` | select | `critical` · `high` · `notable` · `reference` |
| 5 | `status` | select | `active` · `carrying-over` · `resolved` · `archived` |
| 6 | `context` | text | what's going on |
| 7 | `why-it-matters` | text | why it's flagged |
| 8 | `related-task` | rel → tasks | wikilink |
| 9 | `related-project` | rel → projects | wikilink |
| 10 | `journal-link` | url | provenance link |

```markdown
---
title: Vendor contract renewal
date-flagged: 2026-07-10
type: work
importance: high
status: active
context: The current SaaS contract auto-renews at end of month.
why-it-matters: Missing the window locks in another year at the higher rate.
related-task: "[[Tasks/Ship the spec]]"
related-project: "[[Projects/Ship the spec]]"
journal-link:
---

Longer context / decision history in the body.
```

## people — `People/`

| # | field | type | canonical values / notes |
|---|---|---|---|
| 1 | `title` | title | the person's name |
| 2 | `aliases` | list | other names / handles |
| 3 | `relationship` | text | how they relate to the owner |
| 4 | `email` | text | address |
| 5 | `phone` | text | number |
| 6 | `birthday` | date | ISO (year optional) |
| 7 | `notes` | text | free notes |

```markdown
---
title: Jordan Lee
aliases: [Jordan]
relationship: Colleague
email: jordan@example.com
phone:
birthday: 1990-03-14
notes: Prefers async; timezone US-Pacific.
---

Interaction history / longer notes in the body.
```

## reminders — `Reminders/`

The reminders behavior contract (escalation, slots, the ack lifecycle) lives in
`../../references/reminders-policy.md`; this table is only the record shape.

| # | field | type | canonical values / notes |
|---|---|---|---|
| 1 | `title` | title | what to be reminded of |
| 2 | `type` | select | `recurring-habit` · `today-todo` · `deadline-watch` |
| 3 | `importance` | select | `super-critical` · `critical` · `high` · `notable` · `low` |
| 4 | `nag-until-done` | bool | force re-fire regardless of importance |
| 5 | `call-me` | bool | escalate to a phone call when due |
| 6 | `status` | select | `pending` · `reminded` · `done` · `finished` · `skipped` · `snoozed` · `paused` |
| 7 | `cadence` | select | `daily` · `weekdays` · `every-3-days` · `every-5-days` · `weekly` · `multiple-per-day` · `one-off` |
| 8 | `time-window` | select | `morning` · `midday` · `evening` · `bedtime` · `anytime` |
| 9 | `due` | date | ISO (for `deadline-watch` / `one-off`) |
| 10 | `last-reminded` | date | ISO |
| 11 | `last-acknowledged` | date | ISO — the durable proof-of-done-for-a-day |
| 12 | `consecutive-misses` | number | resets to 0 only on a real `done` |
| 13 | `reminded-today` | bool | guards at-most-once-per-slot |
| 14 | `ack` | bool | the one-tap mobile affordance; consumed + reset by a reconciling run |
| 15 | `related-task` | rel → tasks | wikilink |
| 16 | `related-goal` | rel → goals | wikilink |
| 17 | `related-flag` | rel → flags | wikilink |
| 18 | `notes` | text | e.g. `[gate: journal-presence]`, weekday for `weekly` |

```markdown
---
title: Water the plants
type: recurring-habit
importance: low
nag-until-done: true
call-me: false
status: pending
cadence: every-3-days
time-window: morning
due:
last-reminded: 2026-07-11
last-acknowledged: 2026-07-11
consecutive-misses: 0
reminded-today: false
ack: false
related-task:
related-goal:
related-flag:
notes:
---
```

## journal — `Journal/`

**Not a frontmatter record** — a per-day note plus one carry-over note.

- **Daily note** `Journal/YYYY-MM-DD.md`: body is **timestamped bullets** the owner (or a capture) appends
  throughout the day. Optional minimal frontmatter (`date:`). Tasks-plugin checkbox lines here are **capture
  candidates**, not records (see Conventions).
- **`Journal/Carry-Over.md`**: the assistant's rolling carry-over context (rebuilt to current-state each run,
  not an append log). This is the assistant's own note — **not** journal-presence evidence.

```markdown
---
date: 2026-07-14
---

- 08:05 Woke up, slow start. Coffee then desk.
- 10:20 Shipped the store schema draft. Felt good.
- [ ] call the vet 📅 2026-07-15   <!-- capture candidate: wrap offers to promote -->
- 14:40 Walk. Cleared my head.
```

## briefs — `Briefs/`

Rendered morning-brief output, one per day: `Briefs/YYYY-MM-DD.md`. Body is the brief; frontmatter is
minimal (`date:`). Read for provenance; not a queryable domain.

## run-log — `Runs/`

| # | field | type | canonical values / notes |
|---|---|---|---|
| 1 | `run-date` | date | ISO |
| 2 | `mode` | select | `brief` · `wrap` · `triage` · `ask` · `reminders` |
| 3 | `status` | select | `success` · `partial` · `failed` |
| 4 | `items-surfaced` | number | |
| 5 | `actions-taken` | number | |
| 6 | `drafts-held` | number | |

Body = the phase log + any carry-over notes for the run. Filename `YYYY-MM-DD <mode>.md`
(collision → ` (2)`). **Dream prunes `Runs/` older than 90 days.**

```markdown
---
run-date: 2026-07-14
mode: brief
status: success
items-surfaced: 6
actions-taken: 2
drafts-held: 1
---

## Phase log
- Read tasks/reminders due today; surfaced 6.
- Held 1 outbound draft for approval.
```

## achievements — `Achievements/`

| # | field | type | canonical values / notes |
|---|---|---|---|
| 1 | `title` | title | what was achieved |
| 2 | `date` | date | ISO |
| 3 | `category` | list | free-text list |

```markdown
---
title: Finished the store spec
date: 2026-07-14
category: [work]
---
```

## mood — `Mood/`

| # | field | type | canonical values / notes |
|---|---|---|---|
| 1 | `title` | title | short feeling label |
| 2 | `date` | date | ISO |
| 3 | `valence` | select | `positive` · `neutral` · `negative` · `mixed` |
| 4 | `emotions` | list | free-text list |
| 5 | `context` | text | what was happening |

> **Note (mood fields).** The shared surface says only "date + the mood fields"; this backend renders them
> as `valence` / `emotions` / `context` around a short feeling title (mirroring a feelings-and-mood log). If
> the Notion sibling names them differently, reconcile at setup.

```markdown
---
title: Relieved
date: 2026-07-14
valence: positive
emotions: [relief, pride]
context: Shipped the thing I'd been stuck on.
---
```
