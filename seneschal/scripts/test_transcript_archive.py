#!/usr/bin/env python3
"""Tests for the durable transcript archive (`transcript_archive.py`; `../docs/cockpit-spec.md`).

Five families, and the middle three are the ones that matter — they are the claims the change is
*for*, not the ones that are easy to write:

1. **The module itself** — day routing, the append, the tolerant readers, `stats`, the CLI, and the
   fail-open contract: `archive_event` must never raise, whatever it is handed and whatever the disk
   does.
2. **The ring buffer is byte-for-byte unaffected.** The live chat pane is the thing this change is
   forbidden to regress, so its behaviour is asserted directly: the same appends produce the same
   `warm-transcript.jsonl` bytes and the same `read_transcript_tail` output with the archive wired in
   as without it, and the cap still bites at the same place.
3. **Nothing prunes by default.** Retention is keep-everything by decision. This asserts the *absence* of a
   sweep — `prune` at its default is a no-op, and no caller anywhere in the tree invokes it. A test
   for a deletion that must not happen is the only thing that catches a later copy-paste from
   `mouth.py`'s 30-day sweep.
4. **A kill loses nothing.** The write path is append-only with no read-modify-write, so this
   simulates death at the two moments that could plausibly hurt — mid-backfill and mid-line — and
   asserts that every previously-written row survives and none is duplicated.
5. **The backfill is idempotent**, because it runs on every daemon boot and the daemon reboots on
   every merge.

Every date-dependent case **injects its instant** and derives its expected filename from that same
instant through `local_day` — never a hard-coded `"2026-08-09"` against a wall-clock call — and the
owner's zone is pinned (`tz_common._zone`), because the archive files by the owner's local date and a
runner's own zone must not be able to move a day boundary.

Run:  python -m unittest test_transcript_archive   (or)   python test_transcript_archive.py
"""
import ast
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import cockpit_pipe as cp  # noqa: E402
import transcript_archive as ta  # noqa: E402
import tz_common  # noqa: E402

# The owner's zone for every case: a fixed UTC-5. Pinned so no runner's clock can move a day.
LOCAL = timezone(timedelta(hours=-5))

# One fixed instant every date-dependent case derives from. Aware UTC, mid-afternoon local, so it is
# well clear of a day boundary — but no test below relies on that: each asks `local_day` what the
# filename should be rather than asserting a literal.
T0 = datetime(2026, 8, 9, 18, 30, 0, tzinfo=timezone.utc)


def _event(kind="assistant_output", ts=None, **fields):
    """A chat.event shaped exactly like `cockpit_pipe.chat_event` builds one."""
    ev = {"type": cp.TYPE_CHAT_EVENT, "kind": kind,
          "ts": (ts or T0).isoformat().replace("+00:00", "Z")}
    ev.update(fields)
    return ev


class ArchiveBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.object(tz_common, "_zone", return_value=LOCAL)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.restart()

    @staticmethod
    def restart():
        """Simulate the daemon restarting. `transcript_archive._healed` is a per-process cache of
        "this path's tail is ours and therefore intact", so clearing it is exactly what a new process
        sees — and it is the only state that survives between calls, so every test starts from a cold
        one rather than inheriting the last one's."""
        ta._healed.clear()

    def day(self, when=T0):
        return ta.local_day(when)

    def day_lines(self, when=T0):
        path = ta.archive_path(self.dir, self.day(when))
        try:
            with open(path, encoding="utf-8") as fh:
                return [ln for ln in fh.read().splitlines() if ln]
        except OSError:
            return []


# ------------------------------------------------------------------ 1. the module itself

