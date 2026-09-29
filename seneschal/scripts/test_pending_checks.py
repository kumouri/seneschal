#!/usr/bin/env python3
"""Tests for the pending-check register (`pending_checks.py`,
`seneschal/docs/session-coupling-spec.md` §6.1 — phase 0; §8.4 for retention).

Five families, ordered by how much each one is guarding:

1. **The fail-open contract.** `record_check` / `record_resolution` must never raise, whatever they
   are handed and whatever the disk does — the append-only-register family's invariant. A failed
   append costs the row and never the claim, the job or
   the turn, so no caller wraps it in a try/except; if that stopped being true, every call site would
   silently need one. **This is the family the whole design leans on.**
2. **The job id IS the mechanism.** §6.1 requires the evidence to be a job id *"deliberately rather
   than restrictively"* — a backgrounded command has no id, no terminal state and nothing to
   reconcile against, so requiring one makes the long-running-work-goes-through-a-JOB rule
   structural instead of advisory. A row that can be written without one is that rule going back
   to being a sentence someone has to remember. The fail-open/fail-closed line is here too: an
   *absent* jobs directory is unknown and must not cost the row; a *readable* one that does not hold
   the id is known-bad and is refused.
3. **Zero behaviour change, and the shape phase 1 has to be able to join on.** Nothing is delivered,
   nothing is read to decide anything, and `job_id` stays the key the deferred sweep keys off.
4. **Retention — §8.4.** Unresolved kept **indefinitely**, resolved pruned at 90
   days. An open check that a GC can delete is the finding being deleted.
5. **The record, the readers and the CLI** — schema round-trip, uncapped text, append-only under
   concurrent writers, a torn line, and the refusal exit codes.

No network, no model, no Notion, and no job is ever spawned: the `state/jobs/` records here are
written by hand into a temp dir.

Run:  python -m unittest discover -s seneschal/scripts -p "test_pending_checks.py"
"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import jobs  # noqa: E402
import pending_checks as pc  # noqa: E402

NOW = datetime(2026, 8, 10, 2, 8, 46, tzinfo=timezone.utc)

# The exhibit: the claim, the check that would have refuted it, and where it went.
CLAIM = "the tool does not exist"
FALSIFIER = "any hit for a watcher in presence.py refutes it"
ARTIFACT = ("~/.claude/projects/C--Users-you-workspace-repos-seneschal/memory/"
            "some-note.md")


def _write_job(state_dir, job_id, *, status="running", title="confirm whether the watcher exists",
               goal="confirm whether presence.py already has a watcher", session_id="1f0e-sess"):
    """One `state/jobs/<id>.json` record, written by hand. Deliberately not via `jobs.start_job` —
    that spawns a child process, and this suite must never launch one."""
    d = os.path.join(state_dir, "jobs")
    os.makedirs(d, exist_ok=True)
    rec = {"schema": jobs.SCHEMA, "id": job_id, "title": title, "status": status,
           "origin": {"session_id": session_id, "goal": goal, "stamped_by": "env"}}
    with open(os.path.join(d, f"{job_id}.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    return rec


def _record(state_dir, job_id="20260810-020846-1b3c", **kw):
    kw.setdefault("claim", CLAIM)
    kw.setdefault("falsifier", FALSIFIER)
    return pc.record_check(state_dir, job_id=job_id, env={}, **kw)


# --------------------------------------------------------------------------- 1. never raises

class NeverRaises(unittest.TestCase):
    """`record_check` returns a bool and never raises — invariant 1. Every call site is a live turn
    that has already made the claim, so the row is never worth the turn."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_write_failure_returns_false_rather_than_raising(self):
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(_record(self.tmp))

    def test_unwritable_state_dir_returns_false(self):
        """The brief's case: `state/` cannot be written at all."""
        with mock.patch("os.makedirs", side_effect=PermissionError("read-only")):
            self.assertFalse(_record(self.tmp))

    def test_a_directory_squatting_on_the_log_costs_the_row_and_nothing_else(self):
        os.makedirs(pc.checks_path(self.tmp), exist_ok=True)
        self.assertFalse(_record(self.tmp))

    def test_an_exotic_value_is_coerced_rather_than_raising(self):
        """Every field that reaches the row goes through `str`/`_text` first, so there is no shape a
        caller can hand this that makes `json.dumps` throw. Coercion, not rejection — a claim that
        was made must not be lost to a type."""
        self.assertTrue(_record(self.tmp, artifacts=[object()], claim=object()))
        row = pc.read_checks(self.tmp)[0]
        self.assertIn("object object at", row["claim"])
        self.assertEqual(len(row["claim_ref"]["artifacts"]), 1)

    def test_a_serialisation_failure_still_only_costs_the_row(self):
        with mock.patch.object(pc.json, "dumps", side_effect=TypeError("not serialisable")):
            self.assertFalse(_record(self.tmp))

    def test_a_raise_anywhere_inside_returns_false(self):
        with mock.patch.object(pc, "_stamp", side_effect=RuntimeError("boom")):
            self.assertFalse(_record(self.tmp))

    def test_resolution_never_raises_either(self):
        _write_job(self.tmp, "j1")
        _record(self.tmp, "j1")
        check_id = pc.read_checks(self.tmp)[0]["check_id"]
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(pc.record_resolution(self.tmp, check_id=check_id, outcome="refuted"))

    def test_a_broken_refusal_check_never_becomes_a_refusal(self):
        """`refusal` is itself fail-open: if the *check* explodes, that is not evidence the pairing
        is bad, and turning an internal error into a refusal would drop a legitimate row."""
        with mock.patch.object(pc, "job_record", side_effect=RuntimeError("boom")):
            self.assertIsNone(pc.refusal(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER))

    def test_stats_and_readers_survive_an_unreadable_log(self):
        with mock.patch("builtins.open", side_effect=OSError("gone")):
            self.assertEqual(pc.read_checks(self.tmp), [])
            self.assertEqual(pc.stats(self.tmp)["rows"], 0)
            self.assertEqual(pc.prune(self.tmp), 0)


