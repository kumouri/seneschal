#!/usr/bin/env python3
"""Unit tests for discord_export_ingest: channel selection, timestamps, attachments, CSV vintage."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import discord_export_ingest as dc  # noqa: E402

OWNER_ID = "999888777666555444"
ALEX_ID = "333444555666777888"
DM_CHANNEL = "111222333444555666"
GUILD_CHANNEL = "444555666777888999"

MESSAGES = [
    {"ID": 500100200300400500, "Timestamp": "2024-01-15 14:23:45", "Contents": "hello there",
     "Attachments": ""},
    {"ID": "500100200300400501", "Timestamp": "2024-01-15 14:25:00", "Contents": "",
     "Attachments": "https://cdn.discordapp.com/attachments/1/2/pic.jpg?ex=deadline"},
]


def make_package(root: str, channels: dict, index: dict | None = None) -> str:
    """Lay out a fake extracted Discord data package: messages/index.json + c<id>/ dirs."""
    mdir = os.path.join(root, "messages")
    os.makedirs(mdir, exist_ok=True)
    with open(os.path.join(mdir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index or {}, fh)
    for cid, (channel, msgs) in channels.items():
        cdir = os.path.join(mdir, f"c{cid}")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "channel.json"), "w", encoding="utf-8") as fh:
            json.dump(channel, fh)
        if msgs is not None:
            with open(os.path.join(cdir, "messages.json"), "w", encoding="utf-8") as fh:
                json.dump(msgs, fh)
    return root


def dm_channels():
    return {
        DM_CHANNEL: ({"id": DM_CHANNEL, "type": 1, "recipients": [OWNER_ID, ALEX_ID]}, MESSAGES),
        GUILD_CHANNEL: ({"id": GUILD_CHANNEL, "type": 0, "name": "general",
                         "guild": {"id": "777000111222333444", "name": "Some Server"}},
                        [{"ID": 1, "Timestamp": "2024-02-01 09:00:00", "Contents": "guild msg",
                          "Attachments": ""}]),
    }


class TestTimestamp(unittest.TestCase):
    def test_naive_treated_as_utc(self):
        self.assertEqual(dc.parse_ts("2024-01-15 14:23:45"), 1705328625)

    def test_offset_and_z_forms(self):
        self.assertEqual(dc.parse_ts("2024-01-15 14:23:45+00:00"), 1705328625)
        self.assertEqual(dc.parse_ts("2024-01-15T14:23:45.500Z"), 1705328625)

    def test_empty(self):
        self.assertEqual(dc.parse_ts(""), 0)
        self.assertEqual(dc.parse_ts(None), 0)


class TestSelection(unittest.TestCase):
    def test_dm_matched_by_user_id(self):
        with tempfile.TemporaryDirectory() as d:
            make_package(d, dm_channels())
            chans = dc.discover_channels(d)
            sel = dc.select_channels(chans, user_id=ALEX_ID)
        self.assertEqual([c["id"] for c in sel], [DM_CHANNEL])   # guild channel NOT auto-selected

    def test_explicit_channel_id(self):
        with tempfile.TemporaryDirectory() as d:
            make_package(d, dm_channels())
            chans = dc.discover_channels(d)
            sel = dc.select_channels(chans, channel_ids=[GUILD_CHANNEL])
        self.assertEqual([c["id"] for c in sel], [GUILD_CHANNEL])

    def test_union_of_ids_and_user_id(self):
        with tempfile.TemporaryDirectory() as d:
            make_package(d, dm_channels())
            chans = dc.discover_channels(d)
            sel = dc.select_channels(chans, channel_ids=[GUILD_CHANNEL], user_id=ALEX_ID)
        self.assertEqual(sorted(c["id"] for c in sel), sorted([DM_CHANNEL, GUILD_CHANNEL]))

    def test_recipient_dicts_also_match(self):
        chan = {"channel": {"recipients": [{"id": OWNER_ID}, {"id": ALEX_ID}]}, "id": "1", "dir": ""}
        self.assertEqual(dc.select_channels([chan], user_id=ALEX_ID), [chan])

    def test_no_match_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            make_package(d, dm_channels())
            chans = dc.discover_channels(d)
        self.assertEqual(dc.select_channels(chans, user_id="000111222333444555"), [])


class TestIngest(unittest.TestCase):
    def _ingest_dm(self, d):
        make_package(d, dm_channels(), index={DM_CHANNEL: "Direct Message with alex_handle#0000"})
        chans = dc.discover_channels(d)
        chan = dc.select_channels(chans, user_id=ALEX_ID)[0]
        return dc.ingest_channel(chan, "alex", "Owner", "Direct Message with alex_handle#0000")

    def test_all_from_me_and_ids_stringified(self):
        with tempfile.TemporaryDirectory() as d:
            recs, src = self._ingest_dm(d)
        self.assertEqual([r["direction"] for r in recs], ["from_me", "from_me"])
        self.assertEqual(recs[0]["service_msg_id"], "500100200300400500")   # int ID
        self.assertEqual(recs[1]["service_msg_id"], "500100200300400501")   # string ID
        self.assertEqual(recs[0]["ts_utc"], 1705328625)
        self.assertEqual(recs[0]["thread_id"], DM_CHANNEL)
        self.assertTrue(src.endswith("messages.json"))

    def test_attachment_only_message(self):
        with tempfile.TemporaryDirectory() as d:
            recs, _ = self._ingest_dm(d)
        rec = recs[1]
        self.assertEqual(rec["text"], "")                          # empty Contents survives
        self.assertEqual(len(rec["media"]), 1)
        m = rec["media"][0]
        self.assertEqual(m["kind"], "photo")
        self.assertTrue(m["remote_url"].startswith("https://cdn.discordapp.com/"))
        self.assertTrue(m["meta"]["missing"])                      # CDN links expire; never fetched
        self.assertIsNone(m["local_path"])
        self.assertEqual(m["meta"]["original_name"], "pic.jpg")    # query string stripped

    def test_multiple_attachments_space_separated(self):
        media = dc.attachment_media("https://cdn.example/a.png https://cdn.example/b.mp4")
        self.assertEqual([m["kind"] for m in media], ["photo", "video"])
        self.assertEqual(dc.attachment_media(""), [])
        self.assertEqual(dc.attachment_media(None), [])

    def test_csv_vintage_errors_clearly(self):
        with tempfile.TemporaryDirectory() as d:
            make_package(d, {DM_CHANNEL: ({"id": DM_CHANNEL, "type": 1,
                                           "recipients": [OWNER_ID, ALEX_ID]}, None)})
            cdir = os.path.join(d, "messages", f"c{DM_CHANNEL}")
            with open(os.path.join(cdir, "messages.csv"), "w", encoding="utf-8") as fh:
                fh.write("ID,Timestamp,Contents,Attachments\n")
            chan = dc.discover_channels(d)[0]
            with self.assertRaises(ValueError) as ctx:
                dc.ingest_channel(chan, "alex", "Owner")
        self.assertIn("CSV", str(ctx.exception))
        self.assertIn("Request all of my data", str(ctx.exception))

    def test_not_a_package_errors(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                dc.discover_channels(d)                            # no messages/ dir

    def test_rerun_is_idempotent(self):
        # The writer atomically replaces the whole file, so equal inputs → equal outputs.
        with tempfile.TemporaryDirectory() as d:
            first, _ = self._ingest_dm(d)
            second, _ = self._ingest_dm(d)
        self.assertEqual(first, second)

    def test_lowercase_key_vintage(self):
        with tempfile.TemporaryDirectory() as d:
            msgs = [{"id": 7, "timestamp": "2024-01-15 14:23:45", "contents": "hi", "attachments": ""}]
            make_package(d, {DM_CHANNEL: ({"id": DM_CHANNEL, "type": 1,
                                           "recipients": [ALEX_ID]}, msgs)})
            chan = dc.discover_channels(d)[0]
            recs, _ = dc.ingest_channel(chan, "alex", "Owner")
        self.assertEqual(recs[0]["service_msg_id"], "7")
        self.assertEqual(recs[0]["text"], "hi")


if __name__ == "__main__":
    unittest.main()
