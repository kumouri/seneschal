#!/usr/bin/env python3
"""Tests for `check_no_utcnow.py` — the report-only `.utcnow(` scanner."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import check_no_utcnow as cnu  # noqa: E402


class ScanSource(unittest.TestCase):
    def test_finds_datetime_utcnow(self):
        src = "from datetime import datetime\nx = datetime.utcnow()\n"
        found = cnu.scan_source(src, "m.py")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], 2)  # line number

    def test_finds_a_bound_utcnow(self):
        src = "x = dt.utcnow()\n"
        self.assertEqual(len(cnu.scan_source(src, "m.py")), 1)

    def test_finds_utcnow_with_internal_whitespace(self):
        src = "x = datetime.utcnow ()\n"
        self.assertEqual(len(cnu.scan_source(src, "m.py")), 1)

    def test_does_not_match_now(self):
        src = "x = datetime.now()\n"
        self.assertEqual(cnu.scan_source(src, "m.py"), [])

    def test_does_not_match_utcfromtimestamp(self):
        src = "x = datetime.utcfromtimestamp(0)\n"
        self.assertEqual(cnu.scan_source(src, "m.py"), [])

    def test_multiple_occurrences_on_different_lines(self):
        src = "a = datetime.utcnow()\nb = datetime.utcnow()\n"
        found = cnu.scan_source(src, "m.py")
        self.assertEqual([f[1] for f in found], [1, 2])

    def test_a_clean_file_finds_nothing(self):
        src = "def f():\n    return 1\n"
        self.assertEqual(cnu.scan_source(src, "m.py"), [])


class Main(unittest.TestCase):
    def test_main_always_returns_zero_regardless_of_findings(self):
        """Report-only as CODE, not just as a claim -- the live tree may or may not have findings
        (this scanner's own docstring is a known, accepted false-positive source), and the exit
        code must be 0 either way."""
        self.assertEqual(cnu.main([]), 0)

    def test_main_accepts_a_root_argument(self):
        self.assertEqual(cnu.main(["--root", cnu.repo_root()]), 0)



class WorkingTreeTests(unittest.TestCase):
    """Local gates see the working tree: an untracked
    new module is scanned locally, not first on CI."""

    def test_an_untracked_new_module_is_scanned(self):
        root = tempfile.mkdtemp()
        for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git"] + argv, cwd=root, capture_output=True, check=False)
        with open(os.path.join(root, "tracked.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        for argv in (["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git"] + argv, cwd=root, capture_output=True, check=False)
        with open(os.path.join(root, "fresh.py"), "w", encoding="utf-8") as fh:
            fh.write("from datetime import datetime\nx = datetime.utcnow()\n")
        found = cnu.scan(root)
        self.assertEqual([(f[0], f[1]) for f in found], [("fresh.py", 2)])


if __name__ == "__main__":
    unittest.main()
