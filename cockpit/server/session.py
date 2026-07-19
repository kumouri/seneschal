"""Signed, stateless cookies for the cockpit's real OIDC auth (v5, cockpit-spec.md "Auth (Zitadel) &
the archon SSO portal"). Stdlib-only (hmac/hashlib/secrets/json/base64) — a small itsdangerous-style
payload+expiry+signature scheme, hand-rolled so the cockpit's runtime dependency list stays exactly
fastapi + uvicorn (cockpit-spec.md ruling 3). Two uses of the same primitive:

- The real **session cookie** (`cockpit_session`) — set after a successful OIDC login, sliding expiry
  (reissued on every authenticated request; see auth.py's `require_auth`).
- The short-lived **pending-login cookie** (`cockpit_login`) — holds the PKCE `code_verifier` + the
  `state` nonce across the redirect round-trip to Zitadel and back, so `/auth/callback` needs no
  server-side session store (the cookie itself IS the state, signed so it can't be forged/tampered).

Both cookies are HttpOnly + SameSite=Strict; see auth.py's module docstring for the CSRF reasoning.
The signing secret is a random 32 bytes, generated once and written to a gitignored file under the
state dir (never an env var, never logged, never in a doc — `seneschal/state/*` is already blanket
gitignored except README/`*.example.*`, and a raw secret has no sensible `.example` seed).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

SESSION_COOKIE_NAME = "cockpit_session"
PENDING_LOGIN_COOKIE_NAME = "cockpit_login"

SESSION_MAX_AGE_SECONDS = 6 * 3600        # "sliding expiry of a few hours" (cockpit-spec.md)
PENDING_LOGIN_MAX_AGE_SECONDS = 10 * 60   # generous for a human to finish the Zitadel round-trip

SESSION_SECRET_FILENAME = "cockpit-session-secret"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def get_or_create_secret(state_dir, filename: str = SESSION_SECRET_FILENAME) -> bytes:
    """Read the persisted secret, or generate + persist a fresh one on first use. Never raises —
    a write failure just means a fresh secret (and thus a dropped session) next call, which is
    self-healing and never a security hole (worst case: everyone gets logged out)."""
    path = Path(state_dir) / filename
    try:
        data = path.read_bytes()
        if data:
            return data
    except OSError:
        pass
    secret = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(secret)
        os.replace(tmp, path)
    except OSError:
        pass
    return secret


def sign(payload: dict, secret: bytes) -> str:
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def unsign(token: Optional[str], secret: bytes, max_age_seconds: Optional[int] = None) -> Optional[dict]:
    """`None` on ANY failure — bad shape, bad signature, expired, malformed JSON, not an object — so
    every caller treats "not authenticated" / "no pending login" uniformly. Never raises."""
    if not token or "." not in token:
        return None
    body, _, sig_b64 = token.rpartition(".")
    try:
        sig = _b64url_decode(sig_b64)
    except Exception:  # noqa: BLE001 — any decode failure is just "not a valid token"
        return None
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64url_decode(body))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    if max_age_seconds is not None:
        iat = payload.get("iat")
        if not isinstance(iat, (int, float)) or isinstance(iat, bool) or time.time() - iat > max_age_seconds:
            return None
    return payload


def create_session_token(secret: bytes, subject: str) -> str:
    return sign({"sub": subject, "iat": time.time()}, secret)


def read_session_token(secret: bytes, token: Optional[str]) -> Optional[dict]:
    return unsign(token, secret, SESSION_MAX_AGE_SECONDS)


def create_pending_login_token(secret: bytes, state: str, code_verifier: str,
                                breakglass_action: Optional[str] = None) -> str:
    """`breakglass_action` (optional) marks this login round-trip as a break-glass ladder rung 1-2
    re-auth for that action, rather than an ordinary login — see auth.py's `/auth/callback`."""
    payload = {"state": state, "cv": code_verifier, "iat": time.time()}
    if breakglass_action is not None:
        payload["bg"] = breakglass_action
    return sign(payload, secret)


def read_pending_login_token(secret: bytes, token: Optional[str]) -> Optional[dict]:
    return unsign(token, secret, PENDING_LOGIN_MAX_AGE_SECONDS)
