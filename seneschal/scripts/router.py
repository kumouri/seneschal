#!/usr/bin/env python3
"""The assistant's front-door model router — the **Router advisor**, phase 1 (shadow mode) + the
**fable arm** (v3, cockpit-spec.md "Model dials & Fable delegation"). Stdlib only.

A tiny **local Ollama** classifier (`qwen3.5:4b`) that reads each inbound chat message and decides
*trivial-and-safe* vs *escalate*, BEFORE the warm Opus session spins — the same daemon-cheap-model shape
as Watch. The point (phase 2) is to eventually handle clearly-trivial turns locally (instant, offline,
zero Notion) and escalate everything else to Opus.

**Phase 1 is SHADOW ONLY:** the classifier runs and its verdict is *only logged* — it makes **zero**
behavior change. Every message still escalates to the warm session exactly as today. This gathers the
accuracy evidence to review before phase 2 flips on local handling (the same gated pattern as the
autonomy dial — see `../references/advisor-chain.md`).

**The fable arm (v3)** is a second, independent classifier (`classify_fable`) that decides, among
escalations, **standard vs Fable-level** — whether the warm model would plausibly struggle, or Fable
would clearly do substantially better (deep synthesis, long-horizon planning, hard multi-step
debugging). Unlike the trivial/escalate arm, its "fable" verdict is NOT purely observational — it rides
into the warm session's prompt as a hint line (never a command; see `presence.py`'s `fable_arm_classify`
+ `DaemonState.fable_hints`). The caller (`presence.py`) gates whether this arm runs AT ALL on the live
`max_routable_model` ceiling (`model_config.admits_fable`) — when the ceiling isn't Fable-tier, this
classifier is never invoked, "the fable arm doesn't even run" (cockpit-spec.md ruling 4). Safe fallback
direction is the mirror image of the trivial/escalate arm: **standard** (no delegation), since Fable
calls are the rare/expensive path here, not the safe default.

Design, mirroring `rag_common.py`:
  * Python **standard library only** (`urllib`/`json`) — no pip, no third-party client.
  * Same `load_env()` config pattern (`router.env`, all optional; defaults baked here).
  * Ollama over `urllib`, **graceful failure**: `classify()`/`classify_fable()` NEVER raise to the
    caller. Any problem — Ollama unreachable, bad JSON, unknown verdict, or low confidence — returns the
    **safe fallback** verdict (`escalate`/`other` for `classify`; `standard` for `classify_fable`).
    Abstain ⇒ the safe default for that arm.

CLI (smoke test): ``python router.py "did I take my meds"`` → prints the verdict JSON.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "router.env"

DEFAULTS = {
    "OLLAMA_URL": "http://localhost:11434",
    "ROUTER_MODEL": "qwen3.5:4b",
    "ROUTER_CONF_THRESHOLD": "0.7",
}

# The only categories the classifier may emit. Anything else → escalate/other (safe fallback).
TRIVIAL_CATEGORIES = {"ack", "status", "recall"}
ESCALATE_CATEGORY = "other"


def load_env(env_file: Path | str | None = ENV_FILE) -> dict:
    """Config = DEFAULTS, overridden by router.env (if present), then the process env. Matches
    ``rag_common.load_env`` exactly so both advisors read config the same way."""
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
You are the assistant's front-door message ROUTER. The assistant is the owner's chief of staff. You do NOT reply
to the owner and you do NOT do the task — you only CLASSIFY one inbound chat message so the system can decide
whether a tiny local model can safely handle it or whether it must go to the full assistant.

Return STRICT JSON, exactly these keys and nothing else:
  {"verdict": "trivial" | "escalate",
   "category": "ack" | "status" | "recall" | "other",
   "confidence": <number 0.0-1.0>,
   "reason": "<one short clause>"}

Bias HARD toward "escalate". When in any doubt, escalate. Abstaining means escalating.

TRIVIAL (only these three, and only when unambiguous):
  - "ack": a plain acknowledgement that a reminder/task is done. e.g. "done", "took'em", "did that",
    "already ate", "meds taken". A bare confirmation, nothing to draft or decide.
  - "status": a simple schedule/todo status question answerable from a cached digest.
    e.g. "what's next", "what's on today", "anything left", "am I free this afternoon".
  - "recall": a simple factual recall from history/notes. e.g. "when's my next therapy session",
    "what did I say about the database migration", "what's Dana's email".

ESCALATE (category "other") — the DEFAULT. Escalate anything that is:
  - drafting or outbound: writing/sending/replying to an email, Slack, text, message, calendar reply.
  - any triage of comms.
  - ambiguous, multi-step, or a real request/decision ("can you...", "should I...", "help me...").
  - ask-high: anything touching people, money, identity, health decisions, or commitments.
  - anything where the assistant's VOICE carries the message (tone, persuasion, care).
If it is not clearly one of the three trivial cases, it is "other" → escalate.

Examples:
  "took my meds"                                  -> {"verdict":"trivial","category":"ack","confidence":0.95,"reason":"plain ack of a reminder"}
  "did that, and already ate"                     -> {"verdict":"trivial","category":"ack","confidence":0.9,"reason":"acknowledgement, nothing to act on"}
  "what's on today?"                              -> {"verdict":"trivial","category":"status","confidence":0.9,"reason":"schedule status from digest"}
  "anything left on my list?"                     -> {"verdict":"trivial","category":"status","confidence":0.88,"reason":"todo status question"}
  "when's my next therapy session?"               -> {"verdict":"trivial","category":"recall","confidence":0.85,"reason":"simple factual recall"}
  "draft a reply to Dana declining Thursday"      -> {"verdict":"escalate","category":"other","confidence":0.97,"reason":"outbound drafting"}
  "should I move my 3pm to make room for Sam?"    -> {"verdict":"escalate","category":"other","confidence":0.9,"reason":"multi-step decision"}
  "email the team the new plan"                   -> {"verdict":"escalate","category":"other","confidence":0.96,"reason":"outbound send"}
  "I'm feeling really overwhelmed today"          -> {"verdict":"escalate","category":"other","confidence":0.9,"reason":"needs the assistant's voice/care"}
"""


