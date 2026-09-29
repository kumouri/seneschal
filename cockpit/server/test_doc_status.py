"""Tests for the cockpit's open-spec ledger reader.

The property that matters most here is **that there is no second copy**. A prose status vocabulary
maintained by hand drifts into many spellings across a docs tree; a cockpit panel with its own list
would fail the same way, more quietly, because nobody diffs a dashboard. So the tests below check the derivation, not a fixture of expected rows.

Written against a synthetic docs tree plus the real one, in the same posture as every other reader
here: tolerant on every path, `available: false` rather than a 500, and an empty-but-present
directory kept distinct from an unreadable one.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cockpit.server import doc_status  # noqa: E402


def _tree(**docs) -> str:
    """A synthetic repo whose `seneschal/docs/` holds the given `name -> body`."""
    root = tempfile.mkdtemp()
    d = Path(root) / "seneschal" / "docs"
    d.mkdir(parents=True)
    for name, body in docs.items():
        with io.open(d / name.replace("_", "-"), "w", encoding="utf-8") as fh:
            fh.write(body)
    return root


class TheDerivation(unittest.TestCase):

    def test_every_row_comes_from_the_documents_own_header(self):
        root = _tree(**{
            "a-spec.md": "# A\n\n**Status:** `BUILT` — shipped 2026-07-20\n",
            "b-spec.md": "# B\n\n**Status:** `PARTIAL(phases 0 and 1)` — the rest is designed\n",
        })
        res = doc_status.read_status(root)
        self.assertTrue(res["available"])
        by_name = {d["name"]: d for d in res["documents"]}
        self.assertEqual(by_name["a-spec.md"]["token"], "BUILT")
        self.assertEqual(by_name["b-spec.md"]["qualifier"], "phases 0 and 1")
        self.assertIn("shipped 2026-07-20", by_name["a-spec.md"]["summary"])

    def test_open_count_is_computed_HERE_not_in_the_browser(self):
        """It is the answer to the question the ledger exists for. Computing it in the
        panel would let the panel and any other consumer disagree about what "open" means."""
        root = _tree(**{
            "a-spec.md": "# A\n\n**Status:** `BUILT`\n",
            "b-spec.md": "# B\n\n**Status:** `PARTIAL(phase 0)`\n",
            "c-spec.md": "# C\n\n**Status:** `SPEC-ONLY`\n",
            "d-spec.md": "# D\n\n**Status:** `MEMO`\n",
            "e-spec.md": "# E\n\n**Status:** `REFERENCE`\n",
        })
        res = doc_status.read_status(root)
        self.assertEqual(res["total"], 5)
        self.assertEqual(res["open_count"], 2, "PARTIAL + SPEC-ONLY")

    def test_a_memo_and_a_reference_are_not_open_work(self):
        """A memo is complete when written and a router is never finished by construction. Counting
        either as backlog would make the ledger's headline number meaningless."""
        self.assertNotIn("MEMO", doc_status.OPEN_TOKENS)
        self.assertNotIn("REFERENCE", doc_status.OPEN_TOKENS)
        self.assertNotIn("BUILT", doc_status.OPEN_TOKENS)

    def test_the_gloss_is_served_rather_than_written_in_the_browser(self):
        """A legend hand-written in TSX is a third copy of the vocabulary and would be the first
        thing to go stale — cockpit ruling 3, applied to prose rather than to a table."""
        res = doc_status.read_status(_tree(**{"a-spec.md": "# A\n\n**Status:** `BUILT`\n"}))
        self.assertTrue(res["gloss"])
        for token, text in res["gloss"].items():
            self.assertIn(token, doc_status.DEFAULT_ORDER)
            self.assertTrue(text.strip(), token)

    def test_an_unclassifiable_document_is_SERVED_not_dropped(self):
        """Silently omitting it is how an inventory reports itself complete while missing things —
        the exact failure a derived ledger exists to end. It carries WHY, so the panel can say "this
        needs a status" instead of showing an unexplained blank."""
        root = _tree(**{"a-spec.md": "# A\n\nno status at all\n"})
        res = doc_status.read_status(root)
        self.assertEqual(res["total"], 1)
        self.assertIsNone(res["documents"][0]["token"])
        self.assertEqual(res["documents"][0]["finding"], "missing")
        self.assertEqual(res["unclassified_count"], 1)


class Tolerance(unittest.TestCase):

    def test_a_present_but_empty_docs_dir_is_available_with_nothing_in_it(self):
        """Different from "I cannot see the design record at all", and they must not look the same
        to a reader — the same distinction `jobs.py` draws for an empty jobs dir."""
        res = doc_status.read_status(_tree())
        self.assertTrue(res["available"])
        self.assertEqual(res["documents"], [])
        self.assertEqual(res["total"], 0)

    def test_a_missing_tree_does_not_raise(self):
        res = doc_status.read_status(os.path.join(tempfile.mkdtemp(), "nope"))
        self.assertTrue(res["available"])
        self.assertEqual(res["documents"], [])

    def test_an_unimportable_checker_degrades_with_a_REASON(self):
        """A panel may never be the thing that 500s the cockpit — and "unavailable" with no
        explanation is the shape of failure nobody investigates."""
        real = doc_status._checker
        doc_status._checker = lambda: None
        try:
            res = doc_status.read_status()
        finally:
            doc_status._checker = real
        self.assertFalse(res["available"])
        self.assertIn("check_doc_status", res["reason"])
        self.assertEqual(res["documents"], [])
        self.assertEqual(res["open_count"], 0)

    def test_a_raising_scan_degrades_rather_than_propagating(self):
        real = doc_status._checker

        class Boom:
            @staticmethod
            def scan(_root):
                raise RuntimeError("disk went away")

        doc_status._checker = lambda: Boom
        try:
            res = doc_status.read_status()
        finally:
            doc_status._checker = real
        self.assertFalse(res["available"])
        self.assertIn("disk went away", res["reason"])

    def test_the_unavailable_payload_has_the_SAME_SHAPE_as_a_good_one(self):
        """So the panel has one renderer and cannot crash on the degraded case — which is the case
        it will only ever meet in production."""
        good = doc_status.read_status(_tree(**{"a-spec.md": "# A\n\n**Status:** `MEMO`\n"}))
        bad = doc_status._unavailable("because")
        self.assertEqual(set(good), set(bad))


class TheRealDesignRecord(unittest.TestCase):
    """Against this repo's own `seneschal/docs/`, which is what the panel will actually render."""

    def setUp(self):
        self.res = doc_status.read_status()

    def test_it_reads(self):
        self.assertTrue(self.res["available"], self.res["reason"])
        self.assertGreater(self.res["total"], 20)

    def test_nothing_is_unclassified(self):
        """CI enforces this too; asserting it here is what stops the panel shipping a column of
        blanks on the day someone adds a document without a status."""
        self.assertEqual(self.res["unclassified_count"], 0)

    def test_every_partial_row_says_which_parts(self):
        for d in self.res["documents"]:
            if d["token"] == "PARTIAL":
                self.assertTrue(d["qualifier"], d["path"])

    def test_the_ledger_is_not_all_one_token(self):
        """A derivation bug that returned a constant would still pass every test above."""
        self.assertGreater(len([t for t in self.res["counts"].values() if t]), 2)


if __name__ == "__main__":
    unittest.main()
