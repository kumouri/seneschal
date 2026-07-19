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
  recency    0-5    a small freshness bonus *among live postings* — it deliberately cannot
                    sink an old one (see age decay below)
A dealbreaker keyword caps the total at 25 and flags the job.

Post-band adjustments nudge the total before decay: `preferences` keyword boosts (SATURATED —
positives flatten toward a +10 cap, skills-map words excluded as already counted; see
`preference_boost`) and a small west-is-better timezone tilt for remote roles (`timezone_tilt` —
never a penalty).

Then **age decay multiplies the total** (`age_decay`, applied last — after preference boosts,
so a dead posting's boosts can't rescue it). A filled role is worth nothing however well it
matches, and an additive band mathematically can't express that: the recency band floors at 1
point, so a 5-year-old posting used to lose 4 points out of 100 and still clear the 60% alert
bar. Decay is flat for a 30-day grace period, then halves every 120 days, and tags each job
with `age_days` + `age_tier` (fresh/recent/stale/old/ancient). Consumers gate their
score-bypassing paths on the tier: hunt_cycle's express lane refuses `old`/`ancient`.

Run:  python score_jobs.py --profile profile.json --jobs out/jobs.json --out out/scored.json [--top 15]
"""

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone

from proteus_comp import comp_from_text


def _word_hit(text: str, keyword: str) -> bool:
    """Case-insensitive whole-word/phrase match (word boundaries on both ends)."""
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])", text) is not None


_NEGATION = re.compile(r"\b(?:no|not|without|isn'?t|doesn'?t|does not|won'?t|never)\b[^.;!?]{0,60}$")


def _negated(text: str, start: int) -> bool:
    """True when the text just before a match reads as a negation ('no clearance required')."""
    return _NEGATION.search(text[max(0, start - 70):start]) is not None


def dealbreaker_flags(text: str, profile: dict) -> list[str]:
    """Literal profile dealbreakers (negation-guarded). Clearance/citizenship are handled separately
    by clearance_flags — see it for the nuance (a profile marked citizen + clearance-eligible treats
    'able to obtain a clearance' and 'US citizenship required' as targets/fine, NOT blockers; only
    an ACTIVE clearance the owner doesn't yet hold is a day-1 blocker)."""
    flags = []
    for dealbreaker in profile.get("dealbreakers", []):
        match = re.search(rf"(?<![a-z0-9]){re.escape(dealbreaker.lower())}(?![a-z0-9])", text)
        if match and not _negated(text, match.start()):
            flags.append(f"dealbreaker: {dealbreaker}")
    return flags


# --- Security clearance / citizenship ---------------------------------------------------------------
# Profile-driven, not blanket. When the profile marks the owner a citizen and clearance-ELIGIBLE, the
# old blanket "any clearance/citizenship mention = dealbreaker" is wrong — it buries defense-adjacent
# roles the owner may actively want. Rule: citizenship-required is FINE (the owner qualifies);
# "able to obtain a clearance" is a TARGET; only an ACTIVE clearance the owner doesn't yet hold blocks
# a day-1 start.
_CLEARANCE_MENTION = re.compile(r"clearance|ts/?sci|top[\s-]?secret|\bsci\b|security investigation|polygraph", re.I)
_CLEARANCE_OBTAINABLE = re.compile(
    r"(?:ability|able|eligible|eligibility|willing(?:ness)?|must be able)[^.]{0,25}"
    r"(?:to (?:obtain|get)|for)[^.]{0,40}clearance"
    r"|clearance[\s-]?eligib\w+|clearable|(?:will|can|ability to) sponsor[^.]{0,30}clearance"
    r"|able to obtain (?:a |an )?(?:ts/?sci|security clearance|clearance|secret)"
    r"|obtain (?:and maintain )?(?:a |an )?(?:ts/?sci|security )?clearance", re.I)
_CLEARANCE_ACTIVE = re.compile(
    r"(?:active|current(?:ly)?|existing)[^.]{0,25}(?:ts/?sci|top[\s-]?secret|secret|security|dod|government)?[^.]{0,15}clearance"
    r"|must (?:currently )?(?:have|hold|possess|maintain|already have)[^.]{0,30}clearance"
    r"|(?:requires?|require) (?:an? )?active[^.]{0,20}clearance"
    r"|(?:ts/?sci|top[\s-]?secret)[^.]{0,15}(?:required|clearance required)", re.I)


def clearance_flags(text: str, profile: dict) -> list[str]:
    """One flag about a clearance/citizenship requirement, given the profile's eligibility.

    clearance_eligible + us_citizen (profile) → citizenship is a qualifier the owner meets (no flag);
    'able to obtain a clearance' → a positive note (target); an ACTIVE clearance the owner doesn't
    hold → dealbreaker (can't start day-1, though a sponsor could change that); a bare/ambiguous
    clearance mention → soft flag to verify. Not clearance_eligible → any clearance requirement is a
    dealbreaker."""
    if not _CLEARANCE_MENTION.search(text):
        return []
    m = _CLEARANCE_MENTION.search(text)
    if _negated(text, m.start()):
        return []
    if not profile.get("clearance_eligible"):
        return ["dealbreaker: security clearance required — not clearance-eligible"]
    obtainable = _CLEARANCE_OBTAINABLE.search(text)
    active = _CLEARANCE_ACTIVE.search(text)
    if active and not obtainable:
        return [f"dealbreaker: ACTIVE clearance required ('{active.group(0)[:50].strip()}') — "
                "you don't hold one yet (you're clearable, but not a day-1 start unless they sponsor)"]
    if obtainable:
        return ["clearance: obtainable — US citizen & clearance-eligible; this is a target, not a blocker"]
    return ["clearance: mentioned (verify active-vs-obtainable) — you're clearance-eligible, so likely fine"]


_YEARS_REQ = re.compile(r"(\d{1,2})\s*(?:[-–]\s*(\d{1,2})|\s*to\s*(\d{1,2}))?\s*\+?\s*years?[’']?\s*(?:of\s+)?")


# --- avoid_companies / avoid_patterns: the profile's written rules, wired up ------------------------
# `avoid_companies`, `avoid_patterns` and `post_employment_constraints` used to sit in profile.json
# referenced by NOTHING — so a named avoid-company scored like any other employer, and a consultancy
# competing with the owner's current employer could reach the top of the board with no flag at all
# despite the profile saying, verbatim, to flag exactly that shape as legal risk. Catching those by
# hand-reading the profile is not a system.
#
# The two rules are deliberately different strengths, because the profile writes them that way:
#   avoid_companies -> dealbreaker (the profile said avoid)
#   avoid_patterns  -> flag, never a drop (the profile says "don't auto-drop, FLAG for the owner's call")

# Tuned against a live multi-thousand-posting board, because the obvious signals are all wrong.
# "our clients" fires on every B2B company that has customers; "professional services" appears
# verbatim in ordinary product-company boilerplate. Those two alone flagged hundreds of postings —
# including a product company's Forward-Deployed Engineer req, a top target title. A legal-risk flag
# that cries wolf on every FDE role is worse than none: the owner learns to scroll past the one that
# matters.
#
# What actually separates a consultancy from a product company with an FDE team is EMBEDDED DELIVERY —
# selling labour into someone else's engineering org.
_CONSULTANCY_SIGNALS = (
    "alongside our clients", "clients' engineering", "embedded with clients", "embed with clients",
    "client engagements", "our consultants", "consultancy", "consulting firm",
    "staff augmentation", "staffing firm", "staffing agency", "on behalf of clients",
)


def avoid_company_flags(job: dict, profile: dict) -> list[str]:
    """The profile's `avoid_companies` list, as a dealbreaker. Matches the company field, not prose —
    a JD that mentions a competitor isn't a job at one."""
    company = (job.get("company") or "").lower()
    if not company:
        return []
    hits = [c for c in profile.get("avoid_companies", []) if _word_hit(company, c.lower())]
    return [f"dealbreaker: {hits[0]} is on the avoid_companies list"] if hits else []


def legal_risk_flags(job: dict, profile: dict, text: str) -> list[str]:
    """Flag a posting that reads like a consultancy competing with the owner's employer —
    non-compete exposure.

    A FLAG, never a drop: the profile's rule is explicit that these are the owner's call, and the
    score is left untouched. The scorer can't read a contract or judge a jurisdiction; it can only
    notice the shape and hand the owner the question with the profile's own clause attached.
    """
    if not profile.get("avoid_patterns"):
        return []
    hits = [s for s in _CONSULTANCY_SIGNALS if s in text]
    if not hits:
        return []
    constraints = profile.get("post_employment_constraints") or {}
    employer = constraints.get("employer") or "the owner's current employer"
    term = constraints.get("non_compete") or "a post-employment non-compete"
    return [f"legal-risk: reads like a consultancy/staffing firm (matched \"{hits[0]}\") — the profile "
            f"flags competitors of {employer} as non-compete exposure ({str(term)[:90]}...). "
            f"NOT auto-dropped: the profile's rule is that this is the owner's call. Verify the client "
            f"base and get the clause read before applying."]


def title_avoid_flags(job: dict, profile: dict) -> list[str]:
    """Avoid-words in the TITLE are a dealbreaker, not a deduction.

    `score_title` docks 15 points per avoid-word, which a strong skills match plus preference boosts
    simply out-earns: an avoid-listed role once sat high on the board because its skills overlap
    out-voted the deduction. The owner does not want an avoid-listed role at any score.

    This matches what the express lane (`strong_target_title`) has always done — an avoid-word there
    disqualifies outright, even on an exact target-title match — so the two paths now agree.

    A `dealbreaker:` flag caps the total at 25 rather than zeroing it: the role stays visible and
    explains itself instead of vanishing, which is the same "surface, never silently filter" rule
    the comp bands follow. Matches the title only — a JD that merely *mentions* frontend work isn't
    a frontend job.
    """
    title = (job.get("title") or "").lower()
    hits = [k for k in profile.get("title_keywords_avoid", []) if _word_hit(title, k)]
    if not hits:
        return []
    return [f"dealbreaker: title avoid-word — '{', '.join(hits)}' in \"{job.get('title')}\"; "
            f"on the title_keywords_avoid list"]


def experience_gap_flags(text: str, profile: dict) -> list[str]:
    """Check 'N(+|-M) years of X' requirements against the owner's honest tenure map. Added after a
    Principal PM posting (8-10 yrs project management required vs under 1) rode keyword overlap into
    an alert. Postings inflate years, so: mild shortfall → informational flag only; severe shortfall
    (the owner's years under HALF the minimum, gap ≥ 2 yrs) → dealbreaker-class flag.
    """
    tenure = profile.get("experience_years") or {}
    if not tenure:
        return []
    worst: dict[str, tuple[float, float, bool]] = {}  # category → (required_min, owner_years, severe)
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
        cleared_nearby = any(owned >= required for owned, _ in hits.values())
        for category, (owned, soft_only) in hits.items():
            if required <= owned:
                continue
            severe = (not cleared_nearby) and not soft_only \
                and owned < required / 2 and (required - owned) >= 2
            prev = worst.get(category)
            if prev is None or required > prev[0] or (severe and not prev[2]):
                worst[category] = (required, owned, severe)
    flags = []
    for category, (required, owned, severe) in sorted(worst.items()):
        prefix = "dealbreaker: requirement gap" if severe else "requirement gap (soft)"
        flags.append(f"{prefix} — posting asks ~{required:g}+ yrs of {category}; profile has {owned:g}")
    return flags


_ONSITE_ANCHOR = re.compile(r"in[- ]?(?:the )?office|on[- ]?site|hybrid|from our [^.\n]{0,40}office")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
_DAY_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def onsite_days_from_text(text: str):
    """Best-effort onsite-days-per-week read from the posting prose: (days, evidence) or None.

    Added after hunt #1: job boards' structured ``remote`` flag can contradict the posting's own text
    (a req marked remote:true while requiring Mon/Wed/Fri in a named office). Catches "N days per/a
    week" (digit or word) and enumerated weekday schedules near an office/onsite/hybrid anchor. The
    posting text always outranks the structured flag.
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
# A LOAD-BEARING remote declaration — the role itself is remote — as opposed to the word "remote"
# drifting through boilerplate ("flexibility in terms of remote work", "a remote and distributed
# team", an EEO/benefits blurb), which appears in nearly every posting. Only the former may rescue a
# role at a named, non-commutable office to a soft "remote?"; the latter must not. A bare named-city
# req once rode an incidental "remote work" benefits line to a high score and a workup though it was
# 4-days-onsite per its own screening question.
_STRONG_REMOTE = re.compile(
    r"fully[\s-]?remote|remote[\s-]?first|all[\s-]?remote|100%\s*remote"
    r"|work from (?:home|anywhere)|\bwfh\b"
    r"|remote[\s-]?(?:position|role|job|opportunity|eligible)"
    r"|(?:roles?|positions?|jobs?)\s+(?:is|are|can be|may be|will be)\s+(?:fully\s+)?remote"
    r"|open to (?:fully\s+)?remote"
    r"|remote\s*[-,]?\s*(?:us|usa|united states|anywhere|within|across)\b",
    re.I)
_ONSITE_TEXT = re.compile(r"\bon[\s-]?site\b|\bin[\s-]?office\b|\bin office\b", re.I)
# "City, ST" (US), incl. hyphenated/multi-word city names — for postings with no structured location
# field (HN "who's hiring", some aggregators) that bury the location in the title/description text.
_CITY_STATE = re.compile(r"\b([A-Z][A-Za-z.]+(?:[ \-][A-Z][A-Za-z.]+){0,2}),\s*([A-Z]{2})\b")


def effective_locations(job: dict) -> list[str]:
    """Locations to judge against — the structured field PLUS 'City, ST' tokens pulled from the
    title + head of the description. Added after an ONSITE big-city HN post (no structured location)
    alerted: the location was in the title text, invisible to the old field-only read."""
    locs = []
    if job.get("location"):
        locs.append(job["location"])
    head = f"{job.get('title') or ''}\n{(job.get('description') or '')[:500]}"
    for m in _CITY_STATE.finditer(head):
        locs.append(f"{m.group(1)}, {m.group(2)}")
    return locs


def _explicit_onsite(text: str) -> bool:
    m = _ONSITE_TEXT.search(text)
    return bool(m) and not _negated(text, m.start())


# --- "Remote" as a claim vs "remote" as a fact ------------------------------------------------------
# Boards reward the word, so postings wear it. A posting can state remote=False AND
# workplace_type=Hybrid in its own ATS fields, then write its location as "San Francisco, Remote" —
# and the old read handed it a full 15/15 remote score over the posting's own contradiction. When
# relocation is the profile's one hard no, a posting that fudges remote doesn't waste time at the
# margins — it wastes it on roles that were never possible.
#
# The rule: the ATS's own structured fields BEAT the word appearing in a comma-joined location string.
_ONSITE_WORKPLACE_TYPES = ("hybrid", "onsite", "on-site", "on site", "in office", "in-office")
# Greenhouse bakes the workplace type INTO the location string ("Hybrid - San Francisco, New York
# City", "On-site - Austin") instead of exposing a separate `workplace_type` field the way Ashby does.
# So a hybrid/onsite keyword in the LOCATION is just as authoritative as the dropdown — and it was
# invisible to the old read, which consulted only `workplace_type` + the description prose. A
# "Hybrid - San Francisco, New York City" req (remote and workplace_type both null) once scored high
# and got worked up before the hybrid-at-source truth surfaced.
_LOCATION_ONSITE = re.compile(r"\b(hybrid|on[\s-]?site|in[\s-]?office)\b", re.I)


def structured_onsite(job: dict) -> str | None:
    """The posting's own structured onsite signal — Ashby's `workplace_type` dropdown, OR a workplace-
    type keyword Greenhouse bakes into the `location` string ("Hybrid - San Francisco"). Authoritative
    either way: the employer stated it in structured data, not prose we're interpreting."""
    wt = (job.get("workplace_type") or "").strip().lower()
    if wt in _ONSITE_WORKPLACE_TYPES:
        return wt
    loc = job.get("location") or ""
    m = _LOCATION_ONSITE.search(loc)
    # A location keyword ("Hybrid - SF") is authoritative onsite ONLY when the location doesn't also
    # offer remote as an alternative ("Hybrid or Remote", "Remote / Hybrid") — there the employer is
    # naming a real remote option, not a place the owner would have to be.
    if m and not re.search(r"\bremote\b", loc, re.I):
        return m.group(1).lower().replace(" ", "-")   # "hybrid" / "on-site" / "in-office"
    return None


def _location_claims_remote(job: dict) -> bool:
    """'remote' appears in the location field — which on a multi-location posting ("San Francisco,
    Remote") means *one of the options*, not the job."""
    return _word_hit((job.get("location") or "").lower(), "remote")


def remote_contradiction(job: dict) -> str | None:
    """A one-line indictment when a posting's own fields contradict its 'Remote' billing, else None.

    Not a judgement about whether the job is good — a report that its structured data and its
    marketing disagree, so the owner can see who is doing it.
    """
    if not _location_claims_remote(job):
        return None
    onsite_type = structured_onsite(job)
    explicit_not_remote = job.get("remote") is False
    if not onsite_type and not explicit_not_remote:
        return None
    says = []
    if explicit_not_remote:
        says.append("remote=false")
    if onsite_type:
        says.append(f"workplace_type={onsite_type}")
    return (f"misleading: the posting bills itself '{job.get('location')}' while its own ATS fields say "
            f"{' and '.join(says)}. The board data contradicts the listing — treat 'Remote' here as a "
            f"claim, not a fact, and verify at source before spending anything on it.")


def classify_remote(job: dict, profile: dict, text: str) -> str:
    """One honest word about where the owner would actually have to be — read from the structured
    field AND the posting text (HN/aggregator posts bury 'San Francisco, CA | ONSITE' in the title).

      remote      structured remote flag / 'remote' in a location token, no onsite contradiction
      remote?     remote only *mentioned* in the text — verify before trusting
      commutable  an office inside the profile's commute zone (profile commutable_locations)
      relocation  explicitly onsite / too many onsite days / a real non-commutable city, no clean remote
      unknown     no location information at all to judge
    """
    commutable_list = profile.get("commutable_locations", [])
    locs = [loc.lower() for loc in effective_locations(job)]
    commutable = any(c in loc for loc in locs for c in commutable_list)
    has_location = bool(locs)

    onsite = onsite_days_from_text(text)
    max_onsite = profile.get("max_onsite_days_per_week")
    onsite_days_conflict = bool(onsite and max_onsite is not None and onsite[0] > max_onsite)
    # A structured workplace_type of Hybrid/Onsite (Ashby et al.) is an authoritative onsite signal —
    # it means some days in the named office, so a non-commutable one is relocation.
    wt_onsite = bool(structured_onsite(job))
    explicit_onsite = _explicit_onsite(text) or wt_onsite

    # Only a hard DAYS conflict ("3 days/week onsite") overrides a real remote flag — a stray
    # "in-office"/"onsite" word must not nuke a genuinely-remote posting.
    #
    # ...but the posting's OWN structured fields are not a stray word. This used to short-circuit to
    # "remote" before wt_onsite was ever consulted, so `remote=False` + `workplace_type=Hybrid` +
    # location "San Francisco, Remote" returned "remote" — the exact trick. A structured onsite type,
    # or an explicit remote=false, now outranks the word: the location is *claiming* remote (one
    # option among several), the fields are *stating* otherwise. Downgrade to "remote?" — verify,
    # don't assume — rather than "relocation", since a genuine remote option may well exist.
    contradicted = bool(remote_contradiction(job))
    structurally_remote = job.get("remote") is True or any("remote" in loc for loc in locs)
    if structurally_remote and not onsite_days_conflict and not contradicted:
        return "remote"
    if contradicted and not onsite_days_conflict:
        if commutable:
            return "commutable"
        # The "Remote"-in-the-location + structured Hybrid/Onsite type at a non-commutable place is
        # the trick this branch exists for: believe the employer's own dropdown, and don't reward the
        # misdirection with a SOFTER verdict than an honest hybrid there would get — it's relocation.
        return "relocation" if wt_onsite else "remote?"
    if commutable:                         # an office in the commute zone (the days cap is flagged separately)
        return "commutable"
    if onsite_days_conflict:               # too many onsite days, and NOT in the commute zone
        return "relocation"
    if explicit_onsite and has_location:   # explicitly ONSITE at a named non-commutable place
        return "relocation"
    if has_location:                       # a named, non-commutable city, and nothing structured says
        # remote — only a load-bearing remote declaration (not incidental "remote" boilerplate)
        # rescues it to "verify"; otherwise it's onsite there, i.e. relocation.
        return "remote?" if _STRONG_REMOTE.search(text) else "relocation"
    return "remote?" if _REMOTE_HINT.search(text) else "unknown"


# US work-eligibility: a role can be "remote" yet region-locked to a foreign country the owner can't
# work from (Remote-India, Remote-Germany, London…). For a US-based owner who can't relocate, a
# foreign-ONLY role is not takeable. Worldwide/Anywhere/US/ambiguous stays fine.
_US_STATE_ABBR = ("al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia","ks",
    "ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj","nm","ny","nc","nd","oh",
    "ok","or","pa","ri","sc","sd","tn","tx","ut","vt","va","wa","wv","wi","wy","dc")
_US_POSITIVE = re.compile(
    r"\bunited states\b|\bu\.?s\.?a\.?\b|\bu\.?s\.?\b|\bamericas\b|north america|\bworldwide\b"
    r"|\banywhere\b|\bglobal(?:ly)?\b|\bnationwide\b|remote[\s,\-]*(?:us\b|u\.s\.|united states|usa)", re.I)
_FOREIGN_MARK = re.compile(
    r"\b(?:india|canada|united kingdom|\buk\b|england|scotland|germany|deutschland|france|spain|italy"
    r"|netherlands|ireland|poland|romania|portugal|sweden|norway|denmark|finland|switzerland|austria"
    r"|belgium|czech|singapore|japan|south korea|korea|china|hong kong|taiwan|australia|new zealand"
    r"|israel|uae|united arab emirates|saudi|brazil|mexico|argentina|colombia|chile|philippines"
    r"|vietnam|thailand|indonesia|malaysia|nigeria|kenya|south africa|egypt|turkey|ukraine|emea|apac"
    # "europe"/"eu" were missing at first: the list knew EMEA and every individual European country
    # but not the continent, so a role located plainly "Europe" passed as US-eligible.
    r"|latam|\bemea\b|europe|\beu\b)\b"
    r"|\b(?:london|manchester|berlin|munich|m[uü]nchen|hamburg|frankfurt|w[uü]rzburg|amsterdam|paris|madrid"
    r"|barcelona|dublin|lisbon|warsaw|krak[oó]w|stockholm|oslo|copenhagen|helsinki|zurich|vienna"
    r"|tokyo|osaka|seoul|beijing|shanghai|shenzhen|taipei|sydney|melbourne|auckland|tel aviv|dubai"
    r"|bangalore|bengaluru|hyderabad|mumbai|delhi|pune|chennai|gurgaon|noida|toronto|vancouver"
    r"|montreal|ottawa|s[aã]o paulo|mexico city|bogot[aá]|manila|jakarta|bangkok|ho chi minh"
    # German cities the list was missing — a bare 'Heidelberg' (no country) is what let a
    # German-language posting alert as US-eligible. US namesakes keep their uppercase "City, ST"
    # short-circuit above, so these only bite a bare/lowercase foreign location.
    r"|heidelberg|karlsruhe|stuttgart|cologne|k[oö]ln|d[uü]sseldorf|leipzig|hannover|dresden"
    r"|nuremberg|n[uü]rnberg|bremen|essen|dortmund|bonn|mannheim|freiburg|m[uü]nster|aachen"
    r"|wiesbaden|bielefeld|duisburg|bochum|wuppertal|augsburg|braunschweig|regensburg|darmstadt"
    r"|kassel|l[uü]beck|potsdam|jena|erfurt|rostock|ingolstadt|heilbronn|osnabr[uü]ck|paderborn"
    # DACH + the rest of the EU (unambiguous spellings only — no Cambridge/Birmingham/Rome/Naples,
    # which are US cities too)
    r"|basel|bern|geneva|genf|lausanne|lucerne|graz|salzburg|linz|innsbruck"
    r"|rotterdam|utrecht|eindhoven|the hague|antwerp|ghent|brussels|gothenburg|malm[oö]|aarhus"
    r"|bergen|trondheim|tampere|espoo|gda[nń]sk|wroc[lł]aw|pozna[nń]|brno|bratislava|budapest"
    r"|prague|praha|sofia|bucharest|zagreb|ljubljana|tallinn|riga|vilnius|reykjavik|luxembourg"
    r"|edinburgh|glasgow|belfast|leeds|bela[rz]us)\b", re.I)


# A posting written in another language is not a US role, whatever its location field says.
# This is the layer that doesn't depend on a curated city list: one posting had
# location='Heidelberg' (a bare city, no country) and an entirely German description, so every
# location-based rule waved it through and it alerted as a strong match.
# Matching on *distinct* common-word hits keeps English safe — "die" alone appears in English
# ("die casting"), but six different German function words together do not.
_GERMAN_WORDS = re.compile(
    r"\b(?:und|oder|der|die|das|den|dem|des|für|mit|bei|von|zum|zur|wir|du|dich|dein|deine|uns"
    r"|unser|eine|einen|einem|einer|ist|sind|wird|werden|nicht|auch|sowie|unternehmen|erfahrung"
    r"|kenntnisse|aufgaben|bewerbung|mitarbeiter|stelle|arbeit|team|jahre|gute|sehr|mehr)\b", re.I)
_FRENCH_WORDS = re.compile(
    r"\b(?:et|ou|le|la|les|des|une|un|du|au|aux|pour|avec|dans|nous|vous|votre|notre|est|sont"
    r"|sera|être|vos|nos|ainsi|entreprise|exp[ée]rience|comp[ée]tences|poste|travail|[ée]quipe)\b", re.I)
_SPANISH_WORDS = re.compile(
    r"\b(?:y|o|el|la|los|las|una|uno|del|al|para|con|en|nosotros|nuestro|su|es|son|ser[áa]"
    r"|empresa|experiencia|conocimientos|puesto|trabajo|equipo|a[ñn]os)\b", re.I)
# DACH job ads legally carry a gender notation — (m/w/d), (m/w/x), (w/m/d), (gn), (all genders).
# Extremely high precision: no US posting uses it.
_DACH_GENDER = re.compile(r"\(\s*[mwgfd]\s*/\s*[mwgfdx]\s*(?:/\s*[mwgfdx]\s*)?\)|\(\s*gn\s*\)|\(all genders\)", re.I)

_LANG_MIN_HITS = 6  # distinct function words before we call it non-English


def posting_language_foreign(job: dict) -> str | None:
    """Name the non-English language a posting is written in, or None.

    Only the title + the head of the description are read — enough to classify, and it keeps a
    stray foreign phrase deep in a boilerplate footer from tripping it.
    """
    text = f"{job.get('title') or ''}\n{(job.get('description') or '')[:1500]}"
    if _DACH_GENDER.search(job.get("title") or ""):
        return "German"
    for name, rx in (("German", _GERMAN_WORDS), ("French", _FRENCH_WORDS), ("Spanish", _SPANISH_WORDS)):
        if len({m.group(0).lower() for m in rx.finditer(text)}) >= _LANG_MIN_HITS:
            return name
    return None


# ISO-3166 codes for places the owner can't work. Several collide with US state abbreviations
# (ca=Canada/California, in=India/Indiana, il=Israel/Illinois, ma=Morocco/Massachusetts,
# de=Germany/Delaware), so these are only consulted under the guards in us_work_eligible.
_FOREIGN_CC = frozenset("""
 de fr nl be es it pt pl cz sk hu ro bg gr se no dk fi ie ch at uk gb lu is ee lv lt si hr rs
 in cn jp kr sg hk tw au nz br mx ar cl co pe za ng ke eg tr ua ru il ae sa ph vn th id my ca
""".split())


def _trailing_code(loc: str) -> str | None:
    """The final ``, xx`` component of a location, if it is two letters."""
    m = re.search(r",\s*([A-Za-z]{2})\s*$", loc.strip())
    return m.group(1) if m else None


# HN "Who is hiring" posts carry the location as a pipe-delimited field in the TITLE, not as a
# structured field:
#     "Acme | Forward Deployed Engineer - backend - python | REMOTE (Europe) | Full time | Funded"
# Every region check in this file read job['location'] — which is None for those posts — so a
# Europe-only role passed as US-eligible AND took a full 15/15 remote score. It reached the board
# with no flag, and with an exact target title it was one `remote` verdict away from the express
# lane pushing an alert.
#
# Same shape as the misleading-remote trick: the truth is in the text, the structured fields mislead.
_REMOTE_REGION = re.compile(
    r"\bremote\b\s*(?:[\(\[\-–—:,]|\bin\b|\bfrom\b|\bonly\b)+\s*([A-Za-z][A-Za-z .]{1,22})", re.I)


def remote_region_claim(job: dict) -> str | None:
    """The region a "REMOTE (X)" billing names — from the location field, else the title.

    Returns the raw region text ("Europe", "US", "North America"), or None when the posting doesn't
    bill itself remote-with-a-region.

    **Title, never the description body.** A JD that mentions "our European customers" is not a region
    lock; scanning prose for continent names would flag half the board — the same over-firing that made
    the first cut of `legal_risk_flags` useless.
    """
    for text in (job.get("location") or "", job.get("title") or ""):
        match = _REMOTE_REGION.search(text)
        if match:
            return match.group(1).strip(" .-")
    return None


def us_work_eligible(job: dict) -> bool:
    """False only when the posting clearly belongs to a foreign place with no US option."""
    loc = (job.get("location") or "")
    if posting_language_foreign(job):
        return False                                  # written in another language → not a US role
    if not loc.strip():
        # No structured location — but an HN-style title may still name the region. Only a REMOTE(X)
        # billing counts, and only when X is positively foreign: silence still means benefit of the
        # doubt, so a genuinely-unlocated US post is unaffected.
        claim = remote_region_claim(job)
        if claim and not _US_POSITIVE.search(claim) and _FOREIGN_MARK.search(claim.lower()):
            return False
        return True                                   # no location → assume US-remote (benefit of the doubt)
    if _US_POSITIVE.search(loc):
        return True                                   # US / worldwide / anywhere present (checked first so a
        #                                               multi-site "London; …; Remote-Friendly, United States"
        #                                               still counts as reachable)
    # "City, ST" reads as a US state — but a trailing *lowercase* two-letter code is usually an ISO
    # country ("Munich, de", "bengaluru, in", "Tel Aviv, il"), and several collide with US states
    # (de=Delaware, in=Indiana, il=Illinois, ca=California…). When it's lowercase, consult the
    # foreign list first. This can only ever *reject* on a positive foreign hit, so a feed emitting
    # lowercase US locations ("boston, ma") still passes — and uppercase US states keep their
    # short-circuit, which is what protects Manchester NH / Vienna VA / Berlin CT from their
    # foreign namesakes.
    code = _trailing_code(loc)
    lowercase_code = bool(code) and code.islower()
    if not lowercase_code and re.search(r",\s*(" + "|".join(_US_STATE_ABBR) + r")\b", loc, re.I):
        return True
    # "City, REGION, cc" — three or more parts ending in a lowercase foreign ISO code is a
    # country, not a state ("Renningen, BW, de" is in Germany, not Delaware). US feeds
    # write "City, ST" (two parts) or append US/USA, which _US_POSITIVE already caught above —
    # so requiring 3+ parts keeps "boston, ma" (Massachusetts, not Morocco) eligible.
    if lowercase_code and code in _FOREIGN_CC and len(loc.split(",")) >= 3:
        return False
    if _FOREIGN_MARK.search(loc):
        return False
    if lowercase_code and re.search(r",\s*(" + "|".join(_US_STATE_ABBR) + r")\b", loc, re.I):
        return True                                   # lowercase but no foreign marker → still US
    return True                                       # nothing foreign found → assume US


def us_eligibility_flags(job: dict, profile: dict) -> list[str]:
    if not profile.get("us_citizen") or us_work_eligible(job):
        return []
    lang = posting_language_foreign(job)
    if lang:
        return [f"dealbreaker: not US work-eligible — posting is written in {lang} "
                f"(location says '{job.get('location')}'); you're US-based and can't relocate"]
    # Name the region from wherever we actually found it. Printing job['location'] read
    # "region-locked to 'None'" on exactly the postings this is for — HN posts have no location field,
    # which is why they slipped through in the first place.
    where = job.get("location") or remote_region_claim(job) or "an unstated non-US region"
    return [f"dealbreaker: not US work-eligible — role is region-locked to '{where}' "
            "(you're US-based and can't relocate)"]


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


_TITLE_STOP = {"senior", "sr", "staff", "principal", "lead", "member", "of", "technical", "the",
               "ii", "iii", "iv", "i", "and", "-", "|", "(", ")", ",", ":"}


def _title_tokens(title: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9+]+", title.lower()) if t and t not in _TITLE_STOP}


def title_match_fraction(job_title: str, target: str) -> float:
    """Fraction of a target title's meaningful words present in the job title (0..1)."""
    tgt_tokens = _title_tokens(target)
    if not tgt_tokens:
        return 0.0
    job_tokens = _title_tokens(job_title)
    return len(tgt_tokens & job_tokens) / len(tgt_tokens)


def best_target_title(title: str, profile: dict) -> tuple[float, str | None, int]:
    """(best fraction, matched target, its priority rank) of a target title inside the job title."""
    best_frac, best_target, best_rank = 0.0, None, 0
    targets = profile.get("titles", [])
    for rank, target in enumerate(targets):
        frac = title_match_fraction(title, target)
        if frac > best_frac or (frac == best_frac and frac > 0 and rank < best_rank):
            best_frac, best_target, best_rank = frac, target, rank
    return best_frac, best_target, best_rank


# Generic engineering words — a target title made only of these (e.g. "Staff Software Engineer",
# "Senior Java Backend Engineer") is NOT distinctive enough for the express lane: it matches every
# vanilla SWE posting. Those still get scored normally; the express lane is for the profile's
# *distinctive* targets (Forward Deployed Engineer, AI Architect, Robotics Software Engineer, …).
_GENERIC_TITLE_TOKENS = {"software", "engineer", "engineering", "backend", "back", "end", "developer",
                         "development", "senior", "staff", "principal", "lead", "sr"}


def strong_target_title(job: dict, profile: dict) -> str | None:
    """The matched target title when the job title fully contains a DISTINCTIVE target AND carries
    no avoid-word — the 'express lane' signal: a role literally titled one of the profile's
    distinctive targets, not disqualified. A structural match the numeric score can under-summarize
    (comp not posted, etc.). Generic all-SWE targets are excluded (they'd match everything)."""
    title = (job.get("title") or "").lower()
    frac, target, _ = best_target_title(title, profile)
    if frac < 0.99 or not target:
        return None
    if _title_tokens(target) <= _GENERIC_TITLE_TOKENS:  # nothing distinctive → not express-eligible
        return None
    if any(_word_hit(title, k) for k in profile.get("title_keywords_avoid", [])):
        return None
    return target


def score_title(job: dict, profile: dict) -> tuple[float, list[str]]:
    """Two paths, take the better: (a) direct match against the profile's target titles — a role
    literally titled the #1 target should score near-max even though it's one keyword; (b) the
    keyword+seniority tally (rewards well-labeled multi-signal titles). Rebuilt after the
    keyword-count-only path scored exact target-title roles 10/30 and buried them below the alert
    bar."""
    title = (job.get("title") or "").lower()
    notes = []

    # (a) best match to a real target title; earlier entries (the profile's priorities) score a touch higher.
    best_frac, best_target, best_rank = best_target_title(title, profile)
    targets = profile.get("titles", [])
    target_points = 0.0
    if best_frac >= 0.99:                      # full target title present
        priority = 1.0 - 0.25 * (best_rank / max(1, len(targets) - 1))  # 1.0 (top) .. 0.75
        target_points = 24.0 + 6.0 * priority  # 30 for a top-tier exact title, ~28.5 lower tiers
        notes.append(f"target-title match: {best_target}")
    elif best_frac >= 0.6:                      # most of a target title present
        target_points = 18.0 + 6.0 * (best_frac - 0.6) / 0.4
        notes.append(f"partial target-title: {best_target} ({best_frac:.0%})")

    # (b) keyword + seniority tally (the old path, kept as a floor for well-labeled titles).
    strong = [k for k in profile.get("title_keywords_strong", []) if _word_hit(title, k)]
    seniority = [k for k in profile.get("seniority", []) if _word_hit(title, k)]
    kw_points = min(20.0, 10.0 * len(strong)) + (10.0 if seniority else 0.0)
    if seniority:
        notes.append(f"seniority: {', '.join(seniority)}")
    if strong:
        notes.append(f"title hits: {', '.join(strong)}")

    points = max(target_points, kw_points)
    avoid = [k for k in profile.get("title_keywords_avoid", []) if _word_hit(title, k)]
    if avoid:
        points = max(0.0, points - 15.0 * len(avoid))
        notes.append(f"title avoid-words: {', '.join(avoid)}")
    return min(30.0, points), notes


def score_skills(job: dict, profile: dict) -> tuple[float, list[str]]:
    """Absolute weighted-hit score with saturation — NOT normalized by total profile weight.

    Rebuilt after the old `35 * hit_weight / total_weight` meant every skill added to the profile
    *lowered* every job's skills score (bigger denominator), systematically burying good matches.
    Now a job that hits a solid cluster of the profile's skills saturates toward full marks
    regardless of how long the skill list is. K≈24 → ~5 strong skills (java+ai+llm+agentic+mcp)
    reaches ~30/35; a light single-skill hit lands ~12."""
    text = f"{job.get('title') or ''}\n{job.get('description') or ''}".lower()
    skills: dict = profile.get("skills", {})
    if not skills:
        return 17.5, ["no skills in profile — neutral"]
    hit_weight = 0.0
    hits = []
    for keyword, weight in skills.items():
        if weight > 0 and _word_hit(text, keyword):
            hit_weight += weight
            hits.append(keyword)
    points = round(35.0 * (1.0 - math.exp(-hit_weight / 24.0)), 1)
    return points, ([f"skills ({hit_weight:g}w): {', '.join(hits)}"] if hits else ["no profile skills found in posting"])


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


def comp_bounds(job: dict) -> tuple[float | None, str | None]:
    """The job's top-of-band annual, plus a note when it came from the prose rather than a field.

    Structured fields first; if the source gave none, re-read the description. `fetch_jobs._job()`
    already does this at fetch time over the *untruncated* text, so this is the fallback for rows
    cached before that existed and for sources that never populate comp at all.
    """
    high = _annual(job.get("comp_max")) or _annual(job.get("comp_min"))
    if high is not None:
        return high, None
    low_t, high_t, note = comp_from_text(job.get("description") or "")
    parsed = _annual(high_t) or _annual(low_t)
    return (parsed, note) if parsed is not None else (None, None)


def score_comp(job: dict, profile: dict) -> tuple[float, list[str]]:
    floor = profile.get("min_total_comp")
    target = profile.get("target_total_comp") or floor
    high, parsed_note = comp_bounds(job)
    if not floor or high is None:
        note = "comp unknown — neutral" if high is None else "no comp floor in profile — neutral"
        return 8.0, [note]

    if high >= target:
        points, notes = 15.0, [f"comp ~{int(high):,} ≥ target {int(target):,}"]
    elif high >= floor:
        span = max(1.0, float(target - floor))
        points = round(8.0 + 7.0 * (high - floor) / span, 1)
        notes = [f"comp ~{int(high):,} between floor and target"]
    else:
        points = round(max(0.0, 8.0 * high / floor), 1)
        notes = [f"comp ~{int(high):,} below floor {int(floor):,}"]

    if parsed_note:
        # Say where the number came from: a regex over prose, not a field the employer filled in.
        notes.append(parsed_note)
    return points, notes


def comp_unknown_flags(job: dict, profile: dict) -> list[str]:
    """Flag a posting whose comp we genuinely could not establish.

    Deliberately a **flag, not a penalty**. The profile's comp notes are explicit: surface the data
    and ask — loosening the bands is the owner's call, never a silent filter change. An unknown comp
    scoring `8.0 neutral` is honest (we don't know); the bug was that nothing *said so*, so an
    unverified role sat in the top ten looking settled. Now the digest carries a flag, and a work-up
    knows to get a real number before drafting.
    """
    if not profile.get("min_total_comp"):
        return []
    high, _ = comp_bounds(job)
    if high is not None:
        return []
    return ["comp: not posted — scored neutral, NOT verified. Get a real number at source before "
            "a work-up; an unknown comp can hide a role that fails the floor."]


def score_location(job: dict, profile: dict) -> tuple[float, list[str]]:
    remote_pref = (profile.get("remote") or "any").lower()
    location = (job.get("location") or "").lower()
    remote = job.get("remote")
    # A "Remote" token in the location used to upgrade ANY posting to fully-remote — including one
    # whose own fields said remote=false / workplace_type=Hybrid (a full 15/15 for location on a role
    # that would have meant relocating). Believe the fields over the word.
    contradiction = remote_contradiction(job)
    if remote is not True and _word_hit(location, "remote") and not contradiction:
        remote = True
    if contradiction:
        # Half credit: a remote option might be real, but nothing here establishes it. The flag on the
        # job carries the detail; this note is why the band isn't full.
        onsite_type = structured_onsite(job) or "not remote"
        return 6.0, [f"'Remote' in the location but the ATS says {onsite_type} — unverified, see flags"]
    if remote is True:
        if remote_pref in ("required", "preferred"):
            # Remote-but-region-locked postings only count if the region fits. `location` alone missed
            # every HN post — they have none, and bill the region in the title ("REMOTE (Europe)"), so
            # fall back to that claim.
            regions = [r.lower() for r in profile.get("remote_regions_ok", [])]
            region_text = location or (remote_region_claim(job) or "").lower()
            if regions and region_text and not any(_word_hit(region_text, r) for r in regions) and \
               any(_word_hit(region_text, bad) for bad in ("europe", "emea", "apac", "uk only", "canada only")):
                return 3.0, [f"remote but region-locked: {job.get('location') or remote_region_claim(job)}"]
            return 15.0, ["remote"]
        return 12.0, ["remote (no strong preference)"]
    for ok in profile.get("locations_ok", []):
        if location and _word_hit(location, ok.lower()):
            return 12.0, [f"location ok: {job.get('location')}"]
    if remote_pref == "required":
        return (0.0, [f"not remote: {job.get('location') or 'unknown location'}"]) if location else (7.0, ["location unknown"])
    return (4.0, [f"location mismatch: {job.get('location')}"]) if location else (7.0, ["location unknown"])


_WEST_TZ_SIGNALS = (
    ("pacific time", 2.0), ("pacific standard time", 2.0), ("pacific daylight time", 2.0),
    ("pst time zone", 2.0), ("pst timezone", 2.0), ("west coast hours", 2.0), ("west coast time zone", 2.0),
    ("mountain time", 1.0), ("mountain standard time", 1.0),
)


def timezone_tilt(text: str, remote_verdict: str) -> tuple[float, list[str]]:
    """A small WEST-is-better tilt for REMOTE roles (a profile-driven preference): a western required
    timezone means a later start for an owner in US Central time, so it's a mild plus. Never a
    penalty — an Eastern tz just sets earlier hours, it is not a fit risk. Onsite west-coast offices
    are a location question, not a timezone perk, so this only tilts remote / remote? verdicts."""
    if remote_verdict not in ("remote", "remote?"):
        return 0.0, []
    for phrase, pts in _WEST_TZ_SIGNALS:
        if phrase in text:
            return pts, [f"west-coast timezone ('{phrase}'): +{pts:g} — a later start for the owner"]
    return 0.0, []


def job_age_days(job: dict) -> float | None:
    """Age of a posting in days, or None when the date is missing/unparseable."""
    posted = job.get("posted_at") or ""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(posted))
    if not match:
        return None
    try:
        posted_epoch = time.mktime(time.strptime(match.group(0), "%Y-%m-%d"))
    except (ValueError, OverflowError):
        return None
    return max(0.0, (time.time() - posted_epoch) / 86400)


