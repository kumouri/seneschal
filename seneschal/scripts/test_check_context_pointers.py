#!/usr/bin/env python3
"""Tests for the dangling-pointer check (`check_context_pointers.py`).

Two load-bearing families:

1. **`RulingTests`** — one case per skip rule, asserting the *ruling* rather than the
   implementation: widening a skip rule must argue with a red test first. Without the rules a
   naive recogniser reports mostly false findings; a skip rule quietly widened turns this check
   back into a wall, and a skip rule quietly narrowed turns it into a sieve.

2. **`OracleTests`** — the `git check-ignore` traps, exercised against a **real temporary git
   repo**. Both were found by running `git check-ignore`, not by reasoning about it, and both are
   invisible to a mock: they make the oracle silently *under*-report, which emits false
   *dangling* findings — the failure that kills adoption.

Tests run against **fixture trees, never the live repo**: a check whose tests assert against
today's `CLAUDE.md` goes red when someone edits `CLAUDE.md`, which is the fastest route to a
disabled check.

Run:  python -m unittest seneschal.scripts.test_check_context_pointers
"""
import glob
import os
import re
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import check_context_pointers as cp  # noqa: E402


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def make_repo(files: dict, gitignore: str | None = None) -> str:
    """A real, tiny git repo. The check-ignore oracle cannot be mocked without losing the point."""
    root = tempfile.mkdtemp()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    if gitignore is not None:
        files = dict(files, **{".gitignore": gitignore})
    for rel, body in files.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fixture")
    return root


class RulingTests(unittest.TestCase):
    """One case per skip rule. Each asserts the RULING, so a widened skip rule goes red here first."""

    def test_schema_identifiers_are_skipped(self):
        for token in ("seneschal.job-leads/1", "seneschal.turn/1", "seneschal.assertion/1",
                      "seneschal.outbound/1"):
            self.assertEqual(cp.should_skip(token), "schema-identifier", token)

    def test_http_routes_are_skipped(self):
        for token in ("/api/status", "/auth/login", "/api/archons/{id}/restart"):
            self.assertEqual(cp.should_skip(token), "external", token)

    def test_git_refs_are_skipped_but_narrowly(self):
        for token in ("origin/develop", "feat/archon-tiresias", "docs/spend-levers-spec",
                      "chore/whatever"):
            self.assertEqual(cp.should_skip(token), "git-ref", token)
        # Narrow ON PURPOSE: `seneschal/SKILL.md` has an extension and must NOT be skipped as a branch.
        self.assertIsNone(cp.should_skip("seneschal/SKILL.md"))
        self.assertIsNone(cp.should_skip("seneschal/docs/mouth-spec.md"))

    def test_prose_slashes_are_skipped(self):
        for token in ("and/or", "they/them", "try/except", "read/write"):
            self.assertEqual(cp.should_skip(token), "prose", token)

    def test_mcp_tool_names_are_skipped(self):
        """Skipped, though by shape rather than by the `mcp__` rule: the real tokens carry no `/` at
        all, so they never become candidates. The `mcp__` rule still earns its place for the slashed
        variants; what this test pins is that NONE of these reach the resolver."""
        for token in ("mcp__<server>__notion-*", "mcp__Figma__get_metadata",
                      "mcp__server__a/b"):
            self.assertIsNotNone(cp.should_skip(token), token)

    def test_urls_are_skipped(self):
        """A schemeless host is the case the `url` ruling actually exists for — a scheme'd URL carries
        a `:` and is already out of the path-shaped charset, so it is skipped one rung earlier."""
        self.assertEqual(cp.should_skip("github.com/kumouri/seneschal"), "url")
        self.assertEqual(cp.should_skip("docs.claude.com/en/docs/build"), "url")
        for scheme_d in ("https://example.com/x", "mailto:x@y.com"):
            self.assertIsNotNone(cp.should_skip(scheme_d), scheme_d)

    def test_external_paths_are_skipped(self):
        for token in ("~/.claude/commands/assistant.md", "./local",
                      r"C:\Users\someone\repo", "/etc/hosts"):
            self.assertEqual(cp.should_skip(token), "external", token)

    def test_a_dotdot_AFTER_A_REAL_SEGMENT_is_external(self):
        """The first real run hit `SCRIPT_DIR/../..` — a code-shaped token quoted in prose. It escapes
        the repo from the MIDDLE, and feeding it to check-ignore exits 128 and aborts the whole batch
        (trap 2). The oracle guard caught it loudly; this is the ruling that followed.

        `..` after a real segment is external — but a LEADING run of `../` is not, because treating
        it as external would skip most of this repo's cross-directory pointers rather than another
        repo's. See `test_a_leading_dotdot_is_a_pointer_NOT_external`."""
        for token in ("SCRIPT_DIR/../..", "seneschal/../etc/passwd", "a/../b"):
            self.assertEqual(cp.should_skip(token), "external", token)

    def test_a_leading_dotdot_is_a_pointer_NOT_external(self):
        """**This narrowing is why a mode-file bug is findable at all.**

        Treating any `..` segment as `external` skips every explicitly-relative pointer unexamined —
        the dominant way this repo writes a cross-directory pointer. The rule meant to skip *another
        repo* would skip most of *this* one, including a mode's pointer wrong by one level.

        Whether such a token actually escapes depends on the document it was written in, so that half
        of the question is answered in `candidates_for`, not here — see `RelativeResolutionTests`."""
        for token in ("../subagents/store-qa/SKILL.md", "../../persona/persona.default.md",
                      "../state/run-log.md", "../../../seneschal/scripts/proton_send.py"):
            self.assertIsNone(cp.should_skip(token), token)

    def test_real_pointers_are_NOT_skipped(self):
        """The mirror of every rule above — if this goes green while the others do too, the skip rules
        have not swallowed the thing the check exists to find."""
        for token in ("seneschal/scripts/jobs.py", "seneschal/docs/mouth-spec.md",
                      "persona/persona.default.md",
                      "state/jobs/<id>.json", "subagents/reminders/SKILL.md"):
            self.assertIsNone(cp.should_skip(token), token)


