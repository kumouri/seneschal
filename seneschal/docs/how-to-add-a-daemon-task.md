# How to add a daemon task

**Status:** `REFERENCE` — a how-it-works page.

## Today's mechanism — explicit lines, not a registry

`seneschal/scripts/presence.py`'s `main_async` starts every supervised task in one
`asyncio.gather(...)` call, each wrapped in `_supervise(name, coro, state, log)`. There is **no
decorator, no `TASKS` table** — what you're adding to is one line per task. Six ship today:
`telegram`, `discord`, `drainer`, `scheduler`, `control`, `cockpit`.

```python
await asyncio.gather(
    _supervise("telegram", telegram_task(state, args, fake_queue, log), state, log),
    _supervise("drainer", drainer_task(state, args, log, make_session, idle_sec), state, log),
    # ... one line per supervised task
)
```

## The steps

1. **Write an `async def your_task(state: DaemonState, args, log) -> None:` coroutine.** The
   standard shape: `while not state.stop.is_set(): ... await _sleep_or_stop(state, INTERVAL_SEC)`.
   `_sleep_or_stop` returns early the moment the supervisor winds down, so a task never adds
   latency to a graceful shutdown.
2. **Fail open inside your own loop.** Catch and `log()` an expected failure, then keep looping — a
   flaky external call (an HTTP endpoint, a chat API) must never take the whole daemon down.
   `telegram_task` logs a failed poll (`! telegram poll: …`) and polls again; `discord_task` degrades
   from the gateway to REST polling when its module won't import, rather than dying.
3. **Add one `_supervise("your-name", your_task(state, args, log), state, log)` line** to the
   `asyncio.gather(...)` call. `_supervise` is the **outer** net — if your coroutine still raises
   past your own handling, it logs the crash with its traceback, sets `state.crashed = True`, and
   stops the daemon **loudly** (never a silent "alive but missing a task"), so the scheduled task's
   restart-on-failure relaunches a clean process.
4. **Wire an opt-out flag if it can be disabled** (`--no-your-task`), following the existing
   `--no-cockpit` / `--no-discord` / `--no-reminders` / `--no-peek` / `--no-slots` pattern. A task
   that is switched off (or can't run in test modes — `--stub-brain`, `--fake-inbox`) simply
   `return`s at the top; `cockpit_task` and `control_task` are the reference.
5. **Add tests.** `test_presence_*.py` is the existing shape — a task that changes daemon behaviour
   needs a test asserting that behaviour, not just that the task doesn't crash.

## What NOT to do

- **Don't spawn `asyncio.create_task` outside `main_async`'s gather.** An unsupervised task can die
  silently with no `_supervise` catching it — the daemon looks alive with a missing capability,
  exactly what `_supervise` exists to convert into a loud restart.
- **Don't block the event loop.** Every task shares one process; a blocking call (a big store read,
  a long-poll) stalls the reminders, the drainer, everything. Use `asyncio` I/O, or a worker thread
  via `asyncio.to_thread` (`scheduler_task`'s `check_reminders` and `telegram_task`'s long-poll run
  in one for this reason). Mutate shared daemon state only on the event loop, never from inside a
  `to_thread` callable.
- **Don't assume your failure is the daemon's business.** Catch it locally (step 2); let
  `_supervise` catch only what you couldn't handle yourself.

Full design of the reactive core, its invariants and the warm session's lifecycle:
`seneschal/docs/asyncio-daemon-design.md` → "Architecture".
