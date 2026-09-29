#!/usr/bin/env python3
r"""Tests for ``script_file_guard`` — the `PreToolUse` hook that refuses the three shell one-liners
that are mechanically broken, and names the script file that fixes each.

Six classes here are not ordinary unit tests and must not be relaxed into passing:

* **``TheEscapeRouteTest``** is the one that makes the feature legitimate rather than a wall. If
  heredocs are refused then ``cat > script.sh <<'EOF'`` is refused, so there has to be a stated
  legal way to *create* the file — the Write tool, then a short command that runs it. This class
  drives that exact pair through the guard in both tools and asserts it is allowed, and
  ``RejectionNamesTheEscapeTest`` asserts every refusal message says so **in those words**. A guard
  that blocks the only route to compliance protects nothing, because the first thing anyone does is
  turn it off.
* **``TheCasesThatBitTest``** replays the real failure shapes each rule exists for. Its control
  cases — ``git status``, a **Bash** heredoc, a
  same-dialect nesting — are in the same class deliberately: the two assertions are read together or
  not at all, because a guard that refuses both is uninstalled by the end of the week.
* **``FailOpenTest``** asserts ``EXIT_ALLOW`` on every hostile input. `merge_guard`'s
  ``FailClosedTest`` asserts the **opposite** exit code on the same class of input, and that
  disagreement is deliberate: merge_guard guards an irreversible act behind a network call, this one
  is pure string work that fires on every shell call in every session on the machine. **If the two
  ever agree, one of them is wrong.**
* **``SelfImmunityTest``** — this file, the guard's docstring and its refusal text all contain the
  command spellings the guard refuses. A guard that blocks the commands used to write about it is a
  guard somebody turns off. Same shape as `bash_path_guard`'s
  ``test_the_message_does_not_match_its_own_rule`` and `branch_delete_guard`'s ``SelfImmunityTest``.
* **``VocabularyCoverageTest``** pins that :data:`script_file_guard.SHELL_DIALECT` and
  :data:`~script_file_guard.NESTED_FLAGS` stay **supersets** of `merge_guard`'s. The sets are
  deliberately not identical — this module needs ``-lc``, which no ``gh`` or ``git`` invocation uses
  — so they cannot simply be shared; what must never happen is a shell `merge_guard` follows that
  this one has no dialect for, which would be a silent hole rather than a difference of opinion.
* **``ThresholdBandTest``** pins :data:`~script_file_guard.MAX_COMMAND_CHARS` below the transport
  failure band and above the ground where only working commands live. It exists because the
  threshold is the one constant here that looks arbitrary and is not: raising it into the band gives
  up real catches, and lowering it far below starts refusing commands that were never at risk.

**No test may touch the network, the disk, or a subprocess for its verdict.** The guard has no
seam because it needs none — it reads no file and spawns nothing — and ``NoIOTest`` asserts that
against the source.

Run:  python -m unittest test_script_file_guard   (from seneschal/scripts)
"""
import ast
import io
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import merge_guard  # noqa: E402
import script_file_guard as sfg  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "script_file_guard.py")

#: A Bash heredoc big enough to trip `oversize`, built rather than pasted so the file stays small.
BIG = "cat > /tmp/spec.md <<'EOF'\n" + ("a fairly ordinary line of prose\n" * 300) + "EOF\n"


def module_source():
    with open(SCRIPT, "r", encoding="utf-8") as handle:
        return handle.read()


def module_code():
    """The guard's source with its module docstring removed.

    Several assertions here are *absences* — the module must not emit a `permissionDecision`, must
    not import `subprocess`. The docstring names every one of those in order to explain why it does
    not use them, so a naive scan of the whole file fails on the prose that documents the property
    it is checking. Strip the docstring and the assertion means what it says.
    """
    source = module_source()
    docstring = ast.get_docstring(ast.parse(source), clean=False)
    return source.replace(docstring, "", 1) if docstring else source


def block(tool, command):
    return sfg.should_block(tool, command)[0]


def run_hook(payload):
    """Drive `main` exactly as the harness does: JSON on stdin, exit code out."""
    err = io.StringIO()
    code = sfg.main(argv=[], stdin=io.StringIO(payload), stderr=err)
    return code, err.getvalue()


