#!/usr/bin/env python3
"""Tests for the durable **write-behind outbox** store (``outbox_common``).

Stdlib ``unittest`` only (CI byte-compiles Python and runs ``python -m unittest``). Covers the durability
contract the outbox exists for: journal-first enqueue is idempotent, a drainer claims FIFO with
poison-pill attempt-counting, transient failures back off, permanent ones / exhausted budgets
dead-letter, and a dead-lettered entry revives on a fresh intent. See
``../docs/notion-write-behind-outbox-spec.md``.

Run:  python -m unittest seneschal.scripts.test_outbox_common   (or)   python test_outbox_common.py
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import outbox_common as ob  # noqa: E402

NOW = datetime(2026, 7, 14, 15, 0, 0, tzinfo=timezone.utc)
RID = "00000000-0000-0000-0000-feedfacecafe"  # a placeholder ⏰ page id (with dashes)


def _ack_payload():
    return {"reminder_id": RID, "status": "Done", "last_acknowledged": "2026-07-14",
            "consecutive_misses": 0, "untick_ack": True}


class TimeAndKeys(unittest.TestCase):
    def test_iso_roundtrip(self):
        self.assertEqual(ob.parse_iso(ob.iso(NOW)), NOW)

    def test_parse_iso_tolerates_offset_form(self):
        self.assertEqual(ob.parse_iso("2026-07-14T15:00:00+00:00"), NOW)

    def test_ack_key_is_dash_and_case_insensitive(self):
        self.assertEqual(ob.ack_key(RID, "2026-07-14"), ob.ack_key(RID.replace("-", "").upper(), "2026-07-14"))

    def test_ack_key_scoped_per_day(self):
        self.assertNotEqual(ob.ack_key(RID, "2026-07-14"), ob.ack_key(RID, "2026-07-15"))

    def test_medlog_key_unique_per_intent(self):
        self.assertNotEqual(ob.medlog_key(ob.new_intent()), ob.medlog_key(ob.new_intent()))


class Enqueue(unittest.TestCase):
    def setUp(self):
        self.conn = ob.connect(tempfile.mkdtemp())

    def tearDown(self):
        self.conn.close()

    def test_create_then_present(self):
        r = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(),
                       ob.ack_key(RID, "2026-07-14"), now=NOW)
        self.assertTrue(r["created"])
        got = ob.get(self.conn, r["id"])
        self.assertEqual(got["status"], ob.PENDING)
        self.assertEqual(got["attempts"], 0)
        self.assertEqual(got["payload"]["reminder_id"], RID)  # payload deserialized on read

    def test_duplicate_key_is_noop(self):
        key = ob.ack_key(RID, "2026-07-14")
        a = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), key, now=NOW)
        b = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), key, now=NOW)
        self.assertFalse(b["created"])
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(ob.claim_ready(self.conn, now=NOW)), 1)  # only one row exists

    def test_failed_entry_revives_on_reenqueue(self):
        key = ob.ack_key(RID, "2026-07-14")
        r = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), key, now=NOW)
        ob.mark_failed(self.conn, r["id"], "permanent 404", now=NOW)
        again = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), key, now=NOW)
        self.assertFalse(again["created"])
        self.assertTrue(again["revived"])
        got = ob.get(self.conn, r["id"])
        self.assertEqual(got["status"], ob.PENDING)
        self.assertEqual(got["attempts"], 0)
        self.assertIsNone(got["last_error"])

    def test_unknown_op_raises(self):
        with self.assertRaises(ValueError):
            ob.enqueue(self.conn, "delete_everything", "page", RID, {}, "k", now=NOW)

    def test_unknown_target_kind_raises(self):
        with self.assertRaises(ValueError):
            ob.enqueue(self.conn, "ack_reminder", "workspace", RID, {}, "k", now=NOW)

    def test_empty_key_raises(self):
        with self.assertRaises(ValueError):
            ob.enqueue(self.conn, "ack_reminder", "page", RID, {}, "", now=NOW)


class ClaimAndComplete(unittest.TestCase):
    def setUp(self):
        self.conn = ob.connect(tempfile.mkdtemp())

    def tearDown(self):
        self.conn.close()

    def _enqueue(self, key, created_at):
        return ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), key, now=created_at)

    def test_claim_is_fifo_oldest_first(self):
        self._enqueue("k-second", NOW + timedelta(seconds=10))
        self._enqueue("k-first", NOW)
        claimed = ob.claim_ready(self.conn, now=NOW + timedelta(minutes=1))
        self.assertEqual([c["idempotency_key"] for c in claimed], ["k-first", "k-second"])

    def test_claim_increments_attempts_and_sets_inflight(self):
        r = self._enqueue("k", NOW)
        claimed = ob.claim_ready(self.conn, now=NOW)
        self.assertEqual(claimed[0]["attempts"], 1)  # counted BEFORE the write (poison-pill safety)
        self.assertEqual(ob.get(self.conn, r["id"])["status"], ob.INFLIGHT)

    def test_claim_skips_not_before_future(self):
        r = self._enqueue("k", NOW)
        ob.mark_retry(self.conn, ob.claim_ready(self.conn, now=NOW)[0]["id"], "429",
                      now=NOW, rand=lambda: 1.0)  # backs off into the future
        self.assertEqual(ob.claim_ready(self.conn, now=NOW), [])          # not yet eligible
        self.assertEqual(len(ob.claim_ready(self.conn, now=NOW + timedelta(hours=2))), 1)  # later, yes
        self.assertEqual(ob.get(self.conn, r["id"])["attempts"], 2)       # claimed again

    def test_mark_done_records_page_id_and_clears_error(self):
        r = self._enqueue("k", NOW)
        cid = ob.claim_ready(self.conn, now=NOW)[0]["id"]
        ob.mark_retry(self.conn, cid, "transient blip", now=NOW)          # leaves a last_error
        ob.claim_ready(self.conn, now=NOW + timedelta(hours=1))
        done = ob.mark_done(self.conn, cid, notion_page_id="new-page-123", now=NOW + timedelta(hours=1))
        self.assertEqual(done["status"], ob.DONE)
        self.assertEqual(done["notion_page_id"], "new-page-123")
        self.assertIsNone(done["last_error"])

    def test_stale_inflight_is_reclaimed(self):
        r = self._enqueue("k", NOW)
        ob.claim_ready(self.conn, now=NOW)                                 # INFLIGHT @ NOW
        # A drainer that died: nothing marked it. Much later, a fresh claim reclaims + re-claims it.
        again = ob.claim_ready(self.conn, now=NOW + timedelta(seconds=ob.STALE_INFLIGHT_SEC + 60))
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["id"], r["id"])
        self.assertEqual(again[0]["attempts"], 2)


class RetryPolicy(unittest.TestCase):
    def setUp(self):
        self.conn = ob.connect(tempfile.mkdtemp())

    def tearDown(self):
        self.conn.close()

    def test_next_backoff_honors_retry_after(self):
        self.assertEqual(ob.next_backoff(3, retry_after=42), 42.0)

    def test_next_backoff_grows_and_is_bounded_by_jitter(self):
        lo = ob.next_backoff(2, base=5, cap=3600, rand=lambda: 0.0)   # equal-jitter floor = delay/2
        hi = ob.next_backoff(2, base=5, cap=3600, rand=lambda: 1.0)   # ceil = delay
        self.assertEqual(lo, 5.0)   # delay = 5*2^1 = 10 → floor 5
        self.assertEqual(hi, 10.0)
        self.assertLessEqual(ob.next_backoff(99, base=5, cap=3600, rand=lambda: 1.0), 3600.0)  # capped

    def test_mark_retry_backs_off_then_dead_letters_at_budget(self):
        r = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), "k", now=NOW)
        t = NOW
        for _ in range(ob.MAX_ATTEMPTS):        # claim+fail exactly MAX_ATTEMPTS times
            c = ob.claim_ready(self.conn, now=t)
            self.assertEqual(len(c), 1, "should still be retryable")
            ob.mark_retry(self.conn, r["id"], "429 upstream", now=t, rand=lambda: 1.0)
            t += timedelta(hours=2)             # jump past not_before each round
        self.assertEqual(ob.get(self.conn, r["id"])["status"], ob.FAILED)   # budget spent → dead-letter
        self.assertEqual(ob.claim_ready(self.conn, now=t), [])              # dead-letters aren't claimed

    def test_permanent_failure_dead_letters_immediately(self):
        r = ob.enqueue(self.conn, "med_log", "db", "col", {"name": "x"}, ob.medlog_key("i1"), now=NOW)
        ob.claim_ready(self.conn, now=NOW)
        ob.mark_failed(self.conn, r["id"], "404 target gone", now=NOW)
        self.assertEqual(ob.get(self.conn, r["id"])["status"], ob.FAILED)


class StatsAndPrune(unittest.TestCase):
    def setUp(self):
        self.conn = ob.connect(tempfile.mkdtemp())

    def tearDown(self):
        self.conn.close()

    def test_stats_counts_backlog_oldest_and_dead(self):
        ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), "k1", now=NOW)
        r2 = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), "k2",
                        now=NOW + timedelta(minutes=5))
        ob.claim_ready(self.conn, now=NOW + timedelta(minutes=6))
        ob.mark_failed(self.conn, r2["id"], "boom", now=NOW + timedelta(minutes=6))
        s = ob.stats(self.conn, now=NOW + timedelta(minutes=10))
        self.assertEqual(s["counts"][ob.FAILED], 1)
        self.assertEqual(s["backlog"], 1)                       # k1 still pending/inflight
        self.assertEqual(len(s["dead_letters"]), 1)
        self.assertAlmostEqual(s["oldest_pending_age_sec"], 600.0, delta=1.0)  # k1 created 10 min ago

    def test_stats_empty_store(self):
        s = ob.stats(self.conn, now=NOW)
        self.assertEqual(s["backlog"], 0)
        self.assertIsNone(s["oldest_pending_age_sec"])
        self.assertEqual(s["dead_letters"], [])

    def test_prune_drops_old_done_keeps_failed_and_recent(self):
        old = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), "old", now=NOW)
        ob.mark_done(self.conn, old["id"], now=NOW)
        recent = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), "recent",
                            now=NOW + timedelta(days=20))
        ob.mark_done(self.conn, recent["id"], now=NOW + timedelta(days=20))
        dead = ob.enqueue(self.conn, "med_log", "db", "col", {"n": 1}, "dead", now=NOW)
        ob.mark_failed(self.conn, dead["id"], "x", now=NOW)
        removed = ob.prune_done(self.conn, older_than_days=14, now=NOW + timedelta(days=21))
        self.assertEqual(removed, 1)                            # only the 21-day-old DONE
        self.assertIsNone(ob.get(self.conn, old["id"]))
        self.assertIsNotNone(ob.get(self.conn, recent["id"]))  # 1-day-old DONE kept
        self.assertEqual(ob.get(self.conn, dead["id"])["status"], ob.FAILED)  # dead-letter never pruned


if __name__ == "__main__":
    unittest.main()
