# Pluggable backend spec — running the assistant on something other than `claude -p`

**Status:** `PARTIAL(Phase 2 BUILT — the Backend contract, CodexWarmSession, the cockpit's third dial, all behind a config flag defaulting to claude-cli; Phases 3-5 and §4's pre-classifier router path unbuilt)`
— the contract, the first second backend and its cockpit selector ship; the daemon still runs
`claude-cli` until the owner flips the dial, and dual-active routing, the grounding projection, the
host-confirmed run and the pre-classifier path are specified here but not built.

**§6 (Phase 3 — dual-active backends with dynamic routing)** sits on top of the `Backend` contract
and `CodexWarmSession` that §3 names — that pair's concrete build is this document's Phase 2 (below),
which §6 builds on and does not re-design.

**The goal, stated as a requirement:** anyone with a subscription-based AI CLI — one that can run
headless against a logged-in subscription, *not* one that requires an API token — should be able to
run the framework. The first concrete step toward that is one second backend beside `claude -p`:
OpenAI's Codex CLI on a ChatGPT subscription, both because it is the one door that clears the
no-token bar today (§2) and because running the same assistant on a different model family is itself
the most direct way to learn how the assistant's behavior depends on the model under it (§3.6).

**The first build decision:** asked which of §3.5's phases to build first — the `Backend` contract +
`CodexWarmSession` + cockpit dial, a fuller abstraction pass, or stopping at spec-only — the owner
chose **the Codex CLI adapter plus the cockpit dial**. That build is Phase 2: §3.1's contract named
explicitly (the `seneschal/scripts/backends/` package — `__init__.py` holds backend selection,
`claude_cli.py` is the reference implementation, `WarmSession`/`StubWarmSession` moved
byte-for-behavior-identical out of `presence.py`, and `codex_cli.py` is the first second
implementation), §3.2's cockpit dial (a third `backend` field in `seneschal/scripts/model_config.py`
and its cockpit twin, a frontend selector), and the safety posture §3.1/§1.4 required (no MCP grant
reachable from the codex backend at all, sidestepping Codex's own unverified `PreToolUse`-MCP-coverage
question rather than resolving it). The live daemon's backend default stays `claude-cli`; switching is
the owner's own tap in the cockpit, not a merge's to do. §3.3 (`orientation_project.py`/`AGENTS.md`),
§3.4's remaining setup steps beyond MCP (a Codex host-setup guide), and §4 (the pre-classifier router
path) remain unbuilt — separate, later work.

**This is a framework requirement, not a per-install feature**: the framework-level goal is *any*
subscription-billed backend that doesn't require an API token; the narrower thing this document
actually scopes toward a build is one second backend, the Codex CLI on a ChatGPT subscription.

## Router entry

`seneschal/docs/CLAUDE.md`, "Whole-repo designs", carries this file's one-line entry, per that
section's own convention — the status token above, then the gloss: the `claude -p` seam, exhaustively
inventoried (`WarmSession`, the headless spawn sites, hooks, `--mcp-config`, the model-dial rank
table); the codex session structurally cannot mount an outbound MCP server at spawn time; §6 specs
dual-active claude-cli + codex-cli with dynamic routing. Keep that entry and the status line here in
step — `check_doc_status.py` enforces the token.

---

## 1. The seam — every place the code assumes the backend is the `claude` CLI

This section is the core of the document: exhaustive, read from the code rather than from memory or
from what a spec *should* say, grouped by subsystem. Citations name the **file and the function or
constant**, never a line number — line numbers drift on every edit to a file the size of
`presence.py`, and the pointer that must stay resolvable is the file (which
`check_context_pointers.py` checks) plus a name a reader can grep for.

### 1.1 The warm session itself — `seneschal/scripts/backends/claude_cli.py` (formerly inside `presence.py`)

- **`class WarmSession`** — "One resident `claude` process in stream-json mode = a warm in-RAM
  session across turns." Its `__init__` takes `claude_bin` directly as its first constructor argument
  — the CLI binary path is not an implementation detail hidden inside the class, it is the class's own
  identity. Phase 2 moved it (with `StubWarmSession`) out of `presence.py` into
  `seneschal/scripts/backends/claude_cli.py` unchanged in behavior; `presence.py` now reaches it only
  through the `backends` package's selection function.
- **`start()`** builds the literal argv:
  ```
  [claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
   "--verbose", "--permission-mode", <mode>, "--mcp-config", <configs...>, "--model", <model>,
   "--resume", <session_id>]
  ```
  Every one of these flags is Claude-CLI-specific syntax: `--input-format`/`--output-format
  stream-json` is the bidirectional streaming protocol this whole daemon is built around (see the next
  bullet); `--permission-mode` and `--mcp-config` are Claude-Code-CLI flags with no assumed equivalent
  shape elsewhere; `--resume <id>` depends on the CLI's own session-id semantics (verified empirically:
  `--resume <id>` works in `-p --input-format stream-json` and **reports the same session_id back** —
  an *undocumented* behavior the daemon depends on, characterized once by a hand-run probe against
  the real CLI rather than by any vendor contract).
- **stream-json parsing.** `send()` reads the child's stdout line-by-line and parses each as a
  stream-json event (`system`, `assistant`, `tool_use`/`tool_result` inline in the `assistant`
  message's content blocks, `result` terminal). The daemon's stream tee names this directly: turning
  "a claude-CLI stream-json line into a digestible chat.event." This event shape — and the terminal
  `result` event's `usage`/`num_turns`/`total_cost_usd` fields that the warm session's observability
  scalars are read from (`last_usage`, `last_num_turns`, `context_tokens`, `session_cost_usd`,
  `last_turn_cost_usd`) — is Claude-CLI's own wire format. Nothing downstream (the cockpit's
  cost/context gauges, the Trace panel, `spend_levers.py`) reads anything but this shape.
- **`class StubWarmSession`** is the offline test double — and its very existence is the strongest
  evidence in this codebase that a `Backend` abstraction was *already latent*: it duck-types
  `WarmSession`'s full surface (`session_id`, `model`, `turns`, `closed`, `resume_session_id`,
  `resumed`, `resume_failed`, `timed_out`, `age_sec()`, and the observability scalars) with zero
  subprocess underneath. §3's `Backend` contract formalizes exactly this already-shared shape rather
  than inventing a new one.
- **`make_session()`** (in `presence.py`) is the one factory every session goes through — before
  Phase 2 it was `sess = StubWarmSession(...) if args.stub_brain else WarmSession(args.claude_bin,
  model, ...)`. This is the seam a `Backend`-selecting third branch plugs into, and it is where
  Phase 2's selection landed: the stub branch unchanged, otherwise the backend the model dial names.
- **Account identity.** The daemon reads `{account_uuid, organization_uuid, email}` out of
  `~/.claude.json`'s `oauthAccount` — a Claude-Code-CLI-specific config file and field — resolved via
  `CLAUDE_CONFIG_DIR` or the default `~/.claude.json`. A boot-time identity is held and compared every
  heartbeat to decide whether the running process is now authenticated as someone else — the same
  mechanism `seneschald-control.ps1`'s `Update` action reads from the PowerShell side (§1.9).
- **Billing-safety env scrub.** `child_env()` — "billing safety: never let a stray key force metered
  API" — scrubs `ANTHROPIC_API_KEY` from every spawned child. This is Claude-subscription-specific:
  the entire reason it exists is that the `claude` CLI silently prefers an API key over the logged-in
  subscription if one is present in the environment (`presence.py`'s module docstring: the key is
  scrubbed so a stray one can never switch the assistant to metered API billing; for an unattended
  service, authenticate once with `claude setup-token` and set `CLAUDE_CODE_OAUTH_TOKEN`). A second
  backend has its own equivalent footgun (Codex: `OPENAI_API_KEY` vs. `~/.codex/auth.json`; see §2.1)
  and needs its own scrub, not a reuse of this one.
- **MCP grants are wired as CLI flags, not files a second CLI would read.** `active_mcp_configs(args)`
  resolves the store's MCP (and optionally Slack) config paths and every spawn site passes them as
  `--mcp-config <path> [<path>...]` — "the CLI takes multiple space-separated configs after one
  `--mcp-config`." This is a per-invocation CLI argument, not a standing config file Codex's
  `~/.codex/config.toml` `[mcp_servers.*]` tables could simply also read — a second backend needs its
  own translation of "which MCP servers this turn gets," in its own config shape (§2.1, §3).
- **Spawn-fallback and resume semantics are tuned to this CLI's own failure modes.** The two-rung
  fallback ladder in `send()` (rung 1 "resume didn't take," rung 2 "the model won't spawn") encodes
  empirically observed `claude` CLI behavior (a stale `--resume` id fails in a few seconds; a
  not-yet-available model tier fails fast) that would need re-deriving, not re-using, for another
  CLI's own resume/model-availability failure shapes.

### 1.2 Background jobs — `seneschal/scripts/jobs.py`

- **The default job shape is a bare `claude -p` invocation.** `jobs.py`'s own usage example is
  `jobs.py start --title "..." --worktree -- claude -p "<brief>"`, and the module's docstrings
  repeatedly name `claude -p` as the assumed common case, not merely a supported one — "a detached
  `claude -p` job," "a job's own `claude -p` agent can background a verification step," a field
  "absent for every job that isn't a bare `claude -p` invocation," "a job may well be a `claude -p`
  run, and those must stay subscription-billed," "`claude -p` runs whatever it is told to."
