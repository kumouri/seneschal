#!/usr/bin/env python3
"""Tests for sentiment.py — the offline forgetting-event weight cross-check (salience Phase 2).

Stdlib ``unittest`` only, no Ollama: the one network call (``_ollama_chat``) is monkeypatched.
The contract under test is router.py's, transplanted: **never raises**, abstains to the neutral
``{weight 0.0, confidence 0.0}`` on any failure, clamps everything into range — because this
signal is a cross-check, a missing second opinion must mean "no flag", never a crash or a fake
strong weight.

Run:  python -m unittest seneschal.scripts.test_sentiment   (or)   python test_sentiment.py
"""
import os
import sys
import unittest
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import sentiment as st  # noqa: E402

CFG = dict(st.DEFAULTS)


def _patch(fn):
    """Swap _ollama_chat for the duration of a test."""
    orig = st._ollama_chat
    st._ollama_chat = fn
    return orig


class AbstainContract(unittest.TestCase):
    """Any failure → the neutral abstain; classify_sentiment never raises."""

    def tearDown(self):
        st._ollama_chat = self._orig if hasattr(self, "_orig") else st._ollama_chat

    def _expect_abstain(self, result, reason_contains=None):
        self.assertEqual(result["weight"], 0.0)
        self.assertEqual(result["confidence"], 0.0)
        if reason_contains:
            self.assertIn(reason_contains, result["reason"])

    def test_empty_text_abstains_without_calling_ollama(self):
        def boom(text, cfg, timeout):
            raise AssertionError("should not be called")
        self._orig = _patch(boom)
        self._expect_abstain(st.classify_sentiment("", CFG), "empty")
        self._expect_abstain(st.classify_sentiment("   ", CFG), "empty")
        self._expect_abstain(st.classify_sentiment(None, CFG), "empty")

    def test_ollama_down_abstains(self):
        def down(text, cfg, timeout):
            raise urllib.error.URLError("connection refused")
        self._orig = _patch(down)
        self._expect_abstain(st.classify_sentiment("you forgot my birthday", CFG),
                             "classifier unavailable")

    def test_bad_json_and_non_object_abstain(self):
        self._orig = _patch(lambda t, c, x: ["not", "a", "dict"])
        self._expect_abstain(st.classify_sentiment("wow", CFG), "non-object")
        _patch(lambda t, c, x: (_ for _ in ()).throw(ValueError("empty content from Ollama")))
        self._expect_abstain(st.classify_sentiment("wow", CFG), "classifier unavailable")

    def test_non_numeric_weight_abstains(self):
        self._orig = _patch(lambda t, c, x: {"weight": "very sad", "confidence": 0.9})
        self._expect_abstain(st.classify_sentiment("you forgot", CFG), "non-numeric")
        _patch(lambda t, c, x: {"confidence": 0.9})  # weight missing entirely
        self._expect_abstain(st.classify_sentiment("you forgot", CFG), "non-numeric")


class ScoringUnit(unittest.TestCase):
    def tearDown(self):
        st._ollama_chat = self._orig if hasattr(self, "_orig") else st._ollama_chat

    def test_happy_path_passes_through(self):
        self._orig = _patch(lambda t, c, x: {
            "weight": -0.8, "confidence": 0.85, "reason": "hurt disbelief"})
        got = st.classify_sentiment("you seriously forgot my birthday?", CFG)
        self.assertEqual(got["weight"], -0.8)
        self.assertEqual(got["confidence"], 0.85)
        self.assertEqual(got["reason"], "hurt disbelief")
        self.assertEqual(got["model"], CFG["SENTIMENT_MODEL"])

    def test_out_of_range_values_clamp(self):
        self._orig = _patch(lambda t, c, x: {"weight": -7, "confidence": 3.2, "reason": "r"})
        got = st.classify_sentiment("x", CFG)
        self.assertEqual(got["weight"], -1.0)
        self.assertEqual(got["confidence"], 1.0)
        _patch(lambda t, c, x: {"weight": 2.5, "confidence": -1})
        got = st.classify_sentiment("x", CFG)
        self.assertEqual(got["weight"], 1.0)
        self.assertEqual(got["confidence"], 0.0)

    def test_bad_confidence_defaults_zero_but_weight_survives(self):
        self._orig = _patch(lambda t, c, x: {"weight": -0.5, "confidence": "high"})
        got = st.classify_sentiment("x", CFG)
        self.assertEqual(got["weight"], -0.5)
        self.assertEqual(got["confidence"], 0.0)

    def test_reason_truncated_and_defaulted(self):
        self._orig = _patch(lambda t, c, x: {"weight": 0.2, "confidence": 0.5, "reason": "y" * 999})
        self.assertEqual(len(st.classify_sentiment("x", CFG)["reason"]), 200)
        _patch(lambda t, c, x: {"weight": 0.2, "confidence": 0.5})
        self.assertEqual(st.classify_sentiment("x", CFG)["reason"], "no reason given")


class EnvUnit(unittest.TestCase):
    def test_defaults_without_env_file(self):
        cfg = st.load_env(env_file=None)
        self.assertEqual(cfg["SENTIMENT_MODEL"], st.DEFAULTS["SENTIMENT_MODEL"])
        self.assertEqual(cfg["OLLAMA_URL"], st.DEFAULTS["OLLAMA_URL"])


if __name__ == "__main__":
    unittest.main()
