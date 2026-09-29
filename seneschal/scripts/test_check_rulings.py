#!/usr/bin/env python3
"""Tests for `check_rulings.py` — `../docs/cleanroom-remediation-spec.md` §3 Phase 5.

Fixture git repos, never the live tree (`test_rechain_budget.py`'s precedent): a `develop` branch and
a `feature` branch cut from it, so `resolve_base` has a real merge-base to find and `added_ruling_lines`
has a real diff to parse. The live ledger's own shape is checked separately, directly against the
tracked file, so a malformed row in the real `rulings.md` fails CI even though every fixture here uses
its own throwaway one.

Run:  python -m unittest seneschal.scripts.test_check_rulings
"""
import io
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, SCRIPT_DIR)

import check_rulings as cr  # noqa: E402

LEDGER_HEADER = (
    "# Rulings\n\n"
    "| id | date | rule | enforced where | source | incident |\n"
    "|---|---|---|---|---|---|\n"
)


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def write(root: str, files: dict):
    for rel, body in files.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)


def commit(root, msg="change"):
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


def ledger_row(rid="2026-09-03-example-rule") -> str:
    return (f"| {rid} | 2026-09-03 | Do the thing. | prompt | CLAUDE.md § example "
            f"| The owner ruled this once. |\n")


class Fixture:
    """base (no `origin` remote — a bare `develop` fallback, per `BASE_CANDIDATES`) -> feature branch."""

    def __init__(self):
        self.root = tempfile.mkdtemp()
        r = self.root
        _git(r, "init", "-q", "-b", "develop")
        _git(r, "config", "user.email", "t@t")
        _git(r, "config", "user.name", "t")
        write(r, {"seneschal/docs/rulings.md": LEDGER_HEADER + ledger_row(),
                  "seneschal/docs/other-spec.md": "# Other\n\nNothing decided yet.\n"})
        commit(r, "base")
        _git(r, "checkout", "-q", "-b", "feature")


class ResolveBaseTests(unittest.TestCase):
    def test_finds_develop_with_no_origin_remote(self):
        fx = Fixture()
        base = cr.resolve_base(fx.root)
        self.assertIsNotNone(base)

    def test_no_answer_when_nothing_resolves(self):
        root = tempfile.mkdtemp()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@t")
        _git(root, "config", "user.name", "t")
        write(root, {"f.txt": "x\n"})
        commit(root, "only commit")
        self.assertIsNone(cr.resolve_base(root))

    def test_explicit_base_wins(self):
        fx = Fixture()
        sha = _git(fx.root, "rev-parse", "develop").stdout.strip()
        self.assertEqual(cr.resolve_base(fx.root, base_ref=sha), sha)


