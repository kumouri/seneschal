#!/usr/bin/env python3
r"""**A `PreToolUse` hook: refuse the three shell one-liners that are MECHANICALLY broken, and name
the script file that fixes each.** Standard library only. No network, no disk, no subprocess.

A written convention such as *"shell work goes in a script file, not a one-liner"* is easy to agree
with and does not bind: an agent under time pressure reaches for the one-liner anyway, and a prose
rule has no way to refuse it. This hook is the enforcement half of that convention — but only the
half that can be enforced without refusing correct work.

## What it refuses, and why each is a fact rather than a preference

**This hook does not enforce the whole convention.** Several of the obvious clauses — "`;`-chained",
"multi-line", "contains a heredoc" — are properties of *correct* shell scripting: a rule built on
them refuses many working commands for every broken one it catches, and a guard that refuses a large
share of every shell call is uninstalled within a day, and then it protects nothing. So this file
ships only the part that is a **lexing or transport fact**, and `SCRIPT_FILE_GUARD_SETUP.md` states
plainly which part is left to prose.

| Rule | Mechanism |
|---|---|
| :func:`oversize` | the command does not survive transport intact |
| :func:`cross_shell_expansion` | this shell substitutes before the other shell reads |
| :func:`heredoc_in_powershell` | PowerShell has no heredoc; `<` is reserved |

Each one blocks commands that were already broken; that is what makes them shippable.

### `oversize` — and the mechanism is NOT "it is long"

Very large commands fail at the parser even when they are correct: fed to ``bash -n`` as a file,
they parse clean. They did not reach `bash` intact — the text is truncated in transport, and `bash`
then reports an unterminated quote or heredoc at the cut, hundreds of lines from anything a reader
would call a mistake. That is why the diagnostic is so uninformative and why this costs a whole
retry.

The threshold (:data:`MAX_COMMAND_CHARS`) sits below the whole failure band rather than at its top,
because the cliff is a band, not a line: the effective ceiling depends on how much the content
expands when it is escaped for transport, so some failures sit *below* some successes. Blocking the
whole ambiguous band is the safer side — an unnecessary block costs one Write call, and a miss costs
an authored PR body or spec, silently.

### `cross_shell_expansion` — the one whose failure mode is silence

`powershell -Command "… $_.StartTime …"` issued from **Bash**: Bash substitutes `$_` before
PowerShell ever sees the string. This is the strongest argument for enforcement in the whole class,
because **a mangled command that runs rather than erroring emits no diagnostic at all.** Typical
silent casualties: an `Authorization: Bearer \$TOKEN` header that reaches `curl` as `Bearer \`, a
`Write-Output $env:CLAUDE_CODE_SESSION_ID` that prints `:CLAUDE_CODE_SESSION_ID`, a timestamp whose
`$(Get-Date …)` was run by *Bash*. None of those is visible to any scan of diagnostics.

**The exclusion is what keeps it free of false positives, and it is not a tuning knob.** The
legitimate cross-shell interpolations interpolate a variable **the outer command assigned itself**
(`SC=`, `PY=`, `$key`, a `for f in` loop variable) — that is a deliberate hand-off, and the broken
commands it might resemble never do it. So a name assigned **outside the payload** is allowed
through, and `$(`, `${` and a backtick never are: a command substitution cannot have been assigned a
moment earlier.

### `heredoc_in_powershell`

PowerShell has no heredoc and `<` is a reserved redirection operator, so an unquoted `<<` there dies
at the parser and takes the whole payload — a commit message, a PR body, a JSON document — with it.
**A heredoc in the *Bash* tool is correct shell and this hook never touches it.**

## The escape this hook is required to leave open, and it was verified rather than assumed

Refusing heredocs would refuse `cat > script.sh <<'EOF'`, which is the obvious way to *create* the
script the refusal asks for. **A guard that blocks the only route to compliance is a wall.** So:

* the legal route is the **Write tool**, then a short command that runs the file — `bash x.sh`,
  `pwsh -NoProfile -File x.ps1`, `python x.py` — and **every rejection message says so in those
  words**, pinned by :class:`test_script_file_guard.RejectionNamesTheEscapeTest`;
* `test_the_escape_route_is_allowed` drives that exact pair through :func:`should_block` and asserts
  it is allowed, in both tools;
* and a **Bash** heredoc is not refused at all, so the second route stays open too.

## Posture: fail OPEN, on every path, like `bash_path_guard` and unlike `merge_guard`

This hook fires on every Bash and PowerShell call in every session on the machine, including work
with nothing to do with this repo. It reads no file, opens no socket and spawns no process, so there
is no unhappy path whose honest answer is *block*: unreadable stdin, non-JSON, a missing key, an
unrecognised shape, a raise anywhere in the predicate — **all exit 0, silently.**
`merge_guard`'s `FailClosedTest` and this module's `FailOpenTest` assert opposite exit codes on the
same class of input; if they ever agree, one of them is wrong.

**Blocking is exit 2 + stderr, never the JSON `permissionDecision`** — `bash_path_guard`'s argument
verbatim: a malformed JSON decision fails schema validation and the contract then lets the action
*proceed*, so a writer bug would silently disable the guard, and that failure looks exactly like
success.

## What it shares with the other hooks, and what it deliberately does not

:data:`SHELL_DIALECT` and :data:`NESTED_FLAGS` are **supersets** of `merge_guard.NESTED_SHELLS` and
`merge_guard.NESTED_FLAGS`, checked as a superset by a test rather than copied — this module needs
`-lc` (which `wsl … -- bash -lc "…"` uses and `merge_guard` has no reason to know), so the sets are
related but not identical, and the test is what stops a shell added there from becoming a silent gap
here. `merge_guard.strip_exe` is **imported**, not re-spelled.

The **tokenizer below is new, and it has to be**: `merge_guard.command_segments` lexes with
`shlex`, which *discards quoting* — and the whole question this module asks is whether the payload
was single-quoted (the outer shell leaves it alone) or not (the outer shell rewrites it). A lexer
that answers `merge_guard`'s question cannot answer this one, so this is a different function rather
than a second copy.

Install — the owner's to make, since no PR can write `~/.claude/settings.json`, and **the hook is
inert until they do**: `SCRIPT_FILE_GUARD_SETUP.md`, or `settings_merge.py --guard script-file`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

#: `merge_guard` is imported **lazily**, and that is a latency decision rather than tidiness: its
#: import costs several times this hook's whole run, and this hook runs on every Bash and PowerShell
#: call in every session, so paying it for `git status` would multiply the per-call cost. Only
#: :func:`strip_exe` needs it, and only on the small fraction of commands that name a shell at all. `merge_guard` does the
#: same thing to `pr_digest`/`pr_overlap` for the same reason.
_MG_SENTINEL = object()
_mg_cache = _MG_SENTINEL


def _merge_guard():
    """The `merge_guard` module, or `None` if it cannot be imported. Never raises: a guard that
    breaks because a sibling moved is a guard that stops the owner working."""
    global _mg_cache
    if _mg_cache is _MG_SENTINEL:
        try:
            import merge_guard
            _mg_cache = merge_guard
        except Exception:  # noqa: BLE001 - fail open; the local spelling below still answers
            _mg_cache = None
    return _mg_cache

#: Both shells. Unlike `bash_path_guard`, whose Bash-only scope is a correctness constraint, every
#: rule here is defined per-dialect and is meaningful in each.
GUARDED_TOOLS = ("Bash", "PowerShell")

#: `0` allow (silent), `2` block with the reason on stderr. There is no third outcome: this module
#: makes no network call, so it has no "could not check" state to report.
EXIT_ALLOW = 0
EXIT_BLOCK = 2

#: Dialect names. `cmd` exists so that `cmd /c` is recognised as a foreign shell rather than
#: silently ignored; nothing here is scored on it.
POSIX = "posix"
POWERSHELL = "powershell"
CMDEXE = "cmd"

#: Which dialect each tool speaks. A tool absent from this map is not this hook's business.
TOOL_DIALECT = {"Bash": POSIX, "PowerShell": POWERSHELL}

#: Which dialect each shell NAME speaks. A superset of `merge_guard.NESTED_SHELLS`, asserted by
#: `VocabularyCoverageTest`: a shell that guard follows and this one has no dialect for would be a
#: hole rather than a difference of opinion.
SHELL_DIALECT = {
    "bash": POSIX, "sh": POSIX, "zsh": POSIX, "dash": POSIX, "ash": POSIX, "ksh": POSIX,
    "powershell": POWERSHELL, "pwsh": POWERSHELL,
    "cmd": CMDEXE,
}

#: Flags whose argument is *another command*. A superset of `merge_guard.NESTED_FLAGS` — `-lc` and
#: `-lic` are how `wsl … -- bash -lc "…"` is usually spelled on Windows hosts.
NESTED_FLAGS = frozenset({
    "-c", "-C", "-Command", "-command", "-COMMAND",
    "-lc", "-lic", "-ic", "-l", "-e", "-ec", "-EncodedCommand",
    "/c", "/C", "/k", "/K",
})

#: Programs that stand in front of the real shell without being one. `wsl -d Ubuntu-24.04 -- bash
#: -lc "…"` is the case that matters; their own flags take values, so the walk below scans forward
#: for the first token that IS a shell rather than modelling each wrapper's option grammar.
TRANSPARENT_WRAPPERS = frozenset({
    "wsl", "sudo", "env", "command", "nohup", "time", "winpty", "stdbuf", "doas",
})

#: Quoting that stops the OUTER shell from expanding what is inside it. This is the whole hinge of
#: :func:`cross_shell_expansion`: posix `'…'` is literal, and so are PowerShell's `'…'` and its
#: `@'…'@` here-string. Everything else is rewritten before the inner shell sees a byte.
PROTECTED_QUOTES = {POSIX: ("'", "@'"), POWERSHELL: ("'", "@'"), CMDEXE: ("'",)}

#: **7,500, and the number sits below a measured cliff rather than being chosen.** Commands a
#: little under 8,000 characters start failing in transport, and where exactly depends on content,
#: because the transport escapes the text before it measures it — so the failure band overlaps the
#: success band. Sitting below the whole band catches every truncation at the cost of an occasional
#: unnecessary block (one Write call). The trade is argued in `SCRIPT_FILE_GUARD_SETUP.md`.
MAX_COMMAND_CHARS = 7500

#: Shell operators that end a command and start a new one. Command position is what makes
#: `grep "powershell -Command \"$x\""` immune — the same self-immunity `bash_path_guard` and
#: `merge_guard` both pin with a test.
_OPERATORS = ("&&", "||", "|&", ";;", "|", ";", "\n", "(", ")", "{", "}")

#: What each dialect substitutes inside text it is willing to rewrite. `$_` is included for posix
#: deliberately: it is a real Bash parameter, which is exactly why a PowerShell `$_` handed to Bash
#: comes out as something else.
_POSIX_EXPANSION = re.compile(r"(?<!\\)\$(?:\{|\(|[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?!$_-])|(?<!\\)`")
_PS_EXPANSION = re.compile(r"(?<!`)\$(?:\{|\(|[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z0-9_]+)?|\?|\^|\$|_)")

#: A named expansion, for the self-assignment exclusion. `$(`, `${` and a backtick match nothing
#: here and are therefore never excused.
_NAMED_EXPANSION = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)")

#: Assignments the OUTER command makes itself, read only from outside the payload.
_POSIX_ASSIGN = re.compile(
    r"(?:^|[\s;&|(])(?:export\s+|declare\s+|local\s+|readonly\s+)?([A-Za-z_][A-Za-z0-9_]*)=")
_POSIX_FOR = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
_POSIX_READ = re.compile(r"\bread\s+(?:-\S+\s+)*([A-Za-z_][A-Za-z0-9_]*)")
_PS_ASSIGN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)")

#: The cheap pre-filter. The overwhelming majority of shell calls are `git status`-shaped and must
#: cost nothing, so the expensive scan runs only when one of these appears anywhere in the text.
_PREFILTER = re.compile(
    r"<<|(?:^|[^A-Za-z0-9_])(?:bash|sh|zsh|dash|ash|ksh|pwsh|powershell|cmd|wsl)(?:\.exe)?\b")


# --------------------------------------------------------------------------- tokenizer

class Piece:
    """One quoted-or-bare run inside a token, with the quote that enclosed it and its offsets."""

    __slots__ = ("quote", "text", "start", "end")

    def __init__(self, quote, text, start, end):
        self.quote = quote
        self.text = text
        self.start = start
        self.end = end


class Token:
    """A shell word (``pieces``) or an operator (``op``), never both."""

    __slots__ = ("pieces", "op")

    def __init__(self, pieces, op=None):
        self.pieces = pieces
        self.op = op

    @property
    def value(self):
        return "".join(p.text for p in self.pieces)


def tokenize(command, dialect):
    """Split ``command`` into :class:`Token`s, **keeping which quote enclosed each piece**.

    Not a shell parser and not trying to be. It answers exactly two questions: where does a new
    command position begin, and for each run of text, will this dialect rewrite it. Anything it
    cannot make sense of is consumed as a bare piece rather than raising — a string this function
    cannot lex is still a string the caller has to have an answer about, and that answer is *allow*.
    """
    toks = []
    pieces = []
    i = 0
    n = len(command)

    def flush():
        nonlocal pieces
        if pieces:
            toks.append(Token(pieces))
            pieces = []

    while i < n:
        ch = command[i]
        if ch in " \t\r":
            flush()
            i += 1
            continue
        if ch == "\n":
            flush()
            toks.append(Token([], op="\n"))
            i += 1
            continue
        if ch == "#" and not pieces:
            while i < n and command[i] != "\n":
                i += 1
            continue
        op = next((o for o in _OPERATORS if o != "\n" and command.startswith(o, i)), None)
        if op:
            flush()
            toks.append(Token([], op=op))
            i += len(op)
            continue
        # PowerShell here-strings. `@'…'@` is literal; `@"…"@` interpolates, so only the first is
        # protected -- and that asymmetry is the whole reason the quote is recorded rather than
        # stripped.
        if dialect == POWERSHELL and (command.startswith("@'", i) or command.startswith('@"', i)):
            opener = command[i:i + 2]
            closer = opener[1] + "@"
            end = command.find(closer, i + 2)
            body = command[i + 2:end if end != -1 else n]
            pieces.append(Piece(opener, body, i + 2, (end if end != -1 else n)))
            i = (end + 2) if end != -1 else n
            continue
        if ch == "'":
            j, buf = i + 1, []
            while j < n:
                if command[j] == "'":
                    if dialect == POWERSHELL and j + 1 < n and command[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(command[j])
                j += 1
            pieces.append(Piece("'", "".join(buf), i + 1, j))
            i = j + 1
            continue
        if ch == '"':
            j, buf = i + 1, []
            while j < n:
                c = command[j]
                if c == "\\" and dialect == POSIX and j + 1 < n:
                    buf.append(c)
                    buf.append(command[j + 1])
                    j += 2
                    continue
                if c == "`" and dialect == POWERSHELL and j + 1 < n:
                    buf.append(c)
                    buf.append(command[j + 1])
                    j += 2
                    continue
                if c == '"':
                    if dialect == POWERSHELL and j + 1 < n and command[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(c)
                j += 1
            pieces.append(Piece('"', "".join(buf), i + 1, j))
            i = j + 1
            continue
        if ch == "\\" and dialect == POSIX and i + 1 < n:
            pieces.append(Piece(None, command[i:i + 2], i, i + 2))
            i += 2
            continue
        if ch == "`" and dialect == POWERSHELL and i + 1 < n:
            pieces.append(Piece(None, command[i:i + 2], i, i + 2))
            i += 2
            continue
        j, buf = i, []
        while j < n and command[j] not in " \t\r\n'\"":
            if any(command.startswith(o, j) for o in _OPERATORS if o != "\n"):
                break
            if command[j] == "\\" and dialect == POSIX:
                break
            if command[j] == "`" and dialect == POWERSHELL:
                break
            buf.append(command[j])
            j += 1
        if j == i:  # never fail to make progress on a byte we do not understand
            buf.append(command[i])
            j = i + 1
        pieces.append(Piece(None, "".join(buf), i, j))
        i = j
    flush()
    return toks


def strip_exe(token):
    """`C:/Windows/System32/powershell.exe` -> `powershell`. `merge_guard`'s, imported when it is
    importable, so the two hooks normalise a program name identically."""
    mg = _merge_guard()
    if mg is not None:
        try:
            return mg.strip_exe(token)
        except Exception:  # noqa: BLE001 - fall through to the local spelling
            pass
    base = os.path.basename((token or "").strip().strip("'\"").replace("\\", "/"))
    return base[:-4].lower() if base.lower().endswith(".exe") else base.lower()


# --------------------------------------------------------------------------- rule 2 support

def cross_shell_payloads(command, outer):
    """``[(inner_dialect, [payload tokens])]`` for every **cross-dialect** shell invoked at command
    position.

    Two decisions inside this are load-bearing.

    **Same-dialect nesting is not a finding.** `bash -c "echo $HOME"` from Bash expands `$HOME` in
    the outer shell and the inner one would have produced the same bytes; there is no mangling to
    prevent, and blocking it would refuse an idiom that works. It is the *dialect boundary* that
    makes an expansion a bug, exactly as it is the *Bash* scope that makes a trailing backslash one.

    **The payload runs to the end of the segment, not to the next token.** A broken quote splits one
    intended argument across several tokens, and that split IS the failure being caught —
    `wsl … bash -lc "… echo \\"--- $d:\\" …"` puts the mangled `$d` in a *bare* run that a
    next-token-only reading walks straight past.
    """
    toks = tokenize(command, outer)
    found = []
    at_command = True
    idx = 0
    while idx < len(toks):
        tok = toks[idx]
        if tok.op is not None:
            at_command = tok.op not in (")", "}")
            idx += 1
            continue
        if at_command:
            head = strip_exe(tok.value)
            k = idx
            if head in TRANSPARENT_WRAPPERS:
                j, steps = idx + 1, 0
                while j < len(toks) and toks[j].op is None and steps < 10:
                    cand = strip_exe(toks[j].value)
                    if cand in SHELL_DIALECT:
                        head, k = cand, j
                        break
                    if cand not in TRANSPARENT_WRAPPERS and toks[j].value in NESTED_FLAGS:
                        break
                    j += 1
                    steps += 1
            inner = SHELL_DIALECT.get(head)
            if inner is not None and inner != outer:
                j = k + 1
                while j < len(toks) and toks[j].op is None:
                    if toks[j].value in NESTED_FLAGS and j + 1 < len(toks) \
                            and toks[j + 1].op is None:
                        end = j + 1
                        while end < len(toks) and toks[end].op is None:
                            end += 1
                        found.append((inner, toks[j + 1:end]))
                        break
                    j += 1
        at_command = False
        idx += 1
    return found


def _payload_and_outside(command, outer):
    """``(payloads, outside)`` — the text the inner shell was meant to receive, and the text that is
    not it. The assigned-name set is read from ``outside`` **only**, so a `$content = …` inside the
    PowerShell payload cannot whitelist the very expansion Bash is about to eat."""
    protected = PROTECTED_QUOTES.get(outer, ("'",))
    payloads = []
    covered = []
    for inner, toks in cross_shell_payloads(command, outer):
        words = []
        for tok in toks:
            # Pieces WITHIN a token really are concatenated by the shell (`foo"bar"` is one word),
            # but two TOKENS are not -- joining them bare fuses `$cmd` and `2>&1` into a variable
            # named `cmd2` that neither of them contains, and the rule then refuses a deliberate
            # hand-off.
            words.append("".join(
                "" if piece.quote in protected else piece.text for piece in tok.pieces))
            covered.extend((piece.start, piece.end) for piece in tok.pieces)
        payloads.append((inner, " ".join(words)))
    if not covered:
        return payloads, command
    chars = list(command)
    for start, end in covered:
        for pos in range(max(0, start), min(len(chars), end)):
            chars[pos] = " "
    return payloads, "".join(chars)


def _assigned_names(outside, outer):
    if outer == POSIX:
        return (set(_POSIX_ASSIGN.findall(outside)) | set(_POSIX_FOR.findall(outside))
                | set(_POSIX_READ.findall(outside)))
    if outer == POWERSHELL:
        return set(_PS_ASSIGN.findall(outside))
    return set()


# --------------------------------------------------------------------------- the three rules

def oversize(tool_name, command):
    """Is this command too large to reach the shell intact? See :data:`MAX_COMMAND_CHARS`."""
    if tool_name not in GUARDED_TOOLS:
        return False
    return len(command) >= MAX_COMMAND_CHARS


def cross_shell_expansion(tool_name, command):
    """Does this command hand another shell text that THIS shell rewrites first?"""
    outer = TOOL_DIALECT.get(tool_name)
    if outer is None:
        return False
    payloads, outside = _payload_and_outside(command, outer)
    if not payloads:
        return False
    defined = _assigned_names(outside, outer)
    rx = _POSIX_EXPANSION if outer == POSIX else _PS_EXPANSION
    for _inner, text in payloads:
        for match in rx.finditer(text):
            named = _NAMED_EXPANSION.match(match.group(0))
            if named is None or named.group(1) not in defined:
                return True
    return False


def heredoc_in_powershell(tool_name, command):
    """An unquoted `<<` in a PowerShell command. PowerShell has no heredoc and `<` is reserved."""
    if tool_name != "PowerShell":
        return False
    if "<<" not in command:
        return False
    for tok in tokenize(command, POWERSHELL):
        if tok.op is not None:
            continue
        for piece in tok.pieces:
            if piece.quote is None and "<<" in piece.text:
                return True
    return False


#: Rule name -> predicate. Ordered: the cheapest and most certain first, so the reason a command is
#: refused is the most useful one available rather than whichever happened to match.
RULES = (
    ("oversize", oversize),
    ("heredoc_in_powershell", heredoc_in_powershell),
    ("cross_shell_expansion", cross_shell_expansion),
)

_WRITE_TOOL_ESCAPE = """\
The legal way to do this, and it is the rule being enforced:
  1. Create the file with the **Write tool** -- not `cat <<EOF`, not `echo >`, not `Set-Content`.
  2. Run it with a short command:
       bash  /tmp/scratch/x.sh
       pwsh  -NoProfile -File C:/Temp/scratch/x.ps1
       python /tmp/scratch/x.py