class ResolutionTests(unittest.TestCase):
    def test_a_tracked_path_resolves(self):
        root = make_repo({"seneschal/docs/a.md": "see `lib/thing.py`", "lib/thing.py": "x"})
        result = cp.scan(root)
        self.assertEqual(result["stats"]["dangling"], 0)

    def test_a_missing_path_is_reported(self):
        root = make_repo({"seneschal/docs/a.md": "see `lib/gone.py`", "lib/thing.py": "x"})
        result = cp.scan(root)
        self.assertEqual([f["pointer"] for f in result["findings"]], ["lib/gone.py"])

    def test_an_ancestor_relative_pointer_resolves(self):
        """18.1% of real pointers resolve relative to an ancestor of their own document, not to the
        repo root — a doc in `seneschal/docs/` writing `state/x.md` means `seneschal/state/x.md`. The first
        run of this module reported 233 such pointers as dangling before the ancestor walk."""
        root = make_repo({"seneschal/docs/a.md": "see `scripts/thing.py`",
                          "seneschal/scripts/thing.py": "x"})
        self.assertEqual(cp.scan(root)["stats"]["dangling"], 0)

    def test_a_directory_prefix_of_a_tracked_path_resolves(self):
        root = make_repo({"seneschal/docs/a.md": "see `lib/`", "lib/thing.py": "x"})
        self.assertEqual(cp.scan(root)["stats"]["dangling"], 0)

    def test_a_placeholder_must_expand_to_at_least_one_match(self):
        """`<id>` behaves as a glob, and a placeholder expanding to NOTHING is exactly the bug."""
        root = make_repo({"seneschal/docs/a.md": "see `jobs/<id>.json`", "jobs/abc.json": "{}"})
        self.assertEqual(cp.scan(root)["stats"]["dangling"], 0)
        root2 = make_repo({"seneschal/docs/a.md": "see `nope/<id>.json`", "jobs/abc.json": "{}"})
        self.assertEqual(cp.scan(root2)["stats"]["dangling"], 1)

    def test_a_hint_is_offered_only_when_exactly_one_basename_matches(self):
        root = make_repo({"seneschal/docs/a.md": "see `src/persona.ts`", "phone/src/persona.ts": "x"})
        finding = cp.scan(root)["findings"][0]
        self.assertEqual(finding["hint"], "phone/src/persona.ts")


