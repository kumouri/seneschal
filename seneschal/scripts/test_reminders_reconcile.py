#!/usr/bin/env python3
"""Tests for reminders_reconcile.py — Dream step 2's queue reconcile, with an owning script.

The finding it pins: a prose "reconcile `state/reminders.json`" step with no script behind it lets the
queue grow by hundreds of stale entries, because nothing else ever deletes a row.

Every clock-dependent case injects its instant (``_frozen`` wraps the real ``activity_day.today``
around a fixed UTC moment) and patches the owner's zone to a fixed UTC-5 with the default 05:00 day
boundary, so nothing depends on the runner's timezone or a real ``persona/identity.json``.

Run:  python -m unittest seneschal.scripts.test_reminders_reconcile  (or)  python test_reminders_reconcile.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import activity_day as ad  # noqa: E402
import clock  # noqa: E402
import dream_steps as ds  # noqa: E402
import reminders_reconcile as rr  # noqa: E402
import tz_common  # noqa: E402

OWNER_ZONE = timezone(timedelta(hours=-5))

#: 2026-08-20 15:00 owner-local (UTC-5) == 20:00Z. Activity day = 2026-08-20.
NOW = datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc)


class _OwnerZone(unittest.TestCase):
    def setUp(self):
        z = mock.patch.object(tz_common, "_zone", return_value=OWNER_ZONE)
        i = mock.patch.object(clock, "load_identity", return_value={})
        z.start()
        i.start()
        self.addCleanup(z.stop)
        self.addCleanup(i.stop)


@contextlib.contextmanager
def _frozen(instant=NOW):
    real_today = ad.today
    with mock.patch.object(ad, "today", lambda now=None, **kw: real_today(instant, **kw)):
        yield


def _write_queue(state_dir, rows):
    path = os.path.join(state_dir, "reminders.json")
    os.makedirs(state_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh)
    return path


def _read_queue(state_dir):
    with open(os.path.join(state_dir, "reminders.json"), encoding="utf-8") as fh:
        return json.load(fh)


class ReconcilePredicate(_OwnerZone):
    """The pure function — ``today`` is keyword-only and required."""

    TODAY = date(2026, 8, 20)

    def test_a_stale_entry_is_dropped(self):
        rows = [{"id": "e-stale", "due_at": "2026-08-18T13:00:00Z", "fired_at": None}]
        kept, dropped = rr.reconcile(rows, today=self.TODAY)
        self.assertEqual(kept, [])
        self.assertEqual([r["id"] for r in dropped], ["e-stale"])

    def test_todays_entry_is_kept(self):
        # 18:00Z == 13:00 owner-local, well past the 05:00 cut: activity day 2026-08-20.
        rows = [{"id": "e-today", "due_at": "2026-08-20T18:00:00Z", "fired_at": None}]
        kept, dropped = rr.reconcile(rows, today=self.TODAY)
        self.assertEqual([r["id"] for r in kept], ["e-today"])
        self.assertEqual(dropped, [])

    def test_a_future_entry_is_kept(self):
        rows = [{"id": "e-future", "due_at": "2026-08-25T13:00:00Z", "fired_at": None}]
        kept, dropped = rr.reconcile(rows, today=self.TODAY)
        self.assertEqual([r["id"] for r in kept], ["e-future"])
        self.assertEqual(dropped, [])

    def test_the_after_midnight_rule_decides_both_edges(self):
        """01:00 local on the 21st belongs to the 20th (kept); 04:00 local on the 20th belongs to the
        19th (dropped). The comparison is on activity days in the owner's zone, not calendar dates."""
        rows = [{"id": "e-late-night", "due_at": "2026-08-21T06:00:00Z", "fired_at": None},
                {"id": "e-pre-cut", "due_at": "2026-08-20T09:00:00Z", "fired_at": None}]
        kept, dropped = rr.reconcile(rows, today=self.TODAY)
        self.assertEqual([r["id"] for r in kept], ["e-late-night"])
        self.assertEqual([r["id"] for r in dropped], ["e-pre-cut"])

    def test_an_unparseable_due_at_is_kept(self):
        """This script's only power is deletion, so an unreadable date means 'leave it alone'."""
        rows = [{"id": "e-junk", "due_at": "soon", "fired_at": None}]
        kept, dropped = rr.reconcile(rows, today=self.TODAY)
        self.assertEqual([r["id"] for r in kept], ["e-junk"])
        self.assertEqual(dropped, [])

    def test_an_absent_due_at_is_kept(self):
        rows = [{"id": "e-nodue", "fired_at": None}]
        kept, dropped = rr.reconcile(rows, today=self.TODAY)
        self.assertEqual([r["id"] for r in kept], ["e-nodue"])
        self.assertEqual(dropped, [])

    def test_a_non_dict_row_survives(self):
        rows = ["junk", {"id": "e-stale", "due_at": "2026-08-01T00:00:00Z", "fired_at": None}]
        kept, dropped = rr.reconcile(rows, today=self.TODAY)
        self.assertIn("junk", kept)
        self.assertEqual([r["id"] for r in dropped], ["e-stale"])

    def test_already_fired_stale_entries_are_still_dropped(self):
        """Unlike reminders_dequeue.cancel (which only touches un-fired entries), this reconcile is
        what garbage-collects fired history — that is its whole job."""
        rows = [{"id": "e-fired-stale", "due_at": "2026-08-15T13:00:00Z",
                 "fired_at": "2026-08-15T13:00:04Z"}]
        kept, dropped = rr.reconcile(rows, today=self.TODAY)
        self.assertEqual(kept, [])
        self.assertEqual([r["id"] for r in dropped], ["e-fired-stale"])


