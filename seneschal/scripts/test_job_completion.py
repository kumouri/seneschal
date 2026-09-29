#!/usr/bin/env python3
"""Tests for job_completion.py — the job-completion guarantee: detect, resume, rescue
(`background-jobs-spec.md` §3.13).

**DETECT and RESCUE run against REAL temporary git repositories, deliberately — not a fake runner.**
This module's whole design leans on specific, non-obvious git plumbing behaviour (most notably: `git
worktree remove` succeeds even when the worktree's branch holds committed-but-unpushed commits, because
the commits stay reachable via the branch ref in the shared object store — verified empirically before
this module was written, not assumed). A canned `CompletedProcess` would have to already know the
answer this suite exists to pin down, which is the same argument `test_job_worktree.py` makes for using
a fake in the other direction: there, the command SEQUENCE is the thing under test, so a fake that
answers deterministically is the right tool; here, git's actual BEHAVIOUR is the thing under test, so
only real git will do. `gh` is the one exception — real `gh pr list` needs network and auth CI does not
have — and it is exercised only through its own injectable `gh_runner` seam, always faked below.

**RESUME cannot be exercised end to end.** Spawning a real `claude` process from a test is not
feasible in this environment, so only its DECISION logic is covered here: whether a resume is
attempted at all (`can_resume`, the attempt bound), and what gets built (`resume_argv`,
`build_resume_prompt`, `prepare_argv`'s session-id injection). The actual respawn is
`jobs.reconcile`'s existing, separately-tested `_spawn_attempt` path — see `test_jobs.py`'s
`JobCompletionGuaranteeTests` for the integration seam, which fakes the spawn rather than running it.

Run: python -m unittest discover -s seneschal/scripts -p "test_job_completion.py"
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import job_completion as jc  # noqa: E402
import job_worktree  # noqa: E402

NOW = datetime(2026, 9, 6, 21, 0, 0, tzinfo=timezone.utc)


def _run(args, cwd=None, check=False):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


class _Git:
    """The real seam — `job_completion._git`/`_gh` call `.run(args, cwd, check)`, matching
    `job_worktree.Runner`'s own shape exactly, so this is that same interface over real
    `subprocess.run`."""

    def run(self, args, cwd=None, check=False):
        return _run(args, cwd=cwd, check=check)


class _FakeGh:
    """Answers `gh pr list --json number` with a canned list — never touches the network."""

    def __init__(self, open_prs=None, fail=False):
        self.open_prs = open_prs if open_prs is not None else []
        self.fail = fail
        self.calls: list = []

    def run(self, args, cwd=None, check=False):
        self.calls.append((list(args), cwd))
        if self.fail:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="gh: not authenticated")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(self.open_prs), stderr="")


class _RepoTestCase(unittest.TestCase):
    """One bare `origin` + one real clone (`work`), on `develop`, with `develop` already pushed —
    the shape `job_worktree.create` hands an agent, minus the worktree machinery itself (irrelevant
    to this module, which only ever reads/writes a plain working directory)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin = os.path.join(self.tmp.name, "origin.git")
        self.work = os.path.join(self.tmp.name, "work")
        _run(["git", "init", "-q", "--bare", self.origin], check=True)
        _run(["git", "clone", "-q", self.origin, self.work], check=True)
        _run(["git", "-C", self.work, "config", "user.email", "test@example.com"], check=True)
        _run(["git", "-C", self.work, "config", "user.name", "Test"], check=True)
        _run(["git", "-C", self.work, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        # Rename whatever `git init`'s default branch name is (host-dependent — this box's is
        # `master`) to `develop`, matching this repo's own convention, so a test naming `develop`
        # explicitly is actually exercising the branch it says it is.
        _run(["git", "-C", self.work, "branch", "-m", "develop"], check=True)
        _run(["git", "-C", self.work, "push", "-q", "origin", "HEAD:develop"], check=True)
        self.init_sha = _run(["git", "-C", self.work, "rev-parse", "HEAD"], check=True).stdout.strip()
        self.git = _Git()

    def _checkout(self, branch):
        _run(["git", "-C", self.work, "checkout", "-q", "-b", branch], check=True)

    def _commit_file(self, name="f.txt", content="x", msg="work"):
        with open(os.path.join(self.work, name), "w", encoding="utf-8") as fh:
            fh.write(content)
        _run(["git", "-C", self.work, "add", name], check=True)
        _run(["git", "-C", self.work, "commit", "-q", "-m", msg], check=True)

    def _stage_file(self, name="f.txt", content="x"):
        with open(os.path.join(self.work, name), "w", encoding="utf-8") as fh:
            fh.write(content)
        _run(["git", "-C", self.work, "add", name], check=True)

    def _rec(self, **extra):
        rec = {"id": "20260906-210000-aaaa", "cwd": self.work}
        rec.update(extra)
        return rec


# --------------------------------------------------------------------------- DETECT

class DetectCleanRepo(_RepoTestCase):
    def test_a_clean_fully_pushed_branch_with_an_open_pr_is_complete(self):
        self._checkout("feature/clean")
        self._commit_file(msg="real work")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feature/clean"], check=True)
        gh = _FakeGh(open_prs=[{"number": 1}])
        self.assertIsNone(jc.detect_incomplete(self._rec(), runner=self.git, gh_runner=gh))

    def test_a_job_that_produced_no_changes_at_all_is_complete(self):
        """The conservative-by-design floor: nothing happened, so nothing is wrong."""
        self._checkout("feature/idle")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feature/idle"], check=True)
        gh = _FakeGh(open_prs=[{"number": 9}])
        self.assertIsNone(jc.detect_incomplete(self._rec(), runner=self.git, gh_runner=gh))

    def test_a_non_repo_cwd_is_complete_not_flagged(self):
        empty = os.path.join(self.tmp.name, "not-a-repo")
        os.makedirs(empty)
        self.assertIsNone(jc.detect_incomplete(self._rec(cwd=empty), runner=self.git))

    def test_a_missing_cwd_is_complete_not_flagged(self):
        missing = os.path.join(self.tmp.name, "does-not-exist")
        self.assertIsNone(jc.detect_incomplete(self._rec(cwd=missing), runner=self.git))


class DetectDirtyTree(_RepoTestCase):
    def test_staged_but_uncommitted_is_flagged(self):
        self._checkout("feature/staged")
        self._stage_file()
        result = jc.detect_incomplete(self._rec(), runner=self.git)
        self.assertEqual(result["reasons"], [jc.STAGED_UNCOMMITTED])
        self.assertEqual(result["branch"], "feature/staged")

    def test_unstaged_modification_to_a_tracked_file_is_flagged(self):
        self._checkout("feature/unstaged")
        self._commit_file()
        with open(os.path.join(self.work, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("changed")
        result = jc.detect_incomplete(self._rec(), runner=self.git)
        self.assertIn(jc.UNSTAGED_TRACKED, result["reasons"])

    def test_an_untracked_file_alone_is_not_flagged(self):
        """`git diff --quiet` never looks at untracked files — a job's cwd routinely holds gitignored
        build output, and flagging that would make every job read as dirty."""
        self._checkout("feature/untracked")
        with open(os.path.join(self.work, "scratch.tmp"), "w", encoding="utf-8") as fh:
            fh.write("build output")
        self.assertIsNone(jc.detect_incomplete(self._rec(), runner=self.git))

    def test_both_staged_and_unstaged_are_reported_together(self):
        self._checkout("feature/both")
        self._commit_file(name="a.txt")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feature/both"], check=True)
        self._stage_file(name="b.txt")
        with open(os.path.join(self.work, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("edited")
        result = jc.detect_incomplete(self._rec(), runner=self.git)
        self.assertEqual(set(result["reasons"]), {jc.STAGED_UNCOMMITTED, jc.UNSTAGED_TRACKED})


class DetectUnpushedAndPr(_RepoTestCase):
    def test_a_branch_never_pushed_at_all_is_unpushed(self):
        self._checkout("feature/never-pushed")
        self._commit_file(msg="real work")
        result = jc.detect_incomplete(self._rec(), runner=self.git)
        self.assertEqual(result["reasons"], [jc.UNPUSHED_COMMITS])
        self.assertEqual(result["detail"]["unpushed_commits"], 1)

    def test_a_partially_pushed_branch_counts_only_the_unpushed_tail(self):
        self._checkout("feature/partial")
        self._commit_file(name="a.txt", msg="first")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feature/partial"], check=True)
        self._commit_file(name="b.txt", msg="second")
        self._commit_file(name="c.txt", msg="third")
        result = jc.detect_incomplete(self._rec(), runner=self.git)
        self.assertEqual(result["reasons"], [jc.UNPUSHED_COMMITS])
        self.assertEqual(result["detail"]["unpushed_commits"], 2)

    def test_detached_head_with_commits_and_no_branch_is_unpushed(self):
        self._commit_file(msg="orphan work")
        _run(["git", "-C", self.work, "checkout", "-q", "--detach", "HEAD"], check=True)
        result = jc.detect_incomplete(self._rec(), runner=self.git)
        self.assertEqual(result["reasons"], [jc.UNPUSHED_COMMITS])
        self.assertIsNone(result["branch"])
        self.assertTrue(result["detail"]["detached"])

    def test_detached_head_at_a_commit_already_on_a_remote_ref_is_complete(self):
        # A `--cwd` worktree detached at a commit that is already the tip of a remote branch —
        # ahead of `origin/develop`, on no local branch, touched nothing. That is a finished job,
        # not orphan work.
        self._checkout("feat/generation-fixture")
        self._commit_file(msg="fixture work, pushed on its own branch")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feat/generation-fixture"], check=True)
        _run(["git", "-C", self.work, "checkout", "-q", "--detach", "HEAD"], check=True)
        _run(["git", "-C", self.work, "branch", "-q", "-D", "feat/generation-fixture"], check=True)
        # Sanity: the shape under test — detached, ahead of develop, HEAD on a remote ref.
        self.assertIsNone(jc.current_branch(self.work, runner=self.git))
        self.assertEqual(jc.remote_refs_containing_head(self.work, runner=self.git),
                         ["origin/feat/generation-fixture"])
        result = jc.detect_incomplete(self._rec(), runner=self.git, gh_runner=_FakeGh())
        self.assertIsNone(result)

    def test_a_named_branch_at_a_commit_already_on_another_remote_ref_is_not_unpushed(self):
        self._checkout("feat/pushed-elsewhere")
        self._commit_file(msg="pushed under one name")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feat/pushed-elsewhere"], check=True)
        # Same commit, a second local branch name that was never pushed under ITS name.
        self._checkout("feat/local-alias")
        result = jc.detect_incomplete(self._rec(), runner=self.git, gh_runner=_FakeGh())
        self.assertIsNone(result)

    def test_fully_pushed_with_no_open_pr_is_flagged(self):
        self._checkout("feature/no-pr")
        self._commit_file(msg="real work")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feature/no-pr"], check=True)
        gh = _FakeGh(open_prs=[])
        result = jc.detect_incomplete(self._rec(), runner=self.git, gh_runner=gh)
        self.assertEqual(result["reasons"], [jc.PUSHED_NO_PR])
        self.assertEqual(gh.calls[0][0][:3], ["gh", "pr", "list"])

    def test_fully_pushed_with_an_open_pr_is_complete(self):
        self._checkout("feature/has-pr")
        self._commit_file(msg="real work")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feature/has-pr"], check=True)
        gh = _FakeGh(open_prs=[{"number": 42}])
        self.assertIsNone(jc.detect_incomplete(self._rec(), runner=self.git, gh_runner=gh))

    def test_no_gh_runner_fails_open_on_the_pr_check(self):
        """Fail-OPEN on the one ambiguous sub-check: an unreadable `gh` state must never itself
        manufacture a finding."""
        self._checkout("feature/no-gh")
        self._commit_file(msg="real work")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feature/no-gh"], check=True)
        self.assertIsNone(jc.detect_incomplete(self._rec(), runner=self.git, gh_runner=None))

    def test_a_failing_gh_call_fails_open_too(self):
        self._checkout("feature/gh-broken")
        self._commit_file(msg="real work")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feature/gh-broken"], check=True)
        gh = _FakeGh(fail=True)
        self.assertIsNone(jc.detect_incomplete(self._rec(), runner=self.git, gh_runner=gh))

    def test_being_on_develop_itself_never_triggers_the_pr_check(self):
        """`develop` is never something a PR targets FROM itself — even with real local commits ahead
        of the (nonexistent, here) upstream, the branch-vs-base check must not fire."""
        # HEAD is already `develop`, un-pushed relative to a hypothetical further develop —
        # there is nothing ahead of the base here at all, so this is just the "no changes" floor
        # restated for the protected branch specifically.
        self.assertIsNone(jc.detect_incomplete(self._rec(), runner=self.git))


# --------------------------------------------------------------------------- RESCUE

class RescueLandsOrRefuses(_RepoTestCase):
    def test_staged_work_is_committed_and_pushed_to_a_rescue_branch(self):
        self._checkout("feature/rescue-me")
        self._stage_file(content="unsaved")
        result = jc.rescue(self._rec(), None, runner=self.git, now=NOW)
        self.assertTrue(result["committed"])
        self.assertTrue(result["pushed"])
        self.assertEqual(result["branch"], "rescue/20260906-210000-aaaa")
        # The pushed ref exists on origin, at a commit that actually carries the file.
        show = _run(["git", "--git-dir", self.origin, "log", "-1", "--name-only",
                     "refs/heads/rescue/20260906-210000-aaaa"])
        self.assertEqual(show.returncode, 0)
        self.assertIn("f.txt", show.stdout)
        # The branch's OWN name on origin was never touched — rescue never invents a push under it.
        original = _run(["git", "--git-dir", self.origin, "rev-parse", "--verify", "--quiet",
                         "refs/heads/feature/rescue-me"])
        self.assertNotEqual(original.returncode, 0)

    def test_a_clean_tree_with_only_unpushed_commits_pushes_without_committing(self):
        self._checkout("feature/clean-unpushed")
        self._commit_file(msg="already committed, just never pushed")
        result = jc.rescue(self._rec(), None, runner=self.git, now=NOW)
        self.assertFalse(result["committed"])
        self.assertTrue(result["pushed"])

    def test_a_tip_already_on_a_remote_ref_is_never_pushed_as_a_rescue(self):
        # The RESCUE half of the same shape: nothing dirty, HEAD already on origin under
        # another ref — a rescue push would only duplicate that commit under `rescue/<id>`.
        self._checkout("feat/generation-fixture")
        self._commit_file(msg="already on origin")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feat/generation-fixture"], check=True)
        _run(["git", "-C", self.work, "checkout", "-q", "--detach", "HEAD"], check=True)
        result = jc.rescue(self._rec(), None, runner=self.git, now=NOW)
        self.assertFalse(result["committed"])
        self.assertFalse(result["pushed"])
        self.assertEqual(result["already_remote"], ["origin/feat/generation-fixture"])
        self.assertIn("nothing to rescue", result["skipped_reason"])
        gone = _run(["git", "--git-dir", self.origin, "rev-parse", "--verify", "--quiet",
                     "refs/heads/rescue/20260906-210000-aaaa"])
        self.assertNotEqual(gone.returncode, 0)

    def test_dirty_work_on_a_remote_tip_is_still_committed_and_rescued(self):
        # The new commit moves HEAD off the remote ref, so the already-on-remote check must not
        # suppress a rescue that has real unsaved work to land.
        self._checkout("feat/generation-fixture")
        self._commit_file(msg="already on origin")
        _run(["git", "-C", self.work, "push", "-q", "origin", "feat/generation-fixture"], check=True)
        self._stage_file(name="new.txt", content="unsaved")
        result = jc.rescue(self._rec(), None, runner=self.git, now=NOW)
        self.assertTrue(result["committed"])
        self.assertTrue(result["pushed"])

    def test_develop_in_a_shared_checkout_is_refused_not_committed(self):
        self._stage_file(content="oops, ran directly on develop")
        result = jc.rescue(self._rec(), None, runner=self.git, now=NOW)
        self.assertFalse(result["committed"])
        self.assertFalse(result["pushed"])
        self.assertIn("develop", result["skipped_reason"])
        # Nothing was staged or committed onto the live branch — the refusal is total, not partial.
        status = _run(["git", "-C", self.work, "status", "--porcelain"])
        self.assertIn("f.txt", status.stdout)  # still just sitting there, untouched

    def test_master_is_refused_the_same_way(self):
        _run(["git", "-C", self.work, "checkout", "-q", "-b", "master", self.init_sha], check=True)
        self._stage_file()
        result = jc.rescue(self._rec(), None, runner=self.git, now=NOW)
        self.assertFalse(result["pushed"])
        self.assertIn("master", result["skipped_reason"])

    def test_a_non_repo_cwd_is_refused_cleanly(self):
        empty = os.path.join(self.tmp.name, "not-a-repo-2")
        os.makedirs(empty)
        result = jc.rescue(self._rec(cwd=empty), None, runner=self.git, now=NOW)
        self.assertFalse(result["pushed"])
        self.assertIsNotNone(result["skipped_reason"])

    def test_rescue_never_raises_even_when_the_runner_explodes(self):
        class Boom:
            def run(self, *a, **k):
                raise RuntimeError("seam broke")
        self._checkout("feature/boom")
        self._stage_file()
        result = jc.rescue(self._rec(), None, runner=Boom(), now=NOW)
        self.assertFalse(result["pushed"])
        self.assertIn("seam broke", result["skipped_reason"])

    def test_a_conflicting_rescue_branch_is_never_force_pushed_over(self):
        """Someone (or something) already pushed a DIFFERENT, DIVERGING history under this exact
        rescue name. Plain `git push` (no `--force` anywhere in this module) must refuse rather than
        clobber it. Both branches below share `init_sha` as their only common ancestor, so the second
        push is a genuine non-fast-forward, not a rejected-for-the-wrong-reason coincidence."""
        _run(["git", "-C", self.work, "checkout", "-q", "-b", "throwaway", self.init_sha], check=True)
        self._commit_file(name="theirs.txt", content="a different history entirely")
        _run(["git", "-C", self.work, "push", "-q", "origin",
             "HEAD:refs/heads/rescue/20260906-210000-aaaa"], check=True)
        _run(["git", "-C", self.work, "checkout", "-q", "-b", "feature/conflict", self.init_sha],
             check=True)
        self._stage_file(content="new, unrelated history")
        result = jc.rescue(self._rec(), None, runner=self.git, now=NOW)
        self.assertFalse(result["pushed"])
        self.assertIn("push failed", result["skipped_reason"])
        # And the earlier, conflicting history is exactly as it was — never overwritten.
        still_theirs = _run(["git", "--git-dir", self.origin, "log", "-1", "--name-only",
                             "refs/heads/rescue/20260906-210000-aaaa"])
        self.assertIn("theirs.txt", still_theirs.stdout)


# --------------------------------------------------------------------------- scope

class ScopeGate(unittest.TestCase):
    def test_a_worktree_job_is_scoped_regardless_of_cwd(self):
        rec = {"worktree": {"path": "/x"}, "cwd": "/repo-root"}
        self.assertTrue(jc.is_scoped_cwd(rec, "/repo-root"))

    def test_the_shared_repo_root_with_no_worktree_is_not_scoped(self):
        rec = {"cwd": "/repo-root"}
        self.assertFalse(jc.is_scoped_cwd(rec, "/repo-root"))

    def test_an_explicit_different_cwd_is_scoped(self):
        rec = {"cwd": "/somewhere/else"}
        self.assertTrue(jc.is_scoped_cwd(rec, "/repo-root"))

    def test_no_cwd_at_all_is_not_scoped(self):
        self.assertFalse(jc.is_scoped_cwd({}, "/repo-root"))


class EvaluateDecision(_RepoTestCase):
    def test_a_shared_cwd_job_proceeds_even_when_dirty(self):
        """The false-positive guard: a dirty SHARED checkout must never be blamed on whichever
        unrelated job happens to finish next."""
        self._checkout("feature/shared")
        self._stage_file()
        rec = self._rec(cwd=self.work)  # no `worktree` key — indistinguishable from the shared root
        outcome = jc.evaluate(rec, repo_root=self.work, runner=self.git, now=NOW)
        self.assertEqual(outcome["action"], "proceed")
        self.assertEqual(outcome["completion"]["reasons"], [])

    def test_a_scoped_dirty_job_with_no_session_id_goes_straight_to_rescue(self):
        self._checkout("feature/no-session")
        self._stage_file()
        rec = self._rec(worktree={"path": self.work})
        outcome = jc.evaluate(rec, repo_root="/somewhere-else", runner=self.git, now=NOW)
        self.assertEqual(outcome["action"], "rescue")
        self.assertEqual(outcome["completion"]["reasons"], [jc.STAGED_UNCOMMITTED])

    def test_a_scoped_dirty_job_with_a_session_id_resumes(self):
        self._checkout("feature/has-session")
        self._stage_file()
        rec = self._rec(worktree={"path": self.work}, claude_session_id="abc-123")
        outcome = jc.evaluate(rec, repo_root="/somewhere-else", runner=self.git, now=NOW)
        self.assertEqual(outcome["action"], "resume")
        self.assertEqual(outcome["argv"][:3], ["claude", "--resume", "abc-123"])
        self.assertIn(jc.REASON_TEXT[jc.STAGED_UNCOMMITTED], outcome["prompt"])

    def test_exhausted_resume_attempts_fall_through_to_rescue(self):
        self._checkout("feature/exhausted")
        self._stage_file()
        prior = {"resume_attempts": [{"at": "x", "argv": []}] * jc.MAX_RESUME_ATTEMPTS}
        rec = self._rec(worktree={"path": self.work}, claude_session_id="abc-123", completion=prior)
        outcome = jc.evaluate(rec, repo_root="/somewhere-else", runner=self.git, now=NOW)
        self.assertEqual(outcome["action"], "rescue")

    def test_a_clean_scoped_job_proceeds(self):
        self._checkout("feature/all-good")
        rec = self._rec(worktree={"path": self.work})
        outcome = jc.evaluate(rec, repo_root="/somewhere-else", runner=self.git, now=NOW)
        self.assertEqual(outcome["action"], "proceed")


# --------------------------------------------------------------------------- RESUME decision logic

class ResumeBounds(unittest.TestCase):
    def test_no_session_id_cannot_resume(self):
        self.assertFalse(jc.can_resume({"status": "done"}))

    def test_a_cancelled_job_cannot_resume(self):
        self.assertFalse(jc.can_resume({"status": "cancelled", "claude_session_id": "x"}))

    def test_bounded_at_max_resume_attempts(self):
        rec = {"status": "done", "claude_session_id": "x",
               "completion": {"resume_attempts": [{}] * jc.MAX_RESUME_ATTEMPTS}}
        self.assertFalse(jc.can_resume(rec))

    def test_under_the_bound_can_resume(self):
        rec = {"status": "done", "claude_session_id": "x",
               "completion": {"resume_attempts": [{}] * (jc.MAX_RESUME_ATTEMPTS - 1)}}
        self.assertTrue(jc.can_resume(rec))

    def test_resume_argv_is_none_without_a_session_id(self):
        self.assertIsNone(jc.resume_argv({}, "prompt"))

    def test_resume_argv_shape(self):
        argv = jc.resume_argv({"claude_session_id": "sid-1"}, "do the thing")
        self.assertEqual(argv, ["claude", "--resume", "sid-1", "-p", "do the thing"])

    def test_the_prompt_never_tells_the_resumed_agent_to_background_anything(self):
        prompt = jc.build_resume_prompt({"reasons": [jc.UNPUSHED_COMMITS], "branch": "feature/x"})
        self.assertIn("feature/x", prompt)
        self.assertIn("never pushed", prompt)
        self.assertIn("background", prompt.lower())
        self.assertIn("foreground", prompt.lower())


class ResumeArgvCarriesTheOriginalFlags(unittest.TestCase):
    """A bare `["claude", "--resume", sid, "-p", prompt]` drops every other flag — including
    `--permission-mode bypassPermissions` — so the resumed session runs under the CLI's default
    permission mode and can never push. These pin the carry-forward."""

    def test_permission_mode_and_model_are_carried_forward(self):
        rec = {"claude_session_id": "sid-1",
               "argv": ["claude", "-p", "--permission-mode", "bypassPermissions",
                        "--model", "opus", "do the thing"]}
        argv = jc.resume_argv(rec, "continue")
        self.assertEqual(argv, ["claude", "--resume", "sid-1", "--permission-mode",
                                "bypassPermissions", "--model", "opus", "-p", "continue"])

    def test_variadic_and_boolean_flags_are_carried_forward(self):
        # Prompt placed right after `-p`, matching every real call site in this codebase
        # (`presence.py`, `reminders_live.py`) — see `_carried_flags`'s named residual for why a
        # variadic flag's greedy consumption can't safely sit directly before a TRAILING bare prompt.
        rec = {"claude_session_id": "sid-1",
               "argv": ["claude", "-p", "original prompt", "--add-dir", "a", "b",
                        "--dangerously-skip-permissions", "--allowedTools", "Bash", "Edit"]}
        argv = jc.resume_argv(rec, "continue")
        self.assertEqual(argv, ["claude", "--resume", "sid-1", "--add-dir", "a", "b",
                                "--dangerously-skip-permissions", "--allowedTools", "Bash", "Edit",
                                "-p", "continue"])

    def test_session_id_and_the_original_print_flag_and_prompt_are_never_carried(self):
        rec = {"claude_session_id": "sid-1",
               "argv": ["claude", "--session-id", "sid-1", "-p", "--permission-mode",
                        "bypassPermissions", "original prompt"]}
        argv = jc.resume_argv(rec, "continue")
        self.assertEqual(argv, ["claude", "--resume", "sid-1", "--permission-mode",
                                "bypassPermissions", "-p", "continue"])
        self.assertNotIn("--session-id", argv)
        self.assertNotIn("original prompt", argv)
        self.assertEqual(argv.count("-p"), 1)

    def test_an_unrecognized_flag_is_dropped(self):
        rec = {"claude_session_id": "sid-1",
               "argv": ["claude", "-p", "--verbose", "--permission-mode", "acceptEdits", "hi"]}
        argv = jc.resume_argv(rec, "continue")
        self.assertNotIn("--verbose", argv)
        self.assertEqual(argv, ["claude", "--resume", "sid-1", "--permission-mode",
                                "acceptEdits", "-p", "continue"])

    def test_original_argv_wins_over_a_first_resumes_already_rewritten_argv(self):
        """`jobs.reconcile` overwrites `rec["argv"]` with the FIRST resume's own argv (which already
        carries only what THAT resume carried) and stamps the true original launch into
        `original_argv` — a SECOND resume must read `original_argv`, not the already-resumed `argv`,
        or it would compound whatever the first resume already lost."""
        rec = {"claude_session_id": "sid-1",
               "original_argv": ["claude", "-p", "--permission-mode", "bypassPermissions",
                                 "first prompt"],
               "argv": ["claude", "--resume", "sid-1", "-p", "the first resume's own prompt"]}
        argv = jc.resume_argv(rec, "continue again")
        self.assertEqual(argv, ["claude", "--resume", "sid-1", "--permission-mode",
                                "bypassPermissions", "-p", "continue again"])

    def test_no_argv_at_all_still_falls_back_to_the_bare_shape(self):
        # The None path (`test_resume_argv_is_none_without_a_session_id` above) is unchanged; this is
        # the other edge — a session id with no argv/original_argv on the record at all.
        argv = jc.resume_argv({"claude_session_id": "sid-1"}, "do the thing")
        self.assertEqual(argv, ["claude", "--resume", "sid-1", "-p", "do the thing"])


class PermissionModeOf(unittest.TestCase):
    def test_reads_the_value_after_the_flag(self):
        self.assertEqual(
            jc.permission_mode_of(["claude", "-p", "--permission-mode", "bypassPermissions", "hi"]),
            "bypassPermissions")

    def test_none_when_the_flag_is_absent(self):
        self.assertIsNone(jc.permission_mode_of(["claude", "-p", "hi"]))

    def test_none_on_an_empty_or_missing_argv(self):
        self.assertIsNone(jc.permission_mode_of([]))
        self.assertIsNone(jc.permission_mode_of(None))


class PrepareArgvInjection(unittest.TestCase):
    def test_a_bare_claude_dash_p_gets_a_session_id(self):
        argv, sid = jc.prepare_argv(["claude", "-p", "do the thing"])
        self.assertIsNotNone(sid)
        self.assertEqual(argv, ["claude", "--session-id", sid, "-p", "do the thing"])

    def test_flags_before_dash_p_are_preserved_after_the_command(self):
        argv, sid = jc.prepare_argv(["claude", "--mcp-config", "x.json", "-p", "hi"])
        self.assertEqual(argv, ["claude", "--session-id", sid, "--mcp-config", "x.json", "-p", "hi"])

    def test_a_non_claude_command_is_untouched(self):
        original = ["python", "watch_pr.py", "214"]
        argv, sid = jc.prepare_argv(original)
        self.assertEqual(argv, original)
        self.assertIsNone(sid)

    def test_claude_without_dash_p_is_untouched(self):
        argv, sid = jc.prepare_argv(["claude"])
        self.assertEqual(argv, ["claude"])
        self.assertIsNone(sid)

    def test_an_existing_session_id_is_never_overridden(self):
        original = ["claude", "--session-id", "caller-chosen", "-p", "hi"]
        argv, sid = jc.prepare_argv(original)
        self.assertEqual(argv, original)
        self.assertIsNone(sid)

    def test_an_explicit_resume_is_left_alone(self):
        original = ["claude", "--resume", "old-sid", "-p", "continue"]
        argv, sid = jc.prepare_argv(original)
        self.assertEqual(argv, original)
        self.assertIsNone(sid)

    def test_a_windows_exe_path_is_still_recognised_as_claude(self):
        argv, sid = jc.prepare_argv(["C:\\Users\\x\\claude.exe", "-p", "hi"])
        self.assertIsNotNone(sid)

    def test_empty_argv_is_untouched(self):
        argv, sid = jc.prepare_argv([])
        self.assertEqual(argv, [])
        self.assertIsNone(sid)

    def test_two_calls_never_collide_on_the_same_session_id(self):
        _, sid1 = jc.prepare_argv(["claude", "-p", "a"])
        _, sid2 = jc.prepare_argv(["claude", "-p", "b"])
        self.assertNotEqual(sid1, sid2)


if __name__ == "__main__":
    unittest.main()
