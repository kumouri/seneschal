#!/usr/bin/env python3
"""Tests for the FTS5 lexical arm of the RAG index (hybrid retrieval).

**The load-bearing family is `Fusion`**, and specifically
`test_a_buried_rare_token_becomes_rank_one`: it reproduces the SHAPE of the failure the arm exists
for — a rare name whose chunk the dense arm ranks last, below the access floor, *in a corpus that
contains it* — and asserts the fix. If that test can be deleted without anything else going red,
the lexical arm has no reason to exist.

Three families guard the ways this feature could do harm rather than merely fail:

* `RarityCut` / `CommonWordsChangeNothing` — the arm may only fire on tokens RARE in this corpus.
  Without the cut, ORing every token (`who`, `is`, `the`) makes the matching set most of the corpus
  and bm25 then promotes weak, short chunks over better dense hits — a regression on the exact
  "who is X?" query the feature exists for.
* `SafeMatchQuery` — free text is NEVER interpolated into a `MATCH` expression. FTS5 gives `"`,
  `*`, `:`, `^`, `-`, `(`, `)`, `AND`/`OR`/`NOT` and `NEAR` their own meanings, so an apostrophe
  is a syntax error and a bare `NOT` silently inverts the query. Every case here is a real thing
  a person types into a chat.
* `Fusion.test_the_lexical_arm_cannot_readmit_a_superseded_doc` — the bitemporal guarantee is not
  the retriever's to relax. Fusion may only REORDER what survived the currency filter.

Everything else follows the house posture: additive, fail-soft, and byte-identical when off. No
Ollama and no network — the "embedder" is four hand-written floats.

Run:  python -m unittest seneschal.scripts.test_rag_fts
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import rag_common as rc  # noqa: E402
import rag_index as ri  # noqa: E402
import rag_query as rq  # noqa: E402

DIM = 4
NOW = "2026-08-04T09:53:00Z"
NAME = "Ondine"   # the rare proper noun the dense arm buries


def _tmpdir(case):
    d = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, d, True)
    return pathlib.Path(d)


def _add(conn, chunk_id, text, vec, *, source="chat", valid_to=None):
    """Insert one doc+chunk into BOTH arms, the way rag_index.py does."""
    doc_id = chunk_id.split("#")[0]
    ref = doc_id.split(":", 1)[1]
    conn.execute(
        "INSERT OR REPLACE INTO docs (id, source, ref, hash, updated_at, valid_from, valid_to, "
        "supersedes) VALUES (?,?,?,?,?,?,?,NULL)",
        (doc_id, source, ref, "h", NOW, NOW, valid_to))
    conn.execute(
        "INSERT OR REPLACE INTO chunks (id, doc_id, source, ref, idx, text, dim, vec, updated_at) "
        "VALUES (?,?,?,?,0,?,?,?,?)",
        (chunk_id, doc_id, source, ref, text, DIM, rc.pack_vec(vec), NOW))
    rc.fts_index_chunk(conn, chunk_id, doc_id, source, text)


def _corpus(conn, n_decoys=30):
    """30 chunks the dense arm loves, plus one it buries that is the only lexical match.

    A rare proper noun whose surrounding text is *not* what the query is about, so cosine
    similarity ranks it last."""
    for i in range(n_decoys):
        _add(conn, f"chat:turn:d{i}#0",
             f"Owner: decoy chatter number {i} about work and scheduling",
             [1.0, 0.02 * (n_decoys - i), 0.0, 0.0])
    _add(conn, "chat:turn:ans#0", f"Owner: {NAME.upper()} again. moved the deadline a second time.",
         [0.35, 0.0, 0.9, 0.0])
    conn.commit()


QVEC = [1.0, 0.5, 0.0, 0.0]  # points at the decoys, not at the answer


class _DbCase(unittest.TestCase):
    """A throwaway index per test, closed afterwards — an unclosed sqlite handle blocks temp
    directory cleanup on Windows."""

    def setUp(self):
        self.conn = rc.connect(_tmpdir(self) / "x.sqlite")

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass


class LexicalArmSchema(unittest.TestCase):
    def setUp(self):
        self.db = _tmpdir(self) / "x.sqlite"

    def test_connect_creates_the_arm(self):
        conn = rc.connect(self.db)
        self.addCleanup(conn.close)
        self.assertTrue(rc.fts_available(conn))

    def test_migration_is_idempotent(self):
        rc.connect(self.db).close()
        conn = rc.connect(self.db)  # second open must not raise on an existing table
        self.addCleanup(conn.close)
        self.assertTrue(rc.fts_available(conn))

    def test_a_database_without_the_arm_degrades_rather_than_raising(self):
        """An index built before the arm existed must keep answering from the dense arm alone."""
        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        self.assertFalse(rc.fts_available(legacy))
        self.assertEqual(rc.fts_search(legacy, NAME), [])
        rc.fts_index_chunk(legacy, "a", "b", "c", "d")   # all no-ops
        rc.fts_delete_doc(legacy, "b")
        rc.fts_clear(legacy)
        self.assertEqual(rc.fts_backfill(legacy), 0)


class SafeMatchQuery(_DbCase):
    """Raw text into `MATCH` is a syntax minefield. None of these may raise."""

    def setUp(self):
        super().setUp()
        _add(self.conn, "chat:turn:t1#0", f"Owner: {NAME.upper()}", [1.0, 0, 0, 0])
        self.conn.commit()

    def test_fts_operators_in_free_text_do_not_raise(self):
        for q in ['"', 'NOT', 'a OR', 'foo*', 'x:y', '^^^', '(((', ')))', "it's", "NEAR(a b)",
                  'a AND b', '- -', '"unclosed', 'a"b"c', '***', f'{NAME} OR NOT']:
            with self.subTest(q=q):
                self.assertIsInstance(rc.fts_search(self.conn, q), list)

    def test_empty_and_non_string_queries_yield_nothing(self):
        for q in ["", "   ", "\n", "!", "?!", None, 42, [], {"a": 1}]:
            with self.subTest(q=q):
                self.assertEqual(rc.fts_search(self.conn, q), [])

    def test_a_question_around_the_name_still_finds_it(self):
        """"who is X?" is what a person actually types."""
        self.assertEqual(rc.fts_search(self.conn, f"who is {NAME}?"), ["chat:turn:t1#0"])

    def test_the_name_alone_finds_it(self):
        self.assertEqual(rc.fts_search(self.conn, NAME), ["chat:turn:t1#0"])

    def test_matching_is_case_insensitive(self):
        self.assertEqual(rc.fts_search(self.conn, NAME.lower()), ["chat:turn:t1#0"])

    def test_a_single_character_token_is_dropped(self):
        """One-character tokens match almost everything and rank nothing; they are noise."""
        self.assertIsNone(rc.fts_match_query("a"))

    def test_the_term_count_is_bounded(self):
        """A pasted paragraph must not become a 4,000-term MATCH expression."""
        built = rc.fts_match_query(" ".join(f"tok{i}" for i in range(500)))
        self.assertEqual(built.count(" OR ") + 1, rc._FTS_MAX_TERMS)

    def test_source_filter_is_honoured(self):
        _add(self.conn, "run-log:2026-08-04#0", f"{NAME} appears here too", [1.0, 0, 0, 0],
             source="run-log")
        self.conn.commit()
        self.assertEqual(rc.fts_search(self.conn, NAME, source="run-log"),
                         ["run-log:2026-08-04#0"])


class Fusion(_DbCase):
    """The reason the lexical arm exists."""

    def setUp(self):
        super().setUp()
        _corpus(self.conn)

    def _rank_of_answer(self, hits):
        return next((i for i, h in enumerate(hits, 1) if NAME.upper() in h["text"]), None)

    def test_the_dense_arm_alone_buries_the_rare_token(self):
        """The failure being fixed. Asserted first so the fix cannot be read as a no-op."""
        hits = rq.search(self.conn, QVEC, k=99, record_access=False)
        self.assertEqual(self._rank_of_answer(hits), 31)
        self.assertLess(hits[30]["score"], rq.ACCESS_FLOOR)   # below the floor

    def test_a_buried_rare_token_becomes_rank_one(self):
        hits = rq.search(self.conn, QVEC, k=5, record_access=False, query_text=NAME)
        self.assertEqual(self._rank_of_answer(hits), 1)

    def test_the_returned_score_is_still_the_true_cosine(self):
        """Never a fused number masquerading as a similarity — `min_score` and the access floor
        are cosine thresholds and must keep meaning what they say."""
        hits = rq.search(self.conn, QVEC, k=5, record_access=False, query_text=NAME)
        self.assertAlmostEqual(hits[0]["score"], 0.3242, places=3)

    def test_without_query_text_the_result_is_byte_identical(self):
        """Every caller that predates the arm must be untouched — including the output SHAPE."""
        before = rq.search(self.conn, QVEC, k=5, record_access=False)
        self.assertEqual(self._rank_of_answer(before), None)
        self.assertNotIn("lexical", before[0])

    def test_a_content_query_is_not_disturbed_by_the_arm(self):
        """The dense arm was already right about content queries; fusion must not break that."""
        hits = rq.search(self.conn, QVEC, k=3, record_access=False,
                         query_text="decoy chatter about work")
        self.assertIn("decoy", hits[0]["text"])

    def test_the_lexical_arm_cannot_readmit_a_superseded_doc(self):
        """The bitemporal guarantee is not the retriever's to relax. Fusion REORDERS what
        survived the currency filter; it never re-admits."""
        self.conn.execute("UPDATE docs SET valid_to = ? WHERE id = ?",
                          ("2026-08-04T10:00:00Z", "chat:turn:ans"))
        self.conn.commit()
        hits = rq.search(self.conn, QVEC, k=5, record_access=False, query_text=NAME)
        self.assertIsNone(self._rank_of_answer(hits))

    def test_include_superseded_still_reaches_it(self):
        self.conn.execute("UPDATE docs SET valid_to = ? WHERE id = ?",
                          ("2026-08-04T10:00:00Z", "chat:turn:ans"))
        self.conn.commit()
        hits = rq.search(self.conn, QVEC, k=5, record_access=False, query_text=NAME,
                         include_superseded=True)
        self.assertEqual(self._rank_of_answer(hits), 1)

    def test_a_query_matching_nothing_lexically_changes_nothing(self):
        with_arm = rq.search(self.conn, QVEC, k=5, record_access=False,
                             query_text="zzzzqqqq nonexistent")
        without = rq.search(self.conn, QVEC, k=5, record_access=False)
        self.assertEqual([h["ref"] for h in with_arm], [h["ref"] for h in without])

    def test_fusion_is_deterministic(self):
        a = rq.search(self.conn, QVEC, k=5, record_access=False, query_text=f"{NAME} deadline")
        b = rq.search(self.conn, QVEC, k=5, record_access=False, query_text=f"{NAME} deadline")
        self.assertEqual([h["ref"] for h in a], [h["ref"] for h in b])

    def test_the_lexical_marker_is_only_on_lexical_hits(self):
        hits = rq.search(self.conn, QVEC, k=5, record_access=False, query_text=NAME)
        self.assertTrue(hits[0].get("lexical"))
        self.assertNotIn("lexical", hits[1])


class SalienceFloor(_DbCase):
    """A lexical hit counts as a recall even below the cosine floor — otherwise the arm would
    systematically under-count exactly the recalls it exists to enable, and salience is
    measurement, so an undercount there is a wrong answer about what is safe to forget."""

    def setUp(self):
        super().setUp()
        _corpus(self.conn)

    def _touches(self, chunk_id):
        row = self.conn.execute(
            "SELECT hit_count FROM salience_access WHERE chunk_id = ?", (chunk_id,)).fetchone()
        return row[0] if row else 0

    def test_a_sub_floor_lexical_hit_is_counted(self):
        rq.search(self.conn, QVEC, k=5, query_text=NAME)
        self.assertEqual(self._touches("chat:turn:ans#0"), 1)

    def test_the_same_hit_is_not_counted_without_the_arm(self):
        """It scores 0.3242 — the cosine floor legitimately excludes it."""
        rq.search(self.conn, QVEC, k=5)
        self.assertEqual(self._touches("chat:turn:ans#0"), 0)

    def test_a_ledger_failure_never_fails_a_recall(self):
        self.conn.execute("DROP TABLE salience_access")
        self.conn.commit()
        hits = rq.search(self.conn, QVEC, k=5, query_text=NAME)
        self.assertTrue(hits[0]["text"].startswith(f"Owner: {NAME.upper()}"))


class WriteSites(_DbCase):
    """The two indexes are written and deleted by the same key, so they cannot drift into
    disagreeing about which docs exist."""

    def _lex_count(self):
        return self.conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]

    def test_reindexing_a_chunk_does_not_duplicate_it(self):
        for _ in range(3):
            rc.fts_index_chunk(self.conn, "chat:turn:t1#0", "chat:turn:t1", "chat", NAME)
        self.assertEqual(self._lex_count(), 1)

    def test_delete_doc_removes_every_chunk_of_that_doc(self):
        rc.fts_index_chunk(self.conn, "chat:turn:t1#0", "chat:turn:t1", "chat", f"a {NAME}")
        rc.fts_index_chunk(self.conn, "chat:turn:t1#1", "chat:turn:t1", "chat", f"b {NAME}")
        rc.fts_index_chunk(self.conn, "chat:turn:t2#0", "chat:turn:t2", "chat", f"c {NAME}")
        rc.fts_delete_doc(self.conn, "chat:turn:t1")
        self.assertEqual(rc.fts_search(self.conn, NAME), ["chat:turn:t2#0"])

    def test_clear_empties_the_arm(self):
        rc.fts_index_chunk(self.conn, "chat:turn:t1#0", "chat:turn:t1", "chat", "x")
        rc.fts_clear(self.conn)
        self.assertEqual(self._lex_count(), 0)

    def test_index_records_writes_both_arms(self):
        """The real write path, not the helper: an indexed doc is lexically findable."""
        real = rc.embed_texts
        rc.embed_texts = lambda chunks, cfg=None, **_: [[1.0, 0.0, 0.0, 0.0] for _ in chunks]
        self.addCleanup(setattr, rc, "embed_texts", real)
        ri.index_records(self.conn, [{"source": "chat", "ref": "turn:t9",
                                      "text": f"Owner: ask {NAME} about it"}],
                         {"CHUNK_CHARS": "800", "CHUNK_OVERLAP": "150"})
        self.assertEqual(rc.fts_search(self.conn, NAME), ["chat:turn:t9#0"])


class Backfill(_DbCase):
    """An index that predates the arm gains `chunks_fts` empty. An arm that reports itself
    available and returns nothing is the worst of the three states."""

    def setUp(self):
        super().setUp()
        _corpus(self.conn)
        self.conn.execute("DELETE FROM chunks_fts")   # simulate the pre-arm state
        self.conn.commit()

    def test_backfill_populates_from_the_dense_side(self):
        self.assertEqual(rc.fts_search(self.conn, NAME), [])
        self.assertEqual(rc.fts_backfill(self.conn), 31)
        self.assertEqual(rc.fts_search(self.conn, NAME), ["chat:turn:ans#0"])

    def test_backfill_is_a_noop_once_populated(self):
        rc.fts_backfill(self.conn)
        self.assertEqual(rc.fts_backfill(self.conn), 0)

    def test_backfill_on_an_empty_index_writes_nothing(self):
        self.conn.execute("DELETE FROM chunks")
        self.conn.commit()
        self.assertEqual(rc.fts_backfill(self.conn), 0)

    def test_the_indexer_backfills_on_a_dry_run_never(self):
        """`--dry-run` reports; it does not write."""
        db = _tmpdir(self) / "y.sqlite"
        c = rc.connect(db)
        _corpus(c)
        c.execute("DELETE FROM chunks_fts")
        c.commit()
        c.close()
        ingest = db.parent / "one.jsonl"
        ingest.write_text('{"source": "journal", "ref": "r", "text": "t"}\n', encoding="utf-8")
        ri.main(["--db", str(db), "--dry-run", "--ingest", str(ingest)])
        c = sqlite3.connect(db)
        try:
            self.assertEqual(c.execute("SELECT count(*) FROM chunks_fts").fetchone()[0], 0)
        finally:
            c.close()


class RarityCut(_DbCase):
    """The lexical arm may only fire on tokens RARE in this corpus.

    bm25 ranks *within* the matching set, and OR makes that set most of the corpus, so matching
    common words lets a short chunk stuffed with `who` and `is` outrank a genuinely better dense
    hit. The fix is a stopword list DERIVED FROM THE CORPUS rather than written by hand: no
    maintenance, adapts as the corpus grows, and right about *this* corpus rather than about
    English — a project codename can be rare anywhere else and a frequent topic here."""

    def _saturate(self, token, n_chunks, total=200):
        """A corpus of `total` chunks where `token` appears in `n_chunks` of them."""
        for i in range(total):
            text = f"{token} filler {i}" if i < n_chunks else f"filler {i}"
            rc.fts_index_chunk(self.conn, f"s:d{i}#0", f"s:d{i}", "chat", text)
        self.conn.commit()

    def test_a_saturated_token_never_reaches_match(self):
        self._saturate("is", 100)          # 50% of the corpus
        self.assertEqual(rc.fts_rare_terms(self.conn, "is"), [])

    def test_a_rare_token_does(self):
        self._saturate(NAME, 1)            # 0.5% of the corpus
        self.assertEqual(rc.fts_rare_terms(self.conn, NAME), [NAME])

    def test_the_cut_is_corpus_derived_not_a_word_list(self):
        """The SAME token, kept in one corpus and dropped in another — which is the whole
        argument for deriving it rather than writing it down."""
        self._saturate("Kestrel", 1)
        self.assertEqual(rc.fts_rare_terms(self.conn, "Kestrel"), ["Kestrel"])
        other = rc.connect(_tmpdir(self) / "y.sqlite")
        self.addCleanup(other.close)
        for i in range(200):
            rc.fts_index_chunk(other, f"s:d{i}#0", f"s:d{i}", "chat", f"Kestrel filler {i}")
        other.commit()
        self.assertEqual(rc.fts_rare_terms(other, "Kestrel"), [])

    def test_a_token_absent_from_the_corpus_is_dropped(self):
        """A lexical index cannot return a string that was never written. Carrying the term would
        only add MATCH clauses that can never match."""
        self._saturate("filler", 200)
        self.assertEqual(rc.fts_rare_terms(self.conn, NAME), [])

    def test_only_the_rare_tokens_of_a_mixed_query_survive(self):
        self._saturate("is", 100)
        rc.fts_index_chunk(self.conn, "s:x#0", "s:x", "chat", f"{NAME} is here")
        self.conn.commit()
        self.assertEqual(rc.fts_rare_terms(self.conn, f"who is {NAME}?"), [NAME])

    def test_a_query_of_only_common_words_silences_the_arm(self):
        self._saturate("is", 100)
        self.assertEqual(rc.fts_search(self.conn, "what is the"), [])

    def test_a_small_corpus_still_gets_an_arm(self):
        """A percentage alone would silence the arm on a young index: at 20 chunks, 1% is 0.2, so
        a token in ONE chunk already exceeds it. The absolute floor wins when it is larger."""
        for i in range(20):
            rc.fts_index_chunk(self.conn, f"s:d{i}#0", f"s:d{i}", "chat",
                               f"{NAME} here" if i == 0 else f"filler {i}")
        self.conn.commit()
        self.assertEqual(rc.fts_rare_terms(self.conn, NAME), [NAME])

    def test_an_empty_corpus_yields_nothing(self):
        self.assertEqual(rc.fts_rare_terms(self.conn, NAME), [])

    def test_duplicate_tokens_are_asked_about_once(self):
        self._saturate(NAME, 1)
        self.assertEqual(rc.fts_rare_terms(self.conn, f"{NAME} {NAME.upper()} {NAME.lower()}"),
                         [NAME])

    def test_hostile_input_does_not_raise(self):
        self._saturate("filler", 5)
        for q in ['"', "NOT", "(((", None, 42, "", "   "]:
            with self.subTest(q=q):
                self.assertEqual(rc.fts_rare_terms(self.conn, q), [])

    def test_a_legacy_database_yields_nothing(self):
        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        self.assertEqual(rc.fts_rare_terms(legacy, NAME), [])


class CommonWordsChangeNothing(_DbCase):
    """End-to-end: a query whose only tokens are common must leave `search()` returning exactly
    what the dense arm returned — same order, same shape."""

    def setUp(self):
        super().setUp()
        _corpus(self.conn)

    def test_a_common_word_query_is_byte_identical_to_dense_only(self):
        # "decoy", "chatter", "about", "work" are each in 30 of 31 chunks — saturated.
        hybrid = rq.search(self.conn, QVEC, k=5, record_access=False,
                           query_text="decoy chatter about work")
        dense = rq.search(self.conn, QVEC, k=5, record_access=False)
        self.assertEqual(hybrid, dense)

    def test_the_rare_token_still_wins_when_it_is_there(self):
        """The cut must not have neutered the feature it protects."""
        hits = rq.search(self.conn, QVEC, k=5, record_access=False, query_text=NAME)
        self.assertIn(NAME.upper(), hits[0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
