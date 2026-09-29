# The session trace — an agent debug log for the cockpit

**Status:** `PARTIAL(phase 0's readers and daemon-side join keys + phase 1's panel and session-aware trim BUILT; phases 2-4 designed, not built)` —
the readers ship: `seneschal/scripts/trace.py` (the phase-0 CLI join) and `cockpit/server/trace.py`
+ `TracePanel.tsx` (the phase-1 panel, with the §10 layout, the §11 turn grouping and the §11.1
presentation fixes). What they join is **written by the daemon** (`presence.py`): `session_id` on
transcript rows, the `decision` → `opened` pair in `session-starts.jsonl`, and the per-turn
`metrics.jsonl` rows (`writer: "daemon"`, `session_id`, `turn_id`) the sessions list is built from. The
transcript ring's session-aware trim (§9.3, `cockpit_pipe.py`) buckets by that `session_id`. Every
reader still degrades to less data — never an error — on rows that predate the stamp (§6). §9's three decisions are closed, so no phase is gated on a decision.
**Two later decisions bind anyone editing the panel: §10 — the Trace is NOT a grid tile**, it is a
collapsible section under the chat pane, and anything that re-places a panel like it queries the
**container**, never the window; **§11 — the TURN is the unit of the reader, not the event.** An edit
that quietly undoes either is a regression.

Prompted by GitHub Copilot's **Agent Debug Log** panel in VS Code, which does for a Copilot chat
session what nothing did for an assistant session: shows the whole session as an inspectable,
chronological event log after the fact.

---

## 1. What Copilot actually built

Researched from the VS Code docs, not from memory. Four surfaces, and it is worth being precise about
which is which, because they solve different problems:

| Surface | What it shows |
|---|---|
| **Agent Debug Log — Logs view** | Every event in a session, chronologically: timestamp, event name, summary. Toggles between **List** (flat) and **Tree** (grouped by sub-agent). A **filter bar** narrows to event types. Expanding a row gives the full payload — for a tool call, the exact input payload and the returned output; for an LLM request, the full system prompt that was sent |
| **Agent Debug Log — Summary view** | Session aggregates: model turns, tool calls, total tokens, errors, total events |
| **Agent Debug Log — Agent Flow Chart** | The event sequence as a navigable diagram |
| **Chat Debug view** (separate) | Per *request* rather than per session: system prompt, user prompt, context, tool responses — four expandable sections |

Three design decisions in it are worth stealing outright:

1. **A session opens with a customization cluster** — `Load Instructions`, `Load Agents`, `Load
   Hooks`, `Load Skills`, each expandable to the file paths searched and what was found. The question
   "why did it behave that way" usually resolves to "because of what it loaded," and Copilot puts that
   first, before the first model turn.
2. **File logging is opt-in and off by default.** The docs give the reason in one line: logs are
   stored locally on disk, so keep that in mind if sessions include sensitive context. Persisting a
   session is a privacy decision, and they made it a decision rather than a default.
3. **Export is OTLP JSON**, with an import warning above 50 MB. They picked a boring, existing
   observability format instead of inventing one, and discovered the size problem in practice.

**What is deliberately NOT being copied**: the Agent Flow Chart (§8), and Copilot's separation of a
per-session log from a per-request Chat Debug view — the warm session's turns are strictly
serialized, so one view with a turn filter is the same thing with less to build.

---

## 2. What the daemon already emits

This is the surprising part and it reframes the whole job. **The daemon already produces most of the
events Copilot's panel shows. Almost nothing joins them, and one of them is thrown away.**

| Signal | File | Joinable by |
|---|---|---|
| Turn boundaries + assistant output | `warm-transcript.jsonl` (a capped ring) | `turn_id` |
| **Tool calls** | same, as a `tool_uses` array on `assistant_output` | `turn_id` |
| Per-turn cost/tokens/context/duration | `metrics.jsonl` | `turn_id`, `session_id` |
| Spend by token class | `governor-ledger.jsonl` | `turn_id` (once `spend-levers-spec.md` phase 0 is wired) |
| What was said, both sides, verbatim | `turns.jsonl` | `turn_id` |
| What the assistant said, per send site | `assertions.jsonl` | — |
| Why each session spawned resumed or not | `session-starts.jsonl` | — |
| **Which instruction files loaded** | `instructions-loaded.jsonl` | — |
| Per-session distillate | `session-distillations.jsonl` | `session_id` |
| Errors, wind-downs, respawn reasons | `presence.log` | — |