class TheEscapeRouteTest(unittest.TestCase):
    """The stated legal way to comply must actually work. See this module's docstring."""

    RUN_THE_FILE = [
        "bash /c/Users/me/AppData/Local/Temp/x.sh",
        "sh /c/Users/me/AppData/Local/Temp/x.sh",
        "pwsh -NoProfile -File C:/Users/me/AppData/Local/Temp/x.ps1",
        "powershell -NoProfile -File C:/Users/me/AppData/Local/Temp/x.ps1",
        "python C:/Users/me/AppData/Local/Temp/x.py",
        "python3 /c/Users/me/AppData/Local/Temp/x.py",
    ]

    def test_the_escape_route_is_allowed(self):
        for tool in sfg.GUARDED_TOOLS:
            for command in self.RUN_THE_FILE:
                with self.subTest(tool=tool, command=command):
                    self.assertIsNone(block(tool, command))

    def test_a_bash_heredoc_is_the_second_route_and_is_not_refused(self):
        # `cat <<EOF` is correct shell in Bash and stays available; only PowerShell has no heredoc.
        self.assertIsNone(block("Bash", "cat > /tmp/x.sh <<'EOF'\necho hi\nEOF"))
        self.assertIsNone(block("Bash", "python - <<'PY'\nprint(1)\nPY"))

    def test_running_a_script_that_lives_in_a_windows_path_is_allowed(self):
        # The escape route has to survive Windows paths, which is where several sibling guards'
        # false positives live.
        self.assertIsNone(block(
            "PowerShell", r'pwsh -NoProfile -File "C:\Users\me\AppData\Local\Temp\x.ps1"'))


class RejectionNamesTheEscapeTest(unittest.TestCase):
    """Every refusal must name the Write tool and show a command that runs the file. The refusal is
    the only part of this hook a reader ever sees, so its wording is a contract, not prose."""

    def test_every_rule_names_the_write_tool(self):
        for rule, _predicate in sfg.RULES:
            with self.subTest(rule=rule):
                text = sfg.refusal_text(rule, "x" * 9000)
                self.assertIn("Write tool", text)
                self.assertIn("-NoProfile -File", text)
                self.assertTrue(text.startswith("Blocked:"), text[:40])

    def test_an_unknown_rule_name_still_names_the_escape(self):
        # `refusal_text` must not be able to produce a refusal with no way out of it.
        self.assertIn("Write tool", sfg.refusal_text("no-such-rule", "x"))

    def test_the_oversize_message_states_the_actual_size(self):
        self.assertIn("9,000", sfg.refusal_text("oversize", "x" * 9000))


