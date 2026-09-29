#!/usr/bin/env python3
"""Proteus tool: fetch open jobs from ATS job-board APIs + free aggregators (stdlib only).

Sources (all public, no auth unless noted):
  - Greenhouse  boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true   (per-company watchlist)
  - Lever       api.lever.co/v0/postings/<slug>?mode=json                     (per-company watchlist)
  - Ashby       api.ashbyhq.com/posting-api/job-board/<slug>                  (per-company watchlist)
  - Remotive    remotive.com/api/remote-jobs?search=...                       (aggregator, remote-only)
  - Arbeitnow   arbeitnow.com/api/job-board-api                               (aggregator)
  - SmartRecruiters api.smartrecruiters.com/v1/companies/<slug>/postings      (per-company watchlist;
                    descriptions need one detail call per posting — capped via detail_cap)
  - RemoteOK    remoteok.com/api                                              (aggregator, remote-only)
  - HN hiring   hn.algolia.com (the monthly "Ask HN: Who is hiring?" thread — top-level comments)
  - Workday     <tenant>.<host>.myworkdayjobs.com/wday/cxs/... (per-company; UNOFFICIAL endpoint the
                public career sites themselves use — POST search + capped per-posting detail GETs;
                treat as best-effort, it can change without notice)
  - Rippling    api.rippling.com/platform/api/ats/v1/board/<slug>/jobs (per-company; public board API +
                capped per-posting detail GETs for descriptions/comp)
  - Adzuna      api.adzuna.com (aggregator; OPTIONAL — needs ADZUNA_APP_ID/ADZUNA_APP_KEY env)
  - WelcomeToTheJungle  api.welcometothejungle.com/api/v3/organizations/<slug>/jobs (per-company;
                UNOFFICIAL — the endpoint wttj.com's own front end calls, same standing as Workday
                above. Public, no auth, honest User-Agent accepted. Rows carry STRUCTURED
                salary_min/salary_max/currency on most rows and a structured office country_code,
                which is better comp/location signal than Greenhouse gives. Paginates ?page=N at
                30/page. Descriptions are NOT in the list rows — they need one detail call each, so
                a fetch here NEVER buys them. They are bought AFTER scoring, for a capped shortlist
                of the postings that are about to reach the owner, by hunt_cycle.hydrate_and_rescore
                calling hydrate_wttj_descriptions below (a small fixed cap per cycle TOTAL, not per
                company; the rest of the board stays visibly text-less rather than guessed at).

                Its board-wide SEARCH endpoint (/api/v3/search/jobs) is NOT used, by standing
                decision. It 403s every unauthenticated client, as does /api/v3/jobs-matches/counts;
                those are the two personalised endpoints, and the gate is an account session, not a
                missing header (holding the cookies the API itself mints plus the headers its own
                front end sends still 403s). Company DISCOVERY therefore comes from their published
                sitemaps instead — query-string-free and robots-allowed — via wttj_discover.py, which
                is a separate, occasional tool, not part of a fetch cycle.

Reads a watchlist JSON (see watchlist.json next to this tool), emits one normalized JSON document:
  {"fetched_at", "count", "warnings": [...], "jobs": [{source, company, title, location, remote,
   url, apply_url, posted_at, comp_min, comp_max, comp_currency, comp_note, employment_type,
   external_id, description}]}

WHO THE EMPLOYER IS, AND WHERE TO APPLY. A posting that reaches Proteus through republishers can
arrive naming the wrong company — a curation board or a staffing agency rather than the employer.
``attribute_employers`` corrects the employer **from the apply URL and nothing else** — an ATS hosts
a req under the employer's own org slug, so ``jobs.ashbyhq.com/examplecorp/…`` names ExampleCorp no
matter whose name the feed put in the field. Unrecognised host ⇒ nothing changes. A corrected row is
MARKED, never laundered: it keeps ``company_reported`` and gets a scorer flag. Neither spends a
request.

Per-source reach of ``apply_url`` — what each feed can give for free:

  - welcometothejungle      The field exists (``job.apply_url``) but is **detail-only — the list rows
                            carry no such key**, so it fills in only for rows
                            ``hydrate_wttj_descriptions`` bought a detail for.
  - remoteok                Has an ``apply_url`` field, but it is byte-identical to ``url`` in
                            practice — capturing it would add a field no surface can show. Not
                            captured, deliberately.
  - remotive, arbeitnow,    The employer's own link is not in the list payload (Remotive's is loose
    adzuna                  inside description HTML, which ``html_to_text`` has already discarded by
                            normalize time; Adzuna gives only its own tracking redirect). Each would
                            need a fetch per posting. Out of scope.
  - greenhouse, lever,      No gap: a direct board's ``url`` IS the req, so a second link would be
    ashby, smartrecruiters, the same link. These are also never re-attributed — see
    workday, rippling       ``attribute_employers``.
  - hn_hiring               Prose typed by the employer; no structured link to capture.

Per-source failures are warnings, never fatal — a cut-short run still delivers value.

Run:  python fetch_jobs.py --watchlist watchlist.json --out out/jobs.json [--max-per-company 50]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import ats_hosts
from proteus_comp import comp_from_text
from source_tiers import MIRROR_SOURCES, tier_rank

USER_AGENT = "proteus-job-fetch/1.0 (personal job search; stdlib urllib)"
TIMEOUT = 25
# Storage cap only — the cached jobs-latest.json is already tens of MB at this size. NOT a parse cap:
# _job() reads comp out of the full text before applying this, because pay ranges live at the foot
# of a JD.
MAX_DESC_CHARS = 12000


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "li", "br", "ul", "ol", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str) -> str:
    """Best-effort HTML → plain text (stdlib), collapsed whitespace, block-aware newlines.

    Returns the FULL text. Truncation to ``MAX_DESC_CHARS`` happens in ``_job()``, *after* the comp
    parse — this function used to cap here, which silently ate the pay-range disclosure that
    Greenhouse/Ashby render at the foot of a JD (see ``_job``).
    """
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _get_json(url: str, warnings: list[str], label: str, post_body: dict | None = None,
              extra_headers: dict | None = None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    data = None
    if post_body is not None:
        data = json.dumps(post_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        warnings.append(f"{label}: {error}")
        return None


def _job(**fields) -> dict:
    base = {
        "source": None, "company": None, "title": None, "location": None, "remote": None,
        "workplace_type": None, "url": None, "posted_at": None, "comp_min": None, "comp_max": None,
        "comp_currency": None, "comp_note": None, "employment_type": None,
        "external_id": None, "description": "",
        # Where to actually apply, when the posting is somebody's COPY of another board's req and
        # the feed says where the original lives (welcometothejungle's mirror rows, once hydrated).
        # None on a direct board — its `url` already IS the req, so a second link would be the same
        # link — and None on an aggregator whose feed only knows its own page.
        "apply_url": None,
    }
    base.update(fields)

    # Every source funnels through here, so this is the one place the comp rescue has to live.
    # Pay-transparency ranges are prose at the BOTTOM of a Greenhouse/Ashby JD, not a structured
    # field — and the MAX_DESC_CHARS cap used to run before anything read them. A role posting a
    # range that fails the profile floor at top-of-band got stored as comp unknown and scored a free
    # +8/15 "neutral" — better than if it had disclosed honestly. Parse the FULL text, THEN truncate
    # for storage, so the cache stays bounded either way.
    description = base.get("description") or ""
    if base.get("comp_min") is None and base.get("comp_max") is None:
        low, high, note = comp_from_text(description)
        if low or high:
            base["comp_min"], base["comp_max"] = low, high
            base["comp_currency"] = base.get("comp_currency") or "USD"
            base["comp_note"] = base.get("comp_note") or note
    base["description"] = description[:MAX_DESC_CHARS]
    return base


# ---------------------------------------------------------------- per-source normalizers (pure)

def normalize_greenhouse(company: str, payload: dict) -> list[dict]:
    jobs = []
    for row in (payload or {}).get("jobs", []):
        jobs.append(_job(
            source="greenhouse", company=company,
            title=row.get("title"),
            location=((row.get("location") or {}).get("name")),
            url=row.get("absolute_url"),
            posted_at=row.get("updated_at") or row.get("first_published"),
            external_id=str(row.get("id", "")),
            description=html_to_text(row.get("content", "")),
        ))
    return jobs


def normalize_lever(company: str, payload: list) -> list[dict]:
    jobs = []
    for row in payload or []:
        categories = row.get("categories") or {}
        salary = row.get("salaryRange") or {}
        workplace = (row.get("workplaceType") or "").lower()
        jobs.append(_job(
            source="lever", company=company,
            title=row.get("text"),
            location=categories.get("location"),
            remote=True if workplace == "remote" else (False if workplace in ("on-site", "onsite") else None),
            url=row.get("hostedUrl") or row.get("applyUrl"),
            posted_at=_ms_to_iso(row.get("createdAt")),
            comp_min=salary.get("min"), comp_max=salary.get("max"),
            comp_currency=salary.get("currency"),
            employment_type=categories.get("commitment"),
            external_id=str(row.get("id", "")),
            description=(row.get("descriptionPlain") or html_to_text(row.get("description", ""))),
        ))
    return jobs


def normalize_ashby(company: str, payload: dict) -> list[dict]:
    jobs = []
    for row in (payload or {}).get("jobs", []):
        compensation = row.get("compensation") or {}
        summary = compensation.get("compensationTierSummary") or ""
        cmin, cmax = parse_comp_range(summary)   # "$238K – $290K • Offers Equity" → 238000, 290000
        # workplaceType is AUTHORITATIVE over the flaky isRemote flag (roles come back isRemote=true
        # while workplaceType="Hybrid" at a named office — the hybrid wins).
        wt = (row.get("workplaceType") or "").strip().lower()
        if wt == "remote":
            remote = True
        elif wt in ("hybrid", "onsite", "on-site", "in office"):
            remote = False
        else:
            remote = row.get("isRemote")
        location = row.get("location")
        secs = [s.get("location") for s in (row.get("secondaryLocations") or [])
                if isinstance(s, dict) and s.get("location")]
        if secs:
            location = ", ".join([location] + secs) if location else ", ".join(secs)
        jobs.append(_job(
            source="ashby", company=company,
            title=row.get("title"),
            location=location,
            remote=remote,
            workplace_type=(row.get("workplaceType") or None),
            url=row.get("jobUrl") or row.get("applyUrl"),
            posted_at=row.get("publishedAt"),
            comp_min=cmin, comp_max=cmax,
            comp_currency="USD" if (cmin or cmax) else None,
            comp_note=summary or None,
            employment_type=row.get("employmentType"),
            external_id=str(row.get("id", "")),
            description=(row.get("descriptionPlain") or html_to_text(row.get("descriptionHtml", ""))),
        ))
    return jobs


def normalize_remotive(payload: dict) -> list[dict]:
    jobs = []
    for row in (payload or {}).get("jobs", []):
        jobs.append(_job(
            source="remotive", company=row.get("company_name"),
            title=row.get("title"),
            location=row.get("candidate_required_location"),
            remote=True,
            url=row.get("url"),
            posted_at=row.get("publication_date"),
            comp_note=row.get("salary") or None,
            employment_type=row.get("job_type"),
            external_id=str(row.get("id", "")),
            description=html_to_text(row.get("description", "")),
        ))
    return jobs


def normalize_arbeitnow(payload: dict) -> list[dict]:
    jobs = []
    for row in (payload or {}).get("data", []):
        jobs.append(_job(
            source="arbeitnow", company=row.get("company_name"),
            title=row.get("title"),
            location=row.get("location"),
            remote=row.get("remote"),
            url=row.get("url"),
            posted_at=_epoch_to_iso(row.get("created_at")),
            employment_type=", ".join(row.get("job_types") or []) or None,
            external_id=row.get("slug"),
            description=html_to_text(row.get("description", "")),
        ))
    return jobs


def normalize_adzuna(payload: dict) -> list[dict]:
    jobs = []
    for row in (payload or {}).get("results", []):
        jobs.append(_job(
            source="adzuna", company=((row.get("company") or {}).get("display_name")),
            title=row.get("title"),
            location=((row.get("location") or {}).get("display_name")),
            url=row.get("redirect_url"),
            posted_at=row.get("created"),
            comp_min=row.get("salary_min"), comp_max=row.get("salary_max"),
            comp_currency="USD",
            external_id=str(row.get("id", "")),
            description=html_to_text(row.get("description", "")),
        ))
    return jobs


def normalize_smartrecruiters(company: str, payload: dict, details: dict | None = None) -> list[dict]:
    """`details` maps posting id → jobAd detail payload (fetched separately, capped)."""
    jobs = []
    for row in (payload or {}).get("content", []):
        posting_id = str(row.get("id", ""))
        description = ""
        detail = (details or {}).get(posting_id)
        if detail:
            sections = ((detail.get("jobAd") or {}).get("sections") or {})
            description = html_to_text("\n".join(
                str(section.get("text", "")) for section in sections.values() if isinstance(section, dict)))
        location = row.get("location") or {}
        location_name = ", ".join(p for p in (location.get("city"), location.get("region"), location.get("country")) if p)
        jobs.append(_job(
            source="smartrecruiters", company=row.get("company", {}).get("name") or company,
            title=row.get("name"),
            location=location_name or None,
            remote=location.get("remote"),
            url=f"https://jobs.smartrecruiters.com/{urllib.parse.quote(company)}/{posting_id}",
            posted_at=row.get("releasedDate"),
            employment_type=((row.get("typeOfEmployment") or {}).get("label")),
            external_id=posting_id,
            description=description,
        ))
    return jobs


def normalize_remoteok(payload: list) -> list[dict]:
    jobs = []
    for row in payload or []:
        if not isinstance(row, dict) or not row.get("position"):
            continue  # element [0] is a legal notice, not a job
        jobs.append(_job(
            source="remoteok", company=row.get("company"),
            title=row.get("position"),
            location=row.get("location") or "Remote",
            remote=True,
            url=row.get("url") or (f"https://remoteok.com/remote-jobs/{row.get('id')}" if row.get("id") else None),
            posted_at=(row.get("date") or "")[:19] or _epoch_to_iso(row.get("epoch")),
            comp_min=row.get("salary_min") or None, comp_max=row.get("salary_max") or None,
            external_id=str(row.get("id", "")),
            description=html_to_text(row.get("description", "")) or ", ".join(row.get("tags") or []),
        ))
    return jobs


_HN_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{2,3})\s?([kK])?\b")
_ONSITE_MARK = re.compile(r"\bon[\s-]?site\b|\bin[\s-]?office\b", re.I)


def parse_comp_range(text: str) -> tuple[int | None, int | None]:
    """Best-effort (min, max) USD/year from headline text like '$150K-$210K' or '$150,000-$210,000'.
    Bare 2-3 digit numbers and 'k'-suffixed ones are read as thousands; only $30k–$1M is trusted."""
    vals = []
    for m in _HN_MONEY.finditer(text):
        raw = int(m.group(1).replace(",", ""))
        if m.group(2) or raw < 1000:      # 'k' suffix, or a bare "150" → 150,000
            raw *= 1000
        if 30_000 <= raw <= 1_000_000:
            vals.append(raw)
    if len(vals) >= 2:
        return min(vals[0], vals[1]), max(vals[0], vals[1])
    if len(vals) == 1:
        return None, vals[0]
    return None, None


def normalize_hn_hiring(comments: list[dict], story_id: str) -> list[dict]:
    """Top-level comments of the monthly 'Ask HN: Who is hiring?' thread, one job post each.

    Convention: the first line is pipe-delimited tags — 'Company | Role | Location | ONSITE/REMOTE |
    $comp'. Location/comp/remote live in that text (no structured fields), so parse them: the scorer's
    text-aware location read handles the rest. remote is only asserted when the post says remote AND
    not ONSITE (an explicit ONSITE marker wins)."""
    jobs = []
    for row in comments or []:
        if str(row.get("parent_id", "")) != str(story_id):
            continue  # replies to a job post, not a post
        text = html_to_text(row.get("comment_text") or "")
        if not text:
            continue
        first_line = text.split("\n", 1)[0].strip()[:160]
        company = first_line.split("|", 1)[0].strip()[:80] or (row.get("author") or "unknown")
        comment_id = row.get("objectID") or row.get("story_id")
        onsite = bool(_ONSITE_MARK.search(first_line))
        remote = True if (re.search(r"\bremote\b", first_line, re.I) and not onsite) else None
        cmin, cmax = parse_comp_range(first_line)
        jobs.append(_job(
            source="hn_hiring", company=company,
            title=first_line or f"HN hiring post by {row.get('author')}",
            remote=remote,
            comp_min=cmin, comp_max=cmax, comp_currency="USD" if (cmin or cmax) else None,
            url=f"https://news.ycombinator.com/item?id={comment_id}",
            posted_at=(row.get("created_at") or "")[:19] or None,
            external_id=str(comment_id),
            description=text,
        ))
    return jobs


def normalize_rippling(company: str, listing: list, details: dict | None = None) -> list[dict]:
    """Rippling ATS (api.rippling.com/platform/api/ats/v1/board/<slug>/jobs). The list carries
    title/location/url; `details` maps uuid → the per-job detail (description dict {company, role} +
    payRangeDetails), fetched separately and capped."""
    jobs = []
    for row in listing or []:
        uuid = str(row.get("uuid", ""))
        wl = row.get("workLocation") or {}
        location = wl.get("label") if isinstance(wl, dict) else (wl or None)
        detail = (details or {}).get(uuid) or {}
        desc_obj = detail.get("description")
        description = ""
        if isinstance(desc_obj, dict):
            description = html_to_text("\n".join(str(v) for v in desc_obj.values() if v))
        elif isinstance(desc_obj, str):
            description = html_to_text(desc_obj)
        locs = detail.get("workLocations")
        if isinstance(locs, list) and locs:
            location = ", ".join(str(x) for x in locs)
        cmin = cmax = None
        for pr in (detail.get("payRangeDetails") or []):
            if isinstance(pr, dict):
                cmin = pr.get("min") or pr.get("minValue") or cmin
                cmax = pr.get("max") or pr.get("maxValue") or cmax
        emp = detail.get("employmentType") or {}
        jobs.append(_job(
            source="rippling", company=company,
            title=row.get("name"),
            location=location,
            remote=True if location and "remote" in location.lower() else None,
            url=row.get("url"),
            posted_at=(detail.get("createdOn") or "")[:19] or None,
            comp_min=cmin, comp_max=cmax,
            employment_type=(emp.get("id") if isinstance(emp, dict) else None),
            external_id=uuid,
            description=description,
        ))
    return jobs


def _workday_posted_to_iso(posted: str):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' → approx ISO."""
    if not posted:
        return None
    text = posted.lower()
    days = None
    if "today" in text:
        days = 0
    elif "yesterday" in text:
        days = 1
    else:
        match = re.search(r"(\d+)\+?\s*days?", text)
        if match:
            days = int(match.group(1))
    if days is None:
        return None
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))


