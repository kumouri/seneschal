#!/usr/bin/env python3
"""A change that narrates a NEW ruling must also touch the ledger. Stdlib only. Built in the shape of
`check_doc_status.py`, deliberately.

## What this is for

A decision about how the assistant must behave tends to live as prose inside whichever spec happened
to need it that day — and a decision that lives only as prose gets asked again, because nothing points
a later reader at it. `../docs/rulings.md` is the durable, greppable home for a dated decision; this
module is its gate. It does not check that a ruling is *true* or well-formed beyond its own table
shape; it checks that a change which teaches the tree a new "ruling"/"ruled" sentence also left a row
behind — the same "checks the claim is MADE, never that it is true" limit `check_doc_status.py` states
about itself.

## What it actually looks at

A **diff**, not the tree — the one check here that has to be, because "did THIS change add ruling
language" is a question about the patch, not about the file's final text. `git diff -U0
<merge-base> -- '*.md'` — **against the WORKING TREE, not `..HEAD`** — plus a synthetic new-file hunk
for every untracked `*.md` (`gate_git.added_lines_diff`) is parsed by hand for **added** lines (`+`,
never `+++`) matching `\bruling\b` or `\bruled\b`, case-insensitive. If any markdown file other than the
ledger itself gained such a line, the ledger must be one of the files this diff touches. It is not
enough that the ledger merely *exists* — the change must have edited it.

**Why the working tree.** Diffing `<merge-base>..HEAD` sees committed changes only: run BEFORE
`git commit` (gates green, then commit, then push: the natural order) it is shown an empty diff,
passes vacuously, and the commit then makes the ruling language visible to CI, which goes red on the
very gate that passed locally. On a clean checkout — CI — the two forms are identical; locally only
the working-tree form answers the question actually being asked, "is what I am about to commit clean?".
The shared helper is `gate_git.py`.

**Degrades to no-answer, never to a false negative or a false block, when the base cannot be
resolved** — the same posture `check_context_pointers.py` and `check_context_budget.py` take for their
own `origin/develop` reads. A shallow checkout (`fetch-depth: 1`, the GitHub Actions default) has no
`origin/develop` to diff against; rather than either silently passing every change or blocking one
that added no ruling language at all, `scan()` reports `base: None` and zero findings. CI sets
`fetch-depth: 0` for exactly this reason.

## The ledger's own shape

A lightweight second pass over `../docs/rulings.md` itself: every table row (after the header and the
`|---|` separator) must have exactly six cells, a non-empty value in each, and an `id` matching
`YYYY-MM-DD-kebab-slug` — the format `rulings.md`'s own intro documents. `id` values must be unique.
This is NOT a semantic check (nothing here reads whether `enforced where` names a real symbol); it is
the same "a status line that parses" bar `check_doc_status.py` holds itself to. An empty table — the
ledger's starting state — is well-formed.

## What this deliberately does NOT check

- **The dates-in-grounding lint** (a tier A/B file may not carry a bare `YYYY-MM-DD` outside a ruling
  id) is a SEPARATE, report-only module (`check_grounding_dates.py`); folding it into an enforcing
  check here would either block every change or force silently ignoring its findings.
- **Whether a `.md` file's new "ruling" sentence describes an ACTUAL decision** versus using the word
  in passing — a false positive here costs one ledger row that documents something already true, which
  is a much better trade than a missed decision with nowhere durable to live.

## The escape hatch — a marker, not a blocklist

The ordinary English idiom "ruled out" (in a statistical sense, say) is a false positive — but
excluding the idiom outright is REJECTED, because "the owner ruled the send-time check out" is exactly
the shape this check exists to catch, and a blanket exclusion would silently pass it.

So the escape is a per-LINE marker, reason-required like every allowlist in this tree:
`<!-- check-rulings: not-a-ruling — <reason> -->`, an HTML comment (invisible when rendered) on the
SAME added line as the match — `CHECK_RULINGS_MARKER_RE`. **The reason is mandatory**: a marker with an
empty or missing one is not a silent pass, it lands as its own finding
(`check-rulings-marker-empty-reason`), because an unexplained opt-out is the same blind spot in a nicer
hat. The marker suppresses ONLY the line it lives on — never the file, never the check.

The reason text may contain any character, including a bare `>` (a reason mentioning the `->` operator
must not make the whole marker fail to match). The reason is matched non-greedily up to the comment's
own closing `-->`, which is also the correct HTML reading — a comment ends at the first `-->` it
contains, so a reason that itself embeds a literal `-->` ends the marker there rather than swallowing
forward into whatever text (or whichever OTHER marker) comes next.

**A marker that is present but does not parse is not silence either.** A line carrying the literal
substring `check-rulings:` that still fails to match `CHECK_RULINGS_MARKER_RE` — a missing `-->`, a
missing separator, a typo in `not-a-ruling` — lands as its own finding,
`check-rulings-marker-malformed-syntax`, distinct from both a clean pass and the empty-reason case, so
"you didn't try" and "you tried and this didn't parse" never look the same in the report.

USAGE:
  python check_rulings.py                 # report + exit 0
  python check_rulings.py --enforce       # exit 1 on any finding
  python check_rulings.py --base <ref>    # override the merge-base lookup (tests, or a non-develop flow)
  python check_rulings.py --json          # machine-readable result
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, SCRIPT_DIR)

import gate_git  # noqa: E402  — the working-tree diff helper every diff-against-base gate shares

LEDGER_PATH = "seneschal/docs/rulings.md"

#: Word-bounded so "ruler" or "overruled" (neither occurs in practice, but the boundary is the
#: correctness property) never trips the scan.
RULING_WORD_RE = re.compile(r"\b(ruling|ruled)\b", re.IGNORECASE)

#: The escape hatch — module docstring's "The escape hatch" section. `reason` (named group) must be
#: non-empty for a marker to validly suppress its line. `.*?` (non-greedy) rather than `[^>]*?`: a
#: reason may contain a bare `>` (e.g. "the `->` operator") — excluding `>` outright made the WHOLE
#: marker fail to match on any such reason, not just one ending in `-->`. Non-greedy still stops at
#: the first literal `-->` it finds, which is the correct HTML reading (a comment ends at its first
#: `-->`) and is what keeps a reason that itself contains `-->` from swallowing forward across the
#: rest of the line or into a second marker.
CHECK_RULINGS_MARKER_RE = re.compile(
    r"<!--\s*check-rulings:\s*not-a-ruling\s*(?:—|:)\s*(?P<reason>.*?)\s*-->",
    re.IGNORECASE,
)

#: A line carrying this substring is a marker ATTEMPT, whether or not it goes on to parse as a valid
#: `CHECK_RULINGS_MARKER_RE` — used only to tell "no marker was tried" apart from "a marker was tried
#: and is malformed" (module docstring's "The escape hatch" section, last paragraph).
CHECK_RULINGS_ATTEMPT_RE = re.compile(r"check-rulings\s*:", re.IGNORECASE)

#: `YYYY-MM-DD-kebab-slug` — the id shape `rulings.md`'s own intro documents.
ID_RE = re.compile(r"^\d{4}-\d\d-\d\d-[a-z0-9]+(?:-[a-z0-9]+)*$")

REQUIRED_COLUMNS = ("id", "date", "rule", "enforced where", "source", "incident")

#: Candidate `origin/*` base refs, tried in order. `origin/develop` first (the integration branch);
#: a bare `develop` as a fallback for a checkout with no `origin` remote configured at all (a fixture
#: repo in a test, never the live tree — see `test_check_rulings.py`).
BASE_CANDIDATES = ("origin/develop", "develop")


class Finding:
    __slots__ = ("kind", "path", "detail")

    def __init__(self, kind: str, path: str, detail: str):
        self.kind, self.path, self.detail = kind, path, detail

    def as_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "detail": self.detail}


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def resolve_base(root: str, base_ref: str | None = None) -> str | None:
    """The merge-base commit this PR branched from, or `None` if it cannot be determined.

    `None` is a first-class answer, not a failure — see the module docstring's "degrades to
    no-answer" posture. A caller that cannot resolve a base must report zero findings, never a
    guessed one."""
    candidates = [base_ref] if base_ref else list(BASE_CANDIDATES)
    for ref in candidates:
        if _git(root, "rev-parse", "--verify", "--quiet", ref).returncode != 0:
            continue
        mb = _git(root, "merge-base", "HEAD", ref)
        if mb.returncode == 0 and mb.stdout.strip():
            return mb.stdout.strip()
    return None


def changed_paths(root: str, base: str) -> list:
    """Repo-relative paths that differ between `base` and the WORKING TREE (staged, unstaged and
    untracked-not-ignored alike), forward-slashed — `gate_git.changed_paths`, never `<base>..HEAD`."""
    return gate_git.changed_paths(root, base)


def _added_lines_by_ruling_status(root: str, base: str) -> tuple:
    """One pass over the diff. Returns `(unsuppressed, malformed_reason, malformed_syntax)`, each
    `{path: [added line text, ...]}`. Only `+` lines count — a ruling word already present before
    this PR is not this PR's to answer for. A line carrying `CHECK_RULINGS_MARKER_RE` with a
    non-empty reason is validly suppressed and appears in NONE of the three dicts; one with the
    marker but an empty/whitespace-only reason lands in `malformed_reason`; one that carries the
    literal substring `check-rulings:` (a marker was clearly attempted) but never matches
    `CHECK_RULINGS_MARKER_RE` at all lands in `malformed_syntax` — in both malformed cases the
    unexplained/unparseable opt-out stays a finding of its own rather than being scanned as if no
    marker had ever been tried."""
    diff = gate_git.added_lines_diff(root, base, "*.md")
    if diff is None:
        return {}, {}, {}
    unsuppressed: dict = {}
    malformed_reason: dict = {}
    malformed_syntax: dict = {}
    current_path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            spec = line[4:].strip()
            current_path = None if spec == "/dev/null" else spec[2:] if spec.startswith("b/") else spec
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") and current_path:
            text = line[1:]
            if not RULING_WORD_RE.search(text):
                continue
            marker = CHECK_RULINGS_MARKER_RE.search(text)
            if marker:
                if marker.group("reason").strip():
                    continue  # validly suppressed — the marker IS the audit trail
                malformed_reason.setdefault(current_path, []).append(text.strip())
                continue
            if CHECK_RULINGS_ATTEMPT_RE.search(text):
                malformed_syntax.setdefault(current_path, []).append(text.strip())
                continue
            unsuppressed.setdefault(current_path, []).append(text.strip())
    return unsuppressed, malformed_reason, malformed_syntax


def added_ruling_lines(root: str, base: str) -> dict:
    """`{path: [added line text, ...]}` for every tracked `*.md` file whose diff against `base`
    ADDED a line matching `RULING_WORD_RE`, EXCLUDING a line validly suppressed by a
    `check-rulings: not-a-ruling` marker (non-empty reason) — see the module docstring's "The escape
    hatch", `malformed_rulings_markers` for the marker's empty-reason case, and
    `malformed_rulings_marker_syntax` for a marker that was attempted but doesn't parse — neither
    stays a silent pass."""
    return _added_lines_by_ruling_status(root, base)[0]


def malformed_rulings_markers(root: str, base: str) -> dict:
    """`{path: [added line text, ...]}` for an added line carrying `check-rulings: not-a-ruling`
    marker syntax but an empty or whitespace-only reason — the marker used without the one thing
    that makes it accountable."""
    return _added_lines_by_ruling_status(root, base)[1]


def malformed_rulings_marker_syntax(root: str, base: str) -> dict:
    """`{path: [added line text, ...]}` for an added line carrying the literal substring
    `check-rulings:` — a marker was clearly attempted — that never matches `CHECK_RULINGS_MARKER_RE`
    at all (missing `-->`, wrong separator, a typo in `not-a-ruling`, ...). Distinct from
    `malformed_rulings_markers`, which is for a marker that DOES parse but has an empty reason."""
    return _added_lines_by_ruling_status(root, base)[2]


def parse_ledger_rows(text: str) -> tuple:
    """`(rows, findings)`. A row is `{column: cell}`; a finding names a structural defect. Only the
    table body is parsed — everything before the header row and after is prose this module never
    reads, matching `check_doc_status.py`'s "the prose is the document's own voice" stance."""
    lines = text.replace("\r\n", "\n").split("\n")
    rows, findings = [], []
    in_table = False
    header = None
    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if not in_table:
            if line.startswith("| id ") or line == "| id |":
                header = [c.strip() for c in line.strip("|").split("|")]
                in_table = True
            continue
        if not line.startswith("|"):
            break
        if set(line.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
            continue  # the `|---|---|` separator row
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != len(REQUIRED_COLUMNS):
            findings.append(Finding("ledger-malformed-row", LEDGER_PATH,
                                     f"line {i}: expected {len(REQUIRED_COLUMNS)} columns, found "
                                     f"{len(cells)}"))
            continue
        row = dict(zip(REQUIRED_COLUMNS, cells))
        for col in REQUIRED_COLUMNS:
            if not row[col]:
                findings.append(Finding("ledger-empty-cell", LEDGER_PATH,
                                         f"line {i}: `{col}` is empty"))
        if row["id"] and not ID_RE.match(row["id"]):
            findings.append(Finding("ledger-bad-id", LEDGER_PATH,
                                     f"line {i}: id {row['id']!r} does not match YYYY-MM-DD-slug"))
        rows.append(row)
    if header is None:
        findings.append(Finding("ledger-unreadable", LEDGER_PATH,
                                 "no `| id | date | rule | ...` table header found"))
    return rows, findings


def scan(root: str | None = None, base_ref: str | None = None) -> dict:
    """The one reader. Returns `{"root", "base", "ledger_touched", "candidates", "findings"}`."""
    root = root or REPO_ROOT
    findings = []

    ledger_full = os.path.join(root, LEDGER_PATH)
    try:
        with open(ledger_full, encoding="utf-8") as fh:
            ledger_text = fh.read()
    except OSError as exc:
        findings.append(Finding("ledger-unreadable", LEDGER_PATH, str(exc)))
        ledger_text = ""
    rows, row_findings = parse_ledger_rows(ledger_text) if ledger_text else ([], [])
    findings.extend(row_findings)
    ids = [r["id"] for r in rows if r.get("id")]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    for dupe in dupes:
        findings.append(Finding("ledger-duplicate-id", LEDGER_PATH, f"id {dupe!r} appears more than once"))

    base = resolve_base(root, base_ref)
    candidates: dict = {}
    ledger_touched = None
    if base is not None:
        changed = changed_paths(root, base)
        ledger_touched = LEDGER_PATH in changed
        unsuppressed, malformed_reason, malformed_syntax = _added_lines_by_ruling_status(root, base)
        candidates = {p: lines for p, lines in unsuppressed.items() if p != LEDGER_PATH}
        if candidates and not ledger_touched:
            names = ", ".join(sorted(candidates))
            sentences = "; ".join(f"{p}: {' / '.join(lines)}" for p, lines in sorted(candidates.items()))
            findings.append(Finding(
                "ruling-without-ledger-row", names,
                f"this PR adds ruling/ruled language to {names} but never touches {LEDGER_PATH} — "
                f"add or update a row (see rulings.md's own format section), or if this is not "
                f"actually a decision (e.g. the idiom \"ruled out\"), mark the line "
                f"<!-- check-rulings: not-a-ruling — <reason> -->. Matched: {sentences}"))
        reason_candidates = {p: lines for p, lines in malformed_reason.items() if p != LEDGER_PATH}
        if reason_candidates:
            names = ", ".join(sorted(reason_candidates))
            sentences = "; ".join(f"{p}: {' / '.join(lines)}"
                                   for p, lines in sorted(reason_candidates.items()))
            findings.append(Finding(
                "check-rulings-marker-empty-reason", names,
                f"a check-rulings marker in {names} has no reason — "
                f"<!-- check-rulings: not-a-ruling — <reason> --> requires the reason. "
                f"Matched: {sentences}"))
        syntax_candidates = {p: lines for p, lines in malformed_syntax.items() if p != LEDGER_PATH}
        if syntax_candidates:
            names = ", ".join(sorted(syntax_candidates))
            sentences = "; ".join(f"{p}: {' / '.join(lines)}"
                                   for p, lines in sorted(syntax_candidates.items()))
            findings.append(Finding(
                "check-rulings-marker-malformed-syntax", names,
                f"a check-rulings marker in {names} does not parse — expected exactly "
                f"<!-- check-rulings: not-a-ruling — <reason> --> (an em dash or colon after "
                f"not-a-ruling, a non-empty reason, then the closing -->). "
                f"Matched: {sentences}"))

    return {"root": root, "base": base, "ledger_touched": ledger_touched,
            "candidates": {p: lines for p, lines in candidates.items()},
            "rows": len(rows), "findings": [f.as_dict() for f in findings]}


def render(result: dict, out=None) -> None:
    out = out or sys.stdout
    out.write("check_rulings: %d row(s) in %s\n" % (result["rows"], LEDGER_PATH))
    if result["base"] is None:
        out.write("  base ref unresolved — skipping the diff scan (no-answer, not a finding)\n")
    else:
        out.write("  base: %s · ledger touched in this diff: %s\n"
                   % (result["base"][:12], result["ledger_touched"]))
    if result["findings"]:
        out.write("\n")
        for f in result["findings"]:
            out.write("%s\n    %s: %s\n" % (f["path"], f["kind"], f["detail"]))
    else:
        out.write("  no findings.\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", default=REPO_ROOT)
    p.add_argument("--base", default=None, help="override the merge-base ref lookup")
    p.add_argument("--enforce", action="store_true", help="exit 1 on any finding")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    result = scan(args.root, args.base)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)
    if result["findings"] and args.enforce:
        return 1
    if result["findings"]:
        print("\n  [report-only] %d finding(s), exit 0. --enforce blocks." % len(result["findings"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
