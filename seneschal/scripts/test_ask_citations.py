#!/usr/bin/env python3
"""Tests for ``ask_citations`` — *a picker may not cite what it does not show*.

*If you reference a document or section, describe the section; an annotation alone is not
enough.* **As a rule, that is prose**, and prose does not bind. So the rule is a shape, and these are
the tests that keep it one.

Three properties:

1. **A citation that cannot be shown REFUSES.** Not a warning, not a degraded send — the opposite
   polarity from `telegram_ask`'s `origin` stamp, and for a stated reason: a suppressed question is
   the only harm the stamp path can do, whereas a picker citing a section that does not exist asks
   the owner to decide against a document they cannot read.
2. **The excerpt is written by this code**, so there is no wording a caller can produce that passes
   the check while leaving the owner without the text.
3. **Relayed text is exempt and the assistant's own framing is not.** A `--quote` span is the boundary, and
   a span that does not occur in the message is refused rather than silently ignored.

No test here reaches Telegram; nothing writes to the live state directory. The merge-guard
integration tests need the optional `merge_guard` module and skip cleanly without it.
"""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ask_citations as ac  # noqa: E402
import telegram_ask as ta  # noqa: E402

try:  # OPTIONAL — the merge-approval guard; its integration tests skip without it
    import merge_guard as mg  # noqa: E402
except ImportError:
    mg = None

NEEDS_MERGE_GUARD = "needs the optional merge_guard module (not installed)"

#: A real, tracked design doc with a numbered section, for the tests that read the actual checkout.
REAL = "seneschal/docs/ask-provenance-spec.md"
REAL_SECTION = "§2.1"
REAL_SECTION_TEXT = "near-misses"


