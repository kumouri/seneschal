#!/usr/bin/env python3
"""Tests for the cockpit backend's GET /api/ws route (v2, cockpit-spec.md "The daemon pipe") — the
browser-facing fan-out: chat.send/status.get fall back to the inbox file / a synthesized "pipe down"
status when the daemon pipe isn't connected (the normal case in these tests, since no real daemon is
running), control.restart always uses the same always-available control-queue write v1's REST route
uses, and one frame reaches every connected tab. A final test drives the FULL v2 wiring end-to-end
(lifespan-managed PipeClient -> daemon frame -> browser broadcast) against a faked `websockets`
transport — no real socket, no live daemon (cockpit-spec.md ruling 3's testability goal).

Requires the `cockpit` uv extras group; SKIPPED (not errored) when fastapi/websockets aren't installed,
matching test_app.py's pattern.

Run:
  uv sync --extra cockpit --group test
  uv run python -m unittest discover -s cockpit/server -p "test_*.py"
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from fastapi.testclient import TestClient

    from cockpit.server import app as app_module
    from cockpit.server import pipe_client as pc
    from cockpit.server.app import app

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/websockets not installed (uv sync --extra cockpit)")
class CockpitWsFallbackTests(unittest.TestCase):
    """The pipe is never actually started in these tests (TestClient(app), no `with`, never runs the
    lifespan — see test_app.py's docstring precedent) — so every path below exercises the "pipe down"
    fallback discipline, which is exactly the honest-degradation behavior the spec requires."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        (self.state_dir / "sessions").mkdir(parents=True, exist_ok=True)

        self._old_state = os.environ.get("SENESCHAL_STATE_DIR")
        self._old_auth = os.environ.get("COCKPIT_DEV_NO_AUTH")
        os.environ["SENESCHAL_STATE_DIR"] = str(self.state_dir)
        os.environ["COCKPIT_DEV_NO_AUTH"] = "1"

        # The cockpit-pipe state is a module-level singleton (app._state) — reset it so no earlier
        # test's PipeClient/last_status leaks in.
        app_module._state.pipe_client = None
        app_module._state.last_status = None

        self.client = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()
        if self._old_state is None:
            os.environ.pop("SENESCHAL_STATE_DIR", None)
        else:
            os.environ["SENESCHAL_STATE_DIR"] = self._old_state
        if self._old_auth is None:
            os.environ.pop("COCKPIT_DEV_NO_AUTH", None)
        else:
            os.environ["COCKPIT_DEV_NO_AUTH"] = self._old_auth
        app_module._state.pipe_client = None
        app_module._state.last_status = None

    def _ws(self):
        """`dev` mode requires an allowlisted `Origin` on the handshake (app.py's
        `_ALLOWED_DEV_WS_ORIGINS`); supply one so these tests keep exercising fan-out/fallback.
        `CockpitWsDevModeOriginGateTests` covers the gate itself."""
        return self.client.websocket_connect("/api/ws", headers={"origin": "http://testserver"})

    def test_ws_closes_without_dev_auth(self):
        os.environ.pop("COCKPIT_DEV_NO_AUTH", None)
        with self.assertRaises(Exception):
            with self.client.websocket_connect("/api/ws") as ws:
                ws.receive_json()  # the server closed immediately; nothing to receive

    def test_chat_send_falls_back_to_inbox_file_when_pipe_down(self):
        with self._ws() as ws:
            ws.send_json({"type": "chat.send", "id": "c1", "text": "hello from a tab",
                          "force_fable": True})
            ack = ws.receive_json()
        self.assertEqual(ack, {"type": "chat.ack", "id": "c1", "via": "fallback"})
        lines = (self.state_dir / "cockpit-inbox.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        item = json.loads(lines[0])
        self.assertEqual(item["id"], "c1")
        self.assertEqual(item["text"], "hello from a tab")
        self.assertTrue(item["force_fable"])
        self.assertIn("ts", item)

    def test_blank_text_chat_send_is_dropped_silently(self):
        with self._ws() as ws:
            ws.send_json({"type": "chat.send", "id": "c2", "text": "   "})
            ws.send_json({"type": "status.get"})  # a second frame proves the connection is still alive
            frame = ws.receive_json()
        self.assertEqual(frame["type"], "status")  # NOT a chat.ack for c2 — it was never enqueued
        self.assertFalse((self.state_dir / "cockpit-inbox.jsonl").exists())

    def test_status_get_falls_back_to_synthesized_pipe_down(self):
        with self._ws() as ws:
            ws.send_json({"type": "status.get"})
            frame = ws.receive_json()
        self.assertEqual(frame["type"], "status")
        self.assertEqual(frame["pipe"], "down")

    def test_control_restart_enqueues_and_audits_via_ws(self):
        with self._ws() as ws:
            ws.send_json({"type": "control.restart"})
            ack = ws.receive_json()
        self.assertEqual(ack["type"], "control.ack")
        self.assertTrue(ack["ok"])
        self.assertFalse(ack["already_queued"])

        queue = json.loads((self.state_dir / "control-queue.json").read_text(encoding="utf-8"))
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["action"], "restart")
        self.assertTrue(queue[0]["defer_until_idle"])

        audit_lines = (self.state_dir / "cockpit-audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(audit_lines), 1)
        audit = json.loads(audit_lines[0])
        self.assertEqual(audit["action"], "control.restart")
        self.assertEqual(audit["detail"]["via"], "ws")

    def test_a_frame_broadcasts_to_every_connected_tab(self):
        with self._ws() as ws1, self._ws() as ws2:
            ws1.send_json({"type": "status.get"})
            f1 = ws1.receive_json()
            f2 = ws2.receive_json()  # the SAME synthesized status also reached the other tab
        self.assertEqual(f1, f2)

    def test_unknown_frame_type_is_ignored_not_fatal(self):
        with self._ws() as ws:
            ws.send_json({"type": "something.weird"})
            ws.send_json({"type": "status.get"})
            frame = ws.receive_json()
        self.assertEqual(frame["type"], "status")

    def test_status_route_reports_pipe_down_when_not_connected(self):
        self.client.get("/api/health")  # no-op just to ensure the app is importable/healthy
        resp = self.client.get("/api/status", headers={})
        # status requires dev-auth like every other gated route (already set in setUp)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pipe"], "down")


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/websockets not installed (uv sync --extra cockpit)")
class _FakePipeWS:
    """Duck-typed stand-in for the daemon side of the pipe connection, from pipe_client.py's point of
    view — mirrors test_pipe_client.py's fake exactly."""

    def __init__(self, auth_ack: str, inbound_after_auth=None):
        import asyncio
        self.auth_ack = auth_ack
        self.inbound = list(inbound_after_auth or [])
        self.sent: list[str] = []
        self._asyncio = asyncio

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return self.auth_ack

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for msg in self.inbound:
            yield msg
        await self._asyncio.Event().wait()


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/websockets not installed (uv sync --extra cockpit)")
class _FakeConnCM:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc):
        return False


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/websockets not installed (uv sync --extra cockpit)")
class _FakeWebsocketsModule:
    def __init__(self, ws):
        self._ws = ws
        self.urls: list[str] = []

    def connect(self, url, **_kwargs):
        self.urls.append(url)
        return _FakeConnCM(self._ws)


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/websockets not installed (uv sync --extra cockpit)")
class CockpitWsFullPipeWiring(unittest.TestCase):
    """The one true end-to-end test: the lifespan actually starts a PipeClient, which "connects" to a
    faked transport, authenticates with a real token file, and relays a daemon-originated status frame
    all the way to a connected browser tab. Everything runs on the SAME event loop (TestClient's
    portal), so no cross-loop asyncio primitives are touched — see the module docstring."""

    def test_daemon_status_frame_reaches_a_connected_browser_tab(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            state_dir = Path(tmp.name)
            (state_dir / "sessions").mkdir(parents=True, exist_ok=True)
            (state_dir / "cockpit-pipe-token").write_text("secret", encoding="utf-8")

            old_state = os.environ.get("SENESCHAL_STATE_DIR")
            old_auth = os.environ.get("COCKPIT_DEV_NO_AUTH")
            os.environ["SENESCHAL_STATE_DIR"] = str(state_dir)
            os.environ["COCKPIT_DEV_NO_AUTH"] = "1"
            app_module._state.pipe_client = None
            app_module._state.last_status = None

            status_event = {"type": "status", "session_up": True, "model": "opus", "queue_depth": 0}
            fake_ws = _FakePipeWS(auth_ack=json.dumps({"type": "auth.ok"}),
                                 inbound_after_auth=[json.dumps(status_event)])
            fake_module = _FakeWebsocketsModule(fake_ws)

            orig_ws = pc.websockets
            pc.websockets = fake_module
            try:
                with TestClient(app) as client:  # triggers lifespan -> PipeClient.start()
                    deadline = time.monotonic() + 5.0
                    while not (app_module._state.pipe_client and app_module._state.pipe_client.connected):
                        if time.monotonic() > deadline:
                            self.fail("pipe client never reported connected against the fake transport")
                        time.sleep(0.02)

                    with client.websocket_connect("/api/ws", headers={"origin": "http://testserver"}) as ws:
                        frame = ws.receive_json()
                self.assertEqual(frame["type"], "status")
                self.assertTrue(frame["session_up"])
                self.assertEqual(frame["model"], "opus")
                self.assertEqual(frame["pipe"], "up")
            finally:
                pc.websockets = orig_ws
        finally:
            if old_state is None:
                os.environ.pop("SENESCHAL_STATE_DIR", None)
            else:
                os.environ["SENESCHAL_STATE_DIR"] = old_state
            if old_auth is None:
                os.environ.pop("COCKPIT_DEV_NO_AUTH", None)
            else:
                os.environ["COCKPIT_DEV_NO_AUTH"] = old_auth
            app_module._state.pipe_client = None
            app_module._state.last_status = None
            tmp.cleanup()


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/websockets not installed (uv sync --extra cockpit)")
class CockpitWsDevModeOriginGateTests(unittest.TestCase):
    """`dev` mode sets no cookie, and a WebSocket handshake is exempt from the Same-Origin Policy — so
    without an Origin gate any page open in the owner's browser could drive the assistant over
    ws://127.0.0.1:8760/api/ws. These pin the gate from the outside."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_state = os.environ.get("SENESCHAL_STATE_DIR")
        self._old_auth = os.environ.get("COCKPIT_DEV_NO_AUTH")
        os.environ["SENESCHAL_STATE_DIR"] = self._tmp.name
        os.environ["COCKPIT_DEV_NO_AUTH"] = "1"
        app_module._state.pipe_client = None
        app_module._state.last_status = None
        self.client = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()
        for key, old in (("SENESCHAL_STATE_DIR", self._old_state), ("COCKPIT_DEV_NO_AUTH", self._old_auth)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    def _status(self, headers):
        with self.client.websocket_connect("/api/ws", headers=headers) as ws:
            ws.send_json({"type": "status.get"})
            return ws.receive_json()

    def _refused(self, headers):
        with self.assertRaises(Exception):
            self._status(headers)

    def test_no_origin_header_is_refused(self):
        self._refused({})

    def test_a_foreign_origin_is_refused(self):
        self._refused({"origin": "http://evil.example"})

    def test_the_built_frontend_origin_is_accepted(self):
        self.assertEqual(self._status({"origin": "http://127.0.0.1:8760"})["type"], "status")

    def test_the_vite_dev_server_origin_is_accepted(self):
        self.assertEqual(self._status({"origin": "http://localhost:5173"})["type"], "status")

    def test_a_reverse_proxied_origin_matching_the_host_is_accepted(self):
        headers = {"origin": "http://cockpit.lan", "host": "cockpit.lan"}
        self.assertEqual(self._status(headers)["type"], "status")

    def test_a_foreign_origin_with_the_proxied_host_is_refused(self):
        self._refused({"origin": "http://evil.example", "host": "cockpit.lan"})

    def test_a_scheme_mismatch_is_refused(self):
        self._refused({"origin": "https://cockpit.lan", "host": "cockpit.lan"})


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/websockets not installed (uv sync --extra cockpit)")
class DevWsOriginSameHostHelperTests(unittest.TestCase):
    def test_matches_same_scheme_and_host(self):
        self.assertTrue(app_module._dev_ws_origin_same_host("http://cockpit.lan", "cockpit.lan", False))

    def test_matches_with_an_explicit_port_on_both_sides(self):
        self.assertTrue(app_module._dev_ws_origin_same_host("http://cockpit.lan:8080", "cockpit.lan:8080", False))

    def test_rejects_a_foreign_origin(self):
        self.assertFalse(app_module._dev_ws_origin_same_host("http://evil.example", "cockpit.lan", False))

    def test_scheme_must_match_the_connection(self):
        self.assertFalse(app_module._dev_ws_origin_same_host("https://cockpit.lan", "cockpit.lan", False))
        self.assertTrue(app_module._dev_ws_origin_same_host("https://cockpit.lan", "cockpit.lan", True))

    def test_rejects_missing_or_malformed_values(self):
        self.assertFalse(app_module._dev_ws_origin_same_host(None, "cockpit.lan", False))
        self.assertFalse(app_module._dev_ws_origin_same_host("http://cockpit.lan", None, False))
        self.assertFalse(app_module._dev_ws_origin_same_host("not-a-url", "cockpit.lan", False))
        self.assertFalse(app_module._dev_ws_origin_same_host("http://", "cockpit.lan", False))

if __name__ == "__main__":
    unittest.main()
