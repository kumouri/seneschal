# Notion rate limits — why reads get throttled, and how to stay under

**Two distinct 429s — don't conflate them.** There are *two* different throttles, and they call for
different responses:

1. **Classic burst limit** — Notion allows ~3 requests/second per integration (+ a workspace-wide
   ~1,000-per-5-min cap); a wide parallel read fan-out blows past it and returns a plain `rate_limited`
   `429` with a `Retry-After`. **A client-side pace/limiter genuinely helps this one.** (This is what the
   "Why only reads / shape not permission" section below is about.)
2. **`collection_router_upstream_429`** (the one that actually keeps hitting, verified in production) —
   the `rate_limit_reason` is literally `collection_router_upstream_429`. This is **Notion's data-source
   SQL query router being throttled *upstream* (their shared capacity)**, and it is **NOT proportional to
   the request rate.** Confirmed: it fired on **single queries with 30–70 s of quiet before them.**
   Pacing does not prevent it. It hits **`notion-query-data-sources` (the SQL path)** specifically;
   `notion-fetch`, `notion-query-database-view`, and **all writes (`notion-update-page`)** route around the
   collection router and stay clean even during a bout.

**Mitigations that actually work for #2** (since a limiter won't):
- **Avoid the SQL query path for hot operations.** The biggest win: **ack by cached page id.** Every ⏰ row
  id lives in the live cache table `../state/reminders-id-cache.md` (gitignored, runtime-updated; a
  tracked example ships alongside it), so a chat ack is a pure `notion-update-page` (no query). See
  `databases.md` → ⏰ section. This is why, during one observed bout, the ack *writes* landed while the
  *lookups* 429'd.
- **Prefer `notion-fetch` / saved views over `notion-query-data-sources`** whenever an id or a view will do.
- **Backoff with jitter, retry serially, respect `Retry-After`** on the rare SQL query you can't avoid.
- There is **no clean place to inject a token-bucket limiter**: Notion is reached through the *hosted*
  Notion MCP server (the assistant is the client) — unlike Proton/Telegram which go through our own
  Python. So "a limiter on our side" would mean a local proxy — heavy, and useless against #2 anyway.
  Disciplined call *shape* (below) + id-caching is the real lever.

---

