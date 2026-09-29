# Asyncio reactive daemon — design

**Status:** `BUILT` — approved direction; **built and live — `main_async()` supervises NINE tasks.**
Phases 0-2 shipped the first five; the **sixth** (cockpit pipe), the **seventh** (archon-site
supervision), the **eighth** (cockpit supervision) and the **ninth** (the resident PR watch,
`pr_sweep.py`) followed. Each is named in the Architecture table below and detailed in "The loop as
deployed". The phase-3+ items at the end are deferred by decision, not backlog · **Scope:**
`seneschal/scripts/presence.py` and its launcher/update plumbing. Companion implementation plan:
[asyncio-daemon-plan.md](asyncio-daemon-plan.md). The sixth task's own protocol module is
`seneschal/scripts/cockpit_pipe.py`; its design doc is [cockpit-spec.md](cockpit-spec.md)'s "The daemon
pipe" section — this file covers where it plugs into the reactive core.

## Why

`presence.py` before this design was a single-threaded loop whose heartbeat was the Telegram long-poll
(`--poll-timeout`, 25 s). Everything else rode that clock, and everything blocked everything:

1. **A chat turn blocks the world.** `WarmSession.send()` is synchronous — while Claude works a turn
   (often minutes of tool calls), no reminders fire, no Telegram/Discord poll runs, no control request is
   seen. A due nudge waits for the conversation to finish; given how much fire-time precision matters
   (the stagger policy), that is a real defect, not a theoretical one.
2. **Inbound waits on inbound.** A second message sent mid-turn isn't even read off the wire until the
   turn ends. "Wait, cancel that" can't be seen in time.
3. **Discord latency = poll cadence.** `discord_poll.py` REST-polls once per loop wake. The Gateway
   websocket pushes events the moment they happen — but a Gateway connection is *persistent* (heartbeat
   ~41 s, resume state); it cannot be "polled once per loop." It needs concurrency the old daemon didn't
   have.
4. **The future is persistent connections.** Home Assistant (the smart-home plan) is websocket-native;
   Signal and the health feed are on the horizon. Under the old model each is another poll bolted to
   a loop paced by Telegram's timeout. Under asyncio each is just a task.

The original recommendation ("gateway in a background thread; a full asyncio rewrite is a big risky bet
— earn it later") was made under the **stdlib-only constraint**. That constraint was explicitly dropped
by the owner, provided the merge → ff-pull → graceful-restart flow stays fully automatic. With deps
unlocked, the thread option loses its main advantage, and going straight to asyncio avoids paying the
migration cost twice.

## Non-goals

- **No framework adoption** (no `discord.py`, no `aiohttp`). Two small dependencies — `websockets`, for
  the one thing stdlib truly can't do, and `tzdata` (added later, pure IANA timezone *data* with no code:
  Windows ships none and `zoneinfo` needs it so `tz_common` can resolve the owner's configured timezone).
  Everything else stays stdlib.
- **No rewrite of the channel helper library.** `sentinel.py`'s helpers (`send_telegram`, `poll_telegram`,
  `send_discord`, `send_call`, `check_reminders`, …) are subprocess-based, battle-tested, and stay
  byte-identical. The async core calls them via `asyncio.to_thread`.
- **No SDK runner.** Billing model unchanged: the warm session is the `claude` CLI
  (subscription); `ANTHROPIC_API_KEY` stays scrubbed from every child env.
- **No behavior change to reminders policy, quiet windows, ack gating, or the act-low/ask-high gate.**

## Architecture

One process, one asyncio event loop (`asyncio.run`, Windows Proactor), a small set of long-lived tasks
supervised by `main_async()`. All blocking I/O (the subprocess-based helpers, file writes) hops through
`asyncio.to_thread`; all shared state is mutated only on the event loop, so the prior design's freedom
from locks is preserved.

