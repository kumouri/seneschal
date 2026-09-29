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


# A realistic `claude -p --output-format json` result object, trimmed to the fields this script reads.
# The delegate asks for json mode precisely so `usage` comes back — see run_claude.
FAKE_USAGE = {
    "input_tokens": 12,
    "output_tokens": 300,
    "cache_read_input_tokens": 20_000,
    "cache_creation_input_tokens": 1_000,
    "cache_creation": {"ephemeral_1h_input_tokens": 1_000, "ephemeral_5m_input_tokens": 0},
}


def _fake_result_json(text="the delegate's answer", usage=None, is_error=False):
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": is_error,
        "result": text, "usage": FAKE_USAGE if usage is None else usage,
    })


def _fake_runner(returncode=0, stdout=None, stderr=""):
    """Injected stand-in for subprocess.run. `stdout` defaults to a well-formed json-mode result; pass
    a plain string to simulate an older/text-mode CLI."""
    calls = []
    if stdout is None:
        stdout = _fake_result_json()

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
            self.assertNotIn("ANTHROPIC_API_KEY", sorted(env.keys()))
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

    def _write_main_topic(self, entries):
        os.makedirs(os.path.join(self.dir, fd.THREAD_DIR), exist_ok=True)
        with open(os.path.join(self.dir, fd.THREAD_DIR, "main.json"), "w", encoding="utf-8") as fh:
            json.dump(entries, fh)

    def test_reads_the_per_topic_main_cache(self):
        # The cache went per-topic; a reader left on the legacy path would seed "" forever, silently.
        self._write_main_topic([{"role": "owner", "text": "from the main topic"}])
        self.assertIn("from the main topic", fd.thread_seed(self.dir))

    def test_main_cache_wins_over_the_legacy_file(self):
        self._write_main_topic([{"role": "owner", "text": "new layout"}])
        self._write_thread([{"role": "owner", "text": "legacy layout"}])
        seed = fd.thread_seed(self.dir)
        self.assertIn("new layout", seed)
        self.assertNotIn("legacy layout", seed)

    def test_legacy_file_is_the_second_rung(self):
        self._write_thread([{"role": "owner", "text": "legacy layout"}])
        self.assertIn("legacy layout", fd.thread_seed(self.dir))

    def test_spellings_match_the_daemon_when_it_declares_them(self):
        # Drift between this module's duplicated path and the daemon's is SILENT, so pin it — once the
        # daemon carries the per-topic cache constants.
        try:
            import presence  # noqa: WPS433 -- optional: the daemon may not import in every env
        except Exception:  # noqa: BLE001
            self.skipTest("presence.py not importable here")
        if not hasattr(presence, "THREAD_DIR"):
            self.skipTest("wave 26: presence hook THREAD_DIR not wired yet")
        self.assertEqual(fd.THREAD_DIR, presence.THREAD_DIR)
        self.assertEqual(fd.LEGACY_THREAD_FILE, presence.LEGACY_THREAD_FILE)


