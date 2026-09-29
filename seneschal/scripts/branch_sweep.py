#!/usr/bin/env python3
"""**Delete the remote branches whose pull request has merged and which nothing is stacked on.**
Stdlib + the `gh` CLI. The other half of `branch_delete_guard.py`, and it shares that module's
predicate rather than owning one.

## Why this exists, and the one correction it carries

The obvious fix for *"`--delete-branch` closed a stacked pull request"* is to stop passing
`--delete-branch` and let a periodic pass clean up instead. That is the right first move and it is
**not the fix**. A sweep with no predicate is the same deletion on a timer: it takes out a base
branch **unattended**, closes the dependent with nobody watching, and the quick hand recovery
that a person at a terminal would make does not happen because nobody is looking. **The scheduling
was never the fix. The predicate is the fix** — so this module imports
:func:`branch_delete_guard.open_prs_based_on` and there is no second implementation anywhere in this
tree that could come to a different answer than the hook does.

## Two bounds, and why they are there

A repository that has merged with `--delete-branch` off for a while carries a large backlog: nearly
every head branch of a merged pull request is still on the remote, and almost none has anything
stacked on it. A first run that quietly removed hundreds of remote branches would be this feature's
own version of the failure it was written to prevent: a large irreversible-looking act nobody asked
for, performed correctly. Hence:

1. **Dry-run is the DEFAULT.** `--apply` is required to delete anything. This does not contradict
   act-low — a branch deletion that passes the predicate is the assistant's to make without asking — it means
   the *acting* invocation is one somebody typed on purpose. `pr_sweep.py` can afford `--dry-run` as
   the opt-in because its act is sending a question; this one's act does not come back.
2. **`--limit` caps deletions per run** (default :data:`DEFAULT_LIMIT`) and **a capped run names what
   it left behind** — `pr_sweep.py`'s bound 4, for the same reason: a silent truncation reads as
   "covered everything" when it did not.

`--merged-within DAYS` is the third dial and has no default, deliberately. A window would make the
backlog invisible rather than bounded, and the number of old branches on the remote is a fact
the owner should see once rather than a number this script quietly picks for them.

## The predicate is re-read PER BRANCH, immediately before each deletion

The bulk lists (`gh pr list --state merged`, `--state open`) are how candidates are *found*; they are
never how a deletion is *authorised*. Between building the list and reaching branch #40, someone can
open a pull request against branch #40 — and a sweep that trusted its opening snapshot would delete
the base of a pull request that existed by the time it acted. So each deletion is preceded by its own
:func:`branch_delete_guard.open_prs_based_on` call. One extra API call per branch actually deleted,
which is the cheapest possible place to spend it and the only place it is worth anything.

## What it refuses to do

* **It never deletes a branch that is not the head of a MERGED pull request.** An unmerged branch may
  be somebody's work in progress with no PR open yet, and this module cannot tell those apart.
* **It never deletes a protected branch** (:func:`branch_delete_guard.protected_branches` — the
  guard's set, not a copy: the fixed floor plus `pr-guard.json`'s additions) **or the base branch**
  (`--base-branch`, else `repo_config.base_branch()`: the configured integration branch, else `develop`
  when `origin/develop` exists, else `origin/HEAD`'s branch, else `main`).
* **It never deletes on an unknown.** A branch whose predicate could not be evaluated — `gh` failed,
  GitHub unreachable, an unparseable answer — is **skipped and reported**, never swept. This is the
  one place where the hook's fail-open-on-unreachable does *not* carry over: the hook allows an
  outage because a human is standing there watching the command; this runs alone, and "I could not
  check" must never become "so I deleted it".
* **It never merges, approves, or touches a pull request in any way.** It deletes refs.

USAGE:
  python branch_sweep.py                                  # dry run over every candidate
  python branch_sweep.py --merged-within 7                # only PRs merged in the last week
  python branch_sweep.py --apply --limit 10               # actually delete, at most ten
  python branch_sweep.py --apply --branch docs/one-thing  # one named branch, still predicated
  python branch_sweep.py --repo example/repo --base-branch trunk   # another repo, its own base
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling imports work when imported, not only when run

# THE PREDICATE, and the protected set, and the three outcome exceptions. Imported, never re-derived
# — see the module docstring. A sweep whose idea of "safe" can drift from the hook's is the bug.
import branch_delete_guard as bdg  # noqa: E402
import repo_config  # noqa: E402 — the base branch is owner config, not code


#: How many branches one run may delete. Deliberately far below a typical backlog: the first
#: `--apply` should be a thing somebody watches finish, and clearing a historical backlog should take
#: a few deliberate runs rather than one unattended one.
DEFAULT_LIMIT = 25

#: How many pull requests the bulk finder will page in. Generous — a sweep that silently stops
#: seeing PRs past a boundary would report a branch as "no merged PR" and skip it forever — but
#: finite, and :func:`sweep` says when a list came back at the limit.
PR_LIST_LIMIT = 1000
OPEN_LIST_LIMIT = 200

#: `git ls-remote` is a network call against the remote itself rather than the API.
LS_REMOTE_TIMEOUT_SEC = 60

_HEADS_PREFIX = "refs/heads/"


def _run(argv: list, cwd=None, timeout: int = bdg.GH_TIMEOUT_SEC) -> tuple:
    """`(returncode, stdout, stderr)`. One seam, replaced wholesale by the tests."""
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=cwd or None)
    return proc.returncode, proc.stdout, proc.stderr


class SweepError(Exception):
    """A whole-run failure: the candidate list could not be built. Nothing has been deleted."""


def _gh_list(state: str, fields: str, repo=None, limit: int = PR_LIST_LIMIT,
             cwd=None, runner=None) -> list:
    argv = ["gh", "pr", "list", "--state", state, "--json", fields, "--limit", str(limit)]
    if repo:
        argv += ["--repo", repo]
    run = runner or _run
    try:
        rc, out, err = run(argv, cwd)
    except FileNotFoundError:
        raise SweepError("the `gh` CLI is not on PATH")
    except subprocess.TimeoutExpired:
        raise SweepError(f"`gh pr list --state {state}` did not answer in time")
    except OSError as exc:
        raise SweepError(f"could not run `gh`: {exc!r}")
    if rc != 0:
        raise SweepError(f"`gh pr list --state {state}` failed: {(err or out).strip()}")
    try:
        rows = json.loads(out or "[]")
    except (TypeError, ValueError):
        raise SweepError(f"`gh pr list --state {state}` returned output that is not JSON")
    if not isinstance(rows, list):
        raise SweepError(f"`gh pr list --state {state}` returned {type(rows).__name__}, not a list")
    return rows


def remote_branches(remote: str = "origin", cwd=None, runner=None) -> set:
    """The branch names that actually exist on `remote`.

    Read from `git ls-remote` rather than from the pull-request list, because the question being
    answered is *"is there a ref here to delete?"* — and a branch already deleted by hand must not
    come back as a candidate every run forever."""
    run = runner or _run
    try:
        rc, out, err = run(["git", "ls-remote", "--heads", remote], cwd, LS_REMOTE_TIMEOUT_SEC)
    except FileNotFoundError:
        raise SweepError("`git` is not on PATH")
    except subprocess.TimeoutExpired:
        raise SweepError(f"`git ls-remote --heads {remote}` did not answer in time")
    except OSError as exc:
        raise SweepError(f"could not run `git ls-remote`: {exc!r}")
    if rc != 0:
        raise SweepError(f"`git ls-remote --heads {remote}` failed: {(err or out).strip()}")
    names = set()
    for line in (out or "").splitlines():
        _, _, ref = line.partition("\t")
        ref = ref.strip()
        if ref.startswith(_HEADS_PREFIX):
            names.add(ref[len(_HEADS_PREFIX):])
    return names


def _parse_stamp(raw):
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def resolve_base(base=None, cwd=None) -> str:
    """The base (integration) branch the sweep must never delete: `base` when given, else
    `repo_config.base_branch()`. Never raises — `repo_config` degrades to `main`."""
    if isinstance(base, str) and base.strip():
        return base.strip()
    try:
        return repo_config.base_branch(cwd=cwd)
    except Exception:  # noqa: BLE001 — repo_config never raises; belt and braces for a sweep
        return "main"


def protected_set(base=None, cwd=None) -> frozenset:
    """The guard's protected branches plus the base branch."""
    return bdg.protected_branches() | {resolve_base(base, cwd)}


