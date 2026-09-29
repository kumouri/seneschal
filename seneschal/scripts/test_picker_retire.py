#!/usr/bin/env python3
"""Tests for ``picker_retire`` — the pass that takes a merge picker down when its PR stops being open
— and for the ``telegram_ask`` half that does the edit and the stamp.

Six classes here defend properties rather than behaviour, and must not be relaxed into passing:

* **``OutOfScopeTest``** is the one that matters most. A **held outbound approval** of another
  kind (``2cb5cb2d``) is still a live decision, and an **unattributed picker** with ``meta: null``
  (``ghost001``) is something nobody can say the origin of — neither may ever be expired, edited,
  deleted or answered by a retirement. Both are pinned by id and ``meta``, so widening the
  eligibility rule breaks this file by name.
* **``FailOpenTest``** pins the polarity. A wrongly-retired live question is far worse than a stale
  one: the owner can ignore noise, they cannot recover a decision they were never shown. Every
  *cannot tell* —
  an unswept repo, a truncated list, an unreadable ``gh``, a state nobody recognises, an edit
  Telegram never answered — must leave the picker EXACTLY as it is.
* **``NeverFabricatesAnAnswerTest``** is the ledger's floor. A retired question was never answered:
  ``answered_at`` stays null and ``selected`` stays empty forever, and ``merge_guard`` 's approval
  cross-check (which reads exactly those two fields) is proved to still refuse.
* **``MidFlightApprovalTest``** is the race. The owner may tap in the same second the PR merges; a
  retirement may not corrupt an approval that is legitimately in flight, and a tap that arrives after
  a retirement may not mint one.
* **``OrphanedRecordTest``** reconciles records whose messages were deleted by hand.
  A retirement pass must handle *the message is already gone* without erroring and **without
  re-sending anything**.
* **``BoundedCostTest``** pins constraint 1 — no second poller, and no ``gh`` call per pending
  question per tick. A pass with nothing to retire spends nothing at all; a pass with two pickers for
  one PR confirms once.

**No test here reaches the network, calls ``gh``, or sends anything.** Every subprocess goes through
the ``runner=`` seam, every Bot API call through ``api=``, every credential through ``config=``, and
every store lives in a ``tempfile`` directory — nothing reads or writes the live ``seneschal/state/``.
The owner's zone is pinned to a fixed UTC-5 for the whole module (``setUpModule``), so the settled
lines read the same wall clock on every runner.

Run:  python -m unittest seneschal.scripts.test_picker_retire   (or)   python test_picker_retire.py
"""
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
import picker_retire as pkr  # noqa: E402
import telegram_ask as ta  # noqa: E402
import tz_common  # noqa: E402 — the owner-zone seam `clock.to_local` reads
from telegram_send import TelegramAPIError  # noqa: E402

REPO = "example/repo"
OTHER_REPO = "example/other"
THIRD_REPO = "example/third"

#: The owner's zone for the whole module: a FIXED UTC-5 offset, so every settled line below reads
#: the same wall clock on every runner whatever its own timezone or identity config.
OWNER_ZONE = timezone(timedelta(hours=-5), "UTC-5")
_ZONE_PATCH = mock.patch.object(tz_common, "_zone", return_value=OWNER_ZONE)


def setUpModule():
    _ZONE_PATCH.start()


def tearDownModule():
    _ZONE_PATCH.stop()


HEAD = "8f8bc6328d93abba4db7d0fd95ab8bf9700d6bef"
CHAT = "123456"
CONFIG = {"token": "test-token", "chat_id": CHAT, "api_base": "https://example.invalid",
          "parse_mode": "", "format": "plain"}

#: Explicit UTC instants, never the runner's clock. In :data:`OWNER_ZONE` (UTC-5) 22:24Z is 17:24
#: local — the minute #478 merged in these fixtures.
MERGED_AT = "2026-08-27T22:24:00Z"
LATER = datetime(2026, 8, 28, 1, 13, tzinfo=timezone.utc)          # 20:13 local, the late tap
NEXT_DAY = datetime(2026, 8, 29, 1, 13, tzinfo=timezone.utc)


def question(pr=478, repo=REPO, kind=mg.APPROVAL_META_KIND, message_id=10470, answered=None,
             meta="auto", asked_at="2026-08-28T01:08:15Z"):
    """One store record in the shape `telegram_ask.ask` actually writes."""
    if meta == "auto":
        meta = {"kind": kind, "pr": pr, "repo": repo, "head_sha": HEAD, "approve_index": 0}
    return {"question": f"Merge {repo} PR #{pr} — a pull request?",
            "body": f"Merge {repo} PR #{pr} — a pull request?\n\nTap one.\n\n1. Approve",
            "options": [{"label": "Approve", "description": "it merges"},
                        {"label": "Not now", "description": "nothing merges"}],
            "multi": False, "recommended": False, "chat_id": CHAT, "message_id": message_id,
            "asked_at": asked_at, "selected": [], "answered_at": answered, "meta": meta}


def store_with(**questions) -> dict:
    return {"schema": ta.SCHEMA, "questions": dict(questions), "expired": []}


def facts(pr=478, repo=REPO, state="MERGED", merged_at=MERGED_AT, closed_at=""):
    """What `merge_guard.pr_facts` returns, including the two message-only fields added for this."""
    return {"pr": pr, "repo": repo, "paths": ["seneschal/scripts/usage.py"], "head_sha": HEAD,
            "state": state, "title": "feat(usage): read the plan meters on a cadence",
            "url": f"https://github.com/{repo}/pull/{pr}", "base": "develop", "body": "",
            "merged_at": merged_at, "closed_at": closed_at}


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