def normalize_workday(company: str, base_url: str, payload: dict, details: dict | None = None) -> list[dict]:
    """`base_url` = https://<tenant>.<host>.myworkdayjobs.com/wday/cxs/<tenant>/<site>;
    `details` maps externalPath → detail payload (jobPostingInfo), fetched separately and capped."""
    site_root = base_url.replace("/wday/cxs/", "/", 1).rsplit("/", 1)  # human URL prefix fallback
    jobs = []
    for row in (payload or {}).get("jobPostings", []):
        path = row.get("externalPath") or ""
        info = ((details or {}).get(path) or {}).get("jobPostingInfo") or {}
        jobs.append(_job(
            source="workday", company=company,
            title=row.get("title"),
            location=info.get("location") or row.get("locationsText"),
            remote=True if "remote" in (str(info.get("remoteType") or "")).lower() else None,
            url=info.get("externalUrl") or (f"{site_root[0]}/{site_root[1]}{path}" if path else None),
            posted_at=_workday_posted_to_iso(info.get("postedOn") or row.get("postedOn") or ""),
            employment_type=info.get("timeType"),
            external_id=info.get("jobReqId") or path or str(row.get("bulletFields") or ""),
            description=html_to_text(info.get("jobDescription", "")),
        ))
    return jobs


