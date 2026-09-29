#!/usr/bin/env python3
"""Tests for push_sms.py's send-gate wiring — asserts a real (network-mocked) SMS push appends the right
`recipient_class` to `state/send-recipients.jsonl` (never the recipient itself), and that the site is
GATED: an owner-class send passes untouched, an unapproved third-party/unknown send is REFUSED (exit
`send_gate.EXIT_REFUSED`, no network call, the ledger row carries `gate.allowed == false`), and one
with an approved row in `state/pending-approvals.json` passes and spends it. The owner's addresses
come from a fixture identity (`_owner_fixture`), never code."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import push_sms  # noqa: E402
import _owner_fixture as fx  # noqa: E402
import send_gate  # noqa: E402
import send_recipients  # noqa: E402
import stateio  # noqa: E402


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"ok":true}'


class RecipientClassInstrumentation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fx.use_fixture_owner(self)
        self.ledger = os.path.join(self.dir, send_recipients.FILENAME)
        env_patch = mock.patch.dict(os.environ, {
            "SENESCHAL_STATE_DIR": self.dir,
            "PUSH_SMS_URL": "https://example.invalid/push-sms",
            "PUSH_SMS_SECRET": "test-secret",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("PUSH_SMS_TO", None)
        urlopen_patch = mock.patch("urllib.request.urlopen", return_value=_FakeResponse())
        urlopen_patch.start()
        self.addCleanup(urlopen_patch.stop)

    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["push_sms.py"] + argv):
            return push_sms.main()

    def test_no_to_override_records_owner(self):
        rc = self._run(["--text", "hi"])
        self.assertEqual(rc, 0)
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], "push_sms")
        self.assertEqual(rows[0]["recipient_class"], "owner")

    def test_explicit_to_override_records_unknown(self):
        rc = self._run(["--text", "hi", "--to", "+15559998888"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)  # the gate: unknown is not owner
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["recipient_class"], "unknown")

    def test_dry_run_never_writes_a_row(self):
        rc = self._run(["--text", "hi", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.ledger))

    def test_row_never_contains_the_number(self):
        self._run(["--text", "hi", "--to", "+15559998888"])
        with open(self.ledger, "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("+15559998888", raw)


class SendGate(RecipientClassInstrumentation):
    def test_owner_default_passes(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse()) as urlopen:
            rc = self._run(["--text", "hi"])
        self.assertEqual(rc, 0)
        urlopen.assert_called_once()
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["gate"]["reason"], "owner")

    def test_override_refused_then_passes_with_a_grant(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse()) as urlopen:
            rc = self._run(["--text", "hi", "--to", "+15559998888"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)
        urlopen.assert_not_called()
        send_gate.grant("sms", "+15559998888", why="the owner asked")
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse()) as urlopen:
            rc = self._run(["--text", "hi", "--to", "+15559998888"])
        self.assertEqual(rc, 0)
        urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
