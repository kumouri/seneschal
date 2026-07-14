# Archons — the assistant's minted staff (Forge mode reference)

How the assistant's specialized staff work: the sibling **Demiurge** meta-agent owns the lifecycle
(mint → eval-gated admission → A2A delegation → outcome-based tenure); the assistant owns the judgment,
the approvals, and every byte of the owner's data. Skill: `../../subagents/archon-forge/SKILL.md`.

## Where things live

| Thing | Path | Tracked? |
|-------|------|----------|
| Demiurge repo (public, code only) | a sibling checkout, e.g. `../demiurge` | its own repo |
| Need statements (drafted by Forge) | `archons/need/<id>.need.yaml` | ✅ tracked |
| Stable (spec, charter, evals, record, ledger, eval-report) | `archons/stable/<id>/` | ✅ tracked |
| Scaffolds (generated uv projects) | `archons/scaffolds/<id>/` | ❌ gitignored (regenerable) |
| Per-Archon private inputs (profiles, watchlists, outputs) | `archons/<id>/` | tools/examples tracked; private inputs (profile, watchlist, voice profile) + `out/` gitignored |

**Never** put the owner's data (needs, stables, profiles, ledgers) in the demiurge repo — it's public.
Demiurge's CLI takes `--stable-dir`/`--scaffold-dir`, which is what keeps the lifecycle data here.

## Command crib sheet

All commands run from anywhere; paths are absolute. `$D` = the demiurge repo, `$M` = this repo.

```powershell
uv run --project $D demiurge mint     $M\archons\need\<id>.need.yaml --stable-dir $M\archons\stable
uv run --project $D demiurge scaffold <id> --stable-dir $M\archons\stable --scaffold-dir $M\archons\scaffolds --adapter claude-cli
uv run --project $D demiurge deploy   <id> --stable-dir $M\archons\stable --scaffold-dir $M\archons\scaffolds --adapter claude-cli --port <port>   # blocks; run detached
uv run --project $D demiurge admit    <id> --stable-dir $M\archons\stable --endpoint http://127.0.0.1:<port> --timeout 600
uv run --project $D demiurge delegate <id> "<task>" --stable-dir $M\archons\stable --endpoint http://127.0.0.1:<port> --timeout 1800
uv run --project $D demiurge verdict  <id> <task-id> --outcome success|failure --note "…" --stable-dir $M\archons\stable
uv run --project $D demiurge distill  <id> <task-id> --note "…" --stable-dir $M\archons\stable
uv run --project $D demiurge tenure   <id> --stable-dir $M\archons\stable
uv run --project $D demiurge retire   <id> --reason "…" --stable-dir $M\archons\stable
```

Billing: the claude-cli adapter shells to the `claude` CLI — **subscription**, `ANTHROPIC_API_KEY`
scrubbed (demiurge ADR 0006; same rule as `presence.py`). For unattended runs set
`CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`). Never use `--adapter claude-sdk` for the assistant's
staff.

Deploy note: `demiurge deploy` waits healthy then blocks in the foreground — launch it detached
(e.g. `Start-Process`) and health-check `http://127.0.0.1:<port>/.well-known/agent-card.json`.
Tear the process down when the work is done; Archons don't idle.

**Deploy from a sandboxed session (Claude Code shell): inject the token first.** Sandboxed shells
don't inherit the **user-level** `CLAUDE_CODE_OAUTH_TOKEN`, so the archon's `claude` CLI falls back
to a stale credential store and every delegation dies with *"OAuth session expired and could not be
refreshed"* (observed in production — the health-check passes, spend fails). Before `Start-Process`:
`$env:CLAUDE_CODE_OAUTH_TOKEN = [Environment]::GetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN','User')`
(never print it). A healthy agent card proves the door opens, not that the lights are on.

## Autonomy (mirrors `autonomy-policy.md`)

Act-low: reading the stable/roster/ledgers, drafting a need statement, scaffolding.
Ask-high: **mint, deploy, admit, delegate, revise, retire** — roster changes and subscription spend.
Held Forge actions ride the normal approval loop with `kind: "archon"`.

## Port registry & roster

The assistant's archons get ports **9701–9749**, one per Archon, assigned at mint and never reused.

| Archon | Port | Status | Charter (one line) |
|--------|------|--------|--------------------|
| `proteus` | 9701 | worked example | Job-application specialist: job-board search → match % → tailored resume/CV in the owner's voice → company research → draft email held for the assistant's gate. Example tools ship under `archons/proteus/tools/`; private inputs (profile, watchlist, voice profile) stay local and gitignored. |

Keep this table current — it's the roster the Forge orients from (a mint/retire updates it as part
of the approved action).
