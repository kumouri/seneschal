"""Auth for the cockpit backend — real Zitadel OIDC (v5, cockpit-spec.md "Auth (Zitadel) & the archon
SSO portal"), the v0-v4 dev stub, or nothing, by explicit **mode precedence** (checked fresh on every
call, `config.py`-style, so a `.env` reload never needs a restart in dev):

  1. **`oidc`** — `COCKPIT_OIDC_CLIENT_ID` is set (the one field with no sane default; the Zitadel app
     registration is a documented human step, see ZITADEL_SETUP.md). Real session-cookie auth below.
  2. **`dev`** — no OIDC config, but `COCKPIT_DEV_NO_AUTH=1`. The unchanged v0-v4 stub: every gated
     route open, no session, no CSRF check (there is nothing to hijack).
  3. **`unconfigured`** — neither. Every gated route 503s `"auth not configured"`, exactly as before —
     this branch is untouched so all four phases' existing tests keep passing unmodified.

## The auth-code + PKCE flow

`GET /auth/login` builds Zitadel's authorize URL (PKCE S256 challenge + a `state` nonce) and redirects
the browser there, stashing the PKCE verifier + state in a short-lived signed cookie (`session.py`) —
no server-side session store needed, so a backend restart mid-login just means "log in again," never a
stuck flow. `GET /auth/callback` validates `state` against that cookie, exchanges the code for tokens
(stdlib `urllib.request`, server-to-server), then calls Zitadel's **userinfo** endpoint with the access
token to establish identity.

**Deliberately no local JWT/JWKS verification.** Nothing here parses or checks a token signature —
the two HTTP calls above (`oidc.py`) are the *entire* trust chain: the token exchange proves the
authorization code was genuinely issued by the configured issuer for this client, and userinfo proves
the resulting access token is live and names a real Zitadel identity. That is exactly the same trust
anchor a JWT signature check would rely on ("the issuer says so" via its JWKS) — verifying a JWT
locally only saves a network round-trip, it doesn't add a distinct guarantee. Skipping it means the
cockpit's dependency list stays fastapi + uvicorn only (cockpit-spec.md ruling 3: no JWT/JWKS library
needed) and the whole trust chain is two readable HTTP calls in `oidc.py`, not a signature-verification
library to audit.

## Single-user allowlist

The userinfo `preferred_username` (or its `@`-local-part) or `sub` must match `COCKPIT_ALLOWED_USER`
(default `owner`) — anyone else gets a polite, audited 403 (`matches_allowed_user` below).

## Session + CSRF

The session cookie (`cockpit_session`, `session.py`) is HttpOnly + SameSite=Strict with a sliding
expiry (reissued on every authenticated call — see `require_auth`). CSRF protection is two layers:

- The OAuth `state` param (above) prevents an attacker from completing a login flow on the owner's
  browser using a code/state pair of the attacker's choosing (classic OAuth login-CSRF).
- Every **mutating** route also depends on `require_csrf`, which demands a custom header
  (`X-Cockpit-Requested-With: cockpit`) the frontend always sends (`cockpit/web/src/api.ts`). This is
  sufficient on its own, without a separate CSRF token, because: (a) SameSite=Strict already stops the
  session cookie from riding along on any cross-site request, so a forged request from another origin
  arrives with no session at all; (b) *even if* that changes (an older browser, a same-site-but-hostile
  subdomain), adding a non-"simple" header makes the request require a CORS preflight, and this backend
  sets no `Access-Control-Allow-Origin` at all — the browser refuses to send the real request cross-
  origin because no preflight response ever approves it. A `<form>` POST or a plain `fetch()`/`XHR`
  from another page cannot attach a custom header, so it can never satisfy this check. Both layers are
  belt-and-braces on top of the `127.0.0.1`-only bind that already exists (see app.py).
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request, Response

from . import session as session_mod
from .config import get_allowed_user, get_oidc_client_id, get_state_dir, is_dev_no_auth

CSRF_HEADER_NAME = "X-Cockpit-Requested-With"
CSRF_HEADER_VALUE = "cockpit"


def oidc_configured() -> bool:
    return bool(get_oidc_client_id())


def auth_mode() -> str:
    """`"oidc"` | `"dev"` | `"unconfigured"` — see the module docstring for precedence."""
    if oidc_configured():
        return "oidc"
    if is_dev_no_auth():
        return "dev"
    return "unconfigured"


def session_secret() -> bytes:
    return session_mod.get_or_create_secret(get_state_dir())


def current_session(request: Request) -> Optional[dict]:
    token = request.cookies.get(session_mod.SESSION_COOKIE_NAME)
    return session_mod.read_session_token(session_secret(), token)


def current_session_from_cookie(cookie: Optional[str]) -> Optional[dict]:
    """Same check as `current_session`, but from a raw cookie value — for the websocket handshake,
    where Starlette's `WebSocket.cookies` gives a plain dict, not a `Request`."""
    return session_mod.read_session_token(session_secret(), cookie)


