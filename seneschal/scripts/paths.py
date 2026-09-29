#!/usr/bin/env python3
"""Where things live — `REPO_ROOT`, `SCRIPTS_DIR`, state-dir resolution. Zero intra-repo imports.

A foundation primitive. Many modules in this directory still define their own
`DEFAULT_STATE_DIR`/`STATE_DIR` spelling, and only some of them honour `SENESCHAL_STATE_DIR`; this is
the one definition they can collapse onto as they are touched.

**Precedence, exactly:** `SENESCHAL_STATE_DIR` env var, then an explicit argument, then the default —
in that order. The env var outranking an argument a caller passed is deliberate: it is what lets a
test, a worktree-scoped job, or a dependency world with its own state (the cockpit) redirect every
state read/write on the process without hunting down every call site's own default.
"""
from __future__ import annotations

import os

#: The env var a caller — or a process's whole environment — can set to redirect every state read
#: and write without touching a single call site's default.
ENV_VAR = "SENESCHAL_STATE_DIR"

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPTS_DIR, "..", ".."))
_DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPTS_DIR, "..", "state"))


def state_dir(explicit: str | None = None) -> str:
    """Resolve the state directory: `SENESCHAL_STATE_DIR` env, then `explicit`, then the default
    (`seneschal/state`, beside this module).

    Never creates the directory and never checks that it exists — a caller that needs it to exist
    calls `os.makedirs(..., exist_ok=True)` itself, the same as every writer in this tree already
    does before its first write."""
    env = os.environ.get(ENV_VAR)
    if env:
        return env
    if explicit:
        return explicit
    return _DEFAULT_STATE_DIR


#: The resolved default at import time, for a caller that wants a constant rather than calling
#: `state_dir()`. Reflects `SENESCHAL_STATE_DIR` as of import; a caller that needs to react to the env
#: var changing mid-process (a test that patches it, for instance) must call `state_dir()` directly
#: rather than read this constant.
STATE_DIR = state_dir()
