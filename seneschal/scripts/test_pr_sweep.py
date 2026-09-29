#!/usr/bin/env python3
"""Tests for ``pr_sweep`` — the resident PR watch, and ``presence.pr_watch_task`` that runs it.

Six classes here defend properties rather than behaviour, and must not be relaxed into passing:

* **``AskingIsAllItCanDoTest``** is this feature's version of `merge_guard`'s
  ``AskingCannotLowerTheGateTest``. The sweep may only ever *deliver a question*. If any path through
  it merges, approves, records an approval, or makes the guard's refusal one step shorter, it is
  wrong — and a resident finder is exactly the place that temptation would land.
* **``NoSecondClassifierTest``** pins the reuse. The sweep decides ONE thing (is CI terminally green)
  and that decision is ``watch_pr.classify``, imported. A second opinion here about what "changes
  functionality" means, or a second dedupe beside the guard's ask log, is the bug this repo keeps
  re-learning, so it is asserted against the FILE — the property is an absence, which an import graph
  cannot see.
* **``BurstBoundTest``** is the one that would bite. A resident sweep sees every open PR at once on
  its first pass, after a restart, and after an outage. Nothing here may batch, and — equally — the
  bound may never DROP a picker, because a silently un-asked PR is the failure the whole feature
  exists to remove. The cap holds work over and names what it held.
* **``OneAskPerHeadShaAcrossBothAskersTest``** is the assertion the two askers require. The
  daemon and a hand-started ``watch_pr.py`` may both see the same green PR, and two pickers for one
  commit, seconds apart, is exactly what an unkeyed pair of askers produces.
* **``QuietHoursTest``** — a resident watcher makes 3 AM far more likely than a hand-started one did,
  so both layers are pinned: the pass never ASKS inside the window, AND the guard's own deferral is
  proved to still apply on this path with the pass's own check stepped past.
* **``ItNeverAsksAboutAPrThatCannotMergeTest``** pins a POLARITY, and the polarity is the whole risk.
  ``CONFLICTING`` suppresses the question because an approval bound to a conflicted commit cannot be
  spent. Everything else — ``UNKNOWN``, a missing
  field, a value GitHub has not invented yet — **asks**, because this predicate can only ever
  withhold a question and a withheld one is invisible and permanent. A test that starts suppressing
  on the unknowns has re-introduced the drop this whole module exists to remove.

**No test here reaches the network, spawns a daemon, or sends a Telegram message.** Every `gh` call
goes through the `lister=`/`mg._run` seams and every send through `mg._send_question`; the daemon
task is driven directly with a stubbed `sweep`. Nothing reads or writes the live `seneschal/state/`.

**The owner's zone is pinned** (`tz_common._zone` → a fixed UTC-5 zone) for every test here, so the
quiet-window verdicts are the same on every runner whatever its own timezone or identity config.
**The watched set is injected** (`repos=`) by every sweep, so no test reads the live
`pr-guard.json` or this checkout's `origin`.

The daemon-task tests (``DaemonTaskTest`` and the two presence scans in ``AskingIsAllItCanDoTest``)
drive `presence.pr_watch_task`; they skip until that task lands in `presence.py`.

Run:  python -m unittest seneschal.scripts.test_pr_sweep   (or)   python test_pr_sweep.py
"""
import argparse
import asyncio
import contextlib
import json
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

import merge_guard as mg  # noqa: E402
import picker_retire  # noqa: E402 — the third clock on the pass's path; `wall_clock` freezes it too
import pr_red_notify  # noqa: E402 — the red-notify ledger this wiring reads back
import pr_sweep  # noqa: E402
import repo_config  # noqa: E402 — the watched set's source, pinned by `DefaultReposIsConfigTest`
import telegram_ask as ta  # noqa: E402 — the question store the moved-head re-ask test reads
import tz_common  # noqa: E402 — the owner-zone seam every quiet-window reading goes through
import watch_pr  # noqa: E402

try:
    import presence  # noqa: E402 — the daemon task that runs the sweep (wave 26 wiring)
except Exception:  # noqa: BLE001 — a presence that cannot import is simply not wired yet
    presence = None

#: The PR-watch daemon task lands in `presence.py` in a later wave; until it does, the tests that
#: drive or scan it skip rather than fail.
HAS_PR_WATCH_TASK = presence is not None and hasattr(presence, "pr_watch_task")
#: `merge_guard.record_approval`'s single caller is the daemon's Telegram callback path.
HAS_APPROVAL_CALLER = presence is not None and hasattr(presence, "_merge_approval_clause")
PR_WATCH_SKIP = "presence.pr_watch_task (the PR-watch daemon task) lands in wave 26"

REPO = "example/repo"
OTHER_REPO = "example/other"

#: The owner's zone for every test: a FIXED UTC-5 offset, so each instant below reads the same wall
#: clock on every runner. Fixed rather than a real DST zone because nothing here is about DST — the
#: curfew's DST correctness is `test_night_curfew.py`'s — and a fixed offset needs no tz database.
OWNER_ZONE = timezone(timedelta(hours=-5), "UTC-5")
HEAD = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
OTHER_HEAD = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"
CODE_FILES = [{"path": "seneschal/scripts/meal_log.py"}]
DOCS_FILES = [{"path": "README.md"}]

#: Explicit UTC instants, never the runner's clock or zone. In :data:`OWNER_ZONE` (UTC-5) 17:00Z is
#: noon and 08:00Z is 03:00 — inside `sentinel`'s default 01:00-07:00 curfew, the window the
#: auto-send reuses rather than copies.
NOON = datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)
QUIET_NIGHT = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)

#: **A ring of wall-clock readings for `ClockIsPinnedForTheWholePassTest`** — `(what the owner's
#: clock reads, the UTC instant, is it inside the 01:00-07:00 curfew)`. Both boundaries are pinned
#: from both sides, because a window is two comparisons and an off-by-one on either is a picker that
#: arrives an hour early or not at all. The last two are in January, so the ring spans the calendar
#: rather than one afternoon.
CURFEW_RING = (
    ("00:59 local", datetime(2026, 8, 20, 5, 59, tzinfo=timezone.utc), False),   # a minute before
    ("01:00 local", datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc), True),     # the start boundary
    ("01:01 local", datetime(2026, 8, 20, 6, 1, tzinfo=timezone.utc), True),
    ("03:00 local", datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc), True),     # deep inside
    ("06:59 local", datetime(2026, 8, 20, 11, 59, tzinfo=timezone.utc), True),   # the last minute in
    ("07:00 local", datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc), False),   # the end boundary
    ("07:01 local", datetime(2026, 8, 20, 12, 1, tzinfo=timezone.utc), False),
    ("12:00 local", datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc), False),
    ("18:00 local", datetime(2026, 8, 20, 23, 0, tzinfo=timezone.utc), False),
    ("23:59 local", datetime(2026, 8, 21, 4, 59, tzinfo=timezone.utc), False),
    ("03:00 local (January)", datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc), True),
    ("12:00 local (January)", datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc), False),
)

#: Every hour of one owner-local day, 00:00 through 23:00, as UTC instants. The ring above samples
#: the interesting readings; this one samples ALL of them, so "passes only between 07:00 and 01:00"
#: cannot survive as a property of any test that loops over it.
HOURLY_RING = tuple(datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc) + timedelta(hours=h)
                    for h in range(24))


@contextlib.contextmanager
def wall_clock(instant):
    """Freeze what **the runner's own clock** reads, in every module on the sweep's path.

    This is the seam the defect needed and did not have. A sweep that pins an instant and then calls
    something which reads `datetime.now()` anyway is only half-deterministic, and the half that is
    real is invisible until CI happens to run at the wrong hour — which is exactly how
    a daylight test can come to fail for six hours a day. The only way to prove the
    pinning is to make the real clock say something the pinned instant does not.

    A `datetime` **subclass**, not a mock: every other use of the name — `fromisoformat`, subtraction
    against a `timedelta`, comparison — keeps working untouched, so freezing the clock changes one
    answer and nothing else."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    with mock.patch.object(pr_sweep, "datetime", _Frozen), \
            mock.patch.object(mg, "datetime", _Frozen), \
            mock.patch.object(picker_retire, "datetime", _Frozen):
        yield

GREEN = [{"__typename": "CheckRun", "name": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"}]
RED = [{"__typename": "CheckRun", "name": "CI", "status": "COMPLETED", "conclusion": "FAILURE"}]
PENDING = [{"__typename": "CheckRun", "name": "CI", "status": "IN_PROGRESS", "conclusion": None}]


def row(pr, rollup=None, head=HEAD, repo=REPO, draft=False, title="a pull request",
        mergeable=None, merge_state_status=None):
    """One `gh pr list --json …` row, in the shape `gh` actually returns (`pr list` and `pr view`
    hand back byte-identical rollup nodes).

    **`mergeable` is OMITTED unless a test asks for it, deliberately.** Most tests therefore drive a
    row with no mergeability at all, which is the fail-open case: the suppression must be reachable
    only by positive evidence, so every test that does not mention it going on passing IS the
    assertion that an absent verdict still asks.

    **`merge_state_status` is the same convention, one field over.** Omitted unless a test asks for
    it, so most tests drive a row with no `mergeStateStatus` at all."""
    r = {"number": pr, "statusCheckRollup": GREEN if rollup is None else rollup,
         "headRefOid": head, "url": f"https://github.com/{repo}/pull/{pr}",
         "isDraft": draft, "title": title}
    if mergeable is not None:
        r["mergeable"] = mergeable
    if merge_state_status is not None:
        r["mergeStateStatus"] = merge_state_status
    return r


def lister(mapping, code=0, stderr="", raises=None, stdout=None):
    """A `pr_sweep._run`-shaped stub: `{repo_slug: [row, ...]}`. No test may reach a real `gh`."""
    def run(argv):
        if raises is not None:
            raise raises
        if stdout is not None:
            return code, stdout, stderr
        repo = argv[argv.index("--repo") + 1]
        return code, json.dumps(mapping.get(repo, [])), stderr
    return run


def gh_view(files=CODE_FILES, head=HEAD, repo=REPO, state="OPEN"):
    """A `merge_guard._run`-shaped stub, so the REAL guard can be driven end to end offline."""
    def run(argv, cwd=None):
        pr = int(argv[argv.index("view") + 1])
        slug = argv[argv.index("--repo") + 1] if "--repo" in argv else repo
        return 0, json.dumps({"number": pr, "files": files, "headRefOid": head, "state": state,
                              "title": "a pull request",
                              "url": f"https://github.com/{slug}/pull/{pr}"}), ""
    return run


def sender(log, ok=True, code=0):
    """A `merge_guard._send_question`-shaped stub. **The only thing standing between this suite and
    the owner's phone**, so every test that drives the real guard passes it."""
    def send(argv):
        log.append(argv)
        return code, json.dumps({"ok": ok, "question_id": f"q{len(log)}"}), ""
    return send


