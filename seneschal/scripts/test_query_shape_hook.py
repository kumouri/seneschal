#!/usr/bin/env python3
"""Tests for ``query_shape_hook`` — the report-only `PostToolUse` annotation of a Notion query that
carries `LIMIT` with no `ORDER BY`.

Four properties, in the order they matter:

1. **It cannot block, cost a query, or raise.** Every path returns 0 and every hostile input is
   silently allowed through. This is a hook that fires in every Claude Code session on the machine.
2. **It fires on the instance it was built for**, and on the shapes that instance generalises to.
3. **It does not fire on the shapes that look like it and are not** — a quoted literal, an ordered
   query, a different tool. The number which kills a gate is its false-positive rate, and this one
   has no tuning knob to hide behind.
4. **It is Notion-only.** On any other active store backend it is a strict no-op: nothing printed,
   nothing logged (``NotionOnlyGateTest``).

The module-level fixture points the hook's store config at a temporary Notion config, so every class
but the gate's own runs as a Notion install, whatever the checkout's real `store/config.json` says.

Nothing here touches the network, the live state directory, or a real hook event source.
"""
import io
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import query_shape_hook as qs  # noqa: E402

NOTION = "mcp__notion__notion-query-data-sources"

#: The failure shape this hook exists for: a filtered lookup, truncated with no order.
THE_INSTANCE = 'SELECT * FROM "collection://x" WHERE Name LIKE \'%Sentinel%\' LIMIT 4'

_FIXTURE_DIR = None
_SAVED = None


def _write_store_config(directory, active):
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"active": active, "backends": {}}, fh)
    return path


def setUpModule():
    """Every test runs as a Notion install unless it says otherwise — hermetic against the checkout's
    own (gitignored) store config and any legacy notion-mcp.json."""
    global _FIXTURE_DIR, _SAVED
    _FIXTURE_DIR = tempfile.mkdtemp()
    _SAVED = (qs.STORE_CONFIG, qs.LEGACY_NOTION_MCP)
    qs.STORE_CONFIG = _write_store_config(_FIXTURE_DIR, "notion")
    qs.LEGACY_NOTION_MCP = os.path.join(_FIXTURE_DIR, "no-such-notion-mcp.json")


def tearDownModule():
    qs.STORE_CONFIG, qs.LEGACY_NOTION_MCP = _SAVED


def _event(tool=NOTION, query=THE_INSTANCE, session="s-1", **extra):
    ev = {"hook_event_name": "PostToolUse", "session_id": session, "tool_name": tool,
          "tool_input": {"data": {"data_source_urls": ["collection://x"], "query": query}},
          "tool_response": {"rows": []}}
    ev.update(extra)
    return ev


class ThePredicate(unittest.TestCase):
    """`limit_without_order` is the whole detector; a change here changes what fires everywhere."""

    def test_the_instance_fires(self):
        self.assertTrue(qs.limit_without_order(THE_INSTANCE))

    def test_an_ordered_limit_does_not_fire(self):
        """The correctly-ordered query issued against the SAME table moments earlier. If this fired,
        the annotation would be wrong beside a visible ORDER BY — which reads as a broken checker."""
        self.assertFalse(qs.limit_without_order(
            'SELECT * FROM "collection://x" ORDER BY datetime(Date) DESC LIMIT 4'))

    def test_no_limit_does_not_fire(self):
        self.assertFalse(qs.limit_without_order('SELECT * FROM "collection://x" WHERE A = 1'))

    def test_order_by_is_matched_as_two_words_not_as_a_substring(self):
        self.assertTrue(qs.limit_without_order('SELECT "Order" FROM "c://x" LIMIT 2'),
                        'a column called Order is not an ORDER BY')
        self.assertTrue(qs.limit_without_order('SELECT * FROM "c://x" WHERE Reorder = 1 LIMIT 2'))

    def test_limit_is_matched_as_a_word(self):
        self.assertFalse(qs.limit_without_order('SELECT MyLimit FROM "c://x"'))
        self.assertFalse(qs.limit_without_order('SELECT * FROM "c://x" WHERE LIMITED = 1'))

    def test_case_does_not_matter(self):
        self.assertTrue(qs.limit_without_order('select * from "c://x" limit 4'))
        self.assertFalse(qs.limit_without_order('select * from "c://x" order by A limit 4'))

    def test_a_literal_containing_the_word_does_not_invent_a_finding(self):
        """A row whose text happens to contain LIMIT. Notion data is single-quoted."""
        self.assertFalse(qs.limit_without_order(
            "SELECT * FROM \"c://x\" WHERE Note LIKE '%no LIMIT today%'"))

    def test_a_literal_containing_order_by_does_not_suppress_a_real_finding(self):
        """The direction that matters more: a suppressed finding is invisible."""
        self.assertTrue(qs.limit_without_order(
            "SELECT * FROM \"c://x\" WHERE Note = 'sorted ORDER BY hand' LIMIT 3"))

    def test_a_doubled_quote_inside_a_literal_does_not_unbalance_the_scan(self):
        self.assertTrue(qs.limit_without_order(
            "SELECT * FROM \"c://x\" WHERE N = 'it''s fine' LIMIT 3"))
        self.assertFalse(qs.limit_without_order(
            "SELECT * FROM \"c://x\" WHERE N = 'it''s LIMIT fine'"))

    def test_junk_is_never_a_finding(self):
        for bad in (None, 42, "", "   ", [], {}, object()):
            self.assertFalse(qs.limit_without_order(bad))


