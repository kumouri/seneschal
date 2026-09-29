#!/usr/bin/env python3
"""Tests for ``branch_delete_guard`` — the `PreToolUse` hook that refuses to delete a remote branch
an open pull request is stacked on.

Five classes here are not ordinary unit tests and must not be relaxed into passing:

* **``TheCaseThatBitTest``** replays the failure — a merge with ``--delete-branch``, and a bare
  ref deletion — with a PR stacked on the deleted branch. If it goes green on "allowed", the guard is not guarding the only thing it was
  written for. Its control — a merged branch with no dependents — is in the same class deliberately:
  a guard that refuses both is a guard that gets uninstalled by the end of the week, so the two
  assertions are read together or not at all.
* **``FailsClosedOnAParseTest``** and **``FailsOpenOnlyWhenGitHubIsUnreachableTest``** are the two
  halves of the guard's asymmetry and they assert **opposite exit codes on adjacent inputs**. If
  they ever agree, one of them is wrong. The second one additionally pins that the fail-open path is
  :data:`branch_delete_guard.EXIT_ADVISORY` and never :data:`~branch_delete_guard.EXIT_ALLOW` — an
  outage that allows *silently* is indistinguishable from a clean verdict, which is the whole reason
  there are three exit codes here instead of two.
* **``SelfImmunityTest``** is `bash_path_guard`'s `test_the_message_does_not_match_its_own_rule` in
  new clothes. The refusal text, the module's own docstring and this test file all contain the
  command spellings the guard refuses; a guard that blocks the commands used to write about it is a
  guard somebody turns off.
* **``ThePredicateHasOneImplementationTest``** pins that `branch_sweep` calls
  ``branch_delete_guard.open_prs_based_on`` and defines no ``gh pr list --base`` of its own. This is
  checked against the FILES rather than the import graph, because the property being defended is
  *"there is no second copy"* — the same shape as `test_merge_guard`'s
  ``ApprovalIsNotAgentMintableTest``. A sweep whose idea of "safe" can drift from the hook's is the
  bug the sweep exists to avoid, automated.

**No test may reach the network.** Every `gh` and `git` call goes through the ``runner=`` seam, and
``NoRealSubprocessTest`` asserts that the seam is the only door by driving the whole decision with a
runner that raises if it is ever asked for something the test did not stub.

Run:  python -m unittest test_branch_delete_guard   (from seneschal/scripts)
"""
import json
import os
import subprocess
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import branch_delete_guard as g  # noqa: E402
import merge_guard as mg  # noqa: E402


# --------------------------------------------------------------------------- the stub

class FakeGh:
    """A `gh`/`git` stand-in driven by tables. Anything not in a table is a test bug and raises,
    so a stub can never quietly answer a question the test did not think about."""

    def __init__(self, based_on=None, heads=None, remotes=None, gh_fail=None):
        self.based_on = based_on or {}        # branch -> [pr dicts]
        self.heads = heads or {}              # pr number -> head branch
        self.remotes = remotes or {"origin": "https://github.com/example/repo.git"}
        #: How the `gh` half fails: `None` | `(rc, stderr)` | an exception to raise. It is
        #: deliberately NOT applied to `git remote get-url`, which is a local call that cannot
        #: suffer a network outage -- failing both would test the wrong seam and would let an
        #: outage test pass on the remote-resolution refusal instead.
        self.gh_fail = gh_fail
        self.calls = []

    def __call__(self, argv, cwd=None, timeout=None):
        self.calls.append(list(argv))
        if argv and argv[0] == "gh" and self.gh_fail is not None:
            if isinstance(self.gh_fail, BaseException):
                raise self.gh_fail
            return self.gh_fail[0], "", self.gh_fail[1]
        if argv[:2] == ["git", "remote"]:
            remote = argv[-1]
            if remote not in self.remotes:
                return 1, "", f"error: No such remote '{remote}'"
            return 0, self.remotes[remote] + "\n", ""
        if argv[:3] == ["gh", "pr", "list"]:
            branch = argv[argv.index("--base") + 1]
            return 0, json.dumps(self.based_on.get(branch, [])), ""
        if argv[:3] == ["gh", "pr", "view"]:
            number = int(argv[3])
            if number not in self.heads:
                return 1, "", f"no pull request found for {number}"
            return 0, json.dumps({"headRefName": self.heads[number]}), ""
        raise AssertionError(f"unstubbed call: {argv!r}")