# --------------------------------------------------------------------- welcometothejungle
#
# WTTJ hands back a structured country_code per office. The scorer's US-eligibility screen reads
# the *location string*, and its two curated lists are country NAMES and city names — so emitting
# "New York, United States" / "Louviers, France" is what makes the screen work without depending on
# whether a given city happens to be on the curated list. Emitting a bare "Louviers, FR" would fall
# through both (the trailing-code path needs >=3 comma segments), which is exactly the shape of the
# empty-location bug: a location the screen couldn't read let a German req alert as a US match.
_WTTJ_COUNTRY_NAMES = {
    "US": "United States", "GB": "United Kingdom", "UK": "United Kingdom", "CA": "Canada",
    "FR": "France", "DE": "Germany", "NL": "Netherlands", "BE": "Belgium", "ES": "Spain",
    "IT": "Italy", "PT": "Portugal", "PL": "Poland", "CZ": "Czech Republic", "SK": "Slovakia",
    "HU": "Hungary", "RO": "Romania", "BG": "Bulgaria", "GR": "Greece", "SE": "Sweden",
    "NO": "Norway", "DK": "Denmark", "FI": "Finland", "IE": "Ireland", "CH": "Switzerland",
    "AT": "Austria", "LU": "Luxembourg", "IS": "Iceland", "EE": "Estonia", "LV": "Latvia",
    "LT": "Lithuania", "SI": "Slovenia", "HR": "Croatia", "RS": "Serbia", "UA": "Ukraine",
    "IN": "India", "CN": "China", "JP": "Japan", "KR": "South Korea", "SG": "Singapore",
    "HK": "Hong Kong", "TW": "Taiwan", "AU": "Australia", "NZ": "New Zealand", "BR": "Brazil",
    "MX": "Mexico", "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "PE": "Peru",
    "ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya", "EG": "Egypt", "TR": "Turkey",
    "IL": "Israel", "AE": "United Arab Emirates", "SA": "Saudi Arabia", "PH": "Philippines",
    "VN": "Vietnam", "TH": "Thailand", "ID": "Indonesia", "MY": "Malaysia", "MA": "Morocco",
    "TN": "Tunisia", "SN": "Senegal", "CI": "Ivory Coast", "RU": "Russia",
}