class WhatItReadsOutOfTheEvent(unittest.TestCase):

    def test_the_arguments_are_read_from_under_data(self):
        self.assertEqual(qs.notion_sql({"data": {"query": "SELECT 1 LIMIT 1"}}), "SELECT 1 LIMIT 1")

    def test_an_unnested_input_is_tolerated(self):
        """Reading it costs nothing, and assuming the wrapper is how a hook silently stops firing."""
        self.assertEqual(qs.notion_sql({"query": "SELECT 1"}), "SELECT 1")

    def test_view_mode_has_no_sql_and_is_not_this_detectors_business(self):
        """A `view`-mode query returns 100 rows and says nothing about it — the same bug, and
        explicitly a disabled detector's (`DETECTORS`), not something to sneak in here."""
        self.assertIsNone(qs.notion_sql({"data": {"mode": "view", "view_url": "https://n.so/x?v=1",
                                                  "page_size": 100}}))

    def test_junk_inputs_read_as_no_sql(self):
        for bad in (None, "sql", 7, [], {"data": "nope"}, {"data": {"query": 12}},
                    {"data": {"query": "  "}}):
            self.assertIsNone(qs.notion_sql(bad))


class WhichToolsFire(unittest.TestCase):

    def test_the_notion_query_tool_fires(self):
        det, line = qs.detect(NOTION, _event()["tool_input"])
        self.assertEqual(det, "notion_limit_without_order")
        self.assertIn("not evidence of absence", line)

    def test_a_differently_aliased_mcp_server_still_fires(self):
        """Matched by suffix: the same Notion tool behind `mcp__notion-work__…` is the same tool."""
        det, _ = qs.detect("mcp__notion-work__notion-query-data-sources", _event()["tool_input"])
        self.assertEqual(det, "notion_limit_without_order")

    def test_an_unrelated_tool_never_fires_even_holding_the_same_text(self):
        for tool in ("Bash", "Read", "mcp__notion__notion-search", "mcp__notion__notion-fetch"):
            det, line = qs.detect(tool, {"data": {"query": THE_INSTANCE}})
            self.assertIsNone(det, tool)
            self.assertIsNone(line, tool)

    def test_the_disabled_detectors_are_declared_and_do_not_run(self):
        """They are named in `DETECTORS` so the seam is visible, and shipped off because three
        unmeasured firing rates behind one gate is how a gate gets disabled."""
        by_id = {d[0]: d for d in qs.DETECTORS}
        self.assertEqual(set(by_id), {"notion_limit_without_order", "grep_head_n",
                                      "notion_default_page_size"})
        self.assertTrue(by_id["notion_limit_without_order"][1])
        self.assertFalse(by_id["grep_head_n"][1])
        self.assertFalse(by_id["notion_default_page_size"][1])

    def test_junk_tool_names_are_not_our_business(self):
        for name in (None, 42, "", [], {}):
            self.assertEqual(qs.detect(name, {"data": {"query": THE_INSTANCE}}), (None, None))


