#!/usr/bin/env python3
"""The codex-cli `Backend` (`../../docs/pluggable-backend-spec.md` §3.1/§3.5 Phase 2) — the first
second implementation of the seam `claude_cli.py` names: the OpenAI Codex CLI on a ChatGPT
subscription, chosen by a daemon dial (a thin adapter, not a full abstraction rewrite).

**CLI facts this module is built against** (verified empirically against Codex CLI 0.155.x; re-verify
against `codex exec --help` on a Codex upgrade rather than trusting these forever):

  * `codex login status` → `"Logged in using ChatGPT"` — `auth_mode: "chatgpt"` in `~/.codex/auth.json`,
    `OPENAI_API_KEY: null`. The cached-credential-after-one-interactive-login pattern the spec's §1.8/
    §2.1 predicted, matching `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`'s shape exactly.
  * `codex exec --json` emits ONE JSON object per line: `thread.started` (`thread_id`), `turn.started`,
    `item.started`/`item.completed` (`item.type` in `agent_message` (`text`) / `command_execution`
    (`command`, `aggregated_output`, `exit_code`, `status`) / others unverified), `turn.completed`
    (`usage`: `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
    `reasoning_output_tokens` — NO dollar cost field, unlike claude-cli's `total_cost_usd`). Captured
    fixtures (scrubbed of real ids): `test_codex_cli_fixtures/*.jsonl`.
  * `codex exec resume <thread_id> ... <prompt>` ECHOES THE SAME `thread_id` back in its own
    `thread.started` event — verified by a live resume — matching `claude --resume`'s own
    verified same-id-back behavior (`presence.py`'s historical comment at the old `WarmSession.start()`
    call site, `resume_probe.py`). So `resumed`/`resume_failed` mean the same thing here they do in
    `claude_cli.WarmSession`.
  * **`codex exec` READS STDIN WHEN ATTACHED, ALWAYS** — even with nothing piped in, it prints
    `"Reading additional input from stdin..."` to stderr and will hang waiting on an open stdin pipe.
    Every spawn below therefore closes `proc.stdin` IMMEDIATELY after the process starts, never leaves
    it open the way `WarmSession` deliberately does (that one feeds each turn's `user` event through a
    KEPT-OPEN stdin — this backend's process model doesn't have a "next turn" to feed, see below).

## `codex exec` is NOT a long-lived, stdin-driven process — "warm" means something different here

`claude -p --input-format stream-json --output-format stream-json` keeps ONE OS process alive across
every turn of a session, fed new `user` events over a stdin pipe held open the whole time — that is
what `WarmSession.proc` being non-None for the session's lifetime means. `codex exec` has no
equivalent bidirectional mode: **every turn is its own process**, `codex exec [PROMPT]` for the first
turn and `codex exec resume <thread_id> [PROMPT]` for every turn after, each one running to
completion and exiting. So for `CodexWarmSession`, "warm" means "resumable via a stable thread id
across turns", not "one held-open OS process" — `self.proc` here is set only WHILE a turn's
subprocess is actually running and is `None` between turns, unlike `WarmSession.proc`. This is a
process-model difference the spec flagged as needing its own characterization (§2.1: "the exact
event vocabulary... was not enumerated in this pass") rather than an oversight in this file.

## Safety: no MCP grant is reachable from this class at all — see `backends/__init__.py`'s module
docstring for the full reasoning. Concretely: `mcp_configs` is accepted (for constructor-signature
parity with `WarmSession`, so `presence.make_session()` can build either backend with the same call
shape) and then never referenced again — no `-c mcp_servers...` override, no `~/.codex/config.toml`
write, nothing. Codex's MCP servers come ONLY from that standing host file, written ONLY by
`CODEX_SETUP.md`'s host-side step, which grants the store's MCP only, never an outbound-send server.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import cockpit_pipe  # the chat.event shape every backend must feed (see build_chat_event_from_stream)
from backends import REPO_ROOT, _NO_WINDOW, _TurnWatchdog

# Same default as claude_cli.TURN_IDLE_GAP_SEC (a hang is silent, not slow — hung-turn-deadline-spec.md
# P1) — a separate constant, not an import, because each backend owns its own dial per the spec's own
# framing (§1.1: "would need re-deriving, not re-using, for another CLI's own failure shapes") even
# though the STARTING value is identical until this backend's real hang behavior is characterized.
CODEX_TURN_GAP_SEC = 600.0

# `--permission-mode` (claude-cli) has no codex-cli equivalent flag; codex's own two axes are
# `--sandbox {read-only,workspace-write,danger-full-access}` (what the model may touch) and
# `-a/--ask-for-approval {on-request,never}` (whether a blocked action pauses for a human). A headless
# daemon can never answer an approval prompt, so `ask_for_approval` is unconditionally "never" — an
# execution failure is returned to the model instead of hanging the turn, which is codex's own
# documented behavior for that setting ("Execution failures are immediately returned to the model").
# `bypassPermissions` (the daemon's own default) maps to `workspace-write`, the closest analogue
# WITHOUT reaching for `--dangerously-bypass-approvals-and-sandbox` (codex's own help text: "EXTREMELY
# DANGEROUS... externally sandboxed environments only") — this is a deliberately more conservative
# choice than claude-cli's own bypassPermissions, not a parity claim; revisit once this backend has a
# real production track record. `default`/`plan`/anything unrecognized stays read-only.
_SANDBOX_FOR_PERMISSION_MODE = {
    "bypassPermissions": "workspace-write",
    "acceptEdits": "workspace-write",
}


def _sandbox_for(permission_mode: str | None) -> str:
    return _SANDBOX_FOR_PERMISSION_MODE.get(permission_mode or "", "read-only")


def _child_env() -> dict:
    """Billing-safety scrub for a codex child: OPENAI_API_KEY is codex's own analogue of
    ANTHROPIC_API_KEY — `~/.codex/auth.json`'s `OPENAI_API_KEY` field reads `null` under a ChatGPT
    subscription login, and a stray env var could switch billing to metered
    API the same way an ANTHROPIC_API_KEY does for claude-cli (spec §1.1). SENESCHAL_SESSION_SOURCE is
    stamped the same way `presence.child_env` stamps every other spawn, `"daemon"` for the one warm
    session — mirrors claude_cli._child_env's own duplication of that scrub, deliberately (each
    backend owns its own copy rather than reaching back into presence.py)."""
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env["SENESCHAL_SESSION_SOURCE"] = "daemon"
    return env


def build_chat_event_from_stream(ev: dict, source: str, model: str | None = None):
    """Convert one parsed codex-CLI `--json` line into a digestible chat.event, or None to skip it —
    codex-cli's own answer to `cockpit_pipe.build_chat_event_from_stream` (spec §1.6/§3.1: "a second
    backend needs its own converter feeding the *same* `chat.event` shape into the same sink; nothing
    downstream of that one function needs to change"). Uses `cockpit_pipe.chat_event`/`_preview`
    directly rather than reimplementing them — the SHAPE is shared, only the source wire format isn't.

    `item.completed` (`item.type == "agent_message"`) becomes `assistant_output` (text); `item.type ==
    "command_execution"` becomes `assistant_output` carrying one `tool_uses` entry (codex's `--json`
    stream names no per-tool identity beyond "a shell command," unlike claude-cli's named tool_use
    blocks — `name` is the literal string `"shell"`, which is honest rather than invented). `item.
    started` (the `in_progress` half of the same pair) is dropped deliberately, matching claude-cli's
    own "unknown/uninteresting event types are dropped" posture — converting it too would double-count
    every tool call once as started and again as completed. The terminal `turn.completed` becomes
    `turn_done`, carrying `usage` verbatim; unlike claude-cli's terminal `result` event, codex's usage
    block has no per-call dollar cost and `turn.completed` itself carries no reply text (the reply is
    scattered across the turn's own `item.completed`/`agent_message` events, already emitted above) —
    `reply_preview`/`duration_ms`/`num_turns`/`total_cost_usd` all stay absent rather than guessed, this
    function's own "never invents fields" discipline, inherited from the function it mirrors."""
    if not isinstance(ev, dict):
        return None
    t = ev.get("type")
    if t == "item.completed":
        item = ev.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        if item_type == "agent_message":
            text = item.get("text")
            if not text:
                return None
            return cockpit_pipe.chat_event("assistant_output", source=source, model=model,
                                           text=cockpit_pipe._preview(str(text), 4000))
        if item_type == "command_execution":
            command = item.get("command")
            return cockpit_pipe.chat_event(
                "assistant_output", source=source, model=model,
                tool_uses=[{"name": "shell", "input_preview": cockpit_pipe._preview(command)}])
        return None
    if t == "turn.completed":
        usage = ev.get("usage")
        return cockpit_pipe.chat_event("turn_done", source=source, model=model, is_error=False,
                                       usage=usage if isinstance(usage, dict) else None)
    if t in ("turn.failed", "error", "thread.error"):
        return cockpit_pipe.chat_event("turn_done", source=source, model=model, is_error=True,
                                       reply_preview=cockpit_pipe._preview(str(ev), 400))
    return None


def codex_identity(codex_home: str | None = None) -> dict | None:
    """`{account_id}` from `~/.codex/auth.json`'s `tokens.account_id`, or None — generalizes
    `presence.read_claude_identity` (spec §3.1's `identity()`) for the codex backend. Mirrors that
    function's own discipline exactly: None means "could not tell", never "no account"; only an id
    ever leaves this function, and the actual `id_token`/`access_token`/`refresh_token` values are
    NEVER READ past the `tokens` dict's own keys, let alone returned — the same "never read, copy or
    stamp a token" rule `presence.py`'s own identity function states for `~/.claude.json`.
    `CODEX_HOME` is codex's own env-var config-dir override, the same role `CLAUDE_CONFIG_DIR` plays
    for `~/.claude.json` (spec §1.1)."""
    home = codex_home or os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    path = os.path.join(home, "auth.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:  # noqa: BLE001 — missing, unreadable, malformed: all "I could not tell"
        return None
    if not isinstance(cfg, dict):
        return None
    tokens = cfg.get("tokens")
    account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    if not account_id:
        return None
    return {"account_id": str(account_id), "auth_mode": cfg.get("auth_mode")}


class CodexWarmSession:
    """`Backend` contract implementation over `codex exec --json`. See the module docstring for the
    process-model difference from `claude_cli.WarmSession` (no held-open subprocess between turns) and
    the safety posture (no MCP grant is reachable from this class)."""

    cost_model = "subscription"  # child_env scrubs OPENAI_API_KEY; codex reports no per-call $ anyway

    def __init__(self, codex_bin: str, model: str | None, permission_mode: str, log,
                 mcp_configs: list | None = None, fallback_model: str | None = None,
                 resume_session_id: str | None = None):
        self.codex_bin = codex_bin
        self.model = model
        self.resume_session_id = resume_session_id
        self.resumed = bool(resume_session_id)
        self.resume_failed = False
        # Rung-2 (model-won't-spawn) spawn-fallback exists at the SIGNATURE level for parity with
        # WarmSession, but `fallback_model` is expected None until codex's own spawn-failure shapes are
        # characterized (spec §2.1: unverified) — `_can_fallback` below is a correct no-op when it is.
        self.fallback_model = fallback_model
        self.did_fallback = False
        self.fallback_alerted = False
        self._delivered_any = False
        self.timed_out = False
        self.turn_gap_sec = CODEX_TURN_GAP_SEC
        self.permission_mode = permission_mode
        self.log = log
        # Accepted for constructor parity with WarmSession, never turned into a spawn argument — see
        # the module docstring's Safety section. Codex has no per-invocation MCP flag to pass one to.
        self.mcp_configs = mcp_configs or []
        # Only set WHILE a turn's subprocess is running; None between turns (see module docstring).
        self.proc: subprocess.Popen | None = None
        self.session_id: str | None = None
        self._on_opened = None
        self.api_key_source: str | None = None
        self.started_at: float | None = None
        self.turns_served = 0
        self.last_usage: dict | None = None
        self.last_num_turns: int | None = None  # codex has no per-turn iteration count — always None
        self.context_tokens: int | None = None
        self.session_cost_usd: float | None = None   # codex/ChatGPT reports no per-call dollar cost
        self.last_turn_cost_usd: float | None = None  # — both stay None for this backend, honestly

    def age_sec(self) -> float | None:
        return None if self.started_at is None else time.monotonic() - self.started_at

    def start(self) -> None:
        """Nothing to spawn — see the module docstring. Resets the per-"session" counters exactly like
        `WarmSession.start()` does, so `presence.py`'s teardown/respawn bookkeeping (which calls
        `close()` then `start()` on a fallback or a resume-didn't-take retry) behaves identically."""
        self.started_at = time.monotonic()
        self.turns_served = 0
        self.last_usage = None
        self.context_tokens = None
        self.session_cost_usd = None
        self.last_turn_cost_usd = None

    def send(self, text: str, on_event=None, cold_retry_text: str | None = None) -> str | None:
        """Mirrors `WarmSession.send`'s first-turn fallback ladder (rung 1: resume didn't take; rung 2:
        model won't spawn), adapted to codex's per-turn-subprocess model — there is no `close()`/
        `start()` respawn needed between rungs since there is no held-open process to tear down, only
        the resume id / model string to clear before the next `_send_turn` call."""
        self.timed_out = False
        reply = self._send_turn(text, on_event)
        if reply is None and not self.timed_out and not self._delivered_any and self.resume_session_id:
            self.log(f"! codex session couldn't resume {self.resume_session_id!r} "
                     "(stale/unknown thread id?); starting cold and re-grounding.")
            self.resume_session_id = None
            self.resumed = False
            self.resume_failed = True
            reply = self._send_turn(cold_retry_text or text, on_event)
        if reply is None and not self.timed_out and not self._delivered_any and self._can_fallback():
            self.log(f"! codex session produced no reply on its first turn with model {self.model!r}; "
                     f"falling back to {self.fallback_model!r} and retrying (bad warm dial?).")
            self.did_fallback = True
            self.model = self.fallback_model
            reply = self._send_turn(cold_retry_text or text, on_event)
        if reply is not None:
            self._delivered_any = True
        return reply

    def _can_fallback(self) -> bool:
        return (not self.did_fallback and bool(self.fallback_model)
                and self.fallback_model != self.model)

    def _send_turn(self, text: str, on_event=None) -> str | None:
        env = _child_env()
        cmd = [self.codex_bin, "exec", "--skip-git-repo-check", "--json",
               "--sandbox", _sandbox_for(self.permission_mode),
               "--ask-for-approval", "never"]
        if self.model:
            cmd += ["--model", self.model]
        if self.resume_session_id:
            cmd += ["resume", self.resume_session_id, text]
        else:
            cmd += [text]
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=REPO_ROOT, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=_NO_WINDOW,
            )
        except OSError as e:
            self.log(f"! codex exec failed to spawn: {e}")
            self.proc = None
            return None
        try:
            # ALWAYS close stdin immediately — codex reads it when attached, even with nothing to
            # give it (verified: "Reading additional input from stdin..." on every run, hangs on
            # a held-open pipe). Nothing is ever written to it; there is no multi-turn stdin protocol
            # here the way there is for WarmSession.
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        return self._read_until_result(on_event)

    def _read_until_result(self, on_event=None) -> str | None:
        watchdog = _TurnWatchdog(self.proc, getattr(self, "turn_gap_sec", CODEX_TURN_GAP_SEC),
                                 self.log).start()
        try:
            return self._read_events(watchdog, on_event)
        finally:
            watchdog.cancel()
            self.timed_out = watchdog.fired
            try:
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
            self.proc = None  # the turn's process has exited either way — see module docstring

    def _read_events(self, watchdog, on_event=None) -> str | None:
        text_parts: list[str] = []
        saw_error = False
        for raw in self.proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            watchdog.beat()
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "thread.started":
                tid = ev.get("thread_id")
                if tid:
                    self.session_id = tid
                    if self._on_opened:
                        self._on_opened(tid)
            elif t == "item.completed":
                item = ev.get("item") or {}
                item_type = item.get("type")
                if item_type == "agent_message" and item.get("text"):
                    text_parts.append(str(item["text"]))
                elif item_type == "error":
                    saw_error = True
                    self.log(f"! codex turn item error: {str(item.get('message') or item)[:200]}")
            elif t == "turn.completed":
                self._record_turn_usage(ev)
            elif t in ("turn.failed", "error", "thread.error"):
                saw_error = True
                self.log(f"! codex turn error event: {str(ev)[:200]}")
            if on_event is not None:
                try:
                    on_event(ev)
                except Exception:  # noqa: BLE001 — a tee failure must never break the turn
                    pass
        # codex exec is one-shot: stdout closing IS the end of the turn (no separate terminal-event
        # branch to return early from, unlike WarmSession's persistent-process read loop).
        try:
            rc = self.proc.wait(timeout=10) if self.proc else None
        except Exception:  # noqa: BLE001
            rc = None
        if saw_error or not text_parts:
            if rc not in (0, None) or saw_error:
                self.log(f"! codex turn produced no usable reply (exit {rc}, error seen: {saw_error})")
            return None
        self.turns_served += 1
        return "\n".join(text_parts)

    def _record_turn_usage(self, ev: dict) -> None:
        """`turn.completed`'s usage block, verbatim — codex's shape (`input_tokens`,
        `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`)
        has no dollar-cost field and no per-iteration `num_turns` concept, so `session_cost_usd`/
        `last_turn_cost_usd`/`last_num_turns` stay None here — honestly absent, never guessed.
        `context_tokens` uses `input_tokens` directly (already the whole-context figure per turn, per
        the captured fixtures — codex does not sum-across-iterations the way claude-cli's usage block
        does, so none of `context_tokens_from_usage`'s division-by-num_turns arithmetic applies)."""
        try:
            usage = ev.get("usage")
            if isinstance(usage, dict):
                self.last_usage = usage
                ctx = usage.get("input_tokens")
                if isinstance(ctx, int) and not isinstance(ctx, bool):
                    self.context_tokens = ctx
        except Exception:  # noqa: BLE001 — metering must never break a turn
            pass

    def close(self) -> None:
        if self.proc:
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
