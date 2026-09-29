#!/usr/bin/env python3
"""Tests for the per-turn idle-gap deadline and the bounded control hold — the hung-turn deadline
design (P1/P2), the fix for "a prompt takes too long, never finishes, and comes back every time."

The bug these exist for: `WarmSession._read_until_result`'s `for raw in self.proc.stdout` had no
deadline anywhere in the stack. A child that emitted nothing and never closed blocked that read
forever, so `session_busy` stayed on, the message stayed at `pending[0]`, and MAX_TURN_ATTEMPTS —
counted correctly UP FRONT — could only advance once per daemon BOOT. Three restarts to dead-letter,
and every boot in between replayed the same prompt into the same hang. Not an unbounded loop: a loop
clocked by restarts instead of by seconds. The same wedge held every `defer_until_idle` control
forever, which is Path A silently not deploying.

What's covered, and why each one is here rather than for completeness:

  * **A long but LIVE turn is never cut.** The load-bearing test of the whole change. The deadline is
    a GAP between stream events, reset by each one — never a cap on the turn — because a legitimate
    turn runs long all the time (tool use, a big read, a Fable delegation) and a flat total cap would
    kill exactly those. This drives a turn well past 5x its own deadline and asserts it survives.
  * **A hang trips the deadline and the turn ENDS** — returning the same `None` the mid-turn-death
    path has always handled, so a timeout is an already-tested outcome and not a new branch.
  * **`attempts` advances within ONE boot** — the regression that defines the bug.
  * **Three consecutive hangs dead-letter exactly once**, with the existing set-aside wording.
  * **A hung turn keeps its message queued** while a plain mid-turn death still pops — the asymmetry
    §4 Q2 turns on (decided as shipped), pinned in both directions so neither can drift
    into the other.
  * **The fallback ladder does not fire on a timeout.** Both rungs exist for causes that fail in
    seconds; re-sending the prompt would buy two more full deadlines of hanging.
  * **Every watchdog failure path is a no-op.** A broken watchdog must never cost a turn.
  * **P2: a control held past the cap applies mid-turn.** A deploy that never lands is worse than a
    turn cut short.

Offline throughout, like the rest of the presence suites: no real `claude` spawn, no socket, no
network. The fake child is a queue-backed stdout that blocks exactly the way a pipe does.

Run:  python -m unittest seneschal.scripts.test_presence_hung_turn  (or)  python test_presence_hung_turn.py
"""
import argparse
import asyncio
import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402


# --------------------------------------------------------------------------- offline child doubles

class _FakeStdout:
    """A child's stdout pipe: blocks on read until a line is published, and ends iteration when the
    pipe closes — which is exactly what the watchdog's `kill()` produces on a real child."""

    def __init__(self):
        self._q = queue.Queue()

    def feed(self, line: str) -> None:
        self._q.put(line)

    def close(self) -> None:
        self._q.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item


class _FakeStdin:
    """Swallows the user event `_send_turn` writes; the fake child simply never answers it."""

    closed = False

    def write(self, _s):
        return None

    def flush(self):
        return None

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, kill_raises=False):
        self.stdout = _FakeStdout()
        self.stdin = _FakeStdin()
        self.killed = threading.Event()
        self.kill_raises = kill_raises

    def poll(self):
        return None

    def kill(self):
        if self.kill_raises:
            raise OSError("simulated: no such process")
        self.killed.set()
        self.stdout.close()   # a killed child's stdout closes → the read loop ends


def _session(gap, proc=None, **kw):
    """A WarmSession wired to a fake child. The constructor spawns nothing — `start()` is what would,
    and no test here calls it."""
    sess = pr.WarmSession("claude", None, "bypassPermissions", lambda *_: None, **kw)
    sess.proc = proc if proc is not None else _FakeProc()
    sess.turn_gap_sec = gap
    return sess


def _result_line(text="all done"):
    return json.dumps({"type": "result", "is_error": False, "result": text,
                       "usage": {"input_tokens": 1, "output_tokens": 1}, "num_turns": 1}) + "\n"


