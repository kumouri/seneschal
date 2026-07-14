#!/usr/bin/env python3
"""Build / update the assistant's local semantic RAG index (Advisor Chain, phase B).

Embeds text records with a local Ollama embedder and upserts them into the
sqlite index (``state/rag-index.sqlite``). Incremental: a doc whose text hash
is unchanged since last run is skipped; a changed doc has its old chunks
replaced. See ``RAG_SETUP.md`` and ``rag_common.py``.

Corpus sources
--------------
* ``--local``    index the local prose the assistant owns on disk: ``run-log.md``,
                 ``carry-over.md``, ``context-digest.md`` (source = "run-log" etc.).
* ``--ingest F`` index records from a JSONL file, one object per line:
                 ``{"source": "journal", "ref": "2026-07-05", "text": "..."}``.
                 This is how Notion-resident corpus (journal, notes) gets in:
                 a Claude run that HAS the Notion MCP (Dream's nightly refresh)
                 fetches the new entries and writes the JSONL, then calls this.

Salience tags (optional, observe-only — the what's-safe-to-forget experiment)
------------------------------------------------------------------------------
Ingest records may carry two extra keys Dream writes per the rubric in
``../references/salience.md``::

    {"source": "journal", "ref": "2026-07-09", "text": "...",
     "disposable": 1, "salience_cat": "logistics.transient"}

``salience_cat`` is coerced to the closed taxonomy (unknown → "unknown");
``disposable: 1`` is a *logged prediction* ("I won't need this again") accepted
only on eligible categories — on any other it is cleared with a warning
(identity.core can never be predicted away). Tagged rows stay fully retrievable;
nothing here hides or deletes. Absent keys default to 0 / null, so existing
JSONLs are unaffected. See ``SALIENCE_SETUP.md``.

Examples
--------
    python rag_index.py --local
    python rag_index.py --ingest journal.jsonl notes.jsonl
    python rag_index.py --rebuild --local
    python rag_index.py --stats
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import rag_common as rc

LOCAL_SOURCES = {
    "run-log": rc.STATE / "run-log.md",
    "carry-over": rc.STATE / "carry-over.md",
    "context-digest": rc.STATE / "context-digest.md",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iter_local():
    for source, path in LOCAL_SOURCES.items():
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                yield {"source": source, "ref": path.name, "text": text}


def _iter_jsonl(paths):
    for p in paths:
        for lineno, line in enumerate(Path(p).read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"  ! {p}:{lineno} skipped (bad JSON: {exc})", file=sys.stderr)
                continue
            if not rec.get("text") or not rec.get("ref"):
                print(f"  ! {p}:{lineno} skipped (missing text/ref)", file=sys.stderr)
                continue
            yield {
                "source": rec.get("source", Path(p).stem),
                "ref": str(rec["ref"]),
                "text": str(rec["text"]),
                "disposable": rec.get("disposable", 0),
                "salience_cat": rec.get("salience_cat"),
            }


def _norm_salience(rec):
    """Normalize a record's optional salience tags against the closed vocabulary.

    Unknown category → "unknown" (never passed through raw); a ``disposable=1`` prediction on
    an ineligible category is cleared with a warning — ``identity.core`` (birthdays, the
    family-bereavement class) can never be predicted-disposable, even by a buggy tagging pass.
    See ``../references/salience.md``.
    """
    cat = rec.get("salience_cat")
    if cat is not None:
        cat = str(cat)
        if cat not in rc.SALIENCE_CATEGORIES:
            cat = "unknown"
    disposable = 1 if rec.get("disposable") in (1, True, "1") else 0
    if disposable and cat not in rc.DISPOSABLE_ELIGIBLE:
        print(
            f"  ! {rec.get('source')}:{rec.get('ref')} disposable=1 on ineligible category "
            f"{cat!r} — cleared (see references/salience.md)",
            file=sys.stderr,
        )
        disposable = 0
    return disposable, cat


def index_records(conn, records, cfg, *, dry_run=False):
    added = updated = skipped = 0
    for rec in records:
        source, ref, text = rec["source"], rec["ref"], rec["text"].strip()
        if not text:
            continue
        doc_id = f"{source}:{ref}"
        doc_hash = rc.content_hash(text)
        row = conn.execute("SELECT hash FROM docs WHERE id = ?", (doc_id,)).fetchone()
        if row and row[0] == doc_hash:
            skipped += 1
            continue

        chunks = rc.chunk_text(text, int(cfg["CHUNK_CHARS"]), int(cfg["CHUNK_OVERLAP"]))
        if not chunks:
            continue
        if dry_run:
            if row:
                updated += 1
            else:
                added += 1
            continue

        vecs = rc.embed_texts(chunks, cfg=cfg)
        now = _now()
        disposable, salience_cat = _norm_salience(rec)
        predicted_at = now if disposable else None
        # replace: drop any prior chunks for this doc, then insert fresh (chunk ids are
        # deterministic — doc_id#idx — so salience_access counters survive a re-embed)
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        for i, (chunk, vec) in enumerate(zip(chunks, vecs)):
            conn.execute(
                "INSERT INTO chunks (id, doc_id, source, ref, idx, text, dim, vec, updated_at, "
                "disposable, salience_cat, predicted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{doc_id}#{i}", doc_id, source, ref, i, chunk, len(vec),
                 rc.pack_vec(vec), now, disposable, salience_cat, predicted_at),
            )
        conn.execute(
            "INSERT INTO docs (id, source, ref, hash, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET hash=excluded.hash, updated_at=excluded.updated_at",
            (doc_id, source, ref, doc_hash, now),
        )
        conn.commit()
        if row:
            updated += 1
        else:
            added += 1
    return added, updated, skipped


def print_stats(conn):
    docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"index: {docs} docs, {chunks} chunks")
    for source, c in conn.execute(
        "SELECT source, COUNT(*) FROM chunks GROUP BY source ORDER BY source"
    ):
        print(f"  {source}: {c} chunks")
    # salience-learning observability (SALIENCE_SETUP.md): tagged predictions + touched rows
    predicted, forgotten = (
        conn.execute("SELECT COUNT(*) FROM chunks WHERE disposable = 1").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM chunks WHERE disposable >= 2").fetchone()[0],
    )
    touched = conn.execute("SELECT COUNT(*) FROM salience_access").fetchone()[0]
    if predicted or forgotten or touched:
        print(f"salience: {predicted} predicted-disposable, {forgotten} approved-forgotten, "
              f"{touched} chunks in the access ledger")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build/update the assistant's local RAG index.")
    ap.add_argument("--local", action="store_true", help="index run-log/carry-over/context-digest")
    ap.add_argument("--ingest", nargs="+", metavar="JSONL", help="index records from JSONL file(s)")
    ap.add_argument("--rebuild", action="store_true", help="drop the whole index first")
    ap.add_argument("--stats", action="store_true", help="print index stats and exit")
    ap.add_argument("--db", default=str(rc.DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true", help="report what would change; no embedding")
    args = ap.parse_args(argv)

    conn = rc.connect(args.db)

    if args.stats:
        print_stats(conn)
        return 0

    if args.rebuild and not args.dry_run:
        # NOTE: salience_access is deliberately NOT cleared — the index is a regenerable
        # cache, but the access ledger is accumulated *measurement*. Chunk ids are
        # deterministic (doc_id#idx), so counters stay valid across a rebuild.
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM docs")
        conn.commit()
        print("rebuilt: cleared existing index (salience access ledger preserved)")

    records = []
    if args.local:
        records.extend(_iter_local())
    if args.ingest:
        records.extend(_iter_jsonl(args.ingest))
    if not records:
        print("nothing to index (pass --local and/or --ingest)", file=sys.stderr)
        return 2

    try:
        added, updated, skipped = index_records(conn, records, rc.load_env(), dry_run=args.dry_run)
    except rc.OllamaError as exc:
        print(f"embedder unavailable — index not updated: {exc}", file=sys.stderr)
        return 3

    verb = "would index" if args.dry_run else "indexed"
    print(f"{verb}: +{added} new, ~{updated} updated, {skipped} unchanged")
    if not args.dry_run:
        print_stats(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
