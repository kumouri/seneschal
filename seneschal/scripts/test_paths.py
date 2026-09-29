#!/usr/bin/env python3
"""Tests for `paths.py` — REPO_ROOT/SCRIPTS_DIR resolution and the SENESCHAL_STATE_DIR precedence."""
from __future__ import annotations

import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import paths  # noqa: E402


class RepoLayout(unittest.TestCase):
    def test_scripts_dir_is_this_directory(self):
        self.assertEqual(os.path.normpath(paths.SCRIPTS_DIR), os.path.normpath(SCRIPT_DIR))

    def test_repo_root_is_two_levels_above_scripts_dir(self):
        expected = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
        self.assertEqual(os.path.normpath(paths.REPO_ROOT), expected)

    def test_repo_root_contains_seneschal_scripts(self):
        # A cheap live-tree sanity check, not a stronger claim: REPO_ROOT should actually be the
        # checkout root, not some arithmetic that happens to look right.
        self.assertTrue(os.path.isdir(os.path.join(paths.REPO_ROOT, "seneschal", "scripts")))


class StateDirPrecedence(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(paths.ENV_VAR, None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop(paths.ENV_VAR, None)
        else:
            os.environ[paths.ENV_VAR] = self._saved

    def test_default_with_nothing_set(self):
        self.assertEqual(
            os.path.normpath(paths.state_dir()),
            os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state")),
        )

    def test_explicit_argument_wins_over_default(self):
        self.assertEqual(paths.state_dir("/tmp/somewhere"), "/tmp/somewhere")

    def test_env_var_wins_over_explicit_argument(self):
        os.environ[paths.ENV_VAR] = "/tmp/from-env"
        self.assertEqual(paths.state_dir("/tmp/somewhere"), "/tmp/from-env")

    def test_env_var_wins_with_no_explicit_argument(self):
        os.environ[paths.ENV_VAR] = "/tmp/from-env"
        self.assertEqual(paths.state_dir(), "/tmp/from-env")

    def test_empty_string_explicit_falls_through_to_default(self):
        # An explicit "" is falsy, same as None -- a caller passing an empty string (an unset CLI
        # flag with a default of "") must not get a state dir of "".
        self.assertEqual(
            os.path.normpath(paths.state_dir("")),
            os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state")),
        )

    def test_blank_env_var_is_treated_as_unset(self):
        os.environ[paths.ENV_VAR] = ""
        self.assertEqual(paths.state_dir("/tmp/somewhere"), "/tmp/somewhere")


if __name__ == "__main__":
    unittest.main()
