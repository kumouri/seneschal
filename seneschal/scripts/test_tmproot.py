#!/usr/bin/env python3
"""Tests for `tmproot.py` — the one-temp-root-per-run guard behind the 2026-09-29 Temp flood.

The redirect itself is asserted by `test_0_tmproot.py` in every root (`tmproot.guard_tests`); this
file covers the pieces: removal that survives read-only git objects, the stale-root sweep that never
reaches outside `seneschal-test-runs/`, own-vs-adopt, the leak count, and that every root CI discovers
actually carries a guard — a new suite added to `ci.yml` without one would leak silently again."""
from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tmproot  # noqa: E402

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _touch(path: str, readonly: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")
    if readonly:
        os.chmod(path, stat.S_IREAD)


class RmtreeTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(tmproot.rmtree, self.base)

    def test_removes_read_only_files_the_way_a_git_objects_dir_holds_them(self):
        tree = os.path.join(self.base, "seneschald_test_x")
        _touch(os.path.join(tree, ".git", "objects", "ab", "cdef"), readonly=True)
        _touch(os.path.join(tree, ".git", "HEAD"), readonly=True)
        self.assertEqual(tmproot.rmtree(tree), [])
        self.assertFalse(os.path.exists(tree))

    @unittest.skipIf(sys.platform == "win32", "POSIX directory permission bits")
    def test_removes_a_tree_under_a_directory_made_unwritable(self):
        tree = os.path.join(self.base, "locked")
        _touch(os.path.join(tree, "inner", "f"))
        os.chmod(os.path.join(tree, "inner"), stat.S_IREAD | stat.S_IEXEC)
        self.assertEqual(tmproot.rmtree(tree), [])
        self.assertFalse(os.path.exists(tree))

    def test_a_missing_path_is_not_an_error(self):
        self.assertEqual(tmproot.rmtree(os.path.join(self.base, "never-made")), [])


class SweepStaleTest(unittest.TestCase):
    def setUp(self):
        self.parent = tempfile.mkdtemp()
        self.addCleanup(tmproot.rmtree, self.parent)

    def _root(self, name: str, age: float, now: float) -> str:
        path = os.path.join(self.parent, name)
        _touch(os.path.join(path, "tmpabcdefgh", "state.json"), readonly=True)
        os.utime(path, (now - age, now - age))
        return path

    def test_removes_only_roots_older_than_the_limit_and_never_the_kept_one(self):
        now = time.time()
        old = self._root("111-old", tmproot.STALE_AFTER_SEC + 60, now)
        young = self._root("222-young", 60, now)
        kept = self._root("333-kept", tmproot.STALE_AFTER_SEC + 60, now)
        self.assertEqual(tmproot.sweep_stale(self.parent, now=now, keep=kept), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(young))
        self.assertTrue(os.path.exists(kept))

    def test_a_missing_parent_sweeps_nothing(self):
        self.assertEqual(tmproot.sweep_stale(os.path.join(self.parent, "absent"), now=time.time()), 0)


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(tmproot.rmtree, self.base)

    def test_no_inherited_root_means_own_one_under_runs_dir(self):
        root = tmproot.resolve(environ={}, base=self.base)
        self.assertTrue(root.owned)
        self.assertEqual(os.path.dirname(root.path), os.path.join(self.base, tmproot.RUNS_DIR))
        self.assertTrue(os.path.basename(root.path).startswith("%d-" % os.getpid()))
        self.assertEqual(root.real_tmp, self.base)

    def test_a_live_inherited_root_is_adopted_and_never_removed(self):
        live = os.path.join(self.base, tmproot.RUNS_DIR, "999-parent")
        os.makedirs(live)
        root = tmproot.resolve(environ={tmproot.ENV_VAR: live}, base=self.base)
        self.assertFalse(root.owned)
        self.assertEqual(root.path, live)
        self.assertEqual(os.path.normcase(root.real_tmp), os.path.normcase(self.base))
        self.assertEqual(root.close(), (0, []))
        self.assertTrue(os.path.isdir(live))

    def test_a_dead_inherited_root_is_not_adopted(self):
        gone = os.path.join(self.base, tmproot.RUNS_DIR, "999-gone")
        root = tmproot.resolve(environ={tmproot.ENV_VAR: gone}, base=self.base)
        self.assertTrue(root.owned)
        self.assertNotEqual(root.path, gone)

    def test_owner_close_counts_what_the_run_left_and_removes_it(self):
        root = tmproot.resolve(environ={}, base=self.base)
        _touch(os.path.join(root.path, "tmpaaaaaaaa", ".git", "objects", "x"), readonly=True)
        _touch(os.path.join(root.path, "tmpbbbbbbbb", "pantry.json"))
        self.assertEqual(root.close(), (2, []))
        self.assertFalse(os.path.exists(root.path))

    def test_redirect_sets_every_key_a_child_reads(self):
        env = {}
        current = tempfile.gettempdir()  # already the run root; redirecting to it changes nothing
        tmproot.redirect(current, environ=env)
        for key in tmproot.TEMP_ENV_KEYS + (tmproot.ENV_VAR,):
            self.assertEqual(env[key], current)


class ExitReportTest(unittest.TestCase):
    def test_a_clean_run_prints_nothing(self):
        root = tmproot.RunRoot("R", "T", owned=True, started=0.0)
        self.assertEqual(tmproot.exit_report(root, 0, []), "")

    def test_leftovers_and_failures_are_each_named(self):
        root = tmproot.RunRoot("R", "T", owned=True, started=0.0)
        text = tmproot.exit_report(root, 1234, ["R/x/.git/index"])
        self.assertIn("swept 1234 entries", text)
        self.assertIn("could not remove 1 path(s)", text)
        self.assertIn("R/x/.git/index", text)


class NewLeaksTest(unittest.TestCase):
    def setUp(self):
        self.real = tempfile.mkdtemp()
        self.addCleanup(tmproot.rmtree, self.real)
        for name in ("tmpabcd1234", "seneschald_test_abcdefgh", "tmpshort", "seneschal-test-runs", "foo"):
            os.makedirs(os.path.join(self.real, name))

    def test_counts_only_leak_shaped_names_created_since(self):
        count, names = tmproot.new_leaks(self.real, since=time.time() - 60)
        self.assertEqual(count, 2)
        self.assertEqual(sorted(names), ["seneschald_test_abcdefgh", "tmpabcd1234"])

    def test_nothing_created_after_the_instant_counts(self):
        self.assertEqual(tmproot.new_leaks(self.real, since=time.time() + 3600), (0, []))

    def test_an_unreadable_dir_reports_zero_rather_than_raising(self):
        self.assertEqual(tmproot.new_leaks(os.path.join(self.real, "absent"), since=0.0), (0, []))


class EveryCiRootIsGuardedTest(unittest.TestCase):
    """A suite root added to `ci.yml` without a `test_0_tmproot.py` would run un-redirected."""

    def test_every_discover_root_in_ci_yml_has_a_guard(self):
        with open(os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8") as f:
            roots = sorted(set(re.findall(r"unittest discover -s (\S+)", f.read())))
        self.assertGreaterEqual(len(roots), 4, roots)
        missing = [r for r in roots
                   if not os.path.isfile(os.path.join(REPO_ROOT, *r.split("/"), "test_0_tmproot.py"))]
        self.assertEqual(missing, [], "roots CI discovers with no test_0_tmproot.py guard")


if __name__ == "__main__":
    unittest.main()
