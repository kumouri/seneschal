---
name: morning-briefing
description: >-
  The assistant's morning brief. Assembles a single decision-first daily briefing for the owner from
  their calendar and Notion (due/overdue tasks, active Important Flags, in-flight projects, and journal
  carry-over). Mostly reads, but not read-only — it delivers the brief + trace and can act on what it
  surfaces under the act-low / ask-high gate. Use for "morning brief", "what's on today", "catch me up",
  or the scheduled morning run. Delegated to by the seneschal orchestrator (Brief mode).
compatibility: >-
  Requires a configured seneschal store (run /setup-store); the Notion backend additionally requires the
  Notion MCP (mcp__*__notion-*). Calendar comes from a Calendar MCP if one is connected, otherwise from
  the gcal_api.py REST bridge (see ../../seneschal/references/calendar-mapping.md). Reads the
  assistant's persona + references.
---

# Morning Briefing (Seneschal · Brief mode)

Produce one tight, scannable morning brief **in the persona's voice**, lead with what needs the owner
today, and **source every line from real data** — no invention, no padding. Assembling the brief is
mostly reads, but it is **not read-only**: it delivers to chat + (scheduled) the "🗒️ Daily Brief" page
and the Run Log (act-low), and may act on what it surfaces under the act-low / ask-high gate. Email/SMS
delivery attaches in a later phase.

**The Brief is READ-ONLY with respect to `../../seneschal/state/carry-over.md`** — it may read the file
but never writes to it; `carryover_region.py` (`WRAP_SEED`, the Wrap's) and `loops.py render --write`
(`GENERATED`, the open-loops register) stay its only two writers. It may also read `python
../../seneschal/scripts/notes.py list --days N` for ad-hoc witness-only notes worth folding in, but
writes nothing there either — `notes.py`'s only writer is the "note this" path in Chat mode.

## Read first (once)

**Store access.** Read `../../seneschal/store/config.json` for the active backend, then that backend's
`../../seneschal/store/<backend>/schema.md` + `mapping.md`. Speak the six **store verbs** (`store-query`
/ `store-get` / `store-create` / `store-update` / `store-append` / `store-search`) plus domain nouns and
canonical, **emoji-free** option values; the mapping resolves them to the backend's tools. This mode
reads the store (`store-query`/`store-get`) and writes only the one-shot `tomorrow` flag reset below
(`store-update`). **Never fetch schemas at runtime.**

- `../../persona/persona.md` (else `../../persona/persona.default.md`) and
  `../../persona/owner-profile.md` (if present) — voice + who the owner is.
- `../../seneschal/references/briefing.md` — the exact sections, ordering, and rules (this is the spec).
- `../../seneschal/references/databases.md` — the domain/ID map for Tasks, Projects, Important Flags
  (Notion backend).
- `../../seneschal/references/calendar-mapping.md` — the **two calendar doors** (MCP → `gcal_api.py`
  bridge), the include-set, and empty-vs-missing.
- Backend query mechanics (only reading here): `../../seneschal/store/<backend>/mapping.md` — on Notion,
  the `date:<Prop>:start` projection + throttle rules.

## Steps

**1 — Orient + read the pre-stage.** Determine "today" in **the owner's configured timezone**.
(After-midnight = still the prior day for journal/carry-over purposes.) Then read the pre-stage:
`python seneschal/scripts/brief_prestage.py read` — last night's Dream (step 1b) snapshotted this
Brief's store inputs (open Tasks due/overdue today + tomorrow, Active/Carrying-Over Flags, In-Progress
Projects) into `../../seneschal/state/brief-prestage.json`, timestamped (`fetched_at`). A fresh read
exits 0 and prints the payload JSON — it's the **base set**; step 2 only delta-checks it live rather
than re-firing the full `store-query` batch. **A stale or missing payload exits 3** (fresh checkout /
Dream didn't run / the snapshot aged out) — on exit 3, skip this and take the full-batch path noted in
step 2. **`state/context-digest.md` is RETIRED** — it grew into an unbounded catch-all no one pruned;
this pre-stage file (one writer, one reader) replaced its old `## Brief pre-stage (Dream)` block, and
nothing here reads the digest.

**2 — Gather, read-only.** Prefer **pre-staged base + delta live-queries** over a full fan-out (on the
Notion backend this keeps the morning read cheap — `../../seneschal/store/notion/mapping.md` → Throughput).
- **Calendar — try both doors, in order** (`calendar-mapping.md`):
  1. **Calendar MCP** if this session has one: `list_events` for today across the owner's
     **include-set**.
  2. **Otherwise the REST bridge**, one call per calendar in the include-set, issued as one parallel
     batch (`events` reads a single `--calendar`):
     ```sh
     python seneschal/scripts/gcal_api.py events --account personal --env-file seneschal/scripts/google.env \
       --calendar <id> --start <YYYY-MM-DD>T00:00:00<±HH:MM> --end <YYYY-MM-DD>T23:59:59<±HH:MM>
     ```
     **The owner's UTC offset is mandatory** — a bare `--start <YYYY-MM-DD>` is sent as UTC and
     silently returns the wrong day. Take the offset from the owner's timezone (`owner.timezone` in
     `persona/identity.json`) for that date, DST included.

  An owner often has many calendars; don't assume only the primary, and de-dupe any to-do-app
  task-mirror calendars against the store's Tasks. If it's early and today is light, also grab the next
  upcoming event. (Always live — the pre-stage is store-only.)
