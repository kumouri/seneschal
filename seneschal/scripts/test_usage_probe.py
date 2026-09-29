#!/usr/bin/env python3
"""Tests for usage_probe.py — the plan-meter reading (seneschal/docs/usage-telemetry-spec.md phase 1).

What each class is guarding, because "make it green" is the wrong instinct for most of these:

* ``ParseTheVerbatimSample``   — the §3.1 capture, byte-for-byte, and the two real captures agreeing.
* ``ParsingRules``            — one test per §3.2 rule, each with the observation that forces it:
                                reordered lines, a missing ``Top skills``, a swapped separator, an
                                unknown meter label, the sidebar's *"5-hour limit"* wording.
* ``Fingerprint``             — §3.4. Numbers must NOT move it and wording MUST. Without both halves
                                "it still parses" and "nothing changed" are the same observation.
* ``FailureTaxonomy``         — one test per §3.3 outcome, plus **invariant 2 asserted directly**:
                                a non-ok row carries no meter field at all — not 0, not null.
* ``IntervalAndActivity``     — the window is explicit, a gap is flagged, and a first-ever reading
                                carries NO activity rather than an assumed window.
* ``WhoseMeterIsIt``          — §4.2.1. The ``account`` block on every outcome, unknown as a value,
                                the label map degrading to nothing, the fingerprint, and the
                                ``changed`` boundary that tells a reader two rows are two meters.
* ``NoSpawnNoSpend``          — the runner is injected everywhere; nothing here spawns ``claude``,
                                touches the network, or writes outside its own temp dir.

Every test builds its own temp state dir and passes it explicitly. There is no default state dir in
usage_probe at all — a flag that defaults to the live one turns a test into a live writer, which has
bitten this repo before.

**The same trap is closed on the identity read.** ``setUpModule`` repoints
``CLAUDE_JSON_PATH`` / ``CREDENTIALS_PATH`` at paths that do not exist, so no test in this file can
read the host's real credentials *even if a call site forgets to inject a reader* — belt as well as
braces, because the per-call seam is exactly the thing a future edit adds a call site without.

Run:  python -m unittest test_usage_probe
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import usage_probe as up  # noqa: E402

MIDDOT = "·"
EMDASH = "—"

# Obviously-fake identity. A real account uuid, email or fingerprint may not reach a tracked file
# (spec §6.3), and a fixture is a tracked file.
FAKE_UUID = "00000000-0000-0000-0000-000000000001"
OTHER_UUID = "00000000-0000-0000-0000-000000000002"
FAKE_ACCOUNT = {"known": True, "account_uuid": FAKE_UUID, "email": "someone@example.com",
                "credential_fingerprint": "abc123abc123",
                "source": "<fake>", "credential_source": "<fake>"}


def fake_reader(account=None):
    """`read_account`'s seam, injected exactly as `runner` is. Returns a fresh copy each call, so a
    test cannot accidentally share one mutable block between two rows."""
    payload = dict(FAKE_ACCOUNT if account is None else account)
    return lambda **kwargs: dict(payload)


_REAL_PATHS = (up.CLAUDE_JSON_PATH, up.CREDENTIALS_PATH)


def setUpModule():
    """**The trap, closed at the module level.** Point the two identity files at paths that do not
    exist, for every test in this file. The per-call `account_reader` seam is the design; this is
    the guarantee — a future test that forgets to inject one reads a missing file rather than
    the owner's real credentials, which is the difference between a wrong assertion and a leak."""
    absent = os.path.join(tempfile.mkdtemp(prefix="usage-probe-no-home-"), "absent")
    up.CLAUDE_JSON_PATH = os.path.join(absent, ".claude.json")
    up.CREDENTIALS_PATH = os.path.join(absent, ".credentials.json")


def tearDownModule():
    up.CLAUDE_JSON_PATH, up.CREDENTIALS_PATH = _REAL_PATHS

# The §3.1 capture, verbatim, with the REAL separator bytes. Do not "tidy" the whitespace: the
# behaviour lines' two-space indent is what distinguishes them from a meter line.
SAMPLE = (
    "You are currently using your subscription to power your Claude Code usage\n"
    "\n"
    f"Current session: 5% used {MIDDOT} resets Aug 27, 3:49pm (America/Bogota)\n"
    f"Current week (all models): 74% used {MIDDOT} resets Aug 31, 4:59pm (America/Bogota)\n"
    f"Current week (Fable): 31% used {MIDDOT} resets Aug 31, 4:59pm (America/Bogota)\n"
    "\n"
    "What's contributing to your limits usage?\n"
    f"Approximate, based on local sessions on this machine {EMDASH} does not include other devices "
    "or claude.ai. Behaviors are independent characteristics, not a breakdown.\n"
    "\n"
    f"Last 24h {MIDDOT} 6694 requests {MIDDOT} 294 sessions\n"
    "  66% of your usage was at >150k context\n"
    "  49% of your usage was while 4+ sessions ran in parallel\n"
    "  32% of your usage came from subagent-heavy sessions\n"
    "  18% of your usage came from sessions active for 8+ hours\n"
    "  Top subagents: general-purpose 14%, Explore 3%\n"
    "  Top MCP servers: notion 1%\n"
    "\n"
    f"Last 7d {MIDDOT} 32088 requests {MIDDOT} 1918 sessions\n"
    "  76% of your usage was at >150k context\n"
    "  43% of your usage came from sessions active for 8+ hours\n"
    "  39% of your usage was while 4+ sessions ran in parallel\n"
    "  16% of your usage came from subagent-heavy sessions\n"
    "  Top skills: /assistant 2%\n"
    "  Top subagents: general-purpose 6%, workflow-subagent 2%, Explore 1%\n"
    "  Top MCP servers: notion 2%"
)

