# Store federation — the multi-store vision (design note, not built)

> **Status: reserved intent, zero v1 code.** This documents where the store layer is heading so
> the v1 shape doesn't paint it into a corner. Nothing here is implemented; the `domains` key in
> `config.json` is reserved for it.

## The problem

Most people's information is scattered — tasks in one app, notes in another, calendar in a third,
documents in a fourth. A chief-of-staff assistant is most useful when it can hold **one source of
truth** across all of them and make decisions out of the whole picture, rather than forcing
everything into a single tool first.

The v1 store layer is deliberately built so this is an extension, not a rewrite: skills already
speak backend-neutral **store verbs** against an **active backend**, and a backend is just a
`store/<name>/` pair of `schema.md` + `mapping.md`. Federation generalizes "the active backend"
from one global choice to a **per-domain** choice, plus read-only mirrors.

## How it would work

**Per-domain home backend.** The reserved `domains` key in `config.json` maps a domain to the
backend that owns it:

```json
"domains": {
  "tasks":    "notion",
  "journal":  "obsidian",
  "calendar": "google"
}
```

Absent a per-domain entry, a domain falls back to the top-level `active` backend (today's
behavior). Skills don't change — `store-query tasks …` resolves through whichever backend owns
`tasks`. The verb layer already isolates them from that decision.

**Read-only mirrors.** A domain could name a home (read/write) plus mirrors (read-only) it also
searches — so `store-search` can sweep across Notion notes *and* an Obsidian vault *and* Drive
docs, while writes still land in one authoritative place. The existing local RAG index
(`rag_index.py`, already store-neutral) is the natural substrate: ingest from every mirror,
retrieve across all of them.

**More backends.** Two are sketched as future `store/<backend>/` registries:
- **Logseq** — same verbs, block-property frontmatter (`property:: value`) and a journals/pages
  layout instead of one-note-per-record. A mapping.md, no engine change.
- **Google Workspace** — a *partial* backend whose `mapping.md` resolves verbs to **script
  invocations** over the REST bridge that already ships (`gcal_api.py`, `gmail_api.py`,
  `google_common.py`): tasks↔Google Tasks, reminders↔Calendar, people↔Contacts. This proves the
  verb layer supports MCP-, filesystem-, and script-mediated backends alike.

## What stays true

- Skills keep speaking the six verbs and the canonical domain vocabulary; federation is entirely
  a resolution-layer concern.
- Writes remain single-authoritative per domain (no multi-master sync to reconcile).
- The daemon still forwards only the MCP configs the active/home backends need.

When this is built, it lands as a `resolve_backend(domain)` step in the verb layer + a federation
section in `store/README.md` — additive, behind the same seam v1 already established.
