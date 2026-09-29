# Briefings — Morning Brief & End-of-Day Wrap

How the assistant assembles its two recurring briefings — decision-first, in the assistant's voice
(crisp, warm-to-the-owner, no padding). Producing a briefing is mostly reads, but **neither is
read-only**: both can write under the act-low / ask-high gate (`autonomy-policy.md`) — deliver to the
"🗒️ Daily Brief" page + Run Log (act-low) and act on what they surface (act-low directly, ask-high
drafted-and-held). The morning Brief is the MVP. The run procedure is `../modes/brief.md`; this file
defines the **shape**.

## Sourcing (where each part comes from)

Store reads use the backend-neutral verbs (`store-query` on the domain; `databases.md` +
`../store/<backend>/schema.md`); the Notion-specific tools named below apply on the Notion backend.

| Section | Source | Tool(s) |
|---------|--------|---------|
| Today's schedule | Calendar MCP **or** the `gcal_api.py` bridge (in that order) | `list_events` / `gcal_api.py events` (today, owner-local); `get_event` / `get-event` for detail. **An empty read is an empty day, not a missing calendar** (`calendar-mapping.md`) |
| Due / overdue todos | store **tasks** | query `Due ≤ today` and `Status` not in (`Done`,`Archived`) (`databases.md`) |
| Active flags | store **flags** | query `Status` in (`Active`,`Carrying Over`) |
| In-flight projects | store **projects** | query `Status = In Progress` |
| Carry-over | **Interstitial Journal** top callout + last Run Log `Carry-Over Context` | `notion-fetch` / `notion-query-database-view` on Notion |
| Needs-a-decision | un-answered calendar invites, pending held drafts | from the above |
| Rulings wanted | `proposed-learnings.md` **Pending** section | local file read — free, no store call |
| Observation gates ready | rendered by `../scripts/observation_gate.py brief-line` (`../docs/observation-gate-spec.md`) | code-computed — free, no store call, never hand-typed |
| 🎯 Tomorrow's Lead | rendered by `../scripts/tomorrow_marker.py brief-line` from `../state/tomorrow.json` (`../docs/tomorrow-marker-spec.md`) | code-computed — free, no store call, never hand-typed |
| Unassigned work items | rendered by `../scripts/owi_unknowns.py brief-line` | code-computed — free, no store call, never hand-typed |

The three code-rendered lines **arrive in the scheduled run's own prompt**: `presence.py` renders
each script's `brief_line()` into the morning-brief launch context with an instruction to print it
verbatim, or says there's nothing so that section is omitted. A chat-invoked Brief has no such
prompt line; there, the three `brief-line` CLIs are the only sources. **Never type a count or a list
for any of them by hand.**

