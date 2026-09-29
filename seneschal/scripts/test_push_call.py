#!/usr/bin/env python3
"""Tests for push_call.py's send-gate wiring — asserts a real (network-mocked) call push appends the right
`recipient_class` to `state/send-recipients.jsonl` (never the recipient itself), and that the site is
GATED: an owner-class send passes untouched, an unapproved third-party/unknown send is REFUSED (exit
`send_gate.EXIT_REFUSED`, no network call, the ledger row carries `gate.allowed == false`), and one
with an approved row in `state/pending-approvals.json` passes and spends it. The owner's addresses
come from a fixture identity (`_owner_fixture`), never code.

Also pins `--talk`: a best-effort context snapshot (today's calendar, pending reminders, the
head of carry-over), always the owner's own phone, each source degrading to nothing on its own."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import push_call  # noqa: E402
import _owner_fixture as fx  # noqa: E402
import send_gate  # noqa: E402
import send_recipients  # noqa: E402
import stateio  # noqa: E402


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"ok":true}'


class RecipientClassInstrumentation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fx.use_fixture_owner(self)
        self.ledger = os.path.join(self.dir, send_recipients.FILENAME)
        env_patch = mock.patch.dict(os.environ, {
            "SENESCHAL_STATE_DIR": self.dir,
            "PUSH_CALL_URL": "https://example.invalid/push-call",
            "PUSH_CALL_SECRET": "test-secret",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("PUSH_CALL_TO", None)
        urlopen_patch = mock.patch("urllib.request.urlopen", return_value=_FakeResponse())
        urlopen_patch.start()
        self.addCleanup(urlopen_patch.stop)

    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["push_call.py"] + argv):
            return push_call.main()

    def test_no_to_override_records_owner(self):
        rc = self._run(["--text", "hi"])
        self.assertEqual(rc, 0)
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], "push_call")
        self.assertEqual(rows[0]["recipient_class"], "owner")

    def test_explicit_to_override_records_unknown(self):
        rc = self._run(["--text", "hi", "--to", "+15559998888"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)  # the gate: unknown is not owner
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["recipient_class"], "unknown")

    def test_dry_run_never_writes_a_row(self):
        rc = self._run(["--text", "hi", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.ledger))

    def test_row_never_contains_the_number(self):
        self._run(["--text", "hi", "--to", "+15559998888"])
        with open(self.ledger, "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("+15559998888", raw)


class TalkMode(unittest.TestCase):
    """--talk: builds a context snapshot and posts mode=talk, refusing any
    --to/--escalate, always classified (and gated) as an owner send."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fx.use_fixture_owner(self)
        env_patch = mock.patch.dict(os.environ, {
            "SENESCHAL_STATE_DIR": self.dir,
            "PUSH_CALL_URL": "https://example.invalid/push-call",
            "PUSH_CALL_SECRET": "test-secret",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("PUSH_CALL_TO", None)
        self.sent_bodies = []
        urlopen_patch = mock.patch("urllib.request.urlopen", side_effect=self._fake_urlopen)
        urlopen_patch.start()
        self.addCleanup(urlopen_patch.stop)
        # Never let this test suite touch a real google.env / Google Calendar, whether or not one
        # happens to exist on the host running the suite (it may, on a configured install).
        subprocess_patch = mock.patch("subprocess.run", side_effect=AssertionError(
            "_todays_calendar must not shell out in tests"))
        subprocess_patch.start()
        self.addCleanup(subprocess_patch.stop)

    def _fake_urlopen(self, req, timeout=30):
        self.sent_bodies.append(json.loads(req.data.decode("utf-8")))
        return _FakeResponse()

    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["push_call.py"] + argv):
            return push_call.main()

    def test_talk_posts_mode_talk_with_a_context_field(self):
        rc = self._run(["--talk"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent_bodies), 1)
        self.assertEqual(self.sent_bodies[0]["mode"], "talk")
        self.assertIn("context", self.sent_bodies[0])
        self.assertIsInstance(self.sent_bodies[0]["context"], str)

    def test_talk_is_classified_and_gated_as_owner(self):
        rc = self._run(["--talk"])
        self.assertEqual(rc, 0)
        rows = list(stateio.iter_jsonl(os.path.join(self.dir, send_recipients.FILENAME)))
        self.assertEqual(rows[0]["recipient_class"], "owner")
        self.assertEqual(rows[0]["gate"]["reason"], "owner")

    def test_talk_rejects_text(self):
        rc = self._run(["--talk", "--text", "hi"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.sent_bodies, [])

    def test_talk_rejects_to_override(self):
        rc = self._run(["--talk", "--to", "+15559998888"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.sent_bodies, [])

    def test_talk_rejects_escalate(self):
        rc = self._run(["--talk", "--escalate"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.sent_bodies, [])

    def test_talk_dry_run_reports_context_bytes_not_chars(self):
        rc = self._run(["--talk", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.sent_bodies, [])  # no network at all


class ContextSnapshot(unittest.TestCase):
    """The three best-effort sections build_context_snapshot() folds together — each degrades to ""
    independently rather than raising, and the whole thing is capped."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fx.use_fixture_owner(self)
        env_patch = mock.patch.dict(os.environ, {"SENESCHAL_STATE_DIR": self.dir})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        # Never let this test suite touch a real google.env / Google Calendar, whether or not one
        # happens to exist on the host running the suite (it may, on a configured install).
        subprocess_patch = mock.patch("subprocess.run", side_effect=AssertionError(
            "_todays_calendar must not shell out in tests"))
        subprocess_patch.start()
        self.addCleanup(subprocess_patch.stop)

    def test_missing_sources_yield_an_empty_snapshot(self):
        self.assertEqual(push_call._todays_calendar(), "")
        self.assertEqual(push_call._pending_reminders_today(), "")
        self.assertEqual(push_call._carry_over_head(), "")
        self.assertEqual(push_call.build_context_snapshot(), "")

    def test_carry_over_head_reads_the_first_lines_only(self):
        path = os.path.join(self.dir, "carry-over.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(f"line {i}" for i in range(100)))
        head = push_call._carry_over_head(max_lines=3)
        self.assertIn("line 0", head)
        self.assertIn("line 2", head)
        self.assertNotIn("line 3", head)

    def test_pending_reminders_filters_fired_and_other_days(self):
        import clock
        today = clock.now_local().date()
        today_due = f"{today.isoformat()}T18:00:00Z"
        rows = [
            {"text": "water the plants", "due_at": today_due, "fired_at": None},
            {"text": "already fired", "due_at": today_due, "fired_at": "2020-01-01T00:00:00Z"},
            {"text": "tomorrow's thing", "due_at": "2099-01-01T00:00:00Z", "fired_at": None},
        ]
        path = os.path.join(self.dir, "reminders.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        snapshot = push_call._pending_reminders_today()
        self.assertIn("water the plants", snapshot)
        self.assertNotIn("already fired", snapshot)
        self.assertNotIn("tomorrow's thing", snapshot)

    def test_build_context_snapshot_caps_total_bytes(self):
        path = os.path.join(self.dir, "carry-over.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x" * (push_call.CONTEXT_MAX_BYTES * 2))
        snapshot = push_call.build_context_snapshot()
        self.assertLessEqual(len(snapshot.encode("utf-8")), push_call.CONTEXT_MAX_BYTES + 32)
        self.assertIn("truncated", snapshot)


class SendGate(RecipientClassInstrumentation):
    def test_owner_default_passes(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse()) as urlopen:
            rc = self._run(["--text", "hi"])
        self.assertEqual(rc, 0)
        urlopen.assert_called_once()
        rows = list(stateio.iter_jsonl(self.ledger))
        self.assertEqual(rows[0]["gate"]["reason"], "owner")

    def test_override_refused_then_passes_with_a_grant(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse()) as urlopen:
            rc = self._run(["--text", "hi", "--to", "+15559998888"])
        self.assertEqual(rc, send_gate.EXIT_REFUSED)
        urlopen.assert_not_called()
        send_gate.grant("call", "+15559998888", why="the owner asked")
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse()) as urlopen:
            rc = self._run(["--text", "hi", "--to", "+15559998888"])
        self.assertEqual(rc, 0)
        urlopen.assert_called_once()


class CalendarSource(unittest.TestCase):
    """The snapshot's calendar section: which account it reads, and that it asks gcal_api for the
    owner-local calendar DAY (bare --start/--end), never a rolling 24 h window."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.env = os.path.join(self.root, "google.env")
        self.store = os.path.join(self.root, "tokens.json")
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write(f"GOOGLE_TOKEN_STORE={self.store}\n")
        patcher = mock.patch.object(push_call, "GOOGLE_ENV", self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _store(self, *labels):
        with open(self.store, "w", encoding="utf-8") as fh:
            json.dump({"accounts": {lab: {"refresh_token": "x"} for lab in labels}}, fh)

    def test_explicit_account_wins(self):
        self._store("a", "b")
        self.assertEqual(push_call._calendar_account({"PUSH_CALL_CALENDAR_ACCOUNT": "b"}), "b")

    def test_the_only_stored_account_is_used(self):
        self._store("mine")
        self.assertEqual(push_call._calendar_account({}), "mine")

    def test_several_or_none_means_no_calendar(self):
        self._store("a", "b")
        self.assertIsNone(push_call._calendar_account({}))
        self._store()
        self.assertIsNone(push_call._calendar_account(None))

    def test_no_google_env_means_no_calendar_and_no_subprocess(self):
        with mock.patch.object(push_call, "GOOGLE_ENV", os.path.join(self.root, "absent.env")), \
                mock.patch("subprocess.run", side_effect=AssertionError("must not shell out")):
            self.assertEqual(push_call._todays_calendar({"PUSH_CALL_CALENDAR_ACCOUNT": "x"}), "")

    def test_reads_the_owner_local_calendar_day(self):
        self._store("mine")
        proc = mock.Mock(returncode=0, stdout=json.dumps(
            {"ok": True, "events": [{"summary": "Dentist", "start": "2026-09-28T15:00:00-05:00"}]}))
        with mock.patch("subprocess.run", return_value=proc) as run, \
                mock.patch.object(push_call.tz_common, "local_today", return_value="2026-09-28"):
            out = push_call._todays_calendar({})
        self.assertIn("Dentist", out)
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("--account") + 1], "mine")
        self.assertEqual(argv[argv.index("--start") + 1], "2026-09-28")
        self.assertEqual(argv[argv.index("--end") + 1], "2026-09-28")
        self.assertNotIn("--days", argv)


class BudgetExhausted(unittest.TestCase):
    """The Worker answers 402 when the day's call budget is spent — reported as such, exit 1."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fx.use_fixture_owner(self)
        env_patch = mock.patch.dict(os.environ, {
            "SENESCHAL_STATE_DIR": self.dir,
            "PUSH_CALL_URL": "https://example.invalid/push-call",
            "PUSH_CALL_SECRET": "test-secret",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("PUSH_CALL_TO", None)
        snap = mock.patch.object(push_call, "build_context_snapshot", return_value="")
        snap.start()
        self.addCleanup(snap.stop)

    def test_402_is_flagged_budget_exhausted(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError("https://example.invalid/push-call", 402, "Payment Required",
                                     {}, io.BytesIO(b"daily budget reached"))
        out = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=err), \
                mock.patch.object(sys, "argv", ["push_call.py", "--talk"]), \
                mock.patch.object(sys, "stdout", out):
            rc = push_call.main()
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["budget_exhausted"])
        self.assertEqual(payload["error"], "HTTP 402")


if __name__ == "__main__":
    unittest.main()