Keep the file until the task is done, so a fix is an edit instead of a retype. Put it in a scratch
directory, never inside a repo working tree."""

REASONS = {
    "oversize": """\
Blocked: this Bash/PowerShell command is {size:,} characters, and a command that big does not reach
the shell intact.

This is not a style limit. Commands this large fail at the parser even when they parse CLEAN
when fed to `bash -n` as a file: the text is truncated in transport, so the shell reports an
unterminated quote or heredoc at the cut, usually hundreds of lines away from anything that looks
like a mistake. The content you are sending -- a PR
body, a spec, a script -- is lost with it.

{escape}""",
    "heredoc_in_powershell": """\
Blocked: a heredoc (`<<`) in a PowerShell command. PowerShell has no heredoc.

`<` is a reserved redirection operator there, so this dies at the parser -- "The '<' operator is
reserved for future use" / "Missing file specification after redirection operator" -- and the whole
payload goes with it.

{escape}

Then point the command at the file instead of piping it in:
    git commit -F C:/…/msg.txt        gh pr create --body-file C:/…/body.md

A heredoc in the **Bash** tool is correct shell and is not blocked.""",
    "cross_shell_expansion": """\
Blocked: this command hands text to a DIFFERENT shell, and this shell rewrites it first.

Bash substitutes `$name`, `${{…}}`, `$(…)` and backticks inside double quotes -- and PowerShell does
the same to `$_`, `$env:X` and `$null` -- BEFORE the other shell sees a single byte. A backslash
does not protect anything in PowerShell, and a backtick does not protect anything in Bash, so the
escape that looks right is usually the one that fails.

