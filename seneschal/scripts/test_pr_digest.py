#!/usr/bin/env python3
"""Tests for ``pr_digest`` — the PR body reduced to a sentence on each thing it did.

Two classes here are not ordinary unit tests:

* **``TheReferenceOnlyBodyTest``** drives a PR description whose lead-in is nothing but pointers
  (*"`ask-provenance-spec.md` phases 2 and 3"*) while the sentences a reader needs sit three headings
  down the same body — the shape that makes a picker unreadable ("I don't know what those phases
  are; a sentence on each and I'd remember"). If this class goes green on a digest that does not
  name those sections, it is wrong.
* **``TheFlagIsQuietTest``** is the other direction, and the two are in tension on purpose. The
  unexplained-reference note is the only thing here that can be **wrong out loud** — an annotation
  the reader cannot act on is the very shape being fixed — so every path that could fire it
  spuriously is pinned: a coded reference never fires it, and a reference the outline accounts for
  in any way is silent.

Pure text in and text out: nothing here touches the network, the filesystem, `state/`, or Telegram.

Run:  python -m unittest test_pr_digest   (from seneschal/scripts)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pr_digest as pd

#: A reference-only PR description, trimmed to the bands that matter: the lead-in a picker relays,
#: and the headings it does not.
REF_BODY = """`ask-provenance-spec.md` phases 2 and 3. **Phase 4 is deliberately left unbuilt — the question for you is at the bottom of this description.**

Status header moves `PARTIAL(phase 1)` → `PARTIAL(phases 1-3; phase 4 open, gated on the owner's decision)`.

---

## Phase 2 — a landed picker enters the record of what the assistant has said

`state/assertions.jsonl` is *"the append-only record of what the assistant has ACTUALLY said to the owner"*. The census in §3.4 found **thousands of rows in six kinds and not one question**.

## Phase 3 — the no-access assertion, then the literal check

**In §5.3's order: the assertion first.** `references/comms-mapping.md` gains a *What the assistant cannot reach* section.

## Phase 4 — the question, and it is yours

Phases 1-3 are mechanisms: each does a thing that is either done or not, checkable against the tree.
"""


class SectionsTest(unittest.TestCase):
    def test_it_finds_every_heading_with_its_own_body(self):
        secs = pd.sections("## A\n\nalpha text here.\n\n### B\n\nbeta text here.\n")
        self.assertEqual([s["title"] for s in secs], ["A", "B"])
        self.assertEqual([s["level"] for s in secs], [2, 3])
        self.assertIn("alpha", secs[0]["body"])
        self.assertNotIn("beta", secs[0]["body"])

    def test_a_section_stops_at_the_next_heading_of_ANY_level(self):
        """Otherwise an excerpt spans into a sibling and attributes its words to the wrong one —
        `ask_citations.section_excerpt`'s rule, for the same reason."""
        secs = pd.sections("## A\n\nalpha.\n\n#### deep\n\nnot alpha.\n")
        self.assertNotIn("not alpha", secs[0]["body"])

    def test_text_before_the_first_heading_belongs_to_no_section(self):
        secs = pd.sections("preamble prose.\n\n## A\n\nalpha.\n")
        self.assertEqual(len(secs), 1)
        self.assertNotIn("preamble", secs[0]["body"])

    def test_a_body_with_no_headings_yields_nothing(self):
        self.assertEqual(pd.sections("just a paragraph."), [])
        self.assertEqual(pd.sections(""), [])
        self.assertEqual(pd.sections(None), [])


class HeadingLabelsTest(unittest.TestCase):
    def test_a_worded_ordinal(self):
        self.assertEqual(pd.heading_labels("Phase 2 — a landed picker"), {"phase:2"})
        self.assertEqual(pd.heading_labels("Step 3: the gate"), {"step:3"})

    def test_a_coded_ordinal(self):
        self.assertEqual(pd.heading_labels("R1 — the phase-2 caveat"), {"code:R1"})

    def test_a_bare_number(self):
        self.assertEqual(pd.heading_labels("1. The vocabulary"), {"num:1"})

    def test_it_reads_the_LEADING_ordinal_only(self):
        """`### R1 — the phase-2 caveat, written where phase 2 is DEFINED` is a section ABOUT R1
        that mentions phase 2. Harvesting every ordinal in a title would make it the answer to
        *"what is phase 2?"*, and it is not."""
        self.assertEqual(pd.heading_labels("R1 — the phase-2 caveat, written where phase 2 is"),
                         {"code:R1"})

    def test_a_heading_with_no_ordinal_declares_none(self):
        self.assertEqual(pd.heading_labels("What this does not touch"), set())


