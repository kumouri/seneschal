#!/usr/bin/env python3
"""Pure decision logic for the ⏰ premise-review question — no I/O, no store, no send.

``../docs/reminder-premise-spec.md`` is the design record. This module answers exactly one
question — is a row due for a "is this still a thing?" review — and never answers whether a
premise is actually still true, because nothing but the owner can judge that (see the spec's
"What this is NOT"). Its one caller is ``reminder_premise_track.py`` (the durable "already asked"
marker), which ``reminders_seed.py`` calls once per seeded row.
"""
from __future__ import annotations

import reminder_importance as imp

# Recommended defaults (the spec's open questions name them). They exist so the seed has something
# concrete to apply; a caller may override either.
DEFAULT_THRESHOLD = 6
CRITICAL_THRESHOLD = 4


def threshold_for(importance, *, default_threshold: int = DEFAULT_THRESHOLD,
                  critical_threshold: int = CRITICAL_THRESHOLD) -> int:
    """The consecutive-miss count a row must reach before it is due for a premise review.

    Critical-and-above (any backend's spelling — ``reminder_importance``) gets a LOWER bar,
    deliberately the opposite of the gentle rib's own "low-importance only" scope
    (``../references/reminders-policy.md`` → "The gentle rib"): the more a row can pierce quiet hours
    and page like an alarm, the faster an unacked streak needs questioning, because that is the
    surface training the owner to ignore it. An unrecognized or missing importance gets the default
    bar.
    """
    if imp.at_least(importance, "critical"):
        return critical_threshold
    return default_threshold


def review_due(consecutive_misses, importance, last_reviewed_at_misses, *,
               default_threshold: int = DEFAULT_THRESHOLD,
               critical_threshold: int = CRITICAL_THRESHOLD) -> bool:
    """Is this row due for a premise-review question right now?

    Returns a bool, never anything a caller could read as a verdict on the row itself — this
    function cannot suppress a nudge, flip ``Status``, or touch ``Nag Until Done``, because it
    has no such return shape. That is deliberate: the review is a question, never an action.

    **FAIL-OPEN, TWO WAYS, IN OPPOSITE DIRECTIONS — both toward the safer failure:**

    * An unreadable or nonsensical ``consecutive_misses`` (``None``, negative, a string) means we
      cannot confirm the row is actually overdue, so this returns ``False`` — bad data must never
      manufacture an overdue streak that isn't real.
    * An unreadable or nonsensical ``last_reviewed_at_misses`` means we cannot confirm we already
      asked, so — once ``consecutive_misses`` clears the threshold — this returns ``True``. The
      cost of guessing wrong here is one redundant question; the cost of guessing the other way
      is silently never asking again, which recreates the exact bug this module exists to fix.
    """
    if (not isinstance(consecutive_misses, int) or isinstance(consecutive_misses, bool)
            or consecutive_misses < 0):
        return False
    threshold = threshold_for(importance, default_threshold=default_threshold,
                              critical_threshold=critical_threshold)
    if consecutive_misses < threshold:
        return False
    if last_reviewed_at_misses is None:
        return True
    if (not isinstance(last_reviewed_at_misses, int) or isinstance(last_reviewed_at_misses, bool)
            or last_reviewed_at_misses < 0):
        return True
    return consecutive_misses >= last_reviewed_at_misses + threshold


def format_review_question(title: str, consecutive_misses: int, premise=None) -> str:
    """One line, factual, no verdict — the "is this still a thing?" question itself.

    Cite the number, never a feeling — the same rule the gentle rib already follows
    (``../references/reminders-policy.md`` → "The gentle rib"). Never states an importance
    judgment and never proposes retiring anything; it asks, and attaches the reason on file if
    there is one.
    """
    base = f"{title} — {consecutive_misses} cycles, never acked. Still a thing?"
    if premise:
        return f"{base} (on file: {premise})"
    return base
