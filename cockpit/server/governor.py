"""The cockpit's copy of Oikonomos's SCHEMA + config load/save/rollups (cockpit-spec.md "Oikonomos —
the budget governor", v3.5). Deliberately duplicated from `seneschal/scripts/governor.py` rather than
imported — the cockpit is its own dependency world (cockpit-spec.md ruling 3), same posture as
`model_config.py`. Keep the two in sync by hand if SCHEMA or the ledger/rollup shape changes.

Reads/writes the SAME `state/governor-config.json` + `state/governor-ledger.jsonl` files the daemon
uses (via `get_state_dir()`), so a change made through the cockpit's PUT is picked up by the daemon
exactly like a hand-edit or a `governor.save()` call on that side. This module is the cockpit's READ +
VALIDATED-WRITE half only — it has no notion of the daemon-only concerns (the fable_oneshot rail gate,
the in-flight concurrency counter, alert dedupe/send): those stay in `seneschal/scripts/governor.py`, the
one place that actually enforces or alerts on anything. The cockpit only displays state and edits config.
"""
from __future__ import annotations

import copy
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

# Owner-timezone plumbing, mirroring seneschal/scripts/governor.py's guarded import of the sibling
# tz_common (configured identity zone → machine-local fallback). From the cockpit package tz_common
# is normally NOT importable (the scripts dir isn't on sys.path), so this degrades to the
# machine-local clock — the same fallback the daemon side uses on a bare interpreter, and the right
# answer on the owner's own machine.
try:
    import tz_common as _tz_common  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — the normal cockpit runtime state
    _tz_common = None

CONFIG_FILE = "governor-config.json"
LEDGER_FILE = "governor-ledger.jsonl"

# Mirrors seneschal/scripts/governor.py's SCHEMA exactly.
SCHEMA: dict = {
    "max_output_tokens_per_turn": {
        "type": "int", "label": "Max output tokens per turn", "unit": "tokens",
        "kind": "advisory", "default": 8000, "min": 256, "max": 64000,
    },
    "reasoning_effort_by_mode": {
        "type": "dict_enum", "label": "Reasoning effort tier (per mode)", "unit": "tier",
        "kind": "advisory", "options": ["low", "medium", "high"],
        "default": {
            "Chat": "medium", "Brief": "medium", "Wrap": "medium", "Triage": "medium",
            "Ask": "medium", "Reminders": "low", "Forge": "medium", "Archive": "low",
            "Watch": "low", "Dream": "high",
        },
    },
    "turn_checkpoint_n": {
        "type": "int", "label": "Turn checkpoints (autonomous turns before pausing to ask)",
        "unit": "turns", "kind": "advisory", "default": 8, "min": 1, "max": 100,
    },
    "max_turns_per_conversation": {
        "type": "int", "label": "Total-turn cap per conversation", "unit": "turns",
        "kind": "advisory", "default": 40, "min": 1, "max": 1000,
        "alert_at_pct": 80, "hard_stop": False,
    },
    "daily_token_budget_by_model": {
        "type": "dict_int", "label": "Daily token budget (per model)", "unit": "tokens/day",
        "kind": "rail", "min": 0, "max": 50_000_000, "alert_at_pct": 80, "hard_stop": False,
        "default": {
            "claude-haiku-4-5": 2_000_000, "claude-sonnet-5": 1_000_000,
            "claude-opus-4-8": 400_000, "claude-fable-5": 150_000,
        },
    },
    "weekly_token_budget_by_model": {
        "type": "dict_int", "label": "Weekly token budget (per model)", "unit": "tokens/week",
        "kind": "rail", "min": 0, "max": 200_000_000, "alert_at_pct": 80, "hard_stop": False,
        "default": {
            "claude-haiku-4-5": 10_000_000, "claude-sonnet-5": 5_000_000,
            "claude-opus-4-8": 2_000_000, "claude-fable-5": 750_000,
        },
    },
    "fable_oneshots_per_day": {
        "type": "int", "label": "Fable one-shots per day", "unit": "one-shots/day",
        "kind": "rail", "default": 5, "min": 0, "max": 200,
        "alert_at_pct": 80, "hard_stop": True,
    },
    "fable_oneshots_per_conversation": {
        "type": "int", "label": "Max Fable one-shots per conversation", "unit": "one-shots",
        "kind": "rail", "default": 2, "min": 0, "max": 50,
        "alert_at_pct": 100, "hard_stop": True,
    },
    "fable_concurrency_max": {
        "type": "int", "label": "Delegation concurrency", "unit": "concurrent one-shots",
        "kind": "rail", "default": 1, "min": 1, "max": 10,
        "alert_at_pct": 100, "hard_stop": True,
    },
    "context_fill_winddown_pct": {
        "type": "int", "label": "Context-fill wind-down threshold", "unit": "% full",
        "kind": "advisory", "default": 80, "min": 10, "max": 100,
    },
    "proactive_push_rate_per_hour": {
        "type": "int", "label": "Proactive-push rate cap", "unit": "nudges/hour",
        "kind": "advisory", "default": 3, "min": 0, "max": 60,
        "alert_at_pct": 100, "hard_stop": False,
    },
}


