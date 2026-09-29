#!/usr/bin/env python3
"""The durable half of the premise-review question — which ``Consecutive Misses`` count a row was
last asked about, so the same streak doesn't ask again until it climbs a further threshold past
that point.

``reminder_premise.py`` stays pure — no I/O, per its own docstring. This module is its one caller
that touches disk, and the thing that connects it to the live seed path (``reminders_seed.py``).

``../docs/reminder-premise-spec.md`` proposes two row-side store fields (``Premise`` /
``Premise Last Reviewed At Misses``) for the full interactive feature. **They are not part of the
shipped schema** — adding them is an owner-side store edit gated on the spec's open decisions. Until
then, the "already asked" marker ``review_due`` needs lives locally instead, on every backend:
``state/reminder-premise-reviews.json``, one int per reminder id. The channel is the one that already
exists rather than a new one — the seed's stderr ``!`` line, copied into the Run Log verbatim exactly
like the ``no_ladder`` gap (``subagents/reminders/SKILL.md``). Nothing here answers the question or
retires a row; see ``reminder_premise.py``.

No cross-process lock: this store gets at most one writer per row per seed pass (the daily seed,
run once), and a lost update between two overlapping runs costs one skipped or repeated review
line — the same report-only stakes ``review_due`` was already designed to fail open on, never a
lost reminder or a lost ack.

Stdlib only."""
from __future__ import annotations

import json
import os

import reminder_premise as rp
import stateio

STORE_FILE = "reminder-premise-reviews.json"
SCHEMA = "seneschal.reminder-premise-reviews/1"


def _store_path(state_dir: str) -> str:
    return os.path.join(state_dir, STORE_FILE)


def _load(state_dir: str) -> dict:
    """``{reminder_id: last_reviewed_at_misses}``. A missing or corrupt store reads as empty — the
    same fail-open direction ``review_due`` already takes for an unreadable marker: losing this store
    must cost one redundant question, never manufacture silence on a row that really is overdue for
    one."""
    try:
        with open(_store_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    reviewed = data.get("reviewed")
    return reviewed if isinstance(reviewed, dict) else {}


def last_reviewed_at_misses(state_dir: str, reminder_id) -> int | None:
    """The stored miss-count this row was last asked about, or ``None`` if never asked (or the
    stored value is unreadable — same fail-open direction as a missing store)."""
    val = _load(state_dir).get(str(reminder_id))
    return val if isinstance(val, int) and not isinstance(val, bool) else None


def mark_reviewed(state_dir: str, reminder_id, consecutive_misses: int) -> bool:
    """Stamp ``reminder_id`` as asked-about at ``consecutive_misses``. Fail-open like every other
    ``state/`` writer: a failed stamp costs one extra future question, never the seed calling it."""
    try:
        reviewed = _load(state_dir)
        reviewed[str(reminder_id)] = int(consecutive_misses)
        stateio.write_json_atomic(_store_path(state_dir), {"schema": SCHEMA, "reviewed": reviewed})
        return True
    except Exception:  # noqa: BLE001 — never the seed
        return False


def check_premise_review(state_dir: str, reminder_id, consecutive_misses, importance, title, *,
                         mark: bool = True) -> str | None:
    """The one call ``reminders_seed.py`` makes per row. Returns
    ``reminder_premise.format_review_question``'s text if this row is due for a premise-review
    question right now, else ``None`` — never a verdict on the row, exactly like ``review_due``
    itself.

    When ``mark`` (the default for a real seed call; pass ``False`` for ``--dry-run``, which must
    never touch disk), a due row gets the store stamped with the CURRENT ``consecutive_misses`` —
    at ask-time, not answer-time — so the same streak doesn't ask again until it climbs a further
    threshold past this point."""
    if not reminder_id:
        return None
    last = last_reviewed_at_misses(state_dir, reminder_id)
    if not rp.review_due(consecutive_misses, importance, last):
        return None
    if mark:
        mark_reviewed(state_dir, reminder_id, consecutive_misses)
    return rp.format_review_question(title, consecutive_misses, premise=None)
