#!/usr/bin/env python3
"""Tests for presence.py's identity-rendered prompts (GROUNDING_TEMPLATE / build_slots).

The READ FIRST injection (the standing-safety block in the cold-spawn grounding) is tested in the
second half of this module. Two invariants matter for the identity rendering:
  * **No-op when unconfigured** — with no persona/identity.json the rendered grounding and
    slot prompts must be the exact generic prose that used to be hardcoded (this whole
    feature must be invisible to a fresh install), with no identity token left unresolved.
  * **Brace safety** — identity values are substituted at startup, but the grounding is
    still .format()ed at send time for the per-turn tokens ({channel}/{now}/{thread}/{msg}/
    {channel_declare_instruction}/{read_first}/{current_topic_line}).
    A configured name containing '{' must survive both passes without crashing a chat turn.

Stdlib ``unittest`` only, like the rest of the suite; identities are passed explicitly so
these tests are indifferent to whatever persona/identity.json exists on the host.

Run:  python -m unittest seneschal.scripts.test_presence_grounding   (or)   python test_presence_grounding.py
"""
import argparse
import os
import sys
import tempfile
import unittest
from unittest import mock
import unittest.mock
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import identity_common as ic  # noqa: E402
import mouth  # noqa: E402
import presence as pr  # noqa: E402

NAMED = {"assistant": {"name": "Aria"},
         "owner": {"name": "Sam", "timezone": "America/Chicago"}}


def _format_like_drainer(grounding, msg="hey"):
    """Mirror the drainer's send-time fill of the per-turn tokens (presence.drainer_task)."""
    return grounding.format(channel="telegram".capitalize(), now=pr.local_stamp(),
                            thread="", msg=msg, channel_declare_instruction="", read_first="",
                            current_topic_line="")


class GroundingRenderTest(unittest.TestCase):
    def test_default_identity_renders_the_legacy_generic_prose(self):
        g = pr._render_grounding(pr.GROUNDING_TEMPLATE, ic.DEFAULTS)
        # The exact sentences that were hardcoded before identity.json existed — pinned so an
        # unconfigured install stays byte-identical to the pre-identity daemon.
        self.assertTrue(g.startswith(
            "You are the resident assistant — the owner's chief of staff — talking with them "
            "live over {channel}."), g[:120])
        self.assertIn("The current local date and time is {now} (the owner's configured timezone)", g)
        self.assertIn("the assistant", g)
        for token in ("{assistant}", "{owner}", "{tz}"):
            self.assertNotIn(token, g)
        for per_turn in ("{channel}", "{now}", "{thread}", "{msg}"):
            self.assertIn(per_turn, g)  # left for the send-time format

    def test_named_identity_substitutes_all_three_tokens(self):
        g = pr._render_grounding(pr.GROUNDING_TEMPLATE, NAMED)
        self.assertTrue(g.startswith("You are Aria — Sam's chief of staff"), g[:80])
        self.assertIn("(America/Chicago)", g)
        self.assertIn("when Sam acknowledges a", g)
        self.assertIn("If Sam asks you to restart", g)
        for token in ("{assistant}", "{owner}", "{tz}"):
            self.assertNotIn(token, g)

    def test_default_render_formats_at_send_time(self):
        g = pr._render_grounding(pr.GROUNDING_TEMPLATE, ic.DEFAULTS)
        out = _format_like_drainer(g, msg="did I take my meds")
        self.assertIn("did I take my meds", out)
        self.assertNotIn("{msg}", out)

    def test_brace_in_name_survives_send_time_format(self):
        identity = {"assistant": {"name": "Alex {Braces}"}, "owner": {}}
        g = pr._render_grounding(pr.GROUNDING_TEMPLATE, identity)
        for per_turn in ("{channel}", "{now}", "{thread}", "{msg}"):
            self.assertIn(per_turn, g)  # per-turn tokens intact despite the braces in the value
        out = _format_like_drainer(g)  # must not raise (KeyError/IndexError/ValueError)
        self.assertIn("Alex {Braces}", out)  # the doubled braces render back to the literal name

    def test_module_level_grounding_is_rendered(self):
        # Whatever identity the host has, the runnable GROUNDING has no identity tokens left.
        for token in ("{assistant}", "{owner}", "{tz}"):
            self.assertNotIn(token, pr.GROUNDING)


