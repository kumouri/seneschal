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
from unittest import mock

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


class _BlockingStdin:
    """A stdin pipe whose reader never reads: `write` blocks — exactly as a real full pipe does — until
    the child is killed, then fails the way a dead reader makes it fail (`OSError`, which on Windows is
    EINVAL rather than BrokenPipeError)."""

    closed = False

    def __init__(self, proc):
        self._proc = proc
        self.entered = threading.Event()

    def write(self, _s):
        self.entered.set()
        self._proc.killed.wait()
        raise OSError(22, "Invalid argument")

    def flush(self):
        return None

    def close(self):
        self.closed = True


def _send_bounded(sess, text, bound):
    """Run `send()` on a daemon thread and give up after `bound` seconds, so a regression FAILS here
    instead of hanging the whole suite the way it hung the live daemon."""
    box = {}

    def run():
        box["reply"] = sess.send(text)

    t = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    t.start()
    t.join(timeout=bound)
    return t.is_alive(), box.get("reply"), time.monotonic() - started


class AWriteThatNeverLandsIsCovered(unittest.TestCase):
    """A resumed warm session with a large context was handed a ~4.7 KB job-finished prompt and
    produced nothing for most of half an hour; the idle gap never fired and only a manual `reseneschald` freed it.
    The CLI's own transcript has no `queue-operation` row for that message at all — the child never
    read its stdin. The watchdog was armed in `_read_until_result`, i.e. AFTER `stdin.write`+`flush`,
    and a write larger than the pipe buffer blocks until the reader reads. A child wedged before it
    starts reading stdin therefore parked the drainer inside the write with no deadline anywhere.
    The idle gap has to cover the write too."""

    def test_a_write_the_child_never_reads_is_killed_by_the_gap(self):
        proc = _FakeProc()
        proc.stdin = _BlockingStdin(proc)
        sess = _session(0.2, proc=proc)
        hung, reply, elapsed = _send_bounded(sess, "a prompt the child never reads", bound=5.0)

        self.assertFalse(hung, "send() is still blocked in stdin.write — the idle gap does not cover it")
        self.assertTrue(proc.stdin.entered.is_set())
        self.assertIsNone(reply)
        self.assertTrue(sess.timed_out, "a killed write must report as the hang it is, not a session death")
        self.assertTrue(proc.killed.is_set())
        self.assertLess(elapsed, 5.0)

    def test_the_real_pipe_blocks_and_the_gap_still_ends_the_turn(self):
        """The same thing against a REAL child and a REAL pipe, so this pins the mechanism and not just
        a fake's opinion of it: the child is alive and silent and never reads stdin (a CLI wedged in
        startup), and the prompt is far bigger than any OS's pipe buffer (4 KB Windows default, 64 KB
        Linux), so the write genuinely blocks."""
        import subprocess
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.addCleanup(lambda: (child.poll() is None and child.kill()))
        sess = _session(1.0, proc=child)
        hung, reply, elapsed = _send_bounded(sess, "x" * 2_000_000, bound=20.0)
        if hung:
            child.kill()  # unblock the stuck thread before failing, so it doesn't outlive the test

        self.assertFalse(hung, "send() is still blocked writing to a child that never reads stdin")
        self.assertIsNone(reply)
        self.assertTrue(sess.timed_out)
        # wait(), not poll(): TerminateProcess is asynchronous, so the blocked write can fail before
        # the handle reads as exited — poll() here flaked 4 runs in 8 on Windows, on pristine develop.
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self.assertIsNotNone(child.poll(), "the watchdog must have killed the child")


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


# --------------------------------------------------------------------------- the outside backstop

class _NoWatchdog:
    """A `_TurnWatchdog` with a bug that keeps it from ever firing — the failure the backstop exists
    for. Same interface, does nothing."""

    def __init__(self, *_a, **_kw):
        self.fired = False

    def start(self):
        return self

    def beat(self):
        return None

    def cancel(self):
        return None


