#!/usr/bin/env python3
"""Tests for the slot scheduler's per-slot catch-up policy and its never-silent skips.

The failure: a machine down until mid-morning made the daemon log `slot 'daily-journal' skipped
(past 05:00 + 180m catch-up)`, stamp it done in `slots.json`, and neither retry it nor tell the owner —
the journal page was never rolled over until the owner asked hours later. Three things are pinned here:

  1. `daily-journal` is `CATCHUP_UNBOUNDED` — due all day once 05:00 has passed; the Brief, Wrap and
     Dream keep the `--slot-catchup-min` window.
  2. A skip — past the window, retries exhausted, or a whole day missed across midnight — queues ONE
     plain Telegram line through the Mouth, at most once per (slot, date).
  3. `slot-status.json` says `ran` vs `skipped`; `slots.json` keeps its bare-date format.

Stdlib ``unittest`` only. Run:  python -m unittest seneschal.scripts.test_slot_catchup_policy
"""
import argparse
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import mouth  # noqa: E402
import presence as pr  # noqa: E402
import tmproot  # noqa: E402


class _RcProc:
    """Popen stand-in: poll() is None while running, else the chosen return code."""

    def __init__(self, rc=None):
        self._rc = rc

    def poll(self):
        return self._rc


def _slot(name):
    return next(s for s in pr.SLOTS if s["name"] == name)


def _args():
    return argparse.Namespace(no_slots=False, stub_brain=False, fake_inbox=None,
                              slot_catchup_min=180, claude_bin="claude", notion_mcp=None,
                              slack_mcp=None, permission_mode="bypassPermissions",
                              slot_model=None, model=None)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(tmproot.rmtree, self.dir)
        self.logged = []

    def fired(self):
        return pr.load_json(os.path.join(self.dir, "slots.json"), {})

    def status(self):
        return pr.load_json(os.path.join(self.dir, pr.SLOT_STATUS_FILE), {})

    def nudges(self):
        return [i for i in mouth.read_outbound(self.dir) if i.get("kind") == "nudge"]

    def tick(self, when, slots=None, slot_children=None, popen_rc=None):
        """One `maybe_run_slots` pass at local `when`, with `claude -p` stubbed out."""
        slot_children = {} if slot_children is None else slot_children
        with mock.patch.object(pr, "datetime") as mock_dt, \
             mock.patch.object(pr, "SLOTS", slots if slots is not None else pr.SLOTS), \
             mock.patch.object(pr.subprocess, "Popen", side_effect=lambda *a, **k: _RcProc(popen_rc)) as popen:
            mock_dt.now.return_value = when
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            pr.maybe_run_slots(self.dir, _args(), self.logged.append, [], warm_busy=False,
                               slot_children=slot_children, slot_hold_until={})
        return popen, slot_children


class PolicyIsDeclaredPerSlot(unittest.TestCase):
    def test_the_daily_journal_has_no_catchup_cliff(self):
        self.assertEqual(pr.slot_catchup(_slot("daily-journal")), pr.CATCHUP_UNBOUNDED)

    def test_brief_wrap_and_dream_keep_their_window(self):
        for name in ("morning-brief", "eod-wrap", "dream"):
            self.assertEqual(pr.slot_catchup(_slot(name)), pr.CATCHUP_WINDOW, name)

    def test_every_slot_has_a_label_for_the_notice(self):
        for s in pr.SLOTS:
            self.assertIn(s["name"], pr.SLOT_LABELS)
        self.assertIn(pr.SEED_SLOT_NAME, pr.SLOT_LABELS)


class ClassifySlots(unittest.TestCase):
    """A late-morning start after a machine outage, replayed through the pure classifier."""

    def test_at_1021_the_journal_runs_and_the_brief_is_too_late(self):
        slots = [_slot("daily-journal"), _slot("morning-brief")]
        to_run, too_late = pr.classify_slots(slots, datetime(2026, 9, 29, 10, 21), {}, 180)
        self.assertEqual([s["name"] for s in to_run], ["daily-journal"])
        self.assertEqual([s["name"] for s in too_late], ["morning-brief"])

    def test_the_journal_is_still_due_one_minute_before_midnight(self):
        to_run, too_late = pr.classify_slots([_slot("daily-journal")], datetime(2026, 9, 29, 23, 59),
                                             {"daily-journal": "2026-09-28"}, 180)
        self.assertEqual([s["name"] for s in to_run], ["daily-journal"])
        self.assertEqual(too_late, [])

    def test_the_journal_is_not_due_before_0500_or_twice(self):
        j = [_slot("daily-journal")]
        self.assertEqual(pr.classify_slots(j, datetime(2026, 9, 29, 4, 59), {}, 180), ([], []))
        self.assertEqual(pr.classify_slots(j, datetime(2026, 9, 29, 12, 0),
                                           {"daily-journal": "2026-09-29"}, 180), ([], []))

    def test_a_window_slot_inside_its_window_still_runs(self):
        to_run, _ = pr.classify_slots([_slot("morning-brief")], datetime(2026, 9, 29, 9, 30), {}, 180)
        self.assertEqual([s["name"] for s in to_run], ["morning-brief"])