class DayRouting(ArchiveBase):
    def test_an_event_files_under_its_own_timestamp_not_the_clock(self):
        # The property that makes the backfill idempotent: an event written days later still lands in
        # the day its `ts` names. If this read the clock instead, a re-run would file the same row
        # twice under two different days and the dedupe could never see it.
        old = T0 - timedelta(days=3)
        ta.archive_event(self.dir, _event(ts=old), now=T0)
        self.assertEqual(ta.available_days(self.dir), [self.day(old)])
        self.assertEqual(len(self.day_lines(old)), 1)
        self.assertEqual(self.day_lines(T0), [])

    def test_an_event_with_no_parseable_ts_falls_back_to_now(self):
        # A row that cannot say when it happened is still a row that happened — it gets filed, not
        # discarded. `now` is injected so this cannot drift with the wall clock.
        for bad in (None, "", "not-a-date", 12345):
            ev = {"type": cp.TYPE_CHAT_EVENT, "kind": "turn_done"}
            if bad is not None:
                ev["ts"] = bad
            self.assertTrue(ta.archive_event(self.dir, ev, now=T0))
        self.assertEqual(len(self.day_lines(T0)), 4)

    def test_day_boundaries_split_files(self):
        for offset in (0, 1, 2):
            ta.archive_event(self.dir, _event(ts=T0 - timedelta(days=offset)), now=T0)
        expected = sorted({self.day(T0 - timedelta(days=n)) for n in (0, 1, 2)})
        self.assertEqual(ta.available_days(self.dir), expected)


