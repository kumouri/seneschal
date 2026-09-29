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
import reminders_acks as ra  # noqa: E402 — norm_key: the ack-ledger key shape `pull`'s sweep reads

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


class DeadLetterStatusCode(CLIBase):
    """``--dead-letter --status-code`` must classify a caller-shaped 4xx distinctly rather than let it
    read as an unfixable, permanent Notion-side condition."""

    def _first_id(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        return self.run_cli("pull")[1]["claimed"][0]["id"]

    def test_404_status_code_classifies_as_caller_suspect(self):
        eid = self._first_id()
        rc, ok = self.run_cli("mark", "--id", eid, "--dead-letter", "--error", "object_not_found",
                              "--status-code", "404")
        self.assertEqual(ok["dead_letter_kind"], "caller_error_suspected")
        _, st = self.run_cli("status", "--json")
        self.assertEqual(st["dead_letters"][0]["dead_letter_kind"], "caller_error_suspected")

    def test_403_status_code_classifies_as_permanent(self):
        eid = self._first_id()
        rc, ok = self.run_cli("mark", "--id", eid, "--dead-letter", "--error", "restricted_resource",
                              "--status-code", "403")
        self.assertEqual(ok["dead_letter_kind"], "permanent")

    def test_omitted_status_code_is_unclassified_not_permanent(self):
        """The old behavior (no code at all) must not silently read as confirmed-permanent — that
        silent promotion is the bug."""
        eid = self._first_id()
        rc, ok = self.run_cli("mark", "--id", eid, "--dead-letter", "--error", "boom")
        self.assertEqual(ok["dead_letter_kind"], "unclassified")

    def test_status_human_form_flags_caller_suspect_dead_letters(self):
        eid = self._first_id()
        self.run_cli("mark", "--id", eid, "--dead-letter", "--error", "object_not_found",
                    "--status-code", "404")
        rc, text = self.run_cli("status")
        self.assertIn("LIKELY CALLER BUG", text)


class PullSweepsSupersededFirst(CLIBase):
    """`pull` is where the ordering guard has to live.

    Putting it only in the daemon's sweep would leave the documented in-turn drain (`modes/chat.md`)
    able to receive a stale ack and write it — and "the drainer should check first" is an
    instruction, not a guard. Here a drainer *cannot* be handed a superseded entry, because the same
    call that hands out work retires them."""

    def _ledger(self, date):
        with open(os.path.join(self.dir, "acks.json"), "w", encoding="utf-8") as fh:
            json.dump({ra.norm_key(RID): date}, fh)

    def test_pull_retires_the_stale_ack_and_claims_nothing(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", "2026-08-06")
        self._ledger("2026-08-07")                       # the owner acked it by hand today
        _, pulled = self.run_cli("pull")
        self.assertEqual(pulled["claimed"], [])          # the backwards write is never offered
        self.assertEqual(len(pulled["resolved"]), 1)
        _, st = self.run_cli("status", "--json")
        self.assertEqual(st["backlog"], 0)
        self.assertEqual(st["counts"]["done"], 1)

    def test_pull_still_claims_a_current_ack(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", "2026-08-07")
        self._ledger("2026-08-07")
        _, pulled = self.run_cli("pull")
        self.assertEqual(pulled["resolved"], [])
        self.assertEqual(len(pulled["claimed"]), 1)

    def test_pull_is_unchanged_with_no_ledger_on_disk(self):
        """No `acks.json` at all: the guard proves nothing, so everything replays exactly as before."""
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", "2026-08-06")
        _, pulled = self.run_cli("pull")
        self.assertEqual(pulled["resolved"], [])
        self.assertEqual(len(pulled["claimed"]), 1)

    def test_pull_survives_a_corrupt_ledger(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", "2026-08-06")
        with open(os.path.join(self.dir, "acks.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        _, pulled = self.run_cli("pull")
        self.assertEqual(len(pulled["claimed"]), 1)

    def test_resolve_verb_runs_the_sweep_alone(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", "2026-08-06")
        self._ledger("2026-08-07")
        rc, r = self.run_cli("resolve")
        self.assertEqual(rc, 0)
        self.assertEqual(len(r["resolved"]), 1)
        self.assertIn("2026-08-07", r["resolved"][0]["last_error"])
        self.assertEqual(self.run_cli("resolve")[1]["resolved"], [])   # idempotent — nothing left


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


class DreamRunsThePrune(unittest.TestCase):
    """``outbox.py`` documents ``done`` entries pruned at 14 days via ``prune`` — a promise that is
    only true if something calls it. The CLI verb working (above) never catches a missing caller,
    because nothing enters it from a Dream run, so this asserts over the real ``modes/dream.md``
    prose."""

    def test_dream_prunes_the_outbox_nightly(self):
        path = os.path.join(SCRIPT_DIR, "..", "modes", "dream.md")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("outbox.py prune", body)


class Retract(CLIBase):
    """`outbox.py retract` — the honest terminal state for a write that must not happen."""

    def _first_id(self):
        return self.run_cli("pull")[1]["claimed"][0]["id"]

    def test_retract_clears_the_dead_letter_the_alarm_reads(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        eid = self._first_id()
        self.run_cli("mark", "--id", eid, "--dead-letter", "--error", "queued in error")
        self.assertEqual(self.run_cli("status", "--json")[1]["counts"]["failed"], 1)

        rc, r = self.run_cli("retract", "--id", eid, "--reason", "auto-acked off a breakfast report")
        self.assertEqual(rc, 0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["marked"], "retracted")
        st = self.run_cli("status", "--json")[1]
        self.assertEqual(st["counts"]["failed"], 0)             # the daily false alarm is gone
        self.assertEqual(st["dead_letters"], [])
        self.assertEqual(st["retracted"], 1)                    # and it is still on the record

    def test_retract_accepts_the_short_id_status_prints(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        eid = self._first_id()
        rc, r = self.run_cli("retract", "--id", eid[:8], "--reason", "wrong trigger")
        self.assertEqual(rc, 0)
        self.assertEqual(r["id"], eid)

    def test_unknown_id_fails_loudly(self):
        rc, r = self.run_cli("retract", "--id", "deadbeef", "--reason", "x")
        self.assertEqual(rc, 1)
        self.assertFalse(r["ok"])
        self.assertIn("no entry", r["error"])

    def test_retracting_a_landed_write_is_refused(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        eid = self._first_id()
        self.run_cli("mark", "--id", eid, "--done")
        rc, r = self.run_cli("retract", "--id", eid, "--reason", "too late")
        self.assertEqual(rc, 1)
        self.assertIn("already landed", r["error"])

    def test_status_text_names_the_retraction(self):
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        eid = self._first_id()
        self.run_cli("retract", "--id", eid, "--reason", "cancelled")
        line = self.run_cli("status")[1]
        self.assertIn("1 retracted", line)
        self.assertIn("0 dead-letter", line)

    def test_a_re_ack_after_retraction_revives_and_is_drainable(self):
        """The motivating case at the CLI: a wrongly-slotted ack gets retracted, then the real ack
        for the same row+day must land as a fresh, drainable entry — not a swallowed no-op."""
        self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        eid = self._first_id()
        self.run_cli("retract", "--id", eid, "--reason", "wrong slot inferred")
        self.assertEqual(self.run_cli("status", "--json")[1]["retracted"], 1)

        rc, r = self.run_cli("ack", "--reminder-id", RID, "--ack-date", DATE)
        self.assertEqual(rc, 0)
        self.assertEqual(r["id"], eid)             # the same row, re-armed
        self.assertIs(r["created"], False)
        self.assertTrue(r["revived"])

        st = self.run_cli("status", "--json")[1]
        self.assertEqual(st["retracted"], 0)       # no longer resting in the retracted state
        self.assertEqual(st["counts"]["pending"], 1)
        rc, out = self.run_cli("pull", "--json")
        self.assertEqual([c["id"] for c in out["claimed"]], [eid])


if __name__ == "__main__":
    unittest.main()
