#!/usr/bin/env python3
"""Shared OAuth 2.0 + HTTP plumbing for the assistant's Google bridges. Standard library only.

Both the Google Calendar bridge (`gcal_api.py`) and the Gmail bridge (`gmail_api.py`) sit on top of the
same OAuth machinery: one Google Cloud "Desktop app" client, a per-account refresh token minted once by
`google_auth.py`, and short-lived access tokens refreshed on demand. This module is the plumbing they
share — it never talks to the Calendar or Gmail REST surfaces itself.

MULTI-ACCOUNT. The assistant can be wired to several Google accounts (e.g. "personal" and "work"). Every call is keyed by
an account *label*; the label maps to a stored refresh token in the token store. One Cloud client serves
all accounts — only the refresh tokens differ.

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines). See
google.env.example / GOOGLE_SETUP.md:
  GOOGLE_CLIENT_ID       OAuth client id from the Cloud console (Desktop app). Required.
  GOOGLE_CLIENT_SECRET   OAuth client secret (Desktop-app secrets are not truly secret, but Google
                         issues one and the token exchange needs it). Required.
  GOOGLE_SCOPES          Space-separated OAuth scopes. Optional; defaults to calendar + gmail.modify +
                         gmail.send + userinfo (see DEFAULT_SCOPES).
  GOOGLE_TOKEN_STORE     Path to the refresh-token store JSON. Optional; defaults to
                         seneschal/state/google_tokens.json (gitignored).
  GOOGLE_OAUTH_PORT      Fixed loopback port for the consent flow. Optional; 0/unset = pick a free port.

The token store is the durable record — a warm daemon session is volatile, but the refresh tokens on disk
survive a reboot. Access tokens are cached in memory only.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# --- endpoints -----------------------------------------------------------------------------------
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# calendar + read/modify mail (covers drafting & labels) + send (used only on approval) + who-am-i.
DEFAULT_SCOPES = " ".join((
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
))

ENV_KEYS = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_SCOPES",
    "GOOGLE_TOKEN_STORE",
    "GOOGLE_OAUTH_PORT",
)

# Default token store lives beside the other local-first runtime state (gitignored).
_DEFAULT_TOKEN_STORE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "google_tokens.json"
)

_ACCESS_CACHE: dict = {}  # account -> {"token": str, "expires_at": float}


class GoogleAPIError(RuntimeError):
    """A non-2xx response from a Google endpoint. Carries the HTTP status and decoded body."""

    def __init__(self, status: int, body, where: str = ""):
        self.status = status
        self.body = body
        msg = body.get("error") if isinstance(body, dict) else body
        super().__init__(f"HTTP {status} from Google {where}: {msg}")


# --- config --------------------------------------------------------------------------------------
def load_env(env_file: str | None) -> dict:
    """Start from a KEY=VALUE file (if given); real environment variables take precedence."""
    values: dict = {}
    if env_file:
        with open(env_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    for k in ENV_KEYS:
        if os.environ.get(k):
            values[k] = os.environ[k]
    return values


def cfg(creds: dict) -> dict:
    return {
        "client_id": creds.get("GOOGLE_CLIENT_ID"),
        "client_secret": creds.get("GOOGLE_CLIENT_SECRET"),
        "scopes": creds.get("GOOGLE_SCOPES") or DEFAULT_SCOPES,
        "token_store": creds.get("GOOGLE_TOKEN_STORE") or _DEFAULT_TOKEN_STORE,
        "oauth_port": int(creds.get("GOOGLE_OAUTH_PORT") or 0),
    }


def require_client(c: dict) -> None:
    if not c.get("client_id") or not c.get("client_secret"):
        raise RuntimeError(
            "Missing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (set in env or --env-file). See GOOGLE_SETUP.md."
        )


# --- token store ---------------------------------------------------------------------------------
def load_tokens(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"accounts": {}}
    data.setdefault("accounts", {})
    return data


def save_tokens(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def get_account(c: dict, account: str) -> dict:
    tokens = load_tokens(c["token_store"])
    rec = tokens["accounts"].get(account)
    if not rec or not rec.get("refresh_token"):
        known = ", ".join(sorted(tokens["accounts"])) or "(none)"
        raise RuntimeError(
            f"No stored refresh token for account '{account}'. Known accounts: {known}. "
            f"Run:  python google_auth.py --account {account} --env-file google.env"
        )
    return rec


def upsert_account(c: dict, account: str, record: dict) -> None:
    tokens = load_tokens(c["token_store"])
    existing = tokens["accounts"].get(account, {})
    existing.update(record)
    tokens["accounts"][account] = existing
    save_tokens(c["token_store"], tokens)


# --- HTTP ----------------------------------------------------------------------------------------
def http_json(method: str, url: str, *, data: bytes | None = None, headers: dict | None = None,
              timeout: int = 30, where: str = "") -> dict:
    """Do one HTTP call and return parsed JSON. Raise GoogleAPIError on a non-2xx status."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:  # noqa: BLE001
            body = raw
        raise GoogleAPIError(e.code, body, where) from e


