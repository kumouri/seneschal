#!/usr/bin/env python3
"""Tests for ``reminders_live.py`` — the live reminder row list behind ``ack.py``'s stage 2.

The load-bearing property is the same one as ``test_ack.py``'s: **this module must never be the reason
an ack lands on the wrong row.** It is a data pipe with a model at one end, so the coverage is on the
places a model's reply could quietly become a bad candidate list:

* **``NotionBackendOnly``** — on a filesystem backend (or no store at all) nothing is spawned and the
  answer is a refusal, not an empty list; the collection id is read from the rendered schema, never
  a placeholder.
* **``ActiveIsAnAllowList``** — ``finished`` and ``paused`` are out, and so is anything unrecognised.
* **``ParsingIsStrictAndTotal``** — a row missing a title, an id or a status is dropped, not repaired.
* **``TheReplyShapeIsRequired``** — prose, a stray object, or the right JSON under the wrong ``schema``
  is an ERROR rather than an empty row list.
* **``NothingSpendsAndNothingLeaks``** — every test injects a runner, so no test spawns ``claude``; the
  child's env carries no ``ANTHROPIC_API_KEY`` and the spawn is windowless.

Run:  python -m unittest seneschal.scripts.test_reminders_live   (or)  python test_reminders_live.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import reminders_live as rl  # noqa: E402

PID_A = "00000000-0000-0000-0000-00000000000a"
PID_B = "00000000-0000-0000-0000-00000000000b"
PID_C = "00000000-0000-0000-0000-00000000000c"
#: A non-placeholder-looking collection id for the rendered-schema fixture. Assembled at runtime so the
#: tracked source carries only placeholder-shaped UUIDs.
COLLECTION = "collection://" + "-".join(("1" * 8, "2" * 4, "3" * 4, "4" * 4, "5" * 12))

SCHEMA_MD = f"""# notion/schema.md (rendered)

### reminders — `{COLLECTION}`

| canonical | Notion | type |
"""


def reply(rows, schema=rl.ROWS_SCHEMA, prose=""):
    return prose + json.dumps({"schema": schema, "rows": rows})


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class Recorder:
    """A ``subprocess.run``-compatible stand-in. Records the call; spawns nothing, spends nothing."""

    def __init__(self, proc=None, raises=None):
        self.proc, self.raises, self.calls = proc or FakeProc(), raises, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises:
            raise self.raises
        return self.proc


class _Store(unittest.TestCase):
    """Points the module's store config + rendered schema at a temp dir. Default: a Notion install."""

    BACKEND = "notion"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = os.path.join(self.tmp, "config.json")
        self.schema = os.path.join(self.tmp, "schema.md")
        if self.BACKEND:
            with open(self.config, "w", encoding="utf-8") as fh:
                json.dump({"active": self.BACKEND, "backends": {}}, fh)
        with open(self.schema, "w", encoding="utf-8") as fh:
            fh.write(SCHEMA_MD)
        for name, value in (("STORE_CONFIG", self.config), ("NOTION_SCHEMA", self.schema),
                            ("LEGACY_NOTION_MCP", os.path.join(self.tmp, "no-legacy.json"))):
            p = mock.patch.object(rl, name, value)
            p.start()
            self.addCleanup(p.stop)


