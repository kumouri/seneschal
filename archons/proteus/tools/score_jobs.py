#!/usr/bin/env python3
"""Proteus tool: score fetched jobs against the owner's profile (stdlib only, deterministic).

Reads the normalized jobs JSON from fetch_jobs.py and profile.json, computes a 0-100 base match
percentage per job with a transparent breakdown, and writes the ranked shortlist. The archon
reviews the shortlist and may adjust a score with a written rationale — this script is the
reproducible floor, not the final word.

Scoring bands (sum = 100):
  title      0-30   strong title keywords + seniority fit; avoid-keywords subtract
  skills     0-35   weighted skill keywords found in the description (word-boundary match)
  comp       0-15   against min/target total comp (unknown comp = neutral 8, flagged)
  location   0-15   remote/location fit
  recency    0-5    fresher postings score higher
A dealbreaker keyword caps the total at 25 and flags the job.

Run:  python score_jobs.py --profile profile.json --jobs out/jobs.json --out out/scored.json [--top 15]
"""

import argparse
import json
import os
import re
import sys
import time


def _word_hit(text: str, keyword: str) -> bool:
    """Case-insensitive whole-word/phrase match (word boundaries on both ends)."""
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])", text) is not None


# Clearance/citizenship-class requirement patterns that proxy for the profile's literal
# dealbreakers. Added after hunt #1 (2026-07-12): the literal matcher missed Databricks Public
# Sector's "U.S. citizenship is required to access classified information."
DEALBREAKER_PATTERNS = {
    "citizenship requirement": (
        r"(?:u\.?s\.?|united states) citizenship (?:is |will be )?required"
        r"|must (?:be|hold) (?:an? )?(?:u\.?s\.?|united states) citizen"
        r"|citizenship:\s*(?:u\.?s\.?|united states)"
        r"|citizenship (?:is )?required"
    ),
    "classified/clearance work": (
        r"classified (?:information|environment|work|program|material)s?"
        r"|(?:active|current)?\s*(?:secret|top secret)(?:\s*/\s*sci)? clearance"
        r"|government security investigation"
        r"|public trust clearance"
    ),
    "export-controlled work": r"\bitar\b|export.?controlled? (?:regulations?|requirements?|environment|work)",
}

_NEGATION = re.compile(r"\b(?:no|not|without|isn'?t|doesn'?t|does not|won'?t|never)\b[^.;!?]{0,60}$")


def _negated(text: str, start: int) -> bool:
    """True when the text just before a match reads as a negation ('no clearance required')."""
    return _NEGATION.search(text[max(0, start - 70):start]) is not None


def dealbreaker_flags(text: str, profile: dict) -> list[str]:
    """Literal profile dealbreakers plus the built-in requirement-class patterns, negation-guarded."""
    flags = []
    for dealbreaker in profile.get("dealbreakers", []):
        match = re.search(rf"(?<![a-z0-9]){re.escape(dealbreaker.lower())}(?![a-z0-9])", text)
        if match and not _negated(text, match.start()):
            flags.append(f"dealbreaker: {dealbreaker}")
    for name, pattern in DEALBREAKER_PATTERNS.items():
        match = re.search(pattern, text)
        if match and not _negated(text, match.start()):
            flags.append(f"dealbreaker: {name} ('{match.group(0)[:60].strip()}')")
    return flags


_YEARS_REQ = re.compile(r"(\d{1,2})\s*(?:[-–]\s*(\d{1,2})|\s*to\s*(\d{1,2}))?\s*\+?\s*years?[’']?\s*(?:of\s+)?")