# A SECOND real capture, ~20 minutes later on claude-haiku-4-5. Different numbers, one behaviour
# line reordered in the 7d block, `5pm`-style minutes still present. It exists so the fingerprint
# claim ("numbers do not move it") is tested against a real second observation and not a mutation of
# the first.
SAMPLE_2 = (
    "You are currently using your subscription to power your Claude Code usage\n"
    "\n"
    f"Current session: 13% used {MIDDOT} resets Aug 27, 3:49pm (America/Bogota)\n"
    f"Current week (all models): 75% used {MIDDOT} resets Aug 31, 4:59pm (America/Bogota)\n"
    f"Current week (Fable): 31% used {MIDDOT} resets Aug 31, 4:59pm (America/Bogota)\n"
    "\n"
    "What's contributing to your limits usage?\n"
    f"Approximate, based on local sessions on this machine {EMDASH} does not include other devices "
    "or claude.ai. Behaviors are independent characteristics, not a breakdown.\n"
    "\n"
    f"Last 24h {MIDDOT} 5561 requests {MIDDOT} 292 sessions\n"
    "  69% of your usage was at >150k context\n"
    "  44% of your usage was while 4+ sessions ran in parallel\n"
    "  23% of your usage came from sessions active for 8+ hours\n"
    "  20% of your usage came from subagent-heavy sessions\n"
    "  Top subagents: general-purpose 6%, Explore 1%\n"
    "  Top MCP servers: notion 2%\n"
    "\n"
    f"Last 7d {MIDDOT} 31970 requests {MIDDOT} 1919 sessions\n"
    "  76% of your usage was at >150k context\n"
    "  39% of your usage came from sessions active for 8+ hours\n"
    "  38% of your usage was while 4+ sessions ran in parallel\n"
    "  17% of your usage came from subagent-heavy sessions\n"
    "  Top skills: /assistant 2%\n"
    "  Top subagents: general-purpose 6%, workflow-subagent 3%, Explore 1%\n"
    "  Top MCP servers: notion 2%"
)

NOW = datetime(2026, 8, 27, 14, 12, 0, tzinfo=timezone(timedelta(hours=-5)))


def envelope(result: str, **over) -> str:
    """A realistic `--output-format json` envelope. The zeros are MEASURED, not placeholders:
    `/usage` renders client-side, so a real probe comes back `num_turns: 0` with an all-zero usage
    block. That is the observer-effect number the row records."""
    body = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 3086,
            "duration_api_ms": 0, "num_turns": 0, "result": result, "total_cost_usd": 0,
            "session_id": "00000000-0000-0000-0000-00000000000a", "modelUsage": {},
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "service_tier": "standard"}}
    body.update(over)
    return json.dumps(body)


class FakeRunner:
    """A `subprocess.run`-compatible stand-in. Records every argv it is handed so the tests can
    assert the invocation shape (§2.1/§2.2) without a shell ever seeing it."""

    def __init__(self, stdout="", stderr="", returncode=0, raises=None, version="2.1.243"):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
        self.raises, self.version = raises, version
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if len(argv) >= 2 and argv[1] == "--version":
            return subprocess.CompletedProcess(argv, 0, f"{self.version} (Claude Code)\n", "")
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class ParseTheVerbatimSample(unittest.TestCase):

    def test_the_spec_sample_parses_ok(self):
        parsed = up.parse_report(SAMPLE, NOW)
        self.assertEqual(up.classify(parsed), (up.OK, []))
        self.assertEqual(sorted(parsed["meters"]), ["session", "week_all_models", "week_fable"])
        self.assertEqual(parsed["meters"]["week_all_models"]["pct"], 74)
        self.assertEqual(parsed["unmatched"], [])
        self.assertEqual(parsed["unrecognised_meters"], [])

    def test_reset_stamp_becomes_a_real_instant_and_keeps_the_verbatim_text(self):
        m = up.parse_report(SAMPLE, NOW)["meters"]["week_all_models"]
        self.assertEqual(m["resets_text"], "Aug 31, 4:59pm (America/Bogota)")
        self.assertEqual(m["resets_tz"], "America/Bogota")
        self.assertTrue(m["resets_at"].startswith("2026-08-31T16:59:00"))

    def test_minutes_omitted_on_the_hour(self):
        """Both `3:49pm` and `5pm` forms occur. The bare-hour one must not fall back to the parse
        failure that would strip the whole meter's reset."""
        iso, tz = up.parse_reset_stamp("Aug 31, 5pm (America/Bogota)", NOW)
        self.assertTrue(iso.startswith("2026-08-31T17:00:00"))
        self.assertEqual(tz, "America/Bogota")

    def test_the_stamp_is_read_on_the_callers_clock_not_the_hosts(self):
        """The wall-clock time is attached to `now_local`'s zone — the owner's clock — so the same
        report read under two owner offsets names two different instants, and the host's own zone
        never enters into it."""
        east = datetime(2026, 8, 27, 14, 0, tzinfo=timezone(timedelta(hours=9)))
        iso_east, _ = up.parse_reset_stamp("Aug 31, 5pm (Region/City)", east)
        iso_west, _ = up.parse_reset_stamp("Aug 31, 5pm (Region/City)", NOW)
        self.assertEqual(iso_east, "2026-08-31T17:00:00+09:00")
        self.assertEqual(iso_west, "2026-08-31T17:00:00-05:00")

    def test_year_is_inferred_toward_now_across_a_december_rollover(self):
        dec = datetime(2026, 12, 30, 12, 0, tzinfo=timezone(timedelta(hours=-6)))
        iso, _ = up.parse_reset_stamp("Jan 4, 5pm (America/Bogota)", dec)
        self.assertTrue(iso.startswith("2027-01-04"), iso)

    def test_blocks_carry_counts_behaviours_and_tops(self):
        blocks = up.parse_report(SAMPLE, NOW)["blocks"]
        self.assertEqual(blocks["last_24h"]["requests"], 6694)
        self.assertEqual(blocks["last_24h"]["sessions"], 294)
        self.assertEqual(blocks["last_7d"]["top"]["skills"], {"/assistant": 2})
        self.assertEqual(blocks["last_24h"]["top"]["subagents"],
                         {"general-purpose": 14, "Explore": 3})
        self.assertEqual(len(blocks["last_24h"]["behaviors"]), 4)


