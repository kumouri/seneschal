#!/usr/bin/env python3
"""Tests for `job_pr_draft.py` — a PR opened by a still-running job is a draft until it ends.

Every `git`/`gh` call is faked; no test reaches the network or a real `state/jobs/` directory."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import job_pr_draft  # noqa: E402


def runner(mapping=None, default=(0, "", "")):
    """A tuple-returning fake, keyed by the joined argv (substring match), the same shape every
    `_run` seam in this family uses."""
    mapping = mapping or {}
    calls = []

    def run(argv):
        calls.append(argv)
        joined = " ".join(argv)
        for needle, result in mapping.items():
            if needle in joined:
                return result
        return default
    run.calls = calls
    return run


class ListRunningWorktreeBranchesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_maps_a_running_worktree_jobs_branch(self):
        wt = os.path.join(self.dir, "wt1")
        os.makedirs(wt)
        run = runner({"rev-parse --abbrev-ref HEAD": (0, "feat/voice-mvp\n", "")})
        branches = job_pr_draft.list_running_worktree_branches(
            self.dir, runner=run,
            jobs_lister=lambda sd: [{"id": "job-1", "worktree": {"path": wt}}])
        self.assertEqual(branches, {"feat/voice-mvp": "job-1"})

    def test_a_detached_worktree_contributes_nothing(self):
        wt = os.path.join(self.dir, "wt1")
        os.makedirs(wt)
        run = runner({"rev-parse --abbrev-ref HEAD": (0, "HEAD\n", "")})
        branches = job_pr_draft.list_running_worktree_branches(
            self.dir, runner=run,
            jobs_lister=lambda sd: [{"id": "job-1", "worktree": {"path": wt}}])
        self.assertEqual(branches, {})

    def test_a_worktree_path_that_no_longer_exists_is_skipped(self):
        run = runner({"rev-parse --abbrev-ref HEAD": (0, "feat/x\n", "")})
        branches = job_pr_draft.list_running_worktree_branches(
            self.dir, runner=run,
            jobs_lister=lambda sd: [{"id": "job-1",
                                     "worktree": {"path": os.path.join(self.dir, "gone")}}])
        self.assertEqual(branches, {})

    def test_a_job_with_no_worktree_is_skipped(self):
        branches = job_pr_draft.list_running_worktree_branches(
            self.dir, jobs_lister=lambda sd: [{"id": "job-1"}])
        self.assertEqual(branches, {})

    def test_a_broken_jobs_lister_fails_open_to_empty(self):
        def raiser(sd):
            raise RuntimeError("boom")
        self.assertEqual(
            job_pr_draft.list_running_worktree_branches(self.dir, jobs_lister=raiser), {})

    def test_a_broken_git_read_fails_open_for_that_job_only(self):
        wt1 = os.path.join(self.dir, "wt1")
        wt2 = os.path.join(self.dir, "wt2")
        os.makedirs(wt1)
        os.makedirs(wt2)

        def run(argv):
            if wt1 in argv:
                raise RuntimeError("git is missing")
            return 0, "feat/two\n", ""
        branches = job_pr_draft.list_running_worktree_branches(
            self.dir, runner=run,
            jobs_lister=lambda sd: [{"id": "job-1", "worktree": {"path": wt1}},
                                    {"id": "job-2", "worktree": {"path": wt2}}])
        self.assertEqual(branches, {"feat/two": "job-2"})

    def test_an_empty_state_dir_is_empty(self):
        self.assertEqual(job_pr_draft.list_running_worktree_branches(""), {})


class InflightJobForTest(unittest.TestCase):
    def test_matches_the_branch(self):
        row = {"headRefName": "feat/x"}
        self.assertEqual(job_pr_draft.inflight_job_for(row, {"feat/x": "job-1"}), "job-1")

    def test_no_match_returns_none(self):
        row = {"headRefName": "feat/x"}
        self.assertIsNone(job_pr_draft.inflight_job_for(row, {"feat/y": "job-1"}))

    def test_malformed_inputs_return_none_rather_than_raise(self):
        self.assertIsNone(job_pr_draft.inflight_job_for(None, {}))
        self.assertIsNone(job_pr_draft.inflight_job_for({}, {}))
        self.assertIsNone(job_pr_draft.inflight_job_for({"headRefName": 7}, {}))
        self.assertIsNone(job_pr_draft.inflight_job_for({"headRefName": "x"}, None))


class DraftAndRecordTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_a_successful_draft_is_recorded(self):
        run = runner({"pr ready 860": (0, "", "")})
        ok = job_pr_draft.draft_and_record(
            self.dir, repo="example/repo", pr=860, branch="feat/x",
            job_id="job-1", runner=run)
        self.assertTrue(ok)
        entries = job_pr_draft.entries_for_job(self.dir, "job-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["pr"], 860)
        self.assertEqual(entries[0]["branch"], "feat/x")
        self.assertIn("--undo", run.calls[0])

    def test_a_failed_gh_call_records_nothing(self):
        run = runner({"pr ready 860": (1, "", "not authorized")})
        ok = job_pr_draft.draft_and_record(
            self.dir, repo="example/repo", pr=860, branch="feat/x",
            job_id="job-1", runner=run)
        self.assertFalse(ok)
        self.assertEqual(job_pr_draft.entries_for_job(self.dir, "job-1"), [])

    def test_a_raising_runner_is_fail_open(self):
        def raiser(argv):
            raise RuntimeError("no gh on PATH")
        ok = job_pr_draft.draft_and_record(
            self.dir, repo="example/repo", pr=860, branch="feat/x",
            job_id="job-1", runner=raiser)
        self.assertFalse(ok)


class ResolveForJobTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        run = runner({"pr ready 860 --repo": (0, "", "")})
        self.assertTrue(job_pr_draft.draft_and_record(
            self.dir, repo="example/repo", pr=860, branch="feat/x",
            job_id="job-1", runner=run))

    def test_a_successful_job_marks_the_pr_ready_and_clears_the_entry(self):
        run = runner({"pr ready 860": (0, "", "")})
        results = job_pr_draft.resolve_for_job(self.dir, "job-1", success=True, runner=run)
        self.assertEqual(results, [{"repo": "example/repo", "pr": 860,
                                    "action": "readied"}])
        self.assertEqual(job_pr_draft.entries_for_job(self.dir, "job-1"), [])
        ready_calls = [c for c in run.calls if "--undo" not in c]
        self.assertTrue(any("ready" in c for c in ready_calls))

    def test_a_failed_job_leaves_the_pr_a_draft_and_clears_the_entry(self):
        run = runner()
        results = job_pr_draft.resolve_for_job(self.dir, "job-1", success=False, runner=run)
        self.assertEqual(results, [{"repo": "example/repo", "pr": 860,
                                    "action": "left-draft"}])
        self.assertEqual(job_pr_draft.entries_for_job(self.dir, "job-1"), [])
        # never called gh at all for the leave-draft path
        self.assertEqual(run.calls, [])

    def test_a_gh_failure_on_the_ready_call_leaves_the_entry_for_a_retry(self):
        run = runner({"pr ready 860": (1, "", "rate limited")})
        results = job_pr_draft.resolve_for_job(self.dir, "job-1", success=True, runner=run)
        self.assertEqual(results, [{"repo": "example/repo", "pr": 860,
                                    "action": "ready-failed", "detail": "rate limited"}])
        # the entry survives, so a later pass (or a manual `gh pr ready`) can still resolve it
        self.assertEqual(len(job_pr_draft.entries_for_job(self.dir, "job-1")), 1)

    def test_a_raising_runner_reports_ready_failed_and_keeps_the_entry(self):
        def raiser(argv):
            raise RuntimeError("no gh on PATH")
        results = job_pr_draft.resolve_for_job(self.dir, "job-1", success=True, runner=raiser)
        self.assertEqual(results[0]["action"], "ready-failed")
        self.assertEqual(len(job_pr_draft.entries_for_job(self.dir, "job-1")), 1)

    def test_a_job_with_no_entries_returns_nothing_and_touches_no_gh(self):
        run = runner()
        results = job_pr_draft.resolve_for_job(self.dir, "job-does-not-exist", success=True,
                                               runner=run)
        self.assertEqual(results, [])
        self.assertEqual(run.calls, [])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_load_store_on_a_missing_file_is_empty(self):
        self.assertEqual(job_pr_draft.load_store(self.dir),
                         {"schema": job_pr_draft.STORE_SCHEMA, "entries": {}})

    def test_load_store_on_a_corrupt_file_is_empty(self):
        path = job_pr_draft.store_path(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json")
        self.assertEqual(job_pr_draft.load_store(self.dir),
                         {"schema": job_pr_draft.STORE_SCHEMA, "entries": {}})

    def test_load_store_on_a_wrong_shape_is_empty(self):
        path = job_pr_draft.store_path(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(["not", "a", "dict-of-entries"], fh)
        self.assertEqual(job_pr_draft.load_store(self.dir),
                         {"schema": job_pr_draft.STORE_SCHEMA, "entries": {}})


if __name__ == "__main__":
    unittest.main()
