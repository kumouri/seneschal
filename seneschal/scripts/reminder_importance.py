#!/usr/bin/env python3
"""⏰ Importance — the canonical levels and the one normalizer every script compares through. Stdlib only.

A Reminders row's importance is a **canonical key** (``super-critical``, ``critical``, ``high``,
``notable``, ``low`` — ``store/markdown/schema.md`` → reminders). Each backend spells those keys its
own way: the filesystem backends store the key itself; the Notion backend stores an emoji label
(``🛑 Super-Critical``, ``⭐ High`` …; the translation table is ``store/notion/schema.template.md`` →
reminders → **importance**). A queued nudge carries whatever string the seed pass read off the row, so
code must never compare against one backend's labels. :func:`normalize` folds any spelling to the key
— emoji and punctuation dropped, case folded, spaces to hyphens — so ``"🛑 Super-Critical"``,
``"super-critical"`` and ``"Super Critical"`` are one level.

Consumers: ``sentinel._due_sort_key`` (the catch-up drain order, via :data:`RANK`) and
``reminders_seed.ladder_gap`` (the High-and-above "seeded without a ladder" notice, via
:func:`at_least`).
"""
from __future__ import annotations

import re

#: Top → bottom, the order ``references/reminders-policy.md`` → "Importance" defines.
LEVELS = ("super-critical", "critical", "high", "notable", "low")

#: ``{canonical key: rank}``, 0 = most important. Used to break a same-``due_at`` tie in the drain.
RANK = {key: i for i, key in enumerate(LEVELS)}

#: Missing/unrecognized importance — an ad-hoc nudge, a standing roll — ranks NEUTRAL, the same as
#: ``notable``, so it can neither jump a ``high`` row nor get starved behind an explicit ``low`` one
#: just for lacking the field.
DEFAULT_RANK = RANK["notable"]

_NON_WORD_RE = re.compile(r"[^a-z]+")


def normalize(value) -> str | None:
    """Any backend's spelling of an importance level → its canonical key, or ``None`` when it names
    no known level. Total — a non-string is ``None``."""
    if not isinstance(value, str):
        return None
    key = _NON_WORD_RE.sub("-", value.lower()).strip("-")
    return key if key in RANK else None


def rank(value) -> int:
    """The drain-order rank of ``value`` (any spelling); :data:`DEFAULT_RANK` when unrecognized."""
    key = normalize(value)
    return RANK[key] if key is not None else DEFAULT_RANK


def at_least(value, level: str) -> bool:
    """Is ``value`` (any spelling) at or above canonical ``level``? False for an unrecognized value."""
    key = normalize(value)
    return key is not None and RANK[key] <= RANK[level]