# --------------------------------------------------------------------------- 2. the evidence

class TheEvidenceMustBeAJobId(unittest.TestCase):
    """§6.1: *"The evidence must be a job id, and that is deliberate rather than restrictive."* A row
    that can be written without one puts `presence.py`'s job rule back to being advisory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_row_cannot_be_recorded_without_a_job_id(self):
        for missing in (None, "", "   "):
            self.assertFalse(pc.record_check(self.tmp, job_id=missing, claim=CLAIM,
                                             falsifier=FALSIFIER, env={}))
        self.assertEqual(pc.read_checks(self.tmp), [])

    def test_the_refusal_says_why_in_a_real_sentence(self):
        why = pc.refusal(self.tmp, job_id=None, claim=CLAIM, falsifier=FALSIFIER)
        self.assertIn("JOB id", why)
        self.assertIn("jobs.py start", why)

    def test_a_claim_and_a_falsifier_are_both_required(self):
        _write_job(self.tmp, "j1")
        self.assertFalse(pc.record_check(self.tmp, job_id="j1", claim="", falsifier=FALSIFIER, env={}))
        self.assertFalse(pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier="", env={}))
        self.assertEqual(pc.read_checks(self.tmp), [])
        self.assertIn("FALSIFIER", pc.refusal(self.tmp, job_id="j1", claim=CLAIM, falsifier=None))

    def test_an_id_naming_no_job_is_refused_when_the_jobs_dir_is_readable(self):
        """Fail-CLOSED on known-bad: the directory exists and does not hold the id, so this is a
        fabricated id or work that was never a job — the exact thing the requirement prevents."""
        _write_job(self.tmp, "j1")
        self.assertIsNotNone(pc.refusal(self.tmp, job_id="not-a-job", claim=CLAIM, falsifier=FALSIFIER))
        self.assertFalse(_record(self.tmp, "not-a-job"))
        self.assertEqual(pc.read_checks(self.tmp), [])

    def test_an_absent_jobs_dir_is_UNKNOWN_and_does_not_cost_the_row(self):
        """Fail-OPEN on unknown. A fresh checkout, or a `--state-dir` pointed somewhere new, has no
        `jobs/` at all — and losing the pairing there would be this module deciding that its own
        blind spot is the caller's fault."""
        self.assertIsNone(pc.refusal(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER))
        self.assertTrue(_record(self.tmp, "j1"))
        self.assertIs(pc.read_checks(self.tmp)[0]["job_verified"], False)

    def test_a_verified_row_says_so(self):
        _write_job(self.tmp, "j1")
        self.assertTrue(_record(self.tmp, "j1"))
        self.assertIs(pc.read_checks(self.tmp)[0]["job_verified"], True)

    def test_the_goal_and_session_id_come_off_the_job_record(self):
        """`jobs.py` already stamps both (`--goal`, `$CLAUDE_CODE_SESSION_ID`). A caller retyping
        them would be a second source of truth for a field that exists to be joined on."""
        _write_job(self.tmp, "j1", goal="check whether the watcher exists", session_id="sess-42")
        _record(self.tmp, "j1")
        row = pc.read_checks(self.tmp)[0]
        self.assertEqual(row["goal"], "check whether the watcher exists")
        self.assertEqual(row["session_id"], "sess-42")
        self.assertEqual(row["job_title"], "confirm whether the watcher exists")

    def test_the_session_id_falls_back_to_the_environment(self):
        """A job started before the origin auto-stamp, or with `stamped_by: "none"`."""
        d = os.path.join(self.tmp, "jobs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "j1.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": jobs.SCHEMA, "id": "j1", "title": "t", "status": "running",
                       "origin": {}}, fh)
        pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER,
                        env={"CLAUDE_CODE_SESSION_ID": "from-env"})
        self.assertEqual(pc.read_checks(self.tmp)[0]["session_id"], "from-env")


