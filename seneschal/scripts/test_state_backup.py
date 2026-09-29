#!/usr/bin/env python3
"""Tests for the nightly `state/` backup rotation.

**The load-bearing family is what it REFUSES to do**, not that it copies a file. A backup sweep is
trusted precisely to the extent that it cannot damage the thing it is backing up, cannot delete
something it did not make, and cannot quietly stop running — so those are the properties pinned here.

The byte-copy tests exist because the failure this whole change answers is an ENCODING failure. A
"backup" that decoded and re-encoded its input would be a second copy of that bug wearing a helpful
name, and `state/` can be mixed (a CRLF `carry-over.md` beside an LF `run-log.md`), so a normalising
copy would be wrong on one of them whichever way it normalised.

Stdlib unittest only.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import state_backup as sb  # noqa: E402

AT = datetime(2026, 8, 31, 22, 0)


def _write(path, blob):
    with open(path, "wb") as fh:
        fh.write(blob)


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


class Naming(unittest.TestCase):
    def test_it_matches_the_copies_ALREADY_on_disk(self):
        """Hand-made copies in this shape (`reminders.backup-20260830-2202.json`) may already be on
        disk. Byte-compatible naming is what makes them part of the rotation instead of orphans
        beside it."""
        self.assertEqual(sb.backup_name("reminders.json", datetime(2026, 8, 30, 22, 2)),
                         "reminders.backup-20260830-2202.json")
        self.assertEqual(sb.backup_name("carry-over.md", datetime(2026, 8, 1, 0, 13)),
                         "carry-over.backup-20260801-0013.md")


class RotateOne(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "carry-over.md")
        _write(self.src, b"line one\r\nline two\r\n")

    def _rotate(self, **kw):
        kw.setdefault("now", AT)
        return sb.rotate_one(self.d, "carry-over.md", **kw)

    def test_it_copies(self):
        row = self._rotate()
        self.assertEqual(row["action"], "copied")
        self.assertEqual(_read(os.path.join(self.d, row["dest"])), b"line one\r\nline two\r\n")

    def test_the_copy_is_BYTE_identical_including_CRLF(self):
        """`state/` is mixed. A copy that normalised would be wrong about one of the two memory
        files whichever direction it normalised in."""
        for blob in (b"a\r\nb\r\n", b"a\nb\n", "﻿a\r\n".encode("utf-8"), b"\x00\x01\xff"):
            with self.subTest(blob=blob):
                _write(self.src, blob)
                row = self._rotate()
                self.assertEqual(_read(os.path.join(self.d, row["dest"])), blob)

    def test_the_SOURCE_is_never_modified(self):
        before = _read(self.src)
        before_mtime = os.path.getmtime(self.src)
        self._rotate()
        self.assertEqual(_read(self.src), before)
        self.assertEqual(os.path.getmtime(self.src), before_mtime)

    def test_an_absent_source_is_reported_not_raised(self):
        row = sb.rotate_one(self.d, "not-here.json", now=AT)
        self.assertEqual(row["action"], "absent")
        self.assertIsNone(row["error"])

    def test_an_unchanged_file_is_not_copied_again(self):
        """A second identical copy costs a rotation slot and buys nothing — it would push a
        DIFFERENT day's content off the end of the window."""
        self._rotate()
        row = sb.rotate_one(self.d, "carry-over.md", now=AT + timedelta(days=1))
        self.assertEqual(row["action"], "unchanged")
        self.assertEqual(len(sb.existing_backups(self.d, "carry-over.md")), 1)

    def test_a_changed_file_IS_copied_again(self):
        self._rotate()
        _write(self.src, b"different\r\n")
        row = sb.rotate_one(self.d, "carry-over.md", now=AT + timedelta(days=1))
        self.assertEqual(row["action"], "copied")
        self.assertEqual(len(sb.existing_backups(self.d, "carry-over.md")), 2)

    def test_dry_run_touches_nothing(self):
        row = self._rotate(dry_run=True)
        self.assertEqual(row["action"], "would-copy")
        self.assertEqual(os.listdir(self.d), ["carry-over.md"])

    def test_a_copy_failure_is_reported_and_leaves_no_part_file(self):
        with mock.patch("shutil.copyfile", side_effect=OSError("disk full")):
            row = self._rotate()
        self.assertIn("disk full", row["error"])
        self.assertEqual([n for n in os.listdir(self.d) if n.endswith(".part")], [])
        self.assertEqual(_read(self.src), b"line one\r\nline two\r\n")


