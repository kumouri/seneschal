#!/usr/bin/env python3
"""**A `PreToolUse` hook: refuse a Bash command holding a double-quoted Windows path that ends in a
backslash.** One narrow, high-precision rule rather than a broad style policy.

The mechanism is a lexing fact, not a style opinion. In Bash a backslash inside double quotes
escapes the next character, so ``"C:\\Users\\me\\state\\"`` never closes its quote and the command
dies at the parser with ``unexpected EOF while looking for matching `"'`` — routinely reported
against a token far away from the real mistake, which is why it tends to cost a whole retry.

**Why this rule and not a broader one.** Wider candidate rules (refuse any ``;``-chained command,
any ``\\"``, any long one-liner) catch the same failures but refuse many working commands for
each failure they prevent. This rule is deliberately narrow: nearly everything it blocks is a
command that would have died at the parser anyway, and it covers the single most common parser
failure class for shell calls on Windows.

**Scoping to Bash is what makes it shippable, and it is not a tuning choice.** In PowerShell the
backslash is not an escape character, so ``"C:\\path\\"`` closes its quote and runs correctly —
every PowerShell match is a false positive *by construction*. So :data:`GUARDED_TOOL` is a
correctness constraint: widening this hook to another tool imports false positives for nothing.

## The one definition

:data:`WINDOWS_PATH_TRAILING_BACKSLASH_RE` and :func:`should_block` live **here**. This module must
stay importable under a bare ``python`` from any session on the machine — including sessions with
nothing to do with this repo — so it is stdlib only, needs no venv, and reads nothing from disk at
import.

## The hook contract, verified rather than assumed

``PreToolUse`` delivers the event as JSON on **stdin** — ``tool_name`` (a string) and ``tool_input``
(an object; for Bash its ``command`` is the string this rule reads). Two block mechanisms are
supported: **exit 2 with the reason on stderr**, or exit 0 with a
``hookSpecificOutput.permissionDecision = "deny"`` JSON object on stdout.

**This hook uses exit 2 + stderr, deliberately.** The failure this hook is least allowed to have is
*silently never firing*, because that looks exactly like success. Under the JSON mechanism the block
rides on a well-formed object reaching stdout, and a malformed or truncated one fails schema
validation — which the contract resolves by **letting the action proceed**. Under exit 2 the signal
is the exit code, which cannot be half-written, and the reason is free text with no encoding
contract to get wrong. It also gives requirement "fail open, always" for free: every other exit code
means *non-blocking error, action proceeds*, so even an uncaught exception here (exit 1) allows the
command rather than blocking it. Same shape as ``session_stamp.py``, the machine-wide hook the
session registry installs.

**Fail open, absolutely and on every path.** Unreadable stdin, non-JSON, a missing key, an
unexpected schema, a raise inside the predicate — all exit 0, silent. This hook fires in *every*
Claude Code session on the box; a guard that breaks may never be the thing that stops the owner's
work. There is no failure mode here whose answer is "block".

Install (the owner's to make — no PR can write ``~/.claude/settings.json``):
``BASH_PATH_GUARD_SETUP.md``, or ``settings_merge.py --guard bash-path``.

**Known and accepted false positives.** The regex has no notion of escaping at its left edge, so an
*escaped* quote can serve as its opening quote and a doubled backslash can be an escape rather than
a path separator — ``python -c "print(f\\"\\\\n===\\")"``, a ``\\\\"`` crossing into a nested
PowerShell. Those are correct Bash and this hook refuses them. They are rare next to the real
catches; the rejection message names this case and points at the one fix that is right for it
anyway (a script file). Do **not** widen the expression without evidence that the new arm catches
real failures more often than it refuses working commands.
"""
from __future__ import annotations

import json
import re
import sys

# The one definition. A double-quoted run that (a) looks like a Windows path — a drive letter or a
# doubled backslash — and (b) ends in a backslash immediately before the closing quote.
#
# Requiring (a) rather than merely matching any `\"` is the first of two narrowings: it removes the
# largest false-positive class, because `echo "they said \"hi\""` is correct shell and the naive
# rule refuses it.
WINDOWS_PATH_TRAILING_BACKSLASH_RE = re.compile(r'"[^"\n]*(?:[A-Za-z]:\\|\\\\)[^"\n]*\\"')

# The second narrowing, and a correctness constraint rather than a knob — see the module docstring.
GUARDED_TOOL = "Bash"

REJECTION_REASON = """\
Blocked: this Bash command contains a double-quoted Windows path ending in a backslash.

In Bash the trailing \\ escapes the closing " , so the string never terminates. The command dies at
the parser with: unexpected EOF while looking for matching `"' -- and that error is usually reported
against a token far away from the real mistake, which is why it costs a whole retry to find.

Rewrite it either way:
  * use forward slashes           ->  "C:/path/to/repo/seneschal/state/"
  * or drop the trailing backslash ->  "C:\\path\\to\\repo\\seneschal\\state"

If what this matched is an escape rather than a path separator -- a \\\\n inside a python -c string,
a \\" crossing into a nested shell -- then this is a known false positive of the rule. Put that
command in a script file and run the file: the escaping you are fighting is exactly what a script
file removes."""


def should_block(tool_name, command):
    """Should a ``PreToolUse`` hook refuse this call? ``True`` only for the Bash tool.

    The tool check comes first and is not an optimisation: a PowerShell command matching this regex
    is a false positive by construction, so answering ``True`` for one would be wrong rather than
    merely noisy. Anything that is not a string is not a Bash command and is not our business.
    """
    if tool_name != GUARDED_TOOL or not isinstance(command, str) or not command:
        return False
    return bool(WINDOWS_PATH_TRAILING_BACKSLASH_RE.search(command))


def decide(event):
    """One ``PreToolUse`` event -> whether to block. Never raises on a hostile shape; a payload this
    hook cannot understand is a payload it has no opinion about."""
    if not isinstance(event, dict):
        return False
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    return should_block(event.get("tool_name"), tool_input.get("command"))


def main(stdin=None, stderr=None):
    """Hook entrypoint. ``0`` = allow (and print nothing), ``2`` = block with the reason on stderr.

    The whole read-and-decide path is wrapped: the only way out of this function that is not
    ``return 0`` is a decision that positively identified the bug.
    """
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        blocked = decide(json.loads(raw)) if raw and raw.strip() else False
    except Exception:  # noqa: BLE001 -- fail open by contract; see the module docstring
        return 0
    if not blocked:
        return 0
    try:
        (stderr if stderr is not None else sys.stderr).write(REJECTION_REASON + "\n")
    except Exception:  # noqa: BLE001 -- a block we cannot explain is still a correct block
        pass
    return 2


if __name__ == "__main__":
    sys.exit(main())
