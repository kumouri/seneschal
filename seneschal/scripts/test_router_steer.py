#!/usr/bin/env python3
"""Tests for `router.classify_steer` — the **steer arm**, the model layer of the mid-turn relevance
gate.

The arm's whole job is precision, so what is pinned here is every path that must come back **hold**
(the deterministic layer is biased to inject; this layer abstains to HOLD), plus the one structural difference
from the two arms beside it: **it classifies a RELATION, so an empty `in_flight` is refused rather
than being answered as if the message stood alone.** A confident answer to "is this related?" with
nothing to be related to is the worst possible failure of a gate whose value is precision.

**No test here makes a real Ollama call** — `router._ollama_chat` is replaced throughout, which is
also what proves the empty-context refusal never reaches the transport.

Run:  python -m unittest test_router_steer
"""
import json
import os
import sys
import unittest
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import router  # noqa: E402

CFG = {"OLLAMA_URL": "http://127.0.0.1:1", "ROUTER_MODEL": "qwen3.5:4b",
       "ROUTER_CONF_THRESHOLD": "0.7"}

IN_FLIGHT = "The owner asked: review the job queue\nTools used so far this turn: Read, Bash"


class _Transport:
    """Stands in for `_ollama_chat`, recording the payload it was handed."""

    def __init__(self, reply=None, raises=None):
        self.reply, self.raises, self.calls, self.payloads = reply, raises, 0, []

    def __call__(self, message, cfg, timeout, system_prompt=None, model=None, keep_alive=None):
        self.calls += 1
        self.payloads.append(message)
        self.system_prompt = system_prompt
        self.model, self.keep_alive, self.timeout = model, keep_alive, timeout
        if self.raises is not None:
            raise self.raises
        return self.reply


class SteerArm(unittest.TestCase):
    def setUp(self):
        self._orig = router._ollama_chat
        self.addCleanup(lambda: setattr(router, "_ollama_chat", self._orig))

    def _run(self, transport, message="also add an applied button", in_flight=IN_FLIGHT):
        router._ollama_chat = transport
        return router.classify_steer(message, in_flight, cfg=dict(CFG))

    # --- the happy path -------------------------------------------------------------------------

    def test_a_confident_steer_is_passed_through(self):
        t = _Transport({"verdict": "steer", "confidence": 0.85, "reason": "adds to the same work"})
        v = self._run(t)
        self.assertEqual(v["verdict"], "steer")
        self.assertEqual(v["confidence"], 0.85)
        self.assertEqual(v["reason"], "adds to the same work")
        self.assertEqual(v["model"], "qwen3.5:4b")

    def test_the_payload_carries_both_halves_of_the_relation(self):
        t = _Transport({"verdict": "hold", "confidence": 0.9, "reason": "unrelated"})
        self._run(t)
        payload = t.payloads[0]
        self.assertIn("WORK IN FLIGHT", payload)
        self.assertIn("review the job queue", payload)
        self.assertIn("THE OWNER'S NEW MESSAGE", payload)
        self.assertIn("also add an applied button", payload)
        self.assertIs(t.system_prompt, router.STEER_SYSTEM_PROMPT)

    # --- everything that must abstain to hold ---------------------------------------------------

    def test_the_safe_fallback_is_hold(self):
        self.assertEqual(router.STEER_DEFAULT_VERDICT, "hold")

    def test_an_empty_in_flight_is_refused_without_touching_the_transport(self):
        t = _Transport({"verdict": "steer", "confidence": 0.99, "reason": "would have been wrong"})
        v = self._run(t, in_flight="   ")
        self.assertEqual(v["verdict"], "hold")
        self.assertEqual(t.calls, 0, "a relation classifier must not run with nothing to relate to")
        self.assertIn("no in-flight context", v["reason"])

    def test_an_empty_message_is_refused_the_same_way(self):
        t = _Transport({"verdict": "steer", "confidence": 0.99, "reason": "n/a"})
        v = self._run(t, message="   ")
        self.assertEqual(v["verdict"], "hold")
        self.assertEqual(t.calls, 0)

    def test_an_unreachable_ollama_holds(self):
        v = self._run(_Transport(raises=urllib.error.URLError("connection refused")))
        self.assertEqual(v["verdict"], "hold")
        self.assertEqual(v["confidence"], 0.0)
        self.assertIn("classifier unavailable", v["reason"])

    def test_a_timeout_holds(self):
        v = self._run(_Transport(raises=TimeoutError("slower than its own timeout")))
        self.assertEqual(v["verdict"], "hold")
        self.assertIn("classifier unavailable", v["reason"])

    def test_bad_json_holds(self):
        v = self._run(_Transport(raises=json.JSONDecodeError("bad", "{", 0)))
        self.assertEqual(v["verdict"], "hold")

    def test_a_non_object_response_holds(self):
        v = self._run(_Transport(["steer"]))
        self.assertEqual(v["verdict"], "hold")

    def test_a_low_confidence_steer_becomes_a_hold_that_says_so(self):
        v = self._run(_Transport({"verdict": "steer", "confidence": 0.4, "reason": "maybe related"}))
        self.assertEqual(v["verdict"], "hold")
        self.assertIn("low confidence", v["reason"])
        self.assertIn("maybe related", v["reason"], "the model's own reason survives the downgrade")

    def test_an_unknown_verdict_holds_and_is_labelled_an_abstention(self):
        v = self._run(_Transport({"verdict": "inject", "confidence": 0.99, "reason": "who knows"}))
        self.assertEqual(v["verdict"], "hold")
        self.assertIn("abstain", v["reason"])

    def test_a_missing_confidence_holds(self):
        v = self._run(_Transport({"verdict": "steer", "reason": "no number"}))
        self.assertEqual(v["verdict"], "hold")

    def test_a_junk_confidence_holds_rather_than_raising(self):
        v = self._run(_Transport({"verdict": "steer", "confidence": "very", "reason": "r"}))
        self.assertEqual(v["verdict"], "hold")
        self.assertEqual(v["confidence"], 0.0)

    def test_an_explicit_hold_keeps_its_own_reason(self):
        v = self._run(_Transport({"verdict": "hold", "confidence": 0.95, "reason": "cats fed"}))
        self.assertEqual((v["verdict"], v["reason"]), ("hold", "cats fed"))

    def test_the_threshold_is_the_routers_own(self):
        # Inherited from ROUTER_CONF_THRESHOLD, not a second knob to drift from it.
        cfg = dict(CFG, ROUTER_CONF_THRESHOLD="0.95")
        router._ollama_chat = _Transport({"verdict": "steer", "confidence": 0.9, "reason": "r"})
        self.assertEqual(router.classify_steer("m", IN_FLIGHT, cfg=cfg)["verdict"], "hold")


