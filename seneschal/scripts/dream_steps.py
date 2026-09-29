#!/usr/bin/env python3
"""Dream's step ledger — what actually ran, and how long ago.

**The failure this exists for.** A nightly consolidation run that is allowed to defer its expensive
steps "to the next full Dream" — in prose, in the Run Log — will defer them nightly. Nothing carries
the deferral forward, nothing counts the repeats, nothing gets louder, and a feature that depends on
the step (the local RAG index, say) silently ages out while looking built. Nothing crashes; the
deferral is simply never paid.

**The rule this module encodes: a skip must cost something.** Not a prose note — a row, an age,
and an escalation that grows.

**Why the workers stamp this and not the Dream prompt.** A prompt-side-only contract is a bet that a
co-operating session remembers to record itself, and that bet loses. So the record is written by
**the script that did the work** (`rag_index.py`, `salience_rollup.py`, `state_backup.py`), as a side
effect of doing it. A step with no single owning script is still declared here and still ages —
an un-owned step that goes stale is a real finding, not a gap in the ledger.

**Declaring an owner and never calling `record` is its own bug.** A step whose owner does not stamp
reads `never` while running correctly, and a **false alarm in the smoke detector trains exactly the
deafness this module exists to cure**. So adding a step to `STEPS` with an owner and adding the
`record` call to that owner are one change, not two.

**Fail-open, like every other `state/` writer here.** `record()` never raises: a failed stamp costs
the row, never the work that earned it. No caller wraps it in a try/except and none should.

**A reasoned skip still costs — but it must not read as a crash.** A step skipped ON PURPOSE (an
operator refusing a destructive no-op and saying why in `--reason`) still ages past its window, and
the alarm still fires. What changes is the wording: `summary_line` and `nudge_text` say **which** of
three different things happened — *never run*, *stopped (no skip recorded: nothing ran and nothing
said why)*, or *skipped N nights, last <day>: <reason>* — so a policy refusal is not investigated as
a crash. Making a reasoned skip free would re-open the hole above — a skip that costs nothing is a
deferral nobody counts.

**Know what it cannot see.** `consecutive_skips` moves only on an explicit `--skip`, so a step never
considered part of a run leaves no row at all; and `status` is an ALARM, never a scheduler — reading
"none overdue" as permission to skip makes the ledger license the deferral it exists to end.

CLI::

    python dream_steps.py status            # every step, its age, and whether it is overdue
    python dream_steps.py check             # exit 1 if any step is overdue (for a gate)
    python dream_steps.py record 2b         # stamp a step as having just run
    python dream_steps.py record 2b --skip --reason "cold, no new records"

`status` shows a skipped step's count, last-skip day and reason inline; `check` and any nudge built
on `nudge_text` say the same, so a policy refusal never reads as a crash.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import clock  # the owner-local activity day; a Dream skip after midnight is the night BEFORE

LEDGER_FILE = "dream-steps.json"

# Reserved keys in the ledger file. `report()` iterates `STEPS`, never the file's keys, so a
# reserved entry is invisible to every reader — the underscore makes that intent explicit rather
# than leaving it to depend on that.
NUDGE_KEY = "_nudged"      # the local day the owner was last told about stale steps
CREATED_KEY = "_created"   # first-ever write; the grace window for a never-run step

# The canonical steps (ids match the Dream steps in `../SKILL.md`), their owning script where one
# exists, and how long each may go unrun before it is a problem. The ages are deliberately generous —
# this is a smoke alarm for a step that has stopped happening at all, not a stopwatch on a step that
# ran a day late.
#
# `max_age_days` is set from what the step is FOR, not from its cadence: the RAG index feeds every
# retrieval, so two days stale is already degraded recall; the salience rollup is a weekly report
# and three weeks of silence is the first point at which it is clearly not running.
# **`owner` is what makes a step nudgeable, and that is deliberate.** A step no script stamps
# cannot be measured in code — only a co-operating session can say it ran, which is the promise
# that already failed. Declaring such a step and alarming on it would mean a permanent daily
# "never run" for something nobody can clear, and a nagging alarm is an alarm that gets muted:
# it would train exactly the deafness this module exists to cure. So un-owned steps are LISTED
# (their gap is real and worth seeing in `status`) but never nudged, and the honest way to promote
# one is to give it a script that stamps itself.
STEPS = {
    # 3 days: the risk of a stale reconcile is queue bloat, not a safety miss — the fire-time ack
    # gate and staleness cutoff already stop a stale entry from buzzing, so this window is generous.
    # Un-owned until a reconcile script stamps it.
    "2": {"label": "reminders queue reconcile", "owner": None, "max_age_days": 3},
    "2b": {"label": "RAG index ingest", "owner": "rag_index.py", "max_age_days": 2},
    "2c": {"label": "sentiment cross-check", "owner": None, "max_age_days": 14},
    "2d": {"label": "meal-plan staging", "owner": None, "max_age_days": 7},
    # 2 days, not 7: the window this protects is "how much can one bad write cost", and every night
    # it does not run is a night of `carry-over.md` with no copy. See state_backup.py.
    "2g": {"label": "state backups", "owner": "state_backup.py", "max_age_days": 2},
    # 2 days, not 14 (the register's own dormancy window): the whole point is catching a gate the
    # DAY it is met, not weeks later when its evidence may be stale. See observation_gate.py.
    "2i": {"label": "observation-gate scan", "owner": "observation_gate.py", "max_age_days": 2},
    "rollup": {"label": "salience rollup (weekly)", "owner": "salience_rollup.py",
               "max_age_days": 21},
}


def _path(state_dir: str) -> str:
    return os.path.join(state_dir, LEDGER_FILE)


def _load(state_dir: str) -> dict:
    """Tolerant read: a corrupt or absent ledger reads empty rather than raising. The ledger is a
    measurement of other work; it must never be the reason that work fails."""
    try:
        with open(_path(state_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(state_dir: str, data: dict, now: datetime | None = None) -> None:
    """Build-then-`os.replace`. NEVER truncate-write a `state/` file: they are gitignored and exist
    nowhere else, so a crash mid-write against the live path loses the history for good."""
    # Stamped once, on the first write ever. It is what lets a never-run step be given a fair
    # grace window instead of alarming the moment the file appears.
    if not isinstance(data.get(CREATED_KEY), dict):
        data[CREATED_KEY] = {
            "at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")}
    os.makedirs(state_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".dream-steps-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _path(state_dir))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record(state_dir: str, step: str, *, ok: bool = True, note: str | None = None,
           now: datetime | None = None) -> None:
    """Stamp `step` as having run (`ok=True`) or been skipped (`ok=False`).

    **Never raises.** A failed stamp costs the row, never the work — the same contract as
    `failures.record`, and for the same reason: a caller that has to defend against its own
    bookkeeping starts leaving the bookkeeping out.

    An unknown step id is recorded anyway rather than dropped. A step that exists in the running
    Dream but not in `STEPS` is a fact worth having on disk; silently discarding it would rebuild
    the exact blind spot this module is here to close."""
    try:
        now = now or datetime.now(timezone.utc)
        stamp = now.isoformat(timespec="seconds")
        data = _load(state_dir)
        row = data.get(step)
        if not isinstance(row, dict):
            row = {}
        if ok:
            row["last_ok"] = stamp
            row["consecutive_skips"] = 0
        else:
            row["last_skip"] = stamp
            row["consecutive_skips"] = int(row.get("consecutive_skips") or 0) + 1
        if note:
            row["note"] = str(note)[:500]
        data[step] = row
        _save(state_dir, data)
    except Exception:  # noqa: BLE001 — see the docstring; this must never fail the caller
        pass


def parse_stamp(raw) -> datetime | None:
    """An ISO stamp from the ledger as an aware datetime, or None.

    Exported because callers need the *datetime*, not the string: a nudge queue that takes an
    `observed_at` datetime and fails open on a `str` would silently never queue the nudge. Fail-open
    plumbing hides a caller's type error perfectly, which is precisely why the seam is a function."""
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def age_days(row: dict, now: datetime) -> float | None:
    """Days since this step last ran successfully; None when it has never been recorded."""
    ts = parse_stamp((row or {}).get("last_ok"))
    return None if ts is None else (now - ts).total_seconds() / 86400.0


