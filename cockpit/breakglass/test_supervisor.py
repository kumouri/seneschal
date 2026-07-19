#!/usr/bin/env python3
"""Tests for cockpit/breakglass/supervisor.py — the stdlib-only break-glass HTTP server
(cockpit-spec.md "Break-glass"). Runs a REAL `ThreadingHTTPServer` on an OS-assigned localhost port in
a background thread (no mocked transport) so the whole request/response cycle is exercised, with
`Handler.runner` set to a `RecordingRunner` (never a real process/git/file operation) and
`supervisor.telegram_notify` monkeypatched (never a real Telegram call, never a real subprocess).

Run: python -m unittest cockpit.breakglass.test_supervisor
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.breakglass import assertion as assertion_mod  # noqa: E402
from cockpit.breakglass import supervisor  # noqa: E402
from cockpit.breakglass.actions import BreakglassConfig, RecordingRunner  # noqa: E402


class SupervisorHTTPTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "state"
        self.repo_root = Path(self._tmp.name) / "repo"
        self.state_dir.mkdir(parents=True)
        self.repo_root.mkdir(parents=True)
        self.run_presence_cmd = self.repo_root / "seneschal" / "scripts" / "run-presence.cmd"

        self.secret = assertion_mod.get_or_create_secret(self.state_dir)
        self.cfg = BreakglassConfig(repo_root=self.repo_root, state_dir=self.state_dir,
                                    run_presence_cmd=self.run_presence_cmd, deploy_branch="develop")
        self.runner = RecordingRunner()

        supervisor.Handler.state = supervisor.SupervisorState()
        supervisor.Handler.secret = self.secret
        supervisor.Handler.cfg = self.cfg
        supervisor.Handler.telegram_env = None
        supervisor.Handler.runner = self.runner

        self.telegram_messages: list[str] = []
        self._orig_notify = supervisor.telegram_notify

        def fake_notify(_env, text):
            self.telegram_messages.append(text)
            return True

        supervisor.telegram_notify = fake_notify

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        supervisor.telegram_notify = self._orig_notify
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def _post(self, path: str, body: dict):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def _get(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def _audit_actions(self) -> list[str]:
        path = self.state_dir / supervisor.AUDIT_FILE
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(line)["action"] for line in lines if line]

    def _phrase_from(self, message: str) -> str:
        return message.split("Rung 3 phrase: ", 1)[1].split("\n", 1)[0]

    # --- liveness ------------------------------------------------------------------------------

    def test_health(self):
        status, data = self._get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_unknown_route_404(self):
        status, _ = self._get("/nope")
        self.assertEqual(status, 404)

    # --- CORS (the frontend calls this process cross-origin by design) ---------------------------

    def test_options_preflight_answers_204_with_cors_headers(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("OPTIONS", "/start", headers={"Origin": "http://127.0.0.1:8770"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "http://127.0.0.1:8770")
        self.assertIn("POST", resp.getheader("Access-Control-Allow-Methods"))
        conn.close()

    def test_responses_echo_the_request_origin(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/health", headers={"Origin": "http://localhost:5173"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "http://localhost:5173")
        conn.close()

    # --- the full ladder -------------------------------------------------------------------------

    def test_full_ladder_restart(self):
        token = assertion_mod.mint(self.secret, "owner", "restart")
        status, data = self._post("/start", {"assertion": token, "action": "restart"})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.telegram_messages), 1)
        phrase = self._phrase_from(self.telegram_messages[0])

        status, data = self._post("/verify", {"attempt_id": data["attempt_id"], "phrase": phrase})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["action"], "restart")
        self.assertEqual(self.runner.calls[-1][0], "spawn_detached")

        actions = self._audit_actions()
        self.assertIn("breakglass.rung3_started", actions)
        self.assertIn("breakglass.rung3_verified", actions)
        self.assertIn("breakglass.action_done", actions)

    def test_full_ladder_force_pull(self):
        token = assertion_mod.mint(self.secret, "owner", "force-pull")
        _, data = self._post("/start", {"assertion": token, "action": "force-pull"})
        phrase = self._phrase_from(self.telegram_messages[-1])

        status, data = self._post("/verify", {"attempt_id": data["attempt_id"], "phrase": phrase})
        self.assertEqual(status, 200)
        self.assertEqual(data["action"], "force-pull")
        self.assertTrue(any(c[0] == "run" and c[1][0] == "git" for c in self.runner.calls))
        self.assertTrue(any(c[0] == "spawn_detached" for c in self.runner.calls))

    # --- assertion checks ------------------------------------------------------------------------

    def test_invalid_assertion_rejected(self):
        status, _ = self._post("/start", {"assertion": "garbage", "action": "restart"})
        self.assertEqual(status, 403)
        self.assertEqual(self.runner.calls, [])
        self.assertIn("breakglass.start_rejected", self._audit_actions())

    def test_action_mismatch_rejected(self):
        token = assertion_mod.mint(self.secret, "owner", "restart")
        status, _ = self._post("/start", {"assertion": token, "action": "force-pull"})
        self.assertEqual(status, 403)

    def test_bad_action_value_400(self):
        status, _ = self._post("/start", {"assertion": "x", "action": "delete-everything"})
        self.assertEqual(status, 400)

    def test_assertion_is_single_use(self):
        token = assertion_mod.mint(self.secret, "owner", "restart")
        status1, _ = self._post("/start", {"assertion": token, "action": "restart"})
        self.assertEqual(status1, 200)
        status2, data2 = self._post("/start", {"assertion": token, "action": "restart"})
        self.assertEqual(status2, 403)
        self.assertIn("already used", data2["error"])

    def test_telegram_notify_failure_burns_the_assertion(self):
        supervisor.telegram_notify = lambda _e, _t: False
        token = assertion_mod.mint(self.secret, "owner", "restart")
        status, _ = self._post("/start", {"assertion": token, "action": "restart"})
        self.assertEqual(status, 502)
        # the assertion was burned even though rung 3 never got sent — retrying it hits "already used",
        # not another notify attempt (never leaves an assertion replayable after a partial failure).
        supervisor.telegram_notify = lambda _e, _t: True
        status2, data2 = self._post("/start", {"assertion": token, "action": "restart"})
        self.assertEqual(status2, 403)
        self.assertIn("already used", data2["error"])

    # --- rung 3 (the phrase) ---------------------------------------------------------------------

    def test_wrong_phrase_does_not_execute_and_reports_tries_remaining(self):
        token = assertion_mod.mint(self.secret, "owner", "restart")
        _, data = self._post("/start", {"assertion": token, "action": "restart"})
        status, data2 = self._post("/verify", {"attempt_id": data["attempt_id"], "phrase": "nope-nope-nope"})
        self.assertEqual(status, 403)
        self.assertEqual(data2["tries_remaining"], supervisor.MAX_PHRASE_ATTEMPTS - 1)
        self.assertEqual(self.runner.calls, [])

    def test_too_many_wrong_phrases_burns_the_attempt(self):
        token = assertion_mod.mint(self.secret, "owner", "restart")
        _, data = self._post("/start", {"assertion": token, "action": "restart"})
        attempt_id = data["attempt_id"]
        status = None
        for _ in range(supervisor.MAX_PHRASE_ATTEMPTS):
            status, _ = self._post("/verify", {"attempt_id": attempt_id, "phrase": "wrong"})
        self.assertEqual(status, 403)
        status2, data2 = self._post("/verify", {"attempt_id": attempt_id, "phrase": "wrong"})
        self.assertEqual(status2, 400)  # burned — the attempt no longer exists at all
        self.assertEqual(self.runner.calls, [])

    def test_unknown_attempt_id_400(self):
        status, _ = self._post("/verify", {"attempt_id": "nope", "phrase": "whatever"})
        self.assertEqual(status, 400)

    def test_verifying_the_wrong_action_assertion_never_reaches_runner(self):
        # A force-pull assertion started against /start with a mismatched `action` never even creates
        # an attempt, so runner.calls stays empty for the whole scenario.
        token = assertion_mod.mint(self.secret, "owner", "restart")
        self._post("/start", {"assertion": token, "action": "force-pull"})
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
