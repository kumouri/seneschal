#!/usr/bin/env python3
"""Tests for the substance-or-silence mechanism (`seneschal/docs/substance-or-silence-spec.md` phases 1-2).
Stdlib ``unittest`` only.

**A suppression mechanism is guilty until proven innocent, and these tests are the proof.** The harm
it can do is not an extra buzz — it is a message the owner needed that never arrived, and that
failure is invisible from the owner's side and from the log. So the bulk of this file is not *"does it
suppress the redundant turn?"* but *"here are the ways it could suppress the wrong thing, and it does
not"*.

The two that matter most:

  * **The `ledger` arm may never decide it.** `reminders_acks.reminder_acked_today` is positive
    *milliseconds* after a 👍 arrives, because the daemon's own dequeue stamps it. Gating on bare
    `acked` would suppress **the first relay** — the one turn that has to run. Only the
    `notion-outbox` arm, which counts rows that actually **wrote**, may license silence (§4.2).
  * **A failed write ALWAYS speaks.** §7.1, and in the mechanism it is not a rule anybody follows: an
    entry that is not `done` **is** the not-yet-landed case, so the turn relays.

**The predicate is optional here.** `reminders_acks.reaction_ack_fully_landed` lands with a later
wave; until it does, the tests that exercise it SKIP (reason: "wave-24 reminders_acks predicate"),
and the ones that prove `turn_suppression.verdict` relays when it is absent run.

**Not covered here:** the drainer bookkeeping (a suppressed turn is a third outcome — the owner's side
still captured, nothing reaching the Mouth, the arrival marker consumed, no turn window, the attempt
counter untouched) and the inbound side map wiring. Those exercise `presence` functions and land with
the presence wiring.

Run:  python -m unittest test_turn_suppression   (from seneschal/scripts)
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import outbox_common as ob  # noqa: E402
import reminders_acks as ra  # noqa: E402
import turn_suppression as ts  # noqa: E402
import tz_common  # noqa: E402

RID = "00000000-0000-0000-0000-00000000a1b2"

#: The predicate this module gates on lands with a later wave (reminders_acks). Until then every
#: test that calls it directly is skipped rather than removed, so it activates the moment it exists.
HAS_PREDICATE = hasattr(ra, "reaction_ack_fully_landed") and hasattr(ra, "reminder_acked_today")
NEEDS_PREDICATE = unittest.skipUnless(HAS_PREDICATE, "wave-24 reminders_acks predicate")
#: A LANDED verdict also needs the outbox's record of what it actually wrote
#: (`outbox_common.latest_landed_ack`, Notion backend only), which arrives with the outbox port.
#: Without it the store arm has no opinion and every verdict relays — the fail-open direction.
NEEDS_LANDED_READER = unittest.skipUnless(hasattr(ob, "latest_landed_ack"),
                                          "outbox_common.latest_landed_ack (outbox port)")


def _midday() -> datetime:
    """An aware instant whose OWNER-local time is midday.

    `local_today` is owner-local by design, so an anchor at `now()` can straddle local midnight on a
    UTC runner and the test silently stops testing what it names."""
    return tz_common.to_local(datetime.now(timezone.utc)).replace(
        hour=12, minute=0, second=0, microsecond=0)


NOW = _midday()
TODAY = ra.local_today(NOW)


class OutboxFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _conn(self):
        return ob.connect(self.dir)

    def seed_ack(self, *, landed=True, date=TODAY, rid=RID, superseded=False):
        conn = self._conn()
        try:
            entry = ob.enqueue(conn, "ack_reminder", "page", rid,
                               {"last_acknowledged": date, "status": "Done"},
                               ob.ack_key(rid, date))
            if superseded:
                ob.mark_superseded(conn, entry["id"], date)
            elif landed:
                ob.mark_done(conn, entry["id"])
            return entry
        finally:
            conn.close()

    def seed_ledger(self, rid=RID, date=TODAY):
        """The daemon's own local half — positive milliseconds after the 👍, and never sufficient."""
        ra.record_ack(self.dir, rid, date)


# ===================================================================== PHASE 1 — the predicate

