#!/usr/bin/env python3
"""The duplicate-completion-push fix — `job_push_ledger.py` + its two wiring points in `jobs.py`.

**What these guard.** A cancelled job can push the owner the same completion message twice, seconds
apart, while its record carries exactly ONE `notified_at`. That combination is the whole diagnosis:
`cancel_job`'s post-kill re-assert writes back its stale in-memory record after the daemon has already
pushed and stamped, so the next reconcile tick reads a terminal un-notified job and pushes again.
Cancels are the exposed path; a non-cancel terminal job has a single writer at the end.

Two independent mechanisms, and both are tested here because either alone leaves a hole:

  1. `reassert_cancel` MERGES instead of clobbering (fixes the measured writer).
  2. `job_push_ledger` refuses a second push for the same `(job, outcome)` (fixes every OTHER writer,
     including ones nobody has enumerated, plus the ambiguous-send and cross-process cases).

**NOTHING HERE REACHES TELEGRAM.** Every send goes through the `notify=` seam `reconcile` already
takes; no test in this file imports `telegram_send`, spawns a subprocess, or opens a socket.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import job_push_ledger as jpl  # noqa: E402
import jobs  # noqa: E402

NOW = datetime(2026, 8, 27, 2, 29, 41, tzinfo=timezone.utc)   # the cancel instant


class _Recorder:
    """A `notify` seam. `result` is what the callback returns — a bool (the original contract) or a
    mapping carrying the send's phase. Records every push so a duplicate is countable."""

    def __init__(self, result=True):
        self.result = result
        self.sent: list = []

    def __call__(self, channel, text):
        self.sent.append((channel, text))
        return self.result() if callable(self.result) else self.result


class _LedgerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.logged: list = []

    def log(self, line):
        self.logged.append(line)

    def _terminal(self, job_id="20260826-095136-aaaa", status=jobs.CANCELLED, **extra):
        """A terminal, un-notified job record on disk — exactly what a reconcile tick picks up."""
        rec = {
            "schema": "seneschal.job/1", "id": job_id,
            "title": "watch for a new-message email",
            "argv": ["python", "watch.py"], "cwd": self.state,
            "log_path": os.path.join(self.state, f"{job_id}.log"),
            "status": status, "pid": 99684, "child_pid": 101404,
            "created_at": jobs._stamp(NOW - timedelta(hours=16)),
            "started_at": jobs._stamp(NOW - timedelta(hours=16)),
            "ended_at": jobs._stamp(NOW), "exit_code": None, "notified_at": None,
            "notify": {"channel": "telegram"}, "wake": True, "attempts": [],
        }
        if status == jobs.CANCELLED:
            rec["cancelled_at"] = jobs._stamp(NOW)
        rec.update(extra)
        os.makedirs(jobs.jobs_dir(self.state), exist_ok=True)
        jobs.save_job(self.state, rec)
        return rec

    def _reconcile(self, notify, when=NOW, **kw):
        return jobs.reconcile(self.state, notify=notify, log=self.log, now=when, **kw)

    def prevented_lines(self):
        return [ln for ln in self.logged if "DUPLICATE completion push PREVENTED" in ln]


# --------------------------------------------------------------------------- 1. the measured cause

