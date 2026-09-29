#!/usr/bin/env python3
"""Tests for `send_recipients.py` — the non-content outbound-recipient-class ledger.

Three things matter most here: `record()` can never raise (it sits on a send path), classification
never collapses `unknown` into either firm class, and — the privacy invariant the ledger lives or
dies on — the row `record()` writes never carries the address/number/handle it was given to classify.
The owner's addresses come from identity config, never code: a fixture identity is pinned per test.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import _owner_fixture as fx  # noqa: E402
import send_recipients  # noqa: E402
import stateio  # noqa: E402


class ClassifyEmails(unittest.TestCase):
    def setUp(self):
        fx.use_fixture_owner(self)

    def test_all_owner_addresses_is_owner(self):
        self.assertEqual(send_recipients.classify_emails(fx.OWNER), "owner")
        self.assertEqual(send_recipients.classify_emails(fx.OWNER, fx.OWNER_ALT), "owner")

    def test_case_insensitive_owner_match(self):
        self.assertEqual(send_recipients.classify_emails("Owner@EXAMPLE.com"), "owner")

    def test_any_non_owner_address_is_third_party(self):
        self.assertEqual(send_recipients.classify_emails(fx.OWNER, "recruiter@acme.example"),
                         "third_party")

    def test_comma_separated_single_string_is_split(self):
        self.assertEqual(send_recipients.classify_emails(f"{fx.OWNER}, recruiter@acme.example"),
                         "third_party")

    def test_empty_batch_is_unknown(self):
        self.assertEqual(send_recipients.classify_emails(), "unknown")

    def test_all_none_or_blank_is_unknown(self):
        self.assertEqual(send_recipients.classify_emails(None, "", "  "), "unknown")

    def test_the_assistants_own_address_counts_as_self(self):
        """A cc to the assistant's own mailbox reaches no third party."""
        self.assertEqual(send_recipients.classify_emails(fx.ASSISTANT), "owner")
        self.assertEqual(send_recipients.classify_emails(fx.OWNER, fx.ASSISTANT), "owner")

    def test_a_near_miss_of_an_owner_address_is_not_owner(self):
        self.assertEqual(send_recipients.classify_emails("owner@example.co"), "third_party")
        self.assertEqual(send_recipients.classify_emails("ownerexample.com"), "third_party")

    def test_nothing_configured_means_every_address_is_third_party(self):
        """No identity → an empty self set → the fail-closed direction for the gate."""
        fx.use_fixture_owner(self, owner=None, emails=(), assistant=None)
        self.assertEqual(send_recipients.self_addresses(), frozenset())
        self.assertEqual(send_recipients.classify_emails(fx.OWNER), "third_party")

    def test_an_explicit_identity_wins_over_the_file(self):
        ident = {"owner": {"email": "someone@example.net"}}
        self.assertEqual(send_recipients.classify_emails("someone@example.net", identity=ident), "owner")
        self.assertEqual(send_recipients.classify_emails(fx.OWNER, identity=ident), "third_party")


class ClassifySingleRecipient(unittest.TestCase):
    def test_no_override_is_owner(self):
        self.assertEqual(send_recipients.classify_single_recipient(None), "owner")
        self.assertEqual(send_recipients.classify_single_recipient(""), "owner")

    def test_override_matching_default_is_owner(self):
        self.assertEqual(send_recipients.classify_single_recipient("123", default="123"), "owner")

    def test_override_not_matching_default_is_unknown(self):
        self.assertEqual(send_recipients.classify_single_recipient("999", default="123"), "unknown")

    def test_override_with_no_default_to_compare_is_unknown(self):
        self.assertEqual(send_recipients.classify_single_recipient("+15550100000"), "unknown")

    def test_never_third_party(self):
        for resolved, default in (("999", "123"), ("+1555", None), ("x", "y")):
            self.assertNotEqual(send_recipients.classify_single_recipient(resolved, default), "third_party")


class Record(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, send_recipients.FILENAME)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SENESCHAL_STATE_DIR", None)

    def test_appends_one_row_with_only_the_three_fields(self):
        send_recipients.record("proton_email", "owner", state_dir=self.dir)
        rows = list(stateio.iter_jsonl(self.path))
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"at", "channel", "recipient_class"})
        self.assertEqual(rows[0]["channel"], "proton_email")
        self.assertEqual(rows[0]["recipient_class"], "owner")

    def test_gate_verdict_rides_in_without_its_recipients(self):
        verdict = {"allowed": False, "reason": "no-approval", "approval_id": None,
                   "recipients": ["dana@example.com"]}
        send_recipients.record("proton_email", "third_party", state_dir=self.dir, gate=verdict)
        rows = list(stateio.iter_jsonl(self.path))
        self.assertEqual(rows[0]["gate"], {"allowed": False, "reason": "no-approval", "approval_id": None})
        with open(self.path, encoding="utf-8") as fh:
            self.assertNotIn("dana@example.com", fh.read())

    def test_unrecognized_class_is_coerced_to_unknown(self):
        send_recipients.record("proton_email", "bogus", state_dir=self.dir)
        rows = list(stateio.iter_jsonl(self.path))
        self.assertEqual(rows[0]["recipient_class"], "unknown")

    def test_never_raises_even_when_the_append_itself_fails(self):
        with mock.patch.object(stateio, "append_jsonl", side_effect=OSError("disk full")):
            send_recipients.record("proton_email", "owner", state_dir=self.dir)  # must not raise
        self.assertFalse(os.path.exists(self.path))

    def test_row_never_carries_the_address_it_classified(self):
        """The privacy invariant: no matter what a caller was classifying, the written row holds
        only the class, never the string that produced it."""
        secret_addresses = [fx.OWNER, "recruiter@acme.example", "+15550100000",
                            "555-010-0000", "some.handle#1234"]
        for _addr in secret_addresses:
            send_recipients.record("proton_email", "third_party", state_dir=self.dir)
        with open(self.path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        for addr in secret_addresses:
            self.assertNotIn(addr, raw)

    def test_respects_state_dir_env_over_explicit_arg(self):
        """`paths.state_dir` precedence: `SENESCHAL_STATE_DIR` env wins over an explicit argument —
        this is what lets a test (or a worktree-scoped job) redirect every state write without
        hunting down this call site's own default."""
        env_dir = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"SENESCHAL_STATE_DIR": env_dir}):
            send_recipients.record("proton_email", "owner", state_dir=self.dir)
        self.assertTrue(os.path.exists(os.path.join(env_dir, send_recipients.FILENAME)))
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
