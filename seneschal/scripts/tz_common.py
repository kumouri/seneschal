#!/usr/bin/env python3
"""The owner's timezone, in one place — date/label math for every script. Stdlib + tzdata.

The codebase was ported from a single-user stack whose scripts each hand-rolled a US-Central
DST formula (Windows ships no IANA tz database, so ``zoneinfo.ZoneInfo("America/Chicago")``
raised on the stock interpreter). The owner's timezone is config now —
``persona/identity.json`` → ``owner.timezone`` (an IANA string, nullable, read via
``identity_common.load_identity``) — and this module is the one consumer of it.

**The fallback ladder** (every public helper walks it):

1. ``owner.timezone`` configured? If not (the normal fresh-install state) → the **machine-local**
   wall clock, silently. An unconfigured install behaves exactly like the pre-tz_common code.
2. Configured — does ``zoneinfo`` resolve it? (Needs the ``tzdata`` package on Windows.) If yes →
   that zone, DST handled by the real IANA rules.
3. Configured but **unresolvable** (missing tzdata, typo'd key) → machine-local, with ONE warning
   line to stderr per process — the owner asked for a zone and isn't getting it, so they should
   hear about it once, but a bare interpreter must still work.

**Why ``tzdata`` is a sanctioned dependency** (the second ever, after ``websockets``): it is the
pure-data IANA timezone database, no code; Windows ships no system copy and stdlib ``zoneinfo``
needs one to resolve any IANA name. It rides the uv venv like ``websockets`` does — and exactly
like ``websockets``, everything degrades gracefully without the venv (ladder step 3).

**The semantics rule** callers must respect: date / label / day-boundary logic ("today",
transcript timestamps, ack dates, the until-morning target) uses the OWNER's timezone via this
module; **"when to fire" wall-clock logic stays machine-local** — daemon slot fire-times
(``presence.SLOTS_TEMPLATE``) and reminder ``due_at`` instants are untouched by configuration
(``presence.warn_tz_mismatch`` flags the machine/owner mismatch at startup).

Deterministic test/CLI specs: :func:`resolve_offset` keeps ``archive_common``'s exact spec
strings — ``auto`` (now: the owner's zone, else machine-local), ``UTC``, ``±NNN`` fixed minutes,
``chicago``/``America/Chicago`` (explicit US-Central, IANA when resolvable, else the retained
hand-rolled current-rule formula) — plus any resolvable IANA name.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone, tzinfo

from identity_common import get_str, load_identity

# Process-wide resolution cache: {"zone": ZoneInfo|None|_UNSET, "warned": bool}. The zone is
# resolved once (identity.json is read once, like presence.py's startup IDENTITY load); the
# warning fires at most once per process. _reset() exists for tests.
_UNSET = object()
_STATE = {"zone": _UNSET, "warned": False}


def _reset() -> None:
    """Test hook: forget the cached zone resolution and the warned-once flag."""
    _STATE["zone"] = _UNSET
    _STATE["warned"] = False


def _zone():
    """The configured-and-resolvable ZoneInfo, or ``None`` meaning "use machine-local".

    ``None`` covers both the silent path (nothing configured) and the warned path (configured
    but unresolvable). Cached after the first call; never raises.
    """
    if _STATE["zone"] is not _UNSET:
        return _STATE["zone"]
    zone = None
    name = get_str(load_identity(), "owner", "timezone")
    if name:
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415 — lazy so a broken zoneinfo can't block import
            zone = ZoneInfo(name)
        except Exception as e:  # noqa: BLE001 — ZoneInfoNotFoundError, missing tzdata, anything
            if not _STATE["warned"]:
                _STATE["warned"] = True
                print(f"! tz: configured owner timezone {name!r} could not be resolved ({e}) — "
                      f"falling back to the machine-local clock (install the venv/tzdata, or fix "
                      f"persona/identity.json)", file=sys.stderr)
    _STATE["zone"] = zone
    return zone


def owner_tz() -> tzinfo:
    """The owner's tzinfo per the fallback ladder (see module docstring).

    The machine-local fallback is a fixed-offset snapshot of *right now* (fresh on every call,
    so it never staleness-drifts across a DST edge as long as callers re-call rather than cache).
    """
    zone = _zone()
    return zone if zone is not None else datetime.now().astimezone().tzinfo


def local_now() -> datetime:
    """The owner's current local time as an aware datetime."""
    zone = _zone()
    return datetime.now(zone) if zone is not None else datetime.now().astimezone()


def local_today() -> str:
    """The owner's current local calendar date, ``YYYY-MM-DD`` (rule 5: date logic is never UTC)."""
    return local_now().strftime("%Y-%m-%d")


