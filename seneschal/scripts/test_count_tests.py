"""Tests for count_tests.py — the measured suite counts that replaced the prose ones.

Fixtures are throwaway directories and a fake `git`; nothing here spawns git, creates a worktree,
or asserts against the live tree's *content*. The one exception is deliberate and is producer
coverage rather than a content assertion: `TheLiveRoutersAreClean` runs the guard over the real
`CLAUDE.md` and `README.md`, which can only go red when someone writes a suite total
back into one of them — which is exactly when it should.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import count_tests  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

_UNIQUE = itertools.count()


def _write_suite(root: Path, module_specs: dict[str, int]) -> Path:
    """Write a fixture suite: {module_stem: number_of_test_methods}. Module names are made unique
    per fixture so that in-process discovery cannot serve one test's module out of sys.modules to
    another — the same collision the CLI avoids by using one interpreter per suite."""
    tag = next(_UNIQUE)
    root.mkdir(parents=True, exist_ok=True)
    for stem, n in module_specs.items():
        methods = "\n".join(f"    def test_{i}(self):\n        pass" for i in range(n)) or "    pass"
        (root / f"test_{stem}_{tag}.py").write_text(
            "import unittest\n\n\nclass Fixture(unittest.TestCase):\n" + methods + "\n",
            encoding="utf-8",
        )
    return root


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test-count-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class CountingIsMeasurementNotArithmetic(_TmpCase):
    """The number comes off the loader, never off a stored figure plus a delta."""

    def test_counts_every_case_in_every_module(self):
        _write_suite(self.tmp, {"alpha": 7, "beta": 3})
        result = count_tests.count_one(self.tmp)
        self.assertEqual(result["tests"], 10)
        self.assertEqual(result["modules"], 2)
        self.assertEqual(result["errors"], [])

    def test_a_module_with_no_tests_contributes_nothing(self):
        _write_suite(self.tmp, {"alpha": 4, "empty": 0})
        self.assertEqual(count_tests.count_one(self.tmp)["tests"], 4)

    def test_an_empty_tree_is_zero_and_not_an_error(self):
        self.tmp.mkdir(exist_ok=True)
        result = count_tests.count_one(self.tmp)
        self.assertEqual((result["tests"], result["errors"]), (0, []))

    def test_nothing_is_executed_while_counting(self):
        """A count that ran the suite would cost minutes and would be unusable in a fast job."""
        marker = self.tmp / "ran.txt"
        self.tmp.mkdir(exist_ok=True)
        (self.tmp / f"test_side_effect_{next(_UNIQUE)}.py").write_text(
            "import unittest\nfrom pathlib import Path\n\n\n"
            "class Fixture(unittest.TestCase):\n"
            f"    def test_writes(self):\n        Path(r{str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        self.assertEqual(count_tests.count_one(self.tmp)["tests"], 1)
        self.assertFalse(marker.exists(), "counting must not run the test body")

    def test_a_missing_suite_directory_is_reported_not_crashed(self):
        rows = count_tests.measure(root=self.tmp, suites=(("ghost", "no/such/dir"),))
        self.assertEqual(rows[0]["tests"], 0)
        self.assertFalse(rows[0]["present"])
        self.assertTrue(rows[0]["errors"])


class AnUnimportableModuleIsAnErrorNotATest(_TmpCase):
    """unittest hands back a synthetic case for a module it could not import. Counting it would
    inflate the total by one per broken module and read as a healthy suite."""

    def _broken(self):
        self.tmp.mkdir(exist_ok=True)
        (self.tmp / f"test_broken_{next(_UNIQUE)}.py").write_text(
            "import a_module_that_does_not_exist_anywhere\n", encoding="utf-8")

    def test_the_failure_is_not_counted_as_a_test(self):
        _write_suite(self.tmp, {"good": 5})
        self._broken()
        result = count_tests.count_one(self.tmp)
        self.assertEqual(result["tests"], 5)

    def test_the_failure_is_reported(self):
        self._broken()
        self.assertTrue(count_tests.count_one(self.tmp)["errors"])

    def test_main_exits_1_when_a_suite_cannot_be_measured(self):
        _write_suite(self.tmp / "suite", {"good": 2})
        self._broken_in(self.tmp / "suite")
        code = count_tests.main(
            ["--root", str(self.tmp)],
            runner=lambda d: count_tests.count_one(d),
        )
        self.assertEqual(code, 1)

    def _broken_in(self, where: Path):
        where.mkdir(parents=True, exist_ok=True)
        (where / f"test_broken_{next(_UNIQUE)}.py").write_text(
            "import a_module_that_does_not_exist_anywhere\n", encoding="utf-8")

    def setUp(self):
        super().setUp()
        # main() measures count_tests.SUITES; point it at the fixture for the exit-code test.
        self._real = count_tests.SUITES
        count_tests.SUITES = (("fixture", "suite"),)
        self.addCleanup(lambda: setattr(count_tests, "SUITES", self._real))


class TheGuardMatchesTheTotalAndNotTheInventory(_TmpCase):
    """The precision half. A guard that also flagged per-module annotations would fire on dozens of
    lines of a test inventory on a clean tree, and a check that cries wolf gets disabled."""

    def _guard(self, text: str, rel: str = "CLAUDE.md"):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return count_tests.guard(root=self.tmp, files=(rel,))

    # --- the forms the deleted totals actually took, verbatim ---

    def test_the_roots_ci_sentence_is_flagged(self):
        found = self._guard("  suite (3428 tests), the archon suite (102), the sim suite\n")
        self.assertEqual(len(found), 2, found)

    def test_the_inventorys_glob_form_is_flagged(self):
        found = self._guard(
            "unittest suite** (`seneschal/scripts/test_*.py` — **3428 tests as of 2026-08-16**, measured\n")
        self.assertEqual(len(found), 1, found)

    def test_a_second_suites_glob_form_is_flagged(self):
        found = self._guard(
            "**the archon suite** (`archons/example/tools/test_*.py` — 102 tests as of 2026-08-09,\n")
        self.assertEqual(len(found), 1, found)

    def test_a_comma_grouped_total_is_flagged(self):
        self.assertEqual(len(self._guard("the stdlib suite (3,428 tests) runs\n")), 1)

    # --- the forms that are inventory, not the total, and must pass ---

    def test_a_per_module_count_in_parentheses_is_not_flagged(self):
        self.assertEqual(self._guard(
            "`test_pending_checks.py` (62) for the pending-check register\n"), [])

    def test_a_per_module_count_in_prose_is_not_flagged(self):
        self.assertEqual(self._guard(
            "`test_post_watch.py` for the post watcher — 65 tests over four families,\n"), [])

    def test_a_quoted_fixture_string_is_not_flagged(self):
        """'429 tests passed' is a verbatim classifier fixture in the inventory."""
        self.assertEqual(self._guard(
            'terminal too ("429 tests passed", "test timed out", `notion-rate-limits.md`,\n'), [])

    def test_a_historical_figure_in_prose_is_not_flagged(self):
        self.assertEqual(self._guard(
            "`develop` had gained 10 tests across two PRs without either PR moving the number\n"), [])

    def test_the_glob_alone_is_not_flagged(self):
        self.assertEqual(self._guard(
            "runs the stdlib suite (`seneschal/scripts/test_*.py`), counted by CI on every run\n"), [])

    def test_a_missing_router_is_not_a_finding(self):
        self.assertEqual(count_tests.guard(root=self.tmp, files=("nope.md",)), [])

    def test_the_finding_carries_the_line_number(self):
        found = self._guard("a\nb\nthe stdlib suite (3428 tests)\n")
        self.assertEqual(found[0]["line"], 3)


class TheLiveRoutersAreClean(unittest.TestCase):
    """Producer coverage: the guard is pointed at the real files, and they pass. This is the test
    that goes red the day someone types a total back in — which is the point of the whole change."""

    def test_no_tracked_router_states_a_suite_total(self):
        findings = count_tests.guard(root=REPO_ROOT)
        self.assertEqual(findings, [], f"a suite total came back: {findings}")

    def test_the_guarded_files_exist(self):
        for rel in count_tests.GUARDED_FILES:
            self.assertTrue((REPO_ROOT / rel).is_file(), rel)


class CiRunsNoSuiteThisScriptCannotSee(unittest.TestCase):
    """The registry failure mode, closed: a new `discover -s` in CI must be either counted here or
    listed as deliberately skipped. Asserts against the workflow, never against a count."""

    def test_every_discovered_root_in_ci_is_accounted_for(self):
        ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        roots = set(re.findall(r"unittest discover -s (\S+)", ci))
        self.assertTrue(roots, "no discover steps found — did the workflow change shape?")
        known = {rel for _, rel in count_tests.SUITES} | set(count_tests.NOT_COUNTED)
        self.assertEqual(roots - known, set(), "CI discovers a suite count_tests.py cannot see")

    def test_every_counted_suite_is_actually_run_by_ci(self):
        ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        roots = set(re.findall(r"unittest discover -s (\S+)", ci))
        for _, rel in count_tests.SUITES:
            self.assertIn(rel, roots, f"{rel} is counted but CI does not run it")

    def test_ci_invokes_the_counter(self):
        ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("count_tests.py", ci)
        self.assertIn("--check", ci)


class _FakeGit:
    """Records argv; answers from a scripted table. Spawns nothing."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        key = args[0] if args else ""
        rc, out, err = self.answers.get(key, (0, "", ""))
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()


