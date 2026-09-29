#!/usr/bin/env python3
"""Tests for inbound message EDITS — the poller half (seneschal/docs/telegram-inbound-spec.md §6a).
Stdlib ``unittest`` only, like the rest of the suite.

The defect this feature exists to kill: an `allowed_updates` that names `message` and
`message_reaction` is an implicit "and nothing else" — so Telegram never delivers `edited_message` at
all. The owner sends `(it`, edits it into the full sentence, and the assistant only ever sees `(it`.
The failure is silent and asymmetric: the owner's screen reads as corrected, the assistant answers
the uncorrected text, and neither side can see the divergence.

What's covered here:
  * `edited_message` is actually requested — without it, an edit cannot arrive even in principle;
  * the update shape parses through the SAME path as a new message (text, caption, reply-to, media),
    carries `kind: "edit"` + the original's `message_id`, and `message_id` is on every item;
  * the offset advances across a mixed batch of message / edited_message / message_reaction, and an
    edit-only batch still advances it — a regression here is a message-loss bug, not a cosmetic one;
  * a malformed/partial `edited_message` contributes nothing rather than raising.

What the daemon then DOES with an edit — rewrite an un-answered message in place, hand an
already-answered one over as a marked-up new inbound, never rewrite the message being answered right
now — is daemon wiring and is tested with the daemon.

**No network, ever.** `get_updates` is mocked at every call site and no test passes `--download-dir`,
so `getFile` is unreachable. A live daemon may be holding a real conversation on the configured bot
token, and a test that posts for real is a bug.

Run:  python -m unittest seneschal.scripts.test_telegram_edits   (or)   python test_telegram_edits.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402
import telegram_poll as tp  # noqa: E402

# A typo'd fragment and what it was edited into.
TYPO = "(it"
FIXED = "(it's almost like I built you that way lol)"


def _msg(text=TYPO, message_id=7, chat_id=555, **over):
    base = {"message_id": message_id, "chat": {"id": chat_id, "type": "private"},
            "from": {"username": "owner"}, "date": 1_000_000, "text": text}
    base.update(over)
    return base


def _edited(text=FIXED, message_id=7, chat_id=555, edit_date=1_000_060, **over):
    return _msg(text=text, message_id=message_id, chat_id=chat_id, edit_date=edit_date, **over)


class AllowedUpdates(unittest.TestCase):
    def test_edited_message_is_requested(self):
        """Off by default in the Bot API — an unnamed update type is one Telegram will never send."""
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
        # All three, still — adding edits must not have cost us reactions.
        for wanted in ("message", "edited_message", "message_reaction"):
            self.assertIn(wanted, seen["url"], wanted)


class MessagePayload(unittest.TestCase):
    """The one-line discriminator every downstream branch hangs off."""

    def test_new_message(self):
        payload, kind = tp.message_payload({"update_id": 1, "message": _msg()})
        self.assertEqual(kind, "message")
        self.assertEqual(payload["text"], TYPO)

    def test_edited_message(self):
        payload, kind = tp.message_payload({"update_id": 1, "edited_message": _edited()})
        self.assertEqual(kind, "edit")
        self.assertEqual(payload["text"], FIXED)

    def test_anything_else_is_not_a_message(self):
        for update in ({"update_id": 1, "message_reaction": {"chat": {"id": 1}}},
                       {"update_id": 1},
                       {"update_id": 1, "message": "not-a-dict"},
                       {"update_id": 1, "edited_message": "not-a-dict"},
                       {"update_id": 1, "edited_message": {}},
                       {"update_id": 1, "edited_message": []}):
            self.assertEqual(tp.message_payload(update), (None, ""), update)


class PollerExtraction(unittest.TestCase):
    """`main()` end to end with the wire mocked out: what the daemon actually receives."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.offset = os.path.join(self.dir, "telegram-offset")
        self.cache = os.path.join(self.dir, "custom-emoji-cache.json")

    def _run(self, updates, *extra_argv, allowed=""):
        argv = ["telegram_poll.py", "--offset-file", self.offset,
                "--custom-emoji-cache", self.cache, *extra_argv]
        env = {"TELEGRAM_BOT_TOKEN": "not-a-real-token"}
        if allowed:
            env["TELEGRAM_ALLOWED_CHAT_IDS"] = allowed
        out = io.StringIO()
        # `clear=True` so a real TELEGRAM_ALLOWED_CHAT_IDS on the host can't change the verdict, and
        # `get_updates` mocked so nothing here can reach the wire the live daemon is holding.
        with mock.patch.object(tp, "get_updates", return_value=updates), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out):
            rc = tp.main()
        return rc, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_edit_parses_and_is_marked(self):
        rc, res = self._run([{"update_id": 41, "edited_message": _edited()}])
        self.assertEqual(rc, 0)
        self.assertEqual(res["count"], 1)
        item = res["messages"][0]
        self.assertEqual(item["kind"], "edit")
        self.assertEqual(item["text"], FIXED)
        self.assertEqual(item["message_id"], 7)      # the ORIGINAL's id — the only handle we get
        self.assertEqual(item["edit_date"], 1_000_060)
        self.assertEqual(item["chat_id"], "555")
        self.assertEqual(item["from"], "owner")

    def test_a_new_message_now_carries_its_id_too(self):
        """The original has to be findable before its edit shows up, so the id is on every item."""
        _rc, res = self._run([{"update_id": 41, "message": _msg()}])
        item = res["messages"][0]
        self.assertEqual(item["kind"], "message")
        self.assertEqual(item["message_id"], 7)
        self.assertNotIn("edit_date", item)          # additive only where it means something

    def test_edit_reads_caption_and_reply_context_like_any_message(self):
        upd = {"update_id": 41, "edited_message": _edited(
            text="", caption="the fixed caption",
            reply_to_message={"text": "want me to send it?"},
            photo=[{"file_id": "f", "file_unique_id": "u", "file_size": 10}])}
        _rc, res = self._run([upd])       # no --download-dir: described, never fetched
        item = res["messages"][0]
        self.assertEqual(item["caption"], "the fixed caption")
        self.assertEqual(item["reply_to"], "want me to send it?")
        self.assertEqual(item["attachment"]["kind"], "photo")
        self.assertNotIn("local_path", item["attachment"])

    def test_edit_outside_the_allowlist_is_ignored_but_acknowledged(self):
        _rc, res = self._run([{"update_id": 41, "edited_message": _edited(chat_id=999)}],
                             "--commit", allowed="555")
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["next_offset"], 42)     # still consumed, never replayed forever

    def test_malformed_edit_contributes_nothing_rather_than_raising(self):
        rc, res = self._run([{"update_id": 41, "edited_message": "not-a-dict"},
                             {"update_id": 42, "edited_message": {}},
                             {"update_id": 43, "edited_message": None}])
        self.assertEqual(rc, 0)
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["next_offset"], 44)

    def test_partial_edit_survives_all_the_way_to_the_line_the_assistant_reads(self):
        """A shape with no chat, no from and no text parses, and then produces an EMPTY line — which
        telegram_task's filter drops. Nothing raises at either layer."""
        _rc, res = self._run([{"update_id": 41, "edited_message": {"message_id": 3, "chat": "junk"}}])
        item = res["messages"][0]
        self.assertEqual(item["text"], "")
        self.assertEqual(pr.telegram_inbound_text(item), "")


