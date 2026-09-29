#!/usr/bin/env python3
"""Tests for **private-chat TOPICS** — `telegram_topics.py`, and the approval picker's route into
one (`../docs/telegram-capability-map.md` §2.1). Stdlib ``unittest`` only, like the rest of the
suite.

What this feature exists for: pickers buried in a day's conversation are decisions that cannot be
made, and a merge picker the owner cannot find is a merge that cannot happen.

**THE ACCEPTANCE BAR IS ZERO REGRESSION, AND MOST OF THIS FILE IS ABOUT THAT.** Private-chat topics
are gated on a @BotFather **Mini App** toggle that no code can flip, so on a fresh bot this feature is
OFF until the owner flips it by hand. With it off, a picker must be indistinguishable from one sent by
a build that never heard of topics: same chat, same payload, no extra send. `TopicsOff` proves that as
an equality between two payloads rather than as a set of hand-written key assertions.

The rest is the fail-open ladder. Every branch in `thread_id` answers `None`, meaning the main chat:
topics off, `getMe` unreachable, `getMe` from a pre-9.3 Bot API, an unknown purpose, an unreadable
state file, a creation that failed. And at the send, a stored id Telegram now refuses falls back to
the main chat **and the picker still lands** — which is the one outcome this whole feature may never
cost.

**A PICKER THAT NAMES NO TOPIC GOES TO ONE ANYWAY.** `decisions` is the
:data:`telegram_topics.DEFAULT_TOPIC`, so the fail-open ladder is not about one caller's opt-in flag —
it stands between every picker the assistant sends and the owner's phone. **That is why
`DefaultTopic` re-asserts the ladder against the DEFAULT purpose rather than trusting the
`pull-requests` coverage above**: a default that silently drops a picker when topics are off would
be strictly worse than no topic at all, and "it's the same code path" is a claim a test should make
rather than a comment.

What's covered:
  * topics OFF -> the `sendMessage` params are EQUAL to a no-topic send's, and `getMe` is the only
    extra call ever made;
  * the DEFAULT purpose: the parser's default is `decisions`, `_cmd_ask` resolves it when no
    `--topic` is given, `--topic main` forces the main chat, `--dry-run` resolves nothing at all,
    the whole fail-open ladder answers *main chat* for it, and a stale default-topic id is
    forgotten by the same path the explicit flag uses;
  * topics ON + nothing stored -> `createForumTopic` runs ONCE, the id is persisted, and the next
    picker reuses it with no second creation;
  * a stored id Telegram rejects -> `forget` + a re-send to the main chat, and `ok/sent` is still true;
  * an AMBIGUOUS failure with a thread id is NOT re-sent (the picker may already be on the phone);
  * `getMe` unavailable / a bot with no `has_topics_enabled` field -> main chat, and the failure is
    NOT cached, so one blip is not six hours of main-chat pickers;
  * the detect cache: one `getMe` inside the TTL, a fresh one past it;
  * the toggle nudge fires **at most once per boot**, is silent when topics are on, and silent when
    the detect could not be made at all;
  * the table is DATA: the owner's `telegram-topics.json` when present, else the shipped
    `.example.json`, plus the runtime overlay (`RuntimeOverlay`) and the `add`/`list`/`retire` CLI
    (`MintCLI`);
  * `merge_guard.request_argv` carries the topic (skipped until the optional `merge_guard` module is
    installed);
  * inbound: a topic-originated message surfaces its `message_thread_id` and one from the main chat
    is byte-identical to before.

**No network, ever, and no writes outside a temp dir.** Every Bot API call goes through an injected
`api` seam that every test here supplies; `telegram_send.api_call` is never reached. A live daemon may
be holding a real conversation on the configured bot token, and a test that posts for real is a bug.

Run:  python -m unittest seneschal.scripts.test_telegram_topics   (or)   python test_telegram_topics.py
"""
import contextlib
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

try:  # merge_guard is an optional module (the PR-approval gate); its route tests skip without it
    import merge_guard as mg  # noqa: E402
except ImportError:  # pragma: no cover — depends on the install
    mg = None
import telegram_ask as ta  # noqa: E402
import telegram_poll as tp  # noqa: E402
import telegram_send as ts  # noqa: E402
import telegram_topics as tt  # noqa: E402

NOW = datetime(2026, 8, 27, 22, 41, tzinfo=timezone.utc)
THREAD = 17
QUESTION = "Merge example/repo pull request 479 — a functional change?"
OPTIONS = [
    {"label": "Approve", "description": "Merge 479 at cabf9331b2c4. Single-use, this commit only."},
    {"label": "Not now", "description": "Nothing merges. The PR stays open until you say otherwise."},
]


class FakeApi:
    """Stands in for `telegram_send.api_call`. Records every call and answers the three methods this
    feature touches. `topics` is `has_topics_enabled`; `None` makes `getMe` answer like a pre-9.3
    bot, which is a different thing from `False` and must not be read as one."""

    def __init__(self, topics=True, thread=THREAD, fail=(), message_id=4242):
        self.calls = []
        self.topics = topics
        self.thread = thread
        self.fail = dict(fail)   # method -> exception to raise
        self.message_id = message_id

    def __call__(self, c, method, params, timeout=30):
        self.calls.append((method, params))
        if method in self.fail:
            raise self.fail[method]
        if method == "getMe":
            result = {"id": 1, "is_bot": True, "username": "assistant_bot"}
            if self.topics is not None:
                result["has_topics_enabled"] = self.topics
            return {"ok": True, "result": result}
        if method == "createForumTopic":
            return {"ok": True, "result": {"message_thread_id": self.thread,
                                           "name": params.get("name")}}
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": self.message_id}}
        return {"ok": True, "result": True}

    def methods(self):
        return [m for m, _ in self.calls]

    def count(self, method):
        return self.methods().count(method)

    def params(self, method):
        return [p for m, p in self.calls if m == method]


def _cfg(chat_id="555"):
    return {"token": "tok", "chat_id": chat_id, "api_base": "https://api.telegram.org",
            "parse_mode": ""}


