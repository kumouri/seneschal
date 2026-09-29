#!/usr/bin/env python3
"""Tests for `ci_local.py` — the run-every-CI-gate-here-first runner.

Two classes of test:

* **The runner's own plumbing**, against an injected fake runner and fake skip probes — so the
  behaviour pinned here (ci.yml order, stop-at-first-failing-command inside a step, exit 1 iff a
  step FAILED, a SKIP is never counted as a pass and is NAMED in the summary, `--only` selects,
  a failed base fetch degrades rather than aborts) is proven without running one real CI step.
* **`WorkflowMirrorTest`**, the reason the step table can be hardcoded at all: it scans `ci.yml`
  for every `python seneschal/scripts/<x>.py <flags>` invocation and requires `STEPS` to carry the
  same script with the same flags, verbatim. A gate added to CI but not here is exactly the gap
  that lets a change push red, so this is what closes it.

Run:  python -m unittest seneschal.scripts.test_ci_local
"""
from __future__ import annotations

import io
import os
import re
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ci_local as cl  # noqa: E402


class FakeRunner:
    """Records every argv it is handed; answers from a table keyed on the argv's first token after
    the interpreter (or on the whole joined line for a substring match)."""

    def __init__(self, failing=(), exit_codes=None):
        self.calls = []
        self.failing = tuple(failing)
        self.exit_codes = dict(exit_codes or {})

    def __call__(self, argv, cwd):
        line = " ".join(str(a) for a in argv)
        self.calls.append((line, cwd))
        for key, rc in self.exit_codes.items():
            if key in line:
                return rc, "fake output for %s\n" % key
        for key in self.failing:
            if key in line:
                return 1, "fake failure for %s\n" % key
        return 0, "ok\n"


def fake_step(name, commands, skip=None, job="fake"):
    return cl.step(name, job, commands, skip=skip)


class _StepsFixture(unittest.TestCase):
    def setUp(self):
        self._orig = cl.STEPS

    def tearDown(self):
        cl.STEPS = self._orig

    def set_steps(self, *steps):
        cl.STEPS = tuple(steps)

    def run_all(self, runner, only=None, fetch=False):
        # `tree_note=False`: these tests count the runner's calls step by step; the working-tree
        # header line has its own tests in `WorkingTreeNoteTests`.
        out = io.StringIO()
        report = cl.run(only=only, runner=runner, root="unused-for-fakes", out=out, fetch=fetch,
                        tree_note=False)
        return report, out.getvalue()


class OrderAndVerdictTests(_StepsFixture):

    def test_steps_run_in_table_order_and_every_step_runs_after_a_failure(self):
        """A failing step must not hide the ones after it — the whole point is to see every
        objection on the first pass, not one CI visit at a time."""
        self.set_steps(fake_step("a", [["cmd-a"]]), fake_step("b", [["cmd-b"]]),
                       fake_step("c", [["cmd-c"]]))
        runner = FakeRunner(failing=("cmd-a",))
        report, _ = self.run_all(runner)
        self.assertEqual([c[0] for c in runner.calls], ["cmd-a", "cmd-b", "cmd-c"])
        self.assertEqual(report["failed"], ["a"])
        self.assertEqual(report["passed"], ["b", "c"])

    def test_a_step_stops_at_its_first_failing_command(self):
        """Inside ONE step (`npm run typecheck && npm test`), the second command never runs when
        the first fails — that is what `&&` means in CI and a test run over a type error is noise."""
        self.set_steps(fake_step("npm", [["typecheck"], ["test"], ["build"]]))
        runner = FakeRunner(failing=("test",))
        report, _ = self.run_all(runner)
        self.assertEqual([c[0] for c in runner.calls], ["typecheck", "test"])
        self.assertEqual(report["results"][0]["exit_code"], 1)

    def test_verdict_is_the_commands_own_exit_code_never_its_output(self):
        """A report-only gate prints findings and exits 0; the runner must never promote that."""
        self.set_steps(fake_step("report-only", [["noisy"]]))

        def runner(argv, cwd):
            return 0, "12 VIOLATION(S) FOUND — report-only, exit 0 by design\n"
        report, _ = self.run_all(runner)
        self.assertEqual(report["passed"], ["report-only"])
        self.assertEqual(report["exit_code"], 0)

    def test_callable_commands_are_resolved_only_when_the_step_runs(self):
        resolved = []

        def build():
            resolved.append(True)
            return [["late"]]
        self.set_steps(fake_step("lazy", build))
        self.assertEqual(resolved, [])
        runner = FakeRunner()
        self.run_all(runner)
        self.assertEqual(resolved, [True])
        self.assertEqual(runner.calls[0][0], "late")

    def test_cwd_is_joined_onto_the_root(self):
        cl.STEPS = (cl.step("web", "cockpit-web", [["npm"]], cwd="cockpit/web"),)
        runner = FakeRunner()
        self.run_all(runner)
        self.assertEqual(runner.calls[0][1], os.path.join("unused-for-fakes", "cockpit/web"))


