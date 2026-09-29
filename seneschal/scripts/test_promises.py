#!/usr/bin/env python3
"""Tests for the promises ledger, phase 0 (`promises.py`, `seneschal/docs/promises-ledger-spec.md`).

Same two-family shape as `test_mouth.py` / `test_turns.py`, scaled down to what phase 0 actually
ships: **the record** (fail-open, never raises, the fold-on-id semantics `resolve` relies on) and
**the CLI** (record / list-open / resolve, and their `--json` shape). There is no producer-coverage
family here — phase 0 wires no send site, because nothing calls this module yet.

Run:  python -m unittest test_promises   (from seneschal/scripts)
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import promises  # noqa: E402

NOW = datetime(2026, 9, 5, 22, 0, 0, tzinfo=timezone.utc)


class RecordShape(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_writes_the_documented_record(self):
        row = promises.record_promise(self.dir, text="I'll open a PR for the flaky-test fix",
                                       speaker="daemon", now=NOW)
        self.assertIsNotNone(row)
        self.assertEqual(row["schema"], "seneschal.promise/1")
        self.assertEqual(row["promised_at"], "2026-09-05T22:00:00Z")
        self.assertEqual(row["text"], "I'll open a PR for the flaky-test fix")
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["due"])
        self.assertIsNone(row["job_id"])
        self.assertEqual(row["origin_hand"].get("speaker"), "daemon")

    def test_due_and_job_id_are_carried(self):
        row = promises.record_promise(
            self.dir, text="I'll bring you the outcome when it lands", job_id="20260905-220512-a3f1",
            due=datetime(2026, 9, 6, 0, 0, 0, tzinfo=timezone.utc), now=NOW)
        self.assertEqual(row["job_id"], "20260905-220512-a3f1")
        self.assertEqual(row["due"], "2026-09-06T00:00:00Z")

    def test_job_id_is_stringified(self):
        row = promises.record_promise(self.dir, text="job-backed", job_id=12345, now=NOW)
        self.assertEqual(row["job_id"], "12345")

    def test_reuses_mouth_origin_hand(self):
        """The spec names this explicitly: reuse mouth.origin_hand rather than invent a
        second notion of who is speaking. Patch it and confirm this module actually calls through."""
        with mock.patch.object(promises.mouth, "origin_hand",
                               return_value={"speaker": "patched"}) as m:
            row = promises.record_promise(self.dir, text="x", now=NOW)
        m.assert_called_once()
        self.assertEqual(row["origin_hand"], {"speaker": "patched"})

    def test_blank_text_writes_nothing(self):
        self.assertIsNone(promises.record_promise(self.dir, text="", now=NOW))
        self.assertIsNone(promises.record_promise(self.dir, text="   ", now=NOW))
        self.assertEqual(promises.list_all(self.dir), [])

    def test_non_string_text_writes_nothing(self):
        self.assertIsNone(promises.record_promise(self.dir, text=None, now=NOW))  # type: ignore[arg-type]

    def test_ids_are_unique_and_sortable(self):
        r1 = promises.record_promise(self.dir, text="a", now=NOW)
        r2 = promises.record_promise(self.dir, text="b", now=NOW)
        self.assertNotEqual(r1["id"], r2["id"])


class NeverRaises(unittest.TestCase):
    """The one invariant this module cannot break: recording never costs the promise it describes."""

    def test_record_survives_a_squatted_path(self):
        d = tempfile.mkdtemp()
        squat = os.path.join(d, promises.PROMISES_FILE)
        os.makedirs(squat)  # a directory where the file should be
        self.assertIsNone(promises.record_promise(d, text="x", now=NOW))

    def test_resolve_survives_a_squatted_path(self):
        d = tempfile.mkdtemp()
        squat = os.path.join(d, promises.PROMISES_FILE)
        os.makedirs(squat)
        self.assertFalse(promises.resolve(d, promise_id="whatever", status="kept"))

    def test_list_open_survives_an_unreadable_store(self):
        d = tempfile.mkdtemp()
        squat = os.path.join(d, promises.PROMISES_FILE)
        os.makedirs(squat)
        self.assertEqual(promises.list_open(d), [])

    def test_list_open_survives_a_malformed_line(self):
        d = tempfile.mkdtemp()
        path = promises.promises_path(d)
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json at all\n")
            fh.write(json.dumps({"id": "x", "status": "open", "text": "fine"}) + "\n")
        rows = promises.list_open(d)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "x")


class Resolution(unittest.TestCase):
    """`resolve` is an append, never a rewrite — the last row for an id wins, and that is what
    keeps a several-writer append-only log safe with no lock (mouth.py's outbound-queue pattern)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_resolved_promise_leaves_open_list(self):
        row = promises.record_promise(self.dir, text="I'll open the PR", now=NOW)
        self.assertEqual(len(promises.list_open(self.dir)), 1)
        self.assertTrue(promises.resolve(self.dir, promise_id=row["id"], status="kept",
                                          reason="the PR merged", now=NOW))
        self.assertEqual(promises.list_open(self.dir), [])
        all_rows = promises.list_all(self.dir)
        self.assertEqual(len(all_rows), 1)  # folded, not two rows
        self.assertEqual(all_rows[0]["status"], "kept")
        self.assertEqual(all_rows[0]["reason"], "the PR merged")
        # the original fields survive the fold — a resolution is additive, not a replacement
        self.assertEqual(all_rows[0]["text"], "I'll open the PR")

    def test_dropped_promise_is_not_open(self):
        row = promises.record_promise(self.dir, text="I'll circle back", now=NOW)
        promises.resolve(self.dir, promise_id=row["id"], status="dropped", now=NOW)
        self.assertEqual(promises.list_open(self.dir), [])

    def test_unrecognised_status_refuses_and_writes_nothing(self):
        row = promises.record_promise(self.dir, text="I'll check", now=NOW)
        before = promises.list_all(self.dir)
        self.assertFalse(promises.resolve(self.dir, promise_id=row["id"], status="maybe"))
        self.assertEqual(promises.list_all(self.dir), before)  # no row appended
        self.assertEqual(len(promises.list_open(self.dir)), 1)  # still open

    def test_blank_id_refuses(self):
        self.assertFalse(promises.resolve(self.dir, promise_id="", status="kept"))
        self.assertFalse(promises.resolve(self.dir, promise_id="   ", status="dropped"))

    def test_resolving_an_unknown_id_still_appends(self):
        """Not this module's job to know whether an id is real — a duplicate or stray resolution
        costs a redundant line, never a refusal (spec: fail-open on everything but the vocabulary)."""
        self.assertTrue(promises.resolve(self.dir, promise_id="never-recorded", status="kept"))
        rows = promises.list_all(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "kept")

    def test_list_open_is_oldest_first(self):
        r1 = promises.record_promise(self.dir, text="first", now=NOW)
        r2 = promises.record_promise(self.dir, text="second", now=NOW)
        ids = [r["id"] for r in promises.list_open(self.dir)]
        self.assertEqual(ids, [r1["id"], r2["id"]])


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _run(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = promises.main(["--state-dir", self.dir, *args])
        return code, buf.getvalue()

    def test_record_then_list_open_json(self):
        code, out = self._run("record", "--text", "I'll job that", "--speaker", "cli", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        pid = payload["row"]["id"]

        code, out = self._run("list-open", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], pid)

    def test_record_blank_text_exits_nonzero(self):
        code, out = self._run("record", "--text", "  ", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_resolve_roundtrip(self):
        _, out = self._run("record", "--text", "I'll fix it", "--json")
        pid = json.loads(out)["row"]["id"]

        code, out = self._run("resolve", "--id", pid, "--status", "kept", "--reason", "done",
                               "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])

        _, out = self._run("list-open", "--json")
        self.assertEqual(json.loads(out), [])

    def test_resolve_bad_status_exits_nonzero(self):
        # argparse itself refuses an out-of-choice status before main() ever sees it
        with self.assertRaises(SystemExit):
            self._run("resolve", "--id", "x", "--status", "maybe")

    def test_list_open_text_mode_is_readable(self):
        self._run("record", "--text", "I'll do the thing")
        code, out = self._run("list-open")
        self.assertEqual(code, 0)
        self.assertIn("I'll do the thing", out)


if __name__ == "__main__":
    unittest.main()
