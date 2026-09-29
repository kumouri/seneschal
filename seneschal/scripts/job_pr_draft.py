#!/usr/bin/env python3
"""**A pull request opened by a still-running job is a draft until that job finishes.** Stdlib + `gh`
+ `git`.

## Why it exists

A delegated job can run `gh pr create` while it is still working, then push more commits. In between,
anything that sweeps open PRs for merge conflicts or rebase candidates sees the PR at a head that is
about to stop existing, and may launch a repair job that (correctly) refuses because the branch moved
since detection. A PR still being written should never be treated as a merge/repair candidate at all,
and the refused repair still costs the owner a pointless job notice.

**Nothing here trusts a job brief to remember `--draft`.** The invariant is enforced from the READ
side: a PR's branch is checked out in a running job's worktree or it isn't, and that fact is
observable independent of how the PR was created. A prose instruction to an agent does not bind
reliably; this is code.

## What it does

- :func:`list_running_worktree_branches` — every branch a currently-RUNNING `--worktree` job has
  checked out, `{branch: job_id}`. A PR sweeper calls :func:`inflight_job_for` against this map and
  skips a matching PR exactly as it skips a draft — before ever asking about it, repairing it or
  rebasing it.
- :func:`draft_and_record` — the first time an in-flight PR is seen still open (not yet a draft),
  convert it (`gh pr ready --undo`) and record the conversion in the ONE store this module owns,
  `state/job-drafted-prs.json`, keyed `job_id` so the record survives past the git worktree being torn
  down. Called by whichever sweep pass already mutates PRs (a read-only pass must stay read-only).
- :func:`resolve_for_job` — called once from `jobs.py`'s `reconcile`, at a job's terminal transition:
  every PR this mechanism drafted *for that job* is marked ready again (`gh pr ready`) if the job ended
  `done` with exit code 0, and left a draft otherwise. **The record, not a live re-check of the
  worktree, is what answers "did THIS mechanism draft it"** — a PR a human or an agent marked draft for
  its own reasons never has an entry here and is never touched.

The repository is always the caller's `repo` argument (`owner/name`), never a constant here: the
framework runs from whatever fork or clone the owner installed.

## Failure polarity

Every function here is fail-open: an unreadable job ledger, a missing `git`/`gh`, or a broken worktree
path costs this module's answer, never the caller's pass. A `gh` failure while drafting or un-drafting
is reported by the caller (a deferred pass for the sweeper, a `"ready-failed"` action for `jobs.py`'s
completion push) and retried on the next pass — never silently swallowed into "nothing happened".
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from sentinel import load_json, save_json  # noqa: E402 — atomic JSON, Windows PermissionError retry

STORE_FILE = "job-drafted-prs.json"
STORE_SCHEMA = "seneschal.job-pr-draft/1"

GH_TIMEOUT_SEC = 60


def _run(argv: list) -> tuple:
    """`(returncode, stdout, stderr)`. The one subprocess seam, so a test replaces one thing and
    every caller here — `git` or `gh` — passes a bare `argv` list."""
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=GH_TIMEOUT_SEC)
    return proc.returncode, proc.stdout, proc.stderr


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _key(repo: str, pr: int) -> str:
    return f"{repo}#{int(pr)}"


# --------------------------------------------------------------------------- the store

def store_path(state_dir: str) -> str:
    return os.path.join(state_dir, STORE_FILE)


def load_store(state_dir: str) -> dict:
    """`{"schema", "entries": {"<repo>#<pr>": {...}}}`. Never raises — a missing or corrupt file reads
    as empty, which is the safe direction: it costs a re-draft attempt at worst (failure polarity),
    never a wrong un-draft."""
    data = load_json(store_path(state_dir), {"schema": STORE_SCHEMA, "entries": {}})
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"schema": STORE_SCHEMA, "entries": {}}
    return data


def _save_store(state_dir: str, data: dict) -> None:
    try:
        save_json(store_path(state_dir), data)
    except Exception:  # noqa: BLE001 — a state writer is fail-open; the `gh` call already landed
        pass


# --------------------------------------------------------------------------- in-flight detection

def list_running_worktree_branches(state_dir: str, *, runner=None, jobs_lister=None) -> dict:
    """`{branch: job_id}` for every active `--worktree` job whose worktree still exists on disk and is
    on a real (non-detached) branch. `{}` on every failure — see the module docstring's failure
    polarity.

    `jobs_lister` and `runner` are the two seams: the first stands in for `jobs.list_jobs` so no test
    touches a real `state/jobs/` directory, the second is the tuple-returning git seam every caller in
    this module uses. `jobs` is imported lazily — a broken import must cost this map, never the
    caller's whole pass."""
    if not state_dir:
        return {}
    try:
        if jobs_lister is None:
            import jobs as _jobs
            jobs_lister = lambda sd: _jobs.list_jobs(sd, active_only=True)
        records = jobs_lister(state_dir) or []
    except Exception:  # noqa: BLE001 — see the module docstring's failure polarity
        return {}
    run = runner or _run
    out = {}
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
        try:
            if not os.path.isdir(path):
                continue
        except OSError:
            continue
        branch = _current_branch(path, runner=run)
        if branch:
            out[branch] = job_id
    return out


