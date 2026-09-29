#!/usr/bin/env python3
"""Tests for `record_search.py` — existence-first search over the durable conversation record.

The suite is built directly against the measured defect (module docstring, `record_search.py`):
`grep -o` with a fixed-width context window silently drops a real match when the phrase sits nearer
the start or end of its line than the requested padding. `RegressionTest` reconstructs exactly that
shape and asserts this tool still finds it. The rest of the suite covers the other three invariants:
multi-term independence, a missing/unreadable store reporting as an error rather than a zero, the
JSON shape holding `terms`/`searched` unconditionally, and case-insensitivity.

Run:  python -m unittest test_record_search   (or)   python test_record_search.py
"""
import json
import os
import stat
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import record_search as rs  # noqa: E402


def _write(path: str, rows: list) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _turn(text: str, speaker: str = "owner", at: str = "2026-09-10T04:36:00Z",
          surface: str = "telegram") -> dict:
    return {"schema": "seneschal.turn/1", "at": at, "surface": surface, "speaker": speaker,
            "text": text}


class RegressionTest(unittest.TestCase):
    """The exact shape of the measured miss: a phrase closer to the start/end of its record's text
    than a padded-window search would require. A fixed-string count must find it regardless."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_phrase_near_start_of_text_is_found(self):
        # The phrase sits 5 characters from the start — far less than the 500 the original grep -o
        # window required before it would emit anything at all.
        text = "abcde blue notebook on the desk today, and it helped a lot with the planning"
        _write(os.path.join(self.dir, "turns.jsonl"), [_turn(text)])
        result = rs.search(self.dir, ["blue notebook on"], stores=["turns"])
        self.assertEqual(result["matches_per_term"]["blue notebook on"], 1)
        self.assertTrue(result["ok"])

    def test_phrase_near_end_of_text_is_found(self):
        # The needle sits right up against the tail of the string — 0 characters of trailing context,
        # far less than the 400 the original grep -o window required on that side.
        text = "the whole point of a daily review is that it rounds th"
        _write(os.path.join(self.dir, "turns.jsonl"), [_turn(text)])
        result = rs.search(self.dir, ["it rounds th"], stores=["turns"])
        self.assertEqual(result["matches_per_term"]["it rounds th"], 1)

    def test_context_never_suppresses_a_hit_at_the_very_edge(self):
        """A match at character 0 of the text, with a wide requested context, must still be counted
        and must still produce a context excerpt — the property the padded grep window lacked."""
        text = "invoice export landed"
        _write(os.path.join(self.dir, "turns.jsonl"), [_turn(text)])
        result = rs.search(self.dir, ["invoice"], stores=["turns"], context_chars=500)
        self.assertEqual(result["matches_per_term"]["invoice"], 1)
        self.assertEqual(len(result["matches"]["invoice"]), 1)
        self.assertIn("invoice", result["matches"]["invoice"][0]["context"][0])

    def test_overlapping_style_repeats_are_all_counted(self):
        text = "it rounds th, it rounds th, it rounds th"
        _write(os.path.join(self.dir, "turns.jsonl"), [_turn(text)])
        result = rs.search(self.dir, ["it rounds th"], stores=["turns"])
        self.assertEqual(result["matches_per_term"]["it rounds th"], 3)


class MultiTermTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        _write(os.path.join(self.dir, "turns.jsonl"), [
            _turn("the blue notebook on my desk"),
            _turn("the blue notebook on again today"),
            _turn("and it rounds th towards the total too, the blue notebook on"),
        ])

    def test_one_term_absent_does_not_read_as_global_absence(self):
        result = rs.search(self.dir, ["blue notebook on", "invoice"], stores=["turns"])
        self.assertEqual(result["matches_per_term"]["blue notebook on"], 3)
        self.assertEqual(result["matches_per_term"]["invoice"], 0)
        # Both terms are present in the answer regardless of which one hit.
        self.assertEqual(set(result["terms"]), {"blue notebook on", "invoice"})
        self.assertTrue(result["ok"])


class StoreErrorTest(unittest.TestCase):
    """A store that cannot be opened at all must report as an error, never as a silent zero — the
    exact distinction the original incident collapsed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_missing_store_is_an_error_not_a_zero(self):
        # No turns.jsonl written at all in this directory.
        result = rs.search(self.dir, ["anything"], stores=["turns"])
        self.assertFalse(result["ok"])
        entry = result["searched"][0]
        self.assertEqual(entry["store"], "turns")
        self.assertFalse(entry["ok"])
        self.assertIsNotNone(entry["error"])
        # The count is still present and still zero-valued in shape, but `ok: False` is what a caller
        # must check before ever reporting "not found" to the owner.
        self.assertEqual(result["matches_per_term"]["anything"], 0)

    def test_one_unreadable_store_does_not_hide_a_real_match_in_another(self):
        _write(os.path.join(self.dir, "turns.jsonl"), [_turn("blue notebook on the desk")])
        # assertions.jsonl deliberately not created.
        result = rs.search(self.dir, ["blue notebook on"], stores=["turns", "assertions"])
        self.assertFalse(result["ok"])  # the assertions store failed
        self.assertEqual(result["matches_per_term"]["blue notebook on"], 1)  # turns still counted
        by_store = {e["store"]: e for e in result["searched"]}
        self.assertTrue(by_store["turns"]["ok"])
        self.assertFalse(by_store["assertions"]["ok"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits; Windows ACLs behave differently")
    def test_permission_denied_store_is_an_error(self):
        path = os.path.join(self.dir, "turns.jsonl")
        _write(path, [_turn("hello")])
        os.chmod(path, 0)
        try:
            result = rs.search(self.dir, ["hello"], stores=["turns"])
            self.assertFalse(result["ok"])
            self.assertIsNotNone(result["searched"][0]["error"])
        finally:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


class ShapeTest(unittest.TestCase):
    """`terms` and `searched` must appear in the output whatever happened — the structural guarantee
    that "not found" can never be reported as a bare, unstructured absence."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_zero_result_still_carries_terms_and_searched(self):
        _write(os.path.join(self.dir, "turns.jsonl"), [_turn("nothing relevant here")])
        result = rs.search(self.dir, ["totally-absent-phrase"], stores=["turns"])
        self.assertIn("terms", result)
        self.assertIn("searched", result)
        self.assertIn("matches_per_term", result)
        self.assertEqual(result["terms"], ["totally-absent-phrase"])
        self.assertEqual(result["matches_per_term"]["totally-absent-phrase"], 0)
        self.assertTrue(result["ok"])
        # And the JSON-serialised form carries the same guarantee — this is what a caller (or a
        # future turn) actually reads.
        blob = json.loads(json.dumps(result))
        self.assertIn("terms", blob)
        self.assertIn("searched", blob)

    def test_error_result_still_carries_terms_and_searched(self):
        result = rs.search(self.dir, ["x"], stores=["turns"])  # file does not exist
        blob = json.loads(json.dumps(result))
        self.assertEqual(blob["terms"], ["x"])
        self.assertIn("searched", blob)
        self.assertFalse(blob["ok"])


class CaseInsensitivityTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        _write(os.path.join(self.dir, "turns.jsonl"), [_turn("the Blue Notebook On my desk")])

    def test_default_is_case_insensitive(self):
        result = rs.search(self.dir, ["blue notebook on"], stores=["turns"])
        self.assertEqual(result["matches_per_term"]["blue notebook on"], 1)

    def test_case_sensitive_opt_in_respects_case(self):
        result = rs.search(self.dir, ["blue notebook on"], stores=["turns"], case_sensitive=True)
        self.assertEqual(result["matches_per_term"]["blue notebook on"], 0)
        result2 = rs.search(self.dir, ["Blue Notebook On"], stores=["turns"], case_sensitive=True)
        self.assertEqual(result2["matches_per_term"]["Blue Notebook On"], 1)


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        _write(os.path.join(self.dir, "turns.jsonl"), [
            _turn("blue notebook on", speaker="owner", at="2026-09-01T00:00:00Z",
                  surface="telegram"),
            _turn("blue notebook on again", speaker="assistant", at="2026-09-05T00:00:00Z",
                  surface="discord"),
        ])
        _write(os.path.join(self.dir, "assertions.jsonl"), [
            {"schema": "seneschal.assertion/1", "at": "2026-09-06T00:00:00Z", "surface": "telegram",
             "kind": "reply", "text": "blue notebook on, third time"},
        ])

    def test_speaker_filter_on_turns(self):
        result = rs.search(self.dir, ["blue notebook on"], stores=["turns"], speaker="owner")
        self.assertEqual(result["matches_per_term"]["blue notebook on"], 1)

    def test_speaker_assistant_includes_assertions_store(self):
        result = rs.search(self.dir, ["blue notebook on"], speaker="assistant")
        # the assistant's turns.jsonl row + the assertions.jsonl row (implicit assistant) = 2 records.
        self.assertEqual(result["matches_per_term"]["blue notebook on"], 2)

    def test_speaker_owner_excludes_assertions_store(self):
        result = rs.search(self.dir, ["blue notebook on"], speaker="owner")
        self.assertEqual(result["matches_per_term"]["blue notebook on"], 1)

    def test_since_until_bounds(self):
        result = rs.search(self.dir, ["blue notebook on"], stores=["turns"],
                           since=rs._parse_arg_stamp("2026-09-02"),
                           until=rs._parse_arg_stamp("2026-09-10"))
        self.assertEqual(result["matches_per_term"]["blue notebook on"], 1)

    def test_surface_filter(self):
        result = rs.search(self.dir, ["blue notebook on"], stores=["turns"], surface="discord")
        self.assertEqual(result["matches_per_term"]["blue notebook on"], 1)


class MalformedLineTest(unittest.TestCase):
    """A single garbled line inside an otherwise-open file costs that line, never the whole store —
    ordinary fail-open behaviour, distinct from the file being unopenable at all."""

    def test_malformed_line_is_skipped_not_fatal(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "turns.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_turn("blue notebook on")) + "\n")
            fh.write("{not json\n")
            fh.write(json.dumps(_turn("blue notebook on again")) + "\n")
        result = rs.search(d, ["blue notebook on"], stores=["turns"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["matches_per_term"]["blue notebook on"], 2)


class CliTest(unittest.TestCase):
    def test_json_output_round_trips(self):
        d = tempfile.mkdtemp()
        _write(os.path.join(d, "turns.jsonl"), [_turn("blue notebook on the desk")])
        argv = ["--state-dir", d, "--term", "blue notebook on", "--store", "turns", "--json"]
        buf = _CaptureStdout()
        with buf:
            code = rs.main(argv)
        self.assertEqual(code, 0)
        blob = json.loads(buf.value)
        self.assertEqual(blob["matches_per_term"]["blue notebook on"], 1)

    def test_exit_code_nonzero_when_a_store_errors(self):
        d = tempfile.mkdtemp()
        argv = ["--state-dir", d, "--term", "x", "--store", "turns", "--json"]
        buf = _CaptureStdout()
        with buf:
            code = rs.main(argv)
        self.assertEqual(code, 2)


class _CaptureStdout:
    def __enter__(self):
        import io
        self._real = sys.stdout
        sys.stdout = io.StringIO()
        return self

    def __exit__(self, *exc):
        self.value = sys.stdout.getvalue()
        sys.stdout = self._real


if __name__ == "__main__":
    unittest.main()
