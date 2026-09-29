#!/usr/bin/env python3
"""Tests for ``picker_mark`` — the pass that puts a merge picker's state ON the picker as a reaction
— and for the ``telegram_ask.mark`` half that makes the call and stamps the record.

``../docs/picker-state-marking-spec.md`` carries the argument; the classes here defend the
properties it rests on, and must not be relaxed into passing:

* **``DeathClassTest``** pins the death classes, each separately. The first three ways a picker
  dies — its PR merged or closed, its head moved, it cannot merge as it stands — are different
  facts, and a build that only catches one of them still lets a tap be spent on the others. A
  **fourth** applies where the base requires up-to-date branches: a BEHIND head reports
  ``mergeable: MERGEABLE`` — no textual conflict — so it has to be its own test rather than an
  assumed subset of the third.
* **``FailSoftTest``** pins the polarity, and it is the class most likely to be quietly weakened. A
  marker is a convenience and the sweep's job is asking, so **every** failure — an unswept repo, a
  truncated list, ``REACTION_INVALID``, a deleted message, a transport error, missing credentials, an
  unreadable store, an absent ``git`` — costs the mark and nothing else. ``sweep`` never raises.
* **``SpendableNowTest``** pins the AMENDED rule — 🔥 means spendable now, no uniqueness: every
  alive-and-green picker carries 🔥, with no winner-take-all tie-break. It replaces a single-winner
  design that broke as soon as two genuinely spendable, disjoint pickers were green at once. Full
  argument on both sides: ``../docs/picker-state-marking-spec.md`` §6.1.
* **``IdempotenceTest``** is what makes re-marking free. A second pass over an unchanged world makes
  **zero** API calls, through two independent skips.
* **``LocalMergeCheckTest``** guards the trap that would produce a false 😴: ``git merge-tree`` exits
  **1** for a missing object exactly as it does for a conflict. It also pins the narrowing direction
  — the local arm may only ever ADD deadness, never clear GitHub's verdict.
* **``OutOfScopeTest``** is ``test_picker_retire``'s, one module over and for the same reason: a
  **held outbound approval** of another kind and an **unattributed picker** must be untouchable by
  anything, and
  eligibility here is ``picker_retire.pending_pr_pickers`` rather than a second rule that can drift.
* **``NeverSendsTest``** and **``NeverApprovesTest``** are absences. This module may not grow a send,
  a merge, or a route to ``record_approval`` — and the second is asserted against the **BYTECODE**
  rather than the source text, because the module docstring names ``record_approval`` in the sentence
  promising never to call it and a text scan cannot tell a refusal from a call site.

**No test here reaches the network, calls ``git`` or ``gh``, or sends anything.** Every subprocess
goes through the ``runner=`` seam, every Bot API call through ``api=``, every credential through
``config=``, and every store lives in a ``tempfile`` directory — nothing reads or writes the live
``seneschal/state/``. The owner's zone is pinned to a fixed UTC-5 for the whole module
(``setUpModule``), so the one quiet-window test reads the same wall clock on every runner.

Run:  python -m unittest seneschal.scripts.test_picker_mark   (or)   python test_picker_mark.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import merge_guard as mg  # noqa: E402
import picker_mark as pm  # noqa: E402
import pr_sweep as ps  # noqa: E402
import telegram_ask as ta  # noqa: E402
import tz_common  # noqa: E402 — the owner-zone seam the quiet window reads
from telegram_send import TelegramAPIError  # noqa: E402

REPO = "example/repo"
OTHER_REPO = "example/other"
HEAD = "1094cb9e1419e828e535a7fb3da896c7580964f3"     # #573's head in these fixtures
MOVED = "dc1e9f29a0baab12f144bec0490e8c06d84247b0"    # #584's head in these fixtures

#: The owner's zone for the whole module: a FIXED UTC-5 offset, so the quiet-window test reads the
#: same wall clock on every runner whatever its own timezone or identity config.
OWNER_ZONE = timezone(timedelta(hours=-5), "UTC-5")
_ZONE_PATCH = mock.patch.object(tz_common, "_zone", return_value=OWNER_ZONE)


def setUpModule():
    _ZONE_PATCH.start()


def tearDownModule():
    _ZONE_PATCH.stop()

CHAT = "123456"
CONFIG = {"token": "test-token", "chat_id": CHAT, "api_base": "https://example.invalid",
          "parse_mode": "", "format": "plain"}
NOW = datetime(2026, 9, 1, 17, 0, tzinfo=timezone.utc)

#: A terminally-green rollup in the shape `gh pr list --json statusCheckRollup` returns. `is_green`
#: is `watch_pr.classify` and is NOT re-derived here — an empty list is *pending*, never green.
GREEN = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS", "name": "ci"}]


def question(pr=573, repo=REPO, head=HEAD, message_id=10470, asked_at="2026-09-01T15:00:00Z",
             kind=mg.APPROVAL_META_KIND, meta="auto", reaction=None, answered=None):
    """One store record in the shape `telegram_ask.ask` actually writes."""
    if meta == "auto":
        meta = {"kind": kind, "pr": pr, "repo": repo, "head_sha": head, "approve_index": 0}
    rec = {"question": f"Merge {repo} PR #{pr} — a pull request?",
           "body": f"Merge {repo} PR #{pr}?\n\nTap one.\n\n1. Approve",
           "options": [{"label": "Approve", "description": "it merges"},
                       {"label": "Not now", "description": "nothing merges"}],
           "multi": False, "recommended": False, "chat_id": CHAT, "message_id": message_id,
           "asked_at": asked_at, "selected": [], "answered_at": answered, "meta": meta}
    if reaction is not None:
        rec["reaction"] = reaction
    return rec


def row(pr=573, head=HEAD, mergeable="MERGEABLE", green=True, base="develop", draft=False,
        merge_state_status=None):
    """One `gh pr list` row, keyed as `gh` spells it. `merge_state_status` defaults to unset, the
    same fail-open convention `mergeable` already follows here."""
    r = {"number": pr, "headRefOid": head, "mergeable": mergeable, "isDraft": draft,
         "baseRefName": base, "title": "a pull request",
         "url": f"https://github.com/{REPO}/pull/{pr}",
         "statusCheckRollup": GREEN if green else []}
    if merge_state_status is not None:
        r["mergeStateStatus"] = merge_state_status
    return r


def index(*rows, repo=REPO, truncated=False):
    """A `row_index` built through `pr_sweep.open_rows`, never by hand — so a change to that
    function's `None` contract breaks these tests instead of silently passing them."""
    return {mg.repo_key(repo): ps.open_rows(list(rows), truncated=truncated)}


