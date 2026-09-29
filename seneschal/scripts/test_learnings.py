#!/usr/bin/env python3
"""Tests for the Dream-proposal closer, the disposition/closed-at stamp, the archive-retirement
step, and the applied-but-open audit.

**The load-bearing tests are the refusals.** A `close` that quietly does nothing is the same failure
class as the daily reset that quietly did nothing — the caller is often a script applying a fix, and
it needs to know its bookkeeping didn't land. Silence there would rebuild the exact gap this closes.
The same logic extends to `restamp` (never double-stamp, never guess a same-date collision) and to
`retire` (never touch an unstamped legacy row, never retire a declined row unless explicitly asked,
never leave the source and the archive out of sync with each other).

The second family pins `_pending_section`: a naive grep of the whole file counted the format template
and the worked example as backlog, reporting three open proposals when there was one. Off-by-two on a
queue length is how a real item hides.

Stdlib unittest only. Run:  python -m unittest seneschal.scripts.test_learnings
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import learnings as ln  # noqa: E402

DOC = """# Proposed learnings (the held queue)

## Proposal format

- [ ] <YYYY-MM-DD> — <short title>
  Pattern: ...

## Worked example

- [ ] 2026-08-15 — Salience: a worked example, not a real item
  Pattern: shape only.

## Pending

- [ ] 2026-07-17 — Retire the hardcoded standing roll
  Pattern: it outlived the row it serves.
  Applies to: seneschal/scripts/reminders_roll.py (the ROLLS list)
- [ ] 2026-07-20 — Fix the daily-seed reset gap
  Pattern: four rows read Done-from-yesterday.
  Applies to: seneschal/scripts/reminders_seed.py (+ tests)
- [x] 2026-07-02 — Something already done
  Pattern: closed long ago.