def report(state_dir: str, now: datetime | None = None) -> list[dict]:
    """Every declared step, worst-first, with its age and whether it is overdue.

    A step that has NEVER been recorded is overdue **once ITS OWN `created` stamp is older than its
    max age** (`ensure_ledger` backfills that stamp — see its docstring for the two-tier rule). If a
    step somehow carries no `created` stamp at all (a ledger written before this existed, and never
    since re-`ensure_ledger`'d), this falls back to the ledger's own birthday, so a step that never
    once succeeds still cannot hide behind a missing per-step stamp — see `ensure_ledger`'s
    per-step grace rule.

    Both halves matter: "no record" must not read as "fine" — that equivalence is exactly how a
    deferred step goes invisible. But a step first seen five minutes ago knows nothing about
    anything yet, and alarming on a fresh install (or a step that just landed) would make the first
    thing anyone learns about this alarm be how to ignore it."""
    now = now or datetime.now(timezone.utc)
    data = _load(state_dir)
    born = _ledger_age_days(data, now)
    out = []
    for step, meta in STEPS.items():
        row = data.get(step) if isinstance(data.get(step), dict) else {}
        age = age_days(row, now)
        if age is None:
            step_born = _step_created_days(row, now)
            if step_born is None:
                step_born = born  # no per-step stamp yet — fall back rather than never alarm
            overdue = step_born is not None and step_born > meta["max_age_days"]
        else:
            overdue = age > meta["max_age_days"]
        out.append({
            "step": step,
            "label": meta["label"],
            "owner": meta["owner"],
            "max_age_days": meta["max_age_days"],
            "age_days": age,
            "last_ok": row.get("last_ok"),
            "last_skip": row.get("last_skip"),
            "consecutive_skips": int(row.get("consecutive_skips") or 0),
            "note": row.get("note"),
            "overdue": overdue,
            # Only a step some script stamps can be alarmed on — see the note on STEPS.
            "measured": meta["owner"] is not None,
        })
    # Worst first: never-run before merely-stale, then by age. `stale()` slices off the top of
    # this, so the nudge names the most alarming thing rather than the alphabetically first.
    out.sort(key=lambda r: (-1e9 if r["age_days"] is None else -r["age_days"]))
    return out


