# obsidian/mapping.md — the six store verbs on the Obsidian backend

HOW each backend-neutral **store verb** (`../README.md`) becomes a concrete Claude Code
**Read / Write / Edit / Glob / Grep** call against the vault. No MCP, no plugin. Field names, canonical
option values, folder layout, and the `ref = vault-relative path` rule come from `schema.md` — this file is
only the execution plan.

**Setup.** `<vault>` = `backends.obsidian.vault_path`, `<sub>` = `backends.obsidian.subfolder` (default
`Seneschal`), from `store/config.json`. All paths below are `<vault>/<sub>/…`. Records are one note each;
frontmatter is the source of truth; the canonical field order in `schema.md` guarantees a domain's fields
sit in a known, adjacent arrangement inside every note.

**Throughput.** Batch independent reads in one message (parallel `Grep`/`Read`). Prefer *fewer, better*
reads: a single anchored `Grep -A <n>` over a domain folder pulls each survivor's whole frontmatter block in
one pass — only `Read` the notes whose **bodies** you actually need. Never Glob-then-Read the whole domain to
filter in your head.

---

## `store-query <domain> where <filter>` → anchored Grep over the domain folder

List records in a domain by a frontmatter filter.

1. **Grep an anchored frontmatter pattern** in the domain's folder. Anchor on the field line so you match the
   frontmatter value, not prose:
   - one value: `^status: done$`
   - a set: `^status: (pending|reminded|snoozed)$`
   - existence / date compare: `^due: 2026-07-14$`, or `^due: ` then compare in-parse.
   Use `-A <n>` sized to the domain's frontmatter block (e.g. `-A 18` for reminders, `-A 11` for tasks) so the
   **whole block** comes back in the same result. Restrict with `path` = `<vault>/<sub>/<Folder>/` and
   `glob` = `*.md`.
2. **Parse fields from the returned block.** Canonical field order (schema.md) guarantees every field is
   inside the `-A` window, so you read them straight out of the Grep output — no second pass to get a
   sibling field.
3. **`Read` only survivors that need bodies** (narrative, progress log, capture candidates). A count/id-only
   query needs no Read at all.
4. **Exclude sync-conflict files** (see Hazards) — add `--glob '!*conflict*'` semantics by filtering them out
   of results.

Multi-field filters: Grep the most selective field first, then confirm the rest from the same `-A` block
(one pass), or intersect two greps by path. The `ref` of each match is its path.

## `store-get <ref>` → Read the path

`Read` the note at `<ref>` (a vault-relative path, e.g. `Seneschal/Tasks/Ship the spec.md`). Parse
frontmatter for fields, body for narrative. That's the whole verb — the `ref` *is* the location, so there is
no id-cache lookup (this is what replaces Notion's id resolution).

## `store-create <domain>` → Write a new note from the template

1. Compute the **filename**: `<title>` with `\ / : * ? " < > | # ^ [ ]` stripped, trimmed ≤ 80 chars. If
   `<vault>/<sub>/<Folder>/<name>.md` exists, append ` (2)`, ` (3)`, … Date-keyed domains (journal, briefs,
   runs) use their date/mode filename instead (schema.md).
2. **Write** the note: frontmatter in the domain's **canonical field order** (schema.md), option values
   verbatim (emoji-free), relations as quoted wikilinks, unknowns left blank/omitted — then the body
   template.
3. The new note's vault-relative path is its `ref`; return it. **Never** write a note whose title collides by
   overwriting — the ` (2)` suffix keeps both.

## `store-update <ref>` → targeted Edit(s) on frontmatter lines

Set fields on an existing record.

1. **Read** the note (or reuse the block from a prior `store-query`) to get exact current lines.
2. **Edit** the frontmatter line(s). Because canonical order fixes which lines are adjacent, fields that
   change together are set by **one multi-line Edit**; unrelated fields get their own single-line Edit. Match
   the whole `key: value` line as `old_string` so the replace is unambiguous.
3. Frontmatter only — the body is untouched (that's `store-append`). If a value moves a record between
   states that also imply another field (e.g. task → `done` implies stamping `completed`), edit both, adjacent
   where the schema puts them.

## `store-append <ref>` → Edit appending to the body

Add body content (a progress note, a journal bullet, a run phase line) **without touching frontmatter**.
`Edit` by matching an end-of-body anchor (the last line, or a `## Progress log` heading) and replacing it with
itself-plus-the-new-content; or match the trailing content and extend it. For a **daily note**
(`Journal/YYYY-MM-DD.md`) append a new `- HH:MM …` bullet — creating the note first (`store-create` for the
journal domain) if today's doesn't exist yet.

## `store-search <query>` → Grep across the subfolder + the local RAG index

Fuzzy / semantic recall.

1. **Lexical:** `Grep` the query (content mode, case-insensitive) across `<vault>/<sub>/` — **only** widen to
   the whole vault when `backends.obsidian.search_whole_vault: true`. Exclude sync-conflict files.
2. **Semantic:** also call the local RAG index —
   `python seneschal/scripts/rag_query.py "<query>" --k <n>` — which returns best-first JSON
   (`ref`, `source`, `score`, `text`) over journal/notes/run-log. It's **additive**: if Ollama/the index is
   unavailable it exits non-zero with `[]` and you fall back to the lexical Grep alone.
3. Merge, dedupe by `ref`, rank (semantic score + lexical hits), return refs.

---

## Worked example — the reminders ack (mirror of the Notion mapping's ack example)

The owner acknowledges a reminder (a chat "done", or a one-tap `ack: true` flip on mobile). Reconcile the
`Reminders/` note. Given `ref = Seneschal/Reminders/Water the plants.md` with frontmatter (canonical order):

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

1. **Edit** `status: reminded` → `status: done` (`done` = done *for today*; the row stays active and
   re-fires next cycle — write `finished` only on an explicit "I'm finished").
2. **Edit** the adjacent pair
   `last-acknowledged: 2026-07-11\nconsecutive-misses: 2` →
   `last-acknowledged: 2026-07-14\nconsecutive-misses: 0` — one multi-line Edit, because canonical order
   makes `last-acknowledged` and `consecutive-misses` neighbors.
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

- **Sync-conflict files.** Obsidian Sync / Dropbox / iCloud drop copies like `Water the plants (conflict
  2026-07-14).md` or `…sync-conflict….md`. **Exclude `*conflict*` and `*sync-conflict*` from every Glob and
  Grep** so a stale duplicate never masquerades as the record. If two live copies disagree, the newer
  `last-acknowledged` / mtime wins; surface the conflict rather than guessing silently.
- **Concurrent external edits.** Obsidian auto-reloads a note changed on disk, so a `store-update` while the
  vault is open is normally safe. But if a note is edited **in the app** between your Read and your Edit, the
  `Edit` `old_string` no longer matches and **the Edit fails loudly** — that is the **correct** failure
  (last-writer-win avoided). Re-Read and retry; never force a blind overwrite.
- **Frontmatter parse-miss.** If an anchored `Grep` doesn't find an expected field (hand-edited note, missing
  key, reordered fields), **fall back to a whole-file `Read`**, parse leniently, and — as an **act-low**
  write — repair the drift back to canonical order/spelling so the next query is fast. Never treat a parse
  miss as "record absent".
- **Retention.** `Runs/` notes older than **90 days** are pruned by Dream. Don't rely on old run notes as a
  durable store for anything but run history.
- **Never rename to track a title change** (schema.md): a rename breaks inbound wikilinks and every cached
  `ref`. Edit `title:`, leave the filename.
