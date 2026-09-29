#!/usr/bin/env python3
"""The localhost `http.server` fixture the `.closed`-contract tests drive — one that does not
strand the request body.

Tests for the stdlib-HTTP scripts here (`test_domain_age` today, and any test that proves a
caller CLOSES the `urllib.error.HTTPError` it read a body from) drive a REAL localhost server on
purpose: a hand-built `HTTPError(fp=None)` has no socket behind it, so it could not show the
difference those tests exist to prove. That posture is deliberate and this module preserves it — it
fixes the fixture, it does not mock the server away.

Every localhost fixture should use this module, including a GET-only one that cannot hit the bug.
Uniformity is the point: fixtures get written by copying each other, so an exemption left standing
among them is how the next POST fixture gets written the unsafe way.

WHY THIS MODULE EXISTS — the Windows abortive close
---------------------------------------------------
A handler that answers a POST without reading the request body leaves those bytes sitting in the
socket's receive queue. When `socketserver` then closes the connection, Winsock turns that into an
ABORTIVE close: unread received data means RST, not FIN. The RST discards whatever the peer has
buffered but not yet read — so a response that was written correctly, and even acknowledged, is
destroyed in the client's receive buffer before `http.client` gets to parse it. The client sees

    ConnectionResetError:   [WinError 10054] An existing connection was forcibly closed ...
    ConnectionAbortedError: [WinError 10053] An established connection was aborted ...

raised out of `getresponse()` -> `_read_status()`, which reads as a network fault in a test that
never touched the network.

It is invisible on an idle machine because the client normally drains its buffer within microseconds
of the response landing, long before the close. The window only opens when the client thread is
descheduled between sending the request and reading the response — which is exactly what a loaded
full-suite run produces. Hence: passes 3/3 alone, fails ~1 in 3 full-suite runs, and reproduces on
demand by sleeping inside `getresponse()`. Measured on this pattern with a 50 ms client stall:
31/80 requests destroyed without the drain, 0/80 with it.

`http.client` is what makes the race reachable at all: it sets TCP_NODELAY and sends headers and
body in two separate `send()` calls, so they are two segments. Whether the server's header parse
also happens to pull the body into the same `recv` is pure timing.

HOW IT IS FIXED
---------------
`DrainingHTTPRequestHandler` drains in `parse_request`, not at the top of each `do_VERB`. Two
properties come out of that placement, and both are load-bearing:

* It is verb-agnostic. A fixture subclass declares its verbs freely (`do_GET = do_POST = _answer` is
  the house style here) and gets the drain whether or not its author knew this docstring existed.
  There is no rule for anyone to remember and no way to opt out by accident.
* It happens BEFORE the response is written. That is what makes the behaviour testable: by the time
  a client can read the response, the drain has provably already run. Draining afterwards would also
  prevent the RST — the receive queue only has to be empty at CLOSE time — but every assertion about
  it would then be racing the handler thread, which is how the first draft of `test_http_test_server`
  managed to reintroduce a flake into the guard against a flake.

A handler that raises is covered by the same placement, and covered better than a `finally` would:
the body is already off the socket before the failing `do_VERB` is ever entered.

The bytes are kept on `self.request_body` rather than thrown away, so a fixture that wants to assert
on what it received still can.

LIMIT, NAMED: only `Content-Length` bodies are drained. Every stdlib caller in this directory
hands `urllib` a `bytes` body, so `http.client` always frames it with `Content-Length`. A fixture
that starts receiving `Transfer-Encoding: chunked` would be back to stranding the body, and
`test_http_test_server` does not cover that case because nothing here can produce it. Add the branch
WITH a test the day a chunked caller appears; do not add it speculatively.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: Bounds every handler-side socket read, including the drain below. Without it a client that dies
#: mid-body would wedge its handler thread — and the drain is a BLOCKING read, so this module is the
#: reason the bound is needed. `handle_one_request` catches the resulting `TimeoutError` and closes.
#: Kept under the 30 s the callers give `urlopen`, so a wedged handler ends as a clean close on the
#: client rather than as the client's own timeout.
HANDLER_TIMEOUT_SEC = 15

_DRAIN_CHUNK = 65536


class DrainingHTTPRequestHandler(BaseHTTPRequestHandler):
    """`BaseHTTPRequestHandler` that consumes the request body and keeps the test output quiet.

    Subclass this instead of `BaseHTTPRequestHandler` for any localhost fixture. Define whatever
    `do_VERB` methods the fixture needs; the drain is not yours to remember.
    """

    timeout = HANDLER_TIMEOUT_SEC

    #: What `drain_request_body` last read, for a fixture that wants to assert on the request.
    request_body: bytes = b""

    def log_message(self, fmt, *args):
        """Silence the per-request line — these fixtures serve hundreds of requests per run."""

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        # Unconditionally, including a parse failure: `send_error` has answered by then and the
        # connection still closes, so an unread body would still make that close abortive.
        self.drain_request_body()
        return parsed

    def drain_request_body(self) -> int:
        """Read the declared request body off the socket. Returns the byte count consumed.

        Never raises. It runs on the path to every response, so a failure here would replace the
        answer the fixture exists to give with a socket error — which is the very symptom this
        module was written to remove.
        """
        self.request_body = b""
        headers = getattr(self, "headers", None)
        if headers is None:  # connection closed before a request line arrived
            return 0
        try:
            remaining = int(headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return 0
        chunks = []
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, _DRAIN_CHUNK))
                if not chunk:  # peer went away mid-body; nothing left to strand
                    break
                remaining -= len(chunk)
                chunks.append(chunk)
        except (OSError, ValueError):
            pass
        self.request_body = b"".join(chunks)
        return len(self.request_body)


def serve(test, handler_cls, host: str = "127.0.0.1"):
    """Start `handler_cls` on an ephemeral port and tear it down when `test` finishes.

    Returns the `ThreadingHTTPServer`; callers build their own URL from `server.server_address[1]`,
    since each one needs a different path or config shape.

    Cleanups are registered so that LIFO ordering runs `shutdown()` (stop accepting, join the
    `serve_forever` loop) before `server_close()` (release the listening socket). Handler threads
    stay daemonised and are deliberately NOT joined: `shutdown()`'s poll already gives an in-flight
    handler its exit, and a hard join would trade an intermittent failure for an indefinite hang.
    `HANDLER_TIMEOUT_SEC` is what actually bounds a stuck one.
    """
    server = ThreadingHTTPServer((host, 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return server
