#!/usr/bin/env python3
"""Shared plumbing for the assistant's Samsung Health pipeline: CSV reading, the timezone contract, the schema.

Samsung Health's "Download personal data" export is a zip of ~79 CSVs, one per data type. Two things
about it will bite anyone who doesn't pin them down, so they are pinned down here, once:

**1. The header is on line 2.** Line 1 is a provenance comment (``<type>,<sh_ver>,<schema_ver>``). Every
reader must skip it. Rows also carry a trailing comma, so ``csv.DictReader`` yields a ``None`` key.

**2. `start_time` / `end_time` are UTC. `time_offset` is how you get local.** This is the one that
matters, and it is *not* obvious — the strings look like wall clock, and reading them as wall clock
silently shifts every conclusion by 5-6 hours (exactly America/Chicago's offset). Proof, from the data
itself: on the US spring-forward day **2026-03-08** the hourly-binned ``tracker.heart_rate`` rows carry
all 24 hours ``00..23`` with no gap; if the strings were local wall clock, hour ``02`` could not exist.
Symmetrically, on the fall-back day **2025-11-02** no hour is duplicated. The ``time_offset`` column
(``UTC-0600`` in winter, ``UTC-0500`` in summer) is the offset that was in effect — add it to recover
local time. ``update_time`` is UTC too (cross-checked against ``stress.histogram.decay_time``, a raw
epoch). The lone exception is ``day_time`` on the daily-summary tables: that one is local midnight
already, stored as an epoch-as-if-UTC, so it renders as ``YYYY-MM-DD 00:00:00`` and should be read as a
plain local date.

Grouping sleep into "nights" needs a cut hour, and the textbook noon→noon actigraphy window was **wrong
for this dataset's owner**: they often sleep across noon. Measured over the whole record (in correct local
time), the hour they are least likely to be asleep is **19:00** — 73 h of sleep observed in that hour,
against 337 h in the 06:00 hour, the peak. So ``sleep_day`` cuts at ``CUT_HOUR = 19``: a "night" runs 19:00 → 19:00 and is
keyed by the date it starts, which keeps an evening nap and the following 03:00 main block in one night
instead of two. Spans that straddle the cut are split by the consumer, never dropped.

Stdlib only, per the repo convention. Times are stored in SQLite as naive-UTC ISO-8601 strings plus an
integer ``tz_offset_min``, so any consumer can render local without guessing.
"""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import zipfile
from datetime import date, datetime, timedelta

DEFAULT_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")
DEFAULT_DB = os.path.join(DEFAULT_STATE_DIR, "health.db")

#: Samsung's ``sleep_stage.stage`` codes.
SLEEP_STAGES = {"40001": "awake", "40002": "light", "40003": "deep", "40004": "rem"}

#: Filenames in a Samsung export are ``samsunghealth_<user>_<YYYYMMDDHHMMSS>.zip``. The stamp is *local*.
EXPORT_RE = re.compile(r"samsunghealth_.*?_(\d{14})\.zip$", re.IGNORECASE)

_TS_FMT = "%Y-%m-%d %H:%M:%S.%f"
#: Sleep sessions shorter than this are noise (a watch bump, a re-detect), not a block of sleep.
MIN_SESSION_MIN = 5
#: Local hour that starts a "night". 19:00 = the hour the owner is least often asleep (see module doc).
CUT_HOUR = 19
#: Consecutive sessions separated by less than this are one interrupted sleep, not two sleeps.
EPISODE_GAP_MIN = 45


class SamsungExportError(RuntimeError):
    """The zip isn't a Samsung Health export, or a table we need is missing from it."""


# --------------------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------------------

def parse_ts(value: str) -> datetime | None:
    """Parse a Samsung timestamp string into a naive **UTC** datetime. ``None`` for blank/garbage."""
    if not value:
        return None
    try:
        return datetime.strptime(value, _TS_FMT)
    except ValueError:
        try:  # a few rows drop the milliseconds
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def parse_offset(value: str) -> int:
    """``"UTC-0600"`` → ``-360`` minutes. Unknown/blank → ``0`` (treat as UTC, and say so upstream)."""
    if not value or len(value) < 8 or not value.startswith("UTC") or value[3] not in "+-":
        return 0
    sign = -1 if value[3] == "-" else 1
    try:
        return sign * (int(value[4:6]) * 60 + int(value[6:8]))
    except ValueError:
        return 0


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Mon=0) of ``year``-``month`` — e.g. 2nd Sunday of March."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def central_offset(utc: datetime) -> int:
    """US Central offset in minutes for a naive-**UTC** instant: −300 (CDT) or −360 (CST).

    The per-minute JSON export ships absolute epoch-ms (UTC) with **no** ``time_offset`` column, and this
    machine has no system tz database (``zoneinfo`` is unavailable on the stock Windows Python here). So we
    compute America/Chicago's offset directly from the current US rule: DST runs 08:00 UTC on the 2nd
    Sunday of March through 08:00 UTC on the 1st Sunday of November (the transitions happen at 02:00 *local*,
    which is 08:00 UTC on the day before/of). Verified against every distinct offset in the CSV export,
    which *does* carry ``time_offset``. Fine for 2007-onward; predates none of the data seen so far.
    """
    year = utc.year
    dst_start = datetime.combine(_nth_weekday(year, 3, 6, 2), datetime.min.time()) + timedelta(hours=8)
    dst_end = datetime.combine(_nth_weekday(year, 11, 6, 1), datetime.min.time()) + timedelta(hours=8)
    return -300 if dst_start <= utc < dst_end else -360


