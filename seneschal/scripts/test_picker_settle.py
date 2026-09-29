#!/usr/bin/env python3
"""Tests for **settling a picker without a tap** — `seneschal/docs/picker-state-marking-spec.md` §10.5,
items 2 and 3. Stdlib ``unittest`` only, like the rest of the suite.

`answered_at` is written in exactly one place — `telegram_ask.resolve`, the callback path — so
without these paths **a tap is the only thing that ever settles a record**, and §10 describes what
that costs:

  * §10.3, the twin. A picker whose send failed (`message_id: null`) is followed by a second picker
    for the identical question at the identical head, and the owner answers the second one. The
    gate works. What survives is the failed send: same `(repo, pr, head_sha)`, unanswered, the
    oldest record in the store for days — until the owner is shown it again and decides it a
    second time.
  * §10.4, the conversation. A picker whose question the owner answered in chat — and whose answer
    was acted on minutes later — stays in the pending list for days, until it is decided a second
    time too.

What this file covers, and the polarity of each:

  * **the identity is `merge_guard`'s, not a second one** — `ask_identity` is the `(repo, pr,
    head_sha)` tuple the ask log and the approval record already key on, exposed; a case-folded
    repository matches, and every incomplete tuple is `None` rather than a tolerance;
  * **a twin is settled and a different head SHA is not** — the re-ask-at-a-new-head binding
    (`concurrent-pr-collisions-spec.md`) is what this must never defeat, so the not-a-twin cases get
    more assertions than the twin case does;
  * **a settled record is not an answered one** — `answered_at` stays null and `selected` stays `[]`
    forever, which is what `merge_guard._question_confirms` reads as the floor under every approval;
  * **idempotence** — a twin already settled is not re-settled and never counted twice;
  * **`settle-in-chat` REFUSES without a verbatim quote**, in the function as well as at the CLI, and
    the refusal is a sentence rather than an argparse usage dump;
  * **it is reversible** — `unsettle` hands the question back and the picker still taps; and it
    refuses a `picker_retire` retirement, whose message is already edited away;
  * **nothing in the tree calls it**, in the shape of `test_pending_checks.py`'s no-importer guard.

**No network, ever.** Every Bot API call goes through the injected `api` seam, and the settle paths
are asserted to make none at all — they take no credentials and open no socket, which is what lets
the assistant run them from any checkout.

Run:  python -m unittest seneschal.scripts.test_picker_settle   (or)   python test_picker_settle.py
"""
import inspect
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import merge_guard as mg  # noqa: E402
import telegram_ask as ta  # noqa: E402

REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))

NOW = datetime(2026, 8, 26, 2, 33, 15, tzinfo=timezone.utc)
#: The §10.3 shape: one pull request at one head, asked about twice.
REPO, PR, HEAD = "example/repo", 21, "cec87f7fda73aa11bb22cc33dd44ee55ff667788"
OTHER_REPO = "example/other"
#: `_ask`'s "you did not say" — because `meta=None` is a MEANINGFUL argument to `ask()`: it is the
#: unattributed-picker shape, a record with no `meta` key at all.
UNSET = object()
OPTIONS = [
    {"label": "Approve", "description": "Merge it now — this exact commit, single use."},
    {"label": "Not now", "description": "Leave it open; nothing is recorded and nothing merges."},
]


class FakeApi:
    """`telegram_send.api_call`'s stand-in. Records every call; answers sendMessage with an id."""

    def __init__(self, message_id=9773):
        self.calls = []
        self.message_id = message_id

    def __call__(self, c, method, params, timeout=30):
        self.calls.append((method, params))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": self.message_id}}
        return {"ok": True, "result": True}

    def methods(self):
        return [m for m, _ in self.calls]


def _cfg(chat_id="555"):
    return {"token": "tok", "chat_id": chat_id, "api_base": "https://api.telegram.org", "parse_mode": ""}


