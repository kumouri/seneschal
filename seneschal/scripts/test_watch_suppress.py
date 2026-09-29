#!/usr/bin/env python3
"""Tests for the **Watch suppression list** — the second question on the Watch send door.

The shape of the ask: the comms peek keeps pushing

    🔴 Registrar Alert: old-shop.example expired 30+ days ago and will be permanently deleted.

about a domain the owner is **deliberately letting expire**. The peek is right every time — it cannot
know the owner wants that outcome — and the narrow fix beats turning the peek off, so nothing here may
disable or throttle it.

What is pinned, in order of what a later edit is most likely to break:

* **`FailOpen` is the class to argue with before making it green.** A missing, unreadable or
  malformed list suppresses **nothing**. Only a positive reading of a well-formed entry blocks —
  `reminders_acks`' doctrine pointed at a second gate: a message that slips through costs one nudge, a
  gate firing off a half-written file costs a message the owner never learns was withheld.
* **`NeverSuppressible` is the one that has no escape hatch.** 🚨 Critical, 🛑 Super-Critical and
  `Call Me` cannot be suppressed by any pattern, including a pattern written to target them. They are
  also structurally out of reach — they fire from `sentinel.check_reminders`, a different send door —
  and `ReminderPathIsNotThisDoor` asserts that separately, because a guard resting on today's call
  graph is one refactor from being false.
* **`LiveListOverExample`** — the owner's list is gitignored owner data; the tracked example ships
  with no patterns, so a fresh install suppresses nothing, and the live file wins whenever it exists.

Every fixture is synthetic; no instant is read from the wall clock.

Run:  python -m unittest test_watch_suppress   (from seneschal/scripts)
"""
import json
import os
import re
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import watch_suppress as ws  # noqa: E402

DOMAIN = "old-shop.example"
#: The alert as a peek would send it.
DOMAIN_PUSH = ("🔴 Registrar Alert: old-shop.example expired 30+ days ago and will be "
               "**permanently deleted**. Renew now to restore it.")
#: A different domain, same vendor, same wording — the owner decided about ONE domain, not the vendor.
OTHER_DOMAIN_PUSH = ("🔴 Registrar Alert: keep-me.example expired 30+ days ago and will be "
                     "**permanently deleted**. Renew now to restore it.")
#: An ordinary peek line that names nothing on the list.
UNRELATED_PUSH = "Two emails since 09:00: a brokerage statement and a calendar invite from a colleague."


def write_list(path, patterns):
    """A well-shaped list file holding exactly `patterns`."""
    write_raw(path, {"version": 1, "patterns": patterns})


def write_raw(path, document):
    """Whatever JSON the caller asks for, shape and all — for the malformed cases."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(document, fh)


class ListFixture(unittest.TestCase):
    """A temp dir holding a list file of this test's own making, so nothing here depends on what the
    shipped example or an install's live list happens to say."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "watch-suppressions.json")

    def seed(self, patterns=None):
        write_list(self.path, patterns if patterns is not None else [
            {"pattern": DOMAIN, "added": "2026-08-30",
             "reason": "the owner is deliberately letting this domain expire"}])


class Matching(ListFixture):
    """Substring, case-insensitive, literal. Nothing else."""

    def test_a_matching_message_is_blocked_and_names_its_pattern(self):
        self.seed()
        verdict = ws.match(DOMAIN_PUSH, self.path)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["reason"], ws.SUPPRESSED_REASON)
        self.assertEqual(verdict["pattern"], DOMAIN)
        self.assertIn("expire", verdict["why"])  # the owner's reason travels with the verdict
        self.assertEqual(verdict["added"], "2026-08-30")

    def test_case_is_not_identity(self):
        self.seed()
        self.assertIsNotNone(ws.match("Renew OLD-shop.EXAMPLE now", self.path))

    def test_a_non_matching_message_passes(self):
        self.seed()
        self.assertIsNone(ws.match(UNRELATED_PUSH, self.path))

    def test_the_pattern_is_the_domain_not_the_vendor(self):
        """The seeded entry names the DOMAIN on purpose: a real alert about a domain the owner is
        keeping must still reach them. If this goes red because someone widened the pattern to the
        vendor's name, the widening is the bug."""
        self.seed()
        self.assertIsNone(ws.match(OTHER_DOMAIN_PUSH, self.path))

    def test_a_pattern_is_literal_text_not_a_regex(self):
        """No DSL. `.` is a full stop and `.*` matches only itself — a metacharacter in a pattern is
        just that character, which is what keeps this file safe to hand-edit."""
        write_list(self.path, [{"pattern": "a.*b", "reason": "literal"}])
        self.assertIsNone(ws.match("aXXXb", self.path))
        self.assertIsNotNone(ws.match("the a.*b thing", self.path))

    def test_the_first_matching_entry_wins(self):
        write_list(self.path, [{"pattern": DOMAIN, "reason": "first"},
                               {"pattern": "registrar", "reason": "second"}])
        self.assertEqual(ws.match(DOMAIN_PUSH, self.path)["why"], "first")

    def test_a_too_short_pattern_is_ignored(self):
        """`MIN_PATTERN_LEN` — a two-character pattern matches nearly every message, so it is dropped
        rather than honoured. The entry costs itself; the send path is untouched."""
        write_list(self.path, [{"pattern": "a", "reason": "far too broad"},
                               {"pattern": "of", "reason": "far too broad"}])
        self.assertEqual(ws.load_patterns(self.path), [])
        self.assertIsNone(ws.match("a message full of ordinary words", self.path))


