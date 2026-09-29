#!/usr/bin/env python3
"""Tests for the Telegram QUESTION PICKER (seneschal/docs/telegram-inbound-spec.md §6b).
Stdlib ``unittest`` only, like the rest of the suite.

What this feature exists for: a picker is one tap; a prose list is a writing assignment, and a
half-answered question set is worse than an unasked one. Claude Code has `AskUserQuestion`; Telegram
has nothing, so without this every decision the assistant needs over Telegram arrives as prose.

What's covered:
  * `callback_query` is actually requested — an unnamed update type is one Telegram never sends, and
    without it an inline keyboard renders and every tap on it spins forever;
  * extraction: `callback_id` is mandatory (there is nothing to answer without it), the allowlist
    gates it like every other kind, and a mixed batch still advances the offset;
  * **the picker rule enforced as a shape, not a comment** — an option with no description is
    refused, and the first option is marked `(Recommended)` unless that is explicitly turned off;
  * `callback_data` carries an id and an index, never the option text, and stays under 64 bytes;
  * durability: the record is written BEFORE the send, so a crash between the two costs the message
    edit and never the answer;
  * single-select answers, multi-select toggling + Done, and the fact that a toggle wakes nobody;
  * every degrade path — expired, unknown id, unreadable payload, an option that no longer exists —
    calls `answerCallbackQuery` and hands the warm session an honest line rather than vanishing;
  * expiry at `QUESTION_TTL_DAYS`, its tombstone, and the lazy sweep that needs no scheduled task;
  * **provenance** (`../docs/ask-provenance-spec.md` phase 1) — the author is stamped from the
    environment with nothing typed, is recorded as unknown rather than guessed when the environment is
    silent, rides the tombstone, and **never costs a send**: a picker that does not arrive is a
    decision the owner never gets to make, so every failure path in the stamp still sends;
  * **the read door and the completeness shape** — `list` names the author on every row (and carries
    the object verbatim behind `--origin`), a record from before the stamp renders as an older record
    rather than an unknown author, and `origin.stamped` makes `source` countable separately from
    `stamped_by`, which describes `session_id` alone;
  * **the Mouth row** (spec phase 2) - a landed picker appends one `kind: "question"` assertion
    carrying the wording the owner received, the question id and phase 1's author; it is written only
    AFTER the send lands, never on a dry run, never on a send that raised, and **a broken Mouth costs
    the row and never the picker**;
  * the GRID picker (one drop-down-like row per item) and its seven-choice wrap.

The daemon-side wiring (the callback path in `presence.py` that shells out to `resolve`, and the
end-to-end inbound task) is not covered here: that wiring lands with the daemon port.

**No network, ever.** `telegram_ask`'s Bot API calls go through an injected `api` seam that every test
here supplies; nothing falls back to the real `api_call`. `get_updates` is mocked at its call site. A
test that posts to a real bot token is a bug.

Run:  python -m unittest test_telegram_questions   (from seneschal/scripts)
"""
import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import mouth  # noqa: E402
import telegram_ask as ta  # noqa: E402
import telegram_poll as tp  # noqa: E402

# A worked example: a decision that would otherwise be answered in free text, which is the friction
# this feature removes.
QUESTION = "Which base should the picker branch cut from?"
OPTIONS = [
    {"label": "origin/develop", "description": "The integration branch; the fix is already in it."},
    {"label": "master", "description": "The release point — a PR here would target the wrong branch."},
]
NOW = datetime(2026, 8, 11, 20, 16, tzinfo=timezone.utc)


class FakeApi:
    """Stands in for `telegram_send.api_call`. Records every call; answers sendMessage with an id."""

    def __init__(self, message_id=4242, fail=()):
        self.calls = []
        self.message_id = message_id
        self.fail = set(fail)

    def __call__(self, c, method, params, timeout=30):
        self.calls.append((method, params))
        if method in self.fail:
            raise RuntimeError(f"Telegram {method} failed: deliberate test failure")
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": self.message_id}}
        return {"ok": True, "result": True}

    def methods(self):
        return [m for m, _ in self.calls]

    def params(self, method):
        return [p for m, p in self.calls if m == method]


def _cfg(chat_id="555"):
    return {"token": "tok", "chat_id": chat_id, "api_base": "https://api.telegram.org", "parse_mode": ""}


class AllowedUpdates(unittest.TestCase):
    def test_callback_query_is_requested(self):
        """Off by default in the Bot API. Without it the keyboard renders and no tap can ever arrive —
        the same class of silent gap as an edit type nobody asked for."""
        seen = {}

        class FakeResp:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s):
                return b'{"ok":true,"result":[]}'

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url if hasattr(req, "full_url") else req
            return FakeResp()

        with mock.patch.object(tp.urllib.request, "urlopen", fake_urlopen):
            tp.get_updates("tok", "https://api.telegram.org", None, 0, 100)
        self.assertIn("callback_query", seen["url"])

    def test_the_other_three_kinds_are_still_requested(self):
        """Naming a subset is an implicit 'and nothing else', so adding one type must not drop three."""
        seen = {}

        class FakeResp:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s):
                return b'{"ok":true,"result":[]}'

        with mock.patch.object(tp.urllib.request, "urlopen",
                               lambda req, timeout=None: (seen.update(url=req.full_url), FakeResp())[1]):
            tp.get_updates("tok", "https://api.telegram.org", None, 0, 100)
        for kind in ("message", "edited_message", "message_reaction", "callback_query"):
            self.assertIn(kind, seen["url"])


def _cb_update(update_id=10, data="q:abcd1234:0", cb_id="cb1", chat_id=555, message_id=4242, **over):
    cb = {"id": cb_id, "from": {"username": "owner"}, "data": data,
          "message": {"message_id": message_id, "date": 1_000_000,
                      "chat": {"id": chat_id, "type": "private"}}}
    cb.update(over)
    return {"update_id": update_id, "callback_query": cb}


class CallbackExtraction(unittest.TestCase):
    def test_a_tap_becomes_an_inbound_item(self):
        item = tp.extract_callback(_cb_update(), set())
        self.assertEqual(item["kind"], "callback")
        self.assertEqual(item["callback_id"], "cb1")
        self.assertEqual(item["data"], "q:abcd1234:0")
        self.assertEqual(item["message_id"], 4242)
        self.assertEqual(item["chat_id"], "555")
        self.assertEqual(item["text"], "")

    def test_a_query_with_no_id_is_dropped(self):
        """The id is the whole obligation: with no `answerCallbackQuery` to make, there is nothing to
        do with the tap and nothing we could tell the owner's client."""
        self.assertIsNone(tp.extract_callback(_cb_update(cb_id=""), set()))

    def test_the_allowlist_gates_it(self):
        self.assertIsNone(tp.extract_callback(_cb_update(chat_id=999), {"555"}))
        self.assertIsNotNone(tp.extract_callback(_cb_update(chat_id=555), {"555"}))

    def test_a_malformed_query_contributes_nothing(self):
        for bad in ({"callback_query": None}, {"callback_query": []}, {"callback_query": {}}, {}):
            self.assertIsNone(tp.extract_callback(bad, set()))

    def test_a_query_with_no_message_still_parses_without_an_allowlist(self):
        """Telegram omits `message` for an inline-mode callback. We never make one, but the extractor
        must not raise on a shape a future Bot API hands it."""
        item = tp.extract_callback({"update_id": 1, "callback_query": {"id": "x", "data": "q:a:0"}}, set())
        self.assertEqual(item["kind"], "callback")
        self.assertIsNone(item["message_id"])


class OffsetAccounting(unittest.TestCase):
    """A regression here is a message-loss bug, not a cosmetic one."""

    def _run(self, updates):
        out = {}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(tp, "get_updates", lambda *a, **k: updates), \
                mock.patch.object(tp, "resolve_reactions", lambda *a, **k: None), \
                mock.patch.object(sys, "argv", ["telegram_poll.py", "--offset-file",
                                                os.path.join(d, "off")]), \
                mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}, clear=False):
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                tp.main()
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
        return out

    def test_a_callback_advances_the_offset(self):
        res = self._run([_cb_update(update_id=77)])
        self.assertEqual(res["next_offset"], 78)
        self.assertEqual(res["count"], 1)

    def test_a_mixed_batch_advances_past_all_of_it(self):
        updates = [
            {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 555, "type": "private"},
                                         "from": {"username": "owner"}, "text": "hi", "date": 1}},
            {"update_id": 2, "edited_message": {"message_id": 1, "chat": {"id": 555, "type": "private"},
                                                "from": {"username": "owner"}, "text": "hi!",
                                                "date": 1, "edit_date": 2}},
            _cb_update(update_id=3),
        ]
        res = self._run(updates)
        self.assertEqual(res["next_offset"], 4)
        self.assertEqual([m["kind"] for m in res["messages"]], ["message", "edit", "callback"])


