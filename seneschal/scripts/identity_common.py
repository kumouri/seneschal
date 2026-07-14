#!/usr/bin/env python3
"""Structured identity config — who the assistant is and who it works for. Stdlib only.

Reads ``persona/identity.json`` (gitignored; seeded from ``persona/identity.example.json``).
The ``/setup-persona`` wizard and the store onboarding WRITE that file; this module is how
code READS it. Consumers today:
  * ``presence.py`` — renders the warm session's grounding prompt and the scheduled SLOTS
    prompts from the assistant/owner names + the owner's timezone label at daemon startup.
  * ``tz_common`` — ``owner.timezone`` (an IANA string) is the source of truth for date/label
    math (local_now/local_today/offset_minutes, machine-local fallback); slot fire-times stay
    machine-local wall clock.
Skills read ``persona/persona.md`` instead (the wizard generates both from one interview so
they never disagree — see ``persona/README.md``).

Every field is nullable and the file is OPTIONAL: :func:`load_identity` NEVER raises. A
missing file (the normal fresh-install state) silently yields :data:`DEFAULTS`; any other
problem (corrupt JSON, wrong shape, unreadable file) yields :data:`DEFAULTS` with one
warning line to stderr.
"""
from __future__ import annotations

import copy
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
IDENTITY_PATH = os.path.join(REPO_ROOT, "persona", "identity.json")

# Mirrors persona/identity.example.json: assistant.pronouns defaults to "they/them",
# everything else is null. owner.timezone null = machine-local semantics (slot times and
# date math run on the machine's wall clock; see presence.py's tz-mismatch warning).
DEFAULTS = {
    "schema": 1,
    "assistant": {
        "name": None,
        "nameSpoken": None,
        "pronouns": "they/them",
        "email": None,
        "signoff": None,
        "ttsProvider": None,
        "ttsVoiceId": None,
    },
    "owner": {
        "name": None,
        "nameSpoken": None,
        "pronouns": None,
        "email": None,
        "timezone": None,
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    """Merge ``over`` onto ``base`` recursively: nested dicts merge key-by-key, anything
    else in ``over`` wins outright. Keys unknown to ``base`` are preserved (a file written
    by a newer schema survives an older reader), and keys missing from ``over`` keep their
    defaults. Neither input is mutated."""
    out = {k: copy.deepcopy(v) for k, v in base.items()}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_identity(path: str | None = None) -> dict:
    """Load persona/identity.json deep-merged over :data:`DEFAULTS`. Never raises.

    * Missing file → a copy of DEFAULTS, silently (that's the normal fresh-install state).
    * Corrupt JSON / not a JSON object / unreadable → a copy of DEFAULTS, with ONE warning
      line to stderr (the daemon must boot on a mangled file, but the owner should hear).
    * Valid file → DEFAULTS overlaid with its values; unknown keys are preserved.
    """
    path = path or IDENTITY_PATH
    try:
        if not os.path.exists(path):
            return copy.deepcopy(DEFAULTS)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        return _deep_merge(DEFAULTS, data)
    except Exception as e:  # noqa: BLE001 — identity must never take a consumer down
        print(f"! identity: could not read {path} ({e}) — using defaults", file=sys.stderr)
        return copy.deepcopy(DEFAULTS)


def get_str(identity: dict, section: str, key: str) -> str | None:
    """Raw accessor: the configured non-empty string at ``identity[section][key]``, else
    None. Defensive about shape (a hand-edited file may carry wrong types) so callers can
    supply their own fallback phrasing — the convenience accessors below are the usual way."""
    sec = identity.get(section) if isinstance(identity, dict) else None
    val = sec.get(key) if isinstance(sec, dict) else None
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def assistant_name(identity: dict) -> str:
    """The assistant's configured name, or the generic phrase "the assistant"."""
    return get_str(identity, "assistant", "name") or "the assistant"


def owner_name(identity: dict) -> str:
    """The owner's configured name, or the generic phrase "the owner"."""
    return get_str(identity, "owner", "name") or "the owner"


def owner_tz_label(identity: dict) -> str:
    """The owner's configured IANA timezone string, or "the machine's local timezone"."""
    return get_str(identity, "owner", "timezone") or "the machine's local timezone"
