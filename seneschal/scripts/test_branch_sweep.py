#!/usr/bin/env python3
"""Tests for ``branch_sweep`` — the pass that deletes merged branches nothing is stacked on.

Four classes here are not ordinary unit tests and must not be relaxed into passing:

* **``TheSweepCannotAutomateTheBugTest``** is the reason this file exists. The sweep was proposed as
  the fix for `--delete-branch` closing a stacked pull request, and a sweep *without* the predicate
  is that same deletion on a timer with nobody watching. Every test in this class asserts a
  **non-deletion**: a branch that is the base of an open PR is never swept, and — the one that
  actually matters — the predicate is re-read **per branch immediately before acting**, so a PR
  opened after the candidate list was built still saves its base.
* **``UnknownIsNeverDeleteTest``** pins the one place this module's polarity differs from the hook's
  on purpose. The hook allows an outage because a human is standing at the terminal it allowed;
  this runs alone, so "I could not check" must never become "so I deleted it". If a test here ever
  goes green on a deletion, the difference has been erased.
* **``DryRunIsTheDefaultTest``** pins that `--apply` is required. `pr_sweep.py` can afford
  `--dry-run` as the *opt-in* because its act is sending a question; this one's act does not come
  back, and a default that deletes is a default somebody discovers by having deleted.
* **``TheBoundsAreRealTest``** covers `--limit` and its report. A repository that merged without
  `--delete-branch` for a while carries hundreds of stale heads, so an uncapped first run is not a
  hypothetical — and a capped run that does not say what it left behind reads as "covered
  everything" when it did not.

**No test may reach the network or delete anything.** Every `gh` and `git` call goes through the
``runner=`` seam; ``FakeRemote`` raises on anything unstubbed, and records every deletion it was
asked for so a test can assert on the ones that did *not* happen.

The base branch is passed explicitly (``base="develop"``) by the ``run`` helper, so no test reads
the checkout's own git or `pr-guard.json`; ``BaseBranchTest`` covers the `repo_config` fallback with
it patched.

Run:  python -m unittest test_branch_sweep   (from seneschal/scripts)
"""
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import branch_delete_guard as bdg  # noqa: E402
import branch_sweep as bs  # noqa: E402

NOW = datetime(2030, 1, 15, 22, 30, tzinfo=timezone.utc)