class TheFireLog(unittest.TestCase):
    """Written by the code path, not by a model choosing to write it — code-written logs are
    complete, model-written ones are not."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _rows(self):
        path = qs.log_path(self.dir)
        if not os.path.exists(path):
            return []
        with io.open(path, encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def test_a_fire_is_logged_and_the_line_is_returned(self):
        line = qs.decide(_event(), state_dir=self.dir)
        self.assertIn("not evidence of absence", line)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["detector"], "notion_limit_without_order")
        self.assertTrue(rows[0]["annotated"])
        self.assertEqual(rows[0]["session_id"], "s-1")
        self.assertTrue(rows[0]["at"].endswith("Z"))

    def test_a_non_fire_writes_nothing(self):
        self.assertIsNone(qs.decide(_event(query='SELECT * FROM "c://x" ORDER BY A LIMIT 4'),
                                    state_dir=self.dir))
        self.assertEqual(self._rows(), [])

    def test_report_only_logs_the_fire_and_suppresses_the_line(self):
        """A measurement period: the rate becomes knowable with zero behaviour change."""
        self.assertIsNone(qs.decide(_event(), report_only=True, state_dir=self.dir))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["annotated"])

    def test_the_query_is_never_written_to_the_log(self):
        """A Notion SQL string carries the owner's column VALUES. A fire log is not a place for
        that."""
        qs.decide(_event(), state_dir=self.dir)
        blob = json.dumps(self._rows())
        self.assertNotIn("Sentinel", blob)
        self.assertNotIn("SELECT", blob)
        self.assertNotIn("collection://", blob)

    def test_an_unwritable_log_costs_the_row_and_never_the_annotation(self):
        line = qs.decide(_event(), state_dir=os.path.join(self.dir, "x\x00y"))
        self.assertIn("not evidence of absence", line)

    def test_log_fire_answers_rather_than_raising(self):
        self.assertIsInstance(qs.log_fire("d", {}, True, state_dir=self.dir), bool)
        self.assertIsInstance(qs.log_fire("d", "not an event", True, state_dir=self.dir), bool)


class TheHookContract(unittest.TestCase):
    """The properties that make this safe to install machine-wide."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._prev = os.environ.get(qs.STATE_DIR_ENV_VAR)
        os.environ[qs.STATE_DIR_ENV_VAR] = self.dir

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(qs.STATE_DIR_ENV_VAR, None)
        else:
            os.environ[qs.STATE_DIR_ENV_VAR] = self._prev

    def _run(self, payload, argv=()):
        out = io.StringIO()
        code = qs.main(argv=list(argv), stdin=io.StringIO(payload), stdout=out)
        return code, out.getvalue()

    def test_a_fire_prints_only_additional_context(self):
        code, out = self._run(json.dumps(_event()))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(set(payload), {"hookSpecificOutput"})
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("not evidence of absence",
                      payload["hookSpecificOutput"]["additionalContext"])

    def test_nothing_this_module_can_emit_is_a_block(self):
        """`decision: "block"` exists on this event and would make an annotation read as a failure.
        There is no code path here that produces one — asserted over every branch, not by reading."""
        for payload in (json.dumps(_event()),
                        json.dumps(_event(query='SELECT * FROM "c://x" ORDER BY A LIMIT 1')),
                        json.dumps(_event(tool="Bash")),
                        "not json", "", "   ", "null", "[]", json.dumps({"tool_name": None})):
            code, out = self._run(payload)
            self.assertEqual(code, 0, payload[:40])
            self.assertNotIn("block", out)
            self.assertNotIn("deny", out)

    def test_a_non_fire_prints_nothing_at_all(self):
        code, out = self._run(json.dumps(_event(tool="Read")))
        self.assertEqual((code, out), (0, ""))

    def test_every_hostile_input_exits_zero_silently(self):
        for payload in ("", "   ", "not json", "null", "[]", "42", '{"tool_input": 7}',
                        '{"tool_name": [], "tool_input": null}',
                        json.dumps({"tool_name": NOTION, "tool_input": {"data": {"query": None}}})):
            code, out = self._run(payload)
            self.assertEqual(code, 0, payload)
            self.assertEqual(out, "", payload)

    def test_an_unreadable_stdin_exits_zero(self):
        class Exploding:
            def read(self):
                raise OSError("boom")

        self.assertEqual(qs.main(argv=[], stdin=Exploding(), stdout=io.StringIO()), 0)

    def test_report_only_flag_suppresses_the_output(self):
        code, out = self._run(json.dumps(_event()), argv=["--report-only"])
        self.assertEqual((code, out), (0, ""))

    def test_explain_is_the_install_time_verification(self):
        """`QUERY_SHAPE_SETUP.md`'s check is one command, and it must not need a live hook event."""
        out = io.StringIO()
        self.assertEqual(qs.main(argv=["--explain", THE_INSTANCE], stdout=out), 0)
        res = json.loads(out.getvalue())
        self.assertTrue(res["fires"])
        self.assertIn("not evidence of absence", res["line"])

        out = io.StringIO()
        qs.main(argv=["--explain", 'SELECT * FROM "c://x" ORDER BY A LIMIT 1'], stdout=out)
        self.assertEqual(json.loads(out.getvalue()),
                         {"fires": False, "line": None, "notion_backend": True})

        out = io.StringIO()
        qs.main(argv=["--explain"], stdout=out)
        self.assertFalse(json.loads(out.getvalue())["fires"])

    def test_decide_never_raises(self):
        for bad in (None, "x", 7, [], {"tool_name": object()}):
            self.assertIsNone(qs.decide(bad, state_dir=self.dir))


