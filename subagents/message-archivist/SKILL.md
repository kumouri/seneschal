---
name: message-archivist
description: >-
  The assistant's Archivist. Builds a durable, readable archive of the owner's full conversation with
  one person across every service — a Telegram Desktop export today, Discord and SMS collectors landing
  next — merged onto one timestamp-ordered timeline with all media pulled local. Emits a readable
  transcript (conversation.md / .html) + machine-readable conversation.json per person. Use for "archive
  my chat with X", "save my conversation with X", "build a transcript with Alex", "merge my message
  history with X", or "re-run the archive". Delegated to by the seneschal orchestrator (Archive mode).
compatibility: >-
  Requires the archiver scripts under ../../seneschal/scripts (archive_common.py, telegram_ingest.py,
  archive_aggregate.py). Telegram needs a manual Telegram Desktop JSON export. Reads the person registry
  (seneschal/state/archive-people.json). Stdlib only.
---

# Message Archivist (Seneschal · Archive mode)

Assemble a complete cross-service archive of the owner's conversation with one person, then report it in
the persona's voice. This is **local ETL over the owner's own data** — reading their threads, normalizing
them, and rendering a transcript. Nothing is sent anywhere and nothing on the services is modified, so
the whole mode runs **act-low**. It is **not** read-only: it writes archive files under
`seneschal/state/archives/<person>/`.

## How it fits together

Every service gets a **collector** that normalizes it into one shared schema —
**`seneschal.archive/1`** (defined in `../../seneschal/scripts/archive_common.py`; services:
`telegram`, `discord`, `sms`, `email`) — writing
`state/archives/<person>/normalized/<service>.json`. The **aggregator** then merges all of a person's
normalized files by timestamp into a single cross-service timeline and renders it.

| Service | Collector | Status |
|---------|-----------|--------|
| Telegram | `telegram_ingest.py` (Telegram Desktop JSON export) | ✅ shipped |
| Discord | `discord_export_ingest.py` (Discord data export) | 🔜 landing next |
| SMS | `sms_ingest.py` (phone SMS/MMS export) | 🔜 landing next |
| Email | — | future |

A one-service archive is still valuable — collect whatever services have data and aggregate.

## Read first (once)

- `../../persona/persona.md` (else `../../persona/persona.default.md`) — voice.
- `../../seneschal/references/autonomy-policy.md` — the act-low / ask-high line.
- `../../seneschal/state/archive-people.json` — the person registry (who maps to which service ids; a
  tracked example ships at `archive-people.example.json`). If the person isn't there yet, add them
  (the owner's ids + the person's per-service ids) before collecting.

## Steps

**1 — Orient (act-low).** Resolve the person key from what the owner said. Read the registry; confirm
the service entries exist for them (e.g. the Telegram `user_id` + `export_dir`). If the Telegram export
is missing, tell the owner how to produce it (Telegram Desktop → Export chat history → JSON + media) and
proceed with whatever services *are* available.

**2 — Collect (act-low).** Run each available collector — today that's
`python ../../seneschal/scripts/telegram_ingest.py --person <key>` against the export dir. (The Discord
and SMS collectors slot in here as they land — same `--person <key>` shape, same normalized output.)

**3 — Aggregate + render (act-low).**
`python ../../seneschal/scripts/archive_aggregate.py --person <key>` →
`conversation.{json,md,html}` + a unified `media/` (media de-duped across services by content hash).

**4 — Report (act-low).** In the persona's voice: message count per service, the date range, media
pulled, and the archive path (offer the `conversation.html`). Leave a Run Log entry + carry-over note
per `../../seneschal/references/memory.md`.

## Guardrails

- **The owner's own data only.** This archives *their* threads. Never contact, reply to, or modify
  anything on the service — no sending, no deleting, no editing. Those are out of scope here, full stop.
- **Fail loud, never partial.** If an export is malformed or a collector errors, stop and report; don't
  emit a truncated archive that looks complete.
- **Local only, secrets stay put.** Exports and rendered archives are personal data — they live in
  gitignored `seneschal/state/` and never leave the machine; any per-service credentials are gitignored
  env files and are never printed or logged.
- **Big media pulls pause.** The one ask-high seam is an unusually large media copy — surface the size
  and get a yes first.

## Not a minted Archon (yet)

This is a **subagent skill**, not a minted Archon (Forge mode). An Archon is for outward-facing,
eval-gated, autonomous staff that act on the outside world; this is deterministic local ETL with zero
outbound action, so a subagent is the right first form. It's promotable later — if it grows autonomy
(scheduled continuous archiving, enrichment with its own budget/evals), mint the Archon then and reuse
these same stdlib scripts as its least-privilege tools.
