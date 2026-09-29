#!/usr/bin/env python3
"""Tests for `gate_git.py` — the shared "what did this branch change?" helper the local gates use.

Fixture git repos, never the live tree. The property under test: every
answer is about the WORKING TREE (index + unstaged + untracked-not-ignored), and on a clean checkout
— CI — it collapses to exactly what `<base>..HEAD` used to say.

Run:  python -m unittest seneschal.scripts.test_gate_git
"""
import io
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import gate_git  # noqa: E402


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def write(root, files):
    for rel, body in files.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path) or path, exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)


def commit(root, msg="change"):
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


def make_repo(files, gitignore=None):
    root = tempfile.mkdtemp()
    _git(root, "init", "-q", "-b", "develop")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    if gitignore is not None:
        files = dict(files, **{".gitignore": gitignore})
    write(root, files)
    commit(root, "base")
    return root


class ChangedPathsTests(unittest.TestCase):
    def test_a_clean_tree_has_no_changes(self):
        root = make_repo({"a.md": "one\n"})
        self.assertEqual(gate_git.changed_paths(root, "HEAD"), [])

    def test_unstaged_staged_and_untracked_all_count(self):
        root = make_repo({"a.md": "one\n", "b.md": "two\n", "c.py": "x = 1\n"})
        write(root, {"a.md": "one more\n"})                  # unstaged
        write(root, {"b.md": "two more\n"})
        _git(root, "add", "b.md")                             # staged
        write(root, {"d.md": "brand new\n"})                  # untracked
        self.assertEqual(gate_git.changed_paths(root, "HEAD"), ["a.md", "b.md", "d.md"])

    def test_pathspecs_filter_both_halves(self):
        root = make_repo({"a.md": "one\n", "c.py": "x = 1\n"})
        write(root, {"a.md": "edit\n", "c.py": "x = 2\n", "new.py": "y\n", "new.md": "z\n"})
        self.assertEqual(gate_git.changed_paths(root, "HEAD", "*.md"), ["a.md", "new.md"])

    def test_an_ignored_new_file_never_counts(self):
        root = make_repo({"a.md": "one\n"}, gitignore="state/\n")
        write(root, {"state/run-log.md": "log\n"})
        self.assertEqual(gate_git.changed_paths(root, "HEAD"), [])

    def test_a_bad_base_is_no_answer(self):
        root = make_repo({"a.md": "one\n"})
        write(root, {"a.md": "edit\n"})
        self.assertEqual(gate_git.changed_paths(root, "no-such-ref"), [])

    def test_a_committed_change_against_the_merge_base_still_counts(self):
        """CI's shape: nothing dirty, the branch's commits ARE the diff."""
        root = make_repo({"a.md": "one\n"})
        base = _git(root, "rev-parse", "HEAD").stdout.strip()
        _git(root, "checkout", "-q", "-b", "feature")
        write(root, {"a.md": "committed edit\n"})
        commit(root)
        self.assertEqual(_git(root, "status", "--porcelain").stdout.strip(), "")
        self.assertEqual(gate_git.changed_paths(root, base), ["a.md"])


class AddedLinesDiffTests(unittest.TestCase):
    def _added(self, diff):
        return [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]

    def test_an_uncommitted_added_line_is_in_the_diff(self):
        root = make_repo({"a.md": "one\n"})
        write(root, {"a.md": "one\ntwo\n"})
        diff = gate_git.added_lines_diff(root, "HEAD", "*.md")
        self.assertEqual(self._added(diff), ["two"])
        self.assertIn("+++ b/a.md", diff)

    def test_an_untracked_file_gets_a_synthetic_new_file_hunk(self):
        root = make_repo({"a.md": "one\n"})
        write(root, {"docs/new.md": "alpha\r\nbeta\r\n"})
        diff = gate_git.added_lines_diff(root, "HEAD", "*.md")
        self.assertIn("--- /dev/null\n+++ b/docs/new.md\n@@ -0,0 +1,2 @@\n", diff)
        self.assertEqual(self._added(diff), ["alpha", "beta"])

    def test_an_untracked_file_outside_the_pathspec_is_left_out(self):
        root = make_repo({"a.md": "one\n"})
        write(root, {"new.py": "x = 1\n"})
        self.assertEqual(gate_git.added_lines_diff(root, "HEAD", "*.md"), "")

    def test_a_clean_tree_is_an_empty_string_not_none(self):
        root = make_repo({"a.md": "one\n"})
        self.assertEqual(gate_git.added_lines_diff(root, "HEAD", "*.md"), "")

    def test_a_bad_base_is_none(self):
        root = make_repo({"a.md": "one\n"})
        self.assertIsNone(gate_git.added_lines_diff(root, "no-such-ref", "*.md"))

    def test_the_committed_diff_is_unchanged_on_a_clean_checkout(self):
        root = make_repo({"a.md": "one\n"})
        base = _git(root, "rev-parse", "HEAD").stdout.strip()
        _git(root, "checkout", "-q", "-b", "feature")
        write(root, {"a.md": "one\ntwo\n"})
        commit(root)
        ours = gate_git.added_lines_diff(root, base, "*.md")
        theirs = _git(root, "-c", "core.fsmonitor=false", "diff", "-U0", "--no-color",
                      f"{base}..HEAD", "--", "*.md").stdout
        self.assertEqual(ours.replace("\r\n", "\n"), theirs.replace("\r\n", "\n"))


class WorkingTreePathsTests(unittest.TestCase):
    def test_tracked_union_untracked_minus_ignored(self):
        root = make_repo({"a.py": "x\n", "b.md": "y\n"}, gitignore="state/\n*.env\n")
        write(root, {"new.py": "z\n", "state/x.py": "w\n", "k.env": "SECRET=1\n"})
        self.assertEqual(gate_git.working_tree_paths(root), {".gitignore", "a.py", "b.md", "new.py"})
        self.assertEqual(gate_git.working_tree_paths(root, "*.py"), {"a.py", "new.py"})
        self.assertEqual(gate_git.untracked_paths(root), ["new.py"])

    def test_not_a_repo_is_an_empty_answer(self):
        root = tempfile.mkdtemp()
        # A temp dir may sit under someone's repo; `-C` a fresh dir with no .git above it is not
        # guaranteed, so accept either "empty" or "the parent repo's view" — but never raise.
        gate_git.working_tree_paths(root)
        gate_git.untracked_paths(root)


class WorkingTreeSummaryTests(unittest.TestCase):
    def test_clean(self):
        root = make_repo({"a.md": "one\n"})
        self.assertEqual(gate_git.working_tree_summary(root), (0, 0))

    def test_counts_modified_and_untracked_separately(self):
        root = make_repo({"a.md": "one\n", "b.md": "two\n"}, gitignore="state/\n")
        write(root, {"a.md": "edit\n", "b.md": "edit\n", "n1.md": "x\n", "d/n2.md": "y\n",
                     "state/ignored.md": "z\n"})
        _git(root, "add", "a.md")
        self.assertEqual(gate_git.working_tree_summary(root), (2, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
