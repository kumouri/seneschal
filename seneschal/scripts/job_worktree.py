#!/usr/bin/env python3
"""One private git worktree per delegated job. Stdlib only.

**The problem this exists for.** `jobs.py` defaults a job's working directory to `REPO_ROOT` — which
is derived from `jobs.py`'s own file location, so for the daemon it is *always* the live checkout.
Several delegated jobs running concurrently in one shared tree on different branches, or one launched
with no `--cwd` straight into the daemon's checkout, collide in ways that are not errors at the moment
of damage:

  * a commit landing on **another session's branch** succeeds;
  * a push of a branch that has no commits succeeds — `Everything up-to-date`, exit 0.

The first symptom tends to surface minutes later, somewhere else, against a branch belonging to a
session that never knew. **The unit of collision is the checkout, not the branch** — distinct branch
names do not help when the HEAD/index/tree underneath them is shared.

So the goal is not fewer collisions; it is that the failure becomes **impossible or loud**. A worktree
removes the shared mutable HEAD/index/tree entirely, and where `git worktree add` can still fail (a
path that exists, a branch checked out elsewhere) it fails with a non-zero exit and a message, never
with a no-op.

**This is plumbing, not invention.** A transient worktree off `origin/develop` is the standard answer
to this hazard; the delegated-job path simply adopts it. What was wrong was the default, not the
mechanism.

Four things this module refuses to get wrong:

1. **`origin/develop`, spelled in full, never a bare `develop`.** A checkout with more than one
   remote (a vendored subtree, a fork) can carry a `develop` on each, and a bare ref then dies with
   *"'develop' matched multiple remote tracking branches"*.
2. **`-c core.fsmonitor=false` on every git call.** A filesystem monitor can hang git in a
   long-running daemon's checkout; a job wrapper must never wait on one.
3. **Fetch before the add.** A worktree cut from the host checkout's HEAD inherits whatever branch
   that tree was parked on — which is how a fresh worktree ends up many commits stale, measuring
   branch drift instead of the change. A fetch that fails is FATAL here rather than a warning:
   cutting from a stale base is the failure the fetch exists to prevent, and it is silent.
4. **`git worktree remove`, NEVER `--force`.** Plain `remove` refuses when the tree is dirty or holds
   unpushed commits, and **that refusal is the feature.** A job that ends badly is exactly when its
   worktree is most likely to still hold something. So a refused removal is a **leak** — left on
   disk, recorded on the job (`worktree_leaked`), and named in the completion push. A leaked
   directory costs a few megabytes; lost work is not recoverable.

**Where they live:** `<repos>/seneschal-worktrees/<job-id>/` — a sibling of the checkout, derived
from this file's location and never hardcoded, overridable with `$SENESCHAL_WORKTREE_ROOT`. A
sibling rather than a nested `.claude/worktrees/`: a nested tree is hidden from `git status` only by
an untracked, per-clone `.git/info/exclude` line a re-clone would not recreate, and
`rag_projects.find_repos` excludes by **exact directory name**, so per-job names can't be excluded
but one fixed-name parent can. Named after the **job id**, not the branch: the id sorts by start
time, it is what the record and the log are already named after, and it is knowable *before* the
agent picks a branch.

**Detached, not on a new branch.** The delegated agent runs its own `git checkout -b` inside its
private tree exactly as its brief already tells it to, so adoption costs no change to how briefs are
written and leaves no wrapper-created branch to clean up.

**No real `git` in CI.** Every call goes through an injected `Runner`, the same seam
`cockpit/breakglass/actions.py` uses. `test_job_worktree.py` spawns nothing.

Design + rationale: `seneschal/docs/delegated-work-isolation-spec.md` (phase 1).
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Derived from this file's location, exactly like `jobs.REPO_ROOT` — the host repo is whichever
# checkout the launching process loaded these modules from (no new path constant, correct on a
# rebuilt box).
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))

# THE spelling. `origin/develop`, never `develop` — see the module docstring's point 1. Split into
# remote + branch for the fetch, which takes them as two arguments.
BASE_REMOTE = "origin"
BASE_BRANCH = "develop"
BASE_REF = f"{BASE_REMOTE}/{BASE_BRANCH}"
# Non-negotiable on every git invocation in this repo.
GIT_FLAGS = ("-c", "core.fsmonitor=false")

WORKTREE_DIRNAME = "seneschal-worktrees"
WORKTREE_ROOT_ENV = "SENESCHAL_WORKTREE_ROOT"

# How long a leaked worktree is left before the sweep tries again. 14 days, matching
# `jobs.RETENTION_DAYS` — a leaked tree costs a few megabytes, so this is a tidiness number, not a
# capacity one.
LEAK_RETENTION_DAYS = 14

# A leak reason is quoted into the completion push, and `notify_text` joins its parts with newlines —
# so an embedded newline would make the tail of a git error indistinguishable from the next part of
# the message. Same lesson, same fix as `jobs.normalise_why` (`docs/cancel-attribution-spec.md`).
_REASON_CHARS = 200


class WorktreeError(RuntimeError):
    """Setup failed and the job must NOT run. Carries git's own message, because the alternative to a
    loud failure here is a job silently running in the wrong directory — the thing this module
    exists to make impossible."""


class Runner:
    """Thin subprocess seam, so the command sequence is assertable without spawning git.

    `check` defaults to **False**: every caller here inspects `returncode`
    itself in order to put git's own stderr into the error or the leak reason, and a
    `CalledProcessError` would throw that text away at exactly the moment it is worth most."""

    def __init__(self, cwd: str | None = None):
        self.cwd = cwd

    def run(self, args: list, cwd: str | None = None,
            check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(args, cwd=cwd or self.cwd, check=check,
                              capture_output=True, text=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def one_line(text, limit: int = _REASON_CHARS) -> str:
    """Collapse anything to one bounded line — see `_REASON_CHARS`. Total by construction: a `None`,
    a `CompletedProcess` field that came back as bytes, a twelve-line git error all come out as one
    short string."""
    if text is None:
        return ""
    try:
        collapsed = " ".join(str(text).split())
    except Exception:  # noqa: BLE001 — a weird __str__ must never strand a job
        return ""
    return collapsed[: max(0, int(limit))].rstrip()


def _git_message(res) -> str:
    """git's own words for a failure: stderr, falling back to stdout, then to the exit code."""
    for stream in ("stderr", "stdout"):
        text = one_line(getattr(res, stream, "") or "")
        if text:
            return text
    return f"exit {getattr(res, 'returncode', '?')}"


