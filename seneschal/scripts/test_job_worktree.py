#!/usr/bin/env python3
"""Tests for job_worktree.py — one private git worktree per delegated job
(`docs/delegated-work-isolation-spec.md` phase 1).

**No real git runs here, and none ever should.** Every call goes through the injected `Runner`, the
so the assertions are about the exact command sequence — which is where this feature's failures
live. The four that matter most are each pinned by a class below:

  * **`CutFromOriginDevelop`** — the ref is `origin/develop`, spelled in full. A bare `develop` is
    ambiguous in a checkout with more than one Git Flow remote and dies with *"matched multiple
    remote tracking branches"*. The fetch happens BEFORE the add, because a cut taken first is a cut from whatever `origin/develop`
    this checkout last saw — and a stale base looks exactly like a clean one until the diff is wrong.
  * **`NeverForce`** — `git worktree remove` is never passed `--force`, on any path, ever. The refusal
    when a tree is dirty or holds unpushed commits IS the safety mechanism; a job that ended badly is
    exactly when its worktree is most likely to still hold something. There is no escalation and this
    class exists to keep it that way.
  * **`ARefusalIsRecordedNotSwallowed`** — a refused removal comes back `leaked: True` carrying git's
    own words, with the directory untouched. A silent reclaim would be the collision failure shape in
    a new place: something destructive succeeding quietly.
  * **`SetupFailureIsLoud`** — a failed fetch or a failed `worktree add` raises. The caller's
    contract (`jobs.start_job`) is that the job then does not run at all; a fallback to the shared
    tree would reintroduce precisely the collision this module removes.

Run: python -m unittest discover -s seneschal/scripts -p "test_job_worktree.py"
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import job_worktree as jw  # noqa: E402
import jobs  # noqa: E402

NOW = datetime(2026, 8, 9, 18, 0, 0, tzinfo=timezone.utc)


def git_args(call) -> list:
    """One recorded call with the `git -c core.fsmonitor=false` prefix stripped, so a test can talk
    about `["worktree", "add", …]` without re-writing the prefix every time."""
    argv = list(call[0])
    return argv[3:] if argv[:3] == ["git", *jw.GIT_FLAGS] else argv


def verb(call) -> str:
    """`fetch` / `worktree add` / `worktree remove` / `worktree prune` — the key a fake answers on."""
    args = git_args(call)
    if not args:
        return ""
    return " ".join(args[:2]) if args[0] == "worktree" else args[0]


class _FakeGit:
    """Records every argv and answers from a table keyed by `verb`. Spawns nothing.

    `fail` maps a verb to `(returncode, stderr)`; anything unlisted succeeds.

    It does make the **directory** move for a successful `worktree add` / `remove`, because the code
    under test asks the filesystem whether a path is still there before deciding whether an absent
    tree is a leak. A fake that recorded the argv and left the disk untouched would make every
    teardown look like an already-gone tree — green, and testing nothing."""

    def __init__(self, fail: dict | None = None):
        self.calls: list = []
        self.fail = dict(fail or {})

    def run(self, args, cwd=None, check=False):
        args = list(args)
        self.calls.append((args, cwd))
        action = verb((args, cwd))
        code, err = self.fail.get(action, (0, ""))
        if code == 0:
            body = git_args((args, cwd))
            if action == "worktree add":
                os.makedirs(body[-2], exist_ok=True)         # …add --detach <path> origin/develop
            elif action == "worktree remove":
                shutil.rmtree(body[-1], ignore_errors=True)
        return subprocess.CompletedProcess(args, code, stdout="", stderr=err)

    def verbs(self) -> list:
        return [verb(c) for c in self.calls]

    def find(self, wanted: str):
        for call in self.calls:
            if verb(call) == wanted:
                return git_args(call)
        return None


class _WorktreeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "seneschal-worktrees")
        self.host = os.path.join(self.tmp.name, "host")
        os.makedirs(self.host, exist_ok=True)


class CutFromOriginDevelop(_WorktreeTestCase):
    """The ref, its spelling, and the order of the two commands."""

    def test_fetch_happens_before_the_add(self):
        git = _FakeGit()
        jw.create("20260809-180000-aaaa", root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(git.verbs()[:2], ["fetch", "worktree add"])

    def test_the_fetch_names_origin_develop_explicitly(self):
        git = _FakeGit()
        jw.create("j1", root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(git.find("fetch"), ["fetch", "origin", "develop"])

    def test_the_add_cuts_from_origin_develop_and_never_a_bare_develop(self):
        """`develop` alone can match several remote-tracking branches (any second Git Flow remote,
        e.g. a vendored subtree). The full spelling is the fix and this is the assertion that keeps
        it."""
        git = _FakeGit()
        jw.create("j1", root=self.root, host=self.host, runner=git, now=NOW)
        add = git.find("worktree add")
        self.assertIn("origin/develop", add)
        self.assertNotIn("develop", add)          # the bare ref, anywhere in the add
        self.assertEqual(jw.BASE_REF, "origin/develop")

    def test_the_add_is_detached_so_the_agent_picks_its_own_branch(self):
        git = _FakeGit()
        rec = jw.create("j1", root=self.root, host=self.host, runner=git, now=NOW)
        add = git.find("worktree add")
        self.assertIn("--detach", add)
        self.assertNotIn("-b", add)               # no wrapper-created branch to clean up
        self.assertEqual(rec["base"], "origin/develop")

    def test_every_git_call_disables_fsmonitor(self):
        """fsmonitor can hang git in a daemon's checkout; a call without the flag is a hang, not a failure."""
        git = _FakeGit()
        rec = jw.create("j1", root=self.root, host=self.host, runner=git, now=NOW)
        jw.teardown({"worktree": rec}, runner=git, now=NOW)
        self.assertEqual(git.verbs(), ["fetch", "worktree add", "worktree remove"])
        for call in git.calls:
            self.assertEqual(list(call[0])[:3], ["git", "-c", "core.fsmonitor=false"])

    def test_git_runs_in_the_host_checkout(self):
        git = _FakeGit()
        jw.create("j1", root=self.root, host=self.host, runner=git, now=NOW)
        for call in git.calls:
            self.assertEqual(call[1], self.host)