def source(name):
    with open(os.path.join(SCRIPT_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


class SweepCase(unittest.TestCase):
    """A temp state dir per test, the owner's zone pinned, and the quiet window off unless a test is
    about it — otherwise the suite's verdict would depend on what time of day CI happened to run."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patch = mock.patch.object(mg, "ASK_ON_GREEN_QUIET_HOURS", False)
        patch.start()
        self.addCleanup(patch.stop)
        zone = mock.patch.object(tz_common, "_zone", return_value=OWNER_ZONE)
        zone.start()
        self.addCleanup(zone.stop)
        self.sends = []

    def sweep_for_real(self, rows, files=CODE_FILES, head=HEAD, repos=(REPO,), now=NOON, **kw):
        """Drive `sweep` through the REAL `merge_guard.ask_on_green`, with `gh` and the send stubbed.
        This is what makes the ledger assertions mean something: the ask log entries are written by
        the guard's own `record_ask`, not by a fixture.

        `now` defaults to noon as every caller but one wants; passing `None` drives the DAEMON's own
        call shape, where the pass has no injected instant and pins its own."""
        with mock.patch.object(mg, "_run", gh_view(files=files, head=head)), \
                mock.patch.object(mg, "_send_question", sender(self.sends)):
            return pr_sweep.sweep(repos=repos, state_dir=self.dir,
                                  lister=lister({r: rows for r in repos}), now=now, **kw)


# --------------------------------------------------------------------- the one decision it makes

class GreenIsWatchPrsVerdictTest(SweepCase):
    def test_a_terminally_green_pr_is_a_candidate(self):
        self.assertEqual([r["number"] for r in pr_sweep.candidates([row(1)])], [1])

    def test_red_is_not_green(self):
        self.assertEqual(pr_sweep.candidates([row(1, RED)]), [])

    def test_pending_is_not_green(self):
        """Unknown fails closed — `watch_pr`'s rule, and the reason it is imported rather than
        restated."""
        self.assertEqual(pr_sweep.candidates([row(1, PENDING)]), [])

    def test_a_mixed_rollup_with_one_pending_check_is_not_green(self):
        self.assertEqual(pr_sweep.candidates([row(1, GREEN + PENDING)]), [])

    def test_an_empty_rollup_is_not_green(self):
        """A PR with no checks attached is indistinguishable from a repo with no CI. Absence of
        evidence is not a pass — the cheerful lie `watch_pr`'s first dogfood run told."""
        self.assertEqual(pr_sweep.candidates([row(1, [])]), [])

    def test_a_null_rollup_is_not_green(self):
        """`gh` really returns this for a PR with no rollup at all, and it must fold into the empty
        case rather than into a TypeError."""
        self.assertEqual(pr_sweep.candidates([row(1, None) | {"statusCheckRollup": None}]), [])

    def test_skipped_and_neutral_conclusions_still_pass(self):
        rollup = [{"__typename": "CheckRun", "name": "android", "status": "COMPLETED",
                   "conclusion": "SKIPPED"}] + GREEN
        self.assertEqual([r["number"] for r in pr_sweep.candidates([row(1, rollup)])], [1])

    def test_a_legacy_status_context_node_is_understood(self):
        self.assertEqual([r["number"] for r in pr_sweep.candidates(
            [row(1, [{"__typename": "StatusContext", "context": "legacy", "state": "SUCCESS"}])])], [1])

    def test_a_draft_is_never_a_candidate(self):
        self.assertEqual(pr_sweep.candidates([row(1, draft=True)]), [])

    def test_a_draft_carrying_a_leaked_dnm_marker_is_still_untouched(self):
        """A GitHub draft is the real hold signal — unlike a leaked "DO NOT MERGE" title
        (`merge_guard.strip_leaked_dnm_marker`'s whole reason to exist), a draft is never a
        candidate regardless of what its title says, and nothing here needs to know the marker
        exists to keep that true."""
        self.assertEqual(
            pr_sweep.candidates([row(1, draft=True, title="DO NOT MERGE: still a draft")]), [])

    def test_un_drafting_still_gets_asked_about_even_though_no_commit_moved(self):
        """The skip costs nothing because it records nothing: the pass after it is marked ready sees
        an ordinary green PR, even though un-drafting moves no head SHA."""
        self.assertEqual(pr_sweep.candidates([row(7, draft=True)]), [])
        self.assertEqual([r["number"] for r in pr_sweep.candidates([row(7)])], [7])

    def test_candidates_are_ordered_so_a_capped_pass_is_deterministic(self):
        rows = [row(9), row(3), row(7)]
        self.assertEqual([r["number"] for r in pr_sweep.candidates(rows)], [3, 7, 9])

    def test_a_row_with_no_number_is_ignored_rather_than_crashing(self):
        self.assertEqual(pr_sweep.candidates([{"statusCheckRollup": GREEN}, "junk", None]), [])


# ------------------------------------------------------ in-flight is never a candidate either

class InFlightIsNeverACandidateTest(SweepCase):
    """A PR whose branch is checked out in a RUNNING job's worktree is not a merge candidate — the
    same silent skip a draft gets (`job_pr_draft.py`): a job that opens its PR while still pushing
    commits must not have that PR treated as a repair/merge candidate before it is even finished."""

    def test_candidates_drops_a_row_whose_branch_is_inflight(self):
        r = dict(row(1), headRefName="feat/voice-mvp")
        self.assertEqual(pr_sweep.candidates([r], {"feat/voice-mvp": "job-1"}), [])

    def test_candidates_keeps_a_row_on_a_DIFFERENT_inflight_branch(self):
        r = dict(row(1), headRefName="feat/voice-mvp")
        self.assertEqual([x["number"] for x in pr_sweep.candidates([r], {"other-branch": "job-1"})],
                         [1])

    def test_candidates_defaults_to_nothing_inflight(self):
        """Every existing direct call to `candidates(rows)` — no second argument — must keep working
        unchanged."""
        r = dict(row(1), headRefName="feat/voice-mvp")
        self.assertEqual([x["number"] for x in pr_sweep.candidates([r])], [1])

    def test_sweep_never_asks_about_an_inflight_pr(self):
        r = dict(row(1), headRefName="feat/voice-mvp")
        with mock.patch.object(pr_sweep.job_pr_draft, "list_running_worktree_branches",
                              return_value={"feat/voice-mvp": "job-1"}):
            report = self.sweep_for_real([r])
        self.assertEqual(report["asked"], [])
        self.assertEqual(report["looked_at"], 0)

    def test_sweep_never_red_notifies_an_inflight_pr(self):
        r = dict(row(1, RED), headRefName="feat/voice-mvp")
        notifier = lambda text, **kw: self.fail("notified about an in-flight PR")
        with mock.patch.object(pr_sweep.job_pr_draft, "list_running_worktree_branches",
                              return_value={"feat/voice-mvp": "job-1"}):
            report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, lister=lister({REPO: [r]}),
                                    asker=lambda *a, **k: {"ok": True, "sent": False, "reason": "n/a"},
                                    notifier=notifier, now=NOON)
        self.assertEqual(report["notified"], [])
        self.assertEqual(report["red_looked_at"], 0)


# --------------------------------------------------------------------- it asks, and only asks

class HandsGreenPrsToTheGuardTest(SweepCase):
    def test_a_green_functional_pr_gets_a_picker(self):
        report = self.sweep_for_real([row(414)])
        self.assertEqual([a["pr"] for a in report["asked"]], [414])
        self.assertEqual(len(self.sends), 1)

    def test_a_docs_only_pr_gets_a_notice_too(self):
        """The guard decides this, not the sweep — and the guard's own decision for
        a docs-only PR is to send a notice picker, not to stay silent. The sweep just reflects
        whatever `ask_on_green` decided, unchanged."""
        report = self.sweep_for_real([row(414)], files=DOCS_FILES)
        self.assertEqual([a["pr"] for a in report["asked"]], [414])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(report["skipped"], [])

    def test_green_prs_with_no_watcher_started_are_asked_about(self):
        """The whole reason this exists: without a resident sweep, a PR gets a picker only if someone
        started a watcher for it, and two PRs going green with nothing watching get nothing."""
        report = self.sweep_for_real([row(420), row(421)], max_asks=2)
        self.assertEqual(sorted(a["pr"] for a in report["asked"]), [420, 421])

    def test_both_watched_repos_are_swept(self):
        rows = {REPO: [row(414)], OTHER_REPO: [row(45, repo=OTHER_REPO)]}
        with mock.patch.object(mg, "_run", gh_view()), \
                mock.patch.object(mg, "_send_question", sender(self.sends)):
            report = pr_sweep.sweep(repos=(REPO, OTHER_REPO), state_dir=self.dir,
                                    lister=lister(rows), now=NOON, max_asks=9)
        self.assertEqual({(a["repo"], a["pr"]) for a in report["asked"]},
                         {(REPO, 414), (OTHER_REPO, 45)})

    def test_every_ask_names_its_repository(self):
        """The guard does not infer a repository, so an ask that passes `repo=None` is an ask about
        whichever repository the daemon happens to be standing in. This sweep never relied on that default — it iterates a named watched set — and this
        pins it, because "we already pass it" is the kind of thing that is true until an edit makes
        it not, silently and in the one direction that costs a tap."""
        seen = []

        def asker(pr, repo=None, **_kw):
            seen.append(repo)
            return {"ok": True, "sent": True, "reason": "asked"}

        rows = {REPO: [row(414)], OTHER_REPO: [row(45, repo=OTHER_REPO)]}
        pr_sweep.sweep(repos=(REPO, OTHER_REPO), state_dir=self.dir, lister=lister(rows),
                       asker=asker, now=NOON, max_asks=9)
        self.assertEqual(sorted(seen), sorted([OTHER_REPO, REPO]))
        self.assertNotIn(None, seen)

    def test_a_dry_run_renders_and_records_nothing(self):
        report = self.sweep_for_real([row(414)], dry_run=True)
        self.assertTrue(report["asked"][0]["dry_run"])
        self.assertEqual(mg.read_asks(self.dir), [])


# ------------------------------------------------- the watched set is configuration, never code

class DefaultReposIsConfigTest(SweepCase):
    """**Which repositories are asked about is the owner's configuration** — `repo_config`'s
    `watched_repos` (the `pr-guard.json` list, else this checkout's `origin`) — resolved on every
    call, never frozen into a constant at import."""

    def test_default_repos_is_repo_configs_watched_set(self):
        with mock.patch.object(repo_config, "watched_repos", return_value=(REPO, OTHER_REPO)):
            self.assertEqual(pr_sweep.default_repos(), (REPO, OTHER_REPO))

    def test_the_DEFAULT_REPOS_name_still_answers_and_follows_the_config(self):
        """Callers that read `pr_sweep.DEFAULT_REPOS` as a name keep working, and a config change
        is seen on the next read rather than at the next restart."""
        with mock.patch.object(repo_config, "watched_repos", return_value=(REPO,)):
            self.assertEqual(pr_sweep.DEFAULT_REPOS, (REPO,))
        with mock.patch.object(repo_config, "watched_repos", return_value=(OTHER_REPO,)):
            self.assertEqual(pr_sweep.DEFAULT_REPOS, (OTHER_REPO,))

    def test_an_unknown_attribute_is_still_an_AttributeError(self):
        with self.assertRaises(AttributeError):
            pr_sweep.NOT_A_THING  # noqa: B018

    def test_a_sweep_with_no_repos_asks_the_config_once(self):
        calls = []

        def watched(*a, **k):
            calls.append(1)
            return (REPO,)

        with mock.patch.object(repo_config, "watched_repos", side_effect=watched):
            report = pr_sweep.sweep(state_dir=self.dir, lister=lister({REPO: [row(414)]}),
                                    asker=lambda pr, **kw: {"ok": True, "sent": True,
                                                            "reason": "asked"}, now=NOON)
        self.assertEqual([(a["repo"], a["pr"]) for a in report["asked"]], [(REPO, 414)])
        self.assertEqual(len(calls), 1)

    def test_an_explicit_repos_never_consults_the_config(self):
        with mock.patch.object(repo_config, "watched_repos",
                               side_effect=AssertionError("config read despite explicit repos")):
            report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, lister=lister({REPO: []}),
                                    asker=lambda *a, **k: {}, now=NOON)
        self.assertEqual(report["asked"], [])

    def test_no_repository_anywhere_is_an_empty_pass_not_a_crash(self):
        with mock.patch.object(repo_config, "watched_repos", return_value=()):
            report = pr_sweep.sweep(state_dir=self.dir, lister=lister({}),
                                    asker=lambda *a, **k: self.fail("asked about nothing"), now=NOON)
        self.assertEqual(report["looked_at"], 0)

    def test_no_owner_repository_is_named_in_the_module(self):
        """The watched set lives in config; a hard-coded `owner/name` literal here would be a
        watched repository nobody configured."""
        import re
        src = source("pr_sweep.py")
        self.assertNotIn("DEFAULT_REPOS = (", src)
        self.assertIsNone(re.search(r'"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"\s*[,)]', src))


# ------------------------------------------------- a question that cannot be spent is not worth asking