class ThePickerRuleIsTheShape(unittest.TestCase):
    """The picker rule has three clauses and two of them are things a caller can simply forget. They
    are enforced here rather than written down and hoped for."""

    def test_an_option_without_a_description_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            ta.parse_option("Just a label")
        self.assertIn("description", str(cm.exception))

    def test_an_empty_description_is_refused_too(self):
        with self.assertRaises(ValueError):
            ta.parse_option("Label|   ")

    def test_a_description_may_contain_pipes(self):
        opt = ta.parse_option("Label|costs a|b tradeoff")
        self.assertEqual(opt["description"], "costs a|b tradeoff")

    def test_the_first_option_is_marked_recommended(self):
        body = ta.render_body(QUESTION, OPTIONS, multi=False, recommend=True)
        first, second = body.index("origin/develop"), body.index("2. master")
        self.assertLess(first, second)                     # recommendation FIRST
        self.assertIn(f"1. origin/develop  {ta.RECOMMENDED_MARK}", body)
        self.assertNotIn(f"master  {ta.RECOMMENDED_MARK}", body)

    def test_no_recommendation_marks_nothing(self):
        body = ta.render_body(QUESTION, OPTIONS, multi=False, recommend=False)
        self.assertNotIn(ta.RECOMMENDED_MARK, body)

    def test_every_description_reaches_the_body(self):
        """The button label is a phone-width string that truncates silently, so the body is where the
        'what it means and what it costs' half of the rule has to live."""
        body = ta.render_body(QUESTION, OPTIONS, multi=False, recommend=True)
        for opt in OPTIONS:
            self.assertIn(opt["description"], body)

    def test_multi_select_says_how_to_finish(self):
        self.assertIn("Tap all that apply, then Done.",
                      ta.render_body(QUESTION, OPTIONS, multi=True, recommend=True))
        self.assertIn("Tap one.", ta.render_body(QUESTION, OPTIONS, multi=False, recommend=True))

    def test_a_one_option_picker_is_refused(self):
        args = argparse.Namespace(question=QUESTION, option=["Only|one"], multi=False,
                                  no_recommendation=False, chat_id="1", dry_run=True,
                                  state_dir=".", env_file=None)
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ta._cmd_ask(args)
        self.assertEqual(rc, 2)
        self.assertIn("2-10", json.loads(buf.getvalue())["error"])


class CallbackDataBudget(unittest.TestCase):
    def test_it_carries_an_id_not_the_option_text(self):
        kb = ta.build_keyboard("abcd1234", OPTIONS)
        datas = [row[0]["callback_data"] for row in kb["inline_keyboard"]]
        self.assertEqual(datas, ["q:abcd1234:0", "q:abcd1234:1"])
        for label in ("origin/develop", "master"):
            self.assertFalse(any(label in d for d in datas))

    def test_it_stays_under_the_64_byte_cap(self):
        for row in ta.build_keyboard("abcd1234", OPTIONS, multi=True)["inline_keyboard"]:
            self.assertLessEqual(len(row[0]["callback_data"].encode("utf-8")), ta.CALLBACK_DATA_LIMIT)

    def test_an_over_long_payload_raises_rather_than_shipping_a_dead_button(self):
        with self.assertRaises(ValueError):
            ta.callback_data("x" * 80, 0)

    def test_a_long_label_is_truncated_on_the_button_only(self):
        long_opt = [{"label": "a" * 200, "description": "d"}, OPTIONS[1]]
        kb = ta.build_keyboard("abcd1234", long_opt)
        self.assertLessEqual(len(kb["inline_keyboard"][0][0]["text"]), ta.BUTTON_LABEL_CHARS)
        self.assertIn("a" * 200, ta.render_body(QUESTION, long_opt, False, True))

    def test_parse_round_trips_and_rejects_junk(self):
        self.assertEqual(ta.parse_callback_data("q:abcd1234:2"), ("abcd1234", 2))
        self.assertEqual(ta.parse_callback_data("q:abcd1234:done"), ("abcd1234", "done"))
        for bad in ("", "nonsense", "q:abcd1234", "q::0", "x:abcd1234:0", "q:abcd1234:zzz"):
            self.assertEqual(ta.parse_callback_data(bad), (None, None))

    def test_multi_adds_a_done_row_and_checkboxes(self):
        kb = ta.build_keyboard("abcd1234", OPTIONS, multi=True, selected=[1])
        self.assertEqual(kb["inline_keyboard"][-1][0]["callback_data"], "q:abcd1234:done")
        self.assertTrue(kb["inline_keyboard"][0][0]["text"].startswith(ta.UNCHECKED))
        self.assertTrue(kb["inline_keyboard"][1][0]["text"].startswith(ta.CHECKED))


class AskWritesTheRecordFirst(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _store(self):
        return ta.load_store(ta.store_path(self.dir))

    def test_a_question_is_sent_with_a_keyboard_and_recorded(self):
        api = FakeApi()
        res = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=api, now=NOW)
        self.assertEqual(api.methods(), ["sendMessage"])
        kb = json.loads(api.params("sendMessage")[0]["reply_markup"])
        self.assertEqual(len(kb["inline_keyboard"]), 2)
        rec = self._store()["questions"][res["question_id"]]
        self.assertEqual(rec["question"], QUESTION)
        self.assertEqual(rec["message_id"], 4242)
        self.assertIsNone(rec["answered_at"])

    def test_the_record_survives_a_send_that_never_returns(self):
        """Persisted BEFORE the send, deliberately: `callback_data` carries the question id, so the
        record is what makes a tap resolvable. A crash between the two costs the message edit only."""
        api = FakeApi(fail={"sendMessage"})
        with self.assertRaises(RuntimeError):
            ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=api, now=NOW)
        questions = self._store()["questions"]
        self.assertEqual(len(questions), 1)
        self.assertIsNone(next(iter(questions.values()))["message_id"])

    def test_dry_run_touches_neither_the_wire_nor_the_store(self):
        api = FakeApi()
        res = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, dry_run=True, api=api, now=NOW)
        self.assertTrue(res["dry_run"])
        self.assertEqual(api.calls, [])
        self.assertFalse(os.path.exists(ta.store_path(self.dir)))

    def test_two_questions_are_two_messages(self):
        """One question per message — N questions = N messages, no wizard."""
        api = FakeApi()
        a = ta.ask(_cfg(), self.dir, "First?", OPTIONS, api=api, now=NOW)
        b = ta.ask(_cfg(), self.dir, "Second?", OPTIONS, api=api, now=NOW)
        self.assertNotEqual(a["question_id"], b["question_id"])
        self.assertEqual(api.methods(), ["sendMessage", "sendMessage"])
        self.assertEqual(len(self._store()["questions"]), 2)


class SingleSelect(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.api = FakeApi()
        self.qid = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=self.api, now=NOW)["question_id"]
        self.api.calls.clear()

    def _tap(self, choice, **kw):
        return ta.resolve(_cfg(), self.dir, f"q:{self.qid}:{choice}", "cb1", api=self.api, now=NOW, **kw)

    def test_a_tap_answers_and_routes_back_as_ordinary_inbound(self):
        res = self._tap(0)
        self.assertTrue(res["answered"])
        self.assertEqual(res["labels"], ["origin/develop"])
        self.assertEqual(res["line"], f'[the owner answered "{QUESTION}" → "origin/develop"]')

    def test_the_query_is_always_answered_before_anything_cosmetic(self):
        """Telegram spins the client's button until `answerCallbackQuery` lands."""
        self._tap(1)
        self.assertEqual(self.api.methods()[0], "answerCallbackQuery")
        self.assertIn("editMessageText", self.api.methods())

    def test_the_choice_is_folded_into_the_message_and_the_keyboard_goes(self):
        self._tap(1)
        params = self.api.params("editMessageText")[0]
        self.assertIn("✓ master", params["text"])
        self.assertIn(QUESTION, params["text"])
        self.assertNotIn("reply_markup", params)  # a dead keyboard in the scrollback is the bug

    def test_a_failed_edit_never_costs_the_answer(self):
        self.api.fail.add("editMessageText")
        res = self._tap(0)
        self.assertTrue(res["answered"])
        self.assertFalse(res["message_edited"])
        self.assertIn("origin/develop", res["line"])

    def test_a_re_tap_says_so_and_adds_no_second_inbound(self):
        self._tap(0)
        self.api.calls.clear()
        res = self._tap(1)
        self.assertTrue(res["already_answered"])
        self.assertIsNone(res["line"])            # nothing new to say...
        self.assertEqual(self.api.methods(), ["answerCallbackQuery"])   # ...but the owner still gets a popup
        self.assertIn("origin/develop", self.api.params("answerCallbackQuery")[0]["text"])

    def test_an_option_index_off_the_end_degrades_honestly(self):
        res = self._tap(99)
        self.assertIn("ask them what they picked", res["line"])
        self.assertEqual(self.api.methods(), ["answerCallbackQuery"])
        self.assertTrue(self.api.params("answerCallbackQuery")[0]["show_alert"])

    def test_the_answer_persists_across_a_reload(self):
        self._tap(0)
        store = ta.load_store(ta.store_path(self.dir))
        self.assertEqual(store["questions"][self.qid]["selected"], [0])
        self.assertIsNotNone(store["questions"][self.qid]["answered_at"])


