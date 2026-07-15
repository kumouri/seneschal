#!/usr/bin/env python3
"""Tests for the durable **ack ledger** + the ``check_reminders`` fire-time ack gate.

Stdlib ``unittest`` only (matches the Markdown-skill core; CI byte-compiles Python and this runs green
under ``python -m unittest``). Covers the 2026-07-10 fix: a reminder acked in chat must be **suppressed
at fire time** — even a nudge staggered before the ack, or a soft-digest the per-id dequeue can't reach —
because delivery now gates on a durable local ledger (``state/acks.json``), not the volatile warm session
or a snapshot baked at 08:00. Same drop-not-defer contract as the quiet gate.

Run:  python -m unittest seneschal.scripts.test_reminders_acks   (or)   python test_reminders_acks.py
"""
import io
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import reminders_acks as ra  # noqa: E402
import reminders_dequeue as dq  # noqa: E402
import sentinel as sn  # noqa: E402

NOW = datetime(2026, 7, 10, 15, 0, 0, tzinfo=timezone.utc)  # a Friday mid-morning CT
TODAY = ra.local_today(NOW)          # host-consistent "today" (via tz_common: configured owner zone, else machine-local)
YESTERDAY = ra.local_today(NOW - timedelta(days=1))
RID = "00000000-0000-0000-0000-000000000002"  # a ⏰ page-id placeholder (morning walk), with dashes


def _z(dt):
    return dt.isoformat().replace("+00:00", "Z")


