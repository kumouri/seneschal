#!/usr/bin/env python3
"""Tests for the session trace reader — session-trace-spec.md phase 0.

Two families, and the second is the load-bearing one.

**The reader's tolerance**, because a trace is consulted precisely when something has gone wrong,
which is exactly when its inputs are most likely to be damaged. A reader that raises on a
half-written line is useless in the only situation it exists for.

**The joins are exact, never proximity.** Removing timestamp-proximity joins is the entire point of
this spec (§2.1a), so every test here asserts that a pairing comes from a row that *says so* — and the
`decision → opened` cases include the one that must NOT pair (a spawn that never came up), because a
join that silently absorbs a failure is worse than no join.

Stdlib unittest only. Run:  python -m unittest test_trace
"""
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import trace as tr  # noqa: E402


def _write(d: str, name: str, rows) -> None:
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


class Tolerance(unittest.TestCase):
    """Less data, never an exception."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_missing_files_are_empty_not_fatal(self):
        self.assertEqual(tr.sessions(self.d), [])
        self.assertEqual(tr.session_events(self.d, "s1"), [])
        self.assertEqual(tr.spawn_decisions(self.d), [])

    def test_a_corrupt_line_costs_one_row(self):
        _write(self.d, tr.METRICS, [
            {"session_id": "s1", "turn_id": "t1", "ts": "2026-08-06T01:00:00Z"},
            "{ this is not json",
            {"session_id": "s1", "turn_id": "t2", "ts": "2026-08-06T01:01:00Z"},
        ])
        got, = tr.sessions(self.d)
        self.assertEqual(got["turns"], 2, "the bad line must not eat the rows after it")

    def test_a_directory_where_a_log_should_be(self):
        os.makedirs(os.path.join(self.d, tr.METRICS))
        self.assertEqual(tr.sessions(self.d), [])

    def test_non_dict_json_lines_are_skipped(self):
        _write(self.d, tr.SESSION_STARTS, ["[1,2,3]", '"a string"', "null",
                                           json.dumps({"kind": "decision", "class": "too_old"})])
        self.assertEqual(len(tr.spawn_decisions(self.d)), 1)

    def test_a_session_id_that_is_not_a_string_is_ignored(self):
        _write(self.d, tr.METRICS, [{"session_id": 7, "turn_id": "t"}, {"session_id": "", "turn_id": "t"}])
        self.assertEqual(tr.sessions(self.d), [])


class DecisionOpenedPairing(unittest.TestCase):
    """The pairing that replaces a timestamp heuristic. Exact, or it is nothing."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_a_decision_pairs_with_the_next_opened(self):
        _write(self.d, tr.SESSION_STARTS, [
            {"at": "t0", "kind": "decision", "class": "too_old", "resumed": False},
            {"at": "t1", "kind": "opened", "session_id": "sess-A"},
        ])
        d, = tr.spawn_decisions(self.d)
        self.assertEqual(d["session_id"], "sess-A")
        self.assertEqual(d["class"], "too_old")
        self.assertEqual(d["opened_at"], "t1")

    def test_a_spawn_that_never_opened_is_kept_unpaired(self):
        """The case that decides whether this is a join or a fiction: a decision with no open is a
        session that never came up. It must survive with session_id None — absorbing it into the NEXT
        session's open would attribute one spawn's refusal to a different session entirely."""
        _write(self.d, tr.SESSION_STARTS, [
            {"at": "t0", "kind": "decision", "class": "resumed", "resumed": True},
            {"at": "t2", "kind": "decision", "class": "no_record", "resumed": False},
            {"at": "t3", "kind": "opened", "session_id": "sess-B"},
        ])
        first, second = tr.spawn_decisions(self.d)
        self.assertIsNone(first["session_id"], "the dead spawn must not steal the next session's id")
        self.assertEqual(first["class"], "resumed")
        self.assertEqual(second["session_id"], "sess-B")

    def test_a_trailing_decision_is_kept(self):
        _write(self.d, tr.SESSION_STARTS, [{"at": "t0", "kind": "decision", "class": "no_record"}])
        d, = tr.spawn_decisions(self.d)
        self.assertIsNone(d["session_id"])

    def test_legacy_rows_without_a_kind_read_as_decisions(self):
        # Rows written before `kind` existed. They are decisions and must stay so.
        _write(self.d, tr.SESSION_STARTS, [
            {"at": "t0", "class": "context_too_full", "resumed": False, "why": "full"},
            {"at": "t1", "kind": "opened", "session_id": "sess-C"},
        ])
        d, = tr.spawn_decisions(self.d)
        self.assertEqual(d["class"], "context_too_full")
        self.assertEqual(d["session_id"], "sess-C")

    def test_a_stray_opened_with_no_decision_is_dropped(self):
        _write(self.d, tr.SESSION_STARTS, [{"at": "t1", "kind": "opened", "session_id": "sess-D"}])
        self.assertEqual(tr.spawn_decisions(self.d), [])