def score_recency(job: dict) -> tuple[float, list[str]]:
    """The small additive freshness bonus (0-5).

    This only separates *fresh* postings from each other; it deliberately cannot sink an
    old one (1 point vs 5 is noise against a 100-point total). Killing dead postings is
    ``age_decay``'s job — see there.
    """
    age_days = job_age_days(job)
    if age_days is None:
        return 2.0, ["post date unknown"]
    if age_days <= 7:
        return 5.0, [f"posted {int(age_days)}d ago"]
    if age_days <= 30:
        return 3.0, [f"posted {int(age_days)}d ago"]
    return 1.0, [f"posted {int(age_days)}d ago"]


# --- age decay ------------------------------------------------------------------
# A dead posting is worthless no matter how well it matches: a 95% role that was filled
# a year ago is noise, not a 95% opportunity. So age is a MULTIPLIER on the whole score,
# not another additive band — an additive band mathematically can't do this (the old
# recency band was 0-5, so a 5-year-old posting lost 4 points and still cleared 60%).
#
# Shape: a grace period where postings are genuinely live (no penalty), then exponential
# decay — smooth rather than stepwise so two identical roles at 89d and 91d don't differ
# by a tier for no reason. The *tiers* are the readable label on top of that curve, and
# `ancient` is a real hard drop: it flags the job and blocks the hunt's express lane.
AGE_GRACE_DAYS = 30.0        # still live — full score
AGE_HALF_LIFE_DAYS = 120.0   # after grace, score halves every 4 months
AGE_ANCIENT_DAYS = 365.0     # past a year: assume filled