| Task | Owns | Replaces |
|---|---|---|
| `telegram_task` | Telegram long-poll (`poll_telegram` in a thread), enqueue inbound to the durable action queue (folding an `edited_message` into the entry it corrects), thread-append, Router shadow-classify | the loop's blocking heartbeat |
| `discord_task` | Gateway websocket (identify → heartbeat → `MESSAGE_CREATE` → resume), REST `after=` catch-up on every (re)connect; **falls back to REST cadence-polling** if `websockets` is unavailable | once-per-loop `poll_discord` |
| `drainer_task` | The warm session lifecycle + draining the durable queue front-to-back, one turn at a time; idle wind-down | the inline drain loop |
| `scheduler_task` | ~5 s tick: lock heartbeat, `reap_finished_slots`, **`_reconcile_jobs`** (background-job terminal detection + completion pushes — `jobs.py`, `background-jobs-spec.md`), `check_reminders`, roll refill, `maybe_peek`, `maybe_run_slots`, **`_tend_outbox`** (Notion backend only: the write-behind outbox's supersession sweep + gated headless flush + backlog alarm — `notion-write-behind-outbox-spec.md` §9(a′); a tick *duty*, not a supervised task of its own, because it is the same "did what we started actually finish?" work as the slot reap and the job reconcile), drain the cockpit inbox-fallback file | the per-loop cadence work |
| `control_task` | control queue + legacy `restart.request` sentinel; initiates graceful quiesce → detached-respawn restart / shutdown | the end-of-loop control block |
| `cockpit_task` | the **Seneschal Cockpit pipe** (v2) — a localhost-only, token-authed WebSocket server (`seneschal/scripts/cockpit_pipe.py`) serving exactly one client (the cockpit backend); relays `chat.send`/`status.get`/`control.restart` in, streams `chat.event`/`status` out | nothing — net-new surface |
| `archon_sites_task` | **archon UI supervision** (`seneschal/scripts/archon_sites.py`) — a ~20 s reconcile loop over every archon's self-declared `site.json`: health-check over HTTP, (re)spawn what's down or wedged, honor the cockpit's per-site Restart control. Detached survivors + a PID file, so a reload adopts rather than duplicates | nothing — net-new surface |
| `cockpit_app_task` | **cockpit supervision** (`seneschal/scripts/cockpit_site.py`) — the same treatment for the cockpit backend itself: health-check `GET /api/health` on :8760, (re)spawn detached, build `cockpit/web/dist` when stale, bounce it when the checkout's git HEAD no longer matches the rev it was spawned at, and bounce-and-rotate when its log grows oversized ([log-rotation-spec.md](log-rotation-spec.md)). Auto-on when the `cockpit` extras are importable | nothing — net-new surface |
| `pr_watch_task` | **the resident PR watch** (`seneschal/scripts/pr_sweep.py`) — the NINTH task: every ~3 min, list each watched repo's open PRs, keep the terminally-green non-drafts, hand them to `merge_guard.ask_on_green`. **It cannot merge, approve, or write an approval record.** The same pass then runs `pr_repair.py`, in its own `try`: it rebases a BEHIND PR server-side (never a head with a live approval or pending picker) and launches one worktree repair job per `(PR, base sha)` for a CONFLICTING one whose conflict is confined to the two JSON ledgers and Markdown files (both sides kept). `concurrent-pr-collisions-spec.md` §5A.9. Details, the burst bounds and the gap it closes: "The resident PR watch" below | nothing — net-new surface |

**Reactive wins fall out immediately:** reminders fire within ~5 s of due *even mid-chat-turn*
(scheduler no longer waits on the long-poll or the drain loop); inbound is read continuously; Discord
becomes push. Queueing a message received mid-turn behind the in-flight turn is unchanged — the drainer
is the *sole* consumer, so chat turns stay strictly serialized by construction.

### Shared state

A single `DaemonState` object (plain class) holds what the loop's locals held before: `pending` (the
durable action queue), `session`, `last_activity`, `headless_children`, `slot_children`, `slot_retries`,
a `stop` event, and a `control_pending` flag. It is only touched from coroutines (never from inside
`to_thread` callables), which is the concurrency rule that keeps this lock-free.

### Invariants that MUST survive the port (each encodes a production incident)

1. **Durable action queue.** A message is enqueued (and persisted to `presence-state.json`) the moment
   it's consumed off the wire, and popped only after its reply is *delivered*. A restart never drops a
   message it had taken.
2. **Poison-pill guard.** The turn-attempt counter increments and persists *before* the risky send;
   `MAX_TURN_ATTEMPTS` dead-letters a message that keeps killing turns (the "reseneschald self-kill"
   class: a warm-session message that kills the daemon mid-turn never delivers, so without the guard it
   replays from the durable queue forever).
3. **Transient-send rollback.** A produced-but-undeliverable reply rolls the attempt counter back — a
   Telegram outage must never dead-letter a good message — and drops the warm session so the retry
   re-grounds from the thread tail (no phantom replies in continuity).