def _ledger_age_days(data: dict, now: datetime) -> float | None:
    """Days since this ledger was first written. None when unknown."""
    born = data.get(CREATED_KEY)
    ts = parse_stamp(born.get("at")) if isinstance(born, dict) else None
    return None if ts is None else (now - ts).total_seconds() / 86400.0


def _step_created_days(row: dict, now: datetime) -> float | None:
    """Days since THIS STEP's own `created` stamp. None when it has none yet."""
    ts = parse_stamp((row or {}).get("created"))
    return None if ts is None else (now - ts).total_seconds() / 86400.0


def _backfill_step_births(data: dict, now: datetime) -> bool:
    """Give every declared step a `created` stamp the first time anyone looks at it. Mutates `data`
    in place; returns whether anything changed.

    **Two-tier rule (per-step grace).** A step already
    present in the ledger existed before this stamp did, so it inherits the LEDGER's own birthday
    (`CREATED_KEY`) rather than `now` — stamping it with today would hand every already-running step
    a fresh grace window and go quiet on real staleness for days, turning this fix into an outage of
    the very alarm it protects. A step genuinely absent from the ledger has never been seen before —
    `now` IS its honest birthday, the same "the clock starts when we begin watching" semantics this
    module's `ensure_ledger` already documents for the ledger as a whole.

    **The concrete case this exists for:** a step newly added to `STEPS` long after the ledger was
    created would otherwise be judged OVERDUE within hours against the ledger's old birthday — a
    never-run step punished for a clock it never started. A step's own `created` stamp
    is what lets it be judged against ITS OWN grace window instead."""
    ledger_created = data.get(CREATED_KEY)
    ledger_at = ledger_created.get("at") if isinstance(ledger_created, dict) else None
    changed = False
    for step in STEPS:
        row = data.get(step)
        if isinstance(row, dict):
            if "created" not in row:
                row["created"] = ledger_at or now.isoformat(timespec="seconds")
                changed = True
        else:
            data[step] = {"created": now.isoformat(timespec="seconds")}
            changed = True
    return changed