class OffsetAccounting(unittest.TestCase):
    """The durable cursor. An edit must advance it exactly like anything else — stranding it replays
    forever, over-advancing it eats messages."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.offset = os.path.join(self.dir, "telegram-offset")
        self.cache = os.path.join(self.dir, "custom-emoji-cache.json")

    def _run(self, updates, commit=True):
        argv = ["telegram_poll.py", "--offset-file", self.offset,
                "--custom-emoji-cache", self.cache] + (["--commit"] if commit else [])
        out = io.StringIO()
        with mock.patch.object(tp, "get_updates", return_value=updates), \
                mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "not-a-real-token"}, clear=True), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out):
            tp.main()
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def _mixed(self):
        return [
            {"update_id": 100, "message": _msg(text="first", message_id=1)},
            {"update_id": 101, "edited_message": _edited(text="first, fixed", message_id=1)},
            {"update_id": 102, "message_reaction": {
                "chat": {"id": 555, "type": "private"}, "message_id": 90, "user": {"username": "owner"},
                "date": 2, "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}},
            {"update_id": 103, "message": _msg(text="second", message_id=2)},
        ]

    def test_mixed_batch_advances_past_the_highest_update_id(self):
        res = self._run(self._mixed())
        self.assertEqual(res["next_offset"], 104)
        self.assertTrue(res["committed"])
        self.assertEqual(tp.read_offset(self.offset), 104)
        self.assertEqual([m["kind"] for m in res["messages"]],
                         ["message", "edit", "reaction", "message"])

    def test_an_edit_only_batch_still_advances(self):
        """The stranding case: if an edit didn't count toward the cursor it would be re-delivered on
        every poll for ~24h, and the annotation with it."""
        res = self._run([{"update_id": 55, "edited_message": _edited()}])
        self.assertEqual(res["next_offset"], 56)
        self.assertEqual(tp.read_offset(self.offset), 56)

    def test_peek_does_not_commit_an_edit(self):
        self._run(self._mixed(), commit=False)
        self.assertIsNone(tp.read_offset(self.offset))

    def test_offset_is_carried_when_a_batch_is_empty(self):
        self._run([{"update_id": 100, "edited_message": _edited()}])
        res = self._run([])
        self.assertEqual(res["next_offset"], 101)     # unchanged, not reset
        self.assertEqual(tp.read_offset(self.offset), 101)


if __name__ == "__main__":
    unittest.main()
