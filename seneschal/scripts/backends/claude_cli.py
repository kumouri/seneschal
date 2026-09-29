#!/usr/bin/env python3
"""The claude-cli `Backend` (`../../docs/pluggable-backend-spec.md` §3.1) — the reference
implementation, moved here unchanged in behavior from `presence.py` (Phase 2 of the spec's phase
plan). `presence.py` imports every name below straight back (`from backends.claude_cli import
WarmSession, StubWarmSession, ...`), so `presence.WarmSession`, `presence.TURN_IDLE_GAP_SEC`,
`presence._TurnWatchdog` etc. keep resolving unchanged for the many files (mostly tests) that read
them that way — this move changes WHERE the code lives, not what it does or how it's reached.

`WarmSession` — "One resident `claude` process in stream-json mode = a warm in-RAM session across
turns" — and `StubWarmSession`, the offline test double, are exactly as they were in `presence.py`;
see `seneschal/docs/pluggable-backend-spec.md` §1.1 for the full seam inventory this class embodies
(every flag in `start()`'s argv is Claude-CLI-specific syntax, the stream-json parsing in `send()` is
Claude-CLI's own wire format, etc.) and §3.1 for why this is the `Backend` contract's reference shape
rather than a special case of it.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid

from backends import REPO_ROOT, _NO_WINDOW, _TurnWatchdog

# The idle-gap turn deadline (`../../docs/hung-turn-deadline-spec.md` P1, TURN_IDLE_GAP_SEC=600):
# a hang is SILENT, not slow, and a total cap kills the turn regardless of whether
# real work is still happening — an idle GAP is what actually distinguishes a hang from a long turn.
TURN_IDLE_GAP_SEC = 600.0

# The known-good model the warm session drops to if its configured warm_model won't spawn (see
# WarmSession's spawn-fallback). Matches run-presence.cmd's --model; used only when --model was omitted.
DEFAULT_WARM_FALLBACK_MODEL = "claude-opus-4-8"


def _usage_input_total(u) -> int | None:
    """Sum one usage block's INPUT side (prompt + both cache halves). Output tokens are deliberately
    excluded: this feeds the context gauge, and what fills a context window is what gets sent."""
    if not isinstance(u, dict):
        return None
    total = 0
    found = False
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        val = u.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            total += int(val)
            found = True
    return total if found else None


def context_tokens_from_usage(usage, num_turns=None) -> int | None:
    """Estimate how full the warm session's context is, from the terminal `result` event's usage block.

    **This is a proxy, and the arithmetic is the whole point** (measured against a few hundred live
    turns): the CLI's usage block is **summed across the
    turn's iterations**, not a snapshot of the final call. `usage.iterations` is always a ONE-element
    list holding that same aggregate — it is not per-call detail. So a 41-iteration turn reports ~3.09M
    input tokens; taking that at face value would render the gauge at 1546% full. Dividing by
    `num_turns` recovers the mean per-iteration input, which IS context-sized and tracks cleanly
    (74,443 → 74,679 → 75,395 across consecutive turns of one session).

    Precision, stated honestly:
    - `num_turns == 1` (over half of all turns) → **exact** end-of-turn context.
    - `num_turns > 1` → the **mean** across the turn's iterations, so a **lower bound** on the context
      at the turn's end (context only grows within a turn as tool results land). Undershooting is the
      right failure direction for a gauge; the raw sum overshoots by a factor of `num_turns` — up to
      **41×** in the observed data — which is not.

    If a future CLI ever reports genuine per-iteration detail (`len(iterations) > 1`), the last entry
    is the final call's own usage and needs no division — handled first, below.

    The per-turn metrics row keeps `num_turns` and the raw `usage` alongside this, so a later analysis
    can restrict itself to the exact `num_turns == 1` population instead of inheriting this estimate.
    """
    if not isinstance(usage, dict):
        return None
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and len(iterations) > 1:
        return _usage_input_total(iterations[-1])
    total = _usage_input_total(usage)
    if total is None and isinstance(iterations, list) and iterations:
        total = _usage_input_total(iterations[0])
    if total is None:
        return None
    try:
        n = int(num_turns) if num_turns else 1
    except (TypeError, ValueError):
        n = 1
    return int(total / max(1, n))


class WarmSession:
    """One resident `claude` process in stream-json mode = a warm in-RAM session across turns."""

    cost_model = "subscription"  # the daemon scrubs ANTHROPIC_API_KEY (child_env) so this is always true

    def __init__(self, claude_bin: str, model: str | None, permission_mode: str, log,
                 mcp_configs: list | None = None, fallback_model: str | None = None,
                 resume_session_id: str | None = None):
        self.claude_bin = claude_bin
        self.model = model
        # Step 2a: when set, start() adds `--resume <id>` and the session picks up the previous
        # conversation instead of being re-grounded. Cleared by the first-turn fallback below if the
        # resume doesn't take, so the retry (and every later start()) is a clean cold spawn.
        self.resume_session_id = resume_session_id
        self.resumed = bool(resume_session_id)  # what this session BELIEVES it did, until proven wrong
        self.resume_failed = False              # the resume was tried and didn't take
        # Spawn-fallback: if the FIRST turn on `model` fails before the session ever delivers a good
        # reply, the model string is the prime suspect (a warm dial pointed at a model the CLI won't
        # spawn — e.g. a not-yet-available tier). Re-spawn once on this known-good floor and retry, so a
        # bad warm_model dial can never dark the chat. `resolve_warm_model` only checks a dial is in the
        # capability RANK, not that it's actually spawnable — this is the missing net below it. None (or
        # == model) disables it: nothing safer to fall back to.
        self.fallback_model = fallback_model
        self.did_fallback = False          # this session has already spent its one fallback
        self.fallback_alerted = False      # the drainer has already told the owner about it
        self._delivered_any = False        # a turn has produced a real (non-error) result at least once
        # P1: this turn ended because the idle-gap watchdog killed the child, not because the session
        # died on its own. Reset at the top of every send(); read by the drainer, which owes a hang its
        # own wording and its own respawn reason. Also suppresses the fallback ladder below.
        self.timed_out = False
        # Phase 2 (mid-turn-interleave-spec.md §5 step 2): this turn ended because a live-mode fold
        # decision deliberately interrupted it, not because the session died. Mirrors `timed_out`
        # exactly, including "reset at the top of every send(), read after the loop so a kill/interrupt
        # that races the last line still reports honestly" — the two flags are siblings on purpose, so
        # the fallback ladder below and the drainer's "session died" branch treat a deliberate
        # interrupt exactly like a watchdog kill: a known, named reason to stop, never a real failure.
        self.interrupt_requested = False
        self.turn_gap_sec = TURN_IDLE_GAP_SEC  # the deadline this session reads; per-instance so a test
                                               # can shorten it without touching the module global
        self.permission_mode = permission_mode
        self.log = log
        self.mcp_configs = mcp_configs or []  # Notion + Slack configs threaded into this session
        self.proc: subprocess.Popen | None = None
        self.session_id: str | None = None
        # Called once, with the id, the moment the CLI reports it — session-trace phase 0's
        # `mark_session_opened`. Injected rather than imported so WarmSession stays testable without a
        # state dir, and so a raising callback can never be the reason a session fails to come up.
        self._on_opened = None
        self.api_key_source: str | None = None
        # Observability (warm-session lifetime, Step 1). All of this already arrives in
        # the terminal `result` event every turn and was previously discarded. Written ONLY from
        # _read_until_result — i.e. from the single worker thread that owns this session's stdout for
        # the duration of a turn — and read from the event loop for status snapshots. That's why they
        # are plain scalars: a torn read costs one stale gauge reading, never a broken turn.
        self.started_at: float | None = None    # time.monotonic() at spawn; None until start()
        self.turns_served = 0                   # turns that produced a real (non-error) result
        self.last_usage: dict | None = None     # the last turn's usage block, verbatim
        self.last_num_turns: int | None = None  # that turn's iteration count (the context divisor)
        self.context_tokens: int | None = None  # see context_tokens_from_usage — a PROXY
        self.session_cost_usd: float | None = None  # CUMULATIVE for this session, as the CLI reports it
        self.last_turn_cost_usd: float | None = None  # the delta — what this one turn actually cost
        # The background-task tripwire: harness `task_notification` events seen on this process's stdout. With
        # WARM_SESSION_ENV in force this stays 0; anything else means the rail is not holding.
        self.harness_notifications = 0

    def age_sec(self) -> float | None:
        return None if self.started_at is None else time.monotonic() - self.started_at

    def start(self) -> None:
        # Every counter below is PER-PROCESS, so a spawn-fallback respawn (close() + start()) correctly
        # starts from zero rather than inheriting the dead process's numbers.
        self.started_at = time.monotonic()
        self.turns_served = 0
        self.last_usage = None
        self.last_num_turns = None
        self.context_tokens = None
        self.session_cost_usd = None
        self.last_turn_cost_usd = None
        self.harness_notifications = 0
        # `daemon` marks this as THE warm session — the one surface that speaks in the first
        # person about a cancel. The headless one-shots below keep the `scheduled` default.
        # `_warm_session_env` = that child env (billing safety: never let a stray key force metered
        # API) plus WARM_SESSION_ENV — no harness background tasks in THIS process (see below).
        env = _warm_session_env()
        cmd = [self.claude_bin, "-p",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--verbose",
               "--permission-mode", self.permission_mode]
        if self.mcp_configs:
            cmd += ["--mcp-config", *self.mcp_configs]
        if self.model:
            cmd += ["--model", self.model]
        if self.resume_session_id:
            # Verified: the CLI reports the SAME session_id back, so last_session_id stays stable
            # across a resume and a chain of resumed sessions doesn't fragment the id.
            cmd += ["--resume", self.resume_session_id]
        self.proc = subprocess.Popen(
            cmd, cwd=REPO_ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # Force UTF-8 both ways — claude's stream-json output is UTF-8; without this the daemon
            # decodes it with the Windows ANSI codepage (cp1252) and mangles —, emoji, etc. (mojibake).
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=_NO_WINDOW,
        )

    def send(self, text: str, on_event=None, cold_retry_text: str | None = None) -> str | None:
        """Send one user turn; return the assistant's reply text (or None if the session died). If given,
        `on_event(ev)` is invoked for every parsed stream-json line (including `system`/`result`) —
        the cockpit pipe's transcript tee hooks in here (see presence.drainer_task's `_make_stream_tee`).
        Runs on a worker thread (drainer_task calls this via asyncio.to_thread), so `on_event` must be
        thread-safe; a raising callback is swallowed — the read loop must never die because a tee did.

        **First-turn fallback ladder.** Only the FIRST turn is guarded (via `_delivered_any`): a
        mid-conversation death is a real session drop, not a bad start. Two rungs, cheapest-cause first:

        1. **Resume didn't take** (Step 2a). A stale session id fails in ~3s with `is_error` and no
           reply — the same None this method already sees. The resume is the prime suspect, so drop it
           and respawn cold before blaming the model. `cold_retry_text` is what the caller wants sent
           instead on a cold spawn: the drainer passes the FULL grounding prompt, because a cold
           process needs grounding while a resumed one didn't. Falling back to `text` keeps every
           existing caller correct.
        2. **The model won't spawn.** Re-spawn once on `self.fallback_model` (a known-good floor) and
           retry — a bad warm_model dial must never dark the chat.

        Rung 1 running first matters: after it, the session is already cold, so if rung 2 also fires it
        retries the cold text too, and a bad-dial-plus-stale-id lands on a clean grounded session.

        **Neither rung fires on a watchdog timeout** (P1). Both exist for causes that fail FAST and
        cheap — a stale resume id errors in ~3 s, a model the CLI won't spawn dies immediately — and a
        `TURN_IDLE_GAP_SEC` silence is evidence against both. Re-sending the same prompt would buy two
        more full deadlines of hanging (30 minutes at the default) before the drainer ever learns
        the turn is in trouble, so a timeout goes straight back to the drainer, which owns the retry
        policy and counts it against MAX_TURN_ATTEMPTS.

        **Nor does either rung fire on a deliberate interrupt** (phase 2, §3.6/§3.7's warning made
        concrete): an interrupt landing on a session's very first turn reads, from here, exactly like
        a stale resume or a bad model dial — `is_error`, nothing delivered yet — and without this guard
        the ladder would respawn/retry for the wrong reason. `interrupt_requested` is never set on a
        session's first turn in the live daemon (a fold needs a turn already in flight to fold into),
        but a test or a future caller must not have to know that to get the suppression right."""
        self.timed_out = False
        self.interrupt_requested = False
        reply = self._send_turn(text, on_event)
        if (reply is None and not self.timed_out and not self.interrupt_requested
                and not self._delivered_any and self.resume_session_id):
            self.log(f"! warm session couldn't resume {self.resume_session_id!r} "
                     "(stale/unknown id?); starting cold and re-grounding.")
            self.resume_session_id = None
            self.resumed = False
            self.resume_failed = True
            try:
                self.close()
            except Exception:  # noqa: BLE001 — a close hiccup must not block the cold respawn
                pass
            self.start()
            reply = self._send_turn(cold_retry_text or text, on_event)
        if (reply is None and not self.timed_out and not self.interrupt_requested
                and not self._delivered_any and self._can_fallback()):
            self.log(f"! warm session produced no reply on its first turn with model {self.model!r}; "
                     f"falling back to {self.fallback_model!r} and retrying (bad warm dial?).")
            self.did_fallback = True
            self.model = self.fallback_model
            try:
                self.close()
            except Exception:  # noqa: BLE001 — a close hiccup must not block the fallback respawn
                pass
            self.start()
            reply = self._send_turn(cold_retry_text or text, on_event)
        if reply is not None:
            self._delivered_any = True
        return reply

    def _can_fallback(self) -> bool:
        return (not self.did_fallback and bool(self.fallback_model)
                and self.fallback_model != self.model)

    def send_interrupt(self, request_id: str | None = None) -> bool:
        """Write a `control_request`/`interrupt` frame to the child's stdin (§3.2, probed and
        re-confirmed at §3.8): lands in well under a second, including mid-tool-call, and the process
        and session both survive it — the next ordinary turn runs normally on the same `session_id`.

        Called from the EVENT LOOP thread while a different thread is blocked inside `_read_until_result`
        for the turn this is meant to cut off — safe, because stdin writes and the stdout read loop are
        independent file objects and nothing else ever writes to this stdin concurrently (the drainer's
        own `_send_turn` only writes between reads, never while one is in flight).

        Sets `interrupt_requested = True` unconditionally, even if the write itself fails: a caller that
        asked for an interrupt has committed to treating the next `is_error` as deliberate, and a failed
        write changes nothing about that — the dead man's switch (mid-turn-interleave-spec.md §5) means
        a lost interrupt just lets the turn run to its natural end, the safe direction to fail in either
        way. Never raises."""
        self.interrupt_requested = True
        if not self.proc or self.proc.poll() is not None:
            return False
        frame = {"type": "control_request",
                 "request_id": request_id or f"interleave-{uuid.uuid4().hex[:8]}",
                 "request": {"subtype": "interrupt"}}
        try:
            self.proc.stdin.write(json.dumps(frame) + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def _send_turn(self, text: str, on_event=None) -> str | None:
        """One raw turn against the current process: write the user event, read to the result. Returns
        None if the process is gone or the stream closed without a result (the fallback's trigger)."""
        if not self.proc or self.proc.poll() is not None:
            return None
        line = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return None
        return self._read_until_result(on_event)

    def _read_until_result(self, on_event=None) -> str | None:
        # P1's whole footprint in this method: arm an idle-gap watchdog, beat it on every line, cancel
        # it on the way out. Everything between is untouched, and with no hang the behaviour is
        # byte-identical — the watchdog thread parks on an Event it never gets to.
        watchdog = _TurnWatchdog(self.proc, getattr(self, "turn_gap_sec", TURN_IDLE_GAP_SEC),
                                 self.log).start()
        try:
            return self._read_events(watchdog, on_event)
        finally:
            watchdog.cancel()
            # Read AFTER the loop so a kill that races the last line still reports honestly. The
            # drainer branches on this: a hang gets its own wording and its own respawn reason.
            self.timed_out = watchdog.fired

    def _read_events(self, watchdog, on_event=None) -> str | None:
        for raw in self.proc.stdout:  # blocks line-by-line until this turn's result event
            raw = raw.strip()
            if not raw:
                continue
            watchdog.beat()  # a line arrived: the child is alive, whatever it turns out to say
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "task_notification":
                # The background-task tripwire. WARM_SESSION_ENV makes this unreachable; if it fires anyway
                # (the env was dropped, the CLI changed its flag) the rail is not holding and the next owner turn
                # is at risk of being answered one result late. Logged loudly, NOT acted on: from here the
                # daemon cannot tell whether this notification's turn is queued ahead of the message it
                # just wrote (two results coming) or was folded into the running turn (one result), and
                # waiting for a second result that never comes is a TURN_IDLE_GAP_SEC hang.
                self.harness_notifications += 1
                self.log(f"! warm session got a harness task_notification (task {ev.get('task_id')!r}, "
                         f"status {ev.get('status')!r}) — background tasks are supposed to be disabled in "
                         "this process (WARM_SESSION_ENV); the next reply may run one turn behind")
            # Book the turn's own numbers BEFORE the tee runs. `on_event` is what writes the per-turn
            # metrics row (see _make_stream_tee -> _append_turn_metrics), and it reads this session's
            # counters — so with the tee first, every row described the state BEFORE the turn it was
            # supposed to describe: `turns_served: 0` on a successful first turn, `context_tokens` and
            # the costs either absent or a turn stale.
            if t == "result":
                self._record_turn_usage(ev)
                if not ev.get("is_error"):
                    self.turns_served += 1
            if on_event is not None:
                try:
                    on_event(ev)
                except Exception:  # noqa: BLE001 — a tee failure must never break the turn
                    pass
            if t == "system" and ev.get("subtype") == "init":
                self.session_id = ev.get("session_id")
                if self.session_id and self._on_opened:
                    self._on_opened(self.session_id)
                self.api_key_source = ev.get("apiKeySource")
                if self.api_key_source not in (None, "none"):
                    self.log(f"! WARNING apiKeySource={self.api_key_source!r} — expected 'none' (subscription). "
                             "An API key may be in scope; check billing.")
            elif t == "result":
                if ev.get("is_error"):
                    self.log(f"! claude turn error: {ev.get('result', '')[:200]}")
                    return None
                return ev.get("result", "")
        return None  # stdout closed without a result → session ended

    def _record_turn_usage(self, ev: dict) -> None:
        """Capture the terminal `result` event's observability payload (Step 1). Runs on the stdout
        reader thread, before the error check, so a FAILED turn still records what it cost — those are
        exactly the turns worth seeing. Fail-open: this is a gauge, never a reason to lose a reply."""
        try:
            usage = ev.get("usage")
            num_turns = ev.get("num_turns")
            if isinstance(usage, dict):
                self.last_usage = usage
                self.last_num_turns = num_turns if isinstance(num_turns, int) else None
                ctx = context_tokens_from_usage(usage, num_turns)
                if ctx is not None:
                    self.context_tokens = ctx
            # total_cost_usd is CUMULATIVE per session, so the per-turn cost is the delta. Guard the
            # subtraction against a non-monotonic report (shouldn't happen within one process, but a
            # negative "turn cost" in the ledger would be worse than dropping the delta).
            cost = ev.get("total_cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                prev = self.session_cost_usd
                self.last_turn_cost_usd = round(cost - prev, 6) if isinstance(prev, (int, float)) and cost >= prev else cost
                self.session_cost_usd = float(cost)
        except Exception:  # noqa: BLE001 — metering must never break a turn
            pass

    def close(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.terminate()
        self.proc = None


def _child_env() -> dict:
    """Env for the warm `claude` process: scrub ANTHROPIC_API_KEY so we always bill the subscription
    (CLAUDE_CODE_OAUTH_TOKEN / the logged-in account), never the metered API — `presence.child_env`'s
    own billing-safety scrub, duplicated at the one call site that moved out of that module. Kept as a
    private module-level function (not a parameter) so `WarmSession.start()`'s signature stays
    byte-for-byte identical to before the move; presence.py's own `child_env("daemon")` still exists
    and still serves every OTHER spawn site in that file, unchanged."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env["SENESCHAL_SESSION_SOURCE"] = "daemon"
    return env


# No harness background tasks in the warm session. The rationale and the live probe that verified this
# variable are in `presence.py`'s comment block beside `warm_session_env()`; the constant lives HERE because `WarmSession.start()` (the one spawn it applies to) moved here, and
# `presence.WARM_SESSION_ENV` re-exports this exact dict so there is one source.
WARM_SESSION_ENV = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}


def _warm_session_env() -> dict:
    """`_child_env()` plus the warm-session-only overlay `WARM_SESSION_ENV` (above)."""
    env = _child_env()
    env.update(WARM_SESSION_ENV)
    return env


class StubWarmSession:
    """Offline stand-in: no subprocess, canned replies. For --stub-brain tests."""

    cost_model = "subscription"

    def __init__(self, *_, log=lambda *_: None, resume_session_id=None, **__):
        self.session_id = resume_session_id or "stub-session"
        # Mirrors WarmSession: set by make_session AFTER construction, so it must exist and be None
        # here. Fired in start(), not __init__ — a real session learns its id from the CLI's init
        # event, which happens at start, and the stub exists to exercise the same paths.
        self._on_opened = None
        self.api_key_source = "none"
        self.model = None
        self.turns = 0
        self.closed = False
        self.resume_session_id = resume_session_id
        self.resumed = bool(resume_session_id)
        self.resume_failed = False
        self.timed_out = False  # mirrors WarmSession; a stub never hangs, but the drainer reads it
        self.interrupt_requested = False  # mirrors WarmSession; a stub has no send_interrupt, so this
                                          # never flips, but any defensive getattr() read stays honest
        self.fallback_alerted = False
        # Mirror WarmSession's observability surface so --stub-brain runs exercise the same status /
        # metrics paths (an offline harness that skips them can't catch a regression in them).
        self.started_at = None
        self.turns_served = 0
        self.last_usage = None
        self.last_num_turns = None
        self.context_tokens = None
        self.session_cost_usd = None
        self.last_turn_cost_usd = None

    def age_sec(self) -> float | None:
        return None if self.started_at is None else time.monotonic() - self.started_at

    def start(self) -> None:
        self.closed = False
        self.turns = 0
        if self._on_opened:
            self._on_opened(self.session_id)
        self.started_at = time.monotonic()
        self.turns_served = 0
        self.context_tokens = None
        self.session_cost_usd = None

    def send(self, text: str, on_event=None, cold_retry_text: str | None = None) -> str:
        self.turns += 1
        self.turns_served += 1
        # A COMPLIANT canned reply — opens with a channel declaration (message-routing-spec.md §1) AND
        # a reply-marker line (reply-marker-forcing-function-spec.md §1) so an ordinary --stub-brain run
        # doesn't trip either half of the retry loop by default; this stands in for "a model that
        # follows the instructions", the common case this harness exists to exercise cheaply. The
        # channel line is stripped before reaching any consumer, exactly like the real thing; the
        # marker line is NOT stripped (§2) and rides through to whatever a test reads back, so an
        # existing assertion comparing exact delivered text against this stub would need updating —
        # none in this suite does (they match on substrings or turn numbers, never the full string). A
        # test that wants to exercise the retry/default path builds its own stub, the same pattern
        # RecordingSession already uses for prompt inspection.
        reply = (f"[[channel:main]]\n*(answering turn #{self.turns})*\n"
                 f"[stub reply #{self.turns} to: {text.splitlines()[-1][:50]}]")
        # A canned but SHAPE-ACCURATE result event: a growing cache-read (context accretes across a
        # session) inside a one-element `iterations` list, exactly as the real CLI reports it.
        usage = {"input_tokens": 4, "output_tokens": 120,
                 "cache_creation_input_tokens": 500,
                 "cache_read_input_tokens": 60_000 + 1_000 * self.turns}
        usage["iterations"] = [dict(usage)]
        self.last_usage = usage
        self.last_num_turns = 1
        self.context_tokens = context_tokens_from_usage(usage, 1)
        self.last_turn_cost_usd = 0.04
        self.session_cost_usd = round(0.04 * self.turns, 6)
        if on_event is not None:
            try:  # exercise the tee path in offline/test runs too, harmlessly
                on_event({"type": "result", "is_error": False, "result": reply,
                          "usage": usage, "num_turns": 1, "duration_ms": 1200,
                          "total_cost_usd": self.session_cost_usd})
            except Exception:  # noqa: BLE001
                pass
        return reply

    def close(self) -> None:
        self.closed = True