def _comparable(params: dict) -> dict:
    """One `sendMessage` payload, with the ONE field that legitimately differs between two asks
    neutralised: `callback_data` carries a fresh 8-hex question id per picker. Everything else —
    including the presence or absence of `message_thread_id`, the button labels, the text and the
    parse mode — is compared verbatim, so this stays an equality about the whole payload rather than
    a spot check on the key the change happens to have added."""
    out = dict(params)
    keyboard = json.loads(out.pop("reply_markup"))
    for row in keyboard.get("inline_keyboard", []):
        for button in row:
            button["callback_data"] = "q:<id>:" + button["callback_data"].rsplit(":", 1)[-1]
    out["reply_markup"] = keyboard
    return out


class TopicCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        # The nudge's once-per-boot flag is process state. Reset it around every test so no test can
        # be made to pass (or fail) by whichever one ran before it.
        self._nudged = tt._NUDGED_THIS_BOOT
        tt._NUDGED_THIS_BOOT = False
        self.addCleanup(lambda: setattr(tt, "_NUDGED_THIS_BOOT", self._nudged))

    def state(self):
        return tt.load_state(self.dir)

    def seed_detect(self, enabled, at=NOW):
        st = tt.load_state(self.dir)
        st["detect"] = {"enabled": enabled, "checked_at": tt._stamp(at)}
        tt.save_state(self.dir, st)

    def seed_topic(self, tid=THREAD, purpose=tt.TOPIC_PULL_REQUESTS):
        st = tt.load_state(self.dir)
        st["topics"][purpose] = {"message_thread_id": tid, "name": "Pull requests",
                                 "created_at": tt._stamp(NOW)}
        tt.save_state(self.dir, st)


# --------------------------------------------------------------------------- the acceptance bar

class TopicsOff(TopicCase):
    """**The zero-regression bar.** The @BotFather toggle is off on a fresh bot and no code can
    flip it, so this is the state everything ships into."""

    def test_the_payload_is_identical_to_a_send_that_never_heard_of_topics(self):
        """Stated as an EQUALITY, not as `assertNotIn("message_thread_id", …)`.

        The weaker assertion passes on a payload carrying `message_thread_id: None`, and on one that
        grew some other field along the way. Comparing the whole dict against a run with no topic at
        all is the only form that says *nothing changed* rather than *this one thing is absent*."""
        api_off = FakeApi(topics=False)
        tid = tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api_off, now=NOW)
        self.assertIsNone(tid)
        with_topic = FakeApi()
        ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=with_topic, now=NOW, message_thread_id=tid)
        without = FakeApi()
        ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=without, now=NOW)
        self.assertEqual(_comparable(with_topic.params("sendMessage")[0]),
                         _comparable(without.params("sendMessage")[0]))

    def test_the_send_carries_no_thread_key_at_all(self):
        api = FakeApi()
        ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=api, now=NOW, message_thread_id=None)
        self.assertNotIn("message_thread_id", api.params("sendMessage")[0])

    def test_the_result_object_gains_nothing(self):
        """A caller that never asked for a topic reads exactly the object it read before."""
        res = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=FakeApi(), now=NOW)
        self.assertNotIn("message_thread_id", res)
        self.assertNotIn("thread_fallback", res)

    def test_the_only_extra_call_is_getMe_and_no_topic_is_created(self):
        """'No extra call' means no extra SEND. One cached `getMe` is what the detect costs, and a
        bot with topics off must never have a topic created in it."""
        api = FakeApi(topics=False)
        tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW)
        self.assertEqual(api.methods(), ["getMe"])

    def test_a_second_lookup_inside_the_ttl_costs_nothing(self):
        api = FakeApi(topics=False)
        for _ in range(3):
            tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW)
        self.assertEqual(api.count("getMe"), 1)


# --------------------------------------------------------------------------- the happy path

class TopicsOn(TopicCase):
    def test_it_creates_once_persists_and_reuses(self):
        api = FakeApi()
        first = tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW)
        self.assertEqual(first, THREAD)
        self.assertEqual(api.count("createForumTopic"), 1)
        row = self.state()["topics"][tt.TOPIC_PULL_REQUESTS]
        self.assertEqual(row["message_thread_id"], THREAD)
        self.assertEqual(row["name"], tt.TOPIC_NAMES[tt.TOPIC_PULL_REQUESTS])
        again = tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW)
        self.assertEqual(again, THREAD)
        self.assertEqual(api.count("createForumTopic"), 1)  # THE point: it does not create a second

    def test_the_topic_is_named_explicitly(self):
        """`ForumTopic.is_name_implicit` is for threads the OWNER starts and a bot renames. The
        assistant creates this one, so it is named at creation and `editForumTopic` is never called."""
        api = FakeApi()
        tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW)
        self.assertEqual(api.params("createForumTopic")[0]["name"], "Pull requests")
        self.assertNotIn("editForumTopic", api.methods())

    def test_the_picker_goes_into_the_thread(self):
        self.seed_detect(True)
        self.seed_topic()
        api = FakeApi()
        res = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=api, now=NOW,
                     message_thread_id=tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir,
                                                    api=api, now=NOW))
        self.assertEqual(api.params("sendMessage")[0]["message_thread_id"], THREAD)
        self.assertEqual(res["message_thread_id"], THREAD)
        self.assertTrue(res["sent"])

    def test_no_delete_or_archive_verb_exists_anywhere_in_the_module(self):
        """`closeForumTopic`/`reopenForumTopic` do not work in private chats, so the only verb short
        of leaving a topic alone is `deleteForumTopic` — destructive, ask-high, and deliberately not
        built here. Asserted against the FILE, because the point is that nobody can reach it."""
        with open(os.path.join(SCRIPT_DIR, "telegram_topics.py"), encoding="utf-8") as fh:
            src = fh.read()
        for method in ("deleteForumTopic", "closeForumTopic", "reopenForumTopic", "editForumTopic"):
            self.assertNotIn(f'"{method}"', src)


# --------------------------------------------------------------------------- every fail-open branch

