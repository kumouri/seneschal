#!/usr/bin/env python3
"""Mid-turn interleave — the relevance gate, its log, and phase 2's pure helpers. Standard library only.

Spec: `seneschal/docs/mid-turn-interleave-spec.md` (§4.1 Layer A, §4.2 Layer B, §4.3 the observe phase
and the row schema, §5 the phase-2 mechanics, §6 the latency budget, §10 the design decisions). Read it
first — every number and every refusal below comes from there.

A member of the append-only state-log family — `mouth.py` (what the assistant said), `turns.py` (what
was said, both sides), `pending_checks.py` (which claim waits on which job), and this one (**which
mid-turn message the gate would have folded in, and whether its verdict landed in time to matter**).
Same primitive, same never-raises contract, same append-only two-row lifecycle.

## What this is

`state/interleave-log.jsonl` is an append-only record of the relevance gate's verdict on every
message that arrived while the assistant was already answering the previous one. In `observe` — the
daemon's default — **the gate runs and nothing acts on it**: nothing is interrupted, nothing is
reordered, no prompt is altered, and the owner sees exactly what they would see with the gate off.

## Phase 2 (`live`) — built, shipped off

`live` folds a STEER verdict into the running turn: interrupt, then continue with the partial still in
context (spec §3–§5). The daemon owns that loop (`presence.interleave_live_send`); this module holds
its pure pieces — the continuation prompt, §3.8's possibly-landed clause, the MCP-possibly-landed
observability row, and the per-host CLI-version refusal (`refuse_mode`). **The serialization
invariant survives either way**: one consumer, one turn in flight, one reply per delivery.

**There is no fold cap.** `INTERLEAVE_MAX_PER_TURN` does not exist, and §5.1.1 forbids reintroducing it
as a token budget, a time cap, a minimum gap or the `MAX_TURN_ATTEMPTS` counter. `fold_depth` below is
the field that could justify a *new* decision; it is not a bound and must not become one.

## `turn_still_live` is the field the observe phase exists for

A local classifier on a loaded host can take longer than a typical turn. If a verdict typically lands
after the turn it is about has already ended, **phase 2 is not usable on that hardware at all** — and
no design argument substitutes for that number. Everything else here is in service of getting that one
field honestly. The corollary (§6): if it comes back mostly false, the answer is **not** a bigger
timeout. It is a smaller model (`ROUTER_STEER_MODEL`), or the conclusion that this host cannot run a
model-gated interleave. Layer A is unaffected either way.

## Two rows per arrival, and why `arrived_offset_frac` is on the second one

`record_arrival` writes the verdict the moment it lands — `turn_still_live` is a statement about that
instant, and a crash mid-turn must still leave the verdict on disk. `record_resolved` writes a second
row for the same `turn_id` when that turn finishes. These logs are never updated in place; a
resolution is an append, the shape `state/pending-checks.jsonl` already uses.

Two of §4.3's fields are only knowable at turn completion, so they live on the resolution row:
`turn_remaining_sec` and **`arrived_offset_frac`, a fraction of the turn's eventual length** — a
quantity that does not exist while the turn is running. It is written as `null` on the arrival row
rather than dropped, so a reader never has to wonder whether it was measured. `arrival_id` joins the
pair exactly, because one turn can carry several arrivals and `turn_id` alone would not say which.

A verdict that lands **after** its turn ended writes both rows at once, and its `turn_remaining_sec`
is **negative** — the honest number, and the one §6's corollary is read off.

## Layer A may only ever ADD an interleave

The carve-out is `if match: steer` **before** the classifier, never `if no match: hold` after it. A
carve-out that can veto is a second, dumber classifier. Its vocabulary is **frozen** at §4.1's generic
list with **no additions**: a carve-out keyed on one owner's private vocabulary is one more thing to get
wrong for a case (a too-late "stop") that is rare in practice. The one latitude taken is the curly
apostrophe in `don't`, which is the same token as the straight one and not a vocabulary entry.

**The 200-character window is the whole of the anchoring, and its cost is named rather than hidden.**
A directive-shaped word inside the first 200 characters of ordinary prose is a false positive; one
further in is safe only because it falls past the window. That is accepted: Layer A may only add, and
in `observe` it adds precisely nothing.

## Fail-open everywhere

A classifier that times out, an Ollama that is absent, an unwritable log — every one degrades to
today's behaviour silently. `router.classify_steer` already never raises (it returns the `hold`
fallback), and `gate()` catches on top of it, so an absent Ollama still produces a logged row that
records the classifier as unavailable rather than crashing or blocking a turn. `record_arrival` and
`record_resolved` never raise: a failed append costs the row, never the message, the turn, or the
reply. No caller wraps them in a try/except and none should.

## Retention — none, deliberately

A few typed mid-turn arrivals a day × two rows is a few hundred kB a year, and a nightly sweep would
delete exactly the longitudinal evidence the phase-2 decision is read from. `prune` exists and defaults
to `0 = keep everything`; **nothing calls it**, the same shape as `turns.prune`.

USAGE:
  python interleave.py tail --limit 20
  python interleave.py stats
  python interleave.py diagnose              # fail-open by week/reason + latency
  python interleave.py gate "and merge the docs change too, please" \\
      --in-flight "The owner asked: what's on my calendar tomorrow (tools: Read, Bash)"
  python interleave.py prune --days 0        # 0 = keep everything; nothing calls this
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

SCHEMA = "seneschal.interleave/1"
LOG_FILE = "interleave-log.jsonl"

DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

# `--interleave-mode`. `live` is refused per host when the installed CLI version has never been
# probed (`refuse_mode`) — never silently downgraded to `observe`, which is the one outcome worse than
# refusing.
MODE_OFF, MODE_OBSERVE, MODE_LIVE = "off", "observe", "live"
MODES = (MODE_OFF, MODE_OBSERVE, MODE_LIVE)
DEFAULT_MODE = MODE_OBSERVE

KIND_ARRIVAL = "interleave.arrival"
KIND_RESOLVED = "interleave.resolved"
# Phase 2, §3.8: one row per fold that interrupted an MCP tool call in flight — observability only.
# The actual safeguard against a blind retry is `possibly_landed_clause`'s in-context sentence, which
# runs whether or not this append succeeds; this row exists so a later read-out can count how often
# it happens at all, the same way `interleave-log.jsonl` already exists to count everything else here.
KIND_MCP_POSSIBLY_LANDED = "interleave.mcp_possibly_landed"

LAYER_CARVEOUT = "carveout"
LAYER_MODEL = "model"

STEER = "steer"
HOLD = "hold"

# §4.3's `text_preview`, the same 80 characters `router-log.jsonl` keeps. The verbatim text is already
# in `turns.jsonl` (uncapped, by design); a second uncapped copy here would be yet another transcript.
PREVIEW_CHARS = 80

# §4.1: "on the first ~200 characters only". This IS the anchoring — see the module docstring.
CARVEOUT_SCAN_CHARS = 200

# §4.2: the in-flight summary is "the head message's text (capped), plus the tool names seen so far".
IN_FLIGHT_TEXT_CHARS = 400
IN_FLIGHT_MAX_TOOLS = 12

RETENTION_DAYS = 0  # 0 = keep everything. Nothing calls `prune`; see the docstring.

_append_lock = threading.Lock()


# --------------------------------------------------------------------------- paths / stamps

def default_state_dir() -> str:
    return DEFAULT_STATE_DIR


def log_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, LOG_FILE)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_stamp(value) -> datetime | None:
    """Tolerant read of a `ts`. Anything unparseable reads as None, and `prune` keeps such a row —
    a garbled stamp must never be what deletes evidence."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _round(value, places: int = 3):
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- the mode enum

