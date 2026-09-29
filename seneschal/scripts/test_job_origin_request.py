#!/usr/bin/env python3
"""Tests for `origin.request` — WHO ASKED for a job (phase 5 of `docs/job-origin-routing-spec.md`).

**The invariant this whole file exists to hold: A WRONG ATTRIBUTION IS WORSE THAN NO ATTRIBUTION.**
`origin.request` is worth having only if *"who asked for this?"* can be trusted, and one
plausible-but-wrong link destroys that for every row at once — nobody can tell which of the answers
were guesses afterwards. So the tests that matter most here are the **negative** ones: a pointer for
another session, a closed turn, a stale turn, an anonymous turn, a corrupt file, and two inbound
messages that read identically. Every one of them must produce `by: "unresolved"` **with a reason**,
and none of them may produce a plausible-looking id.

The second invariant is the phase-0 one, unchanged and re-asserted here because this feature writes
into the field it guards: **with neither a session id nor a goal, `origin` is still exactly `{}`** —
byte-for-byte what every caller wrote before any of this existed.

The third is that **nothing here is retro-fitted**: a record written before this shipped carries no
`request` and must read as *unresolved*, never as an inferred one. There is no backfill and there
must never be one — the historical rows have no message identity to recover, so inventing one would
be manufacturing evidence in exactly the log whose value is that it doesn't.

No spawns, no network, no clock: `start_job` takes an injectable `runner`, every time-dependent
assertion injects its instant, and the presence-side turn tests drive the real drainer against
`StubWarmSession` with `--stub-send`.

Run: python -m unittest discover -s seneschal/scripts -p "test_job_origin_request.py"   (or) python test_job_origin_request.py
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import jobs  # noqa: E402
import presence as pr  # noqa: E402

# The daemon side of this feature (the drainer opening/closing the pointer, the inbound reverse
# lookup) lands with the newer presence.py; until then those classes skip rather than fail.
_PRESENCE_WIRED = hasattr(pr, "resolve_inbound_message_id")
_PRESENCE_SKIP = "presence wiring lands in wave 26"


class _FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid


def _runner(pid=4321):
    def run(argv, **kwargs):
        return _FakeProc(pid)
    return run


# --------------------------------------------------------------------------- the pointer itself

class TurnPointerTests(unittest.TestCase):
    """The file the daemon leaves so a job can say which turn it was asked for in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_it_carries_ids_and_never_text(self):
        """The privacy property, asserted as an absence rather than trusted to review.

        A `!private` turn must be exactly as safe to stamp as any other, and the cheapest way to
        guarantee that is for there to be no field a later edit could helpfully put content in."""
        jobs.write_turn_pointer(self.state, turn_id="t1", session_id="s1", surface="telegram",
                                by="owner", message_id=99)
        ptr = jobs.read_turn_pointer(self.state)
        self.assertEqual(set(ptr) - {"schema", "turn_id", "session_id", "surface", "by",
                                     "message_id", "message_why", "opened_at", "closed_at"}, set())
        blob = json.dumps(ptr)
        self.assertNotIn("text", blob)
        self.assertNotIn("preview", blob)

    def test_a_corrupt_or_absent_pointer_reads_as_nothing(self):
        self.assertEqual(jobs.read_turn_pointer(self.state), {})
        with open(jobs.turn_pointer_path(self.state), "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        self.assertEqual(jobs.read_turn_pointer(self.state), {})
        with open(jobs.turn_pointer_path(self.state), "w", encoding="utf-8") as fh:
            fh.write('{"schema": "seneschal.turn-pointer/1"}')   # no turn_id
        self.assertEqual(jobs.read_turn_pointer(self.state), {})

    def test_the_writer_never_raises(self):
        """It runs on the drainer's hot path. A turn that cannot write its pointer costs the
        attribution of any job it starts, and nothing else."""
        with mock.patch.object(jobs, "save_json", side_effect=OSError("disk full")):
            self.assertFalse(jobs.write_turn_pointer(self.state, turn_id="t1", by="owner",
                                                     session_id="s1"))
            self.assertFalse(jobs.stamp_turn_pointer_session(self.state, "s1"))
            self.assertFalse(jobs.close_turn_pointer(self.state, "t1"))

    def test_an_anonymous_pointer_is_named_once_and_never_re_pointed(self):
        """The cold-spawn fill, and the thing it may not become.

        On a cold spawn the CLI reports its session id DURING the first send, after the pointer is
        already open — so the fill exists. But a pointer that already names a session is never
        re-pointed at another one: that would hand one session's turn to a different session, which
        is the wrong-attribution failure wearing a helpful face."""
        jobs.write_turn_pointer(self.state, turn_id="t1", by="owner", session_id=None,
                                surface="telegram")
        self.assertIsNone(jobs.read_turn_pointer(self.state)["session_id"])
        self.assertTrue(jobs.stamp_turn_pointer_session(self.state, "s1"))
        self.assertEqual(jobs.read_turn_pointer(self.state)["session_id"], "s1")
        self.assertFalse(jobs.stamp_turn_pointer_session(self.state, "s2"))
        self.assertEqual(jobs.read_turn_pointer(self.state)["session_id"], "s1")

    def test_a_closed_pointer_is_not_re_stamped_or_re_closed(self):
        jobs.write_turn_pointer(self.state, turn_id="t1", by="assistant", session_id=None)
        self.assertTrue(jobs.close_turn_pointer(self.state, "t1"))
        self.assertFalse(jobs.close_turn_pointer(self.state, "t1"))
        self.assertFalse(jobs.stamp_turn_pointer_session(self.state, "s1"))

    def test_a_late_close_cannot_close_a_successor_turns_pointer(self):
        """Closing by id is what makes the `finally` safe: a turn that already lost the pointer to
        its successor must not close the successor's on its way out."""
        jobs.write_turn_pointer(self.state, turn_id="t1", by="owner", session_id="s1")
        jobs.write_turn_pointer(self.state, turn_id="t2", by="owner", session_id="s1")
        self.assertFalse(jobs.close_turn_pointer(self.state, "t1"))
        self.assertIsNone(jobs.read_turn_pointer(self.state)["closed_at"])


# --------------------------------------------------------------------------- the three rungs

class RequestRungTests(unittest.TestCase):
    """The three rungs, strongest first — and the floor underneath them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _rec(self, req):
        return {"origin": {"session_id": "s1", "request": req}}

    def test_rung_1_she_asked_in_this_telegram_message(self):
        jobs.write_turn_pointer(self.state, turn_id="abc123", session_id="s1", surface="telegram",
                                by="owner", message_id=4242)
        req = jobs.build_request(self.state, session_id="s1")
        self.assertEqual(req["by"], "owner")
        self.assertEqual(req["turn_id"], "abc123")
        self.assertEqual(req["message_id"], "4242")
        self.assertEqual(req["surface"], "telegram")
        self.assertEqual(req["resolved_by"], "turn-pointer")
        self.assertNotIn("why", req)          # nothing was missing, so nothing is explained away
        self.assertEqual(jobs.request_rung(self._rec(req)), 1)

    def test_rung_2_she_asked_but_no_message_handle_exists(self):
        """A picker tap is the case that motivates this rung's existence: the tap's `message_id`
        names the ASSISTANT's question, not a message from the owner, so there is no owner message
        to point at — and the row says which thing is missing rather than looking complete."""
        jobs.write_turn_pointer(self.state, turn_id="def456", session_id="s1", surface="telegram",
                                by="owner",
                                message_why="a picker tap names the assistant's question, not the owner's message")
        req = jobs.build_request(self.state, session_id="s1")
        self.assertEqual(req["by"], "owner")
        self.assertEqual(req["turn_id"], "def456")
        self.assertNotIn("message_id", req)
        self.assertIn("picker tap", req["why"])
        self.assertEqual(jobs.request_rung(self._rec(req)), 2)

    def test_rung_3_assistant_started_this_itself(self):
        """A positive claim, not an absence: the row says the assistant decided it, and names the
        turn it decided it in. A `[job finished …]` inbound is the ordinary way this happens."""
        jobs.write_turn_pointer(self.state, turn_id="ghi789", session_id="s1", surface="telegram",
                                by="assistant")
        req = jobs.build_request(self.state, session_id="s1")
        self.assertEqual(req["by"], "assistant")
        self.assertEqual(req["turn_id"], "ghi789")
        self.assertNotIn("message_id", req)
        self.assertEqual(jobs.request_rung(self._rec(req)), 3)

    def test_a_message_id_is_never_attached_to_a_assistant_request(self):
        """Rung 3 says the assistant started it. One of the owner's message ids hanging off that
        says the opposite, so `normalize_request` drops it however it got there."""
        req = jobs.normalize_request({"by": "assistant", "turn_id": "t", "message_id": "4242",
                                      "resolved_by": "turn-pointer"})
        self.assertNotIn("message_id", req)
        self.assertEqual(jobs.request_rung({"origin": {"request": req}}), 3)

    def test_the_rung_is_derived_and_never_stored(self):
        """A stored rung is a second source of truth that can disagree with the fields it
        summarises — and the first time it does, the summary is what someone quotes."""
        jobs.write_turn_pointer(self.state, turn_id="t", session_id="s1", by="owner",
                                message_id=1)
        origin = jobs.build_origin(self.state, goal="g", env={jobs.ORIGIN_ENV_VAR: "s1"})
        self.assertNotIn("rung", origin["request"])
        self.assertNotIn("rung", json.dumps(origin))


# --------------------------------------------------------------- unresolved: the honest floor

class UnresolvedTests(unittest.TestCase):
    """**The invariant.** Every way the resolution can fail must land on `by: "unresolved"` WITH a
    reason, and none of them may produce an id.

    A reason rather than a bare no, deliberately: an `unresolved` row that says which check failed
    is auditable, and a shrug is what makes people start guessing again."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _assert_unresolved(self, req, expect_phrase):
        self.assertEqual(req["by"], "unresolved")
        self.assertNotIn("message_id", req)
        self.assertNotIn("turn_id", req)
        self.assertEqual(req["resolved_by"], "none")
        self.assertIn(expect_phrase, req["why"])
        self.assertEqual(jobs.request_rung({"origin": {"request": req}}), 0)

    def test_no_pointer_at_all(self):
        """The desktop / build-session case, and the commonest one: a session that is not the
        daemon's warm one has no turn to point at."""
        self._assert_unresolved(jobs.build_request(self.state, session_id="s1"),
                                "not started from inside a warm-session turn")

    def test_the_pointer_belongs_to_another_session(self):
        """The one that would matter most if it went the other way: a desktop `/assistant` running
        `jobs.py start` must NOT inherit the daemon's open turn and claim the owner asked for it."""
        jobs.write_turn_pointer(self.state, turn_id="t", session_id="warm-session", by="owner",
                                message_id=7)
        self._assert_unresolved(jobs.build_request(self.state, session_id="desktop-session"),
                                "belongs to a different session")

    def test_the_job_carries_no_session_id(self):
        jobs.write_turn_pointer(self.state, turn_id="t", session_id="s1", by="owner", message_id=7)
        self._assert_unresolved(jobs.build_request(self.state, session_id=None),
                                "carries no session id")

    def test_the_turn_has_not_been_told_its_session_id_yet(self):
        """The cold-spawn window, before `_session_opened` fills it in. An unverified pointer is
        exactly the guess this feature refuses to make, so it resolves to nothing."""
        jobs.write_turn_pointer(self.state, turn_id="t", session_id=None, by="owner", message_id=7)
        self._assert_unresolved(jobs.build_request(self.state, session_id="s1"),
                                "not been told its session id yet")

    def test_the_turn_had_already_ended(self):
        jobs.write_turn_pointer(self.state, turn_id="t", session_id="s1", by="owner", message_id=7)
        jobs.close_turn_pointer(self.state, "t")
        self._assert_unresolved(jobs.build_request(self.state, session_id="s1"),
                                "had already ended")

    def test_a_stale_pointer_is_outside_the_window(self):
        """The residual path the age cap covers: the daemon killed mid-turn, then RESUMED onto the
        same session id with the pointer never closed."""
        opened = jobs._utc_now() - timedelta(seconds=jobs.TURN_POINTER_MAX_AGE_SEC + 60)
        jobs.write_turn_pointer(self.state, turn_id="t", session_id="s1", by="owner",
                                message_id=7, now=opened)
        self._assert_unresolved(jobs.build_request(self.state, session_id="s1"),
                                "outside the")

    def test_an_unreadable_open_time_is_unresolved_not_assumed_fresh(self):
        jobs.write_turn_pointer(self.state, turn_id="t", session_id="s1", by="owner", message_id=7)
        ptr = jobs.read_turn_pointer(self.state)
        ptr["opened_at"] = "not a timestamp"
        jobs.save_json(jobs.turn_pointer_path(self.state), ptr)
        self._assert_unresolved(jobs.build_request(self.state, session_id="s1"),
                                "no readable open time")

    def test_a_pointer_that_did_not_record_a_requester_is_unresolved(self):
        """`write_turn_pointer`'s `by` is REQUIRED and has no default, because a default of `"assistant"`
        would make rung 3's positive claim on behalf of a caller who merely forgot to say. An
        unrecognised value takes the same route out rather than being coerced to either real answer:
        coercing is inventing exactly the thing this must never invent."""
        with self.assertRaises(TypeError):
            jobs.write_turn_pointer(self.state, turn_id="t", session_id="s1")
        jobs.write_turn_pointer(self.state, turn_id="t", by="the cat", session_id="s1",
                                message_id=7)
        self.assertEqual(jobs.read_turn_pointer(self.state)["by"], "unresolved")
        self._assert_unresolved(jobs.build_request(self.state, session_id="s1"),
                                "did not record who composed")

    def test_a_corrupt_pointer_file_is_unresolved(self):
        with open(jobs.turn_pointer_path(self.state), "w", encoding="utf-8") as fh:
            fh.write("}{ truncated")
        self._assert_unresolved(jobs.build_request(self.state, session_id="s1"),
                                "no open turn pointer")

    def test_an_unresolved_row_can_never_carry_a_message_id(self):
        """Belt to the resolver's braces: an id with nobody behind it is the wrong-attribution
        shape, so the normalizer refuses it however it was constructed."""
        req = jobs.normalize_request({"by": "unresolved", "message_id": "4242",
                                      "why": "…", "resolved_by": "none"})
        self.assertNotIn("message_id", req)


# --------------------------------------------------------------------------- the assertion flags

class AssertedRequestTests(unittest.TestCase):
    """`--requested-by` / `--request-turn` — for the callers a pointer cannot cover."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_an_asserted_requester_is_marked_as_asserted(self):
        req = jobs.build_request(self.state, session_id="s1", requested_by="owner",
                                 turn_id="typed-in-by-hand")
        self.assertEqual(req["by"], "owner")
        self.assertEqual(req["turn_id"], "typed-in-by-hand")
        self.assertEqual(req["resolved_by"], "flag")

    def test_resolved_by_is_conservative_when_provenance_is_mixed(self):
        """A provenance marker that over-claims verification is worse than none — the `stamped_by`
        argument one level down. If any field was asserted, the whole row reads `flag`."""
        jobs.write_turn_pointer(self.state, turn_id="verified", session_id="s1", by="assistant")
        req = jobs.build_request(self.state, session_id="s1", requested_by="owner")
        self.assertEqual(req["by"], "owner")          # the flag won
        self.assertEqual(req["turn_id"], "verified")   # the pointer supplied this
        self.assertEqual(req["resolved_by"], "flag")

    def test_asserting_a_requester_with_nothing_verified_still_says_what_is_missing(self):
        req = jobs.build_request(self.state, session_id="s1", requested_by="owner")
        self.assertEqual(req["by"], "owner")
        self.assertNotIn("turn_id", req)
        self.assertIn("not started from inside a warm-session turn", req["why"])
        self.assertEqual(jobs.request_rung({"origin": {"request": req}}), 0)

    def test_an_unknown_requester_value_becomes_unresolved_rather_than_being_dropped(self):
        """A row with a turn id and no requester would read as a rung the record cannot support."""
        req = jobs.normalize_request({"by": "the cat", "turn_id": "t", "resolved_by": "wishing"})
        self.assertEqual(req["by"], "unresolved")
        self.assertEqual(req["resolved_by"], "none")


# ------------------------------------------------------------------- the record it lands in

class RecordShapeTests(unittest.TestCase):
    """What reaches disk, what round-trips, and what is deliberately left alone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _start(self, **kw):
        kw.setdefault("runner", _runner())
        return jobs.start_job(self.state, "a job", ["python", "-c", "pass"], **kw)

    def test_no_id_and_no_goal_is_still_exactly_an_empty_origin(self):
        """Phase 0's load-bearing invariant, re-asserted because this feature writes into the field
        it guards. A `request` block hanging off nothing is a return address for no letter."""
        self.assertEqual(jobs.build_origin(self.state, env={}), {})
        jobs.write_turn_pointer(self.state, turn_id="t", session_id="s1", by="owner", message_id=1)
        self.assertEqual(jobs.build_origin(self.state, env={}), {})

    def test_it_round_trips_through_save_and_load(self):
        jobs.write_turn_pointer(self.state, turn_id="t9", session_id="s1", surface="telegram",
                                by="owner", message_id=555)
        origin = jobs.build_origin(self.state, goal="find the leak",
                                   env={jobs.ORIGIN_ENV_VAR: "s1"})
        rec = self._start(origin=origin)
        back = jobs.load_job(self.state, rec["id"])
        self.assertEqual(back["origin"]["request"]["message_id"], "555")
        self.assertEqual(jobs.request_rung(back), 1)

    def test_the_nested_block_is_normalized_not_stringified(self):
        """`normalize_origin` `str()`s every other key. Doing that to a dict writes
        `"{'by': 'owner', …}"` into the record, which round-trips as a string and reads as data."""
        out = jobs.normalize_origin({"session_id": "s1",
                                     "request": {"by": "owner", "turn_id": "t",
                                                 "resolved_by": "turn-pointer",
                                                 "colour": "purple"}})
        self.assertIsInstance(out["request"], dict)
        self.assertNotIn("colour", out["request"])          # unknown keys dropped, as one level up
        self.assertEqual(out["request"]["by"], "owner")

    def test_an_unusable_request_is_dropped_rather_than_coerced(self):
        for junk in ("a string", 17, [], {}, None):
            out = jobs.normalize_origin({"session_id": "s1", "request": junk})
            self.assertNotIn("request", out, f"{junk!r} should not survive normalization")

    def test_historical_records_are_left_alone_and_read_as_unresolved(self):
        """**There is no backfill and there must never be one.** The rows that predate this have no
        message identity and no way to recover one, so an inferred origin would be manufactured
        evidence in the one log whose entire value is that it isn't."""
        for legacy in ({}, {"origin": {}}, {"origin": {"session_id": "s1", "stamped_by": "env"}}):
            self.assertEqual(jobs.job_request(legacy), {})
            self.assertEqual(jobs.request_rung(legacy), 0)
        rec = self._start(origin={})
        self.assertEqual(jobs.load_job(self.state, rec["id"])["origin"], {})

    def test_an_analysis_job_still_carries_no_origin_and_therefore_no_requester(self):
        """The recursion guard, unchanged: an analysis of an analysis has nowhere to route. A
        `request` block would be the same loop with a friendlier name on it."""
        jobs.write_turn_pointer(self.state, turn_id="t", session_id="s1", by="owner", message_id=1)
        rec = self._start(analysis_for="20260828-000000-aaaa")
        self.assertEqual(rec["origin"], {})
        self.assertEqual(jobs.job_request(rec), {})
        with self.assertRaises(ValueError):
            self._start(analysis_for="20260828-000000-aaaa",
                        origin=jobs.build_origin(self.state, goal="g",
                                                 env={jobs.ORIGIN_ENV_VAR: "s1"}))

    def test_the_cli_start_path_stamps_it(self):
        """The auto-stamp is the whole mechanism (§2(b)): a field a caller must REMEMBER to populate
        is not a mechanism, and `jobs.py start` is typed by a model mid-turn."""
        jobs.write_turn_pointer(self.state, turn_id="cli-turn", session_id="cli-session",
                                surface="telegram", by="owner", message_id=8080)
        real_start = jobs.start_job

        def stubbed(*a, **kw):
            kw.setdefault("runner", _runner())
            return real_start(*a, **kw)

        with mock.patch.dict(os.environ, {jobs.ORIGIN_ENV_VAR: "cli-session"}), \
             mock.patch.object(jobs, "start_job", stubbed), mock.patch("sys.stdout"):
            self.assertEqual(jobs.main(["--state-dir", self.state, "start", "--title", "t",
                                        "--goal", "prove the stamp fires", "--", "python", "-c",
                                        "pass"]), 0)
        rec = jobs.list_jobs(self.state)[0]
        self.assertEqual(rec["origin"]["request"]["turn_id"], "cli-turn")
        self.assertEqual(rec["origin"]["request"]["message_id"], "8080")
        self.assertEqual(jobs.request_rung(rec), 1)

    def test_a_stamp_that_cannot_resolve_never_blocks_the_spawn(self):
        """**Fail-open on the job, fail-honest on the field.** The read sits on the spawn path of
        every job, so its tolerance has to be total rather than a list of anticipated exceptions —
        this test failed on the first cut, against a read that only absorbed the failures
        `sentinel.load_json` already names."""
        with mock.patch.object(jobs, "load_json", side_effect=OSError("boom")):
            self.assertEqual(jobs.read_turn_pointer(self.state), {})
            req = jobs.build_request(self.state, session_id="s1")
            self.assertEqual(req["by"], "unresolved")
            origin = jobs.build_origin(self.state, goal="g", env={jobs.ORIGIN_ENV_VAR: "s1"})
        self.assertEqual(origin["request"]["by"], "unresolved")
        rec = self._start(origin=origin)              # …and the job still starts
        self.assertEqual(rec["status"], jobs.RUNNING)
        self.assertEqual(jobs.request_rung(jobs.load_job(self.state, rec["id"])), 0)


# -------------------------------------------------- WHEN rung 3 is legitimate (spec §3.5.7)

class SelfStartReasonClassTests(unittest.TestCase):
    """`request.reason_class` — which self-starts the owner wants.

    **Three of the four classes are WANTED, not tolerated**, and the fourth is a violation that has
    to be recordable AS a violation. That is the property most of this class defends: if
    `floated-idea` could not be stored, a class-4 spawn would be filed as whichever approved class
    sat nearest, and the one thing the owner needs to see would be the one thing invisible."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _start(self, **kw):
        kw.setdefault("runner", _runner())
        return jobs.start_job(self.state, "a job", ["python", "-c", "pass"], **kw)

    def test_a_self_start_with_no_class_reads_unstated_rather_than_silence(self):
        """`unstated` is a real state — the assistant self-started and did not say why — and an absent key
        would make it indistinguishable from a record written before this existed."""
        jobs.write_turn_pointer(self.state, turn_id="t3", session_id="s1", by="assistant")
        req = jobs.build_request(self.state, session_id="s1")
        self.assertEqual(req["by"], "assistant")
        self.assertEqual(req["reason_class"], jobs.REQUEST_REASON_UNSTATED)
        self.assertEqual(jobs.request_rung({"origin": {"request": req}}), 3)

    def test_each_approved_class_is_recorded_verbatim(self):
        jobs.write_turn_pointer(self.state, turn_id="t3", session_id="s1", by="assistant")
        for cls in jobs.REQUEST_REASON_APPROVED:
            req = jobs.build_request(self.state, session_id="s1", reason_class=cls)
            self.assertEqual(req["reason_class"], cls)
            self.assertFalse(jobs.request_is_violation({"origin": {"request": req}}))

    def test_naming_a_class_does_not_downgrade_a_verified_row_to_flag(self):
        """`resolved_by` says how the IDENTITY fields were learned, and a reason class is not an
        identity field — it can never be verified by anything, so naming one must not make a
        pointer-verified row read as asserted. Otherwise the provenance marker punishes candour."""
        jobs.write_turn_pointer(self.state, turn_id="t3", session_id="s1", by="assistant")
        req = jobs.build_request(self.state, session_id="s1", reason_class="merge-repair")
        self.assertEqual(req["resolved_by"], "turn-pointer")
        self.assertEqual(req["turn_id"], "t3")

    def test_an_unrecognised_class_becomes_unstated_never_an_approved_one(self):
        """The `by` argument one field down: guessing it into an approved class is the failure."""
        jobs.write_turn_pointer(self.state, turn_id="t3", session_id="s1", by="assistant")
        for junk in ("repair", "MERGE-REPAIR", "because I felt like it", "", 17):
            req = jobs.build_request(self.state, session_id="s1", reason_class=junk)
            self.assertEqual(req["reason_class"], jobs.REQUEST_REASON_UNSTATED,
                             f"{junk!r} must not resolve to a named class")

    def test_the_violation_is_representable_and_survives_the_round_trip(self):
        """An unrepresentable failure is an undetectable one. This is the whole design question of
        §3.5.7 and the answer is that class 4 is a stored value like any other."""
        jobs.write_turn_pointer(self.state, turn_id="t3", session_id="s1", by="assistant")
        origin = jobs.build_origin(self.state, goal="build the auto-rebaser",
                                   env={jobs.ORIGIN_ENV_VAR: "s1"},
                                   reason_class=jobs.REQUEST_REASON_VIOLATION)
        back = jobs.load_job(self.state, self._start(origin=origin)["id"])
        self.assertEqual(back["origin"]["request"]["reason_class"], jobs.REQUEST_REASON_VIOLATION)
        self.assertEqual(jobs.request_rung(back), 3)
        self.assertTrue(jobs.request_is_violation(back))

    def test_only_the_violation_answers_the_predicate(self):
        for cls, expected in [("merge-repair", False), ("ruling-durability", False),
                              ("spec-phase", False), (jobs.REQUEST_REASON_UNSTATED, False),
                              (jobs.REQUEST_REASON_VIOLATION, True)]:
            rec = {"origin": {"request": {"by": "assistant", "turn_id": "t", "reason_class": cls}}}
            self.assertIs(jobs.request_is_violation(rec), expected, cls)

    def test_a_class_never_rides_a_row_that_denies_the_self_start(self):
        """A reason class beside `by: owner` would claim a self-start the `by` denies. One of the
        two is wrong, and the class is the one with no evidence behind it."""
        for by in ("owner", "unresolved"):
            out = jobs.normalize_request({"by": by, "turn_id": "t", "resolved_by": "turn-pointer",
                                          "reason_class": "merge-repair"})
            self.assertNotIn("reason_class", out)
            self.assertIsNone(jobs.request_reason({"origin": {"request": out}}))

    def test_normalization_supplies_the_floor_for_a_hand_written_assistant_row(self):
        out = jobs.normalize_request({"by": "assistant", "turn_id": "t", "resolved_by": "flag"})
        self.assertEqual(out["reason_class"], jobs.REQUEST_REASON_UNSTATED)
        out = jobs.normalize_request({"by": "assistant", "turn_id": "t", "reason_class": "vibes"})
        self.assertEqual(out["reason_class"], jobs.REQUEST_REASON_UNSTATED)

    def test_the_owners_turn_is_not_their_consent(self):
        """**The failure this field exists to make visible.** Class 4 happens INSIDE a turn the
        owner opened — they were thinking out loud — so the pointer says `owner` with their
        `message_id` on it, and an unasserted stamp would record the strongest attribution there is
        for a job they never asked for. The turn asserts `assistant`, and the message id goes with
        it: rung 3 says the assistant started this, and an owner id hanging off it would say the
        opposite."""
        jobs.write_turn_pointer(self.state, turn_id="t7", session_id="s1", surface="telegram",
                                by="owner", message_id=4242)
        innocent = jobs.build_request(self.state, session_id="s1")
        self.assertEqual(jobs.request_rung({"origin": {"request": innocent}}), 1)   # the trap
        honest = jobs.build_request(self.state, session_id="s1", requested_by="assistant",
                                    reason_class=jobs.REQUEST_REASON_VIOLATION)
        self.assertEqual(honest["by"], "assistant")
        self.assertEqual(honest["turn_id"], "t7")        # the same turn, still verified
        self.assertNotIn("message_id", honest)
        self.assertEqual(jobs.request_rung({"origin": {"request": honest}}), 3)
        self.assertTrue(jobs.request_is_violation({"origin": {"request": honest}}))

    def test_records_that_predate_this_have_no_class_and_none_is_invented(self):
        """No backfill here either (§3.5.5): a rung-3 row written before this shipped says nothing
        about why, and `unstated` is what that reads as — not a class chosen on its behalf."""
        legacy = {"origin": {"request": {"by": "assistant", "turn_id": "t",
                                         "resolved_by": "turn-pointer"}}}
        self.assertEqual(jobs.request_reason(legacy), jobs.REQUEST_REASON_UNSTATED)
        self.assertFalse(jobs.request_is_violation(legacy))
        for older in ({}, {"origin": {}}, {"origin": {"session_id": "s1"}}):
            self.assertIsNone(jobs.request_reason(older))

    def test_the_cli_flag_reaches_the_record(self):
        jobs.write_turn_pointer(self.state, turn_id="cli-turn", session_id="cli-session",
                                surface="telegram", by="owner", message_id=99)
        real_start = jobs.start_job

        def stubbed(*a, **kw):
            kw.setdefault("runner", _runner())
            return real_start(*a, **kw)

        with mock.patch.dict(os.environ, {jobs.ORIGIN_ENV_VAR: "cli-session"}), \
             mock.patch.object(jobs, "start_job", stubbed), mock.patch("sys.stdout"):
            self.assertEqual(jobs.main(["--state-dir", self.state, "start", "--title", "t",
                                        "--goal", "rebase the PR my merge dirtied",
                                        "--requested-by", "assistant",
                                        "--reason-class", "merge-repair",
                                        "--", "python", "-c", "pass"]), 0)
        rec = jobs.list_jobs(self.state)[0]
        self.assertEqual(jobs.request_rung(rec), 3)
        self.assertEqual(jobs.request_reason(rec), "merge-repair")
        self.assertNotIn("message_id", rec["origin"]["request"])

    def test_the_violation_is_visible_in_list_not_only_in_status(self):
        """The leaked-worktree argument: an answer you only get by already knowing which job to ask
        about is not an answer, and *"I just want to know the situations"* is a `list` question."""
        rec = {"id": "20260828-120000-aaaa", "status": "finished", "title": "the auto-rebaser",
               "created_at": "2026-08-28T17:00:00Z", "ended_at": "2026-08-28T17:01:00Z",
               "origin": {"session_id": "s1", "request": {
                   "by": "assistant", "turn_id": "t7", "resolved_by": "flag",
                   "reason_class": jobs.REQUEST_REASON_VIOLATION}}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            jobs._print_job(rec)                      # `list`'s call: attempts=False
        self.assertIn(jobs.REQUEST_REASON_VIOLATION, buf.getvalue())
        rec["origin"]["request"]["reason_class"] = "merge-repair"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            jobs._print_job(rec)                      # …and an APPROVED class is not shouted at
        self.assertNotIn("SELF-STARTED", buf.getvalue())


# ------------------------------------------------------- presence: which message was the request

@unittest.skipUnless(_PRESENCE_WIRED, _PRESENCE_SKIP)
class InboundMessageIdResolutionTests(unittest.TestCase):
    """The reverse lookup, and why it proves uniqueness instead of assuming it."""

    def test_a_unique_line_resolves(self):
        mid, why = pr.resolve_inbound_message_id({"101": "look into the RAG refresh"},
                                                 "look into the RAG refresh")
        self.assertEqual(mid, "101")
        self.assertIsNone(why)

    def test_two_identical_lines_resolve_to_nothing(self):
        """The failure this whole feature is built to avoid. The owner says `ok` twice; picking either id
        is a wrong attribution, and a wrong one is worse than none."""
        mid, why = pr.resolve_inbound_message_id({"101": "ok", "102": "ok"}, "ok")
        self.assertIsNone(mid)
        self.assertIn("2 messages", why)

    def test_a_line_with_no_recorded_id_says_which_shapes_arrive_without_one(self):
        mid, why = pr.resolve_inbound_message_id({"101": "something else"}, "[job finished] x")
        self.assertIsNone(mid)
        self.assertIn("picker tap", why)

    def test_an_empty_or_missing_window_is_a_reason_not_a_crash(self):
        for ids in ({}, None, "not a dict"):
            mid, why = pr.resolve_inbound_message_id(ids, "hey")
            self.assertIsNone(mid)
            self.assertTrue(why)
        self.assertEqual(pr.resolve_inbound_message_id({"1": ""}, "")[0], None)


# ------------------------------------------------------------- presence: the turn writes it

def _args(state_dir, **over):
    base = dict(state_dir=state_dir, telegram_env="unused.env", call_env=None, discord_env=None,
                no_discord=True, no_discord_gateway=False, stub_brain=True, stub_send=True,
                fake_inbox=None, router_mode="off", no_reminders=True, no_peek=True, no_slots=True,
                max_iterations=0, poll_timeout=1, discord_poll_sec=0.05, tick_sec=0.05, idle_min=0.0,
                model=None, slot_model=None, slot_catchup_min=180, claude_bin="claude",
                notion_mcp=None, slack_mcp=None, permission_mode="bypassPermissions",
                peek_interval_min=0, watch_prompt=None, watch_cmd=None, watch_model=None)
    base.update(over)
    return argparse.Namespace(**base)


async def _drain_one(state, args, timeout=5.0):
    task = asyncio.ensure_future(
        pr.drainer_task(state, args, lambda *_: None, lambda: pr.StubWarmSession(), 600.0))
    try:
        async with asyncio.timeout(timeout):
            while state.pending:
                await asyncio.sleep(0.01)
    finally:
        state.stop.set()
        state.pending_event.set()
        await asyncio.wait_for(task, timeout=5.0)


@unittest.skipUnless(_PRESENCE_WIRED, _PRESENCE_SKIP)
class TurnPointerWiringTests(unittest.IsolatedAsyncioTestCase):
    """End to end through the real drainer: a turn opens the pointer, a job started inside it
    resolves, and the turn closes it on the way out.

    The close is suppressed in the first two tests **on purpose** — the pointer is only readable
    while the turn is live, and reading it live is exactly what a job started mid-turn does. The
    third test is the one that asserts the close really fires."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.args = _args(self.dir)

    async def _turn(self, text, ids, close=False):
        state = pr.DaemonState()
        state.pending = [("telegram", text, 0)]
        state.inbound_ids = dict(ids)
        state.pending_event.set()
        with (contextlib.nullcontext() if close
              else mock.patch.object(jobs, "close_turn_pointer", lambda *a, **kw: False)):
            await _drain_one(state, self.args)
        return jobs.read_turn_pointer(self.dir)

    async def test_a_message_from_owner_lands_on_rung_1(self):
        ptr = await self._turn("look into the RAG refresh", {"777": "look into the RAG refresh"})
        self.assertEqual(ptr["by"], "owner")
        self.assertEqual(ptr["message_id"], "777")
        self.assertEqual(ptr["surface"], "telegram")
        req = jobs.build_request(self.dir, session_id=ptr["session_id"])
        self.assertEqual(jobs.request_rung({"origin": {"request": req}}), 1)
        self.assertEqual(req["turn_id"], ptr["turn_id"])

    async def test_a_daemon_synthesized_inbound_lands_on_rung_3(self):
        """A job-completion notice is not the owner asking for anything. If the assistant starts a
        job off the back of one, that is its decision — a positive claim, stamped with the turn it
        made it in.

        `turns.classify_origin` is the classifier, reused rather than re-spelled: `turns.jsonl`
        already trusts it for exactly this distinction, and two spellings of *"did a person write
        this?"* would drift."""
        ptr = await self._turn("[job finished] 20260828-101010-abcd: done", {})
        self.assertEqual(ptr["by"], "assistant")
        self.assertNotIn("message_id", ptr)
        req = jobs.build_request(self.dir, session_id=ptr["session_id"])
        self.assertEqual(jobs.request_rung({"origin": {"request": req}}), 3)

    async def test_the_turn_closes_its_pointer_on_the_way_out(self):
        """So a job started OUTSIDE any turn cannot inherit the last one's attribution."""
        ptr = await self._turn("hey there", {"1": "hey there"}, close=True)
        self.assertTrue(ptr["closed_at"])
        req = jobs.build_request(self.dir, session_id=ptr["session_id"])
        self.assertEqual(req["by"], "unresolved")
        self.assertIn("had already ended", req["why"])

    async def test_a_pointer_that_cannot_be_written_never_costs_the_turn(self):
        """The turn is what may not be lost, and this is the house rule for every `state/` writer
        here: the guarantee lives IN the writer, so the drainer wraps neither call in a try/except
        and no future caller should either.

        So the failure is injected one layer down — at the write itself, the way a full disk or a
        held handle would really present — rather than by making the writer raise, which would only
        prove that the drainer does not catch what it is documented not to need to catch."""
        state = pr.DaemonState()
        state.pending = [("telegram", "hey there", 0)]
        state.inbound_ids = {"1": "hey there"}
        state.pending_event.set()
        with mock.patch.object(jobs, "save_json", side_effect=OSError("disk full")):
            await _drain_one(state, self.args)
        self.assertEqual(state.pending, [])                       # answered and dequeued
        self.assertEqual(jobs.read_turn_pointer(self.dir), {})     # nothing was written
        sent = os.path.join(self.dir, "sent.jsonl")
        self.assertTrue(os.path.exists(sent), "the reply still went out")


if __name__ == "__main__":
    unittest.main()