def _meta(repo=REPO, pr=PR, head=HEAD, kind=None):
    return {"kind": kind or mg.APPROVAL_META_KIND, "pr": pr, "repo": repo,
            "head_sha": head, "approve_index": 0}


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.api = FakeApi()

    def _ask(self, question="Merge that pull request?", meta=UNSET, api=None, now=NOW):
        """A real picker through the real `ask()`. Hand-building a store record would only assert
        that the fixture was settleable — `test_merge_guard.py` makes the same call for the same
        reason."""
        res = ta.ask(_cfg(), self.dir, question, OPTIONS, api=api or self.api, now=now,
                     meta=_meta() if meta is UNSET else meta)
        return res["question_id"]

    def _store(self):
        return ta.load_store(ta.store_path(self.dir))

    def _rec(self, qid):
        return self._store()["questions"][qid]

    def _tap(self, qid, choice=0):
        return ta.resolve(_cfg(), self.dir, f"q:{qid}:{choice}", "cb1", api=self.api, now=NOW)


# --------------------------------------------------------------------------- 1. the identity

class TheIdentityIsMergeGuardsOwn(unittest.TestCase):
    """`ask_identity` is the tuple `record_ask` already keys the ask log on, exposed — not a second
    definition of sameness beside it."""

    def test_a_whole_meta_yields_the_repo_pr_head_tuple(self):
        self.assertEqual(mg.ask_identity(_meta()), (mg.repo_key(REPO), PR, HEAD))

    def test_the_repository_is_case_folded_the_way_the_ask_log_folds_it(self):
        self.assertEqual(mg.ask_identity(_meta(repo="Example/Repo")), mg.ask_identity(_meta()))

    def test_a_different_head_is_a_different_identity(self):
        self.assertNotEqual(mg.ask_identity(_meta(head="0" * 40)), mg.ask_identity(_meta()))

    def test_a_different_repository_is_a_different_identity(self):
        self.assertNotEqual(mg.ask_identity(_meta(repo=OTHER_REPO)),
                            mg.ask_identity(_meta()))

    def test_every_incomplete_tuple_is_none_rather_than_a_tolerance(self):
        """The opposite polarity from `asks_for`, deliberately: there a missing repository costs at
        worst a duplicate picker, here it would settle a question the owner is still owed."""
        for meta in (None, {}, _meta(kind="held-outbound-approval"), _meta(repo=None),
                     _meta(repo=""), _meta(repo="   "), _meta(head=None), _meta(head=""),
                     _meta(head="   "), _meta(pr=None), _meta(pr="21"), _meta(pr=True)):
            with self.subTest(meta=meta):
                self.assertIsNone(mg.ask_identity(meta))

    def test_it_agrees_with_the_approval_cross_check_it_does_not_replace(self):
        """`_question_confirms` keeps its own per-field ladder — it has to name which field
        disagreed, and it keeps a legacy repo tolerance this tuple deliberately does not. The
        binding that matters is one-directional and this is it: **where `ask_identity` says two
        things are the same question, the floor under every approval agrees.**"""
        tmp = tempfile.mkdtemp()
        api = FakeApi()
        qid = ta.ask(_cfg(), tmp, "Merge that pull request?", OPTIONS, api=api, now=NOW,
                     meta=_meta())["question_id"]
        ta.resolve(_cfg(), tmp, f"q:{qid}:0", "cb1", api=api, now=NOW)
        record = {"question_id": qid}
        want = mg.ask_identity(_meta())
        self.assertEqual(mg._question_confirms(tmp, record, PR, HEAD, repo=REPO), "")
        # ...and where it says they are different, so does it.
        self.assertEqual(want[0], mg.repo_key(REPO))
        self.assertNotEqual("", mg._question_confirms(tmp, record, PR, "0" * 40, repo=REPO))
        self.assertNotEqual("", mg._question_confirms(tmp, record, PR, HEAD,
                                                      repo=OTHER_REPO))


# --------------------------------------------------------------------------- 2. the twin settle