def _git(runner, host: str, *args) -> subprocess.CompletedProcess:
    """One git call in the host checkout, always with `GIT_FLAGS`. **The single place git is spelled**,
    so "every call carries `-c core.fsmonitor=false`" is one assertion rather than N."""
    return runner.run(["git", *GIT_FLAGS, *args], cwd=host)


def worktree_root(root: str | None = None) -> str:
    """`<repos>/seneschal-worktrees/`, or `$SENESCHAL_WORKTREE_ROOT`. Derived from the checkout's
    location, never hardcoded, so it is correct on any box and any clone path."""
    if root:
        return os.path.abspath(root)
    env = (os.environ.get(WORKTREE_ROOT_ENV) or "").strip()
    if env:
        return os.path.abspath(env)
    return os.path.normpath(os.path.join(os.path.dirname(REPO_ROOT), WORKTREE_DIRNAME))


def worktree_path(job_id: str, root: str | None = None) -> str:
    """Where THIS job's tree goes. Named after the job id: it already sorts by start time, the
    record and the log are named after it, and it is knowable before the agent picks a branch."""
    return os.path.join(worktree_root(root), str(job_id))


def create(job_id: str, *, root: str | None = None, host: str | None = None,
           runner=None, now: datetime | None = None) -> dict:
    """Cut a private worktree for `job_id`, detached at `origin/develop`. Returns the block to store
    on the job record. **Raises `WorktreeError` on any failure** — the caller must fail the job rather
    than fall back to a shared checkout, which is the whole point.

    Order is load-bearing: **fetch, then add.** A cut taken before the fetch is a cut from whatever
    `origin/develop` this checkout last saw, and a stale base looks exactly like a clean one until the
    diff is wrong.

    An existing path is refused rather than reused. A directory already sitting at a job's path is
    either a leak that still holds work or somebody else's tree; reusing it would reintroduce the
    shared-mutable-state failure at the level of a single directory."""
    runner = runner or Runner()
    host = os.path.abspath(host or REPO_ROOT)
    now = now or _utc_now()
    path = worktree_path(job_id, root)

    if os.path.exists(path):
        raise WorktreeError(
            f"{path} already exists — refusing to reuse it. If it is a leaked worktree, check what "
            "is in it and remove it by hand (`git worktree remove`, never --force).")

    res = _git(runner, host, "fetch", BASE_REMOTE, BASE_BRANCH)
    if res.returncode != 0:
        # Fatal, not a warning: the fetch is the only thing standing between this job and a cut from
        # a stale base, and a stale base is silent.
        raise WorktreeError(f"git fetch {BASE_REMOTE} {BASE_BRANCH} failed: {_git_message(res)}")

    try:
        os.makedirs(worktree_root(root), exist_ok=True)
    except OSError as e:
        raise WorktreeError(f"cannot create the worktree root: {e}") from e

    res = _git(runner, host, "worktree", "add", "--detach", path, BASE_REF)
    if res.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {_git_message(res)}")

    return {"path": path, "base": BASE_REF, "host": host, "created_at": _stamp(now),
            "removed": False, "leaked": False}


