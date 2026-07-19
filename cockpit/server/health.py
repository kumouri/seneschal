"""Read-only, tolerant readers for the cockpit's v4 Health / workout / meal panels
(cockpit-spec.md "Health pipeline extension (v4)").

Two independent data sources, both read-only:

- **`<state dir>/health.db`** — the SAME sqlite cache `seneschal/scripts/health_import.py` writes (see
  its module docstring + `health_common.SCHEMA` for the authoritative schema). `sleep_session` already
  carries a local calendar date (`sleep_day`) computed from each record's own recorded `tz_offset_min`
  at import time — this module never re-derives timezones, it only *groups* and *rolls up* what's
  already local. The one place this module DOES need "what day is it" is the "this week" / "today"
  reference point for the workouts weekly rollup and the summary card, which follows the house rule
  (rule 5: date logic runs in the OWNER's timezone, never UTC — after-midnight counts as the prior
  day). Those helpers are duplicated from `cockpit/server/governor.py` (itself duplicated from
  `seneschal/scripts/governor.py`) rather than imported — the cockpit is its own dependency world.
- **`<state dir>/meals.json`** — the Dream-staged meal-plan/meal-idea snapshot (written by Dream when
  the active store carries meal plans; absent otherwise). Plain JSON, no sqlite involved.

Every function here degrades gracefully, same posture as `readers.py`: a missing db file, a missing
table (an older `health.db` predating the `workouts`/`nutrition` tables), a missing/corrupt
`meals.json`, or a malformed row never raises or 500s — they return an honest `"available": false`
payload instead. Nothing in this module writes anything.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# Owner-timezone plumbing, mirroring cockpit/server/governor.py's guarded import (see there): from
# the cockpit package tz_common is normally not importable, so this degrades to the machine-local
# clock — the daemon side's own bare-interpreter fallback.
try:
    import tz_common as _tz_common  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — the normal cockpit runtime state
    _tz_common = None

DB_FILE = "health.db"
MEALS_FILE = "meals.json"

DEFAULT_DAYS = 14
MAX_DAYS = 365


# --------------------------------------------------------------------------------------------------
# Owner-local day boundary — duplicated from cockpit/server/governor.py (itself duplicated from
# seneschal/scripts/governor.py; see that module's docstring for the rationale). Only used for "today"/
# "this week" reference points, never for converting health.db's already-local timestamps.
# --------------------------------------------------------------------------------------------------

def local_today() -> date:
    """The owner's current local calendar date (machine-local when tz_common is unavailable)."""
    if _tz_common is not None:
        return _tz_common.local_now().date()
    return datetime.now().astimezone().date()


def _week_key(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


# --------------------------------------------------------------------------------------------------
# health.db
# --------------------------------------------------------------------------------------------------

def _clamp_days(days: Optional[int], default: int = DEFAULT_DAYS) -> int:
    try:
        n = int(days) if days is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_DAYS))


