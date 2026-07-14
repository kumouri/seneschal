#!/usr/bin/env python3
"""Unit tests for telegram_ingest: text flattening, direction, service rows, media resolution."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import telegram_ingest as tg  # noqa: E402


class TestText(unittest.TestCase):
    def test_plain_string(self):
        plain, raw = tg.flatten_text({"text": "hi", "text_entities": [{"type": "plain", "text": "hi"}]})
        self.assertEqual(plain, "hi")
        self.assertIsNone(raw)

    def test_entity_list(self):
        msg = {"text": ["It's ", {"type": "bold", "text": "you"}, "?"],
               "text_entities": [{"type": "plain", "text": "It's "}, {"type": "bold", "text": "you"},
                                 {"type": "plain", "text": "?"}]}
        plain, raw = tg.flatten_text(msg)
        self.assertEqual(plain, "It's you?")
        self.assertEqual(raw, msg["text"])                # lossless original list preserved

    def test_list_without_entities(self):
        plain, raw = tg.flatten_text({"text": ["a", {"type": "link", "text": "b"}]})
        self.assertEqual(plain, "ab")


class TestDirection(unittest.TestCase):
    def test_tg_uid(self):
        self.assertEqual(tg.tg_uid("user2000000002"), "2000000002")
        self.assertEqual(tg.tg_uid(None), "")

    def test_ingest_direction_and_service(self):
        data = {"id": 2000000002, "messages": [
            {"id": 1, "type": "message", "date_unixtime": "1770159199", "from_id": "user1000000001",
             "from": "Owner", "text": "hey"},
            {"id": 2, "type": "message", "date_unixtime": "1770159260", "from_id": "user2000000002",
             "from": "Alex", "text": "hi"},
            {"id": 3, "type": "service", "date_unixtime": "1770159300", "action": "phone_call"},
        ]}
        with tempfile.TemporaryDirectory() as d:
            import json
            with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            recs = tg.ingest(d, "alex", "1000000001", "2000000002", "2000000002")
        self.assertEqual([r["direction"] for r in recs], ["from_me", "from_them", "system"])
        self.assertIn("phone call", recs[2]["text"])

    def test_skip_service(self):
        data = {"id": 1, "messages": [{"id": 3, "type": "service", "date_unixtime": "1", "action": "x"}]}
        with tempfile.TemporaryDirectory() as d:
            import json
            with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            recs = tg.ingest(d, "p", "1", "2", "1", skip_service=True)
        self.assertEqual(recs, [])


class TestMedia(unittest.TestCase):
    def test_photo_and_thumb(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "photos"))
            with open(os.path.join(d, "photos", "p.jpg"), "wb") as fh:
                fh.write(b"x")
            msg = {"photo": "photos/p.jpg", "thumbnail": "photos/p.jpg_thumb.jpg"}
            media = tg.resolve_media(msg, d)
        self.assertEqual(len(media), 1)                   # exactly one entry; thumb is NOT standalone
        self.assertEqual(media[0]["kind"], "photo")
        self.assertTrue(media[0]["local_path"].endswith("p.jpg"))
        self.assertIsNotNone(media[0]["meta"]["thumbnail"])
        self.assertFalse(media[0]["meta"]["missing"])

    def test_video_file_kind(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "video_files"))
            with open(os.path.join(d, "video_files", "v.mp4"), "wb") as fh:
                fh.write(b"x")
            msg = {"file": "video_files/v.mp4", "media_type": "video_file",
                   "mime_type": "video/mp4", "duration_seconds": 12}
            media = tg.resolve_media(msg, d)
        self.assertEqual(media[0]["kind"], "video")
        self.assertEqual(media[0]["meta"]["duration_seconds"], 12)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            media = tg.resolve_media({"file": "gone/x.bin", "media_type": "file"}, d)
        self.assertTrue(media[0]["meta"]["missing"])
        self.assertIsNone(media[0]["local_path"])


if __name__ == "__main__":
    unittest.main()
