#!/usr/bin/env python3
"""Tests for presence.py's cockpit-pipe wiring — the sixth supervised task's glue to DaemonState (see
seneschal/docs/asyncio-daemon-design.md, seneschal/docs/cockpit-spec.md "The daemon pipe"). Complements
test_cockpit_pipe.py (which covers cockpit_pipe.py's transport-agnostic protocol/PipeHub in isolation):
this file covers how presence.py wires that module in — status snapshots, the transcript tee, the
inbox-fallback drain, the `cockpit_task` callback wiring, and the `deliver_reply` cockpit branch.

Runs WITHOUT the ``websockets`` dependency installed (like test_cockpit_pipe.py / test_discord_gateway.py)
— the one test that exercises `cockpit_task`'s live wiring monkeypatches `cockpit_pipe.websockets` to a
non-None sentinel and stubs `run_pipe_server`, so it never needs a real socket or the real dependency.

Run:  python -m unittest seneschal.scripts.test_presence_cockpit   (or)   python test_presence_cockpit.py
"""
import argparse
import asyncio
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import cockpit_pipe as cp  # noqa: E402
import governor as gv  # noqa: E402
import presence as pr  # noqa: E402


def _args(state_dir, **over):
    """A minimal args namespace for exercising presence.py's cockpit wiring directly (mirrors
    test_presence_async.py's helper, plus the cockpit-specific flags)."""
    base = dict(state_dir=state_dir, telegram_env="unused.env", call_env=None, discord_env=None,
                no_discord=True, no_discord_gateway=False, stub_brain=True, stub_send=True,
                fake_inbox=None, router_mode="off",
                no_reminders=True, no_peek=True, no_slots=True, max_iterations=0,
                poll_timeout=1, discord_poll_sec=0.05, tick_sec=0.05, idle_min=0.0,
                model=None, slot_model=None, slot_catchup_min=180, claude_bin="claude",
                notion_mcp=None, slack_mcp=None, permission_mode="bypassPermissions",
                peek_interval_min=0, watch_prompt=None, watch_cmd=None, watch_model=None,
                no_cockpit=False, cockpit_port=cp.DEFAULT_PORT)
    base.update(over)
    return argparse.Namespace(**base)


class _FakeHub:
    """Duck-typed stand-in for cockpit_pipe.PipeHub's outbound side — records every frame handed to
    either broadcast method (thread-safety itself is PipeHub's own concern, covered in
    test_cockpit_pipe.py; here we're only checking presence.py calls the right method with the right
    payload at the right moments)."""

    def __init__(self):
        self.sent = []

    def broadcast(self, frame):
        self.sent.append(frame)

    def broadcast_threadsafe(self, frame, loop):
        self.sent.append(frame)


class _BrokenHub:
    def broadcast(self, frame):
        raise RuntimeError("boom")

    def broadcast_threadsafe(self, frame, loop):
        raise RuntimeError("boom")


class StatusSnapshot(unittest.TestCase):
    def test_idle_snapshot(self):
        state = pr.DaemonState()
        args = _args(tempfile.mkdtemp(), model="opus")
        self.assertEqual(pr._status_snapshot(state, args),
                         {"session_up": False, "turn_in_flight": False, "model": "opus", "queue_depth": 0})

    def test_busy_snapshot_prefers_session_model(self):
        state = pr.DaemonState()
        state.session = pr.StubWarmSession()
        state.session.model = "sonnet"
        state.session_busy = True
        state.pending = [("telegram", "x", 0), ("cockpit", "y", 0)]
        args = _args(tempfile.mkdtemp(), model="opus")   # the args default — session's own model wins
        snap = pr._status_snapshot(state, args)
        self.assertEqual(snap, {"session_up": True, "turn_in_flight": True, "model": "sonnet",
                               "queue_depth": 2})

    def test_falls_back_to_args_model_when_session_has_none(self):
        state = pr.DaemonState()
        state.session = pr.StubWarmSession()  # model defaults to None
        args = _args(tempfile.mkdtemp(), model="opus")
        self.assertEqual(pr._status_snapshot(state, args)["model"], "opus")


