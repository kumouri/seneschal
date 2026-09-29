#!/usr/bin/env python3
"""Tests for record_intel.py — the append-only pending-queue write path.

Stdlib only (unittest + tempfile), matching the sibling ``test_score_jobs.py`` /
``test_promote_intel.py`` so this runs in a plain-python CI job with no venv:

  python -m unittest discover -s archons/proteus/tools -p "test_*.py"

Every recording is stamped from an injected instant (``NOW``), never the wall clock.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import record_intel as ri  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 30, 0, tzinfo=timezone.utc)


class RecordIntelTests(unittest.TestCase):
    """record_intel binds INTEL_PENDING_FILE at import time — save + restore it, or a leaked path
    points a later test (or a real run) at a deleted tmpdir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pending = Path(self.tmp) / "nested" / "company-intel-pending.jsonl"
        self._saved = ri.INTEL_PENDING_FILE
        ri.INTEL_PENDING_FILE = self.pending

    def tearDown(self):
        ri.INTEL_PENDING_FILE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _main(self, payload) -> int:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return ri.main(["--json", raw], now=NOW)

    def _lines(self) -> list[dict]:
        if not self.pending.exists():
            return []
        return [json.loads(line) for line in self.pending.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    # --- happy path ------------------------------------------------------------------------

    def test_happy_path_appends_one_line(self):
        code = self._main({"company": "Acme", "adjust": -5, "tags": ["layoffs"],
                           "note": "reduction in force", "source": "work-up acme-swe"})
        self.assertEqual(code, 0)
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        entry = lines[0]
        self.assertEqual(entry["company"], "Acme")
        self.assertEqual(entry["adjust"], -5.0)
        self.assertEqual(entry["tags"], ["layoffs"])
        self.assertEqual(entry["note"], "reduction in force")
        self.assertEqual(entry["source"], "work-up acme-swe")

    def test_creates_parent_dir(self):
        self.assertFalse(self.pending.parent.exists())
        self._main({"company": "Acme", "adjust": 2})
        self.assertTrue(self.pending.parent.is_dir())
        self.assertTrue(self.pending.exists())

    def test_two_calls_append_two_lines(self):
        self._main({"company": "Acme", "adjust": 2})
        self._main({"company": "Globex", "adjust": -3})
        self.assertEqual([e["company"] for e in self._lines()], ["Acme", "Globex"])

    def test_optional_fields_default_sensibly(self):
        self._main({"company": "Acme", "adjust": 2})
        entry = self._lines()[0]
        self.assertEqual(entry["tags"], [])
        self.assertEqual(entry["note"], "")
        self.assertEqual(entry["source"], "")

    # --- stamping ----------------------------------------------------------------------------

    def test_recorded_at_is_the_injected_instant_in_utc(self):
        self._main({"company": "Acme", "adjust": 2})
        self.assertEqual(self._lines()[0]["recorded_at"], "2026-09-01T12:30:00Z")

    def test_updated_is_a_date(self):
        self._main({"company": "Acme", "adjust": 2})
        updated = self._lines()[0]["updated"]
        # The owner-local date of NOW — within a day of the UTC date whatever the configured zone.
        self.assertIn(updated, ("2026-08-31", "2026-09-01", "2026-09-02"))

    def test_updated_and_recorded_at_are_stamped_not_caller_supplied(self):
        code = self._main({"company": "Acme", "adjust": 2, "updated": "1999-01-01",
                           "recorded_at": "1999-01-01T00:00:00Z"})
        self.assertEqual(code, 0)
        entry = self._lines()[0]
        self.assertNotEqual(entry["updated"], "1999-01-01")
        self.assertEqual(entry["recorded_at"], "2026-09-01T12:30:00Z")

    # --- clamping ------------------------------------------------------------------------------

    def test_clamps_large_positive_adjust(self):
        self._main({"company": "Acme", "adjust": 999})
        self.assertEqual(self._lines()[0]["adjust"], ri.INTEL_ADJUST_MAX)

    def test_clamps_large_negative_adjust(self):
        self._main({"company": "Acme", "adjust": -999})
        self.assertEqual(self._lines()[0]["adjust"], -ri.INTEL_ADJUST_MAX)

    def test_boundary_values_are_not_clamped(self):
        self._main({"company": "Acme", "adjust": 20})
        self._main({"company": "Globex", "adjust": -20})
        lines = self._lines()
        self.assertEqual(lines[0]["adjust"], 20.0)
        self.assertEqual(lines[1]["adjust"], -20.0)

    # --- validation ----------------------------------------------------------------------------

    def test_malformed_json_rejected_with_exit_2(self):
        self.assertEqual(self._main("{not json"), 2)
        self.assertEqual(self._lines(), [])

    def test_json_array_rejected(self):
        self.assertEqual(self._main("[1, 2, 3]"), 2)
        self.assertEqual(self._lines(), [])

    def test_bad_entries_rejected(self):
        for bad in ({"adjust": 2}, {"company": "   ", "adjust": 2}, {"company": "Acme"},
                    {"company": "Acme", "adjust": "very bad"}, {"company": "Acme", "adjust": None},
                    {"company": "Acme", "adjust": 2, "tags": "layoffs"},
                    {"company": "Acme", "adjust": 2, "tags": ["ok", 5]},
                    {"company": "Acme", "adjust": 2, "note": 5},
                    {"company": "Acme", "adjust": 2, "source": 5}):
            with self.subTest(entry=bad):
                self.assertEqual(self._main(bad), 2)
        self.assertEqual(self._lines(), [])

    def test_string_adjust_that_parses_as_number_is_accepted(self):
        self.assertEqual(self._main({"company": "Acme", "adjust": "5"}), 0)
        self.assertEqual(self._lines()[0]["adjust"], 5.0)

    def test_validate_as_a_unit(self):
        self.assertIsNone(ri.validate({"company": "Acme", "adjust": 1}))
        self.assertIsNotNone(ri.validate(["nope"]))


if __name__ == "__main__":
    unittest.main()
