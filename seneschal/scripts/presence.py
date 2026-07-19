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
  * `cockpit_task`   — the seneschald cockpit pipe (seneschal/scripts/cockpit_pipe.py; seneschal/docs/cockpit-spec.md):
    a localhost-only, token-authed WebSocket server. Exactly ONE client (the cockpit backend) at a time;
    `chat.send` enqueues into the SAME action queue as Telegram/Discord (source="cockpit"); the warm
    session's turns stream back out as `chat.event`s (also teed to state/warm-transcript.jsonl, a capped
    ring buffer, so a reconnect can backfill); `status`/`status.get` report turn-in-flight/model/queue
    depth; `control.restart` rides the same control-queue path as request_control.py. Degrades (no pipe)
    rather than dying if disabled, `websockets` is unavailable, or in test/offline modes — never affects
    chat or reminders. See asyncio-daemon-design.md for the fail-open/bounded-queue invariants.
All blocking I/O (the subprocess-based sentinel helpers, the warm session's pipe reads) hops through
asyncio.to_thread; shared state is mutated only on the event loop, so there are no locks to get wrong.

Durability: inbound messages are consumed off Telegram/Discord with the offset committed, so the daemon
persists its **action queue** (unanswered messages) to state/presence-state.json after every step and on
exit, and reloads it on startup — a restart (routine via `reseneschald` after a PR merge) never drops a
message it had already taken. The fire-and-forget headless runs (peek + scheduled slots) are serialized
to one at a time so their store read-bursts don't overlap and (on a rate-limited backend like Notion)
trip its limit; they are ALSO deferred while the warm chat session is actively processing a turn, so a
chat turn's reads and a slot's read-burst never overlap either. Chat is priority and is never delayed —
only the slot/peek waits (it retries on the next tick, reusing the same deferral path as an in-flight
headless run).

**Subscription, not API.** The warm session is the `claude` **CLI** (`-p --input-format stream-json
--output-format stream-json`), which bills against the logged-in Claude subscription. We deliberately
scrub ANTHROPIC_API_KEY from the child env so a stray key can never switch the assistant to metered API billing.
For an unattended service, authenticate once with `claude setup-token` and set CLAUDE_CODE_OAUTH_TOKEN
(see SCHEDULING.md). The SDK runner stays frozen (it is API-billed only).

**Model dials + Fable delegation (v3, cockpit-spec.md "Model dials & Fable delegation").** Two dials,
set separately in the cockpit, live in `state/model-config.json` (`model_config.py`): `warm_model` — read
at warm-session SPAWN time (see `resolve_warm_model`; it wins over the `--model` CLI flag below, which
becomes the fallback) — and `max_routable_model` — the hard ceiling on every Fable delegation, re-read
LIVE per inbound turn. The warm session always owns the conversation; when a turn needs more, it
delegates up via `fable_delegate.py` (a subprocess one-shot, NOT a session handoff), triggered by the
router's **fable arm** (`fable_arm_classify`, a hint line only), the warm session's own judgment, or a
**force-route**: a leading `!fable` prefix on any inbound message, or `force_fable: true` on a cockpit
`chat.send` (see `strip_force_fable` / `cockpit_task.on_chat_send`) — bypasses the classifier, never the
approval gate. The ceiling binds every trigger: when it isn't Fable-tier, the fable arm doesn't even run
(`model_config.admits_fable`), and `fable_delegate.py` itself refuses the call regardless of how it was
triggered.

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
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import cockpit_pipe  # the cockpit pipe protocol — sixth supervised task (see cockpit_task below)
import governor  # Oikonomos, the budget governor (v3.5, cockpit-spec.md) — turn-usage metering + alerts
import model_config  # the two-dial model config (v3, cockpit-spec.md "Model dials & Fable delegation")
import reminders_acks as ra  # local_today — the same owner-local-date rule the fire path gates on
from reminders_roll import refill_rolls
from request_control import enqueue_control  # cockpit control.restart -> the same control-queue path

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
    TELEGRAM_INBOX_DIR,
    check_reminders,
    clear_session_heartbeat,
    load_json,
    load_message_map,
    parse_iso,
    poll_discord,
    poll_telegram,
    record_sent_message,
    save_json,
    send_discord,
    send_telegram,
    session_is_live,
    write_session_heartbeat,
)
from telegram_poll import safe_filename  # shared scrub for sender-supplied filenames

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
# slack-mcp.json is auto-detected the same way (Q9 of the Slack draft-and-hold spec): drop the file in
# scripts/ and the headless daemon gains Slack send/read hands, so a chat-approved `send a7` posts
# immediately instead of parking as status=approved (the Slack-hands gap). It's threaded ALONGSIDE the
# store's MCP (the CLI takes multiple space-separated configs after one --mcp-config), so the warm
# session can both *understand* an approval and *post* it. --no-slack opts out. Sending stays ask-high.
DEFAULT_SLACK_MCP = os.path.join(SCRIPT_DIR, "slack-mcp.json")

def active_mcp_configs(args) -> list:
    """Config paths for every active MCP server the daemon threads into a spawned `claude` — the
    store's MCP first (read/write persistence, resolved by resolve_store_mcp onto args.notion_mcp;
    filesystem backends carry none), then Slack (send hands). The claude CLI accepts multiple
    space-separated configs after a single `--mcp-config`, so both servers load together. Defensive
    getattr so a partial args namespace (e.g. tests) still works. Returns [] when nothing is wired."""
    return [c for c in (getattr(args, "notion_mcp", None), getattr(args, "slack_mcp", None)) if c]

# Heavyweight scheduled runs the daemon owns itself (replacing separate Task Scheduler entries).
# Times are the MACHINE-LOCAL wall clock — assumed to match the owner's timezone (startup warns via
# warn_tz_mismatch when identity.owner.timezone says otherwise). Each fires at most once per local day;
# a run that's missed (machine asleep at its time) fires late on the next loop IF still within the
# catch-up window, else it's skipped for the day. These run the orchestrator/journal.
# Reminders are NOT fixed slots anymore: the four Morning/Midday/Evening/Bedtime reminder slots were
# retired for arbitrary per-reminder times — a once-per-local-day `maybe_seed_day` (date-rollover
# triggered, below) seeds each ⏰ row's exact fire time(s) into the queue and the ~5s delivery tick fires
# them. The journal-presence gate + linked-task refresh moved onto eod-wrap/dream (below). See
# seneschal/docs/reminder-exact-time-scheduling-spec.md + references/reminders-policy.md.
# Prompts carry the {tz} identity token; the runnable SLOTS below is rendered by build_slots().
SLOTS_TEMPLATE = [
    {"name": "daily-journal", "at": "05:00",
     "prompt": "Run the Daily Journal (subagents/journal-steward/daily-journal-steward/SKILL.md). "
               "Use {tz}. Run silently."},
    {"name": "morning-brief", "at": "06:30",
     "prompt": "Run the morning Brief (seneschal/SKILL.md): deliver in chat + push highlights to Telegram "
               "+ email via Proton + write the Run Log. Use {tz}. Run silently."},
    {"name": "eod-wrap", "at": "21:07",
     "prompt": "Run the Wrap (seneschal/SKILL.md -> subagents/eod-wrap/SKILL.md). Done-today includes "
               "Tasks completed today AND ⏰ Reminders rows with Last Acknowledged = today (never "
               "judge by the Ack checkbox - it's consumed on read). Also run the journal-presence gate "
               "(nudge-or-satisfy) and refresh linked-task status for active Deadline-Watches so their "
               "re-fires stop once the linked record is Done. Use {tz}. Run silently."},
    {"name": "dream", "at": "22:00",
     "prompt": "Run the Dream consolidation (seneschal/SKILL.md): rebuild state/context-digest.md, "
               "refresh reminders, propose learnings, then commit + open a PR (Dream step 5). Also run "
               "the journal-presence satisfy-only backstop (auto-tick if the owner journaled after the "
               "Wrap check; no bedtime nudge). Use {tz}. Run silently."},
]

