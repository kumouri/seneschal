"""The cockpit's copy of the model config table (cockpit-spec.md "Model dials & Fable delegation", v3)
— now three dials, `backend` included. Deliberately duplicated from `seneschal/scripts/model_config.py`
rather than imported — the cockpit is its own dependency world (cockpit-spec.md ruling 3), and this
rank/alias table is small enough that a byte-for-byte mirror is cheaper than reaching across the
boundary. Keep the two in sync by hand if the model list, alias list, or backend list changes
(`test_parity.py` is the CI tripwire for a one-sided edit).

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

DEFAULT_BACKEND = "claude-cli"
BACKENDS = ("claude-cli", "codex-cli")

# Mirrors seneschal/scripts/model_config.py's RANK/ALIASES/backend tables exactly — see that file's
# comments for the codex-cli list's provenance (the Codex CLI's local models cache) and the two
# deliberately-excluded codex models (`gpt-reserve`, `codex-auto-review`).
RANK = {
    "claude-cli": [
        "claude-haiku-4-5",
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-fable-5",
        "claude-fable-5-1",
    ],
    "codex-cli": ["gpt-5.5", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra"],
}
_RANK_INDEX = {backend: {model_id: i for i, model_id in enumerate(models)} for backend, models in RANK.items()}

ALIASES = {
    "claude-cli": {
        "haiku": "claude-haiku-4-5",
        "sonnet": "claude-sonnet-5",
        "opus": "claude-opus-4-8",
        "opus-5": "claude-opus-5",
        "opus5": "claude-opus-5",
        "opus-5-5": "claude-opus-5-5",
        "opus5.5": "claude-opus-5-5",
        "opus-5.5": "claude-opus-5-5",
        "opus55": "claude-opus-5-5",
        "fable": "claude-fable-5",
        "fable-5-1": "claude-fable-5-1",
        "fable5.1": "claude-fable-5-1",
        "fable-5.1": "claude-fable-5-1",
        "fable51": "claude-fable-5-1",
        "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    },
    "codex-cli": {
        "astra": "gpt-6-astra",
        "gpt-6": "gpt-6-astra",
        "sol": "gpt-5.6-sol",
        "terra": "gpt-5.6-terra",
        "luna": "gpt-5.6-luna",
    },
}

_FABLE_TIER = {"claude-cli": "claude-fable-5", "codex-cli": None}

# Display labels for the cockpit's dial selects, served over the API so the FRONTEND DOES NOT KEEP ITS
# OWN COPY of the model list. A third, browser-side copy is exactly what drifts: when it lacks an id
# the config genuinely holds, the `<select>` has no matching option and the browser renders the FIRST
# one instead — the panel then shows the wrong model, and a save writes that wrong model back,
# silently downgrading the warm dial.
#
# Ruling 3 sanctions duplicating this table between the daemon and THIS backend (both Python, both
# reviewed together, `test_parity.py` guarding them). It does not sanction a third copy in the
# browser, which is why the labels live here: one list per backend, derived from RANK, so an id can
# never be present in the config and absent from the picker.
#
# Every tier is spelled out with its version ("Opus 4.8", "Opus 5", "Fable 5.1") — never a bare tier
# word, so a click always names the exact tier it sets.
LABELS = {
    "claude-cli": {
        "claude-haiku-4-5": "Haiku 4.5",
        "claude-sonnet-5": "Sonnet 5",
        "claude-opus-4-8": "Opus 4.8",
        "claude-opus-5": "Opus 5",
        "claude-opus-5-5": "Opus 5.5",
        "claude-fable-5": "Fable 5",
        "claude-fable-5-1": "Fable 5.1",
    },
    "codex-cli": {
        "gpt-5.5": "GPT-5.5",
        "gpt-5.6-luna": "GPT-5.6-Luna",
        "gpt-5.6-sol": "GPT-5.6-Sol",
        "gpt-5.6-terra": "GPT-5.6-Terra",
        "gpt-6-astra": "GPT-6-Astra",
    },
}

BACKEND_LABELS = {"claude-cli": "Claude Code CLI", "codex-cli": "Codex CLI"}


def _norm_backend(backend) -> str:
    return backend if isinstance(backend, str) and backend in BACKENDS else DEFAULT_BACKEND


def known_models(backend: str = DEFAULT_BACKEND) -> list:
    """The dial picker's options for one backend, in RANK order (weakest → strongest). Derived FROM
    `RANK`, so a model added there can never go missing from the cockpit's selects — an id with no
    label falls back to the id itself rather than being dropped."""
    backend = _norm_backend(backend)
    labels = LABELS.get(backend, {})
    return [{"id": model_id, "label": labels.get(model_id, model_id)} for model_id in RANK.get(backend, [])]


def known_backends() -> list:
    """Every backend's id/label/model-option-list in one shot — what `GET /api/model-config` serves
    so the frontend's backend selector never needs its own copy of BACKENDS either."""
    return [{"id": b, "label": BACKEND_LABELS.get(b, b), "models": known_models(b)} for b in BACKENDS]


