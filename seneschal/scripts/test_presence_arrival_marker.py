#!/usr/bin/env python3
"""Tests for the **mid-turn arrival marker** — the mid-turn interleave spec's §8(a′), phase 1,
decided as Q2 and approved independently of phase 0.

The whole feature is one prompt-only line, so these tests are about the two places it could go wrong
rather than about the wording:

  * **it fires on exactly the right messages** — a message that landed while a turn was in flight
    gets marked, an idle-time arrival does not, and the note is consumed by the turn that answers it;
  * **it touches nothing else** — the durable queue, the conversation corpus, the thread tail and the
    delivered reply are byte-identical to a run without it. The marker is a LOCAL variable on one
    prompt, the same discipline `fable_hints` already holds, and a test that only checked the prompt
    would miss the failure mode that matters (a hint leaking into the persisted retry text).

Stdlib ``unittest`` only. Nothing here touches the real ``state/`` or sends anything: `--stub-send`
plus a temp dir, the standing rule for this suite.

Run:  python -m unittest seneschal.scripts.test_presence_arrival_marker
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


def _args(state_dir, **over):
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


class RecordingSession(pr.StubWarmSession):
    """A stub that keeps every prompt it was handed — the marker is prompt-only, so the prompt is the
    only place its presence or absence can be observed."""

    prompts: list = []

    def send(self, text, on_event=None, cold_retry_text=None):
        RecordingSession.prompts.append(text)
        return super().send(text, on_event=on_event)


async def _run_drainer_until(state, args, cond, timeout=5.0):
    task = asyncio.ensure_future(
        pr.drainer_task(state, args, lambda *_: None, lambda: RecordingSession(), 600.0))
    try:
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.01)
    finally:
        state.stop.set()
        state.pending_event.set()
        await asyncio.wait_for(task, timeout=5.0)


# --------------------------------------------------------------------------- the pure half

class ArrivalNoteBookkeeping(unittest.TestCase):
    def test_take_returns_the_marker_and_consumes_the_note(self):
        notes = []
        pr.note_midturn_arrival(notes, "also, I'm going to have lunch")
        self.assertEqual(pr.take_arrival_marker(notes, "also, I'm going to have lunch"),
                         pr.ARRIVAL_MARKER_LINE)
        self.assertEqual(notes, [])

    def test_an_unnoted_message_gets_nothing(self):
        notes = []
        pr.note_midturn_arrival(notes, "one")
        self.assertIsNone(pr.take_arrival_marker(notes, "two"))
        self.assertEqual(notes, ["one"], "a miss must not consume someone else's note")

    def test_two_identical_mid_turn_messages_consume_one_note_each(self):
        notes = []
        pr.note_midturn_arrival(notes, "yes")
        pr.note_midturn_arrival(notes, "yes")
        self.assertIsNotNone(pr.take_arrival_marker(notes, "yes"))
        self.assertIsNotNone(pr.take_arrival_marker(notes, "yes"))
        self.assertIsNone(pr.take_arrival_marker(notes, "yes"))

    def test_the_list_is_capped_so_a_stranded_note_ages_out(self):
        # The dead-letter guard pops the queue without ever building a prompt, so its entry is never
        # consumed. Capped FIFO is the leak bound; the cost of dropping one is a missing hint.
        notes = []
        for i in range(pr.MIDTURN_ARRIVAL_CAP + 10):
            pr.note_midturn_arrival(notes, f"m{i}")
        self.assertEqual(len(notes), pr.MIDTURN_ARRIVAL_CAP)
        self.assertIsNone(pr.take_arrival_marker(notes, "m0"), "oldest aged out")
        self.assertIsNotNone(pr.take_arrival_marker(notes, f"m{pr.MIDTURN_ARRIVAL_CAP + 9}"))

    def test_turn_in_flight_reads_the_claimed_head(self):
        state = pr.DaemonState()
        self.assertFalse(pr.turn_in_flight(state))
        state.inflight_text = "answering this"
        self.assertTrue(pr.turn_in_flight(state))


# --------------------------------------------------------------------------- intake

class IntakeMarksOnlyMidTurnArrivals(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)

    async def test_idle_arrival_is_not_marked(self):
        state = pr.DaemonState()
        await pr._enqueue_inbound(state, self.args, lambda *_: None, [("telegram", "morning", 0)])
        self.assertEqual(state.midturn_arrivals, [])

    async def test_arrival_during_a_turn_is_marked(self):
        state = pr.DaemonState()
        state.inflight_text = "the message the owner is waiting on"
        await pr._enqueue_inbound(state, self.args, lambda *_: None,
                                  [("telegram", "oh, and also X", 0)])
        self.assertEqual(state.midturn_arrivals, ["oh, and also X"])

    async def test_every_message_in_a_mid_turn_batch_is_marked(self):
        state = pr.DaemonState()
        state.inflight_text = "in flight"
        await pr._enqueue_inbound(state, self.args, lambda *_: None,
                                  [("telegram", "one", 0), ("telegram", "two", 0)])
        self.assertEqual(state.midturn_arrivals, ["one", "two"])

    async def test_the_note_holds_the_POST_transform_text(self):
        # The queue holds post-force-route text and the drainer claims that same string, so the note
        # has to be keyed to it or the marker never finds its message.
        state = pr.DaemonState()
        state.inflight_text = "in flight"
        await pr._enqueue_inbound(state, self.args, lambda *_: None,
                                  [("telegram", "!fable draft the Q3 plan", 0)])
        self.assertEqual(len(state.midturn_arrivals), 1)
        self.assertEqual(state.midturn_arrivals[0], state.pending[0][1])
        self.assertIn(pr.FORCE_FABLE_DIRECTIVE, state.midturn_arrivals[0])


# --------------------------------------------------------------------------- the drainer

class MarkerRidesExactlyOnePrompt(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)
        RecordingSession.prompts = []

    async def test_a_marked_message_carries_the_line_into_its_prompt(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "oh, and also X", 0)]
        pr.note_midturn_arrival(state.midturn_arrivals, "oh, and also X")
        state.pending_event.set()
        await _run_drainer_until(state, self.args, lambda: not state.pending)
        self.assertEqual(len(RecordingSession.prompts), 1)
        self.assertIn(pr.ARRIVAL_MARKER_LINE, RecordingSession.prompts[0])
        self.assertIn("oh, and also X", RecordingSession.prompts[0])
        self.assertEqual(state.midturn_arrivals, [], "the note is consumed by the turn that used it")

    async def test_an_unmarked_message_prompt_is_untouched(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "morning", 0)]
        state.pending_event.set()
        await _run_drainer_until(state, self.args, lambda: not state.pending)
        self.assertNotIn(pr.ARRIVAL_MARKER_LINE, RecordingSession.prompts[0])

    async def test_the_marker_never_reaches_the_durable_queue_or_the_corpus(self):
        # The failure mode worth a test: a prompt-only hint that leaks into the persisted text would
        # be re-sent on every retry, and would enter `turns.jsonl` as something the owner said.
        state = pr.DaemonState()
        state.pending = [("telegram", "and merge the example.com change, please", 0)]
        pr.note_midturn_arrival(state.midturn_arrivals, "and merge the example.com change, please")
        state.pending_event.set()
        await _run_drainer_until(state, self.args, lambda: not state.pending)
        thread = pr.load_json(pr.thread_path(self.dir), [])
        self.assertTrue(all(pr.ARRIVAL_MARKER_LINE not in t.get("text", "") for t in thread))
        turns_path = os.path.join(self.dir, "turns.jsonl")
        if os.path.exists(turns_path):
            with open(turns_path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        self.assertNotIn(pr.ARRIVAL_MARKER_LINE, json.loads(line).get("text") or "")

    async def test_a_retry_after_a_failed_delivery_goes_out_unmarked(self):
        # Documented, deliberate: the note is consumed by the first prompt built for it, exactly like
        # `fable_hints.pop(0)`. Holding text-keyed notes open across retries is how one starts
        # marking the wrong message. Pinned so a later change is a decision rather than an accident.
        state = pr.DaemonState()
        state.pending = [("telegram", "flaky wire", 0)]
        pr.note_midturn_arrival(state.midturn_arrivals, "flaky wire")
        state.pending_event.set()
        calls = {"n": 0}
        orig = pr.deliver_reply

        def failing_deliver(channel, reply, args, log, retries=1, **_kw):
            calls["n"] += 1
            return False

        orig_sleep = pr._sleep_or_stop

        async def _no_backoff(_state, _sec):
            await asyncio.sleep(0)

        pr.deliver_reply = failing_deliver
        pr._sleep_or_stop = _no_backoff  # the branch's real 5 s backoff, skipped — not under test
        try:
            await _run_drainer_until(state, self.args, lambda: calls["n"] >= 2)
        finally:
            pr.deliver_reply = orig
            pr._sleep_or_stop = orig_sleep
        self.assertGreaterEqual(len(RecordingSession.prompts), 2)
        self.assertIn(pr.ARRIVAL_MARKER_LINE, RecordingSession.prompts[0])
        self.assertNotIn(pr.ARRIVAL_MARKER_LINE, RecordingSession.prompts[1])

    async def test_marker_and_fable_hint_compose(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "map out the migration", 0)]
        pr.note_midturn_arrival(state.midturn_arrivals, "map out the migration")
        state.fable_hints.append(pr.FABLE_HINT_LINE)
        state.pending_event.set()
        await _run_drainer_until(state, self.args, lambda: not state.pending)
        prompt = RecordingSession.prompts[0]
        self.assertIn(pr.ARRIVAL_MARKER_LINE, prompt)
        self.assertIn(pr.FABLE_HINT_LINE, prompt)
        self.assertIn("map out the migration", prompt)


if __name__ == "__main__":
    unittest.main()