class SkipIsNeverSilent(Base):
    def test_a_late_morning_runs_the_journal_and_tells_the_owner_about_the_brief(self):
        popen, children = self.tick(datetime(2026, 9, 29, 10, 21))
        self.assertEqual(popen.call_count, 1)                         # one launch per loop
        self.assertIn("daily-journal", children)                      # the journal, however late
        self.assertEqual(children["daily-journal"].slot_date, "2026-09-29")
        self.assertNotIn("daily-journal", self.fired())               # launched ≠ done
        self.assertEqual(self.fired().get("morning-brief"), "2026-09-29")  # old format, untouched
        self.assertEqual(self.status()["morning-brief"]["status"], "skipped")
        nudges = self.nudges()
        self.assertEqual(len(nudges), 1)
        text = nudges[0]["text"]
        self.assertIn("the morning Brief", text)
        self.assertIn("late", text)
        self.assertNotIn("Daily Journal", text)
        self.assertEqual(nudges[0]["surface"], "telegram")

    def test_the_notice_is_sent_once_per_slot_and_date(self):
        brief = [_slot("morning-brief")]
        self.tick(datetime(2026, 9, 29, 10, 21), slots=brief)
        self.tick(datetime(2026, 9, 29, 10, 22), slots=brief)
        self.tick(datetime(2026, 9, 29, 15, 0), slots=brief)
        self.assertEqual(len(self.nudges()), 1)
        self.assertTrue(self.status()["morning-brief"]["notified"])

    def test_a_failed_enqueue_is_not_marked_told_and_retries(self):
        brief = [_slot("morning-brief")]
        with mock.patch.object(mouth, "enqueue", return_value=None):
            self.tick(datetime(2026, 9, 29, 10, 21), slots=brief)
        self.assertFalse(self.status()["morning-brief"]["notified"])
        self.assertTrue(any("could not be queued" in line for line in self.logged))
        self.tick(datetime(2026, 9, 29, 10, 22), slots=brief)       # the enqueue works again
        self.assertEqual(len(self.nudges()), 1)
        self.assertTrue(self.status()["morning-brief"]["notified"])

    def test_a_running_slot_past_its_window_is_late_not_skipped(self):
        # Launched at 09:25 (inside 06:30 + 180m), still running at 09:31: this used to be logged
        # "skipped" mid-run. It must now be neither stamped, recorded, nor announced.
        children = {"morning-brief": _RcProc(None)}
        self.tick(datetime(2026, 9, 29, 9, 31), slots=[_slot("morning-brief")], slot_children=children)
        self.assertNotIn("morning-brief", self.fired())
        self.assertEqual(self.status(), {})
        self.assertEqual(self.nudges(), [])


class MissedAcrossMidnight(Base):
    """Dream's window (22:00 + 180m) runs past midnight, so its real cliff is the date rollover — and a
    Dream unrun at midnight used to vanish without even a log line."""

    def test_pure_detection(self):
        now = datetime(2026, 9, 30, 8, 0)
        dream = [_slot("dream")]
        self.assertEqual([(s["name"], last) for s, last in
                          pr.missed_yesterday(dream, now, {"dream": "2026-09-28"}, {})],
                         [("dream", "2026-09-28")])
        self.assertEqual(pr.missed_yesterday(dream, now, {"dream": "2026-09-29"}, {}), [])  # ran
        self.assertEqual(pr.missed_yesterday(dream, now, {}, {}), [])  # fresh install: nothing missed
        told = {"dream": {"date": "2026-09-29", "status": "skipped", "notified": True}}
        self.assertEqual(pr.missed_yesterday(dream, now, {"dream": "2026-09-28"}, told), [])

    def test_a_missed_dream_is_recorded_and_told_once(self):
        pr.save_json(os.path.join(self.dir, "slots.json"), {"dream": "2026-09-28"})
        self.tick(datetime(2026, 9, 30, 0, 30), slots=[_slot("dream")])
        self.tick(datetime(2026, 9, 30, 0, 31), slots=[_slot("dream")])
        rec = self.status()["dream"]
        self.assertEqual((rec["date"], rec["status"]), ("2026-09-29", "skipped"))
        nudges = self.nudges()
        self.assertEqual(len(nudges), 1)
        self.assertIn("Dream did not run yesterday", nudges[0]["text"])

    def test_a_run_still_going_at_midnight_is_not_missed(self):
        pr.save_json(os.path.join(self.dir, "slots.json"), {"daily-journal": "2026-09-28"})
        children = {"daily-journal": _RcProc(None)}
        self.tick(datetime(2026, 9, 30, 0, 5), slots=[_slot("daily-journal")], slot_children=children)
        self.assertEqual(self.status(), {})
        self.assertEqual(self.nudges(), [])


