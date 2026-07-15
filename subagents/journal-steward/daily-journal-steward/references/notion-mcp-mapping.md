# Notion MCP Mapping & Gotchas

The original agent was written for Notion's built-in AI (primitives like `createPage`, `updatePage`,
`loadDatabase`, `createAndRunThread`). This skill runs through the **Notion MCP connector**. This file
maps the concepts and lists the mechanics that bite.

The MCP tools are named `mcp__<server>__notion-<suffix>` where `<server>` is whatever the connected
Notion MCP uses (a hash). Refer to tools by suffix; the live tool schema is authoritative for exact
payload shape — consult it when unsure, but get **names and option values from `databases.md`**, never
by fetching schemas at runtime.

## Primitive → MCP tool

| Original concept | MCP tool (suffix) | Notes |
|------------------|-------------------|-------|
| Read a page/db | `notion-fetch` | By page ID, URL, or `collection://…`. Use to read the Interstitial Journal and prior run-log entries. |
| Query a database | `notion-query-database-view` | SQL-like over the data source's SQLite projection (columns exactly as in `databases.md`). Use for "last 3–5 run logs", "active flags". |
| Semantic search | `notion-search` | Find pages by meaning (existing tasks/projects to link, prior entries). `query_type:"user"` to find a Notion user. |
| `createPage` | `notion-create-pages` | New standalone page **or** new DB row. Set `parent` to a page ID (e.g. the Achievements Log) or to the `collection://…` data source for a DB row. Properties by exact name; body in Notion-flavored Markdown. |
| `updatePage` | `notion-update-page` | Update properties and/or **append** body blocks. Use to finalize the run log, append to the Achievements Log page body, edit flag status. |
| `createAndRunThread` (sub-agent) | — | No direct equivalent. See `delegations.md` (inline / on-demand hooks + carry-over notes). |

## Writing properties — the gotchas

- **Dates:** pass ISO `YYYY-MM-DD` (or full datetime). In query projections these appear as
  `date:<Prop>:start`, `date:<Prop>:end` (null unless it's a range), `date:<Prop>:is_datetime` (0/1) —
  that's the *query* shape; for *writes* set the date property to the ISO value per the tool schema.
- **Relations:** set to the target page's URL/ID (an array; most accept several, but **Goal Measurements
  `Goal` is limit 1**). You must already have the target page's URL — search/fetch it first, or reuse a
  URL you just created (e.g. a task you created this run).
- **Status vs select:** both are set by passing the **exact option string including emoji** (e.g.
  `Done`, `🥉 Small`, `🔴 High`, `💼 Work`). Wrong/mis-cased strings fail. Lists of valid options are in
  `databases.md`.
- **Multi-select:** array of exact option strings. To add a brand-new option, just pass the new string
  (the original allows "add new options as needed" for Emotions, Themes, etc.).
- **Checkbox:** true/false (query projection shows `__YES__`/`__NO__`).
- **Formulas are read-only:** read them, never write. If a query doesn't return a computed value,
  compute the fallback deterministically (a small script beats prose re-derivation).
- **Don't set properties that don't exist.** Notably the Agent Run Log has no Goals count fields — put
  those in Actions Summary text (see `databases.md` §5).

## Body content — Notion-flavored Markdown

- **Timestamped journal entries** use date mentions:
  - Day header: `<mention-date start="2026-06-09"/>`
  - Timestamped entry: `<mention-date start="2026-06-09" startTime="09:53" timeZone="<owner's timezone>"/> text…`
- **Toggles** use `<details><summary>…</summary> …nested… </details>` (this is how the journal nests
  entries; preserve nesting when capturing verbatim).
- **Callouts** (the carry-over block) use a callout with an icon, e.g. `<callout icon="📌"> … </callout>`.
- **Append, don't overwrite:** when adding to the Achievements Log page or the run-log body, add new
  blocks rather than replacing existing content.

## Reading patterns you'll reuse

- **Last N run logs:** query the Agent Run Log (`00000000-0000-0000-0000-000000000016`) ordered by
  `date:Run Date:start` desc, limit 5.
- **Active flags for carry-over:** query Important Flags (`00000000-0000-0000-0000-000000000003`) where
  `Status` in (`Active`,`Carrying Over`).
