#!/usr/bin/env python3
"""The cadence check-chain — named, pure predicates over a work-item row (`loops.py`), composed per
row into a chain, deciding whether that row's cadence is due for a recheck today.

## What a check-chain is, and why it is NOT Chain of Responsibility

Cadence can live on the item (flexible, but a number hidden in every row) or on the check
(countable, but inflexible for an odd row). This dissolves the either/or: handlers are defined
CENTRALLY — reviewable, greppable, one place per threshold — and composed per row BY NAME, so an odd
item gets an odd chain without inventing a new number. When an item needs an odd cadence, write a
new handler and make it as general as possible. **The invariant this file enforces: no row carries a
number; rows carry names.** Every threshold lives inside exactly one handler below, so the complete
set of thresholds is what `list_handlers()` / `list` print, never what a docstring claims.

The shape is a **filter chain / middleware pipeline**, deliberately distinct from Chain of
Responsibility, which stops at the first match. This one keeps running while nothing objects.

## The verdict enumeration

A handler never returns a boolean. It returns exactly one of `Verdict`'s four names:

* `INCLUDE` — this handler's predicate matched. The chain KEEPS RUNNING (a disjunction: any
  `INCLUDE` means included, unless something downstream still objects).
* `SKIP` — this handler has no opinion about this row. The chain continues.
* `EXCLUDE` — the row is definitely not due. Stops the chain immediately.
* `ABORT` — the chain itself could not be evaluated (an unknown handler name, or a handler that broke
  its own contract). Stops the chain like `EXCLUDE`, but is reported as `ABORT` — never silently
  downgraded — so a typo in a row's `checks` list is loud in `dry-run` output instead of reading as
  an ordinary, correctly-decided exclusion.

If every handler returns `SKIP`, the row's verdict is `SKIP` — nobody had an opinion, a real,
reportable answer distinct from either `INCLUDE` or `EXCLUDE`.

## No side effects, ever, and a scope anchor

Side effects make dry-runs impossible. Every handler is a PURE predicate over `(row, now, ctx)` — no
store write, no `state/` write, no `print`. `evaluate()` enforces this STRUCTURALLY: the row a handler
receives is wrapped in `types.MappingProxyType`, so an assignment into it raises `TypeError` at the
point of the attempted mutation rather than relying on review to catch it. Actions belong OUTSIDE
this module, hung off a caller that reads a verdict this chain already computed.

**Scope anchor:** this is a CADENCE mechanism, not a policy engine. There is no AND/OR/grouping
grammar and none is planned; "write a new general handler" already absorbs an odd cadence without
one. A future edit adding a combinator must cite a real cadence need, not elegance.

## Store integration — report-only

A `loops.py` row MAY carry an optional `checks: [handler-name, ...]` field naming its own chain.
**A row without one gets `DEFAULT_CHAIN`** — no migration of existing rows is needed for this module
to have an opinion about every row in `state/open-loops.json`. `loops.py` exposes
`cadence_verdict(item, now)` as a thin, lazily-imported wrapper around `evaluate()` (lazy so `loops`
never depends on this module at import time). Nothing here changes what `loops.py render` surfaces;
`dry-run` here, and `loops.py report`/`mine`, show what WOULD be included or excluded today.

## The handler table

Composed in `DEFAULT_CHAIN`'s order — `resolved-excluded`, `dormant-after-14-days`,
`reask-owner-after-10-days` — the two thresholds combine without a fourth handler: a row past 14 days
is `EXCLUDE`d by the dormancy handler before the re-ask handler ever runs, so
`reask-owner-after-10-days` only ever fires inside the 10-14 day window, which leaves a few days to
decide or discover there is time to do it. Order is not cosmetic — swapping it would let a 20-day-old
owner-move row `INCLUDE` before the dormancy handler ever runs.

| handler | verdict | fires when | threshold |
|---|---|---|---|
| `resolved-excluded` | `EXCLUDE` | `status` is terminal (`loops.TERMINAL_STATUSES`) — a closed row has no cadence left | none |
| `dormant-after-14-days` | `EXCLUDE` | `status == "open"`, `last_touched` >14 days old | **14 days** |
| `reask-owner-after-10-days` | `INCLUDE` | `whose_move` is `"owner"` **or** `"both"`, `status == "open"`, `last_touched` >10 days old | **10 days** |

**`loops.py` is the ONE SOURCE OF TRUTH for both numbers** — `REASK_AFTER_DAYS`/`DORMANT_AFTER_DAYS`
below are aliases to `loops.REASK_AFTER_DAYS`/`loops.DORMANT_AFTER_DAYS`, and the two handlers
delegate to `loops.is_dormant`/`loops.needs_reask` rather than to a local copy of either number.

CLI:
    cadence_chain.py list                                   # every handler + its summary
    cadence_chain.py check <item-id> [--state-dir ...]       # one row's verdict, verbose, READ-ONLY
    cadence_chain.py dry-run [--state-dir ...] [--audience ...] [--status ...]   # every row, READ-ONLY
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import types
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import loops  # noqa: E402


class Verdict:
    """The verdict enumeration. Plain string constants rather than `enum.Enum` — matching this
    tree's convention for a fixed vocabulary (`loops.py`'s `STORED_STATUSES` / `KINDS` /
    `PRIORITIES` are tuples of strings), so a verdict serializes into JSON/CLI output as itself."""
    INCLUDE = "INCLUDE"
    SKIP = "SKIP"
    EXCLUDE = "EXCLUDE"
    ABORT = "ABORT"


ALL_VERDICTS = (Verdict.INCLUDE, Verdict.SKIP, Verdict.EXCLUDE, Verdict.ABORT)


class ChainError(Exception):
    """A refusal — this module's sibling to `loops.LoopsError`."""


