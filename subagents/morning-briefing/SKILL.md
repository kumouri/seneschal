---
name: morning-briefing
description: >-
  The assistant's morning brief. Assembles a single decision-first daily briefing for the owner from
  their calendar and Notion (due/overdue tasks, active Important Flags, in-flight projects, and journal
  carry-over). Mostly reads, but not read-only — it delivers the brief + trace and can act on what it
  surfaces under the act-low / ask-high gate. Use for "morning brief", "what's on today", "catch me up",
  or the scheduled morning run. Delegated to by the seneschal orchestrator (Brief mode).
compatibility: Requires a configured seneschal store (run /setup-store) and a Calendar MCP. The Notion backend additionally requires the Notion MCP (mcp__*__notion-*). Reads the assistant's persona + references.
---

# Morning Briefing (Seneschal · Brief mode)

Produce one tight, scannable morning brief **in the persona's voice**, lead with what needs the owner
today, and **source every line from real data** — no invention, no padding. Assembling the brief is
mostly reads, but it is **not read-only**: it delivers to chat + (scheduled) the "🗒️ Daily Brief" page
and the Run Log (act-low), and may act on what it surfaces under the act-low / ask-high gate. Email/SMS
delivery attaches in a later phase.

## Read first (once)

**Store access.** Read `../../seneschal/store/config.json` for the active backend, then that backend's
`../../seneschal/store/<backend>/schema.md` + `mapping.md`. Speak the six **store verbs** (`store-query`
/ `store-get` / `store-create` / `store-update` / `store-append` / `store-search`) plus domain nouns and
canonical, **emoji-free** option values; the mapping resolves them to the backend's tools. This mode only
reads the store (`store-query`/`store-get`). **Never fetch schemas at runtime.**

- `../../persona/persona.md` (else `../../persona/persona.default.md`) and
  `../../persona/owner-profile.md` (if present) — voice + who the owner is.
- `../../seneschal/references/briefing.md` — the exact sections, ordering, and rules (this is the spec).
- `../../seneschal/references/databases.md` — the domain/ID map for Tasks, Projects, Important Flags
  (Notion backend).
- `../../seneschal/references/calendar-mapping.md` — Calendar MCP tools + finding the primary calendar.
- Backend query mechanics (only reading here): `../../seneschal/store/<backend>/mapping.md` — on Notion,
  the `date:<Prop>:start` projection + throttle rules.

## Steps

**1 — Orient + read the pre-stage.** Determine "today" in **the owner's configured timezone**.
(After-midnight = still the prior day for journal/carry-over purposes.) Then read
`../../seneschal/state/context-digest.md`'s **`## Brief pre-stage (Dream)`** block — last night's Dream
snapshotted this Brief's store inputs (open Tasks due/overdue today + tomorrow, Active/Carrying-Over
Flags, In-Progress Projects) there, timestamped. It's the **base set**; step 2 only delta-checks it live
rather than re-firing the full `store-query` batch. **If the block is absent or its stamp isn't last
night** (fresh checkout / Dream didn't run), skip this and take the full-batch path noted in step 2.

**2 — Gather, read-only.** Prefer **pre-staged base + delta live-queries** over a full fan-out (on the
Notion backend this keeps the morning read cheap — `../../seneschal/store/notion/mapping.md` → Throughput).
- **Calendar:** `list_events` for today across the owner's **include-set** of calendars (see the
  confirmed list in `calendar-mapping.md` — an owner often has many calendars; don't assume only the
  primary, and de-dupe any to-do-app task-mirror calendars against the store's Tasks). If it's early and
  today is light, also grab the next upcoming event. (Always live — the digest is store-only.)
- **Store — delta path (when the pre-stage block is fresh):** take Tasks / Flags / Projects from the
  block as the base, then `store-query` only the **delta** the late-evening snapshot can't cover:
  **Tasks** completed today **or** created since the block's stamp — to drop what's since been done and
  add same-day-new due items; and any **Flag** / **Project** changed since the stamp. Filter on the
  due-date field (on Notion the `date:Due:start` projection, never the bare `Due` — the gotcha lives in
  `../../seneschal/store/notion/mapping.md`).
- **Store — full-batch fallback (no fresh block):** run the original three reads in ONE parallel
  `store-query` batch — **Tasks** due on/before today with status not in (done, archived) (most tasks are
  an undated stale backlog, so the date filter is what keeps the brief to genuinely due/overdue items);
  **Important Flags** status in (active, carrying-over); **Projects** status = in-progress. (Exact
  option strings + collection ids per backend are in `databases.md` / `store/<backend>/schema.md`.)
- **Carry-over (always live):** `store-get` the Interstitial Journal and read its top carry-over callout
  — it changes overnight and is never pre-staged.
- **Pending rulings (always live, local — no Notion read):** read the **Pending** section of
  `../../seneschal/references/proposed-learnings.md`. Every un-ruled proposal goes into the brief's
  **📜 Rulings wanted** section until the owner rules on it (their standing instruction).

**3 — Assemble** per the shape in `briefing.md`:
- Compute **"Needs you"** = the 1–3 things that genuinely require a decision/response today (unanswered
  invites, overdue high-priority tasks, a held draft if any, a critical flag).
- **📜 Rulings wanted** = one line per Pending proposal: short title + the specific ask, with lettered
  options when the proposal offers a choice (e.g. *"Water the plants, 9 straight misses — (a) retire /
  (b) digest-only / (c) keep nagging?"*). Decision-first, no re-narrating the full pattern — the detail
  lives in `proposed-learnings.md`; cite it. Omit the section when Pending is empty.
- De-dupe items that appear in multiple sources; surface each once in its most action-relevant section.
- Omit any section that's empty (don't print "nothing here") — except give a one-liner if literally
  nothing needs the owner.
- Times in the owner's configured timezone. Overdue tasks before today's.

**4 — Deliver.** Output the brief in chat; the scheduled run also appends it to the "🗒️ Daily Brief"
page and writes the Run Log (act-low). Email/SMS delivery attaches in a later phase. If the brief
surfaced something actionable, handle it under the gate (act-low directly; ask-high drafted-and-held)
rather than letting it drop. **When the owner rules on a surfaced proposal** (in the brief conversation
or any chat after), follow the `proposed-learnings.md` flow: approve → apply the change + move it to
**Applied** (+ the `autonomy-policy.md` graduation log); decline → move it to **Declined**. The edit
lands via the usual branch → PR → merge-on-green path.

## Guardrails

- **Cite, don't invent.** If a query returns nothing, the section is empty. Never guess a meeting or due
  date.
- **Write within the gate, but stay a brief.** Don't turn the brief into a triage session; but if it
  surfaces something worth acting on, act-low writes are fine directly and ask-high ones are
  drafted-and-held (`../../seneschal/references/autonomy-policy.md`) — never silently dropped.
- If a calendar isn't connected or the primary is ambiguous, say so in the brief and continue with what
  you have; ask the owner to confirm the calendar.
