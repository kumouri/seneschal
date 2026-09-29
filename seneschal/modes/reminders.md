# Reminders mode — nudges & accountability

This file is read **only when the router in `../SKILL.md` dispatches to this mode**.

**Read this whole file before acting, then work its steps in order.**

---

**Advisors:** full chain **+ Prioritize** — `[trace, orientation, oikonomos, retrieval, dispatch, gate,
prioritize]`; a status digest is a **pull** surface (rank, show all), an actual nudge is **push**
(adaptive vital-few) and still fires staggered, one per buzz — the gate picks the few, it never merges
them. Why: `references/advisor-chain.md` → "Per-mode composition".

Delegate to `../../subagents/reminders/SKILL.md`. In short: the assistant tracks what the owner wants
reminding of — recurring habits, today's unconfirmed todos, and deadlines approaching — in the
**Reminders** domain of the store, and fires each reminder at its **own configured time**: a
once-per-owner-local-day **seed** run (`presence.maybe_seed_day`, date-rollover) queues each row's
`times` (or its `time_window` default) and the ~5 s delivery tick fires each at its minute — plus on
demand. It **expects a response**: `nag_until_done` items re-fire until confirmed done (that flag alone
decides it, not importance); the rest fire once and accumulate misses, and a repeated low-stakes skip
earns **one dry, factual rib** (never shaming). Behavior — times + seed, state machine, rib threshold,
tone ladder — lives in `references/reminders-policy.md`. The tracker writes are **act-low**; flipping a
*linked* Task/Goal to Done is **ask-high** (propose it). v1 ack = the one-tap `ack` affordance or
telling the assistant in chat — either way the run `store-update`s the row (`status: done` for every
type — done *for today*, still active; `status: finished` retires it, and on the ack path is
explicit-only — `last_acknowledged: today`, misses zeroed) and **resets `ack`**; `last_acknowledged` is
the durable done-record the Wrap reads. The **seed** also **auto-retires a completed one-off** so a
one-time task the owner already did can't resurrect as overdue — its conditions, safety rail and
log-every-one rule: `references/reminders-policy.md` → "Auto-retire a completed One-off (seed pass)".