def _validate(key: str, spec: dict, value) -> tuple[bool, str | None]:
    t = spec.get("type")
    if t == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return False, f"{key}: expected an integer, got {value!r}"
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and value < lo:
            return False, f"{key}: {value} is below the minimum ({lo})"
        if hi is not None and value > hi:
            return False, f"{key}: {value} is above the maximum ({hi})"
        return True, None
    if t == "dict_int":
        if not isinstance(value, dict):
            return False, f"{key}: expected an object mapping model -> integer"
        lo, hi = spec.get("min"), spec.get("max")
        for sub_key, sub_val in value.items():
            if not isinstance(sub_val, int) or isinstance(sub_val, bool):
                return False, f"{key}.{sub_key}: expected an integer, got {sub_val!r}"
            if lo is not None and sub_val < lo:
                return False, f"{key}.{sub_key}: {sub_val} is below the minimum ({lo})"
            if hi is not None and sub_val > hi:
                return False, f"{key}.{sub_key}: {sub_val} is above the maximum ({hi})"
        return True, None
    if t == "dict_enum":
        options = spec.get("options") or []
        if not isinstance(value, dict):
            return False, f"{key}: expected an object mapping mode -> tier"
        for sub_key, sub_val in value.items():
            if sub_val not in options:
                return False, f"{key}.{sub_key}: {sub_val!r} is not one of {options}"
        return True, None
    return False, f"{key}: unknown schema type {t!r}"


def config_path(state_dir: Path) -> Path:
    return Path(state_dir) / CONFIG_FILE


def ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / LEDGER_FILE


def _raw_load(state_dir: Path) -> dict:
    try:
        with open(config_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        data = None
    return data if isinstance(data, dict) else {}


def load(state_dir: Path) -> dict:
    """Tolerant load, same contract as the daemon-side module: every SCHEMA knob resolves to its
    stored value if valid, else its default. Never raises."""
    raw = _raw_load(state_dir)
    out: dict = {}
    for key, spec in SCHEMA.items():
        if key in raw:
            ok, _ = _validate(key, spec, raw[key])
            if ok:
                out[key] = raw[key]
                continue
        out[key] = copy.deepcopy(spec["default"])
    return out


def save(state_dir: Path, updates: dict) -> dict:
    """Validate every key in `updates` against SCHEMA, then atomically merge + write. Raises
    ValueError (joined messages) on ANY invalid key/value — the route handler turns that into an HTTP
    400. Keys already on disk that `updates` doesn't touch are preserved verbatim (forward compat)."""
    raw = _raw_load(state_dir)
    errors: list[str] = []
    for key, value in updates.items():
        spec = SCHEMA.get(key)
        if spec is None:
            errors.append(f"unknown knob {key!r}")
            continue
        ok, err = _validate(key, spec, value)
        if not ok:
            errors.append(err)
            continue
        raw[key] = value
    if errors:
        raise ValueError("; ".join(errors))
    raw["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    path = config_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2)
    os.replace(tmp, path)
    return load(state_dir)


# ---------------------------------------------------------------------- owner-local day boundary
# Mirrors seneschal/scripts/governor.py's helpers exactly (see that module's docstring for the
# rationale — duplicated rather than imported; the cockpit is its own dependency world). Rule 5:
# date/day-boundary logic runs in the OWNER's timezone, never UTC; without tz_common it degrades to
# the machine-local clock.

def _local_today() -> date:
    """The owner's current local calendar date (machine-local when tz_common is unavailable)."""
    if _tz_common is not None:
        return _tz_common.local_now().date()
    return datetime.now().astimezone().date()


def _to_local_date(dt: datetime) -> date:
    """Convert an aware (or naive-assumed-UTC) instant to the owner's local calendar date — the
    correct after-midnight-is-still-yesterday math the house rule requires."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if _tz_common is not None:
        return _tz_common.to_local(dt).date()
    return dt.astimezone().date()


def _week_key(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_ledger(state_dir: Path) -> list[dict]:
    try:
        with open(ledger_path(state_dir), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            rec = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def rollups(state_dir: Path, now: datetime | None = None) -> dict:
    """Day/week spend totals from the ledger, gated on the owner's local calendar-day boundaries —
    read-only mirror of seneschal/scripts/governor.py's `rollups()`. Used by `GET /api/governor-config` to
    show today/this-week spend alongside the config."""
    today_local = _to_local_date(now) if now is not None else _local_today()
    day_key = today_local.isoformat()
    week_key = _week_key(today_local)

    fable_day = fable_week = 0
    tokens_day: dict[str, int] = {}
    tokens_week: dict[str, int] = {}

    for rec in _read_ledger(state_dir):
        ts = _parse_iso(rec.get("ts"))
        if ts is None:
            continue
        rec_date = _to_local_date(ts)
        same_day = rec_date.isoformat() == day_key
        same_week = _week_key(rec_date) == week_key
        if not same_day and not same_week:
            continue
        kind = rec.get("kind")
        if kind == "fable_oneshot":
            if same_day:
                fable_day += 1
            if same_week:
                fable_week += 1
        elif kind == "tokens":
            model = rec.get("model") if isinstance(rec.get("model"), str) else "unknown"
            tok = rec.get("tokens")
            tok = int(tok) if isinstance(tok, (int, float)) and not isinstance(tok, bool) else 0
            if same_day:
                tokens_day[model] = tokens_day.get(model, 0) + tok
            if same_week:
                tokens_week[model] = tokens_week.get(model, 0) + tok

    return {
        "day": day_key, "week": week_key,
        "fable_oneshots": {"day": fable_day, "week": fable_week},
        "tokens_by_model": {"day": tokens_day, "week": tokens_week},
    }
