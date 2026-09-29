# phone/ — the voice call-screener

A tiered, **cost-optimized** AI call screener — the [seneschal](../README.md) assistant's phone front
door. It fronts your phone number, blocks obvious spam for free, and only spends money letting Claude
talk to the callers that prove they're worth it. This directory is a self-contained Cloudflare Workers
project; deploy it to your own Cloudflare account with `cd phone && wrangler deploy`.

Built for an **Android + Google Voice** setup, but the core is carrier-agnostic.

## Why tiered?

Answering and conversing with every robocaller is expensive. On Twilio you're only billed once a call is
**answered**, so the whole design maximizes free rejects and minimizes answered minutes:

```
 Inbound call
   1. Allowlisted contact?  -> Dial straight through         (free)
   2. Blocklisted number?   -> Reject (declined, not answered)(free, $0)
   3. Press-1 gate          -> robodialers can't press 1      (~$0.01–0.02)
        pressed 1 (human) -> 4. Claude screens the caller     (~$0.10)
                               -> connect / take message / mark spam
```

A **connect** (and an allowlisted dial) rings the owner's cell up to **three times** (12 s each) before
rolling to voicemail; the voicemail's transcript — and, over Telegram, the recording itself — then goes to
the owner. See [Owner notifications](#owner-notifications) and [Live transfer](#live-transfer-three-rings-then-a-message).

Spam that gets flagged is remembered, so repeat offenders are rejected for $0 forever after. Target cost
at ~8 calls/day: **~$5–10/mo**.

## Stack

- **Twilio ConversationRelay** ($0.07/min) — speech-to-text, text-to-speech, interruption handling.
- **Claude** (Haiku 4.5) — the screening brain, over a WebSocket (bring-your-own-LLM).
- **Cloudflare Workers + Durable Objects + D1** — always-on host; D1 holds allowlist / blocklist / call log.

## Quickstart (local)

```bash
npm ci               # see the package-lock caveat in docs/runbook.md
npm run typecheck
npm test
cp .dev.vars.example .dev.vars   # then fill in real values
npm run dev          # wrangler dev; expose with a tunnel for Twilio
```

## What's here

The full pipeline is implemented and unit-tested offline: the cost-saving funnel (allowlist /
blocklist / press-1 gate), the stage-4 Claude conversation (as a configurable persona — nameless by
default; see `src/persona.ts` + the `ASSISTANT_*` vars), owner notifications (Telegram, or SMS as the
fallback — see [Owner notifications](#owner-notifications)), outbound reminder calls
(`POST /push-call`, single-ring or escalating, spoken in the persona's voice when one is configured),
voicemail fallback, and a Google Contacts allowlist sync. The **learning blocklist** flags spam once and rejects it for $0 ever after — a Claude spam
verdict blocklists immediately, a **silent** press-1 timeout blocklists on the first strike (humans
mash keys; robots say nothing), and a wrong-key press gets two strikes of grace. Blocked numbers also
sync to the on-device blocker app ([android/](android/) — "Seneschal Call Shield"). See
[docs/architecture.md](docs/architecture.md) and [docs/setup.md](docs/setup.md).

## Owner notifications

Every note to the owner — the verdict after a screened call (message / spam), the real-time
*"Connecting X — pick up!"* alert, and a voicemail transcript — goes through one door,
`src/notify/owner.ts`, which picks the channel:

- **Telegram, when configured.** The Worker talks to the Bot API directly (it cannot reach the
  assistant's local daemon), ideally with the same bot and chat the daemon uses
  (`seneschal/scripts/TELEGRAM_SETUP.md`). Plain text; a voicemail arrives as **two messages** — the
  transcript first, then the recording as an audio file (the Worker fetches Twilio's auth-protected
  `RecordingUrl` as `.mp3` and uploads it via multipart `sendAudio`). If the audio leg fails, the text
  has already gone.
- **SMS (Twilio, to `USER_CELL_E164`) only as the fallback** — when Telegram is not configured, or as a
  last resort when a configured Telegram send fails. US long-code SMS additionally needs Twilio A2P
  10DLC registration, which is why Telegram is the primary channel.

`wrangler tail` logs which channel carried each send (`notifyOwner: telegram` / `sms`).

**Setup** (these are secrets — never in the repo):

```bash
wrangler secret put TELEGRAM_BOT_TOKEN    # the bot token from @BotFather (same as the daemon's telegram.env)
wrangler secret put TELEGRAM_CHAT_ID      # your chat id with the bot (same as the daemon's telegram.env)
wrangler secret put TELEGRAM_THREAD_ID    # optional: a private-chat topic id; omit for the main chat
```

Both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` must be set for Telegram to count as configured;
either one alone leaves the Worker on SMS. For `wrangler dev`, put the same keys in `.dev.vars`
(`.dev.vars.example` lists them). A merged Worker change is not live until someone runs
`wrangler deploy`.

### Live transfer: three rings, then a message

When the screener connects a caller (or an allowlisted contact dials straight through), the `<Dial>` to
the owner's cell rings for `LIVE_TRANSFER_RING_SEC` (**12 s**) and reports to `/after-bridge?attempt=N`.
Unanswered ⇒ dial again, up to `LIVE_TRANSFER_ATTEMPTS` (**3**) in total; only then does the caller hear
the voicemail prompt. Both constants live in `src/twilio/transfer.ts`. Keep the ring time **below your
cell's no-answer-forward timer** (often 15 s when shortened) — otherwise a missed bridge forwards back to
the Twilio number and the caller is screened all over again. This is **not** the reminder escalation's
cap (`CallEscalation`, default 15 tries): a caller shouldn't be held for minutes, but the assistant can
keep calling the owner about a reminder for half an hour.

## Layout

- `src/screener/funnel.ts` — the tiered routing decision (pure, tested).
- `src/persona.ts` — the shipped default persona (env-overridable name/voice).
- `src/twiml.ts` — Dial / Reject / Gather / ConversationRelay builders.
- `src/index.ts` — Worker router (`/voice`, `/gate`, `/ws`, `/status`, …).
- `src/relay/` — ConversationRelay protocol + the `RelaySession` Durable Object.
- `src/escalation/` — the `CallEscalation` Durable Object (call-until-answered, storage alarm).
- `src/notify/` — owner notifications: `owner.ts` (the Telegram-else-SMS switch), `telegram.ts`,
  `sms.ts`, `format.ts`; `call.ts` places outbound reminder calls.
- `src/twilio/` — `calls.ts` (re-point a live call via REST), `transfer.ts` (the three-attempt live
  transfer + its constants).
- `src/data/` — D1 access + `schema.sql`.
- `android/` — the on-device blocker + presence/health companion app (committed Gradle project).
- `test/` — vitest unit tests for the pure logic.