class TheLedgerArmMayNeverDecideIt(OutboxFixture):
    """§4.2. The single most important property in the file: `record_ack` runs inside
    `reminders_dequeue`, which the reaction path calls synchronously, so the ledger reads positive
    before the relay has even been queued."""

    @NEEDS_PREDICATE
    def test_the_ledger_alone_does_not_license_silence(self):
        self.seed_ledger()
        verdict = ra.reaction_ack_fully_landed(self.dir, RID, NOW)
        self.assertTrue(ra.reminder_acked_today(self.dir, RID, NOW)["acked"],
                        "fixture guard: the ledger arm must be positive, or this proves nothing")
        self.assertFalse(verdict["landed"])
        self.assertEqual(verdict["why"], "ack-not-landed")

    def test_the_first_relay_is_not_suppressed(self):
        """An ack's outbox entry can land well after the ack was relayed, inside a turn that had not
        started yet. At relay time only the ledger knows, and the first turn is the one that reports
        the writes. (Holds with or without the predicate: absent, the verdict relays.)"""
        self.seed_ledger()
        self.assertFalse(ts.verdict(self.dir, RID, NOW)["suppress"])

    @NEEDS_PREDICATE
    @NEEDS_LANDED_READER
    def test_a_landed_outbox_ack_does(self):
        self.seed_ack(landed=True)
        verdict = ra.reaction_ack_fully_landed(self.dir, RID, NOW)
        self.assertTrue(verdict["landed"])
        self.assertEqual(verdict["why"], "all-landed")
        self.assertIn("notion-outbox", verdict["ack_sources"])


class AFailedOrPendingWriteAlwaysSpeaks(OutboxFixture):
    """§7.1 — *the* constraint. The reason a 👍 is worth anything is that failure is audible against a
    background of confirmations, so a write that has not landed relays, whatever else is true."""

    @NEEDS_PREDICATE
    def test_a_pending_ack_relays(self):
        self.seed_ack(landed=False)
        self.assertFalse(ra.reaction_ack_fully_landed(self.dir, RID, NOW)["landed"])

    @NEEDS_PREDICATE
    @unittest.skipUnless(hasattr(ob, "mark_superseded"), "wave-24 outbox_common.mark_superseded")
    def test_a_superseded_ack_relays(self):
        """A `superseded` row never reached Notion at all. `latest_landed_ack` already excludes it;
        this asserts the exclusion survives into the suppression path, where treating it as landed
        would silence a turn over a write that never happened."""
        self.seed_ack(superseded=True)
        self.assertFalse(ra.reaction_ack_fully_landed(self.dir, RID, NOW)["landed"])

    @NEEDS_PREDICATE
    def test_yesterdays_landed_ack_is_not_todays(self):
        self.seed_ack(landed=True, date="2026-01-01")
        self.assertFalse(ra.reaction_ack_fully_landed(self.dir, RID, NOW)["landed"])


class EverythingUnreadableRelays(OutboxFixture):
    """Fail-open in exactly one direction. None of these may be the reason the owner was not told."""

    @NEEDS_PREDICATE
    def test_an_absent_store_relays(self):
        self.assertFalse(ra.reaction_ack_fully_landed(self.dir, RID, NOW)["landed"])

    @NEEDS_PREDICATE
    def test_an_unnamed_row_relays(self):
        for bad in (None, "", "   ", 42, {}):
            self.assertFalse(ra.reaction_ack_fully_landed(self.dir, bad, NOW)["landed"], repr(bad))

    @NEEDS_PREDICATE
    def test_a_raise_anywhere_relays(self):
        self.seed_ack(landed=True)
        with mock.patch.object(ra, "reminder_acked_today", side_effect=RuntimeError("boom")):
            verdict = ra.reaction_ack_fully_landed(self.dir, RID, NOW)
        self.assertFalse(verdict["landed"])
        self.assertEqual(verdict["why"], "raised")

    @NEEDS_PREDICATE
    def test_the_predicate_creates_nothing(self):
        """Read-only, like `_notion_ack_date`. A predicate that creates the store it is reading would
        make `os.path.exists` mean nothing on the next call, and would have the daemon writing files
        from a gate."""
        ra.reaction_ack_fully_landed(self.dir, RID, NOW)
        self.assertFalse(os.path.exists(ob.db_path(self.dir)))

    def test_a_broken_predicate_import_relays(self):
        with mock.patch.dict(sys.modules, {"reminders_acks": None}):
            self.assertFalse(ts.verdict(self.dir, RID, NOW)["suppress"])


# ===================================================================== PHASE 2 — the side map

class TheSideMap(unittest.TestCase):
    def test_it_maps_the_queued_text_to_the_row(self):
        acks: dict = {}
        ts.remember(acks, "[the owner reacted 👍 …]", RID)
        self.assertEqual(ts.take(acks, "[the owner reacted 👍 …]"), RID)

    def test_taking_consumes(self):
        """Consumed on every drain, suppressed or not: a turn that ran and failed to deliver is
        requeued, and on the retry there is no mapping, so it relays. A delivery failure is not
        evidence that the write landed."""
        acks: dict = {}
        ts.remember(acks, "line", RID)
        ts.take(acks, "line")
        self.assertIsNone(ts.take(acks, "line"))

    def test_an_unmapped_text_is_none(self):
        self.assertIsNone(ts.take({}, "a message the owner actually typed"))
        self.assertIsNone(ts.take({}, ""))

    def test_nothing_is_remembered_without_a_row(self):
        acks: dict = {}
        ts.remember(acks, "line", None)
        ts.remember(acks, "line", "")
        ts.remember(acks, "", RID)
        self.assertEqual(acks, {})

    def test_it_is_bounded_and_evicts_oldest_first(self):
        acks: dict = {}
        for i in range(ts.REACTION_ACK_CAP + 10):
            ts.remember(acks, f"line-{i}", f"row-{i}")
        self.assertEqual(len(acks), ts.REACTION_ACK_CAP)
        self.assertIsNone(ts.take(acks, "line-0"))          # evicted -> relays
        self.assertIsNotNone(ts.take(acks, f"line-{ts.REACTION_ACK_CAP + 9}"))


