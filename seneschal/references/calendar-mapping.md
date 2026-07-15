# Calendar MCP — mapping & gotchas

The assistant's time-management and briefing modes read the calendar through the connected **Calendar
MCP** (tools named `mcp__<server>__<verb>`). The provider (Google vs Outlook) and the owner's primary
calendar ID are **discovered at runtime via `list_calendars`** — do not hard-code an ID until confirmed,
then record it here (in your local copy).

## Tools (suffixes)

| Need | Tool suffix | Notes |
|------|-------------|-------|
| Discover calendars | `list_calendars` | Run once to find the owner's primary; cache the ID below. |
| Read events | `list_events` | Filter by time window (today, in the owner's configured timezone). Read-only — used by Brief/Wrap. |
| Read one event | `get_event` | Detail for a specific event (attendees, location, conferencing). |
| Find a slot | `suggest_time` | For scheduling; respects existing busy blocks. |
| Create | `create_event` | **Ask-high** — drafting/holding for approval, not auto-created. |
| Update | `update_event` | **Ask-high.** |
| Delete | `delete_event` | **Ask-high.** |
| RSVP | `respond_to_event` | **Ask-high** — accept/decline/tentative on invites. |

## Briefing usage (read-only)

- "Today" = the owner's configured-timezone calendar day. Pull all of today's events; if it's early
  morning, also note the **next** upcoming event.
- For each event surface: time (local), title, and whether the owner has accepted/declined (so the brief
  can flag un-answered invites for Triage).
- Treat all-day events and focus blocks distinctly from meetings.

## Gotchas

- **Timezone:** always present times in the owner's configured timezone regardless of the event's
  stored zone.
- **Which calendar:** an owner often has **many calendars**; the brief must decide which ones count as
  "their schedule." Work through the roster once with `list_calendars` and record the include-set. An
  illustrative example roster:
  - **Primary:** `owner@example.com` (owner role) — the default for `list_events`.
  - **Work:** `owner@work.example.com` (reader role) and a shared team calendar
    (`team-cal-id@group.calendar.example.com`).
  - **Personal/life:** `Family` (gotcha: some calendars store times in **UTC** — convert carefully),
    `Health`, `Birthdays`, and a public holidays calendar (`en.usa#holiday…`-style).
  - **Task mirrors:** to-do-app calendars (e.g. `todo-cal-id@example.com`) surface task-like all-day
    items. These overlap with Notion tasks — de-dupe or exclude to avoid double-counting.
  - **Noise / not the owner's:** shared game/guild/community calendars — likely exclude from a normal
    brief.
  - **Confirmed include-set for briefs (example):** primary + work + team + Family + Health + Holidays.
    **Exclude** game/community calendars and the **task-mirror** calendars (they double-count Notion
    tasks).
- **Invites vs events:** an invite the owner hasn't answered still appears in `list_events`; check RSVP
  status before assuming attendance.
