---
name: eod-wrap
description: >-
  The assistant's end-of-day wrap. A short evening recap for the owner — what got done today, what
  slipped, what's waiting on them, and a preview of tomorrow — from their calendar and Notion.
  Write-enabled under the act-low / ask-high gate. Use for "end of day", "wrap up", "what did I do
  today", or the scheduled evening run. Delegated to by the seneschal orchestrator (Wrap mode).
compatibility: Requires the Notion MCP (mcp__*__notion-*) and a Calendar MCP. Reads the assistant's persona + references.
---

# End-of-Day Wrap (Seneschal · Wrap mode)

Close out the day in the persona's voice: brief, honest, and forward-looking. **Write-enabled, governed
by the act-low / ask-high gate** (`../../seneschal/references/autonomy-policy.md`): the Wrap makes
**act-low** writes directly (reconcile its own ⏰ tracker, apply acks it surfaces, seed carry-over, write
the Run Log) and **drafts-and-holds ask-high** ones (flip a linked Task/Goal to `Done`, merge/archive
Task rows, any send) unless the owner has approved them. The **scheduled** run (`seneschal-eod-wrap`,
evening daily — e.g. 21:07 local) also delivers + writes the trace: it appends the wrap to the
"🗒️ Daily Brief" page and writes a Run Log row (Mode=Wrap), mirroring the morning brief.

## Read first

- `../../persona/persona.md` (else `../../persona/persona.default.md`) — voice.
- `../../seneschal/references/briefing.md` — the EOD Wrap shape (and the morning Brief it complements).
- `../../seneschal/references/databases.md` — Tasks/Projects/Flags **+ ⏰ Reminders** IDs + the SQL
  projection gotcha.
- `../../seneschal/references/reminders-policy.md` — what counts as acked/done/skipped in the Reminders
  tracker.
- `../../seneschal/references/calendar-mapping.md` — the confirmed calendar include-set.

## Steps

**1 — Window.** "Today" in the owner's configured timezone (after-midnight still counts as today).

**2 — Gather, read-only, in parallel:**
- **Done today (Tasks):** Tasks where `Status = Done` and `"date:Completed:start"` is today (fall back to
  `Last Updated`/`lastEditedTime` for older rows with no `Completed` date); plus achievements if surfaced.
  Note wins from the day's journal entry if present.
- **Done today (⏰ Reminders):** rows with `"date:Last Acknowledged:start"` = today and `Status IN (Done,
  Finished)` — the habits/todos the owner acked today, whether by Notion tick or by telling the assistant
  over Telegram/chat. (`Done` = done-for-today, any `Type`; `Finished` = a retired item acked on its way
  out; **query both**, both count as done today.)
  **Never use the `Ack` checkbox as the signal** — it's a one-shot input the reminder slots consume and
  untick (`databases.md`); all-unticked boxes in the evening is normal, not "nothing finished."
  `Status = Skipped` with today's `Last Acknowledged` = an honest "not today" — neither done nor slipped.
  A done **Today Todo** whose `Related Task` isn't flipped yet → surface it under "Waiting on you" as a
  proposed flip (ask-high) and flip it once the owner okays it.
- **Slipped (Tasks):** Tasks with `"date:Due:start"` ≤ today still not `Done`/`Archived` (the morning
  brief's "due today" that didn't close) → these carry to tomorrow.
- **Slipped (⏰ Reminders):** rows that fired today (`Reminded Today` true or `"date:Last Reminded:start"`
  = today) still `Pending`/`Reminded`/`Snoozed` — nudged, never answered. Fold the important ones
  (re-fire class: Super-Critical/Critical/High or `Nag Until Done`) into Slipped; leave the low-stakes
  ones to their miss counters.
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

- **Write within the gate.** Act-low writes (its own ⏰ tracker, applying acks, carry-over, the Run Log)
  it makes directly; ask-high writes (flip a linked Task/Goal to `Done`, merge/archive Task rows, any
  send) are drafted-and-held and executed only on the owner's approval. **Only claim a write that landed.**
- **Honest and short.** If little happened, say so plainly; don't inflate. Cite sources; don't invent.
- Times in the owner's configured timezone.
