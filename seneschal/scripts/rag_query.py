#!/usr/bin/env python3
"""Query the assistant's local semantic RAG index (Advisor Chain, phase B).

Embeds the query with the local Ollama embedder and returns the top-k most
similar chunks (brute-force cosine over the sqlite index). This is the retriever
the Retrieval/Context advisor calls for semantic recall over the assistant's history
(journal / notes / run-log / chat) — one source *behind* the phase-A pipeline.

**Additive / graceful.** If Ollama is down or the index is empty/missing, it
prints ``[]`` and exits non-zero so the caller falls back to Notion-search — the
semantic layer only ever *adds* recall, it is never a hard dependency.

**Bitemporal currency — "what is true now", not "what is similar".** Docs carry a
validity interval (``valid_from`` / ``valid_to`` / ``supersedes`` — see
``rag_index.py``), and by default a query returns **current facts only**: any row
whose doc has a real ``valid_to`` ≤ now is excluded (the next-best current memory
backfills). A superseded fact returned by default is exactly the coherence bug
this kills — *found three versions, used the stale one*. Two escape hatches:

* ``--as-of <ISO ts>`` — time travel: current means ``valid_from ≤ as-of <
  valid_to-or-open``.
* ``--include-superseded`` — non-current rows come back too, each clearly flagged
  (``"current": false`` + its ``valid_to`` and, when known, ``superseded_by``).

Superseded rows are never deleted or hidden-forever — flagged, always
retrievable, the same posture as salience's ``disposable=1``. Currency and the
``disposable`` ladder are independent axes.

**Hybrid retrieval — dense for content, lexical for names.** The dense arm is bad
at rare proper nouns: it places them near *the token*, not near the events
attached to them, so a bare name can rank dozens of places down — below the access
floor — in a corpus that contains it, while a content query about the same story
ranks it first. So an FTS5 index over the same chunks runs beside the vectors and
the two rankings are fused by reciprocal rank (never by score — cosine and bm25
are incomparable units).

**The arm only fires on tokens that are rare in this corpus** (``rag_common.
fts_rare_terms``). Matching common ones made queries measurably worse, not better:
function words saturate a conversational index, and ORing them in promotes weak
chunks over better dense hits on the very "who is X?" query the arm exists for.
The cut is derived from the corpus rather than written by hand, so it needs no
maintenance and is right about *this* index rather than about English.

**Known limit, stated rather than hidden:** when a query contains exactly one rare
term, the arm promotes hard, and whether that is right depends on whether the term
is the query's *subject* — which document frequency alone cannot tell. Judging that
needs a labelled relevance set, which does not exist here yet.

It is strictly additive: an index without ``chunks_fts``, a SQLite without FTS5, or
a query whose tokens are all common degrade silently to the dense-only behaviour.
``--no-lexical`` asks for that on purpose.

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

(with ``--include-superseded`` every hit also carries ``"current"``, and
non-current hits add ``"valid_to"`` + ``"superseded_by"``; a hit the lexical arm
produced carries ``"lexical": true``.)

Examples
--------
    python rag_query.py "what did I decide about the database migration"
    python rag_query.py "weekly review" --k 3 --source journal
    python rag_query.py "…" --no-record          # analysis read; don't touch counters
    python rag_query.py "who is the supplier" --as-of 2026-03-01T00:00:00+00:00
    python rag_query.py "supplier history" --include-superseded
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

# Reciprocal-rank-fusion constant. Fusion is by RANK, never by score, because the two arms
# return incomparable units — cosine is 0..1 and bm25 is an unbounded negative log-odds — so
# any weighted sum of them would be a number with no meaning that happened to sort. RRF needs
# no tuning and no score normalisation: each arm contributes 1/(K + rank). K=60 is the value
# from the original Cormack et al. paper and is deliberately left alone; a knob here would be a
# knob nothing could tune, since there is no labelled relevance set to tune it against.
RRF_K = 60


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


def _superseded_by(conn, source, ref):
    """Best-effort: the ref of the doc that superseded this one (None when unknown).

    Fail-open — any lookup trouble just means "unknown", never a failed recall.
    """
    try:
        row = conn.execute(
            "SELECT ref FROM docs WHERE source = ? AND supersedes = ? "
            "ORDER BY valid_from LIMIT 1",
            (source, ref),
        ).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def search(conn, query_vec, *, k=5, source=None, min_score=0.0,
           record_access=True, access_floor=ACCESS_FLOOR,
           as_of=None, include_superseded=False, query_text=None):
    """Top-k hybrid search. ``as_of`` is an aware datetime (None = "true now");
    ``include_superseded=True`` keeps non-current rows in the ranking, flagged.

    **``query_text`` turns on the lexical arm.** Passing it fuses an exact-token FTS5 ranking
    with the dense one; omitting it leaves this function byte-for-byte the dense-only search,
    which is why every caller that predates the arm is untouched.

    **Re-ranks; never re-admits.** The dense pass below already scores *every* chunk, so a lexical
    hit is by construction already a candidate — unless it was excluded as superseded, below
    ``min_score``, or embedded at a different dimension. Fusion therefore only reorders what
    survived those filters, and the lexical arm can never smuggle a superseded fact back into a
    default answer. That is load-bearing: the bitemporal guarantee is not the retriever's to relax.
    """
    dim = len(query_vec)
    sql = ("SELECT c.id, c.doc_id, c.source, c.ref, c.text, c.dim, c.vec, "
           "c.disposable, c.salience_cat, d.valid_from, d.valid_to "
           "FROM chunks c LEFT JOIN docs d ON d.id = c.doc_id")
    params = ()
    if source:
        sql += " WHERE c.source = ?"
        params = (source,)
    scored = []
    for (cid, doc_id, src, ref, text, cdim, blob, disposable, cat,
         valid_from, valid_to) in conn.execute(sql, params):
        if cdim != dim:  # embedded with a different model; skip rather than crash
            continue
        # Bitemporal currency: a superseded fact never occupies a default answer slot
        # (the next-best CURRENT memory backfills) — returning it is the stale-version
        # coherence bug. An orphan chunk with no doc row fails open to current.
        current = rc.doc_is_current(valid_from, valid_to, as_of)
        if not current and not include_superseded:
            continue
        score = rc.cosine(query_vec, rc.unpack_vec(blob, cdim))
        if score >= min_score:
            scored.append({
                "chunk_id": cid, "doc_id": doc_id, "source": src, "ref": ref, "text": text,
                "score": score, "disposable": disposable or 0, "salience_cat": cat,
                "current": current, "valid_to": valid_to,
            })
    scored.sort(key=lambda h: h["score"], reverse=True)

    # ---- the lexical arm -----------------------------------------------------------------
    # Ask FTS5 for more candidates than we will return: fusion can only reorder rows it can
    # see, and the case this exists for is one the dense side ranked far down.
    lex_rank = {}
    if query_text:
        for rank, cid in enumerate(
                rc.fts_search(conn, query_text, limit=max(4 * k, 20), source=source)):
            lex_rank.setdefault(cid, rank)
    if lex_rank:
        dense_rank = {h["chunk_id"]: i for i, h in enumerate(scored)}
        for h in scored:
            rrf = 1.0 / (RRF_K + dense_rank[h["chunk_id"]])
            if h["chunk_id"] in lex_rank:
                rrf += 1.0 / (RRF_K + lex_rank[h["chunk_id"]])
                h["lexical"] = True
            h["_rrf"] = rrf
        # Ties break on cosine, so a fused ordering is still deterministic across runs.
        scored.sort(key=lambda h: (h["_rrf"], h["score"]), reverse=True)

    # Answers behave as if an approved-forgotten (disposable>=2) row were deleted: it never
    # occupies an answer slot (the next-best real memory backfills). Predicted-disposable
    # (=1) rows are REAL memories — they stay in answers; the tag is a logged prediction.
    answer = [h for h in scored if h["disposable"] < 2][:k]
    # …but demand for soft-pruned rows is still measured (the "un-forget" signal): any >=2
    # row that would have ranked top-k is counted alongside the answers actually returned.
    shadow = [h for h in scored[:k] if h["disposable"] >= 2]
    if record_access:
        # A lexical hit counts as a recall even below the cosine floor. The floor asks "did the
        # dense arm strongly recall this"; an exact rare-token match is an at-least-as-strong
        # signal that answers a different question. Keeping the cosine-only test would
        # under-count every recall the lexical arm exists to enable, and salience is
        # measurement, so a systematic undercount there is a wrong answer about what is safe to
        # forget, not merely a missing row.
        touched = [h for h in answer + shadow
                   if h["score"] >= access_floor or h.get("lexical")]
        if touched:
            try:
                _record_access(conn, touched)
            except Exception as exc:  # a counter write must never fail a recall
                print(f"salience access-recording skipped: {exc}", file=sys.stderr)

    out = []
    for h in answer:
        item = {"ref": h["ref"], "source": h["source"], "score": round(h["score"], 4),
                "text": h["text"], "disposable": h["disposable"],
                "salience_cat": h["salience_cat"]}
        if h.get("lexical"):
            # Only ever ADDED, and only when the lexical arm produced this hit — so a caller
            # that never passes query_text sees the exact output shape it always did.
            item["lexical"] = True
        if include_superseded:
            # Flag currency explicitly so a superseded fact can never masquerade as the
            # live one; non-current rows also say when they closed and (when known) what
            # replaced them. Default-mode output keeps its historical shape — every row
            # there is current by construction.
            item["current"] = h["current"]
            if not h["current"]:
                item["valid_to"] = h["valid_to"]
                item["superseded_by"] = _superseded_by(conn, h["source"], h["ref"])
        out.append(item)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Semantic query over the assistant's local RAG index.")
    ap.add_argument("query", help="the natural-language query")
    ap.add_argument("--k", type=int, default=5, help="number of chunks to return")
    ap.add_argument("--source", help="restrict to one source (journal/notes/run-log/chat/...)")
    ap.add_argument("--min-score", type=float, default=0.0, help="drop hits below this cosine")
    ap.add_argument("--no-record", action="store_true",
                    help="don't bump salience access counters (debug / analysis reads)")
    ap.add_argument("--access-floor", type=float, default=ACCESS_FLOOR,
                    help="min cosine for a hit to count as a salience 'touch'")
    ap.add_argument("--as-of", metavar="ISO_TS",
                    help="time travel: return what was current at this instant "
                         "(valid_from <= as-of < valid_to-or-open); default is now")
    ap.add_argument("--include-superseded", action="store_true",
                    help="also return superseded (non-current) facts, each flagged with "
                         "current=false, its valid_to, and what superseded it")
    ap.add_argument("--no-lexical", action="store_true",
                    help="dense arm only — skip the FTS5 lexical arm")
    ap.add_argument("--db", default=str(rc.DEFAULT_DB))
    args = ap.parse_args(argv)

    as_of = None
    if args.as_of:
        as_of = rc.parse_ts(args.as_of)
        if as_of is None:
            # A typed CLI arg the caller got wrong should fail loudly (exit 2), never
            # silently answer for the wrong instant.
            ap.error(f"--as-of {args.as_of!r} is not an ISO-8601 timestamp")

    conn = rc.connect(args.db)
    try:  # close on every path — a lingering handle blocks file cleanup on Windows
        if conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0:
            print("[]")
            print("rag index is empty — falling back to Notion-search", file=sys.stderr)
            return EXIT_UNAVAILABLE

        try:
            query_vec = rc.embed_texts([args.query], cfg=rc.load_env())[0]
        except rc.OllamaError as exc:
            print("[]")
            print(f"embedder unavailable — falling back to Notion-search: {exc}",
                  file=sys.stderr)
            return EXIT_UNAVAILABLE

        hits = search(conn, query_vec, k=args.k, source=args.source,
                      min_score=args.min_score, record_access=not args.no_record,
                      access_floor=args.access_floor,
                      as_of=as_of, include_superseded=args.include_superseded,
                      query_text=None if args.no_lexical else args.query)
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return EXIT_OK
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
