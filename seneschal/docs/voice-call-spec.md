# Voice MVP — the assistant calls the owner and they talk

**Status:** `PARTIAL(P1 BUILT — outbound talk mode: Worker + owner-mode RelaySession +
push_call.py --talk + the chat trigger; P2 inbound owner-call auth decided (caller ID + spoken
passphrase), unbuilt; P3 hybrid hand-off to the home daemon unbuilt; P4 other surfaces unbuilt)`.

**Decisions:**
- The objective is a live voice conversation with the assistant. First surface = Twilio phone
  calls; widen later (P4).
- The MVP is **outbound** — the assistant calls the owner and they talk. Inbound (the owner calling
  the assistant's number for a live conversation, as opposed to the existing screener path) is P2.
- The brain is the **Worker, in owner mode** — the existing Cloudflare Worker + Twilio
  ConversationRelay path, NOT a relay to the home daemon's `claude-cli` session. A hybrid (the Worker
  talks live, hands hard asks to the home daemon async) is the named next phase (P3).
- Owner auth for **inbound** talk calls (P2) is **caller ID + a spoken passphrase** — the owner's
  number gets the owner path, and the assistant asks for the passphrase (`OWNER_PASSWORD`) before
  anything private. Chosen over *caller ID only* (spoofable) and *passphrase only for private topics*.
  Outbound needs none: the call only ever goes to the number the Worker itself dialled
  (`env.USER_CELL_E164`), never anywhere the caller/requester names.