class ParsingRules(unittest.TestCase):
    """§3.2, one test per rule, each named for the observation that forces it."""

    def test_rule1_the_same_four_behaviours_in_two_orders_in_one_document(self):
        """The 24h block orders them >150k / 4+ / subagent / 8h and the 7d block orders them
        >150k / 8h / 4+ / subagent. A positional parser is already wrong on the live artifact."""
        blocks = up.parse_report(SAMPLE, NOW)["blocks"]
        self.assertEqual(set(blocks["last_24h"]["behaviors"]), set(blocks["last_7d"]["behaviors"]))
        self.assertNotEqual(list(blocks["last_24h"]["behaviors"]),
                            list(blocks["last_7d"]["behaviors"]))

    def test_rule2_every_attribution_line_is_optional(self):
        """`Top skills:` is in the 7d block and NOT in the 24h block, in every capture. A parser
        that requires it reports a parse failure on a perfectly healthy panel."""
        blocks = up.parse_report(SAMPLE, NOW)["blocks"]
        self.assertNotIn("skills", blocks["last_24h"]["top"])
        self.assertIn("skills", blocks["last_7d"]["top"])
        stripped = "\n".join(l for l in SAMPLE.splitlines() if "Top " not in l)
        self.assertEqual(up.classify(up.parse_report(stripped, NOW))[0], up.OK)

    def test_rule3_the_separator_is_filler(self):
        """A regex containing a literal middot breaks on the first release that switches to an en
        dash — and the break is invisible, because the numbers are still in the line."""
        for sep in ("–", "-", "|", "  "):
            with self.subTest(sep=sep):
                parsed = up.parse_report(SAMPLE.replace(MIDDOT, sep), NOW)
                self.assertEqual(up.classify(parsed)[0], up.OK)
                self.assertEqual(parsed["blocks"]["last_24h"]["requests"], 6694)

    def test_rule4_the_sidebar_label_maps_to_the_same_canonical_key(self):
        """The desktop sidebar panel says "5-hour limit"; the headless text says "Current
        session". Same meter, two names, and the LABEL may never become the key."""
        swapped = SAMPLE.replace("Current session", "5-hour limit")
        parsed = up.parse_report(swapped, NOW)
        self.assertEqual(up.classify(parsed)[0], up.OK)
        self.assertEqual(parsed["meters"]["session"]["pct"], 5)
        self.assertEqual(parsed["meters"]["session"]["label"], "5-hour limit")

    def test_rule4_an_unknown_meter_is_a_finding_not_a_discard(self):
        """If Anthropic adds a third pool, the row that first sees it must SAY so — otherwise the
        instrument under-reports forever and nothing anywhere notices."""
        extra = SAMPLE.replace(
            "What's contributing",
            f"Current fortnight (Opus): 12% used {MIDDOT} resets Sep 7, 5pm (America/Bogota)\n\n"
            "What's contributing")
        parsed = up.parse_report(extra, NOW)
        self.assertEqual(up.classify(parsed)[0], up.OK)  # the three required meters are still there
        self.assertEqual(len(parsed["unrecognised_meters"]), 1)
        self.assertEqual(parsed["unrecognised_meters"][0]["label"], "Current fortnight (Opus)")
        self.assertEqual(parsed["unrecognised_meters"][0]["pct"], 12)

    def test_rule4_a_new_per_model_weekly_pool_gets_its_own_key(self):
        extra = SAMPLE.replace(
            "What's contributing",
            f"Current week (Opus): 12% used {MIDDOT} resets Aug 31, 4:59pm (America/Bogota)\n\n"
            "What's contributing")
        parsed = up.parse_report(extra, NOW)
        self.assertEqual(parsed["meters"]["week_opus"]["pct"], 12)
        self.assertEqual(parsed["unrecognised_meters"], [])

    def test_rule4_a_duplicate_key_never_overwrites_the_first(self):
        dupe = SAMPLE.replace("Current week (Fable): 31%", "Current week (all models): 31%")
        parsed = up.parse_report(dupe, NOW)
        self.assertEqual(parsed["meters"]["week_all_models"]["pct"], 74)  # the first one wins
        self.assertEqual(len(parsed["unrecognised_meters"]), 1)
        self.assertIn("duplicate_key", parsed["unrecognised_meters"][0]["reason"])

    def test_rule5_the_behaviour_phrase_is_the_key_verbatim(self):
        """Not an enum. New behaviours will appear; a phrase-keyed dict absorbs them where an enum
        drops them without a sound. Nothing is stripped, not even the fixed prefix."""
        blocks = up.parse_report(SAMPLE, NOW)["blocks"]
        self.assertIn("of your usage was at >150k context", blocks["last_24h"]["behaviors"])
        novel = SAMPLE.replace("  66% of your usage was at >150k context",
                               "  66% of your usage was at >150k context\n"
                               "  7% of your usage was during a solar eclipse")
        parsed = up.parse_report(novel, NOW)
        self.assertEqual(parsed["blocks"]["last_24h"]["behaviors"]
                         ["of your usage was during a solar eclipse"], 7)
        self.assertEqual(up.classify(parsed)[0], up.OK)

    def test_an_unknown_time_span_block_is_absorbed_rather_than_dropped(self):
        extra = SAMPLE + f"\n\nLast 30d {MIDDOT} 99 requests {MIDDOT} 9 sessions\n  5% of your usage was odd"
        parsed = up.parse_report(extra, NOW)
        self.assertEqual(parsed["blocks"]["last_30d"]["requests"], 99)

    def test_an_unrecognised_line_lands_in_unmatched_and_never_guesses(self):
        parsed = up.parse_report(SAMPLE + "\n\nSomething entirely new appeared here", NOW)
        self.assertIn("Something entirely new appeared here", parsed["unmatched"])
        self.assertIn("unmatched", parsed["line_kinds"])


class Fingerprint(unittest.TestCase):
    """§3.4 — tolerance paired with a detector. BOTH halves have to hold or it detects nothing."""

    def test_two_real_captures_twenty_minutes_apart_share_a_fingerprint(self):
        a, b = up.parse_report(SAMPLE, NOW), up.parse_report(SAMPLE_2, NOW)
        self.assertNotEqual(a["meters"]["week_all_models"]["pct"],
                            b["meters"]["week_all_models"]["pct"])
        self.assertEqual(a["shape_fingerprint"], b["shape_fingerprint"])

    def test_a_separator_change_does_not_move_it(self):
        self.assertEqual(up.parse_report(SAMPLE, NOW)["shape_fingerprint"],
                         up.parse_report(SAMPLE.replace(MIDDOT, "–"), NOW)["shape_fingerprint"])

    def test_a_wording_change_DOES_move_it(self):
        """This is the early warning: the panel changed wording, the parse still worked THIS time,
        and someone should look before the next change breaks it."""
        for mutation in (SAMPLE.replace("Current session", "5-hour limit"),
                         SAMPLE.replace("was at >150k context", "was at over 150k context"),
                         SAMPLE.replace("Top MCP servers", "Top connectors")):
            with self.subTest(mutation=mutation[:60]):
                self.assertNotEqual(up.parse_report(SAMPLE, NOW)["shape_fingerprint"],
                                    up.parse_report(mutation, NOW)["shape_fingerprint"])

    def test_reordering_behaviour_lines_does_not_move_it(self):
        lines = SAMPLE.splitlines()
        i = lines.index("  66% of your usage was at >150k context")
        lines[i], lines[i + 1] = lines[i + 1], lines[i]
        self.assertEqual(up.parse_report(SAMPLE, NOW)["shape_fingerprint"],
                         up.parse_report("\n".join(lines), NOW)["shape_fingerprint"])


