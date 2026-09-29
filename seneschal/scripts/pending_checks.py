#!/usr/bin/env python3
"""Pending checks, phase 0 — the pairing register, and nothing else. Standard library only.

Spec: `seneschal/docs/session-coupling-spec.md` (§6.1 for this phase, §6.2-§6.4 for what is deliberately
NOT here, §8.4 for retention). Read that first; this module is the smallest thing that spec can be
started with. It belongs to the family of append-only JSONL registers (what the assistant said, what
was said on both sides — the `mouth` / `turns` logs where installed — and now **which claim is
waiting on which running job**): same primitive, same fail-open contract.

## What this is

`state/pending-checks.jsonl` is an append-only record of **a claim paired with the running job that
could refute it**, written at launch, *before the answer is known*.

The failure it exists for: a session states a conclusion, backgrounds the command that would test
it, writes the conclusion into durable memory, and is refuted an hour later — by which time the wrong
version has been overwritten and is unrecoverable. **The evidence is never lost**; it sits in a temp
file or a log. What it lacks is a *lifecycle*: a terminal state, and an owner waiting on it. This file
records the one thing nothing else in the tree can express — the **relationship** between a claim and
its outstanding check — and it is deliberately not yet another copy of the conversation.

## What this is NOT — zero behaviour change, and nothing reads it

There is **no delivery, no push, no wording change, no write barrier, no session-start surface and no
sweep** here. Not one reminder, job or turn behaves differently because this module exists. Phases 1-3
are designed and deliberately unbuilt, gated on a few weeks of these rows: **phase 0 only, then look**
(§8.5). Its entire purpose is to answer three questions: how often a claim is outstanding when a
session ends, how check duration compares to session life, and how many are never reconciled by
anyone.

## The evidence must be a job id — the one refusal, and it is the mechanism

A pending check whose evidence is a backgrounded command has **nothing to key on**: no id, no
terminal state, nothing to reconcile against. So `record_check` refuses without one, which makes the
standing rule — *"anything long-running goes through a JOB, never a bare background spawn"* —
**structural rather than advisory**: the row cannot be written for work that was not run as a job.
That is the same move `job_analysis.py`'s schema makes, where "point, don't diagnose" is enforced as a
shape a diagnosis cannot fit into rather than as a rule anyone has to remember. It also means the
register inherits `jobs.py`'s lifecycle, its exactly-once push and its return address for free.

**Fail-open on unknown, fail-closed on known-bad** — `reminders_acks`' gate idiom, for the same
reason. A job id naming no record in a jobs directory that we could read is refused (that is a
fabricated id, or work that was not a job). A jobs directory that is *absent* is unknown, so the row
is written and flagged `job_verified: false` rather than lost.

## Three invariants, and they are the whole contract

1. **Recording never costs the claim, the turn or the job.** `record_check` returns a bool and *never
   raises* — a full disk, a squatted path, an unserialisable payload all cost the row and nothing
   else. No caller wraps it in a try/except and none should. **A refusal is not an exception
   either**: it returns False, and `refusal()` is the separate predicate that says *why*, in a real
   sentence.
2. **`claim` and `falsifier` are verbatim and uncapped.** A truncated claim cannot be matched against
   the result that refuted it, which is the entire join this file exists to make.
3. **Nothing reads this file to decide anything.** `stats()` and `tail` are for a human and for the
   look phases 1-3 are gated on; no behaviour branches on a row.

## The falsifier, and why a late row is flagged rather than refused

§6.1 requires the falsifier to be written **before the answer is known**. Code cannot see intent, but
it can see the clock: if the job had *already* reached a terminal state when the row was written, the
answer was knowable, and the row records `falsifier_blind: false`. **Flagged, not refused** — refusing
would lose the pairing outright for a job that happened to finish in the seconds before the turn got
round to writing it, and losing the measurement is worse than recording an honest caveat about it.
`stats()['falsifier_not_blind']` is where that stops being a rule someone has to remember.

## Where the rows come from — the honest deviation, named rather than hidden

*"Is this claim refutable by that job?"* is a judgment, so the writer is this module's `record` CLI
invoked from a turn — **prompt-side**, and a prompt-side contract is exactly the kind that quietly
collects zero rows. It is named here rather than hidden.

**The mitigation is that the row is not the only detector.** `jobs.py --goal` is already mandatory and
already in code, so a **phase-1 sweep** can count completed jobs whose goal is check-shaped and that
carry no pending-check row, and report the gap — a disk-side check on a prompt-side contract. **That
sweep is NOT built here** (it is phase 1), and nothing in this row's shape prevents it: `job_id` is the
key it joins on.

## The lifecycle, and why `resolve` is an append

open → resolved (→ orphaned, which phase 3 *derives*; nothing writes it). A resolution is a **second
row for the same `check_id`, never a rewrite** — a correction is append-only supersession rather than
an edit (§8.2). The open row stays readable forever; the newest row for a `check_id` is its current
state.

`resolve` exists in phase 0 because **retention needs it**. The retention rule is *unresolved kept
indefinitely, resolved pruned at 90 days*, and without a way to record a resolution that rule would be
prose with nothing implementing it. It is still zero behaviour change: nothing is delivered, and
nothing reads the state.

## Retention — 90 days for a RESOLVED check, and never for an open one

§8.4. **An unresolved check is the artifact that says something was never finished, and pruning it
would delete exactly the finding.** So `prune` drops a check only once it has a resolution row, and
only when that resolution is older than 90 days; it takes the check's open row with it, because half
a pairing is not a pairing. A row whose `at` will not parse is **kept**.

USAGE:
  python pending_checks.py record --job-id 20260810-034102-1b3c \\
      --claim "the tool does not exist" \\
      --falsifier "a grep hit for `def watch_` in presence.py refutes it" \\
      --assertion-at 2026-08-10T01:07:44Z --assertion-text "There's no watcher for that." \\
      --artifact ~/.claude/projects/.../memory/some-note.md
  python pending_checks.py resolve <check-id> --outcome refuted --note "it exists: presence.py:493"
  python pending_checks.py open                  # what is still outstanding
  python pending_checks.py tail --limit 20
  python pending_checks.py stats
  python pending_checks.py prune --days 90       # resolved only; open checks are never pruned

Per-field contract, the `falsifier_blind` flag and the deferred phase-1 sweep:
`../state/README.md` -> `pending-checks.jsonl`.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import jobs  # the evidence: the jobs dir, one record, and the goal line's single cap  # noqa: E402

SCHEMA = "seneschal.pending-check/1"
CHECKS_FILE = "pending-checks.jsonl"

# §8.4. Applies to a RESOLVED check only — an open one is kept indefinitely,
# because it is the artifact that says something was never finished. `prune` enforces both halves.
RETENTION_DAYS = 90

DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

STATE_OPEN = "open"
STATE_RESOLVED = "resolved"

# Loosely validated: an unknown value is recorded verbatim rather
# than rejected, because a resolution that happened must never be lost to this module's opinion about
# labels. `inconclusive` is a first-class answer — a check that came back unreadable is not a check
# that came back the same way, and collapsing the two is how §1's failure looks from the other side.
OUTCOMES = ("confirmed", "refuted", "inconclusive")

ORIGIN_ENV_VAR = "CLAUDE_CODE_SESSION_ID"

# One small append per row: the multi-writer-safe primitive on one machine. The lock is for threads inside one process; the O_APPEND write is
# what makes two processes safe.
_append_lock = threading.Lock()


def default_state_dir() -> str:
    return DEFAULT_STATE_DIR


def checks_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, CHECKS_FILE)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _row_id(now: datetime | None = None) -> str:
    """`20260810-034512-9f2a1b3c4d5e` — a sortable stamp plus twelve hex.

    Why twelve and not four: four hex is 65,536 values, and the birthday problem makes 160 rows
    inside one second collide about **18%** of the time — the shape a concurrent-writers test hits
    intermittently, not because an append is lost but because two rows come back with the same id.

    That matters more than a green suite. This id IS the `check_id` every later row names, so a
    collision does not merely duplicate a string: `by_check` groups on it, and two unrelated checks
    would silently fuse into one entry whose resolutions belong to both. Twelve hex (2^48) puts the
    same 160-row second at roughly 1 in 10^11."""
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{when:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:12]}"


def new_check_id(now: datetime | None = None) -> str:
    """The correlator, minted by the caller so it can print the id it is about to write. Reading the id back off the tail of the file instead
    would be a guess the moment two turns record in the same second, which is exactly the collision
    the hex suffix exists to prevent."""
    return _row_id(now)


def _parse_stamp(value) -> datetime | None:
    """Tolerant read of an `at` field. Anything unparseable reads as None, and every caller treats
    that as "don't touch this row" — a garbled stamp must never make `prune` delete a pairing."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _text(value):
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


