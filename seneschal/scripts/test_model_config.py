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
        self.assertEqual(cfg, {"backend": "claude-cli", "warm_model": None,
                               "max_routable_model": None, "updated_at": None})

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


class OpusFive(unittest.TestCase):
    """Claude Opus 5 sits between Opus 4.8 and Fable 5 in the rank table."""

    def test_present_in_rank(self):
        self.assertIn("claude-opus-5", mc.RANK["claude-cli"])

    def test_ranked_between_opus_4_8_and_fable(self):
        self.assertGreater(mc.rank_of("claude-opus-5"), mc.rank_of("claude-opus-4-8"))
        self.assertLess(mc.rank_of("claude-opus-5"), mc.rank_of("claude-fable-5"))

    def test_aliases_resolve(self):
        self.assertEqual(mc.canonical("opus-5"), "claude-opus-5")
        self.assertEqual(mc.canonical("opus5"), "claude-opus-5")

    def test_opus_alias_unchanged(self):
        # regression guard: bare "opus" still means Opus 4.8, not the new tier
        self.assertEqual(mc.canonical("opus"), "claude-opus-4-8")

    def test_warm_opus5_under_fable_ceiling_ok(self):
        ok, err = mc.validate_pair("claude-opus-5", "claude-fable-5")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_warm_fable_over_opus5_ceiling_rejected(self):
        ok, err = mc.validate_pair("claude-fable-5", "claude-opus-5")
        self.assertFalse(ok)

    def test_opus5_ceiling_does_not_admit_fable(self):
        self.assertFalse(mc.admits_fable("claude-opus-5"))
        self.assertTrue(mc.admits_fable("claude-fable-5"))


class BackendAxis(unittest.TestCase):
    """The backend field, per-backend RANK tables, and the
    claude-cli-only Fable concept."""

    def test_codex_ids_unrecognized_on_claude_cli(self):
        self.assertIsNone(mc.canonical("gpt-6-astra"))                    # default backend: claude-cli
        self.assertIsNone(mc.canonical("gpt-6-astra", backend="claude-cli"))

    def test_codex_ids_recognized_on_codex_cli(self):
        self.assertEqual(mc.canonical("gpt-6-astra", backend="codex-cli"), "gpt-6-astra")
        self.assertEqual(mc.canonical("astra", backend="codex-cli"), "gpt-6-astra")

    def test_claude_ids_unrecognized_on_codex_cli(self):
        self.assertIsNone(mc.canonical("claude-opus-5", backend="codex-cli"))

    def test_codex_rank_order(self):
        self.assertLess(mc.rank_of("gpt-5.5", backend="codex-cli"),
                        mc.rank_of("gpt-6-astra", backend="codex-cli"))

    def test_admits_fable_always_false_on_codex_cli(self):
        # Even the strongest codex tier never admits Fable — it's a claude-family concept, not a
        # missing rank comparison.
        self.assertFalse(mc.admits_fable("gpt-6-astra", backend="codex-cli"))

    def test_unrecognized_backend_normalizes_to_default(self):
        self.assertEqual(mc.canonical("opus", backend="not-a-real-backend"), "claude-opus-4-8")

    def test_validate_pair_scoped_to_backend(self):
        ok, err = mc.validate_pair("gpt-5.5", "gpt-6-astra", backend="codex-cli")
        self.assertTrue(ok)
        self.assertIsNone(err)
        ok, err = mc.validate_pair("gpt-5.5", "claude-fable-5", backend="codex-cli")
        self.assertFalse(ok)
        self.assertIn("max_routable_model", err)

    def test_save_and_load_round_trip_backend(self):
        d = tempfile.mkdtemp()
        written = mc.save(d, "gpt-5.5", "gpt-6-astra", backend="codex-cli")
        self.assertEqual(written["backend"], "codex-cli")
        self.assertEqual(written["warm_model"], "gpt-5.5")
        loaded = mc.load(d)
        self.assertEqual(loaded["backend"], "codex-cli")

    def test_save_without_backend_preserves_stored_backend(self):
        """The silent-downgrade guard: a caller that doesn't pass `backend` must never reset it back
        to claude-cli — the model list's own dialOptions fix, applied to this new axis."""
        d = tempfile.mkdtemp()
        mc.save(d, "gpt-5.5", "gpt-6-astra", backend="codex-cli")
        again = mc.save(d, "gpt-5.6-sol", "gpt-6-astra")  # no backend= kwarg at all
        self.assertEqual(again["backend"], "codex-cli")

    def test_save_rejects_unrecognized_backend(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(ValueError):
            mc.save(d, "opus", "fable", backend="not-a-real-backend")

    def test_default_backend_is_claude_cli(self):
        self.assertEqual(mc.DEFAULT_BACKEND, "claude-cli")
        self.assertIn("claude-cli", mc.BACKENDS)
        self.assertIn("codex-cli", mc.BACKENDS)


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
        # regression guard: bare "fable" still means Fable 5, not the new 5.1 tier
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


if __name__ == "__main__":
    unittest.main()
