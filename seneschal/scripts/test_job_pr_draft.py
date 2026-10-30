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
import tmproot  # noqa: E402  — rmtree that clears git's read-only objects


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
        self.addCleanup(tmproot.rmtree, self.dir)

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
        self.addCleanup(tmproot.rmtree, self.dir)

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
        self.addCleanup(tmproot.rmtree, self.dir)
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
        self.addCleanup(tmproot.rmtree, self.dir)

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


class RebaseInProgressTest(unittest.TestCase):
    """§5A.10: a worktree stopped mid-rebase is detached, but it is still ON the branch it
    is rewriting. A job sat mid-rebase of feat/example-branch for 47 s and the
    watcher, reading `HEAD`, launched a repair onto the same branch."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(tmproot.rmtree, self.dir)
        self.wt = os.path.join(self.dir, "wt1")
        os.makedirs(self.wt)

    def _rebase_dir(self, name, head_name):
        d = os.path.join(self.dir, "gitdir", name)
        os.makedirs(d)
        with open(os.path.join(d, "head-name"), "w", encoding="utf-8") as fh:
            fh.write(head_name + "\n")
        return d

    def _branches(self, git_path_out):
        run = runner({"rev-parse --abbrev-ref HEAD": (0, "HEAD\n", ""),
                      "--git-path": (0, git_path_out, "")})
        return job_pr_draft.list_running_worktree_branches(
            self.dir, runner=run,
            jobs_lister=lambda sd: [{"id": "job-1", "worktree": {"path": self.wt}}])

    def test_a_merge_backend_rebase_maps_the_branch_being_rebased(self):
        merge = self._rebase_dir("rebase-merge", "refs/heads/feat/example-branch")
        apply_ = os.path.join(self.dir, "gitdir", "rebase-apply")  # absent
        self.assertEqual(self._branches(f"{merge}\n{apply_}\n"),
                         {"feat/example-branch": "job-1"})

    def test_an_apply_backend_rebase_is_read_too(self):
        merge = os.path.join(self.dir, "gitdir", "rebase-merge")  # absent
        apply_ = self._rebase_dir("rebase-apply", "refs/heads/feat/x")
        self.assertEqual(self._branches(f"{merge}\n{apply_}\n"), {"feat/x": "job-1"})

    def test_a_relative_git_path_resolves_against_the_worktree(self):
        d = os.path.join(self.wt, ".git", "rebase-merge")
        os.makedirs(d)
        with open(os.path.join(d, "head-name"), "w", encoding="utf-8") as fh:
            fh.write("refs/heads/feat/rel\n")
        self.assertEqual(self._branches(".git/rebase-merge\n.git/rebase-apply\n"),
                         {"feat/rel": "job-1"})

    def test_a_plain_detached_head_with_no_rebase_still_contributes_nothing(self):
        gone = os.path.join(self.dir, "gitdir")
        self.assertEqual(self._branches(f"{gone}/rebase-merge\n{gone}/rebase-apply\n"), {})

    def test_a_detached_rebase_onto_a_non_branch_ref_contributes_nothing(self):
        merge = self._rebase_dir("rebase-merge", "detached HEAD")
        self.assertEqual(self._branches(f"{merge}\n"), {})

    @unittest.skipUnless(shutil.which("git"), "git not on PATH")
    def test_against_a_real_git_rebase_stopped_on_a_conflict(self):
        import subprocess
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)

        def git(*args, cwd=repo):
            return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                                   "-c", "core.autocrlf=false", *args], cwd=cwd,
                                  capture_output=True, text=True)

        git("init", "-q", "-b", "develop")
        with open(os.path.join(repo, "f.md"), "w", encoding="utf-8") as fh:
            fh.write("base\n")
        git("add", "f.md")
        git("commit", "-q", "-m", "base")
        git("checkout", "-q", "-b", "feat/conflicted")
        with open(os.path.join(repo, "f.md"), "w", encoding="utf-8") as fh:
            fh.write("branch side\n")
        git("commit", "-q", "-am", "branch")
        git("checkout", "-q", "develop")
        with open(os.path.join(repo, "f.md"), "w", encoding="utf-8") as fh:
            fh.write("develop side\n")
        git("commit", "-q", "-am", "develop")
        wt = os.path.join(self.dir, "linked")
        git("worktree", "add", "-q", wt, "feat/conflicted")
        rebase = git("rebase", "develop", cwd=wt)
        self.assertNotEqual(rebase.returncode, 0, "the fixture must stop on a conflict")
        branches = job_pr_draft.list_running_worktree_branches(
            self.dir, jobs_lister=lambda sd: [{"id": "job-9516", "worktree": {"path": wt}}])
        self.assertEqual(branches, {"feat/conflicted": "job-9516"})


class ListPrOwnersTest(unittest.TestCase):
    """§5A.10 — which PRs an ACTIVE job says it is working on."""

    def owners(self, records):
        return job_pr_draft.list_pr_owners("state", jobs_lister=lambda sd: records)

    def test_the_structured_prs_stamp_is_read(self):
        got = self.owners([{"id": "j1", "title": "stack rebase", "prs": [880, 881]}])
        self.assertEqual(got, {880: {"job_id": "j1", "via": "prs"},
                               881: {"job_id": "j1", "via": "prs"}})

    def test_the_incident_title_is_caught_by_the_fallback(self):
        """A real-shaped title for a job that stamped nothing — a brief that forgot `--pr`."""
        got = self.owners([{"id": "20260101-010045-0001",
                            "title": "Rebase #880/#881/#882 after #879; renumber cadence lint to 2k"}])
        self.assertEqual(set(got), {879, 880, 881, 882})
        self.assertEqual(got[881], {"job_id": "20260101-010045-0001", "via": "title"})

    def test_the_goal_is_read_when_the_title_names_nothing(self):
        got = self.owners([{"id": "j1", "title": "fix CI",
                            "origin": {"goal": "PR #871 green again"}}])
        self.assertEqual(got, {871: {"job_id": "j1", "via": "title"}})

    def test_a_structured_stamp_is_the_whole_answer_for_that_job(self):
        got = self.owners([{"id": "j1", "title": "rebase #860 and #861", "prs": [861]}])
        self.assertEqual(got, {861: {"job_id": "j1", "via": "prs"}})

    def test_an_empty_or_malformed_stamp_falls_through_to_the_title(self):
        for stamp in ([], [True], ["880"], [0, -3], "880"):
            with self.subTest(stamp=stamp):
                got = self.owners([{"id": "j1", "title": "rebase #862", "prs": stamp}])
                self.assertEqual(got, {862: {"job_id": "j1", "via": "title"}})

    def test_things_that_are_not_pr_references_do_not_match(self):
        got = self.owners([{"id": "j1", "title": "port C#9 samples; strip &#8212; entities"}])
        self.assertEqual(got, {})

    def test_the_older_job_wins_a_pr_claimed_twice(self):
        got = self.owners([{"id": "20260101-020000-bbbb", "prs": [880]},
                           {"id": "20260101-010000-aaaa", "prs": [880]}])
        self.assertEqual(got[880]["job_id"], "20260101-010000-aaaa")

    def test_fail_open_on_an_unreadable_ledger(self):
        def boom(sd):
            raise OSError("no ledger")
        self.assertEqual(job_pr_draft.list_pr_owners("state", jobs_lister=boom), {})
        self.assertEqual(job_pr_draft.list_pr_owners("", jobs_lister=lambda sd: [{"id": "j", "prs": [1]}]),
                         {})

    def test_the_default_lister_reads_active_jobs_only(self):
        d = tempfile.mkdtemp()
        self.addCleanup(tmproot.rmtree, d)
        os.makedirs(os.path.join(d, "jobs"))
        for job_id, status in (("j-run", "running"), ("j-retry", "retry-pending"),
                               ("j-done", "done"), ("j-failed", "failed"), ("j-cancel", "cancelled")):
            with open(os.path.join(d, "jobs", f"{job_id}.json"), "w", encoding="utf-8") as fh:
                json.dump({"id": job_id, "status": status, "prs": [100 + len(job_id)]}, fh)
        got = job_pr_draft.list_pr_owners(d)
        self.assertEqual(sorted(v["job_id"] for v in got.values()), ["j-retry", "j-run"])


if __name__ == "__main__":
    unittest.main()