# --------------------------------------------------------------------------- the evidence

def job_record(state_dir: str | None = None, job_id: str | None = None) -> dict | None:
    """The `state/jobs/<id>.json` record this check is keyed to, or None. Never raises.

    Enrichment AND verification, from one read: the goal line and the origin session id come off it
    (both are already stamped by `jobs.py`, so a caller retyping them would be a second source of
    truth), and its absence is what `refusal()` acts on."""
    try:
        if not job_id:
            return None
        return jobs.load_job(state_dir or DEFAULT_STATE_DIR, str(job_id))
    except Exception:  # noqa: BLE001 — enrichment must never reach `record_check`'s contract
        return None


def _jobs_dir_readable(state_dir: str | None = None) -> bool:
    """Could we have found a job record if one existed? **This is the fail-open/fail-closed line.**

    A jobs directory that is missing (a fresh checkout, a `--state-dir` pointed somewhere new) is
    *unknown*, and unknown must not cost the row. A jobs directory that exists and does not hold the
    named record is *known-bad*: the id is fabricated, or the work was not run as a job, which is the
    exact thing the id requirement exists to make impossible."""
    try:
        return os.path.isdir(jobs.jobs_dir(state_dir or DEFAULT_STATE_DIR))
    except Exception:  # noqa: BLE001
        return False