# The exact-time reminder SEED (retired the four fixed slots). NOT a fixed-time SLOT: it fires on the
# first tick of each new owner-local date, so a machine asleep through midnight still seeds on wake —
# no classify_slots catch-up cliff that could silently skip the daily reset. It reuses the slot lifecycle
# (registered in slot_children, stamped in slots.json under this name, retried on a crashed run) via
# maybe_seed_day. The brain run does the store parts (reset, acks, which rows are due) then calls
# reminders_seed.py per due row to queue that row's exact-time nudges for the whole day.
# Like the slot prompts, the template carries identity tokens; the runnable SEED_PROMPT is rendered
# at startup by build_seed_prompt() (raw replace — this prompt is never .format()ed).
SEED_SLOT_NAME = "reminders-seed"
SEED_PROMPT_TEMPLATE = (
    "Run Reminders mode (subagents/reminders/SKILL.md) in SEED mode for the whole day: apply "
    "pending acks, run the daily reset (Daily/Weekdays habits), then for EACH ⏰ row due today compute "
    "its fire time(s) — the row's Times field, else its Time Window default — and enqueue exact-time "
    "nudges for the full local day via reminders_seed.py (one call per row; include the 90-minute "
    "re-fire schedule for importance >= High or Nag Until Done, and --pierce-quiet for Critical+). "
    "Update the tracker + Run Log. Use {tz}. Run silently."
)


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


def store_backend_active(config_path: str | None = None, legacy_mcp: str | None = None) -> "str | None":
    """The active store backend's name ('notion' / 'obsidian' / 'markdown' / …), or None when no store
    is configured at all. Sibling of resolve_store_mcp (same sources, same never-raises contract):
    a parsed store/config.json answers with its `active` key; with no config, a legacy
    scripts/notion-mcp.json means a pre-/setup-store **notion** install. Used to gate backend-specific
    machinery — chiefly the write-behind outbox, which is Notion-only (store/notion/mapping.md,
    'Outbox — durable act-low writes'); filesystem backends write locally and never touch it."""
    cfg, ok = _load_store_config(config_path or STORE_CONFIG)
    if ok:
        return cfg.get("active") or None
    if os.path.exists(legacy_mcp or DEFAULT_NOTION_MCP):
        return "notion"
    return None


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


# --------------------------------------------------------------------------- model dials (v3)

def resolve_warm_model(state_dir: str, cli_model: str | None, log) -> str | None:
    """Which model the warm session should spawn with — cockpit-spec.md "Model dials & Fable
    delegation": `state/model-config.json`'s `warm_model` WINS over the `--model` CLI flag, which is
    the fallback/default. Called fresh at every warm-session SPAWN (not just process start), so a dial
    change takes effect the next time the session naturally winds down and respawns; the cockpit's
    "apply now" (a graceful restart) forces it promptly for an already-warm session. Tolerant: a
    missing/corrupt config file or an unrecognized `warm_model` falls back to the CLI flag, logging
    which source won either way (observability, not silent drift)."""
    cfg = model_config.load(state_dir)
    warm = cfg.get("warm_model")
    if warm:
        canon = model_config.canonical(warm)
        if canon:
            log(f"• warm model: {canon} (source: state/model-config.json)")
            return canon
        log(f"! model-config.json warm_model {warm!r} not recognized — falling back to --model")
    if cli_model:
        log(f"• warm model: {cli_model} (source: --model CLI flag)")
    else:
        log("• warm model: CLI default (no --model flag, no state/model-config.json warm_model)")
    return cli_model


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
through the active store's mapping (store/<backend>/mapping.md) per reminders-policy.md. On the notion
backend, ALSO make the ack durable the way that mapping's Outbox section specifies: journal it first via
`python seneschal/scripts/outbox.py ack --reminder-id <ref>` (idempotent — the write-behind outbox flushes
it to the store even if this session winds down mid-write); filesystem backends write locally and
atomically, so they skip the outbox. Set those FIELDS
directly; do NOT just flip the one-tap ack affordance (reminder seeds consume and reset it, so it is not
the durable record; last_acknowledged is what the EOD wrap counts). Then the next seeded nudge
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
is delivered — same effect as reseneschald, no dropped message.
Delegating to Fable: you ALWAYS own this conversation — delegation is a subprocess call-out, never a
handoff. `python seneschal/scripts/fable_delegate.py "<task>"` runs a Fable-5 one-shot (seeded with recent
thread context) and prints its answer for you to read and use in your own reply, still fully in
character and still under the act-low/ask-high gate (an outbound/destructive result STILL drafts-and-
holds — delegating never bypasses the approval gate). Use it when ANY of: (1) you see a
"[router hint: ... FABLE-LEVEL ...]" line above a message — a hint only, your judgment governs; (2) your
own judgment mid-turn — deep multi-factor synthesis, long-horizon planning, or hard multi-step debugging
you'd plausibly do worse on than Fable would; (3) a "[force-fable: ...]" directive line above a message —
that one is a MUST-delegate ({owner} typed `!fable` or ticked "Send to Fable" in the cockpit), bypassing
the router classifier but never the gate. The script itself enforces the max-routable-model ceiling
(`state/model-config.json`) and REFUSES with a clear message when the ceiling isn't Fable-tier — accept
that gracefully: say so plainly to {owner} and handle the turn yourself, don't treat the refusal as a bug
to route around.
Oikonomos, the budget governor (`state/governor-config.json` + `scripts/governor.py`), sits behind
Fable delegation too: `fable_delegate.py` may ALSO refuse on a daily/per-conversation Fable quota,
delegation concurrency, or Fable's own token budget, each with a clear reason and when it resets — relay
that refusal to {owner} honestly, exactly like the ceiling refusal, and never try to route around it or
retry the call yourself. {thread}
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


def build_seed_prompt(identity, template: str | None = None) -> str:
    """SEED_PROMPT_TEMPLATE with the identity tokens substituted — the seed's sibling of
    build_slots. Like a slot prompt (and unlike the grounding), the rendered seed is handed to
    `claude -p` verbatim and never .format()ed, so values go in RAW — no brace doubling."""
    out = SEED_PROMPT_TEMPLATE if template is None else template
    for token, value in _identity_tokens(identity).items():
        out = out.replace(token, value)
    return out


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
# never raises and yields the generic defaults when it's absent, so GROUNDING/SLOTS/SEED_PROMPT
# render byte-identical to the pre-identity hardcoded prose on an unconfigured install.
IDENTITY = load_identity()
GROUNDING = _render_grounding(GROUNDING_TEMPLATE, IDENTITY)
SLOTS = build_slots(IDENTITY)
SEED_PROMPT = build_seed_prompt(IDENTITY)


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
            "arm": "triage",  # distinguishes this row from the fable arm's below (both share the log)
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


FABLE_HINT_LINE = (
    "[router hint: this message looks FABLE-LEVEL — deep synthesis / long-horizon planning / hard "
    "multi-step debugging — consider delegating to Fable via fable_delegate.py if it genuinely "
    "warrants it; this is a hint, your own judgment still governs]"
)


