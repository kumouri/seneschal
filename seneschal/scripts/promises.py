#!/usr/bin/env python3
"""The promises ledger, phase 0 — the store and the tool, and nothing else. Standard library only.

Spec: `seneschal/docs/promises-ledger-spec.md` (phase 0; §7 for the definition this module encodes).
Read that first — it is `mouth.py`'s and `turns.py`'s sibling, built to the same shape on purpose:
phase 0 is the artifact every later step reads, and it changes NO behaviour.

## What this is

`state/promises.jsonl` is an append-only record of every commitment the assistant makes — "I'll
open a PR", "I'll check that tomorrow", "I'll bring you the transcript when it lands". `jobs.py`
already proved the pattern for exactly one promise type: move the completion promise from the turn's
own context into a record the daemon owns, because context dies (every merge reload restarts the warm
session) and a record does not. This generalizes that to every promise, not only the ones that happen
to carry a background process.

## What this is NOT (phase 0 is one artifact, on purpose)

**Nothing reads this file yet.** No grounding change, no `/assistant` prompt change, no mode-prompt
instruction to append, no daemon task, no Dream sweep, no nag, no orientation read. Those are phase
1+ and they are the owner's to approve. This phase only builds the place a promise CAN be written down
and the tool to write and resolve one by hand or from a caller that already decided to.

## The definition this store enforces (spec §4)

A promise is a first-person, UNCONDITIONAL statement committing the assistant to a nameable future
action,
whose completion is a discrete event a later turn could check — `loops.py`'s membership test
("can you name the event that would close this?") applied to speech instead of to a work item. It is
deliberately narrower than every "I'll " in a transcript: an offer contingent on the owner's reply
("say the word and I'll…") is not yet a promise, a quotation of someone else's words is not a
promise the assistant made, and a standing behavioural disposition with no discrete completion event
("I'll read them at source or not say them") has nothing this store's `status` field could ever
resolve to. This module does not enforce the definition in code — a caller decides what counts —
it only refuses to accept a record with
no `text` at all.

## Two invariants, and they are the whole contract (the `mouth.py`/`turns.py` family's own two)

1. **Recording never costs the thing that made the promise true.** `record_promise` returns the row
   or `None`, and never raises — a full disk or a clobbered file costs the row, never the reply that
   made the promise, exactly as `mouth.record_assertion` and `turns.record_turn` are contracted.
2. **A resolution is an APPEND, never a rewrite.** `resolve` writes a second, partial row for the
   same `id`; the append-only fold in `_fold` (mouth.py's outbound-queue `state`-row pattern,
   `pending()`/`_mark()`) is what makes the last word for an id win with no lock and no rewrite of a
   file several writers could be appending to at once.

## Where the identity fields come from

`origin_hand` is `mouth.origin_hand()`, reused rather than re-invented — one notion of who is
speaking, not two — and that module already carries the `source`-reads-`build`-for-a-warm-turn caveat
(`mouth-spec.md` §9.4) so this store does not have to re-learn it. `job_id` is a reference, never a
duplicate: if a promise is carried by a `jobs.py` job, this row points at it rather than copying its
state, so the two can never disagree about whether the job finished.

USAGE:
  python promises.py record --text "I'll open a PR for the flaky-test fix" [--speaker daemon]
                             [--due 2026-09-06T00:00:00Z] [--job-id 20260905-220512-a3f1]
  python promises.py list-open [--json]
  python promises.py resolve --id 20260905-220512-a3f1 --status kept --reason "the PR merged"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import mouth  # noqa: E402 — origin_hand(), reused rather than re-invented (spec §5)
import paths  # noqa: E402
import stateio  # noqa: E402

SCHEMA = "seneschal.promise/1"
PROMISES_FILE = "promises.jsonl"

# `kept` / `dropped` are the only terminal values `resolve` accepts. Unlike mouth.py's `KINDS` this
# vocabulary is NOT validated loosely at the `resolve` boundary — a typo'd status would silently
# leave a promise looking open forever, which is the one failure this tiny store cannot afford.
TERMINAL_STATUSES = ("kept", "dropped")


def promises_path(state_dir: str | None = None) -> str:
    return os.path.join(paths.state_dir(state_dir), PROMISES_FILE)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def record_promise(state_dir: str | None = None, *, text: str, speaker: str | None = None,
                    due=None, job_id: str | None = None, now: datetime | None = None,
                    origin: dict | None = None) -> dict | None:
    """Append one open promise. Returns the row that was written, or `None` if it could not be.

    **Never raises** — same contract as `mouth.record_assertion` / `turns.record_turn`: every
    caller is already past the point where the promise was made (spoken, or decided), so a failed
    append must cost the row and nothing upstream. No caller should wrap this in a try/except.

    `text` is verbatim and uncapped, the same reasoning `turns.jsonl` rests on: a promise ledger
    that paraphrased what was promised would be trusted exactly as far as the paraphrase is
    accurate, and that is not a property this store can guarantee for itself.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        now = now or datetime.now(timezone.utc)
        row = {
            "schema": SCHEMA,
            "id": f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}",
            "promised_at": _stamp(now),
            "origin_hand": origin if isinstance(origin, dict) else mouth.origin_hand(state_dir, speaker=speaker),
            "text": text,
            "due": _stamp(due) if due is not None else None,
            "job_id": str(job_id) if job_id else None,
            "status": "open",
        }
        stateio.append_jsonl(promises_path(state_dir), row)
        return row
    except Exception:  # noqa: BLE001 — the row, never the promise it describes
        return None