class PushCockpitStatus(unittest.TestCase):
    def test_noop_without_a_hub(self):
        state = pr.DaemonState()
        args = _args(tempfile.mkdtemp())
        pr._push_cockpit_status(state, args)  # must not raise

    def test_broadcasts_a_status_frame(self):
        state = pr.DaemonState()
        state.cockpit_hub = _FakeHub()
        args = _args(tempfile.mkdtemp(), model="opus")
        pr._push_cockpit_status(state, args)
        self.assertEqual(len(state.cockpit_hub.sent), 1)
        frame = state.cockpit_hub.sent[0]
        self.assertEqual(frame["type"], cp.TYPE_STATUS)
        self.assertEqual(frame["model"], "opus")

    def test_a_broken_hub_never_raises(self):
        state = pr.DaemonState()
        state.cockpit_hub = _BrokenHub()
        args = _args(tempfile.mkdtemp())
        pr._push_cockpit_status(state, args)  # must not raise (fail-open)


class TeeChatEvent(unittest.TestCase):
    def test_appends_ring_buffer_and_broadcasts(self):
        d = tempfile.mkdtemp()
        state = pr.DaemonState()
        state.cockpit_hub = _FakeHub()
        args = _args(d)
        ev = cp.chat_event("turn_started", source="telegram")
        pr._tee_chat_event(state, args, lambda *_: None, ev)
        tail = cp.read_transcript_tail(d)
        self.assertEqual([e["kind"] for e in tail], ["turn_started"])
        self.assertEqual(state.cockpit_hub.sent, [ev])

    def test_ring_buffer_still_written_when_hub_is_broken(self):
        d = tempfile.mkdtemp()
        state = pr.DaemonState()
        state.cockpit_hub = _BrokenHub()
        args = _args(d)
        pr._tee_chat_event(state, args, lambda *_: None, cp.chat_event("turn_started"))
        self.assertEqual(len(cp.read_transcript_tail(d)), 1)  # fail-open: the tee still landed

    def test_threadsafe_sibling_also_appends_and_broadcasts(self):
        d = tempfile.mkdtemp()
        state = pr.DaemonState()
        state.cockpit_hub = _FakeHub()
        args = _args(d)
        ev = cp.chat_event("turn_done", source="cockpit")
        pr._tee_chat_event_threadsafe(state, args, lambda *_: None, ev)
        self.assertEqual(len(cp.read_transcript_tail(d)), 1)
        self.assertEqual(state.cockpit_hub.sent, [ev])