class TheFalsifierIsWrittenBlind(unittest.TestCase):
    """§6.1: the falsifier is written *before the answer is known*. Code cannot see intent, but it
    can see the clock — and a late row is FLAGGED, never refused, because losing the pairing for a
    job that finished in the seconds before the turn wrote it is worse than an honest caveat."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_row_against_a_running_job_is_blind(self):
        _write_job(self.tmp, "j1", status="running")
        _record(self.tmp, "j1")
        self.assertIs(pc.read_checks(self.tmp)[0]["falsifier_blind"], True)

    def test_a_row_against_an_already_terminal_job_is_flagged_not_refused(self):
        for status in jobs.TERMINAL:
            tmp = tempfile.mkdtemp()
            _write_job(tmp, "j1", status=status)
            self.assertTrue(_record(tmp, "j1"), f"{status} must still record")
            self.assertIs(pc.read_checks(tmp)[0]["falsifier_blind"], False, status)

    def test_stats_counts_the_late_ones(self):
        _write_job(self.tmp, "j1", status="running")
        _write_job(self.tmp, "j2", status="done")
        _record(self.tmp, "j1")
        _record(self.tmp, "j2")
        self.assertEqual(pc.stats(self.tmp)["falsifier_not_blind"], 1)


# --------------------------------------------------------------------------- 3. zero behaviour change

class ZeroBehaviourChange(unittest.TestCase):
    """*"Zero behaviour change. Nothing reads it."* (§6.1) — and the shape phase 1 must be able to
    join on, since the deferred sweep keys off `job_id`."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_nothing_is_written_unless_a_caller_calls(self):
        self.assertFalse(os.path.exists(pc.checks_path(self.tmp)))
        pc.stats(self.tmp)
        pc.open_checks(self.tmp)
        pc.prune(self.tmp)
        self.assertFalse(os.path.exists(pc.checks_path(self.tmp)))

    def test_recording_touches_nothing_but_its_own_log(self):
        """It must not write the job record, the assertions log, or anything else in `state/`."""
        _write_job(self.tmp, "j1")
        job_file = os.path.join(self.tmp, "jobs", "j1.json")
        with open(job_file, encoding="utf-8") as fh:
            before = json.load(fh)
        _record(self.tmp, "j1")
        with open(job_file, encoding="utf-8") as fh:
            after = json.load(fh)
        self.assertEqual(before, after)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["jobs", pc.CHECKS_FILE])

    def test_no_module_in_the_tree_imports_this_one(self):
        """Phase 0 has no reader by design. When phase 1 lands, this assertion is the one to retire
        deliberately — a guard that disappears from a diff and a guard that argues with it are very
        different things to read six months later."""
        importers = []
        for name in sorted(os.listdir(SCRIPT_DIR)):
            if not name.endswith(".py") or name.startswith("test_") or name == "pending_checks.py":
                continue
            with open(os.path.join(SCRIPT_DIR, name), encoding="utf-8") as fh:
                if "pending_checks" in fh.read():
                    importers.append(name)
        self.assertEqual(importers, [], f"phase 0 is write-only; {importers} reads it")

    def test_the_job_id_stays_the_key_the_phase_1_sweep_joins_on(self):
        _write_job(self.tmp, "20260810-020846-1b3c")
        _record(self.tmp)
        self.assertEqual(pc.read_checks(self.tmp)[0]["job_id"], "20260810-020846-1b3c")


