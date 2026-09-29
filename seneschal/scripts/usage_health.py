#!/usr/bin/env python3
"""§8.3 of `../docs/usage-telemetry-spec.md`: when the plan-meter INSTRUMENT breaks, say so.

The design brief is one sentence: *failure should speak — but nothing excessive.*

Both halves of it are load-bearing. This module is the *"nothing excessive"* half made executable —
it can produce **at most two messages per outage, ever**:

  1. one when a break has lasted `FAILURE_STREAK` readings (~3 h at the hourly cadence), and
  2. one when it recovers — **and only if the break was announced.** A break nobody was told about
     recovers in silence.

No ladder, no re-nag, no escalation, no second opinion at hour six.

**IT SAYS NOTHING ABOUT THE METER.** Whether the usage LEVEL may ever speak is spec §8.2, and its
conservative branch holds: the level never speaks, unconditionally. This speaks about the instrument,
not the meter, and the two must not leak into each other — so nothing here reads a percentage, and
``TheLevelStillNeverSpeaks`` in `test_usage_health.py` asserts that against the source rather than
trusting this paragraph. A message from here carries an outcome name, two timestamps, a count and a
file path. That is the entire vocabulary.

## What counts as a failure, and the one place this departs from "non-ok"

======================  ==============================================================
``ok`` / ``partial``    **success — it breaks a run.** A reading landed and the series
                        moved. ``partial`` is a degraded parse, and a degraded parse
                        that persists is exactly the wording change §3.4 wants
                        surfaced, so it counts toward a break rather than papering
                        over one.
``unparsed``            failure — zero meters matched.
``auth_failed``         failure — see the canary note in spec §8.5.
``spawn_failed``        failure — the child never started, or exited non-zero.
``timeout``             failure — the reading hung past its ceiling.
``skipped``             **TRANSPARENT: it neither counts toward a run nor breaks one.**
======================  ==============================================================

`skipped` is the departure, and it is deliberate. A `skipped` row is not a broken instrument — it is
the daemon *declining* to read because a warm turn, a headless child or a live Claude Code session
held the deferral gate past the grace (the daemon's usage gate). At the hourly cadence three of those
in a row means a ~3.5 h working session, which is an ordinary day. Counting them would send *"the
instrument is broken"* in the middle of an afternoon when it is working perfectly — the textbook
false alarm, and the fastest way to make a real one unreadable. Making it transparent rather than
run-breaking is the conservative half of the same choice: a skip does not prove the instrument
works, so it must not silently reset a genuine break either.

## Where the episode state lives, and why a restart cannot re-announce

**The consecutive-failure count is DERIVED from `state/plan-usage.jsonl`, every time**, by walking
the tail backwards to the first success. There is no counter to drift, and a restart re-derives the
same answer from the same rows.

The one thing the rows cannot carry is *"the owner was already told"* — the row was written before
the decision existed. So exactly one persisted flag is added, in the file that already exists for
this task's bookkeeping: a `notice` block inside `state/last-usage-reading` (documented in
`../state/README.md`). It records the episode it announced, **keyed by that episode's own
`started_at`** — which is derived from the rows, not invented. That key is what makes the two
restart hazards impossible:

* **Re-announcing a live episode**: the key matches, so the second announcement is refused.
* **Resurrecting a resolved one**: the recovery message clears the block, and re-deriving finds a
  success at the head of the stream and no notice — nothing to say.

Losing the stamp file costs at most one duplicate announcement, which is the direction to fail in:
failure should speak.

Fully testable with no spawn, no network and no send: `send` is injected exactly the way
`usage_probe`'s `runner` is, and `state_dir` is required everywhere — nothing here defaults into the
live state directory.

The caller (the daemon's usage-notice hook) speaks through the Mouth when it is present. The
`notice` block in `state/last-usage-reading` is stamped ONLY AFTER A LANDED SEND — an announcement
that never reached the owner (e.g. because the quiet window held it) must not be recorded as having
happened, or the episode would retire silently on a message nobody got. Whether `auth_failed` should
bypass the failure-streak threshold entirely (it means NOTHING can spawn) is spec §8.5, an open
decision for the owner. The never-speaks rule for the meter itself lives in `usage_probe.py`'s
docstring.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import usage_probe as up

# The failure set. `partial` is in it on purpose (a persistent degraded parse is a break); `skipped`
# is out of it on purpose (see the module docstring). One tuple, so changing the decision is one edit.
FAILURE_OUTCOMES = (up.PARTIAL, up.UNPARSED, up.AUTH_FAILED, up.SPAWN_FAILED, up.TIMEOUT)
# Outcomes that END a run of failures.
SUCCESS_OUTCOMES = (up.OK,)
# Outcomes that are neither: they pass straight through the walk without affecting it.
TRANSPARENT_OUTCOMES = (up.SKIPPED,)

# §8.3's suggested N. At the hourly default this is ~3 h of blindness before a single word is said.
# **A one-off stays silent** — a missed reading is a missing row, not an incident, and that principle
# from the spec survives the decision that failure may speak at all.
FAILURE_STREAK = 3

# The one persisted flag (see the docstring). A key inside the EXISTING cadence stamp, not a new
# sidecar file.
NOTICE_KEY = "notice"

# How much of the append-only series to walk. Rows measure ~2.2 KB, so this is ~116 readings ≈ 4.8
# days at the hourly cadence — far past any streak worth announcing, and bounded so the tick never
# reads a file that grows forever (§4.4 keeps everything).
TAIL_BYTES = 256 * 1024


# --------------------------------------------------------------------------- reading the series

def read_series(state_dir: str, tail_bytes: int = TAIL_BYTES) -> tuple:
    """``(rows, complete)`` — the tail of `plan-usage.jsonl`, oldest first, and whether that tail is
    the WHOLE file. A torn or unparseable line costs that line only, the same posture as
    `usage_probe.last_row` and for the same reason: a crash mid-append must not blind the health
    check. `complete` is what stops a bounded read from being reported as a measured one."""
    path = up.readings_path(state_dir)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            data = fh.read()
    except OSError:
        return [], True  # no file yet is not a truncated read; it is an empty series
    complete = size <= tail_bytes
    lines = data.decode("utf-8", "replace").splitlines()
    if not complete and lines:
        lines = lines[1:]  # the seek almost certainly landed mid-row; drop the fragment
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows, complete


def recent_rows(state_dir: str, tail_bytes: int = TAIL_BYTES) -> list:
    """Just the rows from :func:`read_series`, for a caller that does not care about the bound."""
    return read_series(state_dir, tail_bytes)[0]


def current_episode(rows: list, complete: bool = True) -> dict | None:
    """The unbroken run of failures at the head of the series, or None if the last reading landed.

    Returns ``{"started_at", "last_at", "attempts", "outcomes", "last_outcome", "truncated"}``.

    ``truncated`` means the walk ran off the start of a BOUNDED tail without finding a success, so
    ``started_at`` is a lower bound on how long this has been going and ``attempts`` is a lower bound
    on how many — stated as such rather than presented as measured (§10.2 rule 4, applied here). Pass
    ``complete=False`` when `rows` is a truncated read; a run that reaches the start of the *whole*
    file is not truncated, it is simply a series that has never had a good reading."""
    if not rows:
        return None
    run = []
    truncated = not complete
    for row in reversed(rows):
        outcome = (row or {}).get("outcome")
        if outcome in TRANSPARENT_OUTCOMES:
            continue
        if outcome in FAILURE_OUTCOMES:
            run.append(row)
            continue
        truncated = False  # a success (or an outcome we do not classify) ends the walk
        break
    if not run:
        return None
    run.reverse()
    counts: dict = {}
    for row in run:
        key = row.get("outcome")
        counts[key] = counts.get(key, 0) + 1
    return {
        "started_at": run[0].get("at"),
        "last_at": run[-1].get("at"),
        "attempts": len(run),
        "outcomes": counts,
        "last_outcome": run[-1].get("outcome"),
        "truncated": truncated,
    }


# --------------------------------------------------------------------------- the quiet window

def in_quiet(state_dir: str, now: datetime | None = None) -> bool:
    """Is this a moment the daemon is already forbidden to buzz the owner at?

    **Both gates are the daemon's existing ones, imported rather than copied** — `sentinel.is_quiet`
    (the durable do-not-disturb window Chat writes when the owner says *"quiet till morning"*) and
    the owner's night curfew (`sentinel.curfew_window`, i.e. `owner.nightCurfew`), read on the
    owner's wall clock through `clock.to_local` so it is DST-correct. A second pair of numbers here
    would drift from the first the moment either changed, and *"is 3 a.m. a reasonable hour"* is a
    question this tree has already answered once.

    **A dead instrument is not a Critical reminder and never pierces.** Nothing here sets
    `pierce_quiet`; a notice that comes due inside the window simply is not sent, and the next
    reading re-evaluates — so it goes out when the window opens rather than being lost.

    **Fails toward SPEAKING.** If the hour cannot be determined we are not quiet: the design is that
    failure should speak, and a mistimed message is loud where a swallowed one is silent."""
    now = now or datetime.now(timezone.utc)
    try:
        import clock
        import sentinel
        if sentinel.is_quiet(state_dir, now):
            return True
        start, end = sentinel.curfew_window()
        if start == end:  # an explicitly disabled curfew
            return False
        t = clock.to_local(now).time()
        if start < end:
            return start <= t < end
        return t >= start or t < end  # a window that wraps midnight
    except Exception:  # noqa: BLE001 — cannot tell the hour ⇒ speak, and say nothing about the hour
        return False


# --------------------------------------------------------------------------- the message

def _clock(value) -> str:
    """A stamp the owner can check against their own memory of the day — `"Thu 2:00 PM"`. An as-of time, not
    a duration, for `mouth.staleness_prefix`'s reason: *"(from 14:42)"* is checkable and *"17
    minutes ago"* is not. An unparseable stamp degrades to its raw text rather than to a guess."""
    dt = up.parse_iso(value) if not isinstance(value, datetime) else value
    if dt is None:
        return str(value)
    return dt.strftime("%a ") + dt.strftime("%I:%M %p").lstrip("0")