class StoreCase(unittest.TestCase):
    """A temp state dir with a question store in it. The live `seneschal/state/` is never touched."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="picker-retire-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, store):
        ta.save_store(ta.store_path(self.dir), store)

    def read(self) -> dict:
        return ta.load_store(ta.store_path(self.dir))

    def record(self, qid) -> dict:
        return self.read()["questions"][qid]

    def runner_for(self, *payloads):
        """A `merge_guard._run` seam answering `gh pr view` **by the PR number in the argv**, and
        counting the calls.

        Dispatching on the number rather than on call order is not tidiness: `pr_facts` REFUSES a
        response whose `number` is not the one it asked about, so an order-dependent stub answers
        the wrong PR and the module correctly declines — which would show up as a passing
        fail-open test and a silently unexercised retirement."""
        by_pr = {int(payload["number"]): payload for payload in payloads}
        calls = []

        def run(argv, cwd=None):
            calls.append(argv)
            try:
                wanted = int(argv[3])
            except (IndexError, TypeError, ValueError):
                return 1, "", "unreadable argv"
            if wanted not in by_pr:
                return 1, "", f"no canned answer for #{wanted}"
            return 0, json.dumps(by_pr[wanted]), ""

        run.calls = calls
        return run

    def gh_payload(self, pr=478, repo=REPO, state="MERGED", merged_at=MERGED_AT, closed_at=None):
        """What `gh pr view --json …` returns, keyed as `gh` spells it (camelCase)."""
        return {"number": pr, "files": [{"path": "seneschal/scripts/usage.py"}], "headRefOid": HEAD,
                "state": state, "title": "feat(usage): the plan meters",
                "url": f"https://github.com/{repo}/pull/{pr}", "body": "", "baseRefName": "develop",
                "mergedAt": merged_at, "closedAt": closed_at}


# --------------------------------------------------------------------------- eligibility

class OutOfScopeTest(StoreCase):
    """**NEVER TOUCH A NON-MERGE PICKER.** Pinned against the two shapes that must stay out."""

    #: A held outbound approval of another kind: a live decision about something the assistant is
    #: holding for the owner. Retiring it would silently drop that decision.
    HELD_OUTBOUND = {"date": "2026-08-22", "kind": "held-outbound-approval",
                     "recipient": "example-recipient"}

    def test_a_held_outbound_approval_is_not_a_candidate(self):
        store = store_with(**{"2cb5cb2d": question(meta=self.HELD_OUTBOUND, message_id=8968)})
        self.assertEqual(pkr.pending_pr_pickers(store), [])

    def test_the_ghost_picker_is_not_a_candidate(self):
        # `meta: null` — a picker nobody can attribute. Whatever it is, it is not provably a pull
        # request question, so it must never be expired, edited, deleted or answered by a retirement.
        store = store_with(**{"ghost001": question(meta=None, message_id=9919)})
        self.assertEqual(pkr.pending_pr_pickers(store), [])

    def test_neither_is_touched_by_a_whole_sweep_that_retires_a_real_one(self):
        """The end-to-end version: a pass that DOES retire a merge picker leaves both alone, in the
        store AND on the wire. A per-function assertion cannot catch a second code path."""
        store = store_with(**{"2cb5cb2d": question(meta=self.HELD_OUTBOUND, message_id=8968),
                              "ghost001": question(meta=None, message_id=9919),
                              "890ec559": question(message_id=10470)})
        self.write(store)
        api = Recorder()
        pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()}, api=api,
                  runner=self.runner_for(self.gh_payload()), config=CONFIG, now=LATER)
        after = self.read()["questions"]
        for untouched in ("2cb5cb2d", "ghost001"):
            self.assertNotIn("retired_at", after[untouched])
            self.assertIsNone(after[untouched]["answered_at"])
            self.assertEqual(after[untouched]["selected"], [])
        self.assertIn("retired_at", after["890ec559"])
        edited = [p.get("message_id") for m, p in api.calls]
        self.assertEqual(edited, [10470], "only the merge picker's message may be touched")

    def test_a_merge_picker_with_no_repo_is_not_a_candidate(self):
        """Legacy pickers carry no `repo`. A PR number is not an identity across
        repositories, so there is nothing here to confirm against — leave it up."""
        legacy = {"kind": mg.APPROVAL_META_KIND, "pr": 90, "head_sha": HEAD, "approve_index": 0}
        self.assertEqual(pkr.pending_pr_pickers(store_with(a1=question(meta=legacy))), [])

    def test_an_answered_or_already_retired_question_is_not_a_candidate(self):
        answered = question()
        answered["answered_at"] = "2026-08-28T00:11:17Z"
        retired = question()
        retired["retired_at"] = "2026-08-28T01:30:00Z"
        self.assertEqual(pkr.pending_pr_pickers(store_with(a=answered, b=retired)), [])

    def test_a_pr_number_that_is_a_bool_is_not_a_pull_request(self):
        meta = {"kind": mg.APPROVAL_META_KIND, "pr": True, "repo": REPO, "head_sha": HEAD}
        self.assertEqual(pkr.pending_pr_pickers(store_with(a=question(meta=meta))), [])


class DocsOnlyNoticeIsInScopeTest(StoreCase):
    """**A docs-only notice is a pull-request question too, and retirement must treat it as one** —
    `merge_guard.DOCS_ONLY_META_KIND` is in `RETIRABLE_META_KINDS`. It is the mirror of
    `OutOfScopeTest`: that class pins what must stay OUT, this one pins that the new kind is IN, so a
    future edit narrowing the set back to one member breaks a named test rather than silently
    reintroducing the stale-picker gap for docs-only PRs."""

    def test_a_docs_only_notice_is_a_pending_candidate(self):
        store = store_with(a=question(kind=mg.DOCS_ONLY_META_KIND))
        pending = pkr.pending_pr_pickers(store)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["pr"], 478)

    def test_a_merged_docs_only_notice_is_retired_by_a_real_sweep(self):
        store = store_with(**{"890ec559": question(kind=mg.DOCS_ONLY_META_KIND, message_id=10470)})
        self.write(store)
        api = Recorder()
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()}, api=api,
                           runner=self.runner_for(self.gh_payload()), config=CONFIG, now=LATER)
        self.assertEqual(len(report["retired"]), 1)
        after = self.record("890ec559")
        self.assertIn("retired_at", after)
        self.assertIsNone(after["answered_at"])
        self.assertEqual(after["selected"], [])


class PendingReposTest(StoreCase):
    """`pending_repos` — the ONE enumeration of which repositories hold a stale picker to look for,
    shared by the CLI and the resident pass's retire-only widening. It derives from
    `pending_pr_pickers`, so
    every eligibility rule that class of tests pins is inherited here for free."""

    def test_it_names_each_repository_once_sorted(self):
        store = store_with(a=question(pr=1, repo=THIRD_REPO),
                           b=question(pr=2, repo=THIRD_REPO),
                           c=question(pr=3, repo=OTHER_REPO),
                           d=question(pr=4, repo=REPO))
        self.assertEqual(pkr.pending_repos(store), [OTHER_REPO, REPO, THIRD_REPO])

    def test_it_keys_on_the_guards_repo_key_so_spelling_does_not_double_a_repo(self):
        store = store_with(a=question(pr=1, repo="Example/Third"),
                           b=question(pr=2, repo=THIRD_REPO))
        repos = pkr.pending_repos(store)
        self.assertEqual(len(repos), 1)
        self.assertEqual(mg.repo_key(repos[0]), mg.repo_key(THIRD_REPO))

    def test_an_answered_retired_or_out_of_scope_picker_names_no_repository(self):
        store = store_with(a=question(pr=1, repo=THIRD_REPO, answered="2026-09-12T18:00:00Z"),
                           b={**question(pr=2, repo=OTHER_REPO), "retired_at": "2026-09-12T18:00:00Z"},
                           c=question(pr=3, repo="example/fourth", kind="held-outbound-approval"),
                           d=question(pr=4, repo="example/fifth", meta=None))
        self.assertEqual(pkr.pending_repos(store), [],
                         "only a pending, retirable, PR-shaped question can put a repository here")

    def test_the_state_dir_door_reads_the_same_store(self):
        self.write(store_with(a=question(pr=113, repo=THIRD_REPO)))
        self.assertEqual(pkr.pending_repos_in(self.dir), [THIRD_REPO])

    def test_the_cli_uses_it_rather_than_a_second_comprehension(self):
        with open(os.path.join(SCRIPT_DIR, "picker_retire.py"), encoding="utf-8") as fh:
            body = fh.read().split("def main(", 1)[1]
        self.assertIn("pending_repos(", body)
        self.assertNotIn('{p_["repo"] for p_ in', body,
                         "the CLI's own enumeration is the shared function now, not a private copy")


# --------------------------------------------------------------------------- fail open

class FailOpenTest(StoreCase):
    """**Cannot tell ⇒ leave it.** Every one of these must end with the picker exactly as it was."""

    def setUp(self):
        super().setUp()
        self.write(store_with(**{"890ec559": question()}))
        self.api = Recorder()

    def assertUntouched(self, report):
        rec = self.record("890ec559")
        self.assertNotIn("retired_at", rec)
        self.assertEqual(rec["selected"], [])
        self.assertIsNone(rec["answered_at"])
        self.assertEqual(self.api.calls, [], "nothing may go out on a decline")
        self.assertEqual(report["retired"], [])

    def test_a_repository_this_pass_did_not_sweep(self):
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(OTHER_REPO): set()},
                           api=self.api, config=CONFIG, now=LATER)
        self.assertUntouched(report)
        self.assertEqual(report["confirms"], 0, "an unswept repo may not cost a gh call either")

    def test_a_list_that_could_not_be_read(self):
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): None},
                           api=self.api, config=CONFIG, now=LATER)
        self.assertUntouched(report)

    def test_a_truncated_list_means_nothing(self):
        """`open_numbers` collapses truncation into `None` — an open PR past the page boundary would
        otherwise look merged, and a live picker would be retired on a paging artifact."""
        import pr_sweep
        self.assertIsNone(pr_sweep.open_numbers([{"number": 1}], truncated=True))
        report = pkr.sweep(state_dir=self.dir,
                           open_index={mg.repo_key(REPO): pr_sweep.open_numbers([{"number": 1}],
                                                                               truncated=True)},
                           api=self.api, config=CONFIG, now=LATER)
        self.assertUntouched(report)

    def test_a_pr_that_is_still_open(self):
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): {478}},
                           api=self.api, config=CONFIG, now=LATER)
        self.assertUntouched(report)

    def test_gh_cannot_be_read(self):
        def broken(argv, cwd=None):
            raise FileNotFoundError("gh")

        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()},
                           api=self.api, runner=broken, config=CONFIG, now=LATER)
        self.assertUntouched(report)

    def test_a_state_nobody_recognises_asks_nothing_and_retires_nothing(self):
        """`not_open_refusal` is an allow-list of refusals, never `!= "OPEN"`. An unrecognised state
        leaves the picker up — the same polarity the asking door already uses."""
        payload = self.gh_payload(state="SOMETHING_NEW")
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()}, api=self.api,
                           runner=self.runner_for(payload), config=CONFIG, now=LATER)
        rec = self.record("890ec559")
        self.assertNotIn("retired_at", rec)
        self.assertEqual(report["retired"], [])

    def test_an_edit_telegram_never_answered_writes_nothing(self):
        """A transport failure is not evidence the message is settled — so the record stays pending
        and the next pass tries again. Convergence, not a lost picker."""
        api = Recorder(raises=RuntimeError("connection reset"))
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()}, api=api,
                           runner=self.runner_for(self.gh_payload()), config=CONFIG, now=LATER)
        self.assertNotIn("retired_at", self.record("890ec559"))
        self.assertEqual(report["retired"], [])
        self.assertTrue(report["errors"])

    def test_no_bot_token_retires_nothing(self):
        """The real credential path, with the environment emptied so the host's own `telegram.env`
        can never make this pass by accident — and an empty `--env-file`, which wins over it."""
        empty = os.path.join(self.dir, "telegram.env")
        with open(empty, "w", encoding="utf-8") as fh:
            fh.write("# no token here\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()},
                               api=self.api, runner=self.runner_for(self.gh_payload()),
                               env_file=empty, now=LATER)
        self.assertUntouched(report)
        self.assertTrue(any("token" in e for e in report["errors"]))

    def test_a_missing_store_is_an_empty_pass_not_an_error(self):
        empty = tempfile.mkdtemp(prefix="picker-retire-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        report = pkr.sweep(state_dir=empty, open_index={mg.repo_key(REPO): set()}, config=CONFIG)
        self.assertEqual((report["pending"], report["retired"], report["errors"]), (0, [], []))


# --------------------------------------------------------------------------- the record

class NeverFabricatesAnAnswerTest(StoreCase):
    """**RETIRED is not ANSWERED**, and no reader may ever be able to confuse the two."""

    def setUp(self):
        super().setUp()
        self.write(store_with(**{"890ec559": question()}))

    def test_the_record_keeps_answered_at_null_and_selected_empty(self):
        api = Recorder()
        res = ta.retire(CONFIG, self.dir, "890ec559", "text", api=api, now=LATER)
        rec = self.record("890ec559")
        self.assertTrue(res["retired"])
        self.assertIsNone(rec["answered_at"])
        self.assertEqual(rec["selected"], [])
        self.assertEqual(rec["retired_at"], "2026-08-28T01:13:00Z")
        self.assertEqual(rec["retired_reason"], "text")
        self.assertEqual(rec["retired_edit"], ta.EDIT_LANDED)

    def test_an_approval_cannot_be_verified_from_a_retired_question(self):
        """`merge_guard._question_confirms` reads `answered_at` and `selected` — the two fields
        retirement leaves alone — so a forged approval naming a retired question still fails."""
        ta.retire(CONFIG, self.dir, "890ec559", "text", api=Recorder(), now=LATER)
        mg.record_approval(self.dir, 478, HEAD, "890ec559", repo=REPO, now=LATER)
        why = mg.verify_approval(self.dir, 478, HEAD, repo=REPO, now=LATER)
        self.assertIn("never answered", why)

    def test_list_renders_retired_apart_from_answered(self):
        ta.retire(CONFIG, self.dir, "890ec559", "#478 merged at 17:24 — nothing to approve.",
                  api=Recorder(), now=LATER)
        store = self.read()
        row = store["questions"]["890ec559"]
        self.assertTrue(row.get("retired_at") and not row.get("answered_at"))

    def test_the_keyboard_is_stripped_by_the_edit(self):
        api = Recorder()
        ta.retire(CONFIG, self.dir, "890ec559", "settled", api=api, now=LATER)
        method, params = api.calls[0]
        self.assertEqual(method, "editMessageText")
        self.assertEqual(json.loads(params["reply_markup"]), {"inline_keyboard": []})
        self.assertEqual(params["text"], "settled")


class SettledTextTest(unittest.TestCase):
    """**The exact words a retired picker becomes.** A record, never an apology."""

    def test_a_merge_on_the_same_day_names_the_clock_only(self):
        self.assertEqual(
            pkr.settled_text(facts(), now=LATER),
            "example/repo #478 merged at 17:24 — nothing to approve.")

    def test_a_merge_on_another_day_names_the_date_too(self):
        # Read a week later, "merged at 17:24" quietly lies about WHICH day it means.
        self.assertEqual(
            pkr.settled_text(facts(), now=NEXT_DAY),
            "example/repo #478 merged on Aug 27 at 17:24 — nothing to approve.")

    def test_a_closed_pr_says_it_did_not_merge(self):
        self.assertEqual(
            pkr.settled_text(facts(state="CLOSED", merged_at="",
                                   closed_at="2026-08-27T14:02:00Z"), now=LATER),
            "example/repo #478 was closed without merging at 09:02 — "
            "nothing to approve.")

    def test_an_unreadable_timestamp_costs_the_time_and_not_the_line(self):
        self.assertEqual(pkr.settled_text(facts(merged_at="whenever"), now=LATER),
                         "example/repo #478 has merged — nothing to approve.")

    def test_the_time_is_the_owners_wall_clock_not_the_machines(self):
        """The same instant reads differently for an owner in another zone — `clock.to_local`
        (`tz_common`) decides, never the runner's own zone and never a hard-coded one."""
        with mock.patch.object(tz_common, "_zone",
                               return_value=timezone(timedelta(hours=2), "UTC+2")):
            self.assertEqual(pkr.settled_text(facts(), now=LATER),
                             "example/repo #478 merged at 00:24 — nothing to approve.")
            # ...and the day boundary moves with the zone: "today" is the owner's today.
            self.assertEqual(pkr.settled_text(facts(), now=NEXT_DAY),
                             "example/repo #478 merged on Aug 28 at 00:24 — nothing to approve.")

    def test_it_carries_no_zone_label(self):
        """The owner reads their own clock; a label from the machine-local fallback could name a
        zone they are not in."""
        text = pkr.settled_text(facts(), now=LATER)
        for label in (" UTC", OWNER_ZONE.tzname(None), " GMT", "+00:00", "Z —"):
            self.assertNotIn(label, text)

    def test_it_never_says_the_owner_was_late(self):
        for state, kw in (("MERGED", {}), ("CLOSED", {"merged_at": "", "closed_at": MERGED_AT})):
            text = pkr.settled_text(facts(state=state, **kw), now=NEXT_DAY).lower()
            for scold in ("too late", "missed", "you should", "already answered", "sorry"):
                self.assertNotIn(scold, text)


