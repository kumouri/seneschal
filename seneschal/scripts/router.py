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

**The steer arm (mid-turn interleave, observe-only)** is a third, independent classifier
(`classify_steer`) that decides whether a message which arrived **while a turn was already running** is
about the work in flight (`steer`) or is something new that should wait its turn (`hold`). It is the
only arm that classifies a **relation** rather than a message — hence the two-argument signature — and
in its first phase it is **purely observational**: a mid-turn relevance gate (the interleave layer,
wired by the daemon) runs it and logs the verdict, and nothing acts on it. Safe fallback is **hold**,
the status quo: a false steer would cut off the answer the owner is waiting for, while a false hold
only costs a short wait before the message is answered in its own turn.

Design, mirroring `rag_common.py`:
  * Python **standard library only** (`urllib`/`json`) — no pip, no third-party client.
  * Same `load_env()` config pattern (`router.env`, all optional; defaults baked here).
  * Ollama over `urllib`, **graceful failure**: `classify()`/`classify_fable()`/`classify_steer()`
    NEVER raise to the caller. Any problem — Ollama unreachable, bad JSON, unknown verdict, or low
    confidence — returns the **safe fallback** verdict (`escalate`/`other` for `classify`; `standard`
    for `classify_fable`; `hold` for `classify_steer`).
    Abstain ⇒ the safe default for that arm.

CLI (smoke test): ``python router.py "did I take my meds"`` → prints the verdict JSON.

**The fallback-rate check.** Because every arm is fail-safe, a degraded classifier is *silent*: an
Ollama that is up but slower than the per-call timeout under host load produces nothing but fallback
verdicts for hours, and the daemon behaves exactly as if the router were healthy. Every safe-default
path in this module (`_fallback`, `_fable_fallback`, `_steer_fallback`) hard-codes `confidence: 0.0`,
which a real classification essentially never returns on its own (a genuine sub-threshold verdict keeps
its raw, nonzero score — see the "low confidence" branches below) — so `is_fallback_verdict` treats an
exact 0.0 as the fallback signal, and `check_fallback_rate` reads a log of past verdicts and flags a run
whose trailing window is mostly fallback, so a long blind stretch is visible instead of silent. It only
READS logs; it writes no state. CLI:
``python router.py check-fallback-rate --log <path/to/router-log.jsonl>`` (exit 1 on alert).
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
    # The steer arm's own model and timeout, plus a keep-alive shared by every arm — see the "steer
    # arm" section below for why these are the levers, and empty/unset means "behave exactly as
    # before" for each of them.
    "ROUTER_STEER_MODEL": "",       # "" = fall back to ROUTER_MODEL
    "ROUTER_STEER_TIMEOUT": "20",   # the shipped router timeout, as a knob instead of a literal
    "ROUTER_KEEP_ALIVE": "30m",     # Ollama's own default is "5m"; see `_ollama_chat`'s docstring
}

# The only categories the classifier may emit. Anything else → escalate/other (safe fallback).
TRIVIAL_CATEGORIES = {"ack", "status", "recall"}
ESCALATE_CATEGORY = "other"

# The fallback-rate check. A trailing window of at least this many verdicts is required before judging
# anything — one or two fallbacks in a row is normal noise, not a degraded classifier, and judging on
# too little would tune the alarm to noise. Below this, `check_fallback_rate` abstains.
FALLBACK_RATE_MIN_ROWS = 10
# How many of the most recent verdicts to judge — small enough that a real degraded stretch (hours of
# back-to-back fallbacks) dominates the window, large enough not to alarm on 2-3 ordinary fallbacks
# landing close together.
FALLBACK_RATE_DEFAULT_WINDOW = 20
# A degraded stretch runs near 100% fallback; ordinary operation mixes fallbacks with real verdicts
# sparsely enough that even a rough half-and-half trailing window is already abnormal.
FALLBACK_RATE_DEFAULT_THRESHOLD = 0.5


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


