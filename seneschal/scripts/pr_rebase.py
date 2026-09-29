#!/usr/bin/env python3
"""**Phase 2R — the auto-rebaser: bring a BEHIND pull request up to date server-side, and never move a
head the owner has already tapped.** Stdlib + the `gh` CLI. `../docs/concurrent-pr-collisions-spec.md`
§5A is the authority; this docstring carries only what a reader of the code needs.

## What it does — one server-side call, and only the cheap class

A pull request whose `mergeStateStatus` is `BEHIND` (with `mergeable: MERGEABLE`) cannot merge while
the base branch's protection requires up-to-date branches (`required_status_checks.strict`), and its
CI verdict is a statement about a base that no longer exists (the spec's class 1). The fix is GitHub's
own rebase, requested through GraphQL's `updatePullRequestBranch` with `updateMethod: REBASE` — the
operation `gh pr update-branch --rebase` performs — **plus `expectedHeadOid` pinned to the exact
commit this module's predicate was evaluated at**, so the move cannot land on a head nobody checked.
(The CLI form reads the head itself; pinning it here is a narrowing.)

**No local checkout, no worktree, no `git` at all.** The rebase and the branch update are GitHub's;
this module never holds the branch, so it cannot rewrite one by any other route.

## THE INVARIANT (§5A.3) — a moved head kills a live approval

An approval is single-use and pinned to one exact commit, so a rebaser on a three-minute cadence
could destroy the owner's taps faster than they make them. **It does not move `(repo, pr)`'s head `H`
unless every clause holds, and cannot-tell means don't:**

1. `mergeable` is exactly `MERGEABLE` and `mergeStateStatus` exactly `BEHIND`. `UNKNOWN` and every
   unrecognised value do nothing. (`CONFLICTING` never reaches this module: a server-side rebase has no
   conflict resolution, so a conflicted PR is `pr_repair.py`'s.)
2. `merge_guard.verify_approval(state_dir, pr, H, repo)` **REFUSES** — the same function the merge
   door calls, never a second reading of the record. `""` (a live approval) means WAIT.
3. No merge picker for `(repo, pr)` pinned at `H` is pending and unanswered
   (`picker_retire.pending_pr_pickers`). A pending picker whose head cannot be read counts as pinned.
4. At most one attempt per `(repo, pr, base_sha)` (§5A.6 bound 1) — idempotent against a stationary
   base branch, and it cannot trigger on its own output.
5. The head's CI is terminal (`watch_pr.classify`: done and not empty). A pending rollup means
   somebody just pushed, or the last rebase is still being judged; waiting a pass costs nothing and
   never races a job still pushing to its own branch.
6. Every input was readable. An unreadable approval store, question store or raising verifier is
   *could not tell*, reported as such, and nothing moves.

The corner clause 2 leaves open is the spec's, unchanged: a BEHIND PR with a live approval waits until
the approval is spent or expires (`merge_guard.APPROVAL_TTL_HOURS`). **Extending §5A.3 clause 4's
exemption to BEHIND** — its approval is also unspendable while strict checks are on, unless an admin
bypasses them — was considered and NOT done: it relaxes a rule about the owner's taps, which is the
owner's to relax.

## The race, and what gets said about it

After the call returns, the approval store is re-read at the OLD head. If an approval for `H` is now
live, a tap landed while the rebase was in flight and was destroyed by it — and that is one of the two
things this module ever sends to Telegram (§5A.5): a line naming the PR, the commit that was approved
and the new head. The other is the per-PR ceiling binding (§5A.6 bound 4). Every other outcome is a
report line in `presence.log` — an expected outcome gets no buzz.

## What it CANNOT do

It never merges a pull request, never mints, reuses, extends or re-dates an approval, and never
rewrites a branch by any route but GitHub's own rebase. A test asserts the file does not name the
approval writer, a force flag, or a merge subcommand.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import job_pr_draft  # noqa: E402 — inflight_job_for: a PR still being written by a running job
import merge_guard as mg  # noqa: E402 — verify_approval (the merge door's own predicate), repo_key
import picker_retire  # noqa: E402 — pending_pr_pickers: the ONE "is a picker still live" rule
import watch_pr  # noqa: E402 — classify: the ONE rollup -> verdict rule

#: §5A.6 bound 4 — a ceiling per `(repo, pr)` per rolling 24 h. The spec names no number and says 2a
#: should measure it; this build had to pick one. A busy day can legitimately need three rebases of
#: one PR as the base moves repeatedly, so the ceiling sits well above that.
MAX_REBASES_PER_PR_PER_DAY = 6

GH_TIMEOUT_SEC = 60

UPDATE_MUTATION = (
    "mutation($id:ID!,$oid:GitObjectID!){updatePullRequestBranch(input:{pullRequestId:$id,"
    "expectedHeadOid:$oid,updateMethod:REBASE}){pullRequest{headRefOid}}}"
)


def _run(argv: list) -> tuple:
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=GH_TIMEOUT_SEC)
    return proc.returncode, proc.stdout, proc.stderr


def _parse_stamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ci_terminal(row: dict) -> bool:
    """Has this head's CI reached a terminal verdict (green OR red)? Pending and empty are not."""
    nodes = row.get("statusCheckRollup")
    if not isinstance(nodes, list):
        return False
    verdict = watch_pr.classify(nodes)
    return bool(verdict["done"] and not verdict["empty"])