class NotionOnlyGateTest(unittest.TestCase):
    """On any backend but Notion the hook is a strict no-op: exit 0, nothing printed, nothing logged.
    The Obsidian and Markdown backends have no SQL query tool, so a fire there could only be noise."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg_dir = tempfile.mkdtemp()
        self._saved = (qs.STORE_CONFIG, qs.LEGACY_NOTION_MCP)
        self._prev = os.environ.get(qs.STATE_DIR_ENV_VAR)
        os.environ[qs.STATE_DIR_ENV_VAR] = self.dir

    def tearDown(self):
        qs.STORE_CONFIG, qs.LEGACY_NOTION_MCP = self._saved
        if self._prev is None:
            os.environ.pop(qs.STATE_DIR_ENV_VAR, None)
        else:
            os.environ[qs.STATE_DIR_ENV_VAR] = self._prev

    def _log_exists(self):
        return os.path.exists(qs.log_path(self.dir))

    def _run(self):
        out = io.StringIO()
        code = qs.main(argv=[], stdin=io.StringIO(json.dumps(_event())), stdout=out)
        return code, out.getvalue()

    def test_a_filesystem_backend_is_a_silent_no_op(self):
        for backend in ("markdown", "obsidian"):
            with self.subTest(backend=backend):
                qs.STORE_CONFIG = _write_store_config(self.cfg_dir, backend)
                self.assertFalse(qs.notion_backend_active())
                self.assertEqual(self._run(), (0, ""))
                self.assertIsNone(qs.decide(_event(), state_dir=self.dir))
                self.assertFalse(self._log_exists(), "nothing may be written off-Notion")

    def test_no_store_configured_is_a_silent_no_op(self):
        qs.STORE_CONFIG = os.path.join(self.cfg_dir, "absent.json")
        qs.LEGACY_NOTION_MCP = os.path.join(self.cfg_dir, "absent-mcp.json")
        self.assertIsNone(qs.store_backend())
        self.assertEqual(self._run(), (0, ""))
        self.assertFalse(self._log_exists())

    def test_an_unparseable_config_never_raises_and_does_not_fire(self):
        bad = os.path.join(self.cfg_dir, "config.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        qs.STORE_CONFIG = bad
        qs.LEGACY_NOTION_MCP = os.path.join(self.cfg_dir, "absent-mcp.json")
        self.assertIsNone(qs.store_backend())
        self.assertEqual(self._run(), (0, ""))

    def test_a_legacy_notion_mcp_file_counts_as_notion(self):
        qs.STORE_CONFIG = os.path.join(self.cfg_dir, "absent.json")
        legacy = os.path.join(self.cfg_dir, "notion-mcp.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("{}")
        qs.LEGACY_NOTION_MCP = legacy
        self.assertEqual(qs.store_backend(), "notion")
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("not evidence of absence", out)

    def test_the_notion_backend_fires(self):
        qs.STORE_CONFIG = _write_store_config(self.cfg_dir, "notion")
        self.assertTrue(qs.notion_backend_active())
        self.assertIn("not evidence of absence", self._run()[1])


class TheSentence(unittest.TestCase):

    def test_it_states_a_fact_and_gives_no_instruction(self):
        """Nothing here adds a sentence telling the assistant to be careful. The line says what the
        RESULT is; it does not say what to do about it, and a later edit that turns it into advice
        is one more written rule wearing a hook's clothes."""
        line = qs.LIMIT_WITHOUT_ORDER_NOTE.lower()
        self.assertIn("absence from it is not evidence of absence", line)
        for imperative in ("you should", "be careful", "make sure", "always ", "remember to",
                           "don't forget"):
            self.assertNotIn(imperative, line)

    def test_it_is_one_line(self):
        self.assertNotIn("\n", qs.LIMIT_WITHOUT_ORDER_NOTE)


if __name__ == "__main__":
    unittest.main()