def experience_gap_flags(text: str, profile: dict) -> list[str]:
    """Check 'N(+|-M) years of X' requirements against the owner's honest tenure map. Added
    after a Principal PM posting (8-10 yrs project management required vs under 1) rode keyword
    overlap into an alert. Postings inflate years, so: mild shortfall → informational flag only;
    severe shortfall (the owner's years under HALF the minimum, gap ≥ 2 yrs) → dealbreaker-class flag.
    """
    tenure = profile.get("experience_years") or {}
    if not tenure:
        return []
    worst: dict[str, tuple[float, float, bool]] = {}  # category → (required_min, her_years, severe)
    for match in _YEARS_REQ.finditer(text):
        required = float(match.group(1))
        # Tight attribution window: the discipline of a years-requirement lives right after
        # "N years of" (or just before, for "X experience: N+ years"). A wide window let
        # "…programs. Java a plus." clear a PM requirement, and killed on incidental mentions.
        window = text[max(0, match.start() - 40): match.start()] + text[match.end(): match.end() + 60]
        hits = {category: (float(spec.get("years", 0)), bool(spec.get("soft_only")))
                for category, spec in tenure.items()
                if any(_word_hit(window, kw) for kw in spec.get("keywords", []))}
        # If any matched category in this window CLEARS the bar, the requirement is really about
        # that one — "8+ yrs software engineering with LLM exposure" must not kill on ai/agents.
        cleared_nearby = any(hers >= required for hers, _ in hits.values())
        for category, (hers, soft_only) in hits.items():
            if required <= hers:
                continue
            severe = (not cleared_nearby) and not soft_only \
                and hers < required / 2 and (required - hers) >= 2
            prev = worst.get(category)
            if prev is None or required > prev[0] or (severe and not prev[2]):
                worst[category] = (required, hers, severe)
    flags = []
    for category, (required, hers, severe) in sorted(worst.items()):
        prefix = "dealbreaker: requirement gap" if severe else "requirement gap (soft)"
        flags.append(f"{prefix} — posting asks ~{required:g}+ yrs of {category}; profile has {hers:g}")
    return flags


_ONSITE_ANCHOR = re.compile(r"in[- ]?(?:the )?office|on[- ]?site|hybrid|from our [^.\n]{0,40}office")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
_DAY_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def onsite_days_from_text(text: str):
    """Best-effort onsite-days-per-week read from the posting prose: (days, evidence) or None.

    Added after hunt #1 (2026-07-12): job boards' structured ``remote`` flag can contradict the
    posting's own text (a req marked remote:true while requiring Mon/Wed/Fri in Foster City).
    Catches "N days per/a week" (digit or word) and enumerated weekday schedules near an
    office/onsite/hybrid anchor. The posting text always outranks the structured flag.
    """
    best = None
    for anchor in _ONSITE_ANCHOR.finditer(text):
        lo, hi = max(0, anchor.start() - 160), anchor.end() + 160
        window = text[lo:hi]
        days = 0
        match = re.search(r"(\d)\s*\+?\s*days?\s*(?:/|per|a|each)\s*week", window)
        if match:
            days = int(match.group(1))
        else:
            match = re.search(r"(one|two|three|four|five)\s+days?\s*(?:/|per|a|each)\s*week", window)
            if match:
                days = _DAY_WORDS[match.group(1)]
            elif len({d for d in _WEEKDAYS if d in window}) >= 2:
                days = len({d for d in _WEEKDAYS if d in window})
        if days and (best is None or days > best[0]):
            best = (days, re.sub(r"\s+", " ", window).strip()[:100])
    return best


_REMOTE_HINT = re.compile(r"\bremote\b|work from home|work from anywhere|\bwfh\b", re.I)


def classify_remote(job: dict, profile: dict, text: str) -> str:
    """One honest word about where the owner would actually have to be. Added after 'New York, NY'
    headlines left relocation ambiguous — the verdict is structural, not description-text-dependent.

      remote      structured remote flag / remote in the location field, no onsite contradiction
      remote?     remote only *mentioned* in the text — verify before trusting
      commutable  office location inside the profile's commute zone (profile commutable_locations)
      relocation  no remote signal at all and the location is beyond the commute zone
      unknown     no location information to judge
    """
    location = (job.get("location") or "").lower()
    commutable = any(c in location for c in profile.get("commutable_locations", []))
    onsite = onsite_days_from_text(text)
    max_onsite = profile.get("max_onsite_days_per_week")
    onsite_conflict = bool(onsite and max_onsite is not None and onsite[0] > max_onsite)

    structurally_remote = job.get("remote") is True or _word_hit(location, "remote")
    if structurally_remote and not onsite_conflict:
        return "remote"
    if commutable:
        return "commutable"
    if not location and not structurally_remote:
        return "remote?" if _REMOTE_HINT.search(text) else "unknown"
    if onsite_conflict or not structurally_remote:
        # An office schedule at a non-commutable location, or no remote signal anywhere:
        # only the text-mention softens it to a verify-first verdict.
        if not onsite_conflict and _REMOTE_HINT.search(text):
            return "remote?"
        return "relocation"
    return "unknown"