# --------------------------------------------------------------------------- the race

class MidFlightApprovalTest(StoreCase):
    """A tap that arrives anyway must still be safe, in both directions."""

    def setUp(self):
        super().setUp()
        self.write(store_with(**{"890ec559": question()}))

    def test_an_answer_that_lands_during_the_edit_wins(self):
        """The store is re-read after the edit. The owner's tap is the decision; the retirement stands down
        and leaves the answer — and therefore the approval it authorises — intact."""
        def answer_mid_flight(c, method, params, timeout=30):
            store = self.read()
            store["questions"]["890ec559"]["answered_at"] = "2026-08-28T01:12:59Z"
            store["questions"]["890ec559"]["selected"] = [0]
            self.write(store)
            return {"ok": True, "result": {}}

        res = ta.retire(CONFIG, self.dir, "890ec559", "settled", api=answer_mid_flight, now=LATER)
        rec = self.record("890ec559")
        self.assertFalse(res["retired"])
        self.assertNotIn("retired_at", rec)
        self.assertEqual(rec["selected"], [0])
        self.assertEqual(rec["answered_at"], "2026-08-28T01:12:59Z")

    def test_an_already_answered_question_is_never_retired(self):
        store = self.read()
        store["questions"]["890ec559"]["answered_at"] = "2026-08-28T00:11:17Z"
        store["questions"]["890ec559"]["selected"] = [0]
        self.write(store)
        api = Recorder()
        res = ta.retire(CONFIG, self.dir, "890ec559", "settled", api=api, now=LATER)
        self.assertFalse(res["retired"])
        self.assertEqual(api.calls, [], "an answered question's message may not even be edited")

    def test_a_tap_on_a_retired_picker_records_nothing_and_mints_no_approval(self):
        """`resolve` returns no `meta` and no `answered`, which is what makes
        `presence._callback_line` structurally unable to fire the approval effect."""
        ta.retire(CONFIG, self.dir, "890ec559",
                  "example/repo #478 merged at 17:24 — nothing to approve.",
                  api=Recorder(), now=LATER)
        api = Recorder()
        res = ta.resolve(CONFIG, self.dir, "q:890ec559:0", "cb-1", api=api, now=LATER)
        rec = self.record("890ec559")
        self.assertTrue(res["retired"])
        self.assertNotIn("meta", res)
        self.assertNotIn("answered", res)
        self.assertIsNone(rec["answered_at"])
        self.assertEqual(rec["selected"], [])
        self.assertIn("merged at 17:24", res["line"])
        self.assertIn("answerCallbackQuery", api.methods(), "a tap is never a silent no-op")

    def test_the_tap_popup_and_line_do_not_blame_the_owner(self):
        ta.retire(CONFIG, self.dir, "890ec559", "#478 merged at 17:24 — nothing to approve.",
                  api=Recorder(), now=LATER)
        api = Recorder()
        res = ta.resolve(CONFIG, self.dir, "q:890ec559:0", "cb-1", api=api, now=LATER)
        popup = api.calls[0][1]["text"].lower()
        for scold in ("too late", "you missed", "should have"):
            self.assertNotIn(scold, popup)
            self.assertNotIn(scold, res["line"].lower())
        self.assertIn("nothing was recorded", popup)