# --------------------------------------------------------------------------- 4. retention

class Retention(unittest.TestCase):
    """§8.4: **an unresolved check is kept indefinitely** and a resolved one is pruned at 90 days.
    The *open* half is load-bearing — an unresolved check is the artifact that says something was never finished, so a
    GC that can reach it deletes exactly the finding."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _write_job(self.tmp, "j1")
        _write_job(self.tmp, "j2")

    def test_retention_is_90_days(self):
        self.assertEqual(pc.RETENTION_DAYS, 90)

    def test_an_open_check_is_never_pruned_however_old(self):
        pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER, env={},
                        now=NOW - timedelta(days=4000))
        self.assertEqual(pc.prune(self.tmp, days=1, now=NOW), 0)
        self.assertEqual(len(pc.read_checks(self.tmp)), 1)

    def test_a_resolved_check_older_than_the_window_goes_with_both_its_rows(self):
        pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER, env={},
                        check_id="old", now=NOW - timedelta(days=200))
        pc.record_resolution(self.tmp, check_id="old", outcome="refuted",
                             now=NOW - timedelta(days=199))
        self.assertEqual(pc.prune(self.tmp, days=90, now=NOW), 1)
        self.assertEqual(pc.read_checks(self.tmp), [])

    def test_a_recently_resolved_check_stays(self):
        pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER, env={},
                        check_id="recent", now=NOW - timedelta(days=10))
        pc.record_resolution(self.tmp, check_id="recent", outcome="confirmed",
                             now=NOW - timedelta(days=9))
        self.assertEqual(pc.prune(self.tmp, days=90, now=NOW), 0)
        self.assertEqual(len(pc.read_checks(self.tmp)), 2)

    def test_days_zero_keeps_everything(self):
        pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER, env={},
                        check_id="old", now=NOW - timedelta(days=4000))
        pc.record_resolution(self.tmp, check_id="old", outcome="refuted",
                             now=NOW - timedelta(days=3999))
        self.assertEqual(pc.prune(self.tmp, days=0, now=NOW), 0)
        self.assertEqual(len(pc.read_checks(self.tmp)), 2)

    def test_an_unparseable_stamp_is_kept_rather_than_expired(self):
        pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER, env={},
                        check_id="k", now=NOW - timedelta(days=400))
        with open(pc.checks_path(self.tmp), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema": pc.SCHEMA, "id": "r", "check_id": "k",
                                 "at": "not-a-date", "state": "resolved"}) + "\n")
        self.assertEqual(pc.prune(self.tmp, days=90, now=NOW), 0)
        self.assertEqual(len(pc.read_checks(self.tmp)), 2)

    def test_a_malformed_line_survives_a_prune(self):
        pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER, env={},
                        check_id="old", now=NOW - timedelta(days=200))
        pc.record_resolution(self.tmp, check_id="old", now=NOW - timedelta(days=199))
        with open(pc.checks_path(self.tmp), "a", encoding="utf-8") as fh:
            fh.write("{not json at all\n")
        pc.prune(self.tmp, days=90, now=NOW)
        with open(pc.checks_path(self.tmp), encoding="utf-8") as fh:
            self.assertIn("{not json at all", fh.read())


class Resolution(unittest.TestCase):
    """A resolution is an **append**, never a rewrite — §8.2: a correction supersedes rather than
    edits. It exists in phase 0 only because §8.4's retention needs it: a retention rule with nothing
    implementing it is prose."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _write_job(self.tmp, "j1")
        _record(self.tmp, "j1")
        self.check_id = pc.read_checks(self.tmp)[0]["check_id"]

    def test_resolving_appends_and_leaves_the_open_row_byte_for_byte(self):
        before = pc.read_checks(self.tmp)[0]
        self.assertTrue(pc.record_resolution(self.tmp, check_id=self.check_id, outcome="refuted",
                                             note="it exists: presence.py:493"))
        rows = pc.read_checks(self.tmp)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], before)
        self.assertEqual(rows[1]["state"], "resolved")
        self.assertEqual(rows[1]["outcome"], "refuted")
        self.assertEqual(rows[1]["job_id"], "j1")

    def test_an_unknown_check_id_is_refused(self):
        self.assertFalse(pc.record_resolution(self.tmp, check_id="nope", outcome="refuted"))
        self.assertFalse(pc.record_resolution(self.tmp, check_id="", outcome="refuted"))
        self.assertEqual(len(pc.read_checks(self.tmp)), 1)

    def test_an_unknown_outcome_is_recorded_verbatim_rather_than_rejected(self):
        self.assertTrue(pc.record_resolution(self.tmp, check_id=self.check_id, outcome="sideways"))
        self.assertEqual(pc.read_checks(self.tmp)[1]["outcome"], "sideways")

    def test_open_checks_drops_a_check_once_it_is_resolved(self):
        self.assertEqual(len(pc.open_checks(self.tmp)), 1)
        pc.record_resolution(self.tmp, check_id=self.check_id, outcome="confirmed")
        self.assertEqual(pc.open_checks(self.tmp), [])

    def test_the_newest_resolution_wins_and_the_older_one_stays_readable(self):
        pc.record_resolution(self.tmp, check_id=self.check_id, outcome="confirmed",
                             now=NOW)
        pc.record_resolution(self.tmp, check_id=self.check_id, outcome="refuted",
                             now=NOW + timedelta(minutes=5))
        self.assertEqual(pc.stats(self.tmp, now=NOW + timedelta(hours=1))["by_outcome"],
                         {"refuted": 1})
        self.assertEqual([r.get("outcome") for r in pc.read_checks(self.tmp)[1:]],
                         ["confirmed", "refuted"])


