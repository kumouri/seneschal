#!/usr/bin/env python3
"""Proteus daily job digest (stdlib only, no LLM): roll up the hourly hunt cycles into one email.

Runs once a day (Task Scheduler → run-proteus-digest.cmd). Reads the hourly loop's seen-ledger +
cycle log (hunt_cycle.py) and builds a Markdown digest so nothing slips past unseen: new hot
matches (alerted or not), near-misses worth a second look, flagged-but-strong postings, and jobs
that disappeared since yesterday (the missed-window signal). Emails it to the owner via Proton and
pings Telegram with the one-liner. Deterministic — full Proteus workups stay on-demand.

Run:  python daily_digest.py [--date YYYY-MM-DD] [--threshold 60] [--near 45]
                             [--no-email] [--no-telegram] [--dry-run]
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROTEUS_DIR = os.path.dirname(TOOLS_DIR)
REPO_ROOT = os.path.dirname(os.path.dirname(PROTEUS_DIR))
sys.path.insert(0, TOOLS_DIR)
import proteus_paths  # noqa: E402  (canonical state/ vs out/ paths)

# runtime churn reads from state/; the digest itself is a deliverable, so out/digests/
HOURLY_DIR = str(proteus_paths.STATE_DIR)
DIGEST_DIR = str(proteus_paths.DIGEST_DIR)
SCRIPTS = os.path.join(REPO_ROOT, "seneschal", "scripts")

EMAIL_TO = os.environ.get("PROTEUS_DIGEST_TO", "")  # the owner's address; set via env or run-proteus-digest.cmd


def _day_of(iso: str) -> str:
    """Local calendar date of a UTC ISO stamp (the owner's zone is assumed to be the machine's
    local zone — the same convention `seneschal/scripts/tz_common.py` resolves for the rest of
    the suite)."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ""


VERDICT_LABEL = {
    "remote": "✅ true remote",
    "remote?": "❓ remote mentioned — verify",
    "commutable": "🏢 commutable (home zone)",
    "relocation": "🚚 relocation implied",
    "unknown": "❓ location unclear",
}


def _comp(entry: dict) -> str:
    low, high = entry.get("comp_min"), entry.get("comp_max")
    if low:
        return f"${int(low)//1000}k+"
    if high:
        return f"up to ${int(high)//1000}k"
    return "comp n/p"


def _fmt(url: str, entry: dict) -> str:
    """Headline: company · title · min comp · true-remote verdict · score."""
    verdict = VERDICT_LABEL.get(entry.get("remote_verdict") or "")
    if not verdict:  # pre-verdict ledger rows: fall back to the raw location so nothing hides
        verdict = f"📍 {entry.get('location') or 'location n/a'}"
    flags = entry.get("flags") or []
    flag_s = "".join(f"\n  - ⚠️ {f}" for f in flags)
    return (f"- **{entry.get('company')}** · {entry.get('title')} · {_comp(entry)} · {verdict} · "
            f"**{entry.get('last_score', 0):.1f}%**\n  ({entry.get('location') or 'location n/a'}) "
            f"{url}{flag_s}")