# WTTJ's `remote` enum -> (remote flag, workplace_type). workplace_type reuses the same vocabulary
# the Ashby normalizer emits, because the scorer's structured_onsite() already reads those words.
_WTTJ_REMOTE = {
    "fulltime": (True, "Remote"),
    "partial": (False, "Hybrid"),
    "punctual": (False, "Hybrid"),
    "no": (False, "Onsite"),
}

# salary_period -> multiplier to a yearly figure.
_WTTJ_PERIOD_MULT = {"yearly": 1, "monthly": 12, "weekly": 52, "daily": 260, "hourly": 2080}


def _wttj_location(row: dict) -> str | None:
    """Human location string, PREFERRING US offices when a req lists several.

    A role posted in both Paris and New York would otherwise render one string containing both
    "France" and "United States", and the screen reads a foreign mark as disqualifying — so a
    genuinely US-eligible req would be dropped. When any office is US, only the US ones are
    emitted; when none is, all of them are, so the screen still sees the foreign mark.
    """
    offices = [o for o in (row.get("offices") or []) if isinstance(o, dict)]
    if not offices and isinstance(row.get("office"), dict):
        offices = [row["office"]]
    us = [o for o in offices if (o.get("country_code") or "").strip().upper() == "US"]
    parts = []
    for office in (us or offices):
        city = (office.get("city") or "").strip()
        code = (office.get("country_code") or "").strip().upper()
        country = _WTTJ_COUNTRY_NAMES.get(code, code)
        piece = ", ".join(p for p in (city, country) if p)
        if piece and piece not in parts:
            parts.append(piece)
    return ", ".join(parts) or None


