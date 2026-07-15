#!/usr/bin/env python3
"""Unit tests for archive_aggregate: merge order/tie-break, media collection, md/html rendering."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import archive_common as ac  # noqa: E402
import archive_aggregate as agg  # noqa: E402


def rec(service, mid, ts, direction="from_them", text="", media=None, price=None, is_tip=False):
    return ac.make_record(service=service, service_msg_id=mid, person="alex", thread_id="1",
                          direction=direction, ts_utc=ts, text=text, media=media or [],
                          price=price, is_tip=is_tip)


class TestMerge(unittest.TestCase):
    def test_chronological(self):
        recs = [rec("telegram", 2, 200), rec("discord", 1, 100), rec("telegram", 3, 300)]
        out = agg.merge(recs)
        self.assertEqual([r["ts_utc"] for r in out], [100, 200, 300])

    def test_tie_break_service_then_id(self):
        recs = [rec("telegram", 5, 100), rec("discord", 9, 100), rec("discord", 2, 100)]
        out = agg.merge(recs)
        # same ts → discord before telegram; within discord, numeric id order
        self.assertEqual([(r["service"], r["service_msg_id"]) for r in out],
                         [("discord", "2"), ("discord", "9"), ("telegram", "5")])


class TestRenderMd(unittest.TestCase):
    def test_speakers_and_ppv(self):
        recs = agg.merge([rec("discord", 1, 1770159199, "from_me", "unlock", price=15),
                          rec("telegram", 2, 1770159260, "from_them", "hi")])
        md = agg.render_md(recs, "Alex", multi=True, tz="UTC")
        self.assertIn("**You**", md)
        self.assertIn("**Alex**", md)
        self.assertIn("PPV $15", md)
        self.assertIn("via Discord", md)


class TestRenderHtml(unittest.TestCase):
    def test_self_contained(self):
        recs = agg.merge([rec("telegram", 1, 1770159199, "from_them", "hi <b>there</b>")])
        h = agg.render_html(recs, "Alex", multi=False, tz="UTC")
        head = h.split("</head>")[0]
        self.assertNotIn("http://", head)
        self.assertNotIn("https://", head)                 # no CDN refs
        self.assertIn("&lt;b&gt;there&lt;/b&gt;", h)        # text HTML-escaped

    def test_media_img_relative(self):
        m = ac.make_media(kind="photo", archive_path="media/telegram_1_0.jpg")
        recs = [rec("telegram", 1, 1770159199, "from_them", "", media=[m])]
        h = agg.render_html(recs, "Alex", multi=False, tz="UTC")
        self.assertIn('src="media/telegram_1_0.jpg"', h)


class TestCollectMedia(unittest.TestCase):
    def test_copy_and_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "s.jpg")
            with open(src, "wb") as fh:
                fh.write(b"bytes")
            m1 = ac.make_media(kind="photo", local_path=src)
            m2 = ac.make_media(kind="photo", local_path=src)
            recs = [rec("telegram", 1, 1, media=[m1]), rec("discord", 2, 2, media=[m2])]
            media_dir = os.path.join(d, "media")
            agg.collect_media(recs, media_dir, copy=True)
            self.assertEqual(recs[0]["media"][0]["archive_path"], recs[1]["media"][0]["archive_path"])
            self.assertEqual(len(os.listdir(media_dir)), 1)   # deduped

    def test_no_copy(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "s.jpg")
            with open(src, "wb") as fh:
                fh.write(b"x")
            m = ac.make_media(kind="photo", local_path=src)
            recs = [rec("telegram", 1, 1, media=[m])]
            agg.collect_media(recs, os.path.join(d, "media"), copy=False)
            self.assertIsNone(recs[0]["media"][0]["archive_path"])


if __name__ == "__main__":
    unittest.main()
