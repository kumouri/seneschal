#!/usr/bin/env python3
"""Tests for `worktree_gc.py` — the periodic sweeper for leaked job worktrees.

**Every fixture is built in a `TemporaryDirectory` and every git call goes through a fake runner.**
No test here touches the real worktree root, spawns git, or deletes anything outside its own temp
dir — the same discipline `test_job_worktree.py` holds, and here it is load-bearing twice over,
because the module under test deletes directories for a living.

What these are guarding, in one line: **the sweeper's value is its refusals.** A test that only
proved it removes things would pass on a `shutil.rmtree` of the whole root. So most of what follows
pins a *refusal*, and the removal tests exist mainly to prove the refusals aren't vacuous.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jobs                       # noqa: E402
import job_worktree as jw         # noqa: E402
import worktree_gc as wg          # noqa: E402


HOUR = 3600.0


class FakeRunner:
    """The `job_worktree.Runner` seam, answering from a script instead of spawning git.

    Records every argv, which is how `--force` is asserted against *behaviour* as well as against the
    source text — a `--force` that only ever appeared in a variable would pass a grep and fail here."""

    def __init__(self, registered=(), status="", unpushed="0", fail=()):
        self.registered = list(registered)
        self.status = status
        self.unpushed = unpushed
        self.fail = set(fail)           # any of: list, status, revlist, remove
        self.calls = []

    def _res(self, args, code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    def run(self, args, cwd=None, check=False):
        self.calls.append(list(args))
        a = list(args)
        if "list" in a and "--porcelain" in a:
            if "list" in self.fail:
                return self._res(a, 1, "", "fatal: not a git repository")
            return self._res(a, 0, "".join(f"worktree {p}\n\n" for p in self.registered))
        if "status" in a:
            if "status" in self.fail:
                return self._res(a, 1, "", "fatal: unable to read index")
            return self._res(a, 0, self.status)
        if "rev-list" in a:
            if "revlist" in self.fail:
                return self._res(a, 128, "", "fatal: bad revision 'HEAD'")
            return self._res(a, 0, self.unpushed)
        if "remove" in a:
            if "remove" in self.fail:
                return self._res(a, 1, "", "fatal: contains modified or untracked files")
            return self._res(a, 0, "")
        return self._res(a, 0, "")

    def argv_strings(self):
        return [" ".join(c) for c in self.calls]


class GCFixture(unittest.TestCase):
    """A worktree root, a job ledger, and a clock — all under one temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "seneschal-worktrees")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.root)
        os.makedirs(os.path.join(self.state, "jobs"))
        self.now = time.time()
        self.addCleanup(self.tmp.cleanup)

    # -- fixture builders ---------------------------------------------------

    def make_dir(self, name, *, files=None, dot_git=None, age_hours=48.0):
        """A directory under the root. `dot_git`: None = absent, "file" = a worktree, "dir" = a clone."""
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        for rel, body in (files or {}).items():
            full = os.path.join(path, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(body)
        if dot_git == "file":
            with open(os.path.join(path, ".git"), "w", encoding="utf-8") as fh:
                fh.write("gitdir: /somewhere/.git/worktrees/" + name + "\n")
        elif dot_git == "dir":
            os.makedirs(os.path.join(path, ".git"), exist_ok=True)
        old = self.now - age_hours * HOUR
        os.utime(path, (old, old))
        return path

    def make_job(self, job_id, *, status="done", notified=True, age_hours=48.0):
        rec = {"id": job_id, "status": status, "title": "t"}
        if notified:
            stamp = jw._utc_now().timestamp() - age_hours * HOUR
            from datetime import datetime, timezone
            rec["notified_at"] = (datetime.fromtimestamp(stamp, timezone.utc)
                                  .isoformat().replace("+00:00", "Z"))
        with open(os.path.join(self.state, "jobs", f"{job_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        return rec

    def sweep(self, *, runner=None, apply=False, min_age_hours=6.0):
        buf = io.StringIO()
        res = wg.sweep(self.root, state_dir=self.state, apply=apply,
                       min_age_hours=min_age_hours, host=self.tmp.name,
                       runner=runner or FakeRunner(), now=self.now, out=buf)
        res["_out"] = buf.getvalue()
        return res

    def names(self, result, bucket):
        return [v["name"] for v in result[bucket]]


class EmptyHuskTests(GCFixture):
    """Proof 1 — the common case, and the only one that can be proved without git."""

    def test_an_empty_husk_is_removed(self):
        self.make_dir("20260810-030141-118f", age_hours=200)
        self.make_job("20260810-030141-118f", status="cancelled", age_hours=200)
        res = self.sweep(apply=True)
        self.assertEqual(self.names(res, "removed"), ["20260810-030141-118f"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "20260810-030141-118f")))

    def test_a_husk_with_no_record_at_all_is_still_removed(self):
        """The one place this goes further than `job_worktree.sweep`, which reclaims no orphan.
        Proof 1 does not rest on the record: no git state and no files is nothing to lose."""
        self.make_dir("20260101-000000-aaaa", age_hours=500)
        res = self.sweep(apply=True)
        self.assertEqual(self.names(res, "removed"), ["20260101-000000-aaaa"])

    def test_a_husk_is_removed_with_rmdir_and_git_is_never_asked_to(self):
        self.make_dir("20260810-030141-118f", age_hours=200)
        r = FakeRunner()
        self.sweep(runner=r, apply=True)
        self.assertNotIn("remove", " ".join(r.argv_strings()).split(),
                         "an unregistered husk is not a worktree; git would fail on it")

    def test_a_NON_empty_dir_with_no_git_is_refused_there_is_no_near_empty(self):
        """A directory with no `.git` and files left in it is a removal that stopped halfway;
        which half survived is exactly what is not provable from here."""
        self.make_dir("20260820-230408-5443", files={"seneschal/SKILL.md": "x"}, age_hours=200)
        self.make_job("20260820-230408-5443", age_hours=200)
        res = self.sweep(apply=True)
        self.assertEqual(self.names(res, "refused"), ["20260820-230408-5443"])
        self.assertIn("stopped halfway", res["refused"][0]["reason"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "20260820-230408-5443")))

    def test_rmdir_refuses_a_directory_that_filled_up_after_the_check(self):
        """The TOCTOU close: the OS re-proves emptiness at the instant of deletion. If `remove_one`
        ever became `shutil.rmtree`, this is the test that breaks."""
        path = self.make_dir("20260101-000000-aaaa", age_hours=500)
        verdict = {"path": path, "how": "rmdir", "name": "x"}
        with open(os.path.join(path, "late-arrival.txt"), "w", encoding="utf-8") as fh:
            fh.write("work that appeared between the check and the act")
        ok, why = wg.remove_one(verdict, runner=FakeRunner(), host=self.tmp.name)
        self.assertFalse(ok)
        self.assertTrue(os.path.exists(os.path.join(path, "late-arrival.txt")))
        self.assertTrue(why)


class RegisteredWorktreeTests(GCFixture):
    """Proof 2 — clean AND fully pushed. Either half failing is a refusal."""

    def _registered(self, name, **kw):
        path = self.make_dir(name, dot_git="file", files={"README.md": "x"}, **kw)
        self.make_job(name, **({"age_hours": kw["age_hours"]} if "age_hours" in kw else {}))
        return path

    def test_clean_and_pushed_is_removed_via_git(self):
        path = self._registered("20260813-152008-2fc4", age_hours=200)
        r = FakeRunner(registered=[path], status="", unpushed="0")
        res = self.sweep(runner=r, apply=True)
        self.assertEqual(self.names(res, "removed"), ["20260813-152008-2fc4"])
        self.assertTrue(any("worktree remove" in s for s in r.argv_strings()))

    def test_uncommitted_changes_are_refused(self):
        path = self._registered("20260813-152008-2fc4", age_hours=200)
        r = FakeRunner(registered=[path], status=" M seneschal/scripts/jobs.py\n?? scratch.txt\n")
        res = self.sweep(runner=r, apply=True)
        self.assertEqual(self.names(res, "refused"), ["20260813-152008-2fc4"])
        self.assertIn("uncommitted", res["refused"][0]["reason"])
        self.assertFalse(any("worktree remove" in s for s in r.argv_strings()))

    def test_untracked_files_alone_are_uncommitted_changes(self):
        path = self._registered("20260813-152008-2fc4", age_hours=200)
        r = FakeRunner(registered=[path], status="?? only-untracked.txt\n")
        res = self.sweep(runner=r, apply=True)
        self.assertEqual(self.names(res, "refused"), ["20260813-152008-2fc4"])

    def test_unpushed_commits_are_refused(self):
        """A clean worktree holding a commit that exists in no other ref. THE CHECK PLAIN
        `git worktree remove` DOES NOT DO — it would remove this."""
        path = self._registered("20260810-120000-bbbb", age_hours=200)
        r = FakeRunner(registered=[path], status="", unpushed="1")
        res = self.sweep(runner=r, apply=True)
        self.assertEqual(self.names(res, "refused"), ["20260810-120000-bbbb"])
        self.assertIn("nowhere else", res["refused"][0]["reason"])
        self.assertFalse(any("worktree remove" in s for s in r.argv_strings()))

    def test_a_registered_worktree_with_no_job_record_is_refused_as_an_orphan(self):
        path = self.make_dir("orphan-branch-tree", dot_git="file", files={"a": "b"}, age_hours=300)
        r = FakeRunner(registered=[path], status="", unpushed="0")
        res = self.sweep(runner=r, apply=True)
        self.assertEqual(self.names(res, "refused"), ["orphan-branch-tree"])
        self.assertIn("no job record", res["refused"][0]["reason"])

    def test_a_dot_git_file_that_git_does_not_list_is_refused(self):
        self.make_dir("20260813-152008-2fc4", dot_git="file", files={"a": "b"}, age_hours=200)
        self.make_job("20260813-152008-2fc4", age_hours=200)
        res = self.sweep(runner=FakeRunner(registered=[]), apply=True)
        self.assertEqual(self.names(res, "refused"), ["20260813-152008-2fc4"])
        self.assertIn("does not list it", res["refused"][0]["reason"])


class RunningJobTests(GCFixture):
    """Never race a live job."""

    def test_a_running_jobs_worktree_is_held(self):
        path = self.make_dir("20260821-185442-ae01", dot_git="file", files={"a": "b"}, age_hours=200)
        self.make_job("20260821-185442-ae01", status="running", notified=False, age_hours=200)
        r = FakeRunner(registered=[path], status="", unpushed="0")
        res = self.sweep(runner=r, apply=True)
        self.assertEqual(self.names(res, "held"), ["20260821-185442-ae01"])
        self.assertIn("still running", res["held"][0]["reason"])
        self.assertFalse(any("worktree remove" in s for s in r.argv_strings()))

    def test_a_terminal_job_whose_push_has_not_landed_is_held(self):
        """`jobs.prune`'s own condition, for its own reason: the GC may never be what makes a job go
        silent, and the completion push quotes the leaked path."""
        path = self.make_dir("20260813-152008-2fc4", dot_git="file", files={"a": "b"}, age_hours=200)
        self.make_job("20260813-152008-2fc4", status="failed", notified=False)
        r = FakeRunner(registered=[path], status="", unpushed="0")
        res = self.sweep(runner=r, apply=True)
        self.assertEqual(self.names(res, "held"), ["20260813-152008-2fc4"])
        self.assertIn("has not landed", res["held"][0]["reason"])

    def test_an_absent_ledger_refuses_the_WHOLE_run(self):
        """Without the ledger a running job's tree reads as an orphan — and a running job's tree is
        usually clean and fully pushed, i.e. it would otherwise satisfy proof 2. A state dir passed
        one level too deep makes every record read None."""
        path = self.make_dir("20260821-185442-ae01", dot_git="file", age_hours=200)
        buf = io.StringIO()
        res = wg.sweep(self.root, state_dir=os.path.join(self.state, "jobs"), apply=True,
                       host=self.tmp.name, runner=FakeRunner(registered=[path]),
                       now=self.now, out=buf)
        self.assertTrue(res["error"])
        self.assertEqual(res["removed"], [])
        self.assertIn("REFUSING THE WHOLE RUN", buf.getvalue())


class AgeFloorTests(GCFixture):

    def test_a_young_husk_is_held(self):
        self.make_dir("20260821-185011-e62b", age_hours=0.2)
        self.make_job("20260821-185011-e62b", status="cancelled", age_hours=0.2)
        res = self.sweep(apply=True, min_age_hours=6.0)
        self.assertEqual(self.names(res, "held"), ["20260821-185011-e62b"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "20260821-185011-e62b")))

    def test_the_floor_is_configurable_and_the_same_dir_then_sweeps(self):
        self.make_dir("20260821-185011-e62b", age_hours=0.2)
        self.make_job("20260821-185011-e62b", status="cancelled", age_hours=0.2)
        res = self.sweep(apply=True, min_age_hours=0.1)
        self.assertEqual(self.names(res, "removed"), ["20260821-185011-e62b"])

    def test_age_is_the_YOUNGEST_evidence_not_the_oldest(self):
        """An old record does not make a directory old. Anything still being written to is young."""
        self.make_dir("20260101-000000-aaaa", age_hours=0.5)
        self.make_job("20260101-000000-aaaa", age_hours=900)
        res = self.sweep(apply=True, min_age_hours=6.0)
        self.assertEqual(self.names(res, "held"), ["20260101-000000-aaaa"])

    def test_an_unparseable_notified_stamp_reads_as_young(self):
        self.make_dir("20260101-000000-aaaa", age_hours=900)
        with open(os.path.join(self.state, "jobs", "20260101-000000-aaaa.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"id": "20260101-000000-aaaa", "status": "done",
                       "notified_at": "not-a-timestamp"}, fh)
        res = self.sweep(apply=True)
        self.assertEqual(self.names(res, "held"), ["20260101-000000-aaaa"])