class AnAnswerSettlesTheTwin(Case):
    """§10.5 item 2, driven through the real `ask` + `resolve` rather than a hand-built store."""

    def test_the_stale_twin_settles_on_the_tap_that_answered_its_twin(self):
        """A failed send and a landed, answered picker are the same question at the same commit.
        The answer on one ends both."""
        stale = self._ask()
        live = self._ask()
        res = self._tap(live)
        self.assertTrue(res["answered"])
        self.assertEqual(res["twins_settled"], [stale])
        settled = self._rec(stale)
        self.assertTrue(settled["retired_at"])
        self.assertEqual(settled["retired_by"]["how"], mg.SETTLED_BY_TWIN)
        self.assertEqual(settled["retired_by"]["question_id"], live)
        self.assertEqual(settled["retired_by"]["answered_at"], self._rec(live)["answered_at"])
        self.assertIn(f"#{PR}", settled["retired_reason"])
        self.assertIn(REPO, settled["retired_reason"])

    def test_a_settled_twin_is_never_mistaken_for_an_answered_one(self):
        """The floor under every approval is `answered_at` + `selected`, untouched. A propagated
        settle that wrote either would mint a decision the owner never made."""
        stale, live = self._ask(), self._ask()
        self._tap(live)
        settled = self._rec(stale)
        self.assertIsNone(settled["answered_at"])
        self.assertEqual(settled["selected"], [])
        self.assertEqual(settled["retired_edit"], ta.EDIT_NOT_ATTEMPTED)
        self.assertNotEqual("", mg._question_confirms(
            self.dir, {"question_id": stale}, PR, HEAD, repo=REPO))

    def test_a_record_at_a_different_head_sha_is_a_different_question_and_is_left_alone(self):
        """**The invariant.** A new head is a new question — `concurrent-pr-collisions-spec.md`'s
        re-ask binding — and this must never be the thing that defeats it."""
        other = self._ask(meta=_meta(head="0" * 40))
        live = self._ask()
        res = self._tap(live)
        self.assertNotIn("twins_settled", res)
        self.assertIsNone(self._rec(other).get("retired_at"))

    def test_another_repository_at_the_same_number_is_left_alone(self):
        other = self._ask(meta=_meta(repo=OTHER_REPO))
        self._tap(self._ask())
        self.assertIsNone(self._rec(other).get("retired_at"))

    def test_a_question_of_another_kind_is_out_of_scope_by_shape(self):
        """A positive allow-list on `meta.kind`, `picker_retire`'s rule 1. A held outbound approval
        of another kind is a live decision and may not be settled by anything's inference."""
        digest = self._ask(meta=_meta(kind="held-outbound-approval"))
        ghost = self._ask(meta=None)
        self._tap(self._ask())
        self.assertIsNone(self._rec(digest).get("retired_at"))
        self.assertIsNone(self._rec(ghost).get("retired_at"))

    def test_a_twin_the_owner_also_answered_keeps_its_answer(self):
        both = self._ask()
        live = self._ask()
        self._tap(both, choice=1)
        res = self._tap(live)
        self.assertNotIn("twins_settled", res)
        self.assertIsNotNone(self._rec(both)["answered_at"])
        self.assertIsNone(self._rec(both).get("retired_at"))

    def test_an_already_settled_twin_is_not_re_settled_or_counted_twice(self):
        stale = self._ask()
        first, second = self._ask(), self._ask()
        self.assertEqual(self._tap(first)["twins_settled"], sorted([stale, second]))
        was = self._rec(stale)["retired_at"]
        again = self._tap(second)
        self.assertNotIn("twins_settled", again)
        self.assertEqual(self._rec(stale)["retired_at"], was)
        self.assertEqual(self._rec(stale)["retired_by"]["question_id"], first)

    def test_a_declined_answer_settles_the_twins_too(self):
        """*Answered*, not *approved*. "Not now" is a decision, and being asked it twice is the
        same cost as being asked to approve twice."""
        stale, live = self._ask(), self._ask()
        self.assertEqual(self._tap(live, choice=1)["twins_settled"], [stale])

    def test_a_toggle_settles_nothing(self):
        """Multi-select is not answered until Done, so nothing may propagate off a checkbox."""
        stale = self._ask()
        live = ta.ask(_cfg(), self.dir, "Merge that pull request?", OPTIONS, multi=True,
                      api=self.api, now=NOW, meta=_meta())["question_id"]
        res = self._tap(live)
        self.assertIsNone(res["line"])
        self.assertNotIn("twins_settled", res)
        self.assertIsNone(self._rec(stale).get("retired_at"))

    def test_the_settle_needs_no_extra_wire_call(self):
        """It edits nothing on the owner's phone: a tap on a settled twin still lands on `resolve`'s retired
        branch, which records nothing and says so. `picker_retire` owns the message."""
        self._ask()
        live = self._ask()
        self.api.calls.clear()
        self._tap(live)
        self.assertEqual(sorted(set(self.api.methods())), ["answerCallbackQuery", "editMessageText"])

    def test_a_tap_on_a_settled_twin_records_nothing_and_says_why(self):
        stale, live = self._ask(), self._ask()
        self._tap(live)
        self.api.calls.clear()
        res = self._tap(stale)
        self.assertTrue(res["retired"])
        self.assertNotIn("meta", res)
        self.assertNotIn("answered", res)
        self.assertIn("already answered this question", res["line"])
        self.assertIn(ta.RETIRED_TAP_TAIL, res["line"])

    def test_the_settled_twin_leaves_the_retirement_sweep_and_is_pinned_as_a_residual(self):
        """§10.6.5, asserted rather than left to be discovered. `picker_retire.pending_pr_pickers`
        — which `picker_mark` gates on too — skips any `retired_at`, so the settled twin's message
        is never edited down and never marked 😴. **That is a real change to what the owner sees**: in a
        watched repository the sweep used to tidy that message. It is left this way because
        `resolve` may not put network calls in front of a spinning button, and a tap on the
        survivor lands on the retired branch, which records nothing and says so."""
        import picker_retire as prr
        stale, live = self._ask(), self._ask()
        self.assertEqual(sorted(p["question_id"] for p in prr.pending_pr_pickers(self._store())),
                         sorted([stale, live]))
        self._tap(live)
        self.assertEqual(prr.pending_pr_pickers(self._store()), [])
        self.assertIsNotNone(self._rec(stale)["message_id"])   # the keyboard is still on the phone

    def test_a_broken_identity_lookup_never_costs_the_answer(self):
        """The whole block is wrapped: the answer is on disk before it runs, and a stale duplicate is
        the safe failure."""
        stale, live = self._ask(), self._ask()
        real = mg.twin_settlements
        mg.twin_settlements = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("deliberate"))
        try:
            err = io.StringIO()
            stderr, sys.stderr = sys.stderr, err
            try:
                res = self._tap(live)
            finally:
                sys.stderr = stderr
        finally:
            mg.twin_settlements = real
        self.assertTrue(res["answered"])
        self.assertEqual(res["labels"], ["Approve"])
        self.assertIn("the answer stands", err.getvalue())
        self.assertIsNone(self._rec(stale).get("retired_at"))

    def test_the_settlement_list_is_pure_and_writes_nothing(self):
        """`twin_settlements` is a store dict in, arguments out — the write is `telegram_ask`'s, in
        one save, so the twin case is atomic rather than N chances to crash half-way."""
        stale, live = self._ask(), self._ask()
        store = self._store()
        before = json.dumps(store, sort_keys=True)
        rows = mg.twin_settlements(store, live)
        self.assertEqual(rows, [])                 # not answered yet — only an ANSWER propagates
        store["questions"][live]["answered_at"] = "2026-08-26T02:33:15Z"
        rows = mg.twin_settlements(store, live)
        self.assertEqual([r["question_id"] for r in rows], [stale])
        store["questions"][live]["answered_at"] = None
        self.assertEqual(json.dumps(store, sort_keys=True), before)


