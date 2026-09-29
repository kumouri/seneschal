#!/usr/bin/env python3
"""Tests for the durable per-poll-cycle trace (seneschal/scripts/telegram_poll.py `record_poll_trace`).

The bug this exists to make diagnosable: a plain-text message Telegram delivered (the sender's client
showed it sent) can fail to reach ANY of the durable traces the daemon writes on enqueue — lost
between Telegram and this module handing the batch back. Every trace that existed was conditioned on
a message actually arriving, so there was no way to say whether the daemon was even polling during
the gap. What's covered:
  * a cycle that returns updates writes one line naming offset_in/offset_out/update_count/update_ids;
  * the boring "ok, nothing new" cycle still writes a line — the whole point;
  * a `get_updates` exception still writes a line (ok:false, offset unchanged, an error);
  * the trace NEVER carries message text, only update_ids and counts;
  * the trace path defaults to living beside --offset-file, so a test/tool pointing --offset-file at
    a tempdir never touches the real state/ directory;
  * a broken trace write (append_text raising) never fails the poll cycle itself (fail-open);
  * the file rotates once it crosses the size trigger (log_rotation.roll_closed wired in).

No network, ever: `get_updates` is mocked at every call site, matching every other test in this file's
sibling suite (test_telegram_poll.py, test_telegram_edits.py).

Run:  python -m unittest seneschal.scripts.test_telegram_poll_trace   (or)   python test_telegram_poll_trace.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import telegram_poll as tp  # noqa: E402


def _msg(text="hi", message_id=1, chat_id=555, **over):
    base = {"message_id": message_id, "chat": {"id": chat_id, "type": "private"},
            "from": {"username": "owner"}, "date": 1_000_000, "text": text}
    base.update(over)
    return base


class RecordPollTrace(unittest.TestCase):
    """Direct unit tests of the writer, no subprocess/CLI involved."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.trace = os.path.join(self.dir, "telegram-poll-trace.jsonl")

    def _lines(self):
        with open(self.trace, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_successful_cycle_with_updates(self):
        updates = [{"update_id": 41, "message": _msg()}, {"update_id": 42, "message": _msg()}]
        tp.record_poll_trace(self.trace, offset_in=40, offset_out=43, updates=updates,
                             ok=True, committed=True)
        rows = self._lines()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["ok"])
        self.assertEqual(row["offset_in"], 40)
        self.assertEqual(row["offset_out"], 43)
        self.assertEqual(row["update_count"], 2)
        self.assertEqual(row["update_ids"], [41, 42])
        self.assertTrue(row["committed"])
        self.assertIn("ts", row)
        self.assertNotIn("error", row)

    def test_empty_batch_still_writes_a_line(self):
        """The whole point: a cycle that heard nothing back must not be silent."""
        tp.record_poll_trace(self.trace, offset_in=40, offset_out=40, updates=[],
                             ok=True, committed=False)
        rows = self._lines()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["update_count"], 0)
        self.assertEqual(rows[0]["update_ids"], [])

    def test_failed_cycle_records_the_error_with_offset_unchanged(self):
        tp.record_poll_trace(self.trace, offset_in=40, offset_out=40, updates=[],
                             ok=False, error="Telegram getUpdates failed: timeout")
        rows = self._lines()
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(rows[0]["offset_in"], rows[0]["offset_out"])
        self.assertIn("timeout", rows[0]["error"])

    def test_never_carries_message_text(self):
        updates = [{"update_id": 1, "message": _msg(text="the owner's actual private words")}]
        tp.record_poll_trace(self.trace, offset_in=0, offset_out=2, updates=updates, ok=True)
        with open(self.trace, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("the owner's actual private words", raw)

    def test_one_line_per_call_appends_not_overwrites(self):
        for i in range(3):
            tp.record_poll_trace(self.trace, offset_in=i, offset_out=i + 1, updates=[], ok=True)
        self.assertEqual(len(self._lines()), 3)

    def test_a_write_failure_never_raises(self):
        """Fail-open: this must never be able to cost the poll cycle it is describing."""
        with mock.patch.object(tp.mw, "append_text", side_effect=OSError("disk full")):
            try:
                tp.record_poll_trace(self.trace, offset_in=1, offset_out=2, updates=[], ok=True)
            except Exception as e:  # noqa: BLE001
                self.fail(f"record_poll_trace raised: {e!r}")

    def test_checks_rotation_before_every_append(self):
        """Bounded growth is delegated to log_rotation's own door — same size trigger + generation
        cap as every other rotating state/ log (its own suite, test_log_rotation.py, covers the
        actual roll/compress/prune mechanics; this just confirms the wiring)."""
        with mock.patch.object(tp.lr, "roll_closed") as roll:
            tp.record_poll_trace(self.trace, offset_in=1, offset_out=2, updates=[], ok=True)
        roll.assert_called_once_with(self.trace, max_bytes=tp.lr.DEFAULT_MAX_BYTES,
                                     keep=tp.lr.DEFAULT_KEEP)

    def test_rotates_once_over_an_explicit_trigger(self):
        """`max_bytes`/`keep` pass straight through to log_rotation, so a cheap explicit trigger
        exercises the real roll — no need to write 25 MiB to hit the module default."""
        with open(self.trace, "wb") as fh:
            fh.write(b"x" * 200)
        tp.record_poll_trace(self.trace, offset_in=1, offset_out=2, updates=[], ok=True,
                             max_bytes=100, keep=2)
        # The oversized pre-existing content was rolled aside; the live file holds only this call's line.
        rows = self._lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["offset_in"], 1)
        rolled = [f for f in os.listdir(self.dir) if f != "telegram-poll-trace.jsonl"]
        self.assertTrue(rolled, "expected a rolled generation beside the live trace file")


