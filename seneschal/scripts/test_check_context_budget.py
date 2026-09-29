#!/usr/bin/env python3
"""Tests for the grounding byte budget (`check_context_budget.py`).

The load-bearing class is **`RatchetTests`**: a raise without a matching `raises[]` entry fails,
one with passes, and a *lowering* always passes without ceremony. That asymmetry is the whole
mechanism — the escape hatch has to exist, and it has to leave a mark.

Tests run against **fixture git repos, never the live tree**. A check whose tests assert against
today's `CLAUDE.md` goes red the moment someone edits `CLAUDE.md`, which is the fastest possible route
to a disabled check — and this check exists precisely because that file changes constantly.

Run:  python -m unittest seneschal.scripts.test_check_context_budget
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import check_context_budget as cb  # noqa: E402


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def make_repo(files: dict) -> str:
    root = tempfile.mkdtemp()
    _git(root, "init", "-q", "-b", "develop")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    write(root, files)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def write(root: str, files: dict):
    for rel, body in files.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)


def budget_doc(budgets: dict) -> str:
    return json.dumps({"_basis": "test", "budgets": budgets}, indent=2)


def commit(root, msg="change"):
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


def commit_at(root, date_iso: str, msg="change"):
    """Commit with a CONTROLLED committer/author date, so trailing-growth tests can build a history
    with known day-spans instead of racing the wall clock. `date_iso` is a bare date
    (`"2026-08-01"`); noon UTC avoids any timezone-boundary ambiguity in the day-span arithmetic."""
    env = dict(os.environ)
    ts = f"{date_iso}T12:00:00+00:00"
    env["GIT_AUTHOR_DATE"] = ts
    env["GIT_COMMITTER_DATE"] = ts
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=root, capture_output=True, text=True, env=env)


class Measurement(unittest.TestCase):
    def test_crlf_is_normalised_so_line_endings_cannot_consume_budget(self):
        """git-object bytes, not working-tree bytes — so this box and CI agree exactly, and a PR that
        only changes line endings cannot eat someone's budget (§3.2)."""
        d = tempfile.mkdtemp()
        write(d, {"lf.md": "a\nb\nc\n", "crlf.md": "a\r\nb\r\nc\r\n"})
        self.assertEqual(cb.lf_bytes(os.path.join(d, "lf.md")),
                         cb.lf_bytes(os.path.join(d, "crlf.md")))

    def test_a_module_level_literal_is_measured_by_ast_not_by_import(self):
        """An import would need `websockets`; CI's dependency-free job has to run this (§3.1)."""
        d = tempfile.mkdtemp()
        write(d, {"m.py": 'import nonexistent_module_xyz\n\nGROUNDING = "hello"\nOTHER = 3\n'})
        self.assertEqual(cb.literal_bytes(os.path.join(d, "m.py"), "GROUNDING"), 5)

    def test_a_missing_literal_reads_none_rather_than_raising(self):
        d = tempfile.mkdtemp()
        write(d, {"m.py": "X = 1\n"})
        self.assertIsNone(cb.literal_bytes(os.path.join(d, "m.py"), "GROUNDING"))

    def test_token_figures_are_a_labelled_estimate(self):
        """At the MEASURED rate (~2.59 B/token) — not a 4:1
        guess, which would have said 1,000."""
        self.assertEqual(cb.est_tokens(4000), int(4000 / 2.59))
        self.assertNotEqual(cb.est_tokens(4000), 1000)


class RatchetTests(unittest.TestCase):
    """§3.4. The escape hatch must exist, and must leave a mark."""

    def setUp(self):
        self.root = make_repo({
            "CLAUDE.md": "x" * 100,
            "seneschal/context-budget.json": budget_doc(
                {"CLAUDE.md": {"tier": "A", "max_bytes": 100, "raises": [], "note": "n"}}),
        })
        _git(self.root, "branch", "-f", "origin/develop", "develop")

    def _check(self):
        return cb.check(self.root, "origin/develop")

    def test_at_budget_is_clean(self):
        self.assertEqual(self._check()["violations"], 0)

    def test_over_budget_is_reported(self):
        write(self.root, {"CLAUDE.md": "x" * 150})
        result = self._check()
        self.assertEqual(len(result["over"]), 1)
        self.assertEqual(result["over"][0]["delta"], 50)

    def test_a_raise_WITHOUT_a_raises_entry_fails(self):
        write(self.root, {"seneschal/context-budget.json": budget_doc(
            {"CLAUDE.md": {"tier": "A", "max_bytes": 500, "raises": [], "note": "n"}})})
        commit(self.root)
        self.assertEqual(len(self._check()["unratcheted"]), 1)

    def test_a_raise_WITH_a_matching_reasoned_entry_passes(self):
        write(self.root, {"seneschal/context-budget.json": budget_doc(
            {"CLAUDE.md": {"tier": "A", "max_bytes": 500, "note": "n",
                           "raises": [{"on": "2026-08-05", "from": 100, "to": 500,
                                       "reason": "Mouth phase 3 joins the grounding block."}]}})})
        commit(self.root)
        self.assertEqual(self._check()["unratcheted"], [])

    def test_a_raises_entry_whose_TO_does_not_match_fails(self):
        """A stale entry left over from a previous raise must not authorise a new one."""
        write(self.root, {"seneschal/context-budget.json": budget_doc(
            {"CLAUDE.md": {"tier": "A", "max_bytes": 900, "note": "n",
                           "raises": [{"on": "2026-08-05", "from": 100, "to": 500,
                                       "reason": "an older, smaller raise"}]}})})
        commit(self.root)
        self.assertEqual(len(self._check()["unratcheted"]), 1)

    def test_an_empty_reason_fails(self):
        """An exemption should be a decision someone wrote down."""
        write(self.root, {"seneschal/context-budget.json": budget_doc(
            {"CLAUDE.md": {"tier": "A", "max_bytes": 500, "note": "n",
                           "raises": [{"on": "2026-08-05", "from": 100, "to": 500, "reason": "   "}]}})})
        commit(self.root)
        self.assertEqual(len(self._check()["unratcheted"]), 1)

    def test_a_LOWERING_always_passes_without_ceremony(self):
        """The pawl only bites upward. Tightening must never need paperwork, or nobody tightens."""
        write(self.root, {"CLAUDE.md": "x" * 40,
                          "seneschal/context-budget.json": budget_doc(
                              {"CLAUDE.md": {"tier": "A", "max_bytes": 50, "raises": [], "note": "n"}})})
        commit(self.root)
        self.assertEqual(self._check()["violations"], 0)

    def test_absent_at_the_merge_base_bootstraps_cleanly(self):
        """Every entry is new and permitted — that is how this ships at all (§3.4)."""
        root = make_repo({"CLAUDE.md": "x" * 10})
        write(root, {"seneschal/context-budget.json": budget_doc(
            {"CLAUDE.md": {"tier": "A", "max_bytes": 10, "raises": [], "note": "n"}})})
        commit(root)
        _git(root, "branch", "-f", "origin/develop", "HEAD~1")
        result = cb.check(root, "origin/develop")
        self.assertFalse(result["merge_base_seen"])
        self.assertEqual(result["unratcheted"], [])


