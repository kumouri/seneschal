#!/usr/bin/env python3
"""Tests for the Mouth (`mouth.py`, `seneschal/docs/mouth-spec.md` §7).

What is covered here is `mouth.py` itself — the record shape, the turn correlator and derived scope,
the read/tail/prune helpers, the CLI, the phase-1 queue and drain, and above all the fail-open
contract: `record_assertion` must never raise, whatever it is handed and whatever the disk does.

Producer coverage — that each send site actually writes a row on a landed send and nothing on a failed
one — belongs with the producers (`presence.deliver_reply`, `sentinel`'s reminder delivery, the
break-glass supervisor) and lands with the wave that wires them in.

No network, no real sends, no subprocesses: every sender is a local lambda.

Run:  python -m unittest test_mouth   (from seneschal/scripts)
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import mouth  # noqa: E402
import tz_common  # noqa: E402

NOW = datetime(2026, 7, 31, 18, 0, 0, tzinfo=timezone.utc)


def _rows(state_dir):
    return mouth.read_assertions(state_dir)


# --------------------------------------------------------------------------- 1. the writer


class RecordShape(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_writes_the_documented_record(self):
        self.assertTrue(mouth.record_assertion(self.dir, surface="telegram", kind="reminder",
                                               text="⏰ Reminder: stretch", speaker="sentinel",
                                               now=NOW))
        rows = _rows(self.dir)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["schema"], "seneschal.assertion/1")
        self.assertEqual(row["at"], "2026-07-31T18:00:00Z")
        self.assertEqual(row["surface"], "telegram")
        self.assertEqual(row["kind"], "reminder")
        self.assertEqual(row["text"], "⏰ Reminder: stretch")
        self.assertTrue(row["delivered"])
        self.assertEqual(row["item_ids"], [])
        self.assertEqual(row["superseded"], [])
        self.assertEqual(row["origin_hand"].get("speaker"), "sentinel")

    def test_door_is_direct_in_phase_0(self):
        """There is no queue yet, so every phase-0 row is Door B/direct. The field is written anyway
        so a phase-1 reader never has to special-case rows written before the queue existed."""
        mouth.record_assertion(self.dir, surface="telegram", kind="job", text="done")
        self.assertEqual(_rows(self.dir)[0]["door"], "direct")

    def test_append_only(self):
        for i in range(5):
            mouth.record_assertion(self.dir, surface="discord", kind="alert", text=f"n{i}")
        self.assertEqual([r["text"] for r in _rows(self.dir)], ["n0", "n1", "n2", "n3", "n4"])

    def test_not_delivered_is_recordable(self):
        """Nothing vanishes unrecorded (spec §3.4 rule 2) — the field exists from phase 0 even though
        no phase-0 producer writes it."""
        mouth.record_assertion(self.dir, surface="telegram", kind="job", text="x",
                               delivered=False, reason="expired")
        row = _rows(self.dir)[0]
        self.assertFalse(row["delivered"])
        self.assertEqual(row["reason"], "expired")

    def test_unknown_surface_is_recorded_not_rejected(self):
        """A producer that speaks must never be blocked by this module's opinion about labels."""
        self.assertTrue(mouth.record_assertion(self.dir, surface="carrier-pigeon", kind="???",
                                               text="hi"))
        self.assertEqual(_rows(self.dir)[0]["surface"], "carrier-pigeon")


# --------------------------------------------------- 1b. the turn correlator and the derived scope
#
# Instrumentation only: these tests assert the row's SHAPE and the fail-open contract, and there is
# deliberately nothing here about anything the owner ever sees.


def _stream_tool_use(*calls) -> dict:
    """One raw claude-CLI `assistant` stream event carrying tool_use blocks — the shape TurnScope is
    fed, above cockpit_pipe.build_chat_event_from_stream (which keeps only a 200-char preview)."""
    blocks = [{"type": "tool_use", "name": name, "input": tool_input} for name, tool_input in calls]
    return {"type": "assistant", "message": {"content": blocks}}


