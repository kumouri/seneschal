# Ask mode — store Q&A

This file is read **only when the router in `../SKILL.md` dispatches to this mode** — `SKILL.md`
renders into every invocation whole, so each mode's rules live in their own leaf and cost nothing on
the runs that don't need them (`docs/grounding-restructure-spec.md`).

**Read this whole file before acting, then work its steps in order.**

---

**Advisors:** `[trace?, orientation, oikonomos, retrieval, dispatch, gate]` — Trace off for read-only
one-offs.

Delegate to `../../subagents/store-qa/SKILL.md`. In short: route the question to the right domain
(`store-query` Tasks / Projects / Goals / Flags / People, or `store-search` for fuzzy/journal content),
answer concisely and **cite the pages/refs**, and treat any **write** (`store-create`/`store-update` a
task, change status) as **ask-high** — draft → approve → execute. Reads are act-low. Defer journal
specifics to the journal-steward.