class CancelReassertDoesNotClobberTests(_LedgerBase):
    """The writer half. `cancel_job` claims the record, then spends up to 60 s in two
    `taskkill /T /F` calls, then re-asserts. The daemon's ~5 s tick lands inside that window.

    The wrong fix here is deleting the re-assert: a test that only asserts "no double push" would
    pass on that and silently retire the cancel's belt (the re-assert that lands a claim whose first
    write failed). So the failed-claim case is tested too."""

    def test_the_re_assert_preserves_a_notified_at_written_during_the_kill(self):
        rec = self._terminal(status=jobs.RUNNING, notified_at=None)
        rec.pop("cancelled_at", None)
        jobs.save_job(self.state, rec)
        pushed_at = jobs._stamp(NOW + timedelta(seconds=1))

        def kill_and_push(_pid):
            # THE DAEMON'S TICK, inside the taskkill window: it stamps and saves.
            live = jobs.load_job(self.state, rec["id"])
            live["notified_at"] = pushed_at
            live["notify_delivery"] = jpl.LANDED
            jobs.save_job(self.state, live)

        with mock.patch.object(jobs, "kill_pid", kill_and_push):
            jobs.cancel_job(self.state, rec["id"], now=NOW)

        out = jobs.load_job(self.state, rec["id"])
        self.assertEqual(out["status"], jobs.CANCELLED, "the cancel must still own the ending")
        self.assertEqual(out["cancelled_at"], jobs._stamp(NOW))
        self.assertEqual(out["notified_at"], pushed_at,
                         "the post-kill re-assert erased the completion push's stamp — this IS "
                         "the duplicate-push bug")
        self.assertEqual(out["notify_delivery"], jpl.LANDED)

    def test_it_also_preserves_the_worktree_teardown_written_during_the_kill(self):
        """The same clobber cost `worktree.torn_down_at`, which is how a second `git worktree remove`
        produced the misleading `is not a working tree` reason."""
        rec = self._terminal(status=jobs.RUNNING, worktree={"path": "/tmp/wt"})
        rec.pop("cancelled_at", None)
        jobs.save_job(self.state, rec)

        def kill_and_tear_down(_pid):
            live = jobs.load_job(self.state, rec["id"])
            live["worktree"] = {"path": "/tmp/wt", "torn_down_at": jobs._stamp(NOW)}
            jobs.save_job(self.state, live)

        with mock.patch.object(jobs, "kill_pid", kill_and_tear_down):
            jobs.cancel_job(self.state, rec["id"], now=NOW)
        self.assertTrue(jobs.load_job(self.state, rec["id"])["worktree"].get("torn_down_at"))

    def test_a_claim_whose_write_failed_still_lands_on_the_re_assert(self):
        """The cancel's belt. The claim's `save_job` is inside its own try precisely so an unwritable
        record cannot cost the kill — which means the re-assert is sometimes the ONLY write that
        happens. A merge that skipped a record it could not read would silently retire that."""
        rec = self._terminal(status=jobs.RUNNING)
        rec.pop("cancelled_at", None)
        jobs.save_job(self.state, rec)
        calls = {"n": 0}
        real_save = jobs.save_job

        def save_failing_once(state_dir, r):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full during the claim")
            real_save(state_dir, r)

        with mock.patch.object(jobs, "save_job", save_failing_once), \
             mock.patch.object(jobs, "kill_pid", lambda *_: None):
            jobs.cancel_job(self.state, rec["id"], now=NOW)

        out = jobs.load_job(self.state, rec["id"])
        self.assertEqual(out["status"], jobs.CANCELLED,
                         "the failed claim never landed — the cancel's belt is gone")
        self.assertEqual(out["cancelled_at"], jobs._stamp(NOW))

    def test_a_record_missing_from_disk_falls_back_to_writing_the_whole_record(self):
        rec = self._terminal(status=jobs.RUNNING)
        os.remove(jobs.job_path(self.state, rec["id"]))
        rec["status"] = jobs.CANCELLED
        out = jobs.reassert_cancel(self.state, rec)
        self.assertEqual(out["status"], jobs.CANCELLED)
        self.assertIsNotNone(jobs.load_job(self.state, rec["id"]))


# --------------------------------------------------------- 2. the gate, against ANY other writer

