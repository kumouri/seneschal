#!/usr/bin/env python3
"""Tests for Proteus's standing hunt loop (hunt_cycle.py + daily_digest.py) — pure functions only.

Stdlib ``unittest``; no network, no subprocesses, temp files only for quiet_active().
"""

import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "archons", "proteus", "tools"))

import daily_digest  # noqa: E402
import hunt_cycle  # noqa: E402

NOW = "2026-07-12T20:00:00Z"


def _job(url="https://x/1", score=70.0, flags=None, **extra):
    return dict({"url": url, "title": "Senior AI Engineer", "company": "Acme",
                 "match_percent": score, "flags": flags or [], "comp_max": 210000,
                 "location": "Remote, US"}, **extra)


class DiffJobs(unittest.TestCase):
    def test_new_hot_job_detected_and_ledgered(self):
        hot, ledger = hunt_cycle.diff_jobs([_job()], {}, 60.0, NOW)
        self.assertEqual(len(hot), 1)
        entry = ledger["https://x/1"]
        self.assertEqual(entry["first_seen"], NOW)
        self.assertEqual(entry["best_score"], 70.0)
        self.assertFalse(entry["notified"])

    def test_already_hot_job_not_redetected(self):
        _, ledger = hunt_cycle.diff_jobs([_job()], {}, 60.0, "2026-07-12T19:00:00Z")
        hot, ledger = hunt_cycle.diff_jobs([_job()], ledger, 60.0, NOW)
        self.assertEqual(hot, [])
        self.assertEqual(ledger["https://x/1"]["last_seen"], NOW)

    def test_score_riser_crossing_threshold_detected(self):
        _, ledger = hunt_cycle.diff_jobs([_job(score=50.0)], {}, 60.0, "2026-07-12T19:00:00Z")
        hot, ledger = hunt_cycle.diff_jobs([_job(score=65.0)], ledger, 60.0, NOW)
        self.assertEqual(len(hot), 1)
        self.assertEqual(ledger["https://x/1"]["best_score"], 65.0)

    def test_dealbreaker_flagged_job_never_hot(self):
        hot, ledger = hunt_cycle.diff_jobs(
            [_job(score=80.0, flags=["dealbreaker: ts/sci"])], {}, 60.0, NOW)
        self.assertEqual(hot, [])
        self.assertIn("https://x/1", ledger)  # still ledgered for the digest

    def test_best_score_keeps_maximum(self):
        _, ledger = hunt_cycle.diff_jobs([_job(score=72.0)], {}, 60.0, "2026-07-12T19:00:00Z")
        _, ledger = hunt_cycle.diff_jobs([_job(score=61.0)], ledger, 60.0, NOW)
        self.assertEqual(ledger["https://x/1"]["best_score"], 72.0)
        self.assertEqual(ledger["https://x/1"]["last_score"], 61.0)

    def test_express_lane_hot_below_threshold(self):
        # A sub-60 role literally titled a target, true-remote + clean, is still hot.
        job = _job(score=52.0, remote_verdict="remote")
        job["target_title"] = "Forward Deployed Engineer"
        hot, ledger = hunt_cycle.diff_jobs([job], {}, 60.0, NOW)
        self.assertEqual(len(hot), 1)
        self.assertTrue(hot[0]["_express"])
        self.assertTrue(ledger["https://x/1"]["was_express"])

    def test_express_lane_requires_takeable_location(self):
        job = _job(score=52.0, remote_verdict="relocation")
        job["target_title"] = "Forward Deployed Engineer"
        hot, _ = hunt_cycle.diff_jobs([job], {}, 60.0, NOW)
        self.assertEqual(hot, [])  # target title but not takeable → not hot on its own

    def test_express_dealbreaker_still_blocks(self):
        job = _job(score=80.0, remote_verdict="remote", flags=["dealbreaker: relocation implied"])
        job["target_title"] = "Forward Deployed Engineer"
        hot, _ = hunt_cycle.diff_jobs([job], {}, 60.0, NOW)
        self.assertEqual(hot, [])