def pr(number, head="feat/x", title="a stacked change"):
    return {"number": number, "title": title, "headRefName": head,
            "url": f"https://github.com/example/repo/pull/{number}"}


def event(command, tool="Bash", cwd=None):
    return {"tool_name": tool, "tool_input": {"command": command}, "cwd": cwd}


def run(command, fake, cwd=None):
    """Drive the whole decision through the seam."""
    return g.evaluate(command, cwd=cwd, runner=fake)


# --------------------------------------------------------------------------- the incidents

class TheCaseThatBitTest(unittest.TestCase):
    """The failure, replayed both ways, plus the control that keeps the guard usable."""

    def test_merging_566_with_delete_branch_is_refused_because_565_is_stacked(self):
        fake = FakeGh(heads={566: "docs/some-spec"},
                      based_on={"docs/some-spec": [
                          pr(565, "feat/stacked-change",
                             "feat(x): a change stacked on the spec")]})
        code, message = run("gh pr merge 566 --merge --delete-branch", fake)
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertIn("#565", message)
        self.assertIn("docs/some-spec", message)
        self.assertIn("feat/stacked-change", message)

    def test_deleting_471s_head_is_refused_because_472_is_stacked(self):
        fake = FakeGh(based_on={"feat/lower-layer": [
            pr(472, "feat/upper-layer", "the upper layer of a stack")]})
        code, message = run("git push origin --delete feat/lower-layer", fake)
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertIn("#472", message)

    def test_the_control_a_merged_branch_with_no_dependents_is_ALLOWED(self):
        """The other half, and it is not optional. Most merged branches have nothing stacked on
        them, and a guard that refuses those too is a guard nobody keeps."""
        fake = FakeGh(based_on={})
        for command in ("git push origin --delete docs/some-spec",
                        "git push origin :feat/stacked-change",
                        "gh api repos/example/repo"
                        "/git/refs/heads/docs/some-spec -X DELETE"):
            with self.subTest(command=command):
                code, message = run(command, fake)
                self.assertEqual(code, g.EXIT_ALLOW, message)
                self.assertEqual(message, "")

    def test_the_refusal_names_every_stacked_pr_not_just_a_count(self):
        fake = FakeGh(based_on={"base/branch": [pr(1, "a"), pr(2, "b"), pr(3, "c")]})
        code, message = run("git push origin --delete base/branch", fake)
        self.assertEqual(code, g.EXIT_BLOCK)
        for number in ("#1", "#2", "#3"):
            self.assertIn(number, message)
        self.assertIn("gh pr edit", message)  # and it says what to do instead


# --------------------------------------------------------------------------- detection