# Comp-unverified warnings fire only at/above this. The alert bar is 60; a touch under it keeps the
# borderline cases warned without papering the board (most postings disclose nothing).
COMP_WARN_FLOOR = 55.0

AGE_TIERS = ((30.0, "fresh"), (90.0, "recent"), (180.0, "stale"), (365.0, "old"))


def age_tier(age_days: float | None) -> str:
    if age_days is None:
        return "unknown"
    for limit, name in AGE_TIERS:
        if age_days <= limit:
            return name
    return "ancient"


def age_decay(job: dict) -> tuple[float, str, list[str], list[str]]:
    """Return (multiplier, tier, notes, flags) for how likely this posting is still open.

    Unknown dates are left alone (multiplier 1.0) rather than punished — guessing against a
    missing field would silently bury roles.
    """
    age_days = job_age_days(job)
    tier = age_tier(age_days)
    if age_days is None or age_days <= AGE_GRACE_DAYS:
        return 1.0, tier, [], []
    mult = 0.5 ** ((age_days - AGE_GRACE_DAYS) / AGE_HALF_LIFE_DAYS)
    notes = [f"age decay: {int(age_days)}d old — {tier} (x{mult:.2f})"]
    flags = []
    if age_days > AGE_ANCIENT_DAYS:
        flags.append(f"stale: posted {int(age_days)}d ago — almost certainly filled")
    return mult, tier, notes, flags


