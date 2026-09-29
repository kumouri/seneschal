# Spend levers — logging *why* a turn cost what it cost

**Status:** `PARTIAL(phase 0's ledger field + phase 1's counter, report and stream-tee call sites BUILT; phases 2-4 designed, not built)` —
**phase 0** is the join key: `governor.append_spend` accepts `turn_id=` (R2). **Phase 1** is the levers
that existed nowhere: `seneschal/scripts/spend_levers.py`'s `TurnLevers` counter (`levers.tool_calls`
/ `tool_result_bytes` / `tool_result_images`, written through `governor.append_spend(levers=…)`) plus
`spend_levers.py report`, the reader that runs the join. The caller half is wired:
`presence._make_stream_tee` feeds each raw event to the counter and passes its `turn_id` and the
flushed levers to `governor.append_spend` on every `turn_done` (§8's phase-0 note explains why that half
is the load-bearing one). Zero behaviour change
either way; no lever gates, refuses or alerts anything. §7's decisions are this spec's own and
revisable; §11 holds the owner's open questions.
**Sibling designs:** the context-budget checks (`../scripts/check_context_budget.py` — the
startup-context producer, one of the levers named here and already owned there), and `cockpit-spec.md`
§"Model dials & Fable delegation" + the Oikonomos section (the rails that read the ledger).

> **R1 is the rule this module lives under: diagnosis stays diagnosis.** Oikonomos (`governor.py`,
> Advisor Chain order 15) is the one place a rail may live — it meters the ledger and gates Fable
> delegation. This spec only adds *why* to the rows it reads. The tempting next step — letting a lever
> gate something directly — would grow a second enforcement path beside the governor, and two rails
> that disagree are worse than one that is honest.

## 1. The thesis, in one paragraph

The governor measures spend **honestly** — a ledger row carries the raw token sum, the component
breakdown, and a derived `billable_tokens` on a named weight basis. What it cannot do is answer the
only question worth asking of a budget: **what made it that big?** A total decomposed by *token type*
says a turn was 98.7% cache read. It does not say that the turn was the thirteenth of a session, that
nothing had been evicted since turn one, that four tool results were carried forward, or that two
images were in the window. Those are the **levers** — the things a person or a policy could actually
pull — and the ledger records none of them. This spec adds the levers to the record. It changes no
behaviour, gates nothing, and refuses nothing. It exists so that the *next* time a number looks wrong,
the answer is a query rather than an afternoon.

## 2. What is true without it

### 2.1 The two logs

`state/governor-ledger.jsonl` — what the rails read.

```json
{"ts": "2026-01-01T23:40:33.738914Z", "kind": "tokens", "model": "claude-opus-5",
 "tokens": 505975,
 "components": {"input": 6, "output": 3267, "cache_read": 499309,
                "cache_write_5m": 0, "cache_write_1h": 3393, "cache_write_unspecified": 0},
 "billable_tokens": 59990, "basis": "input_token_equivalent_v1"}
```

`state/metrics.jsonl` — per-turn history; **nothing enforces off it.**

```json
{"ts": "2026-01-01T23:40:34.161272Z", "mode": "Chat", "model": "claude-opus-5", "source": "telegram",
 "turn_id": "e1c31303253d", "session_id": "00000000-…", "turns_served": 13,
 "tokens": 505975, "context_tokens": 167569, "num_turns": 3, "duration_ms": 70718,
 "cost_usd": 0.365289, "session_cost_usd": 2.3882155, "outcome": "Success", "writer": "daemon",
 "usage": {…}}
```

### 2.2 They are written back to back, by the same function, and share nothing

In the design both come out of the stream tee's `turn_done` branch, one after the other (on this
branch the per-turn `metrics.jsonl` writer is itself part of the pending daemon wiring; today the tee
writes only the ledger row):

```python
if chat_ev.get("kind") == "turn_done":
    _governor_meter_turn_usage(args, log, model, chat_ev.get("usage"))
    _append_turn_metrics(state, args, log, channel, turn_id, chat_ev)
```

The first writes the ledger row. The second writes the metrics row **and already has `turn_id` in
hand.** The ledger row does not carry it. So the two files can be joined only by timestamp proximity
— which is a heuristic, not a key, and breaks the moment two turns finish inside the same second or a
delegation writes a ledger row of its own between them.

**This is the cheapest finding in the spec.** Half the levers are already recorded; they are simply in
the file that nothing reads, with no key to join on.

### 2.3 The precedent this is built on

