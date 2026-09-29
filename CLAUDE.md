# CLAUDE.md

Operating guide for Claude Code working in the **seneschal** repo.

## What this repo is

Seneschal is a **local-first chief-of-staff assistant framework** — an agent skill suite
(orchestrator + subagent skills, mostly Markdown) plus a modest amount of stdlib-first
Python (presence daemon, comms bridges, local RAG). It is the open-source, de-personalized
framework: the assistant ships with a default personality, a persona wizard builds a custom
one, and the data store (Notion / Obsidian / Markdown folder) is pluggable behind a schema
registry.

The framework is feature-complete for its first release: default persona + wizard, three
pluggable data backends + onboarding, the full orchestrator/subagent suite, the presence daemon,
the phone screener + Android app, local RAG/salience, and the Forge. The private assistant it was
distilled from stays private; this is the machinery with the person removed.

## Layout

```
.claude/commands/  assistant.md (/assistant — opens an in-character chat), setup*.md (the
                   /setup wizard entrypoints), doctor.md (/doctor — the health board +
                   in-session MCP probes)
persona/           who the assistant is + who it works for (see persona/README.md): tracked
                   persona.default.md / persona.template.md / identity.example.json; the real
                   persona.md / identity.json / owner-profile.md are gitignored per-install
seneschal/
  SKILL.md         the orchestrator — a thin mode router (Chat/Brief/Wrap/Triage/Ask/Reminders/
                   Watch/Dream/Forge → modes/; Journal/Archive → their subagents), execution rules,
                   the Advisor Chain, reference index
  modes/           one self-contained body per dispatchable mode, read imperatively on dispatch —
                   so an edit here edits a live run (design: docs/grounding-restructure-spec.md)
  store/           the pluggable system of record (see store/README.md): config.json picks the
                   active backend; each store/<backend>/ pair is schema.md (domain map) + mapping.md
                   (the six store verbs → that backend's tools). Ships notion/ (MCP; the reference
                   backend), obsidian/ + markdown/ (filesystem). Skills speak backend-neutral verbs
  references/      the policies and maps every mode reads (databases, calendar/comms mapping,
                   briefing, reminders-policy, autonomy-policy(+config), memory, advisor-chain,
                   slack-ssot, salience, archons, proposed-learnings) — router:
                   seneschal/references/CLAUDE.md
  docs/            the design record — every spec + its status, grouped — router:
                   seneschal/docs/CLAUDE.md (spec-prompts/ holds the planning prompts behind specs)
  scripts/         presence.py (resident asyncio daemon), sentinel.py (helper/one-shot),
                   identity_common.py (persona/identity.json reader — never raises, defaults
                   when absent; presence.py renders its grounding/slot prompts from it),
                   telegram/discord/proton/google comms bridges (telegram_* — send/poll, the
                   one HTTP transport, Markdown→HTML, topics, ask pickers: TELEGRAM_SETUP.md),
                   mouth.py + turns.py + promises.py + channel_declare.py + turn_suppression.py
                   + tomorrow_marker.py (said-log/dispatch queue, turn capture, promises, reply
                   routing — each spec in docs/CLAUDE.md), reminders_* queue+ack ledger
                   (incl. reminders_seed.py — the whole-day exact-time seeder; reminders_cadence
                   / reminder_premise* / reminder_suppressions / reminders_live / _reconcile),
                   ack.py (free-text ack) + watch_ack/_reconcile/_suppress (Watch-send gates),
                   cockpit_pipe.py + model_config.py + governor.py + fable_delegate.py (the
                   cockpit pipe, model dials, budget governor, and Fable delegation one-shot),
                   outbox.py + outbox_common.py (durable write-behind journal for act-low
                   Notion writes — Notion backend only; filesystem backends write direct),
                   session_stamp.py + session_heartbeat.py + mini_dream.py (multi-session
                   registry under state/sessions/ + the per-session mini-dream distiller),
                   jobs.py + job_*.py + worktree_gc.py (durable background jobs —
                   docs/background-jobs-spec.md), loops.py + cadence_chain/owi_*/
                   observation_gate/standing_safety/brief_prestage/carryover_region (the
                   open-work register and its satellites — state/README.md),
                   rag_* (local semantic index), router.py, salience tooling,
                   usage_probe/_activity/_health + spend_levers + trace + record_search +
                   transcript_archive/_size_watch (plan-meter, spend and session-trace
                   observability — docs/usage-telemetry-spec.md, docs/session-trace-spec.md),
                   cockpit_site + archon_sites (daemon-supervised web UIs), health/presence
                   pipelines, archive_common.py + telegram_ingest.py + discord_export_ingest.py
                   + sms_ingest.py + archive_aggregate.py (message archiver),
                   setup_state.py + setup_env.py + setup_doctor.py + settings_merge.py +
                   render_units.py + setup_checkout.py (the /setup wizard's deterministic
                   substrate: resumability ledger + manifest-driven env-file writer + the
                   /doctor green/yellow/red health board + the ~/.claude/settings.json hook
                   merger + the daemon chapter's launcher/unit/plist renderer + the
                   git-worktree guard preflight and render_units --apply enforce),
                   foundation primitives (each module's docstring is its spec): paths.py
                   (state-dir resolution), stateio.py + memory_write.py (atomic state writes),
                   envfile.py, clock.py + activity_day.py (owner wall clock + the
                   owner.dayBoundaryHour after-midnight cut, over tz_common), failures.py
                   (state/failures.jsonl), dream_steps.py (Dream step ledger), state_backup.py,
                   notes.py, learnings.py (proposed-learnings close/retire/audit), log_rotation.py,
                   gate_git.py, domain_age.py, _http_test_server.py (test fixture),
                   send_gate.py + send_gate_hook.py + pending_approvals.py + send_recipients.py
                   (the outbound approval gate, its one approval store, the non-content send
                   ledger — SEND_GATE_SETUP.md; _owner_fixture.py is their test fixture),
                   provenance_guard.py (the RAG index's writer-provenance registry),
                   the opt-in guard hooks — merge_guard.py (owner-approved head SHA before any
                   merge; MERGE_GUARD_SETUP.md), branch_delete_guard.py, bash_path_guard.py,
                   script_file_guard.py, query_shape_hook.py (each its *_SETUP.md) — and the
                   PR automation pr_*.py + picker_*.py + watch_pr.py + branch_sweep.py +
                   develop_ci_status.py, configured by repo_config.py (references/pr-guard.json),
                   the CI gate set — check_*.py (each docstring is its spec) + count_tests.py +
                   ci_local.py (runs every CI gate locally) + check_placeholders.py —
                   *_SETUP.md guides + INTEGRATIONS.md,
                   seneschald-control.ps1 + run-*.cmd (Windows scheduled-task wrappers) +
                   seneschald_revive.py (the watchdog's revive predicate), backends/ (the
                   warm-session Backend seam: claude_cli / codex_cli + CODEX_SETUP.md),
                   interleave.py (the mid-turn relevance gate), instructions_loaded.py
                   (opt-in sub-router load logger)
  setup/           env-manifest.json — the machine-readable manifest of every configurable env
                   surface, which the /setup wizard's env walker + the doctor read
  *.json           the CI gates' tracked config: context-budget.json, context-pointers.json,
                   context-stores.json, state-write-allowlist.json, wall-clock-allowlist.json
  state/           local-first runtime cache — gitignored except README + *.example.*
cockpit/           the Seneschal Cockpit — a local-first web observatory over the daemon
                   (see cockpit/README.md): server/ is a FastAPI backend (127.0.0.1:8760; the
                   `cockpit` uv extras group — fastapi + uvicorn, never pulled in by the daemon's
                   `uv sync --frozen`) reading seneschal/state/* tolerantly + holding the one
                   client connection to the daemon's cockpit pipe; web/ is a Vite + TS + React
                   frontend (built dist/ served static by the backend). Real OIDC auth ships
                   (auth-code + PKCE via auth.py/oidc.py; dev-no-auth remains the default until
                   an OIDC app is provisioned) alongside zitadel/ (the self-hosted IdP compose
                   stack + setup walkthrough), breakglass/ (the stdlib-only emergency-recovery
                   supervisor + its 3-rung ladder), and decoy/ (the public honeypot chat —
                   separate process, zero tools/data). test_parity.py is the CI tripwire
                   for its hand-duplicated model_config/governor copies. Router: cockpit/CLAUDE.md
subagents/         morning-briefing, eod-wrap, email-triage, slack-triage, calendar-steward,
                   store-qa, reminders, message-archivist, journal-steward (generic core),
                   archon-forge, persona-wizard, store-setup, setup (the unified /setup
                   sequencer + its chapters/ files) — the delegated skills the orchestrator +
                   the /setup commands dispatch to
phone/             the voice call-screener (Cloudflare Workers + Twilio; deploys as
                   `seneschal-screener`) + the Seneschal Call Shield Android companion app
                   (com.kumouri.seneschal, committed Gradle project) feeding presence/health
archons/           Archon staff data (Forge mode) — the shipped `proteus/` job-application
                   example (profile.example.json + stdlib tools); real needs/stables/profiles
                   are gitignored on installs. Router: archons/CLAUDE.md
```

