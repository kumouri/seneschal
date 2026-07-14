#!/usr/bin/env python3
"""Unit tests for archive_common: schema, registry, timezone (tz_common shims), media, text. Stdlib only."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import archive_common as ac  # noqa: E402


class TestRecord(unittest.TestCase):
    def test_round_trip_and_derived_fields(self):
        r = ac.make_record(service="telegram", service_msg_id=523, person="alex",
                           thread_id=2000000002, direction="from_them", ts_utc=1770159199,
                           text="It's you?")
        self.assertEqual(r["service_msg_id"], "523")       # stringified
        self.assertEqual(r["thread_id"], "2000000002")
        self.assertEqual(r["ts_iso"], "2026-02-03T22:53:19Z")   # epoch is UTC; export's 16:53 was Chicago-local
        self.assertEqual(r["media"], [])
        self.assertEqual(r["text"], "It's you?")

    def test_text_never_none(self):
        r = ac.make_record(service="discord", service_msg_id=1, person="p", thread_id="1",
                           direction="from_me", ts_utc=0, text=None)
        self.assertEqual(r["text"], "")

    def test_bad_direction_raises(self):
        with self.assertRaises(ValueError):
            ac.make_record(service="x", service_msg_id=1, person="p", thread_id="1",
                           direction="sideways", ts_utc=0)


class TestRegistry(unittest.TestCase):
    PEOPLE = {
        "owner": {"discord": {"user_id": "1234567"}, "telegram": {"user_id": "1000000001"}},
        "people": {"alex": {"display_name": "Alex",
                              "services": {"discord": {"chat_id": "87654321"},
                                           "telegram": {"user_id": "2000000002"}}}},
    }

    def test_lookups(self):
        self.assertEqual(ac.owner_id(self.PEOPLE, "telegram"), "1000000001")
        self.assertEqual(ac.person_service(self.PEOPLE, "alex", "discord")["chat_id"], "87654321")
        self.assertEqual(ac.person_display(self.PEOPLE, "alex"), "Alex")

    def test_unknown_person_abstains(self):
        self.assertEqual(ac.person_service(self.PEOPLE, "nobody", "discord"), {})
        self.assertEqual(ac.person_display(self.PEOPLE, "nobody"), "nobody")  # falls back to key

    def test_example_seed_parses(self):
        seed = os.path.join(SCRIPT_DIR, "..", "state", "archive-people.example.json")
        people = ac.load_people(seed)
        self.assertIn("example-person", people["people"])


class TestTimezone(unittest.TestCase):
    def test_central_dst_rule_explicit_spec(self):
        # epoch 1770159199 = 2026-02-03T22:53:19Z (winter) → Chicago CST (−6) → 16:53.
        # Spec'd EXPLICITLY (not "auto") so the pin is host-independent: "auto" now follows the
        # owner's configured timezone (else machine-local) via tz_common instead of hardcoded
        # US-Central; the explicit Chicago spec stays deterministic with or without tzdata.
        self.assertEqual(ac.hhmm(1770159199, "America/Chicago"), "16:53")
        self.assertEqual(ac.resolve_offset(1770159199, "chicago"), -360)
        # a July instant is CDT (−5)
        july = 1783000000  # 2026-07-01ish
        self.assertEqual(ac.resolve_offset(july, "America/Chicago"), -300)

    def test_auto_follows_owner_tz(self):
        # The deprecated shim and tz_common must agree on "auto" — whatever this host's
        # configuration is (configured identity zone, else machine-local).
        import tz_common
        self.assertEqual(ac.resolve_offset(1770159199, "auto"), tz_common.offset_minutes(1770159199))
        self.assertEqual(ac.resolve_offset(1770159199, None), ac.resolve_offset(1770159199, ""))

    def test_central_shim_still_answers(self):
        # central_offset_minutes stays import-compatible and pins the same DST rule as before.
        from datetime import datetime
        self.assertEqual(ac.central_offset_minutes(datetime(2026, 2, 3, 22, 53, 19)), -360)
        self.assertEqual(ac.central_offset_minutes(datetime(2026, 7, 1, 12, 0)), -300)

    def test_utc_and_fixed(self):
        self.assertEqual(ac.hhmm(1770159199, "UTC"), "22:53")
        self.assertEqual(ac.resolve_offset(0, "UTC"), 0)
        self.assertEqual(ac.resolve_offset(0, "-120"), -120)


class TestMedia(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(ac.classify_kind("video_file"), "video")
        self.assertEqual(ac.classify_kind("voice_message"), "voice")
        self.assertEqual(ac.classify_kind("photo"), "photo")
        self.assertEqual(ac.classify_kind("gif"), "animation")
        self.assertEqual(ac.classify_kind(None), "unknown")

    def test_stable_name(self):
        self.assertEqual(ac.stable_media_name("telegram", "523", 1, ".jpg"), "telegram_523_1.jpg")
        self.assertEqual(ac.stable_media_name("discord", "9", 0, "mp4"), "discord_9_0.mp4")

    def test_copy_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.jpg"); b = os.path.join(d, "b.jpg")
            with open(a, "wb") as fh:
                fh.write(b"same-bytes")
            with open(b, "wb") as fh:
                fh.write(b"same-bytes")
            dest = os.path.join(d, "media")
            seen = {}
            r1 = ac.copy_media_dedup(a, dest, "telegram", "1", 0, seen)
            r2 = ac.copy_media_dedup(b, dest, "telegram", "2", 0, seen)
            self.assertEqual(r1, r2)                      # identical bytes → one file
            self.assertEqual(len(os.listdir(dest)), 1)

    def test_copy_in_place_no_selfcopy(self):
        # a collector already downloaded straight into media/ — must not copy onto itself
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "media")
            os.makedirs(dest)
            existing = os.path.join(dest, "discord_9_0.mp4")
            with open(existing, "wb") as fh:
                fh.write(b"vid")
            rel = ac.copy_media_dedup(existing, dest, "discord", "9", 0, {})
            self.assertEqual(rel, "media/discord_9_0.mp4")
            self.assertEqual(len(os.listdir(dest)), 1)    # still one file, no crash


class TestText(unittest.TestCase):
    def test_html_to_text(self):
        self.assertEqual(ac.html_to_text("<p>hi &amp; bye</p>"), "hi & bye")
        self.assertEqual(ac.html_to_text("a<br>b"), "a\nb")
        self.assertEqual(ac.html_to_text(None), "")


if __name__ == "__main__":
    unittest.main()
