#!/usr/bin/env python3
"""Tests for the `InstructionsLoaded` logger (`instructions_loaded.py`,
grounding-restructure-spec.md §6.2, phase 0).

The load-bearing family is the **never-costs-a-session** one. This runs on the critical path of loading
instructions into context, so every failure mode has to be a silent no-op rather than an exception — the
same contract as `mouth.record_assertion`, for the same reason, and the reason no caller wraps it.

The second family is the **root filter**: the hook is registered globally and fires for every Claude
Code session on the machine, so a log that recorded all of them would answer questions about the wrong
tree.

Run:  python -m unittest discover -s seneschal/scripts -p "test_instructions_loaded.py"
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import instructions_loaded as il  # noqa: E402

ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))


def _payload(**over):
    base = {
        "hook_event_name": "InstructionsLoaded",
        "file_path": os.path.join(ROOT, "CLAUDE.md"),
        "load_reason": "session_start",
        "session_id": "sess-1",
        "cwd": ROOT,
    }
    base.update(over)
    return base


class RowShape(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_records_the_documented_fields(self):
        self.assertTrue(il.record(_payload(), self.dir))
        rows = il.read_rows(self.dir)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["schema"], "seneschal.instructions-loaded/1")
        self.assertEqual(row["load_reason"], "session_start")
        self.assertEqual(row["session_id"], "sess-1")
        self.assertEqual(row["rel_path"], "CLAUDE.md")

    def test_rel_path_is_what_consumers_group_by(self):
        il.record(_payload(file_path=os.path.join(ROOT, "seneschal", "references", "CLAUDE.md")),
                  self.dir)
        self.assertEqual(il.read_rows(self.dir)[0]["rel_path"], "seneschal/references/CLAUDE.md")

    def test_an_undocumented_load_reason_is_recorded_not_dropped(self):
        """A payload shape we don't recognise is exactly the thing worth having a row about."""
        il.record(_payload(load_reason="teleported"), self.dir)
        self.assertEqual(il.read_rows(self.dir)[0]["load_reason"], "teleported")

    def test_a_missing_load_reason_reads_unknown(self):
        p = _payload()
        del p["load_reason"]
        il.record(p, self.dir)
        self.assertEqual(il.read_rows(self.dir)[0]["load_reason"], "unknown")


class RootFilter(unittest.TestCase):
    """The hook is global; this log is not."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_a_load_inside_this_tree_is_recorded(self):
        self.assertTrue(il.record(_payload(), self.dir))

    def test_a_load_from_another_project_is_dropped(self):
        other = os.path.join(tempfile.mkdtemp(), "some-other-repo", "CLAUDE.md")
        self.assertFalse(il.record(_payload(file_path=other), self.dir))
        self.assertEqual(il.read_rows(self.dir), [])

    def test_a_sibling_directory_with_a_shared_prefix_is_not_ours(self):
        """`<repo>-dev` starts with `<repo>` — a naive prefix test would swallow the dev checkout."""
        self.assertFalse(il.under_repo_root(ROOT + "-dev/CLAUDE.md", ROOT))

    def test_the_root_itself_is_not_treated_as_a_child(self):
        self.assertFalse(il.under_repo_root(ROOT, ROOT))

    def test_an_unresolvable_path_is_kept(self):
        """Fail-open: a dropped row is the only thing this module can get badly wrong."""
        self.assertTrue(il.under_repo_root("\x00not-a-path", ROOT))

    def test_all_roots_actually_records_everything(self):
        """`all_roots` is a flag, not a magic root value. Passing `os.sep` as "match everything" looks
        right and, on Windows, matches NOTHING — a filter that silently drops every row is the exact
        failure this whole logger exists to make impossible."""
        other = os.path.join(tempfile.mkdtemp(), "some-other-repo", "CLAUDE.md")
        self.assertFalse(il.record(_payload(file_path=other), self.dir))
        self.assertTrue(il.record(_payload(file_path=other), self.dir, all_roots=True))
        self.assertEqual(len(il.read_rows(self.dir)), 1)

    def test_the_cli_flag_reaches_the_filter(self):
        other = os.path.join(tempfile.mkdtemp(), "elsewhere", "CLAUDE.md")
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(_payload(file_path=other)))):
            il.main(["--state-dir", self.dir, "--all-roots"])
        self.assertEqual(len(il.read_rows(self.dir)), 1)


class NeverCostsASession(unittest.TestCase):
    """Every one of these must be a silent no-op. If any raises, a hook failure becomes a session
    failure while instructions are loading."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_a_non_dict_payload(self):
        for junk in (None, [], "text", 42):
            self.assertFalse(il.record(junk, self.dir))

    def test_an_empty_or_missing_file_path(self):
        self.assertFalse(il.record(_payload(file_path=""), self.dir))
        p = _payload()
        del p["file_path"]
        self.assertFalse(il.record(p, self.dir))

    def test_a_directory_squatting_on_the_log_path(self):
        os.makedirs(il.log_path(self.dir), exist_ok=True)
        self.assertFalse(il.record(_payload(), self.dir))

    def test_an_exploding_open(self):
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(il.record(_payload(), self.dir))

    def test_an_unserialisable_field(self):
        self.assertFalse(il.record(_payload(session_id=object()), self.dir))

    def test_hook_mode_exits_zero_on_garbage_stdin(self):
        for raw in ("", "not json", "[]", '{"file_path": null}'):
            with mock.patch.object(sys, "stdin", io.StringIO(raw)):
                self.assertEqual(il.main(["--state-dir", self.dir]), 0, raw)

    def test_hook_mode_exits_zero_when_the_write_fails(self):
        with mock.patch.object(il, "record", side_effect=RuntimeError("boom")):
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(_payload()))):
                # `record` never raises in reality; this proves main() would survive it if it did.
                with self.assertRaises(RuntimeError):
                    il.main(["--state-dir", self.dir])

    def test_hook_mode_prints_nothing_to_stdout(self):
        """A hook's stdout can reach the model. This one has nothing to say."""
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(_payload()))):
                il.main(["--state-dir", self.dir])
        self.assertEqual(buf.getvalue(), "")


class Report(unittest.TestCase):
    """The three questions §6.2 exists to answer."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_empty(self):
        r = il.report(self.dir)
        self.assertEqual(r["rows"], 0)
        self.assertEqual(r["include_sightings"], 0)

    def test_counts_by_path_and_reason(self):
        il.record(_payload(), self.dir)
        il.record(_payload(), self.dir)
        il.record(_payload(file_path=os.path.join(ROOT, "seneschal", "CLAUDE.md"),
                           load_reason="nested_traversal", session_id="sess-2"), self.dir)
        r = il.report(self.dir)
        self.assertEqual(r["rows"], 3)
        self.assertEqual(r["sessions"], 2)
        self.assertEqual(r["by_path"]["CLAUDE.md"], 2)
        self.assertEqual(r["by_reason"]["nested_traversal"], 1)

    def test_include_sightings_flag_the_at_path_trap(self):
        """§3.2's trap: `load_reason: "include"` means an `@path` import crept in. Should stay 0."""
        il.record(_payload(load_reason="include"), self.dir)
        self.assertEqual(il.report(self.dir)["include_sightings"], 1)

    def test_never_loaded_is_the_fold_back_candidate_list(self):
        il.record(_payload(), self.dir)
        r = il.report(self.dir, known_paths=["CLAUDE.md", "seneschal/CLAUDE.md", "cockpit/CLAUDE.md"])
        self.assertEqual(r["never_loaded"], ["cockpit/CLAUDE.md", "seneschal/CLAUDE.md"])


if __name__ == "__main__":
    unittest.main()