First-run setup is `/setup` — the unified, **resumable** wizard (`subagents/setup/SKILL.md` +
its `chapters/`): preflight → persona → store → owner-interview → auth-models → the env walker
(`env:<id>` per manifest entry, required first; an on-demand Ollama sub-chapter) → MCP servers
(notion/calendar/slack, with the awaiting-auth-restart dance) → cockpit → daemon → verify. Every
chapter marks the ledger (`seneschal/state/setup-state.json` via `setup_state.py`; `infer`
respects pre-wizard work), a crashed/partial run resumes at the first unfinished chapter, and
`/setup <chapter>` jumps anywhere. The closing `verify` chapter runs the doctor; **`/doctor`**
re-runs that health check any time — `setup_doctor.py`'s green/yellow/red board (exit code =
red count) plus the in-session MCP probes the script can't do. The `daemon` chapter installs
the always-on layer tri-platform with at most ONE elevation (`render_units.py` renders
gitignored locals into `seneschal/state/setup/` — a `register-tasks.ps1` the owner
approves-then-runs in one `-Verb RunAs` shot on Windows, with an InteractiveToken
`--user-level` fallback; systemd user units + the one `loginctl enable-linger` sudo on
Linux; launchd agents, zero elevation, on macOS) and merges the §5 session hooks into the
user's `~/.claude/settings.json` (`settings_merge.py` — diff-first, append-only,
backup-first, refuses corrupt JSON). Standalone
`/setup-persona` and `/setup-store` still work and update the same ledger. The framework runs
on the shipped defaults (the default persona, no store) until any of it is run.

