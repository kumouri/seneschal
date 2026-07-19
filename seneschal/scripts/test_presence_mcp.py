#!/usr/bin/env python3
"""Unit tests for the daemon's MCP-config threading (store + Slack -> one `claude --mcp-config`).

Covers the Slack draft-and-hold spec's Q9 daemon Slack-hands wiring: ``active_mcp_configs()`` collects
whichever of the store's MCP (resolved onto args.notion_mcp) / Slack is wired, and ``WarmSession`` threads them all after a single ``--mcp-config``
(the CLI accepts multiple space-separated configs). Stdlib only; nothing spawns a real ``claude`` — the
WarmSession test stubs ``subprocess.Popen`` and inspects the argv it would have run.
"""
import argparse
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import presence as pr  # noqa: E402


class ActiveMcpConfigsTest(unittest.TestCase):
    @staticmethod
    def _args(**over):
        base = dict(notion_mcp=None, slack_mcp=None)
        base.update(over)
        return argparse.Namespace(**base)

    def test_none_wired(self):
        self.assertEqual(pr.active_mcp_configs(self._args()), [])

    def test_notion_only(self):
        self.assertEqual(pr.active_mcp_configs(self._args(notion_mcp="n.json")), ["n.json"])

    def test_slack_only(self):
        self.assertEqual(pr.active_mcp_configs(self._args(slack_mcp="s.json")), ["s.json"])

    def test_both_notion_first(self):
        # Order is stable + documented: the store's MCP (persistence) then Slack (send hands).
        self.assertEqual(
            pr.active_mcp_configs(self._args(notion_mcp="n.json", slack_mcp="s.json")),
            ["n.json", "s.json"])

    def test_partial_namespace_is_safe(self):
        # A namespace missing slack_mcp entirely (older callers / partial test args) must not raise.
        self.assertEqual(pr.active_mcp_configs(argparse.Namespace(notion_mcp="n.json")), ["n.json"])
        self.assertEqual(pr.active_mcp_configs(argparse.Namespace()), [])


class WarmSessionThreadsConfigsTest(unittest.TestCase):
    """WarmSession.start() builds the argv and hands it to Popen; we stub Popen to capture the command
    without spawning. start() does nothing with the process after construction, so a trivial stub suffices."""

    def _captured_cmd(self, mcp_configs):
        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kw):
                captured["cmd"] = cmd

        orig = pr.subprocess.Popen
        pr.subprocess.Popen = _FakePopen
        try:
            ws = pr.WarmSession("claude", None, "bypassPermissions", lambda *_: None,
                                mcp_configs=mcp_configs)
            ws.start()
        finally:
            pr.subprocess.Popen = orig
        return captured["cmd"]

    def test_both_configs_after_single_flag(self):
        cmd = self._captured_cmd(["n.json", "s.json"])
        # exactly one --mcp-config, with both paths as consecutive args after it (CLI: space-separated).
        self.assertEqual(cmd.count("--mcp-config"), 1)
        i = cmd.index("--mcp-config")
        self.assertEqual(cmd[i + 1:i + 3], ["n.json", "s.json"])

    def test_no_configs_no_flag(self):
        self.assertNotIn("--mcp-config", self._captured_cmd([]))

    def test_none_configs_defaults_empty(self):
        # mcp_configs=None must behave like [] (no flag), not crash.
        self.assertNotIn("--mcp-config", self._captured_cmd(None))


if __name__ == "__main__":
    unittest.main()
