# Daily Journal Steward — Claude port

A faithful Claude version of an original Notion "Daily Journal Steward" AI agent, as a **skill** + a
**daily recurring task**. It reads the owner's Interstitial Journal, captures the day's verbatim entries
into the run log, extracts tasks/projects, logs achievements/mood/goals/flags into the Notion databases,
clears the journal, builds tomorrow's carry-over, and writes an Agent Run Log entry.

## Files

```
daily-journal-steward/
├── SKILL.md                      # orchestrator: critical path, budget rules, phase plan
├── README.md                     # this file (setup + cutover)
└── references/                   # one file per subsystem, with the Notion schemas baked in
    ├── databases.md              # MASTER schema/ID map — source of truth (placeholder ids)
    ├── notion-mcp-mapping.md     # Notion-AI primitive → Notion MCP tool + gotchas
    ├── run-log.md                # incremental run-log protocol + the verbatim capture
    ├── tasks-achievements-mood.md
    ├── goals.md
    ├── important-flags.md
    ├── clear-and-carryover.md
    └── delegations.md            # extension hooks for delegated sibling skills
```

All database/page ids in `references/databases.md` are **placeholders** — the store setup flow fills a
local (gitignored) copy with your workspace's real ids.

## How the recurring task uses the skill

A scheduled Claude run starts a fresh session, so the schedule's **prompt loads `SKILL.md` from this
folder** and executes it. No capability install required. (Optional: install it via **Settings →
Capabilities** so you can also invoke it by name in any chat.)

## Daily schedule config

Enable a daily scheduled task with:

- **Schedule:** `0 5 * * *`  (daily at **5:00 AM**, in the owner's configured timezone)
- **Prompt:**

  > Daily Journal Steward — daily run. Read the skill at
  > `<repo>\subagents\journal-steward\daily-journal-steward\SKILL.md`
  > and follow it exactly to process the owner's Interstitial Journal for the most recent completed
  > journal day, using the connected Notion MCP. Use the owner's configured timezone; treat
  > after-midnight entries as the prior day. Honor the skill's critical-path-first /
  > never-load-schemas / batch-aggressively rules and write the Agent Run Log entry incrementally.
  > Only pause to ask if you hit one of the skill's "ask for clarification" conditions.

The framework's scheduled-task wiring lives in `../../../seneschal/scripts/SCHEDULING.md`.

## Cutover checklist

1. ⬜ Run store setup so a local copy of `references/databases.md` carries your real Notion ids.
2. ⬜ **Turn off any existing journal automation** (e.g. an original Notion agent), so only one system
   processes the journal.
3. ⬜ Enable the daily schedule above.
4. ⬜ Watch the first live run: check the cleared journal + carry-over callout and the
   **🧠 Agent Run Log** entry (its page body should hold the day's verbatim entries). The run log's
   incremental writes mean even a partial run leaves a trace.

## Extending with delegated sub-skills

The original agent delegated satellite subsystems (scanners and senders) to sibling agents. The core
ships none of them, but the hook points survive: `references/delegations.md` describes the wiring
archetypes, and `../CONVERSION-PATTERN.md` is the recipe for porting another Notion-AI agent into a
sibling skill.

## Known porting notes

- **Run Log has no Goals count fields** → those counts go in `Actions Summary` text. (Add number fields
  in Notion if you want them tracked numerically.)
- **The verbatim capture lives in the run-log page body.** If you want a dedicated per-day archive
  (a digest page or a transcripts database), add an archive step before the journal clear and point the
  provenance URLs at it — see `references/databases.md` §2.
- Schemas are **baked into `references/databases.md`** so runs never fetch schemas (the original agent's
  #1 budget rule). If you change a database in Notion, update your local copy of that file.
