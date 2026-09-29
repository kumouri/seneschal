#!/usr/bin/env python3
"""**Is the base branch's own TIP green, red, pending, or unknown — measured, never eyeballed.**
Stdlib + `git` + the `gh` CLI, in `pr_sweep.py`'s shape.

The module keeps its historical name: under Git Flow the integration branch is `develop`, and that is
the branch this was written to watch. The branch it actually reads is configuration —
`repo_config.base_branch()` (`../references/pr-guard.json`, else `develop` when `origin/develop`
exists, else what `origin/HEAD` names, else `main`) — and `--branch` overrides it per call.

## The failure this closes

A "CI is red on the base branch" alert built by scanning recent commits for failed runs is wrong in
two ordinary ways. A failed commit may **never have been on the branch** at all — the old head of a
pull request that is still open. Or it may be on the branch only as an **intermediate** commit inside
a merged PR: it failed, was fixed in the same PR, and the merge commit ran green. Neither is a fact
about the branch the assistant deploys off.

The defect is conflating **"some recent commit has a failed run"** with **"the branch I deploy is
red"** — two different questions, where the second has a one-line deterministic answer. This module
IS that one line, in code, so a prose turn is never the thing deciding it.

## What it measures, and what it refuses to guess

**A commit may never be described as being on the branch without proving it** — :func:`is_ancestor`
is `git merge-base --is-ancestor`, cheap, and not optional. **The verdict comes from the tip's OWN
recorded workflow runs, never from a scan of nearby commits** — :func:`fetch_runs_for_sha` asks `gh`
for every run at the tip's exact SHA (as `git`'s local view of `origin/<branch>` names it), across
every workflow that might have fired on that push: a repository with more than one workflow (a
path-filtered mobile build beside the main CI, say) cannot be judged by a `--limit 1` read, which can
name the wrong one while the other is still running or failed. An intermediate commit's own failed run
is invisible to this module by construction: it is never fetched, because it is never the tip.

**`unknown` is a real answer, not a value this guesses past.** No local fetch of `origin/<branch>`,
no `gh` on PATH, no recorded run yet for the exact tip SHA (Actions can take a few seconds to pick up
a push) — every one of these returns `unknown`, and :func:`should_alert_ci_red` treats `unknown`
exactly like `pending`: never an alert. **The asymmetry is deliberate and mirrors the never-merge-red
rule in the opposite direction**: a missed red tip costs a silent gap until the next check; a
manufactured red tip on a green branch costs the owner's attention in the middle of the night for
nothing. Never raises — a caller that wants to escalate about CI (Watch, or anything else) may not go
down because GitHub or the local `git` is unreachable.

## What this module deliberately is not

It is not a second PR-CI classifier — `watch_pr.classify` and `pr_sweep.is_green` already own "is a
pull request's rollup green" and this asks a different question (is the base branch's tip green). It
does not fetch — :func:`develop_tip_sha` reads `git rev-parse origin/<branch>` locally, so the answer
is only as fresh as this checkout's last fetch, the same posture `check_context_pointers.py` and
`check_context_budget.py` document for their own base-branch reads. It sends nothing — callers decide
what, if anything, to do with the verdict.

USAGE (diagnostics; a caller imports the functions in production):
  python develop_ci_status.py                       # verdict for origin/<base branch>'s tip, JSON
  python develop_ci_status.py --branch main
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import repo_config  # noqa: E402 — the base branch is configuration, resolved at call time

#: `gh run list`/`git` calls run inline in a Watch peek or a daemon task, never in a loop — bounded so
#: a hung call gives up rather than piling up.
CALL_TIMEOUT_SEC = 30

#: How many of the branch's most recent workflow runs to pull before filtering to the tip's own SHA.
#: Generous enough to cover every workflow that could have fired on one push — a path-filtered
#: workflow only fires on some pushes, so the newest row for a given SHA is not reliably the newest
#: row overall.
RUN_LIST_LIMIT = 20

#: Conclusions that are finished-and-fine, the same vocabulary `watch_pr._OK` uses for a PR's rollup —
#: kept as an allow-list rather than `!= "failure"` so a conclusion GitHub invents later reads as a
#: failure instead of being silently treated as a pass.
_OK_CONCLUSIONS = {"success", "neutral", "skipped"}


def _run(argv: list) -> tuple:
    """`(returncode, stdout, stderr)`. The one subprocess seam — a test replaces this, never a real
    `git`/`gh`."""
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=CALL_TIMEOUT_SEC)
    return proc.returncode, proc.stdout, proc.stderr


def _branch(branch) -> str:
    """The branch a call is about: the caller's, else the configured base branch. Resolved at call
    time, never at import, so a test (or an owner who edits `pr-guard.json`) is always honoured."""
    return branch if isinstance(branch, str) and branch.strip() else repo_config.base_branch()


def develop_tip_sha(branch: str | None = None, remote: str = "origin", runner=None) -> tuple:
    """`(sha, error)` for `<remote>/<branch>`'s current tip, read LOCALLY — never fetches, so this is
    only as fresh as the checkout's last `git fetch`. `sha is None` means the answer could not be
    determined at all (no `git`, no such ref, a bare repo with no remote configured)."""
    branch = _branch(branch)
    argv = ["git", "rev-parse", f"{remote}/{branch}"]
    try:
        code, out, err = (runner or _run)(argv)
    except FileNotFoundError:
        return None, "`git` is not on PATH"
    except subprocess.TimeoutExpired:
        return None, "`git rev-parse` timed out"
    except Exception as e:  # noqa: BLE001 — any transport failure is just a failure to look
        return None, f"`git rev-parse` failed: {e!r}"
    if code != 0:
        return None, (err or out or "git rev-parse returned non-zero").strip()[:300]
    sha = out.strip()
    if not sha:
        return None, "git rev-parse returned an empty sha"
    return sha, ""


def is_ancestor(sha: str, branch: str | None = None, remote: str = "origin", runner=None):
    """Is `sha` an ancestor of `<remote>/<branch>` — i.e., genuinely part of that branch's history?

    Returns `True`, `False`, or `None` when the question could not be answered (missing object, no
    `git`, not a repository). **`None` is not `False`** — a caller must never read "couldn't tell" as
    "not on the branch," which is the exact inversion that lets a still-open PR's old head get called
    part of the base. This is the one check that makes that description falsifiable at all, and it is
    cheap enough that there is no excuse for skipping it before naming a commit that way."""
    argv = ["git", "merge-base", "--is-ancestor", str(sha), f"{remote}/{_branch(branch)}"]
    try:
        code, _out, _err = (runner or _run)(argv)
    except Exception:  # noqa: BLE001 — unanswerable, never guessed
        return None
    if code == 0:
        return True
    if code == 1:
        return False
    return None  # a bad/unknown object, or not a repository at all — refuse to guess


def fetch_runs_for_sha(sha: str, branch: str | None = None, limit: int = RUN_LIST_LIMIT,
                       runner=None) -> tuple:
    """`(rows, error)` — every workflow run `gh` has recorded for `branch`, filtered to the ones whose
    `headSha` is exactly `sha`. `rows is None` means the list could not be read at all; `[]` means it
    was read and nothing at that SHA has run yet (or ever will — Actions can lag a push by seconds)."""
    argv = ["gh", "run", "list", "--branch", _branch(branch), "--limit", str(int(limit)),
            "--json", "headSha,status,conclusion,workflowName,createdAt"]
    try:
        code, out, err = (runner or _run)(argv)
    except FileNotFoundError:
        return None, "`gh` is not on PATH"
    except subprocess.TimeoutExpired:
        return None, "`gh run list` timed out"
    except Exception as e:  # noqa: BLE001 — any transport failure is just a failure to look
        return None, f"`gh run list` failed: {e!r}"
    if code != 0:
        return None, (err or out or "gh returned non-zero").strip()[:300]
    try:
        rows = json.loads(out)
    except (ValueError, TypeError) as e:
        return None, f"unreadable gh output: {e}"
    if not isinstance(rows, list):
        return None, f"`gh run list` returned {type(rows).__name__}, not a list"
    return [r for r in rows if isinstance(r, dict) and r.get("headSha") == sha], ""


def verdict_from_runs(rows: list) -> str:
    """Fold ONE commit's own workflow runs into `green`/`red`/`pending`/`unknown`. Pure — no clock, no
    subprocess — so this is the part a test can pin exactly.

    - `unknown`: no rows at all for this SHA — nothing has run against it yet, or nothing ever will.
    - `pending`: at least one row has not reached `completed`, checked BEFORE any conclusion is read —
      the same `done = (pending == 0)` rule `watch_pr.classify` already uses for a PR's rollup, so a
      workflow that already failed can never out-race a sibling workflow that is still running and
      might yet supersede it.
    - `red`: every row is completed, and at least one conclusion is not a pass.
    - `green`: every row is completed and passed, and there was at least one."""
    if not rows:
        return "unknown"
    if any(str(row.get("status") or "").lower() != "completed" for row in rows):
        return "pending"
    if any(str(row.get("conclusion") or "").lower() not in _OK_CONCLUSIONS for row in rows):
        return "red"
    return "green"


def develop_ci_verdict(branch: str | None = None, remote: str = "origin", runner=None) -> dict:
    """**The one measurement this module exists to provide.** Always returns a dict; never raises —
    this is meant to be called from a cheap Watch peek, which may not go down because GitHub or the
    local `git` is unreachable.

    `{"verdict": "green"|"red"|"pending"|"unknown", "branch", "tip_sha", "reason", "runs"}` — `reason`
    is only populated when `tip_sha` or `runs` could not be read; `runs` is the filtered row list
    `verdict_from_runs` folded, empty whenever the verdict is not derived from at least one row.
    `branch` defaults to the configured base branch."""
    try:
        branch = _branch(branch)
    except Exception as e:  # noqa: BLE001 — repo_config never raises, but this contract is absolute
        return {"verdict": "unknown", "branch": None, "tip_sha": None,
                "reason": f"could not resolve the base branch: {e!r}", "runs": []}
    tip, tip_err = develop_tip_sha(branch, remote, runner)
    if tip is None:
        return {"verdict": "unknown", "branch": branch, "tip_sha": None, "reason": tip_err,
                "runs": []}
    rows, run_err = fetch_runs_for_sha(tip, branch, runner=runner)
    if rows is None:
        return {"verdict": "unknown", "branch": branch, "tip_sha": tip, "reason": run_err,
                "runs": []}
    return {"verdict": verdict_from_runs(rows), "branch": branch, "tip_sha": tip, "reason": "",
            "runs": rows}


def should_alert_ci_red(result: dict) -> bool:
    """The one gate a caller needs: **may this be described to the owner as "CI is red on the base
    branch"?**

    Only ever `True` on a measured `red` tip. `pending` and `unknown` both refuse — an unresolved
    verdict is not evidence of a red one, and treating it as such is the failure this module exists to
    end. There is deliberately no third state a caller can talk itself into alerting on."""
    return isinstance(result, dict) and result.get("verdict") == "red"


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Measure whether origin/<branch>'s own tip is green, red, pending, or unknown "
                    "— never a scan of recent commits.")
    p.add_argument("--branch", default=None,
                   help="default: the configured base branch (repo_config.base_branch())")
    p.add_argument("--remote", default="origin")
    args = p.parse_args(argv)
    result = develop_ci_verdict(args.branch, args.remote)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