def _current_branch(path: str, *, runner) -> str | None:
    """The checked-out branch name, or `None` on detached HEAD or any read failure — a freshly-created
    job worktree is detached until the agent runs its own `checkout -b` (`job_worktree.py`'s own
    docstring), so `None` here is common and not an error."""
    argv = ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"]
    try:
        code, out, _err = runner(argv)
    except Exception:  # noqa: BLE001
        return None
    if code != 0:
        return None
    name = (out or "").strip()
    return None if name in ("", "HEAD") else name


def inflight_job_for(row: dict, branches: dict) -> str | None:
    """Pure: does `row`'s `headRefName` match a branch a running job's worktree is checked out on?
    Returns the job id, or `None`. No network, no clock, no git."""
    if not isinstance(row, dict) or not isinstance(branches, dict):
        return None
    branch = row.get("headRefName")
    if not isinstance(branch, str) or not branch.strip():
        return None
    return branches.get(branch)


# --------------------------------------------------------------------------- drafting / un-drafting

def draft_and_record(state_dir: str, *, repo: str, pr: int, branch: str, job_id: str,
                     runner=None, now: datetime | None = None) -> bool:
    """Convert an open, non-draft PR to a draft because its branch is checked out in a running job's
    worktree, and record that WE did it. Returns whether the `gh` call itself succeeded.

    Fail-open: a `gh` failure changes and records nothing, so the caller's next pass simply tries
    again — it can never be worse than "still not a draft yet", which is the state before this call
    ran. A store-write failure after a successful `gh` call is swallowed the same way (failure
    polarity): the PR is correctly a draft either way, and the worst case is that `resolve_for_job`
    later has no record to un-draft it from — the same "left a draft" outcome every other fail-open
    path here accepts."""
    run = runner or _run
    argv = ["gh", "pr", "ready", str(int(pr)), "--repo", repo, "--undo"]
    try:
        code, _out, _err = run(argv)
    except Exception:  # noqa: BLE001
        return False
    if code != 0:
        return False
    store = load_store(state_dir)
    store["entries"][_key(repo, pr)] = {
        "repo": repo, "pr": int(pr), "branch": branch, "job_id": job_id,
        "drafted_at": _stamp(now),
    }
    _save_store(state_dir, store)
    return True


def entries_for_job(state_dir: str, job_id: str) -> list:
    """Every PR this mechanism is holding draft on behalf of `job_id`. Read-only; used by tests and
    diagnostics — `resolve_for_job` does its own read/write in one place so the two can't race."""
    store = load_store(state_dir)
    return [dict(v) for v in store.get("entries", {}).values()
            if isinstance(v, dict) and v.get("job_id") == job_id]


def resolve_for_job(state_dir: str, job_id: str, *, success: bool, runner=None,
                    now: datetime | None = None) -> list:
    """Called once, at a job's terminal transition (`jobs.py`'s `reconcile`, guarded by `notified_at`
    exactly like the completion push itself). For every PR this mechanism drafted for `job_id`: mark
    it ready again if the job ended cleanly (`success` — `done` with exit code 0), else leave it a
    draft. Either way the job→PR association in the store is spent on a successful `gh` call; a `gh`
    failure on the un-draft side leaves the entry so a later attempt (a retry, or a hand-run `gh pr
    ready`) can still resolve it.

    Returns one `{"repo", "pr", "action"}` dict per PR — `action` is `"readied"`, `"left-draft"`, or
    `"ready-failed"` (gh unreachable; the draft is left standing). Never raises: a raise here must not
    be able to cost the completion push, so every `gh` call is in its own `try`."""
    run = runner or _run
    store = load_store(state_dir)
    entries = store.get("entries", {})
    results = []
    changed = False
    for key, entry in list(entries.items()):
        if not isinstance(entry, dict) or entry.get("job_id") != job_id:
            continue
        repo, pr = entry.get("repo"), entry.get("pr")
        if not success:
            results.append({"repo": repo, "pr": pr, "action": "left-draft"})
            del entries[key]
            changed = True
            continue
        argv = ["gh", "pr", "ready", str(int(pr)), "--repo", repo]
        try:
            code, out, err = run(argv)
        except Exception as e:  # noqa: BLE001 — a broken seam is a reported failure, never a raise
            results.append({"repo": repo, "pr": pr, "action": "ready-failed", "detail": f"{e!r}"})
            continue
        if code == 0:
            results.append({"repo": repo, "pr": pr, "action": "readied"})
            del entries[key]
            changed = True
        else:
            detail = (err or out or "gh returned non-zero").strip()[:200]
            results.append({"repo": repo, "pr": pr, "action": "ready-failed", "detail": detail})
    if changed:
        _save_store(state_dir, store)
    return results
