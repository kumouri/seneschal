#!/usr/bin/env python3
"""Tests for the reminders queue helpers — enqueue (--reminder-id) + dequeue-on-ack.

Stdlib ``unittest`` only (matches the Markdown-skill core; CI byte-compiles Python, and this file runs
green under ``python -m unittest``). Covers the 2026-07-03 fix: an ack must cancel the still-queued,
un-fired nudges it makes obsolete, without touching already-fired history or unrelated items.

**And the activity-day scoping, which is what most of this file now is.** An unscoped cancel drops
every un-fired entry for the row. An ack reported at 01:29 for the *previous* evening's dinner (by the
after-midnight rule it belongs to Tuesday the 11th) would then also delete Wednesday evening's
``rmd-2026-08-12-dinner-2130`` and ``-2300`` — Tuesday's had already fired and were kept as history,
Wednesday's had not — and an explicit ``--ack-date 2026-08-11`` would change nothing, because it only
ever reached the ledger write. :class:`ActivityDayScoping` is that night, frozen.

Every clock-dependent case here **injects its instant** (``_frozen``, wrapping the real mapping around
a fixed UTC moment) and **pins the owner zone** (a fixed UTC-5, ``setUpModule``) rather than reading the
wall clock or the runner's timezone: a test that asks "what day is it?" of the machine agrees with
itself on exactly one day, in one timezone.

Run:  python -m unittest seneschal.scripts.test_reminders_queue   (or)   python test_reminders_queue.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import activity_day as ad  # noqa: E402
import clock  # noqa: E402
import reminders_acks as ra  # noqa: E402
import reminders_dequeue as dq  # noqa: E402
import tz_common  # noqa: E402

ENQUEUE = os.path.join(SCRIPT_DIR, "reminders_enqueue.py")
DEQUEUE = os.path.join(SCRIPT_DIR, "reminders_dequeue.py")

#: The owner zone every case here is pinned to: a fixed UTC-5 (a summer offset), no tzdata needed.
OWNER_ZONE = timezone(timedelta(hours=-5))
#: The reproduction instant: 2026-08-12 01:29 owner-local == 06:29Z. Activity day = Tuesday 2026-08-11.
REPRO_NOW = datetime(2026, 8, 12, 6, 29, tzinfo=timezone.utc)
#: The ⏰ row acked that night (a placeholder page id).
DINNER = "00000000-0000-0000-0000-00000000d1e5"

_PATCHES = []


def setUpModule():
    """Pin the owner zone and identity (default 05:00 day boundary) for the whole module — the
    activity-day mapping must not depend on the runner's timezone or a real persona/identity.json."""
    for p in (mock.patch.object(tz_common, "_zone", return_value=OWNER_ZONE),
              mock.patch.object(clock, "load_identity", return_value={})):
        p.start()
        _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


@contextlib.contextmanager
def _frozen(instant=REPRO_NOW):
    """Freeze *now* for the in-process CLI path, keeping the real after-midnight mapping in the loop —
    only the instant it reads is pinned. Subprocess cases pass ``--activity-day`` instead; a patch
    can't reach a child."""
    real_today = ad.today
    with mock.patch.object(ad, "today", lambda now=None, **kw: real_today(instant, **kw)):
        yield


