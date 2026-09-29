#!/usr/bin/env python3
"""Tests for check #4 — the truncating-write gate.

**The load-bearing family is the EXCLUSIONS, not the detections.** Detecting `open(p, "w")` is the
easy half and nothing about it is in doubt. What decides whether this check survives contact with the
tree is whether it reports the *correct* build-then-`os.replace` pattern as a violation: with the
staging rule off it finds 35 sites of which 7 are real, and 28 of the false ones are the exact
pattern it exists to promote. A gate that calls the fix a bug gets deleted, so every exclusion is
pinned here with the shape that motivated it.

The second family is **recall**, which is where a gate quietly stops being worth anything. Each of
those tests carries the real call site it was written from: a gate that reports zero findings looks
exactly like a clean tree.

Stdlib unittest only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import check_state_writes as csw  # noqa: E402


def findings(src, exports=None):
    return [f"{f.symbol}:{f.kind}" for f in csw.scan_source(src, "m.py", exports)[0]]


def staged(src, exports=None):
    return [f"{f.symbol}:{f.kind}" for f in csw.scan_source(src, "m.py", exports)[1]]


class DetectsTheRealThing(unittest.TestCase):
    def test_open_w_on_a_state_path(self):
        src = ('import os\n'
               'STATE_DIR = os.path.join("..", "state")\n'
               'def save(data):\n'
               '    with open(os.path.join(STATE_DIR, "x.json"), "w") as fh:\n'
               '        fh.write(data)\n')
        self.assertEqual(findings(src), ["save:open(w)"])

    def test_the_2026_08_31_shape_exactly(self):
        """`io.open(PATH, "w")` then a write that raises. This is the line that cost 774 lines."""
        src = ('import io\n'
               'PATH = "seneschal/state/carry-over.md"\n'
               'def prepend(entry, old):\n'
               '    fh = io.open(PATH, "w")\n'
               '    fh.write(entry + old)\n')
        self.assertEqual(findings(src), ["prepend:open(w)"])

    def test_write_text_and_write_bytes(self):
        src = ('from pathlib import Path\n'
               'STATE_DIR = Path("state")\n'
               'F = STATE_DIR / "a.json"\n'
               'def a():\n'
               '    F.write_text("x")\n'
               'def b():\n'
               '    F.write_bytes(b"x")\n')
        self.assertEqual(sorted(findings(src)), ["a:write_text", "b:write_bytes"])

    def test_a_non_literal_mode_cannot_be_shown_to_be_safe(self):
        src = ('import os\n'
               'STATE_DIR = "state"\n'
               'def save(mode):\n'
               '    open(os.path.join(STATE_DIR, "x"), mode).write("y")\n')
        self.assertEqual(findings(src), ["save:open(<dynamic>)"])

    def test_taint_reaches_a_callee_parameter(self):
        """`_save_json(os.path.join(state_dir, f), d)` — the write is one frame down from the only
        place the word `state` appears."""
        src = ('import os\n'
               'def _save(path, data):\n'
               '    with open(path, "w") as fh:\n'
               '        fh.write(data)\n'
               'def run(state_dir):\n'
               '    _save(os.path.join(state_dir, "x.json"), "{}")\n')
        self.assertEqual(findings(src), ["_save:open(w)"])


class TheStagingExclusion(unittest.TestCase):
    """Exclusion 2 — without it this check reports the fix as the bug."""

    SRC = ('import os\n'
           'def save(state_dir, data):\n'
           '    path = os.path.join(state_dir, "x.json")\n'
           '    tmp = path + ".tmp"\n'
           '    with open(tmp, "w", encoding="utf-8") as fh:\n'
           '        fh.write(data)\n'
           '    os.replace(tmp, path)\n')

    def test_a_staged_write_is_not_a_finding(self):
        self.assertEqual(findings(self.SRC), [])

    def test_but_it_is_still_COUNTED_as_the_correct_pattern(self):
        """The audit number is the point of the report — a staged write must be visible as a staged
        write, not merely absent."""
        self.assertEqual(staged(self.SRC), ["save:open(w)"])

    def test_os_rename_counts_too(self):
        src = self.SRC.replace("os.replace(tmp, path)", "os.rename(tmp, path)")
        self.assertEqual(findings(src), [])

    def test_a_DIFFERENT_name_being_replaced_does_not_launder_the_write(self):
        """The exclusion keys on the name actually opened. A function that happens to `os.replace`
        something else must not get a free pass on a truncating write to the real target."""
        src = ('import os\n'
               'def save(state_dir, data):\n'
               '    path = os.path.join(state_dir, "x.json")\n'
               '    other = path + ".2"\n'
               '    with open(path, "w") as fh:\n'
               '        fh.write(data)\n'
               '    os.replace(other, path)\n')
        self.assertEqual(findings(src), ["save:open(w)"])


class TheOtherExclusions(unittest.TestCase):
    def test_os_open_with_O_EXCL_is_not_a_truncating_write(self):
        """Exclusion 1 — `memory_write._FileLock`, `reminders_acks.queue_lock` and
        `job_push_ledger._lock` all take this shape. `O_EXCL` FAILS when the file exists."""
        src = ('import os\n'
               'def lock(state_dir):\n'
               '    path = os.path.join(state_dir, "x.lock")\n'
               '    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n'
               '    os.close(fd)\n')
        self.assertEqual(findings(src), [])

    def test_os_open_is_classified_as_NEITHER_open_form(self):
        """The exclusion is pinned at the classifier rather than only at its effect. Read as
        pathlib's bound form, `os.open(p, flags)` takes `os` itself for the target and happens to
        come out clean — the right answer for the wrong reason, which is one refactor from wrong."""
        import ast
        call = ast.parse("os.open(p, os.O_CREAT)").body[0].value
        self.assertIsNone(csw._open_form(call))
        for src, want in (("open(p, 'w')", "builtin"), ("io.open(p, 'w')", "builtin"),
                          ("codecs.open(p, 'w')", "builtin"), ("p.open('w')", "bound"),
                          ("Path(x).open('w')", "bound")):
            with self.subTest(src=src):
                node = ast.parse(src).body[0].value
                self.assertEqual(csw._open_form(node), want)

    def test_append_mode_is_left_alone(self):
        """Exclusion 3 — the append-only ledgers are correct as they are, and their own failure mode
        is a lost concurrent entry, which is a different module's problem."""
        src = ('import os\n'
               'def log(state_dir, row):\n'
               '    with open(os.path.join(state_dir, "m.jsonl"), "a") as fh:\n'
               '        fh.write(row)\n')
        self.assertEqual(findings(src), [])

    def test_exclusive_create_cannot_destroy_an_existing_file(self):
        src = ('import os\n'
               'def once(state_dir):\n'
               '    with open(os.path.join(state_dir, "x"), "x") as fh:\n'
               '        fh.write("1")\n')
        self.assertEqual(findings(src), [])

    def test_a_path_with_no_state_segment_is_not_our_business(self):
        src = ('def save(data):\n'
               '    with open("out/report.html", "w") as fh:\n'
               '        fh.write(data)\n')
        self.assertEqual(findings(src), [])

    def test_a_variable_merely_called_path_does_not_taint_the_world(self):
        """`STATE_IDENT` is deliberately narrow. A bare `path` is every second variable in this tree
        and tainting it would make the check report everything, which is the same as reporting
        nothing."""
        src = ('def save(path, data):\n'
               '    with open(path, "w") as fh:\n'
               '        fh.write(data)\n')
        self.assertEqual(findings(src), [])