class DuplicatePushIsPreventedByIdentityTests(_LedgerBase):
    """The gate half — and the one that does not depend on having enumerated the writers correctly.

    Each case here re-creates the *effect* of a clobber (a terminal record whose `notified_at` is
    gone) by a different route, and asserts that exactly one message ever leaves."""

    def test_the_cancel_race_sequence_end_to_end_sends_once(self):
        """The race, replayed: push, stamp, clobber, next tick. Without the ledger this emits two
        byte-identical messages seconds apart."""
        rec = self._terminal()
        notify = _Recorder()
        self.assertEqual(len(self._reconcile(notify)), 1)
        self.assertEqual(len(notify.sent), 1)

        # The clobber: cancel_job's stale in-memory record, written back after the taskkills.
        stale = dict(rec)
        with open(jobs.job_path(self.state, rec["id"]), "w", encoding="utf-8") as fh:
            json.dump(stale, fh)
        self.assertIsNone(jobs.load_job(self.state, rec["id"])["notified_at"],
                          "the test's own clobber didn't take")

        self._reconcile(notify, when=NOW + timedelta(seconds=24))
        self.assertEqual(len(notify.sent), 1,
                         "the owner was pushed twice for one ending — the duplicate-push bug")
        self.assertEqual(len(self.prevented_lines()), 1,
                         "a prevented duplicate must be visible in the log")

    def test_the_repaired_record_gets_the_ORIGINAL_stamp_back_not_now(self):
        """`ended_at` → `notified_at` is the push lag a diagnosis reads off. Repairing a clobbered
        record with `now` would silently inflate it for every future measurement."""
        rec = self._terminal()
        self._reconcile(_Recorder())
        with open(jobs.job_path(self.state, rec["id"]), "w", encoding="utf-8") as fh:
            json.dump(dict(rec), fh)
        self._reconcile(_Recorder(), when=NOW + timedelta(seconds=24))
        out = jobs.load_job(self.state, rec["id"])
        self.assertEqual(out["notified_at"], jobs._stamp(NOW))
        self.assertEqual(out["notify_delivery"], jpl.LANDED)

    def test_the_prevented_counter_is_queryable(self):
        """A dedupe nobody can audit is how a silent-drop defect ships."""
        rec = self._terminal()
        self._reconcile(_Recorder())
        for i in range(3):
            with open(jobs.job_path(self.state, rec["id"]), "w", encoding="utf-8") as fh:
                json.dump(dict(rec), fh)
            self._reconcile(_Recorder(), when=NOW + timedelta(seconds=30 * (i + 1)))
        self.assertEqual(jpl.stats(self.state)["prevented"], 3)
        self.assertEqual(jpl.stats(self.state)["landed"], 1)

    def test_a_different_ending_is_a_different_message_and_is_NOT_suppressed(self):
        """The gate is `(job, outcome)`. A record that reaches a genuinely different terminal state
        deserves its own push — suppressing that would be the silence this whole feature prevents."""
        rec = self._terminal(job_id="j-two", status=jobs.DONE)
        notify = _Recorder()
        self._reconcile(notify)
        rec["status"], rec["notified_at"] = jobs.FAILED, None
        jobs.save_job(self.state, rec)
        self._reconcile(notify, when=NOW + timedelta(seconds=10))
        self.assertEqual(len(notify.sent), 2)

    def test_two_jobs_with_identical_TEXT_both_push(self):
        """Identity is the job, never the wording. Two same-titled jobs cancelled minutes apart
        deserve one push each. A text-similarity gate would eat the second."""
        for jid in ("20260813-140307-aaaa", "20260813-140956-bbbb"):
            self._terminal(job_id=jid, status=jobs.CANCELLED)
        notify = _Recorder()
        self._reconcile(notify)
        self.assertEqual(len(notify.sent), 2)
        self.assertEqual(self.prevented_lines(), [])

    def test_the_wake_is_gated_by_the_same_claim_as_the_push(self):
        """The `[job finished: …]` relay duplicates the same way. The wake rides the `notified` list,
        so one gate covers both — asserted, because it is the kind of thing a later edit un-couples."""
        rec = self._terminal()
        woken: list = []
        jobs.reconcile(self.state, notify=_Recorder(), wake=woken.append, log=self.log, now=NOW)
        with open(jobs.job_path(self.state, rec["id"]), "w", encoding="utf-8") as fh:
            json.dump(dict(rec), fh)
        jobs.reconcile(self.state, notify=_Recorder(), wake=woken.append, log=self.log,
                       now=NOW + timedelta(seconds=24))
        self.assertEqual(len(woken), 1)


