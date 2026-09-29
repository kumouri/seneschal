#!/usr/bin/env python3
"""The grounding byte budget. Standard library only.

## What this is

A ratchet on the size of the files the assistant loads **without being asked for**. Startup context is
paid on every cold spawn, and a router file grows fastest when everyone is doing the right thing:
**the growth has a producer, and the producer is faithfully following a standing instruction to keep
docs in sync with code.** Nobody is careless. A trim that doesn't change the producer regrows inside a
fortnight; this is the pawl that holds whatever a trim achieves. The producer change is the rule in
the root `CLAUDE.md`: detail goes in the leaf, the router only points.

## The byte cap is REPORT-ONLY. It cannot fail a build.

`main()` exits 0 on a budget violation unless `--enforce` is passed, and CI does not pass it: **a gate
that is red on the day it merges gets disabled, and then nothing is enforced at all.** A budget over
unchanged producers turns CI red every week and the escape hatch becomes the weekly ritual it exists
to prevent. What CI DOES block on is `--enforce-headroom` — a narrow shape check on a NEW raise (see
"Headroom" below), which has no pre-existing history to be red about.

## What it measures, and what it deliberately does not

**Bytes, not tokens.** Deterministic, free, dependency-free, and stable across model changes — a
tokenizer drifts with the model, so the number would move without the file moving, which is the one
property a ratchet cannot have. Token figures are printed as **labelled estimates** at a fixed ratio
(`BYTES_PER_TOKEN`): nothing decides on the estimate.

**git-object bytes**, i.e. `\\r\\n` normalised to `\\n` before counting, so every host and CI agree
exactly and a PR that only changes line endings cannot consume budget.

**Tier C (`seneschal/references/**`, `seneschal/docs/**`) is deliberately unbudgeted.** Those are the
*destination* of the producer change. Budget the leaves too and "move the detail to the leaf" stops
being available, leaving deletion as the only way to pass.

**`.claude/rules/**/*.md` is budgeted, and it is tier B, not tier C.** A path-scoped rule file is not
a leaf you go and read — it is injected, obeyed as instruction, and re-attached after compaction, so it
is grounding that happens to live outside the `CLAUDE.md` namespace. Left undeclared it would be the
cheapest possible way to make this check report a smaller number while the tree got no smaller: the
`CLAUDE-2.md` trick, one directory over. See `BUDGETED_GLOBS`.

## String literals

Startup context that lives in a `.py` (the warm session's `GROUNDING_TEMPLATE` in `presence.py`) is
budgeted like a file, as `path::NAME`. Read by **parsing the module with `ast`** rather than importing
it — an import would pull in the daemon's dependencies, and CI's dependency-free job has to be able to
run this.

## Two questions, two answers — the after-merge block

**"Did this branch grow it?" and "will the tree be over once this lands?" are different questions.** A
branch is measured against its own merge base, which is correct for attributing growth and is exactly
why it is blind: **two PRs that each grow a shared artifact within their own headroom sum to a
violation neither one can observe** — both green, and the merged tree over budget with no author.

So `check()` also measures the tree HEAD would produce when merged into the **current** `base_ref`,
via `git merge-tree --write-tree` (git ≥ 2.38; older git, an absent base, or a conflicted merge all
degrade to *no answer* rather than to a guess). Budgets for that pass come from the **merged** config,
because the question is what `develop` will say once this lands, and the other side may have tightened
one. When HEAD already contains the base — a push to `develop`, or an up-to-date branch — the merged
tree *is* HEAD's tree and the block is suppressed: `over` already answered the tree-state question.

**After-merge findings are report-only and are NOT counted in `violations`.**

## Reconciliation — measured vs. (base + declared deltas)

**The post-merge byte number is MEASURED from the tree, never summed from the deltas.** For purely
additive edits in disjoint regions, the two agree by arithmetic — the EXPECTED outcome of a clean
resolution. **The informative event is the MISMATCH, and its direction is a diagnosis:**

| measured vs. (base + declared) | what it means |
|---|---|
| equal | clean — nothing lost, nothing duplicated |
| **BELOW** | the resolution DROPPED bytes: a hunk kept from one side only, or an entry claiming bytes that never landed. **The silent-drop failure this ledger exists for.** |
| **ABOVE** | the resolution ADDED its own: a reword, a re-indent, a row kept from both sides. |

Both failure modes are rare — and that is the argument FOR checking: **rare + silent + green is the
worst combination available.** Nobody catches a one-in-fifty event by remembering to look; a
subtraction catches it every time and costs nothing.

**Scope: only an artifact whose `raises[]` GAINED entries on this branch.** Everything else is silent,
because a PR that grew a file inside its headroom declared no delta and so has nothing to reconcile.
**The single-PR case reconciles trivially** and must stay quiet, or this gets ignored and then disabled.

**It ABSTAINS rather than guessing, loudly, in four named cases** (`reconcile`): an unreachable
merge-base (a shallow clone — CI needs `fetch-depth: 0`), an artifact with no bytes at the merge-base
(newly created), one not measurable on the working tree (deleted or renamed), and a `raises[]` chain
that is itself discontinuous — summing deltas across a broken chain would answer confidently off
numbers that do not connect.

**Both measurements come from the paths above** — `measure_in_tree` at the merge-base, `measure` on
the working tree — never a second reader. A check that disagreed with the ratchet about how many bytes
a file has would be worse than no check.

**The headroom qualifier, and why it is not a fudge.** A chain's `from` is the previous **budget**, not
the previous **measured size**. On an artifact sitting *under* its budget at the merge-base, the two
conventions therefore disagree by exactly that slack: a raise that re-anchors `max_bytes` onto the
newly measured size under-claims by the headroom, one that carries the slack forward reconciles to
zero, and **neither dropped nor added a byte.** So a drift that is same-signed as the headroom and no
larger than it is reported as *explained*. **The cost is named rather than hidden:** on an artifact with
H bytes of slack, a real change of up to H is indistinguishable from that convention choice.

**Report-only, and NOT counted in `violations`.**

## Headroom — computed and reported here

A raise sets `max_bytes` to measured size **plus headroom** — approximately four weeks of the
artifact's own trailing growth, floor ~2k tokens, denominated in time — never to exactly the
newly-measured size (that makes a budget a high-water mark, not a limit). `headroom_bytes()` computes
that number for every budgeted artifact and `check()` reports it alongside today's figures,
**report-only, changing no violation**. `headroom_violations()` is the separate, narrower gate: it fires
only on a `raises[]` entry this branch itself ADDS whose `to` does not equal `headroom_bytes()`'s own
computed value at that entry's own date — a raise cannot raise by the wrong amount, because the number
is computed, never hand-typed. That is `--enforce-headroom`, which CI runs.

## The chain must be continuous

**Nothing else reads `from`** — the ratchet reads `raises[-1]`'s `to` and `reason` and nothing else, so
a hand conflict resolution could drop an intermediate entry while leaving `max_bytes` correct and every
check stays green. The loss is not a byte-count error, it is an **attribution** error; a chain with a
hole in it lies by omission, and it looks perfect.

`chain_breaks()` asserts three things over every artifact whose `raises[]` is non-empty:

1. **Continuity** — `raises[i]["from"] == raises[i-1]["to"]` for every `i > 0`. Catches a dropped,
   reordered or duplicated entry.
2. **Termination** — `raises[-1]["to"] == max_bytes`. Catches a `max_bytes` edited without an entry
   **in the one case the ratchet structurally cannot see**: it only arms when the number went *up*
   relative to the merge base, so a resolution that keeps the final number right while losing the
   last entry slips past it.
3. **Origin** — `raises[0]["from"]` is **NOT constrained, deliberately.** The first entry's base
   predates the ledger and there is nothing to check it against.

**No date rule** — a rebase writes an entry on the day it lands, not the day it was drafted, so entries
legitimately land out of date order and a date rule would be red for a reason that is not a bug.

**Report-only, NOT counted in `violations`, and `--enforce-chain` is deliberately NOT in CI.** Whether
this rule should block is an open decision; the flag exists so the answer is one workflow line, and a
test fails if anything wires it first.

USAGE:
  python check_context_budget.py                  # report, exit 0
  python check_context_budget.py --enforce        # exit 1 on any budget violation (not wired in CI)
  python check_context_budget.py --enforce-chain  # exit 1 on a broken raises[] chain, and on
                                                  # NOTHING else (not wired in CI)
  python check_context_budget.py --enforce-headroom  # exit 1 on a NEW raises[] entry whose `to`
                                                  # does not match headroom_bytes()'s own computed
                                                  # value — separate from --enforce; CI runs this
  python check_context_budget.py --json
"""
from __future__ import annotations

