#!/usr/bin/env python3
"""Tests for `backends/codex_cli.py` — the codex-cli `Backend` (docs/pluggable-backend-spec.md §3.1,
Phase 2). Two halves:

  * `BuildChatEventFromStream` — event-mapping unit tests against RECORDED codex `--json` fixtures
    (`backends/test_codex_cli_fixtures/*.jsonl`, captured from a real `codex exec --json` run
    (Codex CLI 0.155.x), then scrubbed of real thread ids per the module's own docstring
    — a real thread id never belongs in a tracked file).
  * `CodexWarmSessionSend` — end-to-end `send()` behavior with `subprocess.Popen` replaced by a fake
    process whose stdout replays one fixture, mirroring `test_presence_warm_fallback.py`'s own
    `_FakeProc` pattern for `WarmSession`.

Stdlib ``unittest`` only; no live `codex`. Run: python -m unittest discover -s seneschal/scripts -p test_codex_cli.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from backends import codex_cli  # noqa: E402

FIXTURE_DIR = os.path.join(SCRIPT_DIR, "backends", "test_codex_cli_fixtures")


def _load_fixture(name: str) -> list[dict]:
    path = os.path.join(FIXTURE_DIR, name)
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class BuildChatEventFromStream(unittest.TestCase):
    """codex_cli.build_chat_event_from_stream — the converter feeding the SAME chat.event shape
    cockpit_pipe.build_chat_event_from_stream produces for claude-cli (spec §1.6/§3.1)."""

    def test_non_dict_is_none(self):
        self.assertIsNone(codex_cli.build_chat_event_from_stream(None, source="telegram"))
        self.assertIsNone(codex_cli.build_chat_event_from_stream("not a dict", source="telegram"))

    def test_thread_started_and_turn_started_are_dropped(self):
        events = _load_fixture("simple_reply.jsonl")
        self.assertIsNone(codex_cli.build_chat_event_from_stream(events[0], source="telegram"))  # thread.started
        self.assertIsNone(codex_cli.build_chat_event_from_stream(events[1], source="telegram"))  # turn.started

    def test_agent_message_becomes_assistant_output(self):
        events = _load_fixture("simple_reply.jsonl")
        ev = next(e for e in events if e.get("type") == "item.completed")
        chat_ev = codex_cli.build_chat_event_from_stream(ev, source="telegram", model="gpt-6-astra")
        self.assertEqual(chat_ev["kind"], "assistant_output")
        self.assertEqual(chat_ev["text"], "pong")
        self.assertEqual(chat_ev["source"], "telegram")
        self.assertEqual(chat_ev["model"], "gpt-6-astra")
        self.assertNotIn("tool_uses", chat_ev)

    def test_turn_completed_becomes_turn_done_with_usage_verbatim(self):
        events = _load_fixture("simple_reply.jsonl")
        ev = next(e for e in events if e.get("type") == "turn.completed")
        chat_ev = codex_cli.build_chat_event_from_stream(ev, source="telegram")
        self.assertEqual(chat_ev["kind"], "turn_done")
        self.assertFalse(chat_ev["is_error"])
        self.assertEqual(chat_ev["usage"], ev["usage"])
        # codex reports no per-call dollar cost and no per-turn iteration count — never guessed.
        self.assertNotIn("total_cost_usd", chat_ev)
        self.assertNotIn("num_turns", chat_ev)
        self.assertNotIn("reply_preview", chat_ev)

    def test_item_started_is_dropped_only_item_completed_converts(self):
        """The in_progress half of a command_execution pair must not double-count the tool call."""
        events = _load_fixture("shell_tool_call.jsonl")
        started = next(e for e in events if e.get("type") == "item.started")
        self.assertIsNone(codex_cli.build_chat_event_from_stream(started, source="telegram"))

    def test_command_execution_completed_becomes_tool_use(self):
        events = _load_fixture("shell_tool_call.jsonl")
        completed = next(e for e in events
                         if e.get("type") == "item.completed"
                         and e["item"].get("type") == "command_execution")
        chat_ev = codex_cli.build_chat_event_from_stream(completed, source="telegram")
        self.assertEqual(chat_ev["kind"], "assistant_output")
        self.assertEqual(len(chat_ev["tool_uses"]), 1)
        self.assertEqual(chat_ev["tool_uses"][0]["name"], "shell")
        self.assertIn("echo hello", chat_ev["tool_uses"][0]["input_preview"])

    def test_full_fixture_yields_expected_event_sequence(self):
        events = _load_fixture("shell_tool_call.jsonl")
        kinds = [codex_cli.build_chat_event_from_stream(e, source="telegram") for e in events]
        kinds = [c["kind"] if c else None for c in kinds]
        # thread.started, turn.started -> None; agent_message -> assistant_output; item.started -> None;
        # command_execution completed -> assistant_output; final agent_message -> assistant_output;
        # turn.completed -> turn_done.
        self.assertEqual(kinds, [None, None, "assistant_output", None,
                                 "assistant_output", "assistant_output", "turn_done"])

    def test_unknown_event_type_is_dropped(self):
        self.assertIsNone(codex_cli.build_chat_event_from_stream({"type": "something.new"}, source="telegram"))

    def test_error_event_becomes_turn_done_is_error(self):
        chat_ev = codex_cli.build_chat_event_from_stream({"type": "turn.failed", "message": "boom"},
                                                          source="telegram")
        self.assertEqual(chat_ev["kind"], "turn_done")
        self.assertTrue(chat_ev["is_error"])


class _FakeStdin:
    def close(self):
        pass


class _FakeProc:
    """Stand-in for subprocess.Popen — codex_cli's own version of the pattern
    test_presence_warm_fallback.py's `_FakeProc` uses for WarmSession, adjusted for codex_cli's
    per-turn (not held-open) process model: stdout is a fixed iterator of `--json` lines, `wait()`
    reports the recorded exit code."""

    def __init__(self, events, exit_code=0):
        self.stdin = _FakeStdin()
        self.stdout = iter([json.dumps(ev) + "\n" for ev in events])
        self._exit = exit_code

    def poll(self):
        return self._exit

    def wait(self, timeout=None):
        return self._exit

    def kill(self):
        pass


def _session(resume_session_id=None):
    return codex_cli.CodexWarmSession("codex", "gpt-6-astra", "bypassPermissions",
                                      lambda *_a, **_k: None, resume_session_id=resume_session_id)


class CodexWarmSessionSend(unittest.TestCase):
    def test_simple_reply_returns_agent_message_text(self):
        events = _load_fixture("simple_reply.jsonl")
        sess = _session()
        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=[_FakeProc(events)]):
            reply = sess.send("Reply with exactly the word: pong")
        self.assertEqual(reply, "pong")
        self.assertEqual(sess.session_id, "00000000-0000-0000-0000-000000000001")
        self.assertEqual(sess.turns_served, 1)
        self.assertIsNone(sess.session_cost_usd)  # codex reports no per-call dollar cost — never guessed
        self.assertIsNotNone(sess.context_tokens)

    def test_multi_message_turn_concatenates_agent_messages(self):
        events = _load_fixture("shell_tool_call.jsonl")
        sess = _session()
        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=[_FakeProc(events)]):
            reply = sess.send("Run echo hello")
        self.assertIn("echo hello", reply)
        self.assertIn("printed", reply)

    def test_on_event_fires_for_every_raw_event(self):
        events = _load_fixture("simple_reply.jsonl")
        sess = _session()
        seen = []
        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=[_FakeProc(events)]):
            sess.send("hi", on_event=seen.append)
        self.assertEqual(len(seen), len(events))

    def test_resume_sends_the_prior_thread_id_and_echoes_it_back(self):
        """The resume shares its thread id with simple_reply.jsonl on purpose — the fixture is what a
        real `codex exec resume <id> ...` echoes back (verified live against the CLI)."""
        events = _load_fixture("resume_echoes_thread_id.jsonl")
        sess = _session(resume_session_id="00000000-0000-0000-0000-000000000001")
        captured_cmd = []

        def _popen(cmd, **kwargs):
            captured_cmd.append(cmd)
            return _FakeProc(events)

        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=_popen):
            reply = sess.send("Reply with exactly the word: pong2")
        self.assertEqual(reply, "pong2")
        self.assertIn("resume", captured_cmd[0])
        self.assertIn("00000000-0000-0000-0000-000000000001", captured_cmd[0])
        self.assertEqual(sess.session_id, "00000000-0000-0000-0000-000000000001")
        self.assertTrue(sess.resumed)
        self.assertFalse(sess.resume_failed)

    def test_stdin_is_closed_immediately_never_written_to(self):
        """Host-verified gotcha: codex reads stdin when attached, even with nothing to give it —
        every spawn must close stdin right away rather than leave it open the way WarmSession does."""
        events = _load_fixture("simple_reply.jsonl")
        sess = _session()
        proc = _FakeProc(events)
        closed = []
        proc.stdin.close = lambda: closed.append(True)
        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=[proc]):
            sess.send("hi")
        self.assertEqual(closed, [True])

    def test_empty_stream_returns_none(self):
        sess = _session()
        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=[_FakeProc([])]):
            reply = sess.send("hi")
        self.assertIsNone(reply)

    def test_error_item_returns_none(self):
        events = [{"type": "thread.started", "thread_id": "t1"},
                  {"type": "item.completed", "item": {"type": "error", "message": "blocked"}},
                  {"type": "turn.completed", "usage": {"input_tokens": 10}}]
        sess = _session()
        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=[_FakeProc(events)]):
            reply = sess.send("hi")
        self.assertIsNone(reply)

    def test_mcp_configs_accepted_but_never_reach_the_spawn_argv(self):
        """Safety: constructor-signature parity with WarmSession, but the argument is inert — see
        backends/__init__.py's module docstring for the full reasoning."""
        events = _load_fixture("simple_reply.jsonl")
        sess = codex_cli.CodexWarmSession("codex", "gpt-6-astra", "bypassPermissions",
                                          lambda *_a, **_k: None,
                                          mcp_configs=["notion.json", "slack.json"])
        captured_cmd = []

        def _popen(cmd, **kwargs):
            captured_cmd.append(cmd)
            return _FakeProc(events)

        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=_popen):
            sess.send("hi")
        joined = " ".join(captured_cmd[0])
        self.assertNotIn("notion.json", joined)
        self.assertNotIn("slack.json", joined)
        self.assertNotIn("mcp", joined.lower())