class RelativeResolutionTests(unittest.TestCase):
    """A leading-`../` pointer resolves against its own document and against NOTHING ELSE.

    The motivating bug: mode bodies relocated out of `seneschal/SKILL.md` one directory deeper
    keep their pointers verbatim, so `../subagents/<name>/SKILL.md` means `seneschal/subagents/` —
    which does not exist. A mode dispatches by an IMPERATIVE read of the path it names, so each is
    a mode that cannot load its subagent."""

    def test_a_document_relative_pointer_resolves_from_its_own_directory(self):
        root = make_repo({"seneschal/modes/a.md": "delegate to `../../subagents/x/SKILL.md`",
                          "subagents/x/SKILL.md": "y"})
        self.assertEqual(cp.scan(root)["stats"]["dangling"], 0)

    def test_off_by_one_is_REPORTED_and_the_ancestor_walk_must_not_rescue_it(self):
        """**The single most important case in this file**, and the reason `candidates_for` special-
        cases these tokens instead of just normalising them into the existing ladder.

        The ladder tries every ancestor as a base, which is what takes hundreds of findings down to a
        handful. Let it
        near an explicitly-relative token and it dissolves the check: `../subagents/x` from
        `seneschal/modes/` would resolve through the `seneschal/` ancestor as `seneschal/../subagents/x` =
        `subagents/x` and report CLEAN — the exact pointer that loads nothing. **A pointer wrong by
        exactly one level is invisible to a resolver that tries every level.** If this test ever goes
        green by way of a second candidate appearing, the check has stopped checking."""
        root = make_repo({"seneschal/modes/a.md": "delegate to `../subagents/x/SKILL.md`",
                          "subagents/x/SKILL.md": "y"})
        result = cp.scan(root)
        self.assertEqual([f["pointer"] for f in result["findings"]], ["../subagents/x/SKILL.md"])
        self.assertEqual(result["findings"][0]["candidates"], ["seneschal/subagents/x/SKILL.md"])

    def test_a_declared_base_does_not_apply_to_a_relative_pointer_either(self):
        """`bases` is rung 0's escape hatch, and it is just as fatal here as the ancestor walk: a base
        of `seneschal/` would make `../subagents/x` resolve and the bug vanish. The author wrote the
        traversal; the traversal is the answer."""
        root = make_repo({
            "seneschal/modes/a.md": "delegate to `../subagents/x/SKILL.md`",
            "subagents/x/SKILL.md": "y",
            "seneschal/context-pointers.json": '{"bases": {"seneschal/modes/a.md": ["seneschal/", ""]}}',
        })
        self.assertEqual(cp.scan(root)["stats"]["dangling"], 1)

    def test_a_relative_pointer_that_escapes_the_repo_is_external_after_all(self):
        """`../sibling-repo` is a sibling checkout, and it is external — but only a document at depth 1 can
        say so. Answered where the document is known, and counted as the ruling it is rather than
        reported. `hint_for` never sees it, so no one is ever advised to 'fix' a deliberate escape."""
        root = make_repo({"seneschal/docs/a.md": "the sibling repo `../../../sibling-repo`", "keep.txt": "x"})
        result = cp.scan(root)
        self.assertEqual(result["stats"]["dangling"], 0)
        self.assertEqual(result["stats"]["skipped"].get("external"), 1)

    def test_an_in_repo_relative_pointer_that_misses_is_still_reported(self):
        """The mirror of the case above — escaping is a ruling, not an excuse. One `../` fewer and the
        same token lands inside the repo, where it must be resolved or reported."""
        root = make_repo({"seneschal/docs/a.md": "see `../../sibling-repo`", "keep.txt": "x"})
        self.assertEqual([f["pointer"] for f in cp.scan(root)["findings"]], ["../../sibling-repo"])