class TurnCorrelator(unittest.TestCase):
    """`turn_id` — the join between what the assistant said and the turn that said it. Optional in
    the strong sense: most producers are not in a turn."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_a_row_with_a_turn_id_round_trips(self):
        self.assertTrue(mouth.record_assertion(self.dir, surface="telegram", kind="reply",
                                               text="on it", turn_id="a1b2c3d4e5f6"))
        self.assertEqual(_rows(self.dir)[0]["turn_id"], "a1b2c3d4e5f6")

    def test_a_row_without_one_round_trips_and_carries_no_key(self):
        """A reminder nudge, a job push, an archon ping and both bypass mouths are not chat turns.
        Absence is the honest reading, so the key is omitted rather than written null."""
        self.assertTrue(mouth.record_assertion(self.dir, surface="telegram", kind="reminder",
                                               text="⏰ stretch"))
        row = _rows(self.dir)[0]
        self.assertNotIn("turn_id", row)
        self.assertNotIn("scope", row)

    def test_both_shapes_validate_as_the_same_schema(self):
        mouth.record_assertion(self.dir, surface="telegram", kind="reply", text="with",
                               turn_id="t1", scope={"tool_calls": 1, "tools": ["Read"]})
        mouth.record_assertion(self.dir, surface="telegram", kind="reminder", text="without")
        rows = _rows(self.dir)
        self.assertEqual([r["schema"] for r in rows], [mouth.SCHEMA, mouth.SCHEMA])
        for row in rows:  # every phase-0 key still present on both, unchanged
            for key in ("at", "surface", "door", "kind", "origin_hand", "text", "item_ids",
                        "superseded", "delivered"):
                self.assertIn(key, row)

    def test_a_consumer_ignorant_of_the_new_fields_still_reads_the_row(self):
        """`mouth-spec.md` §7.0.1: vocabularies are validated loosely and a reader that has never
        heard of a field ignores it. This is the whole basis for calling phase 0 unbreakable."""
        mouth.record_assertion(self.dir, surface="telegram", kind="reply", text="hello",
                               turn_id="t1", scope=mouth.TurnScope())
        row = _rows(self.dir)[0]
        # The pre-phase-0 reader: project the documented §3.3 keys and carry on.
        old_reader = {k: row[k] for k in ("schema", "at", "surface", "door", "kind", "origin_hand",
                                          "text", "item_ids", "superseded", "delivered")}
        self.assertEqual(old_reader["text"], "hello")
        self.assertTrue(old_reader["delivered"])
        # And prune, the one thing that rewrites this file, is indifferent to them.
        self.assertEqual(mouth.prune(self.dir, days=30, now=NOW + timedelta(days=1)), 0)
        self.assertEqual(_rows(self.dir)[0]["turn_id"], "t1")

    def test_a_malformed_turn_id_costs_the_field_and_nothing_else(self):
        """Never an error, never a warning, and never the row. A non-string id writes nothing rather
        than being coerced: a garbled correlator that still LOOKS like one is worse than absence."""
        for bad in (17, object(), b"bytes", [], {}, "", "   ", None):
            self.assertTrue(mouth.record_assertion(self.dir, surface="telegram", kind="reply",
                                                   text="still said", turn_id=bad))
        rows = _rows(self.dir)
        self.assertEqual(len(rows), 8)
        self.assertEqual([r["text"] for r in rows], ["still said"] * 8)
        for row in rows:
            self.assertNotIn("turn_id", row)


class DerivedScope(unittest.TestCase):
    """The `scope` sub-object — DERIVED from the turn's own tool calls, never interpretive. A scope
    the assistant merely *claims* would be the same defect one level up; only a measured one is a
    measurement."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_the_shape_is_exactly_the_derived_keys(self):
        """Asserted as an exact key set so a later edit cannot quietly add a judgment — a confidence,
        a sufficiency verdict, a 'looked enough' flag — to a field whose whole value is that it makes
        no claim (no confidence score, no calibration number, no tool RESULTS).

        **`reads_complete` is not that kind of flag and the difference is the point.** It is not
        "did the assistant look enough" — a judgement nothing here is entitled to make — it is "is
        this LIST the whole list", which is a fact about the recorder, derived from a count it kept.
        It and `unnamed_calls` are both computed; neither is an opinion."""
        scope = mouth.TurnScope()
        scope.observe(_stream_tool_use(("Read", {"file_path": "seneschal/scripts/mouth.py"}),
                                       ("Grep", {"pattern": "turn_id", "path": "seneschal/scripts"})))
        mouth.record_assertion(self.dir, surface="telegram", kind="reply", text="x",
                               turn_id="t1", scope=scope)
        recorded = _rows(self.dir)[0]["scope"]
        self.assertEqual(set(recorded), {"tool_calls", "tools", "reads", "truncated",
                                         "unnamed_calls", "reads_complete"})
        self.assertEqual(recorded["tool_calls"], 2)
        self.assertEqual(recorded["tools"], ["Read", "Grep"])
        # The read TARGETS, verbatim. `pattern` is not a place, so it is not a target.
        self.assertEqual(recorded["reads"], ["seneschal/scripts/mouth.py", "seneschal/scripts"])
        self.assertFalse(recorded["truncated"])

    def test_tool_calls_counts_every_call_while_tools_is_distinct(self):
        """Whether an error correlates with FEW tool calls or with NARROW ones is a question the
        distinct-name list alone cannot answer: twelve Reads is one name."""
        scope = mouth.TurnScope()
        for i in range(12):
            scope.observe(_stream_tool_use(("Read", {"file_path": f"f{i}.py"})))
        snap = scope.snapshot()
        self.assertEqual(snap["tool_calls"], 12)
        self.assertEqual(snap["tools"], ["Read"])
        self.assertEqual(len(snap["reads"]), 12)

    def test_a_turn_that_called_nothing_records_a_measured_zero(self):
        """`tool_calls: 0` and an ABSENT scope are different statements — 'it looked at nothing' vs
        'we did not observe this turn'. Conflating them loses the first count entirely."""
        snap = mouth.TurnScope().snapshot()
        self.assertEqual(snap, {"tool_calls": 0, "tools": [], "reads": [], "truncated": False,
                                "unnamed_calls": 0, "reads_complete": True})
        # An empty `reads` on a turn that made no calls IS complete: nothing was looked at, and the
        # list correctly holds nothing. That is the one empty `reads` that was never a lie.
        mouth.record_assertion(self.dir, surface="telegram", kind="reply", text="off the top of my head",
                               turn_id="t1", scope=snap)
        self.assertEqual(_rows(self.dir)[0]["scope"]["tool_calls"], 0)

    def test_only_location_keys_become_read_targets(self):
        """SCOPE_READ_TARGET_KEYS is the one list, and it holds places. A command, a prompt or a body
        is not a place, is often enormous, and reading intent out of one is the interpretation this
        field exists to refuse."""
        scope = mouth.TurnScope()
        scope.observe(_stream_tool_use(
            ("Bash", {"command": "rm -rf /", "description": "definitely fine"}),
            ("WebFetch", {"url": "https://example.com/x", "prompt": "summarise"}),
            ("NotebookEdit", {"notebook_path": "nb.ipynb", "new_source": "print(1)"})))
        snap = scope.snapshot()
        self.assertEqual(snap["tool_calls"], 3)
        self.assertEqual(snap["reads"], ["https://example.com/x", "nb.ipynb"])
        self.assertNotIn("rm -rf /", " ".join(snap["reads"]))
        # The `Bash` call named no place, so it is counted rather than parsed — and the list stops
        # claiming to be the whole look. `truncated: false` with `Bash` invisible would be the
        # defect, not a smaller answer to it.
        self.assertEqual(snap["unnamed_calls"], 1)
        self.assertFalse(snap["reads_complete"])
        self.assertFalse(snap["truncated"], "a cap was not hit; `truncated` still means only that")

    def test_the_raw_input_is_never_retained(self):
        """A `Write` input holds a whole file. The accumulator keeps what it extracted, never what it
        extracted it from, so its size is bounded by the caps regardless of the turn."""
        scope = mouth.TurnScope()
        scope.observe(_stream_tool_use(("Write", {"file_path": "big.md", "content": "x" * 500_000})))
        blob = json.dumps(scope.snapshot())
        self.assertLess(len(blob), 1000)
        self.assertEqual(scope.snapshot()["reads"], ["big.md"])

    def test_a_cap_is_flagged_never_silent(self):
        """A truncated list read as a complete one is a wrong answer, not a smaller one."""
        scope = mouth.TurnScope()
        for i in range(mouth.SCOPE_MAX_READS + 5):
            scope.observe(_stream_tool_use(("Read", {"file_path": f"f{i}.py"})))
        snap = scope.snapshot()
        self.assertTrue(snap["truncated"])
        self.assertEqual(len(snap["reads"]), mouth.SCOPE_MAX_READS)
        self.assertEqual(snap["tool_calls"], mouth.SCOPE_MAX_READS + 5, "the COUNT is never capped")

    def test_an_over_long_target_is_bounded(self):
        scope = mouth.TurnScope()
        scope.observe(_stream_tool_use(("Read", {"file_path": "/" + "d/" * 5000})))
        self.assertEqual(len(scope.snapshot()["reads"][0]), mouth.SCOPE_TARGET_MAX_LEN)

    def test_unusable_scope_input_costs_the_field_and_never_the_row(self):
        """'A scope that cannot be computed' is normal — a missing transcript, an exotic stream, a
        caller handing over junk. All of it costs the field; none of it costs the message."""
        class Exploding:
            def snapshot(self):
                raise RuntimeError("boom")

        for bad in ("a string", 42, [1, 2], object(), Exploding(),
                    {"tool_calls": "not a number", "tools": "not a list"}):
            self.assertTrue(mouth.record_assertion(self.dir, surface="telegram", kind="reply",
                                                   text="still said", scope=bad))
        rows = _rows(self.dir)
        self.assertEqual(len(rows), 6)
        self.assertEqual([r["text"] for r in rows], ["still said"] * 6)
        # The two dict-ish ones normalise to a clean object; the rest write no field at all.
        scoped = [r for r in rows if "scope" in r]
        self.assertEqual(len(scoped), 1)
        # No `unnamed_calls` in the input, so no `unnamed_calls` and no `reads_complete` out:
        # ABSENT means "not measured", and is never rendered as "complete". See `normalise_scope`.
        self.assertEqual(scoped[0]["scope"], {"tool_calls": 0, "tools": [], "reads": [],
                                              "truncated": False})

    def test_observe_never_raises_on_any_stream_shape(self):
        """A malformed stream event costs whatever it would have contributed and nothing else — this
        runs on the warm session's stdout-reader thread, mid-turn."""
        scope = mouth.TurnScope()
        for ev in (None, "text", 42, [], {}, {"type": "assistant"},
                   {"type": "assistant", "message": None},
                   {"type": "assistant", "message": {"content": "nope"}},
                   {"type": "assistant", "message": {"content": [None, 7, {"type": "text"}]}},
                   {"type": "result", "usage": {}},
                   _stream_tool_use((None, None)),
                   {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}):
            scope.observe(ev)
        snap = scope.snapshot()
        self.assertEqual(snap["tool_calls"], 2, "the two nameless tool_use blocks still COUNT")
        self.assertEqual(snap["tools"], [])
        self.assertEqual(snap["reads"], [])

    def test_normalise_drops_unknown_keys_rather_than_passing_them_through(self):
        clean = mouth.normalise_scope({"tool_calls": 3, "tools": ["Read", "Read", " Grep "],
                                       "reads": ["a.py", "", "  ", "a.py"],
                                       "confidence": 0.9, "sufficient": True})
        self.assertEqual(clean, {"tool_calls": 3, "tools": ["Read", "Grep"], "reads": ["a.py"],
                                 "truncated": False})

    def test_normalise_of_nothing_is_nothing(self):
        self.assertIsNone(mouth.normalise_scope(None))
        self.assertIsNone(mouth.normalise_scope("not a scope"))
        self.assertEqual(mouth.normalise_scope({})["tool_calls"], 0)


