#!/usr/bin/env python3
"""Unit tests for ``identity_common`` (the persona/identity.json plumbing). Stdlib only.

The contract under test: :func:`load_identity` NEVER raises — a missing file silently yields
DEFAULTS (the normal fresh-install state), any other problem yields DEFAULTS with exactly one
stderr warning — and a valid file deep-merges over DEFAULTS (missing keys defaulted, unknown
keys preserved). Each test uses a throwaway temp dir; nothing touches the real persona/.

Run:  python -m unittest seneschal.scripts.test_identity_common   (or)   python test_identity_common.py
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import identity_common as ic  # noqa: E402


class LoadIdentityTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="identity-test-")
        self.path = os.path.join(self.dir, "identity.json")

    def write(self, content):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(content if isinstance(content, str) else json.dumps(content))

    def load(self):
        """load_identity with stderr captured; returns (identity, stderr_text)."""
        err = io.StringIO()
        with redirect_stderr(err):
            identity = ic.load_identity(self.path)
        return identity, err.getvalue()

    def test_missing_file_is_defaults_and_silent(self):
        identity, err = self.load()
        self.assertEqual(identity, ic.DEFAULTS)
        self.assertEqual(err, "")  # fresh install is the normal state — no noise

    def test_missing_file_returns_a_copy_not_defaults_itself(self):
        identity, _ = self.load()
        self.assertIsNot(identity, ic.DEFAULTS)
        identity["assistant"]["name"] = "mutated"
        self.assertIsNone(ic.DEFAULTS["assistant"]["name"])  # caller mutation can't poison DEFAULTS

    def test_corrupt_json_is_defaults_with_one_warning(self):
        self.write("{ not json at all")
        identity, err = self.load()
        self.assertEqual(identity, ic.DEFAULTS)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn(self.path, err)

    def test_wrong_type_file_is_defaults_with_one_warning(self):
        self.write(["a", "list", "not", "an", "object"])
        identity, err = self.load()
        self.assertEqual(identity, ic.DEFAULTS)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_unreadable_path_is_defaults_with_one_warning(self):
        self.path = self.dir  # a directory: exists, but open() fails
        identity, err = self.load()
        self.assertEqual(identity, ic.DEFAULTS)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_partial_file_merges_over_defaults(self):
        self.write({"owner": {"name": "Sam"}})
        identity, err = self.load()
        self.assertEqual(err, "")
        self.assertEqual(identity["owner"]["name"], "Sam")
        self.assertIsNone(identity["owner"]["timezone"])          # missing keys defaulted
        self.assertEqual(identity["assistant"]["pronouns"], "they/them")

    def test_unknown_keys_are_preserved(self):
        self.write({"schema": 2, "owner": {"favoriteColor": "purple"}, "extra": {"x": 1}})
        identity, _ = self.load()
        self.assertEqual(identity["schema"], 2)
        self.assertEqual(identity["owner"]["favoriteColor"], "purple")
        self.assertEqual(identity["extra"], {"x": 1})
        self.assertIsNone(identity["owner"]["name"])  # defaults still filled in around them

    def test_configured_values_win_over_defaults(self):
        self.write({"assistant": {"name": "Aria", "pronouns": "she/her"},
                    "owner": {"timezone": "America/Chicago"}})
        identity, _ = self.load()
        self.assertEqual(identity["assistant"]["name"], "Aria")
        self.assertEqual(identity["assistant"]["pronouns"], "she/her")
        self.assertEqual(identity["owner"]["timezone"], "America/Chicago")


class AccessorTest(unittest.TestCase):
    def test_fallbacks_on_defaults(self):
        self.assertEqual(ic.assistant_name(ic.DEFAULTS), "the assistant")
        self.assertEqual(ic.owner_name(ic.DEFAULTS), "the owner")
        self.assertEqual(ic.owner_tz_label(ic.DEFAULTS), "the machine's local timezone")

    def test_configured_values(self):
        identity = {"assistant": {"name": "Aria"},
                    "owner": {"name": "Sam", "timezone": "America/Chicago"}}
        self.assertEqual(ic.assistant_name(identity), "Aria")
        self.assertEqual(ic.owner_name(identity), "Sam")
        self.assertEqual(ic.owner_tz_label(identity), "America/Chicago")

    def test_wrong_shapes_fall_back(self):
        # A hand-edited file can carry any shape; accessors must never raise.
        self.assertEqual(ic.assistant_name({"assistant": "not-a-dict"}), "the assistant")
        self.assertEqual(ic.owner_name({"owner": {"name": 42}}), "the owner")
        self.assertEqual(ic.owner_tz_label({}), "the machine's local timezone")
        self.assertEqual(ic.assistant_name({"assistant": {"name": "   "}}), "the assistant")

    def test_get_str_strips_and_rejects_non_strings(self):
        self.assertEqual(ic.get_str({"owner": {"name": "  Sam  "}}, "owner", "name"), "Sam")
        self.assertIsNone(ic.get_str({"owner": {"name": ""}}, "owner", "name"))
        self.assertIsNone(ic.get_str({"owner": {"name": None}}, "owner", "name"))
        self.assertIsNone(ic.get_str([], "owner", "name"))


if __name__ == "__main__":
    unittest.main()
