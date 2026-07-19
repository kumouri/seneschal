#!/usr/bin/env python3
"""Tests for cockpit/server/model_config.py — the cockpit's own copy of the two-dial rank/coherence
table (cockpit-spec.md "Model dials & Fable delegation", v3). Pure stdlib, no FastAPI needed, so this
runs unconditionally (mirrors test_emotes.py).

Run: python -m unittest cockpit.server.test_model_config
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import model_config as mc  # noqa: E402


class Canonical(unittest.TestCase):
    def test_full_id_passes_through(self):
        self.assertEqual(mc.canonical("claude-fable-5"), "claude-fable-5")

    def test_alias_resolves(self):
        self.assertEqual(mc.canonical("fable"), "claude-fable-5")
        self.assertEqual(mc.canonical("HAIKU"), "claude-haiku-4-5")

    def test_unknown_is_none(self):
        self.assertIsNone(mc.canonical("nonexistent"))
        self.assertIsNone(mc.canonical(None))


class AdmitsFable(unittest.TestCase):
    def test_fable_admits(self):
        self.assertTrue(mc.admits_fable("fable"))

    def test_opus_does_not_admit(self):
        self.assertFalse(mc.admits_fable("opus"))

    def test_unknown_does_not_admit(self):
        self.assertFalse(mc.admits_fable(None))


class ValidatePair(unittest.TestCase):
    def test_coherent_pair_ok(self):
        ok, err = mc.validate_pair("opus", "fable")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_warm_above_ceiling_rejected(self):
        ok, err = mc.validate_pair("fable", "opus")
        self.assertFalse(ok)
        self.assertIn("outranks", err)

    def test_unrecognized_id_rejected(self):
        ok, err = mc.validate_pair("nonexistent", "fable")
        self.assertFalse(ok)
        self.assertIn("warm_model", err)


class LoadSave(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_missing_file_loads_all_none(self):
        cfg = mc.load(self.dir)
        self.assertEqual(cfg, {"warm_model": None, "max_routable_model": None, "updated_at": None})

    def test_corrupt_file_loads_all_none(self):
        (self.dir / mc.CONFIG_FILE).write_text("not json", encoding="utf-8")
        cfg = mc.load(self.dir)
        self.assertIsNone(cfg["warm_model"])

    def test_save_then_load_round_trip(self):
        written = mc.save(self.dir, "opus", "fable")
        self.assertEqual(written["warm_model"], "claude-opus-4-8")
        self.assertEqual(written["max_routable_model"], "claude-fable-5")
        loaded = mc.load(self.dir)
        self.assertEqual(loaded, written)

    def test_save_rejects_incoherent_pair(self):
        with self.assertRaises(ValueError):
            mc.save(self.dir, "fable", "sonnet")
        self.assertFalse((self.dir / mc.CONFIG_FILE).exists())

    def test_save_atomic_no_leftover_tmp(self):
        mc.save(self.dir, "haiku", "haiku")
        self.assertFalse((self.dir / (mc.CONFIG_FILE + ".tmp")).exists())

    def test_matches_daemon_side_module_on_disk(self):
        """Byte-for-byte compatibility check: what this module writes must be exactly what
        seneschal/scripts/model_config.py's `load()` would read back the same way — the whole point of
        duplicating the table is that both sides agree on the file."""
        written = mc.save(self.dir, "sonnet", "opus")
        with open(self.dir / mc.CONFIG_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["warm_model"], "claude-sonnet-5")
        self.assertEqual(raw["max_routable_model"], "claude-opus-4-8")
        self.assertIn("updated_at", raw)
        self.assertEqual(written, {**raw})


if __name__ == "__main__":
    unittest.main()
