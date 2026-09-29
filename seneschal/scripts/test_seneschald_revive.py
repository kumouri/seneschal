#!/usr/bin/env python3
"""Tests for seneschald_revive.py — the revive/don't-revive DECISION `seneschald-control.ps1` consults every
~10 minutes (see seneschal/docs/seneschald-revive-spec.md and the module docstring in seneschald_revive.py).

Stdlib ``unittest`` only. Covers the decision table (sentinel, give-up latch, budget, window roll +
its boundary) and the fail-closed paths (missing/corrupt/malformed presence-health.json, unexpected
exceptions) — plus the CLI surface both `--decide` and `--record-healthy` present to the .ps1 caller.

Run:  python -m unittest discover -s seneschal/scripts -p "test_seneschald_revive.py"
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import seneschald_revive as mr  # noqa: E402

NOW = datetime(2026, 7, 19, 18, 30, 0, tzinfo=timezone.utc)


def _z(dt: datetime) -> str:
    # Mirrors seneschald_revive._iso: second precision + explicit Z, the one canonical on-disk shape shared
    # with seneschald-control.ps1's ConvertTo-IsoUtcString.
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_raw(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _write_health(path: str, **fields) -> None:
    _write_raw(path, json.dumps(fields))


class DecideUnit(unittest.TestCase):
    """Direct calls into mr.decide() — the core decision table."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.health_path = os.path.join(self.dir, mr.HEALTH_FILE_NAME)
        self.stopped_path = os.path.join(self.dir, mr.STOPPED_SENTINEL_NAME)

    # ---- rule 1: the deliberate-stop sentinel -----------------------------------------------------

    def test_sentinel_present_wins_over_available_budget(self):
        # Budget is wide open (0 revivals used) — the sentinel must still win.
        _write_health(self.health_path, revivals=0, revival_window_start=None, revival_gave_up=False)
        _write_raw(self.stopped_path, "")
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "deliberately-stopped")

    def test_sentinel_present_with_no_health_file(self):
        _write_raw(self.stopped_path, "")
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "deliberately-stopped")
        self.assertEqual(result["next_state"], {"revivals": 0, "revival_window_start": None, "revival_gave_up": False})

    # ---- rule 2: missing health file is a fresh record, not an error -------------------------------

    def test_missing_health_file_revives_and_starts_a_window(self):
        result = mr.decide(self.dir, NOW)
        self.assertTrue(result["revive"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["next_state"]["revivals"], 1)
        self.assertEqual(result["next_state"]["revival_window_start"], _z(NOW))
        self.assertFalse(result["next_state"]["revival_gave_up"])

    # ---- rule 4: under budget --------------------------------------------------------------------

    def test_under_budget_revives_and_increments(self):
        window_start = NOW - timedelta(minutes=10)
        _write_health(self.health_path, revivals=1, revival_window_start=_z(window_start), revival_gave_up=False)
        result = mr.decide(self.dir, NOW)
        self.assertTrue(result["revive"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["next_state"]["revivals"], 2)
        self.assertEqual(result["next_state"]["revival_window_start"], _z(window_start))  # window untouched
        self.assertFalse(result["next_state"]["revival_gave_up"])

    def test_at_budget_in_live_window_gives_up(self):
        window_start = NOW - timedelta(minutes=10)
        _write_health(self.health_path, revivals=mr.MAX_REVIVALS, revival_window_start=_z(window_start),
                       revival_gave_up=False)
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "budget-exhausted")
        self.assertTrue(result["next_state"]["revival_gave_up"])
        self.assertEqual(result["next_state"]["revivals"], mr.MAX_REVIVALS)  # not incremented further
        self.assertEqual(result["next_state"]["revival_window_start"], _z(window_start))  # window untouched

    # ---- rule 3: give-up latches, survives a rolled window -----------------------------------------

    def test_gave_up_already_does_not_revive(self):
        window_start = NOW - timedelta(minutes=10)
        _write_health(self.health_path, revivals=mr.MAX_REVIVALS, revival_window_start=_z(window_start),
                       revival_gave_up=True)
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "already-gave-up")

    def test_gave_up_does_not_revive_even_after_window_rolled(self):
        # The window is long expired (well past REVIVAL_WINDOW_MIN) — a naive implementation that
        # checks the window before the latch would roll the counter and revive. The latch must win.
        window_start = NOW - timedelta(minutes=mr.REVIVAL_WINDOW_MIN * 4)
        _write_health(self.health_path, revivals=mr.MAX_REVIVALS, revival_window_start=_z(window_start),
                       revival_gave_up=True)
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "already-gave-up")
        # Echoed unchanged — the stale window must NOT get silently rolled by this check.
        self.assertEqual(result["next_state"]["revival_window_start"], _z(window_start))
        self.assertEqual(result["next_state"]["revivals"], mr.MAX_REVIVALS)

    # ---- window rolling + its exact boundary --------------------------------------------------------

    def test_window_rolled_resets_counter_and_revives(self):
        window_start = NOW - timedelta(minutes=mr.REVIVAL_WINDOW_MIN + 1)  # just past expiry
        _write_health(self.health_path, revivals=mr.MAX_REVIVALS, revival_window_start=_z(window_start),
                       revival_gave_up=False)
        result = mr.decide(self.dir, NOW)
        self.assertTrue(result["revive"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["next_state"]["revivals"], 1)  # 0 (reset) + 1
        self.assertEqual(result["next_state"]["revival_window_start"], _z(NOW))  # new window

    def test_window_boundary_exactly_at_limit_counts_as_rolled(self):
        window_start = NOW - timedelta(minutes=mr.REVIVAL_WINDOW_MIN)  # exactly on the line
        _write_health(self.health_path, revivals=mr.MAX_REVIVALS, revival_window_start=_z(window_start),
                       revival_gave_up=False)
        result = mr.decide(self.dir, NOW)
        self.assertTrue(result["revive"])
        self.assertEqual(result["next_state"]["revivals"], 1)
        self.assertEqual(result["next_state"]["revival_window_start"], _z(NOW))

    def test_window_boundary_one_minute_short_is_still_live(self):
        window_start = NOW - timedelta(minutes=mr.REVIVAL_WINDOW_MIN - 1)  # not yet expired
        _write_health(self.health_path, revivals=mr.MAX_REVIVALS, revival_window_start=_z(window_start),
                       revival_gave_up=False)
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "budget-exhausted")
        self.assertEqual(result["next_state"]["revival_window_start"], _z(window_start))  # unrolled

    # ---- rule 5: fail closed ------------------------------------------------------------------------

    def test_corrupt_json_is_undetermined_and_does_not_raise(self):
        _write_raw(self.health_path, "{not valid json at all!!")
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "undetermined")

    def test_undetermined_persists_nothing(self):
        """`undetermined` must emit next_state=None so the caller writes NOTHING.

        Emitting a zeroed next_state instead would let a transient unreadable health file (it is
        rewritten every cycle, so reads can lose a race) silently reset the crash-loop counter — handing
        a genuinely crash-looping daemon a fresh budget each time a read raced, so the give-up gate might
        never fire. "I don't know" must mean "change nothing", not "assume the best".
        """
        # A record that WOULD have latched give-up, made unreadable: the counters must survive untouched.
        _write_raw(self.health_path, "{not valid json at all!!")
        result = mr.decide(self.dir, NOW)
        self.assertEqual(result["reason"], "undetermined")
        self.assertIsNone(result["next_state"],
                          "undetermined must not hand the caller any state to persist")

    def test_every_undetermined_path_persists_nothing(self):
        for label, payload in (
            ("corrupt", "{nope"),
            ("non-dict", json.dumps(["a"])),
            ("bad-window", json.dumps({"revivals": 1, "revival_window_start": "nope",
                                       "revival_gave_up": False})),
            ("bad-revivals", json.dumps({"revivals": "three", "revival_window_start": None,
                                         "revival_gave_up": False})),
        ):
            with self.subTest(label):
                _write_raw(self.health_path, payload)
                result = mr.decide(self.dir, NOW)
                self.assertEqual(result["reason"], "undetermined")
                self.assertIsNone(result["next_state"])

    def test_non_dict_json_is_undetermined(self):
        _write_raw(self.health_path, json.dumps(["not", "a", "dict"]))
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "undetermined")

    def test_malformed_window_start_is_undetermined(self):
        _write_health(self.health_path, revivals=1, revival_window_start="not-a-timestamp", revival_gave_up=False)
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "undetermined")

    def test_wrong_typed_revivals_is_undetermined(self):
        _write_health(self.health_path, revivals="three", revival_window_start=None, revival_gave_up=False)
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "undetermined")

    def test_negative_revivals_is_undetermined(self):
        _write_health(self.health_path, revivals=-1, revival_window_start=None, revival_gave_up=False)
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "undetermined")

    def test_wrong_typed_gave_up_is_undetermined(self):
        _write_health(self.health_path, revivals=0, revival_window_start=None, revival_gave_up="nope")
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "undetermined")

    def test_unexpected_exception_in_decide_is_undetermined(self):
        # Simulate a completely unanticipated failure mode reaching decide() — e.g. os.path.exists
        # itself blowing up on a hostile/locked path. The catch-all must still answer, never raise.
        real_exists = mr.os.path.exists

        def _boom(path):
            if path.endswith(mr.STOPPED_SENTINEL_NAME):
                raise OSError("simulated unexpected failure")
            return real_exists(path)

        mr.os.path.exists = _boom
        try:
            result = mr.decide(self.dir, NOW)
        finally:
            mr.os.path.exists = real_exists
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "undetermined")

    # ---- timestamp handling -------------------------------------------------------------------------

    def test_naive_offsetless_timestamp_accepted_as_utc(self):
        window_start = NOW - timedelta(minutes=10)
        naive = window_start.replace(tzinfo=None).isoformat()  # genuinely no 'Z', no offset at all
        self.assertNotIn("+", naive)
        self.assertFalse(naive.endswith("Z"))
        _write_health(self.health_path, revivals=1, revival_window_start=naive, revival_gave_up=False)
        result = mr.decide(self.dir, NOW)  # must not raise
        self.assertTrue(result["revive"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["next_state"]["revivals"], 2)

    def test_plus_00_00_offset_accepted(self):
        window_start = NOW - timedelta(minutes=10)
        explicit = window_start.isoformat()  # already '+00:00' form
        _write_health(self.health_path, revivals=1, revival_window_start=explicit, revival_gave_up=False)
        result = mr.decide(self.dir, NOW)
        self.assertTrue(result["revive"])
        self.assertEqual(result["next_state"]["revivals"], 2)


class CrashloopSentinelUnit(unittest.TestCase):
    """Rule 1b — the daemon's OWN self crash-loop guard (presence.py) dropped seneschald-crashloop. The
    watchdog must treat it exactly like seneschald-stopped: never auto-revive while it's present, so the two
    mechanisms don't fight. See presence.py's "self crash-loop guard" section."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.health_path = os.path.join(self.dir, mr.HEALTH_FILE_NAME)
        self.stopped_path = os.path.join(self.dir, mr.STOPPED_SENTINEL_NAME)
        self.crashloop_path = os.path.join(self.dir, mr.CRASHLOOP_SENTINEL_NAME)

    def test_crashloop_sentinel_blocks_revive(self):
        _write_raw(self.crashloop_path, "")
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "crash-looping")

    def test_crashloop_wins_over_available_budget(self):
        # Budget wide open (0 revivals) — the daemon's give-up must still win.
        _write_health(self.health_path, revivals=0, revival_window_start=None, revival_gave_up=False)
        _write_raw(self.crashloop_path, "")
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "crash-looping")

    def test_crashloop_with_no_health_file_echoes_fresh_record(self):
        _write_raw(self.crashloop_path, "")
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "crash-looping")
        self.assertEqual(result["next_state"],
                         {"revivals": 0, "revival_window_start": None, "revival_gave_up": False})

    def test_crashloop_does_not_touch_revival_bookkeeping(self):
        # A crash-loop give-up says nothing about the revival counter — echo it back unchanged, never
        # increment or reset it (that's the same discipline the deliberate-stop path uses).
        window_start = NOW - timedelta(minutes=10)
        _write_health(self.health_path, revivals=2, revival_window_start=_z(window_start), revival_gave_up=False)
        _write_raw(self.crashloop_path, "")
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "crash-looping")
        self.assertEqual(result["next_state"]["revivals"], 2)
        self.assertEqual(result["next_state"]["revival_window_start"], _z(window_start))
        self.assertFalse(result["next_state"]["revival_gave_up"])

    def test_deliberate_stop_wins_over_crashloop(self):
        # Both sentinels present: the deliberate-stop check runs first, so a human's explicit stop is the
        # reported reason. Either way the answer is don't-revive; this only pins the precedence so the
        # log/telemetry names the stronger signal.
        _write_raw(self.stopped_path, "")
        _write_raw(self.crashloop_path, "")
        result = mr.decide(self.dir, NOW)
        self.assertFalse(result["revive"])
        self.assertEqual(result["reason"], "deliberately-stopped")


class RecordHealthyUnit(unittest.TestCase):
    def test_returns_full_reset(self):
        result = mr.record_healthy()
        self.assertEqual(result, {"revivals": 0, "revival_window_start": None, "revival_gave_up": False})


class CLIBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def run_cli(self, *argv):
        """Invoke the CLI in-process; return (rc, parsed-json-from-last-line)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mr.main(["--state-dir", self.dir, *argv])
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertTrue(lines, "CLI printed no output")
        return rc, json.loads(lines[-1])


class CLIDecideMode(CLIBase):
    def test_missing_health_prints_valid_json_last_line_and_exits_0(self):
        rc, verdict = self.run_cli("--decide", "--now", _z(NOW))
        self.assertEqual(rc, 0)
        self.assertTrue(verdict["revive"])
        self.assertEqual(verdict["reason"], "ok")
        self.assertEqual(verdict["next_state"]["revivals"], 1)

    def test_sentinel_present_exits_0_with_deliberately_stopped(self):
        _write_raw(os.path.join(self.dir, mr.STOPPED_SENTINEL_NAME), "")
        rc, verdict = self.run_cli("--decide", "--now", _z(NOW))
        self.assertEqual(rc, 0)
        self.assertFalse(verdict["revive"])
        self.assertEqual(verdict["reason"], "deliberately-stopped")

    def test_budget_exhausted_exits_0(self):
        window_start = NOW - timedelta(minutes=10)
        _write_health(os.path.join(self.dir, mr.HEALTH_FILE_NAME), revivals=mr.MAX_REVIVALS,
                       revival_window_start=_z(window_start), revival_gave_up=False)
        rc, verdict = self.run_cli("--decide", "--now", _z(NOW))
        self.assertEqual(rc, 0)
        self.assertFalse(verdict["revive"])
        self.assertEqual(verdict["reason"], "budget-exhausted")
        self.assertTrue(verdict["next_state"]["revival_gave_up"])

    def test_already_gave_up_exits_0(self):
        _write_health(os.path.join(self.dir, mr.HEALTH_FILE_NAME), revivals=mr.MAX_REVIVALS,
                       revival_window_start=_z(NOW), revival_gave_up=True)
        rc, verdict = self.run_cli("--decide", "--now", _z(NOW))
        self.assertEqual(rc, 0)
        self.assertFalse(verdict["revive"])
        self.assertEqual(verdict["reason"], "already-gave-up")

    def test_corrupt_health_exits_0_with_undetermined(self):
        _write_raw(os.path.join(self.dir, mr.HEALTH_FILE_NAME), "{{{garbage")
        rc, verdict = self.run_cli("--decide", "--now", _z(NOW))
        self.assertEqual(rc, 0)
        self.assertFalse(verdict["revive"])
        self.assertEqual(verdict["reason"], "undetermined")

    def test_defaults_now_to_wall_clock_without_raising(self):
        # No --now at all: must fall back to real UTC now and still produce one clean verdict line.
        rc, verdict = self.run_cli("--decide")
        self.assertEqual(rc, 0)
        self.assertIn(verdict["reason"], ("ok", "deliberately-stopped", "budget-exhausted",
                                           "already-gave-up", "undetermined"))

    def test_bad_now_is_a_tool_failure_not_a_verdict(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mr.main(["--state-dir", self.dir, "--decide", "--now", "not-a-timestamp-at-all"])
        self.assertEqual(rc, 1)
        line = buf.getvalue().splitlines()[-1]
        payload = json.loads(line)  # still must be valid JSON, just shaped as a tool error
        self.assertFalse(payload.get("ok", True))


class CLIRecordHealthyMode(CLIBase):
    def test_prints_bare_reset_and_exits_0(self):
        rc, payload = self.run_cli("--record-healthy")
        self.assertEqual(rc, 0)
        self.assertEqual(payload, {"revivals": 0, "revival_window_start": None, "revival_gave_up": False})

    def test_ignores_existing_state_on_disk(self):
        # --record-healthy is an unconditional reset, not a merge with whatever's currently stored.
        window_start = NOW - timedelta(minutes=5)
        _write_health(os.path.join(self.dir, mr.HEALTH_FILE_NAME), revivals=mr.MAX_REVIVALS,
                       revival_window_start=_z(window_start), revival_gave_up=True)
        rc, payload = self.run_cli("--record-healthy")
        self.assertEqual(rc, 0)
        self.assertEqual(payload, {"revivals": 0, "revival_window_start": None, "revival_gave_up": False})


# ---- confirm_sustained: the revive settle-window check --------------------------------------------


def _lock(started_at: datetime, heartbeat: datetime) -> dict:
    """A presence.lock shaped the way presence.py's write_lock/touch_lock write it."""
    return {"pid": 4321, "started_at": _z(started_at), "heartbeat": _z(heartbeat)}


class ConfirmSustainedUnit(unittest.TestCase):
    """mr.confirm_sustained() — 'has the daemon been ticking continuously for >= min_uptime_sec?'"""

    STALE = mr.DEFAULT_STALE_SEC        # 180
    MIN_UP = mr.DEFAULT_MIN_UPTIME_SEC  # 90

    def _check(self, lock):
        return mr.confirm_sustained(lock, NOW, self.STALE, self.MIN_UP)

    def test_healthy_long_uptime_is_sustained(self):
        # Up ~200s, heartbeat 2s ago → ticking now AND well past the settle window.
        r = self._check(_lock(NOW - timedelta(seconds=200), NOW - timedelta(seconds=2)))
        self.assertTrue(r["sustained"])
        self.assertEqual(r["reason"], "ok")

    def test_crash_loop_twitch_is_not_sustained(self):
        # THE crash-loop regression: booted, wrote ONE heartbeat, died a second later. We check 91s
        # after that frozen heartbeat — still < the 180s stale window, so the heartbeat reads "fresh"
        # (exactly what fooled the old check) — but observed uptime is ~1s, so it is NOT sustained.
        r = self._check(_lock(NOW - timedelta(seconds=92), NOW - timedelta(seconds=91)))
        self.assertFalse(r["sustained"])
        self.assertEqual(r["reason"], "uptime-too-short")
        self.assertLess(r["hb_age_sec"], self.STALE)  # proves the heartbeat WAS masquerading as fresh

    def test_dead_stale_heartbeat_is_not_sustained(self):
        r = self._check(_lock(NOW - timedelta(seconds=1000), NOW - timedelta(seconds=500)))
        self.assertFalse(r["sustained"])
        self.assertEqual(r["reason"], "heartbeat-stale")

    def test_uptime_exactly_at_threshold_is_sustained(self):
        # '>=' boundary: exactly min_uptime_sec of observed uptime counts as sustained.
        r = self._check(_lock(NOW - timedelta(seconds=self.MIN_UP + 2), NOW - timedelta(seconds=2)))
        self.assertTrue(r["sustained"])

    def test_none_lock_is_not_sustained(self):
        r = self._check(None)
        self.assertFalse(r["sustained"])
        self.assertEqual(r["reason"], "no-lock")

    def test_missing_fields_is_not_sustained(self):
        r = self._check({"pid": 1})
        self.assertFalse(r["sustained"])
        self.assertEqual(r["reason"], "lock-missing-fields")

    def test_garbled_timestamps_are_not_sustained(self):
        r = self._check({"started_at": "not-a-time", "heartbeat": "also-not"})
        self.assertFalse(r["sustained"])
        self.assertEqual(r["reason"], "lock-unreadable")

    def test_heartbeat_before_started_at_is_not_sustained(self):
        # Negative observed uptime (clock weirdness / stale field) fails closed, doesn't raise.
        r = self._check(_lock(NOW - timedelta(seconds=2), NOW - timedelta(seconds=100)))
        self.assertFalse(r["sustained"])
        self.assertEqual(r["reason"], "uptime-too-short")


class CLIConfirmSustainedMode(CLIBase):
    def _write_lock(self, started_at, heartbeat):
        _write_raw(os.path.join(self.dir, mr.LOCK_FILE_NAME), json.dumps(_lock(started_at, heartbeat)))

    def test_absent_lock_prints_not_sustained_exits_0(self):
        rc, payload = self.run_cli("--confirm-sustained", "--now", _z(NOW))
        self.assertEqual(rc, 0)
        self.assertFalse(payload["sustained"])
        self.assertEqual(payload["reason"], "no-lock")

    def test_sustained_lock_prints_true(self):
        self._write_lock(NOW - timedelta(seconds=200), NOW - timedelta(seconds=2))
        rc, payload = self.run_cli("--confirm-sustained", "--now", _z(NOW))
        self.assertEqual(rc, 0)
        self.assertTrue(payload["sustained"])

    def test_crash_loop_lock_prints_false(self):
        self._write_lock(NOW - timedelta(seconds=92), NOW - timedelta(seconds=91))
        rc, payload = self.run_cli("--confirm-sustained", "--now", _z(NOW))
        self.assertEqual(rc, 0)
        self.assertFalse(payload["sustained"])
        self.assertEqual(payload["reason"], "uptime-too-short")

    def test_custom_min_uptime_flag_is_honored(self):
        # ~30s of uptime: not sustained at the 90s default, but sustained once the caller lowers the bar.
        self._write_lock(NOW - timedelta(seconds=32), NOW - timedelta(seconds=2))
        _, strict = self.run_cli("--confirm-sustained", "--now", _z(NOW))
        self.assertFalse(strict["sustained"])
        _, loose = self.run_cli("--confirm-sustained", "--min-uptime-sec", "10", "--now", _z(NOW))
        self.assertTrue(loose["sustained"])


if __name__ == "__main__":
    unittest.main()