def fable_arm_classify(state_dir: str, channel: str, text: str, log) -> str | None:
    """The router's **fable arm** (v3, cockpit-spec.md "Model dials & Fable delegation"): once the live
    `max_routable_model` ceiling admits Fable, classify this inbound escalation standard vs fable-level
    and log the verdict to router-log.jsonl (`arm: "fable"`, alongside the triage arm's rows above).

    Unlike the triage arm, a "fable" verdict here is NOT purely observational: this returns a short hint
    LINE (never a command) for the caller to queue onto `DaemonState.fable_hints`, which `drainer_task`
    best-effort-attaches to the next prompt it builds. **The ceiling gate means this never even calls
    Ollama when `max_routable_model` isn't Fable-tier** — "the fable arm doesn't even run" (ruling 4).
    Fail-open throughout: any exception here just skips the hint, exactly like the triage arm above."""
    try:
        cfg = model_config.load(state_dir)
        if not model_config.admits_fable(cfg.get("max_routable_model")):
            return None
        import router  # local, stdlib-only; imported lazily, same as shadow_classify
        verdict = router.classify_fable(text)
        row = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "channel": channel,
            "text_preview": text[:80],
            "arm": "fable",
            "verdict": verdict.get("verdict"),
            "confidence": verdict.get("confidence"),
            "reason": verdict.get("reason"),
            "model": verdict.get("model"),
        }
        with open(router_log_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        log(f"• router (fable arm): {row['verdict']} conf={row['confidence']} — {row['reason']}")
        if row["verdict"] == "fable":
            return FABLE_HINT_LINE
    except Exception as e:  # noqa: BLE001 — the fable arm must never break a turn either
        log(f"! router fable-arm classify skipped: {e}")
    return None


# --------------------------------------------------------------------------- force-route (!fable)

# A leading "!fable" (any case, an optional ":"/"," and whitespace after) on ANY inbound channel —
# Telegram/Discord typed literally, or synthesized by cockpit_task.on_chat_send when the browser's
# "Send to Fable" toggle (force_fable) is set — force-routes this turn. Bypasses the router classifier
# entirely; NEVER the approval gate (cockpit-spec.md ruling 4 / GROUNDING's delegation section).
FORCE_FABLE_PREFIX_RE = re.compile(r"^\s*!fable\b[:,]?\s*", re.IGNORECASE)
FORCE_FABLE_TRIGGER = "!fable"

FORCE_FABLE_DIRECTIVE = (
    "[force-fable: the owner force-routed this turn (!fable / the cockpit's \"Send to Fable\" toggle) — you "
    "MUST attempt to delegate it to Fable via fable_delegate.py. If the max-routable-model ceiling "
    "refuses the call, accept that gracefully: say so plainly and handle the turn yourself. Force-route "
    "bypasses the router classifier, never the approval gate — any outbound/destructive result from "
    "the delegate's answer still drafts-and-holds.]"
)


def strip_force_fable(text: str) -> tuple[bool, str]:
    """Detect + strip a leading `!fable` force-route prefix. Returns (forced, remaining_text) — pure and
    unit-testable. Matches only a LEADING token (after any reply-to/attachment synthesis already ran),
    so `!fable draft the Q3 plan` triggers but a `!fable` buried mid-sentence does not — the
    deliberately narrow, unambiguous case."""
    m = FORCE_FABLE_PREFIX_RE.match(text or "")
    if not m:
        return False, text or ""
    return True, text[m.end():].strip()


def apply_force_route(channel: str, text: str, log) -> str:
    """If `text` carries the force-route prefix, strip it and replace it with the explicit MUST-delegate
    directive the warm session's grounding tells it to honor (GROUNDING's delegation section) — a pure
    text transform applied once at enqueue time (see `_enqueue_inbound`), so it needs no persisted-queue
    schema change and survives a restart exactly like any other inbound text. A bare `!fable` with
    nothing after it still produces a valid (if task-less) directive; the warm session can ask the
    owner what they meant."""
    forced, clean = strip_force_fable(text)
    if not forced:
        return text
    log(f"• force-route: '!fable' detected on a {channel} message — directive injected")
    return f"{FORCE_FABLE_DIRECTIVE}\n\n{clean}" if clean else FORCE_FABLE_DIRECTIVE


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

    if channel == "cockpit":
        # Cockpit-origin turns are "delivered" via the live transcript stream (chat.events teed to
        # BOTH the ring buffer and the connected pipe client, in drainer_task) rather than a separate
        # outbound push — there's no third-party API to call for a browser tab. Always succeeds so
        # continuity (append_thread) records the reply and the durable queue pops it; a reconnecting
        # cockpit backfills from state/warm-transcript.jsonl regardless of whether a client happened
        # to be attached mid-turn — never lossy, matching every other cockpit-pipe failure mode.
        return True

    def send_once() -> dict:
        if channel == "discord":
            return send_discord(reply, args.discord_env) or {}
        return send_telegram(reply, args.telegram_env) or {}

    for attempt in range(retries + 1):
        res = send_once()
        if res.get("ok"):
            # Remember what this message was, so a reaction to it later has something to point at — a
            # 👍 on "want me to send it?" only reads as "yes" if we know what the owner 👍'd.
            if channel == "telegram":
                record_sent_message(args.state_dir, res, "reply", reply)
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
                 mcp_configs: list | None = None):
        self.claude_bin = claude_bin
        self.model = model
        self.permission_mode = permission_mode
        self.log = log
        self.mcp_configs = mcp_configs or []  # Notion + Slack configs threaded into this session
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
        if self.mcp_configs:
            cmd += ["--mcp-config", *self.mcp_configs]
        if self.model:
            cmd += ["--model", self.model]
        self.proc = subprocess.Popen(
            cmd, cwd=REPO_ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # Force UTF-8 both ways — claude's stream-json output is UTF-8; without this the daemon
            # decodes it with the Windows ANSI codepage (cp1252) and mangles —, emoji, etc. (mojibake).
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=NO_WINDOW,
        )

    def send(self, text: str, on_event=None) -> str | None:
        """Send one user turn; return the assistant's reply text (or None if the session died). If given,
        `on_event(ev)` is invoked for every parsed stream-json line (including `system`/`result`) —
        the cockpit pipe's transcript tee hooks in here (see presence.drainer_task's `_make_stream_tee`).
        Runs on a worker thread (drainer_task calls this via asyncio.to_thread), so `on_event` must be
        thread-safe; a raising callback is swallowed — the read loop must never die because a tee did."""
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
        for raw in self.proc.stdout:  # blocks line-by-line until this turn's result event
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if on_event is not None:
                try:
                    on_event(ev)
                except Exception:  # noqa: BLE001 — a tee failure must never break the turn
                    pass
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
        self.model = None
        self.turns = 0
        self.closed = False

    def start(self) -> None:
        self.closed = False
        self.turns = 0

    def send(self, text: str, on_event=None) -> str:
        self.turns += 1
        reply = f"[stub reply #{self.turns} to: {text.splitlines()[-1][:50]}]"
        if on_event is not None:
            try:  # exercise the tee path in offline/test runs too, harmlessly
                on_event({"type": "result", "is_error": False, "result": reply})
            except Exception:  # noqa: BLE001
                pass
        return reply

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
    (`warm_busy`) so a peek's store reads never stampede in parallel with a slot's or a chat turn's;
    a skipped peek just runs on the next loop once the cadence is still due. Chat is never delayed —
    only the peek waits. Also skipped — without consuming the cadence — while an interactive
    /assistant session is live (`sentinel.session_is_live`): a human is already looking, so the
    peek is redundant this cycle."""
    if args.peek_interval_min <= 0 or not (args.watch_prompt or args.watch_cmd):
        return False
    if warm_busy or (children is not None and heavy_run_in_flight(children)):
        return False  # a chat turn or another headless run is active — don't add a second concurrent reader
    if session_is_live(state_dir, datetime.now(timezone.utc)):
        return False  # a human is actively engaged in a live /assistant session — the peek is redundant; skip
                      # this cycle (it resumes on the next cadence once the session ages out of the TTL)
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
            cfgs = active_mcp_configs(args)
            if cfgs:
                cmd += ["--mcp-config", *cfgs]
            if args.watch_model:
                cmd += ["--model", args.watch_model]
            proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=child_env(), creationflags=NO_WINDOW)
        else:
            proc = subprocess.Popen(args.watch_cmd, shell=True, cwd=REPO_ROOT, env=child_env(), creationflags=NO_WINDOW)
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
    now_local = datetime.now()  # machine-local wall clock (assumed to match the owner's timezone)
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
        cfgs = active_mcp_configs(args)
        if cfgs:
            cmd += ["--mcp-config", *cfgs]
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


def maybe_seed_day(state_dir: str, args, log, children: list | None = None,
                   warm_busy: bool = False, slot_children: dict | None = None) -> None:
    """Spawn the once-per-local-day Reminders SEED run — the date-rollover trigger that retired the four
    fixed reminder slots. Fires on the first tick of each new owner-local date (guarded by
    ``slots.json[SEED_SLOT_NAME]``), NOT at a fixed HH:MM — so a machine asleep through midnight still
    seeds on wake, with no ``classify_slots`` catch-up cliff that could silently skip the daily reset.

    The seed reads the ⏰ tracker, runs the daily reset, and queues every due row's exact-time nudges for
    the day (``reminders_seed.py``); the ~5 s delivery tick then fires each at its due minute. Reuses the
    slot lifecycle: the run is registered in ``slot_children`` under ``SEED_SLOT_NAME`` and
    ``reap_finished_slots`` stamps it only on a clean exit (retrying a crashed seed, giving up after
    ``SLOT_MAX_RETRIES``). Held while the warm chat session is mid-turn or another headless run is in
    flight — its store read-burst must not overlap — exactly like ``maybe_run_slots``; a deferred seed
    stays unstamped and retries next loop.

    Unlike slot fire-TIMES (machine-local by design), the rollover is DATE logic, so it follows the
    owner's calendar via ``local_now()`` (rule 5 — tz_common when configured, machine-local fallback)."""
    if args.no_slots or getattr(args, "no_seed_day", False) or args.stub_brain or args.fake_inbox is not None:
        return
    if warm_busy:
        return  # warm chat mid-turn — hold the seed so its reads don't overlap chat's; retry next loop
    if slot_children is not None and SEED_SLOT_NAME in slot_children:
        return  # already running
    now_local = local_now()  # owner-tz date — the rollover trigger follows the owner's calendar (rule 5)
    today = now_local.strftime("%Y-%m-%d")
    fired = load_json(os.path.join(state_dir, "slots.json"), {})
    if not isinstance(fired, dict):
        fired = {}
    if fired.get(SEED_SLOT_NAME) == today:
        return  # already seeded this local day (reap_finished_slots stamped it on the seed's clean exit)
    if children is not None and heavy_run_in_flight(children):
        log(f"• seed '{SEED_SLOT_NAME}' deferred (another headless run in flight) — retries next loop")
        return
    model = args.slot_model or args.model
    stamp = local_stamp()
    prompt = (f"{SEED_PROMPT} (Authoritative current local date/time: {stamp}, the owner's configured "
              f"timezone — base every date on this, not a UTC clock.)")
    cmd = [args.claude_bin, "-p", prompt, "--permission-mode", args.permission_mode]
    cfgs = active_mcp_configs(args)
    if cfgs:
        cmd += ["--mcp-config", *cfgs]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=child_env(), creationflags=NO_WINDOW)
        if children is not None:
            children.append(proc)
        if slot_children is not None:
            slot_children[SEED_SLOT_NAME] = proc  # reaped on exit — stamped only if it finishes clean
        log(f"• seed '{SEED_SLOT_NAME}' launched (day rollover → {today})")
    except Exception as e:  # noqa: BLE001
        log(f"! seed '{SEED_SLOT_NAME}' launch failed: {e}")


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
    return poll_telegram(args.telegram_env, args.state_dir, commit=True, timeout=args.poll_timeout,
                         download_dir=os.path.join(args.state_dir, TELEGRAM_INBOX_DIR))


