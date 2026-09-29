#!/usr/bin/env python3
"""Tests for ``ack.py`` — the one-call front door for acking reminders.

**What is load-bearing here is the refusal**, not the happy path. An ack written to the wrong reminder
row is a *false record*, and the evening Wrap counts ``Last Acknowledged`` as truth — so a resolver
that guesses is worse than no resolver:

* **``AmbiguityRefuses``** — a phrase fitting a required AND an optional variant of the same habit
  must refuse and NAME both candidates; a resolver that silently picks the likelier row lands the ack
  on the wrong one.
* **``ClockDecidesAmPm``** — a paired row resolves off the owner's clock with **both sides pinned**;
  the injected instant is **naive owner-local wall time**, so the case is runner-independent.
* **``PartialFailure``** — one good phrase and one bad one exits non-zero and says **which** failed.
* **``TitleFallbackForTodoRows``** — stage 2. The pairing that matters is a phrase that resolves
  cleanly while the second matching row is ``Finished`` and refuses the moment it is active, so the
  status filter is proven load-bearing. No test spawns a lookup — the runner is injected.
* **``FilesystemBackend``** — on a non-Notion store the resolve + dequeue + ledger still run, the
  outbox step is skipped (no journal rows), and stage 2 declines without spawning anything.

Fixtures are built here, never read from the live tree. The **shipped** seed
(``reminder-aliases.example.json``) is exercised separately (``ShippedExample``) for shape and safety.

The owner's zone is patched to a fixed UTC-5 with the default 05:00 day boundary, so the dequeue's
activity-day scoping of a ``…Z`` ``due_at`` never depends on the runner's timezone.

Run:  python -m unittest seneschal.scripts.test_ack   (or)  python test_ack.py
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import ack  # noqa: E402
import clock  # noqa: E402
import outbox_common as ob  # noqa: E402
import reminders_acks as ra  # noqa: E402
import reminders_live as rl  # noqa: E402
import tz_common  # noqa: E402

OWNER_ZONE = timezone(timedelta(hours=-5))


def pid(n: int) -> str:
    """A placeholder-shaped page id (CI allows only ``00000000-…`` UUIDs in the tree)."""
    return f"00000000-0000-0000-0000-{n:012d}"


PID_MORNING_REQ = pid(1)
PID_EVENING_REQ = pid(2)
PID_EVENING_OPT = pid(3)
PID_CAT_AM = pid(4)
PID_CAT_PM = pid(5)
PID_TIMESHEET = pid(6)
PID_RELEASE = pid(7)
PID_ARCHIVE = pid(8)

#: A miniature id cache in the live file's real shape — the table ``reminders_acks.id_cache_titles``
#: parses by shape, header and separator included so the parser's row recognition is exercised.
ID_CACHE = f"""# Reminders — row-ID cache (live table)