def stale(state_dir: str, now: datetime | None = None) -> list[dict]:
    """The overdue rows worth ALARMING on, worst first — owner-backed only.

    `report()` is the full picture and `status` prints all of it; this is what an escalation may
    say. The difference is the whole nagging-alarm problem: a step nothing can stamp would be
    permanently overdue and would make the daily nudge unreadable within a week."""
    return [r for r in report(state_dir, now) if r["overdue"] and r["measured"]]


#: How much of a skip's `--reason` a one-line nudge carries. The ledger keeps up to 500 chars; the
#: nudge carries enough to name the refusal (a real reason usually states its point within ~250
#: chars) and points at the run-log entry for the rest.
REASON_CAP = 280


def skip_state(row: dict) -> str:
    """Which of three different silences this row is — the distinction a nudge must draw.

    * ``"never"`` — no `last_ok` ever.
    * ``"skipped"`` — the most recent event was an explicit `--skip` (``consecutive_skips > 0``;
      `record` resets it to 0 on every ok, so a positive count means the skips postdate the last
      run). A policy refusal with a reason on file, not a crash.
    * ``"stopped"`` — it ran once and then nothing stamped it at all: no run, no skip, no reason.
      This is the silent-deferral shape, and the one that deserves the alarming wording."""
    if row.get("age_days") is None and not row.get("last_ok"):
        return "never"
    return "skipped" if int(row.get("consecutive_skips") or 0) > 0 else "stopped"


def describe(row: dict) -> str:
    """One step, one clause: `2b RAG index ingest (3d; skipped 2 nights, last 2026-01-14:
    <reason>)`. The reason is the operator's own `--reason`, capped at `REASON_CAP`."""
    age = "never run" if row.get("age_days") is None else f"{row['age_days']:.0f}d"
    head = f"{row['step']} {row['label']} ({age}"
    if skip_state(row) != "skipped":
        return head + ")"
    n = int(row.get("consecutive_skips") or 0)
    nights = "1 night" if n == 1 else f"{n} nights"
    last = parse_stamp(row.get("last_skip"))
    # The NIGHT it was skipped: the owner's activity day (local clock, after-midnight cut). A late
    # Dream's UTC stamp can fall on the next UTC day, and the run-log entry the reader is being sent
    # to is filed under the local date.
    when = f", last {clock.local_today(now=last).isoformat()}" if last else ""
    reason = (row.get("note") or "").strip()
    if len(reason) > REASON_CAP:
        reason = reason[:REASON_CAP - 1].rstrip() + "…"
    why = f": {reason}" if reason else " (no reason recorded)"
    return f"{head}; skipped {nights}{when}{why})"


def summary_line(rows: list[dict]) -> str:
    """One human line for a Telegram nudge. Names the steps and their ages — and, for a step that
    was deliberately skipped, how many nights and why — because "some Dream steps are stale" is
    the kind of message that gets acknowledged and not acted on, and "stopped running" for a
    reasoned refusal is the kind that gets investigated as a crash."""
    return "; ".join(describe(r) for r in rows)


def nudge_text(rows: list[dict]) -> str:
    """The whole nudge. The headline says what KIND of silence this is, because the reader's next
    move depends on it: a stopped step wants a look at the daemon and the owner script; a skipped
    step wants the run-log entry for the night it was skipped, where the operator already wrote
    the reason. A mixed list gets the neutral headline and per-step detail does the work."""
    states = {skip_state(r) for r in rows}
    if states == {"skipped"}:
        head = ("Dream steps skipped by policy and now overdue (a deliberate skip with a reason "
                "on file, not a crash): ")
        tail = (". The full reason is in state/run-log.md's Dream entry for the night of the "
                "last skip; `python seneschal/scripts/dream_steps.py status` for the ledger.")
    elif "skipped" in states:
        head = "Dream steps overdue: "
        tail = (". A step marked 'skipped' was refused on purpose — its reason is in "
                "state/run-log.md's Dream entry for that night; the others went silent. "
                "`python seneschal/scripts/dream_steps.py status` for the ledger.")
    else:
        head = "Dream steps have stopped running: "
        tail = ". `python seneschal/scripts/dream_steps.py status` for the full ledger."
    return head + summary_line(rows) + tail


