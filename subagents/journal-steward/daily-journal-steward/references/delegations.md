# Phase 4 — Delegations (extension hooks)

The steward can delegate subsystem work to **sibling skills** — separate skill folders beside
`daily-journal-steward/`, each owning its own tracker(s). **The core ships none**; this file defines the
hook points and the wiring rules so you can add your own. The porting recipe is
`../../CONVERSION-PATTERN.md`.

## The two wiring shapes

| Shape | When it runs | How DJS relates to it |
|-------|--------------|------------------------|
| **Inline** | in this same session, during Phase 4 | Load the sibling's `SKILL.md` (`../../<skill-name>/SKILL.md`) and execute it against the entries captured in Phase 1. Note in the run log that it ran (and counts, if surfaced). |
| **On-demand** | outside the daily run, when the owner invokes it | DJS does **not** do the sub-skill's work itself. Its only duty: when it notices that work is outstanding, add a carry-over note (`clear-and-carryover.md`) so nothing is silently dropped. |

Independent inline delegations run against the **same** Phase-1 capture, so kick them off together in
one batch.

## Rules

- **Single ownership.** When a sibling skill owns a database, DJS stops writing to that database
  entirely — one writer per tracker. Record the handoff in the sibling's `SKILL.md` and note it in
  `databases.md` so no future run double-writes.
- **Nothing silently dropped.** Every delegation leaves a trace: inline runs get a run-log line;
  outstanding on-demand work gets a carry-over note.
- **Outbound stays gated.** A sub-skill that sends anything (email, chat) goes through the framework's
  comms bridges (`../../../../seneschal/scripts/`) and the draft-and-hold autonomy policy
  (`../../../../seneschal/references/autonomy-policy.md`) — never its own send path.
- **Budget.** Inline delegations are optional enrichment: if the run is budget-tight, skip them and
  record the skip in Carry-Over Context so the next run (or an on-demand invocation) picks them up.
