# Chapter: mcp

The MCP servers the skills lean on: **notion** (the store backend's tools, when the store is
Notion), **calendar** (time and availability), and **slack** (optional — triage reads +
approved sends). Three ledger chapters: `mcp:notion`, `mcp:calendar`, `mcp:slack`. This
chapter also owns the manifest's two `handled_by: mcp` config files (`slack-mcp.json`,
`notion-mcp.json`) — when a Path B below writes one, mark its `env:<id>` chapter too, so the
board and the doctor stay coherent.

**The one hard truth of this chapter — the restart dance:** `claude mcp add` registers a
server, but the *current* session can neither OAuth it nor see its tools. The flow is
always: register → mark the chapter `awaiting-auth-restart` → tell the owner **loudly** →
they restart the session, authenticate via `/mcp`, run `/setup` — which resumes **right
here** and verifies. Say it exactly that plainly:

> **Restart this session now, then run `/setup` — it resumes right here.**
> (After the restart: `/mcp` → the server → complete the browser OAuth, if it hasn't already
> prompted you.)

## mcp:notion

Only relevant when `seneschal/store/config.json` says the active backend is `notion`
(filesystem backends need no MCP — mark `mcp:notion` done with summary "not needed —
filesystem store" and move on; no store configured yet → the store chapter comes first).

Usually the store chapter **already verified this** during provisioning — so this is a
**confirm, not a re-do**: one cheap probe (`notion-get-teams`, or any trivial `notion-*`
call). Probe answers → `mark mcp:notion done --summary "verified in-session"`. Probe fails
or the tools are absent from this session:

- Walk the owner through `seneschal/scripts/NOTION_MCP_SETUP.md` — Path A (user-scope
  `claude mcp add`, recommended) or Path B (the daemon-scoped
  `seneschal/scripts/notion-mcp.json` copied from its example; Path B also →
  `mark env:notion-mcp done --artifacts seneschal/scripts/notion-mcp.json`).
- Then the restart dance: `mark mcp:notion awaiting-auth-restart --summary "registered; OAuth + restart pending"`.

## mcp:calendar

There is **no config file** for calendar — it's a user-scope registration plus connector
auth, and `seneschal/references/calendar-mapping.md` documents the tool surface the skills
expect (`list_calendars`, `list_events`, …). Ask whether the owner wants calendar wired
(skippable; without it Brief/Wrap simply have no schedule section — say so). If yes:

1. Ask which calendar connector they use (the provider's hosted MCP server for Google /
   Outlook / etc. — the owner supplies the URL from their provider's docs; never guess one).
2. Confirm, then register: `claude mcp add --scope user --transport http calendar <url>`.
3. The restart dance: `mark mcp:calendar awaiting-auth-restart`.

## mcp:slack (optional)

Enable-question first: Slack triage + draft-and-hold replies — worth wiring? (Sends stay
ask-high forever regardless; wiring grants hands, not autonomy.) Decline →
`mark mcp:slack declined --summary "no Slack; slack-triage stays dark"`.

Both paths are `seneschal/scripts/SLACK_MCP_SETUP.md`, and the choice matters:

- **Path A — user scope (recommended):** `claude mcp add --scope user --transport http slack
  https://mcp.slack.com/mcp` (the hosted server whose `slack_*` tool names match the
  skills). Then the restart dance.
- **Path B — daemon-scoped config:** copy `slack-mcp.json.example` → `slack-mcp.json`
  (auto-detected by the daemon; keeps Slack out of interactive sessions). Also
  `mark env:slack-mcp done --artifacts seneschal/scripts/slack-mcp.json`. The one-time
  OAuth still needs an interactive `claude --mcp-config …` + `/mcp` pass — the doc walks
  it; then the restart dance.

## On resume (status `awaiting-auth-restart` at entry)

For each server so marked, **one cheap in-session probe**:

| Server | Probe |
|---|---|
| notion | `notion-get-teams` |
| calendar | `list_calendars` |
| slack | one `slack_search_channels` (any term) |

- Probe answers → `mark mcp:<server> done --summary "OAuth complete; probe verified"`.
- Tools still absent from the session → the restart likely didn't happen, or the add didn't
  take: check `claude mcp list` (does the server show?), re-state the restart dance.
- Tools present but the call errors with an auth failure → `/mcp` → the server → finish the
  browser OAuth, then re-probe. Still failing → leave `awaiting-auth-restart`, point at the
  server's setup doc, move on — never spin here.
