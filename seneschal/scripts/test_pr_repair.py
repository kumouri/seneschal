"""Tests for `pr_repair.py` + `pr_rebase.py` — the PR auto-repair pass (concurrent-pr-collisions-spec
§5, §5A, §5A.9). Every `gh`, job launch and Telegram send is faked; no test reaches the network.

The target repository and base branch are configuration (`repo_config`); this module pins them to
`example/repo` / `develop` for every test (`setUpModule`), so no verdict depends on this checkout's
own remotes. Quiet hours are pinned the same way — `merge_guard.in_quiet_hours` is replaced by a
predicate that is true only at the injected `THREE_AM` instant — so no test depends on the owner's
configured timezone."""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pr_rebase  # noqa: E402
import pr_repair  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
NOON = datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)
THREE_AM = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)

_PATCHES = [
    mock.patch.object(pr_repair.repo_config, "origin_repo", return_value="example/repo"),
    mock.patch.object(pr_repair.repo_config, "base_branch", return_value="develop"),
    mock.patch.object(pr_repair.mg, "in_quiet_hours", lambda now=None: now == THREE_AM),
]


def setUpModule():
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()


def _presence_wires_pr_repair() -> bool:
    """Does this checkout's `presence.py` call `pr_repair.sweep` from its PR-watch task yet? Read from
    the source rather than imported, so collecting this module never pays presence's import cost."""
    try:
        with open(os.path.join(HERE, "presence.py"), encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return False
    return "pr_repair" in src and "def pr_watch_task" in src
BASE1 = "b" * 40
BASE2 = "c" * 40
HEAD = "a" * 40
GREEN = [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]


def row(pr=860, mergeable="CONFLICTING", state="DIRTY", head=HEAD, **kw):
    r = {"number": pr, "headRefOid": head, "headRefName": f"feat/x{pr}", "baseRefName": "develop",
         "isDraft": False, "mergeable": mergeable, "mergeStateStatus": state,
         "statusCheckRollup": GREEN, "title": f"feat: pr {pr}",
         "url": f"https://github.com/example/repo/pull/{pr}",
         "isCrossRepository": False}
    r.update(kw)
    return r


class FakeGh:
    """Dispatches on argv. `rows` is the list; `fresh` overrides the fresh view per PR."""

    def __init__(self, rows, base=BASE1, fresh=None, graphql=None, ready_code=0, ready_err=""):
        self.rows, self.base, self.fresh = rows, base, fresh or {}
        self.graphql = graphql or {"data": {"updatePullRequestBranch": {
            "pullRequest": {"headRefOid": "d" * 40}}}}
        self.ready_code, self.ready_err = ready_code, ready_err
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(self.rows), ""
        if argv[:2] == ["gh", "api"] and argv[2].endswith("/git/ref/heads/develop"):
            return 0, self.base + "\n", ""
        if argv[:3] == ["gh", "pr", "view"]:
            pr = int(argv[3])
            r = next(x for x in self.rows if x["number"] == pr)
            if "id,headRefOid" in argv:
                return 0, json.dumps({"id": f"PR_{pr}", "headRefOid": r["headRefOid"]}), ""
            view = {"mergeable": r["mergeable"], "mergeStateStatus": r["mergeStateStatus"],
                    "headRefOid": r["headRefOid"], "state": "OPEN"}
            view.update(self.fresh.get(pr, {}))
            return 0, json.dumps(view), ""
        if argv[:3] == ["gh", "pr", "ready"]:
            return self.ready_code, "", self.ready_err
        if argv[:3] == ["gh", "api", "graphql"]:
            return 0, json.dumps(self.graphql), ""
        raise AssertionError(f"unexpected call {argv}")

    def updates(self):
        return [c for c in self.calls if c[:3] == ["gh", "api", "graphql"]]


class Harness(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.launches, self.sent = [], []
        self.launch_result = {"ok": True, "job_id": "job-1"}
        self.paths = ["seneschal/context-budget.json"]
        self.approval = "no approval on file for this PR"   # verify_approval refusing = no live tap
        self.store = {"questions": {}}

    def launcher(self, row_, base, paths):
        self.launches.append((row_["number"], base, tuple(paths)))
        return dict(self.launch_result)

    def sender(self, text):
        self.sent.append(text)
        return {"ok": True}

    def run_pass(self, gh, now=NOON, **kw):
        return pr_repair.sweep(
            self.state, now=now, runner=gh, classifier=lambda base, head: list(self.paths),
            launcher=self.launcher, sender=self.sender,
            verifier=lambda sd, pr, head, repo: self.approval,
            store_loader=lambda: self.store, **kw)

    def log(self):
        return pr_repair.read_log(self.state)


class ConflictRepairTest(Harness):
    def test_conflicted_pr_gets_exactly_one_job_per_base_sha(self):
        gh = FakeGh([row()])
        self.run_pass(gh)
        self.run_pass(gh)
        self.run_pass(gh)
        self.assertEqual(self.launches, [(860, BASE1, ("seneschal/context-budget.json",))])
        self.assertEqual(len([r for r in self.log() if r["event"] == "repair_launched"]), 1)

    def test_a_new_base_sha_is_a_new_repair(self):
        self.run_pass(FakeGh([row()], base=BASE1))
        self.run_pass(FakeGh([row()], base=BASE2))
        self.assertEqual([b for _, b, _ in self.launches], [BASE1, BASE2])

    def test_live_approval_does_not_stop_a_conflict_repair_spec_5A3_clause_4(self):
        """§5A.3 clause 4: an approval pinned to a CONFLICTING head can never be spent, so moving that
        head destroys nothing. This is deliberate, not a bug — do not 'fix' it."""
        self.approval = ""  # a live approval at this head
        self.store = {"questions": {"q1": {"meta": {"kind": "merge-approval", "pr": 860,
                                                    "repo": pr_repair.REPO, "head_sha": HEAD}}}}
        self.run_pass(FakeGh([row()]))
        self.assertEqual(len(self.launches), 1)
        detected = [r for r in self.log() if r["event"] == "detected"]
        self.assertTrue(detected[0]["approval_live_at_detection"])
        self.assertTrue(detected[0]["rebaser_live"])

    def test_code_conflict_is_one_notice_and_no_job(self):
        self.paths = ["seneschal/scripts/pr_sweep.py", "cockpit/server/app.py"]
        gh = FakeGh([row()])
        self.run_pass(gh)
        self.run_pass(gh)
        self.assertEqual(self.launches, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("#860", self.sent[0])
        self.assertIn("seneschal/scripts/pr_sweep.py", self.sent[0])
        self.assertIn("allow-list", self.sent[0])

    def test_markdown_only_conflict_gets_a_repair_job(self):
        """§5A.9: a Markdown conflict is repair-eligible (resolved by keeping both sides)."""
        self.paths = ["seneschal/docs/rulings.md", "seneschal/scripts/CLAUDE.md"]
        gh = FakeGh([row()])
        self.run_pass(gh)
        self.run_pass(gh)
        self.assertEqual(self.launches,
                         [(860, BASE1, ("seneschal/docs/rulings.md", "seneschal/scripts/CLAUDE.md"))])
        self.assertEqual(self.sent, [])

    def test_markdown_plus_ledger_conflict_gets_a_repair_job(self):
        self.paths = ["seneschal/context-budget.json", "seneschal/docs/rulings.md"]
        self.run_pass(FakeGh([row()]))
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.sent, [])

    def test_markdown_plus_code_conflict_is_refused(self):
        self.paths = ["seneschal/docs/rulings.md", "seneschal/scripts/pr_repair.py"]
        self.run_pass(FakeGh([row()]))
        self.assertEqual(self.launches, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("seneschal/scripts/pr_repair.py", self.sent[0])

    def test_config_conflict_is_refused(self):
        self.paths = [".github/workflows/ci.yml"]
        self.run_pass(FakeGh([row()]))
        self.assertEqual(self.launches, [])
        self.assertEqual(len(self.sent), 1)

    def test_deleted_markdown_is_a_notice_not_a_job(self):
        def classify(base, head):
            return ["seneschal/docs/old-spec.md"], ["seneschal/docs/old-spec.md"]
        pr_repair.sweep(self.state, now=NOON, runner=FakeGh([row()]), classifier=classify,
                        launcher=self.launcher, sender=self.sender,
                        verifier=lambda *a: self.approval, store_loader=lambda: self.store)
        self.assertEqual(self.launches, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("delete or rename", self.sent[0])

    def test_unclassifiable_conflict_is_told_not_guessed(self):
        def boom(base, head):
            raise pr_repair.CannotTell("compare 404")
        gh = FakeGh([row()])
        pr_repair.sweep(self.state, now=NOON, runner=gh, classifier=boom,
                        launcher=self.launcher, sender=self.sender,
                        verifier=lambda *a: self.approval, store_loader=lambda: self.store)
        self.assertEqual(self.launches, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("couldn't work out", self.sent[0])

    def test_stale_conflicting_is_confirmed_by_a_fresh_read(self):
        gh = FakeGh([row()], fresh={860: {"mergeable": "MERGEABLE"}})
        self.run_pass(gh)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.sent, [])

    def test_clean_three_way_merge_means_github_was_stale(self):
        self.paths = []
        self.run_pass(FakeGh([row()]))
        self.assertEqual((self.launches, self.sent), ([], []))

    def test_capped_launch_records_nothing_and_retries_next_pass(self):
        self.launch_result = {"ok": False, "capped": True}
        gh = FakeGh([row()])
        report = self.run_pass(gh)
        self.assertTrue(any("jobs cap" in d for d in report["deferred"]))
        self.assertFalse([r for r in self.log() if r["event"] == "repair_launched"])
        self.launch_result = {"ok": True, "job_id": "job-2"}
        self.run_pass(gh)
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(len([r for r in self.log() if r["event"] == "repair_launched"]), 1)

    def test_quiet_hours_defer_launch_and_notice(self):
        report = self.run_pass(FakeGh([row()]), now=THREE_AM)
        self.assertEqual((self.launches, self.sent), ([], []))
        self.assertTrue(any("quiet" in d for d in report["deferred"]))

    def test_one_launch_per_pass(self):
        self.run_pass(FakeGh([row(860), row(861)]))
        self.assertEqual([p for p, _, _ in self.launches], [860])

    def test_drafts_forks_and_other_bases_are_ignored(self):
        rows = [row(1, isDraft=True), row(2, isCrossRepository=True), row(3, baseRefName="master")]
        self.run_pass(FakeGh(rows))
        self.assertEqual((self.launches, self.sent), ([], []))

    def test_unknown_mergeable_does_nothing(self):
        gh = FakeGh([row(mergeable="UNKNOWN", state="UNKNOWN")])
        self.run_pass(gh)
        self.assertEqual((self.launches, self.sent, gh.updates()), ([], [], []))

    def test_off_sentinel_disables_the_pass(self):
        open(os.path.join(self.state, pr_repair.OFF_SENTINEL), "w").close()
        gh = FakeGh([row()])
        report = self.run_pass(gh)
        self.assertTrue(report["disabled"])
        self.assertEqual(gh.calls, [])

    def test_unreadable_list_is_an_error_not_a_crash(self):
        report = pr_repair.sweep(self.state, now=NOON,
                                 runner=lambda argv: (1, "", "HTTP 502"))
        self.assertTrue(report["errors"])


class InFlightPrIsDraftedNotRepairedTest(Harness):
    """A PR whose branch is checked out in a RUNNING job's worktree gets converted to a draft and
    skipped entirely — never classified, never rebased, never repaired (`job_pr_draft.py`, the spec's
    §5B): a job that opens its PR early and keeps pushing must not have a repair launched against a
    head it is still moving."""

    def inflight(self, r, job_id="job-1"):
        return mock.patch.object(pr_repair.job_pr_draft, "list_running_worktree_branches",
                                 return_value={r["headRefName"]: job_id})

    def test_a_conflicting_inflight_pr_is_drafted_instead_of_repaired(self):
        r = row(860, mergeable="CONFLICTING", state="DIRTY")
        gh = FakeGh([r])
        with self.inflight(r):
            report = self.run_pass(gh)
        self.assertEqual(report["drafted"], ["#860 (job job-1)"])
        self.assertEqual(report["launched"], [])
        self.assertEqual(report["noticed"], [])
        self.assertEqual(self.launches, [])
        undo_calls = [c for c in gh.calls if c[:3] == ["gh", "pr", "ready"] and "--undo" in c]
        self.assertEqual(len(undo_calls), 1)
        self.assertEqual(undo_calls[0][3], "860")

    def test_a_behind_inflight_pr_is_drafted_instead_of_rebased(self):
        r = row(860, mergeable="MERGEABLE", state="BEHIND")
        gh = FakeGh([r])
        with self.inflight(r):
            report = self.run_pass(gh)
        self.assertEqual(report["drafted"], ["#860 (job job-1)"])
        self.assertEqual(report["rebased"], [])
        self.assertEqual(gh.updates(), [])

    def test_the_store_records_the_drafted_pr_keyed_to_its_job(self):
        r = row(860, mergeable="CONFLICTING", state="DIRTY")
        gh = FakeGh([r])
        with self.inflight(r):
            self.run_pass(gh)
        entries = pr_repair.job_pr_draft.entries_for_job(self.state, "job-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["pr"], 860)
        self.assertEqual(entries[0]["repo"], pr_repair.REPO)
        self.assertEqual(entries[0]["branch"], r["headRefName"])

    def test_a_failed_draft_attempt_is_deferred_not_silently_dropped(self):
        r = row(860, mergeable="CONFLICTING", state="DIRTY")
        gh = FakeGh([r], ready_code=1, ready_err="gh: not authorized")
        with self.inflight(r):
            report = self.run_pass(gh)
        self.assertEqual(report["drafted"], [])
        self.assertTrue(any("draft attempt failed" in d for d in report["deferred"]))
        self.assertEqual(pr_repair.job_pr_draft.entries_for_job(self.state, "job-1"), [])

    def test_a_pr_on_a_different_branch_than_any_running_job_is_untouched(self):
        r = row(860, mergeable="CONFLICTING", state="DIRTY")
        gh = FakeGh([r])
        with mock.patch.object(pr_repair.job_pr_draft, "list_running_worktree_branches",
                              return_value={"some-other-branch": "job-9"}):
            report = self.run_pass(gh)
        self.assertEqual(report["drafted"], [])
        ready_calls = [c for c in gh.calls if c[:3] == ["gh", "pr", "ready"]]
        self.assertEqual(ready_calls, [])


class RebaseTest(Harness):
    def behind(self, **kw):
        return row(mergeable="MERGEABLE", state="BEHIND", **kw)

    def test_behind_pr_takes_the_update_branch_path(self):
        gh = FakeGh([self.behind()])
        report = self.run_pass(gh)
        ups = gh.updates()
        self.assertEqual(len(ups), 1)
        joined = " ".join(ups[0])
        self.assertIn("updateMethod:REBASE", joined)
        self.assertIn(f"oid={HEAD}", joined)   # pinned to the head the predicate checked
        self.assertEqual(self.launches, [])
        self.assertTrue(report["rebased"])

    def test_same_base_twice_is_one_call(self):
        gh = FakeGh([self.behind()])
        self.run_pass(gh)
        self.run_pass(gh)
        self.assertEqual(len(gh.updates()), 1)

    def test_live_approval_on_a_behind_head_waits_spec_5A3_clause_2(self):
        self.approval = ""
        gh = FakeGh([self.behind()])
        report = self.run_pass(gh)
        self.assertEqual(gh.updates(), [])
        self.assertTrue(report["waiting"])

    def test_pending_picker_on_a_behind_head_waits_spec_5A3_clause_3(self):
        self.store = {"questions": {"q1": {"meta": {"kind": "merge-approval", "pr": 860,
                                                    "repo": pr_repair.REPO, "head_sha": HEAD}}}}
        gh = FakeGh([self.behind()])
        self.run_pass(gh)
        self.assertEqual(gh.updates(), [])

    def test_unreadable_store_or_raising_verifier_does_not_move(self):
        def bad_store():
            raise OSError("locked")
        gh = FakeGh([self.behind()])
        pr_repair.sweep(self.state, now=NOON, runner=gh, sender=self.sender,
                        verifier=lambda *a: "no approval", store_loader=bad_store)

        def bad_verifier(*a):
            raise RuntimeError("boom")
        pr_repair.sweep(self.state, now=NOON, runner=gh, sender=self.sender,
                        verifier=bad_verifier, store_loader=lambda: self.store)
        self.assertEqual(gh.updates(), [])

    def test_pending_ci_waits(self):
        pending = [{"__typename": "CheckRun", "name": "ci", "status": "IN_PROGRESS",
                    "conclusion": None}]
        gh = FakeGh([self.behind(statusCheckRollup=pending)])
        self.run_pass(gh)
        self.assertEqual(gh.updates(), [])

    def test_race_path_names_pr_and_both_shas(self):
        seq = {"n": 0}

        def verifier(*a):
            seq["n"] += 1
            return "" if seq["n"] >= 3 else "no approval on file"  # detect, predicate, then race
        gh = FakeGh([self.behind()])
        pr_repair.sweep(self.state, now=NOON, runner=gh, sender=self.sender,
                        verifier=verifier, store_loader=lambda: self.store)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("#860", self.sent[0])
        self.assertIn(HEAD[:12], self.sent[0])
        self.assertIn("d" * 12, self.sent[0])
        self.assertTrue([r for r in self.log() if r["event"] == "tap_destroyed"])

    def test_github_refusal_is_recorded_and_not_retried_on_the_same_base(self):
        gh = FakeGh([self.behind()], graphql={"errors": [{"message": "merge conflict"}]})
        self.run_pass(gh)
        self.run_pass(gh)
        self.assertEqual(len(gh.updates()), 1)

    def test_ceiling_binds_and_says_so_once(self):
        for i in range(pr_rebase.MAX_REBASES_PER_PR_PER_DAY):
            pr_repair.append_log(self.state, {"event": "rebase", "repo": pr_repair.REPO, "pr": 860,
                                              "base_sha": f"{i:040d}", "at": "2026-01-15T16:00:00Z"})
        gh = FakeGh([self.behind()])
        self.run_pass(gh)
        self.run_pass(gh)
        self.assertEqual(gh.updates(), [])
        self.assertEqual(len(self.sent), 1)


class PrRebaseInflightDefenseInDepthTest(unittest.TestCase):
    """`pr_repair.py` already drafts and skips an in-flight PR before it ever reaches
    `pr_rebase.consider` (`InFlightPrIsDraftedNotRepairedTest` above), so `inflight` is normally never
    populated at this call site. This pins the defense-in-depth for a direct caller that hands
    `blocked_reason`/`consider` a row nobody has pre-filtered."""

    def test_blocked_reason_skips_an_inflight_behind_pr(self):
        r = row(860, mergeable="MERGEABLE", state="BEHIND")
        kind, reason = pr_rebase.blocked_reason(
            r, repo=pr_repair.REPO, base_sha=BASE1, state_dir="unused", log_rows=[],
            store_loader=lambda: {"questions": {}}, inflight={r["headRefName"]: "job-1"})
        self.assertEqual(kind, "skip")
        self.assertIn("running job's worktree", reason)

    def test_blocked_reason_with_no_inflight_map_is_unchanged(self):
        r = row(860, mergeable="MERGEABLE", state="BEHIND")
        kind, _reason = pr_rebase.blocked_reason(
            r, repo=pr_repair.REPO, base_sha=BASE1, state_dir="unused", log_rows=[],
            store_loader=lambda: {"questions": {}},
            verifier=lambda sd, pr, head, repo: "no approval on file")
        self.assertEqual(kind, "")

    def test_blocked_reason_ignores_inflight_on_a_different_branch(self):
        r = row(860, mergeable="MERGEABLE", state="BEHIND")
        kind, _reason = pr_rebase.blocked_reason(
            r, repo=pr_repair.REPO, base_sha=BASE1, state_dir="unused", log_rows=[],
            store_loader=lambda: {"questions": {}}, inflight={"some-other-branch": "job-1"},
            verifier=lambda sd, pr, head, repo: "no approval on file")
        self.assertEqual(kind, "")


class LaunchTest(unittest.TestCase):
    def test_launch_argv_respects_the_caps_and_names_the_assistant(self):
        seen = []

        def runner(argv):
            seen.append(argv)
            return 0, json.dumps({"id": "j1", "status": "running"}), ""
        res = pr_repair.launch_repair("/s", row(), BASE1, ["seneschal/context-budget.json"],
                                      claude_bin="claude", runner=runner)
        self.assertEqual(res["job_id"], "j1")
        argv = seen[0]
        for flag in ("--worktree", "--agent"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--requested-by") + 1], "assistant")
        self.assertEqual(argv[argv.index("--reason-class") + 1], "merge-repair")
        self.assertNotIn("--over-soft-cap", argv)

    def test_a_cap_refusal_is_reported_as_capped(self):
        res = pr_repair.launch_repair(
            "/s", row(), BASE1, [], claude_bin="claude",
            runner=lambda argv: (2, "", "refusing to start: 5 of 5 in flight"))
        self.assertTrue(res["capped"])
        self.assertFalse(res["ok"])

    def test_brief_forbids_force_and_leaves_the_merge_to_the_owner(self):
        brief = pr_repair.repair_brief(pr_repair.REPO, row(), BASE1, ["seneschal/context-budget.json"])
        self.assertIn("Never force", brief)
        self.assertIn("leave the merge to the owner", brief)
        self.assertIn("origin/develop", brief)
        self.assertIn("ci_local.py", brief)
        self.assertIn("check_context_budget.py", brief)
        self.assertNotIn("DO NOT MERGE", brief)  # job briefs leak it into PR titles

    def test_brief_spells_the_configured_base_in_full(self):
        brief = pr_repair.repair_brief("example/repo", row(), BASE1, ["README.md"], base="trunk")
        self.assertIn("git merge --no-ff --no-commit origin/trunk", brief)
        self.assertIn("chore(merge): merge trunk into", brief)
        self.assertNotIn("origin/develop", brief)

    def test_brief_keeps_both_sides_of_markdown_and_comments_on_the_pr(self):
        brief = pr_repair.repair_brief(pr_repair.REPO, row(), BASE1, ["seneschal/docs/rulings.md"])
        self.assertIn("keep both sides; for tables/lists keep every row in chronological order; "
                      "never delete either side's content; if both sides edited the SAME sentence "
                      "differently, keep both versions adjacent and flag it in the PR comment", brief)
        self.assertIn(f"gh pr comment 860 --repo {pr_repair.REPO}", brief)
        self.assertIn("Never resolve code or config", brief)

    def test_repair_eligible(self):
        for p in ("seneschal/context-budget.json", "seneschal/context-pointers.json", "README.md",
                  "seneschal/docs/rulings.md", "CLAUDE.md"):
            self.assertTrue(pr_repair.repair_eligible(p), p)
        for p in ("seneschal/scripts/pr_repair.py", "pyproject.toml", "uv.lock", "x.json", "a.mdx"):
            self.assertFalse(pr_repair.repair_eligible(p), p)


class ConflictPathsTest(unittest.TestCase):
    def fake(self, pr_files, base_files, contents, merge_clean):
        def runner(argv):
            if argv[:2] == ["gh", "api"] and "/compare/" in argv[2]:
                a, b = argv[2].split("/compare/")[1].split("...")
                files = pr_files if a == BASE1 else base_files
                return 0, json.dumps({"merge_base_commit": {"sha": "m" * 40}, "files": files}), ""
            if argv[:2] == ["gh", "api"] and argv[2] == "-H":
                path = argv[4].split("/contents/")[1].split("?")[0]
                if path in contents:
                    return 0, contents[path], ""
                return 1, "", "HTTP 404: Not Found"
            if argv[:2] == ["git", "merge-file"]:
                return (0 if merge_clean else 1), "", ""
            raise AssertionError(argv)
        return runner

    def test_only_shared_paths_that_fail_a_three_way_merge(self):
        run = self.fake([{"filename": "a.json"}, {"filename": "only-pr.py"}],
                        [{"filename": "a.json"}, {"filename": "only-base.md"}],
                        {"a.json": "x"}, merge_clean=False)
        self.assertEqual(pr_repair.conflict_paths(pr_repair.REPO, BASE1, HEAD, runner=run),
                         ["a.json"])

    def test_shared_path_that_merges_cleanly_is_not_a_conflict(self):
        run = self.fake([{"filename": "a.md"}], [{"filename": "a.md"}], {"a.md": "x"},
                        merge_clean=True)
        self.assertEqual(pr_repair.conflict_paths(pr_repair.REPO, BASE1, HEAD, runner=run), [])

    def test_delete_on_one_side_counts_as_conflict(self):
        run = self.fake([{"filename": "a.md", "status": "removed"}], [{"filename": "a.md"}], {},
                        merge_clean=True)
        structural = []
        self.assertEqual(pr_repair.conflict_paths(pr_repair.REPO, BASE1, HEAD, runner=run,
                                                  structural=structural), ["a.md"])
        self.assertEqual(structural, ["a.md"])

    def test_content_conflict_is_not_structural(self):
        run = self.fake([{"filename": "a.md"}], [{"filename": "a.md"}], {"a.md": "x"},
                        merge_clean=False)
        structural = []
        self.assertEqual(pr_repair.conflict_paths(pr_repair.REPO, BASE1, HEAD, runner=run,
                                                  structural=structural), ["a.md"])
        self.assertEqual(structural, [])

    def test_truncated_compare_cannot_tell(self):
        many = [{"filename": f"f{i}"} for i in range(pr_repair.COMPARE_FILE_CAP)]
        run = self.fake(many, [], {}, merge_clean=True)
        with self.assertRaises(pr_repair.CannotTell):
            pr_repair.conflict_paths(pr_repair.REPO, BASE1, HEAD, runner=run)

    @unittest.skipUnless(shutil.which("git"), "git not on PATH")
    def test_real_git_merge_file_detects_a_conflict(self):
        self.assertFalse(pr_repair.merges_cleanly("a\nX\nc\n", "a\nb\nc\n", "a\nY\nc\n",
                                                  pr_repair._run))
        self.assertTrue(pr_repair.merges_cleanly("X\nb\nc\nd\ne\n", "a\nb\nc\nd\ne\n",
                                                 "a\nb\nc\nd\nY\n", pr_repair._run))


@unittest.skipUnless(_presence_wires_pr_repair(),
                     "presence wiring (pr_watch_task calls pr_repair.sweep after pr_sweep.sweep) "
                     "lands in wave 26")
class DaemonWiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_repair_runs_after_the_sweep_and_its_crash_is_contained(self):
        import argparse
        import asyncio
        from unittest import mock
        import presence
        state = presence.DaemonState()
        order, logs = [], []
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        args = argparse.Namespace(state_dir=tmp, stub_brain=False, stub_send=False,
                                  fake_inbox=None, no_pr_watch=False, telegram_env="unused.env",
                                  claude_bin="claude-test")

        def sweep(**kw):
            order.append("sweep")
            return {"errors": []}

        def repair(state_dir, **kw):
            order.append(("repair", state_dir, kw.get("claude_bin")))
            state.stop.set()
            raise RuntimeError("boom")

        with mock.patch.object(presence.pr_sweep, "sweep", sweep), \
                mock.patch.object(presence.pr_repair, "sweep", repair), \
                mock.patch.object(presence, "_nudge_topics_toggle", lambda *a: None):
            await asyncio.wait_for(presence.pr_watch_task(state, args, logs.append), timeout=10)
        self.assertEqual(order, ["sweep", ("repair", tmp, "claude-test")])
        self.assertTrue(any("pr-repair pass error" in line for line in logs))


class TargetIsConfigurationTest(Harness):
    """Which repository and which base branch are `repo_config`'s answers, read once per pass — never
    a hard-coded slug, never frozen at import."""

    def test_REPO_and_BASE_BRANCH_resolve_lazily_from_repo_config(self):
        self.assertEqual(pr_repair.REPO, "example/repo")
        self.assertEqual(pr_repair.BASE_BRANCH, "develop")
        with mock.patch.object(pr_repair.repo_config, "origin_repo", return_value="example/other"), \
                mock.patch.object(pr_repair.repo_config, "base_branch", return_value="main"):
            self.assertEqual(pr_repair.REPO, "example/other")
            self.assertEqual(pr_repair.BASE_BRANCH, "main")

    def test_a_checkout_with_no_origin_does_nothing_and_says_why(self):
        gh = FakeGh([row()])
        with mock.patch.object(pr_repair.repo_config, "origin_repo", return_value=""):
            report = self.run_pass(gh)
        self.assertEqual(gh.calls, [])
        self.assertTrue(any("origin" in e for e in report["errors"]))

    def test_the_configured_base_selects_rows_and_names_the_tip(self):
        seen = []

        def gh(argv):
            seen.append(argv)
            if argv[:3] == ["gh", "pr", "list"]:
                return 0, json.dumps([row(baseRefName="main")]), ""
            if argv[:2] == ["gh", "api"] and argv[2].endswith("/git/ref/heads/main"):
                return 0, BASE1 + "\n", ""
            if argv[:3] == ["gh", "pr", "view"]:
                return 0, json.dumps({"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY",
                                      "headRefOid": HEAD, "state": "OPEN"}), ""
            raise AssertionError(argv)
        report = self.run_pass(gh, base_branch="main")
        self.assertEqual(report["looked_at"], 1)
        self.assertEqual(len(self.launches), 1)
        self.assertTrue(any(a[2].endswith("/git/ref/heads/main") for a in seen if a[:2] == ["gh", "api"]))

    def test_an_explicit_repo_wins_over_the_config(self):
        seen = []

        def gh(argv):
            seen.append(argv)
            return 1, "", "HTTP 502"
        pr_repair.sweep(self.state, now=NOON, runner=gh, repo="example/third")
        self.assertIn("example/third", seen[0])


class AbsenceTest(unittest.TestCase):
    """§5.4 invariant 1 / §5A.3: asserted against the files, the way pr_sweep's absence property is."""

    def src(self, name):
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            return fh.read()

    def test_neither_module_names_the_approval_writer_or_a_merge_command(self):
        for name in ("pr_repair.py", "pr_rebase.py"):
            text = self.src(name)
            self.assertNotIn("record_approval", text, name)
            self.assertIsNone(re.search(r"pr[\"', ]+merge\b", text), name)
            self.assertNotIn("--admin", text, name)
            self.assertNotIn("--auto", text, name)

    def test_the_rebaser_holds_no_branch_and_no_force(self):
        text = self.src("pr_rebase.py")
        self.assertNotIn("--force", text)
        self.assertNotIn('"push"', text)
        self.assertNotIn('"git"', text)

    def test_the_repair_pass_never_uses_the_soft_cap_override(self):
        self.assertNotIn("--over-soft-cap\"", self.src("pr_repair.py"))


if __name__ == "__main__":
    unittest.main()
