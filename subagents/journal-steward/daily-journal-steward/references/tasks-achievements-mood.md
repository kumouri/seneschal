# Phase 2 — Tasks/Projects, Achievements, Mood

Run after the Phase-1 capture. Batch the independent creates/updates together.

## Tasks & Projects

Databases: **Tasks** `collection://00000000-0000-0000-0000-000000000001`, **Projects**
`collection://00000000-0000-0000-0000-000000000002`, **Tasks Created by the Journal Steward**
`collection://00000000-0000-0000-0000-000000000021` (see `databases.md` for full props).

Extraction rules:
- **Create a task** when the journal states a concrete, actionable next step.
- **Update a task** when the journal shows progress, new detail, or completion — add a progress note; set
  `Status = Done` (and stamp `Completed`) only when the journal **clearly** says it's finished.
- **Create a Project** only for an ongoing multi-step effort, never a one-off.
- **Link, don't duplicate:** search existing Tasks/Projects first; if several match, link the most likely
  and note the uncertainty in the run log.

For **each task you create** (not ones merely updated/completed), add a row to **Tasks Created by the
Journal Steward**:
- `Task Name` = the task's name · `Date Created` = today · `Task` = relation to the new task page ·
  `Project` = relation if applicable · `Notes` = why it was created.

Reflect the Created/Updated/Completed counts in the run log.

## Achievements — TWO places

Log anything completed or achieved (explicitly completed tasks **and** notable wins/milestones even if
untracked).

1. **Achievements Log page** (`00000000-0000-0000-0000-000000000014`) — append a dated section. Each day
   gets a `## <mention-date>` header with short 1–2 line bullets; link the relevant task/project/page
   when possible. **Append**, don't overwrite. Existing entries use tier emojis inline for bigger wins
   (🥇/🥈/🥉) — match that style.
2. **Achievements Tracker DB** (`collection://00000000-0000-0000-0000-000000000013`) — one row each:
   - `Achievement` (title) · `Date` · `Tier` (`🥉 Small` / `🥈 Medium` / `🥇 Large`) ·
     `Related Task` / `Related Project` (relations) · `Notes` · `Journal Digest Link` (provenance —
     this run's run-log entry URL).

Tier judgment (conservative — when unsure, go lower):
- 🥉 Small — minor wins, chores, quick tasks done.
- 🥈 Medium — notable progress, multi-step tasks completed, meaningful milestones.
- 🥇 Large — major accomplishments, completed projects, significant life events/breakthroughs.

The Tracker DB is the authoritative record; if budget is tight you may skip the Achievements Log **page**
update and note it in Carry-Over Context, but keep the DB rows.

## Feelings & Mood

Database: **💭 Feelings & Mood Log** `collection://00000000-0000-0000-0000-000000000015`.

- Add a row whenever the journal mentions feelings, emotions, mood shifts, or how the owner is doing
  physically or emotionally. Include **both positive and negative**.
- **Capture what was expressed, not what you infer** — be sensitive and accurate.
- Properties: `Feeling` (title, brief) · `Date` · `Emotions` (multi — add new options if needed) ·
  `Valence` (`Positive`/`Neutral`/`Negative`/`Mixed`) · `Context` (what was happening) ·
  `Journal Digest Link` (provenance — this run's run-log entry URL).

Count mood rows for the run log `Mood Entries`.
