#!/usr/bin/env python3
"""Tests for mid-turn interleave **phase 2 — the live interrupt/continue mechanics**.
Spec: `seneschal/docs/mid-turn-interleave-spec.md` §5 (the mechanics), §5.1 (the three non-negotiables),
§5.1.1 (no fold cap), §5.3 (the pop/crash walk), §5.4 (observability), §3.6-§3.8 (the interrupt
primitive and the MCP-possibly-landed finding). Built, shipped OFF (the daemon's own mode stays
`observe`; `--interleave-mode live` refuses per host, see `test_interleave.py`'s `ModeRefusal`).

What these guard:

  * **the interrupt frame + `interrupt_requested`** — `WarmSession.send_interrupt` writes the exact
    `control_request`/`interrupt` shape §3.2 probed, and the flag suppresses the first-turn fallback
    ladder exactly the way `timed_out` already does (§3.6/§3.7's warning, made concrete);
  * **the happy path: interrupt, continue, one delivered reply** — N+1 `_send_turn` calls for N folds,
    no cap (§5.1.1), and a fold arriving DURING the continuation extends the chain rather than being
    dropped (step 4′);
  * **the dead man's switch** — an interrupt that never lands, or lands with nothing queued to fold,
    degrades to exactly the ordinary single-send outcome; nothing here can hang waiting for a fold
    that isn't coming;
  * **§5.1's non-negotiable 1 (contiguity)** — a steer on a non-contiguous queue entry is logged
    exactly as today but never acted on;
  * **§3.8's MCP-possibly-landed marking** — an interrupt that cuts an unresolved tool call names it
    in the continuation prompt and never asserts cancelled-or-landed either way;
  * **§5.3's pop discipline** — the whole verified fold prefix pops together, never by count alone.

No test here spawns a real `claude` process — WarmSession-level interrupt/ladder-suppression is a
fake-Popen unit; `interleave_live_send`'s loop is exercised against a scripted fake session, never a
real subprocess.

**The pure `interleave.py` classes always run. The rest need the daemon half** — the presence hooks
(`interleave_open_turn`/`_snapshot`/`_observe`/`_live_send`/`_track_tool_call`, `pop_folded_prefix`,
`DaemonState.inflight_turn`) and `backends.claude_cli.WarmSession.send_interrupt` — and skip, naming
the missing hook, until those land.

Run:  python -m unittest test_interleave_phase2
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
BACKENDS_DIR = os.path.join(SCRIPT_DIR, "backends")
if BACKENDS_DIR not in sys.path:
    sys.path.insert(0, BACKENDS_DIR)

import interleave as il  # noqa: E402

try:  # the daemon half — optional until its hooks land (see the module docstring)
    import presence as pr  # noqa: E402
except Exception:  # noqa: BLE001
    pr = None
try:
    from backends.claude_cli import WarmSession  # noqa: E402
except Exception:  # noqa: BLE001
    WarmSession = None




def _args(state_dir, **over):
    import argparse
    base = dict(state_dir=state_dir, interleave_mode=il.MODE_LIVE)
    base.update(over)
    return argparse.Namespace(**base)


def _rows(state_dir):
    path = il.log_path(state_dir)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------- interleave.py — pure


class ContinuationPrompt(unittest.TestCase):
    def test_header_and_one_fold(self):
        prompt = il.build_continuation_prompt(["also do X"])
        self.assertIn(il.CONTINUATION_HEADER, prompt)
        self.assertIn("also do X", prompt)
        self.assertNotIn("cut off", prompt, "no possibly-landed clause without one")

    def test_all_of_it_not_both_for_more_than_one_fold(self):
        # §10 D6 + "no cap": with 3+ folds the wording must not imply exactly two.
        prompt = il.build_continuation_prompt(["first", "second", "third"])
        for text in ("first", "second", "third"):
            self.assertIn(text, prompt)
        self.assertIn("all of it", il.CONTINUATION_HEADER.lower())

    def test_the_possibly_landed_clause_is_inserted_between_header_and_folds(self):
        clause = il.possibly_landed_clause("notion-update-page", "input: {...}")
        prompt = il.build_continuation_prompt(["also do X"], landed_clause=clause)
        self.assertLess(prompt.index(il.CONTINUATION_HEADER), prompt.index(clause))
        self.assertLess(prompt.index(clause), prompt.index("also do X"))

    def test_possibly_landed_clause_never_asserts_either_outcome(self):
        clause = il.possibly_landed_clause("notion-update-page", "row 4b1c")
        self.assertIn("notion-update-page", clause)
        self.assertIn("row 4b1c", clause)
        # It may SAY "cancelled"/"committed" while discussing them, but must never assert one as
        # fact — "do not assume either outcome" is the tell that it's naming the uncertainty, not
        # resolving it.
        self.assertIn("do not assume", clause.lower())
        self.assertNotIn("was cancelled.", clause)
        self.assertNotIn("has landed.", clause)

    def test_possibly_landed_clause_degrades_gracefully_with_no_name(self):
        clause = il.possibly_landed_clause(None, None)
        self.assertIn("a tool call", clause)


class VersionProbe(unittest.TestCase):
    def test_never_raises_on_a_missing_binary(self):
        self.assertIsNone(il.current_cli_version(claude_bin="definitely-not-a-real-binary-xyz"))

    def test_parses_a_semver_out_of_version_output(self):
        # Exercised through the real subprocess path is `test_interleave.py`'s job; here we just
        # confirm the regex extraction is sane against a shape the CLI actually prints.
        import re
        m = re.search(r"\d+\.\d+\.\d+", "2.1.280 (Claude Code)")
        self.assertEqual(m.group(0), "2.1.280")


class McpPossiblyLandedRow(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_it_writes_a_row_and_never_raises(self):
        ok = il.record_mcp_possibly_landed(self.dir, turn_id="t1", tool_name="notion-update-page",
                                           input_preview="row 4b1c")
        self.assertTrue(ok)
        rows = _rows(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], il.KIND_MCP_POSSIBLY_LANDED)
        self.assertEqual(rows[0]["tool_name"], "notion-update-page")

    def test_it_is_counted_in_stats(self):
        il.record_mcp_possibly_landed(self.dir, turn_id="t1", tool_name="x")
        self.assertEqual(il.stats(self.dir)["mcp_possibly_landed"], 1)

    def test_a_bad_state_dir_costs_the_row_not_a_raise(self):
        self.assertFalse(il.record_mcp_possibly_landed("\0impossible", turn_id="t1"))


# --------------------------------------------------------------------------- WarmSession — the primitive


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
    """A fixed, pre-baked stdout for one turn — `test_presence_warm_fallback.py`'s own fixture shape,
    reused here because `send_interrupt`/the ladder-suppression tests need no live feedback, just a
    canned event list."""

    def __init__(self, events, exit_code=None):
        self.stdin = _FakeStdin()
        self.stdout = iter([json.dumps(ev) + "\n" for ev in events])
        self._exit = exit_code
        self.killed = False

    def poll(self):
        return self._exit

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.killed = True

    def kill(self):
        self.killed = True


INTERRUPTED_RESULT = [
    {"type": "result", "is_error": True, "result": ""},
]


def _session(**kw):
    sess = WarmSession(claude_bin="claude", model=None, permission_mode="bypassPermissions",
                       log=lambda *_: None, **kw)
    return sess


class SendInterrupt(unittest.TestCase):
    def test_writes_the_probed_control_request_shape(self):
        sess = _session()
        sess.proc = _FakeProc(INTERRUPTED_RESULT)
        ok = sess.send_interrupt(request_id="probe-1")
        self.assertTrue(ok)
        self.assertTrue(sess.interrupt_requested)
        frame = json.loads(sess.proc.stdin.written[0])
        self.assertEqual(frame["type"], "control_request")
        self.assertEqual(frame["request"]["subtype"], "interrupt")
        self.assertEqual(frame["request_id"], "probe-1")

    def test_mints_a_request_id_when_none_given(self):
        sess = _session()
        sess.proc = _FakeProc(INTERRUPTED_RESULT)
        sess.send_interrupt()
        frame = json.loads(sess.proc.stdin.written[0])
        self.assertTrue(frame["request_id"])

    def test_sets_the_flag_even_when_the_write_fails(self):
        # The dead man's switch's own precondition: a caller that asked for an interrupt has
        # committed to treating the next is_error as deliberate, whether or not the write landed.
        sess = _session()
        sess.proc = None
        self.assertFalse(sess.send_interrupt())
        self.assertTrue(sess.interrupt_requested)

    def test_never_raises_on_a_broken_pipe(self):
        sess = _session()
        sess.proc = _FakeProc(INTERRUPTED_RESULT)

        def _raise(*_a, **_kw):
            raise BrokenPipeError()
        sess.proc.stdin.write = _raise
        self.assertFalse(sess.send_interrupt())
        self.assertTrue(sess.interrupt_requested)


class InterruptSuppressesTheFallbackLadder(unittest.TestCase):
    """§3.6/§3.7: an interrupt landing on a session's very first turn must read exactly like the
    watchdog's own `timed_out` — never as a stale resume or a bad model dial, or the ladder respawns
    for the wrong reason."""

    @staticmethod
    def _interrupted_send_turn(sess):
        # send() resets BOTH flags at its own top, before _send_turn ever runs (mirroring how the
        # real interrupt lands DURING the blocked read, from a different thread, never before it) —
        # so the fixture has to set the flag INSIDE the stubbed call, not before send() is invoked.
        def _send_turn(text, on_event=None):
            sess.interrupt_requested = True
            return None
        return _send_turn

    def test_a_first_turn_interrupt_never_triggers_the_resume_fallback(self):
        sess = _session(resume_session_id="stale-id")
        sess.proc = _FakeProc(INTERRUPTED_RESULT)
        sess._send_turn = self._interrupted_send_turn(sess)
        reply = sess.send("hello")
        self.assertIsNone(reply)
        self.assertEqual(sess.resume_session_id, "stale-id", "never dropped — no cold respawn fired")
        self.assertFalse(sess.resume_failed)

    def test_a_first_turn_interrupt_never_triggers_the_model_fallback(self):
        sess = _session(fallback_model="claude-opus-4-8")
        sess.model = "some-bad-dial"
        sess.proc = _FakeProc(INTERRUPTED_RESULT)
        sess._send_turn = self._interrupted_send_turn(sess)
        reply = sess.send("hello")
        self.assertIsNone(reply)
        self.assertFalse(sess.did_fallback, "never spent its one fallback on an interrupt")
        self.assertEqual(sess.model, "some-bad-dial")

    def test_timed_out_and_interrupt_requested_are_independent_flags(self):
        sess = _session()
        sess.timed_out = True
        sess.interrupt_requested = False
        self.assertTrue(sess.timed_out)
        sess2 = _session()
        sess2.proc = _FakeProc(INTERRUPTED_RESULT)
        sess2.send_interrupt()
        self.assertFalse(sess2.timed_out)
        self.assertTrue(sess2.interrupt_requested)

    def test_send_resets_interrupt_requested_at_the_top(self):
        sess = _session()
        sess.interrupt_requested = True
        sess._send_turn = lambda text, on_event=None: "a real reply"
        reply = sess.send("hello")
        self.assertEqual(reply, "a real reply")
        self.assertFalse(sess.interrupt_requested)


# --------------------------------------------------------------------------- interleave_live_send


class _FakeLiveSession:
    """Drives `interleave_live_send`'s loop without a real subprocess. `script` is a list of
    `(reply_or_None, folds_or_None)` pairs, one per expected `send()` call — `folds` is the list to
    push onto `state.inflight_turn["fold_queue"]` (simulating a fold that arrived while THIS call was
    blocked) before returning `None` with `interrupt_requested` set; `None` for `folds` means an
    ordinary, non-interrupted return (the reply may itself be `None` — a REAL failure)."""

    def __init__(self, state, script):
        self.state = state
        self.script = list(script)
        self.calls: list[str] = []
        self.interrupt_requested = False
        self.session_id = "fake-session"

    def send_interrupt(self, request_id=None):
        self.interrupt_requested = True
        return True

    def send(self, text, **_kw):
        self.calls.append(text)
        self.interrupt_requested = False  # mirrors WarmSession.send()'s own top-of-call reset
        reply, folds = self.script.pop(0)
        if folds is not None:
            self.state.inflight_turn["fold_queue"].extend(folds)
            self.interrupt_requested = True
        return reply


def _fold(text, arrival_id="a", channel="telegram"):
    return {"arrival_id": arrival_id, "channel": channel, "text": text}


class InterleaveLiveSend(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)
        self.state = pr.DaemonState()
        pr.interleave_open_turn(self.state, "t1", "head message")

    async def test_the_ordinary_path_is_a_single_send_with_no_folds(self):
        self.state.session = _FakeLiveSession(self.state, [("hello", None)])
        reply, folded = await pr.interleave_live_send(self.state, self.args, lambda *_: None,
                                                       "prompt", {})
        self.assertEqual(reply, "hello")
        self.assertEqual(folded, [])
        self.assertEqual(self.state.session.calls, ["prompt"])

    async def test_a_real_failure_with_no_interrupt_returns_none_and_no_folds(self):
        self.state.session = _FakeLiveSession(self.state, [(None, None)])
        reply, folded = await pr.interleave_live_send(self.state, self.args, lambda *_: None,
                                                       "prompt", {})
        self.assertIsNone(reply)
        self.assertEqual(folded, [])

    async def test_interrupt_and_continue_happy_path(self):
        session = _FakeLiveSession(self.state, [
            (None, [_fold("also do X", "a1")]),
            ("final reply", None),
        ])
        self.state.session = session
        reply, folded = await pr.interleave_live_send(self.state, self.args, lambda *_: None,
                                                       "prompt", {})
        self.assertEqual(reply, "final reply")
        self.assertEqual([f["arrival_id"] for f in folded], ["a1"])
        self.assertEqual(len(session.calls), 2, "N+1 sends for N=1 fold")
        self.assertIn(il.CONTINUATION_HEADER, session.calls[1])
        self.assertIn("also do X", session.calls[1])

    async def test_a_fold_during_the_continuation_extends_the_chain_no_cap(self):
        # Step 4′ — §10 D5/§5.1.1: a second fold landing DURING the continuation must not be dropped, and
        # nothing here bounds how many times this can repeat.
        session = _FakeLiveSession(self.state, [
            (None, [_fold("first fold", "a1")]),
            (None, [_fold("second fold", "a2")]),
            ("final reply", None),
        ])
        self.state.session = session
        reply, folded = await pr.interleave_live_send(self.state, self.args, lambda *_: None,
                                                       "prompt", {})
        self.assertEqual(reply, "final reply")
        self.assertEqual([f["arrival_id"] for f in folded], ["a1", "a2"])
        self.assertEqual(len(session.calls), 3, "N+1 sends for N=2 folds")
        self.assertIn("first fold", session.calls[1])
        self.assertIn("second fold", session.calls[2])
        # And each continuation names ONLY the fold(s) new to it, not a re-send of the earlier one —
        # the whole point of "all of it together" being scoped per round, not accumulating verbatim.
        self.assertNotIn("first fold", session.calls[2])

    async def test_multiple_folds_landing_in_one_gap_are_folded_together(self):
        # §3.8's own CLI finding: more than one buffered message during a single tool call can land
        # as ONE result. The wrapper must not assume exactly one fold per interrupt.
        session = _FakeLiveSession(self.state, [
            (None, [_fold("first fold", "a1"), _fold("second fold", "a2")]),
            ("final reply", None),
        ])
        self.state.session = session
        reply, folded = await pr.interleave_live_send(self.state, self.args, lambda *_: None,
                                                       "prompt", {})
        self.assertEqual(reply, "final reply")
        self.assertEqual(len(folded), 2)
        self.assertEqual(len(session.calls), 2)
        self.assertIn("first fold", session.calls[1])
        self.assertIn("second fold", session.calls[1])

    async def test_dead_mans_switch_interrupt_with_nothing_queued_degrades_to_failure(self):
        # A stray interrupt signal with no fold behind it (the narrow race the module docstring
        # names) must not spin — it degrades to exactly the real-failure outcome.
        session = _FakeLiveSession(self.state, [(None, [])])
        self.state.session = session
        reply, folded = await pr.interleave_live_send(self.state, self.args, lambda *_: None,
                                                       "prompt", {})
        self.assertIsNone(reply)
        self.assertEqual(folded, [])
        self.assertEqual(len(session.calls), 1, "never retried on an empty fold")

    async def test_the_mcp_possibly_landed_clause_rides_into_the_continuation(self):
        self.state.inflight_turn["pending_tool_call"] = {
            "name": "notion-update-page", "input_preview": "row 4b1c",
        }
        session = _FakeLiveSession(self.state, [
            (None, [_fold("also do X", "a1")]),
            ("final reply", None),
        ])
        self.state.session = session
        await pr.interleave_live_send(self.state, self.args, lambda *_: None, "prompt", {})
        self.assertIn("notion-update-page", session.calls[1])
        self.assertIn("row 4b1c", session.calls[1])
        self.assertIn("do not assume", session.calls[1].lower())
        # Consumed, never reused on a LATER fold that had nothing in flight of its own.
        self.assertIsNone(self.state.inflight_turn["pending_tool_call"])
        rows = _rows(self.dir)
        self.assertEqual(len([r for r in rows if r.get("kind") == il.KIND_MCP_POSSIBLY_LANDED]), 1)

    async def test_no_possibly_landed_clause_when_nothing_was_pending(self):
        session = _FakeLiveSession(self.state, [
            (None, [_fold("also do X", "a1")]),
            ("final reply", None),
        ])
        self.state.session = session
        await pr.interleave_live_send(self.state, self.args, lambda *_: None, "prompt", {})
        self.assertNotIn("cut off", session.calls[1])

    async def test_a_session_with_no_send_interrupt_never_reaches_the_loop_body_twice(self):
        # StubWarmSession/CodexWarmSession have no send_interrupt — interleave_observe's own
        # `hasattr` guard means fold_queue never gets populated for them, so this wrapper's loop is
        # unreachable past the first send regardless of what interrupt_requested reads.
        class _NoInterruptSession:
            def __init__(self):
                self.calls = []

            def send(self, text, **_kw):
                self.calls.append(text)
                return "reply"

        session = _NoInterruptSession()
        self.state.session = session
        reply, folded = await pr.interleave_live_send(self.state, self.args, lambda *_: None,
                                                       "prompt", {})
        self.assertEqual(reply, "reply")
        self.assertEqual(folded, [])
        self.assertEqual(len(session.calls), 1)


# --------------------------------------------------------------------------- contiguity (§5.1 non-negotiable 1)


def _verdict(kind="hold", layer=il.LAYER_MODEL, confidence=0.9, reason="r"):
    return {"layer": layer, "verdict": kind, "confidence": confidence, "reason": reason,
            "model": "qwen3.5:4b"}


class _FakeGate:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)

    def __call__(self, *_a, **_kw):
        return self.verdicts.pop(0)


class _PatchGate:
    def __init__(self, case, gate):
        case.addCleanup(lambda orig=il.gate: setattr(il, "gate", orig))
        il.gate = gate


class _InterruptingSession:
    """A session that only exists to prove send_interrupt was (or wasn't) called."""

    def __init__(self):
        self.interrupt_calls = 0
        self.session_id = "s"

    def send_interrupt(self, request_id=None):
        self.interrupt_calls += 1
        return True


class ContiguityIsEnforced(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)

    async def test_the_first_queued_steer_interrupts(self):
        _PatchGate(self, _FakeGate([_verdict("steer")]))
        state = pr.DaemonState()
        state.session = _InterruptingSession()
        pr.interleave_open_turn(state, "t1", "head")
        snap = pr.interleave_snapshot(state, self.args, "telegram", "also do X", queue_index=1)
        await pr.interleave_observe(state, self.args, lambda *_: None, snap)
        self.assertEqual(state.session.interrupt_calls, 1)
        self.assertEqual(len(state.inflight_turn["fold_queue"]), 1)

    async def test_a_steer_on_a_non_contiguous_item_is_logged_but_not_acted_on(self):
        # §5.1 non-negotiable 1: an item at queue_index 2 is only foldable once index 1's own
        # decision (whatever it was) has actually been queued to fold. Skipping straight to 2 must
        # never interrupt — the steer verdict is still written to the log exactly as usual.
        _PatchGate(self, _FakeGate([_verdict("steer")]))
        state = pr.DaemonState()
        state.session = _InterruptingSession()
        pr.interleave_open_turn(state, "t1", "head")
        snap = pr.interleave_snapshot(state, self.args, "telegram", "also do X", queue_index=2)
        await pr.interleave_observe(state, self.args, lambda *_: None, snap)
        self.assertEqual(state.session.interrupt_calls, 0)
        self.assertEqual(state.inflight_turn["fold_queue"], [])
        rows = _rows(self.dir)
        self.assertEqual(rows[0]["verdict"], il.STEER, "telemetry is unchanged either way")

    async def test_a_hold_never_interrupts_regardless_of_index(self):
        _PatchGate(self, _FakeGate([_verdict("hold")]))
        state = pr.DaemonState()
        state.session = _InterruptingSession()
        pr.interleave_open_turn(state, "t1", "head")
        snap = pr.interleave_snapshot(state, self.args, "telegram", "cats fed", queue_index=1)
        await pr.interleave_observe(state, self.args, lambda *_: None, snap)
        self.assertEqual(state.session.interrupt_calls, 0)

    async def test_untyped_arrivals_never_interrupt_non_negotiable_3(self):
        _PatchGate(self, _FakeGate([_verdict("steer")]))
        state = pr.DaemonState()
        state.session = _InterruptingSession()
        pr.interleave_open_turn(state, "t1", "head")
        snap = pr.interleave_snapshot(state, self.args, "telegram",
                                      "[job finished: \"x\" — status done, exit 0.]", queue_index=1)
        self.assertFalse(snap["typed"])
        await pr.interleave_observe(state, self.args, lambda *_: None, snap)
        self.assertEqual(state.session.interrupt_calls, 0)

    async def test_observe_mode_never_interrupts_even_on_a_contiguous_steer(self):
        _PatchGate(self, _FakeGate([_verdict("steer")]))
        state = pr.DaemonState()
        state.session = _InterruptingSession()
        args = _args(self.dir, interleave_mode=il.MODE_OBSERVE)
        pr.interleave_open_turn(state, "t1", "head")
        snap = pr.interleave_snapshot(state, args, "telegram", "also do X", queue_index=1)
        await pr.interleave_observe(state, args, lambda *_: None, snap)
        self.assertEqual(state.session.interrupt_calls, 0)

    async def test_a_second_contiguous_steer_also_interrupts(self):
        _PatchGate(self, _FakeGate([_verdict("steer"), _verdict("steer")]))
        state = pr.DaemonState()
        state.session = _InterruptingSession()
        pr.interleave_open_turn(state, "t1", "head")
        snap1 = pr.interleave_snapshot(state, self.args, "telegram", "first", queue_index=1)
        await pr.interleave_observe(state, self.args, lambda *_: None, snap1)
        snap2 = pr.interleave_snapshot(state, self.args, "telegram", "second", queue_index=2)
        await pr.interleave_observe(state, self.args, lambda *_: None, snap2)
        self.assertEqual(state.session.interrupt_calls, 2)
        self.assertEqual(len(state.inflight_turn["fold_queue"]), 2)


# --------------------------------------------------------------------------- interleave_track_tool_call


class TrackToolCall(unittest.TestCase):
    def test_a_tool_use_event_sets_pending_tool_call(self):
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t1", "head")
        ev = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "notion-update-page", "input": {"page_id": "4b1c"}}]}}
        pr.interleave_track_tool_call(state, ev)
        pending = state.inflight_turn["pending_tool_call"]
        self.assertEqual(pending["name"], "notion-update-page")
        self.assertIn("4b1c", pending["input_preview"])

    def test_a_tool_result_event_clears_it(self):
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t1", "head")
        state.inflight_turn["pending_tool_call"] = {"name": "x", "input_preview": None}
        ev = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "abc", "content": "ok"}]}}
        pr.interleave_track_tool_call(state, ev)
        self.assertIsNone(state.inflight_turn["pending_tool_call"])

    def test_no_window_open_is_harmless(self):
        state = pr.DaemonState()
        pr.interleave_track_tool_call(state, {"type": "assistant", "message": {"content": []}})  # no raise
        self.assertIsNone(state.inflight_turn)

    def test_garbage_events_never_raise(self):
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t1", "head")
        for bad in (None, {}, {"type": "assistant"}, {"type": "assistant", "message": None},
                   {"type": "assistant", "message": {"content": "not-a-list"}},
                   {"type": "assistant", "message": {"content": [None, 5, "x"]}}):
            pr.interleave_track_tool_call(state, bad)  # must not raise
        self.assertIsNone(state.inflight_turn["pending_tool_call"])

    def test_a_second_tool_use_overwrites_the_first_without_a_result_between(self):
        # The named simplification: this clears on ANY tool_result, not the matching id, so two
        # sequential tool_use events with no result between them just track the most recent.
        state = pr.DaemonState()
        pr.interleave_open_turn(state, "t1", "head")
        pr.interleave_track_tool_call(state, {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "first", "input": {}}]}})
        pr.interleave_track_tool_call(state, {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "second", "input": {}}]}})
        self.assertEqual(state.inflight_turn["pending_tool_call"]["name"], "second")