class TheCasesThatBitTest(unittest.TestCase):
    """The real failure shapes each rule exists for, and the controls beside them."""

    def test_powershell_variable_eaten_by_bash(self):
        self.assertEqual(
            "cross_shell_expansion",
            block("Bash", 'powershell -Command "Get-Process | '
                          'Where-Object { $_.StartTime -gt 0 }"'))

    def test_powershell_eats_a_bash_variable_through_wsl(self):
        self.assertEqual(
            "cross_shell_expansion",
            block("PowerShell",
                  'wsl -d Ubuntu-24.04 -- bash -lc "for d in */; do echo \\"--- $d:\\"; done"'))

    def test_the_bare_region_case_is_reached_at_all(self):
        # The `$d` here is in a BARE run, not inside the quoted payload: a next-token-only reading of
        # the payload walks straight past it. This is the assertion that pins the segment-wide read.
        outer = sfg.TOOL_DIALECT["PowerShell"]
        payloads = sfg.cross_shell_payloads(
            'wsl -d Ubuntu-24.04 -- bash -lc "echo \\"x $d\\""', outer)
        self.assertEqual(1, len(payloads))
        self.assertEqual("posix", payloads[0][0])
        self.assertGreater(len(payloads[0][1]), 1, "payload must span the whole segment")

    def test_heredoc_in_powershell(self):
        for command in ("git commit -F - <<'EOF'\nchore: x\nEOF",
                        "gh pr create --body-file - <<'EOF'\nbody\nEOF",
                        "python << 'EOF'\nimport json\nEOF"):
            with self.subTest(command=command.splitlines()[0]):
                self.assertEqual("heredoc_in_powershell", block("PowerShell", command))

    def test_the_silent_ones_that_ran_and_were_wrong(self):
        # None of these three emits a diagnostic. They are the class no scan of errors can see,
        # and the reason this rule exists at all.
        self.assertEqual("cross_shell_expansion", block(
            "Bash", 'powershell -NoProfile -Command "Write-Output $env:CLAUDE_CODE_SESSION_ID"'))
        self.assertEqual("cross_shell_expansion", block(
            "Bash", "pwsh -Command \"Write-Host 'peek at: $(Get-Date -Format 'HH:mm:ss')'\""))
        self.assertEqual("cross_shell_expansion", block(
            "PowerShell",
            'wsl -d Ubuntu-24.04 -- bash -c "curl -H \\"Authorization: Bearer '
            '\\$API_TOKEN\\" https://example.invalid"'))

    def test_oversize_fires_on_the_shape_that_lost_a_spec(self):
        self.assertEqual("oversize", block("Bash", BIG))

    # ---- the controls. Read with the assertions above, never alone.

    def test_ordinary_work_is_untouched(self):
        for tool, command in (
            ("Bash", "git status"),
            ("Bash", "ls -la src"),
            ("Bash", "npm ci"),
            ("Bash", "git -c core.fsmonitor=false fetch origin"),
            ("Bash", "for d in */; do echo $d; done"),
            ("Bash", "awk '{print $1; print $2}' file.txt"),
            ("Bash", "cd x && ls; echo done"),
            ("PowerShell", "Get-ChildItem src"),
            ("PowerShell", "git log --oneline -5"),
            ("PowerShell", 'Get-Content x.json -Raw | ConvertFrom-Json | Out-Null'),
        ):
            with self.subTest(tool=tool, command=command):
                self.assertIsNone(block(tool, command))

    def test_same_dialect_nesting_is_not_a_finding(self):
        # `bash -c "echo $HOME"` from Bash expands in the outer shell and the inner one would have
        # produced the same bytes. It is the DIALECT boundary that makes an expansion a bug.
        self.assertIsNone(block("Bash", 'bash -c "echo $HOME"'))
        self.assertIsNone(block("Bash", 'sh -c "cd $PWD && ls"'))
        self.assertIsNone(block("PowerShell", 'pwsh -Command "Get-Process | % { $_.Name }"'))

    def test_a_single_quoted_payload_is_protected_and_allowed(self):
        # Single quotes stop the outer shell rewriting anything, which is the other legal fix and
        # must not be refused or the message's own advice would be a lie.
        self.assertIsNone(block("Bash", "powershell -Command 'Get-Process | % { $_.Name }'"))
        self.assertIsNone(block("PowerShell", "bash -lc 'for d in */; do echo $d; done'"))

    def test_deliberate_interpolation_of_a_name_this_command_assigned_is_allowed(self):
        # The false positives of the unrefined rule, in miniature. A name the OUTER command
        # assigns is a hand-off, not an accident.
        self.assertIsNone(block(
            "Bash", 'PY="C:/x/python.exe" && powershell -Command "Start-Process $PY"'))
        self.assertIsNone(block(
            "Bash", 'for f in *.ps1; do pwsh -NoProfile -Command "Test-Path $f"; done'))
        self.assertIsNone(block(
            "PowerShell", '$cmd = "ls -la"\nwsl.exe -d Ubuntu-24.04 -- bash -lc $cmd 2>&1'))

    def test_a_command_substitution_is_never_excused_by_an_assignment(self):
        # `$(` and a backtick have no name, so the self-assignment exclusion can never reach them.
        self.assertEqual("cross_shell_expansion", block(
            "Bash", 'X=1 && powershell -Command "Write-Output $(Get-Date)"'))

    def test_a_bare_backtick_crossing_into_powershell_is_caught(self):
        # The backtick is PowerShell's ESCAPE character and Bash's COMMAND SUBSTITUTION -- so the
        # careful `\`$env:X` spelling is exactly the one Bash eats. No `$` is needed for this to be
        # a bug, which is why the backtick is in the posix expansion set on its own.
        self.assertEqual("cross_shell_expansion",
                         block("Bash", 'pwsh -Command "Write-Host `n done"'))

    def test_assigned_names_are_read_from_outside_the_payload_only(self):
        # A `NAME=` inside the payload is the INNER script's variable. Letting it into the assigned
        # set would let a payload whitelist the very expansion the outer shell is about to eat.
        command = 'powershell -Command "Set-Variable a 1; b=2; Write-Output $b"'
        _payloads, outside = sfg._payload_and_outside(command, sfg.POSIX)
        self.assertNotIn("b=2", outside, "the payload must be blanked out of `outside`")
        self.assertNotIn("b", sfg._assigned_names(outside, sfg.POSIX))
        self.assertEqual("cross_shell_expansion", block("Bash", command))