# --------------------------------------------------------------------------- 5. record & readers

class RecordShape(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _write_job(self.tmp, "j1")

    def test_writes_the_documented_record(self):
        pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER,
                        assertion_at="2026-08-10T01:07:44Z", assertion_text="No watcher for that.",
                        artifacts=[ARTIFACT], env={}, check_id="c1", now=NOW)
        row = pc.read_checks(self.tmp)[0]
        self.assertEqual(row, {
            "schema": "seneschal.pending-check/1",
            "id": "c1",
            "check_id": "c1",
            "at": "2026-08-10T02:08:46Z",
            "state": "open",
            "job_id": "j1",
            "job_title": "confirm whether the watcher exists",
            "goal": "confirm whether presence.py already has a watcher",
            "claim": CLAIM,
            "falsifier": FALSIFIER,
            "claim_ref": {
                "assertion_at": "2026-08-10T01:07:44Z",
                "assertion_text": "No watcher for that.",
                "artifacts": [ARTIFACT],
            },
            "session_id": "1f0e-sess",
            "job_verified": True,
            "falsifier_blind": True,
        })

    def test_a_round_trip_through_the_file_preserves_every_field(self):
        pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER,
                        artifacts=[ARTIFACT, "seneschal/docs/session-coupling-spec.md"], env={})
        with open(pc.checks_path(self.tmp), encoding="utf-8") as fh:
            raw = json.loads(fh.read().strip())
        self.assertEqual(raw, pc.read_checks(self.tmp)[0])
        self.assertEqual(raw["claim_ref"]["artifacts"],
                         [ARTIFACT, "seneschal/docs/session-coupling-spec.md"])

    def test_the_claim_and_falsifier_are_verbatim_and_uncapped(self):
        long_claim = "x" * 20000
        pc.record_check(self.tmp, job_id="j1", claim=long_claim, falsifier=long_claim, env={})
        row = pc.read_checks(self.tmp)[0]
        self.assertEqual(row["claim"], long_claim)
        self.assertEqual(row["falsifier"], long_claim)

    def test_the_goal_is_capped_to_one_line_like_jobs_does(self):
        """`jobs.one_line` is the single cap, applied where the value is WRITTEN — reusing it means
        there is one definition of "one line of context" rather than two that drift."""
        _write_job(self.tmp, "j2", goal="a" * 400)
        _record(self.tmp, "j2")
        self.assertLessEqual(len(pc.read_checks(self.tmp)[0]["goal"]), jobs.ORIGIN_GOAL_MAX)

    def test_a_claim_that_was_never_spoken_still_records(self):
        """§6.1: *"the strongest case for it is a belief acted on silently."* An assertion row is
        optional; a claim with no `assertions.jsonl` row is the case this file exists for."""
        _record(self.tmp, "j1")
        ref = pc.read_checks(self.tmp)[0]["claim_ref"]
        self.assertIsNone(ref["assertion_at"])
        self.assertIsNone(ref["assertion_text"])
        self.assertEqual(ref["artifacts"], [])

    def test_the_id_is_a_sortable_stamp_plus_a_collision_suffix(self):
        a, b = pc.new_check_id(NOW), pc.new_check_id(NOW)
        self.assertTrue(a.startswith("20260810-020846-"))
        self.assertNotEqual(a, b)

    def test_the_suffix_is_wide_enough_for_a_whole_second_of_rows(self):
        """The regression guard for the CI flake, stated as the property that was actually violated.

        `test_concurrent_writers_lose_nothing` caught this only ~18% of the time, which is why it read
        as flake rather than bug. Minting 5,000 ids inside ONE frozen second makes it deterministic: a
        four-hex suffix (65,536 values) collides here with probability indistinguishable from 1, while
        twelve hex is ~1 in 2 x 10^7. It asserts the property — no id repeats — rather than the literal
        width, so widening the suffix again keeps passing and narrowing it fails loudly."""
        ids = [pc.new_check_id(NOW) for _ in range(5000)]
        self.assertEqual(len(set(ids)), len(ids))


