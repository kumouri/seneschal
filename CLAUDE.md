# CLAUDE.md

Operating guide for Claude Code working in the **seneschal** repo.

## What this repo is

Seneschal is a **local-first chief-of-staff assistant framework** — a Claude Skill suite
(orchestrator + subagent skills, mostly Markdown) plus a modest amount of stdlib-first
Python (presence daemon, comms bridges, local RAG). It is the open-source, de-personalized
framework: the assistant ships with a default personality, a persona wizard builds a custom
one, and the data store (Notion / Obsidian / Markdown folder) is pluggable behind a schema
registry.

**Status: under construction.** Content lands phase by phase on `develop`; this file grows
with it. Until v0.1.0, expect referenced components to be missing.

## Layout (so far)

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
                   rag_* (local semantic index), router.py, salience tooling, health/presence
                   pipelines, archive_common.py + telegram_ingest.py + discord_export_ingest.py
                   + sms_ingest.py + archive_aggregate.py (message archiver),
                   check_placeholders.py (CI guard), *_SETUP.md guides,
                   seneschald-control.ps1 + run-*.cmd (Windows scheduled-task wrappers)
  state/           local-first runtime cache — gitignored except README + *.example.*
```

subagents/         morning-briefing, eod-wrap, email-triage, slack-triage, calendar-steward,
                   store-qa, reminders, message-archivist, journal-steward (generic core),
                   archon-forge — the delegated skills the orchestrator dispatches to
phone/             the voice call-screener (Cloudflare Workers + Twilio; deploys as
                   `seneschal-screener`) + the Seneschal Call Shield Android companion app
                   (com.kumouri.seneschal, committed Gradle project) feeding presence/health
archons/           Archon staff data (Forge mode) — the shipped `proteus/` job-application
                   example (profile.example.json + stdlib tools); real needs/stables/profiles
                   are gitignored on installs

Still to land: the `/setup-persona` wizard (the `persona/` surface + identity plumbing are in) and the
`/setup-store` flow (the `seneschal/store/` registries + backends are in; the skills + daemon now speak
the abstraction — `/setup-store` writes the gitignored `store/config.json` + renders the real
`store/<backend>/schema.md`).

## The daemon, briefly

`seneschal/scripts/presence.py` is the always-on nerve center: a reactive asyncio core holding a
warm `claude` CLI chat session (subscription-billed; it scrubs `ANTHROPIC_API_KEY`), firing
reminders from the local queue, and running a cheap Watch comms-peek on cadence. It forwards the
**store's MCP** (if any) into every spawned headless `claude` — resolved store-config-driven
(`--store-mcp` override → `store/config.json`'s active backend → legacy `scripts/notion-mcp.json` →
None; filesystem backends need none, `resolve_store_mcp`). It runs off `main` and reloads itself when a
PR merges (`seneschald-update` scheduled task → ff-pull → graceful restart). Runtime state lives in
gitignored `seneschal/state/`.

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
