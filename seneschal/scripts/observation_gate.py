#!/usr/bin/env python3
"""The observation-gate scanner — `../docs/observation-gate-spec.md`.

Some open work is deliberately parked waiting for evidence to accumulate — "two weeks AND >= 30
logged events", "once this job finishes". Without a watcher, such a gate is met silently and nobody
notices for weeks, by which point the data may no longer reflect how things run and a new
observation period is needed. This module is the watcher: it finds every register row in
`observation`, checks whether its requirements are met, and flips a met one to
`observation-complete` so it resurfaces as ready to continue.

**`loops.py` owns the two status values and the two mutations** — its `observe` verb refuses without
a gate, and `mark_observation_complete()` here below is called only by this module's `scan()` — this
module owns the OTHER half: deciding whether a gate's requirements are actually met, and it never
writes to `open-loops.json` directly. Every mutation this module causes goes THROUGH `loops.py`'s
own verbs, the same discipline `owi_resurface.py`/`owi_unknowns.py` already hold, so
`open-loops.json` keeps exactly one writer regardless of how many sibling modules decide WHEN to
call it.

## The requirement vocabulary — `loops.REQUIREMENT_TYPES`

Every requirement is checkable from LOCAL STATE ONLY, never a live network call — a gate that needs
to ask something external is `manual`, surfaced for a human to answer, never guessed:

* **`min_elapsed`** — `{"type": "min_elapsed", "days": N, "since": "..."}` (`since` optional). Met
  when `N` days have passed since `since` if given, else the gate's own `started_at` (stamped by
  `loops.py`'s `observe` verb, never by this module). Without `since`, a backfilled gate for an
  evidence period that began before the gate was attached could never read met on time, because
  `started_at` is always the attachment instant. `since` lets a
  requirement's own clock start earlier than the gate's attachment instant; `started_at` keeps
  meaning "when this gate was attached" and is never itself backdated.
* **`min_rows`** — `{"type": "min_rows", "source": "<path, jsonl or sqlite>", "filter": {...},
  "n": N}`. Met when at least `N` rows in `source` match every key/value pair in `filter` (jsonl:
  top-level key equality per line; sqlite: `table`/`where` sub-keys — see `_count_sqlite_rows`).
  A relative `source` resolves against the repo root (`paths.REPO_ROOT`), same as every other
  tracked-path reference in this tree.
* **`file_exists`** — `{"type": "file_exists", "path": "..."}`. Met when the (resolved) path exists.
* **`job_finished`** — `{"type": "job_finished", "job_id": "..."}`. Met when `jobs.py`'s own record
  for that id has left `running`/`retry-pending` — "finished" means REACHED A TERMINAL STATE,
  success or not, not "succeeded."
* **`manual`** — `{"type": "manual", "note": "..."}`. NEVER auto-satisfied — always reported as a
  human check still owed, exactly as `cadence_chain.py`'s own docstring reserves `ABORT` for
  something the chain genuinely cannot decide rather than silently downgrading it.

**Fail-open on every requirement: an unreadable source, a missing job record, or an unparseable
timestamp all mean `met: None` (unknown) — never `met: True`.** A gate is `met` overall only when
EVERY requirement in it reports `met: True`; one `None`/`False` holds the whole gate, same as one
`False` predicate in an ordinary `all()`.

## The scanner — idempotent, one flip, never closes an item

`scan()` reads every `status == "observation"` row, checks its gate, and for one that is fully met:
calls `loops.mark_observation_complete()` (the status flip + `whose_move` + `gate.met_at`), then
`loops.raise_item()` (so it resurfaces exactly the way any other positively-raised item does,
scored by the same `owi_resurface.py` instrument). **It never touches a row that isn't `observation`, and once a
row has flipped it is no longer `observation`, so re-running `scan()` is a no-op against it** — the
idempotency is structural, not a guard this module has to remember to check. Wired into Dream as
step 2i (`../modes/dream.md`), report-only when `--apply` is omitted.

**Staleness, a REPORT, never a second mutation.** A gate met more than `STALE_AFTER_DAYS` ago with
no follow-up (nothing has touched the record since the flip — `last_touched` still equals
`gate.met_at`) is flagged in `scan()`'s own output and in `brief_line()` — the data it waited on may
no longer reflect how things run. `STALE_AFTER_DAYS` is a default (14, matching the register's own
`DORMANT_AFTER_DAYS`), shipped as a number because no default at all would let the very failure this
module exists to fix repeat indefinitely while a threshold sits unset. The owner's to move.

CLI:
    observation_gate.py check   [--state-dir ...] [--json]   # dry-run over every `observation` row
    observation_gate.py scan    [--state-dir ...] [--apply] [--json]   # Dream's own call
    observation_gate.py gate-show <id> [--state-dir ...]     # one item's gate, verbatim
    observation_gate.py brief-line [--state-dir ...]         # the Brief's lead-with line, or nothing
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import loops  # noqa: E402
import paths  # noqa: E402

#: A tunable default — see the module docstring's "Staleness" section.
STALE_AFTER_DAYS = 14

#: The Brief prints at most this many "ready to continue" lines, so a burst of simultaneous gate
#: completions cannot push the whole Brief off-screen — the same bounded-list posture the register's
#: own `PROJECTION_TRIPWIRE` uses (report the overflow as a count, never truncate silently).
BRIEF_LINE_CAP = 5


def _resolve_path(path: str) -> str:
    """Relative to `paths.REPO_ROOT`; absolute paths pass through unchanged — the same convention
    every tracked-path reference elsewhere in this tree already uses."""
    return path if os.path.isabs(path) else os.path.join(paths.REPO_ROOT, path)


def _row_matches(row: dict, filt: dict) -> bool:
    return all(row.get(k) == v for k, v in filt.items())


def _count_jsonl_rows(path: str, filt: dict) -> int | None:
    """`None` on a missing/unreadable file — fail-open to UNKNOWN, never to zero-or-met. A single
    malformed LINE is skipped, not the whole file — one bad row must not hide every good one."""
    if not os.path.exists(path):
        return None
    try:
        count = 0
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and _row_matches(row, filt):
                    count += 1
        return count
    except OSError:
        return None


_IDENT_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _safe_identifier(name: str) -> bool:
    """A `min_rows` sqlite requirement's `table` and every `where` key must be a plain identifier —
    they are interpolated into SQL text (sqlite3's parameter binding cannot parameterize a table or
    column name), so this is what stands between a gate's own JSON and a SQL-injection surface."""
    return bool(name) and name[0].isalpha() and all(c in _IDENT_OK for c in name)


