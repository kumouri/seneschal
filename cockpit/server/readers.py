"""Read-only, tolerant readers over `seneschal/state/*` for the cockpit's v1 monitor endpoints.

Every function here degrades gracefully: a missing file, an empty directory, a malformed line, or an
unexpected shape returns a sane empty/default result rather than raising — these are cheap dashboard
reads, not a system of record, and a stale/broken cache file must never 500 the whole panel. See
../../seneschal/state/README.md for the on-disk schemas this mirrors (sessions/, session-distillations.jsonl,
seneschald-health.json, presence-context.json, reminders.json, metrics.jsonl).

Nothing in this module writes anything — that split (reads here, the two sanctioned writes in
control.py) is deliberate, per the cockpit-spec.md posture ("ALL reads are read-only").
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Mirrors seneschal/scripts/sentinel.py's SESSION_TTL_SEC / SESSION_VISIBLE_TTL_SEC — kept as local
# constants (not imported) so the cockpit stays its own dependency world (cockpit-spec.md ruling 3)
# and never depends on daemon script internals for its read path.
SESSION_GATING_SOURCES = frozenset({"daemon", "desktop"})
SESSION_GATING_TTL_SEC = 120
SESSION_AWARENESS_TTL_SEC = 3600


def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


def _read_lines(path: Path) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.readlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return []


def _parse_iso(value: Any) -> datetime | None:
    """Best-effort ISO-8601 parse, tolerant of a trailing 'Z' and naive (assumed-UTC) timestamps —
    the registry/health/reminders files mix both conventions across the codebase."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_sessions(state_dir: Path) -> dict:
    """Every JSON file in state/sessions/, tolerant of garbage, with a computed freshness/live flag.
    `daemon`/`desktop` entries gate reminder delivery (120 s TTL, see sentinel.py); `build`/`scheduled`
    entries are awareness-only (a longer 1 h TTL) — both are returned so the dashboard can show idle
    entries too, not just live ones."""
    sessions_dir = state_dir / "sessions"
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    try:
        paths = sorted(sessions_dir.glob("*.json"))
    except OSError:
        paths = []
    for path in paths:
        data = _read_json(path, None)
        if not isinstance(data, dict):
            continue
        source = data.get("source") or "unknown"
        last_seen_raw = data.get("last_seen")
        last_seen = _parse_iso(last_seen_raw)
        age_seconds = (now - last_seen).total_seconds() if last_seen else None
        gates_delivery = source in SESSION_GATING_SOURCES
        ttl = SESSION_GATING_TTL_SEC if gates_delivery else SESSION_AWARENESS_TTL_SEC
        live = age_seconds is not None and age_seconds < ttl
        out.append({
            "file": path.name,
            "session_id": data.get("session_id"),
            "source": source,
            "pid": data.get("pid"),
            "started_at": data.get("started_at"),
            "last_seen": last_seen_raw,
            "working_on": data.get("working_on"),
            "cwd": data.get("cwd"),
            "branch": data.get("branch"),
            "phase": data.get("phase"),
            "age_seconds": age_seconds,
            "gates_delivery": gates_delivery,
            "ttl_seconds": ttl,
            "live": live,
        })
    out.sort(key=lambda s: s.get("last_seen") or "", reverse=True)
    return {"sessions": out, "count": len(out)}


