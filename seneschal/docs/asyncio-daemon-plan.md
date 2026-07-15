# Asyncio reactive daemon — implementation plan

> **Status: phases 0–3 all shipped and live.** Phase 0 = packaging; phase 1 = the core port (incl. an
> adversarial-review fix commit — notably the cross-process `reminders.json` writer lock); phase 2 =
> the Discord gateway; phase 3 = the docs-sync PR. The daemon reloads onto each merged phase via the
> automatic update flow. Discord's gateway stays dark until `discord.env` exists. Deferred phase-3+
> items live in the design doc's last section.

Companion to [asyncio-daemon-design.md](asyncio-daemon-design.md). One PR per phase; **merge on green CI
only**; every merge auto-deploys to the live daemon via Path A (merge → `seneschald-update` ff-pull →
graceful reload), so each phase must leave the daemon healthy on its own.

**Standing constraints for every phase**

- Do not touch `phone/android/` (parallel work may be in flight there) or `phone/` at all.
- Do not touch the Dream PRs. (Dream merges its own PRs on green — see Dream step 5 in
  `seneschal/SKILL.md`.)
- CLAUDE.md / README status-table updates are deferred to the final docs-sync phase so parallel branches
  don't conflict on them.
- After each merge, verify on the host: `seneschald-update.log` shows pull (+ sync) + restart request;
  `presence.log` shows the graceful reload; `state/presence.lock` heartbeat is fresh (< ~90 s).
- Rollback for any phase = revert its merge commit; Path A redeploys the revert automatically.

---

## Phase 0 — packaging + CI hardening (no behavior change)

**Branch:** `seneschal/asyncio-phase0-packaging`

1. `pyproject.toml` (repo root): project `seneschal-daemon`, `requires-python >= 3.11`, dependencies:
   `websockets` (pinned range). `uv.lock` committed. `.venv/` added to `.gitignore`.
2. `.github/workflows/ci.yml`: adopt the unittest step
   (`python -m unittest discover -s seneschal/scripts -p "test_*.py"`), plus a `uv lock --check` step
   (`astral-sh/setup-uv`) so the lockfile can't drift from `pyproject.toml`.
3. `seneschal/scripts/seneschald-control.ps1` `Update-Seneschald`: after a successful ff-pull, `uv sync
   --frozen` (skip gracefully with a loud log if `uv` is missing); request the graceful restart **only
   on sync success**; on failure write `state/pending-restart` and retry the sync + deferred restart on
   subsequent runs. All outcomes logged to `seneschald-update.log`.
4. `seneschal/scripts/run-presence.cmd`: prefer `..\..\.venv\Scripts\python.exe` if present, else
   `python`.
5. On the host (not in git): `uv sync --frozen` once to create `.venv`, and prove
   `.venv\Scripts\python.exe -m unittest discover -s seneschal/scripts -p "test_*.py"` is green.

**Merge gate:** CI green. **Merge care:** if the working tree holds an uncommitted `ci.yml` edit
identical to step 2's base and the post-merge ff-pull refuses over it, discard that working-tree copy
(its content is in `main` by then) and re-run Update. **Post-merge check:** daemon reloads normally;
nothing else changes.

## Phase 1 — asyncio core port (same transports, same semantics)

**Branch:** `seneschal/asyncio-phase1-core`

1. Rewrite `presence.py`'s `main()` into `main_async()` + the five tasks from the design (telegram,
   discord *REST-poll* task on ~10 s cadence, drainer, scheduler, control). Keep every module-level
   helper function with its current signature — the existing tests must pass **unmodified**.
2. Port the drain-loop turn logic verbatim into `drainer_task` (attempt-count-before-send, dead-letter,
   delivery-gated pop, rollback-on-transient-send-failure, session reset, idle wind-down with the
   control-pending 60 s short window).
3. `WarmSession` unchanged; called via `asyncio.to_thread`. All sentinel helpers via `to_thread`.
4. Preserve `--stub-brain / --fake-inbox / --max-iterations` (iteration = one telegram-poll cycle); add
   `--stub-send` so offline runs can't message real Telegram. Keep the single-instance lock + heartbeat,
   SIGINT/KeyboardInterrupt graceful path, and the detached-successor restart mechanism exactly.
5. New tests: drainer lifecycle (delivered / transient-fail / poison-pill), control quiesce ordering,
   scheduler gating (`warm_busy` honored). `unittest.IsolatedAsyncioTestCase`, stdlib only.
6. Host verification before PR: run the offline harness (`--stub-brain --fake-inbox … --stub-send
   --no-peek --no-slots --no-reminders`, scratch `--state-dir`) **with the same interpreter the daemon
   uses**, proving it boots, drains, persists state, and exits clean.

**Merge gate:** CI green + offline harness green on host. **Post-merge check:** graceful reload onto the
asyncio core; watch one full reminder fire + one Telegram exchange land in `presence.log`; confirm
heartbeat cadence (~5 s scheduler tick) and no crash-respawn within 30 min.

## Phase 2 — Discord gateway (push, with REST fallback)

**Branch:** `seneschal/asyncio-phase2-gateway`

1. New `seneschal/scripts/discord_gateway.py`: connect/IDENTIFY/heartbeat/RESUME per the design; pure
   parsing + resume-bookkeeping functions kept import-safe without `websockets` so they're unit-testable
   and `py_compile`-clean everywhere.
2. `presence.py` `discord_task`: gateway when importable + configured, REST cadence fallback otherwise
   (loud log either way). REST `after=` catch-up on every (re)connect; gateway messages advance
   `state/discord-offset`.
3. Auto-detect `scripts/discord.env` (like `notion-mcp.json`), `--no-discord` still forces off. Ships
   dark if the file doesn't exist on the host yet.
4. Update `DISCORD_SETUP.md` (the "why REST polling" rationale block becomes "gateway with REST
   fallback + catch-up") and note the venv requirement.
5. Tests: dispatch/parse/resume/backfill-cursor pure functions; fallback selection with `websockets`
   absent (import-error injection).

**Merge gate:** CI green. **Post-merge check:** daemon reloads; log shows "discord: not configured"
(dark). When the owner later creates `discord.env`: expect gateway connect + instant echo turnaround.

## Phase 3 — docs sync + close-out

**Branch:** `seneschal/asyncio-phase3-docs`

1. CLAUDE.md: architecture section (event-driven asyncio daemon; "small amount of stdlib Python" and
   "no package install needed" claims corrected to "uv-managed venv, one dependency, stdlib-first"),
   dependencies section (uv + websockets), Repo-state section (Update now syncs deps before the reload).
2. README status table row for the reactive daemon; `SCHEDULING.md` launcher notes.
3. Run `/sync-claude-md`; record the outcome + any deferred phase-3+ items (exact-time timer wheel,
   mid-turn interleave, HA/Signal tasks) in the Run Log and carry-over.

**Merge gate:** CI green.