## The daemon, briefly

`seneschal/scripts/presence.py` is the always-on nerve center: a reactive asyncio core holding a
warm chat session behind a pluggable backend (`backends/`: the `claude` CLI by default, or the
`codex` CLI on a ChatGPT subscription — subscription-billed either way; it scrubs `ANTHROPIC_API_KEY`
/ `OPENAI_API_KEY`), firing
reminders from the local queue, and running a cheap Watch comms-peek on cadence. Reminder compute is a
**once-per-owner-local-day seed** (`maybe_seed_day`, a date-rollover trigger — the four fixed reminder
slots are retired): the seed run queues each ⏰ row's exact-time nudges via `reminders_seed.py` and the
~5 s tick delivers them (`docs/reminder-exact-time-scheduling-spec.md`, `references/reminders-policy.md`).
Telegram inbound is full-featured — attachments download to `state/inbox/`, swipe-replies carry their
quoted context, reactions map to intents (a same-day 👍 on a nudge auto-acks; on the notion backend the
ack also journals through the outbox), and a post-restart burst gets one backlog ack
(`docs/telegram-inbound-spec.md`). A **cockpit pipe** (`cockpit_pipe.py`, localhost websocket,
`--no-cockpit` to disable) streams warm-session turns and accepts chat as a third channel; **model
dials** (`state/model-config.json`) pick the warm model + the Fable-delegation ceiling — when a turn
needs more, the warm session **delegates up** via `fable_delegate.py` (a `claude -p` subprocess
one-shot, never a session handoff, badged in the transcript), triggered by the router's ceiling-gated
**fable arm**, its own judgment, or a `!fable` force-route (bypasses the classifier, never the gate);
the **Oikonomos governor** (`governor.py`, Advisor Chain order 15) meters every turn's token spend and
hard-gates delegation quotas (rails), with advisory knobs surfaced honestly as guidance
(`docs/cockpit-spec.md`). It forwards the
**store's MCP** (if any) into every spawned headless `claude` — resolved store-config-driven
(`--store-mcp` override → `store/config.json`'s active backend → legacy `scripts/notion-mcp.json` →
None; filesystem backends need none, `resolve_store_mcp`) — alongside an auto-detected
`scripts/slack-mcp.json` (Slack send hands, `--no-slack` to disable). It runs off `main` and reloads
itself when a PR merges (`seneschald-update` scheduled task → ff-pull → graceful restart). The updater
**self-heals and speaks up**: it reclaims `main` from an abandoned feature-branch park only when the
branch has no unique commits AND the session registry says no live session claims it (`sentinel.py
--branch-claimed`, fail-closed), stamps `state/seneschald-health.json` every cycle (`last_ok` is the
watch-the-watcher field), and nudges the owner on Telegram when blocked > 3 cycles (~30 min, re-alert ≤
every 6 h) — see `scripts/PATH_A_CUTOVER.md`. It also revives a dead daemon (`seneschald_revive.py`,
`docs/seneschald-revive-spec.md`) and restarts it when the owner logs into a different Claude account. Runtime state lives in gitignored `seneschal/state/`.