class UndeclaredFileTests(unittest.TestCase):
    """§3.1: otherwise the cheapest permanent way to satisfy this check is to create `CLAUDE-2.md`."""

    def test_a_new_grounding_file_with_no_entry_is_reported(self):
        root = make_repo({
            "CLAUDE.md": "x" * 10,
            "phone/CLAUDE.md": "y" * 20,
            "seneschal/context-budget.json": budget_doc(
                {"CLAUDE.md": {"tier": "A", "max_bytes": 10, "raises": [], "note": "n"}}),
        })
        result = cb.check(root, "origin/develop")
        self.assertEqual([r["artifact"] for r in result["undeclared"]], ["phone/CLAUDE.md"])
        self.assertEqual(result["undeclared"][0]["measured"], 20)

    def test_an_untracked_file_is_not_policed(self):
        """A scratch CLAUDE.md in an ignored directory is not grounding."""
        root = make_repo({
            "CLAUDE.md": "x" * 10,
            "seneschal/context-budget.json": budget_doc(
                {"CLAUDE.md": {"tier": "A", "max_bytes": 10, "raises": [], "note": "n"}}),
        })
        write(root, {"scratch/CLAUDE.md": "z" * 99})  # never added
        self.assertEqual(cb.check(root, "origin/develop")["undeclared"], [])

    def test_an_unbudgeted_path_scoped_RULE_FILE_is_reported(self):
        """§14.6's third door. A `.claude/rules/*.md` with `paths:` frontmatter is grounding — it is
        injected on a matching `Read`, obeyed as instruction, and re-attached after compaction — so
        moving prose out of a budgeted router into one would make the per-file number fall while the
        routed-tree total did not move. That is the `CLAUDE-2.md` trick one directory over, and this
        is the assertion that makes *"budgeted in the same PR, or not created"* a gate rather than a
        sentence in a spec."""
        root = make_repo({
            "CLAUDE.md": "x" * 10,
            ".claude/rules/archive-cluster.md": "---\npaths: [\"seneschal/scripts/archive_*.py\"]\n---\nz",
            "seneschal/context-budget.json": budget_doc(
                {"CLAUDE.md": {"tier": "A", "max_bytes": 10, "raises": [], "note": "n"}}),
        })
        result = cb.check(root, "origin/develop")
        self.assertEqual([r["artifact"] for r in result["undeclared"]],
                         [".claude/rules/archive-cluster.md"])
        # The reported size is the measured one, so the printed stanza can be pasted as-is.
        self.assertEqual(result["undeclared"][0]["measured"],
                         len(b"---\npaths: [\"seneschal/scripts/archive_*.py\"]\n---\nz"))

    def test_a_rule_file_WITH_a_budget_entry_is_clean(self):
        """The other half — declaring it is what satisfies the check, not deleting it."""
        body = "---\npaths: [\"seneschal/scripts/archive_*.py\"]\n---\nz"
        root = make_repo({
            "CLAUDE.md": "x" * 10,
            ".claude/rules/archive-cluster.md": body,
            "seneschal/context-budget.json": budget_doc({
                "CLAUDE.md": {"tier": "A", "max_bytes": 10, "raises": [], "note": "n"},
                ".claude/rules/archive-cluster.md": {
                    "tier": "B", "max_bytes": len(body.encode()), "raises": [], "note": "n"}}),
        })
        result = cb.check(root, "origin/develop")
        self.assertEqual(result["undeclared"], [])
        self.assertEqual(result["violations"], 0)

    def test_a_nested_rule_file_is_reached_too(self):
        """`**` not `*`: a hole in `BUDGETED_GLOBS` is the entire exploit, so a subdirectory may not
        be a way out of being declared."""
        root = make_repo({
            "CLAUDE.md": "x" * 10,
            ".claude/rules/archive/deep.md": "q" * 40,
            "seneschal/context-budget.json": budget_doc(
                {"CLAUDE.md": {"tier": "A", "max_bytes": 10, "raises": [], "note": "n"}}),
        })
        self.assertEqual([r["artifact"] for r in cb.check(root, "origin/develop")["undeclared"]],
                         [".claude/rules/archive/deep.md"])

    def test_an_UNTRACKED_rule_file_is_not_policed(self):
        """A host-local rule the owner drops in a private checkout is not this repo's grounding, and a
        check that failed on it would fail for something no PR can fix."""
        root = make_repo({
            "CLAUDE.md": "x" * 10,
            "seneschal/context-budget.json": budget_doc(
                {"CLAUDE.md": {"tier": "A", "max_bytes": 10, "raises": [], "note": "n"}}),
        })
        write(root, {".claude/rules/host-local.md": "z" * 99})  # never added
        self.assertEqual(cb.check(root, "origin/develop")["undeclared"], [])

    def test_a_budgeted_file_that_vanished_is_reported(self):
        root = make_repo({
            "CLAUDE.md": "x" * 10,
            "seneschal/context-budget.json": budget_doc(
                {"CLAUDE.md": {"tier": "A", "max_bytes": 10, "raises": [], "note": "n"},
                 "gone.md": {"tier": "A", "max_bytes": 10, "raises": [], "note": "n"}}),
        })
        self.assertEqual([r["artifact"] for r in cb.check(root, "origin/develop")["missing"]],
                         ["gone.md"])


class ReportOnlyTests(unittest.TestCase):
    """The byte cap cannot fail a build without --enforce."""

    def setUp(self):
        self.root = make_repo({
            "CLAUDE.md": "x" * 500,
            "seneschal/context-budget.json": budget_doc(
                {"CLAUDE.md": {"tier": "A", "max_bytes": 100, "raises": [], "note": "n"}}),
        })

    def test_violations_still_exit_zero_without_enforce(self):
        self.assertEqual(cb.main(["--root", self.root]), 0)

    def test_enforce_flips_it(self):
        self.assertEqual(cb.main(["--root", self.root, "--enforce"]), 1)