def candidates(repo=None, remote: str = "origin", merged_within=None, cwd=None,
               runner=None, now=None, base=None) -> dict:
    """Find the branches a sweep would consider, and say why each other one was excluded.

    Returns `{"candidates": [...], "excluded": {reason: [branch, ...]}, "counts": {...}}`. The
    exclusions are returned rather than dropped because the interesting output of a dry run is
    usually *"why is this branch still here"*, and a filter that answers only in the negative makes
    that question cost a second investigation.

    `base` names the base branch (excluded as protected); `None` resolves it via
    :func:`resolve_base`."""
    now = now or datetime.now(timezone.utc)
    protected = protected_set(base, cwd)
    live = remote_branches(remote, cwd=cwd, runner=runner)
    merged = _gh_list("merged", "number,headRefName,mergedAt,title", repo=repo,
                      limit=PR_LIST_LIMIT, cwd=cwd, runner=runner)
    open_prs = _gh_list("open", "number,headRefName,baseRefName", repo=repo,
                        limit=OPEN_LIST_LIMIT, cwd=cwd, runner=runner)

    open_bases = {p.get("baseRefName") for p in open_prs if p.get("baseRefName")}
    open_heads = {p.get("headRefName") for p in open_prs if p.get("headRefName")}

    latest = {}
    for pr in merged:
        head, stamp = pr.get("headRefName"), _parse_stamp(pr.get("mergedAt"))
        if not head or stamp is None:
            continue
        if head not in latest or stamp > latest[head][0]:
            latest[head] = (stamp, pr)

    cutoff = now - timedelta(days=merged_within) if merged_within else None
    out, excluded = [], {"protected": [], "open-base": [], "open-head": [],
                         "no-merged-pr": [], "outside-window": []}
    for branch in sorted(live):
        if branch in protected:
            excluded["protected"].append(branch)
        elif branch in open_bases:
            excluded["open-base"].append(branch)
        elif branch in open_heads:
            excluded["open-head"].append(branch)
        elif branch not in latest:
            excluded["no-merged-pr"].append(branch)
        elif cutoff is not None and latest[branch][0] < cutoff:
            excluded["outside-window"].append(branch)
        else:
            stamp, pr = latest[branch]
            out.append({"branch": branch, "pr": pr.get("number"),
                        "merged_at": pr.get("mergedAt"), "title": pr.get("title") or ""})
    out.sort(key=lambda c: (c["merged_at"] or ""), reverse=True)
    return {"candidates": out, "excluded": excluded,
            "counts": {"remote_branches": len(live), "merged_prs": len(merged),
                       "open_prs": len(open_prs), "candidates": len(out),
                       "merged_list_at_limit": len(merged) >= PR_LIST_LIMIT}}


