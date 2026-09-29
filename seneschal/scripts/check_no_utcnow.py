#!/usr/bin/env python3
"""Check — flag `.utcnow(` (covers both `datetime.utcnow(` and a bound `dt.utcnow(`) in tracked
Python. **Report-only, always** — never exits non-zero. Stdlib only.

`datetime.utcnow` is deprecated since 3.12 (naive, no tzinfo), and a naive "UTC" instant is how a
timezone conversion ends up hand-rolled beside every caller instead of going through one shared clock:
each module that reaches for it needs its own owner-local conversion next to it. `clock.now_utc()`
(aware) plus `clock.to_local()` / `tz_common` for the owner's zone is the replacement. Report-only,
the same adoption ladder every other check here uses: a gate red the day it merges gets disabled.

Usage:
  python check_no_utcnow.py             # print findings, exit 0 always
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gate_git  # noqa: E402  — the working-tree enumeration every local gate shares

_PATTERN = re.compile(r"\.utcnow\s*\(")


def repo_root() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def tracked_python(root: str) -> list[str]:
    """Every `*.py` in the WORKING TREE — tracked plus untracked-not-ignored
    (`gate_git.working_tree_paths`); identical to `git ls-files` on a clean checkout."""
    return sorted(gate_git.working_tree_paths(root, "*.py"))


def scan_source(src: str, rel: str) -> list[tuple[str, int, str]]:
    """`(path, line, source-excerpt)` for every `.utcnow(` in one file's text. Pure — a plain text
    scan, deliberately, since a check this small does not buy an AST pass; it will count a docstring
    or a comment mentioning the pattern too (this module's own included), which is an accepted
    false-positive class for a report-only line-count."""
    return [(rel, i, line.strip()[:120])
            for i, line in enumerate(src.replace("\r\n", "\n").split("\n"), start=1)
            if _PATTERN.search(line)]


def scan(root: str) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for rel in tracked_python(root):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_source(src, rel))
    return findings


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Report-only check for .utcnow( in tracked Python.")
    p.add_argument("--root", default=repo_root())
    args = p.parse_args(argv)

    findings = scan(args.root)
    print(f".utcnow( in tracked Python (report-only): {len(findings)}")
    for rel, line, excerpt in sorted(findings):
        print(f"  {rel}:{line}  {excerpt}")
    if findings:
        print("\n  Use clock.now_utc() instead. .utcnow() is deprecated (naive, no tzinfo), and a naive "
              "UTC instant\n  is how per-caller timezone conversions get hand-rolled. Report-only: "
              "this never fails CI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