class CrossPRBlindness(unittest.TestCase):
    """Two PRs, each individually green, whose MERGE is over budget — and neither can see it.

    The shape: one PR is cut from `develop` and measures a shared router within its budget; a
    second PR merges first, adding to that file. The first merges later — still green, still
    measuring its own merge base — and the merged tree is over with **neither PR ever red.** Each
    grew a shared artifact within its own headroom; the sum is a violation with no author.

    The fix is to measure the tree the PR actually produces — HEAD merged into the *current*
    `origin/develop` — beside the tree the branch has. **Report-only, and deliberately NOT a
    violation:** the byte cap cannot fail a build, and whether an after-merge finding should ever
    block is an open decision, not this module's.
    """

    HEADROOM = 80
    GROWTH = 50

    def setUp(self):
        # Two sections, so the two branches edit disjoint regions and the merge is clean — which is
        # what makes the pair invisible. A conflicting merge is loud; this one is not.
        self.base_body = "# Top\n\n## A\n\naaa\n\n## B\n\nbbb\n"
        self.limit = len(self.base_body) + self.HEADROOM
        self.root = make_repo({
            "shared.md": self.base_body,
            "seneschal/context-budget.json": budget_doc(
                {"shared.md": {"tier": "B", "max_bytes": self.limit, "raises": [], "note": "n"}}),
        })
        _git(self.root, "branch", "-f", "origin/develop", "develop")

    def _grow(self, section: str) -> str:
        """Append `GROWTH` bytes under one section heading, leaving the other untouched."""
        pad = "x" * (self.GROWTH - 1) + "\n"
        return self.base_body.replace(f"## {section}\n", f"## {section}\n{pad}")

    def _land_pr_325(self):
        """The other PR merges into develop while ours is in flight."""
        _git(self.root, "checkout", "-q", "develop")
        write(self.root, {"shared.md": self._grow("A")})
        commit(self.root, "pr-325")
        _git(self.root, "branch", "-f", "origin/develop", "develop")

    def _open_second_pr(self):
        """Our branch, cut from develop BEFORE the other PR landed."""
        _git(self.root, "checkout", "-q", "-b", "pr-326", "develop")
        write(self.root, {"shared.md": self._grow("B")})
        commit(self.root, "pr-326")

    def _both_in_flight(self):
        self._open_second_pr()
        self._land_pr_325()
        _git(self.root, "checkout", "-q", "pr-326")

    def test_the_branch_alone_is_within_budget_which_is_the_whole_problem(self):
        """Documents the blindness rather than fixing it: this is what CI saw on both PRs."""
        self._both_in_flight()
        result = cb.check(self.root, "origin/develop")
        self.assertEqual(result["over"], [], "the branch alone must still read as clean")

    def test_the_MERGED_tree_is_over_and_is_reported(self):
        self._both_in_flight()
        result = cb.check(self.root, "origin/develop")
        self.assertEqual([r["artifact"] for r in result["merged_over"]], ["shared.md"])
        row = result["merged_over"][0]
        self.assertEqual(row["merged"], len(self.base_body) + 2 * self.GROWTH)
        self.assertEqual(row["delta"], 2 * self.GROWTH - self.HEADROOM)

    def test_the_finding_says_it_is_invisible_from_either_side(self):
        """`only_after_merge` is the field that distinguishes "this PR is too big" from "these two
        PRs are too big together" — the second has no author and is the one that shipped."""
        self._both_in_flight()
        row = cb.check(self.root, "origin/develop")["merged_over"][0]
        self.assertTrue(row["only_after_merge"])
        self.assertEqual(row["branch_actual"], len(self.base_body) + self.GROWTH)

    def test_a_prospective_finding_is_NOT_a_violation_and_cannot_fail_a_build(self):
        """The byte cap is report-only and `--enforce` is not wired. Neither may be flipped by
        this addition: an after-merge finding is information for the author, not a gate."""
        self._both_in_flight()
        self.assertEqual(cb.check(self.root, "origin/develop")["violations"], 0)
        self.assertEqual(cb.main(["--root", self.root]), 0)
        self.assertEqual(cb.main(["--root", self.root, "--enforce"]), 0)

    def test_the_merged_budget_is_what_develop_will_actually_apply(self):
        """The second PR *lowered* budgets as it swept. The after-merge question is "what will the check say
        on develop once this lands", so the config comes from the merged tree, not from HEAD."""
        self._open_second_pr()
        _git(self.root, "checkout", "-q", "develop")
        write(self.root, {"seneschal/context-budget.json": budget_doc(
            {"shared.md": {"tier": "B", "max_bytes": len(self.base_body), "raises": [],
                           "note": "tightened by the other PR"}})})
        commit(self.root, "pr-325-tightens")
        _git(self.root, "branch", "-f", "origin/develop", "develop")
        _git(self.root, "checkout", "-q", "pr-326")

        result = cb.check(self.root, "origin/develop")
        self.assertEqual(result["over"], [], "against HEAD's own budget the branch is fine")
        self.assertEqual([r["artifact"] for r in result["merged_over"]], ["shared.md"])
        self.assertEqual(result["merged_over"][0]["budget"], len(self.base_body))

    def test_a_literal_artifact_is_measured_in_the_merged_tree_too(self):
        """`presence.py::GROUNDING` is startup context living in a .py (§3.1); it must not fall out
        of the after-merge check just because it is read by `ast` out of a blob rather than sized."""
        src = 'OTHER = "%s"\n' + "# pad\n" * 8 + 'GROUNDING = "%s"\n'
        root = make_repo({
            "m.py": src % ("o", "aaaa"),
            "seneschal/context-budget.json": budget_doc(
                {"m.py::GROUNDING": {"tier": "A", "max_bytes": 6, "raises": [], "note": "n"}}),
        })
        _git(root, "branch", "-f", "origin/develop", "develop")
        _git(root, "checkout", "-q", "-b", "mine", "develop")
        write(root, {"m.py": src % ("o", "aaaaaaaaaa")})
        commit(root, "grow the literal")
        _git(root, "checkout", "-q", "develop")
        write(root, {"m.py": src % ("ooooo", "aaaa")})   # far enough away to merge cleanly
        commit(root, "unrelated edit lands on develop")
        _git(root, "branch", "-f", "origin/develop", "develop")
        _git(root, "checkout", "-q", "mine")

        result = cb.check(root, "origin/develop")
        self.assertFalse(result["merged_is_head"])
        self.assertEqual([r["artifact"] for r in result["merged_over"]], ["m.py::GROUNDING"])
        self.assertEqual(result["merged_over"][0]["merged"], 10)
        self.assertFalse(result["merged_over"][0]["only_after_merge"],
                         "this one IS over on the branch — it has an author, and must not be "
                         "mislabelled as the authorless kind")

    def test_a_branch_already_up_to_date_says_so_instead_of_repeating_itself(self):
        """Two questions, two answers. On a push to `develop` — and on any branch that already
        contains it — the merged tree IS HEAD's tree, so `over` already answered "is the tree over"
        and the after-merge block would only echo it."""
        write(self.root, {"shared.md": self.base_body + "y" * 200})
        commit(self.root, "over budget on develop itself")
        _git(self.root, "branch", "-f", "origin/develop", "develop")
        result = cb.check(self.root, "origin/develop")
        self.assertTrue(result["merged_is_head"])
        self.assertEqual(result["merged_over"], [])
        self.assertEqual(len(result["over"]), 1, "the tree-state question is still answered")

    def test_a_conflicting_merge_degrades_rather_than_inventing_a_number(self):
        """A merge that does not exist has no bytes. Reporting a guess here would be the one thing
        a ratchet cannot do — same posture as the merge-base bootstrap."""
        _git(self.root, "checkout", "-q", "-b", "pr-326", "develop")
        write(self.root, {"shared.md": self.base_body.replace("bbb", "b" * 60)})
        commit(self.root, "pr-326 rewrites the same line")
        _git(self.root, "checkout", "-q", "develop")
        write(self.root, {"shared.md": self.base_body.replace("bbb", "c" * 60)})
        commit(self.root, "and so does the other PR")
        _git(self.root, "branch", "-f", "origin/develop", "develop")
        _git(self.root, "checkout", "-q", "pr-326")
        result = cb.check(self.root, "origin/develop")
        self.assertIsNone(result["merged_tree"])
        self.assertEqual(result["merged_over"], [])

    def test_an_absent_base_ref_degrades_too(self):
        """CI's shallow checkout used to have no `origin/develop` at all."""
        result = cb.check(self.root, "origin/no-such-branch")
        self.assertIsNone(result["merged_tree"])
        self.assertEqual(result["merged_over"], [])


