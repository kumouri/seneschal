# The assistant's local-first memory — run log, carry-over, context digest

How the assistant remembers across runs. Three runtime files hold its local memory. They are
**local-first caches**: **the active store is the system of record** (the Run Log domain + the carry-over
handoff field), and these files are the cheap on-disk copies the modes read/write between runs.

## The three files (live data lives in `state/`, gitignored)

| Concern | Live file (gitignored) | Tracked seed | System of record |
|---------|------------------------|--------------|------------------|
| Run history | `../state/run-log.md` | `../state/run-log.example.md` | Run Log domain of the active store (Notion backend: `collection://…0008`) |
| Open loops / held approvals | `../state/carry-over.md` | `../state/carry-over.example.md` | Run Log `Carry-Over Context` field |
| Nightly digest (cache) | `../state/context-digest.md` | `../state/context-digest.example.md` | — (regenerable cache; safe to lose) |

**Why they live in `state/`, not here.** The daemon runs off `main` and writes these files every few
minutes. Keeping them gitignored (like `reminders.json`) means a `git pull --ff-only` on the daemon's
checkout never conflicts on an append log — that's what lets the live brain track `main`. It also ends
the old failure mode where a tracked-but-uncommitted `run-log.md` history could get reverted to a stub
under fsmonitor + concurrent-`claude` load.

**Bootstrap-on-absence.** On a fresh checkout the live files don't exist. Any mode may create one by
copying its `*.example.md` seed (e.g. `state/run-log.example.md` → `state/run-log.md`); a missing
`context-digest.md` just means the Brief leans on the store until the next Dream regenerates it. Never
treat "the file is missing" as an error — the durable copy is the store.

---

## Run log — the incremental trace

Mirrors the journal-steward's Agent Run Log protocol. Purpose: a substantive assistant run leaves a
durable trace, so a cut-short run still recorded what it did, and the owner can see the assistant's work
over time.

**When to write:** scheduled/substantive runs that *act* or deliver a pushed briefing (Brief push, Wrap,
Triage sweeps that hold drafts). **Skip:** one-off interactive questions (Ask mode) and a quick
interactive in-chat Brief.

