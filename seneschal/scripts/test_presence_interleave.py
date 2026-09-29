#!/usr/bin/env python3
"""Tests for the daemon half of mid-turn interleave **phase 0 — observe only**.
Spec: the mid-turn interleave spec — §4.3 (the phase and its schema), §5.1 (the serialization
invariant this must not touch), §6 (the latency budget and `turn_still_live`), §10.

Three things are under test, and the first is the most important:

  * **`observe` IS ZERO BEHAVIOUR CHANGE.** `ObserveChangesNothing` runs the same conversation with
    the mode off and with it on and asserts the reply, the durable queue, the thread tail and the
    prompt the assistant was handed are identical. That is the claim the whole phase rests on, and it is the
    one a future edit is most likely to break while everything still looks green;
  * **`turn_still_live` and `turn_remaining_sec` are measured, not assumed** — including the case the
    phase exists to detect, a verdict that lands after its own turn ended, which resolves immediately
    with a NEGATIVE remaining;
  * **`fold_depth` counts folds, not arrivals** — advanced only by a steer, so the maximum over the
    steer rows is the chain depth Q5's override left unbounded.

**No test here makes a real Ollama call**: `interleave.gate` is replaced throughout. Nothing sends —
`--stub-send` plus a temp state dir, this suite's standing rule.

Run:  python -m unittest seneschal.scripts.test_presence_interleave
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

import presence as pr  # noqa: E402

import interleave as il  # noqa: E402



def _args(state_dir, **over):
    base = dict(state_dir=state_dir, telegram_env="unused.env", call_env=None, discord_env=None,
                no_discord=True, no_discord_gateway=False, stub_brain=True, stub_send=True,
                fake_inbox=None, router_mode="off", interleave_mode=il.MODE_OBSERVE,
                no_reminders=True, no_peek=True, no_slots=True, max_iterations=0,
                poll_timeout=1, discord_poll_sec=0.05, tick_sec=0.05, idle_min=0.0,
                model=None, slot_model=None, slot_catchup_min=180, claude_bin="claude",
                notion_mcp=None, slack_mcp=None, permission_mode="bypassPermissions",
                peek_interval_min=0, watch_prompt=None, watch_cmd=None, watch_model=None)
    base.update(over)
    return argparse.Namespace(**base)


def _rows(state_dir):
    path = il.log_path(state_dir)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _verdict(kind="hold", layer=None, confidence=0.9, reason="r"):
    return {"layer": layer if layer is not None else il.LAYER_MODEL, "verdict": kind, "confidence": confidence, "reason": reason,
            "model": "qwen3.5:4b"}


class _FakeGate:
    """Replaces `interleave.gate`. Never touches Ollama; optionally sleeps so a verdict can be made
    to land after its own turn has ended."""

    def __init__(self, verdicts=None, sleep=0.0):
        self.verdicts = list(verdicts or [])
        self.sleep = sleep
        self.calls = []

    def __call__(self, message, in_flight, **_kw):
        self.calls.append((message, in_flight))
        if self.sleep:
            import time
            time.sleep(self.sleep)
        return self.verdicts.pop(0) if self.verdicts else _verdict()


class _Patch:
    """Swap `interleave.gate` for the duration of a test."""

    def __init__(self, case, gate):
        case.addCleanup(lambda orig=il.gate: setattr(il, "gate", orig))
        il.gate = gate


# --------------------------------------------------------------------------- the turn window

class TurnWindow(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)

    def test_the_window_is_keyed_to_the_transcripts_own_turn_id(self):
        state = pr.DaemonState()
        rec = pr.interleave_open_turn(state, "304bc2a2691c", "what's on my calendar")
        self.assertIs(state.inflight_turn, rec)
        self.assertEqual(rec["turn_id"], "304bc2a2691c")
        self.assertIs(state.interleave_turns["304bc2a2691c"], rec)

    def test_finished_windows_age_out(self):
        state = pr.DaemonState()
        for i in range(pr.INTERLEAVE_TURN_MEMORY + 5):
            pr.interleave_open_turn(state, f"t{i}", "x")
        self.assertEqual(len(state.interleave_turns), pr.INTERLEAVE_TURN_MEMORY)

    def test_closing_twice_is_a_no_op(self):
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t", "x")
        pr.interleave_close_turn(state, self.args, lambda *_: None)
        pr.interleave_close_turn(state, self.args, lambda *_: None)
        self.assertIsNone(state.inflight_turn)

    def test_tool_names_reach_the_in_flight_summary(self):
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t", "review the queue")
        for name in ("Read", "Bash", "Read"):
            pr.interleave_note_tool(state, name)
        snap = pr.interleave_snapshot(state, self.args, "telegram", "also do X")
        self.assertIn("Read, Bash", snap["in_flight"])
        self.assertIn("review the queue", snap["in_flight"])

    def test_noting_a_tool_with_no_window_open_is_harmless(self):
        state = pr.DaemonState()
        pr.interleave_note_tool(state, "Read")  # must not raise
        self.assertIsNone(state.inflight_turn)


class SnapshotEligibility(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_no_window_open_means_nothing_to_measure(self):
        self.assertIsNone(pr.interleave_snapshot(pr.DaemonState(), _args(self.dir), "telegram", "x"))

    def test_mode_off_snapshots_nothing(self):
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t", "x")
        self.assertIsNone(pr.interleave_snapshot(state, _args(self.dir, interleave_mode=il.MODE_OFF),
                                                 "telegram", "x"))

    def test_an_args_namespace_without_the_flag_defaults_OFF(self):
        # The parser's default is `observe`; the getattr fallback is `off`, deliberately. A harness
        # namespace that has never heard of the flag must not acquire its behaviour.
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t", "x")
        bare = argparse.Namespace(state_dir=self.dir)
        self.assertEqual(pr.interleave_mode(bare), il.MODE_OFF)
        self.assertIsNone(pr.interleave_snapshot(state, bare, "telegram", "x"))

    def test_a_machine_synthesized_arrival_is_snapshotted_as_untyped(self):
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t", "x")
        snap = pr.interleave_snapshot(state, _args(self.dir), "telegram", "[job finished] 4b1c ok")
        self.assertFalse(snap["typed"])
        self.assertTrue(pr.interleave_snapshot(state, _args(self.dir), "telegram", "hi")["typed"])


# --------------------------------------------------------------------------- observing

class ObservedRows(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)

    async def test_a_verdict_inside_its_turn_resolves_at_turn_close(self):
        _Patch(self, _FakeGate([_verdict("steer", confidence=0.9, reason="adds to it")]))
        state = pr.DaemonState()
        rec = pr.interleave_open_turn(state, "t1", "review the queue")
        snap = pr.interleave_snapshot(state, self.args, "telegram", "also add a button")
        await pr.interleave_observe(state, self.args, lambda *_: None, snap)

        arrival, = _rows(self.dir)
        self.assertEqual(arrival["kind"], il.KIND_ARRIVAL)
        self.assertTrue(arrival["turn_still_live"])
        self.assertEqual(arrival["turn_id"], "t1")
        self.assertEqual(arrival["verdict"], il.STEER)
        self.assertEqual(len(rec["pending"]), 1)

        pr.interleave_close_turn(state, self.args, lambda *_: None)
        rows = _rows(self.dir)
        self.assertEqual(len(rows), 2)
        resolved = rows[1]
        self.assertEqual(resolved["kind"], il.KIND_RESOLVED)
        self.assertEqual(resolved["arrival_id"], arrival["arrival_id"])
        self.assertGreaterEqual(resolved["turn_remaining_sec"], 0.0)
        self.assertIsNotNone(resolved["arrived_offset_frac"])
        self.assertLessEqual(resolved["arrived_offset_frac"], 1.0)

    async def test_a_verdict_that_outran_its_turn_resolves_immediately_and_negative(self):
        # The case the whole phase exists to detect (§6: 60.8 s classifier vs a 51.8 s median turn).
        _Patch(self, _FakeGate([_verdict("hold")], sleep=0.05))
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t1", "a long turn")
        snap = pr.interleave_snapshot(state, self.args, "telegram", "also, cats fed")

        async def close_soon():
            await asyncio.sleep(0.01)
            pr.interleave_close_turn(state, self.args, lambda *_: None)

        await asyncio.gather(pr.interleave_observe(state, self.args, lambda *_: None, snap),
                             close_soon())
        rows = _rows(self.dir)
        self.assertEqual(len(rows), 2, "both rows are written, even though the turn ended first")
        self.assertFalse(rows[0]["turn_still_live"])
        self.assertLess(rows[1]["turn_remaining_sec"], 0.0,
                        "how LATE it was is the number section 6's corollary is read off")

    async def test_fold_depth_is_advanced_by_a_steer_and_not_by_a_hold(self):
        _Patch(self, _FakeGate([_verdict("steer"), _verdict("hold"), _verdict("steer")]))
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t1", "head")
        for text in ("first", "second", "third"):
            snap = pr.interleave_snapshot(state, self.args, "telegram", text)
            await pr.interleave_observe(state, self.args, lambda *_: None, snap)
        depths = [(r["verdict"], r["fold_depth"]) for r in _rows(self.dir)]
        # A hold's depth reads as "the fold it would have been"; only a steer advances the chain, so
        # the MAX over steer rows is the depth Q5's override left uncapped.
        self.assertEqual(depths, [("steer", 1), ("hold", 2), ("steer", 2)])

    async def test_the_verdict_latency_is_measured(self):
        _Patch(self, _FakeGate([_verdict("hold")], sleep=0.05))
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t1", "head")
        snap = pr.interleave_snapshot(state, self.args, "telegram", "hi")
        await pr.interleave_observe(state, self.args, lambda *_: None, snap)
        self.assertGreaterEqual(_rows(self.dir)[0]["verdict_latency_sec"], 0.04)

    async def test_the_gate_is_handed_the_relation_not_just_the_message(self):
        gate = _FakeGate([_verdict("hold")])
        _Patch(self, gate)
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t1", "what's on my calendar tomorrow")
        pr.interleave_note_tool(state, "Bash")
        snap = pr.interleave_snapshot(state, self.args, "telegram", "cats fed!")
        await pr.interleave_observe(state, self.args, lambda *_: None, snap)
        message, in_flight = gate.calls[0]
        self.assertEqual(message, "cats fed!")
        self.assertIn("what's on my calendar tomorrow", in_flight)
        self.assertIn("Bash", in_flight)


class IntakeGatesEveryMidTurnArrival(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)

    async def test_a_whole_batch_is_snapshotted_before_any_gate_runs(self):
        # Otherwise the second message in a batch is charged for the first one's classifier latency,
        # and `arrived_offset_sec` stops being a statement about when the message landed.
        _Patch(self, _FakeGate([_verdict("hold"), _verdict("hold")], sleep=0.1))
        state = pr.DaemonState()
        state.inflight_text = "the head"
        pr.interleave_open_turn(state, "t1", "the head")
        await pr._enqueue_inbound(state, self.args, lambda *_: None,
                                  [("telegram", "one", 0), ("telegram", "two", 0)])
        offsets = [r["arrived_offset_sec"] for r in _rows(self.dir)]
        self.assertEqual(len(offsets), 2)
        self.assertLess(abs(offsets[1] - offsets[0]), 0.05,
                        "the second offset must not include the first gate's latency")

    async def test_an_idle_arrival_is_not_gated_at_all(self):
        _Patch(self, _FakeGate())
        state = pr.DaemonState()
        await pr._enqueue_inbound(state, self.args, lambda *_: None, [("telegram", "morning", 0)])
        self.assertEqual(_rows(self.dir), [])

    async def test_the_gate_is_its_own_knob_not_the_routers(self):
        # --router-mode off must not switch the interleave gate off with it, and vice versa.
        _Patch(self, _FakeGate([_verdict("hold")]))
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t1", "head")
        await pr._enqueue_inbound(state, _args(self.dir, router_mode="off"), lambda *_: None,
                                  [("telegram", "also X", 0)])
        self.assertEqual(len(_rows(self.dir)), 1)


# --------------------------------------------------------------------------- the claim that matters

class ObserveChangesNothing(unittest.IsolatedAsyncioTestCase):
    """`observe` is zero behaviour change (§4.3). Same conversation, mode off and mode observe:
    identical reply, identical durable queue, identical continuity, identical prompt."""

    async def _one_turn(self, mode):
        state_dir = tempfile.mkdtemp()
        args = _args(state_dir, interleave_mode=mode)
        prompts = []

        class Recording(pr.StubWarmSession):
            def send(self, text, on_event=None, cold_retry_text=None):
                prompts.append(text)
                return super().send(text, on_event=on_event)

        state = pr.DaemonState()
        state.pending = [("telegram", "what's on my calendar tomorrow", 0)]
        state.pending_event.set()
        task = asyncio.ensure_future(
            pr.drainer_task(state, args, lambda *_: None, lambda: Recording(), 600.0))
        try:
            async with asyncio.timeout(5.0):
                while state.pending:
                    await asyncio.sleep(0.01)
        finally:
            state.stop.set()
            state.pending_event.set()
            await asyncio.wait_for(task, timeout=5.0)
        sent_path = os.path.join(state_dir, "sent.jsonl")
        with open(sent_path, encoding="utf-8") as fh:
            sent = [json.loads(line) for line in fh if line.strip()]
        return {
            "dir": state_dir,
            "prompts": prompts,
            "sent": [s["text"] for s in sent],
            "queue": pr.load_daemon_state(state_dir)["pending"],
            "thread": pr.load_json(pr.thread_path(state_dir), []),
        }

    async def test_observe_is_indistinguishable_from_off(self):
        _Patch(self, _FakeGate())
        off = await self._one_turn(il.MODE_OFF)
        observe = await self._one_turn(il.MODE_OBSERVE)
        self.assertEqual(off["prompts"], observe["prompts"], "no prompt is altered")
        self.assertEqual(off["sent"], observe["sent"], "the owner sees exactly what they see today")
        self.assertEqual(off["queue"], observe["queue"], "the durable queue is untouched")
        self.assertEqual([t["role"] for t in off["thread"]],
                         [t["role"] for t in observe["thread"]])

    async def test_mode_off_writes_no_log_at_all(self):
        _Patch(self, _FakeGate())
        off = await self._one_turn(il.MODE_OFF)
        self.assertFalse(os.path.exists(il.log_path(off["dir"])))


class LiveIsGatedByHostVersion(unittest.TestCase):
    """Phase 2 (interrupt/continue, §5) is BUILT (behind a flag) — the tests that used to assert
    nothing here implements a fold were the thing phase 2 deliberately made false. What survives from them: `live`
    is still refused rather than silently downgraded, just on a different ground (the per-host CLI
    version probe, §3.7/§3.8, rather than Q8's now-cleared evidence bar), and Q5's no-cap decision is
    still absolute — §5.1.1 forbids a per-turn limit under ANY name, and that invariant does not get a
    sunset just because the rest of phase 2 shipped."""

    def test_an_unprobed_version_still_refuses_rather_than_downgrading(self):
        # A caller who asked for a live interleave and silently got observation would believe a
        # feature was running that is not — the one outcome worse than not starting. Injects a
        # version so this doesn't depend on what `claude` happens to be installed on the test host.
        self.assertIsNotNone(il.refuse_mode(il.MODE_LIVE, version_probe=lambda: "0.0.1"))
        self.assertIn(il.MODE_LIVE, il.MODES, "the enum names it so the refusal can say why")

    def test_there_is_still_no_per_turn_fold_cap_under_any_name(self):
        # Q5 overrode the spec's own lean; §5.1.1 forbids reintroducing it as a token budget, a time
        # cap, a minimum gap, or MAX_TURN_ATTEMPTS in disguise. Phase 2 being built is exactly the
        # moment this is easiest to smuggle back in, so the check stays. `presence.py` only —
        # `interleave.py`'s own module docstring legitimately names `INTERLEAVE_MAX_PER_TURN` by
        # string to explain that it does not exist, the same reason this file avoids naming
        # pending-checks' guard string in prose.
        with open(os.path.join(SCRIPT_DIR, "presence.py"), encoding="utf-8") as fh:
            presence_src = fh.read()
        self.assertNotIn("INTERLEAVE_MAX_PER_TURN", presence_src)
        self.assertNotIn("MAX_FOLD", presence_src)

    def test_phase_2_mechanics_are_present_and_named(self):
        # The inverse of the old "nothing implements a fold" assertion, made explicit rather than
        # silently deleted — the fold path exists now, on purpose, and this says so.
        with open(os.path.join(SCRIPT_DIR, "presence.py"), encoding="utf-8") as fh:
            presence_src = fh.read()
        self.assertIn("interleave_live_send", presence_src)
        self.assertIn("fold_queue", presence_src)
        with open(os.path.join(SCRIPT_DIR, "backends", "claude_cli.py"), encoding="utf-8") as fh:
            claude_cli_src = fh.read()
        self.assertIn("control_request", claude_cli_src)
        self.assertIn("interrupt_requested", claude_cli_src)

    def test_phase_3_is_still_nothing(self):
        # §8: no second queue, no partial delivery, no streaming a half-finished answer. Phase 2 folds
        # a message into the SAME single reply; it must never grow a second delivery path.
        with open(os.path.join(SCRIPT_DIR, "presence.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("partial_delivery", src)
        self.assertNotIn("stream_reply", src)


if __name__ == "__main__":
    unittest.main()
