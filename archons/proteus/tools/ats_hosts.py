#!/usr/bin/env python3
"""Read the hiring employer out of an apply URL, when the URL is evidence and not a guess.

An applicant-tracking system hosts a req **under the employer's own org slug**:
``jobs.ashbyhq.com/examplecorp/1b26f0a2-…``, ``job-boards.greenhouse.io/acme/jobs/…``,
``globex.wd5.myworkdayjobs.com/…``. The org is in the URL because the ATS put it there on the
employer's behalf, which makes it the one piece of employer attribution in the pipeline that
nobody in the republication chain can restate.

**Why this module exists.** A posting can reach the hunt through several hands — employer careers
page → a job-curation board → a republisher → an open aggregator → here — and each hand can put its
own name in the ``company`` field. The curator's name, or a staffing agency's, is what lands, and the
real employer (whose req lives on its own ATS) is nowhere in the row except the apply link.

**What this module deliberately does NOT do: decide whether a company is a curator or a staffing
agency.** There is no name list here and there must never be one — it is unmaintainable, and it is
wrong about real employers (score_jobs' ``_AGGREGATOR_RE`` already carries that job, and carries it
narrowly and on purpose). This module answers a different, checkable question: *does this URL name
an org on a system only that org's own recruiters can publish to?* If the host is not a recognised
ATS the answer is **None** and the caller changes nothing. Declining is the safe direction, and it
is the common one.

Pure and offline: string parsing, no network, no state.
"""
from __future__ import annotations

import re
import urllib.parse

# ---------------------------------------------------------------- recognised ATS URL shapes
#
# Two shapes, because ATSs pick one or the other:
#
#   PATH   the org is the first path segment on a shared host — jobs.lever.co/<org>/<id>
#   HOST   the org is the leftmost label of a per-tenant host — <tenant>.wd5.myworkdayjobs.com
#
# Adding a system is a one-line edit here. The bar for adding one: the org segment must be the
# EMPLOYER's identifier on that system, not a board/brand/region slug — otherwise a correction
# built on it renames a job to something that isn't its employer, which is worse than the bug.

_PATH_ORG_HOSTS: dict[str, str] = {
    "jobs.ashbyhq.com": "ashby",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "boards.eu.greenhouse.io": "greenhouse",
    "job-boards.eu.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "careers.smartrecruiters.com": "smartrecruiters",
    "apply.workable.com": "workable",
}

# Per-tenant hosts: <tenant>.<pod>.myworkdayjobs.com. The suffix needs at least two more labels in
# front of it, so a bare `myworkdayjobs.com` or `wd5.myworkdayjobs.com` yields nothing rather than
# reading a pod id ("wd5") as an employer.
_HOST_ORG_SUFFIXES: dict[str, str] = {
    ".myworkdayjobs.com": "workday",
    ".myworkdaysite.com": "workday",
}

# First-path-segment values that are routing, not an org. Small on purpose: a miss here costs a
# correction we could have made, a false positive costs a WRONG employer on the board. The live
# case is Workable, which serves both `apply.workable.com/<org>/j/<hash>` and a bare
# `apply.workable.com/j/<hash>` with no org in it at all.
_NOT_AN_ORG = frozenset({
    "j", "job", "jobs", "search", "api", "embed", "careers", "company", "companies", "apply", "en",
})

_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")


def _host_and_segments(url: str) -> tuple[str, list[str]]:
    try:
        parsed = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return "", []
    if parsed.scheme not in ("http", "https", ""):
        return "", []
    host = (parsed.hostname or "").lower()
    segments = [urllib.parse.unquote(s) for s in parsed.path.split("/") if s]
    return host, segments


def employer_org(url: str) -> tuple[str, str] | None:
    """``(ats_name, org_slug)`` when ``url`` is a req on a recognised ATS that names its org.

    ``None`` for everything else — an unrecognised host, a recognised host with no org segment, or
    a segment that is plainly routing. **A None here means the caller must leave the feed's value
    alone**; it is not a soft "probably not".

    >>> employer_org("https://jobs.ashbyhq.com/examplecorp/00000000-0000-0000-0000-000000000000")
    ('ashby', 'examplecorp')
    >>> employer_org("https://globex.wd5.myworkdayjobs.com/en-US/GlobexCareers/job/x")
    ('workday', 'globex')
    >>> employer_org("https://reposter.example/join/some-repost") is None
    True
    """
    host, segments = _host_and_segments(url)
    if not host:
        return None

    ats = _PATH_ORG_HOSTS.get(host)
    if ats:
        slug = segments[0].lower() if segments else ""
        if slug and slug not in _NOT_AN_ORG and _SLUG_OK.match(slug):
            return ats, slug
        return None

    for suffix, name in _HOST_ORG_SUFFIXES.items():
        if host.endswith(suffix):
            labels = host[: -len(suffix)].split(".")
            # Needs the tenant AND the pod in front of the suffix, else the "tenant" is the pod.
            if len(labels) >= 2 and labels[0] and _SLUG_OK.match(labels[0]):
                return name, labels[0]
            return None
    return None


def is_ats_url(url: str) -> bool:
    """True when ``url`` is a req on a recognised ATS that names its org — the strongest apply link
    on offer, and the only kind this module will attribute an employer from."""
    return employer_org(url) is not None


# ------------------------------------------------------------------- names, derived not guessed

def normalize_name(name: str) -> str:
    """Casing/punctuation-insensitive key for comparing an employer name against an org slug."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


# Stripped only for the AGREEMENT test, never from what gets displayed: "ExampleCorp, Inc." and the
# Ashby slug "examplecorp" are the same employer, and rewriting one to the other would be churn
# dressed up as a correction.
_LEGAL_SUFFIX = re.compile(
    r"\b(?:inc|llc|l\.?l\.?c|ltd|limited|corp|corporation|co|company|gmbh|plc|s\.?a|n\.?v|b\.?v|"
    r"ab|oy|a\.?s|pbc|pte|pty|srl|sarl|kk)\b\.?", re.I)


def names_agree(reported: str | None, org_slug: str) -> bool:
    """True when the feed's employer name and the ATS org slug are the same employer.

    Agreement means **there is nothing to correct** — the overwhelmingly common case, including
    every direct board. Only a disagreement is news.
    """
    a = normalize_name(_LEGAL_SUFFIX.sub(" ", reported or ""))
    b = normalize_name(org_slug)
    return bool(a) and bool(b) and a == b


def display_name(org_slug: str) -> str:
    """A human-readable employer name from an org slug — ``initech-robotics`` → *Initech Robotics*.

    **Approximate by construction, in exactly one respect: capitalisation.** A slug is lowercase,
    so ``examplecorp`` renders *Examplecorp* rather than *ExampleCorp*. That is a known and accepted
    cost — the alternative is a hand-maintained casing table, which is the same unmaintainable list
    this module refuses to keep. Callers preserve what the feed reported alongside it (see
    ``fetch_jobs.attribute_employers``, which also prefers an exact name already in hand from a
    direct board over this fallback), so nothing is lost, and the *identity* is right even when a
    capital letter isn't.
    """
    words = [w for w in re.split(r"[-_.]+", (org_slug or "").strip()) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or (org_slug or "")
