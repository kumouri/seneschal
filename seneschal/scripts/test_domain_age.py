#!/usr/bin/env python3
"""Tests for domain_age — fixture-driven, never hits the real network.

Pure-function tests (`parse_registration_date`, `age_days`, `render`) drive fixture dicts directly.
`lookup()` is exercised against a real localhost server (`_http_test_server`, the same fixture the
other stdlib-HTTP scripts use here) rather than a monkeypatched `urlopen` — a loopback socket is not
"the network" this suite rules out, and it is the one way to prove `lookup()` degrades to
`"unknown"` on an actual HTTP error / malformed body / connection failure instead of on a mock that
cannot lie the way a real response can.

Run:  python -m unittest seneschal.scripts.test_domain_age
      python test_domain_age.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import unittest
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import domain_age  # noqa: E402
from _http_test_server import DrainingHTTPRequestHandler, serve  # noqa: E402


def _handler(status: int, payload: bytes, *, content_type: str = "application/rdap+json"):
    class _H(DrainingHTTPRequestHandler):
        def _answer(self):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _answer

    return _H


class ParseRegistrationDateTests(unittest.TestCase):
    def test_finds_the_registration_event(self):
        payload = {"events": [
            {"eventAction": "last changed", "eventDate": "2024-08-14T07:01:31Z"},
            {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
        ]}
        self.assertEqual(domain_age.parse_registration_date(payload), "1995-08-14T04:00:00Z")

    def test_no_events_key(self):
        self.assertIsNone(domain_age.parse_registration_date({}))

    def test_no_registration_action(self):
        payload = {"events": [{"eventAction": "expiration", "eventDate": "2025-08-13T04:00:00Z"}]}
        self.assertIsNone(domain_age.parse_registration_date(payload))

    def test_registration_event_with_no_date(self):
        payload = {"events": [{"eventAction": "registration"}]}
        self.assertIsNone(domain_age.parse_registration_date(payload))


class AgeDaysTests(unittest.TestCase):
    def test_computes_whole_days(self):
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        registered = "2026-08-25T00:00:00Z"
        self.assertEqual(domain_age.age_days(registered, now=now), 9)

    def test_naive_iso_date_is_treated_as_utc(self):
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.assertEqual(domain_age.age_days("2026-08-25T00:00:00", now=now), 9)

    def test_unparseable_date_returns_none(self):
        self.assertIsNone(domain_age.age_days("not-a-date"))

    def test_future_registration_floors_at_zero(self):
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.assertEqual(domain_age.age_days("2026-09-10T00:00:00Z", now=now), 0)


class LookupTests(unittest.TestCase):
    def _base(self, server) -> str:
        return f"http://127.0.0.1:{server.server_address[1]}/domain"

    def test_known_registration(self):
        payload = json.dumps({"events": [
            {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
        ]}).encode()
        server = serve(self, _handler(200, payload))
        result = domain_age.lookup("example.com", rdap_base=self._base(server))
        self.assertEqual(result["status"], "known")
        self.assertEqual(result["domain"], "example.com")
        self.assertEqual(result["registered_date"], "1995-08-14T04:00:00Z")
        self.assertIsInstance(result["age_days"], int)
        self.assertIsNone(result["error"])

    def test_domain_is_normalized(self):
        payload = json.dumps({"events": [
            {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
        ]}).encode()
        server = serve(self, _handler(200, payload))
        result = domain_age.lookup("  Example.COM.  ", rdap_base=self._base(server))
        self.assertEqual(result["domain"], "example.com")

    def test_http_404_degrades_to_unknown(self):
        server = serve(self, _handler(404, b'{"errorCode": 404}'))
        result = domain_age.lookup("nosuchdomain.example", rdap_base=self._base(server))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"], "HTTP 404")
        self.assertIsNone(result["age_days"])

    def test_malformed_json_degrades_to_unknown(self):
        server = serve(self, _handler(200, b"<html>not json</html>", content_type="text/html"))
        result = domain_age.lookup("example.com", rdap_base=self._base(server))
        self.assertEqual(result["status"], "unknown")
        self.assertIn("unparseable", result["error"])

    def test_response_with_no_registration_event_degrades_to_unknown(self):
        payload = json.dumps({"events": [
            {"eventAction": "expiration", "eventDate": "2025-08-13T04:00:00Z"},
        ]}).encode()
        server = serve(self, _handler(200, payload))
        result = domain_age.lookup("example.com", rdap_base=self._base(server))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"], "no registration event in response")

    def test_connection_failure_degrades_to_unknown(self):
        # A closed local port: nothing is listening, so the connection is refused immediately —
        # exercises the network-failure branch without any real network access.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        result = domain_age.lookup("example.com", timeout=1,
                                    rdap_base=f"http://127.0.0.1:{port}/domain")
        self.assertEqual(result["status"], "unknown")
        self.assertIsNotNone(result["error"])

    def test_empty_domain_degrades_to_unknown_without_a_request(self):
        result = domain_age.lookup("   ")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"], "empty domain")

    def test_never_raises_on_a_server_that_answers_a_non_object(self):
        server = serve(self, _handler(200, b"[1, 2, 3]"))
        result = domain_age.lookup("example.com", rdap_base=self._base(server))
        self.assertEqual(result["status"], "unknown")
        self.assertIn("unparseable", result["error"])


class RenderTests(unittest.TestCase):
    def test_known_result(self):
        text = domain_age.render({"domain": "example.com", "status": "known",
                                   "registered_date": "1995-08-14T04:00:00Z", "age_days": 9,
                                   "error": None})
        self.assertIn("example.com", text)
        self.assertIn("9 days old", text)

    def test_unknown_result_never_reads_as_clean(self):
        text = domain_age.render({"domain": "example.com", "status": "unknown",
                                   "registered_date": None, "age_days": None,
                                   "error": "HTTP 404"})
        self.assertIn("unknown", text)
        self.assertIn("unchecked", text)
        self.assertNotIn("clean", text.lower())
        self.assertNotIn("safe", text.lower())


if __name__ == "__main__":
    unittest.main()
