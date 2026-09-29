#!/usr/bin/env python3
"""**Look for pull requests a merge broke, and fix the ones it is safe to fix.** Stdlib + `gh` + `git
merge-file`. `../docs/concurrent-pr-collisions-spec.md` §5 (repair), §5A (the rebaser, `pr_rebase.py`)
and §5A.9 (the as-built choices) are the authority.

## Why it exists

When one PR merges, a sibling that edited the same file goes `CONFLICTING`, `pr_sweep` correctly
withholds its merge picker (it does not ask about a PR that cannot merge) — and without this module
nothing else happens: the owner has to notice. The repair after such a break is usually mechanical,
so something should be looking for PRs that go into conflict and be fixing the ones it safely can.

## One pass, riding the daemon's PR-watch task, after `pr_sweep` (ask, then retire, then repair — §5A.2)

`presence.py`'s PR-watch task calls :func:`sweep` right after `pr_sweep.sweep`. One `gh pr list` of
the target repository plus one read of the base branch's tip, then per open, non-draft PR based on
that branch:

- **`BEHIND` (and `MERGEABLE`)** → `pr_rebase.consider`: GitHub's own server-side rebase, under §5A.3's
  invariant (never move a head carrying a live approval or a pending picker). At most one per pass.
- **`CONFLICTING`** → confirmed by a FRESH `gh pr view` (the list's `mergeable` lags by minutes), then
  classified: which paths actually conflict, found without a checkout — the compare API names the
  paths changed on both sides since the merge base, and for each one the three versions are fetched
  and merged with `git merge-file` (which needs no repository). Then:
  - **every conflicting path is repair-eligible** — one of §5.2's two JSON ledgers
    (`seneschal/context-budget.json`, `seneschal/context-pointers.json`) or any Markdown file
    (`*.md`) → launch ONE `jobs.py start --worktree` repair job per `(PR, base sha)`,
    `--requested-by assistant --reason-class merge-repair`, no `--over-soft-cap` — a cap refusal
    records nothing and the next pass retries. Markdown is resolved by KEEPING BOTH SIDES
    (:func:`repair_brief`), and the job comments on the PR listing the files it resolved so the owner
    can review the diff;
  - **anything else** (code, config, a path it could not classify) → no job; ONE Telegram line to
    the pull-requests topic naming the PR, the files and why, once per head.

Every CONFLICTING and BEHIND detection is also appended to `state/pr-repair-log.jsonl` with §5A.7's
detection columns and `rebaser_live: true`, so a later measurement of how often the allow-list is
sufficient can keep the populations with and without a live rebaser separable.

## Which repository, and which base — configuration, resolved at call time

The pass repairs ONE repository: **this checkout's own `origin`** (`repo_config.origin_repo()`), because
a repair job's worktree is cut from this repository's `origin/<base>` (`job_worktree.py`), so a PR
anywhere else cannot be repaired by it; the rebaser is scoped the same way rather than widening
`pr_sweep`'s watched set by a side door (§5A.8). The base branch is `repo_config.base_branch()`
(`../references/pr-guard.json`, else `develop` when `origin/develop` exists, else `origin/HEAD`'s
branch, else `main`). Both are read once per pass, never at import, so an owner who edits
`pr-guard.json` — or a test — is always honoured. The historical module names `REPO` and
`BASE_BRANCH` still resolve (lazily, via module `__getattr__`) to those two values for any caller
that reads them; :func:`sweep` takes `repo=` / `base_branch=` to override both. A checkout with no
`origin` makes the pass report an error and do nothing.

## Bounds

At most one rebase, one repair launch and two conflict classifications per pass; one repair job in
flight at a time; a repair job never relaunched for the same `(PR, base sha)`; at most
:data:`MAX_REPAIRS_PER_PR_PER_DAY` per PR per 24 h (a binding ceiling says so once); no launch and no
notice inside the quiet window (`merge_guard.in_quiet_hours`) — a job's completion push would buzz
the owner at 3 AM, and the spec's §5.4 invariant 3 says a repair defers there like everything else.
Rebases DO run overnight: they send nothing. `state/pr-auto-repair-off` (an empty file) turns the
whole pass off without a deploy.

## What it CANNOT do

It never merges a pull request, never writes or reads-to-decide an approval except through
`merge_guard.verify_approval` (in `pr_rebase.py`), and never touches a branch itself — the rebase is
GitHub's, the repair is a delegated job in its own worktree whose brief forbids force and merging.
A CONFLICTING PR is repaired even when an approval is pinned to it: §5A.3 clause 4 — an approval on a
conflicted head can never be spent, so moving it destroys nothing.

USAGE (diagnostics; the daemon is the production caller):
  python pr_repair.py --dry-run     # what a pass WOULD do; launches, rebases and sends nothing
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import job_pr_draft  # noqa: E402 — a PR whose branch is a running job's worktree gets drafted, not repaired
import merge_guard as mg  # noqa: E402 — DEFAULT_STATE_DIR, in_quiet_hours, verify_approval, repo_key
import pr_rebase  # noqa: E402 — phase 2R: the server-side rebase under §5A.3
import repo_config  # noqa: E402 — which repository, and which base branch: configuration


def target_repo() -> str:
    """The one repository this repairs: this checkout's `origin`, as ``owner/name`` (`""` if none)."""
    return repo_config.origin_repo()