class FailsOpen(TopicCase):
    def test_getMe_unreachable_means_the_main_chat(self):
        api = FakeApi(fail={"getMe": RuntimeError("connection reset")})
        self.assertIsNone(tt.topics_enabled(_cfg(), self.dir, api=api, now=NOW))
        self.assertIsNone(tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW))
        self.assertNotIn("createForumTopic", api.methods())

    def test_a_failed_detect_is_not_cached(self):
        """Caching 'could not tell' would turn one network blip into six hours of main-chat
        pickers."""
        api = FakeApi(fail={"getMe": RuntimeError("boom")})
        tt.topics_enabled(_cfg(), self.dir, api=api, now=NOW)
        self.assertEqual(self.state()["detect"], {})

    def test_a_bot_api_with_no_has_topics_enabled_reads_as_unknown_not_false(self):
        """Absent is not False. Both fall back the same way, but only False may be cached and only
        False may nudge the owner to flip a switch."""
        api = FakeApi(topics=None)
        self.assertIsNone(tt.topics_enabled(_cfg(), self.dir, api=api, now=NOW))
        self.assertEqual(self.state()["detect"], {})

    def test_an_unknown_purpose_is_the_main_chat_not_an_error(self):
        api = FakeApi()
        self.assertIsNone(tt.thread_id(_cfg(), "not-a-real-purpose", self.dir, api=api, now=NOW))
        self.assertEqual(api.methods(), [])  # it does not even ask

    def test_a_corrupt_state_file_costs_the_thread_and_never_raises(self):
        with open(tt.state_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        self.assertEqual(tt.load_state(self.dir)["topics"], {})
        self.assertIsNone(tt.stored_thread_id(self.dir, tt.TOPIC_PULL_REQUESTS))

    def test_a_failed_creation_is_the_main_chat(self):
        self.seed_detect(True)
        api = FakeApi(fail={"createForumTopic": ts.TelegramAPIError("nope", method="createForumTopic")})
        self.assertIsNone(tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW))
        self.assertNotIn(tt.TOPIC_PULL_REQUESTS, self.state()["topics"])

    def test_a_creation_answering_a_shape_we_do_not_recognise_is_the_main_chat(self):
        self.seed_detect(True)
        api = FakeApi(thread=None)
        self.assertIsNone(tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW))


class StaleThread(TopicCase):
    """A stored id Telegram now refuses — the owner deleted the topic, or the bot was re-pointed. **The
    picker still lands**, which is the whole bar: a picker lost to an unavailable thread would be
    strictly worse than the scrolling this feature exists to end."""

    class RejectsThreads:
        """Refuses any `sendMessage` carrying a thread id, the way Telegram refuses a dead one."""

        def __init__(self):
            self.calls = []

        def __call__(self, c, method, params, timeout=30):
            self.calls.append((method, params))
            if method == "sendMessage" and params.get("message_thread_id") is not None:
                raise ts.TelegramAPIError("Bad Request: message thread not found",
                                          method="sendMessage")
            return {"ok": True, "result": {"message_id": 99}}

        def params(self, method):
            return [p for m, p in self.calls if m == method]

    def test_the_picker_falls_back_to_the_main_chat_and_still_sends(self):
        api = self.RejectsThreads()
        res = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=api, now=NOW, message_thread_id=THREAD)
        self.assertTrue(res["ok"])
        self.assertTrue(res["sent"])
        self.assertEqual(res["message_id"], 99)
        self.assertTrue(res["thread_fallback"])
        self.assertNotIn("message_thread_id", res)   # it did NOT go to a thread; do not claim it did
        sends = api.params("sendMessage")
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[0]["message_thread_id"], THREAD)
        self.assertNotIn("message_thread_id", sends[1])
        self.assertEqual(sends[1]["text"], sends[0]["text"])   # the same picker, not a degraded one

    def test_the_fallback_send_is_identical_to_a_plain_one(self):
        api = self.RejectsThreads()
        ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=api, now=NOW, message_thread_id=THREAD)
        plain = FakeApi()
        ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=plain, now=NOW)
        self.assertEqual(_comparable(api.params("sendMessage")[1]),
                         _comparable(plain.params("sendMessage")[0]))

    def test_the_stale_id_is_forgotten_so_the_next_picker_creates_a_fresh_topic(self):
        self.seed_detect(True)
        self.seed_topic(tid=THREAD)
        self.assertTrue(tt.forget(self.dir, tt.TOPIC_PULL_REQUESTS))
        self.assertIsNone(tt.stored_thread_id(self.dir, tt.TOPIC_PULL_REQUESTS))
        api = FakeApi(thread=88)
        self.assertEqual(tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW), 88)

    def test_an_ambiguous_failure_is_never_re_sent(self):
        """The request went out and the picker may already be on the owner's phone. A blind
        fallback here is a SECOND live picker for the same question — exactly the duplicate a
        merge-ask dedupe log exists to prevent."""
        class Ambiguous:
            def __init__(self):
                self.calls = []

            def __call__(self, c, method, params, timeout=30):
                self.calls.append(method)
                raise RuntimeError("read timed out after the request went out")

        api = Ambiguous()
        with self.assertRaises(RuntimeError):
            ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=api, now=NOW, message_thread_id=THREAD)
        self.assertEqual(api.calls.count("sendMessage"), 1)


# --------------------------------------------------------------------------- the detect cache

class DetectCache(TopicCase):
    def test_a_fresh_answer_is_reused(self):
        api = FakeApi(topics=True)
        tt.topics_enabled(_cfg(), self.dir, api=api, now=NOW)
        tt.topics_enabled(_cfg(), self.dir, api=api, now=NOW + timedelta(seconds=tt.DETECT_TTL_SEC - 1))
        self.assertEqual(api.count("getMe"), 1)

    def test_it_is_re_asked_past_the_ttl(self):
        api = FakeApi(topics=False)
        tt.topics_enabled(_cfg(), self.dir, api=api, now=NOW)
        api.topics = True
        later = NOW + timedelta(seconds=tt.DETECT_TTL_SEC + 1)
        self.assertTrue(tt.topics_enabled(_cfg(), self.dir, api=api, now=later))
        self.assertEqual(api.count("getMe"), 2)

    def test_a_stamp_from_the_future_is_not_trusted(self):
        """A clock change must not pin the detect forever."""
        self.seed_detect(False, at=NOW + timedelta(days=2))
        api = FakeApi(topics=True)
        self.assertTrue(tt.topics_enabled(_cfg(), self.dir, api=api, now=NOW))


# --------------------------------------------------------------------------- the one-time nudge

