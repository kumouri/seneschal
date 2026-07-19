#!/usr/bin/env python3
"""Parity tripwire for the cockpit's hand-duplicated daemon-side tables.

`cockpit/server/model_config.py` and `cockpit/server/governor.py` deliberately DUPLICATE (never
import) the rank/alias table and the governor SCHEMA from `seneschal/scripts/model_config.py` /
`seneschal/scripts/governor.py` — the cockpit is its own dependency world. "Keep the two in sync by
hand" is the documented contract; this test is the CI alarm that fires when a hand-edit lands on one
side only.

Pure stdlib, no FastAPI needed, so it runs unconditionally (mirrors test_governor.py /
test_model_config.py). The daemon-side modules are loaded by explicit file path (importlib, not
sys.path) — seneschal/scripts is full of generically-named test_*.py files that would collide with
unittest discovery if it were ever added to sys.path. Their guarded `import tz_common` fails in that
loading mode exactly like the cockpit copies' does, so both sides use the same machine-local
day-boundary fallback and rollups can be compared value-for-value.

Run: python -m unittest cockpit.server.test_parity
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import governor as cockpit_governor  # noqa: E402
from cockpit.server import model_config as cockpit_model_config  # noqa: E402


def _load_daemon_module(filename: str, module_name: str):
    path = REPO_ROOT / "seneschal" / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered under its (unique, non-colliding) name BEFORE exec: governor.py declares a
    # @dataclass, and dataclasses resolves the owning module through sys.modules at class-creation
    # time — an unregistered module would crash the decorator.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


daemon_model_config = _load_daemon_module("model_config.py", "seneschal_scripts_model_config")
daemon_governor = _load_daemon_module("governor.py", "seneschal_scripts_governor")


class ModelConfigParity(unittest.TestCase):
    def test_rank_tables_equal(self):
        self.assertEqual(cockpit_model_config.RANK, daemon_model_config.RANK)

    def test_alias_tables_equal(self):
        self.assertEqual(cockpit_model_config.ALIASES, daemon_model_config.ALIASES)

    def test_canonical_agrees_on_every_alias_and_id(self):
        probes = set(cockpit_model_config.RANK) | set(daemon_model_config.RANK)
        probes |= set(cockpit_model_config.ALIASES) | set(daemon_model_config.ALIASES)
        probes |= {p.upper() for p in probes} | {f"  {p}  " for p in probes}
        probes |= {"", "   ", "not-a-model", None, 42}
        for probe in probes:
            with self.subTest(probe=probe):
                self.assertEqual(
                    cockpit_model_config.canonical(probe),
                    daemon_model_config.canonical(probe),
                )

    def test_config_filename_equal(self):
        self.assertEqual(cockpit_model_config.CONFIG_FILE, daemon_model_config.CONFIG_FILE)


class GovernorParity(unittest.TestCase):
    def test_schema_tables_equal(self):
        self.assertEqual(cockpit_governor.SCHEMA, daemon_governor.SCHEMA)

    def test_state_filenames_equal(self):
        self.assertEqual(cockpit_governor.CONFIG_FILE, daemon_governor.CONFIG_FILE)
        self.assertEqual(cockpit_governor.LEDGER_FILE, daemon_governor.LEDGER_FILE)

    def test_rollup_shape_and_values_agree_on_identical_ledger(self):
        """Same fixture ledger + same `now` through both rollups() implementations must produce the
        exact same dict — day/week keys, one-shot counts, per-model token buckets, everything. (Both
        sides degrade to the machine-local clock here — see the module docstring — so the comparison
        is deterministic on any host.)"""
        ledger_rows = [
            {"ts": "2026-07-18T15:00:00Z", "kind": "tokens", "model": "claude-opus-4-8", "tokens": 100},
            {"ts": "2026-07-18T16:00:00Z", "kind": "tokens", "model": "claude-fable-5", "tokens": 7},
            {"ts": "2026-07-18T12:00:00Z", "kind": "fable_oneshot", "conversation_id": "c1"},
            {"ts": "2026-07-17T20:00:00Z", "kind": "fable_oneshot"},  # prior owner-local day
            {"ts": "2026-06-01T00:00:00Z", "kind": "tokens", "model": "claude-sonnet-5", "tokens": 9},
        ]
        now = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / cockpit_governor.LEDGER_FILE
            with open(ledger, "w", encoding="utf-8") as fh:
                for row in ledger_rows:
                    fh.write(json.dumps(row) + "\n")
            cockpit_roll = cockpit_governor.rollups(Path(tmp), now=now)
            daemon_roll = daemon_governor.rollups(tmp, now=now)
        self.assertEqual(cockpit_roll, daemon_roll)

    def test_rollup_empty_shape_agrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            cockpit_roll = cockpit_governor.rollups(Path(tmp))
            daemon_roll = daemon_governor.rollups(tmp)
        self.assertEqual(set(cockpit_roll), set(daemon_roll))
        self.assertEqual(cockpit_roll["fable_oneshots"], daemon_roll["fable_oneshots"])
        self.assertEqual(cockpit_roll["tokens_by_model"], daemon_roll["tokens_by_model"])


if __name__ == "__main__":
    unittest.main()