- **Store — delta path (when the pre-stage read exited 0):** take Tasks / Flags / Projects from the
  payload as the base, then `store-query` only the **delta** the late-evening snapshot can't cover:
  **Tasks** completed today **or** created since `fetched_at` — to drop what's since been done and add
  same-day-new due items; and any **Flag** / **Project** changed since the stamp. Filter on the
  due-date field (on Notion the `date:Due:start` projection, never the bare `Due` — the gotcha lives in
  `../../seneschal/store/notion/mapping.md`).
- **Store — full-batch fallback (exit 3):** run the original three reads in ONE parallel `store-query`
  batch — **Tasks** due on/before today with status not in (done, archived) (most tasks are an undated
  stale backlog, so the date filter is what keeps the brief to genuinely due/overdue items); **Important
  Flags** status in (active, carrying-over); **Projects** status = in-progress. (Exact option strings +
  collection ids per backend are in `databases.md` / `store/<backend>/schema.md`.)
- **Carry-over (always live):** `store-get` the Interstitial Journal and read its top carry-over callout
  — it changes overnight and is never pre-staged.
- **Pending rulings (always live, local — no store read):** read the **Pending** section of
  `../../seneschal/references/proposed-learnings.md`. Every un-ruled proposal goes into the brief's
  **📜 Rulings wanted** section until the owner rules on it (their standing instruction).
- **Code-rendered lines (always live, local — no store read):** three lines arrive **already rendered
  by code**, and for the scheduled run they are ALREADY IN YOUR PROMPT — the daemon's `morning-brief`
  slot launch (`slot_context` in `seneschal/scripts/presence.py`, the combined `_brief_context` hook)
  appends each one with an instruction to print it verbatim, or says there's nothing so that section is
  omitted:
  - the **observation-gate line(s)** — `python seneschal/scripts/observation_gate.py brief-line`
    (`../../seneschal/docs/observation-gate-spec.md`): a gate that just closed, marking watched work as
    ready to continue;
  - the **📋 Unassigned work** line — `python seneschal/scripts/owi_unknowns.py brief-line`: the count
    only, never the items;
  - the **🎯 Tomorrow's Lead** section — `python seneschal/scripts/tomorrow_marker.py brief-line`
    (`../../seneschal/docs/tomorrow-marker-spec.md` §4.1).

  Never run these scripts yourself on a scheduled run, never type a number or list of your own, and
  never re-order the items or add ones of your own — the prompt IS the source. A chat-invoked Brief has
  no such lines in its prompt; there, those three `brief-line` calls are the only sources.