import argparse
import ast
import datetime
import glob
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
BUDGET_FILE = os.path.join("seneschal", "context-budget.json")

# Measured differentially against a real model rather than guessed at 4:1 — a 4:1 label
# understates real tokens for this tree's prose by ~1.5x. A display-only number: nothing here
# decides on the estimate, and no violation is measured in tokens.
BYTES_PER_TOKEN = 2.59
TOKENS_PER_BYTE = 1 / BYTES_PER_TOKEN  # labelled estimate only; nothing decides on it

# The headroom rule: ~4 weeks of an artifact's own trailing
# growth, floor ~2k tokens, denominated in time. Converted at the measured rate above, so the floor is
# ~5.2 KB, not the ~8 KB a 4:1 guess would have printed.
HEADROOM_WINDOW_DAYS = 28
HEADROOM_FLOOR_TOKENS = 2000
HEADROOM_FLOOR_BYTES = round(HEADROOM_FLOOR_TOKENS * BYTES_PER_TOKEN)
# A single commit changing a file's size by more than this fraction is a refactor, not growth — the
# step-change rule that keeps a router diet from reading as four weeks of "growth."
HEADROOM_STEP_RATIO = 0.4
# Less post-reset history than this and the trailing rate is not trusted; the floor applies instead,
# visibly — never a rate derived from one or two data points close together.
HEADROOM_MIN_HISTORY_DAYS = 3
# How far back `artifact_commit_sizes` asks git to look at all — margin beyond the window itself so a
# step-change reset near the window's edge still has enough history after it to judge, without asking
# git to walk an artifact's entire history on every run.
HEADROOM_LOOKBACK_DAYS = 4 * HEADROOM_WINDOW_DAYS
# A hard cap on commits measured per artifact, so `check()`'s report-only pass over every budgeted
# artifact stays a handful of git invocations each rather than one per historical merge — the most
# recent commits are what a trailing rate is about, and the step-change rule only ever needs the
# LATEST reset, which a truncated-to-recent walk still finds.
HEADROOM_MAX_COMMITS = 60

# A new grounding file with no budget entry is a violation — otherwise the cheapest permanent way to
# satisfy this check is to create `CLAUDE-2.md`. These are the shapes that must be declared.
#
# **`.claude/rules/**/*.md` — the third door, shut before anyone walks through it.** A rule file
# with `paths:` frontmatter is grounding: it loads on a `Read` of any file its globs match, is
# obeyed as instruction rather than read as reference, and is re-attached after compaction. So it
# is startup-shaped context arriving by a route this list would not otherwise cover, and moving
# prose out of a router into an unbudgeted rule file would make the per-file number fall while
# the routed-tree total did not move. **A rule file is created and budgeted in the same PR, or it
# is not created.** `**` rather than `*` because a hole in this list is the whole exploit.
BUDGETED_GLOBS = ("CLAUDE.md", "*/CLAUDE.md", "**/CLAUDE.md",
                  "subagents/**/SKILL.md", "seneschal/SKILL.md", "persona/*.md",
                  ".claude/rules/**/*.md")


# --------------------------------------------------------------------------- measurement

def lf_bytes(path: str) -> int | None:
    """git-object bytes: content with CRLF normalised to LF."""
    try:
        with open(path, "rb") as fh:
            return len(fh.read().replace(b"\r\n", b"\n"))
    except OSError:
        return None