def _count_sqlite_rows(path: str, table: str, filt: dict) -> int | None:
    """`None` on a missing file, a missing table, an unsafe identifier, or any sqlite error — the
    same fail-open-to-unknown posture as `_count_jsonl_rows`."""
    if not os.path.exists(path):
        return None
    if not _safe_identifier(table) or not all(_safe_identifier(k) for k in filt):
        return None
    where = " AND ".join(f"{k} = ?" for k in filt) or "1=1"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", list(filt.values()))
            return cur.fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _count_rows(req: dict) -> int | None:
    source = _resolve_path(req["source"])
    filt = req.get("filter") or {}
    if "table" in req:
        return _count_sqlite_rows(source, req["table"], filt)
    return _count_jsonl_rows(source, filt)


def check_requirement(req: dict, *, started_at: str | None, state_dir: str | None, now) -> dict:
    """One requirement's verdict: `{"type", "met": True|False|None, "detail": str}`. `met: None`
    means UNKNOWN — a source that can't be read, a job record that doesn't exist, or (for `manual`)
    a question nothing but a human can answer. Never raises: a requirement this module cannot
    interpret (an out-of-vocabulary `type`, which `loops.py`'s `observe` verb should already have refused at
    write time) is reported unknown rather than crashing the whole scan."""
    rtype = req.get("type")

    if rtype == "min_elapsed":
        since = req.get("since")
        clock_label = "since" if since else "started_at"
        clock_start = loops._parse_stamp(since if since else started_at)
        if clock_start is None:
            return {"type": rtype, "met": None, "detail": f"gate has no readable {clock_label}"}
        if clock_start.tzinfo is None:
            clock_start = clock_start.replace(tzinfo=now.tzinfo)
        elapsed = now - clock_start
        met = elapsed >= timedelta(days=req["days"])
        return {"type": rtype, "met": met,
               "detail": f"{elapsed.days}d elapsed of {req['days']}d required (from {clock_label})"}

    if rtype == "min_rows":
        count = _count_rows(req)
        if count is None:
            return {"type": rtype, "met": None,
                   "detail": f"{req['source']} unreadable — cannot count rows"}
        return {"type": rtype, "met": count >= req["n"],
               "detail": f"{count} of {req['n']} required rows matched"}

    if rtype == "file_exists":
        try:
            met = os.path.exists(_resolve_path(req["path"]))
        except OSError:
            return {"type": rtype, "met": None, "detail": f"could not check {req['path']}"}
        return {"type": rtype, "met": met, "detail": req["path"]}

    if rtype == "job_finished":
        import jobs  # lazy — same reason loops.py lazily imports cadence_chain: no import-time coupling
        rec = jobs.load_job(state_dir or loops.default_state_dir(), req["job_id"])
        if rec is None:
            return {"type": rtype, "met": None,
                   "detail": f"no record for job {req['job_id']}"}
        status = rec.get("status")
        met = status not in (jobs.RUNNING, jobs.RETRY_PENDING)
        return {"type": rtype, "met": met, "detail": f"job {req['job_id']} is {status}"}

    # `manual` and anything unrecognised: always a human question, never guessed.
    return {"type": rtype, "met": None, "detail": req.get("note") or "manual — needs a human check"}


