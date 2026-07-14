#!/usr/bin/env python3
"""Unit tests for the pure HA action-mapping (``presence_actions``). Stdlib only; no Home Assistant."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import presence_actions as pa  # noqa: E402

AUTOS = [
    {"id": "arrive-lights", "on": {"kind": "geofence", "label": "home", "transition": "enter"},
     "call": {"domain": "light", "service": "turn_on"}, "approved": True},
    {"id": "leave-lights", "on": {"kind": "geofence", "label": "home", "transition": "exit"},
     "call": {"domain": "light", "service": "turn_off"}, "approved": False},
    {"id": "leave-failsafe", "on": {"kind": "geofence", "label": "home", "transition": "exit"},
     "call": {"domain": "switch", "service": "turn_off"}, "approved": True, "failsafe": True},
]


class ActionsForEdgeTest(unittest.TestCase):
    def test_matches_enter(self):
        m = pa.actions_for_edge({"kind": "geofence", "label": "home", "transition": "enter"}, AUTOS)
        self.assertEqual([a["id"] for a in m], ["arrive-lights"])

    def test_matches_exit_multiple(self):
        m = pa.actions_for_edge({"kind": "geofence", "label": "home", "transition": "exit"}, AUTOS)
        self.assertEqual(sorted(a["id"] for a in m), ["leave-failsafe", "leave-lights"])

    def test_no_match(self):
        self.assertEqual(
            pa.actions_for_edge({"kind": "geofence", "label": "work", "transition": "enter"}, AUTOS), [])


class DispositionTest(unittest.TestCase):
    def test_approved_is_act_low(self):
        self.assertEqual(pa.disposition({"approved": True}), "act-low")

    def test_unapproved_is_ask_high(self):
        self.assertEqual(pa.disposition({"approved": False}), "ask-high")
        self.assertEqual(pa.disposition({}), "ask-high")

    def test_failsafe_always_ask_high(self):
        # Even a pre-approved failsafe stays ask-high — the leak cutoff never fires unattended.
        self.assertEqual(pa.disposition({"approved": True, "failsafe": True}), "ask-high")


class LoadTest(unittest.TestCase):
    def test_missing_config_is_empty(self):
        self.assertEqual(pa.load_automations("/no/such/file.json"), [])


if __name__ == "__main__":
    unittest.main()