class ScopeReadsCompleteness(unittest.TestCase):
    """`unnamed_calls` / `reads_complete` — whether `reads` is the whole set of places a turn touched.

    The defect these guard against: a target-key list that misses the Notion argument shapes, plus a
    `Bash` call that names no place, leaves `reads` empty on turns that did look at things — while
    `truncated: false` asserts the emptiness is complete.

    The design has two halves and needs both. **Widening the harvest** stops the Notion looks going
    unrecorded; **counting the calls that named nothing** stops the remainder lying about it. Neither
    half alone is honest: a wider list still can't name a `Bash` command, and a count with the old
    narrow list would mark a real Notion query as unnamed."""

    def test_a_bash_only_turn_no_longer_claims_a_complete_read(self):
        """`Bash` contributes nothing to `reads` — deliberately, because a command is not a place.
        What the row does say is that the list is not the whole look."""
        scope = mouth.TurnScope()
        scope.observe(_stream_tool_use(("Bash", {"command": "grep -rn ack seneschal/scripts"}),
                                       ("Bash", {"command": "python -m pytest"})))
        snap = scope.snapshot()
        self.assertEqual(snap["reads"], [], "a command is still not a place")
        self.assertEqual(snap["unnamed_calls"], 2)
        self.assertFalse(snap["reads_complete"])

    def test_the_mixed_turn_is_the_one_that_used_to_lie_hardest(self):
        """A turn that read a file AND ran a command. `reads` is non-empty, so nothing about it looks
        suspicious — and it is still not the whole look. A reader that treats a non-empty `reads` as
        a complete source list is wrong here, and only `reads_complete` says so."""
        scope = mouth.TurnScope()
        scope.observe(_stream_tool_use(("Read", {"file_path": "state/acks.json"}),
                                       ("Bash", {"command": "sqlite3 state/presence.db .tables"})))
        snap = scope.snapshot()
        self.assertEqual(snap["reads"], ["state/acks.json"])
        self.assertEqual(snap["tool_calls"], 2)
        self.assertEqual(snap["unnamed_calls"], 1)
        self.assertFalse(snap["reads_complete"])

    def test_a_turn_whose_every_call_named_a_place_says_so(self):
        scope = mouth.TurnScope()
        scope.observe(_stream_tool_use(("Read", {"file_path": "a.md"}),
                                       ("Grep", {"pattern": "x", "path": "seneschal"})))
        snap = scope.snapshot()
        self.assertEqual(snap["unnamed_calls"], 0)
        self.assertTrue(snap["reads_complete"])

    def test_a_nested_notion_query_names_its_table(self):
        """A Notion query's arguments live one level down under `data`, which a top-level scan
        cannot see — a turn with live queries against the right data source would record
        `reads: []`."""
        scope = mouth.TurnScope()
        scope.observe(_stream_tool_use(
            ("mcp__notion__notion-query-data-sources",
             {"data": {"data_source_urls": ["collection://f336d0bc", "collection://aa11"],
                       "query": 'SELECT * FROM "collection://f336d0bc" LIMIT 4'}})))
        snap = scope.snapshot()
        self.assertEqual(snap["reads"], ["collection://f336d0bc", "collection://aa11"])
        self.assertEqual(snap["unnamed_calls"], 0)
        self.assertTrue(snap["reads_complete"])
        self.assertNotIn("SELECT", json.dumps(snap), "the QUERY is not a place and never lands here")

    def test_notion_fetch_and_search_name_their_targets(self):
        scope = mouth.TurnScope()
        scope.observe(_stream_tool_use(
            ("mcp__notion__notion-fetch", {"id": "collection://tasks"}),
            ("mcp__notion__notion-search", {"query": "invoice", "data_source_url": "collection://x"})))
        snap = scope.snapshot()
        self.assertEqual(snap["reads"], ["collection://tasks", "collection://x"])
        self.assertTrue(snap["reads_complete"])

    def test_the_walk_is_bounded_and_a_target_past_the_bound_counts_as_unnamed(self):
        """The walk is a BOUND, not a search. A place buried deeper than the bound is not harvested —
        and the honest consequence is that the call reads as unnamed, rather than as a complete read
        of nothing."""
        deep = {"a": {"b": {"c": {"d": {"file_path": "buried.md"}}}}}
        scope = mouth.TurnScope()
        scope.observe(_stream_tool_use(("Weird", deep)))
        snap = scope.snapshot()
        self.assertEqual(snap["reads"], [])
        self.assertEqual(snap["unnamed_calls"], 1)
        self.assertFalse(snap["reads_complete"])

    def test_a_cap_also_defeats_completeness_but_truncated_still_means_only_the_cap(self):
        """Two different facts, two different fields. `truncated` is the cap bit and keeps its narrow
        meaning; `reads_complete` is the question a reader of an empty list actually has."""
        scope = mouth.TurnScope()
        for i in range(mouth.SCOPE_MAX_READS + 3):
            scope.observe(_stream_tool_use(("Read", {"file_path": f"f{i}.py"})))
        snap = scope.snapshot()
        self.assertTrue(snap["truncated"])
        self.assertEqual(snap["unnamed_calls"], 0, "every one of those calls DID name a place")
        self.assertFalse(snap["reads_complete"], "a capped list is not a complete one either")

    def test_a_capped_call_still_counts_as_having_named_a_place(self):
        """`named` is decided before the cap. Otherwise a turn that read fifty files would report
        ten unnamed calls and look blind rather than merely truncated."""
        scope = mouth.TurnScope()
        for i in range(mouth.SCOPE_MAX_READS + 6):
            scope.observe(_stream_tool_use(("Read", {"file_path": f"f{i}.py"})))
        self.assertEqual(scope.snapshot()["unnamed_calls"], 0)

    def test_an_old_shaped_dict_reads_as_UNKNOWN_and_never_as_complete(self):
        """**Reading tolerates both shapes and writing invents nothing.** Rows written without the
        fields are not migrated. So a scope handed in without `unnamed_calls` gets NEITHER field
        back: absent means "not measured", and defaulting it to complete would re-assert the exact
        claim the field exists to stop."""
        clean = mouth.normalise_scope({"tool_calls": 10, "tools": ["Bash"], "reads": [],
                                       "truncated": False})
        self.assertNotIn("reads_complete", clean)
        self.assertNotIn("unnamed_calls", clean)
        self.assertEqual(clean["tool_calls"], 10)

    def test_normalise_recomputes_completeness_rather_than_trusting_the_caller(self):
        """A caller does not get to assert a completeness this function can derive — including the
        case where the normaliser's own re-capping raises `truncated`."""
        lied = mouth.normalise_scope({"tool_calls": 4, "tools": ["Bash"], "reads": [],
                                      "truncated": False, "unnamed_calls": 4,
                                      "reads_complete": True})
        self.assertFalse(lied["reads_complete"])
        capped = mouth.normalise_scope({"tool_calls": 99, "tools": [],
                                        "reads": [f"f{i}" for i in range(mouth.SCOPE_MAX_READS + 4)],
                                        "truncated": False, "unnamed_calls": 0,
                                        "reads_complete": True})
        self.assertTrue(capped["truncated"])
        self.assertFalse(capped["reads_complete"])

    def test_reads_complete_is_total_on_junk(self):
        """It is called on the row-writing path, so it answers rather than raises — and the answer it
        picks when it cannot tell is the non-asserting one."""
        for bad in ((None, None), ("x", False), (object(), False), (1, "y"), (None, "z")):
            self.assertIsInstance(mouth.reads_complete(*bad), bool)
        self.assertFalse(mouth.reads_complete("not a number", False))
        self.assertTrue(mouth.reads_complete(None, None), "no calls unnamed and no cap")