**Supervised tasks + resilience** (the full list and recipe: `docs/how-to-add-a-daemon-task.md`):
beside chat/reminders/control the daemon supervises the cockpit backend (`cockpit_site.py`),
archon sites (`archon_sites.py`) and a PR watch (`pr_sweep.py` + `pr_repair.py` — asks via the merge
guard, never merges). The warm session sits behind a **backend** seam (`scripts/backends/`: claude-cli
default, codex-cli opt-in via the model-config dial), **resumes** across reloads when the gate allows
(`docs/session-continuity-spec.md`), is cut off by an idle-gap **hung-turn deadline**
(`docs/hung-turn-deadline-spec.md`), and observes mid-turn arrivals (`interleave.py`). A self
**crash-loop guard** keeps a fast crash loop from burning, and the watchdog stands down for it and for a
deliberate stop. The Notion write-behind outbox flush is daemon-owned and Notion-backend only.

**Multi-session awareness:** a session registry (`state/sessions/`) tracks every live Claude Code
session on the box — the daemon defers non-piercing nudges into a live interactive `/assistant` chat
(daemon/desktop sources; build sessions are awareness-only) and skips the redundant comms-peek. The
machine-wide `session_stamp.py` hook (installed in the user's `~/.claude/settings.json`, never shipped
here — see `scripts/SCHEDULING.md` → "Session registry hooks") stamps sessions and, on SessionEnd,
fire-and-forgets `mini_dream.py`, which distills the transcript into
`state/session-distillations.jsonl` — the cross-instance memory Dream compacts nightly. The cockpit
displays these distillates as the **oneiroi** feed (singular *oneiros*; canonical pronunciation is the
ancient Greek — "oh-NAY-roy" — script/file names are unchanged).

## Conventions

- **Git Flow**: `main` (releases) + `develop` (integration); `feature/*` branches; PRs merge
  with **merge commits** (never squash/rebase); never merge red or pending CI.