- **`tomorrow` flag reconciliation (`../../seneschal/docs/tomorrow-marker-spec.md` §7):** fold a
  `tomorrow`-flag filter into the SAME store batch above, on both Tasks and Reminders (on the Notion
  backend, the `Tomorrow` checkbox). For every hit, `python ../../seneschal/scripts/tomorrow_marker.py
  reconcile task '<JSON [{"page_id":...,"text":...,"url":...}, ...]>'` (or `reminder`) — idempotent,
  keyed on row id, and it names which row ids to `store-update ... tomorrow: false` right after (the
  same one-shot-input-consumed-on-read shape the `ack` flag already has). **If the field doesn't exist
  (a backend or schema without it) or the query errors, skip this step silently — fail open, never block
  the Brief on it.**

**3 — Assemble** per the shape in `briefing.md`:
- **The observation-gate line(s) lead, then 🎯 Tomorrow's Lead** — both code-rendered (above), placed
  above every other section including "Needs you": a gate that just closed is the day's own leading
  news, and Tomorrow's Lead is the day's own stated plan; neither is a subset of what needs a decision.
  Omit each when empty.
- Compute **"Needs you"** = the 1–3 things that genuinely require a decision/response today (unanswered
  invites, overdue high-priority tasks, a held draft if any, a critical flag).
- **📋 Unassigned work** — the code-rendered line, verbatim; omit when the count is 0.
- **📜 Rulings wanted** = one line per Pending proposal: short title + the specific ask, with lettered
  options when the proposal offers a choice (e.g. *"Water the plants, 9 straight misses — (a) retire /
  (b) digest-only / (c) keep nagging?"*). Decision-first, no re-narrating the full pattern — the detail
  lives in `proposed-learnings.md`; cite it. Omit the section when Pending is empty.
- De-dupe items that appear in multiple sources; surface each once in its most action-relevant section.
- Omit any section that's empty (don't print "nothing here") — except give a one-liner if literally
  nothing needs the owner.
- Times in the owner's configured timezone. Overdue tasks before today's.

**4 — Deliver.** Output the brief in chat; the scheduled run also appends it to the "🗒️ Daily Brief"
page and writes the Run Log (act-low). Email/SMS delivery attaches in a later phase. **The Unassigned
work first-real-message window reset (`owi_unknowns.py note-brief-sent`) is NOT this run's to call:**
`presence.py::reap_finished_slots` fires it the moment the `morning-brief` slot exits clean — the
daemon's own definition of "the Brief delivered" — so a run that forgot it can no longer leave the prior
cycle's window open. If the brief surfaced something actionable, handle it under the gate (act-low
directly; ask-high drafted-and-held) rather than letting it drop. **When the owner rules on a surfaced
proposal** (in the brief conversation or any chat after), follow the `proposed-learnings.md` flow:
approve → apply the change + move it to **Applied** (+ the `autonomy-policy.md` graduation log); decline
→ move it to **Declined**. The edit lands via the usual branch → PR → merge-on-green path.

## Guardrails

- **Cite, don't invent.** If a query returns nothing, the section is empty. Never guess a meeting or due
  date.
- **Write within the gate, but stay a brief.** Don't turn the brief into a triage session; but if it
  surfaces something worth acting on, act-low writes are fine directly and ask-high ones are
  drafted-and-held (`../../seneschal/references/autonomy-policy.md`) — never silently dropped.
- **An empty calendar is not a missing calendar.** A read that succeeds with zero events is *"nothing
  on your calendar today"* — a fact about the owner's day, not about the integration. This is the bug
  the two-door path exists to fix: a brief that reports "Calendar MCP not connected" while a working
  bridge sits in `seneschal/scripts/`.
- Only say the calendar is unreachable when **both** doors failed, and say **which one failed and
  why** ("no Calendar MCP in this session, and the bridge couldn't read `google.env`") — never a bare
  "not connected". Then continue with what you have and tell the owner what would fix it.