| Reminder | Page ID | Type | Window | Cadence | Importance |
|----------|---------|------|--------|---------|------------|
| Morning stretch — required | `{PID_MORNING_REQ}` | Recurring Habit | Morning | Daily | Critical |
| Evening stretch — required | `{PID_EVENING_REQ}` | Recurring Habit | Evening | Daily | Critical |
| Evening stretch — optional | `{PID_EVENING_OPT}` | Recurring Habit | Evening | Daily | Notable |
| Feed the cat (AM) | `{PID_CAT_AM}` | Recurring Habit | Morning | Daily | High |
| Feed the cat (PM) | `{PID_CAT_PM}` | Recurring Habit | Evening | Daily | High |
| Submit timesheet | `{PID_TIMESHEET}` | Recurring Habit | Evening | Weekly | Critical |
"""

ALIASES = {
    "version": 1,
    "side_words": {"am": ["morning", "am"], "pm": ["evening", "night", "tonight", "pm"]},
    "rows": [
        {"title": "Morning stretch — required",
         "aliases": ["morning req stretch", "morning stretch", "stretch"]},
        {"title": "Evening stretch — required",
         "aliases": ["evening req stretch", "evening stretch", "stretch"]},
        {"title": "Evening stretch — optional",
         "aliases": ["optional stretch", "evening stretch", "stretch"]},
        {"title": "Submit timesheet", "aliases": ["timesheet"]},
    ],
    "clock_pairs": [
        {"am": "Feed the cat (AM)", "pm": "Feed the cat (PM)", "pm_from": "12:00",
         "aliases": ["fed the cat", "cat fed", "cat"]},
    ],
}

#: Stage 2's fixture: the rows a live read would return. The two release rows are the pairing the
#: status filter is tested on — one active, one ``Finished``.
LIVE_ROWS = [
    ("Open the release-notes PR", PID_RELEASE, "Pending"),
    ("Archive the release notes", PID_ARCHIVE, "Finished"),
    ("Submit timesheet", PID_TIMESHEET, "Pending"),
    ("Evening stretch — required", PID_EVENING_REQ, "Reminded"),
]

#: Naive owner-local wall clock — see the module docstring.
EVENING = datetime(2026, 8, 9, 18, 33)
MORNING = datetime(2026, 8, 9, 8, 5)
DATE = "2026-08-09"
COLLECTION = "collection://" + "-".join(("1" * 8, "2" * 4, "3" * 4, "4" * 4, "5" * 12))


class AckBase(unittest.TestCase):
    BACKEND = "notion"

    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.refs = tempfile.mkdtemp()
        with open(os.path.join(self.state, "reminders-id-cache.md"), "w", encoding="utf-8") as fh:
            fh.write(ID_CACHE)
        self.write_refs(ALIASES)
        for target, name, value in (
                (tz_common, "_zone", mock.MagicMock(return_value=OWNER_ZONE)),
                (clock, "load_identity", mock.MagicMock(return_value={})),
                (rl, "store_backend", mock.MagicMock(return_value=self.BACKEND)),
                (rl, "reminders_collection", mock.MagicMock(return_value=COLLECTION))):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_refs(self, aliases, name=ack.ALIASES_FILE):
        with open(os.path.join(self.refs, name), "w", encoding="utf-8") as fh:
            json.dump(aliases, fh)

    def run_ack(self, *argv, now=EVENING, runner=None, ack_date=True):
        """Invoke the CLI with the fixture dirs; return ``(rc, parsed-final-JSON, all-output)``."""
        buf = io.StringIO()
        date = ["--ack-date", DATE] if ack_date else []
        with redirect_stdout(buf):
            rc = ack.main(["--state-dir", self.state, "--references-dir", self.refs, *date, *argv],
                          now=now, runner=runner)
        out = buf.getvalue()
        return rc, json.loads(out.strip().splitlines()[-1]), out

    def resolve(self, phrase, now=EVENING, live_rows=None):
        return ack.resolve(phrase, ALIASES, ack.title_index(self.state), now, live_rows=live_rows)

    def outbox_rows(self):
        if not os.path.exists(ob.db_path(self.state)):
            return []
        conn = ob.connect(self.state)
        try:
            return [ob._row_to_dict(r) for r in conn.execute("SELECT * FROM outbox")]
        finally:
            conn.close()

    def ack_rows(self):
        return [r for r in self.outbox_rows() if r["op"] == "ack_reminder"]

    def write_queue(self, queue):
        with open(os.path.join(self.state, "reminders.json"), "w", encoding="utf-8") as fh:
            json.dump(queue, fh)

    def read_queue(self):
        with open(os.path.join(self.state, "reminders.json"), encoding="utf-8") as fh:
            return json.load(fh)


class AmbiguityRefuses(AckBase):
    """An ambiguous phrase refuses and names what it was torn between."""

    def test_evening_stretch_refuses_and_names_both_rows(self):
        res = self.resolve("evening stretch")
        self.assertFalse(res["ok"])
        self.assertIn("ambiguous", res["error"])
        self.assertEqual(res["candidates"],
                         ["Evening stretch — optional", "Evening stretch — required"])
        self.assertIsNone(res["reminder_id"])

    def test_bare_stretch_refuses_across_all_three_rows(self):
        res = self.resolve("did my stretch")
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["candidates"]), 3)

    def test_longest_alias_wins_so_a_qualified_phrase_is_not_ambiguous(self):
        """'evening req stretch' contains 'stretch' and 'evening stretch' too — specificity wins."""
        res = self.resolve("Evening req stretch done!")
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["title"], "Evening stretch — required")
        self.assertEqual(res["matched_alias"], "evening req stretch")

    def test_ambiguous_phrase_writes_nothing_and_exits_nonzero(self):
        rc, summary, out = self.run_ack("evening stretch")
        self.assertEqual(rc, 3)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["refused"], ["evening stretch"])
        self.assertEqual(self.outbox_rows(), [])
        self.assertIn("Evening stretch — optional", out)  # candidates printed for a human too

    def test_unknown_phrase_refuses_without_candidates(self):
        res = self.resolve("took'em")
        self.assertFalse(res["ok"])
        self.assertIn("no alias matches", res["error"])
        self.assertEqual(res["candidates"], [])


class ClockDecidesAmPm(AckBase):
    """'fed the cat' resolves off the owner's clock — both sides pinned, instant injected."""

    def test_evening_resolves_to_pm(self):
        res = self.resolve("fed the cat", now=EVENING)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual((res["title"], res["side"]), ("Feed the cat (PM)", "pm"))
        self.assertEqual(res["reminder_id"], PID_CAT_PM)

    def test_morning_resolves_to_am(self):
        res = self.resolve("fed the cat", now=MORNING)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual((res["title"], res["side"]), ("Feed the cat (AM)", "am"))
        self.assertEqual(res["reminder_id"], PID_CAT_AM)

    def test_the_cutover_minute_itself_is_pm(self):
        """Pinning the boundary, not just the two sides — '>=' is the declared rule."""
        self.assertEqual(self.resolve("cat fed", now=datetime(2026, 8, 9, 12, 0))["title"],
                         "Feed the cat (PM)")
        self.assertEqual(self.resolve("cat fed", now=datetime(2026, 8, 9, 11, 59))["title"],
                         "Feed the cat (AM)")

    def test_an_aware_instant_is_read_on_the_owners_clock(self):
        """17:30Z is 12:30 in the owner's UTC-5 zone — PM, whatever the runner's own zone is."""
        aware = datetime(2026, 8, 9, 17, 30, tzinfo=timezone.utc)
        self.assertEqual(self.resolve("cat fed", now=aware)["title"], "Feed the cat (PM)")
        aware = datetime(2026, 8, 9, 16, 30, tzinfo=timezone.utc)  # 11:30 owner-local
        self.assertEqual(self.resolve("cat fed", now=aware)["title"], "Feed the cat (AM)")

    def test_the_chosen_side_is_stated_in_the_output(self):
        rc, _, out = self.run_ack("--dry-run", "fed the cat", now=EVENING)
        self.assertEqual(rc, 0)
        self.assertIn("Feed the cat (PM)", out)
        self.assertIn("evening", out)
        self.assertIn("18:33", out)

    def test_a_phrase_naming_the_other_side_refuses_rather_than_letting_the_clock_win(self):
        """'fed the cat this morning' at 18:33 must not write the PM row."""
        res = self.resolve("fed the cat this morning", now=EVENING)
        self.assertFalse(res["ok"])
        self.assertEqual(res["candidates"], ["Feed the cat (AM)", "Feed the cat (PM)"])

    def test_a_phrase_agreeing_with_the_clock_still_resolves(self):
        res = self.resolve("fed the cat this evening", now=EVENING)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["title"], "Feed the cat (PM)")


