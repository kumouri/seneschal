#!/usr/bin/env python3
"""CI's "Reference data check" — every tracked `.json` file in the repo parses. Stdlib only.

## Why the job validates EVERY tracked JSON file rather than a named one

The job used to validate exactly one hardcoded file (`references/autonomy-config.json`). A job whose
only subject is one named file has a failure mode that nothing in the file announces: delete or
rename the subject and the job either breaks or gets deleted along with it — and a CI job's *name* is
often a REQUIRED status check in branch protection, which a PR author cannot see from the workflow
file. Deleting that job would leave protection waiting forever on a check nothing can produce.

**The fix is generality**: the job validates that every tracked `.json` file parses, discovered fresh
on every run, so there is no single named file whose deletion can silently empty its scope. When the
last thing a CI job validates goes away, REPOINT the job — don't delete it.

## `JSONC_FILES` is the one exclusion, and it is reasoned, not convenient

`cockpit/web/tsconfig.json` may carry a `//` comment by design — valid TypeScript JSONC, which `tsc`
itself parses, and not strict JSON. That is a different, well-defined dialect, not a broken file, so
excluding it is not "make the file's problem go away." The set is explicit and checked by hand before
it grows.

Usage:
  python check_json_files.py             # report, exit 0 without --enforce
  python check_json_files.py --enforce   # exit 1 on any tracked .json that fails to parse
"""
from __future__ import annotations

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, SCRIPT_DIR)

import gate_git  # noqa: E402  — the working-tree enumeration every local gate shares

# TypeScript JSONC, not broken JSON — see the module docstring. Nothing else belongs here.
JSONC_FILES = {
    "cockpit/web/tsconfig.json",
}


def tracked_json_files(root: str) -> list[str]:
    """Every `*.json` in the WORKING TREE — tracked plus untracked-not-ignored
    (`gate_git.working_tree_paths`); identical to `git ls-files` on a clean checkout. A gitignored
    host-local file is still never policed."""
    return sorted(gate_git.working_tree_paths(root, "*.json"))


def scan(root: str | None = None) -> dict:
    root = root or REPO_ROOT
    findings = []
    files_checked = 0
    for rel in tracked_json_files(root):
        if rel.replace(os.sep, "/") in JSONC_FILES:
            continue
        path = os.path.join(root, rel.replace("/", os.sep))
        try:
            with open(path, encoding="utf-8") as fh:
                json.load(fh)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            findings.append({"file": rel, "error": str(exc)})
            continue
        files_checked += 1
    return {"files_checked": files_checked, "findings": findings}


def render(result: dict) -> str:
    lines = [f"reference-data-check: {result['files_checked']} tracked .json file(s) parsed OK"]
    if not result["findings"]:
        return "\n".join(lines)
    lines.append(f"\n{len(result['findings'])} tracked .json file(s) FAILED to parse:")
    for f in result["findings"]:
        lines.append(f"  {f['file']}: {f['error']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='CI\'s "Reference data check" — every tracked .json '
                                             "file parses.")
    p.add_argument("--root", default=REPO_ROOT)
    p.add_argument("--enforce", action="store_true",
                   help="exit 1 on any unparseable tracked .json file (default: report-only, exit 0)")
    args = p.parse_args(argv)

    result = scan(args.root)
    print(render(result))
    n = len(result["findings"])
    if n and not args.enforce:
        print(f"\n  [report-only] {n} unparseable file(s) found, exit 0 by design.")
    return 1 if (args.enforce and n) else 0


if __name__ == "__main__":
    sys.exit(main())
