#!/usr/bin/env python3
"""**Which other open pull requests change the same files as this one.** Standard library + the `gh`
CLI. Reads GitHub, computes a set intersection, and returns data. It renders nothing, decides
nothing, and writes nothing to disk.

`../docs/concurrent-pr-collisions-spec.md` phase 1. The rendering half lives in
:func:`merge_guard.overlap_band`, because that module is *"the one place the approval picker is
spelled"* and a second speller is a second picker.

## The failure it answers

Two independent PRs cut minutes apart from the same base, both green, both approved by tap. One
merges; the other goes `CONFLICTING` within seconds, and its approval — bound to a head SHA that no
longer exists — is void. The owner taps twice for one pull request, and the picker for the second one
gave no way to know the first existed. On a busy day the same thing recurs three ways at once: a tap
spent a second before a sibling merges, one PR asked about at two different heads while a rebase
moves it, and several PRs going `CONFLICTING` on the same shared files — typically the context-budget
ledger (`seneschal/context-budget.json`), which almost every prose PR edits. That last fact is the
spec's §1.3 as an observation rather than a prediction: **ordering cannot prevent this collision, it
can only decide who rebases.**

## It states the overlap. It does not predict the conflict.

The signal is the **file-list intersection**, and its accuracy is known and accepted rather than
engineered away: two PRs that both add a row to a router table share that path, yet git routinely
auto-merges the two rows because they land in different regions of the file. A module that predicted
conflicts would cry wolf on exactly the pairs it exists for.

So this returns *"these paths are in both diffs"*, which is a fact about two file lists, and never
*"these will conflict"*, which is a claim about a merge nobody has run. A claim is about OUR EVIDENCE,
never the world. The wording is the constraint, and `merge_guard`'s
`OverlapStatesTheFactNeverThePredictionTest` pins it against literal strings.

## What is included, and the two inclusions are the load-bearing ones

* **Drafts are included.** A draft's collision is deferred, not absent — it lands the moment somebody
  marks it ready, and un-drafting moves no commit.
* **Red and pending PRs are included.** CI colour says nothing about which paths a diff touches.
* **Closed and merged PRs are excluded** (`--state open` only). A merged PR is already in the base; a
  closed one will never land.
* **The context-budget ledger is NOT suppressed**, even though nearly every prose PR touches it.
  Hiding the single most collision-prone file in the tree to make the message tidier would hide the
  one thing this exists to show. If the noise ever proves real the fix is to rank it last, never to
  drop it.

## An in-flight job's own worktree is read too

**A merge can invalidate a mid-job worktree exactly as it can invalidate another open PR.** A
delegated job cuts its own private worktree off the base branch (`job_worktree.py`) and may run for
hours before it ever becomes a PR — so the file intersection this module computes against every
*other open PR* never sees it, and a picker could send with no warning that landing it would
stale-base a job mid-flight.

:func:`list_inflight_jobs` reads `state/jobs/*.json` (via `jobs.list_jobs(..., active_only=True)`,
lazily imported the same way `merge_guard.overlapping_prs` imports this module — a broken import
costs the row, never this module's own PR-overlap answer) and, for each job still holding a live
worktree directory, runs `git diff --name-only` between the worktree's own recorded base and its
current state to learn what it has touched. A job record that carries no base falls back to
`origin/<base branch>` (`repo_config.base_branch()`, resolved at call time). :func:`job_overlaps` is
:func:`overlaps`'s own "states the fact, never predicts the merge" contract, applied to that list
instead of a `gh` one. Both are pure/impure split exactly like the PR pair above, and
:func:`find_job_overlaps` is :func:`find`'s twin.

**The gap this leaves, named rather than left implied:** a worktree's `git diff` against its base
sees committed and uncommitted *tracked* changes, never a file the agent created and has not yet
`git add`ed — the same "known and accepted" shape :data:`FILE_LIST_SUSPECT` documents for `gh`'s own
file-list truncation. It under-reports, never over-reports, which is the direction this whole module
already fails in.

## Failure means saying nothing, on every path

**An overlap block is information; a picker is a merge that can happen.** So every failure of every
kind — `gh` missing, unauthenticated, rate-limited, offline, slow, unparseable, a repo that cannot be
read — returns `[]` and the picker goes out exactly as it would without this module. :func:`find`
never raises. `watch_pr.py`'s rule unchanged: *"asking is not merging and may not cost the verdict."*

Three bounds keep the cost of looking small enough that this can sit on the ask path:

1. :data:`LIST_TIMEOUT_SEC` — **shorter than `pr_sweep`'s 30 s**, deliberately. That one runs on a
   cadence and can afford to wait; this one is between the owner and a question they are owed.
2. :data:`CACHE_TTL_SEC` — an **in-process** cache, keyed by repository. The resident daemon imports
   `merge_guard` once and asks about at most one PR per sweep pass, so a burst of pickers costs one
   `gh pr list` rather than one per picker. It is deliberately **not on disk**: the spec's §4.6 says
   phase 1 records nothing, and a cache file is a state file wearing a smaller name.
3. :data:`PR_LIST_LIMIT` — finite, so a repository in a strange state cannot make one look unbounded.

USAGE (diagnostics; `merge_guard` is the production caller):
  python pr_overlap.py --pr 12 --repo owner/name
  python pr_overlap.py --pr 12 --repo owner/name --state-dir seneschal/state
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

#: What one `gh pr list` is asked for. `files` is the signal; `title` is what the picker names;
#: `isDraft` is carried so a reader can see the row was not filtered on colour or readiness, which is
#: the property most likely to be "helpfully" added later. **`statusCheckRollup` is deliberately not
#: requested** — this module states the overlap, never the CI colour, so there is no field to filter
#: on later and no temptation to let one in.
LIST_FIELDS = "number,title,files,isDraft"

#: How many open pull requests one call may return. Generous but finite — see bound 3 above.
PR_LIST_LIMIT = 50

#: **Shorter than `pr_sweep.LIST_TIMEOUT_SEC` (30 s) on purpose.** This call sits between a green PR
#: and the question the owner is owed about it, so it must give up long before anybody notices. A
#: timeout costs the overlap block and nothing else.
LIST_TIMEOUT_SEC = 12

#: How long a repository's PR list is reused within one process. Two minutes is under `pr_sweep`'s
#: ~3-minute cadence, so the resident daemon re-reads GitHub on essentially every pass while a burst
#: of pickers inside one pass shares a single call.
CACHE_TTL_SEC = 120

#: The number of files at which `gh` may have truncated its own answer, so `files` can no longer be
#: read as complete. Carried on the row as `files_truncated` and **not rendered**: a truncated list
#: causes a MISSED overlap, which is the direction this module already fails in, and a caveat in the
#: picker about GitHub's pagination is not something the owner can act on.
#: `../docs/concurrent-pr-collisions-spec.md` §8 records that this boundary is unverified.
FILE_LIST_SUSPECT = 100

#: `{repo: (fetched_at_monotonic, rows)}`. Process-local, never persisted. See bound 2.
_CACHE = {}


def _run(argv: list) -> tuple:
    """`(returncode, stdout, stderr)`. The one subprocess seam, so a test replaces one thing and no
    test can reach a real `gh`."""
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=LIST_TIMEOUT_SEC)
    return proc.returncode, proc.stdout, proc.stderr


def clear_cache() -> None:
    """Forget every cached PR list. For tests and for a caller that knows the world moved."""
    _CACHE.clear()


def _now(now=None) -> float:
    return time.monotonic() if now is None else float(now)


def default_job_base() -> str:
    """The ref a job worktree is diffed against when its record names none:
    `origin/<repo_config.base_branch()>`. `repo_config` never raises; the import is lazy so this
    module's own import surface stays stdlib-only for the hook path that imports it."""
    try:
        import repo_config
        return f"origin/{repo_config.base_branch()}"
    except Exception:  # noqa: BLE001 — an unresolvable base is the Git Flow default, never a crash
        return "origin/develop"


