#!/usr/bin/env python3
"""What was happening in the interval a plan-meter reading covers.

The request behind it: *periodic logging of `/usage` alongside what was happening in that time
period, so the pair can be analysed.* **The second half is what this module is.** A reading of
*"74%"* is nearly useless on its own; *"74%, and in the hour before it there were 3 jobs running, 12
warm turns, 11 comms peeks, and the warm session was at 170k context"* is an analysable row.
`usage_probe.py` takes the reading; this takes the snapshot beside it.

**Every field comes from a source that already exists.** Nothing here instruments anything, nothing
here spawns anything, and nothing here writes: it is a pure read over five files the daemon is
already keeping.

| Block | Source | What it answers |
|---|---|---|
| `jobs` | `state/jobs/*.json` via `jobs.list_jobs` | the fleet — running now, started, ended. **Usually the single most explanatory field**: an overnight weekly-meter burn is most often the job fleet |
| `warm` | `state/metrics.jsonl`, `writer == "daemon"` | warm turns, context, cost, how many sessions the window spans |
| `governor` | `state/governor-ledger.jsonl` | the metered spend summed over the window — `billable_tokens` where rows carry it, and the raw `tokens` beside it, never one folded into the other |
| `peeks` | `state/presence.log`, `comms peek launched` | the peek COUNT — and the fact that peek COST is unknown |
| `daemon` | the running process + `state/presence.log` | did a reload or a cold warm-session start land inside the window |

**Three rules, and they are the honesty of the whole thing:**

1. **A count is only ever reported when the scan actually covered the window.** Every file here is
   read as a bounded, growing tail; if the tail did not reach back to `since`, the block says
   `scan_truncated: true` and the number is a stated LOWER BOUND rather than a measurement.
2. **A source that cannot be read produces an `error` and NO counts.** Never a zero. A zero here
   would be a lie that survives arithmetic, exactly as it would on the meter side
   (`usage_probe.assert_no_meter_fields`).
3. **Peek cost is `null` and stays `null`.** The Watch comms-peek is a bare `subprocess.Popen` with
   no pipes and no `--output-format`, so it writes to neither `metrics.jsonl` nor
   `governor-ledger.jsonl` and its spend is genuinely unrecorded. The count is real; the cost is not
   estimated into a number that would later read as measured. Instrumenting the peek (give it
   `--output-format json` and capture stdout) is the obvious follow-up and is deliberately NOT done
   here.

`collect()` never raises. A snapshot is an addition to a reading, never a reason to lose one.

The `warm` block's `metrics.jsonl` filter to `writer == "daemon"` is LOAD-BEARING, not incidental:
the file has more than one writer, and a reader that counts every row there inflates the turn count
with rows that are not turns. Any other reader of that file (the cockpit's usage panel included) must
apply the same filter. `--state-dir` IS REQUIRED WITH NO DEFAULT and `runner` is injectable end to end,
so the whole path runs with no spawn, no network and no spend. Retention is KEEP EVERYTHING — `prune`
does not exist for `plan-usage.jsonl`. Field contract and the full activity schema:
`../state/README.md` → `plan-usage.jsonl`; the design: `../docs/usage-telemetry-spec.md`. The
never-speaks rule and the account-identity work live in `usage_probe.py`'s docstring; the
announce/recover contract lives in `usage_health.py`'s.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import jobs  # for ACTIVE / list_jobs — the same reader the daemon's tick already uses

# Bounded tail reads. The first slice covers a normal hourly window many times over
# (`presence.log` runs ~1.5 MB over ~10 days); the cap is the ceiling on what one snapshot will ever
# read, and hitting it sets `scan_truncated` rather than silently under-counting.
TAIL_START_BYTES = 262_144
TAIL_CAP_BYTES = 8_388_608

# How many job records a snapshot names before it stops listing and just counts. The counts stay
# exact; only the id+title lists are capped, and the cap is reported.
JOBS_LIST_MAX = 25
JOB_TITLE_CAP = 140

PEEK_MARKER = "comms peek launched"
RELOAD_MARKER = "restart — reloading code"
COLD_SESSION_MARKER = "cold warm-session start"
PEEK_COST_NOTE = ("a comms peek writes to neither metrics.jsonl nor governor-ledger.jsonl "
                  "— the count is measured, the cost is unknown")

_LOG_TS_RE = re.compile(r"^\[([^\]]+)\]")


def _parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _in(dt, since, until) -> bool:
    return dt is not None and since <= dt < until


# --------------------------------------------------------------------------- bounded tail reading

def tail_text(path: str, since, ts_of, start: int = TAIL_START_BYTES, cap: int = TAIL_CAP_BYTES):
    """Read the smallest tail of `path` that reaches back past `since`.
    → `(lines, truncated, hit_file_start)`.

    Doubles the slice until the earliest COMPLETE line in it predates `since`, or the cap is hit.

    **`truncated=True` means the scan never got back to `since`, whatever the reason** — every count
    derived from it is a LOWER BOUND and is labelled as one. `hit_file_start` distinguishes the two
    reasons without changing that verdict: `True` means the whole file was read and the file itself
    does not reach back (a rotation, or a log younger than the window), `False` means the byte cap
    stopped the scan first. Both under-count; conflating them with a complete scan is what would make
    a short hour indistinguishable from a quiet one.

    Reading a growing append-only file this way is what keeps an hourly snapshot cheap on a
    2.8 MB `metrics.jsonl` while staying correct after a rotation leaves a nearly-empty log."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise _SourceError(f"{type(e).__name__}: {e}") from e
    want = max(1, int(start))
    while True:
        offset = max(0, size - want)
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except OSError as e:
            raise _SourceError(f"{type(e).__name__}: {e}") from e
        text = data.decode("utf-8", "replace")
        if offset > 0:
            # The first line of a mid-file slice is almost certainly a fragment — drop it rather
            # than hand a half-line to a JSON parser.
            text = text.split("\n", 1)[1] if "\n" in text else ""
        lines = text.splitlines()
        reached = False
        for line in lines:
            ts = ts_of(line)
            if ts is not None:
                reached = ts <= since
                break
        at_file_start = offset == 0
        if reached or at_file_start or want >= cap:
            return lines, not reached, at_file_start
        want = min(cap, want * 4)


