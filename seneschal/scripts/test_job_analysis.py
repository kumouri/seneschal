#!/usr/bin/env python3
"""Tests for job_analysis.py — the `seneschal.job-leads/1` artifact schema, its STRICT validator, and the
two-scalar delegation.

**What this file mostly exists to hold down: the schema is the product.** "Point, don't diagnose" is
not a request made in a prompt, it is a shape a diagnosis cannot fit into — so the tests that matter
are the rejections. An artifact carrying `cause` must be INVALID rather than quietly trimmed, because
a silently-dropped key teaches the analyst that the rail is decorative. Equally load-bearing in the
other direction: `"leads": []` is a CORRECT answer, and a validator that read it as failure would
push an analyst toward manufacturing a lead — which costs someone real time to test.

The delegation is pinned by its SIGNATURE, not just its output: two scalar parameters is the entire
enforcement of the payload contract, and a test that only checked the rendered string would go green
on a `build_delegation(path, goal, transcript)` that leaks the working agent's reasoning.

No spend, no network, no spawn: everything here is a pure function.

Run: python -m unittest discover -s seneschal/scripts -p "test_job_analysis.py"
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import job_analysis as ja  # noqa: E402
import jobs  # noqa: E402


def artifact(**over) -> dict:
    """A minimal VALID artifact. Every test below is a single deliberate deviation from it, so a
    failure names exactly one rule."""
    base = {
        "schema": ja.LEADS_SCHEMA,
        "job_id": "20260730-104247-4312",
        "analyst": "warm",
        "produced_at": "2026-07-30T14:02:11+02:00",
        "contamination_present": False,
        "leads": [
            {"rank": 1, "location": "seneschal/scripts/rag_projects.py:212",
             "evidence": "the succeeding run logged 41 'indexed' lines here; this run logged none",
             "basis": ja.BASIS_ESTABLISHED,
             "cited": ["state/jobs/20260730-104247-4312.log"]},
        ],
        "notes": "",
    }
    base.update(over)
    return base


class ValidatorTests(unittest.TestCase):

    def test_the_reference_artifact_is_valid(self):
        self.assertEqual(ja.validate_artifact(artifact()), [])
        self.assertIs(ja.require_valid(artifact())["schema"], ja.LEADS_SCHEMA)

    # ------------------------------------------------------------------ empty is valid

    def test_an_empty_lead_list_is_a_valid_answer(self):
        """`"leads": []` is correct when nothing stands out. A validator that rejected it would push
        the analyst to manufacture a lead — strictly worse than silence, because a fabricated lead
        costs someone real time to test. The mirror of `archons.md`'s "a zero exit from `demiurge
        delegate` is not success": judge the artifact, not the exit code."""
        self.assertEqual(ja.validate_artifact(artifact(leads=[])), [])

    # ------------------------------------------------------------------ unknown-key REJECTION

    def test_a_diagnosis_makes_the_artifact_invalid_rather_than_being_dropped(self):
        for key in ("cause", "fix", "patch", "root_cause", "confidence"):
            with self.subTest(key=key):
                errors = ja.validate_artifact(artifact(**{key: "the loop exits early"}))
                self.assertTrue(any(key in e for e in errors), errors)
        with self.assertRaises(ja.InvalidArtifact):
            ja.require_valid(artifact(cause="the loop exits early"))

    def test_an_unknown_key_inside_a_lead_is_rejected_too(self):
        lead = dict(artifact()["leads"][0], cause="off-by-one")
        errors = ja.validate_artifact(artifact(leads=[lead]))
        self.assertTrue(any("cause" in e for e in errors), errors)

    def test_the_schema_has_no_place_to_put_a_diagnosis(self):
        """Belt and braces on the mechanism itself: the absence of the field IS the rail, so a future
        edit that adds one has to argue with this test."""
        for forbidden in ("cause", "fix", "patch", "root_cause", "confidence"):
            self.assertNotIn(forbidden, ja.ARTIFACT_KEYS)
            self.assertNotIn(forbidden, ja.LEAD_KEYS)

    # ------------------------------------------------------------------ required fields

    def test_every_required_top_level_field_is_required(self):
        for key in ja.ARTIFACT_REQUIRED:
            with self.subTest(key=key):
                bad = artifact()
                bad.pop(key)
                self.assertTrue(ja.validate_artifact(bad), f"{key} was not required")

    def test_contamination_present_must_be_a_real_boolean(self):
        """Mandatory so the routing can COUNT contamination events instead of hoping they don't
        happen — which a defaulted-away field could never do."""
        self.assertTrue(ja.validate_artifact(artifact(contamination_present="no")))
        self.assertEqual(ja.validate_artifact(artifact(contamination_present=True)), [])

    def test_a_wrong_schema_string_is_rejected(self):
        self.assertTrue(ja.validate_artifact(artifact(schema="seneschal.job-leads/2")))

    def test_produced_at_must_parse(self):
        self.assertTrue(ja.validate_artifact(artifact(produced_at="yesterday-ish")))
        self.assertEqual(ja.validate_artifact(artifact(produced_at="2026-07-30T19:02:11Z")), [])

    def test_a_non_object_is_not_an_artifact(self):
        for junk in (None, [], "leads", 7):
            with self.subTest(junk=junk):
                self.assertTrue(ja.validate_artifact(junk))

    # ------------------------------------------------------------------ leads

    def test_an_established_lead_must_cite_what_was_read(self):
        """"I verified this" is only meaningful with what was read attached."""
        lead = dict(artifact()["leads"][0])
        lead.pop("cited")
        self.assertTrue(any("cited" in e for e in ja.validate_artifact(artifact(leads=[lead]))))
        lead["cited"] = []
        self.assertTrue(any("cited" in e for e in ja.validate_artifact(artifact(leads=[lead]))))

    def test_a_conjecture_lead_needs_no_citation(self):
        lead = {"rank": 1, "location": "seneschal/scripts/rag_projects.py:188",
                "evidence": "the roots read here is the loop's only earlier exit",
                "basis": ja.BASIS_CONJECTURE}
        self.assertEqual(ja.validate_artifact(artifact(leads=[lead])), [])

    def test_basis_is_a_closed_enum(self):
        lead = dict(artifact()["leads"][0], basis="pretty sure")
        self.assertTrue(any("basis" in e for e in ja.validate_artifact(artifact(leads=[lead]))))

    def test_established_leads_must_sort_before_conjecture_ones(self):
        """Enforced, not requested — a verified lead buried under a guessed one is a ranked list that
        lies about its own ranking."""
        established = dict(artifact()["leads"][0], rank=2)
        conjecture = {"rank": 1, "location": "a.py:1", "evidence": "worth a look",
                      "basis": ja.BASIS_CONJECTURE}
        errors = ja.validate_artifact(artifact(leads=[conjecture, established]))
        self.assertTrue(any("sort before" in e for e in errors), errors)
        # The same two, the right way round, are fine.
        self.assertEqual(
            ja.validate_artifact(artifact(leads=[dict(established, rank=1),
                                                 dict(conjecture, rank=2)])), [])

    def test_ranks_must_be_distinct_positive_integers(self):
        first = artifact()["leads"][0]
        self.assertTrue(ja.validate_artifact(artifact(leads=[dict(first, rank=0)])))
        self.assertTrue(ja.validate_artifact(artifact(leads=[dict(first, rank="1")])))
        self.assertTrue(any("distinct" in e for e in ja.validate_artifact(
            artifact(leads=[first, dict(first, cited=["x"])]))))

    def test_leads_must_be_a_list_and_junk_inside_it_is_caught(self):
        self.assertTrue(ja.validate_artifact(artifact(leads={"rank": 1})))
        self.assertTrue(ja.validate_artifact(artifact(leads=["a string"])))

    def test_every_error_is_reported_not_just_the_first(self):
        """A model fixing one violation per round trip would burn a delegation each time."""
        bad = artifact(cause="x", contamination_present="maybe")
        bad.pop("analyst")
        self.assertGreaterEqual(len(ja.validate_artifact(bad)), 3)


class DelegationTests(unittest.TestCase):
    """The payload contract: exactly two things reach the analyst — the artifact path and one
    goal line."""

    def test_the_signature_is_the_enforcement(self):
        """Two parameters, both scalars. This is the test that actually holds the contract: a
        `build_delegation(path, goal, transcript)` would go green on every string assertion below
        while leaking exactly what the design exists to withhold."""
        params = list(inspect.signature(ja.build_delegation).parameters.values())
        self.assertEqual([p.name for p in params], ["artifact_path", "goal"])
        self.assertTrue(all(p.kind is p.POSITIONAL_OR_KEYWORD for p in params))
        self.assertTrue(all(p.default is p.empty for p in params))

    def test_it_carries_the_path_and_the_goal(self):
        text = ja.build_delegation(r"C:\state\jobs\20260730-104247-4312.log",
                                   "find out why the nightly RAG refresh writes zero project docs")
        self.assertIn("20260730-104247-4312.log", text)
        self.assertIn("why the nightly RAG refresh writes zero project docs", text)
        self.assertIn(ja.LEADS_SCHEMA, text)

    def test_it_states_the_rails_the_schema_enforces(self):
        text = ja.build_delegation("x.log", "g")
        for phrase in ("POINT, NOT TO DIAGNOSE", "contamination_present",
                       "EMPTY LIST IS A CORRECT ANSWER", "One pass"):
            self.assertIn(phrase, text)

    def test_the_goal_is_collapsed_to_one_line(self):
        """The same single cap `start_job` applies — a four-paragraph hypothesis pasted into --goal
        cannot become four paragraphs of context in the delegation."""
        text = ja.build_delegation("x.log", "line one\nline two\n" + "y" * 400)
        self.assertNotIn("line one\nline two", text)
        self.assertIn("line one line two", text)

    def test_the_template_is_fixed_and_the_two_scalars_are_all_that_varies(self):
        """One fixed template. Two builds differing only in their inputs must differ only where those
        inputs land — so nothing else can be smuggled in through the environment, a module global, or
        the job record. (The word "hypothesis" DOES appear, in the contamination rail; that is the
        analyst being told to ignore one, which is the opposite of being handed one.)"""
        a = ja.build_delegation("AAA-PATH", "BBB-GOAL")
        b = ja.build_delegation("CCC-PATH", "DDD-GOAL")
        self.assertEqual(a.replace("AAA-PATH", "«p»").replace("BBB-GOAL", "«g»"),
                         b.replace("CCC-PATH", "«p»").replace("DDD-GOAL", "«g»"))
        self.assertEqual(a.count("AAA-PATH"), 1)
        self.assertEqual(a.count("BBB-GOAL"), 1)


class ExtractJsonTests(unittest.TestCase):
    """Tolerant HERE, strict in the validator: a code fence is a formatting quirk, a `cause` field is
    a contract violation, and conflating the two either rejects good artifacts or accepts bad ones."""

    def test_a_bare_object(self):
        self.assertEqual(ja.extract_json('{"a": 1}'), {"a": 1})

    def test_a_fenced_object_with_prose_either_side(self):
        reply = 'Sure — here you go:\n```json\n{"schema": "x", "leads": []}\n```\nHope that helps.'
        self.assertEqual(ja.extract_json(reply), {"schema": "x", "leads": []})

    def test_braces_inside_strings_do_not_confuse_it(self):
        self.assertEqual(ja.extract_json('{"evidence": "the dict {a: 1} at line 4"}'),
                         {"evidence": "the dict {a: 1} at line 4"})

    def test_prose_with_no_object_is_none(self):
        for junk in ("no json here", "", None, "{ not json at all"):
            with self.subTest(junk=junk):
                self.assertIsNone(ja.extract_json(junk))

    def test_a_leading_non_object_brace_run_does_not_stop_the_search(self):
        self.assertEqual(ja.extract_json('{bad} then {"good": true}'), {"good": True})


class _FakeRun:
    """A `subprocess.run` seam: returns whatever the analyst is pretending to have said. The ONLY
    thing faked — the delegation, the JSON extraction, the validation and the staging are all real,
    so no test here can spend, spawn or reach the network."""

    def __init__(self, stdout="", returncode=0, stderr="", boom=None):
        self.stdout, self.returncode, self.stderr, self.boom = stdout, returncode, stderr, boom
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.boom:
            raise self.boom
        return self


class RunAnalysisTests(unittest.TestCase):
    """Phase 2 — the executor. `run_analysis`'s exit code IS the outcome the analysis job's own
    completion push reports, so each code is pinned."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(jobs.jobs_dir(self.state), exist_ok=True)
        self.job = self._write_job("the-job", goal="why does the refresh write zero docs")

    def _write_job(self, job_id, goal=None, status="failed"):
        rec = {"schema": jobs.SCHEMA, "id": job_id, "title": "a job", "argv": ["x"],
               "status": status, "log_path": jobs.log_path(self.state, job_id),
               "origin": {"session_id": "s-1", "goal": goal} if goal else {},
               "attempts": []}
        jobs.save_job(self.state, rec)
        with open(rec["log_path"], "w", encoding="utf-8") as fh:
            fh.write("boom\n")
        return rec

    def test_a_valid_artifact_is_staged_and_exits_zero(self):
        run = _FakeRun(stdout=json.dumps(artifact(job_id="the-job")))
        self.assertEqual(ja.run_analysis(self.state, "the-job", runner=run,
                                         out=open(os.devnull, "w")), 0)
        staged = ja.load_analysis(self.state, "the-job")
        self.assertEqual(staged["job_id"], "the-job")
        self.assertTrue(os.path.exists(jobs.analysis_path(self.state, "the-job")))

    def test_an_empty_lead_list_is_success_not_failure(self):
        """"Nothing stood out" is a correct answer. An executor that treated it as failure would
        push the analyst toward manufacturing a lead."""
        run = _FakeRun(stdout=json.dumps(artifact(job_id="the-job", leads=[])))
        self.assertEqual(ja.run_analysis(self.state, "the-job", runner=run,
                                         out=open(os.devnull, "w")), 0)
        self.assertEqual(ja.load_analysis(self.state, "the-job")["leads"], [])

    def test_a_diagnosis_is_a_FAILED_delegation_and_never_reaches_disk(self):
        """Judge the artifact, not the exit code: the analyst exited 0 and still failed."""
        run = _FakeRun(stdout=json.dumps(artifact(job_id="the-job", cause="off-by-one")))
        self.assertEqual(ja.run_analysis(self.state, "the-job", runner=run), 4)
        self.assertIsNone(ja.load_analysis(self.state, "the-job"))
        self.assertFalse(os.path.exists(jobs.analysis_path(self.state, "the-job")))

    def test_prose_is_not_an_artifact(self):
        run = _FakeRun(stdout="I had a look and I think the loop exits early.")
        self.assertEqual(ja.run_analysis(self.state, "the-job", runner=run), 4)

    def test_an_analyst_that_cannot_run_exits_five(self):
        self.assertEqual(ja.run_analysis(self.state, "the-job",
                                         runner=_FakeRun(boom=OSError("no claude"))), 5)
        self.assertEqual(ja.run_analysis(self.state, "the-job",
                                         runner=_FakeRun(stdout="", returncode=1,
                                                         stderr="not logged in")), 5)

    def test_an_unknown_job_or_a_missing_goal_exits_three(self):
        self.assertEqual(ja.run_analysis(self.state, "nope", runner=_FakeRun()), 3)
        self._write_job("goalless")
        self.assertEqual(ja.run_analysis(self.state, "goalless", runner=_FakeRun()), 3)

    def test_the_goal_falls_back_to_the_one_the_job_was_started_with(self):
        run = _FakeRun(stdout=json.dumps(artifact(job_id="the-job")))
        ja.run_analysis(self.state, "the-job", runner=run, out=open(os.devnull, "w"))
        prompt = run.calls[0][0][-1]
        self.assertIn("why does the refresh write zero docs", prompt)

    def test_the_analyst_is_handed_a_PATH_not_the_logs_contents(self):
        """A path, because the analyst has Read/Grep/Glob and a path is what lets it go find
        the nearest SUCCEEDING run itself."""
        run = _FakeRun(stdout=json.dumps(artifact(job_id="the-job")))
        ja.run_analysis(self.state, "the-job", runner=run, out=open(os.devnull, "w"))
        prompt = run.calls[0][0][-1]
        self.assertIn(self.job["log_path"], prompt)
        self.assertNotIn("boom", prompt)

    def test_the_analyst_call_is_subscription_billed(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-nope"}):
            run = _FakeRun(stdout=json.dumps(artifact(job_id="the-job")))
            ja.run_analysis(self.state, "the-job", runner=run, out=open(os.devnull, "w"))
        argv, kwargs = run.calls[0]
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertNotIn("ANTHROPIC_API_KEY", sorted(kwargs["env"].keys()))

    def test_an_unadmitted_analyst_is_refused_loudly_not_silently_downgraded(self):
        """The admission seam. An analyst that isn't admitted must fail, rather than quietly
        becoming the warm session and reporting a posture it never had."""
        self.assertEqual(ja.ANALYSTS, ("warm",))
        with self.assertRaises(ValueError):
            ja.analyst_argv("p", analyst="unadmitted-archon")
        self.assertEqual(ja.run_analysis(self.state, "the-job", analyst="unadmitted-archon",
                                         runner=_FakeRun()), 3)


class CliTests(unittest.TestCase):

    def test_validate_exits_nonzero_on_a_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.json")
            bad = os.path.join(tmp, "bad.json")
            with open(good, "w", encoding="utf-8") as fh:
                json.dump(artifact(), fh)
            with open(bad, "w", encoding="utf-8") as fh:
                json.dump(artifact(cause="the loop exits early"), fh)
            self.assertEqual(ja.main(["validate", good]), 0)
            self.assertEqual(ja.main(["validate", bad]), 1)
            self.assertEqual(ja.main(["validate", os.path.join(tmp, "absent.json")]), 2)

    def test_delegation_prints_the_whole_analyst_input(self):
        self.assertEqual(ja.main(["delegation", "--artifact", "a.log", "--goal", "g"]), 0)


if __name__ == "__main__":
    unittest.main()
