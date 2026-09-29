#!/usr/bin/env python3
"""Tests for Step 2a — resume instead of re-ground (the warm-session lifetime research, §5).

The research's actual complaint about the short-lived warm session was never lifetime, it was **amnesia**:
a respawn drops everything except a 6-line thread tail. `--resume` fixes that without a long-lived
process — but it is NOT free, and most of what's tested here is the gate that keeps it honest.

**Resume restores the previous context**, so the next turn starts at the old context size instead of
the ~64k cold floor — exactly the per-turn cost multiplier the research argued against for long-lived
sessions. Hence: resume only below the governor's `context_fill_winddown_pct`, only after a CLEAN death,
and only if it's recent. Those three gates are the point of `resume_decision`, and they're pure.

Behaviour verified live against the CLI (v2.1.220) before any of this was written — see the
RESUMABLE_REASONS comment block in presence.py. The one that shaped the fallback: a stale id fails in
~3s with `is_error` + `num_turns: 0`, which `_read_until_result` already turns into a None reply, so
the fallback keys on the existing signal rather than parsing stderr.

Stdlib ``unittest`` only; no live `claude` (subprocess.Popen is replaced with canned event streams).
Run:  python -m unittest seneschal.scripts.test_presence_resume
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402

# The grounding's opening clause ("You are <assistant>"), rendered from whatever identity this install
# has — the test must not assume a configured assistant name.
GROUNDING_HEAD = pr.GROUNDING.split("\n", 1)[0].split(" — ")[0]

GOOD = [{"type": "system", "subtype": "init", "session_id": "s1", "apiKeySource": "none"},
        {"type": "result", "is_error": False, "result": "hello",
         "usage": {"input_tokens": 2, "cache_read_input_tokens": 60_000}, "num_turns": 1}]
# How a stale/unknown --resume id actually fails, measured live: a result event with is_error and
# num_turns 0 (stderr says "No conversation found with session ID: …").
STALE_RESUME = [{"type": "result", "is_error": True, "num_turns": 0,
                 "result": "No conversation found with session ID: …"}]


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
    def __init__(self, events, exit_code=None):
        self.stdin = _FakeStdin()
        self.stdout = iter([json.dumps(ev) + "\n" for ev in events])
        self._exit = exit_code

    def poll(self):
        return self._exit

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


def _session(resume=None, model="claude-opus-5", fallback=None):
    return pr.WarmSession("claude", model, "bypassPermissions", lambda *_a, **_k: None,
                          fallback_model=fallback, resume_session_id=resume)


def _record(**over):
    base = {"id": "sess-abc", "model": "claude-opus-5", "context_tokens": 100_000,
            "turns_served": 4, "ended_at": time.time() - 60, "reason": pr.RESPAWN_IDLE}
    base.update(over)
    return base


class ResumeDecision(unittest.TestCase):
    """The gate. Every 'no' here is a deliberate one — see the class docstring."""

    def test_happy_path(self):
        sid, why = pr.resume_decision(_record(), time.time())
        self.assertEqual(sid, "sess-abc")
        self.assertIn("resuming", why)

    def test_no_record(self):
        for junk in (None, {}, {"id": ""}, "nope", 7):
            self.assertIsNone(pr.resume_decision(junk, time.time())[0])

    def test_turn_error_is_never_resumed(self):
        # Restoring a session that just died mid-turn risks restoring whatever wedged it — and it
        # shares its failure shape with a stale id, so we'd learn nothing from the retry either.
        sid, why = pr.resume_decision(_record(reason=pr.RESPAWN_TURN_ERROR), time.time())
        self.assertIsNone(sid)
        self.assertIn("not resumable", why)

    def test_undelivered_is_never_resumed(self):
        # Dropping that session so the retry re-grounds cleanly (no phantom reply) is load-bearing.
        self.assertIsNone(pr.resume_decision(_record(reason=pr.RESPAWN_UNDELIVERED), time.time())[0])

    def test_shutdown_is_resumable(self):
        # The highest-value case: a Path A code reload killing a live conversation.
        self.assertEqual(pr.resume_decision(_record(reason=pr.RESPAWN_SHUTDOWN), time.time())[0],
                         "sess-abc")

    def test_too_old(self):
        old = _record(ended_at=time.time() - pr.RESUME_MAX_AGE_SEC - 1)
        sid, why = pr.resume_decision(old, time.time())
        self.assertIsNone(sid)
        self.assertIn("old", why)

    def test_just_inside_the_age_limit(self):
        fresh = _record(ended_at=time.time() - pr.RESUME_MAX_AGE_SEC + 30)
        self.assertEqual(pr.resume_decision(fresh, time.time())[0], "sess-abc")

    def test_clock_skew_backwards_is_refused(self):
        # A record stamped in the future means the clock moved; don't trust the age check.
        self.assertIsNone(pr.resume_decision(_record(ended_at=time.time() + 600), time.time())[0])

    def test_missing_end_timestamp(self):
        for bad in (None, "yesterday", True):
            self.assertIsNone(pr.resume_decision(_record(ended_at=bad), time.time())[0])

    def test_no_context_measurement(self):
        # A session that never recorded a turn's usage has nothing worth carrying anyway.
        for bad in (None, 0, -5, True, "lots"):
            sid, why = pr.resume_decision(_record(context_tokens=bad), time.time())
            self.assertIsNone(sid, bad)
            self.assertIn("context", why)

    def test_fat_context_is_refused(self):
        # THE cost gate: resuming restores the old context, so a fat session would re-import exactly
        # the per-turn multiplier the research argued against.
        fat = _record(context_tokens=int(pr.CONTEXT_WINDOW_TOKENS * 0.85))
        sid, why = pr.resume_decision(fat, time.time(), winddown_pct=80)
        self.assertIsNone(sid)
        self.assertIn("wind-down", why)

    def test_threshold_is_the_governor_knob(self):
        rec = _record(context_tokens=int(pr.CONTEXT_WINDOW_TOKENS * 0.5))
        self.assertEqual(pr.resume_decision(rec, time.time(), winddown_pct=80)[0], "sess-abc")
        self.assertIsNone(pr.resume_decision(rec, time.time(), winddown_pct=40)[0])

    def test_exactly_at_threshold_is_refused(self):
        rec = _record(context_tokens=pr.CONTEXT_WINDOW_TOKENS)  # 100%
        self.assertIsNone(pr.resume_decision(rec, time.time(), winddown_pct=100)[0])

    def test_junk_threshold_falls_back_to_80(self):
        rec = _record(context_tokens=int(pr.CONTEXT_WINDOW_TOKENS * 0.9))
        for junk in ("eighty", None, [80]):
            self.assertIsNone(pr.resume_decision(rec, time.time(), winddown_pct=junk)[0], junk)


class ResumeFlag(unittest.TestCase):
    def test_start_passes_resume_when_set(self):
        cmds = []
        with mock.patch.object(pr.subprocess, "Popen",
                               side_effect=lambda cmd, **kw: (cmds.append(cmd), _FakeProc(GOOD))[1]):
            _session(resume="sess-abc").start()
        self.assertIn("--resume", cmds[0])
        self.assertEqual(cmds[0][cmds[0].index("--resume") + 1], "sess-abc")

    def test_start_omits_resume_when_unset(self):
        cmds = []
        with mock.patch.object(pr.subprocess, "Popen",
                               side_effect=lambda cmd, **kw: (cmds.append(cmd), _FakeProc(GOOD))[1]):
            _session().start()
        self.assertNotIn("--resume", cmds[0])


class ResumeFallbackLadder(unittest.TestCase):
    def test_stale_id_falls_back_to_a_cold_grounded_start(self):
        cmds = []
        procs = [_FakeProc(STALE_RESUME), _FakeProc(GOOD)]
        ws = _session(resume="stale-id")
        with mock.patch.object(pr.subprocess, "Popen",
                               side_effect=lambda cmd, **kw: (cmds.append(cmd), procs.pop(0))[1]):
            ws.start()
            reply = ws.send("light prompt", cold_retry_text="FULL GROUNDING")
        self.assertEqual(reply, "hello")
        self.assertTrue(ws.resume_failed)
        self.assertFalse(ws.resumed)
        self.assertIsNone(ws.resume_session_id)
        self.assertIn("--resume", cmds[0])       # first spawn tried the resume
        self.assertNotIn("--resume", cmds[1])    # the retry is a clean cold spawn
        # ...and the retry carried the GROUNDING, not the light resume preamble: a cold process that
        # got the light prompt would have no idea who it is.
        self.assertIn("FULL GROUNDING", procs_written(cmds, ws))

    def test_cold_retry_text_defaults_to_the_original(self):
        procs = [_FakeProc(STALE_RESUME), _FakeProc(GOOD)]
        ws = _session(resume="stale-id")
        with mock.patch.object(pr.subprocess, "Popen", side_effect=lambda cmd, **kw: procs.pop(0)):
            ws.start()
            self.assertEqual(ws.send("just this"), "hello")  # no cold_retry_text given

    def test_resume_failure_then_bad_model_walks_both_rungs(self):
        cmds = []
        procs = [_FakeProc(STALE_RESUME), _FakeProc([]), _FakeProc(GOOD)]
        ws = _session(resume="stale-id", model="claude-opus-5", fallback="claude-opus-4-8")
        with mock.patch.object(pr.subprocess, "Popen",
                               side_effect=lambda cmd, **kw: (cmds.append(cmd), procs.pop(0))[1]):
            ws.start()
            reply = ws.send("light", cold_retry_text="GROUNDING")
        self.assertEqual(reply, "hello")
        self.assertTrue(ws.resume_failed)
        self.assertTrue(ws.did_fallback)
        self.assertEqual(ws.model, "claude-opus-4-8")
        self.assertEqual(len(cmds), 3)
        self.assertNotIn("--resume", cmds[2])  # never re-attach a resume after it already failed

    def test_a_healthy_resume_does_not_respawn(self):
        procs = [_FakeProc(GOOD)]  # only ONE — a stray respawn would IndexError loudly
        ws = _session(resume="good-id")
        with mock.patch.object(pr.subprocess, "Popen", side_effect=lambda cmd, **kw: procs.pop(0)):
            ws.start()
            self.assertEqual(ws.send("light", cold_retry_text="GROUNDING"), "hello")
        self.assertFalse(ws.resume_failed)
        self.assertTrue(ws.resumed)

    def test_mid_conversation_death_is_not_a_resume_failure(self):
        # Only the FIRST turn is guarded; a later death is a genuine session drop.
        procs = [_FakeProc(GOOD), _FakeProc([])]
        ws = _session(resume="good-id")
        with mock.patch.object(pr.subprocess, "Popen", side_effect=lambda cmd, **kw: procs.pop(0)):
            ws.start()
            ws.send("first", cold_retry_text="GROUNDING")
            ws.proc = _FakeProc([])  # the process dies AFTER a delivered turn
            self.assertIsNone(ws.send("second"))
        self.assertFalse(ws.resume_failed)


def procs_written(_cmds, ws):
    """Everything written to the LAST process's stdin (the retry's prompt)."""
    return "".join(ws.proc.stdin.written) if ws.proc else ""


class SessionEndRecord(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = type("A", (), {"state_dir": self.dir})()
        self.state = pr.DaemonState()
        self.state.session = pr.StubWarmSession()
        self.state.session.start()
        self.state.session.session_id = "sess-xyz"
        self.state.session.model = "claude-opus-5"
        self.state.session.context_tokens = 90_000

    def test_records_vitals_and_persists(self):
        pr._end_session(self.state, self.args, lambda *_: None, pr.RESPAWN_IDLE)
        rec = self.state.last_session_end
        self.assertEqual(rec["id"], "sess-xyz")
        self.assertEqual(rec["context_tokens"], 90_000)
        self.assertEqual(rec["reason"], pr.RESPAWN_IDLE)
        self.assertEqual(self.state.last_respawn_reason, pr.RESPAWN_IDLE)
        on_disk = pr.load_daemon_state(self.dir)["last_session"]
        self.assertEqual(on_disk["id"], "sess-xyz")  # survives a Path A reload — the point of it

    def test_non_resumable_reason_clears_last_session_id_but_keeps_the_record(self):
        pr._end_session(self.state, self.args, lambda *_: None, pr.RESPAWN_TURN_ERROR)
        st = pr.load_daemon_state(self.dir)
        self.assertIsNone(st["last_session_id"])
        self.assertEqual(st["last_session"]["reason"], pr.RESPAWN_TURN_ERROR)
        # and the gate refuses it
        self.assertIsNone(pr.resume_decision(st["last_session"], time.time())[0])

    def test_never_raises_on_a_bare_session(self):
        self.state.session = object()
        pr._end_session(self.state, self.args, lambda *_: None, pr.RESPAWN_IDLE)  # must not raise
        self.assertEqual(self.state.last_respawn_reason, pr.RESPAWN_IDLE)

    def test_plain_queue_saves_do_not_clobber_the_record(self):
        pr._end_session(self.state, self.args, lambda *_: None, pr.RESPAWN_IDLE)
        pr.save_daemon_state(self.dir, [("telegram", "hi", 0)])  # a routine mid-conversation save
        self.assertEqual(pr.load_daemon_state(self.dir)["last_session"]["id"], "sess-xyz")

    def test_load_normalizes_junk(self):
        pr.save_json(pr.daemon_state_path(self.dir), {"pending": [], "last_session": "nope"})
        self.assertIsNone(pr.load_daemon_state(self.dir)["last_session"])


class ResumeTargetWiring(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = type("A", (), {"state_dir": self.dir})()
        self.state = pr.DaemonState()

    def test_reads_the_persisted_record_when_ram_is_empty(self):
        # The Path A case: a NEW process, nothing in RAM, everything from disk.
        pr.save_daemon_state(self.dir, [], "sess-abc", last_session=_record())
        sid, _why = pr._resume_target(self.state, self.args, lambda *_: None)
        self.assertEqual(sid, "sess-abc")

    def test_honours_the_governor_threshold_from_disk(self):
        import governor as gv
        gv.save(self.dir, {"context_fill_winddown_pct": 30})
        pr.save_daemon_state(self.dir, [], "sess-abc",
                             last_session=_record(context_tokens=int(pr.CONTEXT_WINDOW_TOKENS * 0.5)))
        sid, why = pr._resume_target(self.state, self.args, lambda *_: None)
        self.assertIsNone(sid)
        self.assertIn("wind-down", why)

    def test_a_realistically_large_session_still_resumes(self):
        """The context-window correction, pinned at the place it actually cost something.

        `context_pct` divided by a `CONTEXT_WINDOW_TOKENS` of 200_000 while the model's window is 1M,
        so the default 80% ceiling meant 160k tokens — and **31 of 99 observed warm sessions peaked
        at or above that**. A third of resumes were refused `context_too_full` about sessions that
        were really ~16% full, which is most of Step 2a. 197,600 is the measured p90 session peak: a
        perfectly ordinary busy day, refused under the old denominator, resumable under the real one.
        """
        pr.save_daemon_state(self.dir, [], "sess-abc",
                             last_session=_record(context_tokens=197_600))
        sid, why = pr._resume_target(self.state, self.args, lambda *_: None)
        self.assertEqual(sid, "sess-abc", why)

    def test_cold_start_when_nothing_is_stored(self):
        sid, why = pr._resume_target(self.state, self.args, lambda *_: None)
        self.assertIsNone(sid)
        self.assertIn("no prior session", why)


class DrainerResumesEndToEnd(unittest.IsolatedAsyncioTestCase):
    """The whole loop on the stub brain: a wind-down records a resumable record, and the next spawn
    picks it up and sends the light preamble instead of the full grounding."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _args(self):
        import test_presence_observability as tpo
        return tpo._args(self.dir)

    async def test_winddown_then_respawn_resumes(self):
        import asyncio
        args = self._args()
        state = pr.DaemonState()
        state.pending = [("telegram", "first message", 0)]
        sent_prompts = []

        class Recording(pr.StubWarmSession):
            def send(self, text, on_event=None, cold_retry_text=None):
                sent_prompts.append(text)
                return super().send(text, on_event=on_event, cold_retry_text=cold_retry_text)

        task = asyncio.ensure_future(
            pr.drainer_task(state, args, lambda *_: None, lambda: Recording(), 0.0))
        try:
            async with asyncio.timeout(5.0):
                while state.last_respawn_reason != pr.RESPAWN_IDLE:  # wound down
                    await asyncio.sleep(0.01)
                state.pending.append(("telegram", "second message", 0))
                state.pending_event.set()
                while len(sent_prompts) < 2:
                    await asyncio.sleep(0.01)
        finally:
            state.stop.set()
            state.pending_event.set()
            await asyncio.wait_for(task, timeout=5.0)

        self.assertIn(GROUNDING_HEAD, sent_prompts[0])             # cold: full grounding
        self.assertIn("Picking our conversation back up", sent_prompts[1])  # resumed: light preamble
        self.assertNotIn(GROUNDING_HEAD, sent_prompts[1])          # grounding NOT re-sent
        self.assertIn("second message", sent_prompts[1])


