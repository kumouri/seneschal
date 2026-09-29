#!/usr/bin/env python3
"""The carry-over-region prose guard. Stdlib only. Report-only (see below for why).

`carry-over.md` has exactly two writers, each owning one bounded, marked region: `carryover_region.py
write-region --name WRAP_SEED` and `loops.py render --write` (`../references/memory.md`). That only
holds if the prose telling a future run how to write the file actually says so — a rule that lives
only in a doc is a rule that does not run — and the failure this check refuses is a narrow, concrete
recurrence of exactly that: the Wrap's own instructions reverting to "compose the new block, then
`memory_write.py write` the whole file", which is indistinguishable at the byte level from the
prepend-and-keep growth the bounded regions replaced (the file grows by a full copy of itself every
night and nothing ever prunes it).

## Scope, deliberately narrow

Two checks, over a small, NAMED set of files — the operational prose that actually instructs a live
run, not every tracked `.md` file that happens to mention `carry-over.md` in passing:

1. **The instruction files** (`subagents/eod-wrap/SKILL.md`, `seneschal/modes/wrap.md`) must NAME
   `carryover_region.py` somewhere — the tool a live run is supposed to call.
2. **None of the NAMED files** (the two above, plus `seneschal/references/memory.md` and
   `seneschal/state/README.md`, the protocol/contract docs) may contain the old raw-write shape: the
   same line naming both `memory_write.py write` and `carry-over.md` — the command that composed
   "new block + entire old file" and handed it to `memory_write.py write`.

A file-scoped list rather than a repo-wide grep for the same reason `check_carryover_resolved.py`'s
own docstring gives for its narrowness: a checker that infers "this is an instruction to write
carry-over.md" from free text anywhere in the tree is a wall, not a check — this repo's own docs (this
module's docstring, `memory_write.py`'s general docs) legitimately discuss the old pattern BY NAME to
explain why it is wrong, and a repo-wide match would refuse the very documents describing the bug.

## Why report-only

No fire log yet against the real shape of these four files' future edits — the same adoption ladder
`check_carryover_resolved.py` and every other check in this tree ships under before `--enforce` is
even offered: a gate red on the day it lands is a gate someone disables.

USAGE:
  python check_carryover_prose.py             # report, exit 0
  python check_carryover_prose.py --enforce   # exit 1 on any finding
"""
from __future__ import annotations

import argparse
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))

#: The instruction files — must name carryover_region.py.
INSTRUCTION_FILES = (
    "subagents/eod-wrap/SKILL.md",
    "seneschal/modes/wrap.md",
)

#: Every file scanned for the raw-write shape — the instruction files plus the protocol/contract docs.
NAMED_FILES = INSTRUCTION_FILES + (
    "seneschal/references/memory.md",
    "seneschal/state/README.md",
)

TOOL_NAME_RE = re.compile(r"carryover_region\.py")
#: Same line naming BOTH a raw `memory_write.py write` verb AND the carry-over path — the exact shape
#: the bounded regions replaced. Deliberately per-line, not whole-file: the design docs and this
#: module's own docstring discuss `memory_write.py write ... carry-over.md` narratively without ever putting both
#: on one physical line, so a per-line match does not need an exemption list to stay quiet on them.
RAW_WRITE_RE = re.compile(
    r"memory_write\.py\s+write\b.*carry-over\.md|carry-over\.md.*memory_write\.py\s+write\b")


def _read(root: str, rel: str) -> str | None:
    path = os.path.join(root, rel.replace("/", os.sep))
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def find_raw_writes(text: str) -> list[tuple]:
    """`[(line_no, line_text)]` for every line matching the raw-write shape."""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if RAW_WRITE_RE.search(line):
            hits.append((i, line.strip()[:160]))
    return hits


def scan(root: str | None = None) -> dict:
    root = root or REPO_ROOT
    missing_tool_name = []
    raw_writes = []
    files_missing = []

    for rel in INSTRUCTION_FILES:
        text = _read(root, rel)
        if text is None:
            files_missing.append(rel)
            continue
        if not TOOL_NAME_RE.search(text):
            missing_tool_name.append(rel)

    for rel in NAMED_FILES:
        text = _read(root, rel)
        if text is None:
            if rel not in files_missing:
                files_missing.append(rel)
            continue
        for line_no, line_text in find_raw_writes(text):
            raw_writes.append({"file": rel, "line": line_no, "text": line_text})

    return {"missing_tool_name": missing_tool_name, "raw_writes": raw_writes,
            "files_missing": files_missing}


def render(result: dict) -> str:
    lines = ["carryover-prose: instruction files must name carryover_region.py; "
            "no named file may raw-write carry-over.md via memory_write.py write"]
    if result["files_missing"]:
        lines.append(f"  {len(result['files_missing'])} named file(s) not found (nothing to check): "
                    + ", ".join(result["files_missing"]))
    if not result["missing_tool_name"] and not result["raw_writes"]:
        lines.append("  no findings.")
        return "\n".join(lines)
    for rel in result["missing_tool_name"]:
        lines.append(f"\n{rel}: does not mention carryover_region.py — a live Wrap run would have "
                    f"no instruction to use the bounded-region tool.")
    for f in result["raw_writes"]:
        lines.append(f"\n{f['file']}:{f['line']}: raw `memory_write.py write` on carry-over.md:")
        lines.append(f"    {f['text']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=(
        "Checks that the Wrap's own prose names carryover_region.py and that no named prose file "
        "still instructs a raw memory_write.py write against carry-over.md."))
    p.add_argument("--root", default=REPO_ROOT)
    p.add_argument("--enforce", action="store_true", help="exit 1 on any finding (report-only otherwise)")
    args = p.parse_args(argv)

    result = scan(args.root)
    print(render(result))
    n = len(result["missing_tool_name"]) + len(result["raw_writes"])
    if n and not args.enforce:
        print(f"\n  [report-only] {n} finding(s); pass --enforce to fail on them.")
    return 1 if (args.enforce and n) else 0


if __name__ == "__main__":
    sys.exit(main())
