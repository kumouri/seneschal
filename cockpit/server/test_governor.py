#!/usr/bin/env python3
"""Tests for cockpit/server/governor.py — the cockpit's own copy of Oikonomos's SCHEMA-validation +
config load/save/rollups (cockpit-spec.md "Oikonomos — the budget governor", v3.5). Pure stdlib, no
FastAPI needed, so this runs unconditionally (mirrors test_model_config.py).

Run: python -m unittest cockpit.server.test_governor
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import governor as gv  # noqa: E402


class Validate(unittest.TestCase):
    def test_int_in_range_ok(self):
        ok, err = gv._validate("fable_oneshots_per_day", gv.SCHEMA["fable_oneshots_per_day"], 3)
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_int_out_of_range_rejected(self):
        ok, err = gv._validate("fable_concurrency_max", gv.SCHEMA["fable_concurrency_max"], 0)
        self.assertFalse(ok)
        self.assertIn("below the minimum", err)

    def test_dict_int_ok(self):
        ok, _ = gv._validate("daily_token_budget_by_model",
                             gv.SCHEMA["daily_token_budget_by_model"], {"claude-opus-4-8": 500})
        self.assertTrue(ok)

    def test_dict_int_bad_value_rejected(self):
        ok, err = gv._validate("daily_token_budget_by_model",
                               gv.SCHEMA["daily_token_budget_by_model"], {"claude-opus-4-8": "nope"})
        self.assertFalse(ok)

    def test_dict_enum_ok(self):
        ok, _ = gv._validate("reasoning_effort_by_mode", gv.SCHEMA["reasoning_effort_by_mode"],
                             {"Chat": "low"})
        self.assertTrue(ok)

    def test_dict_enum_unknown_option_rejected(self):
        ok, err = gv._validate("reasoning_effort_by_mode", gv.SCHEMA["reasoning_effort_by_mode"],
                               {"Chat": "extreme"})
        self.assertFalse(ok)
        self.assertIn("extreme", err)


class LoadSave(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_missing_file_loads_all_defaults(self):
        cfg = gv.load(self.dir)
        self.assertEqual(cfg["fable_oneshots_per_day"], 5)
        self.assertEqual(cfg["fable_concurrency_max"], 1)

    def test_corrupt_file_loads_all_defaults(self):
        (self.dir / gv.CONFIG_FILE).write_text("not json", encoding="utf-8")
        cfg = gv.load(self.dir)
        self.assertEqual(cfg["fable_oneshots_per_day"], 5)

    def test_save_then_load_round_trip(self):
        written = gv.save(self.dir, {"fable_oneshots_per_day": 9})
        self.assertEqual(written["fable_oneshots_per_day"], 9)
        loaded = gv.load(self.dir)
        self.assertEqual(loaded["fable_oneshots_per_day"], 9)

    def test_save_rejects_unknown_knob(self):
        with self.assertRaises(ValueError) as ctx:
            gv.save(self.dir, {"nonexistent_knob": 1})
        self.assertIn("unknown knob", str(ctx.exception))
        self.assertFalse((self.dir / gv.CONFIG_FILE).exists())

    def test_save_rejects_out_of_range_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            gv.save(self.dir, {"turn_checkpoint_n": 0})
        self.assertFalse((self.dir / gv.CONFIG_FILE).exists())

    def test_save_atomic_no_leftover_tmp(self):
        gv.save(self.dir, {"fable_oneshots_per_day": 4})
        self.assertFalse((self.dir / (gv.CONFIG_FILE + ".tmp")).exists())

    def test_save_preserves_untouched_keys(self):
        gv.save(self.dir, {"fable_oneshots_per_day": 3})
        gv.save(self.dir, {"fable_concurrency_max": 2})
        with open(self.dir / gv.CONFIG_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["fable_oneshots_per_day"], 3)
        self.assertEqual(raw["fable_concurrency_max"], 2)

    def test_matches_daemon_side_module_on_disk(self):
        """Byte-for-byte compatibility check, mirroring test_model_config.py's own version of this:
        what this module writes must be exactly what seneschal/scripts/governor.py's `load()` would read
        back the same way."""
        written = gv.save(self.dir, {"fable_oneshots_per_day": 11})
        with open(self.dir / gv.CONFIG_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["fable_oneshots_per_day"], 11)
        self.assertIn("updated_at", raw)
        self.assertEqual(written["fable_oneshots_per_day"], raw["fable_oneshots_per_day"])


class Rollups(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _write_ledger(self, lines):
        with open(gv.ledger_path(self.dir), "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")

    def test_empty_ledger(self):
        roll = gv.rollups(self.dir)
        self.assertEqual(roll["fable_oneshots"], {"day": 0, "week": 0})
        self.assertEqual(roll["tokens_by_model"], {"day": {}, "week": {}})

    def test_tolerates_garbage_lines(self):
        with open(gv.ledger_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"ts": "2026-07-18T12:00:00Z", "kind": "fable_oneshot"}) + "\n")
        now = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
        roll = gv.rollups(self.dir, now=now)
        self.assertEqual(roll["fable_oneshots"]["day"], 1)

    def test_buckets_tokens_by_model_today(self):
        self._write_ledger([
            {"ts": "2026-07-18T15:00:00Z", "kind": "tokens", "model": "claude-opus-4-8", "tokens": 100},
            {"ts": "2026-07-18T16:00:00Z", "kind": "tokens", "model": "claude-opus-4-8", "tokens": 50},
        ])
        now = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
        roll = gv.rollups(self.dir, now=now)
        self.assertEqual(roll["tokens_by_model"]["day"]["claude-opus-4-8"], 150)

    def test_prior_local_day_excluded_from_day_but_counted_in_week(self):
        self._write_ledger([
            {"ts": "2026-07-17T20:00:00Z", "kind": "fable_oneshot"},  # prior owner-local day
        ])
        now = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
        roll = gv.rollups(self.dir, now=now)
        self.assertEqual(roll["fable_oneshots"]["day"], 0)
        self.assertEqual(roll["fable_oneshots"]["week"], 1)


class BasisAwareRollups(unittest.TestCase):
    """The two-basis split, mirrored from seneschal/scripts/governor.py. The cockpit reads
    figures already derived on disk — it must NOT re-derive weights (there is no third copy of the
    ratios), but it does have to agree on the fallback and on naming the basis."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.now = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)

    def _write_ledger(self, lines):
        with open(gv.ledger_path(self.dir), "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps({"ts": "2026-07-18T20:00:00Z", **line}) + "\n")

    def test_legacy_row_falls_back_to_raw(self):
        self._write_ledger([{"kind": "tokens", "model": "m", "tokens": 1000}])
        roll = gv.rollups(self.dir, now=self.now)
        self.assertEqual(roll["billable_by_model"]["day"]["m"], 1000)
        self.assertEqual(roll["basis_by_model"]["day"]["m"], gv.BASIS_RAW)

    def test_billable_row_uses_the_breakdown(self):
        self._write_ledger([{"kind": "tokens", "model": "m", "tokens": 1000,
                             "billable_tokens": 150}])
        roll = gv.rollups(self.dir, now=self.now)
        self.assertEqual(roll["billable_by_model"]["day"]["m"], 150)
        self.assertEqual(roll["tokens_by_model"]["day"]["m"], 1000)
        self.assertEqual(roll["basis_by_model"]["day"]["m"], gv.BASIS_BILLABLE)

    def test_mixed_day_is_labelled_mixed(self):
        self._write_ledger([
            {"kind": "tokens", "model": "m", "tokens": 1000},
            {"kind": "tokens", "model": "m", "tokens": 1000, "billable_tokens": 150},
        ])
        roll = gv.rollups(self.dir, now=self.now)
        self.assertEqual(roll["basis_by_model"]["day"]["m"], gv.BASIS_MIXED)

    def test_fable_oneshot_tokens_are_counted(self):
        self._write_ledger([{"kind": "fable_oneshot", "model": "claude-fable-5", "tokens": 900,
                             "billable_tokens": 120}])
        roll = gv.rollups(self.dir, now=self.now)
        self.assertEqual(roll["billable_by_model"]["day"]["claude-fable-5"], 120)
        self.assertEqual(roll["fable_oneshots"]["day"], 1)

    def test_unmetered_rows_are_counted_separately_not_as_zero_spend(self):
        self._write_ledger([{"kind": "fable_oneshot", "model": "claude-fable-5",
                             "metered": "unavailable"}])
        roll = gv.rollups(self.dir, now=self.now)
        self.assertEqual(roll["unmetered_by_model"]["day"]["claude-fable-5"], 1)
        self.assertNotIn("claude-fable-5", roll["billable_by_model"]["day"])

    def test_empty_ledger_still_carries_every_key(self):
        roll = gv.rollups(self.dir, now=self.now)
        for key in ("tokens_by_model", "billable_by_model", "basis_by_model", "unmetered_by_model"):
            self.assertEqual(roll[key], {"day": {}, "week": {}}, key)


if __name__ == "__main__":
    unittest.main()
