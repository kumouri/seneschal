#!/usr/bin/env python3
"""Tests for presence.py's identity-rendered prompts (GROUNDING_TEMPLATE / build_slots).

Two invariants matter here:
  * **No-op when unconfigured** — with no persona/identity.json the rendered grounding and
    slot prompts must be the exact generic prose that used to be hardcoded (this whole
    feature must be invisible to a fresh install), with no identity token left unresolved.
  * **Brace safety** — identity values are substituted at startup, but the grounding is
    still .format()ed at send time for the per-turn tokens ({channel}/{now}/{thread}/{msg}).
    A configured name containing '{' must survive both passes without crashing a chat turn.

Stdlib ``unittest`` only, like the rest of the suite; identities are passed explicitly so
these tests are indifferent to whatever persona/identity.json exists on the host.

Run:  python -m unittest seneschal.scripts.test_presence_grounding   (or)   python test_presence_grounding.py
"""
import os
import sys
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import identity_common as ic  # noqa: E402
import presence as pr  # noqa: E402

NAMED = {"assistant": {"name": "Aria"},
         "owner": {"name": "Sam", "timezone": "America/Chicago"}}


def _format_like_drainer(grounding, msg="hey"):
    """Mirror the drainer's send-time fill of the per-turn tokens (presence.drainer_task)."""
    return grounding.format(channel="telegram".capitalize(), now=pr.local_stamp(),
                            thread="", msg=msg)


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


if __name__ == "__main__":
    unittest.main()
