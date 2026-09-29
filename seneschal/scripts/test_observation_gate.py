#!/usr/bin/env python3
"""Tests for `observation_gate.py` — `../docs/observation-gate-spec.md`.

**What this suite is guarding.**

* **`CheckRequirement`** — each requirement type's own arithmetic, and the fail-open contract: an
  unreadable source, a missing job record, or an unparseable timestamp must all report `met: None`
  (unknown), never `True`. A regression here is a look that silently under-consulted its source
  and answered anyway — confidently wrong.
* **`CheckGate`** — a gate is met only when EVERY requirement is met; one unknown/unmet requirement
  holds the whole gate, even alongside requirements that are.
* **`TheScanner`** — idempotent (a second `scan(apply=True)` against an already-flipped item is a
  no-op), never touches anything but a `status == "observation"` row, and never mutates
  `open-loops.json` directly — every effect is `loops.mark_observation_complete`/`loops.raise_item`.
* **`Staleness`** — a gate met more than `STALE_AFTER_DAYS` ago with nothing touching the record
  since is flagged; one that was followed up (started, resolved, anything moving `last_touched`
  past `met_at`) is not.
* **`BriefLine`** — the Brief's lead-with text, capped, and `None` when there is nothing ready.
* **`RequirementValidation`** — SQL-identifier safety for the `min_rows` sqlite door: a filter key or
  table name that isn't a plain identifier must never reach string interpolation.

Every test passes an explicit `--state-dir`/`state_dir`, same convention as `test_loops.py`.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import loops  # noqa: E402
import observation_gate as og  # noqa: E402

GOOD = dict(
    text="watch the rollout's phase-2 gate",
    audience="owner",
    terminal_state="phase 2 either ships or the owner says drop it",
    whose_move="assistant",
    next_action="watch the gate",
    kind="enhancement",
    priority="normal",
)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def add(self, **over):
        kwargs = dict(GOOD)
        kwargs.update(over)
        return loops.add(self.state, **kwargs)

    def observe(self, item_id, requires, **kwargs):
        return loops.observe(self.state, item_id=item_id, requires=requires, **kwargs)

    def jsonl(self, name: str, rows: list) -> str:
        path = os.path.join(self.state, name)
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return path


class CheckRequirement(Base):
    def test_min_elapsed_met(self):
        started = (loops._now() - timedelta(days=20)).isoformat(timespec="seconds")
        r = og.check_requirement({"type": "min_elapsed", "days": 14}, started_at=started,
                                 state_dir=self.state, now=loops._now())
        self.assertTrue(r["met"])

    def test_min_elapsed_not_yet(self):
        started = (loops._now() - timedelta(days=2)).isoformat(timespec="seconds")
        r = og.check_requirement({"type": "min_elapsed", "days": 14}, started_at=started,
                                 state_dir=self.state, now=loops._now())
        self.assertFalse(r["met"])

    def test_min_elapsed_unknown_on_unreadable_started_at(self):
        r = og.check_requirement({"type": "min_elapsed", "days": 14}, started_at=None,
                                 state_dir=self.state, now=loops._now())
        self.assertIsNone(r["met"])

    def test_min_elapsed_since_reads_met_immediately_even_with_a_fresh_started_at(self):
        # `started_at` stamped THIS INSTANT (as `observe()` always does), but the requirement's own
        # `since` backdates it 20 days — the exact shape of a backfilled gate attached the day its
        # evidence period is already long past its bar.
        now = loops._now()
        since = (now - timedelta(days=20)).isoformat(timespec="seconds")
        r = og.check_requirement({"type": "min_elapsed", "days": 14, "since": since},
                                 started_at=now.isoformat(timespec="seconds"),
                                 state_dir=self.state, now=now)
        self.assertTrue(r["met"])
        self.assertIn("since", r["detail"])

    def test_min_elapsed_since_not_yet(self):
        now = loops._now()
        since = (now - timedelta(days=2)).isoformat(timespec="seconds")
        r = og.check_requirement({"type": "min_elapsed", "days": 14, "since": since},
                                 started_at=now.isoformat(timespec="seconds"),
                                 state_dir=self.state, now=now)
        self.assertFalse(r["met"])

    def test_min_elapsed_without_since_is_unchanged(self):
        started = (loops._now() - timedelta(days=20)).isoformat(timespec="seconds")
        r = og.check_requirement({"type": "min_elapsed", "days": 14}, started_at=started,
                                 state_dir=self.state, now=loops._now())
        self.assertTrue(r["met"])
        self.assertIn("started_at", r["detail"])

    def test_min_rows_met(self):
        path = self.jsonl("log.jsonl", [{"kind": "arrival", "typed": True}] * 30)
        r = og.check_requirement(
            {"type": "min_rows", "source": path, "filter": {"kind": "arrival", "typed": True},
             "n": 30}, started_at=None, state_dir=self.state, now=loops._now())
        self.assertTrue(r["met"])

    def test_min_rows_not_enough(self):
        path = self.jsonl("log.jsonl", [{"kind": "arrival"}] * 5)
        r = og.check_requirement(
            {"type": "min_rows", "source": path, "filter": {"kind": "arrival"}, "n": 30},
            started_at=None, state_dir=self.state, now=loops._now())
        self.assertFalse(r["met"])

    def test_min_rows_unknown_on_missing_source(self):
        r = og.check_requirement(
            {"type": "min_rows", "source": os.path.join(self.state, "nope.jsonl"), "n": 1},
            started_at=None, state_dir=self.state, now=loops._now())
        self.assertIsNone(r["met"])

    def test_min_rows_skips_one_bad_line_rather_than_failing_the_whole_file(self):
        path = os.path.join(self.state, "log.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "arrival"}) + "\n")
            fh.write("not json at all\n")
            fh.write(json.dumps({"kind": "arrival"}) + "\n")
        r = og.check_requirement(
            {"type": "min_rows", "source": path, "filter": {"kind": "arrival"}, "n": 2},
            started_at=None, state_dir=self.state, now=loops._now())
        self.assertTrue(r["met"])

    def test_file_exists_met(self):
        path = os.path.join(self.state, "sentinel.txt")
        open(path, "w").close()
        r = og.check_requirement({"type": "file_exists", "path": path}, started_at=None,
                                 state_dir=self.state, now=loops._now())
        self.assertTrue(r["met"])

    def test_file_exists_not_met(self):
        r = og.check_requirement(
            {"type": "file_exists", "path": os.path.join(self.state, "nope.txt")},
            started_at=None, state_dir=self.state, now=loops._now())
        self.assertFalse(r["met"])

    def test_job_finished_unknown_on_missing_record(self):
        r = og.check_requirement({"type": "job_finished", "job_id": "20260101-000000-aaaa"},
                                 started_at=None, state_dir=self.state, now=loops._now())
        self.assertIsNone(r["met"])

    def test_job_finished_met_when_terminal(self):
        import jobs
        os.makedirs(jobs.jobs_dir(self.state), exist_ok=True)
        rec = {"id": "job-1", "status": jobs.DONE}
        with open(jobs.job_path(self.state, "job-1"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        r = og.check_requirement({"type": "job_finished", "job_id": "job-1"}, started_at=None,
                                 state_dir=self.state, now=loops._now())
        self.assertTrue(r["met"])

    def test_job_finished_not_met_while_running(self):
        import jobs
        os.makedirs(jobs.jobs_dir(self.state), exist_ok=True)
        rec = {"id": "job-2", "status": jobs.RUNNING}
        with open(jobs.job_path(self.state, "job-2"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        r = og.check_requirement({"type": "job_finished", "job_id": "job-2"}, started_at=None,
                                 state_dir=self.state, now=loops._now())
        self.assertFalse(r["met"])

    def test_manual_is_never_auto_satisfied(self):
        r = og.check_requirement({"type": "manual", "note": "did the decision land?"},
                                 started_at=None, state_dir=self.state, now=loops._now())
        self.assertIsNone(r["met"])
        self.assertIn("did the decision land?", r["detail"])


class RequirementValidation(Base):
    """`_safe_identifier` — the SQL-injection guard for `min_rows`'s sqlite door."""

    def test_unsafe_table_name_is_refused_to_unknown_not_executed(self):
        count = og._count_sqlite_rows(os.path.join(self.state, "x.sqlite"),
                                      "rows; DROP TABLE rows;--", {})
        self.assertIsNone(count)

    def test_unsafe_filter_key_is_refused_to_unknown(self):
        import sqlite3
        path = os.path.join(self.state, "x.sqlite")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE rows (a TEXT)")
        conn.commit()
        conn.close()
        count = og._count_sqlite_rows(path, "rows", {"a=1 OR 1=1;--": "x"})
        self.assertIsNone(count)

    def test_a_safe_table_and_filter_count_correctly(self):
        import sqlite3
        path = os.path.join(self.state, "x.sqlite")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE rows (kind TEXT)")
        conn.executemany("INSERT INTO rows VALUES (?)", [("a",)] * 3 + [("b",)] * 2)
        conn.commit()
        conn.close()
        self.assertEqual(og._count_sqlite_rows(path, "rows", {"kind": "a"}), 3)


