#!/usr/bin/env python3
"""Offline sentiment cross-check for forgetting events — salience Phase 2. Stdlib only.

A tiny **local Ollama** classifier (the Router's `qwen3.5:4b`) that scores the *emotional weight*
of a recorded reaction to the assistant forgetting something — the second axis of
**salience = frequency × emotional-weight** (`../references/salience.md`; plan §3).

**Division of labor (plan §3.2):** the *source of record* for an event's weight is the warm Opus
session that logged it — only Opus holds the context that makes "you forgot my mom's yahrzeit" a 10
and "you forgot to move my 3pm" a 2. This classifier runs **offline in Dream** over each new event's
bare ``reaction_text`` as an independent second opinion; divergence between the two is a
data-quality flag in the weekly rollup, never an override. The owner's own stated weight always wins.

Design, mirroring `router.py` exactly:
  * Python **standard library only** (`urllib`/`json`) — no pip, no third-party client.
  * Same `load_env()` config pattern (`sentiment.env`, all optional; defaults baked here).
  * Ollama over `urllib`, **graceful failure**: `classify_sentiment()` NEVER raises. Any problem —
    Ollama unreachable, bad JSON, out-of-range weight — returns the **neutral abstain**
    (`weight 0.0, confidence 0.0`). Abstaining is safe because this signal is a cross-check:
    a missing second opinion just means "no flag", it can never delete or protect anything itself.

CLI (smoke test): ``python sentiment.py "you seriously forgot my birthday?"`` → prints the score JSON.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "sentiment.env"

DEFAULTS = {
    "OLLAMA_URL": "http://localhost:11434",
    "SENTIMENT_MODEL": "qwen3.5:4b",
}


def load_env(env_file: Path | str | None = ENV_FILE) -> dict:
    """Config = DEFAULTS, overridden by sentiment.env (if present), then the process env. Matches
    ``router.load_env`` / ``rag_common.load_env`` exactly so every advisor reads config the same way."""
    cfg = dict(DEFAULTS)
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            cfg[key.strip()] = val.strip()
    for key in list(cfg):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


# --------------------------------------------------------------------------- the prompt

SYSTEM_PROMPT = """\
You score the EMOTIONAL WEIGHT of one short reaction to an assistant having forgotten something.
You do not reply and you do not judge who is right — you only score how much the forgetting mattered
emotionally, from the reaction text alone.

Return STRICT JSON, exactly these keys and nothing else:
  {"weight": <number -1.0..1.0>,
   "confidence": <number 0.0..1.0>,
   "reason": "<one short clause>"}

Scale for "weight":
  -1.0 .. -0.7  devastating / a betrayal of trust ("you seriously forgot my mom died?")
  -0.7 .. -0.4  clearly hurt or angry ("you forgot my birthday?")
  -0.4 .. -0.1  mildly annoyed / disappointed ("you forgot again, huh")
   0.0          neutral, purely factual mention, or unscorable from the text alone
  +0.1 .. +1.0  positive (rare): relieved or amused that it was dropped ("ha, glad you forgot that")

Confidence is how sure you are from the BARE TEXT. A short ambiguous string ("wow") is low
confidence. When you cannot tell, use weight 0.0 with low confidence — never guess a strong weight.

Examples:
  "you seriously forgot my birthday?"        -> {"weight": -0.8, "confidence": 0.85, "reason": "hurt disbelief at a personal forget"}
  "you forgot to move my 3pm"                -> {"weight": -0.2, "confidence": 0.7, "reason": "minor logistics annoyance"}
  "lol don't worry about it"                 -> {"weight": 0.1, "confidence": 0.6, "reason": "brushed off lightly"}
  "wow"                                      -> {"weight": -0.3, "confidence": 0.3, "reason": "ambiguous single word"}
"""


def _abstain(reason: str, cfg: dict) -> dict:
    """The neutral fallback — weight 0, confidence 0. Used on any failure or unscorable input."""
    return {
        "weight": 0.0,
        "confidence": 0.0,
        "reason": reason,
        "model": cfg.get("SENTIMENT_MODEL", DEFAULTS["SENTIMENT_MODEL"]),
    }


def _ollama_chat(text: str, cfg: dict, timeout: int) -> dict:
    """One /api/chat call requesting strict JSON. Raises on any transport/parse trouble — the caller
    (`classify_sentiment`) catches everything and abstains, matching router.py's shape."""
    url = cfg["OLLAMA_URL"].rstrip("/") + "/api/chat"
    payload = {
        "model": cfg["SENTIMENT_MODEL"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "format": "json",   # Ollama constrains the output to valid JSON
        "stream": False,
        # A fast scoring call, not a reasoning task — same rationale as router.py.
        "think": False,
        "options": {"temperature": 0},  # deterministic scoring
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = (data.get("message") or {}).get("content", "")
    if not content:
        raise ValueError("empty content from Ollama")
    return json.loads(content)


def classify_sentiment(text: str, cfg: dict | None = None, timeout: int = 20) -> dict:
    """Score one reaction text → a weight dict.

    Returns::

        {"weight": float -1.0..1.0, "confidence": float 0.0..1.0, "reason": str, "model": str}

    **Never raises.** On Ollama unreachable, JSON parse failure, or a non-numeric/out-of-range
    response → the neutral abstain (``weight 0.0, confidence 0.0``). Weights and confidence are
    clamped into range. Abstain is safe: this is a cross-check signal, not the source of record.
    """
    cfg = cfg or load_env()
    text = (text or "").strip()
    if not text:
        return _abstain("empty reaction text", cfg)

    try:
        raw = _ollama_chat(text, cfg, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, json.JSONDecodeError) as exc:
        return _abstain(f"classifier unavailable ({type(exc).__name__})", cfg)

    if not isinstance(raw, dict):
        return _abstain("non-object classifier response", cfg)

    try:
        weight = float(raw.get("weight"))
    except (TypeError, ValueError):
        return _abstain("non-numeric weight", cfg)
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    weight = max(-1.0, min(1.0, weight))
    confidence = max(0.0, min(1.0, confidence))
    reason = str(raw.get("reason", ""))[:200] or "no reason given"

    return {
        "weight": weight,
        "confidence": confidence,
        "reason": reason,
        "model": cfg.get("SENTIMENT_MODEL", DEFAULTS["SENTIMENT_MODEL"]),
    }


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print('usage: python sentiment.py "<reaction text>"', file=sys.stderr)
        return 2
    text = " ".join(argv[1:])
    print(json.dumps(classify_sentiment(text), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
