#!/usr/bin/env python3
"""Tests for gmail_api.py's send-gate wiring — asserts a real (network-mocked) `send`/`send-draft` appends the right
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

import gmail_api  # noqa: E402
import pending_approvals  # noqa: E402
import _owner_fixture as fx  # noqa: E402
import send_gate  # noqa: E402
import send_recipients  # noqa: E402
import stateio  # noqa: E402


class RecipientClassInstrumentation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fx.use_fixture_owner(self)
        self.ledger = os.path.join(self.dir, send_recipients.FILENAME)
        env_patch = mock.patch.dict(os.environ, {"SENESCHAL_STATE_DIR": self.dir})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        req_patch = mock.patch.object(gmail_api.gc, "authorized_request",
                                       return_value={"id": "msg1", "threadId": "t1"})
        req_patch.start()
        self.addCleanup(req_patch.stop)

    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["gmail_api.py"] + argv):
            return gmail_api.main()

    def test_send_to_owner_records_owner(self):
        rc = self._run(["send", "--account", "personal", "--to", "owner@example.com",
                         "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, 0)
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], "gmail_send")
        self.assertEqual(rows[0]["recipient_class"], "owner")

    def test_send_to_third_party_records_third_party(self):
        rc = self._run(["send", "--account", "personal", "--to", "someone@example.com",
                         "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)  # the gate: unapproved → refused, row still written
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["recipient_class"], "third_party")

    def test_send_draft_always_records_unknown(self):
        rc = self._run(["send-draft", "--account", "personal", "--id", "draft123"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)  # the gate: unknown is not owner
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["channel"], "gmail_send_draft")
        self.assertEqual(rows[0]["recipient_class"], "unknown")

    def test_non_send_command_writes_no_row(self):
        rc = self._run(["profile", "--account", "personal"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.ledger))

    def test_row_never_contains_the_address(self):
        self._run(["send", "--account", "personal", "--to", "someone@example.com",
                   "--subject", "hi", "--body", "hi"])
        with open(self.ledger, "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("someone@example.com", raw)


class SendGate(RecipientClassInstrumentation):
    def _req(self):
        return mock.patch.object(gmail_api.gc, "authorized_request", return_value={"id": "msg1", "threadId": "t1"})

    def test_unapproved_third_party_send_is_refused_before_the_api(self):
        with self._req() as req:
            rc = self._run(["send", "--account", "personal", "--to", "someone@example.com",
                             "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)
        req.assert_not_called()
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertFalse(rows[0]["gate"]["allowed"])

    def test_owner_send_passes(self):
        with self._req() as req:
            rc = self._run(["send", "--account", "personal", "--to", "owner@example.com",
                             "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, 0)
        req.assert_called_once()

    def test_approved_draft_passes(self):
        e = pending_approvals.add({"kind": "email", "channelRef": "gmail:t1", "to": "someone@example.com",
                                   "summary": "s", "bodyPreview": "b", "body": "hi"})
        pending_approvals.resolve(e["id"], "approved")
        with self._req() as req:
            rc = self._run(["send", "--account", "personal", "--to", "someone@example.com",
                             "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, 0)
        req.assert_called_once()

    def test_send_draft_is_gated_on_the_draft_id(self):
        with self._req() as req:
            rc = self._run(["send-draft", "--account", "personal", "--id", "draft123"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)
        req.assert_not_called()
        e = pending_approvals.add({"kind": "email", "channelRef": "gmail:t1", "to": "someone@example.com",
                                   "draftId": "draft123", "summary": "s", "bodyPreview": "b", "body": "hi"})
        pending_approvals.resolve(e["id"], "approved")
        with self._req() as req:
            rc = self._run(["send-draft", "--account", "personal", "--id", "draft123"])
        self.assertEqual(rc, 0)
        req.assert_called_once()


if __name__ == "__main__":
    unittest.main()
