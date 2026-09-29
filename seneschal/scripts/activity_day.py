#!/usr/bin/env python3
"""The **after-midnight rule**, in one place: which *activity day* a moment belongs to.

The rule — *after-midnight activity counts as the prior day* — is cut at the owner's configured day
boundary, ``owner.dayBoundaryHour`` in ``persona/identity.json`` (default 05:00, read through
``identity_common.day_boundary_hour``): a 01:29 dinner is **that evening's** dinner, not the next
day's, and an ack at 01:29 must not delete the **next** day's un-fired nudges. Every consumer shares
this one definition, so the rule can only be changed in a place where every consumer sees the diff.

Two "todays", and the difference is the whole point
---------------------------------------------------
* :func:`today` here is the **activity day**: the owner's timezone, cut at the day boundary. It
  answers *"which day's work is this?"*.
* ``tz_common.local_today`` is the **calendar date** in the owner's timezone, no cut. It answers
  *"what date does a ledger stamp?"* — e.g. the ack ledger, whose fire-time gate compares against
  that same function. They agree for most of the day and diverge between midnight and the cut, which
  is the window this module exists to get right.

**The owner's zone, not the machine's.** Conversion goes through ``clock``/``tz_common`` (the
configured ``owner.timezone`` with its machine-local fallback ladder). That matters because the
inputs are often **UTC instants** (``reminders.json``'s ``due_at``): read on a UTC runner through a
bare ``.astimezone()``, a 02:30Z nudge would land on the wrong side of the cut and the comparison
this module feeds would silently invert.

Stdlib only. Nothing here reads or writes state; given the owner's configuration, every function is
pure in its arguments.

:func:`to_owner_local` returns ``None`` FOR A ``None`` INPUT — IT IS NOT "now". A missing timestamp
silently treated as "right now" would date a queue entry with no ``due_at`` to today's activity day
— exactly the day whose nudges a reconcile/dequeue pass is about to delete. :func:`today` is the
function to call when you actually want the clock; :func:`to_owner_local` never guesses on your
behalf.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import clock
import tz_common

#: The day boundary when nothing is configured: 05:00. The live value is the owner's
#: ``owner.dayBoundaryHour`` — see :data:`DAY_CUT_HOUR` and :func:`cut_hour`.
DEFAULT_DAY_CUT_HOUR = clock.DEFAULT_CUT_HOUR

#: Local hour that closes a day — the owner's configured boundary as of import (default 5). Anything
#: earlier belongs to the PRIOR day. A snapshot for callers that want a constant; every function
#: below re-reads the configuration when its ``cut_hour`` is left as ``None``.
DAY_CUT_HOUR = clock.configured_cut_hour()


def cut_hour() -> int:
    """The owner's configured day boundary, read now (``clock.configured_cut_hour``)."""
    return clock.configured_cut_hour()


def _hours(cut: int | None) -> int:
    return cut_hour() if cut is None else cut


def parse_instant(value) -> datetime | None:
    """An ISO-8601 instant → **naive UTC**, or ``None`` when it can't be read.

    Accepts the ``…Z`` form ``reminders.json`` writes, an explicit offset, or a naive string (taken
    as UTC, matching every writer in this tree). A ``datetime`` passes through — aware converted,
    naive assumed UTC. Never raises: a caller deciding whether to *delete* something wants a
    "don't know", not an exception.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def parse_day(value) -> date | None:
    """``"YYYY-MM-DD"`` / ``date`` / ``datetime`` → :class:`date`, or ``None`` when unreadable."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def to_owner_local(instant) -> datetime | None:
    """A UTC instant → the owner's **naive** wall clock, or ``None`` if it can't be read.

    ``instant`` is required, and ``None`` in means ``None`` out — it does **not** mean "now" (see the
    module docstring for the trap that overload sets). Use :func:`today` when you want the clock.
    """
    utc = parse_instant(instant)
    if utc is None:
        return None
    return clock.to_local(utc)


def owner_now(now_utc: datetime | None = None) -> tuple[datetime, int, datetime]:
    """``(naive-UTC now, owner offset in minutes, naive owner-local now)``.

    The three come back together because a caller stamping a record often needs the offset itself
    (a ``tz_offset_min`` column) alongside the wall clock, and recomputing it separately invites the
    two to straddle a DST edge. ``now_utc`` (aware or naive-UTC) is the test seam.
    """
    utc = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
    if utc.tzinfo is not None:
        utc = utc.astimezone(timezone.utc).replace(tzinfo=None)
    aware = tz_common.to_local(utc)
    offset = int(aware.utcoffset().total_seconds() // 60)
    return utc, offset, aware.replace(tzinfo=None)


def from_local(local: datetime, cut_hour: int | None = None) -> date:
    """The activity day of a **local wall-clock** datetime: after-midnight counts as the PRIOR day.

    Takes local time, not an instant — a caller that already holds a wall clock must not have it
    re-derived through a timezone it never went through. ``cut_hour`` defaults to the owner's
    configured boundary.
    """
    return (local - timedelta(hours=_hours(cut_hour))).date()


def from_instant(instant, cut_hour: int | None = None) -> date | None:
    """The activity day of a **UTC instant** — the owner's wall clock first, then the cut.

    ``None`` when the instant can't be read, which callers must treat as "don't know" rather than
    as any particular day. Both directions of the mapping matter: a nudge due 00:30 local on the
    *following* calendar date belongs to the current activity day, and one due 21:30 local on the
    current calendar date belongs to the *next* activity day when read after midnight.
    """
    local = to_owner_local(instant)
    if local is None:
        return None
    return from_local(local, cut_hour)


def today(now: datetime | None = None, cut_hour: int | None = None) -> date:
    """The activity day **now** — the owner's timezone, cut at ``cut_hour`` (default: configured).

    ``now`` is a UTC instant (aware or naive-UTC) and exists as the test seam: a date-dependent test
    must inject its instant here rather than racing the wall clock.
    """
    return from_local(owner_now(now)[2], cut_hour)