def list_open_prs(repo: str, runner=None, limit: int = PR_LIST_LIMIT, now=None,
                  use_cache: bool = True) -> list:
    """Every open pull request in `repo`, as raw `gh` rows. **`[]` on every failure**, including the
    failure to tell a broken repository from an empty one.

    That collapse is deliberate and is the opposite of `pr_sweep.list_open_prs`, which distinguishes
    `None` (could not read) from `[]` (read, nothing there) because its caller *reports* the
    difference. Here there is nothing to report to: the only consumer is a picker's body, and both
    answers produce the same body — no overlap block. A second outcome would only tempt a future edit
    into saying *"I could not check for overlaps"*, which is a sentence about the tool in a message
    about a merge.

    Never raises."""
    key = str(repo or "")
    if not key:
        return []
    stamp = _now(now)
    if use_cache:
        cached = _CACHE.get(key)
        if cached and stamp - cached[0] < CACHE_TTL_SEC:
            return cached[1]
    argv = ["gh", "pr", "list", "--repo", key, "--state", "open",
            "--json", LIST_FIELDS, "--limit", str(int(limit))]
    try:
        code, out, _err = (runner or _run)(argv)
        if code != 0:
            return []
        rows = json.loads(out)
    except Exception:  # noqa: BLE001 — any failure to look is a decision to say nothing
        return []
    if not isinstance(rows, list):
        return []
    rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("number"), int)]
    if use_cache:
        _CACHE[key] = (stamp, rows)
    return rows


