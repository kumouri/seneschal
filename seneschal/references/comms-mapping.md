# Communications — channel mapping & gotchas

How the assistant reads and (draft-only, then ask-high to send) writes across channels. Fill in concrete
tool names + the noise definition per install as each channel is wired up.

## Email

The assistant's identity is its **own address** (e.g. `assistant@example.com`, Proton). **Read both
inboxes; send only via Proton.**

- **Sending (Proton).** Send via **Proton Bridge → SMTP**
  (`../scripts/proton_send.py`), read via **IMAP** (`../scripts/proton_read.py`). Bridge ports vary per
  install (e.g. SMTP `127.0.0.1:1026`, IMAP `127.0.0.1:1169`; STARTTLS, self-signed). Username =
  the Bridge **primary** account (not necessarily the assistant's alias); `PROTON_SENDER=<the assistant's
  address>`. Creds live in `../scripts/proton.env` (git-ignored). Setup: `../scripts/EMAIL_SETUP.md`.
  **Bridge must be running** for send/read. Gotcha: no inline `# comment` after a value in `proton.env`.
- **Reading (both):** the assistant triages **both** the owner's **Gmail** (connected Gmail MCP:
  `*-search_threads`, `*-get_thread`, `*-create_draft`, `*-list_labels`, `*-label_thread`/
  `*-label_message`) — the owner's real inbox — **and** the assistant's **Proton** inbox via Bridge IMAP
  (often empty until mail is routed there). Present one combined triage, tagged by source.
- **Replies always leave from Proton** as the assistant's own address (even for a Gmail-sourced thread).
  Gmail here is draft-only; a Gmail draft is a stopgap only if Bridge is down.

**Triage behavior (act-low / ask-high):**
- Summarize + categorize incoming mail (act-low).
- **Auto-archive obvious noise** (act-low; reversible — archive, never delete). *Graduated to act-low;
  see the graduation log in `autonomy-policy.md`.* Auto-archive only when **all** hold: the
  sender is an automated/no-reply system address, the message is informational (no action or reply
  expected from the owner), and the sender is **not** a person in Notion **People**. Noise categories:
  - Newsletters & marketing blasts (bulk; typically carry a `List-Unsubscribe` header).
  - Receipts & order confirmations for already-completed purchases.
  - Shipping / delivery status updates.
  - Calendar **system notifications** — invite acknowledgements, "event updated/canceled" auto-mails
    (**not** actual invitations awaiting a response, which stay ask-high via the calendar-steward).
  - Social / app notifications (LinkedIn, GitHub/CI bots, social-media digests).
  - **Expired or already-used** OTP / one-time security codes (past their validity window).

  **Never auto-archive:** anything from a human or known contact; anything asking for action or a reply;
  still-actionable security/identity/financial alerts (fraud warnings, a password reset the owner didn't
  request); or anything ambiguous. **When unsure, it's not noise — surface it.**
- **Draft replies as the assistant** from its own address, held for approval (ask-high to send).

## Slack — connected MCP

- Read: `*-slack_read_channel`, `*-slack_read_thread`, `*-slack_search_public(_and_private)`,
  `*-slack_read_user_profile`.
- Write: `*-slack_send_message` (send → **ask-high**), `*-slack_send_message_draft` (a Slack-side draft —
  kept only for an explicit *"leave it in my Slack drafts"* ask; the draft-and-hold flow does **not** use
  it, Q5), `*-slack_schedule_message` (scheduled sends are **out of scope for v1**, Q11).
- Triage: screen DMs/mentions, summarize busy channels, surface what needs a reply.

