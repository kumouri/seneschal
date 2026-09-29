#!/usr/bin/env python3
"""The assistant's resident presence daemon — always on, warm when you talk to it. Stdlib-first.

This is the single always-on local nerve center (it replaces the 5-min sentinel heartbeat). The host
desktop is always up, so a long-lived process is free; and because the daemon only invokes Claude on a
real message, it burns ~zero tokens while idle (event-driven, not timer-polled).

**Reactive core (asyncio).** One event loop supervises nine long-lived tasks — the full design and the
invariants each task must preserve live in seneschal/docs/asyncio-daemon-design.md, and
seneschal/docs/how-to-add-a-daemon-task.md is the recipe for adding one:
  * `telegram_task` — long-polls Telegram (in a worker thread) and enqueues inbound to the durable
    action queue the moment it's off the wire.
  * `discord_task`  — Discord inbound (gateway websocket when available, REST polling otherwise).
  * `drainer_task`  — the SOLE consumer of the action queue: owns the warm session (one resident
    backend process — by default `claude` in stream-json mode, see backends/), drains messages
    front-to-back one turn at a time, and winds the session down after idle (default 20 min) — unless a
    `--lease` background job is holding it open (seneschal/scripts/jobs.py; a pending control always
    wins over a lease, so a job can never block a Path-A deploy). Chat turns stay strictly serialized
    by construction.
  * `scheduler_task`— a ~5 s tick: lock heartbeat, slot reaping/launching, **background-job reconcile +
    completion pushes**, reminder fires, roll refill, comms peek, the plan-meter reading, the outbox
    flush (Notion backend only) and the mouth drain. Reminders fire within a tick of due EVEN while a
    chat turn is running.
  * `control_task`  — watches the control queue (graceful restart/shutdown) and applies it only once the
    warm session is idle and the queue is empty, AND no heavyweight slot child (the Daily Journal etc.)
    is still in flight ("drain, don't kill") — each hold is bounded on its own cap
    (CONTROL_MAX_HOLD_SEC / SLOT_DRAIN_MAX_HOLD_SEC) so neither can block a deploy forever.
  * `cockpit_task`   — the seneschald cockpit pipe (seneschal/scripts/cockpit_pipe.py; seneschal/docs/cockpit-spec.md):
    a localhost-only, token-authed WebSocket server. Exactly ONE client (the cockpit backend) at a time;
    `chat.send` enqueues into the SAME action queue as Telegram/Discord (source="cockpit"); the warm
    session's turns stream back out as `chat.event`s (also teed to state/warm-transcript.jsonl, a capped
    ring buffer, so a reconnect can backfill — and on through to the never-rewritten dated archive
    under state/transcripts/, which is where the HISTORY lives; transcript_archive.py);
    `status`/`status.get` report turn-in-flight/model/queue depth; `control.restart` rides the same
    control-queue path as request_control.py. Degrades (no pipe) rather than dying if disabled,
    `websockets` is unavailable, or in test/offline modes — never affects chat or reminders.
  * `archon_sites_task` — seneschal/scripts/archon_sites.py: a reconcile loop that discovers every
    archon's self-declared `site.json` (`archons/<id>/site.json`), health-checks each over HTTP, and
    (re)spawns one that's down or wedged. Sites are spawned DETACHED (survive a daemon reload) with
    their PIDs persisted to state/archon-sites.pids.json, so a reload ADOPTS rather than duplicates; a
    `restart-site` control (the cockpit's per-site Restart button) is honored here via
    `state.archon_site_restart_requests`. Fail-open — a flaky archon site never bounces chat or
    reminders. Auto-on when any `archons/*/site.json` exists; `--no-archon-sites` opts out.
  * `cockpit_app_task` — seneschal/scripts/cockpit_site.py: the same reconcile-not-spawn treatment for
    the cockpit backend itself (`cockpit/server/app.py`), so the dashboard is up whenever the daemon is
    instead of needing a human to run uvicorn in a window. Each pass: health-check `GET /api/health` on
    127.0.0.1:8760, (re)spawn it detached when it's down, build `cockpit/web/dist` when it's
    missing/stale, and BOUNCE it when the checkout's git HEAD no longer matches the rev it was spawned
    at (so a merged cockpit change actually loads — see cockpit_site.read_head_rev). Auto-on whenever
    the `cockpit` extras are importable — having run `uv sync --extra cockpit` IS the opt-in; a bare
    venv logs one line, nudges the owner once, and stops. `--no-cockpit-app` opts out.
  * `pr_watch_task` — seneschal/scripts/pr_sweep.py: every ~3 min, find the open PRs that are green,
    not drafts, and would be BLOCKED by the merge guard, and hand them to `merge_guard.ask_on_green` so
    the approval picker goes out with nobody having to remember to send it. **It asks; it cannot
    merge, approve, or write an approval record.** Bounded against a burst: the quiet window suppresses
    the ASKING (marking and retirement are silent and still run), the guard's `(repo, pr, head_sha)`
    ask log makes a restart a no-op, and at most ONE picker is sent per pass (the rest are named and
    held, never dropped). The same pass is not green-only: a PR whose CI has gone RED gets a plain
    notification (never a picker — `pr_red_notify.py`), bound to `(repo, pr, head_sha)` the same way.
    `pr_repair.sweep` rides the same pass (rebases BEHIND PRs, repairs CONFLICTING ones). Fail-open —
    an absent/unauth'd `gh` costs the pass and logs once. `--no-pr-watch` opts out; it is also off in
    every offline/stub mode, `--stub-send` included.
All blocking I/O (the subprocess-based sentinel helpers, the warm session's pipe reads) hops through
asyncio.to_thread; shared state is mutated only on the event loop, so there are no locks to get wrong.

**Background jobs (seneschal/scripts/jobs.py) — "I'll tell you when it's done," made durable.** Work the
assistant starts from inside a turn used to be a child of the warm `claude` process, so it died on the
next wind-down / turn error / merge reload, and the promise to report back died with the session that
made it. Now a job is spawned DETACHED and recorded in state/jobs/<id>.json; `scheduler_task`'s tick
reconciles the ledger and fires the completion push itself, via `deliver_reply` (so the ping is stamped
only once it actually landed, and retries when it didn't). Every terminal state notifies — done,
failed, timed-out, ended-unknown, cancelled — so there is no path on which a started job goes quiet.
`--wake` additionally re-enqueues into the action queue so the warm session reads the result in voice;
the bare push is the guarantee, the wake is the bonus.

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
scrub ANTHROPIC_API_KEY from EVERY child env (`child_env`, and the backends' own copies) so a stray key
can never switch the assistant to metered API billing. For an unattended service, authenticate once
with `claude setup-token` and set CLAUDE_CODE_OAUTH_TOKEN (see SCHEDULING.md). An optional second
backend, the Codex CLI on a ChatGPT subscription (`backend: codex-cli` in state/model-config.json,
backends/codex_cli.py, CODEX_SETUP.md), scrubs OPENAI_API_KEY the same way.

**Model dials + Fable delegation (cockpit-spec.md "Model dials & Fable delegation").** Two dials, set
separately in the cockpit, live in `state/model-config.json` (`model_config.py`): `warm_model` — read at
warm-session SPAWN time (see `resolve_warm_model`; it wins over the `--model` CLI flag below, which
becomes the fallback) — and `max_routable_model` — the hard ceiling on every Fable delegation, re-read
LIVE per inbound turn. The warm session always owns the conversation; when a turn needs more, it
delegates up via `fable_delegate.py` (a subprocess one-shot, NOT a session handoff), triggered by the
router's **fable arm** (`fable_arm_classify`, a hint line only), the warm session's own judgment, or a
**force-route**: a leading `!fable` prefix on any inbound message, or `force_fable: true` on a cockpit
`chat.send` (see `strip_force_fable` / `cockpit_task.on_chat_send`) — bypasses the classifier, never the
approval gate. The ceiling binds every trigger: when it isn't Fable-tier, the fable arm doesn't even run
(`model_config.admits_fable`), and `fable_delegate.py` itself refuses the call regardless of how it was
triggered.

**Shipped defaults.** With no persona, no store and no Telegram configured the daemon still boots: the
grounding renders generic identity phrases, `resolve_store_mcp` forwards no MCP, the Telegram task idles
on a missing telegram.env, and the Notion-only machinery (the write-behind outbox, its drain and its
nudges) stays off unless `store_backend_active()` says "notion".

USAGE (service — normally launched via run-presence.cmd, not by hand):
  python presence.py --model claude-opus-4-8 --idle-min 20 --poll-timeout 25 \
      --peek-interval-min 5 --watch-prompt "Run Watch mode (seneschal/SKILL.md)."

USAGE (offline test, no real claude/Telegram — --stub-send guarantees no real sends either):
  python presence.py --stub-brain --fake-inbox msgs.json --stub-send --max-iterations 6 --no-peek

### The conversation cache is per-topic, and the main chat is a topic like any other

(../docs/telegram-capability-map.md §2.1.) `thread_key` maps a `message_thread_id` to digits, and
EVERYTHING else — `None`, `0`, a bool, a hostile string — to the literal `main`, so there is no "is this
the main chat?" branch to drift, and traversal is unreachable by construction rather than filtered. An
unreadable cache is empty, never another topic's history (`read_thread` catches what `load_json` does
not) — no context is honest, the WRONG thread's is a confident answer to a question nobody asked.
Migrate, never orphan: `migrate_legacy_thread` runs once per boot BEFORE anything appends; a
pre-existing main cache wins and the old file is renamed aside, never clobbered or merged. The topic
rides the durable queue (`queue_item`'s `(channel, text, attempts, topic)`, normalized at the one intake
door) because a reload happens on every merge — and `deliver_reply`'s stale-topic fallback does NOT
spend a transport retry, or a blip plus a rejection ends the loop having never tried the main chat.
`record_topic_nudges` appends a reminder nudge that landed in a thread to THAT thread's cache as an
`assistant` turn, so a bare "done" typed under it is grounded on the nudge instead of on nothing. A
main-chat nudge is deliberately NOT recorded (nudges have never been in the main chat's cache).
Fail-open — the nudge is already delivered by the time this runs.

### The reaction ack path

`ack_reminder_by_reaction` is the one ack path for a thumbs-up reaction: dequeue (the durable local
ledger) and — on the **notion** backend only — `outbox.py ack`. Neither touches the store directly: the
reminder row is written LATER, by the turn the reaction line prompts or by `maybe_drain_outbox`,
whichever gets there first; reading the return as "the row is marked Done" is exactly the mistake the
`reminders_acks.reaction_ack_fully_landed` exists to prevent. See ../docs/telegram-inbound-spec.md.

### An album is one turn, and that means the daemon holds messages it has already acked

(../docs/telegram-inbound-spec.md.) Telegram has no album update: nine screenshots are nine `message`
updates sharing one `media_group_id`, and the drainer answers `pending[0]` one entry per turn — so
without a hold the owner gets several confident answers to slices of one conversation. Held in
`state.album_hold` by `album_absorb`, released by `album_due` on QUIET 2 s / CAP 15 s from the FIRST
member, plus a free close: any ordinary message that is not one of its members ends the group on the
spot, so *nine photos then a question* costs nothing and stays in order (an edit, a reaction and a tap
close nothing — they could split an album mid-upload). Three rails, in order:

  * **Never delay a message that isn't in an album** — no `media_group_id` means dispatched in the
    cycle it was polled in, latency byte-for-byte what it was.
  * **Never hold forever** — the due check runs EVERY cycle, including one whose poll returned nothing
    and one whose poll FAILED; `time.monotonic`, never the wall clock; the wind-down flushes on every
    graceful stop, i.e. every merge.
  * **Never lose a message** — the hold is DURABLE (`state/telegram-album-hold.json`), because
    telegram_poll.py already committed the offset and Telegram never re-sends an acked update.

A restart mid-album delivers what it has and does NOT resume the hold: read-then-DELETE so a crash in
the dispatch cannot re-deliver, and members arriving after form their own turn. The caption is SEARCHED
FOR, not assumed to be on the first member, and an album contributes NO edit handle — an edit to its
caption would replace nine descriptors with one string and lose the photos, so it arrives annotated
instead.

### A message that lands mid-answer says so

Phase 1 of ../docs/mid-turn-interleave-spec.md: one prompt-only line so an "oh, and also X" stops
reading as a reply to a reply. A LOCAL var on one prompt, never written back to `state.pending`, so the
persisted retry text, the thread tail and `turns.jsonl` stay byte-identical to what the owner sent.
Noted at INTAKE (was a turn in flight *when it came off the wire*, read before anything awaits), keyed
by the POST-transform text, and consumed by the FIRST prompt built for it — so a retry goes out
unmarked, deliberately. The interleave gate rides beside it (interleave.py): the turn WINDOW opens where
`turn_id` is minted and closes in `send()`'s `finally`, and the whole inbound batch is snapshotted
BEFORE any classifier runs, or the second message is charged for the first one's latency.

### A turn leaves a pointer saying who asked for it

`../state/current-turn.json`, `jobs.write_turn_pointer` where `turn_id` is minted, closed in the SAME
`finally` as the interleave window. It is written there because that is the one moment all four facts
are in scope — the turn's id, its channel, whether a person composed the line, and the Telegram
`message_id`, which the durable queue does NOT carry and only the in-memory `inbound_ids` window can
answer. `by` is `turns.classify_origin`, IMPORTED not re-spelled, so two spellings of "did a person
write this?" cannot drift. The cold-spawn fill is `_session_opened`, the existing `_on_opened` hook:
the CLI reports its session id DURING the first send, so the pointer opens anonymous — and an anonymous
pointer resolves to NOTHING rather than to a guess. A pointer that cannot be written costs the
attribution and never the turn.

### The per-turn deadline is a gap between stream events, never a cap on the turn

`TURN_IDLE_GAP_SEC=600` — a hang is SILENT, not slow, and a total cap kills the long healthy turns. A
watchdog kills the child and the EXISTING mid-turn-death path takes over; a hung turn is the ONE
delivered outcome that does NOT pop the queue. See ../docs/hung-turn-deadline-spec.md.

### The tick writes vitals once a minute

`maybe_write_vitals` writes `state/vitals.json` once a minute: `{tick_at, reminders_fired_today,
reminders_failed_today, last_reminder_fired_at, slots_failed_today, outbox_dead, warm_session}`,
`failures.watchdog_status`'s whole read side. The counters are IN-MEMORY ONLY and reset at the
owner-local date boundary (`_roll_vitals_day`, `reminders_acks.local_today`) — a restart or a day
rollover costs the running total, which is the accepted trade: this is a health SNAPSHOT, not a ledger,
and the durable record of what actually fired is `mouth.jsonl` and `failures.jsonl`.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import archon_sites  # archon human-facing site supervision — seventh supervised task (see below)
from backends import claude_cli, codex_cli  # the Backend contract's two implementations
                                            # (docs/pluggable-backend-spec.md §3) — WarmSession/
                                            # StubWarmSession/CodexWarmSession live here, not in this
                                            # file; the names below are re-exported for every existing
                                            # caller (presence.WarmSession, many test modules) unchanged.
WarmSession = claude_cli.WarmSession
StubWarmSession = claude_cli.StubWarmSession
CodexWarmSession = codex_cli.CodexWarmSession
_TurnWatchdog = claude_cli._TurnWatchdog  # noqa: SLF001 — deliberate re-export, see above
TURN_IDLE_GAP_SEC = claude_cli.TURN_IDLE_GAP_SEC
DEFAULT_WARM_FALLBACK_MODEL = claude_cli.DEFAULT_WARM_FALLBACK_MODEL
context_tokens_from_usage = claude_cli.context_tokens_from_usage
import channel_declare  # the reply-path channel-declaration forcing function, phase 2 LIVE (message-routing-spec.md)
import cockpit_pipe  # the cockpit pipe protocol — sixth supervised task (see cockpit_task below)
import cockpit_site  # the cockpit backend's supervision — cockpit_app_task (see below)
import failures  # a durable row for a failure that used to vanish silently (vitals.json's read side)
import governor  # Oikonomos, the budget governor (v3.5, cockpit-spec.md) — turn-usage metering + alerts
import interleave  # the mid-turn relevance gate, phase 0 — OBSERVE ONLY (docs/mid-turn-interleave-spec.md)
import jobs  # durable background jobs — detached survivors + the daemon-owned completion push
import log_rotation  # the one door every rotating state/logs/ file goes through (log-rotation-spec.md)
import merge_guard as mg  # the merge gate — THIS daemon is the only writer of an approval record
import model_config  # the two-dial model config (v3, cockpit-spec.md "Model dials & Fable delegation")
import dream_steps  # Dream's step ledger — which nightly steps actually ran, and how long ago
import mouth  # the assertions log — what the assistant has actually said to the owner (docs/mouth-spec.md)
import outbox_common as ob  # the write-behind outbox (Notion backend only) — this daemon owns its flush
import pr_sweep  # green-PR discovery — pr_watch_task; it ASKS via merge_guard, never merges
import pr_repair  # rides pr_watch_task after pr_sweep: rebases BEHIND PRs, repairs CONFLICTING ones
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
import spend_levers  # WHY a turn cost what it cost — the stream-tee lever counter (phase 1)
import standing_safety  # the READ FIRST items' own store (docs/read-first-retirement-spec.md)
import owi_unknowns  # the unknown-owner grid picker (owi_unknowns.py)
import tomorrow_marker  # Tomorrow's Lead — the Brief section + the Wrap's two pickers (docs/tomorrow-marker-spec.md)
import observation_gate  # the Brief's "observation complete, ready to continue" lead-in (docs/observation-gate-spec.md)
import stateio  # zero-import atomic write primitive — vitals.json's writer
from telegram_poll import safe_filename  # shared scrub for sender-supplied filenames
import telegram_topics  # phase-2 declared-channel resolution (message-routing-spec.md)
import turn_suppression  # the drainer's THIRD outcome — popped, not spent, not delivered
import turns  # the conversation log — both sides, verbatim (docs/chat-turn-indexing-spec.md, phase 0)
import transcript_archive  # the durable half of the transcript ring buffer (docs/cockpit-spec.md)
import usage_activity  # what was happening in a reading's interval (docs/usage-telemetry-spec.md)
import usage_health  # usage-telemetry-spec §8.3 — the INSTRUMENT speaks when it breaks; the meter never does
import usage_probe  # the plan-meter reading itself — writes a row, NEVER speaks

REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
THREAD_CAP = 20  # rolling turns kept for cross-session continuity
MIN_IDLE_MIN = 10.0  # hard floor: a warm session always stays warm at least this long after activity
SLOT_MAX_RETRIES = 8  # give up relaunching a failed slot after this many bad exits in a day (backstop)
# --- The hybrid retry wait -----------------------------------------------------------------------------
# Without it, a failed slot relaunches on the very next ~5s tick with NO wait at all, so the full
# SLOT_MAX_RETRIES ladder burns in under two minutes — against a multi-hour usage-limit outage, that
# spends the whole retry budget before the outage is even over. Two mechanisms decide the
# wait between a failed exit and its relaunch, tried in this order:
#   1. Reset-time (preferred). If the failed run's own captured output names a `claude` usage-limit
#      reset — "session limit ... resets HH:MM" — hold the relaunch until that clock time instead of
#      guessing. See `parse_slot_reset_time` / `_SLOT_RESET_RE`.
#   2. Exponential backoff (fallback). Every OTHER non-zero exit — a bug, a bad prompt, a transient
#      network blip that names no reset — backs off instead: `SLOT_BACKOFF_BASE_SEC * 2**(attempt-1)`,
#      capped at `SLOT_BACKOFF_CAP_SEC`. See `slot_backoff_seconds`.
# Both stay bounded by the two EXISTING backstops — `SLOT_MAX_RETRIES` above and `--slot-catchup-min`
# (default 180 min, see `classify_slots`) — unchanged by this: this only adds a WAIT between attempts,
# never a new attempt, and a slot held past its catch-up window is stamped `too_late` and abandoned for
# the day exactly as before, regardless of how long it was still holding.
#
# **The backoff cap is deliberately well inside the catch-up window, not equal to it.** The full 8-retry
# backoff ladder (30, 60, 120, 240, 480, 900, 900, 900s) sums to ~3630s, ~60.5 minutes — under half of
# the 180-minute default catch-up window. A cap sized to (or beyond) the window would be pointless: once
# a slot is past its `at` + `--slot-catchup-min`, `classify_slots` routes it to `too_late` and
# `maybe_run_slots` stamps it done-but-skipped before a held retry ever gets to fire, so a wait longer
# than the window it's racing against buys nothing. Leaving headroom below the window (not spending it
# all) means a slot that fails early in its window still gets several real retry attempts inside it,
# rather than one giant wait that consumes the whole window on a single guess.
SLOT_BACKOFF_BASE_SEC = 30
SLOT_BACKOFF_CAP_SEC = 900  # 15 min
# Poison-pill guard for the durable action queue. A message that ends its turn WITHOUT a delivered
# reply — most dangerously one that kills the daemon mid-turn (the "reseneschald" self-kill: a warm-session
# command that restarts the process, so the reply never lands and the message is never popped) — is
# otherwise re-queued and replayed on every boot, forever. We count each turn BEFORE sending and persist
# it up-front, so a mid-turn kill still advances the counter; past this cap the message is dead-lettered.
MAX_TURN_ATTEMPTS = 3
# --- The per-turn deadline (docs/hung-turn-deadline-spec.md P1) -------------------------------------
# **This is a GAP between stream events, not a cap on the turn.** It is reset by every line the child
# produces, and trips only on silence. A legitimate turn runs long all the time — tool use, a big read,
# a Fable delegation — and a flat total-duration cap would kill exactly those. A hang is not *slow*, it
# is *silent*: `_read_until_result`'s `for raw in self.proc.stdout` blocks forever on a child that
# emits nothing and never closes, which stuck `session_busy` on, pinned the message at `pending[0]`,
# and made MAX_TURN_ATTEMPTS above advance only ONCE PER DAEMON BOOT — the "it comes back every time"
# replay, a loop clocked by restarts instead of by seconds. It also held every defer-until-idle control
# forever (see CONTROL_MAX_HOLD_SEC).
#
# **The idle-gap length is 10 minutes.** (Defined in
# backends/claude_cli.py and re-exported above — TURN_IDLE_GAP_SEC — since it's the claude-cli
# backend's own dial; codex_cli.py holds its own CODEX_TURN_GAP_SEC, currently the same value.)
# The maximum a defer-until-idle control may be held before it applies anyway (P2). A deploy that never
# lands is worse than a turn cut short: an unbounded hold is how Path A silently stops deploying — the
# same failure class as seneschald-update's branch drift. Deliberately far above
# TURN_IDLE_GAP_SEC so it never decides the fate of a turn P1 already governs — by the time this fires,
# any watchdog-killed turn has long since returned and released its worker thread (which matters:
# `asyncio.run` waits for its default executor at exit, so a restart requested while a thread is still
# parked on a wedged child would block on the way out). This is for the class P1 cannot see — a queue
# that never drains, a drainer that never comes back — and it triggers the SAME mid-turn apply path a
# `defer_until_idle: false` control already uses, so it is a new trigger, not a new capability.
CONTROL_MAX_HOLD_SEC = 30 * 60.0
# The maximum a defer-until-idle control may be held for an in-flight heavyweight slot child (the Daily
# Journal etc.) before it applies anyway, killing that child ("drain, don't kill"). Without it, a PR
# merging while the early-morning journal run is in flight restarts the daemon mid-slot and silently
# loses the rest of that run, because `reap_finished_slots` only stamps a slot done on a clean exit and
# a plain shutdown terminates in-flight children unconditionally. Separate constant from
# CONTROL_MAX_HOLD_SEC (chat-idle) because the two gate different things on different clocks — a stuck
# chat turn is a wedge to break out of, a running slot is legitimate work to let finish. **20 minutes**:
# long enough to cover a normal heavyweight run (observed runtimes are ~15-20 min), short enough that a
# wedged slot can never hold a deploy indefinitely.
SLOT_DRAIN_MAX_HOLD_SEC = 20 * 60.0
# Denominator for the warm session's context-fill gauge (warm-session lifetime, Step 1). The claude
# CLI reports no window size, so this is a stated assumption, not a measurement — every surface
# showing a percentage derived from it must label it an ESTIMATE.
#
# There is no window setting to get wrong: the CLI has no context flag and settings.json has no
# context key, so the window is whatever the MODEL gives, and the current warm-dial models carry 1M as
# both default and maximum. Measured per-turn context runs well past 200k and answers normally (long
# window, not silent compaction), so a 200k denominator would be wrong by 5x.
#
# **What a wrong denominator actually breaks is the resume gate, not the gauge.** `context_pct` feeds
# `resume_verdict`'s ceiling, so with 200k the 80% line would sit at 160k rather than 800k and refuse
# `context_too_full` on sessions that are in truth a fraction full — and Step 2a exists to stop a Path A
# reload dropping a live conversation to a thread tail.
#
# Still an ESTIMATE, and the caveat below still stands on its own terms: the CLI reports nothing, the
# `context_tokens` input is itself a proxy, and a reading over 100% remains a real thing rather than a
# bug — consumers render it honestly rather than clamping the NUMBER (the cockpit clamps bar width only).
CONTEXT_WINDOW_TOKENS = 1_000_000
# Why the previous warm session ended (DaemonState.last_respawn_reason → the status snapshot). The
# memo's §1 table: an idle wind-down is healthy, the other three are not, and today nothing tells them
# apart from outside — the daemon looks identically quiet whether it wound down cleanly or its session
# has been erroring out every turn.
RESPAWN_IDLE = "idle_winddown"          # the 20-min quiet timer (or the 60s pending-restart window)
RESPAWN_TURN_ERROR = "turn_error"       # the turn errored / the stream closed without a result
RESPAWN_TURN_TIMEOUT = "turn_timeout"   # the idle-gap watchdog killed a silent child (TURN_IDLE_GAP_SEC)
RESPAWN_UNDELIVERED = "undelivered_reply"  # a reply was produced but Telegram/Discord wouldn't take it
RESPAWN_SHUTDOWN = "daemon_shutdown"    # supervisor wind-down (incl. a Path A code reload)
# --- Step 2a: resume instead of re-ground (docs/session-continuity-spec.md) --------------------------
# Verified live against the CLI before any of this was built:
#   * `--resume <id>` works in `-p --input-format stream-json` and REPORTS THE SAME session_id back.
#   * The resumed session genuinely carries context (the whole point).
#   * It resumes a child that was KILLED, not just one closed cleanly — so a crashed daemon is fine.
#   * A stale/unknown id fails FAST and detectably: a `result` event with is_error + num_turns 0 in
#     ~3s ("No conversation found with session ID: …"), which `_read_until_result` already turns into
#     a None reply. The fallback below keys on exactly that existing signal — no new detection.
#   * Resuming onto a DIFFERENT model is allowed and still carries context, so no model guard is
#     needed: a cockpit dial change lands at the next spawn exactly as it did before.
#
# Only clean deaths are resumable. A turn error is excluded deliberately — a session that just died
# mid-turn is the one case where restoring its state risks restoring whatever wedged it, and it shares
# its failure shape with a stale id. RESPAWN_TURN_TIMEOUT is excluded for that reason at its sharpest:
# a hung session is BY DEFINITION the one whose state we have the most evidence against restoring.
# The undelivered-reply path is excluded because dropping that
# session so the retry re-grounds cleanly (no phantom reply) is an existing, load-bearing invariant.
RESUMABLE_REASONS = frozenset({RESPAWN_IDLE, RESPAWN_SHUTDOWN})
# Past this, a "continuous" conversation is a confusing one: only the clock is re-injected per turn, so
# everything else the session believes (carry-over, reminder state, what the store said) has gone stale.
RESUME_MAX_AGE_SEC = 2 * 3600
# Archon-site supervision (archon_sites_task, archon_sites.py) tunables. A ~20s reconcile
# cadence is cheap (a couple of local site.json reads + one HTTP HEAD-equivalent GET per declared site)
# and plenty responsive for a human-facing UI; SPAWN_GRACE_SEC protects a just-(re)spawned site from
# being killed again before `build_site.py --serve`'s own startup rebuild finishes and it starts
# answering — without it, a slow first build would look "still down" and get thrashed every pass.
ARCHON_SITES_INTERVAL = 20
SPAWN_GRACE_SEC = 30
# Give-up backstop, same posture as SLOT_MAX_RETRIES above: a site that spawns fine but NEVER becomes
# healthy would otherwise be killed and respawned every pass forever. That's not theoretical — a site
# whose startup legitimately outlasts SPAWN_GRACE_SEC gets killed mid-build every cycle, and on Windows
# the killed parent doesn't take its own children with it, so each cycle can leak a grandchild process
# (a site that renders via a headless browser, say). After this many consecutive failed revivals, stop
# respawning and say so loudly; a later success (or a cockpit Restart request) resets the counter.
ARCHON_SITE_MAX_RESPAWNS = 5
# Cockpit-backend supervision (cockpit_app_task, cockpit_site.py) tunables. Same shape and reasoning
# as the archon-site trio above — a ~20s reconcile, an anti-thrash grace window, and a give-up backstop
# — with a longer grace because uvicorn's first request has to import fastapi + the whole app module
# (seconds on a cold filesystem, vs. a static-file server answering immediately). The build cap is
# per-daemon-boot, not per-pass: `npm ci` + `npm run build` is minutes of wall time, so a repeatedly
# failing build must not be retried every 20s forever — two attempts, then the cockpit stays API-only until
# the next boot (or the next merge, which resets the counter along with everything else).
COCKPIT_APP_INTERVAL = 20
COCKPIT_APP_SPAWN_GRACE_SEC = 45
COCKPIT_APP_MAX_RESPAWNS = 5
COCKPIT_APP_MAX_BUILDS = 2
# cockpit.log rotation (log-rotation-spec.md). The child holds the file open for its whole life, so
# this is checked only at the two moments the reconcile loop is ABOUT to kill-then-respawn it anyway
# (an ordinary crash recovery, a deploy bounce, or a size-triggered bounce below) — never on its own
# timer, which would mean fighting a live handle instead of reusing one already being closed.
COCKPIT_LOG_MAX_BYTES = log_rotation.DEFAULT_MAX_BYTES
COCKPIT_LOG_KEEP = log_rotation.DEFAULT_KEEP
# --log-file (presence.log) rotation. This process holds the file open itself, so RotatingAppendLog
# can check/roll on every write rather than only at startup — unlike cockpit.log, there's no spawned
# child's handle to work around. 2 MB matches the threshold this file used before it had a real door.
PRESENCE_LOG_MAX_BYTES = 2_000_000
PRESENCE_LOG_KEEP = log_rotation.DEFAULT_KEEP
# PR-watch supervision (pr_watch_task, pr_sweep.py). **Deliberately two orders of
# magnitude slower than its neighbours above, because it is a different KIND of check.** Those two
# poll localhost; this one calls the GitHub API, so the right neighbourhood is the other periodic
# external-git check on this host — `seneschald-update`'s ~10 min fetch — bounded from below by how long
# a picker may lag the green it is about. Three minutes: one `gh pr list` per watched repo, so a
# couple of calls a pass, far under GitHub's 5,000/hour authenticated budget, and comfortably inside
# the ~10 min Path A deploy cadence so the question never arrives about a commit that has already
# been superseded. It costs nothing at all inside the quiet window — `pr_sweep.sweep` returns before
# its first network call there.
PR_WATCH_INTERVAL = 180
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
    # `context` / `on_complete` are CODE hooks around the run (`owi_unknowns.py`'s docstring): the
    # launch appends the unassigned-work count the run must print verbatim, and a clean exit resets the
    # first-real-message ask window. Neither is a step the run is asked to remember — see
    # `slot_context` / `slot_on_complete` below.
    {"name": "morning-brief", "at": "06:30",
     "prompt": "Run the morning Brief (seneschal/SKILL.md): deliver in chat + push highlights to Telegram "
               "+ email via Proton + write the Run Log. Use {tz}. Run silently.",
     "context": "brief_context", "on_complete": "brief_sent"},
    {"name": "eod-wrap", "at": "21:07",
     "prompt": "Run the Wrap (seneschal/SKILL.md -> subagents/eod-wrap/SKILL.md). Done-today includes "
               "Tasks completed today AND ⏰ Reminders rows with Last Acknowledged = today (never "
               "judge by the Ack checkbox - it's consumed on read). Also run the journal-presence gate "
               "(nudge-or-satisfy) and refresh linked-task status for active Deadline-Watches so their "
               "re-fires stop once the linked record is Done. Use {tz}. Run silently."},
    # NB: this prompt NAMES NO STEP LIST — see the enumeration rule below. A prompt that lists some of
    # a mode's steps is read as that run's SCOPE (the prompt arrives first; the mode file reads as
    # background), so a partial list silently skips every unlisted step — with no ledger skip
    # recorded, so the miss is invisible. `modes/dream.md` says there is no such thing as a scoped
    # Dream, and the prompt must say the same. The one id it carries, 2b-2e, rides under "work EVERY
    # step" as emphasis, not as the scope. Only the journal-presence backstop is named here,
    # because it is the one instruction that lives in NO numbered step of that file.
    {"name": "dream", "at": "22:00",
     "prompt": "Run the Dream consolidation (seneschal/SKILL.md -> seneschal/modes/dream.md). Work EVERY "
               "step in that file, in order, including the enrichment block 2b-2e - there is no such "
               "thing as a scoped Dream, and a step being within its dream_steps.py window is not a "
               "reason to skip it. Any step you genuinely cannot run must be recorded: python "
               "seneschal/scripts/dream_steps.py record <step> --skip --reason '<why>'. Also run the "
               "journal-presence satisfy-only backstop (auto-tick if the owner journaled after the "
               "Wrap check; no bedtime nudge). Use {tz}. Run silently."},
]

# **THE ENUMERATION RULE, and it binds SLOTS as well as SEED_PROMPT below.** A slot prompt that lists
# a mode's steps becomes that run's scope: the prompt arrives first and the mode file reads as
# background, so a step missing from the prompt is a step that does not happen even when the mode file
# describes it in full. TWO legal shapes, and nothing in between:
#
#   (a) enumerate the WHOLE mode and keep the two in lockstep — what SEED_PROMPT does, deliberately;
#   (b) point at the mode file and claim EVERY step in it — what the dream slot does. A step id may
#       then appear only as EMPHASIS, never as the list.
#
# The illegal shape is a bare partial list with no whole-mode claim: read as the scope, because nothing
# in the prompt says otherwise. `test_slot_prompts.py` asserts (b) for the dream slot by requiring the
# whole-mode claim WHENEVER a step id is named — a future edit that drops the claim and leaves the ids
# behind is exactly the regression, and it fails there.

# The exact-time reminder SEED (retired the four fixed slots). NOT a fixed-time SLOT: it fires on the
# first tick of each new owner-local date, so a machine asleep through midnight still seeds on wake —
# no classify_slots catch-up cliff that could silently skip the daily reset. It reuses the slot lifecycle
# (registered in slot_children, stamped in slots.json under this name, retried on a crashed run) via
# maybe_seed_day. The brain run does the store parts (reset, acks, one-off auto-retire, which rows are
# due) then calls reminders_seed.py per due row to queue that row's exact-time nudges for the whole day.
# Like the slot prompts, the template carries identity tokens; the runnable SEED_PROMPT is rendered
# at startup by build_seed_prompt() (raw replace — this prompt is never .format()ed).
SEED_SLOT_NAME = "reminders-seed"
# NB: this prompt ENUMERATES the seed's steps, so a step missing here is a step that does not happen even
# when subagents/reminders/SKILL.md describes it — keep the two in lockstep. This is the enumeration rule
# above, and the seed takes the enumerate-the-WHOLE-mode branch of it deliberately: the seed IS these
# steps, whereas Dream's step list is long enough that a prompt-side copy drifts. The one-off auto-retire
# below is why: a `done` one-off past its due date reads as overdue at every later seed and re-fires
# forever, escalating as it goes. All four conditions are load-bearing and the ack-date one is the
# safety rail: retiring an UNacked row would silently drop work the owner still owes, which is worse
# than the noisy bug it fixes. See seneschal/references/reminders-policy.md → "Auto-retire a completed
# One-off (seed pass)."
SEED_PROMPT_TEMPLATE = (
    "Run Reminders mode (subagents/reminders/SKILL.md) in SEED mode for the whole day: apply "
    "pending acks, then AUTO-RETIRE completed one-offs — for each ⏰ row where ALL of (Cadence = "
    "One-off) AND (Status = Done) AND (Last Acknowledged is set AND >= Due / Target) AND (Due / Target "
    "is STRICTLY BEFORE today's owner-local date), set Status = Finished, leave Last Acknowledged and "
    "Consecutive Misses untouched, enqueue nothing for it, and log it in the Run Log (title, due, last "
    "acked). All four conditions are required — never retire an unacked row, another cadence, another "
    "status, or a row due today, and never touch a linked Task/Goal. Then run the daily reset "
    "(Daily/Weekdays habits), then for EACH ⏰ row due today compute "
    "its fire time(s) — the row's Times field, else its Time Window default — and enqueue exact-time "
    "nudges for the full local day via reminders_seed.py (one call per row; pass --importance, "
    "--due-target for a one-off and, for a ticked row, --nag. The 90-minute re-fire ladder is Nag Until "
    "Done ALONE — never infer it from Importance — and --pierce-quiet still goes on Critical+. Also pass "
    "--consecutive-misses with the row's own Consecutive Misses value — feeds the premise-review "
    "question, seneschal/docs/reminder-premise-spec.md). Put any `!` lines the seed prints on "
    "stderr in the Run Log verbatim. Update the tracker + Run Log. Use {tz}. Run silently."
)


def child_env(source: str = "scheduled") -> dict:
    """Env for any child `claude` process: scrub ANTHROPIC_API_KEY so we always bill the
    subscription (CLAUDE_CODE_OAUTH_TOKEN / the logged-in account), never the metered API.

    `source` sets `SENESCHAL_SESSION_SOURCE`, which is how a child can later say what KIND of assistant
    surface it is — `jobs.build_cancelled_by` reads it to decide whether a cancel speaks in the first
    person (docs/cancel-attribution-spec.md §4.2). It exists because the session registry cannot
    answer this: the registry is keyed by session id only for hook-stamped `build` entries, while the
    daemon's own entry is the literal `daemon.json`, so a lookup by UUID returns `build` or nothing
    and **never `daemon`** — the one value the wording ladder turns on.

    **The default is `scheduled`, and only the warm-session spawn passes `daemon`.** Putting the
    marker unconditionally in here would label a Watch peek as the warm session, which is precisely
    the bug this parameter's existence is meant to avoid.

    The comms peek passes its own value, **`watch`** — and there it is a safety
    interlock rather than a label: `telegram_send.ACK_GATE_SOURCES` reads it to arm the ack gate that
    stops a peek re-escalating an already-acked ⏰ row (see `maybe_peek`). `watch` is outside
    `jobs.ASSISTANT_SURFACES`/`sentinel.ASSISTANT_SURFACES` exactly as `scheduled` is, so nothing about cancel
    attribution or the wording ladder changes."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env["SENESCHAL_SESSION_SOURCE"] = source
    return env


# --- No harness background tasks in the warm session ------------------------------------------------
# The warm session is ONE long-lived `claude -p --input-format stream-json` process, and the harness
# queues work BETWEEN its turns. A Bash/PowerShell call that outruns its timeout (BASH_DEFAULT_TIMEOUT_MS,
# 120 s) is otherwise MOVED TO THE BACKGROUND rather than killed; when it finishes while the session is
# idle, the CLI queues a `task_notification` turn of its own, and that turn's `result` is the first one
# `_read_until_result` sees after the next stdin write — so the owner's message is answered by the
# result AFTER it, one turn late, until the queue drains (a long `find`/`grep -r` that hits the 120 s
# timeout is enough to trigger it). Neither of the daemon's existing nets sees it: `_read_events`
# returns at the first `result`, and the channel/marker retry only fires if the notification's answer
# happens to lack both control lines.
#
# Reproduced live in the daemon's exact spawn shape (BASH_DEFAULT_TIMEOUT_MS=5000 + a 15 s sleep):
# WITHOUT this variable the idle process emitted `background_tasks_changed` →
# `task_updated` → `task_notification`, the next stdin message's result was the notification's answer,
# and the message's own answer came one result later; WITH it the tool result was `Command timed out
# after 5s`, in-turn, nothing arrived unprompted, and the next message was answered by the very next
# result. `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` is documented (code.claude.com/docs/en/env-vars —
# "disable all background task functionality, including the `run_in_background` parameter on Bash and
# subagent tools, auto-backgrounding, and the Ctrl+B shortcut"), and the warm session never legitimately
# backgrounds anything: long work is a `jobs.py` job by rule (GROUNDING below; modes/chat.md rule 10).
# Deliberately NOT folded into `child_env`: a one-shot `-p` run (peek, slots) or a job has no next turn
# for a queued notification to steal, and jobs' own background-task rails live in `jobs.py` (§3.13/§3.14).
# The dict itself lives in backends/claude_cli.py beside WarmSession.start(), the spawn it applies to
# (moved there by docs/pluggable-backend-spec.md Phase 2); this is a re-export, so there is one source.
WARM_SESSION_ENV = claude_cli.WARM_SESSION_ENV


def warm_session_env() -> dict:
    """`child_env("daemon")` plus the warm-session-only overlay `WARM_SESSION_ENV` (above)."""
    env = child_env("daemon")
    env.update(WARM_SESSION_ENV)
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
    """Authoritative local-time stamp for prompts, e.g. 'Friday 2026-07-03 12:30 <zone name>'.
    Every spawned claude MUST base 'today'/'yesterday' on this instead of inferring the date — that
    inference drifts a day forward off a UTC clock (rule 5), the classic off-by-one 'yesterday'
    bug. Handed in explicitly so no run ever has to guess what 'now' is. Rendered in the owner's
    timezone via local_now() (machine-local when unconfigured/unresolvable)."""
    return local_now().strftime("%A %Y-%m-%d %H:%M %Z")


# --------------------------------------------------------------------------- model dials (v3)

def resolve_warm_model(state_dir: str, cli_model: str | None, log, backend: str | None = None) -> str | None:
    """Which model the warm session should spawn with — cockpit-spec.md "Model dials & Fable
    delegation": `state/model-config.json`'s `warm_model` WINS over the `--model` CLI flag, which is
    the fallback/default. Called fresh at every warm-session SPAWN (not just process start), so a dial
    change takes effect the next time the session naturally winds down and respawns; the cockpit's
    "apply now" (a graceful restart) forces it promptly for an already-warm session. Tolerant: a
    missing/corrupt config file or an unrecognized `warm_model` falls back to the CLI flag, logging
    which source won either way (observability, not silent drift). NOTE this only checks the dial is in
    the capability RANK, not that the model is actually spawnable — a ranked-but-unavailable tier still
    returns here; `WarmSession`'s spawn-fallback is the net that catches it at first-turn time.

    `backend` (docs/pluggable-backend-spec.md §3.2) selects WHICH RANK table `canonical` checks
    against — None re-reads it from the same config file `make_session` already loaded, so a caller
    that already has `cfg` can pass `cfg.get("backend")` and avoid a second read."""
    cfg = model_config.load(state_dir)
    backend = backend or cfg.get("backend") or model_config.DEFAULT_BACKEND
    warm = cfg.get("warm_model")
    if warm:
        canon = model_config.canonical(warm, backend=backend)
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
#   * per-turn tokens {channel}/{now}/{thread}/{msg}/{channel_declare_instruction}/{read_first}/
#     {current_topic_line} — left untouched until the drainer's send-time GROUNDING.format(...)
#     fills them for each new session.
GROUNDING_TEMPLATE = """You are {assistant} — {owner}'s chief of staff — talking with them live over {channel}.
The current local date and time is {now} ({tz}) — treat this as the AUTHORITATIVE clock for
every "today"/"yesterday"/"tomorrow" and all date math. Do not infer the date yourself and never trust a
UTC clock; if a reminder's text disagrees with this stamp, this stamp wins.
Read persona/persona.md (fall back to persona/persona.default.md) and seneschal/modes/chat.md (Chat
mode's rules live there — seneschal/SKILL.md is a router and holds none of them) and
stay fully in character: reply as the assistant in the persona's voice, never as Claude, no
meta-narration, run any tools silently. Keep
replies concise and chat-appropriate. Honor the act-low / ask-high gate (draft-and-hold anything
outbound or destructive). Only tell {owner} something is done/logged/marked off/cleared when the tool call
actually succeeded — if the store or any tool is unreachable or errors, say so plainly and park it in
carry-over; never claim a write that didn't land.
You have the SAME store read/write access here as the full seneschal skill: reading is act-low, and act-low
tracker writes you should just make — don't merely say you will. In particular, when {owner} acknowledges a
reminder ("took'em", "done", "did it", "already ate"), the ack MUST reach the store (`store-update` via
store/<backend>/mapping.md; on the notion backend journal it first with
`python seneschal/scripts/outbox.py ack --reminder-id <ref>`) AND
scripts/reminders_dequeue.py --reminder-id <that reminder's ref> in THIS TURN, or it is lost — the
warm session is volatile (it winds down / a reboot clears it) and neither survives that; the store + the
dequeue's local ledger (state/acks.json) are the only durable record, and my fire path re-checks them
at due time. Confirm only once the write succeeded; if the store is unreachable, say so plainly and park
it in carry-over. The exact fields and values (done vs finished, last_acknowledged, consecutive_misses,
why NOT the one-tap ack affordance alone, --activity-day for after-midnight acks) are re-read at write
time from seneschal/references/reminders-policy.md + the active store's schema, not memorized here.
Flipping a *linked* Task/Goal to Done stays ask-high.
Keep reads cheap: lean on the baked-in references (store/<backend>/schema.md) before re-querying, keep
concurrent reads to a handful, and on a rate-limit (Notion's 429) back off serially rather than hammering.
If {owner} asks you to restart or reload yourself ("reseneschald", "restart", "reload your code"): do NOT run
seneschald-control.ps1 or otherwise kill the daemon from here — you are running INSIDE that daemon, so a
synchronous restart kills you mid-reply, your answer never sends, and the daemon replays the request on
every boot (a self-kill loop). Instead, finish your reply normally, then run (act-low)
`python seneschal/scripts/request_restart.py`. The resident daemon reloads itself gracefully AFTER your reply
is delivered — same effect as reseneschald, no dropped message.
Anything long-running goes through a JOB (`python seneschal/scripts/jobs.py start --title "<name>" -- <the
command>`) — never a bare background spawn, `Start-Process`, or `&`: this session is volatile (idle
wind-down, any turn error, every code merge) and anything backgrounded as its own child dies with it,
half-done and unreported, where a job spawns DETACHED and the daemon pushes {owner} the outcome regardless.
A Bash/PowerShell call here has a hard 120 s ceiling and background tasks are DISABLED in this process:
past the ceiling the call is killed and the tool result says `Command timed out` — nothing is ever moved
to the background, there is no `run_in_background`, and no "you will be notified" ever arrives. So a
`grep -r`/`find` over the workspace, a test suite, or anything that could outrun 120 s is a job, never an
inline call — and a timed-out result means "narrow it or make it a job", never "wait for it".
THE RULE THAT MUST HOLD WITH NO LOOKUP: never tell {owner} "I'll let you know when it's done" / "I'll watch
that" / "I'll circle back" unless a job id exists — without one, nothing anywhere will ever wake to keep
that promise. Every flag (`--wake`/`--lease`/`--retry`/`--goal`/`--analyze`), the preflight refusal, and
`jobs.py list`/`status`/`cancel`/`mail`: seneschal/docs/background-jobs-spec.md.
Delegating to Fable: you ALWAYS own this conversation — delegation is a subprocess call-out
(`python seneschal/scripts/fable_delegate.py "<task>"`), never a handoff, and never bypasses the
act-low/ask-high gate. A "[router hint: ... FABLE-LEVEL ...]" or "[force-fable: ...]" line above a
message is self-explanatory where it appears and names the script; your own mid-turn judgment (deep
synthesis, long-horizon planning, hard multi-step debugging) may also call it unprompted. The script
enforces the max-routable-model ceiling, and Oikonomos, the budget governor, may also refuse (a Fable
quota, delegation concurrency, or Fable's token budget) — relay any refusal to {owner} honestly, handle
the turn yourself, and never route around it. Mechanics: seneschal/docs/cockpit-spec.md.
{channel_declare_instruction}{read_first}{thread}
{current_topic_line}
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




# Step 2a: what a RESUMED session gets instead of the full GROUNDING block above. It already holds the
# grounding — that's the entire point of resuming — so re-sending it would waste the tokens the resume
# was meant to save and, worse, read as a second system prompt mid-conversation. Carries the same
# identity token ({tz}) and is rendered at startup the same way (RESUME_PREAMBLE, below); {now},
# {current_topic_line} and {msg} are per-turn and filled by the drainer's send-time .format.
#
# The clock is the one thing that MUST be restated. A session only ever saw the date at grounding, and
# a resumed one may have been asleep across midnight; the same reasoning as the per-turn clock refresh
# below it. The nudge about the gap is deliberate too: from inside, a resumed session cannot tell that
# any time passed at all, so anything it "knows" about the store, reminders or carry-over may be stale.
RESUME_PREAMBLE_TEMPLATE = """(Picking our conversation back up — you have the full history above. Two things
before you answer: the authoritative current local time is now {now} ({tz}), which supersedes
any earlier stamp for all date math; and some time has passed since your last turn, so re-check anything
time-sensitive — reminders, carry-over, store state — rather than trusting what you remember of it.)
{current_topic_line}

The owner just said: {msg}"""


# Loaded once at startup (import time). persona/identity.json is optional — load_identity()
# never raises and yields the generic defaults when it's absent, so GROUNDING/SLOTS/SEED_PROMPT/RESUME_PREAMBLE
# render byte-identical to the pre-identity hardcoded prose on an unconfigured install.
IDENTITY = load_identity()
GROUNDING = _render_grounding(GROUNDING_TEMPLATE, IDENTITY)
SLOTS = build_slots(IDENTITY)
SEED_PROMPT = build_seed_prompt(IDENTITY)
RESUME_PREAMBLE = _render_grounding(RESUME_PREAMBLE_TEMPLATE, IDENTITY)


# ------------------------------------------------------------- standing safety (the READ FIRST block)
#
# WHY THIS IS CODE AND NOT A POINTER. A grounding that merely *mentions* a standing-safety source
# never carries a byte of it, so its corrections cost a deliberate tool read that nothing forces — and a
# warm session can then volunteer exactly the thing the durable record says not to raise cold, with the
# correction sitting on disk, un-injected.
#
# The mechanism generalises: a recalled memory file rides
# into context automatically on every session, and the fresh source costs a tool call. **Free-and-stale
# beats costly-and-correct every time, and both sound equally confident.** So the fix has to move the
# fresh source to the same price as the stale one — zero — which means the bytes arrive in the prompt
# whether or not anyone chose to look. A prompt-side "go read the digest first" is the contract that
# already fails in practice (a prompt-side-only instruction to write a log is how a log collects zero
# rows in a month).
#
# The section is injected VERBATIM. Summarising it here would put a paraphrase of a safety correction
# in front of the model instead of the correction, and the wording is load-bearing ("the matter is
# CLOSED — do not raise it" does work that a neutral status line would not do).
#
# In seneschal the store (state/standing-safety.json, standing_safety.py) is the one home; the digest
# path below survives only as a migration shim for an install that still holds a leftover
# state/context-digest.md (a fresh install never has one).
DIGEST_FILE = "context-digest.md"
#: Named here (rather than read off `standing_safety.STORE_FILE` at call time) purely for the
#: truncation marker's `{name}` — the file `read_first_block` credits when ITS output gets capped.
STANDING_SAFETY_FILE = standing_safety.STORE_FILE
# THE CAP. **6,000 B.** A standing-safety section grows by per-bullet narrative bloat far more than by
# bullet count, and a cap that fires truncates for real — silently dropping its last bullet from every
# cold spawn. The cap is a HOLDING MEASURE, NOT THE FIX — the fix is the compression + retirement rule
# in `docs/read-first-retirement-spec.md`, and raising the ceiling without it only moves the same cliff
# further out.
# The ceiling exists for a simple reason: the digest is a GENERATED file that nothing
# reviews, so without a cap one runaway Dream run silently doubles the cost of every cold spawn.
# Truncation drops WHOLE LINES from the end and says so in the output — a standing-safety
# instruction cut mid-sentence is worse than one that is visibly incomplete.
READ_FIRST_MAX_BYTES = 6000
READ_FIRST_TRUNCATED = "[… READ FIRST truncated at {cap:,} B — read state/{name} for the rest …]\n"
READ_FIRST_HEADING_RE = re.compile(r"^(#{1,6})\s+.*READ FIRST", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})\s")
# Written here rather than in the `GROUNDING` literal so the budgeted literal grows by exactly the
# 13 B of `{read_first}` and the framing is versioned beside the reader that produces it.
READ_FIRST_PREFACE = (
    "STANDING SAFETY — the block below is copied VERBATIM out of state/standing-safety.json (or, "
    "while the READ FIRST migration shim is still active, whatever state/context-digest.md still "
    "holds from before it was retired — docs/read-first-retirement-spec.md). It is "
    "the CURRENT state of these items, it OVERRIDES anything you remember to the contrary (including "
    "anything a memory file asserts), and nothing in it may be raised cold.\n")

#: The migration shim's own bookkeeping file — presence.py's, distinct from `standing-safety.json`
#: itself (that store has exactly one writer, `standing_safety.py`). Tracks how
#: many consecutive daemon BOOTS have seen the store hold at least one active item; once that reaches
#: `READ_FIRST_SHIM_CONSECUTIVE_BOOTS_TO_DISABLE`, `disabled` latches permanently True and the digest
#: fallback below never runs again, on this host, no matter what the store does afterward.
READ_FIRST_SHIM_FILE = "read-first-shim.json"
READ_FIRST_SHIM_SCHEMA = "seneschal.read-first-shim/1"
#: Two consecutive boots is enough to trust the store permanently — one lucky boot (a stray manual
#: `add`) should not retire the fallback, but requiring more would just delay a migration that already
#: succeeded. A fallback that outlives its own migration is the same "gains but never loses" shape
#: `docs/read-first-retirement-spec.md` describes for the digest's READ FIRST section — so this one is
#: a one-way latch, never re-armed by this function.
READ_FIRST_SHIM_CONSECUTIVE_BOOTS_TO_DISABLE = 2


def _read_first_shim_path(state_dir: str) -> str:
    return os.path.join(state_dir, READ_FIRST_SHIM_FILE)


def _load_read_first_shim_state(state_dir: str) -> dict:
    """Tolerant read of the shim's own bookkeeping file — absent, unreadable, or malformed all read as
    a fresh, not-yet-disabled shim (`consecutive_nonempty_boots: 0, disabled: False`), never a crash
    and never a guess in the disabled direction (the safe default keeps the fallback ON, not off)."""
    try:
        with open(_read_first_shim_path(state_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    return {"schema": READ_FIRST_SHIM_SCHEMA,
            "consecutive_nonempty_boots": int(data.get("consecutive_nonempty_boots") or 0),
            "disabled": bool(data.get("disabled")),
            "disabled_at": data.get("disabled_at")}


def _save_read_first_shim_state(state_dir: str, data: dict) -> None:
    stateio.write_json_atomic(_read_first_shim_path(state_dir), data)


def read_first_shim_disabled(state_dir: str) -> bool:
    """Whether the digest-fallback migration shim has permanently latched off. Never raises — an
    unreadable bookkeeping file reads as "not yet disabled", the side that keeps the safety fallback
    available rather than the side that could silently drop it."""
    try:
        return _load_read_first_shim_state(state_dir).get("disabled", False)
    except Exception:  # noqa: BLE001
        return False


def check_read_first_migration(state_dir: str, args, log) -> None:
    """**Call exactly once, at daemon boot, before the event loop starts** — like
    `_notify_cockpit_missing_deps`'s call site, "once per boot" is structural because this function is
    only ever invoked from `main()`'s one-time startup sequence, not from a per-turn or per-spawn path.

    Reads `standing_safety.has_active_items(state_dir)` and:

    * **Active items** — increments `consecutive_nonempty_boots`. Once that reaches
      `READ_FIRST_SHIM_CONSECUTIVE_BOOTS_TO_DISABLE`, the shim **latches disabled permanently** (a
      one-way flag `read_first_shim_disabled` then reads forever, on this host) and says so loudly —
      the migration is trusted done and the digest fallback in `read_first_block` never runs again,
      even if the store is later found empty (a broken store at that point is a real signal, not a
      migration-not-yet-done state). Below the threshold, logs progress and does nothing else.
    * **Empty or absent store, leftover digest present** — resets the consecutive-boot counter to 0,
      logs one loud line (the digest fallback is what is actually protecting this boot's cold spawns),
      and queues ONE Telegram nudge via `_notify_read_first_shim_empty` naming that
      `standing_safety.py import-digest` has not run on this host yet.
    * **Empty or absent store, no digest** — the shipped default of a fresh install: there is nothing
      to migrate and nothing to protect, so this logs nothing loud and nudges nobody.

    Once the shim is already disabled, this is a fast no-op — nothing left to count or nudge about.
    Never raises: a broken bookkeeping file must cost only this check, never the boot, and the safe
    failure direction is "keep the fallback available", never "silently disable the safety net"."""
    try:
        shim = _load_read_first_shim_state(state_dir)
        if shim["disabled"]:
            return
        if standing_safety.has_active_items(state_dir):
            shim["consecutive_nonempty_boots"] += 1
            if shim["consecutive_nonempty_boots"] >= READ_FIRST_SHIM_CONSECUTIVE_BOOTS_TO_DISABLE:
                shim["disabled"] = True
                shim["disabled_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                _save_read_first_shim_state(state_dir, shim)
                log(f"• READ FIRST migration shim SELF-DISABLED — {STANDING_SAFETY_FILE} has held "
                    f"active items for {shim['consecutive_nonempty_boots']} consecutive boots; the "
                    f"{DIGEST_FILE} fallback in read_first_block is now permanently off on this host.")
                return
            _save_read_first_shim_state(state_dir, shim)
            log(f"• {STANDING_SAFETY_FILE} holds active items this boot "
                f"({shim['consecutive_nonempty_boots']}/{READ_FIRST_SHIM_CONSECUTIVE_BOOTS_TO_DISABLE} "
                f"consecutive) — the migration shim latches off once that count is reached")
            return
        if shim["consecutive_nonempty_boots"]:
            shim["consecutive_nonempty_boots"] = 0
            _save_read_first_shim_state(state_dir, shim)
        if not os.path.exists(os.path.join(state_dir, DIGEST_FILE)):
            return  # a fresh install: no leftover digest, nothing to migrate, nobody to nudge
        log(f"! {STANDING_SAFETY_FILE} is empty or absent — `standing_safety.py import-digest` has "
            f"not been run on this host yet. Falling back to {DIGEST_FILE}'s READ FIRST section for "
            f"this boot (the migration shim; docs/read-first-retirement-spec.md).")
        _notify_read_first_shim_empty(args, log)
    except Exception as e:  # noqa: BLE001 — this check must never cost the boot
        log(f"! READ FIRST migration-shim check failed (continuing, digest fallback stays available): {e}")


def _notify_read_first_shim_empty(args, log) -> None:
    """ONE Telegram line for this boot while the standing-safety store is still empty — structurally
    once-per-boot the same way `_notify_cockpit_missing_deps` is: this is only ever called from
    `check_read_first_migration`, itself called exactly once at boot. Fail-open: an unreachable
    Telegram is logged, never raised."""
    if args.stub_send or not args.telegram_env:
        return
    text = ("The standing-safety store (state/standing-safety.json) is still empty — "
            "`standing_safety.py import-digest` hasn't been run on this host. READ FIRST is falling "
            "back to state/context-digest.md for now. Run the import once to finish the migration "
            "(docs/read-first-retirement-spec.md).")
    if mouth.enqueue(args.state_dir, surface="telegram", kind="nudge", text=text,
                     speaker="daemon") is None:
        log("! read-first shim empty nudge could not be queued")


def _standing_safety_section(state_dir: str) -> list | None:
    """`standing_safety.render()`'s lines, ready for `_cap_section`, or `None` to mean "no usable
    store — fall back to the digest" (absent, unreadable, or zero active items). **Never raises**: a
    broken store must cost the fallback, never the grounding turn."""
    try:
        rendered = standing_safety.render(state_dir)
    except Exception:  # noqa: BLE001
        return None
    return rendered.splitlines() if rendered else None


def _digest_section(state_dir: str) -> list:
    """The digest's `## …READ FIRST…` section, unbounded and untrimmed — everything past this point
    (trimming, the byte cap, the truncation marker) is shared with the standing-safety path by
    `_cap_section`, below. Raises on an absent/unreadable digest; `read_first_block` is the one place
    that catches it, same as before this function existed as its own name.

    Section bounds are by heading DEPTH, not by "the next `##`": the block is matched at whatever level
    it was written at and ends at the first heading of that level or shallower, so a `###` sub-heading
    inside it comes along instead of silently truncating the section at its first sub-point."""
    path = os.path.join(state_dir, DIGEST_FILE)
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    depth, section = None, []
    for line in lines:
        if depth is None:
            m = READ_FIRST_HEADING_RE.match(line)
            if m:
                depth = len(m.group(1))
                section.append(line)
            continue
        end = _HEADING_RE.match(line)
        if end and len(end.group(1)) <= depth:
            break
        section.append(line)
    return section


def _cap_section(section: list, max_bytes: int, source_name: str) -> str:
    """Trim trailing blank lines, refuse a heading with nothing under it, then enforce `max_bytes`
    by dropping WHOLE lines from the end and appending a visible truncation marker naming
    `source_name` — the one cap-and-truncate implementation both `read_first_block` sources share."""
    while section and not section[-1].strip():
        section.pop()
    if len(section) < 2:  # never found it, or found a heading with nothing under it
        return ""
    text = "\n".join(section) + "\n"
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    marker = READ_FIRST_TRUNCATED.format(cap=max_bytes, name=source_name)
    while section:  # drop whole lines until the kept text AND its marker fit
        candidate = "\n".join(section) + "\n" + marker
        if len(candidate.encode("utf-8")) <= max_bytes:
            return candidate
        section.pop()
    return ""


def read_first_block(state_dir: str, max_bytes: int = READ_FIRST_MAX_BYTES) -> str:
    """The standing-safety section injected into cold grounding, verbatim and byte-capped.

    **This prefers `state/standing-safety.json`** (via `standing_safety.render()`) over the digest
    whenever the store holds at least one active item. **The digest itself is RETIRED — Dream no longer
    writes it — and the fallback below is a MIGRATION SHIM, not standing behavior**: it exists only to cover the gap between "the store went
    live" and "someone ran `standing_safety.py import-digest` on this host," and
    `check_read_first_migration` (called once at boot) permanently latches it off
    (`read_first_shim_disabled`) the moment the store has proven itself for two consecutive boots. Once
    latched, this function reads `standing_safety.render()` ONLY — a store that later reads empty (a
    bug, a bad edit) is a real signal at that point, not a still-migrating one, and does not reopen the
    digest read.

    Returns `""` for every unhappy path — no store and no digest (or a latched shim with an empty
    store), no such section, a heading with no body, an unreadable file, a cap too small to fit even
    one line — and **never raises**. That is the whole contract: this runs inline on the cold-spawn
    path, so a malformed generated file must cost the block and never the session. The caller does not
    wrap it and should not have to."""
    section = _standing_safety_section(state_dir)
    source_name = STANDING_SAFETY_FILE
    if section is None:
        if read_first_shim_disabled(state_dir):
            return ""  # the migration shim has latched off — no digest read may ever happen again
        source_name = DIGEST_FILE
        try:
            section = _digest_section(state_dir)
        except Exception:  # noqa: BLE001 — deliberate: no failure mode here may reach the spawn path
            return ""
    if not section:
        return ""
    try:
        return _cap_section(list(section), max_bytes, source_name)
    except Exception:  # noqa: BLE001
        return ""


def read_first_grounding(state_dir: str, max_bytes: int = READ_FIRST_MAX_BYTES) -> str:
    """`read_first_block` plus its framing line, ready to substitute into `GROUNDING`'s `{read_first}`.

    Empty in, empty out — an absent block contributes no framing, no blank line and no trace, so a
    fresh checkout's grounding is byte-identical to what it was before this existed."""
    block = read_first_block(state_dir, max_bytes)
    return READ_FIRST_PREFACE + block if block else ""


# --------------------------------------------------------------------------- thread continuity

#: Where the per-topic continuity caches live, one JSON file per thread. A DIRECTORY rather than one
#: file (`docs/telegram-capability-map.md` §2.1): private-chat topics mean several long-running
#: conversations share one chat, and a single cache hands every cold spawn the wrong half of them — a
#: question in one thread gets grounded on another thread's conversation. The chat id does not change,
#: so nothing else about the Telegram stack moves.
THREAD_DIR = "telegram-threads"
#: The pre-topics single-file cache. Still named here because :func:`migrate_legacy_thread` moves it
#: into the directory above exactly once; nothing reads this path afterwards.
LEGACY_THREAD_FILE = "telegram-thread.json"
#: The main chat's key. **The main chat is a topic like any other as far as storage is concerned** —
#: it is simply the key you get when there is no `message_thread_id`. It is deliberately not a digit,
#: so it can never collide with a real thread id, and there is deliberately no branch anywhere below
#: that asks "is this the main chat?": one formula, one code path, nothing to drift.
MAIN_THREAD_KEY = "main"


def thread_key(topic=None) -> str:
    """Which continuity cache a message belongs to: a positive `message_thread_id` as digits, or
    :data:`MAIN_THREAD_KEY` for everything else.

    **Total by construction, and that is the safety property.** Every input — `None`, a bool, a
    float, a digit string from a reloaded queue entry, a hostile string — maps to exactly one key,
    and the only strings this can ever return are `str(int)` or `"main"`. So the key can be joined
    onto a path with no escaping question: `..`, a separator and a drive letter are all unreachable
    outputs, not merely filtered ones.

    A value we cannot read as a thread id reads as **the main chat**, which is what "no topic" has
    always meant. Note what that is *not*: it is not another topic's history. The only way to land in
    a topic's cache is to name that topic's id."""
    if isinstance(topic, bool):  # bool is an int; True would key the "1" thread
        return MAIN_THREAD_KEY
    try:
        n = int(topic)
    except (TypeError, ValueError):
        return MAIN_THREAD_KEY
    return str(n) if n > 0 else MAIN_THREAD_KEY


def thread_path(state_dir: str, topic=None) -> str:
    """The continuity cache for one topic. `topic=None` is the main chat, which is why every
    pre-topics caller keeps working unchanged."""
    return os.path.join(state_dir, THREAD_DIR, thread_key(topic) + ".json")


def read_thread(state_dir: str, topic=None) -> list:
    """One topic's rolling history as a list, **empty on every failure**.

    `load_json` already answers `[]` for absent and for malformed JSON, but not for the rest of the
    ways a file can refuse to be read (a binary blob is a `UnicodeDecodeError`, a directory at the
    path is an `OSError`), and here that distinction is the whole point: an unreadable cache must
    degrade to *no context*, never to *someone else's context*. A cold spawn with no continuity block
    is an assistant who asks what this is about; a cold spawn holding another thread's conversation is
    an assistant who answers the wrong question confidently, which is the one outcome topics exist to
    end."""
    try:
        thread = load_json(thread_path(state_dir, topic), [])
    except Exception:  # noqa: BLE001 — deliberate: no read failure here may reach the spawn path
        return []
    return thread if isinstance(thread, list) else []


def migrate_legacy_thread(state_dir: str, log=None) -> bool:
    """Move the pre-topics single-file cache into the directory as the **main chat's** thread. Returns
    whether anything moved. Idempotent, and called once per boot from :func:`main`.

    The old file is the record of the conversation that was happening in the main chat, so that is
    where it belongs — and saying so in code is the point of this function existing at all. The
    alternative was to leave `telegram-thread.json` sitting there while nothing read it: twenty turns
    of continuity silently stops being carried into a cold spawn, with a file on disk that looks
    exactly like it still works.

    Two outcomes, both one-shot:

    * **The usual one** — no main-chat cache yet, so the legacy file simply *becomes* it (`os.replace`,
      same directory tree, atomic).
    * **Both exist**, which can only happen if a daemon already wrote main-chat history before this
      ran. The newer file wins and the legacy one is renamed aside to `.superseded` rather than
      deleted or merged: clobbering it would destroy live history, merging two caps' worth of turns
      would invent an ordering neither file states, and leaving it in place would log this line on
      every boot forever.

    Fail-open: a migration that cannot happen costs continuity across one boundary, never a message,
    so it logs and returns rather than raising into daemon startup."""
    legacy = os.path.join(state_dir, LEGACY_THREAD_FILE)
    if not os.path.isfile(legacy):
        return False
    dest = thread_path(state_dir, None)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            aside = legacy + ".superseded"
            os.replace(legacy, aside)
            if log:
                log(f"• telegram continuity: {LEGACY_THREAD_FILE} was already superseded by "
                    f"{os.path.basename(dest)} — kept the newer one, moved the old one to "
                    f"{os.path.basename(aside)}")
            return False
        os.replace(legacy, dest)
        if log:
            log(f"• telegram continuity: migrated {LEGACY_THREAD_FILE} to the main-chat thread "
                f"({len(read_thread(state_dir, None))} turn(s)) — the cache is per-topic now")
        return True
    except OSError as e:
        if log:
            log(f"! telegram continuity: could not migrate {LEGACY_THREAD_FILE} ({e}); the main "
                f"chat starts with an empty continuity cache and nothing else is affected")
        return False


def append_thread(state_dir: str, role: str, text: str, topic=None) -> None:
    path = thread_path(state_dir, topic)
    thread = read_thread(state_dir, topic)
    thread.append({"role": role, "text": text, "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
    save_json(path, thread[-THREAD_CAP:])


def amend_thread(state_dir: str, old_text: str, new_text: str, role: str = "owner",
                 topic=None) -> bool:
    """Rewrite the most recent `role` entry that still reads exactly `old_text` — the continuity-cache
    half of an in-place edit replacement (`apply_inbound_edit`). True if one was found and rewritten.

    Only ever called for a message that has **not** been answered yet, so nothing the assistant said
    is being rewritten under it. The durable record of what was said (`turns.jsonl`) is written by the drainer
    at answer time and so records the corrected text with no help from here; this file is written at
    *enqueue* time, which is exactly why it needs amending — otherwise a cold spawn's continuity block
    would show the typo'd line and the corrected one as two separate things the owner said.

    Fail-open: this is a cache. If the entry has already aged out of THREAD_CAP, or the write fails, the
    corrected text still reaches the warm session as the message itself.

    `topic` is the thread the edit arrived in — read off the `edited_message` itself, not off the
    queue entry, because an edit lands in the same topic as the message it corrects. Getting it wrong
    costs the amendment (the old line is never found in the wrong file), never the corrected text."""
    path = thread_path(state_dir, topic)
    thread = read_thread(state_dir, topic)
    for entry in reversed(thread):
        if isinstance(entry, dict) and entry.get("role") == role and entry.get("text") == old_text:
            entry["text"] = new_text
            save_json(path, thread)
            return True
    return False


def thread_tail(state_dir: str, topic=None) -> str:
    """The continuity block a COLD spawn carries — the only thing a refused resume gets instead of the
    conversation (docs/session-continuity-spec.md §4.1) — for ONE topic.

    It sends `THREAD_CAP` turns, not six. `append_thread` retains THREAD_CAP (20) explicitly for
    "cross-session continuity", and this — its only reader, and the sole continuity input to
    `GROUNDING` — used to take `thread[-6:]`, dropping fourteen of them at the one moment continuity is
    needed. Nothing chose 6; the two constants simply disagreed and nothing had ever compared them.

    The cost is ~2.5 kB on top of the `GROUNDING` itself. Slicing by the same constant that fills the
    file keeps the two from drifting apart again.

    **Empty is a correct answer and a topic never borrows one.** A thread with no history yet, an
    unreadable file, a topic the owner opened a minute ago — all of them ground the spawn on the message
    itself and nothing else, which is honest. The failure this refuses to have is the other one:
    handing a cold session the conversation from a *different* thread, which reads as continuity and
    is a fabrication."""
    thread = read_thread(state_dir, topic)
    if not thread:
        return ""
    lines = [f"{t.get('role', '?')}: {t.get('text', '')}" for t in thread[-THREAD_CAP:]]
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
    try/except so even an import/write error just logs and continues. The small local model is fast
    (~3s, think off) so the inline call is cheap; a rare slow/unreachable Ollama returns the escalate fallback quickly."""
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
    Ollama when `max_routable_model` isn't Fable-tier** — "the fable arm doesn't even run".
    Fail-open throughout: any exception here just skips the hint, exactly like the triage arm above."""
    try:
        cfg = model_config.load(state_dir)
        if not model_config.admits_fable(cfg.get("max_routable_model"), backend=cfg.get("backend")):
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


# ------------------------------------------------------- mid-turn interleave, phase 0 (OBSERVE ONLY)

# `docs/mid-turn-interleave-spec.md` §4.3. The relevance gate runs on every mid-turn arrival and writes
# what it WOULD have done to `state/interleave-log.jsonl`.
#
# **`observe` is zero behaviour change.** Nothing is interrupted, nothing is reordered, no prompt is
# altered, the durable queue is untouched, and the serialization invariant — one consumer, one turn in
# flight, one reply per delivery — is exactly what it was. Phase 2 (the live interleave, §5) runs only
# under `--interleave-mode live`, an explicit opt-in.

# How many finished turn windows to remember. A verdict that lands after its turn ended still has to
# say how late it was — the whole of §6's corollary — and that needs the turn's end stamp. Small: the
# only rows that read it are ones whose classifier outran the turn, which resolve within seconds.
INTERLEAVE_TURN_MEMORY = 16

# Tool names per turn window, for §4.2's in-flight summary. Bounded because the summary is bounded.
INTERLEAVE_MAX_TOOLS = 24


def interleave_mode(args) -> str:
    """The live mode. **The flag's default is `observe` and lives in the PARSER; the fallback here is
    `off`, deliberately, and the two are not the same number by accident.**

    Every production path builds `args` through `main()`'s parser, which always sets this. An args
    namespace that reaches here without the attribute is a hand-built one — every harness in this
    suite builds its own — and a namespace that has never heard of a flag must not acquire its
    behaviour. Defaulting to `observe` here would have turned two dozen existing offline tests into
    live Ollama callers on a host that has one running (a state-defaulting flag making old tests live
    writers is a known failure shape)."""
    return getattr(args, "interleave_mode", interleave.MODE_OFF)


def interleave_open_turn(state: "DaemonState", turn_id: str, text: str,
                         private_turn: bool = False) -> dict:
    """Open the turn window the gate measures against. Called where `turn_id` is minted.

    Kept in RAM and never persisted: a restart loses the window, which costs the resolution rows of
    any arrival still in flight and nothing else. §5.3's crash walk is the same shape — phase 0 needs
    no schema change to `presence-state.json` and does not get one.

    `fold_queue` and `pending_tool_call` are phase 2's (LIVE-mode-only) additions — empty/`None` for
    the whole life of an `observe`/`off` turn, since only `interleave_observe`'s live-mode branch and
    `interleave_track_tool_call` ever write to them. `private_turn` rides along so a live-mode fold's
    own `_capture_turn` call (§5.2(c)'s fix — capture at each fold, not once at the end) redacts
    exactly as the head's own capture does, without threading a second parameter through every caller."""
    record = {"turn_id": turn_id, "started": time.monotonic(), "ended": None,
              "text": text, "tools": [], "steers": 0, "pending": [],
              "fold_queue": [], "pending_tool_call": None, "private_turn": bool(private_turn)}
    state.inflight_turn = record
    state.interleave_turns[turn_id] = record
    while len(state.interleave_turns) > INTERLEAVE_TURN_MEMORY:
        state.interleave_turns.pop(next(iter(state.interleave_turns)))
    return record


def interleave_note_tool(state: "DaemonState", name) -> None:
    """Remember a tool name seen this turn, for the in-flight summary. Runs on the warm session's
    stdout reader thread, so it does the smallest possible thing: an append to a plain list."""
    try:
        record = state.inflight_turn
        if record is None or not name:
            return
        tools = record["tools"]
        if len(tools) < INTERLEAVE_MAX_TOOLS:
            tools.append(str(name))
    except Exception:  # noqa: BLE001 — observation may never cost the turn it observes
        pass


def interleave_track_tool_call(state: "DaemonState", ev: dict) -> None:
    """§3.8's blind-retry guard: remember the most recent tool call this turn with no `tool_result`
    yet, so a live-mode interrupt landing mid-call can tell the continuation which call was cut and
    that it may have already committed server-side (`interleave.possibly_landed_clause`).

    Reads the RAW stream-json event, not the cockpit's converted chat.event — `tool_result` lives on
    a `user` event, and `cockpit_pipe.build_chat_event_from_stream` drops every `user` event entirely
    (a chat pane has no use for them), so there is no converted shape to read this off. A simplifying
    choice, named rather than hidden: this clears on ANY `tool_result`, not the matching `tool_use_id`
    — the CLI runs tool calls one at a time in the overwhelming common case, and the cost of getting
    this wrong is a possibly-landed clause that fires one call late, not a lost write. Runs on the
    warm session's stdout reader thread; fail-open, same contract as `interleave_note_tool`."""
    try:
        record = state.inflight_turn
        if record is None or not isinstance(ev, dict):
            return
        blocks = (ev.get("message") or {}).get("content")
        if not isinstance(blocks, list):
            return
        t = ev.get("type")
        if t == "assistant":
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    record["pending_tool_call"] = {
                        "name": b.get("name"),
                        "input_preview": json.dumps(b.get("input"), ensure_ascii=False)[:200]
                                         if b.get("input") is not None else None,
                    }
        elif t == "user":
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    record["pending_tool_call"] = None
    except Exception:  # noqa: BLE001 — observation may never cost the turn it observes
        pass


def interleave_close_turn(state: "DaemonState", args, log) -> None:
    """Stamp the turn's end and write the resolution row for every verdict that landed inside it.

    §4.3: `turn_remaining_sec` is written by the same drainer iteration when its turn completes, as a
    SECOND append-only row keyed by the same `turn_id` — these logs are never updated in place. This
    is also where `arrived_offset_frac` becomes knowable, since it is a fraction of the turn's
    *eventual* length. Fail-open: a resolution row that cannot be written costs the row."""
    record = state.inflight_turn
    state.inflight_turn = None
    if record is None or record.get("ended") is not None:
        return
    record["ended"] = time.monotonic()
    total = max(0.0, record["ended"] - record["started"])
    pending, record["pending"] = record["pending"], []
    if not pending or interleave_mode(args) == interleave.MODE_OFF:
        return
    for entry in pending:
        interleave.record_resolved(
            args.state_dir, arrival_id=entry["arrival_id"], turn_id=record["turn_id"],
            turn_remaining_sec=record["ended"] - entry["landed"],
            arrived_offset_frac=(entry["offset"] / total) if total > 0 else None,
            turn_total_sec=total)
    log(f"• interleave: resolved {len(pending)} mid-turn verdict(s) for turn {record['turn_id']}")


def interleave_snapshot(state: "DaemonState", args, channel: str, text: str,
                        queue_index: int | None = None) -> dict | None:
    """Everything about a mid-turn arrival that is only true at the moment it lands, or None when
    this is not one. **Purely synchronous, and taken for the whole batch before any gate runs** — a
    second message in the same batch must not have its arrival offset inflated by the first
    message's classifier latency.

    None when the mode is off, when no turn window is open, or when the window has already closed:
    the brief window between a turn's `result` and its delivery is time the owner is still waiting (so
    phase 1's marker deliberately covers it), but it is not part of a turn the §4.3 offsets can be
    fractions of, so there is nothing here to measure against.

    `queue_index` is phase 2's own addition — this arrival's absolute index in `state.pending` at the
    moment it was persisted (the caller extends `state.pending` before taking any snapshot, so this is
    always knowable). It is what `interleave_observe`'s live-mode branch uses to enforce §5.1's
    non-negotiable 1 — only the contiguous head may absorb a message — rather than trusting arrival
    order alone, which a mixed batch (some hold, some steer) would violate. `None` for a caller that
    doesn't have one (there is currently exactly one caller, and it always does); a live-mode fold
    decision simply never fires without it."""
    if interleave_mode(args) not in (interleave.MODE_OBSERVE, interleave.MODE_LIVE):
        return None
    record = state.inflight_turn
    if record is None or record.get("ended") is not None:
        return None
    return {
        "record": record,
        "channel": channel,
        "text": text,
        "offset": max(0.0, time.monotonic() - record["started"]),
        # §5.1's non-negotiable 3, and §4.3 logs it either way: a machine-synthesized arrival — a job
        # notice, a reaction ack, most measured mid-turn arrivals — is never eligible to fold.
        # It is still classified and still logged, because a rule you cannot see the cost of is a rule
        # you cannot revisit.
        "typed": turns.classify_origin(text) == "human",
        "in_flight": interleave.in_flight_summary(record["text"], record["tools"]),
        "queue_index": queue_index,
    }


async def interleave_observe(state: "DaemonState", args, log, snap: dict) -> None:
    """Run the gate on one mid-turn arrival and log what it would have done.

    **In `observe`, changes nothing** — exactly as before. **In `live`, a STEER verdict that is still
    inside its turn, typed, and the next contiguous queue entry after however much has already folded
    this turn ALSO interrupts the running turn and queues itself to be folded in** (§5 steps 1-2). The
    verdict/logging shape is identical in both modes — a live-mode fold is a side effect bolted onto
    the SAME decision, not a second gate — so a read-out taken from `interleave-log.jsonl` can never
    tell which mode produced a given row, by design.

    §6(1): off the intake path, in a thread, exactly as `shadow_classify` does today — a slow verdict
    costs the interleave, never the message, which is already persisted and already claimable by the
    drainer before this runs.

    Everything after the `await` is synchronous, which is what makes the `turn_still_live` reading and
    the pending-resolution append atomic against `interleave_close_turn`: both run on the event loop
    and neither yields in between — including the live-mode fold decision, so nothing here races
    `interleave_live_send`'s own read of `fold_queue` on the worker thread (a plain list append/copy,
    safe under the GIL for the same reason `interleave_note_tool`'s append already is)."""
    started = time.monotonic()
    verdict = await asyncio.to_thread(interleave.gate, snap["text"], snap["in_flight"])
    landed = time.monotonic()
    record = snap["record"]
    still_live = record.get("ended") is None
    # WHICH fold of this turn this would have been (§4.3). Assigned to
    # every row as "the fold it would have been", and advanced only by a steer — so the maximum over
    # the steer rows IS the chain depth, and the histogram can report the tail rather than assert it.
    # It is NOT a bound and must not become one: §5.1.1 forbids reintroducing a cap under any unit;
    # the one legitimate route back is a new, explicit owner decision informed by this distribution.
    fold_depth = record["steers"] + 1
    if verdict["verdict"] == interleave.STEER:
        record["steers"] += 1
    arrival_id = interleave.record_arrival(
        args.state_dir, channel=snap["channel"], turn_id=record["turn_id"], text=snap["text"],
        layer=verdict["layer"], verdict=verdict["verdict"], confidence=verdict["confidence"],
        reason=verdict["reason"], arrived_offset_sec=snap["offset"], typed=snap["typed"],
        fold_depth=fold_depth, verdict_latency_sec=landed - started, turn_still_live=still_live)
    log(f"• interleave ({interleave_mode(args)}): {verdict['verdict']} via {verdict['layer']} "
        f"depth={fold_depth} live={still_live} in {landed - started:.2f}s — {verdict['reason']}")
    if arrival_id is None:
        return  # the append failed; there is nothing for a resolution row to name
    if still_live:
        record["pending"].append({"arrival_id": arrival_id, "landed": landed,
                                  "offset": snap["offset"]})
        # Phase 2 — the only branch that does anything OFF observe's own behaviour. §5.1's three
        # non-negotiables, all checked before the interrupt is sent: typed (3), a STEER verdict, and
        # CONTIGUOUS — this arrival's queue index is exactly the next one after everything already
        # queued to fold this turn (1). A steer on a non-contiguous item (a hold sits between it and
        # the head) is still logged as a steer above — the telemetry is unchanged — it just isn't
        # acted on, which is what keeps a fold chain from ever skipping over an un-folded message.
        session = state.session
        fold_queue = record.setdefault("fold_queue", [])
        eligible = (interleave_mode(args) == interleave.MODE_LIVE
                   and verdict["verdict"] == interleave.STEER
                   and snap.get("typed")
                   and snap.get("queue_index") is not None
                   and snap["queue_index"] == 1 + len(fold_queue)
                   and session is not None and hasattr(session, "send_interrupt"))
        if eligible:
            session.send_interrupt()
            fold_queue.append({"arrival_id": arrival_id, "channel": snap["channel"],
                               "text": snap["text"]})
            # §5.2(c)'s fix: capture EVERY folded message explicitly, at the fold, not once at the
            # end — a crash mid-chain must still leave what the owner said on disk. Same shape as the
            # head's own capture (drainer_task, `attempts == 1`), redacted the same way.
            _capture_turn(args, log, surface=snap["channel"], speaker="owner", text=snap["text"],
                         turn_id=record["turn_id"], redacted=record.get("private_turn", False),
                         session_id=getattr(session, "session_id", None))
            _tee_chat_event(state, args, log, cockpit_pipe.chat_event(
                "interleaved", source=snap["channel"], turn_id=record["turn_id"],
                text_preview=snap["text"][:200], layer=verdict["layer"], verdict=verdict["verdict"]))
            log(f"• interleave (live): interrupting turn {record['turn_id']} to fold in "
                f"depth={fold_depth} — {verdict['reason']}")
        return
    # The verdict outran its own turn. Resolve it here rather than leaving it unresolved: the turn is
    # over, so `turn_remaining_sec` is knowable now — and NEGATIVE, which is exactly the number §6's
    # corollary is read off. If it comes back mostly negative the answer is not a bigger timeout.
    ended = record.get("ended")
    total = max(0.0, (ended - record["started"])) if ended else 0.0
    interleave.record_resolved(
        args.state_dir, arrival_id=arrival_id, turn_id=record["turn_id"],
        turn_remaining_sec=(ended - landed) if ended else None,
        arrived_offset_frac=(snap["offset"] / total) if total > 0 else None,
        turn_total_sec=total or None)


# --------------------------------------------------------------------- phase 2 — interrupt & continue

async def interleave_live_send(state: "DaemonState", args, log, prompt: str, send_kwargs: dict):
    """§5's mechanics, in full: send one turn; if `interleave_observe`'s live-mode branch decided to
    fold something in WHILE it was running, it already called `session.send_interrupt()` and queued
    the fold onto `state.inflight_turn["fold_queue"]` — this is what notices, builds the continuation,
    and sends it as a FURTHER `_send_turn`, in this SAME drainer iteration (step 4), repeating for as
    long as another fold lands during the continuation itself (step 4′, §5.1.1: no cap).

    **A dead man's switch by construction, not by a special case.** `off`/`observe` never reach the
    loop body more than once, because nothing outside `live` mode ever calls `send_interrupt()` or
    writes to `fold_queue` — the very first `session.send()` either returns a real reply (the ordinary
    path, byte-identical to calling `session.send()` directly) or `None` with
    `interrupt_requested` false (a real death/timeout, handled by the drainer exactly as before). Live
    mode reaches the loop body only when an interrupt genuinely landed, and if the interrupt frame is
    ever LOST in flight (the CLI never sees it, or a race leaves `fold_queue` empty when this reads
    it), that reads identically to "no fold" and this returns `None` — the turn runs to its natural
    end on its own next read, exactly the degrade §5 names as needing no recovery path.

    Returns `(reply, folded)` — `folded` is the ordered list of `{"arrival_id","channel","text"}`
    dicts actually consumed (empty on the ordinary, non-interleaved path, which is every `off`/
    `observe` turn and the overwhelming majority of `live` ones too)."""
    session = state.session
    folded: list[dict] = []
    text = prompt
    while True:
        reply = await asyncio.to_thread(functools.partial(session.send, text, **send_kwargs))
        if reply is not None:
            return reply, folded
        if not getattr(session, "interrupt_requested", False):
            return None, folded  # a real death/timeout — the drainer's existing branch handles it
        record = state.inflight_turn
        new_folds = list(record.get("fold_queue") or []) if record is not None else []
        if record is not None:
            record["fold_queue"] = []
        if not new_folds:
            # Interrupted with nothing (yet) queued to fold — a stray control_response, or a race
            # against the tiny gap between one send() returning and the next one starting (see the
            # module's own note on this in interleave_observe). Nothing to continue WITH, so this
            # degrades to the same "real failure" branch above rather than looping on empty input.
            return None, folded
        folded.extend(new_folds)
        landed_clause = None
        pending_tool = record.get("pending_tool_call") if record is not None else None
        if pending_tool:
            landed_clause = interleave.possibly_landed_clause(
                pending_tool.get("name"), pending_tool.get("input_preview"))
            interleave.record_mcp_possibly_landed(
                args.state_dir, turn_id=record.get("turn_id"), tool_name=pending_tool.get("name"),
                input_preview=pending_tool.get("input_preview"))
            record["pending_tool_call"] = None
        text = interleave.build_continuation_prompt(
            [f["text"] for f in new_folds], landed_clause=landed_clause)
        log(f"• interleave (live): continuing with {len(new_folds)} fold(s) "
            f"({len(folded)} total this turn)"
            + (" — possibly-landed MCP call named in the prompt" if landed_clause else ""))


def pop_folded_prefix(state: "DaemonState", head_channel: str, head_text: str,
                      folded: list[dict]) -> int:
    """§5.3: verify the whole prefix — the head plus every folded message, in order — against the
    durable queue's CURRENT head before popping any of it, then pop the longest matching prefix.
    Never by count alone, never by index alone: a reminder or a job notice that slipped in ahead of a
    fold (or a restart that lost the in-RAM turn window) means the prefix stops matching partway, and
    everything past the mismatch is left queued to take its own turn, exactly as an ordinary single
    pop already leaves everything behind the head.

    Returns how many entries were actually popped — 1 when `folded` is empty, identical to the plain
    `state.pending.pop(0)` this replaces on every `off`/`observe` turn and on a `live` turn that never
    folded anything."""
    expect = [(head_channel, head_text)] + [(f["channel"], f["text"]) for f in folded]
    matched = 0
    for ch, tx in expect:
        if matched >= len(state.pending):
            break
        item = queue_item(state.pending[matched])
        if item[0] != ch or item[1] != tx:
            break
        matched += 1
    for _ in range(matched):
        state.pending.pop(0)
    return matched


# --------------------------------------------------------------------- mid-turn arrival marker (a′)

# `docs/mid-turn-interleave-spec.md` §8(a′) — the whole of phase 1, and the spec calls it "the
# cheapest real win in the document".
#
# The defect it fixes is conversational, not mechanical. Typed messages routinely arrive while the
# assistant is already answering the previous one, and some are the owner *replying to a reply they
# had not yet been sent*. The warm session has no way to tell: by the time the message reaches it it is
# simply the next thing in the queue, so an addition ("oh, and also X") reads as a response to the
# answer it just gave. One line says otherwise.
#
# **The precedent is FABLE_HINT_LINE, deliberately** — a local, prompt-only variable attached to one
# turn's prompt and nothing else. No interrupt, no queue change, no new consumer, no classifier, no
# Ollama call. Nothing here is persisted, so a restart simply loses the marker, which costs ~30 tokens
# of context and never a message.
ARRIVAL_MARKER_LINE = (
    "[context: the owner sent this while you were still answering their previous message — they "
    "hadn't seen that reply yet, so it isn't a response to it.]"
)

# A leak bound, not a policy. An entry is normally consumed by the turn that answers it, but two paths
# pop the queue without ever building a prompt (the dead-letter guard) or rewrite the queued text under
# it (an inbound edit), and either strands its entry here. Capped FIFO so a stranded entry ages out
# instead of accumulating for the life of the process; the cost of dropping one is a missing hint.
MIDTURN_ARRIVAL_CAP = 32


def turn_in_flight(state: "DaemonState") -> bool:
    """Is the drainer mid-turn right now? `inflight_text` is claimed when the head is taken and reset
    at the top of every drainer iteration, so it is non-None for exactly the span "the assistant is
    answering something" — including delivery, which is still time the owner is waiting on a reply."""
    return getattr(state, "inflight_text", None) is not None


def note_midturn_arrival(midturn_arrivals: list, text: str) -> None:
    """Remember that `text` landed while a turn was in flight. Pure; the caller owns the list."""
    midturn_arrivals.append(text)
    while len(midturn_arrivals) > MIDTURN_ARRIVAL_CAP:
        midturn_arrivals.pop(0)


def take_arrival_marker(midturn_arrivals: list, text: str) -> str | None:
    """Consume `text`'s mid-turn arrival note, if it has one, and return the marker line to prepend.

    Matched by text rather than by an id, the same handle `replace_queued_inbound` already keys on:
    the queue holds post-transform text and this runs against the very entry the drainer claimed.
    Consumed on the first prompt built for it, exactly like `fable_hints.pop(0)` — so a retry after a
    failed delivery goes out unmarked. That is the cheap direction: a marker is worth ~30 tokens, and
    holding entries open across retries is how a text-keyed list starts marking the wrong message.

    Two identical mid-turn messages produce two entries and consume one each, in order. Two identical
    messages where only one arrived mid-turn can attach the marker to the wrong one; the cost is a
    slightly wrong hint on a line that is advisory by construction."""
    try:
        midturn_arrivals.remove(text)
    except ValueError:
        return None
    return ARRIVAL_MARKER_LINE


# --------------------------------------------------------------------------- force-route (!fable)

# A leading "!fable" (any case, an optional ":"/"," and whitespace after) on ANY inbound channel —
# Telegram/Discord typed literally, or synthesized by cockpit_task.on_chat_send when the browser's
# "Send to Fable" toggle (force_fable) is set — force-routes this turn. Bypasses the router classifier
# entirely; NEVER the approval gate (cockpit-spec.md / GROUNDING's delegation section).
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


# --------------------------------------------------------------------------- private turn (!private)

# A leading "!private" (any case, an optional ":"/"," and whitespace after) on ANY inbound channel
# marks this turn as one that must NOT be captured verbatim — one of the conversation log's two
# privacy controls (the other, retro-redact, is `turns.py`'s). The same deliberately narrow, leading-token-only shape as FORCE_FABLE_PREFIX_RE, for the same reason:
# "!private" buried mid-sentence is a word, not a command.
#
# It changes exactly two things and nothing else: the prefix is stripped before the text reaches the
# warm session (so the turn reads naturally without a grounding change telling the assistant what the
# token means), and `turns.record_turn` writes a tombstone — `redacted: true`, no `text` — for BOTH
# sides of the turn. Redacting only the owner's half would be theatre: the reply routinely restates
# what they said, so the content this control exists to keep out of the corpus would land in it anyway, one row later.
# Nothing about delivery, the approval gate, `assertions.jsonl` or the cockpit transcript changes.
PRIVATE_PREFIX_RE = re.compile(r"^\s*!private\b[:,]?\s*", re.IGNORECASE)
PRIVATE_TRIGGER = "!private"


def strip_private(text: str) -> tuple[bool, str]:
    """Detect + strip a leading `!private` prefix. Returns (private, remaining_text) — pure and
    unit-testable. A bare `!private` with nothing after it still marks the turn private; the warm
    session sees an empty message and can ask what was meant, exactly as a bare `!fable` does."""
    m = PRIVATE_PREFIX_RE.match(text or "")
    if not m:
        return False, text or ""
    return True, text[m.end():].strip()


def _capture_turn(args, log, **kw) -> None:
    """Write one `seneschal.turn/1` row, best-effort — the phase-0 capture seam (turns.py).

    `record_turn` already never raises, so this wrapper is not a safety net; it is the ONE place the
    daemon's capture policy lives, so the two call sites stay one line each and a `--stub-send` harness
    run cannot write conversation rows for a conversation nobody had. Kept deliberately quiet: a
    capture miss is worth a log line, never a turn."""
    if getattr(args, "stub_send", False):
        return
    if not turns.record_turn(getattr(args, "state_dir", None), **kw):
        log("! chat-turn capture failed (row lost, turn unaffected)")


# `!status` (or `/status`): answer from the daemon's own snapshot WITHOUT spawning or waking the warm
# session. Deliberately whole-message-only — "!status of the report work" is a question for the
# assistant, not a daemon command — the same narrow, unambiguous posture as FORCE_FABLE_PREFIX_RE above.
STATUS_COMMAND_RE = re.compile(r"^\s*[!/]status\b\s*$", re.IGNORECASE)

_RESPAWN_REASON_LABELS = {
    RESPAWN_IDLE: "idle wind-down",
    RESPAWN_TURN_ERROR: "turn error",
    RESPAWN_TURN_TIMEOUT: "turn timed out",
    RESPAWN_UNDELIVERED: "undelivered reply",
    RESPAWN_SHUTDOWN: "daemon restart",
}


def turn_timeout_notice(gap_sec: float = TURN_IDLE_GAP_SEC) -> str:
    """What the assistant says when the idle-gap watchdog cut a turn off (hung-turn-deadline-spec P1).

    Its own wording rather than the borrowed mid-turn-death apology: "I hit a snag" describes something
    that went wrong and *finished*, and this one didn't finish. The honest sentence is that it hung and
    got cut off. Deliberately claims nothing about what happens next — the retry is silent, and the
    dead-letter on the last attempt already says "set it aside" in its own words.

    The duration is DERIVED from the live constant rather than written into the string, so re-tuning the
    gap can't leave a notice quoting a number that is no longer true."""
    mins = gap_sec / 60.0
    span = f"{int(round(mins))} minutes" if mins >= 1.5 else f"{int(round(gap_sec))} seconds"
    return (f"That one hung — {span} without a word out of me, so I cut the turn off rather than "
            "leave you sitting there waiting on it.")


def is_status_command(text: str) -> bool:
    """True when this inbound message is the local `!status` command. Pure and unit-testable."""
    return bool(STATUS_COMMAND_RE.match(text or ""))


def render_status_reply(snap: dict) -> str:
    """Render a status snapshot as one short human line-pair, in the assistant's register.

    Why this exists at all: a wound-down session, a healthy idle one, and a dead daemon are
    indistinguishable from outside. This is the answer to that — and it must be answerable when the
    warm session IS the problem, which is why it renders from a dict rather than asking the model. The context figure is always labelled *est.*; it's a proxy (see
    context_tokens_from_usage), and a gauge that hides its own error bars is worse than no gauge."""
    model = snap.get("model") or "—"
    if snap.get("session_up"):
        bits = [f"**Warm session:** up · {model}"]
        age = snap.get("session_age_sec")
        if isinstance(age, (int, float)):
            bits.append(f"age {int(age // 60)}m{int(age % 60):02d}s")
        turns = snap.get("turns_served")
        if isinstance(turns, int):
            bits.append(f"{turns} turn{'' if turns == 1 else 's'}")
        ctx = snap.get("context_tokens")
        if ctx:
            pct = snap.get("context_pct")
            bits.append(f"ctx ~{ctx // 1000}k{f' ({pct}% est.)' if pct is not None else ' (est.)'}")
        cost = snap.get("session_cost_usd")
        if isinstance(cost, (int, float)):
            bits.append(f"${cost:.2f} this session")
        if snap.get("spawn_fallback_used"):
            bits.append("⚠️ spawn-fallback used")
        head = " · ".join(bits)
    else:
        reason = _RESPAWN_REASON_LABELS.get(snap.get("last_respawn_reason") or "")
        head = (f"**Warm session:** down{f' (last ended: {reason})' if reason else ''} · dial {model}")
    queue = snap.get("queue_depth") or 0
    tail = f"{queue} queued · {'mid-turn' if snap.get('turn_in_flight') else 'nothing in flight'}"
    # Un-landed Notion writes (the outbox is Notion-only), but ONLY when there are some: a
    # permanently-visible "0 pending" is a gauge the owner learns to stop reading, and the whole point of this line is that it should be startling.
    pending, dead = snap.get("outbox_pending") or 0, snap.get("outbox_dead") or 0
    if pending or dead:
        age = snap.get("outbox_oldest_sec")
        bits = []
        if pending:
            bits.append(f"{pending} un-landed Notion write{'' if pending == 1 else 's'}"
                        + (f" (oldest {age / 3600:.1f} h)" if isinstance(age, (int, float)) else ""))
        if dead:
            bits.append(f"{dead} dead-letter{'' if dead == 1 else 's'}")
        tail += "\n**Outbox:** " + " · ".join(bits)
    return f"{head}\n{tail}"


def deliver_reply(channel: str, reply: str, args, log, retries: int = 1, kind: str = "reply",
                  turn_id: str | None = None, redacted: bool = False,
                  turn_origin: str | None = None, session_id: str | None = None,
                  scope: dict | None = None, topic=None,
                  channel_purpose: str | None = None) -> bool:
    """Did it land? The bool face of :func:`deliver_reply_result`, and the one nearly every caller
    wants. See that function for everything this does; see it especially if you are about to retry on
    a `False`, because **`False` does not mean "nothing was sent"** — an ambiguous failure is a
    request that went out and may already have been delivered, and only the dict form can tell you
    which one you have."""
    return deliver_reply_result(channel, reply, args, log, retries=retries, kind=kind,
                                turn_id=turn_id, redacted=redacted, turn_origin=turn_origin,
                                session_id=session_id, scope=scope, topic=topic,
                                channel_purpose=channel_purpose)["ok"]


def _resolve_channel_purpose_thread(channel_purpose: str | None, topic, args, log) -> int | None:
    """message-routing-spec.md phase 2: resolve a DECLARED, already-resolved channel purpose to a
    Telegram thread id — the destination that wins over the inbound thread. `None` throughout means
    the main chat, and every path here is fail-open: nothing may cost the reply.

    **Resolved in-process, not via `telegram_send.py --topic`.** That subprocess flag has its own
    complete fail-open ladder (`telegram_topics.thread_id`'s own docstring: unknown purpose, topics
    off, unreachable `getMe`, unreadable state, failed creation — all land on the main chat), but it
    has no way to know what the INBOUND thread was, so it cannot fall back to it — only to main. The
    rule here is stronger: fall back to the INBOUND thread first, and only then to main, so a
    resolution hiccup does not throw away a perfectly good thread the message already had a home in.
    That needs the resolution done here, where `topic` (the inbound thread) is in scope — the same
    `telegram_send.cfg`/`load_env` in-process pattern `_nudge_topics_toggle` already uses to read
    Telegram config without a subprocess, for the same reason: this is a resolve, not a send, so it
    carries none of the duplicate-send risk that keeps an actual send behind `telegram_send.py`.

    **Under `--stub-send`** (the offline test harness) no credentials exist and none should be used:
    resolution reads only `telegram_topics.stored_thread_id` — a plain state-file read, never an API
    call, and never a topic creation. A purpose with no already-stored thread reads exactly like a
    genuine resolution failure would in production: fall back to the inbound thread, then main."""
    if not channel_purpose or channel_purpose == telegram_topics.TOPIC_MAIN_CHAT:
        return None
    resolved = None
    try:
        if getattr(args, "stub_send", False):
            resolved = telegram_topics.stored_thread_id(args.state_dir, channel_purpose)
        else:
            import telegram_send as tsend  # noqa: PLC0415 — lazy; only a Telegram turn ever needs this
            c = tsend.cfg(tsend.load_env(args.telegram_env))
            if c.get("token") and c.get("chat_id"):
                resolved = telegram_topics.thread_id(c, channel_purpose, args.state_dir, log=log)
    except Exception as e:  # noqa: BLE001 — a resolution failure may never cost the reply
        if log:
            log(f"! channel-purpose resolution for {channel_purpose!r} failed ({e}); "
                f"falling back to the inbound thread")
    if resolved is not None:
        return resolved
    if topic is not None and thread_key(topic) != MAIN_THREAD_KEY:
        return int(topic)
    return None


def _delivered_thread_for_append(channel: str, resolved_purpose: str | None, topic, args) -> int | None:
    """The drainer's own echo of `_resolve_channel_purpose_thread`'s decision, for `append_thread` —
    mirrors that function's branches exactly, but reads the (now-current) state file rather than
    re-resolving, because a successful purpose-routed send has already persisted its topic's thread
    id via `telegram_topics.create_topic` before `deliver_reply` ever returns. Cheap (a state-file
    read, never an API call) and safe to call after ANY delivery, stub or real.

    **Every branch here has to answer "where did the reply actually land", not "what was the inbound
    thread"** — an explicit `main` declaration must echo `None` (the main chat) even though `topic`
    (the inbound thread) may be a real, different thread; the bug this guards is a continuity cache
    that quietly disagrees with where the words themselves went, which is worse than no cache at all
    (this module's own founding argument in `docs/telegram-capability-map.md` §2.1)."""
    if channel != "telegram":
        return topic
    if not resolved_purpose or resolved_purpose == telegram_topics.TOPIC_MAIN_CHAT:
        return None
    stored = telegram_topics.stored_thread_id(args.state_dir, resolved_purpose)
    if stored is not None:
        return stored
    return topic if (topic is not None and thread_key(topic) != MAIN_THREAD_KEY) else None


def deliver_reply_result(channel: str, reply: str, args, log, retries: int = 1, kind: str = "reply",
                         turn_id: str | None = None, redacted: bool = False,
                         turn_origin: str | None = None, session_id: str | None = None,
                         scope: dict | None = None, topic=None,
                         channel_purpose: str | None = None) -> dict:
    """Send the assistant's reply back to the channel it came from, with one retry — **except on an
    ambiguous failure, which is never retried** (see the loop below) — and report whether it
    actually landed. Previously the loop fired send_telegram() and ignored the result — a silently
    dropped reply (transient Telegram error) still got recorded as a delivered turn, so the owner saw
    nothing while the warm session believed it had answered. Callers MUST gate the thread append on
    this so continuity stays honest.

    This is **Door B** of the Mouth (docs/mouth-spec.md §3.1) and the funnel for nearly all of it: turn
    replies, the wake line, the backlog ack, the dead-letter notice, the spawn-fallback alert, and —
    via `_reconcile_jobs`'s `_notify` — every job completion push. So phase 0's assertion row is
    written here, once, on the landed branch. `kind` lets a caller label what it is (`jobs` passes
    `"job"`); it changes nothing about the send.

    It is also **the assistant's side of the chat-turn capture** (turns.py), for
    the same reason and on the same branch: this is the one funnel that already holds the full,
    untruncated reply text and already knows it landed. `turn_id` (minted by the drainer for the
    cockpit tee) joins this row to the message it answers, with no heuristic and no new identifier;
    `redacted` is the `!private` tombstone for both halves of a turn. Both default to "not part of a
    conversation turn", which is the honest reading for a job push or the crash-loop notice.

    `turn_id` and `scope` also ride onto the **assertion** row here (docs/mouth-spec.md) — the
    correlator that joins a claim to its turn, and the derived record of what that turn actually
    looked at. **Zero behaviour change: nothing reads either field, and no
    clause, hedge or prompt-side rule is built on them.** They are supplied only by the drainer, i.e.
    only where the assertion IS a warm turn's reply; every other caller of this funnel — a job push,
    the wake line, the dead-letter notice, `!status` — passes neither, deliberately. A job push's
    claim did not come out of the turn's tool calls, and attaching that turn's scope to it would be
    the field asserting something it did not measure.

    **RETURNS `{"ok": bool, "ambiguous": bool}` — the phase, not just the verdict.**
    The loop below already refuses to retry an ambiguous failure, and then reported it to its caller
    as a plain `False`, indistinguishable from a connection that was refused before a byte moved.
    Callers that retry on `False` therefore rebuilt the duplicate one layer up: `jobs.reconcile`
    re-sends an un-notified terminal record on the NEXT TICK, which is the same blind retry with a
    5 s gap instead of a 2 s one. `ok` is exactly the old bool; `ambiguous` is true only when the
    request provably went out. **`ambiguous` implies `ok` is False** — an ambiguous send is not a
    success, it is a send whose outcome we cannot claim to know, and anything that treats it as
    delivered must say so on its own record rather than here.

    **`topic` sends the reply back into the private-chat thread the message came from**
    (`docs/telegram-capability-map.md` §2.1), and it defaults to `None`, which is the main chat and
    therefore today's behaviour for every caller that does not pass one. It stays the ONLY destination
    signal for a job push, the wake line, the backlog ack, a reminder — every unsolicited surface, and
    every non-Telegram channel — untouched and still landing in the main chat, deliberately. Discord
    and the cockpit have no topics at all, so the argument is `None` there by construction.

    **`channel_purpose` is message-routing-spec.md's phase-2 destination** and, when set
    on a Telegram turn, WINS over `topic` unconditionally — replying from inside a topic does not
    auto-satisfy the declaration requirement, so the two are allowed to disagree and the declared one
    is what is used (`_resolve_channel_purpose_thread`). `None` (every caller above except the
    drainer's own chat-turn delivery) leaves `topic` as the sole signal, byte-for-byte as before.
    Resolution failure — an unknown purpose, topics off, an unreachable `getMe`, an unreadable state
    file, a failed creation — falls back to the INBOUND thread (`topic`) and only then to the main
    chat; it can never cost the reply, only its destination.

    **A thread Telegram REFUSES costs the thread, never the reply.** The owner deleted the topic, or
    the bot was re-pointed: the send is `rejected`, so the loop was going to re-send these words anyway
    and all the fallback changes is the DESTINATION of a re-send that was already going to happen.
    Only `rejected` qualifies — `ambiguous` means the reply may already be on the owner's phone, and
    `undelivered` is a transport failure the thread had nothing to do with; re-sending either of
    those without the thread would manufacture a duplicate or dress a network outage up as a stale
    topic. Same rule and same reasoning as `telegram_ask.ask`'s. **The drop does not consume one of
    the retries** — that budget is for a flaky wire, and spending it on a thread we have already
    given up on is how this guard loses the reply it exists to save.

    **RETURNS a `"thread"` key too** — the actual resolved destination (an int, or `None` for the
    main chat) — so a caller that also maintains a per-topic cache (`append_thread`) can file the
    reply under where it ACTUALLY went rather than where the question came from."""
    # Resolved ONCE, before either early-return branch below, so the stub-send harness and a real
    # send agree on the destination — message-routing-spec.md phase 2.
    thread = None
    if channel == "telegram":
        if channel_purpose is not None:
            thread = _resolve_channel_purpose_thread(channel_purpose, topic, args, log)
        elif topic is not None and thread_key(topic) != MAIN_THREAD_KEY:
            thread = int(topic)

    if getattr(args, "stub_send", False):
        # Offline harness: never touch a real channel (a stub-brain run with a live telegram.env once
        # messaged the owner for real). Record the would-be send so tests can assert on it. Deliberately
        # NOT an assertion row: nothing reached the owner, and the log's whole job is answering what
        # they actually believe — a stubbed send with `delivered: true` would be a lie in the one file
        # that exists to be trusted.
        #
        # **`topic` IS RECORDED HERE BECAUSE A WOULD-BE SEND INTO A THREAD IS A DIFFERENT SEND**.
        # This row is the only thing an offline end-to-end test can see, and without
        # the field the whole outbound half of private-chat topics is unobservable from the harness:
        # the drainer could pass `topic=None` forever and every unit test would stay green, because
        # each function works perfectly on an argument nobody hands it. Additive and absent-not-null,
        # like every other layer of this feature, so a run with topics off writes the row it wrote
        # before — which is what lets the existing assertions here stay exact-equality. `thread` is
        # already the phase-2-aware resolution (declared purpose, else the inbound topic), so a
        # declared purpose that differs from the inbound thread shows up here exactly as it would in
        # a real send.
        row = {"channel": channel, "text": reply}
        if channel == "telegram" and thread is not None:
            row["topic"] = thread
        with open(os.path.join(args.state_dir, "sent.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        log(f"[stub-send:{channel}] {reply[:80]!r}")
        return {"ok": True, "ambiguous": False, "thread": thread}

    if channel == "cockpit":
        # Cockpit-origin turns are "delivered" via the live transcript stream (chat.events teed to
        # BOTH the ring buffer and the connected pipe client, in drainer_task) rather than a separate
        # outbound push — there's no third-party API to call for a browser tab. Always succeeds so
        # continuity (append_thread) records the reply and the durable queue pops it; a reconnecting
        # cockpit backfills from state/warm-transcript.jsonl regardless of whether a client happened
        # to be attached mid-turn — never lossy, matching every other cockpit-pipe failure mode.
        mouth.record_assertion(args.state_dir, surface="cockpit", kind=kind, speaker="daemon",
                               text=reply, session_id=session_id, turn_id=turn_id, scope=scope)
        _capture_turn(args, log, surface="cockpit", speaker="assistant", text=reply,
                      origin=turn_origin or ("human" if kind == "reply" else "system"),
                      turn_id=turn_id, redacted=redacted)
        return {"ok": True, "ambiguous": False, "thread": None}

    # `thread` was already resolved above (before the stub-send branch) — a positive int or None,
    # and `None` is byte-for-byte the pre-topics send. Nothing below needs to think about it again.
    def send_once() -> dict:
        if channel == "discord":
            return send_discord(reply, args.discord_env) or {}
        if thread is None:
            # The CALL is byte-identical too, not just the payload — the same absent-not-null rule
            # this feature holds at every other layer, applied one layer up. It costs a branch and
            # buys the guarantee that a send with no topic cannot be changed by anything downstream
            # of here, including a `message_thread_id=None` that some future wrapper decides to
            # forward. It is also why the many `send_telegram` test doubles in this tree keep their
            # two-argument signature: nothing passes them a third argument they never asked for.
            return send_telegram(reply, args.telegram_env) or {}
        return send_telegram(reply, args.telegram_env, message_thread_id=thread) or {}

    attempt = 0
    while attempt <= retries:
        res = send_once()
        if res.get("ok"):
            # Remember what this message was, so a reaction to it later has something to point at — a
            # 👍 on "want me to send it?" only reads as "yes" if we know what the owner 👍'd.
            if channel == "telegram":
                record_sent_message(args.state_dir, res, "reply", reply)
            mouth.record_assertion(args.state_dir, surface=channel, kind=kind, speaker="daemon",
                                   text=reply, session_id=session_id, turn_id=turn_id, scope=scope)
            # `origin`: a turn reply is composed speech, a job/alert push is machine-generated
            # boilerplate — the same human/system split `turns.classify_origin` draws on the owner's side.
            # `turn_origin` lets a caller that knows better override that mapping WITHOUT touching
            # `kind`, which belongs to `assertions.jsonl` and means something different there.
            _capture_turn(args, log, surface=channel, speaker="assistant", text=reply,
                          origin=turn_origin or ("human" if kind == "reply" else "system"),
                          turn_id=turn_id, redacted=redacted)
            return {"ok": True, "ambiguous": False, "thread": thread}
        log(f"! {channel} send failed (attempt {attempt + 1}/{retries + 1}): {res.get('error')}")
        # **An ambiguous failure is not retried, and this line is load-bearing.**
        # `telegram_send` refuses to re-send a request that went out and may already have been
        # delivered — but this loop is the SAME "it failed, send it again" shape one layer up, and
        # without this it would simply move the duplicate here (a long reply is exactly the kind that
        # times out after going out). The flag comes from `telegram_send`'s result JSON
        # (`ambiguous: true`), never from reading the error text. Absent (Discord, an older
        # subprocess, an unparseable result) is falsy and retries as before, which is the
        # conservative direction only because those paths never had the phase information at all.
        if res.get("ambiguous"):
            log(f"! {channel} send was AMBIGUOUS — the request went out and may have been "
                f"delivered; NOT retrying, because a blind retry is the duplicate.")
            return {"ok": False, "ambiguous": True, "thread": thread}
        # A STALE TOPIC MAY NOT COST THE REPLY. The topic was deleted, or the bot was re-pointed:
        # `rejected` is the one class where Telegram answered and refused, so the loop was going to
        # re-send these words anyway — **all this branch changes is the DESTINATION of a re-send
        # that was already going to happen**, which is the whole of its safety argument. It is
        # deliberately not the stronger claim that nothing was delivered: `send_text` chunks, and a
        # rejection on chunk 3 of 4 leaves two on the owner's phone. That partial-duplicate risk is the
        # pre-existing retry's, identical with and without a thread, and narrowing it belongs to
        # `send_text`'s accounting rather than here.
        #
        # **IT DOES NOT SPEND A RETRY** (`attempt` is not advanced). The budget exists for a flaky
        # wire, and burning it on a thread we have already given up on is how the guard loses the
        # reply it exists to save: with `retries=1`, a transport blip followed by a stale-topic
        # rejection would otherwise end the loop having never tried the main chat at all. Bounded to
        # exactly one extra send for the lifetime of the call, because `thread` is None forever
        # after and this branch is guarded on it.
        if thread is not None and res.get("delivery") == "rejected":
            log(f"! telegram refused the reply in thread {thread} — re-sending to the main chat. "
                f"The topic is gone or the bot was re-pointed; the stored id is stale.")
            thread = None
            continue
        if attempt < retries:
            time.sleep(2)
        attempt += 1
    return {"ok": False, "ambiguous": False, "thread": thread}


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
            entry = {"channel": item.get("channel") or "telegram", "text": item["text"],
                     "attempts": attempts}
            # `topic` is ADDITIVE and absent on every entry written before topics existed, so a queue
            # persisted by an older daemon reloads as main-chat messages — which is exactly what
            # they were. Normalized through `thread_key` on the way back in, so a hand-edited or
            # future-shaped value can never reach a path join.
            if item.get("topic") is not None and thread_key(item["topic"]) != MAIN_THREAD_KEY:
                entry["topic"] = int(item["topic"])
            clean.append(entry)
    st["pending"] = clean
    ls = st.get("last_session")
    st["last_session"] = ls if isinstance(ls, dict) and ls.get("id") else None
    return st


def queue_item(item) -> tuple:
    """**The canonical shape of a queue entry: `(channel, text, attempts, topic)`.**

    Every producer may hand in the short forms — `(channel, text)`, `(channel, text, attempts)` —
    because most of them have no topic to name: Discord, the cockpit, the wake line and the job
    pushes all live in one place. Only the Telegram poller supplies a fourth element, and only when
    the message arrived inside a private-chat topic.

    Normalizing here rather than at each unpack site is what keeps the topic from being *silently
    dropped* by one of them. A dropped topic is not an error anywhere — the entry keeps working and
    the reply just goes to the main chat — which is exactly the kind of defect that ships."""
    channel, text = item[0], item[1]
    attempts = item[2] if len(item) > 2 else 0
    try:
        attempts = max(0, int(attempts))
    except (TypeError, ValueError):
        attempts = 0
    topic = item[3] if len(item) > 3 else None
    if topic is not None and thread_key(topic) == MAIN_THREAD_KEY:
        topic = None  # unreadable ⇒ no topic, the same answer `thread_key` gives the cache
    return (channel, text, attempts, int(topic) if topic is not None else None)


def _queue_entry(item) -> dict:
    """Normalize a queue item to its persisted dict. `topic` is written only when there is one, so a
    state file produced by this daemon and read by an older one is byte-identical to what that
    daemon wrote — and so is one produced with topics switched off."""
    channel, text, attempts, topic = queue_item(item)
    entry = {"channel": channel, "text": text, "attempts": attempts}
    if topic is not None:
        entry["topic"] = topic
    return entry


def save_daemon_state(state_dir: str, pending: list, last_session_id: str | None = None,
                      last_session: dict | None = None) -> None:
    """Snapshot the daemon's runtime state (the action queue + light bookkeeping) so the next start
    can resume it. Written after each enqueue/dequeue and on exit — small and cheap (stdlib JSON).
    Each entry carries `attempts` (turns tried without delivery) so the poison-pill guard survives a
    restart — see MAX_TURN_ATTEMPTS.

    `last_session` (Step 2a) is the ENDED session's resume record — `{id, model, context_tokens,
    ended_at, reason}` — which is what `resume_decision` needs and `last_session_id` alone could never
    supply: whether the session died cleanly, how long ago, and how fat it had got. Passing None
    leaves any previously stored record untouched, so the many callers that just persist the queue
    mid-conversation don't have to know about it; only the teardown paths write one. `last_session_id`
    is still written for back-compat with anything reading the old field."""
    payload = {
        "pending": [_queue_entry(i) for i in pending],
        "last_session_id": last_session_id,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if last_session is None:
        prior = load_json(daemon_state_path(state_dir), {})
        if isinstance(prior, dict) and isinstance(prior.get("last_session"), dict):
            last_session = prior["last_session"]
    if last_session is not None:
        payload["last_session"] = last_session
    save_json(daemon_state_path(state_dir), payload)


# ------------------------------------------------------------------- edits to an inbound message

#: How many inbound `message_id`s we keep the enqueued line for, so a later edit can find what it
#: corrects. Bounded exactly like the OUTBOUND map (`sentinel.MESSAGE_MAP_CAP`) and for the same
#: reason: this is a lookup window, not a record — `turns.jsonl` is the record of what was said.
INBOUND_ID_CAP = 200


def resolve_inbound_message_id(ids: dict, text: str) -> tuple:
    """Which Telegram `message_id` was enqueued as exactly this line — `(message_id, None)` when the
    answer is UNIQUE, `(None, why)` when it is not. Phase 5 of `docs/job-origin-routing-spec.md`.

    This is the reverse of `remember_inbound_id`'s lookup, and the reverse direction is the one that
    can be ambiguous: the forward direction is keyed by id and cannot be. **So uniqueness is PROVED,
    not assumed.** Two messages reading `ok` produce two ids for one string, and picking either would
    be a wrong attribution — the single failure mode this whole feature is built to avoid. A tie
    therefore answers `None` and says so, and a caller records `unresolved`.

    The three ways this legitimately finds nothing, each named rather than collapsed into one shrug:

    * **A picker tap or a reaction contributed no id.** Their `message_id` names one of the
      ASSISTANT's messages (`telegram_poll.extract_callback`), not one of the owner's, so
      `_enqueue_inbound` is handed `None` for them on purpose. There is no owner message to point at,
      and inventing one out of the message they tapped ON would say they sent what the assistant sent.
    * **The window has rolled or a reload emptied it.** `inbound_ids` is in-memory and capped at
      `INBOUND_ID_CAP`; it is a lookup window, not a record.
    * **The line was synthesized by the daemon** — a job notice, an attachment marker — in which case
      nobody sent it and the turn is the assistant's own anyway.

    Pure and unit-testable; it reads the map and nothing else."""
    if not isinstance(ids, dict) or not text:
        return None, "no inbound-id window to match this turn against"
    hits = [mid for mid, seen in ids.items() if seen == text]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, ("no Telegram message id was recorded for this line — a picker tap, a reaction "
                      "and a daemon-synthesized line all arrive without one")
    return None, (f"{len(hits)} messages were enqueued as exactly this line, so no single one of "
                  "them can be named as the request")


def remember_inbound_id(ids: dict, message_id, text: str) -> None:
    """Record `text` as **exactly** what this message's id was enqueued as, evicting oldest-first once
    the window is full.

    Called from `_enqueue_inbound` *after* the force-route transform, deliberately: an edit finds its
    target by matching this string against the durable queue, so a key recorded before the transform
    would never match the entry it is supposed to replace."""
    if message_id in (None, ""):
        return
    ids[str(message_id)] = text
    while len(ids) > INBOUND_ID_CAP:
        ids.pop(next(iter(ids)))


def replace_queued_inbound(pending: list, old_text: str, new_text: str, channel: str = "telegram",
                           inflight_text: str | None = None) -> bool:
    """Swap a still-unanswered queued message's text for its edited version, in place. True if it landed.

    This is the valuable half of edit handling: the owner fixed a typo before the assistant got to it,
    so the warm session should simply see the corrected message — one item in, one item out, no second turn and no annotation about
    a correction to something that was never answered.

    **`inflight_text` is the guard that makes this safe, and it is not optional.** The drainer takes
    `pending[0]` into local variables and, on a delivered turn, pops index 0 *by position* — it never
    re-reads the text. So rewriting the entry it is mid-turn on would answer the OLD text and then throw
    the correction away, silently: strictly worse than not handling edits at all. When the head is
    in flight we refuse, and the caller falls through to the already-answered path, which is the honest
    description of what is happening anyway — a turn is being spent on the uncorrected text right now.
    (Refused by TEXT rather than by index: `state.session_busy` alone would not do, because the drainer
    awaits a session spawn between claiming the head and setting that flag.)

    `attempts` is carried over rather than reset. The counter guards the daemon against a message that
    keeps killing turns (MAX_TURN_ATTEMPTS), and an edit is not evidence that the poison is gone.

    `topic` is carried over for the same reason `attempts` is: an edit corrects the words, not which
    thread they were said in, and rewriting the entry without it would answer the corrected message
    in the main chat."""
    if inflight_text is not None and inflight_text == old_text:
        return False
    for i, entry in enumerate(pending):
        if entry[0] != channel or entry[1] != old_text:
            continue
        _ch, _t, attempts, topic = queue_item(entry)
        pending[i] = (channel, new_text, attempts, topic)
        return True
    return False


# ---------------------------------------------------------------- serialize headless store spawns

def prune_children(children: list) -> list:
    """Drop finished child processes in place; keep the ones still running. Returns the same list."""
    children[:] = [c for c in children if getattr(c, "poll", lambda: 0)() is None]
    return children


def heavy_run_in_flight(children: list) -> bool:
    """True while any fire-and-forget headless `claude -p` (a comms peek or a scheduled slot) is still
    running. We gate new peeks/slots on this so at most ONE headless store reader runs at a time —
    two overlapping runs would each fire their own parallel read burst and, on a rate-limited backend
    like Notion, stampede its limit (429s, which is why reads — not the singular writes — get
    throttled). The warm chat session
    is NOT counted here — that's `warm_session_busy()`'s job (below); this covers only the headless
    children, so the two gates compose cleanly."""
    return bool(prune_children(children))


def warm_session_busy(session, pending: list) -> bool:
    """True when the warm Telegram Chat session is actively mid-turn — i.e. there's a live session AND
    queued inbound still to answer this loop. We gate new peeks/slots on this too, so a headless store
    read-burst never overlaps a chat turn's reads (on Notion both hit the same ~3 req/s bucket → 429s). `session.
    send()` is synchronous, so a chat turn that will run *this* iteration is exactly `pending` being
    non-empty; deferring the slot/peek launched at the top of the loop keeps them off the wire while
    chat reads. **Chat is priority and is never delayed** — only the slot waits (it retries next loop,
    reusing the same deferral path as an in-flight headless run). A live session with an empty queue is
    idle between turns, so a slot may launch then."""
    return session is not None and bool(pending)


# --------------------------------------------------------------------------- warm Claude session
#
# WarmSession / StubWarmSession (and the new CodexWarmSession) moved to backends/claude_cli.py and
# backends/codex_cli.py (docs/pluggable-backend-spec.md §3, Phase 2) and re-exported above under
# their original names, unchanged in behavior for claude-cli. Everything else that lived in this
# section — session-continuity bookkeeping that isn't backend-specific — stays here.


def context_pct(context_tokens) -> float | None:
    """context_tokens as a % of CONTEXT_WINDOW_TOKENS — inherits every caveat above, plus an assumed
    window size the CLI never reports. A trend line, not a fuel gauge."""
    if not isinstance(context_tokens, (int, float)) or isinstance(context_tokens, bool):
        return None
    return round(100.0 * context_tokens / CONTEXT_WINDOW_TOKENS, 1)


# The refusal classes, so §2.2's table is a query rather than a grep over prose. Stable strings:
# they are written to disk and will be counted across weeks.
RESUME_OK = "resumed"
RESUME_NO_RECORD = "no_record"
RESUME_NOT_RESUMABLE = "not_resumable"
RESUME_NO_END_STAMP = "no_end_stamp"
RESUME_TOO_OLD = "too_old"
RESUME_NO_CONTEXT = "no_context"
RESUME_CONTEXT_FULL = "context_too_full"


def resume_verdict(last_session, now_epoch: float, winddown_pct=None,
                   max_age_sec: float = RESUME_MAX_AGE_SEC) -> tuple:
    """`(session_id_or_None, human_reason, refusal_class)` — the single implementation.

    `resume_decision` is the two-tuple wrapper every existing caller and test already uses. The class
    lives HERE rather than in a sibling classifier for the obvious reason: two functions walking the
    same five branches drift, and the one that is only read by a log would drift silently.

    Resume is **not free**, and that's what most of this gate is about. It restores the previous
    session's context, so the next turn starts at the OLD context size rather than the ~64k cold floor
    — which is precisely the per-turn cost multiplier the memo argued against for long-lived sessions
    (cost ∝ context × iterations). Trading 16s of re-grounding latency for a permanently fatter context
    is a bad deal; trading it for continuity on a *small* session is a good one. Hence the ceiling:
    resume only below Oikonomos's `context_fill_winddown_pct` — the advisory knob finally gets a real
    job, and it gets it without ever becoming a hard block on a turn.

    The highest-value case this serves is the **deploy reload**: a large share of session deaths are
    Path A restarts, which would otherwise kill a conversation mid-flow and re-ground from a thread tail. Those
    are `daemon_shutdown`, usually seconds old and mid-sized — squarely inside every gate here."""
    if not isinstance(last_session, dict) or not last_session.get("id"):
        return None, "no prior session id", RESUME_NO_RECORD
    reason = last_session.get("reason")
    if reason not in RESUMABLE_REASONS:
        return None, f"last session ended with {reason or 'an unknown reason'} (not resumable)", RESUME_NOT_RESUMABLE
    ended_at = last_session.get("ended_at")
    if not isinstance(ended_at, (int, float)) or isinstance(ended_at, bool):
        return None, "no end timestamp", RESUME_NO_END_STAMP
    age = now_epoch - float(ended_at)
    if age < 0 or age > max_age_sec:
        return None, f"last session is {int(age / 60)}m old (limit {int(max_age_sec / 60)}m)", RESUME_TOO_OLD
    ctx = last_session.get("context_tokens")
    if not isinstance(ctx, (int, float)) or isinstance(ctx, bool) or ctx <= 0:
        # A session that never recorded a turn's usage has nothing worth carrying anyway.
        return None, "no context measurement for the last session", RESUME_NO_CONTEXT
    pct = context_pct(ctx)
    try:
        limit = float(winddown_pct) if winddown_pct is not None else 80.0
    except (TypeError, ValueError):
        limit = 80.0
    if pct is not None and pct >= limit:
        return None, f"last context ~{int(ctx / 1000)}k ({pct}%) at/over the {limit:g}% wind-down mark", RESUME_CONTEXT_FULL
    return last_session["id"], f"resuming (~{int(ctx / 1000)}k context, {int(age / 60)}m old)", RESUME_OK


def resume_decision(last_session, now_epoch: float, winddown_pct=None,
                    max_age_sec: float = RESUME_MAX_AGE_SEC) -> tuple:
    """`(session_id_or_None, human_reason)` — the two-tuple contract every caller and test already
    depends on. `resume_verdict` is the implementation; this exists so adding the class did not become
    a rippling signature change."""
    sid, why, _cls = resume_verdict(last_session, now_epoch, winddown_pct, max_age_sec)
    return sid, why


SESSION_STARTS_FILE = "session-starts.jsonl"


def mark_session_opened(state_dir: str, session_id: str) -> bool:
    """Append the `opened` row that closes the pairing `record_session_start` cannot close alone.

    The decision row is written BEFORE the spawn — that is the point of it, since a spawn that dies on
    its way up must still record why it was cold. So it cannot carry the id of a session that does not
    exist yet. This row carries it, moments later, once the CLI reports one.

    **The pairing is exact, not a heuristic**, and the invariant is worth naming because a timestamp
    heuristic is precisely what this spec exists to remove: the daemon runs **exactly one** warm
    session at a time and this file has **exactly one** writer, so each decision row is closed by the
    next `opened` row and nothing can interleave between them. Whenever that stops being true — a
    second concurrent warm session — this pairing must be replaced, not patched.

    A decision row with no following `opened` row is not a defect: it is a spawn that never came up,
    which is a thing the trace should show."""
    try:
        row = {"at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
               "kind": "opened", "session_id": session_id}
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, SESSION_STARTS_FILE), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001 — same contract as record_session_start: the row, never the spawn
        return False


def _session_opened(args, session_id: str) -> None:
    """Everything that has to happen the moment the CLI first reports a session id. Two things now,
    and they are here together because they answer the same question — *which session is this?* —
    at the only instant it becomes answerable.

    The second one exists because **the id arrives DURING the first send, not before it**: the drainer
    mints `turn_id` and opens the requester pointer while `state.session.session_id` is still None on
    a cold spawn, so without this the first turn of every new warm session would resolve to
    `unresolved`. `stamp_turn_pointer_session` fills in only a still-open, still-anonymous pointer —
    it can never re-point one session's turn at another.

    **Deliberately unguarded**, and that is not an oversight: both callees already own the "never
    raises, the row is what is lost" contract in their own bodies, which is this directory's house
    rule. Wrapping them here would add a second, weaker guarantee in front of a total one — and this
    runs on the stream-reading thread, where neither half may be why a session fails to come up."""
    mark_session_opened(args.state_dir, session_id)
    jobs.stamp_turn_pointer_session(args.state_dir, session_id)


def record_session_start(state_dir: str, cls: str, why: str, last_session=None) -> bool:
    """One row per warm-session spawn, naming whether it resumed and — when it did not — WHY, as a
    stable class (docs/session-continuity-spec.md §6, phase 0).

    **A new file, and the reason is measured rather than preferred.** The obvious home was
    `state/metrics.jsonl`, but `cockpit/server/readers.py`'s usage aggregator counts EVERY row there as
    a turn (`bucket["turns"] += 1`, no filter on shape or writer), so a session-start row would inflate
    the Usage panel's turn count. The other candidate, `presence.log`, is prose — and the whole point of
    this record is that §2.2's refusal table should be a query instead of a grep.

    Fail-open and silent: a spawn must never be blocked by its own bookkeeping. Same contract as
    `mouth.record_assertion` — a failed append costs the row, never the session."""
    try:
        row = {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "kind": "decision",
            "resumed": cls == RESUME_OK,
            "class": cls,
            "why": why,
        }
        if isinstance(last_session, dict):
            for key in ("reason", "context_tokens", "turns_served"):
                if last_session.get(key) is not None:
                    row[key] = last_session[key]
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, SESSION_STARTS_FILE), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001 — the row is never worth the spawn
        return False


def _session_vitals(session) -> str:
    """One-line age/turns/context/cost summary for the log, e.g. `age 18m · 5 turns · ctx ~74k (37%)
    · $0.31`. Tolerant by construction — it's called on teardown paths, including ones reached because
    the session was already misbehaving, so every field is optional and nothing here may raise."""
    bits = []
    try:
        age = session.age_sec() if hasattr(session, "age_sec") else None
        if age is not None:
            bits.append(f"age {int(age // 60)}m{int(age % 60):02d}s")
        turns = getattr(session, "turns_served", None)
        if turns is not None:
            bits.append(f"{turns} turn{'' if turns == 1 else 's'}")
        ctx = getattr(session, "context_tokens", None)
        if ctx:
            pct = context_pct(ctx)
            bits.append(f"ctx ~{ctx // 1000}k{f' ({pct}%)' if pct is not None else ''}")
        cost = getattr(session, "session_cost_usd", None)
        if cost is not None:
            bits.append(f"${cost:.2f}")
    except Exception:  # noqa: BLE001 — a log decoration must never raise on a teardown path
        pass
    return " · ".join(bits) or "no vitals"


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


def stopped_sentinel_path(state_dir: str) -> str:
    return os.path.join(state_dir, "seneschald-stopped")


def clear_stopped_sentinel(state_dir: str) -> None:
    """Revoke the deliberate-stop marker seneschald-control.ps1's Stop-Seneschald writes.

    The watchdog (seneschald_revive.py) refuses to revive while that file exists, so that an auto-revive can
    never fight an intentional `-Action Stop`. Start-Seneschald clears it on the normal path — but a daemon
    that came up ANY other way (Task Scheduler's logon trigger after a reboot, a manual run, a graceful
    self-restart) would otherwise leave a stale sentinel behind that silently disables the watchdog
    forever after. A running daemon is the ground truth that no stop is in effect, so clearing it here
    makes the sentinel self-healing rather than a permanent off-switch nobody remembers setting."""
    try:
        os.remove(stopped_sentinel_path(state_dir))
    except FileNotFoundError:
        pass
    except OSError:
        pass  # best-effort: never let this block startup


# ------------------------------------------------- which Claude account THIS process authenticated as
#
# A code merge has a deploy path (seneschald-update pulls, syncs, and asks for a graceful restart). A
# CREDENTIAL change had none: the `claude` CLI reads its auth when the warm session spawns, so
# `claude /login` changed nothing until something ELSE happened to bounce the daemon — a switch to a
# different account got picked up only by coincidence (an unrelated merge), and on a quiet evening the
# owner would keep hitting the OLD account's limit with no symptom but a billing-shaped mystery.
#
# THE TRIGGER IS THE ACCOUNT IDENTITY, NOT THE CREDENTIAL FILE'S MTIME. ~/.claude/.credentials.json is
# rewritten on every routine OAuth refresh (with nobody logging in), so an mtime trigger would bounce the daemon on a timer forever. An identity is a real
# event; a refresh is not. See seneschal/docs/seneschald-revive-spec.md §9.
#
# NEVER READ, COPY OR STAMP A TOKEN. Only these three identity fields leave this function, and the
# access/refresh tokens in ~/.claude/.credentials.json are not read at all.
CLAUDE_CONFIG_BASENAME = ".claude.json"


def claude_config_path() -> str:
    """Where the CLI keeps its account profile. CLAUDE_CONFIG_DIR relocates the CLI's config, so honour
    it when it actually holds the file, else fall back to ~/.claude.json.

    seneschald-control.ps1's $ClaudeConfigFile resolves this the SAME two-step way ON PURPOSE: the whole
    guard is a comparison between what this function stamped and what that one reads, so if the two
    ever resolved to different files the comparison would be nonsense rather than merely wrong."""
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg_dir:
        candidate = os.path.join(cfg_dir, CLAUDE_CONFIG_BASENAME)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(os.path.expanduser("~"), CLAUDE_CONFIG_BASENAME)


def read_claude_identity(config_path: str | None = None) -> dict | None:
    """{account_uuid, organization_uuid, email} from ~/.claude.json's oauthAccount, or None.

    None means "I could not tell", and every caller must treat that as *take no action* — an
    unreadable file is never evidence that the account changed.

    Only `account_uuid` + `organization_uuid` are the comparison key (see seneschald-control.ps1's
    Get-ClaudeIdentity for the full justification); `email` rides along purely so the log line and the
    Telegram nudge can say WHICH account in words. organizationType / organizationRateLimitTier are
    deliberately NOT stamped: a Pro->Max upgrade or a server-side tier re-bucket is not a credential
    change, and a field nothing compares is a field a future reader will wrongly start comparing."""
    path = config_path or claude_config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:  # noqa: BLE001 - missing, unreadable, malformed: all "I could not tell"
        return None
    acct = cfg.get("oauthAccount") if isinstance(cfg, dict) else None
    if not isinstance(acct, dict):
        return None
    account_uuid = acct.get("accountUuid")
    if not account_uuid:
        return None  # an oauthAccount with no accountUuid cannot key anything
    return {
        "account_uuid": str(account_uuid),
        "organization_uuid": str(acct.get("organizationUuid") or ""),
        "email": str(acct.get("emailAddress") or ""),
    }


# The identity observed at THIS process's boot, held in memory so beat_lock can restore it into a lock
# it had to rebuild from scratch (see beat_lock). Deliberately NOT re-read per heartbeat: the stamp
# must keep meaning "what the daemon started with", and re-reading would quietly turn it into "what is
# on disk right now" — i.e. it would answer its own question and the guard would never fire.
_BOOT_CLAUDE_IDENTITY: dict | None = None


def write_lock(state_dir: str) -> list:
    global _BOOT_CLAUDE_IDENTITY
    _BOOT_CLAUDE_IDENTITY = read_claude_identity()
    payload = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "heartbeat": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    # The lock is the record of the RUNNING PROCESS's facts (pid, started_at, heartbeat), which is
    # exactly what an auth identity is, and it dies with the process — so a stale identity claim can
    # never outlive the daemon that made it. Absent (unreadable config, or a daemon that booted before
    # this shipped) reads downstream as "nothing to compare", never as "the account changed".
    if _BOOT_CLAUDE_IDENTITY:
        payload["claude_identity"] = _BOOT_CLAUDE_IDENTITY
    save_json(lock_path(state_dir), payload)
    clear_stopped_sentinel(state_dir)
    # Stamp this boot for the self crash-loop guard (below) and hand the pruned window back so the
    # caller can decide, in one place, whether we've been booting too fast to keep trying. This is the
    # SINGLE recording point — every real daemon boot goes through write_lock, whatever relaunched it
    # (Task Scheduler restart-on-failure, the ~10-min watchdog revive, a graceful _respawn_detached, a
    # manual start), so counting here catches every loop shape without double-counting any of them.
    return record_boot_attempt(state_dir)


def beat_lock(state_dir: str) -> None:
    lk = load_json(lock_path(state_dir), {}) or {}
    lk["heartbeat"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    lk.setdefault("pid", os.getpid())
    # Restore the boot identity from memory, same reason as the pid setdefault above: load_json falls
    # back to {} on an unreadable read (a mid-write collision), and a heartbeat that rebuilt the lock
    # without it would silently un-stamp a running daemon. The watchdog would then read "nothing to
    # compare" forever and the credential guard would LOOK present while doing nothing — the exact
    # shape of the silently-dropped revival counter that seneschald-revive-spec.md §3.5 warns about.
    if _BOOT_CLAUDE_IDENTITY and not lk.get("claude_identity"):
        lk["claude_identity"] = _BOOT_CLAUDE_IDENTITY
    save_json(lock_path(state_dir), lk)


def release_lock(state_dir: str) -> None:
    try:
        os.remove(lock_path(state_dir))
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------------ self crash-loop guard
#
# The ~10-min watchdog (seneschald-update -> seneschald_revive.py) can REVIVE a dead daemon, but two gaps stay
# open: even a clean single crash is up to a ~10-min
# outage before the watchdog notices, and the graceful-reload path (_respawn_detached) will respawn
# blindly with no loop guard of its own. This guard gives the daemon SELF-awareness of a FAST crash
# loop. Every boot stamps state/boot-attempts.json (pruned to a rolling window); if we've booted more
# than CRASHLOOP_MAX_BOOTS times inside CRASHLOOP_WINDOW_MIN minutes we stop burning into the same
# crash — write a state/seneschald-crashloop sentinel, push ONE loud Telegram alert, and exit WITHOUT
# respawning. The watchdog treats that sentinel exactly like seneschald-stopped and will NOT auto-revive
# while it's present (seneschald_revive.decide), so the two mechanisms cooperate instead of fighting: the
# daemon gives up loudly, the watchdog stands down, and a human (Start-Seneschald clears it) or a
# sustained-healthy run (CRASHLOOP_SETTLE_SEC, below) lifts it. This catches FAST loops; the watchdog's
# own revival budget (seneschald_revive.py, 3 per 60 min) still covers SLOWER ones — different timescales,
# no overlap. See seneschal/docs/seneschald-revive-spec.md (the gap this closes: a daemon that is
# unsupervised after its first graceful restart).
#
# Everything here is FAIL-OPEN and defensive to the bone: it runs on the daemon's own startup path, and
# "a corrupt state cache crashes the daemon" is the exact failure this guard exists to *prevent* (a
# daemon that is down is most often a state crash-loop) — so a garbage boot-attempts.json reads as "no
# prior boots", never an exception, and a bug in the guard must never itself take a healthy daemon down.
BOOT_ATTEMPTS_FILE = "boot-attempts.json"
CRASHLOOP_SENTINEL = "seneschald-crashloop"
CRASHLOOP_MAX_BOOTS = 3      # trip when boots in the window EXCEED this (i.e. the 4th fast boot)
CRASHLOOP_WINDOW_MIN = 5     # rolling window, minutes
CRASHLOOP_SETTLE_SEC = 90    # continuous uptime that counts as "recovered" and clears the loop state.
                            # Mirrors seneschald-control.ps1 $ReviveSettleSec / seneschald_revive
                            # DEFAULT_MIN_UPTIME_SEC so the daemon's own "I'm healthy" bar matches the
                            # watchdog's "the revive took" bar.


def boot_attempts_path(state_dir: str) -> str:
    return os.path.join(state_dir, BOOT_ATTEMPTS_FILE)


def crashloop_sentinel_path(state_dir: str) -> str:
    return os.path.join(state_dir, CRASHLOOP_SENTINEL)


def _iso_z(dt: datetime) -> str:
    """The one canonical on-disk instant shape used across the daemon/watchdog: UTC, explicit 'Z'."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_boot_attempts(state_dir: str) -> list:
    """Read boot-attempts.json -> list[datetime] (aware UTC), silently dropping anything unparseable.
    A missing/garbage/wrong-shaped file reads as an empty list — never an exception (see the section
    header: a corrupt state cache must not itself crash the startup path)."""
    raw = load_json(boot_attempts_path(state_dir), [])
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        try:
            out.append(parse_iso(item))
        except (ValueError, TypeError):
            continue  # skip a bad entry; a poisoned stamp can't take the whole window down with it
    return out


def prune_boot_attempts(stamps: list, now: datetime, window_min: float = CRASHLOOP_WINDOW_MIN) -> list:
    """Keep only stamps within the rolling window ending at `now`. A FUTURE stamp (clock skew, a
    hand-edited file) is kept, not dropped — it can't be safely aged out, and discarding it would let a
    fast loop hide behind a bad clock."""
    cutoff = now - timedelta(minutes=window_min)
    return [s for s in stamps if s >= cutoff]


def record_boot_attempt(state_dir: str, now: datetime | None = None,
                        window_min: float = CRASHLOOP_WINDOW_MIN) -> list:
    """Append this boot to boot-attempts.json, prune to the rolling window, persist, and return the
    pruned list (INCLUDING this boot). Best-effort: a write failure still returns the freshest in-memory
    list so the caller's crash-loop check runs on real data even if we couldn't persist it."""
    if now is None:
        now = datetime.now(timezone.utc)
    stamps = prune_boot_attempts(_load_boot_attempts(state_dir), now, window_min)
    stamps.append(now)
    try:
        save_json(boot_attempts_path(state_dir), [_iso_z(s) for s in stamps])
    except OSError:
        pass  # PermissionError/disk trouble: the check below still runs on `stamps`
    return stamps


def is_crashloop(stamps: list, max_boots: int = CRASHLOOP_MAX_BOOTS) -> bool:
    """True when recent boots EXCEED the budget — i.e. more than `max_boots` boots inside the window the
    list was already pruned to. Pure and total; the caller owns the fail-open wrapping."""
    return len(stamps) > max_boots


def _crashloop_alert_text(stamps: list) -> str:
    return (
        "This isn't the assistant talking - it's the daemon's own crash-loop guard. presence.py has "
        f"booted {len(stamps)} times in the last {CRASHLOOP_WINDOW_MIN} min and keeps falling over, so "
        "I've STOPPED relaunching to avoid burning in a loop. The assistant is down until someone "
        "looks: check "
        "seneschal/state/presence.log for the crashing task, fix it, then run seneschald-control.ps1 -Action "
        "Restart. The watchdog won't auto-revive while seneschal/state/seneschald-crashloop exists."
    )


def trip_crashloop_guard(state_dir: str, telegram_env, log, stamps: list,
                         now: datetime | None = None) -> None:
    """We've booted too many times too fast. Write the sentinel that tells the watchdog to stand down,
    and push ONE loud Telegram alert — only when NEWLY tripping. A repeat boot that re-trips while the
    sentinel already exists must not re-spam the owner (same once-not-every-pass discipline the watchdog's
    give-up line uses). Every step is best-effort: this runs on the failing startup path and the guard
    must never itself raise."""
    if now is None:
        now = datetime.now(timezone.utc)
    already = os.path.exists(crashloop_sentinel_path(state_dir))
    try:
        save_json(crashloop_sentinel_path(state_dir), {
            "tripped_at": _iso_z(now),
            "boots_in_window": len(stamps),
            "window_min": CRASHLOOP_WINDOW_MIN,
            "boots": [_iso_z(s) for s in stamps],
        })
    except OSError as e:
        log(f"! crash-loop guard: could not write {CRASHLOOP_SENTINEL} sentinel: {e}")
    log(f"! CRASH-LOOP GUARD TRIPPED — {len(stamps)} boots in {CRASHLOOP_WINDOW_MIN} min; refusing to "
        f"respawn. Wrote state/{CRASHLOOP_SENTINEL}; a human must clear it (Start-Seneschald) or fix the crash.")
    if already:
        log(f"crash-loop guard: {CRASHLOOP_SENTINEL} already present — not re-alerting.")
        return
    text = _crashloop_alert_text(stamps)
    try:
        res = send_telegram(text, telegram_env)
        if isinstance(res, dict) and res.get("ok"):
            # Direct, not queued — this fires as the daemon decides not to respawn itself, so it must
            # not depend on a daemon-drained queue (mouth-spec.md §5.3). It still gets logged.
            mouth.record_assertion(state_dir, surface="telegram", kind="alert", speaker="daemon",
                                   text=text, now=now)
            log("crash-loop guard: alerted the owner on Telegram.")
        else:
            log(f"! crash-loop guard: Telegram alert failed ({res}); the watchdog is the backup alerter.")
    except Exception as e:  # noqa: BLE001 — a send failure must never break the exit path
        log(f"! crash-loop guard: Telegram alert errored ({e}); the watchdog is the backup alerter.")


def clear_crashloop_state(state_dir: str) -> None:
    """Lift the crash-loop guard: remove the sentinel AND reset the boot-attempts window. Called on a
    sustained-healthy run (the daemon proved it can stay up) and by Start-Seneschald (a deliberate human
    intervention) — both mean the past attempts are forgiven and the next crash gets a full budget.
    Best-effort; a missing file is success."""
    for path in (crashloop_sentinel_path(state_dir), boot_attempts_path(state_dir)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass  # best-effort: never let a stuck handle block a healthy tick / startup


def maybe_clear_crashloop_on_sustained(state, args, log) -> None:
    """Once the daemon has been up continuously for >= CRASHLOOP_SETTLE_SEC, clear the crash-loop state
    exactly once per run (state.crashloop_cleared latches it). This is the daemon's own self-heal of the
    sentinel (spec: "clear on a confirmed-sustained healthy run"), the mirror of the watchdog's
    Confirm-PresenceSustained reset. Called every scheduler tick; a cheap flag check short-circuits it
    after the first clear so it's effectively free thereafter. Fail-open."""
    if getattr(state, "crashloop_cleared", False):
        return
    if time.monotonic() - getattr(state, "boot_monotonic", time.monotonic()) < CRASHLOOP_SETTLE_SEC:
        return
    state.crashloop_cleared = True
    try:
        had_sentinel = os.path.exists(crashloop_sentinel_path(args.state_dir))
        clear_crashloop_state(args.state_dir)
        if had_sentinel:
            log(f"crash-loop guard: daemon healthy for >= {CRASHLOOP_SETTLE_SEC:g}s — "
                f"cleared state/{CRASHLOOP_SENTINEL} and reset the boot-attempts window.")
    except Exception as e:  # noqa: BLE001 — self-heal is best-effort, never break the scheduler tick
        log(f"! crash-loop guard: sustained-healthy clear failed (non-fatal): {e}")


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
    peek is redundant this cycle.

    **The child is stamped `SENESCHAL_SESSION_SOURCE=watch`** (`child_env("watch")`), and that stamp is
    load-bearing rather than cosmetic: it is what arms `telegram_send.py`'s ack gate, so a peek that
    wanders out of its email/Slack/calendar lane into the ⏰ reminders cannot push a row the owner has
    already acked today. Prose in `modes/watch.md` alone can't hold this — the peek is a cheap small-model
    one-shot — so the gate lives in the send path and this line is how the send path knows what it's
    serving."""
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
            proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=child_env("watch"), creationflags=NO_WINDOW)
        else:
            proc = subprocess.Popen(args.watch_cmd, shell=True, cwd=REPO_ROOT,
                                    env=child_env("watch"), creationflags=NO_WINDOW)
        if children is not None:
            children.append(proc)
        # Name the child. The launch marker carried nothing identifying for 1,429 lines
        # (`docs/ask-provenance-spec.md` §2.2), so a picker sent BY a peek could not be joined back to
        # the peek that sent it from `state/` at all. **The pid, not the session id**: the CLI mints
        # its own session id inside the child and this process never learns it, so the honest
        # identifier at spawn time is the one we hold. The ask record's own `origin.session_id`
        # (phase 1) is what actually attributes a picker; this is the spawn side of the same question.
        log(f"• comms peek launched (pid {proc.pid})")
    except Exception as e:  # noqa: BLE001
        log(f"! comms peek launch failed: {e}")
    return True


# --------------------------------------------------------------------------- plan-meter readings

# `docs/usage-telemetry-spec.md` §2.6 — the reading lives on THIS loop, not in `jobs.py` and not in a
# Windows scheduled task. `jobs.py` owns a completion push, which is the precise anti-pattern for
# something that fires hourly and must say nothing; a scheduled task is host-side and no PR can
# create one, so it would merge green and do nothing until somebody remembered a manual step on the
# box. The honest cost of this choice, named rather than hidden: **when the daemon is down there are
# no readings**, and the daemon being down is a moment you might want one. Accepted — the gap is
# legible because the next row states its own window (`interval.gap`), and nothing is ever carried
# forward across it.
USAGE_STAMP_FILE = "last-usage-reading"
# §8.1 option A, the spec's recommendation and therefore the default. Under peak observed load the
# weekly meter moves ~1.36 points/hour against its own 1-point granularity, so hourly resolves it to
# the finest step it can express and no finer cadence adds information. **This is the ONE place the
# number lives** — everything else takes it from `args.usage_interval_min`.
USAGE_INTERVAL_MIN_DEFAULT = 60
# How long a due reading may sit behind a deferral gate before it is abandoned and RECORDED as
# abandoned. §2.6: a reading deferred past its window is skipped and recorded as skipped, never
# queued to fire late — a meter reading is only meaningful at the moment it was taken.
USAGE_DEFER_GRACE_MIN = 10
# §8.1's boundary pair, which is not an optimisation: without a PRE-reset reading a week's final
# total is never observed, and without a POST-reset one a reset is indistinguishable from an
# instrument failure. Together they are what makes spec Q5 ("did a scheduled reset actually happen?")
# answerable at all.
USAGE_BOUNDARY_LEAD_MIN = 5
USAGE_BOUNDARY_WINDOW_MIN = 2  # ± around the lead, so a 5 s tick cannot step over it
USAGE_BOUNDARY_MEMORY = 6      # boundary keys remembered in the stamp file (3 weekly windows)


def usage_plan(stamp, now, *, interval_min: int, grace_min: int = USAGE_DEFER_GRACE_MIN,
               week_reset=None, lead_min: int = USAGE_BOUNDARY_LEAD_MIN,
               window_min: int = USAGE_BOUNDARY_WINDOW_MIN):
    """What (if anything) this tick owes the plan-meter series. Pure → unit-tested.

    Returns None, or `{"kind", "reason", "overdue", "expired", "boundary_key"}`:

    * `kind` is `boundary` or `cadence`; `reason` is what goes on the row.
    * `overdue` (cadence only) — the reading has been due longer than the grace, so if a deferral
      gate is still holding the caller must record a `skipped` row rather than keep waiting.
    * `expired` (boundary only) — the ±window closed without a reading. The caller records a
      `skipped` row and moves on. **It must NOT read**: a "pre-reset" reading taken forty minutes
      after the reset is a mislabelled sample, and a mislabelled sample is worse than a missing one.

    A cadence reading that comes due after a long outage is NOT expired and is NOT skipped — it
    fires at once. The window it opens is simply longer than nominal, which the row states."""
    last = usage_probe.parse_iso((stamp or {}).get("at")) if isinstance(stamp, dict) else None
    done = (stamp or {}).get("boundaries") or {} if isinstance(stamp, dict) else {}

    if week_reset is not None:
        wid = week_reset.isoformat()
        span = timedelta(minutes=window_min)
        for side, centre in (("pre", week_reset - timedelta(minutes=lead_min)),
                             ("post", week_reset + timedelta(minutes=lead_min))):
            key = f"{wid}|{side}"
            if key in done or now < centre - span:
                continue
            return {"kind": "boundary", "reason": f"boundary_{side}", "overdue": False,
                    "expired": now > centre + span, "boundary_key": key}

    if interval_min <= 0:
        return None
    if last is None:
        return {"kind": "cadence", "reason": "first_reading", "overdue": False, "expired": False,
                "boundary_key": None}
    due_at = last + timedelta(minutes=interval_min)
    if now < due_at:
        return None
    return {"kind": "cadence", "reason": "cadence",
            "overdue": (now - due_at) > timedelta(minutes=grace_min),
            "expired": False, "boundary_key": None}


def usage_gate(state_dir: str, children: list | None, warm_busy: bool) -> str | None:
    """Which deferral gate is holding, or None. The same three `maybe_peek` rides (§2.6)."""
    if warm_busy:
        return "warm_busy"
    if children is not None and heavy_run_in_flight(children):
        return "heavy_run_in_flight"
    if session_is_live(state_dir, datetime.now(timezone.utc)):
        return "session_is_live"
    return None


def _usage_stamp(state_dir: str, plan, row) -> None:
    """Remember when the last reading happened, which weekly window it saw, and which boundary
    readings are already accounted for. Kept in a SMALL file of its own so the tick never has to
    tail the readings log to decide whether anything is due."""
    path = os.path.join(state_dir, USAGE_STAMP_FILE)
    stamp = load_json(path, None)
    if not isinstance(stamp, dict):
        stamp = {}
    stamp["at"] = (row or {}).get("at") or local_now().isoformat(timespec="seconds")
    stamp["outcome"] = (row or {}).get("outcome")
    window = (row or {}).get("week_window_id")
    if window:
        stamp["week_window_id"] = window
    if plan and plan.get("boundary_key"):
        done = stamp.get("boundaries")
        if not isinstance(done, dict):
            done = {}
        done[plan["boundary_key"]] = stamp["at"]
        stamp["boundaries"] = dict(sorted(done.items())[-USAGE_BOUNDARY_MEMORY:])
    save_json(path, stamp)


def maybe_usage_reading(state_dir: str, args, log, children: list | None = None,
                        warm_busy: bool = False, daemon: dict | None = None,
                        runner=None, account_reader=None) -> bool:
    """On cadence (and at each weekly boundary), read `/usage` and append ONE row to
    `state/plan-usage.jsonl`. Returns True if this tick wrote a row.

    **IT DOES NOT SPEAK, ON ANY PATH.** No push, no nudge, no daily summary, no threshold — spec §5.
    The cure for *"I was surprised by 50 → 73 overnight"* is the recorded series, not a message at
    3 a.m. (The governor's own advisory alerts are a separate surface; this instrument never speaks.)

    **Fail-open, always.** A reading that cannot be taken, cannot be parsed or cannot be written
    costs a row and nothing else: every exit here is a log line, and nothing raises into the tick.
    A missed reading is a missing row, never an incident.

    Runs the ~14 s spawn in a worker thread (`asyncio.to_thread` at the call site), so the event loop
    — Telegram polling, the warm session, every other supervised task — is never blocked by it.

    `runner` is a `subprocess.run`-compatible seam (`job_analysis.run_analysis`'s pattern) so the
    whole path is exercisable with no spawn, no network and no spend. Nothing in production passes
    it. `account_reader` is the same seam for `usage_probe.read_account` — without it a test of the
    SKIP path would open the host's real `~/.claude.json`, which the suite may not do."""
    if getattr(args, "no_usage_reading", False):
        return False
    interval_min = int(getattr(args, "usage_interval_min", USAGE_INTERVAL_MIN_DEFAULT) or 0)
    stamp = load_json(os.path.join(state_dir, USAGE_STAMP_FILE), None)
    # `usage_probe.parse_iso`, NOT sentinel's: sentinel's RAISES TypeError on a None, and the
    # very first tick (and every tick until a reading records a window) hands it exactly that.
    week_reset = (usage_probe.parse_iso((stamp or {}).get("week_window_id"))
                  if isinstance(stamp, dict) else None)
    now = local_now()
    try:
        plan = usage_plan(stamp, now, interval_min=interval_min,
                          grace_min=int(getattr(args, "usage_defer_grace_min",
                                                USAGE_DEFER_GRACE_MIN)),
                          week_reset=week_reset)
    except (TypeError, ValueError) as e:
        log(f"! usage reading: could not decide whether one is due: {e}")
        return False
    if plan is None:
        return False

    gate = usage_gate(state_dir, children, warm_busy)
    if plan["expired"] or (gate and plan["overdue"]):
        # Invariant 1: every attempt writes exactly one row, INCLUDING the ones that never spawn. A
        # run of peeks that died in an auth outage leaves an absence indistinguishable from a quiet
        # morning; this instrument does not repeat that.
        why = gate or ("the boundary window closed" if plan["expired"] else "unknown")
        try:
            interval = _usage_interval(state_dir, now, interval_min)
            since = usage_probe.parse_iso(interval.get("since"))
            # A skip carries the activity snapshot too — it is pure-local and costs no spawn, and
            # WHAT was running when a reading could not be taken is exactly as analysable as what was
            # running when one could. It is also usually the answer: the gate that held is a warm
            # turn or a headless child, both of which this snapshot names.
            activity = (usage_activity.collect(state_dir, since, now, daemon=daemon)
                        if since else None)
            # §4.2.1: a skip is an attempt, so it carries the account block too — same argument as
            # the activity snapshot beside it, and `account_for_row` cannot raise into this path.
            row = usage_probe.build_skipped_row(
                at=now.isoformat(timespec="seconds"), gate=why, interval=interval,
                activity=activity, model=getattr(args, "usage_model", None),
                account=usage_probe.account_for_row(
                    state_dir, usage_probe.last_row(state_dir), account_reader))
            row["reason"] = plan["reason"]
            usage_probe.append_row(state_dir, row)
            _usage_stamp(state_dir, plan, row)
            log(f"• usage reading skipped ({plan['reason']}, {why})")
        except Exception as e:  # noqa: BLE001 — fail-open; a lost row must not touch the tick
            log(f"! usage reading: could not record a skip: {e}")
        return True
    if gate:
        return False  # still inside the grace — try again on the next tick

    if getattr(args, "stub_brain", False):
        log(f"• usage reading (stubbed, {plan['reason']})")
        _usage_stamp(state_dir, plan, {"at": now.isoformat(timespec="seconds"), "outcome": "ok"})
        return True

    try:
        row = usage_probe.take_reading(
            state_dir, claude_bin=args.claude_bin,
            **({"runner": runner} if runner is not None else {}),
            model=getattr(args, "usage_model", usage_probe.DEFAULT_MODEL),
            timeout=int(getattr(args, "usage_timeout", usage_probe.DEFAULT_TIMEOUT_SEC)),
            interval_min=interval_min or USAGE_INTERVAL_MIN_DEFAULT,
            collect=lambda sd, since, until: usage_activity.collect(sd, since, until, daemon=daemon),
            account_reader=account_reader, now_local=now)
    except Exception as e:  # noqa: BLE001 — see the fail-open contract above
        log(f"! usage reading failed and recorded nothing: {type(e).__name__}: {e}")
        _usage_stamp(state_dir, plan, {"at": now.isoformat(timespec="seconds"),
                                       "outcome": "spawn_failed"})
        return False
    _usage_stamp(state_dir, plan, row)
    # One line, and it names the OUTCOME rather than the number. The meters are not logged here on
    # purpose: `presence.log` is quoted to the owner and read by other tools, and a percentage in it is
    # a usage level being spoken by a surface that was told never to speak one.
    log(f"• usage reading: {row.get('outcome')} ({plan['reason']})")
    return True


def _usage_interval(state_dir: str, now, interval_min: int):
    """The window a skipped row covers — the same previous-reading → now span a real reading gets,
    so a skip is a legible hole in the series rather than an unlabelled one."""
    previous = usage_probe.last_row(state_dir)
    since = usage_probe.parse_iso((previous or {}).get("at"))
    nominal = max(1, interval_min or USAGE_INTERVAL_MIN_DEFAULT) * 60
    if since is None:
        return {"first_reading": True, "since": None, "until": now.isoformat(timespec="seconds"),
                "nominal_seconds": nominal}
    seconds = (now - since).total_seconds()
    return {"first_reading": False, "since": since.isoformat(timespec="seconds"),
            "until": now.isoformat(timespec="seconds"), "seconds": round(seconds, 1),
            "nominal_seconds": nominal, "since_outcome": (previous or {}).get("outcome"),
            "gap": seconds > 1.75 * nominal}


def maybe_usage_notice(state_dir: str, args, log, now=None, send=None, quiet=None) -> bool:
    """usage-telemetry-spec §8.3: *failure should speak — but nothing crazy.*

    When the plan-meter instrument has failed `usage_health.FAILURE_STREAK` readings in a row, tell
    the owner once. When it recovers, tell them once — **and only if the break was announced.** That caps an
    outage at exactly two messages, and there is no third: no ladder, no re-nag, no escalation.

    **THIS SPEAKS ABOUT THE INSTRUMENT AND NEVER ABOUT THE METER.** §8.2 is still open and its
    conservative branch still stands — the usage LEVEL never speaks. The decision covers the
    instrument, not the meter, and `usage_health` carries no percentage at all so the two cannot leak into each
    other. `maybe_usage_reading` above stays speechless on every path, which is why this is a
    separate function rather than a branch inside it.

    **It never pierces the quiet window.** A dead instrument at 03:00 is not a Critical reminder, so
    a notice due inside quiet hours is simply not sent; nothing is stamped, and the next reading asks
    again — it goes out when the window opens rather than being dropped.

    **Through the Mouth**, like every other unsolicited daemon nudge (`docs/mouth-spec.md` phase 1):
    it gets the assertion record and a `supersede_key`, and it waits on the queue instead of being
    logged-once-and-lost when Telegram is down. `send` is injected the same way `usage_probe`'s
    `runner` is, so the whole path runs in a test with nothing leaving the machine.

    Fail-open in the same shape as the reading: a health check that throws costs a log line."""
    if getattr(args, "no_usage_reading", False) or getattr(args, "no_usage_notice", False):
        return False
    if send is None and (getattr(args, "stub_send", False)
                         or not getattr(args, "telegram_env", None)):
        return False  # no surface to speak on; the rows are still being written either way
    now = now or local_now()
    path = os.path.join(state_dir, USAGE_STAMP_FILE)
    try:
        stamp = load_json(path, None)
        stamp = stamp if isinstance(stamp, dict) else {}
        decision = usage_health.decide(state_dir, stamp.get(usage_health.NOTICE_KEY), now,
                                       quiet=quiet)
    except Exception as e:  # noqa: BLE001 — a health check may never take the tick down
        log(f"! usage health check failed: {type(e).__name__}: {e}")
        return False
    if decision is None:
        return False
    if decision["deferred"]:
        log(f"• usage {decision['kind']} notice held by the quiet window")
        return False

    def _queue(text: str) -> bool:
        return mouth.enqueue(
            state_dir, surface="telegram", kind="nudge", text=text,
            # When the fact became true — the row that made it true, not this tick (mouth-spec §3.2).
            # A notice the quiet window held overnight then carries an honest as-of stamp instead of
            # reading as fresh news at 7 a.m.
            observed_at=usage_probe.parse_iso(decision.get("observed_at")) or now,
            supersede_key="usage-instrument-health") is not None

    try:
        landed = bool((send or _queue)(decision["text"]))
    except Exception as e:  # noqa: BLE001
        landed = False
        log(f"! usage {decision['kind']} notice raised on the way out: {type(e).__name__}: {e}")
    if not landed:
        # Do NOT stamp. Stamping on a message that never queued would retire the announcement on the
        # strength of something the owner never received — the same silent-success shape as the outage.
        log(f"! usage {decision['kind']} notice could not be queued; will retry next reading")
        return False
    if decision["notice"] is None:
        stamp.pop(usage_health.NOTICE_KEY, None)
    else:
        stamp[usage_health.NOTICE_KEY] = decision["notice"]
    save_json(path, stamp)
    # The outcome NAMES, never a number — `presence.log` is quoted to the owner and read by other tools.
    log(f"• usage {decision['kind']} notice sent")
    return True


# --------------------------------------------------------------------------- scheduled slot runs

def parse_hhmm(s: str) -> int:
    """'HH:MM' → minutes since local midnight."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# The `claude` CLI's own usage-limit turn error names a clock-time reset with NO date — e.g.
# "You've hit your session limit · resets 12:40am (<the account's zone>)". Deliberately distinct from usage_probe.py's `_RESET_WHEN_RE`,
# which parses the periodic usage-PROBE report's "Mon Day, H:MMam" shape — that reading always carries
# a month/day because it can be taken hours before the reset it names; a slot's own failure text never
# does, because a 5-hour session limit always resets within the next calendar day.
_SLOT_RESET_RE = re.compile(
    r"\bresets?\b\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[ap]\.?m\.?)?", re.IGNORECASE)


def parse_slot_reset_time(text: str, now_local: datetime) -> datetime | None:
    """Find a `claude` usage-limit reset clock-time in `text` (a failed slot's captured output) and
    return the next wall-clock instant it names relative to `now_local` — today if that time is still
    ahead, else tomorrow (a reset named "12:40am" by a run that failed at 22:05 means 00:40 the NEXT
    day). Returns None if no reset shape is found — including on empty/missing text — so a caller with
    nothing captured degrades straight to the backoff path rather than raising. Pure → unit-tested."""
    m = _SLOT_RESET_RE.search(text or "")
    if not m:
        return None
    hour = int(m.group("hour"))
    minute = int(m.group("minute"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    ampm = (m.group("ampm") or "").replace(".", "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate


def slot_backoff_seconds(attempt: int) -> int:
    """The exponential-backoff fallback's wait for a given failed-exit `attempt` count (1 = the first
    failure). `SLOT_BACKOFF_BASE_SEC * 2**(attempt-1)`, capped at `SLOT_BACKOFF_CAP_SEC` — see the
    module-level comment above `SLOT_MAX_RETRIES` for why the cap sits well inside `--slot-catchup-min`
    rather than at or beyond it. Pure → unit-tested."""
    return min(SLOT_BACKOFF_BASE_SEC * (2 ** max(attempt - 1, 0)), SLOT_BACKOFF_CAP_SEC)


def slot_log_path(state_dir: str, name: str) -> str:
    """Where a slot's most recent run's stdout+stderr is captured, so a failed exit's own text can be
    scanned for a usage-limit reset (`parse_slot_reset_time`). One file per slot NAME, truncated on
    every launch — only the latest attempt's output is ever relevant to that decision, and an unbounded
    per-run append would grow forever for a slot that fails the same way every day."""
    return os.path.join(state_dir, "slot-logs", f"{name}.log")


def _open_slot_log(state_dir: str, name: str):
    """Open (create/truncate) `name`'s slot log for its next launch. The caller passes the returned
    file object as BOTH `stdout` and `stderr` to `Popen`, then closes it immediately after spawning —
    the child holds its own duplicated handle once the process exists (the standard, documented
    `subprocess` pattern for a fire-and-forget child; the parent closing its copy does not touch a
    still-running child's handle). Deliberately NOT `stdout=subprocess.PIPE`: nothing here drains a pipe
    while the child runs, and a heavyweight slot (a 15-20 min Dream/journal run) can write far more than the
    OS pipe buffer holds — an undrained PIPE would eventually block the child's own write() and hang the
    run that was working fine before. A file has no such ceiling."""
    d = os.path.join(state_dir, "slot-logs")
    os.makedirs(d, exist_ok=True)
    return open(slot_log_path(state_dir, name), "w", encoding="utf-8", errors="replace")


def _read_slot_log_tail(state_dir: str, name: str, max_bytes: int = 8000) -> str:
    """Best-effort tail-read of a failed slot's captured output. Never raises — a missing file (nothing
    captured yet, or a slot that never got to write anything before dying) is read the same as a file
    with no reset text in it: `parse_slot_reset_time` returns None and the caller falls through to the
    backoff path."""
    try:
        with open(slot_log_path(state_dir, name), "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read()
    except OSError:
        return ""


def _brief_unknowns_context(state_dir: str) -> str:
    """The Brief's unassigned-work line, rendered by `owi_unknowns.brief_line` at launch — the number
    the run prints is computed here, by code, never by the model (`owi_unknowns.py`). When the count is
    0 the run is told so explicitly, so it omits the section rather than inventing a number."""
    line = owi_unknowns.brief_line(state_dir)
    if not line:
        return ("Unassigned-work count, computed by code at launch: 0 — omit the 📋 Unassigned work "
                "section entirely; do not run owi_unknowns.py count yourself or print any other number.")
    return (f"Unassigned-work line, computed by code at launch — print it VERBATIM as the 📋 Unassigned "
            f"work section, do not run owi_unknowns.py count yourself or print any other number: {line}")


def _brief_tomorrow_lead_context(state_dir: str) -> str:
    """The Brief's 🎯 Tomorrow's Lead section, rendered by `tomorrow_marker.brief_line` at launch
    (`../docs/tomorrow-marker-spec.md` §4.1) — computed here, by code, never by the model. Empty when
    nothing is marked for today, in which case the run is told so explicitly and omits the section,
    the same shape `_brief_unknowns_context` already uses for its own empty case."""
    line = tomorrow_marker.brief_line(state_dir)
    if not line:
        return ("Tomorrow's Lead, computed by code at launch: nothing marked — omit the 🎯 Tomorrow's "
                "Lead section entirely; do not run tomorrow_marker.py yourself.")
    return (f"Tomorrow's Lead section, computed by code at launch — print it VERBATIM, ABOVE \"Needs "
            f"You\" (it is the day's own stated plan, not a subset of what needs a decision): {line}")


def _brief_observation_gate_context(state_dir: str) -> str:
    """The Brief's lead-with line for a gate that just closed — it marks them as ready to continue on
    (`../docs/observation-gate-spec.md`). Computed here, by code, off
    `observation_gate.brief_line`, never by the model — the same reason `_brief_unknowns_context`
    and `_brief_tomorrow_lead_context` are code hooks and not prose the run is asked to remember."""
    line = observation_gate.brief_line(state_dir)
    if not line:
        return ("Observation-gate line, computed by code at launch: nothing ready — omit this "
                "section entirely; do not run observation_gate.py yourself.")
    return (f"Observation-gate line(s), computed by code at launch — print EACH one VERBATIM, "
            f"ABOVE Tomorrow's Lead and above \"Needs You\" (a gate that just closed is the day's "
            f"own leading news, not a subset of what needs a decision): {line}")


def _brief_context(state_dir: str) -> str:
    """The `morning-brief` slot's one `context` hook, combining the observation-gate lead-in
    (`../docs/observation-gate-spec.md`), the unassigned-work line, and Tomorrow's Lead
    (`../docs/tomorrow-marker-spec.md` §4.1) — a slot carries exactly one `context` entry, so every
    code-rendered Brief section launches through this single function. Each piece is guarded on its
    own: a raising helper costs only its own section, never the others', and `slot_context`'s own
    outer try/except is still the last line of defense for the Brief's launch as a whole."""
    parts = []
    for fn in (_brief_observation_gate_context, _brief_unknowns_context,
              _brief_tomorrow_lead_context):
        try:
            text = fn(state_dir)
        except Exception:  # noqa: BLE001 — one section's failure must not cost the other
            continue
        if text:
            parts.append(text)
    return "\n\n".join(parts)


#: name → callable(state_dir) -> str, appended to a slot's prompt at launch (`SLOTS[i]["context"]`).
SLOT_CONTEXT = {"brief_unknowns": _brief_unknowns_context, "brief_context": _brief_context}

#: name → callable(state_dir) -> None, run by `reap_finished_slots` on a CLEAN exit
#: (`SLOTS[i]["on_complete"]`). `brief_sent` is `owi_unknowns.note_brief_sent` — the ask-window
#: reset, fired by the daemon's own definition of "the Brief completed" rather than
#: by a sentence in `modes/brief.md` the run had to remember.
SLOT_ON_COMPLETE = {"brief_sent": lambda state_dir: owi_unknowns.note_brief_sent(state_dir)}


def slot_context(slot: dict, state_dir: str) -> str:
    """The extra prompt text a slot's `context` hook renders, or `""`. NEVER raises — a broken
    helper must not cost the Brief its launch; the run then simply has no code-side line."""
    fn = SLOT_CONTEXT.get(slot.get("context") or "")
    if fn is None:
        return ""
    try:
        return fn(state_dir) or ""
    except Exception:  # noqa: BLE001 — the Brief launches with or without this line
        return ""


def slot_on_complete(slot_name: str, state_dir: str, log) -> bool:
    """Run the slot's `on_complete` hook after a clean exit. True iff a hook ran without raising.
    NEVER raises — the stamp in `reap_finished_slots` must not depend on it."""
    slot = next((s for s in SLOTS if s.get("name") == slot_name), None)
    fn = SLOT_ON_COMPLETE.get((slot or {}).get("on_complete") or "")
    if fn is None:
        return False
    try:
        fn(state_dir)
        log(f"• slot '{slot_name}' on_complete '{slot['on_complete']}' ran")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"! slot '{slot_name}' on_complete '{slot['on_complete']}' failed: {e}")
        return False


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
                    warm_busy: bool = False, slot_children: dict | None = None,
                    slot_hold_until: dict | None = None) -> None:
    """Fire any due heavyweight slot runs (fire-and-forget `claude -p`), at most once per local day.

    Slots are serialized against the peek and each other via `children`: at most one headless run
    launches per loop, and none launches while a prior one is still in flight. This matters most on
    catch-up — several slots can come due at once when the machine wakes, and launching them all
    together would fire several store read-bursts in parallel and trip a rate limit. A deferred
    slot isn't stamped, so it simply retries on the next loop once the running one finishes.

    **A launched slot is NOT stamped done here.** Launch success only means the process *started* — a
    `claude -p` that starts then dies (a network storm, a rate-limited morning) would otherwise be marked
    done-for-the-day and never retried, silently skipping (e.g.) the whole morning nudge batch. Instead
    the launched proc is registered in `slot_children`; `reap_finished_slots` stamps it only when it
    exits **0**, and on a non-zero exit leaves it unstamped so the next loop relaunches it — bounded by
    the same catch-up window (`--slot-catchup-min`), after which `classify_slots` gives up and stamps it.

    **A slot on hold (`slot_hold_until`, set by `reap_finished_slots` on a failed exit) is skipped
    silently here**, exactly like a not-yet-due slot: the hold was already logged once, with its reason,
    when `reap_finished_slots` set it, so re-logging it every ~5s tick until it clears would just be
    noise. Once the hold has passed, the entry is popped and the slot is free to launch normally on this
    same tick.

    Slots are ALSO deferred while the warm chat session is mid-turn (`warm_busy`): a chat turn's store
    reads and a scheduled slot's read burst hit the same bucket (~3 req/s on Notion), so overlapping
    them 429s.
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
        if slot_hold_until is not None:
            hold = slot_hold_until.get(s["name"])
            if hold is not None:
                if now_local < hold:
                    continue  # still holding — reap_finished_slots already logged why; try the next slot
                slot_hold_until.pop(s["name"], None)  # hold elapsed — clear so it isn't re-checked stale
        if children is not None and heavy_run_in_flight(children):
            log(f"• slot '{s['name']}' deferred (another headless run in flight) — retries next loop")
            break  # leave it unstamped; a later loop picks it up once the running one finishes
        model = s.get("model") or args.slot_model or args.model
        # Hand the run its authoritative clock so nudge text never guesses the date (rule 5).
        prompt = (f"{s['prompt']} (Authoritative current local date/time: {stamp}, the owner's "
                  f"configured timezone — base every date on this, not a UTC clock.)")
        extra = slot_context(s, state_dir)
        if extra:
            prompt = f"{prompt} ({extra})"
        cmd = [args.claude_bin, "-p", prompt, "--permission-mode", args.permission_mode]
        cfgs = active_mcp_configs(args)
        if cfgs:
            cmd += ["--mcp-config", *cfgs]
        if model:
            cmd += ["--model", model]
        try:
            log_fh = _open_slot_log(state_dir, s["name"])
            try:
                proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=child_env(), creationflags=NO_WINDOW,
                                        stdout=log_fh, stderr=subprocess.STDOUT)
            finally:
                log_fh.close()  # the child holds its own duplicated handle now — see _open_slot_log
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
                        max_retries: int = SLOT_MAX_RETRIES, now_local: datetime | None = None,
                        on_failed=None, slot_hold_until: dict | None = None) -> None:
    """Stamp a slot done only once its `claude -p` run has actually **finished cleanly** (exit 0).

    Called at the top of each loop, before `maybe_run_slots` re-evaluates what's due. For every tracked
    slot child that has exited: exit 0 → stamp `slots.json[name] = today` (done for the day); non-zero →
    leave it unstamped and log loudly, so the next loop relaunches it (a failed morning run retries
    instead of silently vanishing). Two independent backstops keep a persistently-broken slot from
    relaunching forever: `--slot-catchup-min` (once the slot is that far past its time, `classify_slots`
    routes it to `too_late` and stamps it) and `max_retries` (after this many failed exits in a day, give
    up and stamp it — guards against a fast-failing run that would otherwise respawn every loop). Retry
    counts live in `slot_retries` (in-memory; a clean exit or a give-up clears the slot). Still-running
    children are left in place.

    **A failed-but-not-given-up exit also sets a WAIT before the relaunch, in `slot_hold_until[name]`** (a datetime; `maybe_run_slots` skips a held
    slot until it passes). The wait is chosen by why the run failed, read from its captured output
    (`_read_slot_log_tail` / `slot_log_path` — written by whichever launcher spawned this slot):
      - **Names a `claude` usage-limit reset** (`parse_slot_reset_time` finds one) → hold until that
        clock time. Preferred: it is the actual answer, not a guess.
      - **Everything else** → exponential backoff (`slot_backoff_seconds`), because the failure could be
        anything from a transient network blip to a real bug, and only the ladder-plus-catch-up-window
        combination (see the module comment above `SLOT_MAX_RETRIES`) bounds how long that guess is
        allowed to cost.
    A give-up (`max_retries` reached) or a clean exit clears any existing hold for that name, same as it
    clears `slot_retries`.

    `on_failed`, if given, is called with no arguments once per failed exit (retried or given-up
    alike) — `vitals.json`'s `slots_failed_today` counter, optional so every existing caller and test
    is unaffected by its absence."""
    if not slot_children:
        return
    if slot_retries is None:
        slot_retries = {}
    if slot_hold_until is None:
        slot_hold_until = {}
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
            slot_hold_until.pop(name, None)
            log(f"• slot '{name}' completed")
            slot_on_complete(name, state_dir, log)  # e.g. the Brief's note_brief_sent — never raises
            continue
        if on_failed is not None:
            try:
                on_failed()
            except Exception:  # noqa: BLE001 — a counter must never cost the retry/give-up logic below
                pass
        slot_retries[name] = slot_retries.get(name, 0) + 1
        if slot_retries[name] >= max_retries:
            _stamp_slot(state_dir, name, today)  # give up for the day so it can't respawn endlessly
            slot_retries.pop(name, None)
            slot_hold_until.pop(name, None)
            log(f"! slot '{name}' run failed (exit {rc}) {max_retries}x — giving up for today")
            continue
        reset_at = parse_slot_reset_time(_read_slot_log_tail(state_dir, name), now_local)
        if reset_at is not None:
            slot_hold_until[name] = reset_at
            log(f"! slot '{name}' run failed (exit {rc}) — usage-limit reset named — "
                f"holding slot '{name}' until {reset_at.strftime('%H:%M')} (limit reset) — "
                f"retry {slot_retries[name]}/{max_retries}")
        else:
            wait_sec = slot_backoff_seconds(slot_retries[name])
            slot_hold_until[name] = now_local + timedelta(seconds=wait_sec)
            log(f"! slot '{name}' run failed (exit {rc}) — "
                f"holding slot '{name}' for {wait_sec}s (backoff, attempt {slot_retries[name]}) — "
                f"retry {slot_retries[name]}/{max_retries}")


def maybe_seed_day(state_dir: str, args, log, children: list | None = None,
                   warm_busy: bool = False, slot_children: dict | None = None,
                   slot_hold_until: dict | None = None) -> None:
    """Spawn the once-per-local-day Reminders SEED run — the date-rollover trigger that retired the four
    fixed reminder slots. Fires on the first tick of each new owner-local date (guarded by
    ``slots.json[SEED_SLOT_NAME]``), NOT at a fixed HH:MM — so a machine asleep through midnight still
    seeds on wake, with no ``classify_slots`` catch-up cliff that could silently skip the daily reset.

    The seed reads the ⏰ tracker, runs the daily reset, and queues every due row's exact-time nudges for
    the day (``reminders_seed.py``); the ~5 s delivery tick then fires each at its due minute. Reuses the
    slot lifecycle: the run is registered in ``slot_children`` under ``SEED_SLOT_NAME`` and
    ``reap_finished_slots`` stamps it only on a clean exit (retrying a crashed seed, giving up after
    ``SLOT_MAX_RETRIES`` — and holding for a reset-time/backoff wait between retries exactly like a
    scheduled slot; ``slot_hold_until`` here is the same dict `maybe_run_slots` uses). Held while the
    warm chat session is mid-turn or another headless run is in flight — its store read-burst must not
    overlap — exactly like ``maybe_run_slots``; a deferred seed stays unstamped and retries next loop.

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
    if slot_hold_until is not None:
        hold = slot_hold_until.get(SEED_SLOT_NAME)
        if hold is not None:
            if now_local < hold:
                return  # still holding — reap_finished_slots already logged why
            slot_hold_until.pop(SEED_SLOT_NAME, None)  # hold elapsed — clear so it isn't re-checked stale
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
        log_fh = _open_slot_log(state_dir, SEED_SLOT_NAME)
        try:
            proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=child_env(), creationflags=NO_WINDOW,
                                    stdout=log_fh, stderr=subprocess.STDOUT)
        finally:
            log_fh.close()  # the child holds its own duplicated handle now — see _open_slot_log
        if children is not None:
            children.append(proc)
        if slot_children is not None:
            slot_children[SEED_SLOT_NAME] = proc  # reaped on exit — stamped only if it finishes clean
        log(f"• seed '{SEED_SLOT_NAME}' launched (day rollover → {today})")
    except Exception as e:  # noqa: BLE001
        log(f"! seed '{SEED_SLOT_NAME}' launch failed: {e}")


# The suppression kinds worth a line in presence.log, and how to say each one. Deliberately NOT every
# signal `check_reminders` returns: a `reminder_fired` already leaves a Telegram message and a mouth
# assertion, and a `*_deferred_*`/`*_held` fires every ~5 s tick for as long as the hold lasts, which
# would bury the log. A SUPPRESSION is the one outcome that happens exactly once, silently, and
# destroys the nudge — so it is the one that has to leave a trace.
REMINDER_SUPPRESSION_LOG_KINDS = {
    "reminder_suppressed_curfew": "night curfew",
    "reminder_suppressed_stale": "too late",
}


def log_reminder_suppressions(signals, log) -> None:
    """Emit one presence.log line per nudge the fire path CONSUMED without delivering.

    Why this exists: `check_reminders`' return value was discarded at the call site, so the fire path
    wrote **nothing** to presence.log when it fired or suppressed anything. Reconstructing an
    overnight incident therefore meant diffing `fired_at` stamps in reminders.json against
    session-registry records — a forensic dig for a question ("what was the owner told, and what got
    eaten?") the daemon should simply be able to answer by being read. Fail-open, like every other
    writer in this tree: a logging problem may never cost a reminder pass."""
    for s in signals or []:
        why = REMINDER_SUPPRESSION_LOG_KINDS.get(s.get("kind") if isinstance(s, dict) else None)
        if not why:
            continue
        try:
            extra = ""
            if s.get("late_sec") is not None:
                extra = f" ({int(s['late_sec']) // 60}m past due"
                if s.get("presence_held_sec"):
                    extra += f", {int(s['presence_held_sec']) // 60}m of it presence-held"
                extra += ")"
            log(f"⏰ suppressed [{why}]: {s.get('id')} — {str(s.get('text') or '')[:80]}{extra}")
        except Exception:  # noqa: BLE001
            pass


def record_topic_nudges(state_dir: str, signals, log) -> int:
    """Append every nudge that landed **in a private-chat topic** to that topic's continuity cache,
    as an `assistant` turn. Returns how many were recorded.

    **THIS EXISTS TO KEEP THE ACK ROUND-TRIP HONEST, AND IT IS NARROW ON PURPOSE.**
    Once the reminders move to their own thread, the `done` the owner types is enqueued with that
    thread's topic and a cold spawn is grounded on that thread's cache (`thread_tail`). Without this
    the cache would be **empty**, so a bare *"done"* would arrive with nothing above it — the
    assistant reading a one-word message with no idea what it answers. The nudge is the missing line, and the
    daemon is the only thing that knows both the wording and the thread.

    **A MAIN-CHAT NUDGE IS DELIBERATELY NOT RECORDED, AND THAT IS THE WHOLE OF THE NARROWNESS.**
    Nudges have never appeared in the main chat's cache; adding them there would change what every
    existing conversation is grounded on, which is a separate decision nobody has made. What this
    restores is **parity** — the topic gets the context the main chat already had — not an
    improvement to the main chat. So the gate is *did it land in a thread*, read off the signal.

    **WHAT THAT GATE SEES DEPENDS ON ROUTING, AND THIS FUNCTION DOES NOT.** With
    `telegram_topics.REMINDERS_PIERCING_TO_MAIN_CHAT` `False`, a 🚨 Critical / `Call Me` nudge is
    topic-routed like every other and **is** recorded here — the outcome we want, because a bare
    *"done"* typed under an urgent nudge is exactly the message that most needs the line above it.
    What still records nothing is a nudge that actually LANDED in the main chat, which then means the
    fail-open rungs: topics off, a `getMe` that failed, a creation that failed, a
    stale id Telegram refused. The gate cannot be fooled by one of those, because the thread is read
    off the send result rather than off the purpose that was asked for.

    Fail-open like every other writer in this directory: a cache that cannot be written costs the
    continuity line and never the nudge, which has already been delivered by the time this runs."""
    recorded = 0
    for s in signals or []:
        if not isinstance(s, dict) or s.get("kind") != "reminder_fired":
            continue
        topic = s.get("message_thread_id")
        # `thread_key` is the same normaliser the inbound path uses, so *"which file does this
        # belong in?"* has one answer here and there — and anything that reads as the main chat is
        # skipped rather than written, per the paragraph above.
        if topic is None or thread_key(topic) == MAIN_THREAD_KEY:
            continue
        try:
            append_thread(state_dir, "assistant", f"⏰ Reminder: {s.get('text') or ''}", topic=topic)
            recorded += 1
        except Exception as e:  # noqa: BLE001 — a cache line may never cost anything
            log(f"! could not record the nudge for {s.get('id')} in thread {topic} ({e}); "
                f"the nudge WAS delivered")
    return recorded


def maybe_refill_rolls(state_dir: str, log, now_local: datetime | None = None) -> None:
    """Regenerate standing every-N-hours reminder rolls (reminders_roll.py) at most once per local
    day, stamped in ``rolls.json``. Pure-local queue math (no store, no spawn), so it runs regardless
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


def maybe_nudge_stale_dream_steps(state_dir: str, log, now_local: datetime | None = None) -> None:
    """Tell the owner, at most once per local day, when a Dream step has stopped happening.

    **This is the escalation a silently-deferred Dream step otherwise lacks.** A Dream that defers
    its expensive steps nightly and records that only in Run Log prose never carries the deferral
    forward, counts the repeats, or gets louder — so an index can go unwritten for weeks while the
    features that read it land inert. The ledger (`dream_steps.py`) makes the skip a row; this makes
    the row reach the owner.

    **Through the Mouth, not `send_telegram`.** Unsolicited speech is what the Mouth is for
    (`docs/mouth-spec.md` phase 1): it gets the staleness prefix, the assertion record, and — the
    part that matters here — a `supersede_key`, so a step stale for a week produces ONE current
    nudge rather than seven stacked ones. A nagging alarm is an alarm that gets muted.

    Pure-local: reads a JSON file, enqueues a row. No store, no spawn, no network on this path, so
    it runs regardless of the warm-session and in-flight gates the heavyweight slots wait on.

    **The wording is `dream_steps.nudge_text`'s, not this function's.** Deliberate `--skip` stamps
    (an operator refusing a step on purpose, reason on file) age a step past its window exactly as
    designed, and a generic *"Dream steps have stopped running"* would send the owner hunting a crash.
    The ledger knows it was a policy refusal, so the headline says which kind of silence it is and
    carries the operator's reason, and the age still climbs."""
    now_local = now_local or local_now()
    today = now_local.strftime("%Y-%m-%d")
    try:
        # Start the clock the first time anyone looks. Without this, a deleted or corrupted ledger
        # has no birthday, so no never-run step is ever overdue and the alarm is silent forever —
        # the grace window quietly recreating the failure it was added to avoid.
        dream_steps.ensure_ledger(state_dir)
        if dream_steps.last_nudged(state_dir) == today:
            return
        bad = dream_steps.stale(state_dir)
        if not bad:
            return
        # `observed_at` is when the step actually went stale, NOT now — mouth-spec §3.2. Passing
        # `now` here would make every one of these read as fresh news, which for a fact that has
        # been true for weeks is the specific lie the staleness prefix exists to prevent.
        #
        # It must be a DATETIME: `mouth._stamp` raises on a str, `enqueue` catches that internally
        # and returns None, and the result is a nudge that silently never queues. Passing the
        # ledger's raw ISO string does exactly that — fail-open plumbing hides a caller's type error
        # perfectly, which is why `parse_stamp` is a named seam.
        observed = dream_steps.parse_stamp(bad[0].get("last_ok"))
        item = mouth.enqueue(
            state_dir,
            surface="telegram",
            kind="nudge",
            text=dream_steps.nudge_text(bad),
            observed_at=observed,
            supersede_key="dream-steps-stale",
        )
        if item is None:
            # Enqueue failed. Do NOT stamp the day — stamping here would suppress the alarm for
            # 24 h on the strength of a message that was never queued, which is the same
            # silent-success shape as the outage itself. Leaving it unstamped means the next tick
            # retries; the log line is what makes a persistent failure visible.
            log("! dream-step nudge could not be queued; will retry next tick")
            return
        dream_steps.mark_nudged(state_dir, today)
        log(f"dream steps stale, nudged: {dream_steps.summary_line(bad)}")
    except Exception as e:  # noqa: BLE001 — a bookkeeping alarm must never take the daemon down
        log(f"! dream-step staleness check failed: {e}")


# --------------------------------------------------------------------------- the outbox's owner
#
# **The outbox is Notion-only** (store/notion/mapping.md, "Outbox — durable act-low writes"): every
# helper in this section is reached only when `store_backend_active() == "notion"` (see
# `_tend_outbox`); filesystem backends write locally and atomically and never journal anything here.
#
# **Why the daemon owns the flush.** The write-behind outbox (docs/notion-write-behind-outbox-spec.md)
# journals an act-low Notion write locally and then flushes it — and a sentence in `modes/chat.md`
# (*"opportunistically drain any backlog at a natural point in a turn"*) is a warm-session instruction,
# not an owner. Left to that alone, a queue of reminder acks can sit un-landed for a day with nothing
# anywhere going to drain it: a 👍 on a reminder nudge goes to the local ledger and to this queue, and
# the store row is never touched.
#
# So the flush gets a task, on the same tick as every other "did the thing we started actually finish"
# duty. Two halves, deliberately unequal:
#
#   1. **The sweep** — pure local, no network, every tick that finds a backlog. `outbox.py resolve`
#      retires entries a newer ack has overtaken — often enough on its own to clear the queue, because
#      the rows were already acked by hand for a LATER date.
#   2. **The flush** — a gated, fire-and-forget `claude -p` that replays what's left through the Notion
#      MCP config this daemon already threads into every spawn. The staleness gate answers "wasteful
#      when the queue is usually empty": an empty or fresh queue never spawns anything at all. It costs
#      no new credential, no REST client and no second property-mapping to drift from the first.
#
# Rate limits: the replay is `notion-update-page` **by page id** — the one Notion write path that routes
# around the collection router (store/notion/mapping.md) — with no query fan-out, and it serializes
# against the peek and the slots through the same `children` gate, so at most one headless Notion
# child runs at a time.

# Nothing is spawned until the oldest un-landed entry has had this long to be flushed by an ordinary
# turn. Option (a) is still preferred and still first: a warm session that drains as designed means
# this never fires.
OUTBOX_STALE_SEC = 20 * 60
# `notion-write-behind-outbox-spec.md` §9 (a‴): `session_is_live` is a DELAY, not a veto — a live
# session still gets first refusal, but past this SECOND, LONGER bound the daemon drains anyway, live
# session or not. 3x the plain stale bound — long enough that an ordinary turn's own drain (a few
# minutes, typically) has every chance to win, short enough that it resolves well inside the ~3 h
# nudge below rather than making that last resort the only path.
OUTBOX_LIVE_SESSION_STALE_SEC = 60 * 60
# Cadence ceiling for the spawn. A queue that keeps failing must not become a `claude` fork bomb.
OUTBOX_DRAIN_INTERVAL_SEC = 30 * 60
# When to stop trying quietly and TELL THE OWNER — without it a backlog grows in silence because nothing
# is watching it. (a‴) keeps this the last resort it was always meant to be.
OUTBOX_NUDGE_AGE_SEC = 3 * 60 * 60
OUTBOX_DRAIN_LIMIT = 25
# How often the tick actually opens the store. A queue depth does not change between 5-second ticks, and
# the tick's job is to notice a backlog measured in hours.
OUTBOX_CHECK_SEC = 60.0

OUTBOX_DRAIN_PROMPT = (
    "Drain the assistant's Notion write-behind outbox. This is a mechanical flush, not a conversation "
    "— no persona, no chat surface, no message to the owner. Steps, exactly:\n"
    "1. Run `python seneschal/scripts/outbox.py pull --json --limit {limit}`. It prints `resolved` (entries "
    "a newer ack already superseded — already handled, do nothing with them) and `claimed` (the work).\n"
    "2. For each claimed entry replay `payload` against `target_id` with the matching Notion MCP write "
    "and NOTHING else: `ack_reminder`/`reminder_status` -> `notion-update-page` on that page id "
    "(Status, Last Acknowledged, Consecutive Misses, untick Ack); a row-create op (`med_log`) -> "
    "`notion-create-pages` with `parent: {{\"data_source_id\": target_id}}` — **`target_id` for a "
    "create is a Notion DATA-SOURCE id (the collection://… URL), never a database_id; passing it as "
    "`database_id` 404s every time, wrong object type**; `run_log_finalize` -> "
    "`notion-update-page`; `task_status` -> `notion-update-page` on that page id, setting `Status` to "
    "`payload.status` (and `Completed` to `payload.completed_date` when present — only carried for "
    "`status: \"Done\"`). **Never** run a search or a data-source query to 'find' the row — the id "
    "in `target_id` IS the row, and the query path is the one that gets rate-limited.\n"
    "3. Mark each result: `python seneschal/scripts/outbox.py mark --id <id> --done "
    "[--notion-page-id <created row id>]` on success; `--retry --error \"<msg>\"` on a 429 or a "
    "transient error; `--dead-letter --error \"<msg>\" --status-code <code>` on any other 4xx or 5xx "
    "you cannot retry past. **Always pass the real HTTP status in `--status-code`** — you cannot "
    "verify whether a 4xx means the target is genuinely gone versus this call sent the wrong "
    "id/shape (step 2's never-query rule means you have no way to check), so `--dead-letter` "
    "classifies 400/404 as a likely CALLER bug rather than assuming Notion's side is unfixable; "
    "omitting `--status-code` when you have one just makes that entry harder to triage later. Every "
    "claimed entry gets exactly one mark — an unmarked entry sits `inflight` until it is reclaimed.\n"
    "4. Print a one-line summary and stop. Do not drain twice, do not fix anything else."
)


def outbox_backlog(state_dir: str) -> dict:
    """Sweep superseded entries, then report the queue: ``{"pending", "inflight", "dead", "oldest_sec"}``.

    The sweep rides along because it is pure-local and because a backlog figure that counts entries
    which can never legitimately be written is a figure that will nag about work nobody should do.

    Total: any failure reads as an empty queue. The outbox's own doctrine is fail-*closed* (an entry
    retries until Notion confirms) and that is untouched — nothing here ever drops an entry. It is only
    the *observer* that fails open, in the same shape as every other tick duty: a broken store costs
    this reading, never the daemon.

    **`read_failed: True` is what tells a broken store apart from a genuinely quiet one** — the bare
    `{"pending": 0, "dead": 0}` this function used to return on a raise reads exactly like a healthy
    queue, which is the worst available failure mode for the one signal that would say the outbox
    stopped draining. `failures.record` never raises, so it costs only the row."""
    try:
        conn = ob.connect(state_dir)
    except Exception as e:  # noqa: BLE001 — a broken store must never break the scheduler tick
        failures.record(state_dir, "presence.outbox_backlog", "outbox_connect_failed", detail=str(e))
        return {"pending": 0, "inflight": 0, "dead": 0, "oldest_sec": None, "resolved": 0,
                "read_failed": True}
    try:
        try:
            ledger = ra.load_acks(state_dir)
        except Exception:  # noqa: BLE001 — fail-open: a missing ledger vetoes nothing, blocks nothing
            ledger = {}
        resolved = ob.resolve_superseded(conn, ledger)
        s = ob.stats(conn)
        return {"pending": s["counts"][ob.PENDING], "inflight": s["counts"][ob.INFLIGHT],
                "dead": s["counts"][ob.FAILED], "oldest_sec": s["oldest_pending_age_sec"],
                "resolved": len(resolved),
                "dead_letters": [{"id": d["id"], "op": d["op"], "target_id": d["target_id"],
                                  "last_error": d.get("last_error")} for d in s["dead_letters"]]}
    except Exception as e:  # noqa: BLE001
        failures.record(state_dir, "presence.outbox_backlog", "outbox_stats_failed", detail=str(e))
        return {"pending": 0, "inflight": 0, "dead": 0, "oldest_sec": None, "resolved": 0,
                "read_failed": True}
    finally:
        conn.close()


def outbox_drain_due(backlog: dict, last_drain, now: datetime,
                     stale_sec: float = OUTBOX_STALE_SEC, session_live: bool = False,
                     live_stale_sec: float = OUTBOX_LIVE_SESSION_STALE_SEC,
                     interval_sec: float = OUTBOX_DRAIN_INTERVAL_SEC) -> bool:
    """Should a flush be spawned right now? Pure, so the gate is unit-testable without a subprocess.

    Both conditions, and both matter: the oldest un-landed entry has been waiting longer than an
    ordinary turn would have taken (`stale_sec`) — which is what keeps option (a) first — **and** we
    haven't spawned one inside `interval_sec`, which is what keeps a wedged queue from spawning a
    `claude` every five seconds forever.

    `session_live` swaps in the SECOND, LONGER bound (`live_stale_sec`) rather than refusing outright
    (§9 (a‴)). A live `/assistant` session still gets first refusal — it is
    given `live_stale_sec` to finish the write itself before the daemon steps in — but it is a DELAY,
    not a veto: past that bound the daemon drains regardless of session state, because the session's
    stand-down is automatic every tick while its pickup is discretionary, and leaning on the second
    forever is how an ack sits for half a day waiting on a session that never drains it."""
    age = backlog.get("oldest_sec")
    if not isinstance(age, (int, float)):
        return False
    threshold = live_stale_sec if session_live else stale_sec
    if age < threshold:
        return False
    if last_drain is None:
        return True
    try:
        return (now - parse_iso(last_drain)).total_seconds() >= interval_sec
    except (ValueError, TypeError):
        return True  # an unreadable stamp must not latch the drain off forever


def maybe_drain_outbox(state_dir: str, args, log, children: list | None = None,
                       warm_busy: bool = False, backlog: dict | None = None) -> bool:
    """Flush a stale outbox backlog through a headless `claude -p` (fire-and-forget). Returns whether
    one was launched.

    Gated on the same things the comms peek is — a warm turn mid-flight, another headless child —
    because this is another headless Notion writer and the point of those gates is that only one runs
    at a time. A skipped pass costs nothing: the entries are durable and the next tick re-evaluates.

    **A live `/assistant` session is a DELAY, not a veto** (§9 (a‴)). The session still gets first refusal — `outbox_drain_due` swaps
    in the longer `OUTBOX_LIVE_SESSION_STALE_SEC` bound while one is live — but past that bound the
    daemon drains anyway. **This does not need a new lock**: `outbox_common.claim_ready`'s atomic
    `UPDATE … WHERE status=PENDING` already makes two concurrent drainers safe (only one wins each
    row), so an overlap with an in-turn drain costs a wasted duplicate claim on rows the other drainer
    already took, never a double write — the veto was defending against a race the store already
    closes.

    Also gated on the store backend being **notion** (the outbox is Notion-only) and on its MCP actually
    being wired (`active_mcp_configs`): without it the spawned child has no way to write to Notion, so it
    would burn a turn and mark nothing."""
    if getattr(args, "no_outbox_drain", False) or args.stub_brain or args.fake_inbox is not None:
        return False
    if not (backlog or {}).get("pending"):
        return False
    if store_backend_active() != "notion":
        return False  # the outbox is Notion-only — never spawn a flush on another (or no) backend
    if warm_busy or (children is not None and heavy_run_in_flight(children)):
        return False
    session_live = session_is_live(state_dir, datetime.now(timezone.utc))
    if not active_mcp_configs(args):
        return False
    stamp_file = os.path.join(state_dir, "last-outbox-drain")
    now = datetime.now(timezone.utc)
    live_stale_min = getattr(args, "outbox_live_stale_min", OUTBOX_LIVE_SESSION_STALE_SEC // 60)
    if not outbox_drain_due(backlog, load_json(stamp_file, None), now,
                            stale_sec=args.outbox_stale_min * 60, session_live=session_live,
                            live_stale_sec=live_stale_min * 60,
                            interval_sec=args.outbox_drain_interval_min * 60):
        return False
    # Stamped BEFORE the spawn, exactly like the peek: a launch that fails must still burn its slot,
    # or a store that errors on every open would spawn a child every tick.
    save_json(stamp_file, now.isoformat().replace("+00:00", "Z"))
    try:
        cmd = [args.claude_bin, "-p",
               OUTBOX_DRAIN_PROMPT.format(limit=OUTBOX_DRAIN_LIMIT),
               "--permission-mode", args.permission_mode, "--mcp-config", *active_mcp_configs(args)]
        if args.watch_model:  # the cheap model — this is a replay of decided writes, not a judgment
            cmd += ["--model", args.watch_model]
        proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=child_env(), creationflags=NO_WINDOW)
        if children is not None:
            children.append(proc)
        log(f"• outbox drain launched ({backlog['pending']} pending, oldest "
            f"{(backlog.get('oldest_sec') or 0) / 60:.0f} min)")
        return True
    except Exception as e:  # noqa: BLE001 — a failed drain launch must never take the daemon down
        log(f"! outbox drain launch failed: {e}")
        return False


# A dead-letter is permanent (it has stopped retrying), not a depth reading — so the nudge names it
# instead of folding it into a bare count. Capped so a burst of dead-letters can't blow up one Telegram
# message; the rest fold into a `+N more` tail, mirroring `merge_guard.py`'s `citation_reserve` posture.
DEAD_LETTER_NUDGE_MAX = 5


def _dead_letter_lines(dead_letters: list) -> list:
    """One short, named line per dead-letter — `` `op` -> `id8` (error)`` — so the alarm identifies the
    row and the op instead of only a bare count — otherwise the only place that names the op and the
    row is `outbox.py status`, which nobody is looking at."""
    lines = [f"`{d.get('op')}` -> `{(d.get('id') or '')[:8]}`"
             + (f" ({d['last_error']})" if d.get("last_error") else "")
             for d in dead_letters[:DEAD_LETTER_NUDGE_MAX]]
    extra = len(dead_letters) - len(lines)
    if extra > 0:
        lines.append(f"(+{extra} more — `outbox.py status` for the rest)")
    return lines


def maybe_nudge_outbox_backlog(state_dir: str, log, backlog: dict | None = None,
                               now_local: datetime | None = None) -> None:
    """Tell the owner, at most once per local day, when the outbox has stopped draining.

    **This is the sensor the spec designed.** §7 calls for a Telegram push on an entry stuck past a
    threshold, and §9 makes the whole (a)→(b) escalation conditional on "if the pending-depth metric
    ever shows real lag" — without a watcher, the gate that is supposed to escalate has nothing watching
    it and a backlog grows unremarked.

    Through the Mouth with a `supersede_key`, for the same reason `maybe_nudge_stale_dream_steps` is: a
    backlog stuck for a week must produce one current nudge, not seven stacked ones. A nagging alarm is
    an alarm that gets muted. `observed_at` is when the oldest entry was actually enqueued, not now, so
    a day-old backlog reads as a day old (mouth-spec §3.2).

    A dead-letter is nudged on sight regardless of age — it has *stopped* retrying, so waiting for it to
    get older is waiting for nothing to happen. **Each one gets its own named line**
    (`_dead_letter_lines`) — an aggregate count ("1 dead-letter") says *that* something needs attention
    and nothing about *what*."""
    backlog = backlog or {}
    now_local = now_local or local_now()
    today = now_local.strftime("%Y-%m-%d")
    age = backlog.get("oldest_sec")
    stale = isinstance(age, (int, float)) and age >= OUTBOX_NUDGE_AGE_SEC
    if not stale and not backlog.get("dead"):
        return
    try:
        stamp_file = os.path.join(state_dir, "outbox-nudged.json")
        if load_json(stamp_file, None) == today:
            return
        bits = []
        if backlog.get("pending") or backlog.get("inflight"):
            bits.append(f"{backlog.get('pending', 0) + backlog.get('inflight', 0)} un-landed"
                        + (f", oldest {age / 3600:.1f} h" if isinstance(age, (int, float)) else ""))
        if backlog.get("dead"):
            bits.append(f"{backlog['dead']} dead-letter")
        text = ("Notion writes are backing up in the outbox: " + " · ".join(bits) +
                ". `python seneschal/scripts/outbox.py status` for the detail.")
        dead_lines = _dead_letter_lines(backlog.get("dead_letters") or [])
        if dead_lines:
            text += "\n" + "\n".join(dead_lines)
        item = mouth.enqueue(
            state_dir, surface="telegram", kind="nudge", text=text,
            observed_at=(datetime.now(timezone.utc) - timedelta(seconds=age)
                         if isinstance(age, (int, float)) else None),
            supersede_key="outbox-backlog",
        )
        if item is None:
            # Do NOT stamp the day on a nudge that never queued — that would suppress the alarm for 24 h
            # on the strength of a message nobody got, which is the exact silent-success shape the alarm
            # exists to break. Unstamped means the next tick retries.
            log("! outbox backlog nudge could not be queued; will retry next tick")
            return
        save_json(stamp_file, today)
        log(f"outbox backlog nudged: {' · '.join(bits)}")
    except Exception as e:  # noqa: BLE001 — a bookkeeping alarm must never take the daemon down
        log(f"! outbox backlog check failed: {e}")


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
    a 👍 on yesterday's nudge would mark today Done — for a daily must-do (a medication, say), that is
    exactly the failure that must not happen. Anything that doesn't clear all three is observe-only: the
    warm session reads it and decides. Failing to auto-ack costs a little manual work; auto-acking
    wrongly costs the owner the thing the reminder protects."""
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
    reminder ack, and when it has already run, the line says **exactly how far it got**.

    **That last word is load-bearing.** A line reading *"I've already run the ack for you (⏰ row
    marked Done, re-nudges dropped); no need to repeat it"* would be false — `ack_reminder_by_reaction`
    runs `reminders_dequeue.py` and (Notion backend) `outbox.py ack`, and **neither writes to the
    store.** Worse than cosmetic: the claim is what makes the warm session stand down, so the one thing
    the outbox relies on — an LLM turn flushing it — would be actively instructed not to happen.

    So it says what happened (dequeued, journalled) and what is still owed (the row), and asks for it.
    The daemon's own drain is the backstop if this turn doesn't do it — but the turn is right here,
    holding the row id, and is by far the cheapest place for the write to land. On a filesystem
    backend `acked` is False (nothing was journalled), and the plain line leaves the whole
    `store-update` to the warm session."""
    emoji = m.get("emoji") or "?"
    intent = reaction_intent(m, ctx)
    sent = ((ctx or {}).get("sent") or {}).get(str(m.get("message_id"))) or {}
    quoted = _truncate(sent.get("text", ""))
    # Beyond the map's window, or sent before this feature existed.
    target = f'to: "{quoted}"' if quoted else "to an earlier message"
    line = f"[the owner reacted {emoji} (= {intent}) {target}"
    if acked:
        line += (" — I've dropped the re-nudges and journalled the ack to the outbox, but the ⏰ row's"
                 " store write has NOT happened yet: please write it through now (status, last"
                 " acknowledged, consecutive misses, reset the ack affordance) and mark the outbox"
                 " entry done.")
    return line + "]"


#: What the warm session is handed when a tap could not be resolved at all — the subprocess died, the
#: token is gone, the store is unreadable. **A tap must never be a silent no-op** (spec §6b): the owner
#: has pressed a button and is owed something, so the failure becomes a line the assistant can act on
#: in words.
CALLBACK_UNRESOLVED_LINE = ("[the owner tapped a button on one of my questions and I couldn't resolve "
                            "it — their app may still be showing it as pending; ask them what they "
                            "picked]")


def resolve_callback(args, m: dict, log) -> dict:
    """Apply one button tap by shelling out to `telegram_ask.py resolve` — the same
    thin-subprocess-wrapper shape `sentinel.send_telegram` / `poll_telegram` use, so the Bot API calls
    for a question's whole lifecycle (ask, answer the query, fold the choice back into the message)
    live in one module rather than half here and half there.

    Never raises: it returns `{"ok": False, ...}` and the caller degrades honestly. `--stub-send`
    refuses outright — that flag exists because a stub-brain run with a live `telegram.env` can
    message the owner for real, and `answerCallbackQuery` is just as much a real send as a message is."""
    if getattr(args, "stub_send", False):
        return {"ok": False, "error": "stub-send: a tap is not resolved offline"}
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "telegram_ask.py"),
           "--state-dir", args.state_dir, "--env-file", args.telegram_env, "resolve",
           "--data", m.get("data") or "", "--callback-id", str(m.get("callback_id") or "")]
    if m.get("message_id"):
        cmd += ["--message-id", str(m["message_id"])]
    if m.get("chat_id"):
        cmd += ["--chat-id", str(m["chat_id"])]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=60, creationflags=NO_WINDOW)
    except Exception as e:  # noqa: BLE001 — a failed tap must never take the daemon down
        return {"ok": False, "error": str(e)}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": proc.stderr.strip() or "telegram_ask.py produced no JSON"}


def _merge_approval_clause(args, res: dict, log) -> str:
    """**The one place a merge approval is ever written.** A `merge-approval` tap -> the record
    `merge_guard.py` reads, plus the clause telling the warm session it landed.

    `merge_guard`'s whole design rests on this being here rather than in a script an agent can call:
    an approval any agent can mint is the same trust with extra steps, and the failure being prevented
    is an agent deciding for itself. So `request` — agent-callable, because
    asking is act-low — sends the picker and writes nothing, and the record is minted only when a
    real `callback_query` from Telegram arrives here carrying the question's `meta`.

    Returns "" for every tap that is not an Approve on a merge question, so this is inert for the
    picker's ordinary use. **A failed write is NEVER silent** — the guard would go on refusing while
    the owner believed they had approved, which is the one outcome worse than the wall itself, so it
    becomes a line the assistant has to answer for (telegram-inbound-spec.md §6b's "a tap is never a silent no-op").
    Never raises: the tap's own answer has already been delivered and may not be lost to this."""
    try:
        meta = res.get("meta")
        if not isinstance(meta, dict) or meta.get("kind") != mg.APPROVAL_META_KIND:
            return ""
        pr, head_sha = meta.get("pr"), meta.get("head_sha")
        # The repository rides on the question's own `meta`: a PR number alone is not an identity,
        # and an approval filed under the number alone is one file for every repo on this disk.
        # Absent — a picker sent by an older version, still on the owner's phone — writes the legacy
        # path rather than refusing the tap.
        slug = meta.get("repo")
        where = f"{slug} " if slug else ""
        if list(res.get("selected") or []) != [meta.get("approve_index", 0)]:
            log(f"• merge approval for {where}PR #{pr} declined")
            return ""  # "Not now" — the answer line already says so; nothing to record.
        mg.record_approval(args.state_dir, pr, head_sha, res.get("question_id") or "", repo=slug)
        log(f"• merge approval recorded: {where}PR #{pr} at {str(head_sha)[:12]}")
        # The FULL head, not `[:12]`: this line is what the merging session copies into
        # `--match-head-commit`, and GitHub rejects a prefix — a short head spends the tap on a merge
        # that then fails at GitHub. The log line above keeps its 12.
        return (f"[merge approval recorded for {where}PR #{pr} at {head_sha} — "
                f"single-use, expires in {mg.APPROVAL_TTL_HOURS} h, and only this exact commit]")
    except Exception as e:  # noqa: BLE001 — a failed record may not cost the tap its answer
        log(f"! merge approval NOT recorded: {e}")
        return ("[the owner approved that merge but I could not write the approval record, so the "
                f"merge guard will still refuse: {e} — tell them, and don't merge around it]")


def _owi_unknowns_clause(args, res: dict, log) -> str:
    """The unknown-owner grid's continuation (`owi_unknowns.py`): batches of 5, answered until the
    owner stops responding or says stop. A Done tap on an owi-unknowns grid picker gets applied through
    `owi_unknowns.py on-answer` — one subprocess doing both the write AND (unless the owner has
    stopped, or the pool is empty) sending
    the next batch, so the daemon's own log has exactly one call to reason about rather than two that
    could race.

    Returns "" for every tap that is not a Done on an owi-unknowns grid (inert for the picker's
    ordinary use), the same shape `_merge_approval_clause` uses so the two can be chained with `or`.
    Never raises: the answer is already on disk (`telegram_ask.resolve` wrote it before this runs),
    so a failure here costs the continuation, never the record of what was picked.

    A batch may also carry Archived/Done taps; `on-answer`'s `mirror` names the store rows those items
    `renders_elsewhere` to, and it is appended here verbatim so the assistant can tell the owner what
    to flip in the store themselves — the register write already happened in the subprocess, the
    store flip stays the owner's."""
    try:
        meta = res.get("meta")
        if not isinstance(meta, dict) or meta.get("kind") != owi_unknowns.ASK_META_KIND:
            return ""
        answers = res.get("grid_answers") or {}
        cmd = [sys.executable, os.path.join(SCRIPT_DIR, "owi_unknowns.py"),
               "--state-dir", args.state_dir, "on-answer",
               "--resolution", json.dumps(answers, ensure_ascii=False),
               "--env-file", args.telegram_env]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=60, creationflags=NO_WINDOW)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001 — the assignments are already recorded; the continuation may not cost them
        log(f"! owi-unknowns continuation failed: {e}")
        return (f"[the owner's assignments are recorded, but I couldn't check for the next batch of "
                f"unknowns: {e}]")
    if not out.get("ok"):
        log(f"! owi-unknowns on-answer refused: {out.get('error')}")
        return f"[the owner's assignments may not all have been recorded: {out.get('error')}]"
    log(f"• owi-unknowns batch applied — {len(answers)} assignment(s), {out.get('remaining')} left")
    mirror = out.get("mirror") or ""
    tail = f" — {mirror}" if mirror else ""
    if out.get("next_sent"):
        return (f"[recorded {len(answers)} owner assignment(s); {out.get('remaining')} unknown(s) "
                f"left — sent the next batch of 5{tail}]")
    return f"[recorded {len(answers)} owner assignment(s); no more unknowns queued right now{tail}]"


def _tomorrow_wrap_clause(args, res: dict, log) -> str:
    """A Done tap on the Tomorrow's Lead Close/Roll/Drop grid (`../docs/tomorrow-marker-spec.md`
    §4.2) — `tomorrow_marker.apply_wrap_grid` writes each row's resolution to `state/tomorrow.json`
    directly, no subprocess needed (a lightweight stdlib store, unlike `owi_unknowns`'s own
    continuation which also has to decide whether to send a next batch). A linked item's Close comes
    back as a `proposal` string — an ASK-HIGH suggestion to flip the linked Task/Reminder `Done`,
    relayed here verbatim and never written automatically.

    Returns "" for every tap that is not a Done on this grid, the same shape every sibling clause in
    this chain uses so `_callback_line` can chain them with `or`. Never raises: the taps are already
    on disk (`telegram_ask.resolve` wrote them before this runs)."""
    try:
        meta = res.get("meta")
        if not isinstance(meta, dict) or meta.get("kind") != tomorrow_marker.ASK_META_KIND_WRAP:
            return ""
        answers = res.get("grid_answers") or {}
        out = tomorrow_marker.apply_wrap_grid(args.state_dir, answers)
    except Exception as e:  # noqa: BLE001 — the taps are recorded; the report line may not cost them
        log(f"! tomorrow-marker wrap grid apply failed: {e}")
        return f"[the owner's Tomorrow's Lead taps may not all have been recorded: {e}]"
    n_ok = sum(1 for r in out["results"].values() if r.get("ok"))
    log(f"• tomorrow-marker wrap grid applied — {n_ok}/{len(answers)} item(s)")
    tail = f" — {'; '.join(out['proposals'])}" if out["proposals"] else ""
    return f"[recorded {n_ok} Tomorrow's Lead resolution(s){tail}]"


def _tomorrow_lead_clause(args, res: dict, log) -> str:
    """A Done tap on the "what's tomorrow's lead?" multi-select (`../docs/tomorrow-marker-spec.md`
    §2.3) — `tomorrow_marker.apply_lead_answer` marks every candidate the owner selected,
    `source="wrap_picker"`. This door only ever PROMOTES candidates the Wrap already gathered
    (`meta["candidates"]`, stamped when the picker was sent); it never accepts freeform text (§6.6 —
    Telegram's inline keyboard has none to accept).

    Same "" / never-raises contract as `_tomorrow_wrap_clause` and every sibling clause here."""
    try:
        meta = res.get("meta")
        if not isinstance(meta, dict) or meta.get("kind") != tomorrow_marker.ASK_META_KIND_LEAD:
            return ""
        selected = list(res.get("selected") or [])
        out = tomorrow_marker.apply_lead_answer(args.state_dir, meta, selected)
    except Exception as e:  # noqa: BLE001
        log(f"! tomorrow-marker lead apply failed: {e}")
        return f"[the owner's tomorrow's-lead picks may not all have been recorded: {e}]"
    n = len(out["marked"])
    log(f"• tomorrow-marker lead applied — {n} item(s) marked")
    return f"[marked {n} item(s) to lead tomorrow]" if n else "[nothing marked to lead tomorrow]"


def _callback_line(args, m: dict, log) -> str:
    """The inbound line for one tap, or "" when there is nothing new for the warm session to read.

    "" is the **toggle** case and the **re-tap of an answered question** case — both of which already
    gave the owner a popup from `telegram_ask.py`, so neither is silent from their side. It is
    deliberately not the failure case: a tap that resolved to nothing at all still reaches them, above.

    A tap may also carry an **effect** — see `_merge_approval_clause` / `_owi_unknowns_clause` /
    `_tomorrow_wrap_clause` / `_tomorrow_lead_clause`. Effects run only on the `answered` path
    (`telegram_ask.resolve` returns `meta` nowhere else), so a toggle can never fire one and an
    answered question can never fire more than one — `meta.kind` selects at most one of the four."""
    res = resolve_callback(args, m, log)
    if not res.get("ok"):
        log(f"! callback resolve failed: {res.get('error')}")
        return CALLBACK_UNRESOLVED_LINE
    if res.get("answered"):
        log(f"• question {res.get('question_id')} answered: {', '.join(res.get('labels') or []) or 'nothing'}")
        # A duplicate picker for the same `(repo, pr, head_sha)` is settled inside `telegram_ask`
        # on this same tap (`docs/picker-state-marking-spec.md` §10.5 item 2). That happens in the
        # subprocess, so this is the only place the daemon's own log can say a pending question just
        # stopped being pending — and a settle nobody can see is how a wrong one survives.
        for tid in res.get("twins_settled") or []:
            log(f"• duplicate picker {tid} settled by that answer — the same PR at the same commit")
    line = res.get("line") or ""
    clause = ""
    if res.get("answered"):
        clause = (_merge_approval_clause(args, res, log) or _owi_unknowns_clause(args, res, log)
                 or _tomorrow_wrap_clause(args, res, log) or _tomorrow_lead_clause(args, res, log))
    return f"{line} {clause}".strip() if clause else line


def _human_size(n) -> str:
    """Bytes as a short human string for an attachment descriptor."""
    if not isinstance(n, (int, float)) or n <= 0:
        return "unknown size"
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _edit_line(m: dict) -> str:
    """What the warm session reads when an edit lands on a message that has ALREADY been answered.

    It cannot be un-answered, so the honest thing is to hand the correction over as its own inbound and
    say plainly what it is — otherwise the corrected text reads as the owner saying nearly the same
    thing twice, and it gets answered twice. Same bracketed-annotation shape as the reaction line, for
    the same reason: everything in brackets is the daemon describing, never the owner speaking.

    The *other* case — the edit arrives while the message is still queued and unanswered — never reaches
    here: `apply_inbound_edit` swaps the queued text in place and this line is never built, because
    there is nothing to correct yet from the assistant's side.

    **Deliberately not truncated**, unlike the reaction line's quote. There the quote is context for an
    emoji; here the quoted text IS the message, and clipping it would drop the very words the edit
    exists to deliver."""
    body = plain_inbound_line(m)
    if not body:
        return ""  # nothing survived (an edit down to nothing) — telegram_task drops the empty line
    return f'[the owner edited an earlier message to: "{body}"]'


def plain_inbound_line(m: dict) -> str:
    """The message as the warm session reads it when nothing needs annotating: the owner's text, or a file descriptor,
    with any swipe-reply prefix. Shared by the ordinary inbound path and by the in-place edit
    replacement — an edit that lands before the message was answered has to produce **exactly** the
    line the original produced, or the queue would hold a differently-shaped message than the one it
    is replacing."""
    line = _attachment_or_text(m)
    reply_to = (m.get("reply_to") or "").strip()
    # An empty line is dropped by telegram_task, so don't let a bare reply-prefix stand in for a message.
    if reply_to and line:
        return f'(replying to: "{reply_to}") {line}'
    return line


def telegram_inbound_text(m: dict, ctx: dict | None = None, acked: bool = False) -> str:
    """The line the warm session sees for one inbound message.

    Plain text passes through untouched. An attachment becomes a synthesized descriptor — where the file
    landed, or why it didn't — plus any caption, so a file the owner sends is something the assistant can
    actually open and answer. Before this, a message with no `text` reached the warm session as an empty
    string and was dropped on the floor: that's how an inbound export zip once looked ignored. A
    swipe-reply carries what the owner is replying to, so they never have to restate it. A reaction names
    its intent and quotes what they reacted to (`ctx` = reaction_context(), loaded once per batch). An
    **edit** is annotated as a correction —
    but only when it gets this far; see `apply_inbound_edit` for the case where it replaces a still-
    queued message instead.

    A **callback** (a question-picker tap) never arrives here: `_inbound_lines` resolves it through
    `telegram_ask.py` first, because it owes Telegram an `answerCallbackQuery` and needs the durable
    question store to say what was picked. One reaching this function anyway carries no text and is
    dropped by `telegram_task`'s empty-line filter — the fail-open default, not a second path.

    The daemon describes; it never decides. It runs no tool on an inbound file and writes no canned
    apology — the warm session reads the descriptor and responds in the assistant's own voice."""
    if m.get("kind") == "reaction":
        return _reaction_line(m, ctx, acked)
    if m.get("kind") == "edit":
        return _edit_line(m)
    return plain_inbound_line(m)


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
    elif att.get("refetchable") and att.get("file_id"):
        # **THE PLACEHOLDER NAMES THE RECOVERY, because the file is not actually gone.** The download
        # already retried (telegram_http.IDEMPOTENT) before it gave up, but a `file_id` stays valid on
        # Telegram's side long afterwards, so the honest description of this state is "not fetched
        # YET", not "lost" — read as lost, the warm session answers around a photo that is still
        # sitting there and the owner re-sends it by hand. Describing only — the daemon writes no
        # apology and runs nothing; the warm session decides whether to spend the re-fetch.
        body = (f"[attachment: {desc} — download failed ({att.get('error') or 'unknown error'}); "
                f"the file is still on Telegram and can be re-fetched with "
                f"`python telegram_poll.py --refetch {att['file_id']} --env-file telegram.env`]")
    else:
        body = f"[attachment: {desc} — download failed ({att.get('error') or 'unknown error'})]"
    # A caption rides with the file; `text` is empty on a media message, but keep it if both ever appear.
    trailer = " ".join(p for p in ((m.get("caption") or "").strip(), text) if p)
    return f"{body} {trailer}".strip()


# ------------------------------------------------------------------- albums: N updates, ONE turn
#
# THE DEFECT. Telegram has no "album" update. Nine screenshots sent from one tap of Send arrive as
# nine `message` updates sharing one `media_group_id`, caption on exactly one of them — so the
# daemon enqueued nine queue entries and the drainer, which answers `pending[0]` one entry per turn,
# spends separate turns on separate slices of one conversation — each answer confidently wrong about
# what a LATER image in the same album shows.
#
# THE CAUSE IS PLUMBING. A written reading rule — "multi-attachment = ONE message" — cannot bind,
# because the model is handed a slice and has no way to know a slice is what it has. A prose
# countermeasure against a plumbing defect keeps recurring; so the fix is a turn boundary, not a
# sentence.
#
# THE POLICY, and it has exactly two bounds:
#   * QUIET  — a group is held until no new member has arrived for ALBUM_QUIET_SEC (2 s). Telegram
#              ships album members back-to-back, usually inside one `getUpdates` batch.
#   * CAP    — and never past ALBUM_MAX_HOLD_SEC (15 s) from the FIRST member, whatever else happens.
#   * plus a free close: any ordinary message that is NOT a member of the open group ends it on the
#              spot, at zero added latency. Album members are contiguous, so something else arriving
#              after them means the album is done.
#
# **NEVER LOSE A MESSAGE, NEVER HOLD FOREVER, AND NEVER DELAY A MESSAGE THAT ISN'T IN AN ALBUM.**
# Everything below is arranged around those three, in that order:
#   * a message with no `media_group_id` is dispatched in the same cycle it was polled in — the
#     latency of a plain text message is byte-for-byte what it was;
#   * the hold is DURABLE (`state/telegram-album-hold.json`), because the poller has already
#     committed the offset by the time we see these and Telegram never re-sends an acked update. A
#     hard kill mid-album therefore costs nothing: the successor delivers what was held;
#   * every exit from the poll loop — stop, `--max-iterations`, a poll that FAILED — still runs the
#     due check, so nothing can extend a hold by going quiet or by breaking;
#   * anything uncertain DELIVERS. An unreadable hold file, a non-string group id, a member cap
#     exceeded: each of those resolves to "hand it over now", which is at worst the behaviour this
#     whole section replaces.

#: Quiet window: hand a group over once it has gone this long without a new member.
ALBUM_QUIET_SEC = 2.0
#: Hard ceiling from the FIRST member, whatever the quiet window says. A hold that outlives this is
#: a hold that has stopped being about an album.
ALBUM_MAX_HOLD_SEC = 15.0
#: While anything is held, the long poll shrinks to this so the due check actually gets to run —
#: the same lever `control_pending` already pulls, one notch tighter. Without it a 25 s long poll
#: would park the loop and the 2 s quiet window would be a 25 s one.
ALBUM_POLL_TIMEOUT_SEC = 1
#: Telegram caps a media group at 10. Anything past this is not an album we understand, so the group
#: is handed over rather than grown — bounded memory, and the overflow becomes its own turn.
ALBUM_MEMBER_CAP = 24
#: The durable hold. Gitignored like the rest of state/; documented in ../state/README.md.
ALBUM_HOLD_FILE = "telegram-album-hold.json"


def album_key(m: dict):
    """The album this inbound record belongs to, or `None` for anything that stands alone.

    **Only a `kind: "message"` participates.** An `edited_message` on an album member carries the
    same `media_group_id`, and letting it into the buffer would append a SECOND copy of a photo the
    owner sent once; a reaction or a button tap carries a `message_id` naming one of *the assistant's*
    messages,
    which is a different question entirely. All three go down the paths they already had."""
    if m.get("kind") != "message":
        return None
    gid = m.get("media_group_id")
    if not isinstance(gid, str):
        return None
    gid = gid.strip()
    return gid or None


def album_topic(members: list):
    """The private-chat topic an album arrived in — the first one any member names.

    Every member of one album is one send into one thread, so they agree by construction; taking the
    first present value rather than requiring unanimity means a poller that ever stops carrying the
    field on some members degrades to "answer in the thread it came from" instead of dropping to the
    main chat."""
    for m in members:
        tid = m.get("message_thread_id")
        if tid is not None:
            return tid
    return None


def album_inbound_text(members: list) -> str:
    """The ONE line the warm session reads for one album. Empty if nothing survived.

    Shaped so the turn cannot be mistaken for a slice: a header that says how many attachments
    arrived together, then one descriptor per member in send order, then the caption once.

    **THE CAPTION IS NOT ASSUMED TO BE ON THE FIRST MEMBER — IT IS SEARCHED FOR.** Telegram puts it
    on exactly one member of the group and which one is the sending client's business, not a
    documented guarantee; the same goes for the swipe-reply context. Each member's descriptor is
    built through the SAME `_attachment_or_text` every single photo already goes through, with the
    caption/text/reply keys held back so they cannot be repeated N times — one extraction path, so
    there is no second one to drift from it (the §1 argument the edit path already rests on)."""
    bodies = []
    for m in members:
        stripped = {k: v for k, v in m.items() if k not in ("caption", "text", "reply_to")}
        body = _attachment_or_text(stripped)
        if body:
            bodies.append(body)
    if not bodies:
        return ""
    caption = ""
    reply_to = ""
    for m in members:
        if not caption:
            caption = (m.get("caption") or "").strip() or (m.get("text") or "").strip()
        if not reply_to:
            reply_to = (m.get("reply_to") or "").strip()
    head = (f"[album: {len(bodies)} attachments sent together as ONE message — "
            f"read them as one, answer once]")
    if reply_to:
        head = f'(replying to: "{reply_to}") {head}'
    parts = [head] + bodies + ([caption] if caption else [])
    return "\n".join(parts)


def album_absorb(hold: dict, msgs: list, now: float) -> list:
    """Split one poll batch into the ordered units to dispatch NOW, moving album members into `hold`.

    Returns a list of ``("single", record)`` / ``("album", [members])`` in the order they should be
    queued. Pure apart from mutating `hold`: it reads no clock and touches no disk, so the whole
    policy is unit-testable against an injected `now`.

    Two things close an open group here, both free:
      * a `kind: "message"` that is not one of its members — album members are contiguous, so
        anything else arriving after them means the album finished. This is what keeps the common
        "nine photos, then a question" shape at ZERO added latency **and in the right order**: the
        album is queued ahead of the question that follows it, rather than being overtaken by it.
      * a member of a DIFFERENT group, same reasoning one step further along.

    An EDIT, a reaction and a button tap all close nothing, deliberately. Their arrival is not
    evidence that an album finished — an edit lands on some *earlier* message and a reaction names
    one of the assistant's — so closing on one could split an album mid-upload, which is the bug this exists
    to end. Not closing costs only the order of one annotation, and the quiet window is the backstop
    that does not need them.

    `[INFERRED]` contiguity is not spelled out in the Bot API; it follows from `sendMediaGroup`
    being one call and this being a 1:1 chat with one sender. If it is ever wrong the cost is that
    one album splits into two turns — which is today's behaviour, not a loss."""
    out = []
    for m in msgs:
        gid = album_key(m)
        if gid is None:
            if m.get("kind") == "message":
                out.extend(("album", g) for g in album_flush_all(hold))
            out.append(("single", m))
            continue
        if gid not in hold:
            out.extend(("album", g) for g in album_flush_all(hold))
            hold[gid] = {"members": [], "first": now, "last": now}
        rec = hold[gid]
        rec["members"].append(m)
        rec["last"] = now
        # Bounded rather than trusted. Telegram's own cap is 10; well past that we are no longer
        # looking at an album, and an unbounded in-memory buffer fed by the wire is not a thing to
        # own. Handing it over is the fail-open answer — the overflow becomes its own turn.
        if len(rec["members"]) >= ALBUM_MEMBER_CAP:
            out.append(("album", hold.pop(gid)["members"]))
    return out


def album_due(hold: dict, now: float) -> list:
    """The album member-lists whose hold has expired — quiet for ALBUM_QUIET_SEC, or open for
    ALBUM_MAX_HOLD_SEC. Popped from `hold` as they are returned.

    Called on EVERY cycle of the poll loop, including cycles where the poll returned nothing and
    cycles where it FAILED, which is what makes "never hold forever" true rather than intended: a
    quiet chat and a broken transport both still hand the album over on schedule. `time.monotonic`
    is the clock at every call site, so a wall-clock jump (DST, an NTP step) cannot stretch a hold.

    A record that is not shaped like a hold — a hand-edited state file, a future schema — is
    returned rather than kept, for the same fail-open reason everything else here delivers."""
    due = []
    for gid in list(hold):
        rec = hold.get(gid)
        if not isinstance(rec, dict) or not isinstance(rec.get("members"), list):
            hold.pop(gid, None)
            continue
        first, last = rec.get("first"), rec.get("last")
        if not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
            due.append(hold.pop(gid)["members"])
            continue
        if (now - last) >= ALBUM_QUIET_SEC or (now - first) >= ALBUM_MAX_HOLD_SEC:
            due.append(hold.pop(gid)["members"])
    return due


def album_flush_all(hold: dict) -> list:
    """Empty the hold, returning every member-list in insertion order. The unconditional door — used
    when something else closes a group, when the poll loop winds down, and on the recovery read at
    boot. Never asks whether the hold was ready: it exists so that no exit path can strand one."""
    out = []
    for gid in list(hold):
        rec = hold.pop(gid)
        members = rec.get("members") if isinstance(rec, dict) else None
        if members:
            out.append(members)
    return out


def album_hold_path(state_dir: str) -> str:
    return os.path.join(state_dir, ALBUM_HOLD_FILE)


def save_album_hold(state_dir: str, hold: dict) -> None:
    """Persist the hold. **The reason this is durable at all**: `telegram_poll.py` has already
    advanced the offset for these members, and Telegram never re-sends an acknowledged update — so a
    hold that lived only in memory would turn a hard kill into up to ten lost photos, where the
    behaviour it replaces lost none. `first`/`last` are monotonic and meaningless to another process,
    so they are deliberately NOT written: the successor delivers what it finds immediately rather
    than resuming somebody else's clock.

    Best-effort and never fatal — an unwritable state dir costs the durability, never the turn, and
    the in-memory hold still hands its members over on schedule."""
    try:
        if hold:
            save_json(album_hold_path(state_dir),
                      {"groups": [{"media_group_id": gid, "members": rec.get("members") or []}
                                  for gid, rec in hold.items()]})
        elif os.path.exists(album_hold_path(state_dir)):
            os.remove(album_hold_path(state_dir))
    except Exception:  # noqa: BLE001 — see the docstring; durability is a nicety, the turn is not
        pass


def load_album_hold(state_dir: str) -> list:
    """Read back what a previous run was holding and REMOVE the file, returning the member-lists.

    **Restart mid-album ⇒ deliver, immediately, as one turn.** Not "resume the hold": the daemon has
    no idea how much of the album it has, the wire offset says the rest may never come again, and a
    partial album answered as one turn is strictly better than the nine-turn behaviour this replaces.
    Members that arrive *after* the restart form their own group and their own turn — the same
    split the pre-restart daemon would have produced, and never a loss.

    The file is removed before the members are handed back so a crash in the dispatch that follows
    cannot re-deliver them on the next boot; a duplicate would be a message the owner never sent."""
    path = album_hold_path(state_dir)
    data = load_json(path, None)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    groups = (data or {}).get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return []
    out = []
    for g in groups:
        members = g.get("members") if isinstance(g, dict) else None
        if isinstance(members, list) and members:
            out.append([m for m in members if isinstance(m, dict)])
    return [m for m in out if m]


# --------------------------------------------------------------------------- reactive core (asyncio)

class DaemonState:
    """Everything the old single-threaded loop kept in locals, shared across the tasks. Mutated ONLY
    from coroutines on the event loop (never from inside to_thread callables) — that rule is what keeps
    this lock-free, exactly like the single-threaded loop it replaced."""

    def __init__(self):
        self.pending: list = []              # the durable action queue: (channel, text, attempts)
        self.pending_event = asyncio.Event() # pulsed on enqueue so the drainer wakes instantly
        self.inbound_ids: dict = {}          # telegram message_id -> the line it was enqueued as, so an
                                             # `edited_message` can find what it corrects. Bounded
                                             # (INBOUND_ID_CAP) and deliberately NOT persisted: the queue
                                             # survives a restart, this window doesn't, so an edit to a
                                             # message queued before a reload is handled as an
                                             # already-answered one. Losing the window costs an
                                             # annotation; a bad match would cost the message.
        self.album_hold: dict = {}           # media_group_id -> {members, first, last}: the album
                                             # members polled but deliberately NOT yet dispatched, so
                                             # nine photos become ONE turn. Insertion-ordered, bounded
                                             # (ALBUM_MEMBER_CAP per group), mirrored to
                                             # state/telegram-album-hold.json after every change —
                                             # the offset is already committed for these, so this one
                                             # is persisted where `inbound_ids` below is not.
        self.inflight_text: str | None = None  # the queue head the drainer is mid-turn on, or None.
                                             # Set where the head is claimed and reset at the top of
                                             # every drainer iteration — see replace_queued_inbound.
        self.stop = asyncio.Event()          # supervisor-wide wind-down signal
        self.session = None                  # the warm chat session (drainer-owned)
        self.session_busy = False            # True while a send() is in flight in a worker thread
        self.last_activity = time.monotonic()
        self.headless_children: list = []    # live fire-and-forget peek/slot procs (store-read serialization)
        self.slot_children: dict = {}        # name -> live slot proc (stamped only on clean exit)
        self.slot_retries: dict = {}         # name -> failed-exit count today
        self.slot_hold_until: dict = {}      # name -> datetime to hold the relaunch until (reset-time/backoff)
        self.control_pending = False         # a defer-until-idle control is waiting to apply
        self.control_held_since: float | None = None  # monotonic stamp of when that hold STARTED (P2,
                                              # docs/hung-turn-deadline-spec.md) — past
                                              # CONTROL_MAX_HOLD_SEC the control applies anyway, because
                                              # a deploy that never lands is worse than a turn cut short
        self.slot_drain_held_since: float | None = None  # monotonic stamp of when a pending control
                                              # started holding for an in-flight slot child ("drain,
                                              # don't kill") — past
                                              # SLOT_DRAIN_MAX_HOLD_SEC it applies anyway, killing the
                                              # slot. Independent of control_held_since above: this gate
                                              # is keyed on state.slot_children, not chat_idle().
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
        self.inflight_turn: dict | None = None  # phase 0: the OPEN turn-window record the interleave
                                              # gate measures against, or None. Opened where `turn_id`
                                              # is minted and closed when `send()` returns — a NARROWER
                                              # span than `inflight_text` above, deliberately: §4.3's
                                              # offsets are fractions of the model turn.
        self.interleave_turns: dict = {}      # turn_id -> that same record, kept briefly after close so
                                              # a verdict that lands LATE can still say how late
                                              # (INTERLEAVE_TURN_MEMORY). Never persisted.
        self.turn_scopes: dict = {}           # turn_id -> mouth.TurnScope, the derived scope of that
                                              # turn's look (docs/mouth-spec.md).
                                              # Registered by _make_stream_tee BEFORE the first event
                                              # and read once by the drainer at delivery. Bounded by
                                              # TURN_SCOPE_MEMORY; never persisted — a restart loses
                                              # the accumulator, which costs the field on any turn
                                              # still in flight and nothing else.
        self.reaction_acks: dict = {}         # queued TEXT -> the ⏰ row it relays an ack for
                                              # (docs/substance-or-silence-spec.md §6 option 2).
                                              # The drainer pops (channel, text, attempts) and has no
                                              # reminder id — `ackable_nudge` computes it three layers
                                              # away in `_inbound_lines` — so the id rides here rather
                                              # than widening the persisted queue tuple, which would
                                              # touch nine positional call sites and several hand-built
                                              # test doubles. Bounded (REACTION_ACK_CAP), keyed exactly
                                              # like `inbound_ids` and populated in the same place, and
                                              # deliberately NOT persisted: a lost entry means the turn
                                              # is RELAYED, which is today's behaviour.
        self.midturn_arrivals: list = []      # phase 1 (a′): queued TEXTS that landed while a turn was
                                              # already in flight. Keyed by text, consumed by the
                                              # prompt that answers them (take_arrival_marker); never
                                              # persisted, purely advisory, capped at
                                              # MIDTURN_ARRIVAL_CAP.
        self.archon_site_restart_requests: set = set()  # archon ids the cockpit's per-site Restart
                                              # button flagged (control_task's 'restart-site' dispatch);
                                              # drained by archon_sites_task next reconcile pass
        self.boot_monotonic = time.monotonic()  # ~boot instant, for the self crash-loop guard's
                                              # sustained-healthy self-heal (maybe_clear_crashloop_on_sustained)
        self.boot_at = local_now()            # the same instant as a WALL CLOCK. Only this process
                                              # knows when it came up, and a plan-meter reading needs
                                              # it to say whether a reload landed inside the interval
                                              # it is describing — a reload resets the warm session's
                                              # context, a dominant usage characteristic.
        self.crashloop_cleared = False        # latch: the guard's sentinel/window is cleared at most
                                              # once per run, after CRASHLOOP_SETTLE_SEC of uptime
        self.last_session_end: dict | None = None    # Step 2a: the ended session's resume record
                                              # ({id, model, context_tokens, ended_at, reason}) — the
                                              # in-RAM twin of presence-state.json's `last_session`,
                                              # which is what survives a Path A reload.
        self.last_respawn_reason: str | None = None  # why the PREVIOUS warm session ended — one of
                                              # RESPAWN_* below. Surfaced in the status snapshot because
                                              # "no warm session" is otherwise indistinguishable from
                                              # "the session keeps dying" ("silence is ambiguous").
        self.jobs_active: int = 0             # running background jobs, refreshed by the scheduler
                                              # tick's reconcile pass. CACHED deliberately: the status
                                              # snapshot is a hot path (every turn start/end + every
                                              # queue change) and must stay I/O-free, so the count is
                                              # computed once per ~5 s tick rather than per push. A
                                              # dashboard gauge does not need sub-tick freshness.
        self.outbox: dict = {}                # last outbox_backlog() reading — cache-fed for exactly
                                              # the same reason as jobs_active above, and refreshed on
                                              # its own slower cadence (OUTBOX_CHECK_SEC) because it
                                              # opens sqlite and a queue depth does not change between
                                              # 5-second ticks.
        self.outbox_checked_at: float = 0.0   # monotonic stamp of that reading (0 = never)
        self.vitals_written_at: float = 0.0   # monotonic stamp of the last vitals.json write (0 = never)
        self.vitals_day: str | None = None    # local YYYY-MM-DD the counters below belong to; rolled
                                              # over by _roll_vitals_day the first tick of a new day
        self.reminders_fired_today: int = 0   # vitals.json's counters — in-memory
                                              # only, like slot_retries above; a restart costs the
                                              # day's running total, not a durable record (that's
                                              # mouth.jsonl's job), which is why vitals.json is a
                                              # health snapshot and not a ledger
        self.reminders_failed_today: int = 0
        self.last_reminder_fired_at: str | None = None
        self.slots_failed_today: int = 0      # fed by reap_finished_slots's on_failed callback

    def warm_busy(self) -> bool:
        """Mid-conversation, for the peek/slot gates: a send in flight OR anything queued. Queued with
        no session up yet still counts — a turn is imminent (the drainer is about to spawn one, or is
        in its transient-failure backoff), and launching a headless store read-burst into that window
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
    frame ... carr[ies] the active warm model").

    The lifetime block (`session_age_sec` … `spawn_fallback_used`) is Step 1 of the warm-session
    lifetime work: the evidence needed to answer "should the warm session be
    long-lived?" empirically instead of by argument. Every field is None when there's no live session
    — EXCEPT `last_respawn_reason`, which is most useful precisely then. All of it is read off the
    session object (written by its own stdout-reader thread); a torn read costs one stale gauge
    reading, so this stays free of locks, like the rest of this snapshot."""
    model = getattr(state.session, "model", None)
    if not model:
        cfg = model_config.load(args.state_dir)
        model = model_config.canonical(cfg.get("warm_model") or "", backend=cfg.get("backend")) or args.model
    sess = state.session
    age = sess.age_sec() if sess is not None and hasattr(sess, "age_sec") else None
    ctx = getattr(sess, "context_tokens", None)
    return {
        "session_up": state.session is not None,
        "turn_in_flight": state.session_busy,
        "model": model,
        "queue_depth": len(state.pending),
        "session_age_sec": round(age, 1) if age is not None else None,
        "turns_served": getattr(sess, "turns_served", None),
        "context_tokens": ctx,
        "context_pct": context_pct(ctx),
        "context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "context_estimated": True,  # never let a consumer render this as if it were exact
        "session_cost_usd": getattr(sess, "session_cost_usd", None),
        "last_respawn_reason": state.last_respawn_reason,
        "spawn_fallback_used": bool(getattr(sess, "did_fallback", False)) if sess is not None else None,
        # Read off the cached field, never off disk — see DaemonState.jobs_active on why this one is
        # tick-refreshed rather than computed here.
        "jobs_active": state.jobs_active,
        # Same cache-fed rule. Un-landed Notion writes (the outbox is Notion-only; always 0 on a
        # filesystem backend) are the one backlog otherwise invisible from every surface at once, so
        # `!status` — which answers even when the warm session IS the problem — is where it belongs.
        "outbox_pending": (state.outbox or {}).get("pending", 0) + (state.outbox or {}).get("inflight", 0),
        "outbox_dead": (state.outbox or {}).get("dead", 0),
        "outbox_oldest_sec": (state.outbox or {}).get("oldest_sec"),
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


def _end_session(state: DaemonState, args, log, reason: str) -> None:
    """Record why the warm session ended, and everything `resume_decision` will need to judge whether
    the NEXT one may pick it up (Step 2a). Called at every teardown site, immediately before the
    session reference is dropped — the vitals have to be read off the live object while it still
    exists. Persisted, because the highest-value resume case is a Path A code reload, which is
    precisely the case where the daemon does not survive to remember anything in RAM.

    Fail-open: a bookkeeping error here must never obstruct a teardown, so the worst case is a cold
    start next time (which is exactly today's behaviour)."""
    state.last_respawn_reason = reason
    try:
        sess = state.session
        record = {
            "id": getattr(sess, "session_id", None),
            "model": getattr(sess, "model", None),
            "context_tokens": getattr(sess, "context_tokens", None),
            "turns_served": getattr(sess, "turns_served", None),
            "ended_at": time.time(),  # wall clock, not monotonic — it has to survive a restart
            "reason": reason,
        }
        state.last_session_end = record if record["id"] else None
        save_daemon_state(args.state_dir, state.pending,
                          record["id"] if reason in RESUMABLE_REASONS else None,
                          last_session=state.last_session_end)
    except Exception as e:  # noqa: BLE001
        log(f"! could not record session end: {e}")


def _resume_target(state: DaemonState, args, log) -> tuple:
    """Ask `resume_decision` whether this spawn should resume, using the persisted record (which is
    what survives a Path A reload) and Oikonomos's live `context_fill_winddown_pct`. Returns
    `(session_id_or_None, human_reason)`; the reason is logged either way, because "why did it
    re-ground?" is exactly the kind of silent decision session continuity exists to stop making.

    Fail-open to a cold start: every failure path here already IS today's behaviour."""
    try:
        record = state.last_session_end
        if not isinstance(record, dict):
            record = (load_daemon_state(args.state_dir) or {}).get("last_session")
        try:
            winddown = governor.load(args.state_dir).get("context_fill_winddown_pct")
        except Exception:  # noqa: BLE001 — a governor read must never block a spawn
            winddown = None
        sid, why, cls = resume_verdict(record, time.time(), winddown)
        # One row per spawn, so "how often does the gate refuse, and why?" is a query rather than a
        # grep over prose (docs/session-continuity-spec.md §6). Never raises; never blocks the spawn.
        record_session_start(args.state_dir, cls, why, record)
        return sid, why
    except Exception as e:  # noqa: BLE001
        log(f"! resume check failed ({e}) — starting cold")
        record_session_start(args.state_dir, "check_failed", f"resume check failed: {e}")
        return None, "resume check failed"


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


def _governor_meter_turn_usage(args, log, model: str | None, usage, turn_id: str | None = None,
                               levers: dict | None = None) -> None:
    """Oikonomos (order 15, advisor-chain.md): meter one turn's token spend into the governed ledger and
    self-push at most one Telegram line per knob per ALERT_REALERT_HOURS when a rail's alert-at-%
    crosses. The ledger is the only record of the assistant's own spend, and the alerts are the
    governor's advisory half (cockpit-spec.md) — both stay wired here.

    `usage` is whatever the claude-CLI's terminal `result` event supplied (cockpit_pipe.
    build_chat_event_from_stream forwards it verbatim, optional), handed to the governor WHOLE so it can
    record both bases: the raw flat sum as `tokens`, and the weighted `billable_tokens` the rails
    actually read.

    **This function used to sum those fields itself and claim that was the "real cost".** It is not — a
    cache read meters at 0.1x a fresh input token, and since the warm session re-reads its whole
    conversation every turn, the flat sum tracked conversation LENGTH rather than spend — enough to
    make a rail alert at several times the spend the plan's own usage panel reported. See
    `governor.TOKEN_WEIGHTS` and the correction in that module's docstring; don't reinstate the flat sum
    as the basis anything decides on.

    Note that the CLI's usage block is summed across the turn's ITERATIONS, and for spend that is
    correct — every iteration was a real billed call. (It is `context_tokens_from_usage`, the gauge, that
    has to divide it back out; the two consumers genuinely want different things from the same object.)

    `levers` is the turn's **cause** block (docs/spend-levers-spec.md phase 1) — tool calls, tool-result
    bytes and tool-result images, counted at the tee by spend_levers.TurnLevers and passed straight
    through. Report-only: nothing here reads it back, and the alert arithmetic below still decides on
    `billable_tokens` alone (Oikonomos owns enforcement, the spend-levers spec owns diagnosis).

    Fail-open throughout: a governor hiccup must never break a chat turn, so every step here is
    best-effort (same posture as the cockpit transcript tee just above)."""
    if not isinstance(usage, dict):
        return
    tokens = governor.raw_tokens(usage)
    if not tokens or tokens <= 0:
        return
    model_key = model or "unknown"
    try:
        governor.append_spend(args.state_dir, "tokens", model=model_key, usage=usage,
                              turn_id=turn_id, levers=levers)
        for alert in governor.due_alerts(args.state_dir, model_key):
            res = send_telegram(alert["text"], args.telegram_env)
            if res and res.get("ok"):
                governor.record_alert_sent(args.state_dir, alert["knob"])
                mouth.record_assertion(args.state_dir, surface="telegram", kind="alert",
                                       speaker="daemon", text=alert["text"])
    except Exception as e:  # noqa: BLE001 — fail-open: metering must never break a chat turn
        log(f"! governor turn-usage metering failed: {e}")


_metrics_lock = threading.Lock()  # metrics rows are appended from the warm session's stdout-reader
                                  # thread; same posture as cockpit_pipe's transcript lock.
METRICS_FILE = "metrics.jsonl"


def _append_turn_metrics(state: DaemonState, args, log, channel: str, turn_id: str, chat_ev: dict) -> None:
    """Append one row per completed warm turn to `state/metrics.jsonl` — the per-turn history needed to
    answer the long-lived-session question with data.

    **Why this lives in code.** A metrics log specified prompt-side only (references/advisor-chain.md,
    the Observability advisor) produces ZERO rows in practice, while the cockpit's Usage panel
    (cockpit/server/readers.py::read_usage) reads it every poll. A prompt-side observability contract
    is not an observability contract.

    The row shape is chosen to satisfy the CONSUMER as-is: `readers._extract_tokens` checks a top-level
    `tokens` first, and `read_usage` buckets on `ts` + `model` — so the Usage panel lights up with no
    reader change. `writer: "daemon"` marks these apart from advisor-written rows if those ever land.

    `num_turns` and the verbatim `usage` are kept deliberately: `context_tokens` is an estimate that is
    exact only when `num_turns == 1` (see context_tokens_from_usage), so a later analysis must be able
    to recompute rather than inherit this function's arithmetic.

    Fail-open throughout — a metrics write must never cost a delivered reply."""
    try:
        sess = state.session
        usage = chat_ev.get("usage")
        tokens = 0
        if isinstance(usage, dict):
            for key in ("input_tokens", "output_tokens",
                        "cache_creation_input_tokens", "cache_read_input_tokens"):
                val = usage.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    tokens += int(val)
        row = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": "Chat",  # the warm session is Chat mode by construction (SKILL.md); headless
                             # Brief/Wrap/Dream runs are separate processes and don't come through here
            "model": chat_ev.get("model") or args.model,
            "source": channel,
            "turn_id": turn_id,
            "session_id": getattr(sess, "session_id", None),
            "turns_served": getattr(sess, "turns_served", None),
            "tokens": tokens or None,
            # Derived from THIS row's own usage + num_turns rather than read off the session, so the
            # row is self-consistent by construction and can't drift out of step with the session's
            # bookkeeping again (it did once — see the ordering comment in _read_until_result).
            "context_tokens": context_tokens_from_usage(usage, chat_ev.get("num_turns")),
            "num_turns": chat_ev.get("num_turns"),
            "duration_ms": chat_ev.get("duration_ms"),
            "cost_usd": getattr(sess, "last_turn_cost_usd", None),
            "session_cost_usd": getattr(sess, "session_cost_usd", None),
            "outcome": "Failed" if chat_ev.get("is_error") else "Success",
            "writer": "daemon",
            "usage": usage if isinstance(usage, dict) else None,
        }
        line = json.dumps({k: v for k, v in row.items() if v is not None}, ensure_ascii=False)
        with _metrics_lock:
            with open(os.path.join(args.state_dir, METRICS_FILE), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as e:  # noqa: BLE001 — fail-open, exactly like the governor metering beside it
        log(f"! turn-metrics append failed: {e}")


# How many finished turns' scopes stay in RAM. Only the turn being delivered is ever read, so this is
# slack for the retry/fallback paths that re-enter the drainer, not a window anything depends on.
TURN_SCOPE_MEMORY = 16


def turn_scope_snapshot(state: DaemonState, turn_id: str | None) -> dict | None:
    """The `scope` sub-object for `turn_id`, or None when this turn's look was not observed.

    **None is a real answer and must stay distinguishable from a measured zero** — `!status` spawns no
    model turn at all, and a reply delivered from a hand-built harness has no tee. The accumulator is
    registered before the first stream event, so a warm turn that called no tools snapshots
    `tool_calls: 0` rather than falling through to here ("did it look at nothing?" would be answered
    wrongly if those two collapsed).

    The accumulator is **not** reset the way `spend_levers` is at each `turn_done`, deliberately: the
    ledger writes one row per stream result, and this writes one row per *delivered message*. If the
    warm session's fallback ladder re-sent inside this turn, everything it looked at was looked at in
    service of the reply that landed, and the scope says so.

    Fail-open: anything unexpected costs the field."""
    try:
        scope = state.turn_scopes.get(turn_id) if turn_id else None
        return scope.snapshot() if scope is not None else None
    except Exception:  # noqa: BLE001 — observation may never cost the delivery it rides on
        return None


def _make_stream_tee(state: DaemonState, args, log, channel: str, turn_id: str):
    """Build the `on_event` callback handed to WarmSession.send() for one turn: converts each raw
    claude-CLI stream-json line into a digestible chat.event (dropping the uninteresting ones — see
    cockpit_pipe.build_chat_event_from_stream), stamps it with `turn_id` (the SAME id the turn_started
    event carries — see drainer_task), and tees it. Since the protocol itself carries no turn
    correlator, `turn_id` is what lets the cockpit chat pane group turn_started/assistant_output/
    tool_use/turn_done events into one turn without relying on the (also-true, but implicit) fact that
    chat turns are strictly serialized. Runs on the worker thread reading the session's stdout, so it
    uses the thread-safe tee sibling throughout — including the governor metering below, which does its
    own (blocking-but-off-the-event-loop) Telegram send on an alert.

    It is also the **counting site for the spend levers** (docs/spend-levers-spec.md phase 1). `levers`
    below is fed the RAW event, before the cockpit conversion — deliberately, because
    build_chat_event_from_stream drops `user` events entirely (a chat pane has no use for them) and
    `user` events are where tool RESULTS live. The most expensive thing in the window was passing
    through this very function, being parsed, and being discarded one line later. One accumulator per
    turn, because this closure is already per-turn.

    It is likewise the counting site for the turn's **scope** (docs/mouth-spec.md), and for the same
    reason spend levers are counted here: the raw event carries each tool call's
    input as a dict, while the cockpit conversion one line below keeps only a 200-character preview
    of it. Registered on the state map **now, before the first event**, so a turn that ends up calling
    no tools still records a measured `tool_calls: 0` instead of reading as unobserved."""
    levers = spend_levers.TurnLevers()
    scope = mouth.TurnScope()
    try:
        # Guarded, and not because DaemonState might lack the attribute — because this runs on the
        # critical path of answering the owner, one line before the send, and an instrumentation field
        # may never be the reason a turn does not happen.
        state.turn_scopes[turn_id] = scope
        while len(state.turn_scopes) > TURN_SCOPE_MEMORY:
            state.turn_scopes.pop(next(iter(state.turn_scopes)))
    except Exception:  # noqa: BLE001 — the field, never the turn
        pass

    def _on_event(ev: dict) -> None:
        levers.observe(ev)
        scope.observe(ev)
        # Phase 2, §3.8: track the tool call (if any) with no result yet, off the RAW event — a no-op
        # write to `state.inflight_turn` whether or not live mode (or interleave at all) is on, exactly
        # like `interleave_note_tool` below.
        interleave_track_tool_call(state, ev)
        model = getattr(state.session, "model", None) or args.model
        # docs/pluggable-backend-spec.md §1.6/§3.1: each backend feeds its OWN converter, into the SAME
        # chat.event shape — nothing below this line knows or cares which backend produced the raw
        # event. levers/scope above stay claude-cli-shaped (§1.11's own scope note); they read codex's
        # raw events as "nothing recognized" rather than crash, which is honest, not a regression.
        converter = (codex_cli.build_chat_event_from_stream if isinstance(state.session, CodexWarmSession)
                    else cockpit_pipe.build_chat_event_from_stream)
        chat_ev = converter(ev, source=channel, model=model)
        if chat_ev is not None:
            chat_ev["turn_id"] = turn_id
            # The in-flight summary's second half (interleave §4.2: "the tool names seen so far this
            # turn, which `_make_stream_tee` observes"). Nothing new is instrumented — the names were
            # already parsed one line above, for the cockpit, and were being discarded.
            for use in (chat_ev.get("tool_uses") or []):
                if isinstance(use, dict):
                    interleave_note_tool(state, use.get("name"))
            # The SESSION-level join key (docs/session-trace-spec.md §2.1a, phase 0). `turn_id` groups
            # events into a turn; nothing grouped turns into a session, so "show me everything that
            # happened in session X" was unanswerable at any cost — the same gap `spend-levers` phase 0
            # closed one level down. Omitted, never null, when the CLI hasn't reported an id yet (the
            # first frames of a spawn): a null would claim we measured "no session", which is a lie.
            sid = getattr(state.session, "session_id", None)
            if sid:
                chat_ev["session_id"] = sid
            _tee_chat_event_threadsafe(state, args, log, chat_ev)
            if chat_ev.get("kind") == "turn_done":
                # Both rows describe the SAME turn and are written back to back; `turn_id` is what
                # lets anyone say so afterwards (docs/spend-levers-spec.md §2.2, phase 0). It was
                # already in scope here — the metrics row has always carried it and the ledger row
                # never did, which is why the two files could only be joined by timestamp proximity.
                # flush(), not snapshot(): the first-turn fallback ladder (WarmSession.send) re-sends
                # after a failed resume, and each attempt ends in its own `result` event — so each
                # writes its own ledger row, and without the reset the second row would re-report the
                # first attempt's reads on top of its own.
                _governor_meter_turn_usage(args, log, model, chat_ev.get("usage"), turn_id,
                                           levers.flush())
                _append_turn_metrics(state, args, log, channel, turn_id, chat_ev)
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


async def _answer_status_command(state: DaemonState, args, log, channel: str) -> None:
    """Answer `!status` from the daemon's own snapshot — no warm session spawned, no turn spent, no
    model involved. This is the point: the moment you most need to ask "is it up?" is the moment the
    warm session is wedged, and a status command that needs the warm session to answer is useless
    then."""
    reply = render_status_reply(_status_snapshot(state, args))
    log(f"• !status answered locally on {channel} (no warm turn spent)")
    if channel == "cockpit":
        # deliver_reply is a deliberate no-op for cockpit turns — their "delivery" IS the transcript
        # stream (see deliver_reply). So synthesize the three events the chat pane groups into a turn,
        # or the command would silently do nothing there. Badged `local`: no model ran, and the pane's
        # per-turn model badge should say so rather than implying Opus answered.
        turn_id = uuid.uuid4().hex[:12]
        for ev in (
            cockpit_pipe.chat_event("turn_started", source=channel, model="local",
                                    text_preview="!status", turn_id=turn_id),
            cockpit_pipe.chat_event("assistant_output", source=channel, model="local",
                                    text=reply, turn_id=turn_id),
            cockpit_pipe.chat_event("turn_done", source=channel, model="local", is_error=False,
                                    reply_preview=reply[:400], turn_id=turn_id),
        ):
            _tee_chat_event(state, args, log, ev)
        return
    # `turn_origin="system"`: `!status` is intercepted here, before the drainer, so the owner's side
    # of it is never captured and it leaves no thread-tail entry either. Booking only the answer as
    # conversation would put half an exchange in the corpus and imply a question that was never
    # asked. `kind` is deliberately left alone: it
    # is the assertions log's field, and it means something different there.
    await asyncio.to_thread(functools.partial(deliver_reply, channel, reply, args, log,
                                              turn_origin="system"))


async def _enqueue_inbound(state: DaemonState, args, log, new_inbound: list, ids: list | None = None,
                           rids: list | None = None) -> None:
    """Take messages that are already consumed off the wire (offset committed) into the durable action
    queue. Force-route (!fable) detection, thread-append, + Router shadow all happen here, once per
    message — NOT in the drainer's retry path, or a message that fails delivery would be re-appended/
    re-classified/re-force-routed on every retry.

    `ids` is an optional list of Telegram `message_id`s parallel to `new_inbound` (the other channels
    have no such handle and pass nothing). It exists so `state.inbound_ids` can be keyed to the text as
    it lands in the queue — *after* the force-route transform this function applies — which is what
    lets a later `edited_message` find and replace the entry it corrects.

    `rids` is the same shape for ⏰ reminder ids (`docs/substance-or-silence-spec.md` §6) and is keyed
    at the same moment for the same reason: the drainer matches on the post-transform text."""
    if not new_inbound:
        return
    # NORMALIZED ONCE, AT THE ONE DOOR EVERY CHANNEL COMES THROUGH. Producers hand in whatever shape
    # they have — Discord, the cockpit and the wake line have no topic and pass 3-tuples — and from
    # here down every entry is `(channel, text, attempts, topic)`. Doing it here rather than at each
    # unpack site below is what keeps a comprehension from quietly dropping the fourth element: that
    # loses the thread with no error anywhere, and the reply just turns up in the main chat.
    new_inbound = [queue_item(i) for i in new_inbound]
    # Defensive rather than trusting: a mis-paired list would key the map to the wrong message, and a
    # wrong match is the one failure mode of the edit path that costs a message rather than an
    # annotation. Length disagreement ⇒ no ids at all, and edits degrade to the annotated form.
    ids = list(ids) if ids and len(ids) == len(new_inbound) else [None] * len(new_inbound)
    # Same defensive length check, same reason: a mis-paired list would attach a ⏰ row to the wrong
    # queue entry, and here that is the failure mode that costs a MESSAGE rather than an annotation —
    # a turn suppressed against some other row's landed write. Disagreement ⇒ no ids at all ⇒ every
    # entry relays, which is today's behaviour.
    rids = list(rids) if rids and len(rids) == len(new_inbound) else [None] * len(new_inbound)
    # `!status` is intercepted HERE, before anything is threaded, queued or persisted: it is a daemon
    # command, not a message for the assistant. Answering it locally means it costs no turn, needs no warm
    # session, and — critically — still works when the warm session is the thing that's broken. It is
    # deliberately NOT appended to the thread tail either: that tail is only 6 turns of continuity
    # (thread_tail), and a status check must never evict a real one.
    status_cmds = [(ch, t, a, tp) for ch, t, a, tp in new_inbound if is_status_command(t)]
    if status_cmds:
        keep = [(entry, mid, rid) for entry, mid, rid in zip(new_inbound, ids, rids)
                if not is_status_command(entry[1])]
        new_inbound = [entry for entry, _, _ in keep]
        ids = [mid for _, mid, _ in keep]
        rids = [rid for _, _, rid in keep]
        for ch, _text, _attempts, _topic in status_cmds:
            await _answer_status_command(state, args, log, ch)
        if not new_inbound:
            return
    # Force-route is a pure, synchronous text transform (a regex match) — cheap enough to run inline,
    # before persisting, so the directive is baked into the SAME text that gets threaded/queued/retried
    # (no persisted-queue schema change, survives a restart for free). Unlike the classifiers below, this
    # never touches Ollama, so it can't reintroduce the "persist first" latency concern that motivates
    # deferring classification until after the save.
    new_inbound = [(ch, apply_force_route(ch, t, log), a, tp) for ch, t, a, tp in new_inbound]
    # Phase 1, the arrival marker (spec §8(a′)). Read HERE, in the same synchronous run as the
    # transform above and before anything awaits, so the answer is "was a turn in flight at the moment
    # this came off the wire" rather than "…by the time the classifiers below finished". Every message
    # in the batch is marked: the second one in a batch arrived while the turn for the first had not
    # even started, which is the same defect one degree further along.
    if turn_in_flight(state):
        for _ch, t, _a, _tp in new_inbound:
            note_midturn_arrival(state.midturn_arrivals, t)
        log(f"• mid-turn arrival: {len(new_inbound)} message(s) landed while a turn was in flight")
    # Persist + wake the drainer FIRST: the wire offset is already committed, so until this save lands
    # a hard kill silently loses the batch. The (slow — seconds on a cold Ollama) shadow classification
    # happens after, and the drainer can already be mid-turn while it runs.
    for _ch, t, _a, tp in new_inbound:
        # The topic decides WHICH continuity cache, and `None` is the main chat's — the same
        # file Discord and the cockpit have always shared. `thread_key` makes that a lookup
        # rather than a branch, so there is no main-chat special case here to drift.
        append_thread(args.state_dir, "owner", t, topic=tp)
    for (_ch, t, _a, _tp), mid, rid in zip(new_inbound, ids, rids):
        remember_inbound_id(state.inbound_ids, mid, t)
        turn_suppression.remember(state.reaction_acks, t, rid)
    state.pending.extend(new_inbound)
    save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
    state.pending_event.set()
    _push_cockpit_status(state, args)  # queue depth changed — cheap, best-effort, event-loop call site
    # Mid-turn interleave phase 0 (docs/mid-turn-interleave-spec.md §4.3). The whole
    # batch is snapshotted HERE, synchronously, before a single classifier runs: `arrived_offset_sec`
    # is a statement about when the message landed, and running the gates first would charge the
    # second message in a batch for the first one's latency. `interleave_mode` is its OWN knob — the
    # gate is not the Router's shadow arm and must not switch off with it.
    #
    # `queue_index` (phase 2): `state.pending` was just extended with this whole batch, in order, so
    # the k-th new item's absolute index is its position counting back from the end — computed here,
    # once, rather than inside interleave_snapshot, which has no reason to know about batch geometry.
    batch_start = len(state.pending) - len(new_inbound)
    interleave_snaps = [interleave_snapshot(state, args, ch, t, queue_index=batch_start + i)
                        for i, (ch, t, _a, _tp) in enumerate(new_inbound)]
    if args.router_mode != "off":
        for ch, t, _a, _tp in new_inbound:
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
    for snap in interleave_snaps:
        if snap is not None:
            await interleave_observe(state, args, log, snap)


def apply_inbound_edit(state: DaemonState, args, log, m: dict, batch: list, ids: list) -> bool:
    """Absorb an `edited_message` into work that hasn't been answered yet. True when it was absorbed and
    the caller should queue nothing for it; False to let it through as an annotated new inbound.

    Which of the three outcomes you get is entirely about how far the ORIGINAL message got:

      * **Still in this very poll batch** — the owner typed, fixed it, and both updates arrived in the
        same poll. Rewritten before it is ever enqueued: one message was sent, so one message is seen.
      * **Queued and not yet in a turn** — rewritten in the durable queue (and in the continuity cache),
        then persisted. This is the case the feature exists for; the fix simply arrives corrected.
      * **Anything else** — already answered, being answered *right now*, or enqueued before a reload
        emptied the id window. False. It cannot be un-answered, so it goes to `_edit_line` and the
        warm session reads it as the correction it is.

    An edit whose text is **unchanged** is absorbed as a no-op rather than announced: Telegram emits an
    `edited_message` for things the owner did not do (a link preview attaching is the common one), and
    "edited that to exactly what it already said" would be pure noise.

    Deliberately synchronous — no `await` anywhere in it. The drainer runs on the same event loop, so a
    function that cannot yield cannot interleave with it, and the queue it rewrites can't move under it
    mid-decision."""
    mid = str(m.get("message_id") or "")
    if not mid:
        return False  # nothing to match on; treat it as a new inbound
    new_text = plain_inbound_line(m)
    if not new_text:
        return False  # edited down to nothing — the empty-line filter will drop it either way
    batch_ids = [str(x) if x not in (None, "") else "" for x in ids]
    if mid in batch_ids:
        # Still pre-transform at this point — `_enqueue_inbound` will force-route the whole batch on
        # its way in, this entry included, exactly as it would have done to the original.
        i = batch_ids.index(mid)
        channel, old_text, attempts, topic = queue_item(batch[i])
        if old_text != new_text:
            # The topic comes off the ENTRY being corrected, not off the edit: they agree (an edit
            # lands in the thread the original is in), and preferring the entry means a poller that
            # ever stops carrying the field on an `edited_message` degrades to no change rather than
            # to a message that silently moves conversations.
            batch[i] = (channel, new_text, attempts, topic)
            log("• edit folded into the message it corrects (same poll batch)")
        return True
    old_text = state.inbound_ids.get(mid)
    if old_text is None:
        return False
    # Past this point we are rewriting text that is ALREADY IN the queue, and the queue holds
    # post-transform text: `_enqueue_inbound` bakes `!fable` into the delegation directive once, at
    # enqueue. So the replacement has to be transformed the same way — otherwise a force-routed message
    # would come back out of its own edit as the raw `!fable …` line with the directive stripped off,
    # and an edit that changed nothing would read as a change.
    queued_text = apply_force_route("telegram", new_text, log)
    if old_text == queued_text:
        log("• edit changed nothing (link preview or a no-op edit) — ignored")
        return True
    if replace_queued_inbound(state.pending, old_text, queued_text, "telegram", state.inflight_text):
        state.inbound_ids[mid] = queued_text
        # Amended in the cache the ORIGINAL was appended to, which is the thread the edit arrived in
        # — the caches are per-topic now, so amending the main chat's file would leave the typo'd
        # line standing in the topic and add nothing anywhere. Fail-open either way: a miss costs the
        # amendment, and the corrected text still reaches the warm session as the message itself.
        amend_thread(args.state_dir, old_text, queued_text,
                     topic=m.get("message_thread_id"))
        save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
        log("• edit applied to a queued message in place — nothing new enqueued")
        return True
    return False


async def _inbound_lines(args, log, msgs: list, acks: list | None = None) -> list:
    """Turn a fetched batch into the lines the warm session will read, running Phase B's nudge-ack on
    the way through.

    `acks` is an optional out-list, filled parallel to the returned lines with the ⏰ row each line
    relays an ack for (`None` for everything else). Filled in place rather than returned as a second
    value — it keeps the return type unchanged for every existing caller, and this is the ONE place in the tree that knows
    both the reminder id and the text it became (`docs/substance-or-silence-spec.md` §6).

    The reaction config + sent-message map are read only when a reaction is actually in the batch, so
    the overwhelmingly common text-only poll touches no extra files. The ack itself is two short
    subprocesses, so it goes to a thread — inbound intake must never stall the loop. A **callback**
    (a button tap, §6b) goes to a thread for the same reason and more so: resolving it makes up to
    three Bot API calls, and it is the one inbound kind that owes Telegram a reply."""
    if acks is not None:
        acks[:] = [None] * len(msgs)
    if not any(m.get("kind") in ("reaction", "callback") for m in msgs):
        return [telegram_inbound_text(m) for m in msgs]
    # Only loaded when a REACTION is in the batch — a batch of pure taps needs neither the intent map
    # nor the sent-message map, and `ackable_nudge` reads a `None` ctx as "nothing is ackable".
    ctx = reaction_context(args.state_dir) if any(m.get("kind") == "reaction" for m in msgs) else None
    lines = []
    for i, m in enumerate(msgs):
        if m.get("kind") == "callback":
            lines.append(await asyncio.to_thread(_callback_line, args, m, log))
            continue
        acked = False
        reminder_id = ackable_nudge(m, ctx)
        if acks is not None and reminder_id:
            # Recorded whether or not the ack itself succeeded: a FAILED ack is precisely the case
            # that must still reach a turn, and the drain-time predicate is what decides that — off
            # the outbox, which a failed ack never reaches. Handing the id over is not a vote.
            acks[i] = reminder_id
        if reminder_id:
            acked = await asyncio.to_thread(ack_reminder_by_reaction, args.state_dir, reminder_id, log)
            log(f"reaction-ack {'journalled' if acked else 'not journalled'} for ⏰ {reminder_id}")
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


async def _dispatch_inbound(state: DaemonState, args, log, units: list) -> None:
    """Turn one cycle's dispatch units into durable queue entries, in the order given.

    A unit is ``("single", record)`` — one inbound message/edit/reaction/tap, exactly as before — or
    ``("album", [members])``, one album that has finished being held and becomes ONE entry, i.e. ONE
    turn (the drainer answers `pending[0]`, one entry per turn).

    Lifted out of `telegram_task` unchanged for the singles, so the ordinary path is the ordinary
    path: `_inbound_lines` still runs over exactly the non-album records and still fills `acks`
    parallel to them, and the edit/topic/id rules below are the same rules in the same order."""
    if not units:
        return
    singles = [m for kind, m in units if kind == "single"]
    # `ids` runs parallel to `new_inbound` so an edit arriving later can find the entry it
    # corrects; an edit that finds its target here is absorbed and adds nothing to the queue.
    acks: list = []
    lines = await _inbound_lines(args, log, singles, acks)
    single_iter = iter(zip(singles, lines, acks))
    new_inbound, ids, rids = [], [], []
    for kind, payload in units:
        if kind == "album":
            line = album_inbound_text(payload)
            if not line:
                continue
            new_inbound.append(("telegram", line, 0, album_topic(payload)))
            rids.append(None)
            # **AN ALBUM CONTRIBUTES NO EDIT HANDLE, DELIBERATELY.** An edit to the caption would
            # otherwise replace this entry — nine attachment descriptors and all — with the one
            # edited caption string, losing the images from the turn. With no id the edit falls
            # through to `_edit_line` and arrives as the annotated correction it is, beside an album
            # entry that is still intact. Costs an annotation; the alternative costs the photos.
            ids.append(None)
            continue
        m, line, rid = next(single_iter)
        if m.get("kind") == "edit" and apply_inbound_edit(state, args, log, m, new_inbound, ids):
            continue
        if not line:
            continue
        # The FOURTH element is the private-chat topic this arrived in, straight off the
        # poller's payload and `None` for the main chat (which is every message until the
        # bot's topics toggle is on). It rides the durable queue, so a reload — which
        # happens on every merge — answers in the right thread rather than dropping back to
        # the main chat. A reaction or a tap contributes none: its `message_thread_id` names
        # the thread one of the ASSISTANT's messages lives in, which is a different question.
        new_inbound.append(("telegram", line, 0,
                            m.get("message_thread_id")
                            if m.get("kind") in ("message", "edit") else None))
        rids.append(rid)
        # Only a message/edit contributes an id. A reaction's or a tap's `message_id` names
        # one of the ASSISTANT's messages, not one of the owner's, so keying the edit window to it would
        # point a later edit lookup at the wrong entry — and a wrong match is the one failure
        # mode of the edit path that costs a message rather than an annotation.
        ids.append(m.get("message_id") if m.get("kind") in ("message", "edit") else None)
    _maybe_note_owi_unknowns_ask(args, log, singles, new_inbound, ids, rids)
    await _enqueue_inbound(state, args, log, new_inbound, ids, rids)


def _maybe_note_owi_unknowns_ask(args, log, singles: list, new_inbound: list, ids: list,
                                 rids: list) -> None:
    """The unknown-owner ask (`owi_unknowns.py`): ask after the owner's first REAL message of each
    Brief cycle (the window resets when the daily Brief goes out; acks and taps don't count). Fires at
    most once per Brief cycle
    (`owi_unknowns.should_ask`/`mark_asked`) and, when it fires, appends ONE synthesized instruction
    line to this turn's inbound — the same "told, not asked-for" shape the reminder-ack expansion and
    the job-completion wake line already use, so the warm session reads it as daemon narration and
    decides in the assistant's own voice whether/how to raise it, never a picker (the picker is for
    the 5-at-a-time batches; this ask is a short spoken question).

    Best-effort and additive only: it never touches `new_inbound`'s existing entries and a failure
    here costs only the nudge, never the real turn those entries already carry."""
    if not any(owi_unknowns.is_real_message(m) for m in singles):
        return
    try:
        if not owi_unknowns.should_ask(args.state_dir):
            return
        n = owi_unknowns.count(args.state_dir)
        owi_unknowns.mark_asked(args.state_dir)
    except Exception as e:  # noqa: BLE001 — the nudge may never cost the real turn beside it
        log(f"! owi-unknowns first-real-message check failed: {e}")
        return
    log(f"• owi-unknowns first-real-message ask fired — {n} unknown(s) outstanding")
    new_inbound.append((
        "telegram",
        f"[{n} open-work item(s) still have no owner (whose_move: unknown). This is your ONE chance "
        f"this Brief cycle to offer — ask the owner ONCE, briefly, in your own voice, whether they want "
        f"to go through any of them now. A picker is NOT how you ask this; it's for the 5-at-a-time "
        f"batches once they say yes. If they say yes, run "
        f"`python scripts/owi_unknowns.py ask --env-file scripts/telegram.env` to send the first "
        f"batch. Anything else — no re-ask until the next Brief.]",
        0, None))
    ids.append(None)
    rids.append(None)


async def telegram_task(state: DaemonState, args, fake_queue: list, log) -> None:
    """Inbound Telegram: long-poll (blocking, in a worker thread), enqueue, repeat. The long-poll is
    the reason this is its own task — it no longer paces anything else. While a control is waiting to
    apply, the poll window drops to 2 s so a graceful reload lands promptly after the owner stops typing.

    **ALBUMS ARE HELD HERE AND NOWHERE ELSE**. The poller carries `media_group_id` and
    decides nothing; this loop buffers members in `state.album_hold` and hands each group over as one
    unit once it goes quiet or hits the cap — see the `albums:` block above for the policy and the
    three rails it is arranged around. Three properties of this function are load-bearing:

      * **the due check runs on EVERY cycle**, including a cycle whose poll returned nothing and a
        cycle whose poll FAILED, so neither silence nor a transport blip can extend a hold;
      * **the poll window shrinks while anything is held**, or a 25 s long poll would park the loop
        and make the 2 s quiet window a 25 s one. Nothing shrinks when nothing is held — a chat with
        no album in flight polls exactly as it did;
      * **every exit flushes.** Stop, `--max-iterations`, the loop simply ending: the wind-down at
        the bottom hands over whatever is still held rather than stranding it in memory."""
    inbox = os.path.join(args.state_dir, TELEGRAM_INBOX_DIR)
    # Recovery FIRST, before a single poll: whatever the previous run was still holding is delivered
    # now, as one turn each. See `load_album_hold` — this is the "restart mid-album" answer, and it
    # is deliver-what-we-have rather than resume-the-hold on purpose.
    recovered = await asyncio.to_thread(load_album_hold, args.state_dir)
    if recovered:
        log(f"• recovered {len(recovered)} held album(s) from a previous run — delivering now")
        await _dispatch_inbound(state, args, log, [("album", g) for g in recovered])
    while not state.stop.is_set():
        if args.fake_inbox is not None:
            res = next_messages(args, fake_queue)
        else:
            timeout = 2 if state.control_pending else args.poll_timeout
            if state.album_hold:
                timeout = min(timeout, ALBUM_POLL_TIMEOUT_SEC)
            res = await asyncio.to_thread(poll_telegram, args.telegram_env, args.state_dir, True,
                                          timeout, inbox)
        # Even if stop was set while we were parked on the wire, PROCESS the result first — the poll
        # already committed the offset for anything it fetched, so skipping here would lose messages.
        # Enqueued-but-unanswered items are persisted and the successor picks them up (invariant 1).
        held_before = bool(state.album_hold)
        if res.get("ok"):
            msgs = res.get("messages", [])
            await _maybe_backlog_ack(state, args, log, msgs)
            units = album_absorb(state.album_hold, msgs, time.monotonic())
        else:
            log(f"! telegram poll: {res.get('error')}")
            units = []
        units += [("album", g) for g in album_due(state.album_hold, time.monotonic())]
        # Mirrored to disk BEFORE the dispatch, and only when the hold actually moved: these members
        # are already acked to Telegram, so the window in which they exist nowhere but RAM is the one
        # thing worth closing. A no-op cycle (nothing held, nothing arrived) writes nothing at all.
        if held_before or state.album_hold:
            await asyncio.to_thread(save_album_hold, args.state_dir, state.album_hold)
        await _dispatch_inbound(state, args, log, units)
        state.iterations += 1
        if args.max_iterations and state.iterations >= args.max_iterations:
            state.stop.set()
            break
        if args.fake_inbox is not None:
            await _sleep_or_stop(state, 0.05)  # test mode doesn't block on the network — pace lightly
    # Wind-down: a graceful stop (every `reseneschald`, every merge) must not strand a half-held album in
    # memory. Nothing here waits on the quiet window — the loop is over, so "quiet" is permanent.
    leftover = album_flush_all(state.album_hold)
    if leftover:
        log(f"• winding down with {len(leftover)} album(s) held — delivering them now")
        await _dispatch_inbound(state, args, log, [("album", g) for g in leftover])
    await asyncio.to_thread(save_album_hold, args.state_dir, state.album_hold)


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
    lease_logged = False  # latch: log the "held open" line once per hold, not every 5 s tick
    while not state.stop.is_set():
        # Reset at the TOP, so every path out of the body below — the dead-letter `continue`, the
        # hung-turn `continue`, a delivered turn, an undelivered one — clears it without having to
        # remember to. While this is set, an inbound edit refuses to rewrite that queue entry
        # (replace_queued_inbound): the turn already has the old text in hand and pops by position.
        state.inflight_text = None
        if not state.pending:
            if state.session is not None:
                # Wind the warm session down once it's been quiet (sooner if a control is waiting).
                limit = min(idle_sec, CONTROL_PENDING_IDLE_SEC) if state.control_pending else idle_sec
                quiet = time.monotonic() - state.last_activity
                if quiet >= limit and not state.control_pending and jobs.lease_active(args.state_dir):
                    # A `--lease` job is running: hold the session open until it's done, so work that
                    # genuinely can't detach isn't killed mid-flight by the idle timer. Probed only
                    # here — at the moment we'd otherwise wind down — so it costs one small dir read
                    # per wind-down decision, not one per tick.
                    #
                    # `not state.control_pending` is load-bearing and comes FIRST: a pending graceful
                    # restart always wins over a lease. Otherwise a long job would silently block a
                    # Path-A deploy, and merging-is-deploying is the invariant the whole reload path
                    # rests on. `jobs.lease_active` is separately bounded by LEASE_MAX_SEC, so even
                    # with no control pending a wedged job can't pin the session forever.
                    if not lease_logged:
                        log("• warm session held open by a --lease job — idle wind-down deferred")
                        lease_logged = True
                    await _wait_pending_or_stop(state, 5.0)
                    continue
                lease_logged = False
                if quiet >= limit:
                    # Log what the session WAS before dropping it. presence.log is where every prior
                    # diagnosis in this repo actually came from, and a wind-down line that carries
                    # age/turns/context is free lifetime history — available even if the cockpit and
                    # metrics.jsonl were both somehow lost.
                    log(f"• winding down idle warm session ({_session_vitals(state.session)})")
                    _end_session(state, args, log, RESPAWN_IDLE)  # BEFORE close(): reads the live vitals
                    await asyncio.to_thread(state.session.close)
                    state.session = None
                    clear_session_heartbeat(args.state_dir)  # session no longer live — reminders resume
                    _push_cockpit_status(state, args)  # session_up flipped false — tell the cockpit
                    continue
                await _wait_pending_or_stop(state, min(5.0, max(0.1, limit - quiet)))
            else:
                await _wait_pending_or_stop(state, 5.0)
            continue

        # `topic` is the private-chat thread this arrived in, or None for the main chat. It
        # is read here with everything else and rides every outbound this turn makes — the
        # reply, the dead-letter notice, the hung-turn notice — so a conversation answers
        # where it was held. `queue_item` tolerates the 3-element entries a pre-topics
        # daemon persisted, which reload as main-chat messages because that is what they were.
        channel, text, attempts, topic = queue_item(state.pending[0])
        # Claimed: from here until the top of the next iteration this text is the drainer's, and an
        # edit that lands on it is handled as a correction to an answered message rather than rewriting
        # an entry whose old text is already in local variables above.
        state.inflight_text = text
        # Poison-pill guard: past the cap, a message keeps ending its turn without ever being
        # delivered (classically, one that kills the daemon mid-turn — the "reseneschald" self-kill).
        # Stop replaying it forever: drop it to a dead-letter and tell the owner it was set aside.
        if attempts >= MAX_TURN_ATTEMPTS:
            state.pending.pop(0)
            save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
            log(f"! dead-lettered a message after {attempts} undelivered turns: {text[:80]!r}")
            # The notice quotes the owner's message back, so a `!private` one has to carry its flag
            # here too — otherwise the content the prefix exists to keep out of the corpus lands in it
            # as part of the dead-letter reply. The quoted text they SEE is unchanged; only the
            # captured row is.
            await asyncio.to_thread(
                deliver_reply, channel,
                ("Heads up — I kept hitting a snag on this one and had to set it aside "
                 f"after {MAX_TURN_ATTEMPTS} tries, so I don't loop on it:\n\n{text[:300]}"),
                args, log, redacted=strip_private(text)[0], topic=topic)
            continue
        # THE THIRD TURN OUTCOME — popped, not spent, not delivered.
        # docs/substance-or-silence-spec.md §6: not every inbound line deserves a reply. Without this,
        # one turn can do all the work of three 👍s while the other two each spend a warm-session turn
        # announcing it had already been done — three replies in under a minute.
        #
        # ONLY a `_reaction_line` the daemon synthesized itself is eligible, and only when EVERY
        # store write that 👍 owes has already landed (`reminders_acks.reaction_ack_fully_landed`, off
        # the Notion outbox — so on a filesystem backend nothing ever qualifies and every reaction
        # relays). Anything else — a real message, a question, a job report, an unreadable outbox, a
        # lost side-map entry, a raise — relays, which is the default behaviour. **The harm here is a
        # suppressed message the owner needed, not an extra one they didn't**, so there is exactly one
        # path to silence and it needs an affirmative reading from a store that only records a row
        # after it has written to Notion.
        #
        # Placed AFTER the dead-letter guard and BEFORE the attempt counter, deliberately. After,
        # because a poison message must still reach its dead-letter however it is classified. Before,
        # because a suppressed turn is not an attempt: nothing was tried, so nothing is counted, and
        # MAX_TURN_ATTEMPTS keeps meaning what it means.
        reaction_row = turn_suppression.take(state.reaction_acks, text)
        if reaction_row:
            v = await asyncio.to_thread(turn_suppression.verdict, args.state_dir, reaction_row)
            if v.get("suppress"):
                state.pending.pop(0)
                save_daemon_state(args.state_dir, state.pending,
                                  getattr(state.session, "session_id", None))
                private_turn, spoken_text = strip_private(text)
                turn_id = uuid.uuid4().hex[:12]
                # The bookkeeping a delivered turn does, and what this one owes instead (§6's table).
                # `append_thread("owner", …)` already happened at enqueue and STAYS: an `owner` line
                # with no `assistant` reply is honest. `_capture_turn(speaker="owner")` must still fire
                # — `turns.jsonl` is the record of what the OWNER said, and a 👍 that produced no turn
                # still happened. Nothing is captured for the assistant and NOTHING reaches the Mouth:
                # the owner was told nothing, and a row in `assertions.jsonl` would be exactly the lie
                # that file exists to prevent. The arrival marker is consumed so it cannot attach to an unrelated later
                # turn. `interleave_open_turn`/`close_turn` are both skipped — no turn window opened.
                _capture_turn(args, log, surface=channel, speaker="owner", text=spoken_text,
                              turn_id=turn_id, redacted=private_turn,
                              session_id=getattr(state.session, "session_id", None))
                take_arrival_marker(state.midturn_arrivals, text)
                # Two frames rather than the one the spec's table names, and the reason is the
                # reader: `cockpit/web/src/chatEvents.ts` attaches any event that is not
                # `turn_started` to the NEWEST turn in its list, so a lone `turn_suppressed` frame
                # would graft itself onto somebody else's turn. A started/done pair is the protocol
                # as it exists, needs no web change, and renders the suppression as what it is — a
                # turn that produced no output, with the reason in `reply_preview`.
                note = f"(suppressed — {v.get('why')}; nothing new to say)"
                _tee_chat_event(state, args, log, cockpit_pipe.chat_event(
                    "turn_started", source=channel, model=None, text_preview=text[:200],
                    turn_id=turn_id, suppressed=True))
                _tee_chat_event(state, args, log, cockpit_pipe.chat_event(
                    "turn_done", source=channel, turn_id=turn_id, reply_preview=note,
                    suppressed=True))
                # THE AUDIT ROW. A suppression nobody can see is how a silent-drop defect ships, so
                # this is both a durable record (with the withheld line verbatim) and a log line —
                # two places, because the log is what a human is already reading when they doubt this.
                recorded = await asyncio.to_thread(
                    functools.partial(turn_suppression.record, args.state_dir, channel=channel,
                                      text=text, reminder_id=reaction_row, detail=v.get("detail"),
                                      turn_id=turn_id, redacted=private_turn))
                log(f"• turn suppressed for ⏰ {reaction_row} — every store write it owes already "
                    f"landed ({v.get('why')}); nothing new to say"
                    + ("" if recorded else " [!! the audit row did NOT land]"))
                _push_cockpit_status(state, args)  # queue depth changed
                continue
            log(f"• relaying the ack for ⏰ {reaction_row} — {v.get('why')}")
        # Count this turn UP FRONT and persist before the risky send, so a kill mid-turn (which
        # never reaches the delivery check below) still advances the counter across the restart.
        attempts += 1
        state.pending[0] = (channel, text, attempts, topic)
        save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
        # v3: best-effort attach one pending router fable-arm hint (if any arrived in time — see
        # fable_arm_classify / _enqueue_inbound) to THIS turn's prompt only. Deliberately a LOCAL var,
        # not written back to state.pending[0] — the persisted/retry text (and the delivery-failure
        # requeue comparison below) must stay the hint-free original, so a retry doesn't duplicate or
        # go stale on a hint meant for the first attempt.
        # `!private`: strip the prefix so the turn reads naturally to the warm session — no grounding
        # change needed to explain a stray token — and carry the flag down to the capture site below,
        # which writes a tombstone instead of the text. Applied HERE rather than at enqueue so the
        # persisted queue text stays exactly what the owner typed: a retry after a failed
        # delivery must not lose the marker, and the drainer is where both halves of the turn are known.
        private_turn, spoken_text = strip_private(text)
        prompt_text = f"{state.fable_hints.pop(0)}\n\n{spoken_text}" if state.fable_hints else spoken_text
        # Phase 1 (spec §8(a′)): one line, on the turns where it applies, saying this message was
        # written before the last reply landed. Same LOCAL-variable discipline as the fable hint
        # directly above — never written back to `state.pending[0]`, so the persisted/retry text and
        # the delivery-failure requeue comparison below stay exactly what the owner sent.
        arrival_marker = take_arrival_marker(state.midturn_arrivals, text)
        if arrival_marker:
            prompt_text = f"{arrival_marker}\n\n{prompt_text}"
            log("• arrival marker attached — this one was sent mid-answer")
        cold_prompt = None  # set only on a resumed spawn — what to re-send if the resume doesn't take
        if state.session is None:
            # Step 2a: resume the previous conversation instead of re-grounding, when the gate allows
            # (docs/session-continuity-spec.md). `cold_prompt` is threaded down to send() so
            # a stale id lands on a properly grounded cold session rather than a context-less one.
            resume_id, why = _resume_target(state, args, log)
            state.session = make_session()
            # Set on the session rather than passed through make_session(): resuming is the DRAINER's
            # policy call, not part of what a session factory is for — and `make_session` stays the
            # zero-arg factory every call site and test harness already builds.
            if resume_id:
                state.session.resume_session_id = resume_id
                state.session.resumed = True
            await asyncio.to_thread(state.session.start)
            # `read_first_grounding` is read HERE, at spawn, not cached at import: the standing-safety
            # store changes under a running daemon, and a daemon that has been up for days would otherwise ground
            # every new session on the block as it stood when the process started. It returns "" on
            # every failure — see its docstring; there is deliberately no try/except around it.
            #
            # message-routing-spec.md §8 fork 4: the current topic is stated to the model EVERY TURN,
            # not just at cold grounding — a stale wrong topic looks exactly like a correct one and
            # nothing would flag it. Telegram-only, like the declaration
            # instruction itself: Discord and the cockpit have no topics to be told about.
            topic_line = channel_declare.current_topic_line(args.state_dir, topic) \
                if channel == "telegram" else ""
            cold_prompt = GROUNDING.format(
                channel=channel.capitalize(), now=local_stamp(),
                read_first=read_first_grounding(args.state_dir),
                thread=thread_tail(args.state_dir, topic),
                channel_declare_instruction=(channel_declare.grounding_instruction()
                                             if channel == "telegram" else ""),
                current_topic_line=topic_line, msg=prompt_text)
            if resume_id:
                log(f"• resuming warm session {resume_id} — {why}")
                prompt = RESUME_PREAMBLE.format(now=local_stamp(), msg=prompt_text,
                                                current_topic_line=topic_line)
            else:
                log(f"• cold warm-session start — {why}")
                prompt = cold_prompt
                cold_prompt = None
        else:
            # Refresh the clock every turn — a warm session only saw the date once, at grounding,
            # so a long-lived one would drift across midnight. The current topic rides the same
            # refresh, for the same reason (§8 fork 4, above).
            clock_line = (f"(For reference, the authoritative current local time is {local_stamp()} "
                          f"— the owner's configured timezone.)")
            if channel == "telegram":
                clock_line = f"{clock_line}\n{channel_declare.current_topic_line(args.state_dir, topic)}"
            prompt = f"{clock_line}\n\n{prompt_text}"
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
        # Phase 0's turn window opens on the SAME id — `turn_id` is the only correlator the transcript
        # protocol has (§5.4), so the interleave log joins to the transcript for free. Opened before
        # the send and closed in its `finally` below.
        interleave_open_turn(state, turn_id, spoken_text, private_turn=private_turn)
        # Phase 5 of docs/job-origin-routing-spec.md: leave the pointer that lets a job started INSIDE
        # this turn stamp WHO ASKED FOR IT. Written here rather than anywhere else because this is the
        # one moment all four facts are in scope at once — the turn's id, its channel, whether a person
        # composed the line (`turns.classify_origin`, the same classifier `turns.jsonl` already trusts
        # for exactly this distinction), and the Telegram message id, which the durable queue does not
        # carry and only `state.inbound_ids` can answer. It never raises and its failure costs the
        # attribution of any job this turn starts, nothing else.
        asked_message_id, asked_message_why = resolve_inbound_message_id(state.inbound_ids, text)
        jobs.write_turn_pointer(
            args.state_dir, turn_id=turn_id,
            session_id=getattr(state.session, "session_id", None), surface=channel,
            by=("owner" if turns.classify_origin(text) == "human" else "assistant"),
            message_id=asked_message_id, message_why=asked_message_why)
        _tee_chat_event(state, args, log, cockpit_pipe.chat_event(
            "turn_started", source=channel, model=model_name, text_preview=text[:200], turn_id=turn_id))
        # Chat-turn capture, the OWNER's side (turns.py). Deliberately right next to
        # the `text[:200]` tee above, because that truncation is the bug this exists to fix: the same
        # frame holds the whole message, and the cockpit's 200-char frame budget is correct for a chat
        # pane and catastrophic for a memory. So the tee keeps its cap and the corpus takes the verbatim
        # text. Recorded once per TURN rather than per attempt — a redelivery re-enters the drainer with
        # the same `text`, and a conversation record that repeated a message on every retry would be
        # lying about what was said. `origin` is classified in turns.py: a sizeable share of what
        # arrives on the owner's channel is machine-generated (job notices, reaction notices,
        # attachment markers) and must not read as something the owner said.
        if attempts == 1:
            _capture_turn(args, log, surface=channel, speaker="owner", text=spoken_text,
                          turn_id=turn_id, redacted=private_turn,
                          session_id=getattr(state.session, "session_id", None))
        state.session_busy = True
        _push_cockpit_status(state, args)  # push AFTER flipping busy, so this snapshot says in-flight
        try:
            # The send blocks a worker thread on the child's stdout until this turn's result event —
            # the event loop stays free, so reminders/polls/controls keep running underneath.
            on_event = _make_stream_tee(state, args, log, channel, turn_id)
            # `cold_retry_text` is passed ONLY on a resumed spawn — it's the grounding to fall back to
            # if the resume doesn't take, and it means nothing otherwise. Keeping it off the common
            # path also keeps `send(text, on_event=...)` the whole interface a session type has to
            # implement, which several test doubles rely on.
            send_kwargs = {"on_event": on_event}
            if cold_prompt is not None:
                send_kwargs["cold_retry_text"] = cold_prompt
            # Phase 2: a thin wrapper over session.send() that also owns the interrupt-and-continue
            # loop (§5) when live-mode folded something in. `folded` is `[]` on every off/observe
            # turn and on the ordinary (unfolded) live turn — see interleave_live_send's own docstring
            # for why that makes this call byte-for-outcome-identical to the plain send() it replaces.
            reply, folded = await interleave_live_send(state, args, log, prompt, send_kwargs)
        finally:
            state.session_busy = False
            # Phase 0: close the turn window and write the resolution row for every verdict that
            # landed inside it. In the `finally` so every way out of the send — a reply, a dead
            # session, a watchdog kill, an exception — closes the window exactly once.
            interleave_close_turn(state, args, log)
            # And close the requester pointer on the same guarantee, for the same reason: an OPEN
            # pointer is what a job stamps itself from, so one left open past its turn is how a job
            # started outside any turn inherits the last one's attribution. Closed BY ID, so a late
            # close can never close a successor turn's pointer.
            jobs.close_turn_pointer(args.state_dir, turn_id)
        _push_cockpit_status(state, args)
        # P1: did this turn end because the idle-gap watchdog killed a silent child, rather than because
        # the session died on its own? Read BEFORE the session is dropped below. `getattr` because every
        # test double and the stub implement `send` and not much else.
        timed_out = reply is None and bool(getattr(state.session, "timed_out", False))
        timed_out_gap = getattr(state.session, "turn_gap_sec", TURN_IDLE_GAP_SEC)
        if reply is None:  # session died/errored — reset and apologize
            log(f"! warm session {'hung and was killed' if timed_out else 'died'} mid-turn "
                f"({_session_vitals(state.session)})")
            _end_session(state, args, log,                      # neither is resumable, deliberately
                         RESPAWN_TURN_TIMEOUT if timed_out else RESPAWN_TURN_ERROR)
            try:
                await asyncio.to_thread(state.session.close)
            except Exception:  # noqa: BLE001
                pass
            state.session = None
            # A hang gets its own wording rather than borrowing the mid-turn-death apology. "I hit a
            # snag" describes something that went wrong and finished; this one didn't finish, and the
            # honest thing to say is that it hung and I cut it off. It deliberately does NOT promise a
            # retry or claim to have set it aside: on attempts 1 and 2 the message is retried silently,
            # and on the last one the existing dead-letter notice says "set it aside" in its own words,
            # right after this — so neither line can be the one that's lying.
            reply = (turn_timeout_notice(timed_out_gap) if timed_out
                     else "Sorry — I hit a snag just now. Try me again?")
        # reply-marker-forcing-function-spec.md's empty-reply addendum. `session.send()`
        # can come back an EMPTY (or whitespace-only) string — not `None` — for a live, non-hung turn:
        # the branch above never fires, so `reply` reaches here as `""`. Left alone, that empty string
        # sails into the channel/marker machinery below: `extract_channel_declaration("")` and
        # `has_reply_marker("")` both read BOTH required lines as missing, the bounded corrective
        # retry fires, and its own two-line answer — obtained ONLY to recover the two control lines —
        # gets prepended onto nothing and delivered as the entire reply: a bare `*(answering ...)*`
        # header with no substance behind it (visible in `state/channel-declare-log.jsonl` as
        # `retries_used: 1` rows with nothing delivered). Checked HERE, before any of that machinery
        # runs, so the retry is never spent on a reply that was never there.
        #
        # The decision: send NOTHING — not the marker line alone, not an apology, not an empty
        # message. The substance-or-silence spec's whole point is that not everything needs a reply,
        # and an apology would be a false claim that the
        # turn failed; the turn's substantive work may have happened entirely in tool calls. This is
        # deliberately NOT scoped to Telegram — an empty reply on Discord or the cockpit is the same
        # defect, and the channel/marker machinery just below is Telegram-only regardless.
        #
        # Bookkeeping already ran unconditionally above this point (interleave_open_turn, the turn
        # pointer, the "turn_started" tee, the owner's own `_capture_turn`) and in the `finally` block
        # (interleave_close_turn, the turn-pointer close, the cockpit status push) — none of that is
        # skipped. `turn_done` for the cockpit trace panel already fell out of the raw stream "result"
        # event inside `on_event`, independent of what this function does with `reply` afterward, so it
        # is unaffected too. What this branch skips is everything that exists only to describe or
        # deliver SUBSTANCE that isn't there: the channel/marker retry, `deliver_reply` (so no
        # `mouth.record_assertion` row claims the owner was told something, and no `_capture_turn` row
        # claims the assistant said something), and `channel_declare.record_outcome` — a channel-declare-log row for
        # this turn would have to claim SOME `reply_marker_present`/`outcome` value, and every value
        # available would be a lie about a reply that was never sent; the honest state is "this turn
        # asked no routing question at all," which is silence in that log too, not a new outcome value
        # in it. The message is treated as ANSWERED, not failed — nothing here retries it, matching
        # `turn_suppression.py`'s "popped, not spent, not delivered" precedent for the other case where
        # a turn correctly has nothing new to say.
        if not reply.strip():
            log("• turn produced an empty reply — sending nothing (queue popped, not retried)")
            # §5.3: pop the whole verified fold prefix (head + folds), not just the head — a no-op
            # width-1 pop, byte-identical to `pending.pop(0)`, whenever `folded` is empty.
            pop_folded_prefix(state, channel, text, folded)
            save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
            state.last_activity = time.monotonic()
            _push_cockpit_status(state, args)  # queue depth changed
            continue
        # message-routing-spec.md — PHASE 2 LIVE. Parse and STRIP a leading
        # `[[channel:PURPOSE]]` declaration UNCONDITIONALLY, on every channel, so the marker can never
        # reach the owner regardless of what phase routing is in (§1) — `reply` is reassigned here and every
        # consumer below (deliver_reply, append_thread, _capture_turn, mouth.record_assertion, all
        # fed by this same variable) sees the stripped text. This runs identically whether `reply` is
        # genuine model output or the synthesized apology/timeout string just above — the latter never
        # match, which is correct: those are the daemon's own words, not something the owner is owed a
        # channel choice about.
        #
        # reply-marker-forcing-function-spec.md — a SECOND required opening line, checked right here
        # beside the channel one and sharing its SAME retry budget (never a second, independent retry).
        # Unlike the channel line, the reply-marker line is NOT stripped — it is written for the owner, so
        # `has_reply_marker` never mutates `reply`.
        #
        # The bounded retry (§3 — one extra round trip on the SAME warm session, never a re-run of the
        # substantive reply, which is already sitting in `reply` above) is gated `channel == "telegram"`
        # AND a still-live session: there is no routing decision to retry for on Discord/cockpit (§3
        # scope), and a session that just died (the branch above, which sets `state.session = None`)
        # has nothing left to ask — that path falls straight through to `declared is None`, which
        # `resolve_purpose` turns into `main`, exactly where an undeclared reply already goes. A single
        # iteration may need to recover the channel, the marker, or both at once — `retry_prompt` picks
        # the one prompt that names whichever is actually missing that iteration, and `declare_retries`
        # / `marker_retries` attribute the SAME shared iteration to each concern separately (never a
        # second loop or a second `send()`) — see reply-marker-forcing-function-spec.md §3/§6 for why
        # the two counts must not be merged into one.
        #
        # `deliver_reply`'s destination below IS this phase's resolved purpose on a Telegram turn
        # (message-routing-spec.md §7 phase 2 — enabled after an observation window showed the retry
        # rescuing undeclared replies reliably). `resolved_purpose` below WINS over the inbound `topic` unconditionally;
        # `topic` remains the fallback for resolution failure and the untouched signal for every
        # other channel/caller (see `deliver_reply_result`'s own docstring).
        declared, reply = channel_declare.extract_channel_declaration(reply)
        has_marker = channel_declare.has_reply_marker(reply)
        resolved_purpose = None   # stays None off Telegram — no routing decision exists to act on
        if channel == "telegram":
            declare_retries = 0     # channel-need iterations — classify_outcome's existing meaning
            marker_retries = 0      # marker-need iterations — new, counted separately (see above)
            loop_iterations = 0     # the SHARED bound this loop actually enforces
            while ((declared is None or not has_marker) and state.session is not None
                   and loop_iterations < channel_declare.CHANNEL_DECLARE_MAX_RETRIES):
                loop_iterations += 1
                missing_channel = declared is None
                missing_marker = not has_marker
                if missing_channel:
                    declare_retries += 1
                if missing_marker:
                    marker_retries += 1
                retry_ask = channel_declare.retry_prompt(missing_channel=missing_channel,
                                                         missing_marker=missing_marker)
                state.session_busy = True
                try:
                    corrective = await asyncio.to_thread(
                        functools.partial(state.session.send, retry_ask, on_event=on_event))
                finally:
                    state.session_busy = False
                if corrective is None:
                    # The channel-check itself hung or died — fail open, stop retrying. `reply` (the
                    # real answer, already obtained above) is untouched and still gets delivered; this
                    # failure is scoped to the channel/marker question alone.
                    break
                c_declared, c_rest = channel_declare.extract_channel_declaration(corrective)
                if missing_channel and c_declared is not None:
                    declared = c_declared
                if missing_marker:
                    marker_line = channel_declare.extract_reply_marker_line(c_rest)
                    if marker_line is not None:
                        # The ONE case a corrective's own text reaches delivery — prepended, never
                        # replacing, the substantive reply already obtained before this loop ran.
                        # `reply` may itself carry a stray, malformed attempt at the control
                        # lines further down — narration before them is why position-0 parsing above
                        # called them "missing" in the first place. Clean those out FIRST so the
                        # prepend replaces the orphaned attempt instead of stacking a second marker
                        # (and an unstripped `[[channel:...]]` token) beside it.
                        reply = channel_declare.strip_stray_control_lines(reply)
                        reply = f"{marker_line}\n{reply}"
                        has_marker = True
            resolved_purpose = channel_declare.resolve_purpose(declared)
            channel_declare.record_outcome(
                args.state_dir, turn_id=turn_id, channel=channel,
                inbound_purpose=channel_declare.current_purpose(args.state_dir, topic),
                declared_purpose=declared, resolved_purpose=resolved_purpose,
                retries_used=declare_retries,
                outcome=channel_declare.classify_outcome(declared, resolved_purpose, declare_retries),
                reply_marker_required=True, reply_marker_present=has_marker,
                reply_marker_retries_used=marker_retries)
        # The turn's own look, read once at delivery (docs/mouth-spec.md). Taken
        # here rather than inside deliver_reply so the funnel stays free of daemon state and its other
        # callers — job pushes, the wake line — keep passing nothing, which is the honest answer for
        # them. Read AFTER the send, so a turn that died mid-stream still records what it had looked
        # at before it died.
        turn_scope = turn_scope_snapshot(state, turn_id)
        if await asyncio.to_thread(deliver_reply, channel, reply, args, log,
                                   turn_id=turn_id, redacted=private_turn, scope=turn_scope,
                                   topic=topic, channel_purpose=resolved_purpose):
            # The thread the reply ACTUALLY went to, not the inbound one — `resolved_purpose` wins on
            # a Telegram turn (phase 2), so the continuity cache has to follow the same destination or
            # a cold spawn would be grounded on the wrong topic's history. A successful purpose-routed
            # send has already persisted its topic's thread id (or fallen back to the inbound one), so
            # this is a cheap state-file echo of `deliver_reply`'s own resolution, never a second send
            # or a second network call.
            append_thread(args.state_dir, "assistant", reply,
                          topic=_delivered_thread_for_append(channel, resolved_purpose, topic, args))
            if timed_out:
                # P1/§4 Q2: a hung turn is the ONE delivered outcome that does not pop. Nothing was
                # answered — the owner was told the turn was cut off — so the message stays at the head of
                # the durable queue with the attempt already counted and deliberately NOT rolled back.
                # That is what makes MAX_TURN_ATTEMPTS reachable within one boot: two more tries, then
                # the existing dead-letter sets it aside. Rolling back here would rebuild the very bug
                # this fixes in miniature — a counter that can only be advanced by a restart.
                # (The rollback on a *delivery* failure below is a different case and stays untouched:
                # a Telegram outage must never dead-letter a good message.)
                # Decided: hung turns count toward the poison pill. §4 Q2 of
                # docs/hung-turn-deadline-spec.md says how to flip it if that decision changes.
                log(f"! turn {attempts}/{MAX_TURN_ATTEMPTS} timed out — message kept queued: {text[:80]!r}")
                state.last_activity = time.monotonic()
                continue
            # answered AND delivered — drop the whole verified fold prefix from the durable queue.
            # §5.3: `folded` is `[]` on every off/observe turn and every unfolded live one, so this is
            # exactly `pending.pop(0)` in every case but a live-mode fold chain.
            popped = pop_folded_prefix(state, channel, text, folded)
            if len(folded) and popped < 1 + len(folded):
                log(f"! interleave (live): fold prefix mismatch at delivery — popped {popped} of "
                    f"{1 + len(folded)} expected entries; the rest stay queued for their own turns")
            save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
            state.last_activity = time.monotonic()
            # Warm-model spawn-fallback happened this turn (WarmSession self-healed a bad warm dial):
            # tell the owner ONCE per session so the noisy respawn isn't silent. The config file still
            # points at the bad model, so every fresh session re-heals until the dial is fixed — but we
            # only nag on the first delivered turn of each. Fail-open: an alert hiccup never affects the
            # turn we just delivered.
            if getattr(state.session, "did_fallback", False) and not getattr(state.session, "fallback_alerted", False):
                state.session.fallback_alerted = True
                landed = getattr(state.session, "model", None) or args.model
                await asyncio.to_thread(
                    deliver_reply, channel,
                    ("⚠️ Heads up — my warm_model dial pointed at a model the CLI wouldn't spawn, so I "
                     f"fell back to {landed} to keep this chat alive. The config still needs fixing: say "
                     "the word and I'll set the warm dial back to a known-good model."),
                    args, log)
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
                state.pending[0] = (channel, text, max(0, attempts - 1), topic)
            log(f"! reply to the owner NOT delivered via {channel}; kept queued for retry")
            if state.session is not None:
                _end_session(state, args, log, RESPAWN_UNDELIVERED)  # not resumable, deliberately
                try:
                    await asyncio.to_thread(state.session.close)
                except Exception:  # noqa: BLE001
                    pass
                state.session = None
            save_daemon_state(args.state_dir, state.pending, None)
            await _sleep_or_stop(state, 5.0)


VITALS_INTERVAL_SEC = 60  # vitals.json is "written once a minute"


def _roll_vitals_day(state: DaemonState, now: datetime) -> None:
    """Reset the day's running counters at the local-date boundary, mirroring `slot_retries`'
    in-memory-only posture: `vitals.json` is a health SNAPSHOT, not a ledger, so a restart or a day
    rollover losing the running total is the accepted cost — the durable record of what actually fired
    is `mouth.jsonl` and `failures.jsonl`."""
    today = ra.local_today(now)
    if state.vitals_day == today:
        return
    state.vitals_day = today
    state.reminders_fired_today = 0
    state.reminders_failed_today = 0
    state.slots_failed_today = 0


def maybe_write_vitals(state: DaemonState, state_dir: str, now: datetime | None = None) -> bool:
    """Once a minute: `{tick_at, reminders_fired_today, reminders_failed_today,
    last_reminder_fired_at, slots_failed_today, outbox_dead, warm_session}` — the watchdog's read side
    is `failures.watchdog_status`. Additive: nothing in this tree reads the file yet outside that one
    function. Fail-open — a write failure here must never cost the tick it rides on. Returns whether a
    write was attempted (a test seam, not a caller obligation)."""
    now = now or datetime.now(timezone.utc)
    _roll_vitals_day(state, now)
    nowm = time.monotonic()
    if state.vitals_written_at and (nowm - state.vitals_written_at) < VITALS_INTERVAL_SEC:
        return False
    state.vitals_written_at = nowm
    try:
        stateio.write_json_atomic(os.path.join(state_dir, failures.VITALS_FILENAME), {
            "tick_at": now.isoformat().replace("+00:00", "Z"),
            "reminders_fired_today": state.reminders_fired_today,
            "reminders_failed_today": state.reminders_failed_today,
            "last_reminder_fired_at": state.last_reminder_fired_at,
            "slots_failed_today": state.slots_failed_today,
            "outbox_dead": (state.outbox or {}).get("dead", 0),
            "warm_session": state.session is not None,
        })
    except Exception:  # noqa: BLE001 — a vitals write must never break the scheduler tick
        pass
    return True


async def _reconcile_jobs(state: DaemonState, args, log) -> None:
    """One background-jobs reconcile pass (seneschal/scripts/jobs.py) — the code-backed half of "I'll tell
    you when it's done."

    This is what makes the completion push survive everything the warm session doesn't: the promise
    lives in `state/jobs/<id>.json`, not in the turn that made it, so a wind-down, a turn error, or a
    Path-A merge reload mid-job all end with the successor daemon reading the same un-notified
    terminal record off disk and sending. A prompt-side-only contract, by contrast, produces zero
    rows in practice.

    In a thread because `deliver_reply` does network I/O with a retry+sleep, and the tick must not
    block reminders or chat. `jobs.reconcile` is handed `deliver_reply` itself precisely because it
    reports whether the send actually LANDED — a job is stamped notified only then, so a Telegram
    outage retries next tick instead of eating the ping.

    The `--wake` enqueue is done HERE off the returned records, rather than via `reconcile`'s own
    `wake` seam, because `_enqueue_inbound` is async and reconcile runs in a worker thread. The bare
    push has already landed by this point either way — the wake is the bonus, never the guarantee."""
    if args.no_jobs:
        return

    def _notify(channel: str, text: str) -> dict:
        # `kind="job"` is what makes jobs' speech distinguishable in the assertions log — this wrapper
        # IS jobs.reconcile's mouth (mouth-spec.md §2.1), and deliver_reply writes the row.
        #
        # **The `_result` form, deliberately.** The bool one collapses "refused before a
        # byte moved" and "the request went out and may have landed" into the same `False`, and
        # `reconcile` reads a `False` as licence to send again on the next tick — which is the blind
        # retry `telegram_http`, `telegram_send` and `deliver_reply` each separately refuse. Handing
        # the phase across the boundary is what stops the fourth layer from undoing the other three.
        return deliver_reply_result(channel, text, args, log, kind="job")

    def _pass() -> tuple:
        # Reconcile and count the survivors in ONE thread hop. The count feeds the status snapshot's
        # `jobs_active`, which is deliberately cache-fed so the hot status path does no disk I/O.
        notified = jobs.reconcile(args.state_dir, notify=_notify, log=log)
        return notified, len(jobs.list_jobs(args.state_dir, active_only=True))

    try:
        notified, active = await asyncio.to_thread(_pass)
        state.jobs_active = active
    except Exception as e:  # noqa: BLE001 — a broken job store must never break the scheduler tick
        log(f"! job reconcile failed: {e}")
        return
    wakes = job_wakes(notified)
    if wakes:
        await _enqueue_inbound(state, args, log, wakes)


def job_wakes(notified: list) -> list:
    """Which of this pass's newly-notified jobs should also wake the warm session, as
    `_enqueue_inbound`-shaped tuples. Pulled out of `_reconcile_jobs` as its own pure, sync function
    so it's testable without the daemon's async plumbing around it.

    **`jobs.cancel_wake_suppressed` excludes an assistant-surface self-cancel** — its push already
    landed as the one-line acknowledgement `notify_text` built for it, and waking the session to "read
    the log, tell the owner plainly what happened" would repeat the reason it already gave, a second
    time, in the same turn's own words."""
    return [(rec.get("notify", {}).get("channel") or "telegram", jobs.wake_text(rec), 0)
            for rec in notified if rec.get("wake") and not jobs.cancel_wake_suppressed(rec)]


async def scheduler_task(state: DaemonState, args, log) -> None:
    """The cadence work the old loop did once per (Telegram-paced) iteration, now on its own steady
    ~5 s tick: lock heartbeat, slot reap/launch, reminder fires, roll refill, comms peek. The big
    reactive win lives here — a due reminder no longer waits out a long chat turn or a 25 s poll.

    check_reminders runs in a worker thread while a chat turn may be mid-flight; if the chat's
    reminders_dequeue.py rewrites reminders.json in that window, last-writer-wins could resurrect a
    just-dequeued nudge — the durable ack ledger (state/acks.json, checked at fire time) is the
    correctness backstop that keeps a resurrected entry from actually buzzing (the fire-time ack
    gate)."""
    while not state.stop.is_set():
        beat_lock(args.state_dir)
        # A daemon that's been up past the settle window is proof it recovered — self-heal the crash-loop
        # guard (clear state/seneschald-crashloop + reset boot-attempts) so a past loop stops haunting a now-
        # healthy daemon. One-shot (latched on state), so it's a cheap flag check every tick after that.
        maybe_clear_crashloop_on_sustained(state, args, log)
        # Stamp any slot whose run just finished cleanly (and relaunch — leave unstamped — any that
        # died), BEFORE re-evaluating what's due below, so a failed morning run retries promptly.
        # on_failed feeds vitals.json's slots_failed_today, below.
        reap_finished_slots(state.slot_children, args.state_dir, log, state.slot_retries,
                            on_failed=lambda: setattr(state, "slots_failed_today",
                                                      state.slots_failed_today + 1),
                            slot_hold_until=state.slot_hold_until)
        # Detached background jobs: move any that ended to terminal and fire their completion push.
        # Sits next to the slot reap because it's the same duty — noticing that something we started
        # has finished — except these survive us, so a restart mid-job doesn't lose the ping.
        await _reconcile_jobs(state, args, log)
        discord_on = bool(args.discord_env) and not args.no_discord
        if not args.no_reminders:
            # Capture the signals rather than discarding them: the two suppression kinds
            # destroy a nudge silently, and until now nothing anywhere recorded that it had happened.
            reminder_signals = await asyncio.to_thread(
                check_reminders, args.state_dir, datetime.now(timezone.utc), True, args.telegram_env,
                call_env=args.call_env, discord_env=args.discord_env if discord_on else None)
            log_reminder_suppressions(reminder_signals, log)
            # vitals.json's counters, fed off the same signals — additive, nothing here changes what
            # the signals already drive below.
            now_signal = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            for sig in reminder_signals:
                if sig.get("kind") == "reminder_fired":
                    state.reminders_fired_today += 1
                    state.last_reminder_fired_at = now_signal
                elif sig.get("kind") == "reminder_send_failed":
                    state.reminders_failed_today += 1
            # A nudge that went to the Reminders topic joins that topic's continuity cache, so the
            # owner's reply under it is read against the thing it answers rather than against nothing.
            record_topic_nudges(args.state_dir, reminder_signals, log)
            # Top up standing every-N-hours rolls once per local day. In a thread: it takes the
            # cross-process queue lock, which may wait behind a delivery burst.
            await asyncio.to_thread(maybe_refill_rolls, args.state_dir, log)
        # Once per local day: has a Dream step stopped happening? Deliberately OUTSIDE the
        # `no_reminders` gate above — this is not a nudge the owner scheduled, it is the daemon noticing
        # that nightly work has silently stopped, and `--no-reminders` must not be able to switch
        # that off. Pure-local, so it costs a file read on the one tick per day that finds work.
        maybe_nudge_stale_dream_steps(args.state_dir, log)
        warm_busy = state.warm_busy()
        if not args.no_peek:
            maybe_peek(args.state_dir, args, log, state.headless_children, warm_busy)
        if not args.no_slots:
            maybe_run_slots(args.state_dir, args, log, state.headless_children, warm_busy,
                            slot_children=state.slot_children, slot_hold_until=state.slot_hold_until)
            # The once-per-local-day exact-time reminder seed (retired the four fixed reminder slots).
            # Called after maybe_run_slots so, in a contended tick, a just-launched slot's in-flight
            # marker defers the seed (and vice-versa) — the two never spawn overlapping store bursts.
            maybe_seed_day(args.state_dir, args, log, state.headless_children, warm_busy,
                           slot_children=state.slot_children, slot_hold_until=state.slot_hold_until)
        # The write-behind outbox's flush (Notion backend only — _tend_outbox gates it). AFTER the
        # slots, so a due Brief/Wrap always wins the single headless-child slot; a deferred drain just
        # re-evaluates next tick. Its own `no_reminders` posture matches the Dream-step alarm's — a
        # store write that has stopped landing is not a nudge the owner scheduled, so `--no-reminders`
        # must not silence it.
        await _tend_outbox(state, args, log, warm_busy)
        # Once a minute: the failure watchdog's read side (failures.watchdog_status). AFTER the
        # outbox tend, so outbox_dead is this tick's own reading rather than the previous one's.
        maybe_write_vitals(state, args.state_dir)
        await _drain_cockpit_inbox(state, args, log)
        # The Mouth's queue (mouth-spec phase 1). After the reminder and job paths, so an
        # item those enqueue this tick goes out on the same tick rather than waiting ~5 s.
        await _drain_mouth(state, args, log)
        # LAST in the tick, deliberately. A reading is a ~14 s spawn; putting it here means the
        # worst it can ever delay is the START of the next tick, never this tick's reminders, jobs,
        # outbox or mouth. In a thread, so the event loop keeps serving Telegram and the warm
        # session throughout — and wrapped, because a plan-meter reading may never be why the
        # scheduler task dies (a missed reading is a missing row, not an incident).
        try:
            touched = await asyncio.to_thread(
                maybe_usage_reading, args.state_dir, args, log, state.headless_children,
                warm_busy, {"pid": os.getpid(), "started_at": state.boot_at.isoformat()})
            # usage-telemetry-spec §8.3 — failure should speak, but nothing crazy. Gated on the
            # reading having touched the series, so the ~hourly cadence is also the health check's
            # cadence and the ~5 s tick never tails the readings log. A notice the quiet window holds
            # is simply re-evaluated at the next reading, which is how it waits rather than drops.
            if touched:
                await asyncio.to_thread(maybe_usage_notice, args.state_dir, args, log)
        except Exception as e:  # noqa: BLE001
            log(f"! usage reading raised into the tick (ignored): {type(e).__name__}: {e}")
        await _sleep_or_stop(state, args.tick_sec)


async def _tend_outbox(state: DaemonState, args, log, warm_busy: bool) -> None:
    """One tick's worth of ownership over the Notion write-behind outbox: read it, sweep what a newer
    ack superseded, flush what's stale, and nudge the owner if it has stopped draining altogether.

    **Notion backend only** (the `outbox-backend-gate`): the outbox is a Notion mechanism
    (store/notion/mapping.md, "Outbox — durable act-low writes"), so on a filesystem backend — or with
    no store configured at all, the shipped default — this returns before touching sqlite and
    `state.outbox` stays empty (every outbox gauge reads 0).

    Throttled to `OUTBOX_CHECK_SEC` because the read opens sqlite; the reading is cached on `state` for
    the status snapshot, exactly like `jobs_active`. In a worker thread for the same reason
    `check_reminders` is — the tick must not stall on a contended store's busy timeout."""
    if getattr(args, "no_outbox", False):
        return
    now = time.monotonic()
    if state.outbox_checked_at and (now - state.outbox_checked_at) < OUTBOX_CHECK_SEC:
        return
    state.outbox_checked_at = now
    if store_backend_active() != "notion":
        state.outbox = {}
        return
    backlog = await asyncio.to_thread(outbox_backlog, args.state_dir)
    state.outbox = backlog
    if backlog.get("resolved"):
        log(f"• outbox: {backlog['resolved']} entr{'y' if backlog['resolved'] == 1 else 'ies'} "
            f"superseded by a newer ack (retired without a Notion write)")
    maybe_nudge_outbox_backlog(args.state_dir, log, backlog)
    # ON THE LOOP, not in a thread — unlike the read above. It appends to `state.headless_children`,
    # which `maybe_peek`/`maybe_run_slots`/`prune_children` all mutate from the loop thread; adding a
    # second mutator from a worker would race the gate that exists to keep exactly one headless store
    # child alive. Everything it does is a few small file reads plus a `Popen` that returns
    # immediately, which is precisely what `maybe_peek` already does synchronously here.
    maybe_drain_outbox(args.state_dir, args, log, state.headless_children, warm_busy, backlog)


async def _drain_mouth(state: DaemonState, args, log) -> None:
    """Deliver the Mouth's queue, oldest first (docs/mouth-spec.md phase 1).

    **The second door.** Unsolicited speech queues in `state/outbound.jsonl` and this drains it on the
    tick; **turn replies stay direct** through `deliver_reply`, because that function is synchronous
    and its landed-boolean feeds the poison-pill guard — queueing a reply would break both. Both doors
    write the same assertions log, so the queue is a scheduling change and never a change to what is
    recorded.

    Runs in a thread: `send_telegram` is blocking network I/O, and the tick must not stall on a slow
    surface any more than it does for `check_reminders`.

    Fail-open in the same shape as everything else on this tick — a broken queue costs the queue, not
    the daemon. An unmigrated producer keeps working exactly as it does today (spec §7), so an empty
    queue is the normal state until producers move over one at a time."""
    def _send(surface: str, text: str, item: dict) -> bool:
        if surface == "telegram":
            return bool(send_telegram(text, args.telegram_env))
        if surface == "discord" and args.discord_env and not args.no_discord:
            return bool(send_discord(text, args.discord_env))
        # An unknown or unconfigured surface is not a delivery failure to retry forever — but phase 1
        # deliberately does not drop it either: it stays pending and visible rather than silently
        # discarded, which is the property the whole spec is about.
        return False

    try:
        res = await asyncio.to_thread(mouth.drain, args.state_dir, send=_send, log=log)
        if res.get("sent") or res.get("dropped"):
            log(f"• mouth: sent {res['sent']}, dropped {res['dropped']}, "
                f"pending-after-failure {res['failed']}")
    except Exception as exc:  # noqa: BLE001 — the queue, never the tick
        log(f"! mouth drain failed: {exc}")


def _cockpit_inbox_text(it: dict) -> str:
    """One fallback-inbox item -> the text `_enqueue_inbound` should see, honoring `force_fable` (v3)
    exactly like the live-pipe path (`cockpit_task.on_chat_send`) — the SAME `!fable` synthesis so
    `apply_force_route`'s single detection point covers both the live pipe and this degraded fallback."""
    text = (it.get("text") or "").strip()
    if not text:
        return ""
    if it.get("force_fable") and not strip_force_fable(text)[0] and not is_status_command(text):
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
    pending_action and wind the whole supervisor down; main() re-spawns after state is saved.

    A `restart-site` entry (the cockpit's per-archon-site Restart button, cockpit/server/control.py's
    `enqueue_restart_site`) is a special case: unlike `restart`/`shutdown` it's about ONE archon's own
    site process, never the daemon itself, so it's drained out of FIFO order on every pass — never
    gated on chat idle, never a whole-daemon stop — straight into `state.archon_site_restart_requests`
    for `archon_sites_task` to pick up on its next reconcile pass. Draining it first also keeps it from
    getting stuck behind an idle-deferred `restart`/`shutdown` entry that happens to be ahead of it in
    the queue."""
    if args.stub_brain or args.fake_inbox is not None:
        return  # test modes never restart themselves (matches the old loop's gating)
    while not state.stop.is_set():
        controls = load_control_queue(args.state_dir)
        site_restarts = [c for c in controls if c.get("action") == "restart-site"]
        if site_restarts:
            controls = [c for c in controls if c.get("action") != "restart-site"]
            save_json(control_queue_path(args.state_dir), controls)
            for c in site_restarts:
                target = c.get("target")
                if isinstance(target, str) and target.strip():
                    state.archon_site_restart_requests.add(target.strip())
                    log(f"• control 'restart-site' — queued for archon {target!r}")
                else:
                    log(f"! control 'restart-site' — missing/invalid target {target!r}, ignored")
        if restart_requested(args.state_dir):
            controls = controls + [{"action": "restart", "defer_until_idle": True,
                                    "reason": "restart.request sentinel"}]
        control = controls[0] if controls else None
        if control is None:
            state.control_pending = False
            state.control_held_since = None
            state.slot_drain_held_since = None
        else:
            action = control.get("action", "restart")
            deferring = control.get("defer_until_idle", True) and not state.chat_idle()
            if deferring and state.control_held_since is None:
                state.control_held_since = time.monotonic()
            if not deferring:
                state.control_held_since = None
            held = (0.0 if state.control_held_since is None
                    else time.monotonic() - state.control_held_since)
            chat_forced = deferring and held >= CONTROL_MAX_HOLD_SEC

            # "Drain, don't kill": a slot child is a heavyweight, often DESTRUCTIVE run (the Daily
            # Journal clears its inbox in its own first phase) whose relaunch-from-scratch is a silent no-op,
            # not a retry — so a reload must not kill one in flight. Additional to, never a replacement
            # for, the chat-idle gate above: this holds even when chat_idle() is already true.
            draining = bool(state.slot_children)
            if draining and state.slot_drain_held_since is None:
                state.slot_drain_held_since = time.monotonic()
                log(f"• control {action!r} — holding for in-flight slot(s) "
                    f"{sorted(state.slot_children)} before reload "
                    f"(cap {SLOT_DRAIN_MAX_HOLD_SEC / 60:.0f} min)")
            elif not draining and state.slot_drain_held_since is not None:
                log(f"• control {action!r} — in-flight slot drained, proceeding with reload")
                state.slot_drain_held_since = None
            slot_held = (0.0 if state.slot_drain_held_since is None
                         else time.monotonic() - state.slot_drain_held_since)
            slot_forced = draining and slot_held >= SLOT_DRAIN_MAX_HOLD_SEC
            if slot_forced:
                log(f"! control {action!r} — slot drain timed out after {slot_held / 60:.1f} min "
                    f"(cap {SLOT_DRAIN_MAX_HOLD_SEC / 60:.0f} min) — applying anyway, "
                    f"in-flight slot(s) {sorted(state.slot_children)} will be killed")
                state.slot_drain_held_since = None

            if not (chat_forced or slot_forced) and (deferring or draining):
                # Hold it; the drainer winds the session down after CONTROL_PENDING_IDLE_SEC of quiet and
                # the Telegram task shortens its long-poll, so the reload lands right after a quiet gap.
                state.control_pending = True
                await _sleep_or_stop(state, 2.0)
                continue
            if chat_forced:
                # P2: the hold is bounded. `chat_idle()` is three conditions, and before P1 all three
                # could be false FOREVER on one hung turn — so a single wedge stopped Path A deploying
                # while seneschald-update went on ff-pulling and enqueueing reloads that never applied.
                # Applying mid-turn costs at most the turn in flight, and the durable action queue
                # already survives a restart; a deploy that never lands has no such backstop. This is
                # the same apply path a `defer_until_idle: false` control has always taken.
                log(f"! control '{action}' held {held / 60:.1f} min with no idle window "
                    f"(cap {CONTROL_MAX_HOLD_SEC / 60:.0f} min) — applying anyway, mid-turn")
            elif deferring:
                # The slot-drain cap forced this through while chat was still mid-turn (both gates busy
                # at once — rare). Say so plainly rather than staying silent about the interrupted turn.
                log(f"! control '{action}' — slot drain forced this through with chat still mid-turn "
                    f"({held / 60:.1f} min, under the {CONTROL_MAX_HOLD_SEC / 60:.0f} min cap)")
            log(f"• control '{action}' — applying (reason: {control.get('reason', '')!r})")
            if control.get("reason") != "restart.request sentinel":
                pop_control(args.state_dir)         # drop the queued entry we're applying
            clear_restart_request(args.state_dir)   # and clear any legacy sentinel
            state.pending_action = action
            state.stop.set()
            return
        await _sleep_or_stop(state, 2.0)


async def cockpit_task(state: DaemonState, args, log) -> None:
    """The cockpit pipe task: a localhost-only, token-authed WebSocket pipe
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
        # `!status` is a daemon command, never a prompt — don't let a left-on "Send to Fable" toggle
        # turn it into `!fable !status` and spend a Fable delegation asking a model to guess at the
        # daemon's own vitals.
        if frame.get("force_fable") and not strip_force_fable(text)[0] and not is_status_command(text):
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


async def archon_sites_task(state: DaemonState, args, log) -> None:
    """The archon-site task: reconcile every archon's self-declared human-facing site
    (archon_sites.py; seneschal/docs/cockpit-spec.md's Archons bullet) against reality. Each pass:
    discover declared sites, honor any pending cockpit restart-site request, health-check what's left
    over HTTP, and (re)spawn anything down or wedged (past its spawn-grace window, so a site's own
    startup rebuild isn't mistaken for wedged and thrashed). Detached survivors + a PID file mean a
    healthy site is a complete no-op across a daemon reload — see archon_sites.py's module docstring for
    the full "reconcile-not-spawn" reasoning.

    Degrades — no supervision this run, never a crash — when disabled (`--no-archon-sites`) or in
    test/offline modes (`--stub-brain`/`--fake-inbox`, matching cockpit_task/control_task's gating).
    Auto-on otherwise: with no `archons/*/site.json` at all, `discover_sites` just returns an empty
    list every pass and this is a cheap no-op loop."""
    if args.no_archon_sites or args.stub_brain or args.fake_inbox is not None:
        return
    pids = archon_sites.load_pids(args.state_dir)  # adopt whatever a prior incarnation spawned
    spawned_at: dict = {}  # archon_id -> monotonic spawn time, this run's anti-thrash grace timer
    respawns: dict = {}  # archon_id -> consecutive failed revivals, for the give-up backstop
    while not state.stop.is_set():
        try:
            for spec in archon_sites.discover_sites(args.archons_dir):
                if spec.id in state.archon_site_restart_requests:
                    # The cockpit's per-site Restart button: kill the current process (if any) and clear
                    # its grace timer so the health-check-fails branch below respawns it unconditionally
                    # this pass, ignoring SPAWN_GRACE_SEC (a deliberate restart isn't a thrash to guard
                    # against). Also clears the give-up counter — an explicit ask is a fresh start.
                    archon_sites.kill_pid(pids.pop(spec.id, None))
                    spawned_at.pop(spec.id, None)
                    respawns.pop(spec.id, None)
                    state.archon_site_restart_requests.discard(spec.id)
                    log(f"• archon-site {spec.id}: restart requested — killed, will respawn this pass")

                if await asyncio.to_thread(archon_sites.site_healthy, spec.port, spec.health_path):
                    if respawns.pop(spec.id, 0):
                        log(f"• archon-site {spec.id}: healthy again — revival counter reset")
                    continue
                # "Never spawned" is None, NOT 0. `spawned_at.get(spec.id, 0)` looked equivalent but 0 is
                # a perfectly ordinary time.monotonic() reading — it is time since BOOT, not since epoch.
                # So on a freshly-booted machine the sentinel would read as "spawned at monotonic 0",
                # i.e. `uptime < SPAWN_GRACE_SEC`, and the grace check would swallow the very first spawn.
                # That is exactly when it hurts: the daemon starts from a logon trigger AT boot, so every
                # reboot would silently skip spawning archon sites until uptime passed the grace window.
                # It also makes a grace-window test flaky on a CI runner younger than the grace.
                last_spawn = spawned_at.get(spec.id)
                if last_spawn is not None and time.monotonic() - last_spawn < SPAWN_GRACE_SEC:
                    continue  # just (re)spawned — give its own startup rebuild a chance before retrying
                if respawns.get(spec.id, 0) >= ARCHON_SITE_MAX_RESPAWNS:
                    continue  # gave up (logged once below); a cockpit Restart or a recovery resets it

                archon_sites.kill_pid(pids.get(spec.id))  # clear a wedged one first, in case one's alive
                pid = archon_sites.spawn_site(spec, log)
                if pid is None:
                    continue  # spawn_site already logged why; try again next pass
                pids[spec.id] = pid
                spawned_at[spec.id] = time.monotonic()
                respawns[spec.id] = respawns.get(spec.id, 0) + 1
                archon_sites.save_pids(args.state_dir, pids)
                log(f"• archon-site {spec.id}: (re)started on :{spec.port} pid={pid}")
                if respawns[spec.id] >= ARCHON_SITE_MAX_RESPAWNS:
                    log(f"! archon-site {spec.id}: {respawns[spec.id]} revivals with no healthy "
                        f"response — GIVING UP (no more respawns until it recovers on its own or you "
                        f"hit Restart in the cockpit). Check its own log under archons/{spec.id}/state/logs/.")
        except Exception as e:  # noqa: BLE001 — FAIL-OPEN: a flaky archon site must never take the
            log(f"! archon-sites reconcile error (continuing): {e}")  # daemon's chat/reminders down
        await _sleep_or_stop(state, ARCHON_SITES_INTERVAL)


async def _run_cockpit_build(state: DaemonState, log) -> bool:
    """Run the planned frontend build (`cockpit_site.build_steps`) as ASYNC subprocesses, so a
    multi-minute `npm ci` never blocks the event loop and never stalls a graceful reload — on
    `state.stop` we terminate the child and give up, leaving the cockpit API-only until the next boot
    picks the build back up. Returns True only if every step exited 0.

    Every failure is logged and swallowed: npm missing (an empty plan), a non-zero exit, a timeout, an
    OSError launching it. None of them are fatal — the backend still serves the API and the daemon
    pipe; only `/` is missing, which `GET /api/health`'s `web_dist_present` reports honestly."""
    steps = cockpit_site.build_steps(REPO_ROOT)
    if not steps:
        log(f"! cockpit: no npm/package.json — skipping the UI build (API-only; / will 404)")
        return False
    for step in steps:
        log(f"• cockpit: npm {step.name} starting (cockpit/web) — this can take a few minutes")
        try:
            proc = await asyncio.create_subprocess_exec(
                *step.argv, cwd=step.cwd, env=cockpit_site.build_env(),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        except (OSError, ValueError) as e:
            log(f"! cockpit: npm {step.name} failed to launch: {e}")
            return False
        stop_wait = asyncio.ensure_future(state.stop.wait())
        run_wait = asyncio.ensure_future(proc.communicate())
        try:
            done, _pending = await asyncio.wait(
                {stop_wait, run_wait}, timeout=cockpit_site.NPM_TIMEOUT_SEC,
                return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_wait.cancel()
        if run_wait not in done:
            # Either the daemon is shutting down or npm has hung past its ceiling. Same response
            # either way: kill it and stop building. A reload must not wait on npm.
            reason = "daemon stopping" if state.stop.is_set() else f"timed out after {cockpit_site.NPM_TIMEOUT_SEC}s"
            log(f"! cockpit: npm {step.name} abandoned ({reason}) — API-only for now")
            run_wait.cancel()
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
            return False
        _out, err = run_wait.result()
        if proc.returncode != 0:
            tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
            log(f"! cockpit: npm {step.name} exited {proc.returncode} — API-only. " + " / ".join(tail))
            return False
    log(f"• cockpit: UI built (cockpit/web/dist)")
    return True


async def cockpit_app_task(state: DaemonState, args, log) -> None:
    """The cockpit-app task: keep **the cockpit backend** itself
    (`cockpit/server/app.py`) — running whenever the daemon is, so the dashboard stops depending on a
    human leaving `uvicorn` open in a window. Same reconcile-not-spawn discipline as
    `archon_sites_task`, and for the same reasons (detached survivors + a PID file, so a daemon reload
    ADOPTS rather than duplicates — and the cockpit pipe serves exactly ONE client, so a rival backend
    would fight the adopted one for it). Mechanics live in `cockpit_site.py`; this is the loop.

    Each pass, in order:

    1. **Deploy bounce.** If the checkout's git HEAD no longer matches the rev the running backend was
       spawned at, kill it so this same pass respawns it on the merged code. This is what makes a
       merged cockpit change actually load, and it covers every restart path uniformly (seneschald-update,
       `reseneschald`, break-glass force-pull, crash-restart) without hooking any of them — see
       `cockpit_site.read_head_rev`.
    2. **Health-check** `GET /api/health` — public in every auth mode, so a 503 auth wall can't be
       mistaken for a dead process.
    3. **(Re)spawn** what's down, past the same anti-thrash grace window and give-up backstop the
       archon-site loop uses (uvicorn needs a longer grace: its first request imports fastapi and the
       whole app module).
    4. **Build the UI** when `cockpit/web/dist` is missing or stale, AFTER the spawn — the API and the
       pipe come up in seconds, the UI arrives a few minutes later with one bounce, because `app.py`
       mounts `dist` at import time.

    Degrades — no cockpit this run, never a crash — when disabled (`--no-cockpit-app`), in
    test/offline modes (`--stub-brain`/`--fake-inbox`, matching the sibling tasks' gating), or when the
    `cockpit` extras aren't importable. That last one is the common case on a fresh machine and gets
    ONE Telegram nudge per boot: loud enough not to be a silent log-and-return, quiet enough not to
    nag."""
    if args.no_cockpit_app or args.stub_brain or args.fake_inbox is not None:
        return
    if not cockpit_site.deps_available():
        log(f"! cockpit: cockpit extras not installed — not starting. "
            "Fix: uv sync --extra cockpit, then reseneschald.")
        await _notify_cockpit_missing_deps(args, log)
        return

    record = cockpit_site.load_record(args.state_dir)  # adopt whatever a prior incarnation spawned
    spawned_at = None   # monotonic time of THIS run's last spawn — the anti-thrash grace timer
    respawns = 0        # consecutive failed revivals, for the give-up backstop
    builds = 0          # build attempts this boot (npm is minutes; don't retry it every 20s forever)
    while not state.stop.is_set():
        try:
            cockpit_log_path = os.path.join(args.state_dir, "logs", cockpit_site.LOG_FILENAME)
            rev = cockpit_site.read_head_rev(REPO_ROOT)
            healthy = await asyncio.to_thread(archon_sites.site_healthy, args.cockpit_app_port,
                                              cockpit_site.HEALTH_PATH)
            if healthy and rev and record.get("rev") and record["rev"] != rev:
                log(f"• cockpit: checkout moved {record['rev'][:7]} → {rev[:7]} — bouncing onto "
                    "the merged code")
                archon_sites.kill_pid(record.get("pid"))
                cockpit_site.clear_record(args.state_dir)
                record, spawned_at, respawns, builds = {}, None, 0, 0
                healthy = False  # fall through and respawn in this same pass

            if healthy:
                if respawns:
                    log(f"• cockpit: healthy again — revival counter reset")
                    respawns = 0
                bounced = False
                # The UI may still be missing/stale — first boot spawns API-only and builds after.
                if (not args.no_cockpit_web_build and builds < COCKPIT_APP_MAX_BUILDS
                        and await asyncio.to_thread(cockpit_site.build_needed, REPO_ROOT)):
                    builds += 1
                    if await _run_cockpit_build(state, log):
                        # app.py mounts web/dist at IMPORT time — a fresh build only shows after a bounce
                        log(f"• cockpit: bouncing to serve the freshly built UI")
                        archon_sites.kill_pid(record.get("pid"))
                        cockpit_site.clear_record(args.state_dir)
                        record, spawned_at = {}, None
                        bounced = True
                # The child holds cockpit.log open for its whole life (log-rotation-spec.md), so a
                # size-triggered roll can only happen at a kill-then-respawn point — reuse this one
                # rather than run rotation on its own timer. If the UI-build bounce above already
                # fired this pass, the log gets checked again next pass instead of double-bouncing.
                if not bounced and log_rotation.needs_rotation(cockpit_log_path,
                                                                COCKPIT_LOG_MAX_BYTES):
                    log(f"• cockpit: log oversized — bouncing to rotate it")
                    archon_sites.kill_pid(record.get("pid"))
                    cockpit_site.clear_record(args.state_dir)
                    record, spawned_at = {}, None
            elif spawned_at is not None and time.monotonic() - spawned_at < COCKPIT_APP_SPAWN_GRACE_SEC:
                pass  # just (re)spawned — let uvicorn finish importing before calling it down
            elif respawns >= COCKPIT_APP_MAX_RESPAWNS:
                pass  # gave up (logged once below); a merge or a manual recovery resets it
            else:
                archon_sites.kill_pid(record.get("pid"))  # clear a wedged one first, in case it's alive
                # The kill above is the only moment renaming this file is safe (the process that held
                # it open is now dead, or was never up) — see roll_closed's docstring for why a rename
                # attempted while it's still open can race and decline instead of raising.
                if log_rotation.needs_rotation(cockpit_log_path, COCKPIT_LOG_MAX_BYTES):
                    outcome = log_rotation.roll_closed(cockpit_log_path,
                                                       max_bytes=COCKPIT_LOG_MAX_BYTES,
                                                       keep=COCKPIT_LOG_KEEP)
                    if outcome.rolled:
                        log(f"• cockpit: rotated cockpit.log -> "
                            f"{os.path.basename(outcome.rolled_path)}")
                    elif outcome.reason:
                        log(f"! cockpit: log rotation skipped ({outcome.reason}) — still appending "
                            "to the current file")
                pid = cockpit_site.spawn_backend(REPO_ROOT, args.state_dir, args.cockpit_app_port,
                                                 args.cockpit_port, log)
                if pid is not None:
                    record = {"pid": pid, "rev": rev, "port": args.cockpit_app_port}
                    cockpit_site.save_record(args.state_dir, pid, rev, args.cockpit_app_port)
                    spawned_at = time.monotonic()
                    respawns += 1
                    log(f"• cockpit: (re)started on 127.0.0.1:{args.cockpit_app_port} pid={pid}")
                    if respawns >= COCKPIT_APP_MAX_RESPAWNS:
                        log(f"! cockpit: {respawns} revivals with no healthy response — GIVING UP "
                            f"(no more respawns until it recovers on its own, or the next merge). "
                            f"Check {os.path.join(args.state_dir, 'logs', cockpit_site.LOG_FILENAME)}.")
        except Exception as e:  # noqa: BLE001 — FAIL-OPEN: a flaky cockpit must never take the
            log(f"! cockpit reconcile error (continuing): {e}")  # daemon's chat/reminders down
        await _sleep_or_stop(state, COCKPIT_APP_INTERVAL)


async def _notify_cockpit_missing_deps(args, log) -> None:
    """One Telegram line per daemon boot when the `cockpit` extras aren't installed. Not rate-limited across boots and not repeated within one: the task returns straight
    after calling this, so 'once per boot' is structural rather than a counter to get wrong. Fail-open
    — an unreachable Telegram is logged, never raised."""
    if args.stub_send or not args.telegram_env:
        return
    text = ("The cockpit isn't running: its extras aren't installed in the daemon's venv. "
            "Fix with `uv sync --extra cockpit`, then `reseneschald`.")
    # **The first producer through the Mouth's queue** (mouth-spec.md phase 1). It goes first because
    # it is the safest thing to be wrong about: unsolicited (so it belongs in the queue by
    # definition), not a reminder (so §3.5's carve-out is not in play), not a reply (so the
    # poison-pill guard is not involved), and at most one per boot. Every other producer keeps its
    # direct path until migrated — §7's migration is explicitly producer-by-producer, and an
    # unmigrated one behaves exactly as it does today.
    #
    # What changes for the owner: nothing, on a healthy boot. What changes when Telegram is down: the
    # nudge now WAITS on the queue and goes out when it recovers, instead of being logged once and
    # lost. That is the whole point of the door.
    if mouth.enqueue(args.state_dir, surface="telegram", kind="nudge", text=text,
                     speaker="daemon") is None:
        # The queue is the only path now, so a failed enqueue must be loud rather than silent.
        log(f"! cockpit: deps-missing nudge could not be queued")


def _nudge_topics_toggle(args, log) -> None:
    """One Telegram line per boot if private-chat topics are off for the bot, and silence otherwise.

    Pickers buried in a day's chat are hard to scroll back to. `telegram_topics.py` puts them in
    their own thread, but **the feature is gated on a @BotFather Mini App toggle no PR can flip** and
    the switch is not in the classic `/mybots → Bot Settings` menu anyone would look in. So the choice
    is one nudge or a feature that silently never turns on; this is the nudge, and the module keeps it
    to one per process (`nudge_if_disabled`). `decisions` is the DEFAULT topic for every picker that
    is not a merge approval, so the switch buys a findable list of every open decision.

    Runs in a worker thread because `getMe` is a blocking HTTP call. Never raises — pr_watch_task's
    loop must start whatever this finds, and a nudge is never worth a sweep."""
    try:
        import telegram_send as tsend      # lazy: only pr_watch_task ever needs the Bot API here
        import telegram_topics as ttopics
        c = tsend.cfg(tsend.load_env(args.telegram_env))
        if not c.get("token") or not c.get("chat_id"):
            return  # no credentials on this host: nothing to detect and nobody to tell
        ttopics.nudge_if_disabled(c, args.state_dir, log=log)
    except Exception as e:  # noqa: BLE001 — FAIL-OPEN, like every other path in this feature
        log(f"! telegram topics check failed (continuing): {e}")


async def pr_watch_task(state: DaemonState, args, log) -> None:
    """The PR-watch task: **be the watcher that is always running.**

    The merge-approval picker should auto-send when a PR turns green, and `merge_guard.ask_on_green`
    is that decision in code. But a caller that runs only when somebody starts it (`watch_pr.py`)
    ships *"auto-send when a watcher happens to be running"* — PRs go green with nothing watching and
    produce no picker at all. This task removes the *somebody*. Mechanics, the watched set and
    the burst bounds live in `pr_sweep.py`; this is the loop.

    **It finds and asks. It cannot merge, approve, or record an approval** — everything it does goes
    through `merge_guard.ask_on_green`, which writes no approval on any path, and
    `merge_guard.record_approval` still has exactly one caller: `_merge_approval_clause`, on a real
    Telegram tap. A test asserts this function names neither.

    **AND IT IS NOT GREEN-ONLY: a PR whose CI has gone RED is told about too, once per
    `(repo, pr, head_sha)`, via `pr_red_notify.py`.** Otherwise a red PR simply falls out of
    `pr_sweep.candidates` (green-only by construction) with no picker AND no notification of any
    kind — the same gap this task closes for green, one arm over. **This is a NOTIFICATION,
    never a picker** — a red PR needs fixing, not approving, so it carries no option list and cannot
    reach `merge_guard.record_approval` by construction. Mechanics, the dedupe ledger and the burst
    bound live in `pr_red_notify.py` and `pr_sweep.py`; this is still just the loop.

    Degrades — no sweep this run, never a crash — when disabled (`--no-pr-watch`) or in test/offline
    modes. **The gating includes `--stub-send`, unlike its sibling tasks**, and that difference is
    load-bearing rather than cautious: the other tasks never send, while a sweep's whole purpose is to
    reach the owner's phone, and `telegram_ask.py` is a subprocess that has never heard of the flag. A
    stub run with a live `telegram.env` beside it would message the owner for real.

    Fail-open like its neighbours: `sweep` is contracted never to raise, and this catches anyway,
    because one supervised task may not be able to take the others down. Errors
    are logged **only when the reason changes**, so an unauthenticated `gh` costs one line rather
    than one every three minutes forever."""
    if getattr(args, "no_pr_watch", False) or args.stub_brain or args.stub_send \
            or args.fake_inbox is not None:
        return
    # ONE line per boot if private-chat topics are off, then never again this incarnation — the same
    # shape as the cockpit deps nudge above. It lives HERE rather than in
    # `telegram_ask.py` because that is a subprocess spawned per picker, where "once per boot" is
    # unspellable and every nudge would be another buzz.
    #
    # **RESIDUAL, NAMED RATHER THAN QUIETLY FIXED:** topics carry EVERY picker, not just the PR
    # ones, so this nudge has outgrown the task it is attached to — a daemon started with
    # `--no-pr-watch` says nothing about a toggle that would organise all open decisions. Relocating
    # it is a change to which task owns a send and needs its own reasoning, not a rider.
    await asyncio.to_thread(_nudge_topics_toggle, args, log)
    last_error = None  # dedupe consecutive identical failures; a repeated line is a muted line
    last_repair_error = None
    while not state.stop.is_set():
        try:
            report = await asyncio.to_thread(pr_sweep.sweep, state_dir=args.state_dir)
            # `retired` joins the two: a pass that quietly took a dead picker down is the pass most
            # worth a line, because nothing else about it is visible — an edited message raises no
            # notification, so without this the daemon does it in total silence. `notified`/
            # `red_deferred` likewise: a red-CI notice is exactly as invisible to this log otherwise,
            # and a log that only shows the green arm cannot answer "was the owner told it was red?"
            if report.get("asked") or report.get("deferred") or report.get("retired") \
                    or report.get("notified") or report.get("red_deferred"):
                log("• " + pr_sweep.summary_line(report))
            errors = "; ".join(str(e) for e in report.get("errors") or [])
            if errors and errors != last_error:
                log(f"! pr-watch: {errors}")
            last_error = errors or None
        except Exception as e:  # noqa: BLE001 — FAIL-OPEN: a flaky GitHub must never take the
            log(f"! pr-watch sweep error (continuing): {e}")  # daemon's chat/reminders down
        # AND REPAIR — after the asking and the retiring, never before (concurrent-pr-collisions
        # spec §5A.2 order: ask, retire, repair). `pr_repair.sweep` rebases a BEHIND PR server-side under
        # §5A.3's never-move-a-tapped-head invariant, and launches one worktree repair job per
        # (PR, base sha) for a CONFLICTING one whose conflict is confined to the §5.2 ledgers —
        # anything else gets one Telegram line instead. Its own try, so neither can cost the other.
        try:
            repair = await asyncio.to_thread(pr_repair.sweep, args.state_dir,
                                             claude_bin=getattr(args, "claude_bin", "claude"))
            if any(repair.get(k) for k in ("rebased", "launched", "noticed", "deferred")):
                log("• " + pr_repair.summary_line(repair))
            repair_errors = "; ".join(str(e) for e in repair.get("errors") or [])
            if repair_errors and repair_errors != last_repair_error:
                log(f"! pr-repair: {repair_errors}")
            last_repair_error = repair_errors or None
        except Exception as e:  # noqa: BLE001 — FAIL-OPEN, same reason as the sweep above
            log(f"! pr-repair pass error (continuing): {e}")
        await _sleep_or_stop(state, PR_WATCH_INTERVAL)


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

    # The conversation cache is per-topic, and the pre-topics single file is the main chat's
    # history. Migrated HERE, explicitly, once per boot, and BEFORE anything can append to a
    # cache: the alternative was to leave `telegram-thread.json` on disk while nothing read it, which
    # loses twenty turns of continuity and looks exactly like working. Never raises — see the docstring.
    migrate_legacy_thread(args.state_dir, log)

    # Reload the action queue a prior run left behind (messages taken off Telegram/Discord but not yet
    # answered). A restart — routine after each PR merge — must not drop them.
    dstate = load_daemon_state(args.state_dir)
    state.pending = [queue_item((i["channel"], i["text"], i.get("attempts", 0), i.get("topic")))
                     for i in dstate.get("pending", [])]
    if state.pending:
        log(f"• reloaded {len(state.pending)} pending message(s) from a prior run")
        state.pending_event.set()

    # Rescue whatever is still in the transcript ring buffer into the durable archive before the next
    # trim deletes it (transcript_archive.py). Everything that happened before that module deployed
    # exists ONLY in the ring, on a ~25-session horizon — this one pass is the difference between
    # starting the archive with that history and losing it. Idempotent, so running it on every boot
    # (and the daemon reboots on every merge) costs a scan and appends nothing the second time.
    # Fail-open: it never raises, and a 0 means "nothing to rescue", never "startup broke".
    try:
        rescued = transcript_archive.backfill_from_ring(args.state_dir)
        if rescued:
            log(f"• transcript archive: backfilled {rescued} event(s) from the ring buffer")
    except Exception as e:  # noqa: BLE001 — an archive hiccup must never block the daemon coming up
        log(f"! transcript archive backfill failed: {e}")

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
            _supervise("archon-sites", archon_sites_task(state, args, log), state, log),
            _supervise("cockpit-app", cockpit_app_task(state, args, log), state, log),
            _supervise("pr-watch", pr_watch_task(state, args, log), state, log),
        )
    finally:
        # Snapshot whatever's still unanswered so the next start resumes it (see load_daemon_state).
        save_daemon_state(args.state_dir, state.pending, getattr(state.session, "session_id", None))
        if state.pending:
            log(f"• saved {len(state.pending)} unanswered message(s) for the next start")
        if state.session is not None:
            log(f"• closing warm session on shutdown ({_session_vitals(state.session)})")
            # Resumable, and this is the case that matters most: a large share of session deaths are
            # Path A code reloads, which would otherwise kill a live conversation and re-ground from a
            # thread tail.
            _end_session(state, args, log, RESPAWN_SHUTDOWN)
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


def _respawn_detached(log, state_dir=None, telegram_env=None) -> bool:
    """Relaunch on the (freshly pulled) code, preferring the uv-managed venv interpreter when it
    exists so a graceful reload also migrates the daemon onto the venv (run-presence.cmd only picks
    the interpreter on a HARD start). Windows: do NOT os.execv under Task Scheduler — replacing the
    process image kills the daemon without a successor. Spawn a fresh, DETACHED
    instance — same argv + inherited env, so same flags and subscription billing — then exit; it
    acquires the released lock and takes over.

    Self crash-loop guard (secondary check). Before spawning, PROJECT the boot-attempts window as if
    this respawn had happened (prune + the imminent boot) and, if that would exceed the budget, refuse
    to spawn — the successor would only trip its own startup guard a moment later, so we stop the
    graceful-reload loop one iteration earlier and never launch the doomed child. The startup guard in
    main() is the primary net (every boot funnels through write_lock); this closes the specific gap the
    spec calls out — "the graceful-reload path respawns detached with no loop guard." We don't PERSIST
    the projected boot here: if we spawn, the child's write_lock records the real one; if we refuse,
    no boot happened. Fail-open: a guard error must never block a legitimate reload, so it proceeds to
    spawn on any exception."""
    if state_dir is not None:
        try:
            now = datetime.now(timezone.utc)
            projected = prune_boot_attempts(_load_boot_attempts(state_dir), now) + [now]
            if is_crashloop(projected):
                trip_crashloop_guard(state_dir, telegram_env, log, projected, now)
                log("crash-loop guard: refusing the graceful respawn (would loop); exiting instead.")
                return False
        except Exception as e:  # noqa: BLE001 — never let the guard block a legitimate reload
            log(f"! crash-loop guard respawn check errored (respawning anyway): {e}")
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
    p.add_argument("--codex-bin", default="codex",
                   help="path to the Codex CLI (docs/pluggable-backend-spec.md; only used when "
                        "state/model-config.json's backend is codex-cli). The daemon inherits a "
                        "pre-install PATH, so this defaults to the bare name like --claude-bin — pass "
                        "the full path (see CODEX_SETUP.md) until PATH is updated on the host.")
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
                   help="localhost port for the seneschald cockpit pipe (cockpit_task; "
                        f"default {cockpit_pipe.DEFAULT_PORT} — see seneschal/docs/cockpit-spec.md)")
    p.add_argument("--no-cockpit", action="store_true",
                   help="disable the cockpit pipe entirely (no daemon-side websocket server; the "
                        "cockpit backend falls back to the read-only state/* files + inbox file queue)")
    p.add_argument("--cockpit-app-port", type=int, default=cockpit_site.DEFAULT_PORT,
                   help="localhost port for the cockpit backend the daemon supervises "
                        "(cockpit_app_task; distinct from --cockpit-port, which is the pipe)")
    p.add_argument("--no-cockpit-app", action="store_true",
                   help="don't start/supervise the cockpit backend even if its extras "
                        "are installed")
    p.add_argument("--no-cockpit-web-build", action="store_true",
                   help="never run npm ci/build for cockpit/web — serve whatever dist is already "
                        "there (API-only if there is none)")
    p.add_argument("--archons-dir", default=os.path.join(REPO_ROOT, "archons"),
                   help="root scanned for archons/*/site.json (archon-site supervision, "
                        "archon_sites_task; default: the repo's archons/ dir)")
    p.add_argument("--no-archon-sites", action="store_true",
                   help="disable archon-site supervision even if archons/*/site.json exist (auto-on "
                        "otherwise — mirrors the discord.env/notion-mcp.json auto-detect convention)")
    p.add_argument("--no-pr-watch", action="store_true",
                   help="disable the resident PR watch (pr_watch_task, pr_sweep.py): stop "
                        "auto-sending the owner the merge-approval picker when a functionality PR goes "
                        "green. It never merges and never approves — turning it off only means the "
                        "owner has to be asked by hand again")
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
    p.add_argument("--no-jobs", action="store_true",
                   help="disable background-job reconcile + completion pushes (seneschal/scripts/jobs.py); "
                        "jobs already running keep running, they just go unreported until re-enabled")
    p.add_argument("--no-peek", action="store_true", help="disable the comms peek")
    # --- plan-meter telemetry (docs/usage-telemetry-spec.md) ---------------------------------------
    # ON by default at the spec's recommended cadence, unlike the peek. It is a read of a number
    # Anthropic already publishes: `/usage` renders client-side, so a reading costs zero tokens and
    # one session. It writes a row and says nothing, ever.
    p.add_argument("--usage-interval-min", type=int, default=USAGE_INTERVAL_MIN_DEFAULT,
                   help=f"plan-meter reading cadence in minutes (0 = cadence off, boundary readings "
                        f"only; default {USAGE_INTERVAL_MIN_DEFAULT}, the spec's §8.1 recommendation)")
    p.add_argument("--usage-model", default=usage_probe.DEFAULT_MODEL,
                   help="model pinned for the /usage probe (it renders client-side, so this is a "
                        "no-regression pin rather than a cost lever)")
    p.add_argument("--usage-timeout", type=int, default=usage_probe.DEFAULT_TIMEOUT_SEC,
                   help="hard wall-clock ceiling for one reading; past it the row is a `timeout`")
    p.add_argument("--usage-defer-grace-min", type=int, default=USAGE_DEFER_GRACE_MIN,
                   help="how long a due reading may wait behind a deferral gate before it is "
                        "abandoned AND RECORDED as skipped (never queued to fire late)")
    p.add_argument("--no-usage-reading", action="store_true",
                   help="disable plan-meter readings entirely (the series simply stops; nothing "
                        "carries forward across the gap)")
    p.add_argument("--no-usage-notice", action="store_true",
                   help="keep taking readings but never say the instrument broke (§8.3's branch B "
                        "— this is the off switch, not the default). The readings and their failure "
                        "rows are unaffected")
    p.add_argument("--no-outbox", action="store_true",
                   help="disable ALL Notion write-behind outbox tending (the supersession sweep, the "
                        "flush, and the backlog alarm; the outbox is Notion-backend-only anyway). "
                        "Entries stay durable — they just go unflushed and unwatched; for tests/offline runs")
    p.add_argument("--no-outbox-drain", action="store_true",
                   help="keep the pure-local sweep and the backlog alarm, but never spawn the headless "
                        "`claude -p` flush (leaves the flush to warm/scheduled turns — option (a) only)")
    p.add_argument("--outbox-stale-min", type=int, default=OUTBOX_STALE_SEC // 60,
                   help="how long the oldest un-landed outbox entry may wait for an ordinary turn to "
                        "flush it before the daemon spawns its own drain")
    p.add_argument("--outbox-live-stale-min", type=int, default=OUTBOX_LIVE_SESSION_STALE_SEC // 60,
                   help="the SECOND, LONGER wait applied instead of --outbox-stale-min while a /assistant "
                        "session is live (spec §9 (a‴)) — the session still gets first refusal, but "
                        "past this bound the daemon drains anyway rather than deferring forever")
    p.add_argument("--outbox-drain-interval-min", type=int, default=OUTBOX_DRAIN_INTERVAL_SEC // 60,
                   help="minimum minutes between spawned outbox drains (a wedged queue must not fork a "
                        "`claude` every tick)")
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
    p.add_argument("--interleave-mode", choices=list(interleave.MODES),
                   default=interleave.DEFAULT_MODE,
                   help="mid-turn interleave (seneschal/docs/mid-turn-interleave-spec.md, phase 0). "
                        "'observe' (default) runs the relevance gate on every message that arrives "
                        "while a turn is already in flight and logs what it WOULD have folded to "
                        "state/interleave-log.jsonl — ZERO behaviour change: nothing is interrupted, "
                        "nothing is reordered, no prompt is altered. 'off' disables it entirely. "
                        "'live' is phase 2 (interrupt-and-fold), an explicit opt-in that is REFUSED — "
                        "never silently downgraded — on a CLI version it was never probed against "
                        "(interleave.refuse_mode). Its own knob, independent of --router-mode.")
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
    parser = build_parser()
    args = parser.parse_args()

    # A refused `live` is loud rather than a silent downgrade to `observe` — a caller who believes a
    # live interleave is running when it is not is the one outcome worse than not starting.
    # `argparse`'s `choices` accepts it precisely so this sentence can be printed instead of "invalid
    # choice"; the predicate lives in interleave.py so it is testable without a parser
    # (docs/mid-turn-interleave-spec.md §5).
    refusal = interleave.refuse_mode(args.interleave_mode)
    if refusal:
        parser.error(refusal)

    os.makedirs(args.state_dir, exist_ok=True)

    # Optional durable log — the scheduled task doesn't capture stdout, so failures were invisible.
    # `RotatingAppendLog` (log-rotation-spec.md) replaces the old startup-only "check once, keep one
    # `.1` generation" logic: this process is the log's only writer, so it can close its own handle,
    # roll, and reopen safely at any point in its run, not just at boot — and it now keeps several
    # gzipped generations instead of one plain one.
    log_fh = None
    if args.log_file:
        log_fh = log_rotation.RotatingAppendLog(
            args.log_file, max_bytes=PRESENCE_LOG_MAX_BYTES, keep=PRESENCE_LOG_KEEP,
            on_event=lambda msg: print(f"[{local_now().isoformat(timespec='seconds')}] {msg}",
                                       flush=True))

    log_lock = threading.Lock()  # log() is called from worker threads too now (to_thread callables)

    def log(msg: str) -> None:
        # Local time so the log reads in the owner's timezone, not UTC. Internal reminder /
        # heartbeat math stays on UTC instants (that's correctly tz-safe) — only the display changes.
        line = f"[{local_now().isoformat(timespec='seconds')}] {msg}"
        with log_lock:
            print(line, flush=True)
            if log_fh:
                log_fh.write(line + "\n")  # never raises — see RotatingAppendLog's own contract

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

    # The digest retirement (docs/read-first-retirement-spec.md): once per boot, before anything else
    # touches READ FIRST, check whether the standing-safety store has proven itself yet — see
    # check_read_first_migration's own docstring for the latch/nudge mechanics (a fresh install with
    # no leftover digest is silent).
    check_read_first_migration(args.state_dir, args, log)

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

    # The cockpit backend is auto-on whenever its own dependency world is installed: having run
    # `uv sync --extra cockpit` IS the opt-in signal, the same way dropping discord.env is.
    if args.no_cockpit_app:
        log("Cockpit backend supervision disabled (--no-cockpit-app)")
    elif not cockpit_site.deps_available():
        log("! Cockpit backend not supervised — cockpit extras not installed (uv sync --extra cockpit)")
    else:
        _ui = ("prebuilt UI" if not cockpit_site.build_needed(REPO_ROOT)
               else "UI will be built" if (not args.no_cockpit_web_build
                                           and cockpit_site.npm_available(REPO_ROOT))
               else "API-only — no UI build")
        log(f"Cockpit backend supervised: 127.0.0.1:{args.cockpit_app_port} ({_ui})")

    # Auto-detect archon-site supervision like discord.env/notion-mcp.json: it's simply ON whenever
    # discovery finds a declared site — no flag needed. --no-archon-sites forces it off regardless.
    if args.no_archon_sites:
        log("Archon-site supervision disabled (--no-archon-sites)")
    else:
        _declared_sites = archon_sites.discover_sites(args.archons_dir)
        if _declared_sites:
            log(f"Archon-site supervision: {len(_declared_sites)} site(s) declared under "
                f"{args.archons_dir} ({', '.join(s.id for s in _declared_sites)})")
        else:
            log(f"Archon-site supervision: no archons/*/site.json found under {args.archons_dir} "
                "(nothing to supervise yet)")

    # The resident PR watch. Auto-on, because an opt-in flag an agent must remember is the same class
    # of failure as an agent having to remember to ask — which is the failure this whole feature
    # exists to remove (merge_guard.ask_on_green's docstring makes the identical argument about
    # watch_pr's default). Says the watched set at boot so it is never a mystery which repos the
    # owner will be asked about.
    if args.no_pr_watch:
        log("PR watch disabled (--no-pr-watch) — nothing will auto-send the merge picker")
    elif args.stub_brain or args.stub_send or args.fake_inbox is not None:
        log("PR watch off (offline/stub run — a sweep's whole job is to reach the owner's phone)")
    else:
        log(f"PR watch: {', '.join(pr_sweep.DEFAULT_REPOS)} every {PR_WATCH_INTERVAL}s "
            f"(asks on green, notifies on red; never merges, never approves)")

    stale_sec = max(args.poll_timeout * 3, 90)
    if lock_is_live(args.state_dir, stale_sec):
        log("presence already running (live lock) — exiting.")
        return 0
    boots = write_lock(args.state_dir)  # records this boot + returns the pruned crash-loop window

    # Self crash-loop guard (primary check). If we've booted too many times too fast, STOP — don't run,
    # don't respawn. Every relaunch mechanism funnels through here (write_lock above stamped the boot),
    # so this one check breaks a Task-Scheduler restart-on-failure loop, a watchdog-revive loop, AND a
    # graceful _respawn_detached loop alike. We exit 0 (not 1) deliberately: a loud exit(1) would invite
    # Task Scheduler's restart-on-failure to relaunch us straight back into the same crash. Fail-open —
    # a bug in the guard must never take a healthy daemon down, so any error here just proceeds to run.
    try:
        if is_crashloop(boots):
            trip_crashloop_guard(args.state_dir, args.telegram_env, log, boots)
            release_lock(args.state_dir)  # unambiguously down: let the watchdog see no live lock
            return 0
    except Exception as e:  # noqa: BLE001
        log(f"! crash-loop guard startup check errored (proceeding to run): {e}")

    fake_queue = []
    if args.fake_inbox is not None:
        loaded = load_json(args.fake_inbox, [])
        # Accept either [[msg,...], ...] (batches) or [msg, ...] (one per iteration).
        fake_queue = loaded if loaded and isinstance(loaded[0], list) else [[m] for m in loaded]

    def make_session() -> "WarmSession | StubWarmSession | CodexWarmSession":
        # Resolved FRESH at every spawn (not just process start): state/model-config.json's warm_model
        # wins over the --model CLI flag, which is the fallback — cockpit-spec.md v3 "Model dials".
        # Note this still applies on a RESUMED spawn (Step 2a): the CLI happily resumes onto a
        # different model, so a cockpit dial change lands at the next spawn exactly as it always did.
        # `backend` is the THIRD branch docs/pluggable-backend-spec.md §3.1/§3.5 Phase 2 adds — read
        # from the SAME config read, so a dial change and a backend change apply on the same schedule
        # (next natural respawn, or the cockpit's "apply now"). The live daemon's default stays
        # claude-cli until the owner flips it in the cockpit; nothing here changes that default.
        cfg = model_config.load(args.state_dir)
        backend = cfg.get("backend") or model_config.DEFAULT_BACKEND
        model = resolve_warm_model(args.state_dir, args.model, log, backend=backend)
        if args.stub_brain:
            sess = StubWarmSession(log=log)
        elif backend == "codex-cli":
            # No spawn-fallback floor yet (see CodexWarmSession's own docstring: codex's
            # spawn-failure shapes are unverified, spec §2.1) — fallback_model=None makes the rung-2
            # ladder a correct no-op rather than guessing at a floor model.
            sess = CodexWarmSession(
                args.codex_bin, model, args.permission_mode, log,
                mcp_configs=active_mcp_configs(args), fallback_model=None)
        else:
            # Spawn-fallback floor (see WarmSession): the launcher's --model (claude-opus-4-8) is the
            # known-good model to drop to if `model` won't spawn; a bare default keeps the net armed
            # even if --model was omitted. resolve_warm_model already canonicalized `model`; do the
            # same here so the "same model, nothing to fall back to" short-circuit compares like for
            # like.
            fallback = (model_config.canonical(args.model or "", backend="claude-cli") or args.model
                       or DEFAULT_WARM_FALLBACK_MODEL)
            sess = WarmSession(
                args.claude_bin, model, args.permission_mode, log,
                mcp_configs=active_mcp_configs(args), fallback_model=fallback)
        # session-trace phase 0: close the decision→opened pairing the moment the CLI reports an id.
        # Injected here rather than imported inside WarmSession so the class stays constructible
        # without a state dir, and so this can never be why a session fails to come up.
        sess._on_opened = lambda sid: _session_opened(args, sid)
        return sess

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
            log_fh.close()
        _respawn_detached(log, args.state_dir, args.telegram_env)
        return 0
    if log_fh:
        log_fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
