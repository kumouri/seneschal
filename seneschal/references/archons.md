# Archons — the assistant's minted staff (Forge mode reference)

How the assistant's specialized staff work: the sibling **Demiurge** meta-agent owns the lifecycle
(mint → eval-gated admission → A2A delegation → outcome-based tenure); the assistant owns the judgment,
the approvals, and every byte of the owner's data. Skill: `../../subagents/archon-forge/SKILL.md`.

## Where things live

| Thing | Path | Tracked? |
|-------|------|----------|
| Demiurge repo (public, code only) | a sibling checkout, e.g. `../demiurge` | its own repo |
| Need statements (drafted by Forge) | `archons/need/<id>.need.yaml` | ✅ tracked |
| Stable (spec, charter, evals, record, eval-report) | `archons/stable/<id>/` | ✅ tracked |
| **Delegation ledger** | `archons/<id>/state/ledger.jsonl` | ❌ gitignored — **not** in the stable |
| Scaffolds (generated uv projects) | `archons/scaffolds/<id>/` | ❌ gitignored (regenerable) |
| Per-Archon private inputs (profiles, watchlists, outputs) | `archons/<id>/` | tools/examples tracked; private inputs (profile, watchlist, voice profile) + `state/` + `out/` gitignored |

**Never** put the owner's data (needs, stables, profiles, ledgers) in the demiurge repo — it's public.
Demiurge's CLI takes `--stable-dir`/`--scaffold-dir`/`--ledger-dir`, which is what keeps the lifecycle
data here.

### The ledger lives in `state/`, not the stable

**Always pass `--ledger-dir <M>\archons\<id>\state` on `delegate`/`verdict`/`distill`/`tenure`.** It
must be the same path on all four: the writer and the reader have to agree, or a verdict fails with
*"no delegation with task_id"* against a ledger that plainly has it.

**Why.** The ledger was the one part of `stable/` that *churned* — everything else there is written
once at mint/revise. Demiurge appends on every delegation, so:

1. **It armed the reload bug.** A live merge-is-deploy checkout was permanently dirty after any Forge
   run, and `pull --ff-only` aborts when an incoming merge touches a dirty path — the failure mode
   that silently stops auto-reloads.
2. **It put the owner's operational context in git history.** Delegation lines store the full
   request + response — operational context by nature, and git history can't be scrubbed by editing
   a worktree.

Demiurge reads the **local file** for `tenure`/`distill`, so tracking it bought nothing. Upstream fix:
demiurge's `--ledger-dir` flag — `archon_dir` still answers *which archon*, `ledger_dir` answers
*where its churn goes*. `proteus_paths.migrate_legacy_state()` moves an existing ledger across on the
next tool run.

**When minting a new archon:** its ledger goes in `archons/<id>/state/` from day one. Don't add
`ledger.jsonl` to a tracked stable and don't put churny paths in a charter's task text (rule 6 below).

## Archon internal layout — the `state/` vs `out/` rule

**Every archon that writes anything gets this shape, and is *minted* with it.** Once an archon
accumulates runtime state, mixing it in with deliverables makes both harder to reason about (Proteus
had tens of MB of hourly churn — a giant `scored-latest.json`, a multi-MB dedup ledger — sitting in
`out/` next to the tailored resumes it had drafted).

