#!/usr/bin/env python3
"""Tests for the atomic memory-file writer (the truncating-write loss and the lost-append race).

**The load-bearing family is the failure cases, not the happy path.** Writing a file correctly is not
the thing that breaks; *failing* to write one is. So the tests that matter simulate the total
loss — a write that raises partway — and assert the OLD content is still there. A helper that writes
correctly and still truncates on error would pass a happy-path suite and lose `carry-over.md` again.

Stdlib unittest only. Run:  python -m unittest seneschal.scripts.test_memory_write
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import memory_write as mw  # noqa: E402


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _write_bytes(path, blob):
    with open(path, "wb") as fh:
        fh.write(blob)


class WriteIsAtomic(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "carry-over.md")

    def test_writes_and_replaces(self):
        mw.write_text(self.p, "first\n")
        mw.write_text(self.p, "second\n")
        with open(self.p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "second\n")

    def test_creates_missing_parents(self):
        deep = os.path.join(self.d, "a", "b", "notes.md")
        mw.write_text(deep, "x")
        self.assertTrue(os.path.isfile(deep))

    def test_a_failing_write_leaves_the_OLD_content_intact(self):
        """**The truncating-write loss, reproduced.** `open(path, "w")` truncates first, so a
        UnicodeEncodeError mid-write leaves an empty file and there was no git copy to restore from.
        Here the exception happens while the temp file is being written; the real file must be
        untouched, not empty."""
        mw.write_text(self.p, "PRECIOUS\n")
        real_open = open

        def exploding_open(path, *a, **k):
            fh = real_open(path, *a, **k)
            if str(path).endswith(".tmp"):
                orig_write = fh.write

                def boom(_s):
                    raise UnicodeEncodeError("utf-8", "x", 0, 1, "simulated encode failure")
                fh.write = boom
                del orig_write
            return fh

        with mock.patch("builtins.open", exploding_open):
            with self.assertRaises(UnicodeEncodeError):
                mw.write_text(self.p, "🌱 emoji that used to kill it\n")

        with real_open(self.p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "PRECIOUS\n",
                             "the old content must survive — an empty file is the unrecoverable case")

    def test_non_ascii_survives_a_round_trip(self):
        # The encoding is explicit precisely because Windows's default is not UTF-8.
        text = "🌱 carry-over — the owner's notes · θ_protect\n"
        mw.write_text(self.p, text)
        with open(self.p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), text)

    def test_no_temp_file_is_left_behind(self):
        mw.write_text(self.p, "x")
        self.assertEqual([f for f in os.listdir(self.d) if f.endswith(".tmp")], [])

    def test_a_persistently_locked_target_raises_rather_than_silently_not_saving(self):
        """`save_json`'s rule, restated: a persistent failure is a real one (disk full, a genuinely
        stuck handle) and must not be swallowed into a file that quietly didn't save."""
        with mock.patch("os.replace", side_effect=PermissionError("held open")):
            with self.assertRaises(PermissionError):
                mw.write_text(self.p, "x")
        self.assertEqual([f for f in os.listdir(self.d) if f.endswith(".tmp")], [],
                         "a give-up must still clean up after itself")