class NeverRaises(unittest.TestCase):
    """Invariant 1: recording never costs the message. Every caller is past the point of no return."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_unwritable_state_dir_returns_false(self):
        blocked = os.path.join(self.dir, "afile")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        self.assertFalse(mouth.record_assertion(blocked, surface="telegram", kind="job", text="x"))

    def test_open_failure_returns_false_and_does_not_raise(self):
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(mouth.record_assertion(self.dir, surface="telegram", kind="job",
                                                    text="x"))

    def test_unserialisable_text_still_never_raises(self):
        self.assertTrue(mouth.record_assertion(self.dir, surface="telegram", kind="job",
                                               text=object()))
        self.assertIn("object object", _rows(self.dir)[0]["text"])

    def test_an_exception_anywhere_returns_false_rather_than_raising(self):
        with mock.patch.object(mouth, "origin_hand", side_effect=RuntimeError("boom")):
            self.assertFalse(mouth.record_assertion(self.dir, surface="telegram", kind="job",
                                                    text="x"))
        self.assertEqual(_rows(self.dir), [])

    def test_origin_hand_is_itself_fail_open(self):
        """A broken registry, a broken environment, and an unresolvable cwd each cost a field."""
        with mock.patch.object(mouth, "_registry_source", side_effect=RuntimeError("boom")):
            hand = mouth.origin_hand(self.dir, "daemon", env={mouth.ORIGIN_ENV_VAR: "sess-1"})
        self.assertEqual(hand["speaker"], "daemon")
        self.assertEqual(hand["session_id"], "sess-1")
        self.assertNotIn("source", hand)

    def test_origin_hand_enriches_source_from_the_session_registry(self):
        with mock.patch.object(mouth, "_registry_source", return_value="build"):
            hand = mouth.origin_hand(self.dir, "daemon", env={mouth.ORIGIN_ENV_VAR: "sess-1"})
        # mouth-spec.md §9.4: a warm turn reads `build`, not `daemon` — which is exactly why the
        # process's own `speaker` label is recorded alongside it.
        self.assertEqual(hand["source"], "build")
        self.assertEqual(hand["speaker"], "daemon")


class Reading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_absent_file_reads_empty(self):
        self.assertEqual(mouth.read_assertions(self.dir), [])

    def test_malformed_lines_are_skipped_not_fatal(self):
        mouth.record_assertion(self.dir, surface="telegram", kind="job", text="good")
        with open(mouth.assertions_path(self.dir), "a", encoding="utf-8") as fh:
            fh.write("{not json\n\n[1,2,3]\n")
        mouth.record_assertion(self.dir, surface="telegram", kind="job", text="also good")
        self.assertEqual([r["text"] for r in _rows(self.dir)], ["good", "also good"])

    def test_limit_keeps_the_newest(self):
        for i in range(6):
            mouth.record_assertion(self.dir, surface="telegram", kind="job", text=f"n{i}")
        self.assertEqual([r["text"] for r in mouth.read_assertions(self.dir, limit=2)], ["n4", "n5"])
        self.assertEqual(mouth.read_assertions(self.dir, limit=0), [])

    def test_since_filters_by_stamp(self):
        mouth.record_assertion(self.dir, surface="telegram", kind="job", text="old",
                               now=NOW - timedelta(hours=5))
        mouth.record_assertion(self.dir, surface="telegram", kind="job", text="new", now=NOW)
        rows = mouth.read_assertions(self.dir, since=NOW - timedelta(hours=1))
        self.assertEqual([r["text"] for r in rows], ["new"])


class Prune(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_drops_only_expired_rows(self):
        mouth.record_assertion(self.dir, surface="telegram", kind="job", text="ancient",
                               now=NOW - timedelta(days=45))
        mouth.record_assertion(self.dir, surface="telegram", kind="job", text="recent",
                               now=NOW - timedelta(days=2))
        self.assertEqual(mouth.prune(self.dir, days=30, now=NOW), 1)
        self.assertEqual([r["text"] for r in _rows(self.dir)], ["recent"])

    def test_undated_and_garbled_rows_are_kept(self):
        """This GC must never be the thing that loses a record of something the assistant said."""
        path = mouth.assertions_path(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema": mouth.SCHEMA, "text": "no stamp"}) + "\n")
            fh.write('{"at": "not-a-date", "text": "bad stamp"}\n')
            fh.write("{garbage\n")
        self.assertEqual(mouth.prune(self.dir, days=1, now=NOW), 0)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 3)

    def test_zero_days_keeps_everything(self):
        mouth.record_assertion(self.dir, surface="telegram", kind="job", text="ancient",
                               now=NOW - timedelta(days=999))
        self.assertEqual(mouth.prune(self.dir, days=0, now=NOW), 0)
        self.assertEqual(len(_rows(self.dir)), 1)

    def test_absent_file_is_a_no_op(self):
        self.assertEqual(mouth.prune(self.dir, days=30, now=NOW), 0)


class Cli(unittest.TestCase):
    """The CLI is how the PowerShell bypass mouth (seneschald-control.ps1) records — so it is load-bearing,
    not a convenience."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_record_appends(self):
        rc = mouth.main(["--state-dir", self.dir, "record", "--surface", "telegram",
                         "--kind", "alert", "--speaker", "watchdog", "--text", "brain is down"])
        self.assertEqual(rc, 0)
        row = _rows(self.dir)[0]
        self.assertEqual(row["text"], "brain is down")
        self.assertEqual(row["kind"], "alert")
        self.assertEqual(row["origin_hand"]["speaker"], "watchdog")

    def test_record_not_delivered(self):
        mouth.main(["--state-dir", self.dir, "record", "--surface", "call", "--kind", "reminder",
                    "--text", "x", "--not-delivered", "--reason", "no answer"])
        self.assertFalse(_rows(self.dir)[0]["delivered"])

    def test_prune_subcommand(self):
        mouth.record_assertion(self.dir, surface="telegram", kind="job", text="old",
                               now=datetime.now(timezone.utc) - timedelta(days=90))
        self.assertEqual(mouth.main(["--state-dir", self.dir, "prune", "--days", "30"]), 0)
        self.assertEqual(_rows(self.dir), [])


