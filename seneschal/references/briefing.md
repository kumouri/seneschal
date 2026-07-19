# Briefings — Morning Brief & End-of-Day Wrap

How the assistant assembles its two recurring briefings — decision-first, in the assistant's voice
(crisp, warm-to-the-owner, no padding). Producing a briefing is mostly reads, but **neither is
read-only**: both can write under the act-low / ask-high gate (`autonomy-policy.md`) — deliver to the
"🗒️ Daily Brief" page + Run Log (act-low) and act on what they surface (act-low directly, ask-high
drafted-and-held). The morning Brief is the MVP.

## Sourcing (where each part comes from)

| Section | Source | Tool(s) |
|---------|--------|---------|
| Today's schedule | Calendar MCP | `list_events` (today, in the owner's configured timezone); `get_event` for detail (`calendar-mapping.md`) |
| Due / overdue todos | Notion **Tasks** | query `Due ≤ today` and `Status` not in (`Done`,`Archived`) (`databases.md`) |
| Active flags | Notion **Important Flags** | query `Status` in (`Active`,`Carrying Over`) |
| In-flight projects | Notion **Projects** | query `Status = In Progress` |
| Carry-over | **Interstitial Journal** top callout + last Run Log `Carry-Over Context` | `notion-fetch` / `notion-query-database-view` |
| Needs-a-decision | un-answered calendar invites, pending held drafts | from the above |
| Rulings wanted | `proposed-learnings.md` **Pending** section | local file read — free, no Notion call |

> **Read the pre-stage first — don't re-fire the full Notion batch.** Last night's **Dream** snapshots the
> three Notion reads above (Tasks due/overdue **today + tomorrow**, Active/Carrying-Over Flags, In-Progress
> Projects) into the timestamped **`## Brief pre-stage (Dream)`** block of `state/context-digest.md`
> (`notion-rate-limits.md` — Dream digest pre-staging). The morning Brief reads that block as its base set
> and issues only **delta** live-queries (Tasks completed/created since the snapshot's ~22:00 stamp;
> Flags/Projects changed since) instead of the whole 4-read fan-out. **Staleness caveat:** the snapshot
> predates the overnight hours, so the light morning delta check is still required — the block is a warm
> base, not the last word. If the block is missing or its stamp isn't last night, fall back to the full
> parallel batch. **Calendar + journal carry-over are always live** (not pre-staged).

## Morning Brief — shape

Keep it scannable. Order sections by what needs the owner *now*. Omit empty sections rather than printing
"nothing here."

```
Good morning. Here's <Weekday, Month D>.

⏳ Needs you
  - <the 1–3 things that actually require a decision/response today>

📜 Rulings wanted                          ← only when proposed-learnings.md has Pending items
  - <short title + the specific ask, lettered options if it's a choice>
  - …

📅 Today
  - <HH:MM> <event>  (<accepted/“invite unanswered”>)
  - …
  - Next up after that: <HH:MM> <event>   ← only if morning is light

✅ Due / overdue
  - <task>  (<Priority>, due <date>)        ← overdue first, then today
  - …

⭐ On the radar
  - <Active/Carrying-Over flags, highest importance first>
  - <in-flight projects worth a glance>

🧵 Carry-over
  - <outstanding items from the journal callout / last run>
```

Rules:
- **Lead with "Needs you."** If nothing genuinely needs the owner, say so in one line and move on.
- **Cite, don't invent.** Every line traces to a real calendar event / task / flag. If a query returns
  nothing, the section is empty — never pad with guesses.
- **De-dupe across sources** (a task that's also a flag, an event that's also a carry-over item) —
  mention it once, in the most action-relevant section.
- **Rulings wanted (standing instruction):** every un-ruled proposal in
  `proposed-learnings.md` → Pending appears here **each morning until the owner rules** — one line per
  proposal, decision-first (title + the ask + lettered options), detail cited to the file rather than
  restated. When they rule, apply the `proposed-learnings.md` flow (approve → apply + move to Applied +
  graduation log; decline → Declined) via the normal branch → PR → merge-on-green path. This section is
  a *ruling queue*, not a nag — it never re-argues a proposal, and it disappears when Pending is empty.
- Times in the owner's configured timezone. Concise; this is a glance, not a report.

## End-of-Day Wrap — shape

```
Evening recap — <Weekday, Month D>.

✔ Done today: <completed tasks / reminders acked / wins>
↪ Slipped: <due-today items not done / important reminders never answered> → carried to tomorrow
🗓 Tomorrow: <count> events; first at <HH:MM> <event>
📨 Waiting on you: <held drafts / unanswered invites still open>
```

Wrap-specific sourcing (on top of the table above):

| Section | Source | How |
|---------|--------|-----|
| Done today | Notion **Tasks** | `Status = Done` and `"date:Completed:start"` = today |
| Done today | Notion **⏰ Reminders** | `"date:Last Acknowledged:start"` = today and `Status IN (Done, Finished)` — covers acks made in Notion **or** over Telegram/chat (`Done` = done-for-today, `Finished` = a retired item acked on its way out; query both). **Never the `Ack` checkbox** — it's consumed + unticked by the reminder runs (`reminders-policy.md`) |
| Slipped | Notion **⏰ Reminders** | fired today but still `Pending`/`Reminded`/`Snoozed` — surface the important / `Nag Until Done` ones |

The Wrap also seeds tomorrow's carry-over (`../state/carry-over.md`; protocol in `memory.md`).
