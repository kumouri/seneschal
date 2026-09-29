#!/usr/bin/env python3
"""check_docs.py — ONE command that runs every doc-side CI gate and reports every violation in a
single pass. Stdlib only. Built in the shape of `check_doc_status.py`, deliberately.

## What this is for

A failing gate in CI's one job hides the ones after it in the same step, so a docs-only change can go
red three separate times, each time on a different gate — three round-trips, none of them about what
the document actually said. This runs every doc-side gate the repo has, unconditionally, and prints
every gate's own findings together — so a document author sees everything that will object on the
first pass, not one visit at a time.

## Known tripwires — the three gates a docs-only change most often trips

1. **`check_doc_status.py`** — the header status token must be exactly one of `BUILT` / `PARTIAL` /
   `SPEC-ONLY` / `MEMO` / `REFERENCE`, backticked: `` **Status:** `SPEC-ONLY` ``. What trips it: a
   free-text status such as `SPEC — UNBUILT.` is not one of the five and counts as unclassified. Do
   instead: use one of the five tokens (`SPEC-ONLY` is "a plan of record, nothing built").
2. **`check_context_pointers.py`** — a backticked, slash-bearing, path-shaped code span reads as a
   pointer to resolve. What trips it: `` `multipart/form-data` `` is path-shaped (a slash, no spaces)
   and is not a repo path, so it is reported dangling. Do instead: drop the backticks around a MIME
   type or similar slash-bearing non-path token, or allowlist it in `seneschal/context-pointers.json`
   with a `reason`.
3. **`check_rulings.py`** — an added line matching `\\bruling\\b`/`\\bruled\\b` (case-insensitive)
   demands a row in `seneschal/docs/rulings.md` in the same diff. What trips it: "...without a
   ruling" describes the ABSENCE of a decision, not a decision that was made. Do instead: say what is
   actually meant (e.g. "the owner's explicit approval"), or if the word is truly incidental, mark the
   line `<!-- check-rulings: not-a-ruling — <reason> -->`.

## What this deliberately does not do

- **Never reimplements a gate.** Every gate below is invoked through its own `main()`; duplicating any
  of their logic would recreate exactly the drift the gates exist to catch.
- **Never promotes a report-only gate to a failure.** `check_no_utcnow.py` and
  `check_grounding_dates.py` have no `--enforce` at all, by design, and `check_context_budget.py`'s
  byte-budget half stays report-only even under `--enforce-headroom`. This script runs each gate's own
  CI invocation unmodified and reports its own exit code — never recomputing pass/fail itself.
- **Never touches `.github/workflows/ci.yml`.** CI keeps its separate steps; this is the local
  one-pass view of the doc-side ones.

## The list, and why it's hardcoded rather than derived

`GATES` below is a literal copy of `ci.yml`'s doc-side invocations, not parsed out of the workflow
file: this repo has no YAML parser in its stdlib-only dependency set, and "is this a doc-side check"
is not a syntactic category a script can recognise on its own — `check_json_files.py` is also a
`check_*.py` script invoked from `ci.yml`, but it validates JSON syntax, not whether a document
declares itself correctly, which is the shape all three tripwires above share.

So the safety net is a **test**, not a parser: `test_check_docs.py`'s `WorkflowCoverageTest` scans
`ci.yml` for every `seneschal/scripts/check_*.py` invocation and fails if one appears that is neither
in `GATES` below nor in `EXCLUDED_FROM_CI_AGGREGATION` (with a reason) — so a new gate added to CI
without a matching update here cannot pass silently. It has to be triaged into one list or the other.

USAGE:
  python check_docs.py                        # run every gate, exit non-zero iff any gate failed
  python check_docs.py --json                  # machine-readable result
  python check_docs.py --only check_rulings    # run a single named gate
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
CI_WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")

if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

#: Every doc-side gate, in the order `ci.yml`'s python job runs them. Each `argv` is copied VERBATIM
#: from `ci.yml` — see the module docstring's "why it's hardcoded" section.
GATES = (
    {"name": "check_context_budget", "argv": ["--enforce-headroom"]},
    {"name": "check_context_pointers", "argv": ["--enforce"]},
    {"name": "check_doc_status", "argv": ["--enforce"]},
    {"name": "check_state_writes", "argv": ["--enforce"]},
    {"name": "check_no_utcnow", "argv": []},
    {"name": "check_rulings", "argv": ["--enforce"]},
    {"name": "check_grounding_dates", "argv": []},
)

GATE_NAMES = tuple(g["name"] for g in GATES)

#: Every OTHER `check_*.py` script `ci.yml` invokes, and why it is not a doc-side gate this script
#: runs. `WorkflowCoverageTest` (`test_check_docs.py`) requires every check script named in `ci.yml`
#: to appear either here or in `GATES` — an entry here is a triaged decision, not an oversight.
EXCLUDED_FROM_CI_AGGREGATION = {
    "check_context_stores": (
        "source-to-projection-to-consumer store-pairing check (`--venue ci`) — verifies declared "
        "data-store pairs stay in sync, a different subject from whether a document declares "
        "itself correctly."),
    "check_json_files": (
        "validates every tracked JSON file parses; runs in ci.yml's separate 'Reference data check' "
        "job, and checks JSON syntax rather than documentation."),
    "check_wall_clock": (
        "flags a wall-clock read with no explicit zone inside checkers/tests/ci.yml itself — code "
        "and CI-workflow correctness, not document content."),
    "check_carryover_prose": (
        "scans a small named set of prose files for one narrow shape — naming carryover_region.py "
        "and never a raw memory_write.py write against carry-over.md — a single-purpose content "
        "check, not the doc-status/pointer/ruling family this aggregator covers."),
    "check_placeholders": (
        "the UUID placeholder guard — secrets/personal-data hygiene over every tracked file, not "
        "whether a document declares itself correctly."),
}

CHECK_SCRIPT_RE = re.compile(r"seneschal/scripts/(check_\w+)\.py")


def scripts_invoked_in_ci(text: str) -> set:
    """Every `check_*` script name (no `.py`) that `ci.yml` invokes via `seneschal/scripts/check_*.py`.

    A line scan over the workflow's raw text, not a YAML parse — see the module docstring's "why it's
    hardcoded" section for why this repo has no YAML parser to reach for, and why the judgment call
    (doc-side or not) can't be automated even if it did."""
    return set(CHECK_SCRIPT_RE.findall(text))