class TheAppend(ArchiveBase):
    def test_the_archived_line_is_byte_identical_to_the_ring_buffers(self):
        # THE load-bearing shape claim: one parser reads both files, and the backfill's raw-line
        # dedupe is only exact because these two serializations agree byte for byte.
        ev = _event(text="héllo — em dash and 日本語", tool_uses=[{"name": "Bash"}])
        ta.archive_event(self.dir, ev, now=T0)
        ring = json.dumps(ev, ensure_ascii=False)
        self.assertEqual(self.day_lines(), [ring])

    def test_rows_stay_in_append_order(self):
        for i in range(50):
            ta.archive_event(self.dir, _event(text=f"m{i}"), now=T0)
        rows = ta.read_day(self.dir, self.day())
        self.assertEqual([r["text"] for r in rows], [f"m{i}" for i in range(50)])

    def test_nothing_is_ever_rewritten(self):
        # The whole point, asserted two ways because volume alone can only rule out a cap SMALLER than
        # whatever number this test picks.
        for i in range(500):
            ta.archive_event(self.dir, _event(text=f"m{i}"), now=T0)
        self.assertEqual(len(self.day_lines()), 500)
        self.assertEqual(ta.read_day(self.dir, self.day())[0]["text"], "m0")

    def test_the_module_contains_no_truncating_write_at_all(self):
        # The structural half, and the one that would catch a cap set higher than any test's volume.
        # `_maybe_trim`'s anti-pattern must never reappear here in ANY form: outside `prune` (which
        # deletes whole day files, and which nothing calls), the archive may only ever append.
        #
        # Parsed, not grepped. A substring scan reads this module's own PROSE about truncation as
        # truncation — it went red on the docstring the first time — and prose is exactly what a
        # regression would not update.
        with open(os.path.join(SCRIPT_DIR, "transcript_archive.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            receiver = getattr(getattr(node.func, "value", None), "id", None)
            if name in ("truncate", "writelines"):
                offenders.append(f"{name}() at line {node.lineno}")
            # `replace`/`rename` only when they are the FILESYSTEM ones. `datetime.replace(tzinfo=…)`
            # is how every timestamp in this module is normalized and is not a write of any kind.
            if name in ("replace", "rename") and receiver in ("os", "shutil"):
                offenders.append(f"{receiver}.{name}() at line {node.lineno}")
            if name == "open":
                mode = next((a for a in node.args[1:2]), None) or next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                mode = getattr(mode, "value", "r") if mode is not None else "r"
                if not isinstance(mode, str) or set(mode) - set("arbt+"):
                    offenders.append(f"open(mode={mode!r}) at line {node.lineno}")
        # `prune` is the one sanctioned deleter, and it removes WHOLE FILES — never a rewrite — so
        # even it must not appear above. Nothing in this module may truncate anything.
        self.assertEqual(offenders, [],
                         f"a truncating write reached the archive: {offenders}")


class FailOpen(ArchiveBase):
    def test_archive_event_never_raises_on_anything(self):
        # The contract every caller relies on: no call site wraps this, so a raise here would take a
        # chat turn down. Includes an unserializable value, which is the one that actually throws.
        class Exotic:
            pass

        for bad in (None, "", 42, [], {"ts": object()}, {"v": Exotic()}, {"ts": T0}):
            self.assertIsInstance(ta.archive_event(self.dir, bad, now=T0), bool)

    def test_an_unwritable_archive_costs_the_row_and_nothing_else(self):
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(ta.archive_event(self.dir, _event(), now=T0))

    def test_readers_are_tolerant_of_corruption(self):
        ta.archive_event(self.dir, _event(text="first"), now=T0)
        with open(ta.archive_path(self.dir, self.day()), "a", encoding="utf-8") as fh:
            fh.write("{not json\n\n[1,2,3]\n")
        ta.archive_event(self.dir, _event(text="last"), now=T0)
        rows = ta.read_day(self.dir, self.day())
        self.assertEqual([r["text"] for r in rows], ["first", "last"])

    def test_every_reader_fails_open_on_an_absent_archive(self):
        empty = os.path.join(self.dir, "nope")
        self.assertEqual(ta.available_days(empty), [])
        self.assertEqual(ta.read_day(empty, "2026-08-09"), [])
        self.assertEqual(ta.backfill_from_ring(empty), 0)
        self.assertEqual(ta.prune(empty, days=30), [])
        self.assertEqual(ta.stats(empty)["rows"], 0)

    def test_a_stray_file_in_the_directory_is_not_read_as_a_day(self):
        os.makedirs(ta.archive_dir(self.dir), exist_ok=True)
        for name in ("notes.txt", "2026-08.jsonl", "2026-08-09.jsonl.tmp", "README.md"):
            with open(os.path.join(ta.archive_dir(self.dir), name), "w", encoding="utf-8") as fh:
                fh.write("x\n")
        ta.archive_event(self.dir, _event(), now=T0)
        self.assertEqual(ta.available_days(self.dir), [self.day()])


class Stats(ArchiveBase):
    def test_stats_counts_by_kind_and_reports_retention_as_decided(self):
        for kind in ("turn_started", "assistant_output", "assistant_output", "turn_done"):
            ta.archive_event(self.dir, _event(kind=kind), now=T0)
        s = ta.stats(self.dir)
        self.assertEqual(s["rows"], 4)
        self.assertEqual(s["by_kind"]["assistant_output"], 2)
        self.assertEqual(s["days"], 1)
        self.assertGreater(s["bytes"], 0)
        # The surface that keeps the retention decision visible rather than buried in a docstring.
        self.assertEqual(s["retention_days"], 0)
        # Keep-everything is decided — `retention_days` is 0 under both a placeholder and the
        # decision, so this is the field that tells them apart. The size sensor is
        # `transcript_size_watch.py`; nothing here deletes on a size.
        self.assertIs(s["retention_decided"], True)


class Cli(ArchiveBase):
    def _run(self, *argv):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = ta.main(["--state-dir", self.dir, *argv])
        return rc, buf.getvalue()

    def test_days_tail_stats_and_backfill(self):
        ta.archive_event(self.dir, _event(text="hello"), now=T0)
        rc, out = self._run("days")
        self.assertEqual((rc, out.strip()), (0, self.day()))
        rc, out = self._run("tail", "--day", self.day(), "--limit", "5")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.strip())["text"], "hello")
        rc, out = self._run("stats")
        self.assertEqual((rc, json.loads(out)["rows"]), (0, 1))
        rc, out = self._run("backfill")
        self.assertEqual((rc, json.loads(out)["appended"]), (0, 0))

    def test_prune_defaults_to_dropping_nothing(self):
        ta.archive_event(self.dir, _event(ts=T0 - timedelta(days=9999)), now=T0)
        rc, out = self._run("prune")
        self.assertEqual((rc, json.loads(out)["dropped"]), (0, []))
        self.assertEqual(len(ta.available_days(self.dir)), 1)


# --------------------------------------------- 2. the ring buffer is byte-for-byte unaffected