def check_gate(gate: dict, *, state_dir: str | None = None, now=None) -> dict:
    """Every requirement's verdict, and the gate's own: met only when every requirement is `met:
    True`. `now` defaults to the real clock, same convention as `loops._now`."""
    reference = loops._now(now)
    results = [check_requirement(req, started_at=gate.get("started_at"), state_dir=state_dir,
                                 now=reference)
              for req in (gate.get("requires") or [])]
    met = bool(results) and all(r["met"] is True for r in results)
    return {"met": met, "results": results}


def _is_stale(item: dict, now) -> tuple:
    """Has an `observation-complete` item's gate been met for over `STALE_AFTER_DAYS` with no
    follow-up? "No follow-up" is read structurally, not guessed: `mark_observation_complete` stamps
    `last_touched` to the SAME instant as `gate['met_at']`, so an item nobody has touched since still
    carries the two stamps equal; anything that touched the record afterward (`start`/`update`/
    `hold`/…) moves `last_touched` strictly past it. Returns `(stale: bool, days_since: float|None)`."""
    gate = item.get("gate") or {}
    met_at = loops._parse_stamp(gate.get("met_at"))
    if met_at is None:
        return False, None
    if met_at.tzinfo is None:
        met_at = met_at.replace(tzinfo=now.tzinfo)
    age_days = (now - met_at).total_seconds() / 86400.0
    if age_days < STALE_AFTER_DAYS:
        return False, age_days
    touched = loops._parse_stamp(item.get("last_touched"))
    followed_up = touched is not None and (
        touched if touched.tzinfo else touched.replace(tzinfo=now.tzinfo)) > met_at
    return (not followed_up), age_days


def scan(state_dir: str | None = None, *, now=None, apply: bool = False) -> dict:
    """The whole scanner. `apply=False` (the default, and `check`'s CLI) reports what WOULD happen
    and writes nothing. `apply=True` (`scan --apply`, Dream's own call) additionally flips every
    fully-met gate through `loops.mark_observation_complete` + `loops.raise_item` — nothing else,
    ever: a row that is `open`/`held`/`paused`/terminal/anything but `observation` is never looked at
    by the flipping half of this function."""
    reference = loops._now(now)
    store = loops.load(state_dir)
    checked, flipped = [], []
    for item in list(store["items"].values()):
        if item.get("status") != "observation":
            continue
        result = check_gate(item.get("gate") or {}, state_dir=state_dir, now=reference)
        checked.append({"id": item["id"], "text": item.get("text"), **result})
        if result["met"] and apply:
            loops.mark_observation_complete(state_dir, item_id=item["id"], now=reference)
            loops.raise_item(state_dir, item_id=item["id"], now=reference)
            flipped.append(item["id"])

    # Re-load only if this pass actually wrote — an unapplied dry-run's staleness read must see the
    # same store its own `checked` list just described, not a hypothetical post-flip one.
    store2 = loops.load(state_dir) if flipped else store
    stale = []
    for item in store2["items"].values():
        if item.get("status") != "observation-complete":
            continue
        is_stale, days_since = _is_stale(item, reference)
        if is_stale:
            stale.append({"id": item["id"], "text": item.get("text"),
                         "met_at": (item.get("gate") or {}).get("met_at"),
                         "days_since": round(days_since, 1) if days_since is not None else None})

    return {"at": loops._stamp(reference), "checked": checked, "flipped": flipped, "stale": stale}


