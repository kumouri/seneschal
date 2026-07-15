# Agent Run Log (Critical Path #1 + cross-run memory)

Database: **🧠 Agent Run Log** `collection://00000000-0000-0000-0000-000000000016`. This is DJS's memory
across runs, the durable capture of each processed journal day, and its primary defense against losing
history if a run is cut short.

## Reading — Phase 0 of every run

- Query the last **3–5** entries ordered by `Run Date` descending.
- Read **Carry-Over Context** (things flagged for this run), **Issues / Uncertainties** (unresolved
  items), and the most recent **Retrospective** (self-improvement notes).
- Use it to avoid re-creating tasks you already made, follow up on patterns, and resolve prior
  uncertainties.

## Writing — INCREMENTAL (write as you go, not all at once)

Do **not** wait until the end. Create the row first; update it after each major phase. If the run dies
mid-way, a useful partial log still exists.

**Step A — create the row (Phase 1, before anything else is written or cleared):**
- `Date` (title): run date, e.g. `2026-06-09`
- `Run Date`: the date value
- `Status`: `Partial` (updated at the end)
- `Actions Summary`: `⏳ Run in progress…`
- `Themes`: whatever you've identified so far
- **Page body**: the processed day's **verbatim entries** — timestamps and toggle nesting preserved
  (`notion-mcp-mapping.md` has the Markdown shapes). This is the capture the journal clear depends on,
  and this row's URL is the provenance link (`Journal Digest Link` / `Origin Journal Digest`) every
  tracker row created this run carries.

**Step B — append to the page body** after each phase, via `notion-update-page` (body, not properties).
Add a running log, e.g.:
```
✅ Phase 1 complete: day captured. Entries: 12.
✅ Phase 2 complete: Tasks created: 3. Achievements: 8. Mood entries: 2.
✅ Phase 3 complete: Goals updated: 1. Flags: 1 new, 1 resolved.
✅ Phase 5 complete: journal cleared, carry-over built.
```
Use the body for reasoning too: why an achievement got a tier, why you created/skipped a task, edge
cases, and recurring-pattern observations.

**Step C — finalize at the very end:** update properties with final counts and set `Status` to `Success`
or `Partial`; rewrite `Actions Summary` as a concise bullet list; fill `Carry-Over Context`,
`Issues / Uncertainties`, and `Retrospective`.

## Properties

- `Date` (title) · `Run Date` (date) · `Status` (`Success`/`Partial`/`Failed`)
- `Tasks Created` · `Tasks Updated` · `Tasks Completed` · `Achievements Logged` · `Mood Entries` (numbers)
- `Themes` (multi: Work, Health, Hobbies, Relationships, Errands, Gaming, Self-Care, Productivity)
- `Actions Summary` · `Carry-Over Context` · `Issues / Uncertainties` · `Retrospective` (text)

**No numeric field exists for Goals.** Put "Goals created/updated: N" in **Actions Summary** text.

## Retrospective — make it useful

Brief self-reflection: what went well, what could improve, patterns noticed across recent runs, and
suggestions for handling similar situations better next time. This is what the next run reads first.

## Budget note

If you've completed 5+ steps with several remaining, make sure the Step-A row already exists (it
should). If budget is tight after the capture, skip non-critical trackers — but never the journal clear
+ carry-over (Phase 5) — and record exactly what you skipped in **Carry-Over Context** so the next run
can pick them up.
