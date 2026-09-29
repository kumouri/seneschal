#!/usr/bin/env python3
"""Tests for `interleave.py` — the mid-turn relevance gate, its log, and the mode refusal.
Spec: `seneschal/docs/mid-turn-interleave-spec.md` (§4.1 Layer A, §4.2 Layer B, §4.3 the row schema,
§6 the latency budget, §10 the design decisions).

What these guard, in the order the spec cares about:

  * **Layer A may only ever ADD an interleave** (§4.1's invariant) — every frozen phrase steers, no
    phrase can produce a hold, and the classifier is never consulted after a match. A carve-out that
    can veto is a second, dumber classifier;
  * **the vocabulary is FROZEN (§10 D4)** — the list is pinned here so a later addition is a decision
    someone had to take rather than a diff nobody noticed;
  * **fail-open is HOLD, on every path** (§10 D3, §6(3)) — an absent Ollama, a raising classifier, a
    malformed verdict, all of them are today's behaviour;
  * **§4.3's schema, in full** — every field on every row, including on arrivals that would never
    have been eligible, because a rule you cannot see the cost of is a rule you cannot revisit;
  * **`turn_still_live` and a NEGATIVE `turn_remaining_sec`** — the field the phase exists for, and
    the shape §6's corollary is read off.

Stdlib ``unittest``. **No test here makes a real Ollama call**: Layer B is reached only through the
injected `classifier=` seam.

Run:  python -m unittest test_interleave
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import interleave as il  # noqa: E402


def _rows(state_dir):
    with open(il.log_path(state_dir), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class Explodes:
    """A classifier that must never be called."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *_a, **_kw):
        self.calls += 1
        raise AssertionError("Layer B was consulted after a Layer A match")


# --------------------------------------------------------------------------- the mode enum

class ModeRefusal(unittest.TestCase):
    def test_off_and_observe_are_accepted(self):
        self.assertIsNone(il.refuse_mode("off"))
        self.assertIsNone(il.refuse_mode("observe"))
        self.assertIsNone(il.refuse_mode("OBSERVE"))

    def test_live_is_refused_on_an_unprobed_cli_version(self):
        why = il.refuse_mode("live", version_probe=lambda: "9.9.9")
        self.assertIsNotNone(why)
        self.assertIn("9.9.9", why)
        self.assertIn("PROBED_CLI_VERSIONS", why)

    def test_live_is_refused_when_the_version_cannot_be_read(self):
        why = il.refuse_mode("live", version_probe=lambda: None)
        self.assertIsNotNone(why)
        self.assertIn("could not determine", why)

    def test_live_is_accepted_at_a_probed_version(self):
        # Whether to run `live` is the owner's decision (§10 D8); the ONLY thing this predicate
        # refuses on is the per-host CLI-version probe, so a probed version must not be refused.
        for version in il.PROBED_CLI_VERSIONS:
            with self.subTest(version=version):
                self.assertIsNone(il.refuse_mode("live", version_probe=lambda v=version: v))

    def test_the_real_version_probe_never_raises(self):
        # No injected seam — exercises current_cli_version() for real, on whatever `claude` (or lack
        # of one) this host has. Must degrade to a string reason, never throw.
        why = il.refuse_mode("live")
        self.assertTrue(why is None or isinstance(why, str))

    def test_an_unknown_mode_is_refused_too(self):
        self.assertIsNotNone(il.refuse_mode("sideways"))

    def test_the_default_is_observe(self):
        self.assertEqual(il.DEFAULT_MODE, il.MODE_OBSERVE)


# --------------------------------------------------------------------------- Layer A