class WritesReachTheRealMachinery(AckBase):
    """The downstream writes actually land, through the reused modules rather than a copy."""

    def test_ack_is_enqueued_with_the_shared_payload_and_key(self):
        self.run_ack("timesheet")
        rows = self.ack_rows()
        self.assertEqual(len(rows), 1)
        payload = rows[0]["payload"]
        self.assertEqual(payload["status"], "Done")
        self.assertEqual(payload["last_acknowledged"], DATE)
        self.assertEqual(payload["consecutive_misses"], 0)
        self.assertTrue(payload["untick_ack"])
        # the idempotency key is outbox_common's, not a second one minted here
        self.assertEqual(rows[0]["idempotency_key"], ob.ack_key(PID_TIMESHEET, DATE))

    def test_the_written_reminder_id_is_the_canonical_dashed_form(self):
        self.run_ack("timesheet")
        self.assertEqual(self.ack_rows()[0]["payload"]["reminder_id"], PID_TIMESHEET)

    def test_the_durable_ack_ledger_is_recorded(self):
        """The dequeue's half — the fire-time gate reads acks.json, not the outbox."""
        self.run_ack("timesheet")
        self.assertEqual(ra.load_acks(self.state).get(ra.norm_key(PID_TIMESHEET)), DATE)

    def test_obsolete_queued_nudges_are_dequeued(self):
        self.write_queue([
            {"id": "rmd-1", "reminder_id": PID_TIMESHEET, "text": "Submit timesheet — it's due today.",
             "due_at": "2026-08-09T23:00:00Z", "fired_at": None},
            {"id": "rmd-2", "reminder_id": "other", "text": "unrelated", "fired_at": None}])
        self.run_ack("timesheet")
        self.assertEqual([e["id"] for e in self.read_queue()], ["rmd-2"])

    def test_a_post_midnight_ack_dequeues_the_evening_not_the_next_day(self):
        """01:30 owner-local is still the previous activity day: tonight's nudge goes, tomorrow's
        stays. The ledger stamps the calendar date; the dequeue is scoped by the activity day."""
        self.write_queue([
            # 2026-08-10 01:00Z == 2026-08-09 20:00 owner-local: activity day 08-09
            {"id": "tonight", "reminder_id": PID_TIMESHEET, "due_at": "2026-08-10T01:00:00Z",
             "fired_at": None},
            # 2026-08-10 23:00Z == 2026-08-10 18:00 owner-local: activity day 08-10
            {"id": "tomorrow", "reminder_id": PID_TIMESHEET, "due_at": "2026-08-10T23:00:00Z",
             "fired_at": None}])
        rc, summary, _ = self.run_ack("timesheet", now=datetime(2026, 8, 10, 1, 30), ack_date=False)
        self.assertEqual(rc, 0, summary)
        self.assertEqual([e["id"] for e in self.read_queue()], ["tomorrow"])
        self.assertEqual(summary["results"][0]["ack_date"], "2026-08-10")
        dq = [w for w in summary["results"][0]["writes"] if w["op"] == "reminders_dequeue"][0]
        self.assertEqual(dq["activity_day"], "2026-08-09")

    def test_repeating_the_same_ack_the_same_day_is_a_no_op(self):
        self.run_ack("timesheet")
        rc, _, _ = self.run_ack("timesheet")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.ack_rows()), 1)

    def test_finished_lands_on_its_own(self):
        rc, _, _ = self.run_ack("--status", "Finished", "timesheet")
        self.assertEqual(rc, 0)
        self.assertEqual(self.ack_rows()[0]["payload"]["status"], "Finished")

    def test_a_finished_swallowed_by_a_same_day_done_is_reported_not_silent(self):
        """The outbox dedups on ``ack:<row>:<date>``, which carries no status — so a Finished after a
        same-day Done never lands and the row asked to RETIRE fires again tomorrow. Exit 4, named."""
        self.run_ack("timesheet")
        rc, summary, out = self.run_ack("--status", "Finished", "timesheet")
        self.assertEqual(rc, 4)
        self.assertFalse(summary["ok"])
        self.assertIn("not retired", out)
        self.assertEqual(self.ack_rows()[0]["payload"]["status"], "Done")

    def test_repeating_a_finished_is_still_a_benign_no_op(self):
        self.run_ack("--status", "Finished", "timesheet")
        rc, _, _ = self.run_ack("--status", "Finished", "timesheet")
        self.assertEqual(rc, 0)

    def test_dry_run_writes_nothing(self):
        rc, summary, _ = self.run_ack("--dry-run", "timesheet")
        self.assertEqual(rc, 0)
        self.assertEqual(self.outbox_rows(), [])
        self.assertEqual(ra.load_acks(self.state), {})
        self.assertEqual([w["op"] for w in summary["results"][0]["writes"]],
                         ["outbox.ack", "reminders_dequeue"])