class RunClaude(unittest.TestCase):
    def test_success_returns_answer_and_usage(self):
        runner = _fake_runner(returncode=0, stdout=_fake_result_json("  hello from fable  \n"))
        ok, out, usage = fd.run_claude("do the thing", "claude-fable-5", "claude", 60, runner=runner)
        self.assertTrue(ok)
        self.assertEqual(out, "hello from fable")
        self.assertEqual(usage, FAKE_USAGE)

    def test_asks_the_cli_for_json_so_there_is_usage_to_meter(self):
        # THE metering regression guard. Drop --output-format json and the CLI returns prose only, so
        # the delegation silently meters nothing against the tightest budget on the board.
        runner = _fake_runner()
        fd.run_claude("do the thing", "claude-fable-5", "claude", 60, runner=runner)
        self.assertEqual(runner.calls[0]["cmd"], [
            "claude", "-p", "--model", "claude-fable-5", "--output-format", "json", "do the thing",
        ])

    def test_scrubs_api_key_from_child_env(self):
        old = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-leaked"
        try:
            runner = _fake_runner()
            fd.run_claude("task", "claude-fable-5", "claude", 60, runner=runner)
            env = runner.calls[0]["kwargs"]["env"]
            self.assertNotIn("ANTHROPIC_API_KEY", sorted(env.keys()))
        finally:
            if old is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = old

    def test_nonzero_exit_is_a_failure(self):
        runner = _fake_runner(returncode=1, stdout="", stderr="boom")
        ok, out, usage = fd.run_claude("task", "claude-fable-5", "claude", 60, runner=runner)
        self.assertFalse(ok)
        self.assertIn("boom", out)
        self.assertIsNone(usage)

    def test_empty_stdout_on_success_is_still_a_failure(self):
        runner = _fake_runner(returncode=0, stdout="   ")
        ok, out, usage = fd.run_claude("task", "claude-fable-5", "claude", 60, runner=runner)
        self.assertFalse(ok)

    def test_empty_result_field_is_a_failure(self):
        runner = _fake_runner(stdout=_fake_result_json("   "))
        ok, out, usage = fd.run_claude("task", "claude-fable-5", "claude", 60, runner=runner)
        self.assertFalse(ok)

    def test_is_error_result_falls_through_rather_than_being_read_as_an_answer(self):
        runner = _fake_runner(stdout=_fake_result_json("nope", is_error=True))
        ok, out, usage = fd.run_claude("task", "claude-fable-5", "claude", 60, runner=runner)
        self.assertIsNone(usage)  # not parsed as a success result

    def test_plain_text_stdout_still_answers_but_yields_no_usage(self):
        # An older CLI, or json mode going away. The answer is still delivered; usage is honestly None
        # so the caller marks the row unmetered instead of inventing a zero.
        runner = _fake_runner(stdout="just prose, no json here")
        ok, out, usage = fd.run_claude("task", "claude-fable-5", "claude", 60, runner=runner)
        self.assertTrue(ok)
        self.assertEqual(out, "just prose, no json here")
        self.assertIsNone(usage)

    def test_runner_raising_oserror_is_caught(self):
        def boom(cmd, **kwargs):
            raise OSError("no such binary")

        ok, out, usage = fd.run_claude("task", "claude-fable-5", "claude", 60, runner=boom)
        self.assertFalse(ok)
        self.assertIn("failed to run", out)
        self.assertIsNone(usage)


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

    def test_refuses_cleanly_on_a_non_claude_cli_backend(self):
        """Fable has no meaning off claude-cli, and the refusal must say so rather than read as an
        ordinary ceiling-too-low message."""
        mc.save(self.dir, "gpt-5.5", "gpt-6-astra", backend="codex-cli")
        runner = _fake_runner()
        ok, msg = fd.delegate("do a hard thing", state_dir=self.dir, runner=runner)
        self.assertFalse(ok)
        self.assertIn("codex-cli", msg)
        self.assertIn("claude-cli", msg)
        self.assertEqual(runner.calls, [])

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
        runner = _fake_runner(stdout=_fake_result_json("the answer"))
        ok, msg = fd.delegate("task", state_dir=self.dir, runner=runner, conversation_id="c1")
        self.assertTrue(ok)
        recs = gv._read_ledger(self.dir)
        fable_recs = [r for r in recs if r.get("kind") == "fable_oneshot"]
        self.assertEqual(len(fable_recs), 1)
        self.assertEqual(fable_recs[0]["conversation_id"], "c1")
        self.assertEqual(fable_recs[0]["model"], "claude-fable-5")

    def test_a_delegation_records_non_zero_spend(self):
        """End to end: a delegation must meter real tokens against Fable's budget — the tightest on
        the board and the ONLY token budget that hard-blocks. A rail that meters zero cannot bite, and
        worse, the gate trusts the zero."""
        runner = _fake_runner()
        ok, _ = fd.delegate("task", state_dir=self.dir, runner=runner, conversation_id="c1")
        self.assertTrue(ok)
        rec = [r for r in gv._read_ledger(self.dir) if r.get("kind") == "fable_oneshot"][0]
        self.assertGreater(rec["tokens"], 0)
        self.assertGreater(rec["billable_tokens"], 0)
        # 12 + 300 + 20000*0.1 + 1000*2 = 4312
        self.assertEqual(rec["billable_tokens"], 4312)
        self.assertNotIn("metered", rec)

    def test_that_spend_actually_reaches_the_rollup(self):
        # The other half: rollups must accrue tokens from any row carrying them, not only
        # kind == "tokens", or a fable_oneshot row's spend is dropped even though it carries a count.
        runner = _fake_runner()
        fd.delegate("task", state_dir=self.dir, runner=runner, conversation_id="c1")
        roll = gv.rollups(self.dir)
        self.assertEqual(roll["billable_by_model"]["day"]["claude-fable-5"], 4312)
        self.assertEqual(roll["basis_by_model"]["day"]["claude-fable-5"], gv.BASIS_BILLABLE)

    def test_unreadable_usage_is_marked_unmetered_never_recorded_as_zero(self):
        runner = _fake_runner(stdout="prose, no usage anywhere")
        ok, _ = fd.delegate("task", state_dir=self.dir, runner=runner, conversation_id="c1")
        self.assertTrue(ok)  # the answer still gets delivered
        rec = [r for r in gv._read_ledger(self.dir) if r.get("kind") == "fable_oneshot"][0]
        self.assertEqual(rec["metered"], gv.METERED_UNAVAILABLE)
        self.assertNotIn("tokens", rec)
        self.assertNotIn("billable_tokens", rec)
        # and the gap is visible in the rollup rather than reading as free
        self.assertEqual(gv.rollups(self.dir)["unmetered_by_model"]["day"]["claude-fable-5"], 1)

    def test_metered_spend_can_exhaust_the_hard_gate(self):
        # The point of metering at all: once it records real tokens, the budget can actually refuse.
        gv.save(self.dir, {"daily_token_budget_by_model": {"claude-fable-5": 1000}})
        runner = _fake_runner()
        ok, _ = fd.delegate("task one", state_dir=self.dir, runner=runner, conversation_id="c1")
        self.assertTrue(ok)
        ok2, msg = fd.delegate("task two", state_dir=self.dir, runner=runner, conversation_id="c2")
        self.assertFalse(ok2)
        self.assertIn("daily token budget", msg)
        self.assertEqual(len(runner.calls), 1)  # the second never spawned

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