# --------------------------------------------------------------------------- the orphans

class OrphanedRecordTest(StoreCase):
    """Picker messages deleted by hand leave records with no message behind them.

    A retirement pass must handle *the message is already gone* without erroring and **without
    re-sending anything** — a pass that answered a deleted message with a fresh one would turn a
    tidy-up into six new buzzes."""

    #: Six orphaned records, with the PRs they belonged to. Four distinct pull requests.
    ORPHANS = {"205a14de": (472, 10226), "3a02d1f2": (472, 10232), "70ea1174": (474, 10265),
               "b8c79801": (475, 10273), "57d80354": (535, 10610), "5a86c206": (475, 10617)}

    def test_a_deleted_message_settles_the_record_and_sends_nothing(self):
        self.write(store_with(**{"205a14de": question(pr=472, message_id=10226)}))
        gone = TelegramAPIError("Telegram editMessageText failed: Bad Request: message to edit not "
                                "found", method="editMessageText",
                                description="Bad Request: message to edit not found")
        api = Recorder(raises=gone)
        res = ta.retire(CONFIG, self.dir, "205a14de", "settled", api=api, now=LATER)
        self.assertTrue(res["retired"])
        self.assertEqual(self.record("205a14de")["retired_edit"], ta.EDIT_SETTLED)
        self.assertEqual(api.methods(), ["editMessageText"],
                         "one attempted edit and NOTHING else — never a re-send")

    def test_a_message_already_carrying_the_text_settles_too(self):
        """The crash-mid-retirement case: the edit landed, the store write did not. Telegram says
        *message is not modified*, and the next pass finishes the job instead of looping forever."""
        self.write(store_with(**{"57d80354": question(pr=535, message_id=10610)}))
        same = TelegramAPIError("Telegram editMessageText failed: Bad Request: message is not "
                                "modified", method="editMessageText",
                                description="Bad Request: message is not modified")
        res = ta.retire(CONFIG, self.dir, "57d80354", "settled", api=Recorder(raises=same), now=LATER)
        self.assertTrue(res["retired"])

    def test_a_record_with_no_message_id_retires_with_no_api_call_at_all(self):
        self.write(store_with(**{"92d7351f": question(pr=21, message_id=None)}))
        api = Recorder()
        res = ta.retire(CONFIG, self.dir, "92d7351f", "settled", api=api, now=LATER)
        self.assertTrue(res["retired"])
        self.assertEqual(res["edit"], ta.EDIT_NO_MESSAGE)
        self.assertEqual(api.calls, [])

    def test_all_six_reconcile_in_one_pass_with_four_confirmations(self):
        """End to end, at the real cap: six orphaned records across four PRs. Every one settles, no
        message is re-sent, and the `gh` cost is per pull request rather than per picker."""
        self.write(store_with(**{qid: question(pr=pr, message_id=mid)
                                 for qid, (pr, mid) in self.ORPHANS.items()}))
        gone = TelegramAPIError("Bad Request: message to edit not found", method="editMessageText",
                                description="Bad Request: message to edit not found")
        api = Recorder(raises=gone)
        payloads = [self.gh_payload(pr=pr) for pr in (472, 474, 475, 535)]
        runner = self.runner_for(*payloads)
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()}, api=api,
                           runner=runner, config=CONFIG, now=LATER, max_prs=4)
        self.assertEqual(len(report["retired"]), 6)
        self.assertEqual(report["confirms"], 4, "one `gh pr view` per PULL REQUEST, not per picker")
        self.assertEqual(sorted(set(api.methods())), ["editMessageText"], "nothing was re-sent")
        for qid in self.ORPHANS:
            rec = self.record(qid)
            self.assertEqual(rec["retired_edit"], ta.EDIT_SETTLED)
            self.assertIsNone(rec["answered_at"])