def refusal(state_dir: str | None = None, *, job_id=None, claim=None, falsifier=None) -> str | None:
    """Why this pairing cannot be recorded, as a sentence, or None when it can. **Never raises.**

    Separate from `record_check` on purpose, the way `reminders_acks.watch_escalation_blocked` is
    separate from the send: the refusal is a judgment with a reason, and a caller (or a test) that
    wants the reason should not have to infer it from a False."""
    try:
        if not str(job_id or "").strip():
            return ("the evidence must be a JOB id — a backgrounded command has no id, no terminal "
                    "state and nothing to reconcile against, so there is nothing to pair the claim "
                    "with. Start the work with `jobs.py start --goal \"...\"` and pass its id "
                    "(session-coupling-spec.md §6.1).")
        if not str(claim or "").strip():
            return "a pending check needs the CLAIM, as text — what would be wrong if the check comes back the other way."
        if not str(falsifier or "").strip():
            return ("a pending check needs the FALSIFIER — one line saying what result would refute "
                    "the claim, written before the answer is known (§6.1).")
        if job_record(state_dir, job_id) is None and _jobs_dir_readable(state_dir):
            return (f"no job `{job_id}` in {jobs.jobs_dir(state_dir or DEFAULT_STATE_DIR)} — the row "
                    "cannot be written for work that was not run as a job. If the work is running, "
                    "it is not a job; if it is a job, check the id.")
    except Exception:  # noqa: BLE001 — a broken check may not become a refusal to record
        return None
    return None


# --------------------------------------------------------------------------- writing