class OversizeThresholdTest(unittest.TestCase):
    def test_just_under_is_allowed_and_just_over_is_refused(self):
        under = "echo " + "x" * (sfg.MAX_COMMAND_CHARS - 6)
        over = "echo " + "x" * (sfg.MAX_COMMAND_CHARS - 4)
        self.assertLess(len(under), sfg.MAX_COMMAND_CHARS)
        self.assertGreaterEqual(len(over), sfg.MAX_COMMAND_CHARS)
        self.assertIsNone(block("Bash", under))
        self.assertEqual("oversize", block("Bash", over))

    def test_it_applies_to_powershell_too(self):
        # PowerShell commands of this size are rare, so the cost is negligible, and it closes the
        # route around the Bash rule.
        self.assertEqual("oversize", block("PowerShell", "Write-Output " + "x" * 9000))

    def test_it_does_not_apply_to_other_tools(self):
        self.assertIsNone(block("Read", "x" * 9000))
        self.assertIsNone(block("Write", "x" * 9000))


class ThresholdBandTest(unittest.TestCase):
    """The threshold sits below the transport failure band, and above ground that was never at risk."""

    def test_the_threshold_is_the_documented_one(self):
        self.assertEqual(7500, sfg.MAX_COMMAND_CHARS)

    def test_it_sits_below_the_failure_band(self):
        # Truncation in transport starts a little under 8,000 characters and varies with content.
        self.assertLess(sfg.MAX_COMMAND_CHARS, 7600)

    def test_it_is_not_so_low_that_it_refuses_safe_ground(self):
        # Well below the band only working commands live; a threshold there would refuse shapes
        # that were never at risk.
        self.assertGreater(sfg.MAX_COMMAND_CHARS, 7000)


class HeredocScopeTest(unittest.TestCase):
    def test_a_quoted_double_angle_is_not_a_heredoc(self):
        for command in ('git commit -m "use << to write a heredoc"',
                        "Write-Output 'a << b'",
                        'Write-Output "a << b"',
                        "$note = @'\nuse << here\n'@"):
            with self.subTest(command=command[:32]):
                self.assertIsNone(block("PowerShell", command))

    def test_a_bash_heredoc_is_never_refused_by_this_rule(self):
        self.assertFalse(sfg.heredoc_in_powershell("Bash", "cat > x <<'EOF'\na\nEOF"))

    def test_a_herestring_operator_is_still_caught(self):
        # `<<<` contains `<<` and is equally not PowerShell. Refusing it is correct.
        self.assertEqual("heredoc_in_powershell", block("PowerShell", "grep x <<< 'text'"))

    def test_a_powershell_here_string_holding_an_apostrophe_is_still_one_piece(self):
        # `@'…'@` must be lexed as ONE literal run. Lexing it as a bare `@` plus an ordinary single
        # quote makes the apostrophe in "it's" close the quote early, and the rest of the body --
        # `<<` included -- falls out into a bare run and is misread as a heredoc.
        self.assertIsNone(block("PowerShell", "$x = @'\nit's got << in it\n'@"))
        self.assertIsNone(block("PowerShell", '$x = @"\nit\'s got << in it\n"@'))