class CommandSpellingTest(unittest.TestCase):
    """Coverage of the ACT, not of one string — `test_merge_guard.CommandSpellingTest`'s shape.
    Every row here is a spelling that reaches the same remote-ref deletion."""

    MERGE_DELETES = (
        "gh pr merge 566 --merge --delete-branch",
        "gh pr merge 566 --delete-branch --merge",
        "gh pr merge 566 -d --squash",
        "gh pr merge 566 --merge --delete-branch=true",
        "gh.exe pr merge 566 --delete-branch",
        '"/c/Program Files/GitHub CLI/gh.exe" pr merge 566 --delete-branch',
        "bash -c 'gh pr merge 566 --delete-branch'",
        "pwsh -Command \"gh pr merge 566 --delete-branch\"",
        "git status && gh pr merge 566 --delete-branch",
        "gh pr merge https://github.com/example/repo/pull/566 -d",
        "gh pr merge 566 --delete-branch 2>&1",
    )
    REF_DELETES = (
        "gh api repos/o/r/git/refs/heads/x -X DELETE",
        "gh api -X DELETE repos/o/r/git/refs/heads/x",
        "gh api -XDELETE repos/o/r/git/refs/heads/x",
        "gh api --method DELETE repos/o/r/git/refs/heads/x",
        "gh api --method=DELETE repos/o/r/git/refs/heads/x",
        "gh api https://api.github.com/repos/o/r/git/refs/heads/deep/nested/name -X DELETE",
        "git push origin --delete x",
        "git push origin -d x",
        "git push --delete origin x",
        "git push origin :x",
        "git push origin :refs/heads/x",
        "git push origin --delete refs/heads/x",
        "git push origin --delete a b c",
        "git push --force-with-lease origin --delete x",
    )

    def test_every_merge_spelling_is_detected(self):
        for command in self.MERGE_DELETES:
            with self.subTest(command=command):
                self.assertTrue(g.looks_like_deletion(command))

    def test_every_ref_deletion_spelling_is_detected(self):
        for command in self.REF_DELETES:
            with self.subTest(command=command):
                self.assertTrue(g.looks_like_deletion(command))

    def test_a_deep_branch_name_survives_the_path(self):
        found = g.deletion_invocations(
            "gh api https://api.github.com/repos/o/r/git/refs/heads/deep/nested/name -X DELETE")
        self.assertEqual(found[0]["branch"], "deep/nested/name")
        self.assertEqual(found[0]["repo"], "o/r")

    def test_refs_heads_prefix_is_stripped_so_both_spellings_reach_one_predicate(self):
        for command in ("git push origin :refs/heads/x", "git push origin --delete refs/heads/x"):
            with self.subTest(command=command):
                self.assertEqual(g.deletion_invocations(command)[0]["branch"], "x")

    def test_multiple_branches_in_one_push_are_all_found(self):
        found = g.deletion_invocations("git push origin --delete a b c")
        self.assertEqual([f["branch"] for f in found], ["a", "b", "c"])

    def test_a_shell_REDIRECTION_is_not_a_branch_name(self):
        """`merge_guard.strip_redirections`' measurement, in the second place it applies.
        `shlex` lexes words, not shell grammar, so `2>&1` arrives as an ordinary token and lands
        among the refspecs — and the guard would then run its predicate against a branch called
        `2>&1`, which at best is a wasted API call and at worst refuses a correct command on
        whatever `gh` says about a branch name that cannot exist. A guard that blocks correct
        commands is one everybody learns to route around, which is the whole of its value."""
        for command, expected in (
                ("git push origin --delete x 2>&1", ["x"]),
                ("git push origin --delete x >out.log", ["x"]),
                ("git push origin --delete x > out.log", ["x"]),
                ("git push origin :x 2>/dev/null", ["x"]),
                ("git push origin --delete a b 2>&1", ["a", "b"])):
            with self.subTest(command=command):
                found = g.deletion_invocations(command)
                self.assertEqual([f["branch"] for f in found], expected)

    def test_a_redirect_between_two_chained_pushes_hides_neither(self):
        chained = " && ".join(["git push origin --delete a 2>&1",
                               "git push origin --delete b"])
        self.assertEqual([f["branch"] for f in g.deletion_invocations(chained)], ["a", "b"])


