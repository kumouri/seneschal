# Asyncio reactive daemon — design

**Status:** approved direction · **Scope:** `seneschal/scripts/presence.py` and its launcher/update
plumbing. Companion implementation plan: [asyncio-daemon-plan.md](asyncio-daemon-plan.md).

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
| `scheduler_task` | ~5 s tick: lock heartbeat, `reap_finished_slots`, `check_reminders`, roll refill, `maybe_peek`, `maybe_run_slots` | the per-loop cadence work |
| `control_task` | control queue + legacy `restart.request` sentinel; initiates graceful quiesce → detached-respawn restart / shutdown | the end-of-loop control block |

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