class BuildSlotsTest(unittest.TestCase):
    def test_default_identity_renders_the_legacy_prompts(self):
        slots = pr.build_slots(ic.DEFAULTS)
        self.assertEqual(len(slots), len(pr.SLOTS_TEMPLATE))
        for s in slots:
            self.assertIn("Use the owner's configured timezone.", s["prompt"])
            self.assertNotIn("{tz}", s["prompt"])

    def test_named_identity_substitutes_tz(self):
        slots = pr.build_slots(NAMED)
        for s in slots:
            self.assertIn("Use America/Chicago.", s["prompt"])
            self.assertNotIn("{tz}", s["prompt"])

    def test_times_and_names_are_untouched(self):
        # Slot fire times stay machine-local wall clock — identity only touches the prose.
        slots = pr.build_slots(NAMED)
        self.assertEqual([(s["name"], s["at"]) for s in slots],
                         [(s["name"], s["at"]) for s in pr.SLOTS_TEMPLATE])

    def test_template_is_not_mutated(self):
        before = [dict(s) for s in pr.SLOTS_TEMPLATE]
        pr.build_slots(NAMED)
        self.assertEqual(pr.SLOTS_TEMPLATE, before)

    def test_braces_in_values_stay_literal(self):
        # Slot prompts are never .format()ed, so values are substituted raw — no doubling.
        slots = pr.build_slots({"assistant": {}, "owner": {"timezone": "Zone/{odd}"}})
        self.assertIn("Use Zone/{odd}.", slots[0]["prompt"])
        self.assertNotIn("{{", slots[0]["prompt"])


class BuildSeedPromptTest(unittest.TestCase):
    """The exact-time reminder SEED prompt (SEED_PROMPT_TEMPLATE → build_seed_prompt), pinned like the
    slot prompts: identity-token rendered at startup, handed to `claude -p` verbatim (never .format()ed),
    and carrying zero baked-in personal identity."""

    def test_default_identity_renders_the_generic_tz_phrase(self):
        seed = pr.build_seed_prompt(ic.DEFAULTS)
        self.assertIn("Use the owner's configured timezone.", seed)
        self.assertNotIn("{tz}", seed)

    def test_named_identity_substitutes_the_configured_tz_label(self):
        seed = pr.build_seed_prompt(NAMED)
        self.assertIn("Use America/Chicago.", seed)
        for token in ("{assistant}", "{owner}", "{tz}"):
            self.assertNotIn(token, seed)

    # The upstream assistant/owner names, assembled at runtime so the repo's own PII sweep (which
    # greps source text for these very literals) can't match its own guard.
    LEGACY_NAMES = ("".join(("Mar", "go")), "".join(("Cer", "yce")))

    def test_template_carries_no_baked_in_identity(self):
        # The tracked template itself must be identity-free: any zone/name appears only via the
        # {tz}/{assistant}/{owner} tokens, never as a literal.
        for literal in self.LEGACY_NAMES + ("America/Chicago",):
            self.assertNotIn(literal, pr.SEED_PROMPT_TEMPLATE)

    def test_module_level_seed_prompt_is_rendered(self):
        # Whatever identity the host has, the runnable SEED_PROMPT has no identity tokens left —
        # and no literal legacy names (the configured tz label is the only zone that may appear).
        for token in ("{assistant}", "{owner}", "{tz}"):
            self.assertNotIn(token, pr.SEED_PROMPT)
        for literal in self.LEGACY_NAMES:
            self.assertNotIn(literal, pr.SEED_PROMPT)

    def test_braces_in_values_stay_literal(self):
        # Like slot prompts (and unlike the grounding), the seed is never .format()ed, so values are
        # substituted raw — no doubling.
        seed = pr.build_seed_prompt({"assistant": {}, "owner": {"timezone": "Zone/{odd}"}})
        self.assertIn("Use Zone/{odd}.", seed)
        self.assertNotIn("{{", seed)