def glob_covers(pattern: str, rel: str) -> bool:
    """`**` spans directories, `*` stops at one — `glob.glob(..., recursive=True)` semantics.

    Deliberately NOT `fnmatch`, whose `*` also matches `/`: that is looser than the real scan, so a
    scope test built on it would report coverage the check does not actually have — a green test for
    a file nobody reads."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), rel) is not None


class ScanScopeTests(unittest.TestCase):
    """What the check is pointed AT, pinned — so narrowing the scan is a red test, not a silent hole.

    **This class exists because of a specific bug shape.** A scan that covers the docs and skips
    the instructions (`seneschal/modes/**`, the mode bodies a run dispatches into by an imperative
    read) lets dangling pointers live in full view of a green check. The rules have tests; the
    *scope* needs them too, or the one thing nobody can see is the thing nobody is looking at.

    Asserts against the GLOB LIST, never against file contents. A test that read today's
    `seneschal/modes/ask.md` would go red the next time someone edited it, which is the fastest route to
    a disabled check. This one can only go red when the scan shrinks."""

    #: Every file a run actually executes or routes through. `seneschal/modes/*` are read imperatively on
    #: dispatch; the CLAUDE.md set is every sub-router plus the root.
    MUST_BE_IN_SCOPE = (
        "seneschal/modes/chat.md", "seneschal/modes/brief.md", "seneschal/modes/wrap.md",
        "seneschal/modes/triage.md", "seneschal/modes/ask.md", "seneschal/modes/reminders.md",
        "seneschal/modes/watch.md", "seneschal/modes/dream.md", "seneschal/modes/forge.md",
        "CLAUDE.md", "archons/CLAUDE.md", "cockpit/CLAUDE.md",
        "seneschal/docs/CLAUDE.md", "seneschal/references/CLAUDE.md", "seneschal/scripts/CLAUDE.md",
        "phone/CLAUDE.md",
        "seneschal/SKILL.md", "persona/persona.default.md", "persona/owner-profile.md",
        "subagents/store-qa/SKILL.md",
        "subagents/journal-steward/daily-journal-steward/SKILL.md",
        "seneschal/docs/context-budget-spec.md", "seneschal/references/databases.md",
    )

    def test_every_executed_instruction_file_is_in_scope(self):
        for rel in self.MUST_BE_IN_SCOPE:
            self.assertTrue(any(glob_covers(p, rel) for p in cp.SCAN_GLOBS),
                            f"{rel} is not covered by any SCAN_GLOBS pattern — the scan was narrowed")

    def test_the_glob_helper_is_not_looser_than_the_real_scan(self):
        """Guards the guard. If `glob_covers` matched `/` with a bare `*`, the assertion above would
        pass for files the check never opens, which is worse than having no scope test at all."""
        self.assertTrue(glob_covers("seneschal/modes/**/*.md", "seneschal/modes/chat.md"))
        self.assertTrue(glob_covers("subagents/**/SKILL.md",
                                    "subagents/journal-steward/daily-journal-steward/SKILL.md"))
        self.assertFalse(glob_covers("persona/*.md", "persona/nested/deep.md"))
        self.assertFalse(glob_covers("CLAUDE.md", "seneschal/CLAUDE.md"))

    #: Patterns deliberately in scope BEFORE the directory has any file — so the check is already
    #: pointed at a file type the moment the first one lands, rather than after it goes wrong.
    #: **An entry here owes a fixture test that proves the pattern works** (see
    #: `test_a_prospective_glob_is_proven_by_fixture_not_by_existence` below), because it forfeits
    #: the existence check that covers every other pattern. Delete the entry once a real file exists.
    PROSPECTIVE_GLOBS = (
        # No rule file ships, but the scope entry is in place before the first one: a rule file
        # is budgeted AND checked in the same PR or not created, and a rule nobody has written
        # yet is exactly when that is cheap to guarantee.
        ".claude/rules/**/*.md",
        # The scripts directory's own sub-router: in scope the moment it is written, so detail
        # moved out of the root router into it is pointer-checked from its first commit.
        "seneschal/scripts/CLAUDE.md",
    )

    def test_the_globs_still_resolve_to_real_files_in_this_repo(self):
        """A pattern that matches nothing is a scope entry that silently does nothing. This reads the
        live tree, but only for EXISTENCE — it cannot go red because someone edited a file's prose,
        only because a scanned directory was emptied or renamed out from under the pattern."""
        repo_root = os.path.dirname(os.path.dirname(SCRIPT_DIR))
        for pattern in cp.SCAN_GLOBS:
            if pattern in self.PROSPECTIVE_GLOBS:
                continue
            hits = glob.glob(os.path.join(repo_root, pattern.replace("/", os.sep)), recursive=True)
            self.assertTrue(hits, f"SCAN_GLOBS pattern matches nothing: {pattern}")

    def test_every_prospective_glob_is_still_actually_empty(self):
        """The exemption above is self-deleting, and this is what deletes it. The moment a real file
        matches a prospective pattern, that pattern must graduate to the existence check — otherwise
        the exemption quietly becomes a permanent hole in the one guard `ScanScopeTests` exists to be.
        Going red here means: remove the entry from `PROSPECTIVE_GLOBS`, nothing else."""
        repo_root = os.path.dirname(os.path.dirname(SCRIPT_DIR))
        for pattern in self.PROSPECTIVE_GLOBS:
            hits = glob.glob(os.path.join(repo_root, pattern.replace("/", os.sep)), recursive=True)
            self.assertFalse(hits, f"{pattern} now matches {hits!r} — move it out of "
                                   f"PROSPECTIVE_GLOBS and let the existence check cover it")

    def test_a_prospective_glob_is_proven_by_fixture_not_by_existence(self):
        """What the existence check would have bought, bought a better way: a rule file in a fixture
        repo IS opened, and its dangling pointer IS reported. This answers the question the existence
        check only proxies for — *does this scope entry do anything* — and it keeps answering it after
        a real rule file lands."""
        root = make_repo({
            "CLAUDE.md": "root",
            ".claude/rules/archive-cluster.md":
                "---\npaths: [\"seneschal/scripts/archive_*.py\"]\n---\n"
                "See `seneschal/scripts/ARCHIVE_SETUP.md` and `seneschal/scripts/never-existed.md`.\n",
            "seneschal/scripts/ARCHIVE_SETUP.md": "real",
        })
        findings = cp.scan(root)["findings"]
        self.assertEqual([f["pointer"] for f in findings], ["seneschal/scripts/never-existed.md"])
        self.assertEqual(findings[0]["file"], ".claude/rules/archive-cluster.md")

    def test_the_scripts_sub_router_is_scanned_by_fixture(self):
        """`seneschal/scripts/CLAUDE.md` is prospective: proven here, not by existence."""
        root = make_repo({
            "CLAUDE.md": "root",
            "seneschal/scripts/CLAUDE.md": "See `archive/keep.py` and `archive/gone.py`.\n",
            "seneschal/scripts/archive/keep.py": "x",
        })
        findings = cp.scan(root)["findings"]
        self.assertEqual([f["pointer"] for f in findings], ["archive/gone.py"])

    def test_a_rule_file_pointer_is_resolved_from_the_REPO_ROOT_not_its_own_directory(self):
        """Why a rule file must write repo-root-relative paths, asserted rather than advised.

        The ancestor walk in `candidates_for` is what lets a doc in `seneschal/docs/` write
        `scripts/foo.py` and mean `seneschal/scripts/foo.py` — a large share of this tree's pointers
        resolve that way. A rule file's ancestors are `.claude/rules/` and `.claude/`, so it inherits **none** of
        that: the same shorthand dangles, and only the repo-root rung ever fires."""
        root = make_repo({
            "CLAUDE.md": "root",
            ".claude/rules/r.md": "`seneschal/scripts/keep.py` resolves; `scripts/keep.py` does not.",
            "seneschal/scripts/keep.py": "x",
        })
        findings = cp.scan(root)["findings"]
        self.assertEqual([f["pointer"] for f in findings], ["scripts/keep.py"])
        # And the hint names the fix, because this is a mistake a writer will actually make.
        self.assertEqual(findings[0]["hint"], "seneschal/scripts/keep.py")


class OracleTests(unittest.TestCase):
    """The two under-reporting traps, against a real git repo. A mock cannot show either."""

    def test_a_gitignored_but_real_path_resolves_on_rung_2(self):
        """The declaration already exists, beside the thing it describes. `.gitignore` says
        `seneschal/state/*`; `state/run-log.md` exists on the daemon's box and in no clone."""
        root = make_repo({"seneschal/docs/a.md": "see `state/run-log.md`", "keep.txt": "x"},
                         gitignore="state/*\n")
        self.assertEqual(cp.scan(root)["stats"]["dangling"], 0)
        self.assertEqual(cp.scan(root)["stats"]["ignored"], 1)

    def test_trap_1_crlf_does_not_swallow_entries(self):
        """In text mode the pipe turns \\n into \\r\\n, git takes the \\r as part of the pathname and
        DROPS other entries. All three must come back."""
        root = make_repo({"keep.txt": "x"}, gitignore="state/*\nlogs/*\ntmp/*\n")
        got = cp.check_ignored(root, ["state/a.md", "logs/b.log", "tmp/c.json"])
        self.assertEqual(got, {"state/a.md", "logs/b.log", "tmp/c.json"})

    def test_trap_2_a_malformed_path_would_abort_the_batch_so_it_is_filtered_first(self):
        """Feeding `/api/status` exits 128 and everything after it is silently lost, while the output
        produced before it looks like a normal success."""
        root = make_repo({"keep.txt": "x"}, gitignore="state/*\n")
        got = cp.check_ignored(root, ["/api/status", "state/a.md", "../outside"])
        self.assertEqual(got, {"state/a.md"})

    def test_an_oracle_failure_raises_rather_than_reporting_a_clean_tree(self):
        """A broken oracle is NOT a clean tree. Degrading to 'nothing is ignored' would convert every
        runtime path into a false dangling finding — 264 of them, in the prototype."""
        with self.assertRaises(cp.OracleFailure):
            cp.check_ignored(tempfile.mkdtemp(), ["state/a.md"])  # not a git repo at all