def delete_branch(branch: str, repo: str, cwd=None, runner=None) -> None:
    """Delete one ref. Raises on anything that is not a clean deletion.

    `gh api … -X DELETE` rather than `git push --delete` because the repository is then named in the
    request instead of inferred from whatever remote config the caller happens to have."""
    argv = ["gh", "api", f"repos/{repo}/git/refs/heads/{branch}", "-X", "DELETE"]
    run = runner or _run
    rc, out, err = run(argv, cwd)
    if rc != 0:
        raise SweepError(f"deleting `{branch}` failed: {(err or out).strip()}")


def sweep(repo=None, remote: str = "origin", apply: bool = False, limit: int = DEFAULT_LIMIT,
          merged_within=None, only=None, cwd=None, runner=None, now=None, base=None) -> dict:
    """The pass. Returns a result dict; raises :class:`SweepError` only when no candidate list could
    be built at all, which is the one failure that means nothing was even considered.

    **Per-branch outcomes are isolated from each other.** One branch whose predicate cannot be
    evaluated must not stop the pass, and — more importantly — must not be deleted on the way past.
    """
    found = candidates(repo=repo, remote=remote, merged_within=merged_within,
                       cwd=cwd, runner=runner, now=now, base=base)
    rows = found["candidates"]
    if only:
        wanted = set(only)
        rows = [r for r in rows if r["branch"] in wanted]
        missing = sorted(wanted - {r["branch"] for r in rows})
    else:
        missing = []

    deleted, skipped, deferred, errors = [], [], [], []
    for row in rows:
        branch = row["branch"]
        if len(deleted) + len(skipped) >= limit:
            deferred.append(branch)
            continue
        # THE PREDICATE, re-read per branch immediately before acting. See the module docstring.
        try:
            blockers = bdg.open_prs_based_on(branch, repo=repo, cwd=cwd, runner=runner)
        except bdg.NotGitHub as exc:
            errors.append({"branch": branch, "why": f"not a GitHub repository: {exc}"})
            continue
        except (bdg.Unreachable, bdg.GuardError) as exc:
            # The hook allows an outage because somebody is watching the command it allowed. This
            # runs alone: "could not check" is never "so I deleted it".
            errors.append({"branch": branch, "why": f"predicate could not be evaluated: {exc}"})
            continue
        if blockers:
            skipped.append({"branch": branch,
                            "blocked_by": [p.get("number") for p in blockers],
                            "detail": blockers})
            continue
        if not apply:
            deleted.append({"branch": branch, "pr": row["pr"], "dry_run": True})
            continue
        target = repo or _resolve_repo(remote, cwd=cwd, runner=runner)
        try:
            delete_branch(branch, target, cwd=cwd, runner=runner)
        except SweepError as exc:
            errors.append({"branch": branch, "why": str(exc)})
            continue
        deleted.append({"branch": branch, "pr": row["pr"], "dry_run": False})

    return {"repo": repo, "apply": apply, "limit": limit,
            "deleted": deleted, "skipped": skipped, "deferred": deferred,
            "errors": errors, "not_a_candidate": missing,
            "excluded": found["excluded"], "counts": found["counts"]}


