"""The decoy's only outbound call: local Ollama `/api/chat`. Stdlib only (`urllib`), mirroring
`seneschal/scripts/router.py` and `rag_common.py`'s pattern for talking to Ollama — no httpx, no
`requests`, nothing beyond what `cockpit` extras already ships (fastapi + uvicorn). Kept as its own
module so `server.py`'s route handler is the only importer, and so `test_decoy.py` can monkeypatch
exactly one function (`call_ollama`) without touching FastAPI, threading, or the persona files at all.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

# Shown verbatim — never a stack trace, never an HTTP 5xx with detail — whenever Ollama is
# unreachable, times out, or answers with something unparseable. In-character, per
# seneschal/docs/cockpit-spec.md's "The decoy" section.
OLLAMA_DOWN_LINE = "Reception is closed. Leave no message."


class OllamaUnavailable(Exception):
    """Raised by call_ollama on any transport/parse failure. server.py catches this exactly once and
    substitutes OLLAMA_DOWN_LINE — no other caller should need to inspect the underlying cause."""


def call_ollama(
    *,
    base_url: str,
    model: str,
    system_prompt: str,
    history: list[dict],
    message: str,
    max_output_tokens: int,
    timeout: int,
) -> str:
    """One `/api/chat` call. `history` is a list of already-trimmed ``{"role", "content"}`` dicts
    (oldest first); `message` is the new visitor turn, appended last. Returns the assistant's reply
    text. Raises `OllamaUnavailable` on ANY failure (connection refused, timeout, non-JSON body,
    missing/empty content) — never lets a raw urllib exception or a malformed response escape to the
    route handler."""
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        # num_predict is Ollama's output-token cap — bounds cost/latency regardless of what the
        # visitor asks for; think=False keeps a reasoning-capable model from burning its budget on
        # hidden chain-of-thought for what is, structurally, a short in-character reply.
        "think": False,
        "options": {"num_predict": max_output_tokens},
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaUnavailable(str(exc)) from exc

    content = (data.get("message") or {}).get("content") if isinstance(data, dict) else None
    if not content or not isinstance(content, str):
        raise OllamaUnavailable("empty or malformed content from Ollama")
    return content
