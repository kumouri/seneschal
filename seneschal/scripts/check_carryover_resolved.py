#!/usr/bin/env python3
"""The carry-over resolved-item guard. Stdlib only.

## What this is for

`state/carry-over.md` is rebuilt each EOD Wrap (`../references/memory.md`: resolved items drop off).
In practice a file like this does not get rebuilt clean: it accretes a persistent resolved-items table
alongside a "waiting on the owner" summary block, and nothing re-reads the summary against the table.
That lets a stale line survive for days of Briefs — a summary line near the top reading
`PR #123 green/unmerged ...` while the same file, hundreds of lines lower, carries
`| ~~PR #123~~ | **RESOLVED.** #122 and #123 were both closed and superseded by #124 ... |`. The file
has already recorded its own refutation, and nobody reads it against itself.

Two representations of the same fact, one stale, and a prose rule ("resolved items drop off") that
does not enforce itself. The fix is a check, not a longer sentence in `memory.md`: a written
correction that nothing runs does not stay corrected.

## Why this cannot run in CI

`seneschal/state/carry-over.md` is gitignored — it exists only on the daemon's checkout, never in a PR
diff, so no CI job can ever see a real one. **This is the load-bearing constraint on this module's
design**: it is built to be run by the EOD Wrap against its own freshly-rebuilt draft, immediately
before that draft is written (`../../subagents/eod-wrap/SKILL.md` step 4) — the one place that both
produces the content and can still change it before it lands. Report-only for now, per this repo's
adoption pattern: there is no fire log yet to say what the false-positive rate looks like against a
real file's shape, and a check that blocks before it is measured is a check someone disables.

## What counts as a finding

A **resolved declaration**: a line carrying both a struck-through span naming a PR (`~~PR #123~~` or
`~~#123~~`) and the word RESOLVED elsewhere on that same line — the exact shape above. A **stale
mention**: any OTHER line in the file that still names that PR number outside of a struck-through
span. One resolved declaration silences every later re-mention of the same number; it does not
silence a *different* PR discussed nearby (`#122`/`#124` in the exhibit above are correctly left
alone — they were never struck through).

This is deliberately narrow — PR references only, not every kind of open-loop item the file tracks —
because a checker that tries to infer "resolved" from prose alone is the shape
`check_context_pointers.py`'s own docstring warns against: a naive recognizer over free text is a
wall, not a check.

USAGE:
  python check_carryover_resolved.py [path]             # report, exit 0 (default: seneschal/state/carry-over.md)
  python check_carryover_resolved.py [path] --enforce    # exit 1 on any finding
"""
from __future__ import annotations

import argparse
import re
import sys

STRIKETHROUGH_RE = re.compile(r"~~(.+?)~~")
PR_REF_RE = re.compile(r"\bPR\s*#(\d+)\b|(?<!\w)#(\d+)\b", re.IGNORECASE)
RESOLVED_WORD_RE = re.compile(r"\bRESOLVED\b", re.IGNORECASE)


class Finding:
    __slots__ = ("pr", "resolved_line", "stale_line", "stale_text")

    def __init__(self, pr: str, resolved_line: int, stale_line: int, stale_text: str):
        self.pr = pr
        self.resolved_line = resolved_line
        self.stale_line = stale_line
        self.stale_text = stale_text

    def __str__(self) -> str:
        return (f"PR #{self.pr} marked RESOLVED at line {self.resolved_line}, but still referenced "
                f"as open at line {self.stale_line}: {self.stale_text}")


def _pr_numbers(text: str) -> set:
    return {m.group(1) or m.group(2) for m in PR_REF_RE.finditer(text)}


def find_resolved(lines: list) -> dict:
    """PR number -> 1-based line of its struck-through RESOLVED declaration.

    The first such declaration wins if a PR is somehow struck through twice; a second row could only
    ever be more evidence of resolution, not less."""
    resolved = {}
    for i, line in enumerate(lines, 1):
        if not RESOLVED_WORD_RE.search(line):
            continue
        for m in STRIKETHROUGH_RE.finditer(line):
            for pr in _pr_numbers(m.group(1)):
                resolved.setdefault(pr, i)
    return resolved


def find_stale_mentions(lines: list, resolved: dict) -> list:
    """Every line naming a resolved PR outside of a struck-through span and outside the declaration
    line itself (whose own prose, e.g. "#122 and #123 were both closed", legitimately repeats the
    number in the row that resolved it)."""
    findings = []
    for i, line in enumerate(lines, 1):
        unstruck = STRIKETHROUGH_RE.sub("", line)
        for m in PR_REF_RE.finditer(unstruck):
            pr = m.group(1) or m.group(2)
            if pr not in resolved or i == resolved[pr]:
                continue
            findings.append(Finding(pr, resolved[pr], i, line.strip()[:160]))
    return findings


def scan(text: str) -> list:
    """Pure — the tests drive this directly."""
    lines = text.replace("\r\n", "\n").split("\n")
    resolved = find_resolved(lines)
    if not resolved:
        return []
    return find_stale_mentions(lines, resolved)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=(
        "Flags a PR still cited as open in state/carry-over.md after the same file marks it "
        "~~struck-through~~ RESOLVED. Run this against the Wrap's freshly-rebuilt draft before "
        "writing it -- carry-over.md is gitignored, so CI never sees a real one."))
    p.add_argument("path", nargs="?", default="seneschal/state/carry-over.md")
    p.add_argument("--enforce", action="store_true", help="exit 1 on any finding (report-only otherwise)")
    args = p.parse_args(argv)

    try:
        with open(args.path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        print(f"{args.path}: not present (bootstrap-on-absence; nothing to check)")
        return 0

    findings = scan(text)
    if not findings:
        print(f"{args.path}: no stale references to a resolved item.")
        return 0

    print(f"{args.path}: {len(findings)} stale reference(s) to an item this file itself marks RESOLVED:")
    for f in findings:
        print(f"  {f}")
    if not args.enforce:
        print("\n  [report-only] pass --enforce to fail on these.")
    return 1 if args.enforce else 0


if __name__ == "__main__":
    sys.exit(main())