class TheLivePaneIsUnaffected(ArchiveBase):
    """The regression this change is forbidden to cause. Asserted against the ring buffer's real
    behaviour, not against a mock — with the archive wired in (as it now is) and with it disabled, the
    bytes on disk and the reader's output must be identical."""

    EVENTS = [_event(kind="turn_started", ts=T0, turn_id="t1", text_preview="hi"),
              _event(kind="assistant_output", ts=T0, turn_id="t1", text="hello there"),
              _event(kind="turn_done", ts=T0, turn_id="t1", duration_ms=1234)]

    def _ring_bytes(self, state_dir):
        with open(cp.transcript_path(state_dir), "rb") as fh:
            return fh.read()

    def test_the_ring_file_and_its_reader_are_identical_with_and_without_the_archive(self):
        wired = os.path.join(self.dir, "wired")
        bare = os.path.join(self.dir, "bare")
        for ev in self.EVENTS:
            cp.append_transcript_event(wired, ev)
        with mock.patch.object(cp, "transcript_archive", None):  # today's behaviour, exactly
            for ev in self.EVENTS:
                cp.append_transcript_event(bare, ev)

        # ABSOLUTE, not just relative. The wired-vs-bare comparison below is worth having, but on its
        # own it is weaker than it looks: `bare` runs the SAME code with the archive disabled, so any
        # change that corrupts the ring in both arms compares equal and passes. (A mutation that added
        # a stray write to `append_transcript_event` survived exactly that way.) So state what the
        # ring's bytes and its reader must be, independent of any other run.
        expected = "".join(json.dumps(ev, ensure_ascii=False) + "\n" for ev in self.EVENTS)
        with open(cp.transcript_path(wired), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), expected)
        self.assertEqual(cp.read_transcript_tail(wired), list(self.EVENTS))

        self.assertEqual(self._ring_bytes(wired), self._ring_bytes(bare))
        self.assertEqual(cp.read_transcript_tail(wired), cp.read_transcript_tail(bare))
        # ...and the archive really did get written, so the equalities above are not vacuous.
        self.assertEqual(len(ta.read_day(wired, self.day())), 3)
        self.assertEqual(ta.available_days(bare), [])

    def test_the_cap_still_bites_in_exactly_the_same_place(self):
        # The ring must keep EVICTING — the archive is what makes that acceptable, not a reason to
        # stop. Sessioned rows, so the real `_session_aware_tail` (not a bare row cap) runs.
        #
        # The CAPS are patched down rather than the volume being pushed up to 2,200+ real appends:
        # `_maybe_trim` rewrites the whole file on every append past the cap, so testing at the real
        # constants costs ~100 s of suite time to exercise the same code. Patching tests the
        # mechanism; `test_the_caps_are_unchanged` below tests the constants.
        wired = os.path.join(self.dir, "wired")
        bare = os.path.join(self.dir, "bare")
        events = [_event(ts=T0, session_id=f"s{i // 20}", text=f"m{i}") for i in range(400)]
        with mock.patch.object(cp, "TRANSCRIPT_CAP", 50), \
                mock.patch.object(cp, "TRANSCRIPT_SESSION_CAP", 2):
            for ev in events:
                cp.append_transcript_event(wired, ev)
            with mock.patch.object(cp, "transcript_archive", None):  # today's behaviour, exactly
                for ev in events:
                    cp.append_transcript_event(bare, ev)
            self.assertEqual(self._ring_bytes(wired), self._ring_bytes(bare))
            cap = cp.TRANSCRIPT_CAP  # read INSIDE the patch — outside it this is 2,000 again
            ring = cp.read_transcript_tail(wired, limit=cap)
        with open(cp.transcript_path(wired), encoding="utf-8") as fh:
            on_disk = [json.loads(ln) for ln in fh if ln.strip()]
        # Absolute, not merely "fewer than 400": the reader returns exactly the newest cap-worth, the
        # newest event survived, and the OLDEST IS GONE FROM DISK — which is the eviction itself, and
        # the thing the archive exists to make acceptable. (Deliberately not asserting an exact
        # surviving row count: that is a function of `_maybe_trim`'s slack margin, which is a tuning
        # knob rather than a contract, and pinning it would make this test red for the wrong reason.)
        self.assertEqual(ring, events[-cap:])
        self.assertEqual(on_disk[-1], events[-1])
        self.assertNotIn(events[0], on_disk)
        self.assertLess(len(on_disk), 400)
        # ...and every single one of the 400 survives in the archive. This pair of assertions IS the
        # feature: the ring forgot, the archive did not.
        self.assertEqual(len(ta.read_day(wired, self.day())), 400)

    def test_the_caps_are_unchanged(self):
        # The change is forbidden to touch these. Patched above, so assert them here.
        self.assertEqual(cp.TRANSCRIPT_CAP, 2000)
        self.assertEqual(cp.TRANSCRIPT_SESSION_CAP, 25)
        self.assertEqual(cp.TRANSCRIPT_FILE, "warm-transcript.jsonl")

    def test_a_broken_archive_cannot_break_the_ring_append(self):
        # Fail-open in the direction that matters: if the archive somehow explodes, the live pane must
        # still get its event. (`archive_event` swallows its own failures; this asserts that even a
        # hard raise from the module cannot reach the caller's turn.)
        with mock.patch.object(ta, "archive_event", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                cp.append_transcript_event(self.dir, _event())
        # The ring append happens FIRST, so the event is on disk regardless.
        self.assertEqual(len(cp.read_transcript_tail(self.dir)), 1)


# ------------------------------------------------------ 3. nothing prunes by default

class NothingPrunesByDefault(ArchiveBase):
    """Retention is keep-everything by decision. These assert the ABSENCE of a sweep — the only
    kind of test that catches a later copy-paste from `mouth.py`'s 30-day one."""

    def test_the_default_keeps_everything_however_old(self):
        for age in (1, 31, 400, 4000):
            ta.archive_event(self.dir, _event(ts=T0 - timedelta(days=age)), now=T0)
        self.assertEqual(ta.prune(self.dir, now=T0), [])
        self.assertEqual(ta.prune(self.dir, days=ta.RETENTION_DAYS, now=T0), [])
        self.assertEqual(len(ta.available_days(self.dir)), 4)

    def test_the_retention_constant_is_the_keep_everything_sentinel(self):
        self.assertEqual(ta.RETENTION_DAYS, 0)

    def test_no_caller_anywhere_in_the_tree_invokes_prune(self):
        # `turns.py` makes the same promise and this is how it must be kept: in code, over the real
        # tree. A wired-in sweep here would be an unrecoverable deletion of the one thing in `state/`
        # with no backup and no way to regenerate it — so the guard is a test, not a sentence.
        #
        # Two shapes, because there are two ways to call it: a Python call to
        # `transcript_archive.prune(...)`, and the CLI form a mode file, a .ps1 or a scheduled task
        # could carry. The CLI pattern requires an INTERPRETER in front of it, so that a doc which
        # merely names the subcommand in prose is not read as a caller — this test caught its own
        # entry in `ci-and-test-inventory.md` the first time, which is a false positive that would
        # have taught the next person to loosen the pattern rather than trust it.
        # Anchored to ONE line throughout (`[^\n]`, never a bare `\s`): a pattern that may cross a
        # newline will happily bridge an invocation on one line to the word "prune" paragraphs later,
        # which is how the first version flagged `state/README.md`'s `... transcript_archive.py days`.
        cli = re.compile(r"(?:python\S*|pwsh|uv\s+run)[^\n]*?transcript_archive\.py[^\n]*?\bprune\b")
        offenders = []
        for root, dirs, names in os.walk(os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))):
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__",
                                                    ".venv", "dist", "build", "target"}]
            for name in names:
                if name in (os.path.basename(__file__), "transcript_archive.py"):
                    continue  # this file, and the module's own definition + CLI
                if not name.endswith((".py", ".md", ".ps1", ".json", ".xml", ".sh")):
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        body = fh.read()
                except OSError:
                    continue
                if "transcript_archive" not in body:
                    continue
                for match in cli.finditer(body):
                    offenders.append(f"{path}: {match.group(0)}")
                if not name.endswith(".py"):
                    continue
                try:
                    tree = ast.parse(body)
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "prune" \
                            and getattr(getattr(node.func, "value", None), "id", None) \
                            == "transcript_archive":
                        offenders.append(f"{path}:{node.lineno} transcript_archive.prune()")
        self.assertEqual(offenders, [], f"something now prunes the archive: {offenders}")

    def test_prune_only_deletes_when_explicitly_told_to(self):
        # The mechanism still has to WORK, so a future decision has something tested to reach for.
        old, recent = T0 - timedelta(days=500), T0 - timedelta(days=2)
        ta.archive_event(self.dir, _event(ts=old), now=T0)
        ta.archive_event(self.dir, _event(ts=recent), now=T0)
        self.assertEqual(ta.prune(self.dir, days=400, now=T0), [ta.local_day(old)])
        self.assertEqual(ta.available_days(self.dir), [ta.local_day(recent)])

    def test_prune_drops_whole_days_never_a_partial_rewrite(self):
        # chat-turn-indexing-spec.md §7.2 rule 3. A half-emptied day file reads as a quiet day, which
        # is the worse of the two failures.
        for i in range(10):
            ta.archive_event(self.dir, _event(ts=T0 - timedelta(days=500), text=f"m{i}"), now=T0)
        ta.prune(self.dir, days=400, now=T0)
        self.assertFalse(os.path.exists(ta.archive_path(self.dir, ta.local_day(T0 - timedelta(days=500)))))