def record_check(state_dir: str | None = None, *, job_id=None, claim=None, falsifier=None,
                 assertion_at=None, assertion_text=None, artifacts=None, goal=None,
                 session_id: str | None = None, check_id: str | None = None, env=None,
                 now: datetime | None = None) -> bool:
    """Append one open `seneschal.pending-check/1` row. Returns True iff the row hit disk.

    **This never raises** (invariant 1). It is called from a live turn, and a failed append must cost
    the row and nothing else — never the claim, never the job, never the reply. Callers do not need a
    try/except around it, and should not add one.

    It returns False on a **refusal** too (no job id, no claim, no falsifier, or an id naming no job
    we could have found). Call `refusal()` first when you want to say why — the CLI does.

    `goal` and `session_id` default to the job record's own `origin.goal` / `origin.session_id`,
    because `jobs.py` already stamps both and a caller retyping them would be a second source of
    truth. `session_id` falls back to `$CLAUDE_CODE_SESSION_ID` for a job started before the origin
    auto-stamp, or with `stamped_by: "none"`.
    """
    try:
        state_dir = state_dir or DEFAULT_STATE_DIR
        if refusal(state_dir, job_id=job_id, claim=claim, falsifier=falsifier) is not None:
            return False

        rec = job_record(state_dir, job_id)
        origin = rec.get("origin") if isinstance(rec, dict) and isinstance(rec.get("origin"), dict) else {}
        if not session_id:
            session_id = origin.get("session_id") or None
        if not session_id:
            try:
                session_id = ((env if env is not None else os.environ).get(ORIGIN_ENV_VAR) or "").strip() or None
            except Exception:  # noqa: BLE001
                session_id = None
        if goal is None:
            goal = jobs.origin_goal(rec) if isinstance(rec, dict) else None

        # §6.1's "written before the answer is known", measured rather than assumed. Flagged, never
        # refused — see the module docstring.
        blind = True
        if isinstance(rec, dict) and rec.get("status") in jobs.TERMINAL:
            blind = False

        claim_ref = {
            # The join to an assertion log (where one is installed) is `at` + exact text — there is
            # no shared turn id to join on. Both optional: the
            # strongest case for a pending check is a belief acted on SILENTLY, which has no
            # assertion row at all.
            "assertion_at": _text(assertion_at) or None,
            "assertion_text": _text(assertion_text) or None,
            # Where the claim was written down durably, by path — a memory file, a spec, a note.
            # These may live outside this repo, so the path is all a sweep can key on.
            "artifacts": [str(a) for a in (artifacts or []) if str(a).strip()],
        }

        row_id = str(check_id).strip() if str(check_id or "").strip() else _row_id(now)
        row = {
            "schema": SCHEMA,
            "id": row_id,
            "check_id": row_id,          # the open row IS the correlator every later row names
            "at": _stamp(now),
            "state": STATE_OPEN,
            "job_id": str(job_id),
            "job_title": _text(rec.get("title")) if isinstance(rec, dict) else None,
            "goal": jobs.one_line(goal) if goal else None,
            # Verbatim and uncapped (invariant 2).
            "claim": _text(claim),
            "falsifier": _text(falsifier),
            "claim_ref": claim_ref,
            "session_id": session_id,
            "job_verified": isinstance(rec, dict),
            "falsifier_blind": blind,
        }
        return _append(state_dir, row)
    except Exception:  # noqa: BLE001 — see the docstring: the row is never worth the claim
        return False


