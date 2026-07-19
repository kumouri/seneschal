"""The break-glass assertion — cockpit-spec.md "Break-glass". A short-lived, single-use, HMAC-signed
token the main cockpit backend mints right after a FRESH Zitadel re-auth (ladder rungs 1-2: password +
TOTP) and this package's `supervisor.py` verifies before starting rung 3 (the Telegram phrase).

**Stdlib-only** (hmac/hashlib/json/base64/secrets/time), so BOTH sides can import it with zero extra
runtime dependencies: `cockpit/server/` (fastapi) and `cockpit/breakglass/` (deliberately fastapi-free)
alike. This is the ONE module intentionally **shared**, not duplicated the way cockpit-spec.md ruling 3
duplicates state readers between the daemon and the cockpit backend: an assertion's signature must
agree byte-for-byte between minter and verifier, so importing the same code is strictly safer than
hand-mirroring it, and it costs neither side a new dependency (this file has none). Ruling 3's
duplication pattern is about independent dependency *worlds* choosing not to reach into each other's
runtime deps — it was never a mandate to duplicate code that has none to begin with.

**Secret:** a DEDICATED file (`breakglass-assertion-secret`, 32 random bytes, auto-generated on first
use under the state dir), separate from the cockpit's own session-cookie secret
(`cockpit/server/session.py`) — a stolen browser session cookie must never be enough to mint a valid
assertion; only the backend that just forced a fresh Zitadel re-auth can call `mint`.

**Single-use** is enforced by the CALLER (the supervisor's in-memory spent-`jti` ledger), not by this
module — `verify()` only checks shape/signature/expiry/action, since only the supervisor knows which
assertions it has already consumed.
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

SECRET_FILENAME = "breakglass-assertion-secret"
ASSERTION_TTL_SECONDS = 120  # "a short-lived signed break-glass assertion (2 min...)" — cockpit-spec.md


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def get_or_create_secret(state_dir, filename: str = SECRET_FILENAME) -> bytes:
    """Same shape as `cockpit/server/session.py::get_or_create_secret` (duplicated on purpose here —
    that one primitive IS small enough to mirror; the assertion FORMAT itself is what's shared, via
    this whole module)."""
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


def mint(secret: bytes, subject: str, action: str) -> str:
    """One assertion authorizes exactly one `action` ("restart" | "force-pull") for `subject` — minted
    the instant ladder rungs 1-2 (fresh re-auth + TOTP) succeed."""
    payload = {
        "sub": subject,
        "action": action,
        "jti": secrets.token_hex(16),
        "iat": time.time(),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def verify(secret: bytes, token: Optional[str], expected_action: Optional[str] = None) -> Optional[dict]:
    """`None` on ANY failure (bad shape, bad signature, expired, wrong action, missing fields) — never
    raises, so every caller treats "not a valid assertion" uniformly. Does NOT check single-use — see
    the module docstring."""
    if not token or "." not in token:
        return None
    body, _, sig_b64 = token.rpartition(".")
    try:
        sig = _b64url_decode(sig_b64)
    except Exception:  # noqa: BLE001
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
    iat = payload.get("iat")
    if not isinstance(iat, (int, float)) or isinstance(iat, bool):
        return None
    if time.time() - iat > ASSERTION_TTL_SECONDS:
        return None
    if expected_action is not None and payload.get("action") != expected_action:
        return None
    if not isinstance(payload.get("jti"), str) or not isinstance(payload.get("sub"), str):
        return None
    return payload