class DeadLetterRevivesRatherThanSwallows(AckBase):
    """A re-armed outbox entry must not make a later, legitimate ack look like a no-op.

    ``outbox_common.enqueue`` revives a dead-lettered entry on a repeat of its key (back to pending,
    drainable, new payload). ``created is False`` alone cannot tell that apart from an ordinary same-day
    repeat with nothing to drain — so ``apply_ack`` must carry ``revived`` through to its caller."""

    def _dead_letter_the_journaled_entry(self):
        rows = self.ack_rows()
        self.assertEqual(len(rows), 1)
        conn = ob.connect(self.state)
        try:
            ob.mark_failed(conn, rows[0]["id"], "404 gone")
        finally:
            conn.close()
        return rows[0]["id"]

    def test_a_dead_lettered_ack_revives_and_the_write_records_it(self):
        self.run_ack("timesheet")
        entry_id = self._dead_letter_the_journaled_entry()
        _, summary, _ = self.run_ack("timesheet")
        write = [w for w in summary["results"][0]["writes"] if w["op"] == "outbox.ack"][0]
        self.assertEqual(write["id"], entry_id)   # the SAME row, re-armed — not a second entry
        self.assertIs(write["created"], False)
        self.assertTrue(write["revived"])

    def test_the_revived_row_is_pending_and_carries_the_new_payload(self):
        self.run_ack("--status", "Finished", "timesheet")
        entry_id = self._dead_letter_the_journaled_entry()
        self.run_ack("timesheet")
        conn = ob.connect(self.state)
        try:
            self.assertEqual(ob.get(conn, entry_id)["status"], ob.PENDING)
            claimed = ob.claim_ready(conn)
        finally:
            conn.close()
        self.assertEqual([c["id"] for c in claimed], [entry_id])
        self.assertEqual(claimed[0]["payload"]["status"], "Done")

    def test_a_genuine_same_day_repeat_reports_no_revival(self):
        self.run_ack("timesheet")
        _, summary, _ = self.run_ack("timesheet")
        write = [w for w in summary["results"][0]["writes"] if w["op"] == "outbox.ack"][0]
        self.assertIs(write["created"], False)
        self.assertFalse(write["revived"])