class NotionBackendOnly(_Store):
    def test_a_filesystem_backend_spawns_nothing_and_refuses(self):
        for backend in ("markdown", "obsidian"):
            with self.subTest(backend=backend):
                with open(self.config, "w", encoding="utf-8") as fh:
                    json.dump({"active": backend}, fh)
                rec = Recorder(FakeProc(reply([])))
                rows, err = rl.fetch_active_rows(runner=rec)
                self.assertIsNone(rows)
                self.assertIn("Notion-backend only", err)
                self.assertIn(backend, err)
                self.assertEqual(rec.calls, [])

    def test_no_store_at_all_refuses(self):
        os.remove(self.config)
        rows, err = rl.fetch_active_rows(runner=Recorder(FakeProc(reply([]))))
        self.assertIsNone(rows)
        self.assertIn("unconfigured", err)

    def test_a_legacy_notion_mcp_with_no_config_is_a_notion_install(self):
        os.remove(self.config)
        legacy = os.path.join(self.tmp, "notion-mcp.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("{}")
        with mock.patch.object(rl, "LEGACY_NOTION_MCP", legacy):
            self.assertEqual(rl.store_backend(), "notion")
            self.assertEqual(rl.default_mcp_configs(), [legacy])

    def test_a_missing_rendered_schema_refuses_rather_than_querying_a_guess(self):
        os.remove(self.schema)
        rec = Recorder(FakeProc(reply([])))
        rows, err = rl.fetch_active_rows(runner=rec)
        self.assertIsNone(rows)
        self.assertIn("setup-store", err)
        self.assertEqual(rec.calls, [])

    def test_a_placeholder_collection_id_counts_as_not_set_up(self):
        with open(self.schema, "w", encoding="utf-8") as fh:
            fh.write("### reminders — `collection://00000000-0000-0000-0000-000000000007`\n")
        self.assertIsNone(rl.reminders_collection())

    def test_the_rendered_collection_id_reaches_the_prompt(self):
        rec = Recorder(FakeProc(reply([])))
        rl.fetch_active_rows(runner=rec)
        self.assertIn(COLLECTION, rec.calls[0][0][2])

    def test_the_store_configs_notion_mcp_is_the_default(self):
        mcp = os.path.join(self.tmp, "mcp.json")
        with open(mcp, "w", encoding="utf-8") as fh:
            fh.write("{}")
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump({"active": "notion", "backends": {"notion": {"mcp_config": mcp}}}, fh)
        self.assertEqual(rl.default_mcp_configs(), [os.path.normpath(mcp)])


class ActiveIsAnAllowList(unittest.TestCase):
    """An unrecognised status must read as NOT active — the allow-list direction is the whole point."""

    def rows(self, *statuses):
        return [{"title": f"Row {i}", "key": f"{i:032x}", "status": s}
                for i, s in enumerate(statuses)]

    def test_finished_and_paused_are_excluded(self):
        got = rl.active_rows(self.rows("Pending", "Finished", "Paused", "Done"))
        self.assertEqual([r["status"] for r in got], ["Pending", "Done"])

    def test_an_unknown_status_is_excluded_rather_than_assumed_active(self):
        """A `!= Finished` test would keep this row and let a retired todo be acked again."""
        self.assertEqual(rl.active_rows(self.rows("Archived", "Retired", "???")), [])

    def test_every_documented_live_status_survives(self):
        live = ("Pending", "Reminded", "Done", "Skipped", "Snoozed")
        self.assertEqual(len(rl.active_rows(self.rows(*live))), len(live))

    def test_the_comparison_is_case_insensitive(self):
        self.assertEqual(len(rl.active_rows(self.rows("PENDING", "finished"))), 1)


class ParsingIsStrictAndTotal(unittest.TestCase):
    def test_a_good_row_normalizes_its_page_id(self):
        got = rl.parse_rows({"rows": [{"title": " Submit timesheet ", "page_id": PID_A,
                                       "status": " Pending "}]})
        self.assertEqual(got, [{"title": "Submit timesheet",
                                "key": PID_A.replace("-", ""), "status": "Pending"}])

    def test_rows_missing_a_field_are_dropped_not_repaired(self):
        got = rl.parse_rows({"rows": [
            {"title": "", "page_id": PID_A, "status": "Pending"},
            {"title": "No id", "status": "Pending"},
            {"title": "No status", "page_id": PID_B},
            {"title": "Not an id", "page_id": "later", "status": "Pending"},
            "not even a dict",
            {"title": "Good", "page_id": PID_C, "status": "Pending"},
        ]})
        self.assertEqual([r["title"] for r in got], ["Good"])

    def test_a_repeated_page_id_is_kept_once(self):
        got = rl.parse_rows({"rows": [{"title": "A", "page_id": PID_A, "status": "Pending"},
                                      {"title": "A again", "page_id": PID_A, "status": "Pending"}]})
        self.assertEqual([r["title"] for r in got], ["A"])

    def test_a_malformed_object_parses_to_nothing_rather_than_raising(self):
        for obj in (None, {}, {"rows": "nope"}, {"rows": None}, []):
            self.assertEqual(rl.parse_rows(obj), [])


class TheReplyShapeIsRequired(_Store):
    def fetch(self, **kw):
        rec = Recorder(**kw)
        return rl.fetch_active_rows(runner=rec), rec

    def test_a_valid_reply_yields_the_active_rows(self):
        (rows, err), _ = self.fetch(proc=FakeProc(reply([
            {"title": "Book the dentist", "page_id": PID_A, "status": "Pending"},
            {"title": "Old thing", "page_id": PID_B, "status": "Finished"}])))
        self.assertIsNone(err)
        self.assertEqual([r["title"] for r in rows], ["Book the dentist"])

    def test_a_fence_and_a_sentence_around_the_json_are_tolerated(self):
        (rows, err), _ = self.fetch(proc=FakeProc(
            "Here you go:\n```json\n"
            + reply([{"title": "T", "page_id": PID_A, "status": "Pending"}]) + "\n```"))
        self.assertIsNone(err)
        self.assertEqual(len(rows), 1)

    def test_prose_with_no_json_is_an_error_not_an_empty_list(self):
        """'no rows' and 'no lookup' must stay distinguishable — one is an answer, one is a failure."""
        (rows, err), _ = self.fetch(proc=FakeProc("I couldn't reach Notion.", returncode=1))
        self.assertIsNone(rows)
        self.assertIn("no JSON object", err)

    def test_the_wrong_schema_is_refused_even_when_the_rows_look_fine(self):
        (rows, err), _ = self.fetch(proc=FakeProc(reply(
            [{"title": "T", "page_id": PID_A, "status": "Pending"}], schema="something-else")))
        self.assertIsNone(rows)
        self.assertIn("schema", err)

    def test_an_empty_row_list_is_an_ANSWER_not_an_error(self):
        (rows, err), _ = self.fetch(proc=FakeProc(reply([])))
        self.assertIsNone(err)
        self.assertEqual(rows, [])

    def test_a_runner_that_cannot_start_the_cli_is_an_error(self):
        (rows, err), _ = self.fetch(raises=OSError("no such file"))
        self.assertIsNone(rows)
        self.assertIn("could not run", err)

    def test_a_timeout_is_an_error_rather_than_a_raise(self):
        (rows, err), _ = self.fetch(raises=subprocess.TimeoutExpired("claude", 1))
        self.assertIsNone(rows)
        self.assertIn("could not run", err)


class NothingSpendsAndNothingLeaks(_Store):
    def test_the_one_shot_is_a_plain_claude_p(self):
        argv = rl.fetch_argv("claude", mcp_configs=[], collection=COLLECTION)
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--permission-mode", argv)
        self.assertNotIn("--mcp-config", argv)

    def test_the_mcp_configs_and_model_are_threaded_in_when_given(self):
        argv = rl.fetch_argv("claude", mcp_configs=["a.json", "b.json"], model="cheap")
        self.assertEqual(argv[argv.index("--mcp-config") + 1:argv.index("--mcp-config") + 3],
                         ["a.json", "b.json"])
        self.assertEqual(argv[argv.index("--model") + 1], "cheap")

    def test_the_prompt_names_the_collection_and_the_required_schema(self):
        prompt = rl.fetch_prompt(COLLECTION)
        self.assertIn(COLLECTION, prompt)
        self.assertIn(rl.ROWS_SCHEMA, prompt)

    def test_the_child_never_inherits_an_api_key(self):
        """`claude -p` is subscription-billed; a stray key would silently change the billing model."""
        rec = Recorder(FakeProc(reply([])))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-should-not-reach-the-child"}):
            rl.fetch_active_rows(runner=rec)
        self.assertNotIn("ANTHROPIC_API_KEY", rec.calls[0][1]["env"])

    def test_the_spawn_is_windowless(self):
        """A console-less daemon's child must not pop a console window on Windows."""
        rec = Recorder(FakeProc(reply([])))
        rl.fetch_active_rows(runner=rec)
        self.assertIn("creationflags", rec.calls[0][1])
        self.assertEqual(rec.calls[0][1]["creationflags"],
                         getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_the_child_runs_from_the_repo_root(self):
        rec = Recorder(FakeProc(reply([])))
        rl.fetch_active_rows(runner=rec)
        self.assertEqual(rec.calls[0][1]["cwd"], rl.REPO_ROOT)

    def test_the_configured_binary_is_the_one_invoked(self):
        rec = Recorder(FakeProc(reply([])))
        rl.fetch_active_rows(runner=rec, claude_bin="not-really-claude")
        self.assertEqual(rec.calls[0][0][0], "not-really-claude")


class TheCli(_Store):
    def run_cli(self, *argv, runner=None):
        buf = StringIO()
        with redirect_stdout(buf):
            rc = rl.main(list(argv), runner=runner)
        return rc, json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_a_good_fetch_exits_zero_and_prints_the_rows(self):
        rec = Recorder(FakeProc(reply([{"title": "T", "page_id": PID_A, "status": "Pending"}])))
        rc, out = self.run_cli("--json", runner=rec)
        self.assertEqual(rc, 0)
        self.assertEqual(out["count"], 1)

    def test_a_failed_fetch_exits_three_and_names_the_reason(self):
        rc, out = self.run_cli("--json", runner=Recorder(raises=OSError("nope")))
        self.assertEqual(rc, 3)
        self.assertFalse(out["ok"])
        self.assertIn("could not run", out["error"])


if __name__ == "__main__":
    unittest.main()
