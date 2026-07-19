# Notion store — verb mapping

How the six backend-neutral **store verbs** execute against Notion, and the quirks that bite. Skill
prose speaks verbs + canonical, emoji-free option values; this file resolves each verb to a Notion MCP
tool and looks up the exact Notion strings in **`schema.template.md`** (rendered to the gitignored
`schema.md` at setup). **Never fetch a DB schema at runtime** — trust `schema.md`.

MCP tools are named `mcp__<server>__notion-<suffix>` where `<server>` is the connected Notion MCP's
hash; refer to them by suffix (`notion-fetch`, `notion-query-data-sources`, …). The live tool schema is
authoritative for exact payload shape; get **names and option strings from `schema.md`**, never by
fetching a DB schema mid-run.

## Verb → tool at a glance

| Store verb | Notion tool | One-line resolution |
|---|---|---|
| `store-query` | `notion-query-data-sources` | SQL over the data source; mind the `date:Prop:start` projection |
| `store-get` | `notion-fetch` | by page id / url / `collection://…`; routes around the SQL throttle |
| `store-create` | `notion-create-pages` | parent = `collection://…` for a DB row (or a page id for a standalone page) |
| `store-update` | `notion-update-page` | set properties by page id; may also append body blocks |
| `store-append` | `notion-update-page` | append body blocks (Notion-flavored Markdown) — don't overwrite |
| `store-search` | `notion-search` + local RAG index | semantic find across pages; RAG index for journal/notes/run-log |

---

## store-query → `notion-query-data-sources`

List records in a domain by filter. SQL-like over the data source's SQLite projection; column names are
**exactly** as in `schema.md`.

- **The date projection gotcha (verified).** A **date** property projects as **three** columns —
  `date:<Prop>:start`, `date:<Prop>:end` (null unless a range), `date:<Prop>:is_datetime` (0/1) — **not**
  a column named `<Prop>`. Querying the bare name errors with *no such column*. Filter/sort on
  `"date:<Prop>:start"` (ISO string, directly comparable). Watch the properties whose name has spaces or a
  slash: reminders `Due / Target` → `"date:Due / Target:start"`; `Last Acknowledged` →
  `"date:Last Acknowledged:start"`.
- **Other projection shapes (verified).** `status` / `select` / `title` use their **plain** names + the
  **exact option strings** (from `schema.md`, emoji included). A **multi-select** projects as a
  **JSON-array string** (e.g. `["🔧 Hobbies & Tech"]`) — match with `LIKE '%…%'` or parse client-side. A
  **relation** projects as a **JSON array of page URLs**. Every row also carries `id` and `url` — use
  `url` for citations, and `id` to feed `store-get`.
- **Worked filter (tasks due/overdue):**
  `SELECT "Task name","Status","date:Due:start" FROM "collection://00000000-0000-0000-0000-000000000001"
  WHERE "Status" NOT IN ('Done','Archived') AND "date:Due:start" IS NOT NULL AND "date:Due:start" <=
  '<today>' ORDER BY "date:Due:start"`. (A long-lived Tasks DB accumulates a large undated backlog — the
  date filter is what keeps a query to genuinely due/overdue items.)
- **Throttle warning — this is the hot path.** `notion-query-data-sources` is the **exact** tool the
  `collection_router_upstream_429` upstream throttle hits (see Throughput below). When you already hold the
  page ids (e.g. relation columns from a prior query), prefer capped `store-get` (`notion-fetch` by id)
  over a re-query — `notion-fetch` routes *around* that throttle. For hot, repeated lookups (reminder
  acks), skip the query entirely via the id cache.
- **Saved views around the throttle.** `notion-query-database-view` (a saved view) also routes around the
  collection router; use it where a fixed view answers the question ("last 5 run logs", "active flags")
  instead of an ad-hoc SQL query.

## store-get → `notion-fetch`

One record (or page) by ref. `ref` is a page **id**, a **url**, or a `collection://…` data-source id.
Use it to read the Interstitial Journal page, a prior Run Log entry, or a linked Task/Goal by id.
`notion-fetch` **routes around** `collection_router_upstream_429` and stays clean even during a bout — so
**when you hold ids, fetch don't query.** Cap concurrent fetches to ~3 (the classic burst limit still
applies). Zero calls when a caller has no ids to resolve.

## store-create → `notion-create-pages`

Create a record or a standalone page.

- **DB row:** set `parent` to the domain's `collection://…` data-source id (from `schema.md`); pass
  properties by **exact name**; put any body in **Notion-flavored Markdown**.
