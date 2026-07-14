# Phase 3 — Goals & Goal Measurements

Databases: **🎯 Goals** `collection://00000000-0000-0000-0000-000000000005` and **📊 Goal Measurements**
`collection://00000000-0000-0000-0000-000000000006` (full property lists in `databases.md`).

## During the daily run

1. Scan entries for mentions of **existing** goals (progress, setbacks, reflections, completions) and
   **new** goal ideas.
2. **Existing goals:**
   - Update `Current Value` and `Progress %` if there's measurable progress (`Progress %` is 0–1).
   - Add a **Goal Measurements** row for any concrete data point: `Measurement` (label) · `Goal`
     (relation, **limit 1**) · `Date` · `Value` · `Previous Value` · `Change` · `% of Target` · `Unit` ·
     `Context` · `Journal Digest Link` (provenance — this run's run-log entry URL).
   - Update `Status` if completed/abandoned/on-hold.
   - Append a dated note to the goal page body's **Progress Log**.
3. **New goals** — only create one if the owner explicitly states a goal or clearly describes a
   SMART-style intention (not a one-off task). Set `Source` = `Self` (owner-stated) or `Journal Steward`
   (your suggestion; be conservative — only on a clear, repeated pattern). Fill the SMART fields
   (`Specific`, `Measure`, `Achievable`, `Relevant To`, `Target`/`Unit`, `Confidence`, dates,
   `Review Frequency`, `Category`, `Relevance Tags`) and set `Origin Journal Digest` to this run's
   run-log entry URL.

## Goal page body

Use the body for narrative tracking: **Milestones**, **Progress Log** (dated, linked to the run-log
entries that recorded each data point), **Resources & Dependencies**, **Reflections**.

## Carry-over integration

Add a `🎯 Goals` group to the carry-over callout when any apply:
- A goal's review is due (per `Review Frequency`).
- A goal's `Target Date` is within 7 days.
- A goal's status changed today.
- A new goal was created today.

Format: `🎯 **Goal name** — brief status note (review due / deadline approaching / newly created)`.

## Run log

There are **no** Goals count properties on the Run Log — record "Goals created: N, Goals updated: N"
inside **Actions Summary** text instead (see `databases.md` §5).
