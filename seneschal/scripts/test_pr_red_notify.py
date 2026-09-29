#!/usr/bin/env python3
"""Tests for ``pr_red_notify`` — the red-CI notification ledger, message text and sender.

``test_pr_sweep.RedNotifyTest`` pins how this module is WIRED into the resident sweep (dedupe by
head, quiet hours, the burst cap, fail-open on a bad send). This file pins this module's OWN
behaviour in isolation: the ledger's read/write/dedupe primitives, the message text (which must
reuse ``watch_pr.summary_line`` rather than re-derive it), and ``send()``'s import-and-argument
plumbing — with `sentinel`/`telegram_topics` stubbed via `sys.modules`, so no test here reaches a
real Telegram bot.

Run:  python -m unittest test_pr_red_notify   (from seneschal/scripts)
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import merge_guard as mg  # noqa: E402
import pr_red_notify as prn  # noqa: E402
import watch_pr  # noqa: E402

REPO = "example/repo"
OTHER_REPO = "example/other"
HEAD = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
OTHER_HEAD = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"
NOW = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)

RED_ROLLUP = [{"__typename": "CheckRun", "name": "unittest", "status": "COMPLETED",
              "conclusion": "FAILURE"}]


def red_row(pr=703, head=HEAD, repo=REPO, title="fix(parser): refuse to rebuild a diverged page"):
    return {"number": pr, "statusCheckRollup": RED_ROLLUP, "headRefOid": head,
            "url": f"https://github.com/{repo}/pull/{pr}", "isDraft": False, "title": title}


class LedgerTest(unittest.TestCase):
    """The dedupe ledger — `merge_guard.record_ask`'s shape, one file over."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_an_unwritten_ledger_reads_as_nothing_notified(self):
        self.assertFalse(prn.already_notified(self.dir, 703, HEAD, repo=REPO))
        self.assertEqual(prn.read_notifies(self.dir), [])

    def test_recording_then_reading_round_trips(self):
        prn.record_notified(self.dir, 703, HEAD, repo=REPO, now=NOW)
        self.assertTrue(prn.already_notified(self.dir, 703, HEAD, repo=REPO))
        rows = prn.read_notifies(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pr"], 703)
        self.assertEqual(rows[0]["repo"], REPO)
        self.assertEqual(rows[0]["head_sha"], HEAD)
        self.assertEqual(rows[0]["schema"], prn.NOTIFY_LOG_SCHEMA)

    def test_a_moved_head_is_not_already_notified(self):
        """A new commit is a new question — the same binding `merge_guard.already_asked` uses and
        for the same reason: the failure that made the old head red may not be the one the new
        commit carries."""
        prn.record_notified(self.dir, 703, HEAD, repo=REPO, now=NOW)
        self.assertFalse(prn.already_notified(self.dir, 703, OTHER_HEAD, repo=REPO))

    def test_two_repositories_do_not_collide_on_the_same_pr_number(self):
        prn.record_notified(self.dir, 45, HEAD, repo=OTHER_REPO, now=NOW)
        self.assertFalse(prn.already_notified(self.dir, 45, HEAD, repo=REPO))
        self.assertTrue(prn.already_notified(self.dir, 45, HEAD, repo=OTHER_REPO))

    def test_every_notify_appends_a_row_none_is_ever_overwritten(self):
        """`merge_guard.record_ask`'s defect, refused here the same way: two live notifications must
        not collapse into one row on disk."""
        prn.record_notified(self.dir, 703, HEAD, repo=REPO, now=NOW)
        prn.record_notified(self.dir, 704, HEAD, repo=REPO, now=NOW)
        self.assertEqual(len(prn.read_notifies(self.dir)), 2)

    def test_a_torn_line_does_not_hide_the_good_rows_around_it(self):
        prn.record_notified(self.dir, 703, HEAD, repo=REPO, now=NOW)
        with open(prn.notify_log_path(self.dir), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        prn.record_notified(self.dir, 704, HEAD, repo=REPO, now=NOW)
        rows = prn.read_notifies(self.dir)
        self.assertEqual(sorted(r["pr"] for r in rows), [703, 704])

    def test_an_unreadable_log_costs_at_most_a_duplicate_never_a_crash(self):
        missing_dir = os.path.join(self.dir, "does-not-exist")
        self.assertEqual(prn.read_notifies(missing_dir), [])
        self.assertFalse(prn.already_notified(missing_dir, 703, HEAD, repo=REPO))

    def test_record_notified_never_raises_on_an_unwritable_dir(self):
        # A file where a directory is expected: os.makedirs raises inside, and it must be swallowed.
        blocked = os.path.join(self.dir, "blocked")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("x")
        prn.record_notified(blocked, 703, HEAD, repo=REPO, now=NOW)  # must not raise


class NotifyTextTest(unittest.TestCase):
    """The message body — and it must reuse `watch_pr.summary_line`, never re-derive the rollup
    prose (`pr_sweep.is_green`'s own rule, applied to the red arm)."""

    def test_it_names_the_repo_the_pr_and_the_title(self):
        verdict = watch_pr.classify(RED_ROLLUP)
        text = prn.notify_text(REPO, red_row(), verdict)
        self.assertIn(REPO, text)
        self.assertIn("#703", text)
        self.assertIn("fix(parser)", text)

    def test_it_reuses_watch_prs_own_red_summary_line_rather_than_a_second_phrasing(self):
        verdict = watch_pr.classify(RED_ROLLUP)
        text = prn.notify_text(REPO, red_row(), verdict)
        self.assertIn(watch_pr.summary_line("703", verdict), text)

    def test_it_names_the_failing_checks(self):
        verdict = watch_pr.classify(RED_ROLLUP)
        text = prn.notify_text(REPO, red_row(), verdict)
        self.assertIn("unittest", text)
        self.assertIn("FAILURE", text)

    def test_it_carries_a_bare_link(self):
        verdict = watch_pr.classify(RED_ROLLUP)
        row = red_row()
        text = prn.notify_text(REPO, row, verdict)
        self.assertIn(row["url"], text)

    def test_a_missing_title_does_not_crash_or_leave_a_dangling_dash(self):
        verdict = watch_pr.classify(RED_ROLLUP)
        row = red_row(title="")
        text = prn.notify_text(REPO, row, verdict)
        self.assertNotIn(" — \n", text)

    def test_it_shows_the_short_head(self):
        verdict = watch_pr.classify(RED_ROLLUP)
        text = prn.notify_text(REPO, red_row(), verdict)
        self.assertIn(HEAD[:12], text)


class SendTest(unittest.TestCase):
    """`send()`'s plumbing — never a real network call. `sentinel`/`telegram_topics` are stubbed via
    `sys.modules` because `send()` imports them lazily, from inside a function, exactly as
    `merge_guard.in_quiet_hours` does for the same reason (a shared daemon task must not pay this
    import cost on every shell call elsewhere in the tree)."""

    def test_it_calls_sentinels_send_telegram_with_the_pull_requests_topic(self):
        calls = []

        def fake_send_telegram(text, telegram_env, message_thread_id=None, topic=None, state_dir=None):
            calls.append({"text": text, "telegram_env": telegram_env, "topic": topic,
                          "state_dir": state_dir})
            return {"ok": True, "sent": True}

        fake_sentinel = types.SimpleNamespace(send_telegram=fake_send_telegram)
        fake_topics = types.SimpleNamespace(TOPIC_PULL_REQUESTS="pull-requests")
        with mock.patch.dict(sys.modules, {"sentinel": fake_sentinel,
                                           "telegram_topics": fake_topics}):
            res = prn.send("CI is red", env_file="/tmp/telegram.env", state_dir="/tmp/state")
        self.assertTrue(res["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["text"], "CI is red")
        self.assertEqual(calls[0]["telegram_env"], "/tmp/telegram.env")
        self.assertEqual(calls[0]["topic"], "pull-requests")
        self.assertEqual(calls[0]["state_dir"], "/tmp/state")

    def test_no_env_file_falls_back_to_the_default_telegram_env_never_none(self):
        """`sentinel.send_telegram` builds an argv that always includes `--env-file <value>`; handing
        it `None` would put the literal string `None` on the subprocess command line. A caller that
        passes nothing must still get a real path."""
        calls = []

        def fake_send_telegram(text, telegram_env, **kw):
            calls.append(telegram_env)
            return {"ok": True, "sent": True}

        fake_sentinel = types.SimpleNamespace(send_telegram=fake_send_telegram)
        fake_topics = types.SimpleNamespace(TOPIC_PULL_REQUESTS="pull-requests")
        with mock.patch.dict(sys.modules, {"sentinel": fake_sentinel,
                                           "telegram_topics": fake_topics}):
            prn.send("CI is red")
        self.assertEqual(calls, [mg.DEFAULT_TELEGRAM_ENV])
        self.assertIsNotNone(calls[0])

    def test_an_import_failure_costs_the_send_not_a_crash(self):
        with mock.patch.dict(sys.modules, {"sentinel": None}):
            res = prn.send("CI is red")
        self.assertFalse(res["ok"])
        self.assertIn("error", res)


if __name__ == "__main__":
    unittest.main()