# --------------------------------------------------------------------------- the cost

class BoundedCostTest(StoreCase):
    """**No second poller, and no `gh` call per pending question per tick.**"""

    def test_a_pass_with_nothing_to_retire_spends_nothing(self):
        self.write(store_with(**{"890ec559": question()}))
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): {478}},
                           api=Recorder(), config=CONFIG, now=LATER)
        self.assertEqual(report["confirms"], 0)

    def test_no_candidates_means_no_credentials_are_even_loaded(self):
        """The steady state must not so much as open `telegram.env` — `config=None` here, and a
        pass that tried to load one on this host would either find the real file or raise."""
        self.write(store_with(**{"890ec559": question()}))
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): {478}},
                           api=Recorder(), now=LATER)
        self.assertEqual((report["retired"], report["errors"]), ([], []))

    def test_two_pickers_for_one_pr_confirm_once(self):
        self.write(store_with(**{"b8c79801": question(pr=475, message_id=10273),
                                 "5a86c206": question(pr=475, message_id=10617)}))
        runner = self.runner_for(self.gh_payload(pr=475))
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()},
                           api=Recorder(), runner=runner, config=CONFIG, now=LATER)
        self.assertEqual(report["confirms"], 1)
        self.assertEqual(len(report["retired"]), 2)

    def test_the_cap_holds_over_and_never_drops(self):
        self.write(store_with(**{"a": question(pr=472, message_id=1),
                                 "b": question(pr=474, message_id=2),
                                 "c": question(pr=475, message_id=3)}))
        runner = self.runner_for(self.gh_payload(pr=472))
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()},
                           api=Recorder(), runner=runner, config=CONFIG, now=LATER, max_prs=1)
        self.assertEqual(report["confirms"], 1)
        self.assertEqual(len(report["retired"]), 1)
        self.assertEqual(len(report["deferred"]), 2, "held for the next pass, never dropped")

    def test_max_prs_zero_disables_retiring_without_disabling_the_pass(self):
        self.write(store_with(**{"890ec559": question()}))
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()},
                           api=Recorder(), config=CONFIG, now=LATER, max_prs=0)
        self.assertEqual((report["confirms"], report["retired"]), (0, []))
        self.assertEqual(len(report["deferred"]), 1)

    def test_a_dry_run_confirms_but_edits_and_writes_nothing(self):
        self.write(store_with(**{"890ec559": question()}))
        api = Recorder()
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()}, api=api,
                           runner=self.runner_for(self.gh_payload()), config=CONFIG, now=LATER,
                           dry_run=True)
        self.assertEqual(api.calls, [])
        self.assertNotIn("retired_at", self.record("890ec559"))
        self.assertEqual(report["retired"][0]["text"],
                         "example/repo #478 merged at 17:24 — nothing to approve.")


