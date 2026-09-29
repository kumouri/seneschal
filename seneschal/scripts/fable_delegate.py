#!/usr/bin/env python3
"""fable_delegate.py — the Fable-5 one-shot delegate. cockpit-spec.md "Model dials & Fable delegation"
(v3). Stdlib only.

The WARM SESSION invokes this via its own tool use when it decides (or is force-routed via `!fable` /
the cockpit's "Send to Fable" toggle) to delegate a turn up. It is a subprocess call-out, not a session
handoff: the warm session always owns the conversation, the persona, and the approval gate — this just
answers one question and hands the text back for the warm session to use (and, per the act-low/ask-high
gate, still draft-and-hold anything outbound or destructive in ITS OWN reply).

**Ceiling enforcement lives HERE, not just in prompts.** Every call re-reads `state/model-config.json`
(via `model_config.py`) and refuses — a clear message on stderr, exit 2 — unless `max_routable_model`
admits Fable-tier. The warm session's grounding tells it to accept a refusal gracefully (say so plainly,
handle the turn itself) rather than treat it as an error to route around.

**Governed by Oikonomos (v3.5, cockpit-spec.md "Oikonomos — the budget governor").** Past the ceiling
check, every call also consults `governor.check("fable_oneshot", ...)` — the daily quota, the
per-conversation quota, delegation concurrency, and (the one place a token budget is actually blockable
in this architecture) Fable's own daily/weekly token budget. A refusal here exits 2 with the SAME
clear-message contract as the ceiling refusal: it names the quota and when it resets, so the warm
session can relay it honestly instead of guessing. The conversation key defaults to the daemon's current
warm-session id (`state/presence-state.json`'s `last_session_id`) when `--conversation-id` isn't given
explicitly — best-effort, `None` (not yet tracked) is a safe absence, never an error. **Fail-open on
governor trouble:** an absent config reads as default quotas (never a refusal by itself), a corrupt
ledger reads as zero spend, and any unexpected exception from the governor call itself is treated as an
allow — a governor bug must never cost a legitimate delegation.

**It meters what it spends.** The one-shot asks the CLI for `--output-format json` and records the
`usage` block it gets back on the `fable_oneshot` ledger row, so Fable — the tightest budget on the board
and the only token budget that hard-blocks — rolls up its real cost (`governor.rollups()` accrues tokens
from any row that carries them, not only `kind: "tokens"` rows). When usage genuinely can't be read, the
row is marked `metered: "unavailable"` (`governor.METERED_UNAVAILABLE`) and carries NO count — a 0 would
be indistinguishable from a free call, and the gate believes what it reads.

Seeds a budget-bounded tail of the daemon's main-chat conversation cache —
`state/telegram-threads/main.json`, with the legacy single-file `state/telegram-thread.json` as a second
rung (the same rolling continuity the daemon seeds a fresh warm session with) — so the one-shot has
enough context to be useful without re-sending the whole conversation. Runs
`claude -p --model <fable id> --output-format json <prompt>` as a subprocess, subscription-billed — the
SAME `ANTHROPIC_API_KEY`-scrubbing rule `presence.py`/`mini_dream.py` apply, so a stray key can never
switch this call to metered API billing. Best-effort appends a `chat.event` to the cockpit transcript
ring buffer (`cockpit_pipe.append_transcript_event`, `model: "claude-fable-5"`) so the cockpit's per-turn
model badge shows the seam — fail-open, a tee failure never turns a good delegate answer into a failure.

Usage:
  python fable_delegate.py "the task/prompt text"
  echo "the task" | python fable_delegate.py
  python fable_delegate.py --state-dir "C:\\...\\seneschal\\state" --model claude-fable-5 "..."

Exit codes: 0 = success (the delegate's answer is on stdout); 2 = refused or failed (message on stderr).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import model_config  # noqa: E402
import governor  # noqa: E402 -- Oikonomos: the fable_oneshot rail gate (v3.5)
import cockpit_pipe  # noqa: E402 -- best-effort transcript badge only; import-safe without websockets

DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
DEFAULT_MODEL = "claude-fable-5"
DEFAULT_TIMEOUT_SEC = 600
THREAD_TAIL_TURNS = 6        # matches presence.py's grounding tail length
THREAD_TAIL_CHAR_BUDGET = 4000  # "budget-bounded" — never let a long thread blow out the one-shot prompt
# The daemon's conversation cache, spelled here rather than imported — this module's standalone rule
# (see `thread_seed`). Both rungs must stay in step with the daemon's per-topic thread cache
# (`THREAD_DIR` / `LEGACY_THREAD_FILE` in presence.py); drifting apart fails SILENTLY — an empty seed,
# no exception, no log line.
THREAD_DIR = "telegram-threads"
LEGACY_THREAD_FILE = "telegram-thread.json"

# Windows: hidden console for the claude -p child, same reasoning as presence.py (a
# console child of a console-less parent otherwise steals focus with a fresh visible window). 0 off Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def child_env() -> dict:
    """Same subscription-billing rule as presence.child_env: scrub
    ANTHROPIC_API_KEY so this call always bills the logged-in Claude subscription, never metered API."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return default


