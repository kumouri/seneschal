#!/usr/bin/env python3
"""save_json's atomic-replace retry — the write half of the Windows shared-file-handle race.

Companion to load_json's read-side retry. On Windows, `os.replace` onto a destination another process
holds open (the updater's liveness check reading `presence.lock`, a cockpit reader) raises
PermissionError/WinError 5 where POSIX renames straight through; unhandled, it crashes the scheduler
task and the daemon stays down until a hand restart.

These tests fake the collision (rather than depending on real Windows sharing semantics) so they assert
the retry contract identically on every platform CI runs on.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sentinel  # noqa: E402


class SaveJsonReplaceRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "presence.lock")

    def test_writes_through_when_uncontended(self):
        sentinel.save_json(self.path, {"pid": 1234})
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"pid": 1234})

    def test_retries_past_a_transient_permission_error(self):
        """The real-world case: a reader holds the destination open for one moment, then lets go."""
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(5, "Access is denied")
            return real_replace(src, dst)

        with mock.patch.object(sentinel.os, "replace", side_effect=flaky):
            sentinel.save_json(self.path, {"heartbeat": "beat"})

        self.assertEqual(calls["n"], 2, "should have retried exactly once after the transient failure")
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"heartbeat": "beat"})

    def test_survives_a_contended_burst_within_the_retry_budget(self):
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < sentinel._REPLACE_ATTEMPTS:
                raise PermissionError(5, "Access is denied")
            return real_replace(src, dst)

        with mock.patch.object(sentinel.os, "replace", side_effect=flaky):
            sentinel.save_json(self.path, {"ok": True})

        self.assertEqual(calls["n"], sentinel._REPLACE_ATTEMPTS)
        self.assertTrue(os.path.exists(self.path))

    def test_raises_when_the_handle_never_clears(self):
        """A persistent failure is a REAL failure (disk full, a genuinely stuck handle) and must
        surface — swallowing it would leave a silently-unsaved file, which is worse than a crash."""
        with mock.patch.object(sentinel.os, "replace",
                               side_effect=PermissionError(5, "Access is denied")) as replace:
            with self.assertRaises(PermissionError):
                sentinel.save_json(self.path, {"nope": True})
        self.assertEqual(replace.call_count, sentinel._REPLACE_ATTEMPTS,
                         "should exhaust the full retry budget before giving up")

    def test_retry_budget_stays_under_the_scheduler_beat(self):
        """save_json's hottest caller is the ~5s scheduler heartbeat; the backoff must not stall it."""
        worst_case = sum(sentinel._REPLACE_BACKOFF_SEC * (i + 1)
                         for i in range(sentinel._REPLACE_ATTEMPTS - 1))
        self.assertLess(worst_case, 2.0)


class LoadJsonReadRetryTest(unittest.TestCase):
    """The read half: a transient PermissionError on open is retried once, then reads as the
    default — never propagated into the scheduler."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "x.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"a": 1}, fh)

    def test_a_transient_lock_is_retried(self):
        real_open = open
        calls = {"n": 0}

        def flaky(path, *a, **k):
            if path == self.path:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError(13, "locked")
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", side_effect=flaky):
            self.assertEqual(sentinel.load_json(self.path, None), {"a": 1})
        self.assertEqual(calls["n"], 2)

    def test_a_persistent_lock_reads_as_the_default(self):
        with mock.patch("builtins.open", side_effect=PermissionError(13, "locked")):
            self.assertEqual(sentinel.load_json(self.path, "default"), "default")


class ParseIsoTypeError(unittest.TestCase):
    """A non-string (a cache file clobbered into a JSON object) raises TypeError — which every caller
    already guards alongside ValueError — never the raw AttributeError a str method would throw."""

    def test_non_strings_raise_type_error(self):
        for junk in ({"at": 1}, [1], 12, None):
            with self.assertRaises(TypeError):
                sentinel.parse_iso(junk)

    def test_strings_still_parse(self):
        self.assertEqual(sentinel.parse_iso("2026-06-29T20:00:00Z").isoformat(),
                         "2026-06-29T20:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
