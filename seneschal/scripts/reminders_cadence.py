#!/usr/bin/env python3
"""⏰ **Cadence** — one grammar for *how often*, in place of a hand-enumerated select.

Stdlib only. This module is the whole mechanism: a parser, a due-test, and the even-spread
distribution behind rates. Nothing else in the tree may enumerate cadence values.

## Why this exists

A cadence select that grows one option per new interval ("Every 2 days", then "Every 4 days", …)
costs a schema edit plus doc edits every time, to express a single integer — and it can never express
"any number of days". The interval family was the only part of the select that kept growing, so it
became an expression instead.

## Two shapes, not one

An **interval** is an *offset*: "four days after you last did it." A **rate** is a *count per
period*: "twice a week." A design that only does offsets cannot express the second. "Twice a week"
means an even **spread** (alternating 3 days then 4 days), not a fillable bucket where both events may
land on Monday.

## The decision — an interval is ack-relative; a rate is grid-anchored

The two shapes get **different anchoring**, because they mean different things:

* ``every N days`` is **ack-relative**: due when ``today >= Last Acknowledged + N``. The offset *is*
  the semantic — "N days after you last did it" — so a late ack must push the next one back.

* ``N per D days`` is **grid-anchored**: the event days are a fixed lattice
  (:func:`next_rate_day`), and the next due is the first lattice point strictly after the last ack.
  The **count** is the semantic, and only a fixed grid keeps the count true. Ack-relative gaps would
  let a three-day-late ack quietly turn "twice a week" into "twice per nine days" — the rate would be
  whatever the slippage made it, which is precisely the property a rate exists to pin down.

This also disposes of the state question. A grid needs **no stored cycle position**: the anchor plus
``Last Acknowledged`` determine where in the cycle a row sits. So a rate row adds **no new store
property**, and there is nothing for Dream to reconcile.

The visible consequence, and it is a good one: with the anchor a Monday, ``2 per 7 days`` lands on
**Mondays and Thursdays**, and ``3 per 7 days`` on **Mon/Wed/Fri**. A rate is predictable.

## The spread is Bresenham, not an invention

Spreading N events as evenly as possible over D whole days is the classical Bresenham line /
Euclidean-rhythm distribution::

    gap_j = floor((j+1)*D/N) - floor(j*D/N)

``2 per 7`` -> 3,4 (repeating: 3,4,3,4). ``3 per 7`` -> 2,2,3. ``3 per 10`` -> 3,3,4. Every gap is
either ``floor(D/N)`` or ``floor(D/N)+1``, which is the formal statement of "as evenly as possible."
:func:`gaps` is that one line; :func:`next_rate_day` is the same lattice walked from a date.

## Fractional days are REFUSED, loudly, and told the right spelling

``every 3.5 days`` does not fire. The due test is a **date** comparison and the seed pass runs
**once per owner-local day**, so there is no wake that could deliver a half-day offset — and a value
silently floored to 3 is exactly the quiet wrongness a scheduler must not have.

So a fractional interval raises :class:`CadenceError`, and the message names the spelling that
*works*: ``every 3.5 days`` -> **"2 per 7 days"**. The rate spelling says what is meant (two per week)
without asserting a half-day offset the system cannot honour, and it lands on whole days at a stable
time-of-day. Where the fraction implies **more than one event per day**, the message points at
``Multiple/day`` + ``Times`` instead, which is the mechanism that already owns intraday firing.

Integral floats are fine: ``every 3.0 days`` is ``every 3 days``. The refusal is about a fraction
that cannot land on a day, not about a decimal point.

## An unparseable cadence fails OPEN

A row whose ``Cadence`` this module cannot parse is **due today, and reported** — never silently
dropped. A reminder that cannot be scheduled is better delivered too often than never, it matches the
existing "empty ``Last Acknowledged`` => due now" convention, and a typo that nags is a typo that
gets fixed. See :func:`is_due` and the seed-pass wiring in ``references/reminders-policy.md``.

## Backward compatibility

**Every one of the classic select options is already a well-formed expression in this grammar** —
``Daily``, ``Weekdays``, ``Every 2/3/4/5 days``, ``Weekly``, ``Multiple/day``, ``One-off`` all
parse, to exactly the behaviour they always had. So shipping this module changes nothing in the store
and breaks no row. Converting a select-typed ``Cadence`` property to free text (so arbitrary
expressions become *typeable*) is an optional later step with no data migration, because the strings
survive verbatim; run ``--audit`` first. Runbook: ``seneschal/docs/reminder-cadence-mechanism-spec.md``.

Usage::

  python reminders_cadence.py --parse "every 11 days"
  python reminders_cadence.py --gaps "2 per week"
  python reminders_cadence.py --due "2 per week" --last-ack 2026-08-31 --today 2026-09-03
  python reminders_cadence.py --due "One-off" --due-target 2026-09-14 --today 2026-09-11 \\
      --lookahead-days 0    # Today Todo: not due until its own day (Deadline Watch omits the flag)
  python reminders_cadence.py --audit                  # every row in the live id cache
  python reminders_cadence.py --audit --from-stdin     # one cadence string per line

Invariants worth knowing before an edit:

* Cadence IS AN EXPRESSION, AND NOTHING ELSE IN THIS TREE MAY ENUMERATE ITS VALUES. There are SEVEN
  shapes: daily, weekdays, weekly, multiple/day, one-off, every N days, and N per D days — two of
  which (the last two) carry a magnitude. A bare number cannot express four of the classic options,
  which is why this is a grammar and not a float.
* RATE_EPOCH IS FROZEN AT MONDAY 2024-01-01 — moving it re-phases every live rate row. :func:`gaps`
  and :func:`next_rate_day` walk THE SAME LATTICE, and a test asserts they agree.
* FRACTIONAL DAYS RAISE, THEY NEVER ROUND, and the refusal is computed from ``Fraction(TEXT)``, never
  from a float (``Fraction(0.1)`` is binary noise).
* :func:`is_due` FAILS OPEN ON EVERY PATH: an unparseable cadence, an absent ``Last Acknowledged``,
  and an absent ``Due / Target`` all mean DUE.
* THE ONE-OFF LOOKAHEAD IS TYPE-GATED BY THE CALLER, NOT BY THIS MODULE. :func:`is_due` and
  :func:`next_due` take a plain ``lookahead_days`` defaulting to ``ONE_OFF_LOOKAHEAD_DAYS``. A
  Deadline Watch wants the default (surface an approaching deadline a few days early); a Today Todo
  does not (an appointment due Monday must not read as "do it now" the Friday before). This module
  never learns what "Today Todo" or "Deadline Watch" mean; the seed-side caller decides 0 vs the
  default and passes it through ``--lookahead-days``.
* :func:`is_multi_fire` IS CONTRACTUALLY NEVER NARROWER THAN THE SUBSTRING TEST IT REPLACED, and falls
  back to that substring test verbatim on any parse failure: a stricter predicate would newly GATE
  multi-fire rolls the fire-time ack gate has always exempted.
* THIS MODULE IS A LEAF: stdlib only, and it imports no sibling at module level — the id-cache hex
  predicate is spelled again here rather than imported back from ``reminders_acks``, so
  ``reminders_acks`` can depend on this module without a cycle. (The owner's "today" is read lazily
  through ``tz_common`` when a caller omits it, with a machine-local fallback.)
* ``--audit`` IS THE MIGRATION PRE-FLIGHT: it parses every live row out of the id cache and EXITS 1 IF
  ANY FAIL, and it should run before a ``Cadence`` store property is retyped.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, timedelta
from fractions import Fraction

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
ID_CACHE_FILE = "reminders-id-cache.md"

#: The rate lattice's origin. **2024-01-01 is a Monday**, deliberately: it makes ``2 per 7 days``
#: land on Mon/Thu and ``3 per 7 days`` on Mon/Wed/Fri, which is the human answer. Any fixed date
#: would be correct; this one is also legible. Changing it re-phases every live rate row, so treat
#: it as frozen.
RATE_EPOCH = date(2024, 1, 1)

#: How far ahead a ``One-off`` starts surfacing, in days — the DEFAULT, right for a Deadline Watch
#: (surface an approaching deadline early). Mirrors the "~3 days" in
#: ``references/reminders-policy.md`` -> "Cadence — when a row is 'due today'".
#:
#: **Not right for every ``One-off``.** A ``Today Todo`` (an appointment/commitment for a specific
#: day) surfaced three days early is a false alarm, not a heads-up. So :func:`is_due`/:func:`next_due`
#: take ``lookahead_days`` as a plain parameter defaulting to this constant; the SEED-side caller (which
#: knows the row's ``Type``) passes ``0`` for a ``Today Todo`` and leaves the default for a
#: ``Deadline Watch``. This module never spells "Today Todo" / "Deadline Watch" itself.
ONE_OFF_LOOKAHEAD_DAYS = 3

#: Weekday names the ``Notes`` field may use to pin a ``Weekly`` row to a calendar day. Index is
#: ``date.weekday()`` (Monday = 0). Both the full name and the 3-letter abbreviation are matched, on
#: a word boundary, as an explicit alternation — reconstructing the full name from its abbreviation
#: plus "day" only works for monday/friday/sunday, and silently never matched the other four.
#: Deliberately generous ("Good Friday shopping" also hits), because the alternative to matching is
#: falling back to the +7 rule, which is the safe direction.
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: Legacy substring test for a multi-fire row, kept as :func:`is_multi_fire`'s fail-open floor so
#: this module can never gate a row the old ``"multiple" in cadence.lower()`` check exempted.
MULTI_FIRE_TOKEN = "multiple"

#: A ⏰ page id, normalised (32 hex, dashes dropped) — the shape that tells a real id-cache row from
#: the table's header and its ``|---|`` separator. Same predicate as ``reminders_acks._HEX32_RE``,
#: spelled again rather than imported so this module stays a leaf with no siblings to load.
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")

# --- shapes -----------------------------------------------------------------------------------
#: The seven shapes. ``INTERVAL`` and ``RATE`` carry a magnitude; the other five are complete in
#: themselves. **This is a shape vocabulary, not an option list** — it is closed because these are
#: the genuinely distinct *questions* a cadence can answer, not because someone enumerated values.
DAILY = "daily"
WEEKDAYS = "weekdays"
INTERVAL = "interval"
RATE = "rate"
WEEKLY = "weekly"
MULTIPLE = "multiple"
ONE_OFF = "one-off"

SHAPES = (DAILY, WEEKDAYS, INTERVAL, RATE, WEEKLY, MULTIPLE, ONE_OFF)


class CadenceError(ValueError):
    """An expression this grammar refuses. The message is written to be shown to the owner verbatim —
    it says what was wrong AND, where one exists, the spelling that works."""


class Cadence:
    """A parsed cadence: a ``shape`` plus, for ``INTERVAL``/``RATE``, its magnitude.

    ``n``  — ``INTERVAL``: the day offset. ``RATE``: the event count. Otherwise ``None``.
    ``d``  — ``RATE``: the period in days. Otherwise ``None``.
    ``raw`` — the source string, kept so an error or a log can quote what was actually written.
    """

    __slots__ = ("shape", "n", "d", "raw")

    def __init__(self, shape: str, n=None, d=None, raw: str = ""):
        self.shape, self.n, self.d, self.raw = shape, n, d, raw

    def __eq__(self, other):
        return (isinstance(other, Cadence)
                and (self.shape, self.n, self.d) == (other.shape, other.n, other.d))

    def __hash__(self):
        return hash((self.shape, self.n, self.d))

    def __repr__(self):
        bits = [self.shape]
        if self.n is not None:
            bits.append("n=%s" % self.n)
        if self.d is not None:
            bits.append("d=%s" % self.d)
        return "Cadence(%s)" % ", ".join(bits)

    def as_dict(self) -> dict:
        return {"shape": self.shape, "n": self.n, "d": self.d, "raw": self.raw,
                "canonical": self.canonical()}

    def canonical(self) -> str:
        """The one spelling this module would write. Round-trips through :func:`parse`."""
        if self.shape == INTERVAL:
            return "every %d days" % self.n
        if self.shape == RATE:
            return "%d per %d days" % (self.n, self.d)
        return {DAILY: "daily", WEEKDAYS: "weekdays", WEEKLY: "weekly",
                MULTIPLE: "multiple/day", ONE_OFF: "one-off"}[self.shape]


def _owner_today() -> date:
    """The owner-local calendar date, via ``tz_common`` (imported lazily so this module stays a leaf
    at import time); machine-local if that is unavailable. Only used when a caller omits ``today``."""
    try:
        import tz_common  # noqa: PLC0415 — lazy on purpose (see the module docstring)
        return date.fromisoformat(tz_common.local_today())
    except Exception:  # noqa: BLE001 — a bare interpreter still answers with the machine clock
        return date.today()


# --- the even spread --------------------------------------------------------------------------

def gaps(n: int, d: int) -> list:
    """The Bresenham / Euclidean-rhythm gap sequence for ``n`` events per ``d`` days.

    ``gaps(2, 7) == [3, 4]`` · ``gaps(3, 7) == [2, 2, 3]`` · ``gaps(3, 10) == [3, 3, 4]``.

    Sums to ``d`` by construction (the interior terms telescope), and every gap is ``d//n`` or
    ``d//n + 1`` — the formal statement of "as evenly as possible". The sequence repeats, so
    ``2 per 7`` is experienced as 3, 4, 3, 4, …"""
    if n <= 0 or d <= 0:
        raise CadenceError("a rate needs a positive count and a positive period")
    return [((j + 1) * d) // n - (j * d) // n for j in range(n)]


def next_rate_day(n: int, d: int, after: date) -> date:
    """The first ``n``-per-``d`` lattice point **strictly after** ``after``.

    The lattice is ``RATE_EPOCH + floor(j*d/n)`` for j = 0, 1, 2, … — the same sequence
    :func:`gaps` differences. Solving ``floor(j*d/n) > k`` for the smallest ``j`` gives
    ``j = ceil((k+1)*n/d)``, so this is O(1) rather than a walk, and it is correct for an ``after``
    arbitrarily far from the epoch in either direction.

    This is where the grid-anchored decision lives: the answer depends on *where the last ack fell*,
    not on how many cycles have elapsed, so a late ack rejoins the lattice instead of dragging it."""
    if n <= 0 or d <= 0:
        raise CadenceError("a rate needs a positive count and a positive period")
    k = (after - RATE_EPOCH).days
    j = -((-(k + 1) * n) // d)  # ceil((k+1)*n/d), integer-only
    return RATE_EPOCH + timedelta(days=(j * d) // n)


# --- parsing ----------------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_INTERVAL_RE = re.compile(r"^every\s+(?P<num>\d+(?:\.\d+)?)\s*(?:days?|d)$")
_RATE_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)\s*(?:per|/|x)\s*(?P<period>.+)$")
_PERIOD_NUM_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)\s*(?:days?|d)?$")

#: Whole-string synonyms for the five magnitude-free shapes. Kept deliberately short: this is a
#: grammar, not a natural-language front end, and ``ack.py`` already owns "what did they mean".
_LITERALS = {
    "daily": DAILY, "every day": DAILY, "each day": DAILY, "every 1 day": DAILY,
    "weekdays": WEEKDAYS, "weekday": WEEKDAYS, "every weekday": WEEKDAYS,
    "mon-fri": WEEKDAYS,
    "weekly": WEEKLY, "every week": WEEKLY, "each week": WEEKLY,
    "multiple/day": MULTIPLE, "multiple per day": MULTIPLE, "multiple a day": MULTIPLE,
    "multiple": MULTIPLE, "multiple/ day": MULTIPLE,
    "one-off": ONE_OFF, "one off": ONE_OFF, "oneoff": ONE_OFF, "once": ONE_OFF,
}

#: Named periods for a rate. ``day`` is deliberately ABSENT — "3 per day" is intraday and belongs to
#: ``Multiple/day`` + ``Times``; :func:`_period_days` refuses it by name rather than by arithmetic.
_PERIODS = {"week": 7, "wk": 7, "fortnight": 14, "month": 30, "year": 365}


def _normalize(text) -> str:
    if not isinstance(text, str):
        return ""
    # NBSP and the dash family fold to their ASCII kin so a value pasted out of a rich-text store parses.
    low = text.replace(" ", " ").replace("–", "-").replace("—", "-")
    return _WS_RE.sub(" ", low.strip().lower()).rstrip(".")


def _as_fraction(tok: str) -> Fraction:
    """Exact decimal -> Fraction. Parsed from the TEXT (``Fraction("3.3")``), never from a float,
    so ``3.3`` is 33/10 rather than the binary approximation whose ``limit_denominator`` is noise."""
    return Fraction(tok)


def _rate_spelling(x: Fraction):
    """``every x days`` re-spelled as a rate, or ``None`` when no sane rate says it.

    One event per ``p/q`` days is ``q`` events per ``p`` days, so the numerator and denominator swap
    roles. ``3.5`` -> ``2 per 7 days``. Returns ``None`` when the implied rate is more than one event
    per day, where the answer is ``Multiple/day``, not a rate."""
    if x <= 0:
        return None
    n, d = x.denominator, x.numerator
    if d < 2 or n > d:
        return None
    return "%d per %d days" % (n, d)


def _refuse_fraction(x: Fraction, raw: str) -> None:
    spelling = _rate_spelling(x)
    if spelling:
        raise CadenceError(
            "%r: a fractional interval cannot fire — the due test is a date comparison and the seed "
            "runs once a day. Write it as a rate: %r." % (raw, spelling))
    raise CadenceError(
        "%r: a fractional interval cannot fire, and this one is more than once a day — use "
        "'multiple/day' and set the row's `Times`." % (raw,))


def _period_days(period: str, raw: str) -> int:
    period = period.strip()
    if period in ("day", "days", "d"):
        raise CadenceError(
            "%r: a per-DAY rate is intraday — use 'multiple/day' and set the row's `Times`." % raw)
    if period in _PERIODS:
        return _PERIODS[period]
    m = _PERIOD_NUM_RE.match(period)
    if not m:
        raise CadenceError("%r: %r is not a period I understand (try '7 days' or 'week')."
                           % (raw, period))
    val = _as_fraction(m.group("num"))
    if val.denominator != 1:
        raise CadenceError("%r: a rate's period must be a whole number of days." % raw)
    if val < 1:
        raise CadenceError("%r: a rate's period must be at least one day." % raw)
    return int(val)


def parse(text) -> Cadence:
    """A ``Cadence`` string -> :class:`Cadence`. Raises :class:`CadenceError` on anything else.

    Accepts, case- and spacing-insensitively:

      * ``daily`` / ``every day`` · ``weekdays`` · ``weekly`` · ``multiple/day`` · ``one-off``
      * ``every N days`` — any positive whole N. **This is the enumeration's replacement**: 11 days
        needs nothing added anywhere.
      * ``N per <period>`` (also ``N/period``, ``N x period``) where period is ``week``,
        ``fortnight``, ``month``, ``year`` or a whole number of days — ``2 per week``,
        ``3 per 10 days``, ``2 per 7``.

    Refuses, with a message naming the working spelling: a fractional interval (see
    :func:`_refuse_fraction`), a per-day rate, a rate with more events than days, ``every 0 days``."""
    raw = text if isinstance(text, str) else ""
    s = _normalize(raw)
    if not s:
        raise CadenceError("an empty cadence says nothing about when the row is due")

    lit = _LITERALS.get(s)
    if lit:
        return Cadence(lit, raw=raw)

    m = _INTERVAL_RE.match(s)
    if m:
        val = _as_fraction(m.group("num"))
        if val.denominator != 1:
            _refuse_fraction(val, raw)
        if val < 1:
            raise CadenceError("%r: an interval must be at least one day." % raw)
        n = int(val)
        return Cadence(DAILY, raw=raw) if n == 1 else Cadence(INTERVAL, n=n, raw=raw)

    m = _RATE_RE.match(s)
    if m:
        cnt = _as_fraction(m.group("num"))
        if cnt.denominator != 1:
            raise CadenceError("%r: a rate's event count must be a whole number." % raw)
        if cnt < 1:
            raise CadenceError("%r: a rate needs at least one event." % raw)
        n, d = int(cnt), _period_days(m.group("period"), raw)
        if n > d:
            raise CadenceError(
                "%r: %d events in %d days is more than once a day — use 'multiple/day' and set the "
                "row's `Times`." % (raw, n, d))
        if n == d:
            return Cadence(DAILY, raw=raw)
        return Cadence(RATE, n=n, d=d, raw=raw)

    raise CadenceError(
        "%r is not a cadence I understand. Try 'daily', 'weekdays', 'every N days', "
        "'N per week', 'weekly', 'multiple/day' or 'one-off'." % raw)


def try_parse(text):
    """:func:`parse`, or ``None`` — for the read paths that must never raise."""
    try:
        return parse(text)
    except CadenceError:
        return None


def is_multi_fire(text) -> bool:
    """Does this cadence fire many times a day?

    **Never narrower than the substring test it replaced.** ``reminders_acks.id_cache_titles`` used
    ``"multiple" in cadence.lower()`` to mirror a queue entry's ``ack_gate: false``; a stricter
    predicate would newly *gate* rolls that have always been exempt, which is a silent-suppression
    bug. So an unparseable value falls back to exactly that substring."""
    cad = try_parse(text)
    if cad is not None:
        return cad.shape == MULTIPLE
    return MULTI_FIRE_TOKEN in (text.lower() if isinstance(text, str) else "")


# --- the due test -----------------------------------------------------------------------------

def notes_weekday(notes):
    """The ``date.weekday()`` a ``Weekly`` row's ``Notes`` pins it to, or ``None``.

    Only consulted for ``WEEKLY`` — the "if ``Notes`` names a weekday (e.g. 'Fridays'), due that
    weekday" rule. Two named days is ``None``: ambiguous, so fall back to the +7 rule rather than
    picking one."""
    if not isinstance(notes, str) or not notes.strip():
        return None
    low = notes.lower()
    hits = {i for i, name in enumerate(WEEKDAY_NAMES)
            if re.search(r"\b(?:%s|%s)s?\b" % (name, name[:3]), low)}
    return hits.pop() if len(hits) == 1 else None


def next_due(cadence, last_ack, today=None, notes=None, due_target=None,
             lookahead_days: int = ONE_OFF_LOOKAHEAD_DAYS):
    """The next date this row is due, or ``None`` when the shape has no next-date answer.

    ``cadence`` may be a string or a :class:`Cadence`; a string that will not parse raises. Every
    shape whose answer is "some day, decided by something else" — ``DAILY``, ``WEEKDAYS``,
    ``MULTIPLE``, and ``ONE_OFF`` with no ``Due / Target`` — returns ``None``, and
    :func:`is_due` answers those directly.

    ``lookahead_days`` only affects ``ONE_OFF``: how many days before ``due_target`` it starts
    reading as due. Defaults to :data:`ONE_OFF_LOOKAHEAD_DAYS` (right for a Deadline Watch); the
    caller passes ``0`` for a Today Todo so an appointment doesn't surface early."""
    cad = cadence if isinstance(cadence, Cadence) else parse(cadence)
    today = today or _owner_today()
    if cad.shape == INTERVAL:
        return today if last_ack is None else last_ack + timedelta(days=cad.n)
    if cad.shape == RATE:
        return today if last_ack is None else next_rate_day(cad.n, cad.d, last_ack)
    if cad.shape == WEEKLY:
        wd = notes_weekday(notes)
        if wd is None:
            return today if last_ack is None else last_ack + timedelta(days=7)
        ahead = (wd - today.weekday()) % 7
        return today + timedelta(days=ahead)
    if cad.shape == ONE_OFF and due_target is not None:
        return due_target - timedelta(days=lookahead_days)
    return None


