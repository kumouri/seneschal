#!/usr/bin/env python3
"""Per-source judgment about a posting — a single source of truth shared by the fetch layer
(cross-posting dedup), the scorer (ghost-risk down-rank + flag), and any UI that shows source badges.

Two *separate* graded axes, because they answer different questions and one source splits them.

**Directness** (`tier_of`/`tier_rank`, a standing tier decision) — how structured and first-hand is
the feed? This is what cross-post dedup wants: when the same req arrives twice, keep the copy with
the better fields.

- **S — direct-employer ATS boards.** Structured req straight off the company's own board.
- **A — curated aggregators.** Mostly real, some staleness/reposts.
- **B — open aggregation / freeform.** Open aggregators re-index LinkedIn/Indeed/Glassdoor/
  ZipRecruiter; HN's whoishiring threads are unstructured prose. Useful for reach, thinnest fields.

**Ghost risk** (`ghost_risk`) — is this posting *the* record, or somebody's copy of one? That is the
question the scorer needs, and directness answers it wrongly for exactly one source: `hn_hiring` is
tier B because the text is unstructured, but every HN line is typed by the hiring employer, which is
the *opposite* of ghosty. Reusing the S/A/B rank as a ghost proxy would punish HN for the open
aggregators' sins, so the axes stay apart.

- **low — the posting is the canonical record.** Direct ATS boards, and HN. Low risk is *not* "never
  dead" — a direct-board req can be delisted like any other. It means **dead is cheap to prove**:
  the employer's own board/API answers in one request, so the check always terminates.
- **medium — a curated mirror.** Real postings copied from somewhere else, with a human filter in
  between. No measured evidence either way, so it is the neutral middle and carries no penalty.
- **high — open aggregation.** A mirror with no curation and no guaranteed way back to the original:
  most such postings link to a third-party domain rather than the employer's own. Two distinct
  failure modes live here — a stale copy of a req the employer already closed, and a blinded
  recruiter listing fronting for an unnamed employer, which is not merely expensive to verify but
  *unverifiable in principle*.

**Mirroring** (`MIRROR_SOURCES`) — a third, *narrower* question, asked only by dedup: is this row a
faithful copy of a specific req that also exists on a direct board? Directness can't answer it,
because it grades the feed and this grades the individual posting's relationship to another one.
`welcometothejungle` is the case: employer-maintained profiles syndicated out of the employer's own
ATS, whose `apply_url` points back at it. Left alone, every overlapping req would appear twice on the
board; folded, it collapses into the direct row **and fills in that row's blanks** — which is why
this is the one fold that gives something back rather than only dropping a duplicate
(`fetch_jobs.dedupe` / `_fold_into`).

**Read every `apply_url` claim in this file as design intent, not as a description of the data.** For
WTTJ the field is **detail-only** — the list rows the hourly loop reads carry no such key — so it is
present only on rows `hunt_cycle.hydrate_and_rescore` has bought a detail call for. A WTTJ
`apply_url` is also not always a bare ATS URL: an employer's own careers page can proxy its ATS
(`<employer>.com/careers/job?…&gh_jid=…`). Still the employer's, still one request to settle — so
the 'low' ghost-risk reasoning below survives; only its confidence about the field being *present*
does not.

An unknown source is the neutral middle on **both** graded axes (tier `A`, risk `medium`) and is not
a mirror, so a new source is never silently trusted like a direct board, never silently punished
like an aggregator, and never silently merged into somebody else's row until it's classified here.
"""
from __future__ import annotations

SOURCE_TIERS: dict[str, str] = {
    # S — direct-employer ATS boards
    "ashby": "S", "greenhouse": "S", "lever": "S",
    "smartrecruiters": "S", "workday": "S", "rippling": "S",
    # A — curated aggregators
    "remotive": "A", "remoteok": "A", "arbeitnow": "A",
    # …and welcometothejungle, which is a curated MIRROR: employer-maintained profiles whose
    # postings are syndicated out of the employer's own ATS (a WTTJ job's apply_url points back at
    # the employer's Greenhouse/Lever/… req, and the payload even names the source ATS).
    # Structurally that is an aggregator — the posting is a copy — so it is tier A, not S. See
    # MIRROR_SOURCES below for why that distinction has teeth at dedup time.
    "welcometothejungle": "A",
    # B — open aggregation / freeform
    "jsearch": "B", "hn_hiring": "B", "adzuna": "B",
}

# Sources that are a *faithful copy of a specific employer posting* rather than an independent
# listing — i.e. the same req demonstrably exists on a direct board. Dedup folds these into the
# direct row instead of leaving two rows for one job (`fetch_jobs.dedupe`), and unlike the tier-B
# aggregator fold it may also ENRICH the survivor: WTTJ carries structured salary_min/salary_max
# on most rows where a Greenhouse JD leaves comp as prose the parser can miss.
#
# Distinct from tier-B aggregation, which is a copy with no reliable path back to an original.
MIRROR_SOURCES: frozenset[str] = frozenset({"welcometothejungle"})

_TIER_RANK = {"S": 3, "A": 2, "B": 1}

# Short labels for a UI badge (kept here so any UI and the logic can't drift).
TIER_LABEL = {"S": "direct", "A": "aggregator", "B": "open-aggregation"}

GHOST_RISK: dict[str, str] = {
    # low — the posting IS the employer's own record
    "ashby": "low", "greenhouse": "low", "lever": "low",
    "smartrecruiters": "low", "workday": "low", "rippling": "low",
    # …and HN, deliberately: tier B for directness, but employer-authored. THE carve-out — if a
    # future source lands here, ask "who typed this posting?", not "how structured is it?".
    "hn_hiring": "low",
    # …and welcometothejungle — the SECOND carve-out, and it earns 'low' on this module's own
    # stated criterion rather than on directness. Low risk means "dead is cheap to prove", and a
    # WTTJ posting HAS an apply_url leading back to the employer, so one request settles it. That
    # is the exact property open aggregation lacks.
    #
    # "Has" is about the payload, not about every row: the field is detail-only, so only a hydrated
    # row carries it. See the module docstring — the risk grade stands, the availability does not.
    "welcometothejungle": "low",
    # medium — curated mirrors; real, but a copy. Neutral middle, no penalty (no evidence yet).
    "remotive": "medium", "remoteok": "medium", "arbeitnow": "medium",
    # high — open aggregation; a copy with no curation and no reliable path back to the original
    "jsearch": "high", "adzuna": "high",
}

GHOST_RISK_DEFAULT = "medium"

# Badge text for a UI. Only 'high' is worth pixels — a badge on 99% of the board is wallpaper.
GHOST_RISK_LABEL = {"low": "", "medium": "", "high": "verify at source"}


def tier_of(source: str | None) -> str:
    """The S/A/B tier for a source string; unknown → 'A' (neutral middle)."""
    return SOURCE_TIERS.get((source or "").strip().lower(), "A")


def tier_rank(source: str | None) -> int:
    """Higher = more trustworthy/direct. S=3, A=2 (also the unknown default), B=1."""
    return _TIER_RANK[tier_of(source)]


def ghost_risk(source: str | None) -> str:
    """'low' | 'medium' | 'high' — how likely this source hands over somebody else's dead copy.

    Unknown → ``GHOST_RISK_DEFAULT`` ('medium'), which is penalty-free: an unclassified source is
    never punished on suspicion, and never trusted on silence either.
    """
    return GHOST_RISK.get((source or "").strip().lower(), GHOST_RISK_DEFAULT)
