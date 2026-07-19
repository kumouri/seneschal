#!/usr/bin/env python3
"""Tests for presence.py's v3 model-dial + Fable-delegation wiring (cockpit-spec.md "Model dials &
Fable delegation") — companion to test_presence_cockpit.py (v2) and test_presence_state.py (the
pre-existing Router shadow arm). Covers:
  * `resolve_warm_model` — state/model-config.json's warm_model winning over --model, with fallback;
  * `strip_force_fable` / `apply_force_route` — the `!fable` force-route text transform;
  * `fable_arm_classify` — the router's fable arm, ceiling-gated, logging `arm: "fable"` rows;
  * `_enqueue_inbound` wiring both of the above in for real, end to end;
  * `drainer_task` best-effort attaching a pending fable hint to the next prompt it builds.

Stdlib ``unittest`` only; no live Ollama or `claude` required (router.classify/classify_fable are
monkeypatched where a verdict matters). Run:  python -m unittest seneschal.scripts.test_presence_fable
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import model_config as mc  # noqa: E402
import presence as pr  # noqa: E402
import router  # noqa: E402


def _args(state_dir, **over):
    """Mirrors test_presence_async.py's helper — a minimal args namespace for exercising the tasks
    directly."""
    base = dict(state_dir=state_dir, telegram_env="unused.env", call_env=None, discord_env=None,
                no_discord=True, no_discord_gateway=False, stub_brain=True, stub_send=True,
                fake_inbox=None, router_mode="off",
                no_reminders=True, no_peek=True, no_slots=True, max_iterations=0,
                poll_timeout=1, discord_poll_sec=0.05, tick_sec=0.05, idle_min=0.0,
                model=None, slot_model=None, slot_catchup_min=180, claude_bin="claude",
                notion_mcp=None, slack_mcp=None, permission_mode="bypassPermissions",
                peek_interval_min=0, watch_prompt=None, watch_cmd=None, watch_model=None)
    base.update(over)
    return argparse.Namespace(**base)


class ResolveWarmModel(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_falls_back_to_cli_model_when_no_config(self):
        logs = []
        result = pr.resolve_warm_model(self.dir, "claude-opus-4-8", logs.append)
        self.assertEqual(result, "claude-opus-4-8")
        self.assertTrue(any("CLI flag" in l for l in logs))

    def test_none_when_neither_present(self):
        logs = []
        result = pr.resolve_warm_model(self.dir, None, logs.append)
        self.assertIsNone(result)
        self.assertTrue(any("CLI default" in l for l in logs))

    def test_model_config_wins_over_cli(self):
        mc.save(self.dir, "sonnet", "fable")
        logs = []
        result = pr.resolve_warm_model(self.dir, "claude-opus-4-8", logs.append)
        self.assertEqual(result, "claude-sonnet-5")
        self.assertTrue(any("model-config.json" in l for l in logs))

    def test_unrecognized_config_value_falls_back_to_cli(self):
        with open(mc.config_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"warm_model": "not-a-real-model"}))
        logs = []
        result = pr.resolve_warm_model(self.dir, "claude-opus-4-8", logs.append)
        self.assertEqual(result, "claude-opus-4-8")
        self.assertTrue(any("not recognized" in l for l in logs))


class ForceFableStripping(unittest.TestCase):
    def test_detects_bang_fable_prefix(self):
        forced, clean = pr.strip_force_fable("!fable draft the plan")
        self.assertTrue(forced)
        self.assertEqual(clean, "draft the plan")

    def test_case_insensitive_and_punctuation(self):
        forced, clean = pr.strip_force_fable("!Fable: do the thing")
        self.assertTrue(forced)
        self.assertEqual(clean, "do the thing")

    def test_no_prefix_is_untouched(self):
        forced, clean = pr.strip_force_fable("just a normal message")
        self.assertFalse(forced)
        self.assertEqual(clean, "just a normal message")

    def test_bare_trigger_with_no_message(self):
        forced, clean = pr.strip_force_fable("!fable")
        self.assertTrue(forced)
        self.assertEqual(clean, "")

    def test_mid_sentence_is_not_force_routed(self):
        forced, _ = pr.strip_force_fable("what does !fable even mean?")
        self.assertFalse(forced)

    def test_empty_text(self):
        forced, clean = pr.strip_force_fable("")
        self.assertFalse(forced)
        self.assertEqual(clean, "")


class ApplyForceRoute(unittest.TestCase):
    def test_injects_directive_and_strips_prefix(self):
        logs = []
        result = pr.apply_force_route("telegram", "!fable draft the plan", logs.append)
        self.assertIn("force-fable", result)
        self.assertTrue(result.endswith("draft the plan"))
        self.assertTrue(any("force-route" in l for l in logs))

    def test_untouched_when_no_prefix(self):
        logs = []
        result = pr.apply_force_route("telegram", "hello", logs.append)
        self.assertEqual(result, "hello")
        self.assertEqual(logs, [])

    def test_bare_trigger_produces_directive_only(self):
        result = pr.apply_force_route("telegram", "!fable", lambda *_: None)
        self.assertEqual(result, pr.FORCE_FABLE_DIRECTIVE)

    def test_directive_never_claims_to_bypass_the_gate(self):
        # A quick guard against a future edit accidentally implying force-route bypasses approvals.
        result = pr.apply_force_route("telegram", "!fable send the email", lambda *_: None)
        self.assertIn("never the approval gate", result)


class FableArmClassify(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig_classify_fable = router.classify_fable

    def tearDown(self):
        router.classify_fable = self._orig_classify_fable

    def test_never_calls_ollama_when_ceiling_not_fable_tier(self):
        mc.save(self.dir, "opus", "opus")
        called = []
        router.classify_fable = lambda *a, **k: called.append(1) or {
            "verdict": "fable", "confidence": 0.9, "reason": "x", "model": "m"}
        hint = pr.fable_arm_classify(self.dir, "telegram", "a hard problem", lambda *_: None)
        self.assertIsNone(hint)
        self.assertEqual(called, [])
        self.assertFalse(os.path.exists(pr.router_log_path(self.dir)))

    def test_never_calls_ollama_with_no_config_at_all(self):
        called = []
        router.classify_fable = lambda *a, **k: called.append(1) or {"verdict": "fable"}
        hint = pr.fable_arm_classify(self.dir, "telegram", "a hard problem", lambda *_: None)
        self.assertIsNone(hint)
        self.assertEqual(called, [])

    def test_fable_verdict_returns_hint_and_logs_arm_fable(self):
        mc.save(self.dir, "opus", "fable")
        router.classify_fable = lambda text, *a, **k: {
            "verdict": "fable", "confidence": 0.9, "reason": "deep synthesis", "model": "qwen3.5:4b"}
        hint = pr.fable_arm_classify(self.dir, "telegram", "help me weigh this decision", lambda *_: None)
        self.assertEqual(hint, pr.FABLE_HINT_LINE)
        with open(pr.router_log_path(self.dir), encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh.read().splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["arm"], "fable")
        self.assertEqual(rows[0]["verdict"], "fable")
        self.assertEqual(rows[0]["channel"], "telegram")

    def test_standard_verdict_returns_none_but_still_logs(self):
        mc.save(self.dir, "opus", "fable")
        router.classify_fable = lambda text, *a, **k: {
            "verdict": "standard", "confidence": 0.9, "reason": "routine", "model": "qwen3.5:4b"}
        hint = pr.fable_arm_classify(self.dir, "telegram", "what's on today?", lambda *_: None)
        self.assertIsNone(hint)
        with open(pr.router_log_path(self.dir), encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh.read().splitlines() if l.strip()]
        self.assertEqual(rows[0]["arm"], "fable")
        self.assertEqual(rows[0]["verdict"], "standard")

    def test_a_broken_classifier_never_raises(self):
        mc.save(self.dir, "opus", "fable")

        def boom(*_a, **_k):
            raise RuntimeError("ollama exploded")

        router.classify_fable = boom
        hint = pr.fable_arm_classify(self.dir, "telegram", "hi", lambda *_: None)  # must not raise
        self.assertIsNone(hint)
        self.assertFalse(os.path.exists(pr.router_log_path(self.dir)))


class EnqueueInboundForceRouteAndFableArm(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig_classify = router.classify
        self._orig_classify_fable = router.classify_fable
        # Deterministic, no-network stand-ins for both arms; individual tests override as needed.
        router.classify = lambda *a, **k: {
            "verdict": "escalate", "category": "other", "confidence": 0.0, "reason": "x", "model": "m"}
        router.classify_fable = lambda *a, **k: {
            "verdict": "standard", "confidence": 0.9, "reason": "routine", "model": "m"}

    def tearDown(self):
        router.classify = self._orig_classify
        router.classify_fable = self._orig_classify_fable

    async def test_force_route_prefix_is_transformed_before_persisting(self):
        args = _args(self.dir, router_mode="off")
        state = pr.DaemonState()
        await pr._enqueue_inbound(state, args, lambda *_: None,
                                  [("telegram", "!fable draft the Q3 plan", 0)])
        self.assertEqual(len(state.pending), 1)
        channel, text, attempts = state.pending[0]
        self.assertEqual(channel, "telegram")
        self.assertEqual(attempts, 0)
        self.assertIn("force-fable", text)
        self.assertTrue(text.endswith("draft the Q3 plan"))
        # the persisted text survives a reload byte-for-byte (durability — no schema change needed)
        save_daemon = pr.load_daemon_state(self.dir)
        self.assertEqual(save_daemon["pending"][0]["text"], text)

    async def test_plain_message_is_not_force_routed(self):
        args = _args(self.dir, router_mode="off")
        state = pr.DaemonState()
        await pr._enqueue_inbound(state, args, lambda *_: None, [("telegram", "what's next?", 0)])
        self.assertEqual(state.pending, [("telegram", "what's next?", 0)])

    async def test_fable_hint_is_queued_when_ceiling_admits_and_verdict_is_fable(self):
        mc.save(self.dir, "opus", "fable")
        router.classify_fable = lambda text, *a, **k: {
            "verdict": "fable", "confidence": 0.9, "reason": "deep synthesis", "model": "m"}
        args = _args(self.dir, router_mode="shadow")
        state = pr.DaemonState()
        await pr._enqueue_inbound(state, args, lambda *_: None,
                                  [("telegram", "help me think this through", 0)])
        self.assertEqual(state.fable_hints, [pr.FABLE_HINT_LINE])

    async def test_no_fable_hint_when_ceiling_is_not_fable_tier(self):
        mc.save(self.dir, "opus", "opus")
        called = []
        router.classify_fable = lambda *a, **k: called.append(1) or {"verdict": "fable"}
        args = _args(self.dir, router_mode="shadow")
        state = pr.DaemonState()
        await pr._enqueue_inbound(state, args, lambda *_: None,
                                  [("telegram", "a genuinely hard problem", 0)])
        self.assertEqual(called, [])
        self.assertEqual(state.fable_hints, [])

    async def test_no_fable_hint_when_router_mode_is_off(self):
        mc.save(self.dir, "opus", "fable")
        called = []
        router.classify_fable = lambda *a, **k: called.append(1) or {"verdict": "fable"}
        args = _args(self.dir, router_mode="off")
        state = pr.DaemonState()
        await pr._enqueue_inbound(state, args, lambda *_: None, [("telegram", "hard problem", 0)])
        self.assertEqual(called, [])
        self.assertEqual(state.fable_hints, [])


class _CapturingSession:
    """A StubWarmSession sibling that records the full prompt text it was sent, so a test can check
    whether a fable hint / force-route directive actually made it into the prompt — StubWarmSession
    itself only echoes the LAST LINE of what it received, which hides a prepended hint."""

    def __init__(self, *_, log=lambda *_: None, **__):
        self.session_id = "cap-session"
        self.model = None
        self.prompts: list = []

    def start(self) -> None:
        pass

    def send(self, text: str, on_event=None) -> str:
        self.prompts.append(text)
        if on_event is not None:
            try:
                on_event({"type": "result", "is_error": False, "result": "ok"})
            except Exception:  # noqa: BLE001
                pass
        return "ok"

    def close(self) -> None:
        pass


class DrainerAttachesFableHint(unittest.IsolatedAsyncioTestCase):
    async def test_a_pending_hint_rides_into_the_next_prompt_and_is_consumed(self):
        d = tempfile.mkdtemp()
        args = _args(d)
        state = pr.DaemonState()
        state.fable_hints.append(pr.FABLE_HINT_LINE)
        state.pending = [("telegram", "help me plan the migration", 0)]
        state.pending_event.set()
        session = _CapturingSession()
        task = asyncio.ensure_future(
            pr.drainer_task(state, args, lambda *_: None, lambda: session, 600.0))
        try:
            async with asyncio.timeout(5.0):
                while state.pending:
                    await asyncio.sleep(0.01)
        finally:
            state.stop.set()
            state.pending_event.set()
            await asyncio.wait_for(task, timeout=5.0)
        self.assertEqual(len(session.prompts), 1)
        self.assertIn(pr.FABLE_HINT_LINE, session.prompts[0])
        self.assertIn("help me plan the migration", session.prompts[0])
        self.assertEqual(state.fable_hints, [])  # consumed, not left behind for the next turn

    async def test_no_hint_pending_leaves_the_prompt_unmodified(self):
        d = tempfile.mkdtemp()
        args = _args(d)
        state = pr.DaemonState()
        state.pending = [("telegram", "what's on today?", 0)]
        state.pending_event.set()
        session = _CapturingSession()
        task = asyncio.ensure_future(
            pr.drainer_task(state, args, lambda *_: None, lambda: session, 600.0))
        try:
            async with asyncio.timeout(5.0):
                while state.pending:
                    await asyncio.sleep(0.01)
        finally:
            state.stop.set()
            state.pending_event.set()
            await asyncio.wait_for(task, timeout=5.0)
        # The GROUNDING template itself mentions "router hint" as part of its standing instructions —
        # what must be ABSENT is the actual hint LINE fable_arm_classify would have queued.
        self.assertNotIn(pr.FABLE_HINT_LINE, session.prompts[0])


if __name__ == "__main__":
    unittest.main()