def target_base() -> str:
    """The integration branch PRs target here (`repo_config.base_branch()`)."""
    return repo_config.base_branch()


def __getattr__(name):
    """`REPO` / `BASE_BRANCH` — the historical constant names, now resolved lazily from
    `repo_config` on every read (PEP 562). Kept so a caller or test reading `pr_repair.REPO` sees the
    same repository the pass acts on."""
    if name == "REPO":
        return target_repo()
    if name == "BASE_BRANCH":
        return target_base()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: §5.2 — machine-written additive ledgers whose union is well-defined without reading prose.
ALLOWLIST = ("seneschal/context-budget.json", "seneschal/context-pointers.json")
#: §5A.9 — Markdown is repair-eligible, superseding §5.2's "every `.md` is refused" for auto-repair:
#: a Markdown conflict is repaired by keeping both sides, never by choosing one. The owner still
#: approves every merge, and instruction files (`CLAUDE.md`, `AGENTS.md`, …) stay ask-high in
#: `merge_guard`, which this does not touch. Code and config conflicts still get the notice only.
MARKDOWN_SUFFIX = ".md"


def repair_eligible(path: str) -> bool:
    """A conflicting path the repair job may resolve: a §5.2 ledger, or any Markdown file."""
    return path in ALLOWLIST or path.lower().endswith(MARKDOWN_SUFFIX)


LOG_FILE = "pr-repair-log.jsonl"
LOG_SCHEMA = "seneschal.pr-repair/1"
OFF_SENTINEL = "pr-auto-repair-off"

LIST_FIELDS = ("number,headRefOid,headRefName,baseRefName,isDraft,mergeable,mergeStateStatus,"
               "statusCheckRollup,title,url,isCrossRepository")
PR_LIST_LIMIT = 50
MAX_REBASES_PER_PASS = 1
MAX_REPAIR_LAUNCHES_PER_PASS = 1
MAX_CLASSIFY_PER_PASS = 2
#: The spec names no number; this sits above the handful of repairs one PR can legitimately need on
#: a day when the base moves repeatedly.
MAX_REPAIRS_PER_PR_PER_DAY = 3
#: A shared-path set this large is classified as a superset rather than file by file.
MAX_PATHS_TO_MERGE = 20
#: The compare API lists at most 300 files; a list at that size may be truncated.
COMPARE_FILE_CAP = 300
JOB_TITLE_PREFIX = "pr-repair"
JOB_DEADLINE_SEC = 3600
GH_TIMEOUT_SEC = 60


class CannotTell(Exception):
    """An input needed to classify a conflict could not be read."""


def _run(argv: list) -> tuple:
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="surrogateescape", timeout=GH_TIMEOUT_SEC)
    return proc.returncode, proc.stdout, proc.stderr