def location_conflict_flags(text: str, job: dict, profile: dict, bands: dict) -> list[str]:
    """Cross-check the posting text against the structured location read; may dock the band."""
    flags = []
    max_onsite = profile.get("max_onsite_days_per_week")
    onsite = onsite_days_from_text(text)
    if onsite and max_onsite is not None and onsite[0] > max_onsite:
        contradicted = "; structured remote flag contradicted by posting text" if job.get("remote") is True else ""
        flags.append(
            f"location: ~{onsite[0]} days/week onsite per posting text ('{onsite[1]}') — "
            f"over the {max_onsite}-day max{contradicted}"
        )
        bands["location"] = min(bands["location"], 3.0)
    if (profile.get("relocation") or "").lower() == "no":
        match = re.search(r"must relocate|relocat(?:e|ion)(?: to [^.;\n]{0,40})? (?:is )?required"
                          r"|willing(?:ness)? to relocate", text)
        if match and not _negated(text, match.start()):
            flags.append(f"location: relocation required ('{match.group(0)[:60].strip()}') — profile relocation: no")
            bands["location"] = min(bands["location"], 3.0)
    return flags


def score_title(job: dict, profile: dict) -> tuple[float, list[str]]:
    title = (job.get("title") or "").lower()
    notes = []
    strong = [k for k in profile.get("title_keywords_strong", []) if _word_hit(title, k)]
    avoid = [k for k in profile.get("title_keywords_avoid", []) if _word_hit(title, k)]
    seniority = [k for k in profile.get("seniority", []) if _word_hit(title, k)]
    points = min(20.0, 10.0 * len(strong))
    if seniority:
        points += 10.0
        notes.append(f"seniority: {', '.join(seniority)}")
    if strong:
        notes.append(f"title hits: {', '.join(strong)}")
    if avoid:
        points = max(0.0, points - 15.0 * len(avoid))
        notes.append(f"title avoid-words: {', '.join(avoid)}")
    return min(30.0, points), notes


def score_skills(job: dict, profile: dict) -> tuple[float, list[str]]:
    text = f"{job.get('title') or ''}\n{job.get('description') or ''}".lower()
    skills: dict = profile.get("skills", {})
    if not skills:
        return 17.5, ["no skills in profile — neutral"]
    total_weight = sum(abs(w) for w in skills.values()) or 1
    hit_weight = 0.0
    hits = []
    for keyword, weight in skills.items():
        if _word_hit(text, keyword):
            hit_weight += weight
            hits.append(keyword)
    points = max(0.0, min(35.0, 35.0 * hit_weight / total_weight))
    return points, ([f"skills: {', '.join(hits)}"] if hits else ["no profile skills found in posting"])


def _annual(amount) -> float | None:
    """Normalize a compensation number to annual USD-ish; hourly rates get annualized."""
    if amount is None:
        return None
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    if amount < 500:  # almost certainly an hourly rate
        return amount * 2080
    if amount < 15000:  # probably monthly
        return amount * 12
    return amount


def score_comp(job: dict, profile: dict) -> tuple[float, list[str]]:
    floor = profile.get("min_total_comp")
    target = profile.get("target_total_comp") or floor
    high = _annual(job.get("comp_max")) or _annual(job.get("comp_min"))
    if not floor or high is None:
        note = "comp unknown — neutral" if high is None else "no comp floor in profile — neutral"
        return 8.0, [note]
    if high >= target:
        return 15.0, [f"comp ~{int(high):,} ≥ target {int(target):,}"]
    if high >= floor:
        span = max(1.0, float(target - floor))
        points = 8.0 + 7.0 * (high - floor) / span
        return round(points, 1), [f"comp ~{int(high):,} between floor and target"]
    return round(max(0.0, 8.0 * high / floor), 1), [f"comp ~{int(high):,} below floor {int(floor):,}"]


