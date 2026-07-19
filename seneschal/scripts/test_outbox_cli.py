#!/usr/bin/env python3
"""Tests for the outbox CLI (``outbox.py``) — the verbs the assistant's ack/med paths and the drain
loop call.

Thin wrapper over ``outbox_common`` (exhaustively tested in ``test_outbox_common``), so these cover the
arg surface + the enqueue→pull→mark→status roundtrip a real drain performs. Stdlib ``unittest``.

Run:  python -m unittest seneschal.scripts.test_outbox_cli   (or)   python test_outbox_cli.py
"""
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from datetime import timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import outbox as cli  # noqa: E402

RID = "00000000-0000-0000-0000-feedfacecafe"  # placeholder ⏰ page id
DATE = "2026-07-14"


class CLIBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def run_cli(self, *argv):
        """Invoke the CLI with the shared --state-dir; return (rc, parsed-json-or-raw-text)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--state-dir", self.dir, *argv])
        out = buf.getvalue().strip()
        try:
            return rc, json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            return rc, out


class AckAndDedup(CLIBase):
    def test_ack_enqueues_then_dedups(self):
        rc, a = self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        self.assertEqual(rc, 0)
        self.assertTrue(a["created"])
        rc, b = self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        self.assertFalse(b["created"])           # same row, same day → no-op
        self.assertEqual(a["id"], b["id"])

    def test_ack_payload_is_flushable(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        _, pulled = self.run_cli("pull")
        entry = pulled["claimed"][0]
        self.assertEqual(entry["op"], "ack_reminder")
        self.assertEqual(entry["payload"]["status"], "Done")
        self.assertEqual(entry["payload"]["last_acknowledged"], DATE)
        self.assertTrue(entry["payload"]["untick_ack"])


class DefaultAckDateOwnerTz(CLIBase):
    """A bare ``ack`` (no ``--ack-date``) stamps the **owner's** calendar date via ``tz_common`` — so the
    outbox idempotency key and the ``acks.json`` ledger agree on "today" (house rule: date/day-boundary
    logic uses the owner's timezone, never UTC)."""

    def test_default_date_uses_owner_zone(self):
        import tz_common
        tz_common._reset()
        self.addCleanup(tz_common._reset)
        plus14 = timezone(timedelta(hours=14))
        with unittest.mock.patch.object(tz_common, "load_identity",
                                        return_value={"owner": {"timezone": "Etc/GMT-14"}}), \
                unittest.mock.patch("zoneinfo.ZoneInfo", lambda key: plus14):
            expected = tz_common.local_today()          # the owner-zone date, not the machine's
            self.run_cli("ack", "--reminder-id", RID)
        _, pulled = self.run_cli("pull")
        self.assertEqual(pulled["claimed"][0]["payload"]["last_acknowledged"], expected)

    def test_fallback_without_tz_common_is_machine_local(self):
        from datetime import datetime
        before = datetime.now().astimezone().strftime("%Y-%m-%d")
        with unittest.mock.patch.object(cli, "_tz_common", None):
            got = cli._default_ack_date()
        after = datetime.now().astimezone().strftime("%Y-%m-%d")
        self.assertIn(got, {before, after})             # midnight-race tolerant


class MedLog(CLIBase):
    def test_medlog_mints_intent_and_dedups_on_repeat(self):
        rc, r = self.run_cli("medlog", "--collection", "col", "--name", "Medication A",
                             "--dose-mg", "200", "--taken-at", "2026-07-14T08:05:00-05:00", "--set", "usual-am")
        self.assertEqual(rc, 0)
        self.assertTrue(r["created"])
        self.assertIn("intent", r)
        # replaying the SAME intent is idempotent (a retried enqueue, not a new dose)
        _, again = self.run_cli("medlog", "--collection", "col", "--name", "Medication A",
                                "--dose-mg", "200", "--taken-at", "2026-07-14T08:05:00-05:00",
                                "--intent", r["intent"])
        self.assertFalse(again["created"])


class DrainRoundtrip(CLIBase):
    def test_pull_then_mark_done(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        _, pulled = self.run_cli("pull")
        eid = pulled["claimed"][0]["id"]
        rc, marked = self.run_cli("mark", "--id", eid, "--done", "--notion-page-id", "row-1")
        self.assertEqual(marked["status"], "done")
        _, st = self.run_cli("status", "--json")
        self.assertEqual(st["counts"]["done"], 1)
        self.assertEqual(st["backlog"], 0)
        self.assertEqual(self.run_cli("pull")[1]["claimed"], [])   # nothing left to flush

    def test_mark_retry_sets_backoff(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        eid = self.run_cli("pull")[1]["claimed"][0]["id"]
        _, marked = self.run_cli("mark", "--id", eid, "--retry", "--error", "429", "--retry-after", "30")
        self.assertEqual(marked["status"], "pending")
        self.assertIsNotNone(marked["not_before"])

    def test_dead_letter_requires_error_and_surfaces_in_status(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        eid = self.run_cli("pull")[1]["claimed"][0]["id"]
        rc, bad = self.run_cli("mark", "--id", eid, "--dead-letter")
        self.assertFalse(bad["ok"])                                # --error mandatory
        rc, ok = self.run_cli("mark", "--id", eid, "--dead-letter", "--error", "404 gone")
        self.assertEqual(ok["status"], "failed")
        _, st = self.run_cli("status", "--json")
        self.assertEqual(len(st["dead_letters"]), 1)

    def test_mark_unknown_id_reports_error(self):
        rc, r = self.run_cli("mark", "--id", "nope", "--done")
        self.assertFalse(r["ok"])


class GenericAndHousekeeping(CLIBase):
    def test_generic_enqueue_runlog_finalize(self):
        payload = json.dumps({"run_log_id": "rl-1", "run_status": "Success", "items_surfaced": 3})
        rc, r = self.run_cli("enqueue", "--op", "run_log_finalize", "--target-kind", "page",
                             "--target-id", "rl-1", "--idempotency-key", "runlog-final:rl1",
                             "--payload", payload)
        self.assertEqual(rc, 0)
        self.assertTrue(r["created"])

    def test_generic_enqueue_rejects_bad_op(self):
        rc, r = self.run_cli("enqueue", "--op", "run_log_finalize", "--target-kind", "page",
                             "--target-id", "x", "--idempotency-key", "k", "--payload", "{not json")
        self.assertFalse(r["ok"])                                  # bad JSON payload caught

    def test_status_human_form_runs(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        rc, text = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("pending", text)

    def test_prune_smoke(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        eid = self.run_cli("pull")[1]["claimed"][0]["id"]
        self.run_cli("mark", "--id", eid, "--done")
        rc, r = self.run_cli("prune", "--days", "14")
        self.assertEqual(rc, 0)
        self.assertEqual(r["pruned"], 0)                           # fresh DONE isn't old enough


if __name__ == "__main__":
    unittest.main()