# ------------------------------------------------------------------- 3. the ambiguous-send case

class AmbiguousSendIsNotResentTests(_LedgerBase):
    """The send path splits a failure at connect time: pre-delivery (provably nothing was sent) vs
    AMBIGUOUS (the request was on the wire), and never retries an ambiguous one. A notify callback
    that collapsed that to a bare `False` would have `reconcile` read it as "send it again next
    tick" — **the same blind retry, one layer up.**"""

    def test_an_ambiguous_send_is_treated_as_notified_and_never_re_sent(self):
        self._terminal()
        notify = _Recorder({"ok": False, "ambiguous": True})
        self._reconcile(notify)
        self._reconcile(notify, when=NOW + timedelta(seconds=5))
        self._reconcile(notify, when=NOW + timedelta(minutes=10))
        self.assertEqual(len(notify.sent), 1, "an ambiguous send was blindly retried")

    def test_it_says_so_on_the_record_rather_than_claiming_delivery(self):
        rec = self._terminal()
        self._reconcile(_Recorder({"ok": False, "ambiguous": True}))
        out = jobs.load_job(self.state, rec["id"])
        self.assertEqual(out["notify_delivery"], jpl.AMBIGUOUS)
        self.assertTrue(out["notified_at"])
        self.assertTrue(any("AMBIGUOUS" in ln for ln in self.logged),
                        "an ambiguous push must be loud — it is the one case we cannot verify")

    def test_a_PROVABLY_failed_send_still_retries_next_tick(self):
        """The polarity that must not flip. A pre-delivery failure delivered nothing, so re-sending
        is the guarantee — a job that finished unreported is what `jobs.py` exists to prevent."""
        self._terminal()
        notify = _Recorder({"ok": False, "ambiguous": False})
        self._reconcile(notify)
        self._reconcile(notify, when=NOW + timedelta(seconds=5))
        self.assertEqual(len(notify.sent), 2)
        landed = _Recorder(True)
        self._reconcile(landed, when=NOW + timedelta(seconds=10))
        self.assertEqual(len(landed.sent), 1)

    def test_a_bare_bool_callback_still_works_exactly_as_before(self):
        """Every pre-existing caller and test hands back a bool. `False` from a caller with no phase
        information must keep retrying — conservative only because those paths never had the answer."""
        self._terminal()
        down = _Recorder(False)
        self._reconcile(down)
        self._reconcile(down, when=NOW + timedelta(seconds=5))
        self.assertEqual(len(down.sent), 2)
        self.assertEqual(jobs.delivery_of(True), jpl.LANDED)
        self.assertEqual(jobs.delivery_of(False), jpl.FAILED)


# --------------------------------------------------- 4. a restart between the send and the stamp