def canonical(model_id, backend: str = DEFAULT_BACKEND) -> str | None:
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    backend = _norm_backend(backend)
    m = model_id.strip()
    if m in _RANK_INDEX.get(backend, {}):
        return m
    return ALIASES.get(backend, {}).get(m.lower())


def rank_of(model_id, backend: str = DEFAULT_BACKEND) -> int | None:
    backend = _norm_backend(backend)
    c = canonical(model_id, backend)
    return _RANK_INDEX.get(backend, {}).get(c) if c is not None else None


def admits_fable(model_id, backend: str = DEFAULT_BACKEND) -> bool:
    backend = _norm_backend(backend)
    tier = _FABLE_TIER.get(backend)
    if tier is None:
        return False
    r = rank_of(model_id, backend)
    return r is not None and r >= _RANK_INDEX[backend][tier]


def validate_pair(warm_model, max_routable_model, backend: str = DEFAULT_BACKEND) -> tuple[bool, str | None]:
    backend = _norm_backend(backend)
    warm_rank = rank_of(warm_model, backend)
    if warm_rank is None:
        return False, f"unrecognized warm_model {warm_model!r} for backend {backend!r}"
    ceiling_rank = rank_of(max_routable_model, backend)
    if ceiling_rank is None:
        return False, f"unrecognized max_routable_model {max_routable_model!r} for backend {backend!r}"
    if warm_rank > ceiling_rank:
        return False, (f"warm_model {canonical(warm_model, backend)!r} outranks max_routable_model "
                       f"{canonical(max_routable_model, backend)!r} — raise the ceiling or lower the "
                       f"warm model")
    return True, None


def config_path(state_dir: Path) -> Path:
    return Path(state_dir) / CONFIG_FILE


def load(state_dir: Path) -> dict:
    """Tolerant load, same contract as the daemon-side module: missing/corrupt file or a non-string
    field -> None for that field (DEFAULT_BACKEND for `backend`), never raises."""
    try:
        with open(config_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        data = None
    if not isinstance(data, dict):
        data = {}
    warm = data.get("warm_model")
    ceiling = data.get("max_routable_model")
    backend = data.get("backend")
    return {
        "backend": backend if backend in BACKENDS else DEFAULT_BACKEND,
        "warm_model": warm if isinstance(warm, str) and warm.strip() else None,
        "max_routable_model": ceiling if isinstance(ceiling, str) and ceiling.strip() else None,
        "updated_at": data.get("updated_at"),
    }


def save(state_dir: Path, warm_model, max_routable_model, backend: str | None = None) -> dict:
    """Validate then atomically write the triple, normalized to canonical ids. Raises ValueError on an
    unrecognized id, an unrecognized backend, or an incoherent pair — the route handler turns that into
    an HTTP 400. `backend=None` PRESERVES whatever is already on disk — see
    seneschal/scripts/model_config.py's docstring for why (an older client that doesn't know the
    backend axis exists must never silently reset it)."""
    if backend is None:
        backend = load(state_dir).get("backend") or DEFAULT_BACKEND
    if backend not in BACKENDS:
        raise ValueError(f"unrecognized backend {backend!r} — must be one of {BACKENDS}")
    ok, err = validate_pair(warm_model, max_routable_model, backend)
    if not ok:
        raise ValueError(err)
    data = {
        "backend": backend,
        "warm_model": canonical(warm_model, backend),
        "max_routable_model": canonical(max_routable_model, backend),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path = config_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    return data