def _stamp(now=None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _parse_stamp(value):
    return pr_rebase._parse_stamp(value)


# --------------------------------------------------------------------------- the ledger

def log_path(state_dir: str) -> str:
    return os.path.join(state_dir, LOG_FILE)


def read_log(state_dir: str) -> list:
    """Every row, oldest first. Never raises; a torn line is skipped. An unreadable log reads as empty,
    which at worst repeats one detection row or one notice — never a second job, because the job
    ledger's single-flight check (:func:`active_repair_jobs`) is the other half of that guard."""
    try:
        with open(log_path(state_dir), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except (OSError, UnicodeDecodeError):
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def append_log(state_dir: str, row: dict) -> None:
    """APPEND one row (`state/`'s house shape; `open(..., "a")` never truncates). Fail-open."""
    row = {"schema": LOG_SCHEMA, **row}
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(log_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError):
        pass


def _rows_for(log_rows, event, pr, **match):
    return [r for r in log_rows if r.get("event") == event and r.get("pr") == pr
            and all(r.get(k) == v for k, v in match.items())]


# --------------------------------------------------------------------------- GitHub reads

def list_open(repo: str, runner=None) -> tuple:
    argv = ["gh", "pr", "list", "--repo", repo, "--state", "open", "--json", LIST_FIELDS,
            "--limit", str(PR_LIST_LIMIT)]
    try:
        code, out, err = (runner or _run)(argv)
    except Exception as e:  # noqa: BLE001
        return None, f"gh pr list failed: {e!r}"
    if code != 0:
        return None, (err or out or "gh returned non-zero").strip()[:300]
    try:
        rows = json.loads(out)
    except ValueError as e:
        return None, f"unreadable gh output: {e}"
    if not isinstance(rows, list):
        return None, "gh pr list returned no list"
    return [r for r in rows if isinstance(r, dict)], ""


def base_tip(repo: str, branch: str | None = None, runner=None) -> tuple:
    """`(sha, error)` for `branch`'s tip on GitHub; `branch` defaults to :func:`target_base`."""
    branch = branch or target_base()
    argv = ["gh", "api", f"repos/{repo}/git/ref/heads/{branch}", "--jq", ".object.sha"]
    try:
        code, out, err = (runner or _run)(argv)
    except Exception as e:  # noqa: BLE001
        return None, f"reading {branch}'s tip failed: {e!r}"
    sha = (out or "").strip()
    if code != 0 or len(sha) != 40:
        return None, (err or out or f"could not read {branch}'s tip").strip()[:300]
    return sha, ""


def fresh_view(repo: str, pr: int, runner=None) -> dict | None:
    """A FRESH read of the fields a decision rests on — `gh pr list`'s `mergeable` lags by minutes."""
    argv = ["gh", "pr", "view", str(pr), "--repo", repo, "--json",
            "mergeable,mergeStateStatus,headRefOid,state"]
    try:
        code, out, _err = (runner or _run)(argv)
        data = json.loads(out) if code == 0 else None
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _compare(repo: str, a: str, b: str, runner) -> dict:
    code, out, err = runner(["gh", "api", f"repos/{repo}/compare/{a}...{b}"])
    if code != 0:
        raise CannotTell(f"compare {a[:12]}...{b[:12]}: {(err or out).strip()[:200]}")
    try:
        data = json.loads(out)
    except ValueError as e:
        raise CannotTell(f"compare output unreadable: {e}")
    if not isinstance(data, dict):
        raise CannotTell("compare output unreadable")
    return data


def _changed(files: list) -> dict:
    """`{path: status}` — a rename contributes both names, so a rename on one side intersects an edit
    of the old name on the other."""
    out = {}
    for f in files or []:
        if not isinstance(f, dict):
            continue
        name = f.get("filename")
        if isinstance(name, str):
            out[name] = f.get("status") or "modified"
        prev = f.get("previous_filename")
        if isinstance(prev, str):
            out[prev] = "renamed"
    return out


def _content(repo: str, path: str, sha: str, runner):
    """File text at `sha`, or `None` when the path does not exist there. Raises CannotTell otherwise."""
    code, out, err = runner(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                             f"repos/{repo}/contents/{quote(path)}?ref={sha}"])
    if code == 0:
        return out
    if "404" in (err or "") or "Not Found" in (err or ""):
        return None
    raise CannotTell(f"could not read {path}@{sha[:12]}: {(err or out).strip()[:200]}")


def merges_cleanly(ours: str, base: str, theirs: str, runner) -> bool:
    """`git merge-file` on three temp files — the same xdiff three-way merge git uses, with no
    repository. Exit 0 is clean, a positive count is conflicts, anything else raises CannotTell."""
    tmp = tempfile.mkdtemp(prefix="pr-repair-")
    try:
        paths = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = os.path.join(tmp, name)
            with open(p, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
                fh.write(text)
            paths.append(p)
        code, _out, err = runner(["git", "merge-file", "-p", "-q", *paths])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if code == 0:
        return True
    if code > 0:
        return False
    raise CannotTell(f"git merge-file failed: {(err or '').strip()[:200]}")


def conflict_paths(repo: str, base_sha: str, head_sha: str, runner=None, structural=None) -> list:
    """The paths that genuinely conflict when `base_sha` is merged into `head_sha`, sorted. Raises
    CannotTell when it cannot say. A path deleted or renamed on either side, or too many shared paths to
    merge one by one, counts as conflicting (a superset errs toward the notice, never toward a job).
    A deleted/renamed/missing path is also appended to `structural` when given: keeping both sides has
    no meaning for it, so even a Markdown one gets the notice rather than a repair job."""
    run = runner or _run
    pr_side = _compare(repo, base_sha, head_sha, run)
    merge_base = ((pr_side.get("merge_base_commit") or {}).get("sha"))
    if not isinstance(merge_base, str) or not merge_base:
        raise CannotTell("compare returned no merge base")
    base_side = _compare(repo, head_sha, base_sha, run)
    pr_files, base_files = pr_side.get("files") or [], base_side.get("files") or []
    if len(pr_files) >= COMPARE_FILE_CAP or len(base_files) >= COMPARE_FILE_CAP:
        raise CannotTell(f"a side changed {COMPARE_FILE_CAP}+ files; the compare list may be truncated")
    ours, theirs = _changed(pr_files), _changed(base_files)
    shared = sorted(set(ours) & set(theirs))
    if len(shared) > MAX_PATHS_TO_MERGE:
        return shared
    out = []
    for path in shared:
        if {ours[path], theirs[path]} & {"removed", "renamed"}:
            out.append(path)
            if structural is not None:
                structural.append(path)
            continue
        mine = _content(repo, path, head_sha, run)
        other = _content(repo, path, base_sha, run)
        orig = _content(repo, path, merge_base, run)
        if mine is None or other is None:
            out.append(path)
            if structural is not None:
                structural.append(path)
            continue
        if not merges_cleanly(mine, orig or "", other, run):
            out.append(path)
    return out


# --------------------------------------------------------------------------- jobs

def active_repair_jobs(state_dir: str) -> list:
    try:
        import jobs
        return [r for r in jobs.list_jobs(state_dir, active_only=True)
                if str(r.get("title") or "").startswith(JOB_TITLE_PREFIX)]
    except Exception:  # noqa: BLE001 — cannot tell ⇒ treat as busy (no launch), never as idle
        return [{"title": "(jobs ledger unreadable)"}]


def job_title(pr: int, base: str | None = None) -> str:
    return f"{JOB_TITLE_PREFIX} #{pr}: merge {base or target_base()}, resolve conflicts"


#: The Markdown rule, verbatim as decided for auto-repair (§5A.9). Tested.
MARKDOWN_RULE = ("keep both sides; for tables/lists keep every row in chronological order; never delete "
                 "either side's content; if both sides edited the SAME sentence differently, keep both "
                 "versions adjacent and flag it in the PR comment")


def repair_brief(repo: str, row: dict, base_sha: str, paths: list, base: str | None = None) -> str:
    """The delegated job's brief. `base` is the base branch name (default :func:`target_base`); it is
    always spelled `origin/<base>` in full, because a bare branch name is ambiguous in a checkout with
    more than one remote."""
    base = base or target_base()
    pr, head, branch = row["number"], row["headRefOid"], row["headRefName"]
    listed = ", ".join(paths)
    return f"""You are repairing pull request #{pr} in {repo} ("{row.get('title', '')}"), branch `{branch}`.
The assistant's resident PR watcher launched you (spec: seneschal/docs/concurrent-pr-collisions-spec.md §5, §5A.9).
It went CONFLICTING against {base} at {base_sha[:12]}. The conflict was classified in advance as
confined to: {listed} — repair-eligible paths (the two JSON ledgers of §5.2, and Markdown files per
§5A.9). You are in a private worktree; stay in it.

Do exactly this:
1. `git -c core.fsmonitor=false fetch origin`. `git checkout -B {branch} origin/{branch}`. If
   `git rev-parse HEAD` is not {head}, STOP: the branch moved since detection; change nothing.
2. `git merge --no-ff --no-commit origin/{base}` (spell origin/{base} in full; a bare `{base}` can be ambiguous).
3. `git status --porcelain` lists the conflicted paths (the unmerged ones). If ANY is neither
   seneschal/context-budget.json, seneschal/context-pointers.json nor a `*.md` file, or ANY conflict is not a
   both-modified (`UU`) or both-added (`AA`) content conflict — a delete, a rename, a mode change: `git merge
   --abort`, push nothing, and end with the line `REFUSED: <paths>`. Never resolve code or config.
4. seneschal/context-budget.json: for every artifact whose entry conflicts, keep BOTH sides' `raises[]` entries
   (union, dropping exact duplicates, ordered by `date`), re-chain them so each entry's `from` is the previous
   entry's `to`, and set `max_bytes` and the last entry's `to` to the artifact's MEASURED size in the merged
   working tree (LF-normalised) — never either side's number, never a sum. Name in the tail entry's `reason`
   which PR landed first and what this branch's own delta is. Then run
   `python seneschal/scripts/check_context_budget.py`; if it reports a broken chain or an over-budget artifact,
   abort as in step 3 with its report as the reason.
5. seneschal/context-pointers.json: union of both sides' `allow[]` by `pointer`, both sides' entries kept. The same
   `pointer` with a different `reason` on each side is a REFUSAL — abort as in step 3.
6. Every conflicted `*.md` file: resolve each conflict hunk by hand — {MARKDOWN_RULE}.
   Where a row or entry carries a date/time, "chronological" means by that stamp; where none does, {base}'s
   side first, then this branch's. Remove every conflict marker; change nothing outside the conflict hunks.
   Note each same-sentence case (file + the sentence) for step 10.
7. Commit with a Conventional Commit subject `chore(merge): merge {base} into {branch}` whose body lists every
   file resolved and how (re-chained / unioned / both sides kept).
8. Run `python seneschal/scripts/ci_local.py`. It must be green. If it is not, fix only what the merge itself broke
   in the files you resolved; anything else — push nothing and end with `REFUSED: ci_local red: <why>`.
9. `git push origin HEAD:{branch}` — a plain push. Never force, never `--force-with-lease`, never rewrite history.
   A rejected push means the branch moved: stop and report it.
10. Comment on the PR (`gh pr comment {pr} --repo {repo} --body-file <file>`, the body written to a file outside
   the worktree) so the owner can review the diff: the merge commit's sha, every file resolved and how, and — under
   a heading `Needs your eyes` — each same-sentence conflict where both versions were kept adjacent (write
   `none` if there were none).
11. Do NOT merge the pull request and do not approve anything; leave the merge to the owner. The resident watcher
   will send them a fresh picker once CI on the new head is green.
End with one line: `REPAIRED #{pr} -> <new head sha>` or `REFUSED: <reason>`."""


def launch_repair(state_dir: str, row: dict, base_sha: str, paths: list, *, claude_bin: str,
                  runner=None, repo: str | None = None, base: str | None = None) -> dict:
    """Start the repair job through `jobs.py`'s own CLI, so its caps and preflight apply verbatim.
    Returns `{"ok", "capped", "job_id", "error"}`."""
    repo = repo or target_repo()
    base = base or target_base()
    goal = (f"PR #{row['number']} mergeable again against {base}, CI green, "
            f"merge left to the owner")
    argv = [sys.executable, os.path.join(SCRIPT_DIR, "jobs.py"), "--state-dir", state_dir, "start",
            "--title", job_title(row["number"], base), "--worktree", "--requested-by", "assistant",
            "--reason-class", "merge-repair", "--goal", goal, "--agent",
            "--deadline-sec", str(JOB_DEADLINE_SEC), "--",
            claude_bin, "-p", repair_brief(repo, row, base_sha, paths, base),
            "--permission-mode", "bypassPermissions"]
    try:
        code, out, err = (runner or _run)(argv)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "capped": False, "job_id": None, "error": f"jobs.py failed: {e!r}"}
    if code == 2 and "refusing to start" in (err or ""):
        return {"ok": False, "capped": True, "job_id": None, "error": (err or "").strip()[:300]}
    if code != 0:
        return {"ok": False, "capped": False, "job_id": None,
                "error": (err or out or "jobs.py returned non-zero").strip()[:300]}
    try:
        job_id = json.loads(out).get("id")
    except (ValueError, AttributeError):
        job_id = None
    return {"ok": True, "capped": False, "job_id": job_id, "error": ""}


def refusal_text(repo: str, row: dict, paths: list, why: str, base: str | None = None) -> str:
    shown = ", ".join(paths[:8]) + (f" (+{len(paths) - 8} more)" if len(paths) > 8 else "")
    base = base or row.get("baseRefName") or "its base"
    lines = [f"\U0001f9e9 {repo} #{row['number']} conflicts with {base} and I didn't auto-fix it."]
    if shown:
        lines.append(f"Conflicting: {shown}")
    lines.append(f"Why not: {why}")
    url = row.get("url")
    if isinstance(url, str) and url.strip():
        lines.append(url.strip())
    return "\n".join(lines)


def _send(text: str, state_dir: str) -> dict:
    try:
        import pr_red_notify
        return pr_red_notify.send(text, state_dir=state_dir) or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}