class OrdinalReferencesTest(unittest.TestCase):
    def _labels(self, text):
        return [r["label"] for r in pd.ordinal_references(text)]

    def test_a_conjunction_lists_each(self):
        self.assertEqual(self._labels("phases 2 and 3"), ["phase:2", "phase:3"])

    def test_a_dashed_range_expands(self):
        self.assertEqual(self._labels("phases 1-3"), ["phase:1", "phase:2", "phase:3"])
        self.assertEqual(self._labels("steps 1 to 3"), ["step:1", "step:2", "step:3"])

    def test_a_coded_range_expands_and_keeps_its_letter(self):
        self.assertEqual(self._labels("This is R1–R4 and the picker mechanism"),
                         ["code:R1", "code:R2", "code:R3", "code:R4"])

    def test_plurals_normalise_to_one_label(self):
        self.assertEqual(self._labels("fixes 1 & 2"), ["fix:1", "fix:2"])
        self.assertEqual(self._labels("phase 2 ... phases 2 and 5"), ["phase:2", "phase:5"])

    def test_a_lettered_endpoint_is_not_interpolated(self):
        """`2a-3b` keeps its two endpoints. Nobody can say what lies between them, and inventing
        `2b` would put a section number in front of the reader that the author never wrote."""
        self.assertEqual(self._labels("steps 2a-3b"), ["step:2a", "step:3b"])

    def test_the_two_families_are_marked_apart(self):
        worded = {r["raw"]: r["worded"] for r in pd.ordinal_references("phase 4 and T2")}
        self.assertTrue(worded["phase 4"])
        self.assertFalse(worded["T2"])

    def test_a_bare_word_with_no_number_is_not_a_reference(self):
        self.assertEqual(self._labels("this phase of the work"), [])


class FirstSentenceTest(unittest.TestCase):
    def test_it_stops_at_the_first_full_stop(self):
        got = pd.first_sentence("Alpha happened here and it mattered. Beta later.", 200)
        self.assertEqual(got, "Alpha happened here and it mattered.")

    def test_it_takes_a_second_when_the_first_is_a_fragment(self):
        """`## First: does §6's reason still hold? **No — and it was two clauses.**` opens with a
        two-word answer; alone it says nothing at all."""
        got = pd.first_sentence("No. The clause asks for the change to be specced first.", 200)
        self.assertIn("The clause asks", got)

    def test_an_abbreviation_is_not_a_sentence_end(self):
        got = pd.first_sentence("It drops e.g. the footer and the fences entirely. Then it cuts.",
                                200)
        self.assertIn("the footer and the fences", got)
        self.assertNotIn("Then it cuts", got)

    def test_a_version_number_is_not_a_sentence_end(self):
        got = pd.first_sentence("`state/assertions.jsonl` is the record. Next.", 200)
        self.assertIn("assertions.jsonl", got)

    def test_a_markdown_table_is_dropped_whole(self):
        """Condensed onto one line a table becomes `| Token | Means | |---|---| | BUILT | …`, which
        spends a whole outline entry on punctuation. A status-vocabulary section is exactly this."""
        got = pd.first_sentence("| Token | Means |\n|---|---|\n| `BUILT` | it all exists |\n", 200)
        self.assertEqual(got, "")

    def test_a_section_that_is_prose_then_a_table_keeps_the_prose(self):
        got = pd.first_sentence("Five values, one each.\n\n| a | b |\n|---|---|\n", 200)
        self.assertEqual(got, "Five values, one each.")

    def test_markup_is_stripped_but_the_words_are_not(self):
        got = pd.first_sentence("**In §5.3's order: the assertion first.** And then the check.", 200)
        self.assertNotIn("**", got)
        self.assertIn("In §5.3's order: the assertion first.", got)


