#!/usr/bin/env python3
"""Tests for cockpit/server/transcript.py — the GET /api/transcript backfill reader. Pure stdlib, no
FastAPI needed, so this runs unconditionally.

Run: python -m unittest cockpit.server.test_transcript
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import transcript  # noqa: E402


class TranscriptBackfill(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _write(self, records):
        path = self.dir / transcript.TRANSCRIPT_FILE
        with open(path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def test_missing_file_is_empty(self):
        self.assertEqual(transcript.read_transcript_tail(self.dir), [])

    def test_returns_newest_n_in_chronological_order(self):
        self._write([{"i": i} for i in range(10)])
        tail = transcript.read_transcript_tail(self.dir, limit=3)
        self.assertEqual([e["i"] for e in tail], [7, 8, 9])

    def test_tolerates_corrupt_lines(self):
        path = self.dir / transcript.TRANSCRIPT_FILE
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"i": 1}) + "\n")
            fh.write("not json\n")
            fh.write(json.dumps({"i": 2}) + "\n")
            fh.write("[1, 2]\n")  # valid JSON, not an object — also skipped
        tail = transcript.read_transcript_tail(self.dir, limit=10)
        self.assertEqual([e["i"] for e in tail], [1, 2])

    def test_limit_is_clamped_to_the_cap(self):
        self._write([{"i": i} for i in range(5)])
        tail = transcript.read_transcript_tail(self.dir, limit=transcript.TRANSCRIPT_CAP + 500)
        self.assertEqual(len(tail), 5)  # never errors, just can't return more than exists


if __name__ == "__main__":
    unittest.main()
