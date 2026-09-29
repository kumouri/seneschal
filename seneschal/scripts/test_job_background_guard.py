#!/usr/bin/env python3
"""Tests for `job_background_guard` — the `PreToolUse` hook of `../docs/background-jobs-spec.md`
§3.14, refusing `run_in_background` Bash calls inside a `jobs.py`-launched process.

Run:  python -m unittest discover -s seneschal/scripts -p "test_job_background_guard.py"
"""
import ast
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import job_background_guard as guard

JOB_ENV = {guard.JOB_ENV_VAR: "20260906-211234-268c"}


def event(tool_name, **tool_input):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "00000000-0000-0000-0000-000000000000",
        "cwd": "C:/repos/seneschal-worktrees/some-job",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


def run_hook(raw, env=None):
    """Drive the hook entrypoint over `raw` stdin (+ `env`) -> (exit_code, stderr_text)."""
    err = io.StringIO()
    code = guard.main(stdin=io.StringIO(raw), stderr=err, env=env if env is not None else {})
    return code, err.getvalue()


# --------------------------------------------------------------------------- the rule itself

class BlocksInsideAJobTest(unittest.TestCase):
    def test_run_in_background_true_inside_a_job_blocks(self):
        code, err = run_hook(json.dumps(event("Bash", command="python seneschal/scripts/ci_local.py",
                                               run_in_background=True)), env=JOB_ENV)
        self.assertEqual(code, 2)
        self.assertIn("background", err.lower())

    def test_exit_two_is_the_block_and_stdout_stays_empty(self):
        code, err = run_hook(json.dumps(event("Bash", run_in_background=True)), env=JOB_ENV)
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())

    def test_predicate_matches_the_hook_behaviour(self):
        self.assertTrue(guard.should_block("Bash", {"run_in_background": True}, JOB_ENV))


class RejectionMessageTest(unittest.TestCase):
    def test_names_the_mechanism(self):
        text = guard.REJECTION_REASON
        self.assertIn("single-prompt", text)
        self.assertIn("no next turn", text)

    def test_names_the_remedy(self):
        self.assertIn("FOREGROUND", guard.REJECTION_REASON)
        self.assertIn("run_in_background", guard.REJECTION_REASON)


# --------------------------------------------------------------------------- scoping: NOT a job

class OutsideAJobIsUntouchedTest(unittest.TestCase):
    """The whole point of keying on `SENESCHAL_JOB_ID` rather than the tool call alone: an ordinary
    interactive session has a next turn to receive a background result on, so refusing it there
    would just be blocking working commands for no reason."""

    def test_no_env_var_at_all_does_not_block(self):
        code, err = run_hook(json.dumps(event("Bash", run_in_background=True)), env={})
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_empty_job_id_does_not_block(self):
        code, _err = run_hook(json.dumps(event("Bash", run_in_background=True)),
                               env={guard.JOB_ENV_VAR: ""})
        self.assertEqual(code, 0)

    def test_predicate_is_false_with_no_job_env(self):
        self.assertFalse(guard.should_block("Bash", {"run_in_background": True}, {}))
        self.assertTrue(guard.should_block("Bash", {"run_in_background": True}, JOB_ENV))

    def test_a_non_mapping_env_is_no_evidence_of_a_job(self):
        self.assertFalse(guard.should_block("Bash", {"run_in_background": True}, None))


# --------------------------------------------------------------------------- scoping: tool + flag

class ToolAndFlagScopingTest(unittest.TestCase):
    def test_run_in_background_false_or_absent_does_not_block(self):
        for tool_input in ({"run_in_background": False}, {}, {"command": "git status"}):
            with self.subTest(tool_input=tool_input):
                code, _err = run_hook(json.dumps(event("Bash", **tool_input)), env=JOB_ENV)
                self.assertEqual(code, 0)

    def test_an_unrelated_tool_does_not_block_even_with_background_flag(self):
        for tool in ("Edit", "Write", "Read", "Glob", "Grep", "Agent", "mcp__notion__notion-fetch"):
            with self.subTest(tool=tool):
                code, _err = run_hook(json.dumps(event(tool, run_in_background=True)), env=JOB_ENV)
                self.assertEqual(code, 0)

    def test_guarded_tool_is_exactly_one_tool(self):
        self.assertEqual(guard.GUARDED_TOOL, "Bash")

    def test_ordinary_foreground_bash_inside_a_job_is_untouched(self):
        code, _err = run_hook(
            json.dumps(event("Bash", command="python seneschal/scripts/ci_local.py")), env=JOB_ENV)
        self.assertEqual(code, 0)


# --------------------------------------------------------------------------- fail open

