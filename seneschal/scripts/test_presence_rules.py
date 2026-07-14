#!/usr/bin/env python3
"""Unit tests for the pure presence rule logic (``presence_rules``). Stdlib only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import presence_rules as pr  # noqa: E402


class PresenceGateTest(unittest.TestCase):
    def test_entry_without_conditions_fires(self):
        fire, _ = pr.presence_gate({"text": "take meds"}, {"at_place": None})
        self.assertTrue(fire)

    def test_require_place_defers_when_away(self):
        fire, reason = pr.presence_gate({"require_place": "home"}, {"at_place": None})
        self.assertFalse(fire)
        self.assertIn("home", reason)

    def test_require_place_defers_when_elsewhere(self):
        fire, _ = pr.presence_gate({"require_place": "home"}, {"at_place": "work"})
        self.assertFalse(fire)

    def test_require_place_fires_when_present(self):
        fire, reason = pr.presence_gate({"require_place": "home"}, {"at_place": "home"})
        self.assertTrue(fire)
        self.assertEqual(reason, "ok")

    def test_empty_or_missing_context_defers_gated_entry(self):
        self.assertFalse(pr.presence_gate({"require_place": "home"}, {})[0])
        self.assertFalse(pr.presence_gate({"require_place": "home"}, None)[0])

    def test_should_defer_matches_gate(self):
        self.assertTrue(pr.should_defer({"require_place": "home"}, {"at_place": None}))
        self.assertFalse(pr.should_defer({"require_place": "home"}, {"at_place": "home"}))
        self.assertFalse(pr.should_defer({}, {}))

    def test_driving_defers_non_piercing(self):
        fire, reason = pr.presence_gate({"text": "walk"}, {"activity": "in_vehicle"})
        self.assertFalse(fire)
        self.assertIn("driving", reason)

    def test_driving_fires_piercing(self):
        self.assertTrue(pr.presence_gate({"text": "meds"}, {"activity": "in_vehicle"}, pierces=True)[0])

    def test_not_driving_fires(self):
        self.assertTrue(pr.presence_gate({"text": "walk"}, {"activity": "still"})[0])
        self.assertTrue(pr.presence_gate({"text": "walk"}, {"activity": None})[0])

    def test_should_defer_driving(self):
        self.assertTrue(pr.should_defer({"text": "walk"}, {"activity": "in_vehicle"}))
        self.assertFalse(pr.should_defer({"text": "walk"}, {"activity": "in_vehicle"}, pierces=True))
        self.assertFalse(pr.should_defer({"text": "walk"}, {"activity": "walking"}))

    def test_asleep_is_ignored(self):
        # The Phase-3 sleep rule is RETIRED (2026-07-13): the phone-only Sleep API reads an idle phone
        # as a sleeping owner, so a fresh "asleep" context must NOT hold anything.
        fire, reason = pr.presence_gate({"text": "walk"}, {"asleep": True})
        self.assertTrue(fire)
        self.assertEqual(reason, "ok")
        self.assertFalse(pr.should_defer({"text": "walk"}, {"asleep": True}))

    def test_asleep_does_not_compose_with_place(self):
        # require_place still gates on its own; a (false) asleep flag adds nothing on top.
        self.assertFalse(pr.should_defer({"require_place": "home"}, {"at_place": "home", "asleep": True}))
        self.assertTrue(pr.should_defer({"require_place": "home"}, {"at_place": None, "asleep": True}))


if __name__ == "__main__":
    unittest.main()
