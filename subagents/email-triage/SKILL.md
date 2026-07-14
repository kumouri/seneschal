---
name: email-triage
description: >-
  The assistant's email triage. Screens the owner's email — summarizes and categorizes the inbox,
  auto-archives obvious noise, and drafts replies in the assistant's own voice from its configured
  address (e.g. assistant@example.com), held for approval. Primary mailbox is Proton (the assistant's
  own address via Proton Bridge); the Gmail MCP is the fallback/secondary. Use for "triage my email",
  "what's in my inbox", "draft a reply to …". Delegated to by the seneschal orchestrator (Triage mode).
compatibility: >-
  Proton path requires Proton Bridge running + seneschal/scripts/proton.env (see scripts/EMAIL_SETUP.md).
  Fallback path requires the Gmail MCP (mcp__*__*-search_threads / create_draft / label_*).
---

# Email Triage (Seneschal · Triage mode)

Turn the owner's inbox into signal. Summarize + categorize + archive obvious noise (act-low); **draft
replies as the assistant from its own configured address, held for approval** (ask-high to send). See
`../../seneschal/references/comms-mapping.md` and `../../seneschal/references/autonomy-policy.md`.

## Channels — triage BOTH inboxes (the owner's choice)

Read **both** in one sweep and present a single combined triage, tagging each item with its source.

1. **Gmail (the owner's real inbox).** The connected Gmail MCP — `*-search_threads`, `*-get_thread`,
   `*-label_thread`/`*-label_message`, `*-create_draft`. This is where most real mail lands. Read +
   label; Gmail here is **draft-only** (can't auto-send).
2. **Proton (the assistant's own address, e.g. `assistant@example.com`).** Read via
   `python ../../seneschal/scripts/proton_read.py --mailbox INBOX --env-file
   ../../seneschal/scripts/proton.env …`. **Requires Proton Bridge running** (`scripts/EMAIL_SETUP.md`);
   if `--check-auth` fails, note Proton is unreachable and continue with Gmail. (The Proton inbox may be
   empty until mail is routed there.)

**Sending is always Proton.** Regardless of which inbox an item came from, the assistant replies **from
its own address via `proton_send.py`** (ask-high). A Gmail reply would come from the wrong address, so
use Proton to send; a Gmail draft is only a stopgap if Proton is down. Because the reply leaves from a
different address than the original thread, open with context (e.g. *"I'm <assistant>, <owner>'s
assistant, following up on your note to them…"*).

## Steps

**1 — Scope.** Default: unread since the last triage / last ~24h. (Proton: `--unseen` or `--since`.)

**2 — Read, in one pass.** Pull the candidate messages (sender, subject, snippet; bodies only when a
draft is needed).

**3 — Classify each:**
- **Needs a reply / action** — a real ask to the owner.
- **FYI** — worth knowing, no action.
- **Noise** — machine-generated mail with no reply expected. Use the **canonical noise definition** in
  `../../seneschal/references/comms-mapping.md` (Email → Triage behavior): newsletters/marketing,
  receipts, shipping updates, calendar **system** notifications (not real invites), social/app
  notifications, and expired/used OTP codes — and honor its "never auto-archive" guard list (humans,
  actionable items, live security/identity alerts). *When unsure, it is NOT noise.*

**4 — Act:**
- **Noise → archive/label** (act-low). Proton: leave read+filed; Gmail: `label_thread` (e.g. an
  `Archived`/`Triaged` label) — define the exact label set on first run and record it in
  `comms-mapping.md`.
- **Needs a reply → draft** in the assistant's voice from its own address (it writes as the assistant,
  e.g. *"Hi — I'm <assistant>, <owner>'s assistant; they asked me to…"*). **Hold it** — Proton: build
  with `proton_send.py --dry-run` and show it; Gmail: `create_draft`. **Send only on explicit approval.**

**5 — Deliver the triage summary** (in chat for now):
```
Email — <window> (<source: Proton/Gmail>):
⏳ Needs you (N): <sender> — <ask>   [draft ready]
FYI (M): <sender> — <one-liner>
🗑 Archived as noise (K): <brief categories>
```

## Guardrails

- **The assistant writes as itself, never as the owner.** Signature: "— <assistant>, <owner>'s
  assistant."
- **Ask-high to send.** Drafts wait for approval; nothing leaves without it.
- **Conservative archiving.** Only clearly-automated noise is auto-archived; anything from a real person
  stays in the inbox and gets surfaced.
- **Cite** sender + subject; never invent a message. Resolve senders against Notion **People** when useful.
