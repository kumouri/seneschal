# 🏰 seneschal

[![made by kumouri](https://img.shields.io/badge/made%20by-kumouri-8e00ff?style=flat-square)](https://github.com/kumouri)
[![license: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-00ff0f?style=flat-square)](LICENSE)
[![Git Flow](https://img.shields.io/badge/workflow-git--flow-8e00ff?style=flat-square)](#-git-flow)

> A local-first, chief-of-staff AI assistant framework built as a
> [Claude Code](https://claude.com/claude-code) Skill suite — you bring the persona,
> the data store, and the channels; seneschal brings the machinery.

A *seneschal* was the steward who ran a medieval great house. This one manages your time,
screens your communications (email, Slack, calendar invites, phone), fires your reminders,
keeps a daily journal, and answers questions about your schedule, todos, projects, and notes —
running entirely on your machine, on your Claude subscription, with no hosted brain.

It is **not a conventional application**: it's a suite of Claude Code **Skills** (Markdown skill
definitions) plus a modest amount of stdlib-first Python. The "brain" is Claude Code itself; the
repo is the character, the memory, the routing, and the plumbing.

## Why it's interesting

- **One character, one gate, many channels.** A single orchestrator picks a *mode* (briefing,
  triage, reminders, journal, …), runs every turn through an ordered **Advisor Chain**
  (Spring-AI-style interceptors: trace → prioritize → orient → govern (budget) → retrieve →
  dispatch → critique → safeguard), and enforces one **act-low / ask-high** approval gate.
  Low-stakes things it just does; anything outbound or destructive it drafts and holds for you.
- **Local-first proactive loop, no server.** A resident asyncio **presence daemon** holds a warm
  chat session over Telegram/Discord, fires each reminder at its **exact configured time**, and
  runs a cheap comms-peek on a cadence — event-driven, so idle ≈ free. It's
  **subscription-billed** through the `claude` CLI (it scrubs `ANTHROPIC_API_KEY` to stay off the
  metered API), and it reloads itself when a PR merges. Two **model dials** pick the warm model
  and a delegation ceiling — a hard turn can be **delegated up** to a bigger model as a one-shot,
  quota-gated by a budget **governor** (rails metered in code, advisory knobs honest about being
  guidance).
- **Bring your own everything.** The assistant's **persona** (name, voice, demeanor), your
  **identity/timezone** (a small config file), and your **data backend** are all pluggable. Out of
  the box it's a nameless, default-Claude assistant on no store; tracked templates make it yours
  today, and guided setup wizards land in the next release wave.
- **Pluggable system of record.** Notion, an **Obsidian vault**, or a plain **Markdown folder** —
  behind one seam. Skills speak six backend-neutral verbs (`store-query/get/create/update/append/
  search`); each `store/<backend>/` pair maps those verbs to that backend's tools. A stated goal
  is to grow this into *one place that holds the truth across scattered tools* — see
  [`federation.md`](seneschal/store/federation.md).

## Quickstart

Requires [Claude Code](https://claude.com/claude-code) and Python 3.11+.

```bash
git clone https://github.com/kumouri/seneschal
cd seneschal
uv sync            # optional venv for the daemon deps (websockets, tzdata); everything degrades without it
```

> **Windows note:** a couple of the skill paths are deeply nested. If you clone into an already-deep
> folder and hit a "Filename too long" checkout error, enable long paths once —
> `git config --global core.longpaths true` — or clone somewhere shorter (e.g. `C:\src\seneschal`).

Then, inside Claude Code in the repo:

```
/assistant         # open a chat with your assistant  (or /assistant what's on today)
```

Out of the box that's a working, nameless default-Claude assistant on no store. To make it yours
today, copy the tracked templates ([`persona/README.md`](persona/README.md) and
[`seneschal/store/README.md`](seneschal/store/README.md) walk through persona/identity and backend
setup). The guided **setup wizards** (`/setup` — a persona interview + store onboarding) land in the
**next release wave**.

## What's in the box

| Component | What it is |
|---|---|
| **Orchestrator** ([`seneschal/SKILL.md`](seneschal/SKILL.md)) | The conductor: modes, execution rules, the Advisor Chain, the approval gate, memory. |
| **Subagent skills** ([`subagents/`](subagents/)) | Morning briefing, end-of-day wrap, email/Slack triage, calendar steward, store Q&A, reminders, a generic daily-journal steward, a person-centric message archiver, and the Forge (mints persistent "Archon" staff agents). |
| **Presence daemon** ([`seneschal/scripts/presence.py`](seneschal/scripts/presence.py)) | The always-on reactive core — warm chat, reminders, comms-peek. Stdlib-first asyncio. |
| **Cockpit** ([`cockpit/`](cockpit/)) | A local-first web observatory over the daemon: live chat over the daemon pipe, model dials, budget thresholds, health panels, archon tiles. FastAPI backend + Vite/React frontend, `127.0.0.1`-only, dev-no-auth build (real auth is a deferred follow-up). |
| **Persona + store layers** ([`persona/`](persona/), [`seneschal/store/`](seneschal/store/)) | The pluggable identity and backend layers, with tracked templates. (The guided setup wizards land in the next release wave.) |
| **Voice call-screener** ([`phone/`](phone/)) | A Cloudflare Workers + Twilio screener that fronts your phone line in the assistant's voice, plus a **Call Shield** Android app feeding presence/health signals. |
| **Local RAG + salience** ([`seneschal/scripts/rag_*.py`](seneschal/scripts/)) | A free, local semantic index (Ollama + stdlib sqlite) over your journal/notes, and an observe-only "what's safe to forget" experiment. |

Setup guides for each integration (email, Telegram, Discord, Google, Home Assistant, health,
scheduling) live in [`seneschal/scripts/*_SETUP.md`](seneschal/scripts/).

## Architecture, briefly

The orchestrator **delegates, doesn't duplicate** — it loads a subagent's `SKILL.md` and runs it
rather than reinventing the logic. Every substantive turn writes a **Run Log** and updates
**carry-over**, so the next run (or a restart) picks up where the last left off. A nightly
**Dream** run consolidates the day, pre-stages the morning brief, and proposes gated "learnings"
you approve before they change any behavior. Memory logs are local-first and gitignored; the
configured store is the durable system of record.

## Contributing / development

- **Git Flow**: `main` (tagged releases) + `develop` (integration) + short-lived `feature/*`.
  PRs merge with **merge commits**; never merge red CI.
- **Stdlib-first Python**, two sanctioned deps (`websockets`, `tzdata`) — everything degrades
  gracefully without the venv.
- **CI** byte-compiles every `.py`, runs the unittest suite, checks the uv lockfile, validates
  config, enforces that no real identifiers ship (all UUIDs must be `00000000-…`
  placeholders), and gates the cockpit (backend unit tests incl. the duplicated-table parity
  tripwire; frontend typecheck + build). Reproduce locally:

  ```bash
  git ls-files '*.py' | xargs python -m py_compile
  python -m unittest discover -s seneschal/scripts -p "test_*.py"
  ```

Operating notes for working *in* the repo are in [`CLAUDE.md`](CLAUDE.md).

## 🌿 Git Flow

This repo follows the [Git Flow](https://nvie.com/posts/a-successful-git-branching-model/)
branching model: **`main`** (stable, tagged releases), **`develop`** (integration), and
short-lived `feature/*`, `release/*`, `hotfix/*` branches. Release tags are prefixed with `v`.

## Origin

Seneschal is the de-personalized, open-source distillation of the author's private
chief-of-staff assistant — the framework, with the person removed.

## 🪪 License

[Apache-2.0](LICENSE) © 2026 Ceryce Armstrong

---

<sub>Built with care by **Ceryce** ([@kumouri](https://github.com/kumouri)) · brand purple `#8e00ff`</sub>
