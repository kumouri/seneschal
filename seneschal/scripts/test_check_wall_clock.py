#!/usr/bin/env python3
"""Tests for check_wall_clock.py — the bare-wall-clock-read gate.

Positive fixtures pin the four patterns the module's docstring names; negative fixtures pin the
shapes that must NOT match — an injected/aware clock, `check_context_budget.py`'s own already-correct
f-string, and the unrelated `--since`/`--until` CLI flags a script may define on its own. `TheLiveTree` runs the real check against this repository, same as every other gate's own
test module — a gate that is green only on synthetic input proves nothing about the tree it gates.

Stdlib unittest only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import check_wall_clock as cwc  # noqa: E402


def findings(src):
    return [f"{f.symbol}:{f.kind}" for f in cwc.scan_source(src, "m.py")]


class DetectsDateToday(unittest.TestCase):
    def test_bare_date_today(self):
        src = "from datetime import date\ndef f():\n    return date.today()\n"
        self.assertEqual(findings(src), ["f:date-today"])

    def test_two_hop_datetime_date_today(self):
        src = "import datetime\ndef f():\n    return datetime.date.today().isoformat()\n"
        self.assertEqual(findings(src), ["f:date-today"])

    def test_an_aliased_import_still_matches(self):
        src = "import datetime as dt\ndef f():\n    return dt.date.today()\n"
        self.assertEqual(findings(src), ["f:date-today"])

    def test_module_level_call_has_no_enclosing_function(self):
        src = "from datetime import date\nX = date.today()\n"
        found = cwc.scan_source(src, "m.py")
        self.assertEqual(found[0].symbol, "")


class DetectsNaiveNow(unittest.TestCase):
    def test_bare_datetime_now(self):
        src = "from datetime import datetime\ndef f():\n    return datetime.now()\n"
        self.assertEqual(findings(src), ["f:naive-now"])

    def test_an_aliased_import_still_matches(self):
        src = "import datetime as _dt\ndef f():\n    return _dt.datetime.now()\n"
        self.assertEqual(findings(src), ["f:naive-now"])

    def test_now_with_an_explicit_tz_is_not_flagged(self):
        src = "from datetime import datetime, timezone\ndef f():\n    return datetime.now(timezone.utc)\n"
        self.assertEqual(findings(src), [])

    def test_now_with_a_keyword_tz_is_not_flagged(self):
        src = "from datetime import datetime, timezone\ndef f():\n    return datetime.now(tz=timezone.utc)\n"
        self.assertEqual(findings(src), [])


class DetectsLocaltime(unittest.TestCase):
    def test_bare_time_localtime(self):
        src = "import time\ndef f():\n    return time.localtime()\n"
        self.assertEqual(findings(src), ["f:localtime"])

    def test_localtime_given_an_instant_is_not_flagged(self):
        """Converting a GIVEN epoch is not a live read."""
        src = "import time\ndef f(epoch):\n    return time.localtime(epoch)\n"
        self.assertEqual(findings(src), [])


class DetectsGitWindow(unittest.TestCase):
    def test_since_with_no_offset_is_flagged(self):
        src = 'def f(since):\n    args = ["log", f"--since={since}"]\n'
        self.assertEqual(findings(src), ["f:git-window"])

    def test_until_with_no_offset_is_flagged(self):
        src = 'def f(until):\n    args = ["log", f"--until={until} 23:59:59"]\n'
        self.assertEqual(findings(src), ["f:git-window"])

    def test_a_plain_string_literal_with_no_offset_is_flagged(self):
        src = 'def f():\n    args = ["log", "--until=2026-09-11 23:59:59"]\n'
        self.assertEqual(findings(src), ["f:git-window"])

    def test_the_explicit_offset_shape_carries_its_own_offset_and_is_clean(self):
        """The actual line in check_context_budget.py — an f-string whose STATIC suffix names
        the offset even though the value itself is a variable."""
        src = ('def f(since, until):\n'
               '    args = ["log", f"--since={since}T00:00:00+00:00", '
               'f"--until={until}T23:59:59+00:00"]\n')
        self.assertEqual(findings(src), [])

    def test_a_trailing_z_also_counts_as_explicit(self):
        src = 'def f(until):\n    args = ["log", f"--until={until}Z"]\n'
        self.assertEqual(findings(src), [])

    def test_a_non_zero_offset_also_counts_as_explicit(self):
        src = 'def f(until):\n    args = ["log", f"--until={until}T23:59:59-05:00"]\n'
        self.assertEqual(findings(src), [])

    def test_a_tuple_literal_is_scanned_too(self):
        src = 'def f(since):\n    args = ("log", f"--since={since}")\n'
        self.assertEqual(findings(src), ["f:git-window"])

    def test_a_docstring_mentioning_the_flag_is_not_scanned(self):
        """A prose example ('...`--until=DATE 23:59:59`...') is not a list/tuple literal element and
        must not be mistaken for one — the false-positive class check_no_utcnow.py accepts and this
        gate, being --enforce, cannot."""
        src = '"""A bare `--until=DATE 23:59:59` is read in the process local zone."""\n'
        self.assertEqual(findings(src), [])

    def test_the_scripts_own_since_until_cli_flags_are_not_matched(self):
        """A script can define its OWN --since/--until CLI flags, passed by its tests as
        SEPARATE list elements with no `=` — unrelated to git's flags of the same name, and the
        prefix-with-`=` requirement is exactly what tells them apart (see the module docstring's
        "Known narrowing")."""
        src = 'def f():\n    argv = ["--root", r, "--since", "2026-08-01", "--until", "2026-08-07"]\n'
        self.assertEqual(findings(src), [])

    def test_a_startswith_check_over_the_bare_prefix_is_not_matched(self):
        """`a.startswith("--until=")` — the string constant is a CALL ARGUMENT, not a list/tuple
        element, so it must not be flagged the way a real argv construction would be."""
        src = 'def f(args):\n    return [a for a in args if a.startswith("--until=")]\n'
        self.assertEqual(findings(src), [])

    def test_an_unrelated_flag_of_similar_shape_is_not_matched(self):
        src = 'def f():\n    args = ["hold", "start", "--until-asleep"]\n'
        self.assertEqual(findings(src), [])


class MultiplePatternsAndSyntax(unittest.TestCase):
    def test_multiple_findings_in_one_module(self):
        src = ('from datetime import date, datetime\n'
               'def f():\n    return date.today()\n'
               'def g():\n    return datetime.now()\n')
        self.assertEqual(sorted(findings(src)), ["f:date-today", "g:naive-now"])

    def test_a_string_literal_containing_the_pattern_is_not_a_real_call(self):
        """A fixture string like `"x = datetime.now()\\n"` (this module's OWN positive-test shape,
        e.g. test_check_no_utcnow.py:34) must not be treated as a real Call node."""
        src = 'src = "x = datetime.now()\\n"\n'
        self.assertEqual(findings(src), [])

    def test_a_clean_file_finds_nothing(self):
        src = "def f():\n    return 1\n"
        self.assertEqual(findings(src), [])


class CiWorkflowScan(unittest.TestCase):
    def _write(self, text):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".github", "workflows"))
        with open(os.path.join(d, ".github", "workflows", "ci.yml"), "w", encoding="utf-8") as fh:
            fh.write(text)
        return d

    def test_a_bare_flag_in_a_run_step_is_flagged(self):
        d = self._write("jobs:\n  x:\n    steps:\n      - run: git log --since=2026-09-01\n")
        found = cwc.scan_ci_workflow(d)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, cwc.KIND_GIT_WINDOW)

    def test_an_explicit_offset_in_a_run_step_is_clean(self):
        d = self._write("jobs:\n  x:\n    steps:\n      - run: git log --since=2026-09-01T00:00:00+00:00\n")
        self.assertEqual(cwc.scan_ci_workflow(d), [])

    def test_a_missing_workflow_file_is_not_an_error(self):
        d = tempfile.mkdtemp()
        self.assertEqual(cwc.scan_ci_workflow(d), [])


class TheAllowlist(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "seneschal"))

    def _write(self, obj):
        with open(os.path.join(self.d, cwc.ALLOWLIST_FILE), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def test_a_missing_allowlist_is_empty_not_an_error(self):
        self.assertEqual(cwc.load_allowlist(self.d), {})

    def test_an_entry_without_a_reason_is_REFUSED(self):
        self._write({"allow": [{"path": "a/b.py", "symbol": "f", "kind": "naive-now"}]})
        with self.assertRaises(ValueError):
            cwc.load_allowlist(self.d)

    def test_a_blank_reason_is_also_refused(self):
        self._write({"allow": [{"path": "a/b.py", "symbol": "f", "kind": "naive-now",
                                 "reason": "   "}]})
        with self.assertRaises(ValueError):
            cwc.load_allowlist(self.d)

    def test_it_keys_on_path_plus_symbol_plus_kind_not_a_line_number(self):
        self._write({"allow": [{"path": "a/b.py", "symbol": "f", "kind": "naive-now",
                                 "reason": "because"}]})
        self.assertEqual(cwc.load_allowlist(self.d), {("a/b.py", "f", "naive-now"): "because"})


class MainExitCode(unittest.TestCase):
    def _repo(self, src):
        d = tempfile.mkdtemp()
        for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git"] + argv, cwd=d, capture_output=True, check=False)
        with open(os.path.join(d, "check_x.py"), "w", encoding="utf-8") as fh:
            fh.write(src)
        for argv in (["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git"] + argv, cwd=d, capture_output=True, check=False)
        return d

    def test_report_only_by_default_even_with_findings(self):
        d = self._repo("from datetime import date\nX = date.today()\n")
        self.assertEqual(cwc.main(["--root", d]), 0)

    def test_enforce_fails_on_a_live_finding(self):
        d = self._repo("from datetime import date\nX = date.today()\n")
        self.assertEqual(cwc.main(["--root", d, "--enforce"]), 1)

    def test_enforce_passes_a_clean_tree(self):
        d = self._repo("def f():\n    return 1\n")
        self.assertEqual(cwc.main(["--root", d, "--enforce"]), 0)

    def test_enforce_passes_an_allowlisted_finding(self):
        d = self._repo("from datetime import date\ndef f():\n    return date.today()\n")
        os.makedirs(os.path.join(d, "seneschal"), exist_ok=True)
        with open(os.path.join(d, cwc.ALLOWLIST_FILE), "w", encoding="utf-8") as fh:
            json.dump({"allow": [{"path": "check_x.py", "symbol": "f", "kind": "date-today",
                                   "reason": "test"}]}, fh)
        self.assertEqual(cwc.main(["--root", d, "--enforce"]), 0)


class TheLiveTree(unittest.TestCase):
    """The check runs against this repository, same as every other gate's own test module — a gate
    that is green only on synthetic input proves nothing about the tree it gates."""

    def test_the_tree_is_clean_or_every_survivor_is_allowlisted_with_a_reason(self):
        root = cwc.repo_root()
        found = cwc.scan(root)
        allow = cwc.load_allowlist(root)
        live = [str(f) for f in found if f.key() not in allow]
        self.assertEqual(live, [], "a new bare wall-clock read — see the module docstring")

    def test_the_budget_checks_window_is_present_and_clean(self):
        """Recall check: `check_context_budget.py`'s own git window (explicit +00:00 bounds) must
        still be found by the scanner's list/tuple walk and must NOT be a finding — if this ever goes empty
        because the source moved, the positive-fixture tests above are the only thing still proving
        the scanner works at all."""
        root = cwc.repo_root()
        path = os.path.join(root, "seneschal", "scripts", "check_context_budget.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("--until=", src)
        found = cwc.scan_source(src, "seneschal/scripts/check_context_budget.py")
        self.assertEqual([f for f in found if f.kind == cwc.KIND_GIT_WINDOW], [])


class WorkingTreeTests(unittest.TestCase):
    """Local gates see the working tree: an untracked
    new `check_*.py`/`test_*.py` is scanned locally before it is committed."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git"] + argv, cwd=self.d, capture_output=True, check=False)
        with open(os.path.join(self.d, "clean.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        for argv in (["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git"] + argv, cwd=self.d, capture_output=True, check=False)

    def test_an_untracked_check_script_is_scanned(self):
        with open(os.path.join(self.d, "check_fresh.py"), "w", encoding="utf-8") as fh:
            fh.write("from datetime import date\nX = date.today()\n")
        found = cwc.scan(self.d)
        self.assertEqual([(f.path, f.kind) for f in found], [("check_fresh.py", "date-today")])

    def test_a_file_outside_scope_is_ignored(self):
        with open(os.path.join(self.d, "helper.py"), "w", encoding="utf-8") as fh:
            fh.write("from datetime import date\nX = date.today()\n")
        self.assertEqual(cwc.scan(self.d), [])


if __name__ == "__main__":
    unittest.main()