def epoch_ms_to_utc(ms: int) -> datetime:
    """Epoch milliseconds → naive-UTC datetime. The JSON binning series use this; it's unambiguous UTC."""
    return datetime(1970, 1, 1) + timedelta(milliseconds=ms)


def to_local(utc: datetime, offset_min: int) -> datetime:
    """Apply the recorded ``time_offset`` to a naive-UTC datetime. Result is naive *local* time."""
    return utc + timedelta(minutes=offset_min)


def sleep_day(local: datetime, cut_hour: int = CUT_HOUR) -> date:
    """The night a local timestamp belongs to: the date on which its 19:00→19:00 window opened."""
    return (local - timedelta(hours=cut_hour)).date()


def night_start(night: date, cut_hour: int = CUT_HOUR) -> datetime:
    """The local instant a night's window opens — the left edge of its actigram row."""
    return datetime.combine(night, datetime.min.time()) + timedelta(hours=cut_hour)


def day_time_to_date(value: str) -> date | None:
    """``day_time`` columns are already local midnight (epoch-as-if-UTC). Read the date, ignore the time."""
    ts = parse_ts(value)
    return ts.date() if ts else None


# --------------------------------------------------------------------------------------------------
# Reading the export
# --------------------------------------------------------------------------------------------------

def export_id(path: str) -> str:
    """The 14-digit local stamp that identifies an export, e.g. ``20260707152802``."""
    m = EXPORT_RE.search(os.path.basename(path))
    if not m:
        raise SamsungExportError(f"not a Samsung Health export filename: {os.path.basename(path)}")
    return m.group(1)


def find_exports(watch_dir: str) -> list[str]:
    """Every ``samsunghealth_*.zip`` in ``watch_dir``, oldest export stamp first."""
    if not os.path.isdir(watch_dir):
        return []
    hits = [os.path.join(watch_dir, n) for n in os.listdir(watch_dir) if EXPORT_RE.search(n)]
    return sorted(hits, key=export_id)


def open_table(zf: zipfile.ZipFile, key: str) -> csv.DictReader:
    """A ``DictReader`` over the one CSV whose name contains ``key``, with line 1 (the comment) skipped.

    ``key`` is matched as a substring, so pass enough to disambiguate: ``com.samsung.shealth.sleep.`` and
    ``com.samsung.shealth.sleep_combined`` both contain ``shealth.sleep``.
    """
    names = [n for n in zf.namelist() if key in n and n.endswith(".csv")]
    if not names:
        raise SamsungExportError(f"no table matching {key!r} in export")
    handle = io.TextIOWrapper(zf.open(sorted(names)[0]), encoding="utf-8-sig", newline="")
    handle.readline()  # the provenance comment
    return csv.DictReader(handle)


def col(prefix: str, name: str) -> str:
    """Some tables namespace their columns (``com.samsung.health.heart_rate.start_time``); most don't."""
    return f"{prefix}{name}" if prefix else name