class CarveOut(unittest.TestCase):
    def test_the_frozen_vocabulary_all_matches(self):
        # §10 D4: "the generic list as specced, no additions". Pinned so an addition is a decision.
        self.assertEqual(
            il.CARVEOUT_PHRASES,
            ("scratch that", "not that one", "wrong one", "never mind", "nevermind", "hold on",
             "hold off", "cancel", "abort", "belay", "undo", "wait", "stop"))
        self.assertEqual(il.CARVEOUT_NEGATED_VERBS,
                         ("send", "run", "merge", "post", "push", "delete", "reply", "commit"))
        for phrase in il.CARVEOUT_PHRASES:
            self.assertIsNotNone(il.carveout_match(f"{phrase} — I got it wrong"), phrase)

    def test_the_negated_verbs_need_their_verb(self):
        for verb in il.CARVEOUT_NEGATED_VERBS:
            self.assertIsNotNone(il.carveout_match(f"don't {verb} that yet"), verb)
            self.assertIsNotNone(il.carveout_match(f"do not {verb} that yet"), verb)
        self.assertIsNone(il.carveout_match("don't worry about the formatting"),
                          "a bare don't is prose, not a directive")

    def test_a_curly_apostrophe_is_the_same_token(self):
        self.assertIsNotNone(il.carveout_match("don’t merge that"))

    def test_word_boundaries_hold(self):
        self.assertIsNone(il.carveout_match("stopping by the store"))
        self.assertIsNone(il.carveout_match("cancellation policy question"))
        self.assertIsNone(il.carveout_match("waiter brought the wrong order"))

    def test_the_200_char_window_is_the_anchoring(self):
        # §4.1's own example: "…stop having insomnia…" must not trip it. It is safe because the word
        # falls past the window, and the window is the whole of what separates a directive from prose
        # about stopping. The accepted cost — the same word INSIDE the window does trip it — is
        # asserted too, so nobody later reads the rule as cleverer than it is.
        prose = ("So the thing about last night is that I lay there for what felt like hours and "
                 "just could not get my brain to shut up about the deploy, which is honestly the "
                 "usual pattern at this point and I have kind of made peace with it, but I really "
                 "would like to stop having insomnia one of these decades.")
        self.assertGreater(prose.index("stop"), il.CARVEOUT_SCAN_CHARS)
        self.assertIsNone(il.carveout_match(prose))
        self.assertIsNotNone(il.carveout_match("I want to stop having insomnia"))

    def test_it_never_raises(self):
        for bad in (None, 17, object(), b"stop"):
            self.assertIsNone(il.carveout_match(bad))


class LayerAMayOnlyAdd(unittest.TestCase):
    """§4.1's invariant: `if match: steer` BEFORE the classifier, never `if no match: hold` after."""

    def test_a_match_short_circuits_layer_b_entirely(self):
        boom = Explodes()
        verdict = il.gate("wait — don't merge that", "The owner asked: merge the PR", classifier=boom)
        self.assertEqual(verdict["layer"], il.LAYER_CARVEOUT)
        self.assertEqual(verdict["verdict"], il.STEER)
        self.assertEqual(boom.calls, 0)

    def test_no_carve_out_verdict_is_ever_a_hold(self):
        for phrase in il.CARVEOUT_PHRASES:
            v = il.gate(f"{phrase} please", "The owner asked: anything", classifier=Explodes())
            self.assertEqual(v["verdict"], il.STEER, phrase)

    def test_a_miss_falls_through_to_layer_b(self):
        seen = {}

        def classifier(msg, in_flight, cfg=None, timeout=20):
            seen["msg"], seen["in_flight"] = msg, in_flight
            return {"verdict": "steer", "confidence": 0.9, "reason": "adds to it", "model": "m"}

        v = il.gate("also add an applied button", "The owner asked: review the job queue", classifier=classifier)
        self.assertEqual(v["layer"], il.LAYER_MODEL)
        self.assertEqual(v["verdict"], il.STEER)
        self.assertEqual(seen["in_flight"], "The owner asked: review the job queue")


