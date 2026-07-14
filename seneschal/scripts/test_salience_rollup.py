#!/usr/bin/env python3
"""Tests for salience_rollup.py — the weekly what's-safe-to-forget report (Phase 3).

Stdlib ``unittest`` only, no Ollama: chunks + counters are seeded directly into a temp sqlite
index, events into a temp JSONL. The load-bearing properties (plan §4 + §3.3):

* the **protect clause** dominates frequency — a θ_protect-weight event shields its category
  (and its `chunk_ref` doc) from any prune verdict, no matter how cold the counters;
* the three honest buckets: confirmed (near-zero recall), refuted (real recall), and an
  explicit inconclusive between them — never forced verdicts;
* **abstention** below the evidence window (days AND tagged-doc count), with zero proposals;
* proposals are emitted only for confirmed categories inside a met window;
* the classifier-divergence flags; tolerant event loading; cold-start (missing files) safety.

Run:  python -m unittest seneschal.scripts.test_salience_rollup   (or)  python test_salience_rollup.py
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import rag_common as rc        # noqa: E402
import salience_rollup as sr   # noqa: E402

NOW = datetime(2026, 8, 15, 4, 0, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=40)).isoformat(timespec="seconds")     # past the 30-day window
FRESH = (NOW - timedelta(days=2)).isoformat(timespec="seconds")    # inside it


def _seed_doc(conn, doc, cat, *, disposable=0, predicted_at=None, hits=0, chunks=1):
    """One doc = `chunks` chunk rows (+ optional aggregated hits on chunk #0)."""
    for i in range(chunks):
        cid = f"{doc}#{i}"
        conn.execute(
            "INSERT INTO chunks (id, doc_id, source, ref, idx, text, dim, vec, updated_at, "
            "disposable, salience_cat, predicted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, doc, doc.split(":")[0], doc.split(":")[1], i, "t", 3,
             rc.pack_vec([1.0, 0.0, 0.0]), OLD, disposable, cat, predicted_at),
        )
    if hits:
        conn.execute(
            "INSERT INTO salience_access (chunk_id, doc_id, salience_cat, hit_count, "
            "first_hit_at, last_hit_at, max_score) VALUES (?,?,?,?,?,?,?)",
            (f"{doc}#0", doc, cat, hits, OLD, FRESH, 0.8),
        )
    conn.commit()


class RollupHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "rag-index.sqlite")
        self.events_path = os.path.join(self.tmp.name, "forgetting-events.jsonl")
        self.conn = rc.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _events(self, events):
        with open(self.events_path, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        return sr.load_events(self.events_path)

    def _verdicts(self, events=()):
        stats = sr.category_stats(self.conn)
        return sr.bucket_verdicts(stats, list(events))


class BucketUnit(RollupHarness):
    def test_confirmed_refuted_and_inconclusive(self):
        # 3 cold tagged docs → confirmed; 2 hot tagged docs → refuted; one lukewarm → inconclusive
        for i in range(3):
            _seed_doc(self.conn, f"journal:ack{i}", "ephemeral.ack",
                      disposable=1, predicted_at=OLD, hits=0)
        for i in range(2):
            _seed_doc(self.conn, f"journal:log{i}", "logistics.transient",
                      disposable=1, predicted_at=OLD, hits=3)
        _seed_doc(self.conn, "journal:snap", "status.snapshot",
                  disposable=1, predicted_at=OLD, hits=0)
        _seed_doc(self.conn, "journal:snap2", "status.snapshot",
                  disposable=1, predicted_at=OLD, hits=1)
        verdicts, _ = self._verdicts()
        self.assertEqual(verdicts["ephemeral.ack"]["bucket"], "confirmed")
        self.assertEqual(verdicts["logistics.transient"]["bucket"], "refuted")
        self.assertEqual(verdicts["status.snapshot"]["bucket"], "inconclusive")

    def test_untagged_category_just_observes(self):
        _seed_doc(self.conn, "journal:core", "identity.core", hits=0)
        verdicts, _ = self._verdicts()
        self.assertEqual(verdicts["identity.core"]["bucket"], "observing")

    def test_protect_clause_dominates_frequency(self):
        """The birthday case: zero hits, tagged-eligible… but one heavy event shields it all."""
        for i in range(5):
            _seed_doc(self.conn, f"journal:s{i}", "status.snapshot",
                      disposable=1, predicted_at=OLD, hits=0)  # screams "confirmed"…
        events = self._events([{
            "ts": FRESH, "subject": "the snapshot that mattered",
            "salience_cat": "status.snapshot", "sentiment": -0.85,
            "chunk_ref": "journal:s3", "recorded_by": "chat",
        }])
        verdicts, protected_docs = sr.bucket_verdicts(sr.category_stats(self.conn), events)
        self.assertEqual(verdicts["status.snapshot"]["bucket"], "protected")  # …but is shielded
        self.assertIn("journal:s3", protected_docs)

    def test_light_event_does_not_protect(self):
        _seed_doc(self.conn, "journal:a", "ephemeral.ack", disposable=1,
                  predicted_at=OLD, hits=0)
        events = self._events([{"ts": FRESH, "subject": "meh",
                                "salience_cat": "ephemeral.ack", "sentiment": -0.2}])
        verdicts, protected = sr.bucket_verdicts(sr.category_stats(self.conn), events)
        self.assertEqual(verdicts["ephemeral.ack"]["bucket"], "confirmed")
        self.assertEqual(protected, set())


class WindowUnit(RollupHarness):
    def test_abstains_below_the_window(self):
        _seed_doc(self.conn, "journal:x", "ephemeral.ack", disposable=1,
                  predicted_at=FRESH, hits=0)  # 2 days old, 1 tagged doc — both bars unmet
        stats = sr.category_stats(self.conn)
        ok, days, tagged = sr.window_state(stats, NOW)
        self.assertFalse(ok)
        self.assertEqual(tagged, 1)
        self.assertLess(days, sr.MIN_DAYS)
        report = sr.render_report(*self._verdicts(), [],
                                  (ok, days, tagged), now=NOW)
        self.assertIn("Insufficient data", report)
        self.assertNotIn("🗑️", report)  # no verdict sections rendered at all

    def test_window_needs_both_bars(self):
        for i in range(60):  # plenty tagged, but all fresh → days bar unmet
            _seed_doc(self.conn, f"journal:t{i}", "ephemeral.ack",
                      disposable=1, predicted_at=FRESH)
        ok, days, tagged = sr.window_state(sr.category_stats(self.conn), NOW)
        self.assertFalse(ok)
        self.assertEqual(tagged, 60)

    def test_met_window_reports_verdicts(self):
        for i in range(55):
            _seed_doc(self.conn, f"journal:t{i}", "ephemeral.ack",
                      disposable=1, predicted_at=OLD, hits=0)
        stats = sr.category_stats(self.conn)
        window = sr.window_state(stats, NOW)
        self.assertTrue(window[0])
        verdicts, protected = sr.bucket_verdicts(stats, [])
        report = sr.render_report(verdicts, protected, [], window, now=NOW)
        self.assertIn("`ephemeral.ack` — confirmed", report)

    def test_cold_start_empty_db_and_missing_events(self):
        stats = sr.category_stats(self.conn)      # empty index
        self.assertEqual(stats, {})
        self.assertEqual(sr.load_events(os.path.join(self.tmp.name, "nope.jsonl")), [])
        ok, days, tagged = sr.window_state(stats, NOW)
        self.assertFalse(ok)
        self.assertEqual((days, tagged), (0, 0))


class ProposalUnit(RollupHarness):
    def _confirmed_setup(self):
        for i in range(55):
            _seed_doc(self.conn, f"journal:t{i}", "ephemeral.ack",
                      disposable=1, predicted_at=OLD, hits=0)

    def test_confirmed_category_yields_gated_draft(self):
        self._confirmed_setup()
        stats = sr.category_stats(self.conn)
        verdicts, _ = sr.bucket_verdicts(stats, [])
        text = sr.render_proposals(verdicts, sr.window_state(stats, NOW))
        self.assertIn("ephemeral.ack", text)
        self.assertIn("disposable=2", text)
        self.assertIn("the owner's explicit approval", text)
        self.assertNotIn("delete", text.split("No hard deletion")[0])  # soft language only

    def test_no_proposals_when_window_unmet_even_if_confirmed_shape(self):
        _seed_doc(self.conn, "journal:t", "ephemeral.ack", disposable=1,
                  predicted_at=FRESH, hits=0)
        stats = sr.category_stats(self.conn)
        verdicts, _ = sr.bucket_verdicts(stats, [])
        self.assertEqual(sr.render_proposals(verdicts, sr.window_state(stats, NOW)), "")

    def test_no_proposals_for_protected_or_refuted(self):
        self._confirmed_setup()
        events = self._events([{"ts": FRESH, "subject": "an ack that cut deep",
                                "salience_cat": "ephemeral.ack", "sentiment": -0.9}])
        stats = sr.category_stats(self.conn)
        verdicts, _ = sr.bucket_verdicts(stats, events)
        self.assertEqual(verdicts["ephemeral.ack"]["bucket"], "protected")
        self.assertEqual(sr.render_proposals(verdicts, sr.window_state(stats, NOW)), "")


class EventsUnit(RollupHarness):
    def test_load_events_skips_garbage(self):
        with open(self.events_path, "w", encoding="utf-8") as f:
            f.write('{"subject": "ok", "sentiment": -0.5}\n')
            f.write("not json at all\n")
            f.write('[1,2,3]\n')     # JSON but not an object
            f.write("\n")
            f.write('{"subject": "also ok"}\n')
        events = sr.load_events(self.events_path)
        self.assertEqual([e["subject"] for e in events], ["ok", "also ok"])

    def test_divergence_flags(self):
        events = [
            {"subject": "big gap", "sentiment": -0.8,
             "classifier_weight": -0.1, "classifier_confidence": 0.7},   # flagged
            {"subject": "agrees", "sentiment": -0.8,
             "classifier_weight": -0.7, "classifier_confidence": 0.9},   # fine
            {"subject": "low conf", "sentiment": -0.8,
             "classifier_weight": 0.2, "classifier_confidence": 0.2},    # ignored
            {"subject": "unscored", "sentiment": -0.8},                  # ignored
        ]
        flags = sr.divergences(events)
        self.assertEqual([f["subject"] for f in flags], ["big gap"])

    def test_bad_sentiment_values_never_crash(self):
        events = [{"subject": "weird", "salience_cat": "ephemeral.ack", "sentiment": "very"}]
        cats, docs = sr.protection(events)
        self.assertEqual((cats, docs), (set(), set()))
        verdicts, _ = sr.bucket_verdicts({}, events)
        self.assertEqual(verdicts, {})


if __name__ == "__main__":
    unittest.main()