class ScopeHasNoReaders(unittest.TestCase):
    """`turn_id` / `scope` are instrumentation and NOTHING else."""

    def test_nothing_reads_the_new_fields_for_real(self):
        """Behaviour built on these fields is deliberately not built yet. Asserted against the
        SOURCE: a consumer is the thing an ordinary edit adds while every other test stays green, so
        a new reader must come past this test (and add itself to ALLOWED_READERS) on purpose."""
        import glob

        ALLOWED_READERS: set = set()

        readers = []
        for path in glob.glob(os.path.join(SCRIPT_DIR, "*.py")):
            name = os.path.basename(path)
            if name.startswith("test_") or name in ("mouth.py", "presence.py"):
                continue
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
            if 'row.get("scope")' in body or '["scope"]' in body or "TurnScope" in body:
                readers.append(name)
        self.assertEqual(set(readers) - ALLOWED_READERS, set(),
                         "a consumer of turn_id/scope is a behaviour change, not instrumentation")


class QueueAndDrain(unittest.TestCase):
    """Phase 1 — the queue and the dumb dispatcher (mouth-spec §3.2, §3.4, §7).

    **The load-bearing family is failure, not delivery.** Sending a queued message correctly is not
    the risk; losing one is. So the tests that matter are: a failed send stays pending, an expired item
    is *recorded* rather than vanishing, and a raising surface cannot stop the rest of the queue.
    """

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _sink(self, ok=True):
        got = []

        def send(surface, text, item):
            got.append((surface, text, item["id"]))
            return ok
        return got, send

    # ---- FIFO, the one ordering guarantee phase 1 buys -------------------------------------------
    def test_fifo_per_surface(self):
        for i in range(5):
            mouth.enqueue(self.d, surface="telegram", kind="job", text=f"msg-{i}")
        got, send = self._sink()
        mouth.drain(self.d, send=send)
        self.assertEqual([t for _, t, _ in got], [f"msg-{i}" for i in range(5)],
                         "the owner must never get B-then-A when A-then-B happened")

    def test_a_delivered_item_leaves_the_pending_set(self):
        mouth.enqueue(self.d, surface="telegram", kind="job", text="x")
        mouth.drain(self.d, send=self._sink()[1])
        self.assertEqual(mouth.pending(self.d), [])

    def test_delivery_writes_the_assertion(self):
        mouth.enqueue(self.d, surface="telegram", kind="job", text="x")
        mouth.drain(self.d, send=self._sink()[1])
        row, = mouth.read_assertions(self.d)
        self.assertEqual(row["door"], "queue")
        self.assertTrue(row["delivered"])

    # ---- failure: the half that decides whether this is a queue or a leak ------------------------
    def test_a_failed_send_stays_pending_and_writes_nothing(self):
        """At-least-once. The assertions log dedupes, because a row is only written on a landed
        send — so a retry that eventually lands produces exactly one assertion, not two."""
        mouth.enqueue(self.d, surface="telegram", kind="job", text="x")
        r = mouth.drain(self.d, send=self._sink(ok=False)[1])
        self.assertEqual(r["failed"], 1)
        self.assertEqual(len(mouth.pending(self.d)), 1, "a failed send must not consume the item")
        self.assertEqual(mouth.read_assertions(self.d), [],
                         "nothing landed, so nothing may be recorded as said")
        mouth.drain(self.d, send=self._sink()[1])
        self.assertEqual(len(mouth.read_assertions(self.d)), 1, "exactly one, after the retry landed")

    def test_a_raising_surface_does_not_stop_the_queue(self):
        mouth.enqueue(self.d, surface="telegram", kind="job", text="first")
        mouth.enqueue(self.d, surface="telegram", kind="job", text="second")

        seen = []

        def send(surface, text, item):
            seen.append(text)
            if text == "first":
                raise RuntimeError("surface exploded")
            return True

        r = mouth.drain(self.d, send=send)
        self.assertEqual(seen, ["first", "second"], "the second item must still be attempted")
        self.assertEqual(r["sent"], 1)
        self.assertEqual(len(mouth.pending(self.d)), 1, "the exploding one stays for the next tick")

    def test_an_expired_item_is_dropped_AND_recorded(self):
        """Nothing vanishes unrecorded — that is the whole difference between a queue and a leak."""
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        mouth.enqueue(self.d, surface="telegram", kind="nudge", text="too late", expires_at=past)
        got, send = self._sink()
        r = mouth.drain(self.d, send=send)
        self.assertEqual(r["dropped"], 1)
        self.assertEqual(got, [], "an expired item must not be sent")
        row, = mouth.read_assertions(self.d)
        self.assertFalse(row["delivered"])
        self.assertIn("expired", row["reason"])

    def test_enqueue_never_raises(self):
        # Same contract as record_assertion: a queue that can throw can cost a message.
        os.makedirs(os.path.join(self.d, mouth.OUTBOUND_FILE))
        self.assertIsNone(mouth.enqueue(self.d, surface="telegram", kind="job", text="x"))

    def test_a_corrupt_line_costs_one_item(self):
        mouth.enqueue(self.d, surface="telegram", kind="job", text="a")
        with open(os.path.join(self.d, mouth.OUTBOUND_FILE), "a", encoding="utf-8") as fh:
            fh.write("{ not json\n")
        mouth.enqueue(self.d, surface="telegram", kind="job", text="b")
        self.assertEqual([i["text"] for i in mouth.pending(self.d)], ["a", "b"])