class CliRoundTrip(_OwnerZone):
    """Through main(): the queue lock, the atomic write and the dream_steps stamp."""

    def _rows(self):
        return [
            {"id": "e-stale", "due_at": "2026-08-18T13:00:00Z", "fired_at": None},
            {"id": "e-today", "due_at": "2026-08-20T18:00:00Z", "fired_at": None},
            {"id": "e-future", "due_at": "2026-08-25T13:00:00Z", "fired_at": None},
            {"id": "e-junk", "due_at": "soon", "fired_at": None},
        ]

    def test_reconcile_drops_stale_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, self._rows())
            self.assertEqual(rr.main(["--state-dir", d]), 0)
            self.assertEqual({r["id"] for r in _read_queue(d)}, {"e-today", "e-future", "e-junk"})

    def test_the_file_survives_intact_on_a_no_op_run(self):
        """Nothing stale -> nothing dropped -> the file is never even rewritten."""
        with tempfile.TemporaryDirectory() as d, _frozen():
            path = _write_queue(d, self._rows()[1:])
            before = os.path.getmtime(path)
            self.assertEqual(rr.main(["--state-dir", d]), 0)
            self.assertEqual({r["id"] for r in _read_queue(d)}, {"e-today", "e-future", "e-junk"})
            self.assertEqual(os.path.getmtime(path), before)  # untouched, not just unchanged content

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, self._rows())
            self.assertEqual(rr.main(["--state-dir", d, "--dry-run"]), 0)
            self.assertEqual(len(_read_queue(d)), 4)
            self.assertFalse(os.path.exists(os.path.join(d, "dream-steps.json")))

    def test_self_stamps_dream_steps(self):
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, self._rows())
            with mock.patch.object(ds, "record", wraps=ds.record) as rec:
                rr.main(["--state-dir", d])
            rec.assert_called_once_with(d, rr.DREAM_STEP)
            self.assertEqual(ds.STEPS[rr.DREAM_STEP]["owner"], "reminders_reconcile.py")
            row = next(r for r in ds.report(d) if r["step"] == "2")
            self.assertIsNotNone(row["last_ok"])
            self.assertEqual(row["owner"], "reminders_reconcile.py")

    def test_stamps_even_on_a_night_nothing_drops(self):
        """A quiet night must still look like a night the step ran."""
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, self._rows()[1:])
            rr.main(["--state-dir", d])
            self.assertTrue(os.path.exists(os.path.join(d, "dream-steps.json")))

    def test_missing_queue_file_is_a_no_op_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d, _frozen():
            self.assertEqual(rr.main(["--state-dir", d]), 0)
            self.assertFalse(os.path.exists(os.path.join(d, "reminders.json")))

    def test_output_summary_shape(self):
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, self._rows())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rr.main(["--state-dir", d])
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual((out["ok"], out["before"], out["kept"], out["dropped"]),
                             (True, 4, 3, 1))
            self.assertEqual(out["today"], "2026-08-20")


if __name__ == "__main__":
    unittest.main()