class ItNeverAsksAboutAPrThatCannotMergeTest(SweepCase):
    """**A picker for a pull request that cannot merge spends a tap that buys nothing.**

    An Approve tap on a `CONFLICTING` head is unspendable even when CI is fully green at the
    approved SHA: green was never the whole question, and whether the PR can merge at all is one
    more field on the same list call.

    **The direction of every test here is the point.** `CONFLICTING` suppresses; *everything else
    asks*. This predicate can only ever withhold a question, and a withheld question is silent and
    permanent, where a surplus one costs one visible tap and is recovered on the next pass. Any edit
    that makes an unknown suppress is re-introducing the silent never-asked failure — a green PR
    nobody is ever asked about — behind a field name."""

    def _asker(self, calls):
        def ask(pr, **kw):
            calls.append(pr)
            return {"ok": True, "sent": True, "reason": "sent", "question_id": f"q{len(calls)}"}
        return ask

    def _sweep(self, rows, **kw):
        """Drive the pass with a recording asker, so *what was asked about* is the assertion rather
        than a side effect of the guard's own refusals."""
        calls = []
        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, asker=self._asker(calls),
                                lister=lister({REPO: rows}), now=NOON, **kw)
        return calls, report

    # ---- the one value that suppresses

    def test_a_conflicting_pr_gets_no_picker(self):
        calls, report = self._sweep([row(574, mergeable="CONFLICTING")])
        self.assertEqual(calls, [])
        self.assertEqual(report["asked"], [])

    def test_the_suppression_says_why_rather_than_vanishing(self):
        """A skip that reports nothing reads as *there was nothing there* — the module's own
        complaint about a silent truncation, applied to itself."""
        _, report = self._sweep([row(574, mergeable="CONFLICTING")])
        self.assertEqual(report["looked_at"], 1)
        self.assertEqual(len(report["skipped"]), 1)
        skip = report["skipped"][0]
        self.assertEqual((skip["repo"], skip["pr"]), (REPO, 574))
        self.assertTrue(skip["conflicted"])
        self.assertIn("CONFLICTING", skip["reason"])

    def test_it_is_the_real_thing_gh_returns(self):
        """`CONFLICTING` is not a spelling this suite invented: it is what `gh pr list --json
        mergeable` answers for a conflicted pull request."""
        self.assertEqual(sorted(pr_sweep.CANNOT_MERGE_STATES), ["CONFLICTING"])

    def test_case_and_surrounding_space_do_not_smuggle_a_conflict_past_it(self):
        for value in ("conflicting", " CONFLICTING ", "Conflicting"):
            with self.subTest(value=value):
                calls, _ = self._sweep([row(574, mergeable=value)])
                self.assertEqual(calls, [])

    # ---- everything else asks, and this is the decision

    def test_a_mergeable_pr_is_asked_about_normally(self):
        calls, report = self._sweep([row(414, mergeable="MERGEABLE")])
        self.assertEqual(calls, [414])
        self.assertEqual([a["pr"] for a in report["asked"]], [414])

    def test_unknown_asks_because_it_is_the_absence_of_an_answer(self):
        """**THE DECISION, and the one most likely to be argued with.** `UNKNOWN` is GitHub
        computing mergeability lazily, not a verdict — a PR can read `UNKNOWN` on one list call and
        `CONFLICTING` an hour later. Suppressing on
        it would let a PR whose mergeability never computes go un-asked forever, silently; asking on
        it costs at most one visible tap."""
        calls, _ = self._sweep([row(577, mergeable="UNKNOWN")])
        self.assertEqual(calls, [577])

    def test_a_missing_mergeable_field_asks(self):
        calls, _ = self._sweep([row(414)])
        self.assertEqual(calls, [414])

    def test_a_null_or_non_string_mergeable_asks(self):
        for value in (None, 0, False, [], {}, ["CONFLICTING"]):
            with self.subTest(value=value):
                calls, _ = self._sweep([row(414) | {"mergeable": value}])
                self.assertEqual(calls, [414])

    def test_a_value_github_has_not_invented_yet_asks(self):
        """An allow-list of refusals, so a new GitHub enum member is a question that still goes out
        rather than a picker that silently stops arriving."""
        for value in ("BEHIND", "BLOCKED", "DRAFT", "MERGEABLE_WITH_WARNINGS", ""):
            with self.subTest(value=value):
                calls, _ = self._sweep([row(414, mergeable=value)])
                self.assertEqual(calls, [414])

    def test_the_refusal_is_an_allow_list_never_a_negation(self):
        """The negative spelling silently swallows every unknown, which is the failure the spec's
        §5A.3 clause 1 names in the rebaser and the same one `merge_guard.NOT_OPEN_STATES` avoids.
        Asserted against the FILE, the way this module's other absence properties are."""
        src = source("pr_sweep.py")
        self.assertIn("CANNOT_MERGE_STATES", src)
        self.assertNotIn('!= "MERGEABLE"', src)
        self.assertNotIn("!= 'MERGEABLE'", src)

    def test_the_predicate_answers_a_reason_not_a_bool(self):
        self.assertEqual(pr_sweep.unmergeable_reason({}), "")
        self.assertEqual(pr_sweep.unmergeable_reason({"mergeable": "MERGEABLE"}), "")
        self.assertIn("CONFLICTING", pr_sweep.unmergeable_reason({"mergeable": "CONFLICTING"}))

    # ---- it can never withhold a question indefinitely

    def test_a_suppressed_pr_records_nothing_so_it_asks_the_moment_it_can_merge(self):
        """The bound that makes the suppression safe: no ledger row is written, so this is a HOLD and
        never a drop. The instant GitHub says it can merge, the question goes out at that same
        commit — the head never moved, and nothing recorded a picker that never went."""
        conflicted = [row(573, mergeable="CONFLICTING")]
        self.sweep_for_real(conflicted)
        self.assertEqual(self.sends, [])
        self.assertEqual(mg.read_asks(self.dir), [])
        self.sweep_for_real([row(573, mergeable="MERGEABLE")])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual([a["pr"] for a in mg.read_asks(self.dir)], [573])

    def test_an_unknown_forever_still_gets_its_picker_on_the_very_first_pass(self):
        """Stated as its own test because it is the requirement the `UNKNOWN` decision exists to meet:
        **no pull request may go un-asked indefinitely because of this field.** A PR whose
        mergeability never computes is asked about immediately, not after N passes and not never."""
        calls, _ = self._sweep([row(577, mergeable="UNKNOWN")])
        self.assertEqual(calls, [577])
        self.sweep_for_real([row(577, mergeable="UNKNOWN")])
        self.assertEqual(len(self.sends), 1)

    # ---- it composes with the bounds that were already here

    def test_a_suppressed_pr_does_not_consume_the_per_pass_cap(self):
        """The conflicted one is not a question, so it must not eat the one picker this pass may
        send. Checked ahead of the cap for exactly this reason."""
        calls, report = self._sweep([row(573, mergeable="CONFLICTING"),
                                     row(574, mergeable="MERGEABLE")])
        self.assertEqual(calls, [574])
        self.assertEqual([a["pr"] for a in report["asked"]], [574])

    def test_a_suppressed_pr_is_never_DEFERRED(self):
        """`deferred` is the field that promises *the next pass will ask about this*. A conflicted PR
        carries no such promise, so putting it there would be a lie in the report."""
        _, report = self._sweep([row(573, mergeable="CONFLICTING")])
        self.assertEqual(report["deferred"], [])

    def test_the_moved_head_re_ask_still_works_when_the_new_head_can_merge(self):
        """The `(repo, pr, head_sha)` keying means a rebase is already a
        question nobody asked, and this change must not cost that. Three passes: asked, suppressed
        after a rebase that still conflicts, asked again once it can merge."""
        self.sweep_for_real([row(574, mergeable="MERGEABLE")])
        self.assertEqual(len(self.sends), 1)
        self.sweep_for_real([row(574, head=OTHER_HEAD, mergeable="CONFLICTING")], head=OTHER_HEAD)
        self.assertEqual(len(self.sends), 1)
        self.sweep_for_real([row(574, head=OTHER_HEAD, mergeable="MERGEABLE")], head=OTHER_HEAD)
        self.assertEqual(len(self.sends), 2)
        self.assertEqual([a["head_sha"] for a in mg.read_asks(self.dir)], [HEAD, OTHER_HEAD])

    def test_green_still_decides_first_so_a_red_conflicted_pr_is_not_even_looked_at(self):
        _, report = self._sweep([row(573, RED, mergeable="CONFLICTING")])
        self.assertEqual(report["looked_at"], 0)
        self.assertEqual(report["skipped"], [])

    def test_a_conflicted_draft_is_still_just_a_draft(self):
        """`candidates` drops drafts before mergeability is ever consulted, so un-drafting still gets
        asked about normally rather than being recorded as a conflicted skip."""
        _, report = self._sweep([row(573, draft=True, mergeable="CONFLICTING")])
        self.assertEqual(report["looked_at"], 0)
        self.assertEqual(report["skipped"], [])

    def test_the_already_asked_pre_filter_still_skips_on_an_unchanged_head(self):
        mg.record_ask(self.dir, 414, HEAD, "q1", via="auto-green", repo=REPO)
        calls, report = self._sweep([row(414, mergeable="MERGEABLE")])
        self.assertEqual(calls, [])
        self.assertEqual(len(report["skipped"]), 1)
        self.assertFalse(report["skipped"][0].get("conflicted"))

    # ---- it costs no second call, and it says what it did

    def test_mergeable_rides_the_list_call_this_pass_already_makes(self):
        """The whole design: one more field on one `gh pr list`, never a `gh pr view` per candidate —
        `--json number,mergeable,headRefOid,isDraft` returns mergeability in a single response."""
        self.assertIn("mergeable", pr_sweep.LIST_FIELDS.split(","))
        argvs = []
        inner = lister({REPO: [row(573, mergeable="CONFLICTING")]})

        def counting(argv):
            argvs.append(argv)
            return inner(argv)

        pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, asker=self._asker([]),
                       lister=counting, now=NOON)
        self.assertEqual(len(argvs), 1)
        self.assertIn("mergeable", argvs[0][argvs[0].index("--json") + 1].split(","))

    def test_the_summary_line_names_what_it_did_not_ask_about(self):
        """A suppressed question is the one thing in the report that looks exactly like nothing
        having been there, so `presence.log` has to say it out loud."""
        _, report = self._sweep([row(573, mergeable="CONFLICTING"),
                                 row(577, mergeable="CONFLICTING")])
        line = pr_sweep.summary_line(report)
        self.assertIn("cannot merge", line)
        self.assertIn("573", line)
        self.assertIn("577", line)

    def test_the_summary_line_is_silent_when_nothing_was_suppressed(self):
        _, report = self._sweep([row(414, mergeable="MERGEABLE")])
        self.assertNotIn("cannot merge", pr_sweep.summary_line(report))

    def test_the_report_still_survives_json_with_the_new_key_on_it(self):
        _, report = self._sweep([row(573, mergeable="CONFLICTING")])
        self.assertIn("conflicted", json.loads(json.dumps(report))["skipped"][0])


# ---------------------------------------- a PR that is BEHIND its base is not worth asking about

class ItNeverAsksAboutAPrThatIsBehindItsBaseTest(SweepCase):
    """**A base that requires up-to-date branches refuses a merely-behind PR.** A PR that is behind
    its base has NO textual conflict — `mergeable` reads `MERGEABLE` — so `unmergeable_reason` alone
    cannot see it, and a picker sent about it spends a tap on a merge GitHub is going to refuse.

    `BEHIND` is kept in its own dict (`BEHIND_STATES`) rather than folded into `CANNOT_MERGE_STATES`,
    because the two have different fixes — a conflict needs a hand resolution, BEHIND needs a plain
    merge-up — and the report distinguishes them with a separate `behind` flag rather than overloading
    `conflicted`."""

    def _asker(self, calls):
        def ask(pr, **kw):
            calls.append(pr)
            return {"ok": True, "sent": True, "reason": "sent", "question_id": f"q{len(calls)}"}
        return ask

    def _sweep(self, rows, **kw):
        calls = []
        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, asker=self._asker(calls),
                                lister=lister({REPO: rows}), now=NOON, **kw)
        return calls, report

    def test_a_behind_pr_gets_no_picker(self):
        calls, report = self._sweep([row(574, merge_state_status="BEHIND")])
        self.assertEqual(calls, [])
        self.assertEqual(report["asked"], [])

    def test_the_suppression_says_why_and_flags_behind_rather_than_conflicted(self):
        _, report = self._sweep([row(574, merge_state_status="BEHIND")])
        self.assertEqual(len(report["skipped"]), 1)
        skip = report["skipped"][0]
        self.assertEqual((skip["repo"], skip["pr"]), (REPO, 574))
        self.assertTrue(skip["behind"])
        self.assertFalse(skip.get("conflicted"))
        self.assertIn("BEHIND", skip["reason"])
        self.assertIn("merge", skip["reason"].lower())

    def test_case_and_surrounding_space_do_not_smuggle_it_past_the_check(self):
        for value in ("behind", " BEHIND ", "Behind"):
            with self.subTest(value=value):
                calls, _ = self._sweep([row(574, merge_state_status=value)])
                self.assertEqual(calls, [])

    def test_a_merely_behind_pr_still_reads_mergeable_so_conflicting_is_not_confused_with_it(self):
        """A BEHIND head has NO textual conflict — this is the whole reason `mergeable` alone cannot
        see it, and the fixture proves the two fields are independent."""
        calls, _ = self._sweep([row(574, mergeable="MERGEABLE", merge_state_status="BEHIND")])
        self.assertEqual(calls, [])

    def test_everything_else_asks(self):
        for value in ("CLEAN", "UNSTABLE", "DIRTY", "BLOCKED", "DRAFT", "HAS_HOOKS", "UNKNOWN", ""):
            with self.subTest(value=value):
                calls, _ = self._sweep([row(414, merge_state_status=value)])
                self.assertEqual(calls, [414])

    def test_a_missing_field_asks(self):
        calls, _ = self._sweep([row(414)])
        self.assertEqual(calls, [414])

    def test_a_null_or_non_string_value_asks(self):
        for value in (None, 0, False, [], {}, ["BEHIND"]):
            with self.subTest(value=value):
                calls, _ = self._sweep([row(414) | {"mergeStateStatus": value}])
                self.assertEqual(calls, [414])

    def test_the_refusal_is_an_allow_list_never_a_negation(self):
        src = source("pr_sweep.py")
        self.assertIn("BEHIND_STATES", src)
        self.assertNotIn('!= "CLEAN"', src)
        self.assertNotIn("!= 'CLEAN'", src)

    def test_the_predicate_answers_a_reason_not_a_bool(self):
        self.assertEqual(pr_sweep.behind_reason({}), "")
        self.assertEqual(pr_sweep.behind_reason({"mergeStateStatus": "CLEAN"}), "")
        self.assertIn("BEHIND", pr_sweep.behind_reason({"mergeStateStatus": "BEHIND"}))

    def test_mergestatestatus_rides_the_list_call_this_pass_already_makes(self):
        self.assertIn("mergeStateStatus", pr_sweep.LIST_FIELDS.split(","))
        argvs = []
        inner = lister({REPO: [row(573, merge_state_status="BEHIND")]})

        def counting(argv):
            argvs.append(argv)
            return inner(argv)

        pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, asker=self._asker([]),
                       lister=counting, now=NOON)
        self.assertEqual(len(argvs), 1)
        self.assertIn("mergeStateStatus", argvs[0][argvs[0].index("--json") + 1].split(","))

    def test_a_behind_pr_does_not_consume_the_per_pass_cap(self):
        calls, report = self._sweep([row(573, merge_state_status="BEHIND"),
                                     row(574, merge_state_status="CLEAN")])
        self.assertEqual(calls, [574])
        self.assertEqual([a["pr"] for a in report["asked"]], [574])

    def test_a_behind_pr_is_never_deferred(self):
        _, report = self._sweep([row(573, merge_state_status="BEHIND")])
        self.assertEqual(report["deferred"], [])

    def test_a_suppressed_behind_pr_asks_the_moment_it_is_merged_up(self):
        """No ledger row is written, so this is a HOLD and never a drop — the same guarantee the
        conflicted class already carries, one field over."""
        self.sweep_for_real([row(573, merge_state_status="BEHIND")])
        self.assertEqual(self.sends, [])
        self.assertEqual(mg.read_asks(self.dir), [])
        self.sweep_for_real([row(573, merge_state_status="CLEAN")])
        self.assertEqual(len(self.sends), 1)

    def test_the_summary_line_names_what_it_did_not_ask_about(self):
        _, report = self._sweep([row(573, merge_state_status="BEHIND"),
                                 row(577, merge_state_status="BEHIND")])
        line = pr_sweep.summary_line(report)
        self.assertIn("behind their base", line)
        self.assertIn("573", line)
        self.assertIn("577", line)

    def test_a_conflicting_and_a_behind_pr_are_reported_in_their_own_buckets(self):
        _, report = self._sweep([row(573, mergeable="CONFLICTING"),
                                 row(574, merge_state_status="BEHIND")])
        line = pr_sweep.summary_line(report)
        self.assertIn("cannot merge", line)
        self.assertIn("behind their base", line)