class FailOpenTest(unittest.TestCase):
    """Opposite polarity to `merge_guard`'s FailClosedTest, on the same class of input."""

    HOSTILE = [
        "", "   ", "\n", "not json at all", "null", "[]", "42", '"a string"',
        '{"tool_name":"Bash"}',
        '{"tool_name":"Bash","tool_input":null}',
        '{"tool_name":"Bash","tool_input":[]}',
        '{"tool_name":"Bash","tool_input":{"command":null}}',
        '{"tool_name":"Bash","tool_input":{"command":123}}',
        '{"tool_name":null,"tool_input":{"command":"x"}}',
        '{"tool_input":{"command":"x"}}',
        '{"tool_name":"Bash","tool_input":{"command":"x"}',   # truncated JSON
    ]

    def test_every_hostile_payload_allows(self):
        for payload in self.HOSTILE:
            with self.subTest(payload=payload[:40]):
                code, err = run_hook(payload)
                self.assertEqual(sfg.EXIT_ALLOW, code)
                self.assertEqual("", err)

    def test_an_unguarded_tool_is_never_this_hooks_business(self):
        for tool in ("Read", "Write", "Edit", "Glob", "Grep", "Task", ""):
            with self.subTest(tool=tool):
                self.assertIsNone(block(tool, BIG))

    def test_an_unguarded_tool_short_circuits_before_any_rule_runs(self):
        # The tool check exists in `should_block` AND inside each predicate, deliberately -- the
        # settings.json `matcher` is an optimisation and not the safety mechanism (bash_path_guard
        # says the same). This pins the outer layer so the redundancy cannot quietly become single.
        seen = []

        def always(tool, _command):
            seen.append(tool)
            return True

        original = sfg.RULES
        try:
            sfg.RULES = (("always", always),)
            # The command must get PAST the pre-filter, or this passes for the wrong reason: a
            # payload with no shell name in it never reaches a rule whatever the tool is. Naming
            # `bash` is what makes the tool check the only thing left that can stop it.
            self.assertIsNotNone(sfg._PREFILTER.search("bash /tmp/x.sh"))
            self.assertIsNone(block("Read", "bash /tmp/x.sh"))
            self.assertEqual([], seen, "a rule ran for a tool this hook does not guard")
            # ...and the same command through a guarded tool DOES reach the rule, so the assertion
            # above is about the tool and not about the pre-filter.
            self.assertEqual("always", block("Bash", "bash /tmp/x.sh"))
            self.assertEqual(["Bash"], seen)
        finally:
            sfg.RULES = original

    def test_a_predicate_that_raises_costs_its_rule_and_not_the_call(self):
        def explode(_tool, _command):
            raise RuntimeError("boom")

        original = sfg.RULES
        try:
            sfg.RULES = (("explode", explode),) + original
            self.assertIsNone(block("Bash", "git status"))
            # the surviving rules still answer
            self.assertEqual("oversize", block("Bash", BIG))
        finally:
            sfg.RULES = original

    def test_a_missing_merge_guard_does_not_break_the_guard(self):
        original = sfg._mg_cache
        try:
            sfg._mg_cache = None
            self.assertEqual("cross_shell_expansion", block(
                "Bash", 'powershell -Command "Write-Output $_.Name"'))
            self.assertIsNone(block("Bash", "git status"))
            self.assertEqual("powershell", sfg.strip_exe("C:/Windows/powershell.exe"))
        finally:
            sfg._mg_cache = original

    def test_the_tokenizer_terminates_on_anything(self):
        for command in ('"', "'", "@'", '@"', "`", "\\", "${", "$(", "<<", "|", "&&",
                        '"unterminated', "'unterminated", "\x00\x01", "€" * 50):
            with self.subTest(command=repr(command)):
                for dialect in (sfg.POSIX, sfg.POWERSHELL):
                    sfg.tokenize(command, dialect)   # must not raise or hang
                self.assertIn(block("Bash", command), (None, "oversize"))