class ReconciliationTests(unittest.TestCase):
    """Measured vs. (merge-base + declared deltas).

    **Agreement is arithmetic, not luck.** For purely additive edits in disjoint
    regions, base + declared *is* the merged size — so the informative event is the MISMATCH, and its
    direction is a diagnosis: BELOW means the resolution dropped bytes (the silent-drop failure the
    ledger exists for), ABOVE means it added its own. Both are rare, which is the argument for
    checking rather than against it: rare + silent + green is the worst combination available.

    Two properties are load-bearing beyond the arithmetic and are asserted here rather than assumed.
    **Only NEW `raises[]` entries are summed** — an inherited entry's bytes are already in the
    merge-base measurement, and double-counting them would report drift on a clean branch. And **it
    abstains rather than guessing**, in every case where one of the two numbers does not exist.
    """

    BASE = "# Top\n\n## A\n\naaa\n\n## B\n\nbbb\n"

    def setUp(self):
        self.base_len = len(self.BASE)
        self.root = make_repo({
            "shared.md": self.BASE,
            "seneschal/context-budget.json": budget_doc(
                {"shared.md": {"tier": "B", "max_bytes": self.base_len, "raises": [], "note": "n"}}),
        })
        _git(self.root, "branch", "-f", "origin/develop", "develop")
        _git(self.root, "checkout", "-q", "-b", "mine", "develop")

    def _entry(self, frm, to, reason="one new row"):
        return {"date": "2026-08-24", "from": frm, "to": to, "reason": reason}

    def _declare(self, grown_by, raises, artifact="shared.md", body=None, max_bytes=None):
        """Grow the file by `grown_by` bytes and declare `raises` against it."""
        if body is None:
            body = self.BASE.replace("## B\n", "## B\n" + "x" * (grown_by - 1) + "\n")
        write(self.root, {
            artifact: body,
            "seneschal/context-budget.json": budget_doc(
                {artifact: {"tier": "B", "max_bytes": max_bytes or raises[-1]["to"],
                            "raises": raises, "note": "n"}}),
        })
        commit(self.root, "declare")

    def _row(self):
        result = cb.check(self.root, "origin/develop")
        self.assertTrue(result["reconcilable"])
        self.assertEqual(len(result["reconciled"]), 1)
        return result["reconciled"][0]

    # --- the three directions -------------------------------------------------------------

    def test_a_single_honest_raise_reconciles_to_zero_and_says_nothing(self):
        """The overwhelming majority of PRs. If this is chatty it gets ignored, then disabled."""
        self._declare(50, [self._entry(self.base_len, self.base_len + 50)])
        row = self._row()
        self.assertEqual(row["drift"], 0)
        self.assertEqual(row["expected"], row["measured"])
        rendered = cb.render(cb.check(self.root, "origin/develop"))
        self.assertNotIn("RECONCILE ", rendered)
        self.assertIn("measure exactly", rendered)

    def test_BELOW_the_sum_means_bytes_were_DROPPED(self):
        """The resolution kept one side of a hunk: the file grew by less than was declared."""
        self._declare(30, [self._entry(self.base_len, self.base_len + 80)])
        row = self._row()
        self.assertEqual(row["drift"], -50)
        rendered = cb.render(cb.check(self.root, "origin/develop"))
        self.assertIn("50 B BELOW", rendered)
        self.assertIn("DROPPED", rendered)
        self.assertNotIn("ADDED bytes of its own", rendered)

    def test_ABOVE_the_sum_means_the_resolution_ADDED_ITS_OWN(self):
        """A reword, a re-indent, or a row kept from both sides."""
        self._declare(80, [self._entry(self.base_len, self.base_len + 30)])
        row = self._row()
        self.assertEqual(row["drift"], 50)
        rendered = cb.render(cb.check(self.root, "origin/develop"))
        self.assertIn("50 B ABOVE", rendered)
        self.assertIn("ADDED bytes of its own", rendered)
        self.assertNotIn("DROPPED", rendered)

    def test_two_chained_entries_sum_and_reconcile(self):
        """The rebase shape: a PR carrying its own raise plus a rechained one. The sum of both
        deltas against ONE merge-base measurement is the whole arithmetic."""
        self._declare(90, [self._entry(self.base_len, self.base_len + 40),
                           self._entry(self.base_len + 40, self.base_len + 90)])
        row = self._row()
        self.assertEqual(row["new_entries"], 2)
        self.assertEqual(row["declared"], 90)
        self.assertEqual(row["drift"], 0)

    def test_a_dropped_entry_between_two_declared_ones_reads_as_BELOW(self):
        """The motivating failure, seen through this check: one side's row was lost in the
        resolution, so the file is smaller than the two entries between them claim."""
        self._declare(40, [self._entry(self.base_len, self.base_len + 40),
                           self._entry(self.base_len + 40, self.base_len + 90)])
        self.assertEqual(self._row()["drift"], -50)

    # --- scope: only what this branch declared --------------------------------------------

    def test_only_NEW_entries_are_summed_not_inherited_ones(self):
        """An inherited entry's bytes are ALREADY in the merge-base measurement. Summing it too
        would report -N of drift on a perfectly clean branch — the false positive that would get
        this check switched off in a week."""
        _git(self.root, "checkout", "-q", "develop")
        inherited = self._entry(self.base_len, self.base_len + 60, "landed on develop first")
        write(self.root, {
            "shared.md": self.BASE.replace("## A\n", "## A\n" + "y" * 59 + "\n"),
            "seneschal/context-budget.json": budget_doc(
                {"shared.md": {"tier": "B", "max_bytes": self.base_len + 60,
                               "raises": [inherited], "note": "n"}}),
        })
        commit(self.root, "the other PR")
        _git(self.root, "branch", "-f", "origin/develop", "develop")
        _git(self.root, "checkout", "-q", "-b", "later", "develop")
        self._declare(25, [inherited, self._entry(self.base_len + 60, self.base_len + 85)],
                      body=self.BASE.replace("## A\n", "## A\n" + "y" * 59 + "\n")
                                    .replace("## B\n", "## B\n" + "x" * 24 + "\n"))
        row = self._row()
        self.assertEqual(row["new_entries"], 1)
        self.assertEqual(row["declared"], 25)
        self.assertEqual(row["drift"], 0)

    def test_a_branch_that_declared_nothing_is_not_reconciled_at_all(self):
        """Growth inside the headroom declares no delta, so there is nothing to reconcile and
        nothing to say."""
        write(self.root, {"shared.md": self.BASE + "z" * 5})
        commit(self.root, "small growth, no raise")
        result = cb.check(self.root, "origin/develop")
        self.assertEqual(result["reconciled"], [])
        self.assertNotIn("reconciliation", cb.render(result))

    def test_the_two_measurements_are_the_modules_OWN_two_measurements(self):
        """A check that disagreed with the ratchet about how many bytes a file has would be worse
        than no check, so both numbers come from the existing paths and neither is re-derived."""
        self._declare(50, [self._entry(self.base_len, self.base_len + 50)])
        row = self._row()
        mb = cb.merge_base(self.root, "origin/develop")
        self.assertEqual(row["measured"], cb.measure("shared.md", self.root))
        self.assertEqual(row["base_bytes"], cb.measure_in_tree(self.root, mb, "shared.md"))

    def test_a_CRLF_working_tree_cannot_manufacture_drift(self):
        """git-object bytes, and the reason they are the basis: a Windows host checks out CRLF from an
        LF blob, so a naive read overstates every file by its line count — and would report a
        fictitious ABOVE on every raise made on such a host."""
        self._declare(50, [self._entry(self.base_len, self.base_len + 50)])
        clean = self._row()
        with open(os.path.join(self.root, "shared.md"), encoding="utf-8") as fh:
            body = fh.read()
        write(self.root, {"shared.md": body.replace("\n", "\r\n")})  # as a checkout would leave it
        self.assertEqual(self._row()["drift"], clean["drift"])

    # --- the headroom qualifier -----------------------------------------------------------

    def test_headroom_at_the_merge_base_is_NOT_reported_as_a_dropped_or_added_byte(self):
        """A chain's `from` is the previous BUDGET, not the previous MEASURED size, so on an
        artifact sitting under its budget the two conventions in this ledger differ by exactly the
        slack — which is most artifacts that have not been raised recently. Reporting that as ABOVE
        would be a confident wrong diagnosis on much of the tree, which is how a check gets disabled."""
        _git(self.root, "checkout", "-q", "develop")
        write(self.root, {"seneschal/context-budget.json": budget_doc(
            {"shared.md": {"tier": "B", "max_bytes": self.base_len + 20, "raises": [],
                           "note": "seeded with 20 B of slack"}})})
        commit(self.root, "slack at the base")
        _git(self.root, "branch", "-f", "origin/develop", "develop")
        _git(self.root, "checkout", "-q", "-b", "slack", "develop")
        # The raise re-anchors max_bytes onto the newly measured size, so it under-claims by the 20.
        self._declare(50, [self._entry(self.base_len + 20, self.base_len + 50)])
        row = self._row()
        self.assertEqual(row["base_headroom"], 20)
        self.assertEqual(row["drift"], 20)
        self.assertTrue(row["explained_by_headroom"])
        rendered = cb.render(cb.check(self.root, "origin/develop"))
        self.assertIn("within the 20 B of headroom", rendered)
        self.assertNotIn("RECONCILE ", rendered)
        self.assertNotIn("ADDED bytes of its own", rendered)

    def test_drift_LARGER_than_the_headroom_is_still_reported_in_full(self):
        """The qualifier explains a bounded amount and no more — otherwise slack would become a
        blanket amnesty, and the check would go quiet exactly where the ledger is loosest."""
        _git(self.root, "checkout", "-q", "develop")
        write(self.root, {"seneschal/context-budget.json": budget_doc(
            {"shared.md": {"tier": "B", "max_bytes": self.base_len + 20, "raises": [],
                           "note": "seeded with 20 B of slack"}})})
        commit(self.root, "slack at the base")
        _git(self.root, "branch", "-f", "origin/develop", "develop")
        _git(self.root, "checkout", "-q", "-b", "slack", "develop")
        self._declare(90, [self._entry(self.base_len + 20, self.base_len + 50)])
        row = self._row()
        self.assertEqual(row["drift"], 60)
        self.assertFalse(row["explained_by_headroom"])
        rendered = cb.render(cb.check(self.root, "origin/develop"))
        self.assertIn("60 B ABOVE", rendered)
        self.assertIn("does not account for this", rendered)

    # --- the abstentions ------------------------------------------------------------------

    def test_an_unreachable_merge_base_ABSTAINS_LOUDLY(self):
        """A shallow clone has no merge-base. Match the module's existing degrade-to-no-answer
        posture — and SAY so, because an abstention nobody can see reads as a pass."""
        self._declare(50, [self._entry(self.base_len, self.base_len + 50)])
        result = cb.check(self.root, "origin/no-such-branch")
        self.assertFalse(result["reconcilable"])
        self.assertEqual(result["reconciled"], [])
        self.assertIn("reconciliation: no answer", cb.render(result))
        self.assertIn("fetch-depth: 0", cb.render(result))

    def test_a_NEWLY_CREATED_artifact_abstains(self):
        """There is no base measurement to subtract from, and inventing 0 would report the whole
        file as ABOVE."""
        write(self.root, {
            "new.md": "n" * 120,
            "seneschal/context-budget.json": budget_doc({
                "shared.md": {"tier": "B", "max_bytes": self.base_len, "raises": [], "note": "n"},
                "new.md": {"tier": "B", "max_bytes": 120, "note": "n",
                           "raises": [self._entry(100, 120, "a new router")]}}),
        })
        commit(self.root, "add a new artifact")
        row = self._row()
        self.assertEqual(row["artifact"], "new.md")
        self.assertIn("no bytes at the merge-base", row["abstain"])
        self.assertIn("ABSTAINS on new.md", cb.render(cb.check(self.root, "origin/develop")))

    def test_a_DELETED_artifact_abstains(self):
        os.remove(os.path.join(self.root, "shared.md"))
        write(self.root, {"seneschal/context-budget.json": budget_doc(
            {"shared.md": {"tier": "B", "max_bytes": self.base_len + 50, "note": "n",
                           "raises": [self._entry(self.base_len, self.base_len + 50)]}})})
        commit(self.root, "delete it")
        self.assertIn("not measurable on the working tree", self._row()["abstain"])

    def test_a_DISCONTINUOUS_chain_abstains_rather_than_summing_across_the_break(self):
        """Nothing checks chain continuity today — the ratchet reads `raises[-1]`'s `to` and
        `reason` and never reads `from` at all (`context-budget-collisions-spec.md` §4.2, unbuilt).
        So a broken chain is a DIFFERENT bug, and answering off numbers that do not connect would
        be this check's own confidently-wrong failure."""
        self._declare(90, [self._entry(self.base_len, self.base_len + 40),
                           self._entry(self.base_len + 999, self.base_len + 90)])
        row = self._row()
        self.assertIn("discontinuous", row["abstain"])
        self.assertNotIn("drift", row)

    def test_a_NON_INTEGER_delta_abstains_too(self):
        """Same reason, one layer down: `to - from` on a string is not a number to report."""
        self._declare(50, [{"date": "2026-08-24", "from": self.base_len, "to": "lots",
                            "reason": "unmeasured"}], max_bytes=self.base_len + 50)
        self.assertIn("no integer from/to", self._row()["abstain"])

    # --- report-only ----------------------------------------------------------------------

    def test_a_reconciliation_finding_is_NOT_a_violation_and_cannot_fail_a_build(self):
        """The byte budget is report-only, and whether this should ever block is an open decision,
        not this module's. A gate red on the day it lands is a gate someone disables."""
        self._declare(30, [self._entry(self.base_len, self.base_len + 80)])
        result = cb.check(self.root, "origin/develop")
        self.assertEqual(result["reconciled"][0]["drift"], -50)
        self.assertEqual(result["violations"], 0)
        self.assertEqual(cb.main(["--root", self.root]), 0)
        self.assertEqual(cb.main(["--root", self.root, "--enforce"]), 0)