class NotADeletionTest(unittest.TestCase):
    """The necessary other half of :class:`CommandSpellingTest`. Every row must pass untouched."""

    LEAVE_ALONE = (
        "gh pr merge 566 --merge",
        "gh pr merge 566 --merge --delete-branch=false",
        "gh pr merge 566 --squash --delete-branch=0",
        "gh api repos/o/r/git/refs/heads/x",
        "gh api repos/o/r/git/refs/heads/x -X GET",
        "gh pr list --state open --base develop --json number",
        "git push origin feat/x",
        "git push origin HEAD:feat/x",
        "git push --force origin feat/x",
        "git push",
        "git branch -d localthing",
        "git branch -D localthing",
        "git branch --delete localthing",
        "grep 'git push origin --delete x' notes.md",
        "echo git push origin --delete x",
        "cat docs/BRANCH_DELETE_GUARD_SETUP.md",
        "ls -la",
        "git merge develop",
        "git status",
    )

    def test_none_of_these_are_branch_deletions(self):
        for command in self.LEAVE_ALONE:
            with self.subTest(command=command):
                self.assertFalse(g.looks_like_deletion(command))

    def test_a_local_branch_deletion_cannot_close_a_pull_request_so_it_is_untouched(self):
        """Stated as its own test because it is a deliberate scope decision rather than an
        oversight: `git branch -d` removes a local ref and GitHub never hears about it."""
        fake = FakeGh(based_on={"feat/x": [pr(1)]})
        self.assertEqual(run("git branch -D feat/x", fake)[0], g.EXIT_ALLOW)
        self.assertEqual(fake.calls, [])  # and it costs no API call either


