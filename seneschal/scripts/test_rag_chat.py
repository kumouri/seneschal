#!/usr/bin/env python3
"""Tests for `source: "chat"` — `rag_index.py --chat`, the turns.jsonl reader.

**The load-bearing family is `Tombstone`.** The chat writer records a `!private` turn as a row with
`redacted: true` and no text; this indexer must not resurrect it, and must not reconstruct it from
the metadata either. A redaction that holds in the writer and not in the reader is not a redaction.

Everything else follows the house posture for `state/` readers: a corrupt line costs that line, a
missing file reads empty, and hostile shapes do not raise.

**No dependency on the `turns` module.** The indexer reads the FILE only (rows shaped
`{turn_id, speaker: owner|assistant, origin, text, at, redacted}`), so these fixtures are
hand-written rows and nothing here imports `turns`.

Stdlib unittest only, no Ollama, no network. Run:  python -m unittest seneschal.scripts.test_rag_chat
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import provenance_guard as pg  # noqa: E402
import rag_index as ri  # noqa: E402


def _write(d, rows):
    p = pathlib.Path(d) / ri.CHAT_TURNS_FILE
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _docs(d):
    # Explicit labels: the tests must not depend on whatever persona this machine has configured.
    return list(ri._iter_chat_turns(d, owner_label="Owner", assistant_label="Assistant"))


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name


class OneDocumentPerTurn(_TmpCase):
    def test_both_halves_of_a_turn_become_one_document(self):
        """Splitting them would index the assistant's answer with no idea what it answered."""
        _write(self.d, [
            {"turn_id": "t1", "speaker": "owner", "origin": "human", "text": "what's on today?",
             "at": "2026-08-04T09:53:00Z"},
            {"turn_id": "t1", "speaker": "assistant", "origin": "human", "text": "three things.",
             "at": "2026-08-04T09:53:30Z"},
        ])
        doc, = _docs(self.d)
        self.assertEqual(doc["source"], "chat")
        self.assertEqual(doc["ref"], "turn:t1")
        self.assertIn("Owner: what's on today?", doc["text"])
        self.assertIn("Assistant: three things.", doc["text"])

    def test_turn_order_is_preserved(self):
        _write(self.d, [{"turn_id": f"t{i}", "speaker": "owner", "text": f"msg {i}"}
                        for i in range(5)])
        self.assertEqual([d["ref"] for d in _docs(self.d)], [f"turn:t{i}" for i in range(5)])

    def test_valid_from_is_when_it_was_SAID(self):
        """So `--as-of` and the bitemporal layer place the turn in the conversation's time, not in
        whenever Dream happened to ingest it."""
        _write(self.d, [{"turn_id": "t1", "speaker": "owner", "text": "x",
                         "at": "2026-08-04T09:53:00Z"}])
        doc, = _docs(self.d)
        self.assertEqual(doc["valid_from"], "2026-08-04T09:53:00Z")

    def test_the_chat_source_passes_the_provenance_guard(self):
        """A chat document the guard refused would silently index nothing."""
        _write(self.d, [{"turn_id": "t1", "speaker": "owner", "text": "hello"}])
        doc, = _docs(self.d)
        self.assertTrue(pg.ProvenanceGuard().check(doc).allowed)


class Labels(_TmpCase):
    def test_configured_labels_are_used(self):
        _write(self.d, [{"turn_id": "t1", "speaker": "owner", "text": "hi"},
                        {"turn_id": "t1", "speaker": "assistant", "text": "hello"}])
        doc, = ri._iter_chat_turns(self.d, owner_label="Robin", assistant_label="Jeeves")
        self.assertEqual(doc["text"], "Robin: hi\nJeeves: hello")

    def test_without_identity_the_labels_are_generic(self):
        real = ri._ic
        ri._ic = None
        try:
            self.assertEqual(ri._chat_labels(), ("Owner", "Assistant"))
        finally:
            ri._ic = real

    def test_an_unknown_speaker_is_labelled_as_the_assistant_side(self):
        _write(self.d, [{"turn_id": "t1", "speaker": "unknown", "text": "x"}])
        doc, = _docs(self.d)
        self.assertEqual(doc["text"], "Assistant: x")