Read with `../../phone/CLAUDE.md` (the Worker's architecture) and `../scripts/push_call.py`'s module
docstring (the local trigger).

## P1 — Outbound talk mode (BUILT)

**What it is.** `POST /push-call` on the call-screener Worker gains a second body shape:
`{"mode": "talk", "context"?: "..."}`. Unlike the scripted-line shape (`{"text": "..."}`), talk mode
dials `env.USER_CELL_E164` and connects the answered call to Twilio ConversationRelay, handing it to
the SAME `RelaySession` Durable Object class the inbound screener uses — but seeded into an **owner
mode** that speaks in the assistant's *to-the-owner* register (`Persona.ownerDemeanor` in
`phone/src/persona.ts`, generated from the persona beside the screener's outsider-facing `demeanor`)
rather than the gatekeeper register, and that has no screening tools at all — just `end_call`, called
when the conversation naturally wraps up.

**Why the same Durable Object class, not a new one.** `RelaySession` already holds the exact
machinery a live phone conversation needs — the ConversationRelay WebSocket protocol
(`phone/src/relay/protocol.ts`), the Claude turn loop (`phone/src/screener/brain.ts`), the Twilio
call-control REST calls (`phone/src/twilio/calls.ts`) — and none of it is screener-specific by
construction. Adding a `mode` field and branching the system prompt / tool set / turn engine on it is
additive; screener calls are unchanged (mode defaults to `"screener"`, and only the talk-mode
`/push-call` handler ever seeds `"owner"`).

**How dialing-only-the-owner is enforced.** `notify/call.ts::placeTalkCall` takes NO `to` parameter —
there is no argument shape through which a caller of this function could name a different recipient,
and the talk-mode handler never reads the body's `to` at all (its dispatch branches before that read).
Type-level enforcement, not a runtime check that could be argued around.

**How the seed reaches the Durable Object before the call connects.** A `sessionId` is generated once
in the handler, then used twice: first to POST `{mode: "owner", context}` directly to the
`RelaySession` instance (`env.RELAY_SESSION.idFromName(sessionId)`), then as the `s=` query param on
the `wss://.../ws` URL `placeTalkCall`'s TwiML hands Twilio. Both resolve to the SAME Durable Object
instance, so by the time Twilio's ConversationRelay handshake arrives, the instance already has the
seed. This sidesteps inline TwiML's ~4,000-character limit — the context snapshot (up to 6 KB) never
has to fit inside a `<Parameter>` tag; it rides an ordinary Durable Object fetch instead.

**Surviving eviction.** The phone rings for several seconds before the ConversationRelay handshake
arrives, and an idle Durable Object can be evicted in that gap — a fresh instance whose fields start
at the `"screener"` default would run the screener persona on the owner's own call. So:
1. `seed()` persists `{mode: "owner", ownerContext}` to `state.storage`, and the constructor reloads
   it via `state.blockConcurrencyWhile` before any `fetch()` (seed POST or WS upgrade) is handled.
2. `placeTalkCall`'s TwiML also carries a `mode=owner` `<Parameter>`, read off the ConversationRelay
   `setup` frame. `resolveSessionMode(storageMode, paramMode)` requires **both** signals to agree
   before entering owner mode — either alone falls back to screener, logged loudly.
3. `phone/test/session.test.ts` covers the eviction case (a second `RelaySession` over the same storage
   still resolves owner mode + context) and the disagreement cases; `phone/test/owner-prompt.test.ts`
   asserts the owner prompt never asks for a password.

**Turn engine.** `relay/owner-conversation.ts::runOwnerTurn` is the owner-mode sibling of
`screener/conversation.ts::runCallerTurn`: no triage (there's no one to screen), just
reply-until-`end_call`, with a turn cap (`MAX_OWNER_TURNS`, 40 — much higher than a screening call,
since a real conversation runs long) as a backstop so a stuck model can't run up ConversationRelay
minutes on the owner's own line.

**Model.** Both conversations share `phone/src/relay/session.ts`'s `MODEL` constant (a Haiku model) — fast and
cheap for real-time turns. **Trade-off noted, not exercised**: a larger model would likely read warmer
in a real conversation with the owner, at a higher per-minute cost; the swap (or a `mode`-dependent
choice) is left for measurement against real calls.

**Budget.** Talk-mode calls draw from the SAME `DAILY_BUDGET_USD` cap the screener reports
(`budget.ts::overBudget`). `data/db.ts::sumSpendToday` sums `cost_estimate_usd` across today's `calls`
rows; the handler checks it before dialing and answers **`402`** over budget (`push_call.py` reports
that as `"budget_exhausted": true`, exit 1). `RelaySession::endOwnerCall` records the talk call's own
estimated cost back into the same `calls` table (`outcome_stage`/`verdict`: `"talk"`) once it hangs
up, so talk calls count against later budget checks the same way inbound screening does.

**No screener actions in owner mode.** `endOwnerCall` speaks a short goodbye and hangs up — no
transfer, no blocklist add, no escalation retry, no voicemail. The caller IS the owner; there is
nothing to route them to and nobody to screen.

**Local trigger — `push_call.py --talk`.** Builds a compact, best-effort context snapshot — today's
calendar (the owner-local calendar day, via `gcal_api.py` if `google.env` is configured and an account
label resolves: `PUSH_CALL_CALENDAR_ACCOUNT`, else the only connected account), today's still-pending
`state/reminders.json` entries, and the head of `state/carry-over.md` — capped at `CONTEXT_MAX_BYTES`
(6,000 bytes, truncated with a trailing marker past that), and POSTs `{"mode": "talk", "context":
"..."}`. Each source degrades to `""` independently on any failure — no `google.env`, no network, a
stale calendar token, several accounts and none named, a missing queue file — so a call is never
blocked on a context source that isn't reachable. All three sources are local files or the Google
bridge, the same on every store backend. `--talk` is mutually exclusive with
`--text`/`--body-file`/`--to`/`--escalate`. The snapshot is private by construction: it only ever
rides the wire to the owner's own phone, via a request gated behind `PUSH_CALL_SECRET` and (like every
other `push_call.py` send) the send gate's owner-class fast path.

**Trigger doc.** `../modes/chat.md` documents: when the owner says "call me" / "let's talk on the
phone" (or similar), the assistant runs `python scripts/push_call.py --talk` — act-low, since the call
only ever reaches the owner's own phone.

## P2 — Inbound owner-call auth (decided, unbuilt)

The owner calling the assistant's own Twilio number for a live conversation (as opposed to being
screened) needs an owner-recognition step BEFORE handing the call to owner mode — today's `/voice`
webhook has no caller-ID fast path that isn't the allowlist/blocklist funnel, and that funnel exists
to protect the owner FROM callers, not to authenticate them as themselves. **Decided: caller ID match
against `USER_CELL_E164` PLUS a spoken passphrase against `OWNER_PASSWORD`**, because caller ID alone
can be spoofed. **Still open:** what a wrong or missing passphrase does (screen as an unknown caller?
reject?). That is asked when P2 is built, never defaulted.

## P3 — Hybrid hand-off to the home daemon (unbuilt)

The Worker's owner-mode conversation runs entirely inside the Cloudflare Worker/Durable Object — it
has no access to the home daemon's warm `claude-cli` session, the store, or anything else that
requires being on the owner's network. The named next phase: the Worker keeps driving the live
conversation (latency matters on a phone call), but hands a hard ask off to the home daemon
asynchronously and speaks the answer back once it lands — rather than trying to make the Worker itself
reach into home-network-only capabilities mid-call.

## P4 — Other surfaces (unbuilt)

Phone calls first, widening later — no other surface (e.g. a voice mode inside the desktop/chat
client) is in scope for this spec yet.