class ToggleNudge(TopicCase):
    """The switch lives in the @BotFather **Mini App** and is not in the classic
    `/mybots → Bot Settings` menu, so the alternative to one nudge is a feature that silently never
    turns on. One. Not a ladder."""

    def queued(self):
        path = os.path.join(self.dir, "outbound.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_it_fires_at_most_once_per_boot(self):
        api = FakeApi(topics=False)
        self.assertTrue(tt.nudge_if_disabled(_cfg(), self.dir, api=api, now=NOW))
        for _ in range(4):
            self.assertFalse(tt.nudge_if_disabled(_cfg(), self.dir, api=api, now=NOW))
        self.assertEqual(len([q for q in self.queued() if q.get("kind") == "nudge"]), 1)

    def test_it_says_where_the_switch_is(self):
        api = FakeApi(topics=False)
        tt.nudge_if_disabled(_cfg(), self.dir, api=api, now=NOW)
        text = [q for q in self.queued() if q.get("kind") == "nudge"][0]["text"]
        self.assertIn("BotFather", text)
        self.assertIn("Mini App", text)

    def test_it_is_silent_when_topics_are_on(self):
        self.assertFalse(tt.nudge_if_disabled(_cfg(), self.dir, api=FakeApi(topics=True), now=NOW))
        self.assertEqual(self.queued(), [])

    def test_it_is_silent_when_the_detect_could_not_be_made(self):
        """Nudging the owner to flip a switch that may already be flipped is worse than silence."""
        api = FakeApi(fail={"getMe": RuntimeError("offline")})
        self.assertFalse(tt.nudge_if_disabled(_cfg(), self.dir, api=api, now=NOW))
        self.assertEqual(self.queued(), [])
        self.assertFalse(tt._NUDGED_THIS_BOOT)   # a blip does not burn this boot's one nudge


# --------------------------------------------------------------------------- the default

class DefaultTopic(TopicCase):
    """**A picker that names no topic goes to `decisions` anyway** — a default rather than a flag,
    because no one can retrofit every existing caller.

    **Every fail-open branch is re-asserted here against the DEFAULT purpose, not inherited from the
    `pull-requests` coverage above.** They are the same code path today, and *"they are the same code
    path"* is precisely the kind of claim that is true when it is written and false after the next
    edit — while the consequence of it being false is larger: an opt-in flag that misroutes costs one
    caller's picker, and a default that drops one costs every picker the assistant sends."""

    def _args(self, *extra):
        """A real parsed namespace from the real parser, so the DEFAULT under test is the shipped
        one rather than a value the test supplies. `--chat-id` is passed because `_cmd_ask` refuses
        a non-dry-run send with no recipient, and this host has no `telegram.env`."""
        return ta.build_parser().parse_args([
            "--state-dir", self.dir, "ask", "--chat-id", "555", "--question", QUESTION,
            *[arg for opt in OPTIONS
              for arg in ("--option", f"{opt['label']}|{opt['description']}")],
            *extra])

    # -- the routing table and the two escapes

    def test_a_picker_that_names_no_topic_asks_for_the_decisions_topic(self):
        self.assertEqual(tt.DEFAULT_TOPIC, tt.TOPIC_DECISIONS)
        self.assertEqual(self._args().topic, tt.TOPIC_DECISIONS)
        self.assertEqual(tt.TOPIC_NAMES[tt.TOPIC_DECISIONS], "Decisions")

    def test_an_explicit_topic_still_wins(self):
        self.assertEqual(self._args("--topic", tt.TOPIC_PULL_REQUESTS).topic, tt.TOPIC_PULL_REQUESTS)

    def test_main_is_the_explicit_main_chat_spelling_and_is_not_a_row(self):
        """`main` resolves through the unknown-purpose branch on purpose, so *"the main chat,
        deliberately"* and *"a typo in a purpose name"* cannot diverge. Making it a row — or adding
        argparse `choices` so a typo is refused — would break exactly that."""
        self.assertNotIn(tt.TOPIC_MAIN_CHAT, tt.TOPIC_NAMES)
        self.seed_detect(True)
        api = FakeApi()
        self.assertEqual(self._args("--topic", tt.TOPIC_MAIN_CHAT).topic, tt.TOPIC_MAIN_CHAT)
        self.assertIsNone(tt.thread_id(_cfg(), tt.TOPIC_MAIN_CHAT, self.dir, api=api, now=NOW))
        self.assertEqual(api.methods(), [])       # it does not ask, and creates nothing

    def test_the_two_topics_are_two_threads_with_two_names(self):
        self.seed_detect(True)
        api = FakeApi(thread=21)
        decisions = tt.thread_id(_cfg(), tt.TOPIC_DECISIONS, self.dir, api=api, now=NOW)
        api.thread = 22
        prs = tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW)
        self.assertEqual((decisions, prs), (21, 22))
        self.assertEqual([p["name"] for p in api.params("createForumTopic")],
                         ["Decisions", "Pull requests"])

    # -- the CLI seam: where the default is actually applied

    def test_cmd_ask_resolves_the_default_when_no_topic_is_given(self):
        """The claim the default turns on, driven through `_cmd_ask` rather than read off the
        parser: no `--topic`, and the thread the send carries is the decisions one."""
        seen = []

        def fake_thread_id(c, purpose, state_dir, api=None, now=None, log=None):
            seen.append(("resolve", purpose))
            return THREAD

        def fake_ask(*a, **kw):
            seen.append(("ask", kw.get("message_thread_id")))
            return {"ok": True, "sent": True}

        with mock.patch.object(tt, "thread_id", fake_thread_id), \
                mock.patch.object(ta, "ask", fake_ask):
            self.assertEqual(ta._cmd_ask(self._args()), 0)
        self.assertEqual(seen, [("resolve", tt.TOPIC_DECISIONS), ("ask", THREAD)])

    def test_a_dry_run_resolves_no_topic_even_with_the_default(self):
        """Resolving one can CREATE a topic, and `--dry-run` promises no effects. The default may
        not turn every dry run into a live `createForumTopic`."""
        seen = []
        with mock.patch.object(tt, "thread_id", lambda *a, **kw: seen.append(a) or THREAD):
            self.assertEqual(ta._cmd_ask(self._args("--dry-run")), 0)
        self.assertEqual(seen, [])

    def test_a_stale_default_topic_id_is_forgotten_by_the_same_path(self):
        """`thread_fallback` -> `forget`, the path the explicit flag rides. The default must ride
        it rather than acquire a second one."""
        self.seed_detect(True)
        self.seed_topic(tid=THREAD, purpose=tt.TOPIC_DECISIONS)
        with mock.patch.object(tt, "thread_id", lambda *a, **kw: THREAD), \
                mock.patch.object(ta, "ask", lambda *a, **kw: {"ok": True, "sent": True,
                                                               "thread_fallback": True}):
            self.assertEqual(ta._cmd_ask(self._args()), 0)
        self.assertIsNone(tt.stored_thread_id(self.dir, tt.TOPIC_DECISIONS))

    # -- the ladder, against the default purpose

    def test_the_default_purpose_answers_the_main_chat_on_every_failure(self):
        """`detect_on` is per-case rather than blanket, and getting it wrong is how this test passes
        vacuously: seed the detect for the topics-off case and `topics_enabled` reads the cache, the
        creation succeeds, and a test named *fails open* asserts a thread id."""
        cases = [
            ("topics off", FakeApi(topics=False), False),
            ("getMe unreachable", FakeApi(fail={"getMe": RuntimeError("connection reset")}), False),
            ("a bot with no has_topics_enabled", FakeApi(topics=None), False),
            ("a creation that failed", FakeApi(
                fail={"createForumTopic": ts.TelegramAPIError("no", method="createForumTopic")}),
             True),
            ("a creation answering a shape we do not know", FakeApi(thread=None), True),
        ]
        for name, api, detect_on in cases:
            with self.subTest(case=name):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                if detect_on:
                    tt.save_state(tmp.name, {"detect": {"enabled": True,
                                                        "checked_at": tt._stamp(NOW)},
                                             "topics": {}})
                self.assertIsNone(tt.thread_id(_cfg(), tt.DEFAULT_TOPIC, tmp.name,
                                               api=api, now=NOW))
                self.assertIsNone(tt.stored_thread_id(tmp.name, tt.DEFAULT_TOPIC))

    def test_a_corrupt_state_file_costs_the_default_thread_and_never_raises(self):
        with open(tt.state_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        self.assertIsNone(tt.stored_thread_id(self.dir, tt.DEFAULT_TOPIC))
        self.assertIsNone(tt.thread_id(_cfg(), tt.DEFAULT_TOPIC, self.dir,
                                       api=FakeApi(fail={"getMe": RuntimeError("offline")}),
                                       now=NOW))

    def test_with_topics_off_the_default_sends_exactly_what_it_sent_before(self):
        """The `TopicsOff` equality again, on the DEFAULT purpose — because this is the payload
        nearly every picker produces, and *no `message_thread_id` key at all* is what
        makes an un-flipped @BotFather toggle a no-op rather than a regression."""
        api_off = FakeApi(topics=False)
        tid = tt.thread_id(_cfg(), tt.DEFAULT_TOPIC, self.dir, api=api_off, now=NOW)
        self.assertIsNone(tid)
        self.assertEqual(api_off.methods(), ["getMe"])      # no creation on a bot with topics off
        defaulted = FakeApi()
        ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=defaulted, now=NOW, message_thread_id=tid)
        without = FakeApi()
        ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=without, now=NOW)
        self.assertNotIn("message_thread_id", defaulted.params("sendMessage")[0])
        self.assertEqual(_comparable(defaulted.params("sendMessage")[0]),
                         _comparable(without.params("sendMessage")[0]))

    def test_a_refused_default_thread_still_lands_the_picker(self):
        """The one outcome the whole feature may never cost. Same assertion as `StaleThread`, on the
        purpose that now carries every non-merge picker."""
        api = StaleThread.RejectsThreads()
        res = ta.ask(_cfg(), self.dir, QUESTION, OPTIONS, api=api, now=NOW, message_thread_id=THREAD)
        self.assertTrue(res["ok"] and res["sent"])
        self.assertTrue(res["thread_fallback"])
        sends = api.params("sendMessage")
        self.assertNotIn("message_thread_id", sends[1])
        self.assertEqual(sends[1]["text"], sends[0]["text"])