class MovedHeadTest(StoreCase):
    """**§2 — the PR is still open and the head has moved.** An approval is pinned to one exact
    commit, so a rebase or a force-push turns a live picker into a dead question the owner can still
    tap — and on a busy repository that happens several times a day.

    Two properties carry this whole class and the rest of the tests are scaffolding around them:

    * **``test_an_unchanged_head_is_never_retired`` is the invariant.** Every open PR is in the
      index on every tick; a comparison that reads *moved* when nothing moved eats the live question
      of every open PR every pass.
    * **It costs no API call.** Absence from a list is an inference and §1 pays a ``gh pr view`` to
      confirm it. A row that is PRESENT under a different ``headRefOid`` is already the evidence, so
      ``confirms`` stays 0 and the ``runner`` seam is never touched.

    Everything unsure leaves the picker alone — no head map, a ``None`` map, a PR missing from it, an
    unreadable SHA on either side. Each is asserted separately, because they are four different ways
    to reach the same wrong retirement."""

    #: What the branch was rebased onto. Any SHA that is not `HEAD`; spelled full-length because the
    #: comparison is exact and a 12-char fixture would quietly test a prefix rule that does not exist.
    MOVED = "3c1f0a9d4e2b7856ac09d31f4b6e8a72d5c04193"

    def setUp(self):
        super().setUp()
        self.write(store_with(**{"890ec559": question()}))
        self.api = Recorder()
        self.runner = self.runner_for()  # answers nothing; a §2 pass must never reach it

    def moved_sweep(self, heads="auto", numbers=(478,), **kw):
        """A pass where #478 is **open** and the head map says what `heads` says."""
        if heads == "auto":
            heads = {478: self.MOVED}
        return pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set(numbers)},
                         head_index={mg.repo_key(REPO): heads} if heads is not False else {},
                         api=self.api, runner=self.runner, config=CONFIG, now=LATER, **kw)

    def assertUntouched(self, report):
        rec = self.record("890ec559")
        self.assertNotIn("retired_at", rec)
        self.assertEqual(rec["selected"], [])
        self.assertIsNone(rec["answered_at"])
        self.assertEqual(self.api.calls, [], "nothing may go out on a decline")
        self.assertEqual(report["retired"], [])
        self.assertEqual(self.runner.calls, [], "a decline may not cost a gh call either")

    # ----------------------------------------------------------------- it retires

    def test_an_open_pr_whose_head_moved_retires_the_picker(self):
        report = self.moved_sweep()
        self.assertEqual([(r["repo"], r["pr"], r["why"]) for r in report["retired"]],
                         [(REPO, 478, pkr.MOVED_HEAD)])
        self.assertIn("retired_at", self.record("890ec559"))
        self.assertEqual(self.api.methods(), ["editMessageText"])
        self.assertEqual(self.api.calls[0][1]["message_id"], 10470)

    def test_the_moved_head_retirement_spends_no_gh_call(self):
        """The join is `pr_sweep.open_heads` over rows the sweep already fetched. Confirming it
        would spend a call to be told what the list said."""
        report = self.moved_sweep()
        self.assertEqual(report["confirms"], 0)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(len(report["retired"]), 1)

    def test_it_is_not_bounded_by_the_confirmation_cap(self):
        """`max_prs` bounds `gh pr view` calls. This branch makes none, so letting three unrelated
        merged PRs defer a free retirement would be a budget spent on nothing."""
        report = self.moved_sweep(max_prs=0)
        self.assertEqual(len(report["retired"]), 1)
        self.assertEqual(report["deferred"], [])

    def test_the_keyboard_is_stripped_so_the_dead_question_cannot_be_tapped(self):
        self.moved_sweep()
        markup = json.loads(self.api.calls[0][1]["reply_markup"])
        self.assertEqual(markup.get("inline_keyboard"), [])

    def test_the_record_says_retired_and_never_answered(self):
        self.moved_sweep()
        rec = self.record("890ec559")
        self.assertTrue(rec["retired_at"])
        self.assertIsNone(rec["answered_at"], "a retirement is not an answer")
        self.assertEqual(rec["selected"], [], "no choice the owner did not make is ever fabricated")

    def test_a_dry_run_previews_the_line_and_edits_nothing(self):
        report = self.moved_sweep(dry_run=True)
        self.assertEqual(self.api.calls, [])
        self.assertNotIn("retired_at", self.record("890ec559"))
        self.assertIn("no longer the head", report["retired"][0]["text"])

    # ----------------------------------------------------------------- THE INVARIANT

    def test_an_unchanged_head_is_never_retired(self):
        """**The one that must never go green by accident.** Every open PR is in the index on every
        tick, so a comparison that reads "moved" when nothing moved withdraws the live question of
        every open pull request every pass."""
        report = self.moved_sweep(heads={478: HEAD})
        self.assertUntouched(report)
        self.assertEqual(report["candidates"], 0)
        self.assertIn("still open at", report["skipped"][0]["reason"])

    def test_an_unchanged_head_survives_a_pass_that_retires_a_moved_one_beside_it(self):
        """The end-to-end version. A per-function assertion cannot catch a second code path, and a
        pass that is retiring *something* is exactly when an over-broad rule would take the wrong
        picker with it."""
        self.write(store_with(**{"890ec559": question(),
                                 "aa11bb22": question(pr=539, message_id=10480)}))
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): {478, 539}},
                           head_index={mg.repo_key(REPO): {478: self.MOVED, 539: HEAD}},
                           api=self.api, runner=self.runner, config=CONFIG, now=LATER)
        self.assertEqual([r["pr"] for r in report["retired"]], [478])
        after = self.read()["questions"]
        self.assertIn("retired_at", after["890ec559"])
        self.assertNotIn("retired_at", after["aa11bb22"])
        self.assertEqual([p.get("message_id") for _, p in self.api.calls], [10470])

    # ----------------------------------------------------------------- cannot tell ⇒ leave it

    def test_a_repository_with_no_head_map_at_all_is_not_retired(self):
        """A caller that hands over no heads gets exactly the §1 behaviour it had before. A missing
        index may turn into a skip and never into a retirement."""
        self.assertUntouched(self.moved_sweep(heads=False))

    def test_a_head_map_that_could_not_be_read_whole_is_not_retired(self):
        """`open_heads` collapses an unreadable or page-limited list into `None`, exactly as
        `open_numbers` does — and `None` is not a thing a SHA may be compared against."""
        self.assertUntouched(self.moved_sweep(heads=None))

    def test_a_pr_missing_from_the_head_map_is_not_retired(self):
        self.assertUntouched(self.moved_sweep(heads={539: self.MOVED}))

    def test_an_unreadable_head_on_the_row_is_not_retired(self):
        for unreadable in (None, "", "   ", 12345, ["sha"]):
            with self.subTest(head=unreadable):
                self.write(store_with(**{"890ec559": question()}))
                self.api = Recorder()
                self.assertUntouched(self.moved_sweep(heads={478: unreadable}))

    def test_a_picker_that_names_no_commit_is_not_retired(self):
        """Pre-binding records carry no `head_sha`. There is nothing to compare, so there is nothing
        to conclude."""
        for pinned in (None, "", "  "):
            with self.subTest(head_sha=pinned):
                meta = {"kind": mg.APPROVAL_META_KIND, "pr": 478, "repo": REPO,
                        "head_sha": pinned, "approve_index": 0}
                self.write(store_with(**{"890ec559": question(meta=meta)}))
                self.api = Recorder()
                self.assertUntouched(self.moved_sweep())

    def test_an_already_answered_picker_is_not_a_moved_head_candidate(self):
        """§2 may not disturb the paths §1 already settled: `pending_pr_pickers` never yields an
        answered or retired question, so a head move cannot reach one."""
        answered = question()
        answered["answered_at"] = "2026-08-28T00:11:17Z"
        answered["selected"] = ["Approve"]
        retired = question(pr=539, message_id=10480)
        retired["retired_at"] = "2026-08-28T01:30:00Z"
        self.write(store_with(a=answered, b=retired))
        report = self.moved_sweep(numbers=(478, 539),
                                  heads={478: self.MOVED, 539: self.MOVED})
        self.assertEqual(report["pending"], 0)
        self.assertEqual(report["retired"], [])
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.read()["questions"]["a"]["selected"], ["Approve"])

    # ----------------------------------------------------------------- the predicate, directly

    def test_moved_head_is_sure_in_exactly_one_shape(self):
        picker = {"repo": REPO, "pr": 478, "head_sha": HEAD}
        self.assertEqual(pkr.moved_head(picker, {478: self.MOVED}), (self.MOVED, ""))
        for heads in (None, {}, {478: None}, {478: ""}, {478: HEAD}, {479: self.MOVED}, "nope"):
            with self.subTest(heads=heads):
                new, why = pkr.moved_head(picker, heads)
                self.assertIsNone(new)
                self.assertTrue(why, "a decline must say which decline it was")

    def test_the_comparison_is_exact_and_not_a_prefix(self):
        """Both sides are the same `headRefOid` field of the same `gh` schema — `pr_facts` reads it
        on the ask, `pr_sweep.open_heads` on the sweep. A prefix rule would be inventing a tolerance
        for a disagreement that cannot arise, and it would read two different commits sharing twelve
        characters as one."""
        picker = {"repo": REPO, "pr": 478, "head_sha": HEAD}
        twin = HEAD[:12] + ("0" * (len(HEAD) - 12))
        self.assertNotEqual(twin, HEAD)
        self.assertEqual(pkr.moved_head(picker, {478: twin}), (twin, ""))

    # ----------------------------------------------------------------- both classes at once

    def test_a_merged_pr_and_a_moved_head_settle_in_one_pass_with_one_confirmation(self):
        self.write(store_with(**{"890ec559": question(),
                                 "cc33dd44": question(pr=537, message_id=10490)}))
        report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): {478}},
                           head_index={mg.repo_key(REPO): {478: self.MOVED}},
                           api=self.api, runner=self.runner_for(self.gh_payload(pr=537)),
                           config=CONFIG, now=LATER)
        by_pr = {r["pr"]: r for r in report["retired"]}
        self.assertEqual(sorted(by_pr), [478, 537])
        self.assertEqual(by_pr[478]["why"], pkr.MOVED_HEAD)
        self.assertEqual(by_pr[537]["why"], pkr.NOT_OPEN)
        self.assertEqual(report["confirms"], 1, "only the absent PR costs a gh call")
        self.assertIn("merged at 17:24", by_pr[537]["text"])
        self.assertIn("no longer the head", by_pr[478]["text"])

    def test_the_log_line_distinguishes_the_two(self):
        """A §1 retirement ends that PR's story; a §2 one says another picker is coming for the same
        PR. A summary that renders them the same is how a broken watcher looks healthy."""
        line = pkr.summary_line(self.moved_sweep())
        self.assertIn(f"{REPO}#478 (head moved)", line)


