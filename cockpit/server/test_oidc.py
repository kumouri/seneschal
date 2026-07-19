#!/usr/bin/env python3
"""Tests for cockpit/server/oidc.py — the v5 stdlib OIDC client (real Zitadel auth, cockpit-spec.md
"Auth (Zitadel) & the archon SSO portal"). Pure stdlib, no FastAPI needed, so this runs unconditionally
(mirrors test_governor.py/test_model_config.py/test_health.py/test_session.py).

Uses `_fake_oidc_server.FakeOIDCServer` (a real localhost `http.server`) rather than mocking `oidc.py`'s
functions, so these tests exercise the actual `urllib.request` HTTP calls end-to-end.

Run: python -m unittest cockpit.server.test_oidc
"""
from __future__ import annotations

import sys
import unittest
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import oidc  # noqa: E402
from cockpit.server._fake_oidc_server import FakeOIDCServer  # noqa: E402


class PKCEHelpersTests(unittest.TestCase):
    def test_pkce_pair_shape_and_challenge_matches_verifier(self):
        import base64
        import hashlib

        verifier, challenge = oidc.new_pkce_pair()
        self.assertGreater(len(verifier), 40)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        expected = expected.rstrip(b"=").decode("ascii")
        self.assertEqual(challenge, expected)

    def test_pkce_pairs_are_unique(self):
        v1, _ = oidc.new_pkce_pair()
        v2, _ = oidc.new_pkce_pair()
        self.assertNotEqual(v1, v2)

    def test_state_is_unique(self):
        self.assertNotEqual(oidc.new_state(), oidc.new_state())


class OIDCFlowTests(unittest.TestCase):
    def setUp(self):
        oidc.clear_discovery_cache()
        self.server = FakeOIDCServer()
        self.issuer = self.server.start()

    def tearDown(self):
        self.server.stop()
        oidc.clear_discovery_cache()

    def test_discover(self):
        doc = oidc.discover(self.issuer)
        self.assertEqual(doc["authorization_endpoint"], f"{self.issuer}/oauth/v2/authorize")
        self.assertEqual(doc["token_endpoint"], f"{self.issuer}/oauth/v2/token")
        self.assertEqual(doc["userinfo_endpoint"], f"{self.issuer}/oidc/v1/userinfo")

    def test_discover_is_cached(self):
        oidc.discover(self.issuer)
        self.server.stop()  # if a second call hit the network, this would now fail
        doc = oidc.discover(self.issuer)
        self.assertIn("authorization_endpoint", doc)

    def test_discover_unreachable_raises(self):
        oidc.clear_discovery_cache()
        self.server.stop()
        with self.assertRaises(oidc.OIDCError):
            oidc.discover(self.issuer, timeout=1.0)

    def test_build_authorize_url_has_pkce_and_state(self):
        url = oidc.build_authorize_url(self.issuer, "client-1", "http://127.0.0.1:8770/auth/callback",
                                        "state-1", "challenge-1")
        parsed = urllib.parse.urlparse(url)
        self.assertTrue(url.startswith(f"{self.issuer}/oauth/v2/authorize"))
        qs = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(qs["client_id"], ["client-1"])
        self.assertEqual(qs["state"], ["state-1"])
        self.assertEqual(qs["code_challenge"], ["challenge-1"])
        self.assertEqual(qs["code_challenge_method"], ["S256"])
        self.assertEqual(qs["response_type"], ["code"])
        self.assertEqual(qs["scope"], ["openid profile"])

    def test_build_authorize_url_merges_extra_params(self):
        url = oidc.build_authorize_url(self.issuer, "c", "r", "s", "ch",
                                        extra={"prompt": "login", "max_age": "0"})
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(qs["prompt"], ["login"])
        self.assertEqual(qs["max_age"], ["0"])

    def test_exchange_code_success(self):
        tokens = oidc.exchange_code(self.issuer, "client-1", "http://redirect", "the-code", "verifier-1")
        self.assertEqual(tokens["access_token"], self.server.access_token)
        body = self.server.last_token_request_body.decode("utf-8")
        params = urllib.parse.parse_qs(body)
        self.assertEqual(params["code"], ["the-code"])
        self.assertEqual(params["code_verifier"], ["verifier-1"])
        self.assertEqual(params["client_id"], ["client-1"])
        self.assertEqual(params["grant_type"], ["authorization_code"])

    def test_exchange_code_http_error_raises_oidc_error(self):
        self.server.token_status = 400
        with self.assertRaises(oidc.OIDCError):
            oidc.exchange_code(self.issuer, "client-1", "http://redirect", "bad-code", "v")

    def test_fetch_userinfo_success(self):
        info = oidc.fetch_userinfo(self.issuer, self.server.access_token)
        self.assertEqual(info["preferred_username"], "owner@seneschald.localhost")

    def test_fetch_userinfo_bad_token_raises(self):
        with self.assertRaises(oidc.OIDCError):
            oidc.fetch_userinfo(self.issuer, "not-the-real-token")

    def test_fetch_userinfo_server_error_raises(self):
        self.server.userinfo_status = 500
        with self.assertRaises(oidc.OIDCError):
            oidc.fetch_userinfo(self.issuer, self.server.access_token)


if __name__ == "__main__":
    unittest.main()
