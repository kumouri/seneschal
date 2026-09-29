#!/usr/bin/env python3
"""Tests for `telegram_send.py`'s `--document`/`send_document` — the Telegram half of outbound
attachments, `proton_send.py --attach`'s sibling.

`send_document` mirrors `send_photo`'s own conventions exactly (same `telegram_http.build_multipart`
door, same `th.UNSAFE` transport policy, no ack gate/dedupe/chunking), so `SendDocumentTest` mirrors
`test_telegram_http.SendPhotoTest`'s fixture exactly: offline via the `api` transport seam, a fake
response object standing in for `telegram_http.request`.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import telegram_http as th  # noqa: E402
import telegram_send as ts  # noqa: E402


class SendDocumentTest(unittest.TestCase):
    """`telegram_send.send_document` — offline via the `api` transport seam, exactly as
    `test_telegram_http.SendPhotoTest` drives `send_photo`."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
        self.tmp.write(b"# a spec\n")
        self.tmp.close()
        self.addCleanup(os.remove, self.tmp.name)

    def _cfg(self):
        return {"token": "123:abc", "chat_id": "999", "api_base": "https://api.telegram.org"}

    def _fake_response(self, payload):
        return type("R", (), {"body": json.dumps(payload).encode("utf-8"), "status": 200})()

    def test_happy_path_posts_multipart_with_unsafe_policy(self):
        calls = []

        def api(url, **kw):
            calls.append(kw)
            return self._fake_response({"ok": True, "result": {"message_id": 42}})

        res = ts.send_document(self._cfg(), self.tmp.name, caption="the spec", api=api)
        self.assertEqual(res["result"]["message_id"], 42)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertIs(calls[0]["policy"], th.UNSAFE)
        self.assertIn("multipart/form-data; boundary=", calls[0]["headers"]["Content-Type"])
        basename = os.path.basename(self.tmp.name)
        self.assertIn(f'name="document"; filename="{basename}"'.encode(), calls[0]["data"])
        self.assertIn(b'name="caption"', calls[0]["data"])

    def test_api_rejection_raises_telegram_api_error(self):
        def api(url, **kw):
            return self._fake_response({"ok": False, "description": "chat not found"})

        with self.assertRaises(ts.TelegramAPIError):
            ts.send_document(self._cfg(), self.tmp.name, api=api)

    def test_caption_over_1024_chars_refuses_before_any_network_call(self):
        calls = []

        def api(url, **kw):
            calls.append(kw)
            return self._fake_response({"ok": True, "result": {}})

        with self.assertRaises(ValueError):
            ts.send_document(self._cfg(), self.tmp.name, caption="x" * 1025, api=api)
        self.assertEqual(calls, [], "a local refusal must never touch the network")

    def test_missing_token_refuses_before_any_network_call(self):
        calls = []

        def api(url, **kw):
            calls.append(kw)
            return self._fake_response({"ok": True, "result": {}})

        c = {"token": None, "chat_id": "999", "api_base": "https://api.telegram.org"}
        with self.assertRaises(RuntimeError):
            ts.send_document(c, self.tmp.name, api=api)
        self.assertEqual(calls, [])

    def test_oversized_document_refuses_before_any_network_call(self):
        calls = []

        def api(url, **kw):
            calls.append(kw)
            return self._fake_response({"ok": True, "result": {}})

        with mock.patch.object(ts, "MAX_DOCUMENT_BYTES", 1):
            with self.assertRaises(ValueError) as ctx:
                ts.send_document(self._cfg(), self.tmp.name, api=api)
        self.assertIn("50 MB", str(ctx.exception))
        self.assertEqual(calls, [], "a local refusal must never touch the network")


class DocumentCliTest(unittest.TestCase):
    """`main()`'s `--document` dispatch — mirrors `--photo`'s own CLI shape exactly: mutually
    exclusive with `--text`/`--text-file`, no chat id required beyond the usual, `--dry-run` builds
    nothing and touches no network."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
        self.tmp.write(b"# a spec\n")
        self.tmp.close()
        self.addCleanup(os.remove, self.tmp.name)
        env_patch = mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:abc",
                                                  "TELEGRAM_CHAT_ID": "999"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run_capturing(self, argv):
        with mock.patch.object(sys, "argv", ["telegram_send.py"] + argv), \
             mock.patch("builtins.print") as mock_print:
            rc = ts.main()
        return rc, json.loads(mock_print.call_args[0][0])

    def test_document_and_text_are_mutually_exclusive(self):
        rc, out = self._run_capturing(["--document", self.tmp.name, "--text", "hi"])
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])

    def test_document_and_photo_are_mutually_exclusive(self):
        rc, out = self._run_capturing(["--document", self.tmp.name, "--photo", self.tmp.name])
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])

    def test_caption_over_cap_refused_before_dry_run_or_network(self):
        rc, out = self._run_capturing(["--document", self.tmp.name, "--caption", "x" * 1025])
        self.assertEqual(rc, 2)
        self.assertIn("1024", out["error"])

    def test_dry_run_reports_document_and_touches_no_network(self):
        with mock.patch.object(ts, "send_document") as send:
            rc, out = self._run_capturing(["--dry-run", "--document", self.tmp.name,
                                           "--caption", "the spec"])
        self.assertEqual(rc, 0)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["document"], self.tmp.name)
        self.assertEqual(out["caption"], "the spec")
        send.assert_not_called()

    def test_successful_send_reports_message_id(self):
        with mock.patch.object(ts, "send_document",
                               return_value={"result": {"message_id": 7}}) as send:
            rc, out = self._run_capturing(["--document", self.tmp.name, "--caption", "the spec"])
        self.assertEqual(rc, 0)
        self.assertTrue(out["sent"])
        self.assertEqual(out["message_id"], 7)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], self.tmp.name)
        self.assertEqual(send.call_args.kwargs["caption"], "the spec")

    def test_send_failure_is_reported_not_raised(self):
        with mock.patch.object(ts, "send_document", side_effect=RuntimeError("boom")):
            rc, out = self._run_capturing(["--document", self.tmp.name])
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("boom", out["error"])


if __name__ == "__main__":
    unittest.main()
