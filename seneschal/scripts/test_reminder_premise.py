#!/usr/bin/env python3
"""Unit tests for the pure premise-review decision logic (``reminder_premise``). Stdlib only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reminder_premise as rp  # noqa: E402


class ThresholdForTest(unittest.TestCase):
    def test_any_backend_spelling_of_critical_gets_the_lower_bar(self):
        for imp in ("critical", "super-critical", "Super Critical"):
            self.assertEqual(rp.threshold_for(imp), rp.CRITICAL_THRESHOLD, imp)
        for imp in ("high", "notable", "low"):
            self.assertEqual(rp.threshold_for(imp), rp.DEFAULT_THRESHOLD, imp)

    def test_default_importance_gets_default_threshold(self):
        self.assertEqual(rp.threshold_for("✨ Notable"), rp.DEFAULT_THRESHOLD)
        self.assertEqual(rp.threshold_for(None), rp.DEFAULT_THRESHOLD)
        self.assertEqual(rp.threshold_for("unrecognized"), rp.DEFAULT_THRESHOLD)

    def test_critical_and_super_critical_get_the_lower_bar(self):
        self.assertEqual(rp.threshold_for("🚨 Critical"), rp.CRITICAL_THRESHOLD)
        self.assertEqual(rp.threshold_for("🛑 Super-Critical"), rp.CRITICAL_THRESHOLD)

    def test_overrides_are_honored(self):
        self.assertEqual(rp.threshold_for("🚨 Critical", critical_threshold=2), 2)
        self.assertEqual(rp.threshold_for("📌 Low", default_threshold=9), 9)


class ReviewDueTest(unittest.TestCase):
    def test_below_threshold_is_not_due(self):
        self.assertFalse(rp.review_due(3, "✨ Notable", None))

    def test_at_threshold_never_reviewed_is_due(self):
        self.assertTrue(rp.review_due(rp.DEFAULT_THRESHOLD, "✨ Notable", None))

    def test_critical_row_is_due_sooner_than_a_non_critical_one(self):
        misses = rp.CRITICAL_THRESHOLD
        self.assertTrue(rp.review_due(misses, "🚨 Critical", None))
        self.assertFalse(rp.review_due(misses, "✨ Notable", None))

    def test_reviewed_recently_is_not_due_again(self):
        # Case 1's shape: asked once at the threshold, one more miss since.
        self.assertFalse(rp.review_due(rp.DEFAULT_THRESHOLD + 1, "✨ Notable", rp.DEFAULT_THRESHOLD))

    def test_rearms_once_misses_climb_a_full_threshold_past_the_last_ask(self):
        last = rp.DEFAULT_THRESHOLD
        self.assertFalse(rp.review_due(last + rp.DEFAULT_THRESHOLD - 1, "✨ Notable", last))
        self.assertTrue(rp.review_due(last + rp.DEFAULT_THRESHOLD, "✨ Notable", last))

    def test_malformed_consecutive_misses_is_never_due(self):
        for bad in (None, -1, "6", 3.5, True):
            self.assertFalse(rp.review_due(bad, "🚨 Critical", None))

    def test_malformed_last_reviewed_marker_fails_open_toward_asking(self):
        # An unreadable "already asked" record must not be able to silence a real question —
        # the opposite failure direction from a malformed consecutive_misses.
        for bad in (-1, "6", 3.5, True):
            self.assertTrue(rp.review_due(rp.DEFAULT_THRESHOLD, "✨ Notable", bad))

    def test_a_critical_row_fires_at_the_critical_threshold(self):
        # A Critical row four misses deep is already due — the lower bar for louder rows.
        self.assertTrue(rp.review_due(4, "🚨 Critical", None))

    def test_a_low_row_fires_at_the_default_threshold(self):
        # 6 consecutive misses, never acked once.
        self.assertTrue(rp.review_due(6, "📌 Low", None))


class FormatReviewQuestionTest(unittest.TestCase):
    def test_without_a_premise(self):
        text = rp.format_review_question("Grab a usage-panel screenshot", 6, None)
        self.assertEqual(text, "Grab a usage-panel screenshot — 6 cycles, never acked. Still a thing?")

    def test_with_a_premise_attaches_it(self):
        text = rp.format_review_question("Stretch (AM)", 4, "a sore back")
        self.assertIn("a sore back", text)
        self.assertTrue(text.startswith("Stretch (AM) — 4 cycles"))

    def test_never_states_a_verdict(self):
        text = rp.format_review_question("Some row", 10, None)
        for banned in ("retire", "Finished", "should", "recommend"):
            self.assertNotIn(banned.lower(), text.lower())


if __name__ == "__main__":
    unittest.main()
