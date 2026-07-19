#!/usr/bin/env python3
"""The assistant's resident presence daemon — always on, warm when you talk to it. Stdlib-first.

This is the single always-on local nerve center (it replaces the 5-min sentinel heartbeat). The host
desktop is always up, so a long-lived process is free; and because the daemon only invokes Claude on a
real message, it burns ~zero tokens while idle (event-driven, not timer-polled).

**Reactive core (asyncio).** One event loop supervises a few long-lived tasks — the full design and the
invariants each task must preserve live in seneschal/docs/asyncio-daemon-design.md:
  * `telegram_task` — long-polls Telegram (in a worker thread) and enqueues inbound to the durable
    action queue the moment it's off the wire.
  * `discord_task`  — Discord inbound (REST cadence today; the gateway websocket is phase 2).
  * `drainer_task`  — the SOLE consumer of the action queue: owns the warm Claude session (one resident
    `claude` process in stream-json mode), drains messages front-to-back one turn at a time, and winds
    the session down after idle (default 20 min). Chat turns stay strictly serialized by construction.
  * `scheduler_task`— a ~5 s tick: lock heartbeat, slot reaping/launching, reminder fires, roll refill,
    comms peek. Reminders now fire within a tick of due EVEN while a chat turn is running (they used to
    wait out the whole turn).
  * `control_task`  — watches the control queue (graceful restart/shutdown) and applies it only once the
    warm session is idle and the queue is empty.
All blocking I/O (the subprocess-based sentinel helpers, the warm session's pipe reads) hops through
asyncio.to_thread; shared state is mutated only on the event loop, so there are no locks to get wrong.

Durability: inbound messages are consumed off Telegram/Discord with the offset committed, so the daemon
persists its **action queue** (unanswered messages) to state/presence-state.json after every step and on
exit, and reloads it on startup — a restart (routine via `reseneschald` after a PR merge) never drops a
message it had already taken. The fire-and-forget headless runs (peek + scheduled slots) are serialized
to one at a time so their store read-bursts don't overlap and (on a rate-limited backend like Notion)
trip its limit; they are ALSO deferred while the warm chat session is actively processing a turn, so a
chat turn's reads and a slot's read-burst never overlap either. Chat is priority and is never delayed — only the slot/peek waits (it
retries on the next tick, reusing the same deferral path as an in-flight headless run).

**Subscription, not API.** The warm session is the `claude` **CLI** (`-p --input-format stream-json
--output-format stream-json`), which bills against the logged-in Claude subscription. We deliberately
scrub ANTHROPIC_API_KEY from the child env so a stray key can never switch the assistant to metered API billing.
For an unattended service, authenticate once with `claude setup-token` and set CLAUDE_CODE_OAUTH_TOKEN
(see SCHEDULING.md). The SDK runner stays frozen (it is API-billed only).

USAGE (service — normally launched via run-presence.cmd, not by hand):
  python presence.py --model claude-opus-4-8 --idle-min 20 --poll-timeout 25 \
      --peek-interval-min 5 --watch-prompt "Run Watch mode (seneschal/SKILL.md)."

USAGE (offline test, no real claude/Telegram — --stub-send guarantees no real sends either):
  python presence.py --stub-brain --fake-inbox msgs.json --stub-send --max-iterations 6 --no-peek
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from reminders_roll import refill_rolls

# Identity plumbing (persona/identity.json → the grounding/slot prompts). Guarded like the
# other optional sibling imports (router, discord_gateway): a missing/broken identity_common
# must degrade to the unconfigured behavior, never keep the daemon from booting.
try:
    from identity_common import get_str as _identity_get
    from identity_common import load_identity
except ImportError:  # pragma: no cover — behave exactly like an unconfigured install
    def load_identity(path=None):  # type: ignore[misc]
        return {}

    def _identity_get(identity, section, key):  # type: ignore[misc]
        return None

# Owner-timezone plumbing (tz_common: configured identity zone → machine-local fallback).
# Guarded like identity_common above: without it, local_now/local_stamp keep their original
# pure machine-local behavior, and the daemon still boots.
try:
    import tz_common as _tz_common
except ImportError:  # pragma: no cover — behave exactly like the pre-tz_common daemon
    _tz_common = None

from sentinel import (  # shared helpers — sentinel is now a helper library
    DEFAULT_STATE_DIR,
    DEFAULT_TELEGRAM_ENV,
    NO_WINDOW,  # Windows: console children spawn without a visible console (test_windowless_spawns)
    SCRIPT_DIR,
    check_reminders,
    load_json,
    parse_iso,
    poll_discord,
    poll_telegram,
    save_json,
    send_discord,
    send_telegram,
)

REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
THREAD_CAP = 20  # rolling turns kept for cross-session continuity
MIN_IDLE_MIN = 10.0  # hard floor: a warm session always stays warm at least this long after activity
SLOT_MAX_RETRIES = 8  # give up relaunching a failed slot after this many bad exits in a day (backstop)
# Poison-pill guard for the durable action queue. A message that ends its turn WITHOUT a delivered
# reply — most dangerously one that kills the daemon mid-turn (the "reseneschald" self-kill: a warm-session
# command that restarts the process, so the reply never lands and the message is never popped) — is
# otherwise re-queued and replayed on every boot, forever. We count each turn BEFORE sending and persist
# it up-front, so a mid-turn kill still advances the counter; past this cap the message is dead-lettered.
MAX_TURN_ATTEMPTS = 3
# Sentinel file the warm session drops (via scripts/request_restart.py) to ask for a graceful reload;
# honored AFTER the reply is delivered, so a chat "reseneschald" no longer dies mid-turn. Kept for back-compat
# and folded into the control queue below (request_restart.py now enqueues a restart control instead).
RESTART_REQUEST = "restart.request"
# Control queue: flagged daemon control requests (restart / shutdown) dropped by scripts/request_control.py
# — e.g. seneschald-control.ps1 -Action Update after a main merge, or a chat "reseneschald". A control with
# defer_until_idle is applied only once the warm chat session has wound down AND the action queue is empty,
# so a code reload never interrupts a live conversation. Keep the filename in sync with request_control.py.
CONTROL_QUEUE = "control-queue.json"
# When a defer-until-idle control is waiting, wind the warm session down after just this much quiet (1 min)
# instead of the full --idle-min, so the reload lands promptly after the owner stops typing (never mid-exchange).
CONTROL_PENDING_IDLE_SEC = 60.0
# Store-config file: which backend the assistant uses, and (for MCP-backed backends like Notion) the MCP
# config to thread into every spawned claude. The daemon reads backends[active].mcp_config from here — see
# resolve_store_mcp(). Gitignored (written by /setup-store); seed is store/config.example.json.
STORE_CONFIG = os.path.join(REPO_ROOT, "seneschal", "store", "config.json")
# Legacy Notion MCP config (back-compat): a read/write Notion server the headless daemon threads into every
# claude it spawns. The interactive app's Notion connector is NOT inherited by headless `claude -p`, so
# without a store MCP the warm Telegram session can chat but can't read/write the store (acks never persist
# → the next slot re-fires). This path is now a FALLBACK — the store config (above) is the primary source;
# an install predating /setup-store that has scripts/notion-mcp.json still works. Drop it in place (see
# NOTION_MCP_SETUP.md) and it's picked up when no store/config.json exists.
DEFAULT_NOTION_MCP = os.path.join(SCRIPT_DIR, "notion-mcp.json")
# discord.env is auto-detected the same way (see DISCORD_SETUP.md): drop the file in scripts/ and the
# two-way Discord channel (gateway push, REST fallback) turns on — no launcher edit. --no-discord opts out.
DEFAULT_DISCORD_ENV = os.path.join(SCRIPT_DIR, "discord.env")

# Heavyweight scheduled runs the daemon owns itself (replacing separate Task Scheduler entries).
# Times are the MACHINE-LOCAL wall clock — assumed to match the owner's timezone (startup warns via
# warn_tz_mismatch when identity.owner.timezone says otherwise). Each fires at
# most once per local day; a run that's missed (machine asleep at its time) fires late on the next loop
# IF still within the catch-up window, else it's skipped for the day. Reminder slots run the Reminders
# subagent (which enqueues nudges the daemon then delivers); the rest run the orchestrator/journal.
# Prompts carry the {tz} identity token; the runnable SLOTS below is rendered by build_slots().
SLOTS_TEMPLATE = [
    {"name": "daily-journal", "at": "05:00",
     "prompt": "Run the Daily Journal (subagents/journal-steward/daily-journal-steward/SKILL.md). "
               "Use {tz}. Run silently."},
    {"name": "morning-brief", "at": "06:30",
     "prompt": "Run the morning Brief (seneschal/SKILL.md): deliver in chat + push highlights to Telegram "
               "+ email via Proton + write the Run Log. Use {tz}. Run silently."},
    {"name": "reminders-morning", "at": "08:00",
     "prompt": "Run Reminders mode (subagents/reminders/SKILL.md) for the MORNING slot: run the daily "
               "reset, reconcile acks, compute this slot's fires, enqueue nudges to state/reminders.json "
               "(reminders_enqueue.py), update the tracker + Run Log. Use {tz}. Run silently."},
    {"name": "reminders-midday", "at": "12:30",
     "prompt": "Run Reminders mode (subagents/reminders/SKILL.md) for the MIDDAY slot: reconcile acks, "
               "re-fire unacked important, re-surface snoozed, enqueue nudges to state/reminders.json, "
               "update the tracker + Run Log. Use {tz}. Run silently."},
    {"name": "reminders-evening", "at": "18:30",
     "prompt": "Run Reminders mode (subagents/reminders/SKILL.md) for the EVENING slot: reconcile "
               "acks, re-fire unacked important, enqueue nudges, update the tracker + Run Log. "
               "Use {tz}. Run silently."},
    {"name": "eod-wrap", "at": "21:07",
     "prompt": "Run the Wrap (seneschal/SKILL.md -> subagents/eod-wrap/SKILL.md). Done-today includes "
               "Tasks completed today AND ⏰ Reminders rows with Last Acknowledged = today (never "
               "judge by the Ack checkbox - the slots consume it). Use {tz}. Run silently."},
    {"name": "reminders-bedtime", "at": "21:30",
     "prompt": "Run Reminders mode (subagents/reminders/SKILL.md) for the BEDTIME slot: final re-fire "
               "of unacked important, enqueue nudges, update the tracker + Run Log. Use {tz}. "
               "Run silently."},
    {"name": "dream", "at": "22:00",
     "prompt": "Run the Dream consolidation (seneschal/SKILL.md): rebuild state/context-digest.md, "
               "refresh reminders, propose learnings, then commit + open a PR (Dream step 5). "
               "Use {tz}. Run silently."},
]


def child_env() -> dict:
    """Env for any child `claude` process: scrub ANTHROPIC_API_KEY so we always bill the
    subscription (CLAUDE_CODE_OAUTH_TOKEN / the logged-in account), never the metered API."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env