def num(value: str | None) -> float | None:
    """Samsung writes ``''`` for null and ``-1`` for "not computed" in score-ish columns."""
    if value in (None, "", "-1", "-1.0"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS exports (
    export_id   TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    rows        INTEGER NOT NULL DEFAULT 0
);

-- One row per Samsung-detected sleep session. The owner can average >2 of these a day; the phone app
-- surfaces only the longest, which is the whole reason this pipeline exists.
CREATE TABLE IF NOT EXISTS sleep_session (
    datauuid       TEXT PRIMARY KEY,
    sleep_day      TEXT NOT NULL,          -- local calendar date of the session start
    start_utc      TEXT NOT NULL,
    end_utc        TEXT NOT NULL,
    tz_offset_min  INTEGER NOT NULL,
    start_local    TEXT NOT NULL,
    end_local      TEXT NOT NULL,
    duration_min   REAL,
    efficiency     REAL,
    sleep_score    REAL,
    latency_min    REAL,                   -- Samsung's own sleep_latency
    bedtime_delay_min REAL,                -- Samsung's own admitted onset-detection lag
    wake_delay_min    REAL,                -- ...and wake-detection lag
    mental_recovery   REAL,
    physical_recovery REAL,
    movement_awakening REAL,
    combined_id    TEXT
);
CREATE INDEX IF NOT EXISTS ix_session_day ON sleep_session(sleep_day);

CREATE TABLE IF NOT EXISTS sleep_stage (
    datauuid      TEXT PRIMARY KEY,
    sleep_id      TEXT NOT NULL,
    sleep_day     TEXT NOT NULL,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    tz_offset_min INTEGER NOT NULL,
    stage         TEXT NOT NULL            -- awake | light | deep | rem
);
CREATE INDEX IF NOT EXISTS ix_stage_day   ON sleep_stage(sleep_day);
CREATE INDEX IF NOT EXISTS ix_stage_sleep ON sleep_stage(sleep_id);

-- Hourly bins (binned=1, with min/max) interleaved with spot readings (binned=0). The per-minute
-- series lives in binning_data JSONs that this CSV-only export does not ship -- see HEALTH_SETUP.md.
CREATE TABLE IF NOT EXISTS heart_rate (
    datauuid      TEXT PRIMARY KEY,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT,
    tz_offset_min INTEGER NOT NULL,
    local_date    TEXT NOT NULL,
    hr            REAL,
    hr_min        REAL,
    hr_max        REAL,
    binned        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_hr_date ON heart_rate(local_date);

CREATE TABLE IF NOT EXISTS stress (
    datauuid      TEXT PRIMARY KEY,
    start_utc     TEXT NOT NULL,
    tz_offset_min INTEGER NOT NULL,
    local_date    TEXT NOT NULL,
    score         REAL,
    score_min     REAL,
    score_max     REAL
);
CREATE INDEX IF NOT EXISTS ix_stress_date ON stress(local_date);

CREATE TABLE IF NOT EXISTS spo2 (
    datauuid      TEXT PRIMARY KEY,
    start_utc     TEXT NOT NULL,
    tz_offset_min INTEGER NOT NULL,
    sleep_day     TEXT NOT NULL,
    mean          REAL,
    lo            REAL,
    hi            REAL
);
CREATE INDEX IF NOT EXISTS ix_spo2_day ON spo2(sleep_day);

CREATE TABLE IF NOT EXISTS skin_temp (
    datauuid      TEXT PRIMARY KEY,
    start_utc     TEXT NOT NULL,
    tz_offset_min INTEGER NOT NULL,
    sleep_day     TEXT NOT NULL,
    temp_c        REAL,
    lo            REAL,
    hi            REAL
);
CREATE INDEX IF NOT EXISTS ix_skin_day ON skin_temp(sleep_day);

CREATE TABLE IF NOT EXISTS steps_daily (
    local_date  TEXT PRIMARY KEY,
    count       INTEGER,
    distance_m  REAL,
    calorie     REAL
);

CREATE TABLE IF NOT EXISTS medication_log (
    datauuid    TEXT PRIMARY KEY,
    local_date  TEXT NOT NULL,
    logged_utc  TEXT
);

CREATE TABLE IF NOT EXISTS weight (
    datauuid   TEXT PRIMARY KEY,
    start_utc  TEXT NOT NULL,
    local_date TEXT NOT NULL,
    kg         REAL
);

-- Per-minute series from the SEPARATE JSON export (the CSV export ships only pointers to these). This is
-- the actigraphy signal: `activity_level` per 60s window, the thing that shows the owner lying still through
-- stretches Samsung logged no session for. ~673k rows. Keyed (source, start) so re-import upserts.
CREATE TABLE IF NOT EXISTS movement (
    source_uuid   TEXT NOT NULL,          -- the binning file's parent session uuid
    start_utc     TEXT NOT NULL,
    tz_offset_min INTEGER NOT NULL,
    night         TEXT NOT NULL,
    activity_level REAL,
    PRIMARY KEY (source_uuid, start_utc)
);
CREATE INDEX IF NOT EXISTS ix_move_night ON movement(night);

-- Per-minute heart rate from the JSON export (min/avg/max per 60s window). ~608k rows. Powers the real
-- overnight HR curve and a true resting floor, where the CSV only had hourly bins.
CREATE TABLE IF NOT EXISTS hr_minute (
    source_uuid   TEXT NOT NULL,
    start_utc     TEXT NOT NULL,
    tz_offset_min INTEGER NOT NULL,
    night         TEXT NOT NULL,
    local_date    TEXT NOT NULL,
    hr            REAL,
    hr_min        REAL,
    hr_max        REAL,
    PRIMARY KEY (source_uuid, start_utc)
);
CREATE INDEX IF NOT EXISTS ix_hrmin_night ON hr_minute(night);
CREATE INDEX IF NOT EXISTS ix_hrmin_date  ON hr_minute(local_date);
"""


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Open (creating if needed) the local health cache and ensure the schema exists."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
