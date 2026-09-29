#!/usr/bin/env python3
"""Tests for **the reminder nudges having their own Telegram topic**.

A ⏰ nudge arrives on a schedule the owner did not pick the moment of, so it lands in the middle of
whatever they were saying; the live-session gate defers non-piercing nudges *while a session is
live*, but that is a delay, not a place to put them. `telegram_topics.TOPIC_REMINDERS` is the place.

**Telegram only.** Discord is out of scope by decision. `sentinel._deliver_reminder`'s Discord and
call branches are asserted UNCHANGED below, and there is deliberately no Discord flag, purpose or stub
anywhere in this feature.

## The three things this file exists to prove

**1. A REMINDER IS NEVER LOST TO A TOPIC FAILURE.** Topics are gated on a @BotFather Mini App toggle
no code can flip, and `topics_enabled` is a runtime detect. So `FailsOpen` walks every rung — topics
off, an unreachable `getMe`, a pre-9.3 bot, a corrupt state file, a creation that failed, a
`telegram_topics` that will not even import, a stored id Telegram now refuses — and asserts at each
one that **the message still goes**, to the main chat. A dropped critical nudge is the worst outcome
available here, and it is strictly worse than the interruption this feature removes.

**2. THE ACK ROUND-TRIP STILL WORKS FROM INSIDE THE TOPIC.** `AckRoundTrip`: the owner acks by
reacting 👍, by typing, and by tapping a picker, and every one of those must still resolve to the
right ⏰ row when both the nudge and the reply live in a thread. (The daemon-side halves — the
reaction ack writing through, the per-topic continuity cache — are daemon wiring and are tested with
the daemon.)

**3. CRITICAL / CALL-ME GO TO THE TOPIC.** A topic message is visible in the topic AND in the main
chat on the Telegram client, so the topic is the WIDER audience. `TheSplit` pins that decision, keeps
the other answer covered by exercising the constant patched to `True`, and asserts that ONE constant
(`telegram_topics.REMINDERS_PIERCING_TO_MAIN_CHAT`) decides it — counted off the bytecode.

**NO NETWORK, EVER, AND NO WRITE OUTSIDE A TEMP DIR.** Every Bot API call goes through an injected
or patched seam; `telegram_send.api_call` is never reached. A live daemon may be holding a real
conversation on the configured bot token, and a test that messages the owner for real is a bug.

Run:  python -m unittest seneschal.scripts.test_reminder_topic   (or)   python test_reminder_topic.py
"""
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

import presence as pr  # noqa: E402
import reminders_acks as ra  # noqa: E402
import sentinel as sn  # noqa: E402
import telegram_ask as ta  # noqa: E402
import telegram_poll as tp  # noqa: E402
import telegram_send as ts  # noqa: E402
import telegram_topics as tt  # noqa: E402

NOW = datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc)
THREAD = 91
CHAT = "555"
#: A 32-hex Notion page id shape, which is what a ⏰ row id looks like to `ack.py`'s id cache.
ROW = "1234567890abcdef1234567890abcdef"


def _cfg(chat_id=CHAT):
    return {"token": "tok", "chat_id": chat_id, "api_base": "https://api.telegram.org",
            "parse_mode": "", "format": ""}


def _z(dt):
    return dt.isoformat().replace("+00:00", "Z")


class FakeApi:
    """`telegram_send.api_call`'s stand-in. Records every call; answers the three methods this
    feature touches. `topics=None` makes `getMe` answer like a pre-9.3 bot, which is *unknown* and
    must never be read as *off*."""

    def __init__(self, topics=True, thread=THREAD, fail=(), message_id=4242):
        self.calls = []
        self.topics = topics
        self.thread = thread
        self.fail = dict(fail)
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

    def sends(self):
        return [p for m, p in self.calls if m == "sendMessage"]


class TopicTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def seed_detect(self, enabled, at=NOW):
        st = tt.load_state(self.dir)
        st["detect"] = {"enabled": enabled, "checked_at": tt._stamp(at)}
        tt.save_state(self.dir, st)

    def seed_topic(self, tid=THREAD, purpose=None):
        purpose = purpose or tt.TOPIC_REMINDERS
        st = tt.load_state(self.dir)
        st["topics"][purpose] = {"message_thread_id": tid, "name": "Reminders",
                                 "created_at": tt._stamp(NOW)}
        tt.save_state(self.dir, st)

    def send(self, api, *extra, text="⏰ Reminder: Water the plants."):
        """`telegram_send.main` end to end through the REAL argv parser, with only the Bot API
        replaced. Everything the daemon's send does — the ack gate, the chunk plan, the fallback
        ladder, the result JSON — runs exactly as it does live."""
        argv = ["--text", text, "--chat-id", CHAT, "--state-dir", self.dir, *extra]
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ts, "api_call", api), \
                mock.patch.object(sys, "argv", ["telegram_send.py", *argv]), \
                mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok",
                                             "TELEGRAM_CHAT_ID": CHAT}, clear=False), \
                mock.patch.object(sys, "stdout", buf), mock.patch.object(sys, "stderr", err):
            code = ts.main()
        return code, json.loads(buf.getvalue().strip().splitlines()[-1]), err.getvalue()


