"""Cockpit backend config — env resolution, no secrets.

Kept tiny and dependency-free (stdlib only) so it's trivially unit-testable and so the auth/state
knobs are one obvious place, not scattered across app.py. See ../README.md for the wider design.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# The daemon's default state dir — this checkout's own `seneschal/state/`, which is where the daemon
# keeps its runtime state when both run from the same clone (the normal install). This is a value the
# cockpit READS: the backend only ever reads files under it (+ the two writes documented in
# control.py). Override with SENESCHAL_STATE_DIR for dev/tests (a temp dir, or another checkout's
# seneschal/state).
DEFAULT_STATE_DIR = Path(__file__).resolve().parents[2] / "seneschal" / "state"

# The built frontend (`npm run build` in cockpit/web) — served as static files when present.
WEB_DIST_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"

# The daemon pipe — seneschal/scripts/cockpit_pipe.py's
# sixth-supervised-task WebSocket server. Localhost-only; these three knobs are how pipe_client.py finds
# it. PIPE_TOKEN_FILENAME must match cockpit_pipe.PIPE_TOKEN_FILE.
DEFAULT_PIPE_HOST = "127.0.0.1"
DEFAULT_PIPE_PORT = 8471
PIPE_TOKEN_FILENAME = "cockpit-pipe-token"

# Real OIDC auth config (the auth stack itself — login/callback/logout routes — is a deferred
# follow-up; these getters stay so that PR is a pure re-add). Only COCKPIT_OIDC_CLIENT_ID has no
# sensible default (a real client id doesn't exist until the owner provisions an OIDC app), so its
# presence is what flips `auth.auth_mode()` from "dev"/"unconfigured" to "oidc" (see auth.py) —
# leave it unset in this build.
DEFAULT_OIDC_ISSUER = "http://localhost:8480"
DEFAULT_OIDC_REDIRECT = "http://127.0.0.1:8760/auth/callback"
DEFAULT_ALLOWED_USER = "owner"

# The archon registry — per-install (gitignored; seed from the tracked archon-registry.example.json),
# sibling of this file. Absent → empty roster (archons.load_registry tolerates it). Overridable so
# tests can point at a throwaway registry without touching the real one.
DEFAULT_ARCHON_REGISTRY_PATH = Path(__file__).resolve().parent / "archon-registry.json"


def get_state_dir() -> Path:
    """Read SENESCHAL_STATE_DIR fresh on every call (not cached) — env-driven, request-scoped tests
    flip it between calls with plain os.environ mutation."""
    return Path(os.environ.get("SENESCHAL_STATE_DIR", DEFAULT_STATE_DIR))


def is_dev_no_auth() -> bool:
    """The dev-only auth bypass. Real OIDC auth is a deferred follow-up; until it lands this is the
    one seam every gated route checks. Still 127.0.0.1-bound regardless (see app.py's localhost-only
    middleware) — this flag only controls the auth *stub*, not exposure."""
    return os.environ.get("COCKPIT_DEV_NO_AUTH") == "1"


def get_pipe_host() -> str:
    return os.environ.get("COCKPIT_PIPE_HOST", DEFAULT_PIPE_HOST)


def get_pipe_port() -> int:
    try:
        return int(os.environ.get("COCKPIT_PIPE_PORT", DEFAULT_PIPE_PORT))
    except ValueError:
        return DEFAULT_PIPE_PORT


def get_pipe_token_path() -> Path:
    """The daemon's pipe auth token file. Override with COCKPIT_PIPE_TOKEN_PATH for dev/tests; defaults
    to `<state_dir>/cockpit-pipe-token` — the exact path presence.py writes via
    `cockpit_pipe.ensure_pipe_token`."""
    override = os.environ.get("COCKPIT_PIPE_TOKEN_PATH")
    if override:
        return Path(override)
    return get_state_dir() / PIPE_TOKEN_FILENAME


def get_emote_dir() -> Optional[Path]:
    """`COCKPIT_EMOTE_DIR` — a folder of `:shortcode:` emote images (cockpit-spec.md). The emote
    feature is entirely OFF (GET /api/emotes returns an empty list) when this is unset."""
    d = os.environ.get("COCKPIT_EMOTE_DIR")
    return Path(d) if d else None


# --------------------------------------------------- real OIDC auth (deferred follow-up; see above)

def get_oidc_issuer() -> str:
    return os.environ.get("COCKPIT_OIDC_ISSUER", DEFAULT_OIDC_ISSUER).rstrip("/")


def get_oidc_client_id() -> str:
    return os.environ.get("COCKPIT_OIDC_CLIENT_ID", "").strip()


def get_oidc_redirect() -> str:
    return os.environ.get("COCKPIT_OIDC_REDIRECT", DEFAULT_OIDC_REDIRECT)


def get_allowed_user() -> str:
    return os.environ.get("COCKPIT_ALLOWED_USER", DEFAULT_ALLOWED_USER)


def get_archon_registry_path() -> Path:
    override = os.environ.get("COCKPIT_ARCHON_REGISTRY_PATH")
    return Path(override) if override else DEFAULT_ARCHON_REGISTRY_PATH