# --------------------------------------------------------------------------- 3. settle-in-chat

class AnsweredInConversation(Case):
    """§10.5 item 3, in the EXPLICIT form. The automatic version is the one §10.5 calls *"by far
    the most dangerous of the three"* and it is not built."""

    QUOTE = "Merge it."

    def test_the_function_refuses_without_a_quote_and_the_refusal_is_a_real_sentence(self):
        qid = self._ask()
        for empty in ("", None, "   ", "\n\t "):
            with self.subTest(quote=empty):
                with self.assertRaises(ValueError) as ctx:
                    ta.settle_in_chat(self.dir, qid, empty)
                self.assertIn("verbatim", str(ctx.exception))
                self.assertIsNone(self._rec(qid).get("retired_at"))

    def test_the_refusal_is_in_the_function_so_a_future_caller_cannot_bypass_it(self):
        """`ask`'s citation gate makes the same move for the same reason: a check that lives in
        `_cmd_*` is a check an in-process caller routes around without noticing."""
        src = ta.settle_in_chat.__code__
        self.assertIn("ValueError", str(src.co_names) + str(src.co_consts))

    def test_the_cli_refuses_with_a_non_zero_exit_and_a_sentence_not_a_usage_dump(self):
        qid = self._ask()
        out = io.StringIO()
        with redirect_stdout(out):
            code = ta.main(["--state-dir", self.dir, "settle-in-chat", "--question-id", qid])
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue().strip())
        self.assertFalse(payload["ok"])
        self.assertIn("verbatim", payload["error"])
        self.assertIn("--quote", payload["error"])

    def test_a_settled_in_chat_record_is_distinguishable_from_a_tapped_one_by_a_field(self):
        chatted, tapped = self._ask(), self._ask(meta=_meta(head="0" * 40))
        ta.settle_in_chat(self.dir, chatted, self.QUOTE, now=NOW)
        self._tap(tapped)
        a, b = self._rec(chatted), self._rec(tapped)
        self.assertEqual(a["retired_by"]["how"], ta.SETTLED_BY_IN_CHAT)
        self.assertEqual(a["retired_by"]["by"], ta.IN_CHAT_SETTLER)
        self.assertEqual(a["retired_by"]["quote"], self.QUOTE)
        self.assertIsNone(a["answered_at"])
        self.assertEqual(a["selected"], [])
        self.assertIsNotNone(b["answered_at"])
        self.assertNotIn("retired_by", b)

    def test_the_settler_is_the_assistant_and_never_the_owner(self):
        """The owner gave the answer; the assistant made the claim that they gave it, and a record
        that cannot tell those apart is the inference version wearing a coat."""
        qid = self._ask()
        ta.settle_in_chat(self.dir, qid, self.QUOTE, now=NOW)
        self.assertEqual(self._rec(qid)["retired_by"]["by"], ta.IN_CHAT_SETTLER)
        self.assertEqual(ta.IN_CHAT_SETTLER, "assistant")

    def test_the_full_quote_is_kept_verbatim_even_when_the_popup_line_is_clipped(self):
        qid = self._ask()
        words = "I already told you — " + ("merge it, " * 40) + "yes."
        ta.settle_in_chat(self.dir, qid, words, now=NOW)
        rec = self._rec(qid)
        self.assertEqual(rec["retired_by"]["quote"], words)
        self.assertLess(len(rec["retired_reason"]), len(words))
        self.assertIn(words[:40], rec["retired_reason"])

    def test_it_refuses_a_question_the_owner_actually_answered(self):
        qid = self._ask()
        self._tap(qid)
        res = ta.settle_in_chat(self.dir, qid, self.QUOTE, now=NOW)
        self.assertFalse(res["settled"])
        self.assertIn("nothing to settle", res["reason"])
        self.assertIsNone(self._rec(qid).get("retired_at"))

    def test_it_settles_no_second_record_and_infers_nothing(self):
        """One question id in, one record settled. It has no notion of sameness at all — that is
        item 2's job and it runs on an ANSWER, never on a claim about a conversation."""
        one, two = self._ask(), self._ask()
        ta.settle_in_chat(self.dir, one, self.QUOTE, now=NOW)
        self.assertIsNone(self._rec(two).get("retired_at"))

    def test_it_settles_the_class_no_allow_list_reaches(self):
        """§10.4's class is a general `assistant_ask` picker — answered in conversation, acted on,
        and still pending days later. Every other mechanism in this file is out of scope for it **by
        shape** and none of them is wrong to be. This one narrows on nothing, which is the point of
        it."""
        qid = self._ask("Which list should the new item go to?",
                        meta={"kind": "assistant_ask", "topic": "routing"})
        res = ta.settle_in_chat(self.dir, qid, "Put it in both.", now=NOW)
        self.assertTrue(res["settled"])
        self.assertEqual(self._rec(qid)["retired_by"]["how"], ta.SETTLED_BY_IN_CHAT)
        # ...and the twin settle would never have touched it: it is not a merge question.
        self.assertIsNone(mg.ask_identity(self._rec(qid)["meta"]))

    def test_it_touches_no_socket_and_needs_no_credentials(self):
        qid = self._ask()
        self.api.calls.clear()
        ta.settle_in_chat(self.dir, qid, self.QUOTE, now=NOW)
        self.assertEqual(self.api.calls, [])

    def test_list_still_surfaces_it_and_says_how_it_was_settled(self):
        qid = self._ask(now=None)
        ta.settle_in_chat(self.dir, qid, self.QUOTE)
        out = io.StringIO()
        with redirect_stdout(out):
            ta.main(["--state-dir", self.dir, "list"])
        rows = {r["id"]: r for r in json.loads(out.getvalue().strip())["pending"]}
        self.assertIn(qid, rows)
        self.assertEqual(rows[qid]["retired_by"]["how"], ta.SETTLED_BY_IN_CHAT)
        self.assertEqual(rows[qid]["retired_by"]["quote"], self.QUOTE)
        self.assertIsNone(rows[qid]["answered_at"])


