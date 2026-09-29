#!/usr/bin/env python3
"""Tests for `stateio.py` — the atomic write primitives and the append/tail/prune jsonl family.

The Windows `PermissionError` retry and the empty-payload refusal are the two things that have
actually cost state data, so they get the most coverage."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import stateio  # noqa: E402


class WriteTextAtomic(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "f.txt")

    def test_basic_roundtrip(self):
        stateio.write_text_atomic(self.path, "hello")
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello")

    def test_creates_parent_directories(self):
        nested = os.path.join(self.tmpdir.name, "a", "b", "c.txt")
        stateio.write_text_atomic(nested, "x")
        with open(nested, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "x")

    def test_no_tmp_file_survives_a_successful_write(self):
        stateio.write_text_atomic(self.path, "hello")
        leftovers = [f for f in os.listdir(self.tmpdir.name) if f != "f.txt"]
        self.assertEqual(leftovers, [])

    def test_replaces_existing_content_wholesale(self):
        stateio.write_text_atomic(self.path, "first")
        stateio.write_text_atomic(self.path, "second")
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "second")

    def test_new_empty_file_is_not_refused(self):
        stateio.write_text_atomic(self.path, "")
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")

    def test_re_emptying_an_already_empty_file_is_not_refused(self):
        stateio.write_text_atomic(self.path, "")
        stateio.write_text_atomic(self.path, "")  # must not raise
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")


class EmptyPayloadRefusal(unittest.TestCase):
    """The empty-payload loss, as a test: an atomically-correct write of zero bytes over a target
    that held content must be refused, and must not touch the target."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "context-digest.md")
        stateio.write_text_atomic(self.path, "x" * 19336)

    def test_empty_write_over_held_content_is_refused(self):
        with self.assertRaises(stateio.EmptyWriteRefused):
            stateio.write_text_atomic(self.path, "")

    def test_the_refusal_leaves_the_original_content_untouched(self):
        try:
            stateio.write_text_atomic(self.path, "")
        except stateio.EmptyWriteRefused:
            pass
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(len(fh.read()), 19336)

    def test_the_refusal_leaves_no_tmp_file_behind(self):
        try:
            stateio.write_text_atomic(self.path, "")
        except stateio.EmptyWriteRefused:
            pass
        leftovers = [f for f in os.listdir(self.tmpdir.name) if f != "context-digest.md"]
        self.assertEqual(leftovers, [])

    def test_allow_empty_overrides_the_refusal(self):
        stateio.write_text_atomic(self.path, "", allow_empty=True)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")

    def test_the_refusal_message_names_the_held_size(self):
        with self.assertRaises(stateio.EmptyWriteRefused) as ctx:
            stateio.write_text_atomic(self.path, "")
        self.assertIn("19,336", str(ctx.exception))


