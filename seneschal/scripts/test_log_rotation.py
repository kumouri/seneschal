#!/usr/bin/env python3
"""Tests for log_rotation.py — the module docstring is the design record.

Covers the things that actually break, per the module docstring: rotation under a second
open handle (simulated as a rename that raises `PermissionError`, since this suite doesn't spawn real
child processes), a failed rotation degrading to append with nothing lost, no line lost across a
successful roll, the size-trigger boundary, retention pruning, and compression.

Run: python -m unittest seneschal.scripts.test_log_rotation   (or)   python test_log_rotation.py
"""
from __future__ import annotations

import gzip
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import log_rotation as lr  # noqa: E402


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class NeedsRotationTests(unittest.TestCase):
    def test_under_trigger_is_false(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"a" * 10)
        self.assertFalse(lr.needs_rotation(p, max_bytes=100))

    def test_over_trigger_is_true(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"a" * 101)
        self.assertTrue(lr.needs_rotation(p, max_bytes=100))

    def test_exactly_at_trigger_is_false(self):
        """The boundary: `> max_bytes`, not `>=` — a file sized exactly to the trigger has not yet
        exceeded it."""
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"a" * 100)
        self.assertFalse(lr.needs_rotation(p, max_bytes=100))

    def test_missing_file_is_false(self):
        self.assertFalse(lr.needs_rotation(os.path.join(tempfile.mkdtemp(), "nope.log")))