class RecallGapsThatWereReal(unittest.TestCase):
    """Each of these was silently clean before the rule beside it existed."""

    def test_a_cross_module_path_constant(self):
        """An archon tool writes `QUEUE_FILE`, `DECISIONS_FILE` and `APPLICATIONS_FILE` declared in
        its `<id>_paths.py` module — and the word `state` appears nowhere in
        that file. Three findings, invisible to a per-module pass."""
        exports = {"archon_paths": {"QUEUE_FILE"}}
        src = ('import archon_paths\n'
               'QUEUE_FILE = archon_paths.QUEUE_FILE\n'
               'def save_queue(q):\n'
               '    QUEUE_FILE.write_text(q)\n')
        self.assertEqual(findings(src, exports), ["save_queue:write_text"])

    def test_a_from_import_of_the_same_constant(self):
        exports = {"archon_paths": {"INTEL_PENDING_FILE"}}
        src = ('from archon_paths import INTEL_PENDING_FILE\n'
               'def prune():\n'
               '    INTEL_PENDING_FILE.write_text("")\n')
        self.assertEqual(findings(src, exports), ["prune:write_text"])

    def test_an_argparse_default_carries_the_taint_into_args(self):
        """`discord_poll`'s `--offset-file` and `rag_projects`'s `--map`: a tainted constant becomes
        `args.offset_file`, an attribute whose name says nothing at all."""
        src = ('import argparse, os\n'
               'DEFAULT = os.path.join("..", "state", "discord-offset")\n'
               'def write_offset(path, offset):\n'
               '    with open(path, "w") as fh:\n'
               '        fh.write(offset)\n'
               'def main():\n'
               '    p = argparse.ArgumentParser()\n'
               '    p.add_argument("--offset-file", default=DEFAULT)\n'
               '    args = p.parse_args()\n'
               '    write_offset(args.offset_file, "1")\n')
        self.assertEqual(findings(src), ["write_offset:open(w)"])

    def test_bound_Path_open_puts_the_mode_in_a_DIFFERENT_ARGUMENT(self):
        """`out.open("w")` read with the builtin's offsets takes `"w"` for the FILENAME and `"r"` for
        the mode, so it silently never matched. This is `rag_projects`'s write of
        `state/projects.jsonl`, and it sat unreported through the first three versions of this
        check."""
        src = ('from pathlib import Path\n'
               'STATE = Path("state")\n'
               'def main():\n'
               '    out = STATE / "projects.jsonl"\n'
               '    with out.open("w", encoding="utf-8") as f:\n'
               '        f.write("{}")\n')
        self.assertEqual(findings(src), ["main:open(w)"])

    def test_io_open_and_codecs_open_keep_the_BUILTIN_offsets(self):
        """The mirror of the test above: `io.open` is an attribute access too, but it is the builtin
        signature. Reading it as pathlib's would take the PATH for the mode."""
        for mod in ("io", "codecs"):
            with self.subTest(mod=mod):
                src = (f'import {mod}\n'
                       'def save(state_dir, data):\n'
                       f'    fh = {mod}.open(state_dir + "/x", "w")\n'
                       '    fh.write(data)\n')
                self.assertEqual(findings(src), ["save:open(w)"])


