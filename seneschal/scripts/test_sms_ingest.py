#!/usr/bin/env python3
"""Unit tests for sms_ingest: direction, number matching, MMS parts/dates, tolerance, iterparse."""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import sms_ingest as sm  # noqa: E402

ALEX = ["+15551234567"]
PNG_B64 = base64.b64encode(b"\x89PNG-fake-image-bytes").decode()

SMS_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<smses count="5">
  <sms protocol="0" address="+15551234567" date="1718300000000" type="1" body="hey there"
       contact_name="Alex" date_sent="1718299998000" />
  <sms protocol="0" address="555-123-4567" date="1718300060000" type="2" body="hi!" />
  <sms protocol="0" address="+15559876543" date="1718300120000" type="1" body="wrong person" />
  <sms protocol="0" address="(555) 123-4567" date="1718300180000" type="1" />
  <sms protocol="0" address="+15551234567" date="1718300240000" type="3" body="a draft" />
</smses>
"""

MMS_XML = f"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<smses count="2">
  <mms date="1718300300" msg_box="1" address="+15551234567" m_id="mid-001" contact_name="Alex">
    <parts>
      <part seq="-1" ct="application/smil" text="&lt;smil/&gt;" />
      <part seq="0" ct="text/plain" text="check this out" />
      <part seq="1" ct="image/png" name="photo.png" data="{PNG_B64}" />
    </parts>
    <addrs>
      <addr address="+15551234567" type="137" charset="106" />
      <addr address="+15550001111" type="151" charset="106" />
    </addrs>
  </mms>
  <mms date="1718300400000" msg_box="2">
    <parts><part seq="0" ct="text/plain" text="sent mms" /></parts>
    <addrs><addr address="1-555-123-4567" type="151" charset="106" /></addrs>
  </mms>
</smses>
"""