def literal_bytes(py_path: str, name: str) -> int | None:
    """The UTF-8 size of a module-level string literal, via `ast` — never by importing the module."""
    try:
        with open(py_path, encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        return None
    return literal_bytes_in_source(source, name)


def literal_bytes_in_source(source: str, name: str) -> int | None:
    """Same, over source text already in hand — so a blob out of a merged tree measures identically."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return len(node.value.value.encode("utf-8"))
    return None


def measure(artifact: str, root: str) -> int | None:
    """`path` or `path::LITERAL_NAME` -> bytes, or None if it cannot be measured."""
    if "::" in artifact:
        rel, name = artifact.split("::", 1)
        return literal_bytes(os.path.join(root, rel), name)
    return lf_bytes(os.path.join(root, rel_or(artifact)))


def rel_or(path: str) -> str:
    return path.replace("/", os.sep)


def est_tokens(n: int) -> int:
    return int(n * TOKENS_PER_BYTE)


# --------------------------------------------------------------------------- git

def _git(root: str, *args) -> str | None:
    try:
        out = subprocess.run(["git", "-c", "core.fsmonitor=false", *args],
                             cwd=root, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # Decoded as UTF-8 explicitly, NOT via `text=True`: that uses the locale encoding (cp1252 on a
    # Windows host), and this reads `presence.py` — a file full of em-dashes — out of a git tree.
    return out.stdout.decode("utf-8", "replace")


def _git_allowing_conflict(root: str, *args) -> str | None:
    """`_git`, but exit 1 is a RESULT rather than a failure.

    `git merge-tree --write-tree` exits 1 on a conflicted merge and still writes a usable tree plus
    the list of what conflicted. `_git` reads that as no-answer, which is correct for `merged_tree()`
    — a check whose value is determinism may not answer a question it cannot compute — and wrong for
    a rechain helper, whose whole premise is a conflict in one known file. **Only 0 and 1 are
    accepted**: anything higher is a real git failure (a bad ref, an unknown flag, git too old) and
    still returns None, so widening this cannot make a broken invocation look like a conflict."""
    try:
        out = subprocess.run(["git", "-c", "core.fsmonitor=false", *args],
                             cwd=root, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode not in (0, 1):
        return None
    return out.stdout.decode("utf-8", "replace")


def _blob(root: str, tree_ish: str, rel_path: str) -> str | None:
    """One file's content at a commit or tree, or None if it isn't there."""
    return _git(root, "show", f"{tree_ish}:{rel_path.replace(os.sep, '/')}")


def _budgets_at(root: str, tree_ish: str) -> dict | None:
    blob = _blob(root, tree_ish, BUDGET_FILE)
    if blob is None:
        return None
    try:
        return json.loads(blob).get("budgets", {})
    except ValueError:
        return None


def merge_base(root: str, base_ref: str) -> str | None:
    """The merge-base commit with `base_ref`, or None when there isn't one to be had.

    None is the honest answer for a shallow clone (CI without `fetch-depth: 0`) and for an absent
    base ref alike — both mean *the question cannot be computed here*, and everything downstream
    degrades to no-answer rather than to a guess."""
    mb = _git(root, "merge-base", "HEAD", base_ref)
    return mb.strip() if mb and mb.strip() else None


def merge_base_budgets(root: str, base_ref: str) -> dict | None:
    """The budget config as of the merge-base with `base_ref`.

    Merge-base, not tip: comparing against a moving tip makes an unrelated merge fail your branch, and
    a check that fails for reasons the author didn't cause gets disabled. **Absent at the
    merge-base means every entry is new and permitted** — that is how this bootstraps.

    This governs the RATCHET only — whether a `max_bytes` was raised. It is deliberately *not* what
    decides over/under, and it is not the after-merge question either; see `merged_tree`."""
    mb = merge_base(root, base_ref)
    if mb is None:
        return None
    return _budgets_at(root, mb)


# --------------------------------------------------------------------------- the prospective merge

_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")


def merged_tree(root: str, base_ref: str) -> str | None:
    """The tree HEAD would produce merged into `base_ref` — the tree this branch actually ships.

    `git merge-tree --write-tree` (git ≥ 2.38) computes it without touching the working tree, index or
    any ref; it writes the resulting tree/blob objects into the object store, which is inert and
    collectable. **Every failure path returns None rather than a number:** an absent base ref, git too
    old (the flag is unknown, so it exits non-zero), and a CONFLICTED merge (exit 1) all mean *there is
    no such tree*, and a check whose whole value is being deterministic may not answer a question it
    cannot compute."""
    out = _git(root, "merge-tree", "--write-tree", base_ref, "HEAD")
    if not out:
        return None
    first = out.strip().splitlines()[0].strip() if out.strip() else ""
    return first if _OID_RE.match(first) else None


def measure_in_tree(root: str, tree_ish: str, artifact: str) -> int | None:
    """`measure()`'s twin, against a git tree instead of the working copy.

    A path is sized with `git cat-file -s`, which IS this module's basis (git-object bytes) rather than an
    approximation of it. A `path::LITERAL` is parsed out of the blob with the same `ast` walk, so the
    two paths cannot drift apart."""
    if "::" in artifact:
        rel, name = artifact.split("::", 1)
        source = _blob(root, tree_ish, rel)
        return None if source is None else literal_bytes_in_source(source, name)
    size = _git(root, "cat-file", "-s", f"{tree_ish}:{artifact.replace(os.sep, '/')}")
    try:
        return int((size or "").strip())
    except ValueError:
        return None


def section_growth(root: str, rel_path: str, base_ref: str) -> list:
    """Which markdown sections grew, by bucketing each added line under its nearest preceding heading.

    Approximate by construction — a hunk spanning a heading is charged to the first — and worth it:
    it turns "the file is too big" into "the Repo-state section grew 3 kB", which names the paragraph
    to move. Degrades to [] for non-markdown and when git is unavailable."""
    if not rel_path.endswith(".md"):
        return []
    mb = merge_base(root, base_ref)
    if mb is None:
        return []
    diff = _git(root, "diff", "--unified=0", mb, "--", rel_path)
    if not diff:
        return []

    # `--unified=0` emits NO context lines, so the added lines alone never contain the heading they
    # belong under. Read the hunk headers for their line numbers in the NEW file and look the heading
    # up there instead. (Tracking headings from the diff text alone silently attributes everything to
    # "(top of file)" — which is what the first version of this function did.)
    try:
        with open(os.path.join(root, rel_or(rel_path)), encoding="utf-8") as fh:
            new_lines = fh.read().splitlines()
    except OSError:
        return []

    heading_at = []          # heading in effect for each 1-indexed line of the new file
    current = "(top of file)"
    for line in new_lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            current = stripped.lstrip("#").strip() or current
        heading_at.append(current)

    growth: dict = {}
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    new_line_no = 0
    for line in diff.splitlines():
        m = hunk_re.match(line)
        if m:
            new_line_no = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            body = line[1:]
            idx = min(max(new_line_no, 1), len(heading_at)) - 1
            heading = heading_at[idx] if heading_at else "(top of file)"
            growth[heading] = growth.get(heading, 0) + len(body.encode("utf-8")) + 1
            new_line_no += 1
    return sorted(growth.items(), key=lambda kv: -kv[1])[:5]


# --------------------------------------------------------------------------- headroom

def _parse_git_date(s: str) -> datetime.datetime | None:
    """`%cI` — ISO 8601 with a numeric offset. `fromisoformat` handles it on 3.11+; older builds see
    `+05:00` fine but choke on a bare `Z`, which `%cI` never emits, so no fallback is needed here."""
    try:
        return datetime.datetime.fromisoformat(s.strip())
    except ValueError:
        return None


def _batch_blob_sizes(root: str, shas: list, file_path: str) -> dict:
    """`{sha: bytes}` for one path across many commits, in ONE `git cat-file --batch-check` call.

    The alternative is one `cat-file -s` subprocess per commit — for a router file with a hundred-plus
    historical commits, that is the difference between `check()`'s headroom pass costing whole seconds
    and costing half a minute (measured on this repo: ~33s naive vs. ~1s batched). `--batch-check`
    always emits exactly one output line per input line, even for a miss, so `zip` alignment holds."""
    if not shas:
        return {}
    stdin = "".join(f"{sha}:{file_path}\n" for sha in shas)
    try:
        out = subprocess.run(["git", "-c", "core.fsmonitor=false", "cat-file",
                              "--batch-check=%(objectsize)"],
                             cwd=root, input=stdin, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    sizes = {}
    for sha, line in zip(shas, out.stdout.splitlines()):
        try:
            sizes[sha] = int(line.strip())
        except ValueError:
            continue  # "<sha> missing" — the path did not exist in that commit (a rename, most likely)
    return sizes


def artifact_commit_sizes(root: str, artifact: str, ref: str = "HEAD",
                          as_of: str | None = None) -> list:
    """`[(datetime, bytes), ...]`, chronological, one point per commit on `ref`'s first-parent history
    (default HEAD — this checkout's own tree, the same one `measure()` reads "actual" from; NOT
    `origin/develop`, which can sit behind a branch's own recent growth) that touched the artifact's
    underlying file, within `HEADROOM_LOOKBACK_DAYS` of `as_of` (or now). Size is measured from that
    COMMIT's own tree, so a merge's resulting size is what is walked, not the touching commit's diff.

    Degrades to `[]` on any git failure or on a file with no history in the window — the caller's job
    is to turn that into the FLOOR, visibly, never into a guess."""
    file_path = artifact.split("::", 1)[0]
    literal_name = artifact.split("::", 1)[1] if "::" in artifact else None
    # UTC, not `date.today()` — the DEFAULT day-string must agree with `until_dt`'s own UTC basis
    # below, or a host behind UTC can seed a different `until` day than a UTC CI runner would for
    # the "same" `as_of=None` call (check_wall_clock.py's whole subject).
    until = as_of or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    until_dt = _parse_git_date(until + "T23:59:59+00:00") or datetime.datetime.now(datetime.timezone.utc)
    since = (until_dt - datetime.timedelta(days=HEADROOM_LOOKBACK_DAYS)).date().isoformat()
    # Both bounds carry an explicit +00:00: git reads a bare date in the PROCESS's local zone, so
    # a bare `--until=<date> 23:59:59` ends hours earlier on a UTC runner than on a host behind UTC,
    # and a raise validated on one walks a different commit set on the other. `until_dt` above
    # already treats the day as UTC; the string git sees says so too, so host and CI agree.
    args = ["log", "--first-parent", f"-n{HEADROOM_MAX_COMMITS}", "--format=%H%x00%cI",
            f"--since={since}T00:00:00+00:00", f"--until={until}T23:59:59+00:00"]
    args.append(ref)
    args += ["--", file_path]
    out = _git(root, *args)
    if not out:
        return []
    shas, dates = [], {}
    for line in out.strip().splitlines():
        if "\x00" not in line:
            continue
        sha, cdate = line.split("\x00", 1)
        dt = _parse_git_date(cdate)
        if dt is None:
            continue
        shas.append(sha)
        dates[sha] = dt

    if literal_name:
        points = []
        for sha in shas:
            size = measure_in_tree(root, sha, artifact)
            if size is not None:
                points.append((dates[sha], size))
    else:
        sizes = _batch_blob_sizes(root, shas, file_path)
        points = [(dates[sha], sizes[sha]) for sha in shas if sha in sizes]

    points.reverse()  # git log is newest-first; the walk below wants chronological order

    if not points:
        return points
    cutoff = points[-1][0] - datetime.timedelta(days=HEADROOM_LOOKBACK_DAYS)
    return [p for p in points if p[0] >= cutoff]


def trailing_growth(root: str, artifact: str, ref: str = "HEAD", as_of: str | None = None) -> dict:
    """The artifact's own trailing growth rate, as of `as_of` (or now) — the input to `headroom_bytes`.

    Returns `{"source", "rate_per_day", "window_days_used", "reset_at", "points"}`. `source` is one
    of `"trailing-rate"` (a positive rate over enough post-reset history), `"non-positive-rate"` (flat
    or shrinking), `"insufficient-history"` (a step-change reset, or too little history, left too
    little post-reset span to trust), or `"no-history"` (fewer than two measurable commits at all).
    Every non-`"trailing-rate"` source means `headroom_bytes` falls back to the FLOOR — always
    reported, never silent.

    **The step-change rule:** the LATEST single-commit jump exceeding
    `HEADROOM_STEP_RATIO` anywhere in the walked history is a reset; only history from that commit
    forward is used. A router diet that halves a file in one commit must not read as four weeks of
    negative growth, and a file that quadruples in one commit (the reverse of a router diet, but the
    same mechanism) must not read as organic growth either — both are refactors, and the whole point of a
    trailing rate is that it describes *growth*, not *edits*.
    """
    points = artifact_commit_sizes(root, artifact, ref, as_of)
    if len(points) < 2:
        return {"source": "no-history", "rate_per_day": None, "window_days_used": 0,
                "reset_at": None, "points": len(points)}

    end_dt, end_size = points[-1]

    reset_idx = 0
    for i in range(1, len(points)):
        prev_size = points[i - 1][1]
        cur_size = points[i][1]
        if prev_size <= 0:
            continue
        if abs(cur_size - prev_size) / prev_size > HEADROOM_STEP_RATIO:
            reset_idx = i

    usable = points[reset_idx:]
    reset_at = points[reset_idx][0].date().isoformat() if reset_idx > 0 else None

    if len(usable) < 2:
        return {"source": "insufficient-history", "rate_per_day": None, "window_days_used": 0,
                "reset_at": reset_at, "points": len(usable)}

    start_dt, start_size = usable[0]
    span_days = (end_dt - start_dt).total_seconds() / 86400.0
    if span_days < HEADROOM_MIN_HISTORY_DAYS:
        return {"source": "insufficient-history", "rate_per_day": None,
                "window_days_used": round(span_days, 2), "reset_at": reset_at,
                "points": len(usable)}

    # The full post-reset span is used as-is, even when it runs longer than HEADROOM_WINDOW_DAYS —
    # more data is never worse, and re-truncating to exactly the window would throw away history the
    # reset rule just went to the trouble of keeping.
    rate = (end_size - start_size) / span_days
    return {"source": "trailing-rate" if rate > 0 else "non-positive-rate",
            "rate_per_day": rate, "window_days_used": round(span_days, 2),
            "reset_at": reset_at, "points": len(usable)}


def headroom_bytes(root: str, artifact: str, ref: str = "HEAD", as_of: str | None = None) -> tuple:
    """`(headroom_bytes, meta)` — measured size plus this is the artifact's next `max_bytes`.

    `meta` is `trailing_growth`'s own dict plus `floor_bytes` and `headroom_bytes`, so a caller never
    has to re-derive why the floor applied — it is named in the same object the number came from."""
    growth = trailing_growth(root, artifact, ref, as_of)
    if growth["source"] == "trailing-rate":
        computed = round(growth["rate_per_day"] * HEADROOM_WINDOW_DAYS)
        headroom = max(HEADROOM_FLOOR_BYTES, computed)
        floor_applied = headroom == HEADROOM_FLOOR_BYTES and HEADROOM_FLOOR_BYTES >= computed
    else:
        headroom = HEADROOM_FLOOR_BYTES
        floor_applied = True
    meta = {**growth, "floor_bytes": HEADROOM_FLOOR_BYTES, "headroom_bytes": headroom,
            "floor_applied": floor_applied}
    return headroom, meta


def headroom_violations(root: str, budgets: dict, base: dict | None) -> list:
    """The headroom rule, in code: a raise this branch itself ADDS must set `to` to
    EXACTLY `from + headroom_bytes(...)` computed as of that entry's own date — not a range, an exact
    match, because "can't raise by the wrong amount" means the generator's number is the only number,
    never merely a validated one. Existing history at the merge-base (`base`) is exempt:
    this is a gate on new raises, not a retroactive judgement on the old ratchet's convention.

    The trailing-growth walk is always HEAD's own history (`headroom_bytes`'s default `ref`), never
    `base`'s — `base` here is only the config snapshot used to tell a new entry from an old one.

    A raise entry uses `"date"` in every live example (`raises_of`'s callers never read it), so that
    is what is read here too; `"on"` is accepted as a fallback for a hand-authored entry."""
    rows = []
    for artifact, entry in sorted(budgets.items()):
        base_raises = raises_of((base or {}).get(artifact))
        head_raises = raises_of(entry)
        if head_raises[:len(base_raises)] != base_raises:
            continue  # a rewritten history is the ratchet's/chain check's finding, not this one's
        new_entries = head_raises[len(base_raises):]
        for offset, new_entry in enumerate(new_entries):
            frm, to = new_entry.get("from"), new_entry.get("to")
            if not isinstance(frm, int) or not isinstance(to, int):
                continue  # malformed is chain_scan's finding
            as_of = new_entry.get("date") or new_entry.get("on")
            headroom, meta = headroom_bytes(root, artifact, as_of=as_of)
            expected_to = frm + headroom
            if to != expected_to:
                rows.append({"artifact": artifact, "index": len(base_raises) + offset,
                             "from": frm, "to": to, "expected_to": expected_to,
                             "headroom": headroom, "meta": meta})
    return rows


# --------------------------------------------------------------------------- the chain

def raises_of(entry) -> list:
    """An entry's `raises[]`, tolerantly — a missing or malformed one reads as empty."""
    if not isinstance(entry, dict):
        return []
    raises = entry.get("raises")
    return raises if isinstance(raises, list) else []


def chain_scan(raises: list, limit: int | None = None) -> list:
    """Every break in ONE chain, structured. The single expression of the chain assertions.

    `raises[0]["from"]` is not examined (assertion 3, origin: unconstrained). Termination is checked
    only when `limit` is an int — an absent or non-integer `max_bytes` is a different check's finding,
    and asserting against it here would report one defect twice under two names.

    A `malformed` row stops the walk: past an entry with no integer `to` there is no previous value
    for the next `from` to be compared against, so continuing would manufacture a second finding out
    of the first one.
    """
    rows, prev_to = [], None
    for i, entry in enumerate(raises):
        if not isinstance(entry, dict):
            rows.append({"rule": "malformed", "index": i, "detail": f"raises[{i}] is not an object"})
            return rows
        frm, to = entry.get("from"), entry.get("to")
        if not isinstance(frm, int) or not isinstance(to, int):
            rows.append({"rule": "malformed", "index": i,
                         "detail": f"raises[{i}] has no integer from/to"})
            return rows
        if prev_to is not None and frm != prev_to:
            rows.append({"rule": "continuity", "index": i, "expected": prev_to, "found": frm,
                         "gap": frm - prev_to,
                         "detail": (f"raises[{i}] starts at {frm:,} B but raises[{i - 1}] ended at "
                                    f"{prev_to:,} B")})
        prev_to = to
    if prev_to is not None and isinstance(limit, int) and prev_to != limit:
        last = len(raises) - 1
        rows.append({"rule": "termination", "index": last, "expected": limit, "found": prev_to,
                     "gap": prev_to - limit,
                     "detail": (f"raises[{last}] ends at {prev_to:,} B but max_bytes is "
                                f"{limit:,} B")})
    return rows


def chain_breaks(budgets: dict) -> list:
    """The chain rule over the whole ledger.

    One row per break, each naming the artifact, the entry index, the expected value, the found
    value and the byte gap. **An empty `raises[]` is clean and is skipped entirely**: those are the
    bootstrap seeds, and there is no chain to be continuous.

    **Report-only.** The result is NOT summed into `violations` — see the module docstring:
    folding it in would make `--enforce` fail on a structural finding, which would turn a
    blocking byte budget on as a side effect of this one — an open decision, not this rule's.
    """
    rows = []
    for artifact, entry in sorted(budgets.items()):
        raises = raises_of(entry)
        if not raises:
            continue
        limit = entry.get("max_bytes") if isinstance(entry, dict) else None
        for row in chain_scan(raises, limit):
            rows.append({"artifact": artifact, **row})
    return rows


# --------------------------------------------------------------------------- reconciliation

def chain_break(raises: list) -> str | None:
    """None if this `raises[]` chain is continuous; else a sentence naming the FIRST break.

    **This is `reconcile()`'s gate, NOT the finding** — the finding is `chain_breaks()` above, and
    the two share `chain_scan` so the repo cannot end up with two expressions of one rule. Here the
    question is narrower: it decides only that the deltas **cannot be summed**, so reconciliation
    abstains instead of answering off numbers that do not connect.

    So it deliberately passes `limit=None`: a **termination** break is a real chain finding and is
    reported as one, but it does not stop the deltas from summing — every `from`/`to` pair still
    connects — and abstaining on it would silence the reconciliation on exactly the artifacts whose
    ledger is already known to be wrong."""
    for row in chain_scan(raises):
        return row["detail"]
    return None


def reconcile(root: str, base_ref: str, budgets: dict, base_budgets: dict | None) -> tuple:
    """"Does the measured size equal the merge-base size plus what this branch DECLARED?"

    Returns `(answerable, rows)`. `answerable` is False only when the merge-base itself is
    unreachable — a shallow clone, or a base ref that isn't there — in which case there is one
    abstention for the whole check rather than one per artifact.

    Each row is either a reconciliation (`drift` = measured − expected; **0 is the expected outcome
    of a clean, purely additive resolution, not a coincidence**) or an `abstain` naming why the
    question could not be asked. Only artifacts whose `raises[]` gained entries on this branch appear
    at all: a PR that grew a file inside its headroom declared no delta, so there is nothing to
    reconcile and nothing to say."""
    mb = merge_base(root, base_ref)
    if mb is None:
        return False, []

    rows = []
    for artifact, entry in sorted(budgets.items()):
        head_raises = raises_of(entry)
        base_raises = raises_of((base_budgets or {}).get(artifact))
        if len(head_raises) <= len(base_raises):
            continue
        new = head_raises[len(base_raises):]
        row = {"artifact": artifact, "new_entries": len(new)}

        broken = chain_break(head_raises)
        if broken:
            rows.append({**row, "abstain": f"the raises[] chain is discontinuous — {broken}"})
            continue
        base_bytes = measure_in_tree(root, mb, artifact)
        if base_bytes is None:
            rows.append({**row, "abstain": "no bytes at the merge-base — a newly created artifact "
                                           "has no base to reconcile against"})
            continue
        actual = measure(artifact, root)
        if actual is None:
            rows.append({**row, "abstain": "not measurable on the working tree (deleted or renamed)"})
            continue

        declared = sum(e["to"] - e["from"] for e in new)
        expected = base_bytes + declared
        drift = actual - expected

        # THE HEADROOM QUALIFIER, and it is what keeps this from misdiagnosing 16 of the 36 live
        # artifacts. A chain's `from` is the previous **budget**, not the previous **measured size**,
        # so on an artifact that sat under its budget at the merge-base the two conventions in this
        # ledger disagree by exactly that slack: a raise that re-anchors `max_bytes` onto the newly
        # measured size under-claims by the headroom, while one that carries the slack forward
        # reconciles to zero. Neither dropped or added a byte. So a drift that is same-signed as the
        # headroom and no larger than it is reported as EXPLAINED rather than as a drop or an
        # addition. The cost is named rather than hidden: on an artifact with H bytes of slack, a
        # real change of up to H is indistinguishable from that convention choice — which is why
        # every recently-raised artifact sits exactly AT its budget, where H is 0 and the diagnosis
        # is unambiguous.
        base_entry = (base_budgets or {}).get(artifact)
        base_limit = base_entry.get("max_bytes") if isinstance(base_entry, dict) else None
        headroom = base_limit - base_bytes if isinstance(base_limit, int) else None
        explained = bool(drift and headroom and drift * headroom > 0 and abs(drift) <= abs(headroom))

        rows.append({**row, "base_bytes": base_bytes, "base_headroom": headroom,
                     "declared": declared, "expected": expected, "measured": actual,
                     "drift": drift, "explained_by_headroom": explained})
    return True, rows


# --------------------------------------------------------------------------- the checks

def load_config(root: str) -> dict:
    with open(os.path.join(root, BUDGET_FILE), encoding="utf-8") as fh:
        return json.load(fh)


def undeclared_files(root: str, budgets: dict) -> list:
    """Grounding-shaped files with no budget entry."""
    declared = {k.split("::", 1)[0] for k in budgets}
    found = set()
    for pattern in BUDGETED_GLOBS:
        for hit in glob.glob(os.path.join(root, rel_or(pattern)), recursive=True):
            rel = os.path.relpath(hit, root).replace(os.sep, "/")
            if os.path.isfile(hit):
                found.add(rel)
    # Never police what git does not track: a scratch CLAUDE.md in an ignored dir is not grounding.
    tracked = _git(root, "ls-files")
    if tracked is not None:
        tracked_set = set(tracked.split())
        found = {f for f in found if f in tracked_set}
    return sorted(f for f in found if f not in declared)


def check(root: str | None = None, base_ref: str = "origin/develop") -> dict:
    """Measure every budgeted artifact, compare to budget, and validate the ratchet. Pure data out."""
    root = root or REPO_ROOT
    cfg = load_config(root)
    budgets = cfg.get("budgets", {})
    mb = merge_base(root, base_ref)
    base = _budgets_at(root, mb) if mb else None

    over, missing, undeclared, unratcheted, ok = [], [], [], [], []

    for artifact, entry in sorted(budgets.items()):
        actual = measure(artifact, root)
        limit = entry.get("max_bytes")
        if actual is None:
            missing.append({"artifact": artifact, "reason": "could not be measured (moved or renamed?)"})
            continue
        headroom, headroom_meta = headroom_bytes(root, artifact)
        row = {"artifact": artifact, "tier": entry.get("tier"), "budget": limit, "actual": actual,
               "delta": actual - limit, "note": entry.get("note"),
               "headroom_budget": actual + headroom, "headroom_meta": headroom_meta}
        if actual > limit:
            row["sections"] = section_growth(root, artifact.split("::", 1)[0], base_ref)
            over.append(row)
        else:
            ok.append(row)

        # The ratchet: a raise needs a matching, reasoned raises[] entry.
        if base is not None and artifact in base:
            was = base[artifact].get("max_bytes")
            if isinstance(was, int) and isinstance(limit, int) and limit > was:
                raises = entry.get("raises") or []
                last = raises[-1] if raises else None
                good = (isinstance(last, dict) and last.get("to") == limit
                        and str(last.get("reason") or "").strip())
                if not good:
                    unratcheted.append({"artifact": artifact, "from": was, "to": limit,
                                        "raises_len": len(raises)})

    undeclared = [{"artifact": f, "measured": lf_bytes(os.path.join(root, rel_or(f)))}
                  for f in undeclared_files(root, budgets)]

    merged, merged_is_head, merged_over = after_merge(root, base_ref, over + ok)
    reconcilable, reconciled = reconcile(root, base_ref, budgets, base)

    return {"root": root, "base_ref": base_ref, "merge_base_seen": base is not None,
            "over": over, "missing": missing, "undeclared": undeclared,
            "unratcheted": unratcheted, "ok": ok,
            "merged_tree": merged, "merged_is_head": merged_is_head, "merged_over": merged_over,
            "reconcilable": reconcilable, "reconciled": reconciled,
            "broken_chains": chain_breaks(budgets),
            # `headroom_new_raises` is a SEPARATE, narrow door like `broken_chains` — it is
            # `--enforce-headroom`'s own key, not the byte-budget's. It CAN block on day one: unlike
            # the byte cap, it fires only on a raise this branch
            # itself adds, so there is no pre-existing history for it to be red about.
            "headroom_new_raises": headroom_violations(root, budgets, base),
            # merged_over, reconciled and broken_chains are deliberately absent from this sum:
            # the byte cap is report-only, and whether an after-merge, a reconciliation or a chain
            # finding should ever block is an open decision, not this module's. `--enforce-chain` is the separate,
            # narrow door for the last of the three, and it reads THIS key rather than the sum.
            "violations": len(over) + len(missing) + len(undeclared) + len(unratcheted)}


def after_merge(root: str, base_ref: str, branch_rows: list) -> tuple:
    """"Will the tree be over once this lands?" — the question the per-branch check cannot ask.

    Returns `(merged_tree_oid, merged_is_head, rows)`. `merged_is_head` means HEAD already contains
    `base_ref`, so the merge adds nothing and the block is suppressed rather than echoing `over` — a
    push to `develop` is exactly that case, and there the numbers above ARE the tree state."""
    merged = merged_tree(root, base_ref)
    if not merged:
        return None, False, []

    head_tree = _git(root, "rev-parse", "HEAD^{tree}")
    if head_tree and head_tree.strip() == merged:
        return merged, True, []

    # The MERGED config, not HEAD's: the other side may have tightened a budget as it swept, and the
    # question is what `develop` will say once this lands.
    budgets = _budgets_at(root, merged)
    if budgets is None:
        return merged, False, []

    here = {r["artifact"]: r for r in branch_rows}
    rows = []
    for artifact, entry in sorted(budgets.items()):
        limit = entry.get("max_bytes")
        actual = measure_in_tree(root, merged, artifact)
        if actual is None or not isinstance(limit, int) or actual <= limit:
            continue
        branch = here.get(artifact)
        rows.append({
            "artifact": artifact, "tier": entry.get("tier"), "budget": limit,
            "merged": actual, "delta": actual - limit,
            "branch_actual": branch["actual"] if branch else None,
            # The distinguishing field: "this PR is too big" has an author; "these two PRs are too
            # big together" does not, and the second is the one nobody sees coming.
            "only_after_merge": bool(branch) and branch["actual"] <= branch["budget"],
        })
    return merged, False, rows


# --------------------------------------------------------------------------- reporting

def render(result: dict) -> str:
    lines = []
    total = sum(r["actual"] for r in result["ok"] + result["over"])
    lines.append(f"context-budget: {len(result['ok']) + len(result['over'])} artifacts, "
                 f"{total:,} B (~{est_tokens(total):,} est. tokens)")
    if not result["merge_base_seen"]:
        lines.append("  note: no budget config at the merge-base — every entry reads as new (bootstrap).")

    for row in result["over"]:
        pct = (row["delta"] / row["budget"] * 100) if row["budget"] else 0
        lines.append(f"FAIL  {row['artifact']}")
        lines.append(f"      budget {row['budget']:,} B (~{est_tokens(row['budget']):,} est. tok) | "
                     f"actual {row['actual']:,} B | +{row['delta']:,} B (+{pct:.1f}%)")
        if row.get("sections"):
            what = " · ".join(f"§\"{h}\" +{n:,} B" for h, n in row["sections"])
            lines.append(f"      what grew:  {what}")
        lines.append("      Detail belongs in the leaf. To raise instead, append to raises[].")

    for row in result["missing"]:
        lines.append(f"FAIL  {row['artifact']}\n      {row['reason']}")

    for row in result["undeclared"]:
        size = row["measured"] or 0
        lines.append(f"FAIL  {row['artifact']} is grounding-shaped but has no budget entry.")
        lines.append(f"      Add to seneschal/context-budget.json:")
        lines.append(f'        "{row["artifact"]}": {{"tier": "B", "max_bytes": {size}, '
                     f'"raises": [], "note": "…"}}')

    for row in result["unratcheted"]:
        lines.append(f"FAIL  {row['artifact']} raised {row['from']:,} -> {row['to']:,} B "
                     f"with no matching raises[] entry.")
        lines.append(f'      Append: {{"on": "YYYY-MM-DD", "from": {row["from"]}, "to": {row["to"]}, '
                     f'"reason": "why this is worth the startup context"}}')

    if not result["violations"]:
        lines.append("  this branch: all artifacts within budget.")

    lines.extend(render_chain(result))
    lines.extend(render_headroom_violations(result))
    lines.extend(render_after_merge(result))
    lines.extend(render_reconcile(result))
    lines.extend(render_headroom(result))
    return "\n".join(lines)


def render_headroom(result: dict) -> list:
    """The headroom-derived budget for every artifact, alongside today's — report-only, changing no
    violation. One line per artifact rather than a table: this prints on
    every run, including green ones, and a wall of numbers nobody reads is how a report gets skipped."""
    rows = sorted(result["ok"] + result["over"], key=lambda r: r["artifact"])
    if not rows:
        return []
    lines = ["", "  headroom (next raise, if any, would set max_bytes to):"]
    for row in rows:
        meta = row.get("headroom_meta") or {}
        source = meta.get("source", "?")
        if source == "trailing-rate" and not meta.get("floor_applied"):
            why = f"{meta['rate_per_day']:.1f} B/day over {meta['window_days_used']:.1f}d"
        elif source == "trailing-rate":  # positive but below the floor
            why = f"floor — {meta['rate_per_day']:.1f} B/day rate would be under the floor"
        elif meta.get("reset_at"):
            why = f"floor — reset detected at {meta['reset_at']}, too little history since"
        else:
            why = f"floor — {source}"
        lines.append(f"    {row['artifact']}: actual {row['actual']:,} B -> headroom budget "
                     f"{row['headroom_budget']:,} B (+{meta.get('headroom_bytes', 0):,} B, {why})")
    return lines


def render_headroom_violations(result: dict) -> list:
    """A new raises[] entry whose `to` does not match the generator's own arithmetic — separate from
    every other block, because unlike them this one CAN block today. Silent when clean, like
    every other block that runs on ordinary work."""
    rows = result.get("headroom_new_raises") or []
    if not rows:
        return []
    lines = ["", f"  HEADROOM MISMATCH on a NEW raises[] entry — {len(rows)} finding(s):"]
    for row in rows:
        lines.append(f"HEADROOM {row['artifact']}  raises[{row['index']}]")
        lines.append(f"      from {row['from']:,} B, entry sets to {row['to']:,} B, but "
                     f"headroom_bytes() computes {row['expected_to']:,} B "
                     f"({row['from']:,} + {row['headroom']:,} headroom).")
        lines.append("      Set `to` to the computed value above (the headroom block of this "
                     "report prints it for every artifact) — it is never hand-picked.")
    lines.append("      [--enforce-headroom blocks on this. It is separate from --enforce (the byte "
                 "cap, report-only) — this only validates the SHAPE of a raise.]")
    return lines


# What each rule's break MEANS, and what the local fix is. The whole claim is that this is a
# different kind of red from "this file is too big": nobody has to trim anything to clear it, they
# have to restore an entry they dropped or move a number they forgot — which is why it is the kind
# that does not get disabled.
_CHAIN_MEANING = {
    "continuity": ("An entry was DROPPED, reordered or duplicated. The chain's history is what makes "
                   "the fifth raise legible beside the fourth, and a hole in it looks perfect. "
                   "Restore the entry; do not re-base the "
                   "survivors around the gap."),
    "termination": ("The chain and max_bytes disagree. Either the last entry was dropped, or "
                    "max_bytes was edited without one. The ratchet cannot see this: it only arms "
                    "when the number went UP against the merge base."),
    "malformed": ("An entry is not a well-formed {from, to} pair, so nothing after it in this chain "
                  "could be checked at all."),
}


def render_chain(result: dict) -> list:
    """The chain finding, printed as its own block — never folded into the budget FAILs above.

    **Silent when the chains are clean**, like `render_reconcile`: this runs on every branch and a
    check that is chatty on ordinary work gets ignored and then disabled."""
    rows = result.get("broken_chains") or []
    if not rows:
        return []

    lines = ["", f"  BROKEN raises[] CHAIN — {len(rows)} finding(s):"]
    for row in rows:
        lines.append(f"CHAIN {row['artifact']}  raises[{row['index']}]  ({row['rule']})")
        if row["rule"] == "malformed":
            lines.append(f"      {row['detail']}")
        else:
            field = "from" if row["rule"] == "continuity" else "to"
            lines.append(f"      expected {field} {row['expected']:,} B | found {row['found']:,} B "
                         f"| gap {row['gap']:+,} B")
        lines.append(f"      {_CHAIN_MEANING[row['rule']]}")
    lines.append("      [report-only, and NOT a violation. Whether this blocks is an open decision; "
                 "--enforce-chain exists and is not wired into CI.]")
    return lines


# What each direction of a mismatch MEANS. The direction is the diagnosis, so it is printed
# as one — a finding that only said "these numbers differ" would leave the reader to guess which of
# two opposite failures they are looking at.
_DRIFT_MEANING = {
    "below": ("BELOW the sum means bytes were DROPPED — a hunk kept from one side of a resolution, "
              "or an entry claiming bytes that never landed. This is the silent-drop failure the "
              "ledger exists for."),
    "above": ("ABOVE the sum means the resolution ADDED bytes of its own — a reword, a re-indent, "
              "or a row kept from both sides."),
}


def render_reconcile(result: dict) -> list:
    """The third question: does measured == merge-base + what this branch declared?

    **Silent when it reconciles**, down to one line, because the single-PR case is the overwhelming
    majority of PRs and a check that is chatty on ordinary work gets ignored and then disabled."""
    rows = result.get("reconciled") or []
    if not result.get("reconcilable"):
        # Said out loud on every run, exactly as the after-merge block says its own no-answer: an
        # abstention nobody can see is indistinguishable from a check that passed.
        return [f"  reconciliation: no answer — the merge-base with {result.get('base_ref')} is "
                f"unreachable (a shallow clone? CI needs fetch-depth: 0). Not guessed."]
    if not rows:
        return []

    abstained = [r for r in rows if "abstain" in r]
    explained = [r for r in rows if r.get("explained_by_headroom")]
    drifted = [r for r in rows if r.get("drift") and not r.get("explained_by_headroom")]
    clean = len(rows) - len(drifted) - len(abstained) - len(explained)

    lines = []
    if clean:
        lines.append(f"  reconciliation: {clean} raised artifact(s) measure exactly "
                     f"merge-base + declared deltas.")
    for row in explained:
        lines.append(f"  reconciliation: {row['artifact']} is {abs(row['drift']):,} B "
                     f"{'above' if row['drift'] > 0 else 'below'} merge-base + declared, within the "
                     f"{abs(row['base_headroom']):,} B of headroom it had at the merge-base — the "
                     f"chain anchors on the previous BUDGET, not the previous measured size. "
                     f"Not a dropped or duplicated byte.")
    for row in abstained:
        lines.append(f"  reconciliation ABSTAINS on {row['artifact']}: {row['abstain']}.")
    for row in drifted:
        direction = "BELOW" if row["drift"] < 0 else "ABOVE"
        lines.append("")
        lines.append(f"RECONCILE  {row['artifact']} — measured is {abs(row['drift']):,} B "
                     f"{direction} (merge-base + declared deltas).")
        lines.append(f"      merge-base {row['base_bytes']:,} B + {row['declared']:,} B declared "
                     f"over {row['new_entries']} new raises[] entr"
                     f"{'y' if row['new_entries'] == 1 else 'ies'} = {row['expected']:,} B expected")
        lines.append(f"      measured {row['measured']:,} B ({row['drift']:+,} B)")
        if row.get("base_headroom"):
            lines.append(f"      (the artifact had {row['base_headroom']:+,} B of headroom at the "
                         f"merge-base, which does not account for this.)")
        lines.append(f"      {_DRIFT_MEANING['below' if row['drift'] < 0 else 'above']}")
    if drifted:
        lines.append("      Fix the tree or the entry — never the arithmetic. The measured number "
                     "is the true one.")
        lines.append("      [report-only, and NOT a violation — the byte budget cannot fail a build.]")
    return lines


def render_after_merge(result: dict) -> list:
    """The second question, printed as a second question — never folded into the first."""
    base = result.get("base_ref")
    if not result.get("merged_tree"):
        return [f"  after merging into {base}: no answer — the ref is absent, the merge conflicts, "
                f"or git is older than 2.38. Not guessed."]
    if result.get("merged_is_head"):
        return [f"  HEAD already contains {base}, so the figures above ARE the tree state."]
    if not result.get("merged_over"):
        return [f"  after merging into {base}: still within budget."]

    lines = [f"", f"  AFTER MERGE into {base} — the tree this branch actually produces:"]
    for row in result["merged_over"]:
        pct = (row["delta"] / row["budget"] * 100) if row["budget"] else 0
        lines.append(f"OVER  {row['artifact']}")
        lines.append(f"      budget {row['budget']:,} B | merged {row['merged']:,} B | "
                     f"+{row['delta']:,} B (+{pct:.1f}%)")
        if row["only_after_merge"]:
            lines.append(f"      Within budget on this branch ({row['branch_actual']:,} B). This "
                         f"violation is invisible from either side alone — it is the sum of two "
                         f"PRs.")
    lines.append("      [report-only, and NOT a violation — the byte budget cannot fail a build.]")
    return lines


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="The grounding byte budget (the byte cap is report-only).")
    p.add_argument("--root", default=REPO_ROOT)
    p.add_argument("--base-ref", default="origin/develop")
    p.add_argument("--json", action="store_true")
    p.add_argument("--enforce", action="store_true",
                   help="exit 1 on violations (not wired into CI — the byte cap is report-only by design)")
    # A SEPARATE, NARROW DOOR, and the narrowness is the design. It fails on a broken raises[]
    # chain and on NOTHING else, so the byte ratchet above stays report-only — folding the two
    # together would turn a blocking byte cap on as a side effect of a structural rule. It is NOT
    # in ci.yml: whether it should block is an open decision.
    p.add_argument("--enforce-chain", action="store_true",
                   help="exit 1 on a broken raises[] chain, and on nothing else "
                        "(whether CI should do this is an open decision; it is not wired)")
    p.add_argument("--enforce-headroom", action="store_true",
                   help="exit 1 on a NEW raises[] entry whose `to` does not match headroom_bytes()'s "
                        "own computed value, and on nothing else (separate from --enforce; "
                        "CI runs this)")
    args = p.parse_args(argv)

    result = check(args.root, args.base_ref)
    print(json.dumps(result, indent=2) if args.json else render(result))

    if result["violations"] and not args.enforce:
        print(f"\n  [report-only] {result['violations']} violation(s) reported, "
              f"exit 0 by design.")
    broken = result.get("broken_chains") or []
    if broken and not args.enforce_chain:
        print(f"\n  [report-only] {len(broken)} broken-chain finding(s) reported, exit 0 by design "
              f"(--enforce-chain is the opt-in and is not wired into CI).")
    bad_headroom = result.get("headroom_new_raises") or []
    if bad_headroom and not args.enforce_headroom:
        print(f"\n  [report-only] {len(bad_headroom)} headroom-mismatch finding(s) reported, exit 0 "
              f"by design — pass --enforce-headroom to block.")
    return 1 if ((args.enforce and result["violations"])
                 or (args.enforce_chain and broken)
                 or (args.enforce_headroom and bad_headroom)) else 0


if __name__ == "__main__":
    sys.exit(main())
