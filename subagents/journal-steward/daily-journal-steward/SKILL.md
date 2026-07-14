---
name: daily-journal-steward
description: >-
  Processes the owner's Notion interstitial journal end-to-end — a port of an original Notion-AI
  "Daily Journal Steward" agent. Each run reads the working journal page, captures the day's verbatim
  entries, extracts tasks/projects, logs achievements, mood, goals, and important flags into their
  Notion databases, then clears the journal, builds tomorrow's carry-over callout, and writes an Agent
  Run Log entry. Use this whenever running the Daily Journal Steward or "DJS", processing the
  interstitial journal, or doing the daily/morning journal run. This is the scheduled daily
  journal automation — invoke it for any "run the journal", "process my journal", "journal steward",
  or "daily journal run" request even when not phrased exactly, and whenever a scheduled task asks to
  run the daily journal pipeline.
compatibility: Requires the Notion MCP connector (tools named mcp__*__notion-*) connected to the owner's workspace.
---

# Daily Journal Steward (DJS)

This skill is a Claude port of a Notion-AI journal agent. Each daily run reads the **Interstitial
Journal**, connects the day to related Notion pages, captures next actions, logs to the tracking
databases, then resets the journal for tomorrow and records what it did.

It is built the way the original agent thinks: **do the can't-fail critical path first, never load
schemas at runtime, batch independent writes, and keep a running run-log so a cut-short run still leaves
a useful trace.**

## Prerequisites & anchors

- **Notion MCP must be connected.** All reads/writes go through `mcp__<server>__notion-*` tools (search,
  fetch, create-pages, update-page, query-database-view). See `references/notion-mcp-mapping.md` for the
  tool mapping and the date/relation/status gotchas — read it once before your first write each run.
- **Timezone:** the owner's configured timezone. A "journal day" treats **after-midnight entries as part
  of the prior day** (owners often journal past midnight).
- **Every database ID, property, and option value is in `references/databases.md`.** Do **not** fetch
  schemas at runtime — that was the original agent's #1 budget killer. Only fetch a schema if a write
  fails with an explicit property/schema error, and then only the one affected database.
- **Key page anchors** (full list in `references/databases.md`; placeholder ids — store setup fills a
  local copy):
  - Interstitial Journal (the working page): `00000000-0000-0000-0000-000000000010`
  - Achievements Log (append-only page): `00000000-0000-0000-0000-000000000014`

## Execution philosophy (read before acting)

The original agent had a limited budget per run and busy days pushed it to the limit. Honor these or the
run fails:

1. **Critical path FIRST**, before any optional tracker:
   1. Capture the day — the preliminary Agent Run Log row with the verbatim entries in its page body
      (`references/run-log.md`)
   2. Clear the journal + build tomorrow's carry-over callout (`references/clear-and-carryover.md`) —
      never clear before the capture exists
2. **Never load DB schemas at runtime.** They're in `references/databases.md`.
3. **Batch aggressively.** Every parallel batch should contain *all* independent operations for that
   phase. If 8 things can be created independently, create all 8 in one batch — not two rounds of 4.
   Only split when you need a URL from the first batch to reference in the second.
4. **Don't re-narrate the journal.** Plan your actions in one internal pass, then execute. Jump to tool
   calls.
5. **Act-low / ask-high.** The tracker writes in this pipeline are the steward's own act-low routine;
   anything outbound, destructive beyond the defined journal clear, or outside these databases is
   ask-high — draft it and ask (`../../../seneschal/references/autonomy-policy.md`).
6. **Write a preliminary run log** as soon as the capture is done; finalize it at the end. If budget runs
   low, it's OK to skip non-critical trackers — just note what you skipped in Carry-Over Context.

## Daily Run — phase plan

Each phase points to the reference file with the exact rules, properties, and option values. Read a
reference file just-in-time, right before you execute that phase.

**Phase 0 — Load context** (`references/run-log.md`)
- Query the last 3–5 Agent Run Log entries (sorted by Run Date desc). Read **Carry-Over Context**,
  **Issues / Uncertainties**, and the most recent **Retrospective** to inform today (avoid re-creating
  tasks, follow up on flagged patterns).