class MakeStreamTee(unittest.TestCase):
    def test_converts_and_tees_a_result_event(self):
        d = tempfile.mkdtemp()
        state = pr.DaemonState()
        state.session = pr.StubWarmSession()
        state.session.model = "opus"
        state.cockpit_hub = _FakeHub()
        args = _args(d)
        on_event = pr._make_stream_tee(state, args, lambda *_: None, "telegram", "turn-abc123")
        on_event({"type": "result", "is_error": False, "result": "the answer"})
        tail = cp.read_transcript_tail(d)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["kind"], "turn_done")
        self.assertEqual(tail[0]["model"], "opus")
        self.assertEqual(tail[0]["source"], "telegram")
        self.assertEqual(tail[0]["turn_id"], "turn-abc123")

    def test_skips_uninteresting_events(self):
        d = tempfile.mkdtemp()
        state = pr.DaemonState()
        args = _args(d)
        on_event = pr._make_stream_tee(state, args, lambda *_: None, "telegram", "turn-xyz")
        on_event({"type": "system", "subtype": "init"})
        self.assertEqual(cp.read_transcript_tail(d), [])

    def test_never_raises_on_a_malformed_event(self):
        d = tempfile.mkdtemp()
        state = pr.DaemonState()
        args = _args(d)
        on_event = pr._make_stream_tee(state, args, lambda *_: None, "telegram", "turn-xyz")
        on_event("not a dict")  # must not raise

    def test_result_event_with_usage_meters_the_governor_ledger(self):
        d = tempfile.mkdtemp()
        state = pr.DaemonState()
        state.session = pr.StubWarmSession()
        state.session.model = "claude-opus-4-8"
        args = _args(d)
        on_event = pr._make_stream_tee(state, args, lambda *_: None, "telegram", "turn-1")
        on_event({"type": "result", "is_error": False, "result": "ok",
                  "usage": {"input_tokens": 100, "output_tokens": 50}})
        recs = [r for r in gv._read_ledger(d) if r.get("kind") == "tokens"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["model"], "claude-opus-4-8")
        self.assertEqual(recs[0]["tokens"], 150)

    def test_result_event_without_usage_meters_nothing(self):
        d = tempfile.mkdtemp()
        state = pr.DaemonState()
        state.session = pr.StubWarmSession()
        state.session.model = "opus"
        args = _args(d)
        on_event = pr._make_stream_tee(state, args, lambda *_: None, "telegram", "turn-1")
        on_event({"type": "result", "is_error": False, "result": "ok"})
        self.assertEqual([r for r in gv._read_ledger(d) if r.get("kind") == "tokens"], [])

    def test_non_result_event_never_meters(self):
        d = tempfile.mkdtemp()
        state = pr.DaemonState()
        state.session = pr.StubWarmSession()
        args = _args(d)
        on_event = pr._make_stream_tee(state, args, lambda *_: None, "telegram", "turn-1")
        on_event({"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "hi"}]}})
        self.assertEqual(gv._read_ledger(d), [])


class GovernorMeterTurnUsage(unittest.TestCase):
    """Oikonomos (v3.5): the per-turn metering + self-push alert hook `_make_stream_tee` calls on every
    `turn_done` event. Exercised directly (not just through `_make_stream_tee`) for the alert-push path,
    which needs `pr.send_telegram` monkeypatched (same pattern as DeliverReplyCockpitBranch below)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sent = []
        self._orig_send_telegram = pr.send_telegram

        def fake_send(text, telegram_env):
            self.sent.append(text)
            return {"ok": True}

        pr.send_telegram = fake_send

    def tearDown(self):
        pr.send_telegram = self._orig_send_telegram

    def test_ignores_non_dict_usage(self):
        args = _args(self.dir)
        pr._governor_meter_turn_usage(args, lambda *_: None, "opus", None)
        self.assertEqual(gv._read_ledger(self.dir), [])

    def test_ignores_zero_or_negative_token_totals(self):
        args = _args(self.dir)
        pr._governor_meter_turn_usage(args, lambda *_: None, "opus", {"input_tokens": 0})
        self.assertEqual(gv._read_ledger(self.dir), [])

    def test_sums_every_known_token_field(self):
        args = _args(self.dir)
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 2,
                 "cache_read_input_tokens": 3}
        pr._governor_meter_turn_usage(args, lambda *_: None, "opus", usage)
        recs = gv._read_ledger(self.dir)
        self.assertEqual(recs[0]["tokens"], 20)

    def test_fires_one_alert_when_daily_budget_threshold_crossed(self):
        gv.save(self.dir, {"daily_token_budget_by_model": {"opus": 100}})
        args = _args(self.dir)
        pr._governor_meter_turn_usage(args, lambda *_: None, "opus", {"input_tokens": 90})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("opus", self.sent[0])

    def test_no_alert_when_under_threshold(self):
        gv.save(self.dir, {"daily_token_budget_by_model": {"opus": 1000}})
        args = _args(self.dir)
        pr._governor_meter_turn_usage(args, lambda *_: None, "opus", {"input_tokens": 10})
        self.assertEqual(self.sent, [])

    def test_alert_not_repeated_within_realert_window(self):
        gv.save(self.dir, {"daily_token_budget_by_model": {"opus": 100}})
        args = _args(self.dir)
        pr._governor_meter_turn_usage(args, lambda *_: None, "opus", {"input_tokens": 90})
        pr._governor_meter_turn_usage(args, lambda *_: None, "opus", {"input_tokens": 5})
        self.assertEqual(len(self.sent), 1)  # second call crosses threshold again but is deduped

    def test_a_failed_send_is_not_recorded_as_alerted(self):
        gv.save(self.dir, {"daily_token_budget_by_model": {"opus": 100}})
        pr.send_telegram = lambda text, env: {"ok": False, "error": "boom"}
        args = _args(self.dir)
        pr._governor_meter_turn_usage(args, lambda *_: None, "opus", {"input_tokens": 90})
        # a failed send must not be dedupe-recorded — the alert is still due on the next turn
        pr.send_telegram = self._orig_send_telegram
        self.sent = []

        def fake_send_2(text, telegram_env):
            self.sent.append(text)
            return {"ok": True}

        pr.send_telegram = fake_send_2
        pr._governor_meter_turn_usage(args, lambda *_: None, "opus", {"input_tokens": 1})
        self.assertEqual(len(self.sent), 1)

    def test_unknown_model_falls_back_to_literal_unknown(self):
        args = _args(self.dir)
        pr._governor_meter_turn_usage(args, lambda *_: None, None, {"input_tokens": 5})
        recs = gv._read_ledger(self.dir)
        self.assertEqual(recs[0]["model"], "unknown")


class DeliverReplyCockpitBranch(unittest.TestCase):
    def test_always_succeeds(self):
        args = _args(tempfile.mkdtemp())
        self.assertTrue(pr.deliver_reply("cockpit", "hi", args, lambda *_: None))

    def test_never_touches_telegram_or_discord_senders(self):
        args = _args(tempfile.mkdtemp())

        def boom(*_a, **_kw):
            raise AssertionError("a cockpit reply must never call the telegram/discord senders")

        orig_t, orig_d = pr.send_telegram, pr.send_discord
        pr.send_telegram, pr.send_discord = boom, boom
        try:
            self.assertTrue(pr.deliver_reply("cockpit", "hi", args, lambda *_: None))
        finally:
            pr.send_telegram, pr.send_discord = orig_t, orig_d


class DrainCockpitInbox(unittest.IsolatedAsyncioTestCase):
    async def test_drains_fallback_file_into_pending(self):
        d = tempfile.mkdtemp()
        args = _args(d)
        state = pr.DaemonState()
        cp.append_inbox(d, {"id": "x1", "text": "hi via the fallback inbox"})
        await pr._drain_cockpit_inbox(state, args, lambda *_: None)
        self.assertEqual(state.pending, [("cockpit", "hi via the fallback inbox", 0)])
        # nothing new appended — a second drain enqueues nothing further
        await pr._drain_cockpit_inbox(state, args, lambda *_: None)
        self.assertEqual(len(state.pending), 1)

    async def test_no_cockpit_flag_leaves_the_file_untouched(self):
        d = tempfile.mkdtemp()
        args = _args(d, no_cockpit=True)
        state = pr.DaemonState()
        cp.append_inbox(d, {"id": "y1", "text": "should stay queued"})
        await pr._drain_cockpit_inbox(state, args, lambda *_: None)
        self.assertEqual(state.pending, [])
        self.assertEqual(cp.drain_inbox(d), [{"id": "y1", "text": "should stay queued"}])

    async def test_a_broken_inbox_file_does_not_raise(self):
        d = tempfile.mkdtemp()
        os.makedirs(d, exist_ok=True)
        with open(cp.inbox_path(d), "w", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        args = _args(d)
        state = pr.DaemonState()
        await pr._drain_cockpit_inbox(state, args, lambda *_: None)  # must not raise
        self.assertEqual(state.pending, [])

    def test_force_fable_item_is_synthesized_with_the_trigger(self):
        # v3: the fallback-inbox path honors force_fable exactly like the live pipe's on_chat_send —
        # same "!fable" synthesis, so apply_force_route's one detection point covers both.
        text = pr._cockpit_inbox_text({"id": "z1", "text": "hello", "force_fable": True})
        self.assertEqual(text, "!fable hello")

    def test_no_force_fable_item_is_untouched(self):
        self.assertEqual(pr._cockpit_inbox_text({"id": "z2", "text": "hello"}), "hello")

    def test_force_fable_does_not_double_prefix_an_already_prefixed_message(self):
        text = pr._cockpit_inbox_text({"id": "z3", "text": "!fable already tagged", "force_fable": True})
        self.assertEqual(text, "!fable already tagged")

    async def test_force_fable_from_the_fallback_inbox_reaches_the_queue(self):
        d = tempfile.mkdtemp()
        args = _args(d)
        state = pr.DaemonState()
        cp.append_inbox(d, {"id": "z4", "text": "draft the plan", "force_fable": True})
        await pr._drain_cockpit_inbox(state, args, lambda *_: None)
        self.assertEqual(len(state.pending), 1)
        channel, queued_text, attempts = state.pending[0]
        self.assertEqual(channel, "cockpit")
        self.assertIn("force-fable", queued_text)
        self.assertTrue(queued_text.endswith("draft the plan"))


class CockpitTaskGating(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_flags_never_set_a_hub(self):
        for over in (dict(stub_brain=False, no_cockpit=True), dict(stub_brain=True),
                    dict(stub_brain=False, fake_inbox="x.json")):
            state = pr.DaemonState()
            args = _args(tempfile.mkdtemp(), **over)
            await pr.cockpit_task(state, args, lambda *_: None)
            self.assertIsNone(state.cockpit_hub)

    async def test_missing_websockets_disables_the_pipe(self):
        state = pr.DaemonState()
        args = _args(tempfile.mkdtemp(), stub_brain=False)
        orig = cp.websockets
        cp.websockets = None  # force pipe_available() False regardless of the real environment
        try:
            await pr.cockpit_task(state, args, lambda *_: None)
        finally:
            cp.websockets = orig
        self.assertIsNone(state.cockpit_hub)


class CockpitTaskWiring(unittest.IsolatedAsyncioTestCase):
    """Exercises cockpit_task's callback wiring end-to-end WITHOUT a real socket: `websockets` is
    monkeypatched to a non-None sentinel (pipe_available() -> True) and `run_pipe_server` is stubbed to
    just capture the constructed PipeHub and wait on `stop` — the same shape the real one holds a live
    server with. This is the seam between presence.py and cockpit_pipe.py; PipeHub's own frame-handling
    logic is covered in test_cockpit_pipe.py."""

    async def test_chat_send_status_get_and_control_restart_all_wire_through(self):
        d = tempfile.mkdtemp()
        args = _args(d, stub_brain=False)
        state = pr.DaemonState()
        captured = {}

        async def fake_run_pipe_server(hub, host, port, stop, log):
            captured["hub"] = hub
            captured["host"] = host
            captured["port"] = port
            await stop.wait()

        orig_ws, orig_run = cp.websockets, cp.run_pipe_server
        cp.websockets = object()  # any non-None sentinel
        cp.run_pipe_server = fake_run_pipe_server
        try:
            task = asyncio.ensure_future(pr.cockpit_task(state, args, lambda *_: None))
            async with asyncio.timeout(5.0):
                while "hub" not in captured:
                    await asyncio.sleep(0.01)
            self.assertIsNotNone(state.cockpit_hub)
            self.assertEqual(captured["host"], "127.0.0.1")
            self.assertEqual(captured["port"], args.cockpit_port)
            hub = captured["hub"]

            # chat.send -> the SAME action queue Telegram/Discord use, source="cockpit". v3:
            # force_fable now ACTUALLY forces — the enqueued text carries the MUST-delegate directive,
            # not the raw message (see presence.apply_force_route / FORCE_FABLE_DIRECTIVE).
            await hub.on_chat_send("m1", "hello from the browser", {"force_fable": True})
            self.assertEqual(len(state.pending), 1)
            queued_channel, queued_text, queued_attempts = state.pending[0]
            self.assertEqual(queued_channel, "cockpit")
            self.assertEqual(queued_attempts, 0)
            self.assertIn("force-fable", queued_text)
            self.assertTrue(queued_text.endswith("hello from the browser"))

            # status.get -> the daemon's own live snapshot.
            snap = hub.on_status_get()
            self.assertEqual(snap["queue_depth"], 1)
            self.assertFalse(snap["turn_in_flight"])

            # control.restart -> the same control-queue path request_control.py / the cockpit REST
            # route use — a graceful, deferred restart.
            await hub.on_control_restart()
            queued = pr.load_control_queue(d)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0]["action"], "restart")
            self.assertTrue(queued[0]["defer_until_idle"])

            state.stop.set()
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            cp.websockets = orig_ws
            cp.run_pipe_server = orig_run
        self.assertIsNone(state.cockpit_hub)  # cleared once the pipe-server coroutine returns

    async def test_blank_text_chat_send_is_never_enqueued(self):
        d = tempfile.mkdtemp()
        args = _args(d, stub_brain=False)
        state = pr.DaemonState()
        captured = {}

        async def fake_run_pipe_server(hub, host, port, stop, log):
            captured["hub"] = hub
            await stop.wait()

        orig_ws, orig_run = cp.websockets, cp.run_pipe_server
        cp.websockets = object()
        cp.run_pipe_server = fake_run_pipe_server
        try:
            task = asyncio.ensure_future(pr.cockpit_task(state, args, lambda *_: None))
            async with asyncio.timeout(5.0):
                while "hub" not in captured:
                    await asyncio.sleep(0.01)
            await captured["hub"].on_chat_send("m2", "hello", {})
            self.assertEqual(state.pending, [("cockpit", "hello", 0)])  # no force_fable -> untouched
            state.stop.set()
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            cp.websockets = orig_ws
            cp.run_pipe_server = orig_run


class DrainerCockpitTurnTee(unittest.IsolatedAsyncioTestCase):
    """End-to-end (offline, stub brain): a cockpit-origin turn streams turn_started + turn_done
    chat.events (both to the ring buffer AND the live hub) and pushes turn-in-flight status, exactly
    like a Telegram/Discord turn — the cockpit is just a third `channel`."""

    async def test_cockpit_turn_streams_started_and_done_events(self):
        d = tempfile.mkdtemp()
        args = _args(d)
        state = pr.DaemonState()
        state.cockpit_hub = _FakeHub()
        state.loop = asyncio.get_running_loop()
        state.pending = [("cockpit", "hello from the browser", 0)]
        state.pending_event.set()
        task = asyncio.ensure_future(
            pr.drainer_task(state, args, lambda *_: None, lambda: pr.StubWarmSession(), 600.0))
        try:
            async with asyncio.timeout(5.0):
                while state.pending:
                    await asyncio.sleep(0.01)
        finally:
            state.stop.set()
            state.pending_event.set()
            await asyncio.wait_for(task, timeout=5.0)

        events = [f for f in state.cockpit_hub.sent if f.get("type") == cp.TYPE_CHAT_EVENT]
        self.assertIn("turn_started", [e["kind"] for e in events])
        self.assertIn("turn_done", [e["kind"] for e in events])
        # Both ends of the turn carry the SAME turn_id (the only correlator the protocol has — see
        # presence._make_stream_tee) — this is what lets the chat pane group events into one turn.
        turn_ids = {e.get("turn_id") for e in events}
        self.assertEqual(len(turn_ids), 1)
        self.assertIsNotNone(next(iter(turn_ids)))

        statuses = [f for f in state.cockpit_hub.sent if f.get("type") == cp.TYPE_STATUS]
        self.assertTrue(any(s.get("turn_in_flight") for s in statuses))
        self.assertTrue(any(not s.get("turn_in_flight") for s in statuses))

        tail_kinds = [e.get("kind") for e in cp.read_transcript_tail(d)]
        self.assertIn("turn_started", tail_kinds)
        self.assertIn("turn_done", tail_kinds)

        # Delivered without any external send (there is none for cockpit) — continuity still recorded.
        thread = pr.load_json(pr.thread_path(d), [])
        self.assertEqual([t["role"] for t in thread], ["assistant"])
        self.assertEqual(pr.load_daemon_state(d)["pending"], [])


if __name__ == "__main__":
    unittest.main()
