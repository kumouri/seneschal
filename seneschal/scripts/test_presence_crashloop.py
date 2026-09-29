#!/usr/bin/env python3
"""Tests for presence.py's SELF crash-loop guard (the daemon-side half of the seneschald auto-revive
work — see the "self crash-loop guard" section in presence.py).

Covers: boot-attempt recording + rolling-window pruning, the >K-in-M-min trip predicate, the
loud-once-not-every-pass trip (sentinel write + Telegram push dedupe), the sustained-healthy self-heal,
and — critically — the fail-open/defensive posture (a garbage boot-attempts.json must NOT crash the
startup path, which is the exact failure this guard exists to prevent).

Stdlib ``unittest`` only; each test uses a throwaway temp dir so nothing touches the real ``state/``.

Run:  python -m unittest seneschal.scripts.test_presence_crashloop   (or)   python test_presence_crashloop.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class BootAttemptWindow(unittest.TestCase):
    """record_boot_attempt / prune_boot_attempts / _load_boot_attempts / is_crashloop."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crashloop-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _boots_path(self):
        return pr.boot_attempts_path(self.dir)

    def test_record_appends_and_persists(self):
        window = pr.record_boot_attempt(self.dir, now=NOW)
        self.assertEqual(len(window), 1)
        stored = _read_json(self._boots_path())
        self.assertEqual(stored, [NOW.isoformat().replace("+00:00", "Z")])

    def test_four_fast_boots_trip_three_do_not(self):
        # Three boots inside the window: NOT a loop (budget is CRASHLOOP_MAX_BOOTS = 3, trip is >3).
        for i in range(3):
            window = pr.record_boot_attempt(self.dir, now=NOW + timedelta(seconds=i))
        self.assertFalse(pr.is_crashloop(window))
        # The 4th fast boot exceeds the budget -> trip.
        window = pr.record_boot_attempt(self.dir, now=NOW + timedelta(seconds=3))
        self.assertEqual(len(window), 4)
        self.assertTrue(pr.is_crashloop(window))

    def test_boots_spread_past_the_window_never_trip(self):
        # One boot every 2 minutes: the window (5 min) only ever holds ~3, so this steady-but-slow
        # relaunch pattern is NOT a fast crash loop and must not trip. (The watchdog's slower revival
        # budget is what covers that regime.)
        window = []
        for i in range(6):
            window = pr.record_boot_attempt(self.dir, now=NOW + timedelta(minutes=2 * i))
            self.assertFalse(pr.is_crashloop(window), f"tripped at boot {i}")
        # Latest window holds only the boots within the last 5 min.
        self.assertLessEqual(len(window), pr.CRASHLOOP_MAX_BOOTS)

    def test_prune_keeps_a_future_stamp(self):
        # A future stamp (clock skew / hand-edit) is KEPT — dropping it would let a loop hide behind a
        # bad clock.
        future = NOW + timedelta(minutes=30)
        kept = pr.prune_boot_attempts([NOW - timedelta(minutes=10), future], NOW)
        self.assertIn(future, kept)
        self.assertNotIn(NOW - timedelta(minutes=10), kept)

    def test_boundary_stamp_at_window_edge_is_kept(self):
        edge = NOW - timedelta(minutes=pr.CRASHLOOP_WINDOW_MIN)
        kept = pr.prune_boot_attempts([edge, NOW - timedelta(minutes=pr.CRASHLOOP_WINDOW_MIN + 1)], NOW)
        self.assertEqual(kept, [edge])


