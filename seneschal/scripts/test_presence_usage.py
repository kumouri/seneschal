#!/usr/bin/env python3
"""Tests for presence.py's plan-meter reading task (`maybe_usage_reading`, `usage_plan`).

The daemon half of seneschal/docs/usage-telemetry-spec.md phase 1. What each class guards:

* ``TheCadence``        — §8.1's default and the fact that it is CONFIGURABLE. The hour lives in one
                          named constant and one flag; a test asserts no second copy.
* ``TheBoundaryPair``   — a reading ~5 min either side of the weekly reset (spec Q5). Fires once per
                          side per window, and **never fires LATE** — a "pre-reset" sample taken
                          after the reset is worse than a missing one.
* ``TheDeferralGates``  — the same three ``maybe_peek`` rides, plus §2.6's rule that a reading held
                          past the grace is SKIPPED AND RECORDED, never queued to fire late.
* ``ItNeverSpeaks``     — the scope fence. No push, no nudge, no threshold, on any path. The meter is
                          an instrument, not an alarm — asserted rather than promised.
* ``ItFailsOpen``       — a reading that explodes must cost a row and nothing else. A missed reading
                          is a missing row, never an incident, and never a dead scheduler task.

Nothing here spawns ``claude`` and nothing writes outside its own temp dir.

Run:  python -m unittest seneschal.scripts.test_presence_usage
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402
import usage_probe as up  # noqa: E402
from test_usage_probe import (SAMPLE, FAKE_ACCOUNT, FakeRunner, envelope,  # noqa: E402
                              fake_reader)

# A fixed-offset zone: the arithmetic under test is offset-agnostic, and a named zone would drag the
# host's tz database into a test that has no business depending on it.
TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 8, 27, 14, 0, 0, tzinfo=TZ)
RESET = datetime(2026, 8, 31, 16, 59, 0, tzinfo=TZ)

_REAL_PATHS = (up.CLAUDE_JSON_PATH, up.CREDENTIALS_PATH)


def setUpModule():
    """The identity-file trap, closed here TOO. `test_usage_probe`'s `setUpModule` does not run when
    this module is the one under test — importing a module is not running its fixtures — so the
    guarantee has to be restated rather than inherited."""
    absent = os.path.join(tempfile.mkdtemp(prefix="presence-usage-no-home-"), "absent")
    up.CLAUDE_JSON_PATH = os.path.join(absent, ".claude.json")
    up.CREDENTIALS_PATH = os.path.join(absent, ".credentials.json")


def tearDownModule():
    up.CLAUDE_JSON_PATH, up.CREDENTIALS_PATH = _REAL_PATHS


def args(**over):
    base = dict(state_dir=None, claude_bin="claude", stub_brain=False,
                usage_interval_min=pr.USAGE_INTERVAL_MIN_DEFAULT,
                usage_model=up.DEFAULT_MODEL, usage_timeout=60,
                usage_defer_grace_min=pr.USAGE_DEFER_GRACE_MIN, no_usage_reading=False)
    base.update(over)
    return types.SimpleNamespace(**base)


class TheCadence(unittest.TestCase):
    """§8.1 option A is the DEFAULT, and the number lives in exactly one place."""

    def test_the_default_is_the_specs_recommendation(self):
        self.assertEqual(pr.USAGE_INTERVAL_MIN_DEFAULT, 60)

    def test_the_hour_is_not_hard_coded_anywhere_else(self):
        """*"Do not hard-code an hour anywhere but one named constant / flag."* The constant is the
        default of the flag, and no call site may spell the number itself."""
        with open(os.path.join(SCRIPT_DIR, "presence.py"), encoding="utf-8") as fh:
            source = fh.read()
        block = source[source.index("def maybe_usage_reading"):
                       source.index("# ------", source.index("def maybe_usage_reading"))]
        self.assertNotIn("60,", block)
        self.assertIn("USAGE_INTERVAL_MIN_DEFAULT", block)

    def test_nothing_is_due_before_the_interval_elapses(self):
        stamp = {"at": NOW.isoformat()}
        self.assertIsNone(pr.usage_plan(stamp, NOW + timedelta(minutes=59), interval_min=60))

    def test_a_reading_is_due_once_the_interval_elapses(self):
        stamp = {"at": NOW.isoformat()}
        plan = pr.usage_plan(stamp, NOW + timedelta(minutes=61), interval_min=60)
        self.assertEqual(plan["reason"], "cadence")
        self.assertFalse(plan["overdue"])

    def test_a_shorter_cadence_is_honoured(self):
        stamp = {"at": NOW.isoformat()}
        self.assertIsNotNone(pr.usage_plan(stamp, NOW + timedelta(minutes=16), interval_min=15))
        self.assertIsNone(pr.usage_plan(stamp, NOW + timedelta(minutes=16), interval_min=30))

    def test_interval_zero_turns_the_cadence_off_and_leaves_the_boundary_pair(self):
        """`--usage-interval-min 0` is 'no clock-driven readings'. The reset boundary is a different
        question, and Q5 is unanswerable without it."""
        stamp = {"at": NOW.isoformat()}
        self.assertIsNone(pr.usage_plan(stamp, NOW + timedelta(hours=9), interval_min=0))
        plan = pr.usage_plan(stamp, RESET - timedelta(minutes=5), interval_min=0, week_reset=RESET)
        self.assertEqual(plan["reason"], "boundary_pre")

    def test_a_first_reading_is_due_immediately(self):
        plan = pr.usage_plan({}, NOW, interval_min=60)
        self.assertEqual(plan["reason"], "first_reading")


class TheBoundaryPair(unittest.TestCase):
    """§8.1 — without a PRE reading a week's final total is never observed; without a POST one a
    reset is indistinguishable from an instrument failure."""

    def _plan(self, now, done=None):
        stamp = {"at": (now - timedelta(minutes=90)).isoformat(), "boundaries": done or {}}
        return pr.usage_plan(stamp, now, interval_min=60, week_reset=RESET)

    def test_pre_fires_about_five_minutes_before_the_reset(self):
        plan = self._plan(RESET - timedelta(minutes=5))
        self.assertEqual(plan["reason"], "boundary_pre")
        self.assertFalse(plan["expired"])

    def test_post_fires_about_five_minutes_after(self):
        done = {f"{RESET.isoformat()}|pre": "x"}
        plan = self._plan(RESET + timedelta(minutes=5), done)
        self.assertEqual(plan["reason"], "boundary_post")
        self.assertFalse(plan["expired"])

    def test_each_side_fires_at_most_once_per_window(self):
        done = {f"{RESET.isoformat()}|pre": "x", f"{RESET.isoformat()}|post": "y"}
        plan = self._plan(RESET + timedelta(minutes=5), done)
        self.assertEqual(plan["reason"], "cadence")  # the boundary is spent; ordinary cadence only

    def test_a_missed_boundary_EXPIRES_and_is_never_taken_late(self):
        """A 'pre-reset' reading taken forty minutes after the reset is a mislabelled sample, and a
        mislabelled sample is worse than a missing one."""
        plan = self._plan(RESET - timedelta(minutes=5) + timedelta(minutes=40))
        self.assertTrue(plan["expired"])

    def test_the_boundary_outranks_the_cadence_when_both_are_due(self):
        plan = self._plan(RESET - timedelta(minutes=5))
        self.assertEqual(plan["kind"], "boundary")

    def test_without_a_known_reset_there_is_no_boundary(self):
        """The window id comes from the last successful reading. Before there is one, there is
        nothing to be five minutes early for."""
        stamp = {"at": (NOW - timedelta(minutes=90)).isoformat()}
        self.assertEqual(pr.usage_plan(stamp, NOW, interval_min=60)["kind"], "cadence")


class Base(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="presence-usage-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.logged = []

    def log(self, msg):
        self.logged.append(msg)

    def rows(self):
        path = up.readings_path(self.dir)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]


class TheDeferralGates(Base):
    """§2.6 — the same three `maybe_peek` rides, with one difference that matters."""

    def _run(self, *, warm_busy=False, children=None, runner=None, **over):
        # `account_reader` is injected for the same reason `runner` is: without it the SKIP path
        # would open the host's real ~/.claude.json, which this suite may not do.
        return pr.maybe_usage_reading(self.dir, args(**over), self.log, children, warm_busy,
                                      runner=runner or FakeRunner(stdout=envelope(SAMPLE)),
                                      account_reader=fake_reader())

    def test_a_warm_turn_defers_the_reading_without_writing_anything(self):
        self.assertFalse(self._run(warm_busy=True))
        self.assertEqual(self.rows(), [])

    def test_a_headless_child_in_flight_defers_it(self):
        proc = types.SimpleNamespace(poll=lambda: None, pid=1)
        self.assertFalse(self._run(children=[proc]))
        self.assertEqual(self.rows(), [])

    def test_a_gate_held_past_the_grace_writes_a_SKIPPED_row(self):
        """Invariant 1: every attempt writes exactly one row, including the ones that never spawn.
        An auth outage once killed a run of Watch peeks leaving only their launch lines."""
        past = pr.local_now() - timedelta(minutes=200)
        pr.save_json(os.path.join(self.dir, pr.USAGE_STAMP_FILE), {"at": past.isoformat()})
        self.assertTrue(self._run(warm_busy=True))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], up.SKIPPED)
        self.assertEqual(rows[0]["gate"], "warm_busy")
        up.assert_no_meter_fields(rows[0])

    def test_a_skipped_row_states_the_window_it_could_not_cover(self):
        past = (pr.local_now() - timedelta(minutes=200)).isoformat(timespec="seconds")
        up.append_row(self.dir, up.build_skipped_row(at=past, gate="warm_busy",
                                                     account=dict(FAKE_ACCOUNT)))
        pr.save_json(os.path.join(self.dir, pr.USAGE_STAMP_FILE), {"at": past})
        self._run(warm_busy=True)
        row = self.rows()[-1]
        self.assertEqual(row["interval"]["since"], past)
        self.assertTrue(row["interval"]["gap"])

    def test_a_skipped_row_still_carries_the_activity_snapshot(self):
        """WHAT was running when a reading could not be taken is usually the ANSWER — the gate that
        held is a warm turn or a headless child, both of which the snapshot names."""
        past = (pr.local_now() - timedelta(minutes=200)).isoformat(timespec="seconds")
        up.append_row(self.dir, up.build_skipped_row(at=past, gate="warm_busy",
                                                     account=dict(FAKE_ACCOUNT)))
        pr.save_json(os.path.join(self.dir, pr.USAGE_STAMP_FILE), {"at": past})
        self._run(warm_busy=True)
        self.assertIn("activity", self.rows()[-1])

    def test_a_skip_advances_the_stamp_so_the_next_window_starts_clean(self):
        past = pr.local_now() - timedelta(minutes=200)
        pr.save_json(os.path.join(self.dir, pr.USAGE_STAMP_FILE), {"at": past.isoformat()})
        self._run(warm_busy=True)
        self.assertFalse(self._run(warm_busy=True))  # not due again for another hour
        self.assertEqual(len(self.rows()), 1)

    def test_a_long_outage_reads_at_once_rather_than_recording_a_skip(self):
        """A cadence reading that comes due after the daemon was down fires immediately. The window
        it opens is simply longer than nominal, which the row states — and the meter NOW is
        meaningful now, which is the whole point of not queueing one."""
        stamp = {"at": (NOW - timedelta(hours=6)).isoformat()}
        plan = pr.usage_plan(stamp, NOW, interval_min=60)
        self.assertTrue(plan["overdue"])           # so a HELD gate would record a skip...
        self.assertEqual(plan["kind"], "cadence")  # ...but with no gate it simply reads


class ItNeverSpeaks(Base):
    """The scope fence: the owner asked for logging, and did not ask to be told anything."""

    def test_no_send_path_is_reachable_from_the_reading(self):
        """Checked against the CALLS the function makes, not against its text — the docstring says
        "no push, no nudge, no threshold" on purpose, and a text scan would fail on the sentence
        that states the rule."""
        import ast
        with open(os.path.join(SCRIPT_DIR, "presence.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        forbidden = {"deliver_reply", "send_telegram", "send_discord", "record_assertion",
                     "enqueue_control", "notify_text", "record_turn", "ask"}
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name not in (
                    "maybe_usage_reading", "usage_plan", "usage_gate", "_usage_stamp",
                    "_usage_interval"):
                continue
            called = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    called.add(f.attr if isinstance(f, ast.Attribute) else
                               getattr(f, "id", ""))
            self.assertEqual(called & forbidden, set(),
                             f"{fn.name} reached for {called & forbidden}")

    def test_the_log_line_names_the_outcome_and_never_a_percentage(self):
        """`presence.log` is quoted to the owner and read by other tools. A percentage in it is a usage
        level being spoken by a surface that was told never to speak one."""
        pr.maybe_usage_reading(self.dir, args(), self.log, None, False,
                               runner=FakeRunner(stdout=envelope(SAMPLE)))
        self.assertTrue(self.logged)
        for line in self.logged:
            self.assertNotIn("%", line)

    def test_no_threshold_constant_exists(self):
        """§5.2 refuses any threshold or projection. There is nothing to tune back on."""
        for name in dir(pr):
            if "usage" in name.lower():
                self.assertNotIn("threshold", name.lower())
                self.assertNotIn("alert", name.lower())


class ItFailsOpen(Base):

    def test_a_reading_that_explodes_costs_a_row_and_nothing_else(self):
        def boom(*_a, **_kw):
            raise RuntimeError("the CLI is gone")

        original, up.take_reading = up.take_reading, boom
        self.addCleanup(lambda: setattr(up, "take_reading", original))
        self.assertFalse(pr.maybe_usage_reading(self.dir, args(), self.log, None, False))
        self.assertTrue(any("usage reading failed" in l for l in self.logged))

    def test_a_disabled_reading_does_nothing_at_all(self):
        self.assertFalse(pr.maybe_usage_reading(self.dir, args(no_usage_reading=True), self.log))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.logged, [])

    def test_a_corrupt_stamp_file_does_not_wedge_the_series(self):
        with open(os.path.join(self.dir, pr.USAGE_STAMP_FILE), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        pr.maybe_usage_reading(self.dir, args(stub_brain=True), self.log, None, False)
        self.assertTrue(any("usage reading" in l for l in self.logged))

    def test_stub_brain_spawns_nothing(self):
        self.assertTrue(pr.maybe_usage_reading(self.dir, args(stub_brain=True), self.log,
                                               None, False))
        self.assertEqual(self.rows(), [])
        self.assertIn("stubbed", self.logged[-1])


class TheStamp(Base):

    def test_the_window_id_is_remembered_so_the_tick_never_tails_the_log(self):
        row = {"at": NOW.isoformat(), "outcome": "ok", "week_window_id": RESET.isoformat()}
        pr._usage_stamp(self.dir, None, row)
        stamp = pr.load_json(os.path.join(self.dir, pr.USAGE_STAMP_FILE), None)
        self.assertEqual(stamp["week_window_id"], RESET.isoformat())
        self.assertEqual(stamp["outcome"], "ok")

    def test_boundary_keys_are_remembered_and_bounded(self):
        for i in range(pr.USAGE_BOUNDARY_MEMORY + 4):
            plan = {"boundary_key": f"2026-0{i % 9 + 1}-01T00:00:00-05:00|pre"}
            pr._usage_stamp(self.dir, plan, {"at": NOW.isoformat(), "outcome": "ok"})
        stamp = pr.load_json(os.path.join(self.dir, pr.USAGE_STAMP_FILE), None)
        self.assertLessEqual(len(stamp["boundaries"]), pr.USAGE_BOUNDARY_MEMORY)

    def test_a_window_id_is_never_written_from_a_failed_reading(self):
        """Invariant 3 in the stamp: a failed reading knows no window, and inheriting the previous
        one would carry a fact forward across exactly the gap it must not paper over."""
        pr._usage_stamp(self.dir, None, {"at": NOW.isoformat(), "outcome": "ok",
                                         "week_window_id": RESET.isoformat()})
        pr._usage_stamp(self.dir, None, {"at": NOW.isoformat(), "outcome": up.AUTH_FAILED})
        stamp = pr.load_json(os.path.join(self.dir, pr.USAGE_STAMP_FILE), None)
        self.assertEqual(stamp["week_window_id"], RESET.isoformat())  # the OLD one, unchanged
        self.assertEqual(stamp["outcome"], up.AUTH_FAILED)


class EndToEndWithAFakeRunner(Base):
    """The whole daemon path with a fake `claude`: no spawn, no network, no spend."""

    def _read(self, runner):
        return pr.maybe_usage_reading(self.dir, args(), self.log, None, False, runner=runner)

    def test_a_healthy_reading_lands_a_full_row(self):
        self.assertTrue(self._read(FakeRunner(stdout=envelope(SAMPLE))))
        row = self.rows()[-1]
        self.assertEqual(row["outcome"], up.OK)
        self.assertEqual(row["meters"]["week_all_models"]["pct"], 74)
        self.assertEqual(row["cli_version"], "2.1.243")
        self.assertTrue(row["interval"]["first_reading"])
        self.assertIn("usage reading: ok", self.logged[-1])

    def test_a_second_reading_carries_the_interval_and_the_activity(self):
        self._read(FakeRunner(stdout=envelope(SAMPLE)))
        # Force the cadence forward rather than waiting an hour.
        stamp = pr.load_json(os.path.join(self.dir, pr.USAGE_STAMP_FILE), None)
        stamp["at"] = (pr.local_now() - timedelta(minutes=90)).isoformat()
        pr.save_json(os.path.join(self.dir, pr.USAGE_STAMP_FILE), stamp)
        up.append_row(self.dir, dict(self.rows()[-1], at=stamp["at"]))

        self._read(FakeRunner(stdout=envelope(SAMPLE)))
        row = self.rows()[-1]
        self.assertFalse(row["interval"]["first_reading"])
        self.assertEqual(set(row["activity"]), {"jobs", "warm", "governor", "peeks", "daemon"})
        self.assertIsNone(row["activity"]["peeks"]["cost_usd"])

    def test_an_auth_outage_lands_as_an_auth_failed_row_not_a_silence(self):
        """An auth outage — a stretch where no new claude session can authenticate at all — must not
        vanish from the series."""
        self._read(FakeRunner(returncode=1, stderr="API Error: authentication_error"))
        row = self.rows()[-1]
        self.assertEqual(row["outcome"], up.AUTH_FAILED)
        up.assert_no_meter_fields(row)


if __name__ == "__main__":
    unittest.main()