# --- preference-boost saturation ------------------------------------------------------------------
# The boosts used to add LINEARLY with no ceiling, and the final min(100, total) clamp then flattened
# the whole top of the board into a tie: several jobs at raw 100+ all displayed as exactly 100 — a
# base-79.8 role with unknown comp (+25 in boosts) shown dead-even with a base-93.2 role carrying a
# verified comp number. AI job ads word-drop "agentic"/"inference"/"ml infrastructure" as wallpaper —
# a quarter of a live board collected +7 or more — so keyword bingo was outvoting the bands. Same
# recurring shape as every scorer inflator so far: the text lies, and we were paying it a bonus.
#
# Two rules now:
#   1. A preference keyword that is ALSO in the skills map gets no boost — the (saturating) skills
#      band already counted that word; boosting it again was a straight double-count. The profile
#      entries stay: they document the owner's tilt, and they'd come back to life if the keyword
#      ever left the skills map.
#   2. The remaining POSITIVE boosts saturate like the skills band (cap x (1 - e^(-sum/K))) instead
#      of stacking: ~+2 stays ~+2, but piles of buzzword hits flatten toward the cap, so preferences
#      tilt the ranking without ever outvoting a verified comp number or a real band difference.
# Negative preferences (e.g. banking -3) stay linear — penalties are rare, deliberate, and the
# owner's own.
PREF_BOOST_CAP = 10.0
PREF_BOOST_K = 8.0


