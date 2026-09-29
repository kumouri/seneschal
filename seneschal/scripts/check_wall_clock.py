#!/usr/bin/env python3
"""Check — a wall-clock read with no explicit zone, inside the tree's own checkers, tests and CI
workflow. Stdlib only. Built in the `check_no_utcnow.py`/`check_state_writes.py` shape.

## What this is for

Code that reads the wall clock live and implicitly trusts whichever zone the PROCESS happens to be in
fails in a specific, recurring way — a test or checker that passes on the developer's host and fails
on CI's UTC runner, or passes today and fails at midnight:

1. **A test that freezes `TODAY` at import** while the code under test reads the clock live through a
   `now=None` default. A run that straddles midnight between import and the call seeds today and then
   asks about *tomorrow* — tests fail on a change that touched none of it.
2. **A checker that hands git a bare `--until=<date> 23:59:59`.** Git reads it in the PROCESS's local
   zone, so the same tree walks a different set of commits on a host behind UTC than on a UTC runner,
   and a number computed on one does not reproduce on the other.

**None of these is a tzdata problem.** Every one of them is a function that should be pure — reading
the wall clock live instead of the instant it was actually handed, or handing git a date with no zone
and trusting the reader's own clock to agree with the writer's. This is the gate.

## What it flags

Inside the scope below, an AST walk over each Python file's `Call` nodes flags:

* **`date.today()`** (also the two-hop `datetime.date.today()`) — matched on the callee's attribute
  chain ending in `.date.today` or a bare `date.today`, not on an import alias, so `dt.date.today()`
  and `datetime.date.today()` both match.
* **a NAIVE `datetime.now()`** — `.now()` on anything whose attribute chain ends in `datetime`, with
  ZERO arguments. Any argument (positional or `tz=`) is trusted to be a timezone — `datetime.now(tz)`
  is exactly the fix, not the bug.
* **`time.localtime()`** — bare, zero arguments. `time.localtime(some_epoch)` converts a GIVEN
  instant and is not a live read.

A separate, narrower scan (not AST — see "Known narrowing" below) flags a **git date-window flag
with no explicit offset**: a string or f-string that is an ELEMENT of a `list`/`tuple` LITERAL
(the argv shape every git call in this tree uses — see `gate_git.py`, `check_context_budget.py`)
and starts with `--since=` or `--until=`, whose STATIC text (the literal parts around any `{...}`
interpolation) carries no `+HH:MM`/`-HH:MM` offset and no trailing `Z`. `f"--until={until}T23:59:59
+00:00"` — `check_context_budget.py`'s own shape — is NOT flagged: its static suffix names the offset
even though `until` itself is a variable. The equivalent workflow-file (bash) form is scanned the same
way, as plain text, over `.github/workflows/ci.yml`.

## Scope — WHY these files and no others

`check_*.py`, `test_*.py` (repo-wide — the cockpit's own `test_*.py` included, not just
`seneschal/scripts`), `count_tests.py`, `ci_local.py`, and `.github/workflows/ci.yml` itself. This is
the exact set where the failure above bites: CI-side checkers, the suite CI runs, and the workflow that
invokes both.

**Owner-local reads are the daemon's own business and are deliberately never in this set** —
`clock.py`, `tz_common.py`, `activity_day.py`, `sentinel.py`, `reminders_*.py`, `presence.py` and the
rest of the reminder/activity-day machinery read an explicit aware instant and THEN convert
(`clock.to_local` / `tz_common`), which is a different shape from the patterns above. Scoping by FILE
IDENTITY rather than by suppressing the pattern inside those files means the gate cannot quietly grow
to cover them by a widened glob later — a new `check_*.py`/`test_*.py` is in scope from the moment it
exists; `presence.py` never is, no matter how its clock reads change.

## Known narrowing, stated rather than hidden

The git-window scan only matches the **`--since=`/`--until=` (single string, `=`-joined) shape**,
because that is the shape this tree's git calls use (`check_context_budget.py`'s `args = [...,
f"--since=...", f"--until=..."]`) — and, just as important, it is what tells that shape apart from an
UNRELATED `--since`/`--until` flag a script's OWN CLI defines, which its tests pass as SEPARATE list
elements (`["--since", "2026-01-01", "--until", ...]`) to that script's own `main()` — nothing to do
with git. A future git call using the separate-argument form (`["--since", value]`) would not be caught
here; that is accepted the same way `check_no_utcnow.py`'s plain-text scan accepts counting a docstring.

## Allowlist

`seneschal/wall-clock-allowlist.json`, `reason` required per entry, matched on `(path, symbol, kind)`
— not a line number, so an edit above a call site doesn't silently un-allow it. An entry is DEBT MADE
VISIBLE, not a quiet exemption; see `check_state_writes.py`'s own allowlist for the same convention.
The legitimate class is a test whose SUBJECT is the machine-local clock (a documented fallback to it).

Usage:
  python check_wall_clock.py             # report, exit 0
  python check_wall_clock.py --enforce   # exit 1 on any un-allowlisted finding (this is what CI runs)
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gate_git  # noqa: E402  — the working-tree enumeration every local gate shares

ALLOWLIST_FILE = os.path.join("seneschal", "wall-clock-allowlist.json")
CI_WORKFLOW_REL = ".github/workflows/ci.yml"

KIND_DATE_TODAY = "date-today"
KIND_NAIVE_NOW = "naive-now"
KIND_LOCALTIME = "localtime"
KIND_GIT_WINDOW = "git-window"

#: A `--since=`/`--until=` git date-window flag, `=`-joined — see "Known narrowing" above for why the
#: separate-argument form (`["--since", value]`) is deliberately not matched.
GIT_WINDOW_PREFIX = re.compile(r"^--(since|until)=")

#: An explicit UTC/zone offset in the STATIC text around an interpolation: `+00:00`, `-05:00`, or a
#: trailing `Z` (quoted or at end of string) — the shape `check_context_budget.py`'s window uses.
EXPLICIT_OFFSET = re.compile(r"[+-]\d{2}:\d{2}|Z(?=[\"'\s]|$)")

#: The in-scope file set, as `gate_git` pathspecs. **`:(glob)**/` is required, not decorative** —
#: three shapes were measured while building this gate and only this one matches "any depth,
#: including zero":
#:   * a pattern that is ENTIRELY a wildcard (`check_no_utcnow.tracked_python`'s `"*.py"`) matches at
#:     any depth because `*` alone absorbs the leading path too — but a literal prefix before the
#:     wildcard, `"check_*.py"`, anchors to the pathspec's own root and matches ONLY a file directly
#:     there. `git ls-files -- 'check_*.py'` on this repo returns NOTHING, silently — every file
#:     this gate needs lives under a subdirectory.
#:   * bare `"**/check_*.py"` (no glob magic) fixes the subdirectory case but then matches ONLY a
#:     directory depth of one or more — a hypothetical root-level `check_*.py` would go unscanned.
#:   * `:(glob)**/check_*.py` is the one shape that matches BOTH: `:(glob)` gives `**` real glob
#:     semantics (zero or more path components), verified against a throwaway repo with one fixture
#:     at each depth before landing on it.
SCOPE_PATTERNS = (":(glob)**/check_*.py", ":(glob)**/test_*.py",
                   ":(glob)**/count_tests.py", ":(glob)**/ci_local.py")


class Finding:
    __slots__ = ("path", "line", "kind", "symbol", "source")

    def __init__(self, path, line, kind, symbol, source):
        self.path, self.line, self.kind = path, line, kind
        self.symbol, self.source = symbol, source

    def key(self):
        return (self.path.replace("\\", "/"), self.symbol or "", self.kind)

    def __str__(self):
        where = f"{self.path}:{self.line}"
        sym = self.symbol or "<module>"
        return f"{where}  [{self.kind}] in {sym}(): {self.source}"


def _flatten(node) -> str:
    """Dotted-name text for a `Name`/`Attribute` chain — `"dt.date"` for `dt.date.today`'s callee
    value. Anything else (a call, a subscript, ...) resolves to `""`, which matches no pattern."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _flatten(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _static_text(node) -> str | None:
    """The literal text of a `Constant` string, or the CONSTANT parts of an f-string joined in order
    (interpolations skipped) — enough to see a static offset suffix like `+00:00` even when the value
    itself is computed. `None` for anything that isn't a string-shaped literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    return None


def _function_map(tree: ast.Module) -> dict:
    """node id -> nearest enclosing function name; absent means module level."""
    out = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for child in ast.walk(fn):
            out[id(child)] = fn.name
    return out


def scan_source(src: str, rel: str) -> list[Finding]:
    """Every finding in one module's source. Pure — the tests drive this directly."""
    tree = ast.parse(src, filename=rel)
    fn_of = _function_map(tree)
    lines = src.replace("\r\n", "\n").split("\n")

    def excerpt(lineno: int) -> str:
        return lines[lineno - 1].strip()[:120] if 0 < lineno <= len(lines) else ""

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = _flatten(node.func.value)
            last = base.rsplit(".", 1)[-1] if base else ""
            attr = node.func.attr
            bare = not node.args and not node.keywords
            if attr == "today" and last == "date":
                findings.append(Finding(rel, node.lineno, KIND_DATE_TODAY,
                                         fn_of.get(id(node), ""), excerpt(node.lineno)))
            elif attr == "now" and last == "datetime" and bare:
                findings.append(Finding(rel, node.lineno, KIND_NAIVE_NOW,
                                         fn_of.get(id(node), ""), excerpt(node.lineno)))
            elif attr == "localtime" and last == "time" and bare:
                findings.append(Finding(rel, node.lineno, KIND_LOCALTIME,
                                         fn_of.get(id(node), ""), excerpt(node.lineno)))
        elif isinstance(node, (ast.List, ast.Tuple)):
            for elt in node.elts:
                text = _static_text(elt)
                if text is None or not GIT_WINDOW_PREFIX.match(text):
                    continue
                if not EXPLICIT_OFFSET.search(text):
                    findings.append(Finding(rel, elt.lineno, KIND_GIT_WINDOW,
                                             fn_of.get(id(node), ""), excerpt(elt.lineno)))
    return findings