class Reading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _write_job(self.tmp, "j1")

    def test_absent_file_reads_empty(self):
        self.assertEqual(pc.read_checks(self.tmp), [])
        self.assertEqual(pc.open_checks(self.tmp), [])

    def test_a_malformed_line_does_not_take_the_reader_down(self):
        _record(self.tmp, "j1")
        with open(pc.checks_path(self.tmp), "a", encoding="utf-8") as fh:
            fh.write("{ half a row\n[]\nnull\n")
        _record(self.tmp, "j1")
        self.assertEqual(len(pc.read_checks(self.tmp)), 2)
        self.assertEqual(pc.stats(self.tmp)["rows"], 2)

    def test_a_torn_final_line_costs_that_row_and_not_the_file(self):
        """The shape a crash mid-append leaves: the last line has no newline and stops mid-object."""
        _record(self.tmp, "j1")
        with open(pc.checks_path(self.tmp), "a", encoding="utf-8") as fh:
            fh.write('{"schema": "seneschal.pending-check/1", "id": "tor')
        self.assertEqual(len(pc.read_checks(self.tmp)), 1)

    def test_limit_keeps_the_newest(self):
        for i in range(3):
            pc.record_check(self.tmp, job_id="j1", claim=f"claim {i}", falsifier=FALSIFIER, env={},
                            now=NOW + timedelta(minutes=i))
        self.assertEqual([r["claim"] for r in pc.read_checks(self.tmp, limit=2)],
                         ["claim 1", "claim 2"])

    def test_since_filters_by_stamp(self):
        pc.record_check(self.tmp, job_id="j1", claim="old", falsifier=FALSIFIER, env={},
                        now=NOW - timedelta(days=3))
        pc.record_check(self.tmp, job_id="j1", claim="new", falsifier=FALSIFIER, env={}, now=NOW)
        rows = pc.read_checks(self.tmp, since=NOW - timedelta(days=1))
        self.assertEqual([r["claim"] for r in rows], ["new"])

    def test_a_stray_resolution_is_kept_rather_than_dropped(self):
        """A resolution naming no open row is a real inconsistency. Hiding it would make the file
        agree with itself by losing evidence, which is this spec's own subject."""
        with open(pc.checks_path(self.tmp), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema": pc.SCHEMA, "id": "r1", "check_id": "ghost",
                                 "at": pc._stamp(NOW), "state": "resolved"}) + "\n")
        folded = pc.by_check(self.tmp)
        self.assertIn("ghost", folded)
        self.assertIsNone(folded["ghost"]["open"])


