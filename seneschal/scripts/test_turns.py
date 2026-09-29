#!/usr/bin/env python3
"""Tests for chat-turn capture, phase 0 (`turns.py`).

What is covered here is `turns.py` itself — the record shape, the pure classifiers, the read/prune
helpers, `stats`, the CLI, the `awake_since` presence probe, and above all the fail-open contract:
`record_turn` must never raise, whatever it is handed and whatever the disk does.

Producer coverage — that `presence.deliver_reply` and the drainer actually write both sides of each
turn, and that a `!private` turn writes a tombstone on BOTH sides — belongs with the presence wiring
and lands with the wave that wires it in.

Run:  python -m unittest test_turns   (from seneschal/scripts)
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import turns  # noqa: E402

NOW = datetime(2026, 8, 4, 14, 55, 26, tzinfo=timezone.utc)


def _rows(state_dir):
    return turns.read_turns(state_dir)


# --------------------------------------------------------------------------- 1. the writer


class RecordShape(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_writes_the_documented_record(self):
        self.assertTrue(turns.record_turn(self.dir, surface="telegram", speaker="owner",
                                          text="They're a real person, my second team lead.",
                                          turn_id="9d41c0a7be12", session_id="sess-1", now=NOW))
        rows = _rows(self.dir)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["schema"], "seneschal.turn/1")
        self.assertEqual(row["at"], "2026-08-04T14:55:26Z")
        self.assertEqual(row["surface"], "telegram")
        self.assertEqual(row["speaker"], "owner")
        self.assertEqual(row["origin"], "human")
        self.assertEqual(row["text"], "They're a real person, my second team lead.")
        self.assertEqual(row["turn_id"], "9d41c0a7be12")
        self.assertEqual(row["session_id"], "sess-1")
        self.assertIsNone(row["reply_to"])
        self.assertEqual(row["attachments"], [])
        self.assertFalse(row["redacted"])

    def test_id_is_a_sortable_stamp_plus_a_collision_suffix(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="a", now=NOW)
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="b", now=NOW)
        ids = [r["id"] for r in _rows(self.dir)]
        self.assertTrue(all(i.startswith("20260804-145526-") for i in ids))
        self.assertNotEqual(ids[0], ids[1])  # same second, still distinct

    def test_text_is_verbatim_and_uncapped(self):
        """The whole point: the cockpit ring buffer clips the owner at 200 characters with no marker,
        and a capture layer that inherited that budget would reproduce the bug it exists to fix."""
        long_text = "x" * 9000
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text=long_text)
        stored = _rows(self.dir)[0]["text"]
        self.assertEqual(len(stored), 9000)
        self.assertEqual(stored, long_text)

    def test_both_speakers_land_in_one_file(self):
        """A conversation, not two logs a reader has to zip."""
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="WHY IS THIS BROKEN",
                          turn_id="t1")
        turns.record_turn(self.dir, surface="telegram", speaker="assistant", text="Nothing on file.",
                          turn_id="t1")
        rows = _rows(self.dir)
        self.assertEqual([r["speaker"] for r in rows], ["owner", "assistant"])
        self.assertEqual({r["turn_id"] for r in rows}, {"t1"})

    def test_origin_defaults_to_the_classifier(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner",
                          text='[job finished: "transcribe: x" — status failed, exit 127]')
        self.assertEqual(_rows(self.dir)[0]["origin"], "system")

    def test_an_explicit_origin_wins(self):
        turns.record_turn(self.dir, surface="telegram", speaker="assistant", text="all done",
                          origin="system")
        self.assertEqual(_rows(self.dir)[0]["origin"], "system")

    def test_append_only(self):
        for i in range(5):
            turns.record_turn(self.dir, surface="discord", speaker="owner", text=f"n{i}")
        self.assertEqual([r["text"] for r in _rows(self.dir)], ["n0", "n1", "n2", "n3", "n4"])

    def test_unknown_surface_is_recorded_not_rejected(self):
        """A turn that happened must never be lost to this module's opinion about labels."""
        self.assertTrue(turns.record_turn(self.dir, surface="carrier-pigeon", speaker="owner",
                                          text="hi"))
        self.assertEqual(_rows(self.dir)[0]["surface"], "carrier-pigeon")