def is_due(cadence, today=None, last_ack=None, notes=None, due_target=None,
           lookahead_days: int = ONE_OFF_LOOKAHEAD_DAYS) -> bool:
    """Is a row with this cadence due on ``today``?

    **Fails open in every direction.** An unparseable cadence is due (and the caller reports it —
    see the module docstring); an interval or rate with no ``Last Acknowledged`` is due; a
    ``One-off`` with no ``Due / Target`` is due. A reminder delivered too often is a nuisance; one
    silently never delivered is the failure a reminder system exists to prevent.

    ``ONE_OFF`` is the one shape that needs a field beyond ``Last Acknowledged``: it is due while its
    ``Due / Target`` is today, overdue, or within ``lookahead_days`` (default
    :data:`ONE_OFF_LOOKAHEAD_DAYS`, right for a Deadline Watch surfacing an approaching deadline
    early — **not** right for a Today Todo appointment, which the caller asks for with
    ``lookahead_days=0`` so it never reads as due before its own day). Retirement is a separate rule
    the seed owns (``references/reminders-policy.md`` -> "Auto-retire a completed One-off"), and this
    function deliberately does not model it."""
    today = today or _owner_today()
    cad = cadence if isinstance(cadence, Cadence) else try_parse(cadence)
    if cad is None:
        return True  # fail open, loudly — the caller reports the parse error
    if cad.shape in (DAILY, MULTIPLE):
        return True
    if cad.shape == WEEKDAYS:
        return today.weekday() < 5
    if cad.shape == ONE_OFF:
        return due_target is None or today >= due_target - timedelta(days=lookahead_days)
    if cad.shape == WEEKLY and notes_weekday(notes) is not None:
        return today.weekday() == notes_weekday(notes)
    if last_ack is None:
        return True
    nxt = next_due(cad, last_ack, today=today, notes=notes, lookahead_days=lookahead_days)
    return nxt is None or today >= nxt