class WhereItLives(_WorktreeTestCase):
    """The placement: a fixed-name sibling of the checkout, one subdirectory per job id."""

    def test_the_path_is_the_worktree_root_plus_the_job_id(self):
        git = _FakeGit()
        rec = jw.create("20260809-180000-abcd", root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(rec["path"], os.path.join(self.root, "20260809-180000-abcd"))
        self.assertIn(rec["path"], git.find("worktree add"))

    def test_the_default_root_is_a_sibling_of_the_repo_named_seneschal_worktrees(self):
        """Derived from the module's own location, never hardcoded — and one fixed-name parent, which
        is what makes `rag_projects`' exact-name exclusion able to keep job worktrees out of the
        project corpus."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(jw.WORKTREE_ROOT_ENV, None)
            root = jw.worktree_root()
        self.assertEqual(os.path.basename(root), "seneschal-worktrees")
        self.assertEqual(os.path.dirname(root), os.path.dirname(jw.REPO_ROOT))

    def test_the_root_is_overridable_by_environment(self):
        with mock.patch.dict(os.environ, {jw.WORKTREE_ROOT_ENV: self.root}):
            self.assertEqual(jw.worktree_path("j9"), os.path.join(self.root, "j9"))

    def test_the_root_directory_is_created_when_it_does_not_exist(self):
        self.assertFalse(os.path.isdir(self.root))
        jw.create("j1", root=self.root, host=self.host, runner=_FakeGit(), now=NOW)
        self.assertTrue(os.path.isdir(self.root))


class SetupFailureIsLoud(_WorktreeTestCase):
    """A worktree that cannot be created must stop the job — never silently fall back to a shared
    tree, which is the failure the whole spec exists to remove."""

    def test_a_failed_fetch_raises_and_nothing_is_added(self):
        """Fatal rather than a warning: the fetch is the only thing between this job and a cut from a
        stale base, and a stale base is silent — a worktree many commits behind measures branch drift
        instead of the change."""
        git = _FakeGit(fail={"fetch": (128, "fatal: unable to access 'https://…': timed out")})
        with self.assertRaises(jw.WorktreeError) as cm:
            jw.create("j1", root=self.root, host=self.host, runner=git, now=NOW)
        self.assertIn("timed out", str(cm.exception))
        self.assertNotIn("worktree add", git.verbs())

    def test_a_failed_add_raises_with_gits_own_message(self):
        git = _FakeGit(fail={"worktree add": (128, "fatal: 'origin/develop' is not a commit")})
        with self.assertRaises(jw.WorktreeError) as cm:
            jw.create("j1", root=self.root, host=self.host, runner=git, now=NOW)
        self.assertIn("is not a commit", str(cm.exception))

    def test_an_existing_path_is_refused_without_touching_git(self):
        """A directory already at a job's path is either a leak that still holds work or somebody
        else's tree. Reusing it would rebuild the shared-mutable-state failure one directory down."""
        os.makedirs(os.path.join(self.root, "j1"))
        git = _FakeGit()
        with self.assertRaises(jw.WorktreeError):
            jw.create("j1", root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(git.calls, [])

    def test_the_error_is_one_line_because_it_reaches_a_telegram_push(self):
        git = _FakeGit(fail={"worktree add": (128, "fatal: one\nfatal: two\nfatal: three")})
        with self.assertRaises(jw.WorktreeError) as cm:
            jw.create("j1", root=self.root, host=self.host, runner=git, now=NOW)
        self.assertNotIn("\n", str(cm.exception))


class NeverForce(_WorktreeTestCase):
    """THE line. `git worktree remove --force` deletes uncommitted work; plain `remove` refuses to,
    and the refusal is the whole safety mechanism."""

    def _remove(self, fail=None):
        path = os.path.join(self.root, "j1")
        os.makedirs(path)
        git = _FakeGit(fail=fail)
        result = jw.teardown({"worktree": {"path": path, "host": self.host}}, runner=git, now=NOW)
        return git, result

    def test_a_clean_removal_never_passes_force(self):
        git, result = self._remove()
        self.assertTrue(result["removed"])
        self.assertIn("worktree remove", git.verbs())
        for call in git.calls:
            self.assertNotIn("--force", git_args(call))
            self.assertNotIn("-f", git_args(call))

    def test_a_refused_removal_is_not_retried_with_force(self):
        git, result = self._remove(fail={"worktree remove": (1, "fatal: contains modified files")})
        self.assertTrue(result["leaked"])
        self.assertEqual(git.verbs().count("worktree remove"), 1)
        for call in git.calls:
            self.assertNotIn("--force", git_args(call))

    def test_the_sweep_never_forces_either(self):
        state = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(state, "jobs"))
        os.makedirs(os.path.join(self.root, "j1"))
        jobs.save_job(state, {"id": "j1", "status": jobs.DONE,
                              "notified_at": jobs._stamp(NOW - timedelta(days=30))})
        git = _FakeGit(fail={"worktree remove": (1, "fatal: contains modified files")})
        jw.sweep(state, days=14, root=self.root, host=self.host, runner=git, now=NOW)
        for call in git.calls:
            self.assertNotIn("--force", git_args(call))


class ARefusalIsRecordedNotSwallowed(_WorktreeTestCase):
    """A leak is 6.7 MB. Lost work is not recoverable. So the directory stays and the record says so
    — loudly enough for the completion push to quote it."""

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.root, "j1")
        os.makedirs(self.path)

    def test_a_dirty_tree_leaks_with_gits_reason_and_is_left_on_disk(self):
        git = _FakeGit(fail={"worktree remove":
                             (1, "fatal: 'j1' contains modified or untracked files, use --force")})
        result = jw.teardown({"worktree": {"path": self.path, "host": self.host}},
                             runner=git, now=NOW)
        self.assertTrue(result["leaked"])
        self.assertFalse(result["removed"])
        self.assertIn("modified or untracked", result["reason"])
        self.assertTrue(os.path.isdir(self.path))   # untouched, which is the point

    def test_the_reason_is_collapsed_to_one_line(self):
        """`notify_text` joins its parts with newlines, so an embedded newline would make the tail of
        a git error indistinguishable from the next part of the message (the cancel-attribution
        lesson, same fix)."""
        git = _FakeGit(fail={"worktree remove": (1, "fatal: line one\nline two\nline three")})
        result = jw.teardown({"worktree": {"path": self.path, "host": self.host}},
                             runner=git, now=NOW)
        self.assertNotIn("\n", result["reason"])

    def test_a_runner_that_raises_leaks_rather_than_raising(self):
        """Teardown sits upstream of the completion push. It may cost the reclaim; it may never cost
        the ping."""
        class _Broken:
            def run(self, *a, **k):
                raise OSError("git is not on PATH")

        result = jw.teardown({"worktree": {"path": self.path, "host": self.host}},
                             runner=_Broken(), now=NOW)
        self.assertTrue(result["leaked"])
        self.assertIn("git is not on PATH", result["reason"])
        self.assertTrue(os.path.isdir(self.path))

    def test_git_reports_no_stderr_and_the_exit_code_is_still_recorded(self):
        git = _FakeGit(fail={"worktree remove": (2, "")})
        result = jw.teardown({"worktree": {"path": self.path, "host": self.host}},
                             runner=git, now=NOW)
        self.assertTrue(result["leaked"])
        self.assertIn("2", result["reason"])


