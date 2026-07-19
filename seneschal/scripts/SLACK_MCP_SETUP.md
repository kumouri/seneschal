# Give the presence daemon Slack hands (one-time, optional)

**Why this exists.** The assistant drafts Slack replies and **holds** them for approval (draft-and-hold — see
`../docs/slack-draft-and-hold-spec.md`). When the owner approves one over Telegram (*"send a7"*), the resident
`presence.py` daemon's warm session **understands** the approval — but the headless `claude` it runs does
**not** inherit the interactive Slack connector, so it may have no way to *post* it. That's the **Slack-hands
gap**: the approval is recorded `status: "approved"` and drains on the next Slack-capable turn (interactive
`/assistant`, the next Triage pass). This wires Slack into the daemon so a Telegram-approved send **posts
immediately** (the spec's Q9 ruling).

> **This grants HANDS, not autonomy.** Sending to Slack is **ask-high, permanently** — outside the
> self-graduation path (`../references/autonomy-policy.md`). Wiring Slack here does **not** let the assistant
> post on its own; only the owner's explicit per-item `send <id>` fires a post. It just means the post happens
> *now* instead of on the next Slack-capable run.

The server you wire **must expose the same `slack_*` tool names the assistant's skills use** —
`slack_send_message`, `slack_read_thread`, `slack_read_channel`, `slack_search_public_and_private`, … Slack's
**official hosted MCP server** — **`https://mcp.slack.com/mcp`** (the same connector the claude.ai app uses)
— does; the community npm `server-slack` uses different names, so it won't match the skills. Pick **one** of
the two paths below — Path A is simplest and matches exactly.

## Path A — add Slack at user scope (recommended)

The daemon runs `claude` from the repo, so any **user-scoped** MCP server is inherited automatically — no
`--slack-mcp`, no code, no JSON file. Use the **same** Slack connector the app uses so the tool names match.

1. Add Slack's hosted MCP server at user scope:
   ```sh
   claude mcp add --scope user --transport http slack https://mcp.slack.com/mcp
   ```
2. Authenticate once (interactive): run `claude` in a normal terminal, then `/mcp` → **slack** → complete
   the OAuth flow in the browser. The token is cached under your user profile.
3. **Restart the daemon** (`reseneschald`, or re-run `run-presence.cmd`). Its spawned `claude` processes now
   inherit the authenticated Slack server.
4. Verify (below).

## Path B — an explicit daemon-scoped config (auto-detected)

Use this if you want Slack isolated to the daemon (not added to every interactive session). `presence.py`
passes it as `--mcp-config` — **alongside** the store's MCP config (e.g. the Notion backend's) — to every
`claude` it spawns (warm chat + slots + peek). The CLI accepts multiple space-separated configs after one
`--mcp-config`, so the store and Slack load together: the warm session can both *understand* `send a7` and
*post* it.

1. `cp slack-mcp.json.example slack-mcp.json` (git-ignored). It already points at Slack's hosted MCP server
   (`https://mcp.slack.com/mcp`) — the same connector the app uses, so the `slack_*` tool names match.
2. Authenticate once: `claude --mcp-config seneschal/scripts/slack-mcp.json` in a terminal, then `/mcp` →
   authenticate. The cached token is reused by headless runs using the same config.
3. **Nothing to wire** — `presence.py` **auto-detects** `scripts/slack-mcp.json` and uses it by default.
   (Override the path with `--slack-mcp <file>`; force it off with `--no-slack`.) On startup the daemon logs
   `Slack wired for headless runs (send/read): …` when it finds a config.
4. **Restart the daemon.** Verify (below).

## Verify

Have the assistant hold a Slack draft (run a Slack triage, or ask it to draft a reply), then approve it over
Telegram: *"send a7"*. With Slack wired it should **post it and report the send**; without it, the assistant
says *"approved — I don't have Slack hands in this session,"* records `status: "approved"`, and it goes out on
the next Slack-capable turn. You can also watch the daemon console for the `Slack wired …` line at startup.

## Notes

- **Sends stay ask-high regardless.** This wiring never changes the gate — see the boxed note above.
- If Slack is **not** wired, nothing breaks: drafting still works, approvals just park as `approved` and
  drain on the next interactive `/assistant` or Triage pass (the documented gap, not a failure).
- Keep `ANTHROPIC_API_KEY` unset for the daemon (billing safety); the daemon scrubs it from child env
  regardless (`run-presence.cmd`, `presence.py`).
- The daemon threads **both** the store's and Slack's configs into each spawned `claude`; wiring Slack does
  not disturb the store (acks still persist).
