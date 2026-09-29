#!/usr/bin/env python3
"""Tests for Dream's step ledger — `dream_steps.py`.

**The load-bearing test is `TheOutage.test_the_eighteen_day_gap_is_caught`.** It replays the shape
this module exists for: the local RAG index last written, then eighteen days in which nothing
crashed, Dream ran nightly, and its Run Log recorded the deferral in prose (*"deferred to the next
full Dream: RAG (2b) …"*). If that test can be deleted without anything else going red, this module
has no reason to exist.

Families guarding the ways this alarm could fail:

* `TheOutage` — it fires on the real thing.
* `NotAnAlarmThatGetsMuted` — it does NOT fire on a fresh install, and never on a step nothing can
  stamp. An alarm that cries wolf daily trains the deafness it was built to cure, so the failure
  mode "too loud" is as much a bug here as "too quiet".
* `SilentForever` — the ways it could go quiet and stay quiet, which is the original disease. A
  deleted ledger, a corrupt one, a skip that reads like a run.
* `AReasonedSkipIsNotACrash` — two deliberate `--skip` stamps with a reason on file age the step
  past its window (correct), and the nudge must say skipped-N-nights-because rather than "stopped
  running" (which sends the reader hunting a crash that does not exist).
* `TheLedgerIsWrittenByTheWorkers` / `PerStepGrace` — the owners really stamp, and a step's grace
  window starts when that step is first seen.

The owner's zone and identity are patched (UTC-5, default day boundary) so the activity-day wording
never depends on the runner's own timezone or on a real `persona/identity.json`.

Stdlib unittest only. Run:  python -m unittest seneschal.scripts.test_dream_steps
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import clock  # noqa: E402
import dream_steps as ds  # noqa: E402
import tz_common  # noqa: E402

OWNER_ZONE = timezone(timedelta(hours=-5))

# The outage shape: last good write, then eighteen-plus days of prose deferrals.
LAST_RAG_WRITE = datetime(2026, 1, 1, 3, 9, 14, tzinfo=timezone.utc)
FOUND_ON = datetime(2026, 1, 19, 10, 0, 0, tzinfo=timezone.utc)

# A reasoned-skip case on step 2g (max 2 days): last ok, then two skips stamped ~22:16 local on
# consecutive nights (03:16Z the next UTC day — the activity-day rule bites here).
LAST_2G_OK = datetime(2026, 1, 10, 3, 33, 41, tzinfo=timezone.utc)
SKIP_2G_1 = datetime(2026, 1, 11, 3, 15, 0, tzinfo=timezone.utc)
SKIP_2G_2 = datetime(2026, 1, 12, 3, 16, 2, tzinfo=timezone.utc)
NAGGED_2G = datetime(2026, 1, 12, 3, 38, 0, tzinfo=timezone.utc)
REASON_2G = ("backup target volume is mid-migration and read-only tonight; copying would fail on "
             "every file and leave half-written .part files behind, so the sweep was refused on "
             "purpose until the volume is back. Nothing in state/ was touched; the prior copies "
             "stand. This sentence exists only to push the note past the nudge cap.")


class _Case(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        z = mock.patch.object(tz_common, "_zone", return_value=OWNER_ZONE)
        i = mock.patch.object(clock, "load_identity", return_value={})
        z.start()
        i.start()
        self.addCleanup(z.stop)
        self.addCleanup(i.stop)

    def _row(self, step, now=None):
        return next(r for r in ds.report(self.d, now) if r["step"] == step)


class TheOutage(_Case):
    """Eighteen days, nothing crashed, nobody noticed."""

    def test_the_eighteen_day_gap_is_caught(self):
        ds.record(self.d, "2b", now=LAST_RAG_WRITE)
        row = self._row("2b", FOUND_ON)
        self.assertGreater(row["age_days"], 18)
        self.assertTrue(row["overdue"])

    def test_the_gap_reaches_the_nudge_text(self):
        """A nudge that says "some steps are stale" gets acknowledged and not acted on."""
        ds.record(self.d, "2b", now=LAST_RAG_WRITE)
        text = ds.summary_line(ds.stale(self.d, FOUND_ON))
        self.assertIn("2b", text)
        self.assertIn("RAG index ingest", text)
        self.assertIn("18d", text)

    def test_the_worst_step_leads(self):
        """`stale()` is what an escalation says, so the most alarming row has to come first."""
        ds.record(self.d, "2g", now=FOUND_ON - timedelta(days=8))
        ds.record(self.d, "2b", now=LAST_RAG_WRITE)
        self.assertEqual(ds.stale(self.d, FOUND_ON)[0]["step"], "2b")

    def test_a_working_index_raises_nothing(self):
        for step in ("2b", "2g", "rollup"):
            ds.record(self.d, step, now=FOUND_ON)
        self.assertEqual(ds.stale(self.d, FOUND_ON), [])


class NotAnAlarmThatGetsMuted(_Case):
    """Too loud is as much a bug as too quiet."""

    def test_a_fresh_install_does_not_alarm(self):
        """A ledger created five minutes ago knows nothing about anything. Alarming here would
        make the first thing anyone learns about this alarm be how to ignore it."""
        ds.ensure_ledger(self.d)
        self.assertEqual(ds.stale(self.d), [])

    def test_but_a_fresh_install_alarms_once_it_is_old_enough(self):
        """The grace window is a delay, not an exemption — otherwise a step that never once ran
        would be permanently invisible."""
        ds.ensure_ledger(self.d, now=FOUND_ON - timedelta(days=30))
        steps = {r["step"] for r in ds.stale(self.d, FOUND_ON)}
        self.assertIn("2b", steps)

    def test_an_unowned_step_is_never_alarmed_on(self):
        """2, 2c and 2d have no owning script, so nothing can ever clear them. Alarming would mean
        a permanent daily nudge nobody can act on."""
        ds.ensure_ledger(self.d, now=FOUND_ON - timedelta(days=365))
        alarmed = {r["step"] for r in ds.stale(self.d, FOUND_ON)}
        for step in ("2", "2c", "2d"):
            self.assertNotIn(step, alarmed)

    def test_but_an_unowned_step_is_still_VISIBLE(self):
        """Its gap is real and belongs in `status`; only the alarm is suppressed."""
        shown = {r["step"] for r in ds.report(self.d)}
        self.assertIn("2c", shown)
        self.assertFalse(self._row("2c")["measured"])

    def test_every_declared_owner_exists_in_this_directory(self):
        """An `owner` is a claim that a script calls `record`. A name that points at nothing can
        never stamp, so the step would read `never` forever — the false alarm this guards."""
        for step, meta in ds.STEPS.items():
            if meta["owner"]:
                self.assertTrue(os.path.isfile(os.path.join(SCRIPT_DIR, meta["owner"])),
                                f"{step}: owner {meta['owner']} missing")

    def test_the_nudge_is_once_per_day(self):
        self.assertIsNone(ds.last_nudged(self.d))
        ds.mark_nudged(self.d, "2026-01-19")
        self.assertEqual(ds.last_nudged(self.d), "2026-01-19")


class AReasonedSkipIsNotACrash(_Case):
    """A step the operator refused ON PURPOSE two nights running, reason on file, must not be
    reported as "stopped running" — that wording sends the reader looking for a crash."""

    def _two_skips(self):
        ds.record(self.d, "2g", now=LAST_2G_OK)
        ds.record(self.d, "2g", ok=False, note=REASON_2G, now=SKIP_2G_1)
        ds.record(self.d, "2g", ok=False, note=REASON_2G, now=SKIP_2G_2)

    def test_the_age_still_climbs_and_the_step_is_still_overdue(self):
        """A skip must cost something: the WORDING changes, never the alarm."""
        self._two_skips()
        row = self._row("2g", NAGGED_2G)
        self.assertTrue(row["overdue"])
        self.assertEqual(row["consecutive_skips"], 2)
        self.assertGreater(row["age_days"], 2)
        self.assertEqual([r["step"] for r in ds.stale(self.d, NAGGED_2G)], ["2g"])

    def test_three_silences_are_told_apart(self):
        ds.record(self.d, "2b", now=LAST_RAG_WRITE)            # ran, then nothing at all
        self._two_skips()                                        # ran, then refused with a reason
        rows = {r["step"]: r for r in ds.report(self.d, NAGGED_2G)}
        self.assertEqual(ds.skip_state(rows["2b"]), "stopped")
        self.assertEqual(ds.skip_state(rows["2g"]), "skipped")
        self.assertEqual(ds.skip_state(rows["rollup"]), "never")

    def test_the_nudge_names_the_skip_count_the_night_and_the_reason(self):
        self._two_skips()
        line = ds.summary_line(ds.stale(self.d, NAGGED_2G))
        self.assertIn("2g state backups (2d; skipped 2 nights, last 2026-01-11: ", line)
        self.assertIn("mid-migration and read-only tonight", line)
        self.assertNotIn("stopped", line)

    def test_the_skip_night_is_the_owner_local_night_not_the_utc_day(self):
        """03:16Z on the 12th is 22:16 local on the 11th — the run-log entry the reader is sent to
        is filed under the 11th."""
        self._two_skips()
        self.assertIn("last 2026-01-11", ds.describe(self._row("2g", NAGGED_2G)))

    def test_the_reason_is_capped_and_the_cap_is_visible(self):
        self._two_skips()
        text = ds.describe(self._row("2g", NAGGED_2G))
        self.assertNotIn("push the note past the nudge cap", text)
        self.assertIn("…", text)
        self.assertLess(len(text), ds.REASON_CAP + 120)

    def test_a_skip_with_no_reason_says_so_rather_than_inventing_one(self):
        ds.record(self.d, "2g", now=LAST_2G_OK)
        ds.record(self.d, "2g", ok=False, now=SKIP_2G_2)
        self.assertIn("skipped 1 night, last 2026-01-11 (no reason recorded)",
                      ds.describe(self._row("2g", NAGGED_2G)))

    def test_the_headline_says_skipped_by_policy_when_every_row_is_a_skip(self):
        self._two_skips()
        text = ds.nudge_text(ds.stale(self.d, NAGGED_2G))
        self.assertTrue(text.startswith("Dream steps skipped by policy and now overdue"))
        self.assertIn("not a crash", text)
        self.assertIn("state/run-log.md", text)
        self.assertNotIn("stopped running", text)

    def test_the_headline_still_says_stopped_running_for_a_silent_stop(self):
        """The eighteen-day shape keeps the alarming wording — that sentence is right for it."""
        ds.record(self.d, "2b", now=LAST_RAG_WRITE)
        text = ds.nudge_text(ds.stale(self.d, FOUND_ON))
        self.assertTrue(text.startswith("Dream steps have stopped running: "))
        self.assertNotIn("skipped", text)

    def test_a_mixed_list_gets_the_neutral_headline_and_per_step_detail(self):
        ds.record(self.d, "2b", now=NAGGED_2G - timedelta(days=9))
        self._two_skips()
        text = ds.nudge_text(ds.stale(self.d, NAGGED_2G))
        self.assertTrue(text.startswith("Dream steps overdue: "))
        self.assertIn("2b RAG index ingest (9d)", text)
        self.assertIn("2g state backups (2d; skipped 2 nights", text)

    def test_a_run_after_the_skips_reads_as_a_run_again(self):
        """`record(ok)` zeroes the counter, so the next clean night drops the skip clause."""
        self._two_skips()
        ds.record(self.d, "2g", now=NAGGED_2G + timedelta(days=1))
        self.assertEqual(ds.skip_state(self._row("2g", NAGGED_2G + timedelta(days=1))), "stopped")

    def test_status_prints_the_skip_beneath_the_overdue_row(self):
        import contextlib
        import io
        # `status` reads the wall clock, so the stamps are relative to it here.
        now = datetime.now(timezone.utc)
        ds.record(self.d, "2g", now=now - timedelta(days=3))
        ds.record(self.d, "2g", ok=False, note=REASON_2G, now=now - timedelta(days=2))
        ds.record(self.d, "2g", ok=False, note=REASON_2G, now=now - timedelta(days=1))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ds.main(["--state-dir", self.d, "status"])
        out = buf.getvalue()
        self.assertIn("OVERDUE", out)
        self.assertIn("skipped 2 nights, last ", out)
        self.assertIn("backup target volume is mid-migration", out)


class SilentForever(_Case):
    """Every way this could go quiet and stay quiet — the original disease."""

    def test_a_skip_never_reads_as_a_run(self):
        """The whole eighteen days were a step being skipped and saying so in prose."""
        ds.record(self.d, "2b", now=LAST_RAG_WRITE)
        for _ in range(5):
            ds.record(self.d, "2b", ok=False, note="scoped Dream, deferred to next full Dream")
        row = self._row("2b", FOUND_ON)
        self.assertTrue(row["overdue"])
        self.assertEqual(row["consecutive_skips"], 5)

    def test_a_run_clears_the_skip_counter(self):
        ds.record(self.d, "2b", ok=False)
        ds.record(self.d, "2b", now=FOUND_ON)
        self.assertEqual(self._row("2b", FOUND_ON)["consecutive_skips"], 0)

    def test_a_deleted_ledger_does_not_silence_the_alarm_forever(self):
        """Without `_created` nothing is ever overdue. `ensure_ledger` restarts the clock the
        first time anyone looks, so a wiped `state/` costs a grace window, not the alarm."""
        path = os.path.join(self.d, ds.LEDGER_FILE)
        if os.path.exists(path):
            os.remove(path)
        ds.ensure_ledger(self.d, now=FOUND_ON - timedelta(days=30))
        self.assertIn("2b", {r["step"] for r in ds.stale(self.d, FOUND_ON)})

    def test_ensure_ledger_does_not_reset_an_existing_birthday(self):
        """Otherwise every daily check would restart the grace window and nothing would ever
        become overdue — a clock that resets is a clock that never rings."""
        ds.ensure_ledger(self.d, now=FOUND_ON - timedelta(days=30))
        ds.ensure_ledger(self.d, now=FOUND_ON)
        self.assertIn("2b", {r["step"] for r in ds.stale(self.d, FOUND_ON)})

    def test_a_corrupt_ledger_reads_empty_rather_than_raising(self):
        pathlib.Path(self.d, ds.LEDGER_FILE).write_text("{ not json", encoding="utf-8")
        self.assertEqual(len(ds.report(self.d)), len(ds.STEPS))

    def test_a_corrupt_ledger_can_be_rebuilt(self):
        pathlib.Path(self.d, ds.LEDGER_FILE).write_text("[1,2,3]", encoding="utf-8")
        ds.record(self.d, "2b", now=FOUND_ON)
        self.assertFalse(self._row("2b", FOUND_ON)["overdue"])

    def test_recording_never_raises(self):
        """A failed stamp costs the row, never the run that earned it."""
        ds.record("Z:\\definitely\\not\\a\\path", "2b")
        ds.record(self.d, "2b", note=None)
        ds.record(self.d, None)          # type: ignore[arg-type]
        ds.record(self.d, "2b", note="x" * 5000)

    def test_an_unknown_step_is_kept_not_dropped(self):
        """A step the running Dream has but `STEPS` does not is a fact worth having on disk;
        discarding it would rebuild the exact blind spot this module closes."""
        ds.record(self.d, "2z", now=FOUND_ON)
        raw = json.loads(pathlib.Path(self.d, ds.LEDGER_FILE).read_text(encoding="utf-8"))
        self.assertIn("2z", raw)

    def test_the_write_is_atomic_not_truncating(self):
        """`state/` files are gitignored and exist nowhere else; a truncate-write that dies leaves
        no history to recover."""
        ds.record(self.d, "2b", now=FOUND_ON)
        leftovers = [p for p in os.listdir(self.d) if p.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TheLedgerIsWrittenByTheWorkers(_Case):
    """The record is a side effect of doing the work, not a promise to remember it. This asserts
    the seam each worker calls, so removing the call breaks a test rather than quietly re-opening
    the gap."""

    def test_rag_index_exposes_the_stamp_seam(self):
        import rag_index
        self.assertTrue(callable(rag_index.stamp_dream_step))

    def test_rag_index_stamps_2b_against_its_db_directory(self):
        import rag_index
        db = os.path.join(self.d, "rag-index.sqlite")
        rag_index.stamp_dream_step("2b", db)
        self.assertIsNotNone(self._row("2b")["last_ok"])

    def test_salience_rollup_stamps_rollup_against_its_db_directory(self):
        import salience_rollup
        salience_rollup._stamp_dream_step("rollup", os.path.join(self.d, "rag-index.sqlite"))
        self.assertIsNotNone(self._row("rollup")["last_ok"])

    def test_state_backup_stamps_2g(self):
        import state_backup
        state_backup._stamp_dream_step(self.d)
        self.assertIsNotNone(self._row("2g")["last_ok"])

    def test_a_REAL_rag_index_run_stamps_2b(self):
        """**Testing the seam is not testing the caller.** A helper can be perfect and never
        invoked. So this drives `rag_index.main()` end to end with a stubbed embedder (no Ollama,
        no network) and asserts the ledger row appears. Delete the call in `main` and this goes
        red; delete only the unit tests above and nothing does.

        **Assert `last_ok` is SET, never `overdue is False`.** The negative form passes with the
        call deleted: with no ledger written at all there is no `_created` stamp, so no step is
        overdue and the assertion is satisfied by the absence of everything. A guard whose passing
        condition includes "nothing happened" guards nothing."""
        import rag_common as rc
        import rag_index

        real = rc.embed_texts
        rc.embed_texts = lambda texts, cfg=None, **_: [[1.0, 0.0, 0.0, 0.0] for _ in texts]
        src = pathlib.Path(self.d, "notes.jsonl")
        src.write_text(json.dumps({"source": "journal", "ref": "2026-01-19",
                                   "text": "a real record to embed"}) + "\n", encoding="utf-8")
        try:
            rc_code = rag_index.main(["--db", os.path.join(self.d, "rag-index.sqlite"),
                                      "--ingest", str(src)])
        finally:
            rc.embed_texts = real
        self.assertEqual(rc_code, 0)
        self.assertIsNotNone(self._row("2b")["last_ok"])

    def test_a_DRY_RUN_does_not_stamp(self):
        """A dry run reports; it does not do the work, so it must not clear the alarm."""
        import rag_common as rc
        import rag_index

        real = rc.embed_texts
        rc.embed_texts = lambda texts, cfg=None, **_: [[1.0, 0.0, 0.0, 0.0] for _ in texts]
        src = pathlib.Path(self.d, "notes.jsonl")
        src.write_text(json.dumps({"source": "journal", "ref": "r", "text": "t"}) + "\n",
                       encoding="utf-8")
        try:
            rag_index.main(["--db", os.path.join(self.d, "rag-index.sqlite"), "--dry-run",
                            "--ingest", str(src)])
        finally:
            rc.embed_texts = real
        self.assertIsNone(self._row("2b")["last_ok"])


class PerStepGrace(_Case):
    """A never-run step is judged against its OWN first-seen stamp, not the ledger's. A step
    added to `STEPS` long after the ledger was created must not read OVERDUE within hours — a
    two-hour-old step cannot be two days stale. `2g`'s `max_age_days` is 2."""

    def test_a_first_seen_step_is_not_overdue_inside_its_own_grace(self):
        ds.ensure_ledger(self.d, now=FOUND_ON)
        row = self._row("2g", FOUND_ON + timedelta(hours=2))
        self.assertFalse(row["overdue"])

    def test_a_first_seen_step_is_overdue_once_its_own_max_age_has_passed(self):
        ds.ensure_ledger(self.d, now=FOUND_ON)
        row = self._row("2g", FOUND_ON + timedelta(days=3))
        self.assertTrue(row["overdue"])

    def test_migration_backfills_an_existing_step_from_the_ledger_birthday_not_today(self):
        """A ledger-level `_created` from long ago, and a step row with no per-step `created` at
        all. Stamping that row with TODAY would hand a step that has been watched for 33 days a
        fresh two-day grace window — real staleness would then go quiet for days, which is an
        outage of the alarm this is meant to protect, not a fix."""
        old_born = FOUND_ON - timedelta(days=33)
        with open(ds._path(self.d), "w", encoding="utf-8") as fh:
            json.dump({
                ds.CREATED_KEY: {"at": old_born.isoformat(timespec="seconds")},
                "2b": {"last_skip": old_born.isoformat(timespec="seconds"),
                       "consecutive_skips": 1},
            }, fh)
        ds.ensure_ledger(self.d, now=FOUND_ON)
        raw = json.loads(pathlib.Path(ds._path(self.d)).read_text(encoding="utf-8"))
        self.assertEqual(raw["2b"]["created"], old_born.isoformat(timespec="seconds"))
        # And it follows through to the alarm: a step watched 33 days with no successful run is
        # overdue against its own (inherited) birthday.
        self.assertTrue(self._row("2b", FOUND_ON)["overdue"])

    def test_a_genuinely_new_step_is_not_backdated_to_the_ledger_birthday(self):
        """The other half of the same migration: a step's row is simply ABSENT (never recorded,
        never even seen) even though the ledger itself is old. That step's birthday is `now` — the
        moment anyone first looks — never the ledger's."""
        old_born = FOUND_ON - timedelta(days=33)
        with open(ds._path(self.d), "w", encoding="utf-8") as fh:
            json.dump({ds.CREATED_KEY: {"at": old_born.isoformat(timespec="seconds")}}, fh)
        ds.ensure_ledger(self.d, now=FOUND_ON)
        raw = json.loads(pathlib.Path(ds._path(self.d)).read_text(encoding="utf-8"))
        self.assertEqual(raw["2g"]["created"], FOUND_ON.isoformat(timespec="seconds"))
        self.assertFalse(self._row("2g", FOUND_ON + timedelta(hours=2))["overdue"])

    def test_a_missing_ledger_still_eventually_alarms_a_never_run_step(self):
        """Per-step stamping must not reopen the silent-forever hole. No ledger file exists at all
        yet — `ensure_ledger` must still give every step a real birthday so a step that never once
        runs cannot hide behind a ledger that was never written."""
        self.assertFalse(os.path.exists(ds._path(self.d)))
        ds.ensure_ledger(self.d, now=FOUND_ON - timedelta(days=10))
        self.assertIn("2g", {r["step"] for r in ds.stale(self.d, FOUND_ON)})

    def test_a_step_present_but_stamped_before_this_shipped_falls_back_to_the_ledger_born(self):
        """Defence in depth: even a row that carries neither `created` nor a `last_ok` (bypassing
        `ensure_ledger` entirely, e.g. `report()` called directly against a hand-edited file) must
        fall back to the ledger's own birthday rather than never alarm."""
        old_born = FOUND_ON - timedelta(days=33)
        with open(ds._path(self.d), "w", encoding="utf-8") as fh:
            json.dump({ds.CREATED_KEY: {"at": old_born.isoformat(timespec="seconds")},
                       "2g": {}}, fh)
        self.assertTrue(self._row("2g", FOUND_ON)["overdue"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
