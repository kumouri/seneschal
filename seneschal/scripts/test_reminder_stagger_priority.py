#!/usr/bin/env python3
"""Tests for a High Today Todo that goes stale in the morning drip: seeded into a 10-14-row 08:00
batch at position ~9-11, drained one row per ``sentinel.CATCHUP_STAGGER_SEC`` (15 min) in plain
seed-call order (no importance-aware ordering), its raw due-vs-now gap crosses ``MAX_LATENESS_SEC``
before its turn ever comes — every day, silently, since nothing outside `presence.log` recorded it.

Two fixes, tested here:
  1. The catch-up stagger's wait no longer counts as staleness — ``entry_lateness_sec`` nets out
     ``_stagger_held_sec`` the same way it already netted ``presence_held_sec``.
  2. The drain order is importance first, then seed order (``_due_sort_key``) — a High row can no
     longer queue behind a Low one just because it was seeded later in the same slot. Importance is
     compared in ANY backend's spelling (Notion's emoji labels or the canonical keys).

Stdlib ``unittest`` only. Every instant is explicit UTC, and the owner zone + identity are pinned (a
fixed UTC-5, so local = UTC - 5h), per the ``test_night_curfew.py`` convention.

Run:  python -m unittest seneschal.scripts.test_reminder_stagger_priority
      (or)   python test_reminder_stagger_priority.py
"""
import json
import os
import subprocess
import sys
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import clock  # noqa: E402
import identity_common  # noqa: E402
import reminder_suppressions as rs  # noqa: E402
import reminders_seed as seed  # noqa: E402
import sentinel as sn  # noqa: E402
import tz_common  # noqa: E402

CDT = timedelta(hours=-5)
CDT_TZ = timezone(CDT)   # a fixed UTC-5 owner zone — also reminders_seed's `now_local` offset
RID = "00000000-0000-0000-0000-000000000077"  # a placeholder ⏰ page id

_PATCHES = []