class TheCeilingIsNotABudget(unittest.TestCase):
    """**THE DISTINCTION THAT MUST NOT COLLAPSE.** `fable_delegate` refuses for two unrelated reasons:

      1. The **max-routable-model ceiling** (`state/model-config.json`'s `max_routable_model`) is a
         MODEL-ROUTING POLICY. It is not a budget, it is checked BEFORE the governor is consulted,
         and its refusal must never read as a quota.
      2. The **budget/quota refusals** (daily + per-conversation Fable quota, delegation concurrency,
         Fable's token budget) are the governor's (`GovernorGate` above).

    A change that made the ceiling stop refusing — or blurred its message into a budget message —
    would be wrong however green the rest of the suite went."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    # ---------------------------------------------------------------- the ceiling refuses

    def test_ceiling_still_refuses_with_a_clear_message(self):
        mc.save(self.dir, "opus", "opus")  # ceiling does not admit Fable-tier
        runner = _fake_runner()
        ok, msg = fd.delegate("do a hard thing", state_dir=self.dir, runner=runner)
        self.assertFalse(ok)
        self.assertIn("refused", msg)
        self.assertIn("max_routable_model", msg)
        self.assertIn("claude-opus-4-8", msg)   # names the ceiling it actually read
        self.assertIn("Model dials", msg)       # and where to change it
        self.assertEqual(runner.calls, [])      # never even attempted the subprocess

    def test_ceiling_still_refuses_with_no_config_at_all(self):
        runner = _fake_runner()
        ok, msg = fd.delegate("task", state_dir=self.dir, runner=runner)
        self.assertFalse(ok)
        self.assertIn("refused", msg)
        self.assertEqual(runner.calls, [])

    def test_the_ceiling_refusal_is_not_a_governor_refusal(self):
        """Guards against the sloppy fix: making `delegate` stop refusing at all would pass a test
        that only asserted "budget no longer blocks". The ceiling message must not mention a quota."""
        mc.save(self.dir, "opus", "opus")
        _, msg = fd.delegate("task", state_dir=self.dir, runner=_fake_runner())
        for budget_word in ("quota", "budget", "concurrency", "cap"):
            self.assertNotIn(budget_word, msg.lower())


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