class Recorder:
    """A Bot API seam that records every call and can be told to raise. Nothing here has a socket."""

    def __init__(self, raises=None):
        self.calls, self.raises = [], raises

    def __call__(self, c, method, params, timeout=30):
        self.calls.append((method, params))
        if self.raises is not None:
            raise self.raises
        return {"ok": True, "result": {"message_id": params.get("message_id") or 1}}

    def methods(self):
        return [m for m, _ in self.calls]

    def emoji(self):
        """The glyph of every reaction that went out, in order."""
        out = []
        for method, params in self.calls:
            if method != ta.REACTION_METHOD:
                continue
            payload = json.loads(params["reaction"])
            out.append(payload[0]["emoji"] if payload else None)
        return out


def read_source(name: str) -> str:
    """A sibling module's source, closed properly — an unclosed handle here is a `ResourceWarning`
    in every run of the suite."""
    with io.open(os.path.join(SCRIPT_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def source_names(name: str) -> set:
    """Every name the module's **compiled code** touches, at every nesting depth.

    Docstrings live in `co_consts` as plain strings and never in `co_names`, so this can tell a
    module that *calls* something from one that merely *promises not to* — which a text scan cannot,
    and which matters because the refusals in this tree are written down beside the code that makes
    them."""
    import types
    code = compile(read_source(name), name, "exec")
    out, stack = set(), [code]
    while stack:
        c = stack.pop()
        out.update(c.co_names)
        stack.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
    return out


def no_git(argv, cwd=None):
    """A `git` seam that answers *nothing here is a repository*, so the local arm is off. The default
    for every test that is not about the local arm — otherwise a test would reach the real
    checkout, whose answers change with every commit."""
    return 1, "", "fatal: not a git repository"


class StoreCase(unittest.TestCase):
    """A temp state dir with a question store in it. The live `seneschal/state/` is never touched."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="picker-mark-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, **questions):
        ta.save_store(ta.store_path(self.dir),
                      {"schema": ta.SCHEMA, "questions": dict(questions), "expired": []})

    def read(self) -> dict:
        return ta.load_store(ta.store_path(self.dir))

    def record(self, qid) -> dict:
        return self.read()["questions"][qid]

    def sweep(self, row_index, api=None, runner=no_git, **kw):
        api = Recorder() if api is None else api
        report = pm.sweep(state_dir=self.dir, row_index=row_index, api=api, runner=runner,
                          config=CONFIG, now=NOW, **kw)
        return report, api

    def states(self, report) -> dict:
        """`{pr: state}` over everything the pass decided, marked or already correct."""
        return {m["pr"]: m["state"] for m in report["marked"]}


# --------------------------------------------------------------------------- the three death classes

class DeathClassTest(StoreCase):
    """**The death classes are not one class.** A merged PR, a moved head, and an open-but-
    CONFLICTING PR each make a picker unspendable for a different reason. Each one is a separate
    test because a build that catches two of the three still costs the owner a tap."""

    def test_a_merged_pull_request_puts_its_picker_to_bed(self):
        # The picker is live, the PR is gone from a list read WHOLE.
        self.write(q573=question(pr=573))
        report, api = self.sweep(index(row(pr=999)))
        self.assertEqual(self.states(report), {573: pm.STATE_ASLEEP})
        self.assertEqual(api.emoji(), [pm.MARK_ASLEEP])
        self.assertIn("no longer open", report["marked"][0]["reason"])

    def test_a_moved_head_puts_its_picker_to_bed(self):
        # An approval binds to ONE commit, so a rebase kills the question pinned to the old one.
        self.write(q573=question(pr=573, head=HEAD))
        report, api = self.sweep(index(row(pr=573, head=MOVED)))
        self.assertEqual(self.states(report), {573: pm.STATE_ASLEEP})
        self.assertIn("has moved past", report["marked"][0]["reason"])

    def test_a_conflicting_pull_request_puts_its_picker_to_bed(self):
        # The class NO ask-time check reaches: the picker went out while the PR was clean and the
        # PR went dirty afterwards.
        self.write(q584=question(pr=584, head=MOVED))
        report, api = self.sweep(index(row(pr=584, head=MOVED, mergeable="CONFLICTING")))
        self.assertEqual(self.states(report), {584: pm.STATE_ASLEEP})
        self.assertEqual(api.emoji(), [pm.MARK_ASLEEP])

    def test_unknown_mergeability_does_not_put_a_picker_to_bed(self):
        """`UNKNOWN` is GitHub's lazy-computation state, not a verdict — it is the ABSENCE of an
        answer. `pr_sweep.CANNOT_MERGE_STATES` is an allow-list of refusals for exactly this reason,
        and reading it as *not MERGEABLE* here would put a picker to bed on a value that is transient
        by construction (the query itself schedules the computation)."""
        self.write(q577=question(pr=577))
        report, _ = self.sweep(index(row(pr=577, mergeable="UNKNOWN")))
        self.assertEqual(self.states(report), {577: pm.STATE_NEXT})

    def test_a_state_github_invents_later_does_not_put_a_picker_to_bed(self):
        self.write(q577=question(pr=577))
        report, _ = self.sweep(index(row(pr=577, mergeable="BEHIND_BY_SOME_NEW_THING")))
        self.assertEqual(self.states(report), {577: pm.STATE_NEXT})

    def test_a_red_pull_request_is_queued_and_never_put_to_bed(self):
        """Not green is *not yet*, never *dead*. Red CI is fixed by a commit, which moves the head,
        which is the moved-head class — so marking it asleep here would be a second, weaker opinion
        about the same fact, arriving earlier and less reliably."""
        self.write(q577=question(pr=577))
        report, api = self.sweep(index(row(pr=577, green=False)))
        self.assertEqual(self.states(report), {577: pm.STATE_QUEUED})
        self.assertEqual(api.emoji(), [pm.MARK_QUEUED])

    def test_a_behind_pull_request_puts_its_picker_to_bed(self):
        """**The fourth class.** Where the base requires a branch be up to date with it, a BEHIND
        head reports `mergeable: MERGEABLE` — no textual conflict — so this must be checked as its
        own fact rather than folded into test 3."""
        self.write(q573=question(pr=573))
        report, api = self.sweep(index(row(pr=573, mergeable="MERGEABLE",
                                           merge_state_status="BEHIND")))
        self.assertEqual(self.states(report), {573: pm.STATE_ASLEEP})
        self.assertEqual(api.emoji(), [pm.MARK_ASLEEP])
        self.assertIn("BEHIND", report["marked"][0]["reason"])

    def test_unknown_merge_state_status_does_not_put_a_picker_to_bed(self):
        """The same `UNKNOWN`-is-not-a-verdict argument, one field over: `pr_sweep.BEHIND_STATES` is
        an allow-list, so anything other than a positively-known `BEHIND` leaves the picker alive."""
        self.write(q577=question(pr=577))
        report, _ = self.sweep(index(row(pr=577, merge_state_status="UNKNOWN")))
        self.assertEqual(self.states(report), {577: pm.STATE_NEXT})


# --------------------------------------------------------------------------- fail soft

class FailSoftTest(StoreCase):
    """**A marker is a convenience; the sweep's job is asking.** Every failure costs the mark and
    nothing else, and `sweep` never raises. This is the class an edit will want to weaken by letting
    one path propagate 'so the problem is visible' — the problem is visible in `errors`."""

    def test_an_unswept_repository_leaves_its_pickers_alone(self):
        self.write(q1=question(pr=573, repo=OTHER_REPO))
        report, api = self.sweep(index(row(pr=573)))          # REPO only
        self.assertEqual(report["marked"], [])
        self.assertEqual(api.calls, [])
        self.assertIn("was not swept", report["skipped"][0]["reason"])

    def test_a_truncated_list_leaves_its_pickers_alone(self):
        """Absence from a list that might be truncated means NOTHING — a PR past the page boundary
        would look merged, and the picker would be put to bed on a paging artifact."""
        self.write(q1=question(pr=573))
        report, api = self.sweep(index(row(pr=999), truncated=True))
        self.assertEqual(report["marked"], [])
        self.assertEqual(api.calls, [])
        self.assertIn("could not be read whole", report["skipped"][0]["reason"])

    def test_an_unreadable_repository_leaves_its_pickers_alone(self):
        self.write(q1=question(pr=573))
        report, api = self.sweep({mg.repo_key(REPO): ps.open_rows(None)})
        self.assertEqual(report["marked"], [])
        self.assertEqual(api.calls, [])

    def test_reaction_invalid_costs_the_mark_and_nothing_else(self):
        """The one Bot API refusal this feature can actually provoke: a glyph outside Telegram's
        fixed allow-list. It must not raise, must not stamp, and must not stop the pass."""
        self.write(q1=question(pr=573, message_id=1), q2=question(pr=574, message_id=2,
                                                                 asked_at="2026-09-01T16:00:00Z"))
        api = Recorder(raises=TelegramAPIError("Bad Request: REACTION_INVALID"))
        report, _ = self.sweep(index(row(pr=573), row(pr=574)), api=api)
        self.assertEqual(report["marked"], [])
        self.assertEqual(len(report["errors"]), 2)            # both tried; neither stopped the other
        self.assertNotIn("reaction", self.record("q1"))       # nothing was stamped
        self.assertNotIn("reaction", self.record("q2"))

    def test_a_transport_failure_costs_the_mark_and_is_retried_next_pass(self):
        self.write(q1=question(pr=573))
        api = Recorder(raises=OSError("connection reset"))
        report, _ = self.sweep(index(row(pr=573)), api=api)
        self.assertEqual(report["marked"], [])
        self.assertNotIn("reaction", self.record("q1"))
        # The next pass, with the wire working, marks it — the failure held nothing back.
        report2, api2 = self.sweep(index(row(pr=573)))
        self.assertEqual(api2.emoji(), [pm.MARK_NEXT])

    def test_a_picker_with_no_message_is_reported_not_marked(self):
        """The send died between the record and the response, so there is nothing to react to."""
        self.write(q1=question(pr=573, message_id=None))
        report, api = self.sweep(index(row(pr=573)))
        self.assertEqual(api.calls, [])
        self.assertEqual(report["marked"], [])
        self.assertEqual(report["unchanged"], 1)

    def test_missing_credentials_cost_the_marking_pass_and_nothing_else(self):
        self.write(q1=question(pr=573))
        report = pm.sweep(state_dir=self.dir, row_index=index(row(pr=573)), api=Recorder(),
                          runner=no_git, config=None,
                          env_file=os.path.join(self.dir, "nope.env"), now=NOW)
        self.assertEqual(report["marked"], [])
        self.assertTrue(report["errors"])

    def test_an_unreadable_store_never_raises(self):
        with io.open(ta.store_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{ not json at all")
        report = pm.sweep(state_dir=self.dir, row_index=index(row(pr=573)), api=Recorder(),
                          runner=no_git, config=CONFIG, now=NOW)
        self.assertEqual(report["pending"], 0)                # load_store reads a broken file empty
        self.assertEqual(report["marked"], [])

    def test_a_conflict_check_that_raises_leaves_the_picker_alive(self):
        def boom(repo, base, head):
            raise RuntimeError("git exploded")
        marks, skipped, checks = pm.plan(
            pm.pending({"questions": {"q1": question(pr=573)}}), index(row(pr=573)),
            conflict_check=boom, local_repo_key=mg.repo_key(REPO))
        self.assertEqual([m["state"] for m in marks], [pm.STATE_NEXT])
        self.assertEqual(checks, 1)

    def test_a_marking_failure_does_not_stop_pr_sweep_retiring(self):
        """`pr_sweep` runs marking BEFORE retirement, so a raise there must not cancel the tidying
        that follows it. The two are independent."""
        seen = {}

        def exploding_marker(**kw):
            raise RuntimeError("the marking pass fell over")

        def retirer(**kw):
            seen["retired"] = True
            return {"retired": [], "errors": []}

        report = ps.sweep(repos=(), state_dir=self.dir, marker=exploding_marker, retirer=retirer,
                          now=NOW)
        self.assertTrue(seen.get("retired"))
        self.assertTrue(any("marking pass raised" in e for e in report["errors"]))


# --------------------------------------------------------------------------- spendable now, no uniqueness

class SpendableNowTest(StoreCase):
    """**AMENDED: 🔥 means spendable now, no uniqueness.** Every alive-and-green picker carries 🔥;
    there is no winner-take-all tie-break. This replaces a single-winner design that broke as soon
    as two pickers — both green, both clean, both genuinely spendable and disjoint — were live at
    once. ``../docs/picker-state-marking-spec.md`` §6.1 carries the full reasoning."""

    def test_every_alive_and_green_picker_carries_next(self):
        """No winner-take-all: every spendable picker gets 🔥, not just the oldest."""
        self.write(**{f"q{pr}": question(pr=pr, message_id=pr,
                                         asked_at=f"2026-09-01T1{i}:00:00Z")
                      for i, pr in enumerate([573, 574, 575, 576])})
        report, _ = self.sweep(index(*[row(pr=pr) for pr in (573, 574, 575, 576)]))
        states = list(self.states(report).values())
        self.assertEqual(states.count(pm.STATE_NEXT), 4)
        self.assertEqual(states.count(pm.STATE_QUEUED), 0)

    def test_two_pickers_can_carry_next_at_once_without_being_resolved_down(self):
        """The shape that broke the old design, now the intended behavior rather than a breach: two genuinely
        spendable, disjoint pickers both stay 🔥, and a pass that finds them already correct changes
        nothing."""
        self.write(q573=question(pr=573, message_id=1, asked_at="2026-09-01T15:00:00Z",
                                 reaction=pm.MARK_NEXT),
                   q576=question(pr=576, message_id=4, asked_at="2026-09-01T09:00:00Z",
                                 reaction=pm.MARK_NEXT))
        report, api = self.sweep(index(row(pr=573), row(pr=576)))
        self.assertEqual(report["marked"], [])
        self.assertEqual(api.calls, [])
        after = [self.record(q).get("reaction") for q in ("q573", "q576")]
        self.assertEqual(after.count(pm.MARK_NEXT), 2)

    def test_a_freshly_green_picker_joins_an_existing_holder_rather_than_being_queued(self):
        """Under the old tie-break this newcomer would have been demoted to 👀 behind the current
        holder. Now it gets 🔥 too."""
        self.write(q573=question(pr=573, message_id=1, asked_at="2026-09-01T15:00:00Z",
                                 reaction=pm.MARK_NEXT),
                   q576=question(pr=576, message_id=4, asked_at="2026-09-01T09:00:00Z"))
        report, api = self.sweep(index(row(pr=573), row(pr=576)))
        self.assertEqual(self.states(report), {576: pm.STATE_NEXT})   # 573 was already correct
        self.assertEqual(api.emoji(), [pm.MARK_NEXT])

    def test_a_dead_picker_does_not_affect_its_sibling(self):
        self.write(q573=question(pr=573, message_id=1, asked_at="2026-09-01T09:00:00Z",
                                 reaction=pm.MARK_NEXT),
                   q576=question(pr=576, message_id=4, asked_at="2026-09-01T15:00:00Z"))
        report, _ = self.sweep(index(row(pr=573, mergeable="CONFLICTING"), row(pr=576)))
        self.assertEqual(self.states(report), {573: pm.STATE_ASLEEP, 576: pm.STATE_NEXT})

    def test_no_green_picker_means_no_next_at_all(self):
        """The standing rule is *never merge a PR with red or pending CI*, so 🔥 on a red PR would
        point the owner at a merge they must not make."""
        self.write(q573=question(pr=573, message_id=1), q576=question(pr=576, message_id=4))
        report, _ = self.sweep(index(row(pr=573, green=False), row(pr=576, green=False)))
        self.assertEqual(set(self.states(report).values()), {pm.STATE_QUEUED})

    def test_asked_at_no_longer_affects_state_at_all(self):
        """The old tie-break's whole reason to read `asked_at` is gone — `pick_next`/`_order` are
        removed, so an unreadable or pre-epoch stamp can no longer cost every OTHER picker its
        marker. Both pickers below are alive and green regardless of their (nonsense) `asked_at`."""
        pickers = pm.pending({"questions": {
            "bad": question(pr=573, message_id=1, asked_at="1600-01-01T00:00:00"),
            "good": question(pr=574, message_id=2, asked_at="2026-09-01T16:00:00Z")}})
        marks, skipped, _ = pm.plan(pickers, index(row(pr=573), row(pr=574)))
        self.assertEqual({m["pr"]: m["state"] for m in marks},
                         {573: pm.STATE_NEXT, 574: pm.STATE_NEXT})
        self.assertEqual(skipped, [])

    def test_a_picker_left_alone_is_never_also_marked(self):
        """A picker appears in `skipped` or in `marks`, never in both — otherwise a report could say
        it was left exactly as it is AND that its reaction was changed."""
        self.write(q1=question(pr=573), q2=question(pr=574, repo=OTHER_REPO, message_id=2))
        report, _ = self.sweep(index(row(pr=573)))
        marked = {m["question_id"] for m in report["marked"]}
        skipped = {s["question_id"] for s in report["skipped"]}
        self.assertEqual(marked & skipped, set())

    def test_pick_next_and_order_are_gone(self):
        """The single-winner tie-break isn't merely unused — it's removed, because the rule itself
        (not just an enforcement gap) was the defect."""
        self.assertFalse(hasattr(pm, "pick_next"))
        self.assertFalse(hasattr(pm, "_order"))


# --------------------------------------------------------------------------- idempotence

class IdempotenceTest(StoreCase):
    """**Re-marking has to be free**, because that is what lets 😴 be derived every pass instead of
    latched. Two independent skips: `picker_mark` compares before calling, and `telegram_ask.mark`
    refuses a no-op regardless of caller."""

    def test_a_second_pass_over_an_unchanged_world_calls_nothing(self):
        self.write(q573=question(pr=573, message_id=1), q574=question(pr=574, message_id=2,
                                                                     asked_at="2026-09-01T16:00Z"))
        idx = index(row(pr=573), row(pr=574))
        first, api1 = self.sweep(idx)
        self.assertEqual(len(api1.calls), 2)
        second, api2 = self.sweep(idx)
        self.assertEqual(api2.calls, [])
        self.assertEqual(second["marked"], [])
        self.assertEqual(second["unchanged"], 2)

    def test_the_primitive_refuses_a_no_op_even_when_the_caller_asks(self):
        """The second lock: an in-process caller that goes round `picker_mark`'s comparison still
        cannot hammer the API."""
        self.write(q1=question(pr=573, reaction=pm.MARK_ASLEEP))
        api = Recorder()
        res = ta.mark(CONFIG, self.dir, "q1", pm.MARK_ASLEEP, api=api, now=NOW)
        self.assertFalse(res["marked"])
        self.assertTrue(res["ok"])
        self.assertEqual(api.calls, [])

    def test_a_state_change_does_call(self):
        self.write(q1=question(pr=573, reaction=pm.MARK_QUEUED))
        report, api = self.sweep(index(row(pr=573, mergeable="CONFLICTING")))
        self.assertEqual(api.emoji(), [pm.MARK_ASLEEP])
        self.assertEqual(self.record("q1")["reaction"], pm.MARK_ASLEEP)

    def test_the_wire_call_goes_first_and_the_stamp_second(self):
        """`retire`'s order, for the mirror of its reason: a record claiming a reaction the message
        does not carry is what would stop the next pass re-applying it."""
        self.write(q1=question(pr=573))
        api = Recorder(raises=TelegramAPIError("Bad Request: MESSAGE_NOT_FOUND"))
        ta.mark(CONFIG, self.dir, "q1", pm.MARK_ASLEEP, api=api, now=NOW)
        self.assertEqual(len(api.calls), 1)                   # it tried
        self.assertNotIn("reaction", self.record("q1"))       # and stamped nothing

    def test_an_answered_question_is_never_marked(self):
        """The answer stands; a reaction appearing on a question already settled is noise."""
        self.write(q1=question(pr=573, answered="2026-09-01T16:00:00Z"))
        api = Recorder()
        res = ta.mark(CONFIG, self.dir, "q1", pm.MARK_ASLEEP, api=api, now=NOW)
        self.assertFalse(res["marked"])
        self.assertEqual(api.calls, [])

    def test_a_tap_that_lands_mid_flight_wins(self):
        """The store is re-read after the wire call, exactly as `retire` does. The owner's answer may
        not be clobbered by a marker that was in flight when they tapped."""
        store_dir = self.dir
        self.write(q1=question(pr=573))

        class AnswerMidFlight(Recorder):
            def __call__(self, c, method, params, timeout=30):
                out = super().__call__(c, method, params, timeout)
                store = ta.load_store(ta.store_path(store_dir))
                store["questions"]["q1"]["answered_at"] = "2026-09-01T17:00:00Z"
                store["questions"]["q1"]["selected"] = [0]
                ta.save_store(ta.store_path(store_dir), store)
                return out

        ta.mark(CONFIG, store_dir, "q1", pm.MARK_ASLEEP, api=AnswerMidFlight(), now=NOW)
        self.assertEqual(self.record("q1")["answered_at"], "2026-09-01T17:00:00Z")
        self.assertEqual(self.record("q1")["selected"], [0])


# --------------------------------------------------------------------------- the local merge check

class LocalMergeCheckTest(unittest.TestCase):
    """**A missing object exits 1, exactly like a conflict** — `git merge-tree`'s real behaviour.
    Anything that trusts the exit code alone reads an unfetched head as *conflicted*, and a false 😴
    is a picker the owner is told not to tap. Both locks are pinned here."""

    def runner(self, script):
        """A `git` seam driven by a `{first-two-args: (code, stdout, stderr)}` script."""
        calls = []

        def run(argv, cwd=None):
            calls.append(argv)
            key = argv[3] if argv[3] != "cat-file" else f"cat-file:{argv[5].split('^')[0]}"
            return script.get(key, (128, "", "unscripted"))

        run.calls = calls
        return run

    def test_a_conflict_is_exit_one_WITH_output(self):
        run = self.runner({"cat-file:origin/develop": (0, "", ""), "cat-file:" + HEAD: (0, "", ""),
                           "merge-tree": (1, "250f41d3\nseneschal/context-budget.json\n", "")})
        self.assertEqual(pm.local_conflict("origin/develop", HEAD, runner=run), (True, ""))

    def test_a_clean_merge_is_exit_zero(self):
        run = self.runner({"cat-file:origin/develop": (0, "", ""), "cat-file:" + HEAD: (0, "", ""),
                           "merge-tree": (0, "238e8473\n", "")})
        self.assertEqual(pm.local_conflict("origin/develop", HEAD, runner=run), (False, ""))

    def test_a_missing_head_is_NOT_a_conflict(self):
        """The trap. `cat-file` refuses before `merge-tree` is ever reached."""
        run = self.runner({"cat-file:origin/develop": (0, "", ""),
                           "cat-file:" + HEAD: (1, "", "")})
        hit, why = pm.local_conflict("origin/develop", HEAD, runner=run)
        self.assertIsNone(hit)
        self.assertIn("not a commit in this checkout", why)
        self.assertNotIn("merge-tree", [a[3] for a in run.calls])

    def test_a_missing_base_is_NOT_a_conflict(self):
        run = self.runner({"cat-file:origin/develop": (1, "", "")})
        self.assertIsNone(pm.local_conflict("origin/develop", HEAD, runner=run)[0])

    def test_exit_one_with_EMPTY_stdout_is_not_a_conflict(self):
        """The second lock, which catches any other way git might decline that nobody predicted:
        `not something we can merge` writes to stderr and leaves stdout empty."""
        run = self.runner({"cat-file:origin/develop": (0, "", ""), "cat-file:" + HEAD: (0, "", ""),
                           "merge-tree": (1, "  \n", "not something we can merge")})
        self.assertIsNone(pm.local_conflict("origin/develop", HEAD, runner=run)[0])

    def test_an_unexpected_exit_code_is_not_a_conflict(self):
        run = self.runner({"cat-file:origin/develop": (0, "", ""), "cat-file:" + HEAD: (0, "", ""),
                           "merge-tree": (128, "whatever", "")})
        self.assertIsNone(pm.local_conflict("origin/develop", HEAD, runner=run)[0])

    def test_git_that_cannot_be_run_at_all_is_not_a_conflict(self):
        def explode(argv, cwd=None):
            raise FileNotFoundError("git is not on PATH")
        self.assertIsNone(pm.local_conflict("origin/develop", HEAD, runner=explode)[0])

    def test_every_git_call_disables_fsmonitor(self):
        """A checkout with an fsmonitor daemon attached can hang a background git call without it."""
        run = self.runner({"cat-file:origin/develop": (0, "", ""), "cat-file:" + HEAD: (0, "", ""),
                           "merge-tree": (0, "abc\n", "")})
        pm.local_conflict("origin/develop", HEAD, runner=run)
        self.assertTrue(run.calls)
        for argv in run.calls:
            self.assertEqual(argv[:3], ["git", "-c", "core.fsmonitor=false"])

    def test_it_never_fetches(self):
        run = self.runner({"cat-file:origin/develop": (0, "", ""), "cat-file:" + HEAD: (0, "", ""),
                           "merge-tree": (0, "abc\n", "")})
        pm.local_conflict("origin/develop", HEAD, runner=run)
        self.assertNotIn("fetch", [a[3] for a in run.calls])

    def test_the_local_arm_may_only_ADD_deadness(self):
        """The narrowing invariant. A clean local merge NEVER clears GitHub's `CONFLICTING` — a stale
        local base could otherwise resurrect a picker that genuinely cannot merge."""
        pickers = pm.pending({"questions": {"q1": question(pr=584, head=MOVED)}})
        marks, _, _ = pm.plan(pickers, index(row(pr=584, head=MOVED, mergeable="CONFLICTING")),
                              conflict_check=lambda *a: False, local_repo_key=mg.repo_key(REPO))
        self.assertEqual([m["state"] for m in marks], [pm.STATE_ASLEEP])

    def test_a_local_conflict_puts_a_github_clean_picker_to_bed(self):
        pickers = pm.pending({"questions": {"q1": question(pr=584, head=MOVED)}})
        marks, _, checks = pm.plan(pickers, index(row(pr=584, head=MOVED)),
                                   conflict_check=lambda *a: True,
                                   local_repo_key=mg.repo_key(REPO))
        self.assertEqual([m["state"] for m in marks], [pm.STATE_ASLEEP])
        self.assertEqual(checks, 1)
        self.assertIn("conflicts with develop in this checkout", marks[0]["reason"])

    def test_another_repository_is_never_checked_locally_and_never_spends_the_budget(self):
        """The cap has to bound git calls. A picker for a repository this checkout is not is skipped
        BEFORE the budget is charged, or another repository's backlog spends the whole allowance
        on nothing."""
        pickers = pm.pending({"questions": {
            "q1": question(pr=45, repo=OTHER_REPO, message_id=1),
            "q2": question(pr=584, head=MOVED, message_id=2, asked_at="2026-09-01T16:00:00Z")}})
        idx = {**index(row(pr=584, head=MOVED)),
               mg.repo_key(OTHER_REPO): ps.open_rows([row(pr=45)])}
        seen = []

        def check(repo, base, head):
            seen.append(repo)
            return False

        marks, _, checks = pm.plan(pickers, idx, conflict_check=check,
                                   local_repo_key=mg.repo_key(REPO), max_local_checks=1)
        self.assertEqual(seen, [REPO])
        self.assertEqual(checks, 1)

    def test_the_cap_bounds_the_checks_and_the_rest_use_githubs_field(self):
        pickers = pm.pending({"questions": {
            f"q{pr}": question(pr=pr, message_id=pr, asked_at=f"2026-09-01T1{i}:00:00Z")
            for i, pr in enumerate([573, 574, 575])}})
        idx = index(*[row(pr=pr) for pr in (573, 574, 575)])
        seen = []
        marks, _, checks = pm.plan(pickers, idx,
                                   conflict_check=lambda r, b, h: seen.append(h) or False,
                                   local_repo_key=mg.repo_key(REPO), max_local_checks=2)
        self.assertEqual(checks, 2)
        self.assertEqual(len(marks), 3)                       # the third is still marked, unchecked

    def test_no_local_repository_turns_the_whole_arm_off(self):
        self.assertEqual(pm.local_repo(runner=lambda a, cwd=None: (1, "", "not a repository")), "")
        self.assertEqual(pm.local_repo(runner=lambda a, cwd=None: (0, "https://example.com/x/y", "")),
                         "")

    def test_the_local_repository_is_read_through_merge_guards_own_derivation(self):
        run = lambda a, cwd=None: (0, f"https://github.com/{REPO}\n", "")  # noqa: E731
        self.assertEqual(pm.local_repo(runner=run), REPO)


# --------------------------------------------------------------------------- scope and absences

class OutOfScopeTest(StoreCase):
    """`test_picker_retire.OutOfScopeTest`, one module over and for the same reason. These two
    shapes must be untouchable by anything: a **held outbound approval** of another kind is a live
    decision, and an **unattributed picker** (`meta: null`) is not provably about a pull request."""

    def test_a_held_outbound_approval_is_never_marked(self):
        self.write(**{"2cb5cb2d": question(pr=573, kind="held-outbound-approval")})
        report, api = self.sweep(index(row(pr=573)))
        self.assertEqual(api.calls, [])
        self.assertEqual(report["pending"], 0)

    def test_the_ghost_picker_is_never_marked(self):
        self.write(**{"ghost001": question(meta=None)})
        report, api = self.sweep(index(row(pr=573)))
        self.assertEqual(api.calls, [])
        self.assertEqual(report["pending"], 0)

    def test_a_picker_naming_no_repository_is_never_marked(self):
        self.write(q1=question(meta={"kind": mg.APPROVAL_META_KIND, "pr": 573, "head_sha": HEAD}))
        report, api = self.sweep(index(row(pr=573)))
        self.assertEqual(api.calls, [])
        self.assertEqual(report["pending"], 0)

    def test_eligibility_is_picker_retires_rule_and_not_a_second_one(self):
        """Asserted structurally: `pending` must call `picker_retire.pending_pr_pickers`, so widening
        one rule cannot leave the other behind."""
        self.assertIn("pending_pr_pickers", pm.pending.__code__.co_names)
        self.assertNotIn("APPROVAL_META_KIND", read_source("picker_mark.py"),
                         "picker_mark must not spell the eligibility rule a second time")


class NeverSendsTest(unittest.TestCase):
    """It marks. **It does not send** — a reaction raises no notification and a message does, and the
    whole argument for re-deriving state every pass rests on that."""

    def setUp(self):
        self.source = read_source("picker_mark.py")

    def test_no_send_method_appears_in_the_module(self):
        for method in ("sendMessage", "send_text", "editMessageText", "answerCallbackQuery",
                       "telegram_send.send", "deleteMessage"):
            self.assertNotIn(method, self.source)

    def test_it_never_clears_a_reaction(self):
        """Every pending PR picker resolves to one of three glyphs, so an empty reaction list is
        never needed — and a clear is the one call that could silently un-mark the whole queue."""
        self.assertNotIn("[]}", self.source.replace(" ", ""))
        for state in (pm.STATE_NEXT, pm.STATE_QUEUED, pm.STATE_ASLEEP):
            self.assertTrue(pm.MARKS[state])

    def test_only_one_bot_api_method_is_reachable(self):
        self.assertIn("ta.mark(", self.source)
        self.assertEqual(ta.REACTION_METHOD, "setMessageReaction")


class NeverApprovesTest(unittest.TestCase):
    """`record_approval`'s single-caller property is frozen. Nothing here may widen what merges."""

    def test_no_approval_writer_is_reachable_from_the_compiled_module(self):
        """**Asserted off the BYTECODE, not the source text**, which is `telegram_topics`' rule for
        the same shape of claim: the module docstring names `record_approval` in the sentence
        promising never to call it, and a text scan cannot tell a refusal from a call site. Compiled
        code has no docstrings in it, so this asks the question that actually matters."""
        names = source_names("picker_mark.py")
        for name in ("record_approval", "consume_approval", "verify_approval", "decide_command"):
            self.assertNotIn(name, names)

    def test_marking_never_fabricates_an_answer(self):
        """A marked question was never answered: `answered_at` stays null and `selected` stays empty,
        which is exactly what `merge_guard._question_confirms` reads."""
        d = tempfile.mkdtemp(prefix="picker-mark-approval-")
        self.addCleanup(shutil.rmtree, d, True)
        ta.save_store(ta.store_path(d), {"schema": ta.SCHEMA, "expired": [],
                                         "questions": {"q1": question(pr=573)}})
        ta.mark(CONFIG, d, "q1", pm.MARK_NEXT, api=Recorder(), now=NOW)
        rec = ta.load_store(ta.store_path(d))["questions"]["q1"]
        self.assertIsNone(rec["answered_at"])
        self.assertEqual(rec["selected"], [])
        self.assertEqual(rec["reaction"], pm.MARK_NEXT)


class VocabularyTest(unittest.TestCase):
    """The three glyphs are Telegram's to allow, not the assistant's to choose — and the report is the one
    place a glyph must never appear."""

    def test_the_three_glyphs_are_in_telegrams_allowed_set(self):
        """Pinned against this repo's own tracked copy of the `ReactionTypeEmoji` list, written down
        for the inbound half. Keycap digits and 🛏 are NOT in it, which is why they are not the
        vocabulary."""
        spec = read_source(os.path.join("..", "docs", "telegram-inbound-spec.md"))
        allowed = spec.split("ReactionTypeEmoji` list:", 1)[1].split("**", 1)[0]
        for glyph in (pm.MARK_NEXT, pm.MARK_QUEUED, pm.MARK_ASLEEP):
            self.assertIn(glyph, allowed)
        for impossible in ("\U0001F6CF", "\U0001F4A4"):       # 🛏 and 💤, both REACTION_INVALID
            self.assertNotIn(impossible, allowed)

    def test_the_three_glyphs_are_distinct(self):
        self.assertEqual(len(set(pm.MARKS.values())), 3)

    def test_no_glyph_reaches_the_report(self):
        """`pr_sweep.main` prints its report with `ensure_ascii=False` and a Windows console is
        cp1252, so a non-BMP glyph there is a crash in a diagnostic. State NAMES travel; glyphs do
        not."""
        entry = pm._entry(question(pr=573) | {"repo": REPO, "pr": 573, "question_id": "q1",
                                              "reaction": pm.MARK_NEXT},
                          state=pm.STATE_ASLEEP, reason="because")
        blob = json.dumps(entry, ensure_ascii=False)
        for glyph in pm.MARKS.values():
            self.assertNotIn(glyph, blob)
        self.assertTrue(blob.encode("cp1252"))

    def test_the_summary_line_carries_no_glyph_either(self):
        line = pm.summary_line({"marked": [{"repo": REPO, "pr": 573, "state": pm.STATE_ASLEEP}]})
        self.assertIn("asleep", line)
        self.assertTrue(line.encode("cp1252"))

    def test_a_pass_that_changed_nothing_says_nothing(self):
        self.assertEqual(pm.summary_line({"marked": []}), "")
        self.assertEqual(pm.summary_line({}), "")


class DryRunTest(StoreCase):
    """`--dry-run` answers *what is about to happen to my phone?* — so it must reach the real verdict
    while touching nothing."""

    def test_a_dry_run_reacts_to_nothing_and_writes_nothing(self):
        self.write(q1=question(pr=573))
        api = Recorder()
        report = pm.sweep(state_dir=self.dir, row_index=index(row(pr=573)), api=api, runner=no_git,
                          config=CONFIG, now=NOW, dry_run=True)
        self.assertEqual(api.calls, [])
        self.assertNotIn("reaction", self.record("q1"))
        self.assertEqual([m["state"] for m in report["marked"]], [pm.STATE_NEXT])
        self.assertTrue(report["marked"][0]["dry_run"])

    def test_a_dry_run_needs_no_credentials(self):
        """`telegram.env` is gitignored and lives only in the daemon's checkout, so demanding it
        would make `--dry-run` from a worktree preview nothing — which is exactly when someone wants
        to know what is about to happen. `picker_retire`'s rule, and its reason."""
        self.write(q1=question(pr=573))
        report = pm.sweep(state_dir=self.dir, row_index=index(row(pr=573)), api=Recorder(),
                          runner=no_git, config=None, now=NOW, dry_run=True,
                          env_file=os.path.join(self.dir, "nope.env"))
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(report["marked"]), 1)


class RowIndexTest(unittest.TestCase):
    """`pr_sweep.open_rows` is the third view of one list, and its `None` is the whole point."""

    def test_an_unreadable_repo_and_a_truncated_list_both_answer_None(self):
        self.assertIsNone(ps.open_rows(None))
        self.assertIsNone(ps.open_rows([row(pr=573)], truncated=True))

    def test_a_row_with_no_readable_number_is_absent_not_guessed(self):
        built = ps.open_rows([row(pr=573), {"number": "573"}, {"number": True}, "nonsense"])
        self.assertEqual(list(built), [573])

    def test_all_three_views_describe_the_same_read(self):
        rows = [row(pr=573), row(pr=574, head=MOVED)]
        self.assertEqual(set(ps.open_numbers(rows)), set(ps.open_rows(rows)))
        self.assertEqual(ps.open_heads(rows),
                         {pr: r["headRefOid"] for pr, r in ps.open_rows(rows).items()})

    def test_base_ref_name_is_on_the_call_the_pass_already_makes(self):
        """One more field on one `gh pr list`, which is `headRefOid`'s and `mergeable`'s own move —
        never a `gh pr view` per pull request per pass."""
        self.assertIn("baseRefName", ps.LIST_FIELDS)
        self.assertEqual(ps.LIST_FIELDS.count("baseRefName"), 1)


class SweepWiringTest(StoreCase):
    """`pr_sweep` owns the ordering: ask, then MARK, then retire."""

    def test_marking_runs_between_asking_and_retiring(self):
        order = []

        def marker(**kw):
            order.append("mark")
            return {"marked": [{"repo": REPO, "pr": 573, "state": pm.STATE_ASLEEP}], "errors": []}

        def retirer(**kw):
            order.append("retire")
            return {"retired": [], "errors": []}

        def lister(argv):
            order.append("list")
            return 0, json.dumps([row(pr=573)]), ""

        report = ps.sweep(repos=(REPO,), state_dir=self.dir, asker=lambda *a, **k: {
            "ok": True, "sent": False, "reason": "already asked"}, lister=lister,
            marker=marker, retirer=retirer, now=NOW)
        self.assertEqual(order, ["list", "mark", "retire"])
        self.assertEqual(report["marked"][0]["pr"], 573)
        self.assertIn("marked asleep", ps.summary_line(report))

    def test_the_marker_is_handed_the_row_index_off_this_passs_own_list(self):
        seen = {}

        def marker(**kw):
            seen.update(kw)
            return {"marked": [], "errors": []}

        ps.sweep(repos=(REPO,), state_dir=self.dir,
                 asker=lambda *a, **k: {"ok": True, "sent": False, "reason": "x"},
                 lister=lambda argv: (0, json.dumps([row(pr=573)]), ""),
                 marker=marker, retirer=lambda **kw: {"retired": [], "errors": []}, now=NOW)
        self.assertEqual(set(seen["row_index"]), {mg.repo_key(REPO)})
        self.assertEqual(seen["row_index"][mg.repo_key(REPO)][573]["baseRefName"], "develop")

    def test_a_marking_error_is_reported_under_its_own_prefix(self):
        report = ps.sweep(repos=(), state_dir=self.dir,
                          marker=lambda **kw: {"marked": [], "errors": ["the wire was down"]},
                          retirer=lambda **kw: {"retired": [], "errors": []}, now=NOW)
        self.assertEqual(report["errors"], ["picker-mark: the wire was down"])

    def test_marking_RUNS_inside_the_quiet_window_while_asking_does_not(self):
        """**The overnight hours are when marking is needed most.** Hanging marking off an early
        return inside the 01:00-07:00 window would withhold the marker for a quarter of every day —
        and an owner up late still taps in that quarter, on pickers that went out CLEAN and went
        dirty afterwards, where **marking is the only thing that could catch it**. A reaction raises
        no notification, so it cannot be the reason anyone wakes; a picker can, and is still
        suppressed."""
        seen = []
        report = ps.sweep(
            repos=(REPO,), state_dir=self.dir,
            asker=lambda *a, **k: self.fail("asked inside the quiet window"),
            lister=lambda argv: (0, json.dumps([row(pr=573)]), ""),
            marker=lambda **kw: seen.append("mark") or {"marked": [], "errors": []},
            retirer=lambda **kw: seen.append("retire") or {"retired": [], "errors": []},
            now=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))        # 03:00 owner-local
        self.assertTrue(report["quiet_hours"])
        self.assertEqual(seen, ["mark", "retire"])
        self.assertEqual(report["asked"], [])
        self.assertTrue(any("quiet window" in x["reason"] for x in report["deferred"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