> **Read the pre-stage first — don't re-fire the full store batch.** Last night's **Dream** (step 1b)
> snapshots the three store reads above (Tasks due/overdue **today + tomorrow**, Active/Carrying-Over
> Flags, In-Progress Projects) into `../state/brief-prestage.json` (`../scripts/brief_prestage.py` —
> one writer, Dream; one reader, the Brief). The old `state/context-digest.md` pre-stage block is
> **retired**: its jobs moved to purpose-built files with one writer each. The morning Brief reads the
> pre-stage first (`brief_prestage.py read`) and issues only **delta** live-queries (Tasks
> completed/created since the snapshot's ~22:00 stamp; Flags/Projects changed since) instead of the
> whole 4-read fan-out. **Staleness caveat:** the snapshot predates the overnight hours, so the light
> morning delta check is still required — the store is a warm base, not the last word. If the read
> exits 3 (stale, absent, or corrupt), fall back to the full parallel batch. **Calendar + journal
> carry-over are always live** (not pre-staged).

> **"No calendar" is a two-door verdict.** Try the Calendar MCP; if this session has none, shell out to
> `gcal_api.py` (`--account <label> --env-file seneschal/scripts/google.env`). A read that succeeds with
> **zero events is an empty day** — say *"nothing on your calendar today"*. Only when **both** doors
> fail does the brief say the calendar is unreachable, and it names which door failed and why.
> Contract + failure table: `calendar-mapping.md`.

## Morning Brief — shape

Keep it scannable. Order sections by what needs the owner *now*. Omit empty sections rather than printing
"nothing here."

```
Good morning. Here's <Weekday, Month D>.

<observation-gate line(s), verbatim>       ← only when an observation gate just closed

🎯 Tomorrow's Lead                          ← only when something was marked for today
  - <code-rendered items, in the owner's stored order, verbatim>

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

📋 Unassigned work                         ← only when the count is > 0
  - <N> item(s) with no owner yet — say the word and I'll walk through them

🧵 Carry-over
  - <outstanding items from the journal callout / last run>
```

Rules:
- **The observation-gate line(s) lead, then 🎯 Tomorrow's Lead, then "Needs you."** A gate that just
  closed is the day's own leading news — an item that was waiting on evidence is ready to pick back up,
  and missing that for weeks is the failure the gate scanner exists to end. Tomorrow's Lead is the
  day's own stated plan. Neither is a subset of what needs a decision, so neither is folded into
  "Needs you."
- **Observation gates:** printed verbatim from `observation_gate.py brief-line` — at most a handful of
  "ready to continue" lines (an overflow is reported as a count, never silently truncated), plus a
  staleness note for a gate met long ago with no follow-up. The Brief never re-checks a gate itself.
- **🎯 Tomorrow's Lead:** the items the owner marked (a `<tomorrow>` chat tag, a `Tomorrow` checkbox on
  a Task/Reminder, or the Wrap's picker) whose `for_date` is today and are still open or rolled —
  rendered by code, **in the owner's stored order, never re-derived or re-ordered by the run**. An item
  here that also appears in today's Tasks/Flags read is de-duped into this section only. Empty → omit
  the section, never "nothing marked." It is not carry-over, not a priority-field rewrite, and not a
  reminder: a mark never touches `Priority`/`Due` or the nudge ladder.
- **Lead with "Needs you"** (after the two above). If nothing genuinely needs the owner, say so in one
  line and move on.
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
- **📋 Unassigned work:** one line, the **COUNT only**, never the items themselves (those arrive
  later, a few at a time, only after the owner says yes to the walk-through ask — `../modes/chat.md`).
  The line is code-rendered (`owi_unknowns.py brief-line`) and, for the scheduled run, already in the
  run's prompt, or the prompt says the count is 0 (omit the section). Never a hand-typed number.
  **The `note-brief-sent` reset is the daemon's job, not the run's:** `presence.py` fires it when the
  brief slot exits clean; a run that calls it too changes nothing and should not.
- Times in the owner's timezone (`tz_common`). Concise; this is a glance, not a report.

## End-of-Day Wrap — shape

The Wrap is **built** — it owns `../../subagents/eod-wrap/SKILL.md` and the daemon runs it on its
evening slot. The shape below is what it produces, not a proposal.

```
Evening recap — <Weekday, Month D>.

✔ Done today: <completed tasks / reminders acked / wins>
↪ Slipped: <due-today items not done / important reminders never answered> → carried to tomorrow
🗓 Tomorrow: <count> events; first at <HH:MM> <event>
📨 Waiting on you: <held drafts / unanswered invites still open>
```

Then, for 🎯 Tomorrow's Lead (`../docs/tomorrow-marker-spec.md`), **two pickers, never a prose list**
(`comms-mapping.md` → *PICKER*): one grid picker resolving each of today's marked items —
**Close** (done; a linked Task/Reminder flip is an ask-high *proposal*, never an automatic write) ·
**Roll to tomorrow** (keeps its order) · **Drop** (costs nothing, no post-mortem) — and one
multi-select asking what tomorrow's lead should be, over candidates the Wrap already gathered. There
is **no silent auto-roll**: the Wrap asks every evening, so a chronically rolled item is visible for
free.

Wrap-specific sourcing (on top of the table above):

| Section | Source | How |
|---------|--------|-----|
| Done today | store **tasks** | `Status = Done` and `Completed` = today (Notion: `"date:Completed:start"`) |
| Done today | store **reminders** | `Last Acknowledged` = today (Notion: `"date:Last Acknowledged:start"`) and `Status IN (Done, Finished)` — covers acks made in the store **or** over Telegram/chat (`Done` = done-for-today, `Finished` = a retired item acked on its way out; query both). **Never the `Ack` checkbox** — it's consumed + unticked by the reminder runs (`reminders-policy.md`) |
| Slipped | store **reminders** | fired today but still `Pending`/`Reminded`/`Snoozed` — surface the important / `Nag Until Done` ones |
| Tomorrow's Lead | `../state/tomorrow.json` + `Tomorrow = true` reconciliation | `tomorrow_marker.py reconcile` (fails open), then the two pickers above |

The Wrap also seeds tomorrow's carry-over (`../state/carry-over.md`; protocol in `memory.md`).
