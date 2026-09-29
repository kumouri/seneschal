#!/usr/bin/env python3
"""Proteus hourly hunt cycle (stdlib only, no LLM): fetch → score → hydrate → re-score → diff →
notify → ledger.

Runs unattended every hour (Task Scheduler → run-proteus-hunt.cmd). Costs nothing but HTTP:
it reuses fetch_jobs.py + score_jobs.py deterministically, buys job descriptions for a small
scored shortlist (``hydrate_and_rescore`` below), diffs the scored set against a rolling
seen-ledger, pushes a Telegram nudge for genuinely NEW hot matches (score ≥ threshold, no
dealbreaker flags, capped per cycle, quiet-window aware), and appends a cycle record the daily
digest (daily_digest.py) rolls up. The full Proteus archon (workups, cover letters) stays
on-demand — this loop is the tripwire, not the writer.

Every alert carries the posting link, plus a distinct apply link when the feed gave one (see
``link_block``).

State (all under archons/proteus/state/, gitignored — see proteus_paths.py for the
archon-wide state/ vs out/ rule):
  seen.json     url → {first_seen, last_seen, title, company, best_score, last_score, flags,
                       comp_max, location, apply_url, notified}
  cycles.jsonl  one line per run: {at, fetched, kept, new_hot, notified, deferred, quiet, hydration}
  jobs-latest.json / scored-latest.json   most recent fetch/score output
  paused        (sentinel file) — exists → the cycle exits immediately (pause switch)

Run:  python hunt_cycle.py [--threshold 60] [--notify-cap 3] [--no-notify] [--dry-run]
                           [--hydrate-limit 25]
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

sys.path.insert(0, TOOLS_DIR)
import proteus_paths  # noqa: E402  (canonical state/ vs out/ paths)
from source_tiers import ghost_risk  # noqa: E402  (same per-source truth the scorer uses)

# Runtime churn lives in state/, deliverables in out/ (the archon-wide rule).
OUT_DIR = str(proteus_paths.STATE_DIR)
SENESCHAL_SCRIPTS = os.path.join(REPO_ROOT, "seneschal", "scripts")
SENESCHAL_STATE = os.path.join(REPO_ROOT, "seneschal", "state")
QUIET_FILE = os.path.join(SENESCHAL_STATE, "quiet.json")
TELEGRAM_SEND = os.path.join(SENESCHAL_SCRIPTS, "telegram_send.py")
TELEGRAM_ENV = os.path.join(SENESCHAL_SCRIPTS, "telegram.env")

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
        target_title = job.get("target_title")
        dealbroken = any(str(f).startswith("dealbreaker") for f in flags)
        remote_ok = job.get("remote_verdict") in ("remote", "commutable")
        # A role is hot if it clears the score bar OR it's a structural target-title match that's
        # actually takeable (true-remote/commutable, no dealbreaker) — the express lane, because a
        # role literally titled one of the profile's targets shouldn't need a comp-posted score to
        # reach the owner. The express lane bypasses the score bar, so it needs its own age gate —
        # otherwise a year-old role titled exactly like a target still pings no matter how far age
        # decay sank its score. Source ghost-risk deliberately does NOT get a matching gate here;
        # it marks the nudge instead — see notify_line.
        stale = (job.get("age_tier") or "") in ("old", "ancient")
        express = bool(target_title) and remote_ok and not dealbroken and not stale
        hot = (score >= threshold or express) and not dealbroken

        entry = ledger.get(url)
        was_hot = bool(entry and (float(entry.get("best_score") or 0.0) >= threshold
                                  or entry.get("was_express")))
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
            "target_title": target_title,
            "was_express": bool(entry.get("was_express")) or express,
            # Carried so the daily digest can offer the same "apply here, not there" distinction the
            # alert does. Additive AND sticky: an older ledger row simply has no key, and a later
            # fetch that omits it doesn't erase a URL already learned (both readers treat absent as
            # "no separate apply link").
            "apply_url": job.get("apply_url") or entry.get("apply_url") or "",
        })
        ledger[url] = entry
        if hot and not was_hot:
            newly_hot.append(dict(job, url=url, _express=express))
    return newly_hot, ledger


# --- Lazy description hydration -----------------------------------------------------------------
# WTTJ list rows carry NO description — `fetch_jobs.fetch_wttj_company` is list-only by design: one
# detail call per posting per cycle is ~25 requests per company per hour, which across a watchlist of
# a hundred-odd companies is >100k requests/day at one free public API. Indefensible, and it would
# get the honest User-Agent blocked. The other half of that design — buy the text LAZILY, for the
# jobs that actually matter (a scored shortlist) — is `fetch_jobs.hydrate_wttj_descriptions`, and
# THIS is its caller. Without it, most of a WTTJ-heavy board sits at zero JD text indefinitely and a
# work-up has to reconstruct the req from a title.
#
# THE SHORTLIST IS THE BUDGET, AND THAT IS NOT A RESTATEMENT OF `limit`. hydrate_wttj_descriptions'
# own `limit` counts SUCCESSES — a detail call that fails, or returns a job with no prose, does not
# increment it and the loop keeps walking — so on a large text-less pool `limit` alone bounds
# nothing. The LENGTH OF THE LIST HANDED IN is the hard request bound, and `hydration_shortlist`
# is what enforces it.
HYDRATE_LIMIT = 25
# A text-less row is systematically UNDER-scored: the skills band and every preference boost read
# the description, so its pre-hydration score is a floor rather than an estimate. Rows just under
# the bar are therefore exactly where hydration changes an outcome, and including them costs no
# requests — HYDRATE_LIMIT still decides how many go.
HYDRATE_SCORE_MARGIN = 10.0


def hydration_shortlist(scored_jobs: list[dict], threshold: float, limit: int = HYDRATE_LIMIT,
                        margin: float = HYDRATE_SCORE_MARGIN) -> list[dict]:
    """The text-less jobs worth spending a detail request on this cycle — small, ranked, bounded.

    Eligible = a WTTJ row with no description that is about to reach the owner: at or over the score
    bar, within `margin` of it, or an express-lane candidate (a takeable target-title match, which
    ``diff_jobs`` alerts REGARDLESS of score — so those must be able to earn text at any score).

    **Express rows are ranked ABOVE the score-bar cohort, not merged into it by score.** Ranking on
    score alone — including ranking an express row *as if* it sat at the threshold — lets high
    scorers fill the cap every hour forever, so a low-scoring express row that is genuinely alerted
    every cycle would never once be hydrated. That is the exact failure this whole step exists to
    fix, reproduced inside the fix. Express rows can in principle fill the cap themselves; that is
    the RIGHT worst case, since they are the rows `diff_jobs` alerts unconditionally — and they are
    self-limiting anyway, requiring a target-title match AND takeable AND not stale.

    **Dealbroken rows are excluded, and that is a trade rather than a tidy-up.** They are never
    alerted, so they are not jobs that matter this cycle — but hydration could in principle CLEAR a
    relocation dealbreaker that a text-less row earned. WTTJ list rows carry a structured remote flag
    and structured offices, so that verdict is not made from nothing, and with thousands of text-less
    rows competing for a couple dozen slots the alternative is spending the budget on rows that are
    not reaching the owner at the expense of rows that are.

    Pure — no I/O, no imports — so the budget is unit-testable. Returns at most `limit` jobs, and
    that count IS the per-cycle request bound (see HYDRATE_LIMIT).
    """
    candidates: list[tuple[bool, float, dict]] = []
    for job in scored_jobs:
        if job.get("source") != "welcometothejungle":
            continue                                  # the only source the hydrator can fetch
        if (job.get("description") or "").strip():
            continue                                  # already has text; nothing to buy
        if any(str(flag).startswith("dealbreaker") for flag in (job.get("flags") or [])):
            continue                                  # never alerted — see the docstring
        score = float(job.get("match_percent") or 0.0)
        # The same express predicate diff_jobs applies, minus the dealbreaker term handled above.
        express = (bool(job.get("target_title"))
                   and job.get("remote_verdict") in ("remote", "commutable")
                   and (job.get("age_tier") or "") not in ("old", "ancient"))
        if not (express or score >= threshold - margin):
            continue
        candidates.append((express, score, job))
    # key= compares the (bool, float) pair only; the job dict is never an operand (dicts are
    # unorderable, so putting it in a plain tuple sort would raise on any tie).
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    # max(0, ...) because a negative slice bound would take everything BUT the tail — i.e. a
    # nonsense `--hydrate-limit -5` would spend MORE requests than the default, not fewer.
    return [job for _, _, job in candidates[:max(0, limit)]]


def _score_inputs(job: dict) -> tuple:
    """The fields hydration can change that scoring actually reads — the re-score trigger."""
    return (len(job.get("description") or ""), job.get("comp_min"), job.get("comp_max"))


def hydrate_and_rescore(scored_doc: dict, profile_path: str, threshold: float,
                        limit: int = HYDRATE_LIMIT) -> dict:
    """Buy descriptions for the shortlist, then RE-SCORE exactly the rows that gained something.

    **The order is hydrate → re-score → diff, deliberately, and never hydrate → diff.**
    ``diff_jobs`` decides off ``match_percent``, ``flags``, ``target_title``, ``remote_verdict``
    and ``age_tier`` — and four of those five are derived from title + description. Leaving the
    score stale would put the req's text in the dict while the JUDGEMENT about it stayed
    text-less: the same defect this function exists to fix, moved one layer up and made more
    confident. It matters most in the REFUSING direction — a newly-fetched "requires an active
    TS/SCI clearance", or "3 days a week in our Paris office", has to be able to STOP an alert,
    and a score computed before the text arrived cannot. Re-scoring is local CPU over a couple dozen
    dicts, so the request budget never bears on the choice.

    One round per cycle: the shortlist is picked on PRE-hydration scores, so a row that would only
    become shortlist-eligible after some other row's text arrived waits for the next cycle. That is
    accepted — the alternative is an unbounded loop against the request budget.

    Only rows whose scoring inputs ACTUALLY CHANGED are re-scored. A detail call that failed leaves
    its row byte-for-byte as scoring left it: visibly text-less, never a placeholder, and never a
    score silently re-derived from the same emptiness.

    Returns a summary dict. Imports its siblings lazily and does not defend itself — the caller
    wraps it, because a hunt may not die on an enrichment step.
    """
    import fetch_jobs
    import score_jobs

    jobs = scored_doc.get("jobs") or []
    shortlist = hydration_shortlist(jobs, threshold, limit)
    summary = {"shortlisted": len(shortlist), "hydrated": 0, "rescored": 0, "warnings": 0}
    if not shortlist:
        return summary

    positions = {id(job): index for index, job in enumerate(jobs)}
    before = {id(job): _score_inputs(job) for job in shortlist}
    warnings: list[str] = []
    # `limit` is belt-and-braces here: the list itself is already the bound. Per-job failure is a
    # warning inside the hydrator, never fatal.
    fetch_jobs.hydrate_wttj_descriptions(shortlist, warnings, limit=len(shortlist))
    summary["warnings"] = len(warnings)

    with open(profile_path, encoding="utf-8") as fh:
        profile = json.load(fh)
    intel = score_jobs.load_company_intel(
        os.path.join(os.path.dirname(os.path.abspath(profile_path)), "company-intel.json"))

    for job in shortlist:
        if _score_inputs(job) == before[id(job)]:
            continue                                  # nothing arrived — leave the row untouched
        summary["hydrated"] += 1
        index = positions.get(id(job))
        if index is None:                             # pragma: no cover - defensive
            continue
        jobs[index] = score_jobs.score_job(job, profile, intel)
        summary["rescored"] += 1
    if summary["rescored"]:
        jobs.sort(key=lambda job: job.get("match_percent") or 0.0, reverse=True)
    return summary


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


def link_block(url: str, apply_url: str = "") -> list[str]:
    """The link lines for one job: the posting, and a distinct apply URL if there is one.

    The apply line appears only when it is genuinely a different link (``proteus_paths.norm_url``):
    a mirror or a republished req whose feed says where the employer's own req lives. Degrades one
    line at a time — no URL at all still leaves the headline, because a missing link must never
    cost the owner an alert about a real job.
    """
    lines = [f"📄 posting: {url}"] if url else []
    apply_url = (apply_url or "").strip()
    if apply_url and proteus_paths.norm_url(apply_url) != proteus_paths.norm_url(url or ""):
        lines.append(f"📨 apply: {apply_url}")
    return lines


def notify_line(job: dict) -> str:
    verdict = VERDICT_LABEL.get(job.get("remote_verdict") or "", job.get("location") or "location n/a")
    tag = f"🎯 {job['target_title']}" if job.get("_express") else f"new {job['match_percent']:.1f}% match"
    # A high-ghost-risk posting is MARKED, not blocked. The express lane deliberately bypasses the
    # score bar, so the scorer's ghost-risk dock can't reach it — the obvious fix would be to gate
    # express on risk the way it already gates on age tier. But a gate only changes the outcome for
    # the express-only rows (below the score bar), and suppressing those wholesale destroys the
    # evidence needed to judge the source itself. The lane exists to notify, not to apply; what makes
    # the ping safe is that it arrives honest about what it is.
    risk = "  ⚠️ verify at source" if ghost_risk(job.get("source")) == "high" else ""
    head = (f"🛰️ Proteus: {tag} — {job.get('company')} · "
            f"{job.get('title')} · {comp_label(job)} · {verdict}{risk}")
    return "\n".join([head, *link_block(job.get("url") or "", job.get("apply_url") or "")])


def record_assertion(text: str) -> bool:
    """Log this ping to the assistant's assertions log, when that module is installed — Proteus is
    one of the assistant's hands, so what it says to the owner belongs in the same record as what
    the assistant says itself.

    OPTIONAL HOOK: ``mouth.py`` lives in ``seneschal/scripts/`` and may not be present in every
    checkout, so the import is lazy + guarded: this is a scheduled unattended loop, and a missing
    sibling must cost the row, never the hunt. It writes to a gitignored ``state/`` file, so the "an
    archon never writes a tracked file directly" rule is not in play."""
    try:
        if SENESCHAL_SCRIPTS not in sys.path:
            sys.path.append(SENESCHAL_SCRIPTS)
        import mouth

        return mouth.record_assertion(SENESCHAL_STATE, surface="telegram", kind="archon",
                                      speaker="proteus", text=text)
    except Exception:  # noqa: BLE001 — see the docstring
        return False