def make_repo_bytes(files: dict, gitignore: bytes | None = None) -> str:
    """`make_repo`, but ignore-file content is written as RAW BYTES.

    A line-terminator test cannot go through a text handle: Python would translate `\\n` on write and
    the fixture would silently become the thing it is supposed to contrast with."""
    root = tempfile.mkdtemp()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    # The trap is a property of the WORKING TREE, so neutralise any inherited autocrlf: the fixture
    # must be whatever these bytes say it is, on a Windows dev box and on Linux CI alike.
    _git(root, "config", "core.autocrlf", "false")
    for rel, body in files.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    if gitignore is not None:
        with open(os.path.join(root, ".gitignore"), "wb") as fh:
            fh.write(gitignore)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fixture")
    return root


class CrlfPhantomPatternTests(unittest.TestCase):
    """Trap 3 — the trap that made the oracle over-report, so a green check was looking at nothing.

    A `.gitignore` with CRLF terminators loses its `\\r` when git parses it, which leaves a blank
    line as the **empty pattern** rather than a skipped one; the empty pattern matches any path
    spelled with a trailing slash. Every bare-directory pointer then resolved on rung 2.

    These run against fixture repos, so they hold on Linux CI where the live `.gitignore` is LF and
    the bug is invisible — which is the whole point. The trap was a *checkout* property, and a test
    that could only reproduce it on one host would be a test that never ran where it mattered."""

    CRLF_IGNORE = b"state/\r\n\r\n*.log\r\n"
    LF_IGNORE = b"state/\n\n*.log\n"

    def test_an_invented_directory_does_NOT_resolve_against_a_crlf_blank_line(self):
        """THE REGRESSION. `ghost/` is declared nowhere; before the fix it came back ignored."""
        root = make_repo_bytes({"keep.txt": "x"}, gitignore=self.CRLF_IGNORE)
        got = cp.check_ignored(root, ["ghost/", "state/real.md", "deep/ghost/sub/"])
        self.assertEqual(got, {"state/real.md"})

    def test_the_raw_oracle_IS_still_fooled_so_the_filter_is_not_decoration(self):
        """Pins that the fixture actually reproduces the trap. Without this, the test above could
        pass because git changed behaviour and the filter had become dead code — a guard that has
        quietly stopped guarding is worse than no guard, because it still reads as coverage.

        Skips rather than fails if a future git stops registering the empty pattern: that would be
        the trap being FIXED upstream, which is good news and must not read as a regression here."""
        root = make_repo_bytes({"keep.txt": "x"}, gitignore=self.CRLF_IGNORE)
        raw = subprocess.run(["git", "check-ignore", "-v", "--stdin", "-z"],
                             cwd=root, input=b"ghost/\0", capture_output=True)
        fields = raw.stdout.decode().split("\0")
        if len(fields) < 4 or fields[2] != "":
            self.skipTest(f"this git ({raw.stdout!r}) does not reproduce the empty-pattern match")
        self.assertEqual(cp.ignored_from_verbose(raw.stdout), set(),
                         "the empty-pattern record must be filtered out")

    def test_an_lf_ignore_file_with_a_blank_line_is_unaffected_either_way(self):
        """The control. A blank line in an LF file was never a match, so the fix must be a no-op
        here — if this and the CRLF case ever disagree, the filter is doing more than it claims."""
        root = make_repo_bytes({"keep.txt": "x"}, gitignore=self.LF_IGNORE)
        self.assertEqual(cp.check_ignored(root, ["ghost/", "state/real.md"]), {"state/real.md"})

    def test_a_NEGATED_path_is_not_reported_ignored_because_minus_v_WIDENS_the_output(self):
        """The trap inside the fix, and the reason `-v` is not a free swap.

        Plain `check-ignore` prints only ignored paths. `-v` prints the final matching pattern even
        when it is a NEGATION, and exits 0 doing it — so `keep.log` under `*.log` + `!keep.log`
        appears in `-v` output and not in plain output. Reading "a record came back" as "ignored"
        would have swapped the phantom for a fresh false positive, and this repo's `.gitignore`
        carries ten-plus negations, so it would have fired immediately."""
        root = make_repo_bytes({"keep.txt": "x"}, gitignore=b"*.log\n!keep.log\n")
        got = cp.check_ignored(root, ["drop.log", "keep.log"])
        self.assertEqual(got, {"drop.log"})

    def test_the_verbose_parser_drops_empty_and_negated_patterns_and_nothing_else(self):
        """The filter as a unit, with no git in the way, so a framing change is legible as itself."""
        payload = (b".gitignore\x002\x00\x00ghost/\x00"          # empty pattern -> the phantom
                   b".gitignore\x001\x00state/\x00state/a.md\x00"  # a real rule -> kept
                   b".gitignore\x003\x00!keep.log\x00keep.log\x00")  # a negation -> not ignored
        self.assertEqual(cp.ignored_from_verbose(payload), {"state/a.md"})

    def test_empty_output_is_an_empty_set_not_a_crash(self):
        self.assertEqual(cp.ignored_from_verbose(b""), set())

    def test_unexpected_field_framing_raises_rather_than_guessing(self):
        """Same posture as the oracle guard: a shape we do not understand must abort loudly, never
        degrade into a confident wrong answer."""
        with self.assertRaises(cp.OracleFailure):
            cp.ignored_from_verbose(b".gitignore\x002\x00only-three-fields\x00")