class WarnTzMismatchTest(unittest.TestCase):
    def _machine_offset(self):
        return datetime.now(timezone.utc).astimezone().utcoffset()

    def test_unset_timezone_is_silent(self):
        logs = []
        pr.warn_tz_mismatch(ic.DEFAULTS, logs.append)
        self.assertEqual(logs, [])

    def test_unresolvable_timezone_skips_silently(self):
        logs = []  # no tzdata on Windows / a typo'd key — best-effort means no noise, no raise
        pr.warn_tz_mismatch({"owner": {"timezone": "Not/AZone"}}, logs.append)
        self.assertEqual(logs, [])

    def test_mismatch_logs_one_warning(self):
        off = self._machine_offset()
        fake = timezone(off + (timedelta(hours=-1) if off >= timedelta(hours=13) else timedelta(hours=1)))
        with unittest.mock.patch("zoneinfo.ZoneInfo", lambda key: fake):
            logs = []
            pr.warn_tz_mismatch({"owner": {"timezone": "America/Chicago"}}, logs.append)
        self.assertEqual(len(logs), 1)
        self.assertIn("machine-local", logs[0])
        self.assertIn("America/Chicago", logs[0])

    def test_matching_offset_is_silent(self):
        fake = timezone(self._machine_offset())
        with unittest.mock.patch("zoneinfo.ZoneInfo", lambda key: fake):
            logs = []
            pr.warn_tz_mismatch({"owner": {"timezone": "America/Chicago"}}, logs.append)
        self.assertEqual(logs, [])


# ------------------------------------------------------------------ READ FIRST (standing safety)
#
# `GROUNDING` merely *mentioning* a standing-safety source carries none of it, so its "do NOT re-raise
# this cold" corrections are reachable only by a tool read that nothing forces. These tests pin that
# the block is actually injected, verbatim; that an absent source is a silent no-op (the COMMON path
# on a fresh install); and that the cap truncates on a line boundary, visibly.
import standing_safety as ss  # noqa: E402

# Shaped like a real leftover digest, with a `##` section on either side — the block is never the
# first or last thing in the file, and the reader has to stop at the next same-level heading.
DIGEST = """# Assistant — context digest

**Last consolidated:** (Dream).

## 💊 READ FIRST — standing safety items (do NOT re-raise cold)
- **The vendor dispute is CLOSED BY CHOICE — the chase is OVER (the owner's explicit call).** Do NOT
  chase, nudge, reopen, or offer to draft a message.
- **The team offsite (Mon 08-03) is PAST and the gift is handled** — do NOT re-announce.

## Yesterday in one breath
Quiet-but-solid. This must not be injected.
"""


def _digest(tmp, text=DIGEST):
    with open(os.path.join(tmp, pr.DIGEST_FILE), "w", encoding="utf-8") as fh:
        fh.write(text)
    return tmp


class ReadFirstInjectionTests(unittest.TestCase):
    """(1) The block reaches the prompt, verbatim, and nothing else does."""

    def test_section_is_extracted_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = pr.read_first_block(_digest(tmp))
        self.assertIn("The vendor dispute is CLOSED BY CHOICE — the chase is OVER", block)
        self.assertIn("do NOT re-announce", block)
        self.assertTrue(block.startswith("## 💊 READ FIRST"), block[:40])
        # The wording is load-bearing, so it is copied and not restated: every line of the source
        # section appears in the output exactly as written.
        for line in DIGEST.splitlines()[4:8]:
            self.assertIn(line, block)

    def test_stops_at_the_next_same_level_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = pr.read_first_block(_digest(tmp))
        self.assertNotIn("Yesterday in one breath", block)
        self.assertNotIn("This must not be injected", block)

    def test_deeper_headings_stay_inside_the_section(self):
        # `##` opens the block, so a `###` under it is part of it. Ending at "the next heading" would
        # cut the section at its first sub-point and lose everything after it — silently.
        text = DIGEST.replace("- **The team offsite", "### Work\n- **The team offsite")
        with tempfile.TemporaryDirectory() as tmp:
            block = pr.read_first_block(_digest(tmp, text))
        self.assertIn("### Work", block)
        self.assertIn("do NOT re-announce", block)

    def test_grounding_carries_the_block_and_its_framing(self):
        with tempfile.TemporaryDirectory() as tmp:
            injected = pr.read_first_grounding(_digest(tmp))
            prompt = pr.GROUNDING.format(channel="Telegram", now="2026-08-09 10:00",
                                         read_first=injected, thread="", channel_declare_instruction="",
                                         current_topic_line="", msg="hey")
        self.assertIn("VERBATIM", injected)          # the framing says where it came from
        self.assertIn("The vendor dispute is CLOSED BY CHOICE", prompt)
        # ...and it lands BEFORE the owner's message, not after it: standing instructions the model reads
        # after the thing it is answering are instructions it has already answered without.
        self.assertLess(prompt.index("The vendor dispute is CLOSED"), prompt.index("The owner just said:"))