class SelfImmunityTest(unittest.TestCase):
    """A guard that refuses the commands used to write about it gets uninstalled."""

    def test_the_refusal_message_is_not_itself_a_branch_deletion(self):
        message = g.refusal("some/branch", [pr(1)], "a test")
        self.assertFalse(g.looks_like_deletion(message))

    def test_the_protected_refusal_is_not_itself_a_branch_deletion(self):
        self.assertFalse(g.looks_like_deletion(g.protected_refusal("develop", "a test")))

    def test_the_modules_own_docstring_does_not_trip_the_guard(self):
        for module in (g, __import__("branch_sweep")):
            with self.subTest(module=module.__name__):
                self.assertFalse(g.looks_like_deletion(module.__doc__ or ""))

    def test_this_test_file_does_not_trip_the_guard_when_read_as_a_command(self):
        with open(os.path.join(SCRIPT_DIR, "test_branch_delete_guard.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        # The spellings live inside string literals, never at command position.
        self.assertFalse(g.looks_like_deletion(body))


# --------------------------------------------------------------------------- the polarities

class FailsClosedOnAParseTest(unittest.TestCase):
    """"Cannot tell" is never "allow". Each row identified a deletion and could not read it."""

    UNREADABLE = (
        "git push --some-new-flag origin --delete x",
        "gh pr merge 566 --delete-branch=perhaps",
        "gh api graphql -f query='mutation { deleteRef(input:{refId:\"R\"}) { __typename } }'",
        "gh api -X DELETE /some/other/endpoint",
    )

    def test_each_unreadable_deletion_blocks(self):
        fake = FakeGh()
        for command in self.UNREADABLE:
            with self.subTest(command=command):
                code, message = run(command, fake)
                self.assertEqual(code, g.EXIT_BLOCK, message)
                self.assertTrue(message.startswith("Blocked:"), message)

    def test_an_unrecognised_gh_pr_merge_flag_blocks_rather_than_reading_the_wrong_token(self):
        code, message = run("gh pr merge --brand-new-flag 566 --delete-branch", FakeGh())
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertIn("unrecognised flag", message)

    def test_a_remote_that_cannot_be_resolved_blocks(self):
        fake = FakeGh(remotes={})  # `git remote get-url` fails
        code, message = run("git push nowhere --delete x", fake)
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertIn("refuses", message)

    def test_gh_missing_blocks_and_says_it_is_not_an_outage(self):
        fake = FakeGh(gh_fail=FileNotFoundError("gh"))
        code, message = run("gh api repos/o/r/git/refs/heads/x -X DELETE", fake)
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertIn("NOT GitHub being unreachable", message)

    def test_an_unauthenticated_gh_blocks_and_is_not_read_as_an_outage(self):
        fake = FakeGh(gh_fail=(1, "gh: To use GitHub CLI in a GitHub Actions workflow, "
                                  "you must run gh auth login"))
        code, message = run("git push origin --delete x", fake)
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertIn("not authenticated", message)

    def test_json_that_is_not_a_list_blocks_rather_than_reading_as_no_dependents(self):
        code, message = g.evaluate("git push origin --delete x", cwd=None,
                                   runner=_scripted({("git", "remote"): (0, "git@github.com:o/r.git", ""),
                                                     ("gh", "pr"): (0, '{"not":"a list"}', "")}))
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertIn("not a list", message)

    def test_output_that_is_not_json_at_all_blocks(self):
        code, message = g.evaluate("git push origin --delete x", cwd=None,
                                   runner=_scripted({("git", "remote"): (0, "git@github.com:o/r.git", ""),
                                                     ("gh", "pr"): (0, "<html>rate limited</html>", "")}))
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertIn("not JSON", message)

    def test_a_raise_inside_the_decision_blocks(self):
        """`hook_main`'s inner wrapper. Stage 1's unknowns allow; a raise once stage 2 is running
        is an unknown about a deletion, and those refuse."""
        code = g.hook_main(stdin=_Stdin(json.dumps({"tool_name": "Bash"})), stderr=_Sink())
        self.assertEqual(code, g.EXIT_ALLOW)  # a payload with no command is not a deletion
        original = g.decide
        try:
            g.decide = lambda event: (_ for _ in ()).throw(RuntimeError("boom"))
            sink = _Sink()
            code = g.hook_main(stdin=_Stdin(json.dumps(event("git push origin --delete x"))),
                               stderr=sink)
        finally:
            g.decide = original
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertIn("refuses rather than guess", sink.text)


class FailsOpenOnlyWhenGitHubIsUnreachableTest(unittest.TestCase):
    """The opposite polarity, on adjacent inputs. If this class and the one above ever agree on a
    row, one of them is wrong."""

    OUTAGES = (
        (1, 'Get "https://api.github.com/graphql": dial tcp: lookup api.github.com: no such host'),
        (1, "error connecting to api.github.com: connection refused"),
        (1, "context deadline exceeded (Client.Timeout exceeded)"),
        (1, "HTTP 503: Service Unavailable"),
        (1, "502 Bad Gateway"),
    )

    def test_each_outage_allows_with_the_advisory_code_not_silently(self):
        for rc, stderr in self.OUTAGES:
            with self.subTest(stderr=stderr):
                fake = FakeGh(gh_fail=(rc, stderr))
                code, message = run("git push origin --delete x", fake)
                self.assertEqual(code, g.EXIT_ADVISORY)
                self.assertNotEqual(code, g.EXIT_ALLOW)
                self.assertNotEqual(code, g.EXIT_BLOCK)
                self.assertIn("ALLOWED WITHOUT CHECKING", message)

    def test_a_timeout_is_an_outage(self):
        fake = FakeGh(gh_fail=subprocess.TimeoutExpired(cmd="gh", timeout=30))
        code, message = run("gh api repos/o/r/git/refs/heads/x -X DELETE", fake)
        self.assertEqual(code, g.EXIT_ADVISORY)

    def test_the_advisory_says_which_of_the_two_happened(self):
        """"Allowed because offline" and "allowed because safe" are the same silence otherwise."""
        fake = FakeGh(gh_fail=(1, "dial tcp: no such host"))
        _, message = run("git push origin --delete x", fake)
        self.assertIn("GitHub could not be reached", message)
        self.assertIn("not a clean verdict", message)
        clean_code, clean_message = run("git push origin --delete x", FakeGh(based_on={}))
        self.assertEqual(clean_code, g.EXIT_ALLOW)
        self.assertEqual(clean_message, "")

    def test_not_a_github_repository_allows_SILENTLY_because_it_is_a_correct_negative(self):
        """Outcome 4. No GitHub repo means no GitHub pull request means nothing can be closed —
        so this is not the fail-open path and must not print an advisory."""
        fake = FakeGh(remotes={"origin": "git@gitlab.com:someone/thing.git"})
        code, message = run("git push origin --delete x", fake)
        self.assertEqual(code, g.EXIT_ALLOW)
        self.assertEqual(message, "")

    def test_gh_reporting_no_github_remote_also_allows_silently(self):
        fake = FakeGh(gh_fail=(1, "none of the git remotes configured for this repository "
                                  "point to a known GitHub host"))
        code, message = run("gh api repos/o/r/git/refs/heads/x -X DELETE", fake)
        self.assertEqual(code, g.EXIT_ALLOW)
        self.assertEqual(message, "")

    def test_a_block_beats_an_advisory_in_a_chained_command(self):
        """`a && b` runs both, so the guard refuses the pair or neither."""
        runner = _scripted({
            ("git", "remote"): (0, "git@github.com:o/r.git", ""),
            ("gh", "pr", "list", "--state", "open", "--base", "safe"): (1, "", "dial tcp: no such host"),
            ("gh", "pr", "list", "--state", "open", "--base", "risky"):
                (0, json.dumps([pr(9)]), ""),
        })
        # Built from parts rather than written literally: a bare deleting `git push` after an
        # `&&` in this file would put one at command position in the file itself, and
        # `test_this_test_file_does_not_trip_the_guard_when_read_as_a_command` would go red --
        # correctly, which is the point of keeping that test strict.
        chained = " && ".join(["git push origin --delete safe",
                               "git push origin --delete risky"])
        code, message = g.evaluate(chained, runner=runner)
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertIn("#9", message)


class Stage1FailsOpenTest(unittest.TestCase):
    """`bash_path_guard`'s polarity for the cheap half: this runs on every shell call on the
    machine, so a payload it cannot read is a payload it has no opinion about."""

    HOSTILE = ("", "   ", "not json at all", "[]", "null", '{"tool_name": 12}',
               '{"tool_input": "a string"}', '{"tool_name":"Bash","tool_input":{}}',
               '{"tool_name":"Bash","tool_input":{"command":null}}')

    def test_every_hostile_payload_allows_silently(self):
        for raw in self.HOSTILE:
            with self.subTest(raw=raw):
                sink = _Sink()
                self.assertEqual(g.hook_main(stdin=_Stdin(raw), stderr=sink), g.EXIT_ALLOW)
                self.assertEqual(sink.text, "")

    def test_an_unguarded_tool_is_never_inspected(self):
        for tool in ("Read", "Edit", "Write", "Glob", "Grep", "WebFetch"):
            with self.subTest(tool=tool):
                code, _ = g.decide(event("git push origin --delete x", tool=tool))
                self.assertEqual(code, g.EXIT_ALLOW)

    def test_both_shells_are_guarded(self):
        self.assertEqual(g.GUARDED_TOOLS, ("Bash", "PowerShell"))

    def test_looks_like_deletion_never_raises(self):
        for command in FailsClosedOnAParseTest.UNREADABLE:
            with self.subTest(command=command):
                self.assertTrue(g.looks_like_deletion(command))


class ProtectedBranchesTest(unittest.TestCase):

    def test_the_integration_branch_and_the_release_point_are_refused_outright(self):
        fake = FakeGh(based_on={})
        for branch in ("develop", "master", "main"):
            with self.subTest(branch=branch):
                code, message = run(f"git push origin --delete {branch}", fake)
                self.assertEqual(code, g.EXIT_BLOCK)
                self.assertIn("protected branch", message)

    def test_develop_is_refused_even_when_nothing_is_stacked_on_it(self):
        """The predicate is the wrong question about the integration branch: 'no dependents' would
        clear it, and deleting what everything lands on is never routine."""
        code, _ = run("git push origin --delete develop", FakeGh(based_on={"develop": []}))
        self.assertEqual(code, g.EXIT_BLOCK)

    def test_the_constant_is_the_config_floor(self):
        """`PROTECTED_BRANCHES` is the fixed floor, read without touching the disk."""
        self.assertEqual(g.PROTECTED_BRANCHES, g.repo_config.PROTECTED_FLOOR)
        self.assertLessEqual(g.PROTECTED_BRANCHES, g.protected_branches())

    def test_a_configured_addition_is_protected_too(self):
        original = g.repo_config.protected_branches
        try:
            g.repo_config.protected_branches = (
                lambda cfg=None: g.repo_config.PROTECTED_FLOOR | {"release/next"})
            code, message = run("git push origin --delete release/next", FakeGh(based_on={}))
        finally:
            g.repo_config.protected_branches = original
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertIn("protected branch", message)

    def test_a_broken_config_read_falls_back_to_the_floor_and_never_raises(self):
        original = g.repo_config.protected_branches

        def boom(cfg=None):
            raise RuntimeError("config exploded")
        try:
            g.repo_config.protected_branches = boom
            self.assertEqual(g.protected_branches(), g.PROTECTED_BRANCHES)
            code, _ = run("git push origin --delete main", FakeGh(based_on={}))
        finally:
            g.repo_config.protected_branches = original
        self.assertEqual(code, g.EXIT_BLOCK)


# --------------------------------------------------------------------------- structure

class ThePredicateHasOneImplementationTest(unittest.TestCase):
    """Checked against the FILES, because the property is "there is no second copy"."""

    def _read(self, name):
        with open(os.path.join(SCRIPT_DIR, name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_sweep_imports_the_guards_predicate(self):
        source = self._read("branch_sweep.py")
        self.assertIn("import branch_delete_guard", source)
        self.assertIn("bdg.open_prs_based_on(", source)

    def test_the_sweep_defines_no_predicate_of_its_own(self):
        source = self._read("branch_sweep.py")
        self.assertNotIn("def open_prs_based_on", source)
        # The one `--base` query in this tree belongs to the guard.
        self.assertNotIn('"--base"', source)

    def test_the_sweep_uses_the_guards_protected_set_rather_than_a_copy(self):
        source = self._read("branch_sweep.py")
        self.assertIn("bdg.protected_branches()", source)
        self.assertNotIn("PROTECTED_BRANCHES = ", source)
        self.assertNotIn("PROTECTED_FLOOR", source)

    def test_the_guard_imports_merge_guards_lexer_rather_than_copying_it(self):
        source = self._read("branch_delete_guard.py")
        self.assertIn("mg.command_segments(", source)
        self.assertNotIn("_SEGMENT_SPLIT_RE = ", source)
        self.assertNotIn("_NESTED_SHELLS = ", source)

    def test_gh_segments_is_still_exactly_the_gh_case_of_the_generalised_walk(self):
        """The generalisation may not have widened what `merge_guard` itself detects."""
        for command in ("gh pr merge 5 --merge", "git push origin --delete x",
                        "bash -c 'gh pr merge 5'", "git status && gh pr merge 5"):
            with self.subTest(command=command):
                self.assertEqual(mg.gh_segments(command),
                                 mg.command_segments(command, ("gh",)))
        self.assertEqual(mg.gh_segments("git push origin --delete x"), [])


class NoRealSubprocessTest(unittest.TestCase):
    """The seam is the only door. Driven with a runner that raises on anything unstubbed."""

    def test_the_whole_decision_goes_through_the_runner(self):
        fake = FakeGh(heads={566: "docs/some-spec"},
                      based_on={"docs/some-spec": [pr(565)]})
        code, _ = run("gh pr merge 566 --delete-branch", fake)
        self.assertEqual(code, g.EXIT_BLOCK)
        self.assertEqual(len(fake.calls), 2)      # one `pr view`, one `pr list`
        self.assertEqual(fake.calls[0][:3], ["gh", "pr", "view"])
        self.assertEqual(fake.calls[1][:3], ["gh", "pr", "list"])

    def test_the_predicate_is_the_documented_one_call(self):
        fake = FakeGh(based_on={})
        run("gh api repos/o/r/git/refs/heads/x -X DELETE", fake)
        self.assertEqual(fake.calls, [["gh", "pr", "list", "--state", "open", "--base", "x",
                                       "--json", g.PR_LIST_FIELDS, "--limit", "100",
                                       "--repo", "o/r"]])

    def test_an_allowed_command_costs_nothing_at_all(self):
        fake = FakeGh()
        self.assertEqual(run("git status && ls -la", fake)[0], g.EXIT_ALLOW)
        self.assertEqual(fake.calls, [])


class ClassifyGhFailureTest(unittest.TestCase):
    """The one function that decides which way a failure falls, tested directly."""

    def test_unknown_failures_block(self):
        for text in ("something nobody has seen before", "", "HTTP 422: Unprocessable"):
            with self.subTest(text=text):
                self.assertIsInstance(g.classify_gh_failure(text), g.GuardError)

    def test_the_allowing_direction_is_a_whitelist(self):
        """Widening :data:`_UNREACHABLE_MARKERS` widens the ALLOW path, so this asserts the shape
        rather than a count: nothing reaches `Unreachable` except by matching a listed phrase."""
        self.assertIsInstance(g.classify_gh_failure("dial tcp 1.2.3.4:443"), g.Unreachable)
        self.assertIsInstance(g.classify_gh_failure("not a git repository"), g.NotGitHub)
        self.assertIsInstance(g.classify_gh_failure("bad credentials"), g.GuardError)


class HookContractTest(unittest.TestCase):

    def test_a_block_is_exit_2_with_the_reason_on_stderr(self):
        sink = _Sink()
        original = g.evaluate
        try:
            g.evaluate = lambda command, cwd=None, runner=None: (g.EXIT_BLOCK, "the reason")
            code = g.hook_main(stdin=_Stdin(json.dumps(event("git push origin --delete x"))),
                               stderr=sink)
        finally:
            g.evaluate = original
        self.assertEqual(code, 2)
        self.assertEqual(sink.text.strip(), "the reason")

    def test_the_three_exit_codes_are_distinct_and_are_the_contracts(self):
        self.assertEqual((g.EXIT_ALLOW, g.EXIT_ADVISORY, g.EXIT_BLOCK), (0, 1, 2))

    def test_no_decision_is_ever_written_to_stdout(self):
        """`bash_path_guard`'s and `merge_guard`'s shared decision: the JSON `permissionDecision`
        mechanism resolves a malformed object by letting the action PROCEED, so this guard's verdict
        rides on the exit code only."""
        with open(os.path.join(SCRIPT_DIR, "branch_delete_guard.py"), encoding="utf-8") as fh:
            source = fh.read()
        # Both mechanisms are NAMED in the module docstring, deliberately, so the assertion is
        # against the code below it rather than against the file.
        code = source[source.index('"""', source.index('"""') + 3) + 3:]
        self.assertNotIn("permissionDecision", code)
        self.assertNotIn("hookSpecificOutput", code)
        # `print` exists only under the `check` diagnostic, never on the hook path.
        self.assertNotIn("print(", code[:code.index("def _cmd_check")])


# --------------------------------------------------------------------------- helpers

class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


class _Sink:
    def __init__(self):
        self.text = ""

    def write(self, chunk):
        self.text += chunk


def _scripted(table):
    """A runner driven by argv-prefix -> `(rc, stdout, stderr)`. Longest prefix wins, so a test can
    give one branch a different answer from another."""
    def runner(argv, cwd=None, timeout=None):
        best, best_len = None, -1
        for prefix, response in table.items():
            p = list(prefix)
            if list(argv[:len(p)]) == p and len(p) > best_len:
                best, best_len = response, len(p)
        if best is None:
            raise AssertionError(f"unscripted call: {argv!r}")
        return best
    return runner


if __name__ == "__main__":
    unittest.main()