class TeardownShapes(_WorktreeTestCase):
    """The record shapes teardown has to survive, since it runs on every job the daemon finishes."""

    def test_a_job_with_no_worktree_is_a_no_op_and_runs_no_git(self):
        git = _FakeGit()
        self.assertIsNone(jw.teardown({"id": "j1"}, runner=git))
        self.assertIsNone(jw.teardown({"worktree": None}, runner=git))
        self.assertIsNone(jw.teardown({"worktree": {}}, runner=git))
        self.assertIsNone(jw.teardown({"worktree": {"path": "  "}}, runner=git))
        self.assertIsNone(jw.teardown("not a record", runner=git))
        self.assertEqual(git.calls, [])

    def test_an_already_absent_directory_is_not_a_leak_and_is_pruned(self):
        """`prune` is the right tool for exactly this case and no other: it drops admin records whose
        directory is already gone, and it is blind to a leaked directory forever (`prune --dry-run -v`
        reports nothing for registered trees that are still on disk)."""
        git = _FakeGit()
        result = jw.teardown({"worktree": {"path": os.path.join(self.root, "gone"),
                                           "host": self.host}}, runner=git, now=NOW)
        self.assertTrue(result["removed"])
        self.assertFalse(result["leaked"])
        self.assertEqual(git.verbs(), ["worktree prune"])
        self.assertIsNone(git.find("worktree remove"))


