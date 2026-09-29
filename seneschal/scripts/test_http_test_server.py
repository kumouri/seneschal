#!/usr/bin/env python3
"""Tests for `_http_test_server` — the localhost fixture that drains the request body.

WHAT THESE PIN, AND WHY THEY ARE SHAPED THIS WAY
------------------------------------------------
The bug being guarded against is a Windows abortive close: a handler that answers a POST without
reading the body leaves those bytes in the socket's receive queue, and Winsock then closes with RST
instead of FIN, destroying the response the client has not read yet. Full mechanism in
`_http_test_server`'s docstring.

The obvious test — "hammer it and assert no ConnectionResetError" — is the WRONG test, and writing it
would repeat the mistake being fixed. It only fails when the client happens to be descheduled at the
right moment, which is why the original bug hid for so long: it needs a loaded machine, and on a
quiet one it is green no matter how broken the fixture is. A test whose red depends on load is not a
guard, it is a second flake.

So these assert the INVARIANT instead: the declared body is off the socket before the response is
written. That is deterministic, it is the property the RST depends on, and it goes red against the
pre-fix handler — `_RecordingHandler` never reads the body itself, exactly like a naive fixture
would not, so anything it observes as drained came from `DrainingHTTPRequestHandler`.

"Before the response is written" is what makes these assertions safe to make at all. An earlier
draft drained in a `finally` AFTER the dispatch, which prevents the RST just as well but leaves
every test racing the handler thread for the recording — and duly failed intermittently in the
suite. The guard against a flake is the last place that can afford one.

For the same reason the never-raises cases (a malformed `Content-Length`, no request line at all)
call `drain_request_body` DIRECTLY rather than over a socket: driving them through a real request
would mean deliberately standing up the un-drained condition this module exists to prevent, i.e.
planting the flake in the test that guards against it.

Run:  python -m unittest seneschal.scripts.test_http_test_server
      python test_http_test_server.py
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import _http_test_server as hts  # noqa: E402


class _RecordingHandler(hts.DrainingHTTPRequestHandler):
    """Answers without reading the body — the pre-fix shape — and records what got drained anyway.

    `drained` is a class attribute on purpose: `http.server` builds one handler instance per request,
    so a test cannot hold a reference to the instance that served it.
    """

    drained: list = []
    STATUS = 400
    PAYLOAD = b'{"error": "nope"}'

    def _answer(self):
        self.send_response(self.STATUS)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.PAYLOAD)))
        self.end_headers()
        self.wfile.write(self.PAYLOAD)

    do_GET = do_POST = _answer

    def drain_request_body(self) -> int:
        n = super().drain_request_body()
        type(self).drained.append(n)
        return n


class _RaisingHandler(_RecordingHandler):
    """Dies before writing anything — the case where nothing at all has touched the body.

    Note `BaseHTTPRequestHandler` does NOT turn this into a 500: it lets the exception out of
    `handle_one_request` and answers nothing at all.
    """

    def _answer(self):
        raise RuntimeError("handler exploded")

    do_GET = do_POST = _answer


def _handler(base=_RecordingHandler, **attrs):
    """A fresh subclass per test, so `drained` is never shared between them."""
    return type("H", (base,), {"drained": [], **attrs})


class _StubHandler:
    """Just enough of a handler for a direct `drain_request_body` call: the never-raises arms."""

    def __init__(self, headers=None, rfile=None):
        if headers is not None:
            self.headers = headers
        self.rfile = rfile


class DrainRequestBodyTests(unittest.TestCase):
    def _post(self, server, body: bytes, path: str = "/token"):
        url = f"http://127.0.0.1:{server.server_address[1]}{path}"
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_body_is_drained_although_the_handler_never_reads_it(self):
        """The whole fix in one assertion: the fixture answered without reading, and the body still
        came off the socket. Against the pre-fix `BaseHTTPRequestHandler` this records nothing."""
        cls = _handler()
        server = hts.serve(self, cls)
        body = json.dumps({"raw": "x" * 5000}).encode()
        status, payload = self._post(server, body)
        self.assertEqual(status, 400)
        self.assertEqual(payload, _RecordingHandler.PAYLOAD)
        self.assertEqual(cls.drained, [len(body)],
                         "the declared Content-Length must be off the socket before the close")

    def test_body_is_drained_when_the_handler_raises(self):
        """A handler that died is the case least likely to have read anything itself.

        The clean EOF is the second half of the assertion: with the body drained the peer closes
        with FIN, so the client reports `Remote end closed connection without response`. An
        un-drained close is what turns that same shutdown into an RST. (`RemoteDisconnected` is
        itself a `ConnectionResetError` subclass, and `urllib` does not wrap what `getresponse`
        raises — which is why the original flake surfaced as a bare socket error too.)
        """
        cls = _handler(base=_RaisingHandler)
        server = hts.serve(self, cls)
        # The raise IS the test; `handle_error`'s traceback would just be noise on a green run.
        server.handle_error = lambda request, client_address: None
        body = b'{"raw": "abcdef"}'
        with self.assertRaises(http.client.RemoteDisconnected):
            self._post(server, body)
        self.assertEqual(cls.drained, [len(body)])

    def test_bodyless_request_drains_nothing_and_does_not_block(self):
        """A GET carries no `Content-Length`. The drain must be a no-op, not a blocking read —
        a GET-only fixture (`test_domain_age`'s) takes exactly this path."""
        cls = _handler()
        server = hts.serve(self, cls)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/x", timeout=30):
                pass
        ctx.exception.close()
        self.assertEqual(cls.drained, [0])

    def test_each_request_on_a_reused_connection_drains_its_own_body(self):
        """Keep-alive puts several requests through one `handle()` call. The drain hangs off
        `parse_request`, which is per request — so it must not degrade to once per connection.

        Driven with `http.client` directly: `urllib` opens a fresh connection per `urlopen`, so it
        could not put two requests on one socket and this test would prove nothing through it.
        """
        cls = _handler(protocol_version="HTTP/1.1")
        server = hts.serve(self, cls)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
        self.addCleanup(conn.close)
        first, second = b'{"a": 1}', b'{"bb": 22}'
        for body in (first, second):
            conn.request("POST", "/token", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
            resp.read()
        self.assertEqual(cls.drained, [len(first), len(second)])

    def test_the_drained_body_is_kept_for_the_fixture_to_read(self):
        """Draining before dispatch consumes the body, so it has to be handed back — otherwise a
        fixture that wants to assert on the request it received could no longer be written."""
        seen = {}

        class _Inspecting(_RecordingHandler):
            def _answer(self):
                seen["body"] = self.request_body
                super()._answer()
            do_GET = do_POST = _answer

        server = hts.serve(self, _handler(base=_Inspecting))
        body = b'{"raw": "look at me"}'
        self._post(server, body)
        self.assertEqual(seen["body"], body)

    def test_a_malformed_content_length_drains_nothing_and_raises_nothing(self):
        """`drain_request_body` is on the path to every response; raising there would replace the
        answer the fixture exists to give with a socket error."""
        for value in ("not-a-number", "", None, object()):
            with self.subTest(content_length=value):
                stub = _StubHandler(headers={"Content-Length": value})
                self.assertEqual(hts.DrainingHTTPRequestHandler.drain_request_body(stub), 0)

    def test_drain_is_a_no_op_before_a_request_line_is_parsed(self):
        """`self.headers` does not exist yet if the peer closed before sending a request line —
        `parse_request` calls the drain unconditionally, so this path has to be safe."""
        self.assertEqual(
            hts.DrainingHTTPRequestHandler.drain_request_body(_StubHandler()), 0)

    def test_a_short_body_ends_the_drain_instead_of_blocking(self):
        """A peer that declared more than it sent must not wedge the handler thread: an empty read
        means the connection is done, and there is nothing left to strand."""
        class _ShortFile:
            def __init__(self):
                self.reads = 0

            def read(self, n):
                self.reads += 1
                return b"abc" if self.reads == 1 else b""

        stub = _StubHandler(headers={"Content-Length": "100"}, rfile=_ShortFile())
        self.assertEqual(hts.DrainingHTTPRequestHandler.drain_request_body(stub), 3)

    def test_a_failing_socket_read_is_swallowed(self):
        """Same reason as the malformed length: this is on the path to every response."""
        class _BrokenFile:
            def read(self, n):
                raise OSError("connection reset")

        stub = _StubHandler(headers={"Content-Length": "10"}, rfile=_BrokenFile())
        self.assertEqual(hts.DrainingHTTPRequestHandler.drain_request_body(stub), 0)


class ServeHelperTests(unittest.TestCase):
    def test_cleanups_stop_the_accept_loop_before_releasing_the_socket(self):
        """LIFO registration order is the whole contract: `shutdown()` must run before
        `server_close()`, or the listening socket is released while the loop is still accepting on
        it. Recorded rather than inferred — the order is invisible from the outside once it works."""
        recorded = []

        class _FakeTest:
            def addCleanup(self, fn, *a, **kw):  # noqa: N802 — unittest API
                recorded.append(fn)

        fake = _FakeTest()
        server = hts.serve(fake, _handler())
        try:
            self.assertEqual([f.__name__ for f in recorded], ["server_close", "shutdown"],
                             "registration order is reversed by LIFO into shutdown-then-close")
        finally:
            for fn in reversed(recorded):
                fn()
        self.assertEqual(server.socket.fileno(), -1, "server_close must release the socket")

    def test_serve_tears_the_listener_down_when_its_test_finishes(self):
        """End to end through a real `TestCase`, so the cleanups fire the way they do in the real
        fixtures — a leaked listener would otherwise outlive its test and hold a port all run."""
        captured = {}

        class _Probe(unittest.TestCase):
            def runTest(self):  # noqa: N802 — unittest API
                captured["server"] = hts.serve(self, _handler())

        result = unittest.TestResult()
        _Probe().run(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(captured["server"].socket.fileno(), -1)

    def test_handler_reads_are_bounded(self):
        """The drain is a BLOCKING read, so an unbounded handler would let a client that died
        mid-body wedge its thread for the rest of the run. `timeout` is what bounds it, and the
        stdlib default does not."""
        self.assertEqual(hts.DrainingHTTPRequestHandler.timeout, hts.HANDLER_TIMEOUT_SEC)
        self.assertIsNotNone(hts.HANDLER_TIMEOUT_SEC)
        self.assertIsNone(BaseHTTPRequestHandler.timeout, "the stdlib default is unbounded")


if __name__ == "__main__":
    unittest.main()