class MovedHeadTextTest(unittest.TestCase):
    """**The settled wording, and what it refuses to say.**

    Whether an approval *should* survive a rebase is §7 of `concurrent-pr-collisions`, **an open
    decision that belongs to the owner**. A line on their phone may not pre-empt a decision they
    have not made — not by arguing the SHA binding is right, not by apologising for it, not by
    implying it is a defect being worked around. So the line states the fact and stops, and this class is
    what stops a future edit from adding the sentence."""

    CAND = {"repo": REPO, "pr": 538, "head_sha": HEAD,
            "open_head": "3c1f0a9d4e2b7856ac09d31f4b6e8a72d5c04193"}

    def test_it_names_the_repository_the_pr_and_both_commits(self):
        text = pkr.moved_head_text(self.CAND)
        self.assertIn(f"{REPO} #538", text)
        self.assertIn(HEAD[:12], text)
        self.assertIn("3c1f0a9d4e2b", text)

    def test_it_says_the_pinned_commit_is_no_longer_the_head(self):
        self.assertIn("has moved past the commit this question was pinned to",
                      pkr.moved_head_text(self.CAND))

    def test_it_decides_nothing_about_whether_the_binding_is_right(self):
        text = pkr.moved_head_text(self.CAND).lower()
        for forbidden in ("should have", "would have", "ought", "still counts", "carried over",
                          "carries over", "unfortunately", "sorry", "annoying", "bug", "limitation",
                          "re-approve", "reapprove", "again from scratch"):
            self.assertNotIn(forbidden, text,
                             f"{forbidden!r} takes a position on §7, which is the owner's to take")

    def test_it_never_says_the_owner_was_late_or_missed_anything(self):
        text = pkr.moved_head_text(self.CAND).lower()
        for forbidden in ("you missed", "too late", "expired", "should have tapped", "in time",
                          "no longer valid because you"):
            self.assertNotIn(forbidden, text)

    def test_it_says_what_the_tap_in_front_of_the_owner_is_worth(self):
        self.assertIn("Nothing to approve here", pkr.moved_head_text(self.CAND))

    def test_the_promise_it_makes_is_one_the_ask_log_keeps(self):
        """*"the question comes back when the new head is green"* is not optimism: the ask log is
        keyed `(repo, pr, head_sha)`, so at the new commit `already_asked` is already False."""
        self.assertIn("comes back when the new head is green", pkr.moved_head_text(self.CAND))
        with tempfile.TemporaryDirectory() as d:
            mg.record_ask(d, 538, HEAD, "q1", via="auto-green", repo=REPO)
            self.assertTrue(mg.already_asked(d, 538, HEAD, repo=REPO))
            self.assertFalse(mg.already_asked(d, 538, self.CAND["open_head"], repo=REPO),
                             "the new head is an unasked question with no new mechanism at all")

    def test_it_is_one_line(self):
        self.assertNotIn("\n", pkr.moved_head_text(self.CAND))

    def test_a_candidate_with_no_repository_still_reads(self):
        text = pkr.moved_head_text({**self.CAND, "repo": ""})
        self.assertTrue(text.startswith("#538 "))