class TheBaselineNeverCostsTheRun(_TmpCase):
    """Every unhappy path degrades to 'no baseline', with a note. A comparison that could fail CI
    would be a new failure mode bolted onto a check whose purpose is removing one."""

    def _ok_answers(self, **over):
        answers = {"merge-base": (0, "abc123def4567\n", ""), "rev-parse": (0, "head9999\n", ""),
                   "worktree": (0, "", "")}
        answers.update(over)
        return answers

    def test_a_missing_ref_is_a_note_not_a_failure(self):
        git = _FakeGit({"merge-base": (128, "", "fatal: Not a valid object name")})
        counts, note = count_tests.baseline_counts("origin/develop", root=self.tmp, git=git)
        self.assertIsNone(counts)
        self.assertIn("no merge base", note)

    def test_head_on_the_base_skips_the_comparison(self):
        git = _FakeGit({"merge-base": (0, "same\n", ""), "rev-parse": (0, "same\n", "")})
        counts, note = count_tests.baseline_counts("origin/develop", root=self.tmp, git=git)
        self.assertIsNone(counts)
        self.assertIn("nothing to compare", note)
        self.assertNotIn("worktree", [c[0] for c in git.calls])

    def test_a_failed_worktree_add_is_a_note_not_a_failure(self):
        git = _FakeGit(self._ok_answers(worktree=(1, "", "fatal: is already checked out")))
        counts, note = count_tests.baseline_counts(
            "origin/develop", root=self.tmp, git=git, mkdtemp=lambda: str(self.tmp / "base"))
        self.assertIsNone(counts)
        self.assertIn("could not create a baseline worktree", note)

    def test_the_worktree_removal_is_never_forced(self):
        """delegated-work-isolation-spec.md: the refusal IS the safety mechanism."""
        git = _FakeGit(self._ok_answers())
        count_tests.baseline_counts("origin/develop", root=self.tmp, git=git,
                                    runner=lambda d: {"tests": 1, "modules": 1, "errors": []},
                                    mkdtemp=lambda: str(self.tmp / "base"))
        removals = [c for c in git.calls if c[:2] == ("worktree", "remove")]
        self.assertEqual(len(removals), 1)
        self.assertNotIn("--force", removals[0])

    def test_the_worktree_is_cut_from_the_merge_base_sha(self):
        git = _FakeGit(self._ok_answers())
        count_tests.baseline_counts("origin/develop", root=self.tmp, git=git,
                                    runner=lambda d: {"tests": 1, "modules": 1, "errors": []},
                                    mkdtemp=lambda: str(self.tmp / "base"))
        add = [c for c in git.calls if c[:2] == ("worktree", "add")][0]
        self.assertIn("--detach", add)
        self.assertEqual(add[-1], "abc123def4567")

    def test_a_refused_removal_is_reported_not_swallowed(self):
        git = _FakeGit(self._ok_answers())
        git.answers["worktree"] = (0, "", "")

        calls = {"n": 0}

        def flaky(*args):
            git.calls.append(args)
            if args[:2] == ("worktree", "remove"):
                return type("R", (), {"returncode": 1, "stdout": "", "stderr": "contains modified files"})()
            return git(*args)

        counts, note = count_tests.baseline_counts(
            "origin/develop", root=self.tmp, git=flaky,
            runner=lambda d: {"tests": 1, "modules": 1, "errors": []},
            mkdtemp=lambda: str(self.tmp / "base"))
        self.assertIsNotNone(counts)
        self.assertIn("left at", note)
        self.assertIn("contains modified files", note)
        del calls

    def test_a_suite_absent_from_the_baseline_reads_as_new(self):
        git = _FakeGit(self._ok_answers())
        counts, _ = count_tests.baseline_counts(
            "origin/develop", root=self.tmp, git=git,
            runner=lambda d: {"tests": 3, "modules": 1, "errors": []},
            mkdtemp=lambda: str(self.tmp / "base"))
        # No suite directory exists under the fake worktree, so every suite is None -> "new".
        self.assertTrue(all(v is None for v in counts.values()))
        self.assertEqual(count_tests._delta(3, None), "new")