def picker_pinned(store: dict, repo: str, pr: int, head: str) -> bool:
    """Is a merge picker for `(repo, pr)` pending at `head`? A pending picker whose head cannot be read
    counts as pinned — cannot-tell means don't move."""
    key = mg.repo_key(repo)
    for p in picker_retire.pending_pr_pickers(store):
        if p["pr"] != pr or mg.repo_key(p["repo"]) != key:
            continue
        sha = p.get("head_sha")
        if not isinstance(sha, str) or not sha.strip() or sha.strip() == head:
            return True
    return False


def attempts(log_rows: list, repo: str, pr: int) -> list:
    key = mg.repo_key(repo)
    return [r for r in log_rows if r.get("event") == "rebase" and r.get("pr") == pr
            and mg.repo_key(r.get("repo") or "") == key]


def blocked_reason(row: dict, *, repo: str, base_sha: str, state_dir: str, log_rows: list,
                   store_loader, verifier=None, now=None, inflight: dict | None = None) -> tuple:
    """`(kind, reason)` — `kind` is `""` when the rebaser may move this head, else one of `skip`
    (nothing to do), `wait` (§5A.3 says a tap is at stake) or `cannot-tell` (an input was unreadable).
    Pure apart from the two reads it is handed; never raises.

    **In-flight, checked first** (`job_pr_draft.py`, the spec's §5B): `pr_repair.py` already drafts
    and skips a PR whose branch is a running job's worktree before it ever reaches this function, so
    `inflight` is normally never populated here — this is defense in depth for a direct caller (a
    test, or a future one) that hands `consider`/`blocked_reason` a row `pr_repair.py` has not already
    filtered."""
    pr, head = row.get("number"), row.get("headRefOid")
    if not isinstance(pr, int) or isinstance(pr, bool) or not isinstance(head, str) or not head:
        return "skip", "unreadable row"
    if inflight and job_pr_draft.inflight_job_for(row, inflight):
        return "skip", "PR's branch is checked out in a running job's worktree"
    if row.get("mergeable") != "MERGEABLE" or row.get("mergeStateStatus") != "BEHIND":
        return "skip", "not BEHIND-and-MERGEABLE"
    if any(r.get("base_sha") == base_sha for r in attempts(log_rows, repo, pr)):
        return "skip", f"already attempted against base {base_sha[:12]}"
    if not ci_terminal(row):
        return "skip", "CI on this head is not terminal yet"
    try:
        why_not = (verifier or mg.verify_approval)(state_dir, pr, head, repo)
    except Exception as e:  # noqa: BLE001 — a verifier that raises is cannot-tell, never a pass
        return "cannot-tell", f"verify_approval raised {e!r}"
    if not isinstance(why_not, str):
        return "cannot-tell", "verify_approval returned a non-string"
    if why_not == "":
        return "wait", "a live approval is pinned to this head (§5A.3 clause 2)"
    try:
        store = store_loader()
    except Exception as e:  # noqa: BLE001
        return "cannot-tell", f"question store unreadable: {e!r}"
    if not isinstance(store, dict):
        return "cannot-tell", "question store unreadable"
    if picker_pinned(store, repo, pr, head):
        return "wait", "a merge picker pinned to this head is still pending (§5A.3 clause 3)"
    return "", ""