#: Aliases, not copies: `loops.py` is the one source of truth, so a future change to either number
#: has exactly one place to land rather than two that can drift apart.
REASK_AFTER_DAYS = loops.REASK_AFTER_DAYS
DORMANT_AFTER_DAYS = loops.DORMANT_AFTER_DAYS


# --------------------------------------------------------------------------- the handlers
#
# Every handler here is a PURE predicate: `row` is a `types.MappingProxyType` by the time a handler
# sees it (see `evaluate()`), so an attempted mutation raises `TypeError` rather than depending on a
# reviewer to notice one.

def resolved_excluded(row, now, ctx):
    """EXCLUDE a row whose lifecycle is already over (`loops.TERMINAL_STATUSES`). A closed row is
    never re-checked. SKIP for anything else; this handler takes no stance on live rows."""
    if row.get("status") in loops.TERMINAL_STATUSES:
        return Verdict.EXCLUDE
    return Verdict.SKIP


def dormant_after_14_days(row, now, ctx):
    """EXCLUDE an `open` row nobody has touched in over `DORMANT_AFTER_DAYS`. Reuses
    `loops.is_dormant` — the one definition of "dormant" in this tree — rather than re-deriving the
    arithmetic; `is_dormant` takes a plain `dict`, so `row` is copied out of its `MappingProxyType`
    wrapper for the call rather than mutated. SKIP on anything not `open`, or with an unreadable
    `last_touched` — an unparseable stamp is never treated as fresh OR stale, matching
    `is_dormant`'s own `False`-on-unparseable behaviour."""
    if row.get("status") != "open":
        return Verdict.SKIP
    if loops.is_dormant(dict(row), now=now):
        return Verdict.EXCLUDE
    return Verdict.SKIP


def reask_owner_after_10_days(row, now, ctx):
    """INCLUDE an `open` row whose `whose_move` is `owner` OR `both` and whose `last_touched` is
    older than `REASK_AFTER_DAYS` — the re-ask threshold (a `both` row is gated on the owner and the
    assistant together, and it is still the assistant's move to raise it). Composed AFTER
    `dormant-after-14-days` in `DEFAULT_CHAIN`, this only ever fires inside the 10-14 day window: a
    row already past 14 days is `EXCLUDE`d upstream and this handler never runs for it. Reuses
    `loops.needs_reask` rather than re-deriving the arithmetic. SKIP on anything else, or with an
    unreadable `last_touched`. **This handler decides the ARITHMETIC only** — phrasing a `both` row's
    re-ask as a joint ask rather than a solo nag is `loops.ask_line()`'s job, and this chain stays
    free of side effects regardless."""
    if row.get("status") != "open":
        return Verdict.SKIP
    if loops.needs_reask(dict(row), now=now):
        return Verdict.INCLUDE
    return Verdict.SKIP


#: THE ONE REGISTRY. A handler is reachable by a chain ONLY through this dict — `list_handlers()` and
#: `list` read it directly, so the complete set of handlers (and, via each one's own docstring, the
#: complete set of thresholds) is what running this file prints, never what the module docstring's
#: table claims if the two ever drift apart.
HANDLERS = {
    "resolved-excluded": resolved_excluded,
    "dormant-after-14-days": dormant_after_14_days,
    "reask-owner-after-10-days": reask_owner_after_10_days,
}

#: Applied when a `loops.py` row carries no `checks` field, so no migration of existing rows is
#: required for this module to have an opinion about every row. Order matters (see
#: `reask-owner-after-10-days`'s own docstring): the terminal check first, then the wider exclusion,
#: then the narrower inclusion.
DEFAULT_CHAIN = ("resolved-excluded", "dormant-after-14-days", "reask-owner-after-10-days")


def list_handlers() -> list:
    """Every handler this file knows: name + its docstring's first line. What `list` prints, and
    what a reader should trust over this module's own docstring table if the two ever disagree —
    this reads the live registry, the table above is prose."""
    rows = []
    for name, fn in HANDLERS.items():
        first_line = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        rows.append({"name": name, "summary": first_line})
    return rows


