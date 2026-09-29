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
from unittest import mock

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
        self.assertEqual(cfg, {"backend": "claude-cli", "warm_model": None,
                               "max_routable_model": None, "updated_at": None})

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


class KnownModels(unittest.TestCase):
    """The dial picker's option list. Regression guard for a silent-downgrade failure: a frontend
    that owns its own copy of this list drifts, and a <select> whose value matches no option
    renders the FIRST option — the panel then shows the wrong model, and a save writes it back."""

    def test_covers_every_ranked_model(self):
        # THE invariant: a model in RANK[backend] can be written to the config, so it must be
        # selectable for that backend.
        self.assertEqual([m["id"] for m in mc.known_models()], mc.RANK["claude-cli"])
        self.assertEqual([m["id"] for m in mc.known_models("codex-cli")], mc.RANK["codex-cli"])

    def test_opus_5_is_present_and_distinguishable_from_opus_4_8(self):
        labels = {m["id"]: m["label"] for m in mc.known_models()}
        self.assertIn("claude-opus-5", labels)
        self.assertIn("claude-opus-4-8", labels)
        # A bare "Opus" on both is what made picking the downgrade look reasonable.
        self.assertNotEqual(labels["claude-opus-5"], labels["claude-opus-4-8"])
        for model_id, label in labels.items():
            self.assertTrue(label.strip(), model_id)

    def test_labels_are_unique(self):
        labels = [m["label"] for m in mc.known_models()]
        self.assertEqual(len(labels), len(set(labels)))

    def test_unlabelled_model_falls_back_to_its_id_rather_than_vanishing(self):
        patched = {**mc.RANK, "claude-cli": [*mc.RANK["claude-cli"], "claude-future-9"]}
        with mock.patch.object(mc, "RANK", patched):
            entry = mc.known_models()[-1]
        self.assertEqual(entry, {"id": "claude-future-9", "label": "claude-future-9"})

    def test_order_is_rank_order(self):
        ids = [m["id"] for m in mc.known_models()]
        self.assertLess(ids.index("claude-haiku-4-5"), ids.index("claude-opus-4-8"))
        self.assertLess(ids.index("claude-opus-4-8"), ids.index("claude-opus-5"))
        self.assertLess(ids.index("claude-opus-5"), ids.index("claude-fable-5"))


class BackendAxis(unittest.TestCase):
    """The backend axis, mirrored on the cockpit side (ruling 3)."""

    def test_codex_ids_unrecognized_on_claude_cli(self):
        self.assertIsNone(mc.canonical("gpt-6-astra"))

    def test_codex_ids_recognized_on_codex_cli(self):
        self.assertEqual(mc.canonical("gpt-6-astra", backend="codex-cli"), "gpt-6-astra")

    def test_admits_fable_always_false_on_codex_cli(self):
        self.assertFalse(mc.admits_fable("gpt-6-astra", backend="codex-cli"))

    def test_known_backends_covers_both(self):
        backends = {b["id"]: b for b in mc.known_backends()}
        self.assertEqual(set(backends), {"claude-cli", "codex-cli"})
        self.assertEqual([m["id"] for m in backends["codex-cli"]["models"]], mc.RANK["codex-cli"])

    def test_save_preserves_backend_when_omitted(self):
        d = Path(tempfile.mkdtemp())
        mc.save(d, "gpt-5.5", "gpt-6-astra", backend="codex-cli")
        again = mc.save(d, "gpt-5.6-sol", "gpt-6-astra")
        self.assertEqual(again["backend"], "codex-cli")


class OpusFiveFiveAndFableFiveOne(unittest.TestCase):
    """Opus 5.5 (mid-rank insert) and Fable 5.1 (top-of-chain append), added together."""

    def test_full_rank_order(self):
        self.assertEqual(mc.RANK["claude-cli"], [
            "claude-haiku-4-5",
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-opus-5",
            "claude-opus-5-5",
            "claude-fable-5",
            "claude-fable-5-1",
        ])

    def test_opus_5_5_ranked_between_opus_5_and_fable_5(self):
        self.assertGreater(mc.rank_of("claude-opus-5-5"), mc.rank_of("claude-opus-5"))
        self.assertLess(mc.rank_of("claude-opus-5-5"), mc.rank_of("claude-fable-5"))

    def test_fable_5_1_ranked_above_fable_5(self):
        self.assertGreater(mc.rank_of("claude-fable-5-1"), mc.rank_of("claude-fable-5"))

    def test_opus_5_5_aliases_resolve(self):
        for alias in ("opus-5-5", "opus5.5", "opus-5.5", "opus55"):
            self.assertEqual(mc.canonical(alias), "claude-opus-5-5")

    def test_fable_5_1_aliases_resolve(self):
        for alias in ("fable-5-1", "fable5.1", "fable-5.1", "fable51"):
            self.assertEqual(mc.canonical(alias), "claude-fable-5-1")

    def test_bare_fable_alias_unchanged(self):
        self.assertEqual(mc.canonical("fable"), "claude-fable-5")

    def test_admits_fable_false_for_opus_5_5(self):
        self.assertFalse(mc.admits_fable("claude-opus-5-5"))

    def test_admits_fable_true_for_fable_5_1(self):
        self.assertTrue(mc.admits_fable("claude-fable-5-1"))

    def test_warm_opus_5_5_under_fable_5_1_ceiling_ok(self):
        ok, err = mc.validate_pair("claude-opus-5-5", "claude-fable-5-1")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_warm_fable_5_1_over_fable_5_ceiling_rejected(self):
        ok, err = mc.validate_pair("claude-fable-5-1", "claude-fable-5")
        self.assertFalse(ok)
        self.assertIn("outranks", err)

    def test_labels_never_bare_tier_word(self):
        labels = {m["id"]: m["label"] for m in mc.known_models()}
        self.assertEqual(labels["claude-opus-5-5"], "Opus 5.5")
        self.assertEqual(labels["claude-fable-5-1"], "Fable 5.1")
        self.assertNotEqual(labels["claude-opus-5-5"], labels["claude-opus-5"])
        self.assertNotEqual(labels["claude-fable-5-1"], labels["claude-fable-5"])


if __name__ == "__main__":
    unittest.main()