class FailOpenTest(unittest.TestCase):
    """There is no failure mode of this hook whose answer is "block". Unparseable stdin, a missing
    key, an unexpected schema, a raise anywhere — all exit 0, silent."""

    def test_empty_stdin_allows(self):
        self.assertEqual(run_hook("", env=JOB_ENV), (0, ""))

    def test_whitespace_only_stdin_allows(self):
        self.assertEqual(run_hook("   \n\t ", env=JOB_ENV), (0, ""))

    def test_malformed_json_allows(self):
        for raw in ("{not json", "[", '{"tool_name": ', "\x00\x01\x02", "null", "true", "42"):
            with self.subTest(raw=raw[:12]):
                self.assertEqual(run_hook(raw, env=JOB_ENV), (0, ""))

    def test_json_that_is_not_an_object_allows(self):
        self.assertEqual(run_hook(json.dumps([{"tool_name": "Bash"}]), env=JOB_ENV), (0, ""))

    def test_missing_tool_input_allows(self):
        self.assertEqual(run_hook(json.dumps({"tool_name": "Bash"}), env=JOB_ENV), (0, ""))

    def test_missing_tool_name_allows(self):
        self.assertEqual(
            run_hook(json.dumps({"tool_input": {"run_in_background": True}}), env=JOB_ENV), (0, ""))

    def test_unexpected_schema_shapes_allow(self):
        for payload in (
            {"tool_name": "Bash", "tool_input": "a string, not an object"},
            {"tool_name": "Bash", "tool_input": {"run_in_background": None}},
            {"tool_name": "Bash", "tool_input": None},
            {"tool_name": ["Bash"], "tool_input": {"run_in_background": True}},
            {},
        ):
            with self.subTest(payload=str(payload)[:50]):
                self.assertEqual(run_hook(json.dumps(payload), env=JOB_ENV), (0, ""))

    def test_a_raise_inside_the_predicate_allows(self):
        def boom(_tool_name, _tool_input, _env):
            raise RuntimeError("predicate is broken")

        original = guard.should_block
        guard.should_block = boom
        try:
            self.assertEqual(
                run_hook(json.dumps(event("Bash", run_in_background=True)), env=JOB_ENV), (0, ""))
        finally:
            guard.should_block = original
        # ...and the guard still works once the predicate does.
        self.assertEqual(
            run_hook(json.dumps(event("Bash", run_in_background=True)), env=JOB_ENV)[0], 2)

    def test_a_stdin_that_raises_on_read_allows(self):
        class Exploding:
            def read(self):
                raise OSError("stdin is gone")

        self.assertEqual(guard.main(stdin=Exploding(), stderr=io.StringIO(), env=JOB_ENV), 0)

    def test_a_stderr_that_raises_still_blocks(self):
        class Exploding:
            def write(self, _text):
                raise OSError("stderr is gone")

        self.assertEqual(
            guard.main(stdin=io.StringIO(json.dumps(event("Bash", run_in_background=True))),
                      stderr=Exploding(), env=JOB_ENV),
            2,
        )

    def test_a_none_env_falls_back_to_os_environ(self):
        # main()'s own default, not the test helper's — exercised once so the fallback path itself
        # (`env if env is not None else os.environ`) is covered, not just its wrapper.
        old = os.environ.get(guard.JOB_ENV_VAR)
        os.environ[guard.JOB_ENV_VAR] = "job-under-test"
        try:
            err = io.StringIO()
            code = guard.main(stdin=io.StringIO(json.dumps(event("Bash", run_in_background=True))),
                               stderr=err)
            self.assertEqual(code, 2)
        finally:
            if old is None:
                os.environ.pop(guard.JOB_ENV_VAR, None)
            else:
                os.environ[guard.JOB_ENV_VAR] = old

    def test_decide_never_raises_on_a_hostile_shape(self):
        for payload in (None, [], "", 0, {"tool_input": None}, {"tool_input": []}):
            with self.subTest(payload=str(payload)):
                self.assertFalse(guard.decide(payload, JOB_ENV))


# --------------------------------------------------------------------------- import weight

class ImportWeightTest(unittest.TestCase):
    """This hook runs on every Bash call inside every job, under a bare `python` with no venv. It
    must import nothing but the stdlib and must do no work at import."""

    def _module_source(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_background_guard.py")
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_imports_only_stdlib(self):
        tree = ast.parse(self._module_source())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - {"__future__"}, {"json", "os", "sys"})

    def test_no_work_at_import_beyond_constants(self):
        tree = ast.parse(self._module_source())
        for node in tree.body:
            self.assertIsInstance(
                node,
                (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef, ast.ClassDef, ast.If,
                 ast.Expr),
                msg=f"unexpected module-level statement at line {node.lineno}",
            )
            if isinstance(node, ast.Expr):  # only the docstring
                self.assertIsInstance(node.value, ast.Constant)


if __name__ == "__main__":
    unittest.main()