# --------------------------------------------------------------------------------------------------
# Session-continuity phase 0 (the session-continuity spec, §6): the thread-tail constant and
# the refusal-class record. Phase 0 changes almost nothing on purpose — it MEASURES §2.2's table so the
# later phases argue from counts instead of from a grep over prose.
# --------------------------------------------------------------------------------------------------


class ResumeVerdictClasses(unittest.TestCase):
    """Every branch of the gate names itself, and the two-tuple wrapper stays byte-compatible.

    The classes are written to disk and counted across weeks, so they are stable strings, not prose.
    The reason `resume_verdict` is the single implementation and `resume_decision` a thin wrapper is
    the obvious one: two functions walking the same five branches drift, and the one that is only read
    by a log would drift **silently** — the failure this record exists to prevent, reproduced inside it.
    """

    def test_every_refusal_has_its_own_class(self):
        now = time.time()
        cases = [
            (None, pr.RESUME_NO_RECORD),
            (_record(reason=pr.RESPAWN_TURN_ERROR), pr.RESUME_NOT_RESUMABLE),
            (_record(ended_at=None), pr.RESUME_NO_END_STAMP),
            (_record(ended_at=now - pr.RESUME_MAX_AGE_SEC - 60), pr.RESUME_TOO_OLD),
            (_record(context_tokens=None), pr.RESUME_NO_CONTEXT),
            (_record(context_tokens=int(pr.CONTEXT_WINDOW_TOKENS * 0.95)), pr.RESUME_CONTEXT_FULL),
        ]
        for rec, expected in cases:
            with self.subTest(cls=expected):
                sid, why, cls = pr.resume_verdict(rec, now)
                self.assertIsNone(sid)
                self.assertEqual(cls, expected)
                self.assertTrue(why, "a refusal always carries a human reason too")

    def test_the_success_class(self):
        sid, why, cls = pr.resume_verdict(_record(), time.time())
        self.assertEqual(sid, "sess-abc")
        self.assertEqual(cls, pr.RESUME_OK)

    def test_the_classes_are_distinct(self):
        # A copy-paste that reused a class would silently merge two rows of §2.2's table.
        names = [pr.RESUME_OK, pr.RESUME_NO_RECORD, pr.RESUME_NOT_RESUMABLE, pr.RESUME_NO_END_STAMP,
                 pr.RESUME_TOO_OLD, pr.RESUME_NO_CONTEXT, pr.RESUME_CONTEXT_FULL]
        self.assertEqual(len(set(names)), len(names))

    def test_the_two_tuple_wrapper_is_unchanged(self):
        # Every existing caller and test uses this shape; adding the class must not ripple.
        for rec in (None, _record(), _record(reason=pr.RESPAWN_TURN_ERROR)):
            out = pr.resume_decision(rec, time.time())
            self.assertEqual(len(out), 2)
            sid, why, cls = pr.resume_verdict(rec, time.time())
            self.assertEqual(out, (sid, why))


