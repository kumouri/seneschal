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
default; see `src/persona.ts` + the `ASSISTANT_*` vars), SMS notifications, outbound reminder calls
(`POST /push-call`, single-ring or escalating), voicemail fallback, and a Google Contacts allowlist
sync. The **learning blocklist** flags spam once and rejects it for $0 ever after — a Claude spam
verdict blocklists immediately, a **silent** press-1 timeout blocklists on the first strike (humans
mash keys; robots say nothing), and a wrong-key press gets two strikes of grace. Blocked numbers also
sync to the on-device blocker app ([android/](android/) — "Seneschal Call Shield"). See
[docs/architecture.md](docs/architecture.md) and [docs/setup.md](docs/setup.md).

## Layout

- `src/screener/funnel.ts` — the tiered routing decision (pure, tested).
- `src/persona.ts` — the shipped default persona (env-overridable name/voice).
- `src/twiml.ts` — Dial / Reject / Gather / ConversationRelay builders.
- `src/index.ts` — Worker router (`/voice`, `/gate`, `/ws`, `/status`, …).
- `src/relay/` — ConversationRelay protocol + the Durable Object session.
- `src/data/` — D1 access + `schema.sql`.
- `android/` — the on-device blocker + presence/health companion app (committed Gradle project).
- `test/` — vitest unit tests for the pure logic.
