#!/usr/bin/env python3
"""Tests for fable_delegate.py — the Fable-5 one-shot delegate (cockpit-spec.md v3).

NO real `claude` spawns, NO network: `run_claude`/`delegate` take an injected fake runner matching
`subprocess.run`'s signature. Stdlib ``unittest`` only.

Run:  python -m unittest seneschal.scripts.test_fable_delegate
"""
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import cockpit_pipe as cp  # noqa: E402
import fable_delegate as fd  # noqa: E402
import governor as gv  # noqa: E402
import model_config as mc  # noqa: E402


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(returncode=0, stdout="the delegate's answer", stderr=""):
    calls = []

    def runner(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return _FakeCompleted(returncode=returncode, stdout=stdout, stderr=stderr)

    runner.calls = calls
    return runner


class ChildEnv(unittest.TestCase):
    def test_scrubs_anthropic_api_key(self):
        old = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-should-never-appear"
        try:
            env = fd.child_env()
            self.assertNotIn("ANTHROPIC_API_KEY", env)
        finally:
            if old is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = old


class ThreadSeed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write_thread(self, entries):
        with open(os.path.join(self.dir, "telegram-thread.json"), "w", encoding="utf-8") as fh:
            json.dump(entries, fh)

    def test_empty_when_no_thread_file(self):
        self.assertEqual(fd.thread_seed(self.dir), "")

    def test_empty_when_thread_not_a_list(self):
        self._write_thread({"not": "a list"})
        self.assertEqual(fd.thread_seed(self.dir), "")

    def test_includes_recent_turns_in_order(self):
        self._write_thread([
            {"role": "owner", "text": "first"},
            {"role": "assistant", "text": "second"},
            {"role": "owner", "text": "third"},
        ])
        seed = fd.thread_seed(self.dir)
        self.assertIn("owner: first", seed)
        self.assertIn("assistant: second", seed)
        self.assertIn("owner: third", seed)
        # chronological order preserved
        self.assertLess(seed.index("first"), seed.index("second"))
        self.assertLess(seed.index("second"), seed.index("third"))

    def test_respects_max_turns(self):
        entries = [{"role": "owner", "text": f"turn{i}"} for i in range(20)]
        self._write_thread(entries)
        seed = fd.thread_seed(self.dir, max_turns=3)
        for i in range(17):
            self.assertNotIn(f"turn{i}\n", seed + "\n")
        self.assertIn("turn19", seed)

    def test_respects_char_budget(self):
        entries = [{"role": "owner", "text": "x" * 100} for _ in range(10)]
        self._write_thread(entries)
        seed = fd.thread_seed(self.dir, max_turns=10, char_budget=250)
        # budget-bounded: nowhere near the full 1000+ chars all ten turns would cost
        self.assertLess(len(seed), 500)


class RunClaude(unittest.TestCase):
    def test_success_returns_stdout(self):
        runner = _fake_runner(returncode=0, stdout="  hello from fable  \n")
        ok, out = fd.run_claude("do the thing", "claude-fable-5", "claude", 60, runner=runner)
        self.assertTrue(ok)
        self.assertEqual(out, "hello from fable")
        self.assertEqual(len(runner.calls), 1)
        cmd = runner.calls[0]["cmd"]
        self.assertEqual(cmd, ["claude", "-p", "--model", "claude-fable-5", "do the thing"])

    def test_scrubs_api_key_from_child_env(self):
        old = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-leaked"
        try:
            runner = _fake_runner()
            fd.run_claude("task", "claude-fable-5", "claude", 60, runner=runner)
            env = runner.calls[0]["kwargs"]["env"]
            self.assertNotIn("ANTHROPIC_API_KEY", env)
        finally:
            if old is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = old

    def test_nonzero_exit_is_a_failure(self):
        runner = _fake_runner(returncode=1, stdout="", stderr="boom")
        ok, out = fd.run_claude("task", "claude-fable-5", "claude", 60, runner=runner)
        self.assertFalse(ok)
        self.assertIn("boom", out)

    def test_empty_stdout_on_success_is_still_a_failure(self):
        runner = _fake_runner(returncode=0, stdout="   ")
        ok, out = fd.run_claude("task", "claude-fable-5", "claude", 60, runner=runner)
        self.assertFalse(ok)

    def test_runner_raising_oserror_is_caught(self):
        def boom(cmd, **kwargs):
            raise OSError("no such binary")

        ok, out = fd.run_claude("task", "claude-fable-5", "claude", 60, runner=boom)
        self.assertFalse(ok)
        self.assertIn("failed to run", out)


class Delegate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_refuses_when_ceiling_is_not_fable_tier(self):
        mc.save(self.dir, "opus", "opus")
        runner = _fake_runner()
        ok, msg = fd.delegate("do a hard thing", state_dir=self.dir, runner=runner)
        self.assertFalse(ok)
        self.assertIn("refused", msg)
        self.assertIn("claude-opus-4-8", msg)
        self.assertEqual(runner.calls, [])  # never even attempted the subprocess

    def test_refuses_when_no_config_at_all(self):
        # a fresh state dir with no model-config.json -> ceiling is None -> never admits fable
        runner = _fake_runner()
        ok, msg = fd.delegate("task", state_dir=self.dir, runner=runner)
        self.assertFalse(ok)
        self.assertIn("refused", msg)
        self.assertEqual(runner.calls, [])

    def test_succeeds_when_ceiling_admits_fable(self):
        mc.save(self.dir, "opus", "fable")
        runner = _fake_runner(stdout="here's the plan")
        ok, msg = fd.delegate("map out the migration", state_dir=self.dir, runner=runner)
        self.assertTrue(ok)
        self.assertEqual(msg, "here's the plan")
        self.assertEqual(len(runner.calls), 1)

    def test_seeds_thread_context_by_default(self):
        mc.save(self.dir, "opus", "fable")
        with open(os.path.join(self.dir, "telegram-thread.json"), "w", encoding="utf-8") as fh:
            json.dump([{"role": "owner", "text": "earlier context"}], fh)
        runner = _fake_runner()
        fd.delegate("the task", state_dir=self.dir, runner=runner)
        prompt = runner.calls[0]["cmd"][-1]
        self.assertIn("earlier context", prompt)
        self.assertIn("the task", prompt)

    def test_no_thread_skips_seeding(self):
        mc.save(self.dir, "opus", "fable")
        with open(os.path.join(self.dir, "telegram-thread.json"), "w", encoding="utf-8") as fh:
            json.dump([{"role": "owner", "text": "earlier context"}], fh)
        runner = _fake_runner()
        fd.delegate("the task", state_dir=self.dir, runner=runner, include_thread=False)
        prompt = runner.calls[0]["cmd"][-1]
        self.assertNotIn("earlier context", prompt)
        self.assertEqual(prompt, "the task")

    def test_success_badges_the_transcript(self):
        mc.save(self.dir, "opus", "fable")
        runner = _fake_runner(stdout="the answer")
        fd.delegate("task", state_dir=self.dir, runner=runner)
        tail = cp.read_transcript_tail(self.dir)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["kind"], "assistant_output")
        self.assertEqual(tail[0]["model"], "claude-fable-5")
        self.assertEqual(tail[0]["text"], "the answer")

    def test_failure_does_not_badge_the_transcript(self):
        mc.save(self.dir, "opus", "fable")
        runner = _fake_runner(returncode=1, stderr="crashed")
        ok, _ = fd.delegate("task", state_dir=self.dir, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(cp.read_transcript_tail(self.dir), [])

    def test_canonicalizes_a_short_model_alias(self):
        mc.save(self.dir, "opus", "fable")
        runner = _fake_runner()
        fd.delegate("task", state_dir=self.dir, model="fable", runner=runner)
        self.assertEqual(runner.calls[0]["cmd"][3], "claude-fable-5")


class DefaultConversationId(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_none_when_no_presence_state_file(self):
        self.assertIsNone(fd.default_conversation_id(self.dir))

    def test_reads_last_session_id(self):
        with open(os.path.join(self.dir, "presence-state.json"), "w", encoding="utf-8") as fh:
            json.dump({"last_session_id": "sess-123", "pending": []}, fh)
        self.assertEqual(fd.default_conversation_id(self.dir), "sess-123")

    def test_none_when_file_is_corrupt(self):
        with open(os.path.join(self.dir, "presence-state.json"), "w", encoding="utf-8") as fh:
            fh.write("not json")
        self.assertIsNone(fd.default_conversation_id(self.dir))

    def test_none_when_session_id_missing_or_blank(self):
        with open(os.path.join(self.dir, "presence-state.json"), "w", encoding="utf-8") as fh:
            json.dump({"last_session_id": "  "}, fh)
        self.assertIsNone(fd.default_conversation_id(self.dir))


class GovernorGate(unittest.TestCase):
    """Oikonomos (v3.5): fable_delegate.delegate() consults governor.check("fable_oneshot", ...) after
    the ceiling check but before spawning — same clear-message-on-refusal contract as the ceiling."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        mc.save(self.dir, "opus", "fable")  # ceiling admits fable for every test in this class

    def test_refuses_when_daily_quota_exhausted(self):
        gv.save(self.dir, {"fable_oneshots_per_day": 0})
        runner = _fake_runner()
        ok, msg = fd.delegate("task", state_dir=self.dir, runner=runner, conversation_id="c1")
        self.assertFalse(ok)
        self.assertIn("refused", msg)
        self.assertIn("daily Fable one-shot quota", msg)
        self.assertEqual(runner.calls, [])  # never even attempted the subprocess

    def test_refuses_when_conversation_quota_exhausted(self):
        gv.save(self.dir, {"fable_oneshots_per_conversation": 0})
        runner = _fake_runner()
        ok, msg = fd.delegate("task", state_dir=self.dir, runner=runner, conversation_id="c1")
        self.assertFalse(ok)
        self.assertIn("Fable one-shot cap", msg)
        self.assertEqual(runner.calls, [])

    def test_succeeds_and_appends_ledger_line_on_success(self):
        runner = _fake_runner(stdout="the answer")
        ok, msg = fd.delegate("task", state_dir=self.dir, runner=runner, conversation_id="c1")
        self.assertTrue(ok)
        recs = gv._read_ledger(self.dir)
        fable_recs = [r for r in recs if r.get("kind") == "fable_oneshot"]
        self.assertEqual(len(fable_recs), 1)
        self.assertEqual(fable_recs[0]["conversation_id"], "c1")
        self.assertEqual(fable_recs[0]["model"], "claude-fable-5")

    def test_failure_does_not_append_ledger_line(self):
        runner = _fake_runner(returncode=1, stderr="boom")
        ok, _ = fd.delegate("task", state_dir=self.dir, runner=runner, conversation_id="c1")
        self.assertFalse(ok)
        fable_recs = [r for r in gv._read_ledger(self.dir) if r.get("kind") == "fable_oneshot"]
        self.assertEqual(fable_recs, [])

    def test_concurrency_counter_is_balanced_after_a_call(self):
        runner = _fake_runner(stdout="ok")
        fd.delegate("task", state_dir=self.dir, runner=runner, conversation_id="c1")
        self.assertEqual(gv._read_inflight(self.dir), 0)

    def test_concurrency_counter_balanced_even_on_subprocess_failure(self):
        runner = _fake_runner(returncode=1, stderr="boom")
        fd.delegate("task", state_dir=self.dir, runner=runner, conversation_id="c1")
        self.assertEqual(gv._read_inflight(self.dir), 0)

    def test_second_conversation_unaffected_by_first_conversations_cap(self):
        gv.save(self.dir, {"fable_oneshots_per_conversation": 1})
        runner = _fake_runner(stdout="ok")
        fd.delegate("task one", state_dir=self.dir, runner=runner, conversation_id="c1")
        ok, msg = fd.delegate("task two", state_dir=self.dir, runner=runner, conversation_id="c2")
        self.assertTrue(ok)

    def test_conversation_id_defaults_from_presence_state_when_not_given(self):
        with open(os.path.join(self.dir, "presence-state.json"), "w", encoding="utf-8") as fh:
            json.dump({"last_session_id": "auto-sess"}, fh)
        gv.save(self.dir, {"fable_oneshots_per_conversation": 1})
        runner = _fake_runner(stdout="ok")
        fd.delegate("task one", state_dir=self.dir, runner=runner)  # no explicit conversation_id
        ok, msg = fd.delegate("task two", state_dir=self.dir, runner=runner)  # same auto-derived id
        self.assertFalse(ok)
        self.assertIn("Fable one-shot cap", msg)

    def test_explicit_conversation_id_overrides_presence_state(self):
        with open(os.path.join(self.dir, "presence-state.json"), "w", encoding="utf-8") as fh:
            json.dump({"last_session_id": "auto-sess"}, fh)
        gv.save(self.dir, {"fable_oneshots_per_conversation": 1})
        runner = _fake_runner(stdout="ok")
        fd.delegate("task one", state_dir=self.dir, runner=runner, conversation_id="explicit-1")
        ok, _ = fd.delegate("task two", state_dir=self.dir, runner=runner, conversation_id="explicit-2")
        self.assertTrue(ok)  # different explicit conversation id, so the cap doesn't apply


class CliMain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_missing_task_exits_2(self, ):
        import io
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            rc = fd._main(["fable_delegate.py", "--state-dir", self.dir])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