# --------------------------------------------------------------------------- an explicit-only row

class ExplicitOnlyTopic(TopicCase):
    """A purpose reached ONLY by an explicit `--topic <purpose>` — neither a picker default nor a
    nudge route. Adding one must cost nothing to any existing caller. `projects` stands in for any
    such owner-configured row, via a fixture reference table."""

    def _refs(self):
        refs = tempfile.mkdtemp(dir=self.dir)
        with open(os.path.join(refs, tt.REFERENCES_FILE), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "topics": {**tt.TOPIC_NAMES, "projects": "Projects"}}, fh)
        return refs

    def test_it_is_creatable_like_any_other_row(self):
        self.seed_detect(True)
        api = FakeApi(thread=33)
        tid = tt.thread_id(_cfg(), "projects", self.dir, api=api, now=NOW, references_dir=self._refs())
        self.assertEqual(tid, 33)
        self.assertEqual(api.params("createForumTopic")[0]["name"], "Projects")
        row = self.state()["topics"]["projects"]
        self.assertEqual(row["message_thread_id"], 33)
        self.assertEqual(row["name"], "Projects")

    def test_it_does_not_move_the_default(self):
        """Adding a row must not change what a picker naming no topic gets."""
        self.assertEqual(tt.DEFAULT_TOPIC, tt.TOPIC_DECISIONS)

    def test_it_does_not_move_the_reminder_route(self):
        """Adding a row must not change where a nudge goes, piercing or not."""
        self.assertEqual(tt.reminder_topic(pierces=False), tt.TOPIC_REMINDERS)
        self.assertEqual(tt.reminder_topic(pierces=True), tt.TOPIC_REMINDERS)

    def test_an_unknown_purpose_still_falls_through_to_the_main_chat(self):
        """An extra row does not widen what counts as a known purpose."""
        api = FakeApi()
        self.assertIsNone(tt.thread_id(_cfg(), "not-projects", self.dir, api=api, now=NOW,
                                       references_dir=self._refs()))
        self.assertEqual(api.methods(), [])  # it does not even ask


# ------------------------------------------------------------- the table is DATA, not a literal