def score_location(job: dict, profile: dict) -> tuple[float, list[str]]:
    remote_pref = (profile.get("remote") or "any").lower()
    location = (job.get("location") or "").lower()
    remote = job.get("remote")
    if remote is not True and _word_hit(location, "remote"):
        remote = True
    if remote is True:
        if remote_pref in ("required", "preferred"):
            # Remote-but-region-locked postings only count if the region fits.
            regions = [r.lower() for r in profile.get("remote_regions_ok", [])]
            if regions and location and not any(_word_hit(location, r) for r in regions) and \
               any(_word_hit(location, bad) for bad in ("europe", "emea", "apac", "uk only", "canada only")):
                return 3.0, [f"remote but region-locked: {job.get('location')}"]
            return 15.0, ["remote"]
        return 12.0, ["remote (no strong preference)"]
    for ok in profile.get("locations_ok", []):
        if location and _word_hit(location, ok.lower()):
            return 12.0, [f"location ok: {job.get('location')}"]
    if remote_pref == "required":
        return (0.0, [f"not remote: {job.get('location') or 'unknown location'}"]) if location else (7.0, ["location unknown"])
    return (4.0, [f"location mismatch: {job.get('location')}"]) if location else (7.0, ["location unknown"])


def score_recency(job: dict) -> tuple[float, list[str]]:
    posted = job.get("posted_at") or ""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(posted))
    if not match:
        return 2.0, ["post date unknown"]
    try:
        posted_epoch = time.mktime(time.strptime(match.group(0), "%Y-%m-%d"))
    except (ValueError, OverflowError):
        return 2.0, ["post date unparseable"]
    age_days = max(0.0, (time.time() - posted_epoch) / 86400)
    if age_days <= 7:
        return 5.0, [f"posted {int(age_days)}d ago"]
    if age_days <= 30:
        return 3.0, [f"posted {int(age_days)}d ago"]
    return 1.0, [f"posted {int(age_days)}d ago"]


def score_job(job: dict, profile: dict) -> dict:
    text = f"{job.get('title') or ''}\n{job.get('description') or ''}".lower()
    bands = {}
    notes: list[str] = []
    for band, (points, band_notes) in (
        ("title", score_title(job, profile)),
        ("skills", score_skills(job, profile)),
        ("comp", score_comp(job, profile)),
        ("location", score_location(job, profile)),
        ("recency", score_recency(job)),
    ):
        bands[band] = round(points, 1)
        notes.extend(band_notes)

    flags = dealbreaker_flags(text, profile)
    flags += location_conflict_flags(text, job, profile, bands)  # may dock the location band
    flags += experience_gap_flags(text, profile)
    remote_verdict = classify_remote(job, profile, text)
    if remote_verdict == "relocation" and profile.get("commutable_locations"):
        flags.append(
            f"dealbreaker: relocation implied — '{job.get('location')}' is beyond the "
            f"~{profile.get('max_commute_minutes_one_way', 90)}-min commute zone with no credible "
            "remote signal (none present, or the remote flag is contradicted by an onsite "
            "schedule); relocation is a hard no")
        bands["location"] = min(bands["location"], 1.0)
    total = sum(bands.values())
    if any(flag.startswith("dealbreaker") for flag in flags):
        total = min(total, 25.0)
    for keyword, boost in (profile.get("preferences") or {}).items():
        if _word_hit(text, keyword):
            total += float(boost)
            notes.append(f"preference '{keyword}': {boost:+g}")
    total = max(0.0, min(100.0, total))

    scored = dict(job)
    scored.update({
        "match_percent": round(total, 1),
        "score_breakdown": bands,
        "score_notes": notes,
        "flags": flags,
        "remote_verdict": remote_verdict,
    })
    return scored


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score jobs against the profile (Proteus phase 2).")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--jobs", required=True, help="output of fetch_jobs.py")
    parser.add_argument("--out", required=True)
    parser.add_argument("--top", type=int, default=15, help="keep the N best (default 15; 0 = all)")
    parser.add_argument("--min-score", type=float, default=0.0)
    args = parser.parse_args(argv)

    with open(args.profile, encoding="utf-8") as fh:
        profile = json.load(fh)
    with open(args.jobs, encoding="utf-8") as fh:
        jobs_doc = json.load(fh)

    scored = [score_job(job, profile) for job in jobs_doc.get("jobs", [])]
    scored.sort(key=lambda j: j["match_percent"], reverse=True)
    if args.min_score:
        scored = [j for j in scored if j["match_percent"] >= args.min_score]
    kept = scored[: args.top] if args.top else scored

    document = {
        "scored_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "profile": os.path.abspath(args.profile),
        "considered": len(scored),
        "kept": len(kept),
        "jobs": kept,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=1)
    summary = [{"title": j["title"], "company": j["company"], "match": j["match_percent"]} for j in kept[:5]]
    print(json.dumps({"ok": True, "considered": len(scored), "kept": len(kept), "top": summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