def in_scope_paths(root: str) -> list[str]:
    """`check_*.py`, `test_*.py` (repo-wide), `count_tests.py`, `ci_local.py` — every path that
    exists in the WORKING TREE (`gate_git.working_tree_paths`), so a
    not-yet-`git add`ed module is scanned locally before it is committed, same as every other gate
    here. See the module docstring's "Scope" section for why this list and not a wider one."""
    return sorted(gate_git.working_tree_paths(root, *SCOPE_PATTERNS))


def scan_ci_workflow(root: str) -> list[Finding]:
    """Plain-text scan of `.github/workflows/ci.yml` for the same `--since=`/`--until=` shape, since
    a workflow step is bash, not Python, and has no AST here to walk. Same offset rule as
    `scan_source`'s git-window check."""
    path = os.path.join(root, *CI_WORKFLOW_REL.split("/"))
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    findings: list[Finding] = []
    for i, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        for m in re.finditer(r"--(?:since|until)=\S+", line):
            if not EXPLICIT_OFFSET.search(m.group(0)):
                findings.append(Finding(CI_WORKFLOW_REL, i, KIND_GIT_WINDOW, "", line.strip()[:160]))
    return findings


def scan(root: str) -> list[Finding]:
    findings: list[Finding] = []
    for rel in in_scope_paths(root):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            findings.extend(scan_source(src, rel))
        except SyntaxError:
            continue  # the `python-syntax` job in CI owns this failure, not us
    findings.extend(scan_ci_workflow(root))
    return findings


