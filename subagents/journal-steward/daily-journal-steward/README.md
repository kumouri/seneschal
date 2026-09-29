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

## How the daily run is fired — the presence daemon owns it

**No Task Scheduler entry and no Claude Desktop scheduled task.** The resident daemon has a built-in
slot scheduler: `SLOTS_TEMPLATE` in `../../../seneschal/scripts/presence.py` carries
`{"name": "daily-journal", "at": "05:00"}`, whose prompt is *"Run the Daily Journal
(subagents/journal-steward/daily-journal-steward/SKILL.md). Use {tz}. Run silently."* (`{tz}` is
rendered from the owner's configured timezone). `maybe_run_slots` fires it at most once per local day
and spawns a fresh headless `claude -p` for it, so the run loads `SKILL.md` from disk exactly as a cold
session would. Full cadence, the catch-up window (`--slot-catchup-min`, default 180 min) and the kill
switch (`--no-slots`): `../../../seneschal/scripts/SCHEDULING.md` §1.

The **time lives in `presence.py`** (machine-local wall clock, assumed to match the owner's timezone),
so moving 05:00 is a code change there, not a schedule someone enables. (A standalone Desktop scheduled
task still works — SCHEDULING.md lists it as the legacy path — but with the daemon running it would
double-process the journal.)

**One duplicate-processing risk the repo cannot see:** if an original journal automation (e.g. a
Notion "Daily Journal Steward" agent) is still switched on in the workspace, both systems process the
same journal. Whether it is off is a workspace fact, not a tracked file — hence checklist step 2.

## Cutover checklist

1. ⬜ Run store setup so a local copy of `references/databases.md` carries your real Notion ids.
2. ⬜ **Turn off any existing journal automation** (e.g. an original Notion agent), so only one system
   processes the journal.
3. ✅ Daily run wired — the daemon's `daily-journal` slot at 05:00 (above); nothing to enable by hand
   once `/setup daemon` has installed `seneschald`.
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