class OwnModelTimeoutKeepAlive(unittest.TestCase):
    """The steer arm gets its own model/timeout plus the shared keep-alive, all defaulting to exactly
    the other arms' behaviour when unset."""

    def setUp(self):
        self._orig = router._ollama_chat
        self.addCleanup(lambda: setattr(router, "_ollama_chat", self._orig))

    def _run(self, transport, cfg_over=None, message="also add an applied button",
             in_flight=IN_FLIGHT, timeout=None):
        router._ollama_chat = transport
        cfg = dict(CFG, **(cfg_over or {}))
        return router.classify_steer(message, in_flight, cfg=cfg, timeout=timeout)

    def test_unset_steer_model_falls_back_to_router_model(self):
        t = _Transport({"verdict": "hold", "confidence": 0.9, "reason": "r"})
        v = self._run(t)
        self.assertEqual(t.model, "qwen3.5:4b")
        self.assertEqual(v["model"], "qwen3.5:4b")

    def test_a_configured_steer_model_overrides_router_model_for_this_arm_only(self):
        t = _Transport({"verdict": "hold", "confidence": 0.9, "reason": "r"})
        v = self._run(t, cfg_over={"ROUTER_STEER_MODEL": "small-model:1b"})
        self.assertEqual(t.model, "small-model:1b")
        self.assertEqual(v["model"], "small-model:1b")

    def test_the_steer_model_names_the_fallback_verdict_too(self):
        v = self._run(_Transport(raises=TimeoutError("slow")),
                      cfg_over={"ROUTER_STEER_MODEL": "small-model:1b"})
        self.assertEqual(v["model"], "small-model:1b")

    def test_keep_alive_defaults_to_the_router_env_value(self):
        t = _Transport({"verdict": "hold", "confidence": 0.9, "reason": "r"})
        self._run(t, cfg_over={"ROUTER_KEEP_ALIVE": "45m"})
        self.assertEqual(t.keep_alive, "45m")

    def test_unset_keep_alive_stays_unset(self):
        t = _Transport({"verdict": "hold", "confidence": 0.9, "reason": "r"})
        cfg = {k: v for k, v in CFG.items() if k != "ROUTER_KEEP_ALIVE"}
        router._ollama_chat = t
        router.classify_steer("m", IN_FLIGHT, cfg=cfg)
        self.assertIsNone(t.keep_alive)

    def test_an_explicit_timeout_argument_wins_over_the_env_default(self):
        t = _Transport({"verdict": "hold", "confidence": 0.9, "reason": "r"})
        self._run(t, cfg_over={"ROUTER_STEER_TIMEOUT": "5"}, timeout=99)
        self.assertEqual(t.timeout, 99)

    def test_no_explicit_timeout_reads_router_steer_timeout(self):
        t = _Transport({"verdict": "hold", "confidence": 0.9, "reason": "r"})
        self._run(t, cfg_over={"ROUTER_STEER_TIMEOUT": "7"})
        self.assertEqual(t.timeout, 7)

    def test_no_config_at_all_defaults_to_20(self):
        t = _Transport({"verdict": "hold", "confidence": 0.9, "reason": "r"})
        cfg = {k: v for k, v in CFG.items() if k != "ROUTER_STEER_TIMEOUT"}
        router._ollama_chat = t
        router.classify_steer("m", IN_FLIGHT, cfg=cfg)
        self.assertEqual(t.timeout, 20)

    def test_a_junk_timeout_falls_back_to_20(self):
        t = _Transport({"verdict": "hold", "confidence": 0.9, "reason": "r"})
        self._run(t, cfg_over={"ROUTER_STEER_TIMEOUT": "not a number"})
        self.assertEqual(t.timeout, 20)


class ThePromptIsAboutTheRelation(unittest.TestCase):
    def test_it_names_the_two_verdicts_and_the_bias(self):
        p = router.STEER_SYSTEM_PROMPT
        self.assertIn('"steer"', p)
        self.assertIn('"hold"', p)
        self.assertIn("Bias HARD toward \"hold\"", p)
        self.assertIn("PRECISION MATTERS MORE THAN RECALL", p)


if __name__ == "__main__":
    unittest.main()