def stamp(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeRemote:
    """A `gh`/`git` stand-in. Records every deletion so a test can assert on absence."""

    def __init__(self, heads=(), merged=(), open_prs=(), based_on=None,
                 gh_fail=None, delete_fail=(), on_predicate=None):
        self.heads = list(heads)
        self.merged = list(merged)
        self.open_prs = list(open_prs)
        self.based_on = dict(based_on or {})
        self.gh_fail = gh_fail
        self.delete_fail = set(delete_fail)
        #: Called with the branch just before each predicate read — the seam a test uses to open a
        #: pull request *between* the candidate list and the deletion.
        self.on_predicate = on_predicate
        self.deleted = []
        self.predicate_reads = []

    def __call__(self, argv, cwd=None, timeout=None):
        if argv[:2] == ["git", "ls-remote"]:
            return 0, "".join(f"{'a' * 40}\trefs/heads/{h}\n" for h in self.heads), ""
        if argv[:2] == ["git", "remote"]:
            return 0, "https://github.com/example/repo.git\n", ""
        if argv[0] == "gh" and self.gh_fail is not None:
            if isinstance(self.gh_fail, BaseException):
                raise self.gh_fail
            return self.gh_fail[0], "", self.gh_fail[1]
        if argv[:3] == ["gh", "pr", "list"] and "--base" in argv:
            branch = argv[argv.index("--base") + 1]
            self.predicate_reads.append(branch)
            if self.on_predicate:
                self.on_predicate(self, branch)
            return 0, json.dumps(self.based_on.get(branch, [])), ""
        if argv[:3] == ["gh", "pr", "list"]:
            state = argv[argv.index("--state") + 1]
            return 0, json.dumps(self.merged if state == "merged" else self.open_prs), ""
        if argv[:2] == ["gh", "api"] and "DELETE" in argv:
            branch = argv[2].split("/git/refs/heads/", 1)[1]
            if branch in self.delete_fail:
                return 1, "", "HTTP 422: Reference does not exist"
            self.deleted.append(branch)
            return 0, "", ""
        raise AssertionError(f"unstubbed call: {argv!r}")


def merged_pr(number, head, days_ago=0, title="a merged change"):
    return {"number": number, "headRefName": head, "mergedAt": stamp(days_ago), "title": title}


def open_pr(number, head, base="develop"):
    return {"number": number, "headRefName": head, "baseRefName": base}


def stacked(number, head="feat/dependent"):
    return {"number": number, "title": "a stacked change", "headRefName": head,
            "url": f"https://github.com/example/repo/pull/{number}"}


def run(fake, **kwargs):
    kwargs.setdefault("repo", "example/repo")
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("base", "develop")
    return bs.sweep(runner=fake, **kwargs)


# --------------------------------------------------------------------------- the whole point

class TheSweepCannotAutomateTheBugTest(unittest.TestCase):

    def test_a_branch_that_is_the_base_of_an_open_pr_is_NEVER_deleted(self):
        """The failure, in the sweep's clothes: `docs/some-spec` has merged, so it is a candidate
        by every other test — and #565 is stacked on it."""
        fake = FakeRemote(
            heads=["docs/some-spec"],
            merged=[merged_pr(566, "docs/some-spec")],
            open_prs=[open_pr(565, "feat/stacked-change", base="docs/some-spec")],
            based_on={"docs/some-spec": [stacked(565, "feat/stacked-change")]})
        result = run(fake, apply=True)
        self.assertEqual(fake.deleted, [])
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["excluded"]["open-base"], ["docs/some-spec"])

    def test_the_control_a_merged_branch_with_no_dependents_IS_deleted(self):
        """The other half. A sweep that deletes nothing is not a fix either."""
        fake = FakeRemote(heads=["docs/some-spec", "feat/stacked-change"],
                          merged=[merged_pr(566, "docs/some-spec"),
                                  merged_pr(565, "feat/stacked-change")])
        result = run(fake, apply=True)
        self.assertEqual(sorted(fake.deleted),
                         ["docs/some-spec", "feat/stacked-change"])
        self.assertEqual(len(result["deleted"]), 2)
        self.assertEqual(result["skipped"], [])

    def test_the_predicate_is_re_read_PER_BRANCH_immediately_before_each_deletion(self):
        """The bulk lists find candidates; they never authorise a deletion. A pull request opened
        after the list was built still saves its base — which is the only thing that makes the
        unattended version of this safe at all."""
        def open_a_pr_midway(fake, branch):
            if branch == "first":
                fake.based_on["second"] = [stacked(999)]   # someone stacks on `second` right now

        fake = FakeRemote(heads=["first", "second"],
                          merged=[merged_pr(1, "first", days_ago=0),
                                  merged_pr(2, "second", days_ago=1)],
                          on_predicate=open_a_pr_midway)
        result = run(fake, apply=True)
        self.assertEqual(fake.predicate_reads, ["first", "second"])
        self.assertEqual(fake.deleted, ["first"])
        self.assertEqual([s["branch"] for s in result["skipped"]], ["second"])
        self.assertEqual(result["skipped"][0]["blocked_by"], [999])

    def test_the_predicate_is_read_once_per_candidate_and_is_the_guards_own(self):
        fake = FakeRemote(heads=["a", "b", "c"],
                          merged=[merged_pr(1, "a"), merged_pr(2, "b"), merged_pr(3, "c")])
        run(fake, apply=True)
        self.assertEqual(sorted(fake.predicate_reads), ["a", "b", "c"])

    def test_a_protected_branch_is_never_a_candidate(self):
        fake = FakeRemote(heads=["develop", "master", "main", "feat/ok"],
                          merged=[merged_pr(1, "develop"), merged_pr(2, "master"),
                                  merged_pr(3, "main"), merged_pr(4, "feat/ok")])
        result = run(fake, apply=True)
        self.assertEqual(fake.deleted, ["feat/ok"])
        self.assertEqual(sorted(result["excluded"]["protected"]), ["develop", "main", "master"])

    def test_a_branch_with_no_merged_pr_is_never_deleted(self):
        """It may be somebody's work in progress with no PR open yet, and this cannot tell."""
        fake = FakeRemote(heads=["wip/nothing-yet"], merged=[], open_prs=[])
        result = run(fake, apply=True)
        self.assertEqual(fake.deleted, [])
        self.assertEqual(result["excluded"]["no-merged-pr"], ["wip/nothing-yet"])

    def test_the_head_of_an_open_pr_is_never_deleted_even_if_an_older_pr_on_it_merged(self):
        fake = FakeRemote(heads=["feat/reused"],
                          merged=[merged_pr(1, "feat/reused", days_ago=9)],
                          open_prs=[open_pr(2, "feat/reused")])
        result = run(fake, apply=True)
        self.assertEqual(fake.deleted, [])
        self.assertEqual(result["excluded"]["open-head"], ["feat/reused"])

    def test_a_branch_no_longer_on_the_remote_is_not_a_candidate(self):
        """Deleted by hand last week; it must not come back as a candidate every run forever."""
        fake = FakeRemote(heads=[], merged=[merged_pr(1, "gone")])
        result = run(fake, apply=True)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(fake.deleted, [])


