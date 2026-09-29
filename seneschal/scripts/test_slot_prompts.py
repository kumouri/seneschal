#!/usr/bin/env python3
"""The enumeration rule for `presence.SLOTS` — a slot prompt that lists a mode's steps IS that run's
scope.

**The failure these guard.** A `dream` slot prompt that lists some of `modes/dream.md`'s steps (say
1/1b, 2, 3 and 5, with the enrichment block 2b-2e nowhere in it) is read by the nightly run as its
SCOPE — the run says so in its Run Log and skips the rest. `modes/dream.md` says there is no such
thing as a scoped Dream — **but the prompt arrives first and the mode file reads as background.** A
skip with no `dream_steps.py` row counts nothing and ages nothing, so an index silently stops being
written: the outage `dream_steps.py` exists to prevent, reintroduced one layer up.

**These are shape tests, not wording tests.** A prompt is prose and will be reworded; what may not
come back is a *partial* step list with nothing marking it as partial. So the assertions are: the
dream prompt points at the mode file, claims every step in it, and names the ledger's skip command —
and, the one that actually catches a regression, it never names a step id *without* that whole-mode
claim standing beside it.

Run: ``python -m unittest discover -s seneschal/scripts -p test_slot_prompts.py``
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import presence  # noqa: E402


def slot(name: str) -> dict:
    for s in presence.SLOTS:
        if s["name"] == name:
            return s
    raise AssertionError(f"no slot named {name!r} in presence.SLOTS")


class DreamSlotPromptTest(unittest.TestCase):
    """The dream slot takes branch (b) of the enumeration rule: point at the mode file and claim
    every step in it, with a step id permitted only as emphasis underneath that claim."""

    def setUp(self):
        self.prompt = slot("dream")["prompt"]

    def test_points_at_the_mode_file(self):
        """A prompt that names the file can be corrected by editing the file. One that restates the
        file's contents forks it, and the fork is what went stale."""
        self.assertIn("seneschal/modes/dream.md", self.prompt)

    def test_claims_the_whole_mode(self):
        self.assertRegex(self.prompt.lower(), r"every step")

    def test_names_the_enrichment_block(self):
        """2b-2e is the block a partial list silently drops; naming it is cheap and it is the part a
        re-scoping edit reaches for first."""
        self.assertIn("2b-2e", self.prompt)

    def test_refuses_a_scoped_dream_in_the_prompt_itself(self):
        """`modes/dream.md` already says this — and a prompt that disagrees wins."""
        self.assertIn("no such thing as a scoped Dream", self.prompt)

    def test_within_window_is_not_a_reason_to_skip(self):
        """"`dream_steps.py status` reads all ok, none OVERDUE" is an alarm being read as a scheduler,
        and the prompt says so."""
        self.assertIn("not a reason to skip", self.prompt)

    def test_a_skip_must_be_recorded(self):
        """`dream_steps` has two verbs, ok and --skip. An invented third — "within window, not
        re-run this pass" — has no row, which is why nothing would count."""
        self.assertIn("dream_steps.py record", self.prompt)
        self.assertIn("--skip", self.prompt)

    def test_a_named_step_never_stands_without_the_whole_mode_claim(self):
        """**The regression test**, and the reason it is phrased this way rather than as "names no
        steps".

        A prompt naming steps 1/1b, 2, 3 and 5 with **nothing saying the list is partial** makes the
        list the scope. The current prompt also names ids — `2b-2e` — but as *emphasis*
        underneath "work EVERY step in that file", which inverts the reading: the ids are the part
        most likely to be dropped, not the whole of what is asked.

        So the checkable property is the pairing, not the absence. Naming an id is legal **only**
        while the whole-mode claim is present; an edit that reintroduces a bare list, or that keeps
        the ids and quietly deletes "every step", fails here. That deletion is the realistic
        regression — it reads as a harmless trim."""
        declared = {"1", "1b", "2", "2b", "2c", "2d", "2e", "2g", "2i", "3", "4", "4d", "5"}
        text = self.prompt.replace("2b-2e", " 2b 2c 2d 2e ")
        named = set(re.findall(r"\bstep (\d+[a-z]?)\b", text, re.I))
        named |= {m for m in re.findall(r"\b(\d[a-z])\b", text) if m in declared}
        if named:
            self.assertRegex(
                self.prompt.lower(), r"every step",
                f"the dream slot prompt names steps {sorted(named)} without claiming every step in "
                f"modes/dream.md — a partial step list with no whole-mode claim becomes the run's "
                f"scope",
            )

    def test_does_not_use_scoping_language(self):
        """"only these", "just", "limit yourself to" — the wordings that turn an emphasis back into a
        list. Cheap, and it costs nothing to keep the door shut."""
        low = self.prompt.lower()
        for phrase in ("only the following", "just these", "limit yourself", "restrict yourself",
                       "scoped dream is", "skip the rest"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, low)


class SeedPromptStillEnumeratesTest(unittest.TestCase):
    """The other branch of the rule, asserted so a well-meaning edit does not "fix" it to match.

    The seed prompt enumerates deliberately: the seed IS those steps, and its own NB says a step
    missing there is a step that does not happen. Emptying it would be the same class of bug in the
    opposite direction.
    """

    def test_seed_prompt_is_not_empty_of_instruction(self):
        self.assertGreater(len(presence.SEED_PROMPT), 200)

    def test_seed_prompt_still_names_the_seed_skill(self):
        self.assertIn("subagents/reminders/SKILL.md", presence.SEED_PROMPT)


class SeedPromptCarriesTheOneOffAutoRetire(unittest.TestCase):
    """The seed enumerates its steps, so the one-off auto-retire only happens because the prompt names
    it — with all four conditions, the ack-date one being the safety rail — and the per-row flags the
    seed script needs (reminders-policy.md "Auto-retire a completed One-off (seed pass)")."""

    def test_auto_retire_is_named_with_its_rails(self):
        p = presence.SEED_PROMPT
        self.assertIn("AUTO-RETIRE", p)
        self.assertIn("One-off", p)
        self.assertIn("never retire an unacked row", p)
        self.assertIn("owner-local date", p)

    def test_seed_passes_the_per_row_flags(self):
        for flag in ("--importance", "--nag", "--due-target", "--consecutive-misses", "--pierce-quiet"):
            with self.subTest(flag=flag):
                self.assertIn(flag, presence.SEED_PROMPT)
        self.assertIn("Nag Until Done ALONE", presence.SEED_PROMPT)

    def test_no_identity_token_survives_rendering(self):
        for token in ("{assistant}", "{owner}", "{tz}"):
            self.assertNotIn(token, presence.SEED_PROMPT)


class EveryOtherSlotStillPointsAtItsModeTest(unittest.TestCase):
    """Cheap breadth: each slot must name the file that owns its steps, so a reader of the prompt
    alone can always find the authority. This is what makes "point at the file" a house shape rather
    than a one-off patch on the slot that happened to break."""

    EXPECTED = {
        "daily-journal": "SKILL.md",
        "morning-brief": "seneschal/SKILL.md",
        "eod-wrap": "seneschal/SKILL.md",
        "dream": "seneschal/SKILL.md",
    }

    def test_each_slot_names_a_source_of_truth(self):
        for name, needle in self.EXPECTED.items():
            with self.subTest(slot=name):
                self.assertIn(needle, slot(name)["prompt"])

    def test_slot_names_are_unique(self):
        names = [s["name"] for s in presence.SLOTS]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
