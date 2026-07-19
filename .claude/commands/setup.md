---
description: First-run setup — the unified, resumable wizard (persona, store, auth, channels, cockpit)
argument-hint: [optional chapter id to jump to, e.g. "persona" or "env:telegram"]
---

Run the unified setup wizard: read `subagents/setup/SKILL.md` and follow it exactly — it owns
the chapter order, the ledger conventions, and the chapter files under
`subagents/setup/chapters/`. Keep this command thin; don't restate its rules.

1. **Ledger first.** `python seneschal/scripts/setup_state.py infer` (reconcile with reality —
   respect pre-wizard work), then `python seneschal/scripts/setup_state.py board` (show it).
2. **Jump** if the request names a chapter id from the board (`persona`, `store`,
   `owner-interview`, `auth-models`, `env:<id>`, `mcp:<server>`, `cockpit`, …): go straight to
   that chapter, whatever its status (a done chapter summarizes and offers a redo).
3. **Resume** otherwise: welcome the owner briefly (first run) or note what's already done
   (returning run), then continue at the first chapter that is pending / in-progress /
   awaiting-auth-restart / stale, in the SKILL's chapter order.

The request (if any): $ARGUMENTS
