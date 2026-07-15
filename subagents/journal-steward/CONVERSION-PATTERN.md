# Notion AI Agent → Claude Skill — Conversion Pattern

How the **Daily Journal Steward** was ported from an original Notion-AI custom agent, written down so
the next agent goes fast. Each agent becomes a **skill** (a `SKILL.md` + reference files) plus an
**invocation** (a scheduled task, an event-chain from another skill, or on-demand). New ports reuse the
shared assets already built.

## The recipe (what we did for DJS)

1. **Get the verbatim instructions.** Notion custom-agent instructions live in the agent's settings UI
   and are **not exposed by the Notion API/MCP** — so they must be pasted in (or read from the Notion UI
   via the browser). This is the one manual step per agent.
2. **Map the databases/pages it touches.** Bake every collection/page id, property, and option value
   into a reference file — see `daily-journal-steward/references/databases.md` for the shape (and reuse
   it directly for anything touching the journal-owned databases).
3. **Author the skill**: a lean `SKILL.md` orchestrator + one reference file per subsystem, with the
   schemas baked in (so runs never fetch schemas). Translate Notion-AI primitives via
   `daily-journal-steward/references/notion-mcp-mapping.md`.
4. **Choose the invocation** (see the archetypes below). Anything date-math-heavy (session-day gates,
   biweekly anchors) belongs in a small deterministic script, not in prose the model re-derives.
5. **Dry-run test** against real Notion content (read-only preview, no writes), review, then wire it
   into DJS's delegation hooks (`daily-journal-steward/references/delegations.md`).

## Shared, reusable assets (already built)

- `daily-journal-steward/references/databases.md` — the journal-owned DB/page map: ids (placeholders),
  properties, and option values.
- `daily-journal-steward/references/notion-mcp-mapping.md` — MCP tool mapping + date/relation/status
  gotchas.
- `../../seneschal/references/` + `../../seneschal/scripts/` — the framework's autonomy policy, comms
  bridges (email/chat), and scheduling machinery, so a ported agent never grows its own.

## Invocation archetypes

The original workspace ran the journal steward plus several satellite agents; each mapped onto one of
three invocation shapes, which is the menu for any new port:

| Archetype | Shape | Notes |
|-----------|-------|-------|
| **Scheduled** | its own recurring task (like DJS's daily 5:00 AM run) | For pipelines that must run whether or not anyone asks. Wire via `../../seneschal/scripts/SCHEDULING.md`. |
| **Inline / event-chained** | loaded and executed by a parent skill in the same session, right after the event it reacts to | Cheapest: no extra schedule. E.g. a scanner that runs on each new capture the parent just created. The parent notes the delegated run (and counts) in its run log. |
| **On-demand** | invoked by name/@mention when the owner asks | For irregular work. The parent skill's only duty is a carry-over note when the on-demand work is outstanding. |

Two rules that made the delegations clean:

- **Single ownership.** When a sub-skill owns a database, the parent stops writing to it entirely — one
  writer per tracker, no drift.
- **Nothing silently dropped.** If the parent notices work that belongs to an on-demand sub-skill, it
  leaves a carry-over note rather than doing (or forgetting) the work itself.

Anything **outbound** (a sub-skill that emails or messages someone) goes through the framework's comms
bridges (`../../seneschal/scripts/` — `google_*` / `proton_*`) and is **draft-and-hold** under the autonomy
policy — a ported agent never gets its own send path.

## Status

The pattern is proven: the Daily Journal Steward shipped this way, and four private satellite agents
were ported with the same recipe in the original workspace (they are personal and not part of this
repo). To port your own: follow the recipe, pick an archetype, and wire the hook in
`daily-journal-steward/references/delegations.md`.