class FailureTaxonomy(unittest.TestCase):
    """§3.3 — one test per outcome, and invariant 2 asserted on every one of them."""

    def _row(self, **spawn):
        base = {"status": "completed", "stdout": "", "stderr": "", "returncode": 0,
                "elapsed_ms": 1200}
        base.update(spawn)
        return up.build_row(base, at=NOW.isoformat(), model="m", cli_version="2.1.243",
                            account=dict(FAKE_ACCOUNT), now_local=NOW)

    def test_ok(self):
        row = self._row(stdout=envelope(SAMPLE))
        self.assertEqual(row["outcome"], up.OK)
        self.assertEqual(row["week_window_id"], row["meters"]["week_all_models"]["resets_at"])
        self.assertEqual(row["last_7d"]["requests"], 32088)
        self.assertNotIn("raw", row)  # a clean row keeps no blob

    def test_partial_keeps_what_matched_names_what_did_not_and_retains_the_raw(self):
        text = "\n".join(l for l in SAMPLE.splitlines() if not l.startswith("Current week (Fable)"))
        row = self._row(stdout=envelope(text))
        self.assertEqual(row["outcome"], up.PARTIAL)
        self.assertEqual(row["missing_meters"], ["week_<model>"])
        self.assertEqual(row["meters"]["week_all_models"]["pct"], 74)
        self.assertIn("raw", row)

    def test_unparsed_carries_no_numeric_field_at_all(self):
        row = self._row(stdout=envelope("the usage panel has moved, please see the docs"))
        self.assertEqual(row["outcome"], up.UNPARSED)
        up.assert_no_meter_fields(row)
        for key in ("meters", "week_window_id", "last_24h", "last_7d"):
            self.assertNotIn(key, row)
        self.assertIn("raw", row)

    def test_a_non_json_envelope_is_unparsed_and_says_which(self):
        row = self._row(stdout="not json at all")
        self.assertEqual(row["outcome"], up.UNPARSED)
        self.assertEqual(row["detail"], "the envelope was not JSON")
        up.assert_no_meter_fields(row)

    def test_auth_failed_is_legible_as_an_outage(self):
        """An OAuth outage: no new claude session can authenticate at
        all. It must appear as rows saying so, never as an absence."""
        row = self._row(returncode=1, stderr="API Error: authentication_error - OAuth token expired")
        self.assertEqual(row["outcome"], up.AUTH_FAILED)
        self.assertEqual(row["auth_signature"], "authentication_error")
        up.assert_no_meter_fields(row)

    def test_a_healthy_report_is_never_relabelled_an_outage(self):
        """The auth signature list is broad ON PURPOSE, and is safe only because it is consulted
        exclusively on a path that has already failed. This is that guard."""
        row = self._row(stdout=envelope(SAMPLE + "\n  Top skills: /unauthorized 1%"))
        self.assertEqual(row["outcome"], up.OK)

    def test_spawn_failed_carries_the_exit_code_and_the_stderr_tail(self):
        row = self._row(returncode=127, stderr="claude: command not found")
        self.assertEqual(row["outcome"], up.SPAWN_FAILED)
        self.assertIn("127", row["detail"])
        up.assert_no_meter_fields(row)

    def test_a_child_that_never_started_is_spawn_failed(self):
        row = up.build_row(up.spawn_reading(runner=FakeRunner(raises=OSError("no such file")),
                                            model=None),
                           at=NOW.isoformat(), model=None, cli_version=None,
                           account=dict(FAKE_ACCOUNT), now_local=NOW)
        self.assertEqual(row["outcome"], up.SPAWN_FAILED)
        up.assert_no_meter_fields(row)

    def test_timeout_becomes_a_row_not_a_stuck_child(self):
        runner = FakeRunner(raises=subprocess.TimeoutExpired(cmd="claude", timeout=60))
        row = up.build_row(up.spawn_reading(runner=runner, model=None, timeout=60),
                           at=NOW.isoformat(), model=None, cli_version=None,
                           account=dict(FAKE_ACCOUNT), now_local=NOW)
        self.assertEqual(row["outcome"], up.TIMEOUT)
        up.assert_no_meter_fields(row)

    def test_skipped(self):
        row = up.build_skipped_row(at=NOW.isoformat(), gate="warm_busy", account=dict(FAKE_ACCOUNT))
        self.assertEqual(row["outcome"], up.SKIPPED)
        self.assertEqual(row["gate"], "warm_busy")
        up.assert_no_meter_fields(row)

    def test_invariant_2_is_enforced_and_not_merely_documented(self):
        """A zero here is a lie that survives arithmetic. The assertion is the executable form of
        the rule, and it is called on every write — this proves it actually bites."""
        with self.assertRaises(AssertionError):
            up.assert_no_meter_fields({"outcome": up.UNPARSED, "meters": {"session": {"pct": 0}}})
        with self.assertRaises(AssertionError):
            up.assert_no_meter_fields({"outcome": up.TIMEOUT, "last_24h": {"requests": 0}})

    def test_the_probe_block_is_kept_on_a_failed_row(self):
        """`probe` is the instrument's own measured cost, which is real even when the reading is
        not. It is deliberately outside invariant 2's scope."""
        row = self._row(returncode=1, stderr="boom")
        self.assertIn("elapsed_ms", row["probe"])

    def test_the_probe_block_records_the_observer_effect(self):
        row = self._row(stdout=envelope(SAMPLE))
        self.assertEqual(row["probe"]["cost_usd"], 0)
        self.assertEqual(row["probe"]["num_turns"], 0)
        self.assertIn("usage", row["probe"])