def _run(argv):
    """``reminders_dequeue.main`` in-process (the seam ``ack.py`` uses), returning (rc, parsed JSON)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = dq.main(argv)
    return rc, json.loads(buf.getvalue().strip().splitlines()[-1])


def _write_queue(state_dir, rows):
    path = os.path.join(state_dir, "reminders.json")
    os.makedirs(state_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh)
    return path


def _read_queue(state_dir):
    with open(os.path.join(state_dir, "reminders.json"), encoding="utf-8") as fh:
        return [r["id"] for r in json.load(fh)]


def _that_night():
    """The queue as it stood at 01:29 owner-local on 2026-08-12.

    Tuesday's two dinner nudges have fired (21:30 and 23:00 at UTC-5 = 02:30Z and 04:00Z on the 12th);
    Wednesday's two are seeded and un-fired (21:30 and 23:00 on the 12th = 02:30Z and 04:00Z on the
    **13th**) — the ones an unscoped cancel would eat. The third un-fired Tuesday entry is the deferred 23:45
    one: an ack for Tuesday is exactly what should take it."""
    return [
        {"id": "rmd-2026-08-11-dinner-2130", "reminder_id": DINNER,
         "due_at": "2026-08-12T02:30:00Z", "fired_at": "2026-08-12T02:30:04Z"},
        {"id": "rmd-2026-08-11-dinner-2345", "reminder_id": DINNER,
         "due_at": "2026-08-12T04:45:00Z", "fired_at": None},
        {"id": "rmd-2026-08-12-dinner-2130", "reminder_id": DINNER,
         "due_at": "2026-08-13T02:30:00Z", "fired_at": None},
        {"id": "rmd-2026-08-12-dinner-2300", "reminder_id": DINNER,
         "due_at": "2026-08-13T04:00:00Z", "fired_at": None},
    ]


class CancelUnit(unittest.TestCase):
    """The pure predicate. ``day`` is keyword-only and required — a caller cannot silently fall back
    to the unscoped behaviour this bug was."""

    DAY = date(2026, 7, 3)

    def _rows(self):
        # 2026-07-03, 08:45 and 12:00 owner-local — both on the DAY above
        return [
            {"id": "e-unfired-cats", "reminder_id": "cats-am",
             "due_at": "2026-07-03T13:45:00Z", "fired_at": None},
            {"id": "e-fired-cats", "reminder_id": "cats-am",
             "due_at": "2026-07-03T13:45:00Z", "fired_at": "2026-07-03T13:45:14Z"},
            {"id": "e-unfired-breakfast", "reminder_id": "breakfast",
             "due_at": "2026-07-03T17:00:00Z", "fired_at": None},
            {"id": "e-no-rid", "due_at": "2026-07-03T17:00:00Z", "fired_at": None},
        ]

    def test_cancels_unfired_match_only(self):
        kept, removed = dq.cancel(self._rows(), reminder_ids=["cats-am"], day=self.DAY)
        self.assertEqual([r["id"] for r in removed], ["e-unfired-cats"])
        self.assertIn("e-fired-cats", [r["id"] for r in kept])       # fired history preserved
        self.assertIn("e-unfired-breakfast", [r["id"] for r in kept])  # other item preserved

    def test_dash_insensitive_page_id(self):
        rows = [{"id": "e1", "reminder_id": "00000000-0000-0000-0000-00000000c58a",
                 "due_at": "2026-07-03T13:45:00Z", "fired_at": None}]
        # same id, dashes stripped, should still match
        _, removed = dq.cancel(rows, reminder_ids=["0000000000000000000000000000c58a"], day=self.DAY)
        self.assertEqual(len(removed), 1)

    def test_by_entry_id(self):
        _, removed = dq.cancel(self._rows(), entry_ids=["e-no-rid"], day=self.DAY)
        self.assertEqual([r["id"] for r in removed], ["e-no-rid"])

    def test_empty_reminder_id_never_matches(self):
        # a row with no reminder_id must not be swept by an empty/absent key
        kept, removed = dq.cancel(self._rows(), reminder_ids=[""], day=self.DAY)
        self.assertEqual(removed, [])
        self.assertEqual(len(kept), 4)

    def test_idempotent_absent_key(self):
        _, removed = dq.cancel(self._rows(), reminder_ids=["not-here"], day=self.DAY)
        self.assertEqual(removed, [])

    def test_non_dict_rows_survive(self):
        rows = ["junk", {"id": "e", "reminder_id": "x",
                         "due_at": "2026-07-03T13:45:00Z", "fired_at": None}]
        kept, removed = dq.cancel(rows, reminder_ids=["x"], day=self.DAY)
        self.assertIn("junk", kept)
        self.assertEqual([r["id"] for r in removed], ["e"])

    def test_day_is_required(self):
        with self.assertRaises(TypeError):
            dq.cancel(self._rows(), reminder_ids=["cats-am"])

    def test_an_unreadable_day_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            dq.cancel(self._rows(), reminder_ids=["cats-am"], day="yesterday")

    def test_an_entry_with_no_readable_due_at_is_kept(self):
        """This function deletes, so a date it cannot read has to mean *leave it alone*. The durable
        ack ledger suppresses such an entry at fire time anyway; nothing recovers a deleted nudge."""
        rows = [{"id": "e-nodue", "reminder_id": "cats-am", "fired_at": None},
                {"id": "e-junkdue", "reminder_id": "cats-am", "due_at": "soon", "fired_at": None}]
        kept, removed = dq.cancel(rows, reminder_ids=["cats-am"], day=self.DAY)
        self.assertEqual(removed, [])
        self.assertEqual([r["id"] for r in kept], ["e-nodue", "e-junkdue"])


class ActivityDayScoping(unittest.TestCase):
    """2026-08-12, 01:29 owner-local — a post-midnight ack must not eat the next day's nudges."""

    def test_post_midnight_ack_takes_tuesdays_and_leaves_wednesdays(self):
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, _that_night())
            rc, out = _run(["--reminder-id", DINNER, "--state-dir", d])
            self.assertEqual(rc, 0)
            self.assertEqual(out["activity_day"], "2026-08-11")     # the ACK's day, not the clock's
            self.assertEqual(out["ids"], ["rmd-2026-08-11-dinner-2345"])
            self.assertEqual(_read_queue(d), ["rmd-2026-08-11-dinner-2130",   # fired: history
                                              "rmd-2026-08-12-dinner-2130",   # Wednesday: untouched
                                              "rmd-2026-08-12-dinner-2300"])

    def test_explicit_ack_date_backfill_scopes_the_same_way(self):
        """An explicit backfill ``--ack-date`` now scopes the removal too. Frozen a day
        LATER than the ack date, so a pass can only come from honouring the flag."""
        later = datetime(2026, 8, 13, 15, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d, _frozen(later):
            _write_queue(d, _that_night())
            rc, out = _run(["--reminder-id", DINNER, "--state-dir", d, "--ack-date", "2026-08-11"])
            self.assertEqual(rc, 0)
            self.assertEqual(out["activity_day"], "2026-08-11")
            self.assertEqual(out["ids"], ["rmd-2026-08-11-dinner-2345"])

    def test_activity_day_beats_ack_date(self):
        """``ack.py`` always passes an ``--ack-date`` (a CALENDAR date, from the ledger's own
        function). Post-midnight that names the wrong day, so it passes ``--activity-day`` too and
        that has to win — otherwise the front door reproduces the bug one step further down."""
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, _that_night())
            rc, out = _run(["--reminder-id", DINNER, "--state-dir", d,
                            "--ack-date", "2026-08-12", "--activity-day", "2026-08-11"])
            self.assertEqual(rc, 0)
            self.assertEqual(out["ids"], ["rmd-2026-08-11-dinner-2345"])
            self.assertEqual(ra.load_acks(d), {ra.norm_key(DINNER): "2026-08-12"})  # ledger unmoved

    def test_a_nudge_due_after_midnight_is_the_same_activity_day(self):
        """00:30 local on the FOLLOWING calendar date still belongs to the 11th — and still goes."""
        rows = _that_night() + [{"id": "rmd-2026-08-11-dinner-0030", "reminder_id": DINNER,
                                 "due_at": "2026-08-12T05:30:00Z", "fired_at": None}]
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, rows)
            rc, out = _run(["--reminder-id", DINNER, "--state-dir", d])
            self.assertEqual(rc, 0)
            self.assertEqual(sorted(out["ids"]),
                             ["rmd-2026-08-11-dinner-0030", "rmd-2026-08-11-dinner-2345"])

    def test_fired_entries_are_untouched_even_on_the_scoped_day(self):
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, [r for r in _that_night() if r["fired_at"]])
            rc, out = _run(["--reminder-id", DINNER, "--state-dir", d])
            self.assertEqual((rc, out["removed"]), (0, 0))
            self.assertEqual(_read_queue(d), ["rmd-2026-08-11-dinner-2130"])

    def test_entry_id_stays_exact_and_unscoped(self):
        """Naming an entry outright is already the specific instruction scoping approximates."""
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, _that_night())
            rc, out = _run(["--id", "rmd-2026-08-12-dinner-2300", "--state-dir", d])
            self.assertEqual(rc, 0)
            self.assertEqual(out["ids"], ["rmd-2026-08-12-dinner-2300"])  # tomorrow's, on purpose
            self.assertEqual(out["acked"], [])                            # --id records no ack

    def test_dry_run_reflects_the_scoping_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, _that_night())
            rc, out = _run(["--reminder-id", DINNER, "--state-dir", d, "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertEqual(out["would_remove"], 1)
            self.assertEqual(out["ids"], ["rmd-2026-08-11-dinner-2345"])
            self.assertEqual(out["activity_day"], "2026-08-11")
            self.assertEqual(len(_read_queue(d)), 4)
            self.assertFalse(os.path.exists(ra.acks_path(d)))  # and no ledger write either

    def test_the_ledger_write_is_unchanged(self):
        """The durable ack is the half that was always right. It still stamps the CALENDAR date from
        ``--ack-date`` (or ``ra.local_today``), because the fire-time gate compares against that same
        function — scoping the removal must not quietly re-date the ledger."""
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, _that_night())
            rc, out = _run(["--reminder-id", DINNER, "--state-dir", d, "--ack-date", "2026-08-11"])
            self.assertEqual((rc, out["acked"]), (0, [ra.norm_key(DINNER)]))
            self.assertEqual(ra.load_acks(d), {ra.norm_key(DINNER): "2026-08-11"})

    def test_the_ledger_defaults_to_local_today_not_the_activity_day(self):
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, _that_night())
            rc, out = _run(["--reminder-id", DINNER, "--state-dir", d])
            self.assertEqual(rc, 0)
            self.assertEqual(ra.load_acks(d), {ra.norm_key(DINNER): ra.local_today()})

    def test_no_ack_record_still_records_nothing(self):
        with tempfile.TemporaryDirectory() as d, _frozen():
            _write_queue(d, _that_night())
            rc, out = _run(["--reminder-id", DINNER, "--state-dir", d, "--no-ack-record"])
            self.assertEqual((rc, out["acked"]), (0, []))
            self.assertFalse(os.path.exists(ra.acks_path(d)))

    def test_a_malformed_day_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = _run(["--reminder-id", DINNER, "--state-dir", d, "--activity-day", "08/11/26"])
            self.assertEqual(rc, 2)
            self.assertFalse(out["ok"])
            self.assertIn("YYYY-MM-DD", out["error"])


