#!/usr/bin/env python3
"""Tests for ``bash_path_guard`` — the `PreToolUse` hook refusing a double-quoted Windows path
that ends in a backslash inside a Bash command.

Three things here are not ordinary unit tests and should not be relaxed:

* **``OneDefinitionTest``** pins that the rule is a single expression in this directory. A second
  copy of it elsewhere drifts silently: both keep running, and only one gets fixed.
* **``FailOpenTest``** is the whole safety story. This hook fires in *every* Claude Code session on
  the machine, including work with nothing to do with this repo, so every unknown must allow.
  A test here going green on "blocks" is a defect, not a stricter guard.
* **``KnownFalsePositiveTest``** pins commands this hook refuses that are *correct Bash*. They are
  accepted costs, rare next to the real catches. They are recorded as tests so that a future run
  knows they were known rather than missed — and so that narrowing the expression to chase them is
  a deliberate, visible edit rather than a quiet one.

Run:  python -m unittest test_bash_path_guard   (from seneschal/scripts)
"""
import ast
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bash_path_guard as guard


def event(tool_name, command, **extra):
    """A `PreToolUse` payload of the real shape: `tool_name` plus a `tool_input` object."""
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "00000000-0000-0000-0000-000000000001",
        "cwd": "C:/path/to/repo",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    }
    payload.update(extra)
    return payload


def run_hook(raw):
    """Drive the hook entrypoint over `raw` stdin -> (exit_code, stderr_text)."""
    err = io.StringIO()
    code = guard.main(stdin=io.StringIO(raw), stderr=err)
    return code, err.getvalue()


# The canonical failure: the most common parser-failure shape on Windows.
TRAILING = r'ls -la "C:\Users\me\workspace\repos\seneschal\seneschal\state\"'


# --------------------------------------------------------------------------- the rule itself

class BlocksTheRealBugTest(unittest.TestCase):
    def test_trailing_backslash_in_quoted_windows_path_blocks(self):
        code, err = run_hook(json.dumps(event("Bash", TRAILING)))
        self.assertEqual(code, 2)
        self.assertIn("trailing", err.lower())

    def test_doubled_backslash_form_also_blocks(self):
        # The same path written through a layer of escaping — still a quoted run that looks like a
        # Windows path and still ends in a backslash before the closing quote.
        cmd = r'python -c "import os; print(os.listdir(\"C:\\Users\\me\\state\\"))"'
        self.assertTrue(guard.should_block("Bash", cmd))

    def test_pipeline_reports_the_break_somewhere_else_but_still_matches(self):
        # The trailing `\` escaped the quote, and the diagnostic surfaced at the *grep*. The command shape is what this rule reads, never the diagnostic.
        cmd = r'ls -la "C:\Users\me\state\" | grep -E "(a|b)"'
        self.assertTrue(guard.should_block("Bash", cmd))

    def test_exit_two_is_the_block_and_stdout_stays_empty(self):
        # Exit 2 + stderr is the chosen mechanism; nothing is ever written to stdout, whose plain
        # text would only reach a debug log anyway.
        code, err = run_hook(json.dumps(event("Bash", TRAILING)))
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())


class RejectionMessageTest(unittest.TestCase):
    """The message has to be actionable on first read — that is the entire point of the rule:
    it turns a confusing parser error into an immediate, obvious rewrite."""

    def test_names_the_mechanism(self):
        text = guard.REJECTION_REASON
        self.assertIn("escapes the closing", text)
        self.assertIn("never terminates", text)

    def test_carries_both_remedies(self):
        text = guard.REJECTION_REASON
        self.assertIn("forward slashes", text)
        self.assertIn("drop the trailing backslash", text)

    def test_names_the_known_false_positive_and_its_fix(self):
        self.assertIn("false positive", guard.REJECTION_REASON)
        self.assertIn("script file", guard.REJECTION_REASON)

    def test_the_message_does_not_match_its_own_rule(self):
        # Otherwise every command that writes or greps this message is itself refused. The example
        # paths in it are chosen so that they cannot: one uses forward slashes, the other has no
        # trailing backslash.
        self.assertFalse(guard.should_block("Bash", guard.REJECTION_REASON))


# --------------------------------------------------------------------------- tool scoping

