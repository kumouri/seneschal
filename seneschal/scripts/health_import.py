#!/usr/bin/env python3
"""Import a Samsung Health export zip into the assistant's local health cache (``seneschal/state/health.db``).

Samsung offers no API and no scheduled export: the personal-data zip is produced by a manual tap inside
the phone app. What *can* be automated is everything after that tap. Point ``--watch-dir`` at wherever
the zip lands (Downloads, a synced folder) and this script picks up any export it hasn't seen, imports
it, and exits. Run it from a scheduled task and the manual step is one tap a month rather than an
afternoon of CSV wrangling. The properly automatic path -- Health Connect on the phone, streaming
nightly -- is designed in ``HEALTH_SETUP.md``; this importer is the target either way.

Imports are **idempotent**: every row carries Samsung's ``datauuid``, so re-importing an overlapping
export upserts rather than duplicating. Exports are cumulative, so the newest zip alone rebuilds
everything; older zips are still worth importing if they cover days a later export has aged out of
(the fine-grained pedometer table only reaches back ~30 days).

This is **act-low**: it reads a file the owner already exported and writes a gitignored local cache. It
sends nothing and touches nothing else.

Usage:
  python health_import.py --zip ~/Downloads/samsunghealth_..._20260707152802.zip
  python health_import.py --watch-dir ~/Downloads          # import any export not yet seen
  python health_import.py --watch-dir ~/Downloads --force   # re-import even if seen
  python health_import.py --status                          # what's in the cache
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from datetime import datetime, timezone

import tz_common
from health_common import (
    DEFAULT_DB,
    MIN_SESSION_MIN,
    SLEEP_STAGES,
    SamsungExportError,
    col,
    connect,
    day_time_to_date,
    epoch_ms_to_utc,
    export_id,
    find_exports,
    num,
    open_table,
    parse_offset,
    parse_ts,
    sleep_day,
    to_local,
)

_ISO = "%Y-%m-%dT%H:%M:%S"


def _iso(dt) -> str:
    return dt.strftime(_ISO)


def _times(row, prefix=""):
    """The (utc_start, utc_end, offset_min, local_start) quad every table shares. ``None`` if unusable."""
    start = parse_ts(row.get(col(prefix, "start_time")))
    if start is None:
        return None
    end = parse_ts(row.get(col(prefix, "end_time")))
    off = parse_offset(row.get(col(prefix, "time_offset")) or "")
    return start, end, off, to_local(start, off)


# --------------------------------------------------------------------------------------------------
# Per-table importers. Each returns the number of rows written.
# --------------------------------------------------------------------------------------------------

def _import_sessions(conn, zf) -> int:
    p = "com.samsung.health.sleep."
    rows = []
    for r in open_table(zf, "com.samsung.shealth.sleep."):
        t = _times(r, p)
        if not t or t[1] is None:
            continue
        start, end, off, local = t
        duration = (end - start).total_seconds() / 60.0
        if duration < MIN_SESSION_MIN:
            continue
        rows.append((
            r[col(p, "datauuid")], sleep_day(local).isoformat(), _iso(start), _iso(end), off,
            _iso(local), _iso(to_local(end, off)), duration,
            num(r.get("efficiency")), num(r.get("sleep_score")),
            _ms_to_min(r.get("sleep_latency")), _ms_to_min(r.get("bedtime_detection_delay")),
            _ms_to_min(r.get("wakeup_time_detection_delay")),
            num(r.get("mental_recovery")), num(r.get("physical_recovery")),
            num(r.get("movement_awakening")), r.get("combined_id") or None,
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO sleep_session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _ms_to_min(value) -> float | None:
    v = num(value)
    return None if v is None else v / 60000.0


def _import_stages(conn, zf) -> int:
    rows = []
    for r in open_table(zf, "com.samsung.health.sleep_stage"):
        t = _times(r)
        if not t or t[1] is None:
            continue
        start, end, off, local = t
        stage = SLEEP_STAGES.get((r.get("stage") or "").strip())
        if not stage:
            continue
        rows.append((r["datauuid"], r["sleep_id"], sleep_day(local).isoformat(),
                     _iso(start), _iso(end), off, stage))
    conn.executemany("INSERT OR REPLACE INTO sleep_stage VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def _import_heart_rate(conn, zf) -> int:
    p = "com.samsung.health.heart_rate."
    rows = []
    for r in open_table(zf, "com.samsung.shealth.tracker.heart_rate"):
        t = _times(r, p)
        if not t:
            continue
        start, end, off, local = t
        rows.append((
            r[col(p, "datauuid")], _iso(start), _iso(end) if end else None, off,
            local.date().isoformat(),
            num(r.get(col(p, "heart_rate"))), num(r.get(col(p, "min"))), num(r.get(col(p, "max"))),
            1 if r.get(col(p, "binning_data")) else 0,
        ))
    conn.executemany("INSERT OR REPLACE INTO heart_rate VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _import_stress(conn, zf) -> int:
    rows = []
    for r in open_table(zf, "com.samsung.shealth.stress."):
        t = _times(r)
        if not t:
            continue
        start, _end, off, local = t
        rows.append((r["datauuid"], _iso(start), off, local.date().isoformat(),
                     num(r.get("score")), num(r.get("min")), num(r.get("max"))))
    conn.executemany("INSERT OR REPLACE INTO stress VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def _import_spo2(conn, zf) -> int:
    p = "com.samsung.health.oxygen_saturation."
    rows = []
    for r in open_table(zf, "com.samsung.shealth.tracker.oxygen_saturation"):
        t = _times(r, p)
        if not t:
            continue
        start, _end, off, local = t
        rows.append((r[col(p, "datauuid")], _iso(start), off, sleep_day(local).isoformat(),
                     num(r.get(col(p, "spo2"))), num(r.get(col(p, "min"))), num(r.get(col(p, "max")))))
    conn.executemany("INSERT OR REPLACE INTO spo2 VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def _import_skin_temp(conn, zf) -> int:
    rows = []
    for r in open_table(zf, "com.samsung.health.skin_temperature"):
        t = _times(r)
        if not t:
            continue
        start, _end, off, local = t
        rows.append((r["datauuid"], _iso(start), off, sleep_day(local).isoformat(),
                     num(r.get("temperature")), num(r.get("min")), num(r.get("max"))))
    conn.executemany("INSERT OR REPLACE INTO skin_temp VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def _import_steps_daily(conn, zf) -> int:
    """``step_daily_trend`` holds one row per (day, source). ``source_type = -2`` is Samsung's own
    all-sources rollup; prefer it, and fall back to the largest count for days that lack one."""
    best: dict[str, tuple[int, int, float, float]] = {}
    for r in open_table(zf, "com.samsung.shealth.step_daily_trend"):
        day = day_time_to_date(r.get("day_time") or "")
        if not day:
            continue
        count = int(num(r.get("count")) or 0)
        priority = 1 if (r.get("source_type") or "").strip() == "-2" else 0
        key = day.isoformat()
        cur = best.get(key)
        if cur is None or (priority, count) > (cur[0], cur[1]):
            best[key] = (priority, count, num(r.get("distance")) or 0.0, num(r.get("calorie")) or 0.0)
    rows = [(d, c, dist, cal) for d, (_p, c, dist, cal) in best.items()]
    conn.executemany("INSERT OR REPLACE INTO steps_daily VALUES (?,?,?,?)", rows)
    return len(rows)


def _import_medication(conn, zf) -> int:
    rows = []
    for r in open_table(zf, "com.samsung.shealth.medication.log"):
        raw = r.get("dosage_date")
        if not raw:
            continue
        try:  # epoch-ms of local midnight, stored as-if-UTC
            day = datetime.fromtimestamp(int(raw) / 1000.0, tz=timezone.utc).date()
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        created = parse_ts(r.get("create_time") or "")
        rows.append((r["datauuid"], day.isoformat(), _iso(created) if created else None))
    conn.executemany("INSERT OR REPLACE INTO medication_log VALUES (?,?,?)", rows)
    return len(rows)


def _import_weight(conn, zf) -> int:
    rows = []
    for r in open_table(zf, "com.samsung.health.weight"):
        t = _times(r)
        if not t:
            continue
        start, _end, _off, local = t
        kg = num(r.get("weight"))
        if kg is None:
            continue
        rows.append((r["datauuid"], _iso(start), local.date().isoformat(), kg))
    conn.executemany("INSERT OR REPLACE INTO weight VALUES (?,?,?,?)", rows)
    return len(rows)


# --------------------------------------------------------------------------------------------------
# Per-minute JSON export (the separate jsons.zip): movement + heart-rate binning series.
# --------------------------------------------------------------------------------------------------

def _uuid_of(json_name: str) -> str:
    """``jsons/<type>/<n>/<uuid>.binning_data.json`` → ``<uuid>`` (strip every suffix)."""
    stem = json_name.rsplit("/", 1)[-1]
    return stem.split(".")[0]


def _offset_map(csv_zf, table_key: str, prefix: str) -> dict[str, int]:
    """uuid → recorded ``time_offset`` (minutes) from a CSV table, so per-minute rows get the *true*
    offset (incl. travel days), not just the owner-timezone approximation."""
    out: dict[str, int] = {}
    try:
        for r in open_table(csv_zf, table_key):
            uu = r.get(col(prefix, "datauuid"))
            if uu:
                out[uu] = parse_offset(r.get(col(prefix, "time_offset")) or "")
    except SamsungExportError:
        pass
    return out


def _flush(conn, sql, batch):
    if batch:
        conn.executemany(sql, batch)
        batch.clear()


def _import_movement(conn, json_zf, offsets) -> int:
    sql = "INSERT OR REPLACE INTO movement VALUES (?,?,?,?,?)"
    names = [n for n in json_zf.namelist()
             if "/com.samsung.health.movement/" in n and n.endswith(".json")]
    batch, total = [], 0
    for name in names:
        uu = _uuid_of(name)
        try:
            bins = json.loads(json_zf.read(name))
        except (json.JSONDecodeError, KeyError):
            continue
        for b in bins:
            start = b.get("start_time")
            if start is None:
                continue
            utc = epoch_ms_to_utc(start)
            off = offsets.get(uu)
            if off is None:
                off = tz_common.offset_minutes(utc)
            local = to_local(utc, off)
            batch.append((uu, _iso(utc), off, sleep_day(local).isoformat(), num(b.get("activity_level"))))
        total += 1
        if len(batch) >= 5000:
            _flush(conn, sql, batch)
    _flush(conn, sql, batch)
    return total


def _import_hr_minute(conn, json_zf, offsets) -> int:
    sql = "INSERT OR REPLACE INTO hr_minute VALUES (?,?,?,?,?,?,?,?)"
    names = [n for n in json_zf.namelist()
             if "/com.samsung.shealth.tracker.heart_rate/" in n and n.endswith(".json")]
    batch, total = [], 0
    for name in names:
        uu = _uuid_of(name)
        try:
            bins = json.loads(json_zf.read(name))
        except (json.JSONDecodeError, KeyError):
            continue
        for b in bins:
            start = b.get("start_time")
            if start is None:
                continue
            utc = epoch_ms_to_utc(start)
            off = offsets.get(uu)
            if off is None:
                off = tz_common.offset_minutes(utc)
            local = to_local(utc, off)
            batch.append((uu, _iso(utc), off, sleep_day(local).isoformat(), local.date().isoformat(),
                          num(b.get("heart_rate")), num(b.get("heart_rate_min")), num(b.get("heart_rate_max"))))
        total += 1
        if len(batch) >= 5000:
            _flush(conn, sql, batch)
    _flush(conn, sql, batch)
    return total


def import_jsons(conn, json_path: str, csv_path: str | None = None, verbose: bool = True) -> dict:
    """Import the per-minute JSON export. ``csv_path`` (the paired samsunghealth_*.zip) supplies the true
    per-session offsets; without it we fall back to the owner's timezone at that instant
    (``tz_common.offset_minutes`` — fine unless the owner was travelling). Returns row counts per table."""
    offsets: dict[str, int] = {}
    if csv_path:
        with zipfile.ZipFile(csv_path) as czf:
            offsets.update(_offset_map(czf, "com.samsung.health.movement", "com.samsung.health.movement."))
            offsets.update(_offset_map(czf, "com.samsung.shealth.tracker.heart_rate",
                                       "com.samsung.health.heart_rate."))
    counts = {}
    with zipfile.ZipFile(json_path) as jzf:
        if verbose:
            print("  movement (per-minute) ...", file=sys.stderr)
        counts["movement_files"] = _import_movement(conn, jzf, offsets)
        if verbose:
            print("  heart rate (per-minute) ...", file=sys.stderr)
        counts["hr_minute_files"] = _import_hr_minute(conn, jzf, offsets)
    conn.commit()
    return counts


# --------------------------------------------------------------------------------------------------
# Live feed: NDJSON from the phone's Health Connect sync (one JSON object per line).
#
# This is the format `HealthSyncWorker` (phone/android) POSTs to the desktop listener. It lands in the
# SAME tables as the zip importer, so the dashboard is identical whichever way the data arrived. Health
# Connect gives real per-record zone offsets (better than the zip), absolute epoch-ms times, and a stable
# record id for idempotent upserts. Record shapes (`t` = type):
#   {"t":"sleep_session","uuid":..,"start_ms":..,"end_ms":..,"offset_min":-300,
#    "stages":[["deep",start_ms,end_ms],["light",..]], "score":..,"efficiency":..}
#   {"t":"hr",   "uuid":..,"start_ms":..,"offset_min":-300,"bpm":..}
#   {"t":"spo2", "uuid":..,"start_ms":..,"offset_min":-300,"pct":..}
#   {"t":"steps","date":"YYYY-MM-DD","count":..,"distance_m":..,"calorie":..}
# Unknown types are skipped, not fatal, so the wire format can grow without breaking older importers.
# --------------------------------------------------------------------------------------------------

def _live_sleep(conn, rec) -> int:
    off = int(rec["offset_min"])
    start = epoch_ms_to_utc(rec["start_ms"])
    end = epoch_ms_to_utc(rec["end_ms"])
    local = to_local(start, off)
    duration = (end - start).total_seconds() / 60.0
    if duration < MIN_SESSION_MIN:
        return 0
    conn.execute(
        "INSERT OR REPLACE INTO sleep_session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["uuid"], sleep_day(local).isoformat(), _iso(start), _iso(end), off, _iso(local),
         _iso(to_local(end, off)), duration, num(rec.get("efficiency")), num(rec.get("score")),
         None, None, None, None, None, None, rec.get("combined_id")))
    stage_rows = []
    for i, (stage, s_ms, e_ms) in enumerate(rec.get("stages", [])):
        if stage not in SLEEP_STAGES.values():
            continue
        ss, se = epoch_ms_to_utc(s_ms), epoch_ms_to_utc(e_ms)
        stage_rows.append((f"{rec['uuid']}:{i}", rec["uuid"], sleep_day(to_local(ss, off)).isoformat(),
                           _iso(ss), _iso(se), off, stage))
    conn.executemany("INSERT OR REPLACE INTO sleep_stage VALUES (?,?,?,?,?,?,?)", stage_rows)
    return 1


def _live_hr(conn, rec) -> int:
    off = int(rec["offset_min"])
    utc = epoch_ms_to_utc(rec["start_ms"])
    local = to_local(utc, off)
    bpm = num(rec.get("bpm"))
    conn.execute("INSERT OR REPLACE INTO hr_minute VALUES (?,?,?,?,?,?,?,?)",
                 (rec["uuid"], _iso(utc), off, sleep_day(local).isoformat(), local.date().isoformat(),
                  bpm, bpm, bpm))
    return 1


def _live_spo2(conn, rec) -> int:
    off = int(rec["offset_min"])
    utc = epoch_ms_to_utc(rec["start_ms"])
    pct = num(rec.get("pct"))
    conn.execute("INSERT OR REPLACE INTO spo2 VALUES (?,?,?,?,?,?,?)",
                 (rec["uuid"], _iso(utc), off, sleep_day(to_local(utc, off)).isoformat(), pct, pct, pct))
    return 1


def _live_steps(conn, rec) -> int:
    conn.execute("INSERT OR REPLACE INTO steps_daily VALUES (?,?,?,?)",
                 (rec["date"], int(rec.get("count") or 0), num(rec.get("distance_m")),
                  num(rec.get("calorie"))))
    return 1


_LIVE = {"sleep_session": _live_sleep, "hr": _live_hr, "spo2": _live_spo2, "steps": _live_steps}


def import_ndjson(conn, lines, commit: bool = True) -> dict:
    """Import NDJSON records (an iterable of strings, or a path to a .ndjson file) from the live feed.

    Returns per-type counts. Malformed lines are skipped and counted under ``"bad"`` — one bad line from
    the phone must never abort a whole sync batch.
    """
    if isinstance(lines, str) and os.path.exists(lines):
        with open(lines, encoding="utf-8") as fh:
            lines = fh.readlines()
    counts: dict[str, int] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            fn = _LIVE.get(rec.get("t"))
            if fn is None:
                counts["skipped"] = counts.get("skipped", 0) + 1
                continue
            counts[rec["t"]] = counts.get(rec["t"], 0) + fn(conn, rec)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            counts["bad"] = counts.get("bad", 0) + 1
    if commit:
        conn.commit()
    return counts


#: Order matters only for the progress line; each importer is independent. A table missing from an
#: export (Samsung drops types you never recorded) is skipped, not fatal.
IMPORTERS = (
    ("sleep_session", _import_sessions),
    ("sleep_stage", _import_stages),
    ("heart_rate", _import_heart_rate),
    ("stress", _import_stress),
    ("spo2", _import_spo2),
    ("skin_temp", _import_skin_temp),
    ("steps_daily", _import_steps_daily),
    ("medication_log", _import_medication),
    ("weight", _import_weight),
)


def import_zip(conn, path: str, verbose: bool = True) -> dict:
    eid = export_id(path)
    counts: dict[str, int] = {}
    with zipfile.ZipFile(path) as zf:
        for name, fn in IMPORTERS:
            try:
                counts[name] = fn(conn, zf)
            except SamsungExportError as e:
                counts[name] = 0
                if verbose:
                    print(f"  - {name}: skipped ({e})", file=sys.stderr)
    total = sum(counts.values())
    conn.execute("INSERT OR REPLACE INTO exports VALUES (?,?,?,?)",
                 (eid, os.path.basename(path), datetime.now(timezone.utc).strftime(_ISO + "Z"), total))
    conn.commit()
    return {"export_id": eid, "rows": total, "tables": counts}


def status(conn) -> dict:
    def one(sql, *a):
        row = conn.execute(sql, a).fetchone()
        return row[0] if row and row[0] is not None else None

    return {
        "exports": [dict(r) for r in conn.execute(
            "SELECT export_id, filename, imported_at, rows FROM exports ORDER BY export_id")],
        "sleep_sessions": one("SELECT COUNT(*) FROM sleep_session"),
        "sleep_days": one("SELECT COUNT(DISTINCT sleep_day) FROM sleep_session"),
        "first_day": one("SELECT MIN(sleep_day) FROM sleep_session"),
        "last_day": one("SELECT MAX(sleep_day) FROM sleep_session"),
        "heart_rate_rows": one("SELECT COUNT(*) FROM heart_rate"),
        "step_days": one("SELECT COUNT(*) FROM steps_daily"),
        "movement_minutes": one("SELECT COUNT(*) FROM movement"),
        "hr_minutes": one("SELECT COUNT(*) FROM hr_minute"),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Import a Samsung Health export into the assistant's health cache.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--zip", help="path to a samsunghealth_*.zip")
    src.add_argument("--watch-dir", help="import every export in this dir that hasn't been seen")
    p.add_argument("--jsons", help="path to the per-minute JSON export zip (movement + heart rate). "
                                   "Pair with --zip for accurate offsets; watch-dir auto-detects it.")
    p.add_argument("--force", action="store_true", help="re-import exports already recorded")
    p.add_argument("--status", action="store_true", help="print what the cache holds and exit")
    p.add_argument("--db", default=DEFAULT_DB, help="path to health.db")
    args = p.parse_args()

    conn = connect(args.db)

    if args.status or not (args.zip or args.watch_dir or args.jsons):
        print(json.dumps(status(conn), indent=2))
        return 0

    csv_paths = [args.zip] if args.zip else (find_exports(args.watch_dir) if args.watch_dir else [])
    json_path = args.jsons
    if args.watch_dir and not json_path:  # auto-detect the per-minute zip alongside the CSV export
        for name in os.listdir(args.watch_dir):
            if "json" in name.lower() and name.lower().endswith(".zip"):
                json_path = os.path.join(args.watch_dir, name)
                break

    seen = {r["export_id"] for r in conn.execute("SELECT export_id FROM exports")}
    results = []
    for path in csv_paths:
        try:
            eid = export_id(path)
        except SamsungExportError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
        if eid in seen and not args.force:
            continue
        print(f"importing {os.path.basename(path)} ...", file=sys.stderr)
        results.append(import_zip(conn, path))

    if json_path:
        print(f"importing per-minute JSON {os.path.basename(json_path)} ...", file=sys.stderr)
        pm = import_jsons(conn, json_path, csv_path=(csv_paths[0] if csv_paths else args.zip))
        results.append({"jsons": os.path.basename(json_path), **pm})

    print(json.dumps({"ok": True, "imported": results, **status(conn)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