class AbsentIsSilentTests(unittest.TestCase):
    """(2) Every unhappy path is a no-op. A fresh checkout has no digest at all."""

    def test_absent_file_is_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pr.read_first_block(tmp), "")
            self.assertEqual(pr.read_first_grounding(tmp), "")

    def test_absent_directory_is_empty_string(self):
        self.assertEqual(pr.read_first_grounding(os.path.join(SCRIPT_DIR, "no-such-dir-xyz")), "")

    def test_digest_without_the_section_is_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = pr.read_first_block(_digest(tmp, "# Digest\n\n## Open loops\n- a thing\n"))
        self.assertEqual(block, "")

    def test_heading_with_no_body_is_empty_string(self):
        # An empty block is not a standing-safety block; injecting a bare heading would spend the
        # framing line to say nothing.
        with tempfile.TemporaryDirectory() as tmp:
            block = pr.read_first_block(_digest(tmp, "# D\n\n## READ FIRST\n\n## Open loops\n- x\n"))
        self.assertEqual(block, "")

    def test_absent_block_leaves_the_prompt_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = pr.GROUNDING.format(channel="Telegram", now="N", read_first=pr.read_first_grounding(tmp),
                                      thread="", channel_declare_instruction="", current_topic_line="",
                                      msg="hey")
        want = pr.GROUNDING.format(channel="Telegram", now="N", read_first="", thread="",
                                   channel_declare_instruction="", current_topic_line="", msg="hey")
        self.assertEqual(got, want)

    def test_no_exception_escapes(self):
        # The general claim, not just the three unhappy paths above: whatever goes wrong in there, the
        # spawn path gets a string. If this ever needs a try/except at the call site, this test is why.
        with tempfile.TemporaryDirectory() as tmp:
            _digest(tmp)
            for boom in (OSError("disk"), UnicodeDecodeError("utf-8", b"", 0, 1, "bad"),
                         MemoryError(), RuntimeError("something nobody predicted")):
                with mock.patch("builtins.open", side_effect=boom):
                    self.assertEqual(pr.read_first_block(tmp), "")
                    self.assertEqual(pr.read_first_grounding(tmp), "")


class CapTests(unittest.TestCase):
    """(3) The cap holds, and what it cuts is whole lines with a marker saying so."""

    def test_under_the_cap_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = pr.read_first_block(_digest(tmp), max_bytes=pr.READ_FIRST_MAX_BYTES)
        self.assertNotIn("truncated", block)
        self.assertLessEqual(len(block.encode("utf-8")), pr.READ_FIRST_MAX_BYTES)

    def test_cap_is_a_hard_ceiling(self):
        big = ("# D\n\n## READ FIRST\n" + "".join(f"- line {i} " + "x" * 60 + "\n" for i in range(200))
               + "\n## Open loops\n- x\n")
        with tempfile.TemporaryDirectory() as tmp:
            for cap in (300, 900, 2000, 4000):
                block = pr.read_first_block(_digest(tmp, big), max_bytes=cap)
                self.assertLessEqual(len(block.encode("utf-8")), cap, f"cap={cap}")
                self.assertIn("truncated", block, f"cap={cap}")

    def test_truncation_lands_on_a_line_boundary(self):
        big = ("# D\n\n## READ FIRST\n" + "".join(f"- line {i} " + "x" * 60 + "\n" for i in range(200))
               + "\n## Open loops\n- x\n")
        with tempfile.TemporaryDirectory() as tmp:
            block = pr.read_first_block(_digest(tmp, big), max_bytes=1500)
        lines = block.rstrip("\n").split("\n")
        self.assertTrue(lines[-1].startswith("[…"), lines[-1])       # the marker is the last line
        self.assertTrue(lines[-1].endswith("…]"), lines[-1])
        # Every surviving content line is a WHOLE line of the source — nothing was cut mid-sentence.
        source = set(big.split("\n"))
        for line in lines[:-1]:
            self.assertIn(line, source)

    def test_cap_too_small_for_even_one_line_is_a_no_op(self):
        # Rather than emitting a marker and no content, which would spend bytes to say nothing.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pr.read_first_block(_digest(tmp), max_bytes=40), "")


