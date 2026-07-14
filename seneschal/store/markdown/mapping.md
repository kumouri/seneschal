# markdown/mapping.md — the six store verbs on the plain-Markdown backend

HOW each backend-neutral **store verb** (`../README.md`) becomes a concrete Claude Code
**Read / Write / Edit / Glob / Grep** call against a plain folder of Markdown. No MCP, no plugin, no app.
Field names, canonical option values, folder layout, and the `ref = root-relative path` rule come from
`schema.md` — this file is only the execution plan.

> **Sync note.** This backend is the same engine as `../obsidian/`. The verb-to-operation mapping and the
> worked ack example below **mirror `obsidian/mapping.md`**; **if they ever conflict, fix both.** The **only**
> deltas: paths are `<root>/…` directly (no vault subfolder); folders/filenames are **lowercase kebab-case**;
> relations are **root-relative path strings** (already a `ref` — follow directly, no wikilink parse); there
> is no `search_whole_vault` toggle (there is no vault — search is always the whole `<root>`).

**Setup.** `<root>` = `backends.markdown.root_path` (e.g. `~/seneschal-data`, `~` expanded by the reader),
from `store/config.json`. All paths below are `<root>/…`. Records are one file each; frontmatter is the
source of truth; the canonical field order in `schema.md` guarantees a domain's fields sit in a known,
adjacent arrangement inside every file.

**Throughput.** Batch independent reads in one message (parallel `Grep`/`Read`). Prefer *fewer, better*
reads: a single anchored `Grep -A <n>` over a domain folder pulls each survivor's whole frontmatter block in
one pass — only `Read` the files whose **bodies** you actually need. Never Glob-then-Read a whole domain to
filter in your head.

---

## `store-query <domain> where <filter>` → anchored Grep over the domain folder

List records in a domain by a frontmatter filter.

1. **Grep an anchored frontmatter pattern** in the domain's folder. Anchor on the field line so you match the
   frontmatter value, not prose:
   - one value: `^status: done$`
   - a set: `^status: (pending|reminded|snoozed)$`
   - existence / date compare: `^due: 2026-07-14$`, or `^due: ` then compare in-parse.
   Use `-A <n>` sized to the domain's frontmatter block (e.g. `-A 18` for reminders, `-A 11` for tasks) so
   the **whole block** comes back in the same result. Restrict with `path` = `<root>/<folder>/` and
   `glob` = `*.md`.
2. **Parse fields from the returned block.** Canonical field order (schema.md) guarantees every field is
   inside the `-A` window, so you read them straight out of the Grep output — no second pass for a sibling
   field.
3. **`Read` only survivors that need bodies** (narrative, progress log, capture candidates). A count/id-only
   query needs no Read at all.
4. **Exclude sync-conflict files** (see Hazards).

Multi-field filters: Grep the most selective field first, then confirm the rest from the same `-A` block, or
intersect two greps by path. The `ref` of each match is its path.

## `store-get <ref>` → Read the path

