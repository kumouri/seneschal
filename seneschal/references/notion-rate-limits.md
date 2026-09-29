# Notion rate limits — moved

**This content now lives in [`../store/notion/mapping.md`](../store/notion/mapping.md) → "Throughput —
the two 429s".** The Notion backend's throughput rules (the two distinct 429s — the classic ~3 req/s
burst limit and the `collection_router_upstream_429` upstream query throttle — plus the mitigations:
fetch-by-id / saved views over the SQL query path, ack-by-cached-id, backoff-with-jitter, and the
morning Brief reading Dream's `state/brief-prestage.json` snapshot instead of re-firing its full read
batch) are part of the store's Notion mapping now, alongside the verb→tool resolution they belong with.
(`state/context-digest.md` is retired — its old "Brief pre-stage" block is `state/brief-prestage.json`.)

This stub is kept because existing references point here; update links to `../store/notion/mapping.md`
when you touch them. Rate limits are a **backend concern** — only the Notion backend throttles; the
filesystem backends (obsidian/markdown) have no such limit.
