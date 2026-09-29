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
import reminders_acks as ra  # noqa: E402 — norm_key: the ack ledger's key shape, one of the two sources
                             # the supersession guard consults

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

    def test_task_status_is_a_known_op(self):
        self.assertIn(ob.TASK_STATUS, ob.OPS)
        self.assertEqual(ob.TASK_STATUS, "task_status")

    def test_task_status_key_is_dash_and_case_insensitive_on_the_page_id(self):
        self.assertEqual(ob.task_status_key(RID, "Done"),
                         ob.task_status_key(RID.replace("-", "").upper(), "Done"))

    def test_task_status_key_scoped_per_target_status(self):
        self.assertNotEqual(ob.task_status_key(RID, "Done"), ob.task_status_key(RID, "Archived"))


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

    def test_failed_revive_resets_created_at_for_backlog_age(self):
        """The false-nudge bug: a dead-lettered entry revived hours later must measure its backlog age
        from the revival, not the original create — otherwise a seconds-old intent reads as hours stale
        and fires a false backlog nudge (the daemon's backlog alarm reads `stats()`'s
        `oldest_pending_age_sec`)."""
        key = ob.ack_key(RID, "2026-07-14")
        r = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), key, now=NOW)
        ob.mark_failed(self.conn, r["id"], "permanent 404", now=NOW)
        revive_at = NOW + timedelta(hours=5)
        again = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), key, now=revive_at)
        self.assertTrue(again["revived"])
        got = ob.get(self.conn, r["id"])
        self.assertEqual(got["created_at"], ob.iso(revive_at))
        self.assertEqual(got["revived_from"], ob.iso(NOW))          # old create time kept, not lost
        s = ob.stats(self.conn, now=revive_at + timedelta(seconds=1))
        self.assertAlmostEqual(s["oldest_pending_age_sec"], 1.0, delta=0.01)

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


class DeadLetterClassification(unittest.TestCase):
    """A 4xx is not on its own evidence the TARGET is unwritable — it is equally evidence the CALLER
    sent the wrong id/shape (a data_source_id passed as a database_id 404s regardless of the row's
    actual state). ``mark_failed``'s
    ``status_code``/``kind`` must classify that distinction rather than pooling every dead-letter as
    ``DEAD_LETTER_PERMANENT`` the way the old undifferentiated rule did."""

    def setUp(self):
        self.conn = ob.connect(tempfile.mkdtemp())

    def tearDown(self):
        self.conn.close()

    def _entry(self, key="k"):
        return ob.enqueue(self.conn, "med_log", "db", "col", {"name": "x"}, ob.medlog_key(key), now=NOW)

    def test_classify_dead_letter_400_and_404_are_caller_suspect(self):
        self.assertEqual(ob.classify_dead_letter(400), ob.DEAD_LETTER_CALLER_SUSPECT)
        self.assertEqual(ob.classify_dead_letter(404), ob.DEAD_LETTER_CALLER_SUSPECT)

    def test_classify_dead_letter_other_codes_are_permanent(self):
        self.assertEqual(ob.classify_dead_letter(403), ob.DEAD_LETTER_PERMANENT)
        self.assertEqual(ob.classify_dead_letter(410), ob.DEAD_LETTER_PERMANENT)

    def test_classify_dead_letter_no_code_is_unclassified_never_permanent(self):
        """The bug this exists to fix: 'no status code' must never be silently read as 'confirmed
        permanent' — that silent promotion is how a caller-shaped 404 read as an unfixable target."""
        self.assertEqual(ob.classify_dead_letter(None), ob.DEAD_LETTER_UNCLASSIFIED)

    def test_mark_failed_404_tags_caller_suspect(self):
        r = self._entry()
        ob.claim_ready(self.conn, now=NOW)
        ob.mark_failed(self.conn, r["id"], "object_not_found", now=NOW, status_code=404)
        got = ob.get(self.conn, r["id"])
        self.assertEqual(got["status"], ob.FAILED)                       # still stops retrying
        self.assertEqual(got["dead_letter_kind"], ob.DEAD_LETTER_CALLER_SUSPECT)

    def test_mark_failed_403_tags_permanent(self):
        r = self._entry()
        ob.claim_ready(self.conn, now=NOW)
        ob.mark_failed(self.conn, r["id"], "restricted_resource", now=NOW, status_code=403)
        self.assertEqual(ob.get(self.conn, r["id"])["dead_letter_kind"], ob.DEAD_LETTER_PERMANENT)

    def test_mark_failed_with_no_status_code_is_unclassified(self):
        r = self._entry()
        ob.claim_ready(self.conn, now=NOW)
        ob.mark_failed(self.conn, r["id"], "boom")
        self.assertEqual(ob.get(self.conn, r["id"])["dead_letter_kind"], ob.DEAD_LETTER_UNCLASSIFIED)

    def test_mark_retry_exhaustion_tags_retries_exhausted_not_caller_suspect(self):
        """An exhausted retry budget is a real dead-letter reason of its own — it must not be confused
        with, or accidentally classified as, a caller-shaped 4xx just because no status code was
        passed to this path."""
        r = self._entry()
        t = NOW
        for _ in range(ob.MAX_ATTEMPTS):
            ob.claim_ready(self.conn, now=t)
            ob.mark_retry(self.conn, r["id"], "429 upstream", now=t, rand=lambda: 1.0)
            t += timedelta(hours=2)
        self.assertEqual(ob.get(self.conn, r["id"])["dead_letter_kind"], ob.DEAD_LETTER_RETRY_EXHAUSTED)


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