class ExitCodeTests(_StepsFixture):

    def test_all_pass_is_exit_zero(self):
        self.set_steps(fake_step("a", [["x"]]), fake_step("b", [["y"]]))
        report, _ = self.run_all(FakeRunner())
        self.assertEqual(report["exit_code"], 0)

    def test_one_failure_among_many_is_exit_one(self):
        self.set_steps(fake_step("a", [["x"]]), fake_step("b", [["y"]]), fake_step("c", [["z"]]))
        report, _ = self.run_all(FakeRunner(failing=("y",)))
        self.assertEqual(report["exit_code"], 1)

    def test_many_failures_is_still_exit_one_not_a_count(self):
        self.set_steps(fake_step("a", [["x"]]), fake_step("b", [["y"]]))
        report, _ = self.run_all(FakeRunner(exit_codes={"x": 2, "y": 127}))
        self.assertEqual(report["exit_code"], 1)

    def test_main_returns_the_reports_exit_code(self):
        self.set_steps(fake_step("a", [["x"]]))
        saved = sys.stdout
        sys.stdout = io.StringIO()
        try:
            self.assertEqual(cl.main(["--no-fetch"], runner=FakeRunner(failing=("x",))), 1)
            self.assertEqual(cl.main(["--no-fetch"], runner=FakeRunner()), 0)
        finally:
            sys.stdout = saved