def write_xml(d: str, text: str) -> str:
    path = os.path.join(d, "sms-20260101120000.xml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def run_ingest(xml_text: str, numbers=None):
    with tempfile.TemporaryDirectory() as d:
        path = write_xml(d, xml_text)
        media_dir = os.path.join(d, "media")
        records, stats = sm.ingest(path, "alex", numbers or ALEX, "Alex", media_dir)
        media_files = sorted(os.listdir(media_dir)) if os.path.isdir(media_dir) else []
    return records, stats, media_files


class TestNumbers(unittest.TestCase):
    def test_normalization_forms(self):
        self.assertEqual(sm.norm_number("+1 (555) 123-4567"), "5551234567")
        self.assertEqual(sm.norm_number("555-123-4567"), "5551234567")
        self.assertEqual(sm.norm_number("15551234567"), "5551234567")
        self.assertEqual(sm.norm_number(None), "")

    def test_match(self):
        self.assertTrue(sm.numbers_match("+15551234567", "555.123.4567"))
        self.assertFalse(sm.numbers_match("+15551234567", "+15559876543"))
        self.assertFalse(sm.numbers_match("", ""))                 # empty never matches

    def test_short_code_exact_only(self):
        self.assertTrue(sm.numbers_match("55123", "55123"))
        self.assertFalse(sm.numbers_match("55123", "5551234567"))


class TestEpoch(unittest.TestCase):
    def test_millis_vs_seconds(self):
        self.assertEqual(sm.epoch_seconds("1718300000000"), 1718300000)   # millis
        self.assertEqual(sm.epoch_seconds("1718300300"), 1718300300)      # seconds pass through
        self.assertEqual(sm.epoch_seconds("garbage"), 0)
        self.assertEqual(sm.epoch_seconds(None), 0)


class TestSms(unittest.TestCase):
    def test_directions_matching_and_skips(self):
        records, stats, _ = run_ingest(SMS_XML)
        self.assertEqual(len(records), 3)
        self.assertEqual([r["direction"] for r in records], ["from_them", "from_me", "from_them"])
        self.assertEqual(stats["skipped_nonmatch"], 1)             # the +15559876543 row
        self.assertEqual(stats["skipped_other"], 1)                # the type=3 draft

    def test_fields(self):
        records, _, _ = run_ingest(SMS_XML)
        r = records[0]
        self.assertEqual(r["ts_utc"], 1718300000)                  # millis → seconds
        self.assertEqual(r["text"], "hey there")
        self.assertEqual(r["sender_name"], "Alex")
        self.assertEqual(r["thread_id"], "5551234567")             # normalized peer number
        self.assertEqual(records[1]["sender_name"], "")            # sent → owner, no sender label

    def test_missing_body_tolerated(self):
        records, _, _ = run_ingest(SMS_XML)
        self.assertEqual(records[2]["text"], "")                   # the body-less row still lands


class TestMms(unittest.TestCase):
    def test_received_with_media_and_text(self):
        records, stats, media_files = run_ingest(MMS_XML)
        self.assertEqual(len(records), 2)
        r = records[0]
        self.assertEqual(r["direction"], "from_them")
        self.assertEqual(r["ts_utc"], 1718300300)                  # seconds-form date
        self.assertEqual(r["text"], "check this out")              # text part → body; smil dropped
        self.assertEqual(len(r["media"]), 1)
        m = r["media"][0]
        self.assertEqual(m["kind"], "photo")
        self.assertEqual(m["mime"], "image/png")
        # Named by media-list index (not XML part index) so the aggregator re-derives the
        # identical name and its copy pass self-copy no-ops instead of duplicating the file.
        self.assertEqual(m["archive_path"], "media/sms_mid-001_0.png")
        self.assertEqual(os.path.basename(m["local_path"]), "sms_mid-001_0.png")
        self.assertEqual(media_files, ["sms_mid-001_0.png"])       # decoded into media/
        self.assertEqual(m["bytes"], len(b"\x89PNG-fake-image-bytes"))
        self.assertEqual(stats["media_errors"], 0)

    def test_sent_millis_date_and_addr_matching(self):
        records, _, _ = run_ingest(MMS_XML)
        r = records[1]
        self.assertEqual(r["direction"], "from_me")                # msg_box=2
        self.assertEqual(r["ts_utc"], 1718300400)                  # millis-form date
        self.assertEqual(r["text"], "sent mms")
        self.assertEqual(r["thread_id"], "5551234567")             # matched via <addr>, no address attr

    def test_bad_base64_counted_not_fatal(self):
        xml = """<?xml version='1.0'?><smses count="1">
          <mms date="1718300300" msg_box="1" address="+15551234567">
            <parts><part seq="0" ct="image/jpeg" data="%%%not-base64" /></parts>
          </mms></smses>"""
        records, stats, _ = run_ingest(xml)
        self.assertEqual(len(records), 1)                          # record survives, media dropped
        self.assertEqual(records[0]["media"], [])
        self.assertEqual(stats["media_errors"], 1)


class TestLargeFile(unittest.TestCase):
    def test_iterparse_over_generated_rows(self):
        rows = []
        for i in range(300):
            addr = "+15551234567" if i % 2 == 0 else "+15550009999"
            rows.append(f'<sms protocol="0" address="{addr}" date="{1718300000000 + i * 1000}" '
                        f'type="{1 if i % 4 else 2}" body="msg {i}" />')
        xml = f'<?xml version="1.0"?><smses count="300">{"".join(rows)}</smses>'
        records, stats, _ = run_ingest(xml)
        self.assertEqual(len(records), 150)                        # every other row matches
        self.assertEqual(stats["skipped_nonmatch"], 150)
        self.assertEqual(records[0]["ts_utc"], 1718300000)
        self.assertEqual(records[-1]["ts_utc"], 1718300298)


class TestMimeHelpers(unittest.TestCase):
    def test_kind(self):
        self.assertEqual(sm.mime_kind("image/jpeg"), "photo")
        self.assertEqual(sm.mime_kind("image/gif"), "animation")
        self.assertEqual(sm.mime_kind("video/mp4"), "video")
        self.assertEqual(sm.mime_kind("audio/amr"), "audio")
        self.assertEqual(sm.mime_kind("text/x-vcard"), "file")


if __name__ == "__main__":
    unittest.main()
