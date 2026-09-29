#!/usr/bin/env python3
"""Tests for usage_activity.py — the activity snapshot beside each plan-meter reading.

The request was *"/usage alongside what was happening in that time period"*; this module is the
"alongside", and these tests guard the three rules that make it honest rather than decorative:

* ``TheFiveBlocks``   — each field comes from the source it claims, and the window bounds it.
* ``BoundedScans``    — a tail read that did not reach back to ``since`` reports
                        ``scan_truncated: true``, so the number is a stated LOWER BOUND. A count
                        that quietly under-reports is worse than no count.
* ``NeverAZero``      — an unreadable source produces ``error`` and NO counts. A zero here would be
                        a lie that survives arithmetic, exactly as on the meter side.
* ``PeekCostStaysUnknown`` — the peek COUNT is measurable and the peek COST is not
                        (a peek is ``Popen`` with no pipes). The cost
                        field is null and must never become an estimate that later reads as measured.
* ``TwoWritersInMetrics`` — ``metrics.jsonl`` has two writers, and a reader that counts every row
                        there with no filter counts advisor rows as turns. Not here.

Every test builds its own temp state dir. Nothing reads or writes the live one.

Run:  python -m unittest test_usage_activity
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import usage_activity as ua  # noqa: E402

LOCAL = timezone(timedelta(hours=-5))  # a fixed owner offset; nothing here depends on which
UNTIL = datetime(2026, 8, 27, 14, 0, 0, tzinfo=LOCAL)
SINCE = UNTIL - timedelta(hours=1)
BEFORE = SINCE - timedelta(hours=2)          # outside the window, on the old side
AFTER = UNTIL + timedelta(minutes=5)          # outside the window, on the new side
INSIDE = SINCE + timedelta(minutes=20)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Base(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="usage-activity-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write_job(self, job_id, *, status="done", started=None, ended=None, title="a job"):
        os.makedirs(os.path.join(self.dir, "jobs"), exist_ok=True)
        rec = {"schema": "seneschal.job/1", "id": job_id, "title": title, "status": status,
               "created_at": iso(started or BEFORE), "started_at": iso(started or BEFORE),
               "ended_at": iso(ended) if ended else None}
        with open(os.path.join(self.dir, "jobs", f"{job_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)

    def write_jsonl(self, name, rows):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def write_log(self, lines):
        with open(os.path.join(self.dir, "presence.log"), "w", encoding="utf-8") as fh:
            for at, msg in lines:
                fh.write(f"[{at.isoformat(timespec='seconds')}] {msg}\n")


class TheFiveBlocks(Base):

    def test_jobs_separates_running_started_and_ended(self):
        """Usually the single most explanatory field: an overnight burn is most often the job
        fleet, and without this, when it burned has to be reconstructed by hand from job logs."""
        self.write_job("still-going", status="running", started=BEFORE)
        self.write_job("began-here", status="running", started=INSIDE)
        self.write_job("finished-here", status="done", started=BEFORE, ended=INSIDE)
        self.write_job("old-news", status="done", started=BEFORE, ended=BEFORE)
        block = ua.jobs_block(self.dir, SINCE, UNTIL)
        self.assertEqual(block["running_count"], 2)
        self.assertEqual({j["id"] for j in block["started"]}, {"began-here"})
        self.assertEqual({j["id"] for j in block["ended"]}, {"finished-here"})
        self.assertEqual(block["scanned_records"], 4)

    def test_jobs_names_each_one_by_id_and_title(self):
        self.write_job("j1", status="running", started=INSIDE, title="build: the thing")
        block = ua.jobs_block(self.dir, SINCE, UNTIL)
        self.assertEqual(block["running"][0], {"id": "j1", "title": "build: the thing",
                                               "status": "running"})

    def test_jobs_counts_stay_exact_when_the_lists_are_capped(self):
        for i in range(ua.JOBS_LIST_MAX + 7):
            self.write_job(f"j{i:03d}", status="running", started=INSIDE)
        block = ua.jobs_block(self.dir, SINCE, UNTIL)
        self.assertEqual(block["running_count"], ua.JOBS_LIST_MAX + 7)
        self.assertEqual(len(block["running"]), ua.JOBS_LIST_MAX)
        self.assertTrue(block["running_list_truncated"])

    def test_a_retry_pending_job_counts_as_running(self):
        """`jobs.ACTIVE`, not `RUNNING`: a job waiting out a retry backoff has not finished."""
        self.write_job("waiting", status="retry-pending", started=BEFORE)
        self.assertEqual(ua.jobs_block(self.dir, SINCE, UNTIL)["running_count"], 1)

    def test_warm_turns_context_and_cost_come_from_metrics(self):
        self.write_jsonl("metrics.jsonl", [
            {"ts": iso(BEFORE), "writer": "daemon", "session_id": "s0", "context_tokens": 1,
             "cost_usd": 9.0, "tokens": 10},
            {"ts": iso(INSIDE), "writer": "daemon", "session_id": "s1", "context_tokens": 120000,
             "cost_usd": 0.5, "tokens": 100, "turns_served": 4},
            {"ts": iso(INSIDE + timedelta(minutes=5)), "writer": "daemon", "session_id": "s2",
             "context_tokens": 170000, "cost_usd": 0.25, "tokens": 200, "turns_served": 1},
            {"ts": iso(AFTER), "writer": "daemon", "session_id": "s3", "context_tokens": 999},
        ])
        block = ua.warm_block(self.dir, SINCE, UNTIL)
        self.assertEqual(block["turns"], 2)
        self.assertEqual(block["sessions"], ["s1", "s2"])
        self.assertEqual(block["session_count"], 2)  # >1 means the session turned over mid-window
        self.assertEqual(block["context_tokens_max"], 170000)
        self.assertEqual(block["context_tokens_last"], 170000)
        self.assertEqual(block["cost_usd"], 0.75)
        self.assertEqual(block["tokens"], 300)
        self.assertEqual(block["last_turns_served"], 1)

    def test_governor_sums_billable_tokens_and_carries_the_basis_verbatim(self):
        self.write_jsonl("governor-ledger.jsonl", [
            {"ts": iso(BEFORE), "kind": "tokens", "billable_tokens": 9999,
             "basis": "input_token_equivalent_v1"},
            {"ts": iso(INSIDE), "kind": "tokens", "billable_tokens": 100,
             "basis": "input_token_equivalent_v1", "levers": {"tool_calls": 3,
                                                              "tool_result_bytes": 40}},
            {"ts": iso(INSIDE), "kind": "tokens", "billable_tokens": 57,
             "basis": "input_token_equivalent_v1", "levers": {"tool_calls": 1}},
        ])
        block = ua.governor_block(self.dir, SINCE, UNTIL)
        self.assertEqual(block["billable_tokens"], 157)
        self.assertEqual(block["basis"], ["input_token_equivalent_v1"])
        self.assertEqual(block["levers"]["tool_calls"], 4)
        self.assertEqual(block["kinds"], {"tokens": 2})
        self.assertNotIn("tokens", block)  # no row carried a raw sum → absent, never 0

    def test_governor_reports_raw_tokens_beside_billable_never_folded_in(self):
        """A ledger written before the billable breakdown carries only the flat `tokens`. It is
        reported as its own field; `billable_tokens` stays absent rather than borrowing it."""
        self.write_jsonl("governor-ledger.jsonl", [
            {"ts": iso(INSIDE), "kind": "chat_turn", "model": "m", "tokens": 1200},
            {"ts": iso(INSIDE), "kind": "chat_turn", "model": "m", "tokens": 300},
            {"ts": iso(BEFORE), "kind": "chat_turn", "model": "m", "tokens": 99999},
        ])
        block = ua.governor_block(self.dir, SINCE, UNTIL)
        self.assertEqual(block["tokens"], 1500)
        self.assertEqual(block["token_rows"], 2)
        self.assertNotIn("billable_tokens", block)

    def test_the_peek_count_comes_off_presence_log(self):
        self.write_log([(BEFORE, "• comms peek launched (pid 1)"),
                        (INSIDE, "• comms peek launched (pid 2)"),
                        (INSIDE, "• comms peek launched (pid 3)"),
                        (INSIDE, "• router (shadow): escalate/other"),
                        (AFTER, "• comms peek launched (pid 4)")])
        self.assertEqual(ua.log_block(self.dir, SINCE, UNTIL)["peeks_launched"], 2)

    def test_a_reload_inside_the_window_is_visible(self):
        """A reload resets the warm session's context, and the `/usage` panel shows context as the
        dominant usage characteristic (70-76% of usage at >150k). A burn rate read across a reload
        without noticing it is a wrong read."""
        self.write_log([(INSIDE, "• restart — reloading code"),
                        (INSIDE, "• cold warm-session start — last session is 242m old")])
        block = ua.log_block(self.dir, SINCE, UNTIL)
        self.assertEqual(block["reloads"], 1)
        self.assertEqual(block["cold_warm_session_starts"], 1)

    def test_the_daemon_block_knows_when_the_process_itself_came_up(self):
        self.write_log([])
        part = ua.log_block(self.dir, SINCE, UNTIL)
        fresh = ua.daemon_block({"pid": 7, "started_at": iso(INSIDE)}, SINCE, UNTIL, part)
        self.assertTrue(fresh["restarted_in_interval"])
        old = ua.daemon_block({"pid": 7, "started_at": iso(BEFORE)}, SINCE, UNTIL, part)
        self.assertFalse(old["restarted_in_interval"])
        self.assertGreater(old["uptime_sec"], 0)

    def test_outside_the_daemon_the_block_says_it_does_not_know(self):
        """Run from the CLI there is no daemon process. It says `known: false` rather than inventing
        one — and the log-derived markers still work either way."""
        self.write_log([(INSIDE, "• restart — reloading code")])
        block = ua.daemon_block(None, SINCE, UNTIL, ua.log_block(self.dir, SINCE, UNTIL))
        self.assertFalse(block["known"])
        self.assertEqual(block["reloads"], 1)


class BoundedScans(Base):

    def test_a_scan_that_reached_back_past_since_is_not_truncated(self):
        self.write_jsonl("metrics.jsonl", [{"ts": iso(BEFORE), "writer": "daemon"},
                                           {"ts": iso(INSIDE), "writer": "daemon"}])
        self.assertFalse(ua.warm_block(self.dir, SINCE, UNTIL)["scan_truncated"])

    def test_a_scan_that_did_not_reach_back_reports_a_lower_bound(self):
        """The cap is the ceiling on what one snapshot will ever read. Hitting it must SAY so —
        a silently under-counted hour reads exactly like a quiet one."""
        rows = [{"ts": iso(INSIDE), "writer": "daemon", "pad": "x" * 400} for _ in range(200)]
        self.write_jsonl("metrics.jsonl", rows)
        block = ua.warm_block(self.dir, SINCE, UNTIL)
        self.assertTrue(block["scan_truncated"])
        self.assertTrue(block["scan_hit_file_start"])  # the FILE is younger than the window
        self.assertGreater(block["turns"], 0)  # still a real, useful lower bound

    def test_the_byte_cap_is_distinguished_from_a_short_file(self):
        """Both under-count, so both set `scan_truncated` — but WHY matters when you are deciding
        whether to widen the cap or accept that the log rotated."""
        path = os.path.join(self.dir, "presence.log")
        stamp = INSIDE.isoformat(timespec="seconds")
        padding = "pad " * 60
        with open(path, "w", encoding="utf-8") as fh:
            for _ in range(400):
                fh.write("[" + stamp + "] " + padding + "comms peek launched (pid 1)\n")
        _lines, truncated, hit_start = ua.tail_text(path, SINCE, ua._log_ts, start=64, cap=256)
        self.assertTrue(truncated)
        self.assertFalse(hit_start)  # the CAP stopped us, not the start of the file

    def test_the_tail_grows_until_it_covers_the_window(self):
        rows = ([{"ts": iso(BEFORE), "writer": "daemon", "pad": "y" * 300} for _ in range(400)]
                + [{"ts": iso(INSIDE), "writer": "daemon"} for _ in range(3)])
        self.write_jsonl("metrics.jsonl", rows)
        block = ua.warm_block(self.dir, SINCE, UNTIL)
        self.assertFalse(block["scan_truncated"])
        self.assertEqual(block["turns"], 3)

    def test_a_partial_first_line_is_dropped_rather_than_half_parsed(self):
        path = os.path.join(self.dir, "metrics.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": iso(BEFORE), "writer": "daemon", "pad": "z" * 300}) + "\n")
            fh.write(json.dumps({"ts": iso(INSIDE), "writer": "daemon"}) + "\n")
        lines, _truncated, _at_start = ua.tail_text(path, SINCE, ua._jsonl_ts, start=64)
        for line in lines:
            json.loads(line)  # no fragment survived into the parse


class NeverAZero(Base):

    def test_a_missing_source_produces_an_error_and_no_counts(self):
        snap = ua.collect(self.dir, SINCE, UNTIL)
        for name in ("warm", "governor"):
            with self.subTest(block=name):
                self.assertIn("error", snap[name])
                self.assertNotIn("turns", snap[name])
                self.assertNotIn("billable_tokens", snap[name])

    def test_an_absent_optional_field_is_absent_not_zero(self):
        """`context_tokens` / `cost_usd` are optional on a metrics row (older rows predate them).
        An absent field is skipped, so `context_tokens_max` is missing rather than 0."""
        self.write_jsonl("metrics.jsonl", [{"ts": iso(INSIDE), "writer": "daemon",
                                            "session_id": "s1"}])
        block = ua.warm_block(self.dir, SINCE, UNTIL)
        self.assertEqual(block["turns"], 1)
        self.assertNotIn("context_tokens_max", block)
        self.assertNotIn("cost_usd", block)

    def test_collect_never_raises_whatever_the_state_dir_looks_like(self):
        for state_dir in (self.dir, os.path.join(self.dir, "does-not-exist"), ""):
            with self.subTest(state_dir=state_dir):
                snap = ua.collect(state_dir, SINCE, UNTIL)
                self.assertEqual(set(snap), {"jobs", "warm", "governor", "peeks", "daemon"})

    def test_a_corrupt_line_is_skipped_and_the_rest_still_counts(self):
        path = os.path.join(self.dir, "governor-ledger.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json at all\n")
            fh.write(json.dumps({"ts": iso(INSIDE), "kind": "tokens",
                                 "billable_tokens": 42}) + "\n")
        self.assertEqual(ua.governor_block(self.dir, SINCE, UNTIL)["billable_tokens"], 42)


class PeekCostStaysUnknown(Base):

    def test_the_count_is_measured_and_the_cost_is_null_forever(self):
        """A peek is `subprocess.Popen` with no pipes and no
        `--output-format`, so it writes to neither ledger. Estimating a number here would produce
        something that reads as measured a month later."""
        self.write_log([(INSIDE, "• comms peek launched (pid 2)")])
        peeks = ua.collect(self.dir, SINCE, UNTIL)["peeks"]
        self.assertEqual(peeks["launched"], 1)
        self.assertIsNone(peeks["cost_usd"])
        self.assertFalse(peeks["cost_known"])
        self.assertIn("the cost is unknown", peeks["cost_note"])

    def test_the_cost_note_is_present_even_when_the_log_is_unreadable(self):
        peeks = ua.collect(self.dir, SINCE, UNTIL)["peeks"]
        self.assertIn("error", peeks)
        self.assertIsNone(peeks["cost_usd"])
        self.assertNotIn("launched", peeks)


class TwoWritersInMetrics(Base):

    def test_only_the_daemons_own_turn_rows_are_counted(self):
        """`metrics.jsonl` has two writers: `presence._append_turn_metrics` (one row per warm turn)
        and the Observability advisor, prompt-side. `cockpit/server/readers.py` counts every row
        there with no filter at all — that is a known bug and this block must not reproduce it."""
        self.write_jsonl("metrics.jsonl", [
            {"ts": iso(INSIDE), "writer": "daemon", "session_id": "s1", "cost_usd": 1.0},
            {"ts": iso(INSIDE), "writer": "advisor", "session_id": "s1", "cost_usd": 500.0},
            {"ts": iso(INSIDE), "session_id": "s1", "cost_usd": 500.0},  # no writer at all
        ])
        block = ua.warm_block(self.dir, SINCE, UNTIL)
        self.assertEqual(block["turns"], 1)
        self.assertEqual(block["cost_usd"], 1.0)


class TheWholeSnapshot(Base):

    def test_every_block_names_its_source(self):
        """A field whose source is not on the row is a field you cannot check later."""
        snap = ua.collect(self.dir, SINCE, UNTIL)
        for name, block in snap.items():
            with self.subTest(block=name):
                self.assertTrue(block.get("source"), f"{name} does not name its source")

    def test_the_analysable_row_that_was_asked_for(self):
        """*"74%, and in the hour before it there were 3 jobs running, 12 warm turns, 11 comms
        peeks, and the warm session was at 170k context."* End to end, from five real sources."""
        for i in range(3):
            self.write_job(f"run{i}", status="running", started=BEFORE)
        self.write_jsonl("metrics.jsonl", [
            {"ts": iso(INSIDE), "writer": "daemon", "session_id": "s1", "context_tokens": 170000,
             "cost_usd": 0.1, "tokens": 5} for _ in range(12)])
        self.write_jsonl("governor-ledger.jsonl", [
            {"ts": iso(INSIDE), "kind": "tokens", "billable_tokens": 1000,
             "basis": "input_token_equivalent_v1"}])
        self.write_log([(INSIDE, "• comms peek launched (pid %d)" % i) for i in range(11)])
        snap = ua.collect(self.dir, SINCE, UNTIL,
                          daemon={"pid": 42, "started_at": iso(BEFORE)})
        self.assertEqual(snap["jobs"]["running_count"], 3)
        self.assertEqual(snap["warm"]["turns"], 12)
        self.assertEqual(snap["warm"]["context_tokens_last"], 170000)
        self.assertEqual(snap["peeks"]["launched"], 11)
        self.assertEqual(snap["governor"]["billable_tokens"], 1000)
        self.assertFalse(snap["daemon"]["restarted_in_interval"])


if __name__ == "__main__":
    unittest.main()
