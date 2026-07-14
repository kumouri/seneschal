# CLAUDE.md — phone/ (the voice call-screener)

Tiered, cost-optimized AI call screener — the Seneschal assistant's phone front door. Fronts the
owner's number, blocks obvious spam for free, and only spends on a Claude conversation for callers
worth it. A plain directory of the seneschal repo; deploys as its own Cloudflare Worker
(`seneschal-screener`).

## Stack
- **Twilio ConversationRelay** (STT/TTS/turn-taking) + **Claude** (Haiku 4.5) over a WebSocket.
- **Cloudflare Workers + Durable Objects + D1** (always-on host; D1 = allowlist/blocklist/calls/settings).

## Architecture (the funnel)
Inbound call → Worker `POST /voice` runs the cheapest-first cascade (`src/screener/funnel.ts`):
1. **allowlisted** (D1 `contacts`) → `<Dial>` to the owner's cell (no screening).
2. **blocklisted** → `<Reject>` (free — Twilio only bills *answered* calls).
3. else → **press-1 gate** (`POST /gate`). Robodialers fail it cheaply; each failure (wrong key or
   silent timeout — the `<Gather>` uses `actionOnEmptyResult`) is recorded (`recordGateFail`, never
   counting allowlisted contacts). A **silent timeout blocklists on the first strike** (humans mash
   keys; robots say nothing); a **wrong key** gets two strikes of grace.
4. gate-pass → `<Connect><ConversationRelay>` → the `RelaySession` Durable Object runs the Claude
   conversation as the assistant persona, then transfers / takes a message / marks spam (via Twilio
   REST), logs to D1, and SMSes the owner.

**Critical:** the DO uses the **non-hibernating** WebSocket API (`server.accept()`), not
`state.acceptWebSocket()` — hibernation resets `callSid`/`history` every turn and breaks everything. Don't
"optimize" it back to hibernation.

## Key files
- `src/index.ts` — Worker router: `/voice`, `/gate`, `/ws`, `/status`, `/sync-contacts`, `/blocklist`,
  `/push-call` (+ `/push-call/ack`), `/after-bridge`, `/voicemail`.
- `src/screener/` — `funnel.ts` (routing), `conversation.ts` (turn engine + cap), `brain.ts` +
  `anthropic-client.ts` (LLM), `prompt.ts` (system prompt), `decision.ts` (types).
- `src/persona.ts` — **`DEFAULT_PERSONA`**, the shipped screener persona: nameless, warm-but-unfoolable,
  platform default voice. The env overrides it (`ASSISTANT_NAME` / `ASSISTANT_VOICE_ID` /
  `ASSISTANT_TTS_PROVIDER` → `personaFromEnv` in `src/config.ts`); the canonical persona lives in
  `persona/persona.md` at the repo root, and the setup wizard emits these env values.
- `src/relay/session.ts` — the `RelaySession` Durable Object. `src/twilio/calls.ts` — live-call transfer.
- `src/notify/call.ts` — outbound reminder calls: `placeCall` (single ring) + `placeEscalationCall`.
- `src/escalation/escalation.ts` — the **`CallEscalation`** Durable Object: `POST /push-call {escalate:true}`
  re-calls (default every 2 min, ≤15 tries) via a **storage alarm** until the owner presses a digit
  (`POST /push-call/ack`) or the cap is hit. Pure `nextEscalationStep` for the retry decision.
- `src/data/db.ts` + `schema.sql` — D1 access incl. `syncGoogleContacts`/`reconcileContacts`.
- `scripts/google-contacts-sync.gs` — Apps Script for the contacts sync (see `docs/contacts-sync.md`).

## Config
- **`[vars]`** (wrangler.toml): `OWNER_NAME` (+ `OWNER_NAME_SPOKEN` for TTS pronunciation),
  optional `ASSISTANT_NAME` / `ASSISTANT_TTS_PROVIDER` / `ASSISTANT_VOICE_ID` (nameless + platform
  default voice when unset), `GATE_PROMPT`, `POST_GATE_ACTION`, `REPUTATION_LOOKUP_ENABLED`,
  `DAILY_BUDGET_USD`.
- **Secrets** (`wrangler secret put`): `ANTHROPIC_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
  `TWILIO_NUMBER_E164`, `USER_CELL_E164`, `OWNER_PROFILE`, `CONTACTS_SYNC_SECRET`, `OWNER_PASSWORD` (optional
  easter-egg). Local copies live in `.dev.vars` (gitignored; see `.dev.vars.example`).

## Workflow
- **Test:** `npm run typecheck` && `npm test` (vitest; pure logic only — no Workers runtime needed).
- **Deploy:** `cd phone && wrangler deploy` to **your** Cloudflare account (wrangler reads
  `CLOUDFLARE_API_TOKEN`, or `wrangler login`).
- **Logs:** `wrangler tail` (the DO logs setup/prompt/reply/decision/errors). Sessions time out after a
  while; call records also persist to D1 (`SELECT … FROM calls`).
- **Git Flow:** `feature/*` → PR → **merge commit** to `develop` on **green CI only** (never red/pending).
  CI = `npm ci` + typecheck + test on develop & main.

## Gotchas
- Use **`npm ci`**, not `npm install`, to install (CI uses `npm ci`); see `docs/runbook.md` if the
  lockfile picks up junk.
- Picking a TTS voice: set `ASSISTANT_TTS_PROVIDER` + `ASSISTANT_VOICE_ID` (e.g. an ElevenLabs voice id);
  preview candidates in Twilio's ConversationRelay voice picker. Unset = the platform default voice.