**Protocol (in store verbs — the backend's `mapping.md` resolves each):**
1. **Read prior context (Phase 0).** `store-query` the last 3–5 Run Log rows by run date desc; read
   `carry_over_context` and `issues_uncertainties` to avoid repeating work and to follow up. (The
   `state/run-log.md` mirror is a cheap local pre-read; the store is authoritative.)
2. **`store-create` the row early** (after the mode's critical path) with `status: partial`,
   `actions_summary: "in progress…"`, `mode` set.
3. **`store-append` to the record body after each phase** — a running trace (don't overwrite).
4. **`store-update` to finalize at the end:** set counts (`items_surfaced`, `actions_taken`,
   `drafts_held`), `status` (success/partial/failed), and write `actions_summary` + `carry_over_context`
   + `issues_uncertainties`.
5. **Mirror the entry** into `../state/run-log.md` (append a one-paragraph trace ending with the store
   record's ref/id). Seed the file from `../state/run-log.example.md` if it's missing.

On the **Notion backend**, these resolve to the journal-steward's Notion write mechanics (date format,
body append, exact/emoji-bearing status strings) — `../store/notion/mapping.md` +
`../../subagents/journal-steward/daily-journal-steward/references/notion-mcp-mapping.md`.

---

## Carry-over — the assistant's running "open loops"

Purpose: nothing the assistant is holding for the owner silently falls through. Distinct from the
journal's task carry-over, it tracks **the assistant's** open loops:

- **Held drafts** awaiting approval (email/Slack replies, calendar responses it proposed).
- **Unanswered calendar invites** it flagged.
- **Follow-ups** it promised ("I'll check back on X tomorrow").
- **Active/Carrying-Over Important Flags** worth keeping in view (read from the store, not duplicated).

**Rules:** rebuild from current state each run (resolved items drop off; new ones appear); order by
urgency, group by kind (approvals, invites, follow-ups, flags); a held draft stays until the owner
approves/sends or explicitly drops it. It lives in two interlinked surfaces: the Run Log
`Carry-Over Context` field (machine-readable handoff to the next run) and the local
`../state/carry-over.md` running log.

### Held approvals — the local loop (chat / Telegram / Notion)

A held draft is mirrored to **`../state/pending-approvals.json`** (the local cache the sentinel/Watch read)
as well as the Notion carry-over (system of record). Each held item gets a **stable approval id** so a
reply can refer to it.

**Holding (when the assistant drafts something ask-high):**
1. Write the draft to its channel store **where applicable** (Proton/Gmail draft). **Slack has no channel
   store** — the held entry's stored text is the single copy, sent verbatim on approve (a Slack draft
   would be a second mutable copy with no reliable cleanup on reject; see the Slack spec, Q5).
2. Append an entry to `pending-approvals.json` and the Notion carry-over with: `id`, `kind`
   (`email` | `slack` | `calendar_response` | `notion_write` | `archon` — a Forge lifecycle action or
   delegation, see `archons.md`), `channelRef`, `summary`, `bodyPreview`, `created_at`,
   `status: "pending"`. **Slack drafts add:** `body` (the verbatim send text — the assistant always
   signs; the unsigned/as-the-owner variant was deferred), `sources` (the derivation-contract citations),
   `critique_note`, and `thread_seen_ts` (the newest thread message at draft time — powers the freshness
   re-check). Schema: `../state/README.md`.
3. **Surface it** to the owner on whatever surface fits — in chat (if they're live), and/or a Telegram
   push (*"Drafted a reply to Alex — reply `send a3` or `drop a3`."*), and/or a Notion comment. Use the
   short id so a one-word reply is unambiguous.

**Detecting the owner's decision:**
- **Chat / Telegram:** the sentinel pulls the reply into the inbox; Watch/Chat reads intent — `send`/`yes`/
  `approve` (+ id, or the most recent if only one is pending) → **approve**; `drop`/`no`/`reject` →
  **reject**. A **Slack** draft is a single signed body (the unsigned variant was deferred), so
  `send a7` is unambiguous. `edit a7: <text>` / *"change a7 to say…"* re-holds under the same id
  (`edited_before_approve`). Ambiguous → ask, don't guess.
- **Notion:** Watch checks for new comments on the carry-over / pending-approvals surface and reads the
  same intent. (This is an LLM-tier read — the sentinel doesn't parse Notion.)

**Executing (reuse the `approve_draft` / `reject_draft` path in `SKILL.md`):**
- **Approve →** send/execute via the normal local path (email → `../scripts/proton_send.py` without
  `--dry-run`; Slack → **freshness re-check** the thread since `thread_seen_ts`, then `slack_send_message`
  the stored **`body` verbatim**; calendar → `respond_to_event`). Set the entry `status: "sent"`.
- **Reject →** discard the draft; set `status: "rejected"`. (A Slack reject has nothing to clean up — no
  channel-side copy was written.)
- Either way, **remove it from the open carry-over** and leave a Run Log trace. A failed send →
  `status: "failed"`, kept in carry-over so it isn't lost (**no automatic Slack retry** — re-read the
  channel to see if it landed, report, and let a fresh `send` re-attempt).
- **Slack-hands gap** — a session that *understands* a `send` but lacks Slack tools records
  `status: "approved"` (approved-but-unsent, kept in carry-over) and says so; the next Slack-capable turn
  drains `approved` entries first, re-running the freshness re-check. Closed by wiring the daemon's
  `slack-mcp.json` (`../scripts/SLACK_MCP_SETUP.md`, Q9).

`pending-approvals.json` schema is documented in `../state/README.md`.

---

## Context digest — the nightly orientation cache

**Dream mode** (nightly wind-down) **overwrites** `../state/context-digest.md` with a condensed
view of the day — *yesterday in one breath, open loops, what's queued for today, active flags, pending
reminders* — so the next morning's **Brief** can orient cheaply without re-querying everything.

- **Dream writes** `state/context-digest.md` (overwrite each run).
- **Brief reads** it first, at Phase 0, before any store query.
- **If missing**, fall back to carry-over + the store; the next Dream regenerates it. Keep it short —
  cheap orientation, not a transcript.

---

## Durable act-low writes — the write-behind outbox (Notion backend)

The run-log and carry-over above are how the assistant remembers *across runs*; the **outbox** is how
its **act-low Notion-backend writes don't get lost** *within* the failure window. An ack (⏰ row →
`Status`/`Last Acknowledged`), a med-intake row, and (phase 2) a run-log finalize are **journaled to a
durable local store first** — `../state/notion-outbox.sqlite`, via `../scripts/outbox.py` — **then
flushed to Notion** by the LLM turn via MCP, retried until they land. This closes the chat→store
ack write-through gap (acks that reached `state/acks.json` but never flipped the store row, silently
desyncing the system of record).

- **Journal-first is the guarantee.** Once `outbox.py ack …` (or `medlog …`) returns `ok`, the write
  *will* land even across a reboot — so the assistant may honestly tell the owner "recorded" the
  instant it's journaled, superseding the old **manual "park it in carry-over to record next run"**
  step *for those writes*. Carry-over parking remains the fallback for writes the outbox doesn't cover.
- **Belt-and-suspenders.** The happy path still does the direct MCP write in-turn (zero added latency);
  the outbox only earns its keep when that write fails. Enqueue is idempotent, so the double-write is a
  no-op — see the ack flow in `SKILL.md` (Chat mode) and the schema/lifecycle in `../state/README.md`.
- **Fail-closed**, the mirror of `acks.json`'s fail-open gate: an entry retries until Notion confirms,
  then dead-letters (surfaced, never dropped). Full design + the flush (a)/(b) fork:
  `../docs/notion-write-behind-outbox-spec.md`. Filesystem backends write locally and atomically, so
  their act-low writes never route through the outbox (see the spec's backend-scope section).

## Session distillations — the mini-dream (cross-instance memory, phase 2b)

The registry (`state/sessions/`) says who's live *now*; the **mini-dream** is how sessions that already
ended stay part of the assistant's memory (an LSM-tree analogy): a **fast append path** per session, and
a **slow compaction path** nightly.

- **Fast path (append).** On every SessionEnd — any Claude Code session on the machine, via the global
  `session_stamp.py` hook — `scripts/mini_dream.py` distills the transcript into one JSON line appended
  to `state/session-distillations.jsonl`, **anchored to the assistant's home repo no matter what project
  the session ran in**. Salience-laddered: trivial sessions leave no record; small ones get a free
  deterministic distillate; substantial ones (≥ 6 real user turns) get a headless LLM distill
  (`claude -p`, cheap model, subscription-billed with `ANTHROPIC_API_KEY` scrubbed) that falls back to
  deterministic on any failure. Idempotent per session id; a distiller's own session never dreams itself
  (`SENESCHAL_MINI_DREAM`).
- **Read at orientation.** Every assistant surface (the `/assistant` grounding, the Orientation advisor)
  tails the last ~5 records — "what did the other instances do lately?" — and folds anything relevant in.
- **Slow path (compaction, Dream step 2b).** Nightly, Dream ingests new distillates into the local RAG
  index (`{"source": "session-distillation", "ref": <id>, "text": …}`) and prunes the log
  (`mini_dream.py --prune-days 30`) — recent context is a cheap tail read, old context is semantic
  recall from the index, and the log never accretes. Schema + ladder details: `../state/README.md`.