def _wttj_salary(row: dict) -> tuple[int | None, int | None, str | None]:
    """(min, max, currency) annualized. Non-yearly periods are scaled; junk is dropped."""
    mult = _WTTJ_PERIOD_MULT.get((row.get("salary_period") or "yearly").lower())
    if mult is None:
        return None, None, None
    out = []
    for key in ("salary_min", "salary_max"):
        value = row.get(key)
        out.append(int(value * mult) if isinstance(value, (int, float)) and value > 0 else None)
    low, high = out
    return low, high, (row.get("salary_currency") or None) if (low or high) else None


def normalize_wttj(org_slug: str, payload: dict, details: dict | None = None) -> list[dict]:
    """WTTJ per-organization job rows -> the normalized schema.

    `details` maps a job slug -> its detail payload's `job` object (fetched separately and capped).
    In the hourly loop it is ALWAYS empty — `fetch_wttj_company` is list-only — because
    descriptions are bought after scoring, per row, by `hydrate_wttj_descriptions` below. The
    parameter stays for a caller that already holds detail payloads; nothing in the cycle is one.
    """
    jobs = []
    for row in (payload or {}).get("data", []):
        if not isinstance(row, dict):
            continue
        if (row.get("status") or "published") != "published":
            continue          # archived/unpublished reqs are still served; they are not openings
        slug = row.get("slug") or ""
        org = row.get("organization") or {}
        org_slug_actual = org.get("slug") or org_slug
        remote, workplace = _WTTJ_REMOTE.get((row.get("remote") or "").lower(), (None, None))
        low, high, currency = _wttj_salary(row)
        detail = (details or {}).get(slug) or {}
        description = html_to_text(detail.get("description", "")) if detail else ""
        jobs.append(_job(
            source="welcometothejungle",
            company=org.get("name") or org_slug,
            title=row.get("name"),
            location=_wttj_location(row),
            remote=remote,
            workplace_type=workplace,
            url=(f"https://www.welcometothejungle.com/en/companies/"
                 f"{urllib.parse.quote(org_slug_actual)}/jobs/{urllib.parse.quote(slug)}"
                 if slug else None),
            posted_at=row.get("published_at") or row.get("updated_at"),
            comp_min=low, comp_max=high, comp_currency=currency,
            employment_type=row.get("contract_type"),
            external_id=str(row.get("reference") or slug),
            description=description,
            # The employer's own req this posting mirrors. Only present once hydrated, and it is
            # what lets dedupe() prove two rows are one job rather than guessing from the title.
            apply_url=detail.get("apply_url") or None,
        ))
    return jobs


WTTJ_API = "https://api.welcometothejungle.com/api/v3/organizations"


def fetch_wttj_company(slug: str, warnings: list[str], max_pages: int,
                       max_per_company: int) -> list[dict]:
    """List-only fetch for one WTTJ organization. Cheap by design: one request per 30 postings."""
    quoted = urllib.parse.quote(slug)
    collected: list[dict] = []
    page_count = 1
    for page in range(1, max_pages + 1):
        payload = _get_json(f"{WTTJ_API}/{quoted}/jobs?page={page}", warnings,
                            f"welcometothejungle:{slug}:p{page}")
        if not payload:
            break
        rows = payload.get("data") or []
        collected.extend(normalize_wttj(slug, payload))
        page_count = int((payload.get("metadata") or {}).get("page_count") or 1)
        if page >= page_count or len(rows) < 30 or len(collected) >= max_per_company:
            break
    return collected[:max_per_company]


def hydrate_wttj_descriptions(jobs: list[dict], warnings: list[str], limit: int = 25) -> int:
    """Fill in descriptions for WTTJ jobs that lack one — LAZILY, after scoring.

    Fetching a description for every posting on every cycle is ~25 requests per company per hour,
    which across a watchlist of a hundred-odd companies is >100k requests/day at one free public API.
    Indefensible, and it would get the honest User-Agent blocked. The list rows already carry title,
    structured comp, location and the remote flag — everything the scorer's reproducible floor needs
    — so the description is only worth paying for once a job has earned attention.

    Pass the jobs you actually care about (a scored shortlist). Mutates them in place; returns how
    many were hydrated. Per-job failure is a warning, never fatal, and leaves the row's description
    EMPTY — never a placeholder, so a req that could not be fetched stays visibly text-less.

    **THE CALLER'S LIST IS THE REQUEST BUDGET, NOT `limit`.** `limit` counts SUCCESSES: a detail
    call that fails, or answers with no prose, does not increment it and the walk continues. So on
    a large text-less pool `limit` bounds nothing on its own. The one caller —
    `hunt_cycle.hydrate_and_rescore`, via `hunt_cycle.hydration_shortlist` — hands in an
    already-capped list, which is what makes the per-cycle bound provable.
    """
    hydrated = 0
    for job in jobs:
        if hydrated >= limit:
            break
        if job.get("source") != "welcometothejungle" or job.get("description"):
            continue
        url = job.get("url") or ""
        match = re.search(r"/companies/([^/]+)/jobs/([^/?#]+)", url)
        if not match:
            continue
        org, slug = match.group(1), match.group(2)
        payload = _get_json(f"{WTTJ_API}/{org}/jobs/{slug}", warnings,
                            f"welcometothejungle:detail:{slug[:40]}")
        detail = (payload or {}).get("job") or {}
        if not detail:
            continue
        text = html_to_text(detail.get("description", ""))
        for extra in ("key_missions", "looking_for_candidate_description", "profile"):
            value = detail.get(extra)
            if isinstance(value, str) and value.strip():
                text = f"{text}\n{html_to_text(value)}"
        if text.strip():
            job["description"] = text.strip()[:MAX_DESC_CHARS]
            hydrated += 1
        if detail.get("apply_url"):
            job["apply_url"] = detail["apply_url"]
        # The list row's comp is authoritative, but hydrate it if the list left it blank.
        if job.get("comp_min") is None and job.get("comp_max") is None:
            low, high, currency = _wttj_salary(detail)
            if low or high:
                job["comp_min"], job["comp_max"], job["comp_currency"] = low, high, currency
    return hydrated


