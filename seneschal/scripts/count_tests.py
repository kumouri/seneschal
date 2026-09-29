#!/usr/bin/env python3
"""Measure the test suites, so that no tracked file has to carry the number.

**Why this exists.** A suite total written in prose in a router (the root `CLAUDE.md`'s CI
paragraph, the README's) has to be hand-edited by every PR that adds a test — so any two PRs in
flight both rewrite the same line and collide by construction, and each collision costs a
rebase. The number is now nobody's to type. CI runs this script every run and publishes what it
measures to the job summary; the prose points here instead of restating it.

**Measurement, never arithmetic.** `unittest`'s loader can COUNT a suite without RUNNING it —
`discover()` imports the test modules and instantiates the cases, and this walks the resulting tree.
The stdlib suite counts in ~2 s, against ~4 min to run it, so the number is cheap enough to re-derive
on every CI run and never has to be carried forward, added to, or reconciled across a rebase. A count
that is re-measured cannot go stale; that is the whole trade.

**One subprocess per suite, deliberately.** `.github/workflows/ci.yml` discovers each root separately
so that generically-named modules in different trees cannot collide on `sys.path`. Counting them in
one process would reintroduce exactly that: `discover()` puts `top_level_dir` on `sys.path`, so a
second suite with a module basename the first already imported would silently be counted as the
first's. There is no such collision today (checked), but the isolation is the reason CI is shaped that
way and a count that can be silently wrong is worse than no count.

**A `_FailedTest` is an error, not a test.** When a module fails to import, discovery yields a
synthetic case that would otherwise count as 1. Those are reported and exit non-zero rather than
quietly inflating the total by the number of broken modules.

Usage:

    python seneschal/scripts/count_tests.py                      # the table
    python seneschal/scripts/count_tests.py --check              # + refuse a prose total (CI)
    python seneschal/scripts/count_tests.py --json
    python seneschal/scripts/count_tests.py --compare-to origin/develop

Exit codes: 0 clean; 1 a suite could not be measured (an import error); 2 a tracked router states a
suite total in prose. Two codes rather than one so a partial success stays legible — "I could not
count" and "I counted, and someone wrote the number down again" are different problems with different
fixes, and a single non-zero code would flatten them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The suites CI's `python-syntax` job discovers, in its order. Each entry is (name, path relative to
# the repo root). `test_count_tests.py` parses ci.yml and asserts every `discover -s` there is either
# here or in NOT_COUNTED below — so a new suite added to CI cannot silently go unmeasured, which is
# the registry failure mode this whole change exists to avoid re-creating.
SUITES: tuple[tuple[str, str], ...] = (
    ("stdlib", "seneschal/scripts"),
)

# Discovered by CI, deliberately not counted here: the cockpit is its own dependency world
# (cockpit/README.md) and its suites need `uv sync --extra cockpit`, which the python-syntax
# job does not install. Counting them from a venv without fastapi would report whatever the
# skip-when-absent guards happen to leave standing, which is a number about the environment rather
# than about the suite.
NOT_COUNTED: dict[str, str] = {
    "cockpit/server": "needs the `cockpit` extras (own job)",
    "cockpit/decoy": "needs the `cockpit` extras (own job)",
    "cockpit/breakglass": "runs in the cockpit job's venv (own job)",
}

# The routers that would carry the collision. Scoped to them rather than swept repo-wide on
# purpose: a spec or a raise-ledger entry saying "343 tests as of <date>" is DATED EVIDENCE, and
# nothing rebases over it. A repo-wide guard would fire on those historical records, which is how
# a check becomes a wall and then gets disabled.
GUARDED_FILES: tuple[str, ...] = (
    "CLAUDE.md",
    "README.md",
)

# Both live forms a prose total takes, and nothing wider. `A` is the count written beside the
# discovery glob ("`seneschal/scripts/test_*.py` — **3428 tests**"); `B` is the count written
# beside the suite's name in a CI sentence ("the stdlib suite (3428 tests)").
# Per-module annotations — "(62)", "65 tests over four families", the verbatim "429 tests passed"
# fixture string — are inventory, not the total, and are not matched by either. That is asserted in
# `test_count_tests.py` rather than left as a claim.
_TOTAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("beside a test_*.py glob", r"test_\*\.py.{0,60}?\*{0,2}\d[\d,]*\*{0,2}\s+tests?\b"),
    ("beside a suite name", r"\bsuite\s*\(\s*\*{0,2}\d[\d,]*\*{0,2}(?:\s+tests?)?\s*\)"),
)

FIX_LINE = "python seneschal/scripts/count_tests.py"


# --------------------------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------------------------- #

def _iter_cases(suite):
    """Walk a TestSuite tree, yielding leaf cases."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item


