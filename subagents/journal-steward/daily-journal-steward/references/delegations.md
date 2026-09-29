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

## Gated delegations — when the outcome is load-bearing

Some delegations are not optional enrichment: missing one, or running it twice, costs something real —
an outbound message that never goes, or goes twice to the same recipient. For those, **a sentence in
this file cannot be the owner.** "Invoke X at the end of the run" is prose a turn decides whether to
honour, and over enough runs it will be skipped, deferred to another mode ("the Brief will handle
it"), or double-run. The fix is structural, not a firmer sentence. (The core ships no gated sibling;
this is the shape to build one to.)

1. **A deterministic gate script owns the decision.** Its `status` subcommand prints one line and
   exits with a distinct code per state — e.g. *not due today* / *already done (when, by which
   surface)* / *unknown (can't tell — do nothing, the backstop retries)* / *due and not done*.
2. **The action goes only through the gate's recording wrapper** (e.g. a `send --surface djs`
   subcommand around the comms bridge), which refuses a duplicate, enforces any approval hold, and
   writes the ledger row that makes the outcome a fact. The sibling skill never calls the bridge
   directly.
3. **DJS is a reader.** At the end of the run it calls `status`; on *due and not done* it **may** run
   the sibling inline, and it reports the wrapper's outcome verbatim (sent / held for approval /
   refused, with why). On any other code it records what `status` said and stops.
4. **The daemon is the backstop.** A supervised daemon task runs the gate's `ensure` on a cadence and,
   past a stated time with nothing recorded, spawns the sibling itself as a background job
   (`../../../../seneschal/scripts/jobs.py`). So if DJS is cut short, nothing needs handing off.

**There is no "defer to another mode" path.** DJS never writes that the work "will happen later",
"self-sends", or is "handled by the Brief"; it writes what `status` returned. Any other mode that
mentions the same work does the same — reports `status` and names the daemon as the owner.

## Wiring summary

The **daemon owns the DJS run itself** — `presence.py`'s `daily-journal` slot at 05:00
(`../../../../seneschal/scripts/SCHEDULING.md` §1). Sibling skills need **no schedule of their own**:
inline ones run in the same session against the Phase-1 capture, on-demand ones run when invoked, and a
gated one is read at the end of the run with the daemon's `ensure` as its backstop.