def _assistant_line(i):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text",
                                                                     "text": f"thinking {i}"}]}}) + "\n"


# --------------------------------------------------------------------------- P1: the watchdog itself

class IdleGapIsAGapNotACap(unittest.TestCase):
    """THE test of this change. Everything else guards a failure mode; this one guards the design."""

    def test_a_long_but_live_turn_is_never_cut(self):
        gap = 0.5
        events, interval = 30, 0.1          # 3.0 s of turn — SIX times its own deadline
        sess = _session(gap)

        def feeder():
            for i in range(events):
                time.sleep(interval)
                sess.proc.stdout.feed(_assistant_line(i))
            sess.proc.stdout.feed(_result_line("finally"))

        t = threading.Thread(target=feeder, daemon=True)
        started = time.monotonic()
        t.start()
        reply = sess._read_until_result()
        elapsed = time.monotonic() - started
        t.join(timeout=5)

        self.assertEqual(reply, "finally")          # the turn completed normally...
        self.assertFalse(sess.timed_out)            # ...and the deadline never tripped...
        self.assertFalse(sess.proc.killed.is_set())  # ...and nothing killed the child.
        # Guard against this silently degrading into a fast test that proves nothing: the turn really
        # did outlive its own deadline by a wide margin.
        self.assertGreater(elapsed, gap * 3,
                           "the turn must run well past the deadline for this test to mean anything")

    def test_silence_shorter_than_the_gap_never_trips(self):
        """A quiet stretch is fine as long as it stays under the gap — the near-miss case."""
        sess = _session(0.6)

        def feeder():
            time.sleep(0.3)                  # half the gap of silence, then life
            sess.proc.stdout.feed(_result_line("late but fine"))

        threading.Thread(target=feeder, daemon=True).start()
        self.assertEqual(sess._read_until_result(), "late but fine")
        self.assertFalse(sess.timed_out)


class HangingTurnEnds(unittest.TestCase):
    def test_a_silent_child_is_killed_and_the_turn_returns_none(self):
        sess = _session(0.15)
        started = time.monotonic()
        reply = sess._read_until_result()    # would block forever before P1
        elapsed = time.monotonic() - started

        self.assertIsNone(reply)             # the same None the mid-turn-death path already handles
        self.assertTrue(sess.timed_out)
        self.assertTrue(sess.proc.killed.is_set())
        self.assertLess(elapsed, 5.0)

    def test_a_child_that_goes_quiet_MID_turn_is_killed(self):
        """The realistic shape: the turn starts fine, streams a few events, then wedges."""
        sess = _session(0.15)
        for i in range(3):
            sess.proc.stdout.feed(_assistant_line(i))   # ...and then nothing, ever
        self.assertIsNone(sess._read_until_result())
        self.assertTrue(sess.timed_out)
        self.assertTrue(sess.proc.killed.is_set())

    def test_an_unparseable_line_still_counts_as_life(self):
        """The beat is on the LINE, not the parse: garbage is still proof the child is running."""
        sess = _session(0.5)

        def feeder():
            for _ in range(6):
                time.sleep(0.1)
                sess.proc.stdout.feed("not json at all\n")
            sess.proc.stdout.feed(_result_line("survived"))

        threading.Thread(target=feeder, daemon=True).start()
        self.assertEqual(sess._read_until_result(), "survived")
        self.assertFalse(sess.timed_out)


