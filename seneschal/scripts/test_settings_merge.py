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


class GuardHooks(Base):
    """The optional ``--guard`` hooks: opt-in only, same diff-first/append-only guarantees."""

    def guard_items(self, data, event, script):
        return [(g.get("matcher"), item) for g in data.get("hooks", {}).get(event, [])
                for item in sm._iter_command_items([g]) if script in item["command"]]

    def test_no_guard_is_installed_unless_named(self):
        self.assertEqual(self.run_cli("--apply"), 0)
        data = self.read()
        self.assertNotIn("PreToolUse", data["hooks"])
        self.assertNotIn("PostToolUse", data["hooks"])

    def test_merge_guard_adds_pre_and_post_failure_entries(self):
        self.assertEqual(self.run_cli("--apply", "--guard", "merge"), 0)
        data = self.read()
        pre = self.guard_items(data, "PreToolUse", "merge_guard.py")
        post = self.guard_items(data, "PostToolUseFailure", "merge_guard.py")
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0][0], "Bash|PowerShell")
        self.assertEqual(pre[0][1]["timeout"], 90)
        self.assertTrue(pre[0][1]["command"].endswith("seneschal/scripts/merge_guard.py"))
        self.assertTrue(post[0][1]["command"].endswith("merge_guard.py --post-tool-use"))

    def test_all_excludes_the_notion_only_query_shape_hook(self):
        self.assertEqual(self.run_cli("--apply", "--guard", "all"), 0)
        data = self.read()
        for script in ("bash_path_guard.py", "script_file_guard.py", "merge_guard.py",
                       "branch_delete_guard.py"):
            self.assertEqual(len(self.guard_items(data, "PreToolUse", script)), 1, script)
        self.assertNotIn("PostToolUse", data["hooks"])
        self.assertEqual(self.run_cli("--apply", "--guard", "query-shape"), 0)
        self.assertEqual(len(self.guard_items(self.read(), "PostToolUse", "query_shape_hook.py")), 1)

    def test_guards_are_idempotent_and_append_after_existing_entries(self):
        other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python x.py"}]}
        self.write({"hooks": {"PreToolUse": [other]}})
        self.assertEqual(self.run_cli("--apply", "--guard", "all"), 0)
        first = self.read()
        self.assertEqual(first["hooks"]["PreToolUse"][0], other)
        n_backups = len(self.backups())
        self.assertEqual(self.run_cli("--apply", "--guard", "all"), 0)
        self.assertEqual(self.read(), first)
        self.assertEqual(len(self.backups()), n_backups)

    def test_foreign_guard_left_alone_unless_force_path(self):
        foreign = "python /elsewhere/seneschal/scripts/merge_guard.py"
        self.write({"hooks": {"PreToolUse": [
            {"matcher": "Bash|PowerShell", "hooks": [{"type": "command", "command": foreign}]}]}})
        merged, notes = sm.merge(self.read(), self.repo, guards=["merge"])
        cmds = [i["command"] for _, i in self.guard_items(merged, "PreToolUse", "merge_guard.py")]
        self.assertEqual(cmds, [foreign])
        self.assertTrue(any("DIFFERENT" in n for n in notes))
        merged, _ = sm.merge(self.read(), self.repo, force_path=True, guards=["merge"])
        cmds = [i["command"] for _, i in self.guard_items(merged, "PreToolUse", "merge_guard.py")]
        self.assertEqual(len(cmds), 1)
        self.assertIn(str(self.repo).replace("\\", "/"), cmds[0])

    def test_unknown_guard_is_refused_and_nothing_written(self):
        self.assertEqual(self.run_cli("--apply", "--guard", "nope"), 2)
        self.assertFalse(self.settings.exists())

    def test_each_guard_row_matches_its_setup_guide(self):
        guides = {"bash-path": "BASH_PATH_GUARD_SETUP.md", "script-file": "SCRIPT_FILE_GUARD_SETUP.md",
                  "merge": "MERGE_GUARD_SETUP.md", "branch-delete": "BRANCH_DELETE_GUARD_SETUP.md",
                  "query-shape": "QUERY_SHAPE_SETUP.md"}
        self.assertEqual(set(guides), set(sm.GUARDS))
        for name, guide in guides.items():
            text = (HERE / guide).read_text(encoding="utf-8")
            for event, matcher, script, extra, timeout in sm.GUARDS[name]:
                with self.subTest(guard=name, event=event):
                    self.assertIn(f'"{event}"', text)
                    self.assertIn(f'"matcher": "{matcher}"', text)
                    self.assertIn(f"seneschal/scripts/{script}{extra}", text)
                    self.assertIn(f'"timeout": {timeout}', text)
                    self.assertIn(f"--guard {name}", text)


if __name__ == "__main__":
    unittest.main()
