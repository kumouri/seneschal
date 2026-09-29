# Forge mode — mint & run the staff (Archons)

This file is read **only when the router in `../SKILL.md` dispatches to this mode** — `SKILL.md`
renders into every invocation whole, so each mode's rules live in their own leaf and cost nothing on
the runs that don't need them (`docs/grounding-restructure-spec.md`).

**Read this whole file before acting, then work its steps in order.**

---

**Advisors:** `[trace, orientation, oikonomos, retrieval, dispatch, critique*, gate]` — Critique
reviews any drafted need/charter and anything an Archon drafted for the outside world; the Gate still
matters (mint/revise/retire are ask-high, and every Archon's outbound draft rides the gate), but
deploy/admit/delegate are act-low under the owner's standing authorization (see
`references/autonomy-policy.md`).

Delegate to `../../subagents/archon-forge/SKILL.md`. In short: when a recurring job has *earned* a
persistent specialist, the assistant drives the sibling **Demiurge** meta-agent to mint an **Archon**
(spec + charter + evals + record under `../../archons/stable/`), scaffolds and deploys it with the
**claude-cli** adapter (**subscription-billed** — never the metered-API claude-sdk adapter; same
billing rule as the presence daemon), gates it through its eval suite, and delegates tasks over A2A
with outcomes recorded for tenure. Reads/drafts are act-low, and — by the owner's standing
authorization (the claude-cli adapter is subscription-billed, so there is no API spend for the gate to
guard) — so are **deploy / admit / delegate**; roster changes (**mint / revise / retire**) stay
**ask-high**. The owner's data never enters the public demiurge repo — the roster, ports, paths, and
command crib live in `references/archons.md`; the `../../archons/` layout is routed by
`../../archons/CLAUDE.md`. Archons are staff, not the assistant: whatever they draft for the outside
world comes back through **its** gate — and a *non-spend* objection to a delegation (legal exposure,
an unverifiable posting, an avoid-list hit) is a separate gate that is still held for the owner.