class AddedRulingLinesTests(unittest.TestCase):
    def test_added_ruling_language_is_found(self):
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md":
                         "# Other\n\nThe owner's ruling 2026-09-03: do the thing.\n"})
        commit(fx.root, "add a ruling")
        base = cr.resolve_base(fx.root)
        found = cr.added_ruling_lines(fx.root, base)
        self.assertIn("seneschal/docs/other-spec.md", found)

    def test_preexisting_ruling_language_is_not_flagged(self):
        """Only lines this diff ADDS count — the corpus already says 'ruling' ~1,400 times and none
        of that is this PR's to answer for."""
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md":
                         "# Other\n\nA ruling already lives here.\n\nAnd one new unrelated line.\n"})
        commit(fx.root, "unrelated edit, ruling text pre-existed")
        # Re-baseline: the "ruling" sentence is now part of develop itself.
        _git(fx.root, "checkout", "-q", "develop")
        _git(fx.root, "merge", "-q", "--ff-only", "feature")
        _git(fx.root, "checkout", "-q", "-b", "feature2")
        write(fx.root, {"seneschal/docs/other-spec.md":
                         "# Other\n\nA ruling already lives here.\n\nAnd one MORE unrelated line.\n"})
        commit(fx.root, "another unrelated edit")
        base = cr.resolve_base(fx.root)
        found = cr.added_ruling_lines(fx.root, base)
        self.assertNotIn("seneschal/docs/other-spec.md", found)

    def test_ledger_edits_are_not_self_flagged_by_the_caller(self):
        """The scanner excludes the ledger path from `candidates` itself (scan(), not this helper) —
        this test pins that the raw helper still reports it (it has no opinion), so a regression is
        caught at the right layer."""
        fx = Fixture()
        write(fx.root, {"seneschal/docs/rulings.md": LEDGER_HEADER + ledger_row() +
                         ledger_row("2026-09-04-second-rule")})
        commit(fx.root, "add a row")
        base = cr.resolve_base(fx.root)
        found = cr.added_ruling_lines(fx.root, base)
        self.assertIn("seneschal/docs/rulings.md", found)

    def test_real_ruling_sentence_is_still_caught_unmarked(self):
        """The regression this fix must not cause: a REAL ruling ('The owner ruled out X') is not
        this idiom and must trip the scan exactly as before, with no marker present."""
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md":
                         "# Other\n\nThe owner ruled out the figure-tracing check entirely.\n"})
        commit(fx.root, "a real ruling, no marker")
        base = cr.resolve_base(fx.root)
        found = cr.added_ruling_lines(fx.root, base)
        self.assertIn("seneschal/docs/other-spec.md", found)


