#!/usr/bin/env python3
"""The Brief's unassigned-work count and its `note-brief-sent` reset are CODE in `presence.py`, not a
sentence in `modes/brief.md` the run has to remember.

Surfacing the number of unknowns in the daily Brief, and resetting the first-real-message ask window
(`brief_at`) once the Brief goes out, could both be wired as prose — *run `owi_unknowns.py count`,
print it, and after delivering run `note-brief-sent`*. A written step is sometimes not taken, and a
Brief that skipped the reset would leave the PRIOR day's ask window open, so the first-real-message
ask could fire on a message that was not the owner's first one that day.

What these guard:

* `owi_unknowns.brief_line` renders the count from `loops.py`'s store — `None` at 0 (the section is
  omitted), singular/plural otherwise — and the CLI verb `brief-line` prints the same.
* `presence.maybe_run_slots` appends that rendered line to the `morning-brief` prompt at launch, with
  the instruction to print it verbatim, and tells the run explicitly when the count is 0. The number
  in the prompt is the store's, never the model's. A broken helper costs the Brief nothing but the
  line (`slot_context` never raises). *(Skipped until the presence wiring lands — wave 26.)*
* `presence.reap_finished_slots` calls `owi_unknowns.note_brief_sent` on the `morning-brief` slot's
  CLEAN exit only — a failed run does not reset the window — and a hook that raises never blocks the
  stamp. *(Same skip.)*
* The three prose files no longer instruct the run to call `note-brief-sent` (a second reset from the
  run would be harmless but is exactly the duplicated ownership this closes).
* A `whose_move: both` row is raised as a JOINT ask ("do you have time for X, or should I move on?"),
  never a solo nag — `loops.ask_line`, surfaced through `loops.py mine`'s text output and
  `owi_resurface.report()`'s `missed_asks` (report-only; the `owi-resurface.jsonl` row shape is
  unchanged).

Run:  python -m unittest discover -s seneschal/scripts -p "test_brief_unknowns_wiring.py"   (from the repo root)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))

import loops  # noqa: E402
import owi_unknowns as ou  # noqa: E402
import presence as pr  # noqa: E402

NOW = datetime(2026, 1, 16, 11, 30, tzinfo=timezone.utc)
GOOD = dict(audience="owner", terminal_state="it ships or the owner drops it",
            whose_move="unknown", next_action="unknown", kind="unknown", priority="unknown")


class _RcProc:
    def __init__(self, rc=None):
        self._rc = rc

    def poll(self):
        return self._rc


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"SENESCHAL_STATE_DIR": self.state})
        env.start()
        self.addCleanup(env.stop)

    def add(self, text, **over):
        kwargs = dict(GOOD)
        kwargs.update(over)
        return loops.add(self.state, text=text, now=NOW, **kwargs)


class BriefLine(Base):
    def test_zero_unknowns_renders_nothing(self):
        self.assertIsNone(ou.brief_line(self.state))

    def test_singular_and_plural(self):
        self.add("one thing")
        self.assertIn("1 item with no owner", ou.brief_line(self.state))
        self.add("another thing")
        self.assertIn("2 items with no owner", ou.brief_line(self.state))

    def test_a_known_owner_is_not_counted(self):
        self.add("the owner's", whose_move="owner")
        self.assertIsNone(ou.brief_line(self.state))

    def test_cli_brief_line_prints_count_and_line(self):
        self.add("x")
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            rc = ou.main(["--state-dir", self.state, "brief-line"])
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["count"], 1)
        self.assertIn("1 item", data["line"])


_PRESENCE_WIRING = hasattr(pr, "_brief_context")
_WAVE_26 = "presence wiring lands in wave 26"


@unittest.skipUnless(_PRESENCE_WIRING, _WAVE_26)
class LaunchCarriesTheCount(Base):
    """`maybe_run_slots` appends the code-rendered line to the morning-brief prompt."""

    def _args(self):
        return argparse.Namespace(no_slots=False, stub_brain=False, fake_inbox=None,
                                  slot_catchup_min=180, claude_bin="claude", notion_mcp=None,
                                  slack_mcp=None, permission_mode="bypassPermissions",
                                  slot_model=None, model=None)

    def _launch(self):
        brief = next(s for s in pr.SLOTS if s["name"] == "morning-brief")
        with mock.patch.object(pr, "datetime") as mock_dt, \
             mock.patch.object(pr, "SLOTS", [brief]), \
             mock.patch.object(pr.subprocess, "Popen", return_value=_RcProc()) as popen:
            mock_dt.now.return_value = datetime(2026, 1, 16, 6, 31)
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            pr.maybe_run_slots(self.state, self._args(), lambda *_: None, [], warm_busy=False,
                               slot_children={}, slot_hold_until={})
        self.assertEqual(popen.call_count, 1)
        cmd = popen.call_args[0][0]
        return cmd[cmd.index("-p") + 1]

    def test_prompt_carries_the_rendered_line_verbatim(self):
        self.add("a"); self.add("b"); self.add("c")
        prompt = self._launch()
        self.assertIn(ou.brief_line(self.state), prompt)
        self.assertIn("VERBATIM", prompt)
        self.assertIn("do not run owi_unknowns.py count yourself", prompt)

    def test_prompt_says_zero_explicitly_so_the_section_is_omitted(self):
        prompt = self._launch()
        self.assertIn("computed by code at launch: 0", prompt)
        self.assertIn("omit", prompt)

    def test_a_raising_helper_does_not_cost_the_launch(self):
        with mock.patch.object(ou, "brief_line", side_effect=RuntimeError("boom")):
            prompt = self._launch()
        self.assertIn("morning Brief", prompt)
        self.assertNotIn("Unassigned-work", prompt)

    def test_the_morning_brief_slot_declares_both_hooks(self):
        brief = next(s for s in pr.SLOTS if s["name"] == "morning-brief")
        # `context` is the combined hook — it renders the unassigned-work line AND Tomorrow's Lead
        # in one call, since a slot carries exactly one `context` entry.
        self.assertEqual(brief.get("context"), "brief_context")
        self.assertEqual(brief.get("on_complete"), "brief_sent")
        self.assertIn(brief["context"], pr.SLOT_CONTEXT)
        self.assertIn(brief["on_complete"], pr.SLOT_ON_COMPLETE)


@unittest.skipUnless(_PRESENCE_WIRING, _WAVE_26)
class CleanExitResetsTheWindow(Base):
    """`reap_finished_slots` is the one caller of `note_brief_sent` now."""

    def _gate(self):
        return ou._load_gate(self.state)

    def test_clean_exit_stamps_brief_at_and_clears_stop(self):
        ou.stop(self.state)
        ou.mark_asked(self.state)
        self.assertIsNone(self._gate().get("brief_at"))
        pr.reap_finished_slots({"morning-brief": _RcProc(rc=0)}, self.state, lambda *_: None, {},
                               now_local=datetime(2026, 1, 16, 6, 40))
        gate = self._gate()
        self.assertIsNotNone(gate.get("brief_at"))
        self.assertIsNone(gate.get("asked_at"))
        self.assertFalse(ou._load_cursor(self.state).get("stopped"))

    def test_failed_exit_does_not_reset(self):
        pr.reap_finished_slots({"morning-brief": _RcProc(rc=1)}, self.state, lambda *_: None, {},
                               now_local=datetime(2026, 1, 16, 6, 40))
        self.assertIsNone(self._gate().get("brief_at"))

    def test_other_slots_do_not_reset(self):
        pr.reap_finished_slots({"eod-wrap": _RcProc(rc=0)}, self.state, lambda *_: None, {},
                               now_local=datetime(2026, 1, 16, 21, 20))
        self.assertIsNone(self._gate().get("brief_at"))

    def test_a_raising_hook_never_blocks_the_stamp(self):
        logged = []
        with mock.patch.object(ou, "note_brief_sent", side_effect=RuntimeError("boom")):
            pr.reap_finished_slots({"morning-brief": _RcProc(rc=0)}, self.state, logged.append, {},
                                   now_local=datetime(2026, 1, 16, 6, 40))
        stamp = pr.load_json(os.path.join(self.state, "slots.json"), {})
        self.assertEqual(stamp.get("morning-brief"), "2026-01-16")
        self.assertTrue(any("on_complete" in line and "failed" in line for line in logged))


class JointAskForBoth(Base):
    """`loops.ask_line` — the drafted re-ask, by `whose_move`."""

    def test_both_is_a_joint_ask_not_a_nag(self):
        item = self.add("book the venue", whose_move="both")
        line = loops.ask_line(item)
        self.assertIn("Do you have time for", line)
        self.assertIn("book the venue", line)
        self.assertIn("or should I move on it myself", line)

    def test_owner_asks_where_it_stands(self):
        item = self.add("sign the form", whose_move="owner")
        self.assertIn("is on you", loops.ask_line(item))

    def test_not_the_assistants_to_raise_gets_no_line(self):
        for move in ("assistant", "external", "unknown"):
            self.assertIsNone(loops.ask_line(self.add(f"x {move}", whose_move=move)))

    def test_terminal_row_gets_no_line(self):
        item = self.add("done thing", whose_move="both")
        loops.resolve(self.state, item_id=item["id"], because="done")
        self.assertIsNone(loops.ask_line(loops.get(loops.load(self.state), item["id"])))

    def test_resurface_report_carries_the_asks_but_the_log_row_does_not(self):
        import owi_resurface
        both = self.add("plan the trip", whose_move="both")
        rep = owi_resurface.report(self.state, now=NOW)
        self.assertIn(both["id"], rep["missed_asks"])
        self.assertIn("Do you have time for", rep["missed_asks"][both["id"]])
        row = owi_resurface.log_run(self.state, now=NOW)
        self.assertNotIn("missed_asks", row)

    def test_mine_prints_the_ask_as_the_last_column(self):
        # Stamped at the real clock, not NOW: `mine` reads the default query, which leaves out a row
        # gone dormant — a fixed-date fixture would silently age out of it as the calendar moves.
        kwargs = dict(GOOD, whose_move="both")
        loops.add(self.state, text="plan the trip", **kwargs)
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            loops.main(["--state-dir", self.state, "mine"])
        line = [ln for ln in out.getvalue().splitlines() if "plan the trip" in ln][0]
        self.assertTrue(line.endswith("or should I move on it myself?"))


class ProseNoLongerAsksTheRun(unittest.TestCase):
    """The three prose files must not instruct the run to call `note-brief-sent` — the daemon owns it.
    Naming the verb to SAY it is code-owned is fine; an imperative to run it is the regression."""

    FILES = ("seneschal/modes/brief.md", "seneschal/references/briefing.md",
             "subagents/morning-briefing/SKILL.md")

    def test_no_imperative_to_run_note_brief_sent(self):
        for rel in self.FILES:
            with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotRegex(text, r"\*\*[^*]*run\s+`(python\s+)?[\w./]*owi_unknowns\.py note-brief-sent`",
                                f"{rel} still tells the run to call note-brief-sent itself")
            self.assertIn("presence.py", text, f"{rel} must say where the count/reset now live")


if __name__ == "__main__":
    unittest.main()
