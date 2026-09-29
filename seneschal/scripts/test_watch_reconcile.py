#!/usr/bin/env python3
"""Tests for **thread reconciliation** (`watch_reconcile.py`).

The motivating shape: a bank's overdraft notice is followed hours later by the same sender's own
*"you are no longer in low-balance mode"*, and nothing reads the second message, because the peek
reads mail one message at a time. These pin `classify`'s three verdicts against a synthetic pair of
that shape (a fictional "Northwind" bank), plus the door-fallback contract `default_fetch_messages`
owes callers — never a live network call, never a real subprocess, never the wall clock.

Run:  python -m unittest test_watch_reconcile   (from seneschal/scripts)
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import watch_reconcile as wr  # noqa: E402

SENDER = "alerts@northwind.example"

CANDIDATE = {
    "sender": SENDER,
    "subject": "Low Balance Alert",
    "text": "🚨 Financial alert: your Northwind checking account has entered low-balance mode.",
    "received_at": "2026-09-08T11:28:00Z",
}

RESOLUTION = {
    "id": "m2", "from": SENDER, "subject": "You are no longer in low-balance mode",
    "text": "Good news — your Northwind checking account is no longer in low-balance mode.",
    "date": "2026-09-08T18:16:00Z",
}

NOW = datetime(2026, 9, 8, 19, 0, 0, tzinfo=timezone.utc)


def _fixture_fetch(messages, door="gmail"):
    def fetch(sender, since, *, state_dir=None):
        return messages, door
    return fetch


class NoticeAndResolutionPair(unittest.TestCase):
    """The motivating shape, synthesized."""

    def test_the_resolution_message_classifies_superseded(self):
        result = wr.classify(CANDIDATE, fetch_messages=_fixture_fetch([RESOLUTION]), now=NOW)
        self.assertEqual(result["verdict"], wr.SUPERSEDED)
        self.assertEqual(result["reason"], "superseded-by:m2")
        self.assertEqual(result["matched"]["message_id"], "m2")
        self.assertEqual(result["matched"]["signal"], "no longer")
        self.assertEqual(result["door"], "gmail")

    def test_an_unrelated_later_message_from_the_same_sender_stays_open(self):
        unrelated = {"id": "m3", "from": SENDER, "subject": "New Northwind mobile app",
                     "text": "Check out our redesigned mobile app experience.",
                     "date": "2026-09-08T15:00:00Z"}
        result = wr.classify(CANDIDATE, fetch_messages=_fixture_fetch([unrelated]), now=NOW)
        self.assertEqual(result["verdict"], wr.STILL_OPEN)
        self.assertEqual(result["reason"], "no-resolution-signal")
        self.assertIsNone(result["matched"])

    def test_a_related_later_message_with_no_resolution_signal_stays_open(self):
        """Names the same fact but doesn't resolve it — a status update, not a fix."""
        update = {"id": "m4", "from": SENDER, "subject": "Still in low-balance mode",
                  "text": "Your Northwind checking account remains in low-balance mode.",
                  "date": "2026-09-08T12:00:00Z"}
        result = wr.classify(CANDIDATE, fetch_messages=_fixture_fetch([update]), now=NOW)
        self.assertEqual(result["verdict"], wr.STILL_OPEN)

    def test_the_best_of_several_later_messages_wins(self):
        unrelated = {"id": "m3", "from": SENDER, "subject": "New Northwind mobile app",
                     "text": "Check out our redesigned mobile app.", "date": "2026-09-08T12:00:00Z"}
        result = wr.classify(CANDIDATE, fetch_messages=_fixture_fetch([unrelated, RESOLUTION]),
                             now=NOW)
        self.assertEqual(result["verdict"], wr.SUPERSEDED)
        self.assertEqual(result["matched"]["message_id"], "m2")

    def test_a_different_fact_from_the_same_institution_is_not_superseded(self):
        """THREAD_MATCH_MIN_COVERAGE's reason to exist: two alerts that merely share the bank's name
        must not let one's resolution silence the other."""
        savings = {"id": "m5", "from": SENDER, "subject": "Savings goal reached",
                   "text": "Your Northwind savings goal is resolved and closed.",
                   "date": "2026-09-08T13:00:00Z"}
        result = wr.classify(CANDIDATE, fetch_messages=_fixture_fetch([savings]), now=NOW)
        self.assertEqual(result["verdict"], wr.STILL_OPEN)