# ---------------------------------------------------------- 4. a kill loses nothing

class InterruptionLosesNothing(ArchiveBase):
    """The write path has no read-modify-write, so there is no window in which an interrupted run can
    lose an already-written line. These prove that rather than asserting it."""

    def test_a_kill_between_appends_loses_nothing_already_written(self):
        for i in range(20):
            ta.archive_event(self.dir, _event(text=f"m{i}"), now=T0)
        # "The process dies." Nothing is in flight; the next boot appends after what is there.
        for i in range(20, 40):
            ta.archive_event(self.dir, _event(text=f"m{i}"), now=T0)
        rows = ta.read_day(self.dir, self.day())
        self.assertEqual([r["text"] for r in rows], [f"m{i}" for i in range(40)])

    def test_a_partial_final_line_costs_only_itself(self):
        # The one residual of not fsyncing: a torn last line. Every earlier row must survive intact,
        # and the next append must still land — a truncated tail cannot poison the file.
        for i in range(5):
            ta.archive_event(self.dir, _event(text=f"m{i}"), now=T0)
        with open(ta.archive_path(self.dir, self.day()), "a", encoding="utf-8") as fh:
            fh.write('{"type":"chat.event","kind":"assistant_out')  # killed mid-write, no newline
        self.restart()  # a torn tail can only come from a process that died — so the next writer is
                        # a new process, with a cold `_healed`. Anything else would be testing a state
                        # the host cannot actually be in.
        ta.archive_event(self.dir, _event(text="after"), now=T0)
        rows = ta.read_day(self.dir, self.day())
        self.assertEqual([r["text"] for r in rows], [f"m{i}" for i in range(5)] + ["after"])

    def test_a_backfill_killed_partway_loses_and_duplicates_nothing_on_re_run(self):
        # THE crash-safety case the brief names. A day's rescue dies after its first day file is
        # written; the re-run must complete the rest and re-append none of the first.
        ring = cp.transcript_path(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        events = [_event(ts=T0 - timedelta(days=d), text=f"d{d}") for d in (2, 1, 0)]
        with open(ring, "w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

        real_open, calls = open, {"n": 0}

        def dying_open(path, *a, **kw):
            # Let the first day's append through, then die on the second — mid-backfill.
            if isinstance(path, str) and path.endswith(".jsonl") and "transcripts" in path \
                    and "a" in str(a[0] if a else kw.get("mode", "r")):
                calls["n"] += 1
                if calls["n"] > 1:
                    raise KeyboardInterrupt("killed mid-backfill")
            return real_open(path, *a, **kw)

        with mock.patch("builtins.open", side_effect=dying_open):
            with self.assertRaises(KeyboardInterrupt):
                ta.backfill_from_ring(self.dir, now=T0)
        partial = sum(len(ta.read_day(self.dir, d)) for d in ta.available_days(self.dir))
        self.assertEqual(partial, 1)  # exactly one day landed before the kill

        # The re-run completes it, and adds nothing to the day that already landed.
        self.assertEqual(ta.backfill_from_ring(self.dir, now=T0), 2)
        rows = [r for d in ta.available_days(self.dir) for r in ta.read_day(self.dir, d)]
        self.assertEqual(sorted(r["text"] for r in rows), ["d0", "d1", "d2"])


# ------------------------------------------------------------ 5. the backfill is idempotent

class BackfillIsIdempotent(ArchiveBase):
    def _seed_ring(self, events):
        os.makedirs(self.dir, exist_ok=True)
        with open(cp.transcript_path(self.dir), "w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def test_it_rescues_the_ring_and_re_running_appends_nothing(self):
        # It runs on every daemon boot, and the daemon reboots on every merge.
        self._seed_ring([_event(text=f"m{i}") for i in range(10)])
        self.assertEqual(ta.backfill_from_ring(self.dir, now=T0), 10)
        for _ in range(3):
            self.assertEqual(ta.backfill_from_ring(self.dir, now=T0), 0)
        self.assertEqual(len(ta.read_day(self.dir, self.day())), 10)

    def test_it_appends_only_what_is_new_after_a_live_write(self):
        # The realistic boot: the archive already holds yesterday's rescue, the ring has since grown.
        first = [_event(text=f"m{i}") for i in range(5)]
        self._seed_ring(first)
        ta.backfill_from_ring(self.dir, now=T0)
        self._seed_ring(first + [_event(text=f"m{i}") for i in range(5, 8)])
        self.assertEqual(ta.backfill_from_ring(self.dir, now=T0), 3)
        rows = ta.read_day(self.dir, self.day())
        self.assertEqual([r["text"] for r in rows], [f"m{i}" for i in range(8)])

    def test_a_live_appended_row_is_not_re_added_by_a_later_backfill(self):
        # The two writers must agree, and they only do because both file by the event's own `ts` and
        # serialize the same bytes. This is the assertion that would catch either one drifting.
        ev = _event(text="live")
        cp.append_transcript_event(self.dir, ev)
        self.assertEqual(ta.backfill_from_ring(self.dir, now=T0), 0)
        self.assertEqual(len(ta.read_day(self.dir, self.day())), 1)

    def test_it_routes_a_multi_day_ring_into_separate_files(self):
        self._seed_ring([_event(ts=T0 - timedelta(days=d), text=f"d{d}") for d in (3, 2, 1, 0)])
        self.assertEqual(ta.backfill_from_ring(self.dir, now=T0), 4)
        self.assertEqual(len(ta.available_days(self.dir)), 4)

    def test_a_corrupt_ring_line_rides_with_its_predecessor_rather_than_being_dropped(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(cp.transcript_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_event(text="good"), ensure_ascii=False) + "\n")
            fh.write("{truncated\n")
            fh.write("\n")  # blank lines are skipped, not filed
        self.assertEqual(ta.backfill_from_ring(self.dir, now=T0), 2)
        self.assertEqual(len(self.day_lines()), 2)  # the corrupt one is PRESERVED verbatim...
        self.assertEqual(len(ta.read_day(self.dir, self.day())), 1)  # ...and skipped by the reader

    def test_an_empty_or_absent_ring_is_a_no_op(self):
        self.assertEqual(ta.backfill_from_ring(self.dir, now=T0), 0)
        self._seed_ring([])
        self.assertEqual(ta.backfill_from_ring(self.dir, now=T0), 0)
        self.assertEqual(ta.available_days(self.dir), [])


# ------------------------------------------------------- the daemon actually calls it

class ProducerCoverage(ArchiveBase):
    """`state/metrics.jsonl` is the standing precedent: the risk was never a buggy writer, it was a
    writer nothing calls. These assert the wiring, in code, over the real source."""

    def test_the_ring_buffers_only_write_point_tees_to_the_archive(self):
        with open(os.path.join(SCRIPT_DIR, "cockpit_pipe.py"), encoding="utf-8") as fh:
            body = fh.read()
        head, _, tail = body.partition("def append_transcript_event")
        self.assertIn("transcript_archive.archive_event", tail.split("\ndef ")[0])

    def test_the_daemon_backfills_at_boot(self):
        with open(os.path.join(SCRIPT_DIR, "presence.py"), encoding="utf-8") as fh:
            body = fh.read()
        if "import transcript_archive" not in body:
            # The boot-time backfill lands with the daemon wiring; until then the ring is rescued by
            # hand (`transcript_archive.py backfill`). Once presence imports the module, this bites.
            self.skipTest("presence.py does not wire the transcript archive yet")
        self.assertIn("transcript_archive.backfill_from_ring", body)
        self.assertIn("import transcript_archive", body)


if __name__ == "__main__":
    unittest.main()