# §3-§3.8: every mechanic phase 2 relies on (the interrupt frame, write-then-interrupt steering, the
# MCP-possibly-landed finding) is UNDOCUMENTED `claude` CLI behaviour, established only by probe and
# explicitly version-pinned: it needs a re-run whenever the CLI version moves. These are the versions
# the spec §3 probe has actually been verified against — §3 at 2.1.226, §3.8's re-run (and the MCP
# finding) at 2.1.280. Add a version here ONLY after re-running the probe at it and confirming
# §3.1-§3.4 and §3.8 still hold; never add one because it "should" still work.
PROBED_CLI_VERSIONS = ("2.1.226", "2.1.280")


def current_cli_version(claude_bin: str = "claude", timeout: int = 15) -> str | None:
    """`claude --version`, parsed down to `X.Y.Z`, or None on any failure — a version probe may never
    raise or guess. Mirrors `usage_probe.read_cli_version`'s shape (the existing convention for
    reading this exact string in this repo); duplicated rather than imported so `off`/`observe`
    callers of this module never pull in `usage_probe` at all."""
    try:
        out = subprocess.run([claude_bin, "--version"], capture_output=True, text=True, timeout=timeout)
        text = out.stdout or out.stderr or ""
    except Exception:  # noqa: BLE001 — a version probe may never raise
        return None
    m = re.search(r"\d+\.\d+\.\d+", text)
    return m.group(0) if m else None