class WriteJsonAtomic(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "f.json")

    def test_roundtrip(self):
        stateio.write_json_atomic(self.path, {"a": 1, "b": [1, 2, 3]})
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"a": 1, "b": [1, 2, 3]})

    def test_empty_dict_is_never_refused_over_existing_content(self):
        stateio.write_json_atomic(self.path, {"held": True})
        stateio.write_json_atomic(self.path, {})  # must not raise EmptyWriteRefused
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {})

    def test_unicode_is_written_readably_not_escaped(self):
        stateio.write_json_atomic(self.path, {"name": "Zoë"})
        with open(self.path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertIn("Zoë", raw)


class PermissionErrorRetry(unittest.TestCase):
    """Ported from `memory_write.write_text`'s retry loop: Windows `PermissionError` on
    `os.replace` must be retried, with a bounded backoff, before giving up."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "f.txt")

    def test_retries_and_eventually_succeeds(self):
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("WinError 5")
            return real_replace(src, dst)

        with mock.patch("stateio.time.sleep") as sleep_mock, \
                mock.patch("stateio.os.replace", side_effect=flaky_replace):
            stateio.write_text_atomic(self.path, "hello")
        self.assertEqual(calls["n"], 3)
        self.assertGreaterEqual(sleep_mock.call_count, 2)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello")

    def test_gives_up_after_the_attempt_cap_and_raises(self):
        with mock.patch("stateio.time.sleep"), \
                mock.patch("stateio.os.replace", side_effect=PermissionError("WinError 5")):
            with self.assertRaises(PermissionError):
                stateio.write_text_atomic(self.path, "hello")

    def test_gives_up_cleans_the_tmp_file(self):
        with mock.patch("stateio.time.sleep"), \
                mock.patch("stateio.os.replace", side_effect=PermissionError("WinError 5")):
            with self.assertRaises(PermissionError):
                stateio.write_text_atomic(self.path, "hello")
        leftovers = os.listdir(self.tmpdir.name)
        self.assertEqual(leftovers, [])

    def test_a_non_permission_error_is_not_retried(self):
        calls = {"n": 0}

        def raises_other(src, dst):
            calls["n"] += 1
            raise OSError("disk full")

        with mock.patch("stateio.time.sleep") as sleep_mock, \
                mock.patch("stateio.os.replace", side_effect=raises_other):
            with self.assertRaises(OSError):
                stateio.write_text_atomic(self.path, "hello")
        self.assertEqual(calls["n"], 1)
        sleep_mock.assert_not_called()


class JsonlFamily(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "f.jsonl")

    def test_append_and_iter_roundtrip(self):
        stateio.append_jsonl(self.path, {"a": 1})
        stateio.append_jsonl(self.path, {"a": 2})
        self.assertEqual(list(stateio.iter_jsonl(self.path)), [{"a": 1}, {"a": 2}])

    def test_iter_on_a_missing_file_yields_nothing(self):
        self.assertEqual(list(stateio.iter_jsonl(self.path)), [])

    def test_iter_skips_malformed_lines(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"a": 1}\n')
            fh.write("not json\n")
            fh.write("\n")
            fh.write('{"a": 2}\n')
            fh.write("[1, 2, 3]\n")  # valid JSON, not a dict -- also skipped
        self.assertEqual(list(stateio.iter_jsonl(self.path)), [{"a": 1}, {"a": 2}])

    def test_tail_returns_the_newest_n(self):
        for i in range(5):
            stateio.append_jsonl(self.path, {"i": i})
        self.assertEqual(stateio.tail_jsonl(self.path, 2), [{"i": 3}, {"i": 4}])

    def test_tail_zero_returns_empty(self):
        stateio.append_jsonl(self.path, {"i": 0})
        self.assertEqual(stateio.tail_jsonl(self.path, 0), [])

    def test_tail_negative_returns_everything(self):
        for i in range(3):
            stateio.append_jsonl(self.path, {"i": i})
        self.assertEqual(stateio.tail_jsonl(self.path, -1), [{"i": 0}, {"i": 1}, {"i": 2}])

    def test_append_creates_parent_directories(self):
        nested = os.path.join(self.tmpdir.name, "a", "b", "f.jsonl")
        stateio.append_jsonl(nested, {"a": 1})
        self.assertEqual(list(stateio.iter_jsonl(nested)), [{"a": 1}])


class PruneJsonl(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "f.jsonl")

    def test_drops_rows_the_predicate_rejects(self):
        for i in range(4):
            stateio.append_jsonl(self.path, {"i": i})
        dropped = stateio.prune_jsonl(self.path, keep=lambda row: row["i"] % 2 == 0)
        self.assertEqual(dropped, 2)
        self.assertEqual(list(stateio.iter_jsonl(self.path)), [{"i": 0}, {"i": 2}])

    def test_nothing_dropped_returns_zero_and_does_not_touch_the_file(self):
        stateio.append_jsonl(self.path, {"i": 0})
        mtime_before = os.path.getmtime(self.path)
        dropped = stateio.prune_jsonl(self.path, keep=lambda row: True)
        self.assertEqual(dropped, 0)
        self.assertEqual(os.path.getmtime(self.path), mtime_before)

    def test_a_malformed_line_is_always_kept(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"i": 1}\n')
            fh.write("not json\n")
        dropped = stateio.prune_jsonl(self.path, keep=lambda row: False)
        self.assertEqual(dropped, 1)  # only the one well-formed, rejected row
        with open(self.path, encoding="utf-8") as fh:
            self.assertIn("not json", fh.read())

    def test_a_raising_predicate_keeps_the_row(self):
        stateio.append_jsonl(self.path, {"i": 1})

        def boom(row):
            raise RuntimeError("boom")

        dropped = stateio.prune_jsonl(self.path, keep=boom)
        self.assertEqual(dropped, 0)
        self.assertEqual(list(stateio.iter_jsonl(self.path)), [{"i": 1}])

    def test_pruning_everything_to_empty_is_not_refused(self):
        stateio.append_jsonl(self.path, {"i": 1})
        dropped = stateio.prune_jsonl(self.path, keep=lambda row: False)
        self.assertEqual(dropped, 1)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")

    def test_missing_file_returns_zero(self):
        self.assertEqual(stateio.prune_jsonl(self.path, keep=lambda row: True), 0)


if __name__ == "__main__":
    unittest.main()