class Redaction(unittest.TestCase):
    """`!private` and (later) retro-redact both write the SAME tombstone: the fact survives, the
    content never lands, and nothing lies by omission."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_a_redacted_row_has_no_text_key_at_all(self):
        self.assertTrue(turns.record_turn(self.dir, surface="telegram", speaker="owner",
                                          text="something heavy", redacted=True, now=NOW))
        row = _rows(self.dir)[0]
        self.assertNotIn("text", row)
        self.assertTrue(row["redacted"])
        # The fact of the turn still survives, with everything a transcript needs except the words.
        self.assertEqual(row["at"], "2026-08-04T14:55:26Z")
        self.assertEqual(row["speaker"], "owner")

    def test_the_text_is_nowhere_in_the_serialised_line(self):
        """Not merely absent from the parsed dict — absent from the bytes on disk."""
        turns.record_turn(self.dir, surface="telegram", speaker="owner",
                          text="the unprintable thing", redacted=True)
        with open(turns.turns_path(self.dir), encoding="utf-8") as fh:
            self.assertNotIn("unprintable", fh.read())

    def test_attachment_descriptors_are_dropped_too(self):
        """A filename and a caption are content. Redacting the text but keeping
        `photo "grievance-letter.png"` would leak exactly what the control exists to withhold."""
        turns.record_turn(self.dir, surface="telegram", speaker="owner",
                          text='[attachment: photo "statement.png" saved to state/inbox/x.png]',
                          redacted=True)
        row = _rows(self.dir)[0]
        self.assertEqual(row["attachments"], [])
        with open(turns.turns_path(self.dir), encoding="utf-8") as fh:
            self.assertNotIn("statement", fh.read())


class Classifiers(unittest.TestCase):
    """Pure, and worth pinning: `origin` is what stops a job log ranking above something the owner
    actually said once a later phase indexes this corpus."""

    def test_the_three_measured_system_shapes(self):
        for text in ('[job finished: "transcribe: …" — status failed, exit 127. Log: …]',
                     '[the owner reacted 👍 (= ack) to: "stretch"]',
                     '[attachment: document "export.zip" saved to state/inbox/export.zip]'):
            self.assertEqual(turns.classify_origin(text), "system", text)

    def test_the_owners_words_are_human_even_when_shouting(self):
        for text in ("WHY IS THIS BROKEN", "done with the report", "", "  hello  "):
            self.assertEqual(turns.classify_origin(text), "human", text)

    def test_a_bracket_mid_sentence_is_not_a_system_row(self):
        self.assertEqual(turns.classify_origin("the docs say [sic] it works"), "human")

    def test_a_message_that_OPENS_with_an_unrecognised_bracket_is_the_owners(self):
        """The load-bearing asymmetry, and the one this suite was missing.

        `_SYSTEM_MARKER_RE` is an allow-list, so these are correct — and pinning it is what stops a
        later "simplification" to `^\\[` from being waved through. A system row mislabelled `human`
        only adds noise; a HUMAN row mislabelled `system` drops the owner's own words out of the
        default index, and the owner's raw words must be evicted last, never first."""
        for text in (
            "[sic] that's what they actually said",
            "[note to self] remember the invoice",
            "[1] first thing [2] second thing",
            "[job posting] this one looks decent — https://example.com",
            "[attachment]",              # near-miss: no colon
            "[the owner reacted]",       # near-miss: no trailing space
            "[jobs] are the worst",      # near-miss: `job ` needs the space, `jobs` is not it
        ):
            self.assertEqual(turns.classify_origin(text), "human", text)

    def test_non_string_input_is_human_rather_than_an_exception(self):
        self.assertEqual(turns.classify_origin(None), "human")

    def test_attachment_parsing_records_the_path_never_the_bytes(self):
        text = ('[attachment: document "notes.pdf" saved to C:\\state\\inbox\\notes.pdf] '
                'look at this')
        self.assertEqual(turns.parse_attachments(text),
                         [{"kind": "document", "name": "notes.pdf",
                           "path": "C:\\state\\inbox\\notes.pdf"}])

    def test_an_undownloaded_attachment_still_gets_a_row_with_no_path(self):
        text = "[attachment: video (34.2 MB) NOT downloaded — over Telegram's ~20 MB bot-API limit]"
        att = turns.parse_attachments(text)
        self.assertEqual(len(att), 1)
        self.assertEqual(att[0]["kind"], "video")
        self.assertIsNone(att[0]["path"])

    def test_plain_text_has_no_attachments(self):
        self.assertEqual(turns.parse_attachments("just talking"), [])
        self.assertEqual(turns.parse_attachments(None), [])