class UnknownFailsOpen(unittest.TestCase):
    """UNKNOWN is the fail-open answer — no later message, or the door couldn't be asked."""

    def test_no_source_metadata_never_touches_the_door(self):
        called = []

        def fetch(*a, **k):
            called.append(1)
            return [], "gmail"

        result = wr.classify({"text": "x", "received_at": "2026-09-08T11:28:00Z"},
                             fetch_messages=fetch, now=NOW)
        self.assertEqual(result["verdict"], wr.UNKNOWN)
        self.assertEqual(result["reason"], "no-source-metadata")
        self.assertEqual(called, [])

    def test_missing_received_at_never_touches_the_door(self):
        called = []

        def fetch(*a, **k):
            called.append(1)
            return [], "gmail"

        result = wr.classify({"sender": SENDER, "text": "x"}, fetch_messages=fetch, now=NOW)
        self.assertEqual(result["verdict"], wr.UNKNOWN)
        self.assertEqual(called, [])

    def test_door_unavailable_is_unknown(self):
        result = wr.classify(CANDIDATE, fetch_messages=_fixture_fetch(None, door=None), now=NOW)
        self.assertEqual(result["verdict"], wr.UNKNOWN)
        self.assertEqual(result["reason"], "door-unavailable")

    def test_a_raising_fetcher_is_unknown_not_a_raise(self):
        def fetch(sender, since, *, state_dir=None):
            raise RuntimeError("bridge is down")

        result = wr.classify(CANDIDATE, fetch_messages=fetch, now=NOW)
        self.assertEqual(result["verdict"], wr.UNKNOWN)
        self.assertEqual(result["reason"], "door-unavailable")

    def test_no_later_message_at_all_is_unknown(self):
        result = wr.classify(CANDIDATE, fetch_messages=_fixture_fetch([]), now=NOW)
        self.assertEqual(result["verdict"], wr.UNKNOWN)
        self.assertEqual(result["reason"], "no-later-message")

    def test_a_message_outside_the_window_does_not_count(self):
        stale = {**RESOLUTION, "date": "2026-09-11T11:28:00Z"}  # 3 days later
        result = wr.classify(CANDIDATE, fetch_messages=_fixture_fetch([stale]), window_hours=48.0,
                             now=datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(result["verdict"], wr.UNKNOWN)
        self.assertEqual(result["reason"], "no-later-message")

    def test_a_message_at_or_before_received_at_does_not_count(self):
        same_time = {**RESOLUTION, "date": CANDIDATE["received_at"]}
        result = wr.classify(CANDIDATE, fetch_messages=_fixture_fetch([same_time]), now=NOW)
        self.assertEqual(result["verdict"], wr.UNKNOWN)

    def test_a_message_from_the_future_relative_to_now_does_not_count(self):
        """`now` is a test seam — a message dated after it must never be treated as real evidence."""
        future = {**RESOLUTION, "date": "2026-09-08T20:00:00Z"}
        result = wr.classify(CANDIDATE, fetch_messages=_fixture_fetch([future]),
                             now=datetime(2026, 9, 8, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(result["verdict"], wr.UNKNOWN)


class Signals(unittest.TestCase):
    def test_the_tracked_signals_file_loads_and_contains_no_longer(self):
        signals = wr.load_signals()
        self.assertIn("no longer", signals)
        self.assertIn("resolved", signals)

    def test_a_missing_signals_file_is_silent_not_a_raise(self):
        self.assertEqual(wr.load_signals(references_dir=os.path.join(tempfile.mkdtemp(), "nope")), [])

    def test_a_malformed_signals_file_is_silent_not_a_raise(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, wr.SIGNALS_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"signals": ["no longer"')  # truncated mid-write
        self.assertEqual(wr.load_signals(references_dir=d), [])

    def test_signal_hit_is_a_literal_substring_match(self):
        self.assertEqual(wr.signal_hit("You are no longer overdrawn.", ["no longer"]), "no longer")
        self.assertIsNone(wr.signal_hit("You are still overdrawn.", ["no longer"]))

    def test_signal_hit_on_non_string_is_none(self):
        self.assertIsNone(wr.signal_hit(None, ["no longer"]))


class DateParsing(unittest.TestCase):
    def test_iso_with_trailing_z(self):
        dt = wr._parse_dt("2026-09-08T11:28:00Z")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.hour, 11)

    def test_rfc2822_email_date_header(self):
        dt = wr._parse_dt("Tue, 8 Sep 2026 11:28:00 +0000")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 11)

    def test_garbage_is_none(self):
        self.assertIsNone(wr._parse_dt("not a date"))
        self.assertIsNone(wr._parse_dt(None))
        self.assertIsNone(wr._parse_dt(""))


class DoorFallback(unittest.TestCase):
    """`default_fetch_messages`'s two-doors-in-order contract, with `_run_script` stubbed so nothing
    here ever spawns a real subprocess or touches a network."""

    def _with_run(self, fake):
        real = wr._run_script
        wr._run_script = fake
        self.addCleanup(setattr, wr, "_run_script", real)

    def test_gmail_unavailable_falls_back_to_proton(self):
        calls = []

        def fake_run(script, *args, **kw):
            calls.append(script)
            if script == "gmail_api.py":
                return None
            return {"ok": True, "mailbox": "INBOX", "messages": [
                {"uid": "5", "from": SENDER, "subject": RESOLUTION["subject"],
                 "body": RESOLUTION["text"], "date": RESOLUTION["date"]}]}

        self._with_run(fake_run)
        messages, door = wr.default_fetch_messages(
            SENDER, datetime(2026, 9, 8, 11, 28, tzinfo=timezone.utc))
        self.assertEqual(door, "proton")
        self.assertEqual(calls, ["gmail_api.py", "proton_read.py"])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["from"], SENDER)

    def test_both_doors_unavailable(self):
        self._with_run(lambda *a, **k: None)
        messages, door = wr.default_fetch_messages(
            "nobody@example.com", datetime(2026, 9, 8, tzinfo=timezone.utc))
        self.assertIsNone(messages)
        self.assertIsNone(door)

    def test_gmail_succeeding_never_calls_proton(self):
        calls = []

        def fake_run(script, *args, **kw):
            calls.append(script)
            if script == "gmail_api.py":
                return {"ok": True, "account": "personal", "messages": []}
            raise AssertionError("proton should never be asked when gmail answered")

        self._with_run(fake_run)
        messages, door = wr.default_fetch_messages(
            SENDER, datetime(2026, 9, 8, tzinfo=timezone.utc))
        self.assertEqual(door, "gmail")
        self.assertEqual(messages, [])
        self.assertEqual(calls, ["gmail_api.py"])

    def test_gmail_account_label_comes_from_the_env(self):
        seen = {}

        def fake_run(script, *args, **kw):
            seen[script] = list(args)
            return {"ok": True, "messages": []}

        self._with_run(fake_run)
        real = os.environ.get(wr.GMAIL_ACCOUNT_ENV_VAR)
        os.environ[wr.GMAIL_ACCOUNT_ENV_VAR] = "work"
        try:
            wr.default_fetch_messages(SENDER, datetime(2026, 9, 8, tzinfo=timezone.utc))
        finally:
            if real is None:
                os.environ.pop(wr.GMAIL_ACCOUNT_ENV_VAR, None)
            else:
                os.environ[wr.GMAIL_ACCOUNT_ENV_VAR] = real
        args = seen["gmail_api.py"]
        self.assertEqual(args[args.index("--account") + 1], "work")

    def test_proton_filters_by_sender_client_side(self):
        def fake_run(script, *args, **kw):
            if script == "gmail_api.py":
                return None
            return {"ok": True, "mailbox": "INBOX", "messages": [
                {"uid": "1", "from": "someone-else@example.com", "subject": "irrelevant",
                 "body": "irrelevant", "date": "2026-09-08T12:00:00Z"},
                {"uid": "2", "from": f"Alerts <{SENDER}>", "subject": RESOLUTION["subject"],
                 "body": RESOLUTION["text"], "date": RESOLUTION["date"]},
            ]}

        self._with_run(fake_run)
        messages, door = wr.default_fetch_messages(
            SENDER, datetime(2026, 9, 8, tzinfo=timezone.utc))
        self.assertEqual(door, "proton")
        self.assertEqual([m["id"] for m in messages], ["2"])


class StaysOptionallyImportable(unittest.TestCase):
    def test_no_telegram_or_presence_import(self):
        with open(os.path.join(SCRIPT_DIR, "watch_reconcile.py"), encoding="utf-8") as fh:
            src = fh.read()
        for name in ("import telegram_", "import presence", "from presence", "from telegram_"):
            self.assertNotIn(name, src, name)


if __name__ == "__main__":
    unittest.main()