class ReferenceFileRegistry(TopicCase):
    """**`TOPIC_NAMES` is read from `../references/telegram-topics.json` (else the shipped
    `.example.json`)** — adding a topic must not be a code change. The point: a purpose that exists
    ONLY in a data file, never spelled anywhere in this module, must resolve."""

    def _write_refs(self, topics: dict, extra=None):
        refs_dir = tempfile.mkdtemp(dir=self.dir)
        payload = {"version": 1, "topics": topics}
        if extra:
            payload.update(extra)
        with open(os.path.join(refs_dir, tt.REFERENCES_FILE), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return refs_dir

    def test_a_purpose_named_only_in_the_file_resolves_with_no_code_change(self):
        """`woodworking` is not, and must never become, a Python constant in `telegram_topics.py` —
        the file alone is what makes it routable."""
        self.assertNotIn("woodworking", tt.TOPIC_NAMES)
        refs = self._write_refs({"woodworking": "Woodworking"})
        self.seed_detect(True)
        api = FakeApi(thread=91)
        tid = tt.thread_id(_cfg(), "woodworking", self.dir, api=api, now=NOW, references_dir=refs)
        self.assertEqual(tid, 91)
        self.assertEqual(api.params("createForumTopic")[0]["name"], "Woodworking")
        self.assertEqual(self.state()["topics"]["woodworking"]["message_thread_id"], 91)

    def test_the_shipped_example_carries_the_well_known_purposes(self):
        """The `.example.json` this repo ships is the default every fresh install routes against;
        the purposes other modules name in code must be rows in it."""
        with open(os.path.join(tt.REFERENCES_DIR, tt.REFERENCES_EXAMPLE_FILE), encoding="utf-8") as fh:
            on_disk = json.load(fh)["topics"]
        for purpose in (tt.TOPIC_PULL_REQUESTS, tt.TOPIC_DECISIONS, tt.TOPIC_REMINDERS):
            self.assertIn(purpose, on_disk)
        self.assertNotIn(tt.TOPIC_MAIN_CHAT, on_disk)

    @unittest.skipIf(os.path.exists(os.path.join(tt.REFERENCES_DIR, tt.REFERENCES_FILE)),
                     "this install has its own telegram-topics.json; the snapshot follows that")
    def test_with_no_owner_file_the_snapshot_is_the_example(self):
        """No fixture, no override — without an owner copy, the production `TOPIC_NAMES` snapshot
        must be exactly the shipped example, not a value still hard-coded somewhere."""
        with open(os.path.join(tt.REFERENCES_DIR, tt.REFERENCES_EXAMPLE_FILE), encoding="utf-8") as fh:
            on_disk = json.load(fh)["topics"]
        self.assertEqual(tt.TOPIC_NAMES, on_disk)

    def test_the_owner_file_wins_over_the_example(self):
        refs = tempfile.mkdtemp(dir=self.dir)
        with open(os.path.join(refs, tt.REFERENCES_EXAMPLE_FILE), "w", encoding="utf-8") as fh:
            json.dump({"topics": {"decisions": "Decisions"}}, fh)
        with open(os.path.join(refs, tt.REFERENCES_FILE), "w", encoding="utf-8") as fh:
            json.dump({"topics": {"projects": "Projects"}}, fh)
        self.assertEqual(tt._load_base_topic_names(refs), {"projects": "Projects"})

    def test_the_example_is_the_fallback_when_the_owner_file_is_absent(self):
        refs = tempfile.mkdtemp(dir=self.dir)
        with open(os.path.join(refs, tt.REFERENCES_EXAMPLE_FILE), "w", encoding="utf-8") as fh:
            json.dump({"topics": {"decisions": "Decisions"}}, fh)
        self.assertEqual(tt._load_base_topic_names(refs), {"decisions": "Decisions"})

    def test_a_corrupt_owner_file_does_not_resurrect_the_example(self):
        """Existence decides which file is read, not validity: the owner may have dropped a row on
        purpose, so a broken owner file reads as empty (main chat) rather than as the example."""
        refs = tempfile.mkdtemp(dir=self.dir)
        with open(os.path.join(refs, tt.REFERENCES_EXAMPLE_FILE), "w", encoding="utf-8") as fh:
            json.dump({"topics": {"decisions": "Decisions"}}, fh)
        with open(os.path.join(refs, tt.REFERENCES_FILE), "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        self.assertEqual(tt._load_base_topic_names(refs), {})

    def test_a_missing_reference_file_degrades_to_the_main_chat_not_a_crash(self):
        refs = os.path.join(self.dir, "does-not-exist")
        api = FakeApi()
        self.assertIsNone(tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW,
                                        references_dir=refs))
        self.assertEqual(api.methods(), [])  # unknown purpose short-circuits before any call

    def test_a_corrupt_reference_file_degrades_to_the_main_chat_not_a_crash(self):
        refs = tempfile.mkdtemp(dir=self.dir)
        with open(os.path.join(refs, tt.REFERENCES_FILE), "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        api = FakeApi()
        self.assertIsNone(tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW,
                                        references_dir=refs))
        self.assertEqual(api.methods(), [])

    def test_a_wrong_shaped_reference_file_degrades_to_the_main_chat(self):
        """`topics` must be an object of string -> string; anything else reads as empty rather than
        raising or half-applying."""
        refs = tempfile.mkdtemp(dir=self.dir)
        with open(os.path.join(refs, tt.REFERENCES_FILE), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "topics": ["not", "a", "dict"]}, fh)
        api = FakeApi()
        self.assertIsNone(tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW,
                                        references_dir=refs))

    def test_an_empty_reference_file_degrades_to_the_main_chat(self):
        refs = self._write_refs({})
        api = FakeApi()
        self.assertIsNone(tt.thread_id(_cfg(), tt.TOPIC_PULL_REQUESTS, self.dir, api=api, now=NOW,
                                        references_dir=refs))

    def test_main_in_the_file_is_never_a_routable_row(self):
        """`TOPIC_MAIN_CHAT` must stay out of the registry even if a future edit adds it to the data
        file by mistake — the invariant is structural, not merely a matter of review catching it."""
        refs = self._write_refs({tt.TOPIC_MAIN_CHAT: "Main Chat", "projects": "Projects"})
        self.assertNotIn(tt.TOPIC_MAIN_CHAT, tt._load_topic_names(refs, self.dir))
        self.assertIn("projects", tt._load_topic_names(refs, self.dir))

    def test_non_string_values_are_dropped_not_raised(self):
        """JSON object keys are always strings; the value is what can go wrong (a stray int/null
        typed into the file). Each bad row is dropped on its own — one malformed entry may not cost
        the well-formed rows beside it."""
        refs = self._write_refs({"ok": "Fine", "bad-int": 5, "bad-null": None})
        names = tt._load_topic_names(refs, self.dir)
        self.assertEqual(names, {"ok": "Fine"})