def _ms_to_iso(ms):
    if not ms:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(ms) / 1000))
    except (ValueError, TypeError, OSError):
        return None


def _epoch_to_iso(seconds):
    if not seconds:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(seconds)))
    except (ValueError, TypeError, OSError):
        return None


# ---------------------------------------------------------------------------------- fetch plan

def fetch_all(watchlist: dict, warnings: list[str], max_per_company: int) -> list[dict]:
    jobs: list[dict] = []

    for slug in watchlist.get("greenhouse", []):
        payload = _get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(slug)}/jobs?content=true",
            warnings, f"greenhouse:{slug}")
        if payload:
            jobs.extend(normalize_greenhouse(slug, payload)[:max_per_company])

    for slug in watchlist.get("lever", []):
        payload = _get_json(
            f"https://api.lever.co/v0/postings/{urllib.parse.quote(slug)}?mode=json",
            warnings, f"lever:{slug}")
        if payload:
            jobs.extend(normalize_lever(slug, payload)[:max_per_company])

    for slug in watchlist.get("ashby", []):
        payload = _get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(slug)}?includeCompensation=true",
            warnings, f"ashby:{slug}")
        if payload:
            jobs.extend(normalize_ashby(slug, payload)[:max_per_company])

    remotive = watchlist.get("remotive")
    if remotive:
        query = urllib.parse.urlencode({"search": remotive.get("search", ""), "limit": remotive.get("limit", 100)})
        payload = _get_json(f"https://remotive.com/api/remote-jobs?{query}", warnings, "remotive")
        if payload:
            jobs.extend(normalize_remotive(payload))

    arbeitnow = watchlist.get("arbeitnow")
    if arbeitnow:
        for page in range(1, int(arbeitnow.get("pages", 1)) + 1):
            payload = _get_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}", warnings, "arbeitnow")
            if payload:
                jobs.extend(normalize_arbeitnow(payload))

    smartrecruiters = watchlist.get("smartrecruiters")
    if smartrecruiters:
        slugs = smartrecruiters if isinstance(smartrecruiters, list) else smartrecruiters.get("companies", [])
        detail_cap = 25 if isinstance(smartrecruiters, list) else int(smartrecruiters.get("detail_cap", 25))
        for slug in slugs:
            payload = _get_json(
                f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(slug)}/postings?limit=100",
                warnings, f"smartrecruiters:{slug}")
            if payload:
                details = {}
                for row in (payload.get("content") or [])[:detail_cap]:
                    posting_id = str(row.get("id", ""))
                    detail = _get_json(
                        f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(slug)}/postings/{posting_id}",
                        warnings, f"smartrecruiters:{slug}:{posting_id}")
                    if detail:
                        details[posting_id] = detail
                jobs.extend(normalize_smartrecruiters(slug, payload, details)[:max_per_company])

    if watchlist.get("remoteok"):
        payload = _get_json("https://remoteok.com/api", warnings, "remoteok")
        if payload:
            jobs.extend(normalize_remoteok(payload))

    hn = watchlist.get("hn_hiring")
    if hn:
        stories = _get_json(
            "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring"
            "&query=%22who%20is%20hiring%22&hitsPerPage=6", warnings, "hn_hiring:story-search")
        story_id = None
        for hit in (stories or {}).get("hits", []):
            if (hit.get("title") or "").lower().startswith("ask hn: who is hiring"):
                story_id = hit.get("objectID")
                break
        if story_id:
            for page in range(int(hn.get("pages", 2))):
                payload = _get_json(
                    f"https://hn.algolia.com/api/v1/search_by_date?tags=comment,story_{story_id}"
                    f"&hitsPerPage=100&page={page}", warnings, f"hn_hiring:comments:p{page}")
                if payload:
                    jobs.extend(normalize_hn_hiring(payload.get("hits", []), story_id))
        else:
            warnings.append("hn_hiring: could not locate the current 'Ask HN: Who is hiring?' story")

    for entry in watchlist.get("workday", []):
        tenant, host, site = entry.get("tenant"), entry.get("host", "wd1"), entry.get("site")
        if not tenant or not site:
            warnings.append(f"workday: entry missing tenant/site: {entry}")
            continue
        base = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
        label = f"workday:{tenant}"
        listings = []
        for page in range(int(entry.get("pages", 3))):
            payload = _get_json(f"{base}/jobs", warnings, f"{label}:p{page}", post_body={
                "appliedFacets": {}, "limit": 20, "offset": page * 20,
                "searchText": entry.get("search", "")})
            rows = (payload or {}).get("jobPostings") or []
            listings.extend(rows)
            if len(rows) < 20:
                break
        details = {}
        for row in listings[:int(entry.get("detail_cap", 15))]:
            path = row.get("externalPath") or ""
            if not path:
                continue
            detail = _get_json(f"{base}{path}", warnings, f"{label}:{path[-40:]}")
            if detail:
                details[path] = detail
        jobs.extend(normalize_workday(tenant, base, {"jobPostings": listings}, details)[:max_per_company])

    rippling = watchlist.get("rippling")
    if rippling:
        slugs = rippling if isinstance(rippling, list) else rippling.get("companies", [])
        detail_cap = 25 if isinstance(rippling, list) else int(rippling.get("detail_cap", 25))
        for slug in slugs:
            base = f"https://api.rippling.com/platform/api/ats/v1/board/{urllib.parse.quote(slug)}/jobs"
            payload = _get_json(base, warnings, f"rippling:{slug}")
            listing = payload if isinstance(payload, list) else []
            details = {}
            for row in listing[:detail_cap]:
                uuid = str(row.get("uuid", ""))
                if not uuid:
                    continue
                detail = _get_json(f"{base}/{uuid}", warnings, f"rippling:{slug}:{uuid[:12]}")
                if detail:
                    details[uuid] = detail
            jobs.extend(normalize_rippling(slug, listing, details)[:max_per_company])

    adzuna = watchlist.get("adzuna")
    if adzuna:
        app_id = os.environ.get("ADZUNA_APP_ID", "")
        app_key = os.environ.get("ADZUNA_APP_KEY", "")
        if app_id and app_key:
            query = urllib.parse.urlencode({
                "app_id": app_id, "app_key": app_key,
                "what": adzuna.get("what", ""), "results_per_page": adzuna.get("results_per_page", 50),
                "content-type": "application/json",
            })
            country = adzuna.get("country", "us")
            payload = _get_json(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1?{query}", warnings, "adzuna")
            if payload:
                jobs.extend(normalize_adzuna(payload))
        else:
            warnings.append("adzuna: configured in watchlist but ADZUNA_APP_ID/ADZUNA_APP_KEY not set — skipped")

    wttj = watchlist.get("welcometothejungle")
    if wttj:
        companies = wttj if isinstance(wttj, list) else wttj.get("companies", [])
        max_pages = 3 if isinstance(wttj, list) else int(wttj.get("max_pages", 3))
        for slug in companies:
            jobs.extend(fetch_wttj_company(slug, warnings, max_pages, max_per_company))

    # Before dedupe, deliberately: a corrected employer is part of the role key two lines down, so
    # a republished row can finally fold into the direct board's row for the same job.
    corrected = attribute_employers(jobs)
    if corrected:
        warnings.append(f"employer attribution: corrected {corrected} posting(s) from their apply URL's "
                        "ATS org — each keeps the feed's name in company_reported")

    return dedupe(jobs)


TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROTEUS_DIR = os.path.dirname(TOOLS_DIR)

sys.path.insert(0, TOOLS_DIR)
import proteus_paths  # noqa: E402  (canonical state/ vs out/ paths)


def attribute_employers(jobs: list[dict]) -> int:
    """Correct a feed-reported employer from the ATS org its own apply URL names. Returns the count.

    **The bug.** A posting can pass through several hands before it reaches Proteus — employer
    careers page → a job-curation board → a republisher → an open aggregator → here — and each one
    can put its own name in the employer field. The curator's name is what gets recorded, or one layer
    of laundering further along, a staffing agency's. Meanwhile the req itself lives at
    ``jobs.ashbyhq.com/<employer>/…``.

    **The evidence, and only the evidence.** An ATS org slug is published under the employer's own
    account on a system nobody else can post to, so it is the one attribution in the chain that a
    republisher cannot restate. If the apply URL is not on a recognised ATS, ``ats_hosts`` returns
    None and **this function changes nothing** — no name-matching, no curator/staffing-agency
    list, no inference from the employer field itself. Declining is the common outcome and the
    correct one; a wrong employer written confidently is worse than the feed's wrong employer,
    because it looks derived.

    **Marked, not laundered** (the same rule as the scorer's ``source-risk`` flag). A corrected row
    keeps what the feed claimed:

      ``company``           the derived employer — what the owner sees and what dedup keys on
      ``company_reported``  verbatim what the feed said, never dropped
      ``company_source``    ``"apply-url"`` — the marker every reader keys off
      ``apply_ats`` / ``apply_org``   the system and org slug the correction came from

    ``score_jobs`` turns those into a visible flag, so the disagreement reaches the owner rather than
    being quietly resolved on their behalf.

    Three deliberate scoping calls:

    * **Direct boards are never touched** (tier S). Their ``company`` comes from the employer's own
      board and their ``url`` *is* the req, so there is nothing to correct and a slug-vs-name
      mismatch ("ExampleCorp PBC" vs ``examplecorp``) could only make a right answer worse.
    * **``apply_url`` is preferred over ``url``** as the evidence, falling back to ``url`` so an
      aggregator row that links straight at an ATS is caught too.
    * **It runs before ``dedupe``**, so the corrected name participates in ``_role_key``. That is a
      second win falling out of the first: a repost that used to carry the curator's name could never
      fold into the direct row for the same req, so one job appeared twice under two employers. Now
      it folds.
    """
    # An exact name already in hand beats a slug-derived one: a direct board that Proteus watches
    # writes "ExampleCorp", where ``display_name`` can only manage "Examplecorp". Built from tier-S
    # rows only, and from a plain dict, so the result never depends on fetch order.
    exact: dict[str, str] = {}
    for job in jobs:
        company = (job.get("company") or "").strip()
        if company and tier_rank(job.get("source")) >= 3:
            exact.setdefault(ats_hosts.normalize_name(company), company)

    corrected = 0
    for job in jobs:
        if tier_rank(job.get("source")) >= 3:
            continue
        hit = ats_hosts.employer_org(job.get("apply_url") or "") \
            or ats_hosts.employer_org(job.get("url") or "")
        if not hit:
            continue
        ats, org = hit
        reported = (job.get("company") or "").strip()
        if reported and ats_hosts.names_agree(reported, org):
            continue          # the feed already had it right — the overwhelmingly common case
        job["company"] = exact.get(ats_hosts.normalize_name(org)) or ats_hosts.display_name(org)
        job["company_reported"] = reported or None
        job["company_source"] = "apply-url"
        job["apply_ats"] = ats
        job["apply_org"] = org
        corrected += 1
    return corrected


def _role_key(job: dict) -> tuple[str, str] | None:
    """A cross-source identity for the SAME role: normalized (company, title). None if either is blank."""
    company = re.sub(r"\s+", " ", (job.get("company") or "").strip().lower())
    title = re.sub(r"\s+", " ", (job.get("title") or "").strip().lower())
    return (company, title) if company and title else None


def _fold_into(survivor: dict, job: dict) -> None:
    """Record `job` as another source of `survivor`, and let it fill blanks the survivor left.

    Attribution first: the folded source is credited on ``also_sources`` so the row still shows
    which feeds surfaced it. Then **enrichment**, which only ever fills a None — a direct board's
    own fields are never overwritten by a mirror's copy of them.
    """
    also = set(survivor.get("also_sources") or [])
    if job.get("source"):
        also.add(job["source"])
    survivor["also_sources"] = sorted(also)

    # Comp is the field this is FOR. Greenhouse/Ashby bury pay ranges in JD prose, which
    # comp_from_text catches only sometimes; WTTJ carries salary_min/salary_max as numbers on most
    # rows. A blank comp is not neutral to the scorer — it scores a free 8/15 "unknown", so a role
    # that hides its pay can outscore one that discloses honestly. Filling it in from the mirror
    # makes that job score on what it actually pays.
    if survivor.get("comp_min") is None and survivor.get("comp_max") is None:
        if job.get("comp_min") is not None or job.get("comp_max") is not None:
            survivor["comp_min"] = job.get("comp_min")
            survivor["comp_max"] = job.get("comp_max")
            survivor["comp_currency"] = survivor.get("comp_currency") or job.get("comp_currency")
            survivor["comp_note"] = survivor.get("comp_note") or job.get("comp_note")
            survivor["comp_source"] = job.get("source")
    for field in ("workplace_type", "employment_type", "posted_at"):
        if not survivor.get(field) and job.get(field):
            survivor[field] = job[field]
    if survivor.get("remote") is None and job.get("remote") is not None:
        survivor["remote"] = job["remote"]


def dedupe(jobs: list[dict]) -> list[dict]:
    """De-duplicate in three passes.

    1. **Exact** — drop a posting already seen at the same URL (or same ``source:external_id``).
    2. **Mirror** — a source that republishes a specific employer req verbatim
       (``source_tiers.MIRROR_SOURCES``; today welcometothejungle) is not an independent listing,
       so leaving it beside the direct row would show one job twice. Fold it into the direct row,
       and let it ENRICH that row's blanks — see ``_fold_into``, and note this is the one fold that
       gives something back rather than only dropping a duplicate.
    3. **Cross-posting** — an aggregator (tier-B: HN/Adzuna and other open aggregation) repost of a
       role already carried by a direct/curated board (tier S/A) is the biggest noise vector. Drop it,
       but record its source on the surviving board row's ``also_sources`` so attribution still
       credits it with surfacing the role. Multiple aggregator reposts of one role collapse to one.
       Direct/curated rows are never merged with each other — two same-title reqs on one board are
       distinct jobs, kept apart.

    A mirror with no direct row behind it **survives on its own** — that is the whole point of
    watching companies whose own ATS can't be polled directly.
    """
    seen: set[str] = set()
    unique: list[dict] = []
    for job in jobs:
        key = (job.get("url") or f"{job.get('source')}:{job.get('external_id')}").strip().lower()
        if key and key in seen:
            continue
        seen.add(key)
        unique.append(job)

    # Direct (tier-S) rows are what a mirror folds into. Deliberately NOT tier A: folding one
    # aggregator into another would make the outcome depend on fetch order.
    direct_by_role: dict[tuple[str, str], dict] = {}
    for job in unique:
        if tier_rank(job.get("source")) >= 3 and job.get("source") not in MIRROR_SOURCES:
            rk = _role_key(job)
            if rk and rk not in direct_by_role:
                direct_by_role[rk] = job

    after_mirror: list[dict] = []
    seen_mirror_roles: set[tuple[str, str]] = set()
    for job in unique:
        if job.get("source") in MIRROR_SOURCES:
            rk = _role_key(job)
            survivor = direct_by_role.get(rk) if rk else None
            if survivor is not None:
                _fold_into(survivor, job)
                continue
            if rk and rk in seen_mirror_roles:   # the same req on two WTTJ company profiles
                continue
            if rk:
                seen_mirror_roles.add(rk)
        after_mirror.append(job)

    # First direct/curated (tier S/A) row per role — the survivor an aggregator repost folds into.
    sa_by_role: dict[tuple[str, str], dict] = {}
    for job in after_mirror:
        if tier_rank(job.get("source")) >= 2:      # S or A
            rk = _role_key(job)
            if rk and rk not in sa_by_role:
                sa_by_role[rk] = job

    out: list[dict] = []
    seen_b_roles: set[tuple[str, str]] = set()
    for job in after_mirror:
        rk = _role_key(job)
        if rk and tier_rank(job.get("source")) <= 1:   # tier B (aggregator)
            survivor = sa_by_role.get(rk)
            if survivor is not None:                    # a direct/curated board already carries this role
                also = set(survivor.get("also_sources") or [])
                if job.get("source"):
                    also.add(job["source"])
                survivor["also_sources"] = sorted(also)
                continue
            if rk in seen_b_roles:                      # a second aggregator repost of the same role
                continue
            seen_b_roles.add(rk)
        out.append(job)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fetch + normalize job postings (Proteus phase 1).")
    parser.add_argument("--watchlist", required=True, help="path to watchlist.json")
    parser.add_argument("--out", required=True, help="path to write the normalized jobs JSON")
    parser.add_argument("--max-per-company", type=int, default=100)
    args = parser.parse_args(argv)

    with open(args.watchlist, encoding="utf-8") as fh:
        watchlist = json.load(fh)

    warnings: list[str] = []
    jobs = fetch_all(watchlist, warnings, args.max_per_company)
    document = {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "count": len(jobs),
        "warnings": warnings,
        "jobs": jobs,
    }
    # A raw board aimed at out/ lands in state/runs/ instead — it's machinery, not a deliverable.
    # score_jobs applies the same redirect to --jobs, so the charter's Phase-1 chain still resolves.
    out_path = proteus_paths.resolve_raw_artifact(args.out)
    redirected = str(out_path) != str(args.out)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=1)
    result = {"ok": True, "count": len(jobs), "warnings": warnings, "out": str(out_path)}
    if redirected:
        result["note"] = (f"raw board written to state/ (not {args.out}) — it's runtime churn, not a "
                          "deliverable; out/ is for what a run produces for the owner")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
