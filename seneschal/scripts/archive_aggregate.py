#!/usr/bin/env python3
"""Merge a person's normalized per-service message files into one timeline + rendered transcripts.

Standard library only. Reads every ``normalized/<service>.json`` for a person, merges the records by
``ts_utc`` (integer epoch UTC — the single global sort key), collects all media into one deduplicated
``media/`` dir, and renders three outputs at the person's archive root:

  * ``conversation.json`` — the merged normalized timeline (machine-readable),
  * ``conversation.md``   — a readable transcript (date headers, speaker labels, local times, media links),
  * ``conversation.html`` — a self-contained chat view (inline CSS, media referenced from ``media/``).

USAGE:
  python archive_aggregate.py --person alex
  python archive_aggregate.py --person alex --formats md,html --no-copy-media
  python archive_aggregate.py --person alex --tz UTC     # deterministic times (tests)

Prints a one-line JSON result {ok, person, count, services, media_copied, outputs}. Exit 0/non-zero.
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import sys
import time

import archive_common as ac

def _day_header(dt) -> str:
    """"Tuesday, February 3, 2026" — no leading zero on the day (portable; %-d isn't on Windows)."""
    return dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")


def load_records(in_files: list) -> list:
    records = []
    for path in in_files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                env = json.load(fh)
            records.extend(env.get("messages", []))
        except (OSError, ValueError):
            continue
    return records


def _idkey(v):
    s = str(v)
    return (0, int(s)) if s.lstrip("-").isdigit() else (1, s)


def merge(records: list) -> list:
    """Sort by (ts_utc, service, msg_id) — stable, deterministic across services."""
    return sorted(records, key=lambda r: (int(r.get("ts_utc", 0)), r.get("service", ""),
                                          _idkey(r.get("service_msg_id", ""))))


def collect_media(records: list, media_dir: str, copy: bool) -> int:
    """Copy each record's media into ``media_dir`` (dedup by sha1); fill ``archive_path``. Returns count."""
    seen: dict = {}
    copied = 0
    for rec in records:
        for i, entry in enumerate(rec.get("media", [])):
            lp = entry.get("local_path")
            if copy and lp and os.path.exists(lp):
                try:
                    entry["archive_path"] = ac.copy_media_dedup(
                        lp, media_dir, rec["service"], rec["service_msg_id"], i, seen)
                    copied += 1
                except OSError:
                    entry["archive_path"] = None
    return copied


# --------------------------------------------------------------------- rendering

def _speaker(rec: dict, display: str) -> str:
    d = rec.get("direction")
    if d == "from_me":
        return "You"
    if d == "system":
        return "—"
    return display


def _media_ref(entry: dict) -> str:
    return entry.get("archive_path") or entry.get("local_path") or entry.get("remote_url") or ""


def _price_tag(rec: dict) -> str:
    if rec.get("is_tip") and rec.get("price"):
        return f"  💲 Tip ${rec['price']}"
    if rec.get("price"):
        locked = " (locked)" if rec.get("is_locked") else ""
        return f"  💲 PPV ${rec['price']}{locked}"
    if rec.get("is_locked"):
        return "  🔒 locked"
    return ""


def render_md(records: list, display: str, multi: bool, tz: str) -> str:
    lines = [f"# Conversation with {display}", ""]
    services = sorted({r["service"] for r in records})
    if records:
        span = f"{ac.local_date(records[0]['ts_utc'], tz)} → {ac.local_date(records[-1]['ts_utc'], tz)}"
        lines.append(f"_{' + '.join(s.title() for s in services)} · {len(records)} messages · {span}_")
        lines.append("")
    cur_day = None
    for rec in records:
        day = _day_header(ac.local_dt(rec["ts_utc"], tz))
        if day != cur_day:
            cur_day = day
            lines += ["", f"## {day}", ""]
        who = _speaker(rec, display)
        via = f" · via {rec['service'].title()}" if multi else ""
        stamp = ac.hhmm(rec["ts_utc"], tz)
        if rec.get("direction") == "system":
            lines.append(f"_{rec.get('text', '').strip()}_ · {stamp}{via}")
            lines.append("")
            continue
        lines.append(f"**{who}** · {stamp}{via}{_price_tag(rec)}")
        if rec.get("text"):
            lines.append(rec["text"])
        for entry in rec.get("media", []):
            ref = _media_ref(entry)
            kind = entry.get("kind", "file")
            if not ref:
                lines.append(f"_[{kind} — unavailable]_")
            elif kind in ("photo", "sticker"):
                lines.append(f"![{kind}]({ref})")
            else:
                dur = (entry.get("meta") or {}).get("duration_seconds")
                dtxt = f" ({dur}s)" if dur else ""
                lines.append(f"[🎬 {kind}{dtxt}]({ref})")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


_HTML_HEAD = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Conversation with {title}</title>
<style>
:root{{--me:#8e00ff;--them:#e9e9ee;--bg:#fafafa;--ink:#1a1a1a}}
body{{font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:24px}}
.wrap{{max-width:720px;margin:0 auto}}
h1{{font-size:20px}} .sub{{color:#777;margin-bottom:24px}}
.day{{text-align:center;color:#999;font-size:12px;margin:22px 0 10px;text-transform:uppercase;letter-spacing:.06em}}
.row{{display:flex;margin:6px 0}} .row.me{{justify-content:flex-end}}
.bub{{max-width:76%;padding:8px 12px;border-radius:14px;background:var(--them);white-space:pre-wrap;word-wrap:break-word}}
.me .bub{{background:var(--me);color:#fff}}
.meta{{font-size:11px;color:#888;margin:0 6px 2px}} .me .meta{{text-align:right}}
.sys{{text-align:center;color:#aaa;font-size:12px;font-style:italic;margin:10px 0}}
.tag{{font-size:11px;opacity:.85}}
img,video{{max-width:100%;border-radius:10px;display:block;margin:4px 0}}
audio{{width:240px;margin:4px 0}}
a.file{{color:inherit}}
</style></head><body><div class="wrap">
<h1>Conversation with {title}</h1><div class="sub">{sub}</div>
"""


def _media_html(entry: dict) -> str:
    ref = html.escape(_media_ref(entry))
    kind = entry.get("kind", "file")
    if not ref:
        return f'<div class="tag">[{kind} — unavailable]</div>'
    if kind in ("photo", "sticker", "animation"):
        return f'<img src="{ref}" alt="{kind}">'
    if kind == "video":
        return f'<video controls src="{ref}"></video>'
    if kind in ("voice", "audio"):
        return f'<audio controls src="{ref}"></audio>'
    return f'<a class="file" href="{ref}">📎 {kind}</a>'


def render_html(records: list, display: str, multi: bool, tz: str) -> str:
    services = sorted({r["service"] for r in records})
    span = ""
    if records:
        span = f"{ac.local_date(records[0]['ts_utc'], tz)} → {ac.local_date(records[-1]['ts_utc'], tz)}"
    sub = html.escape(f"{' + '.join(s.title() for s in services)} · {len(records)} messages · {span}")
    out = [_HTML_HEAD.format(title=html.escape(display), sub=sub)]
    cur_day = None
    for rec in records:
        day = _day_header(ac.local_dt(rec["ts_utc"], tz))
        if day != cur_day:
            cur_day = day
            out.append(f'<div class="day">{html.escape(day)}</div>')
        if rec.get("direction") == "system":
            out.append(f'<div class="sys">{html.escape(rec.get("text", "").strip())}</div>')
            continue
        me = rec.get("direction") == "from_me"
        who = _speaker(rec, display)
        via = f" · {rec['service'].title()}" if multi else ""
        meta = f'{html.escape(who)} · {ac.hhmm(rec["ts_utc"], tz)}{html.escape(via)}{html.escape(_price_tag(rec))}'
        body = html.escape(rec.get("text", ""))
        media = "".join(_media_html(e) for e in rec.get("media", []))
        out.append(f'<div class="row {"me" if me else "them"}"><div>'
                   f'<div class="meta">{meta}</div>'
                   f'<div class="bub">{body}{media}</div></div></div>')
    out.append("</div></body></html>")
    return "\n".join(out)


# --------------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="Merge + render a person's cross-service conversation archive.")
    p.add_argument("--person", required=True)
    p.add_argument("--people-file", default=ac.DEFAULT_PEOPLE_FILE)
    p.add_argument("--in", dest="in_files", action="append", help="normalized json (repeatable)")
    p.add_argument("--out-dir", help="default <ARCHIVE_OUT_DIR>/<person>/")
    p.add_argument("--formats", default="json,md,html")
    p.add_argument("--no-copy-media", action="store_true")
    p.add_argument("--tz", default="auto",
                   help="auto (the owner's configured timezone, else machine-local) | UTC | ±minutes")
    p.add_argument("--env-file")
    args = p.parse_args()

    try:
        cfg = ac.load_env(args.env_file)
        people = ac.load_people(args.people_file)
        display = ac.person_display(people, args.person)
        out_dir = args.out_dir or os.path.join(cfg["ARCHIVE_OUT_DIR"], args.person)
        in_files = args.in_files or sorted(glob.glob(os.path.join(out_dir, "normalized", "*.json")))
        if not in_files:
            return _fail(f"no normalized inputs for {args.person!r} (looked in {out_dir}/normalized/)")

        records = merge(load_records(in_files))
        if not records:
            return _fail("no messages found in the normalized inputs")
        services = sorted({r["service"] for r in records})
        multi = len(services) > 1

        media_dir = os.path.join(out_dir, "media")
        copied = collect_media(records, media_dir, copy=not args.no_copy_media)

        formats = {f.strip() for f in args.formats.split(",") if f.strip()}
        outputs = {}
        if "json" in formats:
            fp = os.path.join(out_dir, "conversation.json")
            ac.write_json(fp, ac.envelope(args.person, "+".join(services), records,
                                          sources=in_files, generated_at=ac.iso_utc(int(time.time()))))
            outputs["json"] = fp
        if "md" in formats:
            fp = os.path.join(out_dir, "conversation.md")
            _write_text(fp, render_md(records, display, multi, args.tz))
            outputs["md"] = fp
        if "html" in formats:
            fp = os.path.join(out_dir, "conversation.html")
            _write_text(fp, render_html(records, display, multi, args.tz))
            outputs["html"] = fp

        print(json.dumps({"ok": True, "person": args.person, "count": len(records),
                          "services": services, "media_copied": copied, "outputs": outputs},
                         ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        return _fail(str(e))


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _fail(msg: str) -> int:
    print(json.dumps({"ok": False, "error": msg}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
