# Give the presence daemon Notion access (one-time)

**Why this exists.** The resident `presence.py` daemon (`\seneschald`) drives the assistant's Telegram
chat and the scheduled slot runs by spawning a **headless `claude` CLI**. That headless process does
**not** inherit the interactive Notion connector you use in the app — so, unconfigured, the assistant can
*chat* but can't read/write Notion. When that happens it will say things like *"Notion's not reachable
this session"* and your "mark it done" acks never reach the Tasks / ⏰ Reminders DBs. This wires
**read/write** Notion into the daemon, giving the Telegram assistant the same Notion abilities as the full
`seneschal` skill.

> **The bug this closes:** you tell the Telegram assistant *"took my meds"*, it says *"got it"* — but the
> ack only lives in the volatile warm session, never the ⏰ Reminders DB. A reboot clears the session, the
> next reminder run reads a still-un-acked row, and it **re-nudges you for something you already did**.
> With Notion wired, the assistant writes the ack straight through to Notion, so it survives the reboot
> and the loop stops. (See `seneschal/SKILL.md` Chat mode + the persona's no-false-writes principle.)

> Symptom this fixes: you tell the assistant "I did these," it says "✅ done," but the Tasks DB / brief
> never reflect it — because the write had nowhere to go. (The persona also refuses to claim a write it
> can't actually make — but it still needs *access* to do the work.)

Pick **one** of the two paths below. Path A is simplest and matches the assistant's skills exactly.

## Path A — add Notion at user scope (recommended)

The daemon runs `claude` from the repo, so any **user-scoped** MCP server is inherited automatically — no
`--notion-mcp`, no code. Use the **same** hosted Notion MCP the app uses, so the tool names
(`notion-fetch`, `notion-query-data-sources`, `notion-update-page`, …) match the skills.

1. Add the server at user scope:
   ```sh
   claude mcp add --scope user --transport http notion https://mcp.notion.com/mcp
   ```
2. Authenticate once (interactive): run `claude` in a normal terminal, then `/mcp` → **notion** →
   complete the OAuth flow in the browser. The token is cached under your user profile.
3. **Restart the daemon** (`\seneschald`, or just re-run `run-presence.cmd`). Its spawned `claude`
   processes now inherit the authenticated Notion server.
4. Verify (below).

## Path B — an explicit daemon-scoped config (auto-detected)

Use this if you want Notion isolated to the daemon (not added to every interactive session). `presence.py`
passes the config as `--mcp-config` to every `claude` it spawns (warm chat + slots + peek), so the whole
daemon — Telegram chat included — gets read/write Notion.

1. `cp notion-mcp.json.example notion-mcp.json` (git-ignored). It defaults to the hosted server
   (`https://mcp.notion.com/mcp`). *(A non-interactive internal-integration token variant is included in
   the example if you'd rather not use OAuth — but its tool names differ from the assistant's skills, and
   the integration must be granted **read + write** in Notion, not read-only, or acks still won't land.)*
2. Authenticate once: `claude --mcp-config seneschal/scripts/notion-mcp.json` in a terminal, then `/mcp` →
   authenticate. The cached token is reused by headless runs using the same config.
3. **Wire it through the store config (preferred).** With the Notion backend active, `store/config.json`'s
   `backends.notion.mcp_config` points at this file (default `seneschal/store/notion/mcp.json`), and
   `presence.py` forwards it automatically (`resolve_store_mcp`). As a legacy fallback, when there's **no**
   `store/config.json`, the daemon still **auto-detects** `scripts/notion-mcp.json`. Override the path with
   `--store-mcp <file>` (the deprecated `--notion-mcp` alias still works); force it off with `--no-notion`.
   On startup the daemon logs `Store: notion (MCP wired for headless runs: …)` — or, for a filesystem
   backend, `Store: <backend> (filesystem — no MCP needed)`, or a warning / `run /setup-store` hint if it
   found no usable config.
4. **Restart the daemon.** Verify (below).

## Verify

Message the assistant on Telegram: *"What's the top open task in my Tasks DB right now?"* It should answer
from Notion (not "I can't reach Notion"). Then tell it to mark something and **check the DB actually
changed**. You can also watch the daemon console: the reminder **seed** run (date-rollover;
`maybe_seed_day`) should enqueue nudges without a Notion error.

## Notes

- **Share the databases with the integration** if you use the internal-token (stdio) variant — internal
  integrations only see pages/DBs explicitly shared with them. The hosted OAuth server sees what your
  Notion account can.
- The daily reminder **seed** the daemon runs (`maybe_seed_day`, date-rollover — see `SCHEDULING.md` /
  `presence.py`) reads the ⏰ Reminders DB — so **without** Notion access it can't enqueue the DB-habit
  nudges (only ad-hoc chat-set reminders fire). Notion access + the daemon's seed together restore
  DB-driven reminders.
- Keep `ANTHROPIC_API_KEY` unset for the daemon (billing safety); the daemon scrubs it from child env
  regardless (`run-presence.cmd`, `presence.py`).