class Tombstone(_TmpCase):
    """`!private`. The indexer is a second reader that has to honour it."""

    def test_a_redacted_turn_is_skipped_entirely(self):
        _write(self.d, [
            {"turn_id": "t1", "speaker": "owner", "redacted": True, "at": "2026-08-04T10:00:00Z"},
            {"turn_id": "t1", "speaker": "assistant", "redacted": True},
            {"turn_id": "t2", "speaker": "owner", "text": "public", "at": "2026-08-04T10:05:00Z"},
        ])
        self.assertEqual([d["ref"] for d in _docs(self.d)], ["turn:t2"])

    def test_one_redacted_side_redacts_the_whole_turn(self):
        """A reader that trusted whichever row it saw first would leak the other half."""
        _write(self.d, [
            {"turn_id": "t1", "speaker": "owner", "text": "LEAKME", "at": "x"},
            {"turn_id": "t1", "speaker": "assistant", "redacted": True},
        ])
        self.assertNotIn("LEAKME", json.dumps(_docs(self.d)))

    def test_redaction_wins_regardless_of_row_order(self):
        _write(self.d, [
            {"turn_id": "t1", "speaker": "assistant", "redacted": True},
            {"turn_id": "t1", "speaker": "owner", "text": "LEAKME", "at": "x"},
        ])
        self.assertNotIn("LEAKME", json.dumps(_docs(self.d)))


class SystemRowsAreIncludedHonestly(_TmpCase):
    """Job pushes, reaction markers, attachment lines — dropping them leaves holes that make the
    surrounding turns harder to read, so they are indexed with an honest label."""

    def test_a_system_row_is_indexed_and_labelled(self):
        _write(self.d, [{"turn_id": "t1", "speaker": "owner", "origin": "system",
                         "text": "[job finished: transcribe]", "at": "x"}])
        doc, = _docs(self.d)
        self.assertIn("Owner (system): [job finished: transcribe]", doc["text"])

    def test_a_human_row_is_not_labelled_system(self):
        _write(self.d, [{"turn_id": "t1", "speaker": "owner", "origin": "human", "text": "hi"}])
        doc, = _docs(self.d)
        self.assertNotIn("(system)", doc["text"])


class Tolerance(_TmpCase):
    def test_a_missing_file_reads_empty(self):
        self.assertEqual(_docs(self.d), [])

    def test_a_corrupt_line_costs_one_row(self):
        p = _write(self.d, [{"turn_id": "t1", "speaker": "owner", "text": "a"}])
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("{ not json\n")
            fh.write(json.dumps({"turn_id": "t2", "speaker": "owner", "text": "b"}) + "\n")
        self.assertEqual([d["ref"] for d in _docs(self.d)], ["turn:t1", "turn:t2"])

    def test_hostile_shapes_do_not_raise(self):
        p = pathlib.Path(self.d) / ri.CHAT_TURNS_FILE
        p.write_text("\n".join(["[1,2,3]", '"a string"', "null", "42",
                                json.dumps({"no": "turn_id"}),
                                json.dumps({"turn_id": ["not", "a", "string"], "text": "x"}),
                                json.dumps({"turn_id": "t1", "text": None}),
                                json.dumps({"turn_id": "t1", "text": "   "}),
                                json.dumps({"turn_id": "t2", "speaker": "owner", "text": "ok"})]),
                     encoding="utf-8")
        self.assertEqual([d["ref"] for d in _docs(self.d)], ["turn:t2"])

    def test_a_turn_with_no_usable_text_yields_nothing(self):
        _write(self.d, [{"turn_id": "t1", "speaker": "owner", "text": ""}])
        self.assertEqual(_docs(self.d), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