class PartialFailure(AckBase):
    """A partial success must be legible, never averaged into 'ok'."""

    def test_one_good_one_bad_exits_nonzero_and_names_the_failing_phrase(self):
        rc, summary, out = self.run_ack("fed the cat", "evening stretch")
        self.assertEqual(rc, 3)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["acked"], ["Feed the cat (PM)"])
        self.assertEqual(summary["refused"], ["evening stretch"])
        self.assertIn("evening stretch", out)

    def test_the_good_phrase_still_lands(self):
        """Refusing one phrase must not cost the others."""
        self.run_ack("fed the cat", "evening stretch")
        self.assertEqual([r["payload"]["reminder_id"] for r in self.ack_rows()], [PID_CAT_PM])

    def test_each_phrase_carries_its_own_outcome(self):
        _, summary, _ = self.run_ack("fed the cat", "evening stretch")
        self.assertEqual({r["phrase"]: r["ok"] for r in summary["results"]},
                         {"fed the cat": True, "evening stretch": False})

    def test_no_phrases_is_a_usage_error(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ack.main(["--state-dir", self.state, "--references-dir", self.refs])
        self.assertEqual(rc, 2)


class UnresolvableRow(AckBase):
    """A row the id cache cannot answer for is an error, never a guess and never a store query."""

    def test_a_title_missing_from_the_id_cache_refuses(self):
        with open(os.path.join(self.state, "reminders-id-cache.md"), "w", encoding="utf-8") as fh:
            fh.write("# empty\n")
        res = self.resolve("timesheet")
        self.assertFalse(res["ok"])
        self.assertIn("reminders-id-cache.md", res["error"])

    def test_a_title_listed_twice_refuses_rather_than_picking_the_first(self):
        with open(os.path.join(self.state, "reminders-id-cache.md"), "a", encoding="utf-8") as fh:
            fh.write(f"| Submit timesheet | `{pid(99)}` | Recurring Habit | Evening | Weekly | Critical |\n")
        res = self.resolve("timesheet")
        self.assertFalse(res["ok"])
        self.assertIn("more than once", res["error"])

    def test_an_em_dash_title_matches_a_hyphen_in_the_cache(self):
        """Tolerant reader: a retyped dash must not silently make a row unackable."""
        cache = ID_CACHE.replace("Evening stretch — required", "Evening stretch - required")
        with open(os.path.join(self.state, "reminders-id-cache.md"), "w", encoding="utf-8") as fh:
            fh.write(cache)
        res = self.resolve("evening req stretch")
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["reminder_id"], PID_EVENING_REQ)

    def test_missing_reference_files_are_a_usage_error_not_a_silent_no_match(self):
        os.remove(os.path.join(self.refs, ack.ALIASES_FILE))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ack.main(["--state-dir", self.state, "--references-dir", self.refs, "timesheet"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot load", buf.getvalue())


class TheAliasFileFallsBackToTheSeed(AckBase):
    """The live alias file is per-install and gitignored; absent, the tracked example is read."""

    def test_the_example_is_read_when_the_live_file_is_absent(self):
        os.remove(os.path.join(self.refs, ack.ALIASES_FILE))
        self.write_refs(ALIASES, name=ack.ALIASES_EXAMPLE_FILE)
        rc, summary, _ = self.run_ack("--dry-run", "timesheet")
        self.assertEqual(rc, 0, summary)
        self.assertEqual(summary["aliases_file"], ack.ALIASES_EXAMPLE_FILE)

    def test_the_live_file_wins_over_the_example(self):
        self.write_refs({"rows": []}, name=ack.ALIASES_EXAMPLE_FILE)
        rc, summary, _ = self.run_ack("--dry-run", "timesheet")
        self.assertEqual(rc, 0, summary)
        self.assertEqual(summary["aliases_file"], ack.ALIASES_FILE)


def live_rows(spec=LIVE_ROWS) -> list:
    """``spec`` (title, page id, status) → the row shape ``reminders_live.parse_rows`` produces."""
    return [{"title": t, "key": ra.norm_key(p), "status": s} for t, p, s in spec]


class FakeLive:
    """Stage 2's provider seam: counts its calls, so "at most one lookup per run" is assertable. It
    applies :func:`reminders_live.active_rows` because that is the provider's real contract."""

    def __init__(self, spec=LIVE_ROWS, error=None):
        self.rows = None if error else rl.active_rows(live_rows(spec))
        self.error, self.calls = error, 0

    def __call__(self):
        self.calls += 1
        return self.rows, self.error


class FakeRunner:
    """A ``subprocess.run`` stand-in returning a well-formed live reply. **Spawns nothing.**"""

    class Proc:
        def __init__(self, stdout):
            self.stdout, self.stderr, self.returncode = stdout, "", 0

    def __init__(self, spec=LIVE_ROWS, raises=None):
        self.spec, self.raises, self.calls = spec, raises, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.raises:
            raise self.raises
        return self.Proc(json.dumps({"schema": rl.ROWS_SCHEMA, "rows": [
            {"title": t, "page_id": p, "status": s} for t, p, s in self.spec]}))


class TitleFallbackForTodoRows(AckBase):
    """Stage 2 — a freshly worded todo resolves with nobody maintaining vocabulary for it."""

    def test_release_pr_resolves_to_the_live_todo_row(self):
        res = self.resolve("release PR", live_rows=FakeLive())
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["title"], "Open the release-notes PR")
        self.assertEqual(res["matched_by"], "title")
        self.assertEqual(res["reminder_id"], PID_RELEASE)  # canonical dashed form, as stage 1 writes

    def test_an_alias_beats_a_title_and_stage_two_is_never_even_consulted(self):
        live = FakeLive()
        res = self.resolve("timesheet", live_rows=live)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["matched_by"], "alias")
        self.assertEqual(live.calls, 0)

    def test_two_active_rows_matching_comparably_refuse_and_name_both(self):
        """The SAME phrase that resolves above: flip the second release row active and it is a
        near-tie — a refusal naming both, never a pick."""
        spec = [(t, p, "Pending" if p == PID_ARCHIVE else s) for t, p, s in LIVE_ROWS]
        res = self.resolve("release PR", live_rows=FakeLive(spec))
        self.assertFalse(res["ok"])
        self.assertIn("ambiguous by title", res["error"])
        self.assertEqual(res["candidates"],
                         ["Archive the release notes", "Open the release-notes PR"])
        self.assertIsNone(res["reminder_id"])

    def test_a_finished_row_is_not_a_candidate(self):
        """Matching a retired row would resurrect a dead todo — and its title alone WOULD have matched,
        which the second half proves by flipping only its status. End to end through the real parser."""
        rc, summary, _ = self.run_ack("archive the release notes", runner=FakeRunner())
        self.assertEqual(rc, 3)
        self.assertEqual(self.outbox_rows(), [])
        self.assertIn("no ACTIVE reminder row", summary["results"][0]["error"])

        active = [(t, p, "Pending" if p == PID_ARCHIVE else s) for t, p, s in LIVE_ROWS]
        rc, summary, _ = self.run_ack("archive the release notes", runner=FakeRunner(active))
        self.assertEqual(rc, 0, summary)
        self.assertEqual(summary["acked"], ["Archive the release notes"])

    def test_a_word_the_row_title_cannot_explain_refuses(self):
        res = self.resolve("release PR and the dishes", live_rows=FakeLive())
        self.assertFalse(res["ok"])
        self.assertIn("accounts for every distinctive word", res["error"])

    def test_a_phrase_with_no_distinctive_words_matches_nothing_and_skips_the_lookup(self):
        live = FakeLive()
        res = self.resolve("did it", live_rows=live)
        self.assertFalse(res["ok"])
        self.assertIn("no distinctive word", res["error"])
        self.assertEqual(live.calls, 0)

    def test_an_unavailable_live_lookup_refuses_and_says_why(self):
        res = self.resolve("release PR", live_rows=FakeLive(error="Notion unreachable"))
        self.assertFalse(res["ok"])
        self.assertIn("Notion unreachable", res["error"])
        self.assertIsNone(res["reminder_id"])

    def test_a_title_match_runs_the_full_ack_path_with_no_second_code_path(self):
        self.write_queue([
            {"id": "rmd-r", "reminder_id": PID_RELEASE, "text": "Open the release-notes PR.",
             "due_at": "2026-08-09T23:00:00Z", "fired_at": None},
            {"id": "rmd-x", "reminder_id": "other", "text": "unrelated", "fired_at": None}])
        rc, summary, _ = self.run_ack("release PR", runner=FakeRunner())
        self.assertEqual(rc, 0, summary)
        rows = self.ack_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["reminder_id"], PID_RELEASE)
        self.assertEqual(rows[0]["idempotency_key"], ob.ack_key(PID_RELEASE, DATE))
        self.assertEqual(ra.load_acks(self.state).get(ra.norm_key(PID_RELEASE)), DATE)
        self.assertEqual([e["id"] for e in self.read_queue()], ["rmd-x"])

    def test_the_output_says_it_matched_by_title_and_names_the_row_in_full(self):
        rc, summary, out = self.run_ack("release PR", runner=FakeRunner())
        self.assertEqual(rc, 0)
        self.assertIn("BY TITLE", out)
        self.assertIn("matched by ROW TITLE", out)
        self.assertIn("Open the release-notes PR", out)
        self.assertEqual(summary["by_title"], ["Open the release-notes PR"])

    def test_the_live_lookup_happens_at_most_once_per_run(self):
        runner = FakeRunner()
        rc, summary, _ = self.run_ack("release PR", "no such row anywhere", runner=runner)
        self.assertEqual(rc, 3)
        self.assertEqual(summary["acked"], ["Open the release-notes PR"])
        self.assertEqual(len(runner.calls), 1)

    def test_no_live_skips_the_lookup_entirely_and_writes_nothing(self):
        runner = FakeRunner(raises=AssertionError("--no-live must not spawn a lookup"))
        rc, summary, _ = self.run_ack("--no-live", "release PR", runner=runner)
        self.assertEqual(rc, 3)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.outbox_rows(), [])
        self.assertIn("--no-live", summary["results"][0]["error"])

    def test_a_lookup_that_cannot_run_writes_nothing(self):
        rc, summary, _ = self.run_ack("release PR", runner=FakeRunner(raises=OSError("no claude")))
        self.assertEqual(rc, 3)
        self.assertEqual(self.outbox_rows(), [])
        self.assertIn("could not answer", summary["results"][0]["error"])