class Staleness(unittest.TestCase):
    """§3.4 rule 2. `observed_at` is when the FACT became true — getting it wrong makes the whole
    feature a silent no-op, because everything would always look fresh."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_a_fresh_fact_is_not_annotated(self):
        item = mouth.enqueue(self.d, surface="telegram", kind="job", text="done")
        self.assertEqual(mouth.staleness_prefix(item), "")

    def test_an_old_fact_is_stamped_with_WHEN_not_how_long_ago(self):
        """An as-of stamp is checkable against the owner's memory of the day; '17 minutes ago' is not."""
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        item = mouth.enqueue(self.d, surface="telegram", kind="job", text="done",
                             observed_at=old)
        prefix = mouth.staleness_prefix(item)
        self.assertTrue(prefix.startswith("(from "), prefix)
        self.assertIn(tz_common.to_local(old).strftime("%H:%M"), prefix)

    def test_the_stamp_reaches_the_delivered_text(self):
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        mouth.enqueue(self.d, surface="telegram", kind="job", text="job finished", observed_at=old)
        got = []
        mouth.drain(self.d, send=lambda s, t, i: (got.append(t), True)[1])
        self.assertTrue(got[0].startswith("(from "))
        self.assertIn("job finished", got[0])

    def test_queued_at_is_not_observed_at(self):
        """The distinction the spec calls load-bearing: a job queued now about something that ended
        an hour ago is stale, and using queued_at would call it fresh."""
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        item = mouth.enqueue(self.d, surface="telegram", kind="job", text="x", observed_at=old)
        self.assertNotEqual(item["observed_at"], item["queued_at"])
        self.assertNotEqual(mouth.staleness_prefix(item), "")

    def test_an_unreadable_observed_at_is_not_annotated(self):
        # Fail-open: an un-parseable stamp must not invent a staleness claim.
        self.assertEqual(mouth.staleness_prefix({"observed_at": "who knows"}), "")
        self.assertEqual(mouth.staleness_prefix({}), "")


class ReminderCarveOut(unittest.TestCase):
    """§3.5 — standing policy, named in the spec because breaking it would look like a feature."""

    def test_reminders_and_calls_are_passthrough_kinds(self):
        self.assertIn("reminder", mouth.PASSTHROUGH_KINDS)
        self.assertIn("call", mouth.PASSTHROUGH_KINDS)

    def test_phase_1_ships_no_shaping_at_all(self):
        """The carve-out cannot be violated yet because nothing merges anything — asserted so that
        whoever builds phase 2's digest has to come past this test to do it."""
        self.assertFalse(hasattr(mouth, "digest"), "phase 2 must add shaping deliberately")
        d = tempfile.mkdtemp()
        for i in range(5):
            mouth.enqueue(d, surface="telegram", kind="reminder", text=f"nudge-{i}")
        got = []
        mouth.drain(d, send=lambda s, t, it: (got.append(t), True)[1])
        self.assertEqual(len(got), 5, "five reminders are five pushes — never one digest")


if __name__ == "__main__":
    unittest.main()
