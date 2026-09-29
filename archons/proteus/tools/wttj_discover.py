#!/usr/bin/env python3
"""Proteus tool: discover which companies are worth watching on Welcome to the Jungle (stdlib only).

**Why this exists.** WTTJ's per-company API (``/api/v3/organizations/<slug>/jobs``) is open and
generous — structured salary, structured office country — but it only answers about companies you
can already name. Its board-wide search endpoint (``/api/v3/search/jobs``) refuses every
unauthenticated client, and so does ``/api/v3/jobs-matches/counts``; those are the two *personalised*
endpoints, and holding both cookies the API itself mints plus the two headers its own front end
sends does not lift it. The gate is an account session, so board-wide search is simply not
available to a stdlib client (full write-up in ``fetch_jobs.py``'s docstring).

So discovery comes from the front door instead: WTTJ **publishes sitemaps** of every job listing,
gzipped, at stable paths, with no query strings — and its ``robots.txt`` allows them while
disallowing ``/*?``. This walks those shards, reads each listing URL's company slug and trailing
city slug, and reports which companies are actually posting into US metros and how often.

That is the whole trick: the sitemap says *who is hiring where*, and the open per-company API then
says *what, for how much*. Together they cover what the walled search endpoint would have given us,
using only what WTTJ publishes for crawling.

**This is not part of a fetch cycle.** It is an occasional curation tool: run it, look at the
ranking, and promote the companies worth having into ``watchlist.json``'s ``welcometothejungle``
block by hand. Nine shards is ~23 MB of gzipped XML, which is not something to do hourly.

Run:  python wttj_discover.py [--shards 9] [--min-us 5] [--probe] [--out state/wttj-discovery.json]

  --probe  additionally calls the per-company API for each candidate to confirm the slug resolves
           and report its open-req count, US density and salary coverage. One request per company —
           polite, but it is a request per company, so it is off by default.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS_DIR)
import proteus_paths  # noqa: E402

USER_AGENT = "proteus-job-fetch/1.0 (personal job search; stdlib urllib)"
TIMEOUT = 45
SITE = "https://www.welcometothejungle.com"
API = "https://api.welcometothejungle.com/api/v3/organizations"
SITEMAP = SITE + "/sitemaps/job-listings.{shard}.xml.gz"

# Trailing city slugs that mean "this req is in the US". Deliberately a curated allow-list rather
# than a foreign deny-list: the board is mostly French, so guessing wrong in the permissive direction
# would flood the watchlist with Paris retail. Under-counting is the safe error here — a company
# that clears the bar on a partial list is a company worth watching either way.
US_CITY_SLUGS = frozenset("""
new-york san-francisco austin seattle boston chicago denver los-angeles atlanta dallas houston
miami washington st-louis saint-louis philadelphia phoenix portland san-diego san-jose minneapolis
detroit nashville charlotte raleigh pittsburgh salt-lake-city columbus kansas-city indianapolis
orlando tampa brooklyn palo-alto mountain-view sunnyvale santa-clara redmond cambridge arlington
boulder irvine san-mateo bellevue menlo-park santa-monica el-segundo culver-city pasadena
mountain-view-ca sunnyvale-ca bethesda reston mclean herndon huntsville colorado-springs
ann-arbor madison boise reno las-vegas sacramento oakland berkeley fremont san-bruno
us remote-us usa united-states
""".split())

_LISTING = re.compile(r"/companies/([^/]+)/jobs/([^/?#<]+)")


def _get(url: str, warnings: list[str], label: str) -> bytes | None:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            blob = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                blob = gzip.GzipFile(fileobj=io.BytesIO(blob)).read()
            return blob
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        warnings.append(f"{label}: {error}")
        return None


def is_us_listing(job_slug: str) -> bool:
    """WTTJ listing slugs are ``<role-words>_<city>[_<opaque-id>]``. True when the city is US.

    The role segment is skipped deliberately: a role called "us-partnerships" must not read as a
    US location.
    """
    segments = job_slug.split("_")[1:]
    return any(segment.lower() in US_CITY_SLUGS for segment in segments)


def harvest(shards: int, warnings: list[str]) -> tuple[collections.Counter, collections.Counter, int]:
    """Walk the listing sitemaps. Returns (us_rows_per_company, all_rows_per_company, total_urls)."""
    us_rows: collections.Counter = collections.Counter()
    all_rows: collections.Counter = collections.Counter()
    total = 0
    for shard in range(shards):
        blob = _get(SITEMAP.format(shard=shard), warnings, f"sitemap:{shard}")
        if not blob:
            continue
        if blob[:2] == b"\x1f\x8b":            # some CDNs hand back the .gz un-decoded
            try:
                blob = gzip.GzipFile(fileobj=io.BytesIO(blob)).read()
            except OSError as error:
                warnings.append(f"sitemap:{shard}: could not decompress ({error})")
                continue
        text = blob.decode("utf-8", errors="replace")
        for url in re.findall(r"<loc>([^<]+)</loc>", text):
            match = _LISTING.search(url)
            if not match:
                continue
            total += 1
            company, job_slug = match.group(1), match.group(2)
            all_rows[company] += 1
            if is_us_listing(job_slug):
                us_rows[company] += 1
    return us_rows, all_rows, total


def probe_company(slug: str, warnings: list[str]) -> dict:
    """One list call: does the slug resolve, and what does its first page look like?"""
    request = urllib.request.Request(
        f"{API}/{urllib.parse.quote(slug)}/jobs",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                 "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            blob = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                blob = gzip.GzipFile(fileobj=io.BytesIO(blob)).read()
            payload = json.loads(blob.decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        warnings.append(f"probe:{slug}: {error}")
        return {"resolves": False}
    rows = payload.get("data") or []
    us = sum(1 for row in rows
             if any((office or {}).get("country_code", "").upper() == "US"
                    for office in (row.get("offices") or [row.get("office") or {}])))
    return {
        "resolves": True,
        "name": ((rows[0].get("organization") or {}).get("name") if rows else None),
        "open_reqs": (payload.get("metadata") or {}).get("total"),
        "first_page": len(rows),
        "us_first_page": us,
        "salary_first_page": sum(1 for row in rows if row.get("salary_min") or row.get("salary_max")),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Discover WTTJ companies worth watching.")
    parser.add_argument("--shards", type=int, default=9,
                        help="how many job-listings sitemap shards to walk (default: all 9)")
    parser.add_argument("--min-us", type=int, default=5,
                        help="only report companies with at least this many US-metro listings")
    parser.add_argument("--probe", action="store_true",
                        help="confirm each candidate against the per-company API (1 request each)")
    parser.add_argument("--probe-limit", type=int, default=200)
    parser.add_argument("--out", default=None, help="where to write the JSON report")
    args = parser.parse_args(argv)

    warnings: list[str] = []
    us_rows, all_rows, total = harvest(args.shards, warnings)
    candidates = [(slug, count) for slug, count in us_rows.most_common()
                  if count >= args.min_us]

    companies = []
    for index, (slug, us_count) in enumerate(candidates):
        entry = {"slug": slug, "sitemap_us_rows": us_count,
                 "sitemap_total_rows": all_rows[slug]}
        if args.probe and index < args.probe_limit:
            entry.update(probe_company(slug, warnings))
            time.sleep(0.12)          # be a good citizen on a free public API
        companies.append(entry)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "shards_walked": args.shards,
        "listings_seen": total,
        "companies_with_us_listings": len(us_rows),
        "candidates": len(companies),
        "warnings": warnings,
        "companies": companies,
    }
    out_path = args.out or str(proteus_paths.WTTJ_DISCOVERY_FILE)
    proteus_paths.write_json(out_path, report, indent=1)

    print(json.dumps({"ok": True, "listings_seen": total,
                      "companies_with_us_listings": len(us_rows),
                      "candidates": len(companies), "out": out_path,
                      "warnings": warnings[:5]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