class _SourceError(Exception):
    """A source that could not be read at all. Becomes an `error` block with no counts."""


def _block(fn, source: str) -> dict:
    """Run one collector, never letting it escape. Rule 2: a failure yields `{error, source}` and
    no numbers, so a consumer cannot mistake an unreadable file for a quiet hour."""
    try:
        out = fn()
    except _SourceError as e:
        return {"source": source, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"source": source, "error": f"{type(e).__name__}: {e}"}
    out["source"] = source
    return out


# --------------------------------------------------------------------------- the five blocks

def jobs_block(state_dir: str, since, until) -> dict:
    """The job fleet. Running AT the reading, plus what started and what ended since the last one.

    This is the field a burn investigation needs and otherwise lacks: a large weekly-meter move that
    lands in a long gap between two hand-captured readings has to be reconstructed afterwards from job
    logs by hand to learn *when* it burned. With this block it is a column."""
    records = jobs.list_jobs(state_dir)
    running, started, ended = [], [], []
    for rec in records:
        status = rec.get("status")
        entry = {"id": rec.get("id"),
                 "title": (rec.get("title") or "")[:JOB_TITLE_CAP],
                 "status": status}
        if status in jobs.ACTIVE:
            running.append(entry)
        if _in(_parse_iso(rec.get("started_at") or rec.get("created_at")), since, until):
            started.append(entry)
        if _in(_parse_iso(rec.get("ended_at")), since, until):
            ended.append(entry)
    out = {"scanned_records": len(records)}
    for name, items in (("running", running), ("started", started), ("ended", ended)):
        out[f"{name}_count"] = len(items)
        out[name] = items[:JOBS_LIST_MAX]
        if len(items) > JOBS_LIST_MAX:
            out[f"{name}_list_truncated"] = True
    return out