def setUpModule():
    """Pin the owner zone + identity for the whole module (curfew/activity-day readings must not
    depend on the runner's zone or a real persona/identity.json)."""
    for p in (mock.patch.object(tz_common, "_zone", return_value=CDT_TZ),
              mock.patch.object(clock, "load_identity", return_value={}),
              mock.patch.object(identity_common, "load_identity", return_value={})):
        p.start()
        _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _utc(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _cdt(y, mo, d, h, mi=0, s=0):
    """An owner wall-clock reading, as the UTC INSTANT it names — what sentinel's `now`/`due_at`
    args want. Not a stand-in for reminders_seed's `now_local`, which wants an aware datetime whose
    own tzinfo/offset IS the local zone (see `_local`, below)."""
    return _utc(y, mo, d, h, mi, s) - CDT


def _local(y, mo, d, h, mi=0, s=0):
    """An aware owner-local datetime — what `reminders_seed.seed_entries`'s `now_local` wants."""
    return datetime(y, mo, d, h, mi, s, tzinfo=CDT_TZ)


def _z(dt):
    return dt.isoformat().replace("+00:00", "Z")


class ImportanceSortUnit(unittest.TestCase):
    """`_due_sort_key` — pure function, no state dir, no clock."""

    def test_rank_table_matches_the_policy_order(self):
        self.assertLess(sn.IMPORTANCE_RANK["super-critical"], sn.IMPORTANCE_RANK["critical"])
        self.assertLess(sn.IMPORTANCE_RANK["critical"], sn.IMPORTANCE_RANK["high"])
        self.assertLess(sn.IMPORTANCE_RANK["high"], sn.IMPORTANCE_RANK["notable"])
        self.assertLess(sn.IMPORTANCE_RANK["notable"], sn.IMPORTANCE_RANK["low"])

    def test_any_backend_spelling_ranks_the_same(self):
        # The Notion emoji label and the canonical key are one level (store/notion/schema.template.md).
        for label, key in (("🛑 Super-Critical", "super-critical"), ("🚨 Critical", "critical"),
                           ("⭐ High", "high"), ("✨ Notable", "notable"), ("📌 Low", "low")):
            self.assertEqual(sn._importance_rank({"importance": label}),
                             sn._importance_rank({"importance": key}), label)

    def test_missing_importance_is_neutral_not_top_or_bottom(self):
        self.assertEqual(sn._importance_rank({}), sn.DEFAULT_IMPORTANCE_RANK)
        self.assertEqual(sn._importance_rank({"importance": "nonsense"}), sn.DEFAULT_IMPORTANCE_RANK)
        self.assertGreater(sn.DEFAULT_IMPORTANCE_RANK, sn.IMPORTANCE_RANK["high"])
        self.assertLess(sn.DEFAULT_IMPORTANCE_RANK, sn.IMPORTANCE_RANK["low"])

    def test_high_never_sorts_behind_low_at_the_same_due_at(self):
        due = _z(_cdt(2026, 9, 14, 8, 0))
        low = {"id": "low", "due_at": due, "importance": "📌 Low"}
        high = {"id": "high", "due_at": due, "importance": "⭐ High"}
        # `low` is seeded FIRST (earlier in the list) — insertion order alone would put it first too.
        ordered = sorted([low, high], key=sn._due_sort_key)
        self.assertEqual([r["id"] for r in ordered], ["high", "low"])

    def test_ties_on_due_and_importance_keep_seed_order(self):
        due = _z(_cdt(2026, 9, 14, 8, 0))
        rows = [{"id": f"low-{i}", "due_at": due, "importance": "📌 Low"} for i in range(5)]
        ordered = sorted(rows, key=sn._due_sort_key)
        self.assertEqual([r["id"] for r in ordered], [r["id"] for r in rows])

    def test_earlier_due_at_still_wins_over_importance(self):
        earlier = {"id": "earlier-low", "due_at": _z(_cdt(2026, 9, 14, 7, 0)), "importance": "📌 Low"}
        later = {"id": "later-critical", "due_at": _z(_cdt(2026, 9, 14, 8, 0)),
                 "importance": "🚨 Critical"}
        ordered = sorted([later, earlier], key=sn._due_sort_key)
        self.assertEqual([r["id"] for r in ordered], ["earlier-low", "later-critical"])


class StaggerLatenessNettingUnit(unittest.TestCase):
    """`_stagger_held_sec` / `entry_lateness_sec` — pure functions, no fire-path simulation."""

    def test_no_stamp_means_no_stagger_credit(self):
        now, due = _cdt(2026, 9, 14, 10, 0), _cdt(2026, 9, 14, 8, 0)
        self.assertEqual(sn._stagger_held_sec({}, now), 0.0)
        self.assertEqual(sn.entry_lateness_sec({}, due, now), 7200)

    def test_stagger_wait_is_netted_out(self):
        now, due = _cdt(2026, 9, 14, 11, 0), _cdt(2026, 9, 14, 8, 0)  # 3 h raw
        opened = _cdt(2026, 9, 14, 9, 0)  # started waiting at 09:00 → 2 h of stagger credit
        entry = {"stagger_deferred_since": _z(opened)}
        self.assertEqual(sn._stagger_held_sec(entry, now), 2 * 3600)
        self.assertEqual(sn.entry_lateness_sec(entry, due, now), 3600)

    def test_presence_and_stagger_credit_both_apply(self):
        now, due = _cdt(2026, 9, 14, 14, 0), _cdt(2026, 9, 14, 8, 0)  # 6 h raw
        entry = {"presence_held_sec": 2 * 3600,
                 "stagger_deferred_since": _z(_cdt(2026, 9, 14, 13, 0))}  # +1 h
        self.assertEqual(sn.entry_lateness_sec(entry, due, now), 3 * 3600)

    def test_garbled_stagger_stamp_costs_the_credit_not_the_nudge(self):
        now, due = _cdt(2026, 9, 14, 10, 0), _cdt(2026, 9, 14, 8, 0)
        entry = {"stagger_deferred_since": "not-a-date"}
        self.assertEqual(sn._stagger_held_sec(entry, now), 0.0)
        self.assertEqual(sn.entry_lateness_sec(entry, due, now), 7200)


class SeedEntriesCarryImportance(unittest.TestCase):
    """`reminders_seed.seed_entries` must put `importance` on the queue entry — otherwise
    `sentinel._due_sort_key` has nothing to sort on and the whole fix is inert."""

    def test_importance_is_carried_onto_the_entry(self):
        row = {"reminder_id": RID, "text": "Renew the passport.",
               "times": "08:00", "importance": "⭐ High", "slug": "renew-passport"}
        entries = seed.seed_entries(row, _local(2026, 9, 14, 0, 5))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["importance"], "⭐ High")

    def test_no_importance_means_no_key_at_all(self):
        row = {"reminder_id": "x", "text": "Water plants.", "times": "08:00", "slug": "water"}
        entries = seed.seed_entries(row, _local(2026, 9, 14, 0, 5))
        self.assertEqual(len(entries), 1)
        self.assertNotIn("importance", entries[0])