def thread_seed(state_dir: str, max_turns: int = THREAD_TAIL_TURNS,
               char_budget: int = THREAD_TAIL_CHAR_BUDGET) -> str:
    """Recent conversation tail from the daemon's MAIN-CHAT continuity cache — budget-bounded by BOTH
    a turn count and a character cap, so a long-running thread never blows out the one-shot's own
    prompt.

    Deliberately duplicated (not imported from presence.py) so this stays a standalone, dependency-light
    script anyone can read top to bottom — the same own-parsing precedent mini_dream.py sets rather than
    importing the daemon module. Newest-last (chronological), matching presence.thread_tail's shape.

    **It follows the cache's per-topic layout.** Duplicating the path is what makes this module
    standalone, and it is also what would make it a silent orphan: once the daemon migrates the single
    `telegram-thread.json` into `telegram-threads/main.json`, a reader still naming the old path
    returns "" forever with nothing raising and nothing logging — a delegation quietly losing all of its
    context. The legacy path stays as a SECOND rung because a checkout can run this before the daemon
    has migrated, and the two are never both present after a migration.

    **It seeds the main chat only — a named residual, not an oversight.** A delegation is invoked as a
    CLI from inside a turn that may belong to a private-chat topic, and nothing on that invocation
    carries the topic; plumbing one would mean putting a thread id in the warm session's prompt for it
    to copy, i.e. a fact it can get wrong. So a delegation raised inside a topic is seeded with the
    main chat's tail — honest context, just not topic-scoped."""
    thread = _load_json(os.path.join(state_dir, THREAD_DIR, "main.json"), None)
    if thread is None:
        thread = _load_json(os.path.join(state_dir, LEGACY_THREAD_FILE), [])
    if not isinstance(thread, list) or not thread:
        return ""
    lines: list[str] = []
    used = 0
    for t in reversed(thread[-max_turns:]):
        if not isinstance(t, dict):
            continue
        line = f"{t.get('role', '?')}: {t.get('text', '')}"
        if used + len(line) > char_budget:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    lines.reverse()
    return "Recent conversation (for context):\n" + "\n".join(lines) + "\n\n"


def default_conversation_id(state_dir: str) -> str | None:
    """Best-effort conversation key for Oikonomos's per-conversation Fable cap: the daemon's own warm
    session id, read from `state/presence-state.json`'s `last_session_id` (the SAME file
    `presence.save_daemon_state` writes on every enqueue/dequeue) — cockpit-spec.md's model is ONE
    conversation across Telegram/Discord/cockpit alike, and that session id is the closest thing to a
    stable handle for it. Deliberately duplicated (own-parsing precedent, see `thread_seed` above) rather
    than importing presence.py. None (file absent/corrupt, or no session id yet) is a safe, common
    absence — the daily-quota and concurrency checks still apply; only the per-conversation cap is
    skipped."""
    data = _load_json(os.path.join(state_dir, "presence-state.json"), {})
    sid = data.get("last_session_id") if isinstance(data, dict) else None
    return sid if isinstance(sid, str) and sid.strip() else None


def parse_result_json(stdout: str) -> tuple[str | None, dict | None]:
    """Pull (answer_text, usage) out of `claude -p --output-format json`'s single result object.

    Returns `(None, None)` for anything that isn't a well-formed success result — a plain-text reply
    from an older CLI, a truncated line, an `is_error` result. The caller then falls back to treating
    stdout as the answer and records the spend as explicitly UNMETERED rather than as zero."""
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(data, dict) or data.get("is_error"):
        return None, None
    text = data.get("result")
    if not isinstance(text, str):
        return None, None
    usage = data.get("usage")
    return text, usage if isinstance(usage, dict) else None