class ToolScopingTest(unittest.TestCase):
    """Scoping to Bash is a correctness constraint, not a tuning choice. In PowerShell the
    backslash is not an escape character, so the same string closes its quote and runs; every
    PowerShell match is a false positive **by construction**, so widening the hook to PowerShell
    would add nothing but false positives."""

    def test_powershell_does_not_block(self):
        code, err = run_hook(json.dumps(event("PowerShell", TRAILING)))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_powershell_predicate_is_false_for_the_identical_command(self):
        self.assertTrue(guard.should_block("Bash", TRAILING))
        self.assertFalse(guard.should_block("PowerShell", TRAILING))

    def test_an_unrelated_tool_does_not_block(self):
        for tool in ("Edit", "Write", "Read", "Glob", "Grep", "mcp__notion__notion-fetch"):
            with self.subTest(tool=tool):
                code, _err = run_hook(json.dumps(event(tool, TRAILING)))
                self.assertEqual(code, 0)

    def test_guarded_tool_is_exactly_one_tool(self):
        self.assertEqual(guard.GUARDED_TOOL, "Bash")


# --------------------------------------------------------------------------- what it must NOT block

class LeavesCorrectShellAloneTest(unittest.TestCase):
    def test_escaped_quotes_with_no_windows_path_are_untouched(self):
        # The first narrowing. A naive `any \"` rule blocks this — correct shell — and that class
        # dominates its false positives.
        self.assertFalse(guard.should_block("Bash", r'echo "they said \"hi\""'))

    def test_windows_path_without_a_trailing_backslash_is_untouched(self):
        self.assertFalse(
            guard.should_block("Bash", r'ls -la "C:\Users\me\workspace\repos\seneschal"'))

    def test_forward_slash_windows_path_is_untouched(self):
        self.assertFalse(guard.should_block("Bash", 'ls -la "C:/Users/me/state/"'))

    def test_posix_path_ending_in_a_slash_is_untouched(self):
        self.assertFalse(guard.should_block("Bash", 'ls -la "/mnt/c/Users/me/state/"'))

    def test_ordinary_commands_are_untouched(self):
        for cmd in ("git status", "npm ci", "python -m unittest discover -s seneschal/scripts",
                    'grep -rn "def main" seneschal/scripts', "for d in *; do echo $d; done"):
            with self.subTest(cmd=cmd):
                self.assertFalse(guard.should_block("Bash", cmd))

    def test_empty_command_is_untouched(self):
        self.assertFalse(guard.should_block("Bash", ""))


class KnownFalsePositiveTest(unittest.TestCase):
    """**Two known false-positive shapes, pinned as they actually are.**

    Both are *correct Bash* that this hook refuses. They are recorded here as known and accepted —
    rare next to the real catches — not as bugs to fix by quietly narrowing the expression.

    Both come from the same mechanism. The regex has no notion of escaping at its left edge, so an
    *escaped* quote can serve as its opening quote, and a doubled backslash can be an escape rather
    than a path separator. Neither command contains a Windows path at all, and neither had an
    unterminated quote."""

    # FALSE POSITIVE 1 — a quote escaped across a nested-shell boundary.
    # `\\\"` is "an escaped backslash, then an escaped quote", which the doubled-backslash arm of
    # the regex reads as a path separator. The command runs correctly and as intended.
    FP_NESTED_SHELL_QUOTE = (
        r'''powershell -NoProfile -Command "Get-CimInstance Win32_Process '''
        r'''-Filter \\\"Name='python.exe'\\\""'''
    )

    # FALSE POSITIVE 2 — a `\\n` escape inside a python -c string. Bash collapses
    # `\\` to one backslash and `\"` to a quote, so Python receives `print(f"\n=== ... ===")`.
    # Again: no Windows path, no unterminated quote, no failure.
    FP_ESCAPED_NEWLINE = r'''python3 -c "print(f\"\\n=== Daemon health ===\")"'''

    def test_nested_shell_escaped_quote_is_blocked_and_that_is_known(self):
        self.assertTrue(guard.should_block("Bash", self.FP_NESTED_SHELL_QUOTE))

    def test_escaped_newline_in_a_python_c_string_is_blocked_and_that_is_known(self):
        self.assertTrue(guard.should_block("Bash", self.FP_ESCAPED_NEWLINE))

    def test_neither_false_positive_contains_a_windows_path(self):
        # The honest characterisation: these are escaping collisions, not paths whose quote
        # happened to close later.
        for cmd in (self.FP_NESTED_SHELL_QUOTE, self.FP_ESCAPED_NEWLINE):
            with self.subTest(cmd=cmd[:40]):
                self.assertNotIn("C:", cmd)

    def test_the_rejection_message_speaks_to_them(self):
        # A false positive that arrives with no way out burns the hook's credibility. The message names this shape and gives the fix that is right for it anyway.
        self.assertIn(r"\\n", guard.REJECTION_REASON)
        self.assertIn("nested shell", guard.REJECTION_REASON)


# --------------------------------------------------------------------------- fail open

