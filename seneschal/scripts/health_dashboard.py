#!/usr/bin/env python3
"""Render the assistant's health cache into one self-contained, interactive HTML dashboard.

The phone app shows the owner a single "main sleep" block per night and a score. That framing is the problem:
most nights hold two or more separate Samsung-detected sessions (up to nine), the median night puts ~2h of
real sleep *outside* the longest block, and on some nights Samsung logs no sleep at all for stretches they
were plainly asleep. So this page has two jobs:

1. **Actigram** — one row per night, 19:00→19:00, every session drawn where it happened, shaded by stage.
   Where the per-minute movement + heart-rate series (from the JSON export) show the owner lying still *and*
   at their sleeping heart rate with **no Samsung session**, that stretch is tinted green: sleep the app missed.
2. **Interactive analysis** — stat tiles and trend charts that recompute for any time span the owner picks
   (presets or custom dates), entirely client-side. Pick a month, a season, the whole record.

No CDN, no build step, no network: one `.html` with inline CSS, generated SVG, embedded per-night JSON, and
a little vanilla JS. Openable from disk, works offline. Stdlib only. Reads the gitignored local cache and
writes a gitignored local file — **act-low**.

Usage:
  python health_dashboard.py                 # whole record -> state/health-dashboard.html
  python health_dashboard.py --days 90       # last 90 nights only
  python health_dashboard.py --out ~/me.html
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

from health_common import (
    CUT_HOUR, DEFAULT_DB, DEFAULT_STATE_DIR, EPISODE_GAP_MIN, connect, night_start, sleep_day)
from identity_common import load_identity, owner_tz_label

DEFAULT_OUT = os.path.join(DEFAULT_STATE_DIR, "health-dashboard.html")

# Brand: Kumouri purple primary, toxic green as the accent for *derived* insight (here: sleep Samsung
# missed). Stage ramp is shades of the purple, awake in a warm red so interruptions pop.
C = {
    "bg": "#0b0710", "panel": "#150c1e", "line": "#2b1d3d", "ink": "#ece6f5", "muted": "#8b7fa3",
    "purple": "#8e00ff", "green": "#00ff0f",
    "deep": "#4c1d95", "light": "#8e00ff", "rem": "#c084fc", "awake": "#ff3b6b",
    "nodata": "#1d1428", "missed": "#00ff0f",
}
STAGE_ORDER = ("deep", "light", "rem", "awake")  # tie-break: deeper wins a mixed slot
SLOTS = 480             # 3-minute resolution across the 24h night
SLOT_MIN = 1440 // SLOTS
RASTER_W = 960
ROW_H = 4

# "Possible missed sleep": a stretch Samsung logged no session for, where the owner was both still and at their
# sleeping heart rate. Movement < STILL_MAX (active bins run 3-5), HR < HR_SLEEP_MAX (the observed floor is ~44;
# awake-at-the-computer runs 70-90, so the HR gate is what stops daytime stillness lighting up).
STILL_MAX = 1.0
HR_SLEEP_MAX = 60.0
MISSED_MIN_RUN = 20     # minutes; shorter quiescent blips aren't worth calling sleep


# --------------------------------------------------------------------------------------------------
# Stats helpers (stdlib only)
# --------------------------------------------------------------------------------------------------

def median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def hhmm(minutes) -> str:
    if minutes is None:
        return "—"
    m = int(round(minutes)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def dur(minutes) -> str:
    if minutes is None:
        return "—"
    m = int(round(minutes))
    return f"{m // 60}h {m % 60:02d}m"


# --------------------------------------------------------------------------------------------------
# Load + shape
# --------------------------------------------------------------------------------------------------

def _split_at_cut(a: datetime, b: datetime):
    """Yield ``(night_iso, start, end)`` pieces of ``[a, b)``, cut wherever it crosses the 19:00 boundary.

    A sleep that runs 18:40 → 20:10 belongs half to one night and half to the next, and the actigram must
    draw both halves. Splitting here (rather than clipping) is what keeps painted area == in-bed minutes.
    """
    while a < b:
        night = sleep_day(a)
        end = min(b, night_start(night) + timedelta(days=1))
        yield night.isoformat(), a, end
        a = end


def _slot_of(local: datetime, night: date) -> int:
    """Which 3-minute slot of ``night``'s 19:00→19:00 row a local instant falls in."""
    return int((local - night_start(night)).total_seconds() // 60) // SLOT_MIN


def load(conn, days: int):
    last = conn.execute("SELECT MAX(sleep_day) FROM sleep_session").fetchone()[0]
    if not last:
        raise SystemExit("health.db has no sleep sessions — run health_import.py first.")
    end = date.fromisoformat(last)
    start = end - timedelta(days=days - 1) if days > 0 else date.fromisoformat(
        conn.execute("SELECT MIN(sleep_day) FROM sleep_session").fetchone()[0])
    lo, hi = start.isoformat(), end.isoformat()

    sessions = defaultdict(list)
    for r in conn.execute(
            "SELECT * FROM sleep_session WHERE sleep_day BETWEEN ? AND ? ORDER BY start_local", (lo, hi)):
        sessions[r["sleep_day"]].append(dict(r))

    stages = defaultdict(list)
    staged_ids = set()
    for r in conn.execute(
            "SELECT sleep_day, sleep_id, start_utc, end_utc, tz_offset_min, stage FROM sleep_stage "
            "WHERE sleep_day BETWEEN ? AND ? ORDER BY start_utc", (lo, hi)):
        off = timedelta(minutes=r["tz_offset_min"])
        staged_ids.add(r["sleep_id"])
        stages[r["sleep_day"]].append((
            datetime.fromisoformat(r["start_utc"]) + off,
            datetime.fromisoformat(r["end_utc"]) + off,
            r["stage"]))

    # Painting spans: intervals cut at the 19:00 night boundary, filed under the night each piece falls in.
    spans = defaultdict(list)
    for day_spans in stages.values():
        for a, b, stage in day_spans:
            for piece_day, pa, pb in _split_at_cut(a, b):
                spans[piece_day].append((pa, pb, stage))
    for day_sessions in sessions.values():
        for s in day_sessions:            # sessions the watch never staged still deserve a mark
            if s["datauuid"] in staged_ids:
                continue
            a, b = datetime.fromisoformat(s["start_local"]), datetime.fromisoformat(s["end_local"])
            for piece_day, pa, pb in _split_at_cut(a, b):
                spans[piece_day].append((pa, pb, "light"))

    # Per-minute movement + heart rate, bucketed onto the same slot grid: movement -> max activity per
    # slot; HR -> min per slot. One pass each; both are keyed by night already.
    move_slots = defaultdict(lambda: [None] * SLOTS)
    for su, off, al in conn.execute(
            "SELECT start_utc, tz_offset_min, activity_level FROM movement "
            "WHERE night BETWEEN ? AND ? AND activity_level IS NOT NULL", (lo, hi)):
        night = date.fromisoformat(sleep_day(datetime.fromisoformat(su) + timedelta(minutes=off)).isoformat())
        slot = _slot_of(datetime.fromisoformat(su) + timedelta(minutes=off), night)
        if 0 <= slot < SLOTS:
            cur = move_slots[night.isoformat()][slot]
            move_slots[night.isoformat()][slot] = al if cur is None else max(cur, al)

    hr_slots = defaultdict(lambda: [None] * SLOTS)
    hr_by_night = defaultdict(list)
    for su, off, hr in conn.execute(
            "SELECT start_utc, tz_offset_min, hr FROM hr_minute WHERE night BETWEEN ? AND ? AND hr > 0",
            (lo, hi)):
        local = datetime.fromisoformat(su) + timedelta(minutes=off)
        night = sleep_day(local).isoformat()
        slot = _slot_of(local, date.fromisoformat(night))
        if 0 <= slot < SLOTS:
            cur = hr_slots[night][slot]
            hr_slots[night][slot] = hr if cur is None else min(cur, hr)
        hr_by_night[night].append(hr)

    def series(sql):
        return [(a, b) for a, b in conn.execute(sql, (lo, hi)) if b is not None]

    return {
        "start": start, "end": end,
        "sessions": sessions, "spans": spans,
        "move_slots": move_slots, "hr_slots": hr_slots, "hr_by_night": hr_by_night,
        "spo2": dict(series("SELECT sleep_day, MIN(lo) FROM spo2 WHERE lo > 0 AND sleep_day BETWEEN ? AND ? "
                            "GROUP BY sleep_day")),
        "skin": dict(series("SELECT sleep_day, AVG(temp_c) FROM skin_temp WHERE sleep_day BETWEEN ? AND ? "
                            "GROUP BY sleep_day")),
        "stress": dict(series("SELECT local_date, AVG(score) FROM stress WHERE score > 0 "
                              "AND local_date BETWEEN ? AND ? GROUP BY local_date")),
        "steps": dict(series("SELECT local_date, count FROM steps_daily WHERE local_date BETWEEN ? AND ?")),
        "meds": {a for a, in conn.execute(
            "SELECT DISTINCT local_date FROM medication_log WHERE local_date BETWEEN ? AND ?", (lo, hi))},
        "export": conn.execute("SELECT export_id, imported_at FROM exports "
                               "ORDER BY export_id DESC LIMIT 1").fetchone(),
    }


def episodes(day_sessions, gap_min=EPISODE_GAP_MIN):
    """Merge sessions into blocks, joining any pair separated by <= ``gap_min``. Returns [(start, end)].

    ``gap_min=0`` yields the plain union. That matters: on a handful of nights two devices (watch and
    phone) log the *same* sleep twice, so naively summing ``duration_min`` invents hours that never
    happened — 3,256 double-counted minutes across this record. Every total below is a union.
    """
    out = []
    for s in sorted(day_sessions, key=lambda s: s["start_local"]):
        a = datetime.fromisoformat(s["start_local"])
        b = datetime.fromisoformat(s["end_local"])
        if out and (a - out[-1][1]).total_seconds() / 60 <= gap_min:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _session_slots(day_sessions, night: date) -> set[int]:
    covered = set()
    for a, b in episodes(day_sessions, gap_min=0):
        for _, pa, pb in _split_at_cut(a, b):
            if sleep_day(pa) == night:
                covered.update(range(max(0, _slot_of(pa, night)),
                                     min(SLOTS, _slot_of(pb - timedelta(seconds=1), night) + 1)))
    return covered


def missed_slots(night_iso, data, session_slots) -> list[int]:
    """Slots where the owner was still + at sleeping HR but Samsung logged no session, in runs >= MISSED_MIN_RUN."""
    move = data["move_slots"].get(night_iso)
    hr = data["hr_slots"].get(night_iso)
    if not move or not hr:
        return []
    quiet = []
    for slot in range(SLOTS):
        m, h = move[slot], hr[slot]
        quiet.append(m is not None and m < STILL_MAX and h is not None and h < HR_SLEEP_MAX
                     and slot not in session_slots)
    out, run = [], []
    need = max(1, MISSED_MIN_RUN // SLOT_MIN)
    for slot in range(SLOTS + 1):
        if slot < SLOTS and quiet[slot]:
            run.append(slot)
        else:
            if len(run) >= need:
                out.extend(run)
            run = []
    return out


# --------------------------------------------------------------------------------------------------
# Per-night records (the data the interactive layer runs on)
# --------------------------------------------------------------------------------------------------

def night_records(data):
    """One compact record per night, plus the derived missed-sleep slot lists for the actigram."""
    records, missed_by_night = [], {}
    for night_iso in sorted(data["sessions"]):
        ss = data["sessions"][night_iso]
        night = date.fromisoformat(night_iso)
        blocks = episodes(ss, gap_min=0)
        total = sum((b - a).total_seconds() / 60 for a, b in blocks)
        main = max(ss, key=lambda s: s["duration_min"])
        a0 = datetime.fromisoformat(main["start_local"])
        b0 = datetime.fromisoformat(main["end_local"])

        sess_slots = _session_slots(ss, night)
        missed = missed_slots(night_iso, data, sess_slots)
        missed_by_night[night_iso] = missed

        stage_min = defaultdict(float)
        for start, end, stage in data["spans"].get(night_iso, []):
            stage_min[stage] += (end - start).total_seconds() / 60
        hrs = data["hr_by_night"].get(night_iso, [])

        records.append({
            "d": night_iso,
            "tot": round(total),
            "ses": len(blocks),
            "hid": round(max(total - main["duration_min"], 0.0)),
            "on": a0.hour * 60 + a0.minute,
            "wk": b0.hour * 60 + b0.minute,
            "dp": round(stage_min["deep"]), "lt": round(stage_min["light"]),
            "rm": round(stage_min["rem"]), "aw": round(stage_min["awake"]),
            "rhr": round(min(hrs)) if hrs else None,
            "missed": round(len(missed) * SLOT_MIN),
            "sp": round(data["spo2"][night_iso]) if night_iso in data["spo2"] else None,
            "sk": round(data["skin"][night_iso], 1) if night_iso in data["skin"] else None,
            "st": round(data["stress"][night_iso]) if night_iso in data["stress"] else None,
            "stp": int(data["steps"][night_iso]) if night_iso in data["steps"] else None,
            "med": 1 if night_iso in data["meds"] else 0,
        })
    return records, missed_by_night


# --------------------------------------------------------------------------------------------------
# The actigram (server-rendered; each night a filterable <g>)
# --------------------------------------------------------------------------------------------------

def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def raster(data, missed_by_night) -> str:
    nights = sorted(data["sessions"])
    if not nights:
        return "<p>No sleep sessions in range.</p>"
    first, last = date.fromisoformat(nights[0]), date.fromisoformat(nights[-1])
    all_days = [first + timedelta(days=i) for i in range((last - first).days + 1)]

    pad_l, pad_t = 62, 24
    height = pad_t + len(all_days) * ROW_H + 28
    parts = [f'<svg id="actigram" viewBox="0 0 {pad_l + RASTER_W + 12} {height}" width="100%" '
             f'role="img" aria-label="Sleep actigram, one row per night">']

    for offset in range(0, 25, 3):
        x = pad_l + RASTER_W * offset / 24
        parts.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t + len(all_days)*ROW_H}" '
                     f'stroke="{C["line"]}" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{pad_t - 8}" fill="{C["muted"]}" font-size="10" '
                     f'text-anchor="middle">{(CUT_HOUR + offset) % 24:02d}:00</text>')

    for i, day in enumerate(all_days):
        y = pad_t + i * ROW_H
        key = day.isoformat()
        if day.day == 1:
            parts.append(f'<text x="{pad_l - 8}" y="{y + ROW_H}" fill="{C["muted"]}" font-size="10" '
                         f'text-anchor="end">{day.strftime("%b %Y")}</text>')
        day_spans = data["spans"].get(key)
        missed = missed_by_night.get(key, [])
        if not day_spans and not missed:
            parts.append(f'<g class="ni" data-d="{key}"><rect x="{pad_l}" y="{y}" width="{RASTER_W}" '
                         f'height="{ROW_H - 1}" fill="{C["nodata"]}"/></g>')
            continue

        row_start = night_start(day)
        slots: list[str | None] = [None] * SLOTS
        for slot in missed:                        # derived layer, under the real stages
            slots[slot] = "missed"
        for start, end, stage in day_spans or []:
            lo = min(SLOTS - 1, max(0, int((start - row_start).total_seconds() // 60) // SLOT_MIN))
            hi = min(SLOTS, -(-int((end - row_start).total_seconds() // 60) // SLOT_MIN))
            for slot in range(lo, max(hi, lo + 1)):
                cur = slots[slot]
                if cur in (None, "missed") or STAGE_ORDER.index(stage) < STAGE_ORDER.index(cur):
                    slots[slot] = stage

        cells = [f'<g class="ni" data-d="{key}">']
        run_start, run_stage = 0, slots[0]
        for slot in range(1, SLOTS + 1):
            here = slots[slot] if slot < SLOTS else "\0"
            if here != run_stage:
                if run_stage:
                    x = pad_l + RASTER_W * run_start / SLOTS
                    w = RASTER_W * (slot - run_start) / SLOTS
                    op = ' opacity="0.5"' if run_stage == "missed" else ''
                    cells.append(f'<rect x="{x:.2f}" y="{y}" width="{max(w,0.8):.2f}" '
                                 f'height="{ROW_H - 1}" fill="{C[run_stage]}"{op}/>')
                run_start, run_stage = slot, here

        day_sessions = data["sessions"].get(key, [])
        total = sum(s["duration_min"] for s in day_sessions)
        n = len(day_sessions)
        extra = f'; +{len(missed) * SLOT_MIN}m Samsung missed' if missed else ''
        label = (f'{dur(total)} across {n} session{"s" if n != 1 else ""}{extra}' if n
                 else f'{len(missed) * SLOT_MIN}m of likely sleep Samsung missed')
        cells.append(f'<rect x="{pad_l}" y="{y}" width="{RASTER_W}" height="{ROW_H-1}" fill="transparent">'
                     f'<title>{key} — {label}</title></rect></g>')
        parts.append("".join(cells))

    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------------------

CSS = """
*{box-sizing:border-box}
body{margin:0;background:%(bg)s;color:%(ink)s;font:15px/1.55 "Segoe UI",system-ui,sans-serif}
a{color:%(purple)s}
.wrap{max-width:1120px;margin:0 auto;padding:32px 20px 72px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:14px;margin:0 0 2px;letter-spacing:.06em;text-transform:uppercase;color:%(muted)s}
.sub{color:%(muted)s;font-size:13px;margin:0 0 22px}
.panel{background:%(panel)s;border:1px solid %(line)s;border-radius:12px;padding:18px 20px;margin:0 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:0 0 20px}
.tile{background:%(panel)s;border:1px solid %(line)s;border-radius:12px;padding:14px 16px}
.tile .k{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:%(muted)s}
.tile .v{font-size:23px;font-weight:600;margin-top:5px;letter-spacing:-.02em}
.tile .n{font-size:11px;color:%(muted)s;margin-top:3px}
.hi{color:%(green)s}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:%(muted)s;margin:10px 0 0}
.legend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.note{border-left:2px solid %(green)s;padding:2px 0 2px 14px;color:%(muted)s;font-size:13px;margin:14px 0 0}
.note b{color:%(ink)s;font-weight:600}
.scroll{overflow-x:auto}
.actiwrap{max-height:none}
footer{color:%(muted)s;font-size:12px;margin-top:34px;line-height:1.7}
code{background:#0e0916;padding:1px 5px;border-radius:4px;font-size:12px}
.ctl{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 18px}
.ctl button{background:%(panel)s;color:%(ink)s;border:1px solid %(line)s;border-radius:8px;
  padding:7px 13px;font-size:13px;cursor:pointer;font-family:inherit}
.ctl button:hover{border-color:%(purple)s}
.ctl button.on{background:%(purple)s;border-color:%(purple)s;color:#fff}
.ctl input[type=date]{background:%(panel)s;color:%(ink)s;border:1px solid %(line)s;border-radius:8px;
  padding:6px 9px;font-size:13px;font-family:inherit;color-scheme:dark}
.ctl .rng{color:%(muted)s;font-size:12px;margin-left:auto}
.ni.dim{opacity:.16}
.chart text{fill:%(muted)s}
""" % C


def tile(tid, label, note="") -> str:
    return (f'<div class="tile"><div class="k">{_esc(label)}</div>'
            f'<div class="v" id="{tid}">—</div><div class="n" id="{tid}-n">{_esc(note)}</div></div>')


def build(data) -> str:
    records, missed_by_night = night_records(data)
    export = data["export"]
    payload = json.dumps({
        "nights": records,
        "cut": CUT_HOUR,
        "colors": {k: C[k] for k in ("purple", "green", "deep", "light", "rem", "awake", "muted", "line")},
    }, separators=(",", ":"))

    tiles = "".join([
        tile("t-nights", "Nights in view", "the selected span"),
        tile("t-sleep", "Sleep per night", "median, blocks merged"),
        tile("t-ses", "Sessions per night", "median; % that are 2+"),
        tile("t-hidden", "Hidden by the app", "median outside the longest block"),
        tile("t-missed", "Sleep Samsung missed", "median/night, still + low HR"),
        tile("t-onset", "Falls asleep", "median, main block"),
        tile("t-wake", "Wakes", "median, main block"),
        tile("t-rhr", "Resting HR", "median nightly floor, per-minute"),
    ])

    def panel(cid, title, sub):
        return (f'<div class="panel"><h2>{title}</h2>'
                f'<p class="sub" style="margin:4px 0 6px">{sub}</p>'
                f'<svg class="chart" id="{cid}" viewBox="0 0 460 140" width="100%"></svg></div>')

    charts = "".join([
        panel("c-total", "Total sleep per night", "Bars: all sessions merged. Line: 7-night median."),
        panel("c-missed", "Sleep Samsung missed per night", "Still + at sleeping HR, no logged session."),
        panel("c-clock", "Sleep / wake clock", "Main block. ● asleep · ● awake. Axis 19:00→19:00."),
        panel("c-ses", "Sessions per night", "How many times Samsung split the night."),
        panel("c-rhr", "Resting heart rate", "Per-minute nightly minimum — the true floor."),
        panel("c-spo2", "Blood oxygen (nightly low)", "Band: 95%+ is unremarkable."),
        panel("c-stress", "Stress score", "Daily mean of Samsung's HRV-derived score."),
        panel("c-steps", "Steps", "Daily total."),
    ])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sleep &amp; Health</title><style>{CSS}</style></head>
<body><div class="wrap">

<h1>Sleep &amp; health</h1>
<p class="sub">Samsung Health export <code>{_esc(export["export_id"]) if export else "—"}</code> ·
built {datetime.now().strftime("%Y-%m-%d %H:%M")} · all times local ({_esc(owner_tz_label(load_identity()))}) ·
{_esc(data["start"])} → {_esc(data["end"])}</p>

<div class="ctl" id="ctl">
  <button data-days="30">30d</button>
  <button data-days="90">90d</button>
  <button data-days="180">6mo</button>
  <button data-days="365">1yr</button>
  <button data-days="0">All</button>
  <input type="date" id="from"> <span style="color:{C['muted']}">→</span> <input type="date" id="to">
  <span class="rng" id="rng"></span>
</div>

<div class="tiles">{tiles}</div>

<div class="panel">
  <h2>Actigram — every session, where it actually happened</h2>
  <p class="sub" style="margin:4px 0 12px">One row per night, 19:00 → 19:00. The phone shows you the single
  longest block; this shows all of them. <span style="color:{C['green']}">Green</span> = a stretch you were
  still and at your sleeping heart rate but Samsung logged nothing — likely sleep it missed.</p>
  <div class="scroll actiwrap">{raster(data, missed_by_night)}</div>
  <div class="legend">
    <span><i style="background:{C['deep']}"></i>deep</span>
    <span><i style="background:{C['light']}"></i>light</span>
    <span><i style="background:{C['rem']}"></i>rem</span>
    <span><i style="background:{C['awake']}"></i>awake</span>
    <span><i style="background:{C['missed']};opacity:.6"></i>likely missed by Samsung</span>
    <span><i style="background:{C['nodata']}"></i>no data</span></div>
  <p class="note" id="frag"></p>
</div>

<div class="grid2">{charts}</div>

<footer>
<b>Reading the timestamps.</b> Samsung stores <code>start_time</code>/<code>end_time</code> in <b>UTC</b>
and records the local offset separately in <code>time_offset</code>. Everything here is local time — each
row's recorded offset, falling back to the owner's configured timezone. Read as wall clock instead, every
event would land hours late — the size of the local UTC offset. Verified against the DST transitions in
the data.<br>
<b>The green "missed" layer</b> comes from the per-minute JSON export (movement + heart rate the CSV omits):
a stretch is flagged when activity stays below {STILL_MAX} <i>and</i> heart rate below {HR_SLEEP_MAX:.0f} bpm
for {MISSED_MIN_RUN}+ minutes with no Samsung session — still, at sleeping heart rate, unlogged. It's a
conservative proxy, not a clinical scorer.<br>
<b>Source.</b> <code>seneschal/state/health.db</code>, built by <code>health_import.py</code> (CSV + JSON
exports). Regenerate with <code>python health_dashboard.py</code>. Setup + the fully-automatic feed:
<code>seneschal/scripts/HEALTH_SETUP.md</code>.
</footer>
</div>

<script>
const DATA = {payload};
{JS_ENGINE}
</script>
</body></html>"""


# The interactive engine: filter NIGHTS to a date window, recompute tiles, redraw charts, dim the actigram.
# Vanilla JS, no libraries (CSP-safe, offline). Mirrors the Python stats so the two never disagree.
JS_ENGINE = r"""
const N = DATA.nights, CO = DATA.colors, CUT = DATA.cut;
const $ = id => document.getElementById(id);
const med = a => { a = a.filter(v => v != null).sort((x,y)=>x-y); if(!a.length) return null;
  const m = a.length>>1; return a.length%2 ? a[m] : (a[m-1]+a[m])/2; };
const hhmm = m => m==null ? "—" : (m=(Math.round(m)%1440+1440)%1440, String(m/60|0).padStart(2,"0")+":"+String(m%60).padStart(2,"0"));
const durf = m => m==null ? "—" : (Math.round(m)/60|0)+"h "+String(Math.round(m)%60).padStart(2,"0")+"m";
const clockMed = (arr, anchor) => { arr = arr.filter(v=>v!=null); if(!arr.length) return null;
  const s = arr.map(m => ((m-anchor)%1440+1440)%1440); return (med(s)+anchor)%1440; };

function statsFor(rows){
  const tot = rows.map(r=>r.tot), ses = rows.map(r=>r.ses), hid = rows.map(r=>r.hid);
  const frag = rows.length ? 100*rows.filter(r=>r.ses>=2).length/rows.length : 0;
  const missedNights = rows.filter(r=>r.missed>0);
  return {
    n: rows.length,
    sleep: med(tot), ses: med(ses), frag,
    hidden: med(hid),
    missedTypical: med(missedNights.map(r=>r.missed)),      // median on the nights it happens
    missedPct: rows.length ? 100*missedNights.length/rows.length : 0,
    missedTotal: rows.reduce((a,r)=>a+r.missed,0),
    onset: clockMed(rows.map(r=>r.on), CUT*60),
    wake: clockMed(rows.map(r=>r.wk), CUT*60),
    rhr: med(rows.map(r=>r.rhr)),
  };
}

function renderTiles(s){
  $("t-nights").textContent = s.n;
  $("t-sleep").textContent = durf(s.sleep);
  $("t-ses").textContent = s.ses==null ? "—" : Math.round(s.ses);
  $("t-ses-n").textContent = Math.round(s.frag)+"% are 2+ sessions";
  $("t-hidden").textContent = durf(s.hidden);
  $("t-missed").textContent = durf(s.missedTypical);
  $("t-missed").className = "v hi";
  $("t-missed-n").textContent = Math.round(s.missedPct)+"% of nights · "+durf(s.missedTotal)+" total";
  $("t-onset").textContent = hhmm(s.onset);
  $("t-wake").textContent = hhmm(s.wake);
  $("t-rhr").textContent = s.rhr==null ? "—" : Math.round(s.rhr)+" bpm";
  $("frag").innerHTML = "<b>Fragmentation is the story.</b> "+Math.round(s.frag)+
    "% of the nights in view hold two or more separate sessions, and the median night has "+durf(s.hidden)+
    " of sleep outside the longest block. On top of that, on "+Math.round(s.missedPct)+"% of nights Samsung "+
    "logged nothing through a stretch you were still and at your sleeping heart rate — "+durf(s.missedTotal)+
    " of likely sleep dropped entirely across the nights in view.";
}

// --- tiny SVG charting (line / bar / dot / clock), sized to the element's viewBox ---
function svgEl(id){ const e=$(id); while(e.firstChild) e.removeChild(e.firstChild); return e; }
function vb(id){ return $(id).viewBox.baseVal; }
function mk(tag, attrs){ const e=document.createElementNS("http://www.w3.org/2000/svg",tag);
  for(const k in attrs) e.setAttribute(k, attrs[k]); return e; }
function rollMed(pairs, w){ const out=[], buf=[]; for(const [d,v] of pairs){ buf.push(v); if(buf.length>w) buf.shift();
  out.push([d, med(buf.slice())]); } return out; }

function drawChart(id, pairs, opt){
  opt = opt || {}; const g = svgEl(id), {width:W,height:H} = vb(id);
  const padL=34, padB=16, padT=8;
  if(!pairs.length) return;
  const xs = pairs.map(p=>p[0]), ys = pairs.map(p=>p[1]);
  const x0=Math.min(...xs), x1=Math.max(...xs), spanX=Math.max(x1-x0,1);
  let y0 = opt.zero ? 0 : Math.min(...ys), y1 = Math.max(...ys); if(y1===y0) y1=y0+1;
  const px = x => padL + (W-padL-6)*(x-x0)/spanX;
  const py = y => padT + (H-padT-padB)*(1-(y-y0)/(y1-y0));
  const fmt = opt.fmt || (v=>Math.round(v));
  if(opt.band){ const blo=Math.max(opt.band[0],y0), bhi=Math.min(opt.band[1],y1);
    if(bhi>blo) g.appendChild(mk("rect",{x:padL,y:py(bhi),width:W-padL-6,height:Math.max(py(blo)-py(bhi),1),fill:CO.green,opacity:0.07})); }
  for(const f of [0,0.5,1]){ const y=padT+(H-padT-padB)*f, val=y1-(y1-y0)*f;
    g.appendChild(mk("line",{x1:padL,y1:y,x2:W-6,y2:y,stroke:CO.line}));
    const t=mk("text",{x:padL-6,y:y+3,"font-size":9,"text-anchor":"end"}); t.textContent=fmt(val); g.appendChild(t); }
  const color = opt.color || CO.purple;
  if(opt.kind==="bar"){ const bw=Math.max((W-padL-6)/Math.max(pairs.length,1)*0.9,0.7);
    for(const [d,v] of pairs){ const x=px(d), y=py(v);
      const r=mk("rect",{x:x-bw/2,y:y,width:bw,height:Math.max(py(y0)-y,0.5),fill:color,opacity:0.75});
      r.appendChild(mk("title",{})).textContent=fmt(v); g.appendChild(r); } }
  else if(opt.kind==="dot"){ for(const [d,v] of pairs) g.appendChild(mk("circle",{cx:px(d),cy:py(v),r:1.6,fill:color,opacity:0.8})); }
  else { let pts=""; for(const [d,v] of pairs) pts+=px(d).toFixed(1)+","+py(v).toFixed(1)+" ";
    g.appendChild(mk("polyline",{points:pts.trim(),fill:"none",stroke:color,"stroke-width":1.4,"stroke-linejoin":"round"})); }
  for(const d of [x0,x1]){ const t=mk("text",{x:px(d),y:H-3,"font-size":9,"text-anchor":d===x0?"start":"end"});
    t.textContent=isoShort(d); g.appendChild(t); }
}

function drawClock(id, rows){
  const g=svgEl(id), {width:W,height:H}=vb(id), padL=40,padB=16,padT=8;
  if(!rows.length) return;
  const xs=rows.map(r=>r._x), x0=Math.min(...xs), x1=Math.max(...xs), spanX=Math.max(x1-x0,1);
  const px=x=>padL+(W-padL-6)*(x-x0)/spanX, py=v=>padT+(H-padT-padB)*(v/24);
  for(let h=0;h<=24;h+=6){ const y=py(h);
    g.appendChild(mk("line",{x1:padL,y1:y,x2:W-6,y2:y,stroke:CO.line}));
    const t=mk("text",{x:padL-6,y:y+3,"font-size":9,"text-anchor":"end"}); t.textContent=String((CUT+h)%24).padStart(2,"0")+":00"; g.appendChild(t); }
  const reb = m => (((m - CUT*60)%1440)+1440)%1440/60;
  for(const [key,color] of [["on",CO.purple],["wk",CO.green]])
    for(const r of rows){ if(r[key]==null) continue;
      g.appendChild(mk("circle",{cx:px(r._x),cy:py(reb(r[key])),r:1.7,fill:color,opacity:0.72})); }
}

function isoShort(x){ const d=new Date(x*86400000); return d.toLocaleString("en-US",{month:"short",day:"numeric",timeZone:"UTC"}); }
function ord(iso){ return Math.floor(Date.parse(iso+"T00:00:00Z")/86400000); }

function redraw(rows){
  const P = key => rows.filter(r=>r[key]!=null).map(r=>[ord(r.d), r[key]]);
  // Total sleep: bars, then a 7-night rolling-median line overlaid in the same coordinate space.
  drawChart("c-total", rows.map(r=>[ord(r.d), r.tot]), {kind:"bar", zero:true, fmt:v=>Math.round(v)+"m"});
  if(rows.length){
    const g=$("c-total"), W=vb("c-total").width, H=vb("c-total").height, padL=34,padB=16,padT=8;
    const xs=rows.map(r=>ord(r.d)), x0=Math.min(...xs), x1=Math.max(...xs), y1=Math.max(...rows.map(r=>r.tot),1);
    const px=x=>padL+(W-padL-6)*(x-x0)/Math.max(x1-x0,1), py=v=>padT+(H-padT-padB)*(1-v/y1);
    let pts=""; for(const [d,v] of rollMed(rows.map(r=>[ord(r.d), r.tot]), 7)) if(v!=null) pts+=px(d).toFixed(1)+","+py(v).toFixed(1)+" ";
    g.appendChild(mk("polyline",{points:pts.trim(),fill:"none",stroke:CO.green,"stroke-width":1.4}));
  }
  drawChart("c-missed", rows.map(r=>[ord(r.d), r.missed]), {kind:"bar", zero:true, color:CO.green, fmt:v=>Math.round(v)+"m"});
  drawClock("c-clock", rows.map(r=>({_x:ord(r.d), on:r.on, wk:r.wk})));
  drawChart("c-ses", rows.map(r=>[ord(r.d), r.ses]), {kind:"bar", zero:true});
  drawChart("c-rhr", P("rhr"), {fmt:v=>Math.round(v)});
  drawChart("c-spo2", P("sp"), {band:[95,100], fmt:v=>Math.round(v)+"%"});
  drawChart("c-stress", P("st"), {fmt:v=>Math.round(v)});
  drawChart("c-steps", rows.filter(r=>r.stp!=null).map(r=>[ord(r.d), r.stp]), {kind:"bar", zero:true, fmt:v=>v>=1000?(v/1000).toFixed(1)+"k":v});
}

let curFrom = N.length ? N[0].d : null, curTo = N.length ? N[N.length-1].d : null;
function apply(){
  const rows = N.filter(r => r.d >= curFrom && r.d <= curTo);
  renderTiles(statsFor(rows)); redraw(rows);
  $("from").value = curFrom; $("to").value = curTo;
  $("rng").textContent = rows.length ? (curFrom + "  →  " + curTo) : "no nights in range";
  document.querySelectorAll("#actigram .ni").forEach(g => {
    const d = g.getAttribute("data-d");
    g.classList.toggle("dim", !(d >= curFrom && d <= curTo));
  });
  const active = (curFrom===N[0].d && curTo===N[N.length-1].d);
  document.querySelectorAll("#ctl button").forEach(b => b.classList.toggle("on", active && b.dataset.days==="0"));
}
function setDays(days){
  if(!N.length) return;
  curTo = N[N.length-1].d;
  if(days===0){ curFrom = N[0].d; }
  else { const to = ord(curTo); curFrom = N.find(r => ord(r.d) >= to-days+1)?.d || N[0].d; }
  document.querySelectorAll("#ctl button").forEach(b => b.classList.toggle("on", +b.dataset.days===days));
  apply();
}
document.querySelectorAll("#ctl button").forEach(b => b.addEventListener("click", () => setDays(+b.dataset.days)));
$("from").addEventListener("change", e => { if(e.target.value){ curFrom=e.target.value;
  document.querySelectorAll("#ctl button").forEach(b=>b.classList.remove("on")); apply(); }});
$("to").addEventListener("change", e => { if(e.target.value){ curTo=e.target.value;
  document.querySelectorAll("#ctl button").forEach(b=>b.classList.remove("on")); apply(); }});
setDays(0);
"""


def main() -> int:
    p = argparse.ArgumentParser(description="Render the health cache to a self-contained HTML dashboard.")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--days", type=int, default=0, help="how many recent nights to load (0 = all)")
    args = p.parse_args()

    conn = connect(args.db)
    data = load(conn, args.days)
    page = build(data)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(os.path.abspath(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
