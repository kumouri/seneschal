#!/usr/bin/env python3
"""Tests for check_json_files.py — the "Reference data check" CI job, repointed from a single
hardcoded file (deleted `seneschal/references/autonomy-config.json`) onto every tracked `.json` file.

Stdlib unittest only. Uses a real, tiny git repo (`tracked_json_files` shells out to `git ls-files`),
the same fixture pattern `test_check_no_misquote.py` uses for the same reason: the oracle being tested
is the real command, not a mock of it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import check_json_files as cjf  # noqa: E402


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def make_repo(files: dict) -> str:
    root = tempfile.mkdtemp()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for rel, body in files.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fixture")
    return root


class Scan(unittest.TestCase):
    def test_a_clean_tree_finds_nothing(self):
        root = make_repo({"a/b.json": "{}", "c.json": "[1, 2, 3]"})
        result = cjf.scan(root)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["files_checked"], 2)

    def test_flags_an_unparseable_file(self):
        root = make_repo({"a/b.json": "{not valid json"})
        result = cjf.scan(root)
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["file"], "a/b.json")

    def test_non_json_tracked_files_are_never_scanned(self):
        """`git ls-files '*.json'` is the source of truth — a .md or .py file, however malformed,
        must never appear in the result."""
        root = make_repo({"docs/readme.md": "not json at all", "a.json": "{}"})
        result = cjf.scan(root)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["files_checked"], 1)

    def test_multiple_unparseable_files_are_all_reported(self):
        root = make_repo({"a.json": "{bad", "b.json": "[bad", "c.json": "{}"})
        result = cjf.scan(root)
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(result["files_checked"], 1)

    def test_the_jsonc_allowlist_is_exempt(self):
        """cockpit/web/tsconfig.json carries a deliberate `//` comment — valid TypeScript JSONC,
        not broken JSON — and must not be flagged."""
        root = make_repo({"cockpit/web/tsconfig.json": '{\n  // a comment\n  "a": 1\n}'})
        result = cjf.scan(root)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["files_checked"], 0)

    def test_an_untracked_json_file_IS_scanned(self):
        """Local gates see the working tree: a
        local gate reads the working tree, so a not-yet-added `.json` is policed before the commit
        that would have made CI red. On CI's clean checkout there is no such file."""
        root = make_repo({"a.json": "{}"})
        untracked = os.path.join(root, "untracked.json")
        with open(untracked, "w", encoding="utf-8") as fh:
            fh.write("{not valid")
        result = cjf.scan(root)
        self.assertEqual([f["file"] for f in result["findings"]], ["untracked.json"])
        self.assertEqual(result["files_checked"], 1)  # `files_checked` counts the parse-OK ones

    def test_a_gitignored_json_file_is_still_not_scanned(self):
        """`--exclude-standard`: a host-local, gitignored file was never policed and still isn't."""
        root = make_repo({"a.json": "{}", ".gitignore": "local.json\n"})
        with open(os.path.join(root, "local.json"), "w", encoding="utf-8") as fh:
            fh.write("{not valid")
        result = cjf.scan(root)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["files_checked"], 1)


class MainCLI(unittest.TestCase):
    def test_report_only_exits_zero_even_with_findings(self):
        root = make_repo({"a.json": "{bad"})
        self.assertEqual(cjf.main(["--root", root]), 0)

    def test_enforce_exits_nonzero_with_findings(self):
        root = make_repo({"a.json": "{bad"})
        self.assertEqual(cjf.main(["--root", root, "--enforce"]), 1)

    def test_enforce_exits_zero_on_a_clean_tree(self):
        root = make_repo({"a.json": "{}"})
        self.assertEqual(cjf.main(["--root", root, "--enforce"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