- **`job_completion.py`'s DETECT/RESUME/RESCUE path is a `claude --resume`-specific recovery path,
  gated on the literal command name.** `resume_argv` hardcodes the binary — it returns `["claude",
  "--resume", <sid>, *carried_flags, "-p", prompt]`, and its own docstring names the case it exists
  for as one whose command was never `claude -p` in the first place. The flags it carries across a
  resume are an exhaustive, named, Claude-Code-CLI-only list, split by shape: `_CARRY_SINGLE_VALUE =
  ("--permission-mode", "--model", "--settings", "--append-system-prompt")`, a variadic tuple
  (`--allowedTools`/`--allowed-tools`, `--disallowedTools`/`--disallowed-tools`, `--mcp-config`,
  `--add-dir`) and a boolean one (`--dangerously-skip-permissions`). None of these has an assumed
  Codex-CLI (or any other backend's) equivalent flag name or semantics.
  **`prepare_argv` refuses to even try for a non-`claude` job**: `if not argv or _cmd_name(argv[0])
  != "claude": return argv, None` — so a job launched with any other backend's CLI today silently gets
  NONE of the detect/resume/rescue protection, not a degraded version of it. This is a real design
  fork point for §3, not just a missing feature: a second backend either earns its own
  `resume_argv`/`prepare_argv` branch with its own carried-flag list, or the recovery path stays
  claude-cli-only and a Codex job's unfinished-background-step failure mode is simply unhandled (an
  honest, statable limitation rather than a silent one).
  The mechanism the resume itself leans on is empirically reverse-engineered, not documented: whether
  `--resume` works at all in `-p --input-format stream-json --output-format stream-json` was answered
  by a standalone hand-run probe against the real CLI, at one observed CLI version — the daemon's
  whole resume design rests on undocumented CLI behavior somebody had to characterize once, which is
  the same category of spike §3.5 Phase 1 proposes for Codex's own `codex resume`.
- **The billing-safety scrub is asserted, not just applied, for jobs specifically.** `jobs.py`: "a job
  may well be a `claude -p` run, and those must stay subscription-billed" — the same
  `ANTHROPIC_API_KEY` scrub as §1.1, applied at job-spawn time.
- **The grandchild-console Windows workaround is keyed to `claude` (= `node.exe`) by name.** On
  Windows the job shim's child `Popen` carries `CREATE_NO_WINDOW`, because a console-subsystem command
  (`claude` = node.exe, `gradlew.bat` = cmd.exe) under a console-less `DETACHED_PROCESS` shim is
  otherwise handed a fresh visible console. A Codex-CLI job (also Node-based, per its npm
  distribution) likely shares this specific defect and workaround; a Go- or Rust-distributed backend
  might not need it at all — this is a case where the *fix* generalizes and the *reason it's needed*
  does not, which the `Backend` contract (§3) should let a backend declare rather than assume.
- **`job_background_guard.py`'s scoping signal is a job-spawn env var, backend-agnostic in shape but
  Claude-Code-hook-specific in mechanism** — see §1.4.

### 1.3 The model dials — `seneschal/scripts/model_config.py` + `cockpit/server/model_config.py`

- **Before Phase 2, the `RANK` list was a closed, Claude-model-name enumeration**, duplicated
  byte-for-byte in both files: `["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8",
  "claude-opus-5", "claude-opus-5-5", "claude-fable-5", "claude-fable-5-1"]` (a later mid-rank insert
  and a top-of-chain append left `admits_fable()`'s threshold pinned to `claude-fable-5`, so it still
  means "Fable-tier or above"). `canonical()` / `rank_of()` / `admits_fable()` / `validate_pair()` all
  keyed off this one list — there was no notion of "which backend is this model id for" anywhere in
  the schema, and `state/model-config.json` itself (`{"warm_model": "claude-opus-4-8",
  "max_routable_model": "claude-fable-5", ...}`) had no backend field. **Phase 2 made `RANK` a
  per-backend dict** (`RANK["claude-cli"]` is that list unchanged, `RANK["codex-cli"]` is Codex's own
  tier order, a best-effort reading of its local models cache's descriptions and explicitly not a
  benchmark ranking) and added the `backend` field; every function takes an optional `backend`
  defaulting to `claude-cli`, so no pre-existing caller changed.
