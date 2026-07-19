#!/usr/bin/env python3
"""Tests for cockpit/breakglass/assertion.py — the break-glass assertion (cockpit-spec.md
"Break-glass"). Pure stdlib; runs unconditionally, no fastapi needed (this whole package is
deliberately fastapi-free — see its __init__.py).

Run: python -m unittest cockpit.breakglass.test_assertion
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

from cockpit.breakglass import assertion  # noqa: E402


class MintVerifyTests(unittest.TestCase):
    def setUp(self):
        self.secret = b"s" * 32

    def test_roundtrip(self):
        token = assertion.mint(self.secret, "owner", "restart")
        payload = assertion.verify(self.secret, token)
        self.assertEqual(payload["sub"], "owner")
        self.assertEqual(payload["action"], "restart")
        self.assertIn("jti", payload)

    def test_expected_action_match_required(self):
        token = assertion.mint(self.secret, "owner", "restart")
        self.assertIsNone(assertion.verify(self.secret, token, expected_action="force-pull"))
        self.assertIsNotNone(assertion.verify(self.secret, token, expected_action="restart"))

    def test_wrong_secret_rejected(self):
        token = assertion.mint(self.secret, "owner", "restart")
        self.assertIsNone(assertion.verify(b"x" * 32, token))

    def test_tampered_token_rejected(self):
        token = assertion.mint(self.secret, "owner", "restart")
        body, _, sig = token.rpartition(".")
        self.assertIsNone(assertion.verify(self.secret, body + "z." + sig))

    def test_expired_rejected(self):
        old_token = assertion.mint(self.secret, "owner", "restart")
        # Directly construct an expired payload the same way mint() would, but backdated.
        import base64
        import hashlib
        import hmac
        import json

        payload = {"sub": "owner", "action": "restart", "jti": "abc",
                   "iat": time.time() - assertion.ASSERTION_TTL_SECONDS - 5}
        body = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        sig = hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
        expired = f"{body}.{sig_b64}"
        self.assertIsNone(assertion.verify(self.secret, expired))
        self.assertIsNotNone(assertion.verify(self.secret, old_token))  # sanity: the fresh one still works

    def test_garbage_rejected(self):
        self.assertIsNone(assertion.verify(self.secret, None))
        self.assertIsNone(assertion.verify(self.secret, ""))
        self.assertIsNone(assertion.verify(self.secret, "not-a-token"))

    def test_each_mint_has_a_unique_jti(self):
        t1 = assertion.mint(self.secret, "owner", "restart")
        t2 = assertion.mint(self.secret, "owner", "restart")
        p1 = assertion.verify(self.secret, t1)
        p2 = assertion.verify(self.secret, t2)
        self.assertNotEqual(p1["jti"], p2["jti"])


class SecretPersistenceTests(unittest.TestCase):
    def test_generates_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            s1 = assertion.get_or_create_secret(state_dir)
            s2 = assertion.get_or_create_secret(state_dir)
            self.assertEqual(len(s1), 32)
            self.assertEqual(s1, s2)

    def test_distinct_from_session_secret_filename(self):
        # Just a sanity check that the two secrets are deliberately different files (see the module
        # docstring) — not a claim about cockpit/server/session.py's own filename constant.
        self.assertEqual(assertion.SECRET_FILENAME, "breakglass-assertion-secret")


if __name__ == "__main__":
    unittest.main()