class RollClosedTests(unittest.TestCase):
    def test_under_trigger_does_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"small")
        outcome = lr.roll_closed(p, max_bytes=1000)
        self.assertFalse(outcome.rolled)
        self.assertIsNone(outcome.reason)
        self.assertTrue(os.path.exists(p))
        self.assertEqual(_read_bytes(p), b"small")

    def test_over_trigger_rolls_and_recreates_no_original(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"a" * 200)
        outcome = lr.roll_closed(p, max_bytes=100, compress=False)
        self.assertTrue(outcome.rolled)
        self.assertFalse(os.path.exists(p), "the original path must be free for the next writer")
        self.assertTrue(os.path.exists(outcome.rolled_path))
        self.assertEqual(_read_bytes(outcome.rolled_path), b"a" * 200)

    def test_no_line_lost_across_a_roll(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        payload = b"line-one\nline-two\n" * 20
        _write(p, payload)
        outcome = lr.roll_closed(p, max_bytes=10, compress=False)
        self.assertTrue(outcome.rolled)
        self.assertEqual(_read_bytes(outcome.rolled_path), payload)

    def test_force_rolls_under_the_trigger(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"tiny")
        outcome = lr.roll_closed(p, max_bytes=10_000, force=True, compress=False)
        self.assertTrue(outcome.rolled)

    def test_force_on_missing_file_declines_gracefully(self):
        outcome = lr.roll_closed(os.path.join(tempfile.mkdtemp(), "nope.log"), force=True)
        self.assertFalse(outcome.rolled)
        self.assertIsNotNone(outcome.reason)

    def test_permission_error_declines_and_leaves_the_original_intact(self):
        """Simulates the Windows trap: a subprocess (or a stray reader) still has the file open, so
        the rename raises. The roll must decline, not raise, and the original must be untouched —
        the caller keeps appending to it and tries again next pass."""
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"a" * 200)
        with mock.patch.object(lr.os, "replace", side_effect=PermissionError("still open")):
            outcome = lr.roll_closed(p, max_bytes=100)
        self.assertFalse(outcome.rolled)
        self.assertIn("PermissionError", outcome.reason)
        self.assertTrue(os.path.exists(p))
        self.assertEqual(_read_bytes(p), b"a" * 200)

    def test_timestamp_collision_gets_a_disambiguating_suffix(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"a" * 200)
        first = lr.roll_closed(p, max_bytes=100, compress=False, now=1_700_000_000.0)
        _write(p, b"b" * 200)
        second = lr.roll_closed(p, max_bytes=100, compress=False, now=1_700_000_000.0)
        self.assertTrue(first.rolled and second.rolled)
        self.assertNotEqual(first.rolled_path, second.rolled_path)
        self.assertEqual(_read_bytes(first.rolled_path), b"a" * 200)
        self.assertEqual(_read_bytes(second.rolled_path), b"b" * 200)

    def test_compression_shrinks_and_preserves_content(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        payload = b"repeat-me " * 5000
        _write(p, payload)
        outcome = lr.roll_closed(p, max_bytes=100, compress=True)
        self.assertTrue(outcome.rolled)
        self.assertTrue(outcome.rolled_path.endswith(".gz"))
        with gzip.open(outcome.rolled_path, "rb") as fh:
            self.assertEqual(fh.read(), payload)
        self.assertLess(os.path.getsize(outcome.rolled_path), len(payload))

    def test_failed_compression_leaves_the_plain_roll_readable(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"a" * 200)
        with mock.patch.object(lr.gzip, "open", side_effect=OSError("disk full")):
            outcome = lr.roll_closed(p, max_bytes=100, compress=True)
        self.assertTrue(outcome.rolled)
        self.assertFalse(outcome.rolled_path.endswith(".gz"))
        self.assertEqual(_read_bytes(outcome.rolled_path), b"a" * 200)

    def test_retention_prunes_oldest_generations_first(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        for i in range(5):
            _write(p, b"a" * 200)
            lr.roll_closed(p, max_bytes=100, compress=False, keep=2, now=1_700_000_000.0 + i)
        survivors = lr._generations(d, "x.log")
        self.assertEqual(len(survivors), 2)

    def test_keep_zero_means_never_prune(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        for i in range(5):
            _write(p, b"a" * 200)
            lr.roll_closed(p, max_bytes=100, compress=False, keep=0, now=1_700_000_000.0 + i)
        self.assertEqual(len(lr._generations(d, "x.log")), 5)


class RotatingAppendLogTests(unittest.TestCase):
    def test_writes_append_below_the_trigger(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        log = lr.RotatingAppendLog(p, max_bytes=10_000)
        log.write("one\n")
        log.write("two\n")
        log.close()
        self.assertEqual(_read_text(p), "one\ntwo\n")

    def test_rolls_itself_once_over_the_trigger_and_keeps_writing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        events = []
        log = lr.RotatingAppendLog(p, max_bytes=20, compress=False, on_event=events.append)
        log.write("x" * 30 + "\n")   # crosses the trigger on this write's own bytes
        log.write("after-roll\n")
        log.close()
        self.assertTrue(any("rolled" in e for e in events))
        self.assertIn("after-roll", _read_text(p))
        rolled = [n for n in os.listdir(d) if n != "x.log"]
        self.assertEqual(len(rolled), 1)
        self.assertIn("x" * 30, _read_text(os.path.join(d, rolled[0])))

    def test_a_roll_that_cannot_happen_still_appends(self):
        """The degrade-to-append contract: if the rename can't happen (something else has the file
        open), the object must keep accepting writes against the original, oversized file rather than
        losing them or raising."""
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        events = []
        log = lr.RotatingAppendLog(p, max_bytes=5, compress=False, on_event=events.append)
        with mock.patch.object(lr.os, "replace", side_effect=PermissionError("still open")):
            log.write("x" * 20 + "\n")   # passes the pre-write size check (a fresh fh starts at 0)
            log.write("more\n")          # now the fh IS oversized — this write tries to roll first
        log.close()
        self.assertTrue(any("could not roll" in e for e in events))
        content = _read_text(p)
        self.assertIn("x" * 20, content)
        self.assertIn("more", content)

    def test_reopen_failure_costs_the_line_not_the_process(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        log = lr.RotatingAppendLog(p, max_bytes=10_000)
        log.close()
        with mock.patch.object(lr, "open", side_effect=OSError("no space"), create=True):
            log._fh = None
            log.write("lost\n")  # must not raise


class CliTests(unittest.TestCase):
    def test_check_reports_under(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"a" * 10)
        self.assertEqual(lr.main(["check", "--path", p, "--max-bytes", "100"]), 0)

    def test_check_reports_over(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"a" * 200)
        self.assertEqual(lr.main(["check", "--path", p, "--max-bytes", "100"]), 1)

    def test_roll_without_force_under_trigger_is_a_no_op_exit_0(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"tiny")
        self.assertEqual(lr.main(["roll", "--path", p, "--max-bytes", "10000"]), 0)
        self.assertTrue(os.path.exists(p))

    def test_roll_force_rolls_and_exits_0(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.log")
        _write(p, b"tiny")
        self.assertEqual(lr.main(["roll", "--path", p, "--force", "--no-compress"]), 0)
        self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
