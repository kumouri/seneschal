# Runbook

## Local development

```bash
npm ci            # preferred: installs exactly from the lockfile
npm run typecheck
npm test
npm run dev       # wrangler dev
```

### ⚠️ package-lock / `npm install` caveat (this environment)

In the current local environment, **`npm install` injects a spurious dependency** into `package.json`,
`package-lock.json`, and `node_modules`:

```json
"dependencies": { "most-capable-agent": "file:../most-capable-agent/.claude/worktrees/..." }
```

npm does not normally invent dependencies — this is the surrounding toolchain/harness modifying
`package.json` after npm runs. It does **not** happen on `npm ci`, and it does **not** happen in clean CI.

**What to do:**
- Prefer `npm ci` locally (installs from the lockfile, no injection).
- If you must `npm install` (e.g. to add a dependency), **scrub the injected entry before committing**:
  remove the `dependencies` block from `package.json` and re-sync (`node` one-liner deletes
  `packages[""].dependencies` and `packages["node_modules/most-capable-agent"]` from the lock), then
  confirm with `git diff`.
- CI uses `npm ci`, so the pipeline is unaffected.

This should be investigated and fixed at the environment level; until then, treat any `most-capable-agent`
line in this repo's manifests as junk to remove.

## Debugging a live call

- Tail logs: `wrangler tail`.
- `GET /status` returns liveness + effective settings.
- Each call writes a `calls` row (D1) with `outcome_stage`, `verdict`, `transcript`, and
  `cost_estimate_usd` — query it to see what the funnel decided and what it cost.

## Cost controls

- Flip `POST_GATE_ACTION=ring_through` to stop paying for Claude conversations (gate-only screening).
- `DAILY_BUDGET_USD` caps daily spend; over the cap, stage 4 downgrades to voicemail.
- Keep `REPUTATION_LOOKUP_ENABLED=false` unless too much spam reaches the gate.

## Common issues

- **Calls go to voicemail instead of the screener:** Google Voice forwarding flakiness — see
  [setup.md](setup.md) §4; consider porting the number to Twilio.
- **Legit automated callers (pharmacy, 2FA) fail the gate:** add them to the allowlist, or expose a
  longer gate timeout.
