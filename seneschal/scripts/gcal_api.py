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
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

import google_common as gc

API = "https://www.googleapis.com/calendar/v3"


def _window(args) -> tuple[str, str]:
    """Resolve (timeMin, timeMax) as RFC-3339. --start/--end (date or datetime) override --days."""
    def parse(s: str, end_of_day: bool) -> datetime:
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            d = datetime.strptime(s, "%Y-%m-%d")
            return d.replace(hour=23, minute=59, second=59) if end_of_day else d
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


def cmd_create_event(c, args) -> dict:
    body = _build_event_body(args)
    res = gc.authorized_request(c, args.account, "POST",
                                f"{API}/calendars/{args.calendar}/events", body=body, where="events.insert")
    return {"ok": True, "account": args.account, "created": True, "event": _slim_event(res)}


def cmd_delete_event(c, args) -> dict:
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

    c = gc.cfg(gc.load_env(args.env_file))
    handlers = {
        "list-calendars": cmd_list_calendars, "events": cmd_events, "freebusy": cmd_freebusy,
        "get-event": cmd_get_event, "create-event": cmd_create_event, "delete-event": cmd_delete_event,
    }
    if args.command in ("get-event", "delete-event") and not args.id:
        print(json.dumps({"ok": False, "error": f"{args.command} needs --id"}))
        return 2
    try:
        print(json.dumps(handlers[args.command](c, args), ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "account": args.account, "command": args.command, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
