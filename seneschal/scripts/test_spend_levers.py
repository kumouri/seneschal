#!/usr/bin/env python3
"""Tests for spend_levers.py — the phase-1 lever counter and the ledger↔metrics join reader.

Stdlib unittest only, no network, no real `claude` process: the accumulator is pure and eats plain
dicts, which is the same property that keeps cockpit_pipe.build_chat_event_from_stream testable.

**The event fixtures here are the shapes read off live CLI transcripts**, not shapes assumed from the
docs — including the two that matter: a `tool_result` whose `content` is a bare string (the common
case) and one whose `content` is a block list. A counter written against only the second shape reads
every ordinary file read as 0 bytes and looks perfectly healthy doing it.

Run:  python -m unittest test_spend_levers   (or)   python test_spend_levers.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import governor as gv  # noqa: E402
import spend_levers as sl  # noqa: E402
import tz_common  # noqa: E402

# A fixed owner zone (UTC-5) for every date-window case, so the runner's own zone cannot move a day.
LOCAL = timezone(timedelta(hours=-5))


def _assistant_tool_use(name: str = "Read"):
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_1", "name": name,
                                     "input": {"file_path": "x.md"}}]}}


def _tool_result_str(text: str):
    """The common shape: `content` is a bare string."""
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                     "content": text}]}}


def _tool_result_blocks(parts):
    """The mixed shape: `content` is a block list (text and/or image)."""
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                     "content": parts}]}}


class Accumulator(unittest.TestCase):
    def test_counts_tool_use_blocks(self):
        acc = sl.TurnLevers()
        acc.observe(_assistant_tool_use("Read"))
        acc.observe(_assistant_tool_use("Bash"))
        self.assertEqual(acc.snapshot()["tool_calls"], 2)

    def test_counts_string_shaped_tool_result_bytes(self):
        acc = sl.TurnLevers()
        acc.observe(_tool_result_str("hello"))
        self.assertEqual(acc.snapshot()["tool_result_bytes"], 5)

    def test_bytes_are_UTF8_bytes_not_characters(self):
        """The field is named bytes and regressed against as bytes. `len()` on a str would undercount
        every non-ASCII result — and real transcripts are full of em-dashes and emoji."""
        acc = sl.TurnLevers()
        acc.observe(_tool_result_str("—"))  # U+2014, three UTF-8 bytes, one character
        self.assertEqual(acc.snapshot()["tool_result_bytes"], 3)

    def test_counts_block_shaped_tool_result_text(self):
        acc = sl.TurnLevers()
        acc.observe(_tool_result_blocks([{"type": "text", "text": "abcd"},
                                         {"type": "text", "text": "ef"}]))
        self.assertEqual(acc.snapshot()["tool_result_bytes"], 6)

    def test_counts_images_and_does_NOT_weigh_them_as_bytes(self):
        """An image's base64 payload has no stable relationship to what it costs in the window (that
        is a function of its dimensions), so summing b64 characters into the byte total would make
        the one field anyone regresses against quietly wrong. Counted, never weighed."""
        acc = sl.TurnLevers()
        acc.observe(_tool_result_blocks([
            {"type": "text", "text": "ok"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "A" * 100_000}},
        ]))
        snap = acc.snapshot()
        self.assertEqual(snap["tool_result_images"], 1)
        self.assertEqual(snap["tool_result_bytes"], 2)

    def test_an_error_result_still_counts(self):
        """A failed tool result enters the window exactly like a successful one — and an error that
        says 'file exceeds 256KB' is precisely the habit this lever exists to surface."""
        ev = _tool_result_str("File content (524.5KB) exceeds maximum allowed size (256KB).")
        ev["message"]["content"][0]["is_error"] = True
        acc = sl.TurnLevers()
        acc.observe(ev)
        self.assertGreater(acc.snapshot()["tool_result_bytes"], 0)

    def test_assistant_text_and_thinking_blocks_are_not_counted(self):
        acc = sl.TurnLevers()
        acc.observe({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "a reply"}, {"type": "thinking", "thinking": "hmm"}]}})
        snap = acc.snapshot()
        self.assertEqual((snap["tool_calls"], snap["tool_result_bytes"]), (0, 0))

    def test_snapshot_is_None_before_anything_is_observed(self):
        """R4, applied to the whole block: an unfed counter reports NOTHING, so append_spend omits the
        key rather than writing zeros. A `0` that IS written means observed-and-genuinely-zero."""
        self.assertIsNone(sl.TurnLevers().snapshot())

    def test_a_turn_with_no_tools_reports_measured_zeros(self):
        acc = sl.TurnLevers()
        acc.observe({"type": "system", "subtype": "init"})
        self.assertEqual(acc.snapshot(),
                         {"tool_calls": 0, "tool_result_bytes": 0, "tool_result_images": 0})

    def test_flush_resets_so_a_second_meter_does_not_re_report_the_first(self):
        """WarmSession.send's first-turn fallback ladder re-sends after a failed resume, and each
        attempt ends in its own `result` event — so each writes its own ledger row."""
        acc = sl.TurnLevers()
        acc.observe(_assistant_tool_use())
        acc.observe(_tool_result_str("x" * 10))
        first = acc.flush()
        self.assertEqual((first["tool_calls"], first["tool_result_bytes"]), (1, 10))
        acc.observe({"type": "system"})
        second = acc.flush()
        self.assertEqual((second["tool_calls"], second["tool_result_bytes"]), (0, 0))

    def test_never_raises_on_malformed_events(self):
        acc = sl.TurnLevers()
        for bad in (None, "not a dict", 42, [],
                    {"type": "user", "message": "not a dict"},
                    {"type": "user", "message": {"content": "not a list"}},
                    {"type": "assistant", "message": {"content": ["not a dict"]}},
                    {"type": "user", "message": {"content": [{"type": "tool_result",
                                                              "content": {"unexpected": "dict"}}]}},
                    {"type": "user", "message": {"content": [{"type": "tool_result",
                                                              "content": [None, 7]}]}}):
            acc.observe(bad)  # must not raise
        self.assertIsNotNone(acc.snapshot())

    def test_a_realistic_turn(self):
        acc = sl.TurnLevers()
        for ev in (_assistant_tool_use("Read"), _tool_result_str("a" * 4096),
                   _assistant_tool_use("Grep"), _tool_result_str("b" * 512),
                   {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
                   {"type": "result", "is_error": False, "usage": {"input_tokens": 3}}):
            acc.observe(ev)
        self.assertEqual(acc.snapshot(),
                         {"tool_calls": 2, "tool_result_bytes": 4608, "tool_result_images": 0})


class LedgerRow(unittest.TestCase):
    """The record half — governor.append_spend's `levers=` keyword (spec §5.1, R4, R8)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_levers_are_written_when_measured(self):
        gv.append_spend(self.dir, "tokens", model="m", tokens=10, turn_id="t1",
                        levers={"tool_calls": 2, "tool_result_bytes": 4096, "tool_result_images": 0})
        row = gv._read_ledger(self.dir)[0]
        self.assertEqual(row["levers"]["tool_calls"], 2)
        self.assertEqual(row["levers"]["tool_result_bytes"], 4096)

    def test_a_measured_zero_IS_written(self):
        """R4 forbids a 0 for an UNmeasured lever. A turn that genuinely made no tool calls is
        measured, and its 0 is the finding — dropping it would make 'no tools' indistinguishable from
        'not instrumented'."""
        gv.append_spend(self.dir, "tokens", model="m", tokens=10, turn_id="t1",
                        levers={"tool_calls": 0, "tool_result_bytes": 0, "tool_result_images": 0})
        self.assertEqual(gv._read_ledger(self.dir)[0]["levers"]["tool_calls"], 0)

    def test_levers_are_OMITTED_not_zeroed_when_unmeasured(self):
        for empty in (None, {}):
            d = tempfile.mkdtemp()
            gv.append_spend(d, "tokens", model="m", tokens=10, levers=empty)
            self.assertNotIn("levers", gv._read_ledger(d)[0])

    def test_a_row_without_levers_is_byte_identical_to_the_pre_phase_1_row(self):
        """R8 — `tokens` and `components` keep their meanings forever, and the 07-30 metering fix
        earned its trust by not reinterpreting history. Levers are purely additive: same assertion
        phase 0 made for `turn_id`, re-made one field later."""
        usage = {"input_tokens": 6, "output_tokens": 3267, "cache_read_input_tokens": 499309}
        gv.append_spend(self.dir, "tokens", model="m", usage=usage, turn_id="t1")
        gv.append_spend(self.dir, "tokens", model="m", usage=usage, turn_id="t1",
                        levers={"tool_calls": 1, "tool_result_bytes": 9, "tool_result_images": 0})
        without, with_levers = gv._read_ledger(self.dir)
        self.assertEqual({k: v for k, v in with_levers.items() if k not in ("ts", "levers")},
                         {k: v for k, v in without.items() if k != "ts"})

    def test_levers_change_no_rollup_and_no_rail(self):
        """R1 — record, do not enforce. Oikonomos owns enforcement; this spec owns diagnosis, and the
        two must not grow into each other. A levered row must roll up to exactly what it would have
        without the block."""
        bare = tempfile.mkdtemp()
        usage = {"input_tokens": 100, "output_tokens": 50}
        gv.append_spend(bare, "tokens", model="m", usage=usage)
        gv.append_spend(self.dir, "tokens", model="m", usage=usage,
                        levers={"tool_calls": 9, "tool_result_bytes": 999_999, "tool_result_images": 4})
        self.assertEqual(gv.rollups(self.dir), gv.rollups(bare))