class SessionsList(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        _write(self.d, tr.METRICS, [
            {"session_id": "s1", "turn_id": "t1", "ts": "2026-08-06T01:00:00Z",
             "cost_usd": 1.5, "context_tokens": 100_000, "model": "claude-opus-5", "source": "telegram"},
            {"session_id": "s1", "turn_id": "t2", "ts": "2026-08-06T01:05:00Z",
             "cost_usd": 2.25, "context_tokens": 180_000, "outcome": "error", "source": "cockpit"},
            {"session_id": "s2", "turn_id": "t3", "ts": "2026-08-06T02:00:00Z", "cost_usd": 0.5},
        ])

    def test_aggregates_per_session(self):
        s1 = next(e for e in tr.sessions(self.d) if e["session_id"] == "s1")
        self.assertEqual(s1["turns"], 2)
        self.assertEqual(s1["cost_usd"], 3.75)
        self.assertEqual(s1["context_peak"], 180_000)
        self.assertEqual(s1["errors"], 1)
        self.assertEqual(s1["sources"], ["cockpit", "telegram"])

    def test_newest_first(self):
        self.assertEqual([e["session_id"] for e in tr.sessions(self.d)], ["s2", "s1"])

    def test_enriched_by_the_spawn_decision(self):
        _write(self.d, tr.SESSION_STARTS, [
            {"at": "t0", "kind": "decision", "class": "too_old", "resumed": False, "why": "3h old"},
            {"at": "t1", "kind": "opened", "session_id": "s1"},
        ])
        s1 = next(e for e in tr.sessions(self.d) if e["session_id"] == "s1")
        self.assertEqual(s1["start_class"], "too_old")
        self.assertFalse(s1["resumed"])

    def test_a_session_that_rolled_out_of_the_transcript_still_lists(self):
        """Built from metrics, NOT the transcript ring — a session that aged out of a capped buffer
        still happened, and must not silently cease to have existed."""
        self.assertEqual(len(tr.sessions(self.d)), 2)  # no transcript file at all


class SessionEvents(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        _write(self.d, tr.METRICS, [
            {"session_id": "s1", "turn_id": "t1", "ts": "2026-08-06T01:00:00Z"},
            {"session_id": "s2", "turn_id": "t9", "ts": "2026-08-06T01:00:00Z"},
        ])

    def test_new_rows_join_on_session_id_directly(self):
        _write(self.d, tr.TRANSCRIPT, [
            {"ts": "2026-08-06T01:00:01Z", "kind": "turn_started", "turn_id": "t1", "session_id": "s1"},
            {"ts": "2026-08-06T01:00:02Z", "kind": "turn_started", "turn_id": "t9", "session_id": "s2"},
        ])
        got = tr.session_events(self.d, "s1")
        self.assertEqual([e["turn_id"] for e in got], ["t1"])

    def test_legacy_rows_join_through_metrics_not_through_the_clock(self):
        """Older transcript rows carry `turn_id` and no `session_id`. They are
        attributed because metrics.jsonl says which session that turn belonged to — a row asserting
        the link — never because two timestamps were close."""
        _write(self.d, tr.TRANSCRIPT, [
            {"ts": "2026-08-06T01:00:01Z", "kind": "assistant_output", "turn_id": "t1"},
            {"ts": "2026-08-06T01:00:03Z", "kind": "assistant_output", "turn_id": "t9"},
        ])
        got = tr.session_events(self.d, "s1")
        self.assertEqual([e["turn_id"] for e in got], ["t1"],
                         "t9 is s2's turn and shares the minute — proximity must not attribute it")

    def test_events_are_chronological_across_sources(self):
        _write(self.d, tr.TRANSCRIPT,
               [{"ts": "2026-08-06T01:00:05Z", "kind": "turn_done", "session_id": "s1"}])
        _write(self.d, tr.ASSERTIONS,
               [{"at": "2026-08-06T01:00:06Z", "kind": "reply", "session_id": "s1"}])
        _write(self.d, tr.SESSION_STARTS, [
            {"at": "2026-08-06T00:59:00Z", "kind": "decision", "class": "no_record", "why": "cold"},
            {"at": "2026-08-06T00:59:01Z", "kind": "opened", "session_id": "s1"},
        ])
        got = tr.session_events(self.d, "s1")
        self.assertEqual([e["src"] for e in got], ["spawn", "transcript", "assertion"])

    def test_an_unknown_session_is_empty_not_everything(self):
        _write(self.d, tr.TRANSCRIPT, [{"ts": "x", "kind": "turn_started", "turn_id": "t1"}])
        self.assertEqual(tr.session_events(self.d, "nope"), [])
        self.assertEqual(tr.session_events(self.d, ""), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