class Supersession(unittest.TestCase):
    """The ordering hazard — a stale queued ack must never overwrite a newer one.

    **The failure shape this pins is not a thought experiment.** A queue that stops draining holds
    un-landed `ack_reminder` entries dated yesterday while the ⏰ rows have since been acked *by hand in
    chat* for today. Replaying them in FIFO order writes yesterday's `Last Acknowledged` over today's —
    and that field is what the re-fire and interval logic read, so the repair re-arms reminders the
    owner has already answered."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = ob.connect(self.dir)

    def tearDown(self):
        self.conn.close()

    def _ack(self, date, key=None, rid=RID):
        payload = dict(_ack_payload(), reminder_id=rid, last_acknowledged=date)
        return ob.enqueue(self.conn, "ack_reminder", "page", rid, payload,
                          key or ob.ack_key(rid, date), now=NOW)

    # --- the primitives ---
    def test_entry_ack_date_reads_dated_ops_only(self):
        e = ob.get(self.conn, self._ack("2026-08-06")["id"])
        self.assertEqual(ob.entry_ack_date(e), "2026-08-06")
        med = ob.enqueue(self.conn, "med_log", "db", "col", {"name": "x"}, "medlog:1", now=NOW)
        self.assertIsNone(ob.entry_ack_date(ob.get(self.conn, med["id"])))

    def test_entry_ack_date_is_total_on_junk(self):
        for junk in (None, {}, {"op": "ack_reminder"}, {"op": "ack_reminder", "payload": "not-a-dict"},
                     {"op": "ack_reminder", "payload": {"last_acknowledged": 20260806}}):
            self.assertIsNone(ob.entry_ack_date(junk))

    def test_landed_ack_ignores_pending_and_superseded_rows(self):
        self._ack("2026-08-07")                       # pending — proves nothing landed
        self.assertIsNone(ob.latest_landed_ack(self.conn, RID))
        old = self._ack("2026-08-05")
        ob.mark_superseded(self.conn, old["id"], "2026-08-07", now=NOW)  # done, but never written
        self.assertIsNone(ob.latest_landed_ack(self.conn, RID))
        landed = self._ack("2026-08-06")
        ob.mark_done(self.conn, landed["id"], now=NOW)
        self.assertEqual(ob.latest_landed_ack(self.conn, RID), "2026-08-06")

    def test_landed_ack_matches_ids_across_dash_and_case_forms(self):
        landed = self._ack("2026-08-06")
        ob.mark_done(self.conn, landed["id"], now=NOW)
        self.assertEqual(ob.latest_landed_ack(self.conn, RID.replace("-", "").upper()), "2026-08-06")

    # --- the guard itself ---
    def test_a_ledger_newer_than_the_queued_entry_supersedes_it(self):
        entry = ob.get(self.conn, self._ack("2026-08-06")["id"])
        ledger = {ra.norm_key(RID): "2026-08-07"}
        self.assertEqual(ob.superseding_date(self.conn, entry, ledger), "2026-08-07")

    def test_equal_date_is_not_superseded(self):
        """Replaying the same date converges on the same row state — harmless, and letting it through
        keeps the guard from swallowing a genuinely un-landed same-day ack."""
        entry = ob.get(self.conn, self._ack("2026-08-07")["id"])
        self.assertIsNone(ob.superseding_date(self.conn, entry, {ra.norm_key(RID): "2026-08-07"}))

    def test_older_ledger_never_supersedes(self):
        entry = ob.get(self.conn, self._ack("2026-08-07")["id"])
        self.assertIsNone(ob.superseding_date(self.conn, entry, {ra.norm_key(RID): "2026-08-05"}))

    def test_no_evidence_means_replay(self):
        """Fail-open in the one direction that matters: a missing/empty ledger vetoes nothing, so an
        absent `acks.json` can never *block* a flush — it can only ever stop a backwards one."""
        entry = ob.get(self.conn, self._ack("2026-08-06")["id"])
        for ledger in (None, {}, {"someone-else": "2026-08-09"}, {ra.norm_key(RID): 20260809}):
            self.assertIsNone(ob.superseding_date(self.conn, entry, ledger))

    def test_a_newer_landed_entry_supersedes_without_a_ledger(self):
        newer = self._ack("2026-08-07")
        ob.mark_done(self.conn, newer["id"], now=NOW)
        entry = ob.get(self.conn, self._ack("2026-08-06")["id"])
        self.assertEqual(ob.superseding_date(self.conn, entry, {}), "2026-08-07")

    def test_another_rows_ack_is_not_this_rows_evidence(self):
        other = "00000000-0000-0000-0000-00000000beef"
        landed = self._ack("2026-08-09", rid=other)
        ob.mark_done(self.conn, landed["id"], now=NOW)
        entry = ob.get(self.conn, self._ack("2026-08-06")["id"])
        self.assertIsNone(ob.superseding_date(self.conn, entry, {ra.norm_key(other): "2026-08-09"}))

    # --- the sweep ---
    def test_sweep_retires_stale_and_keeps_current(self):
        stale = self._ack("2026-08-06")
        current = self._ack("2026-08-07")
        resolved = ob.resolve_superseded(self.conn, {ra.norm_key(RID): "2026-08-07"}, now=NOW)
        self.assertEqual([r["id"] for r in resolved], [stale["id"]])
        done = ob.get(self.conn, stale["id"])
        self.assertEqual(done["status"], ob.DONE)
        self.assertEqual(done["resolution"], ob.SUPERSEDED)
        self.assertIn("2026-08-07", done["last_error"])
        self.assertEqual(ob.get(self.conn, current["id"])["status"], ob.PENDING)

    def test_sweep_leaves_inflight_alone(self):
        """An inflight entry is claimed by a drainer that may be mid-write; stealing it would race the
        very write we're reasoning about. It comes back on the stale-claim reclaim."""
        self._ack("2026-08-06")
        ob.claim_ready(self.conn, now=NOW)
        resolved = ob.resolve_superseded(self.conn, {ra.norm_key(RID): "2026-08-07"}, now=NOW)
        self.assertEqual(resolved, [])

    def test_sweep_never_touches_a_med_log(self):
        """A create carries no date and converges on nothing — it must always be replayed."""
        med = ob.enqueue(self.conn, "med_log", "db", "col", {"name": "x"}, "medlog:1", now=NOW)
        self.assertEqual(ob.resolve_superseded(self.conn, {ra.norm_key("col"): "2026-08-09"}), [])
        self.assertEqual(ob.get(self.conn, med["id"])["status"], ob.PENDING)

    def test_superseded_entry_is_not_claimable(self):
        """End to end: after the sweep, a drainer asking for work is handed nothing stale."""
        self._ack("2026-08-06")
        ob.resolve_superseded(self.conn, {ra.norm_key(RID): "2026-08-07"}, now=NOW)
        self.assertEqual(ob.claim_ready(self.conn, now=NOW), [])

    def test_migration_adds_resolution_to_a_pre_existing_store(self):
        """A live store predates this column. `connect` must add it without touching any row."""
        self.conn.execute("ALTER TABLE outbox RENAME TO outbox_old")
        self.conn.execute(
            "CREATE TABLE outbox (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, "
            "op TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL, "
            "payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
            "not_before TEXT, created_at TEXT NOT NULL, last_attempt_at TEXT, last_error TEXT, "
            "notion_page_id TEXT)")
        self.conn.execute("INSERT INTO outbox SELECT id, idempotency_key, op, target_kind, target_id, "
                          "payload, status, attempts, not_before, created_at, last_attempt_at, "
                          "last_error, notion_page_id FROM outbox_old")
        self.conn.commit()
        self.conn.close()
        conn = ob.connect(self.dir)
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(outbox)")}
            self.assertIn("resolution", cols)
        finally:
            conn.close()
            self.conn = ob.connect(self.dir)  # tearDown closes it

    def test_migration_adds_dead_letter_kind_to_a_pre_existing_store(self):
        """A live store predates ``dead_letter_kind`` too. `connect`
        must add it without touching any row, same shape as the ``resolution`` migration above."""
        self.conn.execute("ALTER TABLE outbox RENAME TO outbox_old")
        self.conn.execute(
            "CREATE TABLE outbox (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, "
            "op TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL, "
            "payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
            "not_before TEXT, created_at TEXT NOT NULL, last_attempt_at TEXT, last_error TEXT, "
            "notion_page_id TEXT, resolution TEXT, revived_from TEXT)")
        self.conn.execute("INSERT INTO outbox SELECT id, idempotency_key, op, target_kind, target_id, "
                          "payload, status, attempts, not_before, created_at, last_attempt_at, "
                          "last_error, notion_page_id, resolution, revived_from FROM outbox_old")
        self.conn.commit()
        self.conn.close()
        conn = ob.connect(self.dir)
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(outbox)")}
            self.assertIn("dead_letter_kind", cols)
        finally:
            conn.close()
            self.conn = ob.connect(self.dir)  # tearDown closes it