# --------------------------------------------------------------------------- the routing table

class TheRoutingTable(TopicTestCase):
    """The `reminders` row itself, and what adding it did NOT change."""

    def test_reminders_is_a_row_with_its_own_name(self):
        self.assertEqual(tt.TOPIC_NAMES[tt.TOPIC_REMINDERS], "Reminders")
        self.assertEqual(tt.TOPIC_REMINDERS, "reminders")

    def test_the_picker_default_did_not_move(self):
        """A nudge reaches this table through `reminder_topic`, never through the default — so the
        pickers' `decisions` default is untouched and a caller that names no purpose still gets it.
        Adding a row that silently re-homed every picker would be a different feature."""
        self.assertEqual(tt.DEFAULT_TOPIC, tt.TOPIC_DECISIONS)

    def test_the_other_two_rows_are_untouched(self):
        self.assertEqual(tt.TOPIC_NAMES[tt.TOPIC_PULL_REQUESTS], "Pull requests")
        self.assertEqual(tt.TOPIC_NAMES[tt.TOPIC_DECISIONS], "Decisions")

    def test_main_is_still_not_a_row(self):
        """`TOPIC_MAIN_CHAT` resolves through the unknown-purpose branch, which is what makes *"the
        main chat, deliberately"* and *"a typo"* land in the same place. The reminders row must not
        have quietly turned it into one."""
        self.assertNotIn(tt.TOPIC_MAIN_CHAT, tt.TOPIC_NAMES)

    def test_three_purposes_are_three_threads_with_three_names(self):
        self.seed_detect(True)
        api = FakeApi(thread=21)
        got = {}
        for i, purpose in enumerate((tt.TOPIC_REMINDERS, tt.TOPIC_DECISIONS,
                                     tt.TOPIC_PULL_REQUESTS)):
            api.thread = 21 + i
            got[purpose] = tt.thread_id(_cfg(), purpose, self.dir, api=api, now=NOW)
        self.assertEqual(got, {tt.TOPIC_REMINDERS: 21, tt.TOPIC_DECISIONS: 22,
                               tt.TOPIC_PULL_REQUESTS: 23})
        self.assertEqual([p["name"] for m, p in api.calls if m == "createForumTopic"],
                         ["Reminders", "Decisions", "Pull requests"])

    def test_no_delete_or_archive_verb_appeared(self):
        """The module's standing refusal, re-asserted because this change added a row to the table
        it guards: close/reopen do not work in private chats and delete is destructive."""
        with io.open(os.path.join(SCRIPT_DIR, "telegram_topics.py"), encoding="utf-8") as fh:
            src = fh.read()
        for verb in ("deleteForumTopic", "closeForumTopic", "reopenForumTopic", "editForumTopic"):
            self.assertNotIn(f'"{verb}"', src)


# --------------------------------------------------------------------------- the split