class NeverRaises(unittest.TestCase):
    """Invariant 1: recording never costs the turn."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_unwritable_state_dir_returns_false(self):
        blocked = os.path.join(self.dir, "afile")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        self.assertFalse(turns.record_turn(blocked, surface="telegram", speaker="owner", text="x"))

    def test_open_failure_returns_false_and_does_not_raise(self):
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(turns.record_turn(self.dir, surface="telegram", speaker="owner",
                                               text="x"))

    def test_unserialisable_text_still_never_raises(self):
        self.assertTrue(turns.record_turn(self.dir, surface="telegram", speaker="owner",
                                          text=object()))
        self.assertIn("object object", _rows(self.dir)[0]["text"])

    def test_unserialisable_attachments_cost_the_row_not_an_exception(self):
        self.assertFalse(turns.record_turn(self.dir, surface="telegram", speaker="owner", text="x",
                                           attachments=[{"fh": object()}]))
        self.assertEqual(_rows(self.dir), [])

    def test_an_exception_anywhere_returns_false_rather_than_raising(self):
        with mock.patch.object(turns, "_row_id", side_effect=RuntimeError("boom")):
            self.assertFalse(turns.record_turn(self.dir, surface="telegram", speaker="owner",
                                               text="x"))
        self.assertEqual(_rows(self.dir), [])

    def test_a_broken_attachment_parser_costs_the_descriptor_not_the_row(self):
        with mock.patch.object(turns, "_ATTACHMENT_RE") as re_mock:
            re_mock.finditer.side_effect = RuntimeError("boom")
            self.assertTrue(turns.record_turn(self.dir, surface="telegram", speaker="owner",
                                              text="[attachment: photo saved to x]"))
        self.assertEqual(_rows(self.dir)[0]["attachments"], [])


class Reading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_absent_file_reads_empty(self):
        self.assertEqual(turns.read_turns(self.dir), [])

    def test_malformed_lines_are_skipped_not_fatal(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="good")
        with open(turns.turns_path(self.dir), "a", encoding="utf-8") as fh:
            fh.write("{not json\n\n[1,2,3]\n")
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="also good")
        self.assertEqual([r["text"] for r in _rows(self.dir)], ["good", "also good"])

    def test_limit_keeps_the_newest(self):
        for i in range(6):
            turns.record_turn(self.dir, surface="telegram", speaker="owner", text=f"n{i}")
        self.assertEqual([r["text"] for r in turns.read_turns(self.dir, limit=2)], ["n4", "n5"])
        self.assertEqual(turns.read_turns(self.dir, limit=0), [])

    def test_since_filters_by_stamp(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="old",
                          now=NOW - timedelta(days=400))
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="new", now=NOW)
        rows = turns.read_turns(self.dir, since=NOW - timedelta(hours=1))
        self.assertEqual([r["text"] for r in rows], ["new"])


class Prune(unittest.TestCase):
    """Retention is INDEFINITE. These tests exist to pin that the default is a no-op,
    not to bless an age-based sweep — nothing in Dream or anywhere else calls this."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_the_default_keeps_everything_forever(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="ancient",
                          now=NOW - timedelta(days=3000))
        self.assertEqual(turns.RETENTION_DAYS, 0)
        self.assertEqual(turns.prune(self.dir, now=NOW), 0)
        self.assertEqual(len(_rows(self.dir)), 1)

    def test_an_explicit_days_drops_only_expired_rows(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="ancient",
                          now=NOW - timedelta(days=500))
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="recent",
                          now=NOW - timedelta(days=2))
        self.assertEqual(turns.prune(self.dir, days=400, now=NOW), 1)
        self.assertEqual([r["text"] for r in _rows(self.dir)], ["recent"])

    def test_tombstones_are_never_evicted(self):
        """A tombstone is bytes-cheap and is the thing that stops the record lying by omission."""
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="private",
                          redacted=True, now=NOW - timedelta(days=3000))
        self.assertEqual(turns.prune(self.dir, days=400, now=NOW), 0)
        self.assertEqual(len(_rows(self.dir)), 1)

    def test_undated_and_garbled_rows_are_kept(self):
        path = turns.turns_path(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema": turns.SCHEMA, "text": "no stamp"}) + "\n")
            fh.write('{"at": "not-a-date", "text": "bad stamp"}\n')
            fh.write("{garbage\n")
        self.assertEqual(turns.prune(self.dir, days=1, now=NOW), 0)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 3)

    def test_absent_file_is_a_no_op(self):
        self.assertEqual(turns.prune(self.dir, days=400, now=NOW), 0)


