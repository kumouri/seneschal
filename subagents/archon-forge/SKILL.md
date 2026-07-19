---
name: archon-forge
description: >-
  The assistant's Archon Forge. Mints, deploys, and curates its specialized staff — persistent
  "Archon" agents (e.g. a job-application archon) — by driving the sibling Demiurge meta-agent
  (mint → eval-gated admission → A2A delegation → outcome-based tenure) with the subscription-billed
  claude-cli runtime adapter. Use for "hire/mint an archon", "commission a specialist", "delegate to
  <archon>", "how's my staff doing", "retire/revise an archon". Delegated to by the seneschal
  orchestrator (Forge mode).
compatibility: >-
  Requires the sibling demiurge repo (uv-managed; path + roster in
  ../../seneschal/references/archons.md) and the `claude` CLI (subscription auth). Archon lifecycle
  data lives in this repo under archons/ (needs/stables gitignored on public installs; tools +
  examples tracked). Reads the persona + references.
---

# Archon Forge (Seneschal · Forge mode)

Run the assistant's staff. A recurring job that has *earned* a persistent specialist gets an
**Archon**: the assistant frames the need, Demiurge mints it (spec + charter + evals + record), the
claude-cli adapter deploys it A2A-addressable on the **subscription** (never the metered API), the
eval gate admits it, and every delegated task lands in its ledger for tenure review. Reading the
stable, drafting a need, and **deploying / admitting / delegating** are **act-low** (the owner's
standing authorization — "spin up archons as needed"; the claude-cli adapter is subscription-billed,
so the spend that gate guarded was never real). **Minting, revising, and retiring stay ask-high** —
they change *who is on the staff*, which is a roster judgment, not a spend one.

## Read first

- `../../persona/persona.md` (else `../../persona/persona.default.md`) — voice; Archons are the
  assistant's staff, never its voice.
- `../../seneschal/references/archons.md` — the demiurge path, directory layout, port registry, the
  command crib sheet, and the **roster** (one row per Archon).
- `../../seneschal/references/autonomy-policy.md` — act-low vs ask-high (Forge entries).
- `../../seneschal/references/memory.md` — leave a trace (Run Log + carry-over; held Forge actions
  ride the normal approval loop with `kind: "archon"`).

## Steps