The dangerous case is not the one that errors. When the substitution happens to produce something
valid the command RUNS, with different meaning and plausible output: an
`Authorization: Bearer \\$TOKEN` header that arrived as `Bearer \\`, a
`Write-Output $env:CLAUDE_CODE_SESSION_ID` that printed `:CLAUDE_CODE_SESSION_ID`. Commands like
these run and are silently wrong, which is why this is refused before it runs.

{escape}

If you truly need this shell to interpolate a value, assign it in this command first
(`X=…` in Bash, `$x = …` in PowerShell) -- a name this command assigns is allowed through, because
that is a deliberate hand-off rather than an accident.""",
}


def refusal_text(rule, command):
    """The message for ``rule``. Every one of them names the Write-tool escape."""
    template = REASONS.get(rule)
    if not template:
        return "Blocked: " + rule + "\n\n" + _WRITE_TOOL_ESCAPE
    return template.format(size=len(command or ""), escape=_WRITE_TOOL_ESCAPE)


def should_block(tool_name, command):
    """``(rule_name, reason)`` if this call must be refused, else ``(None, None)``.

    Never raises. A predicate that blows up on a hostile string is a predicate with no opinion about
    it, and this hook's answer when it has no opinion is always *allow*.
    """
    if tool_name not in GUARDED_TOOLS or not isinstance(command, str) or not command:
        return None, None
    for name, predicate in RULES:
        # The pre-filter is skipped for `oversize`, which is a property of the whole string.
        if name != "oversize" and not _PREFILTER.search(command):
            continue
        try:
            hit = predicate(tool_name, command)
        except Exception:  # noqa: BLE001 - fail open, per rule, never for the whole call
            continue
        if hit:
            return name, refusal_text(name, command)
    return None, None


def decide(event):
    """One `PreToolUse` event -> ``(rule, reason)``. Tolerant of any shape it is handed."""
    if not isinstance(event, dict):
        return None, None
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None, None
    return should_block(event.get("tool_name"), tool_input.get("command"))


def main(argv=None, stdin=None, stderr=None, stdout=None):
    """Hook entrypoint, and a `check` subcommand that needs no session.

    With no argv this reads the event from stdin and returns 0 or 2. The whole read-and-decide path
    is wrapped: the only way out that is not ``return EXIT_ALLOW`` is a positive identification.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "check":
        return _cmd_check(argv[1:], stdout or sys.stdout)
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        rule, reason = decide(json.loads(raw)) if raw and raw.strip() else (None, None)
    except Exception:  # noqa: BLE001 - fail open by contract; see the module docstring
        return EXIT_ALLOW
    if not rule:
        return EXIT_ALLOW
    try:
        (stderr if stderr is not None else sys.stderr).write(reason + "\n")
    except Exception:  # noqa: BLE001 - a block we cannot explain is still a correct block
        pass
    return EXIT_BLOCK


def _cmd_check(argv, out):
    """`check --tool Bash --command "…"` — judge one command and print the verdict. Read-only,
    writes nothing, and is what `SCRIPT_FILE_GUARD_SETUP.md` verifies the install with."""
    parser = argparse.ArgumentParser(prog="script_file_guard.py check")
    parser.add_argument("--tool", default="Bash", choices=list(GUARDED_TOOLS))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--command", help="the command text to judge")
    group.add_argument("--command-file", help="read the command text from this file")
    args = parser.parse_args(argv)
    command = args.command
    if command is None:
        with open(args.command_file, "r", encoding="utf-8") as handle:
            command = handle.read()
    rule, reason = should_block(args.tool, command)
    if not rule:
        print(f"allowed: no rule matches this {args.tool} command "
              f"({len(command):,} characters)", file=out)
        return EXIT_ALLOW
    print(f"BLOCKED by {rule}\n\n{reason}", file=out)
    return EXIT_BLOCK


if __name__ == "__main__":
    raise SystemExit(main())
