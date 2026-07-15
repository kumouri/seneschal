# Databases & Pages — Master Reference (journal core)

> **Placeholder IDs.** Every collection/page id in this file is a placeholder of the form
> `00000000-0000-0000-0000-0000000000NN`. The store setup flow replaces them with your workspace's real
> Notion ids in a **local (gitignored) copy** of this file — the tracked copy always keeps the
> placeholders.

The single source of truth for every Notion database and page the Daily Journal Steward touches.
**Do not fetch these schemas at runtime** — use what's here. Only fetch a schema if a write returns an
explicit property/schema error, and then only the one affected database.

> **Relationship to the assistant's map:** `../../../../seneschal/references/databases.md` restates the
> handful of shared databases (Tasks, Projects, Flags, Goals, the assistant's own Run Log, Reminders)
> that the assistant's other modes touch directly. **This file is canonical for journal-owned databases** — if a
> schema here ever conflicts with the restatement there, this file wins; sync them.

Notation: `(title)` = the title property. `(date)`, `(text)`, `(number)`, `(url)`, `(checkbox)`,
`(person)` are scalar types. `(select: …)` = single-select with the exact options listed. `(status: …)` =
a status property (options are exact). `(multi: …)` = multi-select. `(rel → X)` = relation to collection X.

For HOW to set each type via MCP (date expansion, relations as URL arrays, status vs select, checkbox
values), see `notion-mcp-mapping.md`.

## Table of contents
1. Pages (non-database)
2. Core journal databases — Agent Run Log, Tasks Created by the Journal Steward
3. The real Tasks & Projects databases
4. Trackers — Achievements Tracker, Feelings & Mood Log, Important Flags, Goals, Goal Measurements
5. Known quirks & porting notes

---

## 1. Pages (non-database)

| Page | ID | Role |
|------|----|------|
| Interstitial Journal | `00000000-0000-0000-0000-000000000010` | The working journal page. Read the active date toggle; clear it; write the carry-over callout at top. |
| Achievements Log | `00000000-0000-0000-0000-000000000014` | Append-only page: a running chronological list under `## <date>` headers. |
| Personal Home | `00000000-0000-0000-0000-000000000009` | Top-level hub. Parent of People, Tasks, Projects. |

> **Journal-activity signal (relied on by other skills — do not change the structure).** The owner's
> daily writing lives on the **Interstitial Journal** page as **top-level date toggles** —
> `<details><summary><mention-date start="YYYY-MM-DD"/></summary>` with timestamped entries nested under
> each. **"The owner journaled today"** = a top-level date toggle whose `start` equals today (the owner's
> configured timezone) with **≥ 1 entry** under it. The `📌` **carry-over callout** at the very top is
> **the assistant's own** — *not* evidence the owner wrote. The journal-presence reminder gate
> (`../../../../seneschal/references/reminders-policy.md`) consumes this signal, so the steward must
> preserve the date-toggle shape when it clears and resets the page.

Tracker databases are typically **inline databases on the Interstitial Journal page**, so they're easy to
find via search if an id ever changes.

---

## 2. Core journal databases

### 🧠 Agent Run Log — `collection://00000000-0000-0000-0000-000000000016`
The steward's memory across runs. Read at start, write incrementally (see `run-log.md`). Dedicated to
journal runs — the assistant's own 🧭 Run Log (`…0008` in the assistant's map, see the note above) is
separate.
- `Date` **(title)** — run date, e.g. `2026-06-09`
- `Run Date` (date)
- `Status` (select: `Success`, `Partial`, `Failed`)
- `Tasks Created` (number) · `Tasks Updated` (number) · `Tasks Completed` (number)
- `Achievements Logged` (number) · `Mood Entries` (number)
- `Themes` (multi: `Work`, `Health`, `Hobbies`, `Relationships`, `Errands`, `Gaming`, `Self-Care`, `Productivity`)
- `Actions Summary` (text) · `Carry-Over Context` (text) · `Issues / Uncertainties` (text) · `Retrospective` (text)
- **Page body** = the day's verbatim journal entries (captured in Phase 1, before the journal is cleared)
  + the incremental phase log + reasoning.
- **No Goals count field exists** — fold goal counts into Actions Summary text (see §5).

### ✅ Tasks Created by the Journal Steward — `collection://00000000-0000-0000-0000-000000000021`
A log of tasks this agent created (not tasks merely updated/completed). One row per created task.
- `Task Name` **(title)**
- `Date Created` (date)
- `Task` (rel → Tasks `…0001`) — the real task page
- `Project` (rel → Projects `…0002`) — if any
- `Notes` (text) — why it was created