class WatchdogFailuresAreNoOps(unittest.TestCase):
    """A broken watchdog must never be the reason a turn is lost."""

    def test_a_kill_that_raises_is_swallowed(self):
        proc = _FakeProc(kill_raises=True)
        wd = pr._TurnWatchdog(proc, 0.1, None).start()
        deadline = time.monotonic() + 5.0
        while not wd.fired and time.monotonic() < deadline:
            time.sleep(0.01)
        wd.cancel()
        self.assertTrue(wd.fired)            # it tried, the OSError went nowhere, nothing crashed

    def test_a_log_that_raises_is_swallowed(self):
        def boom(*_):
            raise RuntimeError("the log is broken")

        proc = _FakeProc()
        wd = pr._TurnWatchdog(proc, 0.05, boom).start()
        self.assertTrue(proc.killed.wait(timeout=5))   # it still did its actual job
        wd.cancel()

    def test_a_zero_or_negative_gap_disables_it_entirely(self):
        for gap in (0, 0.0, -1, None):
            with self.subTest(gap=gap):
                proc = _FakeProc()
                wd = pr._TurnWatchdog(proc, gap, None).start()
                time.sleep(0.05)
                self.assertFalse(wd.fired)
                self.assertFalse(proc.killed.is_set())
                wd.cancel()

    def test_a_turn_with_no_hang_is_unchanged_at_the_default_gap(self):
        """Additive: at the shipped 10-minute gap a normal turn behaves exactly as it did before."""
        sess = _session(pr.TURN_IDLE_GAP_SEC)
        sess.proc.stdout.feed(_assistant_line(0))
        sess.proc.stdout.feed(_result_line("hello"))
        self.assertEqual(sess._read_until_result(), "hello")
        self.assertFalse(sess.timed_out)
        self.assertFalse(sess.proc.killed.is_set())

    def test_the_shipped_gap_is_ten_minutes(self):
        """The decided idle-gap length is 10 minutes."""
        self.assertEqual(pr.TURN_IDLE_GAP_SEC, 600.0)


class TimeoutSuppressesTheFallbackLadder(unittest.TestCase):
    """Both rungs exist for causes that fail in ~3 s. A ten-minute silence is evidence against each,
    and retrying would buy two more full deadlines of hanging before the drainer hears about it."""

    def _no_spawn(self, sess):
        starts = []
        sess.start = lambda: starts.append(1)   # a real start() would spawn `claude` — never here
        return starts

    def test_a_stale_resume_id_is_not_blamed_for_a_hang(self):
        sess = _session(0.15, resume_session_id="stale-id")
        starts = self._no_spawn(sess)
        self.assertIsNone(sess.send("hi"))
        self.assertTrue(sess.timed_out)
        self.assertEqual(starts, [])                      # no cold respawn, no re-send
        self.assertEqual(sess.resume_session_id, "stale-id")  # and the id was not discarded
        self.assertFalse(sess.resume_failed)

    def test_the_warm_model_dial_is_not_blamed_for_a_hang(self):
        sess = _session(0.15, fallback_model="claude-sonnet-5")
        sess.model = "claude-opus-5"
        starts = self._no_spawn(sess)
        self.assertIsNone(sess.send("hi"))
        self.assertTrue(sess.timed_out)
        self.assertEqual(starts, [])
        self.assertFalse(sess.did_fallback)
        self.assertEqual(sess.model, "claude-opus-5")     # dial untouched

    def test_timed_out_resets_per_send(self):
        """A session that hung once must not report a later clean turn as a timeout."""
        sess = _session(0.15)
        self.assertIsNone(sess.send("hangs"))
        self.assertTrue(sess.timed_out)
        sess.proc = _FakeProc()                           # the drainer would respawn; simulate it
        sess.proc.stdout.feed(_result_line("fine now"))
        self.assertEqual(sess.send("works"), "fine now")
        self.assertFalse(sess.timed_out)


class TimeoutNoticeWording(unittest.TestCase):
    """§4 Q3: a timeout gets its own wording, not the borrowed mid-turn-death apology. Decided as
    shipped."""

    def test_it_says_it_hung_and_not_that_it_hit_a_snag(self):
        notice = pr.turn_timeout_notice()
        self.assertIn("hung", notice)
        self.assertNotIn("snag", notice)

    def test_it_promises_nothing_it_cannot_keep(self):
        """It claims neither a retry nor a set-aside: on attempts 1-2 the retry is silent, and on the
        last one the existing dead-letter says "set it aside" itself, right after this."""
        notice = pr.turn_timeout_notice()
        self.assertNotIn("set it aside", notice)
        self.assertNotIn("try again", notice.lower())

    def test_the_duration_is_derived_from_the_live_constant(self):
        """Re-tuning the gap must not leave a notice quoting a number that is no longer true."""
        self.assertIn("10 minutes", pr.turn_timeout_notice(600.0))
        self.assertIn("2 minutes", pr.turn_timeout_notice(120.0))
        self.assertIn("30 seconds", pr.turn_timeout_notice(30.0))
        self.assertIn("10 minutes", pr.turn_timeout_notice())   # the shipped default


