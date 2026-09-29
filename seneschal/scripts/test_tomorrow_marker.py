#!/usr/bin/env python3
"""Tests for `tomorrow_marker.py` (`../docs/tomorrow-marker-spec.md` §7, all three phases).

Covers: mark/close/roll/drop, append ordering, the never-auto-expire rule (an `open` item survives a
simulated prune pass untouched), a bare chat-tag mark producing `linked_kind: None` and no Reminders-
side effect, the Brief/Wrap renders, the two Wrap-time pickers' apply functions (Close/Roll/Drop; the
"what's tomorrow's lead?" multi-select), and Phase 2's idempotent Notion reconciliation.
`presence.py`'s dispatch wiring for the two pickers is `test_tomorrow_marker_wiring.py`.

Run: ``python -m unittest test_tomorrow_marker``
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import clock  # noqa: E402
import tomorrow_marker as tm  # noqa: E402
import tz_common  # noqa: E402

# A Tuesday, well clear of the after-midnight cut, so `_tomorrow()` is unambiguous: 2026-09-23.
NOW = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)
TODAY = "2026-09-22"
TOMORROW = "2026-09-23"

# The owner's zone is PINNED to a fixed UTC-5 (no tzdata) and the identity to the default 05:00
# boundary, so the activity-day arithmetic is the same on every machine and in CI.
OWNER_ZONE = timezone(timedelta(hours=-5))

# 01:00 local Tuesday (06:00Z at UTC-5) — after-midnight, so the activity day is still Monday and
# "tomorrow" means Monday's tomorrow (Tuesday), per spec §2.1.
AFTER_MIDNIGHT = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        for patcher in (mock.patch.object(tz_common, "_zone", return_value=OWNER_ZONE),
                        mock.patch.object(clock, "load_identity", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)


class MarkAndForDate(Base):
    def test_default_for_date_is_the_after_midnight_tomorrow(self):
        item = tm.mark(self.state, "ship the spec", now=NOW)
        self.assertEqual(item["for_date"], TOMORROW)

    def test_after_midnight_mark_still_means_the_prior_days_tomorrow(self):
        item = tm.mark(self.state, "x", now=AFTER_MIDNIGHT)
        # Activity day at 06:00Z (01:00 local) is still Monday 2026-09-21; tomorrow is Tuesday.
        self.assertEqual(item["for_date"], "2026-09-22")

    def test_bare_chat_tag_mark_has_no_link(self):
        item = tm.mark(self.state, "just a plan", now=NOW)
        self.assertIsNone(item["linked_kind"])
        self.assertIsNone(item["linked_page_id"])
        self.assertEqual(item["status"], "open")
        self.assertIsNone(item["resolved_at"])

    def test_empty_text_refused(self):
        with self.assertRaises(tm.TomorrowMarkerError):
            tm.mark(self.state, "   ", now=NOW)

    def test_unrecognized_source_refused(self):
        with self.assertRaises(tm.TomorrowMarkerError):
            tm.mark(self.state, "x", source="carrier_pigeon", now=NOW)

    def test_append_ordering_is_sequential_per_for_date(self):
        a = tm.mark(self.state, "first", now=NOW)
        b = tm.mark(self.state, "second", now=NOW)
        self.assertEqual(a["order"], 1)
        self.assertEqual(b["order"], 2)

    def test_ordering_is_independent_per_for_date(self):
        tm.mark(self.state, "a", for_date="2026-10-01", now=NOW)
        first_other_day = tm.mark(self.state, "b", for_date="2026-10-02", now=NOW)
        self.assertEqual(first_other_day["order"], 1)


class Resolution(Base):
    def _item(self):
        return tm.mark(self.state, "ship it", now=NOW)

    def test_close_marks_done(self):
        item = self._item()
        res = tm.close(self.state, item["id"], now=NOW)
        self.assertEqual(res["status"], "done")
        self.assertIsNotNone(res["item"]["resolved_at"])
        self.assertNotIn("proposal", res)

    def test_close_on_linked_item_returns_a_proposal_never_writes_notion(self):
        item = tm.mark(self.state, "flip the task", linked_kind="task",
                       linked_page_id="page-123", now=NOW)
        res = tm.close(self.state, item["id"], now=NOW)
        self.assertIn("proposal", res)
        self.assertIn("page-123", res["proposal"])

    def test_roll_advances_for_date_keeps_order(self):
        item = self._item()
        res = tm.roll(self.state, item["id"], now=NOW)
        self.assertEqual(res["status"], "rolled")
        self.assertEqual(res["item"]["for_date"], "2026-09-24")
        self.assertEqual(res["item"]["order"], item["order"])

    def test_drop_costs_nothing_no_proposal(self):
        item = tm.mark(self.state, "x", linked_kind="reminder", linked_page_id="p1", now=NOW)
        res = tm.drop(self.state, item["id"], now=NOW)
        self.assertEqual(res["status"], "dropped")
        self.assertNotIn("proposal", res)

    def test_unknown_id_refused(self):
        with self.assertRaises(tm.TomorrowMarkerError):
            tm.close(self.state, "tmw-nope", now=NOW)

    def test_resolving_a_terminal_item_again_refused(self):
        item = self._item()
        tm.close(self.state, item["id"], now=NOW)
        with self.assertRaises(tm.TomorrowMarkerError):
            tm.drop(self.state, item["id"], now=NOW)


class NeverAutoExpires(Base):
    """Spec §3/§6.2: an `open`/`rolled` item is never pruned by age, only a resolved terminal one."""

    def test_open_item_survives_a_prune_pass_far_in_the_future(self):
        item = tm.mark(self.state, "still open", now=NOW)
        far_future = datetime(2027, 1, 1, tzinfo=timezone.utc)
        dropped = tm.prune(self.state, now=far_future, keep_days=3)
        self.assertEqual(dropped, 0)
        self.assertIsNotNone(tm.get(tm.load(self.state), item["id"]))

    def test_rolled_item_also_survives(self):
        item = tm.mark(self.state, "rolled along", now=NOW)
        tm.roll(self.state, item["id"], now=NOW)
        dropped = tm.prune(self.state, now=datetime(2027, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(dropped, 0)

    def test_a_done_item_ages_out_after_keep_days(self):
        item = tm.mark(self.state, "finished", now=NOW)
        tm.close(self.state, item["id"], now=NOW)
        just_past = NOW.replace(day=NOW.day) + tm.timedelta(days=4)
        dropped = tm.prune(self.state, now=just_past, keep_days=3)
        self.assertEqual(dropped, 1)
        self.assertIsNone(tm.get(tm.load(self.state), item["id"]))

    def test_a_done_item_within_keep_days_survives(self):
        item = tm.mark(self.state, "finished", now=NOW)
        tm.close(self.state, item["id"], now=NOW)
        soon = NOW + tm.timedelta(days=1)
        dropped = tm.prune(self.state, now=soon, keep_days=3)
        self.assertEqual(dropped, 0)


class BriefLine(Base):
    def test_empty_renders_none(self):
        self.assertIsNone(tm.brief_line(self.state, for_date=TODAY))

    def test_renders_open_and_rolled_in_order(self):
        tm.mark(self.state, "first", for_date=TODAY, now=NOW)
        tm.mark(self.state, "second", for_date=TODAY, now=NOW)
        line = tm.brief_line(self.state, for_date=TODAY)
        self.assertIn(tm.BRIEF_HEADING, line)
        self.assertLess(line.index("first"), line.index("second"))

    def test_a_done_item_is_omitted(self):
        item = tm.mark(self.state, "closed already", for_date=TODAY, now=NOW)
        tm.close(self.state, item["id"], now=NOW)
        self.assertIsNone(tm.brief_line(self.state, for_date=TODAY))

    def test_items_for_other_days_are_not_shown(self):
        tm.mark(self.state, "not today", for_date="2026-12-25", now=NOW)
        self.assertIsNone(tm.brief_line(self.state, for_date=TODAY))


class RenderForWrap(Base):
    def test_shape_is_id_and_label(self):
        item = tm.mark(self.state, "wrap me", for_date=TODAY, now=NOW)
        rows = tm.render_for_wrap(self.state, for_date=TODAY)
        self.assertEqual(rows, [{"id": item["id"], "label": "wrap me"}])

    def test_empty_when_nothing_due_today(self):
        self.assertEqual(tm.render_for_wrap(self.state, for_date=TODAY), [])


class ApplyWrapGrid(Base):
    def test_close_roll_drop_all_apply(self):
        a = tm.mark(self.state, "a", for_date=TODAY, now=NOW)
        b = tm.mark(self.state, "b", for_date=TODAY, now=NOW)
        c = tm.mark(self.state, "c", for_date=TODAY, now=NOW)
        out = tm.apply_wrap_grid(self.state, {
            a["id"]: "Close (done)", b["id"]: "Roll to tomorrow", c["id"]: "Drop",
        }, now=NOW)
        self.assertEqual(out["results"][a["id"]]["status"], "done")
        self.assertEqual(out["results"][b["id"]]["status"], "rolled")
        self.assertEqual(out["results"][c["id"]]["status"], "dropped")
        self.assertEqual(out["proposals"], [])

    def test_a_linked_close_produces_a_proposal_line(self):
        item = tm.mark(self.state, "linked", for_date=TODAY, linked_kind="task",
                       linked_page_id="pg1", now=NOW)
        out = tm.apply_wrap_grid(self.state, {item["id"]: "Close (done)"}, now=NOW)
        self.assertEqual(len(out["proposals"]), 1)

    def test_untouched_row_left_alone(self):
        a = tm.mark(self.state, "left alone", for_date=TODAY, now=NOW)
        tm.apply_wrap_grid(self.state, {}, now=NOW)
        self.assertEqual(tm.get(tm.load(self.state), a["id"])["status"], "open")

    def test_an_unrecognized_choice_is_a_per_item_error_not_an_abort(self):
        a = tm.mark(self.state, "a", for_date=TODAY, now=NOW)
        b = tm.mark(self.state, "b", for_date=TODAY, now=NOW)
        out = tm.apply_wrap_grid(self.state, {a["id"]: "Maybe?", b["id"]: "Drop"}, now=NOW)
        self.assertFalse(out["results"][a["id"]]["ok"])
        self.assertEqual(out["results"][b["id"]]["status"], "dropped")


class LeadAnswer(Base):
    def test_selected_candidates_are_marked_wrap_picker(self):
        candidates = [{"id": "pg1", "text": "Finish deck", "why": "due tomorrow", "linked_kind": "task"},
                      {"id": None, "text": "Call the vet", "why": "mentioned twice"}]
        out = tm.apply_lead_answer(self.state, {"candidates": candidates}, [0], now=NOW)
        self.assertEqual(len(out["marked"]), 1)
        self.assertEqual(out["marked"][0]["source"], "wrap_picker")
        self.assertEqual(out["marked"][0]["linked_page_id"], "pg1")

    def test_out_of_range_index_is_skipped_not_raised(self):
        out = tm.apply_lead_answer(self.state, {"candidates": [{"text": "x"}]}, [0, 5], now=NOW)
        self.assertEqual(len(out["marked"]), 1)


class Reconcile(Base):
    def test_a_fresh_row_is_upserted_and_reported_for_untick(self):
        out = tm.reconcile(self.state, "task", [{"page_id": "pg1", "text": "Ship it"}], now=NOW)
        self.assertEqual(len(out["upserted"]), 1)
        self.assertEqual(out["untick"], ["pg1"])
        item = tm.get(tm.load(self.state), out["upserted"][0])
        self.assertEqual(item["source"], "notion_task")
        self.assertEqual(item["linked_page_id"], "pg1")

    def test_running_twice_never_duplicates(self):
        rows = [{"page_id": "pg1", "text": "Ship it"}]
        tm.reconcile(self.state, "task", rows, now=NOW)
        out2 = tm.reconcile(self.state, "task", rows, now=NOW)
        self.assertEqual(out2["upserted"], [])
        self.assertEqual(out2["untick"], ["pg1"])  # still told to untick — the box may still be true
        self.assertEqual(len(tm.load(self.state)["items"]), 1)

    def test_a_terminal_prior_marker_does_not_block_a_fresh_one(self):
        first = tm.mark(self.state, "old", linked_kind="task", linked_page_id="pg1", now=NOW)
        tm.close(self.state, first["id"], now=NOW)
        out = tm.reconcile(self.state, "task", [{"page_id": "pg1", "text": "re-flagged"}], now=NOW)
        self.assertEqual(len(out["upserted"]), 1)

    def test_empty_rows_fails_open(self):
        out = tm.reconcile(self.state, "reminder", [], now=NOW)
        self.assertEqual(out, {"ok": True, "upserted": [], "untick": []})

    def test_a_row_missing_page_id_is_skipped(self):
        out = tm.reconcile(self.state, "task", [{"text": "no id here"}], now=NOW)
        self.assertEqual(out["upserted"], [])
        self.assertEqual(out["untick"], [])

    def test_bad_source_kind_refused(self):
        with self.assertRaises(tm.TomorrowMarkerError):
            tm.reconcile(self.state, "idea", [{"page_id": "x"}], now=NOW)


class Cli(Base):
    def test_mark_and_list_roundtrip(self):
        rc = tm.main(["--state-dir", self.state, "mark", "hello", "--for-date", TODAY])
        self.assertEqual(rc, 0)
        rc = tm.main(["--state-dir", self.state, "list", "--for-date", TODAY])
        self.assertEqual(rc, 0)

    def test_close_unknown_id_exits_2(self):
        rc = tm.main(["--state-dir", self.state, "close", "tmw-nope"])
        self.assertEqual(rc, 2)

    def test_prune_cli_drops_old_terminal_leaves_open_and_rolled_alone(self):
        """Dream calls `prune` through this CLI, never the bare function — the invariant has to hold
        there too: an `open`/`rolled` item survives untouched, only an aged-out `done`/`dropped` row
        goes (spec §3 Retention)."""
        open_item = tm.mark(self.state, "still open", now=NOW)
        rolled_item = tm.mark(self.state, "rolled along", now=NOW)
        tm.roll(self.state, rolled_item["id"], now=NOW)
        done_item = tm.mark(self.state, "finished", now=NOW)
        tm.close(self.state, done_item["id"], now=NOW)
        # Backdate the done item's resolution past the keep-days window — real wall-clock prune()
        # would otherwise need the test to wait 3 real days.
        store = tm.load(self.state)
        tm.get(store, done_item["id"])["resolved_at"] = "2020-01-01T00:00:00Z"
        tm.save(self.state, store)

        rc = tm.main(["--state-dir", self.state, "prune", "--keep-days", "3"])
        self.assertEqual(rc, 0)

        survivors = tm.load(self.state)
        self.assertIsNotNone(tm.get(survivors, open_item["id"]))
        self.assertIsNotNone(tm.get(survivors, rolled_item["id"]))
        self.assertIsNone(tm.get(survivors, done_item["id"]))

    def test_prune_cli_on_missing_store_is_a_no_op(self):
        """Dream's own fail-open contract: an absent `tomorrow.json` must never make the prune call
        (or the Dream run) fail."""
        empty_state = os.path.join(self.state, "no-such-dir-yet")
        rc = tm.main(["--state-dir", empty_state, "prune"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