class FreshAndQuiet(unittest.TestCase):
    def test_fresh_enough_boundary(self):
        self.assertTrue(hunt_cycle.fresh_enough({"first_seen": "2026-07-12T18:00:00Z"}, NOW))
        self.assertFalse(hunt_cycle.fresh_enough({"first_seen": "2026-07-12T02:00:00Z"}, NOW))

    def test_quiet_active_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quiet.json")
            self.assertFalse(hunt_cycle.quiet_active(path))  # absent
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"until": "2099-01-01T00:00:00Z"}, fh)
            self.assertTrue(hunt_cycle.quiet_active(path))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"until": "2000-01-01T00:00:00Z"}, fh)
            self.assertFalse(hunt_cycle.quiet_active(path))

    def test_notify_line_mentions_the_essentials(self):
        line = hunt_cycle.notify_line(dict(_job(), comp_min=200000, remote_verdict="remote"))
        self.assertIn("Acme", line)
        self.assertIn("70.0%", line)
        self.assertIn("$200k+", line)
        self.assertIn("true remote", line)
        self.assertIn("https://x/1", line)

    def test_comp_label_prefers_min(self):
        self.assertEqual(hunt_cycle.comp_label({"comp_min": 200000, "comp_max": 310000}), "$200k+")
        self.assertEqual(hunt_cycle.comp_label({"comp_max": 260000}), "up to $260k")
        self.assertEqual(hunt_cycle.comp_label({}), "comp n/p")

    def test_digest_headline_carries_verdict_or_falls_back_to_location(self):
        entry = {"title": "AI Architect", "company": "Hot Co", "last_score": 71.0,
                 "comp_min": 210000, "location": "Remote", "remote_verdict": "remote"}
        line = daily_digest._fmt("https://x/hot", entry)
        self.assertIn("**Hot Co**", line)
        self.assertIn("$210k+", line)
        self.assertIn("✅ true remote", line)
        old_entry = {"title": "Old Row", "company": "Legacy Co", "last_score": 60.0,
                     "location": "New York, NY"}
        self.assertIn("📍 New York, NY", daily_digest._fmt("https://x/old", old_entry))


class BuildDigest(unittest.TestCase):
    DAY = "2026-07-12"

    def _ledger(self):
        seen = "2026-07-12T15:00:00Z"
        return {
            "https://x/hot": {"first_seen": seen, "last_seen": seen, "title": "AI Architect",
                              "company": "Hot Co", "best_score": 71.0, "last_score": 71.0,
                              "flags": [], "comp_max": 250000, "location": "Remote"},
            "https://x/near": {"first_seen": seen, "last_seen": seen, "title": "Backend Eng",
                               "company": "Near Co", "best_score": 52.0, "last_score": 52.0,
                               "flags": [], "comp_max": None, "location": "Remote"},
            "https://x/flagged": {"first_seen": seen, "last_seen": seen, "title": "Fed Architect",
                                  "company": "Gov Co", "best_score": 25.0, "last_score": 25.0,
                                  "flags": ["dealbreaker: citizenship requirement ('u.s. citizenship is required')"],
                                  "comp_max": 300000, "location": "Remote"},
            "https://x/gone": {"first_seen": "2026-07-10T15:00:00Z", "last_seen": "2026-07-11T23:00:00Z",
                               "title": "Agent Eng", "company": "Gone Co", "best_score": 66.0,
                               "last_score": 66.0, "flags": [], "comp_max": None, "location": "Remote"},
        }

    def test_sections_and_stats(self):
        cycles = [{"at": "2026-07-12T15:00:05Z", "notified": ["https://x/hot"]}]
        markdown, stats = daily_digest.build_digest(self._ledger(), cycles, self.DAY, 60.0, 45.0)
        self.assertEqual(stats["hot"], 1)
        self.assertEqual(stats["near"], 1)
        self.assertEqual(stats["flagged"], 1)
        self.assertEqual(stats["gone"], 1)
        self.assertIn("Hot Co", markdown)
        self.assertIn("(alerted)", markdown)
        self.assertIn("Near Co", markdown)
        self.assertIn("Gov Co", markdown)
        self.assertIn("Gone Co", markdown)

    def test_unalerted_hot_job_is_called_out(self):
        markdown, _ = daily_digest.build_digest(self._ledger(), [], self.DAY, 60.0, 45.0)
        self.assertIn("not alerted", markdown)

    def test_quiet_day_says_none(self):
        markdown, stats = daily_digest.build_digest({}, [], self.DAY, 60.0, 45.0)
        self.assertEqual(stats["hot"], 0)
        self.assertIn("none", markdown)

    def test_target_title_section(self):
        ledger = dict(self._ledger())
        ledger["https://x/fde"] = {"first_seen": "2026-07-12T15:00:00Z",
                                   "last_seen": "2026-07-12T15:00:00Z", "title": "Forward Deployed Engineer",
                                   "company": "Frontier AI", "best_score": 54.0, "last_score": 54.0,
                                   "flags": [], "comp_min": None, "comp_max": None, "location": "Remote",
                                   "remote_verdict": "remote", "target_title": "Forward Deployed Engineer"}
        markdown, stats = daily_digest.build_digest(ledger, [], self.DAY, 60.0, 45.0)
        self.assertEqual(stats["targets"], 1)
        self.assertIn("🎯 Target-title matches", markdown)
        self.assertIn("Frontier AI", markdown)
        self.assertIn("⟵ **Forward Deployed Engineer**", markdown)


if __name__ == "__main__":
    unittest.main()