class Pruning(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "reminders.json")

    def _fill(self, days, keep=3):
        rows = []
        for i in range(days):
            _write(self.src, f'{{"n": {i}}}'.encode())
            rows.append(sb.rotate_one(self.d, "reminders.json",
                                      keep=keep, now=AT + timedelta(days=i)))
        return rows

    def test_it_keeps_exactly_keep_copies_and_drops_the_OLDEST(self):
        self._fill(6, keep=3)
        names = sb.existing_backups(self.d, "reminders.json")
        self.assertEqual(len(names), 3)
        self.assertEqual(names[0], "reminders.backup-20260905-2200.json")   # newest first
        self.assertEqual(names[-1], "reminders.backup-20260903-2200.json")

    def test_it_never_deletes_a_file_that_is_not_one_of_ITS_backups(self):
        """Hand-made rescue copies (`reminders.json.bak-quietstrip`, `*.bak`) live beside the real
        files. A prune that matched loosely would eat one."""
        decoys = ["reminders.json.bak-quietstrip", "reminders.example.json",
                  "reminders.backup-notadate.json", "reminders-id-cache.md", "reminders.json"]
        for name in decoys:
            _write(os.path.join(self.d, name), b"precious")
        self._fill(6, keep=1)
        for name in decoys:
            self.assertTrue(os.path.exists(os.path.join(self.d, name)), name)

    def test_it_never_backs_up_a_backup(self):
        self._fill(3, keep=9)
        rows = sb.rotate(self.d, keep=9, now=AT + timedelta(days=9))
        self.assertEqual([r["file"] for r in rows if r["action"] == "copied"], [])
        stray = [n for n in os.listdir(self.d) if n.count(".backup-") > 1]
        self.assertEqual(stray, [])

    def test_keep_is_floored_at_one_so_a_zero_cannot_wipe_the_rotation(self):
        self._fill(3, keep=0)
        self.assertEqual(len(sb.existing_backups(self.d, "reminders.json")), 1)

    def test_ordering_is_by_the_NAME_not_the_mtime(self):
        """A `git checkout`, a restore or a file-manager touch must not reorder the rotation: a copy
        is what its filename says it is."""
        for stamp in ("20260801-0013", "20260830-2202", "20260829-2200"):
            p = os.path.join(self.d, f"reminders.backup-{stamp}.json")
            _write(p, b"x")
            os.utime(p, (1, 1))                     # identical mtimes on purpose
        self.assertEqual(sb.existing_backups(self.d, "reminders.json"),
                         ["reminders.backup-20260830-2202.json",
                          "reminders.backup-20260829-2200.json",
                          "reminders.backup-20260801-0013.json"])