# The owner's default reaction vocabulary. Overridable per-machine via state/telegram-reactions.json
# (seed: the .example); these are the fallback so the feature works with no config at all.
#
# Several emoji per intent on purpose. Telegram only lets you react with emoji from its own allowed set
# (the Bot API's ReactionTypeEmoji list), and the natural picks for snooze/hold/elaborate — ⏰ 🤚 ❔ — are
# verifiably NOT in it, so the picker will never offer them. They're kept (harmless, and they record the
# intended meaning); the in-set aliases beside each are the ones that can actually fire.
DEFAULT_REACTION_INTENTS = {
    "👍": "ack",                                             # yes / confirm / accept
    "❤": "liked",                                            # warmth about the reply itself — no action
    "👎": "reject",                                           # no / drop a held draft / don't accept
    "⏰": "snooze", "😴": "snooze", "🥱": "snooze",             # more time; on a nudge, snooze it
    "🤚": "hold", "🤝": "hold", "🙏": "hold",                   # wait >= 1 day, don't resurface unless asked
    "❔": "elaborate", "✍": "elaborate", "🤔": "elaborate",     # explain / tell me more
}
REACTION_DEFAULT_INTENT = "note"  # anything unmapped: thread as context, take no action
REACTIONS_CONFIG = "telegram-reactions.json"
QUOTE_CHARS = 300  # how much of a reacted-to message we quote back


def _norm_emoji(e: str) -> str:
    """Drop the variation selector so ❤ and ❤️ are the same key — Telegram is inconsistent about it, and
    a mapping that silently missed on an invisible codepoint would be a miserable thing to debug."""
    return (e or "").replace("️", "").replace("︎", "")


def load_reaction_intents(state_dir: str) -> dict:
    """The emoji → intent map, the owner's to edit. Fail-open to the defaults on absent/broken config."""
    data = load_json(os.path.join(state_dir, REACTIONS_CONFIG), None)
    table = (data or {}).get("reactions") if isinstance(data, dict) else None
    if not isinstance(table, dict) or not table:
        return {_norm_emoji(k): v for k, v in DEFAULT_REACTION_INTENTS.items()}
    return {_norm_emoji(k): v for k, v in table.items() if isinstance(v, str)}


def reaction_context(state_dir: str) -> dict:
    """Loaded once per poll batch (only when a reaction is actually in it), not once per message."""
    return {"intents": load_reaction_intents(state_dir), "sent": load_message_map(state_dir)}


def _truncate(s: str, cap: int = QUOTE_CHARS) -> str:
    s = (s or "").strip()
    return s if len(s) <= cap else s[:cap].rstrip() + "…"


def reaction_intent(m: dict, ctx: dict | None) -> str:
    intents = (ctx or {}).get("intents") or {_norm_emoji(k): v for k, v in DEFAULT_REACTION_INTENTS.items()}
    return intents.get(_norm_emoji(m.get("emoji") or ""), REACTION_DEFAULT_INTENT)


def ackable_nudge(m: dict, ctx: dict | None, now: datetime | None = None) -> str | None:
    """The ⏰ row id a reaction should ack on its own, or None to leave it to the warm session.

    Phase B's whole safety story is in this predicate, so it is deliberately narrow. ALL of:
      * the intent maps to `ack`;
      * the reacted-to message is a **tracked nudge** carrying a `reminder_id` (not a chat reply — a 👍
        on a question is a judgment call and belongs to the LLM tier, per §3.4B);
      * that nudge went out **today, local**.

    The day gate is the one that matters. Reminder rows reset daily and an ack stamps *today's* date, so
    a 👍 on yesterday's nudge would mark today Done — for meds, that is exactly the failure that must not
    happen. Anything that doesn't clear all three is observe-only: the warm session reads it and decides.
    Failing to auto-ack costs a little manual work; auto-acking wrongly costs the owner a dose."""
    if reaction_intent(m, ctx) != "ack":
        return None
    sent = ((ctx or {}).get("sent") or {}).get(str(m.get("message_id"))) or {}
    if sent.get("kind") != "nudge" or not sent.get("reminder_id"):
        return None
    try:
        if ra.local_today(parse_iso(sent["sent_at"])) != ra.local_today(now):
            return None
    except Exception:  # noqa: BLE001 — anything unreadable here means we can't PROVE it's today's
        return None    # nudge, and "don't act" is the safe side of that doubt
    return sent["reminder_id"]