All of these live in the gitignored `seneschal/state/`. In a busy install tool calls are the bulk of
the transcript — roughly three of every four assistant-output rows carry at least one — which is the
measurement that makes a filter a requirement rather than a nicety (§4).

### 2.1 The two findings that decide the design

**(a) There is no session-level join key.** Transcript rows carry `turn_id` and never `session_id`.
`metrics.jsonl` carries both. `assertions.jsonl`, `session-starts.jsonl` and
`instructions-loaded.jsonl` carry neither transcript key. So the question *"show me everything that
happened in session X"* — the entire premise of an agent debug log — **cannot be answered at any
cost**, not slowly, not by a script. This is the shape of the `spend-levers-spec.md` phase-0 finding
(two rows written back to back by the same function sharing no key), one level up.

**(b) Tool *results* are captured nowhere.** `build_chat_event_from_stream` turns each `assistant`
block into an `assistant_output` carrying `tool_uses` — `name` plus a **200-character
`input_preview`** — and drops the CLI's `user`/tool-result events as uninteresting. No transcript row
anywhere carries a tool result. So the panel's single most useful affordance — the exact input payload
and the returned output — has one half that exists truncated and one half that has never existed. A
question like *"what did that `Bash` call actually print when it went wrong?"* is unanswerable after
the turn scrolls past.

Two smaller gaps, both real: the **system prompt is never logged** (the grounding is assembled in
code per spawn and its bytes are not recorded, so "what was the session actually told this time" is
reconstructed from source, not read), and **errors are prose** in `presence.log` rather than typed
events.

---

## 3. The thesis

**This is a join and a view, not an instrumentation project.** Most of the events are already being
written; they are split across nine files, keyed inconsistently, and read by nothing. The work in
order of value is:

1. Give every row a `session_id` (phase 0 — cheap, no behaviour change, unblocks everything).
2. Build the view over what already exists (phase 1 — no new capture at all).
3. Capture the two things genuinely missing — tool results and the system prompt — **behind an
   explicit opt-in**, because that is where the size and privacy cost is (phases 2 and 3).

Doing it in that order means the expensive, sensitive capture is designed against a UI that already
works and a measured row count, rather than guessed at first.

---

## 4. Scope — the Trace panel

A cockpit section, `TracePanel.tsx` + `cockpit/server/trace.py`, read-only, backed by
`GET /api/trace/sessions` and `GET /api/trace/sessions/{id}`.

**Sessions list.** One row per warm session, newest first: start time, end time, why it ended
(`last_respawn_reason`), whether it resumed or which refusal class refused it
(`session-starts.jsonl`), turns served, tool calls, total cost, context peak, error count. Buildable
from `metrics.jsonl` + `session-starts.jsonl` alone — deliberately **not** from the transcript, which
is a capped ring, so a session that scrolled out of it would vanish from the list.

**Session detail — the Logs view.** Chronological, flat, one row per event: `ts`, event kind, a
one-line summary. Copilot's List/Tree toggle collapses to a **turn grouping toggle**, since the warm
session has no sub-agents. A **filter bar** by kind is not optional — at the volume in §2 a flat
unfiltered list is unusable.

**The customization cluster, first.** Copilot's best idea, and it can be served from
`instructions-loaded.jsonl` once that file carries the warm session's id: which `CLAUDE.md` files
loaded, with what `load_reason`, in what order. With the grounding split into a mode router and
sub-routers, this becomes the instrument that shows whether a sub-router or mode file ever actually
loaded — the live half of `grounding-restructure-spec.md` §7.1's check, answered by a panel instead of
a grep.

**Summary view.** Turns, tool calls by name, tokens by class (the governor's `billable_tokens` basis,
named as such), cost, errors, wall-clock. Every figure already exists.

**Expandable rows.** Phase 1 shows what is there: the tool input (full once phase 2 lifts the
200-char cap), the verbatim text of both sides from `turns.jsonl`, the metrics row. Tool output
appears only if phase 3 is enabled.

---

## 5. Phases