# --------------------------------------------------------------------------- P1: drainer routing

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


def _sent(state_dir):
    path = os.path.join(state_dir, "sent.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines() if line.strip()]


class HangingSession(pr.StubWarmSession):
    """A session whose child produces nothing at all. `send` parks on a REAL `_TurnWatchdog` with a
    short gap and returns None when it fires — the same shape `WarmSession._read_until_result`
    produces on a hang, so the drainer sees exactly what it will see in production."""

    GAP = 0.15

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.turn_gap_sec = self.GAP
        self.hangs = 0

    def send(self, text, on_event=None, cold_retry_text=None):
        self.hangs += 1
        self.timed_out = False
        proc = _FakeProc()
        wd = pr._TurnWatchdog(proc, self.turn_gap_sec, None).start()
        try:
            proc.killed.wait(timeout=5)      # blocks like the read loop does, until the deadline trips
        finally:
            wd.cancel()
        self.timed_out = wd.fired
        return None


class HungTurnRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)

    async def _drain_until(self, state, make_session, cond, timeout=20.0):
        task = asyncio.ensure_future(
            pr.drainer_task(state, self.args, lambda *_: None, make_session, 600.0))
        try:
            async with asyncio.timeout(timeout):
                while not cond():
                    await asyncio.sleep(0.01)
        finally:
            state.stop.set()
            state.pending_event.set()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_attempts_advances_within_one_boot(self):
        """THE regression. Before P1 the turn never came back, so this counter could only move once
        per daemon boot — three restarts to dead-letter, replaying the hang in between."""
        state = pr.DaemonState()
        state.pending = [("telegram", "the prompt that hangs", 0)]
        state.pending_event.set()
        await self._drain_until(state, lambda: HangingSession(),
                                lambda: state.pending and state.pending[0][2] >= 2)

        # Still queued, and the count is climbing inside this one process.
        self.assertEqual(state.pending[0][0], "telegram")
        self.assertEqual(state.pending[0][1], "the prompt that hangs")
        self.assertGreaterEqual(state.pending[0][2], 2)
        # ...and persisted, so a restart mid-climb resumes rather than restarting the count.
        st = pr.load_daemon_state(self.dir)
        self.assertGreaterEqual(st["pending"][0]["attempts"], 2)
        # The owner was told, in the timeout's own words rather than the mid-turn-death apology.
        sent = _sent(self.dir)
        self.assertTrue(sent)
        self.assertIn("hung", sent[0]["text"])
        self.assertNotIn("snag", sent[0]["text"])
        self.assertEqual(state.last_respawn_reason, pr.RESPAWN_TURN_TIMEOUT)

    async def test_three_hangs_dead_letter_exactly_once(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "wedges every time", 0)]
        state.pending_event.set()
        await self._drain_until(state, lambda: HangingSession(), lambda: not state.pending)

        sent = _sent(self.dir)
        set_aside = [m for m in sent if "set it aside" in m["text"]]
        self.assertEqual(len(set_aside), 1, f"expected exactly one dead-letter, got {len(set_aside)}")
        self.assertIn("wedges every time", set_aside[0]["text"])
        self.assertIn(str(pr.MAX_TURN_ATTEMPTS), set_aside[0]["text"])
        # It is the LAST thing said about the message, after one notice per hang.
        self.assertEqual(sent[-1]["text"], set_aside[0]["text"])
        self.assertEqual(len([m for m in sent if "hung" in m["text"]]), pr.MAX_TURN_ATTEMPTS)
        self.assertEqual(pr.load_daemon_state(self.dir)["pending"], [])   # gone from the queue for good

    async def test_a_hung_turn_keeps_its_message_but_a_dead_session_still_pops(self):
        """The asymmetry decision 2 turns on, pinned in both directions so neither can drift.

        A hang means nothing was answered, so the message stays with its attempt counted — that is
        what makes the poison pill reachable. A session that merely DIED mid-turn keeps its existing
        behaviour untouched (apologise, pop), because that path has always been able to come back
        around on its own and is not what was broken."""
        hung = pr.DaemonState()
        hung.pending = [("telegram", "hangs", 0)]
        hung.pending_event.set()
        await self._drain_until(hung, lambda: HangingSession(),
                                lambda: bool(_sent(self.dir)))
        self.assertTrue(hung.pending, "a hung turn must NOT drop the message")

        class DeadSession(pr.StubWarmSession):
            def send(self, text, on_event=None, cold_retry_text=None):
                return None                      # died — but did not hang: timed_out stays False

        dead = pr.DaemonState()
        dead.pending = [("telegram", "dies", 0)]
        dead.pending_event.set()
        await self._drain_until(dead, lambda: DeadSession(), lambda: not dead.pending)
        self.assertEqual(dead.pending, [])       # unchanged: it apologises and pops
        self.assertEqual(dead.last_respawn_reason, pr.RESPAWN_TURN_ERROR)

    async def test_a_timeout_is_not_resumable(self):
        """A hung session is the one whose state we have the most evidence against restoring."""
        self.assertNotIn(pr.RESPAWN_TURN_TIMEOUT, pr.RESUMABLE_REASONS)