# --------------------------------------------------------------------------- the pass

def _store_loader(state_dir):
    def load():
        import telegram_ask as ta
        return ta.load_store(ta.store_path(state_dir))
    return load


def _approval_live(state_dir, pr, head, verifier, repo):
    try:
        return (verifier or mg.verify_approval)(state_dir, pr, head, repo) == ""
    except Exception:  # noqa: BLE001
        return None


def _detect_once(state_dir, log_rows, row, base_sha, cls, extra, verifier, now, dry_run, repo):
    pr, head = row["number"], row["headRefOid"]
    if _rows_for(log_rows, "detected", pr, head_sha=head, base_sha=base_sha) or dry_run:
        return
    rec = {"event": "detected", "repo": repo, "pr": pr, "head_sha": head, "base_sha": base_sha,
           "class": cls, "mergeable": row.get("mergeable"),
           "merge_state": row.get("mergeStateStatus"),
           "approval_live_at_detection": _approval_live(state_dir, pr, head, verifier, repo),
           "rebaser_live": True, "at": _stamp(now), **extra}
    append_log(state_dir, rec)
    log_rows.append(rec)


def sweep(state_dir: str = mg.DEFAULT_STATE_DIR, *, claude_bin: str = "claude", now=None,
          runner=None, classifier=None, launcher=None, sender=None, verifier=None,
          store_loader=None, dry_run: bool = False, repo: str | None = None,
          base_branch: str | None = None) -> dict:
    """One pass. Returns a report and NEVER raises — it runs inside the daemon's PR-watch task.

    Seams (tests): `runner` (every subprocess), `classifier(base_sha, head_sha) -> [paths]`,
    `launcher(row, base_sha, paths) -> dict`, `sender(text) -> {"ok"}`, `verifier` (verify_approval),
    `store_loader() -> question store`. `repo` / `base_branch` override the configured target
    (:func:`target_repo` / :func:`target_base`), resolved once per pass."""
    report = {"disabled": False, "rebased": [], "waiting": [], "launched": [], "noticed": [],
              "deferred": [], "errors": [], "looked_at": 0, "drafted": []}
    try:
        return _sweep(report, state_dir, claude_bin=claude_bin, now=now, runner=runner,
                      classifier=classifier, launcher=launcher, sender=sender, verifier=verifier,
                      store_loader=store_loader, dry_run=dry_run, repo=repo,
                      base_branch=base_branch)
    except Exception as e:  # noqa: BLE001 — fail-open: the daemon's task must not go down
        report["errors"].append(f"pr-repair pass failed: {e!r}")
        return report