def refuse_mode(mode: str | None, *, version_probe=current_cli_version) -> str | None:
    """Why this `--interleave-mode` cannot be honoured, as a sentence, or None when it can.

    A pure predicate, separate from the parser, so the refusal is testable and says why — the
    pending-checks register's `refusal` shape.

    **`live` is refused, never downgraded, when the installed CLI version has never been probed.**
    §3-§3.8's mechanics are undocumented CLI behaviour, version-pinned by design (they need a re-run
    whenever the CLI version moves). A host whose `claude --version` isn't in `PROBED_CLI_VERSIONS`
    gets refused here automatically — no host-specific config needed, and nothing to remember to flip
    back on an upgrade; re-running the probe and adding the version is what un-refuses it. Whether to
    run `live` at all is the owner's decision (spec §10 D8: read the observe-phase rows first); this
    predicate only refuses what cannot work. `version_probe` is the injected seam (defaults to a real
    `claude --version` subprocess call) so a test never spawns one.

    A caller who asked for a live interleave and silently got observation would believe a feature was
    running that is not, which is strictly worse than not starting — so both grounds refuse, neither
    downgrades."""
    value = (mode or "").strip().lower()
    if value not in MODES:
        return f"unknown --interleave-mode {mode!r}; expected one of {', '.join(MODES)}"
    if value != MODE_LIVE:
        return None
    version = version_probe()
    if version is None:
        return ("--interleave-mode live refused: could not determine the installed claude CLI "
                "version (claude --version failed, timed out, or was unparsable). live has only ever "
                f"been verified at {', '.join(PROBED_CLI_VERSIONS)} (the spec §3/§3.8 probe) "
                "and this host's version can't be confirmed against that list. Fix the CLI invocation, "
                "or re-run the probe at the version this host actually has and add it to "
                "PROBED_CLI_VERSIONS.")
    if version not in PROBED_CLI_VERSIONS:
        return (f"--interleave-mode live refused: this host's claude CLI is v{version}, which "
                f"the interrupt/continue probe has never verified (probed only at "
                f"{', '.join(PROBED_CLI_VERSIONS)}). The interrupt/continue mechanics (spec §3, §3.8) "
                "are undocumented CLI behaviour and version-pinned by design — re-run the spec §3 "
                "probe at this version, confirm §3.1-§3.4 and the MCP-possibly-landed finding "
                "(§3.8) still hold, then add it to PROBED_CLI_VERSIONS before trying live again.")
    return None


# --------------------------------------------------------------------------- Layer A — the carve-out

# §4.1's list, FROZEN (§10 D4) with no additions. Ordered longest-first within each group only so the
# reported match is the most specific one; the verdict does not depend on the order.
CARVEOUT_PHRASES = (
    "scratch that",
    "not that one",
    "wrong one",
    "never mind",
    "nevermind",
    "hold on",
    "hold off",
    "cancel",
    "abort",
    "belay",
    "undo",
    "wait",
    "stop",
)

# `don't` / `do not` + one of these, and nothing else. A bare "don't" is prose.
CARVEOUT_NEGATED_VERBS = ("send", "run", "merge", "post", "push", "delete", "reply", "commit")

_CARVEOUT_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(p) for p in CARVEOUT_PHRASES)
    + r"|(?:don[’']t|do\s+not)\s+(?:"
    + "|".join(CARVEOUT_NEGATED_VERBS)
    + r"))\b",
    re.IGNORECASE,
)


def carveout_match(text: str | None) -> str | None:
    """The matched directive, verbatim, or None. Pure, and the whole of Layer A.

    Word-boundary matched, case-insensitive, **on the first `CARVEOUT_SCAN_CHARS` characters only** —
    that window is the anchoring, and its accepted cost is in the module docstring. Never raises: a
    non-string reads as no match, because a carve-out that can throw is a carve-out that can take a
    turn down, and this layer's one claim to being cheap insurance is that it cannot fail."""
    try:
        window = (text or "")[:CARVEOUT_SCAN_CHARS]
        m = _CARVEOUT_RE.search(window)
        return m.group(0) if m else None
    except Exception:  # noqa: BLE001 — insurance may not become a liability
        return None


# --------------------------------------------------------------------------- Layer B's input

