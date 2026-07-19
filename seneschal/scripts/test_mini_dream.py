#!/usr/bin/env python3
"""Tests for the mini-dream (phase 2b) — per-session distillation into the shared cross-instance log.

Stdlib ``unittest`` only. Covers: transcript parsing (sidechains + scaffolding excluded), the salience
ladder (skip-small / deterministic / LLM-with-fallback), idempotence per session id, Dream's prune
compaction, the recursion guard, and ``session_stamp``'s SessionEnd spawn wiring (mocked — no real
subprocess, no real LLM anywhere).

Run:  python -m unittest seneschal.scripts.test_mini_dream   (or)   python test_mini_dream.py
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import mini_dream as md  # noqa: E402
import session_stamp as st  # noqa: E402

NOW = datetime(2026, 7, 15, 20, 0, 0, tzinfo=timezone.utc)


def _transcript(path, user_turns=3, title="Registry chat", files=("a.py",), sidechain_turns=0):
    """Write a minimal-but-realistic session transcript JSONL."""
    lines = [{"type": "queue-operation", "operation": "enqueue", "content": "noise"},
             {"type": "custom-title", "customTitle": title, "sessionId": "s1"}]
    for i in range(user_turns):
        lines.append({"type": "user", "gitBranch": "feat/x", "cwd": "C:\\repo",
                      "message": {"role": "user", "content": f"do the thing {i}"}})
        blocks = [{"type": "text", "text": f"did the thing {i}"}]
        for f in (files if i == 0 else ()):
            blocks.append({"type": "tool_use", "name": "Edit", "input": {"file_path": f}})
        lines.append({"type": "assistant", "message": {"role": "assistant", "content": blocks}})
    for _ in range(sidechain_turns):
        lines.append({"type": "user", "isSidechain": True,
                      "message": {"role": "user", "content": "subagent traffic"}})
    lines.append({"type": "user", "message": {"role": "user", "content": "<system-reminder>meta</system-reminder>"}})
    with open(path, "w", encoding="utf-8") as fh:
        for d in lines:
            fh.write(json.dumps(d) + "\n")
    return path


class ParseUnit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tr = _transcript(os.path.join(self.dir, "t.jsonl"), user_turns=3, sidechain_turns=5)

    def test_parse_extracts_facts_and_excludes_noise(self):
        info = md.parse_transcript(self.tr)
        self.assertEqual(info["user_turns"], 3)          # sidechains + <scaffolding> excluded
        self.assertEqual(info["title"], "Registry chat")
        self.assertEqual(info["branch"], "feat/x")
        self.assertEqual(info["files_touched"], ["a.py"])
        self.assertTrue(info["first_prompt"].startswith("do the thing 0"))

    def test_parse_missing_file_is_empty_not_crash(self):
        info = md.parse_transcript(os.path.join(self.dir, "absent.jsonl"))
        self.assertEqual(info["user_turns"], 0)


class DistillUnit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "state")

    def _log(self):
        return md._load_lines(os.path.join(self.state, md.DISTILL_FILE))

    def test_deterministic_distill_writes_record(self):
        tr = _transcript(os.path.join(self.dir, "t.jsonl"), user_turns=3)
        res = md.distill(self.state, tr, "sess-1", cwd="C:\\repo", now=NOW)
        self.assertEqual(res["action"], "written")
        self.assertEqual(res["engine"], "deterministic")  # 3 turns < LLM threshold
        (rec,) = self._log()
        self.assertEqual(rec["session_id"], "sess-1")
        self.assertEqual(rec["title"], "Registry chat")
        self.assertIn("Registry chat", rec["distillate"])
        self.assertEqual(rec["files_touched"], ["a.py"])

    def test_small_session_skipped(self):
        tr = _transcript(os.path.join(self.dir, "t.jsonl"), user_turns=1)
        res = md.distill(self.state, tr, "sess-tiny", now=NOW)
        self.assertEqual(res["action"], "skipped_small")
        self.assertEqual(self._log(), [])

    def test_second_distill_of_same_session_is_noop(self):
        tr = _transcript(os.path.join(self.dir, "t.jsonl"), user_turns=3)
        md.distill(self.state, tr, "sess-1", now=NOW)
        res = md.distill(self.state, tr, "sess-1", now=NOW)
        self.assertEqual(res["action"], "skipped_dupe")
        self.assertEqual(len(self._log()), 1)

    def test_llm_engine_falls_back_to_deterministic_on_failure(self):
        tr = _transcript(os.path.join(self.dir, "t.jsonl"), user_turns=md.LLM_USER_TURNS)
        res = md.distill(self.state, tr, "sess-big", claude_bin="definitely-not-a-real-binary",
                         now=NOW)  # auto → tries LLM → OSError → falls back, still writes
        self.assertEqual(res["action"], "written")
        self.assertEqual(res["engine"], "deterministic")
        (rec,) = self._log()
        self.assertTrue(rec["distillate"])

    def test_llm_engine_used_when_it_succeeds(self):
        tr = _transcript(os.path.join(self.dir, "t.jsonl"), user_turns=md.LLM_USER_TURNS)
        orig = md.llm_distillate
        md.llm_distillate = lambda info, model, claude_bin, timeout=0: "- distilled by llm"
        try:
            res = md.distill(self.state, tr, "sess-big", now=NOW)
        finally:
            md.llm_distillate = orig
        self.assertEqual(res["engine"], "llm")
        (rec,) = self._log()
        self.assertEqual(rec["distillate"], "- distilled by llm")

    def test_llm_child_env_scrubs_key_and_sets_guard(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            raise OSError("stop here")

        orig = md.subprocess.run
        md.subprocess.run = fake_run
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-leak"
        try:
            md.llm_distillate({"excerpt": [("Owner", "hi")], "title": None}, "haiku", "claude")
        finally:
            md.subprocess.run = orig
            del os.environ["ANTHROPIC_API_KEY"]
        self.assertNotIn("ANTHROPIC_API_KEY", captured["env"])   # subscription-billing rule
        self.assertEqual(captured["env"][md.RECURSION_ENV], "1")  # never dream the dreamer

    def test_llm_distill_spawns_windowless(self):
        """The `claude -p` distill runs with CREATE_NO_WINDOW so it never pops a console window from the
        console-less (DETACHED_PROCESS) mini-dream process — the reported 'random Claude windows' that
        appeared on SessionEnd. The flag is 0 (no-op) off Windows, so this asserts the same everywhere."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            raise OSError("stop here")

        orig = md.subprocess.run
        md.subprocess.run = fake_run
        try:
            md.llm_distillate({"excerpt": [("Owner", "hi")], "title": None}, "haiku", "claude")
        finally:
            md.subprocess.run = orig
        self.assertEqual(md._NO_WINDOW, getattr(md.subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(captured.get("creationflags"), md._NO_WINDOW)


class PruneUnit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_prune_drops_old_keeps_recent(self):
        path = os.path.join(self.dir, md.DISTILL_FILE)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"session_id": "old", "ended_at": (NOW - timedelta(days=40)).isoformat()}) + "\n")
            fh.write("not json at all\n")
            fh.write(json.dumps({"session_id": "new", "ended_at": (NOW - timedelta(days=2)).isoformat()}) + "\n")
        removed = md.prune(self.dir, days=30, now=NOW)
        self.assertEqual(removed, 1)  # the 40-day-old record (junk lines aren't counted — see below)
        rows = md._load_lines(path)
        self.assertEqual([r["session_id"] for r in rows], ["new"])
        with open(path, encoding="utf-8") as fh:
            self.assertNotIn("not json", fh.read())  # ...but the rewrite swept the junk line too

    def test_prune_empty_is_noop(self):
        self.assertEqual(md.prune(self.dir, days=30, now=NOW), 0)