class GateFailsOpenToHold(unittest.TestCase):
    """§10 D3 / §6(3): Ollama unreachable, a timeout, bad JSON, an unknown verdict — all today's
    behaviour. A false hold costs a median 44 s; a false steer spends a real turn."""

    def test_a_raising_classifier_holds(self):
        def boom(*_a, **_kw):
            raise OSError("connection refused")

        v = il.gate("also fix the other one", "The owner asked: fix the thing", classifier=boom)
        self.assertEqual(v["verdict"], il.HOLD)
        self.assertEqual(v["layer"], il.LAYER_MODEL)
        self.assertIn("unavailable", v["reason"])

    def test_a_non_dict_verdict_holds(self):
        v = il.gate("m", "f", classifier=lambda *_a, **_kw: "steer")
        self.assertEqual(v["verdict"], il.HOLD)

    def test_an_unknown_verdict_string_holds(self):
        v = il.gate("m", "f", classifier=lambda *_a, **_kw: {"verdict": "maybe", "confidence": 0.99})
        self.assertEqual(v["verdict"], il.HOLD)

    def test_a_hold_is_passed_through_with_its_reason(self):
        v = il.gate("cats fed!", "The owner asked: my calendar",
                    classifier=lambda *_a, **_kw: {"verdict": "hold", "confidence": 0.95,
                                                   "reason": "unrelated status report", "model": "m"})
        self.assertEqual((v["verdict"], v["confidence"]), (il.HOLD, 0.95))
        self.assertEqual(v["reason"], "unrelated status report")


class InFlightSummary(unittest.TestCase):
    def test_it_carries_both_halves(self):
        s = il.in_flight_summary("what's on my calendar tomorrow", ["Read", "Bash"])
        self.assertIn("what's on my calendar tomorrow", s)
        self.assertIn("Read, Bash", s)

    def test_tools_are_deduped_and_bounded_and_text_capped(self):
        s = il.in_flight_summary("x" * 900, ["Read"] * 5 + [f"T{i}" for i in range(40)])
        self.assertLessEqual(len(s.splitlines()[0]), il.IN_FLIGHT_TEXT_CHARS + 40)
        self.assertEqual(s.count("Read"), 1)
        self.assertLessEqual(s.splitlines()[1].count(","), il.IN_FLIGHT_MAX_TOOLS)

    def test_no_tools_yet_says_so_rather_than_going_blank(self):
        self.assertIn("(none yet)", il.in_flight_summary("hey", []))


# --------------------------------------------------------------------------- the log

