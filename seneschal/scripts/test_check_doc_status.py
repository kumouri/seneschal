#!/usr/bin/env python3
"""Tests for ``check_doc_status`` — the gate under the spec-status ledger.

The question it answers: of every spec in the design record, what has and hasn't been built?

The ledger is **derived**, and this is the module it is derived from. So the property that matters
most is not any single parse — it is that there is **one** reader (:func:`scan`) and no second source
of truth. A hand-maintained ledger drifts exactly the way the prose vocabulary it replaces did.

Two classes of test, and they guard different failures:

* **The grammar**, against handmade fixtures. Every spelling the directory actually used before the
  normalisation is represented, because a checker that only understands the spelling it introduced
  would report a clean tree by not looking at the messy parts.
* **The live tree**, which is what makes `--enforce` safe to ship: it ships enforcing only
  because `TheLiveTree` proves every document already passes.
"""
import io
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import check_doc_status as cds  # noqa: E402


def _doc(root: str, name: str, body: str) -> None:
    d = os.path.join(root, "seneschal", "docs")
    os.makedirs(d, exist_ok=True)
    with io.open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        fh.write(body)


class TheGrammar(unittest.TestCase):

    def _parse(self, line):
        return cds.parse_status("# Title\n\n%s\n\nprose\n" % line)

    def test_a_bare_token(self):
        rec, why = self._parse("**Status:** `BUILT`")
        self.assertEqual((rec["token"], rec["qualifier"]), ("BUILT", None))
        self.assertIsNone(why)

    def test_a_qualifier_is_carried_verbatim(self):
        """The qualifier is where every distinction the five values cannot express lives —
        out-of-tree, host-side, prose-shipped, which phases. It is prose and stays prose."""
        rec, _ = self._parse("**Status:** `PARTIAL(phases 0, 1 and §14)` — and then the old prose")
        self.assertEqual(rec["token"], "PARTIAL")
        self.assertEqual(rec["qualifier"], "phases 0, 1 and §14")

    def test_partial_without_a_qualifier_is_REFUSED(self):
        """A bare PARTIAL is the old `PHASE n BUILT` bug wearing a new name: it says some of it
        shipped and refuses to say which. Four documents number their parts four different ways, so
        the qualifier is the only place that answer can live."""
        rec, why = self._parse("**Status:** `PARTIAL`")
        self.assertIsNone(rec)
        self.assertEqual(why, "qualifier-required")

    def test_an_unknown_token_is_refused(self):
        for bad in ("`SHIPPED`", "`PHASE-0-BUILT`", "`WIP`", "`SUPERSEDED`"):
            rec, why = self._parse("**Status:** " + bad)
            self.assertIsNone(rec, bad)
            self.assertEqual(why, "unknown-token", bad)

    def test_superseded_is_not_a_token_and_that_is_the_finding(self):
        """Designed, then dropped on evidence: all five audit slices reported ZERO file-level use.
        What the tree has is SECTION-level supersession, where the rest of the document stands — and
        a whole-document token would be a false statement about both halves. The qualifier carries
        it. Adding a value nothing uses is how a taxonomy starts describing itself."""
        self.assertNotIn("SUPERSEDED", cds.TOKENS)
        rec, _ = self._parse("**Status:** `SPEC-ONLY(§7 superseded by job-fanout-spec.md)`")
        self.assertEqual(rec["token"], "SPEC-ONLY")
        self.assertIn("superseded", rec["qualifier"])

    def test_the_token_must_be_BACKTICKED(self):
        """Several documents open with the word BUILT in their first sentence of prose. Requiring
        the backticks is what stops a document declaring itself built by accident."""
        rec, why = self._parse("**Status:** BUILT — shipped 2026-07-20")
        self.assertIsNone(rec)
        self.assertEqual(why, "malformed")

    def test_prose_mentioning_a_token_does_not_declare_one(self):
        rec, why = cds.parse_status("# Title\n\nThis spec is BUILT and `PARTIAL` in places.\n")
        self.assertIsNone(rec)
        self.assertEqual(why, "missing")

    def test_missing_and_malformed_stay_DIFFERENT_findings(self):
        """They have different fixes, and lumping them together hides the second one."""
        self.assertEqual(cds.parse_status("# Title\n\njust prose\n")[1], "missing")
        self.assertEqual(cds.parse_status("# Title\n\n**Status:** SPEC ONLY\n")[1], "malformed")

    def test_every_pre_normalisation_spelling_reads_as_malformed_not_as_missing(self):
        """The spellings this directory actually used. A checker that reported these as *missing*
        would send the fix the wrong way — they are declarations, just unreadable ones."""
        for old in ("**Status:** SPEC ONLY — nothing built",
                    "**Status: PHASES 0, 1 and 3 BUILT.**",
                    "> **Status (2026-07-12): phases 0–3 all shipped and live.**",
                    "- **Status:** SPEC ONLY — nothing built, nothing run",
                    "**Status:** research memo, not a plan of record",
                    "**Status:** T1, T2 and T3 BUILT"):
            self.assertEqual(self._parse(old)[1], "malformed", old)

    def test_a_status_below_the_header_is_its_own_finding(self):
        """It is a header field. One in §9 is not seen by anyone skimming the top, and calling that
        'missing' would have someone add a second one."""
        text = "# Title\n" + ("\nfiller" * 60) + "\n\n**Status:** `BUILT`\n"
        self.assertEqual(cds.parse_status(text)[1], "too-deep")

    def test_junk_never_raises(self):
        for bad in ("", None, "no header at all", "#", "**Status:**"):
            rec, why = cds.parse_status(bad)
            self.assertIsNone(rec)
            self.assertIsInstance(why, str)