class ProtectedPathTests(GCFixture):
    """Four independent rules, each surviving a different mistake."""

    def test_the_protected_names_derive_from_the_host_checkout(self):
        """Never a hardcoded name — an install can be cloned under any directory name."""
        host = os.path.basename(os.path.normpath(jw.REPO_ROOT))
        self.assertEqual(wg.PROTECTED_BASENAMES, (host, host + "-dev"))

    def test_the_daemon_checkout_and_its_dev_sibling_are_refused_by_name(self):
        for name in wg.PROTECTED_BASENAMES:
            with self.subTest(name=name):
                reason = wg.protection_refusal(os.path.join(self.root, name), self.root, mine=[])
                self.assertIn("protected", reason)

    def test_anything_under_claude_worktrees_is_refused(self):
        p = os.path.join(self.root, ".claude", "worktrees", "agent-abc")
        self.assertIn("harness", wg.protection_refusal(p, self.root, mine=[]))

    def test_a_path_outside_the_root_is_refused(self):
        outside = os.path.join(self.tmp.name, "repos", "seneschal")
        self.assertIn("outside", wg.protection_refusal(outside, self.root, mine=[]))

    def test_a_sibling_root_with_a_shared_prefix_is_outside(self):
        """`commonpath`, not `startswith` — else `seneschal-worktrees-old` is a child of the root."""
        self.assertIn("outside", wg.protection_refusal(self.root + "-old", self.root, mine=[]))

    def test_the_root_itself_is_never_a_candidate(self):
        self.assertIn("outside", wg.protection_refusal(self.root, self.root, mine=[]))

    def test_a_dot_git_DIRECTORY_means_a_clone_and_is_refused(self):
        path = self.make_dir("some-clone", dot_git="dir", age_hours=500)
        self.assertIn("clone", wg.protection_refusal(path, self.root, mine=[]))

    def test_the_sweepers_OWN_checkout_is_refused(self):
        """A sweeper that can delete its own working directory is one --root typo from an incident."""
        path = self.make_dir("20260821-185841-1384", dot_git="file", age_hours=500)
        reason = wg.protection_refusal(path, self.root, mine=[path])
        self.assertIn("running from", reason)

    def test_a_parent_of_the_sweepers_checkout_is_refused_too(self):
        path = self.make_dir("20260821-185841-1384", age_hours=500)
        inner = os.path.join(path, "nested", "checkout")
        self.assertIn("running from", wg.protection_refusal(path, self.root, mine=[inner]))

    def test_protection_beats_every_other_verdict(self):
        """Ordering: nothing about a protected path is even measured."""
        dev = wg.PROTECTED_BASENAMES[1]
        path = self.make_dir(dev, age_hours=500)      # empty: would otherwise be a husk
        res = self.sweep(apply=True)
        self.assertEqual(self.names(res, "refused"), [dev])
        self.assertTrue(os.path.exists(path))