| Phase | What lands | Behaviour change | State on this branch |
|---|---|---|---|
| **0** | `session_id` on transcript rows and assertions; a `decision` → `opened` row pair in `session-starts.jsonl` closing the id a spawn record cannot know; `seneschal/scripts/trace.py` joining them. **No UI.** `instructions-loaded.jsonl` already carries an id — the hook payload's, a different namespace (§4) | **None** — additive fields only | **The reader is BUILT** (`trace.py sessions` / `show <id>`, tolerant of every missing file). **The writer-side fields are not yet wired** — they live in `presence.py` and the pipe's transcript writer |
| **1** | `cockpit/server/trace.py` + the two routes; `TracePanel.tsx` — sessions list, **turn grouping with the per-turn roll-up**, filtered + **searchable** Logs view, tool calls **with inline input**, `turns.jsonl` text (§9.2) **anchored to the turn's boundary rows** with the tombstone honoured **in the reader**; transcript retention as last-N whole sessions (§9.3). The customization cluster waits on `instructions-loaded.jsonl` gaining the warm session's id | Read-only panel; nothing new captured | **The panel and its reader are BUILT**, with reader, route and DOM-free `traceView` tests. **§9.3's session-aware trim is BUILT** in `cockpit_pipe.py` but takes effect only once transcript rows carry `session_id`; until then the row cap governs |
| **2** | Tool **input** uncapped, and the system prompt recorded once per spawn | Bigger transcript rows | Not built. Gate: a measured size delta over a week of phase 1 |
| **3** | Tool **results** captured — **all of them**, 7-day retention, per-result size cap, **off by default** (§9.1) | The one genuinely new capture | Not built. Gate: a measured size delta from phase 2; the `!private` tombstone honoured in the reader |
| **4** | Export (one session → a file) | — | Not built, and only worth building if a session ever needs handing to someone |

**Phase 0 is worth landing alone**, exactly as `spend-levers-spec.md` phase 0 is: it is one field, it
makes a currently-unanswerable question answerable, and every later phase is blocked on it.

---

## 6. Why not just read the files

Because they are not readable as a session. To answer *"what happened in the session that died at
03:14?"* by hand: find its `session_id` in `metrics.jsonl`; take the timestamp range; grep
`warm-transcript.jsonl` for `turn_id`s that fall inside it (there is no direct link); hope the ring
buffer has not rolled past it; accept that tool results do not exist; read `presence.log` prose for the
error. That is not a workflow, it is an archaeology dig, and it is why nobody does it.

**Tolerance is load-bearing here rather than polite.** A trace is opened precisely when something has
gone wrong, which is exactly when its inputs are most likely to be half-written. A reader that raised
on a corrupt line would be useless in the only situation it exists for — so every reader in this spec
parses per line, skips what it cannot read, and reports less rather than failing.

---

## 7. The risk this carries, named

**A trace panel is a surveillance surface pointed at the owner's own conversations.** `turns.jsonl` is
verbatim and uncapped; tool inputs contain file paths, store page contents and email bodies; tool
*results* would contain far more. Copilot's answer — off by default, one line in the docs about
sensitive context — is the right shape and this spec adopts it: **phase 3 ships disabled**, and the
panel is localhost-only behind the existing cockpit session like every other cockpit surface.

The `!private` tombstone in `turns.jsonl` must be honoured by the trace reader, not just by the
writer. A redaction that only holds in one of two readers is not a redaction.

---

## 8. Non-goals

- **The Agent Flow Chart.** A pretty diagram of a strictly-serialized turn sequence is a list with
  extra steps. If sub-agents ever run inside a warm turn, revisit.
- **A separate Chat Debug view.** One view with a turn filter covers it; Copilot needs two because
  its requests interleave and the warm session's do not.
- **Live streaming.** The chat pane already streams. This is for after the fact, which is the gap.
- **OTLP.** Copilot exports OTLP because it lives in an ecosystem that reads it. This install does
  not, and a format nothing consumes is ceremony. Phase 4, if it happens, writes the same JSONL the
  reader already parses.
- **Tracing anything but the warm session.** Desktop `/assistant` and build sessions have their own
  transcripts on disk already, and the machine-wide session hook distills them nightly.

---

## 9. Decisions — all three closed

