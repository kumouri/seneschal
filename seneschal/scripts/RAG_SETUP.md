# Local semantic RAG index — setup (Advisor Chain, phase B)

The assistant's **Retrieval / Context advisor** (order 20) is a modular RAG pipeline: *query-transform →
retrieve → rerank + compress → augment* (`../references/advisor-chain.md`). **Phase A** retrieves from
baked-in references + Notion-search. **Phase B** (this) adds a **local semantic index** over the
assistant's own prose history — journal, notes, run-log — as one more retriever *behind the same
pipeline*, giving true semantic recall ("what did I decide about the database migration a month ago")
without leaning on keyword luck.

It's **free-but-local**: embeddings come from a local **Ollama** server, the index is stdlib **sqlite3**,
and everything is Python standard library — no `pip install`. It's also **additive**: if Ollama is down or
the index is empty, the retriever falls back to phase-A Notion-search. The semantic layer only ever *adds*.

## One-time setup

1. **Ollama.** Pull the embed model once:
   ```
   ollama pull nomic-embed-text
   ```
2. **Config is optional.** Defaults (localhost:11434, `nomic-embed-text`, 800-char chunks) are baked into
   `rag_common.py`. To override, copy the example and edit:
   ```
   cp seneschal/scripts/rag.env.example seneschal/scripts/rag.env
   ```
   `rag.env` is gitignored.
3. **Build the initial index** from the local prose:
   ```
   python seneschal/scripts/rag_index.py --local
   ```
   The sqlite index lands at `seneschal/state/rag-index.sqlite` (gitignored, local-first).

## The two scripts

| Script | Role |
|--------|------|
| `rag_index.py` | Build/update the index. `--local` indexes run-log/carry-over/context-digest; `--ingest F.jsonl` indexes records `{"source","ref","text"}` (how journal/notes get in). Incremental by doc hash; `--rebuild` starts clean; `--stats` reports. |
| `rag_query.py` | `rag_query.py "<query>" [--k N] [--source journal]` → top-k chunks as JSON on stdout. Exit **3** + `[]` when the embedder/index is unavailable (caller falls back). Every query also bumps the observe-only **salience access counters** (`--no-record` for analysis/debug reads) — see `SALIENCE_SETUP.md`. |

## How the corpus stays fresh — nightly in Dream

`rag_index.py` is deliberately **Notion-unaware** (pure, testable). The local prose it reads itself; the
Notion-resident corpus (journal, notes) is fetched by a run that *has* the Notion MCP — **Dream's nightly
refresh**:

1. Dream determines journal/notes entries new-or-changed since the last index.
2. It fetches their text via the Notion MCP and writes a JSONL
   (`{"source":"journal","ref":"<date/page>","text":"..."}` per line).
3. It runs `python seneschal/scripts/rag_index.py --local --ingest <that.jsonl>` (act-low — local only).

Because indexing is incremental, unchanged docs are skipped, so the nightly pass is cheap. A full
`--rebuild --local` (plus a fresh ingest) rebuilds from scratch if the index is ever lost — it's a
regenerable cache, never a system of record.

## How Retrieval uses it

Inside the Retrieval advisor's *retrieve* step, when the turn needs recall over the assistant's history,
it calls `rag_query.py` for semantic hits and blends them with the structured Notion/calendar reads, then
rerank+compress trims to a budget-bounded context. Exit 3 → treat as "no semantic layer this turn" and
proceed on Notion-search alone.