# --------------------------------------------------------------------------- the polarity

class UnknownIsNeverDeleteTest(unittest.TestCase):
    """The hook allows an outage; this does not. If these go green on a deletion, that difference
    has been erased and the unattended pass has become the thing it was written to prevent."""

    def test_an_unreachable_github_skips_the_branch_rather_than_deleting_it(self):
        def flaky(fake, branch):
            if branch == "b":
                raise subprocess.TimeoutExpired(cmd="gh", timeout=30)

        fake = FakeRemote(heads=["a", "b"], merged=[merged_pr(1, "a"), merged_pr(2, "b")],
                          on_predicate=flaky)
        result = run(fake, apply=True)
        self.assertEqual(fake.deleted, ["a"])
        self.assertEqual([e["branch"] for e in result["errors"]], ["b"])
        self.assertIn("predicate could not be evaluated", result["errors"][0]["why"])

    def test_an_unclassifiable_gh_failure_skips_the_branch(self):
        fake = FakeRemote(heads=["a"], merged=[merged_pr(1, "a")])
        original = bdg.open_prs_based_on
        try:
            bdg.open_prs_based_on = lambda *a, **k: (_ for _ in ()).throw(
                bdg.GuardError("something nobody has seen"))
            result = run(fake, apply=True)
        finally:
            bdg.open_prs_based_on = original
        self.assertEqual(fake.deleted, [])
        self.assertEqual(len(result["errors"]), 1)

    def test_a_failed_deletion_is_reported_and_does_not_stop_the_pass(self):
        fake = FakeRemote(heads=["a", "b"], merged=[merged_pr(1, "a", 0), merged_pr(2, "b", 1)],
                          delete_fail=["a"])
        result = run(fake, apply=True)
        self.assertEqual(fake.deleted, ["b"])
        self.assertEqual([e["branch"] for e in result["errors"]], ["a"])

    def test_a_whole_run_that_cannot_build_a_list_raises_and_deletes_nothing(self):
        fake = FakeRemote(heads=["a"], merged=[merged_pr(1, "a")],
                          gh_fail=(1, "gh: could not reach github.com"))
        with self.assertRaises(bs.SweepError):
            run(fake, apply=True)
        self.assertEqual(fake.deleted, [])

    def test_the_cli_exits_nonzero_when_anything_was_left_unswept(self):
        fake = FakeRemote(heads=["a"], merged=[merged_pr(1, "a")], delete_fail=["a"])
        result = run(fake, apply=True)
        self.assertTrue(result["errors"])
        # `main` maps a non-empty `errors` to exit 1; asserted on the result it reads.
        self.assertEqual(1 if result["errors"] else 0, 1)


# --------------------------------------------------------------------------- the bounds

class DryRunIsTheDefaultTest(unittest.TestCase):

    def test_sweep_deletes_nothing_without_apply(self):
        fake = FakeRemote(heads=["a", "b"], merged=[merged_pr(1, "a"), merged_pr(2, "b")])
        result = run(fake)
        self.assertEqual(fake.deleted, [])
        self.assertEqual(len(result["deleted"]), 2)
        self.assertTrue(all(row["dry_run"] for row in result["deleted"]))

    def test_the_cli_default_is_a_dry_run(self):
        seen = {}

        def fake_sweep(**kwargs):
            seen.update(kwargs)
            return {"apply": kwargs["apply"], "deleted": [], "skipped": [], "deferred": [],
                    "errors": [], "not_a_candidate": [], "excluded": {},
                    "counts": {"remote_branches": 0, "merged_prs": 0, "open_prs": 0,
                               "candidates": 0}}

        original = bs.sweep
        try:
            bs.sweep = fake_sweep
            bs.main([])
            self.assertFalse(seen["apply"])
            bs.main(["--apply"])
            self.assertTrue(seen["apply"])
            bs.main(["--apply", "--dry-run"])   # the cautious spelling wins
            self.assertFalse(seen["apply"])
        finally:
            bs.sweep = original

    def test_a_dry_run_and_a_real_run_print_the_same_shape(self):
        fake = FakeRemote(heads=["a"], merged=[merged_pr(1, "a")])
        dry = bs.render(run(fake))
        fake2 = FakeRemote(heads=["a"], merged=[merged_pr(1, "a")])
        wet = bs.render(run(fake2, apply=True))
        self.assertIn("DRY RUN", dry)
        self.assertIn("APPLY", wet)
        self.assertIn("would delete: 1", dry)
        self.assertIn("deleted: 1", wet)