- **`admits_fable()`** is the hard gate every Fable-delegation trigger checks before doing anything —
  "the fable arm doesn't even run" when the ceiling isn't Fable-tier. Fable is a Claude-family model
  variant; this whole mechanism (§1.7's `fable_delegate.py`, the router's fable arm) has no defined
  behavior if the warm model is ever not a Claude model at all — the ceiling concept ("is X at least
  as capable as Fable") stops meaning anything across backends. Phase 2 encodes that honestly:
  `admits_fable` is unconditionally false for any backend other than `claude-cli`.
- **`cockpit/server/model_config.py`'s `LABELS`** hardcoded the Claude model display strings the
  cockpit's dial `<select>` renders — before Phase 2 the cockpit had no UI affordance for "which
  backend," only "which Claude tier." The Phase 2 dial adds the axis; the hand-duplicated tables stay
  duplicated (the cockpit spec's deliberate-duplication decision, guarded by
  `cockpit/server/test_parity.py`).

### 1.4 Hooks — every one is a Claude-Code-CLI hook, registered in `~/.claude/settings.json`

Every hook below shares one mechanism: hooks are registered in `~/.claude/settings.json`, which is
outside version control and absolute-pathed to the live checkout (the /setup wizard's
`settings_merge.py` merges the session hooks in; the guard hooks each have a `*_SETUP.md`). The
contract is Claude Code's own: the event arrives as JSON on stdin (`hook_event_name`, `session_id`,
`cwd`, `tool_name`, `tool_input`, …), the hook may print to stdout (injected into context for
`SessionStart`/`UserPromptSubmit`) and signals a decision by exit code — `0` allow, `2` deny with the
reason on stderr (`session_stamp.py`'s docstring; `send_gate_hook.py` "blocks with exit 2 and the
refusal line on stderr"). None of this is a generic convention — it is specifically Claude Code's hook
protocol.

- **`send_gate_hook.py`** — **the safety-critical one.** Registered as a `PreToolUse` hook matched
  against the Slack/Gmail MCP send-tool names (`SEND_GATE_SETUP.md`: a matcher of the shape
  `"mcp__.*(slack_send_message|slack_schedule_message|Gmail__send_message|Gmail__reply|
  Gmail__forward)"`). This is the hook half of the outbound approval gate (`send_gate.py`) — the ONE
  code mechanism that can refuse an outbound send the owner hasn't approved, refused by code rather
  than by prompt (`how-the-approval-gate-works.md`). **This is the item that needs an
  equivalent-or-refusal**: a backend whose tool-calling loop has no `PreToolUse`-shaped interception
  point cannot host this gate at all, which means it cannot be trusted to make outbound MCP tool calls
  (Slack/Gmail send) until it has one — §2's scoring treats this as a hard requirement, not a
  nice-to-have.
- **`session_stamp.py`** — machine-wide, registered on `SessionStart`/`UserPromptSubmit`/`Stop`/
  `SessionEnd` (its own docstring). Stamps every Claude Code session (not just the assistant's) into
  `state/sessions/`, and on `SessionEnd` fire-and-forgets the mini-dream distiller. There is no
  equivalent "a session started/ended" signal from a second CLI unless that CLI has its own hook
  system wired the same way (Codex CLI does have a hooks mechanism — see §2.1 — but its event names
  and stdin/stdout contract are its own, not a drop-in).
- **An `InstructionsLoaded` observer** — Claude Code fires an `InstructionsLoaded` event when a
  `CLAUDE.md` or `.claude/rules/*.md` file "enters context," and a hook on it is the only way to know
  that happened. **This is the single clearest example in the whole seam of a mechanism with no
  cross-backend equivalent even in concept**: it exists because Claude Code auto-loads `CLAUDE.md` by
  directory-traversal convention, invisibly. Codex's `AGENTS.md` loading (§2.1) is a different,
  not-yet-characterized mechanism; there is no reason to assume it fires an analogous event.
- **`job_background_guard.py`** — `PreToolUse`, matcher `"Bash"` (`JOB_BACKGROUND_GUARD_SETUP.md`),
  refuses a backgrounded Bash call in the scope of a `jobs.py`-spawned session (env var
  `SENESCHAL_JOB_ID`, backend-agnostic in principle, but the hook itself is Claude-Code `PreToolUse`
  machinery).
- **`merge_guard.py`, `bash_path_guard.py`, `branch_delete_guard.py`, `script_file_guard.py`,
  `query_shape_hook.py`** — all `PreToolUse`/`PostToolUse` hooks in the same registry, same protocol.
  None is safety-critical the way `send_gate_hook.py` is, but all assume the same interception point.

**Net finding:** every safety and observability rail this repo has built into "what the assistant
does with tools" lives in Claude Code's `PreToolUse`/`PostToolUse`/`SessionStart`/`SessionEnd`/
`InstructionsLoaded` hook surface. A second backend is not merely "a different model" from this
angle — it is a different *harness*, and the harness is where the safety rails live.

### 1.5 CLAUDE.md / persona / skills loading

- **CLAUDE.md auto-loading is implicit in the `claude` CLI itself, not code this repo owns.** There
  is no loader module to point at — the root `CLAUDE.md`, `seneschal/docs/CLAUDE.md`,
  `cockpit/CLAUDE.md`, etc. are read because the Claude Code CLI walks the directory tree from cwd and
  loads every `CLAUDE.md` it finds, by convention. An `InstructionsLoaded` observer (§1.4) is only an
  *observability* layer on top of that convention, not the convention itself. A second backend with
  no such directory-walk convention needs the equivalent grounding delivered another way — Codex's own
  answer is `AGENTS.md`, a differently-scoped, differently-discovered file (§2.1).
- **The persona-projection precedent already exists and generalizes cleanly.** The canonical persona
  lives in one Markdown file (`persona/README.md` describes the tracked default/template and the
  gitignored per-install copy), and the phone Worker's persona (`phone/src/persona.ts`) is a
  *projection* of it — a curated subset (name, role, demeanor, voice) carried to a second target, not
  a second hand-maintained copy; the /setup wizard emits the Worker's values from the canonical file.
  The mature form of that pattern is a pure extraction script with `--write`/`--check` and a CI gate so
  the projected copy can never silently drift from the source. This is the *design pattern* §3.3
  proposes reusing for a per-backend orientation prompt: not a new mechanism, the same one, a new
  target.
- **Skills have no drop-in equivalent claimed for Codex.** Codex CLI's own docs describe "skills" as
  a feature (§2.1), but nothing in this pass verified they share Claude Code's Skill-tool invocation
  model, frontmatter, or discovery rules — this is flagged as **unverified, needs its own spike**
  before any of the `subagents/*/SKILL.md` files could be assumed portable.

### 1.6 The cockpit — `cockpit/server/`, `cockpit/web/`

- **Model dials** — §1.3, duplicated rank table; the backend axis is Phase 2's addition.
- **The Trace panel's event schema is Claude-CLI's stream-json shape, one hop removed, and the exact
  chokepoint has a name: `cockpit_pipe.build_chat_event_from_stream`.** The daemon's
  `_make_stream_tee` converts each raw claude-CLI stream-json line via that one function
  (`seneschal/scripts/cockpit_pipe.py`), whose own docstring is explicit: convert one parsed
  claude-CLI stream-json line into a digestible `chat.event`. It branches on `ev.get("type")`: for
  `"assistant"`, it reads `(ev.get("message") or {}).get("content")` as a list of content blocks and
  checks each for `b.get("type") == "text"` vs. `"tool_use"` — a direct re-implementation of
  Anthropic's own Messages-API content-block schema; for `"result"`, it reads
  `result`/`is_error`/`duration_ms`/`num_turns`/`total_cost_usd`/`usage` — fields with no
  generic-LLM-API analog (`num_turns`/`total_cost_usd` are CLI-turn accounting concepts, not
  per-request API fields). `cockpit/server/trace.py` deliberately does not import this converter (the
  cockpit is its own dependency world) but reads the *already-converted* `chat.event` rows this
  function wrote to `warm-transcript.jsonl` — `session_id`, `turn_id`, `tool_uses`, `cost_usd`,
  `context_peak` — and the frontend (`cockpit/web/src/components/traceView.ts`) reads that same
  converted shape (`tool_uses` as `{name, input_preview}`, `reply_preview`, grouping by `turn_done`)
  — never raw stream-json. **This is genuinely good news for portability (§3): the cockpit, `trace.py`,
  and the frontend never parse raw backend wire format — only `build_chat_event_from_stream`, the
  single named chokepoint, does.** A second backend needs its own converter feeding the *same*
  `chat.event` shape into the same sink; nothing downstream of that one function needs to change. Phase
  2's `codex_cli.py` carries exactly that: its own `build_chat_event_from_stream` over Codex's JSONL
  events, same output shape.
- **`model: "local"` is the precedent for a non-Claude-model badge** — the `!status` daemon command
  is answered locally by synthesized `chat.event`s badged `model: "local"`, spending no model turn at
  all. §4's pre-classifier router path reuses this exact precedent for its own badge.

### 1.7 `fable_delegate.py`

- Runs `claude -p --model <fable id> --output-format json <prompt>` as a subprocess — the same CLI, a
  different one-shot invocation shape from the warm session's stream-json mode. `--output-format json`
  (not `stream-json`) is deliberately different from `WarmSession`'s flags — it exists only to get a
  single `usage` block back for metering. Ceiling-checked against `model_config.admits_fable` (§1.3)
  before spawning. Same `ANTHROPIC_API_KEY` scrub as §1.1.

### 1.8 `archon-forge` / demiurge — the one place in this codebase an adapter abstraction *already exists*, and the nearest precedent for §3's design

- **Demiurge already has a named, pluggable adapter axis**: `--adapter claude-cli` vs. `--adapter
  claude-sdk` (`seneschal/references/archons.md`). The claude-cli adapter "shells to the `claude` CLI
  — subscription, `ANTHROPIC_API_KEY` scrubbed" — the same rule as `presence.py`. Its implementation
  lives in the sibling demiurge repo — the adapter template's server module and its executor's invoke
  method, named here only as prose since neither exists in *this* tree — **not read in this pass** (a
  sibling repo, outside this spec's scope), but its existence and shape are directly on-point:
  demiurge already had to solve "run an LLM-backed agent process behind one interface, with at least
  two named backends," for a *different* purpose (billing model: subscription vs. metered API, not
  vendor). §3's `Backend` contract is the same shape of problem the daemon never had to solve for
  itself, applied to *which vendor* rather than *which billing mode*. `demiurge scaffold ... --adapter
  claude-cli` is a strong naming precedent for a Codex adapter's own `--adapter codex-cli` if this
  ever generalizes back into demiurge itself (out of scope here — Archon staff run on `claude-cli` only
  today, and nothing in this document proposes changing that).
- **Unattended auth for the claude-cli adapter is `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`**
  (`archons.md`, matching `presence.py`'s docstring) — the same interactive-once, cached-forever
  pattern §2.1 finds Codex CLI uses for its own ChatGPT-subscription login (`~/.codex/auth.json`).
  This parallel is worth naming explicitly in §3: both vendors solve "no API token, but still
  headless-runnable" the same way, which is exactly the shape the goal asks for.

### 1.9 `seneschald-control.ps1` — the account-identity check `Update` uses to decide whether to restart

- **The PowerShell-side identity read** parses `~/.claude.json`'s `oauthAccount`, keyed on
  `(accountUuid, organizationUuid)` only — deliberately excluding `organizationType`/
  `organizationRateLimitTier` so a plan upgrade doesn't misread as an account change (mirroring the
  daemon's own reasoning, §1.1). This is the PowerShell twin of the daemon's identity read and reads
  the *same* Claude-Code-CLI config file, so a `claude /login` alone triggers a graceful daemon restart
  purely because this check fires. A second backend's own login/credential-refresh event (Codex:
  `codex login`, cached to `~/.codex/auth.json`, §2.1) has no equivalent watcher today, and would need
  one if its account identity is ever meant to gate a restart the same way.

### 1.10 The broader sweep — every other headless `claude -p` spawn site, and the one cross-cutting pattern that ties all of them together

Beyond the warm session (§1.1), jobs (§1.2), and Fable (§1.7), a repo-wide sweep for `claude_bin` /
`ANTHROPIC_API_KEY` / `--output-format` / `stream-json` / `CLAUDE_CONFIG_DIR` finds the same
`[claude_bin, "-p", prompt, "--permission-mode", ...]` shape repeated, independently, at a dozen-odd
production call sites — each with its own `--claude-bin` CLI flag default and its own
`env.pop("ANTHROPIC_API_KEY", None)` scrub, never a single shared "backend" object: `mini_dream.py`
(the per-session distillate), `reminders_live.py` (reminders' stage-2 live read), `job_analysis.py`
(the fresh-eyes analyst artifact), the daemon's own slot/seed/Watch-peek one-shots in `presence.py`,
and the research harnesses that deliberately spawn their own `["claude", "-p", "--input-format",
"stream-json", "--output-format", "stream-json", ...]` to mirror the warm session's exact protocol for
measurement. Even non-LLM subprocess spawns (`cockpit_site.py`'s npm build step, `archon_sites.py`)
defensively scrub `ANTHROPIC_API_KEY` anyway — the billing-safety idiom has spread well past the sites
that actually need it, which is itself a sign of how ad hoc the mechanism is: there is no one place a
`Backend`'s "how do I make sure I'm never billed the wrong way" answer lives, only a copy-pasted line
at every spawn site. §3's `Backend.cost_model` field (§3.1) is the proposed fix — one property every
call site can check, instead of a dozen independent re-derivations of the same scrub.

**One genuinely new dependency, not covered elsewhere in this inventory:** `usage_probe.py` reads the
real plan usage by invoking `claude -p "/usage" --output-format json` — **`/usage` is a Claude Code
CLI slash command**, not an API call or a flag; there is no assumed equivalent introspection surface
for a second backend, and this module is simply unreproducible against one until that backend exposes
something analogous (Codex CLI was not verified to have one in this pass). The same module also shells
`[claude_bin, "--version"]` to identify the installed CLI build.

`governor.py` (Oikonomos) and `spend_levers.py` are two more instances of §1.1's "no generic cost
model" finding: `spend_levers.py` is explicitly documented as accumulating over "raw claude-CLI
stream-json events," and `governor.py`'s `TOKEN_WEIGHTS` encode Anthropic's specific prompt-caching
tiers (cache-read vs. 5-minute vs. 1-hour cache-write multipliers) — a cost shape with no reason to
carry over to a different vendor's own pricing structure.

### 1.11 What this inventory does *not* find — scoped explicitly, so a later reader doesn't assume it was missed

- **`router.py`'s fable/steer/triage classifiers already run on Ollama, not Claude** (`_ollama_chat`,
  a small local model). This part of the seam is **already backend-agnostic** — nothing here needs to
  change for a second warm-session backend, and §4's pre-classifier router path sits beside this
  existing local-Ollama-classifier pattern, not against it.
- **`sentiment.py`, `salience_rollup.py`'s embedding calls, `rag_index.py`/`rag_query.py`** — all
  Ollama, not Claude. Also already backend-agnostic.
- **Skills content itself (`subagents/*/SKILL.md`, `seneschal/modes/*.md`) is prose the assistant
  reads, not code that spawns a specific binary.** These files are backend-agnostic *as written* — the
  risk is not in their content but in whether a second backend's harness has any equivalent "load this
  file when this mode activates" mechanism at all (§1.5, §2.1's "unverified" flag on Codex skills).

---

## 2. The candidate doors

The bar: **anyone with an AI subscription that supports headless use — i.e. not requiring an API
token — can run the framework.** That is the filter every candidate is scored against first —
subscription-headless-capable, or not — before any feature-parity question even matters. All claims
below were checked against vendor documentation at the time of writing (these products move fast; a
later reader should re-verify rather than trust this table past a few months), sources cited inline.

### 2.1 OpenAI Codex CLI — the only door that clears the subscription bar cleanly

- **Auth: `codex` (interactive browser sign-in against a ChatGPT Plus/Pro/Business/Edu/Enterprise
  plan) or an API key** ([Authentication | ChatGPT
  Learn](https://learn.chatgpt.com/docs/auth)). The subscription flow is interactive on first use —
  "the sign-in flow opens a browser window" — but caches to `~/.codex/auth.json` (plaintext by
  default, or an OS credential store via `cli_auth_credentials_store`), after which **headless
  `codex exec` runs against the cached credential with no further browser step** — the identical
  shape to `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` (§1.8's parallel). OpenAI's own docs
  still steer *programmatic* (CI/CD) use toward an API key and caution against exposing Codex
  execution in untrusted or public environments — a caution about *unattended/public* automation, not
  a statement that subscription auth can't run headless once logged in; the daemon is neither
  untrusted nor public, and the cached-credential pattern is exactly what `presence.py` already does
  for Claude. **Flagged for a build-time spike, not resolved here**: whether the cached `auth.json`
  survives a long-running resident daemon the way `~/.claude.json`'s OAuth token does, or needs a
  refresh flow this spec hasn't characterized.
- **Non-interactive execution: `codex exec`**, with `--json` for structured output and JSONL event
  streaming ([Codex CLI | ChatGPT Learn](https://learn.chatgpt.com/docs/codex/cli)) — the rough
  shape `WarmSession` needs. The exact event vocabulary (which event names correspond to
  `assistant`/`tool_use`/`tool_result`/`result`) was the first thing Phase 2 had to map against
  §1.1/§1.6's normalization layer; `codex_cli.py`'s converter is that mapping (`turn.failed`/`error`/
  `thread.error` classified as `is_error=True`, among others).
- **Instructions file: `AGENTS.md`, not `CLAUDE.md`.** Created via `codex`'s own `/init`. A
  differently-scoped, differently-discovered convention from Claude Code's directory-walk auto-load
  (§1.5) — **not assumed compatible**; the persona-projection pattern (§1.5, §3.3) is the answer, not a
  shared file.
- **MCP: `~/.codex/config.toml` under `[mcp_servers.<name>]`**, e.g.:
  ```toml
  [mcp_servers.notion]
  command = "npx"
  args = ["-y", "@notionhq/notion-mcp-server"]
  ```
  ([MCP config examples via community docs](https://top-mcps.com/client/codex), cross-checked against
  `codex mcp add`'s existence in OpenAI's own CLI reference). A **standing config file**, not a
  per-invocation CLI flag the way `--mcp-config` is (§1.1) — this is actually a *simpler* surface for
  §3's adapter to manage (write the file once, not re-derive an argv every spawn), but it is a
  different write target, not a drop-in read of the same MCP config JSON the store already has.
- **Hooks: exist, and a `PreToolUse`-equivalent with a genuine deny capability exists** — Codex's
  `PreToolUse` fires before a tool call executes and can return a deny decision, or exit `2` with the
  reason on stderr, which mirrors Claude Code's own contract closely enough that a
  `send_gate_hook.py` equivalent is plausible. **One material caveat surfaced by search but not
  independently verified against Codex's own current docs (the source pages were unreachable on this
  pass)**: at least one third-party write-up describes `PreToolUse` as covering only the Bash tool at
  some point in Codex's history, with MCP tool calls (the exact tools `send_gate_hook.py` guards —
  Slack/Gmail send) *not* firing `PreToolUse` at all. **This is the single highest-priority thing to
  verify before any Codex backend is trusted to make an outbound MCP send call** — if Codex's
  `PreToolUse` genuinely does not fire for MCP tool invocations, then per §1.4's hard requirement, a
  Codex-backed assistant must be refused for outbound Slack/Gmail sends (its own drafts and its own
  triage skills still fine — the refusal is scoped to what §1.4 names, not to Codex generally) until
  that gap closes, verified against Codex's own current release notes at build time, not this spec.
  Phase 2 sidesteps the question structurally instead (§6.7.6): the codex session cannot mount an MCP
  server at all.
- **Skills**: OpenAI's docs claim a "skills" feature exists. **Unverified against Claude Code's own
  Skill-tool model** — flagged in §1.5, not resolved here.
- **Session resume: `codex resume`** — reopens a recent session; the exact identity/continuation
  contract (does a resumed session report the same id back, the way `claude --resume` does per §1.1?)
  is **unverified, spike-required**. Phase 2's `CodexWarmSession` answers the practical half
  differently: "warm" there means no held-open process, but a stable `thread_id` Codex's own server
  resumes by.

**Verdict: the clear first second-implementation target** — it clears the no-token bar, it serves the
goal of seeing the same assistant on a second model family, and it has enough surface-level parity
(`exec`, JSON/JSONL streaming, MCP via config, a hooks system with at least some deny capability) to
be worth a real build. The MCP-tool-hook-coverage question above is the one finding that could change
this verdict from "build it" to "build it, but keep outbound sends Claude-only for now" — which is
itself a perfectly fine phased outcome, not a blocker to starting, and is the posture Phase 2 took.

### 2.2 Google Gemini CLI — the subscription door closed itself in 2026

- **In mid-2026 Google stopped serving Gemini CLI requests for Gemini Code Assist for individuals,
  Google AI Pro, and Google AI Ultra, and removed personal "Login with Google" access.** Google now
  directs individual/consumer users to a *different* product, Antigravity CLI ([Gemini CLI Setup Guide
  (2026) — KissAPI](https://kissapi.ai/blog/gemini-cli-setup-guide-2026.html), cross-referenced
  against [gemini-cli's own authentication
  docs](https://geminicli.com/docs/get-started/authentication/)). What remains reachable —
  `GEMINI_API_KEY`/`GOOGLE_API_KEY`, or Vertex AI, or Gemini Code Assist *Standard/Enterprise* (a paid
  **business** tier, not an individual subscription) — is exactly the API-token shape the goal
  screens out.
- **Verdict: does not clear the bar as of this writing, and the reason is a vendor policy change, not
  a technical gap.** Worth re-checking at build time (Antigravity CLI is the named successor and was
  not evaluated in this pass), but not a door to build against today.

### 2.3 OpenRouter — API key only, by design; the wrong shape for the no-token goal, useful as an optional tier if ever wanted

- **OpenRouter uses credit-based pay-as-you-go billing, not a subscription** — authenticated
  exclusively via `Authorization: Bearer <API key>`
  ([openrouter.ai/docs/api_reference/authentication](https://openrouter.ai/docs/api_reference/authentication)).
  There is no login-once-then-headless subscription flow of any kind; every call is metered against a
  prepaid credit balance.
- **Verdict: fails the stated goal directly and by design** — "not requiring an API token" rules it
  out for the tier this spec is about. It remains a legitimate *optional* future door for someone who
  explicitly wants pay-per-call access to hundreds of models and is fine holding a token, but it is not
  part of the "anyone with a subscription" promise this spec is scoping toward, and nothing in §3's
  phasing should imply otherwise. If it is ever built, it is a second, clearly-labeled tier (§3.4).

### 2.4 Ollama (local) — already present, already the answer for one whole slice of this problem

- Already wired in as the router's classifier backend (§1.11), `sentiment.py`, the RAG embedding
  pipeline, and — as §4 proposes — the explicit pre-classifier router path's model. No subscription,
  no token, no network call at all; the constraint is local hardware and model quality, not access.
  Not a candidate for the *warm session* itself (nothing in this pass suggests a local model is
  anywhere near capable enough to run the assistant's actual conversational/tool-use loop), but
  already fully proven as a **secondary** backend for narrow, specific jobs — which is precisely the
  role §4 assigns it.

### 2.5 What the assistant actually requires of any warm-session backend, and how each door scores

From §1's inventory, a backend that wants to run the assistant's actual chat/tool-use loop — not just
§4's narrow side-door — needs:

| Requirement | Why (§1 citation) | Codex CLI | Gemini CLI | OpenRouter |
|---|---|---|---|---|
| Headless, subscription-billed, no API token | the stated goal | **Yes** (cached `auth.json` after one interactive login) | No (individual login access removed mid-2026) | No (API-key-only by design) |
| Store MCP (read/write, when the store is MCP-backed) | §1.1's `--mcp-config` | Likely (`~/.codex/config.toml` `[mcp_servers.notion]`) | N/A — screened out above | N/A |
| Bash + file read/write | every subagent skill; the filesystem store backends | Yes (Codex is a coding-agent CLI, same category as Claude Code) | N/A | N/A |
| A `PreToolUse`-shaped safety gate for outbound MCP sends (§1.4) | `send_gate_hook.py` — the one mechanism that can refuse an unapproved send | **Unverified for MCP tools specifically** — the single open question that gates whether a Codex-backed assistant may ever send outbound | N/A | N/A |
| Resumable session / continuation | §1.1, §1.2's `job_completion.py` | Exists (`codex resume`; a stable `thread_id`); exact-id-echo semantics unverified | N/A | N/A |
| Some grounding-file convention (CLAUDE.md-equivalent) | §1.5 | `AGENTS.md` — different scope, needs its own projection | N/A | N/A |

OpenRouter and Gemini CLI are excluded from the requirement columns because they already fail the
first, load-bearing row — there is no reason to score them against the rest.

---

## 3. Design — a `Backend` adapter contract, claude-cli as reference, codex-cli as the first second implementation

### 3.1 The contract

`WarmSession`/`StubWarmSession` already shared an informal duck-typed surface (§1.1). The design
names that surface explicitly as `Backend` rather than inventing a new one:

```
Backend
  spawn(model, permission_mode, mcp_grants, resume_id=None) -> a running child/handle
  send(text, on_event) -> reply text | None        # on_event fires per normalized chat.event
  session_id: str | None
  resumed: bool
  resume_failed: bool
  timed_out: bool
  age_sec() -> float | None
  # observability, mirroring WarmSession's existing scalars (§1.1):
  turns_served, last_usage, last_num_turns, context_tokens, session_cost_usd, last_turn_cost_usd
  identity() -> {account_id, org_id, email} | None   # generalizes the daemon's identity read (§1.1, §1.9)
  cost_model: "subscription" | "metered_api"          # generalizes the ANTHROPIC_API_KEY scrub's intent
```

`WarmSession` is the reference implementation (`seneschal/scripts/backends/claude_cli.py`);
`StubWarmSession` needs no change (it already satisfies this shape). `CodexWarmSession`
(`seneschal/scripts/backends/codex_cli.py`) is the first real second implementation. `make_session()`
in `presence.py` selects through `seneschal/scripts/backends/__init__.py` on the
`state/model-config.json` `backend` field (§3.2) rather than only on `args.stub_brain`.

**Every event a `Backend` emits must already be normalized to the daemon's own `chat.event` shape**
(§1.6's finding that the cockpit only ever reads this normalized form, never raw stream-json, is what
makes this tractable) — the normalization step that used to live only inside `WarmSession.send()`'s
stream-json parsing (§1.1) is *per-backend*, each `Backend` responsible for translating its own wire
format into the one `chat.event` shape every downstream consumer (cockpit Trace panel,
`spend_levers.py`, `mouth.py`) already expects.

**The safety gate is not optional per backend — it is a gate on which backends may reach outbound
tools at all.** Per §1.4/§2.1's open question: a `Backend` is only eligible to hold
`send_gate_hook`-equivalent enforcement if its harness has a verified pre-tool-call deny point
covering *every* tool class the assistant can invoke, MCP included. Until that is verified for Codex
specifically, the honest design is: a Codex-backed assistant can chat, read, draft, and hold — exactly
the act-low half of `references/autonomy-policy.md` — but every ask-high outbound send stays refused
at the backend boundary, independent of anything the model itself decides, until the gate question is
closed. This is a stricter default than "trust the new backend's own hook," matching this repo's
existing fail-closed posture (`send_gate.py`'s own fail-CLOSED polarity, §1.4). Phase 2 implements it
in its strongest form: `CodexWarmSession` has no code path that grants an MCP server at spawn time at
all (§6.7.6).

### 3.2 The cockpit warm-model dial grows a backend axis

`state/model-config.json` gains a `backend` field alongside `warm_model`:
`{"backend": "claude-cli" | "codex-cli", "warm_model": "...", "max_routable_model": "...",
"updated_at": "..."}`. `model_config.py`'s `RANK`/`ALIASES`/`LABELS` (§1.3) become **per-backend**
tables — `RANK["claude-cli"]` is the prior list unchanged, `RANK["codex-cli"]` is Codex's own model
tier list (a best-effort reading, re-derived whenever Codex changes its model set). `admits_fable()`
stays a claude-cli-only concept: Fable delegation (§1.3, §1.7) is a Claude-family mechanism and has no
defined meaning if the warm backend isn't Claude — `fable_delegate.py` should refuse cleanly (a clear
message, not a crash) when the live backend isn't `claude-cli`, the same posture it already takes for a
sub-Fable ceiling. `save()` **preserves the stored backend when its own `backend` argument is
omitted**, so an older caller that only sets the warm model never silently flips the backend back to
claude-cli. `cockpit/server/model_config.py` (§1.3, the cockpit spec's deliberate-duplication
decision) mirrors the per-backend structure the same way it mirrors everything else — no change to
that decision, just to what it duplicates.

### 3.3 The grounding prompt projects per backend, following the persona-projection precedent exactly

Per §1.5: the persona-projection pattern — pure extraction, `--write`/`--check`, a CI gate — is the
answer, reused rather than reinvented. A new `orientation_project.py` (name illustrative, not final)
would extract whatever subset of the assistant's grounding (persona, top-level behavior rules, the
modes index) makes sense as a Codex `AGENTS.md`, the same way the phone persona is a curated subset of
the canonical persona file — generated, `--check`-gated in CI, never hand-edited. **What exactly
belongs in that projection is explicitly not decided here** — it is a genuinely open design question
(does Codex need the *whole* root `CLAUDE.md` router, or a curated subset the way the phone persona is
a curated subset of the full persona file?) that should be answered once a real `AGENTS.md` spike
shows what Codex actually does with it, not guessed at now.

### 3.4 Host-side setup a PR cannot do

Following `SEND_GATE_SETUP.md`/`JOB_BACKGROUND_GUARD_SETUP.md`'s own pattern (§1.4) — a `PreToolUse`
hook or an account login is inherently host-side; `~/.claude/settings.json` and `~/.codex/*` are both
outside version control. A Codex host-setup guide beside those (a `CODEX_SETUP.md` in
`seneschal/scripts/`, landing with the backend) covers, at minimum:

1. `codex login` (interactive, once) — the ChatGPT-subscription sign-in, mirroring `claude /login`.
2. Writing `~/.codex/config.toml`'s `[mcp_servers.*]` entries for the store's MCP (and Slack, if
   granted) — the standing-file equivalent of what `active_mcp_configs()` (§1.1) currently computes
   and passes as CLI flags every spawn. **Not needed while Phase 2's no-MCP posture holds** — this is
   the step that stays unbuilt until §2.1's hook-coverage question closes.
3. Registering Codex's own `PreToolUse`-equivalent hook(s), once §2.1's MCP-tool-coverage question is
   resolved and a `send_gate_hook`-equivalent exists to register.
4. Whatever `CLAUDE_CONFIG_DIR`-equivalent env var Codex needs if it should read from a non-default
   config location (mirroring the daemon's handling of `CLAUDE_CONFIG_DIR`).

The /setup wizard's `auth-models` chapter is the natural home for step 1 once a second backend is a
supported install path; until then it is a manual, documented step.

### 3.5 Phases, act-low/ask-high

- **Phase 0 (the spec).** SPEC-ONLY. No code.
- **Phase 1 — spike, act-low.** Stand up a throwaway `codex exec --json` call against a trivial
  prompt, by hand, off `develop`, to answer §2.1's open questions empirically: the real JSONL event
  vocabulary, whether `PreToolUse` fires for MCP tool calls, whether `codex resume` echoes a stable
  session id the way `claude --resume` does. Writes nothing tracked; a memo, not a build.
- **Phase 2 — `Backend` contract + `CodexWarmSession` + the cockpit dial, ask-high before merge.
  BUILT.** The build decision folded what the original phase plan called "Phase 3" (the cockpit dial)
  into Phase 2 — the contract itself (§3.1) is a refactor of `WarmSession`/`StubWarmSession` into an
  explicit shape (low risk, mostly mechanical, since §1.1 already found the two classes share it
  informally); `CodexWarmSession` is new code behind a config flag defaulting to `claude-cli`, so the
  live daemon's behavior does not change until the owner flips `state/model-config.json`'s `backend`
  field; and the cockpit's `backend` dial (§3.2) shipped in the same change rather than waiting for a
  separate one. Per §3.1, outbound MCP sends stay refused on `codex-cli` until Phase 1's hook-coverage
  question is closed — structurally, by the codex session having no MCP grant path at all.
- **Phase 3 — dual-active backends with dynamic routing.** §6. Explicit prefix, router-picks-by-task,
  A/B comparison, and failover, in the order the owner chose them (§6.1) — plus the
  daemon-architecture generalization (§6.7) that all four ultimately depend on. SPEC-ONLY; §6.8 is its
  own sub-phase plan.
- **Phase 4 — `orientation_project.py`/`AGENTS.md`.** §3.3's grounding-projection work, renumbered up
  from the original plan's "Phase 3" slot now that Phase 2 owns the cockpit dial. Independent of
  Phase 3 — a routed-to codex session benefits from it, but nothing in §6 depends on it existing
  first.
- **Phase 5 — the Codex setup guide + a real, host-confirmed run.** The owner logs in, runs the
  assistant on `codex-cli` for real, and this is where "how does the assistant behave on model X"
  (§3.6, extended by §6.4's A/B judging record) starts producing real data instead of a spike's toy
  prompt.

### 3.6 Measurement plan — "how does the assistant behave on model X" as data, not vibes

Reuse what already exists rather than building a second measurement system:

- **`turns.jsonl`** (`turns.py`'s `record_turn`) is the append-only, uncapped record of what was
  actually said, both sides, retained indefinitely by standing decision. It has no backend field today
  — adding one (`backend: "claude-cli" | "codex-cli"`) is the one schema change this plan needs,
  additive and backward-compatible (an absent field reads as `claude-cli`, the only backend that had
  ever produced a row before Phase 2).
- **The assertion-provenance rows** (`mouth.py`'s derived `scope`, and the ask-provenance work in
  `ask-provenance-spec.md`) are the existing instrument for "was a claim actually checked, or stated
  from memory" — segmenting those rows by the new `backend` field on `turns.jsonl` answers "does model
  X assert unchecked claims more or less than Claude on the assistant's own real traffic," which is a
  much stronger claim than any synthetic eval, because it measures the actual thing the goal asks
  about: *how the assistant behaves*, not how a benchmark scores the underlying model.
- **`spend_levers.py`** (diagnosis-only, never gates) already reads the raw stream event above the
  cockpit's own conversion (§1.6) — once a `Backend`'s events are normalized into the same
  `chat.event` shape, this instrument needs no change to compare cost/latency/turn-count across
  backends on equal footing.
- **What this plan deliberately does not propose**: a synthetic benchmark suite, an LLM-as-judge
  comparison, or any new measurement infrastructure. The existing instruments, segmented by one new
  field, are the honest answer to "how does it behave differently" — building a parallel eval harness
  would be exactly the kind of scope creep a measurement phase should refuse: a measurement phase
  ships whole on the instruments it has, rather than growing a second system to measure the first.

---

## 4. Router: an explicit pre-classifier path

Some messages the owner wants answered by a designated **local** model, directly — and for those, the
requirement is structural, not a prompt: **the raw text must never reach a hosted classifier at all.**
Routing such a message through a Claude classifier first defeats the purpose — the hosted model's own
guardrails trip on the content before any routing decision is made. So: not the router's triage arm,
not the fable arm, not the warm session itself. Code decides before any model sees the message.

### 4.1 Where this plugs in — the `!status` precedent, followed exactly

`_enqueue_inbound()` in `presence.py` is the one inbound funnel, and the `!status` daemon command
already has exactly the needed shape for a different reason: a daemon command, not a message for the
assistant — deliberately not appended to the thread tail either; answering it locally means it costs
no turn and needs no warm session. It is pulled out of `new_inbound` *before* `apply_force_route` (the
`!fable` prefix), *before* `append_thread`, *before* the shadow/fable-arm classifiers ever run. The
new prefix follows this identical pattern, added as a second, earlier filter in the same function,
for the same structural reason: what it intercepts must never reach anything downstream — no thread
cache, no classifier, no warm session.

**This is deliberately a different shape from `!fable`.** `!fable`'s prefix is *stripped and the
directive is baked back into the text* (`apply_force_route`) so the message still flows to Claude, now
carrying a routing hint. The pre-classifier prefix is the opposite: detection means the message is
**removed from `new_inbound` entirely** and answered out-of-band, exactly like `!status`, not annotated
and forwarded.

### 4.2 The prefix and the model config key

- **Prefix**: a literal, case-insensitive leading token, proposed `!raw` (illustrative — naming is
  explicitly the owner's to confirm, per §5's open questions; a longer spelling was considered and
  rejected only because it is longer to type under exactly the pressure that motivates this feature
  at all — a fraught question is not when someone wants to type extra characters). Detected with the
  same `re.compile(r"^\s*!raw\b[:,]?\s*", re.IGNORECASE)` shape `FORCE_FABLE_PREFIX_RE` already uses
  in `presence.py` — "any case, an optional ':'/',' and whitespace after," so `!raw <question>` and
  `!raw: <question>` both trigger and `!raw` buried mid-sentence does not (mirroring `!fable`'s own
  stated design intent).
- **Model config key**: a new `state/model-config.json` field (illustrative name `raw_model`) naming
  whichever local model the owner designates — **the specific model is the owner's choice, not this
  spec's** — read by a new, small, dedicated module (illustrative name `router_raw.py`) that calls it
  via the same `_ollama_chat`-shaped local call `router.py` already uses for the triage/fable/steer
  arms (§1.11) — reusing the transport, pointed at a different, owner-designated model and a bare
  pass-through system prompt (or none), never the triage/fable/steer classifiers' own prompts.

### 4.3 What it must NOT do

**No tools, no store, no outbound.** Concretely, enforced structurally, not by prompt:

- The call is a single Ollama chat completion, the same shape `_ollama_chat` already makes — there is
  no tool-use loop to grant tools to in the first place, so "no tools" is true by construction, not by
  refusal.
- The answer is returned via `deliver_reply` directly — never through `append_thread`, never through
  the durable action queue, never through anything the warm session's next turn could read as prior
  context. It does not become part of "the conversation" from the warm session's perspective at all,
  ever.
- No store MCP call is possible because no MCP config is passed to this call in the first place
  (unlike `WarmSession.start()`'s `--mcp-config`, §1.1 — this path never spawns a `claude` process at
  all).
- No outbound send: the reply goes back to the owner on the same channel the question arrived on,
  which is the owner-class case `send_gate.py` (§1.4) already passes without a store read — this path
  sends nothing a human other than the owner could ever receive, by construction (it answers only the
  channel the question arrived on, never a different recipient).

### 4.4 What is logged

**The turn is the owner's; log that the path was taken, not the content, unless the owner decides
otherwise.** Concretely: `turns.jsonl` (§3.6) gets a row with `kind: "raw_route"` or equivalent,
`channel`, timestamp, and a length/hash of the text — **never the text itself, never the model's
answer** — mirroring `send_recipients.py`'s own non-content ledger discipline (§1.4's neighbor: a
non-content `recipient_class` ledger, never the address). This gives a measurable rate (how often the
door is used) without creating a second, more sensitive copy of exactly the content the feature exists
to keep away from a classifier that logs prompts. Cockpit observability follows the `!status`
precedent exactly (§1.6): a `chat.event` badged `model: "local"` (or a new, distinct badge, e.g.
`"local-raw"`, so the Trace panel's reader can tell this turn apart from a genuine local-model chat
turn without inferring it from absence of other fields) so the Trace panel shows *that* the turn
happened without exposing what was asked, matching `cockpit/server/trace.py`'s own `!private`
redaction posture rather than inventing a new privacy tier.

### 4.5 Interaction with the steer/fable arms

**Bypasses all three** (triage, fable, steer), unconditionally, because it never reaches
`_enqueue_inbound`'s classifier block at all (§4.1 — it is filtered out earlier, in the same
`!status`-shaped pre-pass). This is a stronger guarantee than "the classifiers would pass it through
safely" — there is no classifier call to audit for correctness, because none runs. The mid-turn
steer/interleave gate also never sees it, for the same reason `!status` doesn't: it is removed from
`new_inbound` before the interleave snapshot is taken.

---

## 5. Open questions (the owner's, not this spec's to answer)

- **The prefix's literal spelling** (§4.2) — `!raw` proposed, not fixed.
- **Which local Ollama model backs the pre-classifier path** — a model the owner trusts and has
  pulled; this spec deliberately names no default.
- **Whether the pre-classifier-route ledger row should ever carry more than length/hash** — content
  stays out unless the owner decides otherwise; this spec defaults to the stricter reading.
- **Whether OpenRouter is ever worth building as an explicitly-labeled optional tier** (§2.3) — not
  proposed here, but not foreclosed either if a future use case genuinely wants pay-per-call access
  to a wider model catalog than either subscription door offers.
- **What exactly `orientation_project.py` should extract into `AGENTS.md`** (§3.3) — deferred to a
  real spike, not guessed here.

---

## 6. Phase 3 — dual-active backends with dynamic routing

### 6.1 The goal and the decision

**The goal:** both backends active at once, with each turn dynamically routable to either service.
**The decision** — the owner chose all four offered mechanisms, composed in this order:

1. **Explicit prefix** — a literal prefix on the owner's message (e.g. `!gpt` / `!claude`) forces the
   backend for that turn, code-level string match, the same mechanism family as the pre-classifier
   path (§4); always wins.
2. **Router picks by task** — the local Ollama classifier gains a backend arm, shadow phase first
   (verdicts logged, no steering) exactly like the trivial/escalate arm's phase 1, with a decided
   table of who-is-good-at-what (§6.3 proposes one, marked as a proposal, not decided).
3. **A/B for comparison** — the same turn goes to BOTH backends; one answer is sent, the other logged
   for judging (cockpit side-by-side view; a judging record feeding the "how does the assistant behave
   on model X" measurement plan in §3.6). Double cost while on, a dial not a default.
4. **Failover** — rate-limit/down → the other backend answers, and the reply names which backend
   spoke.

This section specs all four, builds explicitly on §3.1's `Backend` contract and Phase 2's
`CodexWarmSession` (neither redesigned here), and is honest about which parts are hard: two warm
sessions are two contexts, and the daemon's single-session architecture (`DaemonState.session`, one
`session_busy` bool, one session-registry entry, one flat cockpit status shape) was never built to
hold two of anything (§6.7).

### 6.2 Mechanism 1 — explicit prefix, always wins

This file already has two prefix precedents (§4.1/§4.2), and the backend prefix is neither of them —
naming the difference matters more than the regex, which is identical in shape to both
(`FORCE_FABLE_PREFIX_RE`'s own case-insensitive-leading-token-plus-optional-`:`/`,` pattern):

- `!fable` is **stripped and baked back into the text** (`apply_force_route`) — the message still
  goes to the *same* session, now carrying a routing hint.
- `!raw` (§4) **never reaches any warm session at all** — pulled out of `new_inbound` before
  `append_thread`, before any classifier, answered out-of-band.
- The backend prefix (`!gpt` / `!claude`, spelling illustrative, the owner's to confirm per §6.9) is a
  **third shape**: stripped, and the clean text still becomes part of "the conversation" — appended
  to the thread, written to `turns.jsonl`, classified normally by whichever arms are backend-neutral
  — but routed to a *different* session slot than the daemon's live default. Detected in
  `_enqueue_inbound()` alongside (not instead of) the `!fable` check, since the two answer orthogonal
  questions (which Claude-family delegate vs. which backend's own warm session runs the turn) — except
  they are not fully orthogonal: Fable is a claude-cli-only concept (`model_config.py`'s per-backend
  Fable tier is `None` for codex-cli, §3.2), so a message carrying both `!fable` and a non-claude
  backend prefix has no coherent meaning and should refuse cleanly (a short line naming the conflict)
  rather than silently picking one.
- "Always wins" is meant literally: unlike the router (§6.3, gated on confidence and phase) or
  failover (§6.5, gated on the preferred backend actually being down), an explicit prefix is never
  overridden by anything else in this section — not even a backend that A/B mode (§6.4) would
  otherwise have queried in parallel.

### 6.3 Mechanism 2 — router picks by task, shadow first

Modeled directly on `router.py`'s existing trivial/escalate arm (`classify`, whose module docstring
states that phase 1 is shadow only: the classifier runs and its verdict is only logged, zero behavior
change) — a new `classify_backend(message, cfg=None, timeout=20) -> {"verdict": "claude-cli" |
"codex-cli", "confidence": ..., "reason": ...}`, the same JSON-schema-constrained Ollama call shape
`_ollama_chat` already makes for `classify`/`classify_fable`/`classify_steer`. **Phase 1 is shadow
only**: runs on every inbound turn, logs its verdict (mirroring `router-log.jsonl`'s existing shape, so
the fallback-rate tooling needs no second log format to learn), and changes *nothing* — every turn
still goes to the daemon's configured default backend regardless of verdict, exactly `classify`'s own
posture.

Every classifier arm in this file names an explicit safe-fallback direction (`classify`: escalate;
`classify_fable`: standard/no-delegate). **Proposed safe fallback for `classify_backend`:
`claude-cli`** — not because of any claimed quality difference, but because claude-cli is the only
backend today with verified send-gate/`PreToolUse` coverage (§1.4, §2.1); an uncertain routing verdict
should never default toward the backend whose outbound-tool safety is structurally weaker by
omission, not by policy. Stated as a proposal, not asserted as fact.

**A who-is-good-at-what table — proposed, explicitly not decided:**

| Task shape | Proposed backend | Why (proposal, not measured) |
|---|---|---|
| Anything outbound-sensitive (Slack/Gmail send, calendar invite with attendees) | `claude-cli`, unconditionally | Codex has no verified `PreToolUse`-MCP coverage (§1.4/§2.1); this ties directly to §6.7.6's safety invariant, not to task quality |
| Quick acks/status/recall (the trivial/escalate arm's own `ack`/`status`/`recall` categories) | Either — cheapest tier on whichever backend the trivial/escalate arm would already have kept local | No claim of a quality gap at this task size; routing choice here is a cost/latency question, not a capability one |
| Long-form reasoning, multi-step planning, or anything the owner explicitly asks to see on GPT | `codex-cli` when asked; otherwise `claude-cli` | The owner's own stated goal (seeing how the assistant works on different models) is itself a task-shape signal the table should honor, not override |
| A message *about* Codex CLI or OpenAI's own product (self-referential) | `claude-cli` | Routing a question about backend B to backend B is circular and produces no useful comparison; flagged as an edge case, not a real category |

This table is exactly the kind of claim §3.6's measurement plan exists to answer with real data
instead of a guess — once Phase 3's shadow log has verdicts and (once A/B, §6.4, has run) real
counterfactual answers to compare, this table should be re-derived from that data, not left as this
section's own guess.

### 6.4 Mechanism 3 — A/B for comparison

The same turn dispatched to **both** backends' sessions concurrently — each `send()` is already a
blocking call run via `asyncio.to_thread` (`presence.py`'s drainer), so this is two concurrent
`to_thread` calls rather than a new concurrency primitive. **One answer is sent, the other is logged**
— which one is sent is not specified by the goal, so this proposes the lower-risk default: the
daemon's *configured default backend's* answer is what the owner sees; the other is purely
observational. (The alternative — sending whichever answered first, or alternating — is not
recommended: it would make the reply's authorship non-deterministic from the owner's side, which cuts
against "one character across channels.")

**Judging record.** Not `turns.jsonl` — that file's `record_turn` contract (`turns.py`) is one row per
*actual* side of a turn the owner saw; the unsent comparison answer was never said to them, so writing
it there would misrepresent what happened. Proposed instead: a new append-only
`state/backend-ab.jsonl`, one row per A/B'd turn: `{turn_id, sent_backend, other_backend,
other_reply_preview, usage/cost per backend, ts}`. Whether `other_reply_preview` should ever hold full
text or only a preview/hash is an open question (§6.9) — a weaker case for withholding than §4.4's
pre-classifier-route ledger (this is an ordinary in-character reply, not sensitive content), so this
section flags rather than defaults it.

**Cockpit side-by-side view.** Reuses the one finding §1.6/§3.1 already established and Phase 2 built
on: every backend's events are already normalized to the same `chat.event` shape
(`build_chat_event_from_stream`, one implementation per backend, same output shape) before anything
downstream reads them. So the side-by-side view is "two already-normalized `chat.event` streams for
one `turn_id`, rendered next to each other" — no new parsing, no backend-specific rendering code, the
same reason the existing Trace panel never had to change when `codex_cli.py` landed.

**Cost.** Explicitly double while on — never inferred, never hidden. This is a dial on
`state/model-config.json` (illustrative field, e.g. `ab_compare: bool`), meaningful only once a second
backend is actually configured; it is a comparison instrument, not a standing routing mode, and
nothing in this section proposes it as any kind of default.

**Feeds §3.6's measurement plan, but adds something that plan couldn't have on its own**: §3.6
proposed segmenting `turns.jsonl` by a `backend` field to compare how the assistant behaves per
backend — but that only ever captures one backend's answer per turn. A/B additionally captures the
**counterfactual** (what the *other* backend would have said on the identical prompt), which no
router-pick or failover turn can ever produce, since those run exactly one backend per turn by
construction.

### 6.5 Mechanism 4 — failover

**Detecting "down."** claude-cli already has two relevant precedents, worth distinguishing because
neither is a drop-in "is this backend down" oracle: `usage_probe.py`'s polled `/usage` read (a Claude
Code CLI slash command, §1.10 — cadence-based, not per-turn, and answers "how much plan quota is
left," not "did the last spawn fail") and `WarmSession`'s own two-rung spawn-fallback ladder (§1.1's
rungs: resume-didn't-take, model-won't-spawn) — the right precedent to *extend*, since it already
treats "won't spawn" as a fallback trigger, but today falls back to a different **model** on the
*same* backend, never across backends; crossing backends is new wiring on an existing idea, not a new
idiom.

codex-cli has **no verified equivalent of either** — §2.1 flagged `usage_probe`'s `/usage`-style
introspection as not verified to exist for Codex, and Phase 2 did not close that gap. The honest
interim answer, proposed and not resolved here: key codex's own down-signal off the turn's exit code
and the `turn.failed`/`error`/`thread.error` JSONL events `codex_cli.py`'s
`build_chat_event_from_stream` already classifies as `is_error=True` — a **reactive**, per-turn signal
("that turn just failed, possibly rate-limited"), not a pre-emptive polled one the way `usage_probe`
theoretically could be for claude-cli. Whether that asymmetry between the two backends' failover
triggers is acceptable is the owner's to judge once real failure data exists (§3.6/§6.4 again), not
decided here.

**Naming which backend spoke.** Today, nothing does this — the daemon had exactly one backend, so no
reply ever needed to say. Two existing precedents apply, and they answer different questions: (a) the
`model: "local"` badge (§1.6, the `!status` synthetic `chat.event`) generalizes directly to badging
every `chat.event`/`turns.jsonl` row with the backend that produced it — this is the *same field* §3.6
already proposed adding to `turns.jsonl` for the measurement plan, so failover's naming need and the
measurement plan's segmentation need are one field, not two; (b) whether the **visible reply text
itself** should ever say so (a literal "(via Codex)" appended to what the owner reads) is a
product/voice question, not an architecture one, and this section proposes **no** — the framework's
"one character across channels" principle argues against the assistant ever breaking character to
announce its own internals, and a silent badge in the Trace panel/`turns.jsonl` already satisfies "the
reply names which backend spoke" in every place that matters (the owner, reading the Trace panel or a
segmented report) without the *reply prose* ever doing so. Flagged as a default, not a decision —
§6.9.

### 6.6 Composition — when more than one mechanism could apply

The chosen ordering (§6.1) is preserved as the proposed precedence, but this is explicitly a proposal,
not a decision extracted from the choice itself (ordering a set of selected options is not the same
act as stating a precedence):

1. **Explicit prefix** — unconditional, per "always wins" (§6.2).
2. **A/B mode**, when the dial (§6.4) is on — a deliberate, explicit toggle; while it's on, every
   turn goes to both backends regardless of what the router (§6.3) would have picked alone, since the
   whole point of turning it on is to stop trusting the router's pick unverified.
3. **Router pick**, once out of shadow phase (§6.3) — the default steering behavior when neither of
   the above apply.
4. **Default backend** (`state/model-config.json`'s configured default) — the floor, when the router
   is still shadow-only or abstains.

**Failover is orthogonal to all four**, not a fifth rung in this list: it fires on a spawn/send
failure regardless of *how* the destination was chosen, so it needs only a small ranked fallback list
(today: try the selected backend, else the other one) rather than re-running the selection logic
above.

### 6.7 Design — the hard parts, honestly

#### 6.7.1 Two warm sessions are two contexts

**Already shared today, verified backend-agnostic by this pass and by §1.11's own inventory** —
nothing here needs new plumbing to keep sharing them: `turns.jsonl` (per-side append-only, keyed by
`turn_id`/`session_id`, no backend-specific shape beyond the one field §3.6 already proposed); the
carry-over and the Brief/Wrap machinery (reads the store and `loops.py`, never a warm session's own
in-RAM memory); the reminders queue and `sentinel.py`'s gates (keyed off `session_is_live`, a
*registry* predicate — see §6.7.4 — never off any session's internal state); the store itself (tool
access is host-config per backend, not backend-specific code); and the session registry
*conceptually* (though its current single-entry shape is exactly what §6.7.4 has to change).

**NOT shared, and this is the fact to design around rather than hide**: the live in-RAM
conversational context each backend holds between turns. For claude-cli this is the actual process
context `--resume <id>` reattaches to (§1.1). For codex-cli, per `codex_cli.py`'s own docstring,
"warm" means something structurally different — no held-open process, a stable `thread_id` Codex's
own server resumes by — but the same point holds regardless: whatever context Codex retains between
turns is Codex's own, invisible to and never merged with claude-cli's. **A message routed to backend B
has not been "seen" by backend A's context**, full stop, no matter how many turns A has served in what
the owner experiences as one continuous conversation.

#### 6.7.2 The handoff — a bounded recap, never a transcript dump

When routing sends a message to the backend that did **not** handle the immediately preceding turn
(mechanisms 1, 2, or 4 — A/B, §6.4, is exempt by construction, since both backends see every A/B'd
turn), that backend's context has a gap exactly where the other backend's turns were. The daemon
already has two precedents for bridging a context gap with a *synthesized* preamble rather than a raw
dump, and both matter here: the resume preamble (situates a cold-resumed `WarmSession` — §1.1) and
`channel_declare.current_topic_line` (a short situating line prepended to every warm-session prompt
already, cheaply, every turn).

Proposed, not designed to the byte: a `backend_handoff_preamble(state_dir, turn_id, other_backend,
max_turns=N)` built the same way — reads the most recent N `turns.jsonl` rows (both sides) since the
last turn *this* backend actually served, and renders a short "since we last spoke" block, on the same
order of magnitude as the resume preamble's own recap, never the raw text of everything that happened
while this backend sat idle (a rarely-routed-to backend could otherwise accumulate an unbounded gap).
**Never a verbatim transcript dump**: this mirrors §4.3/§4.4's own discipline (hand a model the
minimum synthesized context it needs, never raw material to interpret unsupervised) and the general
caution against trusting an unbounded context to summarize itself faithfully — the recap is text the
daemon's *code* composed, not a delegation to the receiving backend to figure out what it missed. The
exact bound and rendering shape are explicitly **not** decided here — a build-time question (§6.9).

#### 6.7.3 Single-flight generalizes per-backend, never per-daemon

Today: `DaemonState.session` is **one** slot and `state.session_busy` is **one** bool;
`warm_session_busy(session, pending)` checks exactly that one flag against exactly that one session
object; `drainer_task` owns spawn/respawn/idle-wind-down for that one slot against one
`last_activity` clock (all in `presence.py`).

Dual-active needs this **per-backend**, not doubled-but-still-singular: `state.sessions: dict[str,
Backend]` keyed by backend id, `state.session_busy: dict[str, bool]`, `warm_session_busy` taking a
backend key. **The invariant that must survive the generalization, stated explicitly because a
careless generalization could break it**: single-flight stays *per-backend-session*, never
*per-daemon* — two turns may be in flight at once (one per backend; that is the whole point of
dual-active) but a given backend's *own* session must never see two turns in flight simultaneously,
for the same reason `state.session_busy` exists today (a stdin-driven process has exactly one "next
turn" slot; two concurrent sends would interleave two replies into one stream).

**A real fork this section flags rather than resolves**: the mid-turn steer/interleave arm (§4.5)
answers "is a turn in flight" as one global boolean. It needs to answer that **per backend** — a steer
injection meant for the codex session must never fire against claude-cli's in-flight turn, and vice
versa — but that arm's design and measurement were built and validated against exactly one session.
Whether its gating logic even generalizes cleanly to two independently-busy sessions is
**unverified**, not designed here.

**Idle wind-down** needs the same per-backend split: one shared clock would either (a) never wind
down a rarely-routed backend while the other stays busy, or (b) wind down a backend the router is
about to need. Propose per-backend `last_activity`/idle timers, explicitly not one shared clock —
flagged as a build-time decision, not designed to the constant here.

#### 6.7.4 Session registry + cockpit status for two warm sessions

Today: `write_session_heartbeat(args.state_dir, "daemon", phase="active", ...)` writes **exactly one**
registry entry, hardcoded id `"daemon"`, which `sentinel.session_is_live` reads as a single yes/no "is
the assistant's own warm session actively engaged" gate for reminder-defer/Watch-peek purposes (the
multi-session awareness the root `CLAUDE.md` describes). `_status_snapshot` is a **flat** dict keyed
to the one `state.session` object — `session_up`/`model`/`turns_served`/`context_tokens`/
`session_cost_usd` are each a single scalar, no backend dimension.

Both need to widen without breaking their existing single-session callers:

- **Registry**: the entry id becomes `f"daemon:{backend}"` (e.g. `daemon:claude-cli`,
  `daemon:codex-cli`) rather than the bare `"daemon"`. `session_is_live`'s own semantic **widens**
  from "is *the* session live" to "is *any* warm session live" — a reminder deferred because the
  assistant is mid-conversation on claude-cli must stay deferred even while codex-cli sits idle, since
  the defer reasoning in `presence_rules.py`/`sentinel.py` was never about *which* backend, only
  whether the assistant is actively engaged at all. This is a widening (a union over registry entries
  matching a `daemon:*` prefix), not a narrowing, so no existing caller of `session_is_live` needs to
  know backends exist at all.
- **Cockpit status**: `_status_snapshot`'s flat dict becomes a dict *of* per-backend snapshots
  (`{"claude-cli": {...same shape as today...}, "codex-cli": {...}}`), and the cockpit's status
  renderer grows a second gauge set only when a second backend is actually warm — mirroring §3.5
  Phase 2's own posture (no live behavior change until the owner acts): a cockpit that has never seen
  two warm sessions renders exactly as it does today.

#### 6.7.5 Cost/rate-limit accounting per backend — two separate gaps, not one

Both are already visible in Phase 2's own field-level honesty; naming them together matters because a
naive "just add the numbers" instinct would paper over both:

- **Dollar cost.** claude-cli's `session_cost_usd`/`last_turn_cost_usd` are real figures
  (`total_cost_usd` off the CLI's terminal `result` event). Codex's `usage` block, per `codex_cli.py`'s
  own usage-recording comment, carries **no dollar-cost field at all** —
  `session_cost_usd`/`last_turn_cost_usd` stay `None` for `CodexWarmSession` **by design**, not
  omission. An A/B comparison or a per-backend spend report can therefore compare **token counts**
  across backends but can **never** produce a combined or side-by-side **dollar** figure for the
  codex side. This is a hard limit inherited from the vendor's own reporting, not a gap Phase 3 can
  close by writing more code — any dashboard or report built on this must say "no cost data" for
  codex-cli, never a guessed or estimated dollar figure standing in for an absent one (an estimate
  presented as a measurement is exactly the confidently-wrong failure this repo's provenance work
  exists to prevent).
- **Rate-limit signal.** claude-cli has `usage_probe.py`'s polled `/usage` read as its live
  plan-usage source, on its own cadence, not per-turn. Codex has no verified equivalent (§2.1, §6.5)
  — so failover's "is this backend down" question cannot be answered the same way on both sides. The
  asymmetry named in §6.5 (codex only ever reacts to an already-failed turn; claude-cli's probe could
  theoretically pre-empt one) applies to accounting too: a per-backend rate-limit dashboard can be
  built for claude-cli today and cannot, honestly, be built the same way for codex-cli until it has
  its own equivalent signal — flagged, not solved.

#### 6.7.6 Safety invariant — codex never mounts outbound tools, regardless of which rule routed to it

**This is the one place Phase 3 needs no new design, and this section's job is to say why, not to
invent a mechanism.** Per §3.1 and the backends package's own docstrings
(`seneschal/scripts/backends/__init__.py`, `seneschal/scripts/backends/codex_cli.py`),
`CodexWarmSession` contains no code path that can grant an MCP server at spawn time **at all** — the
constraint is structural (no `-c mcp_servers...` flag, no config write reachable from the class), not
a policy check a routing decision could accidentally bypass. So the invariant is **already true before
Phase 3 exists**, for all four mechanisms simultaneously and for the same reason: whether a message
reached the codex session via an explicit `!gpt` prefix (§6.2), a router verdict (§6.3), an A/B
dispatch (§6.4), or a failover fallback (§6.5), the session that answers it is still the same
`CodexWarmSession` instance with the same absent MCP-grant code path.

The one thing Phase 3 must preserve, stated as a constraint on its own future code rather than a new
mechanism: **no dispatch/routing code this phase adds may ever pass anything resembling an MCP
config, grant list, or tool allowlist into a `CodexWarmSession` constructor call** — the moment any
future code does that (e.g. "just for this one A/B run, let's see what codex does with the store
too") the invariant stops being structural and starts being a policy someone has to remember, exactly
the failure mode §1.4's "net finding" already names for the harness-level gap Codex has relative to
Claude Code's own hook surface. `send_gate.py`'s script-level gate is untouched either way: it lives
inside the outbound scripts themselves, reachable from any backend's Bash tool identically, regardless
of which of the four mechanisms sent the turn there.

### 6.8 Phasing, act-low/ask-high

- **Phase 3.0 — spike/shadow, act-low.** `classify_backend` shadow-only (§6.3); the
  `state.sessions`/`state.session_busy` dict generalization (§6.7.3), mechanically, with zero behavior
  change while only one backend is ever configured warm; the `daemon:{backend}` registry-key rename
  (§6.7.4) — a rename, not a semantics change, while only one backend is ever live.
- **Phase 3.1 — explicit prefix + failover, ask-high before merge.** Both are narrow and mechanically
  bounded (prefix: a regex match plus a dispatch-target field; failover: extending the existing
  spawn-fallback ladder cross-backend, §6.5) and need neither the router nor A/B's cost/UI investment
  to ship.
- **Phase 3.2 — A/B for comparison + the cockpit side-by-side view.** Ahead of the router by the
  owner's decision: it depends on nothing beyond the `Backend` contract itself (already built), but it
  is the most UI-heavy leg, and it is a dial for occasional deliberate comparison rather than a
  standing routing behavior — its measurement value is highest once real router-pick and failover
  data already exist to compare against.
- **Phase 3.3 — router-picks-by-task, out of shadow.** Deliberately last — the owner rated
  router-picks-by-task the least important of the four. The mechanism stays wanted and shadow-first;
  it is simply built after prefix, failover and A/B. Gated on real shadow-verdict data existing
  (mirroring `classify`'s own phase 1→2 promotion discipline) and on the owner deciding the
  who-is-good-at-what table (§6.3) for real, not the proposal here.

### 6.9 Open questions this section does not decide

- The explicit prefix's literal spelling (§6.2) — `!gpt`/`!claude` proposed, not fixed.
- The router's who-is-good-at-what table (§6.3) — a proposal, not a decision.
- Whether the A/B judging record (§6.4) should ever carry full reply text or only a preview/hash.
- Whether a failover/backend badge should ever surface in the visible reply prose (§6.5) — proposed
  default: no, a silent Trace-panel/`turns.jsonl` badge only.
- The interleave/steer arm's meaning against two independently-busy sessions (§6.7.3) — flagged
  unverified, not resolved.
- The handoff recap's exact bound and rendering shape (§6.7.2) — a build-time spike question.
- The per-backend idle-wind-down policy (§6.7.3) — independent clocks proposed, not decided to the
  constant.
- Composition precedence (§6.6) — proposed from the order of the chosen mechanisms, not extracted
  from a stated precedence.

---

## Cross-links

- **`seneschal/scripts/backends/`** — the built Phase 2 package: `__init__.py` (backend selection),
  `claude_cli.py` (the reference `WarmSession`/`StubWarmSession`), `codex_cli.py`
  (`CodexWarmSession` over `codex exec --json`, its own `build_chat_event_from_stream`, no MCP grant
  path). §6 cites its fields and functions by name; if that code changes, re-check §6's citations
  against it rather than trusting this read.
- **`seneschal/scripts/model_config.py`** + **`cockpit/server/model_config.py`** — the per-backend
  rank tables and the `backend` dial (§1.3, §3.2); `cockpit/server/test_parity.py` keeps the two in
  step.
- **`seneschal/references/archons.md`** — §1.8's adapter precedent; not modified by this document.
- **`seneschal/docs/CLAUDE.md`** — carries this file's router entry (see top of this document).
- **`cockpit-spec.md`** — the cockpit's model dials and the deliberate-duplication decision §1.3/§3.2
  lean on.
- **`how-the-approval-gate-works.md`** — the outbound approval gate (`send_gate.py` +
  `send_gate_hook.py`) whose harness dependency §1.4 names as the hard requirement for any backend.