def _sweep(report, state_dir, *, claude_bin, now, runner, classifier, launcher, sender, verifier,
           store_loader, dry_run, repo, base_branch):
    if os.path.exists(os.path.join(state_dir, OFF_SENTINEL)):
        report["disabled"] = True
        return report
    repo = repo or target_repo()
    if not repo:
        report["errors"].append("no target repository: this checkout has no `origin` remote "
                                "(repo_config.origin_repo)")
        return report
    base = base_branch or target_base()
    run = runner or _run
    now = now or datetime.now(timezone.utc)
    send = sender or (lambda text: _send(text, state_dir))

    def default_classify(base_sha, head):
        structural = []
        return conflict_paths(repo, base_sha, head, runner=run, structural=structural), structural

    classify = classifier or default_classify
    launch = launcher or (lambda row, base_sha, paths: launch_repair(
        state_dir, row, base_sha, paths, claude_bin=claude_bin, runner=run, repo=repo, base=base))
    load_store = store_loader or _store_loader(state_dir)

    rows, err = list_open(repo, runner=run)
    if rows is None:
        report["errors"].append(err)
        return report
    base_sha, err = base_tip(repo, base, runner=run)
    if base_sha is None:
        report["errors"].append(err)
        return report
    quiet = mg.in_quiet_hours(now)
    log_rows = read_log(state_dir)
    rows = sorted((r for r in rows if isinstance(r.get("number"), int)
                   and not isinstance(r.get("number"), bool)
                   and isinstance(r.get("headRefOid"), str) and r.get("headRefOid")
                   and r.get("baseRefName") == base and not r.get("isDraft")
                   and not r.get("isCrossRepository")), key=lambda r: r["number"])
    report["looked_at"] = len(rows)
    rebases = launches = classified = 0

    # **In-flight detection, ahead of everything else.** A PR whose branch is checked out in a
    # RUNNING job's worktree is not a repair or rebase candidate — the job may still push more
    # commits, and treating a PR that is still being written as CONFLICTING launches a repair of a
    # moving target (the spec's §5B). Computed once per pass; `rows` already filtered out drafts
    # above, so every row reaching this loop is, by definition, not yet a draft — the first time one
    # is seen in-flight it gets converted here.
    inflight = job_pr_draft.list_running_worktree_branches(state_dir, runner=run)

    for row in rows:
        pr, head = row["number"], row["headRefOid"]

        job_id = inflight.get(row.get("headRefName")) if row.get("headRefName") else None
        if job_id:
            branch = row.get("headRefName")
            if job_pr_draft.draft_and_record(state_dir, repo=repo, pr=pr, branch=branch,
                                             job_id=job_id, runner=run, now=now):
                rec = {"event": "drafted_inflight", "repo": repo, "pr": pr, "head_sha": head,
                       "job_id": job_id, "branch": branch, "at": _stamp(now)}
                append_log(state_dir, rec)
                log_rows.append(rec)
                report["drafted"].append(f"#{pr} (job {job_id})")
            else:
                report["deferred"].append(f"#{pr} (in-flight; draft attempt failed, retrying next pass)")
            continue

        mergeable, state = row.get("mergeable"), row.get("mergeStateStatus")

        if mergeable == "MERGEABLE" and state == "BEHIND":
            _detect_once(state_dir, log_rows, row, base_sha, "behind",
                         {"rebase_would_suffice": True}, verifier, now, dry_run, repo)
            if rebases >= MAX_REBASES_PER_PASS:
                report["deferred"].append(f"#{pr} (rebase cap)")
                continue
            out = pr_rebase.consider(
                row, repo=repo, base_sha=base_sha, state_dir=state_dir, log_rows=log_rows,
                store_loader=load_store, verifier=verifier, runner=run, now=now, dry_run=dry_run,
                sender=send, record=lambda rec: (append_log(state_dir, rec), log_rows.append(rec)),
                inflight=inflight)
            act = out["action"]
            if act in ("rebased", "refused", "would-rebase", "ceiling"):
                rebases += 1
                report["rebased"].append(f"#{pr} {act}: {out['reason']}")
            elif act in ("wait", "cannot-tell"):
                report["waiting"].append(f"#{pr}: {out['reason']}")
            elif act == "error":
                report["errors"].append(f"#{pr} rebase: {out['reason']}")
            continue

        if mergeable != "CONFLICTING":
            continue  # UNKNOWN and anything unrecognised: ask again next pass (§5.1)

        # ---- CONFLICTING: the repair path
        if _rows_for(log_rows, "repair_launched", pr, base_sha=base_sha):
            continue  # one job per (PR, base sha), whatever it did
        if _rows_for(log_rows, "repair_refused", pr, head_sha=head):
            continue  # the owner has been told about this head once
        if quiet:
            report["deferred"].append(f"#{pr} (quiet hours)")
            continue
        if classified >= MAX_CLASSIFY_PER_PASS:
            report["deferred"].append(f"#{pr} (classify cap)")
            continue
        fresh = fresh_view(repo, pr, runner=run)
        if not fresh or fresh.get("headRefOid") != head or fresh.get("mergeable") != "CONFLICTING":
            continue  # stale list, a moved head, or not confirmed: next pass decides
        classified += 1
        structural = []
        try:
            result = classify(base_sha, head)
            if isinstance(result, tuple):
                result, structural = result
            paths = sorted(result)
            why_not = ""
        except CannotTell as e:
            paths, why_not = [], f"I couldn't work out which files conflict ({e})"
        if not paths and not why_not:
            continue  # GitHub says CONFLICTING, a three-way merge says clean: stale; re-read next pass
        outside = [p for p in paths if not repair_eligible(p)]
        if structural and not why_not:
            why_not = ("a delete or rename is never auto-repaired (there are no two sides to keep); "
                       + ", ".join(sorted(structural)[:4]))
        if outside and not why_not:
            why_not = ("only Markdown and the two JSON ledgers (seneschal/context-budget.json, "
                       "seneschal/context-pointers.json) are on the auto-repair allow-list (§5A.9); "
                       + ", ".join(outside[:4]) + (" is" if len(outside) == 1 else " are") + " not")
        _detect_once(state_dir, log_rows, row, base_sha, "conflicting",
                     {"conflict_paths": paths, "allowlist_sufficient": not why_not,
                      "rebase_would_suffice": False}, verifier, now, dry_run, repo)

        if why_not:
            if dry_run:
                report["noticed"].append(f"#{pr} (would notify): {why_not}")
                continue
            res = send(refusal_text(repo, row, paths, why_not, base)) or {}
            if res.get("ok"):
                rec = {"event": "repair_refused", "repo": repo, "pr": pr, "head_sha": head,
                       "base_sha": base_sha, "paths": paths, "reason": why_not, "at": _stamp(now)}
                append_log(state_dir, rec)
                log_rows.append(rec)
                report["noticed"].append(f"#{pr}: {why_not}")
            else:
                report["errors"].append(f"#{pr} notice not sent: {res.get('error')}")
            continue

        # allow-listed: one job, single-flight, capped
        if launches >= MAX_REPAIR_LAUNCHES_PER_PASS:
            report["deferred"].append(f"#{pr} (launch cap)")
            continue
        if active_repair_jobs(state_dir):
            report["deferred"].append(f"#{pr} (a repair job is already running)")
            continue
        cutoff = now - timedelta(hours=24)
        recent = [r for r in _rows_for(log_rows, "repair_launched", pr)
                  if (_parse_stamp(r.get("at")) or cutoff) > cutoff]
        if len(recent) >= MAX_REPAIRS_PER_PR_PER_DAY:
            if not any((_parse_stamp(r.get("at")) or cutoff) > cutoff
                       for r in _rows_for(log_rows, "repair_ceiling", pr)) and not dry_run:
                res = send(f"⏸️ {repo} #{pr}: I've launched {len(recent)} conflict repairs "
                           f"for it in 24 h and it keeps conflicting, so I've stopped for now.") or {}
                if res.get("ok"):
                    rec = {"event": "repair_ceiling", "repo": repo, "pr": pr, "at": _stamp(now)}
                    append_log(state_dir, rec)
                    log_rows.append(rec)
            report["deferred"].append(f"#{pr} (24 h repair ceiling)")
            continue
        if dry_run:
            report["launched"].append(f"#{pr} (would launch): {', '.join(paths)}")
            launches += 1
            continue
        res = launch(row, base_sha, paths) or {}
        if res.get("ok"):
            launches += 1
            rec = {"event": "repair_launched", "repo": repo, "pr": pr, "head_sha": head,
                   "base_sha": base_sha, "paths": paths, "job_id": res.get("job_id"),
                   "at": _stamp(now)}
            append_log(state_dir, rec)
            log_rows.append(rec)
            report["launched"].append(f"#{pr} job {res.get('job_id')}")
        elif res.get("capped"):
            report["deferred"].append(f"#{pr} (jobs cap; retry next pass)")
        else:
            report["errors"].append(f"#{pr} launch: {res.get('error')}")
    return report


def summary_line(report: dict) -> str:
    parts = []
    for key, label in (("drafted", "drafted"), ("rebased", "rebase"), ("launched", "repair"),
                       ("noticed", "told"), ("waiting", "waiting"), ("deferred", "held")):
        if report.get(key):
            parts.append(f"{label}: " + "; ".join(report[key]))
    return "pr-repair — " + (" | ".join(parts) if parts else f"looked at {report.get('looked_at', 0)}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="One PR auto-repair pass (rebase BEHIND, repair CONFLICTING)")
    p.add_argument("--state-dir", default=mg.DEFAULT_STATE_DIR)
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--repo", default=None,
                   help="owner/name; default: this checkout's origin (repo_config.origin_repo())")
    p.add_argument("--base-branch", default=None,
                   help="default: the configured base branch (repo_config.base_branch())")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would happen; rebase, launch and send nothing")
    args = p.parse_args(argv)
    report = sweep(args.state_dir, claude_bin=args.claude_bin, dry_run=args.dry_run,
                   repo=args.repo, base_branch=args.base_branch)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not report.get("errors") else 1


if __name__ == "__main__":
    sys.exit(main())