class LedgerUnit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_norm_key_is_dash_and_case_insensitive(self):
        self.assertEqual(ra.norm_key("12AB-34CD"), ra.norm_key("12ab34cd"))
        self.assertEqual(ra.norm_key(None), "")  # non-strings never match

    def test_absent_ledger_reads_empty(self):
        self.assertEqual(ra.load_acks(self.dir), {})

    def test_malformed_ledger_fails_open(self):
        with open(ra.acks_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("not json{")
        self.assertEqual(ra.load_acks(self.dir), {})  # broken file must never *suppress* a nudge

    def test_record_then_read(self):
        rec = ra.record_ack(self.dir, RID, TODAY)
        self.assertEqual(rec["date"], TODAY)
        self.assertEqual(ra.load_acks(self.dir)[ra.norm_key(RID)], TODAY)

    def test_record_empty_id_is_noop(self):
        self.assertIsNone(ra.record_ack(self.dir, "   ", TODAY))
        self.assertEqual(ra.load_acks(self.dir), {})

    def test_prune_drops_old_rows(self):
        ra.record_ack(self.dir, RID, YESTERDAY)
        ra.record_ack(self.dir, "other-row", TODAY)
        self.assertEqual(ra.prune_acks(self.dir, keep_date=TODAY), 1)
        self.assertNotIn(ra.norm_key(RID), ra.load_acks(self.dir))


class EntryAckedPredicate(unittest.TestCase):
    def test_single_row_acked_today_is_gated_dash_insensitive(self):
        acks = {ra.norm_key(RID): TODAY}
        self.assertTrue(ra.entry_acked({"reminder_id": RID.replace("-", "")}, acks, TODAY))

    def test_yesterday_ack_does_not_gate_today(self):
        acks = {ra.norm_key(RID): YESTERDAY}
        self.assertFalse(ra.entry_acked({"reminder_id": RID}, acks, TODAY))

    def test_no_keys_never_gated(self):
        self.assertFalse(ra.entry_acked({"text": "ad-hoc, no row"}, {}, TODAY))

    def test_ack_gate_false_opts_out(self):
        acks = {ra.norm_key(RID): TODAY}
        self.assertFalse(ra.entry_acked({"reminder_id": RID, "ack_gate": False}, acks, TODAY))

    def test_digest_gated_only_when_all_members_acked(self):
        entry = {"text": "soft digest", "member_reminder_ids": ["walk", "teeth", "shoulders"]}
        partial = {ra.norm_key("walk"): TODAY, ra.norm_key("teeth"): TODAY}
        self.assertFalse(ra.entry_acked(entry, partial, TODAY))  # shoulders still open → still fires
        full = {**partial, ra.norm_key("shoulders"): TODAY}
        self.assertTrue(ra.entry_acked(entry, full, TODAY))


class LocalTodayOwnerTz(unittest.TestCase):
    """local_today delegates to tz_common: the configured owner zone decides the date, not the
    machine clock — the fix for a daemon hosted outside the owner's timezone."""

    def test_configured_zone_shifts_the_date(self):
        import tz_common
        tz_common._reset()
        self.addCleanup(tz_common._reset)
        # NOW = 2026-07-10T15:00Z. In a +14:00 zone the owner's calendar already reads 07-11.
        plus14 = timezone(timedelta(hours=14))
        with unittest.mock.patch.object(tz_common, "load_identity",
                                        return_value={"owner": {"timezone": "Etc/GMT-14"}}), \
                unittest.mock.patch("zoneinfo.ZoneInfo", lambda key: plus14):
            self.assertEqual(ra.local_today(NOW), "2026-07-11")

    def test_unconfigured_matches_machine_local(self):
        import tz_common
        tz_common._reset()
        self.addCleanup(tz_common._reset)
        with unittest.mock.patch.object(tz_common, "load_identity", return_value={}):
            self.assertEqual(ra.local_today(NOW), NOW.astimezone().strftime("%Y-%m-%d"))


class CheckRemindersAckGate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig = sn.send_telegram
        self.sent = []
        sn.send_telegram = lambda text, env: (self.sent.append(text) or {"ok": True})

    def tearDown(self):
        sn.send_telegram = self._orig

    def _write(self, rows):
        sn.save_json(os.path.join(self.dir, "reminders.json"), rows)

    def _rows(self):
        return sn.load_json(os.path.join(self.dir, "reminders.json"), [])

    def test_acked_row_is_suppressed_unacked_fires(self):
        ra.record_ack(self.dir, RID, TODAY)
        self._write([
            {"id": "walk", "text": "Morning walk.", "due_at": _z(NOW), "channel": "telegram",
             "reminder_id": RID, "fired_at": None},
            {"id": "lunch", "text": "Eat lunch.", "due_at": _z(NOW), "channel": "telegram",
             "reminder_id": "unrelated-row", "fired_at": None},
        ])
        signals = sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        kinds = {s["id"]: s["kind"] for s in signals}
        self.assertEqual(kinds["walk"], "reminder_suppressed_ack")
        self.assertEqual(kinds["lunch"], "reminder_fired")
        rows = {r["id"]: r for r in self._rows()}
        self.assertTrue(rows["walk"].get("acked_at"))       # consumed…
        self.assertIsNone(rows["walk"].get("fired_at"))     # …never delivered
        self.assertNotIn("walk", " ".join(self.sent).lower())
        self.assertIn("Eat lunch.", " ".join(self.sent))

    def test_suppressed_ack_entry_not_redelivered_later(self):
        ra.record_ack(self.dir, RID, TODAY)
        self._write([{"id": "walk", "text": "Morning walk.", "due_at": _z(NOW), "channel": "telegram",
                      "reminder_id": RID, "fired_at": None}])
        sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")          # suppress
        later = sn.check_reminders(self.dir, NOW + timedelta(hours=2), fire=True, telegram_env="x")
        self.assertEqual(later, [])          # dropped, not deferred — no late buzz
        self.assertEqual(self.sent, [])

    def test_roll_entry_still_fires_after_ack(self):
        # A same-day multi-fire roll (ack_gate=False) must keep pinging even once acked today.
        ra.record_ack(self.dir, RID, TODAY)
        self._write([{"id": "alex-11", "text": "Check Alex.", "due_at": _z(NOW), "channel": "telegram",
                      "reminder_id": RID, "ack_gate": False, "fired_at": None}])
        signals = sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        self.assertEqual(signals[0]["kind"], "reminder_fired")
        self.assertEqual(len(self.sent), 1)

    def test_digest_suppressed_only_when_every_member_acked(self):
        self._write([{"id": "soft", "text": "Soft digest: walk / teeth / shoulders.",
                      "due_at": _z(NOW), "channel": "telegram",
                      "member_reminder_ids": ["walk-row", "teeth-row", "shoulders-row"], "fired_at": None}])
        ra.record_ack(self.dir, "walk-row", TODAY)
        ra.record_ack(self.dir, "teeth-row", TODAY)
        self.assertEqual(sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")[0]["kind"],
                         "reminder_fired")           # shoulders still open → digest fires
        # now ack the last member; a fresh identical digest is fully covered and is suppressed
        self._write([{"id": "soft2", "text": "Soft digest: walk / teeth / shoulders.",
                      "due_at": _z(NOW), "channel": "telegram",
                      "member_reminder_ids": ["walk-row", "teeth-row", "shoulders-row"], "fired_at": None}])
        ra.record_ack(self.dir, "shoulders-row", TODAY)
        self.assertEqual(sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")[0]["kind"],
                         "reminder_suppressed_ack")

    def test_no_ledger_fires_normally(self):
        self._write([{"id": "walk", "text": "Morning walk.", "due_at": _z(NOW), "channel": "telegram",
                      "reminder_id": RID, "fired_at": None}])
        signals = sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        self.assertEqual(signals[0]["kind"], "reminder_fired")


class DequeueRecordsAck(unittest.TestCase):
    """The chat/slot ack path calls reminders_dequeue.py; it must ALSO stamp the ledger."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _run(self, argv):
        old = sys.argv
        sys.argv = ["reminders_dequeue.py", "--state-dir", self.dir] + argv
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = dq.main()
            return rc, buf.getvalue()
        finally:
            sys.argv = old

    def test_dequeue_records_ack_even_with_nothing_queued(self):
        rc, _ = self._run(["--reminder-id", RID, "--ack-date", TODAY])
        self.assertEqual(rc, 0)
        self.assertEqual(ra.load_acks(self.dir).get(ra.norm_key(RID)), TODAY)

    def test_no_ack_record_skips_the_ledger(self):
        rc, _ = self._run(["--reminder-id", RID, "--no-ack-record"])
        self.assertEqual(rc, 0)
        self.assertEqual(ra.load_acks(self.dir), {})


class QueueLock(unittest.TestCase):
    """The cross-process reminders.json writer lock (asyncio-daemon era: check_reminders in a worker
    thread can overlap a chat turn's dequeue subprocess). Fail-open by doctrine — a wedged lock must
    degrade to the old race, never to silenced reminders."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, ra.QUEUE_LOCK_FILE)

    def test_lock_held_inside_released_after(self):
        with ra.queue_lock(self.dir):
            self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_second_acquirer_fails_open_and_does_not_steal(self):
        with ra.queue_lock(self.dir):
            # A contender with a tiny timeout proceeds unlocked (fail-open) and must NOT remove the
            # holder's live lock file on its way out.
            with ra.queue_lock(self.dir, timeout=0.2):
                self.assertTrue(os.path.exists(self.path))
            self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_stale_lock_is_stolen(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("99999")  # a holder that crashed long ago
        old = 200.0
        os.utime(self.path, (os.path.getmtime(self.path) - old, os.path.getmtime(self.path) - old))
        with ra.queue_lock(self.dir, timeout=1.0, stale_sec=120.0):
            self.assertTrue(os.path.exists(self.path))  # ours now (stolen + re-created)
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