class ADropIsReportedAndDoesNotBlock(unittest.TestCase):
    """Report-only is a decision, not timidity — pinned so that flipping it is a deliberate edit
    with a red test in front of it, the way the pointer check's report-only phase was."""

    ROWS = [{"suite": "stdlib", "path": "seneschal/scripts", "tests": 3400, "modules": 100, "errors": []}]

    def test_a_drop_is_named_with_both_figures(self):
        lost = count_tests.drops(self.ROWS, {"stdlib": 3428})
        self.assertEqual(len(lost), 1)
        self.assertIn("3428", lost[0])
        self.assertIn("3400", lost[0])
        self.assertIn("-28", lost[0])

    def test_growth_and_parity_are_silent(self):
        self.assertEqual(count_tests.drops(self.ROWS, {"stdlib": 3400}), [])
        self.assertEqual(count_tests.drops(self.ROWS, {"stdlib": 10}), [])

    def test_no_baseline_means_no_claim(self):
        self.assertEqual(count_tests.drops(self.ROWS, None), [])
        self.assertEqual(count_tests.drops(self.ROWS, {}), [])

    def test_a_new_suite_is_not_a_drop(self):
        self.assertEqual(count_tests.drops(self.ROWS, {"stdlib": None}), [])


class TheCliContract(_TmpCase):
    """Exit codes, and the two output paths CI depends on."""

    def setUp(self):
        super().setUp()
        _write_suite(self.tmp / "suite", {"alpha": 6})
        self._real = count_tests.SUITES
        count_tests.SUITES = (("fixture", "suite"),)
        self.addCleanup(lambda: setattr(count_tests, "SUITES", self._real))

    def _run(self, argv):
        return count_tests.main(["--root", str(self.tmp), *argv], runner=count_tests.count_one)

    def test_a_clean_tree_exits_0(self):
        (self.tmp / "CLAUDE.md").write_text("no totals here\n", encoding="utf-8")
        (self.tmp / "README.md").write_text("nor here\n", encoding="utf-8")
        self.assertEqual(self._run(["--check"]), 0)

    def test_a_prose_total_exits_2(self):
        (self.tmp / "CLAUDE.md").write_text("the stdlib suite (3428 tests) runs\n", encoding="utf-8")
        self.assertEqual(self._run(["--check"]), 2)

    def test_the_guard_is_opt_in(self):
        (self.tmp / "CLAUDE.md").write_text("the stdlib suite (3428 tests) runs\n", encoding="utf-8")
        self.assertEqual(self._run([]), 0, "counting alone must not enforce")

    def test_json_output_is_parseable(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self._run(["--json"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["suites"][0]["tests"], 6)

    def test_the_summary_file_is_appended_not_truncated(self):
        summary = self.tmp / "summary.md"
        summary.write_text("earlier step\n", encoding="utf-8")
        self._run(["--summary-file", str(summary)])
        text = summary.read_text(encoding="utf-8")
        self.assertIn("earlier step", text)
        self.assertIn("| fixture | 6 |", text)

    def test_an_unwritable_summary_never_costs_the_check(self):
        blocked = self.tmp / "dir-in-the-way"
        blocked.mkdir()
        self.assertEqual(self._run(["--summary-file", str(blocked)]), 0)

    def test_the_markdown_names_the_command_that_reproduces_it(self):
        rendered = count_tests.render_markdown(
            [{"suite": "stdlib", "path": "seneschal/scripts", "tests": 3428, "modules": 102, "errors": []}])
        self.assertIn("count_tests.py", rendered)


if __name__ == "__main__":
    unittest.main()
