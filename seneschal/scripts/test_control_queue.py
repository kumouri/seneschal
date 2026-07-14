#!/usr/bin/env python3
"""Tests for the daemon control queue — enqueue (dedupe + defer flag) and the daemon's load/pop helpers.

Stdlib ``unittest`` only (matches the Markdown-skill core; CI byte-compiles Python, and this file runs
green under ``python -m unittest``). Covers the Path A graceful-restart mechanism: request_control.py
enqueues flagged control entries (restart/shutdown), deduped by action; presence.py loads + pops them to
apply once the warm session is idle.

Run:  python -m unittest seneschal.scripts.test_control_queue   (or)   python test_control_queue.py
"""
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import request_control as rc  # noqa: E402
import presence as p  # noqa: E402


class ControlQueueUnit(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _queue(self):
        with open(os.path.join(self.d, "control-queue.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_enqueue_defaults_to_defer(self):
        rc.enqueue_control(self.d, "restart", reason="merge to main")
        q = self._queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["action"], "restart")
        self.assertTrue(q[0]["defer_until_idle"])
        self.assertEqual(q[0]["reason"], "merge to main")
        self.assertIn("requested_at", q[0])

    def test_now_flag_disables_defer(self):
        rc.enqueue_control(self.d, "shutdown", defer=False)
        self.assertFalse(self._queue()[0]["defer_until_idle"])

    def test_dedupe_by_action(self):
        rc.enqueue_control(self.d, "restart", reason="first")
        rc.enqueue_control(self.d, "restart", reason="dup — ignored")
        q = self._queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["reason"], "first")  # first one wins; the dup is dropped

    def test_distinct_actions_coexist(self):
        rc.enqueue_control(self.d, "restart")
        rc.enqueue_control(self.d, "shutdown")
        self.assertEqual([i["action"] for i in self._queue()], ["restart", "shutdown"])

    def test_load_and_pop_fifo(self):
        rc.enqueue_control(self.d, "restart")
        rc.enqueue_control(self.d, "shutdown")
        self.assertEqual(len(p.load_control_queue(self.d)), 2)
        head = p.pop_control(self.d)
        self.assertEqual(head["action"], "restart")           # FIFO: restart applied first
        remaining = p.load_control_queue(self.d)
        self.assertEqual([i["action"] for i in remaining], ["shutdown"])

    def test_pop_empty_is_none(self):
        self.assertIsNone(p.pop_control(self.d))

    def test_load_tolerates_garbage(self):
        with open(os.path.join(self.d, "control-queue.json"), "w", encoding="utf-8") as fh:
            fh.write('{"not": "a list"}')
        self.assertEqual(p.load_control_queue(self.d), [])


if __name__ == "__main__":
    unittest.main()