class PollTraceViaMain(unittest.TestCase):
    """`main()` end to end with the wire mocked out — the shape the daemon actually calls."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.offset = os.path.join(self.dir, "telegram-offset")
        self.cache = os.path.join(self.dir, "custom-emoji-cache.json")
        self.default_trace = os.path.join(self.dir, "telegram-poll-trace.jsonl")

    def _run(self, updates, *extra_argv):
        argv = ["telegram_poll.py", "--offset-file", self.offset,
                "--custom-emoji-cache", self.cache, "--commit", *extra_argv]
        env = {"TELEGRAM_BOT_TOKEN": "not-a-real-token"}
        out = io.StringIO()
        with mock.patch.object(tp, "get_updates", return_value=updates), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out):
            rc = tp.main()
        return rc, json.loads(out.getvalue().strip().splitlines()[-1])

    def _trace_lines(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_default_trace_path_lives_beside_the_offset_file(self):
        """No --poll-trace-file passed — this must NOT touch the real repo state/ directory, only the
        tempdir --offset-file already points at (the same hazard test-defaulting-into-live-state
        warns about elsewhere in this tree)."""
        rc, res = self._run([{"update_id": 1, "message": _msg()}])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.default_trace))
        rows = self._trace_lines(self.default_trace)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["update_ids"], [1])
        self.assertEqual(rows[0]["offset_out"], res["next_offset"])

    def test_empty_poll_cycle_through_main_still_traces(self):
        rc, res = self._run([])
        self.assertEqual(rc, 0)
        self.assertEqual(res["count"], 0)
        rows = self._trace_lines(self.default_trace)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["update_count"], 0)

    def test_get_updates_failure_through_main_still_traces(self):
        argv = ["telegram_poll.py", "--offset-file", self.offset, "--custom-emoji-cache", self.cache]
        env = {"TELEGRAM_BOT_TOKEN": "not-a-real-token"}
        out = io.StringIO()
        with mock.patch.object(tp, "get_updates", side_effect=RuntimeError("Telegram getUpdates failed: timeout")), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out):
            rc = tp.main()
        self.assertEqual(rc, 1)
        rows = self._trace_lines(self.default_trace)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])
        self.assertIn("timeout", rows[0]["error"])

    def test_explicit_poll_trace_file_overrides_the_default(self):
        custom = os.path.join(self.dir, "elsewhere.jsonl")
        rc, _ = self._run([], "--poll-trace-file", custom)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(custom))
        self.assertFalse(os.path.exists(self.default_trace))

    def test_gc_mode_and_refetch_write_no_trace(self):
        """Neither mode calls getUpdates, so neither is a poll cycle."""
        argv = ["telegram_poll.py", "--offset-file", self.offset, "--prune-days", "30",
               "--download-dir", os.path.join(self.dir, "inbox")]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            rc = tp.main()
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.default_trace))


if __name__ == "__main__":
    unittest.main()
