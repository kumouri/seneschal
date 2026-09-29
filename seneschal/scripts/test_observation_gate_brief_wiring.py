#!/usr/bin/env python3
"""`presence.py`'s wiring for the observation-gate Brief lead-in (`../docs/observation-gate-spec.md`):

`_brief_context` — the `morning-brief` slot's one `context` hook — renders
`observation_gate.brief_line`'s text FIRST, above the unassigned-work line and Tomorrow's Lead, each
guarded independently so one raising helper never costs the others. Skipped until the presence
wiring lands (wave 26); the bodies activate automatically once `presence._brief_context` exists.

Run:  python -m unittest discover -s seneschal/scripts -p "test_observation_gate_brief_wiring.py"   (from the repo root)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import loops  # noqa: E402
import presence as pr  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"SENESCHAL_STATE_DIR": self.state})
        env.start()
        self.addCleanup(env.stop)


GOOD = dict(
    text="watch the gate", audience="owner", terminal_state="ships",
    whose_move="assistant", next_action="watch", kind="enhancement", priority="normal",
)


@unittest.skipUnless(hasattr(pr, "_brief_context"), "presence wiring lands in wave 26")
class BriefContext(Base):
    def test_nothing_ready_says_so_and_says_omit(self):
        text = pr._brief_context(self.state)
        self.assertIn("nothing ready", text)
        self.assertIn("omit", text)

    def test_a_completed_gate_renders_verbatim_and_leads(self):
        item = loops.add(self.state, **GOOD)
        loops.observe(self.state, item_id=item["id"],
                     requires=[{"type": "min_elapsed", "days": 0}])
        loops.mark_observation_complete(self.state, item_id=item["id"])
        text = pr._brief_context(self.state)
        self.assertIn("ready to continue", text)
        self.assertIn("ABOVE Tomorrow's Lead", text)
        # It is the FIRST section in the combined context.
        self.assertLess(text.index("ready to continue"), text.index("Unassigned-work"))

    def test_a_raising_helper_does_not_cost_the_other_sections(self):
        item = loops.add(self.state, **GOOD)
        loops.observe(self.state, item_id=item["id"],
                     requires=[{"type": "min_elapsed", "days": 0}])
        loops.mark_observation_complete(self.state, item_id=item["id"])
        with mock.patch("observation_gate.brief_line", side_effect=RuntimeError("boom")):
            text = pr._brief_context(self.state)
        self.assertIn("Unassigned-work", text)

        with mock.patch("owi_unknowns.brief_line", side_effect=RuntimeError("boom")):
            text = pr._brief_context(self.state)
        self.assertIn("ready to continue", text)

    def test_slot_context_wires_the_combined_hook(self):
        self.assertIn("brief_context", pr.SLOT_CONTEXT)
        self.assertIs(pr.SLOT_CONTEXT["brief_context"], pr._brief_context)


if __name__ == "__main__":
    unittest.main()