def build_digest(ledger: dict, cycles: list[dict], day: str, threshold: float, near: float) -> tuple[str, dict]:
    """Pure builder: (markdown, stats). Sections: target-title, new-hot, near-miss, flagged, gone."""
    new_today = {u: e for u, e in ledger.items() if _day_of(e.get("first_seen", "")) == day}
    clean = lambda e: not any(str(f).startswith("dealbreaker") for f in e.get("flags") or [])
    # Express tier: literally titled one of the profile's targets, takeable — the roles worth eyes first.
    target_hits = {u: e for u, e in new_today.items()
                   if e.get("target_title") and clean(e)
                   and e.get("remote_verdict") in ("remote", "commutable", "remote?")}
    hot = {u: e for u, e in new_today.items()
           if float(e.get("best_score") or 0) >= threshold and clean(e)
           and u not in target_hits}
    near_miss = {u: e for u, e in new_today.items()
                 if near <= float(e.get("best_score") or 0) < threshold and clean(e)
                 and u not in target_hits}
    flagged = {u: e for u, e in new_today.items() if (e.get("flags") or [])}
    prev_day = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    gone = {u: e for u, e in ledger.items()
            if _day_of(e.get("last_seen", "")) == prev_day
            and float(e.get("best_score") or 0) >= near}

    day_cycles = [c for c in cycles if _day_of(c.get("at", "")) == day]
    notified = {u for c in day_cycles for u in c.get("notified", [])}
    stats = {"cycles": len(day_cycles), "new_today": len(new_today), "targets": len(target_hits),
             "hot": len(hot), "near": len(near_miss), "flagged": len(flagged), "gone": len(gone),
             "ledger": len(ledger)}

    lines = [f"Proteus daily job digest — {day}", "",
             f"{stats['cycles']} hunt cycles today · {stats['new_today']} new postings · "
             f"{stats['targets']} target-title matches · {stats['hot']} more above {threshold:.0f}% · "
             f"ledger tracks {stats['ledger']} total.", ""]

    lines.append(f"## 🎯 Target-title matches ({len(target_hits)}) — roles literally in your lane")
    if target_hits:
        for url, e in sorted(target_hits.items(), key=lambda kv: -float(kv[1].get("best_score") or 0)):
            tag = " *(alerted)*" if url in notified else ""
            lines.append(_fmt(url, e) + f"  ⟵ **{e.get('target_title')}**" + tag)
    else:
        lines.append("- none today.")
    lines.append("")

    lines.append(f"## Also at/above {threshold:.0f}% ({len(hot)})")
    if hot:
        for url, e in sorted(hot.items(), key=lambda kv: -float(kv[1].get("best_score") or 0)):
            tag = " *(alerted)*" if url in notified else " *(not alerted — catch it here)*"
            lines.append(_fmt(url, e) + tag)
    else:
        lines.append("- none — quiet day above the line.")
    lines.append("")

    def _section(title: str, entries: dict, cap: int) -> None:
        lines.append(title)
        if entries:
            ranked = sorted(entries.items(), key=lambda kv: -float(kv[1].get("best_score") or 0))
            lines.extend(_fmt(u, e) for u, e in ranked[:cap])
            if len(ranked) > cap:
                lines.append(f"- …and {len(ranked) - cap} more in the ledger.")
        else:
            lines.append("- none.")

    _section(f"## Near misses {near:.0f}–{threshold:.0f}% ({len(near_miss)}) — worth a human eye",
             near_miss, 12)
    lines.append("")

    _section(f"## Flagged today ({len(flagged)}) — excluded or docked, with reasons", flagged, 8)
    lines.append("")

    _section(f"## Gone since yesterday ({len(gone)}) — closed or delisted, in case one got away",
             gone, 8)
    lines.append("")
    lines.append("Reply to the assistant to work any of these up (tailored resume + cover letter + research) — "
                 "nothing is applied to without your word.")
    return "\n".join(lines), stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Proteus daily job digest (rollup of hourly cycles).")
    parser.add_argument("--date", default=datetime.now().astimezone().strftime("%Y-%m-%d"))
    parser.add_argument("--threshold", type=float, default=60.0)
    parser.add_argument("--near", type=float, default=45.0)
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    ledger, cycles = {}, []
    try:
        with open(os.path.join(HOURLY_DIR, "seen.json"), encoding="utf-8") as fh:
            ledger = json.load(fh)
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(HOURLY_DIR, "cycles.jsonl"), encoding="utf-8") as fh:
            cycles = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        pass

    markdown, stats = build_digest(ledger, cycles, args.date, args.threshold, args.near)
    if args.dry_run:
        print(markdown)
        return 0

    os.makedirs(DIGEST_DIR, exist_ok=True)
    digest_path = os.path.join(DIGEST_DIR, f"{args.date}.md")
    with open(digest_path, "w", encoding="utf-8") as fh:
        fh.write(markdown)

    sent = {"email": False, "telegram": False}
    subject = (f"Proteus daily job digest — {args.date} "
               f"({stats['targets']} target-title, {stats['hot']} more ≥{args.threshold:.0f}%, "
               f"{stats['near']} near)")
    if not args.no_email:
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "proton_send.py"),
             "--env-file", os.path.join(SCRIPTS, "proton.env"),
             "--to", EMAIL_TO, "--subject", subject, "--body-file", digest_path],
            capture_output=True, text=True, timeout=120)
        sent["email"] = result.returncode == 0
    if not args.no_telegram:
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "telegram_send.py"),
             "--env-file", os.path.join(SCRIPTS, "telegram.env"),
             "--text", f"📬 {subject} — emailed to {EMAIL_TO}.", "--parse-mode", "", "--disable-preview"],
            capture_output=True, text=True, timeout=60)
        sent["telegram"] = result.returncode == 0

    print(json.dumps({"ok": True, "digest": digest_path, "stats": stats, "sent": sent}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
