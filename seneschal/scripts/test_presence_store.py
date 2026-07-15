#!/usr/bin/env python3
"""Tests for presence.py's store-config-driven MCP resolution (resolve_store_mcp + the flag alias).

The daemon forwards an MCP config into every spawned headless `claude`. This used to be hardwired to
Notion (--notion-mcp / auto-detected scripts/notion-mcp.json); it is now driven by the pluggable store
config (seneschal/store/config.json). These tests pin the resolution order and the log wording so the
Notion path stays reachable and filesystem backends resolve to None **healthily**:

  * config.json with notion + a present mcp.json → that path (informational, not a warning);
  * config.json with obsidian / markdown → None + a "filesystem — no MCP needed" line;
  * config.json with notion + a missing mcp.json → None + a WARNING (leading '!');
  * absent / unparseable config.json → None + a "run /setup-store" hint;
  * an absent config with a legacy scripts/notion-mcp.json present → that legacy path (back-compat);
  * an explicit --store-mcp overrides the config;
  * the deprecated --notion-mcp alias still populates the same dest as --store-mcp.

Stdlib ``unittest`` only, like the rest of the suite. Config paths are passed explicitly so these tests
never touch the real seneschal/store/config.json on the host.

Run:  python -m unittest seneschal.scripts.test_presence_store   (or)   python test_presence_store.py
"""
import argparse
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402


def _args(store_mcp=None):
    """A minimal args namespace: only the store-MCP explicit-flag slot matters here."""
    return argparse.Namespace(store_mcp=store_mcp)


class ResolveStoreMcpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="store-mcp-test-")
        self.config = os.path.join(self.tmp, "config.json")
        # A legacy path guaranteed ABSENT unless a test deliberately creates it — so "absent config"
        # doesn't accidentally hit the real machine's scripts/notion-mcp.json (or a created one).
        self.legacy = os.path.join(self.tmp, "no-such-notion-mcp.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, obj):
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def _resolve(self, store_mcp=None):
        return pr.resolve_store_mcp(_args(store_mcp), config_path=self.config, legacy_mcp=self.legacy)

    def test_notion_backend_with_present_mcp_returns_that_path(self):
        mcp = os.path.join(self.tmp, "mcp.json")
        with open(mcp, "w", encoding="utf-8") as fh:
            fh.write("{}")
        self._write_config({"active": "notion", "backends": {"notion": {"mcp_config": mcp.replace("\\", "/")}}})
        path, log = self._resolve()
        self.assertEqual(os.path.normpath(path), os.path.normpath(mcp))
        self.assertFalse(log.startswith("!"))          # informational, not a warning
        self.assertIn("notion", log)

    def test_obsidian_backend_resolves_to_none_healthily(self):
        self._write_config({"active": "obsidian",
                            "backends": {"obsidian": {"vault_path": "C:/x", "subfolder": "S"}}})
        path, log = self._resolve()
        self.assertIsNone(path)
        self.assertIn("filesystem — no MCP needed", log)
        self.assertFalse(log.startswith("!"))          # healthy, not a warning

    def test_markdown_backend_resolves_to_none_healthily(self):
        self._write_config({"active": "markdown", "backends": {"markdown": {"root_path": "~/data"}}})
        path, log = self._resolve()
        self.assertIsNone(path)
        self.assertIn("filesystem — no MCP needed", log)

    def test_notion_backend_with_missing_mcp_warns_and_returns_none(self):
        missing = os.path.join(self.tmp, "gone.json")  # never created
        self._write_config({"active": "notion", "backends": {"notion": {"mcp_config": missing.replace("\\", "/")}}})
        path, log = self._resolve()
        self.assertIsNone(path)
        self.assertTrue(log.startswith("!"), log)       # WARN
        self.assertIn("missing", log)

    def test_absent_config_returns_none_and_setup_hint(self):
        # No config.json written; legacy path absent too → nothing to forward.
        path, log = self._resolve()
        self.assertIsNone(path)
        self.assertIn("run /setup-store", log)

    def test_unparseable_config_returns_none_and_setup_hint(self):
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        path, log = self._resolve()
        self.assertIsNone(path)
        self.assertIn("run /setup-store", log)

    def test_absent_config_falls_back_to_legacy_notion_mcp(self):
        # Back-compat: an install predating /setup-store that still has scripts/notion-mcp.json.
        legacy = os.path.join(self.tmp, "notion-mcp.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("{}")
        path, log = pr.resolve_store_mcp(_args(), config_path=self.config, legacy_mcp=legacy)
        self.assertEqual(os.path.normpath(path), os.path.normpath(legacy))
        self.assertIn("legacy", log.lower())

    def test_explicit_flag_overrides_config(self):
        # Even with a filesystem backend configured, an explicit --store-mcp wins.
        self._write_config({"active": "obsidian", "backends": {"obsidian": {"vault_path": "C:/x"}}})
        path, log = self._resolve(store_mcp="C:/custom/mcp.json")
        self.assertEqual(path, "C:/custom/mcp.json")
        self.assertIn("explicit", log.lower())

    def test_never_raises_on_garbage_config(self):
        # A dict that's structurally wrong (backends is a list) must degrade, not raise.
        self._write_config({"active": "notion", "backends": ["nope"]})
        path, log = self._resolve()  # must not raise
        self.assertIsNone(path)


class StoreMcpFlagAliasTest(unittest.TestCase):
    def test_store_mcp_flag_sets_dest(self):
        ns = pr.build_parser().parse_args(["--store-mcp", "cfg.json"])
        self.assertEqual(ns.store_mcp, "cfg.json")

    def test_notion_mcp_alias_still_works(self):
        ns = pr.build_parser().parse_args(["--notion-mcp", "cfg.json"])
        self.assertEqual(ns.store_mcp, "cfg.json")     # deprecated alias → same dest

    def test_default_is_none(self):
        ns = pr.build_parser().parse_args([])
        self.assertIsNone(ns.store_mcp)


if __name__ == "__main__":
    unittest.main()
