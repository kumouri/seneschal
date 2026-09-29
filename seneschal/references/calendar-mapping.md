# Calendar — mapping & gotchas

The assistant reads the calendar through **two doors**, and it is expected to try both before it ever
says it has no calendar. The owner's primary calendar ID and the brief's include-set are confirmed
once per install (see *Which calendar* below) and recorded in the local copy — **after that, do not
re-discover them at runtime** unless a read fails with an explicit "no such calendar".

## The two doors, and the order

| # | Door | How you know it's there | Use it for |
|---|------|-------------------------|------------|
| 1 | **Calendar MCP** (`mcp__<server>__<verb>`) | a `list_events` / `list_calendars` tool is in this session's tool list | everything, when present — it also has `suggest_time` and RSVP, which the bridge doesn't |
| 2 | **`gcal_api.py` REST bridge** | `seneschal/scripts/gcal_api.py` exists **and** `seneschal/scripts/google.env` exists | reads, always; writes only on approval |

**The order is: MCP if present → otherwise the bridge → only then "no calendar".** The daemon's
headless sessions only carry the MCP servers `presence.py` forwards (the store's MCP, plus Slack when
configured) — so on a daemon turn with no Calendar MCP, **door 2 is the live one**, and a run that
reports "Calendar MCP not connected" without trying it has skipped a working calendar.

**Say which door failed and why.** "No calendar" is only honest after both doors are tried, and it is
never a bare sentence — it names what was missing: *"no Calendar MCP in this session, and the bridge
couldn't read `google.env`"*. See **"What each failure actually looks like"** below.

### An empty calendar is NOT a missing calendar

`{"ok": true, "count": 0, "events": []}` — or an MCP `list_events` returning zero items — is a
**successful read of an empty day**. Report it as *"nothing on your calendar today"*. Reporting a
successful empty read as an unavailable integration is the bug this section exists to prevent: a
morning Brief that says "no live calendar" when the truthful answer is "you're clear" teaches the
owner to distrust both answers.

Three distinct outcomes; never collapse them:

| Outcome | What to say |
|---|---|
| read succeeded, ≥1 event | the events |
| read succeeded, 0 events | **"nothing on your calendar today"** |
| both doors failed | "I couldn't reach your calendar — *&lt;which door, which error&gt;*" |

A successful read also says nothing about a calendar **neither door reads** (a work calendar on an
account that was never connected, say): an event there is absent from a successful read, and that
absence is not evidence about it (`comms-mapping.md` → *What the assistant cannot reach*).

## Door 1 — Calendar MCP tools (suffixes)

