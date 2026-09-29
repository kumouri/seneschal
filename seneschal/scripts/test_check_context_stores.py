#!/usr/bin/env python3
"""Tests for the context-stores check (`check_context_stores.py`).

Four families, and the first one is the reason this file exists at all:

1. **`ReportOnlyTests`** — the rail. The checker **prints and exits 0**, findings or not, and
   **writes nothing anywhere**. Every other assertion in this module is about whether a finding
   is correct; these are about whether the module can hurt anything. A regression here turns a
   report into a gate before anyone decided it should be one.

2. **`InvariantTests`** — one case per invariant (I1 / I2 / I3), asserted against **fixture
   trees, never the live repo**. Same posture as `test_check_context_pointers.py`: a check whose
   tests assert against today's `CLAUDE.md` goes red when someone edits `CLAUDE.md`, which is
   the fastest route to a disabled check.

3. **`RatchetTests`** — `declared-only` is the honest escape hatch for a projection nothing can
   regenerate, and it is also the obvious way to make the whole check vacuous. A `reason` is required,
   the count prints on every run, and a venue where nothing is verified must not read as clean.

4. **`TheLiveTree`** — the small set of questions that MUST be asked of the real manifest, because
   they are what makes the artifact true rather than merely well-formed: it parses, every declared
   reader resolves, every `declared-only` pair carries a reason, and `--venue ci` (what CI runs) exits
   0. `test_check_doc_status.py::TheLiveTree` is the precedent.

Run:  python -m unittest seneschal.scripts.test_check_context_stores
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, SCRIPT_DIR)

import check_context_stores as cs  # noqa: E402


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def make_repo(files: dict) -> str:
    """A real, tiny git repo. `git ls-files` (I1d) and `git check-ignore` (I3a rung 2) cannot be
    mocked without losing the thing they are there to test.

    `core.autocrlf` is pinned OFF so a fixture is whatever its bytes say it is on a Windows dev box
    and on Linux CI alike — the line-ending question I2 has to answer is tested by making the
    GENERATOR emit CRLF, not by hoping checkout does."""
    root = tempfile.mkdtemp()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    for rel, body in files.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode, enc = ("wb", None) if isinstance(body, bytes) else ("w", "utf-8")
        with open(path, mode, encoding=enc, newline="" if enc else None) as fh:
            fh.write(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fixture")
    return root


def manifest(*stores) -> str:
    return json.dumps({"_note": ["fixture"], "stores": list(stores)}, indent=1)


def store(sid, source, projections, writer="human", venue="ci") -> dict:
    return {"id": sid, "source": source, "writer": writer, "venue": venue,
            "projections": projections}


def declared(path, readers, reason="because", **extra) -> dict:
    row = {"path": path, "verify": "declared-only", "reason": reason, "readers": readers}
    row.update(extra)
    return row


def codes(result) -> list:
    return sorted(f["invariant"] for f in result["findings"])


def run_cli(root, *args) -> tuple:
    """`main()` in-process with stdout captured — the exit code is the thing under test."""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = cs.main(["--root", root, *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue(), err.getvalue()


def tree_snapshot(root) -> set:
    snap = set()
    for dirpath, _dirs, names in os.walk(root):
        if ".git" in dirpath.split(os.sep):
            continue
        for n in names:
            p = os.path.join(dirpath, n)
            snap.add((os.path.relpath(p, root), os.path.getsize(p)))
    return snap


class ReportOnlyTests(unittest.TestCase):
    """The rail. This check cannot fail a build and cannot write a byte."""

    def test_findings_present_still_exits_zero(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("s", "src/a.md", [declared("out/b.md", [])])),   # I3b: no reader
            "src/a.md": "x", "out/b.md": "y"})
        rc, out, _ = run_cli(root)
        self.assertEqual(rc, 0, "a finding must NEVER be a non-zero exit")
        self.assertIn("I3b", out)
        self.assertIn("report-only", out.lower())

    def test_enforce_is_the_only_way_to_a_nonzero_exit(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("s", "src/a.md", [declared("out/b.md", [])])),
            "src/a.md": "x", "out/b.md": "y"})
        self.assertEqual(run_cli(root, "--enforce")[0], 1)

    def test_enforce_on_a_clean_manifest_is_still_zero(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("s", "src/a.md", [declared("out/b.md", ["docs/r.md"])])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "reads out/b.md"})
        rc, _, _ = run_cli(root, "--enforce")
        self.assertEqual(rc, 0)

    def test_a_full_scan_writes_nothing_into_the_tree(self):
        """The hard rail, asserted rather than promised: report-only means the filesystem is
        untouched. Byte sizes are compared too, so an in-place rewrite is caught as well as a new
        file."""
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("s", "src/a.md", [declared("out/b.md", ["docs/r.md"])])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z"})
        before = tree_snapshot(root)
        run_cli(root)
        self.assertEqual(before, tree_snapshot(root))

    def test_a_generator_cannot_write_into_the_tree(self):
        """The generator runs "in a temp dir", and this is what that buys: a
        generator that writes relative to its cwd lands in a throwaway directory, not in the repo."""
        gen = ("import io, sys\n"
               "io.open('sentinel.txt','w').write('x')\n"
               "sys.stdout.write('body\\n')\n")
        root = make_repo({
            "seneschal/context-stores.json": manifest(store("s", "src/a.md", [
                {"path": "out/b.md", "verify": "regenerate-and-diff",
                 "generator": [sys.executable, "gen.py"], "readers": ["docs/r.md"]}])),
            "src/a.md": "x", "out/b.md": "body\n", "docs/r.md": "z", "gen.py": gen})
        result = cs.scan(root)
        self.assertEqual(codes(result), [], "the fixture regenerates identically")
        self.assertFalse(os.path.exists(os.path.join(root, "sentinel.txt")))

    def test_the_module_declares_itself_report_only_in_json(self):
        root = make_repo({"seneschal/context-stores.json": manifest(), "x.md": "x"})
        self.assertTrue(cs.scan(root)["report_only"])


class InvariantTests(unittest.TestCase):
    """One case per invariant row (I1 / I2 / I3)."""

    # ---- I1 --------------------------------------------------------------------------------
    def test_i1a_two_sources_claiming_one_projection(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("out/b.md", ["docs/r.md"])]),
                store("two", "src/c.md", [declared("out/b.md", ["docs/r.md"])])),
            "src/a.md": "x", "src/c.md": "x", "out/b.md": "y", "docs/r.md": "z"})
        result = cs.scan(root)
        self.assertIn("I1a", codes(result))
        self.assertIn("two sources claim out/b.md: one, two",
                      [f["message"] for f in result["findings"]])

    def test_i1b_a_path_that_is_both_a_source_and_a_projection(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("mid/m.md", ["docs/r.md"])]),
                store("two", "mid/m.md", [declared("out/b.md", ["docs/r.md"])])),
            "src/a.md": "x", "mid/m.md": "x", "out/b.md": "y", "docs/r.md": "z"})
        self.assertIn("I1b", codes(cs.scan(root)))

    def test_i1c_zero_and_two_writers_are_both_findings(self):
        for writer in (None, ["a", "b"], ""):
            with self.subTest(writer=writer):
                s = store("one", "src/a.md", [declared("out/b.md", ["docs/r.md"])])
                if writer is None:
                    s.pop("writer")
                else:
                    s["writer"] = writer
                root = make_repo({"seneschal/context-stores.json": manifest(s),
                                  "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z"})
                self.assertIn("I1c", codes(cs.scan(root)))

    def test_i1d_a_named_writer_of_a_projection_is_reported(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("out/b.md", ["docs/r.md"])])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z",
            "tools/w.py": 'open("out/b.md", "w").write("clobber")\n'})
        result = cs.scan(root)
        self.assertIn("I1d", codes(result))
        self.assertIn("tools/w.py:1 writes projection out/b.md",
                      " ".join(f["message"] for f in result["findings"]))

    def test_i1d_does_not_report_the_declared_generator(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(store("one", "src/a.md", [
                {"path": "out/b.md", "verify": "declared-only", "reason": "r",
                 "generator": ["python", "tools/w.py"], "readers": ["docs/r.md"]}])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z",
            "tools/w.py": 'open("out/b.md", "w").write("legitimate")\n'})
        self.assertNotIn("I1d", codes(cs.scan(root)))

    def test_i1d_does_not_report_a_read(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("out/b.md", ["docs/r.md"])])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z",
            "tools/r.py": 'text = open("out/b.md").read()\n'})
        self.assertNotIn("I1d", codes(cs.scan(root)))

    def test_i1d_skips_notion_addresses(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("notion:collection://abc", ["docs/r.md"])])),
            "src/a.md": "x", "docs/r.md": "z",
            "tools/w.py": 'open("notion:collection://abc", "w")\n'})
        self.assertNotIn("I1d", codes(cs.scan(root)))

    # ---- I2 --------------------------------------------------------------------------------
    def test_i2_regenerate_and_diff_reports_drift_with_a_bounded_excerpt(self):
        gen = "import sys\nsys.stdout.write('\\n'.join(str(i) for i in range(60)) + '\\n')\n"
        root = make_repo({
            "seneschal/context-stores.json": manifest(store("one", "src/a.md", [
                {"path": "out/b.md", "verify": "regenerate-and-diff",
                 "generator": [sys.executable, "gen.py"], "readers": ["docs/r.md"]}])),
            "src/a.md": "x", "out/b.md": "stale\n", "docs/r.md": "z", "gen.py": gen})
        result = cs.scan(root)
        self.assertIn("I2", codes(result))
        row = result["i2_pairs"][0]
        self.assertEqual(row["status"], "DRIFT")
        self.assertEqual((row["added"], row["removed"]), (60, 1))
        self.assertLessEqual(len(result["findings"][0]["diff"]), cs.DIFF_EXCERPT_LINES + 1)
        self.assertTrue(result["findings"][0]["diff"][-1].startswith("…"))

    def test_i2_a_line_ending_only_difference_is_never_drift(self):
        """CRLF normalisation. The committed bytes are LF, the generator emits CRLF, and the pair is CLEAN —
        otherwise every Windows checkout reports drift on every pair and the check gets disabled."""
        gen = "import sys\nsys.stdout.buffer.write(b'one\\r\\ntwo\\r\\n')\n"
        root = make_repo({
            "seneschal/context-stores.json": manifest(store("one", "src/a.md", [
                {"path": "out/b.md", "verify": "regenerate-and-diff",
                 "generator": [sys.executable, "gen.py"], "readers": ["docs/r.md"]}])),
            "src/a.md": "x", "out/b.md": b"one\ntwo\n", "docs/r.md": "z", "gen.py": gen})
        result = cs.scan(root)
        self.assertEqual(result["i2_pairs"][0]["status"], "ok")
        self.assertEqual(codes(result), [])

    def test_i2_venue_dream_diffs_the_disk_and_ci_diffs_the_commit(self):
        """The venue is not cosmetic: `seneschal/state/` is gitignored, so a `dream` pair has no committed
        bytes to diff against and MUST read the disk."""
        gen = "import sys\nsys.stdout.write('live\\n')\n"
        files = {
            "src/a.md": "x", "out/b.md": "committed\n", "docs/r.md": "z", "gen.py": gen}
        proj = {"path": "out/b.md", "verify": "regenerate-and-diff",
                "generator": [sys.executable, "gen.py"], "readers": ["docs/r.md"]}
        for venue, expect in (("ci", "DRIFT"), ("dream", "ok")):
            with self.subTest(venue=venue):
                root = make_repo(dict(files, **{"seneschal/context-stores.json": manifest(
                    store("one", "src/a.md", [proj], venue=venue))}))
                # The disk now says "live"; HEAD still says "committed".
                with open(os.path.join(root, "out", "b.md"), "w", encoding="utf-8",
                          newline="") as fh:
                    fh.write("live\n")
                self.assertEqual(cs.scan(root, venue)["i2_pairs"][0]["status"], expect)

    def test_i2_regenerate_and_diff_without_a_generator_is_a_finding(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(store("one", "src/a.md", [
                {"path": "out/b.md", "verify": "regenerate-and-diff", "readers": ["docs/r.md"]}])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z"})
        result = cs.scan(root)
        self.assertIn("I2", codes(result))
        self.assertEqual(result["i2_pairs"][0]["status"], "error")

    def test_i2_an_unknown_verify_value_is_a_finding(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(store("one", "src/a.md", [
                {"path": "out/b.md", "verify": "trust-me", "readers": ["docs/r.md"]}])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z"})
        self.assertIn("I2", codes(cs.scan(root)))

    # ---- I3 --------------------------------------------------------------------------------
    def test_i3a_a_reader_that_does_not_resolve(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("out/b.md", ["docs/gone.md"])])),
            "src/a.md": "x", "out/b.md": "y"})
        result = cs.scan(root)
        self.assertIn("I3a", codes(result))
        self.assertIn("reader docs/gone.md does not resolve",
                      " ".join(f["message"] for f in result["findings"]))

    def test_i3a_accepts_a_reader_declared_runtime_by_gitignore(self):
        """Rung 2 of the pointer check's ladder, imported rather than reimplemented: a gitignored path whose
        rule is tracked is DECLARED, not dangling."""
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("out/b.md", ["state/live.md"])])),
            ".gitignore": "state/\n", "src/a.md": "x", "out/b.md": "y"})
        self.assertNotIn("I3a", codes(cs.scan(root)))

    def test_i3b_a_projection_generated_for_nobody(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("out/b.md", [])])),
            "src/a.md": "x", "out/b.md": "y"})
        result = cs.scan(root)
        self.assertIn("I3b", codes(result))
        self.assertIn("generated for nobody", " ".join(f["message"] for f in result["findings"]))

    def test_i3c_a_reader_that_names_the_source(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("out/b.md", ["docs/r.md"])])),
            "src/a.md": "x", "out/b.md": "y",
            "docs/r.md": "read the canonical `src/a.md` for this\n"})
        result = cs.scan(root)
        self.assertIn("I3c", codes(result))
        self.assertIn("docs/r.md points at source src/a.md; its projection is out/b.md",
                      " ".join(f["message"] for f in result["findings"]))

    def test_i3c_a_notion_fragment_source_is_not_matched_by_the_bare_database_id(self):
        """The `notion:` scheme is stripped so a bare `collection://…` in prose matches, but a
        FRAGMENT is kept: a document naming the Run Log database has named the run-log store's
        source, and has NOT named the carry-over field that hangs off it. Collapsing the two reports
        one document twice for two different facts."""
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("db", "notion:collection://abc", [declared("out/log.md", ["docs/r.md"])]),
                store("field", "notion:collection://abc#Carry-Over Context",
                      [declared("out/carry.md", ["docs/r.md"])])),
            "out/log.md": "y", "out/carry.md": "y",
            "docs/r.md": "the system of record is collection://abc\n"})
        result = cs.scan(root)
        hits = [f for f in result["findings"] if f["invariant"] == "I3c"]
        self.assertEqual([h["path"] for h in hits], ["out/log.md"])

    # ---- venues ----------------------------------------------------------------------------
    def test_venue_filters_the_stores_it_runs(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("t", "src/a.md", [declared("out/b.md", [])], venue="ci"),
                store("r", "src/c.md", [declared("out/d.md", [])], venue="dream")),
            "src/a.md": "x", "src/c.md": "x", "out/b.md": "y", "out/d.md": "y"})
        self.assertEqual(cs.scan(root, "ci")["stats"]["stores"], 1)
        self.assertEqual(cs.scan(root, "dream")["stats"]["stores"], 1)
        self.assertEqual(cs.scan(root)["stats"]["stores"], 2)

    def test_a_store_belonging_to_no_venue_is_visible_even_under_a_filter(self):
        """The whole reason for a per-venue census: the failure worth catching is a store that is
        verified NOWHERE, and it must be visible from the venue CI actually runs."""
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("t", "src/a.md", [declared("out/b.md", ["docs/r.md"])], venue="ci"),
                store("lost", "src/c.md", [declared("out/d.md", ["docs/r.md"])], venue="somewhere")),
            "src/a.md": "x", "src/c.md": "x", "out/b.md": "y", "out/d.md": "y", "docs/r.md": "z"})
        self.assertEqual(cs.scan(root)["stats"]["venues"].get("unassigned"), 1)
        rc, out, _ = run_cli(root, "--venue", "ci")
        self.assertEqual(rc, 0)
        self.assertIn("unassigned 1", out)
        self.assertIn("verified NOWHERE", out)

    # ---- the manifest itself ---------------------------------------------------------------
    def test_a_broken_manifest_exits_two_rather_than_reporting_clean(self):
        for body in ("{not json", '{"stores": "nope"}', '["a"]'):
            with self.subTest(body=body):
                root = make_repo({"seneschal/context-stores.json": body, "x.md": "x"})
                rc, _, err = run_cli(root)
                self.assertEqual(rc, 2)
                self.assertIn("MANIFEST ERROR", err)

    def test_a_missing_manifest_exits_two(self):
        root = make_repo({"x.md": "x"})
        self.assertEqual(run_cli(root)[0], 2)


class RatchetTests(unittest.TestCase):
    """The three guards that stop `declared-only` from making this a manifest-syntax linter."""

    def test_declared_only_without_a_reason_is_a_finding(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(store("one", "src/a.md", [
                {"path": "out/b.md", "verify": "declared-only", "readers": ["docs/r.md"]}])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z"})
        result = cs.scan(root)
        self.assertIn("I2", codes(result))
        self.assertIn("no `reason`", " ".join(f["message"] for f in result["findings"]))

    def test_an_empty_reason_does_not_satisfy_the_guard(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(store("one", "src/a.md", [
                declared("out/b.md", ["docs/r.md"], reason="   ")])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z"})
        self.assertIn("I2", codes(cs.scan(root)))

    def test_the_declared_only_count_and_ratio_print_on_every_run(self):
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("out/b.md", ["docs/r.md"])])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z"})
        _, out, _ = run_cli(root)
        self.assertIn("0 verified, 1 declared-only, 100%", out)

    def test_zero_verified_does_not_read_as_clean(self):
        """The one thing a report-only check must not do is let '0 findings' be mistaken for
        coverage when nothing was actually checked."""
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("out/b.md", ["docs/r.md"])])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z"})
        _, out, _ = run_cli(root)
        self.assertIn("0 findings", out)
        self.assertIn("NOT CHECKED, not clean", out)

    def test_the_ratio_is_never_itself_a_finding(self):
        """Guard 3: the ratio is a PHASE-4 gate. Nothing is refused on it now — it exists so the
        conversation about enforcing has a measurement rather than an impression."""
        root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("out/b.md", ["docs/r.md"])])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z"})
        result = cs.scan(root)
        self.assertEqual(result["stats"]["declared_only_pct"], 100)
        self.assertEqual(result["findings"], [])


class OutputTests(unittest.TestCase):
    """The specified output shape. Not a byte-for-byte snapshot — the parts a reader acts on."""

    def setUp(self):
        self.root = make_repo({
            "seneschal/context-stores.json": manifest(
                store("one", "src/a.md", [declared("out/b.md", ["docs/r.md"])])),
            "src/a.md": "x", "out/b.md": "y", "docs/r.md": "z"})

    def test_the_header_names_the_manifest_and_the_posture(self):
        _, out, _ = run_cli(self.root)
        self.assertTrue(out.startswith("context stores (seneschal/context-stores.json)"))
        self.assertIn("REPORT-ONLY", out.splitlines()[0])

    def test_the_census_line_carries_every_count_the_spec_names(self):
        _, out, _ = run_cli(self.root)
        self.assertIn("stores 1 · sources 1 · projections 1 · readers 1 · venues: ci 1", out)

    def test_each_invariant_gets_its_own_line_and_the_footer_names_the_escape(self):
        _, out, _ = run_cli(self.root)
        for label in ("I1 one writable surface", "I2 the update flows",
                      "I3 every reader knows its projection"):
            self.assertIn(label, out)
        self.assertIn("Report-only: this module cannot fail a build. --enforce flips it.", out)

    def test_json_is_one_object(self):
        _, out, _ = run_cli(self.root, "--json")
        payload = json.loads(out)
        self.assertEqual(payload["manifest"], "seneschal/context-stores.json")
        self.assertIn("stats", payload)
        self.assertIn("findings", payload)


class TheLiveTree(unittest.TestCase):
    """The questions that must be asked of the REAL manifest, and only those.

    Everything else runs against fixtures on purpose (a check whose tests assert against today's tree
    goes red when someone edits the tree). But a manifest that does not parse, or that declares a
    reader nobody can open, is an artifact making a false claim — and that is exactly what this whole
    check exists to stop being possible."""

    @classmethod
    def setUpClass(cls):
        cls.result = cs.scan(REPO_ROOT)

    def test_the_live_manifest_parses_and_declares_stores(self):
        self.assertGreater(self.result["stats"]["stores"], 0)

    def test_every_declared_reader_resolves(self):
        bad = [f["message"] for f in self.result["findings"] if f["invariant"] == "I3a"]
        self.assertEqual(bad, [], "a declared reader that cannot be opened is a false declaration")

    def test_no_structural_findings_in_the_live_manifest(self):
        """I1a / I1b / I1c and the I2 shape findings are all statements about the MANIFEST rather than
        about the tree, so they are the ones that must be clean the day it lands. I3c is deliberately
        NOT in this list: it is a smell over the tree, it fires today, and it is meant to."""
        structural = [f["message"] for f in self.result["findings"]
                      if f["invariant"] in ("I1", "I1a", "I1b", "I1c", "I2")]
        self.assertEqual(structural, [])

    def test_every_declared_only_pair_carries_a_reason(self):
        for row in self.result["i2_pairs"]:
            if row["status"] == "declared-only":
                self.assertTrue(row["detail"].strip())
                self.assertNotEqual(row["detail"], "NO REASON DECLARED")

    def test_what_ci_runs_exits_zero(self):
        self.assertEqual(run_cli(REPO_ROOT, "--venue", "ci")[0], 0)

    def test_ci_invokes_this_module_without_enforce(self):
        """Enforcing waits for a sustained clean corpus. If a later edit wires the flag into the
        workflow before that decision is made, this goes red first."""
        wf = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
        with io.open(wf, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if "check_context_stores.py" in ln]
        self.assertTrue(lines, "CI must invoke the context-stores check")
        for ln in lines:
            self.assertIn("--venue ci", ln)
            self.assertNotIn("--enforce", ln)


if __name__ == "__main__":
    unittest.main()