| dir | holds | in git? |
|-----|-------|---------|
| `archons/<id>/state/` | **Runtime churn** — rewritten every cycle, regenerable: rolling ledgers, "latest" caches, counters, queues, sentinels, pid/log files. *If a run rewrites it every cycle, it goes here.* | ❌ (only `README.md` + `*.example.*`) |
| `archons/<id>/out/` | **Deliverables** — what a run *produces*: drafted documents, digests, generated views. Products, not machinery. | ❌ (they embed the owner's personal data) |
| `archons/<id>/` | Curated inputs + code: profile, watchlist, voice profile, `tools/`, **`.gitignore`** | ✅ (private inputs as `*.example.*` seeds) |

Rules:

1. **Each archon carries its own `.gitignore`** (`archons/<id>/.gitignore`) ignoring `state/` and
   `out/` (plus `*.env`, `__pycache__`, `*.log`, `*.pid`), with `!state/README.md` +
   `!*.env.example` un-ignored. This makes the split travel with the archon wherever it's checked
   out — the root `.gitignore`'s `archons/*/state|out` rules are only a safety net for archons
   that lack one.
2. **`state/README.md` is tracked** — it documents each state file and keeps the directory present.
   An **`out/README.md`** may be tracked the same way. Both the root and the per-archon `.gitignore`
   must then spell the rule `out/*` + `!out/README.md`, **not** `out/`: git never descends into an
   excluded *directory*, so a re-include inside one is silently ignored.
3. **Define paths in one module** the archon's tools import (Proteus: `tools/proteus_paths.py`), so
   tools can't drift apart on where state lives. **An archon with no tools gets no `tools/` and no
   paths module** — there is nothing to keep from drifting, and an empty directory is scaffolding for
   its own sake (an archon whose work all happens inside the delegated turn on granted local tools
   needs neither). Add one the moment it gains a *second* script.
4. **Moving an existing archon onto this layout goes in code**, not by hand: a
   `migrate_legacy_state()` that moves old → new only when the new path is absent (idempotent,
   self-healing). Scheduled tasks race a manual move, and dropping a dedup ledger re-alerts
   everything.
5. **An archon NEVER writes a tracked file directly.** `state/` and `out/` are gitignored and free to
   write. Anything **tracked** — a curated input the archon *contributes to* — is written by a
   **promotion step that goes through a PR**, never by the archon at runtime.

   **Why it's a rule and not a preference:** the archon runs in the **live daemon's checkout**. A
   tracked file it mutates leaves that checkout permanently dirty, and the first incoming merge that
   touches the same path fails `pull --ff-only` — which silently stops deploys (the daemon's
   auto-reload never fires). It also loses data: a headless archon's whole-file rewrite of an
   existing file is unreliable, so a finding the archon "recorded" can simply never land.

   **The shape:** the archon appends a proposal to a gitignored queue via a small Bash helper
   (Proteus: `tools/record_intel.py`) → whatever consumes it reads queue-over-tracked so the finding
   takes effect *immediately* → a promotion step (Proteus: `tools/promote_intel.py`, nightly in Dream)
   merges it into the tracked file via a reviewed PR, enforcing the data's invariants **in code**
   rather than in charter prose.

   Corollary: any invariant the charter states about a tracked file ("never weaken an entry the owner
   sourced", clamps, update-don't-duplicate) belongs in the promotion step. Prose in a charter is a
   hope; a check in the promoter is a guarantee.
6. **The charter can route churn into `out/` too — check it, not just the tools.** Proteus's own
   tools were already clean, but its *charter* hardcodes `--out .../out/<run-date>/jobs.json`, so
   every delegated run refilled `out/` with raw scored boards — one run dir held a double-digit-MB
   board next to a single 4 KB deliverable. A charter is fixed at mint time and revising it is
   **ask-high**, so the durable fix is a path resolver the tools apply on **both** sides of the pipe
   (`proteus_paths.resolve_raw_artifact()` — writer `--out` *and* reader `--jobs`), which keeps the
   charter's literal commands chaining while the files land correctly. **When minting: don't write
   raw-artifact paths into a charter's task text at all** — name the tool and let its paths module
   decide, or you've baked a layout bug into the one document you can't edit act-low.

New archons are minted with `state/`, `state/README.md`, and `.gitignore` already in place — see
`subagents/archon-forge/SKILL.md` Phase 3.

### A UI-having archon also ships a tracked `site.json`

An archon with a **human-facing web UI** declares it via a tracked `archons/<id>/site.json` at its
root, alongside its `.gitignore` and curated inputs — **not** in `state/` or `out/` (it's non-secret
config, not runtime churn). The presence daemon's `archon_sites.py` (a supervised daemon task,
`../scripts/SCHEDULING.md`) scans `archons/*/site.json` on a reconcile cadence, health-checks each
declared site over HTTP, and (re)spawns one that's down or wedged — so a newly-minted UI archon
auto-registers with the daemon with **no central file to edit**. Schema (`seneschal.archon-site/1`):

```json
{
  "schema": "seneschal.archon-site/1",
  "id": "<id>",
  "enabled": true,
  "port": 9800,
  "health_path": "/",
  "cmd": "python",
  "args": ["tools/<ui_script>.py", "--serve", "--port", "9800"],
  "cwd": "."
}
```

`cmd: "python"` is substituted with the daemon's own `sys.executable` at spawn time (same venv);
`cwd` is relative to the archon's own directory. **The `port` here must stay in sync by hand** with
the cockpit's per-install `cockpit/server/archon-registry.json` (`ui_port`) — deliberately
duplicated (the cockpit is its own dependency world), never cross-imported; `site.json` itself can
carry a `"//"` comment key noting this. Mint/retire a UI archon → update both. Process model
(detached-survivor spawning, PID adoption across a daemon reload, spawn-grace anti-thrash):
`../scripts/archon_sites.py`'s module docstring.

**What the cockpit shows for an archon** (`cockpit/server/archons.py`): an archon with a `ui_port`
and `status: "live"` gets a reachability pill, **Open →**, and its UI behind the cockpit's GET-only
reverse proxy (`/archons/<id>/`). Every other registry entry — delegation-on-demand, `specced`,
`reserved` — gets a tile carrying its registry `status` and nothing else; only `live` entries are
probed. A tile-only archon is registered correctly, not broken: a panel is a real feature (a body
builder, a frontend panel, and a charter/refresh path that stages its data), not a config line.

## Command crib sheet

All commands run from anywhere; paths are absolute. `$D` = the demiurge repo, `$M` = this repo.

```powershell
uv run --project $D demiurge mint     $M\archons\need\<id>.need.yaml --stable-dir $M\archons\stable
uv run --project $D demiurge scaffold <id> --stable-dir $M\archons\stable --scaffold-dir $M\archons\scaffolds --adapter claude-cli
uv run --project $D demiurge deploy   <id> --stable-dir $M\archons\stable --scaffold-dir $M\archons\scaffolds --adapter claude-cli --port <port>   # blocks; run detached
uv run --project $D demiurge admit    <id> --stable-dir $M\archons\stable --endpoint http://127.0.0.1:<port> --timeout 600
uv run --project $D demiurge delegate <id> "<task>" --stable-dir $M\archons\stable --ledger-dir $M\archons\<id>\state --endpoint http://127.0.0.1:<port> --timeout 1800
uv run --project $D demiurge verdict  <id> <task-id> --outcome success|failure --note "…" --stable-dir $M\archons\stable --ledger-dir $M\archons\<id>\state
uv run --project $D demiurge distill  <id> <task-id> --note "…" --stable-dir $M\archons\stable --ledger-dir $M\archons\<id>\state
uv run --project $D demiurge tenure   <id> --stable-dir $M\archons\stable --ledger-dir $M\archons\<id>\state
uv run --project $D demiurge retire   <id> --reason "…" --stable-dir $M\archons\stable
```

Billing: the claude-cli adapter shells to the `claude` CLI — **subscription**, `ANTHROPIC_API_KEY`
scrubbed (demiurge ADR 0006; same rule as `presence.py`). For unattended runs set
`CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`). Never use `--adapter claude-sdk` for the assistant's
staff.

