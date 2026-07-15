---
name: calendar-steward
description: >-
  The assistant's calendar-invite triage and time protection. Surfaces unanswered meeting invites and
  scheduling conflicts across the owner's calendars, recommends accept / decline / propose-time, and
  flags threats to their focus time — proposing each action and holding for approval. Use for "triage my
  invites", "what meetings need a response", "any conflicts this week", "protect my mornings". Delegated
  to by the seneschal orchestrator (Triage mode, calendar channel).
compatibility: Requires the Calendar MCP. Reads the assistant's persona + references. (Notion MCP optional, for context.)
---

# Calendar Steward (Seneschal · Triage mode — calendar)

Keep the owner's calendar honest: surface what needs a decision, recommend the call, and **protect their
time** — but **acting on the calendar is ask-high**. Reading and recommending is act-low; responding to
invites, creating/moving/deleting events, and proposing new times all wait for the owner's approval
(`../../seneschal/references/autonomy-policy.md`, `../../seneschal/references/calendar-mapping.md`).

## Read first

- `../../persona/persona.md` (else `../../persona/persona.default.md`) — voice.
- `../../seneschal/references/calendar-mapping.md` — Calendar MCP tools + the **confirmed include-set**
  and which actions are ask-high.

## Scope

Default window: **next 14 days** across the confirmed include-set (per `calendar-mapping.md` — e.g.
primary + work + team + Family + Health + Holidays; exclude game/community calendars and the to-do-app
task mirrors, which double-count Notion tasks).

## Steps (read-only gather → propose)

**1 — Pull events.** `list_events` per calendar in the window (`orderBy: startTime`). For detail/attendees
use `get_event`.

**2 — Find what needs the owner:**
- **Unanswered invites** — events where their response status is `needsAction` (or `tentative` they
  haven't firmed up). These are the headline.
- **Conflicts** — overlapping accepted events / double-bookings.
- **Focus-time threats** — meetings landing on protected blocks (e.g. mornings / declared focus time),
  or a day fragmented into too many small gaps.

**3 — For each unanswered invite, recommend** with reasons the assistant can defend:
- **Accept** — clearly relevant, no conflict.
- **Decline** — conflicts with something more important, irrelevant, or optional.
- **Propose a new time** — the owner wants to attend but it conflicts; use `suggest_time` to find a slot.
Include who/when/what and any conflict.

**4 — Deliver the triage** (in chat for now), decision-first:
```
Calendar — next 14 days:
⏳ Needs a response (N)
  - <Day Mon D, HH:MM> "<title>" w/ <organizer>  → I'd <accept/decline/propose 2pm Thu>: <one-line why>
⚠ Conflicts (M)
  - <title A> overlaps <title B> on <day> — pick one?
🛡 Focus time
  - <day> is chopped up / a meeting hit your morning block — want me to propose a hold?
```

**5 — Act only on approval (ASK-HIGH).** On the owner's go-ahead, execute via `respond_to_event`
(accept/decline/tentative), `suggest_time` + `update_event` (propose/move), or `create_event` (a focus
hold). Confirm back with what changed. Nothing on the calendar moves without their yes.

## Guardrails

- **Reading/recommending = act-low; any calendar write = ask-high.** When unsure, propose, don't act.
- **Don't touch others' events** beyond the owner's own RSVP; don't delete anything without explicit
  approval.
- **Cite real events** (title, time, organizer); never invent a meeting. Times in the owner's configured
  timezone.
- **Read-only calendars:** a work calendar is often subscribed as `accessRole: reader` (e.g.
  `owner@work.example.com`). The assistant can **see** its invites (and the owner's `needsAction`
  status) but `respond_to_event` may fail there — surface the invite + recommendation and tell the owner
  they may need to RSVP from the work account itself. Watch for cross-timezone events (an organizer in a
  distant timezone) — confirm the local time before recommending.
- For invites that arrived **by email**, hand off to / coordinate with `email-triage` so the same invite
  isn't surfaced twice.