class FilesystemBackend(AckBase):
    """On a non-Notion store: resolve + dequeue + ledger still run; the outbox is skipped; stage 2
    declines without spawning."""

    BACKEND = "markdown"

    def test_the_local_half_runs_and_the_outbox_is_skipped(self):
        self.write_queue([{"id": "rmd-1", "reminder_id": PID_TIMESHEET,
                           "due_at": "2026-08-09T23:00:00Z", "fired_at": None}])
        rc, summary, out = self.run_ack("timesheet")
        self.assertEqual(rc, 0, summary)
        self.assertEqual(self.outbox_rows(), [])
        self.assertEqual(ra.load_acks(self.state).get(ra.norm_key(PID_TIMESHEET)), DATE)
        self.assertEqual(self.read_queue(), [])
        journal = summary["results"][0]["writes"][0]
        self.assertTrue(journal["skipped"])
        self.assertIn("markdown", journal["reason"])
        self.assertIn("skipped", out)

    def test_dry_run_plans_no_outbox_write(self):
        _, summary, _ = self.run_ack("--dry-run", "timesheet")
        self.assertEqual([w["op"] for w in summary["results"][0]["writes"]], ["reminders_dequeue"])

    def test_stage_two_declines_without_spawning(self):
        runner = FakeRunner(raises=AssertionError("a filesystem backend must not spawn a lookup"))
        with mock.patch.object(rl, "store_backend", return_value="markdown"):
            rc, summary, _ = self.run_ack("release PR", runner=runner)
        self.assertEqual(rc, 3)
        self.assertEqual(runner.calls, [])
        self.assertIn("Notion-backend only", summary["results"][0]["error"])


