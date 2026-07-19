#!/usr/bin/env python3
"""Tests for cockpit/server/session.py — the v5 signed-cookie primitives (real OIDC auth,
cockpit-spec.md "Auth (Zitadel) & the archon SSO portal"). Pure stdlib, no FastAPI needed, so this runs
unconditionally (mirrors test_governor.py/test_model_config.py/test_health.py).

Run: python -m unittest cockpit.server.test_session
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import session  # noqa: E402


class SignUnsignTests(unittest.TestCase):
    def setUp(self):
        self.secret = b"a" * 32

    def test_roundtrip(self):
        token = session.sign({"sub": "owner", "iat": time.time()}, self.secret)
        payload = session.unsign(token, self.secret)
        self.assertEqual(payload["sub"], "owner")

    def test_tampered_body_rejected(self):
        token = session.sign({"sub": "owner", "iat": time.time()}, self.secret)
        body, _, sig = token.rpartition(".")
        tampered = body + "x" + "." + sig
        self.assertIsNone(session.unsign(tampered, self.secret))

    def test_wrong_secret_rejected(self):
        token = session.sign({"sub": "owner", "iat": time.time()}, self.secret)
        self.assertIsNone(session.unsign(token, b"b" * 32))

    def test_garbage_token_rejected(self):
        self.assertIsNone(session.unsign("not-a-token", self.secret))
        self.assertIsNone(session.unsign("", self.secret))
        self.assertIsNone(session.unsign(None, self.secret))

    def test_expiry(self):
        old = session.sign({"sub": "owner", "iat": time.time() - 1000}, self.secret)
        self.assertIsNone(session.unsign(old, self.secret, max_age_seconds=10))
        self.assertIsNotNone(session.unsign(old, self.secret, max_age_seconds=100000))

    def test_missing_iat_rejected_when_max_age_given(self):
        token = session.sign({"sub": "owner"}, self.secret)
        self.assertIsNone(session.unsign(token, self.secret, max_age_seconds=100))
        # ...but is fine with no max_age check at all
        self.assertIsNotNone(session.unsign(token, self.secret, max_age_seconds=None))

    def test_non_dict_payload_rejected(self):
        # sign() always produces a dict payload; simulate a hand-crafted non-object body instead.
        body = session._b64url_encode(b"[1, 2, 3]")
        import hashlib
        import hmac as hmac_mod
        sig = hmac_mod.new(self.secret, body.encode("ascii"), hashlib.sha256).digest()
        token = f"{body}.{session._b64url_encode(sig)}"
        self.assertIsNone(session.unsign(token, self.secret))


class SessionTokenHelpersTests(unittest.TestCase):
    def setUp(self):
        self.secret = b"c" * 32

    def test_create_and_read_session_token(self):
        token = session.create_session_token(self.secret, "owner")
        payload = session.read_session_token(self.secret, token)
        self.assertEqual(payload["sub"], "owner")

    def test_expired_session_token(self):
        token = session.sign({"sub": "owner", "iat": time.time() - session.SESSION_MAX_AGE_SECONDS - 5},
                              self.secret)
        self.assertIsNone(session.read_session_token(self.secret, token))

    def test_pending_login_token_roundtrip(self):
        token = session.create_pending_login_token(self.secret, "state123", "verifier456")
        payload = session.read_pending_login_token(self.secret, token)
        self.assertEqual(payload["state"], "state123")
        self.assertEqual(payload["cv"], "verifier456")
        self.assertNotIn("bg", payload)

    def test_pending_login_token_carries_breakglass_action(self):
        token = session.create_pending_login_token(self.secret, "s", "v", breakglass_action="restart")
        payload = session.read_pending_login_token(self.secret, token)
        self.assertEqual(payload["bg"], "restart")

    def test_pending_login_token_expiry_shorter_than_session(self):
        old = session.sign({"state": "s", "cv": "v",
                             "iat": time.time() - session.PENDING_LOGIN_MAX_AGE_SECONDS - 5}, self.secret)
        self.assertIsNone(session.read_pending_login_token(self.secret, old))


class SecretPersistenceTests(unittest.TestCase):
    def test_generates_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            secret1 = session.get_or_create_secret(state_dir)
            self.assertEqual(len(secret1), 32)
            secret2 = session.get_or_create_secret(state_dir)
            self.assertEqual(secret1, secret2)
            path = state_dir / session.SESSION_SECRET_FILENAME
            self.assertTrue(path.is_file())

    def test_different_state_dirs_get_different_secrets(self):
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            s1 = session.get_or_create_secret(Path(tmp1))
            s2 = session.get_or_create_secret(Path(tmp2))
            self.assertNotEqual(s1, s2)

    def test_custom_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            secret = session.get_or_create_secret(state_dir, filename="custom-secret")
            self.assertTrue((state_dir / "custom-secret").is_file())
            self.assertFalse((state_dir / session.SESSION_SECRET_FILENAME).is_file())
            self.assertEqual(secret, session.get_or_create_secret(state_dir, filename="custom-secret"))


if __name__ == "__main__":
    unittest.main()