class Stats(unittest.TestCase):
    """The measurement phase 0 exists to produce: index sizing and a size threshold both need the
    owner's real volume, which no truncating record can supply; this is what replaces an estimate
    with a fact."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_empty_log_reads_as_zero_not_an_error(self):
        s = turns.stats(self.dir)
        self.assertEqual(s["rows"], 0)
        self.assertEqual(s["bytes"], 0)
        self.assertIsNone(s["days_covered"])
        self.assertIsNone(s["bytes_per_day"])

    def test_counts_split_by_surface_speaker_and_origin(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="hi", now=NOW)
        turns.record_turn(self.dir, surface="telegram", speaker="assistant", text="hello", now=NOW)
        turns.record_turn(self.dir, surface="discord", speaker="owner", now=NOW,
                          text='[job finished: "x" — status done. Log: y]')
        s = turns.stats(self.dir)
        self.assertEqual(s["rows"], 3)
        self.assertGreater(s["bytes"], 0)
        self.assertEqual(s["by_surface"], {"telegram": 2, "discord": 1})
        self.assertEqual(s["by_speaker"], {"owner": 2, "assistant": 1})
        self.assertEqual(s["by_origin"], {"human": 2, "system": 1})

    def test_text_bytes_measures_the_words_not_the_envelope(self):
        """`bytes` sizes the file; `text_bytes` sizes what an index would actually embed."""
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="x" * 500, now=NOW)
        s = turns.stats(self.dir)
        self.assertEqual(s["text_bytes"], 500)
        self.assertGreater(s["bytes"], s["text_bytes"])

    def test_a_redacted_tombstone_is_counted_and_contributes_no_text(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="secret",
                          redacted=True, now=NOW)
        s = turns.stats(self.dir)
        self.assertEqual(s["redacted"], 1)
        self.assertEqual(s["text_bytes"], 0)

    def test_rates_ABSTAIN_on_a_single_day(self):
        """One day is not a rate. Extrapolating from it would set the threshold on fiction again —
        which is the error this whole phase exists to end."""
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="hi", now=NOW)
        s = turns.stats(self.dir)
        self.assertIsNone(s["days_covered"])
        self.assertIsNone(s["bytes_per_day"])
        self.assertIsNone(s["rows_per_day"])

    def test_rates_appear_once_two_days_exist(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="day one", now=NOW)
        later = NOW + timedelta(days=2)
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="day three", now=later)
        s = turns.stats(self.dir)
        self.assertEqual(s["days_covered"], 2)
        self.assertIsNotNone(s["bytes_per_day"])
        self.assertEqual(s["first_at"], "2026-08-04T14:55:26Z")
        self.assertEqual(s["last_at"], "2026-08-06T14:55:26Z")

    def test_an_unreadable_log_does_not_raise(self):
        os.makedirs(turns.turns_path(self.dir), exist_ok=True)  # squat the path
        self.assertEqual(turns.stats(self.dir)["rows"], 0)

    def test_text_bytes_by_speaker_splits_the_words_not_the_envelope(self):
        """Real per-speaker text growth, as opposed to `by_speaker`'s row COUNT."""
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="x" * 500, now=NOW)
        turns.record_turn(self.dir, surface="telegram", speaker="assistant", text="y" * 100, now=NOW)
        s = turns.stats(self.dir)
        self.assertEqual(s["text_bytes_by_speaker"], {"owner": 500, "assistant": 100})

    def test_a_redacted_row_contributes_no_text_bytes_by_speaker(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="secret",
                          redacted=True, now=NOW)
        s = turns.stats(self.dir)
        self.assertEqual(s["text_bytes_by_speaker"], {})

    def test_text_bytes_per_day_abstains_on_a_single_day(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="hi", now=NOW)
        self.assertIsNone(turns.stats(self.dir)["text_bytes_per_day"])

    def test_text_bytes_per_day_appears_once_two_days_exist(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="x" * 100, now=NOW)
        later = NOW + timedelta(days=2)
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="x" * 300, now=later)
        s = turns.stats(self.dir)
        self.assertEqual(s["days_covered"], 2)
        self.assertEqual(s["text_bytes_per_day"], 200.0)  # 400 text bytes / 2 days


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_record_appends(self):
        rc = turns.main(["--state-dir", self.dir, "record", "--surface", "telegram",
                         "--speaker", "owner", "--text", "hello", "--turn-id", "t9"])
        self.assertEqual(rc, 0)
        row = _rows(self.dir)[0]
        self.assertEqual(row["text"], "hello")
        self.assertEqual(row["turn_id"], "t9")

    def test_record_redacted(self):
        turns.main(["--state-dir", self.dir, "record", "--surface", "telegram", "--speaker", "owner",
                    "--text", "secret", "--redacted"])
        row = _rows(self.dir)[0]
        self.assertTrue(row["redacted"])
        self.assertNotIn("text", row)

    def test_prune_subcommand_defaults_to_keeping_everything(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="old",
                          now=datetime.now(timezone.utc) - timedelta(days=3000))
        self.assertEqual(turns.main(["--state-dir", self.dir, "prune"]), 0)
        self.assertEqual(len(_rows(self.dir)), 1)

    def test_stats_subcommand_prints_the_volume(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="hi", now=NOW)
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            rc = turns.main(["--state-dir", self.dir, "stats"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["rows"], 1)

    def _two_dated_rows(self):
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="before",
                          now=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))
        turns.record_turn(self.dir, surface="telegram", speaker="owner", text="after",
                          now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc))

    def _tail(self, argv):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            rc = turns.main(["--state-dir", self.dir, "tail"] + argv)
        return rc, [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]

    def test_tail_since_takes_a_bare_date_as_utc_midnight(self):
        self._two_dated_rows()
        rc, rows = self._tail(["--since", "2026-08-12", "--limit", "-1"])
        self.assertEqual(rc, 0)
        self.assertEqual([r["text"] for r in rows], ["after"])

    def test_a_negative_limit_is_the_whole_window(self):
        """The digest reads a session window, not a tail — `-1` is how it says so (turns.py USAGE)."""
        self._two_dated_rows()
        _, rows = self._tail(["--since", "2026-08-01", "--limit", "-1"])
        self.assertEqual([r["text"] for r in rows], ["before", "after"])

    def test_an_unparseable_since_REFUSES_rather_than_widening_to_everything(self):
        """The one loud path in a tolerant module: a silently-ignored window is a digest that
        reaches back over months (see the `tail` branch of `main`)."""
        self._two_dated_rows()
        rc, rows = self._tail(["--since", "last tuesday", "--limit", "-1"])
        self.assertEqual(rc, 2)
        self.assertEqual(rows[0]["ok"], False)
        self.assertNotIn("before", json.dumps(rows))

    def test_tail_without_since_is_byte_identical_to_before(self):
        self._two_dated_rows()
        _, rows = self._tail(["--limit", "20"])
        self.assertEqual([r["text"] for r in rows], ["before", "after"])