class AskingIsAllItCanDoTest(SweepCase):
    """The sweep may only ever deliver a question. Nothing here may shorten the path to a merge."""

    def test_the_sweep_never_calls_the_approval_writer(self):
        """Asserted in CALL form — `record_approval(` — which is exactly the shape
        `merge_guard`'s own single-caller scan looks for, so the two cannot disagree about what
        counts. Naming it in prose is how the module explains what it may not do."""
        src = source("pr_sweep.py")
        self.assertNotIn("record_approval(", src)
        self.assertNotIn("consume_approval(", src)

    def test_the_sweep_cannot_merge(self):
        src = source("pr_sweep.py")
        self.assertNotIn("pr merge", src)
        self.assertNotIn("decide_command", src)

    @unittest.skipUnless(HAS_PR_WATCH_TASK, PR_WATCH_SKIP)
    def test_the_daemon_task_calls_neither_either(self):
        body = source("presence.py")
        start = body.index("async def pr_watch_task")
        task = body[start:body.index("async def _supervise", start)]
        self.assertNotIn("record_approval(", task)
        self.assertNotIn("consume_approval(", task)
        self.assertNotIn("pr merge", task)
        self.assertTrue(hasattr(presence, "pr_watch_task"))

    @unittest.skipUnless(HAS_APPROVAL_CALLER,
                         "presence._merge_approval_clause (the approval writer's one caller) lands "
                         "in wave 26")
    def test_record_approval_still_has_exactly_one_caller_after_this_lands(self):
        """`merge_guard`'s own ``ApprovalIsNotAgentMintableTest`` scans every sibling script; this
        re-asserts it here so adding a finder can never be the thing that quietly widens it."""
        callers = [n for n in os.listdir(SCRIPT_DIR)
                   if n.endswith(".py") and not n.startswith("test_") and n != "merge_guard.py"
                   and "record_approval(" in source(n)]
        self.assertEqual(callers, ["presence.py"])

    def test_a_swept_pr_is_still_refused_until_the_owner_actually_taps(self):
        """The end-to-end promise: the picker went out, and the merge is exactly as blocked as it
        was before it did."""
        self.sweep_for_real([row(414)])
        self.assertEqual(len(self.sends), 1)
        with mock.patch.object(mg, "_run", gh_view()):
            decision = mg.decide_command("gh pr merge 414 --merge", state_dir=self.dir, now=NOON)
        self.assertFalse(decision.allow)
        self.assertIn("BLOCKED by the merge guard", decision.reason)

    def test_a_failed_send_records_nothing_so_the_next_pass_retries(self):
        with mock.patch.object(mg, "_run", gh_view()), \
                mock.patch.object(mg, "_send_question", sender(self.sends, ok=False)):
            report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir,
                                    lister=lister({REPO: [row(414)]}), now=NOON)
        self.assertEqual(report["asked"], [])
        self.assertEqual(mg.read_asks(self.dir), [])
        self.assertTrue(report["errors"])

    def test_the_pre_filter_can_only_skip_never_send(self):
        """`already_handled` saves a subprocess; it must never be able to cause an ask. A row it
        cannot key (no url, no head) declines to pre-filter and falls through to the guard, which is
        the side that can deny."""
        self.assertFalse(pr_sweep.already_handled(self.dir, {"number": 1}))
        self.assertFalse(pr_sweep.already_handled(self.dir, row(1) | {"url": "nonsense"}))
        self.assertFalse(pr_sweep.already_handled(self.dir, row(1) | {"headRefOid": ""}))


class NoSecondClassifierTest(SweepCase):
    def test_the_green_verdict_is_watch_prs_own_function(self):
        self.assertIn("watch_pr.classify", source("pr_sweep.py"))

    def test_it_grew_no_second_rollup_vocabulary(self):
        """`SUCCESS`/`COMPLETED`/`FAILURE` live in `watch_pr._OK` and its comment. Re-spelling them
        here is how the two verdicts start disagreeing."""
        src = source("pr_sweep.py")
        for token in ("SUCCESS", "COMPLETED", "FAILURE", "IN_PROGRESS"):
            self.assertNotIn(token, src)

    def test_it_grew_no_second_docs_only_classifier(self):
        src = source("pr_sweep.py")
        self.assertNotIn("non_docs_paths", src)
        self.assertNotIn("DOCS_ONLY_ALLOWLIST", src)
        self.assertNotIn('".md"', src)

    def test_it_grew_no_second_dedupe(self):
        """The ask ledger is read through the guard's own `already_asked`, never by opening its
        file — two readers of one ledger is how the two keys start to disagree."""
        src = source("pr_sweep.py")
        self.assertIn("mg.already_asked", src)
        self.assertNotIn("merge-ask-log", src)
        self.assertNotIn("read_asks", src)

    def test_it_builds_no_picker_of_its_own(self):
        src = source("pr_sweep.py")
        self.assertNotIn("request_argv", src)
        self.assertNotIn("_send_question", src)

    def test_the_quiet_window_is_the_guards_predicate_not_a_second_copy(self):
        src = source("pr_sweep.py")
        self.assertIn("mg.in_quiet_hours", src)
        self.assertNotIn("CURFEW", src)

    def test_module_scope_imports_only_stdlib_and_siblings(self):
        # `picker_retire`, `picker_mark`, `pr_red_notify` and `repo_config` are the same kind of
        # import the others are — a sibling that owns a decision this file refuses to make. The sweep hands each one the views of its
        # open list and nothing else; whether a PR may be retired is
        # `merge_guard.not_open_refusal`'s answer, reached through that module, what state a picker is
        # in is `picker_mark.classify`'s, reached through that one, and the red-notify ledger + send
        # are `pr_red_notify`'s, reached through it; which repositories are watched is
        # `repo_config`'s. None is re-derived here. `job_pr_draft` is there for the identical reason:
        # whether a PR's branch is checked out in a running job's worktree is `inflight_job_for`'s
        # answer, reached through that module.
        for line in source("pr_sweep.py").splitlines():
            if line.startswith("import ") or line.startswith("from "):
                mod = line.split()[1].split(".")[0]
                self.assertIn(mod, {"argparse", "datetime", "json", "os", "subprocess", "sys",
                                    "job_pr_draft", "merge_guard", "picker_mark", "picker_retire",
                                    "pr_red_notify", "repo_config", "watch_pr", "__future__"},
                              line)

    def test_it_decides_nothing_about_retiring_a_picker(self):
        """The stale-picker pass is `picker_retire`'s, and the temptation this file must resist is
        the same one it resists for the docs-only classifier: a second opinion here about which
        questions may be withdrawn is a second opinion that can drift from the one doing it."""
        src = source("pr_sweep.py")
        self.assertNotIn("telegram-questions", src)
        self.assertNotIn("retired_at", src)
        self.assertNotIn("merge-approval", src)


class StalePickerWiringTest(SweepCase):
    """The sweep hands the retirement the list it already paid for — and hands it a TRUSTWORTHY one.

    The whole cost argument (no second poller, zero added API calls in the steady state) rests on
    the index being a by-product of the `gh pr list` above it, and the whole SAFETY argument rests on
    a list that might be truncated arriving as `None` rather than as a set with holes in it."""

    def sweep_capturing(self, rows, **kw):
        seen = {}

        def retirer(**kwargs):
            seen.update(kwargs)
            return {"retired": [], "errors": []}

        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON, max_asks=0,
                                lister=lister({REPO: rows}), asker=lambda *a, **k: {"ok": True},
                                retirer=retirer, **kw)
        return report, seen

    def test_the_open_index_is_the_list_this_pass_already_read(self):
        report, seen = self.sweep_capturing([row(410), row(411, rollup=RED)])
        self.assertEqual(seen["open_index"], {mg.repo_key(REPO): {410, 411}},
                         "every OPEN PR belongs in the index, green or not — CI has nothing to do "
                         "with whether a picker's question still has an answer")
        # The index is a by-product handed straight over, never a report field: its values are sets
        # and `main` prints the report as JSON.
        self.assertNotIn("open_index", report)
        self.assertEqual(json.loads(json.dumps(report)), report)

    def test_a_page_limited_repo_contributes_None_not_a_partial_set(self):
        rows = [row(n) for n in range(1, pr_sweep.PR_LIST_LIMIT + 1)]
        _, seen = self.sweep_capturing(rows)
        self.assertIsNone(seen["open_index"][mg.repo_key(REPO)])

    def test_an_unreadable_repo_contributes_None_and_does_not_stop_the_pass(self):
        def broken(argv):
            return 1, "", "gh: not logged in"

        seen = {}

        def retirer(**kwargs):
            seen.update(kwargs)
            return {"retired": [], "errors": []}

        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON, lister=broken,
                                asker=lambda *a, **k: {"ok": True}, retirer=retirer)
        self.assertIsNone(seen["open_index"][mg.repo_key(REPO)])
        self.assertTrue(report["errors"])

    def test_a_retirement_that_raises_costs_the_pass_nothing(self):
        def exploding(**kwargs):
            raise RuntimeError("boom")

        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON,
                                lister=lister({REPO: [row(410)]}),
                                asker=lambda *a, **k: {"ok": True}, retirer=exploding)
        self.assertTrue(any("stale-picker" in e for e in report["errors"]))

    def test_quiet_hours_still_TIDY_because_tidying_cannot_wake_anyone(self):
        """**Deliberate, and this is the test that records why.** An early return inside the window
        would be correct if asking were the only thing after it — and is wrong once marking joins
        it, because a reaction raises no notification and an edit to an existing message raises
        none either. **Neither can reach the owner's phone, and an owner up late still taps**, so
        both run and the morning queue is honest rather than hours stale. The asking is still
        suppressed, which is the whole of what the window protects."""
        called = []
        with mock.patch.object(mg, "ASK_ON_GREEN_QUIET_HOURS", True):
            pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=QUIET_NIGHT,
                           lister=lister({REPO: [row(410)]}),
                           asker=lambda *a, **k: self.fail("asked inside the quiet window"),
                           marker=lambda **kw: {"marked": [], "errors": []},
                           retirer=lambda **kw: called.append(kw) or {"retired": [], "errors": []})
        self.assertEqual(len(called), 1)
        self.assertEqual(set(called[0]["open_index"]), {mg.repo_key(REPO)})

    def test_the_head_index_is_built_off_the_same_rows_as_the_numbers(self):
        """The join that lets a picker notice its commit moved. **One `gh pr list`, two views of
        it** — a second call could describe two different reads of one repository."""
        _, seen = self.sweep_capturing([row(410), row(411, head=OTHER_HEAD)])
        self.assertEqual(seen["head_index"], {mg.repo_key(REPO): {410: HEAD, 411: OTHER_HEAD}})
        self.assertEqual(set(seen["open_index"][mg.repo_key(REPO)]),
                         set(seen["head_index"][mg.repo_key(REPO)]))

    def test_a_page_limited_repo_contributes_no_heads_either(self):
        rows = [row(n) for n in range(1, pr_sweep.PR_LIST_LIMIT + 1)]
        _, seen = self.sweep_capturing(rows)
        self.assertIsNone(seen["head_index"][mg.repo_key(REPO)],
                          "at the page limit a head map has holes in it, and a hole is not a "
                          "commit — the whole repo declines")


class OpenHeadsTest(unittest.TestCase):
    """`open_heads` — the second view of the list, and every way it says *I do not know*."""

    def test_it_reads_the_head_off_rows_already_fetched(self):
        self.assertEqual(pr_sweep.open_heads([row(1), row(2, head=OTHER_HEAD)]),
                         {1: HEAD, 2: OTHER_HEAD})

    def test_headrefoid_has_always_been_in_the_fields_so_this_costs_no_call(self):
        self.assertIn("headRefOid", pr_sweep.LIST_FIELDS)

    def test_an_unreadable_repo_is_None_not_empty(self):
        self.assertIsNone(pr_sweep.open_heads(None))

    def test_a_truncated_list_is_None(self):
        self.assertIsNone(pr_sweep.open_heads([row(1)], truncated=True))

    def test_a_row_with_no_readable_head_is_ABSENT_never_present_as_None(self):
        """Absent reads as *I do not know this PR's head*, which declines to retire. A `None` in
        the mapping is a value something downstream could compare a SHA against."""
        for bad in (None, "", "   ", 7, {"oid": HEAD}):
            with self.subTest(head=bad):
                heads = pr_sweep.open_heads([row(1) | {"headRefOid": bad}, row(2)])
                self.assertEqual(heads, {2: HEAD})
                self.assertNotIn(1, heads)

    def test_a_row_that_is_not_a_dict_or_has_no_number_is_skipped(self):
        self.assertEqual(pr_sweep.open_heads(["nope", {"headRefOid": HEAD}, row(3)]), {3: HEAD})

    def test_a_bool_number_is_not_a_pull_request(self):
        self.assertEqual(pr_sweep.open_heads([row(1) | {"number": True}]), {})


