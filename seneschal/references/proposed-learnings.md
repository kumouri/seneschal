# Proposed learnings (the held queue)

How the assistant *learns from the day* without changing its own behavior behind the owner's back.
**Dream mode** (nightly) watches for repeated patterns and writes **proposals** here; each one is
**ask-high** — it becomes policy only when the owner approves it. This is the gated middle path: the
assistant gets sharper over time, but every behavior change is the owner's call and is auditable.

## The flow

1. **Dream proposes.** When the assistant notices a repeated pattern worth encoding — *"you decline
   every recruiter invite," "you archive the X newsletter every time," "you always ask me to remind
   you about meds at 9"* — it appends a proposal under **Pending** and surfaces it to the owner
   (Telegram / Notion / the next Brief). It does **not** act on it.
2. **The owner rules.** Approve → the assistant applies it and moves it to **Applied**. Decline → it
   moves to **Declined** (so the same thing doesn't keep getting re-proposed).
3. **The assistant applies (on approval only).** It makes the concrete change — edit
   `autonomy-config.json` (e.g. graduate an action ask-high → act-low) and/or the relevant reference
   (a noise rule in `comms-mapping.md`, a default in `briefing.md`) and/or the persona — **and logs
   it** in the `autonomy-policy.md` graduation log with the date and trust basis. An outward-facing
   action (sending, accepting/declining invites, posting) only ever graduates by the owner's explicit
   decision (see the graduation criteria in `autonomy-policy.md`).

## Proposal format

```
- [ ] <YYYY-MM-DD> — <short title>
  Pattern: <what the assistant observed, with how many times / over what span>
  Proposal: <the specific behavior change being requested>
  Applies to: <autonomy-config.json | comms-mapping.md | briefing.md | persona | …>
  Gate check: <which graduation criteria it clears / why it's safe — or why it needs a human call>
```

**Worked example — a salience prune recommendation** (shape only; a real one is drafted by
`scripts/salience_rollup.py --propose` and lands here via Dream's PR **only after** the evidence window
is met — ≥30 days / ≥50 tagged docs — and never for a θ_protect-shielded category; see `salience.md`):

```
- [ ] 2026-08-15 — Salience: `ephemeral.ack` predictions look safe to soft-prune (gated)
  Pattern: 61 disposable-tagged docs in `ephemeral.ack` accrued 2 recall hits over 34 days
  (mean 0.03/doc; no protecting forgetting-event). The disposability prediction held.
  Proposal: mark this category's tagged docs older than N days disposable=2 (approved-forgotten —
  SOFT prune: excluded from answers, fully reversible, still counted for un-forget evidence).
  No hard deletion; that would be a separate, later gate.
  Applies to: state/rag-index.sqlite chunk rows (via a small marking script) + references/salience.md.
  Gate check: evidence window met; category is not θ_protect-shielded; the mark is reversible.
```

## Pending

*(nothing yet)*

## Applied

*(nothing yet)*

## Declined

*(nothing yet)*