class CheckGate(Base):
    def test_met_only_when_every_requirement_is_met(self):
        gate = {"started_at": (loops._now() - timedelta(days=20)).isoformat(
            timespec="seconds"),
               "requires": [{"type": "min_elapsed", "days": 14}, {"type": "manual", "note": "?"}]}
        result = og.check_gate(gate, state_dir=self.state)
        self.assertFalse(result["met"])  # the manual requirement is always unknown

    def test_met_true_when_every_requirement_reports_true(self):
        gate = {"started_at": (loops._now() - timedelta(days=20)).isoformat(
            timespec="seconds"),
               "requires": [{"type": "min_elapsed", "days": 14}]}
        result = og.check_gate(gate, state_dir=self.state)
        self.assertTrue(result["met"])

    def test_an_empty_requires_list_is_never_met(self):
        result = og.check_gate({"started_at": None, "requires": []}, state_dir=self.state)
        self.assertFalse(result["met"])


class TheScanner(Base):
    def test_scan_dry_run_writes_nothing(self):
        item = self.add()
        self.observe(item["id"], [{"type": "min_elapsed", "days": 0}])
        result = og.scan(self.state, apply=False)
        self.assertEqual(result["flipped"], [])
        store = loops.load(self.state)
        self.assertEqual(store["items"][item["id"]]["status"], "observation")

    def test_scan_apply_flips_a_fully_met_gate(self):
        item = self.add()
        self.observe(item["id"], [{"type": "min_elapsed", "days": 0}])
        result = og.scan(self.state, apply=True)
        self.assertEqual(result["flipped"], [item["id"]])
        store = loops.load(self.state)
        self.assertEqual(store["items"][item["id"]]["status"], "observation-complete")

    def test_scan_apply_raises_the_item(self):
        item = self.add()
        self.observe(item["id"], [{"type": "min_elapsed", "days": 0}])
        og.scan(self.state, apply=True)
        store = loops.load(self.state)
        self.assertIsNotNone(store["items"][item["id"]]["last_raised_at"])

    def test_scan_leaves_an_unmet_gate_alone(self):
        item = self.add()
        self.observe(item["id"], [{"type": "min_elapsed", "days": 30}])
        og.scan(self.state, apply=True)
        store = loops.load(self.state)
        self.assertEqual(store["items"][item["id"]]["status"], "observation")

    def test_scan_never_touches_a_non_observation_row(self):
        item = self.add()  # plain open, no gate at all
        og.scan(self.state, apply=True)
        store = loops.load(self.state)
        self.assertEqual(store["items"][item["id"]]["status"], "open")

    def test_scan_is_idempotent(self):
        item = self.add()
        self.observe(item["id"], [{"type": "min_elapsed", "days": 0}])
        og.scan(self.state, apply=True)
        second = og.scan(self.state, apply=True)
        self.assertEqual(second["flipped"], [])  # already observation-complete, not `observation`

    def test_scan_never_calls_loops_save_directly(self):
        # The one-writer invariant, structurally: this module must mutate the register only through
        # loops.py's own verbs.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "observation_gate.py")
        text = open(path, encoding="utf-8").read()
        self.assertNotIn("loops.save(", text)