def teardown(rec: dict, *, runner=None, host: str | None = None,
             now: datetime | None = None) -> dict | None:
    """Give the job's worktree back, or leave it and say so. Returns
    `{path, removed, leaked, reason}`, or **None** when the record has no worktree (every job that
    didn't ask for one). Never raises: the completion push is downstream of this and must not be
    reachable-only-on-success.

    **`git worktree remove`, never `--force`.** A refusal means the tree is dirty or holds unpushed
    commits — precisely the state a job that failed, timed out or was killed tends to end in — and
    the refusal is what implements "never delete uncommitted work silently". So a refused removal is
    a leak: `leaked: True`, git's own reason, the directory untouched.

    An already-absent directory is not a leak. Its administrative record is tidied with
    `git worktree prune`, which is the correct tool for exactly that case and no other: `prune` drops
    admin records whose directory is **already gone** and is blind to a leaked directory forever
    (`prune --dry-run -v` reports nothing for a pile of registered, still-present trees). Reclaiming
    one is `remove`."""
    wt = rec.get("worktree") if isinstance(rec, dict) else None
    if not isinstance(wt, dict):
        return None
    path = wt.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    path = path.strip()
    runner = runner or Runner()
    host = os.path.abspath(host or wt.get("host") or REPO_ROOT)
    now = now or _utc_now()
    out = {"path": path, "removed": False, "leaked": False, "reason": "",
           "at": _stamp(now)}

    try:
        absent = not os.path.exists(path)
    except OSError:  # noqa: BLE001 — an unreadable path is not an answer; ask git
        absent = False
    if absent:
        _git(runner, host, "worktree", "prune")   # admin records only; see the docstring
        return {**out, "removed": True, "reason": "already-absent"}

    try:
        res = _git(runner, host, "worktree", "remove", path)
    except Exception as e:  # noqa: BLE001 — a broken seam costs the reclaim, never the push
        return {**out, "leaked": True, "reason": one_line(f"could not run git: {e}")}
    if getattr(res, "returncode", 1) == 0:
        return {**out, "removed": True, "reason": "removed"}
    # NO ESCALATION. There is deliberately no `--force` retry here, and adding one would delete the
    # work the refusal is protecting.
    return {**out, "leaked": True, "reason": _git_message(res)}


def sweep(state_dir: str, *, days: float = LEAK_RETENTION_DAYS, root: str | None = None,
          host: str | None = None, runner=None, now: datetime | None = None) -> dict:
    """The reclaim pass over `<repos>/seneschal-worktrees/`: retry the removals teardown never got to,
    then `git worktree prune` once at the end.

    **NOTHING CALLS THIS — `worktree_gc.py` DOES THE JOB INSTEAD.** Read that module before reviving
    this one: it is a strict superset that keeps this condition and adds the proofs
    `git worktree remove` does not perform, so wiring this in later would be the *weaker* sweeper
    silently replacing the stronger one. Two things this cannot do and that one can — reclaim an
    **empty husk** (this counts every record-less directory as an `orphan` and removes none, which is
    most of a real pile), and refuse a clean worktree holding **unpushed commits** (`remove` permits
    that).
    Kept, tested and unwired rather than deleted: the condition below is still the right one and is
    the part `worktree_gc` reuses.

    **Reclaim, don't guess.** A directory is only removed when its job record is **terminal AND
    already notified** — the identical condition `jobs.prune --days` uses, for the identical reason:
    the GC must never be what makes a job go silent — plus an age floor. Everything else is counted
    and returned rather than acted on:

      * `kept` — the job is still active, or too recent.
      * `orphans` — **no record at all.** Not removed, deliberately: with no record there is nothing
        that says the job is over, and "reclaim, don't guess" means the count is the answer here. A
        pile-up is then visible rather than merely present.

    Returns `{removed, leaked, kept, orphans, root}` — lists of job ids, so a caller can report a
    running count without re-deriving it."""
    runner = runner or Runner()
    host = os.path.abspath(host or REPO_ROOT)
    now = now or _utc_now()
    base = worktree_root(root)
    out = {"removed": [], "leaked": [], "kept": [], "orphans": [], "root": base}
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out          # no worktree root yet is the normal state, not an error

    try:
        import jobs  # deferred: `jobs` imports THIS module, so a top-level import would be circular
    except Exception:  # noqa: BLE001
        return out

    for name in names:
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        rec = jobs.load_job(state_dir, name)
        if rec is None:
            out["orphans"].append(name)
            continue
        if rec.get("status") not in jobs.TERMINAL or not rec.get("notified_at"):
            out["kept"].append(name)
            continue
        try:
            age_days = (now - jobs.parse_iso(rec["notified_at"])).total_seconds() / 86400.0
        except (ValueError, TypeError, KeyError, AttributeError):
            out["kept"].append(name)
            continue
        if age_days < days:
            out["kept"].append(name)
            continue
        result = teardown({"worktree": {"path": path, "host": host}}, runner=runner, host=host,
                          now=now)
        (out["removed"] if (result or {}).get("removed") else out["leaked"]).append(name)

    # LAST, and only here: `prune` tidies admin records for directories that are already gone — a
    # human deleting one by hand is the case it is for. It reclaims nothing on its own.
    _git(runner, host, "worktree", "prune")
    return out