class Retraction(unittest.TestCase):
    """The third terminal state: a write that must NOT happen, cancelled on purpose.

    **The gap this closes is a real one.** A breakfast report auto-acks the lunch reminder; the ack is
    caught and pulled before it ever reaches Notion. The store had only two resting places for it:
    `done` (claiming a Notion write that never occurred) or a dead-letter (which means *permanent
    failure, come look*, and nudges the owner once a day forever). A daily false alarm is how a real
    dead-letter gets ignored."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = ob.connect(self.dir)

    def tearDown(self):
        self.conn.close()

    def _pending(self, key="k1", payload=None):
        return ob.enqueue(self.conn, "ack_reminder", "page", RID,
                          payload or _ack_payload(), key, now=NOW)

    def test_retract_resolves_without_a_write_and_records_the_reason(self):
        e = self._pending()
        r = ob.mark_retracted(self.conn, e["id"], "auto-acked off a breakfast report", now=NOW)
        self.assertEqual(r["status"], ob.DONE)                 # rests in done; the queue can't wedge
        self.assertEqual(r["resolution"], ob.RETRACTED)
        self.assertIn("auto-acked off a breakfast report", r["last_error"])
        self.assertEqual(ob.claim_ready(self.conn, now=NOW + timedelta(hours=1)), [])

    def test_a_retracted_entry_is_not_a_dead_letter(self):
        """The whole point: it must stop nudging. `stats` is what the daemon's alarm reads."""
        e = self._pending()
        ob.mark_retracted(self.conn, e["id"], "cancelled", now=NOW)
        s = ob.stats(self.conn, now=NOW)
        self.assertEqual(s["counts"][ob.FAILED], 0)
        self.assertEqual(s["dead_letters"], [])
        self.assertEqual(s["backlog"], 0)
        self.assertEqual(s["retracted"], 1)                    # …but still VISIBLE, not erased

    def test_a_dead_letter_can_be_retracted(self):
        """The path for the row that motivated this: it is already dead-lettered today."""
        e = self._pending()
        ob.claim_ready(self.conn, now=NOW)
        ob.mark_failed(self.conn, e["id"], "boom", now=NOW)
        r = ob.mark_retracted(self.conn, e["id"], "never should have been queued", now=NOW)
        self.assertEqual(r["resolution"], ob.RETRACTED)
        self.assertEqual(ob.stats(self.conn, now=NOW)["counts"][ob.FAILED], 0)

    def test_retraction_requires_a_reason(self):
        e = self._pending()
        for bad in (None, "", "   "):
            with self.assertRaises(ValueError):
                ob.mark_retracted(self.conn, e["id"], bad, now=NOW)
        self.assertIsNone(ob.get(self.conn, e["id"])["resolution"])   # and nothing was touched

    def test_a_landed_write_cannot_be_retracted(self):
        """It is in Notion. Calling it retracted here would make the store disagree with the record."""
        e = self._pending()
        ob.mark_done(self.conn, e["id"], now=NOW)
        with self.assertRaises(ValueError):
            ob.mark_retracted(self.conn, e["id"], "too late", now=NOW)
        self.assertIsNone(ob.get(self.conn, e["id"])["resolution"])

    def test_unknown_id_returns_none(self):
        self.assertIsNone(ob.mark_retracted(self.conn, "nope", "reason", now=NOW))

    def test_a_retracted_key_re_arms_on_a_fresh_intent(self):
        """**The one that would have made this a regression.** The ack key is day-scoped
        (`ack:<row>:<date>`), so retracting a morning ack must not swallow the real afternoon one."""
        key = ob.ack_key(RID, "2026-07-14")
        first = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), key, now=NOW)
        ob.mark_retracted(self.conn, first["id"], "wrong trigger", now=NOW)
        again = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), key,
                           now=NOW + timedelta(hours=4))
        self.assertTrue(again["revived"])
        self.assertEqual(again["id"], first["id"])
        row = ob.get(self.conn, first["id"])
        self.assertEqual(row["status"], ob.PENDING)
        self.assertIsNone(row["resolution"])                   # no longer cancelled
        self.assertIsNone(row["last_error"])
        self.assertEqual(len(ob.claim_ready(self.conn, now=NOW + timedelta(hours=4))), 1)

    def test_the_revived_entry_carries_the_new_payload_never_the_retracted_one(self):
        """A retraction is a claim that a SPECIFIC write must not happen — not that the key is dead.
        The re-armed row must drain with the fresh payload; the retracted one must never be what
        lands, even though it shares the row's id."""
        key = ob.ack_key(RID, "2026-07-14")
        wrong = ob.enqueue(self.conn, "ack_reminder", "page", RID,
                           dict(_ack_payload(), status="Finished"), key, now=NOW)
        ob.mark_retracted(self.conn, wrong["id"], "wrong row inferred", now=NOW)
        right = ob.enqueue(self.conn, "ack_reminder", "page", RID,
                           dict(_ack_payload(), status="Done"), key, now=NOW + timedelta(hours=1))
        self.assertEqual(right["id"], wrong["id"])              # revived, not a second entry
        self.assertTrue(right["revived"])

        claimed = ob.claim_ready(self.conn, now=NOW + timedelta(hours=1))
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["payload"]["status"], "Done")   # the revived write, not "Finished"

    def test_a_retracted_key_revival_resets_created_at_for_backlog_age(self):
        """An entry created, retracted seconds later, then revived hours later by the real ack is a
        seconds-old intent, not the hours the stale `created_at` would make it measure as. Mirrors
        `Enqueue.test_failed_revive_resets_created_at_for_backlog_age` for the RETRACTED path."""
        key = ob.ack_key(RID, "2026-07-14")
        first = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), key, now=NOW)
        ob.mark_retracted(self.conn, first["id"], "wrong trigger", now=NOW)
        revive_at = NOW + timedelta(hours=5)
        again = ob.enqueue(self.conn, "ack_reminder", "page", RID, _ack_payload(), key, now=revive_at)
        self.assertTrue(again["revived"])
        row = ob.get(self.conn, first["id"])
        self.assertEqual(row["created_at"], ob.iso(revive_at))
        self.assertEqual(row["revived_from"], ob.iso(NOW))
        s = ob.stats(self.conn, now=revive_at + timedelta(seconds=1))
        self.assertAlmostEqual(s["oldest_pending_age_sec"], 1.0, delta=0.01)

    def test_a_retracted_ack_never_counts_as_landed(self):
        """It never reached Notion, so it must not be able to supersede a later, real ack."""
        e = self._pending(payload=dict(_ack_payload(), last_acknowledged="2026-07-20"))
        ob.mark_retracted(self.conn, e["id"], "cancelled", now=NOW)
        self.assertIsNone(ob.latest_landed_ack(self.conn, RID))
        older = ob.enqueue(self.conn, "ack_reminder", "page", RID,
                           dict(_ack_payload(), last_acknowledged="2026-07-14"), "k2", now=NOW)
        self.assertEqual(ob.resolve_superseded(self.conn), [])  # not vetoed by a write that never was
        self.assertEqual(ob.get(self.conn, older["id"])["status"], ob.PENDING)


if __name__ == "__main__":
    unittest.main()