class EnqueueCliCarriesImportance(unittest.TestCase):
    """`reminders_enqueue.py --importance` — the ad-hoc-nudge door's parity with the seed path."""

    ENQUEUE = os.path.join(SCRIPT_DIR, "reminders_enqueue.py")

    def test_importance_round_trips_into_the_queue_entry(self):
        with tempfile.TemporaryDirectory() as d:
            out = subprocess.run(
                [sys.executable, self.ENQUEUE, "--state-dir", d, "--text", "Renew the passport.",
                 "--due-at", "2026-09-14T13:00:00Z", "--id", "rmd-t-renew-passport",
                 "--importance", "⭐ High"],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            with open(os.path.join(d, "reminders.json"), encoding="utf-8") as fh:
                rows = json.load(fh)
            self.assertEqual(rows[0]["importance"], "⭐ High")

    def test_omitting_importance_writes_no_key(self):
        with tempfile.TemporaryDirectory() as d:
            out = subprocess.run(
                [sys.executable, self.ENQUEUE, "--state-dir", d, "--text", "Water plants.",
                 "--due-at", "2026-09-14T13:00:00Z", "--id", "rmd-t-water"],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            with open(os.path.join(d, "reminders.json"), encoding="utf-8") as fh:
                rows = json.load(fh)
            self.assertNotIn("importance", rows[0])


class _GateHarness(unittest.TestCase):
    """Shared fixture: a temp state dir with delivery mocked to succeed (matches test_night_curfew.py)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="stagger-priority-test-")
        self.delivered = []
        self._orig = sn._deliver_reminder
        sn._deliver_reminder = lambda ch, raw, *a, **k: (
            self.delivered.append(raw) or ({"ok": True}, "telegram"))

    def tearDown(self):
        sn._deliver_reminder = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rows):
        sn.save_json(os.path.join(self.dir, "reminders.json"), rows)

    def rows(self):
        return {r["id"]: r for r in sn.load_json(os.path.join(self.dir, "reminders.json"), [])}

    def run_at(self, now):
        return sn.check_reminders(self.dir, now, fire=True, telegram_env="x")

    def kinds(self, signals):
        return {s["id"]: s["kind"] for s in signals if "id" in s}

    def drain(self, start, end, step_min=5):
        """Run the real fire path every `step_min` minutes. Returns (fires, suppressions), each a
        mapping id -> the UTC instant it resolved at — the same shape test_night_curfew.py's
        NightIncidentReplay uses."""
        fires, suppressions = {}, {}
        t = start
        while t <= end:
            for s in self.run_at(t):
                if s["kind"] == "reminder_fired":
                    fires.setdefault(s["id"], t)
                elif s["kind"].startswith("reminder_suppressed_"):
                    suppressions.setdefault(s["id"], (s["kind"], t))
            t += timedelta(minutes=step_min)
        return fires, suppressions


class TwelveRowBatchHighSeededTenth(_GateHarness):
    """**The reproduction, fixed.** 12 rows due 08:00; the High row is seeded 10th (index 9) of 12,
    behind rows the seed pass happened to call the seed script for first — seed-call order carries no
    relationship to importance at all."""

    DUE = _cdt(2026, 9, 14, 8, 0)
    END = _cdt(2026, 9, 14, 11, 0)  # 3 h past due — well past the OLD MAX_LATENESS_SEC=2h cutoff

    def _batch(self):
        rows = []
        for i in range(9):
            rows.append({"id": f"low-{i}", "text": f"Low priority chore {i}.",
                        "due_at": _z(self.DUE), "channel": "telegram", "fired_at": None,
                        "importance": "📌 Low"})
        rows.append({"id": "renew-passport", "text": "Renew the passport.", "due_at": _z(self.DUE),
                    "channel": "telegram", "fired_at": None, "importance": "⭐ High",
                    "reminder_id": RID})
        for i in range(2):
            rows.append({"id": f"low-tail-{i}", "text": f"Low priority chore tail {i}.",
                        "due_at": _z(self.DUE), "channel": "telegram", "fired_at": None,
                        "importance": "📌 Low"})
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[9]["id"], "renew-passport")  # really is 10th in seed/insertion order
        return rows

    def test_renew_passport_fires_and_never_goes_stale(self):
        self.write(self._batch())
        fires, suppressions = self.drain(self.DUE, self.END)
        self.assertIn("renew-passport", fires, "the High row must fire, not go stale behind the Low ones")
        self.assertNotIn("renew-passport", suppressions)
        # And it fires promptly — reordered to the front of the drip, not merely spared at the end.
        self.assertLess(fires["renew-passport"] - self.DUE, timedelta(minutes=30))

    def test_no_suppression_ledger_row_for_the_renew_passport_entry(self):
        self.write(self._batch())
        self.drain(self.DUE, self.END)
        rows = rs.tail(self.dir, 50)
        self.assertNotIn("renew-passport", [r.get("id") for r in rows])


class PureFifoBatchStaggerNetting(_GateHarness):
    """**Fix (1) in isolation, no Importance at all** — the exact bug shape before any ordering fix:
    12 same-priority rows, oldest-due-first tie-break is plain seed order, and the row at position 10
    would have gone stale under the OLD code (no stagger netting): 9 rows ahead of it drain at
    15 min each = 135 min, but the OLD 2 h cutoff fires at 120 min — 15 minutes too soon."""

    DUE = _cdt(2026, 9, 18, 8, 0)
    END = _cdt(2026, 9, 18, 11, 0)

    def _batch(self):
        return [{"id": f"row-{i}", "text": f"Row {i}.", "due_at": _z(self.DUE),
                "channel": "telegram", "fired_at": None} for i in range(12)]

    def test_row_at_position_ten_still_fires(self):
        self.write(self._batch())
        fires, suppressions = self.drain(self.DUE, self.END)
        self.assertIn("row-9", fires, "position 10 (0-indexed 9) must survive the wait")
        self.assertNotIn("row-9", suppressions)
        # It really did wait roughly its queue position's worth of drip (~9 * 15 min), not fire early.
        self.assertGreaterEqual(fires["row-9"] - self.DUE, timedelta(minutes=125))

    def test_stagger_deferred_since_is_cleared_once_it_fires(self):
        self.write(self._batch())
        self.drain(self.DUE, self.END)
        row = self.rows()["row-9"]
        self.assertTrue(row["fired_at"])
        self.assertNotIn("stagger_deferred_since", row)

    def test_every_row_eventually_fires_none_goes_stale(self):
        self.write(self._batch())
        fires, suppressions = self.drain(self.DUE, self.END)
        self.assertEqual(len(fires), 12)
        self.assertEqual(suppressions, {})


class AlreadyStaleBeatsTheQueue(_GateHarness):
    """The netting must not create a hiding place: an entry already > MAX_LATENESS_SEC late the very
    first time it reaches the stagger gate has an empty (just-opened) segment and dies right here —
    matching test_night_curfew.py's `test_stale_beats_stagger`, re-asserted against the new code path."""

    def test_already_three_hours_late_is_suppressed_even_though_stagger_would_hold_it(self):
        now = _cdt(2026, 9, 14, 14, 0)
        due = now - timedelta(hours=3)
        sn.save_json(os.path.join(self.dir, "nudge-stagger.json"),
                     {"last_nonpiercing_fire": _z(now - timedelta(minutes=1))})
        self.write([{"id": "stale", "text": "stale", "due_at": _z(due), "channel": "telegram",
                    "fired_at": None}])
        signals = self.run_at(now)
        self.assertEqual(self.kinds(signals)["stale"], "reminder_suppressed_stale")
        # Consumed at the staleness gate, which runs BEFORE the stagger gate stamps anything — the
        # row never reaches that code at all, so no segment is ever opened on it.
        self.assertNotIn("stagger_deferred_since", self.rows()["stale"])


class StaleSuppressionWritesTheLedger(_GateHarness):
    """Requirement (3): every staleness suppression is durably visible outside `presence.log`."""

    def test_suppression_appends_a_ledger_row(self):
        now = _cdt(2026, 9, 14, 14, 0)
        due = now - timedelta(hours=2, minutes=1)
        self.write([{"id": "renew-passport", "text": "Renew the passport.", "due_at": _z(due),
                    "channel": "telegram", "fired_at": None,
                    "reminder_id": RID}])
        signals = self.run_at(now)
        self.assertEqual(self.kinds(signals)["renew-passport"], "reminder_suppressed_stale")
        rows = rs.tail(self.dir, 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "renew-passport")
        self.assertEqual(rows[0]["reminder_id"], RID)
        self.assertEqual(rows[0]["text"], "Renew the passport.")
        self.assertGreater(rows[0]["late_sec"], 0)

    def test_a_fired_reminder_writes_no_ledger_row(self):
        now = _cdt(2026, 9, 14, 8, 0)
        self.write([{"id": "fresh", "text": "fresh", "due_at": _z(now), "channel": "telegram",
                    "fired_at": None}])
        self.run_at(now)
        self.assertEqual(rs.tail(self.dir, 10), [])


if __name__ == "__main__":
    unittest.main()
