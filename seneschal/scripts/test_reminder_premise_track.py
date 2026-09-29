#!/usr/bin/env python3
"""Unit tests for reminder_premise_track.py — the local (store-independent) durable half of the
premise-review question. Stdlib unittest; a tempdir stands in for state/."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reminder_premise_track as rpt  # noqa: E402


class StoreRoundTrip(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_never_reviewed_reads_none(self):
        self.assertIsNone(rpt.last_reviewed_at_misses(self.d, "row-1"))

    def test_mark_then_read_round_trips(self):
        self.assertTrue(rpt.mark_reviewed(self.d, "row-1", 6))
        self.assertEqual(rpt.last_reviewed_at_misses(self.d, "row-1"), 6)

    def test_mark_overwrites_the_prior_value(self):
        rpt.mark_reviewed(self.d, "row-1", 6)
        rpt.mark_reviewed(self.d, "row-1", 12)
        self.assertEqual(rpt.last_reviewed_at_misses(self.d, "row-1"), 12)

    def test_two_rows_are_independent(self):
        rpt.mark_reviewed(self.d, "row-1", 6)
        rpt.mark_reviewed(self.d, "row-2", 4)
        self.assertEqual(rpt.last_reviewed_at_misses(self.d, "row-1"), 6)
        self.assertEqual(rpt.last_reviewed_at_misses(self.d, "row-2"), 4)

    def test_store_is_atomic_json_with_schema(self):
        rpt.mark_reviewed(self.d, "row-1", 6)
        with open(os.path.join(self.d, rpt.STORE_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["schema"], rpt.SCHEMA)
        self.assertEqual(data["reviewed"], {"row-1": 6})

    def test_reminder_id_is_stringified(self):
        # A store page id could arrive as-is; an int slips in from a careless caller. Both key the
        # same row on read as on write.
        rpt.mark_reviewed(self.d, 42, 6)
        self.assertEqual(rpt.last_reviewed_at_misses(self.d, "42"), 6)


class FailOpenReads(unittest.TestCase):
    """Losing this store must cost one redundant question, never manufacture silence — the same
    direction reminder_premise.review_due already takes for an unreadable marker."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_missing_store_reads_as_never_reviewed(self):
        self.assertIsNone(rpt.last_reviewed_at_misses(self.d, "row-1"))

    def test_corrupt_json_reads_as_never_reviewed(self):
        with open(os.path.join(self.d, rpt.STORE_FILE), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertIsNone(rpt.last_reviewed_at_misses(self.d, "row-1"))

    def test_wrong_shape_reads_as_never_reviewed(self):
        with open(os.path.join(self.d, rpt.STORE_FILE), "w", encoding="utf-8") as fh:
            json.dump(["not", "a", "dict"], fh)
        self.assertIsNone(rpt.last_reviewed_at_misses(self.d, "row-1"))

    def test_non_int_value_reads_as_never_reviewed(self):
        with open(os.path.join(self.d, rpt.STORE_FILE), "w", encoding="utf-8") as fh:
            json.dump({"schema": rpt.SCHEMA, "reviewed": {"row-1": "six"}}, fh)
        self.assertIsNone(rpt.last_reviewed_at_misses(self.d, "row-1"))

    def test_mark_reviewed_never_raises_when_the_path_is_unwritable(self):
        # A directory squatting on the store path makes the write impossible; the caller (the seed)
        # must survive it, matching the house rule for every state/ writer.
        os.makedirs(os.path.join(self.d, rpt.STORE_FILE))
        self.assertFalse(rpt.mark_reviewed(self.d, "row-1", 6))


class CheckPremiseReview(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_below_threshold_returns_none_and_marks_nothing(self):
        result = rpt.check_premise_review(self.d, "row-1", 3, "✨ Notable", "Walk")
        self.assertIsNone(result)
        self.assertIsNone(rpt.last_reviewed_at_misses(self.d, "row-1"))

    def test_at_threshold_returns_the_question_and_marks(self):
        from reminder_premise import DEFAULT_THRESHOLD

        result = rpt.check_premise_review(self.d, "row-1", DEFAULT_THRESHOLD, "✨ Notable", "Walk")
        self.assertIsNotNone(result)
        self.assertIn("Walk", result)
        self.assertIn(str(DEFAULT_THRESHOLD), result)
        self.assertEqual(rpt.last_reviewed_at_misses(self.d, "row-1"), DEFAULT_THRESHOLD)

    def test_mark_false_returns_the_question_but_writes_nothing(self):
        from reminder_premise import DEFAULT_THRESHOLD

        result = rpt.check_premise_review(self.d, "row-1", DEFAULT_THRESHOLD, "✨ Notable", "Walk",
                                           mark=False)
        self.assertIsNotNone(result)
        self.assertIsNone(rpt.last_reviewed_at_misses(self.d, "row-1"))

    def test_asked_once_then_not_asked_again_one_miss_later(self):
        # Asked at the threshold, one more miss, not due yet again.
        from reminder_premise import DEFAULT_THRESHOLD

        first = rpt.check_premise_review(self.d, "row-1", DEFAULT_THRESHOLD, "✨ Notable", "Walk")
        self.assertIsNotNone(first)
        second = rpt.check_premise_review(self.d, "row-1", DEFAULT_THRESHOLD + 1, "✨ Notable", "Walk")
        self.assertIsNone(second)

    def test_rearms_a_full_threshold_past_the_last_ask(self):
        from reminder_premise import DEFAULT_THRESHOLD

        rpt.check_premise_review(self.d, "row-1", DEFAULT_THRESHOLD, "✨ Notable", "Walk")
        third = rpt.check_premise_review(self.d, "row-1", DEFAULT_THRESHOLD * 2, "✨ Notable", "Walk")
        self.assertIsNotNone(third)

    def test_no_reminder_id_returns_none(self):
        self.assertIsNone(rpt.check_premise_review(self.d, None, 6, "✨ Notable", "Walk"))
        self.assertIsNone(rpt.check_premise_review(self.d, "", 6, "✨ Notable", "Walk"))

    def test_critical_row_is_due_sooner_than_a_default_one(self):
        from reminder_premise import CRITICAL_THRESHOLD

        result = rpt.check_premise_review(self.d, "row-crit", CRITICAL_THRESHOLD, "🚨 Critical",
                                           "Stretch")
        self.assertIsNotNone(result)

    def test_never_states_a_verdict(self):
        from reminder_premise import DEFAULT_THRESHOLD

        result = rpt.check_premise_review(self.d, "row-1", DEFAULT_THRESHOLD, "✨ Notable", "Walk")
        for banned in ("retire", "finished", "should", "recommend"):
            self.assertNotIn(banned, result.lower())


if __name__ == "__main__":
    unittest.main()
