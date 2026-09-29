#!/usr/bin/env python3
"""The `failures.jsonl` breadcrumb lands ON THE FAILURE PATH, not merely "the code didn't crash".

One class per instrumented site. `failures.record` is called from inside an already-degraded
``except`` branch, so the only way to know a site is wired is to force that branch and read the row
back. Sites covered here:

  * `sentinel.check_reminders` — a reminder send that comes back not-ok (not a Python raise: the
    send itself failed, so the row is written beside the `reminder_send_failed` signal)
  * `jobs.cancel_job` — a `save_job` claim write that raises (the cancel itself must still land)
  * `telegram_ask.load_store` — a corrupt question store, quarantined aside rather than overwritten
  * `telegram_ask.ask` / `ask_grid` — a Mouth row that could not be written after a LANDED send
  * `telegram_ask.resolve` — a twin settlement (optional `merge_guard`) that raised after the answer
  * `telegram_ask.ask` (CLI) — an ask that raised and was turned into exit 1

The same shape extends to the other fail-open sites as their modules land (the daemon's outbox
backlog read): add one class per site.

No live network, no live store, no live `claude`. Stdlib ``unittest`` only.

Run:  python -m unittest discover -s seneschal/scripts -p "test_failure_rows.py"
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import clock  # noqa: E402
import failures  # noqa: E402
import identity_common  # noqa: E402
import io  # noqa: E402
import jobs  # noqa: E402
import mouth  # noqa: E402
import sentinel as sn  # noqa: E402
import stateio  # noqa: E402
import telegram_ask as ta  # noqa: E402
import tz_common  # noqa: E402

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _rows(state_dir):
    return list(stateio.iter_jsonl(os.path.join(state_dir, failures.FAILURES_FILENAME)))


class SentinelReminderSendFailed(unittest.TestCase):
    """`sentinel.check_reminders`'s `reminder_send_failed` signal is also a failure row."""

    def setUp(self):
        # Pin the owner zone + identity so the curfew/staleness gates can't depend on the runner's zone.
        for p in (mock.patch.object(tz_common, "_zone", return_value=timezone(timedelta(hours=-5))),
                  mock.patch.object(clock, "load_identity", return_value={}),
                  mock.patch.object(identity_common, "load_identity", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self._orig = sn._deliver_reminder
        sn._deliver_reminder = lambda ch, raw, *a, **k: (
            {"ok": False, "error": "telegram 500"}, "telegram")
        self.addCleanup(setattr, sn, "_deliver_reminder", self._orig)

    def _queue(self, rid):
        sn.save_json(os.path.join(self.dir, "reminders.json"),
                     [{"id": rid, "text": "Water the plants.",
                       "due_at": NOW.isoformat().replace("+00:00", "Z")}])

    def test_a_failed_send_writes_a_failure_row(self):
        self._queue("r1")
        signals = sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        self.assertIn("reminder_send_failed", {s.get("kind") for s in signals})
        rows = _rows(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site"], "sentinel.check_reminders")
        self.assertEqual(rows[0]["kind"], "reminder_send_failed")
        self.assertIn("r1", rows[0]["detail"])

    def test_a_successful_send_writes_no_row(self):
        sn._deliver_reminder = lambda ch, raw, *a, **k: ({"ok": True}, "telegram")
        self._queue("r2")
        sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        self.assertEqual(_rows(self.dir), [])


class JobsCancelSaveFailed(unittest.TestCase):
    """`jobs.cancel_job` swallowing a `save_job` failure on its claim write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name

    def _start(self):
        return jobs.start_job(self.state, "doomed", ["python", "-c", "pass"],
                              runner=lambda argv, **k: mock.Mock(pid=1234), now=NOW)

    def test_a_save_job_raise_on_the_claim_still_cancels_and_writes_a_row(self):
        rec = self._start()
        real_save = jobs.save_job
        calls = {"n": 0}

        def flaky(state_dir, r):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_save(state_dir, r)

        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            with mock.patch.object(jobs, "save_job", side_effect=flaky):
                out = jobs.cancel_job(self.state, rec["id"], now=NOW, why="test")
        # The cancel still lands (the post-kill re-assert is the second, successful save) — losing the
        # claim's OWN write must never cost the cancel itself.
        self.assertEqual(out["status"], jobs.CANCELLED)
        rows = _rows(self.state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site"], "jobs.cancel_job")
        self.assertEqual(rows[0]["kind"], "cancel_claim_save_failed")
        self.assertIn(rec["id"], rows[0]["detail"])

    def test_an_ordinary_cancel_writes_no_row(self):
        rec = self._start()
        with mock.patch.object(jobs, "kill_pid", lambda *_: None):
            jobs.cancel_job(self.state, rec["id"], now=NOW, why="test")
        self.assertEqual(_rows(self.state), [])


class _FakeApi:
    """Stands in for `telegram_send.api_call`: answers sendMessage with an id, everything else ok."""

    def __init__(self, fail=()):
        self.fail = set(fail)

    def __call__(self, c, method, params, timeout=30):
        if method in self.fail:
            raise RuntimeError(f"Telegram {method} failed: deliberate test failure")
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 4242}}
        return {"ok": True, "result": True}


_CFG = {"token": "tok", "chat_id": "555", "api_base": "https://example.invalid", "parse_mode": ""}
_OPTIONS = [{"label": "A", "description": "the first"}, {"label": "B", "description": "the second"}]


class _TelegramAskBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name
        err = mock.patch("sys.stderr", io.StringIO())
        err.start()
        self.addCleanup(err.stop)


class TelegramAskCorruptStore(_TelegramAskBase):
    """`telegram_ask.load_store` quarantining a store it cannot parse."""

    def test_a_corrupt_store_is_quarantined_and_writes_a_row(self):
        path = ta.store_path(self.state)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        store = ta.load_store(path)
        self.assertEqual(store["questions"], {})
        rows = _rows(self.state)
        self.assertEqual([(r["site"], r["kind"]) for r in rows],
                         [("telegram_ask.load_store", "corrupt_store")])
        self.assertIn("quarantined=True", rows[0]["detail"])
        self.assertTrue(any(n.startswith(ta.QUESTIONS_FILE + ".corrupt-")
                            for n in os.listdir(self.state)))

    def test_a_missing_store_writes_no_row(self):
        ta.load_store(ta.store_path(self.state))
        self.assertEqual(_rows(self.state), [])


class TelegramAskMouthRowFailed(_TelegramAskBase):
    """`telegram_ask.ask`/`ask_grid` swallowing a Mouth-row failure AFTER the picker landed."""

    def test_a_broken_mouth_still_sends_and_writes_a_row(self):
        with mock.patch.object(mouth, "record_assertion", side_effect=OSError("disk full")):
            res = ta.ask(_CFG, self.state, "Which?", _OPTIONS, api=_FakeApi(), now=NOW)
        self.assertTrue(res["sent"])
        rows = _rows(self.state)
        self.assertEqual([(r["site"], r["kind"]) for r in rows],
                         [("telegram_ask.ask", "mouth_row_failed")])
        self.assertIn(res["question_id"], rows[0]["detail"])

    def test_the_grid_path_writes_its_own_row(self):
        items = [{"id": "a", "label": "item a"}]
        with mock.patch.object(mouth, "record_assertion", side_effect=OSError("disk full")):
            res = ta.ask_grid(_CFG, self.state, "Owner?", items, ["X", "Y"], api=_FakeApi(), now=NOW)
        self.assertTrue(res["sent"])
        self.assertEqual([(r["site"], r["kind"]) for r in _rows(self.state)],
                         [("telegram_ask.ask_grid", "mouth_row_failed")])

    def test_an_ordinary_ask_writes_no_row(self):
        ta.ask(_CFG, self.state, "Which?", _OPTIONS, api=_FakeApi(), now=NOW)
        self.assertEqual(_rows(self.state), [])


class TelegramAskTwinSettleFailed(_TelegramAskBase):
    """`telegram_ask.resolve` swallowing a twin-settlement failure — the answer must still stand.
    `merge_guard` is optional, so a stand-in is patched in whether or not the real one is installed."""

    def test_a_raising_twin_settlement_keeps_the_answer_and_writes_a_row(self):
        qid = ta.ask(_CFG, self.state, "Which?", _OPTIONS, api=_FakeApi(), now=NOW)["question_id"]
        broken = mock.Mock()
        broken.twin_settlements.side_effect = RuntimeError("guard exploded")
        with mock.patch.object(ta, "merge_guard", broken):
            res = ta.resolve(_CFG, self.state, f"q:{qid}:0", "cb1", api=_FakeApi(), now=NOW)
        self.assertTrue(res["answered"])
        rows = _rows(self.state)
        self.assertEqual([(r["site"], r["kind"]) for r in rows],
                         [("telegram_ask.resolve", "twin_settle_failed")])
        self.assertIn(qid, rows[0]["detail"])

    def test_without_merge_guard_there_is_no_dedupe_and_no_row(self):
        qid = ta.ask(_CFG, self.state, "Which?", _OPTIONS, api=_FakeApi(), now=NOW)["question_id"]
        with mock.patch.object(ta, "merge_guard", None):
            res = ta.resolve(_CFG, self.state, f"q:{qid}:0", "cb1", api=_FakeApi(), now=NOW)
        self.assertTrue(res["answered"])
        self.assertNotIn("twins_settled", res)
        self.assertEqual(_rows(self.state), [])


class TelegramAskCliAskFailed(_TelegramAskBase):
    """`telegram_ask._cmd_ask` turning a raised send into exit 1 — and leaving a row for it."""

    def test_a_failed_send_exits_1_and_writes_a_row(self):
        argv = ["--state-dir", self.state, "ask", "--question", "Which?",
                "--option", "A|the first", "--option", "B|the second", "--chat-id", "555",
                "--topic", "main"]
        with mock.patch.object(ta, "load_env", lambda *_: {}), \
                mock.patch.object(ta, "cfg", lambda *_: dict(_CFG)), \
                mock.patch.object(ta, "api_call", _FakeApi(fail={"sendMessage"})), \
                mock.patch("sys.stdout", io.StringIO()):
            rc = ta.main(argv)
        self.assertEqual(rc, 1)
        self.assertEqual([(r["site"], r["kind"]) for r in _rows(self.state)],
                         [("telegram_ask.ask", "ask_failed")])


if __name__ == "__main__":
    unittest.main()