class ScanWiresTheProjectIndexTogether(unittest.TestCase):
    """`scan()` must actually BUILD the cross-module index and hand it to every module.

    Testing `scan_source` with a hand-made `exports` dict proves the rule works and proves nothing
    about whether the real entry point uses it — and with the tree's own violations now fixed, a
    `scan()` that silently passed `{}` would look exactly as clean as one that didn't."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        import subprocess
        def write(name, body):
            with open(os.path.join(self.d, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        write("paths.py", 'from pathlib import Path\n'
                          'STATE_DIR = Path("state")\n'
                          'DECISIONS_FILE = STATE_DIR / "decisions.json"\n')
        write("writer.py", 'import paths\n'
                           'DECISIONS_FILE = paths.DECISIONS_FILE\n'
                           'def save(d):\n'
                           '    DECISIONS_FILE.write_text(d)\n')
        for argv in (["init", "-q"], ["add", "-A", "-f"]):
            subprocess.run(["git"] + argv, cwd=self.d, capture_output=True, check=False)

    def test_scan_finds_a_write_whose_module_never_says_state(self):
        in_place, _ = csw.scan(self.d)
        self.assertEqual([f"{f.path}:{f.symbol}" for f in in_place], ["writer.py:save"])

    def test_build_exports_only_promotes_TOP_LEVEL_constants(self):
        """A local named `state_dir` inside a function is not a module export, and treating it as one
        would taint every importer of that module."""
        exports = csw.build_exports({"m.py": 'def f():\n    state_dir = "state"\n    return state_dir\n'})
        self.assertEqual(exports, {})


class TheAllowlist(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "seneschal"))

    def _write(self, obj):
        with open(os.path.join(self.d, csw.ALLOWLIST_FILE), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def test_a_missing_allowlist_is_empty_not_an_error(self):
        self.assertEqual(csw.load_allowlist(self.d), {})

    def test_an_entry_without_a_reason_is_REFUSED(self):
        """Debt is allowed here; unexplained debt is not. An allowlist that accepts a bare path is
        how a gate becomes a formality."""
        self._write({"allow": [{"path": "a/b.py", "symbol": "f"}]})
        with self.assertRaises(ValueError):
            csw.load_allowlist(self.d)

    def test_a_blank_reason_is_also_refused(self):
        self._write({"allow": [{"path": "a/b.py", "symbol": "f", "reason": "   "}]})
        with self.assertRaises(ValueError):
            csw.load_allowlist(self.d)

    def test_it_keys_on_path_plus_symbol_not_a_line_number(self):
        """An edit above the call site must not silently un-allow it — nor silently allow a NEW
        violation that happens to land on the old line."""
        self._write({"allow": [{"path": "a/b.py", "symbol": "f", "reason": "because"}]})
        self.assertEqual(csw.load_allowlist(self.d), {("a/b.py", "f"): "because"})


class TheLiveTree(unittest.TestCase):
    """The check runs against this repository, so the repository is the fixture that matters — the
    same argument `check_doc_status.TheLiveTree` makes. A gate that is green only on synthetic input
    proves nothing about the tree it gates."""

    def test_the_tree_is_clean_or_every_survivor_is_allowlisted_with_a_reason(self):
        root = csw.repo_root()
        in_place, _ = csw.scan(root)
        allow = csw.load_allowlist(root)
        live = [str(f) for f in in_place if f.key() not in allow]
        self.assertEqual(live, [], "a new in-place write to a state/ path — see the module docstring")

    def test_the_correct_pattern_is_the_overwhelming_majority(self):
        """A sanity bound on the recogniser itself. If `staged` ever collapses to near zero the
        staging rule has broken and the next real finding will be buried in false ones."""
        in_place, stg = csw.scan(csw.repo_root())
        # A floor on the recogniser, plus the ratio the name promises — both, because the tree
        # grows and a bare count calibrated on one tree would rot on the next.
        self.assertGreater(len(stg), 10)
        self.assertGreater(len(stg), 3 * len(in_place))


class ReplaceSiteTracker(unittest.TestCase):
    """The Phase 1 migration tracker (`../docs/cleanroom-remediation-spec.md` §3 Phase 1) — a
    SEPARATE, always-report-only count of `os.replace(` sites outside `stateio.py`. Never part of
    the exit code."""

    def test_finds_a_call_and_its_enclosing_function(self):
        src = "import os\n\ndef f():\n    os.replace(a, b)\n"
        found = csw.scan_source_for_replace(src, "m.py")
        self.assertEqual([(f.symbol, f.line) for f in found], [("f", 4)])

    def test_a_module_level_call_has_no_enclosing_function(self):
        src = "import os\nos.replace('a', 'b')\n"
        found = csw.scan_source_for_replace(src, "m.py")
        self.assertEqual(found[0].symbol, "")

    def test_a_different_replace_method_is_not_matched(self):
        """`str.replace` and any other `.replace(` that isn't `os.replace` must not count."""
        src = "def f(s):\n    return s.replace('a', 'b')\n"
        self.assertEqual(csw.scan_source_for_replace(src, "m.py"), [])

    def test_os_rename_is_not_counted_here(self):
        """This tracker is specifically about `os.replace`, the primitive `stateio.py` hardens —
        `os.rename` (which the truncating-write scan's staging exclusion treats as equivalent) is a
        different migration question and stays out of this count."""
        src = "import os\ndef f():\n    os.rename(a, b)\n"
        self.assertEqual(csw.scan_source_for_replace(src, "m.py"), [])

    def test_a_syntax_error_returns_no_findings_rather_than_raising(self):
        self.assertEqual(csw.scan_source_for_replace("def f(:\n", "m.py"), [])

    def test_multiple_sites_in_one_module(self):
        src = "import os\n\ndef f():\n    os.replace(a, b)\n\ndef g():\n    os.replace(c, d)\n"
        found = csw.scan_source_for_replace(src, "m.py")
        self.assertEqual([f.symbol for f in found], ["f", "g"])

    def test_live_tree_never_counts_stateio_py_itself(self):
        sites = csw.scan_replace_sites(csw.repo_root())
        self.assertTrue(all(os.path.basename(f.path.replace("\\", "/")) != "stateio.py"
                             for f in sites))

    def test_live_tree_never_counts_a_test_file(self):
        sites = csw.scan_replace_sites(csw.repo_root())
        self.assertTrue(all(not os.path.basename(f.path.replace("\\", "/")).startswith("test_")
                             for f in sites))

    def test_it_never_enters_the_enforcing_exit_code(self):
        """Structural, not behavioural: main()'s exit code is computed from `live` alone. Reading
        the source is the honest way to assert an ABSENCE — a behavioural test would need
        `--enforce` already wired to this tracker, which this PR deliberately does not do."""
        import inspect
        src = inspect.getsource(csw.main)
        return_line = [ln for ln in src.splitlines() if ln.strip().startswith("return 1 if")][0]
        self.assertNotIn("replace_sites", return_line)



