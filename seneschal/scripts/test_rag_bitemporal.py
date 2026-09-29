#!/usr/bin/env python3
"""Tests for bitemporal fact tracking in the RAG index (valid_from / valid_to / supersedes).

Stdlib ``unittest`` only — no Ollama and no network: vectors are hand-built and
``embed_texts`` is monkeypatched where indexing needs it. Covers the load-bearing
properties of the "what is true now" layer:

* the docs-table migration is additive + idempotent AND upgrades a legacy index in
  place (columns appear, data intact, ``valid_from`` backfilled) — never a re-embed;
* ingest without the new fields behaves exactly as before;
* ``supersedes`` stamps the older doc's ``valid_to`` exactly once — a second
  superseder never overwrites the close, and nothing is ever deleted;
* the default query is **current-only** ("what is true now"): a closed row leaves
  the answer and the next-best current memory backfills;
* ``--include-superseded`` returns closed rows too, each clearly flagged non-current
  with its ``valid_to`` and (when known) what superseded it;
* ``--as-of`` returns what was current at that instant (half-open interval);
* currency and the salience ``disposable`` ladder stay independent axes.

Run:  python -m unittest seneschal.scripts.test_rag_bitemporal   (or)   python test_rag_bitemporal.py
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import rag_common as rc  # noqa: E402
import rag_index as ri   # noqa: E402
import rag_query as rq   # noqa: E402

CFG = {"CHUNK_CHARS": "800", "CHUNK_OVERLAP": "150"}

T_JAN = "2026-01-01T00:00:00+00:00"
T_MAR = "2026-03-01T00:00:00+00:00"
T_JUN = "2026-06-01T00:00:00+00:00"
T_JUL = "2026-07-01T00:00:00+00:00"
T_FUTURE = "2999-01-01T00:00:00+00:00"


def _db(tmpdir):
    return os.path.join(tmpdir, "rag-index.sqlite")


def _seed(conn, source, ref, vec, *, valid_from=T_JAN, valid_to=None,
          supersedes=None, disposable=0, text=None):
    """Insert one doc + one chunk directly (no embedder)."""
    doc_id = f"{source}:{ref}"
    conn.execute(
        "INSERT INTO docs (id, source, ref, hash, updated_at, valid_from, valid_to, "
        "supersedes) VALUES (?,?,?,?,?,?,?,?)",
        (doc_id, source, ref, f"hash-{ref}", T_JUL, valid_from, valid_to, supersedes),
    )
    conn.execute(
        "INSERT INTO chunks (id, doc_id, source, ref, idx, text, dim, vec, updated_at, "
        "disposable) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"{doc_id}#0", doc_id, source, ref, 0, text or ref, len(vec),
         rc.pack_vec(vec), T_JUL, disposable),
    )
    conn.commit()


def _doc(conn, doc_id):
    return conn.execute(
        "SELECT valid_from, valid_to, supersedes FROM docs WHERE id = ?", (doc_id,)
    ).fetchone()


class MigrationUnit(unittest.TestCase):
    def test_connect_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            for _ in range(3):  # repeated connects must not throw "duplicate column"
                rc.connect(_db(d)).close()
            conn = rc.connect(_db(d))
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(docs)")}
                self.assertLessEqual({"valid_from", "valid_to", "supersedes"}, cols)
            finally:
                conn.close()  # Windows: an open handle blocks tempdir cleanup

    def test_legacy_index_upgrades_in_place(self):
        """A pre-bitemporal index gains the columns without a rebuild or re-embed."""
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
                "INSERT INTO docs VALUES ('journal:old', 'journal', 'old', 'h', ?)",
                (T_JAN,),
            )
            legacy.execute(
                "INSERT INTO chunks VALUES ('journal:old#0','journal:old','journal','old',"
                "0,'legacy',3,?,?)", (rc.pack_vec([1.0, 0.0, 0.0]), T_JAN),
            )
            legacy.commit()
            legacy.close()

            conn = rc.connect(_db(d))
            try:
                # backfilled from updated_at (the closest thing to ingest time), open-ended
                self.assertEqual(_doc(conn, "journal:old"), (T_JAN, None, None))
                hits = rq.search(conn, [1.0, 0.0, 0.0], k=1)  # and it still answers
                self.assertEqual(hits[0]["ref"], "old")
            finally:
                conn.close()  # Windows: an open handle blocks tempdir cleanup


class TimeHelpersUnit(unittest.TestCase):
    def test_parse_ts_accepts_offsets_z_and_naive(self):
        aware = rc.parse_ts("2026-07-01T00:00:00+00:00")
        self.assertEqual(aware, datetime(2026, 7, 1, tzinfo=timezone.utc))
        self.assertEqual(rc.parse_ts("2026-07-01T00:00:00Z"), aware)
        self.assertEqual(rc.parse_ts("2026-07-01T00:00:00"), aware)  # naive → UTC

    def test_parse_ts_fails_open_to_none(self):
        for junk in (None, "", "not a time", 42, "2026-99-99"):
            self.assertIsNone(rc.parse_ts(junk))

    def test_now_mode_only_excludes_a_real_past_close(self):
        self.assertTrue(rc.doc_is_current(T_JAN, None))        # open = current
        self.assertFalse(rc.doc_is_current(T_JAN, T_JUN))      # closed in the past
        self.assertTrue(rc.doc_is_current(T_JAN, T_FUTURE))    # known future expiry
        self.assertTrue(rc.doc_is_current(T_JAN, "garbage"))   # fail-open, never hide
        self.assertTrue(rc.doc_is_current("", None))           # legacy '' fails open

    def test_as_of_interval_is_half_open(self):
        as_of = rc.parse_ts(T_MAR)
        self.assertTrue(rc.doc_is_current(T_JAN, T_JUN, as_of))    # inside the window
        self.assertFalse(rc.doc_is_current(T_JUN, None, as_of))    # not yet valid
        self.assertFalse(rc.doc_is_current(T_JAN, T_MAR, as_of))   # as_of == valid_to → out
        self.assertTrue(rc.doc_is_current(T_MAR, None, as_of))     # as_of == valid_from → in
        self.assertTrue(rc.doc_is_current("junk", T_JUN, as_of))   # bad start fails open


class IngestUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = rc.connect(_db(self.tmp.name))
        self._orig_embed = rc.embed_texts
        rc.embed_texts = lambda texts, cfg=None, timeout=120: [[1.0, 0.0, 0.0] for _ in texts]

    def tearDown(self):
        rc.embed_texts = self._orig_embed
        self.conn.close()
        self.tmp.cleanup()

    def _ingest(self, *recs, dry_run=False):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            counts = ri.index_records(self.conn, list(recs), CFG, dry_run=dry_run)
        return counts, err.getvalue()

    def test_record_without_new_fields_behaves_as_today(self):
        (added, updated, skipped), err = self._ingest(
            {"source": "journal", "ref": "plain", "text": "hi"}
        )
        self.assertEqual((added, updated, skipped), (1, 0, 0))
        self.assertEqual(err, "")
        valid_from, valid_to, supersedes = _doc(self.conn, "journal:plain")
        self.assertIsNotNone(rc.parse_ts(valid_from))  # stamped with ingest time
        self.assertIsNone(valid_to)
        self.assertIsNone(supersedes)

    def test_declared_valid_from_is_stored_verbatim(self):
        self._ingest({"source": "notes", "ref": "fact", "text": "x", "valid_from": T_MAR})
        self.assertEqual(_doc(self.conn, "notes:fact")[0], T_MAR)

    def test_unparsable_valid_from_warns_and_degrades_to_ingest_time(self):
        _, err = self._ingest(
            {"source": "notes", "ref": "bad", "text": "x", "valid_from": "yesterday-ish"}
        )
        self.assertIn("unparsable valid_from", err)
        self.assertIsNotNone(rc.parse_ts(_doc(self.conn, "notes:bad")[0]))

    def test_supersedes_stamps_valid_to_exactly_once(self):
        self._ingest({"source": "notes", "ref": "v1", "text": "old fact"})
        self._ingest({"source": "notes", "ref": "v2", "text": "new fact",
                      "valid_from": T_JUN, "supersedes": "v1"})
        self.assertEqual(_doc(self.conn, "notes:v1")[1], T_JUN)  # closed at v2's start
        self.assertEqual(_doc(self.conn, "notes:v2")[2], "v1")   # pointer recorded

        # a SECOND superseder never overwrites the existing close
        self._ingest({"source": "notes", "ref": "v3", "text": "newest fact",
                      "valid_from": T_JUL, "supersedes": "v1"})
        self.assertEqual(_doc(self.conn, "notes:v1")[1], T_JUN)  # first close stands

        # …and nothing was deleted: v1's doc and chunks are all still there
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id = 'notes:v1'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM docs").fetchone()[0], 3)

    def test_supersedes_unknown_target_warns_but_keeps_the_record(self):
        _, err = self._ingest({"source": "notes", "ref": "n", "text": "x",
                               "supersedes": "never-existed"})
        self.assertIn("supersedes unknown doc", err)
        self.assertEqual(_doc(self.conn, "notes:n")[2], "never-existed")  # intent recorded

    def test_self_supersede_is_ignored(self):
        self._ingest({"source": "notes", "ref": "loop", "text": "x"})
        _, err = self._ingest({"source": "notes", "ref": "loop", "text": "x changed",
                               "supersedes": "loop"})
        self.assertIn("supersedes itself", err)
        self.assertEqual(_doc(self.conn, "notes:loop")[1:], (None, None))  # still open

    def test_unchanged_doc_keeps_valid_from_but_supersede_still_stamps(self):
        self._ingest({"source": "notes", "ref": "old", "text": "prior"})
        self._ingest({"source": "notes", "ref": "same", "text": "stable",
                      "valid_from": T_MAR})
        # re-ingest with identical text: skipped (no re-embed), valid_from untouched,
        # but a newly declared supersedes still closes the target and records the pointer
        (a, u, s), _ = self._ingest({"source": "notes", "ref": "same", "text": "stable",
                                     "supersedes": "old"})
        self.assertEqual((a, u, s), (0, 0, 1))
        self.assertEqual(_doc(self.conn, "notes:same"), (T_MAR, None, "old"))
        self.assertEqual(_doc(self.conn, "notes:old")[1], T_MAR)  # closed at same's start

    def test_reingest_of_changed_text_never_reopens_a_closed_doc(self):
        self._ingest({"source": "notes", "ref": "v1", "text": "old"})
        self._ingest({"source": "notes", "ref": "v2", "text": "new",
                      "valid_from": T_JUN, "supersedes": "v1"})
        self._ingest({"source": "notes", "ref": "v1", "text": "old, edited"})
        self.assertEqual(_doc(self.conn, "notes:v1")[1], T_JUN)  # close survives the edit

    def test_dry_run_writes_nothing_even_with_supersedes(self):
        self._ingest({"source": "notes", "ref": "v1", "text": "old"})
        (a, u, s), _ = self._ingest(
            {"source": "notes", "ref": "v2", "text": "new", "supersedes": "v1"},
            dry_run=True,
        )
        self.assertEqual((a, u, s), (1, 0, 0))
        self.assertEqual(_doc(self.conn, "notes:v1")[1], None)   # not stamped
        self.assertIsNone(_doc(self.conn, "notes:v2"))           # not written

    def test_jsonl_roundtrip_carries_bitemporal_keys(self):
        p = os.path.join(self.tmp.name, "in.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"source": "notes", "ref": "r1", "text": "t",
                                "valid_from": T_MAR, "supersedes": "r0"}) + "\n")
            f.write(json.dumps({"source": "notes", "ref": "r2", "text": "t2"}) + "\n")
        recs = list(ri._iter_jsonl([p]))
        self.assertEqual(recs[0]["valid_from"], T_MAR)
        self.assertEqual(recs[0]["supersedes"], "r0")
        self.assertIsNone(recs[1]["valid_from"])
        self.assertIsNone(recs[1]["supersedes"])

    def test_stats_report_closed_docs(self):
        self._ingest({"source": "notes", "ref": "v1", "text": "old"})
        self._ingest({"source": "notes", "ref": "v2", "text": "new", "supersedes": "v1"})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            ri.print_stats(self.conn)
        self.assertIn("bitemporal: 1 superseded/closed docs", out.getvalue())


class QueryCurrencyUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = rc.connect(_db(self.tmp.name))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_default_excludes_closed_rows_and_backfills(self):
        # the closed row would rank #1 by similarity — the exact stale-version trap
        _seed(self.conn, "notes", "stale", [1.0, 0.0, 0.0],
              valid_to=T_JUN, text="old supplier")
        _seed(self.conn, "notes", "live", [0.9, 0.1, 0.0], text="new supplier")
        _seed(self.conn, "notes", "other", [0.8, 0.2, 0.0], text="unrelated")
        hits = rq.search(self.conn, [1.0, 0.0, 0.0], k=2)
        self.assertEqual([h["ref"] for h in hits], ["live", "other"])  # backfilled, no hole
        self.assertNotIn("current", hits[0])  # default output shape unchanged

    def test_future_valid_to_stays_current(self):
        _seed(self.conn, "notes", "expiring", [1.0, 0.0, 0.0], valid_to=T_FUTURE)
        hits = rq.search(self.conn, [1.0, 0.0, 0.0], k=1)
        self.assertEqual([h["ref"] for h in hits], ["expiring"])

    def test_include_superseded_returns_flagged_rows(self):
        _seed(self.conn, "notes", "v1", [1.0, 0.0, 0.0], valid_to=T_JUN, text="old")
        _seed(self.conn, "notes", "v2", [0.9, 0.1, 0.0],
              valid_from=T_JUN, supersedes="v1", text="new")
        hits = rq.search(self.conn, [1.0, 0.0, 0.0], k=2, include_superseded=True)
        self.assertEqual([h["ref"] for h in hits], ["v1", "v2"])
        stale, live = hits
        self.assertFalse(stale["current"])
        self.assertEqual(stale["valid_to"], T_JUN)
        self.assertEqual(stale["superseded_by"], "v2")
        self.assertTrue(live["current"])
        self.assertNotIn("valid_to", live)  # current rows carry no close

    def test_include_superseded_flags_unknown_successor_as_none(self):
        _seed(self.conn, "notes", "closed", [1.0, 0.0, 0.0], valid_to=T_JUN)
        hits = rq.search(self.conn, [1.0, 0.0, 0.0], k=1, include_superseded=True)
        self.assertFalse(hits[0]["current"])
        self.assertIsNone(hits[0]["superseded_by"])

    def test_as_of_returns_what_was_current_then(self):
        _seed(self.conn, "notes", "v1", [1.0, 0.0, 0.0],
              valid_from=T_JAN, valid_to=T_JUN, text="old fact")
        _seed(self.conn, "notes", "v2", [1.0, 0.0, 0.0],
              valid_from=T_JUN, supersedes="v1", text="new fact")
        march = rq.search(self.conn, [1.0, 0.0, 0.0], k=5, as_of=rc.parse_ts(T_MAR))
        self.assertEqual([h["ref"] for h in march], ["v1"])
        july = rq.search(self.conn, [1.0, 0.0, 0.0], k=5, as_of=rc.parse_ts(T_JUL))
        self.assertEqual([h["ref"] for h in july], ["v2"])
        # boundary: at the instant of supersession the successor is current (half-open)
        june = rq.search(self.conn, [1.0, 0.0, 0.0], k=5, as_of=rc.parse_ts(T_JUN))
        self.assertEqual([h["ref"] for h in june], ["v2"])
        now = rq.search(self.conn, [1.0, 0.0, 0.0], k=5)
        self.assertEqual([h["ref"] for h in now], ["v2"])

    def test_orphan_chunk_without_doc_row_fails_open(self):
        self.conn.execute(
            "INSERT INTO chunks (id, doc_id, source, ref, idx, text, dim, vec, updated_at) "
            "VALUES ('notes:orphan#0','notes:orphan','notes','orphan',0,'t',3,?,?)",
            (rc.pack_vec([1.0, 0.0, 0.0]), T_JUL),
        )
        self.conn.commit()
        hits = rq.search(self.conn, [1.0, 0.0, 0.0], k=1)
        self.assertEqual([h["ref"] for h in hits], ["orphan"])

    def test_currency_and_disposable_are_independent_axes(self):
        # a predicted-disposable CURRENT row is returned as always (observe-only),
        # and its recall still lands in the salience access ledger
        _seed(self.conn, "notes", "ack", [1.0, 0.0, 0.0], disposable=1, text="did that")
        _seed(self.conn, "notes", "closed", [0.9, 0.1, 0.0], valid_to=T_JUN)
        hits = rq.search(self.conn, [1.0, 0.0, 0.0], k=2, access_floor=0.55)
        self.assertEqual([h["ref"] for h in hits], ["ack"])
        self.assertEqual(hits[0]["disposable"], 1)
        touched = {r[0] for r in self.conn.execute("SELECT chunk_id FROM salience_access")}
        self.assertEqual(touched, {"notes:ack#0"})


class CliUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _db(self.tmp.name)
        conn = rc.connect(self.db)
        _seed(conn, "notes", "v1", [1.0, 0.0, 0.0],
              valid_from=T_JAN, valid_to=T_JUN, text="old")
        _seed(conn, "notes", "v2", [1.0, 0.0, 0.0],
              valid_from=T_JUN, supersedes="v1", text="new")
        conn.close()
        self._orig_embed = rc.embed_texts
        rc.embed_texts = lambda texts, cfg=None, timeout=120: [[1.0, 0.0, 0.0] for _ in texts]

    def tearDown(self):
        rc.embed_texts = self._orig_embed
        self.tmp.cleanup()

    def _run(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = rq.main(["query", "--db", self.db, "--no-record", *extra])
        return code, json.loads(out.getvalue())

    def test_default_is_current_only(self):
        code, hits = self._run()
        self.assertEqual(code, 0)
        self.assertEqual([h["ref"] for h in hits], ["v2"])

    def test_as_of_flag_time_travels(self):
        _, hits = self._run("--as-of", T_MAR)
        self.assertEqual([h["ref"] for h in hits], ["v1"])

    def test_include_superseded_flag_returns_flagged(self):
        _, hits = self._run("--include-superseded")
        by_ref = {h["ref"]: h for h in hits}
        self.assertEqual(set(by_ref), {"v1", "v2"})
        self.assertFalse(by_ref["v1"]["current"])
        self.assertEqual(by_ref["v1"]["valid_to"], T_JUN)
        self.assertEqual(by_ref["v1"]["superseded_by"], "v2")
        self.assertTrue(by_ref["v2"]["current"])

    def test_bad_as_of_fails_loudly(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                rq.main(["query", "--db", self.db, "--as-of", "lastweek"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("not an ISO-8601 timestamp", err.getvalue())


if __name__ == "__main__":
    unittest.main()