1. **Tool results: capture ALL of them, 7-day retention, off by default.** The rejected alternative
   is kept here because its reasoning matters for phase 3: persisting results only for turns that
   errored would have buffered results in memory and written them on failure, cutting volume and
   exposure to near zero. **The case against it is that "the turn errored" is a narrow proxy for
   "something went wrong."** A turn that completes cleanly can still have read the wrong file, written
   to the wrong path, or returned stale data that only looks wrong three turns later — precisely the
   cases where the successful call's output is the evidence, and precisely the cases error-only
   would have thrown away. So: full capture, with the volume controlled by **retention (7 days), a
   per-result size cap, and off-by-default** rather than by guessing in advance which results will
   matter.
2. **The trace panel shows `turns.jsonl` text.** The panel is localhost-only behind the existing
   cockpit session, the data is already on disk, and what was said is the primary input to behaviour
   — a trace showing tool calls but not the conversation that caused them is much worse at its one
   job. §7's rule is therefore load-bearing: **the `!private` tombstone must be honoured by this
   reader, not only by the writer.**
3. **Transcript retention becomes last-N whole sessions.** The retention unit then matches the view's
   unit: you either have a session or you do not, never half of one, and a quiet week stops being
   evicted by a busy day. It depends on phase 0's `session_id`, so it lands with phase 1 rather than
   needing its own phase — and it replaces a row cap, so an old session can no longer be *present but
   truncated*, which reads as "nothing happened" rather than "we dropped it".

---

## 10. Laid out properly — out of the tile grid

Phase 1 first shipped the panel into the dashboard's tile grid, and it was unreadable there. Three
faults, stacked, each measured in a live cockpit before anything was changed:

1. **A two-pane log reader does not fit a tile.** `.grid` is `repeat(auto-fill, minmax(320px, 1fr))`,
   so Trace got ~346px. The sessions pane took 240 of it, leaving the detail pane ~94px — and the
   event row's fixed `ts`/`src`/`kind` columns want 220px before the content column gets anything. The
   content column therefore resolved to **literally 0px**: every tool name and every line of
   transcript text rendered into a zero-width box.
2. **The responsive breakpoint asked the wrong element.** `@media (max-width: 720px)` stacks the
   panes — but the *viewport* was 1280px while the *container* was 346px, so it never fired. A panel
   that can be re-placed must measure the box it is in, not the window it is in. It is now a
   `@container` query, which cannot make that mistake.
3. **The inner panes did not scroll** once given room — the grid row sized to its content (tens of
   thousands of pixels of event log) and the panel's `overflow: hidden` silently clipped all but the
   first screenful, with no scrollbar to reach the rest. Fixed with `grid-template-rows: minmax(0,
   1fr)` and a `min-height: 0` chain from panel to pane.

**The decision:** the Trace panel leaves the grid and becomes a **collapsible section under the chat
pane, sized like it** (`min-height: 420px; max-height: 70vh`). Collapsed on first visit — a debug log
should not push every dashboard tile below the fold on every load — and the choice persists in
`localStorage` (via `collapseStorage.ts`), so leaving it open keeps it open.

**And a correctness bug found while measuring.** `read_sessions` counted an error whenever `outcome
not in (None, "ok", "success")`, but the daemon writes it **capitalised** — `"Success"` / `"Failed"`.
So **every successful turn counted as an error**, and a quiet session rendered a wall of red. Now
case-folded against a named `_OK_OUTCOMES` set, and pinned by a test that was confirmed to fail
against the old predicate rather than merely to pass against the new one.

## 11. The turn is the unit

§10 made the panel wide enough to read. This made it worth reading: most entries showed the same two
blocks because every row repeated the full exchange.

**It was the join, not the capture.** `turns.jsonl` is clean — exactly **2 rows per turn**, one per
side. But a turn is **one exchange and many transcript rows** — one per step — and `read_session`
stapled the turn's whole text onto *every* one of them. A busy turn runs to ~68 rows (1
`turn_started`, 66 `assistant_output`, 1 `turn_done`), so the exchange rendered 68 times and over half
of a session payload was duplicate text.

**The fix — `_text_anchors()`.** Each side's words land on the row where they actually happened: the
owner's on `turn_started`, the assistant's on `turn_done`. The middle rows keep their own tool calls
and nothing else, which is what they are. **The fallbacks are the load-bearing part**: a truncated
turn may have no boundary rows at all, so the head and tail stand in, and a one-row turn still carries
both sides. Measured after: roughly a third of the payload, zero duplicate segments.