def form_post(url: str, params: dict, *, timeout: int = 30, where: str = "") -> dict:
    data = urllib.parse.urlencode(params).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    return http_json("POST", url, data=data, headers=headers, timeout=timeout, where=where)


# --- OAuth token exchange / refresh --------------------------------------------------------------
def exchange_code(c: dict, code: str, redirect_uri: str) -> dict:
    """Trade an authorization code for tokens (includes the durable refresh_token)."""
    require_client(c)
    return form_post(TOKEN_ENDPOINT, {
        "code": code,
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, where="token(exchange)")


def refresh_access_token(c: dict, refresh_token: str) -> dict:
    """Mint a fresh access token from a stored refresh token."""
    require_client(c)
    return form_post(TOKEN_ENDPOINT, {
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, where="token(refresh)")


def access_token_for(c: dict, account: str, *, force: bool = False) -> str:
    """Return a valid access token for an account, refreshing (and caching) as needed."""
    cached = _ACCESS_CACHE.get(account)
    if cached and not force and cached["expires_at"] > time.time() + 60:
        return cached["token"]
    rec = get_account(c, account)
    tok = refresh_access_token(c, rec["refresh_token"])
    access = tok.get("access_token")
    if not access:
        raise RuntimeError(f"Refresh for '{account}' returned no access_token: {tok}")
    _ACCESS_CACHE[account] = {
        "token": access,
        "expires_at": time.time() + int(tok.get("expires_in", 3600)),
    }
    return access


def authorized_request(c: dict, account: str, method: str, url: str, *, params: dict | None = None,
                       body=None, extra_headers: dict | None = None, timeout: int = 30,
                       where: str = "") -> dict:
    """Call a Google REST endpoint with the account's bearer token; refresh once on a 401."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    payload = None
    headers = dict(extra_headers or {})
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    for attempt in (1, 2):
        headers["Authorization"] = f"Bearer {access_token_for(c, account, force=attempt == 2)}"
        try:
            return http_json(method, url, data=payload, headers=headers, timeout=timeout, where=where)
        except GoogleAPIError as e:
            if e.status == 401 and attempt == 1:
                continue  # token likely expired — force a refresh and retry once
            raise
    raise RuntimeError("unreachable")


# --- time helpers --------------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def owner_zone():
    """The owner's timezone for date/label math, via ``tz_common``: the configured identity zone
    when resolvable (tzdata rides the uv venv), else the machine-local tzinfo. Replaces the old
    hardcoded ``chicago_tz()`` helper — always returns a usable tzinfo, never None."""
    import tz_common  # noqa: PLC0415 — lazy, mirroring the old helper's lazy zoneinfo import
    return tz_common.owner_tz()


def rfc3339(dt: datetime) -> str:
    """RFC-3339 / ISO-8601 with a 'Z' for UTC (what the Google APIs want for time bounds)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