def _resolve_repo(remote: str, cwd=None, runner=None) -> str:
    """`origin` -> `owner/name`, via the guard's own resolver so a deletion and the predicate that
    cleared it are aimed at the same repository."""
    repo = bdg.repo_for_remote(remote, cwd=cwd, runner=runner)
    if not repo:
        raise SweepError(f"remote {remote!r} is not on github.com, so nothing here can be deleted "
                         f"through the GitHub API")
    return repo


def render(result: dict) -> str:
    """The human report. A dry run and a real run print the same shape on purpose — the difference
    should be one word, so nobody reads one as the other."""
    counts = result["counts"]
    mode = "APPLY" if result["apply"] else "DRY RUN (nothing deleted; pass --apply to act)"
    lines = [f"branch sweep — {mode}",
             f"  remote branches {counts['remote_branches']} · merged PRs {counts['merged_prs']} · "
             f"open PRs {counts['open_prs']} · candidates {counts['candidates']}"]
    if counts.get("merged_list_at_limit"):
        lines.append(f"  ! the merged-PR list came back at its {PR_LIST_LIMIT}-row limit; older "
                     f"branches may be under-reported")
    verb = "would delete" if not result["apply"] else "deleted"
    lines.append(f"  {verb}: {len(result['deleted'])}")
    for row in result["deleted"]:
        lines.append(f"    - {row['branch']}  (PR #{row['pr']})")
    if result["skipped"]:
        lines.append(f"  REFUSED by the predicate — an open PR is stacked on these: "
                     f"{len(result['skipped'])}")
        for row in result["skipped"]:
            lines.append(f"    - {row['branch']}  <- open PR(s) "
                         f"{', '.join('#%s' % n for n in row['blocked_by'])}")
    if result["errors"]:
        lines.append(f"  NOT SWEPT, could not be checked or deleted: {len(result['errors'])}")
        for row in result["errors"]:
            lines.append(f"    - {row['branch']}: {row['why']}")
    if result["deferred"]:
        lines.append(f"  held over by --limit {result['limit']}: {len(result['deferred'])} "
                     f"(run again to continue)")
    if result["not_a_candidate"]:
        lines.append("  named with --branch but not a candidate (no merged PR, protected, or an "
                     "open PR uses it): " + ", ".join(result["not_a_candidate"]))
    excluded = result["excluded"]
    detail = " · ".join(f"{reason} {len(names)}" for reason, names in excluded.items() if names)
    lines.append("  excluded: " + (detail or "nothing"))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete merged branches that no open pull request is stacked on.")
    parser.add_argument("--repo", default=None, help="owner/name; default: resolved from the remote")
    parser.add_argument("--remote", default="origin")
    # `--base-branch`, not `--base`: the only quoted base-flag string in this tree belongs to the
    # guard's predicate (`gh pr list --base`), and test_branch_delete_guard pins that.
    parser.add_argument("--base-branch", default=None,
                        help="the base branch, never swept; default: repo_config.base_branch()")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete. Without this the run is a dry run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="explicit no-op default; accepted so a cautious invocation is spellable")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--merged-within", type=int, default=None, metavar="DAYS")
    parser.add_argument("--branch", action="append", default=None,
                        help="restrict to these branches (repeatable); still fully predicated")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    apply = args.apply and not args.dry_run
    try:
        result = sweep(repo=args.repo, remote=args.remote, apply=apply, limit=args.limit,
                       merged_within=args.merged_within, only=args.branch, cwd=args.cwd,
                       base=args.base_branch)
    except SweepError as exc:
        print(f"branch sweep: could not build a candidate list, nothing was deleted.\n  {exc}",
              file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str) if args.json else render(result))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
