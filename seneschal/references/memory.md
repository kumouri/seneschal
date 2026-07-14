# The assistant's local-first memory — run log, carry-over, context digest

How the assistant remembers across runs. Three runtime files hold its local memory. They are
**local-first caches**: **Notion is the system of record** (the 🧭 Run Log DB + the carry-over handoff
field), and these files are the cheap on-disk copies the modes read/write between runs.

## The three files (live data lives in `state/`, gitignored)

| Concern | Live file (gitignored) | Tracked seed | System of record |
|---------|------------------------|--------------|------------------|
| Run history | `../state/run-log.md` | `../state/run-log.example.md` | 🧭 Run Log DB (`collection://00000000-0000-0000-0000-000000000008`) |
| Open loops / held approvals | `../state/carry-over.md` | `../state/carry-over.example.md` | Run Log `Carry-Over Context` field |
| Nightly digest (cache) | `../state/context-digest.md` | `../state/context-digest.example.md` | — (regenerable cache; safe to lose) |

**Why they live in `state/`, not here.** The daemon runs off `main` and writes these files every few
minutes. Keeping them gitignored (like `reminders.json`) means a `git pull --ff-only` on the daemon's
checkout never conflicts on an append log — that's what lets the live brain track `main`. It also ends
the old failure mode where a tracked-but-uncommitted `run-log.md` history could get reverted to a stub
under fsmonitor + concurrent-`claude` load.

**Bootstrap-on-absence.** On a fresh checkout the live files don't exist. Any mode may create one by
copying its `*.example.md` seed (e.g. `state/run-log.example.md` → `state/run-log.md`); a missing
`context-digest.md` just means the Brief leans on Notion until the next Dream regenerates it. Never treat
"the file is missing" as an error — the durable copy is Notion.

---

## Run log — the incremental trace

Mirrors the journal-steward's Agent Run Log protocol. Purpose: a substantive assistant run leaves a
durable trace, so a cut-short run still recorded what it did, and the owner can see the assistant's work
over time.

**When to write:** scheduled/substantive runs that *act* or deliver a pushed briefing (Brief push, Wrap,
Triage sweeps that hold drafts). **Skip:** one-off interactive questions (Ask mode) and a quick
interactive in-chat Brief.

**Protocol:**
1. **Read prior context (Phase 0).** Query the last 3–5 🧭 Run Log rows by `Run Date` desc; read
   `Carry-Over Context` and `Issues / Uncertainties` to avoid repeating work and to follow up. (The
   `state/run-log.md` mirror is a cheap local pre-read; Notion is authoritative.)
2. **Create the row early** (after the mode's critical path) with `Status = Partial`,
   `Actions Summary = "⏳ in progress…"`, `Mode` set.
3. **Append to the page body after each phase** (`notion-update-page` body append) — a running trace.
4. **Finalize at the end:** set counts (`Items Surfaced`, `Actions Taken`, `Drafts Held`),
   `Status` (Success/Partial/Failed), and write `Actions Summary` + `Carry-Over Context` +
   `Issues / Uncertainties`.
5. **Mirror the entry** into `../state/run-log.md` (append a one-paragraph trace ending with the Notion
   row id). Seed the file from `../state/run-log.example.md` if it's missing.

Reuse the journal-steward's Notion write mechanics (date format, body append, status strings) verbatim.

---

## Carry-over — the assistant's running "open loops"

Purpose: nothing the assistant is holding for the owner silently falls through. Distinct from the
journal's task carry-over, it tracks **the assistant's** open loops:

- **Held drafts** awaiting approval (email/Slack replies, calendar responses it proposed).
- **Unanswered calendar invites** it flagged.
- **Follow-ups** it promised ("I'll check back on X tomorrow").
- **Active/Carrying-Over Important Flags** worth keeping in view (read from Notion, not duplicated).

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
1. Write the draft to its channel store where applicable (Proton/Gmail draft, Slack draft).
2. Append an entry to `pending-approvals.json` and the Notion carry-over with: `id`, `kind`
   (`email` | `slack` | `calendar_response` | `notion_write` | `archon` — a Forge lifecycle action or
   delegation, see `archons.md`), `channelRef`, `summary`, `bodyPreview`, `created_at`,
   `status: "pending"`.
3. **Surface it** to the owner on whatever surface fits — in chat (if they're live), and/or a Telegram
   push (*"Drafted a reply to Alex — reply `send a3` or `drop a3`."*), and/or a Notion comment. Use the
   short id so a one-word reply is unambiguous.

**Detecting the owner's decision:**
- **Chat / Telegram:** the sentinel pulls the reply into the inbox; Watch/Chat reads intent — `send`/`yes`/
  `approve` (+ id, or the most recent if only one is pending) → **approve**; `drop`/`no`/`reject` →
  **reject**. Ambiguous → ask, don't guess.
- **Notion:** Watch checks for new comments on the carry-over / pending-approvals surface and reads the
  same intent. (This is an LLM-tier read — the sentinel doesn't parse Notion.)

**Executing (reuse the `approve_draft` / `reject_draft` path in `SKILL.md`):**
- **Approve →** send/execute via the normal local path (email → `../scripts/proton_send.py` without
  `--dry-run`; Slack → Slack MCP send; calendar → `respond_to_event`). Set the entry `status: "sent"`.
- **Reject →** discard the draft; set `status: "rejected"`.
- Either way, **remove it from the open carry-over** and leave a Run Log trace. A failed send →
  `status: "failed"`, kept in carry-over so it isn't lost.

`pending-approvals.json` schema is documented in `../state/README.md`.

---

## Context digest — the nightly orientation cache

**Dream mode** (nightly wind-down) **overwrites** `../state/context-digest.md` with a condensed
view of the day — *yesterday in one breath, open loops, what's queued for today, active flags, pending
reminders* — so the next morning's **Brief** can orient cheaply without re-querying everything.

- **Dream writes** `state/context-digest.md` (overwrite each run).
- **Brief reads** it first, at Phase 0, before any Notion query.
- **If missing**, fall back to carry-over + Notion; the next Dream regenerates it. Keep it short — cheap
  orientation, not a transcript.
