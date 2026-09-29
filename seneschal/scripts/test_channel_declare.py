#!/usr/bin/env python3
"""Tests for `channel_declare.py` — the pure parse/strip/resolve machinery of
`seneschal/docs/message-routing-spec.md`'s forcing function. The drainer-level wiring (the retry
loop's exit paths, the grounding plumbing) belongs with the presence wiring; this file is everything
that needs no daemon at all.

Topic resolution runs against a FIXTURE routing table in a temp references dir (the shipped
example's three purposes plus a neutral extra, `projects`) and a temp state dir, so neither an
owner's real `telegram-topics.json` nor their runtime-minted topics can change what these assert.
The owner's name is patched the same way, so an installed `persona/identity.json` cannot either.

Stdlib ``unittest`` only; no daemon, no `claude`, no network.
Run:  python -m unittest test_channel_declare   (from seneschal/scripts)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import channel_declare as cd  # noqa: E402
import identity_common  # noqa: E402
import telegram_topics as tt  # noqa: E402

#: The shipped example's purposes plus one neutral extra, so a test can exercise a non-default topic.
FIXTURE_TOPICS = {"pull-requests": "Pull requests", "decisions": "Decisions",
                  "reminders": "Reminders", "projects": "Projects"}

#: The unpatched loader, captured once so a test that swaps tables twice still reads through the real
#: function rather than through the previous patch.
_REAL_LOAD_TOPIC_NAMES = tt._load_topic_names

#: Owner-facing prompt prose names the owner or says "the owner" — it never assumes a pronoun.
GENDERED_PRONOUNS = ("she", "her", "hers", "he", "him", "his")


def assert_no_gendered_pronoun(test: unittest.TestCase, text: str) -> None:
    words = set(text.lower().replace(",", " ").replace(".", " ").split())
    test.assertEqual(words & set(GENDERED_PRONOUNS), set(), text)


def use_fixture_table(test: unittest.TestCase, topics: dict | None = None) -> None:
    """Point `telegram_topics._load_topic_names` at a temp references dir holding `topics` (as the
    owner's `telegram-topics.json`) and an empty temp state dir, for the life of `test`. Same seam
    `test_telegram_topics.py` uses; the real loader still does the reading."""
    refs = tempfile.mkdtemp()
    state = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, refs, True)
    test.addCleanup(shutil.rmtree, state, True)
    with open(os.path.join(refs, tt.REFERENCES_FILE), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "topics": dict(FIXTURE_TOPICS if topics is None else topics)}, fh)
    patcher = mock.patch.object(tt, "_load_topic_names",
                                lambda *a, **kw: _REAL_LOAD_TOPIC_NAMES(refs, state))
    patcher.start()
    test.addCleanup(patcher.stop)


def use_owner_name(test: unittest.TestCase, name: str | None) -> None:
    """Pin what `identity_common.load_identity` answers, so the prompt prose is deterministic."""
    ident = {"owner": {"name": name}}
    patcher = mock.patch.object(identity_common, "load_identity", lambda *a, **kw: ident)
    patcher.start()
    test.addCleanup(patcher.stop)


class ExtractionTests(unittest.TestCase):
    """§1: position-0-only, the exact one-newline strip, and the four named edge cases."""

    def test_a_plain_declaration_is_parsed_and_stripped(self):
        declared, rest = cd.extract_channel_declaration("[[channel:projects]]\nThe build status is X.")
        self.assertEqual(declared, "projects")
        self.assertEqual(rest, "The build status is X.")

    def test_no_leading_blank_line_survives_the_strip(self):
        # The matched line AND its one trailing newline are removed — never two newlines' worth.
        declared, rest = cd.extract_channel_declaration("[[channel:main]]\nhello")
        self.assertEqual(rest, "hello")
        self.assertFalse(rest.startswith("\n"))

    def test_extra_blank_lines_after_the_marker_are_also_swallowed(self):
        # `\s*` before the mandatory trailing newline is greedy, so any run of blank lines right after
        # the marker goes with it — which is what makes "no leading blank line" (the spec's own stated
        # goal) hold regardless of how many the model inserts, not just exactly one.
        declared, rest = cd.extract_channel_declaration("[[channel:main]]\n\n\nhello")
        self.assertEqual(declared, "main")
        self.assertEqual(rest, "hello")
        self.assertFalse(rest.startswith("\n"))

    def test_mid_message_occurrence_is_never_parsed(self):
        text = "Sure, here's the update.\n[[channel:projects]]\nmore text"
        declared, rest = cd.extract_channel_declaration(text)
        self.assertIsNone(declared)
        self.assertEqual(rest, text)

    def test_code_fence_occurrence_is_never_parsed(self):
        text = "```\n[[channel:projects]]\n```\nhere's how the feature works"
        declared, rest = cd.extract_channel_declaration(text)
        self.assertIsNone(declared)
        self.assertEqual(rest, text)

    def test_quoted_example_at_position_zero_still_parses_as_a_real_declaration(self):
        # Position 0 IS the rule, deliberately with no exception for "but it's an example": the parser
        # cannot distinguish the assistant demonstrating the syntax from using it, and the spec is
        # explicit that this asymmetry is accepted rather than solved.
        text = '[[channel:main]]\nThe feature works like "[[channel:projects]]", as I mentioned.'
        declared, rest = cd.extract_channel_declaration(text)
        self.assertEqual(declared, "main")
        self.assertIn("[[channel:projects]]", rest)  # the QUOTED instance is untouched, mid-message

    def test_no_trailing_newline_is_not_a_match(self):
        # No newline after the closing brackets means the line never terminates — position-0 anchor
        # with no newline to consume is a no-match, byte-for-byte, not a partial strip.
        text = "[[channel:main]]"
        declared, rest = cd.extract_channel_declaration(text)
        self.assertIsNone(declared)
        self.assertEqual(rest, text)

    def test_empty_reply_is_a_no_op(self):
        self.assertEqual(cd.extract_channel_declaration(""), (None, ""))

    def test_an_ordinary_reply_with_no_declaration_is_unchanged(self):
        text = "Sure, here's what's on your calendar today."
        self.assertEqual(cd.extract_channel_declaration(text), (None, text))

    def test_mixed_case_purpose_is_captured_and_lower_cased(self):
        # See the module docstring: the spec's literal regex is lowercase-only, which would leak an
        # echoed display title (e.g. "Projects") straight through unstripped. Widened deliberately.
        declared, rest = cd.extract_channel_declaration("[[channel:Projects]]\nhello")
        self.assertEqual(declared, "projects")
        self.assertEqual(rest, "hello")
        self.assertNotIn("[[channel:", rest)  # the marker never leaks regardless of case

    def test_purpose_value_case_never_leaks_unstripped(self):
        # The literal `[[channel:` is matched lowercase only — the instructions always tell the model
        # to spell it that way. What must be tolerant is the VALUE (§4's display-title-echo trap), not
        # the control tokens around it.
        for variant in ("[[channel:MAIN]]\nx", "[[channel:Main]]\nx", "[[channel:Projects]]\nx"):
            with self.subTest(variant=variant):
                declared, rest = cd.extract_channel_declaration(variant)
                self.assertIsNotNone(declared)
                self.assertEqual(declared, declared.lower())
                self.assertNotIn("[[", rest)


class ReplyMarkerTests(unittest.TestCase):
    """`reply-marker-forcing-function-spec.md` §1-§3: the second required opening line. Unlike
    `extract_channel_declaration`, `has_reply_marker` never strips — §2's whole point is that this
    line must reach the owner intact."""

    def test_a_plain_marker_is_detected(self):
        self.assertTrue(cd.has_reply_marker("*(answering your question)*\nHere's the update."))

    def test_no_marker_is_not_detected(self):
        self.assertFalse(cd.has_reply_marker("Here's the update."))

    def test_empty_reply_has_no_marker(self):
        self.assertFalse(cd.has_reply_marker(""))

    def test_mid_message_occurrence_is_never_detected(self):
        text = "Sure, here's the update.\n*(answering nothing)*\nmore text"
        self.assertFalse(cd.has_reply_marker(text))

    def test_marker_alone_with_no_trailing_newline_still_matches(self):
        # The corrective retry's own answer may be exactly this one line, nothing after it.
        self.assertTrue(cd.has_reply_marker("*(answering your question)*"))

    def test_marker_survives_untouched_when_present(self):
        # has_reply_marker is READ-ONLY — the module docstring's "not stripped" rule.
        text = "*(answering your question)*\nHere's the update."
        self.assertTrue(cd.has_reply_marker(text))
        # Confirm nothing about the string can have been mutated by the check itself.
        self.assertEqual(text, "*(answering your question)*\nHere's the update.")

    def test_a_parenthetical_inside_the_content_does_not_break_the_match(self):
        text = "*(answering your question about PRs (the second one))*\nHere's the update."
        self.assertTrue(cd.has_reply_marker(text))

    def test_case_insensitive_on_the_word_answering(self):
        self.assertTrue(cd.has_reply_marker("*(Answering your question)*\nok"))
        self.assertTrue(cd.has_reply_marker("*(ANSWERING your question)*\nok"))

    def test_extract_reply_marker_line_returns_the_matched_line_only(self):
        line = cd.extract_reply_marker_line("*(answering your question)*\nextra prose the model added")
        self.assertEqual(line, "*(answering your question)*")
        self.assertNotIn("extra prose", line)

    def test_extract_reply_marker_line_none_on_no_match(self):
        self.assertIsNone(cd.extract_reply_marker_line("no marker here"))
        self.assertIsNone(cd.extract_reply_marker_line(""))


class StripStrayControlLinesTests(unittest.TestCase):
    """The retry-only cleanup that keeps a corrective's prepended marker from landing beside
    a stray, orphaned control line the original reply already carried mid-message."""

    def test_a_standalone_channel_line_outside_a_fence_is_removed(self):
        text = "Both jobs still running.\n\n[[channel:main]]\n\nmore text"
        self.assertNotIn("[[channel:", cd.strip_stray_control_lines(text))

    def test_a_standalone_marker_line_outside_a_fence_is_removed(self):
        text = "Both jobs still running.\n\n*(answering \"that flake\")*\n\nmore text"
        self.assertNotIn("*(answering", cd.strip_stray_control_lines(text))

    def test_a_buried_pair_of_control_lines_is_cleaned(self):
        original = ('Both jobs still running. Now the reply.\n\n[[channel:main]]\n'
                    '*(answering "that one\'s a flake we can\'t fix")*\n\n'
                    '**You\'re right on the definition …**')
        cleaned = cd.strip_stray_control_lines(original)
        self.assertNotIn("[[channel:", cleaned)
        self.assertNotIn("*(answering", cleaned)
        self.assertIn("Both jobs still running.", cleaned)
        self.assertIn("You're right on the definition", cleaned)

    def test_a_line_inside_a_code_fence_survives_untouched(self):
        # The one deliberate escape hatch: the assistant explaining the feature must still be able to quote
        # the syntax without this cleanup eating the quote.
        text = "Sure, here's how it works:\n```\n[[channel:projects]]\n```\nthat's the syntax."
        self.assertEqual(cd.strip_stray_control_lines(text), text)

    def test_a_marker_quoted_inside_a_fence_also_survives(self):
        text = "Example:\n```\n*(answering your question)*\n```\ndone."
        self.assertEqual(cd.strip_stray_control_lines(text), text)

    def test_narration_that_merely_mentions_the_syntax_mid_sentence_is_untouched(self):
        # Only a line that IS exactly a control line is dropped — not any line that references one.
        text = "The [[channel:main]] token is what gets parsed."
        self.assertEqual(cd.strip_stray_control_lines(text), text)

    def test_a_clean_reply_with_no_stray_lines_is_unchanged(self):
        text = "Here's the update.\nNothing to strip here."
        self.assertEqual(cd.strip_stray_control_lines(text), text)

    def test_empty_text_is_a_no_op(self):
        self.assertEqual(cd.strip_stray_control_lines(""), "")

    def test_multiple_stray_marker_lines_all_come_out(self):
        text = "*(answering one thing)*\nnarration\n*(answering another thing)*\nreal content"
        cleaned = cd.strip_stray_control_lines(text)
        self.assertNotIn("*(answering", cleaned)
        self.assertIn("narration", cleaned)
        self.assertIn("real content", cleaned)


class RetryPromptTests(unittest.TestCase):
    """§3: one prompt naming whichever required line(s) are missing — never two round trips for a
    reply missing both. Rendered fresh: the live topic list, and the owner's configured name."""

    def setUp(self):
        use_fixture_table(self)
        use_owner_name(self, None)

    def test_only_channel_missing_uses_the_channel_prompt(self):
        prompt = cd.retry_prompt(missing_channel=True, missing_marker=False)
        self.assertEqual(prompt, cd.CHANNEL_RETRY_PROMPT_TEMPLATE.format(
            choices="main, decisions, projects, pull-requests, reminders"))
        self.assertNotIn("answering", prompt)

    def test_only_marker_missing_uses_the_marker_only_prompt(self):
        prompt = cd.retry_prompt(missing_channel=False, missing_marker=True)
        self.assertEqual(prompt, cd.REPLY_MARKER_RETRY_PROMPT_TEMPLATE.format(owner="the owner"))
        self.assertNotIn("[[channel:", prompt)

    def test_both_missing_uses_one_combined_prompt_naming_both(self):
        prompt = cd.retry_prompt(missing_channel=True, missing_marker=True)
        self.assertIn("[[channel:", prompt)
        self.assertIn("answering", prompt)
        self.assertIn("projects", prompt)

    def test_neither_missing_is_unreachable_in_practice_but_still_returns_the_marker_prompt(self):
        # The drainer never calls this when both are satisfied (the loop condition guards it), but
        # the function itself has no third state to fall into — documented rather than asserted as
        # unreachable.
        self.assertEqual(cd.retry_prompt(missing_channel=False, missing_marker=False),
                         cd.retry_prompt(missing_channel=False, missing_marker=True))

    def test_the_topic_list_is_the_live_table_not_a_pinned_string(self):
        use_fixture_table(self, {"decisions": "Decisions", "garden": "Garden"})
        prompt = cd.retry_prompt(missing_channel=True, missing_marker=False)
        self.assertIn("garden", prompt)
        self.assertNotIn("projects", prompt)

    def test_a_configured_owner_name_is_used_and_no_pronoun_is_assumed(self):
        use_owner_name(self, "Alex")
        for args in ((True, True), (False, True)):
            prompt = cd.retry_prompt(*args)
            self.assertIn("Alex", prompt)
            assert_no_gendered_pronoun(self, prompt)

    def test_an_unreadable_table_still_yields_a_prompt(self):
        with mock.patch.object(tt, "_load_topic_names", side_effect=RuntimeError("boom")):
            prompt = cd.retry_prompt(missing_channel=True, missing_marker=False)
        self.assertIn("one of: main (or", prompt)


class ResolvePurposeTests(unittest.TestCase):
    """§4, run against the fixture table — pull-requests / decisions / reminders / projects."""

    def setUp(self):
        use_fixture_table(self)

    def test_none_resolves_to_main(self):
        self.assertEqual(cd.resolve_purpose(None), tt.TOPIC_MAIN_CHAT)

    def test_exact_names_resolve_to_themselves(self):
        for name in ("pull-requests", "decisions", "reminders", "projects"):
            self.assertEqual(cd.resolve_purpose(name), name)

    def test_explicit_main_resolves_to_main(self):
        self.assertEqual(cd.resolve_purpose("main"), tt.TOPIC_MAIN_CHAT)

    def test_the_cutoff_constant_is_eighty_percent(self):
        self.assertEqual(cd.CHANNEL_FUZZY_CUTOFF, 0.8)

    def test_close_misspellings_resolve_to_the_real_topic(self):
        cases = {
            "project": "projects", "projcts": "projects", "projectss": "projects",
            "prjects": "projects", "decission": "decisions", "decison": "decisions",
            "decisons": "decisions", "decision": "decisions", "reminder": "reminders",
            "remindrs": "reminders", "remider": "reminders", "pull-request": "pull-requests",
            "pull requests": "pull-requests", "pullrequests": "pull-requests",
        }
        for typo, want in cases.items():
            with self.subTest(typo=typo):
                self.assertEqual(cd.resolve_purpose(typo), want)

    def test_the_display_title_casing_trap_is_fixed(self):
        # §4's finding: an echoed display title in the wrong case can score below the 0.8 cutoff
        # UNNORMALIZED ("PROJECTS" scores 0 against "projects"). `.strip().lower()` first is what
        # makes this resolve correctly instead of silently defaulting to main.
        self.assertEqual(cd.resolve_purpose("Projects"), "projects")
        self.assertEqual(cd.resolve_purpose("  PROJECTS  "), "projects")

    def test_desicions_is_the_documented_near_miss_and_falls_to_main(self):
        # 0.778 against "decisions" — just under the 0.8 cutoff. Not a defect: main is the same place
        # an undeclared reply already lands.
        self.assertEqual(cd.resolve_purpose("desicions"), tt.TOPIC_MAIN_CHAT)

    def test_an_abbreviation_falls_to_main_not_a_wrong_real_topic(self):
        self.assertEqual(cd.resolve_purpose("prs"), tt.TOPIC_MAIN_CHAT)

    def test_main_typos_fall_to_main_not_a_real_topic(self):
        for typo in ("mian", "amin"):
            self.assertEqual(cd.resolve_purpose(typo), tt.TOPIC_MAIN_CHAT)

    def test_junk_input_falls_to_main(self):
        self.assertEqual(cd.resolve_purpose("xyzzy-not-a-real-topic-at-all"), tt.TOPIC_MAIN_CHAT)

    def test_never_returns_none(self):
        for value in (None, "", "   ", "xyz", "projects", "Projects", "MAIN"):
            self.assertIsNotNone(cd.resolve_purpose(value))
            self.assertIsInstance(cd.resolve_purpose(value), str)


class RuntimeOverlayPurposeTests(unittest.TestCase):
    """A purpose minted at runtime with no file edit (`telegram_topics.py add`) must resolve through the SAME `resolve_purpose`/`grounding_instruction` doors a tracked purpose
    does. Both already call `telegram_topics._load_topic_names()` fresh on every call rather than the
    frozen module-level `TOPIC_NAMES` snapshot — proven here by substituting `_load_topic_names`
    itself, the same seam `test_telegram_topics.py` uses for its own base-vs-overlay coverage, rather
    than writing into the real state dir `_load_topic_names`'s own default points at."""

    def test_resolve_purpose_finds_a_purpose_absent_from_the_frozen_snapshot(self):
        self.assertNotIn("garden", tt.TOPIC_NAMES)
        with mock.patch.object(tt, "_load_topic_names",
                               lambda *a, **kw: {**tt.TOPIC_NAMES, "garden": "Garden"}):
            self.assertEqual(cd.resolve_purpose("garden"), "garden")

    def test_grounding_instruction_names_a_purpose_absent_from_the_frozen_snapshot(self):
        with mock.patch.object(tt, "_load_topic_names",
                               lambda *a, **kw: {**tt.TOPIC_NAMES, "garden": "Garden"}):
            self.assertIn("garden", cd.grounding_instruction())

    def test_a_retired_runtime_purpose_falls_back_to_main_like_any_unknown_one(self):
        """`_load_topic_names` itself is what drops a retired row (`RuntimeOverlay` in
        `test_telegram_topics.py` proves that directly); this only proves `resolve_purpose` has no
        second, independent notion of "known" that a retirement could fail to reach."""
        with mock.patch.object(tt, "_load_topic_names", lambda *a, **kw: dict(tt.TOPIC_NAMES)):
            self.assertEqual(cd.resolve_purpose("garden"), tt.TOPIC_MAIN_CHAT)


class ClassifyOutcomeTests(unittest.TestCase):
    def test_never_declared_is_defaulted_main(self):
        self.assertEqual(cd.classify_outcome(None, tt.TOPIC_MAIN_CHAT, 0), "defaulted-main")
        self.assertEqual(cd.classify_outcome(None, tt.TOPIC_MAIN_CHAT, 1), "defaulted-main")

    def test_declared_and_exact_first_try(self):
        self.assertEqual(cd.classify_outcome("projects", "projects", 0), "declared-first-try")

    def test_declared_and_exact_after_retry(self):
        self.assertEqual(cd.classify_outcome("decisions", "decisions", 1), "declared-after-retry(1)")

    def test_declared_but_needed_fuzzy_matching(self):
        self.assertEqual(cd.classify_outcome("projcts", "projects", 0), "fuzzy-matched(projcts->projects)")


class RecordOutcomeTests(unittest.TestCase):
    def test_writes_one_row_with_the_expected_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            cd.record_outcome(tmp, turn_id="t1", channel="telegram", inbound_purpose=None,
                              declared_purpose="projects", resolved_purpose="projects",
                              retries_used=0, outcome="declared-first-try",
                              reply_marker_required=True, reply_marker_present=True,
                              reply_marker_retries_used=0)
            rows = list(_read_jsonl(os.path.join(tmp, cd.CHANNEL_DECLARE_LOG_FILENAME)))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["turn_id"], "t1")
        self.assertEqual(row["channel"], "telegram")
        self.assertIsNone(row["inbound_purpose"])
        self.assertEqual(row["declared_purpose"], "projects")
        self.assertEqual(row["resolved_purpose"], "projects")
        self.assertEqual(row["retries_used"], 0)
        self.assertEqual(row["outcome"], "declared-first-try")
        self.assertIn("at", row)
        self.assertEqual(row["schema"], "seneschal.channel-declare-log/2")
        self.assertIs(row["reply_marker_required"], True)
        self.assertIs(row["reply_marker_present"], True)
        self.assertEqual(row["reply_marker_retries_used"], 0)

    def test_never_raises_on_a_guaranteed_invalid_path(self):
        # An embedded NUL is invalid on every platform's filesystem calls — a real failure mode,
        # exercised directly rather than only through a mock. Never touches a real drive/directory.
        cd.record_outcome("bad\x00dir", turn_id=None, channel="telegram", inbound_purpose=None,
                          declared_purpose=None, resolved_purpose="main", retries_used=0,
                          outcome="defaulted-main", reply_marker_required=True,
                          reply_marker_present=False, reply_marker_retries_used=1)
        # No assertion beyond "did not raise" — that IS the contract.

    def test_never_raises_when_the_underlying_append_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("stateio.append_jsonl", side_effect=OSError("boom")):
                cd.record_outcome(tmp, turn_id="t1", channel="telegram", inbound_purpose=None,
                                  declared_purpose="main", resolved_purpose="main",
                                  retries_used=0, outcome="declared-first-try",
                                  reply_marker_required=True, reply_marker_present=True,
                                  reply_marker_retries_used=0)
            self.assertFalse(os.path.exists(os.path.join(tmp, cd.CHANNEL_DECLARE_LOG_FILENAME)))