def summarise_outcomes(counts: dict) -> str:
    """`"all auth_failed"` / `"timeout ×3, unparsed"`. Outcome NAMES only — this is the whole
    diagnostic vocabulary the message is allowed, and it is the taxonomy's own words (§3.3)."""
    if not counts:
        return "no outcome recorded"
    if len(counts) == 1:
        name, n = next(iter(counts.items()))
        return f"all {name}" if n > 1 else str(name)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return ", ".join(f"{k} ×{v}" if v > 1 else str(k) for k, v in ordered)


def break_text(episode: dict, log_path: str) -> str:
    """What is broken, since when, how many attempts, and where to look. Nothing else — no meter
    number, no diagnosis, no instruction, no *"you may want to"*. It reports an instrument state."""
    since = _clock(episode.get("started_at"))
    attempts = episode.get("attempts", 0)
    if episode.get("truncated"):
        return (f"Plan-meter readings have been failing since at least {since} — at least "
                f"{attempts} attempts, {summarise_outcomes(episode.get('outcomes') or {})}. "
                f"Log: {log_path}")
    return (f"Plan-meter readings have been failing since {since} — {attempts} attempts, "
            f"{summarise_outcomes(episode.get('outcomes') or {})}. Log: {log_path}")


def recovery_text(notice: dict, now, log_path: str) -> str:
    """The closing bracket, sent only when the opening one was. Same vocabulary, no verdict on why."""
    return (f"Plan-meter readings are working again — the gap ran "
            f"{_clock((notice or {}).get('episode_started_at'))} to {_clock(now)}. "
            f"Log: {log_path}")


