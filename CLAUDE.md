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
  a documented reason; everything must degrade gracefully without the venv.
- **Docs stay in sync with code** — when code changes, correct the docs that describe it in
  the same change.

## CI

`.github/workflows/ci.yml` runs on push/PR to `main` and `develop`: byte-compiles every
tracked `.py`, runs the unittest suite under `seneschal/scripts/`, and runs the UUID
placeholder guard. Reproduce locally:

```
git ls-files '*.py' | xargs python -m py_compile
python -m unittest discover -s seneschal/scripts -p "test_*.py"  # from the repo root
python seneschal/scripts/check_placeholders.py
```