def preference_boost(text: str, profile: dict) -> tuple[float, list[str]]:
    """The post-band preference adjustment: saturated positives + linear negatives, with notes."""
    skills = profile.get("skills") or {}
    positive, negative = 0.0, 0.0
    pos_hits, neg_hits, overlap = [], [], []
    for keyword, boost in (profile.get("preferences") or {}).items():
        if not _word_hit(text, keyword):
            continue
        points = float(boost)
        if points > 0 and float(skills.get(keyword, 0) or 0) > 0:
            overlap.append(keyword)
            continue
        if points > 0:
            positive += points
            pos_hits.append(f"{keyword} {points:+g}")
        else:
            negative += points
            neg_hits.append(f"{keyword} {points:+g}")
    saturated = round(PREF_BOOST_CAP * (1.0 - math.exp(-positive / PREF_BOOST_K)), 1) if positive else 0.0
    notes = []
    if saturated:
        notes.append(f"preferences +{saturated:g} (saturated from {', '.join(pos_hits)})")
    if negative:
        notes.append(f"preferences {negative:+g} ({', '.join(neg_hits)})")
    if overlap:
        notes.append(f"preference words already counted by the skills band (no double boost): "
                     f"{', '.join(overlap)}")
    return saturated + negative, notes


# Ghost-filter knobs — aggregator/recruiter + evergreen down-ranks. Down-rank, never hide.
AGGREGATOR_PENALTY = 14.0
EVERGREEN_DAYS = 50.0
EVERGREEN_PENALTY = 6.0