def ack_reminder_by_reaction(state_dir: str, reminder_id: str, log) -> bool:
    """Run the normal ack path for a 👍'd nudge: drop the obsolete re-nudges (+ record the durable local
    ack) and — on the **notion** backend only — journal the store `done` write to the write-behind
    outbox for the next LLM turn to flush.

    Exactly what the chat ack path does — deliberately the same calls rather than a private shortcut,
    so this can't drift from it. Act-low: it's the owner's own reminder, their own content. All calls
    are idempotent, so a warm session that acks again on top of this is harmless.

    The outbox leg is gated on ``store_backend_active() == "notion"`` because the outbox is a
    Notion-only mechanism (store/notion/mapping.md, 'Outbox — durable act-low writes'): filesystem
    backends (obsidian/markdown) write locally/atomically and never touch it. On those backends the
    dequeue leg still records the ack to the durable local ledger (state/acks.json) — the fire path's
    gate — and this returns **False** so the reaction line asks the warm session to perform the
    `store-update` itself (acks persist to the store, never just to chat; the calls are idempotent,
    so the belt-and-suspenders overlap is harmless)."""
    cmds = [
        ([sys.executable, os.path.join(SCRIPT_DIR, "reminders_dequeue.py"),
          "--reminder-id", reminder_id, "--state-dir", state_dir], "dequeue"),
    ]
    backend = store_backend_active()
    store_write_journaled = backend == "notion"
    if store_write_journaled:
        cmds.append(([sys.executable, os.path.join(SCRIPT_DIR, "outbox.py"), "--state-dir", state_dir,
                      "ack", "--reminder-id", reminder_id], "outbox"))
    else:
        log(f"• reaction-ack: outbox skipped (store backend {backend!r}) — local ledger recorded; "
            "the warm session performs the store-update")
    ok = True
    for cmd, label in cmds:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
            if proc.returncode != 0:
                ok = False
                log(f"! reaction-ack {label} failed ({proc.returncode}): {proc.stderr.strip()[:160]}")
        except Exception as e:  # noqa: BLE001 — a failed ack must not take the daemon down
            ok = False
            log(f"! reaction-ack {label} errored: {e}")
    return ok and store_write_journaled


def _reaction_line(m: dict, ctx: dict | None, acked: bool = False) -> str:
    """What the warm session reads when the owner reacts to one of the assistant's messages.

    Observe-first: this NAMES the intent and quotes what they reacted to, then lets the warm session
    act in context. The daemon interprets nothing here — the single automated path (Phase B) is the
    reminder ack, and when it has already run, the line says so, so the assistant acknowledges the
    owner instead of re-acking."""
    emoji = m.get("emoji") or "?"
    intent = reaction_intent(m, ctx)
    sent = ((ctx or {}).get("sent") or {}).get(str(m.get("message_id"))) or {}
    quoted = _truncate(sent.get("text", ""))
    # Beyond the map's window, or sent before this feature existed.
    target = f'to: "{quoted}"' if quoted else "to an earlier message"
    line = f"[the owner reacted {emoji} (= {intent}) {target}"
    if acked:
        line += " — I've already run the ack for you (⏰ row marked Done, re-nudges dropped); no need to repeat it"
    return line + "]"


def _human_size(n) -> str:
    """Bytes as a short human string for an attachment descriptor."""
    if not isinstance(n, (int, float)) or n <= 0:
        return "unknown size"
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def telegram_inbound_text(m: dict, ctx: dict | None = None, acked: bool = False) -> str:
    """The line the warm session sees for one inbound message.

    Plain text passes through untouched. An attachment becomes a synthesized descriptor — where the file
    landed, or why it didn't — plus any caption, so a file the owner sends is something the assistant can
    actually open and answer. Before this, a message with no `text` reached the warm session as an empty
    string and was dropped on the floor: that's how an inbound export zip once looked ignored. A
    swipe-reply carries what the owner is replying to, so they never have to restate it. A reaction names
    its intent and quotes what they reacted to (`ctx` = reaction_context(), loaded once per batch).

    The daemon describes; it never decides. It runs no tool on an inbound file and writes no canned
    apology — the warm session reads the descriptor and responds in the assistant's own voice."""
    if m.get("kind") == "reaction":
        return _reaction_line(m, ctx, acked)
    line = _attachment_or_text(m)
    reply_to = (m.get("reply_to") or "").strip()
    # An empty line is dropped by telegram_task, so don't let a bare reply-prefix stand in for a message.
    if reply_to and line:
        return f'(replying to: "{reply_to}") {line}'
    return line


def _attachment_or_text(m: dict) -> str:
    """The message body itself — the owner's text, or a descriptor of the file they sent."""
    text = (m.get("text") or "").strip()
    att = m.get("attachment")
    if att is None:  # `is None`, not falsy: an empty record still means a file was there to describe
        return text
    kind = att.get("kind") or "file"
    # The name is the sender's string and it's about to be read by an LLM, so it goes through the same
    # scrub as the write path (drops brackets/newlines that could dress themselves up as instructions).
    # A photo carries no name at all — say "photo", not `photo "photo"`.
    name = safe_filename(att.get("file_name"), "")
    desc = f'{kind} "{name}"' if name else kind
    if att.get("too_large"):
        body = (f"[attachment: {desc} ({_human_size(att.get('file_size'))}) NOT downloaded — over "
                f"Telegram's ~20 MB bot-API limit; they'd need to drop it on the machine instead]")
    elif att.get("local_path"):
        body = f"[attachment: {desc} saved to {att['local_path']}]"
    else:
        body = f"[attachment: {desc} — download failed ({att.get('error') or 'unknown error'})]"
    # A caption rides with the file; `text` is empty on a media message, but keep it if both ever appear.
    trailer = " ".join(p for p in ((m.get("caption") or "").strip(), text) if p)
    return f"{body} {trailer}".strip()


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
        self.just_started = True             # cleared by the first non-empty poll — gates the backlog ack
        self.crashed = False                 # a task died unexpectedly — exit non-zero so the task
                                             # scheduler's restart-on-failure brings us back
        self.iterations = 0                  # telegram poll cycles (bounds test runs)
        self.cockpit_hub = None               # cockpit_pipe.PipeHub — the sole pipe client, set by
                                              # cockpit_task; None whenever the pipe is disabled/down
        self.loop = None                      # the running event loop, set once in main_async — lets a
                                              # worker-thread tee (the warm session's stdout reader)
                                              # hand a broadcast back onto the loop via call_soon_threadsafe
        self.fable_hints: list = []            # v3: router fable-arm hint LINES awaiting a prompt to ride
                                              # into (FIFO, best-effort — see fable_arm_classify /
                                              # drainer_task; never persisted, purely advisory)

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


# --------------------------------------------------------------------------- cockpit pipe wiring
#
# Small glue between the reactive core's DaemonState and cockpit_pipe.py's transport-agnostic
# PipeHub/ring-buffer/inbox helpers (see cockpit_task, below, and seneschal/docs/cockpit-spec.md). Every
# function here is fail-open: a cockpit push/tee failure is logged and swallowed, never raised into
# the chat loop or reminder firing (asyncio-daemon-design.md's non-negotiable invariant for this task).

