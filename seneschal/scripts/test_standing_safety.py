#!/usr/bin/env python3
"""Tests for `standing_safety.py` — the READ FIRST items' own store
(`../docs/read-first-retirement-spec.md`).

Families:

1. **`add` / `retire_when` classification.** Only three shapes are ever accepted — a date, a
   `"when <decision> lands"` sentence, or the literal `"standing"` — and a fourth is refused at write
   time rather than stored unclassified, the exact gap that let the digest's items pile up.
2. **`retire`.** Never deletes a row; refuses an unknown id and a double-retirement.
3. **`due_items`.** Only a DATE on or before the owner's activity day (`clock.local_today`) is due;
   a decision-gated or standing item never is.
4. **`render()` — the heading plus verbatim bullets**, and its fail-open contract (no store / empty
   store / all-retired -> `""`, never a raise).
5. **`import-digest`.** One-shot, folds bullets (including a wrapped continuation line) into rows,
   and refuses a second run without `--force`.

Stdlib ``unittest`` only; no daemon, no network.
Run:  python -m unittest discover -s seneschal/scripts -p "test_standing_safety.py"   (from the repo root)
"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import standing_safety as ss  # noqa: E402

NOW = datetime(2026, 1, 15, 21, 41, 0, tzinfo=timezone.utc)


class RetireWhenClassification(unittest.TestCase):
    def test_a_date_classifies_as_date(self):
        self.assertEqual(ss.classify_retire_when("2026-01-20"), "date")

    def test_the_decision_gate_sentence_classifies_as_decision(self):
        self.assertEqual(ss.classify_retire_when("when travel-policy-decision lands"), "decision")

    def test_standing_is_case_insensitive(self):
        self.assertEqual(ss.classify_retire_when("standing"), "standing")
        self.assertEqual(ss.classify_retire_when("Standing"), "standing")

    def test_garbage_is_refused(self):
        for bad in ("soon", "2026-13-40", "next week", "", None, 5, "when lands"):
            self.assertIsNone(ss.classify_retire_when(bad), bad)


class HeadingTests(unittest.TestCase):
    def test_the_heading_is_the_neutral_read_first_line(self):
        self.assertEqual(ss.HEADING, "## READ FIRST — standing safety items (do NOT re-raise cold)")

    def test_the_heading_matches_the_import_digest_section_regex(self):
        # A digest that was itself seeded from this heading must still import.
        self.assertTrue(ss._DIGEST_HEADING_RE.match(ss.HEADING))


class AddTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_valid_add_round_trips(self):
        ok, item_id = ss.add(self.tmp, text="Never schedule anything before 9am.",
                              source="scheduling-preferences.md", retire_when="standing", now=NOW)
        self.assertTrue(ok)
        items = ss.list_items(self.tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], item_id)
        self.assertEqual(items[0]["text"], "Never schedule anything before 9am.")
        self.assertEqual(items[0]["source"], "scheduling-preferences.md")
        self.assertEqual(items[0]["retire_when"], "standing")
        self.assertIsNone(items[0]["retired_at"])
        self.assertIsNone(items[0]["retired_reason"])
        self.assertEqual(items[0]["added"], "2026-01-15T21:41:00Z")

    def test_missing_text_is_refused(self):
        ok, reason = ss.add(self.tmp, text="  ", source="x", retire_when="standing")
        self.assertFalse(ok)
        self.assertIn("TEXT", reason)
        self.assertEqual(ss.list_items(self.tmp), [])

    def test_missing_source_is_refused(self):
        ok, reason = ss.add(self.tmp, text="x", source="", retire_when="standing")
        self.assertFalse(ok)
        self.assertIn("source", reason)

    def test_a_malformed_retire_when_is_refused_not_stored_unclassified(self):
        ok, reason = ss.add(self.tmp, text="x", source="y", retire_when="whenever")
        self.assertFalse(ok)
        self.assertIn("retire-when", reason)
        self.assertEqual(ss.list_items(self.tmp), [])

    def test_two_adds_both_land(self):
        ss.add(self.tmp, text="one", source="s", retire_when="standing", now=NOW)
        ss.add(self.tmp, text="two", source="s", retire_when="2026-01-20", now=NOW)
        self.assertEqual(len(ss.list_items(self.tmp)), 2)


class RetireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _, self.item_id = ss.add(self.tmp, text="closed thread", source="s", retire_when="standing",
                                  now=NOW)

    def test_retire_marks_but_never_deletes(self):
        ok, _ = ss.retire(self.tmp, item_id=self.item_id, reason="the thread is closed", now=NOW)
        self.assertTrue(ok)
        active = ss.list_items(self.tmp)
        self.assertEqual(active, [])  # no longer active
        everything = ss.list_items(self.tmp, include_retired=True)
        self.assertEqual(len(everything), 1)  # but the row still exists
        self.assertEqual(everything[0]["retired_reason"], "the thread is closed")
        self.assertEqual(everything[0]["retired_at"], "2026-01-15T21:41:00Z")

    def test_unknown_id_is_refused(self):
        ok, reason = ss.retire(self.tmp, item_id="ss-nope", reason="x")
        self.assertFalse(ok)
        self.assertIn("no item", reason)

    def test_missing_reason_is_refused(self):
        ok, reason = ss.retire(self.tmp, item_id=self.item_id, reason="")
        self.assertFalse(ok)
        self.assertIn("reason", reason)

    def test_double_retirement_is_refused(self):
        ss.retire(self.tmp, item_id=self.item_id, reason="first", now=NOW)
        ok, reason = ss.retire(self.tmp, item_id=self.item_id, reason="second", now=NOW)
        self.assertFalse(ok)
        self.assertIn("already retired", reason)
        # the FIRST reason survives — a refused second retirement must not overwrite it
        row = ss.list_items(self.tmp, include_retired=True)[0]
        self.assertEqual(row["retired_reason"], "first")


class DueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_past_date_is_due(self):
        ss.add(self.tmp, text="past", source="s", retire_when="2026-01-01", now=NOW)
        due = ss.due_items(self.tmp, now=NOW)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["text"], "past")

    def test_a_future_date_is_not_due(self):
        ss.add(self.tmp, text="future", source="s", retire_when="2026-12-01", now=NOW)
        self.assertEqual(ss.due_items(self.tmp, now=NOW), [])

    def test_due_is_judged_against_the_owners_activity_day(self):
        # The boundary is `clock.local_today` (owner tz + dayBoundaryHour), never a hard-coded zone:
        # an item dated the activity day itself is due; the next day's is not.
        ss.add(self.tmp, text="today", source="s", retire_when="2026-01-15", now=NOW)
        ss.add(self.tmp, text="tomorrow", source="s", retire_when="2026-01-16", now=NOW)
        with mock.patch.object(ss.clock, "local_today", return_value=date(2026, 1, 15)) as lt:
            due = ss.due_items(self.tmp, now=NOW)
        lt.assert_called_once_with(now=NOW)
        self.assertEqual([d["text"] for d in due], ["today"])

    def test_a_decision_gated_item_is_never_due(self):
        # This module cannot verify a decision landed — a decision-gated item is Dream's to check.
        ss.add(self.tmp, text="decision-gated", source="s",
               retire_when="when travel-policy-decision lands", now=NOW)
        self.assertEqual(ss.due_items(self.tmp, now=NOW), [])

    def test_a_standing_item_is_never_due(self):
        ss.add(self.tmp, text="forever", source="s", retire_when="standing", now=NOW)
        self.assertEqual(ss.due_items(self.tmp, now=NOW), [])

    def test_a_retired_item_is_not_due_again(self):
        _, item_id = ss.add(self.tmp, text="past", source="s", retire_when="2026-01-01", now=NOW)
        ss.retire(self.tmp, item_id=item_id, reason="already handled", now=NOW)
        self.assertEqual(ss.due_items(self.tmp, now=NOW), [])


class RenderTests(unittest.TestCase):
    """(4) The heading, then one verbatim bullet per active item — nothing else."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_no_store_renders_empty(self):
        self.assertEqual(ss.render(self.tmp), "")

    def test_empty_store_renders_empty(self):
        ss._save(self.tmp, {"schema": ss.SCHEMA, "items": []})
        self.assertEqual(ss.render(self.tmp), "")

    def test_all_retired_renders_empty(self):
        _, item_id = ss.add(self.tmp, text="x", source="s", retire_when="standing", now=NOW)
        ss.retire(self.tmp, item_id=item_id, reason="done", now=NOW)
        self.assertEqual(ss.render(self.tmp), "")

    def test_active_items_render_the_heading_and_bullets_verbatim(self):
        ss.add(self.tmp, text="Never schedule anything before 9am.", source="s1",
               retire_when="standing", now=NOW)
        ss.add(self.tmp, text="The vendor dispute is PAUSED BY CHOICE — the chase is OVER.",
               source="s2", retire_when="standing", now=NOW)
        block = ss.render(self.tmp)
        self.assertTrue(block.startswith(ss.HEADING))
        self.assertIn("- Never schedule anything before 9am.", block)
        self.assertIn("- The vendor dispute is PAUSED BY CHOICE — the chase is OVER.", block)
        # no bookkeeping metadata leaks into the model-facing text
        self.assertNotIn("source", block)
        self.assertNotIn("s1", block)
        self.assertNotIn(NOW.strftime("%Y"), block)

    def test_a_retired_item_does_not_render_but_an_active_sibling_still_does(self):
        ss.add(self.tmp, text="stays", source="s", retire_when="standing", now=NOW)
        _, gone_id = ss.add(self.tmp, text="goes", source="s", retire_when="standing", now=NOW)
        ss.retire(self.tmp, item_id=gone_id, reason="resolved", now=NOW)
        block = ss.render(self.tmp)
        self.assertIn("- stays", block)
        self.assertNotIn("goes", block)

    def test_render_never_raises_on_a_corrupt_store(self):
        path = ss.store_path(self.tmp)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(ss.render(self.tmp), "")

    def test_render_never_raises_when_open_itself_explodes(self):
        ss.add(self.tmp, text="x", source="s", retire_when="standing", now=NOW)
        with mock.patch("builtins.open", side_effect=OSError("disk")):
            self.assertEqual(ss.render(self.tmp), "")


