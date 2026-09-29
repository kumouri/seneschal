#!/usr/bin/env python3
"""`state/context-digest.md` is retired (`../docs/read-first-retirement-spec.md`). Dream no longer
writes it and Brief no longer reads it; the only `context-digest.md` mentions left in either mode's
own prose describe the RETIREMENT itself or the one-shot `standing_safety.py import-digest` migration.

These are shape tests over the mode bodies' prose, the same kind `test_slot_prompts.py` already runs
over `presence.SLOTS` — a wording test would break on every rephrase, so what is pinned here is the
one property that matters: no live instruction to write, overwrite, or read `state/context-digest.md`
survives outside the retirement/migration narrative.

Stdlib ``unittest`` only; no daemon, no `claude`, no network.
Run:  python -m unittest discover -s seneschal/scripts -p "test_digest_retirement.py"   (from the repo root)
"""
import os
import re
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SENESCHAL_DIR = os.path.dirname(SCRIPT_DIR)
DREAM_MD = os.path.join(SENESCHAL_DIR, "modes", "dream.md")
BRIEF_MD = os.path.join(SENESCHAL_DIR, "modes", "brief.md")

#: A `context-digest.md` mention is allowed ONLY if one of these words appears within `_WINDOW`
#: characters of it — the retirement fact, or the one-shot migration Dream still runs once. Markdown wraps prose across lines, so this checks a character window rather than the line the
#: match sits on; anything else naming the file is a live instruction that should not exist any more.
_ALLOWED_NEARBY = re.compile(r"RETIRED|retirement|import-digest", re.IGNORECASE)
_MENTION = re.compile(r"context-digest", re.IGNORECASE)
_WINDOW = 220

#: The plain statement that the digest is retired — matched case-insensitively, so "is retired" and
#: "is RETIRED" both count; what is pinned is that the statement exists, not its capitalisation.
_RETIRED_STATEMENT = re.compile(r"context-digest\.md`?\*{0,2}\s+is\s+(fully\s+)?retired", re.IGNORECASE)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _unexplained_mentions(text: str) -> list[str]:
    offenders = []
    for m in _MENTION.finditer(text):
        lo, hi = max(0, m.start() - _WINDOW), min(len(text), m.end() + _WINDOW)
        context = text[lo:hi]
        if not _ALLOWED_NEARBY.search(context):
            offenders.append(" ".join(context.split()))
    return offenders


class DreamNoLongerWritesTheDigest(unittest.TestCase):
    def setUp(self):
        self.text = _read(DREAM_MD)

    def test_every_context_digest_mention_is_retirement_or_migration_prose(self):
        offenders = _unexplained_mentions(self.text)
        self.assertEqual(offenders, [], f"live context-digest.md reference(s) survived: {offenders}")

    def test_step_1_no_longer_condenses_the_day_into_the_digest(self):
        self.assertNotIn("Condense the day", self.text)
        self.assertNotIn("**Overwrite** the digest", self.text)

    def test_the_retirement_is_stated_plainly(self):
        self.assertRegex(self.text, _RETIRED_STATEMENT)

    def test_the_one_shot_migration_is_still_instructed(self):
        # Dream runs `import-digest` once, then never again — the CLI's own refusal
        # on a second run (no `--force`) is what makes "never again" true without relying on memory.
        self.assertIn("import-digest", self.text)
        self.assertIn("exactly once", re.sub(r"\s+", " ", self.text))

    def test_open_loops_now_route_to_the_open_work_register(self):
        self.assertIn("open-loops.json", self.text)
        self.assertIn("loops.py", self.text)

    def test_no_fold_into_the_digest_instructions_remain(self):
        self.assertNotIn("into the digest", self.text.lower())


class BriefNoLongerReadsTheDigest(unittest.TestCase):
    def setUp(self):
        self.text = _read(BRIEF_MD)

    def test_every_context_digest_mention_is_retirement_prose(self):
        offenders = _unexplained_mentions(self.text)
        self.assertEqual(offenders, [], f"live context-digest.md reference(s) survived: {offenders}")

    def test_phase_0_no_longer_reads_the_digest_first(self):
        self.assertNotIn("Read `state/context-digest.md` first", self.text)

    def test_phase_0_reads_the_run_log_and_carry_over_instead(self):
        self.assertIn("state/run-log.md", self.text)
        self.assertIn("state/carry-over.md", self.text)

    def test_the_retirement_is_stated_plainly(self):
        self.assertRegex(self.text, _RETIRED_STATEMENT)


if __name__ == "__main__":
    unittest.main()