class SandboxMapping(unittest.TestCase):
    def test_bypass_permissions_maps_to_workspace_write(self):
        self.assertEqual(codex_cli._sandbox_for("bypassPermissions"), "workspace-write")

    def test_accept_edits_maps_to_workspace_write(self):
        self.assertEqual(codex_cli._sandbox_for("acceptEdits"), "workspace-write")

    def test_unknown_or_none_defaults_to_read_only(self):
        self.assertEqual(codex_cli._sandbox_for("default"), "read-only")
        self.assertEqual(codex_cli._sandbox_for(None), "read-only")

    def test_ask_for_approval_is_always_never_in_argv(self):
        events = _load_fixture("simple_reply.jsonl")
        sess = _session()
        captured_cmd = []

        def _popen(cmd, **kwargs):
            captured_cmd.append(cmd)
            return _FakeProc(events)

        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=_popen):
            sess.send("hi")
        cmd = captured_cmd[0]
        self.assertIn("--ask-for-approval", cmd)
        self.assertEqual(cmd[cmd.index("--ask-for-approval") + 1], "never")


class ChildEnvScrub(unittest.TestCase):
    def test_scrubs_openai_api_key(self):
        with mock.patch.dict(codex_cli.os.environ, {"OPENAI_API_KEY": "sk-should-never-survive"}):
            env = codex_cli._child_env()
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_stamps_session_source(self):
        env = codex_cli._child_env()
        self.assertEqual(env["SENESCHAL_SESSION_SOURCE"], "daemon")


class CodexIdentity(unittest.TestCase):
    def test_missing_file_returns_none(self):
        d = tempfile.mkdtemp()
        self.assertIsNone(codex_cli.codex_identity(codex_home=d))

    def test_reads_account_id_never_tokens(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "auth.json"), "w", encoding="utf-8") as fh:
            json.dump({"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                      "tokens": {"id_token": "eyJ...", "access_token": "eyJ...",
                                "refresh_token": "eyJ...", "account_id": "acct-123"}}, fh)
        identity = codex_cli.codex_identity(codex_home=d)
        self.assertEqual(identity, {"account_id": "acct-123", "auth_mode": "chatgpt"})

    def test_malformed_file_returns_none(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "auth.json"), "w", encoding="utf-8") as fh:
            fh.write("not json")
        self.assertIsNone(codex_cli.codex_identity(codex_home=d))

    def test_no_account_id_returns_none(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "auth.json"), "w", encoding="utf-8") as fh:
            json.dump({"tokens": {}}, fh)
        self.assertIsNone(codex_cli.codex_identity(codex_home=d))


if __name__ == "__main__":
    unittest.main()