class ImportDigestTests(unittest.TestCase):
    DIGEST = """# Context digest

## READ FIRST — standing safety items (do NOT re-raise cold)
- **The vendor dispute is PAUSED BY CHOICE — the chase is OVER (the owner's explicit call).** Do NOT
  chase, nudge, reopen, or offer to draft a follow-up.
- **The anniversary dinner is PAST and the gift is handled** — do NOT re-announce.

## Yesterday in one breath
This must not be imported.
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.digest_path = os.path.join(self.tmp, "context-digest.md")
        with open(self.digest_path, "w", encoding="utf-8") as fh:
            fh.write(self.DIGEST)

    def test_imports_each_bullet_as_its_own_item_with_the_wrapped_line_folded_in(self):
        ok, message, count = ss.import_digest(self.tmp, digest_path=self.digest_path, now=NOW)
        self.assertTrue(ok, message)
        self.assertEqual(count, 2)
        items = ss.list_items(self.tmp)
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]["text"].startswith("**The vendor dispute is PAUSED BY CHOICE"))
        # the wrapped continuation line joined the bullet above it
        self.assertIn("offer to draft a follow-up.", items[0]["text"])
        self.assertNotIn("\n", items[0]["text"])
        self.assertTrue(items[1]["text"].startswith("**The anniversary dinner"))
        for item in items:
            self.assertEqual(item["source"], "context-digest.md")
            self.assertEqual(item["retire_when"], "standing")

    def test_an_emoji_prefixed_heading_still_imports(self):
        # Older digests may carry a decorative emoji before READ FIRST; the section regex allows it.
        path = os.path.join(self.tmp, "emoji.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# D\n\n## ⚠️ READ FIRST — items\n- one\n\n## Next\n- not this\n")
        ok, message, count = ss.import_digest(self.tmp, digest_path=path, now=NOW)
        self.assertTrue(ok, message)
        self.assertEqual([i["text"] for i in ss.list_items(self.tmp)], ["one"])

    def test_the_section_after_read_first_is_never_imported(self):
        ss.import_digest(self.tmp, digest_path=self.digest_path, now=NOW)
        texts = " ".join(i["text"] for i in ss.list_items(self.tmp))
        self.assertNotIn("must not be imported", texts)

    def test_a_second_run_is_refused_without_force(self):
        ss.import_digest(self.tmp, digest_path=self.digest_path, now=NOW)
        ok, message, count = ss.import_digest(self.tmp, digest_path=self.digest_path, now=NOW)
        self.assertFalse(ok)
        self.assertEqual(count, 0)
        self.assertIn("one-shot", message)
        self.assertEqual(len(ss.list_items(self.tmp)), 2)  # unchanged

    def test_force_allows_a_second_run(self):
        ss.import_digest(self.tmp, digest_path=self.digest_path, now=NOW)
        ok, message, count = ss.import_digest(self.tmp, digest_path=self.digest_path, force=True, now=NOW)
        self.assertTrue(ok, message)
        self.assertEqual(count, 2)
        self.assertEqual(len(ss.list_items(self.tmp)), 4)

    def test_a_missing_digest_is_refused_cleanly(self):
        ok, message, count = ss.import_digest(self.tmp, digest_path=os.path.join(self.tmp, "nope.md"))
        self.assertFalse(ok)
        self.assertEqual(count, 0)
        self.assertEqual(ss.list_items(self.tmp), [])

    def test_a_digest_with_no_read_first_bullets_is_refused(self):
        empty_path = os.path.join(self.tmp, "empty.md")
        with open(empty_path, "w", encoding="utf-8") as fh:
            fh.write("# D\n\n## Open loops\n- a thing\n")
        ok, message, count = ss.import_digest(self.tmp, digest_path=empty_path)
        self.assertFalse(ok)
        self.assertEqual(count, 0)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_add_then_render_then_retire_round_trips_through_main(self):
        rc = ss.main(["--state-dir", self.tmp, "add", "text here", "--source", "s",
                      "--retire-when", "standing"])
        self.assertEqual(rc, 0)
        items = ss.list_items(self.tmp)
        self.assertEqual(len(items), 1)
        rc = ss.main(["--state-dir", self.tmp, "render"])
        self.assertEqual(rc, 0)
        rc = ss.main(["--state-dir", self.tmp, "retire", items[0]["id"], "--reason", "done"])
        self.assertEqual(rc, 0)
        self.assertEqual(ss.list_items(self.tmp), [])

    def test_cli_add_refusal_exits_2(self):
        rc = ss.main(["--state-dir", self.tmp, "add", "text", "--source", "s",
                      "--retire-when", "garbage"])
        self.assertEqual(rc, 2)

    def test_cli_retire_of_unknown_id_exits_1(self):
        rc = ss.main(["--state-dir", self.tmp, "retire", "ss-nope", "--reason", "x"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
