"""The decoy's config — env resolution, no secrets, no dependency on cockpit/server.

Deliberately its OWN tiny module, not shared with cockpit/server/config.py — the whole point of the
decoy is ZERO shared code/config/secrets with the authed backend (seneschal/docs/cockpit-spec.md,
"The decoy"). Stdlib only, dependency-free, trivially unit-testable.
"""
from __future__ import annotations

import os

# Ollama host convention: standard Ollama listens on 11434. Some boxes run it on a NON-default port
# instead. ALWAYS honor OLLAMA_URL when set; 11434 is only the fallback default for a fresh machine
# that hasn't overridden it.
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
# cockpit-spec.md's decoy model choice: gemma4:12b — strong character/instruction-following at an
# 8-12b size. Env-swappable via DECOY_MODEL.
DEFAULT_MODEL = "gemma4:12b"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8490

# Bounds — deliberately small. This is a public, unauthenticated, zero-value-behind-it chat; the goal
# is "cheap to run, hard to abuse," not "roomy."
DEFAULT_MAX_MESSAGE_CHARS = 2000
DEFAULT_MAX_HISTORY_TURNS = 10  # exchanges (user+assistant pairs), not raw messages
DEFAULT_MAX_OUTPUT_TOKENS = 300
DEFAULT_REQUEST_TIMEOUT = 30  # seconds, for the Ollama call

# Rate limits — see ratelimit.py. Per-IP is a token bucket (burst then steady refill); global is a
# hard concurrent-request cap plus a rolling per-minute cap shared by every visitor.
DEFAULT_RATE_BURST = 5
DEFAULT_RATE_PER_MINUTE = 6
DEFAULT_GLOBAL_CONCURRENT = 3
DEFAULT_GLOBAL_PER_MINUTE = 60


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def get_ollama_url() -> str:
    return _env_str("OLLAMA_URL", DEFAULT_OLLAMA_URL).rstrip("/")


def get_model() -> str:
    return _env_str("DECOY_MODEL", DEFAULT_MODEL)


def get_host() -> str:
    return _env_str("DECOY_HOST", DEFAULT_HOST)


def get_port() -> int:
    return _env_int("DECOY_PORT", DEFAULT_PORT)


def get_max_message_chars() -> int:
    return _env_int("DECOY_MAX_MESSAGE_CHARS", DEFAULT_MAX_MESSAGE_CHARS)


def get_max_history_turns() -> int:
    return _env_int("DECOY_MAX_HISTORY_TURNS", DEFAULT_MAX_HISTORY_TURNS)


def get_max_output_tokens() -> int:
    return _env_int("DECOY_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS)


def get_request_timeout() -> int:
    return _env_int("DECOY_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)


def get_rate_burst() -> int:
    return _env_int("DECOY_RATE_BURST", DEFAULT_RATE_BURST)


def get_rate_per_minute() -> int:
    return _env_int("DECOY_RATE_PER_MINUTE", DEFAULT_RATE_PER_MINUTE)


def get_global_concurrent() -> int:
    return _env_int("DECOY_GLOBAL_CONCURRENT", DEFAULT_GLOBAL_CONCURRENT)


def get_global_per_minute() -> int:
    return _env_int("DECOY_GLOBAL_PER_MINUTE", DEFAULT_GLOBAL_PER_MINUTE)
