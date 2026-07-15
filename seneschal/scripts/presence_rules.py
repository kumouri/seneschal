#!/usr/bin/env python3
"""Pure, local rule logic for the presence feed — **no I/O, no Notion, no daemon coupling**, so it's
fully unit-testable and safe to reason about in isolation. Modeled on the tracked, no-LLM shape of
``reminders_roll.py``.

The rules (all **defer, never drop** — decision #2 — so a gated nudge fires *late*, not *never*; only the
explicit quiet window drops):

  * ``require_place: "<name>"`` (Phase 1) — hold until the presence context says the owner is at that place, then
    fire ("release a home-gated nudge on arrival", decision #6). Applies to any entry carrying the field.
  * **driving** (Phase 2) — while ``context['activity'] == 'in_vehicle'``, hold **non-urgent** nudges until
    they're stationary again. ``pierces`` (a Call Me ring / a Critical-and-above item) fires through.

**The asleep rule (Phase 3) was RETIRED 2026-07-13.** The phone-only Sleep API can't tell a sleeping
owner from an idle phone — an owner can leave theirs sitting for hours while wide awake, and it classified
"asleep" at confidence 73–92 through a Monday afternoon, holding their daytime nudges. The context snapshot
still *carries* ``asleep`` (the phone keeps sending sleep events; informational only) but this gate no
longer reads it. Bring the rule back only with a wearable-grade signal — heart rate + wrist motion from a
watch can distinguish "the owner is asleep" from "their phone is bored"; the phone alone cannot.

**This module is pure** — no I/O. The daemon (``sentinel.check_reminders``) reads
``presence-context.json`` (``presence_common.read_context``), only trusts it when *fresh*
(``_presence_context_fresh``, fail-open), and calls ``should_defer(entry, context, pierces)`` in the fire
loop — deferring instead of firing when the gate says so. No-op for any entry the rules don't match.
"""
from __future__ import annotations


def presence_gate(entry: dict, context: dict, pierces: bool = False) -> tuple[bool, str]:
    """Decide whether a due reminder entry may fire *now* given the current presence context.

    Returns ``(fire, reason)``. ``fire=False`` means **defer** — leave the entry pending and re-check next
    loop; it is never dropped. Transparent to ordinary reminders (fire=True) unless a rule matches.

    * ``require_place`` (Phase 1) — fire only when ``context['at_place']`` equals it. Applies to any entry
      carrying the field, piercing or not (you tagged it place-locked, so honor that).
    * **driving** (Phase 2) — while ``context['activity'] == 'in_vehicle'``, hold **non-piercing** nudges
      until they're stationary again. ``pierces`` (a Call Me ring / a Critical-and-above item) fires through.

    ``context['asleep']`` is deliberately ignored (the retired Phase-3 rule — see the module docstring).
    """
    ctx = context or {}
    require_place = entry.get("require_place")
    if require_place and ctx.get("at_place") != require_place:
        return False, f"deferred: waiting to arrive at {require_place!r} (at={ctx.get('at_place')!r})"
    if not pierces and ctx.get("activity") == "in_vehicle":
        return False, "deferred: driving (fires when stationary)"
    return True, "ok"


def should_defer(entry: dict, context: dict, pierces: bool = False) -> bool:
    """Convenience wrapper: True iff [entry] must be held (not fired) under [context]."""
    return not presence_gate(entry, context, pierces)[0]


# --- Daemon integration (wired in sentinel._check_reminders_locked) ---
#
#   ctx = read_context(state_dir)                    # cheap: state/presence-context.json
#   for entry in due_entries:
#       if _presence_context_fresh(ctx, now) and should_defer(entry, ctx, entry_pierces_quiet(entry)):
#           continue                                 # leave pending; next loop re-checks (defer, not drop)
#       fire(entry)
#
# `require_place` is set at enqueue time (reminders_enqueue.py --require-place); the driving rule needs no
# per-entry flag (it holds any non-piercing nudge while in_vehicle). Fail-open: stale/absent presence fires.
