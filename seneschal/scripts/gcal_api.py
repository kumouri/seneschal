#!/usr/bin/env python3
"""Google Calendar v3 bridge for the assistant — read availability & events, draft events. Stdlib only.

Sits on the shared OAuth plumbing in `google_common.py`; every call is keyed by an account label
(--account) that maps to a stored refresh token. Reads are act-low (just run them). Creating/patching/
deleting events is act-high — the assistant draft-and-holds those for the owner; this script performs the write only
when actually invoked with a write subcommand.

  python gcal_api.py list-calendars --account work --env-file google.env
  python gcal_api.py events         --account work --days 7 --env-file google.env
  python gcal_api.py events         --account work --start 2026-07-10 --end 2026-07-11 --env-file google.env
  python gcal_api.py freebusy       --account work --days 3 --env-file google.env
  python gcal_api.py get-event      --account work --id <eventId> --env-file google.env
  python gcal_api.py create-event   --account work --json '{"summary":"Focus block","start":{...}}' --env-file google.env
  python gcal_api.py create-event   --account work --summary "Coffee" --start 2026-07-11T15:00:00-05:00 \
                                    --end 2026-07-11T15:30:00-05:00 --env-file google.env
  python gcal_api.py delete-event   --account work --id <eventId> --env-file google.env

All output is one JSON object. Exit 0 on success, non-zero on failure.

THIS IS A LIVE CALENDAR DOOR, not a fallback nobody reaches. Calendar is TWO DOORS and the order is
the rule: a Calendar MCP if the session has one, ELSE this script, and "no calendar" only when BOTH
failed — naming which. On a host with no Calendar MCP this is door 1 in practice. And
`{"ok": true, "count": 0}` IS AN EMPTY DAY, not an absent integration — reporting a successful empty
read as unavailable is the bug that contract exists to prevent.

THE WRITE SURFACE IS NARROWER THAN THE MCP'S: create/delete only, NO RSVP AND NO UPDATE. An approved
RSVP on this door is handed back to the owner, never reported as done.

A BARE `--start`/`--end YYYY-MM-DD` IS AN OWNER-LOCAL CALENDAR DAY: it is stamped with the owner's
UTC offset for that date (`tz_common.utc_offset_on` — the configured `owner.timezone`, else the
machine-local clock; the DATE's own DST, not today's), so `--start 2026-08-19 --end 2026-08-19` means
the owner's calendar day, never a naive UTC window shifted by the owner's offset. An explicit ISO
datetime (with or without its own offset) still passes through untouched. `--days N` is still a
rolling window from now, never a calendar day.

THE SEND GATE: an event with attendees is an outbound send to them (Google mails each an invite), so
`create-event` classifies the attendee addresses (`send_recipients.classify_emails`; an
attendee-less event is the owner's own) and `delete-event` — whose cancellation notices go to
attendees this call never fetches — gates on the event id. Anything not owner-class needs an
approved row in `state/pending-approvals.json` (`send_gate.py`), or the command prints the refusal
and exits 3 with nothing written.

A missing or unreadable `--env-file` is a handled `{"ok": false, "error": "..."}` (exit 1) like any
other configuration failure — `load_env` runs inside `main()`'s try/except, not before it.

Contract, failure table and the calendar include-set: ../references/calendar-mapping.md.

`events` takes only ONE `--calendar` at a time (the calendar include-set is one call each, batched by
the caller); only `freebusy` accepts a comma-separated list. `google.env` holds live OAuth secrets: it
is untracked, and is never read back, printed, or committed. Setup: GOOGLE_SETUP.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import google_common as gc
import send_gate
import send_recipients
import tz_common

API = "https://www.googleapis.com/calendar/v3"


def _window(args) -> tuple[str, str]:
    """Resolve (timeMin, timeMax) as RFC-3339. --start/--end (date or datetime) override --days.

    A bare `YYYY-MM-DD` is an owner-local calendar day, not a naive instant to be stamped UTC — it
    is given that date's own owner offset (`tz_common.utc_offset_on`) before conversion. A full ISO
    datetime (its own offset or none) is left exactly as `datetime.fromisoformat` parses it."""
    def parse(s: str, end_of_day: bool) -> datetime:
        if len(s) == 10:  # bare date, same convention _build_event_body.when() uses
            d = datetime.strptime(s, "%Y-%m-%d")
            if end_of_day:
                d = d.replace(hour=23, minute=59, second=59)
            return d.replace(tzinfo=timezone(tz_common.utc_offset_on(d.date())))
        return datetime.fromisoformat(s)
    if args.start or args.end:
        start = parse(args.start, False) if args.start else gc.now_utc()
        end = parse(args.end, True) if args.end else start + timedelta(days=args.days)
        return gc.rfc3339(start), gc.rfc3339(end)
    now = gc.now_utc()
    return gc.rfc3339(now), gc.rfc3339(now + timedelta(days=args.days))


def _slim_event(e: dict) -> dict:
    start = e.get("start", {})
    end = e.get("end", {})
    return {
        "id": e.get("id"),
        "summary": e.get("summary"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": "date" in start,
        "location": e.get("location"),
        "status": e.get("status"),
        "organizer": (e.get("organizer") or {}).get("email"),
        "attendees": len(e.get("attendees", [])) or None,
        "hangout": e.get("hangoutLink"),
        "link": e.get("htmlLink"),
    }


def cmd_list_calendars(c, args) -> dict:
    res = gc.authorized_request(c, args.account, "GET", f"{API}/users/me/calendarList",
                                params={"maxResults": 250}, where="calendarList")
    cals = [{"id": i.get("id"), "summary": i.get("summary"), "primary": i.get("primary", False),
             "accessRole": i.get("accessRole"), "timeZone": i.get("timeZone")}
            for i in res.get("items", [])]
    return {"ok": True, "account": args.account, "count": len(cals), "calendars": cals}


def cmd_events(c, args) -> dict:
    time_min, time_max = _window(args)
    params = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
              "orderBy": "startTime", "maxResults": args.max}
    if args.query:
        params["q"] = args.query
    res = gc.authorized_request(c, args.account, "GET",
                                f"{API}/calendars/{args.calendar}/events", params=params, where="events.list")
    items = res.get("items", [])
    return {"ok": True, "account": args.account, "calendar": args.calendar,
            "timeMin": time_min, "timeMax": time_max, "count": len(items),
            "events": items if args.raw else [_slim_event(e) for e in items]}


def cmd_freebusy(c, args) -> dict:
    time_min, time_max = _window(args)
    calendars = [x.strip() for x in (args.calendar or "primary").split(",") if x.strip()]
    body = {"timeMin": time_min, "timeMax": time_max, "items": [{"id": cid} for cid in calendars]}
    res = gc.authorized_request(c, args.account, "POST", f"{API}/freeBusy", body=body, where="freeBusy")
    cals = {cid: v.get("busy", []) for cid, v in res.get("calendars", {}).items()}
    return {"ok": True, "account": args.account, "timeMin": time_min, "timeMax": time_max, "busy": cals}


def cmd_get_event(c, args) -> dict:
    res = gc.authorized_request(c, args.account, "GET",
                                f"{API}/calendars/{args.calendar}/events/{args.id}", where="events.get")
    return {"ok": True, "account": args.account, "event": res if args.raw else _slim_event(res)}


def _build_event_body(args) -> dict:
    if args.json:
        return json.loads(args.json)
    if not (args.summary and args.start and args.end):
        raise RuntimeError("create-event needs --json, or all of --summary/--start/--end")
    def when(v: str) -> dict:
        return {"date": v} if len(v) == 10 else {"dateTime": v}
    body = {"summary": args.summary, "start": when(args.start), "end": when(args.end)}
    if args.location:
        body["location"] = args.location
    if args.description:
        body["description"] = args.description
    return body


def _event_recipient_class(body: dict) -> str:
    """`owner` when the event body carries no attendees at all (it only ever touches the owner's own
    calendar, nobody else is notified); otherwise classify the attendee emails, the only way a
    calendar write reaches anyone beyond the owner. `--json` is the only door attendees can arrive through
    today (there is no `--attendees` flag), so a summary/start/end-only event is always `owner`."""
    attendees = body.get("attendees") or []
    emails = [a.get("email") for a in attendees if isinstance(a, dict) and a.get("email")]
    if not emails:
        return "owner"
    return send_recipients.classify_emails(*emails)


def cmd_create_event(c, args) -> dict:
    body = _build_event_body(args)
    try:
        cls = _event_recipient_class(body)
    except Exception:  # noqa: BLE001 — a classification failure logs unknown, never raises into the send
        cls = "unknown"
    # The send gate: an attendee-less event is owner-class and passes; attendees need an approved
    # row (send_gate.py) covering every non-owner address — an invite IS an outbound send to them.
    attendees = [a.get("email") for a in (body.get("attendees") or []) if isinstance(a, dict) and a.get("email")]
    verdict = send_gate.require_approval("calendar", attendees, recipient_class=cls, channel="gcal_create_event")
    send_recipients.record("gcal_create_event", cls, gate=verdict)
    if not verdict["allowed"]:
        return send_gate.refusal_payload(verdict, account=args.account)
    res = gc.authorized_request(c, args.account, "POST",
                                f"{API}/calendars/{args.calendar}/events", body=body, where="events.insert")
    return {"ok": True, "account": args.account, "created": True, "event": _slim_event(res)}


def cmd_delete_event(c, args) -> dict:
    # The event's own attendees are never fetched here (that would be a second API call on every
    # delete), so who a deleted event's cancellation notice reaches is structurally undecidable from
    # this call alone — always `unknown`. The send gate keys it on the EVENT ID (`send_gate.py grant
    # --kind calendar --recipient <eventId>`); a delete is ask-high regardless, so the grant IS the
    # owner's approval, recorded.
    verdict = send_gate.require_approval("calendar", args.id, recipient_class="unknown", channel="gcal_delete_event")
    send_recipients.record("gcal_delete_event", "unknown", gate=verdict)
    if not verdict["allowed"]:
        return send_gate.refusal_payload(verdict, account=args.account, eventId=args.id)
    gc.authorized_request(c, args.account, "DELETE",
                          f"{API}/calendars/{args.calendar}/events/{args.id}", where="events.delete")
    return {"ok": True, "account": args.account, "deleted": args.id}


def main() -> int:
    p = argparse.ArgumentParser(description="Google Calendar v3 bridge for the assistant.")
    p.add_argument("command", choices=[
        "list-calendars", "events", "freebusy", "get-event", "create-event", "delete-event"])
    p.add_argument("--account", required=True, help="account label (e.g. personal, work)")
    p.add_argument("--env-file", help="KEY=VALUE file with GOOGLE_* settings (kept untracked)")
    p.add_argument("--calendar", default="primary", help="calendar id (default primary; comma list for freebusy)")
    p.add_argument("--days", type=int, default=7, help="window length in days (default 7)")
    p.add_argument("--start", help="window start (YYYY-MM-DD or ISO datetime)")
    p.add_argument("--end", help="window end (YYYY-MM-DD or ISO datetime)")
    p.add_argument("--max", type=int, default=50, help="max events (default 50)")
    p.add_argument("--query", help="free-text event search (events)")
    p.add_argument("--id", help="event id (get-event / delete-event)")
    p.add_argument("--json", help="full event body as JSON (create-event)")
    p.add_argument("--summary", help="event title (create-event without --json)")
    p.add_argument("--description", help="event description (create-event without --json)")
    p.add_argument("--location", help="event location (create-event without --json)")
    p.add_argument("--raw", action="store_true", help="emit full API objects instead of slimmed ones")
    args = p.parse_args()

    handlers = {
        "list-calendars": cmd_list_calendars, "events": cmd_events, "freebusy": cmd_freebusy,
        "get-event": cmd_get_event, "create-event": cmd_create_event, "delete-event": cmd_delete_event,
    }
    if args.command in ("get-event", "delete-event") and not args.id:
        print(json.dumps({"ok": False, "error": f"{args.command} needs --id"}))
        return 2
    try:
        c = gc.cfg(gc.load_env(args.env_file))
        res = handlers[args.command](c, args)
        print(json.dumps(res, ensure_ascii=False))
        if res.get("refused") == "send_gate":
            return send_gate.EXIT_REFUSED  # the send gate refused: nothing left the machine
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "account": args.account, "command": args.command, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
