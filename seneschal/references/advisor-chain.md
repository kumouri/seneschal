# Advisor Chain — the assistant's per-turn interceptor pipeline (Spring-AI-style)

> **Status: ADOPTED — Markdown-native (Option A). Phases 1–2.** This is the first of the "advisor
> upgrades." It names, orders, and makes composable the per-turn lifecycle the assistant *already* runs
> implicitly, so every mode inherits the same pipeline instead of re-deriving it from prose in
> `SKILL.md`. It's a **naming-and-ordering** upgrade: it does **not** change *what* the assistant does,
> only makes the pipeline explicit and composable. Enforcement is the orchestrator following this spec
> each turn — the same way it follows the execution philosophy. Code-backed rails (Option B) are a
> later, optional phase (see below).

## Why — the Spring AI mapping

Spring AI's **Advisor API** wraps every model call in an ordered chain of interceptors. Each advisor
gets an `around` hook (mutate the request on the way *in*, mutate the response on the way *out*), the
chain shares an **advisor-context** map, and advisors register by `getOrder()` (lower order = *outer* =
runs earliest inbound, latest outbound). The built-ins tell you what the pattern is *for*:

| Spring AI advisor | What it does | The assistant's existing analog |
|-------------------|--------------|-------------------------|
| `MessageChatMemoryAdvisor` | inject conversation memory | context-digest → carry-over → run-log tail (`memory.md`) |
| `QuestionAnswerAdvisor` / `RetrievalAugmentationAdvisor` | RAG: retrieve + augment | the **Retrieval / Context** advisor — modular RAG over baked refs + Notion/calendar (see below; a local semantic index is phase B) |
| `SafeGuardAdvisor` | block sensitive content | the **act-low / ask-high** approval gate (`autonomy-policy.md`) |
| `SimpleLoggerAdvisor` | log the exchange | the **Run Log** "leave a trace" (`memory.md`) |

The assistant already does all four — but as scattered execution-philosophy prose, not a named, ordered,
composable chain. **The upgrade is to make the pipeline explicit**: one Advisor Chain, defined once,
inherited by every mode, with each mode able to add/drop advisors for its turn (Spring's per-request
`.advisors(...)`). That gives modularity, a clear order of operations, an inspectable turn, and clean
extension points for the *rest* of the advisor upgrades.

## The chain (ordered; lower = outer)

Every substantive turn runs through this pipeline. `in:` = before the work (inbound). `out:` = after
the work (outbound), unwound in reverse order.

