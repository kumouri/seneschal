# Asyncio reactive daemon — design

**Status:** approved direction; **sixth task (cockpit pipe) shipped** · **Scope:**
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
| `telegram_task` | Telegram long-poll (`poll_telegram` in a thread), enqueue inbound to the durable action queue, thread-append, Router shadow-classify | the loop's blocking heartbeat |
| `discord_task` | Gateway websocket (identify → heartbeat → `MESSAGE_CREATE` → resume), REST `after=` catch-up on every (re)connect; **falls back to REST cadence-polling** if `websockets` is unavailable | once-per-loop `poll_discord` |
| `drainer_task` | The warm session lifecycle + draining the durable queue front-to-back, one turn at a time; idle wind-down | the inline drain loop |
| `scheduler_task` | ~5 s tick: lock heartbeat, `reap_finished_slots`, `check_reminders`, roll refill, `maybe_peek`, `maybe_run_slots`, drain the cockpit inbox-fallback file | the per-loop cadence work |
| `control_task` | control queue + legacy `restart.request` sentinel; initiates graceful quiesce → detached-respawn restart / shutdown | the end-of-loop control block |
| `cockpit_task` | the **Seneschal Cockpit pipe** (v2) — a localhost-only, token-authed WebSocket server (`seneschal/scripts/cockpit_pipe.py`) serving exactly one client (the cockpit backend); relays `chat.send`/`status.get`/`control.restart` in, streams `chat.event`/`status` out | nothing — net-new surface |

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
   the two backstops (a crashed morning run retries; a broken one can't respawn forever).
6. **Notion read-burst serialization.** At most one headless reader at a time, and none while a chat
   turn is mid-flight; chat is priority and is never delayed. The `warm_session_busy` /
   `heavy_run_in_flight` gates keep their exact semantics (the drainer exposes "mid-turn" state).
7. **Graceful restart, never mid-exchange.** Controls with `defer_until_idle` apply only when the session
   is down and the queue empty; a pending control shortens the wind-down to `CONTROL_PENDING_IDLE_SEC`.
   On Windows the restart **spawns a detached successor and exits** (never `os.execv` — see the
   incident note in the code); that mechanism is kept verbatim.
8. **Billing safety.** Every child env is scrubbed of `ANTHROPIC_API_KEY`; the `apiKeySource` warning on
   session init stays.

### The warm session under asyncio

`WarmSession` keeps its synchronous `subprocess.Popen` + line-reader implementation (proven, including
the UTF-8 forcing); the drainer calls `send()` via `asyncio.to_thread`. The event loop stays free while
a turn runs — that thread is parked on the child's stdout, which is exactly what threads are for. A
native `asyncio.subprocess` port is possible later but buys nothing today and risks re-learning Windows
pipe quirks.

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
  cleanly with the quiet gate (both active = still no drops; piercing still pierces).
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
  warm session and `mini_dream.py`), seeds a budget-bounded `telegram-thread.json` tail, and
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
- **Mid-turn interleaving** (surfacing a "cancel that" to the in-flight turn) — needs product thinking
  about conversation semantics, not just plumbing.
- **Native `asyncio.subprocess` warm session**, HA/Signal/health-listener tasks — the substrate is ready
  for them; they are their own projects.