class CheckRulingsMarkerTests(unittest.TestCase):
    """The idiom false positive: a research memo using "ruled out" in the ordinary statistical sense,
    not a decision. The exact sentence, marked and unmarked, pins the fix without reopening it to a
    blanket idiom exclusion (which would also silently pass a REAL decision shaped like "the owner
    ruled the send-time check out")."""

    IDIOM_SENTENCE = ("This does not invalidate A1/A2, but it means R2 is not cleanly ruled out, "
                       "only weakly disfavoured.")

    def test_unmarked_idiom_is_flagged(self):
        """Without the escape hatch, the ordinary idiom still trips the scan — the false positive
        preserved here as the documented baseline the marker exists to fix."""
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{self.IDIOM_SENTENCE}\n"})
        commit(fx.root, "the idiom, no marker")
        base = cr.resolve_base(fx.root)
        found = cr.added_ruling_lines(fx.root, base)
        self.assertIn("seneschal/docs/other-spec.md", found)

    def test_marked_idiom_is_suppressed(self):
        line = (f"{self.IDIOM_SENTENCE} <!-- check-rulings: not-a-ruling — \"ruled out\" here is "
                 f"the statistical idiom, no decision by anyone -->")
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{line}\n"})
        commit(fx.root, "the idiom, marked with a reason")
        base = cr.resolve_base(fx.root)
        found = cr.added_ruling_lines(fx.root, base)
        self.assertNotIn("seneschal/docs/other-spec.md", found)
        malformed = cr.malformed_rulings_markers(fx.root, base)
        self.assertNotIn("seneschal/docs/other-spec.md", malformed)

    def test_marker_with_colon_separator_also_suppresses(self):
        """The docstring's example uses an em dash; a colon is accepted too, since not every editor
        makes an em dash easy to type."""
        line = f"{self.IDIOM_SENTENCE} <!-- check-rulings: not-a-ruling: the idiom, not a ruling -->"
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{line}\n"})
        commit(fx.root, "the idiom, marked with a colon")
        base = cr.resolve_base(fx.root)
        found = cr.added_ruling_lines(fx.root, base)
        self.assertNotIn("seneschal/docs/other-spec.md", found)

    def test_marker_with_empty_reason_is_not_a_silent_pass(self):
        """An unexplained opt-out must not suppress the line — it must be VISIBLE as its own
        finding, never treated the same as no marker and never treated the same as a valid one."""
        line = f"{self.IDIOM_SENTENCE} <!-- check-rulings: not-a-ruling — -->"
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{line}\n"})
        commit(fx.root, "the idiom, marked with no reason")
        base = cr.resolve_base(fx.root)
        found = cr.added_ruling_lines(fx.root, base)
        self.assertNotIn("seneschal/docs/other-spec.md", found)
        malformed = cr.malformed_rulings_markers(fx.root, base)
        self.assertIn("seneschal/docs/other-spec.md", malformed)

    def test_reason_containing_a_bare_greater_than_still_matches(self):
        """The defect found 2026-09-08: `[^>]*?` excluded ANY `>` from the reason, so a marker whose
        reason mentioned the `->` operator (or any other lone `>`) failed to match at all, silently —
        the gate stayed red with no hint the marker was the problem."""
        line = (f"{self.IDIOM_SENTENCE} <!-- check-rulings: not-a-ruling — score -> threshold is a "
                 f"comparison, not a real decision -->")
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{line}\n"})
        commit(fx.root, "reason contains a bare >")
        base = cr.resolve_base(fx.root)
        found = cr.added_ruling_lines(fx.root, base)
        self.assertNotIn("seneschal/docs/other-spec.md", found)
        self.assertNotIn("seneschal/docs/other-spec.md", cr.malformed_rulings_markers(fx.root, base))
        self.assertNotIn("seneschal/docs/other-spec.md", cr.malformed_rulings_marker_syntax(fx.root, base))

    def test_reason_containing_the_closing_delimiter_does_not_run_away(self):
        """A reason that itself contains a literal `-->` must not let the match swallow forward past
        the comment's real end — the naive-greedy-fix trap. The marker still validly suppresses (the
        first `-->` correctly closes the HTML comment); what matters is it doesn't cross into the
        next sentence."""
        line = (f"{self.IDIOM_SENTENCE} <!-- check-rulings: not-a-ruling — see note --> right there "
                 f"for context --> The owner ruled this stays open regardless.")
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{line}\n"})
        commit(fx.root, "reason contains the closing delimiter")
        base = cr.resolve_base(fx.root)
        match = cr.CHECK_RULINGS_MARKER_RE.search(line)
        self.assertIsNotNone(match)
        self.assertEqual(match.group("reason"), "see note")
        self.assertNotIn("right there for context", match.group(0))
        self.assertNotIn("The owner ruled this stays open", match.group(0))

    def test_two_markers_on_adjacent_lines_stay_independent(self):
        """Each marker's reason must resolve against its OWN line only — never bleed into a sibling
        marker on the next line, which a greedy or multi-line-capable fix could do."""
        line1 = (f"{self.IDIOM_SENTENCE} <!-- check-rulings: not-a-ruling — first reason, score > 1 "
                 f"-->")
        line2 = "The owner ruled this differently. <!-- check-rulings: not-a-ruling — second reason -->"
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{line1}\n\n{line2}\n"})
        commit(fx.root, "two markers, adjacent lines")
        base = cr.resolve_base(fx.root)
        found = cr.added_ruling_lines(fx.root, base)
        self.assertNotIn("seneschal/docs/other-spec.md", found)
        self.assertNotIn("seneschal/docs/other-spec.md", cr.malformed_rulings_markers(fx.root, base))
        self.assertNotIn("seneschal/docs/other-spec.md", cr.malformed_rulings_marker_syntax(fx.root, base))
        m1 = cr.CHECK_RULINGS_MARKER_RE.search(line1)
        m2 = cr.CHECK_RULINGS_MARKER_RE.search(line2)
        self.assertEqual(m1.group("reason"), "first reason, score > 1")
        self.assertEqual(m2.group("reason"), "second reason")

    def test_malformed_marker_syntax_is_not_silent(self):
        """A marker that is PRESENT but does not parse (here: no closing `-->`) must not look
        identical to no marker at all — it lands in its own bucket, distinct from both a clean
        suppress and the empty-reason case."""
        line = f"{self.IDIOM_SENTENCE} <!-- check-rulings: not-a-ruling — forgot to close it"
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{line}\n"})
        commit(fx.root, "malformed marker, missing -->")
        base = cr.resolve_base(fx.root)
        self.assertNotIn("seneschal/docs/other-spec.md", cr.added_ruling_lines(fx.root, base))
        self.assertNotIn("seneschal/docs/other-spec.md", cr.malformed_rulings_markers(fx.root, base))
        syntax = cr.malformed_rulings_marker_syntax(fx.root, base)
        self.assertIn("seneschal/docs/other-spec.md", syntax)

    def test_marker_only_suppresses_its_own_line(self):
        """A marker on one added line must not blanket-suppress a DIFFERENT added line in the same
        file that also matches — each line stands on its own."""
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md":
                         f"# Other\n\n{self.IDIOM_SENTENCE} "
                         f"<!-- check-rulings: not-a-ruling — the idiom -->\n\n"
                         f"The owner ruled out the figure-tracing check entirely.\n"})
        commit(fx.root, "one marked line, one real ruling")
        base = cr.resolve_base(fx.root)
        found = cr.added_ruling_lines(fx.root, base)
        self.assertIn("seneschal/docs/other-spec.md", found)
        lines = found["seneschal/docs/other-spec.md"]
        self.assertTrue(any("figure-tracing" in ln for ln in lines))
        self.assertFalse(any("disfavoured" in ln for ln in lines))