4. **Delivery-gated continuity.** `append_thread(assistant, …)` only after `deliver_reply` confirms.
5. **Slot stamping on exit-0 only**, with `slot_retries`/`SLOT_MAX_RETRIES` and the catch-up window as
   the two backstops (a crashed morning run retries; a broken one can't respawn forever). **A failed
   exit also gets a WAIT before that retry** — `slot_hold_until`, set by `reap_finished_slots`: a
   reset-time hold when the slot's own captured output (`slot-logs/<name>.log`, `state/README.md`)
   names a `claude` usage-limit reset, else exponential backoff (`slot_backoff_seconds`, capped well
   inside `--slot-catchup-min` so the two backstops above still bound the ladder). Without it, a failed
   slot relaunched on the very next ~5 s tick with no wait at all, so `SLOT_MAX_RETRIES` burned in
   under two minutes against a usage outage lasting hours — and the night's Dream run was lost to it.
6. **Notion read-burst serialization.** At most one headless reader at a time, and none while a chat
   turn is mid-flight; chat is priority and is never delayed. The `warm_session_busy` /
   `heavy_run_in_flight` gates keep their exact semantics (the drainer exposes "mid-turn" state).
7. **Graceful restart, never mid-exchange.** Controls with `defer_until_idle` apply only when the session
   is down and the queue empty; a pending control shortens the wind-down to `CONTROL_PENDING_IDLE_SEC`.
   On Windows the restart **spawns a detached successor and exits** (never `os.execv` — see the
   incident note in the code); that mechanism is kept verbatim. **"Drain, don't kill": a
   restart/shutdown ALSO holds while `state.slot_children` is non-empty** — additional to the chat-idle
   gate above, not a replacement, and bounded on its own cap (`SLOT_DRAIN_MAX_HOLD_SEC`, 20 min)
   independent of `CONTROL_MAX_HOLD_SEC`. A PR merging mid-slot used to kill the child outright:
   `reap_finished_slots` only stamps a slot done on a clean exit, so the relaunch found the journal
   steward's already-cleared journal and no-op'd, silently losing the rest of its phases. Never applies
   to a `jobs.py --lease` — that gate is deliberately unchanged, so a job still can never block a
   Path-A deploy.
8. **Billing safety.** Every child env is scrubbed of `ANTHROPIC_API_KEY`; the `apiKeySource` warning on
   session init stays.

### The warm session under asyncio

`WarmSession` keeps its synchronous `subprocess.Popen` + line-reader implementation (proven, including
the UTF-8 forcing); the drainer calls `send()` via `asyncio.to_thread`. The event loop stays free while
a turn runs — that thread is parked on the child's stdout, which is exactly what threads are for. A
native `asyncio.subprocess` port is possible later but buys nothing today and risks re-learning Windows
pipe quirks. (`WarmSession` and its offline twin `StubWarmSession` live in the claude-cli backend,
`seneschal/scripts/backends/`, since the warm session became pluggable —
[pluggable-backend-spec.md](pluggable-backend-spec.md). The drainer talks to whichever backend the
cockpit's `backend` dial selected; everything in this section holds for each.)

**The turn is bounded ([hung-turn-deadline-spec.md](hung-turn-deadline-spec.md)).** That parked thread
had no deadline of any kind, and a child that emitted nothing and never closed held it forever —
`session_busy` stuck on, the message pinned at `pending[0]`, and `MAX_TURN_ATTEMPTS` advancing **once
per daemon boot**, so the same prompt replayed on every restart until three of them dead-lettered it.
`_TurnWatchdog` now kills the child on an idle gap, producing the closed-stdout condition the read loop
already handles, so a timeout arrives at the drainer as the mid-turn-death outcome it already knows how
to serve rather than as a new branch.

**The deadline is a GAP BETWEEN STREAM EVENTS, never a cap on the turn** — `TURN_IDLE_GAP_SEC`, 600 s —
because a legitimate turn runs long all the time (tool use, a big read, a Fable delegation) and a total
cap would kill exactly those. **A hang is not slow; it is silent.** Two consequences worth carrying: a
hung turn is the ONE delivered outcome that does *not* pop the durable queue (nothing was answered, so
the attempt stands and the poison pill becomes reachable within a single boot — the delivery-failure
rollback in invariant 3 above is a different case and is untouched), and `CONTROL_MAX_HOLD_SEC` now
bounds invariant 7's `defer_until_idle` hold, because one wedged turn made all three of `chat_idle()`'s
conditions false forever and that is Path A silently not deploying.

### Session registry (defer noise into an engaged chat; see who's working on what)

A multi-session registry, `state/sessions/<id>.json` (`{session_id, pid, source, started_at, last_seen,
working_on, cwd, branch, phase}`; helpers in `sentinel.py`: `write_session_heartbeat` /
`clear_session_heartbeat` / `session_is_live` / `list_live_sessions` / `prune_sessions`), lets the daemon
know when an interactive `/assistant` chat is actually engaged — and lets every session see **who else is
live and what each is doing**. Phase 1 was a single `seneschal-session.json` heartbeat; phase 2 promoted
it to one entry per session (the legacy file is still *read* as a gate fallback, never written).
Delivery semantics mirror the quiet-window state/helper/gate shape.

- **Writers.** The `drainer_task` calls `write_session_heartbeat(state_dir, "daemon", …)` around every
  warm Telegram/Discord turn (refresh; entry `sessions/daemon.json` with a `working_on`) and
  `clear_session_heartbeat` on idle wind-down (and `main_async`'s finally on shutdown). A desktop
  `/assistant` session writes `source="desktop"` via `session_heartbeat.py` — best-effort only, since a
  slash command is a prompt (a closed desktop session just ages out of the TTL). Every **other** Claude
  Code session on the machine is stamped `source="build"` by the global `session_stamp.py` hook
  (`~/.claude/settings.json`: SessionStart/UserPromptSubmit/Stop write, SessionEnd clears) with
  `working_on` = dir @ git branch. Crash orphans self-prune after `SESSION_PRUNE_SEC` (24 h).
- **Gate vs awareness.** Only `GATING_SOURCES` (`daemon`/`desktop`) fresh within `SESSION_TTL_SEC`
  (120 s) make `session_is_live` true — a `build`/`scheduled` entry is **awareness-only**
  (`list_live_sessions`, 1 h window): it tells other sessions what's in flight (don't touch the tree
  another session is editing) but never silences a reminder — the owner is coding, not conversing.
- **Chokepoint 1 — reminder fire.** `check_reminders` reads `session_is_live` once per pass and **holds**
  (defer, never drop — stamps nothing → re-checked next ~5 s tick) each due **non-piercing** nudge while a
  session is live; it fires naturally once the session ages out. Piercing items (`Call Me` / Critical,
  `entry_pierces_quiet` — the **same** pierce set as quiet, not duplicated) fire immediately. Composes
  cleanly with the quiet gate (piercing still pierces both).
  **This gate still drops nothing itself, but a long hold no longer guarantees eventual delivery.**
  The **staleness cutoff** sits immediately after it, so a non-piercing nudge held past 2 h is consumed
  rather than dripping out overnight — a multi-hour session hold is exactly what once built a
  two-dozen-entry queue whose tail landed in the small hours. The **presence** gate is the deliberate
  contrast: its held time is accumulated on the entry and netted out of lateness, so it keeps NO DROPS
  in full. Full gate order — `acked → quiet → curfew → presence → live-session → staleness → stagger` —
  and why each position is load-bearing: `seneschal/references/reminders-policy.md` → "Night curfew +
  staleness cutoff". The scheduler tick also **captures** `check_reminders`' return value and logs one
  `presence.log` line per suppressed nudge, which is the observability half of that fix.
- **Chokepoint 2 — Watch peek.** `maybe_peek` skips the cheap comms-peek cycle while a session is live (a
  human is already engaged); it resumes on the next cadence.
- **Fail-open-SAFE.** Absent / malformed / stale entries → not live, so the signal's absence can never
  block a reminder. Policy: `seneschal/references/reminders-policy.md` → "Live-session defer"; state
  schema: `seneschal/state/README.md`.

### Discord gateway (`discord_gateway.py`)

- `wss://gateway.discord.gg/?v=10&encoding=json`; IDENTIFY with intents `GUILD_MESSAGES |
  MESSAGE_CONTENT` (Message Content is a privileged intent — see DISCORD_SETUP.md).
- Heartbeat per HELLO's interval; RESUME with `session_id` + last `seq` on reconnect; full re-IDENTIFY
  when RESUME is rejected. Exponential backoff on repeated connect failures.
- Every (re)connect first runs one REST `after=<offset>` catch-up (the existing `discord_poll.py` via
  `to_thread`) so messages that arrived while disconnected are never lost — the gateway's resume window
  alone does not guarantee that, and "a restart never drops a message" must hold per invariant 1.
- Gateway-received message ids also advance `state/discord-offset`, keeping REST catch-up consistent.
- Filters preserved from `discord_poll.py`: watched channel only, no bots (no self-echo), author
  allowlist honored.
- **Fallback:** if `import websockets` fails (venv missing/stale — e.g. the running interpreter predates
  the dep sync), the task logs loudly and runs the legacy REST poll on a ~10 s cadence instead. The
  daemon never hard-requires the dependency; degraded means slower Discord, not a dead daemon.
- `discord.env` is auto-detected like `notion-mcp.json` (with `--no-discord` to force off). If it does
  not exist on the host, this ships dark and activates when the owner wires the env file.

### Cockpit pipe (`cockpit_pipe.py` — v2)

The Seneschal Cockpit's window onto the one conversation: `seneschal/docs/cockpit-spec.md` → "The daemon
pipe". A localhost-only WebSocket server, `websockets.serve` (the same sanctioned dependency the Discord
gateway uses) bound to `127.0.0.1:<--cockpit-port>` (default `8471`).

- **Protocol module split, mirroring `discord_gateway.py`.** `cockpit_pipe.py` is import-safe without
  `websockets` (frame dataclasses/constants, encode/decode, the transcript ring buffer, the token file,
  the inbox-fallback drain, and `PipeHub` — which is duck-typed against whatever `ws` object it's handed,
  so its whole lifecycle is unit-testable with a plain fake and needs no live socket at all). Only
  `run_pipe_server` (which actually binds the listening socket) needs the real library; `cockpit_task`
  (presence.py) checks `pipe_available()` first and disables the pipe rather than dying when it's
  missing — degraded means no cockpit this run, never a dead daemon.
- **Token auth.** The first message on every connection MUST be `{"type":"auth","token":"..."}` matching
  `state/cockpit-pipe-token` (a plain-text file the daemon auto-generates with `secrets.token_hex(32)` on
  first run if missing — `cockpit_pipe.ensure_pipe_token`). Anything else within a 10 s window, or a
  wrong token, gets an `auth.error` and is closed (code 4001).
- **Single-client discipline.** The daemon serves exactly ONE authenticated pipe client — the cockpit
  backend, which itself fans out to N browser tabs. A newer authenticated connection replaces (closes,
  code 4000) whatever was connected before; `PipeHub` never grows bookkeeping beyond "the current
  client." This mirrors the "process-split" rationale in the spec: the pipe endpoint stays in-process
  because the warm session it fronts does, but multiplexing to many *browser* consumers is explicitly
  the cockpit backend's job, not the daemon's.
- **Frames** (JSON objects, a `type` field): inbound `chat.send` (→ the SAME durable action queue
  Telegram/Discord use, `source="cockpit"`; acked with `chat.ack`), inbound `control.restart` (→ the
  same `request_control.enqueue_control` path `request_restart.py`/`seneschald-control.ps1` use — a
  graceful, deferred restart; acked with `control.ack`), inbound/outbound `status`/`status.get`
  (turn-in-flight, warm-session up/down, current model, inbound queue depth — `_status_snapshot`),
  outbound `chat.event` (the transcript stream — `turn_started` / `assistant_output` / `tool_use` /
  `turn_done`, each carrying a `turn_id` that correlates the four across one turn, since the protocol
  has no other correlator).
- **Transcript tee.** Wherever the daemon reads the warm session's stream-json output
  (`WarmSession._read_until_result`'s `on_event` hook, wired up per-turn by `_make_stream_tee`), each
  digestible event is teed to BOTH the connected pipe client (best-effort) AND a ring buffer,
  `state/warm-transcript.jsonl` (`cockpit_pipe.append_transcript_event` — capped at ~2000 events,
  rewriting the tail once it grows past the cap + slack) so the cockpit can backfill after a reconnect
  (`GET /api/transcript`). This applies uniformly to Telegram/Discord/cockpit turns alike — the cockpit
  is just a third `channel`, and `deliver_reply`'s `"cockpit"` branch always "delivers" successfully
  (there's no external send; the transcript stream + ring buffer already covered it, so it's never
  lossy even if no client happened to be attached mid-turn).
- **Fail-open is non-negotiable, everywhere.** Every pipe/tee/ring-buffer call site
  (`_tee_chat_event[_threadsafe]`, `_push_cockpit_status`) wraps its cockpit-facing work in a broad
  `try/except` that logs and continues — a wedged/absent cockpit must never affect the chat loop or
  reminder firing. `PipeHub.broadcast` (and its `broadcast_threadsafe` sibling, used from the worker
  thread reading the warm session's stdout via `loop.call_soon_threadsafe`) is a **bounded queue,
  drop-oldest-with-counter, NEVER block** — a slow/dead pipe client can never backpressure the daemon.
- **Fallback file queue.** When the pipe is down, the cockpit backend appends `{"id","text","ts"}` lines
  to `state/cockpit-inbox.jsonl`; `scheduler_task` drains it every tick (`_drain_cockpit_inbox` →
  `cockpit_pipe.drain_inbox`, deduped by `id` against a small persisted seen-ledger) into the same
  action queue — degraded to ~`--tick-sec` latency, never lossy.
- **`--cockpit-port` / `--no-cockpit`** are the daemon's own knobs (default port `8471`); test/offline
  modes (`--stub-brain`/`--fake-inbox`) skip the task entirely, matching `control_task`'s gating, so no
  test ever opens a real socket.

### Model dials & Fable delegation (v3)

Full design + the owner's rulings: `cockpit-spec.md` → "Model dials & Fable delegation". This section is
just the daemon-side flow, for readers of this file.

- **Two dials, `state/model-config.json`** (`model_config.py`, its own module — reads tolerant, writes
  strict): `warm_model` (read at warm-session SPAWN — `resolve_warm_model`, called fresh by
  `main()`'s `make_session()` closure every time the drainer spawns a session, not just at process
  start — wins over the `--model` CLI flag/`run-presence.cmd`, which becomes the fallback) and
  `max_routable_model` (the ceiling on every Fable delegation, re-read live per inbound turn).
- **Force-route is a pure text transform, no queue-schema change.** `_enqueue_inbound` runs
  `apply_force_route` on every inbound item BEFORE persisting: a leading `!fable` (any channel,
  including a cockpit `chat.send` with `force_fable: true` — `cockpit_task.on_chat_send` synthesizes
  the same `!fable` prefix so one detection path covers all three channels) is stripped and replaced
  with an explicit MUST-delegate directive baked directly into the same text that gets threaded/
  persisted/retried — durable across a restart for free, exactly like the existing attachment/reaction
  text-synthesis precedent (`telegram_inbound_text`).
- **An inbound EDIT is the one thing that rewrites a queue entry after it is persisted**
  (`apply_inbound_edit`, [telegram-inbound-spec.md](telegram-inbound-spec.md) §6a). An
  `edited_message` whose original is still queued and un-answered replaces that entry's text in place
  rather than enqueuing a second item; anything else arrives as its own annotated inbound. Three
  properties make it safe to let it near the durable queue at all: it runs **synchronously** on the
  event loop, so it cannot interleave with the drainer mid-decision; it matches on the
  **post-transform** text (the force-route bullet above is why — the queue holds the directive, not the
  raw `!fable` line); and it **refuses the in-flight head**, because `drainer_task` claims `pending[0]`
  into local variables and pops index 0 *by position* on delivery, so rewriting it would answer the old
  text and discard the correction. `state.inflight_text` carries that claim — set where the head is
  taken, cleared at the top of every drainer iteration, so no exit path has to remember to.
  Deliberately keyed on text rather than `session_busy`: the drainer awaits a session spawn between
  claiming the head and setting that flag.
- **The router's fable arm** (`presence.fable_arm_classify`, `router.classify_fable`) runs in the SAME
  post-persist classify loop as the pre-existing triage shadow-classify, gated on the LIVE ceiling
  (`model_config.admits_fable`) — it never calls Ollama at all when the ceiling isn't Fable-tier. A
  `"fable"` verdict queues a hint LINE onto `DaemonState.fable_hints` (a plain in-memory FIFO list, NOT
  persisted); `drainer_task` best-effort-pops one hint per turn and prepends it to a LOCAL prompt-only
  variable (never written back to `state.pending[0]`, so a retry never duplicates or staleness-carries
  a hint meant for the first attempt). Losing the race (classification not done before the drainer
  builds the prompt) just means that turn proceeds hint-free — advisory only, never a correctness
  requirement, since the warm session's own judgment and force-route are independent triggers.
- **The delegate itself, `fable_delegate.py`, is a standalone script** the warm session invokes via its
  own tool use (a subprocess one-shot, never a session handoff) — it re-reads `model-config.json` and
  refuses (exit 2) on its own if the ceiling doesn't admit Fable, belt-and-braces with the arm-gating
  above. It scrubs `ANTHROPIC_API_KEY` from the child env (the same subscription-billing rule as the
  warm session and `mini_dream.py`), seeds a budget-bounded tail of the main-chat conversation cache (`telegram-threads/main.json`,
  legacy `telegram-thread.json` as a second rung), meters its own spend (`--output-format json` → the
  `usage` block on its `fable_oneshot` ledger row, or `metered: "unavailable"`, never a zero), and
  best-effort badges its answer into the cockpit transcript (`model: "claude-fable-5"`) so the seam
  stays visible.
- **Status/transcript honesty:** `_status_snapshot`'s `model` field prefers the live session's own
  model (set at spawn) and, when idle, falls back to a fresh read of `model-config.json`'s `warm_model`
  before the `--model` CLI default — so the cockpit's dial display stays honest even between turns.

## Dependencies without breaking Path A

The repo gains `pyproject.toml` + `uv.lock` (uv on the host) with the sanctioned runtime dependencies —
`websockets` at this design's writing, joined later by `tzdata` (see Non-goals). `.venv/` is gitignored.

- **`seneschald-control.ps1 -Action Update`** (the 10-min merge detector) gains one step: after a
  successful `git pull --ff-only`, run `uv sync --frozen`; request the graceful restart **only if the
  sync succeeded**. On sync failure: log loudly to `seneschald-update.log`, leave a
  `state/pending-restart` marker, and *don't* restart — the running daemon keeps executing its old
  in-memory code, which matches its old deps. Every Update run retries the sync while the marker exists,
  and requests the deferred restart once it succeeds. Merge-a-PR-and-walk-away stays exactly as automatic
  as before; the failure mode is "keeps running old code + loud log," never "half-updated."
- **`run-presence.cmd`** prefers `.venv\Scripts\python.exe` when it exists, falling back to system
  `python`. Combined with the gateway fallback above, every combination of (old/new code × old/new
  interpreter × synced/unsynced venv) yields a *working* daemon.
- The daemon's self-restart re-execs `sys.executable` (the venv interpreter once launched from it), so
  graceful reloads keep the venv without touching the launcher again.

## Testing & rollout safety

- The existing stdlib `unittest` tests pass **unchanged** — the port keeps every module-level helper
  (`save/load_daemon_state`, `warm_session_busy`, `maybe_peek`, `maybe_run_slots`, `reap_finished_slots`,
  `classify_slots`, `shadow_classify`, …) with identical signatures. Only `main()`'s loop becomes the
  supervisor + tasks.
- New tests: drainer turn-lifecycle (deliver/fail/dead-letter paths), control quiesce, gateway event
  parsing + resume bookkeeping (pure functions, no network), fallback selection.
- **v2 (cockpit pipe) tests**, same discipline — no live socket, no real daemon: `test_cockpit_pipe.py`
  (protocol/ring-buffer/token/inbox + `PipeHub`'s whole lifecycle against a duck-typed fake connection)
  and `test_presence_cockpit.py` (presence.py's wiring — status snapshots, the tee, the inbox drain,
  `cockpit_task`'s callback wiring against a stubbed `run_pipe_server`, and a full offline drainer-turn
  test asserting `turn_started`/`turn_done` share one `turn_id`). The cockpit backend's own suite
  (`cockpit/server/test_pipe_client.py`, `test_ws_hub.py`, `test_ws_endpoint.py`, …) mirrors this on its
  side of the wire — see `cockpit/README.md`.
- **v3 (model dials + Fable delegation) tests**: `test_model_config.py` (the rank/alias table, tolerant
  load, strict save), `test_router.py` (`classify_fable`'s standard/fable verdicts, ceiling-agnostic —
  gating is the CALLER's job), `test_presence_fable.py` (`resolve_warm_model` precedence,
  `strip_force_fable`/`apply_force_route`, `fable_arm_classify`'s ceiling gate + logging,
  `_enqueue_inbound` wiring both in end-to-end, and `drainer_task` best-effort-attaching a pending hint
  to the next prompt), and `test_fable_delegate.py` (ceiling refusal, thread-seed budgeting, transcript
  badging — all against an injected fake runner, no real `claude` spawn or network).
- CI runs the whole suite (the unittest step in `ci.yml`) plus a `uv lock --check` consistency gate.
- The offline harness (`--stub-brain --fake-inbox`) is preserved and gains `--stub-send` so an offline
  run can never message the real Telegram (a lesson already learned once).
- **Every phase merges only on green CI and deploys via Path A's graceful reload**; after each merge the
  daemon's `presence.lock` heartbeat, `presence.log`, and `seneschald-update.log` are verified on the
  host. Rollback = revert the merge commit; the next Update cycle rolls the daemon back just as
  automatically.

## Explicitly deferred (phase 3+, not built now)

- **Exact-time reminder timers** (sleep-until-next-due instead of the 5 s tick) — the tick already beats
  the old worst case by minutes; a timer wheel is polish.
- **Mid-turn interleaving** — BUILT; the authority is [`mid-turn-interleave-spec.md`](mid-turn-interleave-spec.md).
  The CLI offers no in-turn injection point (a mid-turn stdin write runs as the next turn), so an
  interleave is necessarily *interrupt-then-continue*. The motivating case is *additive context*
  ("oh, and also X"), not "cancel that"; the serialization invariant survives it (one consumer, one
  turn in flight, one reply per delivery). The daemon ships in `observe`; `live` is an explicit opt-in.
- **Native `asyncio.subprocess` warm session**, HA/Signal/health-listener tasks — the substrate is ready
  for them; they are their own projects.

---

## The loop as deployed

The long-form description of the daemon as it runs, consolidated here because this file is the
daemon's design authority (detail belongs in the leaf — `context-budget-spec.md` §5). The root
`CLAUDE.md` carries only the short version; this is the long form behind it, including the tasks added
after the sections above were written, the job/retry/lease rules, the session registry, resume gating
and the observability rows.

- **Local-first proactive loop.** The assistant wakes itself with no server. A resident **presence
daemon** (`seneschal/scripts/presence.py`) is the always-on nerve center — a **reactive asyncio core**
(nine supervised tasks: Telegram long-poll, Discord inbound, chat drainer, ~5 s scheduler tick, control
watcher, the cockpit pipe, archon-site supervision, cockpit supervision, and the resident PR watch): it
holds a **warm Telegram Chat session** via the `claude` **CLI** (stream-json mode —
**subscription-billed**, not the metered API; it scrubs `ANTHROPIC_API_KEY` to enforce that) or, when
the cockpit's `backend` dial says so, another subscription CLI
([pluggable-backend-spec.md](pluggable-backend-spec.md)), fires reminders (act-low — within ~5 s of
due, even mid-chat-turn), and runs a cheap **Watch** comms-peek on cadence. It also **owns the
completion push for background jobs** (`seneschal/scripts/jobs.py` +
[background-jobs-spec.md](background-jobs-spec.md)): work the assistant starts from a warm turn is
spawned **detached** and recorded in `state/jobs/`, so it outlives the session (which dies on idle
wind-down, any turn error, and every merge reload — a large share of session deaths), and the daemon's
tick — not the turn that made the promise — pushes the owner the outcome on **every** terminal state
(done / failed / timed-out / ended-unreadably / cancelled), stamped only once the send actually landed.
That closes the "I'll tell you when it's done" gap in code rather than in the prompt (a prompt-side-only
metrics contract that produced zero rows in a month is the cautionary precedent). `--wake` also
re-enqueues so the assistant reads the result in voice; `--lease` holds the warm session open for work
that genuinely can't detach — bounded, and a pending control always wins, so a job can never block a
Path-A deploy. The same tick also **launches a due retry** (spec §7): `--retry N` / `--retry-backoff` /
`--retry-window` absorb transient API failures that would otherwise lose a whole job to a one-second
`API Error: 500` — but **only** on a recognised transient signature (`jobs.TRANSIENT_SIGNATURES`: API
5xx/overloaded/429, connection reset, DNS, TLS, transport timeout). Unrecognised output is TERMINAL
first time — a blanket "exit != 0 ⇒ retry" would re-run a broken command's damage N times — and so are
`timed-out`/`ended-unknown`/`cancelled`; dying fast only *corroborates*, never decides; each attempt is
classified on its own log span, so attempt 1's 500 can't excuse attempt 2's real failure. It's **off by
default** (an untouched caller behaves exactly as it did: one attempt, terminal first time, no delimiter
in its log) — but **the classification is recorded either way** (spec §7.4.1): every job carries an
`attempts[]` whose entries name the transient/terminal verdict, the signature that matched, and
`retry_enabled`, so `state/jobs/` can answer *"has any job failed transiently without retry enabled?"* —
which is the diagnostic that says which callers should opt in. Recording only for jobs that already
opted in would be data collected after the decision it was meant to inform. `--retry` is the caller's
**assertion that the command is safe to re-run from scratch** (named, not solved — a retry re-runs the
whole argv), the wait is a `retry-pending` record + `next_attempt_at` rather than a `sleep`, so a
mid-backoff restart resumes it, and it is still **exactly one push** — at the real outcome, naming the
attempt count so a try-4 success never reads as a clean first try. While an interactive `/assistant`
chat is **live** (the **session registry**, `state/sessions/<id>.json` — one entry per live session,
refreshed around every warm turn; the machine-wide `session_stamp.py` hook stamps every *other* Claude
Code session as awareness-only `build` entries with a `working_on`, so sessions can see who's on what
and not collide), it **defers** non-piercing nudges and skips the redundant peek so a buzz never lands
mid-conversation; `Call Me` + Critical still pierce. The same hook fire-and-forgets the **mini-dream**
on every SessionEnd (`mini_dream.py`): each ended session is distilled — salience-laddered
deterministic/LLM — into `state/session-distillations.jsonl` (anchored to the assistant's home
regardless of the session's project), every surface tails it at orientation, and Dream compacts it
nightly into the RAG index (+ `--prune-days 30`) — the LSM shape (`sentinel.session_is_live`, same
pierce set as the quiet window). Discord inbound is **gateway websocket push** (needs the uv venv's
`websockets`; falls back to REST polling without it; `discord.env` is auto-detected like the store's
MCP config). It's event-driven, so idle ≈ free; the warm session winds down after idle and re-spawns.
- **Archon-site supervision** is the seventh task (`seneschal/scripts/archon_sites.py`): it discovers
every archon's self-declared human-facing UI (`archons/<id>/site.json`), health-checks it over HTTP on a
~20 s reconcile cadence, and (re)spawns one that's down or wedged — **detached**, so it survives the
daemon's own merge-reloads (adopt-not-duplicate via `state/archon-sites.pids.json`); a healthy site is
a complete no-op across a reload. Auto-on whenever a `site.json` exists; `--no-archon-sites` opts out.
The cockpit's Archons tile gets a matching per-site **Restart** button
(`POST /api/archons/{id}/restart`, CSRF-guarded + audited) that queues a `restart-site` control this
task picks up next pass. See `seneschal/references/archons.md`'s `site.json` convention.
- **Cockpit supervision** is the eighth task (`seneschal/scripts/cockpit_site.py`): the daemon starts
and keeps up **the cockpit backend itself** on `127.0.0.1:8760`, so the observatory doesn't depend on a
human leaving `uvicorn` open in a window. Same reconcile-not-spawn discipline (health-check
`GET /api/health` — the public route, so a 503 auth wall can't read as a dead process — respawn
detached, adopt via `state/cockpit-site.pid.json`, which matters more here since the pipe serves
exactly one client and a rival backend would fight the adopted one for it), plus things archon sites
don't need: it **bounces the backend when the checkout's git HEAD moves** (the PID file records the rev
at spawn — that's what makes a merged cockpit change load, and it covers `seneschald-update`, a manual
restart, break-glass force-pull and crash-restart alike without hooking any of them; `read_head_rev`
reads `.git` directly, never shelling out to a `git` that hangs on fsmonitor); it **builds
`cockpit/web/dist`** when missing or stale (`npm ci` + `npm run build` as **async subprocesses** a
shutdown abandons rather than waits on — a worker thread would have held a graceful reload hostage,
since `asyncio.run` waits for its default executor at exit; `app.py` mounts dist at import time, so a
finished build earns one bounce, and attempts are capped per boot); and it **rotates the backend's log**
at the same kill-then-respawn point when it grows oversized ([log-rotation-spec.md](log-rotation-spec.md)).
Auto-on whenever the `cockpit` extras are importable. The daemon itself never installs anything;
without the extras it logs one line and sends **one Telegram nudge per boot**. Auth: it passes
`cockpit/server/cockpit.env` through when present (real OIDC always wins) and sets
`COCKPIT_DEV_NO_AUTH=1` only when no client id ended up configured, so an unprovisioned box gets a
working localhost dashboard instead of a wall of 503s (the backend still binds loopback and rejects
non-loopback clients). Knobs: `--no-cockpit-app`, `--cockpit-app-port`, `--no-cockpit-web-build`. See
[cockpit-spec.md](cockpit-spec.md) + `seneschal/scripts/SCHEDULING.md`.
- **The resident PR watch** is the ninth task (`seneschal/scripts/pr_sweep.py`): every ~3 min it lists
each watched repo's open PRs, keeps the ones that are **terminally green and not drafts**, and hands
them to `merge_guard.ask_on_green` — so the merge-approval picker that is supposed to auto-send actually
does, without an agent remembering to start a watcher. **The gap it closes was measured:**
`ask_on_green` had exactly one caller, `watch_pr.py`, which runs only when somebody starts it, so PRs
with a watcher started produced pickers and PRs that went green with nothing watching produced none.
**It asks and nothing else** — it cannot merge, approve, or write an approval record, and
`record_approval`'s single-caller property (the daemon's Telegram callback path) is untouched. It
decides exactly ONE thing, *is CI terminally green*, and that is `watch_pr.classify` **imported rather
than restated**; docs-only, still-open, no-live-approval, not-already-asked and quiet-hours all remain
the guard's own five refusals, because a second classifier here could drift from the one doing the
blocking.
**The burst is the design problem**, since a resident sweep sees every open PR at once on its first
pass, after a restart and after an outage — four bounds, in the order they apply: **(1)** the quiet
window suppresses the *asking* (`merge_guard.in_quiet_hours`, the same predicate over `sentinel`'s
curfew, not a second copy — a resident watcher makes 3 AM far likelier than a hand-started one did).
It no longer short-circuits the whole pass: a reaction (`picker_mark`) raises no notification, so
withholding it overnight bought nothing, and the pass now looks and tidies; only the picker is held,
and held rather than dropped. **The hold stands down while the owner is demonstrably awake** (a row of
theirs in `turns.jsonl` within 30 min, via `merge_guard.picker_quiet_hours`; fails toward quiet);
**(2)** the guard's `(repo, pr, head_sha)` ask log lives on disk, so the reboot that follows every merge
re-sees the same PRs and asks about **none** — bound 2 needed no new mechanism at all; **(3)** at most
**one picker actually goes out per pass** — the stagger-don't-batch reminders rule applied to a
different queue, so five PRs going green together become five pickers across five passes rather than
five buzzes in one second; and **(4)** a capped pass **names what it held over**, because a silent
truncation reads as "covered everything". **It deliberately does NOT seed its first run**: seeding
would write ask-log rows for pickers that never went out, and the PR would then never get its question
— the cap is a *queue*, a seed would be a *drop*. Fail-open per repo and per pass (absent/unauth'd/
rate-limited `gh` costs the pass), and a repeated error logs **once** rather than every 3 minutes
forever. The watched set is configuration (`pr_sweep.default_repos` → the owner's `watched_repos` in their
per-install `pr-guard.json`, seeded from `seneschal/references/pr-guard.example.json`, else this checkout's own `origin`) rather than discovery, so it
is reviewable and cannot widen without a diff; a subtree mirror is deliberately left out, being a push
target rather than a deploy target.
**The tidyings reach further than the asking**: the same pass also lists every repository a *pending
picker* names — `picker_retire.pending_repos`, the hand-run CLI's own enumeration, at most 10 extra per
pass with the rest named as held — and hands those rows to `picker_mark` and `picker_retire` **only**,
never to the ask path or the red-CI notice. A picker from another session used to stand after its PR
merged until the owner tapped *Not now* on it; now a merged PR's picker in any repository is edited to
*"merged at …"* within minutes, while which repositories get *asked about* is still exactly the watched
set.
**It is also off under `--stub-send`, unlike its siblings**: they never send, while a sweep's whole
purpose is to reach the owner's phone, and `telegram_ask.py` is a subprocess that has never heard of
that flag. Knob: `--no-pr-watch`. See `seneschal/scripts/MERGE_GUARD_SETUP.md` §4b.
- **It runs off `main`** and reloads itself when a PR merges (a `seneschald-update` task ff-pulls +
enqueues a graceful restart the daemon applies once its warm session is idle — see
`seneschal/scripts/PATH_A_CUTOVER.md` + `seneschal/scripts/seneschald-control.ps1`). Its runtime memory
logs live in gitignored `state/` so the pull never conflicts. Scheduled Desktop tasks
(Brief/Wrap/Dream/Journal) run the orchestrator directly. `sentinel.py` is a helper library + manual
one-shot (not the heartbeat). Interaction is `/assistant` chat + the Telegram bot (+ store comments on
the Notion backend). See `seneschal/scripts/SCHEDULING.md`.
- **The warm session is observable.** Its age, turns served, context-fill **estimate**, session cost,
and *why the last session ended* ride the status snapshot into the cockpit's Status panel + Context
gauge, and one row per turn lands in `state/metrics.jsonl` — **in code**, not prompt-side. A `!status`
(or `/status`) message on any channel is intercepted at intake and answered **locally**: no warm
session spawned, no turn spent, no thread-tail entry — so it still answers when the warm session itself
is the problem.
- **And it RESUMES rather than re-grounding where it safely can.** A respawn passes
`claude --resume <last_session_id>` and sends a short `RESUME_PREAMBLE` (restated clock + "re-check
anything time-sensitive") instead of the full `GROUNDING`, so a Path A reload — a large share of
session deaths — no longer drops a live conversation to a short thread tail. **Gated, because resume is
not free:** it restores the OLD context, so `presence.resume_decision` requires a *clean* death
(`idle_winddown`/`daemon_shutdown` — never a turn error, never an undelivered reply, never a
`RESPAWN_TURN_TIMEOUT`), recency (`RESUME_MAX_AGE_SEC`, 2 h), **and** a context below Oikonomos's
`context_fill_winddown_pct` — which is where that advisory knob finally earns its keep. A stale id fails
in ~3 s and falls back to a cold grounded start (`send`'s `cold_retry_text`), ahead of the older
bad-model rung. The ended session's record lives in `state/presence-state.json`'s `last_session`, since
a reload keeps nothing in RAM. **`CONTEXT_WINDOW_TOKENS` is 1_000_000**, the model's window — an
earlier 200_000 ceiling made the gauge's 80% mean 160k, so roughly a third of sessions were refused
`context_too_full` at ~16% real fill. Still an ESTIMATE, still labelled one; the knob's number is
untouched and now means 800k (the owner's to lower). Evidence + phase impact:
[session-continuity-spec.md](session-continuity-spec.md) §3.2.1.

## Router entry

**Router status (covers this file + `asyncio-daemon-plan.md`):** phases 0-2 BUILT; tasks 6-8 added
after; **task 9, the resident PR watch**. **What it decided:** the reactive core — nine supervised
tasks, their invariants, the merge-on-green plan, the loop as deployed. **Task 9 is the measured half of
an already-shipped decision** — auto-send-on-green had one caller that only ran when somebody started
it, so PRs went green unwatched and got no picker. It finds; the guard still decides, and still cannot
be made to approve.
