---
name: slack-triage
description: >-
  The assistant's Slack triage. Screens the owner's Slack — DMs, mentions, and busy channels (e.g.
  #team, #eng) — summarizes what happened, and surfaces what actually needs a reply, drafting responses
  (held for approval) where useful. Use for "triage my Slack", "what did I miss on Slack", "anything
  need me in Slack". Delegated to by the seneschal orchestrator (Triage mode).
compatibility: Requires the Slack MCP (mcp__*__slack_*). Sender lookups use the configured seneschal store's People domain (run /setup-store; the Notion backend needs the Notion MCP) — optional. Reads the assistant's persona + references.
---

# Slack Triage (Seneschal · Triage mode)

Screen the owner's Slack and hand them **signal, not a transcript** — in the persona's voice,
decision-first. Reading and summarizing is act-low. **Sending/posting is ask-high** — draft-and-hold,
never auto-send (`../../seneschal/references/autonomy-policy.md`).

**Store access (optional, for People).** This mode's data lives in Slack, not the store. It touches the
store only to resolve a sender to a known person: `store-search`/`store-query` the **People** domain (the
mapping resolves it to the active backend — on Notion, `mcp__*__notion-*`; see
`../../seneschal/store/config.json` for which backend). If the store isn't configured, skip the lookup.
**Never fetch schemas at runtime.**

The owner's Slack `user_id` (the connected account) is a per-install value — resolve it once
(`slack_search_users` / `slack_read_user_profile`) and record it in your local copy of
`../../seneschal/references/comms-mapping.md` (Slack section, which also has the tool mapping + send
rules). Examples below use the placeholder `U00000000`.

## Steps

**1 — Scope the window.** Default: since the owner's last triage / last ~24h (ask if ambiguous). Use
Unix timestamps for `before`/`after`.

**2 — Gather, read-only, in parallel:**
- **Mentions of the owner:** `slack_search_public_and_private` with `to:me` and/or `from:` filters, plus
  a query for `<@U00000000>` (the owner's id). `sort=timestamp`.
- **DMs:** for each active DM, `slack_read_channel` with the **`user_id` as `channel_id`** (DM history).
- **Threads they're in / busy channels:** `slack_read_thread` for threads surfaced above; skim
  high-volume channels (e.g. #team, #eng) only if the owner asked.

**3 — Classify each item** (resolve senders against the store's **People** domain — `store-search`/`store-query` — when useful):
- **Needs a reply** — a direct question/ask to the owner.
- **FYI** — useful to know, no action.
- **Noise** — automated/bot chatter, resolved threads. (Bot messages excluded by default.)

**4 — Deliver the triage** (in chat for now):
```
Slack — <window>:

⏳ Needs a reply (N)
  - <#channel / DM with @person>: <one-line gist of the ask>   [draft ready ↓]
FYI (M)
  - <#channel>: <one-liner>
```
For each "needs a reply," **draft a reply in the persona's voice** and show it; do **not** send. On the
owner's "send it," post via `slack_send_message` (or `slack_send_message_draft` to leave them a Slack
draft).

## Guardrails

- **Ask-high to send/post.** Drafts are held; sending waits for explicit approval.
- **Summarize, don't dump.** Collapse busy channels to a line or two; link rather than paste walls.
- **Cite the channel/DM** for each item so the owner can jump to it. Don't invent messages.
- The assistant writes **as itself** ("<assistant>, <owner>'s assistant"), not as the owner.