Deploy note: `demiurge deploy` waits healthy then blocks in the foreground — launch it detached
(e.g. `Start-Process`) and health-check `http://127.0.0.1:<port>/.well-known/agent-card.json`.
Tear the process down when the work is done; Archons don't idle.

**Teardown note — kill the TREE, then ask the PORT.** `uv run … demiurge deploy` is a *wrapper*: the
agent server (`scaffolds/<id>/.venv/python server.py --port N`) is its **grandchild**, and it
survives the wrapper's death. A teardown that defines "torn down" as "the process I held is gone"
*reports success* while the agent keeps listening for hours. So:

- Reap by tree: `taskkill /PID <pid> /T /F` on Windows; on POSIX spawn with `start_new_session=True`
  and `killpg` the group (never the group you're *in*).
- **"Already exited" is not an excuse to skip the reap** — an exited wrapper is precisely when its
  children have been orphaned.
- Then poll the port. Only silence there means torn down; anything still answering is a **leak**,
  and it gets said out loud rather than swallowed.

**Read grants stop at the scaffold.** The claude-cli adapter runs the delegated `claude -p` with
`cwd` pinned to the archon's own scaffold directory, so a spec's `Read`/`Grep`/`Glob` grant only
reaches files under the scaffold — whatever the grant text promises. That confinement is invisible
in the spec, the charter and `evals.yaml`. If an archon's method genuinely needs to read beyond it,
widen the deployment (a demiurge adapter that supports `CLAUDE_CLI_ADD_DIR`, an os.pathsep-joined
directory list passed through as `--add-dir`), never the charter; and when an eval fails on a
read-dependent case, rule out this confound before blaming the charter.

**Deploy from a sandboxed session (Claude Code shell): inject the token first.** Sandboxed shells
don't inherit the **user-level** `CLAUDE_CODE_OAUTH_TOKEN`, so the archon's `claude` CLI falls back
to a stale credential store and every delegation dies with *"OAuth session expired and could not be
refreshed"* (observed in production — the health-check passes, spend fails). Before `Start-Process`:
`$env:CLAUDE_CODE_OAUTH_TOKEN = [Environment]::GetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN','User')`
(never print it). A healthy agent card proves the door opens, not that the lights are on.

## Autonomy (mirrors `autonomy-policy.md`)

Act-low: reading the stable/roster/ledgers, drafting a need statement, scaffolding, and — by the
owner's standing authorization ("spin up archons as needed") — **deploy, admit, delegate**. The
basis: the claude-cli adapter runs headless `claude` on the **subscription** with
`ANTHROPIC_API_KEY` scrubbed, so a delegation can't reach the metered API, and the rest is budgeted
into the plan.

Ask-high: **mint, revise, retire** — roster changes (who works for the owner is the owner's call,
not a spend question). Held Forge actions ride the normal approval loop with `kind: "archon"`.

Two things the standing auth does **not** touch: (1) whatever an Archon drafts for the outside world
still comes back through **the assistant's** draft-and-hold gate — it authorizes *running* the
staff, not *shipping* their work; (2) a **non-spend** objection to a specific delegation —
non-compete/legal exposure, a posting that can't be verified as genuine, an
`avoid_companies`/`avoid_patterns` hit — is a **separate gate**, and the assistant still holds those
for the owner.

### When something is a subagent skill rather than a minted Archon

An Archon (Proteus-style) is for **outward-facing, subscription-billed, eval-gated, autonomous staff
that act on the outside world**. Work that is deterministic local ETL over the owner's own data, with
**zero outbound action**, is an assistant **subagent skill** — the right first form. **Archive mode**
(`subagents/message-archivist/`) is the worked case: it reads the owner's threads, normalizes them and
renders a transcript, sends nothing and modifies nothing on any service.

Such a skill is **promotable later**: if it grows autonomy — scheduled continuous archiving, LLM
enrichment with its own budget and evals — mint the Archon then and reuse the same stdlib scripts as
its least-privilege tools.

### A decision that lives only in a chat turn will be asked for again

**When the owner rules on something an archon is blocked on, write it to a tracked file in the same
turn — with its date and its provenance — or it does not exist.** A ruling delivered only in chat
never reaches a tracked artifact, so the next run reads `archons/<id>/` and carry-over, sees *"open
since <date>"*, and asks the owner again — for something already decided. Record it in the archon's
tracked context file (and correct its charter context through the ask-high revise path if that is
where the question lives).

This is the same failure `jobs.py` fixes for completion pushes, one rung up: **the record, not the
turn, holds the decision.** Chat context dies at the next reload; a roster note or a charter context
file does not. Two consequences:

- **A blocked-on-the-owner item is only unblocked once its ruling is in git.** Marking it resolved
  in a reply, carry-over or a run-log line is not the durable copy — those are gitignored local
  caches.
- **Anywhere the repo says a decision is open, it must be corrected in the same change that records
  the ruling.** A stale *"still open"* in a second file re-arms the question for the next cold
  reader.

## Port registry & roster

The assistant's archons get ports **9701–9749**, one per Archon, assigned at mint and never reused.

| Archon | Port | Status | Charter (one line) |
|--------|------|--------|--------------------|
| `proteus` | 9701 | worked example | Job-application specialist: job-board search → match % → tailored resume/CV in the owner's voice → company research → draft email held for the assistant's gate. Example tools ship under `archons/proteus/tools/`; private inputs (profile, watchlist, voice profile) stay local and gitignored. |

**A port is a reservation, not a listener.** Most archons are **delegation-on-demand**: deployed
only for admission or a delegation and torn down after, so their port is a reservation for
`demiurge deploy --port`, and a `curl 127.0.0.1:<port>` failing is normal, not an outage. Only an
archon with a `site.json` (above) has something resident — and that is its **UI** port, not its
agent port.

Keep this table current — it's the roster the Forge orients from (a mint/retire updates it as part
of the approved action, together with the cockpit's `archon-registry.json` for a UI archon).