class ChainContinuityHelper(unittest.TestCase):
    """`chain_break` decides only whether the deltas CAN be summed — it is not a finding."""

    def test_a_continuous_chain_has_no_break(self):
        self.assertIsNone(cb.chain_break([{"from": 1, "to": 2}, {"from": 2, "to": 9}]))

    def test_an_empty_chain_has_no_break(self):
        self.assertIsNone(cb.chain_break([]))

    def test_the_FIRST_break_is_the_one_named(self):
        why = cb.chain_break([{"from": 1, "to": 2}, {"from": 5, "to": 9}, {"from": 40, "to": 50}])
        self.assertIn("raises[1]", why)
        self.assertNotIn("raises[2]", why)

    def test_a_malformed_entry_is_a_break(self):
        self.assertIn("not an object", cb.chain_break(["nope"]))

    def test_raises_of_tolerates_a_missing_or_malformed_list(self):
        self.assertEqual(cb.raises_of({}), [])
        self.assertEqual(cb.raises_of({"raises": None}), [])
        self.assertEqual(cb.raises_of(None), [])


def one_artifact(max_bytes: int, raises: list) -> dict:
    return {"CLAUDE.md": {"tier": "A", "max_bytes": max_bytes, "raises": raises, "note": "n"}}


def raise_entry(frm: int, to: int, date: str = "2026-09-01") -> dict:
    return {"date": date, "from": frm, "to": to, "reason": "why this is worth the startup context"}