class SessionStartRecord(unittest.TestCase):
    """`state/session-starts.jsonl` — one row per spawn, saying whether it resumed and why not."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, pr.SESSION_STARTS_FILE)

    def _rows(self):
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_writes_one_row_per_spawn(self):
        pr.record_session_start(self.dir, pr.RESUME_TOO_OLD, "too old", _record())
        pr.record_session_start(self.dir, pr.RESUME_OK, "resuming", _record())
        rows = self._rows()
        self.assertEqual([r["class"] for r in rows], [pr.RESUME_TOO_OLD, pr.RESUME_OK])
        self.assertEqual([r["resumed"] for r in rows], [False, True])
        self.assertTrue(all(r["at"].endswith("Z") for r in rows))

    def test_carries_the_dead_sessions_vitals_when_it_has_them(self):
        pr.record_session_start(self.dir, pr.RESUME_CONTEXT_FULL, "full",
                                _record(context_tokens=910_000, turns_served=31))
        row, = self._rows()
        self.assertEqual(row["context_tokens"], 910_000)
        self.assertEqual(row["turns_served"], 31)
        self.assertEqual(row["reason"], pr.RESPAWN_IDLE)

    def test_omits_vitals_it_does_not_have(self):
        # A cold start has no prior session at all — the row still exists, it just says less.
        self.assertTrue(pr.record_session_start(self.dir, pr.RESUME_NO_RECORD, "cold", None))
        row, = self._rows()
        self.assertNotIn("context_tokens", row)
        self.assertEqual(row["class"], pr.RESUME_NO_RECORD)

    def test_never_raises_and_never_blocks_a_spawn(self):
        """The `mouth.record_assertion` contract: a failed append costs the ROW, never the session.

        No caller wraps this in a try/except and none should — which is only safe if it is true here.
        Squat the path with a directory so the open cannot succeed.
        """
        os.makedirs(self.path)
        self.assertFalse(pr.record_session_start(self.dir, pr.RESUME_OK, "resuming", _record()))

    def test_survives_an_unserialisable_record(self):
        self.assertFalse(
            pr.record_session_start(self.dir, pr.RESUME_OK, "ok", _record(context_tokens=object())))


class ThreadTailCap(unittest.TestCase):
    """The cold-start continuity block sends THREAD_CAP turns, not six.

    `append_thread` retains THREAD_CAP (20) explicitly for "cross-session continuity", and its only
    reader — the sole continuity input to `GROUNDING` — took `thread[-6:]`, dropping fourteen of them
    at the one moment continuity is needed. No decision chose 6; the constants simply disagreed and
    nothing had compared them. Slicing by the same constant that fills the file is what stops them
    drifting apart again, so that is what this pins.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_sends_thread_cap_turns(self):
        for i in range(pr.THREAD_CAP + 10):
            pr.append_thread(self.dir, "owner" if i % 2 else "assistant", f"turn-{i}")
        tail = pr.thread_tail(self.dir)
        self.assertEqual(tail.count("turn-"), pr.THREAD_CAP)
        self.assertIn(f"turn-{pr.THREAD_CAP + 9}", tail)      # the newest survives
        self.assertNotIn("turn-9:", tail)                      # everything older is dropped

    def test_slices_by_the_constant_not_a_literal(self):
        # If THREAD_CAP moves and the slice does not, this goes red — which is the whole point.
        for i in range(pr.THREAD_CAP + 5):
            pr.append_thread(self.dir, "owner", f"turn-{i}")
        with mock.patch.object(pr, "THREAD_CAP", 3):
            self.assertEqual(pr.thread_tail(self.dir).count("turn-"), 3)

    def test_empty_thread_is_empty_string(self):
        self.assertEqual(pr.thread_tail(self.dir), "")