def _is_import_failure(case) -> bool:
    """A synthetic case unittest produces for a module it could not import."""
    return type(case).__module__ == "unittest.loader"


def count_one(start_dir) -> dict:
    """Count (never run) the tests discoverable under one root.

    Returns {"tests": int, "modules": int, "errors": [str]}. Import failures are counted as errors
    and NOT as tests — a broken module must not read as one passing test.
    """
    start = Path(start_dir).resolve()
    loader = unittest.TestLoader()
    errors: list[str] = []
    tests = 0
    modules: set[str] = set()

    try:
        suite = loader.discover(str(start), pattern="test_*.py", top_level_dir=str(start))
    except Exception as exc:  # a start dir that cannot be walked at all
        return {"tests": 0, "modules": 0, "errors": [f"{start}: {exc!r}"]}

    for case in _iter_cases(suite):
        if _is_import_failure(case):
            errors.append(str(case))
            continue
        tests += 1
        modules.add(type(case).__module__)

    # loader.errors carries the import tracebacks; keep the first line of each, which names the
    # module and the exception without pasting a traceback into a CI summary.
    for err in getattr(loader, "errors", []) or []:
        first = str(err).strip().splitlines()
        if first:
            errors.append(first[0])

    return {"tests": tests, "modules": len(modules), "errors": errors}


def _subprocess_runner(start_dir) -> dict:
    """Count one suite in a fresh interpreter (see the module docstring on sys.path collisions)."""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--count-one", str(start_dir)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = (proc.stderr or proc.stdout or "no output").strip().splitlines()
        return {"tests": 0, "modules": 0, "errors": [f"{start_dir}: {detail[-1] if detail else '?'}"]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"tests": 0, "modules": 0, "errors": [f"{start_dir}: unparseable count output"]}


def measure(root=None, suites=None, runner=None) -> list[dict]:
    """Measure every suite. `runner` takes a directory and returns count_one()'s shape."""
    root = Path(root or REPO_ROOT)
    runner = runner or _subprocess_runner
    out = []
    # Read SUITES through the module rather than a default argument: a default binds at import and
    # would quietly ignore a caller (or a test) that changed the table.
    for name, rel in suites if suites is not None else SUITES:
        path = root / rel
        if not path.is_dir():
            out.append({"suite": name, "path": rel, "tests": 0, "modules": 0,
                        "errors": [f"{rel}: no such directory"], "present": False})
            continue
        result = runner(path)
        out.append({"suite": name, "path": rel, "present": True, **result})
    return out


# --------------------------------------------------------------------------------------------- #
# The guard: no suite total may come back into a router
# --------------------------------------------------------------------------------------------- #

def guard(root=None, files=None) -> list[dict]:
    """Find prose suite totals in the guarded routers. Empty list == clean."""
    root = Path(root or REPO_ROOT)
    findings = []
    for rel in files if files is not None else GUARDED_FILES:
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue  # a router that isn't there is not this check's business
        for why, pattern in _TOTAL_PATTERNS:
            for match in re.finditer(pattern, text, re.S):
                findings.append({
                    "file": rel,
                    "line": text[: match.start()].count("\n") + 1,
                    "text": " ".join(match.group(0).split()),
                    "why": why,
                })
    findings.sort(key=lambda f: (f["file"], f["line"]))
    return findings


