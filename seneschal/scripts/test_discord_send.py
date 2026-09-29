#!/usr/bin/env python3
"""Tests for discord_send's REST layer — specifically that `api_request` closes the error response.

An `urllib.error.HTTPError` IS the response: it inherits `urllib.response.addinfourl`, itself a
`tempfile._TemporaryFileWrapper`. So `e.read()` without a close hands the body back while holding the
connection open until the cycle collector happens to reach the traceback cycle, which surfaces as
`ResourceWarning: Implicitly cleaning up <HTTPError ...>`.

Also the send-gate wiring: the default channel is the owner's, an explicit different `--channel-id`
is gated.

Real localhost servers rather than stubs (same posture as cockpit/server/test_archons.py) — a hand-built
`HTTPError(fp=None)` has no socket behind it, so it could not show the difference.

Run:  python -m unittest seneschal.scripts.test_discord_send
      python test_discord_send.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import discord_send  # noqa: E402
import _owner_fixture as fx  # noqa: E402
import send_gate  # noqa: E402
import send_recipients  # noqa: E402
import stateio  # noqa: E402
from _http_test_server import DrainingHTTPRequestHandler, serve  # noqa: E402


class _ErroringHandler(DrainingHTTPRequestHandler):
    """Answers every request with `STATUS` and Discord's JSON error shape.

    `DrainingHTTPRequestHandler`, not `BaseHTTPRequestHandler`: `api_request` POSTs a JSON body, and
    a fixture that answers without reading it makes the close abortive on Windows, destroying the
    response the client has not read yet. See `_http_test_server`.
    """

    STATUS = 401
    PAYLOAD = b'{"message": "401: Unauthorized", "code": 0}'

    def _answer(self):
        self.send_response(self.STATUS)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.PAYLOAD)))
        self.end_headers()
        self.wfile.write(self.PAYLOAD)

    do_GET = do_POST = _answer


class ApiRequestErrorArmTests(unittest.TestCase):
    def _cfg(self, status=401, payload=None) -> dict:
        attrs = {"STATUS": status}
        if payload is not None:
            attrs["PAYLOAD"] = payload
        server = serve(self, type("H", (_ErroringHandler,), attrs))
        return {"token": "bot-token", "channel_id": "123",
                "api_base": f"http://127.0.0.1:{server.server_address[1]}"}

    def _capture_errors(self) -> list:
        """Keep every `HTTPError` urllib raises so `.closed` can be read afterwards.

        `.closed` rather than a ResourceWarning: `api_request` re-raises `from e`, so the `RuntimeError`
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

    def test_error_response_is_closed_on_send(self):
        cfg = self._cfg(401)
        seen = self._capture_errors()
        with self.assertRaises(RuntimeError):
            discord_send.api_request(cfg, "POST", "/channels/123/messages", {"content": "hi"})
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].closed, "api_request must close the HTTPError it reads the body from")

    def test_error_response_is_closed_on_check_auth(self):
        """`--check-auth` is a GET through the same handler — the body-less request path."""
        cfg = self._cfg(403)
        seen = self._capture_errors()
        with self.assertRaises(RuntimeError):
            discord_send.api_request(cfg, "GET", "/users/@me")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].closed)

    def test_closing_does_not_swallow_the_error_message(self):
        """The body is read inside the `with`, so Discord's own message still reaches the caller."""
        cfg = self._cfg(429, json.dumps({"message": "You are being rate limited.",
                                         "retry_after": 4.2}).encode())
        with self.assertRaises(RuntimeError) as ctx:
            discord_send.api_request(cfg, "POST", "/channels/123/messages", {"content": "hi"})
        self.assertIn("HTTP 429", str(ctx.exception))
        self.assertIn("You are being rate limited.", str(ctx.exception))

    def test_non_json_error_body_still_reaches_the_message(self):
        """A gateway 502 is usually HTML, not Discord's JSON shape — the raw text must survive."""
        cfg = self._cfg(502, b"<html>bad gateway</html>")
        with self.assertRaises(RuntimeError) as ctx:
            discord_send.api_request(cfg, "GET", "/users/@me")
        self.assertIn("bad gateway", str(ctx.exception))


class RecipientClassInstrumentation(unittest.TestCase):
    """The `recipient_class` send ledger — `discord_send.py` only ever posts to the assistant's one
    dedicated private channel, so the default channel id is always `owner`; an explicit
    `--channel-id` override that differs is `unknown`."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fx.use_fixture_owner(self)
        self.ledger = os.path.join(self.dir, send_recipients.FILENAME)
        env_patch = mock.patch.dict(os.environ, {
            "SENESCHAL_STATE_DIR": self.dir,
            "DISCORD_BOT_TOKEN": "test-token",
            "DISCORD_CHANNEL_ID": "111",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        api_patch = mock.patch.object(discord_send, "api_request", return_value={"id": "msg1"})
        api_patch.start()
        self.addCleanup(api_patch.stop)

    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["discord_send.py"] + argv):
            return discord_send.main()

    def test_default_channel_records_owner(self):
        rc = self._run(["--text", "hi"])
        self.assertEqual(rc, 0)
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], "discord_send")
        self.assertEqual(rows[0]["recipient_class"], "owner")

    def test_override_matching_default_records_owner(self):
        rc = self._run(["--text", "hi", "--channel-id", "111"])
        self.assertEqual(rc, 0)
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["recipient_class"], "owner")

    def test_override_to_a_different_channel_records_unknown(self):
        rc = self._run(["--text", "hi", "--channel-id", "987654321098765432"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)  # the gate: unknown is not owner
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["recipient_class"], "unknown")

    def test_dry_run_never_writes_a_row(self):
        rc = self._run(["--text", "hi", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.ledger))

    def test_row_never_contains_the_channel_id(self):
        """A real Discord channel id is a 17+-digit snowflake — long enough that it cannot collide
        with a digit run inside the row's own microsecond `at` timestamp, unlike a short test id
        such as "222" (an `at` that happens to read `...12.222509Z` would false-positive)."""
        self._run(["--text", "hi", "--channel-id", "987654321098765432"])
        with open(self.ledger, "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("987654321098765432", raw)


class SendGate(RecipientClassInstrumentation):
    """The send gate (`send_gate.py`): the default channel is owner-class and passes; a
    different explicit `--channel-id` is refused before the API call unless a grant names it."""

    def _api(self):
        return mock.patch.object(discord_send, "api_request", return_value={"id": "msg1"})

    def test_default_channel_passes(self):
        with self._api() as api:
            rc = self._run(["--text", "hi"])
        self.assertEqual(rc, 0)
        api.assert_called_once()

    def test_other_channel_refused_then_passes_with_a_grant(self):
        with self._api() as api:
            rc = self._run(["--text", "hi", "--channel-id", "987654321098765432"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)
        api.assert_not_called()
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertFalse(rows[0]["gate"]["allowed"])
        send_gate.grant("discord", "987654321098765432", why="the owner asked for that channel")
        with self._api() as api:
            rc = self._run(["--text", "hi", "--channel-id", "987654321098765432"])
        self.assertEqual(rc, 0)
        api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
