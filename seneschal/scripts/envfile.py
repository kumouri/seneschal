#!/usr/bin/env python3
"""`load_env` — parse a `KEY=VALUE` env file, real process env taking precedence. Zero intra-repo
imports.

A foundation primitive. Several near-identical copies of this function exist in this directory
(`google_common.py`, `telegram_send.py`, `proton_send.py`, `discord_poll.py`, ... — they are not
byte-identical); this is the one they can collapse onto as they are touched. It is the **union** of
their behaviour: `#`-comments, blank lines skipped, only
the FIRST `=` splits (so a value may itself contain `=`), and one layer of matching quotes stripped
from the value. `rag_common.load_env` is the one outlier that pre-seeds a `DEFAULTS` dict rather
than starting empty — that shape is the caller's to build on top of this, not this function's job.
"""
from __future__ import annotations

import os


def load_env(env_file: str | None, env_keys=None) -> dict:
    """Start from a `KEY=VALUE` file (if given); real environment variables take precedence.

    A line that is blank, starts with `#`, or holds no `=` is skipped. Only the FIRST `=` splits a
    line, so a value may contain `=` itself. Both the key and value are stripped of surrounding
    whitespace, and the value additionally has one layer of matching quotes stripped (`'…'` or
    `"…"`) — exactly what every existing copy of this function does.

    `env_keys`, if given, is the set of keys the process environment is allowed to override — every
    existing copy of this function enumerates its own fixed set (`TELEGRAM_BOT_TOKEN`,
    `GOOGLE_CLIENT_ID`, ...) rather than overriding blindly, so a migrating caller passes its own
    list to keep that behaviour exactly. With `env_keys=None` (the default), every key already
    present in the parsed file may be overridden by the process environment — the permissive
    default for a caller with no fixed key list of its own."""
    values: dict = {}
    if env_file:
        with open(env_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    for k in (env_keys if env_keys is not None else list(values)):
        if os.environ.get(k):
            values[k] = os.environ[k]
    return values