class ScanTests(unittest.TestCase):
    def test_ruling_without_ledger_row_is_a_finding(self):
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md":
                         "# Other\n\nThe owner ruled this on 2026-09-03.\n"})
        commit(fx.root, "add a ruling, no ledger row")
        result = cr.scan(fx.root)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("ruling-without-ledger-row", kinds)

    def test_ruling_with_ledger_row_is_clean(self):
        fx = Fixture()
        write(fx.root, {
            "seneschal/docs/other-spec.md": "# Other\n\nThe owner ruled this on 2026-09-03.\n",
            "seneschal/docs/rulings.md": LEDGER_HEADER + ledger_row() + ledger_row("2026-09-03-new-one"),
        })
        commit(fx.root, "add a ruling AND a ledger row")
        result = cr.scan(fx.root)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertNotIn("ruling-without-ledger-row", kinds)

    def test_marked_idiom_is_clean_with_no_ledger_touch(self):
        """The idiom false positive, at the scan() level: the idiom, marked with a reason, must not
        produce `ruling-without-ledger-row` even though the ledger is untouched."""
        fx = Fixture()
        line = ("R2 is not cleanly ruled out, only weakly disfavoured. "
                 "<!-- check-rulings: not-a-ruling — the statistical idiom, no real decision -->")
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{line}\n"})
        commit(fx.root, "the idiom, marked")
        result = cr.scan(fx.root)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertNotIn("ruling-without-ledger-row", kinds)
        self.assertNotIn("check-rulings-marker-empty-reason", kinds)

    def test_finding_detail_names_the_matched_sentence(self):
        """The secondary refinement: a human should be able to adjudicate from the finding alone,
        without going and grepping the diff for what actually matched."""
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md":
                         "# Other\n\nThe owner ruled out the figure-tracing check entirely.\n"})
        commit(fx.root, "a real ruling, no ledger row")
        result = cr.scan(fx.root)
        finding = next(f for f in result["findings"] if f["kind"] == "ruling-without-ledger-row")
        self.assertIn("figure-tracing", finding["detail"])

    def test_marker_with_empty_reason_is_a_distinct_finding(self):
        fx = Fixture()
        line = "R2 is not cleanly ruled out, only weakly disfavoured. <!-- check-rulings: not-a-ruling — -->"
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{line}\n"})
        commit(fx.root, "the idiom, marked with no reason")
        result = cr.scan(fx.root)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("check-rulings-marker-empty-reason", kinds)
        self.assertNotIn("ruling-without-ledger-row", kinds)

    def test_malformed_marker_syntax_is_a_distinct_finding(self):
        fx = Fixture()
        line = ("R2 is not cleanly ruled out, only weakly disfavoured. "
                 "<!-- check-rulings: not-a-ruling — forgot to close it")
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{line}\n"})
        commit(fx.root, "malformed marker, missing -->")
        result = cr.scan(fx.root)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("check-rulings-marker-malformed-syntax", kinds)
        self.assertNotIn("ruling-without-ledger-row", kinds)
        self.assertNotIn("check-rulings-marker-empty-reason", kinds)

    def test_reason_with_greater_than_is_clean_at_scan_level(self):
        """Regression pin for the found-the-hard-way bug, at the level CI actually runs: a marker
        whose reason contains a bare `>` must be a clean scan, not `ruling-without-ledger-row`."""
        fx = Fixture()
        line = ("R2 is not cleanly ruled out, only weakly disfavoured. "
                 "<!-- check-rulings: not-a-ruling — 1 -> 2 is an arrow in prose, not a ruling -->")
        write(fx.root, {"seneschal/docs/other-spec.md": f"# Other\n\n{line}\n"})
        commit(fx.root, "reason with a bare >")
        result = cr.scan(fx.root)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertEqual(kinds, [])

    def test_no_ruling_language_no_finding_even_without_ledger_touch(self):
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md": "# Other\n\nJust an ordinary edit.\n"})
        commit(fx.root, "ordinary edit")
        result = cr.scan(fx.root)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertNotIn("ruling-without-ledger-row", kinds)

    def test_no_answer_degrades_to_zero_findings(self):
        root = tempfile.mkdtemp()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@t")
        _git(root, "config", "user.name", "t")
        write(root, {"seneschal/docs/rulings.md": LEDGER_HEADER + ledger_row(),
                     "notes.md": "The owner ruled something here.\n"})
        commit(root, "only commit, no base to diff against")
        result = cr.scan(root)
        self.assertIsNone(result["base"])
        self.assertEqual(result["findings"], [])


