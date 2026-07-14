#!/usr/bin/env python3
"""Tests for salience Phase 1 — disposability tagging + access instrumentation (observe-only).

Stdlib ``unittest`` only, no Ollama: vectors are hand-built and ``embed_texts`` is monkeypatched
where indexing needs it. Covers the load-bearing properties from the plan
(``~/.claude/plans/salience-learning-memory.md`` §2 + the §2.4 implementation resolution):

* schema migrations are idempotent AND upgrade a pre-salience legacy index in place;
* ``search()`` counts hits ≥ the access floor into ``salience_access`` (upsert, bounded);
* a predicted-disposable (``disposable=1``) row stays **fully retrievable** — observe-only;
* an approved-forgotten (``disposable=2``) row is excluded from answers (next-best backfills)
  but **still counted** — the un-forget signal;
* ``--no-record`` / ``record_access=False`` is a true no-op on the ledger;
* a broken ledger can never fail a recall;
* ingest carries/normalizes the optional ``disposable``/``salience_cat`` JSONL keys
  (closed-vocab coercion; ineligible-category predictions cleared).

Run:  python -m unittest seneschal.scripts.test_salience_access   (or)   python test_salience_access.py
"""
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import rag_common as rc  # noqa: E402
import rag_index as ri   # noqa: E402
import rag_query as rq   # noqa: E402

CFG = {"CHUNK_CHARS": "800", "CHUNK_OVERLAP": "150"}


def _db(tmpdir):
    return os.path.join(tmpdir, "rag-index.sqlite")


def _seed_chunk(conn, cid, vec, *, disposable=0, cat=None, text="t"):
    """Insert one chunk row directly (no embedder)."""
    conn.execute(
        "INSERT INTO chunks (id, doc_id, source, ref, idx, text, dim, vec, updated_at, "
        "disposable, salience_cat, predicted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, cid.split("#")[0], "journal", cid.split(":")[1].split("#")[0], 0, text,
         len(vec), rc.pack_vec(vec), "2026-07-11T00:00:00+00:00",
         disposable, cat, "2026-07-11T00:00:00+00:00" if disposable else None),
    )
    conn.commit()


def _ledger(conn):
    return {
        row[0]: {"hits": row[1], "cat": row[2], "max": row[3]}
        for row in conn.execute(
            "SELECT chunk_id, hit_count, salience_cat, max_score FROM salience_access"
        )
    }


class MigrationUnit(unittest.TestCase):
    def test_connect_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            for _ in range(3):  # repeated connects must not throw "duplicate column"
                conn = rc.connect(_db(d))
                conn.close()
            conn = rc.connect(_db(d))
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
                self.assertLessEqual({"disposable", "salience_cat", "predicted_at"}, cols)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM salience_access").fetchone()[0], 0
                )
            finally:
                conn.close()  # Windows: an open handle blocks tempdir cleanup

    def test_legacy_index_upgrades_in_place(self):
        """A pre-salience index (the live daemon's) gains the columns without a rebuild."""
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            legacy = sqlite3.connect(_db(d))
            legacy.execute(
                "CREATE TABLE docs (id TEXT PRIMARY KEY, source TEXT NOT NULL, "
                "ref TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            legacy.execute(
                "CREATE TABLE chunks (id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, "
                "source TEXT NOT NULL, ref TEXT NOT NULL, idx INTEGER NOT NULL, "
                "text TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL, "
                "updated_at TEXT NOT NULL)"
            )
            legacy.execute(
                "INSERT INTO chunks VALUES ('journal:old#0','journal:old','journal','old',"
                "0,'legacy',3,?, '2026-01-01T00:00:00+00:00')", (rc.pack_vec([1.0, 0.0, 0.0]),),
            )
            legacy.commit()
            legacy.close()

            conn = rc.connect(_db(d))
            try:
                row = conn.execute(
                    "SELECT disposable, salience_cat, predicted_at FROM chunks "
                    "WHERE id='journal:old#0'"
                ).fetchone()
                self.assertEqual(row, (0, None, None))  # defaulted, not lost
                hits = rq.search(conn, [1.0, 0.0, 0.0], k=1)  # and it still answers
                self.assertEqual(hits[0]["ref"], "old")
            finally:
                conn.close()  # Windows: an open handle blocks tempdir cleanup


class AccessCountingUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = rc.connect(_db(self.tmp.name))
        _seed_chunk(self.conn, "journal:a#0", [1.0, 0.0, 0.0], text="alpha")
        _seed_chunk(self.conn, "journal:b#0", [0.9, 0.1, 0.0], text="beta")
        _seed_chunk(self.conn, "journal:c#0", [0.0, 1.0, 0.0], text="gamma")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_counts_only_hits_above_floor(self):
        hits = rq.search(self.conn, [1.0, 0.0, 0.0], k=3, access_floor=0.55)
        self.assertEqual(len(hits), 3)  # answering is unchanged by counting
        led = _ledger(self.conn)
        self.assertEqual(led["journal:a#0"]["hits"], 1)
        self.assertEqual(led["journal:b#0"]["hits"], 1)
        self.assertNotIn("journal:c#0", led)  # orthogonal → below floor → not a touch

    def test_repeat_hits_aggregate_not_append(self):
        for _ in range(3):
            rq.search(self.conn, [1.0, 0.0, 0.0], k=1, access_floor=0.55)
        led = _ledger(self.conn)
        self.assertEqual(led["journal:a#0"]["hits"], 3)  # one row, counted up
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM salience_access").fetchone()[0], 1
        )
        first, last = self.conn.execute(
            "SELECT first_hit_at, last_hit_at FROM salience_access WHERE chunk_id='journal:a#0'"
        ).fetchone()
        self.assertIsNotNone(first)
        self.assertGreaterEqual(last, first)

    def test_max_score_keeps_the_best(self):
        rq.search(self.conn, [0.9, 0.1, 0.0], k=1, access_floor=0.55)   # good hit on b
        best = _ledger(self.conn)["journal:b#0"]["max"]
        rq.search(self.conn, [0.7, 0.3, 0.0], k=2, access_floor=0.55)   # weaker hit on b
        self.assertAlmostEqual(_ledger(self.conn)["journal:b#0"]["max"], best, places=6)

    def test_no_record_is_a_true_noop(self):
        rq.search(self.conn, [1.0, 0.0, 0.0], k=3, record_access=False)
        self.assertEqual(_ledger(self.conn), {})

    def test_broken_ledger_never_fails_a_recall(self):
        self.conn.execute("DROP TABLE salience_access")
        self.conn.commit()
        hits = rq.search(self.conn, [1.0, 0.0, 0.0], k=2)  # must not raise
        self.assertEqual(hits[0]["text"], "alpha")


class DisposableLadderUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = rc.connect(_db(self.tmp.name))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_predicted_disposable_stays_fully_retrievable(self):
        """disposable=1 is a logged prediction, never a hiding — the §0 commitment."""
        _seed_chunk(self.conn, "journal:ack#0", [1.0, 0.0, 0.0],
                    disposable=1, cat="ephemeral.ack", text="took meds")
        _seed_chunk(self.conn, "journal:keep#0", [0.9, 0.1, 0.0], text="keeper")
        hits = rq.search(self.conn, [1.0, 0.0, 0.0], k=2, access_floor=0.55)
        self.assertEqual([h["ref"] for h in hits], ["ack", "keep"])  # tagged row ranks #1, returned
        self.assertEqual(hits[0]["disposable"], 1)
        self.assertEqual(hits[0]["salience_cat"], "ephemeral.ack")
        self.assertEqual(_ledger(self.conn)["journal:ack#0"]["hits"], 1)  # …and counted

    def test_approved_forgotten_filtered_but_counted(self):
        """disposable=2 answers as-if-deleted (backfilled) while demand is still measured."""
        _seed_chunk(self.conn, "journal:gone#0", [1.0, 0.0, 0.0],
                    disposable=2, cat="ephemeral.ack", text="forgotten")
        _seed_chunk(self.conn, "journal:b#0", [0.9, 0.1, 0.0], text="beta")
        _seed_chunk(self.conn, "journal:c#0", [0.8, 0.2, 0.0], text="gamma")
        hits = rq.search(self.conn, [1.0, 0.0, 0.0], k=2, access_floor=0.55)
        self.assertEqual([h["ref"] for h in hits], ["b", "c"])  # backfilled to k, no hole
        led = _ledger(self.conn)
        self.assertEqual(led["journal:gone#0"]["hits"], 1)  # un-forget evidence accrues
        self.assertEqual(led["journal:b#0"]["hits"], 1)     # returned answers count too


class IngestTaggingUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = rc.connect(_db(self.tmp.name))
        self._orig_embed = rc.embed_texts
        rc.embed_texts = lambda texts, cfg=None, timeout=120: [[1.0, 0.0, 0.0] for _ in texts]

    def tearDown(self):
        rc.embed_texts = self._orig_embed
        self.conn.close()
        self.tmp.cleanup()

    def _row(self, ref):
        return self.conn.execute(
            "SELECT disposable, salience_cat, predicted_at FROM chunks WHERE ref=?", (ref,)
        ).fetchone()

    def test_tags_land_on_chunk_rows(self):
        ri.index_records(self.conn, [{
            "source": "journal", "ref": "2026-07-09", "text": "moved 3pm to 4",
            "disposable": 1, "salience_cat": "logistics.transient",
        }], CFG)
        disposable, cat, predicted_at = self._row("2026-07-09")
        self.assertEqual((disposable, cat), (1, "logistics.transient"))
        self.assertIsNotNone(predicted_at)

    def test_untagged_records_default_clean(self):
        ri.index_records(self.conn, [{"source": "journal", "ref": "plain", "text": "hi"}], CFG)
        self.assertEqual(self._row("plain"), (0, None, None))

    def test_unknown_category_coerced_ineligible_prediction_cleared(self):
        ri.index_records(self.conn, [
            {"source": "j", "ref": "weird", "text": "x", "salience_cat": "not.a.category"},
            {"source": "j", "ref": "bereavement", "text": "a family bereavement",
             "disposable": 1, "salience_cat": "identity.core"},
        ], CFG)
        self.assertEqual(self._row("weird")[:2], (0, "unknown"))
        disposable, cat, predicted_at = self._row("bereavement")
        self.assertEqual((disposable, cat), (0, "identity.core"))  # never predicted away
        self.assertIsNone(predicted_at)

    def test_jsonl_roundtrip_carries_tags(self):
        p = os.path.join(self.tmp.name, "in.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"source": "journal", "ref": "r1", "text": "t",
                                "disposable": 1, "salience_cat": "ephemeral.ack"}) + "\n")
            f.write(json.dumps({"source": "journal", "ref": "r2", "text": "t2"}) + "\n")
        recs = list(ri._iter_jsonl([p]))
        self.assertEqual(recs[0]["disposable"], 1)
        self.assertEqual(recs[0]["salience_cat"], "ephemeral.ack")
        self.assertEqual(recs[1]["disposable"], 0)
        self.assertIsNone(recs[1]["salience_cat"])

    def test_counters_survive_reingest_of_changed_doc(self):
        ri.index_records(self.conn, [{"source": "journal", "ref": "d", "text": "v1"}], CFG)
        rq.search(self.conn, [1.0, 0.0, 0.0], k=1, access_floor=0.5)
        self.assertEqual(_ledger(self.conn)["journal:d#0"]["hits"], 1)
        ri.index_records(self.conn, [{"source": "journal", "ref": "d", "text": "v2 changed"}], CFG)
        self.assertEqual(_ledger(self.conn)["journal:d#0"]["hits"], 1)  # deterministic id kept it


if __name__ == "__main__":
    unittest.main()