# --------------------------------------------------------------------------- P2: bounded control hold

class ControlHoldIsBounded(unittest.IsolatedAsyncioTestCase):
    """A deploy that never lands is worse than a turn cut short. Before P2 a `defer_until_idle`
    control waited on `chat_idle()` with no maximum — and one hung turn made all three of its
    conditions false forever, so seneschald-update went on ff-pulling and enqueueing reloads that never
    applied. That is Path A silently not deploying — a failure class that can go unnoticed for days."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir, stub_brain=False, fake_inbox=None)
        self._cap = pr.CONTROL_MAX_HOLD_SEC

    def tearDown(self):
        pr.CONTROL_MAX_HOLD_SEC = self._cap

    def _busy_state(self):
        state = pr.DaemonState()
        state.session = pr.StubWarmSession()          # a live session...
        state.session_busy = True                     # ...with a turn in flight...
        state.pending = [("telegram", "mid-turn", 1)]  # ...and work queued: chat_idle() is false
        self.assertFalse(state.chat_idle())
        return state

    async def test_a_control_held_past_the_cap_applies_mid_turn(self):
        pr.CONTROL_MAX_HOLD_SEC = 0.3
        state = self._busy_state()
        pr.save_json(pr.control_queue_path(self.dir),
                     [{"action": "restart", "defer_until_idle": True, "reason": "merge to develop"}])
        task = asyncio.ensure_future(pr.control_task(state, self.args, lambda *_: None))
        try:
            await asyncio.wait_for(task, timeout=10.0)
        finally:
            state.stop.set()
            if not task.done():
                await asyncio.wait_for(task, timeout=5.0)

        self.assertEqual(state.pending_action, "restart")
        self.assertTrue(state.stop.is_set())
        # It applied while the turn was STILL in flight — that is the whole point.
        self.assertTrue(state.session_busy)
        self.assertTrue(state.pending)
        self.assertFalse(state.chat_idle())
        self.assertEqual(pr.load_control_queue(self.dir), [])   # consumed, not left to re-apply

    async def test_it_holds_first_and_only_applies_after_the_cap(self):
        """The cap is a backstop, not the normal path: a busy chat still defers a reload."""
        pr.CONTROL_MAX_HOLD_SEC = 3600.0
        state = self._busy_state()
        pr.save_json(pr.control_queue_path(self.dir),
                     [{"action": "restart", "defer_until_idle": True, "reason": "merge to develop"}])
        task = asyncio.ensure_future(pr.control_task(state, self.args, lambda *_: None))
        try:
            async with asyncio.timeout(5.0):
                while not state.control_pending:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            self.assertIsNone(state.pending_action)   # still held — nowhere near the cap
            self.assertIsNotNone(state.control_held_since)
        finally:
            state.stop.set()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_the_hold_clock_resets_when_the_queue_empties(self):
        """Otherwise a long-idle daemon would apply the next control instantly on an old stamp."""
        pr.CONTROL_MAX_HOLD_SEC = 3600.0
        state = self._busy_state()
        pr.save_json(pr.control_queue_path(self.dir),
                     [{"action": "restart", "defer_until_idle": True, "reason": "merge"}])
        task = asyncio.ensure_future(pr.control_task(state, self.args, lambda *_: None))
        try:
            async with asyncio.timeout(5.0):
                while state.control_held_since is None:
                    await asyncio.sleep(0.01)
                pr.save_json(pr.control_queue_path(self.dir), [])   # the control went away
                while state.control_held_since is not None:
                    await asyncio.sleep(0.01)
        finally:
            state.stop.set()
            await asyncio.wait_for(task, timeout=5.0)
        self.assertIsNone(state.control_held_since)

    def test_the_cap_sits_well_above_the_turn_deadline(self):
        """Ordering that has to hold: by the time P2 fires, any watchdog-killed turn has long since
        returned and released its worker thread — `asyncio.run` waits for its default executor at
        exit, so a restart requested while a thread is still parked on a wedged child would block on
        the way out. P2 is for the class P1 cannot see, not a second opinion on the same turn."""
        self.assertGreater(pr.CONTROL_MAX_HOLD_SEC, pr.TURN_IDLE_GAP_SEC * 2)


class SlotDrainHoldsControl(unittest.IsolatedAsyncioTestCase):
    """R1, "Drain, don't kill": a PR merging mid-slot used to kill an in-flight journal-steward run
    outright — the relaunch found
    an already-cleared journal and no-op'd, losing Phases 2-6 silently. A restart/shutdown must now ALSO
    hold while `state.slot_children` is non-empty, additional to (never a replacement for) the existing
    chat-idle gate, bounded on its own cap so a wedged slot can't block a deploy forever."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir, stub_brain=False, fake_inbox=None)
        self._cap = pr.SLOT_DRAIN_MAX_HOLD_SEC

    def tearDown(self):
        pr.SLOT_DRAIN_MAX_HOLD_SEC = self._cap

    def _idle_state(self):
        state = pr.DaemonState()
        self.assertTrue(state.chat_idle())   # the chat-idle gate is satisfied throughout this class —
        return state                         # only the slot gate is under test

    async def test_a_slot_child_holds_the_control(self):
        state = self._idle_state()
        state.slot_children["daily-journal"] = object()
        pr.save_json(pr.control_queue_path(self.dir),
                     [{"action": "restart", "defer_until_idle": True, "reason": "merge to develop"}])
        task = asyncio.ensure_future(pr.control_task(state, self.args, lambda *_: None))
        try:
            async with asyncio.timeout(5.0):
                while not state.control_pending:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            self.assertIsNone(state.pending_action)     # held — the slot hasn't finished
            self.assertIsNotNone(state.slot_drain_held_since)
        finally:
            state.stop.set()
            if not task.done():
                await asyncio.wait_for(task, timeout=5.0)

    async def test_it_applies_the_moment_the_slot_clears(self):
        state = self._idle_state()
        state.slot_children["daily-journal"] = object()
        pr.save_json(pr.control_queue_path(self.dir),
                     [{"action": "restart", "defer_until_idle": True, "reason": "merge to develop"}])
        task = asyncio.ensure_future(pr.control_task(state, self.args, lambda *_: None))
        try:
            async with asyncio.timeout(5.0):
                while not state.control_pending:
                    await asyncio.sleep(0.01)
            self.assertIsNone(state.pending_action)
            del state.slot_children["daily-journal"]     # reap_finished_slots would do this on exit 0
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            state.stop.set()
            if not task.done():
                await asyncio.wait_for(task, timeout=5.0)

        self.assertEqual(state.pending_action, "restart")
        self.assertEqual(pr.load_control_queue(self.dir), [])   # consumed, not left to re-apply

    async def test_a_wedged_slot_is_forced_through_after_the_cap(self):
        """The cap is a backstop, not the normal path — same shape as the chat-idle cap test above."""
        pr.SLOT_DRAIN_MAX_HOLD_SEC = 0.3
        state = self._idle_state()
        state.slot_children["daily-journal"] = object()   # never cleared — the wedge
        pr.save_json(pr.control_queue_path(self.dir),
                     [{"action": "restart", "defer_until_idle": True, "reason": "merge to develop"}])
        task = asyncio.ensure_future(pr.control_task(state, self.args, lambda *_: None))
        try:
            await asyncio.wait_for(task, timeout=10.0)
        finally:
            state.stop.set()
            if not task.done():
                await asyncio.wait_for(task, timeout=5.0)

        self.assertEqual(state.pending_action, "restart")
        self.assertIn("daily-journal", state.slot_children)   # still there — it was NEVER reaped,
        self.assertEqual(pr.load_control_queue(self.dir), [])  # only the deploy stopped waiting for it

    async def test_no_slot_children_behaves_exactly_as_before(self):
        """Chat behaviour is unchanged: an idle chat with no slot in flight applies immediately, same
        as pre-R1."""
        state = self._idle_state()
        self.assertEqual(state.slot_children, {})
        pr.save_json(pr.control_queue_path(self.dir),
                     [{"action": "restart", "defer_until_idle": True, "reason": "merge to develop"}])
        task = asyncio.ensure_future(pr.control_task(state, self.args, lambda *_: None))
        await asyncio.wait_for(task, timeout=5.0)

        self.assertEqual(state.pending_action, "restart")
        self.assertIsNone(state.slot_drain_held_since)

    async def test_a_busy_chat_still_gates_exactly_as_before_with_no_slot(self):
        """The existing chat-idle gate is untouched by this change when no slot is in flight."""
        pr.CONTROL_MAX_HOLD_SEC_before = pr.CONTROL_MAX_HOLD_SEC
        try:
            pr.CONTROL_MAX_HOLD_SEC = 3600.0
            state = pr.DaemonState()
            state.session = pr.StubWarmSession()
            state.session_busy = True
            state.pending = [("telegram", "mid-turn", 1)]
            self.assertFalse(state.chat_idle())
            pr.save_json(pr.control_queue_path(self.dir),
                         [{"action": "restart", "defer_until_idle": True, "reason": "merge"}])
            task = asyncio.ensure_future(pr.control_task(state, self.args, lambda *_: None))
            try:
                async with asyncio.timeout(5.0):
                    while not state.control_pending:
                        await asyncio.sleep(0.01)
                await asyncio.sleep(0.1)
                self.assertIsNone(state.pending_action)
                self.assertIsNotNone(state.control_held_since)
                self.assertIsNone(state.slot_drain_held_since)   # the new gate never engaged
            finally:
                state.stop.set()
                if not task.done():
                    await asyncio.wait_for(task, timeout=5.0)
        finally:
            pr.CONTROL_MAX_HOLD_SEC = pr.CONTROL_MAX_HOLD_SEC_before
            del pr.CONTROL_MAX_HOLD_SEC_before

    def test_the_cap_is_named_and_bounded(self):
        """A wedged slot must never hold a deploy forever — the cap must be a positive, finite module
        constant, distinct from the chat-idle cap it sits beside."""
        self.assertGreater(pr.SLOT_DRAIN_MAX_HOLD_SEC, 0)
        self.assertLess(pr.SLOT_DRAIN_MAX_HOLD_SEC, float("inf"))


if __name__ == "__main__":
    unittest.main()
