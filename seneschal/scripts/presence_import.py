#!/usr/bin/env python3
"""Import presence events (NDJSON from the phone's ``PresenceSyncWorker``) into ``state/presence.db`` and
recompute ``state/presence-context.json``.

This is the desktop end of the presence feed's transport — a sibling of ``health_import.import_ndjson``.
It lands events in one shape whatever their source and refreshes the derived context snapshot, so the
daemon's next loop sees the new level-state. **Idempotent:** each event carries a stable id (an explicit
``uuid``, else one derived from ``kind:label:transition:ts_ms``), so re-POSTing a batch never duplicates.

Wire format — one JSON object per line, ``t`` = type. Unknown ``t`` is skipped, not fatal, so the format
can grow without breaking older importers:

  {"t":"geofence","place":"home","transition":"enter"|"exit","ts_ms":..,"offset_min":-300,"uuid":opt}
  {"t":"activity","activity":"in_vehicle"|"still"|"walking"|"running"|"on_bicycle"|"unknown",
                  "transition":"enter"|"exit","ts_ms":..,"offset_min":-300,"confidence":opt,"uuid":opt}
  {"t":"sleep","state":"asleep"|"awake","ts_ms":..,"offset_min":-300,"confidence":opt,"uuid":opt}

Coordinates never appear here: the phone resolves geofences on-device and sends only the place name.
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from presence_common import (  # noqa: E402
    DEFAULT_DB, DEFAULT_STATE_DIR, connect, epoch_ms_to_utc, iso, now_iso,
    prune_events, read_context, recompute_context, write_context,
)

_TRANSITIONS = {"enter", "exit"}


def _event_id(rec: dict, kind: str, label: str, transition: str, ts_ms: int) -> str:
    uuid = rec.get("uuid")
    return str(uuid) if uuid else f"{kind}:{label}:{transition}:{ts_ms}"


def _insert(conn, rec, kind, label, transition, ts_ms, off, confidence) -> int:
    ts_ms = int(ts_ms)
    utc = epoch_ms_to_utc(ts_ms)
    eid = _event_id(rec, kind, label, transition, ts_ms)
    conn.execute(
        "INSERT OR REPLACE INTO events "
        "(event_id, kind, label, transition, ts_utc, ts_ms, tz_offset_min, confidence, ingested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (eid, kind, label, transition, iso(utc), ts_ms, int(off),
         None if confidence is None else int(confidence), now_iso()))
    return 1


def _geofence(conn, rec) -> int:
    place = str(rec["place"]).strip()
    transition = str(rec["transition"]).strip().lower()
    if not place or transition not in _TRANSITIONS:
        raise ValueError("bad geofence event")
    return _insert(conn, rec, "geofence", place, transition, rec["ts_ms"],
                   rec["offset_min"], rec.get("confidence"))


def _activity(conn, rec) -> int:
    activity = str(rec["activity"]).strip().lower()
    transition = str(rec["transition"]).strip().lower()
    if not activity or transition not in _TRANSITIONS:
        raise ValueError("bad activity event")
    # Unknown activity labels are allowed through (the Activity Recognition API may add types).
    return _insert(conn, rec, "activity", activity, transition, rec["ts_ms"],
                   rec["offset_min"], rec.get("confidence"))


def _sleep(conn, rec) -> int:
    state = str(rec["state"]).strip().lower()
    if state not in ("asleep", "awake"):
        raise ValueError("bad sleep state")
    return _insert(conn, rec, "sleep", state, "enter", rec["ts_ms"],
                   rec["offset_min"], rec.get("confidence"))


_HANDLERS = {"geofence": _geofence, "activity": _activity, "sleep": _sleep}


def import_events(conn, lines, state_dir: str = DEFAULT_STATE_DIR, commit: bool = True) -> dict:
    """Import presence NDJSON events, then recompute + write the context snapshot. Returns per-type counts
    plus the recomputed ``context`` dict.

    ``lines`` is an iterable of strings, or a path to an ``.ndjson`` file. Malformed lines are skipped and
    counted under ``"bad"`` — one bad line from the phone must never abort a whole batch.
    """
    if isinstance(lines, str) and os.path.exists(lines):
        with open(lines, encoding="utf-8") as fh:
            lines = fh.readlines()
    counts: dict = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            fn = _HANDLERS.get(rec.get("t"))
            if fn is None:
                counts["skipped"] = counts.get("skipped", 0) + 1
                continue
            counts[rec["t"]] = counts.get(rec["t"], 0) + fn(conn, rec)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            counts["bad"] = counts.get("bad", 0) + 1
    if commit:
        conn.commit()
    ctx = recompute_context(conn)
    write_context(ctx, state_dir)
    counts["context"] = ctx
    return counts


def status(conn) -> dict:
    def one(sql, *a):
        row = conn.execute(sql, a).fetchone()
        return row[0] if row and row[0] is not None else None
    return {
        "events": one("SELECT COUNT(*) FROM events") or 0,
        "geofence": one("SELECT COUNT(*) FROM events WHERE kind='geofence'") or 0,
        "activity": one("SELECT COUNT(*) FROM events WHERE kind='activity'") or 0,
        "sleep": one("SELECT COUNT(*) FROM events WHERE kind='sleep'") or 0,
        "first_ts": one("SELECT MIN(ts_utc) FROM events"),
        "last_ts": one("SELECT MAX(ts_utc) FROM events"),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Import presence events (NDJSON) into the assistant's presence store.")
    p.add_argument("--ndjson", help="path to a .ndjson file of presence events to import")
    p.add_argument("--status", action="store_true", help="print what the store holds and exit")
    p.add_argument("--context", action="store_true", help="print the current presence-context.json and exit")
    p.add_argument("--prune-days", type=int, default=None,
                   help="delete presence events older than N days (retention; run nightly in Dream), then exit")
    p.add_argument("--db", default=DEFAULT_DB, help="path to presence.db")
    args = p.parse_args()

    conn = connect(args.db)
    try:
        if args.status:
            print(json.dumps(status(conn), indent=2))
            return 0
        if args.context:
            print(json.dumps(read_context(), indent=2))
            return 0
        if args.prune_days is not None:
            removed = prune_events(conn, args.prune_days)
            write_context(recompute_context(conn))  # keep the snapshot consistent after a prune
            print(json.dumps({"ok": True, "pruned": removed, "keep_days": args.prune_days}))
            return 0
        if args.ndjson:
            counts = import_events(conn, args.ndjson)
            print(json.dumps({k: v for k, v in counts.items() if k != "context"}, indent=2))
            return 0
        p.print_help()
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