class OutlineTest(unittest.TestCase):
    BODY = ("## Alpha\n\nalpha prose here and it runs on.\n\n"
            "## Beta\n\nbeta prose here and it runs on.\n\n"
            "## Gamma\n\ngamma prose here and it runs on.\n")

    def test_each_entry_is_a_heading_and_a_sentence(self):
        lines, dropped = pd.outline(self.BODY, room=2000, entry_chars=220, max_entries=6)
        self.assertEqual(dropped, 0)
        self.assertEqual(lines[0], "• Alpha: alpha prose here and it runs on.")

    def test_a_referenced_section_is_selected_first_but_rendered_in_document_order(self):
        """Selection and rendering are two different orders. A reordered outline reads as a
        different argument than the one the author made."""
        body = ("## Alpha\n\n" + "a" * 300 + ".\n\n"
                "## Beta\n\n" + "b" * 300 + ".\n\n"
                "## Phase 3 — the late one\n\nthe third thing happened here.\n")
        lines, _ = pd.outline(body, room=250, entry_chars=220, max_entries=6,
                              wanted=["phase:3"])
        self.assertEqual(len(lines), 1)
        self.assertIn("Phase 3", lines[0])

    def test_document_order_survives_a_reference_driven_selection(self):
        body = ("## Phase 9 — the late one\n\nlate prose.\n\n"
                "## Aardvark\n\nearly prose.\n")
        lines, _ = pd.outline(body, room=2000, entry_chars=220, max_entries=6, wanted=["phase:9"])
        self.assertTrue(lines[0].startswith("• Phase 9"))

    def test_a_bare_number_heading_answers_a_worded_reference(self):
        body = "## 1. The vocabulary\n\nfive values, one each.\n\n## Elsewhere\n\nother prose.\n"
        lines, _ = pd.outline(body, room=60, entry_chars=220, max_entries=6, wanted=["step:1"])
        self.assertEqual(len(lines), 1)
        self.assertIn("vocabulary", lines[0])

    def test_the_budget_binds_and_what_did_not_fit_is_COUNTED(self):
        lines, dropped = pd.outline(self.BODY, room=90, entry_chars=220, max_entries=6)
        self.assertEqual(len(lines), 2)
        self.assertEqual(dropped, 1)

    def test_max_entries_binds_independently_of_room(self):
        lines, dropped = pd.outline(self.BODY, room=5000, entry_chars=220, max_entries=2)
        self.assertEqual(len(lines), 2)
        self.assertEqual(dropped, 1)

    def test_one_long_section_cannot_eat_the_others(self):
        body = "## Alpha\n\n" + ("word " * 400) + ".\n\n## Beta\n\nbeta prose here.\n"
        lines, _ = pd.outline(body, room=2000, entry_chars=120, max_entries=6)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(l) <= 122 for l in lines), lines)

    def test_a_section_with_no_prose_renders_as_its_heading_alone(self):
        lines, _ = pd.outline("## Alpha\n\n## Beta\n\nbeta prose.\n", room=2000, entry_chars=220,
                              max_entries=6)
        self.assertEqual(lines[0], "• Alpha")

    def test_a_heading_that_already_ends_in_a_stop_is_not_given_a_colon(self):
        lines, _ = pd.outline("## R2 — it was blind. Deadline set.\n\nWhat it was.\n",
                              room=2000, entry_chars=220, max_entries=6)
        self.assertNotIn(".:", lines[0])

    def test_avoid_drops_a_section_already_shown_in_the_lead_in(self):
        lines, _ = pd.outline("## Summary\n\nIt does the thing entirely.\n\n## Why\n\nBecause.\n",
                              room=2000, entry_chars=220, max_entries=6,
                              avoid="It does the thing entirely.")
        self.assertEqual(lines, ["• Why: Because."])

    def test_a_dropped_duplicate_is_not_counted_as_TRUNCATED(self):
        """It was shown, not cut. Counting it would print `(+1 more section)` under an outline
        that is in fact complete — visible truncation lying in the other direction."""
        _lines, dropped = pd.outline("## Summary\n\nIt does the thing entirely.\n",
                                     room=2000, entry_chars=220, max_entries=6,
                                     avoid="It does the thing entirely.")
        self.assertEqual(dropped, 0)


