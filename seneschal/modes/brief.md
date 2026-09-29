# Brief mode — phase plan

This file is read **only when the router in `../SKILL.md` dispatches to this mode** — `SKILL.md`
renders into every invocation whole, so each mode's rules live in their own leaf and cost nothing on
the runs that don't need them (`docs/grounding-restructure-spec.md`).

**Read this whole file before acting, then work its steps in order.**

---

**Advisors:** full chain **+ Prioritize (pull → rank, show all)** — `[trace, orientation, oikonomos,
retrieval, dispatch, gate, prioritize]`. A Brief is a surface the owner opens: order it decision-first,
but show everything.

Delegate to `../../subagents/morning-briefing/SKILL.md`; it owns the details. In short:

**Phase 0 — Orient.** Read persona + owner profile, then the most recent Run Log (`state/run-log.md`)
+ carry-over (`state/carry-over.md`, including the open-work register's generated region —
`scripts/loops.py render`) and the journal's carry-over callout. **The Brief is READ-ONLY with respect
to `state/carry-over.md`** — it may read the file but never writes to it; `scripts/carryover_region.py`
(the Wrap's `WRAP_SEED` region) and `loops.py render --write` (the `GENERATED` region) stay its only two
writers (`docs/carry-over-region-spec.md`). It may also read `python scripts/notes.py list --days N`
for ad-hoc witness-only notes worth folding in, but writes nothing there either — `notes.py`'s only
writer is the "note this" path in `chat.md`. **`state/context-digest.md` is retired** — it is no longer
written and nothing here reads it; the Run Log + carry-over are the whole of cheap local orientation
now. Determine "today" in the owner's timezone.

**Phase 1 — Gather (read-only) — pre-staged block first, then delta live-queries.**
- **Store (pre-staged):** last night's Dream snapshotted the Brief's Phase-1 store inputs — open Tasks
  due/overdue (today + tomorrow), Active/Carrying-Over Important Flags, in-flight Projects — into their
  own tiny store, `state/brief-prestage.json` (`scripts/brief_prestage.py` — **this store has exactly
  one writer, Dream step 1b, and one reader, this phase**). **Read it first, via `python
  scripts/brief_prestage.py read`, and treat it as the base set** — do **not** re-fire the full 4-read
  `store-query` batch on a fresh read. A fresh read exits 0 and prints the payload JSON; **a stale or
  missing payload exits 3 with a one-line reason** (too old, absent, or corrupt) — on exit 3, fall back
  to the full parallel `store-query` batch exactly as if the store had never existed, and don't treat
  the exit as an error to surface. On a fresh read, issue only **delta** live `store-query`s for what the
  snapshot can't have (it's ~22:00 the night before): Tasks completed today or newly created since its
  `fetched_at` (to drop what's since done and add same-day new due items), and any Flag/Project changed
  since that stamp. Always still fetch the **journal carry-over** callout live (cheap, and it changes
  overnight). Filter on the due-date field, not the raw name — on the Notion backend that's the
  `date:<Prop>:start` projection (see `store/notion/mapping.md`). See `references/databases.md` +
  `references/briefing.md`. Every job the retired digest used to do now has its own home: this store
  for the pre-staged reads, the Run Log for the condensed day, `state/open-loops.json` for open loops,
  and `state/standing-safety.json` for the standing-safety "read first" block (injected automatically
  by `presence.py`'s cold grounding — Brief mode never has to read it itself).
- **Calendar:** today's events (and the next event if early) — always live (not pre-staged; the
  pre-stage above is store-only). **Two doors: Calendar MCP if this session has one, otherwise the
  `gcal_api.py` bridge** (`--env-file seneschal/scripts/google.env`, owner-offset `--start`/`--end`).
  **Zero events = "nothing on your calendar today"; only say "no calendar" if BOTH doors failed, and
  name which.** See `references/calendar-mapping.md`.
- **Pending rulings:** read `references/proposed-learnings.md` → **Pending** (a local file — free, no
  store call). Every un-ruled proposal appears in the brief's **📜 Rulings wanted** section each morning
  until the owner rules on it (shape + rules in `references/briefing.md`).
- **Tomorrow reconciliation (`docs/tomorrow-marker-spec.md`):** in the same `store-query` batch, also
  filter Tasks + Reminders on `tomorrow: true` (on the Notion backend, the `Tomorrow` checkbox). For
  each hit, call `python scripts/tomorrow_marker.py reconcile task '<JSON rows>'` (or `reminder` for the
  reminder rows) — it upserts each row into `state/tomorrow.json` (idempotent, keyed on the row id) and
  returns which row ids to untick; `store-update` each one to `tomorrow: false` right after. **If the
  field doesn't exist or the query errors, skip this silently** — fail open, never block the Brief on
  it.

**Phase 2 — Assemble & deliver.** Produce one tight brief in the persona's voice, decision-first
(`references/briefing.md` defines the shape) — including the **observation-gate line(s)**
(`docs/observation-gate-spec.md`), the **📋 Unassigned work** line, and the **🎯 Tomorrow's Lead**
section (`docs/tomorrow-marker-spec.md`), all three of which **arrive in the run's own prompt, already
rendered by code**: the daemon's morning launch (`SLOTS_TEMPLATE` → the `morning-brief` slot's context
hook in `scripts/presence.py`) appends `observation_gate.brief_line()`'s text,
`owi_unknowns.brief_line()`'s text, and `tomorrow_marker.brief_line()`'s text and says to print each
verbatim, or says there's nothing so that section is omitted. **The observation-gate line(s) LEAD —
above Tomorrow's Lead, which sits ABOVE "Needs You"**: a gate that just closed is the day's own leading
news (work that was waiting on evidence is ready to continue), Tomorrow's Lead is the day's own stated
plan, and neither is a subset of what needs a decision. Never run any of these three scripts yourself
or type a number/list — the prompt IS the source (a chat-invoked Brief has no such line; there,
`python scripts/observation_gate.py brief-line` / `python scripts/owi_unknowns.py brief-line` /
`python scripts/tomorrow_marker.py brief-line` are the only sources). Output it in chat; the scheduled
run also appends it to the Daily Brief page (`store-append`) and writes the Run Log (act-low). **The
unassigned-work first-real-message window reset (`note-brief-sent`) is the daemon's, not this run's:**
`presence.py` calls `owi_unknowns.note_brief_sent` the moment the `morning-brief` slot exits clean. Do
not call it from the run. If the brief surfaces something actionable, handle it under the gate (act-low
directly; ask-high held).