class WorkingTreeTests(unittest.TestCase):
    """Local gates see the working tree: an in-place
    `state/` write in a script the author has written but not yet `git add`ed is found locally,
    not first on CI after the commit."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git"] + argv, cwd=self.d, capture_output=True, check=False)
        with open(os.path.join(self.d, "clean.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        for argv in (["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git"] + argv, cwd=self.d, capture_output=True, check=False)

    def test_an_untracked_script_with_an_in_place_state_write_is_found(self):
        with open(os.path.join(self.d, "new_writer.py"), "w", encoding="utf-8") as fh:
            fh.write('def save(d):\n    with open("seneschal/state/x.json", "w") as fh:\n'
                     '        fh.write(d)\n')
        in_place, _ = csw.scan(self.d)
        self.assertEqual([f"{f.path}:{f.symbol}" for f in in_place], ["new_writer.py:save"])

    def test_an_uncommitted_edit_to_a_tracked_script_is_found(self):
        with open(os.path.join(self.d, "clean.py"), "w", encoding="utf-8") as fh:
            fh.write('def save(d):\n    with open("seneschal/state/x.json", "w") as fh:\n'
                     '        fh.write(d)\n')
        in_place, _ = csw.scan(self.d)
        self.assertEqual([f"{f.path}:{f.symbol}" for f in in_place], ["clean.py:save"])


if __name__ == "__main__":
    unittest.main()
