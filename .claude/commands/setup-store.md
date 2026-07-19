---
description: Pick and set up your data backend (Notion / Obsidian / Markdown) and build your owner profile
argument-hint: [optional, e.g. "use an obsidian vault" or "switch to markdown"]
---

Run the **store onboarding wizard**: read `seneschal/subagents/store-setup/SKILL.md` and follow
it exactly.

- If a request names a backend, start at Phase 1 with that choice pre-selected (still confirm).
- If a store is already configured, take the Phase 0 detect path — offer reconfigure / switch /
  just-the-interview / abort; never overwrite silently.
- Standalone runs still update the shared setup ledger (the SKILL's close step), so `/setup`
  sees this chapter as done.

The request (if any): $ARGUMENTS
