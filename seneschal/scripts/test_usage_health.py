#!/usr/bin/env python3
"""Tests for §8.3 of seneschal/docs/usage-telemetry-spec.md — failure should speak, but nothing excessive.

The design lets instrument failure speak. This suite is the *"nothing excessive"* half, asserted
rather than promised. What each class guards:

* ``TheEpisodeWalk``            — the consecutive-failure run, derived from the row stream and
                                  nothing else. Includes the one departure from a literal "non-ok":
                                  a ``skipped`` row is TRANSPARENT, because three of those in a row
                                  is a long working session, not a broken instrument.
* ``ItFiresAtTheThreshold``     — at 3, never at 2. A one-off stays silent.
* ``OneMessagePerEpisode``      — after it fires it does not fire again for that unbroken run. No
                                  ladder, no re-nag, no escalation.
* ``RecoverySpeaksOnlyIfTheBreakDid`` — a break nobody was told about recovers in silence, which is
                                  what caps an outage at exactly two messages.
* ``ARestartCannotReAnnounce``  — the episode identity is derived, the announcement flag is
                                  persisted, and neither a reboot nor a re-derivation can resurrect
                                  a resolved episode.
* ``TheQuietWindow``            — a dead instrument at 03:00 is not a Critical reminder. It defers,
                                  it never pierces, and it goes out when the window opens.
* ``TheMessage``                — what is broken, since when, how many attempts. And nothing else.
* ``TheLevelStillNeverSpeaks``  — §8.2's conservative branch holds: the instrument may speak,
                                  the meter may not.
* ``TheDaemonWiring``           — presence.py's half: the sender is injected, the stamp is written
                                  only after a landed send, and `maybe_usage_reading` stays mute.
                                  SKIPPED until the daemon carries the usage hooks
                                  (`maybe_usage_reading` / `maybe_usage_notice`).

Nothing here spawns anything, sends anything, or writes outside its own temp dir.

Run:  python -m unittest test_usage_health
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, time, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import tz_common  # noqa: E402
import usage_health as uh  # noqa: E402
import usage_probe as up  # noqa: E402

import presence as pr  # noqa: E402

LOCAL = timezone(timedelta(hours=-5))  # a fixed owner offset for every instant below
NOW = datetime(2026, 8, 27, 17, 0, 0, tzinfo=LOCAL)
LOG = "state/plan-usage.jsonl"


def row(hours_ago, outcome, **extra):
    """One row of the series, `hours_ago` before NOW."""
    return dict({"schema": up.SCHEMA, "outcome": outcome,
                 "at": (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")}, **extra)


def series(*spec):
    """`series((5, "ok"), (3, "timeout"))` — oldest first, the order the file holds."""
    return [row(h, o) for h, o in spec]


def plan(rows, notice=None, now=NOW, **kw):
    kw.setdefault("log_path", LOG)
    return uh.plan_notice(rows, notice, now, **kw)


class TheEpisodeWalk(unittest.TestCase):
    """The count is DERIVED from the rows, every time. There is no counter to drift."""

    def test_a_healthy_series_has_no_episode(self):
        self.assertIsNone(uh.current_episode(series((3, "ok"), (2, "ok"), (1, "ok"))))

    def test_the_run_stops_at_the_last_success(self):
        ep = uh.current_episode(series((5, "timeout"), (4, "ok"), (3, "timeout"), (2, "timeout")))
        self.assertEqual(ep["attempts"], 2)
        self.assertEqual(ep["started_at"], row(3, "timeout")["at"])

    def test_partial_counts_as_a_failure(self):
        """A degraded parse that persists IS the wording change §3.4 wants surfaced, and the series
        it produces is not one anybody should keep trusting silently."""
        ep = uh.current_episode(series((3, "partial"), (2, "partial"), (1, "partial")))
        self.assertEqual(ep["attempts"], 3)

    def test_a_skipped_row_is_transparent_and_does_not_count(self):
        """The one departure from a literal 'non-ok', and the reason is a false alarm this would
        otherwise send most afternoons: a `skipped` row means the daemon DECLINED to read because a
        warm turn or a live session held the gate — the instrument is fine."""
        self.assertIsNone(uh.current_episode(series((3, "skipped"), (2, "skipped"),
                                                    (1, "skipped"))))

    def test_a_skipped_row_does_not_break_a_run_either(self):
        """Transparent cuts both ways: a skip does not prove the instrument works, so it may not
        silently reset a genuine break."""
        ep = uh.current_episode(series((4, "ok"), (3, "timeout"), (2, "skipped"), (1, "timeout")))
        self.assertEqual(ep["attempts"], 2)
        self.assertEqual(ep["started_at"], row(3, "timeout")["at"])

    def test_mixed_outcomes_are_all_counted_and_named(self):
        ep = uh.current_episode(series((4, "ok"), (3, "timeout"), (2, "timeout"), (1, "unparsed")))
        self.assertEqual(ep["outcomes"], {"timeout": 2, "unparsed": 1})
        self.assertEqual(ep["last_outcome"], "unparsed")

    def test_a_complete_file_that_never_succeeded_is_not_truncated(self):
        self.assertFalse(uh.current_episode(series((2, "timeout"), (1, "timeout")))["truncated"])

    def test_a_bounded_read_that_never_reaches_a_success_says_so(self):
        ep = uh.current_episode(series((2, "timeout"), (1, "timeout")), complete=False)
        self.assertTrue(ep["truncated"])

    def test_an_unknown_outcome_ends_the_walk_rather_than_counting(self):
        """An outcome this module does not classify is treated as a success (it ends the run), which
        is the direction that cannot invent an outage out of a schema change."""
        ep = uh.current_episode(series((3, "timeout"), (2, "some_new_thing"), (1, "timeout")))
        self.assertEqual(ep["attempts"], 1)


class ItFiresAtTheThreshold(unittest.TestCase):
    """Three consecutive failures — ~3 h of blindness before a single word is said."""

    def test_two_failures_say_nothing(self):
        self.assertIsNone(plan(series((4, "ok"), (2, "timeout"), (1, "timeout"))))

    def test_one_failure_says_nothing(self):
        """A missed reading is a missing row, not an incident. That principle survives the ruling."""
        self.assertIsNone(plan(series((4, "ok"), (1, "auth_failed"))))

    def test_three_failures_speak(self):
        d = plan(series((5, "ok"), (3, "timeout"), (2, "timeout"), (1, "timeout")))
        self.assertEqual(d["kind"], "break")
        self.assertFalse(d["deferred"])

    def test_a_tick_that_lands_past_the_threshold_still_speaks(self):
        """The threshold is a floor, not an equality — a restart or a cleared deferral can land on
        attempt 4, and firing only at exactly 3 would mean it never speaks at all."""
        d = plan(series((6, "ok"), (4, "timeout"), (3, "timeout"), (2, "timeout"), (1, "timeout")))
        self.assertEqual(d["kind"], "break")

    def test_an_empty_series_says_nothing(self):
        self.assertIsNone(plan([]))

    def test_the_threshold_is_configurable_in_one_place(self):
        self.assertEqual(uh.FAILURE_STREAK, 3)
        self.assertIsNone(plan(series((3, "timeout"), (2, "timeout"), (1, "timeout")), streak=4))


class OneMessagePerEpisode(unittest.TestCase):
    """*"Nothing crazy"* is this class. After it fires, it is done."""

    def _announced(self):
        rows = series((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        return rows, plan(rows)["notice"]

    def test_it_does_not_fire_again_on_the_next_failure(self):
        rows, notice = self._announced()
        self.assertIsNone(plan(rows + [row(2, "timeout")], notice))

    def test_it_does_not_fire_again_ten_failures_later(self):
        """No ladder, no escalation, no 'still broken' at hour six."""
        rows, notice = self._announced()
        self.assertIsNone(plan(rows + [row(2 - i * 0.1, "timeout") for i in range(10)], notice))

    def test_it_does_not_fire_again_when_the_outcome_changes_mid_episode(self):
        """`timeout` becoming `auth_failed` is the same unbroken run of blindness, not news."""
        rows, notice = self._announced()
        self.assertIsNone(plan(rows + [row(2, "auth_failed")], notice))

    def test_a_NEW_episode_after_a_recovery_speaks_again(self):
        """One message per episode, not one message ever. A second outage is a second fact."""
        rows, _ = self._announced()
        rows = rows + series((2.5, "ok"), (2, "timeout"), (1.5, "timeout"), (1, "timeout"))
        # The recovery cleared the notice (see RecoverySpeaksOnlyIfTheBreakDid), so this is fresh.
        self.assertEqual(plan(rows, None)["kind"], "break")


class RecoverySpeaksOnlyIfTheBreakDid(unittest.TestCase):

    def test_an_announced_break_gets_exactly_one_recovery(self):
        rows = series((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        notice = plan(rows)["notice"]
        rows = rows + [row(2, "ok")]
        d = plan(rows, notice)
        self.assertEqual(d["kind"], "recovery")
        self.assertIsNone(d["notice"])          # the block is cleared...
        self.assertIsNone(plan(rows, None))     # ...so there is no second one

    def test_an_UNannounced_break_recovers_in_silence(self):
        """A blip nobody was told about does not get a 'good news' message. That is what caps an
        outage at exactly two messages, ever."""
        rows = series((4, "ok"), (3, "timeout"), (2, "timeout"), (1, "ok"))
        self.assertIsNone(plan(rows, None))

    def test_a_partial_does_not_count_as_a_recovery(self):
        """`partial` is inside the failure set, so it cannot close an episode it belongs to."""
        rows = series((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        notice = plan(rows)["notice"]
        self.assertIsNone(plan(rows + [row(2, "partial")], notice))


class ARestartCannotReAnnounce(unittest.TestCase):
    """The count is derived; only *'the owner was already told'* is persisted, keyed by the derived episode."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="usage-health-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _write(self, rows):
        for r in rows:
            up.append_row(self.dir, r)

    def test_the_same_answer_comes_back_from_a_cold_read_of_the_rows(self):
        rows = series((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        self._write(rows)
        d = uh.decide(self.dir, None, NOW, quiet=lambda *_a: False)
        self.assertEqual(d["kind"], "break")
        # Persist it the way the daemon does, then "restart" — a fresh derivation, same notice.
        self.assertIsNone(uh.decide(self.dir, d["notice"], NOW, quiet=lambda *_a: False))

    def test_a_resolved_episode_is_not_resurrected_by_a_restart(self):
        rows = series((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"), (2, "ok"))
        self._write(rows)
        # The notice was cleared when the recovery went out, so a cold start finds nothing to say.
        self.assertIsNone(uh.decide(self.dir, None, NOW, quiet=lambda *_a: False))

    def test_the_episode_key_is_the_runs_own_start_not_a_counter(self):
        rows = series((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        notice = plan(rows)["notice"]
        self.assertEqual(notice["episode_started_at"], row(5, "timeout")["at"])

    def test_a_truncated_read_holds_the_episode_open_rather_than_re_announcing(self):
        """A run longer than the bounded tail would otherwise appear to 'start' later each time the
        window slid, and re-announce forever."""
        rows = series((3, "timeout"), (2, "timeout"), (1, "timeout"))
        notice = {"episode_started_at": "2026-08-20T04:00:00-05:00"}
        self.assertIsNone(plan(rows, notice, complete=False))

    def test_a_torn_last_line_costs_that_line_and_not_the_check(self):
        self._write(series((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout")))
        with open(up.readings_path(self.dir), "a", encoding="utf-8") as fh:
            fh.write('{"at": "2026-08-2')
        self.assertEqual(uh.decide(self.dir, None, NOW, quiet=lambda *_a: False)["kind"], "break")

    def test_no_readings_file_at_all_says_nothing(self):
        self.assertIsNone(uh.decide(self.dir, None, NOW, quiet=lambda *_a: False))


class TheQuietWindow(unittest.TestCase):
    """A dead instrument at 03:00 is not a Critical reminder."""

    def _rows(self):
        return series((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))

    def test_a_notice_due_inside_quiet_hours_is_DEFERRED_not_dropped(self):
        d = plan(self._rows(), quiet=True)
        self.assertEqual(d["kind"], "break")
        self.assertTrue(d["deferred"])

    def test_the_deferred_notice_goes_out_once_the_window_opens(self):
        """Nothing is persisted while deferred, so the next reading simply asks again — which is how
        it waits for the window rather than being lost in it."""
        rows = self._rows()
        self.assertTrue(plan(rows, quiet=True)["deferred"])
        self.assertFalse(plan(rows + [row(2, "timeout")], quiet=False)["deferred"])

    def test_a_recovery_is_held_by_the_window_too(self):
        rows = self._rows()
        notice = plan(rows)["notice"]
        d = plan(rows + [row(2, "ok")], notice, quiet=True)
        self.assertEqual(d["kind"], "recovery")
        self.assertTrue(d["deferred"])

    def setUp(self):
        # Pin the owner's zone and the curfew so neither the runner's clock nor a real identity.json
        # can move the answer. The curfew is the shipped default (01:00-07:00).
        import sentinel
        for patcher in (mock.patch.object(tz_common, "_zone", return_value=LOCAL),
                        mock.patch.object(sentinel, "curfew_window",
                                          return_value=(time(1, 0), time(7, 0)))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_night_curfew_is_sentinels_and_is_never_a_second_copy(self):
        """*"Is 3 a.m. a reasonable hour"* is a question this tree has already answered — the
        owner's `nightCurfew`. A second pair of numbers here would drift from the first."""
        src = read("usage_health.py")
        self.assertIn("sentinel.curfew_window()", src)
        self.assertNotIn("01:00", src)

    def test_three_in_the_morning_is_quiet_and_two_in_the_afternoon_is_not(self):
        d = tempfile.mkdtemp(prefix="usage-health-quiet-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.assertTrue(uh.in_quiet(d, datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)))   # 03:00 local
        self.assertFalse(uh.in_quiet(d, datetime(2026, 8, 27, 19, 0, tzinfo=timezone.utc)))  # 14:00 local

    def test_a_curfew_that_wraps_midnight_holds_on_both_sides_of_it(self):
        import sentinel
        d = tempfile.mkdtemp(prefix="usage-health-wrap-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with mock.patch.object(sentinel, "curfew_window", return_value=(time(23, 0), time(6, 0))):
            self.assertTrue(uh.in_quiet(d, datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc)))  # 23:30
            self.assertTrue(uh.in_quiet(d, datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)))   # 04:00
            self.assertFalse(uh.in_quiet(d, datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)))  # 07:00
        with mock.patch.object(sentinel, "curfew_window", return_value=(time(2, 0), time(2, 0))):
            self.assertFalse(uh.in_quiet(d, datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)))  # disabled

    def test_an_explicit_do_not_disturb_window_also_holds_it(self):
        import sentinel
        d = tempfile.mkdtemp(prefix="usage-health-dnd-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        noon = datetime(2026, 8, 27, 17, 0, tzinfo=timezone.utc)  # 12:00 local, well outside the curfew
        self.assertFalse(uh.in_quiet(d, noon))
        sentinel.set_quiet(d, noon + timedelta(hours=2), reason="test")
        self.assertTrue(uh.in_quiet(d, noon))

    def test_it_never_pierces(self):
        """`pierce` / `tone="critical"` is the queue's Critical exemption, and nothing here may reach
        for it — that is the difference between an instrument notice and a missed dose.

        Checked against the CALL's keywords, not the text: the docstring says *"it never pierces"* on
        purpose, and a substring scan would fail on the sentence that states the rule."""
        for node in ast.walk(pr_function("maybe_usage_notice")):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                self.assertNotEqual(kw.arg, "pierce")
                if kw.arg == "tone":
                    self.assertNotEqual(getattr(kw.value, "value", None), "critical")


class TheMessage(unittest.TestCase):
    """What is broken, since when, how many attempts. And nothing else."""

    def _break(self, *spec, **kw):
        return plan(series(*spec), **kw)["text"]

    def test_it_names_the_outcome_the_start_the_count_and_the_log(self):
        text = self._break((6, "ok"), (5, "auth_failed"), (4, "auth_failed"), (3, "auth_failed"))
        self.assertIn("auth_failed", text)
        self.assertIn("12:00 PM", text)   # NOW is 17:00 local; the run started five hours earlier
        self.assertIn("3 attempts", text)
        self.assertIn(LOG, text)

    def test_mixed_outcomes_are_itemised_rather_than_averaged(self):
        text = self._break((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "unparsed"))
        self.assertIn("timeout ×2", text)
        self.assertIn("unparsed", text)

    def test_a_truncated_run_says_AT_LEAST_rather_than_claiming_a_measurement(self):
        text = plan(series((3, "timeout"), (2, "timeout"), (1, "timeout")),
                    complete=False)["text"]
        self.assertIn("at least", text)

    def test_the_recovery_gives_both_ends_of_the_gap(self):
        rows = series((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        text = plan(rows + [row(2, "ok")], plan(rows)["notice"])["text"]
        self.assertIn("12:00 PM", text)   # the gap's start
        self.assertIn("5:00 PM", text)    # now

    def test_it_gives_no_diagnosis_and_no_instruction(self):
        """It reports an instrument state. It does not say what to do about it, and it does not
        guess why — nothing here knows why."""
        text = self._break((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        for banned in ("you may want", "you might", "try ", "check ", "please ", "should ",
                       "because", "probably", "likely", "run `"):
            self.assertNotIn(banned, text.lower())

    def test_the_stamp_is_an_as_of_time_she_can_check_against_her_own_day(self):
        self.assertEqual(uh._clock(NOW), "Thu 5:00 PM")
        self.assertEqual(uh._clock(NOW.replace(hour=9, minute=5)), "Thu 9:05 AM")

    def test_an_unreadable_stamp_degrades_to_its_raw_text_rather_than_to_a_guess(self):
        self.assertEqual(uh._clock("not a date"), "not a date")


class TheLevelStillNeverSpeaks(unittest.TestCase):
    """§8.2's conservative branch: the instrument may speak, the meter may not, and one may not
    leak into the other."""

    def test_the_module_never_names_the_meter_block_at_all(self):
        src = read("usage_health.py")
        self.assertNotIn('"meters"', src)
        self.assertNotIn("'meters'", src)
        self.assertNotIn("week_all_models", src)
        self.assertNotIn("resets_at", src)

    def test_no_percentage_reaches_the_message_even_when_the_rows_carry_one(self):
        """A `partial` row DOES carry meter numbers. The message must still contain none of them."""
        rows = [row(6, "ok"),
                row(5, "partial", meters={"week_all_models": {"pct": 74}}),
                row(4, "partial", meters={"week_all_models": {"pct": 81}}),
                row(3, "partial", meters={"week_all_models": {"pct": 88}})]
        text = plan(rows)["text"]
        self.assertNotIn("%", text)
        for n in ("74", "81", "88"):
            self.assertNotIn(n, text)

    def test_no_threshold_or_projection_constant_exists(self):
        """§5.2 refuses any threshold on a usage percentage and any rate projection. There is
        nothing here to tune back on."""
        for name in dir(uh):
            lowered = name.lower()
            self.assertNotIn("pct", lowered)
            self.assertNotIn("projection", lowered)
            self.assertNotIn("burn", lowered)

    def test_the_reading_itself_is_still_mute(self):
        """The instrument writes rows and says nothing, on every path — `maybe_usage_reading` must
        stay that way, which is why the announcement is a SEPARATE function."""
        block = pr_source_block("maybe_usage_reading")
        for banned in ("send_telegram", "mouth.enqueue", "usage_health"):
            self.assertNotIn(banned, block)


def read(name: str) -> str:
    with open(os.path.join(SCRIPT_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def pr_function(fn_name: str) -> ast.FunctionDef:
    """One presence.py function as an AST node — so a rule can be checked against what the code
    CALLS rather than against prose that may state the very rule being tested."""
    for node in ast.walk(ast.parse(read("presence.py"))):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            return node
    raise AssertionError(f"{fn_name} not found in presence.py")


def pr_source_block(fn_name: str) -> str:
    """The source text of one presence.py function, docstring stripped — the docstrings here quote
    the rules, so a text scan over them tests the prose and not the code."""
    lines = read("presence.py").splitlines(keepends=True)
    node = pr_function(fn_name)
    body = node.body[1:] if ast.get_docstring(node) else node.body
    return "".join(lines[body[0].lineno - 1:node.end_lineno]) if body else ""


def args(**over):
    base = dict(state_dir=None, telegram_env="telegram.env", stub_send=False,
                no_usage_reading=False, no_usage_notice=False)
    base.update(over)
    return types.SimpleNamespace(**base)


class TheDaemonWiring(unittest.TestCase):
    """presence.py's half. Nothing in this class sends anything: the sender is injected the same way
    `usage_probe`'s runner is."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="presence-usage-health-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.logged = []
        self.sent = []

    def log(self, msg):
        self.logged.append(msg)

    def send(self, text):
        self.sent.append(text)
        return True

    def _rows(self, *spec):
        for r in series(*spec):
            up.append_row(self.dir, r)

    def _stamp(self):
        return pr.load_json(os.path.join(self.dir, pr.USAGE_STAMP_FILE), None) or {}

    def _run(self, send=..., quiet=None, **over):
        return pr.maybe_usage_notice(self.dir, args(**over), self.log,
                                     now=NOW, send=self.send if send is ... else send,
                                     quiet=quiet or (lambda *_a: False))

    def test_a_break_reaches_the_injected_sender_and_stamps_the_notice(self):
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        self.assertTrue(self._run())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("failing since", self.sent[0])
        self.assertEqual(self._stamp()[uh.NOTICE_KEY]["episode_started_at"],
                         row(5, "timeout")["at"])

    def test_a_second_tick_in_the_same_episode_sends_nothing(self):
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        self._run()
        up.append_row(self.dir, row(2, "timeout"))
        self.assertFalse(self._run())
        self.assertEqual(len(self.sent), 1)

    def test_recovery_sends_once_and_clears_the_stamp(self):
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        self._run()
        up.append_row(self.dir, row(2, "ok"))
        self.assertTrue(self._run())
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn(uh.NOTICE_KEY, self._stamp())
        self.assertFalse(self._run())

    def test_the_quiet_window_sends_nothing_and_stamps_nothing(self):
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        self.assertFalse(self._run(quiet=lambda *_a: True))
        self.assertEqual(self.sent, [])
        self.assertNotIn(uh.NOTICE_KEY, self._stamp())
        self.assertTrue(any("quiet window" in l for l in self.logged))
        # ...and it goes out unchanged once the window opens.
        self.assertTrue(self._run())
        self.assertEqual(len(self.sent), 1)

    def test_a_failed_send_does_NOT_stamp_so_the_next_reading_retries(self):
        """Stamping on a message that never landed would retire the announcement on the strength of
        something the owner never received."""
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        self.assertFalse(self._run(send=lambda _t: False))
        self.assertNotIn(uh.NOTICE_KEY, self._stamp())
        self.assertTrue(self._run())
        self.assertEqual(len(self.sent), 1)

    def test_a_raising_sender_costs_a_log_line_and_nothing_else(self):
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        def boom(_t):
            raise RuntimeError("telegram is gone")
        self.assertFalse(self._run(send=boom))
        self.assertNotIn(uh.NOTICE_KEY, self._stamp())
        self.assertTrue(any("could not be queued" in l for l in self.logged))

    def test_the_off_switch_works(self):
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        self.assertFalse(self._run(no_usage_notice=True))
        self.assertFalse(self._run(no_usage_reading=True))
        self.assertEqual(self.sent, [])

    def test_stub_send_never_reaches_the_default_surface(self):
        """With no injected sender, `--stub-send` (and an absent telegram.env) is the same wall
        every other producer honours: the rows are still written, nothing goes out."""
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        self.assertFalse(pr.maybe_usage_notice(self.dir, args(stub_send=True), self.log, now=NOW))
        self.assertFalse(pr.maybe_usage_notice(self.dir, args(telegram_env=None), self.log,
                                               now=NOW))

    def test_a_corrupt_stamp_file_does_not_wedge_the_notice(self):
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        with open(os.path.join(self.dir, pr.USAGE_STAMP_FILE), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertTrue(self._run())

    def test_the_stamp_keeps_the_cadence_fields_it_already_held(self):
        """It is ONE persisted flag inside the EXISTING file — it may not clobber the scheduling
        bookkeeping that file exists for."""
        pr.save_json(os.path.join(self.dir, pr.USAGE_STAMP_FILE),
                     {"at": row(3, "timeout")["at"], "outcome": "timeout",
                      "week_window_id": "2026-08-31T16:59:00-05:00"})
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        self._run()
        stamp = self._stamp()
        self.assertEqual(stamp["week_window_id"], "2026-08-31T16:59:00-05:00")
        self.assertIn(uh.NOTICE_KEY, stamp)

    def test_the_log_line_names_the_kind_and_never_a_percentage(self):
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        self._run()
        self.assertTrue(any("usage break notice sent" in l for l in self.logged))
        for line in self.logged:
            self.assertNotIn("%", line)

    def test_nothing_is_written_outside_the_temp_dir(self):
        """The whole suite's contract, asserted once: `state_dir` is required and there is no
        default anywhere on this path."""
        self._rows((6, "ok"), (5, "timeout"), (4, "timeout"), (3, "timeout"))
        self._run()
        written = sorted(os.listdir(self.dir))
        self.assertEqual(written, sorted([up.READINGS_FILENAME, pr.USAGE_STAMP_FILE]))
        json.loads(json.dumps(self._stamp()))  # and the stamp stays JSON-round-trippable


if __name__ == "__main__":
    unittest.main()