# ===================================================================== PHASE 2 — the audit trail

class TheAuditTrail(unittest.TestCase):
    """*"A suppression nobody can see is how a silent-drop defect ships."* This is the half that makes
    the mechanism arguable after the fact."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_a_row_carries_the_withheld_line_verbatim(self):
        line = "[the owner reacted 👍 to 'Morning stretch — required']"
        ts.record(self.dir, channel="telegram", text=line, reminder_id=RID,
                  detail={"why": "all-landed"}, turn_id="abc123")
        rows = ts.read(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["withheld"], line)
        self.assertEqual(rows[0]["reminder_id"], RID)
        self.assertEqual(rows[0]["verdict"]["why"], "all-landed")
        self.assertFalse(rows[0]["redacted"])

    def test_a_private_turn_writes_a_tombstone(self):
        ts.record(self.dir, channel="telegram", text="secret", reminder_id=RID, redacted=True)
        self.assertNotIn("secret", ts.read(self.dir)[0]["withheld"])
        self.assertTrue(ts.read(self.dir)[0]["redacted"])

    def test_it_never_raises_and_reports_failure_by_return_value(self):
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(ts.record(self.dir, channel="telegram", text="x", reminder_id=RID))

    def test_an_absent_log_reads_empty_rather_than_raising(self):
        self.assertEqual(ts.read(self.dir), [])

    def test_a_malformed_line_is_skipped_not_fatal(self):
        ts.record(self.dir, channel="telegram", text="a", reminder_id=RID)
        with open(ts.log_path(self.dir), "a", encoding="utf-8") as fh:
            fh.write("{not json\n\n")
        ts.record(self.dir, channel="telegram", text="b", reminder_id=RID)
        self.assertEqual([r["withheld"] for r in ts.read(self.dir)], ["a", "b"])

    def test_the_cli_counts_them_by_row(self):
        ts.record(self.dir, channel="telegram", text="a", reminder_id=RID)
        ts.record(self.dir, channel="telegram", text="b", reminder_id=RID)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.assertEqual(ts.main(["--state-dir", self.dir, "count"]), 0)
        out = json.loads(buf.getvalue().strip().splitlines()[-1])
        self.assertEqual(out["suppressed"], 2)
        self.assertEqual(out["by_reminder"][RID], 2)

    def test_the_cli_tails_them_verbatim(self):
        ts.record(self.dir, channel="telegram", text="the withheld line", reminder_id=RID)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            ts.main(["--state-dir", self.dir, "tail", "--limit", "5"])
        self.assertIn("the withheld line", buf.getvalue())


class TheStructuralExemptions(unittest.TestCase):
    """§5 and §7.2-§7.3 — the never-silent list, as *which code path the change lives in* rather than
    as a rule anything follows."""

    def test_reminders_do_not_come_out_of_this_door(self):
        """`🚨 Critical` / `🛑 Super-Critical` / `Call Me` fire from `sentinel.check_reminders`, a
        different Mouth door from the chat drainer. This asserts the separation still holds: the
        suppression symbol appears nowhere in the sentinel."""
        import sentinel
        with io.open(sentinel.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("turn_suppression", source)

    def test_the_only_caller_of_the_verdict_is_the_drainer(self):
        """A second caller is how a mechanism scoped to one queue entry becomes a general mute. If
        one is ever added deliberately, this test is the place to argue for it. The drainer
        (`presence.py`) is the one sanctioned caller; before the presence wiring lands there is none."""
        here = os.path.dirname(os.path.abspath(__file__))
        callers = []
        for name in sorted(os.listdir(here)):
            if not name.endswith(".py") or name.startswith("test_") or name == "turn_suppression.py":
                continue
            with io.open(os.path.join(here, name), encoding="utf-8") as fh:
                if "turn_suppression.verdict" in fh.read():
                    callers.append(name)
        self.assertIn(callers, ([], ["presence.py"]))


if __name__ == "__main__":
    unittest.main()