> **Provenance links.** The original agent archived a daily digest page and stamped its URL on every
> tracker row via a `Journal Digest Link` / `Origin Journal Digest` (url) property. The core pipeline
> keeps those properties as **provenance links**: set them to **this run's Agent Run Log entry URL**
> (created in Phase 1, which also holds the day's verbatim entries). If you extend the skill with a
> digest/archive step, point them at your digest page instead.

---

## 3. Tasks & Projects (the real databases)

### Tasks — `collection://00000000-0000-0000-0000-000000000001`
- `Task name` **(title)**
- `Status` (status: `Not Started`, `In Progress`, `Paused`, `Done`, `Archived`) — mark complete via `Done`
- `Due` (date) · `Priority` (select: `Low`, `Medium`, `High`)
- `Completed` (date) — **set this when you move a task to `Done`** (the real completion date; query as
  `date:Completed:start`).
- `Last Updated` (last_edited_time) — auto-maintained; no manual writes.
- `Tags` (multi: `Mobile`, `Website`, `Improvement`)
- `Summary` (text)
- `Project` (rel → Projects `…0002`) · `Parent-task` (rel → self) · `Sub-tasks` (rel → self)
- `Assignee` (person)

### Projects — `collection://00000000-0000-0000-0000-000000000002`
- `Project name` **(title)**
- `Status` (status: `Backlog`, `Planning`, `In Progress`, `Paused`, `Done`, `Canceled`)
- `Priority` (select: `Low`, `Medium`, `High`, `Critical`)
- `Category` (multi: `Home`, `Work`, `Hobby`)
- `Summary` (text) · `Dates` (date) · `Tasks` (rel → Tasks) · `Owner` (person)
- `Blocked By` / `Is Blocking` (rel → self)

---

## 4. Trackers

### 🏆 Achievements Tracker — `collection://00000000-0000-0000-0000-000000000013`
Authoritative achievement record (one row each).
- `Achievement` **(title)**
- `Date` (date)
- `Tier` (select: `🥉 Small`, `🥈 Medium`, `🥇 Large`)
- `Related Task` (rel → Tasks) · `Related Project` (rel → Projects)
- `Notes` (text) · `Journal Digest Link` (url — provenance, see §2)

Tier guide: 🥉 minor wins/chores/quick tasks · 🥈 notable progress/multi-step done/milestones ·
🥇 major accomplishments/completed projects/significant life events. When unsure, pick the lower tier.

### 💭 Feelings & Mood Log — `collection://00000000-0000-0000-0000-000000000015`
- `Feeling` **(title)** — brief description
- `Date` (date)
- `Emotions` (multi: `Happy`, `Excited`, `Proud`, `Calm`, `Anxious`, `Frustrated`, `Sad`, `Fatigued`,
  `Motivated`, `Overwhelmed` — add options as needed)
- `Valence` (select: `Positive`, `Neutral`, `Negative`, `Mixed`)
- `Context` (text) · `Journal Digest Link` (url — provenance)

### ⭐ Important Flags — `collection://00000000-0000-0000-0000-000000000003`
- `Flag` **(title)**
- `Date Flagged` (date)
- `Type` (select: `💼 Work`, `👨‍👩‍👧 Family`, `💜 Relationship`, `🧠 Internal / Feelings`, `🩺 Health`,
  `💰 Finances`, `🏠 Home / Life`, `🎨 Hobby / Creative`, `🤝 Friends / Social`, `Other`)
- `Importance Level` (select: `🚨 Critical`, `⭐ High`, `✨ Notable`, `📌 Reference`)
- `Status` (status: `Active`, `Carrying Over`, `Resolved`, `Archived`)
- `Context` (text) · `Why It Matters` (text) · `Resolution / Outcome` (text)
- `Related Task` (rel → Tasks) · `Related Project` (rel → Projects)
- `Journal Digest Link` (url — provenance) · `Tags` (multi: empty — add as needed)

### 🎯 Goals — `collection://00000000-0000-0000-0000-000000000005`
- `Goal` **(title)**
- `Status` (status: `Not Started`, `Planning`, `In Progress`, `On Hold`, `Completed`, `Abandoned`)
- `Category` (multi: `💼 Work`, `🩺 Health & Self-Care`, `💜 Relationships`, `🏠 Home & Errands`,
  `🔧 Hobbies & Tech`, `🎮 Gaming`, `💵 Finances`, `🌱 Personal Growth`)
- `Priority` (select: `🔴 High`, `🟡 Medium`, `🟢 Low`)
- `Specific` (text) · `Measure` (text) · `Achievable` (text) · `Relevant To` (text) · `Notes` (text)
- `Target Value` (number) · `Current Value` (number) · `Unit` (text) · `Progress %` (number, percent: 0–1)
- `Confidence` (select: `💪 High`, `🤔 Medium`, `😬 Low`)
- `Relevance Tags` (multi: `Career Growth`, `Physical Health`, `Mental Health`, `Financial Security`,
  `Relationships`, `Independence`, `Creativity`, `Skill Building`, `Quality of Life`)
- `Start Date` / `Target Date` / `Completed Date` (date)
- `Review Frequency` (select: `Daily`, `Weekly`, `Biweekly`, `Monthly`, `Quarterly`)
- `Source` (select: `Self`, `Journal Steward`, `Work`)
- `Related Projects` (rel → Projects) · `Related Tasks` (rel → Tasks)
- `Origin Journal Digest` (url — provenance, see §2)
- **Page body**: Milestones, Progress Log, Resources & Dependencies, Reflections.

### 📊 Goal Measurements — `collection://00000000-0000-0000-0000-000000000006`
- `Measurement` **(title)** — e.g. `Week 3 check-in`
- `Goal` (rel → Goals `…0005`, **limit 1**)
- `Date` (date)
- `Value` (number) · `Previous Value` (number) · `Change` (number) · `% of Target` (number, percent) · `Unit` (text)
- `Context` (text) · `Journal Digest Link` (url — provenance)

---

## 5. Known quirks & porting notes

- **Run Log has no Goals count fields.** Record "Goals created: N, Goals updated: N" in **Actions
  Summary** text (or add number fields to your workspace's copy). Don't try to set non-existent
  properties.
- **`Status` vs `select`:** Tasks/Projects/Goals/Important Flags use a **status** property;
  Tier/Valence/Priority/etc. are **select**. They're set the same way via MCP but only accept their
  exact option strings (emoji included).
- **Emoji is part of option values** (e.g. `🥉 Small`, `🔴 High`, `💼 Work`). Copy them exactly.
- **Provenance URLs:** `Journal Digest Link` / `Origin Journal Digest` point at this run's Agent Run Log
  entry (see §2) — create that row first and reuse its URL everywhere.