class CommittedIgnoreFilesAreLfTests(unittest.TestCase):
    """The other half of trap 3: a CR must never reach the SHARED object.

    `.gitattributes` pins these to LF on checkout, but that only binds checkouts made after it
    landed. This is the ratchet that says so out loud — if a CRLF `.gitignore` is ever committed,
    every clone everywhere inherits the phantom and no local setting can undo it.

    `crlf_ignore_files` is unit-tested against fixtures below; the live-repo assertion after it is a
    deliberate, narrow exception to the fixtures-not-the-live-repo rule. That rule exists so a
    check does not go red when someone edits `CLAUDE.md`. This asserts an invariant that should hold
    forever and whose entire purpose is to go red when it stops holding — the opposite case."""

    def test_a_committed_crlf_ignore_file_is_detected(self):
        root = make_repo_bytes({"keep.txt": "x"}, gitignore=b"state/\r\n\r\n*.log\r\n")
        self.assertEqual(cp.crlf_ignore_files(root), [".gitignore"])

    def test_a_committed_lf_ignore_file_is_clean(self):
        root = make_repo_bytes({"keep.txt": "x"}, gitignore=b"state/\n\n*.log\n")
        self.assertEqual(cp.crlf_ignore_files(root), [])

    def test_a_nested_ignore_file_is_reached_too(self):
        """`archons/*/.gitignore` and friends are as load-bearing as the root one, and rung 2 reads
        all of them — so a scan that only looked at the root would miss most of the surface."""
        root = make_repo_bytes({"sub/keep.txt": "x"}, gitignore=b"state/\n")
        with open(os.path.join(root, "sub", ".gitignore"), "wb") as fh:
            fh.write(b"out/\r\n\r\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "nested")
        self.assertEqual(cp.crlf_ignore_files(root), ["sub/.gitignore"])

    def test_a_non_git_directory_is_empty_not_an_error(self):
        """A hygiene probe may never be the thing that fails a run for an unrelated reason."""
        self.assertEqual(cp.crlf_ignore_files(tempfile.mkdtemp()), [])

    def test_THIS_REPO_commits_no_crlf_ignore_file(self):
        """The live ratchet. Reads the committed BLOB, so it gives the same verdict on a Windows
        checkout with `core.autocrlf=true` as on Linux CI — the worktree is allowed to be CRLF."""
        repo_root = os.path.dirname(os.path.dirname(SCRIPT_DIR))
        # `exists`, NOT `isdir`: in a git WORKTREE `.git` is a FILE pointing at the real gitdir, and
        # worktrees are where every delegated job runs — an isdir check would skip this exactly where
        # it most needs to run, while still passing on CI, which is the shape of a guard that isn't.
        if not os.path.exists(os.path.join(repo_root, ".git")):
            self.skipTest("not a git checkout")
        offenders = cp.crlf_ignore_files(repo_root)
        self.assertEqual(offenders, [], f"CRLF committed in: {offenders} — see trap 3")


class TrailingSlashTests(unittest.TestCase):
    """`normpath` drops a trailing slash, and a `dir/` .gitignore rule only matches a DIRECTORY — so a
    relative pointer to a declared-runtime directory must keep the author's slash through
    normalisation, or its declaration is lost and it reads as dangling."""

    def test_resolve_relative_keeps_the_trailing_slash(self):
        self.assertEqual(cp.resolve_relative("../../archons/stable/", "seneschal/modes/forge.md"),
                         "archons/stable/")
        self.assertEqual(cp.resolve_relative("../x.md", "seneschal/modes/a.md"), "seneschal/x.md")

    def test_a_relative_pointer_to_a_gitignored_directory_resolves_on_rung_2(self):
        root = make_repo({"seneschal/modes/forge.md": "write under `../../archons/stable/`",
                          "keep.txt": "x"}, gitignore="archons/stable/\n")
        result = cp.scan(root)
        self.assertEqual(result["stats"]["dangling"], 0)
        self.assertEqual(result["stats"]["ignored"], 1)


class StaleAllowlistTests(unittest.TestCase):
    """An allowlist row whose pointer has started resolving on its own (a forward pointer whose
    target landed) is NAMED so it gets deleted — and is never a finding."""

    def _repo(self, with_target: bool) -> str:
        files = {
            "seneschal/docs/a.md": "see `seneschal/scripts/later.py`",
            "seneschal/context-pointers.json":
                '{"allow": [{"pointer": "seneschal/scripts/later.py", "reason": "lands later"}]}',
            "keep.txt": "x",
        }
        if with_target:
            files["seneschal/scripts/later.py"] = "x"
        return make_repo(files)

    def test_a_still_needed_row_is_not_stale(self):
        result = cp.scan(self._repo(with_target=False))
        self.assertEqual(result["stats"]["allowlisted"], 1)
        self.assertEqual(result["stale_allow"], [])

    def test_a_row_that_now_resolves_is_named_but_is_not_a_finding(self):
        root = self._repo(with_target=True)
        result = cp.scan(root)
        self.assertEqual(result["stale_allow"], ["seneschal/scripts/later.py"])
        self.assertEqual(result["stats"]["dangling"], 0)
        self.assertIn("now resolve on their own", cp.render(result))
        self.assertEqual(cp.main(["--root", root, "--enforce"]), 0)


class ReportOnlyTests(unittest.TestCase):
    """Without --enforce this cannot fail a build; CI passes --enforce."""

    def setUp(self):
        self.root = make_repo({"seneschal/docs/a.md": "see `lib/gone.py`", "lib/thing.py": "x"})

    def test_findings_still_exit_zero_without_enforce(self):
        self.assertEqual(cp.main(["--root", self.root]), 0)

    def test_enforce_flips_it(self):
        self.assertEqual(cp.main(["--root", self.root, "--enforce"]), 1)

    def test_a_clean_tree_exits_zero_either_way(self):
        clean = make_repo({"seneschal/docs/a.md": "see `lib/thing.py`", "lib/thing.py": "x"})
        self.assertEqual(cp.main(["--root", clean]), 0)
        self.assertEqual(cp.main(["--root", clean, "--enforce"]), 0)



class WorkingTreeTests(unittest.TestCase):
    """Local gates see the working tree: rung 1 is the working tree's answer, not the index's.
    Otherwise an un-added `test_x.py` makes a pointer to it read as dangling locally, and an
    un-added `.md` is not scanned at all."""

    def test_a_pointer_to_an_untracked_new_file_resolves(self):
        root = make_repo({"seneschal/docs/a.md": "see `lib/thing.py`", "lib/thing.py": "x"})
        with open(os.path.join(root, "seneschal", "docs", "a.md"), "w", encoding="utf-8") as fh:
            fh.write("see `lib/thing.py` and `seneschal/scripts/test_new.py`")
        os.makedirs(os.path.join(root, "seneschal", "scripts"), exist_ok=True)
        with open(os.path.join(root, "seneschal", "scripts", "test_new.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        # Neither the edit nor the new file is added or committed.
        result = cp.scan(root)
        self.assertEqual(result["stats"]["dangling"], 0, result["findings"])

    def test_an_untracked_new_doc_is_scanned_and_its_dangling_pointer_fires(self):
        root = make_repo({"seneschal/docs/a.md": "see `lib/thing.py`", "lib/thing.py": "x"})
        with open(os.path.join(root, "seneschal", "docs", "new.md"), "w", encoding="utf-8") as fh:
            fh.write("see `lib/gone.py`")
        result = cp.scan(root)
        self.assertEqual([f["pointer"] for f in result["findings"]], ["lib/gone.py"])
        self.assertEqual(result["findings"][0]["file"], "seneschal/docs/new.md")

    def test_a_gitignored_new_file_is_still_not_rung_1(self):
        """`--exclude-standard` keeps the runtime cache out: a pointer to an ignored path is rung 2's
        business (declared runtime), exactly as before."""
        root = make_repo({"seneschal/docs/a.md": "see `state/run-log.md`", "lib/thing.py": "x"},
                         gitignore="state/\n")
        os.makedirs(os.path.join(root, "state"), exist_ok=True)
        with open(os.path.join(root, "state", "run-log.md"), "w", encoding="utf-8") as fh:
            fh.write("log")
        result = cp.scan(root)
        self.assertEqual(result["stats"]["dangling"], 0)
        self.assertEqual(result["stats"]["ignored"], 1)
        self.assertEqual(result["stats"]["tracked"], 0)


if __name__ == "__main__":
    unittest.main()