class SessionOpenedProducer(unittest.TestCase):
    """session-trace phase 0's producer half: the id actually gets written where the reader looks.

    `test_mouth.py`'s standing lesson applies — the risk was never a buggy writer, it was a writer
    nothing calls. `state/metrics.jsonl` is the precedent: a contract that produced zero rows in a
    month because nothing invoked it.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _rows(self):
        with open(os.path.join(self.dir, pr.SESSION_STARTS_FILE), encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def test_the_stub_session_fires_the_hook_on_start_not_construction(self):
        # The hook is attached by make_session AFTER the object exists, so firing it in __init__
        # would raise before it could ever be set — which is how it was written first.
        s = pr.StubWarmSession()
        self.assertIsNone(s._on_opened, "must exist and be None before make_session attaches it")
        seen = []
        s._on_opened = seen.append
        self.assertEqual(seen, [], "constructing must not fire it")
        s.start()
        self.assertEqual(seen, ["stub-session"])

    def test_mark_session_opened_pairs_with_the_decision(self):
        pr.record_session_start(self.dir, pr.RESUME_TOO_OLD, "3h old", _record())
        pr.mark_session_opened(self.dir, "sess-new")
        decision, opened = self._rows()
        self.assertEqual(decision["kind"], "decision")
        self.assertEqual(opened, {**opened, "kind": "opened", "session_id": "sess-new"})
        self.assertIn("at", opened)

    def test_it_never_raises(self):
        # Same contract as record_session_start and mouth.record_assertion: the row, never the spawn.
        os.makedirs(os.path.join(self.dir, pr.SESSION_STARTS_FILE))
        self.assertFalse(pr.mark_session_opened(self.dir, "sess-new"))


if __name__ == "__main__":
    unittest.main()
