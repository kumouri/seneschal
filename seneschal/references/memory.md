# The assistant's local-first memory — run log, carry-over

How the assistant remembers across runs. Two runtime files hold its local memory. They are
**local-first caches**: **the active store is the system of record** (the Run Log domain + the carry-over
handoff field), and these files are the cheap on-disk copies the modes read/write between runs. **A
third, `state/context-digest.md`, used to hold a nightly LLM-written orientation cache here too; it is
RETIRED** — see "Context digest — RETIRED" below for where its jobs went.

## The two files (live data lives in `state/`, gitignored)

| Concern | Live file (gitignored) | Tracked seed | System of record |
|---------|------------------------|--------------|------------------|
| Run history | `../state/run-log.md` | `../state/run-log.example.md` | Run Log domain of the active store (Notion backend: `collection://…0008`) |
| Open loops / held approvals | `../state/carry-over.md` | `../state/carry-over.example.md` | Run Log `Carry-Over Context` field |

**Why they live in `state/`, not here.** The daemon runs off `main` and writes these files every few
minutes. Keeping them gitignored (like `reminders.json`) means a `git pull --ff-only` on the daemon's
checkout never conflicts on an append log — that's what lets the live brain track `main`. It also ends
the old failure mode where a tracked-but-uncommitted `run-log.md` history could get reverted to a stub
under fsmonitor + concurrent-`claude` load.

**Bootstrap-on-absence.** On a fresh checkout the live files don't exist. Any mode may create one by
copying its `*.example.md` seed (e.g. `state/run-log.example.md` → `state/run-log.md`). Never treat
"the file is missing" as an error — the durable copy is the store.

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

### Writing these files — use the helper, never a one-liner

**Every write to a memory file or a `state/*.json` goes through `../scripts/memory_write.py`.** Not a
convention: a rule, with real data loss behind it.

```bash
python ../scripts/memory_write.py append  ../state/run-log.md    < new-entry.md
python ../scripts/carryover_region.py write-region --name WRAP_SEED < rebuilt.md
python ../scripts/memory_write.py prepend ../state/run-log.md    < newest.md
```

**`carry-over.md` is the one exception to calling `memory_write.py` directly.** It has exactly two
writers (`../docs/carry-over-region-spec.md`), each owning one bounded, marked region and using
`memory_write.write_text` underneath rather than being called against the whole file: the Wrap's
nightly snapshot goes through `carryover_region.py write-region --name WRAP_SEED` (above), and the
open-loops projection goes through `loops.py render --write` (below). Composing "tonight's content +
the entire existing file" and handing that to `memory_write.py write` is indistinguishable at the
byte level from a prepend — the file grows every night and nothing ever prunes it.
`carryover_region.py check` reports the file's shape (each region present/well-formed, the
hand-written head's line count against a tripwire) without writing anything.