def _connect_ro(state_dir) -> Optional[sqlite3.Connection]:
    """Open `health.db` if (and only if) it already exists — never create one. `None` for a missing
    file or one sqlite can't open at all; callers treat that as `available: false`. This module only
    ever executes SELECTs, so a plain connection stays read-only in practice."""
    db_path = Path(state_dir) / DB_FILE
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def read_sleep(state_dir, days: Optional[int] = DEFAULT_DAYS) -> dict:
    """Recent nights, one row per `sleep_day` (fragmented sleepers log >2 sleep_session rows a night —
    see `health_common.py`'s module doc — so sessions are summed into one night). `efficiency`/`sleep_score`/
    `start_local`/`end_local` come from that night's LONGEST session (its "primary" one), since a short
    nap's score isn't representative of the night. Newest night first, capped at `days` distinct nights
    (NOT a date-range filter — the most recent N nights, whatever their actual dates)."""
    n = _clamp_days(days)
    conn = _connect_ro(state_dir)
    if conn is None:
        return {"available": False, "nights": [], "count": 0}
    try:
        rows = conn.execute(
            "SELECT sleep_day, start_local, end_local, duration_min, efficiency, sleep_score "
            "FROM sleep_session ORDER BY sleep_day DESC, duration_min DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"available": False, "nights": [], "count": 0}
    finally:
        conn.close()

    order: list[str] = []
    by_night: dict[str, dict[str, Any]] = {}
    for r in rows:
        night = r["sleep_day"]
        if night not in by_night:
            if len(order) >= n:
                break  # rows are sleep_day-DESC, so every later "new" night is even older
            order.append(night)
            by_night[night] = {
                "night": night,
                "total_duration_min": 0.0,
                "session_count": 0,
                "efficiency": r["efficiency"],
                "sleep_score": r["sleep_score"],
                "start_local": r["start_local"],
                "end_local": r["end_local"],
            }
        bucket = by_night[night]
        bucket["total_duration_min"] += r["duration_min"] or 0.0
        bucket["session_count"] += 1

    nights = [by_night[night] for night in order]
    return {"available": True, "nights": nights, "count": len(nights)}


def read_workouts(state_dir, days: Optional[int] = DEFAULT_DAYS) -> dict:
    """Recent workout sessions (a `local_date >= today - (days-1)` window, newest first) plus a
    per-ISO-week rollup (count, minutes, kcal) over that same window."""
    n = _clamp_days(days)
    conn = _connect_ro(state_dir)
    if conn is None:
        return {"available": False, "sessions": [], "weekly": []}
    try:
        cutoff = (local_today() - timedelta(days=n - 1)).isoformat()
        rows = conn.execute(
            "SELECT uuid, start_local, end_local, local_date, duration_min, exercise_type, "
            "title, notes, energy_kcal, distance_m FROM workouts WHERE local_date >= ? "
            "ORDER BY local_date DESC, start_local DESC",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"available": False, "sessions": [], "weekly": []}
    finally:
        conn.close()

    sessions = [dict(r) for r in rows]
    weekly: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            d = date.fromisoformat(r["local_date"])
        except (TypeError, ValueError):
            continue
        wk = _week_key(d)
        bucket = weekly.setdefault(wk, {"week": wk, "count": 0, "minutes": 0.0, "kcal": 0.0})
        bucket["count"] += 1
        bucket["minutes"] += r["duration_min"] or 0.0
        bucket["kcal"] += r["energy_kcal"] or 0.0
    weekly_list = sorted(weekly.values(), key=lambda b: b["week"], reverse=True)
    return {"available": True, "sessions": sessions, "weekly": weekly_list}


def read_nutrition(state_dir, days: Optional[int] = DEFAULT_DAYS) -> dict:
    """Per-day nutrition totals (kcal, protein/carbs/fat) + that day's individual logged entries, over
    a `local_date >= today - (days-1)` window, newest day first."""
    n = _clamp_days(days)
    conn = _connect_ro(state_dir)
    if conn is None:
        return {"available": False, "days": []}
    try:
        cutoff = (local_today() - timedelta(days=n - 1)).isoformat()
        rows = conn.execute(
            "SELECT local_date, meal_type, name, energy_kcal, protein_g, carbs_g, fat_g "
            "FROM nutrition WHERE local_date >= ? ORDER BY local_date DESC, start_utc DESC",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"available": False, "days": []}
    finally:
        conn.close()

    order: list[str] = []
    by_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = r["local_date"]
        if d not in by_day:
            order.append(d)
            by_day[d] = {
                "date": d, "total_kcal": 0.0, "total_protein_g": 0.0,
                "total_carbs_g": 0.0, "total_fat_g": 0.0, "entries": [],
            }
        bucket = by_day[d]
        bucket["total_kcal"] += r["energy_kcal"] or 0.0
        bucket["total_protein_g"] += r["protein_g"] or 0.0
        bucket["total_carbs_g"] += r["carbs_g"] or 0.0
        bucket["total_fat_g"] += r["fat_g"] or 0.0
        bucket["entries"].append({
            "meal_type": r["meal_type"], "name": r["name"], "energy_kcal": r["energy_kcal"],
            "protein_g": r["protein_g"], "carbs_g": r["carbs_g"], "fat_g": r["fat_g"],
        })

    days_list = [by_day[d] for d in order]
    return {"available": True, "days": days_list}


def read_summary(state_dir) -> dict:
    """One compact card: last night's sleep, this (owner-local) week's workouts, today's kcal/protein so
    far. Reuses `read_sleep`/`read_workouts`/`read_nutrition` directly (same tolerance, no duplicated
    query logic) with just enough of a window to find "last night"/"this week"/"today"."""
    sleep = read_sleep(state_dir, days=1)
    workouts = read_workouts(state_dir, days=7)
    nutrition = read_nutrition(state_dir, days=1)

    last_night = sleep["nights"][0] if sleep["available"] and sleep["nights"] else None

    this_week_key = _week_key(local_today())
    this_week = None
    if workouts["available"]:
        this_week = next((w for w in workouts["weekly"] if w["week"] == this_week_key), None)

    today_str = local_today().isoformat()
    today_nutrition = None
    if nutrition["available"]:
        today_nutrition = next((d for d in nutrition["days"] if d["date"] == today_str), None)

    return {
        "available": sleep["available"] or workouts["available"] or nutrition["available"],
        "sleep": {"available": sleep["available"], "last_night": last_night},
        "workouts": {"available": workouts["available"], "this_week": this_week},
        "nutrition": {"available": nutrition["available"], "today": today_nutrition},
    }


# --------------------------------------------------------------------------------------------------
# state/meals.json — the Dream-staged Notion meal-plan snapshot (seneschal/SKILL.md, Dream mode step 2d)
# --------------------------------------------------------------------------------------------------

def read_meals(state_dir) -> dict:
    """Tolerant read of `meals.json` (`{"staged_at", "plans": [{"title","url","summary","tags"}]}`).
    Absent/corrupt/wrong-shaped -> `{"available": false, "staged_at": null, "plans": []}`; a malformed
    individual plan entry (missing a title) is dropped rather than failing the whole read."""
    path = Path(state_dir) / MEALS_FILE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"available": False, "staged_at": None, "plans": []}
    if not isinstance(data, dict):
        return {"available": False, "staged_at": None, "plans": []}

    plans: list[dict[str, Any]] = []
    raw_plans = data.get("plans")
    if isinstance(raw_plans, list):
        for p in raw_plans:
            if not isinstance(p, dict):
                continue
            title = p.get("title")
            if not isinstance(title, str) or not title:
                continue
            tags_raw = p.get("tags")
            plans.append({
                "title": title,
                "url": p.get("url") if isinstance(p.get("url"), str) else None,
                "summary": p.get("summary") if isinstance(p.get("summary"), str) else None,
                "tags": [t for t in tags_raw if isinstance(t, str)] if isinstance(tags_raw, list) else [],
            })

    staged_at = data.get("staged_at") if isinstance(data.get("staged_at"), str) else None
    return {"available": True, "staged_at": staged_at, "plans": plans}
