#!/usr/bin/env python3
"""`state/brief-prestage.json` — the Brief's pre-staged Phase-1 store reads, in their own tiny store.

Part of the context-digest retirement (`../docs/read-first-retirement-spec.md`): the pre-stage
snapshot that used to ride inside the digest gets a store of its own.

## What this is, and the one payload shape it carries

Dream step 1b snapshots the Brief's Phase-1 store reads — open Tasks due/overdue today and
tomorrow, Active/Carrying-Over Important Flags, in-progress Projects — so the morning Brief can skip
its full 4-read batch and delta-check instead (`../store/notion/mapping.md` → Throughput, on the
Notion backend). The snapshot gets its own tiny store rather than being dropped (that would re-open
the rate-limit pressure the cache exists to avoid) or folded into the open-work register (a cache
living inside a durable store is a second copy that drifts).

**One writer (Dream step 1b), one reader (the Brief's Phase 1), by construction** — nothing else in
this tree calls `write()` or `read()`, so the "every durable write target names its reader" test is
passed here rather than merely asserted.

**This module has no opinion on the payload's shape and does not validate it**; it only decides
where the snapshot lives, never what belongs in it. Dream step 1b
(`../modes/dream.md`) and the Brief's Phase 1 (`../modes/brief.md`) are the two sites that own the
actual shape (tasks / flags / projects, each item carrying whatever `name`/`id`/`url` fields the
Brief already cites). Moving the container never touches the contents.

## `fetched_at`, and why staleness is the READER's decision

Every write stamps `fetched_at` (UTC, ISO 8601, second precision) at write time. `read()` takes
`max_age_hours` (default `DEFAULT_MAX_AGE_HOURS`) and refuses — never guesses — a payload older than
that, or a store that is missing, unreadable, or shaped wrong. In every one of those cases the
Brief's own instruction is to fall back to its live 4-read batch; this module's job is only to say
whether it may skip that, and one honest line about why not when it may not.

The default is **not** the nominal Dream→Brief gap (a late-evening Dream and an early-morning
Brief sit roughly 8-9 h apart) — it is that gap plus BOTH slots' own catch-up grace
(`--slot-catchup-min`, `SCHEDULING.md`), so a Dream that ran late and a Brief that catches up late
don't manufacture a false staleness verdict against each other, while a payload left over from the
PRIOR night (24 h+ old by the time of the next read) still reads as stale rather than as tonight's.
12 h sits between those: past the ordinary gap, well clear of the 24 h floor a stuck Dream would hit.

## Atomicity

`write()` goes through `stateio.write_json_atomic` — build-then-`os.replace`. `read()`/`status()`
are plain tolerant reads; nothing here reaches for a bare `open(p, "w")`.

CLI:
    python brief_prestage.py write                          # payload JSON on stdin, atomic write
    python brief_prestage.py read [--max-age-hours N]        # payload JSON on stdout, or exit 3
    python brief_prestage.py status                          # exists / fetched_at / age_hours / fresh
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import paths
import stateio

FILENAME = "brief-prestage.json"
SCHEMA = "seneschal.brief-prestage/1"

#: See the module docstring's "fetched_at" section: past the nominal Dream→Brief gap plus catch-up
#: slack, and well under the 24h floor that would start conflating tonight's payload with last
#: night's.
DEFAULT_MAX_AGE_HOURS = 12.0

#: `read()`'s exit code on EITHER failure shape (stale or unreadable) — the Brief's fallback is the
#: same live 4-read batch either way, so the two are not distinguished by exit code, only by the
#: printed reason.
EXIT_READ_REFUSED = 3


def _path(state_dir: str | None = None) -> str:
    return os.path.join(paths.state_dir(state_dir), FILENAME)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(raw) -> datetime | None:
    try:
        return datetime.strptime(str(raw), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class Stale(Exception):
    """`read()` found a real store, but it is older than `max_age_hours` allowed. `.reason` is the
    one-line explanation the Brief's fallback message can use verbatim."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Unreadable(Exception):
    """`read()` found no usable store at all — absent, unparseable JSON, or missing the fields this
    module itself always writes. `.reason` is the one-line explanation."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def write(payload, *, state_dir: str | None = None, now: datetime | None = None) -> dict:
    """Stamp `payload` with `fetched_at` (UTC) and atomically replace the store. Returns the record
    actually written, so the CLI can echo confirmation back to Dream step 1b's own log.

    `payload` is stored VERBATIM under `"payload"` — this function does not validate its shape (the
    module docstring's tasks/flags/projects shape is Dream's and the Brief's own contract, not a
    schema enforced here), so a field either side adds later costs no change to this module."""
    record = {"schema": SCHEMA, "fetched_at": _stamp(now), "payload": payload}
    stateio.write_json_atomic(_path(state_dir), record)
    return record


def read(*, max_age_hours: float = DEFAULT_MAX_AGE_HOURS, state_dir: str | None = None,
          now: datetime | None = None):
    """The payload, if a fresh one exists. Raises `Unreadable` (absent / corrupt / wrong-shape) or
    `Stale` (a real payload, too old) — never returns a guess and never invents a payload. The
    caller's job on either exception is the live 4-read batch; this function's job ends at telling it
    whether that is necessary."""
    path = _path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        raise Unreadable(f"no {FILENAME} — Dream step 1b has not run yet") from None
    except (OSError, ValueError) as exc:
        raise Unreadable(f"{FILENAME} unreadable: {exc}") from None
    if not isinstance(record, dict) or "payload" not in record or "fetched_at" not in record:
        raise Unreadable(f"{FILENAME} is missing fetched_at/payload — wrong shape")
    fetched_at = _parse_stamp(record.get("fetched_at"))
    if fetched_at is None:
        raise Unreadable(f"{FILENAME}'s fetched_at is unparseable")
    now = now or datetime.now(timezone.utc)
    age_hours = (now - fetched_at).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        raise Stale(f"{FILENAME} is {age_hours:.1f}h old (fetched_at={record['fetched_at']}), "
                    f"past the {max_age_hours:.1f}h freshness window")
    return record["payload"]


def status(*, state_dir: str | None = None, now: datetime | None = None) -> dict:
    """A dict describing the store's current health for a human/CLI glance — `exists`, `readable`,
    `fetched_at`, `age_hours`, `fresh` (against `DEFAULT_MAX_AGE_HOURS`). Never raises: this is the
    inspection door, not the Brief's own gated `read()`."""
    path = _path(state_dir)
    if not os.path.exists(path):
        return {"exists": False}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"exists": True, "readable": False, "error": str(exc)}
    fetched_at = _parse_stamp(record.get("fetched_at")) if isinstance(record, dict) else None
    if fetched_at is None:
        return {"exists": True, "readable": True, "fetched_at": None, "age_hours": None,
                 "fresh": False}
    now = now or datetime.now(timezone.utc)
    age_hours = (now - fetched_at).total_seconds() / 3600.0
    return {
        "exists": True,
        "readable": True,
        "fetched_at": record.get("fetched_at"),
        "age_hours": round(age_hours, 2),
        "fresh": age_hours <= DEFAULT_MAX_AGE_HOURS,
    }


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="state/brief-prestage.json — write / read / status.")
    ap.add_argument("--state-dir", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("write", help="payload JSON on stdin; atomic write + fetched_at stamp")

    rd = sub.add_parser("read", help="print the payload JSON if fresh, else exit 3 with a reason")
    rd.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)

    sub.add_parser("status", help="exists / fetched_at / age_hours / fresh — never fails")

    args = ap.parse_args(argv)

    if args.cmd == "write":
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError as exc:
            print(f"brief_prestage: stdin is not valid JSON: {exc}", file=sys.stderr)
            return 2
        record = write(payload, state_dir=args.state_dir)
        print(f"brief_prestage: wrote {FILENAME}, fetched_at={record['fetched_at']}")
        return 0

    if args.cmd == "read":
        try:
            payload = read(max_age_hours=args.max_age_hours, state_dir=args.state_dir)
        except (Stale, Unreadable) as exc:
            print(f"brief_prestage: {exc.reason}", file=sys.stderr)
            return EXIT_READ_REFUSED
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    print(json.dumps(status(state_dir=args.state_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