def _fixture(tmp: str) -> str:
    """A tiny repo of the shape this reads: a doc with numbered sections, and a name that collides."""
    os.makedirs(os.path.join(tmp, "seneschal", "docs"), exist_ok=True)
    with open(os.path.join(tmp, "seneschal", "docs", "demo-spec.md"), "w", encoding="utf-8") as fh:
        fh.write("# Demo — the title line\n\nOpening prose for the whole document.\n\n"
                 "## 1. First\n\nThe first section says one thing.\n\n"
                 "### 1.2 Nested\n\n> ### A decision inside a quote\n>\n"
                 "> **The quoted paragraph** with *emphasis* inside it.\n\n"
                 "## 2. Second\n\nThe second section, which must never leak into the first.\n")
    with open(os.path.join(tmp, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("# root\n\nroot text\n")
    with open(os.path.join(tmp, "seneschal", "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("# seneschal\n\nseneschal text\n")
    # A name whose FIRST character is a dot but which is not `./`-prefixed. `lstrip("./")` stripped
    # any leading run of `.` and `/` rather than the two-character prefix, so this arrived at the
    # search as `keeprc.md` — a name the caller never wrote.
    with open(os.path.join(tmp, "seneschal", "docs", ".keeprc.md"), "w", encoding="utf-8") as fh:
        fh.write("# keeprc\n\nthe dot survived\n")
    return tmp


class FindingReferences(unittest.TestCase):

    def test_a_document_and_a_section_are_both_references(self):
        refs = ac.find_references("see foo-spec.md and bar-spec.md §4.2 for that")
        self.assertEqual([(r["doc"], r["section"]) for r in refs],
                         [("foo-spec.md", None), ("bar-spec.md", "4.2")])

    def test_a_section_anchors_to_the_nearest_document_BEFORE_it(self):
        refs = ac.find_references("a-spec.md §1 then b-spec.md §2")
        self.assertEqual([(r["doc"], r["section"]) for r in refs],
                         [("a-spec.md", "1"), ("b-spec.md", "2")])

    def test_a_section_with_no_document_anywhere_is_unanchored(self):
        """The failure this whole module was built for: a picker cites `§8.2` and the owner has to
        stop and ask what it says. `doc` is None, and `citation_block` refuses on it."""
        refs = ac.find_references("approve per §8.2?")
        self.assertEqual(refs[0]["doc"], None)
        self.assertEqual(refs[0]["section"], "8.2")

    def test_a_document_cited_AND_sectioned_is_described_by_its_sections_only(self):
        """Both would spend the budget twice on one file and push the section actually asked about
        further down the message."""
        refs = ac.find_references("x-spec.md §3 and x-spec.md again")
        self.assertEqual([(r["doc"], r["section"]) for r in refs], [("x-spec.md", "3")])

    def test_duplicates_collapse(self):
        refs = ac.find_references("a.md §1, a.md §1, a.md §1")
        self.assertEqual(len(refs), 1)

    def test_a_non_markdown_path_is_not_a_reference(self):
        """A file name says what it is; a section number does not. `.py`, `.json`, a URL and a bare
        `#heading` are all deliberately out of scope."""
        refs = ac.find_references("see mouth.py and config.json and https://x/y and #results")
        self.assertEqual(refs, [])

    def test_a_dot_slash_prefix_SURVIVES_the_scan(self):
        """**The resolver's root qualification is worth nothing if the scanner eats it first**, and
        it would: matched from its first alphanumeric, a picker spelling `./CLAUDE.md` would hand
        `resolve_doc` a bare, ambiguous `CLAUDE.md` and the send would be refused anyway. Fixing only the resolver would have left the caller with no spelling that worked."""
        refs = ac.find_references("It changes ./CLAUDE.md.")
        self.assertEqual([(r["doc"], r["section"]) for r in refs], [("./CLAUDE.md", None)])

    def test_a_parent_directory_HOP_is_not_a_root_qualification(self):
        """`../docs/x.md` reads exactly as it always has — matched at `docs/`. The `./` inside a
        `../` is the tail of a hop OUT of the tree, and quietly reading one as the other would
        resolve a pointer to a file nobody named."""
        refs = ac.find_references("see ../docs/some-binding-spec.md for that")
        self.assertEqual([r["doc"] for r in refs], ["docs/some-binding-spec.md"])

    def test_a_quoted_span_hides_everything_inside_it(self):
        refs = ac.find_references("The assistant asks about a-spec.md §1. PR body: 'fixes b-spec.md §9'",
                                  quoted=["PR body: 'fixes b-spec.md §9'"])
        self.assertEqual([(r["doc"], r["section"]) for r in refs], [("a-spec.md", "1")])

    def test_masking_preserves_offsets_so_anchoring_still_works(self):
        """Spans are blanked, not removed. If they were removed, a later section would anchor to a
        document that is no longer before it."""
        refs = ac.find_references("a-spec.md QQQQ §5", quoted=["QQQQ"])
        self.assertEqual([(r["doc"], r["section"]) for r in refs], [("a-spec.md", "5")])

    def test_junk_is_no_references(self):
        for bad in (None, "", 42, []):
            self.assertEqual(ac.find_references(bad), [])


class Resolving(unittest.TestCase):

    def setUp(self):
        self.root = _fixture(tempfile.mkdtemp())

    def test_a_bare_name_resolves_through_the_docs_directory(self):
        path, amb = ac.resolve_doc("demo-spec.md", self.root)
        self.assertEqual(path, "seneschal/docs/demo-spec.md")
        self.assertEqual(amb, [])

    def test_a_path_qualified_name_is_looked_for_in_exactly_one_place(self):
        self.assertEqual(ac.resolve_doc("seneschal/docs/demo-spec.md", self.root)[0],
                         "seneschal/docs/demo-spec.md")
        self.assertIsNone(ac.resolve_doc("seneschal/docs/nope.md", self.root)[0])

    def test_an_ambiguous_bare_name_resolves_to_NOTHING_rather_than_to_a_guess(self):
        """`CLAUDE.md` names five files in the real tree. Picking one would make the excerpt a guess
        wearing a citation's clothes — which is the harm, not the inconvenience."""
        path, amb = ac.resolve_doc("CLAUDE.md", self.root)
        self.assertIsNone(path)
        self.assertEqual(sorted(amb), ["CLAUDE.md", "seneschal/CLAUDE.md"])

    def test_a_dot_slash_name_resolves_to_the_REPO_ROOT_file(self):
        """**The qualification the ambiguity refusal asks for, which must therefore be possible.**
        Every other file in the tree can be qualified with a directory; a root file
        has none, so without this there is no spelling of `CLAUDE.md` that resolves — and the
        repo-root `CLAUDE.md` is a guarded path, so a merge picker naming it could not be sent."""
        path, amb = ac.resolve_doc("./CLAUDE.md", self.root)
        self.assertEqual(path, "CLAUDE.md")
        self.assertEqual(amb, [])

    def test_the_dot_slash_DOES_NOT_soften_the_bare_refusal(self):
        """**The candidate list is unchanged, deliberately.** Preferring the root for a bare name
        would make the excerpt a guess wearing a citation's clothes, which is the exact thing the
        refusal exists to prevent. The dot-slash is the CALLER saying which one it means; silence
        is not a quieter way of saying it."""
        path, amb = ac.resolve_doc("CLAUDE.md", self.root)
        self.assertIsNone(path)
        self.assertEqual(sorted(amb), ["CLAUDE.md", "seneschal/CLAUDE.md"])

    def test_a_dot_slash_MISS_is_not_ambiguous_and_names_no_candidates(self):
        """One place to look, exactly like a token containing `/`. The caller said which file it
        meant and that file is not there, so there is nothing to disambiguate."""
        path, amb = ac.resolve_doc("./nope.md", self.root)
        self.assertIsNone(path)
        self.assertEqual(amb, [])

    def test_a_dot_slash_MISS_does_not_fall_through_to_the_basename_search(self):
        """`demo-spec.md` exists — one directory down. A fallback would resolve `./demo-spec.md` to
        it and turn the qualification back into the guess it was written to replace."""
        path, amb = ac.resolve_doc("./demo-spec.md", self.root)
        self.assertIsNone(path)
        self.assertEqual(amb, [])

    def test_a_leading_dot_that_is_NOT_dot_slash_keeps_its_dot(self):
        """The second, smaller defect on the same line: `lstrip("./")` strips any leading run of
        `.` and `/` characters rather than the literal prefix, so `.keeprc.md` was hunted for as
        `keeprc.md` in six directories under a name nobody wrote."""
        path, amb = ac.resolve_doc(".keeprc.md", self.root)
        self.assertEqual(path, "seneschal/docs/.keeprc.md")
        self.assertEqual(amb, [])

    def test_dot_slash_also_qualifies_a_path_that_already_names_a_directory(self):
        self.assertEqual(ac.resolve_doc("./seneschal/docs/demo-spec.md", self.root)[0],
                         "seneschal/docs/demo-spec.md")

    def test_a_bare_dot_slash_resolves_to_nothing_rather_than_to_the_root(self):
        self.assertEqual(ac.resolve_doc("./", self.root), (None, []))


class Excerpting(unittest.TestCase):

    def setUp(self):
        self.root = _fixture(tempfile.mkdtemp())

    def test_a_section_excerpt_is_the_heading_plus_its_own_prose(self):
        title, body = ac.section_excerpt("seneschal/docs/demo-spec.md", "1", 400, self.root)
        self.assertEqual(title, "1. First")
        self.assertIn("first section says one thing", body)

    def test_an_excerpt_never_spans_into_the_next_section(self):
        """Otherwise a citation attributes a sibling section's words to the one that was cited."""
        _t, body = ac.section_excerpt("seneschal/docs/demo-spec.md", "1", 400, self.root)
        self.assertNotIn("second section", body)

    def test_a_heading_inside_a_blockquote_is_prose_not_a_boundary(self):
        """In this repo a recorded decision usually lives in exactly that shape, so treating `> ###` as a
        section break truncates the section at its most important paragraph."""
        _t, body = ac.section_excerpt("seneschal/docs/demo-spec.md", "1.2", 400, self.root)
        self.assertIn("A decision inside a quote", body)
        self.assertIn("The quoted paragraph", body)

    def test_markdown_noise_is_stripped_and_the_words_are_not(self):
        _t, body = ac.section_excerpt("seneschal/docs/demo-spec.md", "1.2", 400, self.root)
        for marker in (">", "###", "**", "*"):
            self.assertNotIn(marker, body)
        self.assertIn("emphasis", body)

    def test_an_html_comment_is_stripped_and_does_not_count_against_the_budget(self):
        """A CI checker's escape-hatch marker in an HTML comment renders as nothing on GitHub and must render as nothing in a citation excerpt too — otherwise its own
        character count can push the words the citation was FOR past the truncation cutoff."""
        line = ("word one <!-- ci-marker: exempt — a long reason that would otherwise eat "
                "the whole budget on its own --> word two")
        out = ac._condense([line], 100)
        self.assertNotIn("<!--", out)
        self.assertNotIn("ci-marker", out)
        self.assertIn("word one", out)
        self.assertIn("word two", out)

    def test_a_document_reference_excerpts_the_document_itself(self):
        title, body = ac.section_excerpt("seneschal/docs/demo-spec.md", None, 400, self.root)
        self.assertEqual(title, "Demo — the title line")
        self.assertIn("Opening prose", body)

    def test_a_missing_section_is_None_not_an_empty_excerpt(self):
        self.assertIsNone(ac.section_excerpt("seneschal/docs/demo-spec.md", "99", 400, self.root))

    def test_truncation_is_marked(self):
        _t, body = ac.section_excerpt("seneschal/docs/demo-spec.md", "2", 20, self.root)
        self.assertTrue(body.endswith("…"), body)

    def test_an_unreadable_file_is_None(self):
        self.assertIsNone(ac.section_excerpt("seneschal/docs/gone.md", "1", 400, self.root))


class PRHeadClass(unittest.TestCase):
    """`PRHead` in isolation — the closed allow-list and the fail-closed `read()`, before any of it
    reaches `resolve_doc` or `citation_block`."""

    def test_has_normalizes_a_root_qualified_path_the_same_way_resolve_doc_does(self):
        head = ac.PRHead("o/r", "a" * 40, ["./CLAUDE.md", "seneschal/docs/x.md"])
        self.assertTrue(head.has("CLAUDE.md"))
        self.assertTrue(head.has("./CLAUDE.md"))
        self.assertTrue(head.has("seneschal/docs/x.md"))
        self.assertFalse(head.has("seneschal/docs/y.md"))

    def test_has_is_false_with_no_repo_or_no_sha(self):
        """A `PRHead` with either half missing vouches for nothing, rather than half-working."""
        self.assertFalse(ac.PRHead("", "a" * 40, ["x.md"]).has("x.md"))
        self.assertFalse(ac.PRHead("o/r", "", ["x.md"]).has("x.md"))

    def test_read_never_calls_fetch_for_a_path_outside_the_allowlist(self):
        calls = []
        head = ac.PRHead("o/r", "a" * 40, ["x.md"],
                         fetch=lambda repo, sha, path: calls.append(path) or "text")
        self.assertIsNone(head.read("y.md"))
        self.assertEqual(calls, [])

    def test_read_memoizes_a_success_and_a_failure_alike(self):
        calls = []
        head = ac.PRHead("o/r", "a" * 40, ["x.md"],
                         fetch=lambda repo, sha, path: calls.append(path) or None)
        head.read("x.md")
        head.read("x.md")
        self.assertEqual(calls, ["x.md"], "a failed fetch must not be retried per reference")

    def test_deleted_is_true_only_for_a_confirmed_404_on_a_vouched_for_path(self):
        head = ac.PRHead("o/r", "a" * 40, ["x.md"],
                         fetch=lambda repo, sha, path: ac.NOT_FOUND_AT_HEAD)
        self.assertTrue(head.deleted("x.md"))

    def test_deleted_is_false_for_an_ordinary_fetch_failure(self):
        """`None` (gh missing/offline/rate-limited) is a DIFFERENT failure than a confirmed 404 —
        `deleted` must not conflate "could not read" with "confirmed gone"."""
        head = ac.PRHead("o/r", "a" * 40, ["x.md"], fetch=lambda repo, sha, path: None)
        self.assertFalse(head.deleted("x.md"))

    def test_deleted_is_false_for_a_path_outside_the_allowlist(self):
        """`deleted` must never call `fetch` for a path the caller never vouched for — same rail as
        `read`'s own allow-list gate."""
        calls = []
        head = ac.PRHead("o/r", "a" * 40, ["x.md"],
                         fetch=lambda repo, sha, path: calls.append(path) or ac.NOT_FOUND_AT_HEAD)
        self.assertFalse(head.deleted("y.md"))
        self.assertEqual(calls, [])


class FetchHeadContent(unittest.TestCase):
    """`_fetch_head_content`'s own failure modes, via the `runner` seam — no real `gh`, no network."""

    def _fake(self, returncode=0, stdout="", stderr=""):
        return lambda argv, **kw: type("R", (), {"returncode": returncode, "stdout": stdout,
                                                  "stderr": stderr})()

    def test_a_non_zero_exit_is_None(self):
        self.assertIsNone(ac._fetch_head_content("o/r", "a" * 40, "x.md",
                                                  runner=self._fake(returncode=1, stdout="")))

    def test_a_confirmed_404_is_NOT_FOUND_AT_HEAD_not_None(self):
        """`gh api`'s own error shape for a missing path: `gh: Not Found (HTTP 404)`, non-zero exit,
        empty stdout. This is the one failure `PRHead.deleted` needs to tell apart from every other
        one — a path genuinely gone at this commit, not a fetch that merely didn't work."""
        got = ac._fetch_head_content(
            "o/r", "a" * 40, "x.md",
            runner=self._fake(returncode=1, stderr="gh: Not Found (HTTP 404)"))
        self.assertIs(got, ac.NOT_FOUND_AT_HEAD)

    def test_a_non_404_error_stays_None(self):
        """A rate limit, an auth failure, anything else non-zero that isn't specifically a 404 — the
        generic, un-narrated failure this function has always returned."""
        got = ac._fetch_head_content(
            "o/r", "a" * 40, "x.md",
            runner=self._fake(returncode=1, stderr="gh: API rate limit exceeded (HTTP 403)"))
        self.assertIsNone(got)

    def test_unparseable_json_is_None(self):
        self.assertIsNone(ac._fetch_head_content("o/r", "a" * 40, "x.md",
                                                  runner=self._fake(stdout="not json")))

    def test_a_non_object_response_is_None(self):
        """A directory listing is a JSON ARRAY, not the base64-file object — same refusal."""
        self.assertIsNone(ac._fetch_head_content("o/r", "a" * 40, "x.md",
                                                  runner=self._fake(stdout="[]")))

    def test_a_missing_content_field_is_None(self):
        self.assertIsNone(ac._fetch_head_content(
            "o/r", "a" * 40, "x.md",
            runner=self._fake(stdout=json.dumps({"encoding": "base64"}))))

    def test_undecodable_base64_is_None(self):
        self.assertIsNone(ac._fetch_head_content(
            "o/r", "a" * 40, "x.md",
            runner=self._fake(stdout=json.dumps({"encoding": "base64", "content": "!!!not b64!!!"}))))

    def test_a_transport_failure_is_None(self):
        def raiser(argv, **kw):
            raise OSError("gh not found")
        self.assertIsNone(ac._fetch_head_content("o/r", "a" * 40, "x.md", runner=raiser))

    def test_a_well_formed_response_decodes(self):
        payload = base64.b64encode("hello from head".encode("utf-8")).decode("ascii")
        got = ac._fetch_head_content(
            "o/r", "a" * 40, "x.md",
            runner=self._fake(stdout=json.dumps({"encoding": "base64", "content": payload})))
        self.assertEqual(got, "hello from head")


class TheRefusals(unittest.TestCase):
    """Every way a picker can cite something it cannot show. All of them raise."""

    def setUp(self):
        self.root = _fixture(tempfile.mkdtemp())

    def _block(self, text, **kw):
        kw.setdefault("room", 3000)
        kw.setdefault("root", self.root)
        return ac.citation_block(text, **kw)

    def test_a_bare_section_number_refuses_and_says_how_to_fix_it(self):
        with self.assertRaises(ac.CitationError) as cm:
            self._block("Approve per §8.2?")
        self.assertIn("names no document", str(cm.exception))
        self.assertIn("§8.2", str(cm.exception))

    def test_a_dot_slash_ROOT_document_resolves_AND_is_excerpted(self):
        """The end-to-end shape of the merge picker that could not be sent: a question naming the
        repo-root `CLAUDE.md` now carries that file's own opening prose instead of a refusal."""
        block = self._block("It changes ./CLAUDE.md.")
        self.assertIn("./CLAUDE.md", block)
        self.assertIn("root text", block)

    def test_an_unresolvable_document_refuses(self):
        with self.assertRaises(ac.CitationError) as cm:
            self._block("See seneschal/docs/never-existed.md §1.")
        self.assertIn("does not resolve", str(cm.exception))

    def test_an_ambiguous_document_refuses_and_names_the_candidates(self):
        with self.assertRaises(ac.CitationError) as cm:
            self._block("See CLAUDE.md.")
        self.assertIn("ambiguous", str(cm.exception))
        self.assertIn("seneschal/CLAUDE.md", str(cm.exception))

    def test_a_section_that_is_not_in_the_file_refuses(self):
        """The renumbering case: the file is right, the section moved, and the citation is now a
        confident pointer at nothing."""
        with self.assertRaises(ac.CitationError) as cm:
            self._block("See demo-spec.md §99.")
        self.assertIn("no heading numbered 99", str(cm.exception))

    def test_a_quoted_span_that_is_not_in_the_message_refuses(self):
        """An exemption that silently fails to apply reads as protection and is not."""
        with self.assertRaises(ac.CitationError) as cm:
            self._block("See demo-spec.md §1.", quoted=["text that is not here"])
        self.assertIn("quoted span not found", str(cm.exception))

    def test_too_little_room_refuses_rather_than_shrinking_into_uselessness(self):
        """The failure this module prevents, in miniature: an excerpt below the floor teases a
        section instead of describing it."""
        with self.assertRaises(ac.CitationError) as cm:
            self._block("See demo-spec.md §1 and demo-spec.md §2.", room=300)
        self.assertIn("floor", str(cm.exception))

    def test_citing_nothing_is_not_a_refusal(self):
        self.assertEqual(self._block("Approve this?"), "")
        self.assertEqual(self._block("Deploy mouth.py to the daemon?"), "")


class MinRoomIsTheRefusalArithmetic(unittest.TestCase):
    """`min_room` is what a caller reserves BEFORE spending the message; `citation_block` is what
    refuses AFTER. They are one sum, and this class holds them together at the boundary: the room
    `min_room` names is accepted, and one character less is refused. A copy of the arithmetic in a
    caller would go green while the picker died."""

    def setUp(self):
        self.root = _fixture(tempfile.mkdtemp())

    def test_nothing_cited_reserves_nothing(self):
        self.assertEqual(ac.min_room([]), 0)

    def test_the_room_min_room_names_is_exactly_the_floor(self):
        text = "It changes seneschal/docs/demo-spec.md and ./CLAUDE.md."
        descs = ["seneschal/docs/demo-spec.md", "./CLAUDE.md"]
        room = ac.min_room(descs)
        block = ac.citation_block(text, room=room, root=self.root)
        self.assertIn("Demo — the title line", block)
        self.assertIn("root text", block)
        with self.assertRaises(ac.CitationError) as cm:
            ac.citation_block(text, room=room - 1, root=self.root)
        self.assertIn("floor", str(cm.exception))

    def test_min_room_grows_with_every_description(self):
        one = ac.min_room(["seneschal/docs/demo-spec.md"])
        two = ac.min_room(["seneschal/docs/demo-spec.md", "./CLAUDE.md"])
        self.assertGreater(one, ac.MIN_EXCERPT_CHARS)
        self.assertGreaterEqual(two - one, ac.MIN_EXCERPT_CHARS + len("./CLAUDE.md"))


class PRHeadFallback(unittest.TestCase):
    """"Resolve citations against the PR's own head", as properties rather than snapshots: a file the PR CREATES resolves from head and the picker builds; an unreadable
    head still refuses; a base-resolvable path keeps resolving from the base exactly as before; and
    a path NOT in the allow-list is still uncitable even with a working `head` sitting right there."""

    def setUp(self):
        self.root = _fixture(tempfile.mkdtemp())

    def _head(self, paths, content=None, fetch=None):
        return ac.PRHead("owner/x", "a" * 40, paths,
                         fetch=fetch or (lambda repo, sha, path: content))

    def test_a_file_the_PR_creates_resolves_from_head_and_the_picker_builds(self):
        head = self._head(["seneschal/docs/new-spec.md"],
                          "# New — the title\n\nOpening prose.\n\n"
                          "## 1. First\n\nHead-only text nobody wrote to disk.\n")
        block = ac.citation_block("See seneschal/docs/new-spec.md §1.", room=3000, root=self.root,
                                  head=head)
        self.assertIn("Head-only text nobody wrote to disk", block)

    def test_a_bare_created_name_resolves_through_the_same_search_dirs_as_the_checkout(self):
        head = self._head(["seneschal/docs/created-bare.md"],
                          "# Created\n\nprose\n\n## 1. Sec\n\nBare-name head text.\n")
        block = ac.citation_block("See created-bare.md §1.", room=3000, root=self.root, head=head)
        self.assertIn("Bare-name head text", block)

    def test_a_document_only_citation_of_a_created_file_also_resolves(self):
        """`section=None` — the whole-document citation path — reads through `head` too, not only
        the section-numbered one."""
        head = self._head(["seneschal/docs/new-spec.md"], "# New title\n\nHead-only opening prose.\n")
        block = ac.citation_block("See seneschal/docs/new-spec.md.", room=3000, root=self.root,
                                  head=head)
        self.assertIn("Head-only opening prose", block)

    def test_an_unreadable_head_still_refuses(self):
        """`gh` missing/offline/rate-limited: `read()` returns None, and that must degrade to the
        SAME refusal as a file that was never there — never a partial or placeholder excerpt."""
        head = self._head(["seneschal/docs/new-spec.md"], fetch=lambda repo, sha, path: None)
        with self.assertRaises(ac.CitationError) as cm:
            ac.citation_block("See seneschal/docs/new-spec.md §1.", room=3000, root=self.root,
                              head=head)
        self.assertIn("could not be read", str(cm.exception))

    def test_a_file_the_pr_deletes_gets_a_specific_refusal(self):
        """The deleted-file case: a path the checkout never had AND `gh`
        confirms is gone at head — added and removed within the same still-open PR, most often —
        refuses with an HONEST reason instead of the generic "could not be read" every other head
        failure gets, which would otherwise read identically to `gh` being offline."""
        head = self._head(["seneschal/docs/new-spec.md"],
                          fetch=lambda repo, sha, path: ac.NOT_FOUND_AT_HEAD)
        with self.assertRaises(ac.CitationError) as cm:
            ac.citation_block("See seneschal/docs/new-spec.md §1.", room=3000, root=self.root, head=head)
        self.assertIn("this PR deletes that file", str(cm.exception))
        self.assertNotIn("could not be read", str(cm.exception))

    def test_a_base_resolvable_path_keeps_resolving_from_the_checkout_never_from_head(self):
        """The fallback fires ONLY on a checkout miss. A path the checkout already has must never be
        re-read from `head`, even when `head` also lists it — modifying an existing file is out of
        this fallback's scope; only creating one is in it."""
        head = self._head(["seneschal/docs/demo-spec.md"],
                          "# Wrong\n\n## 1. First\n\nTHIS MUST NEVER BE SHOWN.\n")
        block = ac.citation_block("See demo-spec.md §1.", room=3000, root=self.root, head=head)
        self.assertIn("first section says one thing", block)
        self.assertNotIn("THIS MUST NEVER BE SHOWN", block)

    def test_a_path_not_in_the_allowlist_is_still_uncitable_with_a_working_head(self):
        """`head` vouching for a DIFFERENT path may not widen what this reference can resolve to —
        the allow-list is closed per-path, not per-object."""
        head = self._head(["seneschal/docs/other-file.md"], "# Other\n\ntext\n")
        with self.assertRaises(ac.CitationError) as cm:
            ac.citation_block("See seneschal/docs/new-spec.md §1.", room=3000, root=self.root,
                              head=head)
        self.assertIn("does not resolve", str(cm.exception))

    def test_quote_behavior_is_unchanged_by_a_head_that_could_have_resolved_it(self):
        """A relayed span stays exempt even when `head` could otherwise have resolved the path it
        names — `--quote` masks before resolution is ever attempted."""
        head = self._head(["seneschal/docs/new-spec.md"], "# New\n\nhead text\n")
        relayed = "PR body: touches seneschal/docs/new-spec.md."
        block = ac.citation_block("Merge?\n\n" + relayed, quoted=[relayed], room=3000,
                                  root=self.root, head=head)
        self.assertEqual(block, "")

    def test_annotate_end_to_end_through_a_head_only_citation(self):
        out = ac.annotate("Scope this? See seneschal/docs/new-spec.md §1.", root=self.root,
                          head=self._head(["seneschal/docs/new-spec.md"],
                                          "# New\n\n## 1. First\n\nIt resolves end to end.\n"))
        self.assertIn(ac.CITATION_HEADER, out)
        self.assertIn("It resolves end to end", out)


class AnnotateEndToEnd(unittest.TestCase):

    def setUp(self):
        self.root = _fixture(tempfile.mkdtemp())

    def test_a_body_with_no_citation_is_returned_unchanged(self):
        """Every existing caller that names no document is untouched — which is most of them."""
        body = "Merge the PR?\n\nTap one.\n\n1. Approve\n   Merges it."
        self.assertEqual(ac.annotate(body, root=self.root), body)

    def test_the_excerpt_is_appended_under_a_header(self):
        out = ac.annotate("Scope phase 2? See demo-spec.md §1.", root=self.root)
        self.assertIn(ac.CITATION_HEADER, out)
        self.assertIn("first section says one thing", out)

    def test_the_result_fits_inside_one_telegram_message(self):
        """A picker whose keyboard lands on chunk 3 of a chunked send is a broken picker."""
        out = ac.annotate("See demo-spec.md §1 and demo-spec.md §2.", root=self.root)
        self.assertLess(len(out), ac.TELEGRAM_MESSAGE_CHARS)

    def test_it_reads_the_real_repo_by_default(self):
        """No `root` given: the module resolves against this checkout, which is what the CLI does."""
        out = ac.annotate("Scope phase 2? See %s %s." % (REAL, REAL_SECTION))
        self.assertIn(ac.CITATION_HEADER, out)
        self.assertIn(REAL_SECTION_TEXT, out)


class ThroughTheCLI(unittest.TestCase):
    """`ask()` is where the gate lives, so a Python caller is bound exactly as tightly as argv is."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.c = {"chat_id": "123", "token": "t", "api_base": "https://example.invalid"}

    def _ask(self, question, options=None, **kw):
        options = options or [{"label": "Yes", "description": "do it"},
                              {"label": "No", "description": "do not"}]
        return ta.ask(self.c, self.dir, question, options, dry_run=True,
                      api=lambda *a, **k: self.fail("no test may reach the wire"), **kw)

    def test_a_resolvable_citation_is_inlined_into_the_body(self):
        res = self._ask("Scope phase 2? See %s %s." % (REAL, REAL_SECTION))
        self.assertTrue(res["ok"])
        self.assertIn(ac.CITATION_HEADER, res["body"])

    def test_a_citation_in_an_OPTION_description_is_resolved_too(self):
        """An option description is the assistant's own words in exactly the way the question is."""
        res = self._ask("Which?", options=[
            {"label": "Scope it", "description": "As decided in %s %s." % (REAL, REAL_SECTION)},
            {"label": "Leave it", "description": "Keep the wider bar."}])
        self.assertIn(ac.CITATION_HEADER, res["body"])

    def test_an_unresolvable_citation_raises_BEFORE_anything_is_written_or_sent(self):
        with self.assertRaises(ac.CitationError):
            self._ask("Approve per §8.2?")
        self.assertFalse(os.path.exists(ta.store_path(self.dir)),
                         "a refused question must leave no half-written record")

    def test_dry_run_shows_the_refusal_without_sending(self):
        """`--dry-run` reaches the gate, so a caller can discover a refusal without a message going
        anywhere. That is the whole of 'do not break existing callers silently'."""
        args = _Args(state_dir=self.dir, question="Approve per §8.2?",
                     option=["A|first", "B|second"], dry_run=True)
        code = ta._cmd_ask(args)
        self.assertEqual(code, 2)

    def test_the_refusal_exit_code_and_shape_match_the_other_half_of_the_rule(self):
        """An option with no description is exit 2. A citation with no text is the same kind of
        malformed ask and gets the same code, so a caller has one thing to handle."""
        bad = _Args(state_dir=self.dir, question="See seneschal/docs/never.md §1.",
                    option=["A|first", "B|second"], dry_run=True)
        self.assertEqual(ta._cmd_ask(bad), 2)

    def test_a_quoted_span_lets_a_relayed_citation_through(self):
        relayed = "PR description: this reverts never-existed-spec.md §4."
        res = self._ask("Merge the PR?\n\n" + relayed, quoted=[relayed])
        self.assertTrue(res["ok"])
        self.assertNotIn(ac.CITATION_HEADER, res["body"])

    def test_a_quoted_span_does_not_exempt_the_assistants_own_framing(self):
        """The point of the span: it is an exemption for relayed text, not an off switch."""
        relayed = "PR description: touches a-spec.md §1."
        with self.assertRaises(ac.CitationError):
            self._ask("Merge, per §8.2?\n\n" + relayed, quoted=[relayed])

    def test_the_three_cite_head_flags_build_a_working_PRHead(self):
        """The CLI wiring, not just the library call: `--cite-head-repo/-sha/-path` must reach
        `_cmd_ask` and actually resolve a citation the checkout does not have — proving the flags
        are read and assembled into a `PRHead`, not merely accepted by argparse."""
        args = _Args(state_dir=self.dir, question="See seneschal/docs/cli-created.md §1.",
                    option=["A|first", "B|second"], dry_run=True,
                    cite_head_repo="o/r", cite_head_sha="f" * 40,
                    cite_head_path=["seneschal/docs/cli-created.md"])
        out = io.StringIO()
        with mock.patch.object(ac, "_fetch_head_content",
                               return_value="# CLI\n\n## 1. Sec\n\nfetched via the CLI flags.\n"):
            with contextlib.redirect_stdout(out):
                code = ta._cmd_ask(args)
        self.assertEqual(code, 0)
        res = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertIn("fetched via the CLI flags", res["body"])

    def test_missing_sha_means_no_head_fallback_at_all(self):
        """All three flags are required together — `--cite-head-repo` with no `--cite-head-sha` must
        not half-build a `PRHead`; the reference stays as uncitable as it is with no flags at all."""
        args = _Args(state_dir=self.dir, question="See seneschal/docs/cli-created.md §1.",
                    option=["A|first", "B|second"], dry_run=True,
                    cite_head_repo="o/r", cite_head_path=["seneschal/docs/cli-created.md"])
        self.assertEqual(ta._cmd_ask(args), 2)


@unittest.skipIf(mg is None, NEEDS_MERGE_GUARD)
class MergeGuardStillSends(unittest.TestCase):
    """The approval picker is the highest-stakes caller and a PR body routinely cites specs. If this
    class goes red, a green PR cannot be approved — which is worse than anything the gate prevents."""

    def test_request_argv_marks_both_relayed_bands_as_quoted(self):
        facts = {"repo": "owner/repo", "head_sha": "a" * 40,
                 "title": "docs: retire never-existed-spec.md",
                 "url": "https://github.com/owner/repo/pull/12",
                 "body": "Removes never-existed-spec.md and its §4 pointer. See also gone-spec.md §9."}
        argv = mg.request_argv(12, facts, ["seneschal/scripts/x.py"], tempfile.mkdtemp(), dry_run=True)
        quotes = [argv[i + 1] for i, a in enumerate(argv) if a == "--quote"]
        self.assertTrue(quotes, "the relayed bands must be exempt")
        question = argv[argv.index("--question") + 1]
        for span in quotes:
            self.assertIn(span, question, "a span that does not occur is refused, not ignored")

    def test_a_citation_heavy_pr_body_still_produces_a_sendable_picker(self):
        """End to end through `telegram_ask`'s own parser and `ask()`, on a body naming files that
        do not exist — which is what a PR deleting a spec looks like."""
        state = tempfile.mkdtemp()
        facts = {"repo": "owner/repo", "head_sha": "b" * 40,
                 "title": "docs: retire never-existed-spec.md",
                 "url": "https://github.com/owner/repo/pull/12",
                 "body": "Deletes never-existed-spec.md. Rationale in gone-spec.md §9 and §2."}
        argv = mg.request_argv(12, facts, ["seneschal/scripts/x.py"], state, dry_run=True)
        parsed = ta.main.__globals__  # not used; the parse below is the real assertion
        self.assertIn("--quote", argv)

        options = [ta.parse_option(argv[i + 1]) for i, a in enumerate(argv) if a == "--option"]
        quotes = [argv[i + 1] for i, a in enumerate(argv) if a == "--quote"]
        res = ta.ask({"chat_id": "1"}, state, argv[argv.index("--question") + 1], options,
                     recommend=False, dry_run=True, quoted=quotes,
                     api=lambda *a, **k: self.fail("no wire"))
        self.assertTrue(res["ok"])
        self.assertNotIn(ac.CITATION_HEADER, res["body"], "relayed citations are not excerpted")
        del parsed

    def test_request_argv_emits_the_closed_allowlist_for_a_new_md_blocker(self):
        """The shape: a blocker path that is a `.md` the PR itself CREATES. `request_argv` must
        hand `ask_citations` exactly that path — via `--cite-head-*` — and nothing wider."""
        facts = {"repo": "owner/repo", "head_sha": "c" * 40,
                 "title": "docs(scripts): add a new prompt-path spec",
                 "url": "https://github.com/owner/repo/pull/659", "body": ""}
        argv = mg.request_argv(659, facts, ["seneschal/docs/brand-new-spec.md"], tempfile.mkdtemp(),
                               dry_run=True)
        self.assertIn("--cite-head-repo", argv)
        self.assertEqual(argv[argv.index("--cite-head-repo") + 1], facts["repo"])
        self.assertEqual(argv[argv.index("--cite-head-sha") + 1], facts["head_sha"])
        paths = [argv[i + 1] for i, a in enumerate(argv) if a == "--cite-head-path"]
        self.assertEqual(paths, ["seneschal/docs/brand-new-spec.md"])

    def test_a_docs_only_notice_never_gets_cite_head_flags(self):
        """Its header names no blocker path at all — nothing there could ever need this fallback."""
        facts = {"repo": "owner/repo", "head_sha": "d" * 40,
                 "title": "docs: add brand-new-spec.md",
                 "url": "https://github.com/owner/repo/pull/660", "body": ""}
        argv = mg.request_argv(660, facts, [], tempfile.mkdtemp(), dry_run=True, docs_only=True)
        self.assertNotIn("--cite-head-repo", argv)

    def test_a_pr_that_creates_its_own_cited_blocker_file_now_sends(self):
        """THE DEFECT, end to end: searching only the checkout, a blocker `.md` the PR itself
        creates could never be found there and the picker would refuse forever. `PRHead`, built the same way `_cmd_ask` builds it from `request_argv`'s own flags,
        is what makes it resolve."""
        state = tempfile.mkdtemp()
        facts = {"repo": "owner/repo", "head_sha": "e" * 40,
                 "title": "docs(scripts): add brand-new-spec.md",
                 "url": "https://github.com/owner/repo/pull/659", "body": ""}
        argv = mg.request_argv(659, facts, ["seneschal/docs/brand-new-spec.md"], state, dry_run=True)

        # Confirm the checkout genuinely does not have this file — otherwise the test would pass
        # for the wrong reason (an ordinary base resolution, not the head fallback).
        self.assertIsNone(ac.resolve_doc("seneschal/docs/brand-new-spec.md")[0])

        cite_repo = argv[argv.index("--cite-head-repo") + 1]
        cite_sha = argv[argv.index("--cite-head-sha") + 1]
        cite_paths = [argv[i + 1] for i, a in enumerate(argv) if a == "--cite-head-path"]
        head = ac.PRHead(cite_repo, cite_sha, cite_paths,
                         fetch=lambda repo, sha, path: "# Brand new\n\nIt exists at head.\n")

        options = [ta.parse_option(argv[i + 1]) for i, a in enumerate(argv) if a == "--option"]
        quotes = [argv[i + 1] for i, a in enumerate(argv) if a == "--quote"]
        res = ta.ask({"chat_id": "1"}, state, argv[argv.index("--question") + 1], options,
                     recommend=False, dry_run=True, quoted=quotes, head=head,
                     api=lambda *a, **k: self.fail("no wire"))
        self.assertTrue(res["ok"])
        self.assertIn(ac.CITATION_HEADER, res["body"])
        self.assertIn("It exists at head", res["body"])


class _Args:
    """The argparse namespace `_cmd_ask` reads, with the defaults `main()` would have set."""

    def __init__(self, **kw):
        self.state_dir = kw.get("state_dir")
        self.env_file = None
        self.question = kw.get("question", "")
        self.option = kw.get("option", [])
        self.multi = False
        self.no_recommendation = False
        self.chat_id = "123"
        self.meta = None
        self.origin_session = None
        self.quote = kw.get("quote")
        self.dry_run = kw.get("dry_run", True)
        # Deliberately absent unless a test asks for them (`kw.get(..., None)` -> attribute exists
        # but is `None`, not "missing"): `_cmd_ask` reads all three via `getattr(args, name, None)`
        # for a namespace, like this one, built before the flags existed. Setting them all requires
        # opting in explicitly, matching how `_cmd_ask` only builds a `PRHead` when repo+sha are both
        # truthy.
        self.cite_head_repo = kw.get("cite_head_repo")
        self.cite_head_sha = kw.get("cite_head_sha")
        self.cite_head_path = kw.get("cite_head_path")


if __name__ == "__main__":
    unittest.main()