# --------------------------------------------------------------------------- the caller

@unittest.skipIf(mg is None, "merge_guard (the optional PR-approval gate) is not installed")
class ApprovalPickerRoute(TopicCase):
    """`merge_guard.ask_on_green` -> `request_argv` -> the `telegram_ask.py` subprocess. The thread
    id is injected at the far end; this is the flag that asks for it."""

    FACTS = {"pr": 479, "repo": "example/repo", "head_sha": "c" * 40,
             "paths": ["seneschal/scripts/telegram_send.py"], "state": "OPEN",
             "title": "a functional change", "url": "https://github.com/example/repo/pull/479",
             "base": "develop", "body": "Fixes the thing."}

    def test_the_argv_asks_for_the_pull_requests_topic(self):
        argv = mg.request_argv(479, self.FACTS, ["seneschal/scripts/telegram_send.py"], self.dir)
        self.assertIn("--topic", argv)
        self.assertEqual(argv[argv.index("--topic") + 1], tt.TOPIC_PULL_REQUESTS)

    def test_the_purpose_is_spelled_once(self):
        """`merge_guard` reads `telegram_topics`' constant rather than a literal, so the routing
        table cannot drift from its one caller."""
        argv = mg.request_argv(479, self.FACTS, ["seneschal/scripts/telegram_send.py"], self.dir)
        self.assertIn(argv[argv.index("--topic") + 1], tt.TOPIC_NAMES)

    def test_an_unknown_purpose_still_sends(self):
        """`--topic` has no argparse `choices` deliberately: a typo must cost the thread, never the
        picker. Proven at the resolver, which is where a bad purpose lands."""
        self.assertIsNone(tt.thread_id(_cfg(), "typo", self.dir, api=FakeApi(), now=NOW))

    def test_the_merge_picker_still_lands_in_pull_requests_after_the_default_landed(self):
        """**VERIFIED, not assumed.** `decisions` is `telegram_ask`'s default, and the merge picker
        must keep going to its own thread — so the
        real `request_argv` output is parsed by the real `telegram_ask` parser, which is the only
        form that can tell *"the flag is present"* from *"the flag still beats the default."*

        Reading the explicit `--topic` as redundant and deleting it would move every merge approval
        into the decisions topic, silently and with every test above still green."""
        self.assertNotEqual(tt.TOPIC_PULL_REQUESTS, tt.DEFAULT_TOPIC)
        argv = mg.request_argv(479, self.FACTS, ["seneschal/scripts/telegram_send.py"], self.dir)
        # Everything up to the subcommand is the interpreter + script path; the parser reads the rest.
        parsed = ta.build_parser().parse_args(argv[argv.index("ask"):])
        self.assertEqual(parsed.topic, tt.TOPIC_PULL_REQUESTS)


# ------------------------------------------------------------- a topic minted at runtime

