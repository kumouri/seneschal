# Mid-turn interleave — the message that arrives while the previous one is still being answered

**Status:** `BUILT` —
`seneschal/scripts/interleave.py` carries Layer A's carve-out, `gate()`, the append-only
`state/interleave-log.jsonl` writers and readers (`stats`, `diagnose`), the `--interleave-mode`
enum and its per-host refusal (`refuse_mode`), and phase 2's pure helpers (the continuation prompt, the
possibly-landed clause, the MCP-possibly-landed row). Layer B is `router.classify_steer`
(`seneschal/scripts/router.py`). The daemon half is wired in `presence.py` — the turn window
(`interleave_open_turn` / `interleave_close_turn`), the intake snapshot and off-path gate run
(`interleave_snapshot` / `interleave_observe`), the stream-tee taps (`interleave_note_tool` /
`interleave_track_tool_call`), phase 1's arrival marker, phase 2's loop (`interleave_live_send`) and pop
discipline (`pop_folded_prefix`), the `--interleave-mode` flag, and `WarmSession.send_interrupt` /
`interrupt_requested` in `seneschal/scripts/backends/claude_cli.py`. The daemon's default is `observe`; `live` ships
**off** and is refused on an unprobed CLI version.

**Sits on, and must not contradict:** [`asyncio-daemon-design.md`](asyncio-daemon-design.md) (the
reactive core; this spec's whole subject is its serialization property),
[`session-trace-spec.md`](session-trace-spec.md) + [`cockpit-spec.md`](cockpit-spec.md) (`turn_id` is
the only correlator the transcript protocol has),
[`notion-write-behind-outbox-spec.md`](notion-write-behind-outbox-spec.md) (the nearest existing
discipline for a write whose landing is uncertain), and `seneschal/references/advisor-chain.md` (the
Router advisor, whose shadow-first rollout this copies deliberately). The per-turn hung-turn deadline
is a **gap between stream events, never a cap**, and nothing here changes that.

---

## 0. The ask, and the premise

The owner wants to be able to add to a request while the assistant is still answering it — *"oh, and
also X"* — and have the assistant take it into account without waiting for the whole current turn to
finish. The refined form of that ask is the design: **decide whether the new message is related to the
work in flight; if it is, steer the turn with it; otherwise hold it until the turn is done.** That is a
**relevance gate**. Unconditional interleave is §7's rejected alternative.

**The motivating case is additive context, not the too-late "wait, cancel that."** An earlier framing
of this feature (from the daemon's own deferral note) was built around an emergency cancel. That
framing was the implementer's invention, not the requirement, and it matters because it inverts the
error asymmetry (§4.4): a wrongly-held *cancel* lets a destructive action complete, while a
wrongly-held *context* message costs a short wait and the next turn still gets it.

---

## 1. What is already true

**Half of the ask is built by the asyncio core.** `telegram_task` reads the wire continuously and
`_enqueue_inbound` persists a message the moment it comes off it, whether or not a turn is running.
What remains is the other half: **can a message be inserted into a turn already in flight, at a
boundary where doing so improves the answer rather than corrupting it — and what breaks if we try.**

## 2. How often it happens, and where in the turn

Measured on a working install over about a week of real use (numbers are indicative, not
normative — re-measure on your own logs with `interleave.py stats` once observe is wired):

- A **meaningful minority** of inbound messages arrive mid-turn, and most of those are
  **machine-synthesized** (job-completion notices, reaction acks). A few typed messages a day arrive
  while the assistant is mid-answer, with a median wait of well under a minute and a long tail of
  several minutes.
- **Most typed mid-turn messages are steers** (a correction of a detail, an addition to the same
  request, a clarification, a go-ahead). The holds are exactly the class that would derail a turn: a
  status update, a meal log, an unrelated question.
- **The too-late cancel essentially never occurs.** A deliberately wide cancel/correction net over the
  verbatim record found no mid-turn "stop what you are doing" at all.
- **Arrivals land mid-turn, not at the start** — the median arrival is past the turn's midpoint, and
  almost none land in the first few seconds. **There is no arrival-time substitute for the
  classifier.** Whether a fold is worth less the later it lands is plausible but unmeasured, which is
  why `arrived_offset_frac` is in the schema (§4.3).

**A confounder to remove before re-running this.** The Router's shadow classifier stamps its row
*after* `router.classify()` returns, while the drainer starts that same message's turn within
milliseconds — so every message's own row lands inside its own turn. Drop any row whose text matches
the `text_preview` on the `turn_started` that opened the window it landed in.

---

## 3. What the `claude` CLI actually supports — established by probe

The warm session is `claude -p --input-format stream-json --output-format stream-json --verbose`.
Everything below was run at CLI **v2.1.226** and re-run at **v2.1.280**; it is undocumented CLI
behaviour and **version-pinned** (`interleave.PROBED_CLI_VERSIONS`).

### 3.1 There is no in-turn injection point

A second `{"type":"user"}` line written mid-turn is *accepted* but does not reach the model until the
turn boundary: the turn runs to completion, and only then does the CLI consume the second message and
answer it as its own turn. **"Insert the message into the stream" mid-turn is not something the CLI
offers.**

### 3.2 There is a supported interrupt, and it works — including inside a tool call

```json
{"type":"control_request","request_id":"probe-1","request":{"subtype":"interrupt"}}
```

`control_response` comes back in well under a second, followed by `[Request interrupted by user]` and a
terminal `result` with `is_error: true, result: ""`. Fired inside a `Bash` tool call it lands too
(`[Request interrupted by user for tool use]` about a second later). **The process survives** — the next
user line runs a normal turn on the same `session_id`.

### 3.3 The interrupted turn's work stays in context

After an interrupt, the next turn can say what it had been doing (sometimes verbatim, sometimes as a
paraphrase). **A cancel does not lose the reasoning; it loses the undelivered prose.** The cost is the
output tokens already produced and a slightly longer cache-read context — not the work.

### 3.4 Write-then-interrupt is a working steer

Write the new user message mid-turn, then interrupt: the interrupted turn ends, and the buffered
message runs as its own turn **with the partial output in context**, and answers correctly —
interrupt to steered answer in a few seconds. **The seam is a turn boundary created on purpose, not a
splice inside a turn.** Any design in this space is a design about creating that boundary well.

### 3.5 The trap: a buffered message runs itself

A message written mid-turn is consumed with no further write. A design that writes mid-turn and then
returns to the normal loop leaves a turn the daemon never asked for running; its next `_send_turn`
would read the *previous* message's answer — one turn out of step, permanently. **Every mid-turn write
must be paired with an owner for the extra `result` it produces**, which is why §5 sends the
continuation explicitly.

### 3.6 Unhandled, an interrupt reads as a dead session

`WarmSession` treats any `is_error` result as a failed turn; the drainer then ends the session and
apologises, and on a session's **first** turn `send()`'s fallback ladder reads the same shape as "the
resume didn't take" / "the model won't spawn" and respawns. `timed_out` already suppresses exactly this
for the watchdog's kill; **a deliberate interrupt needs the same flag** (`interrupt_requested`).

### 3.7 What was re-established at v2.1.280

- **An interrupted MCP write CAN still commit server-side.** The CLI's interrupt reaches the MCP layer
  fast, as a real MCP cancellation notification, but whether anything stops is up to the SERVER; one that
  has already committed (or already sent an outbound request it cannot recall) finishes regardless.
  The interrupted turn's own `result` carries **no signal** either way — from the client side,
  "cancelled cleanly" and "committed anyway" are indistinguishable.
- **Several buffered messages during a tool call fold into ONE turn**; a buffered message during plain
  text generation gets its own turn. A real seam, not version drift.
- **An interrupt before the first token** still lands and reads `is_error: true` — indistinguishable
  from a failed spawn — while the process is fine. This makes §3.6's warning concrete.
- **Not measured:** whether the continuation's re-read of a large warm context measurably raises cost.

**Consequence for phase 2: the MCP question is cleared on one condition.** Any MCP tool call
interrupted mid-flight is treated as **possibly-landed**, never as cancelled, and the continuation
says so (§5, `interleave.possibly_landed_clause`).

---

## 4. The design — a relevance-gated interleave

```
inbound message ─▶ persist to the durable queue (unchanged — this always happens first)
                   │
                   ├─ is a turn in flight?  no ──▶ nothing to decide.
                   │                        yes
                   ├─ LAYER A: deterministic carve-out (regex, in code, ~0 ms)
                   │            match ──▶ STEER
                   ├─ LAYER B: router.classify_steer(new_message, in-flight summary)
                   │            "steer" ──▶ STEER      anything else / no answer in time ──▶ HOLD
                   └─ HOLD = today's behaviour, byte-for-byte: it waits its turn.
```

### 4.1 Layer A — the carve-out

A stop/cancel/correction bypasses the classifier on a deterministic match. It is **insurance, not the
point**: it costs a regex and cannot time out (a loaded host's classifier can return nothing but
fallbacks for hours), and it covers the one case where the error asymmetry really does favour
injecting — a destructive action in flight.

**The invariant: Layer A may only ever ADD an interleave, never suppress one** — `if match: steer`
before the classifier, never `if no match: hold` after it. **Vocabulary, frozen (D4):** `wait`, `stop`,
`hold on`, `hold off`, `cancel`, `nevermind` / `never mind`, `abort`, `scratch that`, `belay`, `undo`,
`don't` / `do not` + (`send`, `run`, `merge`, `post`, `push`, `delete`, `reply`, `commit`),
`not that one`, `wrong one`. Word-boundary matched, case-insensitive, **on the first ~200 characters
only** — the window is the whole of the anchoring that separates a directive from prose about stopping,
and its false-positive cost inside the window is accepted.

### 4.2 Layer B — `router.classify_steer`

A third Router arm: same transport (`_ollama_chat`, a swappable system prompt), same never-raise
contract, same env file. The one difference is that it classifies a **relation**:
`classify_steer(new_message, in_flight) -> dict`, where `in_flight` is a short bounded summary the
daemon already has — the head message's text (capped) plus the tool names seen so far this turn
(`interleave.in_flight_summary`, framed `"The owner asked: …"` to match the prompt's own examples).

Safe fallback: **`hold`**. **D3: Layer A biased hard to inject; Layer B abstains to HOLD**, so
**precision beats recall** when tuning the prompt: a false steer spends a real turn and cuts off an
answer the owner is waiting for; a false hold costs a short wait. The steer arm has its own knobs —
`ROUTER_STEER_MODEL` (empty = `ROUTER_MODEL`), `ROUTER_STEER_TIMEOUT`, and the shared
`ROUTER_KEEP_ALIVE` (`seneschal/scripts/ROUTER_SETUP.md`).

### 4.3 The observe phase, and what it records

**D1: build it.** `--interleave-mode off|observe|live`, default `observe`. In `observe` the gate runs
on every mid-turn arrival and writes to **`state/interleave-log.jsonl`** (gitignored, append-only,
fail-open, never raises):

```json
{"schema":"seneschal.interleave/1","kind":"interleave.arrival","arrival_id":"…12 hex…",
 "ts":"…","channel":"telegram","turn_id":"304bc2a2691c","text_preview":"…80 chars…",
 "layer":"carveout|model","verdict":"steer|hold","confidence":0.95,"reason":"…",
 "arrived_offset_sec":47.4, "arrived_offset_frac":null,
 "typed":true, "fold_depth":2, "verdict_latency_sec":1.05,
 "turn_still_live":true, "turn_remaining_sec":null}
```

plus a second `interleave.resolved` row (joined by `arrival_id`) when the turn ends, carrying
`turn_remaining_sec`, `arrived_offset_frac` and `turn_total_sec` — the two fields only knowable at turn
completion. A verdict that lands after its turn ended writes both at once with a **negative**
`turn_remaining_sec`.

- **`turn_still_live` is the field this phase exists for.** A loaded host's classifier can take longer
  than a typical turn; if verdicts usually land after their turn ends, phase 2 is not usable there.
- **`fold_depth`** — which fold of the turn this would have been — is the only thing that measures how
  deep fold chains go, which §5.1.1's no-cap argument rests on.
- **`typed`** — machine arrivals are logged but never eligible (§5.1).

**Zero behaviour change in `observe`**: nothing is interrupted, nothing is reordered, no prompt is
altered. The daemon-side test for this must be a comparison — the same conversation run with the mode
off and on produces an identical prompt, reply, durable queue and thread tail.

### 4.4 The error asymmetry

Once Layer A handles stop/cancel deterministically, every case left for the model is **additive**. For
those, a false steer (a cut-off answer, possibly derailed by an unrelated message) is worse than a
false hold (a short wait, answered next turn). Hence D3, with the confidence threshold inherited from
`ROUTER_CONF_THRESHOLD`.

---

## 5. Phase 2 mechanics — interrupt, then continue

One drainer iteration, **N + 1 CLI turns for N folds**, one reply.

| # | Step | Why |
|---|---|---|
| 1 | The gate says **steer** for message B while the turn for A is in flight | B is at `state.pending[1]`; nothing is reordered or popped |
| 2 | Set `interrupt_requested`, then write the `control_request` interrupt frame | §3.6: the flag stops the fallback ladder and the "session died" branch firing on the `is_error` result |
| 3 | The current read returns `None` with `interrupt_requested` true | The **existing** read loop, watchdog and all — no new read path |
| 4 | Build the continuation prompt and send it as a **further** `_send_turn` in the same iteration | Explicit rather than relying on §3.4's auto-run: it keeps one-write-one-read symmetry, so §3.5's desync is unreachable |
| 4′ | A further typed steer **during the continuation** repeats steps 2–4 | No cap (D5, §5.1.1) |
| 5 | Deliver **one** reply | The interrupted halves produced no delivered text, so nothing false is asserted |

The continuation prompt (`interleave.CONTINUATION_HEADER`, then §3.7's possibly-landed clause when an
MCP call was cut, then every folded message in order):

> `[The owner sent this while you were still answering their previous message — they have not seen a
> reply yet. Answer all of it together, in one reply. If you had already started down a path their new
> message changes, say so in a clause rather than pretending it didn't happen.]`

*"All of it"* rather than *"both"*, because with no cap the group can be three messages or more. **D6:
one clause naming the cut** — a silently discarded half-answer reads as the assistant ignoring the
owner; a paragraph of apology or a recital of the discarded draft is worse.

**A dead man's switch, by construction:** if the interrupt does not land, the turn runs to its natural
end, the reply to A is delivered, and B stays queued for its own turn. An interrupt with nothing queued
to fold degrades to the ordinary failure branch; nothing waits on a fold that isn't coming.

### 5.1 The serialization invariant, restated

> **Before:** every message is answered in arrival order, in its own turn.
>
> **With interleave:** every message is answered in arrival order, **either in its own turn or folded
> into the reply to the message(s) immediately ahead of it** — never out of order, never twice, never
> partially.

Still one consumer, one turn in flight, one reply per delivery. Three non-negotiables:

1. **Only the contiguous head may absorb a message** — `state.pending[0..n]` as an unbroken prefix; the
   arrival's queue index must be exactly `1 + len(fold_queue)`.
2. **Only a message that arrived while the head's iteration was in flight** — so a *retry* never
   interleaves. The iteration includes every continuation it spawns.
3. **Only a message the owner typed** (`turns.classify_origin(text) == "human"`). Machine arrivals always
   hold. This is what makes §5.1.1's bound structural.

### 5.1.1 Why there is no cap, and what bounds the loop instead

**D5: there is no per-turn fold cap** — not 1, not a constant to tune. On the measured data the only
turns that took a second mid-turn message were ones where **the second message clarified the first**; a
cap of 1 would have fired in exactly those cases and dropped the disambiguating half.

What bounds the loop, none of it a count:

- **No autonomous driver.** Only a typed message may fold (non-negotiable 3), so every extra cycle costs
  a fresh human keystroke, and the chain ends the moment the owner stops typing.
- **Its terminal state is a more complete answer**, not a livelock — under additive context each fold
  makes the pending reply more complete.
- **Nothing else starves.** The chain lives inside one drainer iteration; reminders, the comms peek and
  the cockpit pipe keep running, and the watchdog beats on every stream line.
- **The attempt counter must not become the cap in disguise.** One drainer iteration is one attempt
  however many CLI turns it contained; counting folds into `MAX_TURN_ATTEMPTS` would turn a fast typist
  into a poison pill whose head message gets dropped as unanswerable.

**The residue is accepted plainly:** if the owner types faster than turns complete, no reply lands until
they pause, and every cycle bills discarded output tokens. **Do not reintroduce a cap under another
name** — a token budget, a discarded-output ceiling, a minimum gap, an elapsed-time cap, a "max
continuations". The one legitimate route back is evidence: the `fold_depth` histogram, taken to the
owner as a new decision.

### 5.2 The records an interleaved exchange writes

| Record | What happens |
|---|---|
| the thread cache | The owner's side is appended at intake, so every folded message is in the tail before the continuation runs; the assistant's side once, after delivery |
| `state/turns.jsonl` | **The trap.** Capture runs for the head only, so a folded message would vanish from the verbatim record. The live branch captures **every** folded message at the fold (same `turn_id`, redacted like the head), not once at the end |
| `state/assertions.jsonl` | Unchanged — one landed reply, one assertion |
| the Notion outbox (notion backend) | An interrupt cannot un-journal a write that already landed through the outbox, but an MCP call interrupted mid-flight is outside the outbox entirely and may still commit (§3.7) — hence the possibly-landed clause |

### 5.3 Ordering and the durable queue

No schema change to `presence-state.json`: the fold is decided and consumed inside one iteration. A
crash before delivery re-sends the head alone and the rest follow as their own turns — the existing
undelivered-reply behaviour. **After delivery the whole folded group pops together**, and the pop
re-verifies identity first: verify the whole prefix (head plus every fold, in order) against the
queue's current head, pop the longest matching prefix, and let the rest take their own turns. Never pop
by index alone or by count alone (`pop_folded_prefix`).

### 5.4 Observability

- **One `turn_id` for the whole exchange** — the cockpit renders a new card per `turn_started`.
- **A `chat.event` kind `interleaved`** at each fold (preview, layer, verdict). An older frontend
  degrades it to an extra block, per the tolerant-reader rule.
- **Every discarded partial stays in the transcript**, each followed by its `interleaved` marker.
- **N + 1 results mean N + 1 metrics rows and N + 1 ledger rows** sharing a `turn_id`. **D7: leave it
  honest** — they really ran and were really billed; the Usage panel counts N + 1 turns.

### 5.5 As built — where each step lives

| Design step | Where |
|---|---|
| §3.2's interrupt frame | `WarmSession.send_interrupt()` (claude-cli backend) — never raises, sets `interrupt_requested` even on a failed write *(pending)* |
| The flag mirroring `timed_out` | `WarmSession.interrupt_requested`, reset at the top of `send()`; both fallback-ladder rungs guard on it *(pending)* |
| §5.1's non-negotiables | `presence.interleave_observe`'s live branch — queue index, `STEER`, `typed`, and a session with `send_interrupt`, before interrupting *(pending)* |
| Steps 2–4, 4′ | `presence.interleave_live_send()` — an async loop around `session.send()` returning `(reply, folded)`; `folded == []` on every `off`/`observe` turn *(pending)* |
| The continuation prompt | `interleave.build_continuation_prompt()` + `CONTINUATION_HEADER` |
| The possibly-landed clause | `interleave.possibly_landed_clause()`, fed by `presence.interleave_track_tool_call()`'s last unresolved `tool_use` *(pending)*; row via `interleave.record_mcp_possibly_landed()` |
| §5.2's capture-at-each-fold | `_capture_turn()` from `interleave_observe`'s live branch *(pending)* |
| §5.3's pop discipline | `presence.pop_folded_prefix()` *(pending)* |
| The version pin | `interleave.PROBED_CLI_VERSIONS` + `current_cli_version()`; `refuse_mode("live")` refuses any other installed version, per host |

**Deliberately unfixed:** a narrow race between one continuation's `send()` starting and a stray
`interrupt_requested` from the tail of the preceding fold decision can cut the new continuation before
it produces anything. Worst case is one extra, harmless empty-fold round; nothing pops until final
delivery. Engineering it away would need a correlation id the CLI's `control_request` does not offer.

**Tests:** `seneschal/scripts/test_interleave.py` (carve-out, gate, schema, stats/diagnose, the mode
refusal) and `seneschal/scripts/test_interleave_phase2.py` (continuation prompt, possibly-landed clause,
and — skipping until the daemon hooks land — the interrupt primitive, ladder suppression, the live
loop, contiguity, tool-call tracking and the pop discipline).

---

## 6. The latency budget

A warm local classifier answers in about a second; **a cold, contended one can take a minute**
(model load plus prompt eval), well past the default 20 s timeout. Three rules follow:

1. **The gate runs off the intake path**, after the persist, in a thread — a slow verdict costs the
   interleave, never the message.
2. **A verdict that lands after its turn ended is not acted on** — it is logged and resolved with a
   negative `turn_remaining_sec`.
3. **Fail-open is HOLD** — Ollama unreachable, timeout, bad JSON, low confidence, unknown verdict, or a
   late verdict all mean today's behaviour. Layer A does not depend on any of it.

**Corollary:** if `turn_still_live` comes back mostly false, the answer is not a bigger timeout. It is a
smaller model (`ROUTER_STEER_MODEL`), a longer `ROUTER_KEEP_ALIVE` so the model stays resident between
sparse steer calls, or the conclusion that this host cannot run a model-gated interleave.

### 6a. Fail-open is measured separately

A timeout/unreachable fallback lands on the MODEL layer looking exactly like a real verdict. Every
fallback path in `router.py` hard-codes `confidence: 0.0`, so `router.is_fallback_verdict` identifies
them, and `interleave.stats()` reports `fail_open` / `fail_open_rate` / `classified_real` and reads D8's
bar off `classified_real` (`q8_met_on_real_verdicts`). `interleave.diagnose()` breaks fail-opens down by
ISO week, by exception, and by latency (real vs fail-open), so a contended week is visible. Steer
arrivals are sparse — often further apart than Ollama's default idle-unload window — so without a keep-
alive most steer calls are cold loads; other Ollama traffic displacing the model makes that worse.

---

## 7. The rejected alternative — unconditional interleave

Inject every mid-turn message, no gate. Simpler, no classifier, no latency budget — and dangerous: the
holds in §2 (a status update, an unrelated question) would cut off the answer the owner is waiting for.
With no cap (D5) the gate is also the only bound that discriminates at all; an ungated chain converges
on nothing. And it is not what was asked for.

## 8. The alternatives, weighed

| | Cost | Buys | Verdict |
|---|---|---|---|
| **(a) Status quo** | nothing | nothing | The floor |
| **(a′) The arrival marker** — one prompt-only line telling the assistant a message arrived while it was mid-answer, so it isn't read as a reply to a reply | a few tokens on some turns; no interrupt, no queue change, no classifier | fixes the *conversational* half: an addition stops reading as a response to an answer the owner hasn't seen | **D2: ship it, on its own** (phase 1) |
| **(b) Cancel and restart with the whole group** | the partial prose (not the work, §3.3) | everything (c) would | this *is* (c) — there is no injection primitive |
| **(c) True injection** | — | — | **does not exist** (§3.1) |
| **(d) Relevance-gated interleave** | the §5.2 surface | the wait, plus corrections landing before the answer | phase 0 approved; phase 2 built behind the flag |

**Shipping order:** phase 0 (observe) and phase 1 (the arrival marker) first and independently; phase 2
(`live`) gated on D8's evidence bar and then on the owner's own decision; **phase 3 is nothing** — no
second queue, no partial delivery, no streaming of a half-finished answer, and no fold cap.

## 9. What this spec does not claim

- **The win is ordinary and frequent, not dramatic** — a short wait on a few messages a day, each
  otherwise answered without context the owner already sent. If the observe rows show the gate
  mediocre, its verdicts late, or its folds landing too late to matter, "(a′) and nothing else" is a
  legitimate outcome.
- **It does not bound the type-fast loop** (§5.1.1) — named and accepted.
- **It does not make a healthy turn worse** — no path touches a turn with no mid-turn arrival.
- **No new dependency, process, or persisted-queue schema change.**
- **It is not a fix for slow turns** — interleave makes the wait interruptible, not shorter.
- **It changes no policy** — reminders, the quiet window and act-low/ask-high gate a folded message
  exactly as they would in its own turn.

---

## 10. Design decisions

| | Question | Decision |
|---|---|---|
| **D1** | Build the observe phase | **Yes** |
| **D2** | Ship the arrival marker on its own | **Yes** |
| **D3** | Which way the model layer abstains | **Layer A biased hard to inject; Layer B abstains to HOLD** |
| **D4** | Carve-out vocabulary | **The generic list in §4.1, no additions** |
| **D5** | A per-turn fold cap | **None at all** (§5.1.1) |
| **D6** | Name the cut when a half-written answer is discarded | **Yes, one clause** |
| **D7** | An interleaved exchange's spend rows | **Leave it honest — N + 1 rows, N + 1 turns** |
| **D8** | Phase 2's evidence gate | **Two weeks AND ≥ 30 real (non-fail-open) classified mid-turn arrivals, whichever lands second — then the owner decides** |

**D8's bar is when the decision gets made, not what it is.** Read the count with
`python seneschal/scripts/interleave.py stats` → `classified_real` (a carve-out verdict is instant by
construction and a fail-open never ran, so either would flatter the number). The `q8_` key names in
`stats()` output are the stable row schema. `live` additionally refuses on any host whose installed CLI
version is not in `PROBED_CLI_VERSIONS` — re-run the §3 probe at the new version before adding it.