- **Commits**: Conventional Commits with a scope, e.g. `feat(reminders):`, `docs:`, `chore:`.
- **Markdown is canonical** for every document deliverable; other formats are rendered
  build artifacts.
- **Secrets and personal data never get committed.** All `*.env` are gitignored, only
  `*.example` templates are tracked; generated persona/identity/store files are gitignored.
  CI enforces that any UUID in the tree is a `00000000-…` placeholder
  (`seneschal/scripts/check_placeholders.py`).
- **Stdlib-first Python.** Sanctioned third-party deps live in `pyproject.toml` and require
  a documented reason — today exactly two: `websockets` (Discord gateway push) and `tzdata`
  (the pure-data IANA timezone database `zoneinfo` needs on Windows, so the owner's configured
  timezone resolves in `tz_common`); everything must degrade gracefully without the venv
  (gateway → REST polling; tz math → the machine-local clock).
- **Docs stay in sync with code** — when code changes, correct the docs that describe it in
  the same change.

## CI

`.github/workflows/ci.yml` runs on push/PR to `main` and `develop`. The `python` job (full-history
checkout — three gates diff against `origin/develop`) byte-compiles every tracked `.py`, measures
the suites (`count_tests.py --check`: the total is published to the job summary and must never be
typed into this file or the README), runs the unittest suites under `seneschal/scripts/` and
`archons/proteus/tools/`, checks
uv.lock, runs the UUID placeholder guard, then the context/doc gates — **blocking**:
`check_context_pointers` (dangling pointers), `check_doc_status` (every `seneschal/docs/` file
declares a status), `check_state_writes` (no truncating `state/` write), `check_wall_clock` (no
zone-less clock read in checkers/tests), `check_rulings` (new ruling language needs a
`seneschal/docs/rulings.md` row), and `check_context_budget --enforce-headroom`; **report-only**:
the byte budget itself, `check_no_utcnow`, `check_context_stores --venue ci`,
`check_carryover_prose`, `check_grounding_dates` — and AST-parses every `.ps1`. Each gate's
docstring is its spec. `Reference data check` validates every tracked `.json`
(`check_json_files.py`). Two cockpit jobs cover the web observatory: `cockpit-server`
(`uv sync --extra cockpit --group test`, then unittest discover over `cockpit/server/`,
`cockpit/decoy/`, and `cockpit/breakglass/` — including `test_parity.py`, the tripwire for the
cockpit's hand-duplicated model_config/governor tables) and `cockpit-web` (Node 22, `npm ci` +
`npm run typecheck` + `npm test` + `npm run build` in `cockpit/web/`). `android.yml` builds the Call Shield app
on `phone/android/**` changes. The phone Worker has its own npm gates (`cd phone && npm run
typecheck && npm test`). Run every gate CI runs, locally, before pushing:

```
python seneschal/scripts/ci_local.py            # every CI step, ci.yml order; --list / --only <step>
python seneschal/scripts/check_docs.py          # just the doc-side gates, every finding in one pass
```

**Test temp is run-scoped.** Each discovery root (`seneschal/scripts`, `archons/proteus/tools`,
`cockpit/server`, `cockpit/decoy`, `cockpit/breakglass`) has a `test_0_tmproot.py`. `discover` imports it first because
it sorts first, and it points `tempfile` and `TMP`/`TEMP`/`TMPDIR` at one
`<system temp>/seneschal-test-runs/<pid>-<rand>` root that is removed at exit. This was added
2026-09-29, after ~1M leaked test dirs in `%LOCALAPPDATA%\Temp` stalled Windows logon. A new root
added to `ci.yml` needs one too, and `test_tmproot.py` fails until it has one. A test that deletes a
tree it made uses `tmproot.rmtree`, never `shutil.rmtree(..., ignore_errors=True)`, which silently
skips git's read-only objects on Windows. `ci_local.py` owns one root for its whole run, and each
step adopts it. After the last step it prints a report-only `temp:` line that counts leak-shaped dirs
created in the real temp dir during the run. Details: `seneschal/scripts/tmproot.py`.