def paths_of(row: dict) -> list:
    """The changed paths on one `gh` row, forward-slashed and stripped, in the order `gh` gave them.

    Case is left alone: GitHub is case-sensitive about paths and lowercasing here would call
    `README.md` and `readme.md` the same file, which on a case-sensitive filesystem they are not."""
    out = []
    for entry in (row or {}).get("files") or []:
        path = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(path, str) and path.strip():
            out.append(path.strip().replace("\\", "/"))
    return out


def overlaps(pr: int, paths, rows) -> list:
    """The open PRs sharing a changed path with `pr`, as
    `[{"pr", "title", "draft", "shared", "files_truncated"}]`.

    Pure: no network, no clock, no cache. `shared` is the intersection **in this PR's own path
    order**, so two pickers about the same pair name the files in a stable, reviewable order rather
    than in whatever order GitHub answered in. The list is ordered by descending overlap size, then
    ascending PR number — most-shared first, because with only three shown that is the one worth the
    line."""
    mine, seen = [], set()
    for path in paths or []:
        if isinstance(path, str) and path.strip():
            cleaned = path.strip().replace("\\", "/")
            if cleaned not in seen:
                seen.add(cleaned)
                mine.append(cleaned)
    if not mine:
        return []
    out = []
    for row in rows or []:
        number = row.get("number")
        if not isinstance(number, int) or number == int(pr):
            continue
        theirs = paths_of(row)
        shared = [p for p in mine if p in set(theirs)]
        if not shared:
            continue
        out.append({"pr": number, "title": str(row.get("title") or ""),
                    "draft": bool(row.get("isDraft")), "shared": shared,
                    "files_truncated": len(theirs) >= FILE_LIST_SUSPECT})
    out.sort(key=lambda r: (-len(r["shared"]), r["pr"]))
    return out


def find(pr: int, repo: str, paths, runner=None, now=None, use_cache=None) -> list:
    """:func:`list_open_prs` then :func:`overlaps`, and **never raises**.

    A missing repository slug answers `[]` rather than guessing: overlap is computed within one
    repository, because `#45` in two repos is two pull requests and a cross-repo path collision is
    not a merge conflict.

    **An injected `runner` bypasses the cache, and that is correctness rather than test convenience.**
    The cache is keyed by repository and knows nothing about who answered; a caller handing over its
    own lister — `merge_guard --overlap-from`, or a test — is *telling* this module what the world
    looks like, and serving it a cached real answer instead would silently ignore what it said. Pass
    `use_cache` explicitly to override in either direction."""
    try:
        if not repo:
            return []
        cached = (runner is None) if use_cache is None else bool(use_cache)
        return overlaps(int(pr), paths, list_open_prs(repo, runner=runner, now=now,
                                                      use_cache=cached))
    except Exception:  # noqa: BLE001 — see the module docstring's failure polarity
        return []


def _git_diff_paths(path: str, base: str, runner=None) -> list:
    """The paths one in-flight job's worktree has touched, relative to its own recorded base.
    **`[]` on every failure** — an unreadable worktree, a missing `git`, a bad ref — the identical
    polarity :func:`list_open_prs` holds for `gh`: this row costs nothing but itself.

    `git diff --name-only <base>` (no second ref) compares `base` against the *working tree*, so a
    committed change since the cut and an uncommitted-but-tracked edit both show; see the module
    docstring for what this deliberately still misses."""
    if not (isinstance(path, str) and path.strip() and isinstance(base, str) and base.strip()):
        return []
    argv = ["git", "-C", path, "diff", "--name-only", base]
    try:
        code, out, _err = (runner or _run)(argv)
        if code != 0:
            return []
    except Exception:  # noqa: BLE001 — a broken git is a decision to say nothing, not a crash
        return []
    return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]