def _status_snapshot(state: DaemonState, args) -> dict:
    """The daemon's own view of itself for the cockpit pipe's status frames / status.get replies —
    turn-in-flight, warm-session up/down, current model, inbound queue depth. Cheap and side-effect
    free; called both on push (state-change points below) and on-demand (a fresh cockpit connection's
    status.get, handled inside PipeHub).

    `model` prefers the LIVE session's own model (set at spawn by `resolve_warm_model`); when no session
    is up (idle), it falls back to a fresh (tolerant, cheap) read of `state/model-config.json`'s
    `warm_model` — so the cockpit's dial badge stays honest even while idle — and only then to the
    `--model` CLI flag, matching `resolve_warm_model`'s own precedence (cockpit-spec.md v3: "the status
    frame ... carr[ies] the active warm model")."""
    model = getattr(state.session, "model", None)
    if not model:
        model = model_config.canonical(model_config.load(args.state_dir).get("warm_model") or "") or args.model
    return {
        "session_up": state.session is not None,
        "turn_in_flight": state.session_busy,
        "model": model,
        "queue_depth": len(state.pending),
    }


def _push_cockpit_status(state: DaemonState, args) -> None:
    """Best-effort push of a status frame to the connected cockpit client (a silent no-op if none is
    connected — see PipeHub.broadcast). EVENT-LOOP callers only — a worker thread must not touch
    state.cockpit_hub's asyncio.Queue directly; there is no thread-safe status push because every
    status-change call site in this module already runs on the loop."""
    hub = state.cockpit_hub
    if hub is None:
        return
    try:
        hub.broadcast(cockpit_pipe.status_frame(**_status_snapshot(state, args)))
    except Exception:  # noqa: BLE001 — a cockpit push must never affect the chat loop
        pass


def _tee_chat_event(state: DaemonState, args, log, event: dict) -> None:
    """Fan a digestible chat.event out to BOTH the ring buffer (always — so a reconnecting cockpit can
    backfill) and the live pipe client (best-effort). EVENT-LOOP callers only; see
    `_tee_chat_event_threadsafe` for the worker-thread sibling used inside the warm session's blocking
    stdout reader."""
    try:
        cockpit_pipe.append_transcript_event(args.state_dir, event)
    except Exception as e:  # noqa: BLE001 — fail-open: a tee failure must never break a chat turn
        log(f"! cockpit transcript tee failed: {e}")
    hub = state.cockpit_hub
    if hub is not None:
        try:
            hub.broadcast(event)
        except Exception:  # noqa: BLE001
            pass


def _tee_chat_event_threadsafe(state: DaemonState, args, log, event: dict) -> None:
    """Thread-safe sibling of `_tee_chat_event` — safe to call from inside asyncio.to_thread (the warm
    session's blocking stdout reader lives on a worker thread). The ring-buffer append is plain,
    lock-guarded file I/O (fine from any thread); the live broadcast hops back onto the event loop via
    PipeHub.broadcast_threadsafe."""
    try:
        cockpit_pipe.append_transcript_event(args.state_dir, event)
    except Exception as e:  # noqa: BLE001
        log(f"! cockpit transcript tee failed: {e}")
    hub = state.cockpit_hub
    if hub is not None:
        hub.broadcast_threadsafe(event, state.loop)


def _governor_meter_turn_usage(args, log, model: str | None, usage) -> None:
    """Oikonomos (order 15, advisor-chain.md): meter one turn's token spend into the governed ledger and
    self-push at most one Telegram line per knob per ALERT_REALERT_HOURS when a rail's alert-at-%
    crosses. `usage` is whatever the claude-CLI's terminal `result` event supplied (cockpit_pipe.
    build_chat_event_from_stream forwards it verbatim, optional) — summed across every token-count field
    it carries so a cache-heavy turn still meters its real cost. Fail-open throughout: a governor hiccup
    must never break a chat turn, so every step here is best-effort (same posture as the cockpit
    transcript tee just above)."""
    if not isinstance(usage, dict):
        return
    tokens = 0
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            tokens += val
    if tokens <= 0:
        return
    model_key = model or "unknown"
    try:
        governor.append_spend(args.state_dir, "tokens", model=model_key, tokens=int(tokens))
        for alert in governor.due_alerts(args.state_dir, model_key):
            res = send_telegram(alert["text"], args.telegram_env)
            if res and res.get("ok"):
                governor.record_alert_sent(args.state_dir, alert["knob"])
    except Exception as e:  # noqa: BLE001 — fail-open: metering must never break a chat turn
        log(f"! governor turn-usage metering failed: {e}")