# --------------------------------------------------------------------------- the chain

def evaluate(row: dict, now: datetime, *, ctx: dict | None = None) -> dict:
    """Run `row`'s own chain (`row["checks"]`, or `DEFAULT_CHAIN` if absent/empty) and return its
    verdict as `{"verdict", "decided_by", "reason"}`.

    **Filter chain, not Chain of Responsibility:** every handler runs in order. `EXCLUDE` and `ABORT`
    stop it immediately and are reported verbatim — `ABORT` is never silently downgraded to
    `EXCLUDE`, so a typo in `checks` reads as a configuration problem, not a quiet exclusion.
    `INCLUDE` is remembered but does NOT stop the chain — a later handler can still exclude it.
    `SKIP` has no effect either way. If nothing ever returns `INCLUDE` or `EXCLUDE`, the row's
    verdict is `SKIP` — a real, reportable "nobody had an opinion."

    **No side effects, enforced:** `row` is wrapped in `types.MappingProxyType` before any handler
    sees it, so a handler that assigns into it raises `TypeError` at the attempted mutation."""
    names = row.get("checks") or DEFAULT_CHAIN
    frozen = types.MappingProxyType(dict(row))
    context = ctx if ctx is not None else {}

    included_by = None
    for name in names:
        handler = HANDLERS.get(name)
        if handler is None:
            return {"verdict": Verdict.ABORT, "decided_by": None,
                    "reason": f"unknown handler {name!r} — not in the registry "
                              f"({', '.join(sorted(HANDLERS))}). Refused, never guessed at."}
        verdict = handler(frozen, now, context)
        if verdict not in ALL_VERDICTS:
            return {"verdict": Verdict.ABORT, "decided_by": name,
                    "reason": f"handler {name!r} returned {verdict!r}, not one of "
                              f"{ALL_VERDICTS} — a handler returns the enumeration, never a "
                              f"boolean."}
        if verdict in (Verdict.EXCLUDE, Verdict.ABORT):
            return {"verdict": verdict, "decided_by": name,
                    "reason": f"{name!r} returned {verdict}"}
        if verdict == Verdict.INCLUDE and included_by is None:
            included_by = name
        # SKIP: no opinion, continue to the next handler.

    if included_by is not None:
        return {"verdict": Verdict.INCLUDE, "decided_by": included_by,
                "reason": f"{included_by!r} returned INCLUDE and nothing downstream excluded it"}
    return {"verdict": Verdict.SKIP, "decided_by": None,
            "reason": "no handler in the chain expressed an opinion"}


def dry_run_store(state_dir: str | None = None, *, audience: str | None = None,
                  status: str | None = None, now: datetime | None = None) -> list:
    """Every row in the store (optionally filtered by `audience`/`status`), each with its verdict.
    READ-ONLY — this loads the store and evaluates the chain; it writes nothing anywhere, which is
    exactly what makes a dry-run possible at all."""
    store = loops.load(state_dir)
    reference = now if now is not None else loops._now()
    rows = loops.items(store, audience=audience, status=status)
    return [{"id": row["id"], **evaluate(row, reference)} for row in rows]


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="The cadence check-chain — named pure handlers, an enumeration verdict, no "
                    "side effects. READ-ONLY: nothing here writes.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="every handler + its summary, from the live registry")

    c = sub.add_parser("check", help="one row's verdict, verbose")
    c.add_argument("item_id")
    c.add_argument("--state-dir", default=None)

    d = sub.add_parser("dry-run", help="every row in the store: verdict + deciding handler. "
                                       "Report-only — changes nothing.")
    d.add_argument("--state-dir", default=None)
    d.add_argument("--audience", default=None, choices=loops.AUDIENCES)
    d.add_argument("--status", default=None, choices=loops.RENDERED_STATUSES)

    args = p.parse_args(argv)

    if args.cmd == "list":
        for row in list_handlers():
            print(f"{row['name']}\t{row['summary']}")
        return 0

    if args.cmd == "check":
        try:
            store = loops.load(args.state_dir)
            item = loops.get(store, args.item_id)
        except loops.LoopsError as exc:
            print(f"cadence_chain: refused — {exc}", file=sys.stderr)
            return 2
        result = evaluate(item, loops._now())
        print(json.dumps({"id": item["id"], **result}, ensure_ascii=False, indent=2))
        return 0

    # dry-run
    try:
        results = dry_run_store(args.state_dir, audience=args.audience, status=args.status)
    except loops.LoopsError as exc:
        print(f"cadence_chain: refused — {exc}", file=sys.stderr)
        return 2
    for r in results:
        print(f"{r['id']}\t{r['verdict']}\t{r['decided_by']}\t{r['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