def over_ceiling(log_rows: list, repo: str, pr: int, now=None,
                 cap: int = MAX_REBASES_PER_PR_PER_DAY) -> bool:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=24)
    recent = [r for r in attempts(log_rows, repo, pr)
              if (_parse_stamp(r.get("at")) or cutoff) > cutoff]
    return len(recent) >= cap


def fetch_node_id(repo: str, pr: int, runner=None) -> tuple:
    """`(node_id, head_sha)` from a FRESH read, or `(None, why)`."""
    argv = ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "id,headRefOid"]
    try:
        code, out, err = (runner or _run)(argv)
    except Exception as e:  # noqa: BLE001
        return None, f"gh pr view failed: {e!r}"
    if code != 0:
        return None, (err or out or "gh returned non-zero").strip()[:300]
    try:
        data = json.loads(out)
        return data["id"], data["headRefOid"]
    except (ValueError, KeyError, TypeError) as e:
        return None, f"unreadable gh pr view output: {e}"


def update_branch(repo: str, pr: int, head: str, runner=None) -> dict:
    """Ask GitHub to rebase `pr` onto its base, pinned to `head`. Returns
    `{"ok": bool, "definitive": bool, "new_head": str|None, "error": str}` — `definitive` is False for a
    transport failure (costs this pass, recorded nowhere) and True when GitHub itself answered."""
    run = runner or _run
    node_id, fresh = fetch_node_id(repo, pr, runner=run)
    if node_id is None:
        return {"ok": False, "definitive": False, "new_head": None, "error": fresh}
    if fresh != head:
        return {"ok": False, "definitive": False, "new_head": None,
                "error": f"head moved to {str(fresh)[:12]} since the list was read"}
    argv = ["gh", "api", "graphql", "-f", f"query={UPDATE_MUTATION}", "-f", f"id={node_id}",
            "-f", f"oid={head}"]
    try:
        code, out, err = run(argv)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "definitive": False, "new_head": None, "error": f"gh api failed: {e!r}"}
    try:
        data = json.loads(out) if out and out.strip() else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"ok": False, "definitive": False, "new_head": None,
                "error": (err or out or "no response").strip()[:300]}
    if data.get("errors") or code != 0:
        msgs = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e)
                         for e in (data.get("errors") or [])) or (err or "gh returned non-zero")
        return {"ok": False, "definitive": True, "new_head": None, "error": msgs.strip()[:300]}
    try:
        new_head = data["data"]["updatePullRequestBranch"]["pullRequest"]["headRefOid"]
    except (KeyError, TypeError):
        new_head = None
    return {"ok": True, "definitive": True, "new_head": new_head, "error": ""}


def tap_destroyed_text(repo: str, pr: int, old_head: str, new_head, base: str = "its base") -> str:
    return (f"⚠️ {repo} #{pr}: an approval you gave at {old_head[:12]} landed while I was "
            f"rebasing it onto {base}, so it no longer matches the head "
            f"({str(new_head or 'unknown')[:12]}). The merge picker will come again for the new head "
            f"once CI is green; that approval can't be used.")


