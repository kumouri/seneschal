#!/usr/bin/env python3
"""Tests for ``develop_ci_status`` — the measured answer to "is the base branch's tip red", built so an
alert can never again be assembled from a scan of recent commits: a green tip, one named commit that
was never on the branch at all (the old head of a still-open PR), and another that was on it only as
an intermediate commit a later commit in the same PR had already fixed.

``TheConflationShapeTest`` reproduces that shape directly: a green tip plus a red non-ancestor commit
plus a red superseded ancestor must produce no alert. Every other class here defends one measurement
this module makes so a later edit cannot quietly reintroduce the conflation:

* ``VerdictFromRunsTest`` pins the pure fold — green requires at least one completed+passing row,
  any non-completed row is ``pending``, any completed-but-failing row is ``red``, and no rows at all
  is ``unknown`` rather than a guess in either direction.
* ``ShouldAlertTest`` pins the gate a caller actually needs: only a measured ``red`` may ever be
  described to the owner as "CI is red on the base branch" — ``pending`` and ``unknown`` both refuse,
  on purpose, because an unresolved verdict is not evidence of a red one.
* ``IsAncestorTest`` pins the three-way answer (``True``/``False``/``None``) a caller needs before it
  may describe any commit as "on the branch" at all.
* ``NeverRaisesTest`` — a Watch peek may not go down because GitHub or the local ``git`` is
  unreachable, so every failure mode returns ``unknown`` rather than propagating.
* ``BranchIsConfigurationTest`` — the branch is ``repo_config.base_branch()`` unless a caller names
  one, resolved at call time.

No test here reaches a real ``git`` or ``gh``; every call goes through the ``runner=`` seam, and the
configured base branch is pinned to ``develop`` for the module so no verdict depends on this checkout's
own remotes.

Run:  python -m unittest test_develop_ci_status   (from seneschal/scripts)
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import develop_ci_status as dcs  # noqa: E402

_BASE_PATCH = mock.patch.object(dcs.repo_config, "base_branch", return_value="develop")


def setUpModule():
    _BASE_PATCH.start()


def tearDownModule():
    _BASE_PATCH.stop()


def _runner(table):
    """Build a `runner(argv) -> (code, out, err)` from `{tuple(argv): (code, out, err)}`, keyed by the
    argv exactly as the module builds it. Raises AssertionError on an unexpected call, so a test can't
    silently pass by matching the wrong command."""
    def run(argv):
        key = tuple(argv)
        if key not in table:
            raise AssertionError(f"unexpected call: {argv!r}")
        return table[key]
    return run


TIP = "aaaa000011112222333344445555666677778888"
OPEN_PR_HEAD = "97ee10bb00000000000000000000000000000000"  # never merged — not an ancestor
SUPERSEDED_ANCESTOR = "b52e062200000000000000000000000000000000"  # merged, then fixed in-PR


def _rev_parse_call(branch="develop", remote="origin"):
    return ("git", "rev-parse", f"{remote}/{branch}")


def _run_list_call(branch="develop", limit=dcs.RUN_LIST_LIMIT):
    return ("gh", "run", "list", "--branch", branch, "--limit", str(limit),
            "--json", "headSha,status,conclusion,workflowName,createdAt")


def _merge_base_call(sha, branch="develop", remote="origin"):
    return ("git", "merge-base", "--is-ancestor", sha, f"{remote}/{branch}")


class TheConflationShapeTest(unittest.TestCase):
    """A green tip, a red commit that is not on the branch at all, and a red commit that WAS on it but
    only as an intermediate step a later commit in the same PR fixed. The verdict must be green and
    the alert gate must refuse."""

    def test_green_tip_with_unrelated_red_history_produces_no_alert(self):
        rows = [
            # the still-open PR's old head — never on the branch, and never even the same SHA as the
            # tip, so a correct implementation cannot see it no matter what it says
            {"headSha": OPEN_PR_HEAD, "status": "completed", "conclusion": "failure",
             "workflowName": "CI"},
            # the superseded intermediate commit inside an already-merged PR — ALSO not the tip
            {"headSha": SUPERSEDED_ANCESTOR, "status": "completed", "conclusion": "failure",
             "workflowName": "CI"},
            # the tip itself — green
            {"headSha": TIP, "status": "completed", "conclusion": "success", "workflowName": "CI"},
        ]
        table = {
            _rev_parse_call(): (0, TIP + "\n", ""),
            _run_list_call(): (0, json.dumps(rows), ""),
        }
        result = dcs.develop_ci_verdict(runner=_runner(table))
        self.assertEqual(result["verdict"], "green")
        self.assertEqual(result["tip_sha"], TIP)
        self.assertFalse(dcs.should_alert_ci_red(result))

        # And the two commits are correctly distinguishable, if a caller checks:
        ancestor_table = {
            _merge_base_call(OPEN_PR_HEAD): (1, "", ""),  # not an ancestor — never merged
            _merge_base_call(SUPERSEDED_ANCESTOR): (0, "", ""),  # is an ancestor — was merged
        }
        run = _runner(ancestor_table)
        self.assertFalse(dcs.is_ancestor(OPEN_PR_HEAD, runner=run))
        self.assertTrue(dcs.is_ancestor(SUPERSEDED_ANCESTOR, runner=run))


class VerdictFromRunsTest(unittest.TestCase):
    def test_no_rows_is_unknown_not_green(self):
        self.assertEqual(dcs.verdict_from_runs([]), "unknown")

    def test_one_pending_row_is_pending(self):
        rows = [{"headSha": TIP, "status": "in_progress", "conclusion": None}]
        self.assertEqual(dcs.verdict_from_runs(rows), "pending")

    def test_one_failed_row_is_red(self):
        rows = [{"headSha": TIP, "status": "completed", "conclusion": "failure"}]
        self.assertEqual(dcs.verdict_from_runs(rows), "red")

    def test_all_passing_rows_is_green(self):
        rows = [
            {"headSha": TIP, "status": "completed", "conclusion": "success"},
            {"headSha": TIP, "status": "completed", "conclusion": "neutral"},
        ]
        self.assertEqual(dcs.verdict_from_runs(rows), "green")

    def test_one_failed_among_several_passing_is_red(self):
        rows = [
            {"headSha": TIP, "status": "completed", "conclusion": "success"},
            {"headSha": TIP, "status": "completed", "conclusion": "cancelled"},
        ]
        self.assertEqual(dcs.verdict_from_runs(rows), "red")

    def test_pending_outranks_a_prior_failure_in_scan_order(self):
        # a run still queued must never be swallowed by a run that already failed — both need to be
        # seen, and "pending" is the answer a caller must not skip past on the way to "red"
        rows = [
            {"headSha": TIP, "status": "completed", "conclusion": "failure"},
            {"headSha": TIP, "status": "queued", "conclusion": None},
        ]
        self.assertEqual(dcs.verdict_from_runs(rows), "pending")


class ShouldAlertTest(unittest.TestCase):
    def test_red_alerts(self):
        self.assertTrue(dcs.should_alert_ci_red({"verdict": "red"}))

    def test_green_never_alerts(self):
        self.assertFalse(dcs.should_alert_ci_red({"verdict": "green"}))

    def test_pending_never_alerts(self):
        self.assertFalse(dcs.should_alert_ci_red({"verdict": "pending"}))

    def test_unknown_never_alerts(self):
        self.assertFalse(dcs.should_alert_ci_red({"verdict": "unknown"}))

    def test_malformed_input_never_alerts(self):
        self.assertFalse(dcs.should_alert_ci_red(None))
        self.assertFalse(dcs.should_alert_ci_red({}))
        self.assertFalse(dcs.should_alert_ci_red("red"))


class IsAncestorTest(unittest.TestCase):
    def test_exit_0_is_true(self):
        table = {_merge_base_call("deadbeef"): (0, "", "")}
        self.assertTrue(dcs.is_ancestor("deadbeef", runner=_runner(table)))

    def test_exit_1_is_false_never_none(self):
        table = {_merge_base_call("deadbeef"): (1, "", "")}
        self.assertFalse(dcs.is_ancestor("deadbeef", runner=_runner(table)))

    def test_unrecognised_exit_is_none_not_false(self):
        # a bad object / not-a-repo (git's usual 128) must read as "couldn't tell", never as "not an
        # ancestor" — the inversion that lets a wrong description pass as a checked one
        table = {_merge_base_call("deadbeef"): (128, "", "fatal: not a valid object name")}
        self.assertIsNone(dcs.is_ancestor("deadbeef", runner=_runner(table)))

    def test_missing_git_is_none(self):
        def run(argv):
            raise FileNotFoundError("git")
        self.assertIsNone(dcs.is_ancestor("deadbeef", runner=run))


class NeverRaisesTest(unittest.TestCase):
    """A Watch peek may not go down because GitHub or the local git is unreachable."""

    def test_missing_git_is_unknown(self):
        def run(argv):
            raise FileNotFoundError("git")
        result = dcs.develop_ci_verdict(runner=run)
        self.assertEqual(result["verdict"], "unknown")
        self.assertIsNone(result["tip_sha"])
        self.assertFalse(dcs.should_alert_ci_red(result))

    def test_rev_parse_nonzero_is_unknown(self):
        table = {_rev_parse_call(): (128, "", "fatal: bad revision 'origin/develop'")}
        result = dcs.develop_ci_verdict(runner=_runner(table))
        self.assertEqual(result["verdict"], "unknown")
        self.assertTrue(result["reason"])

    def test_gh_missing_is_unknown_but_tip_sha_still_reported(self):
        def run(argv):
            if argv[0] == "git":
                return (0, TIP + "\n", "")
            raise FileNotFoundError("gh")
        result = dcs.develop_ci_verdict(runner=run)
        self.assertEqual(result["verdict"], "unknown")
        self.assertEqual(result["tip_sha"], TIP)
        self.assertFalse(dcs.should_alert_ci_red(result))

    def test_gh_timeout_is_unknown(self):
        def run(argv):
            if argv[0] == "git":
                return (0, TIP + "\n", "")
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
        result = dcs.develop_ci_verdict(runner=run)
        self.assertEqual(result["verdict"], "unknown")

    def test_unparseable_gh_output_is_unknown(self):
        def run(argv):
            if argv[0] == "git":
                return (0, TIP + "\n", "")
            return (0, "not json", "")
        result = dcs.develop_ci_verdict(runner=run)
        self.assertEqual(result["verdict"], "unknown")

    def test_no_run_yet_for_the_tip_is_unknown_not_green(self):
        # gh has runs, but none of them are at the tip's exact sha yet (Actions can lag a push) —
        # this must never fold into "green" by the absence of a red one
        rows = [{"headSha": "deadbeef", "status": "completed", "conclusion": "success"}]
        table = {
            _rev_parse_call(): (0, TIP + "\n", ""),
            _run_list_call(): (0, json.dumps(rows), ""),
        }
        result = dcs.develop_ci_verdict(runner=_runner(table))
        self.assertEqual(result["verdict"], "unknown")
        self.assertEqual(result["runs"], [])


class BranchIsConfigurationTest(unittest.TestCase):
    """The branch comes from `repo_config.base_branch()` unless the caller names one — so a repository
    whose integration branch is `main` or `trunk` gets the right answer with no code change."""

    def test_no_branch_reads_the_configured_base_at_call_time(self):
        rows = [{"headSha": TIP, "status": "completed", "conclusion": "success"}]
        table = {
            _rev_parse_call("trunk"): (0, TIP + "\n", ""),
            _run_list_call("trunk"): (0, json.dumps(rows), ""),
        }
        with mock.patch.object(dcs.repo_config, "base_branch", return_value="trunk"):
            result = dcs.develop_ci_verdict(runner=_runner(table))
        self.assertEqual(result["branch"], "trunk")
        self.assertEqual(result["verdict"], "green")

    def test_an_explicit_branch_wins_over_the_config(self):
        table = {_rev_parse_call("main"): (128, "", "fatal: bad revision")}
        with mock.patch.object(dcs.repo_config, "base_branch", return_value="trunk"):
            result = dcs.develop_ci_verdict("main", runner=_runner(table))
        self.assertEqual(result["branch"], "main")
        self.assertEqual(result["verdict"], "unknown")


if __name__ == "__main__":
    unittest.main()
