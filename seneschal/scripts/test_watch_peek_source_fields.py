#!/usr/bin/env python3
"""Watch peek → `telegram_send.py` source-field wiring: the prose half.

`watch_reconcile.classify` only runs when the peek forwards `--source-sender`/`--source-subject`/
`--source-received-at` to `telegram_send.py`. The peek is not code: the daemon spawns a `claude -p`
turn pointed at `seneschal/modes/watch.md`, and it is that prose turn — not any Python here — that
decides what to pass. So two things are pinned:

1. **The documentation says the right thing** — `watch.md` instructs the peek to forward all three
   fields for an email-sourced escalation, and to invent none of them for a Slack/calendar finding
   (which has no thread to reconcile against). `PeekInstructionContent` reads the file, exactly the
   way a content check must when the "code path" is prose.
2. **The received-at field is safe to pass verbatim** — `ReceivedAtAcceptsTheVerbatimEmailTimestamp`.

3. **The sender forwards them** — `PeekSendPathForwarding` builds the exact `telegram_send.py` call
   the convention prescribes and asserts what reaches `watch_reconcile.classify`.

Run:  python -m unittest test_watch_peek_source_fields   (from seneschal/scripts)
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, SCRIPT_DIR)

import telegram_send as ts  # noqa: E402
import watch_reconcile as wr  # noqa: E402

NOW = datetime(2026, 9, 8, 19, 0, 0, tzinfo=timezone.utc)

WATCH_MD = os.path.join(REPO_ROOT, "seneschal", "modes", "watch.md")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class PeekInstructionContent(unittest.TestCase):
    """The peek is a prose Claude Code turn, not Python — this is the only place the wiring lives."""

    def setUp(self):
        # Whitespace collapsed: a phrase may wrap across a line break in the Markdown source.
        self.text = " ".join(_read(WATCH_MD).split())

    def test_all_three_source_flags_are_documented_together(self):
        for flag in ("--source-sender", "--source-subject", "--source-received-at"):
            with self.subTest(flag=flag):
                self.assertIn(flag, self.text, f"{flag} is not documented in watch.md")

    def test_instruction_is_scoped_to_email(self):
        self.assertIn("Escalating something read from email", self.text)

    def test_instruction_names_the_fields_the_peek_already_has(self):
        # From/Subject/Date — fields read off the email itself, never a new lookup.
        for word in ("its From", "its Subject", "its Date"):
            with self.subTest(word=word):
                self.assertIn(word, self.text)

    def test_never_invent_for_slack_or_calendar(self):
        self.assertIn("Never invent these for a Slack or calendar finding", self.text)
        self.assertIn("email thread to reconcile", self.text)


class ReceivedAtAcceptsTheVerbatimEmailTimestamp(unittest.TestCase):
    """`watch.md` tells the peek to pass the email's Date header through VERBATIM rather than
    converting it to a UTC ISO string by hand — a cheap peek doing timezone arithmetic is exactly the
    kind of step that goes quietly wrong. This pins that `watch_reconcile`'s own parser already accepts
    the raw RFC 2822 `Date:` header `gmail_api.py`/`proton_read.py` hand back, so the "pass it
    verbatim" instruction is actually safe to give, not just simpler."""

    def test_rfc2822_date_header_parses(self):
        dt = wr._parse_dt("Tue, 08 Sep 2026 06:28:00 -0500")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.utcoffset().total_seconds(), -5 * 3600)


class PeekSendPathForwarding(unittest.TestCase):
    """Builds the exact `telegram_send.py` call the documented convention prescribes, and asserts
    what actually reaches `watch_reconcile.classify` — the forwarding mechanism the peek's prose
    instruction depends on existing."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.captured = []
        self.real_classify = wr.classify

        def capture(candidate, **kw):
            self.captured.append(candidate)
            return {"verdict": wr.UNKNOWN, "reason": "no-later-message", "fact_key": "x",
                    "matched": None, "door": None}

        wr.classify = capture

    def tearDown(self):
        wr.classify = self.real_classify

    def _run_send(self, argv):
        real_argv, buf = sys.argv, io.StringIO()
        sys.argv = ["telegram_send.py"] + argv
        os.environ[ts.SOURCE_ENV_VAR] = "watch"
        try:
            with redirect_stdout(buf):
                ts.main(now=NOW)
        finally:
            sys.argv = real_argv
            os.environ.pop(ts.SOURCE_ENV_VAR, None)
        return buf.getvalue()

    def test_an_email_sourced_escalation_forwards_all_three_fields(self):
        """The convention `watch.md` documents for an email finding: pass all three, verbatim."""
        self._run_send([
            "--text", "🚨 Financial alert: your Northwind checking account has entered Low Cash Mode.",
            "--chat-id", "1", "--state-dir", self.dir, "--dry-run",
            "--source-sender", "alerts@northwind.example",
            "--source-subject", "Low Cash Mode Alert",
            "--source-received-at", "Tue, 08 Sep 2026 06:28:00 -0500",
        ])
        self.assertEqual(len(self.captured), 1)
        candidate = self.captured[0]
        self.assertEqual(candidate["sender"], "alerts@northwind.example")
        self.assertEqual(candidate["subject"], "Low Cash Mode Alert")
        self.assertEqual(candidate["received_at"], "Tue, 08 Sep 2026 06:28:00 -0500")

    def test_a_slack_or_calendar_finding_forwards_none_of_them(self):
        """The convention for a non-email finding: pass none — never a guessed/invented value."""
        self._run_send([
            "--text", "Heads up: a Slack DM from a colleague needs a reply.",
            "--chat-id", "1", "--state-dir", self.dir, "--dry-run",
        ])
        self.assertEqual(len(self.captured), 1)
        candidate = self.captured[0]
        self.assertIsNone(candidate["sender"])
        self.assertIsNone(candidate["subject"])
        self.assertIsNone(candidate["received_at"])


if __name__ == "__main__":
    unittest.main()