# --------------------------------------------------------------------------- store MCP resolution

def _load_store_config(path: str) -> "tuple[dict, bool]":
    """Read store/config.json defensively — NEVER raises (wrapped exactly like load_identity, so a
    broken/absent config can't keep the daemon from booting). Returns (config_dict, ok): ok is False
    when the file is absent or unparseable (config_dict is {} then), True when it parsed to a dict."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):  # missing file / bad JSON — treat as "not configured"
        return {}, False
    if isinstance(data, dict):
        return data, True
    return {}, False


def _resolve_store_path(p: str) -> str:
    """Resolve a store-config path string. Paths are forward-slash (Windows-safe JSON); `~` is expanded
    here, and a relative path is taken against the repo root (mcp_config ships as e.g.
    'seneschal/store/notion/mcp.json'). See store/README.md."""
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(REPO_ROOT, p)
    return os.path.normpath(p)


def resolve_store_mcp(args, config_path: str | None = None,
                      legacy_mcp: str | None = None) -> "tuple[str | None, str]":
    """Decide which MCP config (if any) the daemon forwards into every spawned headless `claude` — now
    driven by the pluggable store config (store/README.md), not hardwired to Notion. Returns
    (path_or_None, log_line); the caller assigns the path to args.notion_mcp (the attribute the peek /
    slot / warm-session spawns still read) and logs the line. Never raises.

    Resolution order:
      1. **Explicit flag** — `--store-mcp` (or the deprecated `--notion-mcp` alias), i.e. args.store_mcp.
      2. **store/config.json** — `backends[active].mcp_config`, when that key exists AND the file is
         present. This is how a Notion-configured install wires its MCP.
      3. **Legacy auto-detect** — scripts/notion-mcp.json, ONLY when config.json is absent/unparseable
         (back-compat for an install that predates /setup-store).
      4. **None.**

    Filesystem backends (obsidian/markdown) carry no mcp_config, so they resolve to None — and that is
    HEALTHY (logged informationally, not warned). The one WARN case is an active **notion** backend whose
    mcp_config names a file that is missing. An absent/unparseable config.json logs a 'run /setup-store'
    hint and forwards no MCP (chat still works)."""
    config_path = config_path or STORE_CONFIG
    legacy_mcp = legacy_mcp or DEFAULT_NOTION_MCP

    explicit = getattr(args, "store_mcp", None)
    if explicit:
        return explicit, f"Store MCP wired for headless runs (explicit --store-mcp): {explicit}"

    cfg, ok = _load_store_config(config_path)
    if ok:
        active = cfg.get("active") or "?"
        backends = cfg.get("backends")
        backend = backends.get(active) if isinstance(backends, dict) else None
        mcp = backend.get("mcp_config") if isinstance(backend, dict) else None
        if mcp:
            resolved = _resolve_store_path(mcp)
            if os.path.exists(resolved):
                return resolved, f"Store: {active} (MCP wired for headless runs: {resolved})"
            # A store that names an MCP but is missing it is genuinely broken — WARN, forward nothing.
            return None, (f"! Store: active backend '{active}' names mcp_config '{mcp}' but the file is "
                          f"missing ({resolved}) — headless runs can't reach the store (acks won't "
                          "persist). Run /setup-store (see seneschal/store/notion/mapping.md).")
        # No mcp_config → a filesystem backend (obsidian/markdown). Nothing to forward, and that's fine.
        return None, f"Store: {active} (filesystem — no MCP needed)"

    # config.json absent/unparseable: fall back to the legacy Notion MCP if present, else nudge to setup.
    if os.path.exists(legacy_mcp):
        return legacy_mcp, (f"Store not configured (no store/config.json); using legacy Notion MCP "
                            f"{legacy_mcp}. Run /setup-store to migrate.")
    return None, ("store not configured — run /setup-store. Spawning without a store MCP "
                  "(chat still works; the store can't be read/written this run).")


def local_now() -> datetime:
    """The owner's current local time as an aware datetime, via tz_common: the configured
    identity.owner.timezone when it resolves (tzdata ships in the uv venv now), else the
    machine-local wall clock — the same fallback contract this function has always had (an
    unconfigured or venv-less install behaves identically to before; startup logs a warning via
    warn_tz_mismatch when the configured zone and the machine clock disagree). Date/label logic
    only: slot fire-times stay machine-local (see SLOTS_TEMPLATE / maybe_run_slots)."""
    if _tz_common is not None:
        return _tz_common.local_now()
    return datetime.now().astimezone()


def local_stamp() -> str:
    """Authoritative local-time stamp for prompts, e.g. 'Friday 2026-07-03 12:30 Central Daylight Time'.
    Every spawned claude MUST base 'today'/'yesterday' on this instead of inferring the date — that
    inference drifts a day forward off a UTC clock (rule 5), the classic off-by-one 'yesterday'
    bug. Handed in explicitly so no run ever has to guess what 'now' is. Rendered in the owner's
    timezone via local_now() (machine-local when unconfigured/unresolvable)."""
    return local_now().strftime("%A %Y-%m-%d %H:%M %Z")

# The warm session's first-turn grounding. Two token vocabularies live here:
#   * identity tokens {assistant}/{owner}/{tz} — substituted ONCE at startup by
#     _render_grounding() from persona/identity.json (generic phrases when unconfigured);
#   * per-turn tokens {channel}/{now}/{thread}/{msg} — left untouched until the drainer's
#     send-time GROUNDING.format(...) fills them for each new session.
GROUNDING_TEMPLATE = """You are {assistant} — {owner}'s chief of staff — talking with them live over {channel}.
The current local date and time is {now} ({tz}) — treat this as the AUTHORITATIVE clock for
every "today"/"yesterday"/"tomorrow" and all date math. Do not infer the date yourself and never trust a
UTC clock; if a reminder's text disagrees with this stamp, this stamp wins.
Read persona/persona.md (fall back to persona/persona.default.md) and seneschal/SKILL.md (Chat mode) and
stay fully in character: reply as the assistant in the persona's voice, never as Claude, no
meta-narration, run any tools silently. Keep
replies concise and chat-appropriate. Honor the act-low / ask-high gate (draft-and-hold anything
outbound or destructive). Only tell {owner} something is done/logged/marked off/cleared when the tool call
actually succeeded — if the store or any tool is unreachable or errors, say so plainly and park it in
carry-over; never claim a write that didn't land.
You have the SAME store read/write access here as the full seneschal skill: reading is act-low, and act-low
tracker writes you should just make — don't merely say you will. In particular, when {owner} acknowledges a
reminder ("took'em", "done", "did it", "already ate"), immediately `store-update` that reminder — set
status: done (for EVERY type — a plain ack means done FOR TODAY, not retired; the item still re-fires on
its next cycle. Only set status: finished if they EXPLICITLY say they're FINISHED with the item, e.g.
done-for-good, no more reminders), last_acknowledged: today, consecutive_misses: 0 — resolving the write
through the active store's mapping (store/<backend>/mapping.md) per reminders-policy.md. Set those FIELDS
directly; do NOT just flip the one-tap ack affordance (reminder slots consume and reset it, so it is not
the durable record; last_acknowledged is what the EOD wrap counts). Then the next reminder slot
sees it acked and stops re-firing. AFTER the store write, also run
scripts/reminders_dequeue.py --reminder-id <that reminder's ref>: I fire queued nudges by due_at and
cannot read store acks, so that one call does two things — it pulls any obsolete nudge already staggered
for the thing they just did, AND it records the ack to the durable local ledger (state/acks.json) so my
fire path suppresses any OTHER un-fired nudge for that row (including a soft-digest that covers it, which a
per-id pull can't reach). Run it once per acked row. The warm session is volatile (it winds down / a
reboot clears it); the store + that ledger are the durable record, so the ack MUST land there, not just in
this chat. Confirm only once the write succeeded; if
the store is unreachable, say so plainly and park it in carry-over. Flipping a *linked* Task/Goal to Done
stays ask-high.
Keep reads cheap: don't fan out a big parallel read burst — lean on the baked-in references
(store/<backend>/schema.md, state/context-digest.md) instead of re-querying, and honor the active
backend's throughput notes. On the Notion backend specifically, keep concurrent reads to a handful and,
on a 429, wait a beat and retry serially rather than hammering (store/notion/mapping.md).
If {owner} asks you to restart or reload yourself ("reseneschald", "restart", "reload your code"): do NOT run
seneschald-control.ps1 or otherwise kill the daemon from here — you are running INSIDE that daemon, so a
synchronous restart kills you mid-reply, your answer never sends, and the daemon replays the request on
every boot (a self-kill loop). Instead, finish your reply normally, then run (act-low)
`python seneschal/scripts/request_restart.py`. The resident daemon reloads itself gracefully AFTER your reply
is delivered — same effect as reseneschald, no dropped message. {thread}
The owner just said: {msg}"""


# --------------------------------------------------------------------------- identity rendering

def _identity_tokens(identity) -> dict:
    """Raw values for the three identity tokens. The fallbacks are EXACTLY the generic phrases
    that were hardcoded in the grounding/slot prompts before persona/identity.json existed, so
    an unconfigured install renders byte-identical prose (this templating is a no-op for it)."""
    return {
        "{assistant}": _identity_get(identity, "assistant", "name") or "the resident assistant",
        "{owner}": _identity_get(identity, "owner", "name") or "the owner",
        "{tz}": _identity_get(identity, "owner", "timezone") or "the owner's configured timezone",
    }


def _render_grounding(template: str, identity) -> str:
    """Substitute the identity tokens ({assistant}/{owner}/{tz}) into the grounding template,
    passing the per-turn tokens ({channel}/{now}/{thread}/{msg}) through UNTOUCHED for the
    drainer's send-time .format. Deliberately str.replace on the three exact tokens rather
    than a .format pass: format would try to resolve the per-turn tokens too (KeyError), and
    escaping around that is fiddlier than three replaces. The substituted VALUES get their
    braces doubled so a stray '{' in a configured name can never crash the send-time format."""
    out = template
    for token, value in _identity_tokens(identity).items():
        out = out.replace(token, value.replace("{", "{{").replace("}", "}}"))
    return out


def build_slots(identity, template: list | None = None) -> list:
    """SLOTS_TEMPLATE with the identity tokens substituted into each prompt (fresh copies —
    the template is never mutated). Slot prompts are handed to `claude -p` verbatim, never
    .format()ed, so values are substituted RAW here — no brace doubling, unlike the grounding.
    Slot fire TIMES are untouched: slots run on the machine-local wall clock regardless of
    identity.owner.timezone (see warn_tz_mismatch)."""
    tokens = _identity_tokens(identity)
    slots = []
    for s in (SLOTS_TEMPLATE if template is None else template):
        s = dict(s)
        for token, value in tokens.items():
            s["prompt"] = s["prompt"].replace(token, value)
        slots.append(s)
    return slots


def warn_tz_mismatch(identity, log) -> None:
    """Best-effort startup check: if identity.owner.timezone is set AND resolvable on this
    machine, compare its current UTC offset to the machine-local one and log ONE warning when
    they differ — slot times and reminder math run machine-local, so a mismatch means nudges
    land on the machine's clock, not the owner's (date/label math DOES follow the owner zone,
    via tz_common). The tzdata package rides the uv venv; on a bare interpreter an unresolvable
    zone skips silently. Never raises."""
    tz_name = _identity_get(identity, "owner", "timezone")
    if not tz_name:
        return
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tz_name)
        now = datetime.now(timezone.utc)
        configured, machine = now.astimezone(tz).utcoffset(), now.astimezone().utcoffset()
    except Exception:  # noqa: BLE001 — informational check only; no tzdata/bad key = skip
        return
    if configured != machine:
        log(f"! configured owner timezone {tz_name} (UTC{configured}) differs from "
            f"machine-local (UTC{machine}) — slot times fire machine-local")


# Loaded once at startup (import time). persona/identity.json is optional — load_identity()
# never raises and yields the generic defaults when it's absent, so GROUNDING/SLOTS render
# byte-identical to the pre-identity hardcoded prose on an unconfigured install.
IDENTITY = load_identity()
GROUNDING = _render_grounding(GROUNDING_TEMPLATE, IDENTITY)
SLOTS = build_slots(IDENTITY)


# --------------------------------------------------------------------------- thread continuity

def thread_path(state_dir: str) -> str:
    return os.path.join(state_dir, "telegram-thread.json")


def append_thread(state_dir: str, role: str, text: str) -> None:
    path = thread_path(state_dir)
    thread = load_json(path, [])
    if not isinstance(thread, list):
        thread = []
    thread.append({"role": role, "text": text, "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
    save_json(path, thread[-THREAD_CAP:])


def thread_tail(state_dir: str) -> str:
    thread = load_json(thread_path(state_dir), [])
    if not isinstance(thread, list) or not thread:
        return ""
    lines = [f"{t.get('role', '?')}: {t.get('text', '')}" for t in thread[-6:]]
    return "Recent conversation (for continuity):\n" + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- Router advisor (shadow)

ROUTER_LOG = "router-log.jsonl"


def router_log_path(state_dir: str) -> str:
    return os.path.join(state_dir, ROUTER_LOG)


def shadow_classify(state_dir: str, channel: str, text: str, log) -> None:
    """Front-door **Router advisor**, phase 1 (SHADOW ONLY). Classify one inbound chat message with the
    local Ollama router and append the verdict to state/router-log.jsonl. This makes **zero** behavior
    change — the caller proceeds to escalate to the warm session exactly as before, regardless of the
    verdict. It only gathers accuracy evidence to review before phase 2 enables local handling.

    Wrapped so it can NEVER delay or break a chat turn: the whole thing is best-effort. router.classify()
    already never raises (it returns an escalate fallback on any failure), and this adds a belt-and-braces
    try/except so even an import/write error just logs and continues. qwen3.5:4b is fast (~3s, think off)
    so the inline call is cheap; a rare slow/unreachable Ollama returns the escalate fallback quickly."""
    try:
        import router  # local, stdlib-only; imported lazily so `off` mode never touches Ollama
        verdict = router.classify(text)
        row = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "channel": channel,
            "text_preview": text[:80],
            "verdict": verdict.get("verdict"),
            "category": verdict.get("category"),
            "confidence": verdict.get("confidence"),
            "model": verdict.get("model"),
        }
        with open(router_log_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        log(f"• router (shadow): {row['verdict']}/{row['category']} "
            f"conf={row['confidence']} — escalating as usual")
    except Exception as e:  # noqa: BLE001 — shadow must never break a turn
        log(f"! router shadow classify skipped: {e}")


def deliver_reply(channel: str, reply: str, args, log, retries: int = 1) -> bool:
    """Send the assistant's reply back to the channel it came from, with one retry, and report whether it
    actually landed. Previously the loop fired send_telegram() and ignored the result — a silently
    dropped reply (transient Telegram error) still got recorded as a delivered turn, so the owner saw
    nothing while the warm session believed it had answered. Callers MUST gate the thread append on
    this so continuity stays honest."""
    if getattr(args, "stub_send", False):
        # Offline harness: never touch a real channel (a stub-brain run with a live telegram.env once
        # messaged the owner for real). Record the would-be send so tests can assert on it.
        with open(os.path.join(args.state_dir, "sent.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"channel": channel, "text": reply}, ensure_ascii=False) + "\n")
        log(f"[stub-send:{channel}] {reply[:80]!r}")
        return True

    def send_once() -> dict:
        if channel == "discord":
            return send_discord(reply, args.discord_env) or {}
        return send_telegram(reply, args.telegram_env) or {}

    for attempt in range(retries + 1):
        res = send_once()
        if res.get("ok"):
            return True
        log(f"! {channel} send failed (attempt {attempt + 1}/{retries + 1}): {res.get('error')}")
        if attempt < retries:
            time.sleep(2)
    return False


# --------------------------------------------------------------------------- persisted daemon state

def daemon_state_path(state_dir: str) -> str:
    return os.path.join(state_dir, "presence-state.json")


def load_daemon_state(state_dir: str) -> dict:
    """Reload the daemon's own runtime state saved by a prior run. The key field is `pending`: the
    action queue of inbound messages that were consumed from Telegram/Discord (their offset already
    committed) but not yet successfully answered. Persisting it means a restart — routine via
    `reseneschald` after every PR merge — never drops a message it had already taken off the wire."""
    st = load_json(daemon_state_path(state_dir), {})
    if not isinstance(st, dict):
        st = {}
    pending = st.get("pending")
    if not isinstance(pending, list):
        pending = []
    # Normalize entries; tolerate legacy/partial ones (missing channel or attempts).
    clean = []
    for item in pending:
        if isinstance(item, dict) and item.get("text"):
            attempts = item.get("attempts", 0)
            try:
                attempts = max(0, int(attempts))
            except (TypeError, ValueError):
                attempts = 0
            clean.append({"channel": item.get("channel") or "telegram", "text": item["text"],
                          "attempts": attempts})
    st["pending"] = clean
    return st


def _queue_entry(item) -> dict:
    """Normalize a queue item to its persisted dict. Tolerates both (channel, text) and
    (channel, text, attempts) — attempts defaults to 0 — so every caller (and older tests) is safe."""
    channel, text = item[0], item[1]
    attempts = item[2] if len(item) > 2 else 0
    try:
        attempts = max(0, int(attempts))
    except (TypeError, ValueError):
        attempts = 0
    return {"channel": channel, "text": text, "attempts": attempts}


def save_daemon_state(state_dir: str, pending: list, last_session_id: str | None = None) -> None:
    """Snapshot the daemon's runtime state (the action queue + light bookkeeping) so the next start
    can resume it. Written after each enqueue/dequeue and on exit — small and cheap (stdlib JSON).
    Each entry carries `attempts` (turns tried without delivery) so the poison-pill guard survives a
    restart — see MAX_TURN_ATTEMPTS."""
    save_json(daemon_state_path(state_dir), {
        "pending": [_queue_entry(i) for i in pending],
        "last_session_id": last_session_id,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })


# ---------------------------------------------------------------- serialize headless Notion spawns

def prune_children(children: list) -> list:
    """Drop finished child processes in place; keep the ones still running. Returns the same list."""
    children[:] = [c for c in children if getattr(c, "poll", lambda: 0)() is None]
    return children


def heavy_run_in_flight(children: list) -> bool:
    """True while any fire-and-forget headless `claude -p` (a comms peek or a scheduled slot) is still
    running. We gate new peeks/slots on this so at most ONE headless Notion reader runs at a time —
    two overlapping runs would each fire their own parallel read burst and stampede Notion's rate
    limit (429s, which is why reads — not the singular writes — get throttled). The warm chat session
    is NOT counted here — that's `warm_session_busy()`'s job (below); this covers only the headless
    children, so the two gates compose cleanly."""
    return bool(prune_children(children))


def warm_session_busy(session, pending: list) -> bool:
    """True when the warm Telegram Chat session is actively mid-turn — i.e. there's a live session AND
    queued inbound still to answer this loop. We gate new peeks/slots on this too, so a headless Notion
    read-burst never overlaps a chat turn's reads (both hit the same ~3 req/s bucket → 429s). `session.
    send()` is synchronous, so a chat turn that will run *this* iteration is exactly `pending` being
    non-empty; deferring the slot/peek launched at the top of the loop keeps them off the wire while
    chat reads. **Chat is priority and is never delayed** — only the slot waits (it retries next loop,
    reusing the same deferral path as an in-flight headless run). A live session with an empty queue is
    idle between turns, so a slot may launch then."""
    return session is not None and bool(pending)


# --------------------------------------------------------------------------- warm Claude session

class WarmSession:
    """One resident `claude` process in stream-json mode = a warm in-RAM session across turns."""

    def __init__(self, claude_bin: str, model: str | None, permission_mode: str, log,
                 mcp_config: str | None = None):
        self.claude_bin = claude_bin
        self.model = model
        self.permission_mode = permission_mode
        self.log = log
        self.mcp_config = mcp_config
        self.proc: subprocess.Popen | None = None
        self.session_id: str | None = None
        self.api_key_source: str | None = None

    def start(self) -> None:
        env = child_env()  # billing safety: never let a stray key force metered API
        cmd = [self.claude_bin, "-p",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--verbose",
               "--permission-mode", self.permission_mode]
        if self.mcp_config:
            cmd += ["--mcp-config", self.mcp_config]
        if self.model:
            cmd += ["--model", self.model]
        self.proc = subprocess.Popen(
            cmd, cwd=REPO_ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # Force UTF-8 both ways — claude's stream-json output is UTF-8; without this the daemon
            # decodes it with the Windows ANSI codepage (cp1252) and mangles —, emoji, etc. (mojibake).
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=NO_WINDOW,  # a console child of the console-less daemon must not pop a window
        )

    def send(self, text: str) -> str | None:
        """Send one user turn; return the assistant's reply text (or None if the session died)."""
        if not self.proc or self.proc.poll() is not None:
            return None
        line = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return None
        return self._read_until_result()

    def _read_until_result(self) -> str | None:
        for raw in self.proc.stdout:  # blocks line-by-line until this turn's result event
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                self.session_id = ev.get("session_id")
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


class StubWarmSession:
    """Offline stand-in: no subprocess, canned replies. For --stub-brain tests."""

    def __init__(self, *_, log=lambda *_: None, **__):
        self.session_id = "stub-session"
        self.api_key_source = "none"
        self.turns = 0
        self.closed = False

    def start(self) -> None:
        self.closed = False
        self.turns = 0

    def send(self, text: str) -> str:
        self.turns += 1
        return f"[stub reply #{self.turns} to: {text.splitlines()[-1][:50]}]"

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- single-instance lock

def lock_path(state_dir: str) -> str:
    return os.path.join(state_dir, "presence.lock")


def lock_is_live(state_dir: str, stale_sec: float) -> bool:
    lk = load_json(lock_path(state_dir), None)
    if not isinstance(lk, dict) or not lk.get("heartbeat"):
        return False
    try:
        age = (datetime.now(timezone.utc) - parse_iso(lk["heartbeat"])).total_seconds()
    except (ValueError, TypeError):
        return False
    return age < stale_sec


def write_lock(state_dir: str) -> None:
    save_json(lock_path(state_dir), {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "heartbeat": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })


def beat_lock(state_dir: str) -> None:
    lk = load_json(lock_path(state_dir), {}) or {}
    lk["heartbeat"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    lk.setdefault("pid", os.getpid())
    save_json(lock_path(state_dir), lk)


def release_lock(state_dir: str) -> None:
    try:
        os.remove(lock_path(state_dir))
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------- graceful self-restart request

def restart_request_path(state_dir: str) -> str:
    return os.path.join(state_dir, RESTART_REQUEST)


def restart_requested(state_dir: str) -> bool:
    """True when the warm session has asked for a graceful reload (chat 'reseneschald'). It drops the
    sentinel via scripts/request_restart.py; we honor it only after delivering its reply, then re-exec
    — never a synchronous kill from inside the turn, which would die mid-reply and re-queue forever."""
    return os.path.exists(restart_request_path(state_dir))


def clear_restart_request(state_dir: str) -> None:
    try:
        os.remove(restart_request_path(state_dir))
    except FileNotFoundError:
        pass


# --------------------------------------------------------------- graceful control queue (restart/shutdown)

def control_queue_path(state_dir: str) -> str:
    return os.path.join(state_dir, CONTROL_QUEUE)


def load_control_queue(state_dir: str) -> list:
    """The daemon's control requests (restart/shutdown), enqueued by scripts/request_control.py. Applied
    only when the warm session is idle (see the main loop), so a code reload never cuts off a live chat."""
    items = load_json(control_queue_path(state_dir), [])
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and i.get("action")]


def pop_control(state_dir: str) -> dict | None:
    """Remove and return the head control entry (or None), persisting the shortened queue."""
    items = load_control_queue(state_dir)
    if not items:
        return None
    head = items.pop(0)
    save_json(control_queue_path(state_dir), items)
    return head


# --------------------------------------------------------------------------- comms peek cadence

def maybe_peek(state_dir: str, args, log, children: list | None = None,
               warm_busy: bool = False) -> bool:
    """On cadence, launch the cheap headless Watch peek (fire-and-forget). Prefers --watch-prompt
    (built into an argv list — no shell quoting); falls back to a raw --watch-cmd string.

    Deferred while another headless run is in flight (`children`) OR the warm chat session is mid-turn
    (`warm_busy`) so a peek's Notion reads never stampede in parallel with a slot's or a chat turn's;
    a skipped peek just runs on the next loop once the cadence is still due. Chat is never delayed —
    only the peek waits."""
    if args.peek_interval_min <= 0 or not (args.watch_prompt or args.watch_cmd):
        return False
    if warm_busy or (children is not None and heavy_run_in_flight(children)):
        return False  # a chat turn or another headless run is active — don't add a second concurrent reader
    peek_file = os.path.join(state_dir, "last-peek")
    last = load_json(peek_file, None)
    now = datetime.now(timezone.utc)
    try:
        due = last is None or (now - parse_iso(last)).total_seconds() >= args.peek_interval_min * 60
    except (ValueError, TypeError):
        due = True
    if not due:
        return False
    save_json(peek_file, now.isoformat().replace("+00:00", "Z"))
    if args.stub_brain:
        log("• comms peek (stubbed)")
        return True
    try:  # fire-and-forget; Watch escalates/notifies on its own
        if args.watch_prompt:
            wp = (f"{args.watch_prompt} (Authoritative current local date/time: {local_stamp()}, "
                  f"the owner's configured timezone — base every date on this, not a UTC clock.)")
            cmd = [args.claude_bin, "-p", wp, "--permission-mode", args.permission_mode]
            if args.notion_mcp:
                cmd += ["--mcp-config", args.notion_mcp]
            if args.watch_model:
                cmd += ["--model", args.watch_model]
            proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=child_env(), creationflags=NO_WINDOW)
        else:
            proc = subprocess.Popen(args.watch_cmd, shell=True, cwd=REPO_ROOT, env=child_env(),
                                    creationflags=NO_WINDOW)
        if children is not None:
            children.append(proc)
        log("• comms peek launched")
    except Exception as e:  # noqa: BLE001
        log(f"! comms peek launch failed: {e}")
    return True


# --------------------------------------------------------------------------- scheduled slot runs

def parse_hhmm(s: str) -> int:
    """'HH:MM' → minutes since local midnight."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def classify_slots(slots: list, now_local: datetime, fired: dict, window_min: int):
    """Split not-yet-fired-today slots into (to_run, too_late). A slot is due once its local time has
    arrived; if we're more than `window_min` past it (e.g. the machine was asleep through its window),
    it's stamped-but-skipped so a 6:30 brief never fires at 11pm. Pure → unit-tested."""
    today = now_local.strftime("%Y-%m-%d")
    now_min = now_local.hour * 60 + now_local.minute
    to_run, too_late = [], []
    for s in slots:
        if fired.get(s["name"]) == today:
            continue
        start = parse_hhmm(s["at"])
        if now_min < start:
            continue  # not yet
        (to_run if now_min <= start + window_min else too_late).append(s)
    return to_run, too_late


def maybe_run_slots(state_dir: str, args, log, children: list | None = None,
                    warm_busy: bool = False, slot_children: dict | None = None) -> None:
    """Fire any due heavyweight slot runs (fire-and-forget `claude -p`), at most once per local day.

    Slots are serialized against the peek and each other via `children`: at most one headless run
    launches per loop, and none launches while a prior one is still in flight. This matters most on
    catch-up — several slots can come due at once when the machine wakes, and launching them all
    together would fire several Notion read-bursts in parallel and trip the rate limit. A deferred
    slot isn't stamped, so it simply retries on the next loop once the running one finishes.

    **A launched slot is NOT stamped done here.** Launch success only means the process *started* — a
    `claude -p` that starts then dies (a network storm, a Notion-429 morning) would otherwise be marked
    done-for-the-day and never retried, silently skipping (e.g.) the whole morning nudge batch. Instead
    the launched proc is registered in `slot_children`; `reap_finished_slots` stamps it only when it
    exits **0**, and on a non-zero exit leaves it unstamped so the next loop relaunches it — bounded by
    the same catch-up window (`--slot-catchup-min`), after which `classify_slots` gives up and stamps it.

    Slots are ALSO deferred while the warm chat session is mid-turn (`warm_busy`): a chat turn's Notion
    reads and a scheduled slot's read burst hit the same ~3 req/s bucket, so overlapping them 429s.
    Chat is priority and is never delayed — only the slot waits (it stays unstamped and retries next
    loop, exactly like the in-flight-headless case)."""
    if args.no_slots or args.stub_brain or args.fake_inbox is not None:
        return
    if warm_busy:
        return  # warm chat session mid-turn — hold slots so their reads don't overlap chat's; retry next loop
    path = os.path.join(state_dir, "slots.json")
    fired = load_json(path, {})
    if not isinstance(fired, dict):
        fired = {}
    now_local = datetime.now()  # machine-local wall clock (the owner's configured timezone)
    to_run, too_late = classify_slots(SLOTS, now_local, fired, args.slot_catchup_min)
    if not to_run and not too_late:
        return
    today = now_local.strftime("%Y-%m-%d")
    stamp = local_stamp()
    changed = False
    for s in too_late:  # missed its window — stamp so it doesn't linger, but don't run
        fired[s["name"]] = today
        changed = True
        log(f"• slot '{s['name']}' skipped (past {s['at']} + {args.slot_catchup_min}m catch-up)")
    for s in to_run:
        if children is not None and heavy_run_in_flight(children):
            log(f"• slot '{s['name']}' deferred (another headless run in flight) — retries next loop")
            break  # leave it unstamped; a later loop picks it up once the running one finishes
        model = s.get("model") or args.slot_model or args.model
        # Hand the run its authoritative clock so nudge text never guesses the date (rule 5).
        prompt = (f"{s['prompt']} (Authoritative current local date/time: {stamp}, the owner's "
                  f"configured timezone — base every date on this, not a UTC clock.)")
        cmd = [args.claude_bin, "-p", prompt, "--permission-mode", args.permission_mode]
        if args.notion_mcp:
            cmd += ["--mcp-config", args.notion_mcp]
        if model:
            cmd += ["--model", model]
        try:
            proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=child_env(), creationflags=NO_WINDOW)
            if children is not None:
                children.append(proc)
            if slot_children is not None:
                slot_children[s["name"]] = proc  # reaped on exit — stamped only if it finishes clean
            log(f"• slot '{s['name']}' launched ({s['at']} local)")
        except Exception as e:  # noqa: BLE001
            log(f"! slot '{s['name']}' launch failed: {e}")
    if changed:
        save_json(path, fired)


def _stamp_slot(state_dir: str, name: str, today: str) -> None:
    path = os.path.join(state_dir, "slots.json")
    fired = load_json(path, {})
    if not isinstance(fired, dict):
        fired = {}
    if fired.get(name) != today:
        fired[name] = today
        save_json(path, fired)


def reap_finished_slots(slot_children: dict, state_dir: str, log, slot_retries: dict | None = None,
                        max_retries: int = SLOT_MAX_RETRIES, now_local: datetime | None = None) -> None:
    """Stamp a slot done only once its `claude -p` run has actually **finished cleanly** (exit 0).

    Called at the top of each loop, before `maybe_run_slots` re-evaluates what's due. For every tracked
    slot child that has exited: exit 0 → stamp `slots.json[name] = today` (done for the day); non-zero →
    leave it unstamped and log loudly, so the next loop relaunches it (a failed morning run retries
    instead of silently vanishing). Two independent backstops keep a persistently-broken slot from
    relaunching forever: `--slot-catchup-min` (once the slot is that far past its time, `classify_slots`
    routes it to `too_late` and stamps it) and `max_retries` (after this many failed exits in a day, give
    up and stamp it — guards against a fast-failing run that would otherwise respawn every loop). Retry
    counts live in `slot_retries` (in-memory; a clean exit or a give-up clears the slot). Still-running
    children are left in place."""
    if not slot_children:
        return
    if slot_retries is None:
        slot_retries = {}
    now_local = now_local or datetime.now()
    today = now_local.strftime("%Y-%m-%d")
    for name, proc in list(slot_children.items()):
        rc = proc.poll()
        if rc is None:
            continue  # still running
        del slot_children[name]
        if rc == 0:
            _stamp_slot(state_dir, name, today)
            slot_retries.pop(name, None)
            log(f"• slot '{name}' completed")
            continue
        slot_retries[name] = slot_retries.get(name, 0) + 1
        if slot_retries[name] >= max_retries:
            _stamp_slot(state_dir, name, today)  # give up for the day so it can't respawn endlessly
            slot_retries.pop(name, None)
            log(f"! slot '{name}' run failed (exit {rc}) {max_retries}x — giving up for today")
        else:
            log(f"! slot '{name}' run failed (exit {rc}) — retry {slot_retries[name]}/{max_retries} next loop")


def maybe_refill_rolls(state_dir: str, log, now_local: datetime | None = None) -> None:
    """Regenerate standing every-N-hours reminder rolls (reminders_roll.py) at most once per local
    day, stamped in ``rolls.json``. Pure-local queue math (no Notion, no spawn), so it runs regardless
    of the warm-session/in-flight gates that hold the heavyweight slots. Idempotent even within a day."""
    now_local = now_local or local_now()
    today = now_local.strftime("%Y-%m-%d")
    path = os.path.join(state_dir, "rolls.json")
    stamp = load_json(path, {})
    if not isinstance(stamp, dict):
        stamp = {}
    if stamp.get("refilled") == today:
        return
    try:
        refill_rolls(state_dir, now_local=now_local, log=log)
    except Exception as e:  # noqa: BLE001 — a bad roll must never take the daemon down
        log(f"! reminder-roll refill failed: {e}")
        return
    stamp["refilled"] = today
    save_json(path, stamp)


# --------------------------------------------------------------------------- inbound source

def next_messages(args, fake_queue: list) -> dict:
    """Return a telegram_poll-style result. Real Telegram in prod; the fake queue in tests."""
    if args.fake_inbox is not None:
        batch = fake_queue.pop(0) if fake_queue else []
        return {"ok": True, "messages": batch}
    return poll_telegram(args.telegram_env, args.state_dir, commit=True, timeout=args.poll_timeout)


# --------------------------------------------------------------------------- reactive core (asyncio)

class DaemonState:
    """Everything the old single-threaded loop kept in locals, shared across the tasks. Mutated ONLY
    from coroutines on the event loop (never from inside to_thread callables) — that rule is what keeps
    this lock-free, exactly like the single-threaded loop it replaced."""

    def __init__(self):
        self.pending: list = []              # the durable action queue: (channel, text, attempts)
        self.pending_event = asyncio.Event() # pulsed on enqueue so the drainer wakes instantly
        self.stop = asyncio.Event()          # supervisor-wide wind-down signal
        self.session = None                  # the warm chat session (drainer-owned)
        self.session_busy = False            # True while a send() is in flight in a worker thread
        self.last_activity = time.monotonic()
        self.headless_children: list = []    # live fire-and-forget peek/slot procs (Notion serialization)
        self.slot_children: dict = {}        # name -> live slot proc (stamped only on clean exit)
        self.slot_retries: dict = {}         # name -> failed-exit count today
        self.control_pending = False         # a defer-until-idle control is waiting to apply
        self.pending_action: str | None = None  # 'restart' | 'shutdown' once a control is applied
        self.crashed = False                 # a task died unexpectedly — exit non-zero so the task
                                             # scheduler's restart-on-failure brings us back
        self.iterations = 0                  # telegram poll cycles (bounds test runs)

    def warm_busy(self) -> bool:
        """Mid-conversation, for the peek/slot gates: a send in flight OR anything queued. Queued with
        no session up yet still counts — a turn is imminent (the drainer is about to spawn one, or is
        in its transient-failure backoff), and launching a headless Notion read-burst into that window
        is exactly the 429 stampede this gate exists to prevent. Strictly wider than the old loop's
        `warm_session_busy` (which required a live session), closing its retry-path hole."""
        return self.session_busy or bool(self.pending)

    def chat_idle(self) -> bool:
        """True when a defer-until-idle control may apply: session wound down, nothing queued,
        nothing mid-turn."""
        return self.session is None and not self.pending and not self.session_busy


async def _sleep_or_stop(state: DaemonState, seconds: float) -> None:
    """Sleep, but return immediately if the supervisor starts winding down."""
    try:
        await asyncio.wait_for(state.stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _wait_pending_or_stop(state: DaemonState, seconds: float) -> None:
    """Park the drainer until new inbound arrives (pending_event), the daemon winds down, or the
    timeout lapses (so idle wind-down arithmetic re-runs on schedule)."""
    if state.pending or state.stop.is_set():
        return
    state.pending_event.clear()
    if state.pending:  # enqueued in the same tick between the check and the clear — don't sleep on it
        return
    waiters = [asyncio.ensure_future(state.pending_event.wait()),
               asyncio.ensure_future(state.stop.wait())]
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()


async def _enqueue_inbound(state: DaemonState, args, log, new_inbound: list) -> None:
    """Take messages that are already consumed off the wire (offset committed) into the durable action
    queue. Thread-append + Router shadow happen here, once per message — NOT in the drainer's retry
    path, or a message that fails delivery would be re-appended/re-classified on every retry."""
    if not new_inbound:
        return
    # Persist + wake the drainer FIRST: the wire offset is already committed, so until this save lands
    # a hard kill silently loses the batch. The (slow — seconds on a cold Ollama) shadow classification
    # happens after, and the drainer can already be mid-turn while it runs.
    for ch, t, _ in new_inbound:
        append_thread(args.state_dir, "owner", t)
    state.pending.extend(new_inbound)
    save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
    state.pending_event.set()
    if args.router_mode != "off":
        for ch, t, _ in new_inbound:
            # Shadow only (phase 1): classify + log, zero behavior change. In a thread because a cold
            # Ollama can take seconds, and inbound intake must never stall the loop.
            await asyncio.to_thread(shadow_classify, args.state_dir, ch, t, log)


async def telegram_task(state: DaemonState, args, fake_queue: list, log) -> None:
    """Inbound Telegram: long-poll (blocking, in a worker thread), enqueue, repeat. The long-poll is
    the reason this is its own task — it no longer paces anything else. While a control is waiting to
    apply, the poll window drops to 2 s so a graceful reload lands promptly after the owner stops typing."""
    while not state.stop.is_set():
        if args.fake_inbox is not None:
            res = next_messages(args, fake_queue)
        else:
            timeout = 2 if state.control_pending else args.poll_timeout
            res = await asyncio.to_thread(poll_telegram, args.telegram_env, args.state_dir, True, timeout)
        # Even if stop was set while we were parked on the wire, PROCESS the result first — the poll
        # already committed the offset for anything it fetched, so skipping here would lose messages.
        # Enqueued-but-unanswered items are persisted and the successor picks them up (invariant 1).
        if res.get("ok"):
            new_inbound = [("telegram", (m.get("text") or "").strip(), 0)
                           for m in res.get("messages", []) if (m.get("text") or "").strip()]
            await _enqueue_inbound(state, args, log, new_inbound)
        else:
            log(f"! telegram poll: {res.get('error')}")
        state.iterations += 1
        if args.max_iterations and state.iterations >= args.max_iterations:
            state.stop.set()
            return
        if args.fake_inbox is not None:
            await _sleep_or_stop(state, 0.05)  # test mode doesn't block on the network — pace lightly


async def discord_task(state: DaemonState, args, log) -> None:
    """Inbound Discord: **gateway websocket** (push — messages land the moment they're sent) when the
    `websockets` dependency is importable, REST cadence-polling otherwise. The gateway path still runs
    one REST `after=` catch-up poll on every (re)connect, so messages that arrived while disconnected
    are never lost — and both paths share the same snowflake offset file, so they stay coherent.

    The fallback matters operationally: a graceful reload re-execs the *current* interpreter, so a
    daemon that predates the uv venv keeps running without `websockets` until its next hard start —
    degraded (slower Discord), never dead. See seneschal/docs/asyncio-daemon-design.md."""
    discord_on = bool(args.discord_env) and not args.no_discord
    if not discord_on or args.fake_inbox is not None:
        return

    async def rest_catchup() -> None:
        res = await asyncio.to_thread(poll_discord, args.discord_env, args.state_dir, True)
        if res.get("ok"):
            new_inbound = [("discord", (m.get("text") or "").strip(), 0)
                           for m in res.get("messages", []) if (m.get("text") or "").strip()]
            await _enqueue_inbound(state, args, log, new_inbound)
        else:
            log(f"! discord poll: {res.get('error')}")

    if not args.no_discord_gateway:
        try:
            import discord_gateway
        except Exception as e:  # noqa: BLE001 — a broken module must degrade, not kill the daemon
            discord_gateway = None
            log(f"! discord gateway import failed ({e}) — REST polling instead")
        if discord_gateway is not None and discord_gateway.gateway_available():
            env = discord_gateway.load_env(args.discord_env)
            offset_file = os.path.join(args.state_dir, "discord-offset")

            async def enqueue_gateway(messages: list) -> None:
                new_inbound = [("discord", (m.get("text") or "").strip(), 0)
                               for m in messages if (m.get("text") or "").strip()]
                await _enqueue_inbound(state, args, log, new_inbound)

            clean = await discord_gateway.run_gateway(
                env, offset_file, state.stop, enqueue_gateway, rest_catchup, log)
            if clean or state.stop.is_set():
                return
            log("! discord gateway gave up (fatal close) — REST polling for the rest of this run")
        else:
            log("! discord: websockets unavailable (venv missing/stale?) — REST polling; "
                "a hard restart after `uv sync --frozen` enables the gateway")

    while not state.stop.is_set():
        await rest_catchup()
        await _sleep_or_stop(state, args.discord_poll_sec)


async def drainer_task(state: DaemonState, args, log, make_session, idle_sec: float) -> None:
    """The SOLE consumer of the durable action queue — owns the warm session, drains front-to-back one
    turn at a time (chat turns stay strictly serialized by construction), winds the session down after
    idle. This is the old loop's drain block ported verbatim; every branch below encodes an incident,
    so change it with the design doc open."""
    while not state.stop.is_set():
        if not state.pending:
            if state.session is not None:
                # Wind the warm session down once it's been quiet (sooner if a control is waiting).
                limit = min(idle_sec, CONTROL_PENDING_IDLE_SEC) if state.control_pending else idle_sec
                quiet = time.monotonic() - state.last_activity
                if quiet >= limit:
                    log("• winding down idle warm session")
                    await asyncio.to_thread(state.session.close)
                    state.session = None
                    continue
                await _wait_pending_or_stop(state, min(5.0, max(0.1, limit - quiet)))
            else:
                await _wait_pending_or_stop(state, 5.0)
            continue

        channel, text, attempts = state.pending[0]
        # Poison-pill guard: past the cap, a message keeps ending its turn without ever being
        # delivered (classically, one that kills the daemon mid-turn — the "reseneschald" self-kill).
        # Stop replaying it forever: drop it to a dead-letter and tell the owner it was set aside.
        if attempts >= MAX_TURN_ATTEMPTS:
            state.pending.pop(0)
            save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
            log(f"! dead-lettered a message after {attempts} undelivered turns: {text[:80]!r}")
            await asyncio.to_thread(
                deliver_reply, channel,
                ("Heads up — I kept hitting a snag on this one and had to set it aside "
                 f"after {MAX_TURN_ATTEMPTS} tries, so I don't loop on it:\n\n{text[:300]}"),
                args, log)
            continue
        # Count this turn UP FRONT and persist before the risky send, so a kill mid-turn (which
        # never reaches the delivery check below) still advances the counter across the restart.
        attempts += 1
        state.pending[0] = (channel, text, attempts)
        save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
        if state.session is None:
            state.session = make_session()
            await asyncio.to_thread(state.session.start)
            prompt = GROUNDING.format(channel=channel.capitalize(), now=local_stamp(),
                                      thread=thread_tail(args.state_dir), msg=text)
        else:
            # Refresh the clock every turn — a warm session only saw the date once, at grounding,
            # so a long-lived one would drift across midnight.
            prompt = (f"(For reference, the authoritative current local time is {local_stamp()} "
                      f"— the owner's configured timezone.)\n\n{text}")
        state.session_busy = True
        try:
            # The send blocks a worker thread on the child's stdout until this turn's result event —
            # the event loop stays free, so reminders/polls/controls keep running underneath.
            reply = await asyncio.to_thread(state.session.send, prompt)
        finally:
            state.session_busy = False
        if reply is None:  # session died/errored — reset and apologize
            try:
                await asyncio.to_thread(state.session.close)
            except Exception:  # noqa: BLE001
                pass
            state.session = None
            reply = "Sorry — I hit a snag just now. Try me again?"
        if await asyncio.to_thread(deliver_reply, channel, reply, args, log):
            append_thread(args.state_dir, "assistant", reply)
            state.pending.pop(0)  # answered AND delivered — drop it from the durable queue
            save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
            state.last_activity = time.monotonic()
        else:
            # Never delivered — don't record it as sent (that corrupts continuity: the warm session
            # would think it answered) and DON'T pop it from the queue, so the durable action queue
            # retries it / persists it for the next start. Drop the warm session so the retry
            # re-grounds cleanly from the thread tail (no phantom reply), then back off so we don't
            # spin on the same undelivered head. A reply WAS produced (the turn survived) — this is a
            # transient send failure, not a poison message, so roll the up-front count back: a
            # Telegram outage must never dead-letter a good message. Only mid-turn deaths (which
            # never reach here) accrue.
            if state.pending and state.pending[0][0] == channel and state.pending[0][1] == text:
                state.pending[0] = (channel, text, max(0, attempts - 1))
            log(f"! reply to the owner NOT delivered via {channel}; kept queued for retry")
            if state.session is not None:
                try:
                    await asyncio.to_thread(state.session.close)
                except Exception:  # noqa: BLE001
                    pass
                state.session = None
            save_daemon_state(args.state_dir, state.pending, None)
            await _sleep_or_stop(state, 5.0)


async def scheduler_task(state: DaemonState, args, log) -> None:
    """The cadence work the old loop did once per (Telegram-paced) iteration, now on its own steady
    ~5 s tick: lock heartbeat, slot reap/launch, reminder fires, roll refill, comms peek. The big
    reactive win lives here — a due reminder no longer waits out a long chat turn or a 25 s poll.

    check_reminders runs in a worker thread while a chat turn may be mid-flight; if the chat's
    reminders_dequeue.py rewrites reminders.json in that window, last-writer-wins could resurrect a
    just-dequeued nudge — the durable ack ledger (state/acks.json, checked at fire time) is the
    correctness backstop that keeps a resurrected entry from actually buzzing (see the 2026-07-10
    fire-time ack-gate work)."""
    while not state.stop.is_set():
        beat_lock(args.state_dir)
        # Stamp any slot whose run just finished cleanly (and relaunch — leave unstamped — any that
        # died), BEFORE re-evaluating what's due below, so a failed morning run retries promptly.
        reap_finished_slots(state.slot_children, args.state_dir, log, state.slot_retries)
        discord_on = bool(args.discord_env) and not args.no_discord
        if not args.no_reminders:
            await asyncio.to_thread(
                check_reminders, args.state_dir, datetime.now(timezone.utc), True, args.telegram_env,
                call_env=args.call_env, discord_env=args.discord_env if discord_on else None)
            # Top up standing every-N-hours rolls once per local day. In a thread: it takes the
            # cross-process queue lock, which may wait behind a delivery burst.
            await asyncio.to_thread(maybe_refill_rolls, args.state_dir, log)
        warm_busy = state.warm_busy()
        if not args.no_peek:
            maybe_peek(args.state_dir, args, log, state.headless_children, warm_busy)
        if not args.no_slots:
            maybe_run_slots(args.state_dir, args, log, state.headless_children, warm_busy,
                            slot_children=state.slot_children)
        await _sleep_or_stop(state, args.tick_sec)


async def control_task(state: DaemonState, args, log) -> None:
    """Watch the control queue (+ the legacy restart.request sentinel) and apply the head entry once
    the warm session is idle — a code reload never cuts off a live conversation. Applying = set
    pending_action and wind the whole supervisor down; main() re-spawns after state is saved."""
    if args.stub_brain or args.fake_inbox is not None:
        return  # test modes never restart themselves (matches the old loop's gating)
    while not state.stop.is_set():
        controls = load_control_queue(args.state_dir)
        if restart_requested(args.state_dir):
            controls = controls + [{"action": "restart", "defer_until_idle": True,
                                    "reason": "restart.request sentinel"}]
        control = controls[0] if controls else None
        if control is None:
            state.control_pending = False
        elif control.get("defer_until_idle", True) and not state.chat_idle():
            # Hold it; the drainer winds the session down after CONTROL_PENDING_IDLE_SEC of quiet and
            # the Telegram task shortens its long-poll, so the reload lands right after a quiet gap.
            state.control_pending = True
        else:
            action = control.get("action", "restart")
            log(f"• control '{action}' — applying (reason: {control.get('reason', '')!r})")
            if control.get("reason") != "restart.request sentinel":
                pop_control(args.state_dir)         # drop the queued entry we're applying
            clear_restart_request(args.state_dir)   # and clear any legacy sentinel
            state.pending_action = action
            state.stop.set()
            return
        await _sleep_or_stop(state, 2.0)


async def _supervise(name: str, coro, state: DaemonState, log) -> None:
    """A task that dies unexpectedly must take the daemon down LOUDLY (exit non-zero → the scheduled
    task's restart-on-failure relaunches us), never linger as a half-daemon that looks alive but has,
    say, no drainer."""
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        log(f"! task '{name}' crashed: {e!r}\n{traceback.format_exc()}")
        state.crashed = True
        state.stop.set()


async def main_async(args, log, make_session, fake_queue: list) -> DaemonState:
    """Run the supervisor until a control applies, max-iterations trips, or a signal lands. Returns
    the final DaemonState so main() can act on pending_action / crashed."""
    state = DaemonState()

    # Reload the action queue a prior run left behind (messages taken off Telegram/Discord but not yet
    # answered). A restart — routine after each PR merge — must not drop them.
    dstate = load_daemon_state(args.state_dir)
    state.pending = [(i["channel"], i["text"], i.get("attempts", 0)) for i in dstate.get("pending", [])]
    if state.pending:
        log(f"• reloaded {len(state.pending)} pending message(s) from a prior run")
        state.pending_event.set()

    idle_min = max(args.idle_min, MIN_IDLE_MIN)  # warm sessions last at least MIN_IDLE_MIN minutes
    if idle_min != args.idle_min:
        log(f"• idle-min {args.idle_min}m raised to the {MIN_IDLE_MIN:g}m floor")
    idle_sec = idle_min * 60

    loop = asyncio.get_running_loop()
    hooked_signals = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(state.stop.set))
            hooked_signals.append(sig)
        except (ValueError, OSError):
            pass  # not in main thread / unsupported

    log(f"presence up (stub={args.stub_brain}, model={args.model or 'CLI-default'}, "
        f"idle={idle_min:g}m, reactive core)")
    try:
        await asyncio.gather(
            _supervise("telegram", telegram_task(state, args, fake_queue, log), state, log),
            _supervise("discord", discord_task(state, args, log), state, log),
            _supervise("drainer", drainer_task(state, args, log, make_session, idle_sec), state, log),
            _supervise("scheduler", scheduler_task(state, args, log), state, log),
            _supervise("control", control_task(state, args, log), state, log),
        )
    finally:
        # Snapshot whatever's still unanswered so the next start resumes it (see load_daemon_state).
        save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
        if state.pending:
            log(f"• saved {len(state.pending)} unanswered message(s) for the next start")
        if state.session is not None:
            try:
                state.session.close()
            except Exception:  # noqa: BLE001
                pass
            state.session = None
        if state.pending_action:  # deliberate reload/shutdown — don't orphan in-flight peek/slot procs
            for child in state.headless_children:
                try:
                    child.terminate()
                except Exception:  # noqa: BLE001
                    pass
        # Un-hook our handlers: they capture THIS (about-to-close) loop, and a Ctrl+C landing after
        # asyncio.run returns would raise "Event loop is closed" mid-respawn in main().
        for sig in hooked_signals:
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (ValueError, OSError):
                pass
        release_lock(args.state_dir)
        log("presence stopped")
    return state


def _respawn_detached(log) -> bool:
    """Relaunch on the (freshly pulled) code, preferring the uv-managed venv interpreter when it
    exists so a graceful reload also migrates the daemon onto the venv (run-presence.cmd only picks
    the interpreter on a HARD start). Windows: do NOT os.execv under Task Scheduler — replacing the
    process image kills the daemon without a successor (observed 2026-07-06). Spawn a fresh, DETACHED
    instance — same argv + inherited env, so same flags and subscription billing — then exit; it
    acquires the released lock and takes over."""
    script = os.path.abspath(sys.argv[0])
    try:
        if os.name == "nt":
            venv_py = os.path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")
            exe = venv_py if os.path.exists(venv_py) else sys.executable
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen([exe, script] + sys.argv[1:], close_fds=True,
                             creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
            return True
        venv_py = os.path.join(REPO_ROOT, ".venv", "bin", "python")
        exe = venv_py if os.path.exists(venv_py) else sys.executable
        os.execv(exe, [exe, script] + sys.argv[1:])
    except OSError as e:  # relaunch failed — exit cleanly (the scheduled task can bring us back)
        log(f"! relaunch failed: {e}; exiting")
    return False


# --------------------------------------------------------------------------- entry point

def build_parser() -> argparse.ArgumentParser:
    """The daemon's CLI parser. Extracted so the store-MCP flags (and their deprecated alias) can be
    unit-tested without spinning the whole daemon."""
    p = argparse.ArgumentParser(description="Resident presence daemon (warm Telegram chat + reminders + peek).")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    p.add_argument("--telegram-env", default=DEFAULT_TELEGRAM_ENV)
    p.add_argument("--call-env", default=None, help="push-call.env — enables reminders with channel=call")
    p.add_argument("--discord-env", default=None,
                   help="discord.env — enables two-way Discord (gateway push inbound + channel routing). "
                        "If omitted, auto-detects scripts/discord.env when present.")
    p.add_argument("--no-discord", action="store_true", help="disable Discord even if discord.env exists")
    p.add_argument("--no-discord-gateway", action="store_true",
                   help="force Discord inbound onto REST polling even when websockets is available")
    p.add_argument("--claude-bin", default="claude", help="path to the claude CLI")
    # --store-mcp is the primary flag; the store config (store/config.json) is the normal source, so this
    # is only for an override. --notion-mcp is a DEPRECATED ALIAS (same dest) kept for back-compat.
    p.add_argument("--store-mcp", dest="store_mcp", default=None,
                   help="override: path to an MCP config JSON giving the headless daemon read/write access "
                        "to the store; passed as --mcp-config to every spawned claude (chat + slots + peek). "
                        "If omitted, resolved from store/config.json (backends[active].mcp_config), then a "
                        "legacy scripts/notion-mcp.json. Filesystem backends (obsidian/markdown) need none.")
    p.add_argument("--notion-mcp", dest="store_mcp", default=None,
                   help="DEPRECATED alias for --store-mcp (same effect). Prefer --store-mcp / store/config.json.")
    p.add_argument("--no-notion", action="store_true",
                   help="don't wire any store MCP even if one is configured (chat can't read/write the store)")
    p.add_argument("--model", default=None, help="model id for the warm chat session (default: CLI default)")
    p.add_argument("--permission-mode", default="bypassPermissions",
                   help="claude --permission-mode (the assistant's act-low/ask-high gate is the real safety)")
    p.add_argument("--idle-min", type=float, default=20.0,
                   help="wind the warm session down after this many idle minutes (floored at "
                        f"{MIN_IDLE_MIN:g} — warm sessions always last at least that long)")
    p.add_argument("--poll-timeout", type=int, default=25,
                   help="Telegram long-poll seconds (drops to 2s while a graceful reload is waiting)")
    p.add_argument("--discord-poll-sec", type=float, default=10.0,
                   help="Discord REST inbound cadence seconds (phase 1; the gateway makes this moot)")
    p.add_argument("--tick-sec", type=float, default=5.0,
                   help="scheduler tick seconds (reminder-fire / slot / peek cadence)")
    p.add_argument("--peek-interval-min", type=int, default=0, help="comms-peek cadence (0 = off)")
    p.add_argument("--watch-prompt", default=None,
                   help="prompt for the comms peek; run as `claude -p <prompt>` (no shell quoting needed)")
    p.add_argument("--watch-model", default=None, help="cheap model for the comms peek (default: CLI default)")
    p.add_argument("--watch-cmd", default=None, help="advanced: raw shell command for the peek (instead of --watch-prompt)")
    p.add_argument("--no-reminders", action="store_true", help="don't fire reminders (e.g. tests)")
    p.add_argument("--no-peek", action="store_true", help="disable the comms peek")
    p.add_argument("--no-slots", action="store_true",
                   help="disable the internal scheduled runs (brief/wrap/dream/journal + reminder slots)")
    p.add_argument("--slot-model", default=None, help="model for scheduled slot runs (default: --model)")
    p.add_argument("--slot-catchup-min", type=int, default=180,
                   help="how many minutes past a slot's time it may still fire late (else skip for the day)")
    p.add_argument("--router-mode", choices=["off", "shadow"], default="shadow",
                   help="front-door Router advisor (seneschal/scripts/router.py). 'shadow' (default) classifies "
                        "each inbound chat message with the local Ollama model and LOGS the verdict to "
                        "state/router-log.jsonl — ZERO behavior change, everything still escalates to the "
                        "warm session. 'off' disables it entirely. ('live' local-handling is phase 2, gated "
                        "on shadow accuracy — not implemented.)")
    p.add_argument("--stub-brain", action="store_true", help="use a canned reply stub instead of real claude (tests)")
    p.add_argument("--stub-send", action="store_true",
                   help="record outbound replies to state/sent.jsonl instead of hitting a real channel "
                        "(tests — pair with --stub-brain/--fake-inbox so an offline run can't message anyone)")
    p.add_argument("--fake-inbox", default=None, help="JSON file of message batches to feed instead of Telegram (tests)")
    p.add_argument("--max-iterations", type=int, default=0, help="stop after N telegram poll cycles (0 = run forever)")
    p.add_argument("--log-file", default=None,
                   help="append daemon log lines here too (Task Scheduler drops stdout, so without this "
                        "there's no trace of e.g. a failed Telegram send). Rotated at ~2 MB.")
    return p


def main() -> int:
    args = build_parser().parse_args()

    os.makedirs(args.state_dir, exist_ok=True)

    # Optional durable log — the scheduled task doesn't capture stdout, so failures were invisible.
    log_fh = None
    if args.log_file:
        try:
            if os.path.exists(args.log_file) and os.path.getsize(args.log_file) > 2_000_000:
                try:
                    os.replace(args.log_file, args.log_file + ".1")  # keep one prior generation
                except OSError:
                    pass
            log_fh = open(args.log_file, "a", encoding="utf-8")
        except OSError as e:
            print(f"! could not open --log-file {args.log_file}: {e}", flush=True)

    log_lock = threading.Lock()  # log() is called from worker threads too now (to_thread callables)

    def log(msg: str) -> None:
        # Local time so the log reads in the owner's timezone, not UTC. Internal reminder /
        # heartbeat math stays on UTC instants (that's correctly tz-safe) — only the display changes.
        line = f"[{local_now().isoformat(timespec='seconds')}] {msg}"
        with log_lock:
            print(line, flush=True)
            if log_fh:
                try:
                    log_fh.write(line + "\n")
                    log_fh.flush()
                except (OSError, ValueError):
                    # ValueError = handle already closed (the respawn path logs a launch failure
                    # after main() closed the file) — stdout above still got the line.
                    pass

    # Wire the store's MCP (if any) into every spawned claude. The resolution is store-config-driven —
    # see resolve_store_mcp(): explicit --store-mcp / --notion-mcp → store/config.json's active backend →
    # legacy scripts/notion-mcp.json → None. Filesystem backends (obsidian/markdown) resolve to None, and
    # that's HEALTHY. --no-notion forces it off entirely. The resolved path is kept on args.notion_mcp —
    # the attribute the warm session / slots / peek spawns forward as --mcp-config.
    if args.no_notion:
        args.notion_mcp = None
        log("! Store MCP disabled (--no-notion) — headless chat/slots can't read or write the store.")
    else:
        args.notion_mcp, store_log = resolve_store_mcp(args)
        log(store_log)

    # Auto-detect discord.env like notion-mcp.json: drop the file in scripts/ and Discord turns on.
    if args.no_discord:
        args.discord_env = None
    elif not args.discord_env and os.path.exists(DEFAULT_DISCORD_ENV):
        args.discord_env = DEFAULT_DISCORD_ENV
    if args.discord_env:
        log(f"Discord wired (gateway push, REST fallback): {args.discord_env}")

    # One-line heads-up when the configured owner timezone and the machine clock disagree
    # (slot times + reminder math fire machine-local; date/label math follows the owner zone
    # via tz_common). Best-effort: skips silently when zoneinfo can't resolve (tzdata ships in
    # the uv venv; a bare interpreter without it just skips).
    warn_tz_mismatch(IDENTITY, log)

    stale_sec = max(args.poll_timeout * 3, 90)
    if lock_is_live(args.state_dir, stale_sec):
        log("presence already running (live lock) — exiting.")
        return 0
    write_lock(args.state_dir)

    fake_queue = []
    if args.fake_inbox is not None:
        loaded = load_json(args.fake_inbox, [])
        # Accept either [[msg,...], ...] (batches) or [msg, ...] (one per iteration).
        fake_queue = loaded if loaded and isinstance(loaded[0], list) else [[m] for m in loaded]

    def make_session() -> "WarmSession | StubWarmSession":
        return StubWarmSession(log=log) if args.stub_brain else WarmSession(
            args.claude_bin, args.model, args.permission_mode, log, mcp_config=args.notion_mcp)

    try:
        state = asyncio.run(main_async(args, log, make_session, fake_queue))
    except KeyboardInterrupt:
        # Ctrl+C before/around the signal handler — main_async's finally already saved state.
        return 0

    if state.crashed:
        return 1  # loud exit → the scheduled task's restart-on-failure relaunches us
    if state.pending_action == "shutdown":
        log("• shutdown — exiting cleanly (pair with Stop-Seneschald to keep the task down)")
        return 0
    if state.pending_action == "restart":
        log("• restart — reloading code")
        if log_fh:
            try:
                log_fh.close()
            except OSError:
                pass
        _respawn_detached(log)
        return 0
    if log_fh:
        try:
            log_fh.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
