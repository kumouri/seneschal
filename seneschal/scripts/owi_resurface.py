#!/usr/bin/env python3
"""The positive-resurfacing instrument — how often owner's-move open work goes un-raised.

The assistant owes the owner more than silence: unfinished work and unanswered questions should come
back up. An open-work item whose move is the owner's — `whose_move: "owner"`, and `"both"` counts
too — that has gone longer than `loops.REASK_AFTER_DAYS` without the assistant raising it is a miss
on the assistant's side, and the rate is measured rather than felt: silence cannot discharge the
obligation, so the metric cannot be gamed by doing nothing. This module is that measurement. It is
report-only — `report` computes the rate and writes nothing; `log` is the only thing that appends
anywhere, and it appends one row.

## What counts as "raised" — `last_raised_at`, not `last_touched`

`loops.py::raise_item` stamps `last_raised_at` when the assistant actually surfaces an item to the
owner. This module scores against THAT field, never `last_touched`: a routine `update`/`hold`/`drop`
moves `last_touched` without anything having been said to the owner, and scoring off it would let an
unrelated edit read as a resurfacing. A record with no `last_raised_at` at all (one nobody has ever
raised) is a miss by construction — silence discharges a never-raised item no more than one raised
once and then left to go stale.

## Eligibility — the dormant-at-epoch carve-out, and why dormant items are NOT excluded otherwise

The eligible population is every **non-terminal** row with `whose_move` in `("owner", "both")`,
**excluding only** a row `loops.dormant_at_epoch()` finds in the optional
`state/owi-dormant-at-epoch.json` snapshot — work already dormant when measurement began is not a
miss the instrument can fairly charge. **A row that goes dormant AFTER that point is deliberately NOT
excluded**: reaching dormancy without ever having been raised is exactly the failure this instrument
exists to count, so excluding a currently-dormant row would hide it. `external`/`assistant`/`unknown`
are never eligible — this instrument scores only work genuinely gated on the owner.

## Report-only

`report()` reads the store and the optional `owi-epoch.json`/`owi-dormant-at-epoch.json` pair (absent
on a fresh install — then nothing is carved out and the epoch reports as unset) and returns a dict.
It never calls `loops.save`, `loops.raise_item`, or any other writer — nothing here marks an item
raised, drafts a resurfacing message, or nudges anyone. `log_run()` is the one function that writes,
and it writes exactly one thing: a JSON line appended to `state/owi-resurface.jsonl`, via
`stateio.append_jsonl`. Wired into Dream as one nightly call (`../modes/dream.md` step 2, beside the
other report-only sweeps) so the series exists to read.

CLI:
    owi_resurface.py report [--state-dir ...] [--json]   # the rate, READ-ONLY — writes nothing
    owi_resurface.py log    [--state-dir ...] [--json]   # report, then append one row to
                                                          # state/owi-resurface.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import loops  # noqa: E402
import stateio  # noqa: E402

LOG_FILE = "owi-resurface.jsonl"
LOG_SCHEMA = "seneschal.owi-resurface/1"

#: When measurement began (`{"epoch_at": "<iso>"}`) — optional, written by a one-time register
#: migration if the install had one. Read DIRECTLY as plain JSON, the same way
#: `loops.dormant_at_epoch` reads its snapshot: nothing here depends on a migration script.
EPOCH_FILE = "owi-epoch.json"

#: The two `whose_move` values this obligation is about. `external`/`assistant`/`unknown` are never
#: eligible: this instrument measures what is genuinely gated on the owner, not on the assistant, on
#: someone the owner cannot hurry, or on a value nobody has classified yet.
ELIGIBLE_WHOSE_MOVE = ("owner", "both")


def _epoch_at(state_dir: str | None = None):
    """`owi-epoch.json`'s `epoch_at`, or `None` if the migration has never fired. Fails to `None` on
    any read problem — missing, unreadable, malformed — never to a guessed timestamp: an absent
    epoch is honestly "measurement hasn't started", not a reason to invent a start date."""
    path = os.path.join(state_dir or loops.default_state_dir(), EPOCH_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("epoch_at")


def eligible_items(store: dict, state_dir: str | None = None) -> list:
    """Non-terminal rows with `whose_move` in `ELIGIBLE_WHOSE_MOVE`, excluding the permanent
    dormant-at-epoch carve-out. A row dormant for reasons OTHER than the epoch snapshot — including
    one that went dormant well after it — stays eligible, deliberately (see module docstring)."""
    rows = []
    for item in store["items"].values():
        if item.get("status") in loops.TERMINAL_STATUSES:
            continue
        if item.get("whose_move") not in ELIGIBLE_WHOSE_MOVE:
            continue
        if loops.dormant_at_epoch(item, state_dir):
            continue
        rows.append(item)
    return sorted(rows, key=lambda r: (r.get("opened") or "", r.get("id") or ""))


def _is_missed(item: dict, now) -> bool:
    """The miss test: has `item` gone longer than `loops.REASK_AFTER_DAYS` since it was last RAISED
    (never `last_touched` — see the module docstring)? An absent `last_raised_at` — one the
    assistant has simply never raised — is a miss by construction: silence cannot discharge it."""
    raised = loops._parse_stamp(item.get("last_raised_at"))
    if raised is None:
        return True
    if raised.tzinfo is None:
        raised = raised.replace(tzinfo=now.tzinfo)
    return (now - raised) > timedelta(days=loops.REASK_AFTER_DAYS)


def report(state_dir: str | None = None, *, now=None) -> dict:
    """The rate, READ-ONLY. Loads the store and the epoch file and returns a dict; calls no writer
    anywhere in this tree. This is the function `--dry-run`-style callers and tests should use —
    `log_run()` below is the only one that appends anything."""
    reference = loops._now(now)
    store = loops.load(state_dir)
    eligible = eligible_items(store, state_dir)
    missed = [item for item in eligible if _is_missed(item, reference)]
    rate = (len(missed) / len(eligible)) if eligible else None
    return {
        "at": loops._stamp(reference),
        "epoch_at": _epoch_at(state_dir),
        "eligible": len(eligible),
        "missed": len(missed),
        "missed_ids": [item["id"] for item in missed],
        # The drafted re-ask per missed row (`loops.ask_line`) — a `both` row's is the JOINT ask,
        # so the turn that raises it copies that rather than composing a solo nag. Report-only; `log_run` does not persist it (the jsonl schema is
        # unchanged).
        "missed_asks": {item["id"]: loops.ask_line(item) for item in missed},
        "rate": rate,
    }


def log_run(state_dir: str | None = None, *, now=None) -> dict:
    """`report()`, then append exactly ONE row to `state/owi-resurface.jsonl` — the only writer of
    that file. Report-only at the open-work level: nothing here surfaces to the owner, marks an item
    raised, or nudges anyone — it only extends the measurement series `report()` is a single point
    of."""
    result = report(state_dir, now=now)
    row = {"schema": LOG_SCHEMA, **{k: v for k, v in result.items() if k != "missed_asks"}}
    path = os.path.join(state_dir or loops.default_state_dir(), LOG_FILE)
    stateio.append_jsonl(path, row)
    return row


# --------------------------------------------------------------------------- CLI

def _print_human(cmd: str, result: dict) -> None:
    print(f"owi_resurface {cmd} — positive-resurfacing rate")
    epoch = result["epoch_at"] or "UNSET — no measurement epoch recorded on this store"
    print(f"  measuring since epoch {epoch}")
    if result["rate"] is None:
        print("  0 eligible items — no rate to report yet")
    else:
        print(f"  missed {result['missed']} of {result['eligible']} eligible "
              f"— rate {result['rate'] * 100:.1f}%")
    if result["missed_ids"]:
        print("  missed: " + ", ".join(result["missed_ids"]))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="The positive-resurfacing instrument — report-only: computes a rate, never "
                    "surfaces or nudges.")
    p.add_argument("--state-dir", default=None,
                   help="override seneschal/state/ (tests MUST pass this)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="the rate, READ-ONLY — writes nothing anywhere")
    r.add_argument("--json", action="store_true")

    lg = sub.add_parser("log", help="report, then append one row to state/owi-resurface.jsonl")
    lg.add_argument("--json", action="store_true")

    args = p.parse_args(argv)

    result = report(args.state_dir) if args.cmd == "report" else log_run(args.state_dir)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(args.cmd, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
