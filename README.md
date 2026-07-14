# 🏰 seneschal

[![made by kumouri](https://img.shields.io/badge/made%20by-kumouri-8e00ff?style=flat-square)](https://github.com/kumouri)
[![license: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-00ff0f?style=flat-square)](LICENSE)
[![Git Flow](https://img.shields.io/badge/workflow-git--flow-8e00ff?style=flat-square)](#-git-flow)

> A local-first, chief-of-staff AI assistant framework built as a
> [Claude Code](https://claude.com/claude-code) Skill suite — you bring the persona,
> the data store, and the channels; seneschal brings the machinery.

A *seneschal* was the steward who ran a medieval great house. This seneschal manages your
time, screens your communications (email, chat, calendar invites, phone), fires your
reminders, keeps a daily journal, and answers questions about your schedule, todos,
projects, and notes — running entirely on your machine, on your Claude subscription,
with no hosted brain.

**Status: under construction.** The framework is being assembled phase by phase; see the
commit history on `develop`. A v0.1.0 release will mark the first complete cut.

## What's in the box (when it's done)

- **An orchestrator + subagent Skill suite** — briefing, end-of-day wrap, triage, reminders,
  Q&A, journal, and more, coordinated through a mode-aware conductor with an
  act-low / ask-high autonomy policy.
- **A resident presence daemon** — a stdlib-first Python asyncio core that holds a warm chat
  session (Telegram / Discord), fires reminders on time, and wakes the assistant on a
  schedule. Subscription-billed via the `claude` CLI; no metered API.
- **A persona wizard** — the assistant ships with a default personality and no name; an
  interview builds the character you actually want.
- **Pluggable data stores** — Notion, an Obsidian vault, or a plain Markdown folder as the
  system of record, behind one schema-registry seam.
- **A voice call-screener** (Cloudflare Workers + Twilio) and an Android presence/health
  companion app.

## 🌿 Git Flow

This repo follows the [Git Flow](https://nvie.com/posts/a-successful-git-branching-model/)
branching model: **`main`** (stable, tagged releases), **`develop`** (integration), and
short-lived `feature/*`, `release/*`, `hotfix/*` branches. Release tags are prefixed with
`v`.

## 🪪 License

[Apache-2.0](LICENSE) © 2026 Ceryce Armstrong

---

<sub>Built with care by **Ceryce** ([@kumouri](https://github.com/kumouri)) · brand purple `#8e00ff`</sub>
