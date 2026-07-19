#!/usr/bin/env python3
"""Source reliability tiers — a single source of truth shared by the fetch layer (cross-posting
dedup), the scorer, and any UI that shows source badges.

Ranks each job source by **directness** and **ghost/recruiter density** (a standing tier decision):

- **S — direct-employer ATS boards.** Every posting is a real req at that company. Near-zero ghosts.
- **A — curated aggregators.** Mostly real, some staleness/reposts.
- **B — open aggregation / freeform.** Highest recruiter/ghost/repost density (open aggregators
  re-index LinkedIn/Indeed/Glassdoor/ZipRecruiter; HN is unstructured). Useful for reach, needs the
  most skepticism.

An unknown source is treated as tier **A** (the neutral middle) so a new source is never silently
trusted like a direct board nor punished like an aggregator until it's classified here.
"""
from __future__ import annotations

SOURCE_TIERS: dict[str, str] = {
    # S — direct-employer ATS boards
    "ashby": "S", "greenhouse": "S", "lever": "S",
    "smartrecruiters": "S", "workday": "S", "rippling": "S",
    # A — curated aggregators
    "remotive": "A", "remoteok": "A", "arbeitnow": "A",
    # B — open aggregation / freeform
    "jsearch": "B", "hn_hiring": "B", "adzuna": "B",
}

_TIER_RANK = {"S": 3, "A": 2, "B": 1}

# Short labels for a UI badge (kept here so any UI and the logic can't drift).
TIER_LABEL = {"S": "direct", "A": "aggregator", "B": "open-aggregation"}


def tier_of(source: str | None) -> str:
    """The S/A/B tier for a source string; unknown → 'A' (neutral middle)."""
    return SOURCE_TIERS.get((source or "").strip().lower(), "A")


def tier_rank(source: str | None) -> int:
    """Higher = more trustworthy/direct. S=3, A=2 (also the unknown default), B=1."""
    return _TIER_RANK[tier_of(source)]