class MovedHeadReAskTest(SweepCase):
    """**The whole point of the fix, end to end: the dead question comes down and a live one takes
    its place, on the API calls the pass was already making.**

    A rebase or force-push leaves a picker pinned to a commit that has stopped being the head. Both
    halves of the repair ride facts already on the table — the ask log is keyed `(repo, pr,
    head_sha)`, so the new commit is *already* an unasked question, and `headRefOid` is *already*
    in the list `pr_sweep` runs every few minutes."""

    #: The instant every fixture and every pass in this class reads. Inside the picker's 7-day TTL,
    #: outside the quiet window (NOON is 12:00 owner-local).
    ASKED_AT = "2026-08-20T15:00:00Z"

    def picker(self, pr=414, head=HEAD, message_id=9001):
        """One never-answered merge picker in the shape `telegram_ask.ask` writes."""
        return {"question": f"Merge {REPO} PR #{pr}?", "body": "Tap one.\n\n1. Approve",
                "options": [{"label": "Approve", "description": "it merges"},
                            {"label": "Not now", "description": "nothing merges"}],
                "multi": False, "recommended": False, "chat_id": "123456",
                "message_id": message_id, "asked_at": self.ASKED_AT, "selected": [],
                "answered_at": None,
                "meta": {"kind": mg.APPROVAL_META_KIND, "pr": pr, "repo": REPO,
                         "head_sha": head, "approve_index": 0}}

    def write_picker(self, **questions):
        ta.save_store(ta.store_path(self.dir),
                      {"schema": ta.SCHEMA, "questions": dict(questions), "expired": []})

    def full_pass(self, rows, head=HEAD):
        """Drive `pr_sweep.sweep` through the REAL guard AND the REAL retirement, with `gh`, the
        picker send and the Bot API all stubbed. Returns `(report, gh_calls, api_calls)`."""
        gh_calls, api_calls = [], []
        real_view = gh_view(head=head)

        def counting_view(argv, cwd=None):
            gh_calls.append(argv)
            return real_view(argv, cwd)

        def api(c, method, params, timeout=30):
            api_calls.append((method, params))
            return {"ok": True, "result": {"message_id": params.get("message_id") or 1}}

        with mock.patch.object(mg, "_run", counting_view), \
                mock.patch.object(mg, "_send_question", sender(self.sends)), \
                mock.patch.object(ta, "api_call", api), \
                mock.patch.object(picker_retire, "_config",
                                  lambda env_file=None: ({"token": "t", "chat_id": "123456",
                                                          "api_base": "https://example.invalid",
                                                          "parse_mode": "", "format": "plain"}, "")):
            report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir,
                                    lister=lister({REPO: rows}), now=NOON)
        return report, gh_calls, api_calls

    def test_a_moved_head_retires_the_dead_picker_and_a_fresh_one_goes_out(self):
        self.write_picker(q1=self.picker())
        report, _, api_calls = self.full_pass([row(414, head=OTHER_HEAD)], head=OTHER_HEAD)

        self.assertEqual([a["pr"] for a in report["asked"]], [414],
                         "the new commit is an unasked question, so it is asked")
        self.assertEqual(len(self.sends), 1)
        self.assertEqual([(r["pr"], r["why"]) for r in report["retired"]],
                         [(414, picker_retire.MOVED_HEAD)])
        self.assertEqual([m for m, _ in api_calls], ["editMessageText"])
        self.assertTrue(ta.load_store(ta.store_path(self.dir))["questions"]["q1"]["retired_at"])

    def test_the_re_ask_costs_no_extra_api_call_and_neither_does_the_retirement(self):
        """The control is the same pass with nothing pending. **Identical `gh` spend**: the ask pays
        its ordinary `gh pr view` either way, and the retirement pays nothing at all because the
        `gh pr list` above it already carried the head."""
        self.write_picker()  # no pending picker
        _, control_gh, _ = self.full_pass([row(414, head=OTHER_HEAD)], head=OTHER_HEAD)

        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        self.sends = []
        self.write_picker(q1=self.picker())
        report, with_picker_gh, _ = self.full_pass([row(414, head=OTHER_HEAD)], head=OTHER_HEAD)

        self.assertEqual(len(with_picker_gh), len(control_gh))
        self.assertEqual(report["retirements"]["confirms"], 0,
                         "a moved head is positive evidence off the list, not an inference to "
                         "confirm")
        self.assertEqual([a[a.index("view") + 1] for a in with_picker_gh], ["414"],
                         "exactly the ask's own read of the PR, and nothing else")

    def test_an_unmoved_head_neither_re_asks_nor_retires(self):
        """The steady state, which is nearly every pass: the picker is still live and still the
        right question, so the pass does nothing to it and buzzes nobody."""
        self.write_picker(q1=self.picker())
        mg.record_ask(self.dir, 414, HEAD, "q1", via="auto-green", repo=REPO)
        report, _, api_calls = self.full_pass([row(414)])

        self.assertEqual(report["asked"], [])
        self.assertEqual(self.sends, [])
        self.assertEqual(report["retired"], [])
        self.assertEqual(api_calls, [])
        self.assertNotIn("retired_at", ta.load_store(ta.store_path(self.dir))["questions"]["q1"])

    def test_the_pass_asks_before_it_retires(self):
        """The order is ask -> retire -> repair, and the retire-before-repair half is load-bearing.
        Here the ask-before-retire half is what stops the PR being momentarily unrepresented: the
        new question is on the owner's phone before the old one settles."""
        self.write_picker(q1=self.picker())
        order = []

        def spy_asker(pr, **kw):
            order.append("ask")
            return {"ok": True, "sent": True, "question_id": "q2", "reason": "asked"}

        def spy_retirer(**kw):
            order.append("retire")
            return {"retired": [], "errors": []}

        pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON,
                       lister=lister({REPO: [row(414, head=OTHER_HEAD)]}),
                       asker=spy_asker, retirer=spy_retirer)
        self.assertEqual(order, ["ask", "retire"])

    def test_a_red_new_head_still_retires_the_dead_picker(self):
        """The two halves are independent, and this is the case that proves the retirement is not
        riding on the ask. CI is not green at the new commit, so no question goes out — and the
        picker pinned to the old one is dead all the same."""
        self.write_picker(q1=self.picker())
        report, _, api_calls = self.full_pass([row(414, RED, head=OTHER_HEAD)], head=OTHER_HEAD)
        self.assertEqual(report["asked"], [])
        self.assertEqual(self.sends, [])
        self.assertEqual([r["pr"] for r in report["retired"]], [414])
        self.assertEqual([m for m, _ in api_calls], ["editMessageText"])

    def test_the_report_survives_json_with_the_new_fields_on_it(self):
        """`main` prints the report; a set or a non-serialisable value on a retirement record would
        crash the diagnostic door rather than the pass."""
        self.write_picker(q1=self.picker())
        report, _, _ = self.full_pass([row(414, head=OTHER_HEAD)], head=OTHER_HEAD)
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertIn("head moved", pr_sweep.summary_line(report))


class RetireOnlyWideningTest(SweepCase):
    """**The resident pass retires across every repository with a pending picker — and asks about
    none of the extra ones.** A picker for a repository outside the watched set (sent by another
    session) would otherwise stand after its PR merged, because only the hand-run CLI ever read that
    repository, and the owner would have to tap "Not now" just to make it stop looking undecided.

    Two properties carry the class. The widening is REAL — an unwatched repository's merged-PR
    picker comes down on the ordinary pass — and it is RETIRE-ONLY: the extra rows reach the two
    silent tidyings and nothing else, so the WATCHED set stays exactly what was configured
    (`picker-state-marking-spec.md` §10.5 item 1). The asked-about set is untouched, which is the
    watched-repo-behaviour-is-unchanged assertion."""

    UNWATCHED = "example/third"
    ASKED_AT = "2026-08-20T15:00:00Z"

    def picker(self, pr, repo, head=HEAD, message_id=9001):
        return {"question": f"Merge {repo} PR #{pr}?", "body": "Tap one.\n\n1. Approve",
                "options": [{"label": "Approve", "description": "it merges"},
                            {"label": "Not now", "description": "nothing merges"}],
                "multi": False, "recommended": False, "chat_id": "123456",
                "message_id": message_id, "asked_at": self.ASKED_AT, "selected": [],
                "answered_at": None,
                "meta": {"kind": mg.APPROVAL_META_KIND, "pr": pr, "repo": repo,
                         "head_sha": head, "approve_index": 0}}

    def write_pickers(self, **questions):
        ta.save_store(ta.store_path(self.dir),
                      {"schema": ta.SCHEMA, "questions": dict(questions), "expired": []})

    def full_pass(self, mapping, states=None, **kw):
        """Drive `pr_sweep.sweep` through the REAL guard, the REAL marker and the REAL retirement,
        with `gh`, the picker send, the Bot API and the local git arm all stubbed. `states` maps a
        repo slug to what `gh pr view` says its PRs' `state` is. Returns `(report, gh_view_calls,
        api_calls, list_calls)`."""
        states = states or {}
        gh_calls, api_calls, list_calls = [], [], []

        def view(argv, cwd=None):
            gh_calls.append(argv)
            pr = int(argv[argv.index("view") + 1])
            slug = argv[argv.index("--repo") + 1] if "--repo" in argv else REPO
            return 0, json.dumps({"number": pr, "files": CODE_FILES, "headRefOid": HEAD,
                                  "state": states.get(slug, "OPEN"), "title": "a pull request",
                                  "url": f"https://github.com/{slug}/pull/{pr}",
                                  "mergedAt": "2026-08-20T13:50:00Z"}), ""

        base = lister(mapping)

        def counting_list(argv):
            list_calls.append(argv[argv.index("--repo") + 1])
            return base(argv)

        def api(c, method, params, timeout=30):
            api_calls.append((method, params))
            return {"ok": True, "result": {"message_id": params.get("message_id") or 1}}

        cfg = {"token": "t", "chat_id": "123456", "api_base": "https://example.invalid",
               "parse_mode": "", "format": "plain"}
        import picker_mark
        with mock.patch.object(mg, "_run", view), \
                mock.patch.object(mg, "_send_question", sender(self.sends)), \
                mock.patch.object(ta, "api_call", api), \
                mock.patch.object(picker_mark, "local_repo", lambda runner=None, cwd=None: ""), \
                mock.patch.object(picker_mark, "_config", lambda env_file=None: (cfg, "")), \
                mock.patch.object(picker_retire, "_config", lambda env_file=None: (cfg, "")):
            report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, lister=counting_list,
                                    now=NOON, **kw)
        return report, gh_calls, api_calls, list_calls

    def test_an_unwatched_repos_merged_pr_picker_is_retired(self):
        self.write_pickers(q1=self.picker(113, self.UNWATCHED))
        report, _, api_calls, list_calls = self.full_pass(
            {REPO: [], self.UNWATCHED: []}, states={self.UNWATCHED: "MERGED"})

        self.assertEqual([(r["repo"], r["pr"], r["why"]) for r in report["retired"]],
                         [(self.UNWATCHED, 113, picker_retire.NOT_OPEN)])
        self.assertIn("editMessageText", [m for m, _ in api_calls])
        rec = ta.load_store(ta.store_path(self.dir))["questions"]["q1"]
        self.assertTrue(rec["retired_at"])
        self.assertIsNone(rec["answered_at"], "retired is not answered — never fabricate a tap")
        self.assertEqual(list_calls, [REPO, self.UNWATCHED],
                         "the watched set first, then exactly the repositories a picker names")
        self.assertEqual(report["retire_repos"], [self.UNWATCHED])
        self.assertEqual(report["retire_repos_deferred"], [])

    def test_the_extra_repo_is_never_asked_about_even_with_a_green_open_pr(self):
        """The retire-only half. A green, mergeable, un-asked #114 sits open in the unwatched repo
        beside the merged #113's picker; the pass must retire the one and say NOTHING about the
        other — no picker, no red notice, no ledger row."""
        self.write_pickers(q1=self.picker(113, self.UNWATCHED))
        rows = [row(114, repo=self.UNWATCHED), row(115, rollup=RED, repo=self.UNWATCHED)]
        report, gh_calls, _, _ = self.full_pass(
            {REPO: [], self.UNWATCHED: rows}, states={self.UNWATCHED: "MERGED"})

        self.assertEqual([r["pr"] for r in report["retired"]], [113])
        self.assertEqual(report["asked"], [])
        self.assertEqual(self.sends, [])
        self.assertEqual(report["notified"], [])
        self.assertEqual(report["red_looked_at"], 0)
        self.assertEqual(report["looked_at"], 0)
        self.assertEqual(report["deferred"], [])
        self.assertFalse(mg.already_asked(self.dir, 114, HEAD, repo=self.UNWATCHED))
        self.assertFalse(pr_red_notify.already_notified(self.dir, 115, HEAD, repo=self.UNWATCHED))
        # The only `gh pr view` spent is the retirement's own confirmation of #113 — the ask path
        # never so much as looked at #114.
        self.assertEqual([a[a.index("view") + 1] for a in gh_calls], ["113"])

    def test_an_unwatched_repos_still_open_unmoved_picker_is_left_alone(self):
        """The invariant, one repository over: an unchanged head never retires."""
        self.write_pickers(q1=self.picker(113, self.UNWATCHED))
        report, _, api_calls, _ = self.full_pass(
            {REPO: [], self.UNWATCHED: [row(113, repo=self.UNWATCHED)]})
        self.assertEqual(report["retired"], [])
        self.assertNotIn("editMessageText", [m for m, _ in api_calls])
        self.assertNotIn("retired_at", ta.load_store(ta.store_path(self.dir))["questions"]["q1"])

    def test_the_extra_rows_reach_the_marker_too(self):
        """Marking is a reaction on an existing message — as silent as the retirement, and its own
        CLI already covers every repository with a pending picker — so the pass hands the extra
        repositories to it as well, and a merged #113 wears 😴 before it is settled exactly as a
        watched repository's picker does. The local `git merge-tree` arm is scoped to the daemon's own checkout by
        `local_repo_key`, so an unwatched repository can never trigger it."""
        self.write_pickers(q1=self.picker(113, self.UNWATCHED))
        report, _, api_calls, _ = self.full_pass(
            {REPO: [], self.UNWATCHED: []}, states={self.UNWATCHED: "MERGED"})
        self.assertEqual([(m["repo"], m["pr"], m["state"]) for m in report["marked"]],
                         [(self.UNWATCHED, 113, "asleep")])
        self.assertEqual([m for m, _ in api_calls], ["setMessageReaction", "editMessageText"])
        self.assertEqual(report["markings"]["local_checks"], 0)

    def test_the_cap_holds_the_rest_over_named_never_dropped(self):
        repos = [f"example/repo-{i:02d}" for i in range(12)]
        self.write_pickers(**{f"q{i}": self.picker(1, r) for i, r in enumerate(repos)})
        seen = {}

        def retirer(**kwargs):
            seen.update(kwargs)
            return {"retired": [], "errors": []}

        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON, max_asks=0,
                                lister=lister({r: [] for r in repos + [REPO]}),
                                asker=lambda *a, **k: {"ok": True},
                                marker=lambda **kw: {"marked": [], "errors": []}, retirer=retirer)
        self.assertEqual(picker_retire.MAX_EXTRA_REPOS_PER_PASS, 10)
        self.assertEqual(report["retire_repos"], repos[:10])
        self.assertEqual(report["retire_repos_deferred"], repos[10:])
        self.assertEqual(set(seen["open_index"]) - {mg.repo_key(REPO)},
                         {mg.repo_key(r) for r in repos[:10]})
        for r in repos[10:]:
            self.assertNotIn(mg.repo_key(r), seen["open_index"],
                             "a held repository is NOT in the index — absence from an index it was "
                             "never in must read as 'not swept', never as 'not open'")
        line = pr_sweep.summary_line(report)
        self.assertIn("2 pending-picker repo(s) past the retire-read cap", line)
        self.assertIn("example/repo-10", line)
        self.assertEqual(json.loads(json.dumps(report)), report)

    def test_a_cap_of_zero_reads_the_watched_set_only(self):
        self.write_pickers(q1=self.picker(113, self.UNWATCHED))
        report, _, _, list_calls = self.full_pass(
            {REPO: [], self.UNWATCHED: []}, states={self.UNWATCHED: "MERGED"}, max_extra_repos=0)
        self.assertEqual(list_calls, [REPO])
        self.assertEqual(report["retired"], [])
        self.assertEqual(report["retire_repos"], [])
        self.assertEqual(report["retire_repos_deferred"], [self.UNWATCHED])

    def test_a_watched_repo_is_never_read_twice(self):
        """A picker naming a watched repository; `extra_retire_repos` must key on the guard's
        repo key so it is excluded however the picker spells it."""
        self.write_pickers(q1=self.picker(414, REPO.upper()))
        self.assertEqual(pr_sweep.extra_retire_repos(self.dir, (REPO,)), [])
        _, _, _, list_calls = self.full_pass({REPO: [row(414)]})
        self.assertEqual(list_calls, [REPO])

    def test_no_pending_pickers_means_no_extra_reads_at_all(self):
        """The steady state: the widening costs nothing when there is nothing to tidy."""
        self.write_pickers()
        report, _, _, list_calls = self.full_pass({REPO: []})
        self.assertEqual(list_calls, [REPO])
        self.assertEqual(report["retire_repos"], [])

    def test_an_unreadable_extra_repo_costs_only_itself(self):
        self.write_pickers(q1=self.picker(113, self.UNWATCHED), q2=self.picker(414, REPO))

        def flaky(argv):
            slug = argv[argv.index("--repo") + 1]
            if slug == self.UNWATCHED:
                return 1, "", "gh: not logged in"
            return 0, json.dumps([]), ""

        seen = {}

        def retirer(**kwargs):
            seen.update(kwargs)
            return {"retired": [], "errors": []}

        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON, max_asks=0,
                                lister=flaky, asker=lambda *a, **k: {"ok": True},
                                marker=lambda **kw: {"marked": [], "errors": []}, retirer=retirer)
        self.assertIsNone(seen["open_index"][mg.repo_key(self.UNWATCHED)],
                          "unreadable contributes None — 'could not look', never 'nothing there'")
        self.assertEqual(seen["open_index"][mg.repo_key(REPO)], set())
        self.assertTrue(any("retire-only read" in e for e in report["errors"]))

    def test_a_page_limited_extra_repo_contributes_None(self):
        self.write_pickers(q1=self.picker(113, self.UNWATCHED))
        seen = {}

        def retirer(**kwargs):
            seen.update(kwargs)
            return {"retired": [], "errors": []}

        rows = [row(n, repo=self.UNWATCHED) for n in range(1, pr_sweep.PR_LIST_LIMIT + 1)]
        pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON, max_asks=0,
                       lister=lister({REPO: [], self.UNWATCHED: rows}),
                       asker=lambda *a, **k: {"ok": True},
                       marker=lambda **kw: {"marked": [], "errors": []}, retirer=retirer)
        self.assertIsNone(seen["open_index"][mg.repo_key(self.UNWATCHED)])
        self.assertIsNone(seen["head_index"][mg.repo_key(self.UNWATCHED)])

    def test_an_unreadable_store_falls_back_to_the_watched_set(self):
        with open(ta.store_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        seen = {}

        def retirer(**kwargs):
            seen.update(kwargs)
            return {"retired": [], "errors": []}

        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON, max_asks=0,
                                lister=lister({REPO: [row(410)]}),
                                asker=lambda *a, **k: {"ok": True},
                                marker=lambda **kw: {"marked": [], "errors": []}, retirer=retirer)
        self.assertEqual(set(seen["open_index"]), {mg.repo_key(REPO)})
        self.assertEqual(report["retire_repos"], [])

    def test_the_enumeration_is_picker_retires_own_not_a_second_copy(self):
        body = source("pr_sweep.py")
        self.assertIn("picker_retire.pending_repos_in(", body)
        self.assertNotIn("RETIRABLE_META_KINDS", body,
                         "which pickers are retirable is picker_retire's rule; this file may not "
                         "grow its own reading of the store")

    def test_the_asked_about_set_is_untouched_by_the_widening(self):
        """The ASK set is the watched set and nothing else: a pending picker for another repository
        widens what gets READ, never what gets asked about."""
        self.write_pickers(q1=self.picker(113, self.UNWATCHED))
        with mock.patch.object(repo_config, "watched_repos", return_value=(REPO,)):
            self.assertEqual(pr_sweep.DEFAULT_REPOS, (REPO,))
            self.assertNotIn(self.UNWATCHED, pr_sweep.default_repos())