def brief_line(state_dir: str | None = None, *, now=None) -> str | None:
    """The Brief's lead-with line — "ready to continue" — for every `observation-complete` row, READ-ONLY, capped at `BRIEF_LINE_CAP` with the
    overflow reported as a count rather than silently dropped. `None` when there is nothing to say,
    the same convention `owi_unknowns.brief_line`/`tomorrow_marker.brief_line` already use so the
    caller can omit the section outright instead of printing an empty one."""
    reference = loops._now(now)
    store = loops.load(state_dir)
    ready = [item for item in store["items"].values() if item.get("status") == "observation-complete"]
    if not ready:
        return None
    ready.sort(key=lambda r: (r.get("gate") or {}).get("met_at") or "")
    lines = []
    for item in ready[:BRIEF_LINE_CAP]:
        stale, days_since = _is_stale(item, reference)
        note = (f" — met {round(days_since)}d ago, no follow-up yet: data may be stale, a new "
               f"observation period may be needed" if stale else "")
        lines.append(f"observation complete, ready to continue: {item.get('text')} "
                    f"({item['id']}){note}")
    if len(ready) > BRIEF_LINE_CAP:
        lines.append(f"…and {len(ready) - BRIEF_LINE_CAP} more (see `loops.py list --status "
                     f"observation-complete`)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI

def _stamp_dream_step(state_dir: str | None) -> None:
    """Record that Dream's step 2i actually ran (`dream_steps.py`). Lazy import, swallowed whole —
    the same contract `state_backup.py`/`rag_index.py` already use: bookkeeping that cannot import
    must never stop the scan it is measuring."""
    try:
        import dream_steps
        dream_steps.record(state_dir or loops.default_state_dir(), "2i")
    except Exception:  # noqa: BLE001 — see the docstring
        pass


def _print_checked(rows: list) -> None:
    for row in rows:
        verdict = "MET" if row["met"] else "not yet"
        print(f"{row['id']}\t{verdict}\t{row.get('text')}")
        for r in row["results"]:
            flag = "OK" if r["met"] else ("??" if r["met"] is None else "--")
            print(f"    [{flag}] {r['type']}: {r['detail']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="The observation-gate scanner (observation-gate-spec.md) — checks every "
                    "`observation` row's gate and, on --apply, flips a fully-met one to "
                    "observation-complete.")
    p.add_argument("--state-dir", default=None,
                   help="override seneschal/state/ (tests MUST pass this)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ck = sub.add_parser("check", help="dry-run over every `observation` row — writes nothing")
    ck.add_argument("--json", action="store_true")

    sc = sub.add_parser("scan", help="check, and on --apply flip every fully-met gate "
                                     "(Dream's own call)")
    sc.add_argument("--apply", action="store_true")
    sc.add_argument("--json", action="store_true")

    gs = sub.add_parser("gate-show", help="one item's gate, verbatim")
    gs.add_argument("item_id")

    bl = sub.add_parser("brief-line", help="the Brief's lead-with line, or nothing")

    args = p.parse_args(argv)

    if args.cmd in ("check", "scan"):
        result = scan(args.state_dir, apply=(args.cmd == "scan" and args.apply))
        if args.cmd == "scan":
            _stamp_dream_step(args.state_dir)  # `check` is a dry-run; only Dream's own call stamps
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_checked(result["checked"])
            if result["flipped"]:
                print("flipped: " + ", ".join(result["flipped"]))
            if result["stale"]:
                print("STALE (met, no follow-up):")
                for row in result["stale"]:
                    print(f"  {row['id']} — met {row['met_at']}, {row['days_since']}d ago: "
                         f"{row.get('text')}")
        return 0

    if args.cmd == "gate-show":
        item = loops.get(loops.load(args.state_dir), args.item_id)
        print(json.dumps(item.get("gate"), ensure_ascii=False, indent=2))
        return 0

    # brief-line
    line = brief_line(args.state_dir)
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