class RuntimeOverlay(TopicCase):
    """`_load_topic_names` merges the reference table with rows minted at runtime, in the SAME
    `state/telegram-topics.json` `create_topic` already writes `message_thread_id` into — no second
    file, no edit to a table file. Base wins on a shared key; a nameless or retired row does not resolve; `main` still
    can't be smuggled in from either side."""

    def test_a_state_only_purpose_resolves_with_no_reference_row(self):
        self.assertNotIn("projects", tt._load_base_topic_names())
        tt.save_state(self.dir, {"topics": {"projects": {"name": "Projects"}}})
        self.assertEqual(tt._load_topic_names(state_dir=self.dir).get("projects"), "Projects")

    def test_the_reference_table_wins_on_a_shared_key(self):
        tt.save_state(self.dir, {"topics": {tt.TOPIC_DECISIONS: {"name": "Stale Name"}}})
        names = tt._load_topic_names(state_dir=self.dir)
        self.assertEqual(names[tt.TOPIC_DECISIONS], tt.TOPIC_NAMES[tt.TOPIC_DECISIONS])

    def test_a_row_with_no_name_yet_is_not_a_resolvable_purpose(self):
        """`create_topic` writes `message_thread_id` before it ever has a `name`-less row to worry
        about, but a hand-edited or partially-written state file must not resolve on id alone."""
        tt.save_state(self.dir, {"topics": {"no-name-yet": {"message_thread_id": 5}}})
        self.assertNotIn("no-name-yet", tt._load_topic_names(state_dir=self.dir))

    def test_a_retired_row_no_longer_resolves(self):
        tt.save_state(self.dir, {"topics": {"projects": {"name": "Projects", "retired": True}}})
        self.assertNotIn("projects", tt._load_topic_names(state_dir=self.dir))

    def test_main_in_the_state_file_is_never_a_routable_row(self):
        tt.save_state(self.dir, {"topics": {tt.TOPIC_MAIN_CHAT: {"name": "Main Chat"}}})
        self.assertNotIn(tt.TOPIC_MAIN_CHAT, tt._load_topic_names(state_dir=self.dir))

    def test_a_corrupt_state_file_degrades_to_the_reference_table_only(self):
        with open(tt.state_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        self.assertEqual(tt._load_topic_names(state_dir=self.dir), tt._load_base_topic_names())

    def test_a_missing_state_file_degrades_to_the_reference_table_only(self):
        self.assertEqual(tt._load_topic_names(state_dir=self.dir), tt._load_base_topic_names())

    def test_a_runtime_purpose_is_creatable_through_thread_id(self):
        """The overlay is not just a name lookup — a minted purpose with no thread yet goes through
        the exact same `create_topic` fail-open ladder every reference row already does."""
        tt.save_state(self.dir, {"topics": {"projects": {"name": "Projects"}}})
        self.seed_detect(True)
        api = FakeApi(thread=55)
        tid = tt.thread_id(_cfg(), "projects", self.dir, api=api, now=NOW)
        self.assertEqual(tid, 55)
        self.assertEqual(api.params("createForumTopic")[0]["name"], "Projects")


class MintCLI(TopicCase):
    """`telegram_topics.py add` / `list` / `retire` — no file edit and no reload for one purpose."""

    def _args(self, argv):
        return tt.build_parser().parse_args(
            ["--state-dir", self.dir, "--references-dir", tt.REFERENCES_DIR, *argv])

    def _stdout(self, fn) -> dict:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn()
        return rc, json.loads(buf.getvalue())

    # -- add: validation

    def test_add_refuses_a_slug_with_uppercase_or_punctuation(self):
        rc, out = self._stdout(lambda: tt._cmd_add(self._args(["add", "Bad Slug!", "Title"])))
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])
        self.assertNotIn("Bad Slug!", tt._load_topic_names(state_dir=self.dir))

    def test_add_refuses_main(self):
        rc, out = self._stdout(lambda: tt._cmd_add(self._args(["add", "main", "Main"])))
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])

    def test_add_refuses_a_purpose_already_in_the_reference_table(self):
        rc, out = self._stdout(
            lambda: tt._cmd_add(self._args(["add", tt.TOPIC_DECISIONS, "Decisions"])))
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])

    def test_add_refuses_a_purpose_already_in_the_overlay(self):
        tt.save_state(self.dir, {"topics": {"projects": {"name": "Projects"}}})
        rc, out = self._stdout(lambda: tt._cmd_add(self._args(["add", "projects", "Again"])))
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])

    # -- add: the happy and unhappy creation paths, `create_topic` stubbed so no network is touched

    def test_add_calls_create_topic_once_and_records_the_id(self):
        calls = []

        def fake_create_topic(c, state_dir, purpose, api=None, now=None, log=None,
                              references_dir=tt.REFERENCES_DIR, name=None):
            calls.append((purpose, name))
            tt.save_state(state_dir, {"topics": {purpose: {"message_thread_id": 71, "name": name,
                                                            "created_at": tt._stamp(NOW)}}})
            return 71

        with mock.patch("telegram_send.load_env", return_value={"TELEGRAM_BOT_TOKEN": "tok"}), \
                mock.patch.object(tt, "create_topic", fake_create_topic):
            rc, out = self._stdout(
                lambda: tt._cmd_add(self._args(["add", "projects", "Projects", "--chat-id", "555"])))
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["message_thread_id"], 71)
        self.assertEqual(calls, [("projects", "Projects")])   # exactly once
        self.assertEqual(self.state()["topics"]["projects"]["message_thread_id"], 71)

    def test_add_reports_failure_without_pretending_the_purpose_now_exists(self):
        with mock.patch("telegram_send.load_env", return_value={"TELEGRAM_BOT_TOKEN": "tok"}), \
                mock.patch.object(tt, "create_topic", lambda *a, **kw: None):
            rc, out = self._stdout(
                lambda: tt._cmd_add(self._args(["add", "projects", "Projects", "--chat-id", "555"])))
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(self.state()["topics"], {})

    def test_add_refuses_with_no_token_configured(self):
        with mock.patch("telegram_send.load_env", return_value={}):
            rc, out = self._stdout(
                lambda: tt._cmd_add(self._args(["add", "projects", "Projects", "--chat-id", "555"])))
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])

    # -- list

    def test_list_marks_provenance_and_thread_ids(self):
        tt.save_state(self.dir, {"topics": {
            "projects": {"name": "Projects", "message_thread_id": 9},
            tt.TOPIC_DECISIONS: {"name": "Decisions", "message_thread_id": 3},
        }})
        rc, out = self._stdout(lambda: tt._cmd_list(self._args(["list"])))
        self.assertEqual(rc, 0)
        rows = {r["purpose"]: r for r in out["topics"]}
        self.assertEqual(rows["projects"]["source"], "runtime")
        self.assertEqual(rows["projects"]["message_thread_id"], 9)
        self.assertEqual(rows[tt.TOPIC_DECISIONS]["source"], "reference")
        self.assertEqual(rows[tt.TOPIC_PULL_REQUESTS]["source"], "reference")
        self.assertIsNone(rows[tt.TOPIC_PULL_REQUESTS]["message_thread_id"])  # never created yet

    # -- retire

    def test_retire_refuses_a_reference_purpose(self):
        reason = tt.retire_overlay_purpose(self.dir, tt.TOPIC_DECISIONS)
        self.assertIsNotNone(reason)
        self.assertIn(tt.TOPIC_DECISIONS, tt._load_topic_names(state_dir=self.dir))

    def test_retire_refuses_an_unknown_purpose(self):
        self.assertIsNotNone(tt.retire_overlay_purpose(self.dir, "never-minted"))

    def test_retire_marks_without_deleting_the_row_or_its_thread(self):
        tt.save_state(self.dir, {"topics": {"projects": {"name": "Projects", "message_thread_id": 9}}})
        self.assertIsNone(tt.retire_overlay_purpose(self.dir, "projects", now=NOW))
        row = self.state()["topics"]["projects"]
        self.assertTrue(row["retired"])
        self.assertEqual(row["message_thread_id"], 9)   # the Telegram-side topic is never touched
        self.assertNotIn("projects", tt._load_topic_names(state_dir=self.dir))

    def test_retire_via_the_cli(self):
        tt.save_state(self.dir, {"topics": {"projects": {"name": "Projects"}}})
        rc, out = self._stdout(lambda: tt._cmd_retire(self._args(["retire", "projects"])))
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertTrue(self.state()["topics"]["projects"]["retired"])


# --------------------------------------------------------------------------- inbound

class Inbound(TopicCase):
    """Surfacing only. The chat id does not change when a topic is used, so the allowlist, the offset
    and every downstream consumer behave exactly as before — nothing was ever at risk of being
    dropped, and this makes the fact legible rather than adding a second routing rule."""

    def test_a_topic_message_carries_its_thread(self):
        self.assertEqual(tp.extract_thread_id({"message_thread_id": THREAD}), THREAD)

    def test_a_main_chat_message_carries_nothing(self):
        self.assertIsNone(tp.extract_thread_id({"text": "hi"}))

    def test_zero_and_nonsense_are_not_threads(self):
        for raw in (0, -1, True, "17", None, {}):
            with self.subTest(raw=raw):
                self.assertIsNone(tp.extract_thread_id({"message_thread_id": raw}))


if __name__ == "__main__":
    unittest.main()