# --------------------------------------------------------------------- the burst

class BurstBoundTest(SweepCase):
    def test_five_prs_going_green_together_produce_one_picker_not_five(self):
        report = self.sweep_for_real([row(n) for n in (410, 411, 412, 413, 414)])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual([a["pr"] for a in report["asked"]], [410])

    def test_the_held_over_prs_are_named_rather_than_silently_dropped(self):
        report = self.sweep_for_real([row(n) for n in (410, 411, 412, 413, 414)])
        self.assertEqual([d["pr"] for d in report["deferred"]], [411, 412, 413, 414])
        self.assertIn("411", pr_sweep.summary_line(report))

    def test_the_next_pass_asks_about_the_next_one(self):
        """A queue, not a drop. The cap converts simultaneity into cadence and nothing else."""
        rows = [row(n) for n in (410, 411, 412)]
        self.sweep_for_real(rows)
        self.sweep_for_real(rows)
        self.assertEqual(len(self.sends), 2)
        asked = sorted(a["pr"] for a in mg.read_asks(self.dir))
        self.assertEqual(asked, [410, 411])

    def test_a_restart_re_seeing_every_open_pr_asks_about_none_of_them(self):
        """The daemon reboots on every merge. The ask log is on disk and keyed on
        `(repo, pr, head_sha)`, so bound 2 needed no new mechanism at all."""
        rows = [row(n) for n in (410, 411, 412)]
        self.sweep_for_real(rows, max_asks=9)
        before = len(self.sends)
        report = self.sweep_for_real(rows, max_asks=9)  # "reboot": same PRs, same commits
        self.assertEqual(len(self.sends), before)
        self.assertEqual(report["asked"], [])
        self.assertEqual(len(report["skipped"]), 3)

    def test_the_pre_filter_spends_no_subprocess_on_an_already_asked_pr(self):
        calls = []

        def counting_asker(pr, **kw):
            calls.append(pr)
            return {"ok": True, "sent": False, "reason": "x"}

        mg.record_ask(self.dir, 414, HEAD, "q1", via="auto-green", repo=REPO)
        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, asker=counting_asker,
                                lister=lister({REPO: [row(414)]}), now=NOON)
        self.assertEqual(calls, [])
        self.assertEqual(report["looked_at"], 1)
        self.assertEqual(len(report["skipped"]), 1)

    def test_new_commits_reopen_the_question(self):
        """A new head SHA is a new artifact, so it is a new question — the same binding the approval
        record uses. Bound 2 must not become "asked once, never again"."""
        self.sweep_for_real([row(414)])
        self.sweep_for_real([row(414, head=OTHER_HEAD)], head=OTHER_HEAD)
        self.assertEqual(len(self.sends), 2)

    def test_max_asks_zero_disables_asking_without_disabling_the_sweep(self):
        report = self.sweep_for_real([row(414)], max_asks=0)
        self.assertEqual(self.sends, [])
        self.assertEqual(report["looked_at"], 1)
        self.assertEqual([d["pr"] for d in report["deferred"]], [414])

    def test_one_per_pass_is_the_shipped_default(self):
        self.assertEqual(pr_sweep.MAX_ASKS_PER_PASS, 1)

    def test_a_page_limit_hit_is_reported_rather_than_silently_truncating(self):
        rows = [row(n, PENDING) for n in range(1, pr_sweep.PR_LIST_LIMIT + 1)]
        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, asker=lambda *a, **k: {},
                                lister=lister({REPO: rows}), now=NOON)
        self.assertTrue(any("page limit" in e for e in report["errors"]))

    def test_the_first_pass_does_not_seed(self):
        """A feed watcher seeds its first run so nothing bulk-pulls a back catalogue. Here that would
        write ask-log rows for pickers that never went out, and the PR would never get its question —
        the blocked-and-couldn't-ask collapse the guard refuses on its failed-send path. So the held
        PR leaves NO ledger row: the cap is a queue, and #411 is asked about next pass."""
        report = self.sweep_for_real([row(410), row(411)])
        self.assertEqual([a["pr"] for a in mg.read_asks(self.dir)], [410])
        self.assertEqual([d["pr"] for d in report["deferred"]], [411])
        self.sweep_for_real([row(410), row(411)])
        self.assertEqual([a["pr"] for a in mg.read_asks(self.dir)], [410, 411])


# --------------------------------------------------------------------- 3 AM