class FailOpen(ListFixture):
    """**A list that cannot be read suppresses nothing.** Argue with this class before changing it —
    the direction of error is the whole design (see the module docstring)."""

    def test_a_missing_file_suppresses_nothing(self):
        self.assertEqual(ws.load_patterns(os.path.join(self.dir, "nope.json")), [])
        self.assertIsNone(ws.match(DOMAIN_PUSH, os.path.join(self.dir, "nope.json")))

    def test_a_missing_directory_suppresses_nothing(self):
        gone = os.path.join(self.dir, "nowhere", "watch-suppressions.json")
        self.assertIsNone(ws.match(DOMAIN_PUSH, gone))

    def test_an_unreadable_file_suppresses_nothing(self):
        """A directory squatting on the path — `open` raises `IsADirectoryError`/`PermissionError`
        depending on the OS, and neither may reach the send path."""
        os.mkdir(os.path.join(self.dir, "dir.json"))
        self.assertIsNone(ws.match(DOMAIN_PUSH, os.path.join(self.dir, "dir.json")))

    def test_undecodable_bytes_suppress_nothing(self):
        with open(self.path, "wb") as fh:
            fh.write(b"\xff\xfe\x00 not utf-8 \x00")
        self.assertIsNone(ws.match(DOMAIN_PUSH, self.path))

    def test_malformed_json_suppresses_nothing(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"patterns": [{"pattern": "old-shop.example"')  # truncated mid-write
        self.assertEqual(ws.load_patterns(self.path), [])
        self.assertIsNone(ws.match(DOMAIN_PUSH, self.path))

    def test_a_wrong_top_level_shape_suppresses_nothing(self):
        for shape in ([{"pattern": DOMAIN}], DOMAIN, 7, None):
            write_raw(self.path, shape)
            self.assertEqual(ws.load_patterns(self.path), [], shape)
            self.assertIsNone(ws.match(DOMAIN_PUSH, self.path), shape)

    def test_a_patterns_value_that_is_not_a_list_suppresses_nothing(self):
        for value in ({"a": "b"}, DOMAIN, None):
            write_raw(self.path, {"version": 1, "patterns": value})
            self.assertEqual(ws.load_patterns(self.path), [], value)

    def test_malformed_entries_cost_themselves_and_nothing_else(self):
        """A half-written entry drops out; a well-formed one beside it still binds. The file is
        hand-maintained, so partial validity is the realistic failure, not total corruption."""
        write_list(self.path, ["a bare string", 42, None, {"no_pattern_key": "x"},
                               {"pattern": 17}, {"pattern": "   "},
                               {"pattern": DOMAIN, "reason": "kept"}])
        self.assertEqual([r["pattern"] for r in ws.load_patterns(self.path)], [DOMAIN])
        self.assertEqual(ws.match(DOMAIN_PUSH, self.path)["why"], "kept")

    def test_an_empty_list_suppresses_nothing(self):
        write_list(self.path, [])
        self.assertIsNone(ws.match(DOMAIN_PUSH, self.path))

    def test_a_non_string_message_is_never_suppressed(self):
        self.seed()
        for text in (None, 17, [DOMAIN]):
            self.assertIsNone(ws.match(text, self.path), text)


class NeverSuppressible(ListFixture):
    """**🚨 Critical / 🛑 Super-Critical / `Call Me` cannot be suppressed, by any pattern.** Including
    by a pattern written to target them, which is what these seed."""

    def test_a_critical_message_is_never_suppressed(self):
        write_list(self.path, [{"pattern": "boiler", "reason": "a pattern that should not bind"}])
        for marker in ("🚨 Critical", "🛑 Super-Critical", "Call Me"):
            msg = f"{marker}: the boiler inspection is overdue."
            self.assertIsNotNone(ws.never_suppressible(msg), marker)
            self.assertIsNone(ws.match(msg, self.path), marker)

    def test_the_marker_is_matched_case_insensitively(self):
        write_list(self.path, [{"pattern": "watering", "reason": "a pattern that should not bind"}])
        self.assertIsNone(ws.match("call me — the greenhouse watering is due", self.path))

    def test_a_pattern_aimed_straight_at_a_critical_marker_still_cannot_fire(self):
        write_list(self.path, [{"pattern": "🛑 Super-Critical", "reason": "deliberately hostile"}])
        self.assertIsNone(ws.match("🛑 Super-Critical: boiler inspection due.", self.path))

    def test_an_ordinary_message_is_suppressible(self):
        """The other half — without this, the class above would pass on a gate that blocks nothing."""
        self.seed()
        self.assertIsNone(ws.never_suppressible(DOMAIN_PUSH))
        self.assertIsNotNone(ws.match(DOMAIN_PUSH, self.path))


class LiveListOverExample(unittest.TestCase):
    """The owner's live list is gitignored; the tracked example documents the shape and ships empty."""

    def _repoint(self, live, example):
        self.addCleanup(setattr, ws, "LIVE_LIST_PATH", ws.LIVE_LIST_PATH)
        self.addCleanup(setattr, ws, "EXAMPLE_LIST_PATH", ws.EXAMPLE_LIST_PATH)
        ws.LIVE_LIST_PATH, ws.EXAMPLE_LIST_PATH = live, example

    def test_the_live_file_wins_when_it_exists(self):
        d = tempfile.mkdtemp()
        live, example = os.path.join(d, "live.json"), os.path.join(d, "example.json")
        write_list(live, [{"pattern": DOMAIN, "reason": "live"}])
        write_list(example, [])
        self._repoint(live, example)
        self.assertEqual(ws.default_list_path(), live)
        self.assertEqual(ws.match(DOMAIN_PUSH)["why"], "live")

    def test_the_example_is_read_when_there_is_no_live_file(self):
        d = tempfile.mkdtemp()
        live, example = os.path.join(d, "live.json"), os.path.join(d, "example.json")
        write_list(example, [{"pattern": DOMAIN, "reason": "example"}])
        self._repoint(live, example)
        self.assertEqual(ws.default_list_path(), example)
        self.assertEqual(ws.match(DOMAIN_PUSH)["why"], "example")

    def test_neither_file_suppresses_nothing(self):
        d = tempfile.mkdtemp()
        self._repoint(os.path.join(d, "a.json"), os.path.join(d, "b.json"))
        self.assertIsNone(ws.match(DOMAIN_PUSH))


class ShippedExample(unittest.TestCase):
    """The tracked example, read as it ships."""

    def _data(self):
        with open(ws.EXAMPLE_LIST_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_shipped_example_suppresses_nothing(self):
        """A fresh install must not silently drop anything the owner never asked to drop."""
        self.assertEqual(ws.load_patterns(ws.EXAMPLE_LIST_PATH), [])
        self.assertIsNone(ws.match(DOMAIN_PUSH, ws.EXAMPLE_LIST_PATH))

    def test_the_shipped_example_documents_itself(self):
        """`_doc` + `_matching` + `_never`: the file has to say what it is and what it must never
        become, because the next person to edit it will read it, not this."""
        data = self._data()
        for key in ("_doc", "_matching", "_never"):
            self.assertTrue(data.get(key), key)

    def test_the_example_entry_is_well_formed_and_records_why(self):
        """The documented shape must itself be a valid entry — copy it into `patterns` and it binds —
        and it carries a `reason`, because a pattern nobody can name a reason for should be deleted."""
        entry = self._data()["_example_entry"]
        self.assertTrue(entry.get("reason"))
        d = tempfile.mkdtemp()
        path = os.path.join(d, "list.json")
        write_list(path, [entry])
        self.assertEqual([r["pattern"] for r in ws.load_patterns(path)], [entry["pattern"]])


#: An actual import of this module — prose that merely NAMES it (a docstring explaining the two
#: gates) is not a caller.
_IMPORTS_WATCH_SUPPRESS = re.compile(
    r"^\s*(?:import\s+watch_suppress\b|from\s+watch_suppress\s+import\b)", re.MULTILINE)


class ReminderPathIsNotThisDoor(unittest.TestCase):
    """Reminders fire from `sentinel.check_reminders`, which never consults this module. Pinned as an
    ABSENCE against the files, so nobody can "extend" the suppression list into the reminder path
    without this going red."""

    def _source(self, name):
        with open(os.path.join(SCRIPT_DIR, name), "r", encoding="utf-8") as fh:
            return fh.read()

    def _imports_it(self, name):
        return bool(_IMPORTS_WATCH_SUPPRESS.search(self._source(name)))

    def test_the_reminder_fire_path_never_imports_the_suppression_list(self):
        checked = 0
        for module in ("sentinel.py", "reminders_acks.py", "reminders_enqueue.py",
                       "reminders_seed.py", "reminders_roll.py"):
            if not os.path.exists(os.path.join(SCRIPT_DIR, module)):
                continue
            checked += 1
            self.assertFalse(self._imports_it(module), module)
        self.assertGreater(checked, 0)

    def test_the_only_caller_is_the_watch_send_gate(self):
        """The Telegram sender is the one permitted caller (it may not have landed yet — then there
        are none)."""
        callers = [n for n in sorted(os.listdir(SCRIPT_DIR))
                   if n.endswith(".py") and not n.startswith("test_")
                   and n != "watch_suppress.py" and self._imports_it(n)]
        self.assertTrue(set(callers) <= {"telegram_send.py"}, callers)

    def test_no_telegram_or_presence_import(self):
        src = self._source("watch_suppress.py")
        for name in ("import telegram_", "import presence", "from presence", "from telegram_"):
            self.assertNotIn(name, src, name)


if __name__ == "__main__":
    unittest.main()
