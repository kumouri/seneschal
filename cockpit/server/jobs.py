"""Read-only, tolerant reader for the cockpit's Jobs panel — the durable background jobs
`seneschal/scripts/jobs.py` writes (design: `seneschal/docs/background-jobs-spec.md`).

**What it reads.** `<state dir>/jobs/<id>.json` — one record per job — plus the sibling
`<id>.log` for a tail. Nothing here writes, kills, cancels, or starts anything: the cockpit is an
observatory, and a job's lifecycle belongs to the daemon and to `jobs.py` itself. In particular the
panel deliberately has **no cancel button** — cancelling is `jobs.py cancel`, which is a decision, not
a dashboard click.

**Deliberately NOT importing `seneschal/scripts/jobs.py`.** The cockpit is its own dependency world
(cockpit-spec.md, the same posture `governor.py` and `model_config.py` take here): the
backend must keep working against a state dir written by a daemon on a different commit. So this is
an independent reader of the on-disk shape, not a shared library — which also means it must tolerate
records from an *older or newer* `jobs.py` than it was written against. Every field is read
defensively and every unknown status passes through as-is rather than being coerced.

**Tolerance, same posture as `health.py` / `readers.py`.** A missing `jobs/` dir, a corrupt record, a
stray file, an unreadable log, a garbled timestamp — each degrades to an honest `"available": false`
or a dropped row, never a 500 and never an exception. The one thing worth stating plainly: an empty
but *present* jobs dir is `available: true` with no jobs, which is different from "the jobs feature
isn't here" — the panel says so, because "no jobs right now" and "I can't see jobs at all" should not
look the same.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

JOBS_DIRNAME = "jobs"

DEFAULT_LIMIT = 40
MAX_LIMIT = 200
DEFAULT_TAIL_LINES = 3       # in the list payload — enough to see the outcome line
DEFAULT_DETAIL_LINES = 60    # in the single-job payload
MAX_TAIL_LINES = 500
TAIL_READ_BYTES = 64 * 1024  # cap what we read off the end of a log, however big it grew

RUNNING = "running"
TERMINAL = ("done", "failed", "timed-out", "ended-unknown", "cancelled")

# Job ids are minted as `YYYYmmdd-HHMMSS-xxxx` (jobs.new_job_id). Anything outside this alphabet is
# rejected before it can reach the filesystem — the id arrives from a URL path segment, so this is
# the path-traversal guard, not a cosmetic validation.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _jobs_dir(state_dir) -> Path:
    return Path(state_dir) / JOBS_DIRNAME


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _duration_sec(rec: dict, now: Optional[datetime] = None) -> Optional[float]:
    """Elapsed for a running job, total for a finished one. `None` when the record's timestamps are
    unreadable — the panel then just omits the duration rather than showing a wrong one."""
    start = _parse_iso(rec.get("started_at")) or _parse_iso(rec.get("created_at"))
    if start is None:
        return None
    end = _parse_iso(rec.get("ended_at")) or (now or datetime.now(timezone.utc))
    return max(0.0, (end - start).total_seconds())


def log_tail(path: Any, lines: int = DEFAULT_TAIL_LINES) -> list:
    """Last `lines` non-empty lines of a job log, reading at most the final `TAIL_READ_BYTES`.
    Unreadable / absent / binary -> `[]`, never raises."""
    if not path or lines <= 0:
        return []
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > TAIL_READ_BYTES:
                fh.seek(-TAIL_READ_BYTES, 2)
            blob = fh.read()
    except (OSError, ValueError):
        return []
    text = blob.decode("utf-8", errors="replace")
    kept = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return kept[-min(lines, MAX_TAIL_LINES):]


def _normalize(rec: dict, state_dir, tail_lines: int, now: Optional[datetime] = None) -> Optional[dict]:
    """One on-disk record -> the panel's shape. `None` if it isn't recognizably a job."""
    if not isinstance(rec, dict):
        return None
    job_id = rec.get("id")
    if not isinstance(job_id, str) or not job_id:
        return None
    status = rec.get("status") if isinstance(rec.get("status"), str) else "unknown"
    notify = rec.get("notify") if isinstance(rec.get("notify"), dict) else {}
    log_path = rec.get("log_path")
    if not isinstance(log_path, str) or not log_path:
        # Fall back to the conventional sibling path so an older record without the field still
        # shows a tail.
        log_path = str(_jobs_dir(state_dir) / f"{job_id}.log")
    argv = rec.get("argv")
    return {
        "id": job_id,
        "title": rec.get("title") if isinstance(rec.get("title"), str) else job_id,
        "status": status,
        "is_running": status == RUNNING,
        "is_terminal": status in TERMINAL,
        "exit_code": rec.get("exit_code") if isinstance(rec.get("exit_code"), int) else None,
        "created_at": rec.get("created_at") if isinstance(rec.get("created_at"), str) else None,
        "started_at": rec.get("started_at") if isinstance(rec.get("started_at"), str) else None,
        "ended_at": rec.get("ended_at") if isinstance(rec.get("ended_at"), str) else None,
        "duration_sec": _duration_sec(rec, now),
        "deadline_sec": rec.get("deadline_sec") if isinstance(rec.get("deadline_sec"), int) else None,
        "lease": bool(rec.get("lease")),
        "wake": bool(rec.get("wake")),
        # `notified_at` is the honest "was the owner actually told" field: jobs.py stamps it only
        # once the push landed — or once it provably went out and cannot safely be re-sent, which
        # the record then says in `notify_delivery: "ambiguous"`. Either way a terminal
        # job with a NULL one is a ping still being retried, and the panel surfaces exactly that.
        "notified_at": rec.get("notified_at") if isinstance(rec.get("notified_at"), str) else None,
        "notify_channel": notify.get("channel") if isinstance(notify.get("channel"), str) else None,
        "pid": rec.get("pid") if isinstance(rec.get("pid"), int) else None,
        "error": rec.get("error") if isinstance(rec.get("error"), str) else None,
        "command": " ".join(str(a) for a in argv) if isinstance(argv, list) else None,
        "log_path": log_path,
        "log_tail": log_tail(log_path, tail_lines),
    }


def _load_records(state_dir, tail_lines: int, now: Optional[datetime] = None) -> Optional[list]:
    """Every readable job record. `None` (not `[]`) when the jobs dir itself is missing/unreadable —
    the caller turns that into `available: false`, keeping "nothing running" distinct from "can't
    see jobs at all"."""
    directory = _jobs_dir(state_dir)
    try:
        names = sorted(p.name for p in directory.iterdir() if p.suffix == ".json")
    except (OSError, ValueError):
        return None
    out: list = []
    for name in names:
        try:
            with open(directory / name, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
        job = _normalize(raw, state_dir, tail_lines, now)
        if job is not None:
            out.append(job)
    return out


def _started_key(job: dict) -> str:
    return job.get("started_at") or job.get("created_at") or job.get("id") or ""


def _ended_key(job: dict) -> str:
    """Finished jobs sort by when they FINISHED, not when they started — a long job that began first
    but ended last belongs at the top of "recently finished", which is what the reader of that list
    is actually asking about. Falls back to the start keys so a record with no `ended_at` still
    orders deterministically instead of collapsing to the empty string."""
    return job.get("ended_at") or _started_key(job)


def read_jobs(state_dir, limit: Optional[int] = DEFAULT_LIMIT,
              tail_lines: Optional[int] = DEFAULT_TAIL_LINES,
              now: Optional[datetime] = None) -> dict:
    """The Jobs panel's payload: everything still running, plus the most recent finished ones.

    Running jobs are **never** truncated by `limit` — a long-running fleet must not push its own
    members out of view; `limit` bounds only the finished tail. `awaiting_push` counts terminal jobs
    whose completion push hasn't landed yet, which is the one number worth alarming on: it means the
    daemon is trying to reach the owner and failing."""
    try:
        n = max(1, min(int(limit) if limit is not None else DEFAULT_LIMIT, MAX_LIMIT))
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    try:
        tail = max(0, min(int(tail_lines) if tail_lines is not None else DEFAULT_TAIL_LINES,
                          MAX_TAIL_LINES))
    except (TypeError, ValueError):
        tail = DEFAULT_TAIL_LINES

    records = _load_records(state_dir, tail, now)
    if records is None:
        return {"available": False, "active": [], "recent": [], "counts": {},
                "active_count": 0, "lease_held": False, "awaiting_push": 0}

    # Running: oldest first, so whatever has been going longest is at the top (that's the one you
    # want to notice). Finished: most recently ended first.
    active = sorted((j for j in records if j["is_running"]), key=_started_key)
    finished = sorted((j for j in records if not j["is_running"]), key=_ended_key, reverse=True)[:n]

    counts: dict[str, int] = {}
    for job in records:
        counts[job["status"]] = counts.get(job["status"], 0) + 1

    return {
        "available": True,
        "active": active,
        "recent": finished,
        "counts": counts,
        "active_count": len(active),
        "lease_held": any(j["lease"] for j in active),
        "awaiting_push": sum(1 for j in records if j["is_terminal"] and not j["notified_at"]),
    }


def read_job(state_dir, job_id: str, tail_lines: Optional[int] = DEFAULT_DETAIL_LINES) -> dict:
    """One job with a longer log tail — what the panel opens when a job failed and you want to know
    why. Returns `{"available": false}` for an unknown, unreadable, or unsafely-named id."""
    if not isinstance(job_id, str) or not _SAFE_ID.match(job_id):
        return {"available": False, "job": None}
    try:
        tail = max(0, min(int(tail_lines) if tail_lines is not None else DEFAULT_DETAIL_LINES,
                          MAX_TAIL_LINES))
    except (TypeError, ValueError):
        tail = DEFAULT_DETAIL_LINES
    path = _jobs_dir(state_dir) / f"{job_id}.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {"available": False, "job": None}
    job = _normalize(raw, state_dir, tail)
    return {"available": job is not None, "job": job}
