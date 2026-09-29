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

The third half — that `telegram_send.py` actually forwards those flags into `classify` — belongs
beside the sender and lands with it.

Run:  python -m unittest test_watch_peek_source_fields   (from seneschal/scripts)
"""
import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, SCRIPT_DIR)

import watch_reconcile as wr  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