class MultiSelect(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.api = FakeApi()
        self.qid = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, multi=True,
                          api=self.api, now=NOW)["question_id"]
        self.api.calls.clear()

    def _tap(self, choice):
        return ta.resolve(_cfg(), self.dir, f"q:{self.qid}:{choice}", "cb1", api=self.api, now=NOW)

    def test_a_toggle_wakes_nobody(self):
        """A half-made choice must never read as the final one — and the assistant isn't woken per checkbox."""
        res = self._tap(0)
        self.assertIsNone(res["line"])
        self.assertEqual(res["selected"], [0])
        self.assertIn("editMessageReplyMarkup", self.api.methods())

    def test_toggling_twice_deselects(self):
        self._tap(1)
        self.assertEqual(self._tap(1)["selected"], [])

    def test_done_delivers_every_selection_as_one_inbound(self):
        self._tap(0)
        self._tap(1)
        res = self._tap("done")
        self.assertTrue(res["answered"])
        self.assertEqual(res["labels"], ["origin/develop", "master"])
        self.assertEqual(res["line"], f'[the owner answered "{QUESTION}" → "origin/develop", "master"]')

    def test_done_with_nothing_selected_is_said_plainly(self):
        res = self._tap("done")
        self.assertTrue(res["answered"])
        self.assertIn("nothing", res["line"])
        self.assertIn("✓ (nothing selected)", self.api.params("editMessageText")[0]["text"])

    def test_the_checkboxes_follow_the_selection(self):
        self._tap(0)
        kb = json.loads(self.api.params("editMessageReplyMarkup")[-1]["reply_markup"])
        self.assertTrue(kb["inline_keyboard"][0][0]["text"].startswith(ta.CHECKED))
        self.assertTrue(kb["inline_keyboard"][1][0]["text"].startswith(ta.UNCHECKED))

    def test_a_toggle_survives_a_reload(self):
        self._tap(0)
        self.assertEqual(ta.load_store(ta.store_path(self.dir))["questions"][self.qid]["selected"], [0])