class TheSweepIsResilient(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_one_bad_file_does_not_stop_the_others(self):
        """A sweep that aborts on the first unreadable file protects the alphabetically-early half of
        a list and silently abandons the rest."""
        for name in ("carry-over.md", "run-log.md", "acks.json"):
            _write(os.path.join(self.d, name), b"content\r\n")
        real = sb.shutil.copyfile

        def flaky(src, dst, *a, **k):
            if os.path.basename(src) == "run-log.md":
                raise OSError("locked by another process")
            return real(src, dst, *a, **k)

        with mock.patch("shutil.copyfile", flaky):
            rows = sb.rotate(self.d, now=AT)
        by = {r["file"]: r for r in rows}
        self.assertEqual(by["run-log.md"]["action"], None)
        self.assertIn("locked", by["run-log.md"]["error"])
        self.assertEqual(by["carry-over.md"]["action"], "copied")
        self.assertEqual(by["acks.json"]["action"], "copied")

    def test_the_summary_names_errors(self):
        rows = [{"file": "a", "action": "copied", "kept": 1, "pruned": [], "error": None},
                {"file": "b", "action": None, "kept": 0, "pruned": [], "error": "boom"}]
        self.assertIn("ERROR", sb.summary_line(rows))

    def test_main_exits_zero_even_when_a_file_failed(self):
        """A backup failure may never be what aborts Dream's remaining steps."""
        _write(os.path.join(self.d, "carry-over.md"), b"x")
        with mock.patch("shutil.copyfile", side_effect=OSError("nope")):
            with mock.patch.object(sb, "_stamp_dream_step"):
                rc = sb.main(["--state-dir", self.d, "--json"])
        self.assertEqual(rc, 0)


class TheRotationSet(unittest.TestCase):
    def test_no_append_only_ledger_is_in_it(self):
        """Half (b) of the selection rule. A truncating rewrite cannot happen to a file opened `"a"`,
        so copying megabytes of append-only history nightly would buy nothing and make the sweep too expensive
        to keep. If one of these is ever added, the rule changed and the docstring must say why."""
        for name in ("turns.jsonl", "metrics.jsonl", "assertions.jsonl", "governor-ledger.jsonl",
                     "router-log.jsonl", "warm-transcript.jsonl", "seed-log.jsonl"):
            self.assertNotIn(name, sb.FILES)

    def test_the_wholesale_rewritten_memory_files_are_in_it(self):
        self.assertIn("carry-over.md", sb.FILES)
        self.assertIn("reminders.json", sb.FILES)

    def test_nothing_enormous_is_in_it(self):
        """`health.db` and `rag-index.sqlite` can run to hundreds of MB — a nightly copy of either is
        gigabytes a week, which is how a rotation gets turned off."""
        for name in ("health.db", "presence.db", "rag-index.sqlite", "health-feed.ndjson"):
            self.assertNotIn(name, sb.FILES)

    def test_every_entry_is_a_plain_filename(self):
        """A path separator here would put copies somewhere the prune's `listdir` cannot see them."""
        for name in sb.FILES:
            self.assertEqual(os.path.basename(name), name)


class DreamStamping(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        _write(os.path.join(self.d, "carry-over.md"), b"x")

    def test_a_real_run_stamps_step_2g(self):
        """A sweep nobody can tell has stopped running is the mechanism it replaced."""
        with mock.patch.object(sb, "_stamp_dream_step") as stamp:
            sb.main(["--state-dir", self.d])
        stamp.assert_called_once_with(self.d)

    def test_a_dry_run_stamps_NOTHING(self):
        with mock.patch.object(sb, "_stamp_dream_step") as stamp:
            sb.main(["--state-dir", self.d, "--dry-run"])
        stamp.assert_not_called()

    def test_it_stamps_even_when_a_file_errored(self):
        """The step DID run. Conflating "one file was locked" with "the sweep never happened" lets a
        single locked file mark the whole night as unattempted."""
        with mock.patch("shutil.copyfile", side_effect=OSError("locked")):
            with mock.patch.object(sb, "_stamp_dream_step") as stamp:
                sb.main(["--state-dir", self.d])
        stamp.assert_called_once()

    def test_a_failing_stamp_never_costs_the_backup(self):
        with mock.patch("dream_steps.record", side_effect=RuntimeError("boom")):
            rc = sb.main(["--state-dir", self.d])
        self.assertEqual(rc, 0)
        self.assertEqual(len(sb.existing_backups(self.d, "carry-over.md")), 1)

    def test_dream_steps_declares_2g_with_an_owner(self):
        """`dream_steps.py`'s own rule: a step nothing stamps can never be cleared, so declaring the
        owner and adding the `record` call are ONE change — 2e shipped without it and read `never`
        while running correctly."""
        import dream_steps
        self.assertEqual(dream_steps.STEPS["2g"]["owner"], "state_backup.py")


if __name__ == "__main__":
    unittest.main()