def _fallback(reason: str, cfg: dict) -> dict:
    """The safe default verdict — always escalate. Used on any failure or abstention."""
    return {
        "verdict": "escalate",
        "category": ESCALATE_CATEGORY,
        "confidence": 0.0,
        "reason": reason,
        "model": cfg.get("ROUTER_MODEL", DEFAULTS["ROUTER_MODEL"]),
    }


def _ollama_chat(message: str, cfg: dict, timeout: int, system_prompt: str = SYSTEM_PROMPT) -> dict:
    """One /api/chat call requesting strict JSON. Raises on any transport/parse trouble — the caller
    (`classify`/`classify_fable`) catches everything and falls back to the safe default for that arm.
    Kept separate so the failure surface is one try/except per caller, matching rag_common's
    OllamaError-then-fallback shape. `system_prompt` is swappable so `classify_fable` reuses this
    exact transport with its own instructions instead of duplicating the HTTP plumbing."""
    url = cfg["OLLAMA_URL"].rstrip("/") + "/api/chat"
    payload = {
        "model": cfg["ROUTER_MODEL"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        "format": "json",   # Ollama constrains the output to valid JSON
        "stream": False,
        # think=False disables qwen3.5's chain-of-thought. This is a fast, deterministic *classification*,
        # not a reasoning task: with thinking on it emits ~5k tokens of scratchpad (~17s); off it's ~3s for
        # the identical verdict. The router runs inline in the daemon on every inbound chat turn, so latency
        # matters — it must never delay a message. Harmless if the model ignores the key (non-reasoning models).
        "think": False,
        "options": {"temperature": 0},  # deterministic classification
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = (data.get("message") or {}).get("content", "")
    if not content:
        raise ValueError("empty content from Ollama")
    return json.loads(content)


def classify(message: str, cfg: dict | None = None, timeout: int = 20) -> dict:
    """Classify one inbound chat message → a verdict dict.

    Returns::

        {"verdict": "trivial"|"escalate", "category": "ack"|"status"|"recall"|"other",
         "confidence": float, "reason": str, "model": str}

    **Never raises.** Conservative / safe-by-default: on Ollama unreachable, JSON parse failure, an
    unknown category, OR confidence below ``ROUTER_CONF_THRESHOLD`` → returns the escalate fallback.
    Escalate is the safe direction (phase 1 escalates everything regardless; the verdict is logged only).
    """
    cfg = cfg or load_env()
    text = (message or "").strip()
    if not text:
        return _fallback("empty message", cfg)

    try:
        threshold = float(cfg.get("ROUTER_CONF_THRESHOLD", DEFAULTS["ROUTER_CONF_THRESHOLD"]))
    except (TypeError, ValueError):
        threshold = float(DEFAULTS["ROUTER_CONF_THRESHOLD"])

    try:
        raw = _ollama_chat(text, cfg, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, json.JSONDecodeError) as exc:
        return _fallback(f"classifier unavailable ({type(exc).__name__})", cfg)

    if not isinstance(raw, dict):
        return _fallback("non-object classifier response", cfg)

    verdict = raw.get("verdict")
    category = raw.get("category")
    reason = str(raw.get("reason", ""))[:200] or "no reason given"
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    model = cfg.get("ROUTER_MODEL", DEFAULTS["ROUTER_MODEL"])

    # Unknown/inconsistent shape → escalate. A "trivial" verdict must carry a known trivial category.
    if verdict == "trivial" and category in TRIVIAL_CATEGORIES:
        if confidence < threshold:
            return {
                "verdict": "escalate", "category": ESCALATE_CATEGORY,
                "confidence": confidence,
                "reason": f"low confidence ({confidence:.2f} < {threshold:.2f}); {reason}",
                "model": model,
            }
        return {
            "verdict": "trivial", "category": category,
            "confidence": confidence, "reason": reason, "model": model,
        }

    # Anything else — explicit escalate, unknown verdict, or a trivial verdict with a bad category.
    if category not in TRIVIAL_CATEGORIES:
        category = ESCALATE_CATEGORY
    return {
        "verdict": "escalate", "category": ESCALATE_CATEGORY,
        "confidence": confidence,
        "reason": reason if verdict == "escalate" else f"abstain⇒escalate; {reason}",
        "model": model,
    }


# --------------------------------------------------------------------------- the fable arm (v3)

FABLE_DEFAULT_VERDICT = "standard"  # the safe/no-delegate direction — the mirror of "escalate" above

FABLE_SYSTEM_PROMPT = """\
You are the assistant's front-door FABLE-DELEGATION router. The assistant is the owner's chief of
staff, running on a capable "warm" model. The owner has ALSO enabled delegation to Fable, an even more
capable model reserved for the hardest turns. You do NOT reply to the owner and you do NOT do the task
— you only decide whether THIS inbound message is a candidate for delegating up to Fable.

Return STRICT JSON, exactly these keys and nothing else:
  {"verdict": "standard" | "fable",
   "confidence": <number 0.0-1.0>,
   "reason": "<one short clause>"}

Bias HARD toward "standard". The warm model handles the overwhelming majority of turns well; only flag
"fable" when the warm model would plausibly struggle, or Fable would clearly do substantially better:
  - deep, multi-source synthesis (weighing many considerations against each other)
  - long-horizon planning (multi-week/month plans, dependency chains)
  - hard, multi-step debugging or architecture/design reasoning
  - genuinely novel or ambiguous problems with no obvious playbook

NOT fable-level (verdict "standard") — the DEFAULT: ordinary chat, status questions, drafting,
scheduling, simple triage, anything routine even if it's "escalate"-tier for the trivial/escalate arm.
Being hard to do QUICKLY is not the same as being hard to do WELL — favor "standard" when unsure.

Examples:
  "what's on today?"
    -> {"verdict":"standard","confidence":0.95,"reason":"routine status"}
  "draft a reply to Dana declining Thursday"
    -> {"verdict":"standard","confidence":0.9,"reason":"routine drafting"}
  "help me think through whether to take the new job offer, weighing comp, growth, and the team"
    -> {"verdict":"fable","confidence":0.85,"reason":"deep multi-factor synthesis"}
  "map out a 6-month plan to migrate the whole stack off the legacy queue, in phases"
    -> {"verdict":"fable","confidence":0.88,"reason":"long-horizon planning"}
  "my daemon keeps dying at 3am and I can't figure out why — walk the whole failure chain with me"
    -> {"verdict":"fable","confidence":0.82,"reason":"hard multi-step debugging"}
"""


def _fable_fallback(reason: str, cfg: dict) -> dict:
    """The safe default verdict for the fable arm — always standard (no delegation)."""
    return {
        "verdict": FABLE_DEFAULT_VERDICT,
        "confidence": 0.0,
        "reason": reason,
        "model": cfg.get("ROUTER_MODEL", DEFAULTS["ROUTER_MODEL"]),
    }


def classify_fable(message: str, cfg: dict | None = None, timeout: int = 20) -> dict:
    """The **fable arm** (v3): classify one inbound ESCALATION → standard vs Fable-level.

    Returns::

        {"verdict": "standard"|"fable", "confidence": float, "reason": str, "model": str}

    **Never raises.** Callers are expected to gate whether this runs at all on
    ``model_config.admits_fable(max_routable_model)`` — this function itself doesn't know about the
    ceiling; it's a pure text classifier, same shape as `classify`. Safe fallback (Ollama unreachable,
    bad JSON, unknown verdict, or confidence below ``ROUTER_CONF_THRESHOLD``) is **"standard"** — the
    mirror of `classify`'s "escalate": here the expensive/rare path is "fable", so uncertainty defaults
    to NOT delegating.
    """
    cfg = cfg or load_env()
    text = (message or "").strip()
    if not text:
        return _fable_fallback("empty message", cfg)

    try:
        threshold = float(cfg.get("ROUTER_CONF_THRESHOLD", DEFAULTS["ROUTER_CONF_THRESHOLD"]))
    except (TypeError, ValueError):
        threshold = float(DEFAULTS["ROUTER_CONF_THRESHOLD"])

    try:
        raw = _ollama_chat(text, cfg, timeout, system_prompt=FABLE_SYSTEM_PROMPT)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, json.JSONDecodeError) as exc:
        return _fable_fallback(f"classifier unavailable ({type(exc).__name__})", cfg)

    if not isinstance(raw, dict):
        return _fable_fallback("non-object classifier response", cfg)

    verdict = raw.get("verdict")
    reason = str(raw.get("reason", ""))[:200] or "no reason given"
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    model = cfg.get("ROUTER_MODEL", DEFAULTS["ROUTER_MODEL"])

    if verdict == "fable":
        if confidence < threshold:
            return {
                "verdict": FABLE_DEFAULT_VERDICT, "confidence": confidence,
                "reason": f"low confidence ({confidence:.2f} < {threshold:.2f}); {reason}",
                "model": model,
            }
        return {"verdict": "fable", "confidence": confidence, "reason": reason, "model": model}

    # Anything else — explicit "standard", an unknown verdict, or a malformed one — all standard.
    return {
        "verdict": FABLE_DEFAULT_VERDICT, "confidence": confidence,
        "reason": reason if verdict == "standard" else f"abstain⇒standard; {reason}",
        "model": model,
    }


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print('usage: python router.py ["--fable"] "<inbound chat message>"', file=sys.stderr)
        return 2
    args = argv[1:]
    fable = args and args[0] == "--fable"
    if fable:
        args = args[1:]
    if not args:
        print('usage: python router.py ["--fable"] "<inbound chat message>"', file=sys.stderr)
        return 2
    message = " ".join(args)
    verdict = classify_fable(message) if fable else classify(message)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