class ATapIsNeverASilentNoOp(unittest.TestCase):
    """Requirement 4 of the brief. Every one of these is a tap the daemon cannot honour, and every one
    of them still answers the query AND hands the warm session something to act on."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.api = FakeApi()

    def test_a_question_the_daemon_never_knew(self):
        res = ta.resolve(_cfg(), self.dir, "q:deadbeef:0", "cb1", api=self.api, now=NOW)
        self.assertTrue(res["expired"])
        self.assertIn("no longer have a record of", res["line"])
        self.assertEqual(self.api.methods(), ["answerCallbackQuery"])
        self.assertTrue(self.api.params("answerCallbackQuery")[0]["show_alert"])

    def test_an_expired_question_is_named_by_its_tombstone(self):
        old = NOW - timedelta(days=ta.QUESTION_TTL_DAYS + 1)
        qid = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=self.api, now=old)["question_id"]
        self.api.calls.clear()
        res = ta.resolve(_cfg(), self.dir, f"q:{qid}:0", "cb1", api=self.api, now=NOW)
        self.assertTrue(res["expired"])
        self.assertIn(QUESTION, res["line"])           # WHICH question expired, not a shrug
        self.assertIn(QUESTION[:40], self.api.params("answerCallbackQuery")[0]["text"])

    def test_an_unreadable_payload(self):
        res = ta.resolve(_cfg(), self.dir, "not-our-button", "cb1", api=self.api, now=NOW)
        self.assertIn("couldn't read", res["line"])
        self.assertEqual(self.api.methods(), ["answerCallbackQuery"])

    def test_a_corrupt_store_does_not_raise(self):
        with open(ta.store_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        res = ta.resolve(_cfg(), self.dir, "q:abcd1234:0", "cb1", api=self.api, now=NOW)
        self.assertTrue(res["ok"])
        self.assertIn("no longer have a record of", res["line"])

    def test_a_failed_answer_call_never_raises_into_the_answer(self):
        """The toast is cosmetic; the answer is already on disk by the time it is attempted."""
        qid = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=self.api, now=NOW)["question_id"]
        self.api.fail.add("answerCallbackQuery")
        res = ta.resolve(_cfg(), self.dir, f"q:{qid}:0", "cb1", api=self.api, now=NOW)
        self.assertTrue(res["answered"])
        self.assertEqual(ta.load_store(ta.store_path(self.dir))["questions"][qid]["selected"], [0])


class Expiry(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.api = FakeApi()

    def test_a_question_inside_the_window_is_untouched(self):
        qid = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=self.api,
                     now=NOW - timedelta(days=ta.QUESTION_TTL_DAYS - 1))["question_id"]
        store = ta.load_store(ta.store_path(self.dir))
        self.assertEqual(ta.prune_store(store, NOW), 0)
        self.assertIn(qid, store["questions"])

    def test_the_sweep_is_lazy_so_nothing_has_to_schedule_it(self):
        """`dream_steps.py` exists because a step nothing runs reads as a step that ran. Every read and
        every write of this store prunes, so there is no owner to forget."""
        ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=self.api,
               now=NOW - timedelta(days=ta.QUESTION_TTL_DAYS + 1))
        # A brand-new, unrelated question is the only thing that happens next — and it sweeps.
        ta.ask(_cfg(), self.dir, "Something else?", OPTIONS, api=self.api, now=NOW)
        store = ta.load_store(ta.store_path(self.dir))
        self.assertEqual(len(store["questions"]), 1)
        self.assertEqual(store["expired"][-1]["question"], QUESTION)

    def test_an_undateable_record_is_retired_rather_than_kept_forever(self):
        store = {"schema": ta.SCHEMA, "questions": {"x": {"question": "?", "asked_at": "garbage"}},
                 "expired": []}
        self.assertEqual(ta.prune_store(store, NOW), 1)
        self.assertEqual(store["questions"], {})

    def test_tombstones_are_capped(self):
        store = {"schema": ta.SCHEMA, "questions": {}, "expired": [
            {"id": str(i), "question": f"q{i}", "expired_at": "x"} for i in range(ta.TOMBSTONE_CAP + 25)]}
        ta.prune_store(store, NOW)
        self.assertEqual(len(store["expired"]), ta.TOMBSTONE_CAP)
        self.assertEqual(store["expired"][-1]["id"], str(ta.TOMBSTONE_CAP + 24))  # newest kept

    def test_a_tombstone_keeps_the_text_and_drops_the_options(self):
        """`origin` rides with the text — the tombstone is what survives the week, so it is where
        "who asked this?" is hardest and matters most. The OPTIONS are still dropped."""
        ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=self.api,
               now=NOW - timedelta(days=ta.QUESTION_TTL_DAYS + 1))
        store = ta.load_store(ta.store_path(self.dir))
        ta.prune_store(store, NOW)
        self.assertEqual(set(store["expired"][-1]), {"id", "question", "expired_at", "origin"})
        self.assertNotIn("options", store["expired"][-1])


class AskProvenance(unittest.TestCase):
    """`docs/ask-provenance-spec.md` phase 1. A picker sent from a headless background surface (a
    Watch comms peek) is the one whose author is hardest to recover afterwards — and that process
    already carries `SENESCHAL_SESSION_SOURCE=watch` in its own environment.

    So the property under test is **auto**, not *available*: a field a caller must remember to
    populate is not a mechanism (`jobs.py` makes the same argument)."""

    PEEK_ENV = {"CLAUDE_CODE_SESSION_ID": "00000000-0000-0000-0000-000000000194",
                "SENESCHAL_SESSION_SOURCE": "watch"}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.api = FakeApi()

    def _ask(self, env, **kw):
        with mock.patch.dict(os.environ, env, clear=True):
            return ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=self.api, now=NOW, **kw)

    def _record(self, res):
        return ta.load_store(ta.store_path(self.dir))["questions"][res["question_id"]]

    # --- the stamp fires with nothing typed ---------------------------------------------------

    def test_the_environment_is_stamped_onto_the_record(self):
        rec = self._record(self._ask(self.PEEK_ENV))
        self.assertEqual(rec["origin"]["session_id"], self.PEEK_ENV["CLAUDE_CODE_SESSION_ID"])
        self.assertEqual(rec["origin"]["source"], "watch")
        self.assertEqual(rec["origin"]["stamped_by"], "env")

    def test_a_peek_shaped_environment_names_the_peek(self):
        """A peek, as a shape: the source variable is the only thing that has to be set."""
        origin = self._ask({"SENESCHAL_SESSION_SOURCE": "watch"})["origin"]
        self.assertEqual(origin["source"], "watch")
        self.assertNotIn("session_id", origin)          # absent, never invented
        self.assertEqual(origin["stamped_by"], "none")  # ...and the record says which

    def test_the_daemon_and_scheduled_surfaces_are_distinguishable(self):
        for source in ("daemon", "scheduled", "watch"):
            with self.subTest(source=source):
                self.assertEqual(self._ask({"SENESCHAL_SESSION_SOURCE": source})["origin"]["source"],
                                 source)

    def test_the_process_is_always_named(self):
        origin = self._ask(self.PEEK_ENV)["origin"]
        self.assertEqual(origin["cwd"], os.getcwd())
        self.assertEqual(origin["pid"], os.getpid())

    def test_nothing_had_to_be_typed(self):
        """The whole point. No flag, no `--meta`, no cooperation from the model writing the argv."""
        rec = self._record(self._ask(self.PEEK_ENV))
        self.assertNotIn("meta", rec)
        self.assertEqual(rec["origin"]["stamped_by"], "env")

    # --- unknown is RECORDED, not guessed -----------------------------------------------------

    def test_an_absent_environment_records_unknown(self):
        origin = self._ask({})["origin"]
        self.assertEqual(origin["stamped_by"], "none")
        self.assertNotIn("session_id", origin)
        self.assertNotIn("source", origin)

    def test_an_empty_environment_variable_is_the_same_as_an_absent_one(self):
        origin = self._ask({"CLAUDE_CODE_SESSION_ID": "   ", "SENESCHAL_SESSION_SOURCE": ""})["origin"]
        self.assertEqual(origin["stamped_by"], "none")
        self.assertNotIn("session_id", origin)
        self.assertNotIn("source", origin)

    def test_stamped_by_separates_found_nothing_from_never_ran(self):
        """`origin` is written unconditionally, so a missing key means the code is old — not that the
        environment was empty. That is the distinction a census of this store has to be able to make."""
        for env, expected in (({}, "none"), (self.PEEK_ENV, "env")):
            with self.subTest(expected=expected):
                rec = self._record(self._ask(env))
                self.assertIn("origin", rec)
                self.assertEqual(rec["origin"]["stamped_by"], expected)

    # --- the flag is an override, never the mechanism -------------------------------------------

    def test_an_explicit_session_wins_and_says_so(self):
        origin = self._ask(self.PEEK_ENV, origin_session="hand-typed")["origin"]
        self.assertEqual(origin["session_id"], "hand-typed")
        self.assertEqual(origin["stamped_by"], "flag")

    def test_an_empty_flag_falls_back_to_the_environment(self):
        origin = self._ask(self.PEEK_ENV, origin_session="  ")["origin"]
        self.assertEqual(origin["session_id"], self.PEEK_ENV["CLAUDE_CODE_SESSION_ID"])
        self.assertEqual(origin["stamped_by"], "env")

    def test_the_cli_flag_reaches_the_record(self):
        import io as _io
        argv = ["telegram_ask.py", "--state-dir", self.dir, "ask", "--question", QUESTION,
                "--option", "A|because a", "--option", "B|because b",
                "--origin-session", "from-argv", "--dry-run"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(sys, "stdout", _io.StringIO()) as out:
            self.assertEqual(ta.main(), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["origin"]["session_id"], "from-argv")
        self.assertEqual(payload["origin"]["stamped_by"], "flag")

    def test_a_dry_run_is_inspectable_without_sending(self):
        res = self._ask(self.PEEK_ENV, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["origin"]["source"], "watch")
        self.assertEqual(self.api.calls, [])
        self.assertFalse(os.path.exists(ta.store_path(self.dir)))

    # --- fail open: the stamp may cost itself and nothing else -----------------------------------

    def test_an_ask_still_sends_when_every_provenance_source_is_missing(self):
        """**The rule that outranks the field.** A picker that does not arrive is far worse than one
        with a blank author — being unable to ask is strictly worse than asking without a name."""
        res = self._ask({})
        self.assertTrue(res["sent"])
        self.assertEqual(self.api.methods(), ["sendMessage"])
        keyboard = json.loads(self.api.params("sendMessage")[0]["reply_markup"])
        self.assertEqual(len(keyboard["inline_keyboard"]), 2)

    def test_a_raising_environment_costs_the_field_and_not_the_send(self):
        class Hostile(dict):
            def get(self, *a, **k):
                raise RuntimeError("this mapping is having a bad day")

        with mock.patch.object(ta.os, "environ", Hostile()):
            res = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=self.api, now=NOW)
        self.assertTrue(res["sent"])
        self.assertEqual(res["origin"]["stamped_by"], "none")

    def test_an_unreadable_cwd_costs_only_the_cwd(self):
        with mock.patch.object(ta.os, "getcwd", side_effect=OSError("cwd is gone")):
            origin = self._ask(self.PEEK_ENV)["origin"]
        self.assertNotIn("cwd", origin)
        self.assertEqual(origin["session_id"], self.PEEK_ENV["CLAUDE_CODE_SESSION_ID"])

    def test_stamp_origin_never_raises(self):
        with mock.patch.object(ta, "build_origin", side_effect=RuntimeError("boom")):
            self.assertEqual(ta.stamp_origin(), {"stamped": {}, "stamped_by": "none"})

    # --- old records still read -----------------------------------------------------------------

    def test_a_record_written_before_this_change_still_reads(self):
        """Pre-stamp records are deliberately NOT migrated: nothing in one says who sent it, so a
        migration would guess, and a wrong guess is worse than a blank. New rows carry the field, old
        rows do not, and **reading tolerates its absence** — including on a tap."""
        legacy = {"question": QUESTION, "body": ta.render_body(QUESTION, OPTIONS, False, True),
                  "options": OPTIONS, "multi": False, "recommended": True, "chat_id": "555",
                  "message_id": 9919, "asked_at": ta._stamp(NOW), "selected": [], "answered_at": None}
        ta.save_store(ta.store_path(self.dir),
                      {"schema": ta.SCHEMA, "questions": {"43d1b1c2": dict(legacy)}, "expired": []})
        res = ta.resolve(_cfg(), self.dir, "q:43d1b1c2:0", "cb1", api=self.api, now=NOW)
        self.assertTrue(res["answered"])
        self.assertEqual(res["labels"], ["origin/develop"])
        self.assertIsNone(ta.record_origin(legacy))

    def test_an_unstamped_record_expires_without_an_invented_author(self):
        old = ta._stamp(NOW - timedelta(days=ta.QUESTION_TTL_DAYS + 1))
        store = {"schema": ta.SCHEMA, "expired": [],
                 "questions": {"43d1b1c2": {"question": QUESTION, "asked_at": old}}}
        self.assertEqual(ta.prune_store(store, NOW), 1)
        self.assertNotIn("origin", store["expired"][-1])

    def test_a_stamped_record_carries_its_author_onto_the_tombstone(self):
        with mock.patch.dict(os.environ, self.PEEK_ENV, clear=True):
            ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=self.api,
                   now=NOW - timedelta(days=ta.QUESTION_TTL_DAYS + 1))
        store = ta.load_store(ta.store_path(self.dir))
        ta.prune_store(store, NOW)
        self.assertEqual(store["expired"][-1]["origin"]["source"], "watch")

    def test_a_garbled_origin_reads_as_absent_rather_than_raising(self):
        for junk in ("watch", [], {}, None, 7):
            with self.subTest(junk=junk):
                self.assertIsNone(ta.record_origin({"origin": junk}))

    # --- the picker is otherwise byte-identical ---------------------------------------------------

    def test_the_stamp_changes_nothing_about_what_goes_out(self):
        """Provenance is a RECORDED FIELD AND NOTHING ELSE. The body, the keyboard and the option
        order are what the owner actually sees, and none of them may move."""
        stamped = self._ask(self.PEEK_ENV, dry_run=True)
        bare = self._ask({}, dry_run=True)
        self.assertEqual(stamped["body"], bare["body"])
        self.assertEqual(stamped["buttons"], bare["buttons"])
        self.assertEqual(stamped["body"], ta.render_body(QUESTION, OPTIONS, False, True))

    def test_origin_is_not_meta_and_neither_displaces_the_other(self):
        """`--meta` stays opaque and uninterpreted; `origin` is a separate, non-omittable key beside
        it. Conflating them is how provenance became optional in the first place."""
        rec = self._record(self._ask(self.PEEK_ENV, meta={"kind": "merge-approval", "pr": 406}))
        self.assertEqual(rec["meta"], {"kind": "merge-approval", "pr": 406})
        self.assertEqual(rec["origin"]["source"], "watch")

    def test_a_tap_still_hands_back_meta_and_not_origin(self):
        """The resolve path is untouched: `meta` rides back out on the answered path exactly as
        before, and `origin` does not join it — a tap dispatches on caller data, never on the author."""
        res = self._ask(self.PEEK_ENV, meta={"kind": "merge-approval"})
        qid = res["question_id"]
        out = ta.resolve(_cfg(), self.dir, "q:" + qid + ":0", "cb1", api=self.api, now=NOW)
        self.assertEqual(out["meta"], {"kind": "merge-approval"})
        self.assertNotIn("origin", out)


class ProvenanceCompleteness(unittest.TestCase):
    """`origin.stamped` — WHICH parts of the author were captured.

    `stamped_by` is inherited from `jobs.py`, where it describes `session_id` and nothing else.
    Carried over verbatim it produces a record reading `stamped_by: "none"` while carrying
    `source: "watch"` — **the unattributed-picker shape**, if the CLI ever stops exporting
    `CLAUDE_CODE_SESSION_ID`. A census keyed on `stamped_by` alone would count that record as
    unattributed and never learn that the sending surface *was* on the record. So the question here
    is not "did the stamp fire" but "is `source` separately countable"."""

    def _origin(self, env, **kw):
        with mock.patch.dict(os.environ, env, clear=True):
            return ta.build_origin(**kw)

    def test_a_peek_shaped_environment_records_both_parts_and_their_doors(self):
        origin = self._origin(AskProvenance.PEEK_ENV)
        self.assertEqual(origin["stamped"], {"session_id": "env", "source": "env"})
        self.assertEqual(origin["stamped_by"], "env")

    def test_a_bare_environment_records_that_it_captured_nothing(self):
        """`{}` is an ANSWER — "the stamp ran and found nothing" — not a missing field."""
        origin = self._origin({})
        self.assertEqual(origin["stamped"], {})
        self.assertEqual(origin["stamped_by"], "none")

    def test_the_mixed_case_is_the_whole_point(self):
        """Source present, session absent. `stamped_by` says `none` — correctly, about `session_id`
        — and `stamped` is what stops a census reading that as an unattributed ask."""
        origin = self._origin({"SENESCHAL_SESSION_SOURCE": "watch"})
        self.assertEqual(origin["stamped_by"], "none")
        self.assertEqual(origin["stamped"], {"source": "env"})
        self.assertEqual(origin["stamped"].get("source"), "env")  # ...separately countable

    def test_the_flag_and_the_environment_are_distinguishable_per_part(self):
        origin = self._origin({"SENESCHAL_SESSION_SOURCE": "daemon"}, session_id="hand-typed")
        self.assertEqual(origin["stamped"], {"session_id": "flag", "source": "env"})

    def test_stamped_by_still_means_exactly_what_it_meant(self):
        """**Not silently redefined.** Every already-written record keeps its meaning; the new field
        is a superset beside it, never a reinterpretation of the old one."""
        for env, expected in (({}, "none"),
                              ({"SENESCHAL_SESSION_SOURCE": "watch"}, "none"),
                              (AskProvenance.PEEK_ENV, "env")):
            with self.subTest(env=sorted(env)):
                self.assertEqual(self._origin(env)["stamped_by"], expected)
        self.assertEqual(self._origin({}, session_id="s")["stamped_by"], "flag")

    def test_the_new_shape_is_detectable_rather_than_assumed(self):
        """A reader can tell a post-change record from a pre-change one, so it never has to guess."""
        self.assertIn("stamped", self._origin({}))
        self.assertIsNotNone(ta.origin_stamped({"origin": self._origin({})}))
        self.assertIsNone(ta.origin_stamped({"origin": {"source": "watch", "stamped_by": "none"}}))

    def test_a_pre_stamped_record_is_never_back_filled_from_stamped_by(self):
        """`stamped_by` would recover `session_id`'s door and say nothing about `source`, so filling
        the rest in would invent the one fact this field exists to make countable."""
        legacy = {"origin": {"session_id": "s", "source": "watch", "stamped_by": "env"}}
        self.assertIsNone(ta.origin_stamped(legacy))
        self.assertIsNone(ta.origin_stamped({}))
        self.assertIsNone(ta.origin_stamped({"origin": {"stamped": "watch"}}))

    def test_it_still_fails_open(self):
        """The polarity that outranks every field here: suppressing a question is the only harm this
        path can do. A stamp that cannot complete costs itself and nothing else."""
        with mock.patch.object(ta, "build_origin", side_effect=RuntimeError("boom")):
            self.assertEqual(ta.stamp_origin(), {"stamped": {}, "stamped_by": "none"})
        api = FakeApi()
        with mock.patch.dict(os.environ, {}, clear=True):
            res = ta.ask(_cfg(), tempfile.mkdtemp(), QUESTION, OPTIONS, api=api, now=NOW)
        self.assertTrue(res["sent"])
        self.assertEqual(res["origin"]["stamped"], {})


class TheReadDoorNamesTheAuthor(unittest.TestCase):
    """`list` is the sanctioned read-only way into the question store. A provenance field `list` did
    not show would still mean opening the JSON by hand to answer *"who asked this?"*. The field being
    present is not the fix; the field being READABLE is."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _store(self, questions, expired=()):
        ta.save_store(ta.store_path(self.dir), {"schema": ta.SCHEMA, "questions": questions,
                                                "expired": list(expired)})

    def _record(self, origin=None, **kw):
        rec = {"question": QUESTION, "options": OPTIONS, "multi": False, "recommended": True,
               "chat_id": "555", "message_id": 1, "asked_at": ta._stamp(), "selected": [],
               "answered_at": None}
        if origin is not None:
            rec["origin"] = origin
        rec.update(kw)
        return rec

    def _list(self, *flags):
        import io as _io
        argv = ["telegram_ask.py", "--state-dir", self.dir, "list", *flags]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(sys, "stdout", _io.StringIO()) as out:
            self.assertEqual(ta.main(), 0)
        return json.loads(out.getvalue())

    # --- what a row says now --------------------------------------------------------------------

    def test_a_fully_stamped_record_names_its_sender_and_its_session(self):
        self._store({"43d1b1c2": self._record(
            {"session_id": "00000000-0000-0000-0000-000000000194", "source": "watch",
             "cwd": "/x", "pid": 7, "stamped": {"session_id": "env", "source": "env"},
             "stamped_by": "env"})})
        self.assertEqual(self._list()["pending"][0]["author"], "watch(env) · 00000000(env)")

    def test_a_partial_origin_says_what_it_has_and_claims_nothing_else(self):
        """The mixed case again, from the reading end: a surface with no session id reads as the
        surface, NOT as unattributed — which is what `stamped_by` alone would have shown."""
        self._store({"a1": self._record({"source": "watch", "cwd": "/x", "pid": 7,
                                         "stamped": {"source": "env"}, "stamped_by": "none"}),
                     "b2": self._record({"session_id": "9c3fd962-aaaa", "cwd": "/x", "pid": 7,
                                         "stamped": {"session_id": "flag"}, "stamped_by": "flag"})})
        rows = {r["id"]: r["author"] for r in self._list()["pending"]}
        self.assertEqual(rows["a1"], "watch(env)")
        self.assertEqual(rows["b2"], "9c3fd962(flag)")

    def test_an_origin_that_captured_nothing_reads_as_unattributed(self):
        self._store({"a1": self._record({"cwd": "/x", "pid": 7, "stamped": {},
                                         "stamped_by": "none"})})
        self.assertEqual(self._list()["pending"][0]["author"], ta.NO_AUTHOR_LABEL)

    def test_a_phase_one_record_renders_without_inventing_the_doors(self):
        """Written after `origin` shipped and before `origin.stamped` did. Both parts are known; how
        each was obtained is not, so no door is printed rather than a plausible one."""
        self._store({"a1": self._record({"session_id": "00000000-0c8c", "source": "watch",
                                         "stamped_by": "env"})})
        self.assertEqual(self._list()["pending"][0]["author"], "watch · 00000000")

    # --- the record that predates all of it -----------------------------------------------------

    def test_a_pre_provenance_record_neither_crashes_nor_lies(self):
        """**An absent stamp is not an unknown author — it is an older record.** Pre-stamp records
        are deliberately not migrated, so rendering them as "unknown" would make the store's own
        history read as a provenance failure rate."""
        self._store({"43d1b1c2": self._record()})
        row = self._list()["pending"][0]
        self.assertEqual(row["author"], ta.NO_STAMP_LABEL)
        self.assertNotIn("unknown", row["author"].lower())
        self.assertNotEqual(row["author"], ta.NO_AUTHOR_LABEL)  # a DIFFERENT fact from stamp-ran

    def test_a_garbled_origin_reads_as_a_pre_provenance_record_rather_than_raising(self):
        for junk in ("watch", [], {}, None, 7):
            with self.subTest(junk=junk):
                self._store({"a1": self._record(junk)})
                self.assertEqual(self._list()["pending"][0]["author"], ta.NO_STAMP_LABEL)

    def test_the_rest_of_the_row_is_untouched(self):
        """`author` is additive. Anything already parsing this output keeps its keys and its shape."""
        self._store({"a1": self._record()})
        row = self._list()["pending"][0]
        self.assertEqual({k: row[k] for k in ("id", "question", "multi", "answered_at", "selected")},
                         {"id": "a1", "question": QUESTION, "multi": False, "answered_at": None,
                          "selected": []})
        self.assertNotIn("origin", row)  # the full object is opt-in

    # --- the full object, behind the flag -------------------------------------------------------

    def test_the_origin_flag_carries_the_object_verbatim(self):
        origin = {"session_id": "00000000-0c8c", "source": "watch", "cwd": "/x", "pid": 7,
                  "stamped": {"session_id": "env", "source": "env"}, "stamped_by": "env"}
        self._store({"a1": self._record(origin)})
        row = self._list("--origin")["pending"][0]
        self.assertEqual(row["origin"], origin)
        self.assertEqual(row["author"], "watch(env) · 00000000(env)")

    def test_the_origin_flag_reports_a_pre_provenance_record_as_null_not_as_empty(self):
        """`null` means *the record has no origin*; `{}` would mean *it has one and it is blank*, a
        thing no record carries. Distinguishable in the machine output as well as the human cell."""
        self._store({"a1": self._record()})
        self.assertIsNone(self._list("--origin")["pending"][0]["origin"])

    # --- the tombstone, where it matters most ----------------------------------------------------

    def test_an_expired_ask_still_names_its_author(self):
        """A week on the options are gone by design and the author is the only thing left worth
        having — `docs/ask-provenance-spec.md` §4.2's reason for putting `origin` on the tombstone."""
        self._store({}, expired=[{"id": "43d1b1c2", "question": QUESTION, "expired_at": ta._stamp(),
                                  "origin": {"source": "watch", "stamped": {"source": "env"},
                                             "stamped_by": "none"}},
                                 {"id": "20bcea35", "question": QUESTION,
                                  "expired_at": ta._stamp()}])
        stones = {e["id"]: e["author"] for e in self._list("--all")["expired"]}
        self.assertEqual(stones["43d1b1c2"], "watch(env)")
        self.assertEqual(stones["20bcea35"], ta.NO_STAMP_LABEL)

    # --- end to end, against a record this module actually wrote ---------------------------------

    def test_an_ask_is_readable_by_list_without_opening_the_json(self):
        """The property the fix is for: ask, then read back the sanctioned way, and get the sender's
        name."""
        api = FakeApi()
        env = {ta.SOURCE_ENV_VAR: "watch", "CLAUDE_CODE_SESSION_ID": "00000000-0000-0000-0000-000000000194"}
        with mock.patch.dict(os.environ, env, clear=True):
            ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=api)
        self.assertEqual(self._list()["pending"][0]["author"], "watch(env) · 00000000(env)")


