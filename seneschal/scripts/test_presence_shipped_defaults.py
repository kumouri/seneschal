#!/usr/bin/env python3
"""The daemon boots on the SHIPPED DEFAULTS — no persona, no store, no Telegram.

A fresh clone has no `persona/identity.json`, no `seneschal/store/config.json`, no legacy
`scripts/notion-mcp.json` and no `telegram.env`. The framework promises it still runs (CLAUDE.md:
"The framework runs on the shipped defaults ... until any of it is run"), so this pins the whole
boot → drain → shutdown path end to end in-process, with every host-specific source pointed at a
path that does not exist:

  * `presence.main()` returns 0 and logs a clean "presence up" … "presence stopped";
  * the store resolves to "not configured" (no MCP forwarded, no crash), and the Notion-only outbox
    machinery stays off (`store_backend_active()` is None, `_tend_outbox` touches nothing);
  * a fake-inbox message is answered by the stub brain and recorded by `--stub-send`;
  * the grounding renders with the generic identity phrases and no unresolved identity token.

Stdlib ``unittest`` only; no `claude`, no network, no real channel.
"""
import argparse
import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import identity_common as ic  # noqa: E402
import presence as pr  # noqa: E402


class ShippedDefaultsBoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state")
        self.missing = os.path.join(self.tmp, "does-not-exist")
        self.inbox = os.path.join(self.tmp, "inbox.json")
        with open(self.inbox, "w", encoding="utf-8") as fh:
            json.dump([[{"kind": "message", "text": "hello there", "message_id": 1}]], fh)

    def _run_main(self):
        argv = ["presence.py", "--stub-brain", "--stub-send", "--fake-inbox", self.inbox,
                "--state-dir", self.state, "--telegram-env", os.path.join(self.missing, "telegram.env"),
                "--max-iterations", "3", "--no-peek", "--tick-sec", "0.2",
                "--router-mode", "off", "--interleave-mode", "off", "--no-usage-reading"]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(pr, "STORE_CONFIG", os.path.join(self.missing, "config.json")), \
                mock.patch.object(pr, "DEFAULT_NOTION_MCP", os.path.join(self.missing, "notion-mcp.json")), \
                mock.patch.object(pr, "DEFAULT_SLACK_MCP", os.path.join(self.missing, "slack-mcp.json")), \
                mock.patch.object(pr, "DEFAULT_DISCORD_ENV", os.path.join(self.missing, "discord.env")), \
                mock.patch.object(pr, "read_claude_identity", lambda *a, **k: None), \
                redirect_stdout(out):
            rc = pr.main()
        return rc, out.getvalue()

    def test_boots_answers_and_stops_cleanly_with_nothing_configured(self):
        rc, log = self._run_main()
        self.assertEqual(rc, 0, log)
        self.assertIn("store not configured", log)
        self.assertIn("presence up", log)
        self.assertIn("presence stopped", log)
        self.assertNotIn("crashed", log)
        with open(os.path.join(self.state, "sent.jsonl"), encoding="utf-8") as fh:
            sent = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(any("hello there" in row.get("text", "") for row in sent), sent)

    def test_the_notion_only_outbox_stays_off(self):
        with mock.patch.object(pr, "STORE_CONFIG", os.path.join(self.missing, "config.json")), \
                mock.patch.object(pr, "DEFAULT_NOTION_MCP", os.path.join(self.missing, "notion-mcp.json")):
            self.assertIsNone(pr.store_backend_active())
            state = pr.DaemonState.__new__(pr.DaemonState)
            state.outbox, state.outbox_checked_at = {"pending": 3}, 0.0
            args = argparse.Namespace(no_outbox=False, state_dir=self.state)
            with mock.patch.object(pr, "outbox_backlog",
                                   side_effect=AssertionError("outbox read on a non-notion store")):
                asyncio.run(pr._tend_outbox(state, args, lambda *_: None, False))
            self.assertEqual(state.outbox, {})

    def test_generic_identity_renders_every_prompt(self):
        g = pr._render_grounding(pr.GROUNDING_TEMPLATE, ic.DEFAULTS)
        r = pr._render_grounding(pr.RESUME_PREAMBLE_TEMPLATE, ic.DEFAULTS)
        seed = pr.build_seed_prompt(ic.DEFAULTS)
        for text in (g, r, seed, *[s["prompt"] for s in pr.build_slots(ic.DEFAULTS)]):
            for token in ("{assistant}", "{owner}", "{tz}"):
                self.assertNotIn(token, text)
        self.assertIn("the owner's configured timezone", r)


class ApiKeyScrubParity(unittest.TestCase):
    """Every spawned child scrubs ANTHROPIC_API_KEY (billing safety): the headless one-shots via
    `child_env`, the warm session via the backend's own copy, and the codex backend its OPENAI key."""

    def test_every_child_env_scrubs_the_metered_key(self):
        from backends import claude_cli, codex_cli
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-never", "OPENAI_API_KEY": "sk-never"}):
            for env in (pr.child_env(), pr.child_env("watch"), pr.warm_session_env(),
                        claude_cli._warm_session_env()):
                self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("OPENAI_API_KEY", codex_cli._child_env())
        self.assertEqual(pr.child_env("watch")["SENESCHAL_SESSION_SOURCE"], "watch")
        self.assertEqual(pr.warm_session_env()["SENESCHAL_SESSION_SOURCE"], "daemon")


if __name__ == "__main__":
    unittest.main()