| Order | Advisor | `in:` (inbound) | `out:` (outbound) |
|-------|---------|-----------------|-------------------|
| 0 | **Trace** (`SimpleLoggerAdvisor`) | open a Run Log row early (`Status = Partial`) once past critical path | finalize counts + `Status` + carry-over; mirror to `state/run-log.md`; **emit a metrics line** (Observability, below) |
| 5 | **Prioritization / ranking** *(out-only)* | — | shape the assembled deliverable before Trace logs it: **pull → rank, show all; push → adaptive vital-few** (see below) |
| 10 | **Orientation / Memory** (`MessageChatMemoryAdvisor`) | read `context-digest.md` → `carry-over.md` → run-log tail; resolve "today" in the owner's configured timezone | persist updated carry-over / digest deltas |
| 20 | **Retrieval / Context** (`RetrievalAugmentationAdvisor`) | modular RAG: **query-transform → retrieve → rerank + compress → augment** — baked-in refs first, cap concurrent `notion-*` reads; *fewer, better* reads (see below) | (usually none) |
| 30 | **Dispatch / Delegate** | pick the mode; load the owning subagent `SKILL.md` (delegate, don't duplicate) | — |
| 35 | **Critique / self-review** | review any *drafted outbound* against the five-point check; **revise-once-and-note**, else flag (see below) | — |
| 40 | **SafeGuard / Gate** (`SafeGuardAdvisor`) | classify each pending action act-low vs ask-high | act-low passes through; **ask-high is drafted-and-held** to carry-over + `pending-approvals.json`, never auto-sent |

**Semantics that make it a chain, not a checklist:**

- **Shared turn-context.** A per-turn scratch map (Spring's *advisor-context*) flows across advisors, so
  the reads Retrieval made are visible to Gate and Trace without re-querying. Concretely: what was read,
  what's being written, which drafts are held, the resolved date. It's the connective tissue.
- **Reverse unwinding.** Trace opens outermost/first and closes outermost/last, so it brackets the whole
  turn — a cut-short run still left a `Partial` row (critical-path-first, honored).
- **The Gate wraps the *action*, not the words.** Spring's SafeGuard blocks sensitive *tokens*; the
  assistant's variant intercepts sensitive *actions* (outbound/destructive) — same interceptor shape,
  right layer for an assistant that acts.

## Critique / self-review advisor (order 35) — ADOPTED

The first **guardrail** advisor. It sits between Dispatch (which produces a draft) and the Gate (which
routes it): **Dispatch drafts → Critique reviews/revises → Gate holds-for-approval or passes.** It does
not replace the Gate — the Gate decides *whether the owner must see it*; Critique decides *whether it's
good enough to leave the assistant's hands*. It runs on the `in:` side toward the Gate; it only fires
when the turn produced **outbound content** (nothing to review otherwise).

**The five-point check** (run against `../../persona/persona.md (else persona.default.md)` + `../../persona/owner-profile.md`):

1. **Register** — right voice for the recipient (firm-gatekeeper for outsiders, crisp-warm for the
   owner's people). Never sharp.
2. **Identity** — signs as the assistant, in the assistant's own name ("<Assistant>, <the owner>'s
   assistant"); never as the owner, never first-person-as-the-owner.
3. **Faithfulness** — every claimed fact (date, commitment, task) traces to Notion/calendar/email; nothing
   invented. For **Slack** drafts this is the **derivation contract**: every fact traces to the SSOT
   (`slack-ssot.md`), a live Retrieval read, or the inbound thread — anything else becomes the escalation
   phrase or no draft (`../../subagents/slack-triage/SKILL.md`). A claim Critique can't trace is a fail.
4. **Privacy** — no health / identity / financial data leaking to an **external** recipient. For Slack
   this maps to the SSOT's **"Never state" fence** (`slack-ssot.md`). *(Seed of
   the future dedicated Privacy advisor — Critique grows into it.)*
5. **Concision** — leads with the point, no padding.

**Behavior — revise-once-and-note** *(the owner's call)*:

- On a fail, Critique **revises the draft once** and appends a one-line changelog to the approval surface
  — *"tightened the tone; dropped a line that referenced your health details."* The owner approves the
  **improved** draft, not a bounced one.
- If it **can't safely fix** (e.g. a factual claim it can't verify against a source), it **flags** the
  issue and holds rather than guessing — never invents a fix.
- The revision + note ride the normal held-approval surface (`carry-over.md` / `pending-approvals.json`);
  the changelog is part of the "ready to send?" line.

**Scope & cost:**

- **Outbound + surfaced ask-high drafts first** — emails, Slack replies, calendar responses. Internal
  deliverables (Brief/Wrap) may earn a lighter voice-and-concision pass later; not in this phase.
- **Not the owner's own nudges.** Act-low Telegram nudges *to* the owner aren't third-party outbound —
  Critique skips them (and skips trivial one-line acks), so it never taxes the warm session.
- **Cheap by design** — a focused checklist pass, not a regeneration.

## Observability advisor (extends Trace, order 0) — ADOPTED

The **meta** advisor: it rides Trace's `out:` hook and, when Trace finalizes a substantive turn, appends
one structured metrics line to `../state/metrics.jsonl` (JSON Lines; gitignored local-first cache — schema
in `../state/README.md`). It adds no new turn phase; it's Trace, instrumented.

**Per-turn line (lean v1):** `mode`, `model` tier, `reads` (Notion/calendar reads issued), `rate_limited`
(429s), `drafts_held` (count + kinds), `actions` (act-low vs ask-high counts), **`correction`**, and
`outcome`. Skipped on the same turns Trace skips (idle chat, read-only one-offs) — no metrics without a
trace.

**The `correction` field is the point.** It records how a surfaced ask-high draft resolved:
`approved_as_is` | `edited_before_approve` | `rejected` | `null`. That's the evidence the autonomy dial
has been missing — an action surfaced ask-high and **repeatedly `approved_as_is` with zero edits** is a
graduation candidate.

**Closing the loop — Dream reads it *(the owner's call: fold into Dream, store locally)*.** A weekly
**Dream** rollup scans a rolling window of `metrics.jsonl` and, when an action clears the pattern
(N approvals, 0 `edited_before_approve`/`rejected`), drafts a **gated graduation proposal** into
`proposed-learnings.md` — surfaced to the owner, never auto-applied. This wires Observability into the
existing graduation machinery (`autonomy-policy.md` → *Proposed learnings*): the chain now generates the
evidence that moves its own Gate, but only the owner flips the dial. No new mode, no Notion writes.

## Prioritization / ranking advisor (order 5, out-only) — ADOPTED

The delivery-shaping advisor. It's **out-only** and sits just inside Trace (order 5), so it shapes the
assembled deliverable *before* Trace finalizes the surfaced-item count. Its discriminator is **pull vs
push** — is the owner *reading*, or is the assistant *interrupting*? — not the surface type *(the
owner's call)*:

- **Pull — surfaces the owner opens** (morning **Brief**, EOD **Wrap**, a **Triage** summary, a
  **Reminders status digest**): **rank, show everything.** Order by importance/urgency and lead with the
  decision, but do **not** trim — when they're reading, they want the whole picture and can scan it
  themselves. No held-back footer; completeness is the point.
- **Push — proactive interruptions the assistant initiates** (a **Watch** escalation, a proactive
  Telegram ping, a **reminder nudge**): **adaptive vital-few.** Only interrupt for the vital few —
  typically ~3, but never bury a 🚨 Critical / time-sensitive item to hit a number. Interrupting the
  owner is attention-expensive, so the push path is ruthless.

**Composes with stagger-don't-batch.** On the push path the vital-few gate decides *which* few things are
worth a proactive nudge — it **never merges** them into one buzz. The Reminders policy still fires the
chosen items **staggered, one per notification** (`reminders-policy.md`); Prioritization gates the set, the
policy owns the timing. (A Reminders *status digest* is a pull surface — show all.)

## Retrieval / Context advisor — deepened into modular RAG (order 20)

This **deepens an existing advisor** rather than adding a new order — the first upgrade of that kind. It
follows Spring's `RetrievalAugmentationAdvisor` (modular RAG) instead of the old ad-hoc "fetch what looks
relevant." The `in:` hook becomes a four-stage pipeline:

1. **Query transformation** — turn the turn's intent into **1–3 precise queries** (decompose / rewrite),
   not one guess. *"Catch me up on the website project"* → targeted queries against Projects, open
   Tasks, and Active Flags — instead of a single fuzzy search that misses.
2. **Route + retrieve** — pick the right source per query: **baked-in refs first** (`databases.md`,
   `state/context-digest.md`), then **structured** Notion queries (Tasks/Projects/Goals/Flags/People via
   `notion-query-data-sources`) for precise records, `notion-search` for fuzzy/prose, calendar via its
   MCP. Honor the rate-limit rule — **cap concurrent `notion-*` reads**, prefer one broad query over many
   narrow ones (`notion-rate-limits.md`).
3. **Rerank + compress** — rank the hits by relevance to the turn, drop the tail, and **compress to a
   tight, budget-bounded context** — only what the turn actually needs. This is the precision win.
4. **Augment + record** — hand the compressed context to Dispatch, and record *what was retrieved*
   (sources + counts) in the shared **turn-context**, so Trace/Observability log real `reads`/`rate_limited`
   and Critique/Prioritization can cite sources.

**Budget principle — deeper = more precise, not more voracious.** The whole point is *fewer, better*
reads: precise queries + rerank/compress means fetching the right three things once instead of scattershot
fetches that trip Notion's 429s. This directly serves the "conserve credits" constraint and makes
retrieval **observable** (it populates the Observability metrics that used to under-count ad-hoc reads).

**The retriever is pluggable — phase A + phase B.** Phase A's retrievers are Notion-search + baked refs.
**Phase B (✅ adopted, *free-but-local*)** adds a **local semantic index** over the assistant's prose
corpus (journal, notes, run-log) as another retriever *behind the same pipeline* — query-transform and
rerank/compress are unchanged; only the retrieve step gains a source (the `VectorStoreChatMemoryAdvisor`
analog: true semantic recall over the owner's history). The stack, all stdlib + local:

- **Embedder** — a local **Ollama** server (`nomic-embed-text`), called over localhost with `urllib`.
- **Index** — stdlib **sqlite3**, vectors as float32 blobs, brute-force cosine (`scripts/rag_common.py`).
  Lives at `../state/rag-index.sqlite` (gitignored, regenerable).
- **Scripts** — `scripts/rag_index.py` (build/update, incremental by doc hash) + `scripts/rag_query.py`
  (top-k semantic hits as JSON). Setup + the Dream-refresh flow: `scripts/RAG_SETUP.md`.
- **Refresh** — nightly, incremental, in **Dream**: it fetches new journal/notes via the Notion MCP,
  writes a JSONL, and calls `rag_index.py --local --ingest` (act-low, local only).
- **Additive, never a hard dep** — if Ollama is down or the index is empty, `rag_query.py` exits non-zero
  with `[]` and the retrieve step falls back to phase-A Notion-search. The semantic layer only *adds*.

## Router advisor — front-door model router (daemon, Watch's sibling) — ✅ ADOPTED (phase 1 shadow)

The first advisor that runs **before the chain proper** — a **front-door**. It lives in the **daemon**
(`scripts/presence.py`), not in a mode: when an inbound chat message comes off the wire, a small **local
Ollama** classifier (`qwen3.5:4b`) reads it and decides **trivial-and-safe** vs **escalate** *before the
warm Opus session spins*. It's the **same daemon-cheap-model shape as Watch** — a cheap local gate that
runs on cadence/events to avoid waking the expensive brain — applied to the inbound-chat door instead of
the comms-peek door.

**Why:** most inbound chat turns are trivial (an ack, a "what's on today", a quick recall). Routing those
to a local model would make them **instant, offline, and zero-Notion** — which also makes it a
**rate-limit mitigation**: a locally-handled turn issues no `notion-*` reads at all, so it can't contribute
to the 429s the Retrieval advisor already fights. Everything else escalates to Opus, unchanged.

**Two-phase rollout (gated, same pattern as the autonomy dial):**

- **Phase 1 — SHADOW (this build). Zero behavior change.** The classifier runs and its verdict is *only
  logged* to `../state/router-log.jsonl` (one line per inbound message: `ts`, `channel`, `text_preview`,
  `verdict`, `category`, `confidence`, `model`). **Every message still escalates to the warm session
  exactly as today** — no branching on the verdict. This is pure instrumentation: it gathers the accuracy
  evidence to review before turning anything on. It's the Observability-style move — log first, act later.
- **Phase 2 — LIVE (gated on shadow accuracy, NOT built).** Once the shadow log shows the classifier is
  reliably right on the trivial whitelist, `--router-mode live` would let the daemon *handle clearly-trivial
  turns locally* (from the cached digest / reminder-ack path) and escalate the rest. Graduating shadow→live
  is **the owner's call**, on the evidence — exactly like an ask-high → act-low autonomy graduation. A
  future **Dream** rollup can summarize router accuracy from `router-log.jsonl` to tee up that decision.

**The whitelist (what "trivial" means) — bias HARD toward escalate; abstain ⇒ escalate:**

- **trivial / `ack`** — a plain acknowledgement of a reminder/task: *"done"*, *"took care of it"*, *"did
  that"*, *"already ate"*.
- **trivial / `status`** — a schedule/todo status question answerable from the cached digest: *"what's
  next"*, *"what's on today"*, *"anything left"*.
- **trivial / `recall`** — simple factual recall from history/notes: *"when's my next dentist
  appointment"*, *"what did I say about X"*.
- **escalate / `other` (DEFAULT)** — anything drafting or outbound (email/Slack/reply/send), any triage,
  anything ambiguous or multi-step, anything ask-high, anything touching people/money/identity, and
  anything where the assistant's **voice** carries the message. **When unsure, escalate.**

**Conservative by construction.** `router.classify()` **never raises** — on Ollama unreachable, a JSON
parse failure, an unknown category, OR confidence below `ROUTER_CONF_THRESHOLD` (default 0.7), it returns
`escalate`/`other`. Escalate is always the safe direction (and in phase 1 it's a no-op anyway — everything
escalates). The call is wrapped so it can **never delay or break a chat turn**; `qwen3.5:4b` runs with
chain-of-thought disabled (~3s) so the inline classify is cheap.

**Ties-in.** Observability logs the verdicts → that log is the accuracy evidence to graduate phase 2 (same
gated machinery as the autonomy dial); local-handled turns (phase 2) are zero-Notion, so the router doubles
as a rate-limit mitigation alongside Retrieval's *fewer, better* reads; and it's the same daemon-cheap-model
pattern as Watch. Model = `qwen3.5:4b`, local Ollama, stdlib-only (`scripts/router.py`, mirrors
`rag_common.py`). Setup + how to read the log + the phase-2 plan: `scripts/ROUTER_SETUP.md`.

## Per-mode composition (the per-request `.advisors(...)`)

Each mode declares its chain as a one-liner — the default chain, plus/minus advisors:

- **Chat** — `[trace?, orientation, retrieval, dispatch, critique*, gate, prioritize*]`. Trace is
  *conditional* (idle chat and read-only questions skip it); `critique*` fires only when the turn drafts
  outbound content; `prioritize*` applies when the turn surfaces a list — **pull** (they asked) → show all,
  **push** (the assistant pinging unprompted) → vital-few.
- **Ask** — `[trace?, orientation, retrieval, dispatch, gate]`. Read-only Q&A; Critique only if it drafts
  an outbound (rare). A Q&A answer the owner asked for is a pull surface — rank, show all.
- **Brief / Wrap** — full chain **+ Prioritize (pull → rank, show all)**; Trace always on. (Critique's
  lighter internal pass is a later phase.)
- **Triage** — full chain **+ Critique** on every drafted reply **+ Prioritize (pull → rank, show all)** on
  the summary — `[trace, orientation, retrieval, dispatch, critique, gate, prioritize]`; the Gate is
  load-bearing here (every send is ask-high).
- **Reminders** — full chain **+ Prioritize**: the **status digest** is pull (rank, show all); an actual
  **nudge** is push (adaptive vital-few, fired staggered per `reminders-policy.md` — the gate never merges
  buzzes).
- **Watch** — **trimmed**: `[orientation(light), gate, prioritize(push)]`, no heavy Retrieval — it's the
  cheap peek gate; on escalation it pushes only the vital few and writes a trace.
- **Dream** — full chain **+ a `Propose-Learnings` advisor** at order 45 (`out:` step): spot patterns,
  draft gated proposals, open the PR and merge it on green (red/pending CI holds the merge for the owner)
  — never auto-apply; a merged proposal is *recorded*, not policy, until the owner approves it.

Adding a future advisor = insert a row + name it in the modes that want it. That's the extensibility
this design buys.

## Where it lives

**Adopted: Option A — Markdown-native.** This doc is the canonical chain; `SKILL.md`'s "Execution
philosophy" carries a short **"Run the Advisor Chain"** section pointing here, and each mode declares its
one-line advisor set. Zero new runtime code. Enforcement = the orchestrator following the spec each turn,
exactly as it follows the execution philosophy today. Honest fit for a Markdown-skill brain whose
reasoning is the CLI, not code.

**Deferred: Option B — code-backed rails (optional, Phase 3).** A small `seneschal/scripts/advisors.py`
would harden only the *mechanical* advisors the daemon can deterministically enforce: Trace open/close,
memory load/persist, and a **Gate assertion any outbound script calls before it fires** (`proton_send.py`,
Slack send, `respond_to_event`) — reusing existing `pending-approvals.json` / `carry-over.md`. The
*semantic* advisors (retrieval, dispatch, ask-high judgment) stay in the reasoning loop; only the rails
become code. Do this after A proves the shape.

## Phased plan

1. **Phase 1 — Spec the chain (this doc).** Adopt the Advisor Chain + shared turn-context + per-mode
   composition. Add the "Run the Advisor Chain" section to `seneschal/SKILL.md` and register this file in
   the reference index. **No behavior change** — it names and orders what the assistant already does.
2. **Phase 2 — Declare per-mode chains.** Add the one-line advisor declaration to each mode section /
   subagent `SKILL.md` (Watch trimmed, Dream extended). Mirror the persona if any voice-facing behavior
   shifts (it shouldn't).
3. **Phase 3 — Harden the mechanical rails (Option B, optional).** `seneschal/scripts/advisors.py`: Trace
   open/close, memory load/persist, the pre-send Gate assertion. Wire the existing send scripts to call
   it. Covered by the CI `py_compile` check.
4. **Phase 4 — The advisor-upgrades arc** *(order set by the owner)*. Spring AI was the first; the next
   three, in order, each its own PR (merge on green):
   1. **Critique / self-review advisor** — ✅ *adopted* (order 35; spec above).
   2. **Observability advisor** — ✅ *adopted* (extends Trace, order 0; spec above). Emits per-turn
      metrics to `../state/metrics.jsonl`; a weekly Dream rollup turns the `correction` history into gated
      graduation proposals. Closes the loop — the chain informs its own dial, the owner still flips it.
   3. **Prioritization / ranking advisor** — ✅ *adopted* (order 5, out-only; spec above). Pull → rank,
      show all; push → adaptive vital-few. **Initial arc complete.**

5. **Phase 5 — Deepen Retrieval into modular RAG** *(the owner's call: A then B, free-but-local)*. The
   first *deepening* of an existing advisor rather than a new one.
   1. **A — modular pipeline** (query-transform → retrieve → rerank + compress → augment) over the current
      retrievers (Notion-search + baked refs). Markdown-native, ships now; payoff is precision + budget.
      ✅ *adopted* (spec above).
   2. **B — local semantic index** over the prose corpus (journal, notes, run-log) as a new retriever
      behind A's pipeline. ✅ *adopted* — free-but-local: Ollama (`nomic-embed-text`) + stdlib sqlite3
      brute-force cosine; corpus = journal + notes + run-log; incremental nightly refresh in Dream. The
      first advisor with real code (`scripts/rag_common.py` / `rag_index.py` / `rag_query.py`,
      `RAG_SETUP.md`); additive (falls back to Notion-search when unavailable).

6. **Phase 6 — Front-door Router (model routing)** *(model = `qwen3.5:4b`, shadow-first — the owner's
   call)*. A **front-door** advisor in the daemon (Watch's sibling), classifying inbound chat *trivial*
   vs *escalate* before the warm Opus session spins.
   1. **Phase 1 — shadow.** ✅ *adopted* (spec above). The classifier runs and only **logs** its verdict to
      `../state/router-log.jsonl`; **zero behavior change** — everything still escalates. Gathers accuracy
      evidence. First code: `scripts/router.py` (stdlib + local Ollama, mirrors `rag_common.py`),
      `scripts/ROUTER_SETUP.md`, `presence.py --router-mode shadow`.
   2. **Phase 2 — live (local handling), gated on shadow accuracy.** Handle clearly-trivial turns locally
      (instant/offline/zero-Notion), escalate the rest. **Not built** — graduation is the owner's call on
      the shadow evidence, same pattern as the autonomy dial; a Dream rollup can summarize router accuracy
      to tee it up.

   Other future advisors slot in the same way (a row + per-mode declaration): a dedicated Privacy advisor
   split out of Critique, a Cost-Guard advisor. Open when the owner wants them.

## Decisions — resolved

1. **Scope:** Option A (Markdown-native), Phases 1–2, in one PR. ✅ *(The owner's call.)*
2. **Rollout:** normal branch → PR → **the owner merges**; Phase 3 (code rails) is a later follow-up. ✅