class HookContractTest(unittest.TestCase):
    def test_a_block_is_exit_two_with_the_reason_on_stderr(self):
        code, err = run_hook(json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": 'powershell -Command "Write-Output $_.Name"'}}))
        self.assertEqual(sfg.EXIT_BLOCK, code)
        self.assertIn("Blocked:", err)
        self.assertIn("Write tool", err)

    def test_an_allow_prints_nothing_at_all(self):
        code, err = run_hook(json.dumps({
            "tool_name": "Bash", "tool_input": {"command": "git status"}}))
        self.assertEqual(sfg.EXIT_ALLOW, code)
        self.assertEqual("", err)

    def test_it_never_emits_a_json_permission_decision(self):
        # bash_path_guard's argument: a malformed JSON decision fails schema validation and the
        # contract then lets the action PROCEED, so a writer bug silently disables the guard.
        # Checked against the CODE, not the module docstring -- which names the mechanism in order
        # to explain why it is not used, and would otherwise fail this test for saying so.
        self.assertNotIn("permissionDecision", module_code())
        self.assertNotIn("hookSpecificOutput", module_code())

    def test_check_subcommand_reports_without_writing(self):
        out = io.StringIO()
        code = sfg.main(argv=["check", "--tool", "Bash", "--command", "git status"], stdout=out)
        self.assertEqual(sfg.EXIT_ALLOW, code)
        self.assertIn("allowed", out.getvalue())

        out = io.StringIO()
        code = sfg.main(
            argv=["check", "--tool", "PowerShell", "--command", "git commit -F - <<'EOF'\nx\nEOF"],
            stdout=out)
        self.assertEqual(sfg.EXIT_BLOCK, code)
        self.assertIn("heredoc_in_powershell", out.getvalue())


class SelfImmunityTest(unittest.TestCase):
    """A guard that refuses the commands used to write about it is a guard somebody turns off."""

    def test_the_refusal_messages_do_not_match_their_own_rules(self):
        for rule, _predicate in sfg.RULES:
            text = sfg.refusal_text(rule, "x" * 9000)
            with self.subTest(rule=rule):
                # The message is not a command, but the shapes inside it must not be re-blockable
                # when someone echoes or greps them.
                self.assertIsNone(block("Bash", "grep -n 'Blocked:' script_file_guard.py"))
                self.assertLess(len(text), sfg.MAX_COMMAND_CHARS)

    def test_writing_about_the_guard_is_allowed(self):
        for command in (
            "grep -rn 'cross_shell_expansion' seneschal/scripts/",
            'grep -n "powershell -Command" seneschal/docs/*.md',
            "git commit -m 'feat(guard): refuse cross-shell expansion'",
            "git log --oneline --grep='heredoc'",
            'echo "powershell -Command \\"$_\\" is the shape it refuses"',
            "rg '<<' seneschal/scripts/script_file_guard.py",
        ):
            with self.subTest(command=command[:48]):
                self.assertIsNone(block("Bash", command))

    def test_detection_is_at_command_position(self):
        # `grep`/`echo` are first, so there is no shell being invoked here at all.
        self.assertIsNone(block("Bash", 'echo "pwsh -Command \\"$_\\""'))
        self.assertIsNone(block("Bash", "cat notes.md | grep 'bash -lc'"))


class VocabularyCoverageTest(unittest.TestCase):
    """The sets are deliberately wider than `merge_guard`'s, and must never be narrower."""

    def test_every_shell_merge_guard_follows_has_a_dialect_here(self):
        missing = set(merge_guard.NESTED_SHELLS) - set(sfg.SHELL_DIALECT)
        self.assertEqual(set(), missing,
                         f"merge_guard follows {sorted(missing)} into a nested command and this "
                         f"guard has no dialect for them, which is a hole, not a difference")

    def test_every_nested_flag_merge_guard_knows_is_known_here(self):
        missing = set(merge_guard.NESTED_FLAGS) - set(sfg.NESTED_FLAGS)
        self.assertEqual(set(), missing)

    def test_this_guard_is_deliberately_wider(self):
        # If this ever becomes equality, somebody has "tidied" the sets into one and `-lc` — the
        # spelling `wsl … -- bash -lc "…"` uses — has gone with it.
        self.assertIn("-lc", sfg.NESTED_FLAGS)
        self.assertNotIn("-lc", merge_guard.NESTED_FLAGS)

    def test_strip_exe_is_merge_guards(self):
        self.assertEqual("powershell", sfg.strip_exe(r"C:\Windows\System32\powershell.exe"))
        self.assertEqual("bash", sfg.strip_exe("/usr/bin/bash"))
        self.assertEqual(merge_guard.strip_exe("PWSH.EXE"), sfg.strip_exe("PWSH.EXE"))


class NoIOTest(unittest.TestCase):
    """It reads no file, opens no socket and spawns no process — which is what licenses the 10 s
    timeout and the fail-open posture both."""

    def test_the_module_imports_nothing_that_could_reach_out(self):
        head = module_code().split("# ---", 1)[0]
        for forbidden in ("import subprocess", "import socket", "import urllib",
                          "import requests", "import http"):
            self.assertNotIn(forbidden, head, f"{forbidden} at module scope")

    def test_the_decision_path_opens_no_file(self):
        # `open(` appears exactly once, in `check --command-file`, which is not on the hook path.
        with open(SCRIPT, "r", encoding="utf-8") as handle:
            source = handle.read()
        opens = re.findall(r"\bopen\(", source)
        self.assertEqual(1, len(opens), f"expected one open() (check --command-file), got {opens}")

    def test_it_writes_no_state(self):
        source = module_code()
        for forbidden in ('open(', 'os.replace', 'makedirs', 'state/'):
            if forbidden == 'open(':
                continue
            self.assertNotIn(forbidden, source)


class RuleSetTest(unittest.TestCase):
    """The rule set is deliberately small: each rule refuses only commands that were already broken.
    If a fourth is added, this fails, and whoever added it has to show the new rule clears the same
    bar — it refuses broken commands, not merely unusual ones — and update the setup doc."""

    def test_the_rule_set_is_the_documented_one(self):
        self.assertEqual(
            ["oversize", "heredoc_in_powershell", "cross_shell_expansion"],
            [name for name, _ in sfg.RULES])

    def test_every_rule_has_a_reason_and_every_reason_has_a_rule(self):
        self.assertEqual(set(sfg.REASONS), {name for name, _ in sfg.RULES})


if __name__ == "__main__":
    unittest.main(verbosity=2)