class CurrentTopicTests(unittest.TestCase):
    def test_no_state_file_is_the_main_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(tt.purpose_for_thread(tmp, None))
            self.assertIsNone(tt.purpose_for_thread(tmp, 555))
            self.assertIn("main chat", cd.current_topic_line(tmp, None))

    def test_a_stored_thread_resolves_to_its_purpose(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = tt.load_state(tmp)
            state["topics"]["projects"] = {"message_thread_id": 42, "name": "Projects"}
            tt.save_state(tmp, state)
            self.assertEqual(tt.purpose_for_thread(tmp, 42), "projects")
            self.assertEqual(cd.current_purpose(tmp, 42), "projects")
            line = cd.current_topic_line(tmp, 42)
            self.assertIn("projects", line)
            self.assertIn("[[channel:...]]", line)

    def test_an_unmapped_thread_id_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = tt.load_state(tmp)
            state["topics"]["projects"] = {"message_thread_id": 42, "name": "Projects"}
            tt.save_state(tmp, state)
            self.assertIsNone(tt.purpose_for_thread(tmp, 999))

    def test_bool_and_none_thread_ids_are_none_never_a_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(tt.purpose_for_thread(tmp, None))
            self.assertIsNone(tt.purpose_for_thread(tmp, True))
            self.assertIsNone(tt.purpose_for_thread(tmp, False))

    def test_a_corrupt_state_file_fails_open_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(tt.state_path(tmp), "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            self.assertIsNone(tt.purpose_for_thread(tmp, 42))
            self.assertIn("main chat", cd.current_topic_line(tmp, 42))

    def test_current_purpose_never_raises(self):
        with mock.patch.object(tt, "load_state", side_effect=RuntimeError("boom")):
            self.assertIsNone(cd.current_purpose(tempfile.mkdtemp(), 42))


class GroundingInstructionTests(unittest.TestCase):
    def setUp(self):
        use_fixture_table(self)
        use_owner_name(self, None)

    def test_names_the_live_topics_and_main(self):
        text = cd.grounding_instruction()
        self.assertIn("[[channel:PURPOSE]]", text)
        for name in ("pull-requests", "decisions", "reminders", "projects"):
            self.assertIn(name, text)
        self.assertIn("`main`", text)

    def test_also_names_the_reply_marker_line(self):
        # reply-marker-forcing-function-spec.md §5: folded into the SAME instruction slot, no new
        # template placeholder.
        text = cd.grounding_instruction()
        self.assertIn("*(answering", text)
        self.assertIn("NOT STRIPPED", text)

    def test_unconfigured_owner_reads_as_the_owner(self):
        text = cd.grounding_instruction()
        self.assertIn("the owner", text)
        assert_no_gendered_pronoun(self, text)

    def test_a_configured_owner_name_is_used(self):
        use_owner_name(self, "Alex")
        text = cd.grounding_instruction()
        self.assertIn("reaches Alex verbatim", text)
        self.assertNotIn("the owner", text)


def _read_jsonl(path):
    import json
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


if __name__ == "__main__":
    unittest.main()
