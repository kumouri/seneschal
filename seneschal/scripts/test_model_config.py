#!/usr/bin/env python3
"""Tests for the two-dial model config (cockpit-spec.md "Model dials & Fable delegation", v3).

Stdlib ``unittest`` only. Run:  python -m unittest seneschal.scripts.test_model_config
"""
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import model_config as mc  # noqa: E402


class Canonical(unittest.TestCase):
    def test_full_id_passes_through(self):
        self.assertEqual(mc.canonical("claude-opus-4-8"), "claude-opus-4-8")

    def test_alias_resolves(self):
        self.assertEqual(mc.canonical("opus"), "claude-opus-4-8")
        self.assertEqual(mc.canonical("fable"), "claude-fable-5")
        self.assertEqual(mc.canonical("Sonnet"), "claude-sonnet-5")  # case-insensitive alias

    def test_known_variant_alias(self):
        self.assertEqual(mc.canonical("claude-haiku-4-5-20251001"), "claude-haiku-4-5")

    def test_unknown_returns_none(self):
        self.assertIsNone(mc.canonical("gpt-5"))
        self.assertIsNone(mc.canonical(""))
        self.assertIsNone(mc.canonical(None))
        self.assertIsNone(mc.canonical(42))


class RankOf(unittest.TestCase):
    def test_order(self):
        self.assertLess(mc.rank_of("haiku"), mc.rank_of("sonnet"))
        self.assertLess(mc.rank_of("sonnet"), mc.rank_of("opus"))
        self.assertLess(mc.rank_of("opus"), mc.rank_of("fable"))

    def test_unknown_is_none(self):
        self.assertIsNone(mc.rank_of("nonexistent"))


class AdmitsFable(unittest.TestCase):
    def test_fable_ceiling_admits(self):
        self.assertTrue(mc.admits_fable("fable"))
        self.assertTrue(mc.admits_fable("claude-fable-5"))

    def test_lower_ceilings_do_not_admit(self):
        self.assertFalse(mc.admits_fable("opus"))
        self.assertFalse(mc.admits_fable("sonnet"))
        self.assertFalse(mc.admits_fable("haiku"))

    def test_unknown_does_not_admit(self):
        self.assertFalse(mc.admits_fable("nonexistent"))
        self.assertFalse(mc.admits_fable(None))


class ValidatePair(unittest.TestCase):
    def test_coherent_pair_ok(self):
        ok, err = mc.validate_pair("opus", "fable")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_equal_pair_ok(self):
        ok, err = mc.validate_pair("opus", "opus")
        self.assertTrue(ok)

    def test_warm_above_ceiling_rejected(self):
        ok, err = mc.validate_pair("fable", "opus")
        self.assertFalse(ok)
        self.assertIn("outranks", err)

    def test_unrecognized_warm_rejected(self):
        ok, err = mc.validate_pair("nonexistent", "fable")
        self.assertFalse(ok)
        self.assertIn("warm_model", err)

    def test_unrecognized_ceiling_rejected(self):
        ok, err = mc.validate_pair("opus", "nonexistent")
        self.assertFalse(ok)
        self.assertIn("max_routable_model", err)


class LoadSave(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_missing_file_loads_all_none(self):
        cfg = mc.load(self.dir)
        self.assertEqual(cfg, {"warm_model": None, "max_routable_model": None, "updated_at": None})

    def test_corrupt_file_loads_all_none(self):
        with open(mc.config_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("not json at all")
        cfg = mc.load(self.dir)
        self.assertIsNone(cfg["warm_model"])
        self.assertIsNone(cfg["max_routable_model"])

    def test_non_dict_body_loads_all_none(self):
        with open(mc.config_path(self.dir), "w", encoding="utf-8") as fh:
            json.dump([1, 2, 3], fh)
        cfg = mc.load(self.dir)
        self.assertEqual(cfg["warm_model"], None)

    def test_save_then_load_round_trip(self):
        written = mc.save(self.dir, "opus", "fable")
        self.assertEqual(written["warm_model"], "claude-opus-4-8")
        self.assertEqual(written["max_routable_model"], "claude-fable-5")
        self.assertIn("updated_at", written)
        loaded = mc.load(self.dir)
        self.assertEqual(loaded["warm_model"], "claude-opus-4-8")
        self.assertEqual(loaded["max_routable_model"], "claude-fable-5")

    def test_save_normalizes_aliases_to_canonical(self):
        mc.save(self.dir, "sonnet", "opus")
        loaded = mc.load(self.dir)
        self.assertEqual(loaded["warm_model"], "claude-sonnet-5")
        self.assertEqual(loaded["max_routable_model"], "claude-opus-4-8")

    def test_save_rejects_incoherent_pair(self):
        with self.assertRaises(ValueError):
            mc.save(self.dir, "fable", "sonnet")
        # the bad write must never land
        self.assertFalse(os.path.exists(mc.config_path(self.dir)))

    def test_save_atomic_tmp_file_not_left_behind(self):
        mc.save(self.dir, "haiku", "haiku")
        self.assertFalse(os.path.exists(mc.config_path(self.dir) + ".tmp"))

    def test_load_ignores_non_string_fields(self):
        with open(mc.config_path(self.dir), "w", encoding="utf-8") as fh:
            json.dump({"warm_model": 5, "max_routable_model": None}, fh)
        cfg = mc.load(self.dir)
        self.assertIsNone(cfg["warm_model"])
        self.assertIsNone(cfg["max_routable_model"])


if __name__ == "__main__":
    unittest.main()
