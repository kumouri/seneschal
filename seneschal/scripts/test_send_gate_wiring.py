#!/usr/bin/env python3
"""The send gate's structural check — the refusal that stops a new outbound script (or a rewrite of
an existing one) from skipping the gate silently — plus the `PreToolUse` hook for the MCP send
tools.

Two structural rules, both read from SOURCE TEXT:

1. Every script in `OUTBOUND_SCRIPTS` imports `send_gate` and calls `send_gate.require_approval(`
   BEFORE its first network call (the marker per script is the function that actually talks to the
   wire).
2. Every non-test module in this directory that calls `send_recipients.record(` (the send ledger)
   also calls `send_gate.require_approval(` — a new outbound site that instruments but does not gate
   fails here by construction.

The hook (`send_gate_hook.py`): matched tools are Slack `slack_send_message`/`slack_schedule_message`
and Gmail `send_message`/`reply`/`forward`; fail CLOSED on a matched tool (exit 2, reason on stderr),
fail OPEN on everything else and on an unreadable event; an owner-only Gmail send passes; an approved
held draft is spent on the way through; a purpose label comes only from a detector.

Run: ``python -m unittest test_send_gate_wiring``
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import _owner_fixture as fx  # noqa: E402
import pending_approvals as pa  # noqa: E402
import send_gate as sg  # noqa: E402
import send_gate_hook as hook  # noqa: E402

#: script → the substring that is its first real network call. `require_approval(` must precede it.
OUTBOUND_SCRIPTS = {
    "proton_send.py": "server.send_message(",
    "gmail_api.py": "f\"{API}/drafts/send\"",
    "gcal_api.py": "\"events.insert\"",
    "push_sms.py": "urllib.request.urlopen(",
    "push_call.py": "urllib.request.urlopen(",
    "discord_send.py": "api_request(c, \"POST\"",
}

GATE_CALL = "send_gate.require_approval("
IMPORT_RE = re.compile(r"^\s*import send_gate\b", re.MULTILINE)


def _src(name: str) -> str:
    with open(os.path.join(SCRIPT_DIR, name), encoding="utf-8") as fh:
        return fh.read()


class EveryChokepointIsGated(unittest.TestCase):
    def test_each_named_script_imports_and_calls_the_gate_before_its_network_call(self):
        for name, marker in OUTBOUND_SCRIPTS.items():
            with self.subTest(script=name):
                src = _src(name)
                self.assertRegex(src, IMPORT_RE, f"{name} does not import send_gate")
                gate_at = src.find(GATE_CALL)
                self.assertNotEqual(gate_at, -1, f"{name} never calls {GATE_CALL}")
                net_at = src.find(marker)
                self.assertNotEqual(net_at, -1, f"{name}: network marker {marker!r} not found — update this test")
                self.assertLess(gate_at, net_at, f"{name} calls the gate AFTER its network call")

    def test_every_module_that_instruments_also_gates(self):
        exempt = {"send_recipients.py", "send_gate.py", "send_gate_hook.py"}
        for name in sorted(os.listdir(SCRIPT_DIR)):
            if not name.endswith(".py") or name.startswith("test_") or name in exempt:
                continue
            src = _src(name)
            if "send_recipients.record(" not in src:
                continue
            with self.subTest(script=name):
                self.assertIn(GATE_CALL, src,
                              f"{name} calls send_recipients.record but never send_gate.require_approval "
                              f"— a seventh outbound site skipping the gate")

    def test_each_named_script_returns_the_refusal_exit_code(self):
        for name in OUTBOUND_SCRIPTS:
            with self.subTest(script=name):
                self.assertIn("EXIT_REFUSED", _src(name))


class HookBase(unittest.TestCase):
    def setUp(self):
        self.dir = fx.use_fixture_owner(self)

    def run_hook(self, event):
        err = io.StringIO()
        rc = hook.main([], stdin=io.StringIO(json.dumps(event) if event is not None else ""),
                       stderr=err, stdout=io.StringIO())
        return rc, err.getvalue()


class HookClassifies(HookBase):
    def test_slack_send_is_the_channel(self):
        self.assertEqual(hook.classify_call("mcp__claude_ai_Slack__slack_send_message", {"channel_id": "C1", "text": "x"}),
                         ("slack", "C1", "third_party"))

    def test_gmail_send_owner_only(self):
        kind, rec, cls = hook.classify_call("mcp__claude_ai_Gmail__send_message",
                                            {"to": [fx.OWNER], "body": "x"})
        self.assertEqual((kind, cls), ("email", "owner"))

    def test_gmail_reply_uses_the_thread_id(self):
        self.assertEqual(hook.classify_call("mcp__claude_ai_Gmail__reply", {"thread_id": "t9", "body": "x"}),
                         ("email", "t9", "unknown"))

    def test_the_framework_ships_no_purpose_detectors(self):
        self.assertEqual(hook._PURPOSE_DETECTORS, ())
        self.assertIsNone(hook.detect_purpose("slack", {"channel_id": "C1", "text": "x"}))

    def test_reads_and_other_tools_are_not_matched(self):
        for name in ("mcp__claude_ai_Gmail__search_threads", "mcp__claude_ai_Slack__slack_read_channel",
                     "Bash", "mcp__notion__notion-create-pages", "mcp__claude_ai_Gmail__create_draft"):
            self.assertIsNone(hook.classify_call(name, {"to": "x@y"}), name)


class HookDecides(HookBase):
    def test_unmatched_tool_exits_0_silently(self):
        rc, err = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual((rc, err), (0, ""))

    def test_unreadable_event_exits_0(self):
        rc, _ = self.run_hook(None)
        self.assertEqual(rc, 0)
        rc = hook.main([], stdin=io.StringIO("{not json"), stderr=io.StringIO())
        self.assertEqual(rc, 0)

    def test_third_party_slack_send_without_approval_is_blocked(self):
        rc, err = self.run_hook({"tool_name": "mcp__claude_ai_Slack__slack_send_message",
                                 "tool_input": {"channel_id": "C1", "text": "hi"}})
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err)

    def test_matched_tool_with_no_recipient_is_blocked_not_waved_through(self):
        rc, _ = self.run_hook({"tool_name": "mcp__claude_ai_Slack__slack_send_message",
                               "tool_input": {"text": "hi"}})
        self.assertEqual(rc, 2)

    def test_owner_only_gmail_send_passes(self):
        rc, _ = self.run_hook({"tool_name": "mcp__claude_ai_Gmail__send_message",
                               "tool_input": {"to": fx.OWNER, "body": "hi"}})
        self.assertEqual(rc, 0)

    def test_approved_slack_draft_passes_and_is_spent(self):
        e = pa.add({"kind": "slack", "channelRef": "slack:C1:1.2", "summary": "s", "bodyPreview": "b", "body": "hi"},
                   now=datetime(2026, 9, 16, tzinfo=timezone.utc))
        pa.resolve(e["id"], "approved")
        ev = {"tool_name": "mcp__claude_ai_Slack__slack_send_message", "tool_input": {"channel_id": "C1", "text": "hi"}}
        rc, _ = self.run_hook(ev)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(pa.find(e["id"]).get("gate_consumed_at"))
        rc, _ = self.run_hook(ev)  # spent
        self.assertEqual(rc, 2)

    def test_send_message_by_draft_id_alone_is_refused_with_the_resend_fix_named(self):
        # A `send_message` call carrying only a draftId hides the real recipient from the gate, so
        # even a recipient with a STANDING grant on their actual address gets refused — the refusal
        # must say to resend with explicit to/cc, not just print a generic "no approval".
        rc, err = self.run_hook({"tool_name": "mcp__claude_ai_Gmail__send_message",
                                 "tool_input": {"draftId": "r-abc123", "body": "hi"}})
        self.assertEqual(rc, 2)
        self.assertIn("draftId", err)
        self.assertIn("r-abc123", err)
        self.assertIn("explicit", err)

    def test_send_message_by_draft_id_is_not_special_cased_once_addresses_are_explicit(self):
        e = pa.add({"kind": "email", "to": "carol@example.com", "summary": "s", "bodyPreview": "b", "body": "hi"})
        pa.resolve(e["id"], "approved")
        rc, err = self.run_hook({"tool_name": "mcp__claude_ai_Gmail__send_message",
                                 "tool_input": {"draftId": "r-abc123", "to": ["carol@example.com"], "body": "hi"}})
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_reply_by_thread_id_keeps_the_generic_refusal_not_the_draft_id_message(self):
        # reply/forward's thread-id gate is deliberate (send_gate.py's own docstring), not the
        # draft-id defect — it must keep the ordinary refusal line, never the draftId-specific one.
        rc, err = self.run_hook({"tool_name": "mcp__claude_ai_Gmail__reply",
                                 "tool_input": {"thread_id": "t9", "body": "hi"}})
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err)
        self.assertNotIn("draftId", err)

    def test_explain_never_consumes(self):
        e = pa.add({"kind": "slack", "channelRef": "slack:C1", "summary": "s", "bodyPreview": "b", "body": "hi"})
        pa.resolve(e["id"], "approved")
        out = io.StringIO()
        rc = hook.main(["--explain", json.dumps({"tool_name": "mcp__claude_ai_Slack__slack_send_message",
                                                  "tool_input": {"channel_id": "C1"}})], stdout=out)
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["allowed"])
        self.assertIsNone(pa.find(e["id"]).get("gate_consumed_at"))


class HookPurpose(HookBase):
    """The message is the marker, the detector's ledger is the proof, the purpose grant is the
    approval — and all three are needed. The framework ships no detectors, so a fake one that
    "proves" exactly one verbatim text stands in."""

    SLACK = "mcp__claude_ai_Slack__slack_send_message"
    TEXT = "release notes are up"

    def setUp(self):
        super().setUp()
        self.proven = True

        def detector(kind, tool_input, *, state_dir=None):
            text = tool_input.get("text") if isinstance(tool_input, dict) else None
            return "release-notes" if kind == "slack" and text == self.TEXT and self.proven else None

        patcher = mock.patch.object(hook, "_PURPOSE_DETECTORS", (detector,))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _grant(self):
        return sg.grant("slack", why="announcements", standing=True, purpose="release-notes",
                        expires_at="2099-01-01T00:00:00Z")

    def test_a_proven_labelled_send_passes_any_channel_and_is_never_spent(self):
        row = self._grant()
        for chan in ("C1", "D0DM"):
            rc, err = self.run_hook({"tool_name": self.SLACK, "tool_input": {"channel_id": chan, "text": self.TEXT}})
            self.assertEqual(rc, 0, err)
        self.assertIsNone(pa.find(row["id"]).get("gate_consumed_at"))

    def test_the_grant_alone_is_not_enough(self):
        self._grant()
        self.proven = False
        rc, _ = self.run_hook({"tool_name": self.SLACK, "tool_input": {"channel_id": "C1", "text": self.TEXT}})
        self.assertEqual(rc, 2)

    def test_the_proof_alone_is_not_enough(self):
        rc, err = self.run_hook({"tool_name": self.SLACK, "tool_input": {"channel_id": "C1", "text": self.TEXT}})
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err)

    def test_a_purpose_grant_never_widens_an_unlabelled_send(self):
        self._grant()
        rc, _ = self.run_hook({"tool_name": self.SLACK, "tool_input": {"channel_id": "C1", "text": "anything else"}})
        self.assertEqual(rc, 2)
        rc, _ = self.run_hook({"tool_name": "mcp__claude_ai_Gmail__send_message",
                               "tool_input": {"to": "bob@example.com", "body": self.TEXT}})
        self.assertEqual(rc, 2)

    def test_a_raising_detector_is_a_no(self):
        self._grant()
        with mock.patch.object(hook, "_PURPOSE_DETECTORS", (mock.Mock(side_effect=RuntimeError("boom")),)):
            rc, _ = self.run_hook({"tool_name": self.SLACK, "tool_input": {"channel_id": "C1", "text": self.TEXT}})
        self.assertEqual(rc, 2)

    def test_explain_reports_the_purpose(self):
        self._grant()
        out = io.StringIO()
        rc = hook.main(["--explain", json.dumps({"tool_name": self.SLACK,
                                                  "tool_input": {"channel_id": "C9", "text": self.TEXT}})], stdout=out)
        self.assertEqual(rc, 0)
        v = json.loads(out.getvalue())
        self.assertTrue(v["allowed"])
        self.assertEqual(v["reason"], "purpose")


if __name__ == "__main__":
    unittest.main()