class ChainContinuityTests(unittest.TestCase):
    """The chain rule — the rule that catches a DROPPED entry.

    Fixture configs, never the live ledger. The live one changes on a third of all merges, so a
    test asserting against it would go red on somebody else's PR — which is the fastest route to a
    disabled check, and this rule exists precisely because that file is edited constantly.

    The one deliberate exception is `test_enforce_chain_is_NOT_wired_into_ci`, which reads the real
    workflow: its whole job is to go red if a later edit wires the flag, because whether this rule
    should block is an open decision.
    """

    def test_a_clean_chain_passes(self):
        chain = [raise_entry(100, 250), raise_entry(250, 400), raise_entry(400, 900)]
        self.assertEqual(cb.chain_breaks(one_artifact(900, chain)), [])

    def test_a_DROPPED_MIDDLE_entry_fails(self):
        """The motivating event: a conflict resolution dropped a middle step and every check stayed
        green."""
        chain = [raise_entry(100, 250), raise_entry(400, 900)]  # the 250 -> 400 step is gone
        rows = cb.chain_breaks(one_artifact(900, chain))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rule"], "continuity")
        self.assertEqual(rows[0]["index"], 1)
        self.assertEqual(rows[0]["expected"], 250)
        self.assertEqual(rows[0]["found"], 400)
        self.assertEqual(rows[0]["gap"], 150)

    def test_a_MAX_BYTES_edited_without_a_final_entry_fails(self):
        """The case the ratchet structurally cannot see — it only arms when the number went UP
        against the merge base, so a resolution that keeps max_bytes right while losing the last
        entry slips straight past it (§1.2(3))."""
        rows = cb.chain_breaks(one_artifact(900, [raise_entry(100, 250)]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rule"], "termination")
        self.assertEqual(rows[0]["expected"], 900)
        self.assertEqual(rows[0]["found"], 250)
        self.assertEqual(rows[0]["gap"], -650)

    def test_an_EMPTY_raises_list_is_clean(self):
        """11 of the 36 live artifacts carry one. They are the bootstrap seeds: no chain, no rule."""
        self.assertEqual(cb.chain_breaks(one_artifact(900, [])), [])

    def test_a_SINGLE_ENTRY_chain_is_clean(self):
        self.assertEqual(cb.chain_breaks(one_artifact(250, [raise_entry(100, 250)])), [])

    def test_raises_ZERO_from_is_UNCONSTRAINED(self):
        """Assertion 3. The first entry's base predates the ledger for most artifacts, so a rule
        there would go red on history rather than on mistakes."""
        for origin in (0, 1, 999_999):
            chain = [raise_entry(origin, 250), raise_entry(250, 400)]
            self.assertEqual(cb.chain_breaks(one_artifact(400, chain)), [], f"from={origin}")

    def test_an_absent_max_bytes_does_not_manufacture_a_termination_finding(self):
        """A missing max_bytes is a DIFFERENT check's finding; reporting it here names one defect
        twice under two names."""
        budgets = {"CLAUDE.md": {"tier": "A", "raises": [raise_entry(100, 250)], "note": "n"}}
        self.assertEqual(cb.chain_breaks(budgets), [])

    def test_a_malformed_entry_STOPS_the_walk(self):
        """Past an entry with no integer `to` there is no previous value to compare the next `from`
        against, so continuing would manufacture a second finding out of the first one."""
        chain = [raise_entry(100, 250), {"date": "x", "from": 250, "reason": "no to"},
                 raise_entry(9999, 10000)]
        rows = cb.chain_breaks(one_artifact(10000, chain))
        self.assertEqual([r["rule"] for r in rows], ["malformed"])
        self.assertEqual(rows[0]["index"], 1)

    def test_every_finding_names_the_artifact(self):
        budgets = {"a/CLAUDE.md": {"max_bytes": 9, "raises": [raise_entry(1, 2), raise_entry(5, 9)]},
                   "b/CLAUDE.md": {"max_bytes": 9, "raises": [raise_entry(1, 9)]}}
        rows = cb.chain_breaks(budgets)
        self.assertEqual([r["artifact"] for r in rows], ["a/CLAUDE.md"])

    def test_reconcile_s_gate_does_NOT_fire_on_a_termination_break(self):
        """`chain_break` passes `limit=None` deliberately: a termination break is a real §4.2
        finding, but every from/to pair still connects, so the deltas CAN still be summed. Abstaining
        on it would silence the reconciliation on exactly the artifacts already known to be wrong."""
        self.assertIsNone(cb.chain_break([raise_entry(100, 250)]))
        self.assertIsNotNone(cb.chain_break([raise_entry(100, 250), raise_entry(400, 900)]))


class ChainIsReportOnlyTests(unittest.TestCase):
    """Folding the chain rule into `violations` is rejected OUTRIGHT — that would turn the byte
    ratchet blocking as a side effect of a structural rule. The narrow `--enforce-chain` door is the
    proposal; whether CI uses it is an open decision."""

    def setUp(self):
        # A chain with a dropped middle entry, on a file that is comfortably WITHIN budget — so the
        # only thing wrong with this tree is the chain, and nothing else can account for an exit 1.
        self.root = make_repo({
            "CLAUDE.md": "x" * 50,
            "seneschal/context-budget.json": budget_doc(
                one_artifact(900, [raise_entry(100, 250), raise_entry(400, 900)])),
        })
        _git(self.root, "branch", "-f", "origin/develop", "develop")

    def test_a_broken_chain_is_NOT_counted_in_violations(self):
        result = cb.check(self.root, "origin/develop")
        self.assertEqual(len(result["broken_chains"]), 1)
        self.assertEqual(result["violations"], 0)

    def test_plain_run_exits_zero(self):
        self.assertEqual(cb.main(["--root", self.root]), 0)

    def test_ENFORCE_alone_does_not_fail_on_a_chain_break(self):
        """The byte gate stays exactly as report-only as it was — that is the whole point of the
        separate flag."""
        self.assertEqual(cb.main(["--root", self.root, "--enforce"]), 0)

    def test_ENFORCE_CHAIN_flips_it(self):
        self.assertEqual(cb.main(["--root", self.root, "--enforce-chain"]), 1)

    def test_ENFORCE_CHAIN_exits_zero_on_a_clean_chain(self):
        write(self.root, {"seneschal/context-budget.json": budget_doc(
            one_artifact(900, [raise_entry(100, 250), raise_entry(250, 900)]))})
        commit(self.root)
        self.assertEqual(cb.main(["--root", self.root, "--enforce-chain"]), 0)

    def test_the_rendered_block_names_index_expected_found_and_gap(self):
        text = cb.render(cb.check(self.root, "origin/develop"))
        self.assertIn("raises[1]", text)
        self.assertIn("expected from 250 B", text)
        self.assertIn("found 400 B", text)
        self.assertIn("gap +150 B", text)

    def test_the_block_is_SILENT_when_the_chains_are_clean(self):
        write(self.root, {"seneschal/context-budget.json": budget_doc(
            one_artifact(900, [raise_entry(100, 250), raise_entry(250, 900)]))})
        commit(self.root)
        self.assertNotIn("CHAIN", cb.render(cb.check(self.root, "origin/develop")))

    def test_enforce_chain_is_NOT_wired_into_ci(self):
        """**This test is supposed to fail if someone wires the flag** — that is its whole job.

        Whether CI should block on a broken chain is a genuine fork: the argument FOR blocking is
        that a broken chain is unambiguously a bug with a local fix and no producer problem behind
        it, the same reason the POINTER check was the safe one to turn blocking first. The argument
        against is that CI turning red on the repo's most-edited file is exactly the pressure that
        gets a gate disabled. It is not a test's to answer by adding a word to a workflow."""
        with open(os.path.join(cb.REPO_ROOT, ".github", "workflows", "ci.yml"),
                  encoding="utf-8") as fh:
            workflow = fh.read()
        self.assertNotIn("--enforce-chain", workflow,
                         "--enforce-chain is wired into CI, but whether it should block is an open "
                         "decision. Once it is decided, delete this test and cite the ledger row.")


class SectionAttribution(unittest.TestCase):
    def test_growth_is_bucketed_under_the_nearest_preceding_heading(self):
        """"The file is too big" gets worked around; "the Repo-state section grew 3 kB" names the
        paragraph to move (§3.5)."""
        root = make_repo({"CLAUDE.md": "# Top\n\n## Repo state\n\nold\n"})
        _git(root, "branch", "-f", "origin/develop", "develop")
        write(root, {"CLAUDE.md": "# Top\n\n## Repo state\n\nold\nnew line one\nnew line two\n"})
        commit(root)
        sections = cb.section_growth(root, "CLAUDE.md", "origin/develop")
        self.assertTrue(sections)
        self.assertEqual(sections[0][0], "Repo state")


class HeadroomConstantsTests(unittest.TestCase):
    """A 4:1 bytes-per-token conversion understates this tree's real token count: measured, it is
    ~2.59 B/token."""

    def test_bytes_per_token_is_the_measured_rate_not_4_to_1(self):
        self.assertAlmostEqual(cb.BYTES_PER_TOKEN, 2.59)
        self.assertNotAlmostEqual(cb.BYTES_PER_TOKEN, 4.0)

    def test_the_floor_is_5180_bytes_not_the_old_8kb_guess(self):
        """A "2k token floor" at the correct rate is ~5.2 KB, not the ~8 KB a 4:1 guess
        would print."""
        self.assertEqual(cb.HEADROOM_FLOOR_BYTES, 5180)
        self.assertLess(cb.HEADROOM_FLOOR_BYTES, 8000)


def grow_linearly(root: str, path: str, start_size: int, end_size: int,
                  start_date: str, end_date: str, steps: int):
    """`steps` intermediate commits, evenly spaced in both time and size, between the two endpoints —
    keeps every single-commit delta small (matching how organic growth actually lands, many small
    merges, never one big jump), so a fast-grower fixture does not itself trip the step-change reset
    it isn't testing. `trailing_growth` only reads the FIRST and LAST point for its rate, so the exact
    intermediate values don't matter — only that none of the steps between them is large enough to
    read as a reset."""
    import datetime as _dt
    d0 = _dt.date.fromisoformat(start_date)
    d1 = _dt.date.fromisoformat(end_date)
    total_days = (d1 - d0).days
    for i in range(1, steps + 1):
        frac = i / steps
        day = d0 + _dt.timedelta(days=round(total_days * frac))
        size = round(start_size + (end_size - start_size) * frac)
        write(root, {path: "x" * size})
        commit_at(root, day.isoformat())


def _amend_date(root: str, date_iso: str, msg: str = "base"):
    """Re-date the CURRENT HEAD commit (usually `make_repo`'s base) rather than adding a new one —
    used when a test needs to control BOTH endpoints of a span, not just the later ones `commit_at`
    naturally covers."""
    ts = f"{date_iso}T12:00:00+00:00"
    subprocess.run(["git", "commit", "--amend", "-qm", msg], cwd=root,
                   capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_DATE": ts, "GIT_COMMITTER_DATE": ts})


class TrailingGrowthTests(unittest.TestCase):
    """§3 of the new spec — the arithmetic `headroom_bytes` is built on. Fixture git repos with
    CONTROLLED commit dates (`commit_at`/`_amend_date`), never wall-clock timing. `headroom_bytes`
    defaults to walking HEAD's own history — these fixtures are single-branch, so HEAD is all there
    is, and no `origin/develop` bookkeeping is needed for this class."""

    def test_a_single_commit_is_no_history_and_floors(self):
        root = make_repo({"CLAUDE.md": "x" * 100})
        headroom, meta = cb.headroom_bytes(root, "CLAUDE.md", as_of="2026-08-01")
        self.assertEqual(meta["source"], "no-history")
        self.assertEqual(headroom, cb.HEADROOM_FLOOR_BYTES)
        self.assertTrue(meta["floor_applied"])

    def test_flat_or_shrinking_history_floors(self):
        root = make_repo({"CLAUDE.md": "x" * 100})
        _amend_date(root, "2026-07-01")
        write(root, {"CLAUDE.md": "x" * 80})  # SHRUNK, not just flat
        commit_at(root, "2026-07-15")
        headroom, meta = cb.headroom_bytes(root, "CLAUDE.md", as_of="2026-07-15")
        self.assertEqual(meta["source"], "non-positive-rate")
        self.assertEqual(headroom, cb.HEADROOM_FLOOR_BYTES)

    def test_a_fast_grower_produces_a_rate_based_headroom_above_the_floor(self):
        """300 B/day over 28 days -> 8,400 B, comfortably above the 5,180 B floor. Grown in small
        steps (never one big jump) so the growth itself doesn't trip the step-change reset."""
        root = make_repo({"CLAUDE.md": "x" * 20000})
        _amend_date(root, "2026-08-01")
        grow_linearly(root, "CLAUDE.md", 20000, 20000 + 300 * 28, "2026-08-01", "2026-08-29", steps=10)
        headroom, meta = cb.headroom_bytes(root, "CLAUDE.md", as_of="2026-08-29")
        self.assertEqual(meta["source"], "trailing-rate")
        self.assertFalse(meta["floor_applied"])
        self.assertAlmostEqual(meta["rate_per_day"], 300.0, places=1)
        self.assertEqual(headroom, 8400)

    def test_a_slow_grower_still_floors_even_with_a_positive_rate(self):
        """50 B/day over 28 days -> 1,400 B computed, which the floor overrides to 5,180 B — a
        positive rate is not the same as a rate worth trusting over the floor."""
        root = make_repo({"CLAUDE.md": "x" * 20000})
        _amend_date(root, "2026-08-01")
        grow_linearly(root, "CLAUDE.md", 20000, 20000 + 50 * 28, "2026-08-01", "2026-08-29", steps=4)
        headroom, meta = cb.headroom_bytes(root, "CLAUDE.md", as_of="2026-08-29")
        self.assertEqual(meta["source"], "trailing-rate")
        self.assertTrue(meta["floor_applied"])
        self.assertEqual(headroom, cb.HEADROOM_FLOOR_BYTES)

    def test_a_step_change_resets_the_window_and_ignores_history_before_it(self):
        """The hard case: a router-diet-shaped jump must not be read as growth, and the slow, real
        growth AFTER it must not be diluted by averaging across the jump."""
        root = make_repo({"CLAUDE.md": "x" * 100})
        _amend_date(root, "2026-08-01")
        # The step change: 100 B -> 5,000 B in one commit (a 49x jump).
        write(root, {"CLAUDE.md": "x" * 5000})
        commit_at(root, "2026-08-02")
        # Slow real growth after the reset: +100 B over the next 10 days (10 B/day).
        write(root, {"CLAUDE.md": "x" * 5100})
        commit_at(root, "2026-08-12")

        growth = cb.trailing_growth(root, "CLAUDE.md", as_of="2026-08-12")
        self.assertEqual(growth["reset_at"], "2026-08-02")
        # If the jump were NOT excluded, the naive rate over the full 11-day span would be
        # (5100-100)/11 ~= 454.5 B/day — nothing like the real 10 B/day post-reset growth.
        self.assertLess(growth["rate_per_day"], 50)
        self.assertAlmostEqual(growth["window_days_used"], 10.0, places=1)

    def test_a_step_change_with_too_little_history_after_it_floors_visibly(self):
        """Exactly what Phase 6's router diet does to `seneschal/scripts/CLAUDE.md` today: a reset with
        only a day or two of history after it must floor, and SAY it floored because of the reset —
        never fabricate a rate from two points a day apart."""
        root = make_repo({"CLAUDE.md": "x" * 100})
        _amend_date(root, "2026-08-01")
        write(root, {"CLAUDE.md": "x" * 5000})
        commit_at(root, "2026-08-11")
        write(root, {"CLAUDE.md": "x" * 5100})
        commit_at(root, "2026-08-12")  # only ONE day of post-reset history

        headroom, meta = cb.headroom_bytes(root, "CLAUDE.md", as_of="2026-08-12")
        self.assertEqual(meta["source"], "insufficient-history")
        self.assertEqual(meta["reset_at"], "2026-08-11")
        self.assertEqual(headroom, cb.HEADROOM_FLOOR_BYTES)
        self.assertTrue(meta["floor_applied"])


class HeadroomViolationTests(unittest.TestCase):
    """The headroom rule in code: a NEW raises[] entry must set `to` to
    EXACTLY the generator's own computed value, never a hand-typed number, right or wrong.

    `headroom_violations` walks HEAD's own history (via `headroom_bytes`'s default), while `base`
    (the merge-base config) only decides which `raises[]` entries are NEW — that split is what these
    fixtures exercise: `origin/develop` here is the merge base, not the ref the growth is measured
    against."""

    def _repo_with_history(self):
        """A slow, floor-bound grower — deterministic (always floors), so the expected `to` never
        depends on wall-clock timing between building the fixture and running the assertion. `develop`
        is left pointed at THIS commit — the merge base — before any "branch" commits are added."""
        root = make_repo({"CLAUDE.md": "x" * 1000,
                          "seneschal/context-budget.json": budget_doc(
                              {"CLAUDE.md": {"tier": "A", "max_bytes": 1000, "raises": [],
                                            "note": "n"}})})
        _amend_date(root, "2026-08-01")
        _git(root, "branch", "-f", "origin/develop", "develop")
        return root

    def test_a_raise_matching_the_generator_exactly_passes(self):
        root = self._repo_with_history()
        headroom, _ = cb.headroom_bytes(root, "CLAUDE.md", as_of="2026-08-10")
        write(root, {"seneschal/context-budget.json": budget_doc(
            {"CLAUDE.md": {"tier": "A", "max_bytes": 1000 + headroom, "note": "n",
                           "raises": [{"date": "2026-08-10", "from": 1000, "to": 1000 + headroom,
                                       "reason": "generated"}]}})})
        commit_at(root, "2026-08-10")
        base = cb._budgets_at(root, "origin/develop")
        head = cb._budgets_at(root, "HEAD")
        self.assertEqual(cb.headroom_violations(root, head, base), [])

    def test_a_raise_to_exactly_the_measured_size_the_old_ratchet_is_refused(self):
        """The old ratchet's defect: raising the cap to exactly the new measured size,
        zero headroom, is precisely what this check must never let through again."""
        root = self._repo_with_history()
        write(root, {"CLAUDE.md": "x" * 1200,
                     "seneschal/context-budget.json": budget_doc(
                         {"CLAUDE.md": {"tier": "A", "max_bytes": 1200, "note": "n",
                                       "raises": [{"date": "2026-08-10", "from": 1000, "to": 1200,
                                                   "reason": "old-style raise to measured size"}]}})})
        commit_at(root, "2026-08-10")
        base = cb._budgets_at(root, "origin/develop")
        head = cb._budgets_at(root, "HEAD")
        rows = cb.headroom_violations(root, head, base)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["to"], 1200)
        self.assertNotEqual(rows[0]["expected_to"], 1200)

    def test_an_arbitrary_wrong_number_is_also_refused(self):
        root = self._repo_with_history()
        write(root, {"seneschal/context-budget.json": budget_doc(
            {"CLAUDE.md": {"tier": "A", "max_bytes": 999999, "note": "n",
                           "raises": [{"date": "2026-08-10", "from": 1000, "to": 999999,
                                       "reason": "made up"}]}})})
        commit_at(root, "2026-08-10")
        base = cb._budgets_at(root, "origin/develop")
        head = cb._budgets_at(root, "HEAD")
        self.assertEqual(len(cb.headroom_violations(root, head, base)), 1)

    def test_history_already_at_the_merge_base_is_exempt(self):
        """This is a gate on NEW raises, not a retroactive judgement on the old ratchet's whole
        history — an old-convention entry already on develop must not suddenly go red."""
        root = make_repo({"CLAUDE.md": "x" * 1200,
                          "seneschal/context-budget.json": budget_doc(
                              {"CLAUDE.md": {"tier": "A", "max_bytes": 1200, "note": "n",
                                            "raises": [{"date": "2026-08-01", "from": 1000,
                                                        "to": 1200, "reason": "old-style, pre-rule"}]}})})
        _git(root, "branch", "-f", "origin/develop", "develop")
        write(root, {"persona/notes.md": "unrelated change"})
        commit(root)
        base = cb._budgets_at(root, "origin/develop")
        head = cb._budgets_at(root, "HEAD")
        self.assertEqual(cb.headroom_violations(root, head, base), [])

    def test_a_malformed_new_entry_is_left_to_the_chain_check_not_double_reported(self):
        root = self._repo_with_history()
        write(root, {"seneschal/context-budget.json": budget_doc(
            {"CLAUDE.md": {"tier": "A", "max_bytes": 1200, "note": "n",
                           "raises": [{"date": "2026-08-10", "from": "not-a-number", "to": 1200,
                                       "reason": "malformed"}]}})})
        commit_at(root, "2026-08-10")
        base = cb._budgets_at(root, "origin/develop")
        head = cb._budgets_at(root, "HEAD")
        self.assertEqual(cb.headroom_violations(root, head, base), [])


class EnforceHeadroomWiringTests(unittest.TestCase):
    def test_enforce_headroom_flag_is_wired_into_ci(self):
        """This check CAN block on day one (it only fires on a raise the branch itself adds), so
        unlike --enforce-chain it belongs in CI now."""
        with open(os.path.join(cb.REPO_ROOT, ".github", "workflows", "ci.yml"),
                  encoding="utf-8") as fh:
            workflow = fh.read()
        self.assertIn("--enforce-headroom", workflow)


if __name__ == "__main__":
    unittest.main()


class CommitSizesWindowIsUtcExplicit(unittest.TestCase):
    """The git window's bounds must name their zone. A bare `--until=DATE 23:59:59` is read in the
    git process's LOCAL zone, so the same entry validates on a host behind UTC and fails on the UTC
    runner (a different commit set, a different byte count). Pinning `+00:00` on both bounds is what makes
    `headroom_bytes()` a function of the tree alone."""

    def test_since_and_until_carry_an_explicit_utc_offset(self):
        seen = []

        def fake_git(root, *args):
            seen.append(args)
            return ""

        with mock.patch.object(cb, "_git", fake_git):
            cb.artifact_commit_sizes("unused", "CLAUDE.md", as_of="2026-09-11")
        self.assertEqual(len(seen), 1)
        args = seen[0]
        until = [a for a in args if a.startswith("--until=")]
        since = [a for a in args if a.startswith("--since=")]
        self.assertEqual(until, ["--until=2026-09-11T23:59:59+00:00"])
        self.assertEqual(len(since), 1)
        self.assertTrue(since[0].endswith("T00:00:00+00:00"), since[0])