def warm_block(state_dir: str, since, until) -> dict:
    """Warm chat turns in the window, from `metrics.jsonl`.

    **Filtered to `writer == "daemon"`, and that filter is load-bearing.** The file has two writers
    (`state/README.md`): `presence._append_turn_metrics` writes one row per warm turn, and the
    Observability advisor emits its own rows prompt-side. A reader that counts every row there with
    no filter counts advisor rows as turns — and inflates exactly the number that matters most here —
    so every reader of this file must apply this same filter.

    `context_tokens`, `cost_usd` and `session_cost_usd` are OPTIONAL on a row (older rows predate
    them). An absent field is skipped, never read as zero — so `context_tokens_max` is absent
    entirely rather than 0 when no row in the window carried one."""
    lines, truncated, hit_start = tail_text(os.path.join(state_dir, "metrics.jsonl"), since,
                                            _jsonl_ts)
    turns = 0
    sessions: list = []
    contexts: list = []
    cost = 0.0
    cost_rows = 0
    tokens = 0
    token_rows = 0
    last_row = None
    for line in lines:
        row = _loads(line)
        if row is None or row.get("writer") != "daemon":
            continue
        if not _in(_parse_iso(row.get("ts")), since, until):
            continue
        turns += 1
        last_row = row
        sid = row.get("session_id")
        if isinstance(sid, str) and sid not in sessions:
            sessions.append(sid)
        ctx = row.get("context_tokens")
        if isinstance(ctx, (int, float)):
            contexts.append(int(ctx))
        c = row.get("cost_usd")
        if isinstance(c, (int, float)):
            cost += float(c)
            cost_rows += 1
        t = row.get("tokens")
        if isinstance(t, (int, float)):
            tokens += int(t)
            token_rows += 1
    out = {"turns": turns, "sessions": sessions, "session_count": len(sessions),
           "scan_truncated": truncated}
    if truncated:
        out["scan_hit_file_start"] = hit_start
    if contexts:
        out["context_tokens_max"] = max(contexts)
        out["context_tokens_last"] = contexts[-1]
    if cost_rows:
        out["cost_usd"] = round(cost, 6)
        out["cost_rows"] = cost_rows
    if token_rows:
        out["tokens"] = tokens
        out["token_rows"] = token_rows
    if last_row is not None:
        for key in ("turns_served", "session_cost_usd", "model"):
            if key in last_row:
                out[f"last_{key}"] = last_row[key]
    return out


def governor_block(state_dir: str, since, until) -> dict:
    """`billable_tokens` (and the raw `tokens`) summed over the window, plus the lever counts that
    explain them.

    The ledger is Oikonomos's (`governor.py`) record of the assistant's own metered spend. **It counts
    a different thing from the plan meter and is not a check on it** (`../docs/usage-telemetry-spec.md`
    §7.7); it is here as a
    correlate, so a weekly-meter move can be set beside what the daemon itself metered in the same
    hour. `basis` is carried verbatim so a change of basis is visible rather than absorbed."""
    lines, truncated, hit_start = tail_text(os.path.join(state_dir, "governor-ledger.jsonl"),
                                            since, _jsonl_ts)
    rows = 0
    billable = 0
    billable_rows = 0
    raw = 0
    raw_rows = 0
    bases: list = []
    kinds: dict = {}
    levers = {"tool_calls": 0, "tool_result_bytes": 0, "tool_result_images": 0}
    lever_rows = 0
    for line in lines:
        row = _loads(line)
        if row is None or not _in(_parse_iso(row.get("ts")), since, until):
            continue
        rows += 1
        kinds[row.get("kind")] = kinds.get(row.get("kind"), 0) + 1
        b = row.get("billable_tokens")
        if isinstance(b, (int, float)):
            billable += int(b)
            billable_rows += 1
        t = row.get("tokens")
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            raw += int(t)
            raw_rows += 1
        basis = row.get("basis")
        if isinstance(basis, str) and basis not in bases:
            bases.append(basis)
        lv = row.get("levers")
        if isinstance(lv, dict):
            lever_rows += 1
            for key in levers:
                v = lv.get(key)
                if isinstance(v, (int, float)):
                    levers[key] += int(v)
    out = {"rows": rows, "kinds": kinds, "scan_truncated": truncated}
    if truncated:
        out["scan_hit_file_start"] = hit_start
    if billable_rows:
        out["billable_tokens"] = billable
        out["billable_rows"] = billable_rows
        out["basis"] = bases
    if raw_rows:
        # The flat raw `tokens` sum, reported beside (never folded into) `billable_tokens`: a ledger
        # written before the billable breakdown existed carries only this, and absent-not-zero holds.
        out["tokens"] = raw
        out["token_rows"] = raw_rows
    if lever_rows:
        # `levers` is omitted-not-zeroed on the source rows too, so an absent block here means "no
        # row in the window carried one", never "the turns did nothing".
        out["levers"] = levers
        out["lever_rows"] = lever_rows
    return out