def _ollama_chat(message: str, cfg: dict, timeout: int, system_prompt: str = SYSTEM_PROMPT,
                 model: str | None = None, keep_alive: str | None = None) -> dict:
    """One /api/chat call requesting strict JSON. Raises on any transport/parse trouble — the caller
    (`classify`/`classify_fable`/`classify_steer`) catches everything and falls back to the safe
    default for that arm. Kept separate so the failure surface is one try/except per caller, matching
    rag_common's OllamaError-then-fallback shape. `system_prompt` is swappable so `classify_fable` and
    `classify_steer` reuse this exact transport with their own instructions instead of duplicating the
    HTTP plumbing.

    `model` overrides `cfg["ROUTER_MODEL"]` — the steer arm's lever for a smaller/faster model on its
    one call. `keep_alive` is Ollama's own `/api/chat` field controlling how long the model stays
    resident after this call — passed through only when truthy, so a caller that omits it gets
    Ollama's own default (`"5m"`) exactly as before. **Why it matters**: a sparse arm (the steer arm
    fires only on a mid-turn arrival — on the order of once an hour) lands well past Ollama's 5-minute
    idle window between its own calls, so every call is a cold model load unless something else's
    traffic happens to keep the shared model warm, and a cold load under host contention is what
    pushes a call past its timeout into the fallback. Pinning it longer (`ROUTER_KEEP_ALIVE`, default
    `"30m"`) trades idle VRAM for fewer of those cold loads."""
    url = cfg["OLLAMA_URL"].rstrip("/") + "/api/chat"
    payload = {
        "model": model or cfg["ROUTER_MODEL"],
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
    if keep_alive:
        payload["keep_alive"] = keep_alive
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
        raw = _ollama_chat(text, cfg, timeout, keep_alive=cfg.get("ROUTER_KEEP_ALIVE"))
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
        raw = _ollama_chat(text, cfg, timeout, system_prompt=FABLE_SYSTEM_PROMPT,
                           keep_alive=cfg.get("ROUTER_KEEP_ALIVE"))
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


# ------------------------------------------------------------------- the steer arm (interleave B)

STEER_DEFAULT_VERDICT = "hold"  # status quo — the direction every other classifier here abstains in

STEER_SYSTEM_PROMPT = """\
You are the assistant's MID-TURN RELEVANCE gate. The assistant is the owner's chief of staff. The
assistant is ALREADY answering an earlier message from the owner right now, and a NEW message from the
owner has just arrived before that answer was delivered. You do NOT reply and you do NOT do the task —
you decide ONE thing: does the new message change or add to the work already in flight?

Return STRICT JSON, exactly these keys and nothing else:
  {"verdict": "steer" | "hold",
   "confidence": <number 0.0-1.0>,
   "reason": "<one short clause>"}

Bias HARD toward "hold". Holding is the status quo: the message simply waits and is answered in its
own turn a few seconds later. Choosing "steer" cuts a half-written answer off, so a wrong "steer"
costs the owner the answer they are waiting for while a wrong "hold" costs them a short wait.
PRECISION MATTERS MORE THAN RECALL. When in any doubt, hold.

STEER — the new message is ABOUT the work in flight:
  - a correction of a detail in it ("no, the meeting is Thursday, not Tuesday", "it's the staging
    server, not prod")
  - an addition to the same request ("also go through the queue while you're in there", "and merge
    the docs change too")
  - a clarification of what they just asked ("meaning fix it firing at night, not in the morning")
  - a constraint or fact that changes how it should be done ("my laptop just died, so you'll have to
    figure those out without it")
  - permission or a go-ahead for the thing being done ("just want you to run it", "do it")

HOLD — the DEFAULT. Hold anything that is:
  - a new, unrelated question or request ("what's the deadline on the quarterly report?")
  - a status report or life update ("cats fed!", "I'm going to have lunch")
  - a log of something they ate, took, or did ("had a coffee")
  - an acknowledgement with nothing to act on
  - anything you cannot connect to the work in flight with confidence

You are given the work in flight and then the new message. Judge the RELATION between them, not
whether the new message is important on its own.

Examples (work in flight -> new message):
  "The owner asked: what's on my calendar tomorrow" -> "Cats fed!"
    -> {"verdict":"hold","confidence":0.95,"reason":"unrelated status report"}
  "The owner asked: move my dentist appointment to next week" -> "It's with Dr. Lee, not Dr. Park."
    -> {"verdict":"steer","confidence":0.9,"reason":"corrects a detail of the work in flight"}
  "The owner asked: fix the reminder that fires at the wrong time" -> "Meaning fix it firing at night and not in the morning or midday."
    -> {"verdict":"steer","confidence":0.9,"reason":"clarifies the request being worked on"}
  "The owner asked: draft a reply to Dana" -> "What's the deadline on the quarterly report?"
    -> {"verdict":"hold","confidence":0.92,"reason":"a new, unrelated question"}
  "The owner asked: review the job queue" -> "Also add an 'applied' button to the job description pages."
    -> {"verdict":"steer","confidence":0.8,"reason":"adds to the same piece of work"}
"""


def _steer_model(cfg: dict) -> str:
    """The model the steer arm actually calls. `ROUTER_STEER_MODEL` overrides `ROUTER_MODEL` for this
    arm only — the triage/fable arms are untouched. Empty/unset (the default) means byte-for-byte the
    same model as the other arms."""
    return cfg.get("ROUTER_STEER_MODEL") or cfg.get("ROUTER_MODEL", DEFAULTS["ROUTER_MODEL"])


def _steer_timeout(cfg: dict, timeout: int | None) -> int:
    """An explicit `timeout=` argument always wins (test seams, a caller with its own budget).
    Otherwise this reads `ROUTER_STEER_TIMEOUT`, which defaults to the same `20` the other arms use —
    a knob, not a different number, until someone tunes it against the turn's remaining budget. A
    junk value falls back to the default rather than raising."""
    if timeout is not None:
        return timeout
    try:
        return int(cfg.get("ROUTER_STEER_TIMEOUT", DEFAULTS["ROUTER_STEER_TIMEOUT"]))
    except (TypeError, ValueError):
        return int(DEFAULTS["ROUTER_STEER_TIMEOUT"])


def _steer_fallback(reason: str, cfg: dict, model: str | None = None) -> dict:
    """The safe default verdict for the steer arm — always hold (today's behaviour, byte for byte)."""
    return {
        "verdict": STEER_DEFAULT_VERDICT,
        "confidence": 0.0,
        "reason": reason,
        "model": model or _steer_model(cfg),
    }


def classify_steer(new_message: str, in_flight: str, cfg: dict | None = None,
                   timeout: int | None = None) -> dict:
    """The **steer arm** — the model layer of the mid-turn relevance gate. Classify one message that
    arrived while a turn was already running: does it steer the work in flight, or hold?

    Returns::

        {"verdict": "steer"|"hold", "confidence": float, "reason": str, "model": str}

    **Never raises**, like both arms above. Safe fallback is **`hold`** — and that is the *intended*
    direction rather than a grudging default: the gate's deterministic layer (the caller's carve-out
    vocabulary, which runs before this) is biased toward injecting the obvious cases, so everything
    left for this layer is **additive**, where a false steer cuts off the answer the owner is waiting
    for and a false hold costs a short wait with the next turn still getting the message.

    **The one genuine difference from the two arms above, and the reason this is a new arm rather
    than a new prompt: it classifies a RELATION, not a message.** It needs the message *and* a short,
    bounded description of the work in flight (built by the caller from what the daemon already
    has — the turn's prompt and the tools it has used so far). `in_flight` is not optional — an empty
    one is refused to the fallback rather than being classified as if the message stood alone,
    because "is this related?" with nothing to be related to is not a question this prompt can
    answer, and a confident answer to it would be the worst possible failure of a gate whose whole
    job is precision.

    **Own model, timeout and keep-alive.** When a steer fallback happens it is almost always a
    `TimeoutError`/`URLError`, never a real judgement — a sparse arm hitting a cold model under host
    contention. So this arm calls its OWN model (`ROUTER_STEER_MODEL`, empty = `ROUTER_MODEL`) with
    the shared keep-alive (`ROUTER_KEEP_ALIVE`) and its own tunable timeout (`ROUTER_STEER_TIMEOUT`).
    None of the three change the verdict shape or the safe-hold direction — they change how often
    the classifier actually gets to render a verdict before falling back to it."""
    cfg = cfg or load_env()
    text = (new_message or "").strip()
    if not text:
        return _steer_fallback("empty message", cfg)
    context = (in_flight or "").strip()
    if not context:
        return _steer_fallback("no in-flight context to relate it to", cfg)

    try:
        threshold = float(cfg.get("ROUTER_CONF_THRESHOLD", DEFAULTS["ROUTER_CONF_THRESHOLD"]))
    except (TypeError, ValueError):
        threshold = float(DEFAULTS["ROUTER_CONF_THRESHOLD"])

    model = _steer_model(cfg)
    resolved_timeout = _steer_timeout(cfg, timeout)
    payload = f"WORK IN FLIGHT:\n{context}\n\nTHE OWNER'S NEW MESSAGE:\n{text}"
    try:
        raw = _ollama_chat(payload, cfg, resolved_timeout, system_prompt=STEER_SYSTEM_PROMPT,
                           model=model, keep_alive=cfg.get("ROUTER_KEEP_ALIVE"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, json.JSONDecodeError) as exc:
        return _steer_fallback(f"classifier unavailable ({type(exc).__name__})", cfg, model=model)

    if not isinstance(raw, dict):
        return _steer_fallback("non-object classifier response", cfg, model=model)

    verdict = raw.get("verdict")
    reason = str(raw.get("reason", ""))[:200] or "no reason given"
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    if verdict == "steer":
        if confidence < threshold:
            return {
                "verdict": STEER_DEFAULT_VERDICT, "confidence": confidence,
                "reason": f"low confidence ({confidence:.2f} < {threshold:.2f}); {reason}",
                "model": model,
            }
        return {"verdict": "steer", "confidence": confidence, "reason": reason, "model": model}

    # Anything else — explicit "hold", an unknown verdict, or a malformed one — all hold.
    return {
        "verdict": STEER_DEFAULT_VERDICT, "confidence": confidence,
        "reason": reason if verdict == "hold" else f"abstain⇒hold; {reason}",
        "model": model,
    }


# --------------------------------------------------------------------------- fallback-rate check

def is_fallback_verdict(row: dict) -> bool:
    """Did this logged verdict come from a safe-default fallback rather than a real classification?

    `_fallback`, `_fable_fallback` and `_steer_fallback` are the ONLY code paths in this module that
    write `confidence: 0.0` — every real classification, including a sub-threshold "low confidence"
    downgrade, keeps the model's own raw nonzero score (see the `classify*` functions above). So an
    exact 0.0 is the fallback signal, with no new field needed on the verdict logs the daemon already
    writes. Pure and tolerant: a non-dict or missing `confidence` reads as `False`, never raises."""
    if not isinstance(row, dict):
        return False
    return row.get("confidence") == 0.0


def read_verdict_rows(log_path, *, filter_field: str | None = None, filter_value=None) -> list:
    """Tolerant JSONL read of a verdict log (`router-log.jsonl`'s `arm` rows, or a mid-turn gate
    log's arrival rows) — malformed or blank lines skipped, an absent file reads empty, the same
    fail-open posture as every other reader in this tree. Keeps only rows that carry a `confidence`
    key (a resolution/bookkeeping row does not, and must not be counted).

    `filter_field`/`filter_value` narrow to one arm/layer — `router-log.jsonl` mixes `arm: "triage"`
    and `arm: "fable"` rows in one file, and a gate log can mix deterministic `layer: "carveout"`
    matches (not a classifier call) with `layer: "model"` rows."""
    rows = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or "confidence" not in row:
                    continue
                if filter_field is not None and row.get(filter_field) != filter_value:
                    continue
                rows.append(row)
    except OSError:
        return []
    return rows


def fallback_rate(rows: list, window: int | None = FALLBACK_RATE_DEFAULT_WINDOW) -> dict:
    """The fallback fraction over the trailing `window` rows (all of them if `window` is falsy).

    Returns ``{"rows": int, "fallbacks": int, "rate": float|None}`` — `rate` is `None` on zero rows
    (abstain rather than divide by zero)."""
    considered = rows[-window:] if window else list(rows)
    n = len(considered)
    fallbacks = sum(1 for r in considered if is_fallback_verdict(r))
    return {"rows": n, "fallbacks": fallbacks, "rate": (fallbacks / n) if n else None}


def check_fallback_rate(rows: list, *, window: int = FALLBACK_RATE_DEFAULT_WINDOW,
                        threshold: float = FALLBACK_RATE_DEFAULT_THRESHOLD,
                        min_rows: int = FALLBACK_RATE_MIN_ROWS) -> dict:
    """The alert verdict: is the classifier currently degraded into mostly-fallback? Abstains
    (`alert: False`, `rate: None`) below `min_rows` — a real degraded stretch is many rows deep, not
    two, and judging a thin window is how an alarm ends up tuned to noise instead of the real thing.
    Never raises: a bad log reads as no evidence, not a crash.

    Returns ``{"alert": bool, "rate": float|None, "rows": int, "fallbacks": int, "threshold": float,
    "reason": str}`` — the shape the CLI prints as JSON."""
    stats = fallback_rate(rows, window=window)
    if stats["rows"] < min_rows:
        return {
            "alert": False, "rate": None, "rows": stats["rows"], "fallbacks": stats["fallbacks"],
            "threshold": threshold, "reason": f"insufficient data ({stats['rows']} < {min_rows} rows)",
        }
    alert = stats["rate"] >= threshold
    reason = (f"fallback rate {stats['rate']:.2f} >= threshold {threshold:.2f} over "
              f"{stats['rows']} rows" if alert else
              f"fallback rate {stats['rate']:.2f} under threshold {threshold:.2f}")
    return {
        "alert": alert, "rate": stats["rate"], "rows": stats["rows"], "fallbacks": stats["fallbacks"],
        "threshold": threshold, "reason": reason,
    }


# --------------------------------------------------------------------------- CLI

_USAGE = ('usage: python router.py ["--fable" | "--steer <in-flight summary>"] "<inbound chat message>"\n'
          '       python router.py check-fallback-rate --log <path> [--filter-field F --filter-value V] '
          '[--window N] [--threshold T] [--min-rows N]')


def _check_fallback_rate_main(args: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="router.py check-fallback-rate")
    p.add_argument("--log", required=True, help="path to a verdict log, e.g. state/router-log.jsonl")
    p.add_argument("--filter-field", default=None, help='e.g. "arm" or "layer"')
    p.add_argument("--filter-value", default=None, help='e.g. "triage" or "model"')
    p.add_argument("--window", type=int, default=FALLBACK_RATE_DEFAULT_WINDOW)
    p.add_argument("--threshold", type=float, default=FALLBACK_RATE_DEFAULT_THRESHOLD)
    p.add_argument("--min-rows", type=int, default=FALLBACK_RATE_MIN_ROWS)
    ns = p.parse_args(args)
    rows = read_verdict_rows(ns.log, filter_field=ns.filter_field, filter_value=ns.filter_value)
    result = check_fallback_rate(rows, window=ns.window, threshold=ns.threshold, min_rows=ns.min_rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["alert"] else 0


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(_USAGE, file=sys.stderr)
        return 2
    if argv[1] == "check-fallback-rate":
        return _check_fallback_rate_main(argv[2:])
    args = argv[1:]
    fable = args and args[0] == "--fable"
    if fable:
        args = args[1:]
    in_flight = None
    if args and args[0] == "--steer":
        if len(args) < 3:
            print(_USAGE, file=sys.stderr)
            return 2
        in_flight = args[1]
        args = args[2:]
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    message = " ".join(args)
    if in_flight is not None:
        verdict = classify_steer(message, in_flight)
    else:
        verdict = classify_fable(message) if fable else classify(message)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
