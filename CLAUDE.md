# CLAUDE.md

Operating guide for Claude Code working in the **seneschal** repo.

## What this repo is

Seneschal is a **local-first chief-of-staff assistant framework** — a Claude Skill suite
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
.claude/commands/  assistant.md — the /assistant slash command (opens an in-character chat)
persona/           who the assistant is + who it works for (see persona/README.md): tracked
                   persona.default.md / persona.template.md / identity.example.json; the real
                   persona.md / identity.json / owner-profile.md are gitignored per-install
seneschal/
  SKILL.md         the orchestrator — modes (Chat/Brief/Wrap/Triage/Ask/Watch/Dream/Journal/
                   Reminders/Forge/Archive), execution rules, the Advisor Chain, reference index
  store/           the pluggable system of record (see store/README.md): config.json picks the
                   active backend; each store/<backend>/ pair is schema.md (domain map) + mapping.md
                   (the six store verbs → that backend's tools). Ships notion/ (MCP; the reference
                   backend), obsidian/ + markdown/ (filesystem). Skills speak backend-neutral verbs
  references/      databases (placeholder-id schema registry, Notion backend), calendar/comms mapping,
                   briefing, reminders-policy, autonomy-policy(+config), memory protocol, advisor-chain,
                   salience, archons, notion-rate-limits (stub → store/notion/mapping.md),
                   proposed-learnings (Dream's PR target)
  docs/            asyncio-daemon-design.md + asyncio-daemon-plan.md (the reactive-core design)
  scripts/         presence.py (resident asyncio daemon), sentinel.py (helper/one-shot),
                   identity_common.py (persona/identity.json reader — never raises, defaults
                   when absent; presence.py renders its grounding/slot prompts from it),
                   telegram/discord/proton/google comms bridges, reminders_* queue+ack ledger,
                   session_stamp.py + session_heartbeat.py + mini_dream.py (multi-session
                   registry under state/sessions/ + the per-session mini-dream distiller),
                   rag_* (local semantic index), router.py, salience tooling, health/presence
                   pipelines, archive_common.py + telegram_ingest.py + discord_export_ingest.py
                   + sms_ingest.py + archive_aggregate.py (message archiver),
                   check_placeholders.py (CI guard), *_SETUP.md guides,
                   seneschald-control.ps1 + run-*.cmd (Windows scheduled-task wrappers)
  state/           local-first runtime cache — gitignored except README + *.example.*
subagents/         morning-briefing, eod-wrap, email-triage, slack-triage, calendar-steward,
                   store-qa, reminders, message-archivist, journal-steward (generic core),
                   archon-forge, persona-wizard, store-setup — the delegated skills the
                   orchestrator + the /setup commands dispatch to
phone/             the voice call-screener (Cloudflare Workers + Twilio; deploys as
                   `seneschal-screener`) + the Seneschal Call Shield Android companion app
                   (com.kumouri.seneschal, committed Gradle project) feeding presence/health
archons/           Archon staff data (Forge mode) — the shipped `proteus/` job-application
                   example (profile.example.json + stdlib tools); real needs/stables/profiles
                   are gitignored on installs
```

First-run setup is `/setup` (persona wizard → store onboarding), or the chapters standalone:
`/setup-persona` (name/voice/demeanor → `persona/persona.md` + `identity.json`) and `/setup-store`
(pick + provision a backend, read existing content, interview → `owner-profile.md`). The framework
runs on the shipped defaults (default-Claude persona, no store) until they're run.

## The daemon, briefly

`seneschal/scripts/presence.py` is the always-on nerve center: a reactive asyncio core holding a
warm `claude` CLI chat session (subscription-billed; it scrubs `ANTHROPIC_API_KEY`), firing
reminders from the local queue, and running a cheap Watch comms-peek on cadence. It forwards the
**store's MCP** (if any) into every spawned headless `claude` — resolved store-config-driven
(`--store-mcp` override → `store/config.json`'s active backend → legacy `scripts/notion-mcp.json` →
None; filesystem backends need none, `resolve_store_mcp`). It runs off `main` and reloads itself when a
PR merges (`seneschald-update` scheduled task → ff-pull → graceful restart). Runtime state lives in
gitignored `seneschal/state/`.

**Multi-session awareness:** a session registry (`state/sessions/`) tracks every live Claude Code
session on the box — the daemon defers non-piercing nudges into a live interactive `/assistant` chat
(daemon/desktop sources; build sessions are awareness-only) and skips the redundant comms-peek. The
machine-wide `session_stamp.py` hook (installed in the user's `~/.claude/settings.json`, never shipped
here — see `scripts/SCHEDULING.md` → "Session registry hooks") stamps sessions and, on SessionEnd,
fire-and-forgets `mini_dream.py`, which distills the transcript into
`state/session-distillations.jsonl` — the cross-instance memory Dream compacts nightly.

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

`.github/workflows/ci.yml` runs on push/PR to `main` and `develop`: byte-compiles every
tracked `.py`, runs the unittest suite under `seneschal/scripts/`, checks uv.lock consistency,
validates autonomy-config.json, and runs the UUID placeholder guard. `android.yml` builds the
Call Shield app on `phone/android/**` changes. The phone Worker has its own npm gates
(`cd phone && npm run typecheck && npm test`). Reproduce the Python checks locally:

```
git ls-files '*.py' | xargs python -m py_compile
python -m unittest discover -s seneschal/scripts -p "test_*.py"  # from the repo root
python seneschal/scripts/check_placeholders.py
```
