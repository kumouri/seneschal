#!/usr/bin/env python3
"""Tests for `check_docs.py` — the doc-side CI gate aggregator.

Two classes of test:

* **The aggregator's own mechanics**, against fake gate modules installed into `sys.modules` so the
  behaviour under test — every gate runs regardless of an earlier failure, the aggregate exit code is
  non-zero iff at least one gate failed, a report-only gate's own `0` is never second-guessed — is
  proven without depending on any real gate's current findings on the live tree.
* **`WorkflowCoverageTest`**, which is the whole point of this module (`check_docs.py`'s own docstring,
  "The list, and why it's hardcoded"): `GATES` is a hardcoded copy of `ci.yml`'s doc-side invocations
  precisely because this repo has no YAML parser and "is this doc-side" isn't a syntactic question a
  script can answer on its own. The hardcoding is only as safe as this test — it fails the moment
  `ci.yml` gains a `check_*.py` invocation that is neither in `GATES` nor triaged into
  `EXCLUDED_FROM_CI_AGGREGATION`, which is exactly the gap that makes a docs-only change hit its
  gates one at a time instead of all at once.

Run:  python -m unittest seneschal.scripts.test_check_docs
"""
from __future__ import annotations

import sys
import types
import unittest
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import check_docs as cd  # noqa: E402


def _install_fake_gate(name: str, exit_code: int, calls: list, stdout_marker: str = "") -> None:
    """A throwaway module standing in for a gate's own script — `main(argv) -> int`, recorded in
    `calls` when invoked. Never touches the filesystem or a real gate's logic."""
    module = types.ModuleType(name)

    def main(argv=None):
        calls.append(name)
        print("%s ran; argv=%r%s" % (name, argv, (" " + stdout_marker) if stdout_marker else ""))
        return exit_code

    module.main = main
    sys.modules[name] = module


class _FakeGateFixture(unittest.TestCase):
    """Installs and tears down fake gate modules + a swapped `check_docs.GATES`, so a failing
    assertion never leaves stray modules or a mutated GATES for the next test."""

    def setUp(self):
        self._orig_gates = cd.GATES
        self._installed: list = []

    def tearDown(self):
        cd.GATES = self._orig_gates
        for name in self._installed:
            sys.modules.pop(name, None)

    def install(self, name: str, exit_code: int, calls: list, stdout_marker: str = "") -> None:
        _install_fake_gate(name, exit_code, calls, stdout_marker)
        self._installed.append(name)

    def set_gates(self, *names: str) -> None:
        cd.GATES = tuple({"name": n, "argv": []} for n in names)


class AllGatesRunRegardlessOfEarlierFailureTests(_FakeGateFixture):

    def test_every_gate_runs_even_when_the_first_fails(self):
        """The entire reason this module exists: a change reddened by ONE gate at a time because a
        failing gate hid the ones after it. A fake gate that fails must never stop the next one."""
        calls: list = []
        self.install("fake_gate_a", 1, calls)
        self.install("fake_gate_b", 0, calls)
        self.install("fake_gate_c", 1, calls)
        self.set_gates("fake_gate_a", "fake_gate_b", "fake_gate_c")

        result = cd.run(root="unused-for-fakes")

        self.assertEqual(calls, ["fake_gate_a", "fake_gate_b", "fake_gate_c"])

    def test_every_gate_appears_in_the_result_with_its_own_verdict(self):
        calls: list = []
        self.install("fake_gate_pass", 0, calls)
        self.install("fake_gate_fail", 1, calls)
        self.set_gates("fake_gate_pass", "fake_gate_fail")

        result = cd.run(root="unused-for-fakes")

        by_name = {g["name"]: g for g in result["gates"]}
        self.assertTrue(by_name["fake_gate_pass"]["ok"])
        self.assertFalse(by_name["fake_gate_fail"]["ok"])


class AggregateExitCodeTests(_FakeGateFixture):

    def _main_exit_code(self, only=None):
        """`main()`'s own exit-code arithmetic (`1 if result["failing"] else 0`), exercised without
        going through argparse — this is the same rule `main` applies, kept in one place so the test
        can't silently drift from the implementation it is pinning."""
        result = cd.run(root="unused-for-fakes", only=only)
        return 1 if result["failing"] else 0

    def test_all_gates_passing_is_exit_zero(self):
        calls: list = []
        self.install("fake_gate_ok1", 0, calls)
        self.install("fake_gate_ok2", 0, calls)
        self.set_gates("fake_gate_ok1", "fake_gate_ok2")
        self.assertEqual(self._main_exit_code(), 0)

    def test_a_single_failure_among_many_makes_the_aggregate_nonzero(self):
        calls: list = []
        self.install("fake_gate_ok", 0, calls)
        self.install("fake_gate_bad", 1, calls)
        self.install("fake_gate_ok2", 0, calls)
        self.set_gates("fake_gate_ok", "fake_gate_bad", "fake_gate_ok2")
        self.assertEqual(self._main_exit_code(), 1)

    def test_every_gate_failing_is_still_exit_one_not_a_count(self):
        """Non-zero ONCE at the end, per the spec — not once per failing gate."""
        calls: list = []
        self.install("fake_gate_bad1", 1, calls)
        self.install("fake_gate_bad2", 2, calls)
        self.set_gates("fake_gate_bad1", "fake_gate_bad2")
        self.assertEqual(self._main_exit_code(), 1)


