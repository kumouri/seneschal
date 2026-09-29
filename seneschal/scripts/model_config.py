#!/usr/bin/env python3
"""Two-dial model configuration — cockpit-spec.md "Model dials & Fable delegation" (v3). Stdlib only.

The owner picks the **warm model** and the **max routable model** SEPARATELY, in the cockpit:

  * ``warm_model``         — the model the resident warm session runs. presence.py reads this at
    warm-session SPAWN time (it wins over the ``--model`` CLI flag, which becomes the fallback/default);
    an already-warm session needs the cockpit's "apply now" (a graceful restart) to pick up a change.
  * ``max_routable_model`` — the hard ceiling on every Fable delegation, by any trigger (the router's
    fable arm, the warm session's own judgment, or a force-route). presence.py re-reads this LIVE, per
    inbound turn — cheap and tolerant, so a live poll is fine.

**A third field, ``backend``** (``"claude-cli"`` | ``"codex-cli"``), names WHICH rank table
``warm_model``/``max_routable_model`` are checked against. Every model-facing function below
(``canonical``/``rank_of``/``admits_fable``/``validate_pair``) takes an optional ``backend`` parameter
defaulting to ``DEFAULT_BACKEND`` (``"claude-cli"``) — so every caller written before this axis existed
keeps behaving exactly as it did; only a caller that reads or sets a non-default backend needs to know
the parameter exists at all. This module only carries the dial VALUES: the pluggable-backend runtime
that would actually run a codex-cli warm session is a separate layer, and until it is present the
daemon keeps running claude-cli whatever this field says.

``state/model-config.json`` (gitignored; seed ``model-config.example.json``):
    {"backend": "claude-cli", "warm_model": "claude-opus-4-8", "max_routable_model": "claude-fable-5",
     "updated_at": "..."}

A small explicit capability rank per backend — for claude-cli, ``haiku < sonnet < opus-4.8 < opus-5 <
opus-5.5 < fable-5 < fable-5.1``; for codex-cli, see ``RANK["codex-cli"]``'s own comment — both
validates a pair (a warm model may never outrank its own ceiling, WITHIN one backend) and answers "does
the ceiling admit Fable at all" (the fable arm and every delegation trigger hinge on that one question,
and Fable is a Claude-family concept — ``admits_fable`` is unconditionally False for any backend other
than claude-cli, by construction, not by an empty rank list happening to sort that way). On claude-cli
the threshold stays pinned to ``claude-fable-5``, so ``claude-opus-5-5`` sits below it and
``claude-fable-5-1`` above it, both correctly.
``cockpit/server/model_config.py`` duplicates this table rather than importing it (cockpit-spec.md
ruling 3 — the cockpit is its own dependency world; ``cockpit/server/test_parity.py`` is the CI
tripwire); keep the two in sync by hand if the rank/alias list or the backend list changes.

Reads are deliberately tolerant (missing/corrupt file -> every field None/default, never raises) since
this sits on hot-ish paths (every inbound turn, `fable_delegate.py`'s own ceiling check, the cockpit's
GET). Writes (``save``) are strict — an unrecognized id, an unrecognized backend, or an incoherent pair
(warm above the ceiling, within the resolved backend) raises ``ValueError``, which callers turn into a
clear CLI message or an HTTP 400. **``save`` PRESERVES the currently-stored backend when its own
``backend`` argument is omitted** rather than defaulting to claude-cli — an older caller that doesn't
know this axis exists must never silently reset it (the same silent-downgrade failure mode the
cockpit's served model list exists to prevent for the model dials themselves).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

CONFIG_FILE = "model-config.json"

DEFAULT_BACKEND = "claude-cli"
BACKENDS = ("claude-cli", "codex-cli")

# Full canonical ids, ranked low -> high capability/cost, PER BACKEND. Extend a list here — insert at
# the correct rank position (a new tier is not always the top of the tier) — if a new tier ships;
# everything below keys off these, so nothing else needs to change.
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
    # Codex CLI's own model tiers, as its local models cache lists them. OpenAI publishes no explicit
    # numeric capability tier there, only a one-line description per model, so this order is a
    # best-effort reading of those descriptions (`gpt-5.5`: previous generation; `gpt-5.6-luna`: fast
    # and affordable; `gpt-5.6-sol`: an everyday agentic workhorse; `gpt-5.6-terra`: balanced everyday
    # work; `gpt-6-astra`: the most capable, for complex work) — NOT a confirmed benchmark ranking.
    # Re-derive this list (and this comment) if Codex ever ships an explicit tier field, or on any
    # Codex upgrade that changes the cache's model set. Two models the cache also lists are
    # DELIBERATELY EXCLUDED from this general-purpose warm-session picker: `gpt-reserve` (an
    # ambiguously-named "reserve capacity" tier, not clearly a capability rung) and
    # `codex-auto-review` (a specialized approval-review model, not a general chat model at all).
    "codex-cli": ["gpt-5.5", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra"],
}
_RANK_INDEX = {backend: {model_id: i for i, model_id in enumerate(models)} for backend, models in RANK.items()}

# Short aliases someone might type (in the cockpit form, a CLI flag, etc.) plus one known full-id variant
# already in use elsewhere in the repo (run-presence.cmd's --watch-model) — all normalize onto RANK.
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

# The Fable tier per backend — None means "this backend has no such concept", and `admits_fable`
# refuses cleanly (returns False) rather than comparing against a rank that doesn't exist. Fable
# delegation (fable_delegate.py) is a Claude-family mechanism by construction: it shells
# `claude -p --model <fable id>`, which has no meaning at all when the live warm backend isn't
# claude-cli, so this is correct even before anyone asks "which codex model is Fable-tier" — there
# isn't one.
_FABLE_TIER = {"claude-cli": "claude-fable-5", "codex-cli": None}


def _norm_backend(backend) -> str:
    return backend if isinstance(backend, str) and backend in BACKENDS else DEFAULT_BACKEND


def canonical(model_id, backend: str = DEFAULT_BACKEND) -> str | None:
    """Normalize a full canonical id or a short alias to its RANK entry for `backend`; None if
    unrecognized (including a recognized id from a DIFFERENT backend's table — ids don't cross)."""
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    backend = _norm_backend(backend)
    m = model_id.strip()
    if m in _RANK_INDEX.get(backend, {}):
        return m
    return ALIASES.get(backend, {}).get(m.lower())


def rank_of(model_id, backend: str = DEFAULT_BACKEND) -> int | None:
    """0-based capability rank within `backend`'s table, or None if `model_id` doesn't resolve."""
    backend = _norm_backend(backend)
    c = canonical(model_id, backend)
    return _RANK_INDEX.get(backend, {}).get(c) if c is not None else None


def admits_fable(model_id, backend: str = DEFAULT_BACKEND) -> bool:
    """True when `model_id` (read as the live max-routable ceiling, on `backend`) is Fable-tier or
    above — the hard gate cockpit-spec.md ruling 4 hangs everything off: 'no Fable call ever happens,
    by any trigger' when this is False, and the fable arm 'doesn't even run' — callers check this
    BEFORE spending an Ollama call or a `claude -p` subprocess, not just before acting on the result.

    Unconditionally False for any backend with no Fable tier at all (`_FABLE_TIER[backend] is None`) —
    today that's every backend except claude-cli, by construction: Fable delegation has no defined
    meaning when the warm backend isn't Claude."""
    backend = _norm_backend(backend)
    tier = _FABLE_TIER.get(backend)
    if tier is None:
        return False
    r = rank_of(model_id, backend)
    return r is not None and r >= _RANK_INDEX[backend][tier]