# --------------------------------------------------------------------------------------------- #
# The baseline comparison: a vanished module is visible without a stored number
# --------------------------------------------------------------------------------------------- #

def _git(root):
    def run(*args):
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            capture_output=True, text=True,
        )
    return run


def baseline_counts(ref, root=None, git=None, runner=None, mkdtemp=None) -> tuple[dict | None, str]:
    """Count each suite as of the merge base with `ref`, in a throwaway worktree.

    Returns (counts_by_suite_name | None, note). **It never raises and never fails the run**: every
    unhappy path returns None with a note saying why there is no baseline. A comparison that could
    break CI on a shallow clone, a missing ref or a busy worktree would be a new failure mode
    bolted onto a check whose whole purpose is to remove one.
    """
    root = Path(root or REPO_ROOT)
    git = git or _git(root)
    runner = runner or _subprocess_runner
    mkdtemp = mkdtemp or (lambda: tempfile.mkdtemp(prefix="seneschal-count-base-"))

    base = git("merge-base", "HEAD", ref)
    if base.returncode != 0 or not base.stdout.strip():
        return None, f"no merge base with {ref}"
    base_sha = base.stdout.strip()

    head = git("rev-parse", "HEAD")
    if head.returncode == 0 and head.stdout.strip() == base_sha:
        return None, f"HEAD is the merge base with {ref} — nothing to compare"

    tmp = Path(mkdtemp())
    # The worktree is cut from the merge base explicitly. `--detach` because nothing here wants a
    # branch, and the sha because a ref name would re-resolve to whatever the checkout last saw.
    added = git("worktree", "add", "--detach", str(tmp), base_sha)
    if added.returncode != 0:
        why = (added.stderr or "").strip().splitlines()
        return None, f"could not create a baseline worktree: {why[-1] if why else 'git said nothing'}"

    try:
        counts = {}
        for name, rel in SUITES:
            path = tmp / rel
            counts[name] = runner(path)["tests"] if path.is_dir() else None
        note = f"baseline: {base_sha[:12]} (merge base with {ref})"
    finally:
        # NEVER --force. The refusal when a tree is dirty or holds unpushed commits IS the safety
        # mechanism (never destroy work a worktree might hold), and a refusal is reported, not swallowed.
        removed = git("worktree", "remove", str(tmp))
        if removed.returncode != 0:
            note_suffix = f" · baseline worktree left at {tmp}: {removed.stderr.strip()}"
        else:
            note_suffix = ""
    return counts, note + note_suffix


# --------------------------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------------------------- #

def _delta(head: int, base) -> str:
    if base is None:
        return "new"
    diff = head - base
    return "±0" if diff == 0 else f"{diff:+d}"


def render_text(rows, baseline=None, note="") -> str:
    width = max([len(r["suite"]) for r in rows] + [5])
    out = [f"{'suite'.ljust(width)}  {'tests':>7}  {'modules':>7}  {'vs base':>8}  path"]
    for r in rows:
        base = None if baseline is None else baseline.get(r["suite"])
        out.append(
            f"{r['suite'].ljust(width)}  {r['tests']:>7}  {r['modules']:>7}  "
            f"{(_delta(r['tests'], base) if baseline is not None else '—'):>8}  {r['path']}"
        )
    for path, why in sorted(NOT_COUNTED.items()):
        out.append(f"{'(skipped)'.ljust(width)}  {'—':>7}  {'—':>7}  {'—':>8}  {path} — {why}")
    if note:
        out.append(note)
    return "\n".join(out)