def _run_gate(name: str, argv: list, root: str) -> dict:
    """Invoke one gate's own `main()` in-process, its stdout/stderr captured. Never re-implements the
    gate's logic and never recomputes its verdict — the returned `exit_code` IS the gate's own."""
    module = importlib.import_module(name)
    full_argv = list(argv) + ["--root", root]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exit_code = module.main(full_argv)
    except SystemExit as exc:
        # Defensive only — no gate's main() calls sys.exit today (only their `__main__` guards do),
        # but a future gate doing so must not crash the aggregator, just be treated as its exit code.
        exit_code = exc.code if isinstance(exc.code, int) else 1
    return {"name": name, "argv": full_argv, "exit_code": exit_code,
            "stdout": out.getvalue(), "stderr": err.getvalue(), "ok": exit_code == 0}


def run(root: str | None = None, only: str | None = None) -> dict:
    """The one aggregator. Returns `{"root", "gates": [...], "failing": n}`. Every gate in `GATES`
    runs even when an earlier one fails — that is the entire reason this module exists."""
    root = root or REPO_ROOT
    gates = [g for g in GATES if g["name"] == only] if only else list(GATES)
    results = [_run_gate(g["name"], g["argv"], root) for g in gates]
    failing = sum(1 for r in results if not r["ok"])
    return {"root": root, "gates": results, "failing": failing}


def render(result: dict, out=None) -> None:
    out = out or sys.stdout
    for g in result["gates"]:
        out.write("=== %s (%s) ===\n" % (g["name"], " ".join(g["argv"]) or "(no flags)"))
        out.write(g["stdout"])
        if g["stdout"] and not g["stdout"].endswith("\n"):
            out.write("\n")
        if g["stderr"]:
            out.write("[stderr]\n%s" % g["stderr"])
            if not g["stderr"].endswith("\n"):
                out.write("\n")
        out.write("  exit %d — %s\n\n" % (g["exit_code"], "PASS" if g["ok"] else "FAIL"))
    out.write("%d gate(s) run · %d failing\n" % (len(result["gates"]), result["failing"]))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="check_docs.py",
        description="Run every doc-side CI gate in one pass and report every finding together — "
                    "see the module docstring's \"known tripwires\" for the three gates that used to "
                    "be discovered one at a time.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=REPO_ROOT)
    p.add_argument("--json", action="store_true", help="machine-readable result")
    p.add_argument("--only", choices=GATE_NAMES, default=None,
                   help="run a single named gate instead of all of them")
    args = p.parse_args(argv)

    result = run(args.root, args.only)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)
    return 1 if result["failing"] else 0


if __name__ == "__main__":
    sys.exit(main())
