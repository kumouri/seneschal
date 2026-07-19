#!/usr/bin/env python3
"""Tests for the presence daemon's persisted action queue + headless-spawn serialization helpers.

Stdlib ``unittest`` only (matches the Markdown-skill core; CI byte-compiles Python and this runs green
under ``python -m unittest``). Covers the 2026-07 additions to ``presence.py``:
  * the action queue survives a restart (save → load round-trip, with normalization of legacy entries),
  * the fire-and-forget peek/slot runs are gated to one at a time so their Notion reads don't stampede.

Run:  python -m unittest seneschal.scripts.test_presence_state   (or)   python test_presence_state.py
"""
import argparse
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402


class _FakeProc:
    """Stand-in for subprocess.Popen: poll() returns None while running, an int once finished."""
    def __init__(self, running=True):
        self._running = running

    def poll(self):
        return None if self._running else 0

    def finish(self):
        self._running = False


class ActionQueuePersistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_roundtrip_preserves_queue_order_and_channel(self):
        pending = [("telegram", "hey assistant"), ("discord", "did I take my meds")]
        pr.save_daemon_state(self.dir, pending, last_session_id="sess-1")
        st = pr.load_daemon_state(self.dir)
        self.assertEqual(
            [(i["channel"], i["text"]) for i in st["pending"]],
            [("telegram", "hey assistant"), ("discord", "did I take my meds")],
        )
        self.assertEqual(st.get("last_session_id"), "sess-1")

    def test_missing_file_is_empty_queue(self):
        st = pr.load_daemon_state(self.dir)
        self.assertEqual(st["pending"], [])

    def test_load_normalizes_bad_entries(self):
        # Write a state file with junk: a non-dict, a dict with no text, and a good one missing channel.
        pr.save_json(pr.daemon_state_path(self.dir), {
            "pending": ["a bare string", {"channel": "discord"}, {"text": "keep me"}],
        })
        st = pr.load_daemon_state(self.dir)
        self.assertEqual(st["pending"], [{"channel": "telegram", "text": "keep me", "attempts": 0}])

    def test_corrupt_file_falls_back_to_empty(self):
        with open(pr.daemon_state_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual(pr.load_daemon_state(self.dir)["pending"], [])

    def test_attempts_survive_roundtrip(self):
        # 3-tuples carry the poison-pill turn counter across a restart.
        pr.save_daemon_state(self.dir, [("telegram", "loops me", 2), ("discord", "fresh", 0)])
        st = pr.load_daemon_state(self.dir)
        self.assertEqual([(i["channel"], i["text"], i["attempts"]) for i in st["pending"]],
                         [("telegram", "loops me", 2), ("discord", "fresh", 0)])

    def test_two_tuple_save_is_back_compatible(self):
        # Older callers passing (channel, text) still persist, defaulting attempts to 0.
        pr.save_daemon_state(self.dir, [("telegram", "no counter")])
        st = pr.load_daemon_state(self.dir)
        self.assertEqual(st["pending"], [{"channel": "telegram", "text": "no counter", "attempts": 0}])

    def test_bad_attempts_value_coerced_to_zero(self):
        pr.save_json(pr.daemon_state_path(self.dir), {
            "pending": [{"channel": "telegram", "text": "x", "attempts": "nope"},
                        {"channel": "telegram", "text": "y", "attempts": -5}],
        })
        st = pr.load_daemon_state(self.dir)
        self.assertEqual([i["attempts"] for i in st["pending"]], [0, 0])


class RestartRequest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_absent_by_default(self):
        self.assertFalse(pr.restart_requested(self.dir))

    def test_set_then_clear(self):
        with open(pr.restart_request_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("restart requested\n")
        self.assertTrue(pr.restart_requested(self.dir))
        pr.clear_restart_request(self.dir)
        self.assertFalse(pr.restart_requested(self.dir))

    def test_clear_is_idempotent(self):
        pr.clear_restart_request(self.dir)  # no file — must not raise
        self.assertFalse(pr.restart_requested(self.dir))


class HeadlessSerialization(unittest.TestCase):
    def test_prune_drops_finished_keeps_running(self):
        a, b, c = _FakeProc(True), _FakeProc(False), _FakeProc(True)
        children = [a, b, c]
        pr.prune_children(children)
        self.assertEqual(children, [a, c])  # b (finished) removed, in place

    def test_in_flight_true_only_while_a_child_runs(self):
        children = []
        self.assertFalse(pr.heavy_run_in_flight(children))  # nothing running
        proc = _FakeProc(True)
        children.append(proc)
        self.assertTrue(pr.heavy_run_in_flight(children))   # one running
        proc.finish()
        self.assertFalse(pr.heavy_run_in_flight(children))  # it finished → pruned → clear


class _FakeSession:
    """Minimal stand-in for a warm WarmSession (truthy, not None)."""


class WarmSessionGate(unittest.TestCase):
    """The warm-chat-session gate: peeks/slots defer while a chat turn is mid-flight, never otherwise."""

    def test_busy_requires_both_live_session_and_queued_work(self):
        sess = _FakeSession()
        self.assertFalse(pr.warm_session_busy(None, []))          # no session, no queue → idle
        self.assertFalse(pr.warm_session_busy(None, [("telegram", "hi", 0)]))  # queue but no session
        self.assertFalse(pr.warm_session_busy(sess, []))          # live session, empty queue → idle between turns
        self.assertTrue(pr.warm_session_busy(sess, [("telegram", "hi", 0)]))  # live + queued → mid-turn

    def test_peek_deferred_while_warm_busy(self):
        # A due peek is held when the warm session is mid-turn; it launches once idle.
        d = tempfile.mkdtemp()
        args = argparse.Namespace(peek_interval_min=5, watch_prompt="peek", watch_cmd=None,
                                  stub_brain=True)
        # warm_busy=True → deferred (returns False, launches nothing)
        self.assertFalse(pr.maybe_peek(d, args, lambda *_: None, [], warm_busy=True))
        # warm_busy=False → cadence is due (no last-peek stamp) → it fires (stubbed)
        self.assertTrue(pr.maybe_peek(d, args, lambda *_: None, [], warm_busy=False))

    def test_slots_deferred_while_warm_busy(self):
        # maybe_run_slots returns early (launches nothing) when warm_busy — verified by no slots.json write.
        d = tempfile.mkdtemp()
        args = argparse.Namespace(no_slots=False, stub_brain=False, fake_inbox=None,
                                  slot_catchup_min=180, claude_bin="claude", notion_mcp=None,
                                  permission_mode="bypassPermissions", slot_model=None, model=None)
        pr.maybe_run_slots(d, args, lambda *_: None, [], warm_busy=True)
        self.assertFalse(os.path.exists(os.path.join(d, "slots.json")))  # nothing launched/stamped


class _RcProc:
    """Popen stand-in whose poll() yields None while running, else a chosen return code."""

    def __init__(self, rc=None):
        self._rc = rc  # None = still running

    def poll(self):
        return self._rc


class SlotReap(unittest.TestCase):
    """A slot is stamped done only when its run exits 0; a failed run stays unstamped and retries,
    bounded by max_retries (the fix for a launched-but-crashed morning run vanishing silently)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.when = datetime(2026, 7, 8, 8, 1)  # a Wed 08:01 local — morning-slot territory

    def _stamp(self):
        return pr.load_json(os.path.join(self.dir, "slots.json"), {})

    def test_clean_exit_stamps_and_clears(self):
        children, retries = {"reminders-morning": _RcProc(rc=0)}, {}
        pr.reap_finished_slots(children, self.dir, lambda *_: None, retries, now_local=self.when)
        self.assertEqual(self._stamp().get("reminders-morning"), "2026-07-08")
        self.assertEqual(children, {})   # reaped
        self.assertEqual(retries, {})    # cleared

    def test_running_slot_is_left_untouched(self):
        children = {"reminders-morning": _RcProc(rc=None)}
        pr.reap_finished_slots(children, self.dir, lambda *_: None, {}, now_local=self.when)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "slots.json")))  # not stamped
        self.assertIn("reminders-morning", children)                            # still tracked

    def test_failed_exit_does_not_stamp_and_counts_retry(self):
        children, retries = {"reminders-morning": _RcProc(rc=1)}, {}
        pr.reap_finished_slots(children, self.dir, lambda *_: None, retries, now_local=self.when)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "slots.json")))  # NOT done → relaunches
        self.assertEqual(retries["reminders-morning"], 1)
        self.assertEqual(children, {})   # this attempt reaped; next loop relaunches it

    def test_gives_up_and_stamps_after_max_retries(self):
        retries = {"reminders-morning": pr.SLOT_MAX_RETRIES - 1}
        children = {"reminders-morning": _RcProc(rc=1)}
        pr.reap_finished_slots(children, self.dir, lambda *_: None, retries,
                               max_retries=pr.SLOT_MAX_RETRIES, now_local=self.when)
        self.assertEqual(self._stamp().get("reminders-morning"), "2026-07-08")  # gave up → stamped
        self.assertNotIn("reminders-morning", retries)                          # cleared on give-up


class RouterShadow(unittest.TestCase):
    """The Router advisor, phase 1 (shadow): classify → append one JSONL row; never break a turn."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_shadow_classify_appends_a_log_row(self):
        # Stub router.classify so the test never needs a live Ollama (CI byte-compiles + runs this).
        import router
        orig = router.classify
        router.classify = lambda text, *a, **k: {
            "verdict": "trivial", "category": "ack", "confidence": 0.9,
            "reason": "stub", "model": "qwen3.5:4b"}
        try:
            pr.shadow_classify(self.dir, "telegram", "took my meds", lambda *_: None)
        finally:
            router.classify = orig
        with open(pr.router_log_path(self.dir), encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh.read().splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["channel"], "telegram")
        self.assertEqual(row["text_preview"], "took my meds")
        self.assertEqual(row["verdict"], "trivial")
        self.assertEqual(row["category"], "ack")
        self.assertEqual(row["model"], "qwen3.5:4b")
        self.assertIn("ts", row)

    def test_text_preview_is_truncated_to_80_chars(self):
        import router
        orig = router.classify
        router.classify = lambda text, *a, **k: {
            "verdict": "escalate", "category": "other", "confidence": 0.0,
            "reason": "stub", "model": "qwen3.5:4b"}
        try:
            long_msg = "x" * 200
            pr.shadow_classify(self.dir, "discord", long_msg, lambda *_: None)
        finally:
            router.classify = orig
        with open(pr.router_log_path(self.dir), encoding="utf-8") as fh:
            row = json.loads(fh.read().strip())
        self.assertEqual(len(row["text_preview"]), 80)

    def test_shadow_never_raises_on_classify_error(self):
        # A crash inside router.classify must be swallowed (shadow can't break a chat turn) — no row, no raise.
        import router
        orig = router.classify

        def boom(*_a, **_k):
            raise RuntimeError("ollama exploded")

        router.classify = boom
        try:
            pr.shadow_classify(self.dir, "telegram", "hi", lambda *_: None)  # must not raise
        finally:
            router.classify = orig
        self.assertFalse(os.path.exists(pr.router_log_path(self.dir)))


class SeedDayRollover(unittest.TestCase):
    """maybe_seed_day — the once-per-local-day exact-time reminder seed that retired the four fixed
    reminder slots. Fires on the first tick of each new owner-local date (slots.json[SEED_SLOT_NAME]
    guard, via presence.local_now — owner tz), reusing the slot lifecycle; deferred while warm /
    another headless run in flight; killable via --no-seed-day (and --no-slots)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.today = pr.local_now().strftime("%Y-%m-%d")

    def _args(self, **over):
        base = dict(no_slots=False, no_seed_day=False, stub_brain=False, fake_inbox=None,
                    claude_bin="claude", notion_mcp=None, slack_mcp=None,
                    permission_mode="bypassPermissions", slot_model=None, model=None)
        base.update(over)
        return argparse.Namespace(**base)

    def _stamp(self, name, date):
        pr.save_json(os.path.join(self.dir, "slots.json"), {name: date})

    def test_seeds_on_a_fresh_new_day(self):
        slot_children = {}
        with mock.patch.object(pr.subprocess, "Popen", return_value=_FakeProc()) as m:
            pr.maybe_seed_day(self.dir, self._args(), lambda *_: None, [], False, slot_children)
        self.assertEqual(m.call_count, 1)                    # launched
        self.assertIn(pr.SEED_SLOT_NAME, slot_children)      # tracked so reap can stamp/retry it

    def test_no_seed_when_already_stamped_today(self):
        self._stamp(pr.SEED_SLOT_NAME, self.today)
        with mock.patch.object(pr.subprocess, "Popen", return_value=_FakeProc()) as m:
            pr.maybe_seed_day(self.dir, self._args(), lambda *_: None, [], False, {})
        m.assert_not_called()

    def test_seeds_after_rollover_from_a_stale_stamp(self):
        self._stamp(pr.SEED_SLOT_NAME, "2020-01-01")         # a prior-day stamp → new local date
        with mock.patch.object(pr.subprocess, "Popen", return_value=_FakeProc()) as m:
            pr.maybe_seed_day(self.dir, self._args(), lambda *_: None, [], False, {})
        self.assertEqual(m.call_count, 1)

    def test_deferred_while_warm_busy(self):
        with mock.patch.object(pr.subprocess, "Popen", return_value=_FakeProc()) as m:
            pr.maybe_seed_day(self.dir, self._args(), lambda *_: None, [], True, {})   # warm_busy
        m.assert_not_called()

    def test_disabled_by_no_seed_day_flag(self):
        with mock.patch.object(pr.subprocess, "Popen", return_value=_FakeProc()) as m:
            pr.maybe_seed_day(self.dir, self._args(no_seed_day=True), lambda *_: None, [], False, {})
        m.assert_not_called()

    def test_disabled_by_no_slots_flag(self):
        with mock.patch.object(pr.subprocess, "Popen", return_value=_FakeProc()) as m:
            pr.maybe_seed_day(self.dir, self._args(no_slots=True), lambda *_: None, [], False, {})
        m.assert_not_called()

    def test_deferred_while_other_headless_in_flight(self):
        with mock.patch.object(pr.subprocess, "Popen", return_value=_FakeProc()) as m:
            pr.maybe_seed_day(self.dir, self._args(), lambda *_: None,
                              [_FakeProc(running=True)], False, {})   # a slot/peek already running
        m.assert_not_called()

    def test_no_double_launch_when_already_running(self):
        slot_children = {pr.SEED_SLOT_NAME: _FakeProc(running=True)}
        with mock.patch.object(pr.subprocess, "Popen", return_value=_FakeProc()) as m:
            pr.maybe_seed_day(self.dir, self._args(), lambda *_: None, [], False, slot_children)
        m.assert_not_called()

    def test_reaper_stamps_seed_under_its_name_on_clean_exit(self):
        # The reused slot lifecycle: a clean seed exit stamps slots.json[SEED_SLOT_NAME], so the
        # next-loop guard reads "already seeded today" — the once-per-day contract.
        children = {pr.SEED_SLOT_NAME: _RcProc(rc=0)}
        pr.reap_finished_slots(children, self.dir, lambda *_: None, {}, now_local=datetime.now())
        stamped = pr.load_json(os.path.join(self.dir, "slots.json"), {})
        self.assertEqual(stamped.get(pr.SEED_SLOT_NAME), datetime.now().strftime("%Y-%m-%d"))


class StoreBackendActive(unittest.TestCase):
    """store_backend_active — the backend gate for Notion-only machinery (chiefly the outbox leg of
    the reaction-ack). Same sources + never-raises contract as resolve_store_mcp."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = os.path.join(self.dir, "config.json")
        self.legacy = os.path.join(self.dir, "no-such-notion-mcp.json")  # absent unless created

    def _write_config(self, obj):
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def test_reads_active_backend_from_config(self):
        for backend in ("notion", "obsidian", "markdown"):
            self._write_config({"active": backend, "backends": {}})
            self.assertEqual(pr.store_backend_active(self.config, self.legacy), backend)

    def test_absent_config_and_legacy_is_none(self):
        self.assertIsNone(pr.store_backend_active(self.config, self.legacy))

    def test_legacy_notion_mcp_reads_as_notion(self):
        legacy = os.path.join(self.dir, "notion-mcp.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.assertEqual(pr.store_backend_active(self.config, legacy), "notion")

    def test_unparseable_config_falls_back_to_legacy_probe(self):
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(pr.store_backend_active(self.config, self.legacy))


class ReactionAckOutboxGate(unittest.TestCase):
    """The Phase-B reaction-ack shells reminders_dequeue.py always, but the outbox leg only on the
    notion backend — the outbox is Notion-only (store/notion/mapping.md, 'Outbox — durable act-low
    writes'). Filesystem backends record the local ledger and return **False** so the reaction line
    asks the warm session to perform the store-update itself (acks persist to the store, never just
    to chat; the overlap is idempotent)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.calls = []

    def _run(self, rc=0, stderr=""):
        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            return type("P", (), {"returncode": rc, "stderr": stderr, "stdout": "{}"})()
        return fake_run

    def _ack(self, backend):
        with mock.patch.object(pr, "store_backend_active", return_value=backend), \
             mock.patch.object(pr.subprocess, "run", self._run()):
            return pr.ack_reminder_by_reaction(self.dir, "row-1", lambda *_: None)

    def test_notion_backend_runs_dequeue_and_outbox(self):
        self.assertTrue(self._ack("notion"))
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(any("reminders_dequeue.py" in c for c in self.calls[0]))
        self.assertTrue(any("outbox.py" in c for c in self.calls[1]))
        self.assertIn("ack", self.calls[1])

    def test_filesystem_backend_skips_outbox_and_reports_store_write_pending(self):
        for backend in ("markdown", "obsidian", None):
            self.calls = []
            # False = the store write is NOT journaled — the reaction line must ask the warm
            # session to perform the store-update itself.
            self.assertFalse(self._ack(backend))
            self.assertEqual(len(self.calls), 1, backend)          # dequeue only
            self.assertTrue(any("reminders_dequeue.py" in c for c in self.calls[0]))

    def test_skip_is_logged(self):
        logged = []
        with mock.patch.object(pr, "store_backend_active", return_value="markdown"), \
             mock.patch.object(pr.subprocess, "run", self._run()):
            pr.ack_reminder_by_reaction(self.dir, "row-1", logged.append)
        self.assertTrue(any("outbox skipped" in l for l in logged))


if __name__ == "__main__":
    unittest.main()
