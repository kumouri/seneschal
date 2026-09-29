#!/usr/bin/env python3
"""The owner's wall clock and activity day — a thin facade over ``tz_common``. Stdlib only.

``tz_common`` is the one owner-timezone implementation (``owner.timezone`` in
``persona/identity.json``, with its documented fallback ladder: unconfigured → machine-local,
configured-but-unresolvable → machine-local with one warning). This module adds nothing to that
ladder; it only keeps a small, stable set of names — ``now_utc``, ``to_local``, ``now_local``,
``local_today``, ``parse_iso`` — that date-dependent modules import, so none of them hand-rolls its
own conversion beside the clock.

**Naive on purpose.** :func:`to_local` / :func:`now_local` return the owner's wall clock as a *naive*
datetime: callers compare wall-clock *readings* (is it past 01:00 local? was this due before
curfew?), and a naive reading compares cleanly with every other naive reading in this tree. Call
``tz_common.to_local`` directly when an aware value is wanted.

**Two "todays", and they differ on purpose.** ``tz_common.local_today()`` is the owner's *calendar
date* as a ``YYYY-MM-DD`` string. :func:`local_today` here is the **activity day**: the same local
clock, cut at the owner's day boundary (``owner.dayBoundaryHour``, default 05:00, read through
``identity_common.day_boundary_hour``) so after-midnight activity counts as the PRIOR day. They agree
for most of the day and diverge in the hours between midnight and the cut, which is exactly the
window an activity-day caller exists to get right.

Test seam: every function that reads "now" takes an injectable instant. A date-dependent test must
inject its instant (and patch the zone — ``tz_common._zone`` — and the identity) rather than racing
the wall clock or depending on the runner's own timezone.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import tz_common
from identity_common import DEFAULT_DAY_BOUNDARY_HOUR, day_boundary_hour, load_identity

#: The after-midnight cut when nothing is configured — ``identity_common``'s default, re-exported so
#: a caller that wants the constant does not need a second import.
DEFAULT_CUT_HOUR = DEFAULT_DAY_BOUNDARY_HOUR


def configured_cut_hour() -> int:
    """The owner's configured day boundary (``owner.dayBoundaryHour``), validated, default 5.

    Read fresh on every call (``load_identity`` never raises and is a small file read), so an owner
    who changes the setting does not need a daemon restart for date logic to follow."""
    return day_boundary_hour(load_identity())


def now_utc() -> datetime:
    """The current instant, naive UTC.

    Never ``datetime.utcnow`` — deprecated since 3.12, and the root of every hand-rolled conversion
    that ended up living beside it rather than going through one shared clock."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(instant: datetime) -> datetime:
    """An aware (or naive-assumed-UTC) instant → the owner's **naive** wall clock.

    The zone is ``tz_common``'s (configured owner zone, else machine-local); see the module
    docstring for why the result is naive."""
    return tz_common.to_local(instant).replace(tzinfo=None)


def now_local(now: datetime | None = None) -> datetime:
    """The current instant (or ``now``, a UTC instant — the test seam) as the owner's naive wall
    clock."""
    return to_local(now if now is not None else datetime.now(timezone.utc))


def local_today(cut_hour: int | None = None, now: datetime | None = None) -> date:
    """The **activity day** now (or at ``now``): the owner's wall clock, cut at ``cut_hour`` —
    after-midnight activity counts as the PRIOR day.

    ``cut_hour`` defaults to the configured boundary (:func:`configured_cut_hour`). ``now`` is a UTC
    instant (aware or naive-UTC) and exists as the test seam: a date-dependent test must inject its
    instant here rather than racing the wall clock."""
    hours = configured_cut_hour() if cut_hour is None else cut_hour
    return (now_local(now) - timedelta(hours=hours)).date()


def parse_iso(s, *, strict: bool = False) -> datetime | None:
    """Tolerant ISO-8601 parse → aware UTC ``datetime``, or ``None`` on anything unparseable.

    Accepts a trailing ``Z``, an explicit offset, or a naive string (assumed UTC, matching every
    writer in this tree). **``None`` in means ``None`` out on the lenient path — never "now"**: a
    caller silently defaulting a missing timestamp to the current instant is a documented trap (a
    queue entry with no ``due_at`` must not be dated to *today*, which may be exactly the day whose
    entries are about to be deleted).

    ``strict=True`` raises ``ValueError`` on anything that would otherwise return ``None`` — a caller
    that would rather fail loudly at the parse than propagate a silent ``None`` several calls deep."""
    if not isinstance(s, str) or not s.strip():
        if strict:
            raise ValueError(f"parse_iso: not a non-empty string: {s!r}")
        return None
    text = s.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        if strict:
            raise
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
