# Architecture

## The funnel

Every inbound call runs a cheapest-first cascade in the Worker (`src/index.ts` →
`src/screener/funnel.ts`). Each stage is cheaper than the next, so most spam dies at ~$0.

| Stage | Check | Action | Cost |
|------|-------|--------|------|
| 1 | Allowlisted contact (D1) | `<Dial>` to the user's cell | free |
| 2 | Blocklisted number (D1) | `<Reject>` — declined before answer | **$0** |
| 3 | Unknown | `<Gather>` "press 1" gate | ~$0.01–0.02 |
| 4 | Pressed 1 | `<Connect><ConversationRelay>` → Claude | ~$0.10 |

Stage 4 decides via one tool call: `connect_call` (bridge to the user), `take_message` (store + SMS), or
`mark_spam` (hang up + add to blocklist). Spam flagged here or a repeated gate-fail feeds the **learning
blocklist**, so the number is rejected for $0 next time.

Free heuristics (`src/filter/heuristics.ts`) — anonymous caller, neighbor-spoofing (same area code +
prefix as the user) — don't hard-reject (too many false positives); they route to the gate and add signal
to the log and to Claude.

## Components

- **`src/index.ts`** — Worker HTTP router: `POST /voice` (funnel → TwiML), `POST /gate` (the pressed
  digit), `GET /ws` (ConversationRelay WebSocket → Durable Object), `GET /status`.
- **`src/twiml.ts`** — pure TwiML string builders.
- **`src/screener/`** — `funnel.ts` (routing), `brain.ts` (stage-4 decision behind an `LlmClient`
  interface), `prompt.ts` (system prompt + tool schemas), `decision.ts` (shared types).
- **`src/relay/`** — `protocol.ts` (ConversationRelay JSON frames), `session.ts` (the `RelaySession`
  Durable Object holding the WebSocket + conversation state).
- **`src/data/`** — `db.ts` (D1 access) + `schema.sql`.
- **`src/notify/sms.ts`** — Twilio SMS to the owner.
- **`src/budget.ts`** — per-call cost estimation + the daily budget guard.

## Data model (D1)

`contacts` (allowlist) · `blocklist` (learning) · `calls` (one row per call, incl. `outcome_stage`,
`verdict`, `transcript`, `cost_estimate_usd`) · `settings` (single-row operator overrides). Canonical
truth is the stored call records; D1 is a rebuildable index. See `src/data/schema.sql`.

## Cost model

Twilio bills only **answered** calls. `<Reject>` is free; the gate is a few seconds of answered call;
ConversationRelay is $0.07/min and only runs at stage 4. Cost knobs:
- `POST_GATE_ACTION = ring_through` skips the Claude conversation entirely (gate-only screening).
- `REPUTATION_LOOKUP_ENABLED` (paid Twilio Lookup) is off by default.
- `DAILY_BUDGET_USD` caps daily spend; over the cap, stage 4 downgrades to voicemail.

## Why not the raw Twilio Media Streams path?

ConversationRelay hands us **text** (transcripts in, text out), not raw audio — so Cloudflare Workers (which
can't decode audio in-runtime) are a clean fit, and Twilio handles STT/TTS/barge-in for us. The raw
Media-Streams path (bring-your-own STT/TTS) is more control but far more to build; revisit only if needed.