class Staleness(Base):
    def _completed_with_met_at(self, days_ago: float):
        item = self.add()
        self.observe(item["id"], [{"type": "min_elapsed", "days": 0}])
        loops.mark_observation_complete(self.state, item_id=item["id"])
        store = loops.load(self.state)
        stamp = (loops._now() - timedelta(days=days_ago)).isoformat(
            timespec="seconds")
        store["items"][item["id"]]["gate"]["met_at"] = stamp
        store["items"][item["id"]]["last_touched"] = stamp
        loops.save(store, self.state)
        return item["id"]

    def test_flagged_when_met_long_ago_with_no_follow_up(self):
        item_id = self._completed_with_met_at(og.STALE_AFTER_DAYS + 5)
        result = og.scan(self.state, apply=False)
        self.assertIn(item_id, [row["id"] for row in result["stale"]])

    def test_not_flagged_when_recent(self):
        item_id = self._completed_with_met_at(1)
        result = og.scan(self.state, apply=False)
        self.assertNotIn(item_id, [row["id"] for row in result["stale"]])

    def test_not_flagged_once_something_followed_up(self):
        item_id = self._completed_with_met_at(og.STALE_AFTER_DAYS + 5)
        loops.start(self.state, item_id=item_id)  # last_touched now moves past met_at
        result = og.scan(self.state, apply=False)
        self.assertNotIn(item_id, [row["id"] for row in result["stale"]])