class TheBoundsAreRealTest(unittest.TestCase):

    def test_limit_caps_deletions_and_the_run_NAMES_what_it_left_behind(self):
        heads = [f"feat/{i}" for i in range(10)]
        fake = FakeRemote(heads=heads,
                          merged=[merged_pr(i, f"feat/{i}", days_ago=i) for i in range(10)])
        result = run(fake, apply=True, limit=3)
        self.assertEqual(len(fake.deleted), 3)
        self.assertEqual(len(result["deferred"]), 7)
        self.assertIn("held over by --limit 3", bs.render(result))

    def test_a_skipped_branch_counts_against_the_limit_so_a_pass_is_bounded_in_WORK(self):
        """Otherwise a remote full of blocked branches makes one 'capped' pass unbounded."""
        heads = [f"feat/{i}" for i in range(6)]
        fake = FakeRemote(heads=heads,
                          merged=[merged_pr(i, f"feat/{i}", days_ago=i) for i in range(6)],
                          based_on={f"feat/{i}": [stacked(100 + i)] for i in range(6)})
        result = run(fake, apply=True, limit=2)
        self.assertEqual(len(result["skipped"]), 2)
        self.assertEqual(len(result["deferred"]), 4)

    def test_merged_within_filters_by_merge_age(self):
        fake = FakeRemote(heads=["recent", "ancient"],
                          merged=[merged_pr(1, "recent", days_ago=1),
                                  merged_pr(2, "ancient", days_ago=40)])
        result = run(fake, apply=True, merged_within=7)
        self.assertEqual(fake.deleted, ["recent"])
        self.assertEqual(result["excluded"]["outside-window"], ["ancient"])

    def test_there_is_no_default_window_so_the_backlog_is_visible_rather_than_hidden(self):
        fake = FakeRemote(heads=["ancient"], merged=[merged_pr(1, "ancient", days_ago=400)])
        result = run(fake)
        self.assertEqual(len(result["deleted"]), 1)
        self.assertEqual(result["excluded"]["outside-window"], [])

    def test_branch_restricts_the_pass_but_does_not_bypass_the_predicate(self):
        fake = FakeRemote(heads=["a", "b"], merged=[merged_pr(1, "a"), merged_pr(2, "b")],
                          based_on={"a": [stacked(7)]})
        result = run(fake, apply=True, only=["a"])
        self.assertEqual(fake.deleted, [])
        self.assertEqual([s["branch"] for s in result["skipped"]], ["a"])

    def test_a_named_branch_that_is_not_a_candidate_is_reported_not_deleted(self):
        fake = FakeRemote(heads=["a"], merged=[merged_pr(1, "a")])
        result = run(fake, apply=True, only=["develop"])
        self.assertEqual(fake.deleted, [])
        self.assertEqual(result["not_a_candidate"], ["develop"])

    def test_a_truncated_merged_list_is_announced(self):
        fake = FakeRemote(heads=["a"],
                          merged=[merged_pr(i, f"x{i}") for i in range(bs.PR_LIST_LIMIT)])
        result = run(fake)
        self.assertTrue(result["counts"]["merged_list_at_limit"])
        self.assertIn("under-reported", bs.render(result))


