#!/usr/bin/env python3
"""Tests for ``pr_overlap`` — which other open pull requests change the same files as this one.

Two classes here are the module's reason to exist and must not be relaxed into passing:

* **``EveryFailureSaysNothingTest``** is the polarity. An overlap block is *information*; a picker is
  a *merge that can happen*. So every failure of every kind — no `gh`, non-zero exit, a timeout,
  bytes that are not JSON, a JSON array of the wrong shape — must answer `[]` and let the picker go
  out unwarned. A test here going green on "raises" or "reports the error" is the feature costing a
  question, which is strictly worse than the problem it fixes.
* **``TheSetIsNotFilteredOnColourOrReadinessTest``** pins the two inclusions the design turns on.
  Drafts and red PRs collide exactly as green ones do — a draft's collision is deferred, not absent,
  and CI colour says nothing about which paths a diff touches. Filtering either would **silently
  under-report**, which for a warning is the worst available direction.
* **``JobOverlapFailuresSayNothingTest``** is the same polarity, for the newer half:
  ``list_inflight_jobs``/``find_job_overlaps`` must answer ``[]`` on a broken job store, a job with
  no worktree, a worktree directory that is gone, or a `git diff` that fails — never raise, never
  invent a row.

`gh` and `git` are stubbed everywhere: every test passes a `runner`, so none can reach the network or
spawn a real process. The job-overlap tests pass a `jobs_lister`, so none touch a real
`state/jobs/` directory either — a real (empty) directory is used in exactly one place, to stand in
for a worktree path that must merely *exist* on disk. Nothing here reads or writes any state
directory otherwise — the module has none, deliberately (the spec's §4.6: phase 1 records nothing,
and a cache file is a state file wearing a smaller name).

Run:  python -m unittest test_pr_overlap   (from seneschal/scripts)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pr_overlap as po

REPO = "example/repo"

#: The typical three-way collision shape: one shared ledger plus two shared prose-and-code files —
#: the files several concurrent PRs all go `CONFLICTING` on when the guard, its router and the
#: context budget are edited in parallel.
COLLISION = ["seneschal/scripts/merge_guard.py", "seneschal/scripts/CLAUDE.md",
             "seneschal/context-budget.json"]


def row(number, paths, title="a pull request", draft=False):
    return {"number": number, "title": title, "isDraft": draft,
            "files": [{"path": p} for p in paths]}


def job_rec(job_id, path=None, base="origin/develop"):
    """A `jobs.list_jobs`-shaped record, worktree included only when `path` is given — the shape
    `list_inflight_jobs` reads."""
    rec = {"id": job_id}
    if path is not None:
        rec["worktree"] = {"path": path, "base": base}
    return rec


def git_runner(out="", code=0, raises=None):
    """A `pr_overlap._run`-shaped stub for `git diff`, the twin of `lister` above."""
    def run(argv):
        if raises is not None:
            raise raises
        return code, out, ""
    return run


def lister(rows=None, code=0, out=None, raises=None):
    """A `pr_overlap._run`-shaped stub. Defaults to a healthy `gh pr list` answer."""
    def run(argv):
        if raises is not None:
            raise raises
        if out is not None:
            return code, out, ""
        return code, json.dumps(rows or []), ""
    return run


class OverlapCase(unittest.TestCase):
    """The cache is process-global, so every test starts from an empty one."""

    def setUp(self):
        po.clear_cache()
        self.addCleanup(po.clear_cache)


# --------------------------------------------------------------------------- the intersection

class TheIntersectionIsExactTest(OverlapCase):
    """`overlaps` is pure: no network, no clock, no cache. These are the arithmetic."""

    def test_it_names_the_shared_paths_and_not_the_others(self):
        rows = [row(475, COLLISION + ["seneschal/docs/CLAUDE.md"])]
        got = po.overlaps(472, COLLISION, rows)
        self.assertEqual([r["shared"] for r in got], [COLLISION])

    def test_no_shared_path_is_no_row_at_all(self):
        self.assertEqual(po.overlaps(536, ["archons/example/data.json"],
                                     [row(537, COLLISION)]), [])

    def test_the_pr_never_overlaps_itself(self):
        self.assertEqual(po.overlaps(472, COLLISION, [row(472, COLLISION)]), [])

    def test_paths_are_compared_case_sensitively(self):
        """GitHub is case-sensitive about paths. Folding case here would call `README.md` and
        `readme.md` the same file, which is a shared path that does not exist."""
        self.assertEqual(po.overlaps(1, ["README.md"], [row(2, ["readme.md"])]), [])

    def test_backslashes_are_normalised_on_both_sides(self):
        got = po.overlaps(1, ["seneschal\\scripts\\merge_guard.py"],
                          [row(2, ["seneschal/scripts/merge_guard.py"])])
        self.assertEqual(got[0]["shared"], ["seneschal/scripts/merge_guard.py"])

    def test_shared_paths_come_back_in_this_prs_own_order(self):
        """Stable and reviewable, rather than whatever order GitHub answered in — two pickers about
        the same pair must name the files the same way."""
        got = po.overlaps(1, COLLISION, [row(2, list(reversed(COLLISION)))])
        self.assertEqual(got[0]["shared"], COLLISION)

    def test_most_shared_first_then_ascending_pr(self):
        rows = [row(3, COLLISION[:1]), row(2, COLLISION), row(1, COLLISION[:1])]
        self.assertEqual([r["pr"] for r in po.overlaps(9, COLLISION, rows)], [2, 1, 3])

    def test_a_pr_with_no_paths_of_its_own_overlaps_nothing(self):
        self.assertEqual(po.overlaps(1, [], [row(2, COLLISION)]), [])

    def test_duplicate_paths_are_named_once(self):
        got = po.overlaps(1, COLLISION[:1] * 3, [row(2, COLLISION)])
        self.assertEqual(got[0]["shared"], COLLISION[:1])


class TheSetIsNotFilteredOnColourOrReadinessTest(OverlapCase):
    """The two inclusions the design turns on. See this module's docstring."""

    def test_a_draft_is_included_and_marked(self):
        got = po.overlaps(1, COLLISION, [row(2, COLLISION, draft=True)])
        self.assertEqual([r["pr"] for r in got], [2])
        self.assertTrue(got[0]["draft"])

    def test_ci_colour_is_never_read(self):
        """`statusCheckRollup` is not even requested: a red PR collides exactly as a green one does,
        so asking for the field would only invite a filter on it."""
        self.assertNotIn("statusCheckRollup", po.LIST_FIELDS)

    def test_only_open_prs_are_ever_asked_for(self):
        seen = {}

        def run(argv):
            seen["argv"] = argv
            return 0, "[]", ""
        po.list_open_prs(REPO, runner=run)
        self.assertIn("--state", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--state") + 1], "open")


# --------------------------------------------------------------------------- failing quietly

class EveryFailureSaysNothingTest(OverlapCase):
    """The polarity. See this module's docstring — a picker that fails to send because overlap
    detection broke is strictly worse than the problem."""

    FAILURES = {
        "gh missing": lister(raises=FileNotFoundError("gh")),
        "gh timed out": lister(raises=subprocess.TimeoutExpired("gh", 12)),
        "gh exploded": lister(raises=RuntimeError("boom")),
        "non-zero exit": lister(code=1, out="gh: not logged in"),
        "not json": lister(out="<html>rate limited</html>"),
        "json but not a list": lister(out='{"message": "Bad credentials"}'),
        "json null": lister(out="null"),
    }

    def test_every_way_gh_can_fail_answers_an_empty_list(self):
        for name, run in self.FAILURES.items():
            with self.subTest(failure=name):
                po.clear_cache()
                self.assertEqual(po.list_open_prs(REPO, runner=run), [])

    def test_find_never_raises_on_any_of_them(self):
        for name, run in self.FAILURES.items():
            with self.subTest(failure=name):
                po.clear_cache()
                self.assertEqual(po.find(472, REPO, COLLISION, runner=run), [])

    def test_a_missing_repo_slug_answers_nothing_rather_than_guessing(self):
        """Overlap is computed within ONE repository: `#45` in two repos is two pull requests, and a
        cross-repo path collision is not a merge conflict."""
        called = []
        po.find(472, "", COLLISION, runner=lambda argv: called.append(argv) or (0, "[]", ""))
        self.assertEqual(called, [])

    def test_rows_that_are_not_objects_are_dropped_not_fatal(self):
        out = json.dumps(["a string", 7, None, row(2, COLLISION), {"no": "number"}])
        self.assertEqual([r["pr"] for r in po.find(1, REPO, COLLISION, runner=lister(out=out))], [2])

    def test_a_row_with_unreadable_files_is_not_fatal(self):
        out = json.dumps([{"number": 2, "title": "t", "files": "not a list"},
                          {"number": 3, "title": "t", "files": [{"nope": 1}, "raw/path.py"]}])
        self.assertEqual(po.find(1, REPO, ["raw/path.py"], runner=lister(out=out))[0]["pr"], 3)


# --------------------------------------------------------------------------- the cost of looking

class TheLookupIsCachedAndBoundedTest(OverlapCase):
    """It sits on the ask path, so looking has to be cheap and has to give up early."""

    def test_a_second_ask_inside_the_ttl_costs_no_second_call(self):
        calls = []

        def run(argv):
            calls.append(argv)
            return 0, json.dumps([row(2, COLLISION)]), ""
        for tick in (1000.0, 1000.0 + po.CACHE_TTL_SEC - 1):
            po.list_open_prs(REPO, runner=run, now=tick)
        self.assertEqual(len(calls), 1)

    def test_past_the_ttl_it_looks_again(self):
        calls = []

        def run(argv):
            calls.append(argv)
            return 0, "[]", ""
        for tick in (1000.0, 1000.0 + po.CACHE_TTL_SEC + 1):
            po.list_open_prs(REPO, runner=run, now=tick)
        self.assertEqual(len(calls), 2)

    def test_the_cache_is_per_repository(self):
        calls = []

        def run(argv):
            calls.append(argv[argv.index("--repo") + 1])
            return 0, "[]", ""
        po.list_open_prs(REPO, runner=run, now=1000.0)
        po.list_open_prs("example/other", runner=run, now=1000.0)
        self.assertEqual(calls, [REPO, "example/other"])

    def test_a_failed_look_is_not_cached_as_an_answer(self):
        """`[]` from a broken `gh` must not become two minutes of confidently claiming no overlap."""
        calls = []

        def run(argv):
            calls.append(argv)
            raise FileNotFoundError("gh")
        po.list_open_prs(REPO, runner=run, now=1000.0)
        po.list_open_prs(REPO, runner=run, now=1000.5)
        self.assertEqual(len(calls), 2)

    def test_it_gives_up_sooner_than_the_sweep_does(self):
        """`pr_sweep` runs on a cadence and can afford to wait; this one is between the owner and a
        question they are owed."""
        import pr_sweep
        self.assertLess(po.LIST_TIMEOUT_SEC, pr_sweep.LIST_TIMEOUT_SEC)

    def test_the_list_is_bounded(self):
        seen = {}

        def run(argv):
            seen["argv"] = argv
            return 0, "[]", ""
        po.list_open_prs(REPO, runner=run, limit=7)
        self.assertEqual(seen["argv"][seen["argv"].index("--limit") + 1], "7")

    def test_a_file_list_at_githubs_own_cap_is_carried_as_suspect(self):
        """A truncated file list causes a MISSED overlap, which is the direction this fails in
        anyway. It is recorded rather than rendered — a caveat about pagination is not something the owner
        can act on. `../docs/concurrent-pr-collisions-spec.md` §8 records that the boundary is
        unverified."""
        big = row(2, ["f%d.py" % i for i in range(po.FILE_LIST_SUSPECT)] + ["shared.py"])
        got = po.overlaps(1, ["shared.py"], [big])
        self.assertTrue(got[0]["files_truncated"])
        self.assertFalse(po.overlaps(1, ["shared.py"], [row(3, ["shared.py"])])[0]["files_truncated"])


# --------------------------------------------------------------------------- in-flight jobs

class JobOverlapIsExactTest(unittest.TestCase):
    """`job_overlaps` is pure — `overlaps`'s own arithmetic, applied to a job row instead of a PR
    row."""

    def test_it_names_the_shared_paths(self):
        got = po.job_overlaps(COLLISION, [{"id": "j1", "paths": COLLISION + ["extra.py"]}])
        self.assertEqual(got, [{"job": "j1", "shared": COLLISION}])

    def test_no_shared_path_is_no_row_at_all(self):
        self.assertEqual(po.job_overlaps(["a.py"], [{"id": "j1", "paths": ["b.py"]}]), [])

    def test_a_job_with_no_touched_paths_overlaps_nothing(self):
        self.assertEqual(po.job_overlaps(COLLISION, [{"id": "j1", "paths": []}]), [])

    def test_this_prs_own_empty_path_list_overlaps_nothing(self):
        self.assertEqual(po.job_overlaps([], [{"id": "j1", "paths": COLLISION}]), [])

    def test_most_shared_first_then_ascending_job_id(self):
        rows = [{"id": "j3", "paths": COLLISION[:1]}, {"id": "j1", "paths": COLLISION},
                {"id": "j2", "paths": COLLISION[:1]}]
        self.assertEqual([r["job"] for r in po.job_overlaps(COLLISION, rows)], ["j1", "j2", "j3"])

    def test_rows_missing_an_id_or_not_a_dict_are_dropped_not_fatal(self):
        rows = [{"paths": COLLISION}, "not a dict", {"id": "j1", "paths": COLLISION}]
        self.assertEqual([r["job"] for r in po.job_overlaps(COLLISION, rows)], ["j1"])


class ListInflightJobsTest(unittest.TestCase):
    """`list_inflight_jobs` reads `jobs.list_jobs(..., active_only=True)` (via the injected
    `jobs_lister`) and `git diff`s each job's own recorded worktree/base pair."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_a_job_with_a_live_worktree_and_a_real_diff_is_reported(self):
        out = po.list_inflight_jobs(
            "unused", jobs_lister=lambda sd: [job_rec("j1", path=self.tmp)],
            runner=git_runner(out="seneschal/scripts/merge_guard.py\nseneschal/context-budget.json\n"))
        self.assertEqual(out, [{"id": "j1",
                                "paths": ["seneschal/scripts/merge_guard.py",
                                          "seneschal/context-budget.json"]}])

    def test_a_job_with_no_worktree_block_is_skipped(self):
        out = po.list_inflight_jobs("unused", jobs_lister=lambda sd: [job_rec("j1")],
                                    runner=git_runner(out="x.py\n"))
        self.assertEqual(out, [])

    def test_a_worktree_directory_that_is_gone_is_skipped(self):
        """`job_worktree.teardown` removes (or leaks, but never keeps live) the directory at the
        terminal transition — a path that no longer exists is not this module's to diff."""
        gone = os.path.join(self.tmp, "already-torn-down")
        out = po.list_inflight_jobs("unused", jobs_lister=lambda sd: [job_rec("j1", path=gone)],
                                    runner=git_runner(out="x.py\n"))
        self.assertEqual(out, [])

    def test_a_job_that_touched_nothing_is_not_reported(self):
        out = po.list_inflight_jobs("unused", jobs_lister=lambda sd: [job_rec("j1", path=self.tmp)],
                                    runner=git_runner(out=""))
        self.assertEqual(out, [])

    def test_paths_are_backslash_normalised(self):
        out = po.list_inflight_jobs(
            "unused", jobs_lister=lambda sd: [job_rec("j1", path=self.tmp)],
            runner=git_runner(out="seneschal\\scripts\\merge_guard.py\n"))
        self.assertEqual(out[0]["paths"], ["seneschal/scripts/merge_guard.py"])

    def test_rows_missing_or_malformed_are_dropped_not_fatal(self):
        records = ["not a dict", {"id": None, "worktree": {"path": self.tmp}},
                  {"worktree": {"path": self.tmp}}, job_rec("j1", path=self.tmp)]
        out = po.list_inflight_jobs("unused", jobs_lister=lambda sd: records,
                                    runner=git_runner(out="x.py\n"))
        self.assertEqual([r["id"] for r in out], ["j1"])

    def test_a_record_with_no_base_is_diffed_against_the_configured_base_branch(self):
        """The fallback ref is `origin/<repo_config.base_branch()>`, read at call time — a
        repository whose integration branch is not `develop` diffs against the right thing."""
        seen = []

        def run(argv):
            seen.append(argv)
            return 0, "x.py\n", ""
        rec = {"id": "j1", "worktree": {"path": self.tmp}}
        import repo_config
        with mock.patch.object(repo_config, "base_branch", return_value="trunk"):
            out = po.list_inflight_jobs("unused", jobs_lister=lambda sd: [rec], runner=run)
        self.assertEqual(out, [{"id": "j1", "paths": ["x.py"]}])
        self.assertEqual(seen[0][-1], "origin/trunk")

    def test_a_record_that_names_its_base_keeps_it(self):
        seen = []

        def run(argv):
            seen.append(argv)
            return 0, "x.py\n", ""
        po.list_inflight_jobs("unused", jobs_lister=lambda sd: [job_rec("j1", path=self.tmp,
                                                                        base="origin/main")],
                              runner=run)
        self.assertEqual(seen[0][-1], "origin/main")

    def test_an_empty_state_dir_answers_nothing_rather_than_guessing(self):
        called = []
        po.list_inflight_jobs("", jobs_lister=lambda sd: called.append(sd) or [])
        self.assertEqual(called, [])


class JobOverlapFailuresSayNothingTest(unittest.TestCase):
    """The polarity `EveryFailureSaysNothingTest` pins for `gh`, applied to the job-store half:
    a merge can invalidate a mid-job worktree, and a warning that can break must never cost the
    question it is trying to protect."""

    def test_a_broken_jobs_lister_answers_an_empty_list(self):
        def broken(sd):
            raise RuntimeError("state/jobs unreadable")
        self.assertEqual(po.list_inflight_jobs("unused", jobs_lister=broken), [])

    def test_a_jobs_lister_returning_garbage_answers_an_empty_list(self):
        self.assertEqual(po.list_inflight_jobs("unused", jobs_lister=lambda sd: "not a list"), [])

    def test_every_way_git_diff_can_fail_answers_an_empty_list(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        failures = {
            "git missing": git_runner(raises=FileNotFoundError("git")),
            "git timed out": git_runner(raises=subprocess.TimeoutExpired("git", 12)),
            "git exploded": git_runner(raises=RuntimeError("boom")),
            "non-zero exit": git_runner(code=1, out=""),
        }
        for name, runner in failures.items():
            with self.subTest(failure=name):
                out = po.list_inflight_jobs("unused", jobs_lister=lambda sd: [job_rec("j1", path=tmp)],
                                            runner=runner)
                self.assertEqual(out, [])

    def test_find_job_overlaps_never_raises_on_a_broken_lister(self):
        def broken(sd):
            raise RuntimeError("boom")
        self.assertEqual(po.find_job_overlaps(COLLISION, "unused", jobs_lister=broken), [])

    def test_find_job_overlaps_reports_a_real_collision_end_to_end(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        got = po.find_job_overlaps(
            COLLISION, "unused", jobs_lister=lambda sd: [job_rec("j1", path=tmp)],
            runner=git_runner(out="seneschal/scripts/merge_guard.py\nunrelated.py\n"))
        self.assertEqual(got, [{"job": "j1", "shared": ["seneschal/scripts/merge_guard.py"]}])


class ItWritesNothingTest(OverlapCase):
    """Phase 1 records nothing (the spec's §4.6). The cache is in-process on purpose."""

    def test_the_module_never_opens_a_file_for_writing(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pr_overlap.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        for forbidden in ('"w"', "'w'", '"a"', "os.replace", "makedirs", "json.dump("):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
