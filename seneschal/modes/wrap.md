# Wrap mode — end-of-day

This file is read **only when the router in `../SKILL.md` dispatches to this mode** — `SKILL.md`
renders into every invocation whole, so each mode's rules live in their own leaf and cost nothing on
the runs that don't need them (`docs/grounding-restructure-spec.md`).

**Read this whole file before acting, then work its steps in order.**

---

**Advisors:** full chain **+ Prioritize (pull → rank, show all)** — `[trace, orientation, oikonomos,
retrieval, dispatch, gate, prioritize]`. Like the Brief, a Wrap is a pull surface: ranked, shown in
full.

Delegate to `../../subagents/eod-wrap/SKILL.md`: a short evening recap (done / slipped / waiting on you /
tomorrow preview), write-enabled under the gate. Complements the morning Brief. "Done today" comes from
**both** Tasks (`completed` = today) **and** Reminders (`last_acknowledged` = today, `status` in (done,
finished) — both count as acked-done: `done` = done-for-today, `finished` = a retired item acked on its
way out) — so acks made over Telegram/chat count; never judge completion by the one-tap `ack`
affordance (it's consumed + reset by the reconciling seed runs).

Seeding carry-over (step 4 there) goes through `scripts/carryover_region.py write-region --name
WRAP_SEED` — a bounded region, replaced whole every night, never the whole file
(`docs/carry-over-region-spec.md`).

Before composing that block, read today's and yesterday's ad-hoc notes (read-only) — `python
scripts/notes.py list --days 2` — for anything worth folding into a decision-shaped line. **Read only;
never copy them into `WRAP_SEED` wholesale** — a witness-only note has no closing event by design
(`docs/carry-over-region-spec.md`), so pasting the raw file into a nightly-rebuilt region would just
relocate the same never-revisited accumulation the region was built to stop.

**Tomorrow reconciliation (`docs/tomorrow-marker-spec.md`)**, in the same read pass as step 2: filter
Tasks + Reminders on `tomorrow: true` (on the Notion backend, the `Tomorrow` checkbox), call `python
scripts/tomorrow_marker.py reconcile task '<JSON rows>'` (or `reminder`), then `store-update` each
returned row id to `tomorrow: false`. **A missing/erroring field fails open** — skip silently, never
block the Wrap.

**Tomorrow's Lead — two pickers, never a prose list** (`docs/tomorrow-marker-spec.md`). Run `python
scripts/tomorrow_marker.py wrap-ask` — sends the Close/Roll/Drop grid over every item still marked for
today, one row per item, and does nothing (no send) when nothing is marked. Then offer the **separate**
"what's tomorrow's lead?" multi-select over today's own gather (step 2's tomorrow candidates — due Tasks
/ near-due deadline watches): `python scripts/tomorrow_marker.py lead-ask '<JSON candidates>'`, each
candidate carrying a real `why` (every picker option needs a description). Both taps resolve through
`scripts/presence.py`'s callback dispatch — the Wrap run never writes `state/tomorrow.json` itself. A
linked item's Close comes back as an ask-high proposal to flip the linked Task/Reminder to done; relay
it, never flip it yourself.
