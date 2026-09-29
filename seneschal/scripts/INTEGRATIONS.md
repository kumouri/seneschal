# Integrations — what the assistant can reach, and what each one needs

A one-screen index of the integrations this framework ships. Each line names what the integration is
for, the config it reads, and the setup guide that owns the detail — the guides beside this file are
the source of truth; this page only points at them.

**The rule all of these share:** every optional integration **auto-detects** its config and degrades
to *absent* rather than failing. A missing `*.env` is a capability the assistant doesn't have, never
an error it raises. Every `*.env` / `*-mcp.json` is gitignored; only the `*.example` templates are
tracked. Anything outbound (sending mail, posting, writing calendar events, placing calls) is
draft-and-hold unless the owner has explicitly graduated it (`../references/autonomy-policy.md`).

| Integration | What it's for | Config (gitignored) | Setup |
|---|---|---|---|
| **Store MCP** (Notion backend) | Reads/writes to the system of record when the active store is Notion; forwarded into the daemon's headless `claude` | `notion-mcp.json` (or the store's own `mcp.json`) | [`NOTION_MCP_SETUP.md`](NOTION_MCP_SETUP.md), `../store/README.md` |
| **Calendar** | Brief/Watch calendar reads; event writes are draft-and-hold. Two doors: a Calendar MCP if connected, otherwise the Google REST bridge below — "no calendar" only when both fail | — | `../references/calendar-mapping.md` |
| **Google (Calendar + Gmail) REST bridge** | Stdlib OAuth bridge for one or more Google accounts (`google_auth.py`, `gcal_api.py`, `gmail_api.py`); reads/drafts act-low, sends ask-high | `google.env`; tokens in `../state/google_tokens.json` | [`GOOGLE_SETUP.md`](GOOGLE_SETUP.md) |
| **Assistant email (Proton Mail Bridge)** | The assistant's own address, send/read over local SMTP/IMAP (`proton_send.py`, `proton_read.py`) | `proton.env` | [`EMAIL_SETUP.md`](EMAIL_SETUP.md) |
| **Telegram bot** | Primary two-way chat + push channel (nudges, approvals, pickers, reactions, attachments) | `telegram.env` | [`TELEGRAM_SETUP.md`](TELEGRAM_SETUP.md), `../docs/telegram-inbound-spec.md` |
| **Discord bot** | Optional second two-way chat surface; gateway push with the uv venv, REST polling without | `discord.env` | [`DISCORD_SETUP.md`](DISCORD_SETUP.md) |
| **Slack MCP** | Slack Triage reads + held reply drafts; optional daemon "send hands" for approved sends | `slack-mcp.json` | [`SLACK_MCP_SETUP.md`](SLACK_MCP_SETUP.md), `../docs/slack-draft-and-hold-spec.md` |
| **Phone call-screener** (`../../phone/`) | Cloudflare Worker + Twilio: screens inbound calls; `POST /push-call` / `/push-sms` for `Call Me` reminders | `push-call.env`, `push-sms.env` (Twilio creds stay in Cloudflare) | `../../phone/README.md` |
| **Android presence & health feed** | The Call Shield app feeds presence (geofence/activity/sleep) and Health Connect sleep into `health_listener.py` over the LAN/tailnet | `HEALTH_INGEST_TOKEN` in the listener's environment | [`HEALTH_SETUP.md`](HEALTH_SETUP.md), `../../phone/android/README.md` |
| **Home Assistant** | Optional device control from presence context edges (`presence_actions.py` + `ha_client.py`); inert until configured, every automation draft-and-hold until approved | `ha.env`, `../state/presence-automations.json` | [`HA_SETUP.md`](HA_SETUP.md) |
| **Send gate** (every outbound above) | Enforces ask-high in code: a send reaching anyone but the owner is refused (exit 3) unless an approved row covers it (`send_gate.py`, `pending_approvals.py`); `send_gate_hook.py` puts the same decision in front of the Slack/Gmail MCP send tools | `owner.email` / `owner.emails` in `../../persona/identity.json`; approvals in `../state/pending-approvals.json`; the hook in `~/.claude/settings.json` | [`SEND_GATE_SETUP.md`](SEND_GATE_SETUP.md) |
| **Ollama** (local models) | RAG embedder (`nomic-embed-text`), the front-door Router classifier, and the salience sentiment cross-check; everything falls back gracefully when it's down | `rag.env`, `router.env`, `sentiment.env` (all optional) | [`RAG_SETUP.md`](RAG_SETUP.md), [`ROUTER_SETUP.md`](ROUTER_SETUP.md), [`SALIENCE_SETUP.md`](SALIENCE_SETUP.md) |
| **Demiurge** (Forge mode) | The sibling public repo that owns the Archon lifecycle machinery; always driven through its `claude-cli` adapter | a sibling checkout | `../references/archons.md` |

The unattended daemon itself (`presence.py`) runs on the owner's Claude subscription via
`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`, with `ANTHROPIC_API_KEY` left unset — see
[`SCHEDULING.md`](SCHEDULING.md). The `/setup` wizard walks every configurable surface above
(`../setup/env-manifest.json`), and `/doctor` reports which are live.