class ShippedExample(unittest.TestCase):
    """The **tracked** seed — shape and safety only, never contents beyond one smoke resolve."""

    def setUp(self):
        path = os.path.join(ack.REFERENCES_DIR, ack.ALIASES_EXAMPLE_FILE)
        with open(path, encoding="utf-8") as fh:
            self.aliases = json.load(fh)

    def test_every_alias_row_names_a_title_and_aliases(self):
        for row in self.aliases["rows"]:
            self.assertTrue(row.get("title"))
            self.assertTrue(row.get("aliases"))

    def test_every_clock_pair_names_both_sides(self):
        for pair in self.aliases["clock_pairs"]:
            self.assertTrue(pair.get("am") and pair.get("pm"))
            self.assertNotEqual(pair["am"], pair["pm"])

    def test_no_alias_is_a_duplicate_within_one_row(self):
        for row in self.aliases["rows"]:
            toks = [tuple(ack.tokens(a)) for a in row["aliases"]]
            self.assertEqual(len(toks), len(set(toks)), row["title"])

    def test_the_seed_resolves_against_a_matching_id_cache(self):
        titles = [r["title"] for r in self.aliases["rows"]]
        for pair in self.aliases["clock_pairs"]:
            titles += [pair["am"], pair["pm"]]
        state = tempfile.mkdtemp()
        with open(os.path.join(state, "reminders-id-cache.md"), "w", encoding="utf-8") as fh:
            fh.write("| Reminder | Page ID |\n|---|---|\n")
            for i, title in enumerate(titles, start=1):
                fh.write(f"| {title} | `{pid(i)}` |\n")
        index = ack.title_index(state)
        with mock.patch.object(tz_common, "_zone", return_value=OWNER_ZONE):
            res = ack.resolve("watered the plants", self.aliases, index, EVENING)
            self.assertTrue(res["ok"], res["error"])
            self.assertEqual(res["title"], "Water the plants")
            res = ack.resolve("did my stretch", self.aliases, index, EVENING)
            self.assertFalse(res["ok"])  # the shared bare word is a refusal by design
            res = ack.resolve("walked the dog", self.aliases, index, EVENING)
            self.assertEqual(res["title"], "Walk the dog (PM)")