class DefensiveLoad(unittest.TestCase):
    """A corrupt boot-attempts.json must read as 'no prior boots', never raise — the guard exists to
    PREVENT a state-cache crash loop, so it can't be the source of one."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crashloop-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _write_raw(self, text):
        with open(pr.boot_attempts_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_garbage_json_reads_empty(self):
        self._write_raw("{not json at all")
        self.assertEqual(pr._load_boot_attempts(self.dir), [])

    def test_non_list_top_level_reads_empty(self):
        self._write_raw('{"boots": 3}')
        self.assertEqual(pr._load_boot_attempts(self.dir), [])

    def test_bad_entries_are_skipped_not_fatal(self):
        good = NOW.isoformat().replace("+00:00", "Z")
        self._write_raw(json.dumps([good, "not-a-timestamp", 42, None]))
        loaded = pr._load_boot_attempts(self.dir)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0], NOW)

    def test_missing_file_reads_empty(self):
        self.assertEqual(pr._load_boot_attempts(self.dir), [])

    def test_record_after_garbage_starts_fresh(self):
        # A poisoned file must not stop a boot from being recorded, and the recorded window is just this
        # boot (the garbage contributed nothing).
        self._write_raw("garbage")
        window = pr.record_boot_attempt(self.dir, now=NOW)
        self.assertEqual(window, [NOW])


class TripGuard(unittest.TestCase):
    """trip_crashloop_guard: writes the sentinel, alerts ONCE, and never raises."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crashloop-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.logs = []
        self.stamps = [NOW + timedelta(seconds=i) for i in range(4)]

    def _sentinel(self):
        return pr.crashloop_sentinel_path(self.dir)

    def test_writes_sentinel_and_pushes_once(self):
        with mock.patch.object(pr, "send_telegram", return_value={"ok": True}) as send:
            pr.trip_crashloop_guard(self.dir, "telegram.env", self.logs.append, self.stamps, now=NOW)
        self.assertTrue(os.path.exists(self._sentinel()))
        payload = _read_json(self._sentinel())
        self.assertEqual(payload["boots_in_window"], 4)
        self.assertEqual(payload["window_min"], pr.CRASHLOOP_WINDOW_MIN)
        self.assertEqual(send.call_count, 1)

    def test_does_not_realert_when_sentinel_already_present(self):
        # First trip alerts; a second trip (a repeat boot re-tripping the same episode) must NOT re-spam.
        with mock.patch.object(pr, "send_telegram", return_value={"ok": True}) as send:
            pr.trip_crashloop_guard(self.dir, "telegram.env", self.logs.append, self.stamps, now=NOW)
            pr.trip_crashloop_guard(self.dir, "telegram.env", self.logs.append, self.stamps, now=NOW)
        self.assertEqual(send.call_count, 1)

    def test_sentinel_written_even_if_telegram_fails(self):
        # A failed push must never stop the sentinel being written — the sentinel is what makes the
        # watchdog stand down, and losing it would let the revive fight the guard.
        with mock.patch.object(pr, "send_telegram", return_value={"ok": False, "error": "no env"}):
            pr.trip_crashloop_guard(self.dir, "telegram.env", self.logs.append, self.stamps, now=NOW)
        self.assertTrue(os.path.exists(self._sentinel()))

    def test_never_raises_when_telegram_errors(self):
        with mock.patch.object(pr, "send_telegram", side_effect=RuntimeError("boom")):
            # Must not propagate — the guard runs on the failing startup path.
            pr.trip_crashloop_guard(self.dir, "telegram.env", self.logs.append, self.stamps, now=NOW)
        self.assertTrue(os.path.exists(self._sentinel()))


class ClearAndSelfHeal(unittest.TestCase):
    """clear_crashloop_state + maybe_clear_crashloop_on_sustained."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crashloop-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.logs = []
        # Seed a tripped state: a sentinel + a boot-attempts window.
        with mock.patch.object(pr, "send_telegram", return_value={"ok": True}):
            pr.trip_crashloop_guard(self.dir, "t.env", self.logs.append,
                                    [NOW + timedelta(seconds=i) for i in range(4)], now=NOW)
        pr.record_boot_attempt(self.dir, now=NOW)

    def test_clear_removes_sentinel_and_boot_attempts(self):
        self.assertTrue(os.path.exists(pr.crashloop_sentinel_path(self.dir)))
        self.assertTrue(os.path.exists(pr.boot_attempts_path(self.dir)))
        pr.clear_crashloop_state(self.dir)
        self.assertFalse(os.path.exists(pr.crashloop_sentinel_path(self.dir)))
        self.assertFalse(os.path.exists(pr.boot_attempts_path(self.dir)))

    def test_clear_is_idempotent_on_missing_files(self):
        pr.clear_crashloop_state(self.dir)
        pr.clear_crashloop_state(self.dir)  # no raise on the second pass

    def _state(self, boot_monotonic):
        return types.SimpleNamespace(boot_monotonic=boot_monotonic, crashloop_cleared=False)

    def _args(self):
        return types.SimpleNamespace(state_dir=self.dir)

    def test_self_heal_waits_for_the_settle_window(self):
        # Uptime below the settle window: nothing cleared yet.
        state = self._state(time.monotonic())  # ~0s uptime
        pr.maybe_clear_crashloop_on_sustained(state, self._args(), self.logs.append)
        self.assertFalse(state.crashloop_cleared)
        self.assertTrue(os.path.exists(pr.crashloop_sentinel_path(self.dir)))

    def test_self_heal_clears_after_the_settle_window(self):
        # Uptime past the settle window: clears sentinel + window, exactly once (latched).
        state = self._state(time.monotonic() - (pr.CRASHLOOP_SETTLE_SEC + 5))
        pr.maybe_clear_crashloop_on_sustained(state, self._args(), self.logs.append)
        self.assertTrue(state.crashloop_cleared)
        self.assertFalse(os.path.exists(pr.crashloop_sentinel_path(self.dir)))
        self.assertFalse(os.path.exists(pr.boot_attempts_path(self.dir)))

    def test_self_heal_is_one_shot(self):
        state = self._state(time.monotonic() - (pr.CRASHLOOP_SETTLE_SEC + 5))
        pr.maybe_clear_crashloop_on_sustained(state, self._args(), self.logs.append)
        # Re-trip the sentinel after the first clear; a second call must NOT clear it again (latched).
        with mock.patch.object(pr, "send_telegram", return_value={"ok": True}):
            pr.trip_crashloop_guard(self.dir, "t.env", self.logs.append, [NOW], now=NOW)
        pr.maybe_clear_crashloop_on_sustained(state, self._args(), self.logs.append)
        self.assertTrue(os.path.exists(pr.crashloop_sentinel_path(self.dir)))


if __name__ == "__main__":
    unittest.main()