def _make_stream_tee(state: DaemonState, args, log, channel: str, turn_id: str):
    """Build the `on_event` callback handed to WarmSession.send() for one turn: converts each raw
    claude-CLI stream-json line into a digestible chat.event (dropping the uninteresting ones — see
    cockpit_pipe.build_chat_event_from_stream), stamps it with `turn_id` (the SAME id the turn_started
    event carries — see drainer_task), and tees it. Since the protocol itself carries no turn
    correlator, `turn_id` is what lets the cockpit chat pane group turn_started/assistant_output/
    tool_use/turn_done events into one turn without relying on the (also-true, but implicit) fact that
    chat turns are strictly serialized. Runs on the worker thread reading the session's stdout, so it
    uses the thread-safe tee sibling throughout — including the governor metering below, which does its
    own (blocking-but-off-the-event-loop) Telegram send on an alert."""
    def _on_event(ev: dict) -> None:
        model = getattr(state.session, "model", None) or args.model
        chat_ev = cockpit_pipe.build_chat_event_from_stream(ev, source=channel, model=model)
        if chat_ev is not None:
            chat_ev["turn_id"] = turn_id
            _tee_chat_event_threadsafe(state, args, log, chat_ev)
            if chat_ev.get("kind") == "turn_done":
                _governor_meter_turn_usage(args, log, model, chat_ev.get("usage"))
    return _on_event


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
    queue. Force-route (!fable) detection, thread-append, + Router shadow all happen here, once per
    message — NOT in the drainer's retry path, or a message that fails delivery would be re-appended/
    re-classified/re-force-routed on every retry."""
    if not new_inbound:
        return
    # Force-route is a pure, synchronous text transform (a regex match) — cheap enough to run inline,
    # before persisting, so the directive is baked into the SAME text that gets threaded/queued/retried
    # (no persisted-queue schema change, survives a restart for free). Unlike the classifiers below, this
    # never touches Ollama, so it can't reintroduce the "persist first" latency concern that motivates
    # deferring classification until after the save.
    new_inbound = [(ch, apply_force_route(ch, t, log), a) for ch, t, a in new_inbound]
    # Persist + wake the drainer FIRST: the wire offset is already committed, so until this save lands
    # a hard kill silently loses the batch. The (slow — seconds on a cold Ollama) shadow classification
    # happens after, and the drainer can already be mid-turn while it runs.
    for ch, t, _ in new_inbound:
        append_thread(args.state_dir, "owner", t)
    state.pending.extend(new_inbound)
    save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
    state.pending_event.set()
    _push_cockpit_status(state, args)  # queue depth changed — cheap, best-effort, event-loop call site
    if args.router_mode != "off":
        for ch, t, _ in new_inbound:
            # Shadow only (phase 1): classify + log, zero behavior change. In a thread because a cold
            # Ollama can take seconds, and inbound intake must never stall the loop.
            await asyncio.to_thread(shadow_classify, args.state_dir, ch, t, log)
            # The fable arm (v3): gated on the LIVE ceiling inside fable_arm_classify itself (never
            # calls Ollama when max_routable_model isn't Fable-tier). A "fable" verdict queues a hint
            # LINE that drainer_task best-effort-attaches to the next prompt it builds — FIFO, advisory
            # only, never persisted (losing the race just means no hint, never a broken turn).
            hint = await asyncio.to_thread(fable_arm_classify, args.state_dir, ch, t, log)
            if hint:
                state.fable_hints.append(hint)


async def _inbound_lines(args, log, msgs: list) -> list:
    """Turn a fetched batch into the lines the warm session will read, running Phase B's nudge-ack on
    the way through.

    The reaction config + sent-message map are read only when a reaction is actually in the batch, so
    the overwhelmingly common text-only poll touches no extra files. The ack itself is two short
    subprocesses, so it goes to a thread — inbound intake must never stall the loop."""
    if not any(m.get("kind") == "reaction" for m in msgs):
        return [telegram_inbound_text(m) for m in msgs]
    ctx = reaction_context(args.state_dir)
    lines = []
    for m in msgs:
        acked = False
        reminder_id = ackable_nudge(m, ctx)
        if reminder_id:
            acked = await asyncio.to_thread(ack_reminder_by_reaction, args.state_dir, reminder_id, log)
            log(f"reaction-ack {'ok' if acked else 'FAILED'} for ⏰ {reminder_id}")
        lines.append(telegram_inbound_text(m, ctx, acked))
    return lines


async def _maybe_backlog_ack(state: DaemonState, args, log, messages: list) -> None:
    """On the FIRST non-empty poll after (re)start, if a burst is waiting, say so before answering it.

    Seen for real: after a `reseneschald` the daemon drains a backlog serially and quietly, so from the
    owner's side only the first reply appears and the rest look lost — they re-forward everything. One
    line up front costs a send and buys the knowledge that the batch landed. Then it's answered in order.

    Deliberately narrow: only right after a cold start, only on a burst (N >= 2), never on the
    steady-state single-message path — no chatter in normal use. Act-low (the owner's own content, their
    own chat) and best-effort: a failed ack must not cost us the backlog it was announcing."""
    if not state.just_started or not messages:
        return
    state.just_started = False  # first non-empty poll is the only shot, ack or not
    if len(messages) < 2:
        return
    line = f"Back up — got your {len(messages)} messages, working through them now."
    if not await asyncio.to_thread(deliver_reply, "telegram", line, args, log):
        log("! backlog ack send failed — answering the backlog anyway")
        return
    append_thread(args.state_dir, "assistant", line)


async def telegram_task(state: DaemonState, args, fake_queue: list, log) -> None:
    """Inbound Telegram: long-poll (blocking, in a worker thread), enqueue, repeat. The long-poll is
    the reason this is its own task — it no longer paces anything else. While a control is waiting to
    apply, the poll window drops to 2 s so a graceful reload lands promptly after the owner stops typing."""
    inbox = os.path.join(args.state_dir, TELEGRAM_INBOX_DIR)
    while not state.stop.is_set():
        if args.fake_inbox is not None:
            res = next_messages(args, fake_queue)
        else:
            timeout = 2 if state.control_pending else args.poll_timeout
            res = await asyncio.to_thread(poll_telegram, args.telegram_env, args.state_dir, True,
                                          timeout, inbox)
        # Even if stop was set while we were parked on the wire, PROCESS the result first — the poll
        # already committed the offset for anything it fetched, so skipping here would lose messages.
        # Enqueued-but-unanswered items are persisted and the successor picks them up (invariant 1).
        if res.get("ok"):
            msgs = res.get("messages", [])
            await _maybe_backlog_ack(state, args, log, msgs)
            new_inbound = [("telegram", t, 0)
                           for t in await _inbound_lines(args, log, msgs) if t]
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
                    clear_session_heartbeat(args.state_dir)  # session no longer live — reminders resume
                    _push_cockpit_status(state, args)  # session_up flipped false — tell the cockpit
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
        # v3: best-effort attach one pending router fable-arm hint (if any arrived in time — see
        # fable_arm_classify / _enqueue_inbound) to THIS turn's prompt only. Deliberately a LOCAL var,
        # not written back to state.pending[0] — the persisted/retry text (and the delivery-failure
        # requeue comparison below) must stay the hint-free original, so a retry doesn't duplicate or
        # go stale on a hint meant for the first attempt.
        prompt_text = f"{state.fable_hints.pop(0)}\n\n{text}" if state.fable_hints else text
        if state.session is None:
            state.session = make_session()
            await asyncio.to_thread(state.session.start)
            prompt = GROUNDING.format(channel=channel.capitalize(), now=local_stamp(),
                                      thread=thread_tail(args.state_dir), msg=prompt_text)
        else:
            # Refresh the clock every turn — a warm session only saw the date once, at grounding,
            # so a long-lived one would drift across midnight.
            prompt = (f"(For reference, the authoritative current local time is {local_stamp()} "
                      f"— the owner's configured timezone.)\n\n{prompt_text}")
        # Register this warm session in the session registry as a LIVE interactive session so the
        # scheduler's reminder-fire and Watch-peek gates defer noise into it (defer, never drop — see
        # sentinel.session_is_live). Refreshed every turn (entry `state/sessions/daemon.json`); cleared
        # on idle wind-down and otherwise aged out by SESSION_TTL_SEC.
        write_session_heartbeat(args.state_dir, "daemon", phase="active",
                                working_on="warm chat with the owner (Telegram/Discord)")
        # Cockpit transcript tee (seneschal/docs/cockpit-spec.md "The daemon pipe"): a turn_started marker
        # now, then a per-event tee for the duration of the turn (via on_event, below), then turn_done
        # falls out of the `result` stream event itself — one code path covers Telegram/Discord/cockpit
        # turns alike, since the cockpit is just a third `channel`. Fail-open throughout; a tee/push
        # failure here must never affect the turn itself.
        model_name = getattr(state.session, "model", None) or args.model
        turn_id = uuid.uuid4().hex[:12]  # correlates turn_started/assistant_output/tool_use/turn_done
                                        # for the cockpit chat pane — the protocol carries no other link
        _tee_chat_event(state, args, log, cockpit_pipe.chat_event(
            "turn_started", source=channel, model=model_name, text_preview=text[:200], turn_id=turn_id))
        state.session_busy = True
        _push_cockpit_status(state, args)  # push AFTER flipping busy, so this snapshot says in-flight
        try:
            # The send blocks a worker thread on the child's stdout until this turn's result event —
            # the event loop stays free, so reminders/polls/controls keep running underneath.
            on_event = _make_stream_tee(state, args, log, channel, turn_id)
            reply = await asyncio.to_thread(state.session.send, prompt, on_event=on_event)
        finally:
            state.session_busy = False
        _push_cockpit_status(state, args)
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
            # The once-per-local-day exact-time reminder seed (retired the four fixed reminder slots).
            # Called after maybe_run_slots so, in a contended tick, a just-launched slot's in-flight
            # marker defers the seed (and vice-versa) — the two never spawn overlapping Notion bursts.
            maybe_seed_day(args.state_dir, args, log, state.headless_children, warm_busy,
                           slot_children=state.slot_children)
        await _drain_cockpit_inbox(state, args, log)
        await _sleep_or_stop(state, args.tick_sec)


def _cockpit_inbox_text(it: dict) -> str:
    """One fallback-inbox item -> the text `_enqueue_inbound` should see, honoring `force_fable` (v3)
    exactly like the live-pipe path (`cockpit_task.on_chat_send`) — the SAME `!fable` synthesis so
    `apply_force_route`'s single detection point covers both the live pipe and this degraded fallback."""
    text = (it.get("text") or "").strip()
    if not text:
        return ""
    if it.get("force_fable") and not strip_force_fable(text)[0]:
        text = f"{FORCE_FABLE_TRIGGER} {text}".strip()
    return text


