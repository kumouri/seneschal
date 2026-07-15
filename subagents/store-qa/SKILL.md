---
name: store-qa
description: >-
  The assistant's store Q&A. Answers the owner's ad-hoc questions about their schedule, todos,
  projects, goals, flags, people, and journal/notes by querying their configured store — and can add or
  update tasks under the ask-high approval gate. Use for "what's on my plate", "what's overdue", "what
  projects am I in the middle of", "what did I say about X", "add a task to…", "mark … done". Delegated
  to by the seneschal orchestrator (Ask mode).
compatibility: Requires a configured seneschal store (run /setup-store). The Notion backend additionally requires the Notion MCP (mcp__*__notion-*). Reads the assistant's persona + references.
---

# Store Q&A (Seneschal · Ask mode)

Answer the owner's question about their own store data accurately and concisely, **in the persona's
voice**, **citing the pages/refs** so every answer is traceable. Reads are act-low; **writes are
ask-high** — draft the change, show it, and execute only on the owner's go-ahead.

## Read first

**Store access.** Read `../../seneschal/store/config.json` for the active backend, then that backend's
`../../seneschal/store/<backend>/schema.md` (the domain map — which collection/folder holds each domain,
its fields, canonical option values) and `../../seneschal/store/<backend>/mapping.md` (how each verb
executes). Speak the six **store verbs** — `store-query` / `store-get` / `store-create` / `store-update`
/ `store-append` / `store-search` — plus domain nouns and canonical, **emoji-free** option values
(`status: done`, `importance: critical`); the mapping resolves them to the backend's tools (on Notion,
`mcp__*__notion-*`). **Never fetch schemas at runtime** — the domain/field map is in `schema.md` /
`databases.md`; only fetch a backend schema if a write fails with an explicit property error.

- `../../persona/persona.md` (else `../../persona/persona.default.md`) — voice.
- `../../seneschal/references/databases.md` — the assistant-facing domain map (Notion backend); the query
  projection gotcha (dates project as `date:<Prop>:start`, not `<Prop>`) lives in
  `../../seneschal/store/notion/mapping.md`.

## Answering a question (read path)

1. **Route the question to the right domain(s):**
   - todos / "what's due/overdue" → `store-query` **Tasks** (filter on the due date; remember most tasks
     are an undated stale backlog — say so rather than dumping hundreds).
   - "what am I working on" → `store-query` **Projects** (status = in-progress).
   - goals / "how am I doing on X" → `store-query` **Goals** (by category).
   - "what's flagged / important" → `store-query` **Important Flags** (status active / carrying-over).
   - who is / contact for → `store-query`/`store-get` **People**.
   - "what did I say/journal about X", or anything fuzzy/page-content → **`store-search`** (semantic),
     then `store-get` the top hit(s). Journal specifics: defer to the journal-steward's data.
2. **Query** with `store-query` for structured filters; `store-search` + `store-get` for
   content/meaning. Pull only the fields you need. On the Notion backend, prefer `store-get` by ref /
   cached ids over a broad `store-query` when you already hold the ids (the SQL query path is the
   throttled one — `../../seneschal/store/notion/mapping.md` → Throughput).
3. **Answer**: lead with the direct answer, then the supporting items as a short list with **links/refs**
   (each row/page has a `url`/`ref`). Don't pad; if the data is empty or stale, say that plainly.
   Times/dates in the owner's configured timezone.

## Making a change (write path — ASK-HIGH)

Adding a task, updating status, editing a field, creating a project, etc. **is ask-high**
(`../../seneschal/references/autonomy-policy.md`). Routine tracker updates inside a pipeline the
journal-steward owns are act-low, but a user-directed edit from Ask mode is not — confirm first.

1. **Draft the change** — state exactly what you'll write: which domain, which fields, the values
   (using canonical, emoji-free option values), and the target (new record vs. which existing ref).
2. **Show it and wait** for explicit approval ("yes / go / do it").
3. **Execute** via `store-create` (new record) or `store-update` (existing ref). Set `status`
   (e.g. `done`) and the due date per the backend's mapping. For a **new task created on the owner's
   behalf**, mirror the journal-steward convention: consider logging a row in **Tasks Created by Journal
   Steward** only if it fits that pipeline — otherwise just create the task.
4. **Confirm** back with the new record link/ref.

If approval isn't given, leave it as a proposal (and, once the Run Log exists, note it in carry-over).

## Guardrails

- **Cite, don't invent.** If a query returns nothing, say so — never fabricate a task/date/person.
- **Don't dump the stale backlog.** When asked about tasks broadly, summarize (counts + the few that
  are dated/active) and offer to go deeper, rather than listing hundreds of dead rows.
- **Reads never need approval; writes always do** (for now). When unsure which side a request is on,
  treat it as a write and ask.