class ErrorIsARefusalTests(GCFixture):
    """An error is a refusal, never a green light — on every path that can produce one."""

    def _reg(self, name="20260813-152008-2fc4"):
        path = self.make_dir(name, dot_git="file", files={"a": "b"}, age_hours=200)
        self.make_job(name, age_hours=200)
        return path

    def test_a_failing_status_is_a_refusal(self):
        path = self._reg()
        r = FakeRunner(registered=[path], fail=("status",))
        res = self.sweep(runner=r, apply=True)
        self.assertEqual(len(res["refused"]), 1)
        self.assertIn("git status failed", res["refused"][0]["reason"])

    def test_a_failing_rev_list_is_a_refusal(self):
        path = self._reg()
        r = FakeRunner(registered=[path], fail=("revlist",))
        res = self.sweep(runner=r, apply=True)
        self.assertIn("git rev-list failed", res["refused"][0]["reason"])

    def test_an_unreadable_commit_count_is_a_refusal(self):
        path = self._reg()
        r = FakeRunner(registered=[path], unpushed="not a number")
        res = self.sweep(runner=r, apply=True)
        self.assertIn("could not read a commit count", res["refused"][0]["reason"])

    def test_a_runner_that_raises_is_a_refusal(self):
        path = self._reg()

        class Exploding(FakeRunner):
            def run(self, args, cwd=None, check=False):
                if "status" in list(args):
                    raise OSError("the seam broke")
                return super().run(args, cwd=cwd, check=check)

        res = self.sweep(runner=Exploding(registered=[path]), apply=True)
        self.assertEqual(len(res["refused"]), 1)
        self.assertIn("could not run git status", res["refused"][0]["reason"])

    def test_a_failing_worktree_list_refuses_the_WHOLE_run(self):
        """Without the registration list a husk cannot be told from a worktree, and guessing that
        difference is the judgement this module exists to avoid."""
        path = self._reg()
        res = self.sweep(runner=FakeRunner(registered=[path], fail=("list",)), apply=True)
        self.assertTrue(res["error"])
        self.assertEqual(res["removed"], [])
        self.assertIn("REFUSING THE WHOLE RUN", res["_out"])

    def test_a_git_removal_that_fails_is_reported_as_a_refusal_not_a_removal(self):
        path = self._reg()
        r = FakeRunner(registered=[path], fail=("remove",))
        res = self.sweep(runner=r, apply=True)
        self.assertEqual(res["removed"], [])
        self.assertIn("removal refused", res["refused"][0]["reason"])

    def test_an_unreadable_directory_is_not_an_empty_one(self):
        """`_entry_count` answers -1, which must not satisfy the empty-husk proof."""
        v = wg.classify(os.path.join(self.root, "does-not-exist"), root=self.root,
                        registered=set(), rec=None, runner=FakeRunner(),
                        min_age_hours=0.0, now=self.now, mine=[], terminal=jobs.TERMINAL)
        self.assertEqual(v["verdict"], wg.REFUSE)
        self.assertIn("could not be read", v["reason"])