def list_inflight_jobs(state_dir: str, runner=None, jobs_lister=None) -> list:
    """Every in-flight job holding a live `--worktree`, as `[{"id", "paths"}]`. **`[]` on every
    failure** — the same polarity :func:`list_open_prs` holds for `gh`: a job store that cannot be
    read costs this row, never the PR-overlap answer this module already gives.

    A job's worktree exists on disk only while the job is non-terminal — `job_worktree.teardown`
    removes or leaks it at the terminal transition, before the completion push — so
    `active_only=True` (`jobs.py`'s own "still in flight, including a job waiting out a retry
    backoff") is the correct filter, not a hand-picked set of status strings re-declared here.

    `jobs_lister` and `runner` are the two seams: the first stands in for `jobs.list_jobs` so no
    test touches a real `state/jobs/` directory, the second is the same `git`-shaped runner
    :func:`list_open_prs` uses for `gh`, reused here for `git diff`. `jobs` is imported lazily,
    exactly as `merge_guard.overlapping_prs` imports this module lazily — a broken import must cost
    this row, never the PR overlap the rest of this module already provides."""
    if not state_dir:
        return []
    try:
        if jobs_lister is None:
            import jobs as _jobs
            jobs_lister = lambda sd: _jobs.list_jobs(sd, active_only=True)
        records = jobs_lister(state_dir) or []
    except Exception:  # noqa: BLE001 — see the module docstring's failure polarity
        return []
    out = []
    fallback_base = None
    for rec in records:
        if not isinstance(rec, dict):
            continue
        job_id = rec.get("id")
        if not isinstance(job_id, str) or not job_id.strip():
            continue
        wt = rec.get("worktree")
        if not isinstance(wt, dict):
            continue
        path = wt.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        base = wt.get("base")
        if not base:
            fallback_base = fallback_base or default_job_base()
            base = fallback_base
        try:
            if not os.path.isdir(path):
                continue
        except OSError:
            continue
        paths = _git_diff_paths(path, base, runner=runner)
        if not paths:
            continue
        out.append({"id": job_id, "paths": paths})
    return out


def job_overlaps(paths, job_rows) -> list:
    """The in-flight jobs sharing a changed path with this PR, as `[{"job", "shared"}]` — the exact
    "states the fact, never predicts the merge" contract :func:`overlaps` holds for another PR,
    applied to a job's own worktree instead. Pure: no network, no clock, no git.

    Ordered the same way :func:`overlaps` is — most-shared first, then ascending job id — for the
    same reason: with only a few ever shown, that is the one worth the line."""
    mine, seen = [], set()
    for path in paths or []:
        if isinstance(path, str) and path.strip():
            cleaned = path.strip().replace("\\", "/")
            if cleaned not in seen:
                seen.add(cleaned)
                mine.append(cleaned)
    if not mine:
        return []
    out = []
    for row in job_rows or []:
        if not isinstance(row, dict):
            continue
        job_id = row.get("id")
        if not isinstance(job_id, str) or not job_id.strip():
            continue
        theirs = {p for p in (row.get("paths") or []) if isinstance(p, str)}
        shared = [p for p in mine if p in theirs]
        if not shared:
            continue
        out.append({"job": job_id, "shared": shared})
    out.sort(key=lambda r: (-len(r["shared"]), r["job"]))
    return out


def find_job_overlaps(paths, state_dir, runner=None, jobs_lister=None) -> list:
    """:func:`list_inflight_jobs` then :func:`job_overlaps`, and **never raises** — :func:`find`'s
    own contract, for the other half of the collision this module states: not only can two open PRs
    fight over the same files, landing this PR can stale-base a job that has no PR yet to be seen
    in."""
    try:
        return job_overlaps(paths, list_inflight_jobs(state_dir, runner=runner,
                                                       jobs_lister=jobs_lister))
    except Exception:  # noqa: BLE001 — see the module docstring's failure polarity
        return []


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Which open PRs change the same files as this one?")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--limit", type=int, default=PR_LIST_LIMIT)
    p.add_argument("--state-dir", default=None,
                   help="if given, also report in-flight jobs whose worktree shares a path")
    args = p.parse_args(argv)
    rows = list_open_prs(args.repo, limit=args.limit)
    mine = next((paths_of(r) for r in rows if r.get("number") == args.pr), None)
    if mine is None:
        print(json.dumps({"ok": False, "pr": args.pr, "repo": args.repo,
                          "error": "not among the open pull requests read back"},
                         ensure_ascii=False))
        return 1
    result = {"ok": True, "pr": args.pr, "repo": args.repo,
              "overlaps": overlaps(args.pr, mine, rows)}
    if args.state_dir:
        result["job_overlaps"] = find_job_overlaps(mine, args.state_dir)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