def ceiling_text(repo: str, pr: int, cap: int = MAX_REBASES_PER_PR_PER_DAY,
                 base: str = "its base") -> str:
    return (f"⏸️ {repo} #{pr}: I've auto-rebased it onto {base} {cap} times in 24 h, so "
            f"I'm stopping for now rather than keep re-running its CI. It'll be picked up again once "
            f"the 24 h window clears.")


def consider(row: dict, *, repo: str, base_sha: str, state_dir: str, log_rows: list, store_loader,
             record, sender, verifier=None, runner=None, now=None, dry_run: bool = False,
             inflight: dict | None = None) -> dict:
    """Evaluate one row and, if every clause holds, rebase it. Returns an outcome dict
    `{"pr", "action", "reason", ...}` where `action` is one of `rebased`, `refused`, `wait`,
    `cannot-tell`, `skip`, `ceiling`, `error`, `would-rebase`. Never raises. `record(row_dict)` appends
    to the ledger; `sender(text) -> {"ok": bool}` is the Telegram door. The base branch named in the
    two messages is the row's own `baseRefName`."""
    pr, head = row.get("number"), row.get("headRefOid")
    base_name = row.get("baseRefName") or "its base"
    kind, reason = blocked_reason(row, repo=repo, base_sha=base_sha, state_dir=state_dir,
                                  log_rows=log_rows, store_loader=store_loader,
                                  verifier=verifier, now=now, inflight=inflight)
    if kind:
        return {"pr": pr, "action": kind, "reason": reason}
    stamp = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    if over_ceiling(log_rows, repo, pr, now=now):
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=24)
        told = any(r.get("event") == "rebase_ceiling" and r.get("pr") == pr
                   and mg.repo_key(r.get("repo") or "") == mg.repo_key(repo)
                   and (_parse_stamp(r.get("at")) or cutoff) > cutoff for r in log_rows)
        if not told and not dry_run:
            res = sender(ceiling_text(repo, pr, base=base_name)) or {}
            if res.get("ok"):
                record({"event": "rebase_ceiling", "repo": repo, "pr": pr, "at": stamp})
        return {"pr": pr, "action": "ceiling", "reason": "per-PR 24 h rebase ceiling reached"}
    if dry_run:
        return {"pr": pr, "action": "would-rebase", "reason": f"BEHIND base {base_sha[:12]}"}
    res = update_branch(repo, pr, head, runner=runner)
    if not res["definitive"]:
        return {"pr": pr, "action": "error", "reason": res["error"]}
    record({"event": "rebase", "repo": repo, "pr": pr, "head_sha": head, "base_sha": base_sha,
            "outcome": "rebased" if res["ok"] else "refused", "new_head": res["new_head"],
            "error": res["error"] or None, "at": stamp})
    if not res["ok"]:
        return {"pr": pr, "action": "refused", "reason": res["error"]}
    # §5A.3's race: re-read the approval at the OLD head. A live one now means a tap landed mid-flight.
    try:
        after = (verifier or mg.verify_approval)(state_dir, pr, head, repo)
    except Exception:  # noqa: BLE001 — cannot re-read: nothing to report, nothing to claim
        after = None
    if after == "":
        sent = sender(tap_destroyed_text(repo, pr, head, res["new_head"], base=base_name)) or {}
        record({"event": "tap_destroyed", "repo": repo, "pr": pr, "head_sha": head,
                "new_head": res["new_head"], "told": bool(sent.get("ok")), "at": stamp})
        return {"pr": pr, "action": "rebased", "reason": "a tap landed mid-rebase; told the owner",
                "new_head": res["new_head"], "tap_destroyed": True}
    return {"pr": pr, "action": "rebased", "reason": f"onto {base_sha[:12]}",
            "new_head": res["new_head"]}
