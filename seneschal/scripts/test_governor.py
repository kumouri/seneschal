#!/usr/bin/env python3
"""Tests for governor.py — Oikonomos, the budget-governor advisor (cockpit-spec.md v3.5).

Stdlib unittest only, no network, no real `claude`/Ollama calls. Covers: SCHEMA validation, tolerant
load / strict save (incl. forward-compat unknown-key preservation), the owner-local day/week rollups
(incl. the after-midnight-is-still-yesterday boundary, exercised through tz_common with a mocked
fixed-offset owner zone), the fable_oneshot rail gate, the concurrency counter, alert dedupe, and the
billable token basis (weight table, basis-aware rollups/alerts/gate, the turn_id join key).

Run:  python -m unittest seneschal.scripts.test_governor   (or)   python test_governor.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import governor as gv  # noqa: E402
import tz_common as tzc  # noqa: E402


class SchemaValidate(unittest.TestCase):
    def test_int_in_range_ok(self):
        ok, err = gv._validate("fable_oneshots_per_day", gv.SCHEMA["fable_oneshots_per_day"], 3)
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_int_below_min_rejected(self):
        ok, err = gv._validate("fable_concurrency_max", gv.SCHEMA["fable_concurrency_max"], 0)
        self.assertFalse(ok)
        self.assertIn("below the minimum", err)

    def test_int_above_max_rejected(self):
        ok, err = gv._validate("turn_checkpoint_n", gv.SCHEMA["turn_checkpoint_n"], 999)
        self.assertFalse(ok)
        self.assertIn("above the maximum", err)

    def test_bool_is_not_an_int(self):
        ok, _ = gv._validate("fable_oneshots_per_day", gv.SCHEMA["fable_oneshots_per_day"], True)
        self.assertFalse(ok)

    def test_dict_int_ok(self):
        ok, err = gv._validate("daily_token_budget_by_model",
                               gv.SCHEMA["daily_token_budget_by_model"],
                               {"claude-opus-4-8": 1000})
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_dict_int_rejects_non_dict(self):
        ok, err = gv._validate("daily_token_budget_by_model",
                               gv.SCHEMA["daily_token_budget_by_model"], "nope")
        self.assertFalse(ok)

    def test_dict_int_rejects_bad_value(self):
        ok, err = gv._validate("daily_token_budget_by_model",
                               gv.SCHEMA["daily_token_budget_by_model"],
                               {"claude-opus-4-8": "a lot"})
        self.assertFalse(ok)
        self.assertIn("claude-opus-4-8", err)

    def test_dict_enum_ok(self):
        ok, err = gv._validate("reasoning_effort_by_mode", gv.SCHEMA["reasoning_effort_by_mode"],
                               {"Chat": "high"})
        self.assertTrue(ok)

    def test_dict_enum_rejects_unknown_option(self):
        ok, err = gv._validate("reasoning_effort_by_mode", gv.SCHEMA["reasoning_effort_by_mode"],
                               {"Chat": "extreme"})
        self.assertFalse(ok)
        self.assertIn("extreme", err)


class LoadSave(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_missing_file_loads_all_defaults(self):
        cfg = gv.load(self.dir)
        self.assertEqual(cfg["fable_oneshots_per_day"], 5)
        self.assertEqual(cfg["fable_oneshots_per_conversation"], 2)
        self.assertEqual(cfg["reasoning_effort_by_mode"]["Dream"], "high")

    def test_corrupt_file_loads_all_defaults(self):
        with open(gv.config_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("not json")
        cfg = gv.load(self.dir)
        self.assertEqual(cfg["fable_concurrency_max"], 1)

    def test_save_then_load_round_trip(self):
        written = gv.save(self.dir, {"fable_oneshots_per_day": 10})
        self.assertEqual(written["fable_oneshots_per_day"], 10)
        loaded = gv.load(self.dir)
        self.assertEqual(loaded["fable_oneshots_per_day"], 10)
        # untouched knobs keep their defaults
        self.assertEqual(loaded["fable_concurrency_max"], 1)

    def test_save_rejects_unknown_knob(self):
        with self.assertRaises(ValueError) as ctx:
            gv.save(self.dir, {"not_a_real_knob": 1})
        self.assertIn("unknown knob", str(ctx.exception))
        self.assertFalse(os.path.exists(gv.config_path(self.dir)))

    def test_save_rejects_out_of_range_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            gv.save(self.dir, {"fable_concurrency_max": 0})
        self.assertFalse(os.path.exists(gv.config_path(self.dir)))

    def test_save_atomic_no_leftover_tmp(self):
        gv.save(self.dir, {"fable_oneshots_per_day": 4})
        self.assertFalse(os.path.exists(gv.config_path(self.dir) + ".tmp"))

    def test_save_preserves_unknown_keys_forward_compat(self):
        # simulate a NEWER version's knob already on disk that this SCHEMA doesn't recognize
        with open(gv.config_path(self.dir), "w", encoding="utf-8") as fh:
            json.dump({"some_future_knob": 42}, fh)
        gv.save(self.dir, {"fable_oneshots_per_day": 7})
        with open(gv.config_path(self.dir), encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["some_future_knob"], 42)
        self.assertEqual(raw["fable_oneshots_per_day"], 7)

    def test_load_falls_back_per_knob_on_invalid_stored_value(self):
        with open(gv.config_path(self.dir), "w", encoding="utf-8") as fh:
            json.dump({"fable_oneshots_per_day": "not an int", "fable_concurrency_max": 3}, fh)
        cfg = gv.load(self.dir)
        self.assertEqual(cfg["fable_oneshots_per_day"], 5)   # bad value -> default
        self.assertEqual(cfg["fable_concurrency_max"], 3)    # valid value -> preserved

    def test_dict_default_is_deep_copied_not_shared(self):
        cfg1 = gv.load(self.dir)
        cfg1["daily_token_budget_by_model"]["claude-opus-4-8"] = 999
        cfg2 = gv.load(self.dir)
        self.assertNotEqual(cfg2["daily_token_budget_by_model"]["claude-opus-4-8"], 999)


class OwnerLocalDateBoundary(unittest.TestCase):
    """Rule 5: after-midnight activity counts as the PRIOR local day. governor delegates the
    day-boundary math to tz_common (the owner's configured timezone → machine-local fallback);
    these pin a fixed-offset owner zone through the same ``zoneinfo.ZoneInfo`` seam
    test_tz_common.LadderStep2Resolvable mocks, so no tzdata install is needed."""

    def setUp(self):
        tzc._reset()
        self.addCleanup(tzc._reset)

    @contextmanager
    def _owner_zone(self, offset: timedelta):
        fixed = timezone(offset)
        with unittest.mock.patch.object(
                tzc, "load_identity",
                return_value={"owner": {"timezone": "Test/Zone"}}), \
                unittest.mock.patch("zoneinfo.ZoneInfo", lambda key: fixed):
            yield

    def test_utc_instant_just_after_midnight_is_still_prior_local_day(self):
        # 04:30Z in a UTC-6 owner zone is 22:30 the previous local evening — still the 14th locally.
        with self._owner_zone(timedelta(hours=-6)):
            ts = datetime(2026, 1, 15, 4, 30, tzinfo=timezone.utc)
            self.assertEqual(gv._to_local_date(ts).isoformat(), "2026-01-14")

    def test_utc_instant_well_into_the_local_morning_is_the_same_day(self):
        # 15:00Z in a UTC-6 owner zone is 09:00 local — the 15th locally.
        with self._owner_zone(timedelta(hours=-6)):
            ts = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
            self.assertEqual(gv._to_local_date(ts).isoformat(), "2026-01-15")

    def test_positive_offset_owner_rolls_forward(self):
        # 20:00Z in a UTC+9:30 owner zone is 05:30 the NEXT local morning (half-hour zones intact).
        with self._owner_zone(timedelta(hours=9, minutes=30)):
            ts = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)
            self.assertEqual(gv._to_local_date(ts).isoformat(), "2026-07-15")

    def test_naive_datetime_assumed_utc(self):
        with self._owner_zone(timedelta(hours=-6)):
            ts = datetime(2026, 1, 15, 4, 30)  # naive
            self.assertEqual(gv._to_local_date(ts).isoformat(), "2026-01-14")

    def test_machine_local_fallback_without_tz_common(self):
        # With tz_common absent (a bare interpreter), the helper degrades to the machine clock.
        ts = datetime(2026, 1, 15, 4, 30, tzinfo=timezone.utc)
        with unittest.mock.patch.object(gv, "_tz_common", None):
            self.assertEqual(gv._to_local_date(ts), ts.astimezone().date())

    def test_rollups_use_the_owner_zone_for_day_keys(self):
        # A record at 04:30Z lands on the PRIOR owner-local day in a UTC-6 zone — end to end
        # through rollups(), the path the fable_oneshot rail actually consults.
        with self._owner_zone(timedelta(hours=-6)), tempfile.TemporaryDirectory() as d:
            with open(gv.ledger_path(d), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": "2026-07-18T04:30:00Z", "kind": "fable_oneshot"}) + "\n")
            roll = gv.rollups(d, now=datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc))
            self.assertEqual(roll["day"], "2026-07-17")
            self.assertEqual(roll["fable_oneshots"]["day"], 1)

    def test_week_key_stable_within_a_week(self):
        from datetime import date
        mon = date(2026, 7, 13)   # a Monday
        sun = date(2026, 7, 19)   # its Sunday
        self.assertEqual(gv._week_key(mon), gv._week_key(sun))


class Ledger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_append_and_read_round_trip(self):
        gv.append_spend(self.dir, "tokens", model="claude-opus-4-8", tokens=100)
        recs = gv._read_ledger(self.dir)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "tokens")
        self.assertEqual(recs[0]["tokens"], 100)

    def test_missing_ledger_reads_empty(self):
        self.assertEqual(gv._read_ledger(self.dir), [])

    def test_turn_id_is_recorded_when_given(self):
        """The join key. Without it the ledger row and the metrics row for the same turn — written
        moments apart by the same function — can only be paired by timestamp proximity, which is a
        heuristic, not a key."""
        gv.append_spend(self.dir, "tokens", model="claude-opus-5", tokens=100,
                        turn_id="e1c31303253d")
        self.assertEqual(gv._read_ledger(self.dir)[0]["turn_id"], "e1c31303253d")

    def test_turn_id_is_OMITTED_not_zeroed_when_absent(self):
        """A delegation or a job child is not a warm turn and has no turn to name. The
        never-write-a-0 reasoning applies to a fabricated key just as much as to a fabricated count:
        an empty-string or null `turn_id` would look like a turn that could be looked up."""
        gv.append_spend(self.dir, "fable_oneshot", model="claude-fable-5", tokens=50)
        row = gv._read_ledger(self.dir)[0]
        self.assertNotIn("turn_id", row)

    def test_an_empty_turn_id_is_treated_as_absent(self):
        for empty in ("", None):
            gv.append_spend(self.dir, "tokens", model="m", tokens=1, turn_id=empty)
        for row in gv._read_ledger(self.dir):
            self.assertNotIn("turn_id", row)

    def test_adding_the_key_changes_nothing_else_about_the_row(self):
        """`tokens` and `components` keep their meanings forever. Levers are purely additive."""
        gv.append_spend(self.dir, "tokens", model="m", tokens=100)
        gv.append_spend(self.dir, "tokens", model="m", tokens=100, turn_id="abc")
        without, with_key = gv._read_ledger(self.dir)
        self.assertEqual({k: v for k, v in with_key.items() if k not in ("ts", "turn_id")},
                         {k: v for k, v in without.items() if k != "ts"})

    def test_corrupt_line_is_skipped_not_fatal(self):
        path = gv.ledger_path(self.dir)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"ts":"2026-07-18T00:00:00Z","kind":"tokens","tokens":5}\n')
            fh.write("garbage not json\n")
        recs = gv._read_ledger(self.dir)
        self.assertEqual(len(recs), 1)

    def test_rollups_bucket_tokens_by_model_and_day(self):
        # Records stamped moments ago are always "today" for a real-now rollup — no pinned clock, so
        # this stays green on any date and in any machine timezone.
        gv.append_spend(self.dir, "tokens", model="claude-opus-4-8", tokens=100)
        gv.append_spend(self.dir, "tokens", model="claude-opus-4-8", tokens=50)
        gv.append_spend(self.dir, "tokens", model="claude-sonnet-5", tokens=10)
        roll = gv.rollups(self.dir)
        self.assertEqual(roll["tokens_by_model"]["day"]["claude-opus-4-8"], 150)
        self.assertEqual(roll["tokens_by_model"]["day"]["claude-sonnet-5"], 10)

    def test_rollups_excludes_prior_day_from_day_bucket_but_counts_week(self):
        # write a record from "yesterday" (local) directly, bypassing append_spend's own timestamp
        yesterday_utc = datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        with open(gv.ledger_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": yesterday_utc, "kind": "fable_oneshot", "model": "claude-fable-5"}) + "\n")
        now = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
        roll = gv.rollups(self.dir, now=now)
        self.assertEqual(roll["fable_oneshots"]["day"], 0)   # not today
        self.assertEqual(roll["fable_oneshots"]["week"], 1)  # same ISO week

    def test_conversation_fable_count_is_all_time_not_day_bound(self):
        old_utc = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        with open(gv.ledger_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": old_utc, "kind": "fable_oneshot", "conversation_id": "c1"}) + "\n")
        self.assertEqual(gv._conversation_fable_count(self.dir, "c1"), 1)
        self.assertEqual(gv._conversation_fable_count(self.dir, "c2"), 0)
        self.assertEqual(gv._conversation_fable_count(self.dir, None), 0)


class Concurrency(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_starts_at_zero(self):
        self.assertEqual(gv._read_inflight(self.dir), 0)

    def test_begin_increments_end_decrements(self):
        gv.begin_fable_call(self.dir)
        self.assertEqual(gv._read_inflight(self.dir), 1)
        gv.begin_fable_call(self.dir)
        self.assertEqual(gv._read_inflight(self.dir), 2)
        gv.end_fable_call(self.dir)
        self.assertEqual(gv._read_inflight(self.dir), 1)
        gv.end_fable_call(self.dir)
        self.assertEqual(gv._read_inflight(self.dir), 0)

    def test_end_never_goes_negative(self):
        gv.end_fable_call(self.dir)
        gv.end_fable_call(self.dir)
        self.assertEqual(gv._read_inflight(self.dir), 0)

    def test_corrupt_inflight_file_reads_zero(self):
        with open(gv._inflight_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("not json")
        self.assertEqual(gv._read_inflight(self.dir), 0)


class FableOneshotGate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_allows_when_under_every_quota(self):
        v = gv.check("fable_oneshot", self.dir, conversation_id="c1")
        self.assertTrue(v.allowed)
        self.assertIsNone(v.reason)
        self.assertEqual(v.remaining["day"], 5)
        self.assertEqual(v.remaining["conversation"], 2)

    def test_refuses_when_daily_quota_exhausted(self):
        gv.save(self.dir, {"fable_oneshots_per_day": 1})
        gv.append_spend(self.dir, "fable_oneshot", model="claude-fable-5", conversation_id="c1")
        v = gv.check("fable_oneshot", self.dir, conversation_id="c2")
        self.assertFalse(v.allowed)
        self.assertIn("daily Fable one-shot quota", v.reason)
        self.assertIn("resets at local midnight", v.reason)

    def test_refuses_when_conversation_quota_exhausted(self):
        gv.save(self.dir, {"fable_oneshots_per_conversation": 1})
        gv.append_spend(self.dir, "fable_oneshot", model="claude-fable-5", conversation_id="c1")
        v = gv.check("fable_oneshot", self.dir, conversation_id="c1")
        self.assertFalse(v.allowed)
        self.assertIn("this conversation has reached its Fable one-shot cap", v.reason)
        # a DIFFERENT conversation is unaffected
        v2 = gv.check("fable_oneshot", self.dir, conversation_id="other")
        self.assertTrue(v2.allowed)

    def test_refuses_when_concurrency_limit_reached(self):
        gv.begin_fable_call(self.dir)  # one already in flight, default max is 1
        v = gv.check("fable_oneshot", self.dir, conversation_id="c1")
        self.assertFalse(v.allowed)
        self.assertIn("concurrency limit reached", v.reason)

    def test_refuses_when_fable_daily_token_budget_exhausted(self):
        gv.save(self.dir, {"daily_token_budget_by_model": {"claude-fable-5": 100}})
        gv.append_spend(self.dir, "tokens", model="claude-fable-5", tokens=150)
        v = gv.check("fable_oneshot", self.dir, conversation_id="c1")
        self.assertFalse(v.allowed)
        self.assertIn("daily token budget", v.reason)

    def test_no_conversation_id_skips_conversation_check_but_others_apply(self):
        v = gv.check("fable_oneshot", self.dir)
        self.assertTrue(v.allowed)
        self.assertIsNone(v.remaining["conversation"])

    def test_unknown_kind_fails_open(self):
        v = gv.check("something_unrecognized", self.dir)
        self.assertTrue(v.allowed)


class Alerts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_no_alert_below_threshold(self):
        self.assertFalse(gv.should_alert(self.dir, "k", used=10, limit=100, alert_at_pct=80))

    def test_alert_at_or_above_threshold(self):
        self.assertTrue(gv.should_alert(self.dir, "k", used=80, limit=100, alert_at_pct=80))
        self.assertTrue(gv.should_alert(self.dir, "k", used=95, limit=100, alert_at_pct=80))

    def test_zero_limit_never_alerts(self):
        self.assertFalse(gv.should_alert(self.dir, "k", used=5, limit=0, alert_at_pct=80))

    def test_dedupe_within_realert_window(self):
        now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
        gv.record_alert_sent(self.dir, "k", now=now)
        soon = now + timedelta(hours=1)
        self.assertFalse(gv.should_alert(self.dir, "k", used=90, limit=100, alert_at_pct=80, now=soon))

    def test_realerts_after_window_passes(self):
        now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
        gv.record_alert_sent(self.dir, "k", now=now)
        later = now + timedelta(hours=7)
        self.assertTrue(gv.should_alert(self.dir, "k", used=90, limit=100, alert_at_pct=80, now=later))

    def test_due_alerts_reports_daily_and_weekly(self):
        # Real-now end to end (records stamped moments ago are always "today") — no pinned clock,
        # so this stays green on any date and in any machine timezone.
        gv.save(self.dir, {
            "daily_token_budget_by_model": {"claude-opus-4-8": 100},
            "weekly_token_budget_by_model": {"claude-opus-4-8": 500},
        })
        gv.append_spend(self.dir, "tokens", model="claude-opus-4-8", tokens=90)
        alerts = gv.due_alerts(self.dir, "claude-opus-4-8")
        knobs = {a["knob"] for a in alerts}
        self.assertIn("daily_token_budget_by_model:claude-opus-4-8", knobs)

    def test_due_alerts_empty_when_under_threshold(self):
        gv.append_spend(self.dir, "tokens", model="claude-opus-4-8", tokens=1)
        alerts = gv.due_alerts(self.dir, "claude-opus-4-8")
        self.assertEqual(alerts, [])

    def test_record_alert_sent_persists_and_dedupes_next_due_alerts_call(self):
        gv.save(self.dir, {"daily_token_budget_by_model": {"claude-opus-4-8": 100}})
        gv.append_spend(self.dir, "tokens", model="claude-opus-4-8", tokens=90)
        alerts = gv.due_alerts(self.dir, "claude-opus-4-8")
        self.assertEqual(len(alerts), 1)
        gv.record_alert_sent(self.dir, alerts[0]["knob"])
        alerts_again = gv.due_alerts(self.dir, "claude-opus-4-8")  # seconds later — inside the 6h window
        self.assertEqual(alerts_again, [])


class BillableBasis(unittest.TestCase):
    """The weight table and its derivation — the arithmetic that makes the rail mean something.

    The regression these guard is the ORIGINAL bug, restated: a flat sum over the usage block counts a
    cache read like a fresh input token, so the number tracks conversation length instead of spend.
    The fixture is a realistic long-session warm turn in the exact shape the claude CLI reports, so the
    two bases are compared on the real usage-block layout, not on a shape invented to make the test
    pass."""

    # A cache-heavy warm turn in the CLI's usage-block shape: ~98.9% of its "tokens" are cache reads —
    # which is the whole point.
    REAL_USAGE = {
        "input_tokens": 7,
        "cache_creation_input_tokens": 3051,
        "cache_read_input_tokens": 529573,
        "output_tokens": 2647,
        "cache_creation": {"ephemeral_1h_input_tokens": 3051, "ephemeral_5m_input_tokens": 0},
        "service_tier": "standard",
    }

    def test_raw_tokens_is_the_flat_sum_it_has_always_been(self):
        self.assertEqual(gv.raw_tokens(self.REAL_USAGE), 7 + 3051 + 529573 + 2647)

    def test_raw_tokens_none_when_nothing_reported(self):
        # "nothing reported" must stay distinguishable from "reported zero" — an unread usage block must never pass for a free one.
        self.assertIsNone(gv.raw_tokens({"service_tier": "standard"}))
        self.assertIsNone(gv.raw_tokens(None))
        self.assertEqual(gv.raw_tokens({"input_tokens": 0}), 0)

    def test_components_split_cache_writes_by_ttl(self):
        comp = gv.usage_components(self.REAL_USAGE)
        self.assertEqual(comp, {
            "input": 7, "output": 2647, "cache_read": 529573,
            "cache_write_5m": 0, "cache_write_1h": 3051, "cache_write_unspecified": 0,
        })

    def test_weight_table_applies_as_documented(self):
        # input 7*1 + output 2647*1 + cache_read 529573*0.1 + 1h write 3051*2 = 61,713.3 -> 61,713
        self.assertEqual(gv.billable_tokens(self.REAL_USAGE), 61713)

    def test_billable_is_a_small_fraction_of_raw_on_a_cache_heavy_turn(self):
        raw = gv.raw_tokens(self.REAL_USAGE)
        billable = gv.billable_tokens(self.REAL_USAGE)
        self.assertLess(billable, raw * 0.2)  # the rail was overstating by >5x on turns like this

    def test_cache_write_without_a_ttl_breakdown_books_as_unspecified(self):
        comp = gv.usage_components({"cache_creation_input_tokens": 1000})
        self.assertEqual(comp["cache_write_unspecified"], 1000)
        # priced at the 5m default TTL, 1.25x
        self.assertEqual(gv.billable_tokens({"cache_creation_input_tokens": 1000}), 1250)

    def test_partial_ttl_breakdown_books_the_remainder_rather_than_dropping_it(self):
        comp = gv.usage_components({
            "cache_creation_input_tokens": 1000,
            "cache_creation": {"ephemeral_1h_input_tokens": 400},
        })
        self.assertEqual(comp["cache_write_1h"], 400)
        self.assertEqual(comp["cache_write_unspecified"], 600)

    def test_breakdown_exceeding_its_own_total_is_distrusted_not_double_counted(self):
        comp = gv.usage_components({
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_1h_input_tokens": 900, "ephemeral_5m_input_tokens": 900},
        })
        self.assertEqual(comp["cache_write_1h"], 0)
        self.assertEqual(comp["cache_write_5m"], 0)
        self.assertEqual(comp["cache_write_unspecified"], 100)

    def test_garbage_usage_reads_as_unmetered_rather_than_raising(self):
        # A usage block is provider-shaped data this repo doesn't control; a shape change must degrade
        # to "nothing metered", never take down a chat turn.
        for bad in (None, [], "usage", 7, {"input_tokens": "lots"}, {"cache_creation": "nope"}):
            self.assertIsNone(gv.raw_tokens(bad), bad)
            self.assertIsNone(gv.billable_tokens(bad), bad)

    def test_a_non_numeric_field_alongside_a_good_one_is_ignored_not_fatal(self):
        usage = {"input_tokens": 10, "output_tokens": None, "cache_creation": "nope"}
        self.assertEqual(gv.raw_tokens(usage), 10)
        self.assertEqual(gv.billable_tokens(usage), 10)

    def test_every_weight_key_is_a_component_key(self):
        # The one guard against the table and the normalizer drifting apart: a typo'd weight key would
        # otherwise just silently price its component at zero.
        comp = gv.usage_components({"input_tokens": 1})
        self.assertEqual(set(gv.TOKEN_WEIGHTS), set(comp))


class LedgerBreakdown(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_append_spend_with_usage_records_both_bases(self):
        usage = {"input_tokens": 100, "output_tokens": 100, "cache_read_input_tokens": 1000}
        gv.append_spend(self.dir, "tokens", model="claude-opus-5", usage=usage)
        rec = gv._read_ledger(self.dir)[0]
        self.assertEqual(rec["tokens"], 1200)          # raw sum, unchanged meaning
        self.assertEqual(rec["billable_tokens"], 300)  # 100 + 100 + 1000*0.1
        self.assertEqual(rec["basis"], gv.BILLABLE_BASIS)
        self.assertEqual(rec["components"]["cache_read"], 1000)

    def test_explicit_tokens_scalar_still_writes_a_legacy_shaped_row(self):
        gv.append_spend(self.dir, "tokens", model="m", tokens=42)
        rec = gv._read_ledger(self.dir)[0]
        self.assertEqual(rec["tokens"], 42)
        self.assertNotIn("billable_tokens", rec)

    def test_unmetered_row_carries_no_count_at_all(self):
        # NEVER a 0: the fable gate trusts what it reads, and a 0 reads as "measured, and free".
        gv.append_spend(self.dir, "fable_oneshot", model="claude-fable-5",
                        metered=gv.METERED_UNAVAILABLE)
        rec = gv._read_ledger(self.dir)[0]
        self.assertEqual(rec["metered"], "unavailable")
        self.assertNotIn("tokens", rec)
        self.assertNotIn("billable_tokens", rec)


class BasisAwareRollups(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.now = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
        self.ts = self.now.isoformat().replace("+00:00", "Z")

    def _write(self, *rows):
        # Every row stamped at the SAME instant the rollup queries with, so it lands on the query's
        # owner-local day in any zone — no dependence on the runner's clock or timezone.
        with open(gv.ledger_path(self.dir), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps({"ts": self.ts, **row}) + "\n")

    def test_legacy_row_with_no_billable_still_rolls_up_via_the_raw_fallback(self):
        # THE regression that would matter most: if the fallback broke, every pre-upgrade day would
        # silently read as zero spend and the rails would go quiet on real usage.
        self._write({"kind": "tokens", "model": "m", "tokens": 1000})
        roll = gv.rollups(self.dir, now=self.now)
        self.assertEqual(roll["billable_by_model"]["day"]["m"], 1000)
        self.assertEqual(roll["tokens_by_model"]["day"]["m"], 1000)
        self.assertEqual(roll["basis_by_model"]["day"]["m"], gv.BASIS_RAW)

    def test_new_row_rolls_up_on_the_billable_basis(self):
        self._write({"kind": "tokens", "model": "m", "tokens": 1000, "billable_tokens": 150,
                     "basis": gv.BILLABLE_BASIS})
        roll = gv.rollups(self.dir, now=self.now)
        self.assertEqual(roll["billable_by_model"]["day"]["m"], 150)
        self.assertEqual(roll["tokens_by_model"]["day"]["m"], 1000)  # raw preserved side by side
        self.assertEqual(roll["basis_by_model"]["day"]["m"], gv.BASIS_BILLABLE)

    def test_mixed_basis_day_is_reported_honestly_not_averaged_away(self):
        self._write(
            {"kind": "tokens", "model": "m", "tokens": 1000},                              # legacy
            {"kind": "tokens", "model": "m", "tokens": 1000, "billable_tokens": 150},       # new
        )
        roll = gv.rollups(self.dir, now=self.now)
        self.assertEqual(roll["basis_by_model"]["day"]["m"], gv.BASIS_MIXED)
        self.assertEqual(roll["billable_by_model"]["day"]["m"], 1150)
        self.assertIn("MIXED", gv.describe_basis(gv.BASIS_MIXED))
        self.assertIn("not a single unit", gv.describe_basis(gv.BASIS_MIXED))

    def test_fable_oneshot_row_tokens_are_counted_not_dropped(self):
        # Tokens used to accrue ONLY from kind == "tokens", so a Fable delegation's own spend would
        # have been ignored even once it started reporting usage.
        self._write({"kind": "fable_oneshot", "model": "claude-fable-5", "tokens": 5000,
                     "billable_tokens": 800})
        roll = gv.rollups(self.dir, now=self.now)
        self.assertEqual(roll["billable_by_model"]["day"]["claude-fable-5"], 800)
        self.assertEqual(roll["fable_oneshots"]["day"], 1)  # still counted as a one-shot too

    def test_pre_change_fable_row_counts_as_a_oneshot_but_contributes_no_tokens(self):
        self._write({"kind": "fable_oneshot", "model": "claude-fable-5"})
        roll = gv.rollups(self.dir, now=self.now)
        self.assertEqual(roll["fable_oneshots"]["day"], 1)
        self.assertNotIn("claude-fable-5", roll["billable_by_model"]["day"])
        self.assertIsNone(roll["basis_by_model"]["day"].get("claude-fable-5"))

    def test_unmetered_rows_are_surfaced_as_a_gap_not_folded_in_as_zero(self):
        self._write(
            {"kind": "fable_oneshot", "model": "claude-fable-5", "metered": "unavailable"},
            {"kind": "fable_oneshot", "model": "claude-fable-5", "tokens": 100,
             "billable_tokens": 40},
        )
        roll = gv.rollups(self.dir, now=self.now)
        self.assertEqual(roll["unmetered_by_model"]["day"]["claude-fable-5"], 1)
        self.assertEqual(roll["billable_by_model"]["day"]["claude-fable-5"], 40)
        self.assertEqual(roll["basis_by_model"]["day"]["claude-fable-5"], gv.BASIS_BILLABLE)

    def test_a_model_with_no_rows_has_no_basis(self):
        roll = gv.rollups(self.dir, now=self.now)
        self.assertIsNone(roll["basis_by_model"]["day"].get("nobody"))


class BasisAwareAlerts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.now = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
        self.ts = self.now.isoformat().replace("+00:00", "Z")

    def _write(self, *rows):
        with open(gv.ledger_path(self.dir), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps({"ts": self.ts, **row}) + "\n")

    def test_alert_names_its_basis(self):
        gv.save(self.dir, {"daily_token_budget_by_model": {"m": 100}})
        self._write({"kind": "tokens", "model": "m", "tokens": 900, "billable_tokens": 90})
        alerts = gv.due_alerts(self.dir, "m", now=self.now)
        self.assertEqual(len(alerts), 1)
        self.assertIn("90/100", alerts[0]["text"])          # the BILLABLE figure, not the raw 900
        self.assertIn("billable basis", alerts[0]["text"])
        self.assertIn("cache reads discounted", alerts[0]["text"])

    def test_mixed_basis_alert_says_so_rather_than_presenting_one_unit(self):
        gv.save(self.dir, {"daily_token_budget_by_model": {"m": 100}})
        self._write(
            {"kind": "tokens", "model": "m", "tokens": 50},                            # legacy
            {"kind": "tokens", "model": "m", "tokens": 400, "billable_tokens": 40},    # new
        )
        alerts = gv.due_alerts(self.dir, "m", now=self.now)
        self.assertIn("MIXED basis", alerts[0]["text"])
        self.assertIn("90/100", alerts[0]["text"])

    def test_legacy_only_alert_warns_that_the_number_overstates(self):
        gv.save(self.dir, {"daily_token_budget_by_model": {"m": 100}})
        self._write({"kind": "tokens", "model": "m", "tokens": 90})
        alerts = gv.due_alerts(self.dir, "m", now=self.now)
        self.assertIn("legacy raw basis", alerts[0]["text"])
        self.assertIn("overstates", alerts[0]["text"])

    def test_alert_that_cannot_compute_its_basis_does_not_fire(self):
        # Budget configured, model over threshold on nothing measurable — no unit, no alert.
        gv.save(self.dir, {"daily_token_budget_by_model": {"claude-fable-5": 100}})
        self._write({"kind": "fable_oneshot", "model": "claude-fable-5", "metered": "unavailable"})
        self.assertEqual(gv.due_alerts(self.dir, "claude-fable-5", now=self.now), [])

    def test_alert_discloses_unmetered_calls_alongside_the_figure(self):
        gv.save(self.dir, {"daily_token_budget_by_model": {"claude-fable-5": 100}})
        self._write(
            {"kind": "fable_oneshot", "model": "claude-fable-5", "tokens": 90,
             "billable_tokens": 90},
            {"kind": "fable_oneshot", "model": "claude-fable-5", "metered": "unavailable"},
        )
        alerts = gv.due_alerts(self.dir, "claude-fable-5", now=self.now)
        self.assertIn("could not be metered", alerts[0]["text"])

    def test_a_cache_heavy_day_does_not_false_alarm(self):
        """End-to-end on the real shape: a day of long warm turns against the shipped 30M budget.

        The raw sum blows far past the budget; the billable basis is a small fraction of that and
        stays quiet. If someone reinstates the flat sum, this test is what goes red."""
        gv.save(self.dir, {"daily_token_budget_by_model": {"claude-opus-5": 30_000_000}})
        turn = dict(BillableBasis.REAL_USAGE)
        rows = []
        for _ in range(190):
            rows.append({"kind": "tokens", "model": "claude-opus-5",
                         "tokens": gv.raw_tokens(turn), "billable_tokens": gv.billable_tokens(turn),
                         "components": gv.usage_components(turn)})
        self._write(*rows)
        roll = gv.rollups(self.dir, now=self.now)
        raw = roll["tokens_by_model"]["day"]["claude-opus-5"]
        billable = roll["billable_by_model"]["day"]["claude-opus-5"]
        self.assertGreater(raw, 100_000_000)      # the old basis: >3x the budget
        self.assertLess(billable, 24_000_000)     # the honest one: under the 80% alert line
        self.assertEqual(gv.due_alerts(self.dir, "claude-opus-5", now=self.now), [])


class FableGateOnTheBillableBasis(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_hard_gate_still_refuses_on_a_genuine_quota(self):
        # The rail must still BITE — the fix discounts cache reads, it does not defang the gate.
        gv.save(self.dir, {"daily_token_budget_by_model": {"claude-fable-5": 100}})
        gv.append_spend(self.dir, "fable_oneshot", model="claude-fable-5",
                        usage={"input_tokens": 150, "output_tokens": 0})
        v = gv.check("fable_oneshot", self.dir, conversation_id="c1")
        self.assertFalse(v.allowed)
        self.assertIn("daily token budget", v.reason)
        self.assertIn("billable basis", v.reason)

    def test_gate_reads_billable_not_raw_so_cache_reads_do_not_falsely_exhaust_it(self):
        # 100k of cache reads is 10k billable — under a 50k budget. On the old basis this refused.
        gv.save(self.dir, {"daily_token_budget_by_model": {"claude-fable-5": 50_000}})
        gv.append_spend(self.dir, "fable_oneshot", model="claude-fable-5",
                        usage={"cache_read_input_tokens": 100_000, "output_tokens": 500})
        v = gv.check("fable_oneshot", self.dir, conversation_id="c1")
        self.assertTrue(v.allowed)

    def test_legacy_raw_rows_still_exhaust_the_gate_via_the_fallback(self):
        gv.save(self.dir, {"daily_token_budget_by_model": {"claude-fable-5": 100}})
        gv.append_spend(self.dir, "tokens", model="claude-fable-5", tokens=150)
        v = gv.check("fable_oneshot", self.dir, conversation_id="c1")
        self.assertFalse(v.allowed)
        self.assertIn("legacy raw basis", v.reason)


if __name__ == "__main__":
    unittest.main()