class StandingSafetyPreferenceTests(unittest.TestCase):
    """`state/standing-safety.json` takes over the injection the moment it holds an active
    item, and cedes back to the digest read the moment it does not — the fallback this ruling exists
    to make safe to ship before the one-shot migration ever runs."""

    def test_an_active_store_item_wins_over_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            _digest(tmp)  # a real digest with its own READ FIRST content sits right there too
            ss.add(tmp, text="Store-sourced instruction.", source="test", retire_when="standing")
            block = pr.read_first_block(tmp)
        self.assertTrue(block.startswith(ss.HEADING))
        self.assertIn("Store-sourced instruction.", block)
        # the digest's own content must NOT also leak in — one source wins, not both
        self.assertNotIn("The vendor dispute is CLOSED BY CHOICE", block)

    def test_an_absent_store_falls_back_to_the_digest_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = pr.read_first_block(_digest(tmp))
        self.assertTrue(block.startswith("## \U0001F48A READ FIRST"))
        self.assertIn("The vendor dispute is CLOSED BY CHOICE", block)

    def test_an_empty_store_falls_back_to_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ss._save(tmp, {"schema": ss.SCHEMA, "items": []})
            block = pr.read_first_block(_digest(tmp))
        self.assertIn("The vendor dispute is CLOSED BY CHOICE", block)

    def test_a_store_with_only_retired_items_falls_back_to_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, item_id = ss.add(tmp, text="gone", source="test", retire_when="standing")
            ss.retire(tmp, item_id=item_id, reason="resolved")
            block = pr.read_first_block(_digest(tmp))
        self.assertIn("The vendor dispute is CLOSED BY CHOICE", block)
        self.assertNotIn("gone", block)

    def test_a_broken_store_falls_back_to_the_digest_rather_than_going_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ss, "render", side_effect=RuntimeError("boom")):
                block = pr.read_first_block(_digest(tmp))
        self.assertIn("The vendor dispute is CLOSED BY CHOICE", block)

    def test_no_store_and_no_digest_is_still_a_silent_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pr.read_first_block(tmp), "")
            self.assertEqual(pr.read_first_grounding(tmp), "")

    def test_the_store_path_is_capped_and_truncated_same_as_the_digest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(200):
                ss.add(tmp, text=f"line {i} " + "x" * 60, source="test", retire_when="standing")
            block = pr.read_first_block(tmp, max_bytes=1500)
        self.assertLessEqual(len(block.encode("utf-8")), 1500)
        self.assertIn("truncated", block)
        self.assertIn(ss.STORE_FILE, block)


