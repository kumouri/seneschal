"""Tests for the cockpit's Trace read layer (`seneschal/docs/session-trace-spec.md` phase 1).

**Written against the on-disk shape, never by importing `seneschal/scripts/trace.py`** — the same posture
`test_jobs.py` takes, and for the same reason: it is what proves the cockpit's independence from the
daemon's commit. If these tests imported the writer they would pass forever while the deployed backend
broke against a state dir written by a daemon on a different revision.

The load-bearing families:

* **Tolerance** — a trace is opened precisely when something has gone wrong, which is exactly when its
  inputs are most likely damaged. Every degradation is "less data", never an exception.
* **The `!private` tombstone**, honoured in the READER. Conversation text appears in this panel,
  which makes the caution load-bearing: a redaction that holds in one reader and not the other is
  not a redaction. The test that matters asserts the words appear nowhere in the response's
  BYTES, not merely that a flag is set.
* **Attribution is asserted, never inferred from the clock.** The legacy-join test deliberately puts
  two different sessions' turns in the same minute.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# CI discovers this file with `unittest discover -s cockpit/server`, which imports it as a top-level
# module with no parent package — so a relative `from . import trace` raises ImportError there while
# passing locally under `python -m unittest cockpit.server.test_trace`. Every sibling test here uses
# this shim for the same reason; it is not decoration.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import trace  # noqa: E402


def _write(d: Path, name: str, rows) -> None:
    with open(d / name, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


class Tolerance(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_no_metrics_log_is_unavailable_not_a_crash(self):
        got = trace.read_sessions(self.d)
        self.assertFalse(got["available"])
        self.assertEqual(got["sessions"], [])

    def test_an_empty_but_present_log_is_available_with_nothing(self):
        """"Nothing ran" and "I can't see anything" must not look the same — the same distinction
        the Jobs panel makes, and the reason this panel can be trusted when it says a session is missing."""
        _write(self.d, trace.METRICS, [])
        got = trace.read_sessions(self.d)
        self.assertTrue(got["available"])
        self.assertEqual(got["sessions"], [])

    def test_a_corrupt_line_costs_one_row(self):
        _write(self.d, trace.METRICS, [
            {"session_id": "s1", "turn_id": "t1", "ts": "2026-08-06T01:00:00Z"},
            "{ not json at all",
            {"session_id": "s1", "turn_id": "t2", "ts": "2026-08-06T01:01:00Z"},
        ])
        self.assertEqual(trace.read_sessions(self.d)["sessions"][0]["turns"], 2)

    def test_hostile_field_types_do_not_raise(self):
        _write(self.d, trace.METRICS, [
            {"session_id": "s1", "cost_usd": "free", "context_tokens": True, "outcome": None},
            {"session_id": ["not", "a", "string"]},
            {"session_id": "s1", "cost_usd": True},
        ])
        e, = trace.read_sessions(self.d)["sessions"]
        self.assertEqual(e["turns"], 2)
        self.assertEqual(e["cost_usd"], 0.0, "a bool is not a cost and True must not become 1.0")
        self.assertEqual(e["context_peak"], 0, "a bool is not a token count")

    def test_an_empty_session_id_is_refused(self):
        self.assertFalse(trace.read_session(self.d, "")["available"])
        self.assertFalse(trace.read_session(self.d, "   ")["available"])
        self.assertFalse(trace.read_session(self.d, None)["available"])


class Tombstone(unittest.TestCase):
    """Conversation text in the panel makes this reader a second place a redaction has to hold."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        _write(self.d, trace.METRICS, [{"session_id": "s1", "turn_id": "t1", "ts": "2026-08-06T01:00:00Z"}])

    def test_a_private_turn_yields_no_text_and_no_preview(self):
        _write(self.d, trace.TURNS, [
            {"turn_id": "t1", "speaker": "owner", "redacted": True},
            {"turn_id": "t1", "speaker": "assistant", "redacted": True},
        ])
        _write(self.d, trace.TRANSCRIPT, [
            {"ts": "2026-08-06T01:00:01Z", "kind": "turn_done", "turn_id": "t1", "session_id": "s1",
             "reply_preview": "SECRETWORD should never surface"},
        ])
        got = trace.read_session(self.d, "s1")
        ev, = got["events"]
        self.assertTrue(ev["redacted"])
        self.assertNotIn("said", ev)
        self.assertNotIn("reply_preview", ev,
                         "the transcript preview must not backfill a redacted turn")
        self.assertNotIn("SECRETWORD", json.dumps(got),
                         "the words must appear nowhere in the response bytes")

    def test_one_redacted_side_redacts_the_whole_turn(self):
        # !private tombstones BOTH halves, but a reader that trusted only the row it happened to see
        # first would leak the other. Redaction is per-turn here, deliberately.
        _write(self.d, trace.TURNS, [
            {"turn_id": "t1", "speaker": "owner", "text": "LEAKME"},
            {"turn_id": "t1", "speaker": "assistant", "redacted": True},
        ])
        _write(self.d, trace.TRANSCRIPT,
               [{"ts": "x", "kind": "turn_done", "turn_id": "t1", "session_id": "s1"}])
        got = trace.read_session(self.d, "s1")
        self.assertNotIn("LEAKME", json.dumps(got))

    def test_an_ordinary_turn_still_carries_its_text(self):
        _write(self.d, trace.TURNS, [
            {"turn_id": "t1", "speaker": "owner", "text": "what's on today?"},
            {"turn_id": "t1", "speaker": "assistant", "text": "three things."},
        ])
        _write(self.d, trace.TRANSCRIPT,
               [{"ts": "x", "kind": "turn_done", "turn_id": "t1", "session_id": "s1"}])
        ev, = trace.read_session(self.d, "s1")["events"]
        self.assertEqual([s["text"] for s in ev["said"]], ["what's on today?", "three things."])