- **Standalone page:** set `parent` to a page id (e.g. the Achievements Log page `…0014`).
- **Property setters (the gotchas):**
  - **Dates:** pass ISO `YYYY-MM-DD` (or a full datetime). (That's the *write* shape; the three-column
    `date:Prop:*` split is only the *query* projection.)
  - **Relations:** an **array of target page urls/ids** — you must already hold the target url (search /
    fetch it first, or reuse a url you created this run). Most relations accept several; **Goal
    Measurements `Goal` is limit 1**.
  - **Status vs select:** pass the **exact option string incl. emoji** (`Done`, `🚨 Critical`, `🥉 Small`,
    `💼 Work`). Wrong/mis-cased strings fail. Canonical → Notion lookups live in `schema.md`.
  - **Multi-select:** array of exact option strings; pass a brand-new string to add an option (allowed for
    `Emotions`, `Themes`, `Tags`, `Relevance Tags`).
  - **Checkbox:** `true`/`false` (query projection shows `__YES__`/`__NO__`).
  - **Formulas are read-only** (e.g. Goals `Progress %` may be a formula): read, never write. If a query
    omits a computed value, derive the fallback deterministically.
  - **Don't set properties that don't exist** — notably the journal Agent Run Log (`…0016`) has **no Goals
    count field**; fold goal counts into `Actions Summary` text.

## store-update → `notion-update-page`

Set fields on an existing record by page id (and/or append body blocks). **Writes route around**
`collection_router_upstream_429` and one write never bursts the classic limit — so persist acks/edits
immediately (act-low). Same property-setter gotchas as `store-create`. **Prefer updating by a page id you
already hold** over querying to find the row first (the query is the throttled path).

## store-append → `notion-update-page` (body blocks)

Add body content to a page — the Run Log's incremental phase log, the Achievements Log page, or a journal
date toggle. Append **new blocks**; never overwrite existing content. Body is **Notion-flavored
Markdown**:

- **Timestamped journal entry:**
  `<mention-date start="<date>" startTime="HH:MM" timeZone="<owner's timezone>"/> text…`
- **Day header / date toggle:**
  `<details><summary><mention-date start="<date>"/></summary> …nested entries… </details>` — preserve the
  toggle nesting (the journal-presence signal depends on the top-level date-toggle shape).
- **Callout (carry-over block):** `<callout icon="📌"> … </callout>`.

## store-search → `notion-search` + local RAG index

