#!/usr/bin/env python3
"""Tests for the transcript archive's size sensor (`transcript_size_watch.py`).

**What is being guarded is a promise, not a feature.** Retention is keep-everything with a condition
attached — *tell the owner if it goes above ~200 MB* — and the failure mode this whole module exists
for is the one `notion-write-behind-outbox-spec.md` §7 describes: a threshold named in a decision with
nothing measuring it, which stays silent through the very growth it names. So the tests that matter here are the ones about *when the alarm does not sound*.

Five families:

1. **The threshold is exact** — it fires at 200 MB and does not fire at one byte under. A sensor with
   a fuzzy edge is one nobody can reason about from the decision that set it.
2. **It fires ONCE.** Repeated runs over the same breach send exactly one message. A nightly re-nag
   is muted, and a muted alarm is worse than none.
   And the marker keys on the THRESHOLD, so changing the number re-arms the sensor without anyone
   remembering to delete a file.
3. **A failed send does not spend the one-shot.** The marker is written only after delivery lands.
   The opposite ordering leaves the archive over the threshold with the sensor permanently quiet —
   the §7 failure again, arrived at from the other side, and it is a one-line mistake to make.
4. **The rate is computed, never constant** — a doubled corpus reports a doubled rate; the newest
   (partial) day is excluded; a gap in the day files divides by elapsed calendar days rather than by
   file count; and an archive too short to support a rate abstains instead of extrapolating.
5. **Nothing here can cost a Dream run, and nothing here deletes.** A missing directory, an
   unreadable marker, an exploding sender, an exploding measurement: every one is a quiet no-op with
   exit 0. And the module is parsed to assert it contains no deletion at all — the decision is
   keep-everything, so `prune`/`remove`/`unlink` appearing here later is the bug, not the fix.

**No test may ever send for real.** Every path that could reach Telegram takes an injected `sender`
(`check(sender=...)` / `main(argv, sender=...)`); the default sender is never exercised. That is the
same rule `presence.py`'s offline tests hold, and for the same reason — the real one messages the owner.

Run:  python -m unittest test_transcript_size_watch   (or)  python test_transcript_size_watch.py
"""
import ast
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import transcript_archive as ta          # noqa: E402
import transcript_size_watch as tsw      # noqa: E402
import tz_common                         # noqa: E402

T0 = datetime(2026, 8, 9, 18, 30, tzinfo=timezone.utc)
# The owner's zone, pinned (UTC-5) so the day files T0 names cannot move with the runner's clock.
LOCAL = timezone(timedelta(hours=-5))

#: The threshold every family except `TheThresholdIsExact` runs against. Firing once, keying the
#: marker on the number, and not spending the one-shot on a failed send are all **threshold-
#: independent**, and `threshold` is a real parameter of `check()` — so only the family whose subject
#: IS the 200 MB edge pays for 200 MB of real files. The first cut used the live constant everywhere
#: and spent several gigabytes of writes per run to assert nothing about size at all.
SMALL = 4096


class Sender:
    """A stand-in for the Telegram push. Records what it was asked to send; `ok` is switchable so a
    failed delivery can be tested without a network anywhere near it."""

    def __init__(self, ok=True, error="boom"):
        self.ok, self.error, self.sent = ok, error, []

    def __call__(self, text):
        self.sent.append(text)
        return {"ok": True, "message_id": 1} if self.ok else {"ok": False, "error": self.error}


class WatchBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(tz_common, "_zone", return_value=LOCAL)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_day(self, day, size):
        """Put a day file of exactly `size` bytes on disk. The sensor stats files rather than reading
        them, so the CONTENT is irrelevant and only the byte count is under test — which is the whole
        reason it can be cheap enough to run nightly."""
        path = ta.archive_path(self.dir, day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.truncate(size)   # sets st_size without writing 200 MB — the sensor only ever stats
        return path

    def days_back(self, n, size, end=T0):
        """`n` consecutive day files ending at `end`, each `size` bytes. Dates are derived through
        `ta.local_day` from an injected instant, never hard-coded — the `test_watch_ack_gate.py` trap
        (a wall-clock fixture that agreed on exactly one calendar day and went red on UTC CI)."""
        for i in range(n):
            self.write_day(ta.local_day(end - timedelta(days=i)), size)


# ────────────────────────────────────────────────────────── 1. the threshold is exact

class TheThresholdIsExact(WatchBase):
    """200 MB is the owner's number. A sensor whose edge is approximate cannot be reasoned about
    from the decision that set it."""

    def test_it_fires_at_the_threshold(self):
        self.write_day(ta.local_day(T0 - timedelta(days=1)), tsw.THRESHOLD_BYTES)
        self.write_day(ta.local_day(T0), 0)
        sender = Sender()
        verdict = tsw.check(self.dir, sender=sender, now=T0)
        self.assertTrue(verdict["breached"])
        self.assertTrue(verdict["fired"])
        self.assertEqual(len(sender.sent), 1)

    def test_it_does_not_fire_one_byte_under(self):
        # 199-and-change is not 200. The off-by-one here is the difference between a sensor and a
        # rounding opinion.
        self.write_day(ta.local_day(T0 - timedelta(days=1)), tsw.THRESHOLD_BYTES - 1)
        sender = Sender()
        verdict = tsw.check(self.dir, sender=sender, now=T0)
        self.assertFalse(verdict["breached"])
        self.assertFalse(verdict["fired"])
        self.assertEqual(sender.sent, [])
        self.assertIsNone(tsw.read_marker(self.dir))

    def test_the_threshold_constant_is_the_owners_number(self):
        self.assertEqual(tsw.THRESHOLD_BYTES, 200 * 1024 * 1024)

    def test_the_whole_directory_counts_not_only_day_files(self):
        # The decision is about what `state/transcripts/` costs on disk, so the measurement is the
        # directory's footprint. A day file plus a stray sibling still adds up to what was asked about.
        self.write_day(ta.local_day(T0), 10)
        with open(os.path.join(ta.archive_dir(self.dir), "notes.txt"), "wb") as fh:
            fh.write(b"y" * 90)
        self.assertEqual(tsw.measure(self.dir)["bytes"], 100)


# ────────────────────────────────────────────────────────── 2. it fires exactly once

class ItFiresOnce(WatchBase):
    """A threshold that re-nags every night is one the owner mutes, and then it is worse than nothing."""

    def _breach(self):
        self.write_day(ta.local_day(T0 - timedelta(days=2)), SMALL)
        self.write_day(ta.local_day(T0 - timedelta(days=1)), 1024)
        self.write_day(ta.local_day(T0), 512)

    def check(self, **kw):
        kw.setdefault("threshold", SMALL)
        return tsw.check(self.dir, **kw)

    def test_repeated_runs_send_exactly_one_message(self):
        self._breach()
        sender = Sender()
        first = self.check(sender=sender, now=T0)
        rest = [self.check(sender=sender, now=T0 + timedelta(days=i)) for i in range(1, 6)]
        self.assertTrue(first["fired"])
        self.assertEqual(len(sender.sent), 1, "the sensor re-nagged")
        self.assertTrue(all(v["breached"] for v in rest), "it stopped noticing the breach")
        self.assertFalse(any(v["fired"] for v in rest))

    def test_a_later_run_reports_when_it_already_fired(self):
        # Quiet must not mean invisible: a Dream run that looks at the verdict can still see that the
        # archive is over and when the owner was told.
        self._breach()
        self.check(sender=Sender(), now=T0)
        again = self.check(sender=Sender(), now=T0 + timedelta(days=30))
        self.assertTrue(again["breached"])
        self.assertTrue(again["already_fired_at"])

    def test_the_marker_records_the_threshold_it_fired_at(self):
        self._breach()
        self.check(sender=Sender(), now=T0)
        marker = tsw.read_marker(self.dir)
        self.assertEqual(marker["threshold_bytes"], SMALL)
        self.assertEqual(marker["schema"], tsw.SCHEMA)
        self.assertIn("fired_at", marker)

    def test_changing_the_number_re_arms_the_sensor(self):
        # "Stay quiet until the number changes" is mechanical, not remembered: the marker is keyed on
        # the threshold, so a new number is a new decision and gets its own single alert.
        self._breach()
        sender = Sender()
        self.check(sender=sender, now=T0)
        self.assertEqual(len(sender.sent), 1)
        rerruled = self.check(threshold=SMALL // 2, sender=sender, now=T0)
        self.assertTrue(rerruled["fired"])
        self.assertEqual(len(sender.sent), 2)

    def test_deleting_the_marker_re_arms_the_sensor(self):
        self._breach()
        sender = Sender()
        self.check(sender=sender, now=T0)
        os.remove(tsw.marker_path(self.dir))
        self.assertTrue(self.check(sender=sender, now=T0)["fired"])
        self.assertEqual(len(sender.sent), 2)

    def test_a_corrupt_marker_re_arms_rather_than_silencing(self):
        # A duplicate alert costs a buzz. A marker that cannot be read but silences anyway costs the
        # feature — so an unreadable marker fails toward speaking.
        self._breach()
        self.check(sender=Sender(), now=T0)
        with open(tsw.marker_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        sender = Sender()
        self.assertTrue(self.check(sender=sender, now=T0)["fired"])
        self.assertEqual(len(sender.sent), 1)

    def test_dry_run_computes_the_message_and_spends_nothing(self):
        self._breach()
        sender = Sender()
        verdict = self.check(sender=sender, dry_run=True, now=T0)
        self.assertTrue(verdict["breached"])
        self.assertFalse(verdict["fired"])
        self.assertIn("crossed", verdict["message"])
        self.assertEqual(sender.sent, [])
        self.assertIsNone(tsw.read_marker(self.dir))


# ─────────────────────────────────────────── 3. a failed send does not spend the one-shot

class AFailedSendDoesNotSpendTheOneShot(WatchBase):
    """There is exactly one alert to spend, and it is spent on DELIVERY, not on the attempt —
    `jobs.py`'s rule, load-bearing here for the same reason it is there."""

    def _breach(self):
        self.write_day(ta.local_day(T0 - timedelta(days=1)), SMALL)
        self.write_day(ta.local_day(T0), 64)

    def check(self, **kw):
        kw.setdefault("threshold", SMALL)
        return tsw.check(self.dir, **kw)

    def test_a_failed_send_writes_no_marker(self):
        self._breach()
        verdict = self.check(sender=Sender(ok=False, error="telegram down"), now=T0)
        self.assertFalse(verdict["fired"])
        self.assertEqual(verdict["send_error"], "telegram down")
        self.assertIsNone(tsw.read_marker(self.dir))

    def test_tomorrow_retries_after_a_failed_send(self):
        self._breach()
        self.check(sender=Sender(ok=False), now=T0)
        good = Sender()
        verdict = self.check(sender=good, now=T0 + timedelta(days=1))
        self.assertTrue(verdict["fired"])
        self.assertEqual(len(good.sent), 1)

    def test_a_sender_that_raises_costs_the_alert_not_the_run(self):
        self._breach()

        def explode(_text):
            raise RuntimeError("network on fire")

        verdict = self.check(sender=explode, now=T0)
        self.assertFalse(verdict["ok"])
        self.assertFalse(verdict["fired"])
        self.assertIsNone(tsw.read_marker(self.dir))

    def test_a_sender_returning_junk_is_not_a_landed_send(self):
        # Only `{"ok": True}` counts. Anything else is an unproven delivery, and an unproven delivery
        # must not be allowed to look like the one alert this sensor has to spend.
        for i, junk in enumerate((None, "sent!", {}, {"ok": False}, [])):
            with self.subTest(junk=junk):
                self.dir = os.path.join(self._tmp.name, f"case{i}")
                os.makedirs(self.dir)
                self._breach()
                self.assertFalse(self.check(sender=lambda _t, j=junk: j, now=T0)["fired"])
                self.assertIsNone(tsw.read_marker(self.dir))


# ───────────────────────────────────────────────────── 4. the rate is computed, not constant

class TheRateIsComputed(WatchBase):
    """*"Measured growth today is ~242 KB/day"* is a fact about one day on one install, not a
    constant. These assert the number moves with the disk."""

    def test_the_rate_is_the_whole_days_average(self):
        # Three whole days of 1000 B, plus today's partial file.
        self.write_day(ta.local_day(T0 - timedelta(days=3)), 1000)
        self.write_day(ta.local_day(T0 - timedelta(days=2)), 1000)
        self.write_day(ta.local_day(T0 - timedelta(days=1)), 1000)
        self.write_day(ta.local_day(T0), 7)
        self.assertEqual(tsw.measure(self.dir)["bytes_per_day"], 1000)

    def test_doubling_the_corpus_doubles_the_reported_rate(self):
        self.days_back(4, 1000)
        base = tsw.measure(self.dir)["bytes_per_day"]
        self.days_back(4, 2000)
        self.assertEqual(tsw.measure(self.dir)["bytes_per_day"], base * 2)

    def test_todays_partial_day_is_excluded(self):
        # Today's file is still being appended to. Counting it drags the observed rate BELOW the
        # truth, and an under-stated rate over-states how long there is.
        self.write_day(ta.local_day(T0 - timedelta(days=1)), 1000)
        self.write_day(ta.local_day(T0), 1)
        self.assertEqual(tsw.measure(self.dir)["bytes_per_day"], 1000)

    def test_a_gap_in_the_days_divides_by_elapsed_days_not_by_file_count(self):
        # The daemon was down for a week. Dividing by the number of FILES would report the rate as
        # though those days never happened.
        self.write_day(ta.local_day(T0 - timedelta(days=10)), 1000)
        self.write_day(ta.local_day(T0 - timedelta(days=1)), 1000)
        self.write_day(ta.local_day(T0), 5)
        m = tsw.measure(self.dir)
        self.assertEqual(m["rate_over_days"], 10)
        self.assertEqual(m["bytes_per_day"], 200)

    def test_it_abstains_from_a_rate_on_a_single_day(self):
        # `turns.py stats` and `journal_offers.py stats` abstain on exactly this. One partial day is
        # not a rate, and a made-up one is worse than none.
        self.write_day(ta.local_day(T0), 5000)
        m = tsw.measure(self.dir)
        self.assertIsNone(m["bytes_per_day"])
        self.assertIsNone(m["days_to_threshold"])

    def test_the_yearly_figure_follows_the_daily_one(self):
        self.days_back(3, 1000)
        m = tsw.measure(self.dir)
        self.assertEqual(m["bytes_per_year"], m["bytes_per_day"] * 365)

    def test_days_to_threshold_is_projected_from_the_measured_rate(self):
        self.write_day(ta.local_day(T0 - timedelta(days=1)), 1000)
        self.write_day(ta.local_day(T0), 0)
        m = tsw.measure(self.dir, threshold=11_000)
        self.assertEqual(m["bytes_per_day"], 1000)
        self.assertEqual(m["days_to_threshold"], 10)

    def test_the_message_quotes_the_measured_rate_not_a_constant(self):
        self.write_day(ta.local_day(T0 - timedelta(days=1)), 3000)
        self.write_day(ta.local_day(T0), 1096)
        sender = Sender()
        tsw.check(self.dir, threshold=SMALL, sender=sender, now=T0)
        message = sender.sent[0]
        self.assertIn(tsw.human_bytes(3000) + "/day", message)   # the whole day, not the partial one
        self.assertIn("2026-08-09", message)
        self.assertNotIn("242 KB", message, "the message is quoting the day this was written")

    def test_the_message_abstains_rather_than_inventing_a_rate(self):
        self.write_day(ta.local_day(T0), SMALL)
        sender = Sender()
        tsw.check(self.dir, threshold=SMALL, sender=sender, now=T0)
        self.assertIn("can't measure a growth rate", sender.sent[0])

    def test_human_bytes_reads_the_way_the_threshold_is_worded(self):
        self.assertEqual(tsw.human_bytes(tsw.THRESHOLD_BYTES), "200 MB")
        self.assertEqual(tsw.human_bytes(1024), "1 KB")
        self.assertEqual(tsw.human_bytes(1536), "1.5 KB")
        self.assertEqual(tsw.human_bytes(512), "512 B")
        self.assertEqual(tsw.human_bytes(None), "unknown")


# ──────────────────────────────────────── 5. it cannot cost a Dream run, and it deletes nothing

class ItCannotCostTheDreamRun(WatchBase):
    """Same contract as every other observer in this directory: a failure costs the observation and
    nothing else."""

    def test_a_missing_directory_is_a_silent_no_op(self):
        missing = os.path.join(self.dir, "nowhere")
        sender = Sender()
        verdict = tsw.check(missing, sender=sender, now=T0)
        self.assertTrue(verdict["ok"])
        self.assertFalse(verdict["breached"])
        self.assertFalse(verdict["fired"])
        self.assertEqual(sender.sent, [])
        self.assertEqual(verdict["bytes"], 0)
        self.assertIsNone(verdict["bytes_per_day"])

    def test_a_missing_directory_raises_nothing_from_measure_or_status(self):
        missing = os.path.join(self.dir, "nowhere")
        self.assertEqual(tsw.measure(missing)["days"], 0)
        self.assertFalse(tsw.already_fired(missing))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(tsw.main(["--state-dir", missing, "status"]), 0)
        self.assertEqual(json.loads(buf.getvalue())["bytes"], 0)

    def test_an_exploding_measurement_is_caught(self):
        real = tsw.measure
        tsw.measure = lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone"))
        try:
            verdict = tsw.check(self.dir, sender=Sender(), now=T0)
        finally:
            tsw.measure = real
        self.assertFalse(verdict["ok"])
        self.assertFalse(verdict["fired"])
        self.assertIn("disk gone", verdict["error"])

    def test_an_unwritable_marker_does_not_undo_a_landed_send(self):
        # The owner has already been told. A marker that fails to write means a second telling, which
        # is the right way round — but the send itself must still read as having happened.
        self.write_day(ta.local_day(T0 - timedelta(days=1)), SMALL)
        self.write_day(ta.local_day(T0), 1)
        real = tsw._write_marker
        tsw._write_marker = lambda *a, **k: False
        try:
            verdict = tsw.check(self.dir, threshold=SMALL, sender=Sender(), now=T0)
        finally:
            tsw._write_marker = real
        self.assertTrue(verdict["fired"])
        self.assertFalse(verdict["marked"])

    def test_the_cli_always_exits_zero(self):
        for argv in (["status"], ["check"], ["check", "--dry-run"]):
            with self.subTest(argv=argv):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = tsw.main(["--state-dir", self.dir] + argv, sender=Sender())
                self.assertEqual(rc, 0)
                json.loads(buf.getvalue())

    def test_the_cli_check_fires_once_through_the_injected_sender(self):
        self.write_day(ta.local_day(T0 - timedelta(days=1)), SMALL)
        self.write_day(ta.local_day(T0), 1)
        sender = Sender()
        argv = ["--state-dir", self.dir, "--threshold-bytes", str(SMALL), "check"]
        with redirect_stdout(io.StringIO()):
            tsw.main(argv, sender=sender)
            tsw.main(argv, sender=sender)
        self.assertEqual(len(sender.sent), 1)

    def test_the_sensor_deletes_nothing_from_the_archive(self):
        # The decision is keep-everything. Every file that was there before a fired alert is there
        # after it, byte for byte.
        self.days_back(3, SMALL)
        before = {n: os.path.getsize(os.path.join(ta.archive_dir(self.dir), n))
                  for n in os.listdir(ta.archive_dir(self.dir))}
        tsw.check(self.dir, threshold=SMALL, sender=Sender(), now=T0)
        after = {n: os.path.getsize(os.path.join(ta.archive_dir(self.dir), n))
                 for n in os.listdir(ta.archive_dir(self.dir))}
        self.assertEqual(before, after)

    def test_the_module_contains_no_deletion_at_all(self):
        # Parsed, not grepped — a substring scan reads this module's own prose about not deleting as
        # a deletion (`test_transcript_archive.py` learned that the hard way, going red on its own
        # docstring). The marker is written through `stateio`, so the module holds no `remove` at
        # all; the `tmp` exemption stays only so a hand-rolled staging cleanup would still pass.
        with open(tsw.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in {"remove", "unlink", "rmtree", "prune", "truncate"}:
                arg = node.args[0] if node.args else None
                if not (name == "remove" and isinstance(arg, ast.Name) and arg.id == "tmp"):
                    offenders.append(f"line {node.lineno}: {name}()")
        self.assertEqual(offenders, [], f"the sensor grew a deleter: {offenders}")


# ──────────────────────────────────────────────────────────────────── the wiring, over the real tree

class ProducerCoverage(WatchBase):
    """`state/metrics.jsonl` is the standing precedent, and §7 of the outbox spec is the specific
    one: the risk was never a buggy sensor, it was a sensor nothing runs. Asserted over the real
    file, not assumed."""

    def test_dream_runs_the_sensor_nightly(self):
        path = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "modes", "dream.md"))
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("transcript_size_watch.py check", body)

    def test_the_archive_records_the_decision_as_taken(self):
        # The decision and the sensor are one change. If the archive still describes retention as an
        # open question, one half of it did not land.
        self.assertIs(ta.stats(self.dir)["retention_decided"], True)
        self.assertEqual(ta.RETENTION_DAYS, 0)


if __name__ == "__main__":
    unittest.main()