class SkipTests(_StepsFixture):

    def test_a_skipped_step_runs_nothing_and_carries_its_reason(self):
        self.set_steps(fake_step("npm", [["npm", "test"]],
                                 skip=lambda: "runner/node_modules is absent — run `npm ci`"))
        runner = FakeRunner()
        report, text = self.run_all(runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(report["skipped"], ["npm"])
        self.assertIn("SKIP", text)
        self.assertIn("node_modules is absent", text)

    def test_a_skip_is_not_green_the_summary_names_it(self):
        """The load-bearing one: a run with a skip must read as a partial run. 'N skipped' is in
        the totals AND the skipped step is named, so nobody mistakes it for a full green."""
        self.set_steps(fake_step("ok", [["x"]]),
                       fake_step("cockpit", [["y"]], skip=lambda: "extras absent"))
        report, text = self.run_all(FakeRunner())
        self.assertEqual(report["exit_code"], 0)
        self.assertNotIn("cockpit", report["passed"])
        self.assertIn("1 passed, 0 failed, 1 skipped", text)
        self.assertIn("NOT checked here: cockpit", text)
        self.assertIn("not a full run", text)

    def test_no_skips_and_no_failures_reads_as_a_full_run(self):
        self.set_steps(fake_step("a", [["x"]]), fake_step("b", [["y"]]))
        _, text = self.run_all(FakeRunner())
        self.assertIn("2 passed, 0 failed, 0 skipped", text)
        self.assertIn("every CI gate ran here", text)

    def test_a_green_only_subset_never_claims_a_full_run(self):
        self.set_steps(fake_step("a", [["x"]]), fake_step("b", [["y"]]))
        _, text = self.run_all(FakeRunner(), only=["a"])
        self.assertIn("1 passed, 0 failed, 0 skipped", text)
        self.assertIn("--only run, not a full run", text)
        self.assertNotIn("every CI gate ran here", text)

    def test_a_failure_outranks_skips_in_the_summary(self):
        self.set_steps(fake_step("bad", [["x"]]),
                       fake_step("skipped", [["y"]], skip=lambda: "no toolchain"))
        report, text = self.run_all(FakeRunner(failing=("x",)))
        self.assertEqual(report["exit_code"], 1)
        self.assertIn("RED — fix before pushing: bad", text)

    def test_a_skip_probe_returning_none_means_run(self):
        self.set_steps(fake_step("a", [["x"]], skip=lambda: None))
        runner = FakeRunner()
        report, _ = self.run_all(runner)
        self.assertEqual(report["passed"], ["a"])
        self.assertEqual(len(runner.calls), 1)

    def test_the_python_only_steps_have_no_skip_probe(self):
        """Everything in ci.yml's python + references jobs needs only Python + git, so nothing
        there may ever SKIP — except `uv lock --check`, which needs uv, and the PowerShell parse,
        which needs pwsh. That job is where a prose gate goes red, and it must always be fully
        runnable here."""
        for s in cl.STEPS:
            if s["job"] in ("python", "references") and s["name"] not in ("uv-lock", "ps-syntax"):
                self.assertIsNone(s["skip"], "%s must not be skippable" % s["name"])
        self.assertIn("py-compile", cl.ALWAYS_RUNNABLE)
        self.assertIn("rulings", cl.ALWAYS_RUNNABLE)
        self.assertNotIn("uv-lock", cl.ALWAYS_RUNNABLE)


class OnlyAndListTests(_StepsFixture):

    def test_only_selects_named_steps_in_table_order(self):
        self.set_steps(fake_step("a", [["x"]]), fake_step("b", [["y"]]), fake_step("c", [["z"]]))
        runner = FakeRunner()
        report, _ = self.run_all(runner, only=["c", "a"])
        self.assertEqual([c[0] for c in runner.calls], ["x", "z"])
        self.assertEqual(len(report["results"]), 2)

    def test_only_with_an_unknown_name_refuses(self):
        self.set_steps(fake_step("a", [["x"]]))
        with self.assertRaises(SystemExit):
            cl.select_steps(["nope"])

    def test_list_prints_every_step_without_running_anything(self):
        self.set_steps(fake_step("a", [["x"]]), fake_step("b", lambda: [["y"]],
                                                          skip=lambda: "never called"))
        out = io.StringIO()
        cl.list_steps(out)
        text = out.getvalue()
        self.assertIn("a ", text)
        self.assertIn("b ", text)
        self.assertIn("may SKIP", text)

    def test_the_real_table_lists_with_no_host_probe(self):
        """`--list` on the real table must not spawn npm/pwsh/uv — it is a lookup, not a run."""
        out = io.StringIO()
        cl.list_steps(out)
        for name in cl.STEP_NAMES:
            self.assertIn(name, out.getvalue())


class WorkingTreeNoteTests(unittest.TestCase):
    """The header names the tree the run checked, so a local PASS and a CI FAIL on the
    same gate can be told apart by whether the local run saw uncommitted changes."""

    def test_counts_modified_and_untracked_from_porcelain(self):
        class Runner(FakeRunner):
            def __call__(self, argv, cwd):
                self.calls.append((" ".join(argv), cwd))
                return 0, " M seneschal/docs/a.md\nA  seneschal/scripts/b.py\n?? seneschal/scripts/test_c.py\n"
        runner = Runner()
        note = cl.working_tree_note(runner, "root")
        self.assertEqual(note, "working tree: 2 modified, 1 untracked "
                               "(gates diff the working tree, not HEAD)")
        self.assertIn("status --porcelain --untracked-files=all", runner.calls[0][0])
        self.assertIn("-c core.fsmonitor=false", runner.calls[0][0])

    def test_a_clean_tree_reads_zero_zero(self):
        class Runner(FakeRunner):
            def __call__(self, argv, cwd):
                return 0, ""
        self.assertIn("0 modified, 0 untracked", cl.working_tree_note(Runner(), "root"))

    def test_a_git_failure_is_reported_not_guessed(self):
        runner = FakeRunner(exit_codes={"status": 128})
        self.assertIn("unknown", cl.working_tree_note(runner, "root"))

    def test_run_prints_the_line_even_without_a_fetch(self):
        """`--no-fetch` skips the base-ref preamble; it must not also lose the tree line."""
        orig = cl.STEPS
        try:
            cl.STEPS = (fake_step("one", [["x"]]),)
            out = io.StringIO()
            cl.run(runner=FakeRunner(), root="unused-for-fakes", out=out, fetch=False)
        finally:
            cl.STEPS = orig
        self.assertIn("working tree:", out.getvalue())
        self.assertIn("gates diff the working tree, not HEAD", out.getvalue())

    def test_the_line_precedes_the_first_verdict(self):
        orig = cl.STEPS
        try:
            cl.STEPS = (fake_step("one", [["x"]]),)
            out = io.StringIO()
            cl.run(runner=FakeRunner(), root="unused-for-fakes", out=out, fetch=False)
        finally:
            cl.STEPS = orig
        text = out.getvalue()
        self.assertLess(text.index("working tree:"), text.index("PASS"))


class BaseRefTests(unittest.TestCase):

    def test_present_ref_is_not_fetched(self):
        runner = FakeRunner()
        note = cl.ensure_base_ref(runner, "root")
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("rev-parse", runner.calls[0][0])
        self.assertIn("present", note)

    def test_missing_ref_is_fetched_once(self):
        runner = FakeRunner(exit_codes={"rev-parse": 1})
        note = cl.ensure_base_ref(runner, "root")
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("fetch origin develop", runner.calls[1][0])
        self.assertIn("fetched", note)

    def test_failed_fetch_degrades_and_says_so_never_raises(self):
        """CI on a missing ref runs the gates without a baseline; so does this. The run goes on."""
        runner = FakeRunner(exit_codes={"rev-parse": 1, "fetch": 128})
        note = cl.ensure_base_ref(runner, "root")
        self.assertIn("MISSING", note)
        self.assertIn("without a baseline", note)

    def test_git_is_invoked_with_fsmonitor_off(self):
        runner = FakeRunner()
        cl.ensure_base_ref(runner, "root")
        self.assertIn("-c core.fsmonitor=false", runner.calls[0][0])


CI_INVOCATION_RE = re.compile(r"^\s*(?:run:\s*)?python seneschal/scripts/(\w+)\.py([^\n|]*)", re.M)
SHELL_CONTINUATION_RE = re.compile(r"\\\r?\n\s*")


def invocations_in_ci(text: str) -> set:
    """Every `python seneschal/scripts/<x>.py <flags>` line in ci.yml as `(script, flags-tuple)`,
    shell continuation lines (backslash-newline) joined first, and `--summary-file
    "$GITHUB_STEP_SUMMARY"` (CI-only plumbing) dropped."""
    text = SHELL_CONTINUATION_RE.sub(" ", text)
    found = set()
    for m in CI_INVOCATION_RE.finditer(text):
        flags = m.group(2).split()
        if "--summary-file" in flags:
            i = flags.index("--summary-file")
            del flags[i:i + 2]
        found.add((m.group(1), tuple(flags)))
    return found


def invocations_in_steps() -> set:
    found = set()
    for s in cl.STEPS:
        if callable(s["commands"]):
            continue
        for argv in s["commands"]:
            if len(argv) >= 2 and argv[0] == sys.executable:
                rel = argv[1].replace("\\", "/")
                if rel.startswith("seneschal/scripts/") and rel.endswith(".py"):
                    found.add((rel[len("seneschal/scripts/"):-3], tuple(argv[2:])))
    return found


class WorkflowMirrorTest(unittest.TestCase):
    """The hardcoded `STEPS` table is only as safe as this test."""

    def _ci_text(self):
        with open(cl.CI_WORKFLOW, encoding="utf-8") as fh:
            return fh.read()

    def test_every_script_invocation_in_ci_yml_is_in_steps_with_the_same_flags(self):
        """A gate CI runs that this runner does not, or runs with different flags, is exactly how a
        change pushes red. `--enforce` where CI enforces, bare where CI reports — verbatim."""
        ci = invocations_in_ci(self._ci_text())
        self.assertTrue(ci, "parsed nothing out of ci.yml — the regex is broken")
        missing = ci - invocations_in_steps()
        self.assertEqual(missing, set(),
                         "ci.yml invokes these but STEPS does not (or with other flags): %r"
                         % sorted(missing))

    def test_every_script_step_here_is_still_in_ci_yml(self):
        """The other direction — this runner must not enforce something CI stopped enforcing."""
        extra = invocations_in_steps() - invocations_in_ci(self._ci_text())
        self.assertEqual(extra, set(), "STEPS runs these but ci.yml does not: %r" % sorted(extra))

    def test_the_discovery_roots_match_ci(self):
        roots = set(re.findall(r"unittest discover -s (\S+)", self._ci_text()))
        here = set()
        for s in cl.STEPS:
            if not callable(s["commands"]):
                for argv in s["commands"]:
                    if "discover" in argv:
                        here.add(argv[argv.index("-s") + 1])
        # cockpit roots are built lazily (their interpreter is probed at run time) — add them.
        here |= {"cockpit/server", "cockpit/decoy", "cockpit/breakglass"}
        self.assertEqual(roots, here)

    def test_parser_reads_a_representative_snippet(self):
        snippet = (
            "        run: |\n"
            "          python seneschal/scripts/count_tests.py --check \\\n"
            "            --compare-to origin/develop \\\n"
            '            --summary-file "$GITHUB_STEP_SUMMARY"\n'
            "          python seneschal/scripts/check_doc_status.py --enforce\n"
            "      - name: x\n"
            "        run: python seneschal/scripts/check_grounding_dates.py\n")
        got = invocations_in_ci(snippet)
        self.assertIn(("check_doc_status", ("--enforce",)), got)
        self.assertIn(("check_grounding_dates", ()), got)
        # continuation lines joined; --summary-file (CI-only plumbing) dropped
        self.assertIn(("count_tests", ("--check", "--compare-to", "origin/develop")), got)

    def test_count_tests_carries_compare_to_origin_develop(self):
        """Pinned explicitly as well as via the mirror test: the base-ref comparison is the half
        of count_tests that needs origin/develop, and it is what `ensure_base_ref` exists for."""
        for s in cl.STEPS:
            if s["name"] == "count-tests":
                argv = s["commands"][0]
                self.assertIn("--check", argv)
                self.assertIn("--compare-to", argv)
                self.assertEqual(argv[argv.index("--compare-to") + 1], "origin/develop")
                return
        self.fail("no count-tests step")

    def test_uv_lock_check_is_a_step(self):
        self.assertIn("uv-lock", cl.STEP_NAMES)
        self.assertIn("uv lock --check", self._ci_text())


if __name__ == "__main__":
    unittest.main()