The governor once alarmed at several hundred percent of a daily token budget while the provider's own
plan panel read well under its limits — because nearly all of that day's "tokens" were cache reads,
summed flat. The fix was to stop summing token *types* flat and start weighting them (the billable
basis, `governor.TOKEN_WEIGHTS`).

That fix is the same move this spec makes, one level down. **Decomposing by token type turned a wrong
number into a right one. Decomposing by cause turns a right number into an actionable one.** The row
above is not wrong — 59,990 billable is exactly correct — but nothing in it explains why one chat turn
carried half a million cache-read tokens, and the ledger is where anyone would look.

## 3. The levers

Each row: what it is, who or what controls it, and whether it is observable. **"Observed and
discarded"** is the important category — it means the data flows through a function we already run.

| # | Lever | Controlled by | Recorded |
|---|---|---|---|
| L1 | **Session length** — turns served so far. Cache read grows monotonically with conversation. | Idle wind-down, the resume age limit, restart cadence | ✅ in metrics (`turns_served`), ❌ in ledger |
| L2 | **Cold grounding vs. resume** — a cold start pays the full grounding; a resume pays the resume preamble | `presence.resume_decision` (clean death + recency + context ceiling) | ❌ not recorded per turn |
| L3 | **Startup context size** — the bytes of the grounding files themselves | The producer; **owned by the context-budget checks** | ❌ (that design's job, named here for completeness) |
| L4 | **Tool-schema surface** — every attached MCP server's schemas ride in the system prompt, and servers connect and *disconnect mid-session* | Which MCP servers are configured; `ToolSearch` fetches | ❌ not recorded |
| L5 | **Tool-call volume and result size** — each `Read`/`Bash`/`Grep` result enters context and is re-read on **every subsequent turn** | The assistant's own choices within a turn | ✅ **in ledger** (`levers.tool_calls`, `levers.tool_result_bytes`) once phase 1 is wired |
| L6 | **Images in the window** | The assistant (see §3.2) | ✅ **in ledger** (`levers.tool_result_images`) once phase 1 is wired — **not where the first draft said they were**; see §3.2 |
| L7 | **Cache write class** — 1 h writes cost 2×, 5 m 1.25× | The CLI, partly | ✅ in `components` |
| L8 | **Model** | `state/model-config.json` dials | ✅ in ledger |
| L9 | **Delegated spend** — Fable one-shots, `jobs.py` children | `fable_delegate.py`, job spawns | ✅ metered as own rows, ❌ **not attributed to a parent** |
| L10 | **Output length** — the assistant's own verbosity | The assistant | ✅ in `components.output` |
| L11 | **Retrieval volume** — how much RAG/store material was pulled into the window | The Retrieval advisor | ❌ not recorded |
| L12 | **Server tools** — web search / fetch requests | The assistant | ✅ in `metrics.usage.server_tool_use`, ❌ in ledger |

**As specced:** five already measured (L7, L8, L10, and — in the wrong file — L1, L12), two observed
and thrown away (L5, L6), four not observed at all (L2, L4, L9-attribution, L11). L3 belongs to
another design and is listed so nobody specs it twice.

**With phase 1 wired: seven are measured** — L7, L8, L10 in the ledger; L1 and L12 in `metrics.jsonl`
and *joinable* rather than merely adjacent; **L5 and L6 in the ledger's `levers` block.** **Four remain
unmeasured**: L2 (session start), L4 (tool-schema surface), L9-attribution, L11 (retrieval volume) —
phases 2-4, each with a named reason it is not free (§8.1).

### 3.1 The compounding one, named separately

**L5 is not like the others.** A 40 kB file read on turn 3 is not a turn-3 cost; it is a cost on turns
3 through 13, because the whole conversation is re-sent every turn. A single careless `cat` of a large
log is charged ten times and shows up in the ledger as ten unremarkable cache-read totals.

That is precisely the shape of the 505,975-token row in §2.1 — turn 13, `cache_read` at 98.7%, and no
field anywhere naming what was in it. **Any lever list that omits L5 will keep producing rows nobody
can explain.**

### 3.2 L6 was in the right list and at the wrong address (found building phase 1)

The first draft of L6 said images "arrive unpredictably" and are controlled by the owner. That
describes a daemon this is not. Two facts from the code, not from inference:

- `WarmSession._send_turn` hardcodes the user message to `[{"type": "text", "text": text}]`. **Every
  turn the warm session is ever sent is text.** There is no code path by which an inbound message
  becomes an image block in the window.
- `presence._attachment_or_text` turns an inbound photo into a *descriptor*:
  `[attachment: photo saved to <path>]`. That costs a few dozen tokens, once.

So an image enters the window **only** when the assistant chooses to `Read` the file — which arrives
as a **tool result**. L6 is therefore not the owner's lever at all; it is L5's sibling, and it is the
assistant's. Phase 1 counts `levers.tool_result_images` at the tool result, where the cost actually is.

**§5.1's proposed `attachments: {"image": 2}` field is deliberately not written.** It could have been
populated off the inbound queue — the attachment records are right there — and it would have been a
number that looked measured, sat next to honest ones, and attributed the spend to the wrong party. R4
exists to stop a 0 reading as *measured, and free*; this is the same failure with a non-zero value,
and it is worse, because a diagnosis tool that misattributes sends the next investigation the wrong
way.

Image blocks in session transcripts are rare in practice — well under one percent of sessions — but
real, and now counted rather than assumed either way.

## 4. Where the data already flows

`_make_stream_tee` builds one `on_event` callback per turn. Every stream-json line from the CLI passes
through it, gets converted by `cockpit_pipe.build_chat_event_from_stream`, stamped with `turn_id`, and
teed to the cockpit. `tool_use` events are among them — that is how the cockpit chat pane renders
per-turn tool summaries.

So the counting site for L5, L6 and L12 is a **local accumulator in a function we already call, on
data we already parse.** No new subscription, no re-derivation, no second pass over a transcript.

`state/warm-transcript.jsonl` (the capped ring buffer) holds the same events for recent turns, which
matters only for §11's backfill question.

## 5. The record

Two additions, both to rows that already exist.

### 5.1 The ledger row gains a join key and a lever block

> **As built, this example is the MAXIMAL shape, not the shipped one.** `turn_id`, `tool_calls` and
> `tool_result_bytes` are real; `attachments` became `tool_result_images` for the reason in §3.2;
> `turns_served` and `server_tools` are deliberately not duplicated (Q1); `session_start`,
> `mcp_servers` and `retrieval_docs` are phases 2 and 4. See §8.

```json
{"ts": "…", "kind": "tokens", "model": "claude-opus-5",
 "tokens": 505975, "components": {…}, "billable_tokens": 59990, "basis": "input_token_equivalent_v1",

 "turn_id": "e1c31303253d",
 "levers": {
   "turns_served": 13,
   "session_start": "cold_grounded",
   "tool_calls": 4,
   "tool_result_bytes": 38112,
   "attachments": {"image": 2},
   "server_tools": {"web_search": 1},
   "mcp_servers": ["notion", "slack"],
   "retrieval_docs": 0
 }}
```

### 5.2 The delegation row gains a parent

```json
{"ts": "…", "kind": "fable_oneshot", "model": "claude-fable-5", …,
 "parent_turn_id": "e1c31303253d", "trigger": "force_fable"}
```

`trigger` is one of `router` / `judgment` / `force_fable`, which is the lever behind L9: the three
paths have very different volumes and only one of them is the assistant's own discretion.

## 6. What this buys, concretely

Three questions that are an afternoon of hand-analysis without it and a one-line query with it:

- *"Why was today expensive?"* — group billable by `levers.session_start`; a day of cold starts and a
  day of resumes are different products.
- *"Is a long session cheaper than a fresh one?"* — the load-bearing question behind the warm
  session's wind-down policy, deliberately deferred until there was data. This spec is that data.
  Regress `billable_tokens` on `turns_served`, and the wind-down policy stops being a guess.
- *"Which of the assistant's own habits costs the most?"* — `tool_result_bytes` per turn against
  billable. If the answer is that reading files is the dominant lever, that is a finding about the
  assistant's behaviour, and it is the one this spec is most likely to produce.

## 7. Decisions — this spec's own, and revisable

**R1 — Record, do not enforce.** No lever becomes a gate, a refusal, or an alert in this spec. Observe
first, decide later, and let the data argue. A lever that throttled a turn before anyone had seen a
month of it would be a policy written from a guess. Enforcement is Oikonomos's, and only Oikonomos's.

**R2 — `turn_id` is phase 0, alone.** One field, in a row the same function already writes, joining
two files that otherwise share nothing. It is worth shipping by itself even if the rest of this spec
is never built.

**R3 — Levers are recorded where they are observed, never re-derived later.** A second pass that
reconstructs tool counts from a transcript is how a feature quietly becomes optional and then becomes
zero rows. An observability contract written prompt-side — one that costs tokens every turn and
collects nothing — is the standing cautionary case.

**R4 — Never write a 0 for an unmeasured lever.** Absent field, or an explicit `"unavailable"` —
never a 0. This is `append_spend`'s existing `metered: "unavailable"` rule, and it exists because a 0
reads as *measured, and free*, which is exactly how an unmetered delegation once rolled up as nothing
against the tightest budget on the board.

**R5 — Fail-open, at the field level.** A lever that cannot be computed costs that field. It never
costs the row, and the row never costs the turn. Same contract as `append_spend`: swallow and move on.

**R6 — Attribution, not folding.** A delegation row names its `parent_turn_id`; its tokens are **not**
added into the parent's total. Double-counting a child's spend into the parent would inflate exactly
the rows the fable rail reads.

**R7 — No new file.** Both logs already exist and already have readers. A third store is a third thing
to keep in sync.

**R8 — `tokens` and `components` keep their current meanings, forever.** The billable-basis fix earned
its trust by *not* reinterpreting the rows already on disk. Levers are additive; no existing field
changes.

## 8. Phases

- **Phase 0 — the join key.** **The field is BUILT; the call site is not yet wired.**
  `governor.append_spend` accepts a `turn_id=` keyword; the tee must pass the id it already has in
  hand through `presence._governor_meter_turn_usage`. Zero behaviour change. Unlocks every lever
  `metrics.jsonl` already carries, prospectively for free.

  Three notes on the built half:

  - **Omitted, never zeroed** (R4's reasoning, applied to a key rather than a count). A delegation or
    a job child is not a warm turn and has no turn to name, so its row simply has no `turn_id`. An
    empty string or a null would read as a turn that could be looked up and cannot.
  - **Purely additive** (R8). A row written *with* the key is byte-identical to one written without it
    apart from `ts` and `turn_id` itself, so the billable-basis fix's "history keeps its meaning"
    property is preserved.
  - **The load-bearing test is the caller's, not the field's.** When this was built against a wired
    daemon and then verified by reverting the caller — the tee no longer passing the id — the
    governor's own unit tests all stayed green and only the tee's join test failed. That asymmetry is
    the entire finding of §2.2 restated as a test: the field was never the hard part, the wiring was.
    It is also why this spec reads PARTIAL until the wiring lands.
- **Phase 1 — the observed-and-discarded three.** **The counter and the report are BUILT; the tee
  wiring is not.** `seneschal/scripts/spend_levers.py`: `TurnLevers`, a local accumulator meant for
  `_make_stream_tee`, flushed at `turn_done` (§4), writing `levers.tool_calls` / `tool_result_bytes` /
  `tool_result_images` onto the ledger row via `governor.append_spend(levers=…)`. Plus
  `spend_levers.py report`, the ledger↔metrics join — phase 0 builds the key and nothing else turns it.

  Six notes, four of which are deviations from §5.1 and are the reason to read this block rather than
  the schema above:

  - **The counting site is the RAW event, before the cockpit conversion.** §4 said the tee already
    sees what we need, and it was half right: `build_chat_event_from_stream` **returns None for
    `user` events** — a chat pane has no use for them — and `user` events are exactly where tool
    *results* live. The most expensive thing in the window was passing through the tee, being parsed,
    and being discarded one line later. `levers.observe(ev)` belongs on the first line of the callback
    for that reason.
  - **`attachments` is not written; `tool_result_images` is** — §3.2.
  - **`turns_served` and `server_tools` are not copied into `levers`.** Both already exist on the
    `metrics.jsonl` row for the same turn, and phase 0's key reaches them. Duplicating them is **Q1**
    (fat rows vs. a join), an open question and not phase 1's to pre-empt.
  - **Q5 is answered by measurement, not by decision.** The question assumed a trade — "bytes are the
    real driver ... counts are nearly free." Measured: counting the bytes of a 40 kB string tool result
    costs **~1.6 µs**, against the **~29 µs** the `json.loads` on that same line already cost — about
    5%; at the 256 kB `Read` ceiling it is under 4%. A whole realistic turn (6 tool calls, 6 × 20 kB
    results) costs **~10 µs** end to end. There was no trade to make.
  - **Overhead, stated plainly:** ~10 µs of CPU per turn on the stdout-reader thread, **~83 bytes** on
    the ledger row, and — the number that matters — **zero tokens.** Nothing here enters a model's
    context; it is a counter over a stream the daemon already parses.
  - **`flush()` resets, and that is load-bearing.** `WarmSession.send`'s first-turn fallback ladder
    re-sends after a failed resume, and each attempt ends in its own `result` event — so one tee can
    meter twice. Without the reset the second row re-charges the first attempt's reads. A lever that
    over-reports is worse than one that is absent.

  **The load-bearing test is the caller's here too** — phase 0's asymmetry again: with
  `levers.observe(ev)` reverted, the counter's unit tests stay green and only the tee tests fail. The
  counter was never the hard part.
- **Phase 2 — session shape.** `session_start` (`cold_grounded` / `resumed`) off `resume_decision`,
  and `mcp_servers` off the spawn environment. Requires threading two values that exist but do not
  currently reach the tee.
- **Phase 3 — delegation attribution.** `parent_turn_id` + `trigger` on Fable and job rows (R6).
- **Phase 4 — retrieval volume (L11).** Last, because the Retrieval advisor has no equivalent single
  choke point and this may cost more plumbing than it returns.

Phases 0–1 are the ones with a live question behind them. **3 and 4 may never be worth building**, and
that is an acceptable ending.

### 8.1 What is still unmeasured after phase 1, and why each one costs more than it looks

Recorded here rather than approximated, per R4 — an honest gap beats a made-up number.

| Lever | Why phase 1 does not take it |
|---|---|
| **L2 — cold grounding vs. resume** | `resume_decision`'s verdict is known at *spawn*, not at turn end, and the tee closure is built per turn with no reference to it. Real plumbing, not a counter: phase 2. Approximating it from `turns_served == 1` would be wrong on exactly the resumed sessions the field exists to identify. |
| **L4 — tool-schema surface** | The daemon does not know it. MCP servers connect and *disconnect mid-session*, and the CLI reports the set nowhere the daemon parses. Counting the *configured* servers would measure the config rather than the window. |
| **L9 — delegation attribution** | The child rows exist and are metered; what is missing is `parent_turn_id`, which lives in `fable_delegate.py` and `jobs.py`, not in the tee. Phase 3. `spend_levers.py report` therefore prints unattributed spend as its **own line** rather than folding or dropping it (R6). |
| **L11 — retrieval volume** | No single choke point — phase 4's reason, unchanged, and still the one most likely never to be worth it. |

## 9. Deliberately not in scope

- **Dollars.** `metrics.jsonl` already carries `cost_usd` exactly; the ledger's basis is deliberately
  input-token-equivalent, not currency, and this spec does not blur them.
- **Re-sizing the budgets.** Budgets set before the billable basis existed are looser than intended
  now that the basis moved under them. That is a cockpit action the owner takes, not one a change
  takes for them.
- **The startup-context producer** — the context-budget checks own L3.
- **Any cockpit panel.** §11 Q4.

## 10. What this spec does NOT answer

- Whether a long warm session is cheaper than frequent cold ones. It **produces the data**; it does
  not conclude.
- What a healthy `tool_result_bytes` per turn is. Unknown, and there is no prior to guess from.
- Whether the eventual finding is actionable. It is entirely possible the dominant lever turns out to
  be conversation length, which is already governed by the wind-down policy, and the answer is "we
  were already doing the right thing." **That is a real outcome and not a failed spec.**

## 11. Open, and the owner's to decide

**Q1 — Fat ledger rows, or a thin ledger and a join?** §5.1 proposes fat rows so the rails can read
levers without opening a second file. The alternative is phase 0 only — `turn_id` — and every lever
lives in `metrics.jsonl`. Fat duplicates data; thin means no rail can ever act on a lever without a
join, which R1 says we are not doing anyway.

**Q2 — Retention.** Both logs grow, with no prune on the ledger. The levers block adds ~83 bytes to a
~300-byte row. Prune the ledger on a fixed window like the assertions log, or keep it forever because
a year-over-year comparison is the whole point?

**Q3 — Observe-only forever, or is there a trigger to build toward?** R1 says record-don't-enforce.
Is that permanent, or should there be a named condition — e.g. *if `tool_result_bytes` clears N for a
week* — at which point this comes back as a proposal (to Oikonomos, never as a rail of its own)?

**Q4 — Cockpit panel?** A "why was today expensive" view is the obvious payoff and is also a whole
cockpit phase. The floor is `spend_levers.py report`, a read-only CLI that runs the join and prints
the day dearest-turn-first — what a panel would render if one is ever wanted.

**Q5 — Bytes, or just counts, for tool results?** **Answered by measurement — bytes, and the trade
this question assumed does not exist.** See §8 phase 1.

**Q6 — Backfill?** `warm-transcript.jsonl` is capped, and ledger rows written before the key existed
cannot be given one. Accept a clean start, or spend effort on partial reconstruction that will be
honest only for recent turns?

## Router entry

**What it decides:** *why* a turn cost what it cost — twelve levers, seven measured once phase 1 is
wired, four unmeasured (§8.1), L6 re-grounded at the tool result (§3.2). Diagnosis only; Oikonomos
remains the only rail.