class NoSecondPredicateTest(StoreCase):
    """The retirement asks `merge_guard` whether a PR is open. It may never grow its own opinion —
    the retirement and the merge door disagreeing about the same PR is exactly the drift one shared
    predicate prevents.

    **The VERDICT, not the wording.** `settled_text` legitimately branches on `MERGED` to choose a
    verb; what may not exist here is a second answer to *is it open*. So this asserts the absence of
    a re-derived rule in the file AND, behaviourally, that stubbing out `not_open_refusal` to refuse
    nothing stops every retirement dead."""

    def source(self) -> str:
        with open(os.path.join(SCRIPT_DIR, "picker_retire.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_it_re_derives_no_not_open_rule(self):
        body = self.source().split('"""', 2)[-1]  # past the module docstring
        for spelling in ('!= "OPEN"', "!= 'OPEN'", "NOT_OPEN_STATES = ", '== "OPEN"'):
            self.assertNotIn(spelling, body,
                             f"{spelling} here is a second not-open rule; use not_open_refusal")

    def test_the_guards_predicate_is_the_only_thing_that_can_retire(self):
        self.write(store_with(**{"890ec559": question()}))
        with mock.patch.object(mg, "not_open_refusal", return_value=""):
            report = pkr.sweep(state_dir=self.dir, open_index={mg.repo_key(REPO): set()},
                               api=Recorder(), runner=self.runner_for(self.gh_payload()),
                               config=CONFIG, now=LATER)
        self.assertEqual(report["retired"], [])
        self.assertNotIn("retired_at", self.record("890ec559"))

    def test_it_records_no_approval(self):
        self.assertNotIn("record_approval", self.source())

    def test_it_sends_no_message(self):
        for spelling in ("sendMessage", "telegram_send.send_text", "ta.ask("):
            self.assertNotIn(spelling, self.source(),
                             "retirement edits what is already there; it never sends")


if __name__ == "__main__":
    unittest.main(verbosity=2)
