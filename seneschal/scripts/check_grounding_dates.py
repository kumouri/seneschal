#!/usr/bin/env python3
"""The dates-in-grounding lint — REPORT-ONLY, structurally, and it stays that way. Stdlib only.

## What this is for

A tier A/B file (the always- or often-loaded prompt-side grounding — `../context-budget.json`'s own
tier A/B artifact list is the scope, reused rather than re-derived so the two checks cannot disagree
about which files count) should state a rule as an imperative plus a `../docs/rulings.md` id, not as
narrative carrying its own bare date. A raw `YYYY-MM-DD` outside a ruling id is narrative that belongs
in the ledger row — the incident story, the "why", the thing a reader looks up only when they want to
argue with the rule — rather than in startup context every run pays for.

## What counts as "inside a ruling reference"

A date immediately followed by `-` and a lowercase-alphanumeric slug character — i.e. it matches the
head of a `rulings.md` id (`YYYY-MM-DD-kebab-slug`). That is a mechanical, cheap test: it does not
verify the id actually resolves to a real ledger row (that is `check_rulings.py`'s job), only that the
date is being used AS an id rather than as a bare narrative date. Every other `YYYY-MM-DD` — including
one immediately followed by a space, a closing paren, a comma, or nothing at all — is flagged.

## Why this is report-only, on purpose

**There is no `--enforce` flag anywhere in this module, and none should be added** without a sweep
behind it that moves the existing dated narrative out first. Enforcing it against a corpus that still
carries dated narrative would either block every PR touching a grounding file or get disabled on day
one — this repo's own named failure mode for a gate that goes red the moment it lands. It prints the
count so the number is visible and can only be driven down deliberately.

## What it deliberately does not check

- **Whether the file HAS a corresponding ledger row** — `check_rulings.py` owns that, off the diff, not
  the tree.
- **Dates inside a fenced code block, a table cell showing a literal timestamp format, or a quoted
  incident string that itself belongs in a ledger row already** — a lint this coarse cannot tell those
  apart from an undated narrative date, and is not trying to; it reports every raw date and leaves the
  judgment about which are already accounted for to whoever reads the report.

USAGE:
  python check_grounding_dates.py          # report + exit 0, always
  python check_grounding_dates.py --json   # machine-readable result
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
BUDGET_PATH = os.path.join(REPO_ROOT, "seneschal", "context-budget.json")

#: A bare date, NOT immediately followed by the slug half of a ruling id (`-word`). The lookahead is
#: the whole mechanism: `2026-01-15-example-slug` is exempt, `2026-01-15` alone (or followed by
#: anything but `-[a-z0-9]`) is flagged.
BARE_DATE_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b(?!-[a-z0-9])")


def tier_ab_paths(budget_path: str = BUDGET_PATH) -> list:
    """Repo-relative, file-based tier A/B artifacts from `context-budget.json`. A
    `presence.py::GROUNDING_TEMPLATE`-shaped entry is a string constant inside a `.py`, not a file on
    its own, and is skipped — a known, named gap rather than an attempt to parse Python
    source for embedded string literals here."""
    try:
        with open(budget_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    paths = []
    for artifact, entry in data.get("budgets", {}).items():
        if not isinstance(entry, dict) or entry.get("tier") not in ("A", "B"):
            continue
        if "::" in artifact:
            continue
        paths.append(artifact)
    return sorted(paths)


def scan(root: str | None = None) -> dict:
    root = root or REPO_ROOT
    findings = []
    paths = tier_ab_paths(os.path.join(root, "seneschal", "context-budget.json"))
    for rel in paths:
        full = os.path.join(root, rel.replace("/", os.sep))
        try:
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        for lineno, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
            for m in BARE_DATE_RE.finditer(line):
                findings.append({"path": rel, "line": lineno, "date": m.group(1)})
    return {"root": root, "scanned": len(paths), "findings": findings}


def render(result: dict, out=None) -> None:
    out = out or sys.stdout
    out.write("check_grounding_dates: %d tier A/B file(s) scanned, %d bare date(s)\n"
               % (result["scanned"], len(result["findings"])))
    by_path: dict = {}
    for f in result["findings"]:
        by_path.setdefault(f["path"], []).append(f)
    for path, rows in sorted(by_path.items()):
        out.write("  %s: %d\n" % (path, len(rows)))
    out.write("\n  [report-only, always — this module has no --enforce flag by design]\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", default=REPO_ROOT)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    result = scan(args.root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