def render_markdown(rows, baseline=None, note="") -> str:
    out = ["### Test suites (measured by discovery, not run)", "",
           "| suite | tests | modules | vs base | path |", "|---|---:|---:|---:|---|"]
    for r in rows:
        base = None if baseline is None else baseline.get(r["suite"])
        delta = _delta(r["tests"], base) if baseline is not None else "—"
        out.append(f"| {r['suite']} | {r['tests']} | {r['modules']} | {delta} | `{r['path']}` |")
    if note:
        out += ["", f"_{note}_"]
    out += ["", f"Reproduce locally: `{FIX_LINE}`"]
    return "\n".join(out) + "\n"


def drops(rows, baseline) -> list[str]:
    """Suites that lost tests against the baseline — the silent-test-loss signal."""
    if not baseline:
        return []
    lost = []
    for r in rows:
        base = baseline.get(r["suite"])
        if base is not None and r["tests"] < base:
            lost.append(f"{r['suite']}: {base} -> {r['tests']} ({r['tests'] - base})")
    return lost


# --------------------------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------------------------- #

def main(argv=None, runner=None, git=None) -> int:
    parser = argparse.ArgumentParser(description="Count the test suites without running them.")
    parser.add_argument("--count-one", metavar="DIR",
                        help="internal: count one root and print JSON (one suite per interpreter)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--markdown", action="store_true", help="render a Markdown table")
    parser.add_argument("--summary-file", metavar="PATH",
                        help="append the Markdown table here (CI passes $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--check", action="store_true",
                        help="also refuse a suite total written into a tracked router")
    parser.add_argument("--compare-to", metavar="REF",
                        help="report per-suite deltas against the merge base with REF")
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    root = Path(args.root or REPO_ROOT)

    if args.count_one:
        print(json.dumps(count_one(args.count_one)))
        return 0

    rows = measure(root=root, runner=runner)

    baseline, note = (None, "")
    if args.compare_to:
        baseline, note = baseline_counts(args.compare_to, root=root, git=git, runner=runner)

    findings = guard(root=root) if args.check else []
    errors = [e for r in rows for e in r["errors"]]

    if args.json:
        print(json.dumps({"suites": rows, "baseline": baseline, "note": note,
                          "findings": findings, "not_counted": NOT_COUNTED}, indent=2))
    elif args.markdown:
        print(render_markdown(rows, baseline, note), end="")
    else:
        print(render_text(rows, baseline, note))

    if args.summary_file:
        try:
            with open(args.summary_file, "a", encoding="utf-8") as fh:
                fh.write(render_markdown(rows, baseline, note))
        except OSError as exc:  # a summary nobody can write must not cost the check
            print(f"note: could not write {args.summary_file}: {exc}", file=sys.stderr)

    lost = drops(rows, baseline)
    if lost:
        # REPORT-ONLY, and that is a decision rather than timidity: replacing a module with a better
        # one is a legitimate net loss, and a gate that is red on a legitimate action is a gate
        # someone disables. Turning this blocking waits for a record of drops that were real.
        print("\n".join(["", "⚠️  A SUITE LOST TESTS AGAINST ITS BASE — check nothing vanished silently:",
                         *(f"    {line}" for line in lost),
                         "    (report-only by design; see this module's docstring)"]), file=sys.stderr)

    if errors:
        print("\n".join(["", "A suite could not be measured — these modules failed to import:",
                         *(f"    {e}" for e in errors),
                         f"    Fix the import, then re-run: {FIX_LINE}"]), file=sys.stderr)
        return 1

    if findings:
        print("\n".join([
            "",
            "A suite total is written in prose again. Delete the number — do not update it:",
            *(f"    {f['file']}:{f['line']}  {f['text']}   ({f['why']})" for f in findings),
            "",
            "  Two PRs that each add a test both rewrite that line, so they collide by construction.",
            "  CI measures the counts on every run and publishes them to the job summary.",
            f"  Locally: {FIX_LINE}",
        ]), file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