class TextAnchoring(unittest.TestCase):
    """A turn is ONE exchange but MANY transcript rows. Stapling the turn's whole text onto every
    one of them would repeat both sides once per row, burying the steps in duplicate bytes. Each
    side's words land on the row where they actually happened."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        _write(self.d, trace.METRICS, [{"session_id": "s1", "turn_id": "t1", "ts": "2026-08-12T01:00:00Z"}])
        _write(self.d, trace.TURNS, [
            {"turn_id": "t1", "speaker": "owner", "text": "OWNER-WORDS"},
            {"turn_id": "t1", "speaker": "assistant", "text": "ASSISTANT-WORDS"},
        ])

    def _busy_turn(self):
        """One turn, five rows — the shape that produced the repetition."""
        _write(self.d, trace.TRANSCRIPT, [
            {"ts": "2026-08-12T01:00:00Z", "kind": "turn_started", "turn_id": "t1", "session_id": "s1"},
            {"ts": "2026-08-12T01:00:01Z", "kind": "assistant_output", "turn_id": "t1",
             "session_id": "s1", "tool_uses": [{"name": "Bash"}]},
            {"ts": "2026-08-12T01:00:02Z", "kind": "assistant_output", "turn_id": "t1",
             "session_id": "s1", "tool_uses": [{"name": "Read"}]},
            {"ts": "2026-08-12T01:00:03Z", "kind": "assistant_output", "turn_id": "t1",
             "session_id": "s1", "tool_uses": [{"name": "Edit"}]},
            {"ts": "2026-08-12T01:00:04Z", "kind": "turn_done", "turn_id": "t1", "session_id": "s1"},
        ])
        return trace.read_session(self.d, "s1")["events"]

    def test_each_side_appears_exactly_once_across_the_whole_turn(self):
        """THE REGRESSION. Five rows must not mean five copies of both sides."""
        events = self._busy_turn()
        blob = json.dumps(events)
        self.assertEqual(blob.count("OWNER-WORDS"), 1)
        self.assertEqual(blob.count("ASSISTANT-WORDS"), 1)

    def test_the_owners_words_open_the_turn_and_the_assistants_close_it(self):
        events = self._busy_turn()
        self.assertEqual([s["text"] for s in events[0].get("said", [])], ["OWNER-WORDS"])
        self.assertEqual([s["text"] for s in events[-1].get("said", [])], ["ASSISTANT-WORDS"])

    def test_the_middle_rows_keep_their_tool_calls_and_carry_no_text(self):
        """What they are: steps. The text burying them is what made the log unreadable."""
        middle = self._busy_turn()[1:-1]
        self.assertEqual([e["tool_uses"][0]["name"] for e in middle], ["Bash", "Read", "Edit"])
        for e in middle:
            self.assertNotIn("said", e)

    def test_a_turn_with_no_boundary_rows_still_carries_both_sides(self):
        """THE FALLBACK, and the reason it exists: a truncated turn must not lose the owner's words. Head
        and tail stand in for the missing `turn_started` / `turn_done`."""
        _write(self.d, trace.TRANSCRIPT, [
            {"ts": "2026-08-12T01:00:01Z", "kind": "assistant_output", "turn_id": "t1", "session_id": "s1"},
            {"ts": "2026-08-12T01:00:02Z", "kind": "assistant_output", "turn_id": "t1", "session_id": "s1"},
        ])
        events = trace.read_session(self.d, "s1")["events"]
        self.assertEqual([s["text"] for s in events[0].get("said", [])], ["OWNER-WORDS"])
        self.assertEqual([s["text"] for s in events[-1].get("said", [])], ["ASSISTANT-WORDS"])

    def test_a_single_row_turn_carries_everything(self):
        """Head and tail are the same row. Nothing may fall between the two anchors."""
        _write(self.d, trace.TRANSCRIPT,
               [{"ts": "2026-08-12T01:00:01Z", "kind": "turn_done", "turn_id": "t1", "session_id": "s1"}])
        ev, = trace.read_session(self.d, "s1")["events"]
        self.assertEqual([s["text"] for s in ev["said"]], ["OWNER-WORDS", "ASSISTANT-WORDS"])

    def test_an_unrecognised_speaker_surfaces_rather_than_vanishing(self):
        """Anchoring splits the text two ways; a third speaker must not fall through the gap."""
        _write(self.d, trace.TURNS, [{"turn_id": "t1", "speaker": "archon", "text": "THIRD-VOICE"}])
        _write(self.d, trace.TRANSCRIPT, [
            {"ts": "2026-08-12T01:00:00Z", "kind": "turn_started", "turn_id": "t1", "session_id": "s1"},
            {"ts": "2026-08-12T01:00:04Z", "kind": "turn_done", "turn_id": "t1", "session_id": "s1"},
        ])
        self.assertIn("THIRD-VOICE", json.dumps(trace.read_session(self.d, "s1")["events"]))

    def test_speaker_slugs_are_owner_assistant_and_any_other_role_passes_through_raw(self):
        """The reader's vocabulary is `turns.jsonl`'s own: `owner` / `assistant`, never a personal
        name. Any other role is served under its raw slug, unrenamed, riding the turn's head."""
        _write(self.d, trace.TURNS, [
            {"turn_id": "t1", "speaker": "owner", "text": "OWNER-WORDS"},
            {"turn_id": "t1", "speaker": "assistant", "text": "ASSISTANT-WORDS"},
            {"turn_id": "t1", "speaker": "archon", "text": "THIRD-VOICE"},
        ])
        self.assertEqual((trace.SPEAKER_OWNER, trace.SPEAKER_ASSISTANT), ("owner", "assistant"))
        events = self._busy_turn()
        speakers = [s["speaker"] for e in events for s in e.get("said", [])]
        self.assertEqual(sorted(speakers), ["archon", "assistant", "owner"])
        self.assertEqual([s["speaker"] for s in events[0]["said"]], ["owner", "archon"])
        self.assertEqual([s["speaker"] for s in events[-1]["said"]], ["assistant"])

    def test_a_redacted_busy_turn_leaks_no_preview_from_ANY_row(self):
        """Anchoring moved where the MARKER renders. It must not have moved where SUPPRESSION
        applies — every row of a redacted turn stays preview-free, anchor or not."""
        _write(self.d, trace.TURNS, [
            {"turn_id": "t1", "speaker": "owner", "redacted": True},
            {"turn_id": "t1", "speaker": "assistant", "redacted": True},
        ])
        _write(self.d, trace.TRANSCRIPT, [
            {"ts": "2026-08-12T01:00:00Z", "kind": "turn_started", "turn_id": "t1", "session_id": "s1",
             "reply_preview": "SECRETWORD one"},
            {"ts": "2026-08-12T01:00:01Z", "kind": "assistant_output", "turn_id": "t1",
             "session_id": "s1", "reply_preview": "SECRETWORD two"},
            {"ts": "2026-08-12T01:00:04Z", "kind": "turn_done", "turn_id": "t1", "session_id": "s1",
             "reply_preview": "SECRETWORD three"},
        ])
        got = trace.read_session(self.d, "s1")
        self.assertNotIn("SECRETWORD", json.dumps(got),
                         "a middle row is not an anchor, but it is still part of a redacted turn")
        self.assertTrue(got["events"][0]["redacted"])
        self.assertTrue(got["events"][-1]["redacted"])