class Invocation(unittest.TestCase):
    """§2.1/§2.2 — the shape of the call, which is what a mangled one-liner got wrong."""

    def test_argv_is_a_list_and_carries_the_json_flag(self):
        argv = up.build_argv("claude", "claude-haiku-4-5-20251001")
        self.assertEqual(argv[:5], ["claude", "-p", "/usage", "--output-format", "json"])
        self.assertIn("--model", argv)

    def test_no_mcp_config_is_ever_threaded_in(self):
        """The reading needs no Notion and no Slack. Every MCP server threaded in would be
        tool-schema bytes on a request that does no tool work."""
        self.assertNotIn("--mcp-config", up.build_argv("claude", None))

    def test_stdin_is_closed_and_the_encoding_is_forced(self):
        """Measured: without `stdin=DEVNULL` the CLI waits 3 s for piped input and warns. And the
        report is full of middots the platform would otherwise decode as cp1252."""
        runner = FakeRunner(stdout=envelope(SAMPLE))
        up.spawn_reading(runner=runner, model=None)
        _, kw = runner.calls[-1]
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["encoding"], "utf-8")

    def test_the_api_key_is_scrubbed_so_the_reading_bills_what_it_measures(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-should-not-survive"
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        env = up.child_env()
        self.assertNotIn("ANTHROPIC_API_KEY", sorted(env.keys()))
        self.assertEqual(env["SENESCHAL_SESSION_SOURCE"], "usage")

    def test_the_cwd_is_a_scratch_dir_outside_every_repo(self):
        """§2.3 — spawned at a repo root a reading pays ~31 KB for a root CLAUDE.md it will never
        consult, and puts a bypassPermissions child inside a checkout for nothing."""
        runner = FakeRunner(stdout=envelope(SAMPLE))
        up.spawn_reading(runner=runner, model=None)
        cwd = runner.calls[-1][1]["cwd"]
        self.assertTrue(os.path.isdir(cwd))
        self.assertTrue(cwd.lower().startswith(tempfile.gettempdir().lower()))
        self.assertFalse(os.path.exists(os.path.join(cwd, "CLAUDE.md")))
        self.assertFalse(os.path.exists(os.path.join(cwd, ".git")))
        self.assertEqual(os.listdir(cwd), [])


class IntervalAndActivity(unittest.TestCase):
    """The new requirement: every row states the window its activity fields cover."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="usage-probe-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _take(self, *, stdout=None, collect=None, now=NOW):
        return up.take_reading(self.dir, runner=FakeRunner(stdout=stdout or envelope(SAMPLE)),
                               cli_version="2.1.243", collect=collect, now_local=now,
                               interval_min=60)

    def test_a_first_ever_reading_carries_no_activity_rather_than_an_assumed_window(self):
        """Invariant 3 at the other end: an assumed window is a fabricated measurement. There is
        exactly one such row in the file's life."""
        row = self._take(collect=lambda *a: {"jobs": {"running_count": 9}})
        self.assertTrue(row["interval"]["first_reading"])
        self.assertIsNone(row["interval"]["since"])
        self.assertNotIn("activity", row)

    def test_the_second_reading_carries_the_window_and_the_snapshot(self):
        self._take()
        seen = {}

        def collect(state_dir, since, until):
            seen["since"], seen["until"] = since, until
            return {"jobs": {"running_count": 3}, "warm": {"turns": 12}}

        later = NOW + timedelta(minutes=61)
        row = self._take(collect=collect, now=later)
        self.assertFalse(row["interval"]["first_reading"])
        self.assertEqual(row["interval"]["since"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(row["interval"]["until"], later.isoformat(timespec="seconds"))
        self.assertAlmostEqual(row["interval"]["seconds"], 3660, delta=1)
        self.assertEqual(row["activity"]["jobs"]["running_count"], 3)
        self.assertEqual(seen["since"], NOW)

    def test_a_long_window_is_flagged_as_a_gap_and_never_smoothed_over(self):
        """The daemon was down / the machine was asleep / auth was out. The row says the window was
        longer than intended; it does not guess why, and it does not interpolate."""
        self._take()
        row = self._take(collect=lambda *a: {}, now=NOW + timedelta(hours=5))
        self.assertTrue(row["interval"]["gap"])
        self.assertAlmostEqual(row["interval"]["seconds"], 18000, delta=1)

    def test_a_normal_window_is_not_a_gap(self):
        self._take()
        row = self._take(collect=lambda *a: {}, now=NOW + timedelta(minutes=62))
        self.assertFalse(row["interval"]["gap"])

    def test_the_window_opens_on_the_previous_row_whatever_its_outcome(self):
        """A failed reading still closes one window and opens the next — otherwise an outage would
        silently widen the NEXT row's interval without saying so."""
        up.append_row(self.dir, up.build_skipped_row(at=NOW.isoformat(), gate="warm_busy", account=dict(FAKE_ACCOUNT)))
        row = self._take(collect=lambda *a: {}, now=NOW + timedelta(minutes=61))
        self.assertEqual(row["interval"]["since"], NOW.isoformat())
        self.assertEqual(row["interval"]["since_outcome"], up.SKIPPED)

    def test_a_broken_collector_costs_the_snapshot_and_never_the_reading(self):
        self._take()

        def boom(*_a):
            raise RuntimeError("the state dir vanished")

        row = self._take(collect=boom, now=NOW + timedelta(minutes=61))
        self.assertEqual(row["outcome"], up.OK)
        self.assertIn("the state dir vanished", row["activity"]["error"])

    def test_a_failed_reading_still_carries_its_interval_and_activity(self):
        """WHEN a reading failed and WHAT was running while it failed is the whole reason the
        outage has to be legible. Only the METER numbers are withheld."""
        self._take()
        row = up.take_reading(self.dir, runner=FakeRunner(returncode=1, stderr="oauth token expired"),
                              cli_version=None, collect=lambda *a: {"jobs": {"running_count": 9}},
                              now_local=NOW + timedelta(minutes=61), interval_min=60)
        self.assertEqual(row["outcome"], up.AUTH_FAILED)
        self.assertEqual(row["activity"]["jobs"]["running_count"], 9)
        self.assertIn("since", row["interval"])
        up.assert_no_meter_fields(row)


class TheLog(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="usage-probe-log-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_every_attempt_writes_exactly_one_line(self):
        """Invariant 1. There is no code path that tries a reading and records nothing."""
        for runner in (FakeRunner(stdout=envelope(SAMPLE)),
                       FakeRunner(returncode=1, stderr="boom"),
                       FakeRunner(stdout="not json"),
                       FakeRunner(raises=subprocess.TimeoutExpired("claude", 60))):
            up.take_reading(self.dir, runner=runner, cli_version=None, now_local=NOW)
        with open(up.readings_path(self.dir), encoding="utf-8") as fh:
            lines = [l for l in fh if l.strip()]
        self.assertEqual(len(lines), 4)
        for line in lines:
            json.loads(line)  # every line is valid JSON on its own

    def test_last_row_survives_a_torn_final_line(self):
        """A crash mid-append leaves a half-line. It is skipped, never repaired and never guessed."""
        up.take_reading(self.dir, runner=FakeRunner(stdout=envelope(SAMPLE)), cli_version=None,
                        now_local=NOW)
        with open(up.readings_path(self.dir), "a", encoding="utf-8") as fh:
            fh.write('{"schema": "seneschal.plan-usage/1", "at": "2026-08')
        self.assertEqual(up.last_row(self.dir)["outcome"], up.OK)

    def test_last_row_on_a_missing_file_is_none_not_an_error(self):
        self.assertIsNone(up.last_row(os.path.join(self.dir, "nope")))

    def test_write_can_be_suppressed_for_a_dry_run(self):
        up.take_reading(self.dir, runner=FakeRunner(stdout=envelope(SAMPLE)), cli_version=None,
                        now_local=NOW, write=False)
        self.assertFalse(os.path.exists(up.readings_path(self.dir)))


class WhoseMeterIsIt(unittest.TestCase):
    """§4.2.1 — the account block. An owner who hits the weekly ceiling on one subscription and
    switches to a second leaves `plan-usage.jsonl` holding two meters with two weekly resets, and
    without this no row says which is which. These tests are what makes a row say so."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="usage-probe-account-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _paths(self, *, oauth=..., credentials=..., accounts=...):
        """Write whichever of the three files a test wants and return the kwargs for `read_account`.
        A file left as `...` is simply not created, which is the absent case."""
        out = {}
        for name, payload, key in (("claude.json", oauth, "claude_json"),
                                   ("credentials.json", credentials, "credentials"),
                                   ("accounts.json", accounts, "accounts_map")):
            path = os.path.join(self.dir, name)
            out[key] = path
            if payload is ...:
                continue
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(payload if isinstance(payload, str)
                         else json.dumps(payload, ensure_ascii=False))
        return out

    @staticmethod
    def _oauth(uuid=FAKE_UUID, **over):
        blob = {"accountUuid": uuid, "emailAddress": "someone@example.com",
                "organizationUuid": "00000000-0000-0000-0000-0000000000ff",
                "billingType": "claude_max", "subscriptionCreatedAt": "2026-08-29T06:18:59Z",
                "accountCreatedAt": "2026-03-29T00:00:00Z", "profileFetchedAt": 1787984670158,
                "displayName": "someone", "fullName": "Some One"}
        blob.update(over)
        return {"oauthAccount": blob}

    @staticmethod
    def _creds(token="fake-token-material"):
        return {"claudeAiOauth": {"accessToken": token},
                "mcpOAuth": {"notion|deadbeef": {"accessToken": "a-different-service's-secret"}}}

    # ---------------------------------------------------------------- reading the identity

    def test_a_clean_read_carries_the_identity_and_not_the_person(self):
        got = up.read_account(**self._paths(oauth=self._oauth(), credentials=self._creds()))
        self.assertTrue(got["known"])
        self.assertEqual(got["account_uuid"], FAKE_UUID)
        self.assertEqual(got["email"], "someone@example.com")
        self.assertEqual(got["billing_type"], "claude_max")
        self.assertEqual(got["subscription_created_at"], "2026-08-29T06:18:59Z")
        self.assertIn("profile_fetched_at", got)  # how stale the cached blob is
        # The blob also holds a display name and a full name. A machine series does not need a
        # person's name to say which meter a row belongs to, so they are not copied.
        self.assertNotIn("displayName", got)
        self.assertFalse(any("Some One" == v for v in got.values()))

    def test_a_missing_file_is_known_false_with_a_reason_never_an_error(self):
        got = up.read_account(**self._paths())
        self.assertFalse(got["known"])
        self.assertIn("claude.json", got["detail"])

    def test_a_corrupt_file_is_known_false_and_says_so(self):
        got = up.read_account(**self._paths(oauth="{not json at all"))
        self.assertFalse(got["known"])
        self.assertIn("not JSON", got["detail"])

    def test_a_file_with_no_oauth_account_is_known_false(self):
        got = up.read_account(**self._paths(oauth={"someOtherKey": 1}))
        self.assertFalse(got["known"])
        self.assertIn("accountUuid", got["detail"])

    # ---------------------------------------------------------------- the second signal

    def test_the_fingerprint_is_stable_across_two_reads_and_is_not_the_token(self):
        paths = self._paths(oauth=self._oauth(), credentials=self._creds())
        first = up.read_account(**paths)
        second = up.read_account(**paths)
        self.assertEqual(first["credential_fingerprint"], second["credential_fingerprint"])
        self.assertEqual(len(first["credential_fingerprint"]), up.FINGERPRINT_CHARS)
        # Never the token, and never a PREFIX of the token: a truncated secret is still a secret.
        blob = json.dumps(first)
        self.assertNotIn("fake-token-material", blob)
        for n in range(1, len("fake-token-material") + 1):
            self.assertNotIn(f'"{"fake-token-material"[:n]}', blob)

    def test_different_token_material_gives_a_different_fingerprint(self):
        one = up.read_account(**self._paths(oauth=self._oauth(), credentials=self._creds("aaa")))
        two = up.read_account(**self._paths(oauth=self._oauth(), credentials=self._creds("bbb")))
        self.assertNotEqual(one["credential_fingerprint"], two["credential_fingerprint"])

    def test_only_the_claude_token_is_hashed_never_an_mcp_server_s(self):
        """The same file holds a token per connected MCP server. Those are other services' secrets
        and say nothing about which subscription is billed, so changing one may not move this."""
        base = self._creds("stable")
        moved = self._creds("stable")
        moved["mcpOAuth"]["notion|deadbeef"]["accessToken"] = "rotated"
        one = up.read_account(**self._paths(oauth=self._oauth(), credentials=base))
        two = up.read_account(**self._paths(oauth=self._oauth(), credentials=moved))
        self.assertEqual(one["credential_fingerprint"], two["credential_fingerprint"])

    def test_an_unreadable_credential_costs_the_fingerprint_and_not_the_identity(self):
        got = up.read_account(**self._paths(oauth=self._oauth()))
        self.assertTrue(got["known"])
        self.assertNotIn("credential_fingerprint", got)
        self.assertIn("credential_detail", got)

    # ---------------------------------------------------------------- the optional label

    def test_a_mapped_uuid_gets_its_label(self):
        got = up.read_account(**self._paths(
            oauth=self._oauth(), credentials=self._creds(),
            accounts={"schema": "seneschal.accounts/1",
                      "accounts": {FAKE_UUID: {"label": "main", "email": "someone@example.com"}}}))
        self.assertEqual(got["label"], "main")

    def test_an_unmapped_uuid_absent_map_and_broken_map_all_mean_no_label(self):
        """A convenience may not be able to fail a reading. All three degrade the same way, and
        none of them raises."""
        cases = {
            "unmapped": {"schema": "seneschal.accounts/1", "accounts": {OTHER_UUID: {"label": "x"}}},
            "wrong schema": {"schema": "something.else/9",
                             "accounts": {FAKE_UUID: {"label": "x"}}},
            "corrupt": "{{{",
            "absent": ...,
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                got = up.read_account(**self._paths(oauth=self._oauth(), credentials=self._creds(),
                                                    accounts=payload))
                self.assertTrue(got["known"])
                self.assertNotIn("label", got)

    def test_the_map_is_looked_up_in_the_state_dir_by_default(self):
        with open(os.path.join(self.dir, up.ACCOUNTS_MAP_FILENAME), "w", encoding="utf-8") as fh:
            json.dump({"schema": "seneschal.accounts/1",
                       "accounts": {FAKE_UUID: {"label": "weekend"}}}, fh)
        paths = self._paths(oauth=self._oauth(), credentials=self._creds())
        paths.pop("accounts_map")
        self.assertEqual(up.read_account(state_dir=self.dir, **paths)["label"], "weekend")

    # ---------------------------------------------------------------- the boundary

    def test_the_first_row_is_first_reading_and_never_a_change(self):
        block = up.account_for_row(self.dir, None, fake_reader())
        self.assertTrue(block["first_reading"])
        self.assertNotIn("changed", block)

    def test_a_real_switch_is_marked_and_names_the_account_it_left(self):
        previous = {"account": {"account_uuid": OTHER_UUID}}
        block = up.account_for_row(self.dir, previous, fake_reader())
        self.assertTrue(block["changed"])
        self.assertEqual(block["previous_account_uuid"], OTHER_UUID)

    def test_the_same_account_twice_is_not_a_change(self):
        previous = {"account": {"account_uuid": FAKE_UUID}}
        block = up.account_for_row(self.dir, previous, fake_reader())
        self.assertNotIn("changed", block)
        self.assertNotIn("first_reading", block)

    def test_a_row_from_before_this_field_reads_as_unknown_not_as_the_main_account(self):
        """Invariant 7: absent is UNKNOWN. A previous row with no account block must not silently
        read as continuity — that is the assumption the whole field exists to remove."""
        block = up.account_for_row(self.dir, {"outcome": "ok"}, fake_reader())
        self.assertFalse(block.get("previous_account_known"))
        self.assertNotIn("changed", block)

    def test_account_changed_is_a_pure_helper_a_consumer_can_call(self):
        self.assertTrue(up.account_changed({"account_uuid": "a"}, {"account_uuid": "b"}))
        self.assertFalse(up.account_changed({"account_uuid": "a"}, {"account_uuid": "a"}))
        for unknown in ({}, {"known": False}, None, "nonsense"):
            with self.subTest(unknown=unknown):
                self.assertFalse(up.account_changed(unknown, {"account_uuid": "b"}))
                self.assertFalse(up.account_changed({"account_uuid": "a"}, unknown))

    # ---------------------------------------------------------------- on the row itself

    def _outcomes(self):
        rows = {}
        for name, runner in (("ok", FakeRunner(stdout=envelope(SAMPLE))),
                             ("unparsed", FakeRunner(stdout=envelope("nothing recognisable"))),
                             ("auth_failed", FakeRunner(returncode=1, stderr="oauth token expired")),
                             ("spawn_failed", FakeRunner(raises=OSError("no such file"))),
                             ("timeout",
                              FakeRunner(raises=subprocess.TimeoutExpired("claude", 60)))):
            rows[name] = up.take_reading(self.dir, runner=runner, cli_version=None,
                                         account_reader=fake_reader(), now_local=NOW, write=False)
        rows["skipped"] = up.build_skipped_row(at=NOW.isoformat(), gate="warm_busy",
                                               account=up.account_for_row(self.dir, None,
                                                                          fake_reader()))
        return rows

    def test_every_outcome_carries_an_account_block(self):
        for name, row in self._outcomes().items():
            with self.subTest(outcome=name):
                self.assertEqual(row["outcome"], name if name != "unparsed" else up.UNPARSED)
                self.assertEqual(row["account"]["account_uuid"], FAKE_UUID)

    def test_invariant_2_still_passes_on_a_non_ok_row_that_carries_an_account(self):
        """Account identity is NOT a meter field. `assert_no_meter_fields` must keep passing — and
        it must keep FAILING for an actual meter, or this test proves nothing."""
        for name, row in self._outcomes().items():
            with self.subTest(outcome=name):
                up.assert_no_meter_fields(row)
        broken = self._outcomes()["timeout"]
        broken["meters"] = {"session": {"pct": 0}}
        with self.assertRaises(AssertionError):
            up.assert_no_meter_fields(broken)

    def test_an_unreadable_identity_still_writes_a_row_and_never_carries_forward(self):
        """Invariant 5, and it is invariant 3 wearing a different hat: the row that CANNOT say whose
        meter it is must say that, rather than inheriting the last row that could."""
        up.take_reading(self.dir, runner=FakeRunner(stdout=envelope(SAMPLE)), cli_version=None,
                        account_reader=fake_reader(), now_local=NOW)
        row = up.take_reading(self.dir, runner=FakeRunner(stdout=envelope(SAMPLE)),
                              cli_version=None, now_local=NOW + timedelta(minutes=61))
        self.assertFalse(row["account"]["known"])
        self.assertNotIn("account_uuid", row["account"])
        self.assertNotEqual(row["account"].get("label"), "main")

    def test_a_reader_that_explodes_costs_the_identity_and_never_the_row(self):
        def boom(**kwargs):
            raise RuntimeError("the profile blob was on fire")
        row = up.take_reading(self.dir, runner=FakeRunner(stdout=envelope(SAMPLE)),
                              cli_version=None, account_reader=boom, now_local=NOW)
        self.assertEqual(row["outcome"], up.OK)
        self.assertFalse(row["account"]["known"])
        self.assertIn("RuntimeError", row["account"]["detail"])

    def test_the_switch_is_visible_on_the_row_a_consumer_would_difference_across(self):
        """The whole point, end to end: two readings under two accounts, and the second row says in
        one field that a delta spanning it would be arithmetic on unrelated quantities."""
        up.take_reading(self.dir, runner=FakeRunner(stdout=envelope(SAMPLE)), cli_version=None,
                        account_reader=fake_reader(), now_local=NOW)
        second = up.take_reading(
            self.dir, runner=FakeRunner(stdout=envelope(SAMPLE)), cli_version=None,
            account_reader=fake_reader(dict(FAKE_ACCOUNT, account_uuid=OTHER_UUID)),
            now_local=NOW + timedelta(minutes=61))
        self.assertTrue(second["account"]["changed"])
        self.assertEqual(second["account"]["previous_account_uuid"], FAKE_UUID)

    def test_the_schema_does_not_move_and_old_rows_are_not_rewritten(self):
        """Rule 7: the field is additive and optional. An old row keeps its bytes exactly."""
        legacy = ('{"schema": "seneschal.plan-usage/1", "at": "2026-08-28T12:00:00-05:00", '
                  '"outcome": "unparsed", "parser_version": 1}')
        with open(up.readings_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write(legacy + "\n")
        row = up.take_reading(self.dir, runner=FakeRunner(stdout=envelope(SAMPLE)),
                              cli_version=None, account_reader=fake_reader(),
                              now_local=NOW + timedelta(minutes=61))
        self.assertEqual(row["schema"], "seneschal.plan-usage/1")
        self.assertEqual(row["parser_version"], 1)
        with open(up.readings_path(self.dir), encoding="utf-8") as fh:
            lines = [l.rstrip("\n") for l in fh if l.strip()]
        self.assertEqual(lines[0], legacy)  # byte-identical, no backfill
        self.assertNotIn("account", json.loads(lines[0]))


class NoSpawnNoSpend(unittest.TestCase):
    """The test trap, closed explicitly: nothing in this suite may spawn `claude`, reach the
    network, or write anywhere near the live state directory."""

    def test_state_dir_has_no_default_anywhere(self):
        with open(os.devnull, "w", encoding="utf-8") as quiet:
            stderr, sys.stderr = sys.stderr, quiet
            try:
                with self.assertRaises(SystemExit):
                    up.main(["read"])  # must refuse: there is no default state dir to fall into
            finally:
                sys.stderr = stderr

    @staticmethod
    def _source(name):
        with open(os.path.join(SCRIPT_DIR, name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_module_holds_no_path_into_the_live_state_dir(self):
        source = self._source("usage_probe.py")
        self.assertNotIn('"..", "state"', source)
        self.assertNotIn("DEFAULT_STATE_DIR", source)

    def test_the_identity_paths_are_module_constants_and_are_redirected_under_test(self):
        """Two halves. In PRODUCTION the defaults must resolve under the real home, or the probe
        reads nobody's credentials and every row is `known: false`. In the SUITE they must not —
        `setUpModule` repoints them, and this is the assertion that the redirect actually took."""
        source = self._source("usage_probe.py")
        self.assertIn('CLAUDE_JSON_PATH = os.path.join(os.path.expanduser("~"), ".claude.json")',
                      source)
        # Not "outside $HOME" — TEMP lives under the home on Windows. The property that matters is
        # that neither constant is the real file and neither resolves to anything at all.
        for path, real in ((up.CLAUDE_JSON_PATH, _REAL_PATHS[0]),
                           (up.CREDENTIALS_PATH, _REAL_PATHS[1])):
            self.assertNotEqual(path, real)
            self.assertFalse(os.path.exists(path), f"the suite can reach a real {path}")

    def test_the_credential_reader_hashes_and_never_returns_token_material(self):
        """Read against the SOURCE, because the rule is about what the function is allowed to
        return: a hash, truncated. A future edit adding a `token_prefix` field for debugging is
        exactly what this is here to stop."""
        source = self._source("usage_probe.py")
        body = source[source.index("def credential_fingerprint"):source.index("def account_label")]
        self.assertIn("hashlib.sha256", body)
        for leak in ("token[:", "token[0:", "return token", "accessToken\"]:"):
            self.assertNotIn(leak, body)

    def test_it_never_imports_a_send_path(self):
        """Spec §5: the instrument is write-only and says nothing, on every path, forever. A future
        edit that reaches for telegram_send / mouth / the outbox fails on this test first.

        Checked against the parsed IMPORT GRAPH, not against the text — the docstrings name these
        modules on purpose, to say why they are absent."""
        import ast
        forbidden = {"telegram_send", "mouth", "outbox", "outbox_common", "sentinel",
                     "telegram_ask", "call_out"}
        for module in ("usage_probe.py", "usage_activity.py"):
            with self.subTest(module=module):
                imported = set()
                for node in ast.walk(ast.parse(self._source(module))):
                    if isinstance(node, ast.Import):
                        imported.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.add(node.module.split(".")[0])
                self.assertEqual(imported & forbidden, set(),
                                 f"{module} reached for a send path: {imported & forbidden}")


if __name__ == "__main__":
    unittest.main()
