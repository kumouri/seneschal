#!/usr/bin/env python3
"""Tests for :mod:`reminders_cadence_lint` — Dream step 2k, the free-text ``Cadence``'s nightly rail.

``TheLintFindsOnlyWhatFailsOpen``  a row that parses is never asked about; one that does not is,
                                   with the parser's own message and fix.
``TheOncePerValueRule``            the dedupe: never twice for one value inside the picker's own
                                   lifetime, again when the value changes or the old picker expires.
``ThePickerShape``                 recommendation first, every option described, and it passes
                                   ``telegram_ask``'s own shape rules end to end (dry run, no network).
``TheCliNeverBlocksDream``         a bad hand-off is reported; a dry run writes and stamps nothing.

Every test passes an explicit temp ``state_dir``; none can reach the live ``state/``.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reminders_cadence_lint as lint  # noqa: E402

NOW = datetime(2026, 9, 28, 3, 0, tzinfo=timezone.utc)

ROWS = [
    {"id": "a" * 32, "title": "Evening walk", "cadence": "3 per week"},
    {"id": "b" * 32, "title": "Clean dab bowl", "cadence": "Every 4 days"},
    {"id": "c" * 32, "title": "Water plants", "cadence": "every 3.5 days"},
    {"id": "d" * 32, "title": "Mystery row", "cadence": "bananas"},
]


class FakeSend:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, question, options, meta):
        self.calls.append((question, options, meta))
        if not self.ok:
            return {"ok": False, "error": "boom"}
        return {"ok": True, "question_id": "q%d" % len(self.calls)}


class TheLintFindsOnlyWhatFailsOpen(unittest.TestCase):

    def test_only_the_unparseable_rows_are_findings(self):
        found = {f["title"]: f for f in lint.findings(ROWS)}
        self.assertEqual(set(found), {"Water plants", "Mystery row"})
        self.assertEqual(found["Water plants"]["suggestion"], "2 per week")
        self.assertIn("2 per 7 days", found["Water plants"]["error"])
        self.assertIsNone(found["Mystery row"]["suggestion"])

    def test_an_empty_cadence_is_a_finding_not_a_crash(self):
        found = lint.findings([{"id": "", "title": "Blank", "cadence": ""}])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["key"], "title:Blank")

    def test_rows_json_normalizes_ids_and_refuses_a_non_list(self):
        rows = lint.rows_from_json(json.dumps(
            [{"id": "00000000-0000-0000-0000-000000000000", "title": "X", "cadence": "Daily"}]))
        self.assertEqual(rows[0]["id"], "0" * 32)
        with self.assertRaises(ValueError):
            lint.rows_from_json('{"id": "x"}')


class TheOncePerValueRule(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _ledger(self):
        with open(os.path.join(self.d, lint.LEDGER_FILE), encoding="utf-8") as fh:
            return json.load(fh)["rows"]

    def test_the_reask_window_is_the_pickers_own_lifetime(self):
        import telegram_ask
        self.assertEqual(lint.REASK_AFTER_DAYS, telegram_ask.QUESTION_TTL_DAYS)

    def test_first_night_asks_once_per_bad_row(self):
        send = FakeSend()
        report = lint.run(ROWS, state_dir=self.d, send=send, now=NOW)
        self.assertEqual(len(send.calls), 2)
        self.assertEqual(report["bad"], 2)
        self.assertEqual(set(self._ledger()), {"c" * 32, "d" * 32})

    def test_the_next_night_the_same_value_is_held_not_reasked(self):
        lint.run(ROWS, state_dir=self.d, send=FakeSend(), now=NOW)
        send = FakeSend()
        report = lint.run(ROWS, state_dir=self.d, send=send, now=NOW + timedelta(days=1))
        self.assertEqual(send.calls, [])
        self.assertEqual(len(report["held"]), 2)

    def test_after_the_window_it_asks_again(self):
        lint.run(ROWS, state_dir=self.d, send=FakeSend(), now=NOW)
        send = FakeSend()
        lint.run(ROWS, state_dir=self.d, send=send, now=NOW + timedelta(days=6, hours=23))
        self.assertEqual(send.calls, [])
        lint.run(ROWS, state_dir=self.d, send=send,
                 now=NOW + timedelta(days=lint.REASK_AFTER_DAYS))
        self.assertEqual(len(send.calls), 2)

    def test_a_changed_bad_value_is_a_new_question(self):
        lint.run(ROWS, state_dir=self.d, send=FakeSend(), now=NOW)
        changed = [dict(r, cadence="bananas!!") if r["title"] == "Mystery row" else r
                   for r in ROWS]
        send = FakeSend()
        lint.run(changed, state_dir=self.d, send=send, now=NOW + timedelta(days=1))
        self.assertEqual([m["title"] for _, _, m in send.calls], ["Mystery row"])

    def test_a_failed_send_is_not_marked_and_retries_next_night(self):
        report = lint.run(ROWS, state_dir=self.d, send=FakeSend(ok=False), now=NOW)
        self.assertEqual(len(report["errors"]), 2)
        self.assertFalse(os.path.exists(os.path.join(self.d, lint.LEDGER_FILE)))
        send = FakeSend()
        lint.run(ROWS, state_dir=self.d, send=send, now=NOW + timedelta(days=1))
        self.assertEqual(len(send.calls), 2)

    def test_a_fixed_row_drops_out_so_a_later_regression_asks_afresh(self):
        lint.run(ROWS, state_dir=self.d, send=FakeSend(), now=NOW)
        fixed = [dict(r, cadence="2 per week") if r["title"] == "Water plants" else r
                 for r in ROWS]
        report = lint.run(fixed, state_dir=self.d, send=FakeSend(), now=NOW + timedelta(days=1))
        self.assertEqual(report["cleared"], ["c" * 32])
        send = FakeSend()
        lint.run(ROWS, state_dir=self.d, send=send, now=NOW + timedelta(days=2))
        self.assertEqual([m["title"] for _, _, m in send.calls], ["Water plants"])

    def test_an_unreadable_stamp_asks_rather_than_going_quiet(self):
        f = lint.findings(ROWS)[0]
        self.assertTrue(lint.should_ask({"value": f["cadence"], "asked_at": "garbage"}, f, NOW))
        self.assertTrue(lint.should_ask(None, f, NOW))

    def test_a_dry_run_writes_nothing(self):
        lint.run(ROWS, state_dir=self.d, send=FakeSend(), now=NOW, dry_run=True)
        self.assertEqual(os.listdir(self.d), [])


class ThePickerShape(unittest.TestCase):

    def test_the_parsers_fix_is_the_first_option(self):
        f = [x for x in lint.findings(ROWS) if x["title"] == "Water plants"][0]
        question, options, meta = lint.build_ask(f)
        self.assertIn("Water plants", question)
        self.assertIn("every 3.5 days", question)
        self.assertTrue(options[0].startswith('Change to "2 per week"|'))
        self.assertTrue(any(o.startswith("Leave it|") for o in options))
        self.assertEqual(meta["kind"], lint.ASK_META_KIND)
        self.assertEqual(meta["reminder_id"], "c" * 32)

    def test_with_no_fix_typing_it_is_the_recommendation(self):
        f = [x for x in lint.findings(ROWS) if x["title"] == "Mystery row"][0]
        _, options, meta = lint.build_ask(f)
        self.assertTrue(options[0].startswith("I'll type it|"))
        self.assertIsNone(meta["suggestion"])

    def test_a_multiple_per_day_fix_says_times_must_be_set(self):
        f = lint.findings([{"id": "e" * 32, "title": "Drink water", "cadence": "4 per day"}])[0]
        _, options, _ = lint.build_ask(f)
        self.assertIn("Times", options[0])

    def test_every_picker_passes_telegram_asks_own_shape_rules(self):
        """End to end through the real CLI in --dry-run: the description refusal and the citation
        gate run; nothing is sent and nothing is recorded."""
        with tempfile.TemporaryDirectory() as tmp:
            for f in lint.findings(ROWS):
                question, options, meta = lint.build_ask(f)
                with self.subTest(title=f["title"]):
                    res = lint.send_via_cli(question, options, meta, state_dir=tmp,
                                            env_file=None, dry_run=True)
                    self.assertTrue(res.get("ok"), res)
                    self.assertTrue(res.get("dry_run"))
            self.assertEqual(os.listdir(tmp), [])


class TheCliNeverBlocksDream(unittest.TestCase):

    def _main(self, argv):
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            return lint.main(argv), sys.stdout.getvalue()
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    def test_a_malformed_handoff_is_reported_not_read_as_no_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rows.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            code, out = self._main(["--rows-json", path, "--state-dir", tmp])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])

    def test_a_real_pass_stamps_the_dream_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rows.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(ROWS, fh)
            with mock.patch.object(lint, "send_via_cli",
                                   lambda *a, **k: {"ok": True, "question_id": "q1"}):
                code, out = self._main(["--rows-json", path, "--state-dir", tmp])
            with open(os.path.join(tmp, "dream-steps.json"), encoding="utf-8") as fh:
                ledger = json.load(fh)
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertEqual((body["source"], body["bad"], len(body["asked"])), ("rows-json", 2, 2))
        self.assertIn("last_ok", ledger[lint.DREAM_STEP])

    def test_the_id_cache_is_the_fallback_source_and_a_dry_run_stamps_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "reminders-id-cache.md"), "w", encoding="utf-8") as fh:
                fh.write("| Reminder | Page id | Type | Window | Cadence | Importance |\n"
                         "|---|---|---|---|---|---|\n"
                         "| Water plants | `%s` | Habit | Evening | every 3.5 days | 📌 Low |\n"
                         % ("c" * 32))
            code, out = self._main(["--state-dir", tmp, "--dry-run"])
            left = sorted(os.listdir(tmp))
        body = json.loads(out)
        self.assertEqual((code, body["source"], body["bad"]), (0, "id-cache", 1))
        self.assertEqual(left, ["reminders-id-cache.md"])


if __name__ == "__main__":
    unittest.main()