**Redaction moved carefully.** The `!private` *marker* now rides the anchors rather than every row —
but *suppression* stayed unconditional: no row of a redacted turn carries a preview, anchor or not.
That split is pinned by its own test, because it is the half where a mistake leaks.

**Three enhancements shipped alongside:**

1. **Turn grouping** — §4's List/Tree toggle, built as `traceView.groupIntoTurns()`. Collapsed, a turn
   block *is* the exchange plus what it cost (duration · cost · tool count · tokens); expanded, it is
   the steps. Grouping is by **consecutive** runs, never a group-by: merging non-adjacent runs would
   reorder a chronological trace to make its grouping tidier, and that is the one thing a trace must
   not do.
2. **The roll-up figures were already being sent and thrown away.** `turn_done` carries
   `duration_ms`, `total_cost_usd` and `usage`; the panel had rendered the bare word `turn_done`. The
   token figure is **titled**, because it is every token *processed* — a multi-step turn re-reads its
   cached context each step, so it runs to millions beside a session context peak in the hundreds of
   thousands, and two unlabelled token numbers differing by 6× just look like one of them is broken.
3. **Tool input renders inline**, not hover-only, and **free-text search** narrows the log. Any active
   narrowing **auto-expands** the turns — a filter that hid its own results behind a second click
   would be worse than no filter.

**The frontend also de-duplicates defensively** (`pushUnique`). The backend is the fix; but the turn
header is built by concatenating its events, so a header that could print one sentence 68 times if a
duplicate ever arrived from anywhere would be a worse bug than the one it replaced.

**Search is held to the tombstone too**: a redacted event contributes nothing to the haystack, so
search can never become the one reader that reconstructs what §7 removed.

## 11.1 Four presentation fixes

All in `cockpit/web` (plus one reader field) — none of them touches §10's or §11's decisions, and none
changes what the backend captures.

1. **Resizable.** The sessions/detail split was a fixed `minmax(200px, 260px)`; the only way to read a
   long session title was to hover for the native tooltip. It is now a drag-resizable column —
   `traceResize.ts` owns the clamping (never past the pane's own bounds, never so wide it starves the
   detail pane), `TracePanel.tsx` owns the pointer drag, and the width persists via
   `collapseStorage.ts`'s numeric read/write pair.
2. **Untruncated content.** `traceView.toolPreview` cut a tool's input to 90 characters and the CSS
   ellipsized even that — quietly contradicting §11's "tool input renders inline". The client-side cap
   is gone (the backend's own 200-char preview in `cockpit_pipe.py` is the only one left, and raising
   *that* is phase 2's job); the CSS wraps instead of hiding behind a hover.
3. **Sortable.** `traceView.sortSessions` adds cost/turns/errors, sorting a **copy** of the
   already-fetched page client-side — the backend's own newest-first order is never mutated.
4. **Useful session titles.** The label was eight hex characters of the session id, which named
   nothing. `cockpit/server/trace.py`'s `read_sessions` now derives a `title` from the first thing the
   owner said in the session, reusing the `_turn_text` join `read_session` already does. **The
   `!private` tombstone is honoured here too** — a redacted first turn yields no title, the same as it
   yields no text anywhere else in this panel.

## 12. Related

- `grounding-restructure-spec.md` §7.1 — the customization cluster is an instrument for that live check.
- `spend-levers-spec.md` — the same join problem one level down; its phase 0 is the `turn_id` this
  spec's phase 0 extends to sessions.
- `cockpit-spec.md` — the panel conventions, the tolerant-reader rule, and the auth model this
  inherits; `../../cockpit/CLAUDE.md` holds the panel decisions a tidy-up could undo.

## Sources

- [Debugging agent work — VS Code](https://code.visualstudio.com/learn/foundations/debugging-and-whats-happening-behind-the-scenes)
- [Debug chat interactions — VS Code](https://code.visualstudio.com/docs/agents/agent-troubleshooting/chat-debug-view)

## Router entry

**What it decides:** an agent-debug-log view in the cockpit, after GitHub Copilot's. **A join and a
view, not instrumentation** — the events are already written across nine files and read by nothing.
The readers and the panel ship; the daemon-side join keys are the remaining wiring.