| Need | Tool suffix | Notes |
|------|-------------|-------|
| Discover calendars | `list_calendars` | Once the include-set below is confirmed, this is a re-check, not a routine step. |
| Read events | `list_events` | Filter by time window (today, in the owner's timezone). **Act-low.** |
| Read one event | `get_event` | Detail for a specific event (attendees, location, conferencing). **Act-low.** |
| Find a slot | `suggest_time` | For scheduling; respects existing busy blocks. **Act-low** (it only reads). |
| Create | `create_event` | **Ask-high** — draft and hold, never auto-created. |
| Update | `update_event` | **Ask-high.** |
| Delete | `delete_event` | **Ask-high.** |
| RSVP | `respond_to_event` | **Ask-high** — accept/decline/tentative on invites. |

## Door 2 — the `gcal_api.py` REST bridge

A stdlib-only Google Calendar **v3** client on the shared OAuth plumbing in `google_common.py`, with
the full `calendar` scope. Setup, consent flow and the refresh-token gotcha: `../scripts/GOOGLE_SETUP.md`.

**The invocation, in full — every part of it is required:**

```sh
python seneschal/scripts/gcal_api.py <subcommand> --account <label> --env-file seneschal/scripts/google.env [...]
```

- **`--env-file seneschal/scripts/google.env` is not optional.** Without it the script has no
  `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` and every call fails. The path is resolved against the
  **current working directory**, so from the repo root it is `seneschal/scripts/google.env`; from
  inside `seneschal/scripts/` it is `google.env`. The file is **untracked and holds live OAuth
  secrets** — never read it, print it, edit it or commit it.
- **`--account <label>`** is the label the owner's refresh token is stored under at consent time
  (`personal`, `work`, … — whatever was chosen in `GOOGLE_SETUP.md`);
  `python seneschal/scripts/google_auth.py --list --env-file …` shows the stored labels. Record which
  label is the owner's own calendar account in the local copy of this file.
- Run it from the repo root — `gcal_api.py` does `import google_common`, which resolves because Python
  puts the script's own directory on the path.

| Need | Subcommand | Gate | Notes |
|------|-----------|------|-------|
| Discover calendars | `list-calendars` | **act-low** | `{ok, count, calendars:[{id, summary, primary, accessRole, timeZone}]}` |
| Read events | `events` | **act-low** | `--days N` \| `--start`/`--end`, `--calendar <id>`, `--max`, `--query`, `--raw` |
| Free/busy | `freebusy` | **act-low** | `--calendar` takes a **comma-separated list** here (the only subcommand that does) |
| Read one event | `get-event` | **act-low** | `--id <eventId>` |
| Create | `create-event` | **ASK-HIGH** | `--json '{...}'`, or `--summary` + `--start` + `--end` (+ `--location`/`--description`) |
| Delete | `delete-event` | **ASK-HIGH** | `--id <eventId>` |

**No `update-event`, no `suggest_time`, no RSVP.** The bridge cannot answer an invite. When door 2 is
the only door and an invite needs a response, compute the slot from `freebusy` yourself, put the
recommendation on the approval surface, and tell the owner plainly that the RSVP itself has to happen
in Google Calendar by hand.

**Output contract.** One JSON object on stdout, always. **Exit 0** = success; **exit 2** = a usage
refusal (today exactly one: `get-event`/`delete-event` invoked without `--id`, which prints
`{"ok": false, "error": "<cmd> needs --id"}` and never touches the API); **exit 1** = a handled
failure, `{"ok": false, "error": "..."}` — a missing or unreadable `--env-file` is one of these, same
as any other configuration failure (see the failure table below). `--account` is `required=True`, so
omitting it is an argparse exit **2** with no JSON at all — that's the one path with no JSON.

### `--start`/`--end` and the owner-offset trap

**A bare `--start 2026-08-19` (or `--end`) is read as that date's calendar day in the owner's
timezone**, not a naive instant sent to Google as UTC — the script stamps it with the owner's UTC
offset for that date (resolved through `tz_common`, so DST is that date's own, not today's). The trap
this closes: a naive date read as UTC shifts the whole window by the owner's offset, so the evening's
events fall into "tomorrow" and last night's late events leak into "today". So:

```sh
# today, owner-local
python seneschal/scripts/gcal_api.py events --account <label> --env-file seneschal/scripts/google.env \
  --start 2026-08-19 --end 2026-08-19
```

asks for the whole owner-local calendar day of 2026-08-19, correctly. An explicit ISO datetime
(`--start 2026-08-19T00:00:00+02:00`) still works exactly as written and needs no offset arithmetic
from the caller — pass one only when you actually mean a specific instant, not a calendar day, and
never hand-compute the owner's offset into one to mean "today".

`--days N` is a **rolling window from now**, not a calendar day: `--days 1` means the next 24 hours.
It is fine for "what's next", wrong for "what's on today".

### One calendar per call

`events` reads **one** `--calendar` at a time (default `primary`). The include-set below therefore
costs one invocation per calendar — issue them as one parallel batch, not serially. Only `freebusy`
accepts a comma list.

### What each failure actually looks like

Name the one you got; don't paraphrase it into "not connected".

| Symptom | What it means | What to say |
|---|---|---|
| `{"ok": false, "error": "[Errno 2] No such file or directory: '...google.env'"}`, exit 1 | the bridge isn't configured on this host — a missing/unreadable `--env-file` | "the Google bridge isn't set up here (`google.env` missing)" |
| `{"ok": false, "error": "Missing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET …"}` | env file present but empty/partial | same, plus point at `GOOGLE_SETUP.md` |
| `{"ok": false, "error": "No stored refresh token for account '<label>'. Known accounts: …"}` | consent never run for that label, or the token was revoked | "your Google connection needs re-consenting" — `google_auth.py`, and **that's the owner's to run**, not the assistant's |
| `{"ok": false, "error": "…invalid_grant…"}` (from the token refresh) | refresh token expired — usually the consent screen fell back to *Testing*, which caps them at 7 days | same as above; `GOOGLE_SETUP.md` §1 |
| `{"ok": true, "count": 0}` | **a working calendar with nothing on it** | "nothing on your calendar today" — **not** a failure |

## Briefing usage (read-only)

- "Today" = the owner-local calendar day (`owner.timezone` in `persona/identity.json`, via
  `tz_common`). Pull all of today's events; if it's early morning, also note the **next** upcoming
  event.
- For each event surface: time (local), title, and whether the owner has accepted/declined (so the
  brief can flag un-answered invites for Triage). RSVP status is in the `--raw` payload
  (`attendees[].responseStatus`); the slimmed default doesn't carry it.
- Treat all-day events and focus blocks distinctly from meetings. The bridge's slimmed shape marks
  all-day with `"all_day": true`.

## Gotchas

- **Timezone:** always present times in the owner's timezone regardless of the event's stored zone.
- **Which calendar:** an owner often has **many calendars**; the brief must decide which ones count as
  "their schedule." Work through the roster once (`list_calendars` / `list-calendars`) and record the
  include-set in the local copy of this file — which calendars are the owner's comes from that
  confirmed configuration, never from guessing at names. An illustrative example roster:
  - **Primary:** `owner@example.com` (owner role) — the default (`primary`) for both doors.
  - **Work:** `owner@work.example.com` (reader role) and a shared team calendar
    (`team-cal-id@group.calendar.example.com`).
  - **Personal/life:** `Family` (gotcha: some calendars store times in **UTC** — convert carefully),
    `Health`, `Birthdays`, and a public holidays calendar (`en.usa#holiday…`-style).
  - **Task mirrors:** to-do-app calendars (e.g. `todo-cal-id@example.com`) surface task-like all-day
    items. These overlap with the store's tasks — de-dupe or exclude to avoid double-counting.
  - **Noise / not the owner's:** shared game/guild/community calendars — likely exclude from a normal
    brief.
  - **Confirmed include-set for briefs (example):** primary + work + team + Family + Health + Holidays.
    **Exclude** game/community calendars and the **task-mirror** calendars (they double-count the
    store's tasks).
- **Invites vs events:** an invite the owner hasn't answered still appears in the event list; check
  RSVP status before assuming attendance.
- **A reader-role calendar is read-only.** A calendar subscribed as `accessRole: reader` (a work
  calendar shared into the owner's account, typically) shows its invites and the owner's
  `needsAction` status, but the assistant cannot RSVP there through either door. Surface the invite
  and tell the owner they need to answer from that calendar's own account.

## The gate — unchanged by any of this

**Reads are act-low** on both doors: `list_calendars`/`list-calendars`, `list_events`/`events`,
`get_event`/`get-event`, `freebusy`, `suggest_time`. Just run them.

**Every write and every RSVP is ask-high, permanently** — `create_event`/`create-event`,
`update_event`, `delete_event`/`delete-event`, `respond_to_event`. Draft and hold
(`autonomy-policy.md`); nothing on the owner's calendar moves without their yes. Bridge writes pass
the send gate (`comms-mapping.md` → *The send gate*): an event with attendees needs an approved row,
and `delete-event` gates on the event id. Having a second door does **not** widen what the assistant
may do unasked — it only widens what it can *read*.