class LedgerShapeTests(unittest.TestCase):
    def test_well_formed_table_parses_clean(self):
        rows, findings = cr.parse_ledger_rows(LEDGER_HEADER + ledger_row() + ledger_row("2026-09-04-b"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(findings, [])

    def test_bad_id_is_flagged(self):
        bad = "| not-an-id | 2026-09-03 | Do it. | prompt | src | inc |\n"
        rows, findings = cr.parse_ledger_rows(LEDGER_HEADER + bad)
        self.assertTrue(any(f.kind == "ledger-bad-id" for f in findings))

    def test_empty_cell_is_flagged(self):
        bad = "| 2026-09-03-x |  | Do it. | prompt | src | inc |\n"
        rows, findings = cr.parse_ledger_rows(LEDGER_HEADER + bad)
        self.assertTrue(any(f.kind == "ledger-empty-cell" for f in findings))

    def test_wrong_column_count_is_flagged(self):
        bad = "| 2026-09-03-x | 2026-09-03 | Do it. | prompt | src |\n"
        rows, findings = cr.parse_ledger_rows(LEDGER_HEADER + bad)
        self.assertTrue(any(f.kind == "ledger-malformed-row" for f in findings))

    def test_missing_header_is_flagged(self):
        rows, findings = cr.parse_ledger_rows("# Rulings\n\nNo table here.\n")
        self.assertTrue(any(f.kind == "ledger-unreadable" for f in findings))

    def test_duplicate_ids_are_flagged(self):
        fx = Fixture()
        write(fx.root, {"seneschal/docs/rulings.md":
                         LEDGER_HEADER + ledger_row("2026-09-03-dup") + ledger_row("2026-09-03-dup")})
        commit(fx.root, "duplicate id")
        result = cr.scan(fx.root)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("ledger-duplicate-id", kinds)


class LiveLedgerTests(unittest.TestCase):
    """The tracked `seneschal/docs/rulings.md`, not a fixture — this is the one test in the file that
    would fail on a real malformed row landing in the repo."""

    def test_the_real_ledger_parses_clean(self):
        path = os.path.join(REPO_ROOT, cr.LEDGER_PATH)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        rows, findings = cr.parse_ledger_rows(text)
        # The ledger starts EMPTY (a header and nothing else) and that is well-formed — the parse
        # must find the header (else `ledger-unreadable`) and every row it does find must be clean.
        self.assertEqual([f.as_dict() for f in findings], [])
        self.assertGreaterEqual(len(rows), 0)

    def test_an_empty_ledger_with_its_header_is_well_formed(self):
        rows, findings = cr.parse_ledger_rows(
            "# L\n\n| id | date | rule | enforced where | source | incident |\n"
            "|---|---|---|---|---|---|\n")
        self.assertEqual((rows, [f.as_dict() for f in findings]), ([], []))



class WorkingTreeTests(unittest.TestCase):
    """Diffing `<base>..HEAD` would let a ruling sentence that exists only as an UNCOMMITTED edit
    pass a local `ci_local.py` run vacuously and go red on CI one commit later. The gate diffs the
    working tree."""

    def test_an_uncommitted_ruling_still_fires(self):
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md":
                         "# Other\n\nThe owner ruled this on 2026-09-12.\n"})
        # NO commit, NO `git add` — exactly the state ci_local.py runs in before the commit.
        result = cr.scan(fx.root)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("ruling-without-ledger-row", kinds)

    def test_a_staged_but_uncommitted_ruling_still_fires(self):
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md":
                         "# Other\n\nThe owner ruled this on 2026-09-12.\n"})
        _git(fx.root, "add", "-A")
        result = cr.scan(fx.root)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("ruling-without-ledger-row", kinds)

    def test_an_untracked_new_doc_with_a_ruling_still_fires(self):
        """A brand-new file appears in no `git diff` at all; the helper synthesizes its hunk."""
        fx = Fixture()
        write(fx.root, {"seneschal/docs/brand-new-spec.md":
                         "# New\n\nThe owner ruled this on 2026-09-12.\n"})
        result = cr.scan(fx.root)
        finding = [f for f in result["findings"] if f["kind"] == "ruling-without-ledger-row"]
        self.assertEqual(len(finding), 1)
        self.assertIn("seneschal/docs/brand-new-spec.md", finding[0]["path"])

    def test_an_uncommitted_ledger_edit_counts_as_touching_the_ledger(self):
        """The other direction: the author added the row but has not committed yet — that is a
        clean tree, not a finding, or the fix would just move the false answer."""
        fx = Fixture()
        write(fx.root, {
            "seneschal/docs/other-spec.md": "# Other\n\nThe owner ruled this on 2026-09-12.\n",
            "seneschal/docs/rulings.md": LEDGER_HEADER + ledger_row() + ledger_row("2026-09-12-new-one"),
        })
        result = cr.scan(fx.root)
        self.assertTrue(result["ledger_touched"])
        kinds = [f["kind"] for f in result["findings"]]
        self.assertNotIn("ruling-without-ledger-row", kinds)

    def test_a_clean_checkout_reads_exactly_as_before(self):
        """CI's case: everything committed, nothing dirty — the committed diff is the whole answer."""
        fx = Fixture()
        write(fx.root, {"seneschal/docs/other-spec.md":
                         "# Other\n\nThe owner ruled this on 2026-09-12.\n"})
        commit(fx.root, "committed ruling, no ledger row")
        self.assertEqual(_git(fx.root, "status", "--porcelain").stdout.strip(), "")
        result = cr.scan(fx.root)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("ruling-without-ledger-row", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
