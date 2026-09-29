#!/usr/bin/env python3
"""Tests for `pending_approvals.py` — the one draft-and-hold ledger.

Covers the three things the contract depends on: the schema stamp lands on every save regardless
of what was read, a legacy un-versioned file is tolerated on read rather than rejected, and
add/resolve round-trip without ever colliding ids or minting a phantom row. The corruption test is
the load-bearing one — a store that fails to parse must raise, never quietly read as empty, because
"empty" plus one new entry is a real, destructive overwrite of a file that might still hold the
owner's actual pending sends.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import pending_approvals  # noqa: E402


class SchemaStamp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_fresh_store_has_no_entries_and_no_file_yet(self):
        doc = pending_approvals.load(state_dir=self.dir)
        self.assertEqual(doc, {"schema": pending_approvals.SCHEMA, "approvals": []})
        self.assertFalse(os.path.exists(os.path.join(self.dir, pending_approvals.FILENAME)))

    def test_save_always_stamps_the_current_schema(self):
        pending_approvals.save({"approvals": []}, state_dir=self.dir)
        path = os.path.join(self.dir, pending_approvals.FILENAME)
        with open(path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["schema"], pending_approvals.SCHEMA)

    def test_save_overwrites_a_stale_schema_value(self):
        pending_approvals.save({"schema": "some-old-value", "approvals": []}, state_dir=self.dir)
        path = os.path.join(self.dir, pending_approvals.FILENAME)
        with open(path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["schema"], pending_approvals.SCHEMA)


class LegacyShapeTolerance(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, pending_approvals.FILENAME)

    def _write_raw(self, obj) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def test_bare_array_is_tolerated_on_read(self):
        self._write_raw([{"id": "a3", "kind": "slack", "status": "pending"}])
        doc = pending_approvals.load(state_dir=self.dir)
        self.assertEqual(doc["schema"], pending_approvals.SCHEMA)
        self.assertEqual(doc["approvals"], [{"id": "a3", "kind": "slack", "status": "pending"}])

    def test_next_id_continues_a_legacy_bare_array_sequence(self):
        self._write_raw([{"id": "a3", "kind": "slack"}, {"id": "a7", "kind": "slack"}])
        doc = pending_approvals.load(state_dir=self.dir)
        self.assertEqual(pending_approvals.next_id(doc), "a8")

    def test_add_against_a_legacy_file_upgrades_it_in_place(self):
        self._write_raw([{"id": "a1", "kind": "slack", "status": "pending"}])
        pending_approvals.add({"kind": "email", "body": "hi"}, state_dir=self.dir)
        with open(self.path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["schema"], pending_approvals.SCHEMA)
        self.assertEqual(len(on_disk["approvals"]), 2)
        self.assertEqual(on_disk["approvals"][0]["id"], "a1")  # the old entry survived, untouched
        self.assertEqual(on_disk["approvals"][1]["id"], "a2")

    def test_object_shape_with_no_schema_field_is_also_tolerated(self):
        self._write_raw({"approvals": [{"id": "a1", "kind": "slack"}]})
        doc = pending_approvals.load(state_dir=self.dir)
        self.assertEqual(doc["schema"], pending_approvals.SCHEMA)
        self.assertEqual(len(doc["approvals"]), 1)

    def test_corrupt_json_raises_rather_than_reading_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        with self.assertRaises(pending_approvals.CorruptStore):
            pending_approvals.load(state_dir=self.dir)

    def test_unrecognized_shape_raises_rather_than_reading_as_empty(self):
        self._write_raw({"some": "other", "shape": True})
        with self.assertRaises(pending_approvals.CorruptStore):
            pending_approvals.load(state_dir=self.dir)

    def test_corrupt_file_is_never_silently_overwritten_by_add(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        with self.assertRaises(pending_approvals.CorruptStore):
            pending_approvals.add({"kind": "email", "body": "hi"}, state_dir=self.dir)
        # the original (corrupt) bytes must still be there — add() must not have "fixed" it by
        # replacing it with a fresh store holding only the new entry.
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not valid json")


class AddResolveRoundTrip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_add_stamps_id_created_at_and_pending_status(self):
        entry = pending_approvals.add(
            {"kind": "email", "channelRef": "proton:msg-1", "body": "Hi — the assistant here."},
            state_dir=self.dir)
        self.assertEqual(entry["id"], "a1")
        self.assertEqual(entry["status"], "pending")
        self.assertIn("created_at", entry)
        self.assertEqual(entry["kind"], "email")
        self.assertEqual(entry["body"], "Hi — the assistant here.")

    def test_add_refuses_a_caller_supplied_id(self):
        with self.assertRaises(ValueError):
            pending_approvals.add({"kind": "email", "id": "a99"}, state_dir=self.dir)

    def test_add_refuses_a_caller_supplied_status(self):
        with self.assertRaises(ValueError):
            pending_approvals.add({"kind": "email", "status": "sent"}, state_dir=self.dir)

    def test_add_requires_a_kind(self):
        with self.assertRaises(ValueError):
            pending_approvals.add({"body": "hi"}, state_dir=self.dir)

    def test_ids_increment_across_kinds_in_one_shared_sequence(self):
        e1 = pending_approvals.add({"kind": "slack", "body": "one"}, state_dir=self.dir)
        e2 = pending_approvals.add({"kind": "email", "body": "two"}, state_dir=self.dir)
        e3 = pending_approvals.add({"kind": "slack", "body": "three"}, state_dir=self.dir)
        self.assertEqual([e1["id"], e2["id"], e3["id"]], ["a1", "a2", "a3"])

    def test_list_entries_filters_by_status_and_kind(self):
        pending_approvals.add({"kind": "email", "body": "one"}, state_dir=self.dir)
        e2 = pending_approvals.add({"kind": "slack", "body": "two"}, state_dir=self.dir)
        pending_approvals.resolve(e2["id"], "sent", state_dir=self.dir)
        pending = pending_approvals.list_entries(status="pending", state_dir=self.dir)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "email")
        slack_only = pending_approvals.list_entries(kind="slack", state_dir=self.dir)
        self.assertEqual(len(slack_only), 1)
        self.assertEqual(slack_only[0]["id"], e2["id"])

    def test_resolve_flips_status_and_persists(self):
        entry = pending_approvals.add({"kind": "email", "body": "hi"}, state_dir=self.dir)
        updated = pending_approvals.resolve(entry["id"], "sent", state_dir=self.dir)
        self.assertEqual(updated["status"], "sent")
        reloaded = pending_approvals.find(entry["id"], state_dir=self.dir)
        self.assertEqual(reloaded["status"], "sent")

    def test_resolve_merges_extra_fields(self):
        entry = pending_approvals.add({"kind": "email", "body": "hi"}, state_dir=self.dir)
        updated = pending_approvals.resolve(
            entry["id"], "failed", fields={"error": "SMTP timeout"}, state_dir=self.dir)
        self.assertEqual(updated["error"], "SMTP timeout")

    def test_resolve_is_case_insensitive_on_id(self):
        entry = pending_approvals.add({"kind": "email", "body": "hi"}, state_dir=self.dir)
        updated = pending_approvals.resolve(entry["id"].upper(), "sent", state_dir=self.dir)
        self.assertEqual(updated["status"], "sent")

    def test_resolve_raises_not_found_for_unknown_id(self):
        with self.assertRaises(pending_approvals.NotFound):
            pending_approvals.resolve("a999", "sent", state_dir=self.dir)

    def test_find_returns_none_for_unknown_id(self):
        self.assertIsNone(pending_approvals.find("a999", state_dir=self.dir))


class CLI(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _run(self, args, stdin_text=""):
        old_stdin = sys.stdin
        try:
            import contextlib
            import io
            sys.stdin = io.StringIO(stdin_text)
            with contextlib.redirect_stdout(io.StringIO()):
                return pending_approvals._main(["--state-dir", self.dir] + args)
        finally:
            sys.stdin = old_stdin

    def test_add_then_list_round_trip_through_the_cli(self):
        rc = self._run(["add"], stdin_text=json.dumps({"kind": "email", "body": "hi"}))
        self.assertEqual(rc, 0)
        rc = self._run(["list"])
        self.assertEqual(rc, 0)

    def test_add_rejects_invalid_json_on_stdin(self):
        rc = self._run(["add"], stdin_text="{not json")
        self.assertEqual(rc, 2)

    def test_add_rejects_a_missing_kind(self):
        rc = self._run(["add"], stdin_text=json.dumps({"body": "hi"}))
        self.assertEqual(rc, 2)

    def test_resolve_exits_3_for_unknown_id(self):
        rc = self._run(["resolve", "a999", "--status", "sent"])
        self.assertEqual(rc, pending_approvals.EXIT_NOT_FOUND)

    def test_resolve_round_trip_through_the_cli(self):
        self._run(["add"], stdin_text=json.dumps({"kind": "email", "body": "hi"}))
        rc = self._run(["resolve", "a1", "--status", "sent"])
        self.assertEqual(rc, 0)
        entry = pending_approvals.find("a1", state_dir=self.dir)
        self.assertEqual(entry["status"], "sent")

    def test_resolve_rejects_invalid_fields_json(self):
        self._run(["add"], stdin_text=json.dumps({"kind": "email", "body": "hi"}))
        rc = self._run(["resolve", "a1", "--status", "failed", "--fields", "{not json"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