- Read the Interstitial Journal page. Process only the **most recent / active date toggle** (the prior
  journal day). Hold the verbatim entries in mind for the capture.

**Phase 1 — Capture (critical path #1)** (`references/run-log.md`)
- Create the **preliminary Agent Run Log** row (Status = Partial, "⏳ Run in progress…") and append the
  day's **verbatim entries** (timestamps + nesting preserved) to its page body. This is the durable
  record of the day — it must exist before anything is cleared, and its URL is the provenance link every
  tracker row created this run carries.

**Phase 2 — Core trackers** (one batch where possible) (`references/tasks-achievements-mood.md`)
- Update existing tasks (progress notes, completions); create new tasks/projects for clear next actions;
  add a row to **Tasks Created by the Journal Steward** for each task created.
- Log **Achievements**: append to the Achievements Log page **and** add rows to the Achievements Tracker
  DB (tier-classified, conservative).
- Log **Feelings & Mood** for any feelings/mood expressed (as expressed, not inferred).

**Phase 3 — Goals & flags** (one batch) — skip individually if budget is low, noting it in the run log
- **Goals** + **Goal Measurements** (`references/goals.md`)
- **Important Flags** lifecycle (`references/important-flags.md`)

**Phase 4 — Delegations** (`references/delegations.md`)
- Extension hooks for delegated sibling skills (the core ships none). If you've wired any, run the
  inline ones here against the entries captured in Phase 1 and note the results in the run log.

**Phase 5 — Clear + carry-over (critical path #2)** (`references/clear-and-carryover.md`)
- **Clear** the journal's processed day (its entries live in the Phase-1 capture) and build **tomorrow's
  carry-over callout** — grouped `<details>` toggles by category + all Active/Carrying-Over Important
  Flags. Runs after Phase 3 so flag statuses are current; never skip it.

**Phase 6 — Finalize the run log** (`references/run-log.md`)
- Update counts, set Status (Success/Partial/Failed), write Actions Summary, Carry-Over Context,
  Issues / Uncertainties, and a brief Retrospective.

## Cross-cutting rules (quick reference; details in the phase files)

- **Linking:** prefer linking existing pages over creating duplicates; if several pages match, link the
  most likely and note the uncertainty. Only create a **Project** for an ongoing multi-step effort, not a
  one-off.
- **Task extraction:** create a task for a concrete next step; update on progress; mark complete only
  when the journal clearly says it's done.
- **Mood:** capture what was expressed (positive and negative), never inferred.
- **Achievements:** classify tier conservatively; when unsure, choose the lower tier.
- **Provenance links:** almost every tracker row carries a `Journal Digest Link` / `Origin Journal
  Digest` URL — set it to the Agent Run Log entry you created in Phase 1, so create that row first and
  reuse its URL everywhere (see `references/databases.md` §2).
- **Journal-presence signal:** other skills rely on "the owner journaled today" = a top-level date
  toggle for today with ≥ 1 entry on the Interstitial Journal page. Preserve the date-toggle shape when
  clearing (see `references/databases.md` §1).

## When to ask for clarification (don't guess)

- You can't find the Interstitial Journal page.
- You can't confidently find where to create a task/project.

## Reference index

| File | Use it for |
|------|------------|
| `references/databases.md` | **Master schema/ID map** for every database + page. Source of truth — read first. |
| `references/notion-mcp-mapping.md` | Notion-AI primitive → Notion MCP tool mapping; date/relation/status gotchas. |
| `references/run-log.md` | Agent Run Log incremental write protocol, the verbatim capture, and reading prior runs. |
| `references/tasks-achievements-mood.md` | Tasks/Projects extraction, Tasks Created log, Achievements (page + DB), Mood. |
| `references/goals.md` | Goals + Goal Measurements. |
| `references/important-flags.md` | Important Flags DB + lifecycle (carry-over visual lives in clear-and-carryover). |
| `references/clear-and-carryover.md` | Clearing the journal, date toggles, and the carry-over callout format. |
| `references/delegations.md` | Extension hooks for delegated sibling skills (none ship in the core). |