def set_session_cookie(response: Response, subject: str) -> None:
    token = session_mod.create_session_token(session_secret(), subject)
    response.set_cookie(
        session_mod.SESSION_COOKIE_NAME, token,
        max_age=session_mod.SESSION_MAX_AGE_SECONDS, httponly=True, samesite="strict",
        # secure=False: this backend only ever binds 127.0.0.1 (plain http) at this phase (ruling 12,
        # no Cloudflare Tunnel yet). Flip to True in the same change that stands up the tunnel (TLS
        # terminates there) — see cockpit-spec.md's "Open items".
        secure=False, path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(session_mod.SESSION_COOKIE_NAME, path="/")


def set_pending_login_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        session_mod.PENDING_LOGIN_COOKIE_NAME, token,
        max_age=session_mod.PENDING_LOGIN_MAX_AGE_SECONDS, httponly=True, samesite="strict",
        secure=False, path="/",
    )


def clear_pending_login_cookie(response: Response) -> None:
    response.delete_cookie(session_mod.PENDING_LOGIN_COOKIE_NAME, path="/")


def matches_allowed_user(userinfo: dict, allowed: str) -> bool:
    """Whether the userinfo response identifies `allowed` (default `owner`) — checked against
    `preferred_username` (verbatim, and its part before an `@`, since Zitadel logins are often
    `user@domain`) and `sub`, case-insensitively."""
    allowed_l = (allowed or "").strip().lower()
    if not allowed_l:
        return False
    candidates: list[str] = []
    pu = userinfo.get("preferred_username")
    if isinstance(pu, str) and pu:
        candidates.append(pu.lower())
        candidates.append(pu.split("@", 1)[0].lower())
    sub = userinfo.get("sub")
    if isinstance(sub, str) and sub:
        candidates.append(sub.lower())
    return allowed_l in candidates


def require_auth(request: Request, response: Response) -> None:
    """FastAPI dependency for every gated REST route. Precedence per the module docstring; in `oidc`
    mode, a valid session cookie is required (401, JSON body — "401 JSON for APIs" per the spec) and
    is reissued with a fresh `iat` on every call, which is what makes the expiry "sliding" without a
    server-side session store."""
    mode = auth_mode()
    if mode == "unconfigured":
        raise HTTPException(status_code=503, detail="auth not configured")
    if mode == "dev":
        return
    sess = current_session(request)
    if sess is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    set_session_cookie(response, sess["sub"])


def require_csrf(request: Request) -> None:
    """Every mutating route ALSO depends on this (in addition to `require_auth`) — see the module
    docstring's "Session + CSRF" section. A no-op outside `oidc` mode: the dev stub has no session
    cookie to forge a request against, and the unconfigured mode already 503s everything."""
    if auth_mode() != "oidc":
        return
    if request.headers.get(CSRF_HEADER_NAME) != CSRF_HEADER_VALUE:
        raise HTTPException(status_code=403, detail="missing CSRF header")


def auth_status(request: Request) -> dict:
    """Backs the PUBLIC `GET /api/auth/status` (no auth required — it only reports mode + whether
    THIS request is authenticated, which is what the frontend needs to decide whether to show a
    "log in" screen instead of the dashboard)."""
    mode = auth_mode()
    authenticated = False
    user: Optional[str] = None
    if mode == "dev":
        authenticated = True
    elif mode == "oidc":
        sess = current_session(request)
        if sess is not None:
            authenticated = True
            user = sess.get("sub")
    return {"mode": mode, "authenticated": authenticated, "user": user}
