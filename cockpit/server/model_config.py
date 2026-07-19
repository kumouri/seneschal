"""The cockpit's copy of the two-dial model config table (cockpit-spec.md "Model dials & Fable
delegation", v3). Deliberately duplicated from `seneschal/scripts/model_config.py` rather than imported —
the cockpit is its own dependency world (cockpit-spec.md ruling 3), and this rank/alias table is small
enough that a byte-for-byte mirror is cheaper than reaching across the boundary. Keep the two in sync by
hand if the model list changes.

Reads/writes the SAME `state/model-config.json` file the daemon reads (via `get_state_dir()`), so a
change made through the cockpit's PUT is picked up by the daemon exactly like a hand-edit or a
`model_config.save()` call on that side.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

CONFIG_FILE = "model-config.json"

# Mirrors seneschal/scripts/model_config.py's RANK/ALIASES exactly.
RANK = ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8", "claude-fable-5"]
_RANK_INDEX = {model_id: i for i, model_id in enumerate(RANK)}

ALIASES = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
    "fable": "claude-fable-5",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
}


def canonical(model_id) -> str | None:
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    m = model_id.strip()
    if m in _RANK_INDEX:
        return m
    return ALIASES.get(m.lower())


def rank_of(model_id) -> int | None:
    c = canonical(model_id)
    return _RANK_INDEX.get(c) if c is not None else None


def admits_fable(model_id) -> bool:
    r = rank_of(model_id)
    return r is not None and r >= _RANK_INDEX["claude-fable-5"]


def validate_pair(warm_model, max_routable_model) -> tuple[bool, str | None]:
    warm_rank = rank_of(warm_model)
    if warm_rank is None:
        return False, f"unrecognized warm_model {warm_model!r}"
    ceiling_rank = rank_of(max_routable_model)
    if ceiling_rank is None:
        return False, f"unrecognized max_routable_model {max_routable_model!r}"
    if warm_rank > ceiling_rank:
        return False, (f"warm_model {canonical(warm_model)!r} outranks max_routable_model "
                       f"{canonical(max_routable_model)!r} — raise the ceiling or lower the warm model")
    return True, None


def config_path(state_dir: Path) -> Path:
    return Path(state_dir) / CONFIG_FILE


def load(state_dir: Path) -> dict:
    """Tolerant load, same contract as the daemon-side module: missing/corrupt file or a non-string
    field -> None for that field, never raises."""
    try:
        with open(config_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        data = None
    if not isinstance(data, dict):
        data = {}
    warm = data.get("warm_model")
    ceiling = data.get("max_routable_model")
    return {
        "warm_model": warm if isinstance(warm, str) and warm.strip() else None,
        "max_routable_model": ceiling if isinstance(ceiling, str) and ceiling.strip() else None,
        "updated_at": data.get("updated_at"),
    }


def save(state_dir: Path, warm_model, max_routable_model) -> dict:
    """Validate then atomically write the pair, normalized to canonical ids. Raises ValueError on an
    unrecognized id or an incoherent pair — the route handler turns that into an HTTP 400."""
    ok, err = validate_pair(warm_model, max_routable_model)
    if not ok:
        raise ValueError(err)
    data = {
        "warm_model": canonical(warm_model),
        "max_routable_model": canonical(max_routable_model),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path = config_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    return data
