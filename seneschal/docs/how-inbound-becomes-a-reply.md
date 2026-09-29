# How an inbound message becomes a reply

**Status:** `REFERENCE` — a how-it-works page. Where a step depends on daemon wiring that has not
landed in this tree yet, the step says so.

## The path, in order

1. **A message arrives.** Telegram (`telegram_poll.py`'s long-poll) or Discord (the gateway
   websocket) hands `presence.py` raw text, an attachment, or a callback tap. Both channels feed
   the **same** action queue rather than two separate ones; the cockpit is a third door into it.
2. **It's queued, not answered inline.** The scheduler tick (~5 s) and the channel tasks are
   separate `asyncio` coroutines — receiving and replying are decoupled, which is what lets a
   reminder fire mid-turn without waiting on Telegram's poll interval.
3. **The drainer task picks it up.** It pops the queue and either sends the text into the
   **already-open warm session** (the common case — a long-lived `claude` CLI process in
   stream-json mode) or cold-spawns one if none is warm.
4. **Grounding is assembled — cold spawn only.** A resumed/warm turn skips to step 5; a cold spawn
   gets the full `GROUNDING` prompt (`presence.py`): who the assistant is (from the persona and
   `persona/identity.json`), the authoritative owner-local clock, the act-low/ask-high gate, the
   reminder-ack contract, and the standing safety block (`scripts/standing_safety.py`), injected at
   zero read cost.
5. **The turn runs.** The model reads persona/mode files, calls tools (the store, Calendar, the
   reminder scripts), and composes a reply — the **Advisor Chain**
   (`seneschal/references/advisor-chain.md`) is the ordering discipline: Trace → Prioritization →
   Orientation/Memory → Retrieval → Dispatch → Critique → SafeGuard/Gate.
6. **The reply's opening lines are checked (Telegram).** A chat-turn reply must open with a channel
   declaration and an answering line; `channel_declare.py` parses, strips and bounded-retries them
   (`message-routing-spec.md`, `reply-marker-forcing-function-spec.md`). The module ships; the
   drainer call site that runs it is part of the daemon wiring still to land.
7. **The reply is delivered.** `deliver_reply` sends through `telegram_send.py` (topic-aware) or the
   Discord equivalent, with a retry/fallback ladder that never double-sends on an ambiguous failure.
8. **The record is kept.** `turns.py` appends the turn to `state/turns.jsonl` (verbatim, both sides,
   uncapped), and anything the assistant actually *told* the owner lands in `state/assertions.jsonl`
   via `mouth.py` — a separate record, because the Mouth answers "what has the owner actually been
   told" (`mouth-spec.md`). Both modules ship; recording at every send site arrives with the daemon
   wiring.

## The `/assistant` CLI path is a sibling, not a copy

A desktop `claude` session running the `/assistant` command reads the same
persona and mode files but does **not** go through `presence.py`'s queue — it is its own session.
Both converge on the same persona, modes, and approval gate; only the transport differs.

## Where this can go wrong

- **An unnamed Telegram `allowed_updates` type is never delivered** — a whole category (edits,
  reactions) can go silently missing with no symptom either side.
- **An album (multiple photos) arrives as N updates**, not one — the daemon holds and coalesces
  them so nine screenshots read as one turn.
- **A cold spawn pays the full grounding cost; a resumed one gets a short resume preamble** — the
  clock restated, never a second copy of the full prompt.
- **An empty model reply is not silence.** It must be recognised and treated as "nothing to send"
  before any delivery or opening-line machinery runs (`reply-marker-forcing-function-spec.md` §9,
  `substance-or-silence-spec.md` §3).

Full mechanics: `seneschal/docs/asyncio-daemon-design.md` → "The loop as deployed";
`seneschal/docs/telegram-inbound-spec.md`.