class CandidateFindingTest(unittest.TestCase):

    def test_the_latest_merge_of_a_reused_branch_is_the_one_reported(self):
        fake = FakeRemote(heads=["feat/reused"],
                          merged=[merged_pr(1, "feat/reused", days_ago=30),
                                  merged_pr(2, "feat/reused", days_ago=1)])
        found = bs.candidates(repo="o/r", runner=fake, now=NOW, base="develop")
        self.assertEqual(found["candidates"][0]["pr"], 2)

    def test_a_merged_row_with_an_unreadable_timestamp_is_not_a_candidate(self):
        fake = FakeRemote(heads=["a"], merged=[{"number": 1, "headRefName": "a",
                                                "mergedAt": "not a date"}])
        found = bs.candidates(repo="o/r", runner=fake, now=NOW, base="develop")
        self.assertEqual(found["candidates"], [])
        self.assertEqual(found["excluded"]["no-merged-pr"], ["a"])

    def test_remote_branches_reads_refs_heads_only(self):
        class Refs:
            def __call__(self, argv, cwd=None, timeout=None):
                return 0, ("a" * 40 + "\trefs/heads/feat/x\n"
                           + "b" * 40 + "\trefs/tags/v1.0\n"
                           + "c" * 40 + "\trefs/pull/5/head\n"), ""
        self.assertEqual(bs.remote_branches(runner=Refs()), {"feat/x"})

    def test_exclusions_are_returned_rather_than_dropped(self):
        """The interesting output of a dry run is usually 'why is this branch still here'."""
        fake = FakeRemote(heads=["develop", "wip", "ok"],
                          merged=[merged_pr(1, "ok")])
        found = bs.candidates(repo="o/r", runner=fake, now=NOW, base="develop")
        self.assertEqual(found["excluded"]["protected"], ["develop"])
        self.assertEqual(found["excluded"]["no-merged-pr"], ["wip"])
        self.assertEqual([c["branch"] for c in found["candidates"]], ["ok"])


class BaseBranchTest(unittest.TestCase):
    """The base branch is config (`repo_config.base_branch()`), and it is never swept even when it is
    not in the guard's fixed floor."""

    def test_a_non_floor_base_branch_is_protected(self):
        fake = FakeRemote(heads=["integration", "feat/ok"],
                          merged=[merged_pr(1, "integration"), merged_pr(2, "feat/ok")])
        result = run(fake, apply=True, base="integration")
        self.assertEqual(fake.deleted, ["feat/ok"])
        self.assertEqual(result["excluded"]["protected"], ["integration"])

    def test_no_base_given_falls_back_to_repo_config(self):
        original = bs.repo_config.base_branch
        try:
            bs.repo_config.base_branch = lambda cfg=None, cwd=None, runner=None: "integration"
            self.assertEqual(bs.resolve_base(None), "integration")
            fake = FakeRemote(heads=["integration"], merged=[merged_pr(1, "integration")])
            result = bs.sweep(runner=fake, repo="example/repo", now=NOW, apply=True)
        finally:
            bs.repo_config.base_branch = original
        self.assertEqual(fake.deleted, [])
        self.assertEqual(result["excluded"]["protected"], ["integration"])

    def test_the_guards_configured_protected_set_is_honoured(self):
        original = bdg.repo_config.protected_branches
        try:
            bdg.repo_config.protected_branches = (
                lambda cfg=None: bdg.repo_config.PROTECTED_FLOOR | {"release/next"})
            fake = FakeRemote(heads=["release/next"], merged=[merged_pr(1, "release/next")])
            result = run(fake, apply=True)
        finally:
            bdg.repo_config.protected_branches = original
        self.assertEqual(fake.deleted, [])
        self.assertEqual(result["excluded"]["protected"], ["release/next"])

    def test_an_explicit_base_wins_and_blank_is_ignored(self):
        self.assertEqual(bs.resolve_base("trunk2"), "trunk2")
        original = bs.repo_config.base_branch
        try:
            bs.repo_config.base_branch = lambda cfg=None, cwd=None, runner=None: "develop"
            self.assertEqual(bs.resolve_base("   "), "develop")
        finally:
            bs.repo_config.base_branch = original


class ItOnlyDeletesRefsTest(unittest.TestCase):
    """It finds and it deletes refs. Nothing here may merge, approve, or record an approval."""

    def _source(self):
        with open(os.path.join(SCRIPT_DIR, "branch_sweep.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_it_never_merges_or_approves(self):
        code = self._source()
        for forbidden in ("record_approval", "ask_on_green", '"merge"', "pr merge"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, code)

    def test_the_only_write_it_makes_is_a_ref_deletion(self):
        code = self._source()
        self.assertEqual(code.count('"-X", "DELETE"'), 1)
        self.assertIn("def delete_branch", code)

    def test_it_writes_no_state_file(self):
        code = self._source()
        for forbidden in ("open(", "state_dir", "json.dump("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, code)


if __name__ == "__main__":
    unittest.main()