# Employer-name shapes that mean "not the hiring company" — job-board reposts ("… Jobs"), staffing/
# recruiting firms, known aggregators. High-precision on purpose: must NOT hit real employers (CVS
# Health, Airbnb, Campbell Soup…), so no generic "Solutions / Technologies / Group" suffixes.
_AGGREGATOR_RE = re.compile(
    r"\bjobs\s*$"                                          # employer name ending in "… Jobs"
    r"|\b(?:job\s*board|hiring\s+network|career\s+portal)\b"
    r"|\brecruit(?:ing|er|ers|ment)?\b|\bstaffing\b|\bheadhunt\w*\b"
    r"|\btalent\s+(?:acquisition|solutions|partners|group)\b|\bplacement\s+(?:agency|services)\b"
    r"|\b(?:jobot|cybercoders|ziprecruiter|crossover|braintrust|turing|toptal|mastech|teksystems|"
    r"apex\s+systems|robert\s+half|insight\s+global|motion\s+recruit\w*|dice)\b",
    re.I)


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
    flags += clearance_flags(text, profile)
    flags += us_eligibility_flags(job, profile)
    flags += location_conflict_flags(text, job, profile, bands)  # may dock the location band
    flags += experience_gap_flags(text, profile)
    flags += title_avoid_flags(job, profile)      # avoid-word in the title = dealbreaker (caps at 25)
    flags += avoid_company_flags(job, profile)    # named avoid-companies = dealbreaker
    flags += legal_risk_flags(job, profile, text)  # competitor/non-compete exposure = FLAG, never a drop
    contradiction = remote_contradiction(job)     # the posting's own fields vs its 'Remote' billing
    if contradiction:
        flags.append(contradiction)
    remote_verdict = classify_remote(job, profile, text)
    if remote_verdict == "relocation" and profile.get("commutable_locations"):
        where = job.get("location") or (effective_locations(job) or ["location unstated"])[0]
        onsite_type = structured_onsite(job)
        onsite_note = (f" (structured {onsite_type} at source)" if onsite_type
                       else " (posting says ONSITE/in-office)" if _explicit_onsite(text) else "")
        flags.append(
            f"dealbreaker: relocation implied — '{where}'{onsite_note} is beyond the "
            f"~{profile.get('max_commute_minutes_one_way', 90)}-min commute zone with no credible "
            "remote signal; relocation is a hard no")
        bands["location"] = min(bands["location"], 1.0)
    total = sum(bands.values())
    if any(flag.startswith("dealbreaker") for flag in flags):
        total = min(total, 25.0)
    pref_points, pref_notes = preference_boost(text, profile)
    total += pref_points
    notes.extend(pref_notes)

    # Aggregator / recruiter down-rank (ghost filter): a posting fronted by a job-board aggregator or
    # a staffing/recruiting firm (mostly via open-aggregation sources) is lower-signal and often
    # ghost/repost noise. Graded, company-field penalty + flag. Down-ranks, never hides — the source
    # filter and a "show all" view still surface it.
    company_l = (job.get("company") or "").lower()
    if company_l and _AGGREGATOR_RE.search(company_l):
        total -= AGGREGATOR_PENALTY
        notes.append(f"aggregator/recruiter source ({job.get('company')}): -{AGGREGATOR_PENALTY:g}")
        flags.append(f"aggregator/recruiter: '{job.get('company')}' looks like a job-board or staffing "
                     "front, not the hiring employer — verify the real company before investing")

    # Evergreen down-rank (ghost filter): a role LISTED for months (first_seen age, fed in as
    # `days_listed` from seen.json via `score_jobs --seen`) is likely an always-open / perpetual req
    # collecting résumés, not a live opening. Age decay works off the self-reported posted date, which a
    # repost refreshes; first_seen age can't be gamed that way.
    days_listed = job.get("days_listed")
    if isinstance(days_listed, (int, float)) and days_listed >= EVERGREEN_DAYS:
        total -= EVERGREEN_PENALTY
        notes.append(f"evergreen: listed ~{int(days_listed)}d: -{EVERGREEN_PENALTY:g}")
        flags.append(f"evergreen: listed ~{int(days_listed)} days — likely an always-open / perpetual "
                     "req, verify it's a live opening before investing")

    # WEST-is-better timezone tilt for remote roles (never a penalty) — see timezone_tilt.
    tz_pts, tz_notes = timezone_tilt(text, remote_verdict)
    if tz_pts:
        total += tz_pts
        notes.extend(tz_notes)

    # Age decay multiplies the finished assessment — applied last, after preference
    # boosts, so a dead posting's boosts can't rescue it.
    decay, tier, decay_notes, decay_flags = age_decay(job)
    total *= decay
    notes.extend(decay_notes)
    flags += decay_flags

    total = max(0.0, min(100.0, total))

    # Only warn about an unverified comp on a role the owner might actually pursue. Flagged
    # unconditionally it fires on most of the board (many employers still don't post), and a flag on
    # two-thirds of the board is wallpaper, not a signal. Below the alert bar the role is never worked
    # up anyway, so the warning has nothing to protect. Deliberately set a touch under the bar so a
    # borderline role still carries it.
    if total >= COMP_WARN_FLOOR:
        flags += comp_unknown_flags(job, profile)

    days = job_age_days(job)
    scored = dict(job)
    scored.update({
        "match_percent": round(total, 1),
        "score_breakdown": bands,
        "score_notes": notes,
        "flags": flags,
        "age_days": None if days is None else int(days),
        "age_tier": tier,
        "remote_verdict": remote_verdict,
        "target_title": strong_target_title(job, profile),
    })
    return scored


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proteus_paths  # noqa: E402  (canonical state/ vs out/ paths — same late-import as fetch_jobs)


