#!/usr/bin/env python3
"""Contract tests over the /setup wizard's Markdown chapter files.

The chapters are prose the model follows, so their load-bearing instructions can rot
silently. These tests pin the ones that fixed real snags from a first full walk:
the git-worktree guard, one-question-per-turn pickers, sourced prefills, per-section
resume cursors, and `blocked` reserved for real verify failures.

Stdlib ``unittest`` only. Run:  python -m unittest seneschal.scripts.test_setup_chapters
"""
import os
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(os.path.dirname(os.path.abspath(__file__))).resolve().parents[1]
SETUP = REPO_ROOT / "subagents" / "setup"


def read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


class WorktreeGuard(unittest.TestCase):
    def test_preflight_checks_and_offers_the_explicit_accept(self):
        text = read("subagents/setup/chapters/preflight.md")
        self.assertIn("setup_checkout.py check", text)
        self.assertIn("setup_checkout.py accept", text)
        self.assertIn("use this worktree", text)

    def test_daemon_requires_an_accepted_checkout(self):
        self.assertIn("setup_checkout.py require", read("subagents/setup/chapters/daemon.md"))


class InteractionMode(unittest.TestCase):
    def test_sequencer_ground_rules(self):
        text = read("subagents/setup/SKILL.md")
        self.assertIn("One question per turn", text)
        self.assertIn("AskUserQuestion", text)
        self.assertIn("ends on the question", text)
        self.assertIn("names its source", text)

    def test_owner_interview_restates_pickers_for_the_free_form_sections(self):
        text = read("subagents/setup/chapters/owner-interview.md")
        self.assertIn("Sections 4–6 are free-form, and still one question per turn", text)

    def test_persona_owner_basics_names_prefill_sources(self):
        text = read("subagents/persona-wizard/SKILL.md")
        step7 = text[text.index("**7 — Owner basics**"):text.index("## Generate")]
        self.assertIn("with its source", step7)
        self.assertIn("from your global CLAUDE.md", step7)
        self.assertIn("Never silently accept an inferred fact", step7)


class ResumeCursors(unittest.TestCase):
    def test_owner_interview_marks_a_step_per_section(self):
        text = read("subagents/setup/chapters/owner-interview.md")
        self.assertIn('mark owner-interview in-progress --step "section <n>: <name>"', text)
        sections = re.findall(r"^(\d)\. \*\*", text, flags=re.M)
        self.assertEqual(sections[:8], [str(n) for n in range(1, 9)])


class BlockedMeansFailed(unittest.TestCase):
    def test_env_walker_handles_pending_data_as_done(self):
        text = read("subagents/setup/chapters/env-walker.md")
        self.assertIn("verify.pending_data", text)
        self.assertIn("reserved for a verify that actually failed", text)


if __name__ == "__main__":
    unittest.main()
