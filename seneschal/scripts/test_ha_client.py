#!/usr/bin/env python3
"""Tests for ha_client.call_service — that it closes the error response it never reads.

This is the subtlest of the unclosed-`HTTPError` sites: the error arm only reads `e.code`/`e.reason`,
so there is no body read to wrap in a `with`. It leaks anyway — an `HTTPError` IS the response (it
inherits `urllib.response.addinfourl`, itself a `tempfile._TemporaryFileWrapper`), so it holds its
connection until the cycle collector reaches it, surfacing as
`ResourceWarning: Implicitly cleaning up <HTTPError ...>`.

`call_service` documents that it NEVER raises, so — like archon_sites.site_healthy (#218) and cockpit
archons._probe (#219), and unlike the body-reading sites — the close is guarded and happens only AFTER
the verdict is built, rather than via `with e:` whose `__exit__` would propagate a failing close.

Run:  python -m unittest seneschal.scripts.test_ha_client
      python test_ha_client.py
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import warnings

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import ha_client  # noqa: E402
from _http_test_server import DrainingHTTPRequestHandler, serve  # noqa: E402


class _Handler(DrainingHTTPRequestHandler):
    """Answers every service call with `STATUS`.

    `DrainingHTTPRequestHandler`, not `BaseHTTPRequestHandler`: `call_service` POSTs a JSON body, and
    a fixture that answers without reading it makes the close abortive on Windows, destroying the
    response the client has not read yet. See `_http_test_server`.
    """

    STATUS = 500

    def do_POST(self):  # noqa: N802 — HA service calls are POSTs
        body = b'{"message": "entity not found"}'
        self.send_response(self.STATUS)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ClosingHTTPError(urllib.error.HTTPError):
    """Records `close()` — and optionally fails it, to pin the never-raises contract. Used only where a
    real socket cannot show the difference (a real close does not fail on demand).

    `super().close()` runs BEFORE the simulated failure so the fixture itself stays leak-free: CPython
    substitutes an empty buffer when `fp is None`, so even a hand-built HTTPError carries a real
    `tempfile` closer and would emit the very ResourceWarning these tests exist to eliminate. From
    `call_service`'s side the boundary behaves identically — the `e.close()` it calls raises."""

    def __init__(self, close_raises=False):
        super().__init__("http://ha.invalid/api/services/light/turn_on", 503,
                         "Service Unavailable", None, None)
        self.close_recorded = False
        self._close_raises = close_raises

    def close(self):
        self.close_recorded = True
        super().close()
        if self._close_raises:
            raise OSError("socket already torn down")


class CallServiceTestCase(unittest.TestCase):
    def _env(self, port) -> str:
        env = os.path.join(tempfile.mkdtemp(), "ha.env")
        with open(env, "w", encoding="utf-8") as fh:
            fh.write(f"HA_URL=http://127.0.0.1:{port}\nHA_TOKEN=long-lived-token\n")
        return env

    def _serve(self, status=500) -> str:
        server = serve(self, type("H", (_Handler,), {"STATUS": status}))
        return self._env(server.server_address[1])

    def _patch_urlopen(self, err) -> None:
        original = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", original)

        def urlopen(*a, **kw):
            raise err
        urllib.request.urlopen = urlopen

    def _capture_errors(self) -> list:
        seen = []
        original = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", original)

        def urlopen(*a, **kw):
            try:
                return original(*a, **kw)
            except urllib.error.HTTPError as e:
                seen.append(e)
                raise
        urllib.request.urlopen = urlopen
        return seen


class ErrorArmTests(CallServiceTestCase):
    def test_error_response_is_closed(self):
        env = self._serve(500)
        seen = self._capture_errors()
        result = ha_client.call_service("light", "turn_on", {"entity_id": "light.desk"}, env_path=env)
        self.assertEqual(result, {"ok": False, "status": 500, "error": "Internal Server Error"})
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].closed, "call_service must close the HTTPError it inspects")

    def test_error_call_emits_no_resource_warning(self):
        """The leak's own diagnostic. Nothing holds the `HTTPError` once `call_service` returns its
        dict — the verdict copies `e.code`/`e.reason`, not the response — so a `gc.collect()` here
        reaches it, and an unclosed one warns from `tempfile`'s finalizer."""
        env = self._serve(500)
        gc.collect()  # drain any earlier test's garbage so only this call's warnings land in the window
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            ha_client.call_service("light", "turn_on", {}, env_path=env)
            gc.collect()
        self.assertEqual([str(w.message) for w in caught
                          if issubclass(w.category, ResourceWarning)
                          and "Implicitly cleaning up <HTTPError" in str(w.message)], [])

    def test_close_happens_on_the_error_arm(self):
        err = _ClosingHTTPError()
        self._patch_urlopen(err)
        result = ha_client.call_service("light", "turn_on", {}, env_path=self._env(1234))
        self.assertTrue(err.close_recorded)
        self.assertEqual(result["status"], 503)

    def test_a_failing_close_neither_raises_nor_flips_the_verdict(self):
        """`call_service` promises it never raises, and it closes only AFTER building the verdict — so a
        torn-down socket can neither escape nor rewrite what the caller is told."""
        err = _ClosingHTTPError(close_raises=True)
        self._patch_urlopen(err)
        result = ha_client.call_service("light", "turn_on", {}, env_path=self._env(1234))
        self.assertTrue(err.close_recorded)
        self.assertEqual(result, {"ok": False, "status": 503, "error": "Service Unavailable"})


class UnchangedBehaviourTests(CallServiceTestCase):
    """Guards the contracts the close must not disturb."""

    def test_success_arm_reports_ok(self):
        env = self._serve(200)
        self.assertEqual(ha_client.call_service("light", "turn_on", {}, env_path=env),
                         {"ok": True, "status": 200})

    def test_unconfigured_stays_inert(self):
        missing = os.path.join(tempfile.mkdtemp(), "absent.env")
        result = ha_client.call_service("light", "turn_on", {}, env_path=missing)
        self.assertFalse(result["ok"])
        self.assertIn("not configured", result["error"])

    def test_transport_failure_is_reported_not_raised(self):
        env = self._env(1)  # nothing listening on port 1
        result = ha_client.call_service("light", "turn_on", {}, env_path=env, timeout=2.0)
        self.assertFalse(result["ok"])
        self.assertNotIn("status", result)


if __name__ == "__main__":
    unittest.main()
