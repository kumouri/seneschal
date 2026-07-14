# Salience access instrumentation — setup & reading the data

Phase 1 of the **what's-safe-to-forget** experiment (`../references/salience.md`): disposability
**tagging** at ingest + a passive **access counter** at query time, both inside the existing local RAG
index. **Observe-only** — zero behavior change, nothing hidden, nothing deleted. It mirrors the shadow
Router: run the mechanism, log the evidence, let the owner gate any action on it much later.

## Setup

**None.** It piggybacks on the RAG index (`RAG_SETUP.md`):

- The schema migrates itself — `rag_common.connect()` adds the `disposable` / `salience_cat` /
  `predicted_at` columns and the `salience_access` ledger to `state/rag-index.sqlite` in place,
  idempotently, the first time any script touches the index. No rebuild needed.
- Tagging arrives via Dream's nightly ingest JSONL (optional `disposable` / `salience_cat` keys —
  rubric in `../references/salience.md`). Untagged records behave exactly as before.
- Counting happens inside `rag_query.py` on every semantic recall. No Ollama beyond what RAG already
  uses; if the semantic layer is down, there's simply nothing to count.

## The knobs

| Knob | Default | Meaning |
|------|---------|---------|
| `--access-floor` (`rag_query.py`) | `0.55` | min cosine for a hit to count as a "touch" — separates a real recall from a near-miss. `max_score` is logged per chunk so the right floor can be re-derived post-hoc. |
| `--no-record` (`rag_query.py`) | off | skip counter writes for this query. **Required for analysis / debug reads** so they don't pollute the experiment. |

## What gets written where

- **`chunks.disposable`** — `0` normal · `1` predicted-disposable (fully retrievable; the prediction
  being scored) · `2` approved-forgotten (gated soft prune; excluded from answers, still counted).
  Ladder details: `../references/salience.md`.
- **`chunks.salience_cat` / `predicted_at`** — the taxonomy tag + when the prediction was made.
- **`salience_access`** — one aggregated row per touched chunk:
  `chunk_id, doc_id, salience_cat, hit_count, first_hit_at, last_hit_at, max_score`.
  An upsert counter (bounded), not a per-touch log. Counters key on deterministic chunk ids
  (`doc:ref#idx`), so they survive re-embeds and even `--rebuild` (the ledger is measurement, not
  cache — rebuilds deliberately preserve it).

## Reading the data

```sh
python rag_index.py --stats            # includes a salience line once anything is tagged/touched
python salience_rollup.py              # the weekly report (Dream runs this): per-category buckets
python salience_rollup.py --json       # machine shape; --propose adds gated draft text
```

```sh
# per-category demand (the phase-3 comparison, by hand):
sqlite3 ../state/rag-index.sqlite "
  SELECT c.salience_cat, c.disposable, COUNT(*) AS chunks,
         COALESCE(SUM(a.hit_count),0) AS hits, ROUND(COALESCE(MAX(a.max_score),0),3) AS best
  FROM chunks c LEFT JOIN salience_access a ON a.chunk_id = c.id
  GROUP BY c.salience_cat, c.disposable ORDER BY hits DESC;"
```

A disposable-tagged category accruing real hits = **prediction refuted** (keep more of that kind);
flat-zero over a long window = **prediction confirmed** (candidate for a *gated* prune proposal). The
honest caveat: a hit means *returned above the floor*, not *used in the answer* — treat counts as
relative between categories, never absolute truth.

## Cold start

Fresh index → zero tags, zero counters, and that's the expected state; accumulation is the phase. The
Phase-3 rollup abstains below **≥30 days / ≥50 tagged entries**. Missing tables self-create on next
`connect()`; a broken ledger write can never fail a query (it logs to stderr and the recall proceeds).