class AppendOnly(unittest.TestCase):
    """Append-only, never truncate-written — this directory's house rule, because `state/` is
    gitignored and exists nowhere else. `prune` is the one rewrite, and it is build-then-`os.replace`."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _write_job(self.tmp, "j1")

    def test_concurrent_writers_lose_nothing(self):
        errors = []

        def writer(n):
            try:
                for i in range(20):
                    pc.record_check(self.tmp, job_id="j1", claim=f"{n}-{i}", falsifier=FALSIFIER,
                                    env={})
            except Exception as exc:  # noqa: BLE001 — a raise here would be invariant 1 breaking
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        rows = pc.read_checks(self.tmp)
        self.assertEqual(len(rows), 160)
        self.assertEqual(len({r["claim"] for r in rows}), 160)
        self.assertEqual(len({r["check_id"] for r in rows}), 160)

    def _raw(self):
        with open(pc.checks_path(self.tmp), encoding="utf-8") as fh:
            return fh.read()

    def test_a_second_write_never_rewrites_the_first(self):
        _record(self.tmp, "j1", claim="first")
        first_line = self._raw().splitlines()[0]
        _record(self.tmp, "j1", claim="second")
        self.assertEqual(self._raw().splitlines()[0], first_line)

    def test_prune_leaves_the_old_file_intact_when_the_rewrite_fails(self):
        pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER, env={},
                        check_id="old", now=NOW - timedelta(days=200))
        pc.record_resolution(self.tmp, check_id="old", now=NOW - timedelta(days=199))
        before = self._raw()
        with mock.patch("os.replace", side_effect=OSError("busy")):
            self.assertEqual(pc.prune(self.tmp, days=90, now=NOW), 0)
        self.assertEqual(self._raw(), before)


class Stats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_cold_log_reads_zero_and_abstains_from_a_rate(self):
        s = pc.stats(self.tmp)
        self.assertEqual((s["rows"], s["checks"], s["open"], s["resolved"]), (0, 0, 0, 0))
        self.assertIsNone(s["checks_per_day"])
        self.assertIsNone(s["days_covered"])

    def test_a_per_day_rate_abstains_until_two_distinct_days_exist(self):
        _write_job(self.tmp, "j1")
        pc.record_check(self.tmp, job_id="j1", claim="a", falsifier=FALSIFIER, env={}, now=NOW)
        self.assertIsNone(pc.stats(self.tmp)["checks_per_day"])
        pc.record_check(self.tmp, job_id="j1", claim="b", falsifier=FALSIFIER, env={},
                        now=NOW + timedelta(days=1))
        self.assertEqual(pc.stats(self.tmp)["checks_per_day"], 1.0)

    def test_an_open_check_whose_job_already_finished_is_the_number_that_matters(self):
        """*"How many are never reconciled by anyone"* (§6.1), from the side this file can see. NOT
        phase 1's sweep, which asks the inverse — completed jobs carrying no row at all."""
        _write_job(self.tmp, "running", status="running")
        _write_job(self.tmp, "finished", status="done")
        _record(self.tmp, "running")
        _record(self.tmp, "finished")
        s = pc.stats(self.tmp)
        self.assertEqual(s["open"], 2)
        self.assertEqual(s["open_with_terminal_job"], 1)
        self.assertEqual(s["open_job_unknown"], 0)

    def test_a_pruned_away_job_reads_unknown_rather_than_reconciled(self):
        """`state/jobs/` prunes at 14 days and this file keeps an open check forever, so the job
        record WILL go missing. Counting that as reconciled would be the cheerful reading of a gap."""
        _write_job(self.tmp, "j1")
        _record(self.tmp, "j1")
        os.remove(os.path.join(self.tmp, "jobs", "j1.json"))
        s = pc.stats(self.tmp)
        self.assertEqual(s["open_job_unknown"], 1)
        self.assertEqual(s["open_with_terminal_job"], 0)

    def test_durations_are_reported_for_both_halves(self):
        _write_job(self.tmp, "j1")
        pc.record_check(self.tmp, job_id="j1", claim=CLAIM, falsifier=FALSIFIER, env={},
                        check_id="c1", now=NOW)
        pc.record_resolution(self.tmp, check_id="c1", outcome="refuted",
                             now=NOW + timedelta(minutes=70))
        pc.record_check(self.tmp, job_id="j1", claim="still open", falsifier=FALSIFIER, env={},
                        check_id="c2", now=NOW)
        s = pc.stats(self.tmp, now=NOW + timedelta(minutes=30))
        self.assertEqual(s["median_resolution_sec"], 4200)
        self.assertEqual(s["median_open_sec"], 1800)
        self.assertEqual(s["sessions"], 1)


class CLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _write_job(self.tmp, "j1")

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = pc.main(["--state-dir", self.tmp, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_record_prints_the_check_id_it_wrote(self):
        code, out, _ = self._run("record", "--job-id", "j1", "--claim", CLAIM,
                                 "--falsifier", FALSIFIER)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(pc.read_checks(self.tmp)[0]["check_id"], payload["check_id"])

    def test_a_refusal_exits_2_and_says_why_on_stderr(self):
        """Exit 2 keeps a refusal distinguishable from a disk failure (1) — `ack.py`'s convention,
        for the same reason: a partial success has to stay legible."""
        code, out, err = self._run("record", "--job-id", "not-a-job", "--claim", CLAIM,
                                   "--falsifier", FALSIFIER)
        self.assertEqual(code, 2)
        self.assertIn("refused", err)
        self.assertFalse(json.loads(out)["ok"])
        self.assertEqual(pc.read_checks(self.tmp), [])

    def test_a_missing_job_id_is_argparse_s_refusal_not_a_silent_row(self):
        with mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                pc.main(["--state-dir", self.tmp, "record", "--claim", CLAIM,
                         "--falsifier", FALSIFIER])
        self.assertEqual(pc.read_checks(self.tmp), [])

    def test_artifact_is_repeatable(self):
        self._run("record", "--job-id", "j1", "--claim", CLAIM, "--falsifier", FALSIFIER,
                  "--artifact", ARTIFACT, "--artifact", "seneschal/state/run-log.md")
        self.assertEqual(pc.read_checks(self.tmp)[0]["claim_ref"]["artifacts"],
                         [ARTIFACT, "seneschal/state/run-log.md"])

    def test_resolve_open_tail_stats_and_prune_all_round_trip(self):
        _, out, _ = self._run("record", "--job-id", "j1", "--claim", CLAIM,
                              "--falsifier", FALSIFIER)
        check_id = json.loads(out)["check_id"]

        _, out, _ = self._run("open")
        self.assertEqual(json.loads(out)["check_id"], check_id)

        code, out, _ = self._run("resolve", check_id, "--outcome", "refuted", "--note", "it exists")
        self.assertEqual(code, 0)

        _, out, _ = self._run("open")
        self.assertEqual(out, "")

        _, out, _ = self._run("tail", "--limit", "1")
        self.assertEqual(json.loads(out)["outcome"], "refuted")

        _, out, _ = self._run("stats")
        self.assertEqual(json.loads(out)["resolved"], 1)

        _, out, _ = self._run("prune", "--days", "90")
        self.assertEqual(json.loads(out)["dropped"], 0)

    def test_resolving_an_unknown_check_exits_1_and_names_the_reader(self):
        code, _, err = self._run("resolve", "nope", "--outcome", "refuted")
        self.assertEqual(code, 1)
        self.assertIn("pending_checks.py open", err)


if __name__ == "__main__":
    unittest.main()