class _WaitableFakeProc(_FakeProc):
    """`_FakeProc` plus what `WarmSession.close()` calls on the way out."""

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.kill()


class FakeChildSession(pr.WarmSession):
    """A REAL `WarmSession` — its real send(), _send_turn() and fallback ladder — over a fake child.
    `start()` hands it a fresh fake child instead of spawning `claude`; `script(proc)`, if given, runs
    on a thread against each child to play its stdout.

    Every child is also killed after `SAFETY_KILL_SEC` no matter what, so a regression that leaves
    BOTH deadlines dead FAILS these tests instead of parking a worker thread the event loop then waits
    on forever at exit — which is the very wedge under test, and would hang the suite."""

    SAFETY_KILL_SEC = 10.0

    def __init__(self, gap, script=None):
        super().__init__("claude", None, "bypassPermissions", lambda *_: None)
        self.turn_gap_sec = gap
        self.script = script
        self.children = []

    def start(self):
        self.started_at = time.monotonic()
        self.proc = _WaitableFakeProc()
        self.children.append(self.proc)
        safety = threading.Timer(self.SAFETY_KILL_SEC, self.proc.stdout.close)
        safety.daemon = True
        safety.start()
        if self.script is not None:
            threading.Thread(target=self.script, args=(self.proc,), daemon=True).start()


BACKSTOP_ALERT = "Turn backstop fired"


class BackstopNeverCompetesWithTheGap(unittest.TestCase):
    """The backstop fires only if a turn has no output for 30 min, so it can only catch a failed
    watchdog and never competes with the 10-min idle gap. The 2x floor is what makes "never competes"
    true for any gap, not just today's."""

    def test_the_shipped_backstop_is_thirty_minutes(self):
        self.assertEqual(pr.TURN_BACKSTOP_SEC, 30 * 60.0)

    def test_the_backstop_is_at_least_twice_every_shipped_gap(self):
        self.assertGreaterEqual(pr.TURN_BACKSTOP_SEC, 2 * pr.TURN_IDLE_GAP_SEC)
        self.assertGreaterEqual(pr.TURN_BACKSTOP_SEC, 2 * pr.codex_cli.CODEX_TURN_GAP_SEC)

    def test_a_longer_gap_drags_the_backstop_with_it(self):
        sess = pr.StubWarmSession()
        sess.turn_gap_sec = 2 * pr.TURN_BACKSTOP_SEC        # a hypothetical gap past 15 minutes
        self.assertEqual(pr.turn_backstop_sec(sess), 2 * sess.turn_gap_sec)

    def test_a_disabled_or_unreadable_gap_leaves_the_plain_backstop(self):
        for gap in (0, -1, None, "nonsense"):
            sess = pr.StubWarmSession()
            sess.turn_gap_sec = gap
            self.assertEqual(pr.turn_backstop_sec(sess), pr.TURN_BACKSTOP_SEC, gap)


