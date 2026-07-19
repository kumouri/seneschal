"""Stdlib-only OIDC client helpers for the cockpit's real auth (v5, cockpit-spec.md "Auth (Zitadel) &
the archon SSO portal"). Authorization code + PKCE (S256) against Zitadel — or any spec-compliant OIDC
issuer, since nothing here is Zitadel-specific: endpoints come from the standard OIDC discovery
document (`<issuer>/.well-known/openid-configuration`), not hardcoded paths.

**Deliberately no local JWT/JWKS verification.** The trust anchor is the live, server-to-server token
exchange + userinfo call against the configured issuer (auth.py's `/auth/callback` does both, in
order) — the same "the issuer says so" trust a JWT signature check would rely on anyway, just without
needing a JWT/JWKS library on the cockpit's minimal dependency list (fastapi + uvicorn only,
cockpit-spec.md ruling 3). See auth.py's module docstring for the full reasoning.

Only stdlib: `urllib.request` for both HTTP calls, `hashlib`/`secrets`/`base64` for PKCE.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

DISCOVERY_TTL_SECONDS = 3600
_discovery_cache: dict[str, tuple[float, dict]] = {}


class OIDCError(RuntimeError):
    """Any failure talking to the issuer (network, non-2xx, malformed response) — auth.py turns this
    into a clear HTTP error rather than a raw traceback."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) — RFC 7636, S256 method."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def new_state() -> str:
    return _b64url(secrets.token_bytes(16))


def discover(issuer: str, timeout: float = 5.0) -> dict:
    """Fetch (and briefly cache, in-process) the OIDC discovery document. Raises OIDCError on any
    network/parse failure — callers decide how to surface that (auth.py: a 502, never a crash)."""
    now = time.time()
    cached = _discovery_cache.get(issuer)
    if cached and now - cached[0] < DISCOVERY_TTL_SECONDS:
        return cached[1]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise OIDCError(f"could not reach the OIDC issuer {issuer!r}: {e}") from e
    except json.JSONDecodeError as e:
        raise OIDCError(f"OIDC discovery document at {issuer!r} was not valid JSON: {e}") from e
    if not isinstance(doc, dict) or "authorization_endpoint" not in doc:
        raise OIDCError(f"OIDC discovery document at {issuer!r} is missing required fields")
    _discovery_cache[issuer] = (now, doc)
    return doc


def clear_discovery_cache() -> None:
    """Test hook — production code never needs this (the TTL handles staleness)."""
    _discovery_cache.clear()


def build_authorize_url(issuer: str, client_id: str, redirect_uri: str, state: str,
                         code_challenge: str, scope: str = "openid profile",
                         extra: Optional[dict] = None) -> str:
    doc = discover(issuer)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if extra:
        params.update(extra)
    return doc["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)


def exchange_code(issuer: str, client_id: str, redirect_uri: str, code: str,
                   code_verifier: str, timeout: float = 10.0) -> dict:
    """POST the authorization code + PKCE verifier to the issuer's token endpoint. Public client (no
    secret) — Zitadel's PKCE app registration (see ZITADEL_SETUP.md) needs none."""
    doc = discover(issuer)
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }).encode("utf-8")
    req = urllib.request.Request(
        doc["token_endpoint"], data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        with e:
            body = e.read().decode("utf-8", errors="replace")
        raise OIDCError(f"token exchange failed: HTTP {e.code} {body}") from e
    except urllib.error.URLError as e:
        raise OIDCError(f"token exchange failed: {e}") from e
    except json.JSONDecodeError as e:
        raise OIDCError(f"token endpoint returned invalid JSON: {e}") from e


def fetch_userinfo(issuer: str, access_token: str, timeout: float = 10.0) -> dict:
    doc = discover(issuer)
    req = urllib.request.Request(
        doc["userinfo_endpoint"],
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        with e:
            body = e.read().decode("utf-8", errors="replace")
        raise OIDCError(f"userinfo request failed: HTTP {e.code} {body}") from e
    except urllib.error.URLError as e:
        raise OIDCError(f"userinfo request failed: {e}") from e
    except json.JSONDecodeError as e:
        raise OIDCError(f"userinfo endpoint returned invalid JSON: {e}") from e
