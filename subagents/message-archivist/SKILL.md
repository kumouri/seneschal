---
name: message-archivist
description: >-
  The assistant's Archivist. Builds a durable, readable archive of the owner's full conversation with
  one person across every service — a Telegram Desktop export, the official Discord data package (the
  owner's own half only), and an SMS Backup & Restore XML export today — merged onto one
  timestamp-ordered timeline with all media pulled local. Emits a readable transcript
  (conversation.md / .html) + machine-readable conversation.json per person. Use for "archive
  my chat with X", "save my conversation with X", "build a transcript with Alex", "merge my message
  history with X", or "re-run the archive". Delegated to by the seneschal orchestrator (Archive mode).
compatibility: >-
  Requires the archiver scripts under ../../seneschal/scripts (archive_common.py, telegram_ingest.py,
  discord_export_ingest.py, sms_ingest.py, archive_aggregate.py). Every collector reads a manual
  offline export (Telegram Desktop JSON export; Discord data package; SMS Backup & Restore XML).
  Reads the person registry (seneschal/state/archive-people.json). Stdlib only.
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
| Discord | `discord_export_ingest.py` (official Discord data package — **owner's own messages only**) | ✅ shipped |
| SMS/MMS | `sms_ingest.py` (SMS Backup & Restore XML export) | ✅ shipped |
| Email | — | future |

A one-service archive is still valuable — collect whatever services have data and aggregate.

### Service caveats (tell the owner up front)

- **Discord shows the owner's half only.** Discord's data package contains just the requesting
  account's own messages — nobody else's — so every Discord record is the owner's side of the
  conversation. Where the bot is present in a channel, the live REST collector
  (`../../seneschal/scripts/discord_poll.py`) remains the supplementary source for both sides.
- **Discord attachments are expired links.** The package carries CDN URLs, not files; the signed
  links are usually dead on arrival, so attachments are recorded (`remote_url`, flagged missing)
  but never downloaded.
- **Old CSV-vintage Discord packages are unsupported.** If a channel folder has `messages.csv`
  instead of `messages.json`, request a fresh package — current ones export JSON.
- **Discord timestamps are naive** (zone undocumented, varies by package vintage) and are treated
  as UTC — expect skew up to a few hours on older packages.
- **SMS matching is last-10-digits.** `+1 (555) 123-4567` and `555-123-4567` match; international
  numbers sharing a 10-digit suffix would collide, and short codes must match exactly.

## Read first (once)

- `../../persona/persona.md` (else `../../persona/persona.default.md`) — voice.
- `../../seneschal/references/autonomy-policy.md` — the act-low / ask-high line.
- `../../seneschal/state/archive-people.json` — the person registry (who maps to which service ids; a
  tracked example ships at `archive-people.example.json`). If the person isn't there yet, add them
  (the owner's ids + the person's per-service ids) before collecting.

## Steps

**1 — Orient (act-low).** Resolve the person key from what the owner said. Read the registry; confirm
the service entries exist for them (Telegram `user_id` + `export_dir`; Discord `user_id` and/or
`channel_ids`; SMS `numbers`). If a source export is missing, tell the owner how to produce it and
proceed with whatever services *are* available:

- **Telegram:** Telegram Desktop → the chat → Export chat history → format **JSON**, include media.
- **Discord:** Settings → Privacy & Safety → **Request all of my data** (include Messages). The
  package arrives by email (can take days–weeks); extract the zip somewhere local.
- **SMS/MMS:** install **SMS Backup & Restore** on the phone → Back up → Messages (XML, include MMS
  media) → copy the `sms-YYYYMMDDHHMMSS.xml` to this machine.

**2 — Collect (act-low).** Run each available collector — same `--person <key>` shape, same
normalized output:

```
python ../../seneschal/scripts/telegram_ingest.py       --person <key>
python ../../seneschal/scripts/discord_export_ingest.py --person <key> --package <extracted-package-dir>
python ../../seneschal/scripts/sms_ingest.py            --person <key> --xml <sms-YYYYMMDDHHMMSS.xml>
```

(`--package` / `--xml` can be omitted when the registry entry carries `package_dir` / `xml_file`;
`--channel-id <id>` pins the Discord ingest to specific channels instead of registry auto-select.)

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