class BackstopRouting(unittest.IsolatedAsyncioTestCase):
    """The backstop through the real drainer, against a real WarmSession over a fake child."""

    BACKSTOP = 1.0   # "1 seconds" in the notice; well clear of the 2x floor on every gap used here

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)
        self.logs = []
        for name, value in (("TURN_BACKSTOP_SEC", self.BACKSTOP), ("TURN_BACKSTOP_RELEASE_SEC", 30.0)):
            patcher = mock.patch.object(pr, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _break_the_watchdog(self):
        patcher = mock.patch.object(pr.claude_cli, "_TurnWatchdog", _NoWatchdog)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _drain_until(self, state, make_session, cond, timeout=20.0):
        task = asyncio.ensure_future(
            pr.drainer_task(state, self.args, self.logs.append, make_session, 600.0))
        try:
            async with asyncio.timeout(timeout):
                while not cond():
                    await asyncio.sleep(0.01)
        finally:
            state.stop.set()
            state.pending_event.set()
            await asyncio.wait_for(task, timeout=5.0)

    def _texts(self):
        return [m["text"] for m in _sent(self.dir)]

    async def _assert_the_backstop_ends_a_silent_turn(self, sess):
        state = pr.DaemonState()
        state.pending = [("telegram", "the prompt that wedges", 0)]
        state.pending_event.set()
        notice = pr.turn_timeout_notice(self.BACKSTOP)
        await self._drain_until(
            state, lambda: sess,
            lambda: any(BACKSTOP_ALERT in t for t in self._texts()) and notice in self._texts())

        texts = self._texts()
        self.assertTrue(sess.children[0].killed.is_set(), "the backstop must kill the silent child")
        self.assertIn(notice, texts, "the turn ends on the EXISTING timeout notice, quoting the backstop")
        self.assertFalse([t for t in texts if "snag" in t], "a backstop kill is a hang, not a death")
        self.assertEqual(state.last_respawn_reason, pr.RESPAWN_TURN_TIMEOUT)
        self.assertTrue(state.pending, "a hung turn keeps its message queued")
        self.assertEqual(state.pending[0][1], "the prompt that wedges")
        self.assertTrue(any(line.startswith("!! TURN BACKSTOP fired") for line in self.logs), self.logs)
        self.assertTrue(any("killed by the TURN BACKSTOP" in line for line in self.logs), self.logs)

    async def test_it_fires_when_the_watchdog_is_disabled(self):
        await self._assert_the_backstop_ends_a_silent_turn(FakeChildSession(gap=0))

    async def test_it_fires_when_the_watchdog_is_broken(self):
        self._break_the_watchdog()
        await self._assert_the_backstop_ends_a_silent_turn(FakeChildSession(gap=0.15))

    async def test_it_never_fires_while_output_keeps_flowing(self):
        """A turn three times the backstop's length, never silent for long — it must survive whole,
        even with the watchdog broken, because silence is the only thing the backstop measures."""
        self._break_the_watchdog()
        reply = "[[channel:main]]\n*(answering turn #1)*\nslow but alive"

        def chatty(proc):
            for i in range(30):                              # ~3 s of output at 0.1 s intervals
                proc.stdout.feed(_assistant_line(i))
                time.sleep(0.1)
            proc.stdout.feed(json.dumps({"type": "result", "is_error": False, "result": reply,
                                         "usage": {"input_tokens": 1, "output_tokens": 1},
                                         "num_turns": 1}) + "\n")

        sess = FakeChildSession(gap=0.15, script=chatty)
        state = pr.DaemonState()
        state.pending = [("telegram", "think hard about this", 0)]
        state.pending_event.set()
        await self._drain_until(state, lambda: sess, lambda: not state.pending)

        texts = self._texts()
        self.assertTrue(any("slow but alive" in t for t in texts), texts)
        self.assertFalse([t for t in texts if BACKSTOP_ALERT in t])
        self.assertFalse(sess.children[0].killed.is_set())
        self.assertFalse([line for line in self.logs if "TURN BACKSTOP" in line])

    async def test_it_never_touches_a_turn_the_watchdog_already_ended(self):
        """The watchdog works: every hang is cut at the gap, the turn returns, and the backstop is
        cancelled with it — three hangs to the dead-letter and not one backstop line among them."""
        made = []

        def make():
            s = FakeChildSession(gap=0.15)                   # a real watchdog, firing at 0.15 s
            made.append(s)
            return s

        state = pr.DaemonState()
        state.pending = [("telegram", "wedges every time", 0)]
        state.pending_event.set()
        await self._drain_until(state, make, lambda: not state.pending)
        await asyncio.sleep(self.BACKSTOP * 1.5)             # past the backstop: a stray one would fire

        texts = self._texts()
        hung = [t for t in texts if t == pr.turn_timeout_notice(0.15)]
        self.assertEqual(len(hung), pr.MAX_TURN_ATTEMPTS, texts)   # the watchdog's notice, each time
        self.assertFalse([t for t in texts if BACKSTOP_ALERT in t], texts)
        self.assertFalse([line for line in self.logs if "TURN BACKSTOP" in line], self.logs)
        self.assertTrue(made and all(s.timed_out for s in made))


class BackstopSuppressesTheFallbackLadder(unittest.TestCase):
    """The 02:39 wedge was the FIRST turn of a resumed session — exactly where the ladder would read
    an unexplained `None` as a stale resume and re-send into a fresh process. A backstop kill is a
    hang, so it must stop the ladder the way `timed_out` does."""

    def setUp(self):
        patcher = mock.patch.object(pr.claude_cli, "_TurnWatchdog", _NoWatchdog)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _kill_like_the_backstop(self, sess, after):
        def go():
            time.sleep(after)
            sess.backstop_killed = True
            sess.proc.kill()
        threading.Thread(target=go, daemon=True).start()

    def test_a_stale_resume_id_is_not_blamed(self):
        sess = _session(0.15, resume_session_id="the-resumed-id")
        starts = []
        sess.start = lambda: starts.append(1)
        self._kill_like_the_backstop(sess, 0.2)
        hung, reply, _ = _send_bounded(sess, "hi", bound=5.0)
        self.assertFalse(hung)
        self.assertIsNone(reply)
        self.assertEqual(starts, [])
        self.assertEqual(sess.resume_session_id, "the-resumed-id")
        self.assertFalse(sess.resume_failed)
        self.assertFalse(sess.timed_out, "the watchdog never fired — only the backstop knows")

    def test_the_warm_model_dial_is_not_blamed(self):
        sess = _session(0.15, fallback_model="claude-sonnet-5")
        sess.model = "claude-opus-5"
        starts = []
        sess.start = lambda: starts.append(1)
        self._kill_like_the_backstop(sess, 0.2)
        hung, reply, _ = _send_bounded(sess, "hi", bound=5.0)
        self.assertFalse(hung)
        self.assertIsNone(reply)
        self.assertEqual(starts, [])
        self.assertFalse(sess.did_fallback)

    def test_the_flag_resets_per_send(self):
        sess = _session(0.15)
        sess.backstop_killed = True
        sess.proc.stdout.feed(_result_line("fine"))
        self.assertEqual(sess.send("works"), "fine")
        self.assertFalse(sess.backstop_killed)


class BackstopWhenTheKillDoesNotRelease(unittest.IsolatedAsyncioTestCase):
    """If killing the child doesn't give the turn back (the worker is stuck somewhere a dead pipe
    can't reach), the backstop says so once more — that one needs `reseneschald` — rather than going
    quiet. And no path through it raises."""

    async def _watch(self, session):
        d = tempfile.mkdtemp()
        args, logs = _args(d), []
        state = pr.DaemonState()
        state.session = session
        with mock.patch.object(pr, "TURN_BACKSTOP_RELEASE_SEC", 0.1):
            task = asyncio.ensure_future(
                pr.turn_backstop_watch(state, args, logs.append, pr.TurnBackstop(0.05), "turn-x"))
            try:
                async with asyncio.timeout(10.0):
                    while len(_sent(d)) < 2:
                        await asyncio.sleep(0.01)
            finally:
                task.cancel()
        return [m["text"] for m in _sent(d)], logs

    async def test_a_kill_that_frees_nothing_asks_for_reseneschald(self):
        sess = pr.StubWarmSession()
        sess.proc = _FakeProc()                              # nothing reads its stdout; kill frees no one
        texts, logs = await self._watch(sess)
        self.assertIn(BACKSTOP_ALERT, texts[0])
        self.assertIn("reseneschald", texts[1])
        self.assertTrue(sess.proc.killed.is_set())
        self.assertTrue(sess.backstop_killed)
        self.assertTrue(any("still in flight" in line for line in logs), logs)

    async def test_no_child_and_a_kill_that_raises_are_both_survived(self):
        for proc in (None, _FakeProc(kill_raises=True)):
            sess = pr.StubWarmSession()
            sess.proc = proc
            texts, logs = await self._watch(sess)
            self.assertIn(BACKSTOP_ALERT, texts[0])
            self.assertTrue(any("reseneschald" in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main()
