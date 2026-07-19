"""A minimal, in-process fake OIDC provider for the cockpit's auth tests (test_oidc.py, test_app.py).

Deliberately named with a leading underscore so unittest's `test_*.py` discovery never tries to run it
as a standalone test module — it's a fixture, not a test file. Stdlib-only (`http.server`), same shape
as `seneschal/scripts/health_listener.py`'s `Handler` pattern: implements just enough of the OIDC discovery
+ token + userinfo endpoints for `cockpit/server/oidc.py`'s real `urllib.request` calls to hit a real
socket end-to-end, rather than mocking `oidc.py`'s functions directly (which would test less of our own
code — this exercises the actual HTTP request/response shapes).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional


class FakeOIDCServer:
    def __init__(self):
        self.userinfo: dict = {"sub": "user-1", "preferred_username": "owner@seneschald.localhost"}
        self.token_status = 200
        self.userinfo_status = 200
        self.access_token = "fake-access-token"
        self.last_token_request_body: Optional[bytes] = None
        self.token_requests = 0

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # keep test output quiet
                pass

            def _send(self, code: int, obj: dict) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 (http.server API)
                if self.path.startswith("/.well-known/openid-configuration"):
                    base = f"http://127.0.0.1:{outer.port}"
                    self._send(200, {
                        "issuer": base,
                        "authorization_endpoint": f"{base}/oauth/v2/authorize",
                        "token_endpoint": f"{base}/oauth/v2/token",
                        "userinfo_endpoint": f"{base}/oidc/v1/userinfo",
                    })
                elif self.path.startswith("/oidc/v1/userinfo"):
                    auth = self.headers.get("Authorization", "")
                    if auth != f"Bearer {outer.access_token}":
                        self._send(401, {"error": "invalid_token"})
                        return
                    self._send(outer.userinfo_status, outer.userinfo)
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                if self.path.startswith("/oauth/v2/token"):
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                    except ValueError:
                        length = 0
                    outer.last_token_request_body = self.rfile.read(length) if length else b""
                    outer.token_requests += 1
                    if outer.token_status != 200:
                        self._send(outer.token_status, {"error": "invalid_grant"})
                        return
                    self._send(200, {"access_token": outer.access_token, "token_type": "Bearer"})
                else:
                    self._send(404, {"error": "not found"})

        self._handler_cls = Handler
        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = 0

    def start(self) -> str:
        self._httpd = HTTPServer(("127.0.0.1", 0), self._handler_cls)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