# --------------------------------------------------------------------------- the decision

def plan_notice(rows: list, notice, now, *, log_path: str, quiet: bool = False,
                streak: int = FAILURE_STREAK, complete: bool = True) -> dict | None:
    """What (if anything) the instrument owes the owner right now. Pure → unit-tested.

    Returns None (say nothing), or ``{"kind", "text", "notice", "deferred", "observed_at"}`` where
    ``kind`` is ``"break"`` or ``"recovery"``, ``notice`` is the value the caller must persist under
    :data:`NOTICE_KEY` (``None`` means *clear it*), and ``deferred`` means *the quiet window is
    holding this* — the caller sends nothing, persists nothing, and the next reading asks again.

    ``observed_at`` is **when the fact became true**, which is the row that made it true and not the
    tick that noticed — `mouth.enqueue`'s §3.2 contract, and the difference between an honest as-of
    stamp on a quiet-deferred notice and a message that reads as fresh news at 7 a.m.

    The four rules, in the order they are checked:

    1. **An empty series says nothing.** No rows is not a recovery; it is no information.
    2. **A live episode already announced stays silent** — one message per episode, forever. A
       `truncated` run counts as the same episode: we cannot see a success since the notice, so the
       break never ended.
    3. **A recovery speaks only if the break did.** No notice ⇒ nothing to close.
    4. **The threshold is a floor, not an equality.** Firing only at exactly `streak` would mean a
       tick that lands on attempt 4 (a deferral that just cleared, a restart) never speaks at all.
    """
    if not rows:
        return None
    episode = current_episode(rows, complete)
    announced = (notice or {}).get("episode_started_at") if isinstance(notice, dict) else None

    if episode is None:
        if announced is None:
            return None
        return {"kind": "recovery", "deferred": bool(quiet), "notice": None,
                "observed_at": rows[-1].get("at"),
                "text": recovery_text(notice, now, log_path)}

    same = announced is not None and (announced == episode["started_at"] or episode["truncated"])
    if same or episode["attempts"] < max(1, int(streak)):
        return None
    return {
        "kind": "break",
        "deferred": bool(quiet),
        "observed_at": episode["last_at"],
        "notice": {
            "episode_started_at": episode["started_at"],
            "announced_at": now.isoformat(timespec="seconds") if isinstance(now, datetime) else now,
            "attempts": episode["attempts"],
            "last_outcome": episode["last_outcome"],
        },
        "text": break_text(episode, log_path),
    }


def decide(state_dir: str, notice, now: datetime, *, streak: int = FAILURE_STREAK,
           quiet=None) -> dict | None:
    """:func:`plan_notice` over the live series — the one door a caller needs.

    `quiet` is an injectable predicate (``f(state_dir, now) -> bool``) so the window can be exercised
    without touching the host's clock or its quiet file; production passes nothing and gets
    :func:`in_quiet`."""
    quiet_fn = in_quiet if quiet is None else quiet
    rows, complete = read_series(state_dir)
    return plan_notice(rows, notice, now, log_path=up.readings_path(state_dir),
                       quiet=bool(quiet_fn(state_dir, now)), streak=streak, complete=complete)