def record_resolution(state_dir: str | None = None, *, check_id=None, outcome=None, note=None,
                      now: datetime | None = None) -> bool:
    """Append a `resolved` row for an existing check. Returns True iff it hit disk. **Never raises.**

    **An append, never a rewrite** (§8.2: a correction supersedes rather than edits): the open row stays readable forever and the
    newest row for a `check_id` is its current state. `outcome` is recorded verbatim even when it is
    not one of `OUTCOMES` — a resolution that happened must not be lost to a label.

    Resolving an unknown `check_id` is refused: a resolution with nothing to resolve is not a fact
    about anything, and writing it would put a `check_id` in the file that `open_checks` would then
    have to guess about."""
    try:
        state_dir = state_dir or DEFAULT_STATE_DIR
        check_id = str(check_id or "").strip()
        if not check_id:
            return False
        opened = {r.get("check_id"): r for r in read_checks(state_dir)
                  if r.get("state") == STATE_OPEN}
        if check_id not in opened:
            return False
        return _append(state_dir, {
            "schema": SCHEMA,
            "id": _row_id(now),
            "check_id": check_id,
            "at": _stamp(now),
            "state": STATE_RESOLVED,
            "job_id": opened[check_id].get("job_id"),
            "outcome": _text(outcome) or None,
            "note": _text(note),
        })
    except Exception:  # noqa: BLE001
        return False