class BriefLine(Base):
    def test_none_when_nothing_is_ready(self):
        self.assertIsNone(og.brief_line(self.state))

    def test_names_the_item_when_ready(self):
        item = self.add(text="rollout phase 2")
        self.observe(item["id"], [{"type": "min_elapsed", "days": 0}])
        loops.mark_observation_complete(self.state, item_id=item["id"])
        line = og.brief_line(self.state)
        self.assertIn("rollout phase 2", line)
        self.assertIn("ready to continue", line)

    def test_caps_and_reports_the_overflow(self):
        for n in range(og.BRIEF_LINE_CAP + 2):
            item = self.add(text=f"item {n}", item_id=f"loop-x{n}")
            self.observe(item["id"], [{"type": "min_elapsed", "days": 0}])
            loops.mark_observation_complete(self.state, item_id=item["id"])
        line = og.brief_line(self.state)
        self.assertEqual(line.count("ready to continue"), og.BRIEF_LINE_CAP)
        self.assertIn("2 more", line)

    def test_notes_staleness_inline(self):
        item = self.add()
        self.observe(item["id"], [{"type": "min_elapsed", "days": 0}])
        loops.mark_observation_complete(self.state, item_id=item["id"])
        store = loops.load(self.state)
        stamp = (loops._now() - timedelta(days=og.STALE_AFTER_DAYS + 1)).isoformat(
            timespec="seconds")
        store["items"][item["id"]]["gate"]["met_at"] = stamp
        store["items"][item["id"]]["last_touched"] = stamp
        loops.save(store, self.state)
        line = og.brief_line(self.state)
        self.assertIn("stale", line)


if __name__ == "__main__":
    unittest.main()