class JoinReader(unittest.TestCase):
    """The read half — the join phase 0's key made possible and which nothing had actually run."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.now = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc)  # 13:00 local, safely mid-day
        patcher = mock.patch.object(tz_common, "_zone", return_value=LOCAL)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ledger(self, rows):
        with open(gv.ledger_path(self.dir), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def _metrics(self, rows):
        with open(os.path.join(self.dir, sl.METRICS_FILE), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def _ts(self, **delta):
        return (self.now - timedelta(**delta)).isoformat().replace("+00:00", "Z")

    def test_joins_a_ledger_row_to_its_metrics_row(self):
        self._ledger([{"ts": self._ts(hours=1), "kind": "tokens", "model": "claude-opus-5",
                       "billable_tokens": 59990, "turn_id": "abc",
                       "levers": {"tool_calls": 4, "tool_result_bytes": 38112,
                                  "tool_result_images": 0}}])
        self._metrics([{"ts": self._ts(hours=1), "turn_id": "abc", "turns_served": 13,
                        "source": "telegram"}])
        joined = sl.join_turns(self.dir, days=1, now=self.now)
        self.assertEqual(len(joined["turns"]), 1)
        turn = joined["turns"][0]
        self.assertEqual(turn["metrics"]["turns_served"], 13)
        self.assertEqual(turn["levers"]["tool_result_bytes"], 38112)

    def test_a_row_with_no_turn_to_name_is_reported_SEPARATELY_not_dropped(self):
        """A Fable delegation and a job child are not warm turns and carry no turn_id by design. A
        report that silently dropped them would understate the day it exists to explain."""
        self._ledger([
            {"ts": self._ts(hours=1), "kind": "tokens", "billable_tokens": 100, "turn_id": "abc"},
            {"ts": self._ts(hours=2), "kind": "fable_oneshot", "billable_tokens": 7000},
        ])
        joined = sl.join_turns(self.dir, days=1, now=self.now)
        self.assertEqual(len(joined["turns"]), 1)
        self.assertEqual(len(joined["unattributed"]), 1)
        self.assertEqual(joined["unattributed"][0]["billable"], 7000)

    def test_the_window_is_an_OWNER_LOCAL_day_not_a_UTC_one(self):
        """The house rule: date logic is the owner's local calendar, never UTC — a UTC 'today'
        compared against a local boundary fires on the wrong side of it. 04:00 UTC on the 10th is
        still the 9th at UTC-5, so a --days 1 report must contain it."""
        after_midnight_utc = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)  # 23:00 local on the 9th
        self._ledger([{"ts": after_midnight_utc.isoformat().replace("+00:00", "Z"),
                       "kind": "tokens", "billable_tokens": 5, "turn_id": "late"}])
        joined = sl.join_turns(self.dir, days=1, now=after_midnight_utc)
        self.assertEqual(joined["day"], "2026-08-09")
        self.assertEqual(len(joined["turns"]), 1)

    def test_rows_outside_the_window_are_excluded(self):
        self._ledger([{"ts": self._ts(days=5), "kind": "tokens", "billable_tokens": 5,
                       "turn_id": "old"},
                      {"ts": self._ts(hours=2), "kind": "tokens", "billable_tokens": 6,
                       "turn_id": "new"}])
        joined = sl.join_turns(self.dir, days=1, now=self.now)
        self.assertEqual([t["turn_id"] for t in joined["turns"]], ["new"])

    def test_falls_back_to_raw_tokens_on_a_legacy_row(self):
        """Pre-2026-07-30 rows carry no billable_tokens. They are still real spend and must appear."""
        self._ledger([{"ts": self._ts(hours=1), "kind": "tokens", "tokens": 1234, "turn_id": "legacy"}])
        joined = sl.join_turns(self.dir, days=1, now=self.now)
        self.assertEqual(joined["turns"][0]["billable"], 1234)

    def test_missing_files_report_an_empty_day_rather_than_raising(self):
        joined = sl.join_turns(tempfile.mkdtemp(), days=1, now=self.now)
        self.assertEqual(joined["turns"], [])
        self.assertIn("0 metered turn", sl.render_report(joined))

    def test_metrics_read_skips_a_corrupt_line(self):
        with open(os.path.join(self.dir, sl.METRICS_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"turn_id": "a"}\n{ not json\n\n{"turn_id": "b"}\n')
        self.assertEqual([r["turn_id"] for r in sl.read_metrics(self.dir)], ["a", "b"])

    def test_report_renders_pre_phase_1_rows_without_claiming_they_were_measured(self):
        """History carries no levers and never will (spec Q6 — the join key could not be backfilled,
        and neither can these). The report must say so rather than printing zeros."""
        self._ledger([{"ts": self._ts(hours=1), "kind": "tokens", "billable_tokens": 500,
                       "turn_id": "old-shape"}])
        text = sl.render_report(sl.join_turns(self.dir, days=1, now=self.now))
        self.assertIn("levers present on 0 of 1 turn(s)", text)
        row = next(ln for ln in text.splitlines() if "500" in ln)
        # An em-dash, not a 0.0 KB — the row is unmeasured, not measured-and-free (R4).
        self.assertIn("—", row)
        self.assertNotIn("0.0", row)

    def test_report_names_the_dearest_turn_first(self):
        self._ledger([{"ts": self._ts(hours=3), "kind": "tokens", "billable_tokens": 10,
                       "turn_id": "cheap",
                       "levers": {"tool_calls": 0, "tool_result_bytes": 0, "tool_result_images": 0}},
                      {"ts": self._ts(hours=1), "kind": "tokens", "billable_tokens": 90000,
                       "turn_id": "dear",
                       "levers": {"tool_calls": 6, "tool_result_bytes": 512000,
                                  "tool_result_images": 2}}])
        self._metrics([{"turn_id": "dear", "turns_served": 13, "source": "telegram"}])
        text = sl.render_report(sl.join_turns(self.dir, days=1, now=self.now))
        body = text.split("source")[1]
        self.assertLess(body.index("90,000"), body.index("10"))
        self.assertIn("500.0", body)  # 512000 bytes rendered as KB — the lever, named

    def test_cli_report_runs_read_only(self):
        self._ledger([{"ts": self._ts(hours=1), "kind": "tokens", "billable_tokens": 1,
                       "turn_id": "x"}])
        before = sorted(os.listdir(self.dir))
        self.assertEqual(sl.main(["report", "--state-dir", self.dir, "--json"]), 0)
        self.assertEqual(sorted(os.listdir(self.dir)), before)


if __name__ == "__main__":
    unittest.main()
