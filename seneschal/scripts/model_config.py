#!/usr/bin/env python3
"""Two-dial model configuration — cockpit-spec.md "Model dials & Fable delegation" (v3). Stdlib only.

The owner picks the **warm model** and the **max routable model** SEPARATELY, in the cockpit:

  * ``warm_model``         — the model the resident warm session runs. presence.py reads this at
    warm-session SPAWN time (it wins over the ``--model`` CLI flag, which becomes the fallback/default);
    an already-warm session needs the cockpit's "apply now" (a graceful restart) to pick up a change.
  * ``max_routable_model`` — the hard ceiling on every Fable delegation, by any trigger (the router's
    fable arm, the warm session's own judgment, or a force-route). presence.py re-reads this LIVE, per
    inbound turn — cheap and tolerant, so a live poll is fine.

``state/model-config.json`` (gitignored; seed ``model-config.example.json``):
    {"warm_model": "claude-opus-4-8", "max_routable_model": "claude-fable-5", "updated_at": "..."}

A small explicit capability rank — ``haiku < sonnet < opus < fable`` — both validates a pair (a warm
model may never outrank its own ceiling) and answers "does the ceiling admit Fable at all" (the fable
arm and every delegation trigger hinge on that one question). ``cockpit/server/model_config.py``
duplicates this table rather than importing it (cockpit-spec.md ruling 3 — the cockpit is its own
dependency world); keep the two in sync by hand if the rank/alias list changes.

Reads are deliberately tolerant (missing/corrupt file -> both dial fields None, never raises) since this
sits on hot-ish paths (every inbound turn, `fable_delegate.py`'s own ceiling check, the cockpit's GET).
Writes (``save``) are strict — an unrecognized id or an incoherent pair (warm above the ceiling) raises
``ValueError``, which callers turn into a clear CLI message or an HTTP 400.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

CONFIG_FILE = "model-config.json"

# Full canonical ids, ranked low -> high capability/cost. Extend here (append to the end) if a new tier
# ships; everything below keys off this one list, so nothing else needs to change.
RANK = ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8", "claude-fable-5"]
_RANK_INDEX = {model_id: i for i, model_id in enumerate(RANK)}

# Short aliases someone might type (in the cockpit form, a CLI flag, etc.) plus one known full-id variant
# already in use elsewhere in the repo (run-presence.cmd's --watch-model) — all normalize onto RANK.
ALIASES = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
    "fable": "claude-fable-5",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
}


def canonical(model_id) -> str | None:
    """Normalize a full canonical id or a short alias to its RANK entry; None if unrecognized."""
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    m = model_id.strip()
    if m in _RANK_INDEX:
        return m
    return ALIASES.get(m.lower())


def rank_of(model_id) -> int | None:
    """0-based capability rank, or None if `model_id` doesn't resolve to a known model."""
    c = canonical(model_id)
    return _RANK_INDEX.get(c) if c is not None else None


def admits_fable(model_id) -> bool:
    """True when `model_id` (read as the live max-routable ceiling) is Fable-tier or above — the hard
    gate cockpit-spec.md ruling 4 hangs everything off: 'no Fable call ever happens, by any trigger'
    when this is False, and the fable arm 'doesn't even run' — callers check this BEFORE spending an
    Ollama call or a `claude -p` subprocess, not just before acting on the result."""
    r = rank_of(model_id)
    return r is not None and r >= _RANK_INDEX["claude-fable-5"]


def validate_pair(warm_model, max_routable_model) -> tuple[bool, str | None]:
    """Reject an incoherent pair: either id unrecognized, or the warm model outranks the ceiling.
    Returns (ok, error_message) — error_message is None iff ok."""
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


def config_path(state_dir) -> str:
    return os.path.join(state_dir, CONFIG_FILE)


def load(state_dir) -> dict:
    """Tolerant load: a missing file, corrupt JSON, or a non-dict/non-string field all fail OPEN to
    None for that field — never raises. Every caller (presence.py's per-spawn/per-turn reads, the
    router's fable-arm gate, fable_delegate.py's own ceiling check, the cockpit backend's GET) is on a
    path that must survive an absent or half-written file."""
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


def save(state_dir, warm_model, max_routable_model) -> dict:
    """Validate then atomically write the pair, normalized to canonical ids. Raises ValueError on an
    unrecognized id or an incoherent pair — writes are the one place this config is enforced strictly;
    reads (`load`) stay tolerant. Returns the written dict."""
    ok, err = validate_pair(warm_model, max_routable_model)
    if not ok:
        raise ValueError(err)
    data = {
        "warm_model": canonical(warm_model),
        "max_routable_model": canonical(max_routable_model),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    os.makedirs(state_dir, exist_ok=True)
    path = config_path(state_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    return data


def _main(argv: list[str]) -> int:
    """Smoke test: print the current config (or a fresh path's tolerant defaults) as JSON."""
    here = os.path.dirname(os.path.abspath(__file__))
    state_dir = argv[1] if len(argv) > 1 else os.path.normpath(os.path.join(here, "..", "state"))
    print(json.dumps(load(state_dir), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