def _annotate_days_listed(jobs: list[dict], seen_path: str | None) -> None:
    """Stamp ``job['days_listed']`` from the seen-ledger's ``first_seen`` (for evergreen flagging).

    Best-effort and non-fatal: no ``--seen``, a missing/broken ledger, or an unparseable date all just
    leave ``days_listed`` unset. Keys the ledger exactly as ``hunt_cycle`` writes it (url, else
    ``source:external_id``). The ledger is written *after* scoring each cycle, so at scoring time it
    holds prior cycles' first-sightings — a brand-new posting isn't in it yet, which is correct.
    """
    if not seen_path or not os.path.exists(seen_path):
        return
    try:
        with open(seen_path, encoding="utf-8") as fh:
            ledger = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(ledger, dict):
        return
    now = datetime.now(timezone.utc)
    for job in jobs:
        key = job.get("url") or f"{job.get('source')}:{job.get('external_id')}"
        first_seen = (ledger.get(key) or {}).get("first_seen")
        if not first_seen:
            continue
        try:
            fs = datetime.fromisoformat(str(first_seen).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if fs.tzinfo is None:
            fs = fs.replace(tzinfo=timezone.utc)
        job["days_listed"] = max(0.0, (now - fs).total_seconds() / 86400.0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score jobs against the profile (Proteus phase 2).")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--jobs", required=True, help="output of fetch_jobs.py")
    parser.add_argument("--out", required=True)
    parser.add_argument("--top", type=int, default=15, help="keep the N best (default 15; 0 = all)")
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--seen", default=None,
                        help="path to seen.json (first_seen per url) — enables evergreen flagging")
    args = parser.parse_args(argv)

    with open(args.profile, encoding="utf-8") as fh:
        profile = json.load(fh)
    # Same redirect fetch_jobs applied when writing, so `--jobs out/<date>/jobs.json` still resolves
    # after the board moved to state/runs/. Falls back to the literal path if that's what exists.
    jobs_path = proteus_paths.resolve_raw_artifact(args.jobs)
    if not os.path.exists(jobs_path) and os.path.exists(args.jobs):
        jobs_path = args.jobs
    with open(jobs_path, encoding="utf-8") as fh:
        jobs_doc = json.load(fh)

    _annotate_days_listed(jobs_doc.get("jobs", []), args.seen)
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
    out_path = proteus_paths.resolve_raw_artifact(args.out)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=1)
    summary = [{"title": j["title"], "company": j["company"], "match": j["match_percent"]} for j in kept[:5]]
    result = {"ok": True, "considered": len(scored), "kept": len(kept), "out": str(out_path), "top": summary}
    if str(out_path) != str(args.out):
        result["note"] = "raw board written to state/ (runtime churn); out/ is for deliverables"
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