def _fold(state_dir: str | None = None) -> list:
    """Every promise id, folded to its newest fields.

    `resolve` writes a second, partial row for an existing id rather than rewriting the first one —
    an append-only log with several potential writers cannot be safely rewritten in place
    (`mouth.py`'s outbound queue made this exact argument for `state`-only rows). Unknown fields on
    either row are tolerated, never rejected: a schema field a future phase adds must not make an
    older row unreadable."""
    final: dict = {}
    order: list = []
    for row in stateio.iter_jsonl(promises_path(state_dir)):
        rid = row.get("id")
        if not isinstance(rid, str) or not rid:
            continue
        if rid not in final:
            order.append(rid)
        final[rid] = {**final.get(rid, {}), **row}
    return [final[i] for i in order]


def list_open(state_dir: str | None = None) -> list:
    """Every promise still `status: "open"`, oldest first. Fail-open: an absent or unreadable store
    reads empty (`stateio.iter_jsonl`'s own contract), never raises."""
    return [r for r in _fold(state_dir) if r.get("status") == "open"]


def list_all(state_dir: str | None = None) -> list:
    """Every promise, any status, oldest first — for the CLI's `list` and for tests; nothing in the
    tree reads this yet (phase 0's whole point)."""
    return _fold(state_dir)


def resolve(state_dir: str | None = None, *, promise_id: str, status: str,
            reason: str | None = None, now: datetime | None = None) -> bool:
    """Append a resolution row for `promise_id`. Returns whether it landed.

    Refuses (returns `False`, writes nothing) on an unrecognised `status` or a blank `promise_id` —
    the one place this module validates a vocabulary strictly rather than loosely, because a typo'd
    status would leave the promise reading `open` forever with no way to notice. Resolving an id
    that does not exist in the store, or one already resolved, still writes the row: `_fold` takes
    the newest, so a duplicate or out-of-order resolution costs nothing but a redundant line."""
    if status not in TERMINAL_STATUSES:
        return False
    if not isinstance(promise_id, str) or not promise_id.strip():
        return False
    try:
        row = {"schema": SCHEMA, "id": promise_id.strip(), "status": status,
               "resolved_at": _stamp(now)}
        if reason:
            row["reason"] = str(reason)
        stateio.append_jsonl(promises_path(state_dir), row)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------------------------- #


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="The promises ledger (phase 0): record and resolve.")
    p.add_argument("--state-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="append one open promise")
    rec.add_argument("--text", required=True)
    rec.add_argument("--speaker", default=None, help="the calling process's own label for itself")
    rec.add_argument("--due", default=None, help="ISO-8601; optional")
    rec.add_argument("--job-id", default=None, help="a jobs.py id this promise is carried by")
    rec.add_argument("--json", action="store_true")

    lst = sub.add_parser("list-open", help="print every still-open promise")
    lst.add_argument("--json", action="store_true")

    res = sub.add_parser("resolve", help="mark a promise kept or dropped")
    res.add_argument("--id", required=True, dest="promise_id")
    res.add_argument("--status", required=True, choices=list(TERMINAL_STATUSES))
    res.add_argument("--reason", default=None)
    res.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    state_dir = paths.state_dir(args.state_dir)

    if args.cmd == "record":
        row = record_promise(state_dir, text=args.text, speaker=args.speaker, due=args.due,
                              job_id=args.job_id)
        if args.json:
            print(json.dumps({"ok": row is not None, "row": row}, ensure_ascii=False))
        else:
            print(f"{'recorded' if row else 'FAILED'}: {row.get('id') if row else ''}")
        return 0 if row else 1

    if args.cmd == "list-open":
        rows = list_open(state_dir)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for row in rows:
                print(f"{row.get('id')}  {row.get('promised_at')}  {row.get('text')}")
        return 0

    ok = resolve(state_dir, promise_id=args.promise_id, status=args.status, reason=args.reason)
    if args.json:
        print(json.dumps({"ok": ok}, ensure_ascii=False))
    else:
        print("resolved" if ok else "FAILED — unrecognised status or blank id")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
