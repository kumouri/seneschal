#!/usr/bin/env python3
"""Tests for proton_send.py's send-gate wiring — asserts a real (network-mocked) send appends the right
`recipient_class` to `state/send-recipients.jsonl` (never the recipient itself), and that the site is
GATED: an owner-class send passes untouched, an unapproved third-party/unknown send is REFUSED (exit
`send_gate.EXIT_REFUSED`, no network call, the ledger row carries `gate.allowed == false`), and one
with an approved row in `state/pending-approvals.json` passes and spends it. The owner's addresses
come from a fixture identity (`_owner_fixture`), never code.

Also pins `--attach`: refused locally before the gate and before any connection, and a
message with no attachment is byte-identical to one built without the feature."""
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

import pending_approvals  # noqa: E402
import proton_send  # noqa: E402
import _owner_fixture as fx  # noqa: E402
import send_gate  # noqa: E402
import send_recipients  # noqa: E402
import stateio  # noqa: E402


class _FakeSMTP:
    def send_message(self, msg, from_addr=None, to_addrs=None):
        pass

    def quit(self):
        pass


class RecipientClassInstrumentation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fx.use_fixture_owner(self)
        self.ledger = os.path.join(self.dir, send_recipients.FILENAME)
        env_patch = mock.patch.dict(os.environ, {"SENESCHAL_STATE_DIR": self.dir})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        connect_patch = mock.patch.object(proton_send, "_connect", return_value=_FakeSMTP())
        connect_patch.start()
        self.addCleanup(connect_patch.stop)

    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["proton_send.py"] + argv):
            return proton_send.main()

    def test_owner_only_recipient_records_owner(self):
        rc = self._run(["--to", "owner@example.com", "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, 0)
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], "proton_email")
        self.assertEqual(rows[0]["recipient_class"], "owner")

    def test_third_party_recipient_records_third_party(self):
        rc = self._run(["--to", "recruiter@acme.example", "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)  # the gate: unapproved → refused, row still written
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["recipient_class"], "third_party")

    def test_mixed_owner_and_third_party_cc_records_third_party(self):
        rc = self._run(["--to", "owner@example.com", "--cc", "recruiter@acme.example",
                         "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["recipient_class"], "third_party")

    def test_dry_run_never_writes_a_row(self):
        rc = self._run(["--dry-run", "--to", "recruiter@acme.example", "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.ledger))

    def test_row_never_contains_the_address(self):
        self._run(["--to", "recruiter@acme.example", "--subject", "hi", "--body", "hi"])
        with open(self.ledger, "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("recruiter@acme.example", raw)


class SendGate(RecipientClassInstrumentation):
    def _connect_mock(self):
        m = mock.patch.object(proton_send, "_connect", return_value=_FakeSMTP())
        return m

    def test_unapproved_third_party_is_refused_before_smtp(self):
        with self._connect_mock() as connect:
            rc = self._run(["--to", "recruiter@acme.example", "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)
        connect.assert_not_called()
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertFalse(rows[0]["gate"]["allowed"])
        self.assertEqual(rows[0]["gate"]["reason"], "no-approval")

    def test_owner_passes_untouched(self):
        with self._connect_mock() as connect:
            rc = self._run(["--to", "owner@example.com", "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, 0)
        connect.assert_called_once()
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["gate"], {"allowed": True, "reason": "owner", "approval_id": None})

    def test_approved_draft_passes_and_is_spent(self):
        e = pending_approvals.add({"kind": "email", "channelRef": "proton:1", "to": "recruiter@acme.example",
                                   "summary": "s", "bodyPreview": "b", "body": "hi"})
        pending_approvals.resolve(e["id"], "approved")
        with self._connect_mock() as connect:
            rc = self._run(["--to", "recruiter@acme.example", "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, 0)
        connect.assert_called_once()
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["gate"]["approval_id"], e["id"])
        self.assertIsNotNone(pending_approvals.find(e["id"]).get("gate_consumed_at"))
        with self._connect_mock() as connect:
            rc = self._run(["--to", "recruiter@acme.example", "--subject", "hi", "--body", "hi"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)  # single-use
        connect.assert_not_called()


class LoadAttachmentsTests(unittest.TestCase):
    """`proton_send.load_attachments` — resolved and refused BEFORE send_gate and before any
    connection, per its own docstring."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fx.use_fixture_owner(self)

    def _write(self, name, content=b"hi"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def test_missing_path_raises(self):
        with self.assertRaises(proton_send.AttachmentError):
            proton_send.load_attachments([os.path.join(self.dir, "nope.txt")])

    def test_guesses_mime_type_and_falls_back_to_octet_stream(self):
        txt = self._write("notes.txt", b"hello")
        blob = self._write("blob.bin", b"\x00\x01")
        attachments = proton_send.load_attachments([txt, blob])
        by_name = {a["name"]: a for a in attachments}
        self.assertEqual(by_name["notes.txt"]["mime"], "text/plain")
        self.assertEqual(by_name["blob.bin"]["mime"], "application/octet-stream")
        self.assertEqual(by_name["notes.txt"]["size"], 5)

    def test_total_size_over_cap_raises(self):
        big = self._write("big.bin", b"x" * 100)
        with mock.patch.object(proton_send, "MAX_ATTACHMENT_BYTES", 50):
            with self.assertRaises(proton_send.AttachmentError) as ctx:
                proton_send.load_attachments([big])
        self.assertIn("50", str(ctx.exception))  # the patched cap, named in the error


class BuildMessageAttachmentShapeTests(unittest.TestCase):
    """No `--attach` must build the exact same message shape as before attachments existed; one or
    more attachments wrap the existing alternative(text/html) part in an outer `multipart/mixed`."""

    def test_no_attachments_message_shape_is_unchanged(self):
        msg = proton_send.build_message(["a@x.com"], [], "s", "body", None, "assistant@example.com")
        self.assertEqual(msg.get_content_type(), "text/plain")

    def test_no_attachments_html_message_shape_is_unchanged(self):
        msg = proton_send.build_message(["a@x.com"], [], "s", "body", "<p>hi</p>", "assistant@example.com")
        self.assertEqual(msg.get_content_type(), "multipart/alternative")

    def test_attachment_wraps_plain_body_in_multipart_mixed(self):
        att = [{"name": "spec.md", "data": b"# hi", "mime": "text/markdown", "size": 4}]
        msg = proton_send.build_message(["a@x.com"], [], "s", "body", None, "assistant@example.com",
                                        attachments=att)
        self.assertEqual(msg.get_content_type(), "multipart/mixed")
        parts = msg.get_payload()
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].get_content_type(), "text/plain")
        self.assertEqual(parts[1].get_filename(), "spec.md")
        self.assertEqual(parts[1].get_content_type(), "text/markdown")

    def test_attachment_wraps_alternative_body_inside_mixed(self):
        att = [{"name": "spec.md", "data": b"# hi", "mime": "text/markdown", "size": 4}]
        msg = proton_send.build_message(["a@x.com"], [], "s", "body", "<p>hi</p>", "assistant@example.com",
                                        attachments=att)
        self.assertEqual(msg.get_content_type(), "multipart/mixed")
        parts = msg.get_payload()
        self.assertEqual(parts[0].get_content_type(), "multipart/alternative")
        self.assertEqual(parts[1].get_filename(), "spec.md")

    def test_multiple_attachments_all_land(self):
        att = [{"name": "a.txt", "data": b"a", "mime": "text/plain", "size": 1},
               {"name": "b.bin", "data": b"b", "mime": "application/octet-stream", "size": 1}]
        msg = proton_send.build_message(["a@x.com"], [], "s", "body", None, "assistant@example.com",
                                        attachments=att)
        parts = msg.get_payload()
        self.assertEqual([p.get_filename() for p in parts[1:]], ["a.txt", "b.bin"])


class AttachmentCliTests(unittest.TestCase):
    """End-to-end `--attach` behaviour through `main()`: refusal before any connection, dry-run
    listing, and the success JSON naming what was attached."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fx.use_fixture_owner(self)
        env_patch = mock.patch.dict(os.environ, {"SENESCHAL_STATE_DIR": self.dir})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        connect_patch = mock.patch.object(proton_send, "_connect", return_value=_FakeSMTP())
        self.connect_mock = connect_patch.start()
        self.addCleanup(connect_patch.stop)

    def _write(self, name, content=b"hello attachment"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["proton_send.py"] + argv):
            return proton_send.main()

    def _run_capturing(self, argv):
        with mock.patch("builtins.print") as mock_print:
            rc = self._run(argv)
        return rc, json.loads(mock_print.call_args[0][0])

    def test_missing_attachment_is_refused_before_any_connection(self):
        rc, out = self._run_capturing(
            ["--to", "owner@example.com", "--subject", "s", "--body", "b",
             "--attach", os.path.join(self.dir, "nope.txt")])
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])
        self.connect_mock.assert_not_called()

    def test_oversized_total_is_refused_before_any_connection(self):
        big = self._write("big.bin", b"x" * 10)
        with mock.patch.object(proton_send, "MAX_ATTACHMENT_BYTES", 5):
            rc, out = self._run_capturing(
                ["--to", "owner@example.com", "--subject", "s", "--body", "b",
                 "--attach", big])
        self.assertEqual(rc, 2)
        self.assertIn("20 MB", out["error"])
        self.connect_mock.assert_not_called()

    def test_dry_run_lists_attachment_name_size_and_mime(self):
        path = self._write("spec.md", b"# hello")
        rc, out = self._run_capturing(
            ["--dry-run", "--to", "t@x.com", "--subject", "s", "--body", "b", "--attach", path])
        self.assertEqual(rc, 0)
        self.assertEqual(out["attachments"], [{"name": "spec.md", "size": 7, "mime": "text/markdown"}])

    def test_dry_run_with_no_attachments_reports_empty_list(self):
        rc, out = self._run_capturing(
            ["--dry-run", "--to", "t@x.com", "--subject", "s", "--body", "b"])
        self.assertEqual(rc, 0)
        self.assertEqual(out["attachments"], [])

    def test_success_json_reports_attachment_names(self):
        path = self._write("spec.md", b"# hello")
        rc, out = self._run_capturing(
            ["--to", "owner@example.com", "--subject", "s", "--body", "b",
             "--attach", path])
        self.assertEqual(rc, 0)
        self.assertEqual(out["attachments"], ["spec.md"])
        self.connect_mock.assert_called_once()

    def test_success_json_with_no_attachments_has_no_attachments_key(self):
        rc, out = self._run_capturing(
            ["--to", "owner@example.com", "--subject", "s", "--body", "b"])
        self.assertEqual(rc, 0)
        self.assertNotIn("attachments", out)


if __name__ == "__main__":
    unittest.main()