class QuietHoursTest(SweepCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(mg, "ASK_ON_GREEN_QUIET_HOURS", True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_no_picker_is_sent_inside_the_window(self):
        """**The window's whole content, and the only part of it that is a rule.** An early return
        before the first network call would be a cost optimisation that changes no behaviour only
        while asking is all the pass does — with `picker_mark` behind it, it would withhold the
        marker for a quarter of every day. So the pass looks; what it must never do is ASK."""
        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=QUIET_NIGHT,
                                lister=lister({REPO: [row(414)]}),
                                asker=lambda *a, **k: self.fail("asked inside the quiet window"),
                                marker=lambda **kw: {"marked": [], "errors": []},
                                retirer=lambda **kw: {"retired": [], "errors": []})
        self.assertTrue(report["quiet_hours"])
        self.assertEqual(report["asked"], [])

    def test_a_held_picker_is_DEFERRED_rather_than_skipped(self):
        """`deferred` is the field that promises the next pass will ask. A quiet-hours hold belongs
        there and not in `skipped`, which reads as *dealt with*."""
        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=QUIET_NIGHT,
                                lister=lister({REPO: [row(414)]}), asker=lambda *a, **k: {},
                                marker=lambda **kw: {"marked": [], "errors": []},
                                retirer=lambda **kw: {"retired": [], "errors": []})
        self.assertEqual([d_["pr"] for d_ in report["deferred"]], [414])
        self.assertIn("quiet window", report["deferred"][0]["reason"])

    def test_the_summary_line_still_says_what_the_pass_DID(self):
        """It used to be one whole line meaning *nothing happened*. A marker set at 3 AM rendered as
        'no sweep' is exactly the quiet lie the rest of `summary_line` exists to avoid."""
        line = pr_sweep.summary_line({
            "quiet_hours": True, "looked_at": 1, "asked": [], "deferred": [], "skipped": [],
            "errors": [], "markings": {"marked": [{"repo": REPO, "pr": 414, "state": "asleep"}]}})
        self.assertIn("quiet window", line)
        self.assertIn("marked asleep", line)

    def test_the_guards_own_deferral_still_applies_on_this_path(self):
        """The short-circuit is a cost optimisation, not the rule. With it stepped past — the pass
        clock at noon, the guard's clock at 03:00 — nothing sends, which is what proves the deferral
        lives in the guard rather than in `sweep`."""
        def night_asker(pr, **kw):
            # The sweep threads its own pinned instant now, so stepping past the short-circuit means
            # DISCARDING it here rather than merely not supplying one — which is the point of the
            # test: with the two clocks forced apart on purpose, the guard is still the one refusing.
            kw.pop("now", None)
            return mg.ask_on_green(pr, now=QUIET_NIGHT, runner=gh_view(),
                                   sender=sender(self.sends), **kw)

        with mock.patch.object(mg, "_send_question", sender(self.sends)):
            report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, asker=night_asker,
                                    lister=lister({REPO: [row(414)]}), now=NOON)
        self.assertEqual(self.sends, [])
        self.assertEqual(report["asked"], [])
        self.assertTrue(any("quiet window" in s["reason"] for s in report["skipped"]))

    def test_a_quiet_hours_skip_records_nothing_so_it_is_recoverable(self):
        """A PR that happens to go green at 03:00 must not lose its picker permanently and
        silently — at 07:00 the queue drains at one per pass."""
        pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=QUIET_NIGHT,
                       lister=lister({REPO: [row(414)]}), asker=lambda *a, **k: {})
        self.assertEqual(mg.read_asks(self.dir), [])
        self.assertFalse(os.path.exists(mg.ask_log_path(self.dir)))

    # ----- the quiet window stands down while the owner is demonstrably awake

    def _turn(self, text, minutes_ago, speaker="owner"):
        import turns
        turns.record_turn(self.dir, surface="telegram", speaker=speaker, text=text,
                          now=QUIET_NIGHT - timedelta(minutes=minutes_ago))

    def _quiet_sweep(self, rows, asker, **kw):
        return pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=QUIET_NIGHT,
                              lister=lister({REPO: rows}), asker=asker,
                              marker=lambda **k: {"marked": [], "errors": []},
                              retirer=lambda **k: {"retired": [], "errors": []}, **kw)

    def test_the_owner_is_awake_so_the_pass_asks_inside_the_window(self):
        """End to end through the REAL guard: an owner message four minutes ago stands the window down in
        the sweep's short-circuit AND in `ask_on_green`'s own re-check, or nothing would send."""
        self._turn("still up — anything need me?", 4)
        report = self._quiet_sweep(
            [row(414)], lambda pr, **kw: mg.ask_on_green(pr, runner=gh_view(),
                                                         sender=sender(self.sends), **kw))
        self.assertFalse(report["quiet_hours"])
        self.assertEqual(report["awake_override"], "2026-08-20T07:56:00Z")
        self.assertEqual([a["pr"] for a in report["asked"]], [414])
        self.assertEqual(len(self.sends), 1)
        self.assertIn("the owner is awake", pr_sweep.summary_line(report))

    def test_a_job_finished_notice_does_not_stand_the_window_down(self):
        self._turn('[job finished: "feat(jobs): x" — done in 3m]', 1)
        report = self._quiet_sweep([row(414)],
                                   lambda *a, **k: self.fail("asked on a job notice"))
        self.assertTrue(report["quiet_hours"])
        self.assertNotIn("awake_override", report)

    def test_a_stale_message_keeps_the_window(self):
        self._turn("goodnight", 31)
        report = self._quiet_sweep([row(414)],
                                   lambda *a, **k: self.fail("asked on a stale message"))
        self.assertTrue(report["quiet_hours"])

    def test_an_unreadable_turns_file_keeps_the_window(self):
        self._turn("hi", 1)
        with mock.patch("turns.awake_since", side_effect=OSError("locked")):
            report = self._quiet_sweep([row(414)],
                                       lambda *a, **k: self.fail("asked on a read error"))
        self.assertTrue(report["quiet_hours"])
        self.assertEqual([d_["pr"] for d_ in report["deferred"]], [414])

    def test_the_red_arm_stands_down_too_when_the_owner_is_awake(self):
        self._turn("[the owner reacted 👍 (= yes) to: x]", 2)
        notes = []
        report = self._quiet_sweep([row(703, rollup=RED)], lambda *a, **k: {},
                                   notifier=lambda text, **kw: notes.append(text) or {"ok": True})
        self.assertEqual([n["pr"] for n in report["notified"]], [703])
        self.assertEqual(len(notes), 1)

    def test_daylight_sweeps_normally(self):
        report = self.sweep_for_real([row(414)])
        self.assertEqual([a["pr"] for a in report["asked"]], [414])

    def test_an_undeterminable_hour_sweeps_rather_than_swallowing(self):
        with mock.patch.object(mg, "in_quiet_hours", side_effect=RuntimeError("no tz db")):
            report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir,
                                    lister=lister({REPO: []}), asker=lambda *a, **k: {}, now=NOON)
        self.assertFalse(report["quiet_hours"])
        self.assertTrue(any("quiet-hours check failed" in e for e in report["errors"]))


# --------------------------------------------------------------------- CI RED, never a picker

class RedNotifyTest(SweepCase):
    """A PR whose CI goes red must reach the owner as a notice — green-only candidacy used to mean
    a red PR produced nothing at all. `pr_red_notify.py` is the fix; these pin its WIRING into
    `sweep` — the ledger/text/send unit behaviour is `test_pr_red_notify.py`'s.

    Every test here uses a stub `asker` that never sends a real picker, because the point of this
    class is the red arm alone; `HandsGreenPrsToTheGuardTest` already covers the green arm."""

    def notifying_sweep(self, rows, notifier=None, now=NOON, **kw):
        return pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, lister=lister({REPO: rows}),
                              asker=lambda *a, **k: {"ok": True, "sent": False, "reason": "n/a"},
                              notifier=notifier, now=now, **kw)

    def test_a_red_pr_notifies_once_and_not_twice_at_the_same_head(self):
        sent = []
        notifier = lambda text, **kw: (sent.append(text) or {"ok": True, "sent": True})
        report = self.notifying_sweep([row(703, RED)], notifier=notifier)
        self.assertEqual([n["pr"] for n in report["notified"]], [703])
        self.assertEqual(len(sent), 1)
        self.assertIn("703", sent[0])
        self.assertIn("CI RED", sent[0])

        # Same PR, same head, a second pass: no second buzz, and it says why.
        report2 = self.notifying_sweep([row(703, RED)], notifier=notifier)
        self.assertEqual(report2["notified"], [])
        self.assertEqual(len(sent), 1)
        self.assertEqual([s["pr"] for s in report2["red_skipped"]], [703])
        self.assertIn("already notified", report2["red_skipped"][0]["reason"])

    def test_a_moved_head_re_notifies(self):
        """A fixup, a rebase, a force-push — the failure that made it red the first time may not be
        the one the new commit carries, so a moved head is a new question, exactly as the ask
        ledger's binding already works."""
        sent = []
        notifier = lambda text, **kw: (sent.append(text) or {"ok": True, "sent": True})
        self.notifying_sweep([row(703, RED)], notifier=notifier)
        report = self.notifying_sweep([row(703, RED, head=OTHER_HEAD)], notifier=notifier)
        self.assertEqual([n["pr"] for n in report["notified"]], [703])
        self.assertEqual(len(sent), 2)

    def test_a_pending_pr_notifies_nothing(self):
        """Unknown fails closed, `watch_pr`'s rule read the same way `is_green` already reads it:
        pending is neither green nor red."""
        notifier = lambda text, **kw: self.fail("notified about a merely-pending PR")
        report = self.notifying_sweep([row(703, PENDING)], notifier=notifier)
        self.assertEqual(report["notified"], [])
        self.assertEqual(report["red_looked_at"], 0)

    def test_a_green_pr_still_asks_and_does_not_notify(self):
        notifier = lambda text, **kw: self.fail("notified about a green PR")
        report = self.notifying_sweep([row(703)], notifier=notifier)
        self.assertEqual(report["notified"], [])
        self.assertEqual(report["red_looked_at"], 0)
        # The green arm is untouched: `candidates()` still sees it and hands it to the (stubbed) asker.
        self.assertEqual(report["looked_at"], 1)

    def test_quiet_hours_defers_the_red_notice(self):
        patch = mock.patch.object(mg, "ASK_ON_GREEN_QUIET_HOURS", True)
        patch.start()
        self.addCleanup(patch.stop)
        notifier = lambda text, **kw: self.fail("notified inside the quiet window")
        report = self.notifying_sweep([row(703, RED)], notifier=notifier, now=QUIET_NIGHT)
        self.assertTrue(report["quiet_hours"])
        self.assertEqual(report["notified"], [])
        self.assertEqual([d["pr"] for d in report["red_deferred"]], [703])
        self.assertIn("quiet window", report["red_deferred"][0]["reason"])
        # And nothing was recorded, so the first pass after the window lifts notifies normally.
        self.assertFalse(pr_red_notify.already_notified(self.dir, 703, HEAD, repo=REPO))

    def test_a_failing_send_does_not_raise_and_does_not_mark_notified(self):
        """Marking-then-failing is how a notification gets permanently lost — the same asymmetry
        `merge_guard.record_ask` uses. A failed send costs this pass's notice and nothing else."""
        report = self.notifying_sweep([row(703, RED)],
                                      notifier=lambda text, **kw: {"ok": False, "error": "telegram down"})
        self.assertEqual(report["notified"], [])
        self.assertTrue(any("703" in e and "telegram down" in e for e in report["errors"]))
        self.assertFalse(pr_red_notify.already_notified(self.dir, 703, HEAD, repo=REPO))

        # The next pass retries rather than treating the PR as handled.
        sent = []
        report2 = self.notifying_sweep(
            [row(703, RED)],
            notifier=lambda text, **kw: (sent.append(text) or {"ok": True, "sent": True}))
        self.assertEqual([n["pr"] for n in report2["notified"]], [703])
        self.assertEqual(len(sent), 1)

    def test_a_raising_notifier_does_not_take_the_pass_down(self):
        def exploding(text, **kw):
            raise RuntimeError("boom")
        report = self.notifying_sweep([row(703, RED)], notifier=exploding)
        self.assertEqual(report["notified"], [])
        self.assertTrue(any("raised" in e for e in report["errors"]))
        self.assertFalse(pr_red_notify.already_notified(self.dir, 703, HEAD, repo=REPO))

    def test_one_red_notice_per_pass_is_the_shipped_default(self):
        self.assertEqual(pr_sweep.MAX_RED_NOTIFIES_PER_PASS, 1)

    def test_a_burst_of_red_prs_is_staggered_named_and_not_dropped(self):
        sent = []
        notifier = lambda text, **kw: (sent.append(text) or {"ok": True, "sent": True})
        rows = [row(n, RED) for n in (701, 702, 703)]
        report = self.notifying_sweep(rows, notifier=notifier)
        self.assertEqual(len(sent), 1)
        self.assertEqual([n["pr"] for n in report["notified"]], [701])
        self.assertEqual([d["pr"] for d in report["red_deferred"]], [702, 703])
        self.assertIn("701", pr_sweep.summary_line(report))

    def test_a_dry_run_reports_but_sends_and_records_nothing(self):
        report = self.notifying_sweep([row(703, RED)], dry_run=True,
                                      notifier=lambda text, **kw: self.fail("dry run sent for real"))
        self.assertEqual([n["pr"] for n in report["notified"]], [703])
        self.assertTrue(report["notified"][0]["dry_run"])
        self.assertFalse(pr_red_notify.already_notified(self.dir, 703, HEAD, repo=REPO))


# --------------------------------------------------------------------- one instant, whole pass

class ClockIsPinnedForTheWholePassTest(SweepCase):
    """**A test that passes only between 07:00 and 01:00 is the bug, not the symptom.**

    `test_daylight_sweeps_normally` above is exactly that if the pass's instant is not threaded
    through: it injects `now=NOON` into `sweep`, `sweep` clears its own check on that instant — and
    if it then handed the candidate to `ask_on_green` **without it**, the guard would read the
    runner's real clock and defer inside the curfew. Between 01:00 and 07:00 on the runner's clock
    the expected id list would come back empty and CI would be red for six hours a day.

    So the property is not "the daylight case sends". It is **the pass's answer depends on the
    instant it was given and on nothing else** — and the only way to assert that is to make the real
    clock disagree with the pinned one, which `wall_clock` above does.

    Every test here re-runs the whole ring on a FRESH ledger per instant. Reusing one would make the
    loop prove nothing after its first iteration: the second pass at the same head SHA is correctly
    skipped as already-asked, which looks identical to a pass that declined for the hour."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(mg, "ASK_ON_GREEN_QUIET_HOURS", True)
        patch.start()
        self.addCleanup(patch.stop)

    def _fresh_ledger(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.sends = []

    def test_an_injected_daylight_instant_sends_at_every_hour_of_the_runners_day(self):
        """The regression, stated as the property it should always have had: `sweep_for_real`'s
        noon pass asks about #414 whatever hour the machine running it thinks it is — all 24."""
        for wall in HOURLY_RING:
            with self.subTest(wall_clock=wall.isoformat()):
                self._fresh_ledger()
                with wall_clock(wall):
                    report = self.sweep_for_real([row(414)])
                self.assertEqual([a["pr"] for a in report["asked"]], [414])
                self.assertEqual(len(self.sends), 1)

    def test_an_injected_curfew_instant_defers_at_every_hour_of_the_runners_day(self):
        """The other direction, which is what stops the fix from being 'ignore the clock'. A pass
        pinned to 03:00 defers even when the machine is at high noon — the injected instant is
        authoritative both ways, or it is merely a hint."""
        for wall in HOURLY_RING:
            with self.subTest(wall_clock=wall.isoformat()):
                self._fresh_ledger()
                with wall_clock(wall):
                    report = self.sweep_for_real([row(414)], now=QUIET_NIGHT)
                self.assertTrue(report["quiet_hours"])
                self.assertEqual(report["asked"], [])
                self.assertEqual(self.sends, [])

    def test_the_daemons_own_call_shape_agrees_with_itself_at_every_reading(self):
        """**The production half.** `presence.pr_watch_task` calls `sweep(state_dir=…)` and injects
        no instant, so without the pinning the pass's check and the asker would each read the clock —
        two readings, microseconds apart, which straddle 01:00:00 exactly once a night. Here the
        pass has no injected instant at all and the ring drives the real clock instead: inside the
        window nothing is asked, outside it the picker goes out, and at both boundaries the two
        layers give the same answer because they are reading the same pinned instant."""
        for label, wall, inside in CURFEW_RING:
            with self.subTest(owner_clock=label):
                self._fresh_ledger()
                with wall_clock(wall):
                    report = self.sweep_for_real([row(414)], now=None)
                if inside:
                    self.assertTrue(report["quiet_hours"], f"{label} should be inside the curfew")
                    self.assertEqual(self.sends, [])
                else:
                    self.assertFalse(report["quiet_hours"], f"{label} should be outside the curfew")
                    self.assertEqual([a["pr"] for a in report["asked"]], [414])
                    self.assertEqual(len(self.sends), 1)

    def test_the_asker_and_the_retirer_are_handed_the_instant_the_pass_pinned(self):
        """The threading itself, asserted rather than inferred from an outcome. Both seams receive
        the SAME instant the short-circuit used — not `None`, which is how each of them ended up
        reading a clock of its own."""
        seen = {}
        wall = datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)  # noon local: the pass proceeds

        def spy_asker(pr, **kw):
            seen["asker"] = kw.get("now")
            return {"ok": True, "sent": True, "reason": "spy"}

        def spy_retirer(**kw):
            seen["retirer"] = kw.get("now")
            return {"retired": [], "errors": []}

        with wall_clock(wall):
            report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=None,
                                    lister=lister({REPO: [row(414)]}),
                                    asker=spy_asker, retirer=spy_retirer)
        self.assertEqual([a["pr"] for a in report["asked"]], [414])
        self.assertEqual(seen.get("asker"), wall)
        self.assertEqual(seen.get("retirer"), wall)

    def test_a_pass_reads_the_clock_once_not_once_per_decision(self):
        """The pinning is what makes the two layers agree, so a pass that re-reads is the defect
        rebuilt however consistent its answers happened to be on the day."""
        reads = []

        class _Counting(datetime):
            @classmethod
            def now(cls, tz=None):
                reads.append(tz)
                return NOON.astimezone(tz) if tz is not None else NOON.replace(tzinfo=None)

        with mock.patch.object(pr_sweep, "datetime", _Counting), \
                mock.patch.object(mg, "datetime", _Counting), \
                mock.patch.object(picker_retire, "datetime", _Counting):
            self.sweep_for_real([row(414)], now=None)
        self.assertEqual(len(reads), 1, f"the pass read the wall clock {len(reads)} times")