def load_allowlist(root: str) -> dict:
    """`{(path, symbol, kind): reason}`. A missing file is an empty allowlist, not an error — the
    check must work in a checkout that has not got one yet."""
    try:
        with open(os.path.join(root, ALLOWLIST_FILE), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    out = {}
    for entry in raw.get("allow", []):
        reason = str(entry.get("reason") or "").strip()
        if not reason:
            raise ValueError(f"{ALLOWLIST_FILE}: entry {entry!r} has no `reason`. "
                              "Debt is allowed here; unexplained debt is not.")
        key = (entry["path"].replace("\\", "/"), entry.get("symbol") or "", entry["kind"])
        out[key] = reason
    return out


def repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", ".."))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Check — a wall-clock read with no explicit zone, inside checkers/tests/CI.")
    p.add_argument("--root", default=repo_root())
    p.add_argument("--enforce", action="store_true", help="exit 1 on any un-allowlisted finding")
    args = p.parse_args(argv)

    findings = scan(args.root)
    allow = load_allowlist(args.root)

    live = [f for f in findings if f.key() not in allow]
    excused = [f for f in findings if f.key() in allow]

    print(f"wall-clock read audit over checkers/tests/CI (root: {args.root})")
    print(f"  bare wall-clock reads : {len(findings)}   "
          f"[{len(excused)} allowlisted, {len(live)} live]")

    if excused:
        print("\n-- allowlisted (visible debt) --")
        for f in sorted(excused, key=lambda f: (f.path, f.line)):
            print(f"  {f}\n      reason: {allow[f.key()]}")
    if live:
        print("\n-- BARE WALL-CLOCK READS --")
        for f in sorted(live, key=lambda f: (f.path, f.line)):
            print(f"  {f}")
        print("\n  Each of these reads the wall clock with no explicit zone: `date.today()`, a naive"
              "\n  `datetime.now()`, `time.localtime()`, or a git `--since=`/`--until=` window with no"
              "\n  explicit offset. Use `datetime.now(timezone.utc)`, an injected clock parameter, or a"
              "\n  frozen instant in a test — or add a row with a `reason` to " + ALLOWLIST_FILE + "."
              "\n  Why this is a blocking check: a test or checker that reads the process's own zone"
              " passes on one host and fails on another — see this module's docstring.")
    if live and not args.enforce:
        print(f"\n  [report-only] {len(live)} finding(s); pass --enforce to fail on them.")

    return 1 if (args.enforce and live) else 0


if __name__ == "__main__":
    sys.exit(main())