class TheScan(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_counts_and_findings(self):
        _doc(self.root, "a-spec.md", "# A\n\n**Status:** `BUILT`\n")
        _doc(self.root, "b-spec.md", "# B\n\n**Status:** `PARTIAL(phase 0)`\n")
        _doc(self.root, "c-spec.md", "# C\n\nnothing\n")
        res = cds.scan(self.root)
        self.assertEqual(res["counts"]["BUILT"], 1)
        self.assertEqual(res["counts"]["PARTIAL"], 1)
        self.assertEqual([f["kind"] for f in res["findings"]], ["missing"])

    def test_an_unclassifiable_document_still_appears_in_the_ledger(self):
        """**Silently omitting a document nobody could classify is how an inventory reports itself
        complete while missing things** — which is the failure this whole exercise exists to end. It
        appears with `token: None` so the cockpit shows it as unclassified rather than dropping it."""
        _doc(self.root, "c-spec.md", "# C\n\nnothing\n")
        res = cds.scan(self.root)
        self.assertEqual(len(res["documents"]), 1)
        self.assertIsNone(res["documents"][0]["token"])

    def test_the_summary_is_the_documents_own_prose_and_is_never_rewritten(self):
        _doc(self.root, "a-spec.md",
             "# A\n\n**Status:** `BUILT` — **shipped 2026-07-20** (`6d5775e`), and it has run since\n")
        summary = cds.scan(self.root)["documents"][0]["summary"]
        self.assertIn("shipped 2026-07-20", summary)
        self.assertIn("6d5775e", summary)

    def test_a_missing_directory_is_not_a_crash(self):
        self.assertEqual(cds.scan(tempfile.mkdtemp())["documents"], [])

    def test_json_output_is_the_cockpit_contract(self):
        """The panel renders the DERIVED view and keeps no copy. If these keys move, it breaks —
        which is the correct failure, and the alternative is a second source of truth."""
        _doc(self.root, "a-spec.md", "# A\n\n**Status:** `PARTIAL(phase 0)` — prose\n")
        payload = json.loads(json.dumps(cds.scan(self.root)))
        self.assertEqual(set(payload), {"root", "documents", "findings", "counts"})
        self.assertEqual(set(payload["documents"][0]), {"path", "token", "qualifier", "summary"})
        self.assertEqual(set(payload["counts"]), set(cds.TOKENS))


class TheCLI(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_enforce_blocks_and_the_default_does_not(self):
        _doc(self.root, "c-spec.md", "# C\n\nnothing\n")
        self.assertEqual(cds.main(["--root", self.root]), 0)
        self.assertEqual(cds.main(["--root", self.root, "--enforce"]), 1)

    def test_a_clean_tree_exits_zero_under_enforce(self):
        _doc(self.root, "a-spec.md", "# A\n\n**Status:** `MEMO`\n")
        self.assertEqual(cds.main(["--root", self.root, "--enforce"]), 0)


class TheLiveTree(unittest.TestCase):
    """**This is what makes `--enforce` safe to ship on the day it lands.** A gate that is red when
    it arrives is a gate someone disables — `check_context_pointers.py` cleared 76 standing findings
    before turning blocking, for exactly this reason."""

    def setUp(self):
        self.result = cds.scan()

    def test_every_document_in_the_design_record_declares_a_readable_status(self):
        self.assertEqual(self.result["findings"], [],
                         "normalise the header before this check can enforce")

    def test_the_directory_is_not_empty(self):
        """A scan of nothing has no findings either. Without this, deleting the docs directory would
        make the gate green — a check that passes by not looking."""
        # A floor, not a count: the design record ships about a dozen documents and only grows.
        self.assertGreater(len(self.result["documents"]), 8)

    def test_every_partial_names_which_parts(self):
        for d in self.result["documents"]:
            if d["token"] == "PARTIAL":
                self.assertTrue(d["qualifier"], d["path"])

    def test_the_ledger_renders(self):
        out = io.StringIO()
        cds.render_ledger(self.result, out)
        text = out.getvalue()
        for token in cds.TOKENS:
            if self.result["counts"][token]:
                self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