class SpawnWiringUnit(unittest.TestCase):
    """session_stamp's SessionEnd → detached distiller spawn (mocked Popen; no real process)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tr = _transcript(os.path.join(self.dir, "t.jsonl"))
        self.spawned = []
        self._orig = st.subprocess.Popen
        st.subprocess.Popen = lambda cmd, **kw: self.spawned.append(cmd)

    def tearDown(self):
        st.subprocess.Popen = self._orig
        os.environ.pop(md.RECURSION_ENV, None)

    def _event(self, **over):
        ev = {"hook_event_name": "SessionEnd", "session_id": "sess-9",
              "transcript_path": self.tr, "cwd": self.dir}
        ev.update(over)
        return ev

    def test_session_end_spawns_distiller_and_clears_entry(self):
        self.assertEqual(st.handle_event(self._event(), self.dir), "cleared")
        (cmd,) = self.spawned
        self.assertIn("mini_dream.py", cmd[1])
        self.assertIn("sess-9", cmd)

    def test_recursion_guard_blocks_spawn(self):
        os.environ[md.RECURSION_ENV] = "1"
        st.handle_event(self._event(), self.dir)
        self.assertEqual(self.spawned, [])  # a distiller's own SessionEnd never dreams the dreamer

    def test_no_transcript_no_spawn(self):
        st.handle_event(self._event(transcript_path=None), self.dir)
        st.handle_event(self._event(transcript_path=os.path.join(self.dir, "gone.jsonl")), self.dir)
        self.assertEqual(self.spawned, [])


if __name__ == "__main__":
    unittest.main()