class Attribution(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        _write(self.d, trace.METRICS, [
            {"session_id": "s1", "turn_id": "t1", "ts": "2026-08-06T01:00:00Z"},
            {"session_id": "s2", "turn_id": "t9", "ts": "2026-08-06T01:00:00Z"},
        ])

    def test_legacy_rows_join_through_metrics_not_the_clock(self):
        """Transcript rows written before the session key existed carry `turn_id` and no `session_id`. They are
        attributed because a metrics row SAYS which session that turn belonged to. Both turns here
        share the same minute, so a proximity join would get it wrong."""
        _write(self.d, trace.TRANSCRIPT, [
            {"ts": "2026-08-06T01:00:01Z", "kind": "assistant_output", "turn_id": "t1"},
            {"ts": "2026-08-06T01:00:02Z", "kind": "assistant_output", "turn_id": "t9"},
        ])
        got = trace.read_session(self.d, "s1")
        self.assertEqual([e["turn_id"] for e in got["events"]], ["t1"])

    def test_new_rows_join_directly(self):
        _write(self.d, trace.TRANSCRIPT, [
            {"ts": "a", "kind": "turn_started", "session_id": "s1"},
            {"ts": "b", "kind": "turn_started", "session_id": "s2"},
        ])
        self.assertEqual(len(trace.read_session(self.d, "s1")["events"]), 1)

    def test_events_sort_across_sources_and_count_tools(self):
        _write(self.d, trace.TRANSCRIPT, [
            {"ts": "2026-08-06T01:00:05Z", "kind": "assistant_output", "session_id": "s1",
             "tool_uses": [{"name": "Bash"}, {"name": "Read"}]},
        ])
        _write(self.d, trace.ASSERTIONS,
               [{"at": "2026-08-06T01:00:06Z", "kind": "reply", "session_id": "s1"}])
        _write(self.d, trace.SESSION_STARTS, [
            {"at": "2026-08-06T00:59:00Z", "kind": "decision", "class": "no_record", "why": "cold"},
            {"at": "2026-08-06T00:59:01Z", "kind": "opened", "session_id": "s1"},
        ])
        got = trace.read_session(self.d, "s1")
        self.assertEqual([e["src"] for e in got["events"]], ["spawn", "transcript", "assertion"])
        self.assertEqual(got["tool_calls"], 2)


class SpawnPairing(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        _write(self.d, trace.METRICS, [{"session_id": "s1", "turn_id": "t"},
                                       {"session_id": "s2", "turn_id": "u"}])

    def test_a_dead_spawn_does_not_steal_the_next_sessions_id(self):
        """The case that decides whether this is a join or a fiction."""
        _write(self.d, trace.SESSION_STARTS, [
            {"at": "t0", "kind": "decision", "class": "resumed", "resumed": True},
            {"at": "t2", "kind": "decision", "class": "too_old", "resumed": False},
            {"at": "t3", "kind": "opened", "session_id": "s1"},
        ])
        s1 = next(e for e in trace.read_sessions(self.d)["sessions"] if e["session_id"] == "s1")
        self.assertEqual(s1["start_class"], "too_old", "the SECOND decision is s1's, not the first")

    def test_a_session_with_no_decision_on_file_says_so(self):
        s2 = next(e for e in trace.read_sessions(self.d)["sessions"] if e["session_id"] == "s2")
        self.assertIsNone(s2["resumed"], "unknown must stay unknown, never default to False")

    def test_legacy_rows_without_a_kind_read_as_decisions(self):
        _write(self.d, trace.SESSION_STARTS, [
            {"at": "t0", "class": "context_too_full", "resumed": False},
            {"at": "t1", "kind": "opened", "session_id": "s1"},
        ])
        s1 = next(e for e in trace.read_sessions(self.d)["sessions"] if e["session_id"] == "s1")
        self.assertEqual(s1["start_class"], "context_too_full")


class SessionsList(unittest.TestCase):
    def test_a_session_that_rolled_out_of_the_transcript_still_lists(self):
        """Built from metrics, NOT the transcript ring — a session that aged out of a capped buffer
        still happened, and must not silently cease to have existed."""
        d = Path(tempfile.mkdtemp())
        _write(d, trace.METRICS, [{"session_id": "gone", "turn_id": "t", "ts": "2026-08-06T01:00:00Z"}])
        got = trace.read_sessions(d)
        self.assertEqual(got["sessions"][0]["session_id"], "gone")

    def test_newest_first_and_limit_applies(self):
        d = Path(tempfile.mkdtemp())
        _write(d, trace.METRICS, [{"session_id": f"s{i}", "turn_id": "t",
                                   "ts": f"2026-08-06T0{i}:00:00Z"} for i in range(1, 6)])
        got = trace.read_sessions(d, limit=2)
        self.assertEqual([e["session_id"] for e in got["sessions"]], ["s5", "s4"])
        self.assertEqual(got["total"], 5, "total reports the whole set, not the page")


class SessionTitle(unittest.TestCase):
    """A session-id hash truncated to 8 hex characters names nothing a human recognises. The first
    thing the owner said in the session stands in for it instead."""

    def test_the_first_turns_first_owner_line_becomes_the_title(self):
        d = Path(tempfile.mkdtemp())
        _write(d, trace.METRICS, [
            {"session_id": "s1", "turn_id": "t1", "ts": "2026-08-06T01:00:00Z"},
            {"session_id": "s1", "turn_id": "t2", "ts": "2026-08-06T01:05:00Z"},
        ])
        _write(d, trace.TURNS, [
            {"turn_id": "t1", "speaker": "owner", "text": "what's on today?"},
            {"turn_id": "t1", "speaker": "assistant", "text": "three things."},
            {"turn_id": "t2", "speaker": "owner", "text": "a later question"},
        ])
        s1, = trace.read_sessions(d)["sessions"]
        self.assertEqual(s1["title"], "what's on today?")

    def test_a_redacted_first_turn_yields_no_title(self):
        """The tombstone must hold here too — a title is the one field read before opening a
        session, so it is the last place a `!private` turn should leak by accident."""
        d = Path(tempfile.mkdtemp())
        _write(d, trace.METRICS, [{"session_id": "s1", "turn_id": "t1", "ts": "2026-08-06T01:00:00Z"}])
        _write(d, trace.TURNS, [
            {"turn_id": "t1", "speaker": "owner", "redacted": True},
            {"turn_id": "t1", "speaker": "assistant", "redacted": True},
        ])
        s1, = trace.read_sessions(d)["sessions"]
        self.assertIsNone(s1["title"])

    def test_no_turns_jsonl_yields_no_title_not_a_crash(self):
        d = Path(tempfile.mkdtemp())
        _write(d, trace.METRICS, [{"session_id": "s1", "turn_id": "t1", "ts": "2026-08-06T01:00:00Z"}])
        s1, = trace.read_sessions(d)["sessions"]
        self.assertIsNone(s1["title"])

    def test_a_turn_id_with_no_owner_line_yields_no_title(self):
        d = Path(tempfile.mkdtemp())
        _write(d, trace.METRICS, [{"session_id": "s1", "turn_id": "t1", "ts": "2026-08-06T01:00:00Z"}])
        _write(d, trace.TURNS, [{"turn_id": "t1", "speaker": "assistant", "text": "only the assistant spoke"}])
        s1, = trace.read_sessions(d)["sessions"]
        self.assertIsNone(s1["title"])


class ErrorCount(unittest.TestCase):
    """The daemon writes `outcome` CAPITALISED. A lowercase-only comparison would score every
    successful turn as an error, burying the real failures and rendering a quiet session as a red
    error count."""

    def _errors(self, outcomes):
        d = Path(tempfile.mkdtemp())
        _write(d, trace.METRICS, [{"session_id": "s1", "turn_id": f"t{i}", "outcome": o}
                                  for i, o in enumerate(outcomes)])
        return trace.read_sessions(d)["sessions"][0]["errors"]

    def test_the_daemons_own_vocabulary_counts_correctly(self):
        """The regression, in the exact spelling `metrics.jsonl` actually contains."""
        self.assertEqual(self._errors(["Success"] * 5), 0)
        self.assertEqual(self._errors(["Failed"] * 3), 3)
        self.assertEqual(self._errors(["Success", "Failed", "Success"]), 1)

    def test_case_and_whitespace_do_not_decide_whether_a_turn_failed(self):
        self.assertEqual(self._errors(["ok", "OK", "success", "SUCCESS", " Success "]), 0)

    def test_a_missing_outcome_is_not_an_error(self):
        """Most rows predate the field; absence is silence, not failure."""
        self.assertEqual(self._errors([None, None]), 0)

    def test_an_unrecognised_outcome_still_counts(self):
        """An alarm count errs toward surfacing something unfamiliar, never toward swallowing it —
        including a non-string, which is a shape this reader cannot vouch for."""
        self.assertEqual(self._errors(["timed_out"]), 1)
        self.assertEqual(self._errors([{"not": "a string"}]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