**Phase 0 — Orient (act-low).** Read `archons.md` (roster + ports) and, for an existing Archon, its
`archons/stable/<id>/record.json`, `eval-report.json`, and the `archons/<id>/state/ledger.jsonl` tail
(the ledger sits with the archon's runtime state, **not** in the tracked stable — see `archons.md`).
Status questions ("how's my staff doing", "tenure review") are answered here — reads only, cite the
files.

**Phase 1 — Frame the need (act-low, the judgment step).** A new specialist starts as a draft
`archons/need/<id>.need.yaml`. Hold the persistence bar: `why_persistent` must argue why this
recurring job earned a curated staff member over a one-off run of the assistant itself — if it
can't, say so and offer to just do the task instead. Grant tools **least-privilege**: `local_tools`
for CLI built-ins (prefer scoped specifiers like `Bash(python *)` over bare `Bash`), `tool_grants`
for MCP servers, and a real `budget` (`max_duration_seconds` is the hard rail on this runtime).
Show the owner the draft need.

**Phase 2 — Mint (ASK-HIGH).** On the owner's go-ahead: `demiurge mint` against **this repo's**
stable (`archons/stable/`). Show them the generated charter — it, not the conversation, is the
scope authority from here on. Add the Archon's row to the `archons.md` roster (id, port, status).

**Phase 3 — Scaffold + wire (act-low).** `demiurge scaffold … --adapter claude-cli` into
`archons/scaffolds/` (gitignored, regenerable). If the spec has MCP grants, copy the seeded
`mcp-servers.example.json` → `mcp-servers.json` and fill real connections. Private inputs an Archon
needs (profiles, watchlists) live under `archons/<id>/` per `archons.md` — never in the scaffold,
never in the public demiurge repo. The shipped `archons/proteus/` (a job-application archon's
tools + `profile.example.json`) is the worked example of this layout.

**Mint the home directory with the standard layout** — do this now, not once the archon has already
scattered state around (`archons.md` → *"Archon internal layout"*). Every archon gets:

```
archons/<id>/
  .gitignore        state/* (+ !state/README.md), out/, *.env (+ !*.env.example),
                    __pycache__/, *.log, *.pid   ← copy archons/proteus/.gitignore as the template
  state/README.md   documents each runtime file; keeps the dir present
  out/              deliverables (created on first run)
  tools/            if it has any
```

- **`state/` = runtime churn** (ledgers, latest-caches, counters, queues, sentinels, logs) —
  anything a run rewrites every cycle. **`out/` = deliverables** — what a run produces for the owner.
- **That includes demiurge's own delegation ledger.** Pass
  `--ledger-dir <M>\archons\<id>\state` on **every** `delegate`/`verdict`/`distill`/`tenure` — the
  same path on all four, or a verdict can't find the delegation it judges. Left in the tracked
  stable it churns a live checkout dirty (arming the `pull --ff-only` reload failure) and writes
  every request/response into git history. Rationale + crib: `../../seneschal/references/archons.md`.
- The **per-archon `.gitignore` is the point**: it makes the split travel with the archon wherever
  it's checked out, rather than depending on this repo's root ignore file.
- If the archon has more than one tool, give it **one paths module** they all import
  (`tools/<id>_paths.py`, modeled on `proteus_paths.py`) so they can't disagree about where state lives.

**Phase 4 — Deploy + Admit (act-low, standing authorization).** Deploy on the Archon's registered
port (detached; `demiurge deploy` blocks in the foreground) and run `demiurge admit` — every eval
case is a subscription-billed CLI turn, which the owner has budgeted for. Report the case-by-case
results. A failed gate goes back to Phase 1 as a `demiurge revise` (**still ask-high** — it changes
the roster). Tear the process down when the session's work is done — Archons don't idle.

**Phase 5 — Delegate + Curate (act-low, standing authorization).** `demiurge delegate` with a
self-contained request (inline the private context it needs — profile, paths, dates; the spec's
instructions say *how*, the request says *what/with-what*). Afterward record the outcome
(`demiurge verdict`), distill any failure into a regression eval (`demiurge distill`), and check
`demiurge tenure` when the ledger has history. Anything the Archon *drafts* for the outside world
(email, application, message) comes back to **the assistant's** normal gate — the Archon never
sends.

**Phase 6 — Trace.** Substantive Forge runs write a Run Log entry (Mode: Forge) + carry-over for
any held approvals or a mid-lifecycle Archon.

## Guardrails

- **Subscription only.** Always `--adapter claude-cli`; never `claude-sdk` (metered API — the wrong
  cost model for a personal assistant). Never set or pass `ANTHROPIC_API_KEY`; the adapter and
  daemon both scrub it. Unattended auth is `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`.
- **Private data stays home.** The public demiurge repo gets code only. The owner's needs, stables,
  ledgers, profiles, and outputs live in this repo's `archons/` (gitignored) — never commit them to
  demiurge, never push them anywhere.
- **The persistence bar is real.** No honest `why_persistent`, no mint — Demiurge refuses, and so
  does the assistant. Offer the cheaper shape (just do it, or a one-off headless run).
- **Charter is the scope authority.** Work outside an Archon's charter is a new need (or the
  assistant's own), never a stretched delegation.
- **Spend is authorized, not unlimited.** The owner has budgeted the subscription turns, so
  deploy/admit/delegate no longer need a per-action go-ahead — but they still run on the same
  subscription the owner chats on, and the spec's `budget` rails (`max_duration_seconds`,
  `max_steps`, `max_token_usage`) are hard limits a fat delegation will hit and truncate. Batch
  delegations to fit the rails, keep eval suites lean, and wind Archons down after use.
- **One character, one gate.** Archons are staff with their own names; they never speak as the
  assistant or as the owner, and nothing they produce goes outbound except through the assistant's
  draft-and-hold gate.
- **Leave the demiurge repo green.** If a Forge run changes demiurge itself (rare), its gates run
  (**all three**: `uv run pytest`, `uv run ruff check .`, **and `uv run ruff format --check .`**)
  before the change lands — and via its normal PR flow. The format gate is separate from the lint
  gate and CI runs both; passing `ruff check` says nothing about `ruff format --check` (a PR once
  went up red on every Python version because only two of the three ran locally).