class RestartBetweenSendAndStampTests(_LedgerBase):
    """`seneschald-update` ff-pulls and reloads on every merge — several a day on a busy day — so a
    successor daemon reading a record its predecessor had already pushed is a live possibility, not
    a thought experiment. The record cannot answer it (the stamp is what died); the
    ledger can, because the claim goes down BEFORE the send."""

    def _die_after_sending(self, notify):
        """Send, then die before `notified_at` is written — the exact window."""
        sent: list = []

        def notify_then_die(channel, text):
            sent.append((channel, text))
            notify(channel, text)
            raise KeyboardInterrupt("daemon reloaded mid-push")

        try:
            self._reconcile(notify_then_die)
        except KeyboardInterrupt:
            pass
        return sent

    def test_the_successor_does_not_re_send_a_push_that_landed(self):
        rec = self._terminal()
        notify = _Recorder()
        self._die_after_sending(notify)
        self.assertEqual(len(notify.sent), 1)
        self.assertIsNone(jobs.load_job(self.state, rec["id"])["notified_at"],
                          "the test's premise is that the stamp never landed")
        # The successor daemon, one tick later.
        self._reconcile(notify, when=NOW + timedelta(seconds=5))
        self.assertEqual(len(notify.sent), 1)
        self.assertEqual(len(self.prevented_lines()), 1)

    def test_a_daemon_that_died_BEFORE_the_send_lands_still_notifies(self):
        """The other direction, and the one that must never regress into silence. An unresolved
        claim whose holder is gone goes stale and is TAKEN OVER — we accept a possible duplicate
        rather than risk a job finishing unreported, and the takeover says so in the log."""
        rec = self._terminal()
        jpl.claim(self.state, rec["id"], rec["status"], now=NOW)   # claimed, never resolved
        notify = _Recorder()
        later = NOW + timedelta(seconds=jpl.IN_FLIGHT_STALE_SEC + 1)
        self._reconcile(notify, when=later)
        self.assertEqual(len(notify.sent), 1, "a dead sender's claim silenced the push")
        self.assertEqual(jpl.stats(self.state)["taken_over"], 1)

    def test_an_unwritable_ledger_never_silences_a_push(self):
        """Every degradation here points at SENDING. A ledger that cannot be written is a bug worth
        knowing about; a completion push it swallowed is a job that finished unreported."""
        self._terminal()
        notify = _Recorder()
        with mock.patch.object(jpl, "_write", lambda *a, **kw: False):
            self._reconcile(notify)
        self.assertEqual(len(notify.sent), 1)


# ------------------------------------------------------- 5. a second sender racing the first

class SecondSenderRacingTests(_LedgerBase):
    """Two processes can genuinely reach the push at once: the daemon's ~5 s tick and a hand-run
    `python jobs.py reconcile`. Before the ledger, whether they duplicated came down to which one
    read the record first — the same lost-race shape as the cancel's, on the message rather than
    on the label."""

    def test_a_reconcile_that_starts_mid_send_does_not_send_too(self):
        second = _Recorder()

        def notify_with_a_rival_tick(channel, text):
            # Another process's reconcile pass, running while THIS send is still on the wire.
            self._reconcile(second, when=NOW + timedelta(seconds=1))
            return True

        self._terminal()
        first = _Recorder(True)

        def notify(channel, text):
            first.sent.append((channel, text))
            return notify_with_a_rival_tick(channel, text)

        self._reconcile(notify)
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(len(second.sent), 0, "the second sender pushed on top of an in-flight send")
        self.assertTrue(any("in-flight-elsewhere" in ln for ln in self.logged))

    def test_an_in_flight_refusal_is_a_WAIT_not_a_verdict(self):
        """It must not stamp anything: if the in-flight sender turns out to have failed, the next
        tick has to be free to send."""
        rec = self._terminal()
        jpl.claim(self.state, rec["id"], rec["status"], now=NOW)
        blocked = _Recorder()
        self._reconcile(blocked, when=NOW + timedelta(seconds=5))
        self.assertEqual(blocked.sent, [])
        self.assertIsNone(jobs.load_job(self.state, rec["id"])["notified_at"])
        jpl.resolve(self.state, rec["id"], jpl.FAILED, now=NOW + timedelta(seconds=6))
        after = _Recorder()
        self._reconcile(after, when=NOW + timedelta(seconds=10))
        self.assertEqual(len(after.sent), 1)


# --------------------------------------------------------------------------- 6. the ledger itself