def validate_pair(warm_model, max_routable_model, backend: str = DEFAULT_BACKEND) -> tuple[bool, str | None]:
    """Reject an incoherent pair: either id unrecognized (for `backend`), or the warm model outranks
    the ceiling. Returns (ok, error_message) — error_message is None iff ok."""
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


def config_path(state_dir) -> str:
    return os.path.join(state_dir, CONFIG_FILE)


def load(state_dir) -> dict:
    """Tolerant load: a missing file, corrupt JSON, or a non-dict/non-string field all fail OPEN to
    None for that field (DEFAULT_BACKEND for `backend`) — never raises. Every caller (presence.py's
    per-spawn/per-turn reads, the router's fable-arm gate, fable_delegate.py's own ceiling check, the
    cockpit backend's GET) is on a path that must survive an absent or half-written file."""
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


def save(state_dir, warm_model, max_routable_model, backend: str | None = None) -> dict:
    """Validate then atomically write the triple, normalized to canonical ids. Raises ValueError on an
    unrecognized id, an unrecognized backend, or an incoherent pair — writes are the one place this
    config is enforced strictly; reads (`load`) stay tolerant. Returns the written dict.

    `backend=None` (the default) PRESERVES whatever backend is already on disk (DEFAULT_BACKEND on a
    fresh store) rather than resetting it — see the module docstring's silent-downgrade note. Pass a
    backend explicitly to actually switch it."""
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