def log_block(state_dir: str, since, until) -> dict:
    """One pass over `presence.log` for the three markers a reading wants: peeks, daemon reloads and
    cold warm-session starts.

    Returns `(peeks, daemon_markers)` merged by `collect`. The log rotates at 2 MB to `.log.1`; this
    reads only the live file, so an interval that spans a rotation comes back `scan_truncated: true`
    with a lower-bound count — which is the honest answer and cheaper than stitching two files."""
    lines, truncated, hit_start = tail_text(os.path.join(state_dir, "presence.log"), since,
                                            _log_ts)
    peeks = reloads = cold = 0
    for line in lines:
        ts = _log_ts(line)
        if not _in(ts, since, until):
            continue
        if PEEK_MARKER in line:
            peeks += 1
        elif RELOAD_MARKER in line:
            reloads += 1
        elif COLD_SESSION_MARKER in line:
            cold += 1
    out = {"peeks_launched": peeks, "reloads": reloads, "cold_warm_session_starts": cold,
           "scan_truncated": truncated}
    if truncated:
        # `presence.log` rotates at 2 MB to `.log.1`; only the live file is read, so an interval
        # that spans a rotation comes back here. The lower bound is the honest answer and is cheaper
        # than stitching two files together for a count.
        out["scan_hit_file_start"] = hit_start
    return out


def daemon_block(daemon, since, until, log_part: dict) -> dict:
    """Whether the daemon itself turned over inside the window.

    **A restart matters because it resets the warm session's context**, and the `/usage` panel's own
    behaviour lines typically show context size as the dominant usage characteristic (most usage at
    >150k context).
    So a window containing a reload is a window where the most expensive thing about a turn changed
    partway through, and a burn-rate read across it without noticing that is a wrong read.

    `daemon` is the live process's own `{pid, started_at}`, passed in by the caller because only the
    daemon knows it. Run from the CLI there is no such process, and the block says so (`known:
    false`) rather than inventing one — the log-derived markers still work either way."""
    out = {"known": False}
    if isinstance(daemon, dict):
        started = _parse_iso(daemon.get("started_at"))
        out = {"known": True, "pid": daemon.get("pid"),
               "started_at": started.isoformat(timespec="seconds") if started else None,
               "restarted_in_interval": bool(started and _in(started, since, until))}
        if started:
            out["uptime_sec"] = round((until - started).total_seconds(), 1)
    for key in ("reloads", "cold_warm_session_starts", "scan_truncated"):
        if key in log_part:
            out[key] = log_part[key]
    if "error" in log_part:
        out["log_error"] = log_part["error"]
    return out


def _loads(line: str):
    line = (line or "").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _jsonl_ts(line: str):
    row = _loads(line)
    return _parse_iso(row.get("ts")) if row else None


def _log_ts(line: str):
    m = _LOG_TS_RE.match(line or "")
    return _parse_iso(m.group(1)) if m else None


# --------------------------------------------------------------------------- the snapshot

def collect(state_dir: str, since, until, *, daemon=None) -> dict:
    """The activity snapshot for `[since, until)`. Never raises.

    `since` and `until` are aware datetimes — the previous reading's `at` and this one's. The caller
    puts the same window on the row as `interval`, so a reader never has to assume what period these
    numbers cover."""
    log_part = _block(lambda: log_block(state_dir, since, until), "state/presence.log")
    peeks = {"source": log_part["source"]}
    if "error" in log_part:
        peeks["error"] = log_part["error"]
    else:
        peeks["launched"] = log_part["peeks_launched"]
        peeks["scan_truncated"] = log_part["scan_truncated"]
    # Rule 3, always present and always null: the count is measured, the cost is not knowable, and
    # writing a plausible estimate here would produce a number that reads as measured a month later.
    peeks["cost_usd"] = None
    peeks["cost_known"] = False
    peeks["cost_note"] = PEEK_COST_NOTE
    return {
        "jobs": _block(lambda: jobs_block(state_dir, since, until), "state/jobs/*.json"),
        "warm": _block(lambda: warm_block(state_dir, since, until),
                       "state/metrics.jsonl (writer=daemon)"),
        "governor": _block(lambda: governor_block(state_dir, since, until),
                           "state/governor-ledger.jsonl"),
        "peeks": peeks,
        "daemon": _block(lambda: daemon_block(daemon, since, until, log_part),
                         "the daemon process + state/presence.log"),
    }