# --- the migration pre-flight -----------------------------------------------------------------

def id_cache_cadences(state_dir: str = DEFAULT_STATE_DIR) -> list:
    """``[(title, cadence_string)]`` from ``state/reminders-id-cache.md`` — the only list of live ⏰
    rows a stdlib process on this host can see. Same table-shape parse as
    ``reminders_acks.id_cache_titles`` (a first cell that is the title, a second that is a 32-hex
    page id), so the header and the ``|---|`` separator fall out for free.

    This is the **migration pre-flight**: run ``--audit`` on the daemon's checkout to prove every
    live row parses BEFORE anyone retypes the store property. Total — an absent cache is ``[]``."""
    rows = []
    try:
        with open(os.path.join(state_dir, ID_CACHE_FILE), "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except Exception:  # noqa: BLE001 — an absent cache is the normal case on a fresh checkout
        return rows
    for line in lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        if not _HEX32_RE.match(cells[1].strip("`").replace("-", "").lower()):
            continue
        title = cells[0].strip("`* ")
        if title:
            rows.append((title, cells[4]))
    return rows


def audit(pairs) -> dict:
    """``[(label, cadence)]`` -> a verdict per row plus totals. ``ok`` is False if any row fails."""
    out = {"rows": [], "total": 0, "parsed": 0, "failed": 0, "ok": True}
    for label, text in pairs:
        out["total"] += 1
        try:
            cad = parse(text)
        except CadenceError as exc:
            out["failed"] += 1
            out["ok"] = False
            out["rows"].append({"label": label, "cadence": text, "ok": False, "error": str(exc)})
            continue
        out["parsed"] += 1
        out["rows"].append({"label": label, "cadence": text, "ok": True,
                            "shape": cad.shape, "canonical": cad.canonical()})
    return out


# --- CLI --------------------------------------------------------------------------------------

def _as_date(s):
    return None if not s else date.fromisoformat(s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="⏰ cadence — parse, spread, and test due-ness.")
    ap.add_argument("--parse", metavar="CADENCE", help="parse one expression and print it")
    ap.add_argument("--gaps", metavar="CADENCE", help="print a rate's even-spread gap sequence")
    ap.add_argument("--due", metavar="CADENCE", help="is a row with this cadence due?")
    ap.add_argument("--last-ack", help="Last Acknowledged, YYYY-MM-DD")
    ap.add_argument("--due-target", help="Due / Target, YYYY-MM-DD (One-off only)")
    ap.add_argument("--lookahead-days", type=int, default=None,
                    help="One-off only: days before --due-target it starts reading as due. Default "
                         "%d (a Deadline Watch); pass 0 for a Today Todo so it never surfaces before "
                         "its own day." % ONE_OFF_LOOKAHEAD_DAYS)
    ap.add_argument("--today", help="override today's date, YYYY-MM-DD (testing)")
    ap.add_argument("--notes", default="", help="the row's Notes (a Weekly row may name a weekday)")
    ap.add_argument("--audit", action="store_true",
                    help="parse every live row's cadence — the migration pre-flight")
    ap.add_argument("--from-stdin", action="store_true",
                    help="with --audit: read one cadence per line from stdin instead")
    ap.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    args = ap.parse_args(argv)

    try:
        if args.parse:
            print(json.dumps(parse(args.parse).as_dict(), ensure_ascii=False))
            return 0
        if args.gaps:
            cad = parse(args.gaps)
            if cad.shape != RATE:
                print("! %s is not a rate — gaps apply to 'N per D days' only" % cad.canonical(),
                      file=sys.stderr)
                return 2
            print(json.dumps({"cadence": cad.canonical(), "gaps": gaps(cad.n, cad.d)}))
            return 0
        if args.due:
            today = _as_date(args.today) or _owner_today()
            cad = parse(args.due)
            lookahead = (ONE_OFF_LOOKAHEAD_DAYS if args.lookahead_days is None
                         else args.lookahead_days)
            nxt = next_due(cad, _as_date(args.last_ack), today=today, notes=args.notes,
                           due_target=_as_date(args.due_target), lookahead_days=lookahead)
            print(json.dumps({
                "cadence": cad.canonical(), "today": today.isoformat(),
                "due": is_due(cad, today=today, last_ack=_as_date(args.last_ack),
                              notes=args.notes, due_target=_as_date(args.due_target),
                              lookahead_days=lookahead),
                "next_due": nxt.isoformat() if nxt else None,
            }))
            return 0
    except CadenceError as exc:
        print("! %s" % exc, file=sys.stderr)
        return 2

    if args.audit:
        if args.from_stdin:
            pairs = [(line.strip(), line.strip()) for line in sys.stdin if line.strip()]
        else:
            pairs = id_cache_cadences(args.state_dir)
        result = audit(pairs)
        for row in result["rows"]:
            if not row["ok"]:
                print("! %s: %s" % (row["label"], row["error"]), file=sys.stderr)
        print(json.dumps({k: v for k, v in result.items() if k != "rows"}))
        return 0 if result["ok"] else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