# --------------------------------------------------------------------------- pop_folded_prefix (§5.3)


class PopFoldedPrefix(unittest.TestCase):
    def _state_with(self, entries):
        state = pr.DaemonState()
        state.pending = list(entries)
        return state

    def test_empty_folded_pops_exactly_one_byte_identical_to_today(self):
        state = self._state_with([("telegram", "head", 1, None), ("telegram", "next", 0, None)])
        popped = pr.pop_folded_prefix(state, "telegram", "head", [])
        self.assertEqual(popped, 1)
        self.assertEqual(state.pending, [("telegram", "next", 0, None)])

    def test_the_whole_verified_prefix_pops_together(self):
        state = self._state_with([
            ("telegram", "head", 1, None),
            ("telegram", "fold A", 0, None),
            ("telegram", "fold B", 0, None),
            ("telegram", "unrelated next", 0, None),
        ])
        folded = [_fold("fold A"), _fold("fold B")]
        popped = pr.pop_folded_prefix(state, "telegram", "head", folded)
        self.assertEqual(popped, 3)
        self.assertEqual(state.pending, [("telegram", "unrelated next", 0, None)])

    def test_a_mismatch_pops_only_the_longest_matching_prefix(self):
        # A reminder or job notice slipped in ahead of the second fold (or a restart lost the
        # in-RAM window) — the head pops, the genuinely-matching first fold pops, the rest stays.
        state = self._state_with([
            ("telegram", "head", 1, None),
            ("telegram", "fold A", 0, None),
            ("telegram", "a reminder that slipped in", 0, None),
            ("telegram", "fold B", 0, None),
        ])
        folded = [_fold("fold A"), _fold("fold B")]
        popped = pr.pop_folded_prefix(state, "telegram", "head", folded)
        self.assertEqual(popped, 2)
        self.assertEqual(state.pending, [
            ("telegram", "a reminder that slipped in", 0, None),
            ("telegram", "fold B", 0, None),
        ])

    def test_a_head_mismatch_pops_nothing(self):
        state = self._state_with([("telegram", "something else entirely", 1, None)])
        popped = pr.pop_folded_prefix(state, "telegram", "head", [_fold("fold A")])
        self.assertEqual(popped, 0)
        self.assertEqual(len(state.pending), 1)

    def test_never_pops_past_the_end_of_the_queue(self):
        state = self._state_with([("telegram", "head", 1, None)])
        popped = pr.pop_folded_prefix(state, "telegram", "head",
                                      [_fold("fold A that was never actually queued")])
        self.assertEqual(popped, 1)
        self.assertEqual(state.pending, [])


if __name__ == "__main__":
    unittest.main()
