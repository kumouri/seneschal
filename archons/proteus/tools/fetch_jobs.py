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
  - Adzuna      api.adzuna.com (aggregator; OPTIONAL — needs ADZUNA_APP_ID/ADZUNA_APP_KEY env)

Reads a watchlist JSON (see watchlist.json next to this tool), emits one normalized JSON document:
  {"fetched_at", "count", "warnings": [...], "jobs": [{source, company, title, location, remote,
   url, posted_at, comp_min, comp_max, comp_currency, comp_note, employment_type, external_id,
   description}]}

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

USER_AGENT = "proteus-job-fetch/1.0 (personal job search; stdlib urllib)"
TIMEOUT = 25
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
    """Best-effort HTML → plain text (stdlib), collapsed whitespace, block-aware newlines."""
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
    return text.strip()[:MAX_DESC_CHARS]


def _get_json(url: str, warnings: list[str], label: str, post_body: dict | None = None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
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
        "url": None, "posted_at": None, "comp_min": None, "comp_max": None,
        "comp_currency": None, "comp_note": None, "employment_type": None,
        "external_id": None, "description": "",
    }
    base.update(fields)
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
            description=(row.get("descriptionPlain") or html_to_text(row.get("description", "")))[:MAX_DESC_CHARS],
        ))
    return jobs


def normalize_ashby(company: str, payload: dict) -> list[dict]:
    jobs = []
    for row in (payload or {}).get("jobs", []):
        compensation = row.get("compensation") or {}
        jobs.append(_job(
            source="ashby", company=company,
            title=row.get("title"),
            location=row.get("location"),
            remote=row.get("isRemote"),
            url=row.get("jobUrl") or row.get("applyUrl"),
            posted_at=row.get("publishedAt"),
            comp_note=compensation.get("compensationTierSummary"),
            employment_type=row.get("employmentType"),
            external_id=str(row.get("id", "")),
            description=(row.get("descriptionPlain") or html_to_text(row.get("descriptionHtml", "")))[:MAX_DESC_CHARS],
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


def normalize_hn_hiring(comments: list[dict], story_id: str) -> list[dict]:
    """Top-level comments of the monthly 'Ask HN: Who is hiring?' thread, one job post each.

    Convention: first line reads 'Company | Role | Location | Comp | REMOTE'; kept as-is for the
    title with the company split out. The whole comment is the description the scorer reads.
    """
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
        jobs.append(_job(
            source="hn_hiring", company=company,
            title=first_line or f"HN hiring post by {row.get('author')}",
            remote=True if re.search(r"\bremote\b", text, re.I) else None,
            url=f"https://news.ycombinator.com/item?id={comment_id}",
            posted_at=(row.get("created_at") or "")[:19] or None,
            external_id=str(comment_id),
            description=text,
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

    return dedupe(jobs)


def dedupe(jobs: list[dict]) -> list[dict]:
    """Drop duplicates (same URL, or same source+external_id)."""
    seen: set[str] = set()
    unique = []
    for job in jobs:
        key = (job.get("url") or f"{job.get('source')}:{job.get('external_id')}").strip().lower()
        if key and key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


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
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=1)
    print(json.dumps({"ok": True, "count": len(jobs), "warnings": warnings, "out": args.out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