class ALandedPickerReachesTheMouth(unittest.TestCase):
    """Spec phase 2. `state/assertions.jsonl` is the append-only record of what the assistant has
    actually said to the owner, and a question is something it said. These tests are about the row
    existing and about it never costing the send."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _rows(self):
        return [r for r in mouth.read_assertions(self.dir) if r.get("kind") == "question"]

    def test_a_landed_picker_writes_exactly_one_question_row(self):
        api = FakeApi()
        res = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=api, now=NOW)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item_ids"], [res["question_id"]])
        self.assertEqual(rows[0]["surface"], "telegram")
        self.assertTrue(rows[0]["delivered"])

    def test_the_row_carries_the_wording_the_owner_received_not_just_the_question(self):
        """The options are the promises. A row holding only the question string would drop every
        offer the picker made - and the question store self-liquidates in 7 days into a tombstone that
        keeps no wording at all, so this row is what is left."""
        ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=FakeApi(), now=NOW)
        text = self._rows()[0]["text"]
        self.assertIn(QUESTION, text)
        for opt in OPTIONS:
            self.assertIn(opt["description"], text)
        self.assertIn(ta.RECOMMENDED_MARK, text)

    def test_the_row_names_the_author_the_stamp_found(self):
        env = {ta.SOURCE_ENV_VAR: "watch", "CLAUDE_CODE_SESSION_ID": "00000000-0000-0000-0000-000000000194"}
        with mock.patch.dict(os.environ, env, clear=True):
            ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=FakeApi(), now=NOW)
        hand = self._rows()[0]["origin_hand"]
        self.assertEqual(hand["speaker"], ta.ASSERTION_SPEAKER)
        self.assertEqual(hand["source"], "watch")
        self.assertEqual(hand["session_id"], "00000000-0000-0000-0000-000000000194")
        # `source` here is $SENESCHAL_SESSION_SOURCE, NOT the session registry `mouth.origin_hand`
        # reads. Same key, two different facts - and a headless peek has no registry entry at all, so
        # the registry answer for it would be silence. The row says which door it
        # came through rather than leaving it to a reader who has no way to tell.
        self.assertEqual(hand["source_from"], ta.SOURCE_ENV_VAR)

    def test_a_dry_run_says_nothing_so_it_records_nothing(self):
        ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, dry_run=True, api=FakeApi(), now=NOW)
        self.assertEqual(self._rows(), [])

    def test_a_send_that_raised_records_nothing(self):
        """Invariant 2 of the Mouth: *a row means it landed*. The durable question record is still
        written (that is the crash-between-the-two design), but the Mouth must not claim the owner
        was told something Telegram may never have shown them."""
        with self.assertRaises(RuntimeError):
            ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=FakeApi(fail={"sendMessage"}), now=NOW)
        self.assertEqual(self._rows(), [])

    def test_a_broken_mouth_costs_the_row_and_never_the_picker(self):
        """The polarity the whole spec turns on: suppressing a question is the only harm this path can
        do. `record_assertion` never raises by contract - this proves the call site does not undo that
        if the contract is ever broken from underneath it."""
        api = FakeApi()
        with mock.patch.object(mouth, "record_assertion", side_effect=OSError("disk full")), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            res = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=api, now=NOW)
        self.assertTrue(res["sent"])
        self.assertEqual(api.methods(), ["sendMessage"])
        self.assertIn("the picker WAS sent", err.getvalue())
        self.assertEqual(self._rows(), [])

    def test_question_is_a_declared_kind(self):
        """`mouth.KINDS` is advisory - an unknown value is recorded verbatim rather than rejected - so
        this is not about validation. It is about a reader of that tuple knowing pickers are in here."""
        self.assertIn(ta.ASSERTION_KIND, mouth.KINDS)


def _list_rows(state_dir):
    """`telegram_ask.py list` through its own entry point, parsed. The read door is where a human
    counts these, so it is the door the test uses rather than reaching into the store."""
    buf = io.StringIO()
    with mock.patch("sys.stdout", buf):
        ta._cmd_list(argparse.Namespace(state_dir=state_dir, all=False, origin=False))
    return json.loads(buf.getvalue())["pending"]


GRID_ITEMS = [{"id": "loop-a", "label": "Add a collapse toggle to the Jobs panel"},
              {"id": "loop-b", "label": "Fix the flaky sweep test"}]
GRID_CHOICES = ["Owner", "Assistant", "Both", "External", "Unknown"]
_SENTINEL = object()


class GridPicker(unittest.TestCase):
    """`--grid` mode — one picker with one drop-down per item, each offering the same choices (the
    OWI unknown-owner batches). Telegram has no dropdowns, so this is a grid of buttons: one row per item, one button per choice, a tap marks/replaces that row's
    pick, Done submits a `{item_id: choice}` map, and an untouched row submits as unchanged — never a
    guessed answer.

    Every `resolve()` call below passes `now=self.now` (same fixed `NOW` the classic suite uses):
    without it, `prune_store`'s lazy sweep dates itself against the REAL wall clock, which is well
    past `QUESTION_TTL_DAYS` from the fixture's `asked_at` and expires the record before the tap ever
    reaches it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.now = NOW

    def tearDown(self):
        self._tmp.cleanup()

    def _send(self, api=None, now=_SENTINEL, **over):
        api = api or FakeApi()
        if now is _SENTINEL:
            now = self.now
        return ta.ask_grid(_cfg(), self.dir, "2 items need an owner", GRID_ITEMS, GRID_CHOICES,
                           api=api, now=now, **over), api

    def _tap(self, qid, row, choice, cb_id, api):
        return ta.resolve(_cfg(), self.dir, ta.grid_callback_data(qid, row, choice), cb_id, api=api,
                          now=self.now)

    def test_parse_grid_item_needs_no_description(self):
        """Unlike `parse_option`, a grid row is data (a work item's own text), not a recommendation —
        the "every option needs a real description" rule doesn't apply."""
        item = ta.parse_grid_item("loop-a|Add a toggle")
        self.assertEqual(item, {"id": "loop-a", "label": "Add a toggle"})

    def test_parse_grid_item_refuses_no_id(self):
        with self.assertRaises(ValueError):
            ta.parse_grid_item("|Add a toggle")

    def test_grid_callback_data_round_trips(self):
        data = ta.grid_callback_data("abcd1234", 2, 3)
        self.assertEqual(data, "q:abcd1234:g2c3")
        self.assertEqual(ta.parse_grid_callback_data(data), ("abcd1234", 2, 3))

    def test_grid_callback_data_done_round_trips(self):
        data = ta.grid_callback_data("abcd1234", None, "done")
        self.assertEqual(data, "q:abcd1234:gdone")
        self.assertEqual(ta.parse_grid_callback_data(data), ("abcd1234", None, "done"))

    def test_classic_and_grid_payloads_never_collide(self):
        """A classic tap's third segment is a bare int or the literal `"done"`; neither ever starts
        with `"g"`, so the two parsers can never both claim the same string."""
        self.assertEqual(ta.parse_grid_callback_data("q:abcd1234:0"), (None, None, None))
        self.assertEqual(ta.parse_callback_data("q:abcd1234:g0c1"), (None, None))

    def test_build_grid_keyboard_shape(self):
        kb = ta.build_grid_keyboard("qid", GRID_ITEMS, GRID_CHOICES, {})
        self.assertEqual(len(kb["inline_keyboard"]), 3)  # 2 item rows + Done
        self.assertEqual(len(kb["inline_keyboard"][0]), len(GRID_CHOICES))
        self.assertEqual(kb["inline_keyboard"][-1][0]["text"], ta.DONE_LABEL)

    def test_build_grid_keyboard_marks_the_current_pick(self):
        kb = ta.build_grid_keyboard("qid", GRID_ITEMS, GRID_CHOICES, {"0": 2})
        self.assertTrue(kb["inline_keyboard"][0][2]["text"].startswith(ta.CHECKED))
        self.assertFalse(kb["inline_keyboard"][0][0]["text"].startswith(ta.CHECKED))
        self.assertFalse(kb["inline_keyboard"][1][2]["text"].startswith(ta.CHECKED))

    def test_first_option_is_never_marked_recommended(self):
        """The first option is NOT marked recommended in grid mode — a grid row is not a ranked
        choice."""
        body = ta.render_grid_body("2 items need an owner", GRID_ITEMS)
        self.assertNotIn(ta.RECOMMENDED_MARK, body)

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        api = FakeApi()
        res, _ = self._send(api=api, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(api.calls, [])
        self.assertEqual(ta.load_store(ta.store_path(self.dir))["questions"], {})

    def test_ask_grid_persists_before_send(self):
        """Same durability contract as `ask()`: the record exists in the store even if we inspect it
        mid-flight, because it is written before the network call."""
        api = FakeApi()

        def recording_send(c, method, params, timeout=30):
            if method == "sendMessage":
                store = ta.load_store(ta.store_path(self.dir))
                self.assertEqual(len(store["questions"]), 1)
            return FakeApi.__call__(api, c, method, params, timeout)

        res, _ = self._send(api=recording_send)
        self.assertTrue(res["sent"])

    def test_a_tap_edits_only_the_keyboard(self):
        res, api = self._send()
        r = self._tap(res["question_id"], 0, 0, "cb1", api)
        self.assertIsNone(r["line"])  # a row tap is not an answer yet
        self.assertEqual(r["selected"], {"0": 0})
        self.assertIn("editMessageReplyMarkup", api.methods())
        self.assertNotIn("editMessageText", api.methods())

    def test_a_second_tap_in_the_same_row_replaces_the_first(self):
        res, api = self._send()
        qid = res["question_id"]
        self._tap(qid, 0, 0, "cb1", api)
        r = self._tap(qid, 0, 3, "cb2", api)
        self.assertEqual(r["selected"], {"0": 3})

    def test_done_submits_touched_rows_and_names_untouched_ones(self):
        res, api = self._send()
        qid = res["question_id"]
        self._tap(qid, 0, 0, "cb1", api)  # loop-a -> Owner
        r = ta.resolve(_cfg(), self.dir, ta.grid_callback_data(qid, None, "done"), "cb2", api=api,
                       now=self.now)
        self.assertTrue(r["answered"])
        self.assertEqual(r["grid_answers"], {"loop-a": "Owner"})
        self.assertEqual(r["untouched"], ["loop-b"])
        self.assertIn("Owner", r["line"])
        self.assertIn("1 left unchanged", r["line"])  # named, not silently dropped

    def test_untouched_row_is_never_guessed(self):
        """An untouched row is left unchanged — not recorded as any of the five choices, including
        Unknown. Silence is not an answer."""
        res, api = self._send()
        qid = res["question_id"]
        r = ta.resolve(_cfg(), self.dir, ta.grid_callback_data(qid, None, "done"), "cb1", api=api,
                       now=self.now)
        self.assertEqual(r["grid_answers"], {})
        self.assertEqual(sorted(r["untouched"]), ["loop-a", "loop-b"])

    def test_done_carries_meta_back(self):
        res, api = self._send(meta={"kind": "owi-unknowns", "batch": 2})
        qid = res["question_id"]
        r = ta.resolve(_cfg(), self.dir, ta.grid_callback_data(qid, None, "done"), "cb1", api=api,
                       now=self.now)
        self.assertEqual(r["meta"], {"kind": "owi-unknowns", "batch": 2})

    def test_a_toggle_carries_no_meta(self):
        res, api = self._send(meta={"kind": "owi-unknowns", "batch": 2})
        r = self._tap(res["question_id"], 0, 0, "cb1", api)
        self.assertNotIn("meta", r)

    def test_a_retap_after_done_is_already_answered(self):
        res, api = self._send()
        qid = res["question_id"]
        ta.resolve(_cfg(), self.dir, ta.grid_callback_data(qid, None, "done"), "cb1", api=api,
                  now=self.now)
        r = self._tap(qid, 0, 0, "cb2", api)
        self.assertTrue(r["already_answered"])
        self.assertIsNone(r["line"])

    def test_an_unreadable_grid_tap_is_never_silent(self):
        api = FakeApi()
        r = ta.resolve(_cfg(), self.dir, "q:deadbeef:gdone", "cb1", api=api, now=self.now)
        self.assertTrue(r["expired"])
        self.assertIn("answerCallbackQuery", api.methods())

    def test_a_tap_on_an_out_of_range_row_does_not_crash(self):
        res, api = self._send()
        r = self._tap(res["question_id"], 99, 0, "cb1", api)
        self.assertIsNotNone(r["line"])
        self.assertIn("answerCallbackQuery", api.methods())

    def test_list_shows_grid_metadata(self):
        # No fixed `now` — `_cmd_list`'s own lazy prune dates itself against the real wall clock, so
        # the record must be fresh by it too.
        self._send(now=None)
        rows = _list_rows(self.dir)
        self.assertEqual(rows[0]["kind"], "grid")
        self.assertEqual(rows[0]["items"], 2)
        self.assertEqual(rows[0]["touched"], 0)

    def test_cli_refuses_missing_choices(self):
        args = argparse.Namespace(
            question="2 items", item=["loop-a|Add a toggle"], choices=None, chat_id=None, meta=None,
            origin_session=None, env_file=None, state_dir=self.dir, dry_run=True, topic=None)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = ta._cmd_ask_grid(args)
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(buf.getvalue())["ok"])

    def test_cli_refuses_duplicate_item_ids(self):
        args = argparse.Namespace(
            question="2 items", item=["loop-a|One", "loop-a|Two"], choices="A|B",
            chat_id=None, meta=None, origin_session=None, env_file=None, state_dir=self.dir,
            dry_run=True, topic=None)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = ta._cmd_ask_grid(args)
        self.assertEqual(rc, 2)


