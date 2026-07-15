#!/usr/bin/env python3
"""Query the assistant's local semantic RAG index (Advisor Chain, phase B).

Embeds the query with the local Ollama embedder and returns the top-k most
similar chunks (brute-force cosine over the sqlite index). This is the retriever
the Retrieval/Context advisor calls for semantic recall over the assistant's history
(journal / notes / run-log) — one source *behind* the phase-A pipeline.

**Additive / graceful.** If Ollama is down or the index is empty/missing, it
prints ``[]`` and exits non-zero so the caller falls back to Notion-search — the
semantic layer only ever *adds* recall, it is never a hard dependency.

**Salience access instrumentation (observe-only).** Every search also bumps the
``salience_access`` ledger for hits scoring ≥ the access floor — the passive
"retrieval-touch" counter behind the what's-safe-to-forget experiment
(``SALIENCE_SETUP.md`` / ``../references/salience.md``). The counter means
*returned-above-floor*, a relative comparator, not proof of use. Recording never
affects results and can never fail a query; ``--no-record`` keeps debug/analysis
reads out of the data. Predicted-disposable rows (``disposable=1``) are counted
AND returned like any other memory; only **approved-forgotten** rows
(``disposable>=2``, the gated soft prune — none exist until the owner approves one)
are excluded from answers, while still counted as un-forget evidence.

Output: a JSON array on stdout, best first::

    [{"ref": "2026-07-05", "source": "journal", "score": 0.82, "text": "...",
      "disposable": 0, "salience_cat": null}]

Examples
--------
    python rag_query.py "what did I decide about the Clover migration"
    python rag_query.py "therapy homework" --k 3 --source journal
    python rag_query.py "…" --no-record          # analysis read; don't touch counters
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import rag_common as rc

# exit codes: 0 = hits (or a valid empty result), 3 = semantic layer unavailable
EXIT_OK = 0
EXIT_UNAVAILABLE = 3

# A hit must score at least this to count as a salience "touch" — separates a real recall
# from a weak near-miss. A knob, not a truth: max_score is logged so the right floor can be
# re-derived post-hoc without re-running (see ../references/salience.md).
ACCESS_FLOOR = 0.55


def _record_access(conn, hits, now=None):
    """Upsert the ``salience_access`` ledger for the given hits (aggregated counter rows).

    Caller wraps this in try/except — a ledger write must NEVER fail a recall.
    """
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    for h in hits:
        conn.execute(
            """
            INSERT INTO salience_access
                (chunk_id, doc_id, salience_cat, hit_count, first_hit_at, last_hit_at, max_score)
            VALUES (?,?,?,1,?,?,?)
            ON CONFLICT(chunk_id) DO UPDATE SET
                hit_count    = hit_count + 1,
                last_hit_at  = excluded.last_hit_at,
                max_score    = MAX(max_score, excluded.max_score),
                salience_cat = excluded.salience_cat
            """,
            (h["chunk_id"], h["doc_id"], h["salience_cat"], now, now, h["score"]),
        )
    conn.commit()


def search(conn, query_vec, *, k=5, source=None, min_score=0.0,
           record_access=True, access_floor=ACCESS_FLOOR):
    dim = len(query_vec)
    sql = ("SELECT id, doc_id, source, ref, text, dim, vec, disposable, salience_cat "
           "FROM chunks")
    params = ()
    if source:
        sql += " WHERE source = ?"
        params = (source,)
    scored = []
    for cid, doc_id, src, ref, text, cdim, blob, disposable, cat in conn.execute(sql, params):
        if cdim != dim:  # embedded with a different model; skip rather than crash
            continue
        score = rc.cosine(query_vec, rc.unpack_vec(blob, cdim))
        if score >= min_score:
            scored.append({
                "chunk_id": cid, "doc_id": doc_id, "source": src, "ref": ref, "text": text,
                "score": score, "disposable": disposable or 0, "salience_cat": cat,
            })
    scored.sort(key=lambda h: h["score"], reverse=True)

    # Answers behave as if an approved-forgotten (disposable>=2) row were deleted: it never
    # occupies an answer slot (the next-best real memory backfills). Predicted-disposable
    # (=1) rows are REAL memories — they stay in answers; the tag is a logged prediction.
    answer = [h for h in scored if h["disposable"] < 2][:k]
    # …but demand for soft-pruned rows is still measured (the "un-forget" signal): any >=2
    # row that would have ranked top-k is counted alongside the answers actually returned.
    shadow = [h for h in scored[:k] if h["disposable"] >= 2]
    if record_access:
        touched = [h for h in answer + shadow if h["score"] >= access_floor]
        if touched:
            try:
                _record_access(conn, touched)
            except Exception as exc:  # a counter write must never fail a recall
                print(f"salience access-recording skipped: {exc}", file=sys.stderr)

    return [
        {"ref": h["ref"], "source": h["source"], "score": round(h["score"], 4),
         "text": h["text"], "disposable": h["disposable"], "salience_cat": h["salience_cat"]}
        for h in answer
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Semantic query over the assistant's local RAG index.")
    ap.add_argument("query", help="the natural-language query")
    ap.add_argument("--k", type=int, default=5, help="number of chunks to return")
    ap.add_argument("--source", help="restrict to one source (journal/notes/run-log/...)")
    ap.add_argument("--min-score", type=float, default=0.0, help="drop hits below this cosine")
    ap.add_argument("--no-record", action="store_true",
                    help="don't bump salience access counters (debug / analysis reads)")
    ap.add_argument("--access-floor", type=float, default=ACCESS_FLOOR,
                    help="min cosine for a hit to count as a salience 'touch'")
    ap.add_argument("--db", default=str(rc.DEFAULT_DB))
    args = ap.parse_args(argv)

    conn = rc.connect(args.db)
    if conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0:
        print("[]")
        print("rag index is empty — falling back to Notion-search", file=sys.stderr)
        return EXIT_UNAVAILABLE

    try:
        query_vec = rc.embed_texts([args.query], cfg=rc.load_env())[0]
    except rc.OllamaError as exc:
        print("[]")
        print(f"embedder unavailable — falling back to Notion-search: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    hits = search(conn, query_vec, k=args.k, source=args.source, min_score=args.min_score,
                  record_access=not args.no_record, access_floor=args.access_floor)
    print(json.dumps(hits, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
