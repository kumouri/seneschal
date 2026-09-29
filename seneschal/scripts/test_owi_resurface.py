#!/usr/bin/env python3
"""Tests for `owi_resurface.py` — the positive-resurfacing instrument.

**What this suite is guarding, in one sentence each.**

* **`Eligibility`** — the instrument is about `whose_move` `owner`/`both` only, and the
  dormant-at-epoch carve-out is PERMANENT while ordinary post-epoch dormancy is deliberately NOT a
  carve-out — a row that goes quiet after measurement starts is exactly the case that must count.
* **`TheMissTest`** — the miss test reads `last_raised_at`, never `last_touched` or `carried_reason`;
  an absent `last_raised_at` is a miss by construction (silence cannot discharge it).
* **`TheRateArithmetic`** — `missed / eligible`, a defined `None` (not a crash) at zero eligible, and
  the epoch is read from `owi-epoch.json` and reported, never invented.
* **`ReportNeverWrites`** — `report()` is the report-only half: it must not create the log file,
  touch the register, or have any side effect at all.
* **`TheLogRow`** — `log_run()` is the ONE function that appends, and it appends exactly one row per
  call, carrying `report()`'s own fields plus a schema tag.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import loops  # noqa: E402
import owi_resurface  # noqa: E402

GOOD = dict(
    text="review the quarterly plan",
    audience="owner",
    terminal_state="the plan is reviewed or the owner drops it",
    next_action="block an hour for the review",
    kind="chore",
    priority="normal",
)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def add(self, whose_move="owner", **over):
        kwargs = dict(GOOD)
        kwargs.update(over)
        return loops.add(self.state, whose_move=whose_move, **kwargs)

    def age(self, item_id: str, days: int, field: str = "last_touched") -> None:
        store = loops.load(self.state)
        old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        store["items"][item_id][field] = old
        loops.save(store, self.state)

    def write_epoch(self, epoch_at: str = "2026-01-01T00:00:00+00:00") -> None:
        path = os.path.join(self.state, owi_resurface.EPOCH_FILE)
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": "seneschal.owi-epoch/1", "epoch_at": epoch_at}, fh)

    def write_dormant_snapshot(self, ids) -> None:
        path = os.path.join(self.state, loops.DORMANT_SNAPSHOT_FILE)
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": "seneschal.owi-dormant-at-epoch/1", "dormant_ids": list(ids)}, fh)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = owi_resurface.main(["--state-dir", self.state, *argv])
        return code, out.getvalue(), err.getvalue()


class Eligibility(Base):
    def test_owner_and_both_are_eligible(self):
        a = self.add(whose_move="owner")
        b = self.add(whose_move="both", text="another", item_id="loop-both")
        store = loops.load(self.state)
        got = {i["id"] for i in owi_resurface.eligible_items(store, self.state)}
        self.assertEqual(got, {a["id"], b["id"]})

    def test_assistant_external_and_unknown_are_not_eligible(self):
        for i, wm in enumerate(("assistant", "external", "unknown")):
            self.add(whose_move=wm, text=f"item for {wm}", item_id=f"loop-x-{i}")
        store = loops.load(self.state)
        self.assertEqual(owi_resurface.eligible_items(store, self.state), [])

    def test_terminal_rows_are_never_eligible(self):
        item = self.add()
        loops.resolve(self.state, item_id=item["id"], because="shipped")
        store = loops.load(self.state)
        self.assertEqual(owi_resurface.eligible_items(store, self.state), [])

    def test_dormant_at_epoch_is_permanently_excluded(self):
        item = self.add()
        self.write_dormant_snapshot([item["id"]])
        store = loops.load(self.state)
        self.assertEqual(owi_resurface.eligible_items(store, self.state), [])

    def test_dormant_after_the_epoch_stays_eligible(self):
        # Only items ALREADY dormant when measurement began are exempt. Going quiet later is
        # exactly the permanent-miss case — it must NOT be excluded here.
        item = self.add()
        self.age(item["id"], 200)  # dormant now, but no snapshot says it was dormant AT the epoch
        store = loops.load(self.state)
        got = {i["id"] for i in owi_resurface.eligible_items(store, self.state)}
        self.assertIn(item["id"], got)


class TheMissTest(Base):
    def test_never_raised_is_a_miss(self):
        item = self.add()
        self.age(item["id"], loops.REASK_AFTER_DAYS + 1)
        result = owi_resurface.report(self.state)
        self.assertEqual(result["missed"], 1)
        self.assertEqual(result["missed_ids"], [item["id"]])

    def test_raised_recently_is_not_a_miss(self):
        item = self.add()
        loops.raise_item(self.state, item_id=item["id"])
        result = owi_resurface.report(self.state)
        self.assertEqual(result["missed"], 0)

    def test_raised_long_ago_is_a_miss_again(self):
        item = self.add()
        loops.raise_item(self.state, item_id=item["id"])
        self.age(item["id"], loops.REASK_AFTER_DAYS + 1, field="last_raised_at")
        result = owi_resurface.report(self.state)
        self.assertEqual(result["missed"], 1)

    def test_a_bare_touch_never_counts_as_raised(self):
        # last_touched moves on update/hold/drop; only last_raised_at counts as a resurfacing.
        item = self.add()
        loops.update(self.state, item_id=item["id"], next_action="poke at it")
        self.age(item["id"], loops.REASK_AFTER_DAYS + 1)  # ages last_touched, not last_raised_at
        result = owi_resurface.report(self.state)
        self.assertEqual(result["missed"], 1)

    def test_carrying_never_counts_as_raised(self):
        item = self.add()
        loops.carry(self.state, item_id=item["id"], because="still live")
        self.age(item["id"], loops.REASK_AFTER_DAYS + 1)
        result = owi_resurface.report(self.state)
        self.assertEqual(result["missed"], 1)


class TheRateArithmetic(Base):
    def test_rate_is_missed_over_eligible(self):
        hit = self.add(text="a")
        self.age(hit["id"], loops.REASK_AFTER_DAYS + 1)
        clean = self.add(text="b", item_id="loop-clean")
        loops.raise_item(self.state, item_id=clean["id"])
        result = owi_resurface.report(self.state)
        self.assertEqual(result["eligible"], 2)
        self.assertEqual(result["missed"], 1)
        self.assertAlmostEqual(result["rate"], 0.5)

    def test_zero_eligible_is_a_defined_none_rate_not_a_crash(self):
        result = owi_resurface.report(self.state)
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["missed"], 0)
        self.assertIsNone(result["rate"])

    def test_the_epoch_is_read_and_reported(self):
        self.write_epoch("2026-01-01T00:00:00+00:00")
        result = owi_resurface.report(self.state)
        self.assertEqual(result["epoch_at"], "2026-01-01T00:00:00+00:00")

    def test_a_missing_epoch_reads_as_none_never_a_guess(self):
        result = owi_resurface.report(self.state)
        self.assertIsNone(result["epoch_at"])

    def test_a_corrupt_epoch_file_reads_as_none_not_a_crash(self):
        path = os.path.join(self.state, owi_resurface.EPOCH_FILE)
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        result = owi_resurface.report(self.state)
        self.assertIsNone(result["epoch_at"])


class ReportNeverWrites(Base):
    def test_report_creates_no_log_file(self):
        item = self.add()
        self.age(item["id"], loops.REASK_AFTER_DAYS + 1)
        owi_resurface.report(self.state)
        log_path = os.path.join(self.state, owi_resurface.LOG_FILE)
        self.assertFalse(os.path.exists(log_path))

    def test_report_does_not_touch_the_register(self):
        item = self.add()
        self.age(item["id"], loops.REASK_AFTER_DAYS + 1)
        before = io.open(loops.store_path(self.state), encoding="utf-8").read()
        owi_resurface.report(self.state)
        after = io.open(loops.store_path(self.state), encoding="utf-8").read()
        self.assertEqual(before, after)

    def test_report_cli_writes_nothing(self):
        item = self.add()
        self.age(item["id"], loops.REASK_AFTER_DAYS + 1)
        code, out, _ = self.cli("report")
        self.assertEqual(code, 0)
        log_path = os.path.join(self.state, owi_resurface.LOG_FILE)
        self.assertFalse(os.path.exists(log_path))


class TheLogRow(Base):
    def test_log_appends_one_row_with_the_schema(self):
        item = self.add()
        self.age(item["id"], loops.REASK_AFTER_DAYS + 1)
        row = owi_resurface.log_run(self.state)
        log_path = os.path.join(self.state, owi_resurface.LOG_FILE)
        lines = io.open(log_path, encoding="utf-8").read().splitlines()
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["schema"], owi_resurface.LOG_SCHEMA)
        self.assertEqual(parsed["missed"], 1)
        self.assertEqual(parsed["eligible"], 1)
        self.assertEqual(parsed, row)

    def test_two_runs_append_two_rows(self):
        self.add()
        owi_resurface.log_run(self.state)
        owi_resurface.log_run(self.state)
        log_path = os.path.join(self.state, owi_resurface.LOG_FILE)
        lines = io.open(log_path, encoding="utf-8").read().splitlines()
        self.assertEqual(len(lines), 2)

    def test_log_never_touches_the_register(self):
        item = self.add()
        self.age(item["id"], loops.REASK_AFTER_DAYS + 1)
        before = io.open(loops.store_path(self.state), encoding="utf-8").read()
        owi_resurface.log_run(self.state)
        after = io.open(loops.store_path(self.state), encoding="utf-8").read()
        self.assertEqual(before, after)

    def test_cli_log_writes_the_row(self):
        self.add()
        code, out, _ = self.cli("log", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["schema"], owi_resurface.LOG_SCHEMA)
        log_path = os.path.join(self.state, owi_resurface.LOG_FILE)
        self.assertEqual(len(io.open(log_path, encoding="utf-8").read().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