def in_flight_summary(head_text: str | None, tool_names=None) -> str:
    """The `in_flight` half of `router.classify_steer` (§4.2) — a short, bounded description of the
    work the assistant is doing right now, built from things the daemon already has: the head message's text
    (capped) and the tool names seen so far this turn, which `_make_stream_tee` already observes.
    Nothing new has to be instrumented, which is the point."""
    text = (head_text or "").strip().replace("\n", " ")
    if len(text) > IN_FLIGHT_TEXT_CHARS:
        text = text[:IN_FLIGHT_TEXT_CHARS].rstrip() + "…"
    names = []
    for name in (tool_names or []):
        name = str(name or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= IN_FLIGHT_MAX_TOOLS:
            break
    # "The owner asked:" is the exact framing router.STEER_SYSTEM_PROMPT's examples use.
    line = f"The owner asked: {text}" if text else "The owner asked: (no text)"
    return f"{line}\nTools used so far this turn: {', '.join(names) if names else '(none yet)'}"


# --------------------------------------------------------------------------- the gate

def gate(new_message: str, in_flight: str, *, cfg=None, timeout: int | None = None,
         classifier=None) -> dict:
    """Run both layers, cheapest and most certain first, and return the verdict. **Never raises.**

    Returns ``{"layer": "carveout"|"model", "verdict": "steer"|"hold", "confidence": float,
    "reason": str, "model": str|None}``.

    The §4 diagram exactly: Layer A first, and Layer B **only on a total miss**. That ordering is what
    makes Layer A additive-only — it cannot veto a verdict it never sees — and it is also what phase 2
    would do, so what `observe` measures is what would actually have happened.

    `classifier` is the injected seam every test uses; without it this calls `router.classify_steer`,
    which has the same never-raises contract. **`timeout=None` (the default) lets the classifier
    resolve its own** — `router.classify_steer` reads `ROUTER_STEER_TIMEOUT` when its caller doesn't
    override; passing an explicit value here (as the CLI's `--timeout` does) still wins. **Fail-open
    is HOLD** (§10 D3, §6(3)): Ollama unreachable,
    a timeout, bad JSON, an unknown verdict or low confidence all mean today's behaviour. A false hold
    costs a short wait and the next turn still gets the message; a false steer spends a real turn.
    Precision beats recall here, and this is the cheap direction."""
    matched = carveout_match(new_message)
    if matched:
        return {
            "layer": LAYER_CARVEOUT,
            "verdict": STEER,
            # 1.0 is not a model's confidence: a deterministic match either happened or it did not.
            "confidence": 1.0,
            "reason": f"carve-out matched {matched!r} in the first {CARVEOUT_SCAN_CHARS} characters",
            "model": None,
        }
    try:
        if classifier is None:
            import router  # local, stdlib-only; imported lazily so `off` never touches Ollama
            classifier = router.classify_steer
        verdict = classifier(new_message, in_flight, cfg=cfg, timeout=timeout)
        if not isinstance(verdict, dict):
            raise ValueError("non-object classifier response")
    except Exception as exc:  # noqa: BLE001 — a gate that raises is a turn that breaks
        return {
            "layer": LAYER_MODEL,
            "verdict": HOLD,
            "confidence": 0.0,
            "reason": f"classifier unavailable ({type(exc).__name__})",
            "model": None,
        }
    return {
        "layer": LAYER_MODEL,
        "verdict": STEER if verdict.get("verdict") == STEER else HOLD,
        "confidence": _round(verdict.get("confidence"), 3) or 0.0,
        "reason": str(verdict.get("reason") or "no reason given")[:200],
        "model": verdict.get("model"),
    }


# --------------------------------------------------------------------------- writing

def _append(state_dir: str, row: dict) -> bool:
    line = json.dumps(row, ensure_ascii=False) + "\n"
    os.makedirs(state_dir, exist_ok=True)
    with _append_lock:
        with open(log_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(line)
    return True


def new_arrival_id() -> str:
    """The correlator the resolution row names. Twelve hex, matching `presence.py`'s `turn_id` and
    the pending-checks register's row ids — four hex is the birthday problem wearing a disguise, and this
    id is a join key rather than a label."""
    return uuid.uuid4().hex[:12]


def record_arrival(state_dir: str | None = None, *, channel=None, turn_id=None, text=None,
                   layer=None, verdict=None, confidence=None, reason=None,
                   arrived_offset_sec=None, typed=None, fold_depth=None,
                   verdict_latency_sec=None, turn_still_live=None,
                   arrival_id: str | None = None, now: datetime | None = None) -> str | None:
    """Append one `interleave.arrival` row. Returns the `arrival_id` iff it hit disk, else None.

    **Never raises** — the family contract. A full disk, a squatted path, an unserialisable payload
    all cost the row and nothing else: never the message, never the turn, never the reply. Callers do
    not wrap this in a try/except and should not add one.

    Every §4.3 field is written on every row, including on arrivals the gate would not have folded
    anyway (`typed: false`), because a rule you cannot see the cost of is a rule you cannot revisit.
    `arrived_offset_frac` is written as `null` here and carried on the resolution row — it is a
    fraction of the turn's eventual length, which does not exist yet."""
    try:
        state_dir = state_dir or DEFAULT_STATE_DIR
        row_id = str(arrival_id or "").strip() or new_arrival_id()
        row = {
            "schema": SCHEMA,
            "kind": KIND_ARRIVAL,
            "arrival_id": row_id,
            "ts": _stamp(now),
            "channel": str(channel) if channel is not None else None,
            "turn_id": str(turn_id) if turn_id is not None else None,
            "text_preview": (text or "")[:PREVIEW_CHARS],
            "layer": layer,
            "verdict": verdict,
            "confidence": _round(confidence, 3),
            "reason": reason,
            "arrived_offset_sec": _round(arrived_offset_sec, 3),
            "arrived_offset_frac": None,   # resolution-row field; see the docstring
            "typed": bool(typed),
            "fold_depth": int(fold_depth) if fold_depth is not None else None,
            "verdict_latency_sec": _round(verdict_latency_sec, 3),
            "turn_still_live": bool(turn_still_live),
            "turn_remaining_sec": None,    # resolution-row field; §4.3 says so outright
        }
        return row_id if _append(state_dir, row) else None
    except Exception:  # noqa: BLE001 — the row is never worth the turn
        return None


def record_resolved(state_dir: str | None = None, *, arrival_id=None, turn_id=None,
                    turn_remaining_sec=None, arrived_offset_frac=None,
                    turn_total_sec=None, now: datetime | None = None) -> bool:
    """Append the second row for an arrival, once its turn has finished. **Never raises.**

    An append, never a rewrite — the pending-checks register's rule.
    Keyed by `turn_id` as §4.3 asks, and by `arrival_id` as well, because one turn can carry several
    arrivals and `turn_id` alone would not say which one this resolves.

    A **negative** `turn_remaining_sec` is not an error: it is a verdict that landed after its turn
    had already ended, which is the measurement §6's corollary is read off."""
    try:
        state_dir = state_dir or DEFAULT_STATE_DIR
        return _append(state_dir, {
            "schema": SCHEMA,
            "kind": KIND_RESOLVED,
            "arrival_id": str(arrival_id) if arrival_id is not None else None,
            "ts": _stamp(now),
            "turn_id": str(turn_id) if turn_id is not None else None,
            "turn_remaining_sec": _round(turn_remaining_sec, 3),
            "arrived_offset_frac": _round(arrived_offset_frac, 4),
            "turn_total_sec": _round(turn_total_sec, 3),
        })
    except Exception:  # noqa: BLE001
        return False


def record_mcp_possibly_landed(state_dir: str | None = None, *, turn_id=None, tool_name=None,
                               input_preview=None, now: datetime | None = None) -> bool:
    """One row per fold that interrupted an MCP tool call in flight (§3.8). **Never raises** — this is
    observability, not the safeguard; `possibly_landed_clause` below is what actually stops a blind
    retry, and it runs whether or not this append lands."""
    try:
        state_dir = state_dir or DEFAULT_STATE_DIR
        return _append(state_dir, {
            "schema": SCHEMA,
            "kind": KIND_MCP_POSSIBLY_LANDED,
            "ts": _stamp(now),
            "turn_id": str(turn_id) if turn_id is not None else None,
            "tool_name": str(tool_name) if tool_name is not None else None,
            "input_preview": str(input_preview)[:200] if input_preview is not None else None,
        })
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- phase 2 — the continuation

# §5's continuation prompt, verbatim (§10 D6: one clause naming the cut, not a paragraph, not a recital
# of the discarded draft). "All of it" rather than "both" — with no fold cap (§10 D5) the group may be
# more than two messages, so the wording must not imply exactly one.
CONTINUATION_HEADER = (
    "[The owner sent this while you were still answering their previous message — they have not seen "
    "a reply yet. Answer all of it together, in one reply. If you had already started down a path "
    "their new message changes, say so in a clause rather than pretending it didn't happen.]"
)


def possibly_landed_clause(tool_name, input_preview=None) -> str:
    """§3.8's finding, said out loud rather than left implicit: an interrupt reaching an MCP tool call
    in flight does not mean the call was cancelled. The server may have already committed it — a
    `notion-update-page` cut mid-flight is a real outbound write, not a `sleep` — and the interrupted
    turn's own result carries no signal either way (§3.8: "from the client side, cancelled cleanly and
    committed anyway are indistinguishable"). So this never asserts either outcome; it tells the model
    which call was cut and makes verification the model's job before it retries anything that writes.

    This is the smallest mechanism that makes a blind retry loud rather than silent: no new store, no
    new reconciliation loop — the continuation prompt itself carries the uncertainty forward, the same
    place `notion-write-behind-outbox-spec.md`'s journal-then-flush discipline puts an uncertain write
    (never claim it happened without confirming it), extended here to an interrupted call rather than
    the assistant's own queued one."""
    name = tool_name or "a tool call"
    where = f" (input: {input_preview})" if input_preview else ""
    return (f"The {name} call you were making{where} was cut off by this interruption. It may have "
            "already committed on the other end — an interrupt landing mid-call does not mean it was "
            "cancelled, and there is no way to tell from here which happened. Do not assume either "
            "outcome. If it was a write, verify by reading back what it targeted before you retry it; "
            "if it was a read, retrying it plainly is fine.")


def build_continuation_prompt(folded_texts, *, landed_clause: str | None = None) -> str:
    """§5's continuation prompt for N folds (no cap — §5.1.1): the header, then §3.8's possibly-landed
    clause when the interrupt cut an MCP call in flight, then every folded message in arrival order.
    `folded_texts` is never empty in practice — the caller (presence.py's `interleave_live_send`) only
    reaches here after confirming there is at least one fold to continue with."""
    parts = [CONTINUATION_HEADER]
    if landed_clause:
        parts.append(landed_clause)
    parts.append("\n\n".join(str(t) for t in folded_texts))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- reading

def read_rows(state_dir: str | None = None, limit: int | None = None) -> list:
    """Every row, oldest first. A torn or unparseable line is skipped, not fatal — the same tolerance
    the rest of the family reads with."""
    rows = []
    try:
        path = log_path(state_dir)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return rows
    return rows[-limit:] if limit else rows


def _is_fail_open(row: dict) -> bool:
    """Was this MODEL-layer verdict a real judgement, or a timeout/unreachable-Ollama fallback that
    never actually ran? `router.is_fallback_verdict` is the one place that predicate is defined
    (every safe-default path in `router.py` hard-codes `confidence: 0.0`, which a real classification
    essentially never returns on its own) — imported lazily, the same reason `gate()` imports `router`
    lazily: a pure read of an already-written log must never gain an Ollama dependency at import time.
    Without this split a contended host's timeouts land on the MODEL layer looking exactly like real
    verdicts and get counted as ordinary ones (spec §6a)."""
    import router  # local, stdlib-only; see the docstring for why this stays a lazy import
    return router.is_fallback_verdict(row)


def stats(state_dir: str | None = None) -> dict:
    """The observe-phase read-out, in the shape the phase-2 decision (§10 D8) is actually taken on.

    Deliberately abstains rather than extrapolating, `turns.stats`' habit: no verdict-quality claim,
    no accuracy number, and no rate until there is something to divide. **`classified` counts only
    the rows a decision could rest on** — typed arrivals whose verdict came from the model layer —
    because that is the population D8's *"≥ 30 classified mid-turn arrivals"* names.

    **`classified` is split in two.** A classifier timeout/unreachable-Ollama fallback lands on the
    MODEL layer exactly like a real verdict — same `layer: "model"`, same shape — and would otherwise
    be counted toward D8's bar and toward `verdict_landed_in_turn`/`verdict_landed_late` as if the
    model had actually judged something. It hadn't: `confidence == 0.0` on those rows is the fallback
    signal (`router.is_fallback_verdict`), never a genuine low-confidence score.
    `fail_open`/`fail_open_rate` name that population; `classified_real` is `classified` with it
    removed, and **that** is the count D8's bar is read against (`q8_met_on_real_verdicts`) —
    `classified` itself is kept, unchanged in meaning."""
    rows = read_rows(state_dir)
    arrivals = [r for r in rows if r.get("kind") == KIND_ARRIVAL]
    resolved = {r.get("arrival_id"): r for r in rows if r.get("kind") == KIND_RESOLVED}
    typed = [r for r in arrivals if r.get("typed")]
    classified = [r for r in typed if r.get("layer") == LAYER_MODEL]
    fail_open = [r for r in classified if _is_fail_open(r)]
    classified_real = [r for r in classified if not _is_fail_open(r)]
    live = [r for r in classified_real if r.get("turn_still_live")]
    depths: dict = {}
    for r in arrivals:
        if r.get("verdict") == STEER and r.get("fold_depth") is not None:
            key = str(r["fold_depth"])
            depths[key] = depths.get(key, 0) + 1
    firsts = [_parse_stamp(r.get("ts")) for r in arrivals]
    firsts = [f for f in firsts if f]
    q8_target = 30
    return {
        "rows": len(rows),
        "arrivals": len(arrivals),
        "resolved": len([a for a in arrivals if a.get("arrival_id") in resolved]),
        "typed": len(typed),
        "classified": len(classified),
        # Phase 2 (§3.8), observability only — how often a live-mode fold interrupted an MCP call in
        # flight. Zero in `observe`/`off` by construction: nothing here is ever written except from
        # presence.py's live-mode fold path.
        "mcp_possibly_landed": len([r for r in rows if r.get("kind") == KIND_MCP_POSSIBLY_LANDED]),
        "fail_open": len(fail_open),
        "fail_open_rate": (len(fail_open) / len(classified)) if classified else None,
        "classified_real": len(classified_real),
        "by_layer": {
            LAYER_CARVEOUT: len([r for r in arrivals if r.get("layer") == LAYER_CARVEOUT]),
            LAYER_MODEL: len([r for r in arrivals if r.get("layer") == LAYER_MODEL]),
        },
        "by_verdict": {
            STEER: len([r for r in arrivals if r.get("verdict") == STEER]),
            HOLD: len([r for r in arrivals if r.get("verdict") == HOLD]),
        },
        # The field the phase exists for. Reported over CLASSIFIED_REAL rows only: a
        # carve-out verdict is instant by construction and a fail-open never ran at all, so folding
        # either in would flatter the number this decides on.
        "verdict_landed_in_turn": len(live),
        "verdict_landed_late": len(classified_real) - len(live),
        "fold_depth_histogram": depths,
        "first_row": _stamp(min(firsts)) if firsts else None,
        "last_row": _stamp(max(firsts)) if firsts else None,
        # D8's bar, reported rather than judged — meeting it is when the decision gets made, and the
        # decision is the owner's. Read against `classified_real`, not `classified` — see the docstring.
        # (The `q8_` key names are the stable row schema, kept for readers of older read-outs.)
        "q8_classified_target": q8_target,
        "q8_met_on_real_verdicts": len(classified_real) >= q8_target,
    }


def _reason_kind(reason) -> str | None:
    """Which exception a fail-open row's `reason` names — `"classifier unavailable (TimeoutError)"`
    -> `"TimeoutError"` — the by-reason breakdown as reusable code instead of a one-off regex. `None` for a row that isn't a fail-open at all."""
    if not isinstance(reason, str) or "classifier unavailable" not in reason:
        return None
    m = re.search(r"\(([^)]+)\)", reason)
    return m.group(1) if m else "unknown"


def _iso_week(ts) -> str | None:
    """`"YYYY-Www"`-shaped bucket (e.g. `"2026-W37"`), or `None` on an unparseable stamp — the by-week
    table, generalised so it can run again without hand-editing a scratch script."""
    dt = _parse_stamp(ts)
    if dt is None:
        return None
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def _percentile(values: list, pct: float):
    """Linear-interpolated percentile over a list of numbers. `None` on an empty list — the same
    abstain-rather-than-guess posture as `fallback_rate`'s `rate: None`."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def _latency_dist(rows: list) -> dict:
    values = [r["verdict_latency_sec"] for r in rows
              if isinstance(r.get("verdict_latency_sec"), (int, float))]
    return {"n": len(values), "median": _round(_percentile(values, 0.5)),
            "p90": _round(_percentile(values, 0.9))}


def diagnose(state_dir: str | None = None) -> dict:
    """The fail-open diagnosis (**"which model and timeout, what was contending, what the latency
    distribution looked like"**), as reusable code rather than a one-off script. Answers three
    questions over the fail-open population `stats()` separates out:

    1. **When did it happen** — `by_week`, the per-week fallback rate, so a contended week is visible
       without re-deriving it.
    2. **Why** — `fail_open_reasons`, `TimeoutError` vs `URLError` vs anything else, parsed off the
       reason string every `_steer_fallback` already writes.
    3. **What it cost** — `latency_real` vs `latency_fail_open`, median/p90 `verdict_latency_sec` for
       rows that got a genuine verdict against rows that hit the fallback. A fail-open's latency
       clusters near whatever `ROUTER_STEER_TIMEOUT` was AT THE TIME (the wall-clock cost of a timeout
       that never resolves), which is the number the "smaller model / longer keep-alive" fix in
       `router.py` is meant to bring down — not by racing the timeout, but by making the model already
       resident so most calls never approach it.

    Never raises: an unreadable log reads as all-empty, the same tolerance `stats()`/`read_rows()`
    already carry. This does NOT tell you what the daemon was doing at the time (no cross-read of
    `router-log.jsonl`'s triage/fable timestamps, no job list) — that correlation needs the host's
    other logs, not this file alone."""
    rows = read_rows(state_dir)
    arrivals = [r for r in rows if r.get("kind") == KIND_ARRIVAL]
    classified = [r for r in arrivals if r.get("typed") and r.get("layer") == LAYER_MODEL]
    fail_open = [r for r in classified if _is_fail_open(r)]
    real = [r for r in classified if not _is_fail_open(r)]

    by_week: dict = {}
    for r in classified:
        wk = _iso_week(r.get("ts"))
        if wk is None:
            continue
        bucket = by_week.setdefault(wk, {"rows": 0, "fail_open": 0})
        bucket["rows"] += 1
        if _is_fail_open(r):
            bucket["fail_open"] += 1
    weekly = [
        {"week": wk, "rows": b["rows"], "fail_open": b["fail_open"],
         "rate": _round(b["fail_open"] / b["rows"]) if b["rows"] else None}
        for wk, b in sorted(by_week.items())
    ]

    reasons: dict = {}
    for r in fail_open:
        kind = _reason_kind(r.get("reason")) or "unknown"
        reasons[kind] = reasons.get(kind, 0) + 1

    return {
        "classified": len(classified),
        "fail_open": len(fail_open),
        "fail_open_rate": _round(len(fail_open) / len(classified)) if classified else None,
        "fail_open_reasons": reasons,
        "by_week": weekly,
        "latency_real": _latency_dist(real),
        "latency_fail_open": _latency_dist(fail_open),
    }


def prune(state_dir: str | None = None, days: int = RETENTION_DAYS,
          now: datetime | None = None) -> int:
    """Drop rows older than `days`. **`days=0` keeps everything and is the default**, and NOTHING
    calls this — `turns.prune`'s shape, for the reason in the module docstring. A row whose `ts` will
    not parse is kept. Returns the number dropped."""
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return 0
    rows = read_rows(state_dir)
    if not rows:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    keep = []
    dropped = 0
    for row in rows:
        stamp = _parse_stamp(row.get("ts"))
        if stamp is not None and stamp < cutoff:
            dropped += 1
            continue
        keep.append(row)
    if not dropped:
        return 0
    path = log_path(state_dir)
    tmp = path + ".tmp"
    # Build-then-replace, never truncate-write: this directory's house rule, and these files are
    # gitignored so they exist nowhere else.
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in keep:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return dropped


# --------------------------------------------------------------------------- CLI

def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="mid-turn interleave — the relevance gate and its log")
    p.add_argument("--state-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tail", help="the newest rows")
    t.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats", help="the observe-phase read-out")
    sub.add_parser("diagnose", help="the fail-open breakdown — by week, by reason, latency")

    g = sub.add_parser("gate", help="run both layers against one message (smoke test)")
    g.add_argument("message")
    g.add_argument("--in-flight", default="", help="a description of the work in flight")
    g.add_argument("--timeout", type=int, default=None,
                   help="override ROUTER_STEER_TIMEOUT for this smoke test")

    pr_ = sub.add_parser("prune", help="0 = keep everything (the default); nothing calls this")
    pr_.add_argument("--days", type=int, default=RETENTION_DAYS)

    args = p.parse_args(argv[1:])
    if args.cmd == "tail":
        for row in read_rows(args.state_dir, limit=args.limit):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.cmd == "stats":
        print(json.dumps(stats(args.state_dir), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "diagnose":
        print(json.dumps(diagnose(args.state_dir), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "gate":
        print(json.dumps(gate(args.message, args.in_flight, timeout=args.timeout),
                         ensure_ascii=False, indent=2))
        return 0
    dropped = prune(args.state_dir, days=args.days)
    print(f"pruned {dropped} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