**Draft-and-hold (the reply-drafting flow).** When a DM / direct @-mention needs a reply, the assistant
**drafts** it (act-low) from the pinned **Slack SSOT** (`slack-ssot.md`) under the derivation contract,
and **holds** it for approval on the standard held-approvals loop (`memory.md` → *Held approvals*; schema
in `../state/README.md`). Each held draft is a **single signed body** — `send a7` (the assistant always
signs; Q4's unsigned/as-the-owner variant was deferred by the owner). On approve, the assistant runs a
**freshness re-check** (re-read the thread since `thread_seen_ts`; a moved thread re-surfaces instead of
sending) then posts **verbatim** via `slack_send_message`. A send failure is **never auto-retried**
(double-post risk) — it's kept in carry-over, surfaced, and re-attempted only on a fresh `send`. Full
behavior + the owner's 11 rulings: `../../subagents/slack-triage/SKILL.md` +
`../docs/slack-draft-and-hold-spec.md`.

**Daemon Slack hands.** The headless daemon's warm session may *understand* a Telegram `send a7` but lack
Slack tools to execute it — it then records `status: "approved"` and drains it on the next Slack-capable
turn (the *Slack-hands gap*). Wiring `scripts/slack-mcp.json` (auto-detected by `presence.py`;
`--no-slack` opts out — Q9) closes the gap so a Telegram `send` posts immediately:
`../scripts/SLACK_MCP_SETUP.md`.

## Telegram — the assistant's primary push + two-way chat (free, local)

The assistant's everyday, always-with-the-owner surface — reminders, nudges, approval prompts, and a real
back-and-forth chat, all over a free Telegram bot. **Same assistant as `/assistant` and the scheduled
runs** (one persona, one brain, one approval gate); the *to-the-owner* register applies. Setup:
`../scripts/TELEGRAM_SETUP.md`.

- **Outbound (act-low to the owner themselves):** `../scripts/telegram_send.py --text "…" --env-file
  ../scripts/telegram.env`. Pushing the owner their *own* content (reminders, brief highlights, "ready to
  send?" prompts) is inbound-style and act-low. Outbound to **third parties** is still ask-high and goes
  through the real channel (email/Slack), never Telegram.
- **Inbound (two-way chat):** `../scripts/telegram_poll.py --commit --env-file ../scripts/telegram.env`
  returns new messages and advances the offset. The **sentinel** polls each cycle; a new message wakes
  the brain in **Chat mode** (`SKILL.md`) to reply via `telegram_send.py`. A short rolling thread is
  cached in `../state/telegram-thread.json` so fresh sessions keep conversational continuity.
- **Creds** live in `../scripts/telegram.env` (git-ignored); offset in `../state/telegram-offset`.
  An allowlist (`TELEGRAM_ALLOWED_CHAT_IDS`) restricts who can drive the assistant.
- **Asleep machine:** Telegram retains updates ~24h, so messages are picked up on the next poll
  (same catch-up behavior as the rest of the local stack).

## Discord — a second two-way surface (free, local)

Same assistant as Telegram — one persona, one brain, one approval gate — in a private Discord channel.
For an owner who lives in Discord, this makes the assistant reachable there for the same reminders,
nudges, approval prompts, and a real back-and-forth chat. Setup: `../scripts/DISCORD_SETUP.md`.

- **Outbound (act-low to the owner themselves):** `../scripts/discord_send.py --text "…" --env-file
  ../scripts/discord.env`. Same rule as Telegram — pushing the owner their *own* content is inbound-style
  and act-low; outbound to **third parties** stays ask-high and goes through the real channel.
- **Inbound (two-way chat):** `../scripts/discord_poll.py --commit --env-file ../scripts/discord.env`.
  Discord has no long-poll/getUpdates; the stdlib fallback **REST-polls** `GET
  /channels/{id}/messages?after=<id>` each presence cycle, advancing a stored snowflake id (the primary
  path is the gateway websocket — see `../docs/asyncio-daemon-design.md`). The presence daemon listens
  alongside Telegram; a new message wakes the **same** warm Chat session, and the assistant replies on
  the channel it came from. First run **seeds "from now"** (no history replay); bot messages (incl. the
  assistant's own) are ignored, so there's no echo loop.
- **Creds** live in `../scripts/discord.env` (git-ignored); offset in `../state/discord-offset`. An
  allowlist (`DISCORD_ALLOWED_USER_IDS`) restricts who can drive the assistant. Requires the bot's
  **Message Content intent**.
- **Asleep machine:** Discord keeps channel history server-side, so messages sent while the machine is off
  are picked up on the next poll (bounded by the `--limit` per call) — same catch-up behavior as the rest
  of the local stack.

## Signal — deferred (designed, not built)

Wanted as a third two-way surface, but Signal has **no official API**: it needs an external `signal-cli`
daemon (Java) linked as a secondary device to a Signal number, running in JSON-RPC mode — a *second
always-on local process*, unlike the token-plus-HTTP of Telegram/Discord. The channel is designed to slot
in the same way (`signal_send.py` / `signal_poll.py`, `state/signal-offset`, a `signal` enqueue channel),
gated on that daemon.

## Reminder / nudge delivery priority

When the assistant needs to reach the owner proactively (a due reminder, a time-sensitive flag), deliver
in this order, using what's configured:

1. **Telegram** (`telegram_send.py`) — primary; free, instant, works from the owner's phone.
2. **Discord** (`discord_send.py`) — a peer free two-way surface when configured; use whichever the owner
   is on.
3. **Phone call — escalation, rare** (`push_call.py` → Worker `/push-call`) — for a `Call Me`-flagged
   reminder, the assistant *rings the owner's phone* and speaks the line. Metered + intrusive, so
   reserved for can't-miss items (`reminders-policy.md`).
4. **Proton email** (`proton_send.py`) — for non-urgent nudges, or a richer message; needs the machine +
   Bridge up at send time.

Non-urgent items can also simply **wait for the next brief** rather than pushing at all — prefer signal
over interruption.

**The ⏰ Reminders system delivers this way.** The Reminders mode (recurring habits / today's todos /
deadline watch, in the Notion ⏰ Reminders DB) does **not** send directly. Each slot run **enqueues**
nudges into `state/reminders.json` via `scripts/reminders_enqueue.py`, each tagged with a `channel`; the
resident **presence daemon** (`presence.py`) fires due entries — routing **telegram** / **call** /
**discord** (falling back to Telegram if a channel isn't configured) — and stamps `fired_at`; **Dream**
prunes fired entries. See `reminders-policy.md`.

## Phone / SMS — Twilio (the `phone/` Worker, in this repo)

The voice call-screener Worker lives in [`../../phone/`](../../phone/). Twilio creds stay in the
Cloudflare Worker; the assistant's local scripts hold only a Worker URL + bearer secret (so no Twilio
secret ever lands in this repo). Each capability is a small bearer-authed Worker endpoint.

- The voice call-screener runs on Twilio. `phone/src/notify/sms.ts` wraps the Twilio SMS API for
  outbound.
- **Phone call to the owner (can't-miss reminders).** The Worker exposes a bearer-authed
  `POST /push-call` (`phone/src/index.ts`) that originates a Twilio call speaking one line (inline TwiML
  `<Say>`, then hangs up). The assistant calls it via `../scripts/push_call.py --env-file
  ../scripts/push-call.env`; reminders flagged `Call Me` route here (`reminders-policy.md`). Deploy
  (`cd phone && wrangler deploy`) with `PUSH_CALL_SECRET` set and `push-call.env` filled in; the daemon
  runs with `--call-env`. If a call can't be placed, a `channel: call` reminder falls back to Telegram.
- **Call-until-answered (escalation).** `POST /push-call` with `{ "escalate": true }` (from
  `push_call.py --escalate`, optional `--interval-sec` / `--max-attempts`) starts a `CallEscalation`
  Durable Object that re-calls **every 2 min, up to 15 tries** until the owner **presses a digit** (Twilio
  hits `POST /push-call/ack`). The retry loop is Worker-side (a storage alarm), so it survives the daemon
  being off. Used for "call me until I answer" alarms (`reminders-policy.md` → *Call-until-answered*).
- **SMS push of the brief (act-low — to the owner's own cell).** Designed to work via a bearer-authed
  `POST /push-sms` (`../scripts/push_sms.py --env-file ../scripts/push-sms.env`). ⚠️ **Not necessarily in
  the deployed Worker** — land the endpoint into `phone/` and deploy to activate. Reminders don't use SMS
  anyway (they ride Telegram/Discord/call above); kept here for the brief-highlights use case once
  activated.
- **Inbound SMS triage — deferred** (intentionally out of scope for now). When built it extends the
  `phone/` screener with a `POST /sms` webhook; outbound to third parties is ask-high.

## Cross-channel rules

- The assistant always writes **as itself** (`../../persona/persona.md (else persona.default.md)`), never as the owner.
- Everything outbound is **ask-high** by default (`autonomy-policy.md`).
- Resolve senders against Notion **People** (`databases.md`) so triage knows who's who.
