#!/usr/bin/env python3
"""Tests for governor.py — Oikonomos, the budget-governor advisor (cockpit-spec.md v3.5).

Stdlib unittest only, no network, no real `claude`/Ollama calls. Covers: SCHEMA validation, tolerant
load / strict save (incl. forward-compat unknown-key preservation), the owner-local day/week rollups
(incl. the after-midnight-is-still-yesterday boundary, exercised through tz_common with a mocked
fixed-offset owner zone), the fable_oneshot rail gate, the concurrency counter, and alert dedupe.

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


if __name__ == "__main__":
    unittest.main()
