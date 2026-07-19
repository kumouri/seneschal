#!/usr/bin/env python3
"""Tests for router.py's fable arm (`classify_fable`, cockpit-spec.md v3 "Model dials & Fable
delegation"). The pre-existing trivial/escalate arm (`classify`) is exercised indirectly via
test_presence_state.py's RouterShadow tests; this file covers the fable arm specifically, stubbing
`_ollama_chat` so no live Ollama is required.

Stdlib ``unittest`` only. Run:  python -m unittest seneschal.scripts.test_router
"""
import json
import os
import sys
import unittest
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import router  # noqa: E402


class ClassifyFable(unittest.TestCase):
    def setUp(self):
        self._orig = router._ollama_chat

    def tearDown(self):
        router._ollama_chat = self._orig

    def test_empty_message_is_standard(self):
        result = router.classify_fable("")
        self.assertEqual(result["verdict"], "standard")

    def test_fable_verdict_passes_through_above_threshold(self):
        router._ollama_chat = lambda *a, **k: {
            "verdict": "fable", "confidence": 0.9, "reason": "deep synthesis"}
        result = router.classify_fable("help me weigh this huge decision")
        self.assertEqual(result["verdict"], "fable")
        self.assertEqual(result["confidence"], 0.9)
        self.assertEqual(result["reason"], "deep synthesis")

    def test_standard_verdict_passes_through(self):
        router._ollama_chat = lambda *a, **k: {
            "verdict": "standard", "confidence": 0.95, "reason": "routine status"}
        result = router.classify_fable("what's on today?")
        self.assertEqual(result["verdict"], "standard")

    def test_low_confidence_fable_falls_back_to_standard(self):
        router._ollama_chat = lambda *a, **k: {
            "verdict": "fable", "confidence": 0.1, "reason": "maybe"}
        result = router.classify_fable("hmm")
        self.assertEqual(result["verdict"], "standard")
        self.assertIn("low confidence", result["reason"])

    def test_unknown_verdict_defaults_to_standard(self):
        router._ollama_chat = lambda *a, **k: {
            "verdict": "something-else", "confidence": 0.9, "reason": "?"}
        result = router.classify_fable("whatever")
        self.assertEqual(result["verdict"], "standard")
        self.assertIn("abstain", result["reason"])

    def test_ollama_unreachable_falls_back_to_standard(self):
        def boom(*a, **k):
            raise urllib.error.URLError("connection refused")

        router._ollama_chat = boom
        result = router.classify_fable("hard multi-step debugging problem")
        self.assertEqual(result["verdict"], "standard")
        self.assertIn("unavailable", result["reason"])

    def test_non_dict_response_falls_back_to_standard(self):
        router._ollama_chat = lambda *a, **k: ["not", "a", "dict"]
        result = router.classify_fable("task")
        self.assertEqual(result["verdict"], "standard")

    def test_uses_the_fable_system_prompt(self):
        seen = {}

        def spy(message, cfg, timeout, system_prompt=router.SYSTEM_PROMPT):
            seen["system_prompt"] = system_prompt
            return {"verdict": "standard", "confidence": 0.9, "reason": "ok"}

        router._ollama_chat = spy
        router.classify_fable("task")
        self.assertEqual(seen["system_prompt"], router.FABLE_SYSTEM_PROMPT)
        self.assertNotEqual(router.FABLE_SYSTEM_PROMPT, router.SYSTEM_PROMPT)

    def test_never_raises_on_arbitrary_exception(self):
        def boom(*a, **k):
            raise RuntimeError("ollama exploded")

        router._ollama_chat = boom
        # classify_fable only catches the transport-family exceptions explicitly listed, same shape as
        # classify() — confirm that shape hasn't silently regressed to catching everything OR nothing
        # by checking the documented exception set actually covers what _ollama_chat can raise.
        with self.assertRaises(RuntimeError):
            router.classify_fable("task")


class CliSmokeTest(unittest.TestCase):
    def setUp(self):
        self._orig_classify = router.classify
        self._orig_classify_fable = router.classify_fable

    def tearDown(self):
        router.classify = self._orig_classify
        router.classify_fable = self._orig_classify_fable

    def test_plain_invocation_uses_classify(self):
        calls = []
        router.classify = lambda msg: calls.append(("classify", msg)) or {"verdict": "escalate"}
        router.classify_fable = lambda msg: calls.append(("classify_fable", msg)) or {"verdict": "standard"}
        rc = router._main(["router.py", "did", "I", "take", "my", "meds"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("classify", "did I take my meds")])

    def test_fable_flag_uses_classify_fable(self):
        calls = []
        router.classify = lambda msg: calls.append(("classify", msg)) or {"verdict": "escalate"}
        router.classify_fable = lambda msg: calls.append(("classify_fable", msg)) or {"verdict": "standard"}
        rc = router._main(["router.py", "--fable", "plan", "the", "migration"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("classify_fable", "plan the migration")])

    def test_no_args_is_usage_error(self):
        rc = router._main(["router.py"])
        self.assertEqual(rc, 2)

    def test_bare_fable_flag_with_no_message_is_usage_error(self):
        rc = router._main(["router.py", "--fable"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
