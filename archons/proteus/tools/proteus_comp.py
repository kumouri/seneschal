#!/usr/bin/env python3
"""Pull a posted salary range out of a job description's prose.

**Why this exists.** US pay-transparency law made employers disclose a range, and
Greenhouse/Ashby render that disclosure as a paragraph at the *bottom* of the JD rather than as a
structured field. `fetch_jobs.MAX_DESC_CHARS` then truncated the stored description at 12 000 chars —
cutting the disclosure off before anything could read it. The scorer honestly reported "comp unknown —
neutral" and handed out a free +8/15, so a role paying **below** the profile's floor looked *better*
than one that disclosed honestly. (Live example: a role posting $105,910–$160,200 — failing the floor
at the top of band — scored 84.1% with "comp unknown". The number was in the posting the whole time,
~2 000 chars past the cap.)

So: `fetch_jobs._job()` runs this over the **full** text before truncating for storage, and
`score_jobs.score_comp()` re-runs it as a fallback for rows already cached without comp.

**Precision over recall, deliberately.** A wrong range is worse than no range — it would silently
re-inflate exactly the score this was written to fix, and a bogus "clears your floor" is how a
work-up gets wasted. Hence: a range shape, a salary word nearby, and a plausible annual figure.
"$500M Series C" and "saving customers $1.2M" do not match.
"""
from __future__ import annotations

import re

# A salary word must appear near the figure. Without this, any two dollar amounts in a sentence
# ("grew ARR from $2M to $8M") read as a pay band.
_CONTEXT = re.compile(
    r"compensation|salary|pay\s+range|pay\s+scale|base\s+pay|paid|pay\s+for\s+this|"
    r"range\s+for\s+this|expected\s+pay|target\s+pay|annual|per\s+year|/\s*yr|yearly|"
    r"per\s+hour|/\s*hr|hourly|base\s+range|cash\s+compensation|on-target\s+earnings|ote",
    re.I,
)
_CONTEXT_WINDOW = 400  # chars either side of the figure to search for a salary word

# $105,910 — $160,200  |  $250K – $485K  |  $180k to $230k  |  $105,910-$160,200 USD
_RANGE = re.compile(
    r"\$\s*(?P<lo>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<lok>[kK])?\s*"
    r"(?:-|–|—|to|through|\.\.)\s*"
    r"\$?\s*(?P<hi>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<hik>[kK])?",
)

_HOURLY_NEAR = re.compile(r"per\s+hour|/\s*hr\b|hourly|an\s+hour", re.I)

# Annualized plausibility band. Below: an hourly rate we failed to spot, or a bonus/stipend. Above:
# a funding round or a revenue figure that slipped past the context check.
_MIN_ANNUAL = 20_000.0
_MAX_ANNUAL = 2_000_000.0
_HOURS_PER_YEAR = 2080


def _num(raw: str, k_suffix: str | None) -> float | None:
    try:
        val = float(raw.replace(",", ""))
    except (TypeError, ValueError):
        return None
    if k_suffix:
        val *= 1000
    return val or None


def _annualize(val: float, hourly: bool) -> float:
    return val * _HOURS_PER_YEAR if hourly else val


def comp_from_text(text: str) -> tuple[float | None, float | None, str | None]:
    """``(low, high, note)`` for the posted range, or ``(None, None, None)``.

    Scans every ``$x – $y`` in the text, keeps the ones with a salary word within
    ``_CONTEXT_WINDOW`` and a plausible annualized value, and returns the **last** survivor — pay
    disclosures live at the foot of a JD, while earlier dollar figures tend to be funding or revenue
    boilerplate. Hourly rates ("$60 – $75 per hour") are annualized at 2080 h.
    """
    if not text:
        return (None, None, None)

    best: tuple[float, float, str] | None = None
    for match in _RANGE.finditer(text):
        start, end = match.span()
        window = text[max(0, start - _CONTEXT_WINDOW):end + _CONTEXT_WINDOW]
        if not _CONTEXT.search(window):
            continue

        lo = _num(match.group("lo"), match.group("lok"))
        hi = _num(match.group("hi"), match.group("hik"))
        if lo is None or hi is None:
            continue
        # "$250K – $485K" often marks only the second figure; carry the suffix across.
        if match.group("hik") and not match.group("lok") and lo < hi / 100:
            lo *= 1000
        if lo > hi:
            lo, hi = hi, lo

        hourly = bool(_HOURLY_NEAR.search(window))
        lo_a, hi_a = _annualize(lo, hourly), _annualize(hi, hourly)
        if not (_MIN_ANNUAL <= lo_a <= _MAX_ANNUAL and _MIN_ANNUAL <= hi_a <= _MAX_ANNUAL):
            continue

        note = f"parsed from posting text: {match.group(0).strip()}"
        if hourly:
            note += f" (hourly → ~{int(hi_a):,}/yr at {_HOURS_PER_YEAR} h)"
        best = (lo_a, hi_a, note)  # keep scanning: the last contextual range wins

    if best is None:
        return (None, None, None)
    return best