class AppendIsConcurrencySafe(unittest.TestCase):
    """A run-log mirror entry vanishes when the write is a whole-file read-modify-write and a
    concurrent session rewrites the file in between."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "run-log.md")

    def test_concurrent_appends_all_survive(self):
        # The old shape (read, concat, write) loses entries here. O_APPEND cannot.
        n = 24

        def worker(i):
            mw.append_text(self.p, f"entry-{i:02d}\n")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(self.p, encoding="utf-8") as fh:
            lines = [l for l in fh.read().split("\n") if l]
        self.assertEqual(len(lines), n, "an append was lost — this is the exact 07-19 defect")
        self.assertEqual(len(set(lines)), n)

    def test_append_creates_the_file(self):
        mw.append_text(self.p, "first\n")
        self.assertTrue(os.path.isfile(self.p))


class PrependIsGuarded(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "run-log.md")

    def test_newest_goes_on_top(self):
        mw.prepend_text(self.p, "older\n")
        mw.prepend_text(self.p, "newer\n")
        with open(self.p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "newer\nolder\n")

    def test_concurrent_prepends_all_survive(self):
        n = 12

        def worker(i):
            mw.prepend_text(self.p, f"entry-{i:02d}\n")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(self.p, encoding="utf-8") as fh:
            lines = [l for l in fh.read().split("\n") if l]
        self.assertEqual(len(lines), n, "a prepend was lost — the lock did not hold")

    def test_the_lock_is_released_even_when_the_write_explodes(self):
        with mock.patch.object(mw, "write_text", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                mw.prepend_text(self.p, "x")
        self.assertFalse(os.path.exists(self.p + ".lock"), "a stuck lock would wedge every later run")

    def test_a_stale_lock_is_broken_rather_than_deadlocking(self):
        # A crashed holder must not wedge the daemon forever. Losing a race is recoverable; hanging
        # is not — so the lock has a timeout and takes it.
        open(self.p + ".lock", "w").close()
        with mock.patch.object(mw, "_LOCK_TIMEOUT_SEC", 0.05):
            mw.prepend_text(self.p, "went through\n")
        with open(self.p, encoding="utf-8") as fh:
            self.assertIn("went through", fh.read())


class LineEndingsArePreserved(unittest.TestCase):
    """**`state/` can be MIXED**: one memory file all CRLF, its neighbour all LF, both written
    through these same functions.

    A `prepend_text` that read universal-newline (`\r\n` -> `\n`) and wrote with `newline=""` would
    rewrite every ending of a CRLF `carry-over.md` on one prepend. Nothing lost, and every
    subsequent diff of that file 100% noise. A durability helper that makes the
    diff unreadable does not get used, so this is guarded rather than trusted."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "carry-over.md")

    def _endings(self):
        blob = _read_bytes(self.p)
        crlf = blob.count(b"\r\n")
        return crlf, blob.count(b"\n") - crlf

    def test_detect_newline_reads_bytes_not_text(self):
        _write_bytes(self.p, b"a\r\nb\r\n")
        self.assertEqual(mw.detect_newline(self.p), "\r\n")
        _write_bytes(self.p, b"a\nb\n")
        self.assertEqual(mw.detect_newline(self.p), "\n")

    def test_detect_newline_does_not_guess_when_there_is_nothing_to_go_on(self):
        """A missing file, an empty one, and a single unterminated line all mean "no evidence".
        Returning a default here is how a directory becomes mixed."""
        self.assertIsNone(mw.detect_newline(self.p))
        _write_bytes(self.p, b"")
        self.assertIsNone(mw.detect_newline(self.p))
        _write_bytes(self.p, b"no ending at all")
        self.assertIsNone(mw.detect_newline(self.p))

    def test_write_text_keeps_a_CRLF_file_CRLF(self):
        _write_bytes(self.p, b"old\r\nlines\r\n")
        mw.write_text(self.p, "new\nlines\nhere\n")     # caller supplies LF, as callers do
        self.assertEqual(self._endings(), (3, 0))

    def test_write_text_keeps_an_LF_file_LF(self):
        _write_bytes(self.p, b"old\nlines\n")
        mw.write_text(self.p, "new\r\nlines\r\n")
        self.assertEqual(self._endings(), (0, 2))

    def test_a_new_file_is_written_verbatim_and_not_guessed_at(self):
        mw.write_text(self.p, "a\nb\n")
        self.assertEqual(self._endings(), (0, 2))
        other = os.path.join(self.d, "other.md")
        mw.write_text(other, "a\r\nb\r\n")
        blob = _read_bytes(other)
        self.assertEqual(blob.count(b"\r\n"), 2)

    def test_prepend_round_trips_a_CRLF_file(self):
        """The motivating shape: prepend one entry to a CRLF `carry-over.md`."""
        _write_bytes(self.p, b"## old\r\nbody\r\n")
        mw.prepend_text(self.p, "## new\n\n")
        crlf, lf = self._endings()
        self.assertEqual(lf, 0, "a prepend converted the file to LF")
        self.assertEqual(crlf, 4)
        self.assertTrue(_read_bytes(self.p).decode("utf-8").startswith("## new"))

    def test_append_does_not_MIX_endings(self):
        """Appending is the one operation that can leave a file half-CRLF, which is worse than
        converting it: half the diff is noise and the other half is real."""
        _write_bytes(self.p, b"a\r\nb\r\n")
        mw.append_text(self.p, "c\n")
        self.assertEqual(self._endings(), (3, 0))

    def test_an_explicit_newline_argument_wins(self):
        _write_bytes(self.p, b"a\r\n")
        mw.write_text(self.p, "x\ny\n", newline="\n")
        self.assertEqual(self._endings(), (0, 2))

    def test_newline_None_translates_nothing(self):
        _write_bytes(self.p, b"a\r\n")
        mw.write_text(self.p, "x\ny\r\n", newline=None)
        self.assertEqual(self._endings(), (1, 1))

    def test_a_BOM_survives_a_write_and_is_not_doubled_by_an_append(self):
        """Dropping the BOM changes how another reader decodes the file; writing a second one plants
        a `\ufeff` in the middle of it. Both are silent."""
        _write_bytes(self.p, "\ufeffhello\r\n".encode("utf-8"))
        mw.write_text(self.p, "goodbye\n")
        blob = _read_bytes(self.p)
        self.assertTrue(blob.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(blob.count(b"\xef\xbb\xbf"), 1)
        mw.append_text(self.p, "more\n")
        self.assertEqual(_read_bytes(self.p).count(b"\xef\xbb\xbf"), 1)

    def test_append_to_a_NEW_file_does_not_invent_a_BOM(self):
        """`utf-8-sig` would. Measured: the sig codec does not plant a second BOM mid-file, but it
        DOES give an empty or brand-new file one — so an append that happens to be the first write
        would pick an encoding marker for a file nobody chose one for. `write_text` preserves a BOM
        that exists; nothing here may create one."""
        fresh = os.path.join(self.d, "run-log.md")
        mw.append_text(fresh, "first entry\n")
        self.assertEqual(_read_bytes(fresh), b"first entry\n")

    def test_prepend_does_not_duplicate_a_BOM(self):
        _write_bytes(self.p, "\ufeffold\r\n".encode("utf-8"))
        mw.prepend_text(self.p, "new\r\n")
        blob = _read_bytes(self.p)
        self.assertEqual(blob.count(b"\xef\xbb\xbf"), 1)
        self.assertTrue(blob.startswith(b"\xef\xbb\xbf" + "new".encode("utf-8")))


class SurrogatesFailWithoutDestroyingAnything(unittest.TestCase):
    """A `\U0001F9F7` pasted into a source literal as its UTF-16 surrogate pair. Through a bare
    `io.open(p, "w")` the file would already be truncated when the encode raises."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "carry-over.md")
        _write_bytes(self.p, b"774 lines of real content\r\n")

    def test_the_target_survives_and_the_error_says_so(self):
        lone = "pin: \ud83e\uddf7\n"                  # the surrogate pair, unpaired-as-text
        with self.assertRaises(UnicodeEncodeError) as caught:
            mw.write_text(self.p, lone)
        self.assertEqual(_read_bytes(self.p), b"774 lines of real content\r\n")
        self.assertIn("NOT modified", str(caught.exception))

    def test_no_temp_file_is_left_behind_after_a_surrogate_failure(self):
        with self.assertRaises(UnicodeEncodeError):
            mw.write_text(self.p, "\ud83e\uddf7")
        leftovers = [n for n in os.listdir(self.d) if n.startswith(".")]
        self.assertEqual(leftovers, [])


class ZeroBytesOverContentIsRefused(unittest.TestCase):
    """**The loss the atomic write cannot prevent.** A full `state/context-digest.md` replaced with
    0 bytes — not through `open(p, "w")`, but through this module, correctly, handed an empty payload
    because the staging step that was supposed to produce the content had already failed while the
    shell ran the next statement anyway.

    **Atomicity answers a PARTIAL write. It has nothing to say about a complete write of nothing** —
    and from the outside the two are indistinguishable: exit 0, no leftover temp file, no error.

    **Zero bytes ONLY, and these tests must not grow a shrink heuristic.** All four losses on the
    record ended at exactly 0 bytes, so the narrow guard covers every case there is; a size-ratio rule
    would fire on a legitimate prune (`reminders.json`, 150 → 70 rows in one Dream step) and a check
    that calls the correct operation a bug is a check somebody switches off."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "context-digest.md")

    def test_zero_over_a_non_empty_file_refuses_and_changes_nothing(self):
        blob = b"## \xf0\x9f\x92\x8a READ FIRST\r\n" + b"digest line\r\n" * 40
        _write_bytes(self.p, blob)
        with self.assertRaises(mw.EmptyWriteRefused):
            mw.write_text(self.p, "")
        self.assertEqual(_read_bytes(self.p), blob,
                         "the guard must leave the file BYTE-identical, not merely non-empty")

    def test_the_refusal_names_the_size_and_the_way_out(self):
        """The question in the first minute is *did the file survive, and how do I proceed*. A
        message that answers neither is a stack trace with extra steps."""
        _write_bytes(self.p, b"x" * 19336)
        with self.assertRaises(mw.EmptyWriteRefused) as caught:
            mw.write_text(self.p, "")
        msg = str(caught.exception)
        self.assertIn("19,336 bytes", msg)
        self.assertIn(self.p, msg)
        self.assertIn("NOT modified", msg)
        self.assertIn("--allow-empty", msg)

    def test_a_refusal_leaves_no_temp_file(self):
        """A stray `.context-digest.md.tmp` would read as an *interrupted atomic write*, which is a
        different incident with a different diagnosis. The guard fires before anything is opened."""
        _write_bytes(self.p, b"content")
        with self.assertRaises(mw.EmptyWriteRefused):
            mw.write_text(self.p, "")
        self.assertEqual([n for n in os.listdir(self.d) if n.startswith(".")], [])

    def test_zero_over_an_ALREADY_EMPTY_file_is_allowed(self):
        # Nothing to lose. Refusing here would make the guard fire on the second run of a legitimate
        # drain, which is how a correct guard earns a reputation for false positives.
        _write_bytes(self.p, b"")
        mw.write_text(self.p, "")
        self.assertEqual(_read_bytes(self.p), b"")

    def test_zero_to_a_file_that_does_not_exist_is_allowed(self):
        # Creating an empty file is not destruction, and a first-ever write must not need the flag.
        fresh = os.path.join(self.d, "brand-new.md")
        mw.write_text(fresh, "")
        self.assertTrue(os.path.isfile(fresh))
        self.assertEqual(_read_bytes(fresh), b"")

    def test_allow_empty_permits_a_deliberate_drain(self):
        """A queue drain is the real caller: every queued item reaching its destination
        empties the queue, and that is the SUCCESS case, not a failed producer."""
        _write_bytes(self.p, b"one pending line\n")
        mw.write_text(self.p, "", allow_empty=True)
        self.assertEqual(_read_bytes(self.p), b"")

    def test_a_normal_write_over_content_is_untouched_by_the_guard(self):
        _write_bytes(self.p, b"old\n")
        mw.write_text(self.p, "new\n")
        self.assertEqual(_read_bytes(self.p), b"new\n")

    def test_a_whitespace_payload_is_NOT_empty(self):
        """Zero bytes is the whole rule. A bare newline is content, and widening this to `.strip()`
        starts exactly the judgment calls the narrow guard exists to avoid."""
        _write_bytes(self.p, b"old\n")
        mw.write_text(self.p, "\n")
        self.assertEqual(_read_bytes(self.p), b"\n")

    def test_the_atomic_replace_still_holds_under_the_guard(self):
        """The guard is ADDITIVE. A non-empty write that explodes must still leave the old content —
        the truncating-write property, and the reason this module exists at all."""
        _write_bytes(self.p, b"PRECIOUS\r\n")
        with mock.patch("os.replace", side_effect=PermissionError("held open")):
            with self.assertRaises(PermissionError):
                mw.write_text(self.p, "replacement\n")
        self.assertEqual(_read_bytes(self.p), b"PRECIOUS\r\n")
        self.assertEqual([n for n in os.listdir(self.d) if n.endswith(".tmp")], [])


class AppendAndPrependAreDeliberatelyNotGuarded(unittest.TestCase):
    """**The ruling, tested rather than left in a comment.** `write` is guarded because it REPLACES.
    `append` and `prepend` are additive, so an empty payload is a no-op by construction: an append
    writes no bytes to a file it never truncates, and a prepend writes `"" + old`, which *is* `old`.
    Guarding them would refuse an operation that cannot lose anything.

    The prepend case is the load-bearing one — it goes THROUGH `write_text`, so the guard must not
    fire on it."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "run-log.md")

    def test_an_empty_append_is_a_harmless_no_op(self):
        _write_bytes(self.p, b"## 2026-09-01\nreal content\n")
        before = _read_bytes(self.p)
        mw.append_text(self.p, "")
        self.assertEqual(_read_bytes(self.p), before)

    def test_an_empty_prepend_writes_the_old_content_back_rather_than_tripping_the_guard(self):
        _write_bytes(self.p, b"## 2026-09-01\r\nreal content\r\n")
        before = _read_bytes(self.p)
        mw.prepend_text(self.p, "")
        self.assertEqual(_read_bytes(self.p), before)

    def test_the_library_functions_stay_SILENT_on_an_empty_payload(self):
        """The warning belongs to the CLI. These two are called from the fail-open `state/` writers —
        the reminder path, the transcript mirror — where stderr noise costs more than it buys."""
        _write_bytes(self.p, b"kept\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mw.append_text(self.p, "")
            mw.prepend_text(self.p, "")
        self.assertEqual(err.getvalue(), "")


class TheIncidentThroughTheCLI(unittest.TestCase):
    """**The empty-payload loss, reproduced end to end: content piped from a producer that had
    already failed.** The staging step raises, the shell moves to the next statement anyway, and
    `memory_write.py write <path>` reads an empty stdin. Unguarded, it writes 0 bytes over the whole
    file — atomically, faithfully — and **exits 0**. This is that command, and the exit code is the assertion."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "context-digest.md")

    def _run(self, argv, stdin_text):
        err = io.StringIO()
        stub = type("S", (), {"read": staticmethod(lambda: stdin_text)})()
        with mock.patch("sys.stdin", new=stub), contextlib.redirect_stderr(err):
            return mw._main(argv), err.getvalue()

    def test_empty_stdin_from_a_failed_producer_is_refused_and_the_digest_survives(self):
        blob = b"## READ FIRST\r\n" + b"digest line\r\n" * 500
        _write_bytes(self.p, blob)
        code, err = self._run(["write", self.p], "")   # the producer failed; nothing on stdin
        self.assertEqual(code, 2, "it exited 0 on the night — that is the whole bug")
        self.assertEqual(_read_bytes(self.p), blob)
        self.assertIn("REFUSED", err)
        self.assertIn(f"{len(blob):,} bytes", err)
        self.assertIn("--allow-empty", err)

    def test_allow_empty_gets_a_deliberate_drain_through_the_CLI(self):
        _write_bytes(self.p, b"drain me\n")
        code, _ = self._run(["write", self.p, "--allow-empty"], "")
        self.assertEqual(code, 0)
        self.assertEqual(_read_bytes(self.p), b"")

    def test_a_normal_CLI_write_is_unaffected_and_silent(self):
        _write_bytes(self.p, b"old\n")
        code, err = self._run(["write", self.p], "new\n")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(_read_bytes(self.p), b"new\n")

    def test_an_empty_append_warns_and_still_succeeds(self):
        _write_bytes(self.p, b"kept\n")
        code, err = self._run(["append", self.p], "")
        self.assertEqual(code, 0, "an empty append loses nothing — refusing it would be noise")
        self.assertEqual(_read_bytes(self.p), b"kept\n")
        self.assertIn("empty stdin", err)

    def test_an_empty_prepend_warns_and_still_succeeds(self):
        _write_bytes(self.p, b"kept\r\n")
        code, err = self._run(["prepend", self.p], "")
        self.assertEqual(code, 0)
        self.assertEqual(_read_bytes(self.p), b"kept\r\n")
        self.assertIn("empty stdin", err)


class CLI(unittest.TestCase):
    """The CLI is the point of the module: the file that got emptied was written by prompt-side code,
    so the skills need something to CALL rather than a rule to remember."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "carry-over.md")

    def _run(self, mode, text):
        with mock.patch("sys.stdin", new=type("S", (), {"read": staticmethod(lambda: text)})()):
            return mw._main([mode, self.p])

    def test_write_append_prepend(self):
        self.assertEqual(self._run("write", "base\n"), 0)
        self._run("append", "after\n")
        self._run("prepend", "before\n")
        with open(self.p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "before\nbase\nafter\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