class ArrivalRowSchema(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_every_4_3_field_is_present(self):
        rid = il.record_arrival(
            self.dir, channel="telegram", turn_id="304bc2a2691c", text="x" * 200,
            layer=il.LAYER_MODEL, verdict=il.STEER, confidence=0.95, reason="adds to it",
            arrived_offset_sec=47.4321, typed=True, fold_depth=2, verdict_latency_sec=1.0512,
            turn_still_live=True)
        row, = _rows(self.dir)
        self.assertEqual(row["arrival_id"], rid)
        self.assertEqual(row["kind"], il.KIND_ARRIVAL)
        for field in ("ts", "channel", "turn_id", "text_preview", "layer", "verdict", "confidence",
                      "reason", "arrived_offset_sec", "arrived_offset_frac", "typed", "fold_depth",
                      "verdict_latency_sec", "turn_still_live", "turn_remaining_sec"):
            self.assertIn(field, row, field)
        self.assertEqual(len(row["text_preview"]), il.PREVIEW_CHARS)
        self.assertEqual(row["arrived_offset_sec"], 47.432)
        self.assertEqual(row["verdict_latency_sec"], 1.051)
        # The two resolution-row fields are null here, never absent: a reader must never have to
        # wonder whether they were measured.
        self.assertIsNone(row["arrived_offset_frac"])
        self.assertIsNone(row["turn_remaining_sec"])

    def test_an_ineligible_arrival_is_still_logged(self):
        # §5.1's non-negotiable 3 says a machine-synthesized arrival may never fold. §4.3 says log it
        # anyway — "a rule you cannot see the cost of is a rule you cannot revisit".
        il.record_arrival(self.dir, channel="telegram", turn_id="t", text="[job finished] 4b1c",
                          layer=il.LAYER_MODEL, verdict=il.HOLD, confidence=0.9, reason="notice",
                          arrived_offset_sec=1.0, typed=False, fold_depth=1,
                          verdict_latency_sec=0.5, turn_still_live=True)
        row, = _rows(self.dir)
        self.assertFalse(row["typed"])

    def test_it_never_raises_and_returns_none_when_the_append_fails(self):
        # A path that cannot be a directory — the row is lost, and nothing else is.
        squatted = os.path.join(self.dir, "squat")
        with open(squatted, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        self.assertIsNone(il.record_arrival(squatted, channel="telegram", turn_id="t", text="hi"))

    def test_an_unserialisable_payload_costs_the_row_and_nothing_else(self):
        self.assertIsNone(il.record_arrival(self.dir, channel=object(), turn_id="t", text="hi",
                                            reason=object()))


class ResolutionIsASecondRow(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_the_arrival_row_is_not_rewritten(self):
        rid = il.record_arrival(self.dir, channel="telegram", turn_id="t1", text="hi",
                                layer=il.LAYER_MODEL, verdict=il.HOLD, confidence=0.8,
                                reason="r", arrived_offset_sec=10.0, typed=True, fold_depth=1,
                                verdict_latency_sec=1.0, turn_still_live=True)
        before = _rows(self.dir)[0]
        self.assertTrue(il.record_resolved(self.dir, arrival_id=rid, turn_id="t1",
                                           turn_remaining_sec=12.1, arrived_offset_frac=0.56,
                                           turn_total_sec=22.1))
        rows = _rows(self.dir)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], before, "append-only: these logs are never updated in place")
        self.assertEqual(rows[1]["kind"], il.KIND_RESOLVED)
        self.assertEqual(rows[1]["turn_id"], "t1")
        self.assertEqual(rows[1]["arrival_id"], rid, "arrival_id is what makes the join exact")
        self.assertEqual(rows[1]["turn_remaining_sec"], 12.1)
        self.assertEqual(rows[1]["arrived_offset_frac"], 0.56)

    def test_a_negative_remaining_is_kept_as_measured(self):
        # A verdict that landed AFTER its turn ended. This is not an error; it is §6's corollary.
        il.record_resolved(self.dir, arrival_id="a", turn_id="t", turn_remaining_sec=-9.4,
                           arrived_offset_frac=1.0, turn_total_sec=51.8)
        self.assertEqual(_rows(self.dir)[0]["turn_remaining_sec"], -9.4)

    def test_it_never_raises(self):
        squatted = os.path.join(self.dir, "squat")
        with open(squatted, "w", encoding="utf-8") as fh:
            fh.write("x")
        self.assertFalse(il.record_resolved(squatted, arrival_id="a", turn_id="t"))


class Stats(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _arrival(self, **over):
        kw = dict(channel="telegram", turn_id="t1", text="hi", layer=il.LAYER_MODEL,
                  verdict=il.HOLD, confidence=0.8, reason="r", arrived_offset_sec=1.0,
                  typed=True, fold_depth=1, verdict_latency_sec=1.0, turn_still_live=True)
        kw.update(over)
        return il.record_arrival(self.dir, **kw)

    def test_classified_counts_only_what_q8s_bar_names(self):
        self._arrival()                                     # typed + model  -> classified
        self._arrival(layer=il.LAYER_CARVEOUT, verdict=il.STEER)   # carve-out -> not classified
        self._arrival(typed=False)                          # machine        -> not classified
        s = il.stats(self.dir)
        self.assertEqual(s["arrivals"], 3)
        self.assertEqual(s["typed"], 2)
        self.assertEqual(s["classified"], 1)
        self.assertEqual(s["q8_classified_target"], 30)

    def test_verdict_landed_in_turn_excludes_carve_outs(self):
        # A carve-out verdict is instant by construction, so counting it would flatter the one number
        # the phase-2 decision turns on.
        self._arrival(turn_still_live=True)
        self._arrival(turn_still_live=False)
        self._arrival(layer=il.LAYER_CARVEOUT, verdict=il.STEER, turn_still_live=True)
        s = il.stats(self.dir)
        self.assertEqual(s["verdict_landed_in_turn"], 1)
        self.assertEqual(s["verdict_landed_late"], 1)

    def test_the_fold_depth_histogram_reports_the_tail(self):
        self._arrival(verdict=il.STEER, fold_depth=1)
        self._arrival(verdict=il.STEER, fold_depth=2)
        self._arrival(verdict=il.STEER, fold_depth=2)
        self._arrival(verdict=il.HOLD, fold_depth=3)  # a hold is not a fold
        self.assertEqual(il.stats(self.dir)["fold_depth_histogram"], {"1": 1, "2": 2})

    def test_it_abstains_on_an_empty_log(self):
        s = il.stats(self.dir)
        self.assertEqual(s["arrivals"], 0)
        self.assertIsNone(s["first_row"])

    # -- the fail-open split: `classified` no longer flatters D8's bar ------------------------------

    def test_a_fail_open_is_classified_but_not_classified_real(self):
        # confidence == 0.0 on a model-layer row is the fallback signal (router.is_fallback_verdict) —
        # it never ran a real judgement, and must not count toward the population D8's bar is read on.
        self._arrival(confidence=0.0, reason="classifier unavailable (TimeoutError)")
        self._arrival(confidence=0.9)  # a real verdict
        s = il.stats(self.dir)
        self.assertEqual(s["classified"], 2)
        self.assertEqual(s["fail_open"], 1)
        self.assertEqual(s["classified_real"], 1)
        self.assertEqual(s["fail_open_rate"], 0.5)

    def test_q8_bar_is_read_on_real_verdicts_only(self):
        for _ in range(29):
            self._arrival(confidence=0.9)
        self._arrival(confidence=0.0, reason="classifier unavailable (URLError)")
        s = il.stats(self.dir)
        # 30 classified rows total, but only 29 are real — the bar (30) is NOT met on the honest count.
        self.assertEqual(s["classified"], 30)
        self.assertEqual(s["classified_real"], 29)
        self.assertFalse(s["q8_met_on_real_verdicts"])
        self._arrival(confidence=0.85)
        s = il.stats(self.dir)
        self.assertEqual(s["classified_real"], 30)
        self.assertTrue(s["q8_met_on_real_verdicts"])

    def test_verdict_landed_in_turn_excludes_fail_opens_too(self):
        self._arrival(turn_still_live=True, confidence=0.9)          # real, live -> counts
        self._arrival(turn_still_live=True, confidence=0.0,          # fail-open, "live" -> excluded
                       reason="classifier unavailable (TimeoutError)")
        s = il.stats(self.dir)
        self.assertEqual(s["verdict_landed_in_turn"], 1)
        self.assertEqual(s["classified_real"], 1)

    def test_zero_confidence_is_the_only_fail_open_signal_a_low_real_score_is_not(self):
        # A genuine sub-threshold verdict keeps its own raw nonzero score (router.py's own contract) —
        # only an exact 0.0 is the fallback.
        self._arrival(confidence=0.05, reason="low confidence (0.05 < 0.70); some reason")
        s = il.stats(self.dir)
        self.assertEqual(s["fail_open"], 0)
        self.assertEqual(s["classified_real"], 1)


class Diagnose(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _arrival(self, ts, **over):
        kw = dict(channel="telegram", turn_id="t1", text="hi", layer=il.LAYER_MODEL,
                  verdict=il.HOLD, confidence=0.8, reason="r", arrived_offset_sec=1.0,
                  typed=True, fold_depth=1, verdict_latency_sec=1.0, turn_still_live=True,
                  now=datetime.fromisoformat(ts))
        kw.update(over)
        return il.record_arrival(self.dir, **kw)

    def test_it_abstains_on_an_empty_log(self):
        d = il.diagnose(self.dir)
        self.assertEqual(d["classified"], 0)
        self.assertIsNone(d["fail_open_rate"])
        self.assertEqual(d["by_week"], [])

    def test_fail_open_reasons_are_parsed_from_the_reason_string(self):
        self._arrival("2026-09-08T12:00:00+00:00", confidence=0.0,
                      reason="classifier unavailable (TimeoutError)")
        self._arrival("2026-09-08T12:05:00+00:00", confidence=0.0,
                      reason="classifier unavailable (URLError)")
        self._arrival("2026-09-08T12:10:00+00:00", confidence=0.0,
                      reason="classifier unavailable (TimeoutError)")
        self._arrival("2026-09-08T12:15:00+00:00", confidence=0.9)  # real, not counted in reasons
        d = il.diagnose(self.dir)
        self.assertEqual(d["fail_open_reasons"], {"TimeoutError": 2, "URLError": 1})
        self.assertEqual(d["fail_open"], 3)
        self.assertEqual(d["classified"], 4)

    def test_by_week_buckets_are_iso_weeks(self):
        # 2026-09-08 is a Tuesday in ISO week 2026-W37; 2026-08-24 is ISO week 2026-W35.
        self._arrival("2026-09-08T12:00:00+00:00", confidence=0.0,
                      reason="classifier unavailable (TimeoutError)")
        self._arrival("2026-09-08T13:00:00+00:00", confidence=0.9)
        self._arrival("2026-08-24T12:00:00+00:00", confidence=0.9)
        d = il.diagnose(self.dir)
        by_week = {row["week"]: row for row in d["by_week"]}
        self.assertEqual(by_week["2026-W37"]["rows"], 2)
        self.assertEqual(by_week["2026-W37"]["fail_open"], 1)
        self.assertEqual(by_week["2026-W37"]["rate"], 0.5)
        self.assertEqual(by_week["2026-W35"]["rows"], 1)
        self.assertEqual(by_week["2026-W35"]["fail_open"], 0)

    def test_latency_is_split_real_vs_fail_open(self):
        self._arrival("2026-09-08T12:00:00+00:00", confidence=0.9, verdict_latency_sec=1.0)
        self._arrival("2026-09-08T12:01:00+00:00", confidence=0.9, verdict_latency_sec=3.0)
        self._arrival("2026-09-08T12:02:00+00:00", confidence=0.0, verdict_latency_sec=20.0,
                      reason="classifier unavailable (TimeoutError)")
        d = il.diagnose(self.dir)
        self.assertEqual(d["latency_real"]["n"], 2)
        self.assertEqual(d["latency_real"]["median"], 2.0)
        self.assertEqual(d["latency_fail_open"]["n"], 1)
        self.assertEqual(d["latency_fail_open"]["median"], 20.0)

    def test_it_never_raises_on_a_torn_log(self):
        path = il.log_path(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json\n")
        d = il.diagnose(self.dir)
        self.assertEqual(d["classified"], 0)


class Retention(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write(self, ts, arrival_id="a"):
        with open(il.log_path(self.dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema": il.SCHEMA, "kind": il.KIND_ARRIVAL,
                                 "arrival_id": arrival_id, "ts": ts}) + "\n")

    def test_the_default_keeps_everything_and_nothing_calls_it(self):
        self.assertEqual(il.RETENTION_DAYS, 0)
        self._write("2020-01-01T00:00:00Z")
        self.assertEqual(il.prune(self.dir), 0)
        self.assertEqual(len(_rows(self.dir)), 1)

    def test_a_positive_days_drops_older_rows_and_leaves_no_temp_file(self):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat().replace("+00:00", "Z")
        new = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._write(old, "old")
        self._write(new, "new")
        self.assertEqual(il.prune(self.dir, days=30), 1)
        self.assertEqual([r["arrival_id"] for r in _rows(self.dir)], ["new"])
        self.assertFalse(os.path.exists(il.log_path(self.dir) + ".tmp"),
                         "build-then-replace, never truncate-write")

    def test_an_unparseable_stamp_is_kept(self):
        self._write("not a date", "garbled")
        self.assertEqual(il.prune(self.dir, days=1), 0)
        self.assertEqual(len(_rows(self.dir)), 1)


class TolerantReader(unittest.TestCase):
    def test_a_torn_line_is_skipped_not_fatal(self):
        d = tempfile.mkdtemp()
        with open(il.log_path(d), "w", encoding="utf-8") as fh:
            fh.write('{"kind":"interleave.arrival","typed":true,"layer":"model"}\n')
            fh.write('{"kind":"interleave.arriv\n')   # torn tail
        self.assertEqual(len(il.read_rows(d)), 1)

    def test_a_missing_file_reads_as_empty(self):
        self.assertEqual(il.read_rows(tempfile.mkdtemp()), [])


if __name__ == "__main__":
    unittest.main()
