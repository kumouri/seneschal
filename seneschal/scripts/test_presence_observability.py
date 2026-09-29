#!/usr/bin/env python3
"""Tests for the warm session's observability layer (presence.py) — step 1 of the warm-session
lifetime research.

The research's recommendation was "stay short-lived, but MEASURE first, then revisit lifetime with data".
Everything here is that measurement: the context proxy, the per-turn metrics rows, the widened status
snapshot, the respawn-reason attribution, and the local `!status` command.

The context arithmetic is the part most worth pinning down, because the obvious implementation is
wrong. The claude CLI's terminal `result` event reports usage **summed across the turn's iterations**,
and `usage.iterations` is a ONE-element list holding that same aggregate rather than per-call detail.
Measured against 229 live turns: a `num_turns: 41` turn reports ~3.09M input tokens. Rendering that
against a 200k window would show 1546% full. Dividing by `num_turns` recovers a context-sized, stable
figure. These tests lock that in so nobody "simplifies" the divisor away.

Stdlib ``unittest`` only; no live `claude`, no network, no ``websockets``.
Run:  python -m unittest seneschal.scripts.test_presence_observability
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

import cockpit_pipe as cp  # noqa: E402
import presence as pr  # noqa: E402


def _args(state_dir, **over):
    """Minimal args namespace (mirrors test_presence_cockpit.py's helper)."""
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


def _usage(input_tokens=2, cache_read=120_000, cache_creation=700, output=250, iterations=None):
    """A shape-accurate usage block. `iterations` defaults to the real CLI's one-element aggregate."""
    u = {"input_tokens": input_tokens, "output_tokens": output,
         "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_creation}
    u["iterations"] = [dict(u)] if iterations is None else iterations
    return u


def _sent(state_dir):
    path = os.path.join(state_dir, "sent.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines() if line.strip()]


def _metrics(state_dir):
    path = os.path.join(state_dir, pr.METRICS_FILE)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines() if line.strip()]


class ContextProxy(unittest.TestCase):
    """The divisor is the whole point — see the module docstring."""

    def test_single_iteration_turn_is_exact(self):
        # num_turns == 1 (over half of observed turns): the sum IS the end-of-turn context.
        self.assertEqual(pr.context_tokens_from_usage(_usage(), 1), 120_702)

    def test_multi_iteration_turn_is_divided_by_num_turns(self):
        # The failure this guards: a 41-iteration turn reporting ~3.09M would render at 1546% full.
        big = _usage(input_tokens=41 * 2, cache_read=41 * 120_000, cache_creation=41 * 700)
        self.assertEqual(pr.context_tokens_from_usage(big, 41), 120_702)

    def test_output_tokens_are_excluded(self):
        # Only the INPUT side fills a context window on the next call.
        quiet = pr.context_tokens_from_usage(_usage(output=10), 1)
        chatty = pr.context_tokens_from_usage(_usage(output=9_000), 1)
        self.assertEqual(quiet, chatty)

    def test_genuine_per_iteration_detail_is_used_directly(self):
        # Future-proofing: if the CLI ever reports real per-call entries, the LAST one is the final
        # call's own context and must NOT be divided again.
        u = _usage(iterations=[{"input_tokens": 1, "cache_read_input_tokens": 50_000},
                               {"input_tokens": 1, "cache_read_input_tokens": 90_000}])
        self.assertEqual(pr.context_tokens_from_usage(u, 2), 90_001)

    def test_missing_num_turns_falls_back_to_one(self):
        self.assertEqual(pr.context_tokens_from_usage(_usage(), None), 120_702)
        self.assertEqual(pr.context_tokens_from_usage(_usage(), "nonsense"), 120_702)

    def test_zero_num_turns_never_divides_by_zero(self):
        self.assertEqual(pr.context_tokens_from_usage(_usage(), 0), 120_702)

    def test_tolerates_junk(self):
        for junk in (None, "usage", 7, [], {}, {"iterations": "no"}):
            self.assertIsNone(pr.context_tokens_from_usage(junk, 1))

    def test_empty_iterations_list_falls_back_to_top_level(self):
        # Two of 229 observed turns carried `iterations: []`.
        u = _usage()
        u["iterations"] = []
        self.assertEqual(pr.context_tokens_from_usage(u, 1), 120_702)

    def test_context_pct(self):
        # Assert the ARITHMETIC against the constant, not a number that restates the window. This test
        # used to pin `context_pct(100_000) == 50.0`, which was a claim about the denominator rather
        # than about the function, and it went red when the 200k assumption was corrected to the
        # model's real 1M — a correct change failing a test that had frozen the mistake.
        w = pr.CONTEXT_WINDOW_TOKENS
        self.assertEqual(pr.context_pct(w // 2), 50.0)
        self.assertEqual(pr.context_pct(w), 100.0)
        self.assertIsNone(pr.context_pct(None))
        self.assertIsNone(pr.context_pct(True))  # bool is not a token count

    def test_the_largest_observed_context_is_not_over_full(self):
        # The behavioural claim of the context-window correction, stated as the measurement that
        # forced it: across 867 turns / 99 warm sessions the biggest context seen was 350,525 tokens.
        # Under the old 200k denominator that read 175% and the gauge looked catastrophic; under the
        # real window it is a third full. Goes red if anyone reinstates the smaller number.
        self.assertLess(pr.context_pct(350_525), 100.0)


class SessionVitals(unittest.TestCase):
    def test_formats_a_live_session(self):
        s = pr.StubWarmSession()
        s.start()
        s.started_at -= 125  # pretend it's been up ~2 minutes
        s.turns_served = 3
        s.context_tokens = 74_443
        s.session_cost_usd = 1.234
        line = pr._session_vitals(s)
        self.assertIn("age 2m05s", line)
        self.assertIn("3 turns", line)
        self.assertIn("ctx ~74k", line)
        self.assertIn("$1.23", line)

    def test_singular_turn(self):
        s = pr.StubWarmSession()
        s.start()
        s.turns_served = 1
        self.assertIn("1 turn ", pr._session_vitals(s) + " ")

    def test_never_raises_on_a_bare_object(self):
        class Bare:
            pass
        self.assertEqual(pr._session_vitals(Bare()), "no vitals")


class RecordTurnUsage(unittest.TestCase):
    """WarmSession._record_turn_usage — captures what the result event already carried."""

    def _session(self):
        return pr.WarmSession("claude", "claude-opus-5", "bypassPermissions", lambda *_a, **_k: None)

    def test_captures_context_and_cumulative_cost(self):
        ws = self._session()
        ws._record_turn_usage({"type": "result", "usage": _usage(), "num_turns": 1,
                               "total_cost_usd": 0.9})
        self.assertEqual(ws.context_tokens, 120_702)
        self.assertEqual(ws.session_cost_usd, 0.9)
        self.assertEqual(ws.last_turn_cost_usd, 0.9)   # first turn: cumulative == this turn
        self.assertEqual(ws.last_num_turns, 1)

    def test_per_turn_cost_is_the_delta_of_a_cumulative_total(self):
        ws = self._session()
        ws._record_turn_usage({"usage": _usage(), "num_turns": 1, "total_cost_usd": 0.90})
        ws._record_turn_usage({"usage": _usage(), "num_turns": 1, "total_cost_usd": 1.22})
        self.assertAlmostEqual(ws.last_turn_cost_usd, 0.32, places=6)
        self.assertEqual(ws.session_cost_usd, 1.22)

    def test_non_monotonic_cost_never_reports_a_negative_turn(self):
        ws = self._session()
        ws._record_turn_usage({"usage": _usage(), "total_cost_usd": 5.0})
        ws._record_turn_usage({"usage": _usage(), "total_cost_usd": 1.0})
        self.assertEqual(ws.last_turn_cost_usd, 1.0)
        self.assertGreaterEqual(ws.last_turn_cost_usd, 0)

    def test_junk_event_is_swallowed(self):
        ws = self._session()
        ws._record_turn_usage({"usage": "not a dict", "total_cost_usd": "free"})  # must not raise
        self.assertIsNone(ws.context_tokens)

    def test_start_resets_per_process_counters(self):
        s = pr.StubWarmSession()
        s.start()
        s.send("hi")
        self.assertEqual(s.turns_served, 1)
        s.start()  # a spawn-fallback respawn is a NEW process — it has served nothing
        self.assertEqual(s.turns_served, 0)
        self.assertIsNone(s.context_tokens)


class TurnMetrics(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)
        self.state = pr.DaemonState()
        self.state.session = pr.StubWarmSession()
        self.state.session.start()
        self.state.session.model = "claude-opus-5"

    def _tee_a_turn(self, is_error=False):
        ev = cp.chat_event("turn_done", source="telegram", model="claude-opus-5", is_error=is_error,
                           usage=_usage(), num_turns=1, duration_ms=30_512, total_cost_usd=0.31)
        pr._append_turn_metrics(self.state, self.args, lambda *_: None, "telegram", "abc123", ev)

    def test_writes_one_row_per_turn(self):
        self._tee_a_turn()
        self._tee_a_turn()
        self.assertEqual(len(_metrics(self.dir)), 2)

    def test_row_shape(self):
        self.state.session.turns_served = 4
        self.state.session.context_tokens = 120_702
        self.state.session.session_cost_usd = 1.24
        self.state.session.last_turn_cost_usd = 0.31
        self._tee_a_turn()
        row = _metrics(self.dir)[0]
        self.assertEqual(row["mode"], "Chat")
        self.assertEqual(row["model"], "claude-opus-5")
        self.assertEqual(row["source"], "telegram")
        self.assertEqual(row["turn_id"], "abc123")
        self.assertEqual(row["outcome"], "Success")
        self.assertEqual(row["writer"], "daemon")
        self.assertEqual(row["turns_served"], 4)
        self.assertEqual(row["context_tokens"], 120_702)
        self.assertEqual(row["num_turns"], 1)
        self.assertEqual(row["duration_ms"], 30_512)
        self.assertEqual(row["cost_usd"], 0.31)
        self.assertEqual(row["session_cost_usd"], 1.24)
        # The raw usage is kept deliberately: context_tokens is an ESTIMATE for num_turns > 1, so a
        # later analysis must be able to recompute rather than inherit this module's arithmetic.
        self.assertEqual(row["usage"]["cache_read_input_tokens"], 120_000)

    def test_tokens_field_matches_the_cockpit_usage_readers_contract(self):
        # cockpit/server/readers.py::_extract_tokens checks a TOP-LEVEL `tokens` first, and read_usage
        # buckets on `ts` + `model` — so the Usage panel lights up with no reader change. This test is
        # the contract between the two files.
        self._tee_a_turn()
        row = _metrics(self.dir)[0]
        self.assertEqual(row["tokens"], 2 + 250 + 700 + 120_000)  # in + out + both cache halves
        self.assertTrue(row["ts"].endswith("Z"))
        self.assertIn("model", row)

    def test_failed_turn_is_recorded_as_failed(self):
        self._tee_a_turn(is_error=True)
        self.assertEqual(_metrics(self.dir)[0]["outcome"], "Failed")

    def test_unwritable_state_dir_never_raises(self):
        args = _args(os.path.join(self.dir, "does", "not", "exist"))
        logged = []
        pr._append_turn_metrics(self.state, args, logged.append, "telegram", "t", cp.chat_event(
            "turn_done", source="telegram", usage=_usage()))
        self.assertTrue(logged)  # complained, did not raise

    def test_stream_tee_writes_a_row_end_to_end(self):
        # The real wiring: _make_stream_tee -> turn_done -> metrics row (next to governor metering).
        tee = pr._make_stream_tee(self.state, self.args, lambda *_: None, "telegram", "t1")
        tee({"type": "result", "is_error": False, "result": "ok", "usage": _usage(),
             "num_turns": 1, "duration_ms": 900, "total_cost_usd": 0.05})
        rows = _metrics(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn_id"], "t1")

    def test_row_context_is_self_consistent_with_its_own_usage(self):
        """The row must describe ITSELF, not whatever the session happened to hold when the tee ran.
        Regression guard: the session's counters are deliberately set to a DIFFERENT, stale value."""
        self.state.session.context_tokens = 999_999
        self._tee_a_turn()
        row = _metrics(self.dir)[0]
        self.assertEqual(row["context_tokens"], pr.context_tokens_from_usage(_usage(), 1))
        self.assertNotEqual(row["context_tokens"], 999_999)


class TurnBookkeepingOrder(unittest.TestCase):
    """A turn's own numbers must be booked BEFORE the tee runs.

    The tee writes the per-turn metrics row and reads the session's counters, so with the tee first
    every row described the state *before* the turn it documented. The first row this ever produced
    on the live host said `turns_served: 0` on a successful turn, with no context and no cost — which
    is how the bug was found. These tests pin the ordering.
    """

    def _session(self):
        return pr.WarmSession("claude", "claude-opus-5", "bypassPermissions", lambda *_a, **_k: None)

    def _drive(self, events, on_event):
        ws = self._session()
        ws.proc = _FakeProc(events)
        return ws._read_until_result(on_event=on_event)

    def test_tee_sees_this_turns_counters_not_the_previous_ones(self):
        seen = {}
        ws = self._session()
        ws.proc = _FakeProc([{"type": "result", "is_error": False, "result": "hi",
                              "usage": _usage(), "num_turns": 1, "total_cost_usd": 0.25}])
        ws._read_until_result(on_event=lambda ev: seen.update(
            turns_served=ws.turns_served, context=ws.context_tokens, cost=ws.last_turn_cost_usd))
        self.assertEqual(seen["turns_served"], 1)          # was 0 before the fix
        self.assertEqual(seen["context"], 120_702)          # was None before the fix
        self.assertEqual(seen["cost"], 0.25)                # was None before the fix

    def test_a_failed_turn_does_not_count_as_served_but_still_records_usage(self):
        seen = {}
        ws = self._session()
        ws.proc = _FakeProc([{"type": "result", "is_error": True, "result": "boom",
                              "usage": _usage(), "num_turns": 1, "total_cost_usd": 0.10}])
        reply = ws._read_until_result(on_event=lambda ev: seen.update(
            turns_served=ws.turns_served, context=ws.context_tokens))
        self.assertIsNone(reply)
        self.assertEqual(seen["turns_served"], 0)   # errored turns are not "served"
        self.assertEqual(seen["context"], 120_702)  # ...but what it cost is still worth seeing

    def test_init_event_still_captures_the_session_id(self):
        ws = self._session()
        ws.proc = _FakeProc([{"type": "system", "subtype": "init", "session_id": "s-42",
                              "apiKeySource": "none"},
                             {"type": "result", "is_error": False, "result": "ok"}])
        self.assertEqual(ws._read_until_result(), "ok")
        self.assertEqual(ws.session_id, "s-42")


class _FakeStdin:
    def __init__(self):
        self.closed = False
        self.written = []

    def write(self, s):
        self.written.append(s)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, events):
        self.stdin = _FakeStdin()
        self.stdout = iter([json.dumps(ev) + "\n" for ev in events])

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


class StatusCommandParsing(unittest.TestCase):
    def test_recognises_the_command(self):
        for good in ("!status", "/status", "  !status  ", "!STATUS", "/Status"):
            self.assertTrue(pr.is_status_command(good), good)

    def test_ignores_a_question_that_merely_starts_with_it(self):
        # "!status of the build work" is a question for the assistant, not a daemon command.
        for bad in ("!status of the build work", "what's the status?", "status", "!fable",
                    "", None, "tell me !status"):
            self.assertFalse(pr.is_status_command(bad), repr(bad))


class StatusCommandNeverForceRoutes(unittest.TestCase):
    """A left-on "Send to Fable" toggle must not turn `!status` into `!fable !status` — that would
    spend a Fable delegation asking a model to guess at the daemon's own vitals. Both cockpit intake
    paths (live pipe and the degraded file-queue fallback) carry the same guard."""

    def test_inbox_fallback_does_not_prefix_a_status_command(self):
        self.assertEqual(_cockpit_text({"text": "!status", "force_fable": True}), "!status")

    def test_inbox_fallback_still_prefixes_a_real_prompt(self):
        out = _cockpit_text({"text": "draft the Q3 plan", "force_fable": True})
        self.assertTrue(pr.strip_force_fable(out)[0])


def _cockpit_text(item):
    return pr._cockpit_inbox_text(item)


class StatusRendering(unittest.TestCase):
    def _snap(self, **over):
        base = {"session_up": True, "turn_in_flight": False, "model": "claude-opus-5",
                "queue_depth": 0, "session_age_sec": 725.0, "turns_served": 4,
                "context_tokens": 120_702, "context_pct": 60.4, "session_cost_usd": 1.24,
                "last_respawn_reason": None, "spawn_fallback_used": False}
        base.update(over)
        return base

    def test_live_session(self):
        out = pr.render_status_reply(self._snap())
        self.assertIn("up · claude-opus-5", out)
        self.assertIn("age 12m05s", out)
        self.assertIn("4 turns", out)
        self.assertIn("ctx ~120k (60.4% est.)", out)   # ALWAYS labelled — it's a proxy
        self.assertIn("$1.24", out)
        self.assertIn("0 queued", out)
        self.assertIn("nothing in flight", out)

    def test_down_session_says_why_it_ended(self):
        # The "silence is ambiguous" fix: a clean wind-down and a session erroring out every turn look
        # identical from outside without this.
        out = pr.render_status_reply(self._snap(session_up=False,
                                                last_respawn_reason=pr.RESPAWN_TURN_ERROR))
        self.assertIn("down (last ended: turn error)", out)
        self.assertIn("dial claude-opus-5", out)

    def test_down_without_a_reason_is_still_renderable(self):
        out = pr.render_status_reply({"session_up": False, "model": "opus", "queue_depth": 2})
        self.assertIn("down ·", out)
        self.assertIn("2 queued", out)

    def test_flags_a_spawn_fallback(self):
        self.assertIn("spawn-fallback", pr.render_status_reply(self._snap(spawn_fallback_used=True)))

    def test_mid_turn(self):
        self.assertIn("mid-turn", pr.render_status_reply(self._snap(turn_in_flight=True)))


class StatusCommandHandling(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)
        self.state = pr.DaemonState()

    async def test_answers_without_spawning_or_queueing(self):
        # The core promise: !status costs no turn, needs no warm session, and never enters the queue.
        await pr._enqueue_inbound(self.state, self.args, lambda *_: None,
                                  [("telegram", "!status", 0)])
        self.assertEqual(self.state.pending, [])
        self.assertIsNone(self.state.session)
        sent = _sent(self.dir)
        self.assertEqual(len(sent), 1)
        self.assertIn("Warm session:", sent[0]["text"])

    async def test_does_not_pollute_the_thread_tail(self):
        # thread_tail is only 6 turns of continuity — a status check must never evict a real one.
        await pr._enqueue_inbound(self.state, self.args, lambda *_: None,
                                  [("telegram", "!status", 0)])
        self.assertEqual(pr.thread_tail(self.dir).strip(), "")

    async def test_real_messages_alongside_it_still_queue(self):
        await pr._enqueue_inbound(self.state, self.args, lambda *_: None,
                                  [("telegram", "!status", 0), ("telegram", "what's on today?", 0)])
        self.assertEqual([t for _c, t, _a, _tp in self.state.pending], ["what's on today?"])
        self.assertIn("what's on today?", pr.thread_tail(self.dir))

    async def test_reports_a_live_session_when_there_is_one(self):
        self.state.session = pr.StubWarmSession()
        self.state.session.start()
        self.state.session.model = "claude-opus-5"
        self.state.session.turns_served = 2
        await pr._enqueue_inbound(self.state, self.args, lambda *_: None,
                                  [("telegram", "!status", 0)])
        self.assertIn("up · claude-opus-5", _sent(self.dir)[0]["text"])

    async def test_cockpit_gets_a_synthetic_turn_instead_of_a_dead_command(self):
        # deliver_reply is a no-op for cockpit turns (their delivery IS the transcript stream), so
        # without this the command would silently do nothing in the browser.
        hub = _RecordingHub()
        self.state.cockpit_hub = hub
        self.state.loop = asyncio.get_running_loop()
        await pr._enqueue_inbound(self.state, self.args, lambda *_: None, [("cockpit", "!status", 0)])
        kinds = [f.get("kind") for f in hub.sent if f.get("type") == cp.TYPE_CHAT_EVENT]
        self.assertEqual(kinds, ["turn_started", "assistant_output", "turn_done"])
        out = [f for f in hub.sent if f.get("kind") == "assistant_output"][0]
        self.assertIn("Warm session:", out["text"])
        self.assertEqual(out["model"], "local")  # no model ran; the badge must not imply otherwise


class _RecordingHub:
    def __init__(self):
        self.sent = []

    def broadcast(self, frame):
        self.sent.append(frame)

    def broadcast_threadsafe(self, frame, loop):
        self.sent.append(frame)


class RespawnReasons(unittest.IsolatedAsyncioTestCase):
    """Attribution for why the last warm session ended — the other half of "silence is ambiguous"."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)

    async def _drain_once(self, state, make_session, idle_sec=600.0, until=None):
        task = asyncio.ensure_future(
            pr.drainer_task(state, self.args, lambda *_: None, make_session, idle_sec))
        try:
            async with asyncio.timeout(5.0):
                while not (until or (lambda: not state.pending))():
                    await asyncio.sleep(0.01)
        finally:
            state.stop.set()
            state.pending_event.set()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_starts_unset(self):
        self.assertIsNone(pr.DaemonState().last_respawn_reason)

    async def test_turn_error_is_attributed(self):
        class DeadSession(pr.StubWarmSession):
            def send(self, text, on_event=None):
                return None  # died mid-turn

        state = pr.DaemonState()
        state.pending = [("telegram", "hey", 0)]
        await self._drain_once(state, lambda: DeadSession())
        self.assertEqual(state.last_respawn_reason, pr.RESPAWN_TURN_ERROR)
        self.assertIsNone(state.session)

    async def test_idle_winddown_is_attributed(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "hey", 0)]
        # idle_sec = 0 → the session winds down on the very next empty-queue pass.
        await self._drain_once(state, lambda: pr.StubWarmSession(), idle_sec=0.0,
                               until=lambda: state.last_respawn_reason is not None)
        self.assertEqual(state.last_respawn_reason, pr.RESPAWN_IDLE)

    async def test_undelivered_reply_is_attributed(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "hey", 0)]
        args = _args(self.dir, stub_send=False)   # force deliver_reply down the real path...
        task = asyncio.ensure_future(
            pr.drainer_task(state, args, lambda *_: None, lambda: pr.StubWarmSession(), 600.0))
        try:
            async with asyncio.timeout(5.0):
                # ...where send_telegram against a nonexistent env can't deliver, so the reply is
                # kept queued and the session is dropped so the retry re-grounds cleanly.
                while state.last_respawn_reason is None:
                    await asyncio.sleep(0.01)
        finally:
            state.stop.set()
            state.pending_event.set()
            await asyncio.wait_for(task, timeout=5.0)
        self.assertEqual(state.last_respawn_reason, pr.RESPAWN_UNDELIVERED)
        self.assertTrue(state.pending)  # never dropped — it retries


class SnapshotWiring(unittest.TestCase):
    def test_live_session_fields(self):
        state = pr.DaemonState()
        state.session = pr.StubWarmSession()
        state.session.start()
        state.session.model = "claude-opus-5"
        state.session.send("hi")     # one stub turn: sets turns_served / context / cost
        snap = pr._status_snapshot(state, _args(tempfile.mkdtemp()))
        self.assertTrue(snap["session_up"])
        self.assertEqual(snap["turns_served"], 1)
        self.assertEqual(snap["context_tokens"], 61_504)   # stub's shape-accurate canned usage
        self.assertEqual(snap["context_pct"], pr.context_pct(61_504))
        self.assertEqual(snap["context_window_tokens"], pr.CONTEXT_WINDOW_TOKENS)
        self.assertTrue(snap["context_estimated"])         # never rendered as exact
        self.assertIsNotNone(snap["session_age_sec"])
        self.assertEqual(snap["session_cost_usd"], 0.04)

    def test_respawn_reason_survives_the_session_it_describes(self):
        state = pr.DaemonState()
        state.last_respawn_reason = pr.RESPAWN_IDLE
        snap = pr._status_snapshot(state, _args(tempfile.mkdtemp()))
        self.assertFalse(snap["session_up"])
        self.assertEqual(snap["last_respawn_reason"], pr.RESPAWN_IDLE)


if __name__ == "__main__":
    unittest.main()