def ensure_ledger(state_dir: str, now: datetime | None = None) -> None:
    """Make sure the ledger exists and carries a `_created` stamp, AND that every declared step
    carries its own `created` stamp (the per-step grace rule). Idempotent; fail-open.

    **This closes the hole the grace window opens — twice over.** A never-run step is only overdue
    once ITS OWN `created` stamp is old enough — so a ledger that is *missing* (deleted, corrupted, a
    fresh `state/`) must still eventually alarm rather than going permanently quiet. And a step
    *newly declared* in `STEPS` long after the ledger already existed must NOT inherit the
    ledger's old birthday either — that reads a step that has existed for two hours as already
    overdue.

    So the per-step backfill runs on **every** call, not once: `_backfill_step_births` is itself
    idempotent per step (a step that already carries `created` is left alone), so a step already
    known keeps its stamp and a step that just landed in `STEPS` gets one dated to the first call
    that ever looks at it — this function, called at the top of every check/status path.

    Calling this from the daily check means the clock starts the first time anyone *looks*, which
    is the honest semantics: we can only claim a step stopped happening from the moment we began
    watching for it."""
    try:
        now = now or datetime.now(timezone.utc)
        data = _load(state_dir)
        changed = _backfill_step_births(data, now)
        if changed or not isinstance(data.get(CREATED_KEY), dict):
            _save(state_dir, data, now)
    except Exception:  # noqa: BLE001 — never fail a caller over bookkeeping
        pass


def last_nudged(state_dir: str) -> str | None:
    """The local day (YYYY-MM-DD) the owner was last told about stale steps, or None."""
    row = _load(state_dir).get(NUDGE_KEY)
    return row.get("day") if isinstance(row, dict) else None


def mark_nudged(state_dir: str, day: str) -> None:
    """Stamp that today's nudge went out. Fail-open like `record` — a lost stamp costs at worst one
    duplicate nudge tomorrow, which is enormously better than the failure it is guarding."""
    try:
        data = _load(state_dir)
        data[NUDGE_KEY] = {"day": day}
        _save(state_dir, data)
    except Exception:  # noqa: BLE001
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Dream's step ledger — what ran, and how long ago.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("record", help="stamp a step as run (or skipped)")
    p.add_argument("step", help=f"one of: {', '.join(STEPS)}")
    p.add_argument("--skip", action="store_true", help="record a SKIP rather than a run")
    p.add_argument("--reason", help="why (recorded verbatim, capped)")

    sub.add_parser("status", help="every step, its age, and whether it is overdue")
    c = sub.add_parser("check", help="exit 1 if any step is overdue")
    c.add_argument("--json", action="store_true", help="emit the overdue rows as JSON")

    for q in (p, c):
        q.add_argument("--state-dir", default=None)
    ap.add_argument("--state-dir", dest="top_state_dir", default=None)

    args = ap.parse_args(argv)
    state_dir = (getattr(args, "state_dir", None) or args.top_state_dir
                 or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state"))
    state_dir = os.path.abspath(state_dir)

    if args.cmd == "record":
        record(state_dir, args.step, ok=not args.skip, note=args.reason)
        print(f"{args.step}: recorded {'skip' if args.skip else 'ok'}")
        return 0

    # The check/status path is where a never-run step first gets seen — see ensure_ledger's
    # docstring and its per-step grace rule.
    ensure_ledger(state_dir)
    rows = report(state_dir)
    if args.cmd == "status":
        for r in rows:
            age = "never" if r["age_days"] is None else f"{r['age_days']:.1f}d"
            flag = "OVERDUE" if r["overdue"] else "ok"
            owner = r["owner"] or "(no owning script)"
            print(f"  {r['step']:<8} {r['label']:<28} {age:>8}  max {r['max_age_days']}d  "
                  f"{flag:<8} {owner}")
            if skip_state(r) == "skipped":
                # A deliberate skip is a different fact from "nothing ran" — show it where the
                # OVERDUE flag is read, not only in the raw JSON.
                print(f"           └ {describe(r).split('; ', 1)[1].rstrip(')')}")
        return 0

    bad = [r for r in rows if r["overdue"]]
    if args.json:
        print(json.dumps(bad, indent=2))
    elif bad:
        print(f"dream steps OVERDUE: {summary_line(bad)}", file=sys.stderr)
    else:
        print("dream steps: all current")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
