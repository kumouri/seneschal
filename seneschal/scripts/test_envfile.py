#!/usr/bin/env python3
"""Tests for `envfile.py` — the `load_env` union of the existing copies' behaviour."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import envfile  # noqa: E402


def _write(tmpdir: str, name: str, content: str) -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


class LoadEnvFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def test_none_env_file_returns_empty_dict(self):
        self.assertEqual(envfile.load_env(None), {})

    def test_basic_key_value(self):
        p = _write(self.tmpdir.name, "e.env", "FOO=bar\n")
        self.assertEqual(envfile.load_env(p), {"FOO": "bar"})

    def test_blank_lines_and_comments_are_skipped(self):
        p = _write(self.tmpdir.name, "e.env", "\n# a comment\nFOO=bar\n   \n# FOO=baz\n")
        self.assertEqual(envfile.load_env(p), {"FOO": "bar"})

    def test_a_line_with_no_equals_is_skipped(self):
        p = _write(self.tmpdir.name, "e.env", "not-a-kv-line\nFOO=bar\n")
        self.assertEqual(envfile.load_env(p), {"FOO": "bar"})

    def test_only_the_first_equals_splits(self):
        p = _write(self.tmpdir.name, "e.env", "FOO=a=b=c\n")
        self.assertEqual(envfile.load_env(p), {"FOO": "a=b=c"})

    def test_double_quotes_are_stripped(self):
        p = _write(self.tmpdir.name, "e.env", 'FOO="bar"\n')
        self.assertEqual(envfile.load_env(p), {"FOO": "bar"})

    def test_single_quotes_are_stripped(self):
        p = _write(self.tmpdir.name, "e.env", "FOO='bar'\n")
        self.assertEqual(envfile.load_env(p), {"FOO": "bar"})

    def test_key_and_value_whitespace_is_stripped(self):
        p = _write(self.tmpdir.name, "e.env", "  FOO  =  bar  \n")
        self.assertEqual(envfile.load_env(p), {"FOO": "bar"})

    def test_a_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            envfile.load_env(os.path.join(self.tmpdir.name, "nope.env"))


class ProcessEnvOverride(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self._saved = {}

    def _set_env(self, key, value):
        self._saved[key] = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(self._restore, key)

    def _restore(self, key):
        if self._saved.get(key) is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = self._saved[key]

    def test_default_env_keys_overrides_any_key_present_in_the_file(self):
        self._set_env("FOO", "from-env")
        p = _write(self.tmpdir.name, "e.env", "FOO=from-file\n")
        self.assertEqual(envfile.load_env(p), {"FOO": "from-env"})

    def test_a_key_not_in_the_file_is_not_added_by_default(self):
        self._set_env("BAR", "from-env")
        p = _write(self.tmpdir.name, "e.env", "FOO=from-file\n")
        # BAR is in the real environment but never appeared in the file, so with env_keys=None
        # (only override keys already present) it must not silently appear.
        self.assertEqual(envfile.load_env(p), {"FOO": "from-file"})

    def test_explicit_env_keys_restricts_which_keys_may_be_overridden(self):
        self._set_env("FOO", "from-env")
        p = _write(self.tmpdir.name, "e.env", "FOO=from-file\nBAZ=untouched\n")
        got = envfile.load_env(p, env_keys=["QUUX"])  # FOO not in the allowed set
        self.assertEqual(got, {"FOO": "from-file", "BAZ": "untouched"})

    def test_explicit_env_keys_can_pull_in_a_key_absent_from_the_file(self):
        self._set_env("QUUX", "from-env")
        p = _write(self.tmpdir.name, "e.env", "FOO=from-file\n")
        got = envfile.load_env(p, env_keys=["QUUX"])
        self.assertEqual(got, {"FOO": "from-file", "QUUX": "from-env"})

    def test_a_blank_process_env_value_does_not_override(self):
        self._set_env("FOO", "")
        p = _write(self.tmpdir.name, "e.env", "FOO=from-file\n")
        self.assertEqual(envfile.load_env(p), {"FOO": "from-file"})


if __name__ == "__main__":
    unittest.main()
