#!/usr/bin/env python3
"""Proteus hourly hunt cycle (stdlib only, no LLM): fetch → score → diff → notify → ledger.

Runs unattended every hour (Task Scheduler → run-proteus-hunt.cmd). Costs nothing but HTTP:
it reuses fetch_jobs.py + score_jobs.py deterministically, diffs the scored set against a rolling
seen-ledger, pushes a Telegram nudge for genuinely NEW hot matches (score ≥ threshold, no
dealbreaker flags, capped per cycle, quiet-window aware), and appends a cycle record the daily
digest (daily_digest.py) rolls up. The full Proteus archon (workups, cover letters) stays
on-demand — this loop is the tripwire, not the writer.

State (all under archons/proteus/out/hourly/, gitignored):
  seen.json     url → {first_seen, last_seen, title, company, best_score, last_score, flags,
                       comp_max, location, notified}
  cycles.jsonl  one line per run: {at, fetched, kept, new_hot, notified, deferred, errors}
  jobs-latest.json / scored-latest.json   most recent fetch/score output
  paused        (sentinel file) — exists → the cycle exits immediately (pause switch)

Run:  python hunt_cycle.py [--threshold 60] [--notify-cap 3] [--no-notify] [--dry-run]
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROTEUS_DIR = os.path.dirname(TOOLS_DIR)
REPO_ROOT = os.path.dirname(os.path.dirname(PROTEUS_DIR))
OUT_DIR = os.path.join(PROTEUS_DIR, "out", "hourly")
QUIET_FILE = os.path.join(REPO_ROOT, "margo", "state", "quiet.json")
TELEGRAM_SEND = os.path.join(REPO_ROOT, "margo", "scripts", "telegram_send.py")
TELEGRAM_ENV = os.path.join(REPO_ROOT, "margo", "scripts", "telegram.env")

# Only alert on a fresh find; older ones wait for the daily digest.
NOTIFY_FRESH_HOURS = 6.0


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def quiet_active(path: str = QUIET_FILE) -> bool:
    """True while a do-not-disturb window is open (job alerts defer to it; digest catches all)."""
    try:
        with open(path, encoding="utf-8") as fh:
            until = json.load(fh).get("until") or ""
        return datetime.now(timezone.utc) < datetime.fromisoformat(until.replace("Z", "+00:00"))
    except (OSError, ValueError, AttributeError):
        return False


def diff_jobs(scored_jobs: list[dict], ledger: dict, threshold: float, at: str) -> tuple[list[dict], dict]:
    """Fold this cycle's scored jobs into the ledger; return (newly-hot jobs, updated ledger).

    Newly-hot = first time this URL is seen at/above threshold with no dealbreaker flag —
    either brand new, or a previously-seen job whose score rose across the threshold.
    Pure function (no I/O) so it stays unit-testable.
    """
    newly_hot = []
    for job in scored_jobs:
        url = job.get("url") or f"{job.get('source')}:{job.get('external_id')}"
        score = float(job.get("match_percent") or 0.0)
        flags = job.get("flags") or []
        entry = ledger.get(url)
        was_hot = bool(entry and float(entry.get("best_score") or 0.0) >= threshold)
        if entry is None:
            entry = {"first_seen": at, "notified": False}
        entry.update({
            "last_seen": at,
            "title": job.get("title"),
            "company": job.get("company"),
            "last_score": score,
            "best_score": max(score, float(entry.get("best_score") or 0.0)),
            "flags": flags,
            "comp_min": job.get("comp_min"),
            "comp_max": job.get("comp_max"),
            "location": job.get("location"),
            "remote_verdict": job.get("remote_verdict"),
        })
        ledger[url] = entry
        dealbroken = any(str(f).startswith("dealbreaker") for f in flags)
        if score >= threshold and not was_hot and not dealbroken:
            newly_hot.append(dict(job, url=url))
    return newly_hot, ledger


def fresh_enough(entry: dict, at: str, hours: float = NOTIFY_FRESH_HOURS) -> bool:
    try:
        first = datetime.fromisoformat(entry["first_seen"].replace("Z", "+00:00"))
        now = datetime.fromisoformat(at.replace("Z", "+00:00"))
        return (now - first).total_seconds() <= hours * 3600
    except (KeyError, ValueError):
        return True


VERDICT_LABEL = {
    "remote": "✅ true remote",
    "remote?": "❓ remote mentioned — verify",
    "commutable": "🏢 commutable (home zone)",
    "relocation": "🚚 relocation implied",
    "unknown": "❓ location unclear",
}


def comp_label(entry: dict) -> str:
    low, high = entry.get("comp_min"), entry.get("comp_max")
    if low:
        return f"${int(low)//1000}k+"
    if high:
        return f"up to ${int(high)//1000}k"
    return "comp n/p"


def notify_line(job: dict) -> str:
    verdict = VERDICT_LABEL.get(job.get("remote_verdict") or "", job.get("location") or "location n/a")
    return (f"🛰️ Proteus: new {job['match_percent']:.1f}% match — {job.get('company')} · "
            f"{job.get('title')} · {comp_label(job)} · {verdict}\n{job.get('url')}")


def send_telegram(text: str) -> bool:
    try:
        result = subprocess.run(
            [sys.executable, TELEGRAM_SEND, "--env-file", TELEGRAM_ENV,
             "--text", text, "--parse-mode", "", "--disable-preview"],
            capture_output=True, text=True, timeout=60)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_tool(script: str, args: list[str]) -> None:
    result = subprocess.run([sys.executable, os.path.join(TOOLS_DIR, script), *args],
                            capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"{script} failed rc={result.returncode}: {result.stderr[-400:]}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Proteus hourly hunt cycle (fetch+score+diff+notify).")
    parser.add_argument("--threshold", type=float, default=60.0)
    parser.add_argument("--notify-cap", type=int, default=3)
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="fetch+score+diff but write/send nothing")
    args = parser.parse_args(argv)

    if os.path.exists(os.path.join(OUT_DIR, "paused")):
        print(json.dumps({"ok": True, "skipped": "paused"}))
        return 0
    os.makedirs(OUT_DIR, exist_ok=True)
    at = now_iso()
    jobs_path = os.path.join(OUT_DIR, "jobs-latest.json")
    scored_path = os.path.join(OUT_DIR, "scored-latest.json")

    run_tool("fetch_jobs.py", ["--watchlist", os.path.join(PROTEUS_DIR, "watchlist.json"),
                               "--out", jobs_path])
    run_tool("score_jobs.py", ["--profile", os.path.join(PROTEUS_DIR, "profile.json"),
                               "--jobs", jobs_path, "--out", scored_path, "--top", "0"])

    with open(scored_path, encoding="utf-8") as fh:
        scored_doc = json.load(fh)
    ledger_path = os.path.join(OUT_DIR, "seen.json")
    ledger = {}
    if os.path.exists(ledger_path):
        with open(ledger_path, encoding="utf-8") as fh:
            ledger = json.load(fh)

    newly_hot, ledger = diff_jobs(scored_doc.get("jobs", []), ledger, args.threshold, at)

    notified, deferred = [], []
    quiet = quiet_active()
    for job in sorted(newly_hot, key=lambda j: j["match_percent"], reverse=True):
        entry = ledger[job["url"]]
        if entry.get("notified") or args.no_notify or args.dry_run:
            continue
        if quiet or len(notified) >= args.notify_cap or not fresh_enough(entry, at):
            deferred.append(job["url"])  # the daily digest still carries it
            continue
        if send_telegram(notify_line(job)):
            entry["notified"] = True
            notified.append(job["url"])

    if not args.dry_run:
        with open(ledger_path, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, ensure_ascii=False, indent=1)
        with open(os.path.join(OUT_DIR, "cycles.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": at, "fetched": scored_doc.get("considered"),
                                 "kept": scored_doc.get("kept"), "new_hot": len(newly_hot),
                                 "notified": notified, "deferred": deferred,
                                 "quiet": quiet}, ensure_ascii=False) + "\n")

    print(json.dumps({"ok": True, "at": at, "considered": scored_doc.get("considered"),
                      "new_hot": len(newly_hot), "notified": len(notified),
                      "deferred": len(deferred), "quiet": quiet}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
