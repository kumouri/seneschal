---
name: setup
description: >-
  The unified first-run wizard. Sequences every setup chapter — preflight, persona, store,
  owner interview, auth + model dials, the env walker, MCP servers, cockpit, the daemon,
  the closing verify/doctor pass — through one resumable ledger, so a crashed / restarted / partial setup picks up where it left off
  instead of starting over. Use for "/setup", "resume setup", "finish setting up", or
  "/setup <chapter>" to jump straight to one chapter. Re-runnable forever: done chapters
  summarize and offer a redo.
compatibility: >-
  Orchestrates the chapter files under subagents/setup/chapters/ plus the persona-wizard and
  store-setup skills. All state lands in gitignored files (seneschal/state/setup-state.json,
  *.env, persona/*, store/config.json). Needs only Python 3.11+ and a git checkout; each
  chapter declares its own extras and degrades honestly without them.
---

# Setup wizard (`/setup`)

One guided, resumable walk from a fresh clone to a working assistant. The wizard is a
**sequencer**: it reads the ledger, finds the first unfinished chapter, loads that chapter's
file, and runs it — one chapter at a time, one question at a time. The chapters own their
domain logic; this skill owns the order, the ledger conventions, and the resume rules.

## Ground rules (every chapter inherits these)

- **Everything is skippable.** Any question, any chapter. A skipped chapter is marked
  `declined` with an honest one-line note of what won't work — never a silent gap. The
  framework runs on the shipped defaults (default-Claude persona, no store, Telegram-less)
  even if the owner skips it all.
- **One question at a time, defaults shown.** This is a conversation, not a form. Offer the
  vanilla default; Enter/skip keeps it.
- **Act-low locally; per-step confirmation for anything that installs or registers.** Writing
  gitignored local files is act-low. Running an installer (`winget`/`brew`/a curl script),
  `uv sync`, `npm ci`, `claude mcp add`, or anything that opens an OAuth browser tab gets a
  named, per-command confirmation first.
- **Secrets are handled, never surfaced.** A secret value is pasted once into the chat,
  acknowledged without repeating it, delivered to `setup_env.py` via its stdin payload (never
  on a command line), and never echoed, logged, or written anywhere but its gitignored env
  file. Presence checks report *set / not set*, never the value. The ledger stores statuses,
  paths, and hashes only.
- **Identity-neutral.** Never assume the owner's name, pronouns, platform, or tools — detect
  or ask.
- **Tri-platform.** Every command has a Windows / Linux / macOS story; chapters detect the
  platform from the ledger (`preflight` records it) and show the right variant.

## The ledger

`seneschal/state/setup-state.json` (gitignored), driven entirely through
`seneschal/scripts/setup_state.py` — never hand-edit the JSON mid-run.

- **Board at entry.** Every wizard session starts:
  `python seneschal/scripts/setup_state.py infer` (reconcile with reality first — it promotes
  chapters whose artifacts already exist to `done (inferred)`, so pre-wizard hand-setup is
  respected, and demotes `done` chapters whose artifacts vanished to `stale`), then
  `python seneschal/scripts/setup_state.py board` (the aligned status table — show it).
- **Artifacts are ground truth.** Each chapter records the repo-root-relative files it
  produced (`--artifacts`); `infer` reconciles status from their presence. When a chapter's
  record and reality disagree, reality wins.
- **Every chapter marks itself.** On entry:
  `python seneschal/scripts/setup_state.py mark <chapter> in-progress --step "<where>"`;
  on exit: `mark <chapter> done|declined|blocked|awaiting-auth-restart --summary "<one line>"
  [--artifacts p1,p2] [--hash-artifacts]`. The `--step` cursor is what makes a mid-chapter
  crash resume inside the chapter, not at its top.
- **Resume point** = the first chapter, in the order below, whose status is `pending`,
  `in-progress`, `awaiting-auth-restart`, or `stale`. `declined` and `blocked` chapters are
  *not* resume points — mention them in one line when showing the board and move on (the
  owner can jump to them explicitly).
- **`/setup <chapter>` jumps** straight to that chapter id (any id on the board — `persona`,
  `env:telegram`, `mcp:slack`, …), regardless of its status.
- **Done chapters summarize + offer redo.** Entering a `done` chapter never re-runs the
  interview: state the recorded summary in a line or two and ask whether to redo it. Only an
  explicit yes re-runs it.
- **Wizard facts** (`features` / `deps` / `models`) have no CLI subcommand; chapters write
  them through the module API — the canonical pattern (no secrets ever go in these):

  ```
  python -c "import sys; sys.path.insert(0, 'seneschal/scripts'); import setup_state; s = setup_state.load(); s['deps']['ollama'] = {'status': 'ready', 'url': 'http://localhost:11434', 'models': []}; setup_state.save(s)"
  ```

## Chapters, in order

| # | Chapter id(s) | File | Configures | Artifacts |
|---|---|---|---|---|
| 1 | `preflight` | `chapters/preflight.md` | platform + tool inventory, repo sanity, ledger init | — (ledger only) |
| 2 | `persona` | `../persona-wizard/SKILL.md` | the assistant's name, voice, demeanor, channel identities | `persona/identity.json`, `persona/persona.md` |
| 3 | `store` | `../store-setup/SKILL.md` | the data backend (Notion / Obsidian / Markdown) + provisioning | `seneschal/store/config.json` |
| 4 | `owner-interview` | `chapters/owner-interview.md` | who the owner is — profile, timezone, habits, escalation | `persona/owner-profile.md` |
| 5 | `auth-models` | `chapters/auth-models.md` | subscription token presence, API-key hygiene, the two model dials | `seneschal/state/model-config.json` |
| 6 | `env:telegram` | `chapters/env-walker.md` | the **required** core chat + push channel | `seneschal/scripts/telegram.env` |
| 7 | `env:<id>` (each optional manifest entry) | `chapters/env-walker.md`, with `chapters/ollama.md` inline on demand | proton, google, discord, router, rag, sentiment, ha, push-call, push-sms | that entry's `path` from the manifest |
| 8 | `mcp:notion`, `mcp:calendar`, `mcp:slack` | `chapters/mcp.md` | MCP servers + their OAuth (incl. the manifest's `handled_by: mcp` configs) | user-scope registrations; `seneschal/scripts/slack-mcp.json` / `notion-mcp.json` when Path B |
| 9 | `cockpit` | `chapters/cockpit.md` | the local web observatory (optional) | `cockpit/server/cockpit.env` (+ built `cockpit/web/dist`) |
| 10 | `daemon` | `chapters/daemon.md` | launcher locals + platform registration (Task Scheduler / systemd user units / launchd), the session hooks, the resident daemon | `seneschal/state/setup/` renders (the chapter records the platform's actual files) |
| 11 | `verify` | `chapters/verify.md` | the end-to-end doctor pass (`setup_doctor.py` board + in-session MCP probes) + the send-off | — (stamps the ledger's `doctor_last` block) |

Rows 6–7 are both the **env walker** — one generic chapter driven by
`seneschal/setup/env-manifest.json` (one ledger chapter per entry, id `env:<id>`). The
**required-before-optional rule**: never open an optional chapter while a *required* one
(today: `env:telegram`) is still `pending`/`in-progress`/`stale` — offer the required one
first (the owner may still decline it; `declined` is a resolution, not a gap). The manifest
lists required entries first (CI-enforced), so file order is walk order.

**Missing chapter files** (vestigial — every chapter file ships today; the rule stays for
any future not-yet-landed chapter): if a chapter's file does not exist, do not improvise
its content. Mark it `blocked --summary "chapter lands in a later PR"` and say so plainly —
the board stays honest, the wizard stays shippable, and when the file appears a re-run
picks the chapter up (a jump to it, or `infer`-then-resume). Never treat an absent chapter
file as an error.

## Composition contract

- **`persona` owns `assistant.*`** in `persona/identity.json` and `persona/persona.md`.
- **`owner-interview` owns `owner.*`** in `persona/identity.json` and
  `persona/owner-profile.md` (the `store` chapter *produces* the candidate-facts sheet the
  interview consumes, and hands off — see its Phase 5).
- **Whoever runs second confirms shared fields** (owner name, pronouns, timezone) rather than
  re-asking, and never overwrites the other chapter's fields without showing what would
  change. Merge `identity.json`, never clobber it.

## Running a chapter

1. `mark <chapter> in-progress` (with a `--step` cursor as it moves).
2. Read the chapter file and follow it exactly — the chapter is the authority on its domain;
   this skill never duplicates chapter logic.
3. On exit, the chapter marks its own terminal status with a one-line `--summary` a future
   board reader will thank you for ("Telegram wired + verified end-to-end", "declined —
   email triage falls back to Gmail drafts").
4. Between chapters: one line of progress ("3 of 9 done — next: the store backend"), then
   continue. No recaps, no ceremony.
5. At the end of the walk: the `verify` chapter (row 11) owns the close — the doctor pass,
   the combined board, and the send-off.

## Guardrails

- Act-low throughout **except** the per-step-confirmed installs/registrations named above.
  Nothing outbound, ever — setup talks to the owner, not to their contacts.
- Never fabricate a verify result: run the entry's real verify command and report what
  actually happened. A chapter with a failing verify is `blocked` with the (secret-safe)
  error, not `done`.
- Never store a secret in the ledger, a summary, a step cursor, or a chat-visible command.
- Respect pre-wizard work: `infer` before assuming anything is unconfigured, and merge-write
  env files (that's `setup_env.py`'s default) so a hand-set var survives a re-run.