class FailOpenTest(unittest.TestCase):
    """There is no failure mode of this hook whose answer is "block". Unparseable stdin, a missing
    key, an unexpected schema, a raise anywhere — all exit 0, silent."""

    def test_empty_stdin_allows(self):
        self.assertEqual(run_hook(""), (0, ""))

    def test_whitespace_only_stdin_allows(self):
        self.assertEqual(run_hook("   \n\t "), (0, ""))

    def test_malformed_json_allows(self):
        for raw in ("{not json", "[", '{"tool_name": ', "\x00\x01\x02", "null", "true", "42"):
            with self.subTest(raw=raw[:12]):
                self.assertEqual(run_hook(raw), (0, ""))

    def test_json_that_is_not_an_object_allows(self):
        self.assertEqual(run_hook(json.dumps([{"tool_name": "Bash"}])), (0, ""))

    def test_missing_tool_input_allows(self):
        self.assertEqual(run_hook(json.dumps({"tool_name": "Bash"})), (0, ""))

    def test_missing_tool_name_allows(self):
        self.assertEqual(run_hook(json.dumps({"tool_input": {"command": TRAILING}})), (0, ""))

    def test_unexpected_schema_shapes_allow(self):
        for payload in (
            {"tool_name": "Bash", "tool_input": "a string, not an object"},
            {"tool_name": "Bash", "tool_input": {"command": None}},
            {"tool_name": "Bash", "tool_input": {"command": 42}},
            {"tool_name": "Bash", "tool_input": {"command": [TRAILING]}},
            {"tool_name": ["Bash"], "tool_input": {"command": TRAILING}},
            {"tool_name": "Bash", "tool_input": {}},
            {},
        ):
            with self.subTest(payload=str(payload)[:50]):
                self.assertEqual(run_hook(json.dumps(payload)), (0, ""))

    def test_a_raise_inside_the_predicate_allows(self):
        class Exploding:
            def search(self, _text):
                raise RuntimeError("predicate is broken")

        original = guard.WINDOWS_PATH_TRAILING_BACKSLASH_RE
        guard.WINDOWS_PATH_TRAILING_BACKSLASH_RE = Exploding()
        try:
            self.assertRaises(RuntimeError, guard.should_block, "Bash", TRAILING)
            self.assertEqual(run_hook(json.dumps(event("Bash", TRAILING))), (0, ""))
        finally:
            guard.WINDOWS_PATH_TRAILING_BACKSLASH_RE = original
        # ...and the guard still works once the predicate does.
        self.assertEqual(run_hook(json.dumps(event("Bash", TRAILING)))[0], 2)

    def test_a_stdin_that_raises_on_read_allows(self):
        class Exploding:
            def read(self):
                raise OSError("stdin is gone")

        self.assertEqual(guard.main(stdin=Exploding(), stderr=io.StringIO()), 0)

    def test_a_stderr_that_raises_still_blocks(self):
        # The one asymmetry: we cannot explain the block, but the block itself was a positive
        # identification of the bug and stands.
        class Exploding:
            def write(self, _text):
                raise OSError("stderr is gone")

        self.assertEqual(
            guard.main(stdin=io.StringIO(json.dumps(event("Bash", TRAILING))), stderr=Exploding()),
            2,
        )

    def test_decide_never_raises_on_a_hostile_shape(self):
        for payload in (None, [], "", 0, {"tool_input": None}, {"tool_input": []}):
            with self.subTest(payload=str(payload)):
                self.assertFalse(guard.decide(payload))


# --------------------------------------------------------------------------- the shared definition

class OneDefinitionTest(unittest.TestCase):
    """The rule must be ONE expression. If a second copy appears, both keep running and only one
    gets fixed the next time the rule changes."""

    def test_the_regex_is_defined_exactly_once_in_this_directory(self):
        # A second `re.compile` of the same source anywhere here is the drift this class exists to
        # prevent, so it is checked against the files rather than against imports.
        here = os.path.dirname(os.path.abspath(__file__))
        source = guard.WINDOWS_PATH_TRAILING_BACKSLASH_RE.pattern
        definers = []
        for name in sorted(os.listdir(here)):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            with open(os.path.join(here, name), "r", encoding="utf-8") as fh:
                if source in fh.read():
                    definers.append(name)
        self.assertEqual(definers, ["bash_path_guard.py"])


class ImportWeightTest(unittest.TestCase):
    """This hook runs on every Bash call in every session on the machine, under a bare `python` with
    no venv. It must import nothing but the stdlib and must do no work at import."""

    def _module_source(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bash_path_guard.py")
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
        self.assertEqual(imported - {"__future__"}, {"json", "re", "sys"})

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