class Helpers(unittest.TestCase):
    def test_alias_matching_requires_contiguous_tokens(self):
        self.assertTrue(ack._contains(["fed", "the", "cat"], ["fed", "the"]))
        self.assertFalse(ack._contains(["fed", "my", "cat"], ["fed", "the"]))

    def test_dashed_id_is_exact_and_passes_anything_else_through(self):
        self.assertEqual(ack.dashed_id("0000000000000000000000000000000a"),
                         "00000000-0000-0000-0000-00000000000a")
        self.assertEqual(ack.dashed_id("cat-am"), "cat-am")

    def test_a_naive_now_is_owner_wall_time_and_an_aware_one_converts(self):
        self.assertEqual(ack._local_now(datetime(2026, 8, 9, 18, 33)).hour, 18)
        with mock.patch.object(tz_common, "_zone", return_value=OWNER_ZONE):
            aware = datetime(2026, 8, 9, 18, 33, tzinfo=timezone.utc)
            self.assertEqual(ack._local_now(aware), datetime(2026, 8, 9, 13, 33))

    def test_an_unparseable_cutover_falls_back_to_noon(self):
        self.assertEqual(ack._parse_hhmm("nonsense"), (12, 0))
        self.assertEqual(ack._parse_hhmm("25:00"), (12, 0))
        self.assertEqual(ack._parse_hhmm("11:00"), (11, 0))


if __name__ == "__main__":
    unittest.main()
