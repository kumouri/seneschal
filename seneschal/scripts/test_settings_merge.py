#!/usr/bin/env python3
"""Tests for settings_merge.py — the ~/.claude/settings.json hook merger."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import settings_merge as sm  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = self.tmp / "settings.json"
        self.repo = self.tmp / "checkout"
        self.repo.mkdir()

    def run_cli(self, *extra) -> int:
        argv = ["settings_merge.py", "--settings", str(self.settings),
                "--repo", str(self.repo)] + list(extra)
        return sm._main(argv)

    def read(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def write(self, data) -> None:
        self.settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def stamp_commands(self, data, event):
        return [item["command"]
                for item in sm._iter_command_items(data.get("hooks", {}).get(event, []))
                if sm.STAMP_MARKER in item["command"]]

    def backups(self):
        return sorted(self.tmp.glob("settings.json.bak-*"))


class FreshFile(Base):
    def test_apply_creates_all_four_events_and_env(self):
        self.assertEqual(self.run_cli("--apply"), 0)
        data = self.read()
        want = sm.expected_command(self.repo)
        for event in sm.HOOK_EVENTS:
            cmds = self.stamp_commands(data, event)
            self.assertEqual(cmds, [want], event)
        self.assertEqual(data["env"]["PYTHONUTF8"], "1")
        self.assertEqual(data["env"]["PYTHONIOENCODING"], "utf-8")

    def test_hook_entry_shape_matches_scheduling_doc(self):
        # SCHEDULING.md section 5: event -> [ {"hooks": [{"type","command","timeout"}]} ].
        self.run_cli("--apply")
        group = self.read()["hooks"]["SessionStart"][0]
        self.assertEqual(list(group), ["hooks"])
        item = group["hooks"][0]
        self.assertEqual(item["type"], "command")
        self.assertEqual(item["timeout"], 10)
        self.assertTrue(item["command"].startswith("python "))
        self.assertNotIn("\\", item["command"])  # forward-slashed, per the documented shape

    def test_dry_run_default_writes_nothing(self):
        self.assertEqual(self.run_cli(), 0)
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.backups(), [])


class ExistingContent(Base):
    def test_other_hooks_and_unknown_keys_preserved(self):
        other = {"hooks": [{"type": "command", "command": "python other_tool.py"}],
                 "matcher": "Bash"}
        self.write({
            "model": "opus",
            "env": {"FOO": "bar"},
            "hooks": {"PreToolUse": [other], "Stop": [other]},
            "unknown_key": {"nested": True},
        })
        self.run_cli("--apply")
        data = self.read()
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["unknown_key"], {"nested": True})
        self.assertEqual(data["env"]["FOO"], "bar")
        self.assertEqual(data["hooks"]["PreToolUse"], [other])   # untouched event
        self.assertEqual(data["hooks"]["Stop"][0], other)        # appended AFTER, not replaced
        self.assertEqual(len(self.stamp_commands(data, "Stop")), 1)

    def test_env_keys_added_without_clobbering(self):
        self.write({"env": {"PYTHONUTF8": "0", "OTHER": "x"}})
        self.run_cli("--apply")
        env = self.read()["env"]
        self.assertEqual(env["PYTHONUTF8"], "0")          # existing value never clobbered
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")  # missing key added
        self.assertEqual(env["OTHER"], "x")

    def test_idempotent_second_apply_no_diff_no_backup(self):
        self.run_cli("--apply")
        first = self.settings.read_text(encoding="utf-8")
        n_backups = len(self.backups())
        self.assertEqual(self.run_cli("--apply"), 0)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), first)
        self.assertEqual(len(self.backups()), n_backups)  # no-change apply takes no backup

    def test_backup_created_on_changing_apply(self):
        self.write({"env": {}})
        original = self.settings.read_text(encoding="utf-8")
        self.run_cli("--apply")
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)


class DifferentCheckout(Base):
    def foreign_settings(self):
        cmd = "python C:/somewhere/else/seneschal/scripts/session_stamp.py"
        return {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}]
                for event in sm.HOOK_EVENTS
            }
        }, cmd

    def test_foreign_entry_left_alone_without_force(self):
        data, cmd = self.foreign_settings()
        self.write(data)
        self.run_cli("--apply")
        out = self.read()
        for event in sm.HOOK_EVENTS:
            self.assertEqual(self.stamp_commands(out, event), [cmd], event)  # untouched, no dupe

    def test_foreign_entry_reported_in_merge_notes(self):
        data, _ = self.foreign_settings()
        merged, notes = sm.merge(data, self.repo)
        self.assertEqual(merged["hooks"], data["hooks"])  # hooks untouched (env keys still added)
        self.assertTrue(any("DIFFERENT" in n and "--force-path" in n for n in notes))

    def test_force_path_repoints_in_place(self):
        data, _ = self.foreign_settings()
        self.write(data)
        self.run_cli("--apply", "--force-path")
        out = self.read()
        want = sm.expected_command(self.repo)
        for event in sm.HOOK_EVENTS:
            self.assertEqual(self.stamp_commands(out, event), [want], event)
            self.assertEqual(len(out["hooks"][event]), 1)  # rewritten, not duplicated

    def test_same_checkout_backslash_variant_recognized(self):
        cmd = "python " + str(self.repo / "seneschal" / "scripts" / "session_stamp.py")
        self.write({"hooks": {e: [{"hooks": [{"type": "command", "command": cmd,
                                              "timeout": 10}]}] for e in sm.HOOK_EVENTS}})
        merged, _ = sm.merge(self.read(), self.repo)
        for event in sm.HOOK_EVENTS:
            self.assertEqual(len(self.stamp_commands(merged, event)), 1, event)


class Refusals(Base):
    def test_corrupt_json_refused_untouched(self):
        self.settings.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_cli("--apply"), 2)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")
        self.assertEqual(self.backups(), [])

    def test_non_object_top_level_refused(self):
        self.settings.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(self.run_cli("--apply"), 2)

    def test_non_list_event_refused(self):
        self.write({"hooks": {"Stop": {"weird": True}}})
        self.assertEqual(self.run_cli("--apply"), 2)
        self.assertEqual(self.read(), {"hooks": {"Stop": {"weird": True}}})

    def test_non_dict_env_refused(self):
        self.write({"env": ["not", "a", "dict"]})
        self.assertEqual(self.run_cli("--apply"), 2)


class DiffOutput(Base):
    def test_dry_run_prints_unified_diff(self):
        self.write({"env": {}})
        old = self.settings.read_text(encoding="utf-8")
        merged, _ = sm.merge(self.read(), self.repo)
        diff = sm.unified_diff(old, sm.render(merged), self.settings)
        self.assertIn("+++", diff)
        self.assertIn("session_stamp.py", diff)
        self.assertIn("SessionEnd", diff)


if __name__ == "__main__":
    unittest.main()