class ItIsReversible(Case):
    """The assistant claiming it heard an answer is a claim, and the recovery from a wrong one has to be
    cheaper than the claim — otherwise the safe move is never to settle anything."""

    def test_unsettle_hands_the_question_back_and_the_picker_still_taps(self):
        qid = self._ask()
        ta.settle_in_chat(self.dir, qid, "Merge it.", now=NOW)
        res = ta.unsettle(self.dir, qid, now=NOW)
        self.assertTrue(res["unsettled"])
        rec = self._rec(qid)
        for key in ("retired_at", "retired_reason", "retired_edit", "retired_by"):
            self.assertNotIn(key, rec)
        tap = self._tap(qid)
        self.assertTrue(tap["answered"])
        self.assertEqual(tap["labels"], ["Approve"])

    def test_the_undone_settle_stays_on_the_record_as_history(self):
        qid = self._ask()
        ta.settle_in_chat(self.dir, qid, "Merge it.", now=NOW)
        ta.unsettle(self.dir, qid, now=NOW)
        history = self._rec(qid)["unsettled"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["was"]["retired_by"]["quote"], "Merge it.")

    def test_a_twin_settle_is_reversible_too(self):
        stale, live = self._ask(), self._ask()
        self._tap(live)
        self.assertTrue(ta.unsettle(self.dir, stale, now=NOW)["unsettled"])
        self.assertIsNone(self._rec(stale).get("retired_at"))

    def test_it_refuses_a_retirement_whose_message_was_edited_away(self):
        """`picker_retire` edits the message down to one settled line with no keyboard. Undoing the
        record would leave a question that reads live in `list` with nothing on the phone to tap."""
        qid = self._ask()
        ta.retire(_cfg(), self.dir, qid, "example/repo #21 merged — nothing to approve.",
                  api=self.api, now=NOW)
        self.assertEqual(self._rec(qid)["retired_edit"], ta.EDIT_LANDED)
        res = ta.unsettle(self.dir, qid, now=NOW)
        self.assertFalse(res["ok"])
        self.assertIn("Ask again", res["reason"])
        self.assertTrue(self._rec(qid)["retired_at"])

    def test_unsettling_something_that_is_not_settled_changes_nothing(self):
        qid = self._ask()
        res = ta.unsettle(self.dir, qid, now=NOW)
        self.assertFalse(res["unsettled"])
        self.assertIn("nothing to undo", res["reason"])


