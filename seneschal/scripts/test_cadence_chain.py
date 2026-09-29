#!/usr/bin/env python3
"""Tests for `cadence_chain.py` — the cadence check-chain (named pure handlers, an enumeration verdict, no side effects).

**What this suite is guarding, in one sentence each.**

* **`TheEnumeration`** — a handler returns one of four names, never a boolean, and an
  unrecognised return value is a config error (`ABORT`), never a crash.
* **`TheFilterChainSemantics`** — this is a filter chain, not Chain of Responsibility. Every
  handler runs unless one of them `EXCLUDE`s/`ABORT`s; a single `INCLUDE` among `SKIP`s still
  includes; `EXCLUDE`/`ABORT` short-circuit the handlers after them.
* **`UnknownHandlerNames`** — a typo in a row's `checks` list is a loud, reportable `ABORT`, never a
  `KeyError`/crash and never a silent `EXCLUDE`.
* **`Purity`** — a handler cannot mutate the row it is given; `evaluate()` enforces this
  structurally (a frozen mapping), not by convention.
* **`TheRegistryAndListing`** — the registry is the one place a handler is reachable from, and
  `list_handlers()` is what a reader should trust over this module's own docstring table.
* **`TheHandlersThemselves`** — each of the three shipped handlers, at its own threshold's boundary.
* **`DryRunOverAFixtureStore`** — `--dry-run`/`dry_run_store` reads the store and writes nothing.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cadence_chain  # noqa: E402
import loops  # noqa: E402

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _row(**over):
    base = dict(
        id="loop-20260901-example",
        text="an example row",
        audience="owner",
        status="open",
        opened="2026-09-01T09:00:00+00:00",
        last_touched="2026-09-01T09:00:00+00:00",
        whose_move="assistant",
        next_action="do the thing",
        kind_assistant="enhancement",
        priority_assistant="normal",
        kind_owner=None,
        priority_owner=None,
        checks=None,
    )
    base.update(over)
    return base


def _days_ago(n: int) -> str:
    return (NOW - timedelta(days=n)).isoformat()


class TheEnumeration(unittest.TestCase):
    def test_all_four_names_are_distinct_strings(self):
        self.assertEqual(len(set(cadence_chain.ALL_VERDICTS)), 4)
        for v in cadence_chain.ALL_VERDICTS:
            self.assertIsInstance(v, str)

    def test_a_handler_returning_something_else_aborts_rather_than_crashing(self):
        def bad_handler(row, now, ctx):
            return True  # a boolean, exactly what the enumeration refuses

        cadence_chain.HANDLERS["_test-bad-handler"] = bad_handler
        try:
            result = cadence_chain.evaluate(_row(checks=["_test-bad-handler"]), NOW)
            self.assertEqual(result["verdict"], cadence_chain.Verdict.ABORT)
            self.assertIn("_test-bad-handler", result["reason"])
        finally:
            del cadence_chain.HANDLERS["_test-bad-handler"]


class UnknownHandlerNames(unittest.TestCase):
    def test_unknown_handler_name_aborts_with_a_clear_reason_never_a_crash(self):
        result = cadence_chain.evaluate(_row(checks=["not-a-real-handler"]), NOW)
        self.assertEqual(result["verdict"], cadence_chain.Verdict.ABORT)
        self.assertIn("not-a-real-handler", result["reason"])
        self.assertIsNone(result["decided_by"])

    def test_an_unknown_name_earlier_in_the_chain_stops_before_a_later_real_one(self):
        calls = []

        def spy(row, now, ctx):
            calls.append("spy")
            return cadence_chain.Verdict.INCLUDE

        cadence_chain.HANDLERS["_test-spy"] = spy
        try:
            result = cadence_chain.evaluate(
                _row(checks=["nope", "_test-spy"]), NOW)
            self.assertEqual(result["verdict"], cadence_chain.Verdict.ABORT)
            self.assertEqual(calls, [])  # the spy after the bad name never ran
        finally:
            del cadence_chain.HANDLERS["_test-spy"]


class TheFilterChainSemantics(unittest.TestCase):
    """A filter chain, not Chain of Responsibility."""

    def _skip(self, row, now, ctx):
        return cadence_chain.Verdict.SKIP

    def _include(self, row, now, ctx):
        return cadence_chain.Verdict.INCLUDE

    def _exclude(self, row, now, ctx):
        return cadence_chain.Verdict.EXCLUDE

    def _abort(self, row, now, ctx):
        return cadence_chain.Verdict.ABORT

    def _install(self, **handlers):
        for name, fn in handlers.items():
            cadence_chain.HANDLERS[name] = fn
        self.addCleanup(lambda: [cadence_chain.HANDLERS.pop(n, None) for n in handlers])

    def test_all_skip_is_a_skip_verdict_not_include_or_exclude(self):
        self._install(**{"_t-skip-a": self._skip, "_t-skip-b": self._skip})
        result = cadence_chain.evaluate(_row(checks=["_t-skip-a", "_t-skip-b"]), NOW)
        self.assertEqual(result["verdict"], cadence_chain.Verdict.SKIP)
        self.assertIsNone(result["decided_by"])

    def test_one_include_among_skips_includes_the_disjunction(self):
        self._install(**{"_t-skip": self._skip, "_t-include": self._include})
        result = cadence_chain.evaluate(
            _row(checks=["_t-skip", "_t-include", "_t-skip"]), NOW)
        self.assertEqual(result["verdict"], cadence_chain.Verdict.INCLUDE)
        self.assertEqual(result["decided_by"], "_t-include")

    def test_exclude_after_include_overrides_it_short_circuiting_what_follows(self):
        calls = []

        def tail(row, now, ctx):
            calls.append("tail")
            return cadence_chain.Verdict.INCLUDE

        self._install(**{"_t-include": self._include, "_t-exclude": self._exclude,
                          "_t-tail": tail})
        result = cadence_chain.evaluate(
            _row(checks=["_t-include", "_t-exclude", "_t-tail"]), NOW)
        self.assertEqual(result["verdict"], cadence_chain.Verdict.EXCLUDE)
        self.assertEqual(result["decided_by"], "_t-exclude")
        self.assertEqual(calls, [])  # the handler after EXCLUDE never ran — the short-circuit

    def test_abort_short_circuits_exactly_like_exclude(self):
        calls = []

        def tail(row, now, ctx):
            calls.append("tail")
            return cadence_chain.Verdict.INCLUDE

        self._install(**{"_t-abort": self._abort, "_t-tail": tail})
        result = cadence_chain.evaluate(_row(checks=["_t-abort", "_t-tail"]), NOW)
        self.assertEqual(result["verdict"], cadence_chain.Verdict.ABORT)
        self.assertEqual(result["decided_by"], "_t-abort")
        self.assertEqual(calls, [])


class Purity(unittest.TestCase):
    def test_a_handler_may_not_mutate_the_row_it_is_given(self):
        def rude_handler(row, now, ctx):
            row["status"] = "done"  # attempted mutation — must raise, not silently succeed
            return cadence_chain.Verdict.SKIP

        cadence_chain.HANDLERS["_test-rude"] = rude_handler
        try:
            with self.assertRaises(TypeError):
                cadence_chain.evaluate(_row(checks=["_test-rude"]), NOW)
        finally:
            del cadence_chain.HANDLERS["_test-rude"]

    def test_the_row_passed_in_is_never_mutated_even_on_a_frozen_copy(self):
        original = _row()
        frozen_copy = types.MappingProxyType(dict(original))
        cadence_chain.evaluate(original, NOW)
        self.assertEqual(dict(frozen_copy), original)

    def test_each_shipped_handler_receives_a_read_only_mapping(self):
        for name, fn in cadence_chain.HANDLERS.items():
            with self.subTest(handler=name):
                frozen = types.MappingProxyType(_row())
                # Must not raise for a well-behaved (read-only) handler.
                fn(frozen, NOW, {})
                with self.assertRaises(TypeError):
                    frozen["status"] = "done"


class TheRegistryAndListing(unittest.TestCase):
    def test_list_handlers_matches_the_live_registry_exactly(self):
        rows = cadence_chain.list_handlers()
        self.assertEqual({r["name"] for r in rows}, set(cadence_chain.HANDLERS))

    def test_every_listed_handler_has_a_non_empty_summary(self):
        for row in cadence_chain.list_handlers():
            self.assertTrue(row["summary"], f"{row['name']} has no docstring first line")

    def test_default_chain_names_are_all_real_handlers(self):
        for name in cadence_chain.DEFAULT_CHAIN:
            self.assertIn(name, cadence_chain.HANDLERS)

    def test_cli_list_prints_every_handler_name(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cadence_chain.main(["list"])
        self.assertEqual(code, 0)
        for name in cadence_chain.HANDLERS:
            self.assertIn(name, out.getvalue())


class TheHandlersThemselves(unittest.TestCase):
    def test_resolved_excluded_fires_on_every_terminal_status(self):
        for status in loops.TERMINAL_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(
                    cadence_chain.resolved_excluded(_row(status=status), NOW, {}),
                    cadence_chain.Verdict.EXCLUDE)

    def test_resolved_excluded_skips_open_held_and_paused(self):
        for status in ("open", "held", "paused"):
            with self.subTest(status=status):
                self.assertEqual(
                    cadence_chain.resolved_excluded(_row(status=status), NOW, {}),
                    cadence_chain.Verdict.SKIP)

    def test_dormant_after_14_days_excludes_past_the_threshold(self):
        row = _row(status="open", last_touched=_days_ago(15))
        self.assertEqual(cadence_chain.dormant_after_14_days(row, NOW, {}),
                          cadence_chain.Verdict.EXCLUDE)

    def test_dormant_after_14_days_skips_at_and_under_the_threshold(self):
        row = _row(status="open", last_touched=_days_ago(14))
        self.assertEqual(cadence_chain.dormant_after_14_days(row, NOW, {}),
                          cadence_chain.Verdict.SKIP)

    def test_dormant_after_14_days_skips_non_open_rows(self):
        row = _row(status="held", last_touched=_days_ago(30))
        self.assertEqual(cadence_chain.dormant_after_14_days(row, NOW, {}),
                          cadence_chain.Verdict.SKIP)

    def test_reask_includes_the_owners_move_past_ten_days(self):
        row = _row(status="open", whose_move="owner", last_touched=_days_ago(11))
        self.assertEqual(cadence_chain.reask_owner_after_10_days(row, NOW, {}),
                          cadence_chain.Verdict.INCLUDE)

    def test_reask_skips_at_and_under_ten_days(self):
        row = _row(status="open", whose_move="owner", last_touched=_days_ago(10))
        self.assertEqual(cadence_chain.reask_owner_after_10_days(row, NOW, {}),
                          cadence_chain.Verdict.SKIP)

    def test_reask_skips_when_whose_move_is_not_owner(self):
        row = _row(status="open", whose_move="assistant", last_touched=_days_ago(30))
        self.assertEqual(cadence_chain.reask_owner_after_10_days(row, NOW, {}),
                          cadence_chain.Verdict.SKIP)

    def test_reask_includes_a_both_row_past_ten_days(self):
        # A joint item re-asks the same way an owner-move item does; the phrasing difference is
        # loops.ask_line()'s job, not this handler's.
        row = _row(status="open", whose_move="both", last_touched=_days_ago(11))
        self.assertEqual(cadence_chain.reask_owner_after_10_days(row, NOW, {}),
                          cadence_chain.Verdict.INCLUDE)

    def test_default_chain_source_of_truth_is_loops_py(self):
        # These must be aliases, not a second copy of the numbers.
        self.assertEqual(cadence_chain.DORMANT_AFTER_DAYS, loops.DORMANT_AFTER_DAYS)
        self.assertEqual(cadence_chain.REASK_AFTER_DAYS, loops.REASK_AFTER_DAYS)

    def test_default_chain_composition_only_includes_inside_the_10_to_14_day_window(self):
        # Past 14 days: dormant-after-14-days EXCLUDEs before reask ever runs (the 10-14 day window).
        stale = _row(status="open", whose_move="owner", last_touched=_days_ago(20))
        result = cadence_chain.evaluate(stale, NOW)
        self.assertEqual(result["verdict"], cadence_chain.Verdict.EXCLUDE)
        self.assertEqual(result["decided_by"], "dormant-after-14-days")

        # Inside the window (11 days): reask-owner-after-10-days gets to fire.
        in_window = _row(status="open", whose_move="owner", last_touched=_days_ago(11))
        result = cadence_chain.evaluate(in_window, NOW)
        self.assertEqual(result["verdict"], cadence_chain.Verdict.INCLUDE)
        self.assertEqual(result["decided_by"], "reask-owner-after-10-days")

        # A `both` row obeys the same 10-14 day window.
        both_stale = _row(status="open", whose_move="both", last_touched=_days_ago(20))
        self.assertEqual(cadence_chain.evaluate(both_stale, NOW)["verdict"],
                          cadence_chain.Verdict.EXCLUDE)
        both_in_window = _row(status="open", whose_move="both", last_touched=_days_ago(11))
        self.assertEqual(cadence_chain.evaluate(both_in_window, NOW)["decided_by"],
                          "reask-owner-after-10-days")

    def test_default_chain_used_when_row_carries_no_checks_field(self):
        row = _row(checks=None)
        # Should not raise, and should be evaluable the same as passing DEFAULT_CHAIN explicitly.
        via_default = cadence_chain.evaluate(row, NOW)
        via_explicit = cadence_chain.evaluate(
            dict(row, checks=list(cadence_chain.DEFAULT_CHAIN)), NOW)
        self.assertEqual(via_default, via_explicit)


class DryRunOverAFixtureStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _add(self, **over):
        kwargs = dict(
            text="a fixture row", audience="owner",
            terminal_state="it ships or the owner says drop it",
            whose_move="owner", next_action="unknown",
            kind="chore", priority="normal",
        )
        kwargs.update(over)
        return loops.add(self.state, **kwargs)

    def test_dry_run_touches_no_file(self):
        self._add()
        before = io.open(loops.store_path(self.state), "rb").read()
        cadence_chain.dry_run_store(self.state)
        after = io.open(loops.store_path(self.state), "rb").read()
        self.assertEqual(before, after)

    def test_dry_run_covers_every_row_with_a_verdict_and_a_decider(self):
        a = self._add(text="row one")
        b = self._add(text="row two", whose_move="assistant")
        results = cadence_chain.dry_run_store(self.state, now=NOW)
        ids = {r["id"] for r in results}
        self.assertEqual(ids, {a["id"], b["id"]})
        for r in results:
            self.assertIn(r["verdict"], cadence_chain.ALL_VERDICTS)
            self.assertIn("reason", r)

    def test_cli_dry_run_prints_one_line_per_row_and_writes_nothing(self):
        self._add()
        self._add(text="a second fixture row")
        before = io.open(loops.store_path(self.state), "rb").read()
        out = io.StringIO()
        with redirect_stdout(out):
            code = cadence_chain.main(["dry-run", "--state-dir", self.state])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 2)
        after = io.open(loops.store_path(self.state), "rb").read()
        self.assertEqual(before, after)

    def test_cli_check_reports_one_rows_verdict(self):
        item = self._add()
        out = io.StringIO()
        with redirect_stdout(out):
            code = cadence_chain.main(["check", item["id"], "--state-dir", self.state])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["id"], item["id"])
        self.assertIn("verdict", payload)


if __name__ == "__main__":
    unittest.main()
