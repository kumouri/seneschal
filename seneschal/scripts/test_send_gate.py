#!/usr/bin/env python3
"""`send_gate.py` — the outbound approval gate.

What these pin, and why each is the mechanism rather than a nicety:

* **Owner passes without a store read.** A send to the owner is act-low; the gate must never be
  able to slow a reminder, a picker or a job push — so for `owner` it does not even open the file
  (a corrupt store still lets an owner send through). Who the owner is comes from identity config
  (`_owner_fixture` pins a throwaway identity per test).
* **Everything else is fail-CLOSED.** No approval → refused, exit 3, nothing sent. A corrupt store →
  refused. A gate bug (simulated by a raising loader) → refused, never a pass. `unknown` is not owner.
* **ONE store.** Approvals are rows in `pending-approvals.json` with `status: "approved"`; there is
  no second file anywhere in this module (asserted on the temp dir after every verb).
* **Single-use and exact-target.** A held draft approved for `dana@example.com` covers a send to Dana
  (with the owner cc'd), does not cover a send to Bob, does not cover Dana + Bob, and is spent on the
  way through (`gate_consumed_at`) — an approval is the pair, not the id alone.
* **Standing grants** cover their recipient on every send, are never consumed, expire if given an
  expiry, and are the one door a pre-cleared recipient walks through.
* **Purpose grants** cover only a send the caller labels with that purpose, and never widen an
  unlabelled one.
* **`grant` refuses without a reason**; `check` never consumes.

Run: ``python -m unittest test_send_gate``
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import _owner_fixture as fx  # noqa: E402
import pending_approvals as pa  # noqa: E402
import send_gate as sg  # noqa: E402
import send_recipients  # noqa: E402
import stateio  # noqa: E402

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
OWNER = fx.OWNER


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = fx.use_fixture_owner(self)

    def hold(self, **fields):
        base = {"kind": "email", "channelRef": "proton:x", "summary": "s", "bodyPreview": "b",
                "body": "hi", "sources": []}
        base.update(fields)
        return pa.add(base, now=NOW)

    def approve(self, entry_id):
        return pa.resolve(entry_id, "approved")

    def files(self):
        return sorted(os.listdir(self.dir))


class OwnerPasses(Base):
    def test_owner_class_is_allowed_without_reading_the_store(self):
        with open(os.path.join(self.dir, pa.FILENAME), "w", encoding="utf-8") as fh:
            fh.write("{not json")  # a corrupt store must not matter for an owner send
        v = sg.require_approval("email", OWNER, recipient_class="owner")
        self.assertTrue(v["allowed"])
        self.assertEqual(v["reason"], "owner")

    def test_owner_is_derived_from_the_address_when_no_class_is_passed(self):
        v = sg.require_approval("email", f"{OWNER}, {fx.OWNER_ALT}")
        self.assertTrue(v["allowed"])
        self.assertEqual(v["recipient_class"], "owner")

    def test_the_assistants_own_address_is_self(self):
        v = sg.require_approval("email", [OWNER, fx.ASSISTANT])
        self.assertTrue(v["allowed"])
        self.assertEqual(v["reason"], "owner")

    def test_an_owner_cc_needs_no_cover_on_a_third_party_send(self):
        self.assertEqual(sg._third_party_targets("email", ["dana@example.com", OWNER, fx.OWNER_ALT]),
                         ["dana@example.com"])

    def test_single_recipient_channel_with_no_override_is_owner(self):
        v = sg.require_approval("sms", None)
        self.assertTrue(v["allowed"])
        self.assertEqual(v["reason"], "owner")


class FailClosed(Base):
    def test_third_party_with_no_approval_is_refused(self):
        v = sg.require_approval("email", "dana@example.com")
        self.assertFalse(v["allowed"])
        self.assertEqual(v["reason"], "no-approval")
        self.assertIn("grant", sg.refusal_line(v))
        self.assertIn("dana@example.com", sg.refusal_line(v))

    def test_unknown_is_not_owner(self):
        v = sg.require_approval("sms", "+15550001111", recipient_class="unknown")
        self.assertFalse(v["allowed"])

    def test_corrupt_store_refuses_a_third_party(self):
        with open(os.path.join(self.dir, pa.FILENAME), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        v = sg.require_approval("email", "dana@example.com")
        self.assertFalse(v["allowed"])
        self.assertEqual(v["reason"], "store-corrupt")

    def test_a_gate_bug_is_a_refusal_never_a_pass(self):
        with mock.patch.object(pa, "load", side_effect=RuntimeError("boom")):
            v = sg.require_approval("email", "dana@example.com")
        self.assertFalse(v["allowed"])
        self.assertEqual(v["reason"], "gate-error")

    def test_pending_is_not_approved(self):
        self.hold(to="dana@example.com")
        v = sg.require_approval("email", "dana@example.com")
        self.assertFalse(v["allowed"])

    def test_unknown_kind_is_refused(self):
        v = sg.require_approval("carrier-pigeon", "dana@example.com", recipient_class="third_party")
        self.assertFalse(v["allowed"])
        self.assertEqual(v["reason"], "gate-error")

    def test_refusal_payload_shape(self):
        v = sg.require_approval("email", "dana@example.com")
        p = sg.refusal_payload(v, to=["dana@example.com"])
        self.assertFalse(p["ok"])
        self.assertEqual(p["refused"], "send_gate")
        self.assertIn("Nothing was sent", p["error"])


class SingleUseExactTarget(Base):
    def test_approved_draft_covers_its_recipient_and_is_consumed(self):
        e = self.hold(to="dana@example.com")
        self.approve(e["id"])
        v = sg.require_approval("email", ["dana@example.com", OWNER], channel="proton_email", now=NOW)
        self.assertTrue(v["allowed"])
        self.assertEqual(v["reason"], "approved")
        self.assertEqual(v["approval_id"], e["id"])
        row = pa.find(e["id"])
        self.assertEqual(row["status"], "approved")  # the caller still resolves sent/failed
        self.assertEqual(row["gate_consumed_at"], "2026-09-16T12:00:00Z")
        self.assertEqual(row["gate_channel"], "proton_email")
        # spent: the very same send again is refused
        v2 = sg.require_approval("email", "dana@example.com")
        self.assertFalse(v2["allowed"])

    def test_a_different_recipient_is_not_covered(self):
        e = self.hold(to="dana@example.com")
        self.approve(e["id"])
        v = sg.require_approval("email", "bob@example.com")
        self.assertFalse(v["allowed"])
        self.assertIsNone(pa.find(e["id"]).get("gate_consumed_at"))  # untouched

    def test_a_wider_send_is_not_covered(self):
        e = self.hold(to="dana@example.com")
        self.approve(e["id"])
        v = sg.require_approval("email", "dana@example.com, bob@example.com")
        self.assertFalse(v["allowed"])

    def test_a_row_naming_both_covers_both(self):
        e = self.hold(to="dana@example.com, bob@example.com")
        self.approve(e["id"])
        v = sg.require_approval("email", ["bob@example.com", "dana@example.com"])
        self.assertTrue(v["allowed"])

    def test_kind_must_match(self):
        e = self.hold(kind="slack", channelRef="slack:C123:171.2", to=None)
        self.approve(e["id"])
        self.assertTrue(sg.require_approval("slack", "C123", recipient_class="third_party")["allowed"])
        e2 = self.hold(kind="slack", channelRef="slack:C123", to=None)
        self.approve(e2["id"])
        self.assertFalse(sg.require_approval("email", "c123", recipient_class="third_party")["allowed"])

    def test_oldest_approval_is_spent_first(self):
        a = self.hold(to="dana@example.com")
        b = self.hold(to="dana@example.com")
        self.approve(a["id"]); self.approve(b["id"])
        v = sg.require_approval("email", "dana@example.com")
        self.assertEqual(v["approval_id"], a["id"])

    def test_draft_id_gates_gmail_send_draft(self):
        e = self.hold(to="dana@example.com", draftId="r-77")
        self.approve(e["id"])
        v = sg.require_approval("email", "r-77", recipient_class="unknown")
        self.assertTrue(v["allowed"])

    def test_check_never_consumes(self):
        e = self.hold(to="dana@example.com")
        self.approve(e["id"])
        v = sg.require_approval("email", "dana@example.com", consume=False)
        self.assertTrue(v["allowed"])
        self.assertIsNone(pa.find(e["id"]).get("gate_consumed_at"))


class StandingGrants(Base):
    def test_standing_grant_covers_every_send_and_is_never_consumed(self):
        row = sg.grant("email", "Accountant@Firm.example", why="monthly statement pre-cleared",
                       standing=True, now=NOW)
        self.assertEqual(row["status"], "approved")
        self.assertTrue(row["standing"])
        for _ in range(3):
            v = sg.require_approval("email", ["accountant@firm.example", OWNER])
            self.assertTrue(v["allowed"])
            self.assertEqual(v["reason"], "standing")
            self.assertEqual(v["approval_id"], row["id"])
        self.assertIsNone(pa.find(row["id"]).get("gate_consumed_at"))

    def test_standing_plus_single_use_cover_a_mixed_send(self):
        s = sg.grant("email", "t@firm.example", why="pre-cleared", standing=True, now=NOW)
        e = self.hold(to="bob@example.com")
        self.approve(e["id"])
        v = sg.require_approval("email", "t@firm.example, bob@example.com")
        self.assertTrue(v["allowed"])
        self.assertEqual(v["reason"], "approved")
        self.assertEqual(sorted(v["approval_ids"]), sorted([s["id"], e["id"]]))

    def test_expired_grant_does_not_cover(self):
        sg.grant("email", "t@firm.example", why="one week", standing=True, expires_days=7, now=NOW)
        self.assertTrue(sg.require_approval("email", "t@firm.example", now=NOW + timedelta(days=6))["allowed"])
        self.assertFalse(sg.require_approval("email", "t@firm.example", now=NOW + timedelta(days=8))["allowed"])

    def test_revoked_grant_does_not_cover(self):
        row = sg.grant("email", "t@firm.example", why="pre-cleared", standing=True, now=NOW)
        sg.revoke(row["id"], why="the owner withdrew it", now=NOW)
        self.assertFalse(sg.require_approval("email", "t@firm.example")["allowed"])
        self.assertEqual(pa.find(row["id"])["status"], "revoked")

    def test_grant_refuses_without_a_reason(self):
        with self.assertRaises(ValueError):
            sg.grant("email", "t@firm.example", why="  ")

    def test_single_use_grant_is_spent(self):
        sg.grant("calendar", "evt_123", why="the owner said delete it", now=NOW)
        self.assertTrue(sg.require_approval("calendar", "evt_123", recipient_class="unknown")["allowed"])
        self.assertFalse(sg.require_approval("calendar", "evt_123", recipient_class="unknown")["allowed"])


class PurposeGrants(Base):
    """A purpose-scoped standing row (`purpose`, `to: "*"`) covers any recipient of a
    send the CALLER labels with that purpose, is never spent, and is invisible to the ordinary
    recipient match — so it can never widen an unlabelled send."""

    def test_purpose_grant_covers_any_recipient_of_a_labelled_send(self):
        row = sg.grant("slack", why="announcements, too", standing=True, purpose="release-notes", now=NOW)
        self.assertEqual(row["to"], sg.PURPOSE_ANY)
        self.assertEqual(row["purpose"], "release-notes")
        self.assertTrue(row["standing"])
        for chan in ("C1", "D0DM", "C_ANY"):
            v = sg.require_approval("slack", chan, recipient_class="third_party", purpose="release-notes")
            self.assertTrue(v["allowed"], chan)
            self.assertEqual(v["reason"], "purpose")
            self.assertEqual(v["approval_id"], row["id"])
            self.assertEqual(v["purpose"], "release-notes")
        self.assertIsNone(pa.find(row["id"]).get("gate_consumed_at"))

    def test_unlabelled_send_is_not_covered_by_a_purpose_grant(self):
        sg.grant("slack", why="announcements, too", standing=True, purpose="release-notes", now=NOW)
        v = sg.require_approval("slack", "C1", recipient_class="third_party")
        self.assertFalse(v["allowed"])
        self.assertEqual(v["reason"], "no-approval")

    def test_a_different_purpose_is_not_covered(self):
        sg.grant("slack", why="x", standing=True, purpose="release-notes", now=NOW)
        self.assertFalse(sg.require_approval("slack", "C1", recipient_class="third_party",
                                             purpose="something-else")["allowed"])

    def test_kind_must_match(self):
        sg.grant("slack", why="x", standing=True, purpose="release-notes", now=NOW)
        self.assertFalse(sg.require_approval("email", "bob@example.com", purpose="release-notes")["allowed"])

    def test_labelled_send_still_passes_on_an_ordinary_approval(self):
        e = self.hold(kind="slack", channelRef="slack:C1:1.2")
        self.approve(e["id"])
        v = sg.require_approval("slack", "C1", recipient_class="third_party", purpose="release-notes")
        self.assertTrue(v["allowed"])
        self.assertEqual(v["reason"], "approved")

    def test_expiry_and_revocation_apply(self):
        row = sg.grant("slack", why="x", standing=True, purpose="release-notes",
                       expires_at="2026-10-03T05:00:00Z", now=NOW)
        self.assertEqual(row["expires_at"], "2026-10-03T05:00:00Z")
        before = datetime(2026, 10, 2, 23, 0, tzinfo=timezone.utc)
        after = datetime(2026, 10, 3, 5, 0, tzinfo=timezone.utc)
        self.assertTrue(sg.require_approval("slack", "C1", recipient_class="third_party",
                                            purpose="release-notes", now=before)["allowed"])
        self.assertFalse(sg.require_approval("slack", "C1", recipient_class="third_party",
                                             purpose="release-notes", now=after)["allowed"])
        sg.revoke(row["id"], why="release is out", now=NOW)
        self.assertFalse(sg.require_approval("slack", "C1", recipient_class="third_party",
                                             purpose="release-notes", now=before)["allowed"])

    def test_purpose_grant_shape_is_refused_when_wrong(self):
        with self.assertRaises(ValueError):  # must be standing
            sg.grant("slack", why="x", purpose="release-notes", now=NOW)
        with self.assertRaises(ValueError):  # no recipient on a purpose grant
            sg.grant("slack", "C1", why="x", standing=True, purpose="release-notes", now=NOW)
        with self.assertRaises(ValueError):  # slug shape
            sg.grant("slack", why="x", standing=True, purpose="Jam Access!", now=NOW)
        with self.assertRaises(ValueError):  # a bad purpose on the check side too
            sg.evaluate("slack", "C1", recipient_class="third_party", purpose="Bad Slug")
        self.assertFalse(sg.require_approval("slack", "C1", recipient_class="third_party",
                                             purpose="Bad Slug")["allowed"])

    def test_expires_at_accepts_a_bare_date_and_refuses_junk(self):
        self.assertEqual(sg.parse_expiry("2026-10-03"), "2026-10-03T00:00:00Z")
        self.assertEqual(sg.parse_expiry("2026-10-03T05:00:00Z"), "2026-10-03T05:00:00Z")
        with self.assertRaises(ValueError):
            sg.parse_expiry("next friday")
        with self.assertRaises(ValueError):
            sg.grant("slack", "C1", why="x", expires_days=1, expires_at="2026-10-03", now=NOW)

    def test_list_shows_the_purpose(self):
        sg.grant("slack", why="x", standing=True, purpose="release-notes", now=NOW)
        rows = sg.list_grants(standing_only=True)
        self.assertEqual([(r["purpose"], r["recipients"]) for r in rows], [("release-notes", ["*"])])


class OneStore(Base):
    def test_no_second_file_is_ever_minted(self):
        sg.grant("email", "t@firm.example", why="x", standing=True, now=NOW)
        e = self.hold(to="dana@example.com"); self.approve(e["id"])
        sg.require_approval("email", "dana@example.com")
        sg.require_approval("email", "nobody@example.com")
        sg.list_grants()
        self.assertEqual(self.files(), [pa.FILENAME])

    def test_store_schema_is_stamped(self):
        sg.grant("email", "t@firm.example", why="x", now=NOW)
        with open(os.path.join(self.dir, pa.FILENAME), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["schema"], pa.SCHEMA)


class Cli(Base):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            rc = sg.main(["--state-dir", self.dir] + argv)
        return rc, out.getvalue(), err.getvalue()

    def test_check_exit_codes(self):
        rc, _, err = self._run(["check", "--kind", "email", "--recipient", "dana@example.com"])
        self.assertEqual(rc, sg.EXIT_REFUSED)
        self.assertIn("REFUSED", err)
        rc, _, _ = self._run(["check", "--kind", "email", "--recipient", OWNER])
        self.assertEqual(rc, 0)

    def test_grant_then_check_then_list(self):
        rc, out, _ = self._run(["grant", "--kind", "email", "--recipient", "t@firm.example",
                                "--why", "pre-cleared", "--standing"])
        self.assertEqual(rc, 0)
        gid = json.loads(out)["id"]
        rc, _, _ = self._run(["check", "--kind", "email", "--recipient", "t@firm.example"])
        self.assertEqual(rc, 0)
        rc, out, _ = self._run(["list", "--standing"])
        self.assertEqual([r["id"] for r in json.loads(out)], [gid])

    def test_grant_without_why_is_usage_error(self):
        with self.assertRaises(SystemExit):
            self._run(["grant", "--kind", "email", "--recipient", "x@y"])

    def test_purpose_grant_via_cli_then_labelled_check(self):
        rc, out, _ = self._run(["grant", "--kind", "slack", "--purpose", "release-notes", "--standing",
                                "--why", "replies, too", "--expires-at", "2026-10-03T05:00:00Z"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["purpose"], "release-notes")
        rc, _, _ = self._run(["check", "--kind", "slack", "--recipient", "C1", "--class", "third_party"])
        self.assertEqual(rc, sg.EXIT_REFUSED)
        rc, out, _ = self._run(["check", "--kind", "slack", "--recipient", "C1", "--class", "third_party",
                                "--purpose", "release-notes"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["reason"], "purpose")

    def test_grant_needs_a_recipient_or_a_purpose(self):
        rc, _, err = self._run(["grant", "--kind", "slack", "--why", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("--recipient", err)


class BlastRadius(Base):
    def test_reads_ledger_rows_and_flags_non_owner(self):
        path = os.path.join(self.dir, send_recipients.FILENAME)
        stateio.append_jsonl(path, {"at": "2026-09-15T20:55:57Z", "channel": "proton_email", "recipient_class": "third_party"})
        stateio.append_jsonl(path, {"at": "2026-09-15T21:00:00Z", "channel": "push_call", "recipient_class": "owner"})
        stateio.append_jsonl(path, {"at": "2026-09-16T10:00:00Z", "channel": "gmail_send", "recipient_class": "third_party",
                                    "gate": {"allowed": True, "reason": "approved", "approval_id": "a9"}})
        stateio.append_jsonl(path, {"at": "2026-08-01T10:00:00Z", "channel": "proton_email", "recipient_class": "third_party"})
        rep = sg.blast_radius(days=7, now=NOW)
        self.assertEqual(rep["rows"], 3)
        self.assertEqual(rep["owner"], 1)
        self.assertEqual(rep["non_owner"], 2)
        blocked = [r for r in rep["non_owner_rows"] if r["would_block"]]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["channel"], "proton_email")


if __name__ == "__main__":
    unittest.main()
