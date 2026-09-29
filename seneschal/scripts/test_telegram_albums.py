#!/usr/bin/env python3
"""Tests for the ALBUM field on the poller — N Telegram updates share one `media_group_id`
(docs/telegram-inbound-spec.md §6c). Stdlib ``unittest`` only, like the rest of the suite.

The defect the album feature exists to kill: Telegram delivers a multi-photo album as one `message`
update per photo, sharing a `media_group_id`, with the caption on exactly one of them. A pipeline that
ignores the field answers after the 1st photo, again after the 4th, and again after the last — each
time confidently, each time on a slice (`docs/telegram-capability-map.md` §3.5).

The poller's half is the first rail and the only one covered here: **it carries the field and decides
nothing** — `media_group_id` on the record, additive, with the offset/ack semantics byte-for-byte what
they were (including across an album batch). Holding a growing group and handing it to the warm
session as ONE turn is the daemon's half and is tested with the daemon.

**No network, ever.** `get_updates` is mocked at every call site and no test passes `--download-dir`.
A live daemon may be holding a real conversation on the configured bot token, and a test that posts
for real is a bug.

Run:  python -m unittest seneschal.scripts.test_telegram_albums   (or)   python test_telegram_albums.py
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

import telegram_poll as tp  # noqa: E402

GROUP = "13500219378420999"
CAPTION = "look at how they answered me here"


class PollerCarriesTheField(unittest.TestCase):
    """`telegram_poll.py` extracts `media_group_id` and does NOTHING with it (spec §6c, rail 1)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.offset = os.path.join(self.dir, "telegram-offset")
        self.cache = os.path.join(self.dir, "custom-emoji-cache.json")

    def _run(self, updates, *extra_argv):
        argv = ["telegram_poll.py", "--offset-file", self.offset,
                "--custom-emoji-cache", self.cache, *extra_argv]
        out = io.StringIO()
        with mock.patch.object(tp, "get_updates", return_value=updates), \
                mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "not-a-real-token"}, clear=True), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out):
            rc = tp.main()
        return rc, json.loads(out.getvalue().strip().splitlines()[-1])

    @staticmethod
    def _update(update_id, message_id, group=GROUP, caption=None):
        msg = {"message_id": message_id, "chat": {"id": 555, "type": "private"},
               "from": {"username": "owner"}, "date": 1_000_000,
               "photo": [{"file_id": f"f{message_id}", "file_unique_id": f"u{message_id}",
                          "file_size": 10}]}
        if group is not None:
            msg["media_group_id"] = group
        if caption:
            msg["caption"] = caption
        return {"update_id": update_id, "message": msg}

    def test_extract_returns_the_id(self):
        self.assertEqual(tp.extract_media_group_id({"media_group_id": GROUP}), GROUP)

    def test_absent_is_none_and_the_record_omits_it(self):
        self.assertIsNone(tp.extract_media_group_id({"text": "hi"}))
        _rc, res = self._run([self._update(41, 7, group=None)])
        self.assertNotIn("media_group_id", res["messages"][0])

    def test_a_non_string_or_empty_id_is_read_as_no_album(self):
        """Fail-open: the answer is today's behaviour, one message per photo — never a coercion we
        would then have to defend, and never a group that swallows unrelated media."""
        for bad in (0, 1, True, None, [], {}, "", "   "):
            self.assertIsNone(tp.extract_media_group_id({"media_group_id": bad}), repr(bad))

    def test_the_id_reaches_the_record(self):
        _rc, res = self._run([self._update(41, 7, caption=CAPTION)])
        item = res["messages"][0]
        self.assertEqual(item["media_group_id"], GROUP)
        self.assertEqual(item["caption"], CAPTION)
        self.assertEqual(item["kind"], "message")

    def test_the_offset_advances_across_an_album_batch_exactly_as_before(self):
        """THE INVARIANT THIS FEATURE MUST NOT TOUCH. Nothing is acked before it has been read, and
        every update in the batch is acked once it has been — carrying a field changes neither."""
        updates = [self._update(40 + i, 7 + i) for i in range(9)]
        _rc, res = self._run(updates, "--commit")
        self.assertEqual(res["count"], 9)
        self.assertEqual(res["next_offset"], 49)
        self.assertTrue(res["committed"])
        with open(self.offset, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "49")
        # A peek still reads without acknowledging — the poller holds nothing back either way.
        _rc, res = self._run([self._update(60, 99)])
        self.assertFalse(res["committed"])


if __name__ == "__main__":
    unittest.main()
