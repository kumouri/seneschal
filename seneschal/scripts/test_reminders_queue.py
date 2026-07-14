#!/usr/bin/env python3
"""Tests for the reminders queue helpers — enqueue (--reminder-id) + dequeue-on-ack.

Stdlib ``unittest`` only (matches the Markdown-skill core; CI byte-compiles Python, and this file runs
green under ``python -m unittest``). Covers the 2026-07-03 fix: an ack must cancel the still-queued,
un-fired nudges it makes obsolete, without touching already-fired history or unrelated items.

Run:  python -m unittest seneschal.scripts.test_reminders_queue   (or)   python test_reminders_queue.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import reminders_dequeue as dq  # noqa: E402

ENQUEUE = os.path.join(SCRIPT_DIR, "reminders_enqueue.py")
DEQUEUE = os.path.join(SCRIPT_DIR, "reminders_dequeue.py")


class CancelUnit(unittest.TestCase):
    def _rows(self):
        return [
            {"id": "e-unfired-cats", "reminder_id": "cats-am", "fired_at": None},
            {"id": "e-fired-cats", "reminder_id": "cats-am", "fired_at": "2026-07-03T13:45:14Z"},
            {"id": "e-unfired-breakfast", "reminder_id": "breakfast", "fired_at": None},
            {"id": "e-no-rid", "fired_at": None},
        ]

    def test_cancels_unfired_match_only(self):
        kept, removed = dq.cancel(self._rows(), reminder_ids=["cats-am"])
        self.assertEqual([r["id"] for r in removed], ["e-unfired-cats"])
        self.assertIn("e-fired-cats", [r["id"] for r in kept])       # fired history preserved
        self.assertIn("e-unfired-breakfast", [r["id"] for r in kept])  # other item preserved

    def test_dash_insensitive_page_id(self):
        rows = [{"id": "e1", "reminder_id": "00000000-0000-0000-0000-000000000001", "fired_at": None}]
        # same id, dashes stripped, should still match
        _, removed = dq.cancel(rows, reminder_ids=["00000000000000000000000000000001"])
        self.assertEqual(len(removed), 1)

    def test_by_entry_id(self):
        _, removed = dq.cancel(self._rows(), entry_ids=["e-no-rid"])
        self.assertEqual([r["id"] for r in removed], ["e-no-rid"])

    def test_empty_reminder_id_never_matches(self):
        # a row with no reminder_id must not be swept by an empty/absent key
        kept, removed = dq.cancel(self._rows(), reminder_ids=[""])
        self.assertEqual(removed, [])
        self.assertEqual(len(kept), 4)

    def test_idempotent_absent_key(self):
        _, removed = dq.cancel(self._rows(), reminder_ids=["not-here"])
        self.assertEqual(removed, [])

    def test_non_dict_rows_survive(self):
        rows = ["junk", {"id": "e", "reminder_id": "x", "fired_at": None}]
        kept, removed = dq.cancel(rows, reminder_ids=["x"])
        self.assertIn("junk", kept)
        self.assertEqual([r["id"] for r in removed], ["e"])


class CliRoundTrip(unittest.TestCase):
    """Enqueue a nudge with --reminder-id, then dequeue by that id — through the real CLIs."""

    def test_enqueue_then_dequeue(self):
        with tempfile.TemporaryDirectory() as d:
            def run(script, *args):
                out = subprocess.run([sys.executable, script, "--state-dir", d, *args],
                                     capture_output=True, text=True)
                self.assertEqual(out.returncode, 0, out.stderr)
                return json.loads(out.stdout.strip().splitlines()[-1])

            enq = run(ENQUEUE, "--text", "Cats' breakfast.",
                      "--id", "rmd-t-cats", "--reminder-id", "cats-am")
            self.assertTrue(enq["enqueued"])

            # an unrelated, already-fired entry that must survive the cancel
            path = os.path.join(d, "reminders.json")
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
            rows.append({"id": "rmd-t-meds", "reminder_id": "cats-am",
                         "text": "x", "due_at": "2026-07-03T13:00:00Z",
                         "channel": "telegram", "created_at": "2026-07-03T13:00:00Z",
                         "fired_at": "2026-07-03T13:00:05Z"})
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(rows, fh)

            deq = run(DEQUEUE, "--reminder-id", "cats-am")
            self.assertEqual(deq["ids"], ["rmd-t-cats"])   # only the un-fired one

            with open(path, encoding="utf-8") as fh:
                left = json.load(fh)
            self.assertEqual([r["id"] for r in left], ["rmd-t-meds"])  # fired one stays

            # idempotent: a second cancel removes nothing
            again = run(DEQUEUE, "--reminder-id", "cats-am")
            self.assertEqual(again["removed"], 0)

    def test_dequeue_requires_a_selector(self):
        with tempfile.TemporaryDirectory() as d:
            out = subprocess.run([sys.executable, DEQUEUE, "--state-dir", d],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 2)


if __name__ == "__main__":
    unittest.main()
