---
name: calendar-steward
description: >-
  The assistant's calendar-invite triage and time protection. Surfaces unanswered meeting invites and
  scheduling conflicts across the owner's calendars, recommends accept / decline / propose-time, and
  flags threats to their focus time — proposing each action and holding for approval. Use for "triage my
  invites", "what meetings need a response", "any conflicts this week", "protect my mornings". Delegated
  to by the seneschal orchestrator (Triage mode, calendar channel).
compatibility: >-
  Needs a calendar — either a Calendar MCP or the gcal_api.py REST bridge (see
  ../../seneschal/references/calendar-mapping.md). Without an MCP the bridge covers every read but
  CANNOT RSVP. Reads the assistant's persona + references. (Store optional, for context.)
---

# Calendar Steward (Seneschal · Triage mode — calendar)

Keep the owner's calendar honest: surface what needs a decision, recommend the call, and **protect their
time** — but **acting on the calendar is ask-high**. Reading and recommending is act-low; responding to
invites, creating/moving/deleting events, and proposing new times all wait for the owner's approval
(`../../seneschal/references/autonomy-policy.md`, `../../seneschal/references/calendar-mapping.md`).

## Read first

- `../../persona/persona.md` (else `../../persona/persona.default.md`) — voice.
- `../../seneschal/references/calendar-mapping.md` — the **two doors** (Calendar MCP → `gcal_api.py`
  bridge) and their order, the **confirmed include-set**, and which actions are ask-high.

## Scope

Default window: **next 14 days** across the confirmed include-set (per `calendar-mapping.md` — e.g.
primary + work + team + Family + Health + Holidays; exclude game/community calendars and the to-do-app
task mirrors, which double-count Notion tasks).

## Steps (read-only gather → propose)

**1 — Pull events. Try both doors, in order** (`calendar-mapping.md`):

- **Calendar MCP** if this session has one: `list_events` per calendar in the window
  (`orderBy: startTime`); `get_event` for detail/attendees.
- **Otherwise the REST bridge**, one call per calendar, issued as one parallel batch:
  ```sh
  python seneschal/scripts/gcal_api.py events --account personal --env-file seneschal/scripts/google.env     --calendar <id> --start <YYYY-MM-DD>T00:00:00<±HH:MM> --end <YYYY-MM-DD>T23:59:59<±HH:MM> --raw
  ```
  `--raw` matters here: **RSVP status lives in `attendees[].responseStatus`**, which the slimmed
  default output drops — and RSVP status is the whole point of this subagent. The owner's UTC offset
  is mandatory (from the owner's timezone, `owner.timezone` in `persona/identity.json`, DST-aware for
  the date in question); a bare date is sent as UTC and shifts the window. `get-event --id <eventId>`
  for one event's detail.

**Zero events across the window is an empty calendar, not a broken one** — say "nothing needs a
response." Only if **both** doors fail is the calendar unreachable, and then name which failed and why.

**2 — Find what needs the owner:**
- **Unanswered invites** — events where their response status is `needsAction` (or `tentative` they
  haven't firmed up). These are the headline.
- **Conflicts** — overlapping accepted events / double-bookings.
- **Focus-time threats** — meetings landing on protected blocks (e.g. mornings / declared focus time),
  or a day fragmented into too many small gaps.

**3 — For each unanswered invite, recommend** with reasons the assistant can defend:
- **Accept** — clearly relevant, no conflict.
- **Decline** — conflicts with something more important, irrelevant, or optional.
- **Propose a new time** — the owner wants to attend but it conflicts; use `suggest_time` to find a
  slot. **The bridge has no `suggest_time`** — on that door, derive the slot yourself from
  `gcal_api.py freebusy --calendar <id1>,<id2>,…` (the one subcommand that takes a comma list).
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

**On the bridge door the write surface is narrower, and you say so rather than pretending.** It has
`create-event` and `delete-event` only — **no RSVP and no update**. So with no Calendar MCP connected:

| Approved action | Bridge |
|---|---|
| Focus hold / new event | `gcal_api.py create-event --account personal --env-file seneschal/scripts/google.env --summary … --start … --end …` |
| Cancel an event the owner owns | `gcal_api.py delete-event … --id <eventId>` |
| **RSVP (accept/decline/tentative)** | **not possible** — hand it back: give the owner the event, your recommendation and the reason, and tell them to answer it in their calendar app. Never report an RSVP as done. |
| **Move an event** | no `update-event` — treat as delete + create, and **only if the owner approved exactly that**; otherwise hand it back. |

These are still **ask-high**: having a second door widens what the assistant can *read*, never what it
may do unasked.

## Guardrails

- **Reading/recommending = act-low; any calendar write = ask-high.** When unsure, propose, don't act.
- **Don't touch others' events** beyond the owner's own RSVP; don't delete anything without explicit
  approval.
- **Cite real events** (title, time, organizer); never invent a meeting. Times in the owner's configured
  timezone.
- **Read-only calendars:** a work calendar is often subscribed as `accessRole: reader` (e.g.
  `owner@work.example.com`). The assistant can **see** its invites (and the owner's `needsAction`
  status) but `respond_to_event` may fail there (and the bridge can't RSVP anywhere) — surface the
  invite + recommendation and tell the owner they need to RSVP from the work account itself. Watch for cross-timezone events (an organizer in a
  distant timezone) — confirm the local time before recommending.
- For invites that arrived **by email**, hand off to / coordinate with `email-triage` so the same invite
  isn't surfaced twice.
