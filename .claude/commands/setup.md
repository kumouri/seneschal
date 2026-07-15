---
description: First-run setup — build your assistant's persona and connect your data store
argument-hint: [optional]
---

Run the full first-run setup as a single guided flow. Welcome the owner briefly, explain the two
chapters, then run them in order:

1. **Persona** — read `seneschal/subagents/persona-wizard/SKILL.md` and run it (the assistant's
   name, voice, demeanor, channel identities). It owns `assistant.*` + `persona/persona.md`.
2. **Store + profile** — read `seneschal/subagents/store-setup/SKILL.md` and run it (pick the
   data backend, provision it, read existing content, interview to build
   `persona/owner-profile.md`). It owns `owner.*` + `owner-profile.md`.

Because the persona chapter runs first, the store chapter's interview should **confirm** any owner
fields already captured (timezone, name) rather than re-asking. Either chapter is fully skippable
— skipping both leaves the working default-Claude persona and no store (the owner can run
`/setup-persona` or `/setup-store` later).

Close by printing what was written and the next steps (start the daemon, `/brief`).

The request (if any): $ARGUMENTS