def local_stamp() -> str:
    """Authoritative local-time stamp for prompts, e.g. 'Friday 2026-07-03 12:30 Central Daylight
    Time'. Byte-format-identical to the stamp ``presence.local_stamp`` has always produced (%Z
    naming differs between zoneinfo's short names and Windows' long names; the format doesn't)."""
    return local_now().strftime("%A %Y-%m-%d %H:%M %Z")


def to_local(dt_utc: datetime) -> datetime:
    """An instant → aware datetime in the owner's timezone. Naive input is taken as UTC."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    zone = _zone()
    return dt_utc.astimezone(zone) if zone is not None else dt_utc.astimezone()


def offset_minutes(when) -> int:
    """The owner-tz UTC offset, in minutes, at a given moment (epoch seconds or datetime).

    This is the number the archive/health display paths store next to each record: −300/−360 for
    a US-Central owner, +570/+630 for an Adelaide one. Naive datetimes are taken as UTC.
    """
    if isinstance(when, (int, float)):
        dt = datetime.fromtimestamp(when, tz=timezone.utc)
    elif when.tzinfo is None:
        dt = when.replace(tzinfo=timezone.utc)
    else:
        dt = when
    off = to_local(dt).utcoffset()
    return int(off.total_seconds() // 60)


# ------------------------------------------------------------------ deterministic specs / legacy

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Mon=0) of ``year``-``month`` (e.g. 2nd Sunday of March)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def us_central_offset_minutes(dt_utc: datetime) -> int:
    """US Central (America/Chicago) offset in minutes for a UTC instant: −300 (CDT) or −360 (CST).

    Prefers real IANA data when ``zoneinfo`` can resolve it; otherwise falls back to the retained
    hand-rolled current-rule formula (valid 2007-onward) so a tzdata-less interpreter still
    answers, and the tests pin both DST transitions so the two paths can never drift apart. The
    formula: both transitions happen at 02:00 *local*, which is 08:00 UTC in March (02:00 CST,
    spring-forward) but **07:00 UTC in November** (02:00 CDT, fall-back). The legacy per-script
    copies used 08:00 UTC for both — off by one hour inside the 07:00-08:00 UTC window of every
    fall-back day; migrating here fixed that (IANA agrees with 07:00). Kept for the explicit
    ``chicago``/``America/Chicago`` :func:`resolve_offset` specs and the deprecated
    ``archive_common``/``health_common`` shims; new code wants :func:`offset_minutes` (owner-tz).
    Accepts naive (taken as UTC) or aware datetimes.
    """
    if dt_utc.tzinfo is not None:
        dt_utc = dt_utc.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        zone = ZoneInfo("America/Chicago")
        aware = dt_utc.replace(tzinfo=timezone.utc).astimezone(zone)
        return int(aware.utcoffset().total_seconds() // 60)
    except Exception:  # noqa: BLE001 — no tzdata: use the formula
        pass
    year = dt_utc.year
    dst_start = datetime.combine(_nth_weekday(year, 3, 6, 2), datetime.min.time()) + timedelta(hours=8)
    dst_end = datetime.combine(_nth_weekday(year, 11, 6, 1), datetime.min.time()) + timedelta(hours=7)
    return -300 if dst_start <= dt_utc < dst_end else -360


def resolve_offset(ts_utc: int, tz: str = "auto") -> int:
    """Display offset in minutes for an epoch instant, from a CLI/test spec string.

    Keeps ``archive_common.resolve_offset``'s exact accepted specs:

    * ``None`` / ``""`` / ``"auto"`` — the owner's timezone (configured, else machine-local).
      Under the old code these meant hardcoded US-Central; for a machine-local-in-Central install
      that is the same answer, for everyone else it is now the *right* answer.
    * ``"chicago"`` / ``"America/Chicago"`` — explicit US-Central (deterministic on any host,
      tzdata or not — see :func:`us_central_offset_minutes`).
    * ``"UTC"`` (any case) — 0. The deterministic renderer-test spec.
    * ``"±NNN"`` — fixed minutes, e.g. ``"-120"``.
    * any other resolvable IANA name — that zone's offset at the instant (additive; needs tzdata).
    * anything else — 0, as before.
    """
    if tz in (None, "", "auto"):
        return offset_minutes(int(ts_utc))
    if tz in ("chicago", "America/Chicago"):
        return us_central_offset_minutes(datetime.fromtimestamp(int(ts_utc), tz=timezone.utc))
    if tz.upper() == "UTC":
        return 0
    try:
        return int(tz)
    except (TypeError, ValueError):
        pass
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        aware = datetime.fromtimestamp(int(ts_utc), tz=timezone.utc).astimezone(ZoneInfo(tz))
        return int(aware.utcoffset().total_seconds() // 60)
    except Exception:  # noqa: BLE001 — unknown spec/no tzdata reads as UTC, matching the old code
        return 0