class TheReferenceOnlyBodyTest(unittest.TestCase):
    """The lead-in is what a picker relays; the headings are what the reader needs."""

    def _lines(self, room=900):
        wanted = [r["label"] for r in pd.ordinal_references(
            "feat(telegram_ask): ask-provenance phases 2-3 — the Mouth row and the no-access flag")]
        return pd.outline(REF_BODY, room=room, entry_chars=220, max_entries=6, wanted=wanted)[0]

    def test_the_lead_in_alone_is_made_of_pointers(self):
        """The premise. Every ordinal in the two relayed sentences is a reference to something the
        message never shows."""
        lead = REF_BODY.split("---")[0]
        self.assertEqual([r["label"] for r in pd.ordinal_references(lead)],
                         ["phase:2", "phase:3", "phase:4", "phase:1"])

    def test_it_now_gives_a_sentence_on_each(self):
        lines = self._lines()
        self.assertIn("• Phase 2 — a landed picker enters the record of what the assistant has said:",
                      "\n".join(lines))
        self.assertIn("append-only record of what the assistant has ACTUALLY said", "\n".join(lines))
        self.assertIn("Phase 3 — the no-access assertion", "\n".join(lines))
        self.assertIn("Phase 4 — the question, and it is yours", "\n".join(lines))

    def test_the_TITLE_s_promise_is_kept_even_under_a_tight_budget(self):
        """The title says *"phases 2-3"*. Those two sections are what survives when there is only
        room for some — the picker's own words are a promise the outline has to keep."""
        text = "\n".join(self._lines(room=400))
        self.assertIn("Phase 2", text)
        self.assertIn("Phase 3", text)

    def test_nothing_is_left_unexplained(self):
        self.assertEqual(pd.unexplained(REF_BODY.split("---")[0], self._lines()), [])


class TheFlagIsQuietTest(unittest.TestCase):
    """The unexplained note is the only thing here that can be wrong OUT LOUD."""

    def test_it_names_a_reference_the_body_never_defines(self):
        lines = ["• Fix 1: the read door omitted the field."]
        self.assertEqual(pd.unexplained("Phase 1 of the spec merged this morning.", lines),
                         ["Phase 1"])

    def test_a_coded_reference_NEVER_fires_it(self):
        """`T2` in prose might be anything. A note about it is precisely the unexplained annotation
        this whole change is against — selection may guess, a flag may not."""
        self.assertEqual(pd.unexplained("This lands T2 and R7.", ["• Something else: prose."]), [])

    def test_a_heading_that_declares_the_ordinal_silences_it(self):
        self.assertEqual(pd.unexplained("builds phase 2", ["• Phase 2 — the predicate: prose."]), [])

    def test_a_MENTION_inside_an_outline_entry_silences_it_too(self):
        """Generous on purpose. A section whose sentence says *"phases 1-3 are mechanisms"* has told
        the reader what phases 1-3 are well enough, and a flag beside it would read as a broken checker."""
        lines = ["• Phase 4 — the question: Phases 1-3 are mechanisms, each done or not."]
        self.assertEqual(pd.unexplained("moves PARTIAL(phase 1) onward", lines), [])

    def test_it_dedupes_on_the_words_the_reader_would_see(self):
        got = pd.unexplained("phase 7 here, and phase 7 again, and phase 8.", [])
        self.assertEqual(got, ["phase 7", "phase 8"])

    def test_an_empty_outline_still_answers(self):
        self.assertEqual(pd.unexplained("builds phase 2", []), ["phase 2"])
        self.assertEqual(pd.unexplained("", ["• A: prose."]), [])


class NothingHereReachesTheWorldTest(unittest.TestCase):
    """Pure text shaping. This module is reached from a `PreToolUse` hook's send path, so an import
    that opens a file, a socket or a subprocess would run on a merge picker."""

    def test_module_scope_imports_are_stdlib_plus_ask_citations(self):
        import ast
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pr_digest.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"__future__", "re", "ask_citations"})

    def test_condense_is_ask_citations_own_function_and_not_a_second_copy(self):
        """Two spellings of *"markdown, readable on a phone"* is two things to keep honest, and the
        one that drifts is the one nobody is looking at."""
        import ask_citations
        self.assertIs(pd.condense, ask_citations._condense)


if __name__ == "__main__":
    unittest.main()