GRID_CHOICES_SEVEN = GRID_CHOICES + ["Archived", "Done"]


class GridPickerTerminalChoices(unittest.TestCase):
    """The grid's two TERMINAL choices (Archived / Done) — for an item that no longer needs an owner
    because it was closed elsewhere. What is pinned here is the ENCODING contract: the five owner choices keep indices 0-4 byte-identical, the two new ones are indices
    5-6 through the same `g<row>c<choice>` payload, a classic tap still can't be mistaken for either,
    and a seven-button row WRAPS on the phone rather than truncating (`GRID_ROW_WIDTH`) without the
    wrap ever reaching the callback data."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.now = NOW

    def tearDown(self):
        self._tmp.cleanup()

    def _send(self):
        api = FakeApi()
        res = ta.ask_grid(_cfg(), self.dir, "2 items need an owner", GRID_ITEMS, GRID_CHOICES_SEVEN,
                          api=api, now=self.now)
        return res, api

    def test_first_five_choices_are_byte_identical(self):
        self.assertEqual(GRID_CHOICES_SEVEN[:5], ["Owner", "Assistant", "Both", "External", "Unknown"])
        self.assertEqual(GRID_CHOICES_SEVEN[5:], ["Archived", "Done"])

    def test_seven_choices_are_within_the_ceiling(self):
        self.assertLessEqual(len(GRID_CHOICES_SEVEN), ta.MAX_GRID_CHOICES)

    def test_archived_and_done_round_trip_through_a_done_tap(self):
        res, api = self._send()
        qid = res["question_id"]
        ta.resolve(_cfg(), self.dir, ta.grid_callback_data(qid, 0, 5), "cb1", api=api, now=self.now)
        ta.resolve(_cfg(), self.dir, ta.grid_callback_data(qid, 1, 6), "cb2", api=api, now=self.now)
        r = ta.resolve(_cfg(), self.dir, ta.grid_callback_data(qid, None, "done"), "cb3", api=api,
                       now=self.now)
        self.assertTrue(r["answered"])
        self.assertEqual(r["grid_answers"], {"loop-a": "Archived", "loop-b": "Done"})
        self.assertEqual(r["untouched"], [])

    def test_a_terminal_pick_can_still_be_replaced_before_done(self):
        """Drop-down semantics survive the new indices: Archived then Owner on one row is Owner."""
        res, api = self._send()
        qid = res["question_id"]
        ta.resolve(_cfg(), self.dir, ta.grid_callback_data(qid, 0, 5), "cb1", api=api, now=self.now)
        ta.resolve(_cfg(), self.dir, ta.grid_callback_data(qid, 0, 0), "cb2", api=api, now=self.now)
        r = ta.resolve(_cfg(), self.dir, ta.grid_callback_data(qid, None, "done"), "cb3", api=api,
                       now=self.now)
        self.assertEqual(r["grid_answers"], {"loop-a": "Owner"})

    def test_new_indices_never_collide_with_a_classic_payload(self):
        self.assertEqual(ta.parse_grid_callback_data("q:abcd1234:g0c6"), ("abcd1234", 0, 6))
        self.assertEqual(ta.parse_callback_data("q:abcd1234:g0c6"), (None, None))
        self.assertEqual(ta.parse_grid_callback_data("q:abcd1234:6"), (None, None, None))

    def test_seven_buttons_wrap_to_two_keyboard_rows_per_item(self):
        kb = ta.build_grid_keyboard("qid", GRID_ITEMS, GRID_CHOICES_SEVEN, {})
        rows = kb["inline_keyboard"]
        self.assertEqual(len(rows), 2 * 2 + 1)  # two keyboard rows per item + Done
        self.assertEqual([len(r) for r in rows[:2]], [4, 3])  # balanced, never 5 + 2
        self.assertEqual(rows[-1][0]["text"], ta.DONE_LABEL)

    def test_the_wrap_never_reaches_the_callback_data(self):
        """Every button still names the ITEM index — item 1's second keyboard row is still `g1c…`."""
        kb = ta.build_grid_keyboard("qid", GRID_ITEMS, GRID_CHOICES_SEVEN, {})
        rows = kb["inline_keyboard"]
        flat = [b["callback_data"] for r in rows[:-1] for b in r]
        expected = [ta.grid_callback_data("qid", i, c) for i in range(2) for c in range(7)]
        self.assertEqual(flat, expected)

    def test_five_choices_still_render_as_one_row(self):
        kb = ta.build_grid_keyboard("qid", GRID_ITEMS, GRID_CHOICES, {})
        self.assertEqual(len(kb["inline_keyboard"]), 3)

    def test_the_mark_lands_on_a_wrapped_row(self):
        kb = ta.build_grid_keyboard("qid", GRID_ITEMS, GRID_CHOICES_SEVEN, {"0": 6})
        rows = kb["inline_keyboard"]
        self.assertTrue(rows[1][-1]["text"].startswith(ta.CHECKED))  # item 0, second keyboard row
        self.assertFalse(rows[0][0]["text"].startswith(ta.CHECKED))

    def test_cli_accepts_seven_choices(self):
        args = argparse.Namespace(
            question="2 items", item=["loop-a|One", "loop-b|Two"],
            choices="|".join(GRID_CHOICES_SEVEN), chat_id=None, meta=None, origin_session=None,
            env_file=None, state_dir=self.dir, dry_run=True, topic=None)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf), mock.patch.object(ta, "load_env", lambda *_: {}), \
                mock.patch.object(ta, "cfg", lambda *_: _cfg()):
            rc = ta._cmd_ask_grid(args)
        self.assertEqual(rc, 0, buf.getvalue())


