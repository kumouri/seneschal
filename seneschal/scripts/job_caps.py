#!/usr/bin/env python3
"""The VALUES of `jobs.py`'s two concurrency caps — the owner's to set, from the cockpit or the CLI.

Spec: `../docs/background-jobs-spec.md` §3.12 "The values are the owner's to set".

**What changed and what did not.** The two caps stay DISTINCT kinds of limit — the soft one is the
owner's interactive headroom per warm session and yields to `jobs.py start --over-soft-cap`; the hard one is
absolute across every session and has no per-job override flag. What moved is who owns the NUMBERS:
they were module constants (3 / 5), and they are now a small state file the owner edits through the
cockpit Jobs panel or `jobs.py caps set`. A cap the owner can set is still a cap nothing can talk its
way past.

**The store.** `state/job-caps.json`, gitignored:
`{"soft_per_session": 3, "hard_global": 5, "updated_at": "...Z", "updated_by": "cockpit"|"cli"}`.
Written ONLY through `save()` — `stateio.write_json_atomic`, never an in-place write (the
`check_state_writes.py --enforce` rule, and the four `state/` losses behind it).

**Read fresh at every cap check, and fail to the defaults.** `load()` never raises: a missing file is
the ordinary case (nobody has changed anything) and reads as the defaults silently; an unreadable,
corrupt or INVALID file also reads as the defaults, and says so ONCE per process on stderr, naming the
file and the fix. Falling back rather than refusing every start is deliberate: a hand-mangled cap file
must not stop all delegated work, and the defaults are exactly the caps that ran for a month before
this file existed. It is never "no cap" — there is no path from a bad file to an unbounded fleet.

**Validate, never clamp.** `validate()` rejects rather than adjusts: integers only (a JSON `true` is
an int to Python and is refused by name), soft >= 1, hard >= soft, hard <= `HARD_CEILING`. A silently
clamped 50 that reads back as 12 is a setting that lies about what it did. The ceiling is a sanity
bound on a typo, not a policy: a dozen concurrent agent jobs is already far past what a couple of
subscriptions plus one machine's own load can carry.

**Lowering a cap never cancels anything.** It only changes what the NEXT `start` is refused at; jobs
already in flight finish normally. Same posture as the refusal at the hard cap: nothing here ever
kills work — that is `jobs.py cancel`, a decision.

**Imported from two dependency worlds.** `jobs.py` imports it normally. The cockpit backend
(`cockpit/server/caps_control.py`) loads it BY FILE PATH, because `seneschal/scripts/` on `sys.path`
would collide with the cockpit's own generically-named modules — so this module must stay stdlib-only
and must not import any sibling at module scope except `stateio`, which it reaches by path when the
plain import is unavailable.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import stateio  # noqa: F401 — the one atomic writer for state/
except ImportError:  # loaded by file path from the cockpit (see the module docstring)
    _spec = importlib.util.spec_from_file_location("seneschal_scripts_stateio",
                                                   os.path.join(_HERE, "stateio.py"))
    stateio = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(stateio)

CAPS_FILE = "job-caps.json"
SCHEMA = "seneschal.job-caps/1"

DEFAULT_SOFT = 3   # agent jobs per warm session — the owner's interactive headroom
DEFAULT_HARD = 5   # agent jobs across every session
SOFT_MIN = 1
HARD_CEILING = 12  # a typo guard, not a policy — see the module docstring

UPDATERS = ("cockpit", "cli")

_warned: set = set()


class CapsError(ValueError):
    """A cap pair that fails validation. The message is one line naming what is wrong."""


def caps_path(state_dir: str) -> str:
    return os.path.join(state_dir, CAPS_FILE)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate(soft, hard) -> tuple:
    """`(soft, hard)` as ints, or raise `CapsError`. Rejects; never clamps."""
    for name, value in (("soft", soft), ("hard", hard)):
        if not _is_int(value):
            raise CapsError(f"the {name} cap must be a whole number, got {value!r}")
    if soft < SOFT_MIN:
        raise CapsError(f"the soft cap must be at least {SOFT_MIN}, got {soft}")
    if hard < soft:
        raise CapsError(f"the hard cap ({hard}) must be at least the soft cap ({soft}) — the soft "
                        f"cap is one session's share of the hard one")
    if hard > HARD_CEILING:
        raise CapsError(f"the hard cap must be at most {HARD_CEILING}, got {hard}")
    return soft, hard


def defaults() -> dict:
    return {"soft_per_session": DEFAULT_SOFT, "hard_global": DEFAULT_HARD,
            "updated_at": None, "updated_by": None, "source": "default"}


def _warn_once(path: str, reason: str, stream=None) -> None:
    key = (path, reason)
    if key in _warned:
        return
    _warned.add(key)
    print(f"job caps: {path} {reason} — using the defaults ({DEFAULT_SOFT} soft / "
          f"{DEFAULT_HARD} hard). Fix it with `jobs.py caps set --soft N --hard M` or the "
          f"cockpit's Jobs panel.", file=stream or sys.stderr)


def load(state_dir: str, *, stream=None) -> dict:
    """The caps in force: `{"soft_per_session", "hard_global", "updated_at", "updated_by",
    "source"}` where `source` is `"file"` or `"default"`, plus `"error"` when a file was present but
    unusable. Never raises."""
    path = caps_path(state_dir)
    if not os.path.exists(path):
        return defaults()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        reason = f"is unreadable ({e.__class__.__name__})"
        _warn_once(path, reason, stream)
        return {**defaults(), "error": reason}
    if not isinstance(raw, dict):
        reason = "is not a JSON object"
        _warn_once(path, reason, stream)
        return {**defaults(), "error": reason}
    try:
        soft, hard = validate(raw.get("soft_per_session"), raw.get("hard_global"))
    except CapsError as e:
        reason = f"is invalid ({e})"
        _warn_once(path, reason, stream)
        return {**defaults(), "error": reason}
    return {"soft_per_session": soft, "hard_global": hard,
            "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
            "updated_by": raw.get("updated_by") if isinstance(raw.get("updated_by"), str) else None,
            "source": "file"}


def save(state_dir: str, soft, hard, *, updated_by: str, now: datetime | None = None) -> dict:
    """Validate, then write atomically. Raises `CapsError` on a bad pair (and writes nothing).
    Returns what `load()` will now read."""
    soft, hard = validate(soft, hard)
    if updated_by not in UPDATERS:
        raise CapsError(f"updated_by must be one of {', '.join(UPDATERS)}, got {updated_by!r}")
    now = now or datetime.now(timezone.utc)
    data = {"schema": SCHEMA, "soft_per_session": soft, "hard_global": hard,
            "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "updated_by": updated_by}
    stateio.write_json_atomic(caps_path(state_dir), data)
    return {**{k: data[k] for k in ("soft_per_session", "hard_global", "updated_at", "updated_by")},
            "source": "file"}


def limits() -> dict:
    """The validation bounds, served to the cockpit so the browser never keeps its own copy."""
    return {"soft_min": SOFT_MIN, "hard_ceiling": HARD_CEILING,
            "default_soft": DEFAULT_SOFT, "default_hard": DEFAULT_HARD}


def main(argv: list | None = None) -> int:
    """Debug read only — the CLI that sets the caps is `jobs.py caps set`, which also prints the live
    in-flight counts beside them."""
    p = argparse.ArgumentParser(description="show the job concurrency caps in force")
    p.add_argument("--state-dir", default=os.path.normpath(os.path.join(_HERE, "..", "state")))
    args = p.parse_args(argv)
    print(json.dumps(load(args.state_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