class SweepReclaims(_WorktreeTestCase):
    """The sweep ships tested but unwired (`worktree_gc.py` is the sweeper Dream runs). The rule it
    holds is `jobs.prune`'s, for the same reason: the GC must never be what makes a job silent."""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.state, "jobs"))

    def _job(self, job_id, **fields):
        os.makedirs(os.path.join(self.root, job_id), exist_ok=True)
        rec = {"id": job_id, "status": jobs.DONE,
               "notified_at": jobs._stamp(NOW - timedelta(days=30))}
        rec.update(fields)
        jobs.save_job(self.state, rec)
        return rec

    def test_a_finished_notified_old_job_is_reclaimed(self):
        self._job("j1")
        git = _FakeGit()
        out = jw.sweep(self.state, days=14, root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(out["removed"], ["j1"])
        self.assertIn(os.path.join(self.root, "j1"), git.find("worktree remove"))

    def test_a_still_running_job_keeps_its_worktree(self):
        self._job("j1", status=jobs.RUNNING, notified_at=None)
        git = _FakeGit()
        out = jw.sweep(self.state, days=14, root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(out["kept"], ["j1"])
        self.assertIsNone(git.find("worktree remove"))

    def test_a_terminal_job_that_never_got_its_push_is_kept(self):
        """The identical condition `jobs.prune --days` uses. A job whose ping hasn't landed is not
        finished with, whatever its status says."""
        self._job("j1", notified_at=None)
        git = _FakeGit()
        out = jw.sweep(self.state, days=14, root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(out["kept"], ["j1"])
        self.assertIsNone(git.find("worktree remove"))

    def test_a_recently_notified_job_is_under_the_age_floor(self):
        self._job("j1", notified_at=jobs._stamp(NOW - timedelta(days=1)))
        git = _FakeGit()
        out = jw.sweep(self.state, days=14, root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(out["kept"], ["j1"])

    def test_a_directory_with_no_record_is_counted_not_guessed_at(self):
        """Reclaim, don't guess: with no record there is nothing that says the job is over. Counting
        it is the answer here, so a pile-up is visible rather than merely present."""
        os.makedirs(os.path.join(self.root, "20260101-000000-zzzz"))
        git = _FakeGit()
        out = jw.sweep(self.state, days=14, root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(out["orphans"], ["20260101-000000-zzzz"])
        self.assertIsNone(git.find("worktree remove"))

    def test_a_refused_removal_is_reported_as_a_leak(self):
        self._job("j1")
        git = _FakeGit(fail={"worktree remove": (1, "fatal: contains modified files")})
        out = jw.sweep(self.state, days=14, root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(out["leaked"], ["j1"])
        self.assertEqual(out["removed"], [])

    def test_prune_runs_once_at_the_end(self):
        self._job("j1")
        git = _FakeGit()
        jw.sweep(self.state, days=14, root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(git.verbs()[-1], "worktree prune")
        self.assertEqual(git.verbs().count("worktree prune"), 1)

    def test_no_worktree_root_yet_is_the_normal_state_not_an_error(self):
        git = _FakeGit()
        out = jw.sweep(self.state, days=14, root=os.path.join(self.tmp.name, "nope"),
                       host=self.host, runner=git, now=NOW)
        self.assertEqual(out["removed"], [])
        self.assertEqual(git.calls, [])

    def test_a_stray_file_in_the_root_is_ignored(self):
        os.makedirs(self.root, exist_ok=True)
        with open(os.path.join(self.root, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("not a worktree\n")
        git = _FakeGit()
        out = jw.sweep(self.state, days=14, root=self.root, host=self.host, runner=git, now=NOW)
        self.assertEqual(out["orphans"], [])
        self.assertEqual(out["kept"], [])


if __name__ == "__main__":
    unittest.main()