class ATapIsTheOnlyAnswerAndSettlingIsManual(unittest.TestCase):
    """The two structural guards the module docstring promises: `answered_at` is assigned in exactly
    ONE place (only a real tap may write it), and nothing in the tree calls `settle_in_chat`
    automatically — a background caller could withdraw a live decision the owner never saw."""

    def test_answered_at_is_assigned_in_exactly_one_place(self):
        with open(ta.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(src.count('q["answered_at"] ='), 1)

    def test_nothing_in_the_tree_calls_settle_in_chat(self):
        callers = []
        for name in os.listdir(SCRIPT_DIR):
            if not name.endswith(".py") or name.startswith("test_") or name == "telegram_ask.py":
                continue
            with open(os.path.join(SCRIPT_DIR, name), encoding="utf-8", errors="replace") as fh:
                if "settle_in_chat(" in fh.read():
                    callers.append(name)
        self.assertEqual(callers, [])

    def test_settle_in_chat_refuses_without_a_quote_and_unsettle_hands_it_back(self):
        d = tempfile.mkdtemp()
        qid = ta.ask(_cfg(), d, QUESTION, OPTIONS, api=FakeApi(), now=NOW)["question_id"]
        with self.assertRaises(ValueError):
            ta.settle_in_chat(d, qid, "   ", now=NOW)
        res = ta.settle_in_chat(d, qid, "go with develop", now=NOW)
        self.assertTrue(res["settled"])
        self.assertEqual(res["retired_by"]["by"], ta.IN_CHAT_SETTLER)
        rec = ta.load_store(ta.store_path(d))["questions"][qid]
        self.assertIsNone(rec["answered_at"])          # a settle is never an answer
        self.assertEqual(rec["selected"], [])
        self.assertTrue(ta.unsettle(d, qid, now=NOW)["unsettled"])
        self.assertNotIn("retired_at", ta.load_store(ta.store_path(d))["questions"][qid])


if __name__ == "__main__":
    unittest.main()
