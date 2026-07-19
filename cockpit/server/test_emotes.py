#!/usr/bin/env python3
"""Tests for cockpit/server/emotes.py — GET /api/emotes listing + file resolution. Pure stdlib, no
FastAPI needed, so this runs unconditionally.

Run: python -m unittest cockpit.server.test_emotes
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import emotes  # noqa: E402


class ListEmotes(unittest.TestCase):
    def test_none_dir_means_feature_off(self):
        self.assertEqual(emotes.list_emotes(None), [])

    def test_missing_dir_is_empty_not_an_error(self):
        d = Path(tempfile.mkdtemp()) / "does-not-exist"
        self.assertEqual(emotes.list_emotes(d), [])

    def test_lists_allowed_extensions_sorted_by_shortcode(self):
        d = Path(tempfile.mkdtemp())
        (d / "pog.png").write_bytes(b"x")
        (d / "Aww.gif").write_bytes(b"x")
        (d / "notes.txt").write_text("not an image")  # ignored — wrong extension
        out = emotes.list_emotes(d)
        self.assertEqual(out, [
            {"shortcode": "aww", "file": "Aww.gif"},
            {"shortcode": "pog", "file": "pog.png"},
        ])

    def test_case_collision_keeps_first_seen(self):
        d = Path(tempfile.mkdtemp())
        (d / "pog.png").write_bytes(b"x")
        (d / "POG.gif").write_bytes(b"x")
        out = emotes.list_emotes(d)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["shortcode"], "pog")

    def test_subdirectories_are_ignored(self):
        d = Path(tempfile.mkdtemp())
        (d / "sub").mkdir()
        (d / "pog.png").write_bytes(b"x")
        out = emotes.list_emotes(d)
        self.assertEqual([e["shortcode"] for e in out], ["pog"])


class ResolveEmoteFile(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "pog.png").write_bytes(b"x")

    def test_none_dir_resolves_nothing(self):
        self.assertIsNone(emotes.resolve_emote_file(None, "pog.png"))

    def test_resolves_a_real_file(self):
        self.assertEqual(emotes.resolve_emote_file(self.dir, "pog.png"), self.dir / "pog.png")

    def test_missing_file_is_none(self):
        self.assertIsNone(emotes.resolve_emote_file(self.dir, "nope.png"))

    def test_blank_filename_is_none(self):
        self.assertIsNone(emotes.resolve_emote_file(self.dir, ""))

    def test_path_traversal_is_rejected(self):
        outside = self.dir.parent / "secret.txt"
        outside.write_text("shh")
        self.assertIsNone(emotes.resolve_emote_file(self.dir, "../secret.txt"))
        self.assertIsNone(emotes.resolve_emote_file(self.dir, "..\\secret.txt"))


if __name__ == "__main__":
    unittest.main()