class NeverForceTests(GCFixture):
    """`--force` must appear nowhere — in the source, or in anything the runner is handed."""

    def test_the_source_contains_no_force_flag(self):
        with open(wg.__file__, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("--force", source.replace("never `--force`", "")
                                          .replace("never --force", "")
                                          .replace("`--force` appears nowhere", "")
                                          .replace("`--force` must appear nowhere", "")
                                          .replace("forget the `--force` rule", ""),
                         "--force must never be emitted by this module")

    def test_no_git_call_ever_carries_force(self):
        path = self.make_dir("20260813-152008-2fc4", dot_git="file", files={"a": "b"},
                             age_hours=200)
        self.make_job("20260813-152008-2fc4", age_hours=200)
        r = FakeRunner(registered=[path])
        self.sweep(runner=r, apply=True)
        for call in r.calls:
            self.assertNotIn("--force", call)
            self.assertNotIn("-f", call)

    def test_the_removal_path_is_job_worktrees_teardown(self):
        """One implementation of remove-without-force, shared with the per-job path. A second copy
        here would be the copy that could forget the rule."""
        path = self.make_dir("20260813-152008-2fc4", dot_git="file", files={"a": "b"},
                             age_hours=200)
        self.make_job("20260813-152008-2fc4", age_hours=200)
        seen = []
        original = jw.teardown
        jw.teardown = lambda rec, **kw: (seen.append(rec) or original(rec, **kw))
        try:
            self.sweep(runner=FakeRunner(registered=[path]), apply=True)
        finally:
            jw.teardown = original
        self.assertEqual(len(seen), 1)


class QuietWhenThereIsNothingToSayTests(GCFixture):
    """A nightly worker that pings the owner about routine holds is worse than no worker."""

    def test_holds_alone_do_not_count_as_news(self):
        self.make_dir("20260821-185011-e62b", age_hours=0.2)
        self.make_job("20260821-185011-e62b", status="cancelled", age_hours=0.2)
        res = self.sweep(apply=True)
        self.assertFalse(wg.has_news(res))
        self.assertIn("nothing to report", res["_out"])

    def test_a_removal_is_news(self):
        self.make_dir("20260101-000000-aaaa", age_hours=500)
        res = self.sweep(apply=True)
        self.assertTrue(wg.has_news(res))

    def test_a_refusal_is_news(self):
        self.make_dir("20260820-230408-5443", files={"a": "b"}, age_hours=500)
        res = self.sweep(apply=True)
        self.assertTrue(wg.has_news(res))

    def test_an_empty_root_is_quiet_and_not_an_error(self):
        res = self.sweep(apply=True)
        self.assertFalse(wg.has_news(res))
        self.assertEqual(res["error"], "")

    def test_an_absent_root_is_not_an_error(self):
        buf = io.StringIO()
        res = wg.sweep(os.path.join(self.tmp.name, "never-existed"), state_dir=self.state,
                       host=self.tmp.name, runner=FakeRunner(), now=self.now, out=buf)
        self.assertEqual(res["error"], "")
        self.assertFalse(wg.has_news(res))


class DryRunTests(GCFixture):
    """Dry run is the DEFAULT, because a swept checkout is not regenerable from anything."""

    def test_without_apply_nothing_is_deleted(self):
        path = self.make_dir("20260101-000000-aaaa", age_hours=500)
        res = self.sweep(apply=False)
        self.assertEqual(self.names(res, "removed"), ["20260101-000000-aaaa"])
        self.assertTrue(os.path.exists(path), "dry run must not delete")
        self.assertIn("would remove", res["_out"])

    def test_a_dry_run_makes_no_git_removal_call(self):
        path = self.make_dir("20260813-152008-2fc4", dot_git="file", files={"a": "b"},
                             age_hours=200)
        self.make_job("20260813-152008-2fc4", age_hours=200)
        r = FakeRunner(registered=[path])
        self.sweep(runner=r, apply=False)
        self.assertFalse(any("worktree remove" in s for s in r.argv_strings()))
        self.assertFalse(any("worktree prune" in s for s in r.argv_strings()))


class CliTests(GCFixture):

    def _main(self, argv):
        buf = io.StringIO()
        err = io.StringIO()
        out, sys.stdout = sys.stdout, buf
        e, sys.stderr = sys.stderr, err
        try:
            code = wg.main(argv)
        finally:
            sys.stdout, sys.stderr = out, e
        return code, buf.getvalue(), err.getvalue()

    def test_default_is_a_dry_run(self):
        path = self.make_dir("20260101-000000-aaaa", age_hours=500)
        code, out, _ = self._main(["--root", self.root, "--state-dir", self.state])
        self.assertEqual(code, 0)
        self.assertIn("would remove", out)
        self.assertTrue(os.path.exists(path))

    def test_apply_and_dry_run_together_are_refused_rather_than_ranked(self):
        code, _, err = self._main(["--root", self.root, "--state-dir", self.state,
                                   "--apply", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("contradict", err)

    def test_a_negative_age_floor_is_refused(self):
        code, _, err = self._main(["--root", self.root, "--min-age-hours", "-1"])
        self.assertEqual(code, 2)
        self.assertIn("must not be negative", err)

    def test_finding_nothing_is_exit_zero(self):
        code, out, _ = self._main(["--root", self.root, "--state-dir", self.state])
        self.assertEqual(code, 0, "a GC that exits non-zero on a quiet night trains everyone to "
                                  "ignore its exit code")

    def test_a_real_error_is_exit_one(self):
        code, _, _ = self._main(["--root", self.root,
                                 "--state-dir", os.path.join(self.state, "jobs")])
        self.assertEqual(code, 1)

    def test_json_output_is_parseable(self):
        self.make_dir("20260101-000000-aaaa", age_hours=500)
        code, out, _ = self._main(["--root", self.root, "--state-dir", self.state, "--json"])
        payload = json.loads(out[out.index("{"):])
        self.assertEqual([v["name"] for v in payload["removed"]], ["20260101-000000-aaaa"])


class RootResolutionTests(unittest.TestCase):
    """The silent no-op found on this script's own first run."""

    def test_an_explicit_root_wins(self):
        self.assertEqual(wg.resolve_root("some/where"), os.path.abspath("some/where"))

    def test_running_from_inside_a_worktree_resolves_to_the_pile_not_a_child_of_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, jw.WORKTREE_DIRNAME)
            here = os.path.join(root, "20260821-185841-1384")
            os.makedirs(here)
            original, env = jw.REPO_ROOT, os.environ.pop(jw.WORKTREE_ROOT_ENV, None)
            jw.REPO_ROOT = here
            try:
                self.assertEqual(wg._norm(wg.resolve_root()), wg._norm(root))
            finally:
                jw.REPO_ROOT = original
                if env is not None:
                    os.environ[jw.WORKTREE_ROOT_ENV] = env

    def test_running_from_a_normal_checkout_keeps_the_derived_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            here = os.path.join(tmp, "repos", "seneschal")
            os.makedirs(here)
            original, env = jw.REPO_ROOT, os.environ.pop(jw.WORKTREE_ROOT_ENV, None)
            jw.REPO_ROOT = here
            try:
                self.assertEqual(wg._norm(wg.resolve_root()),
                                 wg._norm(os.path.join(tmp, "repos", jw.WORKTREE_DIRNAME)))
            finally:
                jw.REPO_ROOT = original
                if env is not None:
                    os.environ[jw.WORKTREE_ROOT_ENV] = env


if __name__ == "__main__":
    unittest.main()