**Never `open(path, "w")`.** `open(w)` truncates the file *first*; if the write then raises (e.g. a
`UnicodeEncodeError` on an emoji under Windows's non-UTF-8 default), the file is left at **0 bytes**.
`state/` is gitignored, so there is nothing to restore from — it has to be rebuilt from a session
transcript.

**Never read-modify-write either.** A concurrent session that rewrites the file between one run's
read and its write silently drops that run's entry. The store still has the row, but the local
mirror is what every mode pre-reads at orientation, so the day under-reports.

The helper closes both: an atomic temp-then-`os.replace` for whole-file writes (so a failure leaves
the *old* content, never an empty file), and a lock for append/prepend. The lock is deliberate rather
than lazy — POSIX `O_APPEND` would be enough, but **Windows emulates it as seek-then-write, which is
not atomic**, and the daemon may run on Windows: concurrent appends through plain `"a"` mode
measurably lose entries.

**And check that the thing on the left of the `<` actually produced something.** If the step that
was supposed to generate the content has already failed and the shell runs the next statement
anyway, a `write` piped an **empty stdin** would atomically, faithfully replace the file with
**nothing**. Using the helper correctly is not sufficient; the payload has to be real. So `write`
**refuses** a zero-byte payload over a file that holds bytes and **exits 2 having changed nothing**:

```bash
python ../scripts/memory_write.py write ../state/run-log.md < /dev/null
# memory_write: REFUSED to write 0 bytes over ../state/run-log.md, which currently holds
# N bytes. The file was NOT modified. … pass --allow-empty (CLI) or allow_empty=True (write_text).
```

(`carry-over.md` gets the same refusal one layer up — `carryover_region.py write-region` refuses an
empty region body without `--allow-empty` before it ever reaches `memory_write.write_text`.)

Pass `--allow-empty` **only** when emptying the file is the actual intent. `append`/`prepend` are not
refused — they cannot lose content to an empty payload — but they do warn on stderr, because an empty
payload means the same failed producer either way. `../docs/state-durability-spec.md` §3.3.

---

## Carry-over — the assistant's running "open loops"

Purpose: nothing the assistant is holding for the owner silently falls through. Distinct from the
journal's task carry-over, it tracks **the assistant's** open loops:

- **Held drafts** awaiting approval (email/Slack replies, calendar responses it proposed).
- **Unanswered calendar invites** it flagged.
- **Follow-ups** it promised ("I'll check back on X tomorrow").
- **Active/Carrying-Over Important Flags** worth keeping in view (read from the store, not duplicated).

**Rules:** rebuild the `WRAP_SEED` region from current state each run — resolved items drop off, new
ones appear, **and yesterday's region is fully replaced, never kept underneath** — order by urgency,
group by kind (approvals, invites, follow-ups, flags); a held draft stays until the owner
approves/sends or explicitly drops it. It lives in two interlinked surfaces: the Run Log
`Carry-Over Context` field (machine-readable handoff to the next run) and the local
`../state/carry-over.md` running log, whose own shape — the `WRAP_SEED` region, the hand-written
head, the `GENERATED` open-loops region — is `../docs/carry-over-region-spec.md`.

**What does NOT belong in `carry-over.md`.** A witness-only ad-hoc note — something with no closing
event, meant to be found later by grep or recall rather than re-surfaced every night — fails
`../scripts/loops.py add`'s own "can you name the event that would close this?" membership test ON
PURPOSE, and it fails this file's purpose too: dated notes appended at the bottom and never revisited
are exactly how the file accretes. **It goes to `../scripts/notes.py` instead** — `add "<text>"
[--source chat|watch|job]` appends to its own dated file, `state/notes/YYYY-MM-DD.md` (the owner-local
activity day — before the day-boundary hour, default 05:00, it is still yesterday), pruned by age
(`prune --days N`, default 90) rather than accreting forever in a file something else already rebuilds
nightly (`../docs/carry-over-region-spec.md` §2.4).

**"Resolved items drop off" is a rule, not a mechanism — a written correction inside the file does
not make the older reference to it disappear.** A summary line citing a PR as still open can survive
many mornings of the Brief while the same file, hundreds of lines lower, already marks that PR
`~~struck-through~~ RESOLVED` — the file records its own refutation and nothing reads the summary
against it. **`../scripts/check_carryover_resolved.py`** flags exactly this shape and is called from
the EOD Wrap (`../../subagents/eod-wrap/SKILL.md`) against the freshly-rebuilt draft, **before** it is
handed to `carryover_region.py write-region --name WRAP_SEED` — report-only for now. **It cannot run
in CI**: `state/carry-over.md` is gitignored, so no PR diff ever contains a real one; the Wrap's own
rebuild step is the only point in the system that both produces the content and can still change it
before it lands.

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
  For a non-owner recipient the order is fixed: **`../scripts/pending_approvals.py resolve a<N>
  --status approved` first, then the send** — the send gate (`../scripts/send_gate.py`,
  `autonomy-policy.md` → *"Where the gate actually lives"*) refuses a send with no matching
  `approved` row and spends it on use.
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

## Context digest — RETIRED

Dream mode used to **overwrite** `../state/context-digest.md` nightly with a condensed view of the
day — *yesterday in one breath, open loops, what's queued for today, active flags, pending
reminders* — plus, over time, a standing-safety section and the Brief's pre-stage block. It was
retired because an LLM-written digest duplicates durable stores that already exist, and a free,
stale summary in context out-competes the fresh record it summarizes
(`../docs/read-first-retirement-spec.md`, `../docs/grounding-restructure-spec.md` §4.4). Each job it
did now has its own single-writer home instead:

- **The day's summary** is the Run Log itself (`../state/run-log.md`) — nothing was mirroring it that
  the Run Log didn't already say.
- **Open loops** live in the open-loops register, `../state/open-loops.json` (`../scripts/loops.py`),
  rendered into `../state/carry-over.md`'s `GENERATED` region.
- **Standing-safety items** (things a warm session must not re-raise cold) live in
  `../state/standing-safety.json` (`../scripts/standing_safety.py`, written by Dream) —
  `presence.py` renders them into the warm session's cold grounding, so they arrive whether or not
  anyone chose to look.
- **The Brief's Phase-1 pre-stage** lives in `../state/brief-prestage.json`
  (`../scripts/brief_prestage.py` — one writer, Dream step 1b; one reader, Brief Phase 1).

A leftover `context-digest.md` on an upgraded install is no longer written or read for orientation;
`standing_safety.py import-digest` migrates any standing-safety items it held into the store once
(the CLI refuses a second run without `--force`).

---

## Durable act-low writes — the write-behind outbox (Notion backend)

The run-log and carry-over above are how the assistant remembers *across runs*; the **outbox** is how
its **act-low Notion-backend writes don't get lost** *within* the failure window. An ack (⏰ row →
`Status`/`Last Acknowledged`) and a med-intake row are **journaled to a durable local store first** —
`../state/notion-outbox.sqlite`, via `../scripts/outbox.py` — **then flushed to Notion** by the LLM
turn via MCP, retried until they land. This closes the chat→store ack write-through gap (acks that
reached `state/acks.json` but never flipped the store row, silently desyncing the system of record).

**Two of the four ops have a producer; two do not.** `outbox_common.OPS` declares four —
`ack_reminder` · `med_log` · `run_log_finalize` · `reminder_status` — and the store, the idempotency
keys and the drain handle all four. But only the first two have a dedicated CLI verb (`outbox.py
ack`, `outbox.py medlog`) and a caller. **`run_log_finalize` and `reminder_status` are reachable only
through the generic `outbox.py enqueue` escape hatch and nothing enqueues them today**, so a run-log
finalize is still an in-turn MCP write with **no durable journal** — if the turn dies between the
work and the finalize, the row stays `Partial`. That is the designed phase-2 gap, not a defect; it is
written here so nobody reads the guarantee below as covering it.

- **Journal-first is the guarantee.** Once `outbox.py ack …` (or `medlog …`) returns `ok`, the write
  *will* land even across a reboot — so the assistant may honestly tell the owner "recorded" the
  instant it's journaled, superseding the old **manual "park it in carry-over to record next run"**
  step *for those writes*. Carry-over parking remains the fallback for writes the outbox doesn't cover.
- **Belt-and-suspenders.** The happy path still does the direct MCP write in-turn (zero added latency);
  the outbox only earns its keep when that write fails. Enqueue is idempotent, so the double-write is a
  no-op — see the ack flow in `../modes/chat.md` and the schema/lifecycle in `../state/README.md`.
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
