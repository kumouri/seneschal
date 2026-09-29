#!/usr/bin/env python3
"""Tests for google_common's HTTP layer — specifically that `http_json` closes the error response.

An `urllib.error.HTTPError` IS the response: it inherits `urllib.response.addinfourl`, itself a
`tempfile._TemporaryFileWrapper`. So `e.read()` without a close hands the body back while holding the
connection open until the cycle collector happens to reach the traceback cycle, which surfaces as
`ResourceWarning: Implicitly cleaning up <HTTPError ...>`. Same bug class as archon_sites.site_healthy
(#218) and cockpit archons._probe (#219).

Real localhost servers rather than stubs (same posture as cockpit/server/test_archons.py) — a hand-built
`HTTPError(fp=None)` has no socket behind it, so it could not show the difference.

Run:  python -m unittest seneschal.scripts.test_google_common
      python test_google_common.py
"""
from __future__ import annotations

import os
import sys
import unittest
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import google_common as gc_  # noqa: E402
from _http_test_server import DrainingHTTPRequestHandler, serve  # noqa: E402

ERROR_BODY = b'{"error": {"code": 403, "message": "insufficient scope"}}'


class _ErroringHandler(DrainingHTTPRequestHandler):
    """Answers every request with `STATUS` and a Google-shaped JSON error body.

    `DrainingHTTPRequestHandler`, not `BaseHTTPRequestHandler`: `http_json` POSTs a JSON body, and a
    fixture that answers without reading it makes the close abortive on Windows, destroying the
    response the client has not read yet. See `_http_test_server`.
    """

    STATUS = 500

    def _answer(self):
        self.send_response(self.STATUS)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(ERROR_BODY)))
        self.end_headers()
        self.wfile.write(ERROR_BODY)

    do_GET = do_POST = _answer


class HttpJsonErrorArmTests(unittest.TestCase):
    def _serve(self, status=500) -> str:
        handler = type("H", (_ErroringHandler,), {"STATUS": status})
        server = serve(self, handler)
        return f"http://127.0.0.1:{server.server_address[1]}/v3/calendars"

    def _capture_errors(self) -> list:
        """Keep every `HTTPError` urllib raises so `.closed` can be read afterwards.

        `.closed` rather than a ResourceWarning: `http_json` re-raises `from e`, so the `GoogleAPIError`
        holds the original as `__cause__` for as long as the caller holds it — no collection, hence no
        warning to count. The close state is the honest signal here, and it is gc-independent besides.
        """
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

    def test_error_response_is_closed(self):
        url = self._serve(500)
        seen = self._capture_errors()
        with self.assertRaises(gc_.GoogleAPIError):
            gc_.http_json("GET", url, where="calendars.list")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].closed, "http_json must close the HTTPError it reads the body from")

    def test_post_error_response_is_closed(self):
        """`form_post` (the OAuth token exchange/refresh path) routes through the same handler."""
        url = self._serve(400)
        seen = self._capture_errors()
        with self.assertRaises(gc_.GoogleAPIError):
            gc_.form_post(url, {"grant_type": "refresh_token"}, where="token(refresh)")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].closed)

    def test_closing_does_not_swallow_the_error_body(self):
        """The body is read inside the `with`, so the decoded JSON still reaches GoogleAPIError."""
        url = self._serve(403)
        with self.assertRaises(gc_.GoogleAPIError) as ctx:
            gc_.http_json("GET", url, where="calendars.list")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.body["error"]["message"], "insufficient scope")

    def test_non_json_error_body_still_falls_back_to_text(self):
        """A Google 5xx can be an HTML error page; the raw text must survive the close."""
        class _Html(_ErroringHandler):
            def _answer(self):
                body = b"<html>backend error</html>"
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST = _answer

        server = serve(self, _Html)
        with self.assertRaises(gc_.GoogleAPIError) as ctx:
            gc_.http_json("GET", f"http://127.0.0.1:{server.server_address[1]}/x", where="probe")
        self.assertEqual(ctx.exception.body, "<html>backend error</html>")


if __name__ == "__main__":
    unittest.main()