def read_oneiroi(state_dir: Path, limit: int = 20) -> dict:
    """Last N records of session-distillations.jsonl (the mini-dream log), newest first. Tolerant
    JSONL tail: a broken line is skipped and doesn't count against the limit — every line is parsed
    first, THEN the last N *valid* records are kept, so a bit of trailing corruption never shorts the
    feed below what was asked for."""
    limit = max(1, min(int(limit or 20), 500))
    records: list[dict] = []
    for line in _read_lines(state_dir / "session-distillations.jsonl"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    records = records[-limit:]
    records.reverse()
    return {"oneiroi": records, "count": len(records)}


def read_seneschald_health(state_dir: Path) -> dict:
    """Passthrough of seneschald-health.json + a derived `last_ok_age_seconds` (the watch-the-watcher
    field — see state/README.md: if it stops advancing, seneschald-update itself is dead)."""
    data = _read_json(state_dir / "seneschald-health.json", None)
    if not isinstance(data, dict):
        return {"available": False}
    result: dict = {"available": True, **data}
    last_ok = _parse_iso(data.get("last_ok"))
    if last_ok is not None:
        result["last_ok_age_seconds"] = (datetime.now(timezone.utc) - last_ok).total_seconds()
    return result


def read_presence(state_dir: Path) -> dict:
    """Passthrough of presence-context.json (`{at_place, activity, asleep, since, updated_at}`)."""
    data = _read_json(state_dir / "presence-context.json", None)
    if not isinstance(data, dict):
        return {"available": False}
    return {"available": True, **data}


def read_reminders_summary(state_dir: Path) -> dict:
    """Summarized reminders.json: how many are still pending (not fired/suppressed/acked) + the next
    few by due_at."""
    data = _read_json(state_dir / "reminders.json", [])
    if not isinstance(data, list):
        data = []
    pending = [
        r for r in data
        if isinstance(r, dict)
        and r.get("fired_at") is None
        and r.get("suppressed_at") is None
        and r.get("acked_at") is None
    ]
    pending.sort(key=lambda r: r.get("due_at") or "")
    next_few = [
        {
            "id": r.get("id"),
            "text": r.get("text"),
            "due_at": r.get("due_at"),
            "channel": r.get("channel", "telegram"),
        }
        for r in pending[:5]
    ]
    return {"pending_count": len(pending), "total_count": len(data), "next": next_few}


_TOKEN_TOP_LEVEL_KEYS = ("tokens", "total_tokens")
_TOKEN_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def _extract_tokens(rec: dict) -> int | None:
    """Best-effort token count from a metrics.jsonl line. The CURRENT schema (state/README.md) has no
    token field at all — this is forward-compatible plumbing for when/if one lands, and the endpoint
    already labels itself `estimated` and reports `tokens_available` so the UI can tell the
    difference between "zero tokens" and "no token data yet"."""
    for key in _TOKEN_TOP_LEVEL_KEYS:
        val = rec.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return int(val)
    usage = rec.get("usage")
    if isinstance(usage, dict):
        total = 0
        found = False
        for key in _TOKEN_USAGE_KEYS:
            val = usage.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                total += val
                found = True
        if found:
            return int(total)
    return None


def read_usage(state_dir: Path) -> dict:
    """Best-effort plan-usage estimate aggregated from metrics.jsonl: turns (+ tokens where
    derivable) per day, per model. Always labeled `estimated` — there's no official quota API."""
    lines = _read_lines(state_dir / "metrics.jsonl")
    by_day: dict[str, dict] = {}
    totals = {"turns": 0, "tokens": 0}
    tokens_seen = False

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = _parse_iso(rec.get("ts"))
        day = ts.date().isoformat() if ts else "unknown"
        model = rec.get("model") or "unknown"
        bucket = by_day.setdefault(day, {"turns": 0, "tokens": 0, "by_model": {}})
        bucket["turns"] += 1
        bucket["by_model"][model] = bucket["by_model"].get(model, 0) + 1
        totals["turns"] += 1
        tokens = _extract_tokens(rec)
        if tokens is not None:
            tokens_seen = True
            bucket["tokens"] += tokens
            totals["tokens"] += tokens

    days = [{"date": day, **bucket} for day, bucket in sorted(by_day.items())]
    return {
        "estimated": True,
        "tokens_available": tokens_seen,
        "days": days,
        "totals": totals,
    }


def read_router_stats(state_dir: Path, limit: int = 20) -> dict:
    """Tolerant summary of `state/router-log.jsonl` (v3, cockpit-spec.md "Model dials & Fable
    delegation") — the front-door Router advisor's two arms share this one log, distinguished by
    `arm` ("triage" = the pre-existing trivial/escalate shadow classifier; "fable" = the v3 arm gated
    on the live max-routable ceiling; older rows written before the `arm` field existed are bucketed
    under "triage", their only possible origin at the time).

    Returns ``{"counts": {arm: {verdict: n, ...}, ...}, "recent": [...], "total": N}`` — `recent` is the
    last `limit` valid rows, NEWEST FIRST, with `text_preview` re-truncated defensively (the writer
    already caps it at 80 chars, but this reader doesn't trust that boundary to hold forever)."""
    limit = max(1, min(int(limit or 20), 200))
    counts: dict[str, dict[str, int]] = {}
    entries: list[dict] = []
    for line in _read_lines(state_dir / "router-log.jsonl"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        arm = rec.get("arm") if isinstance(rec.get("arm"), str) else "triage"
        verdict = rec.get("verdict") if isinstance(rec.get("verdict"), str) else "unknown"
        bucket = counts.setdefault(arm, {})
        bucket[verdict] = bucket.get(verdict, 0) + 1
        entries.append({
            "ts": rec.get("ts"),
            "channel": rec.get("channel"),
            "arm": arm,
            "verdict": verdict,
            "category": rec.get("category"),
            "confidence": rec.get("confidence"),
            "reason": rec.get("reason"),
            "text_preview": (rec.get("text_preview") or "")[:80],
        })
    recent = list(reversed(entries[-limit:]))
    return {"counts": counts, "recent": recent, "total": len(entries)}


def read_status(state_dir: Path) -> dict:
    """Warm-session status derived from the registry's `daemon` entry — a placeholder until the
    daemon pipe (v2) lands live turn-in-flight / model / queue-depth data."""
    sessions = read_sessions(state_dir)["sessions"]
    daemon = next((s for s in sessions if s.get("source") == "daemon"), None)
    if daemon is None:
        return {
            "available": False,
            "note": "no daemon session-registry entry found; placeholder until the daemon pipe (v2)",
        }
    return {
        "available": True,
        "phase": daemon.get("phase"),
        "working_on": daemon.get("working_on"),
        "last_seen": daemon.get("last_seen"),
        "live": daemon.get("live"),
        "note": "derived from the session registry; placeholder until the daemon pipe (v2)",
    }