class ReadFirstMigrationShimTests(unittest.TestCase):
    """docs/read-first-retirement-spec.md: the digest is retired and its fallback in `read_first_block`
    is a MIGRATION SHIM, not standing behavior. `check_read_first_migration` runs once per boot; it
    latches the shim off (permanently) once the store has held active items for two consecutive boots,
    and until then it loudly logs + nudges once per boot while the store is still empty AND a leftover
    digest exists. A fresh install (no digest) has nothing to migrate and stays silent."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _args(self, **over):
        base = dict(state_dir=self.dir, telegram_env="unused.env", stub_send=False)
        base.update(over)
        return argparse.Namespace(**base)

    def test_a_populated_store_needs_no_digest_at_all(self):
        ss.add(self.dir, text="Store item.", source="test", retire_when="standing")
        logs = []
        pr.check_read_first_migration(self.dir, self._args(), logs.append)
        self.assertEqual(mouth.read_assertions(self.dir), [])  # no nudge queued
        self.assertFalse(pr.read_first_shim_disabled(self.dir))  # only one boot so far
        self.assertEqual(pr.read_first_block(self.dir), ss.render(self.dir))

    def test_a_fresh_install_with_no_digest_is_silent(self):
        """The shipped default: no store, no leftover digest — nothing to migrate, nobody to nudge."""
        logs = []
        pr.check_read_first_migration(self.dir, self._args(), logs.append)
        self.assertEqual(logs, [])
        mouth.drain(self.dir, send=lambda s, t, i: True)
        self.assertEqual(mouth.read_assertions(self.dir), [])

    def test_an_empty_store_logs_loudly_and_queues_one_nudge(self):
        _digest(self.dir)
        logs = []
        pr.check_read_first_migration(self.dir, self._args(), logs.append)
        self.assertTrue(any("empty" in line.lower() and "standing-safety" in line.lower()
                             for line in logs), logs)
        mouth.drain(self.dir, send=lambda s, t, i: True)
        rows = mouth.read_assertions(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "nudge")
        self.assertIn("import-digest", rows[0]["text"])

    def test_an_empty_store_still_falls_back_to_the_digest_for_this_boot(self):
        _digest(self.dir)
        pr.check_read_first_migration(self.dir, self._args(), lambda *_: None)
        block = pr.read_first_block(self.dir)
        self.assertIn("The vendor dispute is CLOSED BY CHOICE", block)

    def test_self_disables_after_two_consecutive_nonempty_boots(self):
        ss.add(self.dir, text="Store item.", source="test", retire_when="standing")
        logs1, logs2 = [], []
        pr.check_read_first_migration(self.dir, self._args(), logs1.append)
        self.assertFalse(pr.read_first_shim_disabled(self.dir))
        pr.check_read_first_migration(self.dir, self._args(), logs2.append)
        self.assertTrue(pr.read_first_shim_disabled(self.dir))
        self.assertTrue(any("self-disabled" in line.lower() for line in logs2), logs2)

    def test_once_latched_the_digest_never_reads_again_even_if_the_store_empties(self):
        ss.add(self.dir, text="Store item.", source="test", retire_when="standing")
        pr.check_read_first_migration(self.dir, self._args(), lambda *_: None)
        pr.check_read_first_migration(self.dir, self._args(), lambda *_: None)
        self.assertTrue(pr.read_first_shim_disabled(self.dir))
        ss._save(self.dir, {"schema": ss.SCHEMA, "items": []})  # store goes empty after latching
        _digest(self.dir)  # a real digest sits right there, with real content
        self.assertEqual(pr.read_first_block(self.dir), "")

    def test_a_gap_resets_the_consecutive_count(self):
        ss.add(self.dir, text="Store item.", source="test", retire_when="standing")
        pr.check_read_first_migration(self.dir, self._args(), lambda *_: None)  # 1/2
        ss._save(self.dir, {"schema": ss.SCHEMA, "items": []})
        pr.check_read_first_migration(self.dir, self._args(), lambda *_: None)  # empty boot resets
        ss.add(self.dir, text="Store item 2.", source="test", retire_when="standing")
        pr.check_read_first_migration(self.dir, self._args(), lambda *_: None)  # 1/2 again
        self.assertFalse(pr.read_first_shim_disabled(self.dir))

    def test_already_disabled_is_a_fast_no_op(self):
        ss.add(self.dir, text="Store item.", source="test", retire_when="standing")
        pr.check_read_first_migration(self.dir, self._args(), lambda *_: None)
        pr.check_read_first_migration(self.dir, self._args(), lambda *_: None)
        self.assertTrue(pr.read_first_shim_disabled(self.dir))
        logs = []
        pr.check_read_first_migration(self.dir, self._args(), logs.append)
        self.assertEqual(logs, [])
        self.assertEqual(mouth.read_assertions(self.dir), [])

    def test_the_boot_check_never_raises(self):
        with mock.patch.object(ss, "has_active_items", side_effect=RuntimeError("boom")):
            pr.check_read_first_migration(self.dir, self._args(), lambda *_: None)
        self.assertFalse(pr.read_first_shim_disabled(self.dir))

    def test_a_stub_send_boot_queues_no_nudge(self):
        pr.check_read_first_migration(self.dir, self._args(stub_send=True), lambda *_: None)
        self.assertEqual(mouth.read_assertions(self.dir), [])


if __name__ == "__main__":
    unittest.main()