def send_telegram(text: str) -> bool:
    try:
        result = subprocess.run(
            [sys.executable, TELEGRAM_SEND, "--env-file", TELEGRAM_ENV,
             "--text", text, "--parse-mode", "", "--disable-preview"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    record_assertion(text)
    return True


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
    parser.add_argument("--hydrate-limit", type=int, default=HYDRATE_LIMIT,
                        help="max WTTJ descriptions to fetch per cycle (0 = none). This IS the "
                             "per-cycle detail-request budget — see hydration_shortlist")
    parser.add_argument("--prune-runs-days", type=int, default=proteus_paths.RUNS_RETENTION_DAYS,
                        help="delete state/runs/<run-date>/ boards older than N days (0 = keep forever). "
                             "They're forensic only — nothing reads them; the live board is scored-latest.json")
    args = parser.parse_args(argv)

    # self-healing move of runtime files from the pre-state/ layout (out/hourly, out/)
    migrated = proteus_paths.migrate_legacy_state()
    pruned = proteus_paths.prune_runs(args.prune_runs_days)

    if os.path.exists(os.path.join(OUT_DIR, "paused")):
        print(json.dumps({"ok": True, "skipped": "paused"}))
        return 0
    os.makedirs(OUT_DIR, exist_ok=True)
    at = now_iso()
    jobs_path = os.path.join(OUT_DIR, "jobs-latest.json")
    scored_path = os.path.join(OUT_DIR, "scored-latest.json")

    profile_path = os.path.join(PROTEUS_DIR, "profile.json")
    run_tool("fetch_jobs.py", ["--watchlist", os.path.join(PROTEUS_DIR, "watchlist.json"),
                               "--out", jobs_path])
    run_tool("score_jobs.py", ["--profile", profile_path,
                               "--jobs", jobs_path, "--out", scored_path, "--top", "0",
                               "--seen", os.path.join(OUT_DIR, "seen.json")])  # evergreen flagging

    with open(scored_path, encoding="utf-8") as fh:
        scored_doc = json.load(fh)

    # Buy the descriptions the WTTJ list rows don't carry, for a capped shortlist, and re-score
    # those rows BEFORE the diff decides what the owner hears about. Non-fatal by design: an
    # enrichment step may cost itself, never the hunt — a cut-short run still delivers the board it
    # scored.
    try:
        hydration = hydrate_and_rescore(scored_doc, profile_path, args.threshold,
                                        limit=args.hydrate_limit)
        if hydration.get("rescored") and not args.dry_run:
            proteus_paths.write_json(scored_path, scored_doc, indent=1)
    except Exception as exc:  # noqa: BLE001 — see above
        hydration = {"error": str(exc)}

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
        # Build-then-replace: the ledger is the only record of which jobs were already notified,
        # and a torn in-place write would re-notify every one of them on the next cycle.
        staged = ledger_path + ".tmp"
        with open(staged, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, ensure_ascii=False, indent=1)
        os.replace(staged, ledger_path)
        with open(os.path.join(OUT_DIR, "cycles.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": at, "fetched": scored_doc.get("considered"),
                                 "kept": scored_doc.get("kept"), "new_hot": len(newly_hot),
                                 "notified": notified, "deferred": deferred,
                                 "quiet": quiet, "hydration": hydration},
                                ensure_ascii=False) + "\n")

    result = {"ok": True, "at": at, "considered": scored_doc.get("considered"),
              "new_hot": len(newly_hot), "notified": len(notified),
              "deferred": len(deferred), "quiet": quiet, "hydration": hydration}
    if migrated:
        result["migrated_to_state"] = migrated
    if pruned:
        result["pruned_runs"] = pruned
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