def run_claude(prompt: str, model: str, claude_bin: str, timeout: int,
               runner=subprocess.run) -> tuple[bool, str, dict | None]:
    """One `claude -p --model <model> --output-format json <prompt>` one-shot. `runner` is swappable
    (tests pass a fake with subprocess.run's signature) so this is testable with NO real `claude` spawn
    and NO network. Returns (ok, text, usage) — `text` is the delegate's answer on success, a short error
    message on failure. Never raises past here.

    **`--output-format json` is what makes this meterable.** The CLI's default `text` mode returns the
    answer and nothing else, which would leave `delegate` no usage to record — and Fable is the one token
    budget that hard-blocks, so it is the single worst place in the system to meter nothing. The json
    mode carries the same `usage` block the daemon's stream-json mode already parses; `usage` is None
    only if that ever stops being true."""
    try:
        proc = runner(
            [claude_bin, "-p", "--model", model, "--output-format", "json", prompt],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=child_env(), creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        return False, f"fable delegate failed to run: {e}", None
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        err = (proc.stderr or "").strip()[:400]
        return False, f"fable delegate exited {proc.returncode}: {err or 'no output'}", None
    text, usage = parse_result_json(out)
    if text is None:
        # Not a parseable success result. Hand back the raw stdout (it may well be the answer, from a
        # CLI without json mode) and let the caller mark the spend unmetered — honest, not zero.
        return True, out, None
    text = text.strip()
    if not text:
        return False, "fable delegate returned an empty result", None
    return True, text, usage


def append_transcript_badge(state_dir: str, text: str, source: str = "cockpit") -> None:
    """Best-effort transcript tee so the cockpit chat pane's per-turn model badge shows the delegation
    seam (cockpit-spec.md: "badged in the transcript ... the seams stay visible"). Fail-open — a broken
    tee must never turn a successful delegate call into a reported failure."""
    try:
        ev = cockpit_pipe.chat_event("assistant_output", source=source, model=DEFAULT_MODEL,
                                     text=text[:4000])
        cockpit_pipe.append_transcript_event(state_dir, ev)
    except Exception:  # noqa: BLE001
        pass


def delegate(task: str, state_dir: str = DEFAULT_STATE_DIR, model: str = DEFAULT_MODEL,
            claude_bin: str = "claude", timeout: int = DEFAULT_TIMEOUT_SEC,
            include_thread: bool = True, runner=subprocess.run,
            conversation_id: str | None = None) -> tuple[bool, str]:
    """The whole delegate call: ceiling check -> governor rail check -> seed context -> claude -p
    one-shot -> ledger + transcript badge.

    Returns (ok, message): on success `message` is the delegate's answer (also badged to the cockpit
    transcript); on failure it's a clear, complete message ready to print/relay as-is — refused-by-ceiling,
    refused-by-governor, and run-failure alike. NEVER raises; the CLI below is the only thing that turns
    `ok` into an exit code.
    """
    cfg = model_config.load(state_dir)
    ceiling = cfg.get("max_routable_model")
    backend = cfg.get("backend") or model_config.DEFAULT_BACKEND
    if backend != "claude-cli":
        # Fable is a Claude-family mechanism with no defined meaning on another backend — refuse
        # cleanly rather than let admits_fable's unconditional False on this backend read as an
        # ordinary ceiling-too-low refusal.
        return False, (
            f"Fable delegation refused: the live warm backend is {backend!r}, not claude-cli. Fable "
            "delegation is a Claude-family mechanism and has no meaning on another backend — switch "
            "the cockpit's backend dial back to claude-cli before any delegation can run, or handle "
            "this turn yourself."
        )
    if not model_config.admits_fable(ceiling, backend=backend):
        return False, (
            f"Fable delegation refused: state/model-config.json's max_routable_model ({ceiling!r}) "
            "doesn't admit Fable-tier. Raise the ceiling in the cockpit's Model dials panel (or "
            "model_config.save()) before any delegation can run — for this turn, handle it yourself."
        )
    conv_id = conversation_id if conversation_id is not None else default_conversation_id(state_dir)
    try:
        verdict = governor.check("fable_oneshot", state_dir, conversation_id=conv_id)
    except Exception:  # noqa: BLE001 -- a governor bug must never block a legitimate delegation
        verdict = governor.Verdict(True, None, {})
    if not verdict.allowed:
        return False, f"Fable delegation refused: {verdict.reason}"

    canon = model_config.canonical(model) or model
    seed = thread_seed(state_dir) if include_thread else ""
    full_prompt = f"{seed}{task}".strip()
    governor.begin_fable_call(state_dir)
    try:
        ok, out, usage = run_claude(full_prompt, canon, claude_bin, timeout, runner=runner)
    finally:
        governor.end_fable_call(state_dir)
    if ok:
        try:
            # The one-shot's REAL usage, or an explicit unmetered marker — never a silent zero. The
            # fable_oneshot gate trusts this number, so "unknown" has to look different from "free".
            if usage is not None:
                governor.append_spend(state_dir, "fable_oneshot", model=canon,
                                      conversation_id=conv_id, usage=usage)
            else:
                governor.append_spend(state_dir, "fable_oneshot", model=canon,
                                      conversation_id=conv_id,
                                      metered=governor.METERED_UNAVAILABLE)
        except Exception:  # noqa: BLE001 -- a ledger-append failure must never turn a good answer into one
            pass
        append_transcript_badge(state_dir, out)
    return ok, out


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="One-shot delegate a task up to Fable-5 (cockpit-spec.md v3).")
    p.add_argument("task", nargs="?", default=None, help="the task/prompt text (else read from stdin)")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    p.add_argument("--model", default=DEFAULT_MODEL, help="the Fable model id/alias to request")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)
    p.add_argument("--no-thread", action="store_true",
                   help="skip seeding the conversation-cache tail (a self-contained task)")
    p.add_argument("--conversation-id", default=None,
                   help="override Oikonomos's per-conversation Fable-quota key (default: the daemon's "
                        "current warm-session id from state/presence-state.json, if any)")
    args = p.parse_args(argv[1:])

    task = args.task
    if task is None:
        task = sys.stdin.read()
    task = (task or "").strip()
    if not task:
        print("fable_delegate: no task text given (positional arg or stdin)", file=sys.stderr)
        return 2

    ok, out = delegate(task, state_dir=args.state_dir, model=args.model, claude_bin=args.claude_bin,
                       timeout=args.timeout, include_thread=not args.no_thread,
                       conversation_id=args.conversation_id)
    if not ok:
        print(out, file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
