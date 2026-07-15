---
name: eod-wrap
description: >-
  The assistant's end-of-day wrap. A short evening recap for the owner — what got done today, what
  slipped, what's waiting on them, and a preview of tomorrow — from their calendar and Notion.
  Write-enabled under the act-low / ask-high gate. Use for "end of day", "wrap up", "what did I do
  today", or the scheduled evening run. Delegated to by the seneschal orchestrator (Wrap mode).
compatibility: Requires a configured seneschal store (run /setup-store) and a Calendar MCP. The Notion backend additionally requires the Notion MCP (mcp__*__notion-*). Reads the assistant's persona + references.
---

# End-of-Day Wrap (Seneschal · Wrap mode)

Close out the day in the persona's voice: brief, honest, and forward-looking. **Write-enabled, governed
by the act-low / ask-high gate** (`../../seneschal/references/autonomy-policy.md`): the Wrap makes
**act-low** writes directly (reconcile its own Reminders tracker, apply acks it surfaces, seed carry-over,
write the Run Log) and **drafts-and-holds ask-high** ones (flip a linked Task/Goal to done, merge/archive
Task rows, any send) unless the owner has approved them. The **scheduled** run (`seneschal-eod-wrap`,
evening daily — e.g. 21:07 local) also delivers + writes the trace: it appends the wrap to the
"🗒️ Daily Brief" page and writes a Run Log row (Mode=Wrap), mirroring the morning brief.

## Read first

**Store access.** Read `../../seneschal/store/config.json` for the active backend, then that backend's
`../../seneschal/store/<backend>/schema.md` + `mapping.md`. Speak the six **store verbs** (`store-query`
/ `store-get` / `store-create` / `store-update` / `store-append` / `store-search`) plus domain nouns and
canonical, **emoji-free** option values; the mapping resolves them to the backend's tools. **Never fetch
schemas at runtime.**

- `../../persona/persona.md` (else `../../persona/persona.default.md`) — voice.
- `../../seneschal/references/briefing.md` — the EOD Wrap shape (and the morning Brief it complements).
- `../../seneschal/references/databases.md` — the Tasks/Projects/Flags **+ Reminders** domain/ID map;
  the Notion query projection gotcha lives in `../../seneschal/store/notion/mapping.md`.
- `../../seneschal/references/reminders-policy.md` — what counts as acked/done/skipped in the Reminders
  tracker.
- `../../seneschal/references/calendar-mapping.md` — the confirmed calendar include-set.

## Steps

**1 — Window.** "Today" in the owner's configured timezone (after-midnight still counts as today).

**2 — Gather, read-only, in parallel (`store-query`):**
- **Done today (Tasks):** Tasks where `status: done` and `completed` is today (fall back to the
  last-edited time for older rows with no `completed` date); plus achievements if surfaced. Note wins
  from the day's journal entry if present.
- **Done today (Reminders):** rows with `last_acknowledged` = today and status in (done, finished) — the
  habits/todos the owner acked today, whether by the one-tap flag or by telling the assistant over
  Telegram/chat. (`done` = done-for-today, any type; `finished` = a retired item acked on its way out;
  **query both**, both count as done today.)
  **Never use the one-tap `ack` flag as the signal** — it's a one-shot input the reminder slots consume
  and reset (`databases.md`); all-cleared flags in the evening is normal, not "nothing finished."
  `status: skipped` with today's `last_acknowledged` = an honest "not today" — neither done nor slipped.
  A done **Today Todo** whose `related_task` isn't flipped yet → surface it under "Waiting on you" as a
  proposed flip (ask-high) and flip it once the owner okays it.
- **Slipped (Tasks):** Tasks with due ≤ today still not done/archived (the morning brief's "due today"
  that didn't close) → these carry to tomorrow.
- **Slipped (Reminders):** rows that fired today (`reminded_today: true` or `last_reminded` = today)
  still `pending`/`reminded`/`snoozed` — nudged, never answered. Fold the important ones (re-fire class:
  super-critical/critical/high or `nag_until_done`) into Slipped; leave the low-stakes ones to their miss
  counters. (On the Notion backend, filter the date fields via their `date:<Prop>:start` projection — see
  `../../seneschal/store/notion/mapping.md`.)
- **Waiting on the owner:** open held drafts / unanswered calendar invites (from the day's Triage, if any).
- **Tomorrow:** `list_events` for tomorrow across the include-set; first event + count.

**3 — Deliver** per `briefing.md` → *End-of-Day Wrap*:
```
Evening recap — <Weekday, Month D>.

✔ Done today: <completed tasks / reminders acked / wins>      (or "quiet day" if nothing closed)
↪ Slipped: <due-today not done / important reminders never answered> → carried to tomorrow
🗓 Tomorrow: <N> events; first at <HH:MM> <event>
📨 Waiting on you: <held drafts / unanswered invites>
```

**4 — (later) Seed carry-over.** When the Run Log/carry-over are active for scheduled runs, the Wrap
seeds tomorrow's carry-over (`../../seneschal/state/carry-over.md`; protocol in
`../../seneschal/references/memory.md`).

## Guardrails

- **Write within the gate.** Act-low writes (its own Reminders tracker, applying acks, carry-over, the
  Run Log) it makes directly; ask-high writes (flip a linked Task/Goal to done, merge/archive Task rows,
  any send) are drafted-and-held and executed only on the owner's approval. **Only claim a write that landed.**
- **Honest and short.** If little happened, say so plainly; don't inflate. Cite sources; don't invent.
- Times in the owner's configured timezone.