# --------------------------------------------------------------------- two askers, one question

class OneAskPerHeadShaAcrossBothAskersTest(SweepCase):
    """`watch_pr.py` may still be hand-started on a PR the daemon is also sweeping. The
    `(repo, pr, head_sha)` key is what makes that a no-op — asserted in both orders, because a
    property that only holds one way round is not the property."""

    def _watch_pr_ask(self):
        """Drive `watch_pr`'s green hook — the OTHER caller — with the same stubs."""
        with mock.patch.object(mg, "_run", gh_view()), \
                mock.patch.object(mg, "_send_question", sender(self.sends)):
            return watch_pr.ask_on_green("414", repo=REPO, state_dir=self.dir, out=lambda *_: None)

    def test_the_sweep_goes_first_and_the_hand_started_watcher_stays_quiet(self):
        self.sweep_for_real([row(414)])
        self.assertEqual(len(self.sends), 1)
        res = self._watch_pr_ask()
        self.assertFalse(res.get("sent"))
        self.assertIn("already asked", res.get("reason", ""))
        self.assertEqual(len(self.sends), 1)

    def test_the_hand_started_watcher_goes_first_and_the_sweep_stays_quiet(self):
        self._watch_pr_ask()
        self.assertEqual(len(self.sends), 1)
        report = self.sweep_for_real([row(414)])
        self.assertEqual(report["asked"], [])
        self.assertEqual(len(self.sends), 1)

    def test_exactly_one_ask_row_exists_for_the_commit(self):
        self.sweep_for_real([row(414)])
        self._watch_pr_ask()
        rows = [a for a in mg.read_asks(self.dir) if a.get("pr") == 414]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["head_sha"], HEAD)

    def test_there_is_exactly_one_asker_implementation_and_both_doors_call_it(self):
        """The residual race is two processes passing `already_asked` in the same instant, which no
        lock-free ledger can close and which this module does not pretend to. What IS closed is
        divergence: both doors go through one function, so they can never disagree about the key.
        The ledger is append-only, so a real race leaves TWO rows and is visible rather than
        clobbered into silence."""
        self.assertIn("mg.ask_on_green", source("pr_sweep.py"))
        self.assertIn("merge_guard", source("watch_pr.py"))
        self.assertEqual(mg.ASK_LOG_FILE, "merge-ask-log.jsonl")


# --------------------------------------------------------------------- fail-open

class FailOpenTest(SweepCase):
    def _quiet(self, **kw):
        return pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON,
                              asker=lambda *a, **k: self.fail("asked despite a failed look"), **kw)

    def test_gh_missing_costs_the_pass_and_nothing_else(self):
        report = self._quiet(lister=lister({}, raises=FileNotFoundError()))
        self.assertIn("not on PATH", report["errors"][0])

    def test_a_timeout_costs_the_pass(self):
        report = self._quiet(lister=lister({}, raises=subprocess.TimeoutExpired("gh", 30)))
        self.assertIn("timed out", report["errors"][0])

    def test_rate_limiting_costs_the_pass(self):
        report = self._quiet(lister=lister({}, code=1, stderr="API rate limit exceeded"))
        self.assertIn("rate limit", report["errors"][0])

    def test_a_malformed_response_costs_the_pass(self):
        report = self._quiet(lister=lister({}, stdout="<html>502</html>"))
        self.assertIn("unreadable", report["errors"][0])

    def test_a_json_object_where_a_list_belongs_costs_the_pass(self):
        report = self._quiet(lister=lister({}, stdout='{"message":"Not Found"}'))
        self.assertIn("not a list", report["errors"][0])

    def test_an_arbitrary_transport_explosion_costs_the_pass(self):
        report = self._quiet(lister=lister({}, raises=OSError("winsock 10054")))
        self.assertIn("failed", report["errors"][0])

    def test_one_broken_repo_does_not_stop_the_other(self):
        def half_broken(argv):
            if argv[argv.index("--repo") + 1] == REPO:
                raise OSError("boom")
            return 0, json.dumps([row(45, repo=OTHER_REPO)]), ""

        with mock.patch.object(mg, "_run", gh_view()), \
                mock.patch.object(mg, "_send_question", sender(self.sends)):
            report = pr_sweep.sweep(repos=(REPO, OTHER_REPO), state_dir=self.dir,
                                    lister=half_broken, now=NOON)
        self.assertEqual([a["pr"] for a in report["asked"]], [45])
        self.assertEqual(len(report["errors"]), 1)

    def test_an_asker_that_raises_is_caught_even_though_it_promises_not_to(self):
        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON,
                                lister=lister({REPO: [row(414)]}),
                                asker=mock.Mock(side_effect=RuntimeError("kaboom")))
        self.assertIn("kaboom", report["errors"][0])

    def test_an_asker_returning_nonsense_is_reported_rather_than_crashing(self):
        report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON,
                                lister=lister({REPO: [row(414)]}), asker=lambda *a, **k: "nope")
        self.assertIn("returned str", report["errors"][0])

    def test_an_unreadable_pr_is_reported_and_never_raises(self):
        with mock.patch.object(mg, "_run", lambda argv, cwd=None: (1, "", "no such PR")):
            report = pr_sweep.sweep(repos=(REPO,), state_dir=self.dir, now=NOON,
                                    lister=lister({REPO: [row(414)]}))
        self.assertEqual(report["asked"], [])
        self.assertTrue(report["errors"])

    def test_sweep_never_raises_on_any_of_these(self):
        """The contract a supervised daemon task depends on. `None` resolves the configured watched
        set, pinned here so the test reads neither the live config nor this checkout's `origin`."""
        with mock.patch.object(repo_config, "watched_repos", return_value=(REPO,)):
            for bad in (None, (), ["not/a/repo"]):
                self.assertIsInstance(
                    pr_sweep.sweep(repos=bad, state_dir=self.dir, now=NOON,
                                   lister=lister({}, raises=OSError("x")), asker=lambda *a, **k: {}),
                    dict)


# --------------------------------------------------------------------- the daemon task

def _args(state_dir, **over):
    base = dict(state_dir=state_dir, stub_brain=False, stub_send=False, fake_inbox=None,
                no_pr_watch=False, telegram_env="unused.env")
    base.update(over)
    return argparse.Namespace(**base)


@unittest.skipUnless(HAS_PR_WATCH_TASK, PR_WATCH_SKIP)
class DaemonTaskTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.presence = presence
        # The same task runs `pr_repair.sweep` after this one; left real, it would read GitHub and
        # could rebase a live PR on the strength of a temp dir with no approvals in it.
        repair = mock.patch.object(presence.pr_repair, "sweep", return_value={})
        repair.start()
        self.addCleanup(repair.stop)

    async def _run_task(self, args, sweep_impl):
        state = self.presence.DaemonState()
        calls = []
        logs = []

        def stub(**kw):
            calls.append(kw)
            state.stop.set()  # one pass, then wind down — no test spawns a real daemon
            return sweep_impl(**kw)

        with mock.patch.object(self.presence.pr_sweep, "sweep", stub):
            await asyncio.wait_for(
                self.presence.pr_watch_task(state, args, logs.append), timeout=10.0)
        return calls, logs

    async def test_it_sweeps_with_the_daemons_own_state_dir(self):
        """The ledgers live beside whichever copy is running — an approval already given is
        invisible from the wrong `state/`."""
        calls, _ = await self._run_task(_args(self.dir), lambda **kw: {"asked": [], "errors": []})
        self.assertEqual(calls, [{"state_dir": self.dir}])

    async def test_a_stub_send_run_never_sweeps(self):
        """**The gating difference that matters.** The sibling tasks never send; a sweep's whole
        job is to reach the owner's phone, and `telegram_ask.py` is a subprocess that has never heard
        of `--stub-send`. A stub run with a live telegram.env beside it would message them for real."""
        calls, _ = await self._run_task(_args(self.dir, stub_send=True), lambda **kw: {})
        self.assertEqual(calls, [])

    async def test_a_stub_brain_run_never_sweeps(self):
        calls, _ = await self._run_task(_args(self.dir, stub_brain=True), lambda **kw: {})
        self.assertEqual(calls, [])

    async def test_a_fake_inbox_run_never_sweeps(self):
        calls, _ = await self._run_task(_args(self.dir, fake_inbox="msgs.json"), lambda **kw: {})
        self.assertEqual(calls, [])

    async def test_no_pr_watch_opts_out(self):
        calls, _ = await self._run_task(_args(self.dir, no_pr_watch=True), lambda **kw: {})
        self.assertEqual(calls, [])

    async def test_a_sweep_that_explodes_does_not_take_the_daemon_down(self):
        """It is one of the daemon's supervised tasks: `_supervise` turns an escape into
        `state.crashed` and a dead daemon, so nothing may escape."""
        calls, logs = await self._run_task(
            _args(self.dir), mock.Mock(side_effect=RuntimeError("github is on fire")))
        self.assertTrue(any("pr-watch sweep error" in line for line in logs))

    async def test_a_repeated_error_is_logged_once_not_every_pass(self):
        """An unauthenticated `gh` would otherwise write a line every pass forever, and a
        log line that repeats forever is a log line that gets ignored."""
        state = self.presence.DaemonState()
        logs, passes = [], []

        def stub(**kw):
            passes.append(1)
            if len(passes) >= 3:
                state.stop.set()
            return {"asked": [], "deferred": [], "errors": ["gh: not logged in"]}

        with mock.patch.object(self.presence.pr_sweep, "sweep", stub), \
                mock.patch.object(self.presence, "PR_WATCH_INTERVAL", 0.01):
            await asyncio.wait_for(
                self.presence.pr_watch_task(state, _args(self.dir), logs.append), timeout=10.0)
        self.assertEqual(len([line for line in logs if "not logged in" in line]), 1)

    async def test_a_capped_pass_says_what_it_held_over(self):
        _calls, logs = await self._run_task(_args(self.dir), lambda **kw: {
            "asked": [{"repo": REPO, "pr": 410, "reason": "asked"}],
            "deferred": [{"repo": REPO, "pr": 411, "reason": "capped"}],
            "errors": [], "looked_at": 2})
        self.assertTrue(any("held for the next pass" in line for line in logs))

    async def test_a_red_notification_alone_still_logs(self):
        """A log trigger that reads only `asked`/`deferred`/`retired` would print NOTHING for this
        dict, so a pass whose only news was a red-CI notice would vanish from `presence.log`."""
        _calls, logs = await self._run_task(_args(self.dir), lambda **kw: {
            "asked": [], "deferred": [], "retired": [], "errors": [],
            "notified": [{"repo": REPO, "pr": 703}], "red_deferred": []})
        self.assertTrue(any("told the owner about" in line for line in logs))

    async def test_a_deferred_red_notification_alone_still_logs(self):
        _calls, logs = await self._run_task(_args(self.dir), lambda **kw: {
            "asked": [], "deferred": [], "retired": [], "errors": [], "notified": [],
            "red_deferred": [{"repo": REPO, "pr": 703, "reason": "capped"}]})
        self.assertTrue(any("held for the next pass" in line for line in logs))

    def test_the_task_is_registered_in_the_supervisor(self):
        body = source("presence.py")
        gather = body[body.index("await asyncio.gather("):]
        gather = gather[:gather.index(")\n")]
        self.assertIn('_supervise("pr-watch", pr_watch_task(state, args, log)', gather)
        # Registered exactly once: a second supervised copy would double every pass's asks.
        self.assertEqual(gather.count('_supervise("pr-watch"'), 1)

    def test_the_interval_is_in_the_external_check_neighbourhood(self):
        """Its neighbours poll localhost every 20 s; this one calls the GitHub API, so the right
        neighbour is `seneschald-update`'s ~10 min fetch. Slower than a health check, faster than a
        deploy cycle."""
        self.assertGreaterEqual(self.presence.PR_WATCH_INTERVAL, 60)
        self.assertLessEqual(self.presence.PR_WATCH_INTERVAL, 600)


if __name__ == "__main__":
    unittest.main()