**Symptom (throttle #1).** The assistant periodically gets rate-limited (HTTP 429) by Notion **on
reads** — `notion-fetch`, `notion-query-data-sources`, `notion-search` — while **writes**
(`notion-update-page`, `notion-create-*`) go through fine.

**Why only reads.** Notion's API allows an *average of ~3 requests/second* per integration, with a small
burst allowance; exceed it and you get a `429` with a `Retry-After`. Reads and writes share the same
bucket — the asymmetry is about **shape**, not permission:

- **Reads are fired as a burst.** The execution rules say to *batch aggressively — issue all independent
  reads for a phase in one parallel batch* (Brief/Wrap/Reminders each pull several databases + pages at
  once). A dozen `notion-*` reads dispatched simultaneously blows straight past 3 req/s → 429.
- **Writes are singular.** An ack or a task edit is almost always **one** `notion-update-page`. One call
  never bursts, so writes ~never 429. (If you ever batch many writes in parallel, they'd throttle too.)
- **Overlapping runs compound it.** The presence daemon can have the warm chat session + a comms-peek +
  a scheduled slot alive at once; each fires its own read burst. `presence.py` **serializes the headless
  peek/slot spawns to one at a time**, and additionally **defers launching a slot/peek while the warm chat
  session is actively processing a turn** (chat is priority and is never delayed — only the slot waits;
  it retries next loop, reusing the in-flight-headless deferral path). Together those remove the
  daemon-side overlap, but a single run can still self-throttle if it fans out too wide.

## Rules — keep reads cheap and unbursty

1. **Reuse the baked-in references before querying.** `references/databases.md` (IDs + schema) and
   `state/context-digest.md` (last night's Dream digest) answer most orientation questions with
   **zero** Notion calls. **Never fetch a DB schema at runtime** (the standing rule) — that's pure
   avoidable read load. The digest now also carries a **`## Brief pre-stage (Dream)`** block — the morning
   Brief's Notion inputs, snapshotted the night before — so the Brief reads that instead of re-firing its
   full 4-read batch (see mitigations below).
2. **Fewer calls — but mind *which path*.** For throttle #1, a single `notion-query-data-sources` with a
   filter beats N narrow calls — ask for what you need in as few calls as possible. **But** (throttle #2,
   above) `notion-query-data-sources` is the very path the upstream `collection_router_upstream_429` hits,
   while `notion-fetch` / saved views route *around* it — so **when you already hold the page-ids, prefer
   capped `notion-fetch` by id over a broad query.** **Concrete case:** Reminders Phase 1 has the linked
   Task/Goal ids from the ⏰ query's relation columns → read their statuses with capped `notion-fetch` by
   id (around #2), **not** a broad `query-data-sources` (see mitigations).
3. **Cap concurrent reads to a handful (~3).** Still batch for latency, but don't dispatch a whole
   phase's reads in one giant simultaneous fan-out — chunk large batches so you stay near Notion's
   ~3 req/s. This refines rule #3 in `SKILL.md` ("Batch aggressively"): aggressive ≠ unbounded.
4. **On a 429, back off — don't hammer.** Respect `Retry-After` (or wait a couple of seconds), then
   retry **serially**, not by re-firing the whole parallel batch. A retry-storm deepens the throttle.
5. **Only re-read to confirm a write when it matters** (e.g. an ack you're about to claim landed). Don't
   read-back reflexively.

Writes are unaffected by all of this — keep persisting acks/edits immediately (act-low). The goal is
just to stop the *read* side from stampeding.

## Applied mitigations (Tranche 1)

Three confirmed burst sources, each fixed against a rule above:

1. **Reminders linked-status reads → capped `notion-fetch` by id** (routes around throttle #2). The
   Reminders slots run 4×/day and must read each linked Today-Todo / Deadline-Watch's real `Status`.
   Phase 1 collects the linked page-ids from the ⏰ query's `Related Task` / `Related Goal` relation
   columns and reads them with **`notion-fetch` by id, capped to ~3 concurrent** — `notion-fetch` routes
   around the `collection_router_upstream_429` upstream throttle (which hits `notion-query-data-sources`),
   and the cap keeps it under the classic ~3 req/s burst too. Zero reads when a slot has no linked items.
   See `subagents/reminders/SKILL.md` Phase 1. *(Correction: the first cut of this fix used "one broad
   `query-data-sources` per DB" — fewer calls, but on the exact SQL path throttle #2 hits; fetch-by-id is
   the right lever for the throttle we actually see.)*
2. **Dream pre-stages the Brief's Phase-1 inputs → the digest** (rule #1). The morning Brief fired a full
   4-read Notion batch (Tasks due/overdue, Active/Carrying-Over Flags, In-Progress Projects, + carry-over)
   every morning. Now **Dream** (nightly, ~22:00) snapshots the three DB reads (Tasks due/overdue **today +
   tomorrow**, Flags, Projects) into the timestamped **`## Brief pre-stage (Dream)`** block of
   `state/context-digest.md`; the **Brief reads that block first** and issues only **delta** live-queries
   (Tasks completed/created since the snapshot stamp; Flags/Projects changed since) instead of the whole
   fan-out. **Staleness caveat (explicit):** the snapshot predates the overnight hours, so the light
   morning delta check is still required — the block is a warm base, not the last word; if it's missing or
   its stamp isn't last night, the Brief falls back to the full batch. Calendar + journal carry-over stay
   live. See `seneschal/SKILL.md` (Dream step 1b, Brief Phase 1), `references/briefing.md`,
   `subagents/morning-briefing/SKILL.md`.
3. **presence.py gates slot/peek launches on warm-session activity.** Beyond serializing the headless
   peek/slot spawns to one at a time, the daemon now also **defers launching a slot or peek while the warm
   Telegram chat session is actively processing a turn**, so a chat turn's Notion reads and a scheduled
   slot's read burst can't overlap. **Chat is priority and is never delayed** — only the slot waits, and it
   simply retries on the next loop (reusing the existing in-flight-headless deferral path). See
   `scripts/presence.py` (`warm_session_busy()` + the `maybe_peek` / `maybe_run_slots` gates).