`Read` the file at `<ref>` (a root-relative path, e.g. `tasks/ship-the-spec.md`). Parse frontmatter for
fields, body for narrative. That's the whole verb — the `ref` *is* the location, so there is no id-cache
lookup (this replaces Notion's id resolution). A relation value is already a `ref`, so following a relation is
just `store-get` on that string.

## `store-create <domain>` → Write a new file from the template

1. Compute the **filename**: kebab-case the `<title>` (lowercase, spaces → `-`, illegal set
   `\ / : * ? " < > | # ^ [ ]` stripped, collapsed hyphens, ≤ 80 chars). If `<root>/<folder>/<name>.md`
   exists, append `-2`, `-3`, … Date-keyed domains (journal, briefs, runs) use their date/mode filename
   instead (schema.md).
2. **Write** the file: frontmatter in the domain's **canonical field order** (schema.md), option values
   verbatim (emoji-free), relations as root-relative path strings, unknowns left blank/omitted — then the body
   template.
3. The new file's root-relative path is its `ref`; return it. **Never** overwrite a colliding file — the `-2`
   suffix keeps both.

## `store-update <ref>` → targeted Edit(s) on frontmatter lines

Set fields on an existing record.

1. **Read** the file (or reuse the block from a prior `store-query`) to get exact current lines.
2. **Edit** the frontmatter line(s). Because canonical order fixes which lines are adjacent, fields that
   change together are set by **one multi-line Edit**; unrelated fields get their own single-line Edit. Match
   the whole `key: value` line as `old_string` so the replace is unambiguous.
3. Frontmatter only — the body is untouched (that's `store-append`). If a value implies another field (task →
   `done` implies stamping `completed`), edit both, adjacent where the schema puts them.

## `store-append <ref>` → Edit appending to the body

Add body content (a progress note, a journal bullet, a run phase line) **without touching frontmatter**.
`Edit` by matching an end-of-body anchor (the last line, or a `## Progress log` heading) and replacing it
with itself-plus-the-new-content. For a **daily note** (`journal/YYYY-MM-DD.md`) append a new `- HH:MM …`
bullet — creating the file first (`store-create` for the journal domain) if today's doesn't exist yet.

## `store-search <query>` → Grep across the root + the local RAG index

Fuzzy / semantic recall.

1. **Lexical:** `Grep` the query (content mode, case-insensitive) across all of `<root>/`. (There is no vault
   subfolder and no `search_whole_vault` toggle — the root *is* the search scope.) Exclude sync-conflict
   files.
2. **Semantic:** also call the local RAG index —
   `python seneschal/scripts/rag_query.py "<query>" --k <n>` — which returns best-first JSON
   (`ref`, `source`, `score`, `text`) over journal/notes/run-log. It's **additive**: if Ollama/the index is
   unavailable it exits non-zero with `[]` and you fall back to the lexical Grep alone.
3. Merge, dedupe by `ref`, rank (semantic score + lexical hits), return refs.

---

## Worked example — the reminders ack (mirror of the Notion mapping's ack example)

The owner acknowledges a reminder (a chat "done", or a one-tap `ack: true` flip on mobile). Reconcile the
`reminders/` file. Given `ref = reminders/water-the-plants.md` with frontmatter (canonical order):

```yaml
status: reminded
...
last-reminded: 2026-07-14
last-acknowledged: 2026-07-11
consecutive-misses: 2
reminded-today: true
ack: true
```

Apply the ack per `../../references/reminders-policy.md` — this is **`store-update <ref>`** resolved to
adjacent-line frontmatter `Edit`s (no rename, body untouched):

1. **Edit** `status: reminded` → `status: done` (`done` = done *for today*; the row stays active and re-fires
   next cycle — write `finished` only on an explicit "I'm finished").
2. **Edit** the adjacent pair
   `last-acknowledged: 2026-07-11\nconsecutive-misses: 2` →
   `last-acknowledged: 2026-07-14\nconsecutive-misses: 0` — one multi-line Edit, because canonical order makes
   `last-acknowledged` and `consecutive-misses` neighbors.
3. **Edit** `ack: true` → `ack: false` — **consume and reset** the one-tap affordance so it can't
   auto-complete the next cycle. (`reminded-today` stays `true` — it *was* reminded today.)

Result:

```yaml
status: done
...
last-acknowledged: 2026-07-14
consecutive-misses: 0
reminded-today: true
ack: false
```

`last-acknowledged: <today>` is now the durable proof-of-done the Wrap reads. A skip instead of a done →
`status: skipped`, `last-acknowledged: <today>`, misses **unchanged** (an honest skip isn't a miss). The
daemon-side nudge cancellation (dequeue / ack ledger) is unchanged and lives in `reminders-policy.md`; it is
orthogonal to which store backend holds the row.

---

## Hazards

- **Sync-conflict files.** Dropbox / iCloud / Syncthing drop copies like `water-the-plants (conflict
  2026-07-14).md` or `…sync-conflict….md`. **Exclude `*conflict*` and `*sync-conflict*` from every Glob and
  Grep** so a stale duplicate never masquerades as the record. If two live copies disagree, the newer
  `last-acknowledged` / mtime wins; surface the conflict rather than guessing silently.
- **Concurrent external edits.** If a file is edited by another process between your Read and your Edit, the
  `Edit` `old_string` no longer matches and **the Edit fails loudly** — that is the **correct** failure
  (last-writer-win avoided). Re-Read and retry; never force a blind overwrite.
- **Frontmatter parse-miss.** If an anchored `Grep` doesn't find an expected field (hand-edited file, missing
  key, reordered fields), **fall back to a whole-file `Read`**, parse leniently, and — as an **act-low**
  write — repair the drift back to canonical order/spelling so the next query is fast. Never treat a parse
  miss as "record absent".
- **Retention.** `runs/` files older than **90 days** are pruned by Dream. Don't rely on old run files as a
  durable store for anything but run history.
- **Never rename to track a title change** (schema.md): a rename breaks inbound relation paths and every
  cached `ref`. Edit `title:`, leave the filename.
