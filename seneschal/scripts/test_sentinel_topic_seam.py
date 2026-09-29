#!/usr/bin/env python3
"""The sentinel half of topic-routed reminder nudges — the seam, tested without `telegram_topics`.

`telegram_topics` (the Telegram forum-topic resolver) is an OPTIONAL module that may be absent. The
sentinel reaches it lazily through `reminder_topic_purpose`, and everything here is about what the
sentinel does with (or without) its answer:

* no module (or one that raises) → no topic, and the nudge's argv is byte-identical to the pre-topics
  one — an import may never cost a nudge;
* the main-chat answer COLLAPSES to no flag at all, rather than being passed through;
* `--state-dir` rides only when `--topic` does;
* `pierces` is `entry_pierces_quiet`'s answer passed down, never recomputed;
* `reminder_fired` carries `message_thread_id` read off the SEND RESULT, absent otherwise;
* a `send_telegram` subprocess timeout is `ambiguous`.

A fake `telegram_topics` is injected through `sys.modules` so this runs identically whether or not the
real module is installed. The full routing/stale-thread/continuity suite lives with `telegram_topics`.
"""
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clock  # noqa: E402
import identity_common  # noqa: E402
import sentinel as sn  # noqa: E402
import tz_common  # noqa: E402

NOW = datetime(2026, 9, 1, 17, 0, tzinfo=timezone.utc)  # midday owner-local (pinned UTC-5)


def _fake_topics(main="main", pierce_purpose="reminders", plain_purpose="reminders", explode=False):
    mod = types.ModuleType("telegram_topics")
    mod.TOPIC_MAIN_CHAT = main

    def reminder_topic(pierces):
        if explode:
            raise RuntimeError("boom")
        return pierce_purpose if pierces else plain_purpose

    mod.reminder_topic = reminder_topic
    return mod


class _Seam(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(tz_common, "_zone", return_value=timezone(timedelta(hours=-5))),
                  mock.patch.object(clock, "load_identity", return_value={}),
                  mock.patch.object(identity_common, "load_identity", return_value={})):
            p.start()
            self.addCleanup(p.stop)

    def use_topics(self, mod):
        p = mock.patch.dict(sys.modules, {"telegram_topics": mod})
        p.start()
        self.addCleanup(p.stop)


class ReminderTopicPurpose(_Seam):
    def test_no_module_means_no_topic(self):
        self.use_topics(None)  # a None entry makes `import telegram_topics` raise ImportError
        self.assertIsNone(sn.reminder_topic_purpose(False))
        self.assertIsNone(sn.reminder_topic_purpose(True))

    def test_an_exploding_module_costs_the_topic_not_the_nudge(self):
        self.use_topics(_fake_topics(explode=True))
        self.assertIsNone(sn.reminder_topic_purpose(False))

    def test_the_main_chat_collapses_to_none(self):
        self.use_topics(_fake_topics(pierce_purpose="main"))
        self.assertIsNone(sn.reminder_topic_purpose(True))
        self.assertEqual(sn.reminder_topic_purpose(False), "reminders")


class TheArgv(_Seam):
    def _argv(self, **kw):
        seen = {}

        def fake_run(argv, **_k):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}\n', stderr="")

        with mock.patch.object(sn.subprocess, "run", fake_run):
            sn.send_telegram("hello", "t.env", **kw)
        return seen["argv"]

    def test_no_topic_is_the_two_argument_argv(self):
        base = [sys.executable, os.path.join(sn.SCRIPT_DIR, "telegram_send.py"),
                "--text", "hello", "--env-file", "t.env"]
        self.assertEqual(self._argv(), base)
        self.assertEqual(self._argv(state_dir="/s"), base)  # state_dir alone adds nothing

    def test_a_topic_brings_its_state_dir(self):
        self.assertEqual(self._argv(topic="reminders", state_dir="/s")[-4:],
                         ["--topic", "reminders", "--state-dir", "/s"])

    def test_an_explicit_thread_id_is_a_different_argument(self):
        argv = self._argv(message_thread_id=42)
        self.assertEqual(argv[-2:], ["--message-thread-id", "42"])
        self.assertNotIn("--topic", argv)

    def test_a_timeout_is_ambiguous(self):
        def boom(argv, **_k):
            raise subprocess.TimeoutExpired(argv, 60)

        with mock.patch.object(sn.subprocess, "run", boom):
            res = sn.send_telegram("hello", "t.env")
        self.assertFalse(res["ok"])
        self.assertTrue(res["ambiguous"])


class DeliverAndFire(_Seam):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self._orig = sn.send_telegram
        self.addCleanup(setattr, sn, "send_telegram", self._orig)

    def _send(self, result):
        def fake(text, env, **kw):
            self.calls.append(kw)
            return dict(result)
        sn.send_telegram = fake

    def test_no_topic_module_is_the_two_argument_call(self):
        self.use_topics(None)
        self._send({"ok": True})
        sn._deliver_reminder("telegram", "x", "t.env", None, None, pierces=False, state_dir="/s")
        self.assertEqual(self.calls, [{}])

    def test_a_topic_purpose_is_passed_with_the_state_dir(self):
        self.use_topics(_fake_topics())
        self._send({"ok": True})
        sn._deliver_reminder("telegram", "x", "t.env", None, None, pierces=True, state_dir="/s")
        self.assertEqual(self.calls, [{"topic": "reminders", "state_dir": "/s"}])

    def _fire(self, result):
        self._send(result)
        sn.save_json(os.path.join(self.tmp.name, "reminders.json"),
                     [{"id": "r1", "text": "Water the plants.", "channel": "telegram",
                       "due_at": NOW.isoformat().replace("+00:00", "Z"), "fired_at": None}])
        return [s for s in sn.check_reminders(self.tmp.name, NOW, fire=True, telegram_env="t.env")
                if s["kind"] == "reminder_fired"]

    def test_the_fired_signal_carries_the_thread_off_the_send_result(self):
        self.use_topics(_fake_topics())
        fired = self._fire({"ok": True, "message_thread_id": 77})
        self.assertEqual(fired[0]["message_thread_id"], 77)

    def test_no_thread_in_the_result_means_no_key(self):
        self.use_topics(None)
        for junk in ({"ok": True}, {"ok": True, "message_thread_id": True},
                     {"ok": True, "message_thread_id": 0}, {"ok": True, "message_thread_id": "7"}):
            with self.subTest(result=junk):
                self.tmp = tempfile.TemporaryDirectory()
                self.addCleanup(self.tmp.cleanup)
                fired = self._fire(junk)
                self.assertNotIn("message_thread_id", fired[0])


if __name__ == "__main__":
    unittest.main()