class ReapRecordsTheOutcome(Base):
    def test_a_clean_exit_records_ran(self):
        pr.reap_finished_slots({"daily-journal": _RcProc(0)}, self.dir, self.logged.append, {},
                               now_local=datetime(2026, 9, 29, 10, 40))
        self.assertEqual(self.fired()["daily-journal"], "2026-09-29")
        self.assertEqual(pr.slot_outcome(self.dir, "daily-journal", "2026-09-29"), "ran")

    def test_a_run_finishing_after_midnight_stamps_its_launch_date(self):
        # Journal launched 23:50 on the 29th, exits 00:10 on the 30th: it is the 29th's run, and
        # stamping the 30th would silently suppress the 30th's own 05:00 run.
        proc = _RcProc(0)
        proc.slot_date = "2026-09-29"
        pr.reap_finished_slots({"daily-journal": proc}, self.dir, self.logged.append, {},
                               now_local=datetime(2026, 9, 30, 0, 10))
        self.assertEqual(self.fired()["daily-journal"], "2026-09-29")
        to_run, _ = pr.classify_slots([_slot("daily-journal")], datetime(2026, 9, 30, 5, 0),
                                      self.fired(), 180)
        self.assertEqual([s["name"] for s in to_run], ["daily-journal"])

    def test_a_give_up_is_a_skip_and_is_told(self):
        retries = {"daily-journal": pr.SLOT_MAX_RETRIES - 1}
        pr.reap_finished_slots({"daily-journal": _RcProc(1)}, self.dir, self.logged.append, retries,
                               now_local=datetime(2026, 9, 29, 7, 0))
        self.assertEqual(pr.slot_outcome(self.dir, "daily-journal", "2026-09-29"), "skipped")
        self.tick(datetime(2026, 9, 29, 7, 1), slots=[_slot("daily-journal")])  # nothing launches
        nudges = self.nudges()
        self.assertEqual(len(nudges), 1)
        self.assertIn("the Daily Journal did not run today", nudges[0]["text"])
        self.assertIn(f"failed {pr.SLOT_MAX_RETRIES} times", nudges[0]["text"])


class SlotOutcomeReader(Base):
    def test_old_format_stamp_reads_as_stamped_not_ran(self):
        pr.save_json(os.path.join(self.dir, "slots.json"), {"morning-brief": "2026-09-28"})
        self.assertEqual(pr.slot_outcome(self.dir, "morning-brief", "2026-09-28"), "stamped")
        self.assertIsNone(pr.slot_outcome(self.dir, "morning-brief", "2026-09-29"))

    def test_skipped_and_ran_are_distinguishable(self):
        self.tick(datetime(2026, 9, 29, 10, 21), slots=[_slot("morning-brief")])
        self.assertEqual(pr.slot_outcome(self.dir, "morning-brief", "2026-09-29"), "skipped")
        pr.reap_finished_slots({"eod-wrap": _RcProc(0)}, self.dir, self.logged.append, {},
                               now_local=datetime(2026, 9, 29, 21, 20))
        self.assertEqual(pr.slot_outcome(self.dir, "eod-wrap", "2026-09-29"), "ran")


class NoticeText(unittest.TestCase):
    def test_several_skips_share_one_line(self):
        pending = [("morning-brief", {"date": "2026-09-29", "reason": "past its window"}),
                   ("dream", {"date": "2026-09-28", "reason": "no run recorded"})]
        text = pr.slot_skip_notice_text(pending, "2026-09-29")
        self.assertNotIn("\n", text)
        self.assertIn("the morning Brief (today — past its window)", text)
        self.assertIn("Dream (yesterday — no run recorded)", text)
        self.assertIn("late", text)

    def test_an_unknown_slot_name_falls_back_to_the_raw_name(self):
        text = pr.slot_skip_notice_text([("mystery", {"date": "2026-09-29"})], "2026-09-29")
        self.assertIn("mystery did not run today", text)


if __name__ == "__main__":
    unittest.main()