# --------------------------------------------------------------------------- 4. the guards

class NothingCallsTheInChatSettleAutomatically(unittest.TestCase):
    """**It is deliberately manual and this is what keeps it that way.** §10.5 item 3 calls the
    automatic version the most dangerous of the three: a background mechanism that concludes *the
    owner answered this in chat* can withdraw a live decision they were never shown, and `picker_retire`'s
    standing rule is that a wrongly-retired live question is far worse than a stale one.

    Built in the shape of `test_pending_checks.py`'s no-importer guard — **a scan for a string, not
    for an import**, because the danger is a `subprocess.run([... "settle-in-chat" ...])` that no
    import graph would show. It reads executable files only: a `.md` naming the verb is the assistant being
    told the verb exists, which is the feature, and a prompt is not a program."""

    #: Everything that could run unattended: the daemon and its tasks, the cockpit's server, the
    #: scheduled PowerShell, CI. Deliberately not `.md`.
    CODE_SUFFIXES = (".py", ".ps1", ".sh", ".yml", ".yaml")
    SKIP_DIRS = {".git", "node_modules", "dist", "build", ".venv", "venv", "__pycache__",
                 "state", "out", "scaffolds", ".pytest_cache", "coverage"}
    #: The module that DEFINES the verb, and the tests that drive it.
    ALLOWED = {"telegram_ask.py"}
    NAMES = ("settle-in-chat", "settle_in_chat")

    def test_no_executable_file_in_the_tree_invokes_it(self):
        callers = []
        for root, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
            for name in files:
                if not name.endswith(self.CODE_SUFFIXES):
                    continue
                if name in self.ALLOWED or name.startswith("test_") or name.startswith("test-"):
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        body = fh.read()
                except OSError:
                    continue
                if any(n in body for n in self.NAMES):
                    callers.append(os.path.relpath(path, REPO_ROOT).replace("\\", "/"))
        self.assertEqual(sorted(callers), [],
                         f"settle-in-chat is deliberately manual; {callers} invokes it")

    def test_the_scan_can_actually_find_a_caller(self):
        """A guard that cannot fail is not a guard. `telegram_ask.py` is excluded by name because it
        defines the verb — so the scan is proved against the file it is excluding."""
        with open(os.path.join(SCRIPT_DIR, "telegram_ask.py"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertTrue(any(n in body for n in self.NAMES))


class ATapIsStillTheOnlyThingThatWritesAnsweredAt(unittest.TestCase):
    """The property §10.4 names — *"`answered_at` is written in exactly one place"* — is the thing
    both new paths were built to work AROUND rather than to relax. If a settle ever starts writing
    it, every count of how many decisions the owner actually made becomes a count of how many the
    assistant decided they had made."""

    def test_answered_at_is_assigned_in_exactly_one_place(self):
        with open(os.path.join(SCRIPT_DIR, "telegram_ask.py"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertEqual(body.count('["answered_at"] = '), 1)

    def test_no_settle_path_assigns_it(self):
        """**Asserted on the assignment, not on the name.** Every one of these functions has to
        *read* `answered_at` — refusing a record the owner already answered is the first thing each of
        them does — and each names it in the docstring sentence promising not to write it. A
        name-level scan would go red on the promise, which is `test_picker_mark.py`'s lesson one
        module over."""
        for fn in (ta.mark_settled, ta.settle, ta.settle_in_chat, ta.unsettle):
            with self.subTest(fn=fn.__name__):
                self.assertNotIn('["answered_at"] = ', inspect.getsource(fn))

    def test_the_two_gating_paths_still_READ_it(self):
        """The other half, and the one that would rot silently: refusing a record the owner has already
        answered is the first thing both of them do, and a settle that stopped checking would
        overwrite a real tap with a claim about one."""
        for fn in (ta.mark_settled, ta.settle):
            with self.subTest(fn=fn.__name__):
                body = inspect.getsource(fn).split('"""')[-1]
                self.assertIn('answered_at', body)


if __name__ == "__main__":
    unittest.main()
