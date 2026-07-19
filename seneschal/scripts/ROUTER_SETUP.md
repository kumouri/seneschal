# Front-door Router — setup (Advisor Chain — Router advisor: phase 1 shadow + the v3 fable arm)

The assistant's **Router advisor** is a **front-door model router**: it runs in the daemon
(`presence.py`), Watch's sibling, and classifies each **inbound chat message** *trivial-and-safe* vs
*escalate* with a small **local Ollama** model (`qwen3.5:4b`) **before** the warm Opus session spins. The
point (phase 2) is to eventually handle clearly-trivial turns locally — instant, offline, zero-Notion —
and escalate everything else to Opus. Full spec: `../references/advisor-chain.md` (Router advisor
section).

As of **v3** (`../docs/cockpit-spec.md` "Model dials & Fable delegation"), the SAME `router.py` also
carries a second, independent classifier — the **fable arm** — that decides, among escalations,
*standard* vs *Fable-level*. See "The fable arm (v3)" below.

It's **free-but-local** (local Ollama + Python **standard library** — no `pip`, mirrors `rag_common.py`)
and **conservative by construction**: `router.classify()` never raises, and any failure or low-confidence
verdict falls back to **escalate** (the safe direction). Abstain ⇒ escalate.

## What phase 1 (shadow) does — and does NOT do

**Phase 1 is SHADOW ONLY.** The classifier runs on every inbound chat message and **only logs** its verdict
to `../state/router-log.jsonl`. It makes **zero behavior change** — every message still escalates to the
warm session exactly as before. There is **no branching on the verdict.** Shadow mode exists purely to
gather accuracy evidence, to be reviewed before phase 2 turns on local handling.

- `--router-mode shadow` (default) — classify + log, then escalate as usual.
- `--router-mode off` — disable the router entirely (no Ollama call, no log).
- `--router-mode live` — **reserved for phase 2; not implemented.** Would let the daemon handle
  clearly-trivial turns locally. Graduating shadow → live is **the owner's call** on the shadow evidence
  (same gated pattern as an ask-high → act-low autonomy graduation).

## One-time setup

1. **Ollama** (already used on this host for the RAG embedder). Pull the classifier model once:
   ```
   ollama pull qwen3.5:4b
   ```
2. **Config is optional.** Defaults (localhost:11434, `qwen3.5:4b`, confidence threshold 0.7) are baked
   into `router.py`. To override, copy the example and edit:
   ```
   cp seneschal/scripts/router.env.example seneschal/scripts/router.env
   ```
   `router.env` is gitignored.
3. **Nothing else.** The daemon defaults to `--router-mode shadow`, so once `presence.py` restarts on the
   merged code it starts logging verdicts automatically. To try it standalone:
   ```
   python seneschal/scripts/router.py "took my meds"
   ```
   prints the verdict JSON — a quick way to sanity-check the classifier.

## The whitelist (what "trivial" means)

The classifier biases **hard toward escalate**; only three narrow cases are trivial:

| category | verdict | what it is | examples |
|----------|---------|-----------|----------|
| `ack` | trivial | plain acknowledgement of a reminder/task | "done", "took'em", "did that", "already ate" |
| `status` | trivial | schedule/todo status from the cached digest | "what's next", "what's on today", "anything left" |
| `recall` | trivial | simple factual recall from history/notes | "when's my next dentist appointment", "what did I say about X" |
| `other` | **escalate (default)** | drafting/outbound, triage, ambiguous/multi-step, ask-high, anything touching people/money/identity, anything where the assistant's *voice* carries the message | "draft a reply to Alex…", "should I move my 3pm?", "I'm overwhelmed today" |

**When unsure, escalate.** Ollama unreachable, bad JSON, an unknown category, or confidence below
`ROUTER_CONF_THRESHOLD` (default 0.7) all → `escalate`/`other`.

## Reading the shadow log

Each inbound message appends one JSON line to `../state/router-log.jsonl` (schema in `../state/README.md`):

```json
{"ts":"2026-07-06T22:34:37Z","channel":"telegram","text_preview":"took my meds","arm":"triage","verdict":"trivial","category":"ack","confidence":0.95,"model":"qwen3.5:4b"}
```

Quick views (from the repo root):

```bash
# tail the latest verdicts
tail -n 20 seneschal/state/router-log.jsonl

# count verdicts by category (needs Python; stdlib only)
python -c "import json,collections;print(collections.Counter(json.loads(l)['category'] for l in open('seneschal/state/router-log.jsonl')))"
```

Review the log periodically: where a `trivial` verdict would have been *wrong* (a message that really needed
Opus) is a **false-trivial** — the dangerous error, since phase 2 would have handled it locally. A `escalate`
on something genuinely trivial is only a missed optimization, not a risk. The whitelist and threshold are
tuned to make false-trivials rare.

## The fable arm (v3, `cockpit-spec.md` "Model dials & Fable delegation")

A second, independent classifier in the same `router.py` — `classify_fable()` — that decides, among
escalations, **standard vs Fable-level**: whether the warm model would plausibly struggle, or Fable would
clearly do substantially better (deep multi-factor synthesis, long-horizon planning, hard multi-step
debugging or architecture reasoning). Wired in by `presence.fable_arm_classify`.

**The ceiling gates whether this arm runs AT ALL.** `fable_arm_classify` reads `state/model-config.json`
live and, if `max_routable_model` isn't Fable-tier (`model_config.admits_fable`), returns immediately —
**no Ollama call, no log row** — "the fable arm doesn't even run" (cockpit-spec.md ruling 4). Once the
ceiling admits Fable, every inbound message is classified and logged (`arm: "fable"`, schema in
`../state/README.md`), alongside the triage arm's `arm: "triage"` rows in the same file.

**Unlike the triage arm, a `"fable"` verdict is not purely observational.** It queues a short hint LINE
(`presence.FABLE_HINT_LINE`) onto `DaemonState.fable_hints`, which `drainer_task` best-effort-attaches to
the next prompt it builds (FIFO, never persisted — if the classification hasn't finished by the time a
turn's prompt is built, that turn just proceeds without a hint; this is advisory only, never a command,
and the warm session's own judgment plus force-route remain independent triggers regardless). The safe
fallback direction is **`"standard"`** (no delegation) — the mirror image of the triage arm's `"escalate"`
default, since a Fable call is the rare/expensive path here, not the safe one.

**Delegation itself** happens via `fable_delegate.py` (a `claude -p --model claude-fable-5` one-shot the
warm session invokes via its own tool use — never a session handoff), triggered by this hint, the warm
session's own judgment, or a force-route (`!fable` prefix / the cockpit's "Send to Fable" toggle — see
`GROUNDING`'s delegation section in `presence.py`). `fable_delegate.py` re-enforces the ceiling itself
(refuses with exit 2 if it doesn't admit Fable) — belt and braces with the arm-gating above.

## The phase-2 plan (how the TRIAGE arm graduates)

Once the shadow log shows the classifier is reliably right on the whitelist — in particular, near-zero
false-trivials — phase 2 adds `--router-mode live`: the daemon handles clearly-trivial turns locally (an
`ack` writes the reminder ack + dequeues the nudge; `status`/`recall` answer from the cached
`context-digest.md`), and escalates everything else to the warm Opus session unchanged. That's the payoff:
instant, offline, zero-Notion trivial turns, and fewer Notion reads overall (a rate-limit win). Flipping
shadow → live is **the owner's decision** on the evidence; a future **Dream** rollup can summarize router
accuracy from `router-log.jsonl` to tee up the call. Live handling is **not** implemented in this phase.