class AwakeSinceTest(unittest.TestCase):
    """`awake_since` — the evidence a quiet-hours gate uses to stand down when the owner is awake.
    Every doubt must answer `None`, because `None` keeps the quiet window and a wrong non-`None`
    pages the owner at 3 AM."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def fresh_dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def say(self, text, minutes_ago, speaker="owner", state_dir=None, **kw):
        turns.record_turn(state_dir or self.dir, surface="telegram", speaker=speaker, text=text,
                          now=NOW - timedelta(minutes=minutes_ago), **kw)

    def test_the_owners_recent_message_is_evidence(self):
        self.say("still up, can't sleep", 5)
        self.assertEqual(turns.awake_since(self.dir, now=NOW), NOW - timedelta(minutes=5))

    def test_the_newest_qualifying_row_wins(self):
        self.say("first", 20)
        self.say("second", 3)
        self.assertEqual(turns.awake_since(self.dir, now=NOW), NOW - timedelta(minutes=3))

    def test_a_picker_tap_a_reaction_and_an_attachment_all_count(self):
        for text in ('[the owner answered "merge this?" -> Approve]',
                     "[the owner reacted 👍 (= yes) to: want me to send it?]",
                     "[attachment: photo saved to C:\\x.jpg]"):
            with self.subTest(text=text):
                d = self.fresh_dir()
                self.say(text, 1, state_dir=d)
                self.assertIsNotNone(turns.awake_since(d, now=NOW))

    def test_a_job_finished_notice_on_the_owners_channel_does_not_count(self):
        """The overnight case this must never trip on: jobs finish at 3 AM while the owner sleeps,
        and their notices are written with `speaker: owner` because they arrive on that channel."""
        self.say('[job finished: "fix(tests): x" — done in 4m]', 1)
        self.say("[force-fable] something", 1)
        self.assertIsNone(turns.awake_since(self.dir, now=NOW))

    def test_the_assistants_own_lines_do_not_count(self):
        self.say("Good morning!", 1, speaker="assistant")
        self.assertIsNone(turns.awake_since(self.dir, now=NOW))

    def test_a_stale_message_is_not_evidence(self):
        self.say("goodnight", 31)
        self.assertIsNone(turns.awake_since(self.dir, now=NOW))

    def test_a_row_stamped_in_the_future_is_not_evidence(self):
        self.say("from a skewed clock", -10)
        self.assertIsNone(turns.awake_since(self.dir, now=NOW))

    def test_a_redacted_private_message_still_counts_but_a_redacted_system_row_does_not(self):
        self.say("!private x", 2, redacted=True, origin="human")
        self.assertIsNotNone(turns.awake_since(self.dir, now=NOW))
        d = self.fresh_dir()
        self.say("[the owner reacted 👍 x]", 2, state_dir=d, redacted=True)
        self.assertIsNone(turns.awake_since(d, now=NOW))

    def test_a_missing_file_is_no_evidence(self):
        self.assertIsNone(turns.awake_since(self.dir, now=NOW))

    def test_an_unreadable_file_is_no_evidence(self):
        self.say("hi", 1)
        with mock.patch("builtins.open", side_effect=PermissionError("locked")):
            self.assertIsNone(turns.awake_since(self.dir, now=NOW))

    def test_a_path_that_is_a_directory_is_no_evidence(self):
        os.makedirs(turns.turns_path(self.dir))
        self.assertIsNone(turns.awake_since(self.dir, now=NOW))

    def test_garbage_lines_are_skipped_not_fatal(self):
        with open(turns.turns_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write('not json\n{"speaker": "owner", "origin": "human", "at": 7}\n')
        self.say("real", 2)
        self.assertEqual(turns.awake_since(self.dir, now=NOW), NOW - timedelta(minutes=2))

    def test_only_the_tail_is_read_and_its_partial_first_line_is_dropped(self):
        """A large file is not read whole on every overnight pass. A row cut in half by the tail
        boundary must not be misparsed, and a row wholly before it is simply not seen."""
        self.say("old but inside the window", 10)
        for i in range(50):
            self.say(f"filler {i}", 40, speaker="assistant")
        size = os.path.getsize(turns.turns_path(self.dir))
        self.assertIsNone(turns.awake_since(self.dir, now=NOW, tail_bytes=size // 2))
        self.assertIsNotNone(turns.awake_since(self.dir, now=NOW, tail_bytes=size))


if __name__ == "__main__":
    unittest.main()