class LedgerUnitTests(_LedgerBase):
    def test_a_landed_claim_is_never_granted_again(self):
        self.assertTrue(jpl.claim(self.state, "j", "done", now=NOW)["granted"])
        jpl.resolve(self.state, "j", jpl.LANDED, now=NOW)
        again = jpl.claim(self.state, "j", "done", now=NOW + timedelta(seconds=5))
        self.assertFalse(again["granted"])
        self.assertEqual(again["reason"], "already-delivered")
        self.assertEqual(again["delivered_at"], jpl._stamp(NOW))

    def test_a_failed_claim_is_granted_again_immediately(self):
        jpl.claim(self.state, "j", "done", now=NOW)
        jpl.resolve(self.state, "j", jpl.FAILED, now=NOW)
        again = jpl.claim(self.state, "j", "done", now=NOW + timedelta(seconds=5))
        self.assertTrue(again["granted"])
        self.assertEqual(again["reason"], "retry-after-failed-send")

    def test_a_landed_attempt_outranks_a_later_failed_one(self):
        """A takeover appends on top. A `landed` underneath is still the truth about what the owner saw,
        so `delivery_of` scans for a delivered attempt rather than reading only the last."""
        jpl.claim(self.state, "j", "done", now=NOW)
        jpl.resolve(self.state, "j", jpl.LANDED, now=NOW)
        entry = jpl.read(self.state, "j")
        entry["attempts"].append({"at": jpl._stamp(NOW), "delivery": jpl.FAILED,
                                  "resolved_at": jpl._stamp(NOW), "reason": "x"})
        jpl._write(self.state, entry)
        self.assertEqual(jpl.delivery_of(jpl.read(self.state, "j")), jpl.LANDED)

    def test_an_unreadable_entry_reads_as_no_record_of_a_push(self):
        os.makedirs(jpl.push_dir(self.state), exist_ok=True)
        with open(jpl.entry_path(self.state, "j"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        self.assertIsNone(jpl.read(self.state, "j"))
        self.assertTrue(jpl.claim(self.state, "j", "done", now=NOW)["granted"])

    def test_resolve_on_an_unknown_job_is_a_silent_no_op(self):
        jpl.resolve(self.state, "never-claimed", jpl.LANDED, now=NOW)   # must not raise

    def test_prune_forgets_the_entry_with_the_record(self):
        rec = self._terminal(status=jobs.DONE)
        self._reconcile(_Recorder())
        self.assertIsNotNone(jpl.read(self.state, rec["id"]))
        jobs.prune(self.state, days=0, now=NOW + timedelta(days=90))
        self.assertIsNone(jpl.read(self.state, rec["id"]),
                          "the ledger outlived the job it describes — it would grow forever")

    def test_the_ledger_directory_never_confuses_list_jobs(self):
        self._terminal(status=jobs.DONE)
        self._reconcile(_Recorder())
        self.assertEqual([r["id"] for r in jobs.list_jobs(self.state)],
                         ["20260826-095136-aaaa"])

    def test_nothing_but_the_push_path_writes_the_ledger(self):
        """The property that makes this work where `notified_at` did not: ONE writer. Asserted
        against the source — an import graph would not catch a future `cancel_job` learning to
        stamp it."""
        here = os.path.dirname(os.path.abspath(__file__))
        writers = []
        for name in sorted(os.listdir(here)):
            if not name.endswith(".py") or name.startswith("test_") or name == "job_push_ledger.py":
                continue
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                body = fh.read()
            if "job_push_ledger.claim(" in body or "job_push_ledger.resolve(" in body:
                writers.append(name)
        self.assertEqual(writers, ["jobs.py"], f"a second ledger writer appeared: {writers}")


class NothingReachesTelegramTests(_LedgerBase):
    """The hard constraint, asserted rather than assumed."""

    def test_no_send_module_is_reachable_from_the_push_path_under_test(self):
        self._terminal()
        with mock.patch.object(jobs.subprocess, "run",
                               side_effect=AssertionError("a test tried to spawn a subprocess")), \
             mock.patch.object(jobs.subprocess, "Popen",
                               side_effect=AssertionError("a test tried to spawn a process")):
            self._reconcile(_Recorder())

    def test_this_module_imports_no_sender(self):
        """Parsed, not grepped — a substring search over this file matches its own assertion list."""
        with open(os.path.abspath(__file__), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & {"telegram_send", "telegram_http", "telegram_ask",
                                     "discord_send", "sentinel", "presence"}, set())


if __name__ == "__main__":
    unittest.main()