"""


class PendingSectionOnly(unittest.TestCase):
    """The template and the worked example are `- [ ]` lines and neither is a backlog item."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "proposed-learnings.md")
        with open(self.p, "w", encoding="utf-8") as fh:
            fh.write(DOC)

    def test_counts_only_real_items(self):
        items = ln.list_items(self.p)
        self.assertEqual([i["date"] for i in items], ["2026-07-17", "2026-07-20"],
                         "the 2026-08-15 worked example is above ## Pending and must not count")

    def test_include_done(self):
        self.assertEqual(len(ln.list_items(self.p, include_done=True)), 3)

    def test_a_file_without_a_pending_section_is_empty_not_a_crash(self):
        p = os.path.join(self.d, "other.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("# nothing here\n")
        self.assertEqual(ln.list_items(p), [])

    def test_a_missing_file_is_empty_not_a_crash(self):
        self.assertEqual(ln.list_items(os.path.join(self.d, "nope.md")), [])


class CloseRefusesRatherThanNoOps(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "proposed-learnings.md")
        with open(self.p, "w", encoding="utf-8") as fh:
            fh.write(DOC)

    def test_closes_stamps_and_records_the_note(self):
        res = ln.close("2026-07-17", "applied in abc1234", self.p, now="2026-09-06")
        self.assertTrue(res["ok"])
        self.assertEqual(res["disposition"], "applied")
        self.assertEqual(res["closed_at"], "2026-09-06")
        with open(self.p, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(
            "- [x] 2026-07-17 — Retire the hardcoded standing roll — "
            "CLOSED 2026-09-06 (applied): applied in abc1234",
            text)
        self.assertEqual([i["date"] for i in ln.list_items(self.p)], ["2026-07-20"])

    def test_declined_flag_stamps_declined(self):
        res = ln.close("2026-07-17", "no clean fix path", self.p, disposition="declined",
                        now="2026-09-06")
        self.assertTrue(res["ok"])
        self.assertEqual(res["disposition"], "declined")
        with open(self.p, encoding="utf-8") as fh:
            self.assertIn("CLOSED 2026-09-06 (declined): no clean fix path", fh.read())

    def test_an_invalid_disposition_is_refused(self):
        res = ln.close("2026-07-17", "x", self.p, disposition="maybe")
        self.assertFalse(res["ok"])
        self.assertIn("disposition", res["reason"])

    def test_an_unknown_date_is_refused(self):
        res = ln.close("2026-01-01", "x", self.p)
        self.assertFalse(res["ok"])
        self.assertIn("no proposal", res["reason"])

    def test_an_already_closed_item_is_refused(self):
        res = ln.close("2026-07-02", "x", self.p)
        self.assertFalse(res["ok"])
        self.assertIn("already closed", res["reason"])

    def test_the_body_below_the_item_is_untouched(self):
        ln.close("2026-07-17", "note", self.p, now="2026-09-06")
        with open(self.p, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Pattern: it outlived the row it serves.", text)
        self.assertIn("Applies to: seneschal/scripts/reminders_roll.py", text)

    def test_closing_without_a_note(self):
        res = ln.close("2026-07-17", "", self.p, now="2026-09-06")
        self.assertTrue(res["ok"])
        with open(self.p, encoding="utf-8") as fh:
            self.assertIn(
                "- [x] 2026-07-17 — Retire the hardcoded standing roll — CLOSED 2026-09-06 (applied)\n",
                fh.read())

    def test_the_cli_exits_nonzero_on_a_refusal(self):
        self.assertEqual(ln._main(["close", "2026-07-17", "--path", self.p]), 0)
        self.assertEqual(ln._main(["close", "2026-07-17", "--path", self.p]), 1,
                         "a second close must fail loudly, not succeed silently")

    def test_the_cli_declined_flag(self):
        self.assertEqual(ln._main(["close", "2026-07-17", "--declined", "--path", self.p]), 0)
        with open(self.p, encoding="utf-8") as fh:
            self.assertIn("(declined)", fh.read())


class Restamp(unittest.TestCase):
    """The one-time backfill primitive for a row closed before the structured stamp existed."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "proposed-learnings.md")
        with open(self.p, "w", encoding="utf-8") as fh:
            fh.write(DOC + (
                "- [x] 2026-06-01 — A duplicate date, first\n"
                "  Pattern: one of two sharing a date.\n"
                "- [x] 2026-06-01 — A duplicate date, second\n"
                "  Pattern: the other of two sharing a date.\n"
            ))

    def test_backfills_the_stamp(self):
        res = ln.restamp("2026-07-02", "2026-07-03", "applied", self.p)
        self.assertTrue(res["ok"])
        with open(self.p, encoding="utf-8") as fh:
            self.assertIn(
                "- [x] 2026-07-02 — Something already done — CLOSED 2026-07-03 (applied)",
                fh.read())

    def test_refuses_an_open_row(self):
        res = ln.restamp("2026-07-17", "2026-07-18", "applied", self.p)
        self.assertFalse(res["ok"])
        self.assertIn("still open", res["reason"])

    def test_refuses_an_unknown_date(self):
        res = ln.restamp("2020-01-01", "2020-01-02", "applied", self.p)
        self.assertFalse(res["ok"])
        self.assertIn("no proposal", res["reason"])

    def test_refuses_double_stamping(self):
        ln.restamp("2026-07-02", "2026-07-03", "applied", self.p)
        res = ln.restamp("2026-07-02", "2026-07-04", "applied", self.p)
        self.assertFalse(res["ok"])
        self.assertIn("already stamped", res["reason"])

    def test_refuses_an_ambiguous_shared_date(self):
        res = ln.restamp("2026-06-01", "2026-06-05", "applied", self.p)
        self.assertFalse(res["ok"])
        self.assertIn("narrow with --match", res["reason"])

    def test_match_disambiguates_a_shared_date(self):
        res = ln.restamp("2026-06-01", "2026-06-05", "applied", self.p, match="second")
        self.assertTrue(res["ok"])
        with open(self.p, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("A duplicate date, second — CLOSED 2026-06-05 (applied)", text)
        self.assertNotIn("A duplicate date, first — CLOSED", text)

    def test_an_invalid_disposition_is_refused(self):
        res = ln.restamp("2026-07-02", "2026-07-03", "sideways", self.p)
        self.assertFalse(res["ok"])
        self.assertIn("disposition", res["reason"])


class Retire(unittest.TestCase):
    """The move-never-delete archival step: only a stamped, aged-out row is ever touched."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "proposed-learnings.md")
        self.archive = os.path.join(self.d, "proposed-learnings-archive.md")
        with open(self.p, "w", encoding="utf-8") as fh:
            fh.write(
                "# doc\n\n"
                "## Proposal format\n\n"
                "- [ ] <YYYY-MM-DD> — <short title>\n\n"
                "## Pending\n\n"
                "- [ ] 2026-07-17 — Still open, never touched\n"
                "  Pattern: open.\n"
                "\n"
                "- [x] 2026-07-02 — Legacy closed row, never stamped\n"
                "  Pattern: closed before this stamp existed.\n"
                "\n"
                "- [x] 2026-06-01 — Old enough, applied, eligible — CLOSED 2026-06-05 (applied)\n"
                "  Pattern: closed well in the past.\n"
                "\n"
                "- [x] 2026-08-20 — Too young, applied, not yet eligible — CLOSED 2026-08-20 (applied)\n"
                "  Pattern: closed recently.\n"
                "\n"
                "- [x] 2026-05-01 — Old enough but declined — CLOSED 2026-05-05 (declined)\n"
                "  Pattern: turned down.\n"
                "\n"
                "## Applied\n\n"
                "- [x] 2026-04-01 — A legacy Applied-section row, already stamped — "
                "CLOSED 2026-04-10 (applied)\n"
                "  Pattern: filed here before the CLI existed.\n"
                "\n"
                "## Declined\n\n"
            )

    def _retire(self, **kw):
        kw.setdefault("now", "2026-09-06")
        return ln.retire(self.p, self.archive, **kw)

    def test_dry_run_reports_without_writing(self):
        before = open(self.p, encoding="utf-8").read()
        res = self._retire()
        self.assertTrue(res["ok"])
        self.assertFalse(res["apply"])
        dates = sorted(p["date"] for p in res["retired"])
        self.assertEqual(dates, ["2026-04-01", "2026-06-01"])
        self.assertEqual(open(self.p, encoding="utf-8").read(), before,
                         "a dry run must not write anything")
        self.assertFalse(os.path.exists(self.archive))

    def test_apply_moves_only_the_eligible_rows(self):
        res = self._retire(apply=True)
        self.assertTrue(res["ok"], res.get("reason"))
        main = open(self.p, encoding="utf-8").read()
        archive = open(self.archive, encoding="utf-8").read()

        # Moved: old enough + applied, from both Pending and Applied.
        self.assertNotIn("Old enough, applied, eligible", main)
        self.assertIn("Old enough, applied, eligible", archive)
        self.assertNotIn("A legacy Applied-section row", main)
        self.assertIn("A legacy Applied-section row", archive)

        # Left alone: open, unstamped-legacy, too-young, and declined.
        self.assertIn("Still open, never touched", main)
        self.assertIn("Legacy closed row, never stamped", main)
        self.assertIn("Too young, applied, not yet eligible", main)
        self.assertIn("Old enough but declined", main)
        self.assertNotIn("Too young", archive)
        self.assertNotIn("Old enough but declined", archive)

    def test_archive_gets_a_header_only_once(self):
        self._retire(apply=True)
        first = open(self.archive, encoding="utf-8").read()
        self.assertEqual(first.count("# Retired learnings"), 1)
        # A second run with nothing left eligible must not touch the archive again.
        res = self._retire(apply=True)
        self.assertEqual(res["retired"], [])
        self.assertEqual(open(self.archive, encoding="utf-8").read(), first)

    def test_retirement_is_marked_with_its_source_section(self):
        self._retire(apply=True)
        archive = open(self.archive, encoding="utf-8").read()
        self.assertIn("<!-- retired 2026-09-06 from ## Pending -->", archive)
        self.assertIn("<!-- retired 2026-09-06 from ## Applied -->", archive)

    def test_declined_is_retired_only_when_asked(self):
        res = self._retire(apply=True, include_declined=True)
        dates = sorted(p["date"] for p in res["retired"])
        self.assertIn("2026-05-01", dates)
        self.assertNotIn("Old enough but declined", open(self.p, encoding="utf-8").read())

    def test_min_age_days_is_honored(self):
        res = self._retire(min_age_days=0)
        dates = sorted(p["date"] for p in res["retired"])
        # Now even the too-young-at-30-days row clears a 0-day bar (declined still excluded).
        self.assertIn("2026-08-20", dates)
        self.assertNotIn("2026-05-01", dates)

    def test_exactly_min_age_days_is_eligible(self):
        # CLOSED 2026-08-07, "now" 2026-09-06 -> exactly 30 days.
        with open(self.p, "a", encoding="utf-8") as fh:
            fh.write(
                "\n- [x] 2026-08-01 — Exactly thirty days old\n"
                "  Pattern: boundary. — CLOSED 2026-08-07 (applied)\n")
        res = self._retire()
        self.assertIn("2026-08-01", [p["date"] for p in res["retired"]])

    def test_a_missing_file_is_reported_not_a_crash(self):
        res = ln.retire(os.path.join(self.d, "nope.md"), self.archive, now="2026-09-06")
        self.assertFalse(res["ok"])

    def test_the_cli_dry_run_writes_nothing(self):
        before_exists = os.path.exists(self.archive)
        code = ln._main(["retire", "--path", self.p, "--archive-path", self.archive,
                          "--now", "2026-09-06"])
        self.assertEqual(code, 0)
        self.assertEqual(os.path.exists(self.archive), before_exists)

    def test_the_cli_apply_writes(self):
        code = ln._main(["retire", "--apply", "--path", self.p, "--archive-path", self.archive,
                          "--now", "2026-09-06"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.archive))


class Audit(unittest.TestCase):
    """The motivating shape: an open item whose named file changed after it was raised."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=self.d, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.d, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.d, check=True)
        os.makedirs(os.path.join(self.d, "seneschal", "scripts"))
        for rel in ("seneschal/scripts/reminders_roll.py", "seneschal/scripts/reminders_seed.py"):
            with open(os.path.join(self.d, rel), "w", encoding="utf-8") as fh:
                fh.write("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.d, check=True)
        subprocess.run(["git", "commit", "-qm", "seed", "--date", "2026-07-01T00:00:00"],
                       cwd=self.d, check=True,
                       env={**os.environ, "GIT_COMMITTER_DATE": "2026-07-01T00:00:00"})
        self.p = os.path.join(self.d, "proposed-learnings.md")
        with open(self.p, "w", encoding="utf-8") as fh:
            fh.write(DOC)

    def _touch(self, rel, msg, date):
        with open(os.path.join(self.d, rel), "a", encoding="utf-8") as fh:
            fh.write("y = 2\n")
        subprocess.run(["git", "add", "-A"], cwd=self.d, check=True)
        subprocess.run(["git", "commit", "-qm", msg, "--date", date], cwd=self.d, check=True,
                       env={**os.environ, "GIT_COMMITTER_DATE": date})

    def test_a_file_changed_after_the_proposal_is_flagged(self):
        self._touch("seneschal/scripts/reminders_roll.py", "retire the roll", "2026-07-19T00:00:00")
        rep = ln.audit(self.p, repo_root=self.d)
        self.assertTrue(rep["available"])
        self.assertEqual(rep["open"], 2)
        self.assertEqual([s["date"] for s in rep["suspects"]], ["2026-07-17"])
        self.assertIn("retire the roll", rep["suspects"][0]["commit"])

    def test_an_untouched_file_is_not_flagged(self):
        self.assertEqual(ln.audit(self.p, repo_root=self.d)["suspects"], [])

    def test_a_change_BEFORE_the_proposal_is_not_flagged(self):
        # The seed commit predates every proposal; flagging it would make the audit useless noise.
        rep = ln.audit(self.p, repo_root=self.d)
        self.assertEqual(rep["suspects"], [])

    def test_a_closed_item_is_never_flagged(self):
        self._touch("seneschal/scripts/reminders_roll.py", "retire", "2026-07-19T00:00:00")
        ln.close("2026-07-17", "done", self.p)
        self.assertEqual(ln.audit(self.p, repo_root=self.d)["suspects"], [])

    def test_it_reports_and_never_edits(self):
        self._touch("seneschal/scripts/reminders_roll.py", "retire", "2026-07-19T00:00:00")
        before = open(self.p, encoding="utf-8").read()
        ln.audit(self.p, repo_root=self.d)
        self.assertEqual(open(self.p, encoding="utf-8").read(), before,
                         "an auto-closer working off a heuristic would hide the backlog, not fix it")

    def test_the_cli_always_exits_zero(self):
        self._touch("seneschal/scripts/reminders_seed.py", "fix the seed", "2026-07-21T00:00:00")
        self.assertEqual(ln._main(["audit", "--path", self.p]), 0,
                         "a heuristic must not be able to fail a nightly run")


if __name__ == "__main__":
    unittest.main(verbosity=2)