def _append(state_dir: str, row: dict) -> bool:
    line = json.dumps(row, ensure_ascii=False) + "\n"
    os.makedirs(state_dir, exist_ok=True)
    with _append_lock:
        with open(checks_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(line)
    return True


# --------------------------------------------------------------------------- reading

def read_checks(state_dir: str | None = None, limit: int | None = None,
                since: datetime | None = None) -> list:
    """Rows oldest-first, malformed lines skipped. `limit` keeps the **newest** N. Fail-open: an
    absent or unreadable log reads empty, never an error.

    A partial last line — the shape a crash mid-append leaves — is one unparseable line and is
    skipped like any other, so a torn tail costs that row and not the file."""
    rows: list = []
    try:
        with open(checks_path(state_dir), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if since is not None:
                    at = _parse_stamp(row.get("at"))
                    if at is None or at < since:
                        continue
                rows.append(row)
    except OSError:
        return []
    if limit is not None and limit >= 0:
        rows = rows[-limit:] if limit else []
    return rows


def by_check(state_dir: str | None = None, rows=None) -> dict:
    """`{check_id: {"open": <row>, "resolutions": [<row>, ...]}}`, oldest-first within each.

    The file is append-only with several row kinds, so *every* reader wants it folded by check first.
    A resolution whose `check_id` names no open row is kept under its own key with `open: None` —
    dropping it would hide a real inconsistency rather than record one."""
    folded: dict = {}
    for row in (read_checks(state_dir) if rows is None else rows):
        key = row.get("check_id") or row.get("id")
        if not key:
            continue
        slot = folded.setdefault(key, {"open": None, "resolutions": []})
        if row.get("state") == STATE_OPEN and slot["open"] is None:
            slot["open"] = row
        elif row.get("state") != STATE_OPEN:
            slot["resolutions"].append(row)
    return folded


def open_checks(state_dir: str | None = None) -> list:
    """The open rows with no resolution — what is still outstanding, oldest-first."""
    return [slot["open"] for slot in by_check(state_dir).values()
            if slot["open"] is not None and not slot["resolutions"]]


def prune(state_dir: str | None = None, days: int = RETENTION_DAYS,
          now: datetime | None = None) -> int:
    """Drop **resolved** checks whose resolution is older than `days`, and rewrite the log. Returns
    how many checks were dropped (not rows).

    **An open check is never pruned, at any `days`** — §8.4: an unresolved check is
    the artifact that says something was never finished, and pruning it deletes exactly the finding.
    Both rows of a dropped check go together, because half a pairing is not a pairing.

    A row whose `at` is missing or unparseable is **kept**, and so is anything this module cannot
    read: this GC must never be what loses a pairing. `days <= 0` keeps everything.

    Built-then-replaced, so a crash mid-prune leaves the old file intact."""
    if days is None or days <= 0:
        return 0
    path = checks_path(state_dir)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0

    parsed = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            row = None
        parsed.append((line if line.endswith("\n") else line + "\n",
                       row if isinstance(row, dict) else None))

    folded = by_check(rows=[row for _, row in parsed if row is not None])
    expired = set()
    for key, slot in folded.items():
        if slot["open"] is None or not slot["resolutions"]:
            continue  # open, or a stray resolution — keep either way
        stamps = [_parse_stamp(r.get("at")) for r in slot["resolutions"]]
        if any(s is None for s in stamps) or not stamps:
            continue  # unreadable ≠ expired
        if max(stamps) < cutoff:
            expired.add(key)
    if not expired:
        return 0

    keep = [line for line, row in parsed
            if row is None or (row.get("check_id") or row.get("id")) not in expired]
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0
    return len(expired)


def stats(state_dir: str | None = None, now: datetime | None = None) -> dict:
    """The numbers §6.1 exists to produce, and nothing that decides anything.

    - **`open`** — how many claims are outstanding right now.
    - **`open_with_terminal_job`** — an open check whose job has already reached a terminal state:
      **the answer came back and nobody reconciled it.** This is the "how many are never reconciled"
      question, from the side this file can see. It is NOT phase 1's sweep, which asks the inverse —
      completed jobs with check-shaped goals carrying *no row at all* — and which is deliberately not
      built here. Jobs prune at 14 days, so an older check simply reads `job_unknown`.
    - **`median_open_sec` / `median_resolution_sec`** — how long a check actually runs, against
      session lifetimes (the session registry). The join is a reader's to make; nothing here
      reaches for it.
    - **`falsifier_not_blind`** — rows written after the job was already terminal (§6.1's "before the
      answer is known", measured), and **`job_unverified`** — rows whose job record could not be read
      at write time.

    `checks_per_day` reads **None until two distinct days exist**, deliberately — a rate extrapolated
    from one day is fiction, and this phase exists precisely so phases 1-3 are not sized on one.

    Fail-open like every reader here: an absent or unreadable log reads as zero rows."""
    rows = read_checks(state_dir)
    try:
        size_bytes = os.path.getsize(checks_path(state_dir))
    except OSError:
        size_bytes = 0

    folded = by_check(rows=rows)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    days: set = set()
    first_at = last_at = None
    for row in rows:
        at = _parse_stamp(row.get("at"))
        if at is None:
            continue
        days.add(at.date())
        first_at = at if first_at is None or at < first_at else first_at
        last_at = at if last_at is None or at > last_at else last_at

    by_outcome: dict = {}
    open_ages: list = []
    resolution_secs: list = []
    open_n = resolved_n = 0
    open_terminal = open_job_unknown = 0
    not_blind = unverified = 0
    sessions: set = set()

    for slot in folded.values():
        opened = slot["open"]
        if opened is None:
            continue
        if opened.get("falsifier_blind") is False:
            not_blind += 1
        if opened.get("job_verified") is False:
            unverified += 1
        if opened.get("session_id"):
            sessions.add(opened["session_id"])
        started = _parse_stamp(opened.get("at"))
        if slot["resolutions"]:
            resolved_n += 1
            newest = slot["resolutions"][-1]
            by_outcome[newest.get("outcome") or "unstated"] = \
                by_outcome.get(newest.get("outcome") or "unstated", 0) + 1
            ended = _parse_stamp(newest.get("at"))
            if started is not None and ended is not None and ended >= started:
                resolution_secs.append((ended - started).total_seconds())
        else:
            open_n += 1
            if started is not None:
                open_ages.append((now - started).total_seconds())
            rec = job_record(state_dir, opened.get("job_id"))
            if rec is None:
                open_job_unknown += 1
            elif rec.get("status") in jobs.TERMINAL:
                open_terminal += 1

    covered = len(days) if len(days) > 1 else None
    return {
        "rows": len(rows),
        "bytes": size_bytes,
        "checks": len(folded),
        "open": open_n,
        "resolved": resolved_n,
        "by_outcome": by_outcome,
        "sessions": len(sessions),
        "first_at": _stamp(first_at) if first_at else None,
        "last_at": _stamp(last_at) if last_at else None,
        "days_covered": covered,
        "checks_per_day": round(len(folded) / covered, 2) if covered else None,
        "open_with_terminal_job": open_terminal,
        "open_job_unknown": open_job_unknown,
        "median_open_sec": round(statistics.median(open_ages)) if open_ages else None,
        "median_resolution_sec": round(statistics.median(resolution_secs)) if resolution_secs else None,
        "falsifier_not_blind": not_blind,
        "job_unverified": unverified,
    }


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Pending checks, phase 0: pair a claim with the running job that could refute it.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="pair a claim with the job that could refute it")
    rec.add_argument("--job-id", required=True,
                     help="the evidence — a `jobs.py start` id. A backgrounded command has none, "
                          "which is the point (§6.1)")
    rec.add_argument("--claim", required=True,
                     help="the claim, as text: what would be wrong if the check comes back the "
                          "other way")
    rec.add_argument("--falsifier", required=True,
                     help="one line: what RESULT would refute the claim. Written before the answer "
                          "is known")
    rec.add_argument("--assertion-at", default=None,
                     help="the `assertions.jsonl` row's `at`, when the claim was spoken")
    rec.add_argument("--assertion-text", default=None, help="that row's exact text")
    rec.add_argument("--artifact", action="append", default=None, dest="artifacts",
                     help="a durable file the claim was written into, by path (repeatable)")
    rec.add_argument("--goal", default=None,
                     help="overrides the job's own --goal line, which is used by default")
    rec.add_argument("--session-id", default=None,
                     help="overrides the job's origin session id / $CLAUDE_CODE_SESSION_ID")

    res = sub.add_parser("resolve", help="append a resolution for a check (never rewrites the open row)")
    res.add_argument("check_id")
    res.add_argument("--outcome", default=None,
                     help=f"{' | '.join(OUTCOMES)} — an unknown value is recorded verbatim")
    res.add_argument("--note", default=None)

    sub.add_parser("open", help="the checks still outstanding")

    tail = sub.add_parser("tail", help="print the newest rows")
    tail.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats", help="what phase 0 exists to measure")

    pr = sub.add_parser("prune", help="drop RESOLVED checks older than --days; open ones are kept")
    pr.add_argument("--days", type=int, default=RETENTION_DAYS)

    args = p.parse_args(argv)

    if args.cmd == "record":
        # The refusal is a real message, never a silent False — `jobs.py`'s rule. It runs before the
        # write so the reason is the one the caller sees, and exit 2 keeps it distinguishable from a
        # disk failure (1).
        why = refusal(args.state_dir, job_id=args.job_id, claim=args.claim, falsifier=args.falsifier)
        if why:
            print(f"pending_checks: refused — {why}", file=sys.stderr)
            print(json.dumps({"ok": False, "refused": why}, ensure_ascii=False))
            return 2
        check_id = new_check_id()
        ok = record_check(args.state_dir, job_id=args.job_id, claim=args.claim,
                          falsifier=args.falsifier, assertion_at=args.assertion_at,
                          assertion_text=args.assertion_text, artifacts=args.artifacts,
                          goal=args.goal, session_id=args.session_id, check_id=check_id)
        print(json.dumps({"ok": ok, "check_id": check_id if ok else None}, ensure_ascii=False))
        return 0 if ok else 1
    if args.cmd == "resolve":
        ok = record_resolution(args.state_dir, check_id=args.check_id, outcome=args.outcome,
                               note=args.note)
        if not ok:
            print(f"pending_checks: no open check `{args.check_id}` — "
                  "`pending_checks.py open` lists what is outstanding.", file=sys.stderr)
        print(json.dumps({"ok": ok, "check_id": args.check_id}, ensure_ascii=False))
        return 0 if ok else 1
    if args.cmd == "open":
        for row in open_checks(args.state_dir):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.cmd == "tail":
        for row in read_checks(args.state_dir, limit=args.limit):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.cmd == "stats":
        print(json.dumps(stats(args.state_dir), ensure_ascii=False, indent=2))
        return 0
    dropped = prune(args.state_dir, days=args.days)
    print(json.dumps({"ok": True, "dropped": dropped}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