class TheSplit(TopicTestCase):
    """**Critical / Call-Me go to the topic.**

    The piercing set is defined by what it defeats: `entry_pierces_quiet` names the nudges that come
    through a quiet window, through the night curfew, through a day-off hold and through a
    live-session defer, and every one of those gates keeps noise out of a place the owner is not
    looking. A topic could look like another such place. **It is not, on the Telegram client**: a
    message in a topic is visible BOTH there and in the main chat, while a main-chat message is
    visible only in the main chat — so the topic is the wider audience, and `True` is the less
    visible answer.

    That premise is an observation of the client rather than a Bot API guarantee, so both directions
    stay covered here — the decided behaviour, and the `True` behaviour under a patched constant — and
    the day anyone reconsiders that one line, the other answer is already proved."""

    def test_the_decision_sends_piercing_nudges_to_the_reminders_topic(self):
        self.assertIs(tt.REMINDERS_PIERCING_TO_MAIN_CHAT, False)
        self.assertEqual(tt.reminder_topic(pierces=True), tt.TOPIC_REMINDERS)
        self.assertEqual(tt.reminder_topic(pierces=False), tt.TOPIC_REMINDERS)

    def test_flipping_the_one_constant_back_keeps_them_in_the_main_chat(self):
        """The other answer, still whole and still one line — which is what keeps the decision
        reversible by a decision rather than by a rewrite."""
        with mock.patch.object(tt, "REMINDERS_PIERCING_TO_MAIN_CHAT", True):
            self.assertEqual(tt.reminder_topic(pierces=True), tt.TOPIC_MAIN_CHAT)
            self.assertEqual(tt.reminder_topic(pierces=False), tt.TOPIC_REMINDERS)
            # …and the fire path follows without a second edit: a Critical nudge names no topic at
            # all.
            self.assertIsNone(sn.reminder_topic_purpose(True))

    def test_the_constant_has_exactly_one_reader_in_the_tree(self):
        """*"Flip the line and every nudge moves"* is a claim a second reader makes false, so it is
        asserted rather than hoped for.

        **It counts NAMES THE BYTECODE LOADS, not mentions in the text** — `sentinel` and `presence`
        both point at this constant in a docstring, which is a pointer and not a reader, and a
        grep-based version of this test would either fail on those or be silenced by weakening it
        into uselessness. Compiling each file and walking its code objects asks the question that
        actually matters. Static: nothing here is imported or run."""
        def names(code):
            found = set(code.co_names)
            for const in code.co_consts:
                if hasattr(const, "co_names"):
                    found |= names(const)
            return found

        readers = []
        for name in sorted(os.listdir(SCRIPT_DIR)):
            if not name.endswith(".py") or name == os.path.basename(__file__):
                continue
            with io.open(os.path.join(SCRIPT_DIR, name), encoding="utf-8") as fh:
                src = fh.read()
            try:
                code = compile(src, name, "exec")
            except SyntaxError:  # not ours to judge; a compile gate covers it in CI
                continue
            if "REMINDERS_PIERCING_TO_MAIN_CHAT" in names(code):
                readers.append(name)
        self.assertEqual(readers, ["telegram_topics.py"])

    def test_the_one_reader_is_the_resolver_and_not_a_call_site(self):
        """And inside that module it is `reminder_topic` that reads it — the function every caller
        goes through — rather than a branch at one of them."""
        self.assertIn("REMINDERS_PIERCING_TO_MAIN_CHAT", tt.reminder_topic.__code__.co_names)

    def test_every_way_of_piercing_goes_to_the_topic(self):
        """`entry_pierces_quiet` has three arms — a `call` channel, an escalating entry, and the
        `pierce_quiet` flag the brain stamps for Critical-and-above — and the topic split reads its
        answer rather than re-deriving one, so all three behave identically here."""
        for entry in ({"channel": "call"}, {"escalate": True}, {"pierce_quiet": True}):
            pierces = sn.entry_pierces_quiet(entry)
            self.assertTrue(pierces, entry)
            self.assertEqual(sn.reminder_topic_purpose(pierces), tt.TOPIC_REMINDERS, entry)

    def test_an_ordinary_nudge_is_not_piercing_and_goes_to_the_topic(self):
        entry = {"channel": "telegram", "text": "Drink water."}
        self.assertFalse(sn.entry_pierces_quiet(entry))
        self.assertEqual(sn.reminder_topic_purpose(sn.entry_pierces_quiet(entry)),
                         tt.TOPIC_REMINDERS)

    def test_main_chat_collapses_to_no_purpose_at_all(self):
        """Not `"main"` handed onward: `None`, so the argv carries no flag, no state dir and spends
        no `getMe`. That is what makes the main-chat answer an EQUALITY between two argv lists rather
        than an argument about a branch two functions away.

        Only the flipped constant reaches that branch, so that is how it is reached
        here — the collapse is a property of `TOPIC_MAIN_CHAT` and outlives which answer is current."""
        with mock.patch.object(tt, "REMINDERS_PIERCING_TO_MAIN_CHAT", True):
            self.assertIsNone(sn.reminder_topic_purpose(True))


# --------------------------------------------------------------------------- the argv seam

