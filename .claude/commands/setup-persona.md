---
description: Build or change your assistant's persona (name, voice, demeanor) via a short interview
argument-hint: [optional, e.g. "rename the assistant to Piper" or "make it warmer"]
---

Run the **persona wizard**: read `subagents/persona-wizard/SKILL.md` and follow it exactly.

- If a request is present, treat it as the interview's starting point (e.g. a rename request
  jumps straight to the name step, confirms, regenerates).
- If the persona is already configured, take the re-run edit path — summarize, ask what to
  change, don't re-interview from scratch.
- Standalone runs still update the shared setup ledger (the SKILL's close step), so `/setup`
  sees this chapter as done.

The request (if any): $ARGUMENTS