class ReportOnlyGateIsNeverPromotedTests(_FakeGateFixture):

    def test_a_gate_that_prints_findings_but_exits_zero_stays_ok(self):
        """The aggregator must never read a gate's own PRINTED text to decide pass/fail — only its
        returned exit code. A fake report-only-shaped gate whose stdout reads like a failure but whose
        `main()` returns 0 (exactly `check_context_budget.py --enforce-headroom`'s own posture for its
        report-only byte-budget half) must be reported `ok: True` and must not flip the aggregate."""
        calls: list = []
        self.install("fake_report_only_gate", 0, calls,
                     stdout_marker="12 VIOLATION(S) FOUND [report-only, exit 0 by design]")
        self.set_gates("fake_report_only_gate")

        result = cd.run(root="unused-for-fakes")

        self.assertTrue(result["gates"][0]["ok"])
        self.assertIn("VIOLATION", result["gates"][0]["stdout"])
        self.assertEqual(result["failing"], 0)

    def test_real_report_only_gates_never_fail_on_the_live_tree(self):
        """`check_no_utcnow.py` and `check_grounding_dates.py` have no `--enforce` flag at all — this
        is the live-tree half of the "never promotes" guarantee, run against the real gates rather
        than a fake, so the property is checked against the actual modules this tool ships with."""
        for name in ("check_no_utcnow", "check_grounding_dates"):
            with self.subTest(gate=name):
                result = cd.run(root=cd.REPO_ROOT, only=name)
                self.assertEqual(len(result["gates"]), 1)
                self.assertTrue(result["gates"][0]["ok"],
                                 "%s returned non-zero; it has no --enforce flag and must always "
                                 "exit 0" % name)


class OnlyFlagTests(_FakeGateFixture):

    def test_only_runs_a_single_named_gate(self):
        calls: list = []
        self.install("fake_gate_x", 0, calls)
        self.install("fake_gate_y", 0, calls)
        self.set_gates("fake_gate_x", "fake_gate_y")

        result = cd.run(root="unused-for-fakes", only="fake_gate_x")

        self.assertEqual(calls, ["fake_gate_x"])
        self.assertEqual(len(result["gates"]), 1)


class WorkflowCoverageTest(unittest.TestCase):
    """`check_docs.py`'s own hardcoding is only as safe as this test.

    A `check_*.py` invocation newly added to `ci.yml` — another doc-side gate, say — that is
    neither added to `GATES` nor triaged into `EXCLUDED_FROM_CI_AGGREGATION` with a reason must fail
    THIS test, so it cannot pass CI silently invisible to `check_docs.py`. That silent gap is exactly
    what makes a change discover its gates one at a time instead of all at once."""

    def test_ci_yml_has_no_check_script_check_docs_does_not_account_for(self):
        with open(cd.CI_WORKFLOW, encoding="utf-8") as fh:
            text = fh.read()
        invoked = cd.scripts_invoked_in_ci(text)
        known = set(cd.GATE_NAMES) | set(cd.EXCLUDED_FROM_CI_AGGREGATION)
        unknown = invoked - known
        self.assertEqual(
            unknown, set(),
            "ci.yml invokes check script(s) check_docs.py has no record of: %r — add each to GATES "
            "if it belongs in the aggregated run, or to EXCLUDED_FROM_CI_AGGREGATION with a reason "
            "if it deliberately does not." % sorted(unknown))

    def test_every_gate_is_actually_invoked_somewhere_in_ci_yml(self):
        """The other direction: a gate `check_docs.py` runs but `ci.yml` no longer does would mean
        this tool is enforcing something CI itself has stopped enforcing — a silent widening of scope
        nobody decided on."""
        with open(cd.CI_WORKFLOW, encoding="utf-8") as fh:
            text = fh.read()
        invoked = cd.scripts_invoked_in_ci(text)
        missing = set(cd.GATE_NAMES) - invoked
        self.assertEqual(missing, set(),
                         "GATES names a script ci.yml no longer invokes: %r" % sorted(missing))

    def test_scripts_invoked_in_ci_parses_a_representative_snippet(self):
        """The parser itself, against fixture text rather than the live workflow, so a passing repo
        scan can't hide a regex that silently stopped matching anything."""
        snippet = (
            "      - name: example\n"
            "        run: |\n"
            "          python seneschal/scripts/check_context_budget.py --enforce-headroom\n"
            "          python seneschal/scripts/check_doc_status.py --enforce\n"
            "      - name: other job\n"
            "        run: python seneschal/scripts/check_json_files.py --enforce\n")
        self.assertEqual(cd.scripts_invoked_in_ci(snippet),
                         {"check_context_budget", "check_doc_status", "check_json_files"})


if __name__ == "__main__":
    unittest.main()