class CliRoundTrip(unittest.TestCase):
    """Enqueue a nudge with --reminder-id, then dequeue by that id — through the real CLIs."""

    def test_enqueue_then_dequeue(self):
        with tempfile.TemporaryDirectory() as d:
            def run(script, *args):
                out = subprocess.run([sys.executable, script, "--state-dir", d, *args],
                                     capture_output=True, text=True)
                self.assertEqual(out.returncode, 0, out.stderr)
                return json.loads(out.stdout.strip().splitlines()[-1])

            # An explicit due instant + an explicit --activity-day: a subprocess can't be frozen (or
            # zone-pinned) from here, and letting both ends read the wall clock would make this test
            # straddle the day-boundary cut once a night. 13:45Z is 2026-07-03's activity day in every
            # zone from UTC-8 to UTC+14, which covers any runner this suite realistically meets.
            enq = run(ENQUEUE, "--text", "Cats' breakfast.", "--due-at", "2026-07-03T13:45:00Z",
                      "--id", "rmd-t-cats", "--reminder-id", "cats-am")
            self.assertTrue(enq["enqueued"])

            # an unrelated, already-fired entry that must survive the cancel
            path = os.path.join(d, "reminders.json")
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
            rows.append({"id": "rmd-t-fired", "reminder_id": "cats-am",
                         "text": "x", "due_at": "2026-07-03T13:00:00Z",
                         "channel": "telegram", "created_at": "2026-07-03T13:00:00Z",
                         "fired_at": "2026-07-03T13:00:05Z"})
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(rows, fh)

            deq = run(DEQUEUE, "--reminder-id", "cats-am", "--activity-day", "2026-07-03")
            self.assertEqual(deq["ids"], ["rmd-t-cats"])   # only the un-fired one
            self.assertEqual(deq["activity_day"], "2026-07-03")

            with open(path, encoding="utf-8") as fh:
                left = json.load(fh)
            self.assertEqual([r["id"] for r in left], ["rmd-t-fired"])  # fired one stays

            # idempotent: a second cancel removes nothing
            again = run(DEQUEUE, "--reminder-id", "cats-am", "--activity-day", "2026-07-03")
            self.assertEqual(again["removed"], 0)

    def test_dequeue_requires_a_selector(self):
        with tempfile.TemporaryDirectory() as d:
            out = subprocess.run([sys.executable, DEQUEUE, "--state-dir", d],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 2)


if __name__ == "__main__":
    unittest.main()