class TheArgv(TopicTestCase):
    """`sentinel.send_telegram` reaches Telegram by RUNNING `telegram_send.py`, so the argv IS the
    routing decision as far as this process can observe it."""

    def run_deliver(self, entry, channel="telegram"):
        """`_deliver_reminder` with the subprocess replaced, returning the argv it built."""
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            return mock.Mock(stdout=json.dumps({"ok": True, "message_id": 7}), stderr="",
                             returncode=0)

        with mock.patch.object(sn.subprocess, "run", fake_run):
            res, used = sn._deliver_reminder(
                channel, entry.get("text", "x"), "tg.env", None, None,
                pierces=sn.entry_pierces_quiet(entry), state_dir=self.dir)
        return seen.get("argv"), res, used

    def test_a_piercing_nudge_names_the_reminders_topic(self):
        """The decision at the argv, which is the lowest place this process can observe it: the 🚨
        nudge names the reminders topic too."""
        argv, _, used = self.run_deliver({"text": "Pay rent.", "pierce_quiet": True})
        self.assertEqual(used, "telegram")
        self.assertEqual(argv[argv.index("--topic") + 1], tt.TOPIC_REMINDERS)
        self.assertEqual(argv[argv.index("--state-dir") + 1], self.dir)

    def test_the_main_chat_answer_still_builds_byte_identically_to_the_pre_topics_argv(self):
        """The other answer in full: not *"it ends up in the main chat"* but *"it sends the argv it
        sent before topics existed"*, asserted as a list equality. Reached only through the flipped
        constant, and kept because that is a decision the owner could revisit, not dead code — this is
        what the revisit would already have proved."""
        with mock.patch.object(tt, "REMINDERS_PIERCING_TO_MAIN_CHAT", True):
            pierce_argv, _, _ = self.run_deliver({"text": "Pay rent.", "pierce_quiet": True})
        expected = [sys.executable, os.path.join(SCRIPT_DIR, "telegram_send.py"),
                    "--text", "⏰ Reminder: Pay rent.", "--env-file", "tg.env"]
        self.assertEqual(pierce_argv, expected)

    def test_an_ordinary_nudge_names_the_reminders_topic_and_its_state_dir(self):
        argv, _, used = self.run_deliver({"text": "Drink water."})
        self.assertEqual(used, "telegram")
        self.assertIn("--topic", argv)
        self.assertEqual(argv[argv.index("--topic") + 1], tt.TOPIC_REMINDERS)
        self.assertEqual(argv[argv.index("--state-dir") + 1], self.dir)

    def test_the_state_dir_rides_only_when_the_topic_does(self):
        """It is what makes the purpose resolvable, so it travels with it — and adding it
        unconditionally would break the equality above. The property belongs to `send_telegram`
        rather than to whichever answer is current, so it is asserted on the no-topic case wherever
        that case now lives."""
        with mock.patch.object(tt, "REMINDERS_PIERCING_TO_MAIN_CHAT", True):
            pierce_argv, _, _ = self.run_deliver({"text": "x", "pierce_quiet": True})
        self.assertNotIn("--state-dir", pierce_argv)
        self.assertNotIn("--topic", pierce_argv)

    def test_send_telegram_with_no_topic_is_the_two_argument_call_it_always_was(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            return mock.Mock(stdout='{"ok": true}', stderr="", returncode=0)

        with mock.patch.object(sn.subprocess, "run", fake_run):
            sn.send_telegram("hello", "tg.env")
        self.assertEqual(seen["argv"], [sys.executable,
                                        os.path.join(SCRIPT_DIR, "telegram_send.py"),
                                        "--text", "hello", "--env-file", "tg.env"])

    def test_an_explicit_thread_id_and_a_purpose_are_different_arguments(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            return mock.Mock(stdout='{"ok": true}', stderr="", returncode=0)

        with mock.patch.object(sn.subprocess, "run", fake_run):
            sn.send_telegram("hi", "tg.env", message_thread_id=5)
        self.assertIn("--message-thread-id", seen["argv"])
        self.assertNotIn("--topic", seen["argv"])

    def test_discord_is_out_of_scope_and_its_branch_is_untouched(self):
        """Telegram only, by decision. Not overlooked — and deliberately with no flag waiting for it, because a half-built second channel is where a fail-open ladder rots
        unobserved."""
        seen = {}
        with mock.patch.object(sn, "send_discord",
                               lambda text, env: seen.setdefault("d", (text, env)) or {"ok": True}):
            res, used = sn._deliver_reminder("discord", "Drink water.", "tg.env", None, "dc.env",
                                             pierces=False, state_dir=self.dir)
        self.assertEqual(used, "discord")
        self.assertEqual(seen["d"], ("⏰ Reminder: Drink water.", "dc.env"))

    def test_a_call_is_untouched_and_still_speaks_the_raw_line(self):
        seen = {}
        with mock.patch.object(sn, "send_call",
                               lambda raw, env, **kw: seen.setdefault("c", raw) or {"ok": True}):
            res, used = sn._deliver_reminder("call", "Pay rent.", "tg.env", "call.env", None,
                                             pierces=True, state_dir=self.dir)
        self.assertEqual((used, seen["c"]), ("call", "Pay rent."))


# --------------------------------------------------------------------------- fail-open

class FailsOpen(TopicTestCase):
    """**Every rung, and at every one the nudge still arrives.** Constraint 1: a dropped critical
    nudge is the worst outcome here, so a topic may only ever change WHERE a nudge lands."""

    def assert_landed_in_main_chat(self, code, out, api):
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"] and out["sent"])
        self.assertNotIn("message_thread_id", out)
        sends = api.sends()
        self.assertEqual(len(sends), 1)
        self.assertNotIn("message_thread_id", sends[0])

    def test_topics_off_is_the_main_chat_and_the_payload_is_byte_identical(self):
        """The acceptance bar: the @BotFather Mini App toggle is off on a fresh bot, so this is the
        state the feature ships into. Asserted as an EQUALITY between two whole payloads."""
        without = FakeApi()
        code_a, _, _ = self.send(without)
        with_topic = FakeApi(topics=False)
        code_b, out, _ = self.send(with_topic, "--topic", tt.TOPIC_REMINDERS)
        self.assertEqual((code_a, code_b), (0, 0))
        self.assertEqual(with_topic.sends(), without.sends())
        self.assertNotIn("message_thread_id", out)
        self.assertNotIn("createForumTopic", with_topic.methods())

    def test_getme_unreachable_is_the_main_chat(self):
        api = FakeApi(fail={"getMe": RuntimeError("no network")})
        code, out, _ = self.send(api, "--topic", tt.TOPIC_REMINDERS)
        self.assert_landed_in_main_chat(code, out, api)

    def test_a_pre_9_3_bot_reads_as_unknown_not_as_off(self):
        api = FakeApi(topics=None)
        code, out, _ = self.send(api, "--topic", tt.TOPIC_REMINDERS)
        self.assert_landed_in_main_chat(code, out, api)
        self.assertEqual(tt.load_state(self.dir)["detect"], {})  # and it is NOT cached

    def test_a_failed_creation_is_the_main_chat(self):
        api = FakeApi(fail={"createForumTopic": RuntimeError("nope")})
        code, out, _ = self.send(api, "--topic", tt.TOPIC_REMINDERS)
        self.assert_landed_in_main_chat(code, out, api)

    def test_a_corrupt_state_file_costs_the_thread_and_never_the_nudge(self):
        with io.open(tt.state_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        api = FakeApi()
        code, out, _ = self.send(api, "--topic", tt.TOPIC_REMINDERS)
        # It cannot READ the stored id, so it creates one — and the nudge lands either way.
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])

    def test_an_unknown_purpose_is_the_main_chat_not_an_error(self):
        api = FakeApi()
        code, out, _ = self.send(api, "--topic", "reminderz")
        self.assert_landed_in_main_chat(code, out, api)
        self.assertNotIn("getMe", api.methods())  # an unknown purpose does not even ask

    def test_a_telegram_topics_that_explodes_costs_the_topic_and_not_the_message(self):
        api = FakeApi()
        with mock.patch.object(tt, "thread_id", side_effect=RuntimeError("module is broken")):
            code, out, err = self.send(api, "--topic", tt.TOPIC_REMINDERS)
        self.assert_landed_in_main_chat(code, out, api)
        self.assertIn("main chat", err)

    def test_sentinel_falls_back_when_the_topics_module_cannot_be_reached(self):
        """One rung further out than the CLI's: if `telegram_topics` cannot even be consulted,
        `reminder_topic_purpose` answers `None` and the argv carries no flag at all."""
        with mock.patch.object(tt, "reminder_topic", side_effect=RuntimeError("boom")):
            self.assertIsNone(sn.reminder_topic_purpose(False))

    def test_topics_on_creates_once_persists_and_reuses(self):
        api = FakeApi()
        code, out, _ = self.send(api, "--topic", tt.TOPIC_REMINDERS)
        self.assertEqual(code, 0)
        self.assertEqual(out["message_thread_id"], THREAD)
        self.assertEqual(api.sends()[0]["message_thread_id"], THREAD)
        row = tt.load_state(self.dir)["topics"][tt.TOPIC_REMINDERS]
        self.assertEqual((row["message_thread_id"], row["name"]), (THREAD, "Reminders"))

        again = FakeApi()
        code, out, _ = self.send(again, "--topic", tt.TOPIC_REMINDERS)
        self.assertEqual(out["message_thread_id"], THREAD)
        self.assertNotIn("createForumTopic", again.methods())

    def test_an_explicit_thread_id_wins_over_a_purpose_and_resolves_nothing(self):
        """A caller that already knows the thread is not asking a question, so naming both must not
        spend a `getMe` and must not be able to CREATE a topic. Added after a mutation run: nothing
        covered the two flags TOGETHER, so `if thread is None and args.topic` could be weakened to
        `if args.topic` with the suite still green — and that weakening silently overrides every
        caller that resolved its own thread."""
        api = FakeApi()
        code, out, _ = self.send(api, "--message-thread-id", "31", "--topic", tt.TOPIC_REMINDERS)
        self.assertEqual((code, out["message_thread_id"]), (0, 31))
        self.assertEqual(api.sends()[0]["message_thread_id"], 31)
        self.assertEqual(api.methods(), ["sendMessage"])
        self.assertEqual(tt.load_state(self.dir)["topics"], {})

    def test_a_dry_run_names_the_purpose_and_resolves_nothing(self):
        """Resolving a purpose can CREATE a topic, which is exactly what `--dry-run` promises not to
        do — `telegram_ask._cmd_ask` draws the same line."""
        api = FakeApi()
        code, out, _ = self.send(api, "--topic", tt.TOPIC_REMINDERS, "--dry-run")
        self.assertEqual((code, out["topic"]), (0, tt.TOPIC_REMINDERS))
        self.assertEqual(api.calls, [])
        self.assertEqual(tt.load_state(self.dir)["topics"], {})


# --------------------------------------------------------------------------- a stale thread

class StaleThread(TopicTestCase):
    """The owner deleted the topic, or the bot was re-pointed. Telegram REFUSES the send, so nothing was
    delivered and re-sending into the main chat cannot duplicate anything."""

    def rejecting_api(self):
        api = FakeApi()
        api.fail["sendMessage"] = ts.TelegramAPIError(
            "Telegram sendMessage failed: message thread not found",
            method="sendMessage", description="message thread not found")
        return api

    def test_the_nudge_falls_back_to_the_main_chat_and_still_sends(self):
        self.seed_detect(True)
        self.seed_topic(THREAD)
        api = self.rejecting_api()
        calls = {"n": 0}
        real = api.__call__

        def once_rejecting(c, method, params, timeout=30):
            if method == "sendMessage":
                calls["n"] += 1
                if params.get("message_thread_id") is not None:
                    raise api.fail["sendMessage"]
                api.calls.append((method, params))
                return {"ok": True, "result": {"message_id": 77}}
            return real(c, method, params, timeout)

        code, out, err = self.send(once_rejecting, "--topic", tt.TOPIC_REMINDERS)
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"] and out["sent"])
        self.assertTrue(out["thread_fallback"])
        self.assertNotIn("message_thread_id", out)  # it reports where it ACTUALLY went
        self.assertIn("stale", err)

    def test_the_stale_id_is_forgotten_so_the_next_nudge_creates_a_fresh_topic(self):
        self.seed_detect(True)
        self.seed_topic(THREAD)
        api = FakeApi()
        real = api.__call__

        def once_rejecting(c, method, params, timeout=30):
            if method == "sendMessage" and params.get("message_thread_id") is not None:
                raise ts.TelegramAPIError("gone", method="sendMessage", description="gone")
            return real(c, method, params, timeout)

        self.send(once_rejecting, "--topic", tt.TOPIC_REMINDERS)
        self.assertNotIn(tt.TOPIC_REMINDERS, tt.load_state(self.dir)["topics"])

    def test_an_ambiguous_failure_is_never_re_sent(self):
        """The request went out and the nudge may already be on the owner's phone. Under-sending is
        recoverable; double-sending is not, on the channel carrying critical reminders."""
        self.seed_detect(True)
        self.seed_topic(THREAD)
        sends = {"n": 0}

        def ambiguous(c, method, params, timeout=30):
            if method == "sendMessage":
                sends["n"] += 1
                raise RuntimeError("connection reset after the request went out")
            return FakeApi()(c, method, params, timeout)

        code, out, _ = self.send(ambiguous, "--topic", tt.TOPIC_REMINDERS)
        self.assertEqual((code, sends["n"]), (1, 1))
        self.assertFalse(out["ok"])
        self.assertTrue(out.get("ambiguous"))
        # And the id is NOT forgotten: nothing proved the thread was the problem.
        self.assertIn(tt.TOPIC_REMINDERS, tt.load_state(self.dir)["topics"])

    def test_a_rejected_multi_chunk_message_is_not_re_sent_either(self):
        """`send_text` re-raises without saying WHICH chunk failed, so this frame cannot prove
        nothing landed — and re-sending a message whose first chunk arrived is the duplicate every
        other rule in that file exists to prevent."""
        self.seed_detect(True)
        self.seed_topic(THREAD)
        sends = {"n": 0}

        def rejecting(c, method, params, timeout=30):
            if method == "sendMessage":
                sends["n"] += 1
                raise ts.TelegramAPIError("no thread", method="sendMessage",
                                          description="message thread not found")
            return FakeApi()(c, method, params, timeout)

        code, out, err = self.send(rejecting, "--topic", tt.TOPIC_REMINDERS, text="y" * 9000)
        self.assertEqual((code, sends["n"]), (1, 1))
        self.assertFalse(out["ok"])
        self.assertIn("multi-chunk", err)

    def test_an_explicit_thread_id_still_falls_back_on_nothing(self):
        """`--message-thread-id`'s contract is unchanged: a caller that named a raw id owns the
        decision, because only that caller knows whether the words are worth re-sending."""
        sends = {"n": 0}

        def rejecting(c, method, params, timeout=30):
            if method == "sendMessage":
                sends["n"] += 1
                raise ts.TelegramAPIError("no thread", method="sendMessage",
                                          description="message thread not found")
            return FakeApi()(c, method, params, timeout)

        code, out, _ = self.send(rejecting, "--message-thread-id", "31")
        self.assertEqual((code, sends["n"]), (1, 1))
        self.assertFalse(out["ok"])


# --------------------------------------------------------------------------- the fire path

class TheFirePath(TopicTestCase):
    """`check_reminders`, the single delivery chokepoint, with only the subprocess replaced — so the
    whole gate order runs and the entry really is stamped."""

    def fire(self, entries, reply=None, now=NOW):
        sent = []

        def fake_run(argv, **kw):
            sent.append(list(argv))
            body = reply if reply is not None else {"ok": True, "message_id": 7}
            return mock.Mock(stdout=json.dumps(body), stderr="", returncode=0)

        sn.save_json(os.path.join(self.dir, "reminders.json"), entries)
        with mock.patch.object(sn.subprocess, "run", fake_run):
            signals = sn.check_reminders(self.dir, now, fire=True, telegram_env="tg.env")
        rows = {r["id"]: r for r in sn.load_json(os.path.join(self.dir, "reminders.json"), [])}
        return signals, rows, sent

    def entry(self, **over):
        e = {"id": "n1", "reminder_id": ROW, "text": "Drink water.",
             "due_at": _z(NOW - timedelta(minutes=5)), "channel": "telegram"}
        e.update(over)
        return e

    def test_an_ordinary_nudge_fires_into_the_topic_and_is_stamped(self):
        signals, rows, sent = self.fire([self.entry()],
                                        reply={"ok": True, "message_id": 7,
                                               "message_thread_id": THREAD})
        self.assertEqual([s["kind"] for s in signals], ["reminder_fired"])
        self.assertEqual(signals[0]["message_thread_id"], THREAD)
        self.assertTrue(rows["n1"]["fired_at"])
        self.assertIn("--topic", sent[0])

    def test_a_topic_failure_still_fires_and_the_signal_names_no_thread(self):
        """The whole of constraint 1 at the fire path: `telegram_send` fell back to the main chat,
        so the nudge landed, `fired_at` is stamped, and nothing claims a thread."""
        signals, rows, _ = self.fire([self.entry()],
                                     reply={"ok": True, "message_id": 7, "thread_fallback": True})
        self.assertEqual(signals[0]["kind"], "reminder_fired")
        self.assertNotIn("message_thread_id", signals[0])
        self.assertTrue(rows["n1"]["fired_at"])

    def test_a_piercing_nudge_fires_into_the_topic(self):
        signals, rows, sent = self.fire([self.entry(pierce_quiet=True, text="Pay rent.")],
                                        reply={"ok": True, "message_id": 7,
                                               "message_thread_id": THREAD})
        self.assertEqual(signals[0]["kind"], "reminder_fired")
        self.assertEqual(sent[0][sent[0].index("--topic") + 1], tt.TOPIC_REMINDERS)
        self.assertEqual(signals[0]["message_thread_id"], THREAD)
        self.assertTrue(rows["n1"]["fired_at"])

    def test_a_piercing_nudge_with_the_constant_flipped_back_fires_with_no_topic_flag(self):
        """The other answer at the fire path, so the whole chain is proved in both directions and
        not just the pure function at the top of it."""
        with mock.patch.object(tt, "REMINDERS_PIERCING_TO_MAIN_CHAT", True):
            signals, rows, sent = self.fire([self.entry(pierce_quiet=True, text="Pay rent.")])
        self.assertEqual(signals[0]["kind"], "reminder_fired")
        self.assertNotIn("--topic", sent[0])
        self.assertTrue(rows["n1"]["fired_at"])

    def test_a_failed_send_is_still_a_failed_send(self):
        signals, rows, _ = self.fire([self.entry()], reply={"ok": False, "error": "down"})
        self.assertEqual(signals[0]["kind"], "reminder_send_failed")
        self.assertIsNone(rows["n1"].get("fired_at"))

    def test_a_nonsense_thread_in_the_result_is_not_recorded_as_one(self):
        for junk in (0, -1, True, "91", None, {"a": 1}):
            with self.subTest(junk=junk):
                signals, _, _ = self.fire([self.entry()],
                                          reply={"ok": True, "message_id": 7,
                                                 "message_thread_id": junk})
                self.assertNotIn("message_thread_id", signals[0])

    def test_the_gates_above_it_are_untouched(self):
        """A quiet window still drops a non-piercing nudge before delivery, so nothing reaches the
        topic decision at all — the split may not have moved a gate."""
        sn.set_quiet(self.dir, NOW + timedelta(hours=2), reason="test")
        signals, rows, sent = self.fire([self.entry()])
        self.assertEqual(signals[0]["kind"], "reminder_suppressed_quiet")
        self.assertEqual(sent, [])
        self.assertTrue(rows["n1"]["suppressed_at"])


# --------------------------------------------------------------------------- THE ACK ROUND-TRIP

class AckRoundTrip(TopicTestCase):
    """**The load-bearing class.** The owner acknowledges reminders by reacting 👍, by typing, and
    by tapping a picker. If a nudge lands in a topic and the reply lands there too, every one of those
    must still resolve to the right ⏰ row — because *"the reaction path is keyed on `message_id`, so
    a thread cannot matter"* is exactly the kind of claim that is true when it is written and false
    after the next edit."""

    # -- the 👍

    def test_the_sent_message_map_entry_is_identical_whether_or_not_it_went_to_a_topic(self):
        """`record_sent_message` keys on `message_id`, which is unique per chat and says nothing
        about threads. This is the join the whole reaction ack hangs off, so it is asserted as an
        equality between the two worlds rather than inspected field by field."""
        sn.record_sent_message(self.dir, {"ok": True, "message_id": 900}, "nudge",
                               "⏰ Reminder: Drink water.", reminder_id=ROW, now=NOW)
        main_entry = sn.load_message_map(self.dir)["900"]

        other = tempfile.mkdtemp(dir=self.dir)
        sn.record_sent_message(other, {"ok": True, "message_id": 900,
                                       "message_thread_id": THREAD}, "nudge",
                               "⏰ Reminder: Drink water.", reminder_id=ROW, now=NOW)
        self.assertEqual(sn.load_message_map(other)["900"], main_entry)

    def test_a_reaction_inside_a_topic_extracts_the_same_item_as_one_in_the_main_chat(self):
        """`MessageReactionUpdated` carries no `message_thread_id` at all, and `extract_reaction`
        gates on the chat allowlist only — so the ack is thread-blind BY CONSTRUCTION. Fed one with
        a thread field bolted on anyway, to prove nothing reads it."""
        def upd(**extra):
            u = {"update_id": 1, "message_reaction": {
                "chat": {"id": int(CHAT), "type": "private"}, "message_id": 900,
                "user": {"username": "owner"}, "date": 1,
                "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}}
            u["message_reaction"].update(extra)
            return u

        allowed = {CHAT}
        plain = tp.extract_reaction(upd(), allowed)
        in_topic = tp.extract_reaction(upd(message_thread_id=THREAD, is_topic_message=True),
                                       allowed)
        self.assertEqual(plain, in_topic)
        self.assertEqual(plain["message_id"], 900)

    def test_a_thumbs_up_on_a_nudge_sent_into_the_topic_resolves_to_its_row(self):
        sn.record_sent_message(self.dir, {"ok": True, "message_id": 900,
                                          "message_thread_id": THREAD}, "nudge",
                               "⏰ Reminder: Water the plants.", reminder_id=ROW, now=NOW)
        ctx = pr.reaction_context(self.dir)
        reaction = {"kind": "reaction", "emoji": "👍", "message_id": 900}
        self.assertEqual(pr.ackable_nudge(reaction, ctx, now=NOW), ROW)

    # -- typing

    def test_a_typed_reply_in_the_topic_carries_the_thread_and_is_never_dropped(self):
        """Delivery is unchanged: the chat id does not move, so the poller's allowlist and the
        offset behave exactly as before — a message from a topic was never at risk of being lost."""
        msg = {"message_id": 12, "chat": {"id": int(CHAT), "type": "private"},
               "from": {"username": "owner"}, "text": "done", "date": 1,
               "message_thread_id": THREAD, "is_topic_message": True}
        self.assertEqual(tp.extract_thread_id(msg), THREAD)
        self.assertIsNone(tp.extract_thread_id({k: v for k, v in msg.items()
                                                if k != "message_thread_id"}))

    # -- a picker tap

    def test_a_tap_from_inside_a_topic_extracts_and_resolves(self):
        """A tap is a `callback_query`, resolved by the question id in `callback_data`;
        `extract_callback` reads the chat and the query id and never a thread. Driven through the
        REAL `telegram_ask.resolve`, so the claim is about the shipped path."""
        api = FakeApi()
        sent = ta.ask(_cfg(), self.dir, "Did you water the plants?",
                      [{"label": "Yes", "description": "Mark the row Done for today."},
                       {"label": "Not yet", "description": "Leave it queued."}],
                      recommend=False, api=api, now=NOW)
        qid = sent["question_id"]

        update = {"update_id": 2, "callback_query": {
            "id": "cbq-1", "from": {"username": "owner"}, "data": f"q:{qid}:0",
            "message": {"message_id": sent["message_id"],
                        "chat": {"id": int(CHAT), "type": "private"},
                        "message_thread_id": THREAD, "is_topic_message": True}}}
        item = tp.extract_callback(update, {CHAT})
        self.assertEqual(item["data"], f"q:{qid}:0")
        self.assertNotIn("message_thread_id", item)  # it never needed one

        res = ta.resolve(_cfg(), self.dir, item["data"], item["callback_id"],
                         message_id=item["message_id"], chat_id=CHAT, api=api, now=NOW)
        self.assertTrue(res["ok"])
        self.assertEqual(res["selected"], [0])
        self.assertIn("answerCallbackQuery", api.methods())


# --------------------------------------------------------------------------- no live sends

class NoLiveSends(unittest.TestCase):
    """A test that messages the owner for real is a bug. The whole feature is exercised through
    injected seams, so state a couple of those as properties rather than as habits."""

    def test_a_send_that_forgot_to_patch_the_api_cannot_reach_the_wire(self):
        """The backstop, stated as a property of the code rather than as care taken by the author:
        the real `api_call` refuses a config with no token before it builds a URL, and every fixture
        here carries a fake one. So a test that forgot to inject its seam fails loudly on this line
        instead of posting into a live conversation."""
        with self.assertRaises(RuntimeError):
            ts.api_call({"token": None, "chat_id": CHAT,
                         "api_base": "https://example.invalid"}, "getMe", {})
        self.assertEqual(_cfg()["token"], "tok")

    def test_the_topic_resolver_cannot_reach_the_wire_either(self):
        """`thread_id` is the one function this feature adds to the send path, and every failure in
        it answers *the main chat* — including an `api` that refuses to talk at all."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def refuse(*a, **kw):
            raise AssertionError("a test reached the network")

        self.assertIsNone(tt.thread_id(_cfg(), tt.TOPIC_REMINDERS, tmp.name,
                                       api=lambda *a, **kw: (_ for _ in ()).throw(
                                           RuntimeError("offline")), now=NOW))
        self.assertIsNone(tt.thread_id(_cfg(), tt.TOPIC_MAIN_CHAT, tmp.name, api=refuse, now=NOW))

    def test_the_two_topic_mechanisms_still_do_not_meet_in_code(self):
        """*Which thread does a PURPOSE belong in* (off a state file) and *which thread did THIS
        MESSAGE arrive in* (off the payload) are different questions, and they touch only at the Bot
        API parameter they both set. The reminders row did not change that — re-asserted here
        because this is where BOTH halves are load-bearing at once (a nudge routed by purpose, the
        owner's ack routed by payload) and that is exactly when someone reaches for
        the other module."""
        with io.open(os.path.join(SCRIPT_DIR, "telegram_topics.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("thread_key", src)
        self.assertNotIn("import presence", src)


if __name__ == "__main__":
    unittest.main()