async def _drain_cockpit_inbox(state: DaemonState, args, log) -> None:
    """Fallback path for when the cockpit pipe is down (or the backend hasn't reconnected yet): the
    backend appends {"id","text","ts","force_fable"?} lines to state/cockpit-inbox.jsonl
    (cockpit_pipe.append_inbox); this drains them into the SAME chat queue Telegram/Discord use, deduped
    by id, every scheduler tick — degraded to ~tick_sec latency, never lossy (cockpit-spec.md "The
    daemon pipe")."""
    if args.no_cockpit:
        return
    try:
        items = await asyncio.to_thread(cockpit_pipe.drain_inbox, args.state_dir)
    except Exception as e:  # noqa: BLE001 — a broken inbox file must never break the scheduler tick
        log(f"! cockpit inbox drain failed: {e}")
        return
    if not items:
        return
    new_inbound = [("cockpit", t, 0) for t in (_cockpit_inbox_text(it) for it in items) if t]
    if new_inbound:
        await _enqueue_inbound(state, args, log, new_inbound)


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


async def cockpit_task(state: DaemonState, args, log) -> None:
    """The SIXTH supervised task: a localhost-only, token-authed WebSocket pipe
    (seneschal/scripts/cockpit_pipe.py; seneschal/docs/cockpit-spec.md "The daemon pipe") that lets the cockpit
    backend send chat into the SAME action queue Telegram/Discord use (source="cockpit") and stream the
    warm session's turns back out live. Serves exactly ONE client at a time (PipeHub handles the
    replace-on-reconnect discipline) — this task itself stays trivially small.

    Degrades — no pipe this run, never a crash — when: disabled (`--no-cockpit`), `websockets` isn't
    importable (a stale venv predating `uv sync`), or in test/offline modes (`--stub-brain`/
    `--fake-inbox`, matching control_task's gating so tests never open a real socket)."""
    if args.no_cockpit or args.stub_brain or args.fake_inbox is not None:
        return
    if not cockpit_pipe.pipe_available():
        log("! cockpit: websockets unavailable — pipe disabled (uv sync needed)")
        return
    token = cockpit_pipe.ensure_pipe_token(args.state_dir)

    async def on_chat_send(msg_id, text, frame) -> None:
        # The cockpit is the third mouth of one brain: enqueue exactly like a Telegram/Discord message.
        # v3: `force_fable` (the composer's "Send to Fable" toggle) now ACTUALLY forces — synthesized as
        # the SAME `!fable` prefix Telegram/Discord force-route on, so `_enqueue_inbound`'s single
        # detection path (`apply_force_route`) handles all three channels uniformly. A guard avoids a
        # double prefix if the owner also typed `!fable` themselves with the toggle on.
        if frame.get("force_fable") and not strip_force_fable(text)[0]:
            text = f"{FORCE_FABLE_TRIGGER} {text}".strip()
        await _enqueue_inbound(state, args, log, [("cockpit", text, 0)])

    async def on_control_restart() -> None:
        # Same control-queue path request_control.py / the cockpit backend's REST route use — a
        # graceful restart, applied once the warm session goes idle.
        await asyncio.to_thread(enqueue_control, args.state_dir, "restart",
                                "cockpit pipe control.restart", True)

    def on_status_get() -> dict:
        return _status_snapshot(state, args)

    hub = cockpit_pipe.PipeHub(token=token, on_chat_send=on_chat_send,
                               on_control_restart=on_control_restart,
                               on_status_get=on_status_get, log=log)
    state.cockpit_hub = hub
    try:
        await cockpit_pipe.run_pipe_server(hub, "127.0.0.1", args.cockpit_port, state.stop, log)
    finally:
        state.cockpit_hub = None


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
    state.loop = loop  # lets a worker-thread tee (the warm session's stdout reader) hand a cockpit
                       # broadcast back onto this loop via PipeHub.broadcast_threadsafe
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
            _supervise("cockpit", cockpit_task(state, args, log), state, log),
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
        clear_session_heartbeat(args.state_dir)  # daemon down → no live session; reminders fire normally
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
    p.add_argument("--slack-mcp", default=None,
                   help="path to an MCP config JSON (e.g. slack-mcp.json) giving the headless daemon Slack "
                        "send/read hands, so a chat-approved `send a7` posts immediately; threaded as "
                        "--mcp-config alongside the store's MCP into every spawned claude. If omitted, "
                        "auto-detects scripts/slack-mcp.json when present. (Sending stays ask-high.)")
    p.add_argument("--no-slack", action="store_true",
                   help="don't wire Slack even if scripts/slack-mcp.json exists (Slack approvals record "
                        "status=approved and drain on the next Slack-capable turn — the Slack-hands gap)")
    p.add_argument("--cockpit-port", type=int, default=cockpit_pipe.DEFAULT_PORT,
                   help="localhost port for the seneschald cockpit pipe (sixth supervised task; "
                        f"default {cockpit_pipe.DEFAULT_PORT} — see seneschal/docs/cockpit-spec.md)")
    p.add_argument("--no-cockpit", action="store_true",
                   help="disable the cockpit pipe entirely (no daemon-side websocket server; the "
                        "cockpit backend falls back to the read-only state/* files + inbox file queue)")
    p.add_argument("--model", default=None,
                   help="FALLBACK model id for the warm chat session (default: CLI default) — "
                        "state/model-config.json's warm_model, if set, wins over this at every "
                        "warm-session spawn (cockpit-spec.md v3 'Model dials'; see resolve_warm_model)")
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
                   help="disable ALL internal scheduled runs (brief/wrap/dream/journal + the daily "
                        "reminder seed)")
    p.add_argument("--no-seed-day", action="store_true",
                   help="disable ONLY the once-per-day exact-time reminder seed (the date-rollover run "
                        "that replaced the four fixed reminder slots); leaves brief/wrap/dream/journal on")
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
    # the attribute the warm session / slots / peek spawns forward as --mcp-config (via active_mcp_configs).
    if args.no_notion:
        args.notion_mcp = None
        log("! Store MCP disabled (--no-notion) — headless chat/slots can't read or write the store.")
    else:
        args.notion_mcp, store_log = resolve_store_mcp(args)
        log(store_log)

    # Wire Slack (send/read) into every spawned claude, threaded alongside the store's MCP (Q9 of the
    # Slack draft-and-hold spec). Explicit --slack-mcp wins; otherwise auto-detect scripts/slack-mcp.json
    # so a chat-approved `send a7` posts immediately. --no-slack forces it off. Absence is graceful — Slack
    # approvals just record status=approved and drain on the next Slack-capable turn (the Slack-hands gap),
    # so (unlike Notion) there's no warning when it's missing.
    if args.no_slack:
        args.slack_mcp = None
    elif not args.slack_mcp and os.path.exists(DEFAULT_SLACK_MCP):
        args.slack_mcp = DEFAULT_SLACK_MCP
    if args.slack_mcp:
        log(f"Slack wired for headless runs (send/read): {args.slack_mcp}")

    # Auto-detect discord.env like notion-mcp.json: drop the file in scripts/ and Discord turns on.
    if args.no_discord:
        args.discord_env = None
    elif not args.discord_env and os.path.exists(DEFAULT_DISCORD_ENV):
        args.discord_env = DEFAULT_DISCORD_ENV
    if args.discord_env:
        log(f"Discord wired (gateway push, REST fallback): {args.discord_env}")

    if args.no_cockpit:
        log("Cockpit pipe disabled (--no-cockpit)")
    elif not cockpit_pipe.pipe_available():
        log("! Cockpit pipe unavailable — websockets not importable (uv sync needed)")
    else:
        log(f"Cockpit pipe wired: 127.0.0.1:{args.cockpit_port} "
            f"(token: {cockpit_pipe.pipe_token_path(args.state_dir)})")

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
        # Resolved FRESH at every spawn (not just process start): state/model-config.json's warm_model
        # wins over the --model CLI flag, which is the fallback — cockpit-spec.md v3 "Model dials".
        model = resolve_warm_model(args.state_dir, args.model, log)
        return StubWarmSession(log=log) if args.stub_brain else WarmSession(
            args.claude_bin, model, args.permission_mode, log, mcp_configs=active_mcp_configs(args))

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
