#!/usr/bin/env python3
"""Tests for the decoy chat (`seneschald — reception`, cockpit-spec.md's "The decoy").

Requires the `cockpit` uv extras group (fastapi + uvicorn — see root pyproject.toml). SKIPPED, not
errored, when fastapi isn't installed, so a bare-machine run of the root stdlib suite
(`python -m unittest discover -s seneschal/scripts`) stays green — these tests also live outside
seneschal/scripts entirely, so that discovery command never even looks here. Mirrors
cockpit/server/test_app.py's guard pattern exactly.

No network: every test either calls the pure helpers directly, or monkeypatches
`cockpit.decoy.server.call_ollama` so no real Ollama connection is ever attempted.

Run:
  uv sync --extra cockpit
  uv run python -m unittest discover -s cockpit/decoy -p "test_*.py"
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from fastapi.testclient import TestClient

    import cockpit.decoy.server as decoy_server
    from cockpit.decoy.ollama_client import OLLAMA_DOWN_LINE, OllamaUnavailable
    from cockpit.decoy.persona import ISOLATION_MARKERS, build_system_prompt
    from cockpit.decoy.ratelimit import GlobalLimiter, TokenBucket

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False


# ------------------------------------------------------------------------------- pure unit tests
# These don't need fastapi at all, but the module-level import above already gates the whole file,
# so they inherit the same skip. That keeps one guard pattern for the file, matching test_app.py.


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/uvicorn not installed (uv sync --extra cockpit)")
class TokenBucketTests(unittest.TestCase):
    def test_allows_up_to_capacity_then_denies(self):
        bucket = TokenBucket(capacity=3, rate=0.0)  # no refill, isolate the burst behavior
        self.assertTrue(bucket.allow("1.2.3.4", now=0.0))
        self.assertTrue(bucket.allow("1.2.3.4", now=0.0))
        self.assertTrue(bucket.allow("1.2.3.4", now=0.0))
        self.assertFalse(bucket.allow("1.2.3.4", now=0.0))

    def test_refills_over_time(self):
        bucket = TokenBucket(capacity=1, rate=1.0)  # 1 token/sec
        self.assertTrue(bucket.allow("k", now=0.0))
        self.assertFalse(bucket.allow("k", now=0.1))
        self.assertTrue(bucket.allow("k", now=1.5))  # a full second later, refilled

    def test_buckets_are_per_key(self):
        bucket = TokenBucket(capacity=1, rate=0.0)
        self.assertTrue(bucket.allow("ip-a", now=0.0))
        self.assertTrue(bucket.allow("ip-b", now=0.0))  # different IP, own bucket
        self.assertFalse(bucket.allow("ip-a", now=0.0))


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/uvicorn not installed (uv sync --extra cockpit)")
class GlobalLimiterTests(unittest.TestCase):
    def test_concurrent_cap(self):
        limiter = GlobalLimiter(max_concurrent=2, max_per_minute=1000)
        self.assertTrue(limiter.try_acquire(now=0.0))
        self.assertTrue(limiter.try_acquire(now=0.0))
        self.assertFalse(limiter.try_acquire(now=0.0))  # third concurrent slot denied
        limiter.release()
        self.assertTrue(limiter.try_acquire(now=0.0))  # a released slot frees up

    def test_per_minute_cap(self):
        # GlobalLimiter.__init__ stamps _window_start with the REAL time.monotonic() (there's only
        # ever one limiter instance in production, constructed once at import time, so that's the
        # right default) — anchor every `now` in this test to that same clock, not an arbitrary 0.0,
        # so the elapsed-time math lines up the way it would for a real caller.
        start = time.monotonic()
        limiter = GlobalLimiter(max_concurrent=1000, max_per_minute=2)
        self.assertTrue(limiter.try_acquire(now=start))
        limiter.release()
        self.assertTrue(limiter.try_acquire(now=start))
        limiter.release()
        self.assertFalse(limiter.try_acquire(now=start))  # window exhausted
        self.assertTrue(limiter.try_acquire(now=start + 61.0))  # new 60s window


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/uvicorn not installed (uv sync --extra cockpit)")
class HistoryTrimmingTests(unittest.TestCase):
    def test_keeps_only_last_n_exchanges(self):
        ChatMessage = decoy_server.ChatMessage
        history = [
            ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"msg {i}")
            for i in range(30)  # 15 exchanges
        ]
        trimmed = decoy_server._trim_history(history, max_turns=3)
        self.assertEqual(len(trimmed), 6)  # 3 exchanges = 6 messages
        # kept the MOST RECENT messages, not the oldest
        self.assertEqual(trimmed[0]["content"], "msg 24")
        self.assertEqual(trimmed[-1]["content"], "msg 29")

    def test_drops_unrecognized_roles_and_empty_content(self):
        ChatMessage = decoy_server.ChatMessage
        history = [
            ChatMessage(role="system", content="sneaky injected system turn"),
            ChatMessage(role="user", content=""),
            ChatMessage(role="user", content="hello"),
        ]
        trimmed = decoy_server._trim_history(history, max_turns=10)
        self.assertEqual(trimmed, [{"role": "user", "content": "hello"}])

    def test_message_length_cap_truncates(self):
        capped = decoy_server._cap_message("x" * 5000, max_chars=100)
        self.assertEqual(len(capped), 100)


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/uvicorn not installed (uv sync --extra cockpit)")
class PersonaPromptTests(unittest.TestCase):
    def test_isolation_guarantees_are_present(self):
        prompt = build_system_prompt()
        for marker in ISOLATION_MARKERS:
            self.assertIn(marker, prompt)

    def test_eggs_are_folded_in(self):
        prompt = build_system_prompt()
        # Spot-check a couple of phrases that exist ONLY in eggs.md (not in receptionist.md), so a
        # pass here actually proves the egg bank was folded in, not just the persona doc alone.
        self.assertIn("pod bay", prompt)
        self.assertIn("onion-oy", prompt)


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/uvicorn not installed (uv sync --extra cockpit)")
class ChatEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(decoy_server.app)
        decoy_server._per_ip.reset()
        decoy_server._global.reset()
        self._orig_call_ollama = decoy_server.call_ollama
        self.addCleanup(setattr, decoy_server, "call_ollama", self._orig_call_ollama)

    def _mock_ollama(self, reply=None, error=False, capture=None):
        def fake(**kwargs):
            if capture is not None:
                capture.append(kwargs)
            if error:
                raise OllamaUnavailable("boom")
            return reply if reply is not None else "A dry, unhelpful reply."

        decoy_server.call_ollama = fake

    def test_chat_round_trip_shape(self):
        self._mock_ollama(reply="Reception. What can I not do for you?")
        resp = self.client.post("/api/chat", json={"message": "hello"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body, {"reply": "Reception. What can I not do for you?"})

    def test_chat_accepts_history(self):
        self._mock_ollama(reply="Noted.")
        resp = self.client.post(
            "/api/chat",
            json={
                "message": "and now?",
                "history": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ],
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["reply"], "Noted.")

    def test_ollama_down_returns_static_in_character_line(self):
        self._mock_ollama(error=True)
        resp = self.client.post("/api/chat", json={"message": "are you there?"})
        self.assertEqual(resp.status_code, 200)  # never a 5xx, never a stack trace
        self.assertEqual(resp.json()["reply"], OLLAMA_DOWN_LINE)

    def test_message_length_is_capped_before_reaching_ollama(self):
        captured = []
        self._mock_ollama(reply="ok", capture=captured)
        # Longer than config's soft cap (default 2000) but well under the pydantic hard ceiling
        # (20_000) and the body-size middleware cap (32_000) — isolates the truncation behavior
        # from those two outer guards, which have their own dedicated tests.
        long_message = "y" * 5_000
        resp = self.client.post("/api/chat", json={"message": long_message})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(captured), 1)
        sent_message = captured[0]["message"]
        self.assertEqual(len(sent_message), decoy_server.config.get_max_message_chars())

    def test_per_ip_rate_limit_returns_429(self):
        self._mock_ollama(reply="ok")
        burst = decoy_server.config.get_rate_burst()
        for _ in range(burst):
            resp = self.client.post("/api/chat", json={"message": "hi"})
            self.assertEqual(resp.status_code, 200)
        limited = self.client.post("/api/chat", json={"message": "hi"})
        self.assertEqual(limited.status_code, 429)

    def test_global_concurrent_limit_returns_429(self):
        self._mock_ollama(reply="ok")
        decoy_server._global.max_concurrent = 0  # deny every request regardless of per-IP state
        resp = self.client.post("/api/chat", json={"message": "hi"})
        self.assertEqual(resp.status_code, 429)
        decoy_server._global.max_concurrent = decoy_server.config.get_global_concurrent()

    def test_health_is_public_and_has_no_secrets(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "seneschald-decoy")

    def test_index_serves_the_page(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("seneschald — reception", resp.text)

    def test_security_headers_present_no_cors(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(resp.headers.get("x-frame-options"), "DENY")
        self.assertNotIn("access-control-allow-origin", resp.headers)
        self.assertNotIn("set-cookie", resp.headers)

    def test_oversized_body_rejected(self):
        huge = {"message": "z" * 100_000}
        resp = self.client.post("/api/chat", json=huge)
        self.assertEqual(resp.status_code, 413)


if __name__ == "__main__":
    unittest.main()