Fuzzy / semantic find. `notion-search` finds pages by **meaning** (existing tasks/projects to link, prior
entries); pass `query_type:"user"` to resolve a Notion user. Layer the **local RAG index** (the
Retrieval-phase-B semantic index over journal / notes / run-log) for owner-memory recall that Notion
search alone misses; it falls back to `notion-search` when the index is cold. `notion-search` is a read —
keep it out of wide parallel bursts (Throughput #1).

---

## Throughput — the two 429s (consolidated)

**There are two distinct throttles; they need different responses.**

1. **Classic burst limit (~3 req/s per integration, + a workspace-wide ~1,000/5-min cap).** A wide
   parallel read fan-out blows past it → plain `rate_limited` 429 with a `Retry-After`. A client-side
   pace genuinely helps this one.
   - **Cap concurrent reads to ~3.** Batch for latency, but chunk large batches; aggressive ≠ unbounded.
   - **On 429, respect `Retry-After` (or wait a couple seconds), then retry serially** — never re-fire the
     whole parallel batch; a retry-storm deepens the throttle.
   - **Writes are singular** (one `notion-update-page`) so they ~never burst — keep persisting them.
2. **`collection_router_upstream_429`** (the one that keeps hitting; `rate_limit_reason` is literally
   that). Notion's data-source SQL **query router** throttled *upstream* — **not proportional to your
   request rate** (fired on single queries with 30–70 s of quiet before them). A limiter won't prevent it.
   It hits **`notion-query-data-sources`** specifically; **`notion-fetch`, saved views
   (`notion-query-database-view`), and all writes (`notion-update-page`) route around it** and stay clean
   during a bout.
   - **Avoid the SQL query path for hot operations.** Prefer `store-get` (fetch-by-id) / saved views over
     `store-query` whenever an id or a view will do.
   - **Ack by cached page id — skip the query** (below).

**Reuse the baked-in references first.** `schema.md` (ids + schema) and the Dream context digest answer
most orientation with **zero** Notion calls. Only re-read to confirm a write when it actually matters
(an ack you're about to claim landed) — don't read-back reflexively.

### Reminders id-cache — ack by cached id, skip the query

Every reminder row's stable **page id** lives in the runtime cache table
`../../state/reminders-id-cache.md` (gitignored; a tracked example ships as
`reminders-id-cache.example.md`). When the owner acks a reminder in chat, **match their phrase to that
table and write by id** — do **not** run a `store-query` (`notion-query-data-sources`) lookup first, which
is the exact path throttle #2 hits. Fall back to a single query only on a cache miss / stale id, then
refresh the cache. (During an observed bout, ack *writes* landed while *lookups* 429'd — this is why.)

---

## Worked example — the reminders ack

The owner says "took my meds." The reminder is already in the id cache.

**Store-verb form (what skill prose emits):**

```
store-update reminders <ref> { status: done, last_acknowledged: <today>, consecutive_misses: 0 }
```

**Resolution on Notion:**

1. **Resolve `<ref>` from the id cache**, not a query — match the phrase to
   `../../state/reminders-id-cache.md` → the row's Notion page id. (Cache miss only → one `store-query`
   fallback, then refresh the cache.)
2. **One `notion-update-page`** on that page id (a write → routes around `collection_router_upstream_429`,
   and one write never bursts the classic limit):
   - `Status` (**select**) = `Done` — the canonical `done` → Notion `Done` lookup from `schema.md`.
     *(Reminder `Status` strings are emoji-free; a field like `Importance` would resolve to an
     emoji-bearing string such as `🚨 Critical` — the translation table in `schema.md` is where that
     emoji lives.)*
   - `Last Acknowledged` (**date**) = today's ISO date in the owner's configured timezone. (This also
     serves as the durable "done that day" record the EOD Wrap counts.)
   - `Consecutive Misses` (**number**) = `0`.
   - If the `Ack` checkbox was ticked, also set it `false` (the one-shot input is consumed on ack so a
     stale tick can't auto-complete a later cycle).
3. **No read-back** — the single write is authoritative; don't re-query to confirm.

**Why by cached id:** the lookup query is the throttled path; the ack write is not. Writing directly by
the cached page id sidesteps throttle #2 entirely and keeps acks landing even during a query bout.

---

## Outbox — durable act-low writes

Because Notion is **remote and rate-limited**, a `store-update`/`store-create` issued by a volatile
session (chiefly a daemon-originated chat ack) can fail *after* the assistant has already said "done."
The **write-behind outbox** (`../../scripts/outbox.py` + `outbox_common.py`, journal
`../../state/notion-outbox.sqlite`) closes that gap: the write's *intent* is journaled locally first
(durable — survives a reboot), then flushed here idempotently. The skill layer still speaks
`store-update`; the outbox is HOW this backend makes that verb durable for daemon-originated acks. It is
**Notion-backend only** — enabled only when `store/config.json` says `active: "notion"`; the filesystem
backends (obsidian/markdown) write locally and atomically, so their acks take the direct path and never
touch it. Full design: `../../docs/notion-write-behind-outbox-spec.md`.

**Intent → tool.** Each journaled entry is a *logical* intent, translated to an MCP call at flush time —
never a pre-baked payload. An `ack_reminder` intent flushes as one **`notion-update-page`** on the
**cached** ⏰ page id (the worked example above, replayed verbatim: `Status` = the translated option
string, `Last Acknowledged` = the ack's local date, `Consecutive Misses` = 0, untick `Ack` — no lookup
query, no read-back). A `med_log` intent flushes as one **`notion-create-pages`** into its target
`collection://…`. The generic enqueued intents (`run_log_finalize`, `reminder_status`) flush as
`notion-update-page` by the row id they carry. Property names and option strings resolve through
`schema.md`, exactly like a direct write.

**Flush rules.** The drain is **single-consumer FIFO** (oldest first — per-target order for free) and
runs opportunistically inside LLM turns: `outbox.py pull --json` → replay each intent via the tool above
→ `outbox.py mark --done` (for a create, pass `--notion-page-id` so the landed row id is recorded in the
same local transaction). Every entry carries a **`UNIQUE` idempotency key** (`ack:<row>:<date>`,
`medlog:<intent-uuid>`, …), so a repeat enqueue is a no-op and a replayed update converges — flushing
twice is safe. A transient failure (429/5xx/network) is `mark --retry` — exponential backoff, honoring
`Retry-After`; a permanent one (404/400/403) or an exhausted attempt budget is `mark --dead-letter` —
the entry stops retrying but **stays in the table and is surfaced** (`outbox.py status` lists every
dead-letter; never silently dropped, never blocking the rest of the queue). The happy path is
belt-and-suspenders: the turn's direct write still fires, and the drainer finds the entry
already-satisfied.
