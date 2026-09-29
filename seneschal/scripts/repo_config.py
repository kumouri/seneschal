#!/usr/bin/env python3
"""repo_config.py — which repositories the PR guards watch, and what "the base branch" means there.

The guard hooks (`merge_guard.py`, `branch_delete_guard.py`, `branch_sweep.py`) and the PR
automation (`pr_sweep.py`, `pr_repair.py`, `develop_ci_status.py`, …) protect the OWNER's own
repositories. Nothing about those repositories — the account, the names, the branch model — is
code. It comes from two places, in this order:

1. **The config file** `../references/pr-guard.json` (gitignored, per-install owner config), else
   the shipped `../references/pr-guard.example.json`. Existence decides, not validity: an owner
   file that exists but will not parse reads as `{}` (every key falls back to git), never as the
   example — the same rule `telegram_topics.py` applies to its routing table. Keys:

   * ``watched_repos`` — ``["owner/name", …]``, the repositories the PR sweep asks about. Empty
     (the shipped default) means *this checkout's own `origin` repository*, and nothing else.
   * ``base_branch`` — the integration branch PRs target. ``null`` (the default) derives it.
   * ``protected_branches`` — extra branch names no guard ever deletes, on top of the fixed
     floor :data:`PROTECTED_FLOOR`.
   * ``deploy_on_merge`` — ``{"owner/name": "branch"}``: repositories where merging to that
     branch changes what the running assistant executes. Empty means *this checkout's own
     `origin` repository deploys on* :data:`DEFAULT_DEPLOY_BRANCH` — the branch the
     `seneschald-update` merge-detector pulls (`PATH_A_CUTOVER.md`).

2. **Git itself**, for anything the file leaves unset:

   * the repository is `git remote get-url origin`, parsed into ``owner/name`` (https, scp-style
     `git@host:owner/name.git` and `ssh://` forms);
   * the base branch is ``develop`` when `origin/develop` exists (Git Flow: develop integrates,
     main releases — seneschal's own model), else the branch `origin/HEAD` points at, else
     ``main``.

Every reader here **never raises**: a missing git, a checkout without an `origin`, an unreadable
file all degrade to the documented defaults, because the callers are PreToolUse hooks and a hook
that crashes is a hook that stops guarding. Stdlib only.

Usage (diagnostic):
  python repo_config.py            # the resolved config, JSON
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
REFERENCES_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "references"))
REFERENCES_FILE = "pr-guard.json"
REFERENCES_EXAMPLE_FILE = "pr-guard.example.json"

#: Branch names no guard ever deletes, whatever the config says. The config can ADD names; it
#: cannot remove these, because a typo in a JSON file must not be what un-protects `main`.
PROTECTED_FLOOR = frozenset({"develop", "master", "main", "trunk", "HEAD"})

#: The branch the resident daemon runs from and the merge-detector pulls (`PATH_A_CUTOVER.md`).
DEFAULT_DEPLOY_BRANCH = "main"

#: The integration branch of a Git Flow repository.
GIT_FLOW_BASE = "develop"

#: `https://github.com/owner/name(.git)`, `git@github.com:owner/name(.git)`, `ssh://git@host/owner/name`.
_REMOTE_RE = re.compile(
    r"^(?:[a-z+]+://(?:[^@/]+@)?[^/]+/|[^@\s]+@[^:\s]+:)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _git(args: list, cwd: str | None = None, runner=None) -> str:
    """stdout of `git <args>` stripped, or `""` on any failure. `runner(argv, cwd) -> (rc, out)`
    is injectable for tests."""
    argv = ["git", "-c", "core.fsmonitor=false", *args]
    try:
        if runner is not None:
            rc, out = runner(argv, cwd or REPO_ROOT)
        else:
            p = subprocess.run(argv, cwd=cwd or REPO_ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=15)
            rc, out = p.returncode, p.stdout
    except Exception:  # noqa: BLE001 — a hook's config read never raises
        return ""
    return (out or "").strip() if rc == 0 else ""


def parse_remote(url: str) -> str:
    """A git remote URL -> ``owner/name``; `""` when it is not one."""
    m = _REMOTE_RE.match(str(url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def load(references_dir: str = REFERENCES_DIR) -> dict:
    """The owner's `pr-guard.json`, else the shipped example; `{}` when neither parses."""
    path = os.path.join(references_dir, REFERENCES_FILE)
    if not os.path.exists(path):
        path = os.path.join(references_dir, REFERENCES_EXAMPLE_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def origin_repo(cwd: str | None = None, runner=None) -> str:
    """This checkout's `origin` as ``owner/name``; `""` when there is none."""
    return parse_remote(_git(["remote", "get-url", "origin"], cwd, runner))


def watched_repos(cfg: dict | None = None, cwd: str | None = None, runner=None) -> tuple:
    """The repositories the PR sweep asks about: the config's list, else this checkout's origin."""
    cfg = load() if cfg is None else cfg
    listed = cfg.get("watched_repos") or []
    repos = tuple(r.strip() for r in listed if isinstance(r, str) and _SLUG_RE.match(r.strip()))
    if repos:
        return repos
    own = origin_repo(cwd, runner)
    return (own,) if own else ()


def base_branch(cfg: dict | None = None, cwd: str | None = None, runner=None) -> str:
    """The integration branch: the config's `base_branch`, else `develop` when `origin/develop`
    exists, else the branch `origin/HEAD` names, else `main`."""
    cfg = load() if cfg is None else cfg
    named = cfg.get("base_branch")
    if isinstance(named, str) and named.strip():
        return named.strip()
    if _git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{GIT_FLOW_BASE}"], cwd, runner):
        return GIT_FLOW_BASE
    head = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd, runner)
    if head.startswith("origin/") and len(head) > len("origin/"):
        return head[len("origin/"):]
    return "main"


def protected_branches(cfg: dict | None = None) -> frozenset:
    """:data:`PROTECTED_FLOOR` plus the config's `protected_branches` (additions only)."""
    cfg = load() if cfg is None else cfg
    extra = cfg.get("protected_branches") or []
    return PROTECTED_FLOOR | {b.strip() for b in extra if isinstance(b, str) and b.strip()}


def deploy_on_merge(cfg: dict | None = None, cwd: str | None = None, runner=None) -> dict:
    """``{"owner/name": branch}`` — the repositories whose merges redeploy the running assistant.
    The config's map, else this checkout's origin on :data:`DEFAULT_DEPLOY_BRANCH`."""
    cfg = load() if cfg is None else cfg
    listed = cfg.get("deploy_on_merge") or {}
    if isinstance(listed, dict):
        out = {k.strip(): v.strip() for k, v in listed.items()
               if isinstance(k, str) and isinstance(v, str) and _SLUG_RE.match(k.strip()) and v.strip()}
        if out:
            return out
    own = origin_repo(cwd, runner)
    return {own: DEFAULT_DEPLOY_BRANCH} if own else {}


def resolved(cwd: str | None = None, runner=None) -> dict:
    cfg = load()
    return {"watched_repos": list(watched_repos(cfg, cwd, runner)),
            "base_branch": base_branch(cfg, cwd, runner),
            "protected_branches": sorted(protected_branches(cfg)),
            "deploy_on_merge": deploy_on_merge(cfg, cwd, runner)}


def main(argv=None) -> int:
    json.dump(resolved(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
