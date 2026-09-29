# Deleting a branch closes the pull requests stacked on it

**Status:** `BUILT` — the hook, the sweep and their tests ship; installing the hook is a host-side
edit to the owner's `~/.claude/settings.json`, deferred to the owner by design (no PR can write it).

**Scope:** one mechanical failure with a one-call predicate, and the guard that enforces it. It is
**not** a document about merge hygiene, branch naming, or when to stack pull requests.

**Vocabulary note, for the grep that brings someone here** — specs tend to be filed under their
author's words and found by their reader's. So: this document is about **`--delete-branch`**,
**`-d`**, a **PR that closed itself**, a **stacked PR**, a **dependent PR**, **retargeting**,
**base branch deleted**, **my PR disappeared**, **GitHub closed my pull request**,
**`git push origin --delete`**, and **branch cleanup**.

---

## 1. The failure

`gh pr merge <N> --delete-branch` merges and then deletes the head branch. If another **open** pull
request has that branch as its `base`, GitHub **closes** the dependent rather than retargeting it.
The dependent's diff is not lost — the branch is still there, and the PR can be reopened — but
recovery is three manual steps that only happen if somebody is watching:

1. recreate the deleted ref,
2. reopen the closed pull request,
3. retarget it by hand.

The dependent's timeline records a `BaseRefDeletedEvent` and a close in the same second as the head
ref's deletion. Every occurrence is recovered by the same three steps, which is what makes this a
class rather than an accident, and the justification for spending code on it.

## 2. Counting past occurrences — the census, not the recollection

**This does not show up in PR history as a failure.** A dependent that was closed, reopened and
merged reads afterwards as an ordinary merged PR; the only durable trace is a `BaseRefDeletedEvent`
in the GraphQL timeline. So a count has to be recovered rather than remembered:

```bash
gh api graphql -f query='
  query { repository(owner:"example", name:"repo") {
    pullRequests(first:50, orderBy:{field:CREATED_AT, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title state baseRefName headRefName
        timelineItems(first:60, itemTypes:[REOPENED_EVENT, HEAD_REF_RESTORED_EVENT,
                      HEAD_REF_DELETED_EVENT, BASE_REF_CHANGED_EVENT, BASE_REF_DELETED_EVENT,
                      CLOSED_EVENT, MERGED_EVENT]) {
          nodes { __typename
            ... on BaseRefDeletedEvent { createdAt actor { login } }
            ... on BaseRefChangedEvent { createdAt previousRefName currentRefName }
            ... on ReopenedEvent { createdAt }
            ... on HeadRefRestoredEvent { createdAt }
            ... on HeadRefDeletedEvent { createdAt headRefName } } } } } } }'
```

Page through with `after: <endCursor>`. A PR that was **reopened after a `BaseRefDeletedEvent`** is
this failure; a close/reopen with no base-ref deletion nearby is a deliberate act and a different
thing. Also check the repository setting `delete_branch_on_merge`: when it is `false`, every
occurrence was an explicit `--delete-branch` (or a manual ref deletion), not the setting.

## 3. Why a written rule was not offered

A rule like "check nothing is stacked before deleting a branch" has to be remembered at exactly the
moment the author is thinking about something else — the merge. Written countermeasures of that kind
recur; the mechanical guards in this tree (`seneschal/scripts/bash_path_guard.py`,
`seneschal/scripts/merge_guard.py`) are the ones that hold.

**This document deliberately adds no rule**, and nothing in it belongs in `CLAUDE.md` as an
instruction: the countermeasure is `seneschal/scripts/branch_delete_guard.py`, which refuses.

## 4. The predicate, which is the whole fix

```bash
gh pr list --state open --base <branch> --json number   # must be empty
```

One indexed call — the same one a person would run by hand, the same one the guard makes, and the
same one the sweep makes.

**§4.1 — the scheduling is not the fix.** The obvious remedy is to stop passing `--delete-branch` and
run a periodic sweep of merged branches instead. The first half is right. The second half, *without
this predicate*, is **the same bug automated**: an unattended pass deletes a base branch, closes the
dependent, and the quick recovery a person at a terminal would make does not happen because nobody is
looking. **The predicate is the fix.** So `branch_sweep.py` imports
`branch_delete_guard.open_prs_based_on` rather than owning a copy, and a test asserts the sweep
defines no base-branch query of its own.

**§4.2 — the backlog is not a handful of branches.** A repository that has been merging without
`--delete-branch` for a while typically carries hundreds of stale heads: nearly every head branch of
a merged pull request is still on the remote, and almost none has anything stacked on it. A sweep
with no bounds would remove all of them on its first run. That is why `branch_sweep.py` is dry-run by
default and capped per run — §7.

## 5. UNDECIDED: whether the repository setting behaves differently

**The question.** Does GitHub's repository-level *"Automatically delete head branches"* retarget
dependents where `--delete-branch` does not?

**What the documentation says.** `github/docs` carries exactly one sentence on the subject —
[`retargeted-on-branch-deletion.md`](https://github.com/github/docs/blob/main/data/reusables/pull_requests/retargeted-on-branch-deletion.md),
reused on two pages:

> *"If you delete a head branch after its pull request has been merged, GitHub checks for any open
> pull requests in the same repository that specify the deleted branch as their base branch. GitHub
> automatically updates any such pull requests, changing their base branch to the merged pull
> request's base branch."*

**Why that does not answer it.** The sentence is **unconditional**. It draws no distinction between
the repository setting, the web UI's *Delete branch* button, `gh pr merge --delete-branch`, and a
raw `DELETE /repos/{o}/{r}/git/refs/heads/{b}` — and it describes behaviour that **does not occur**
on the `--delete-branch` path. So the documentation is either incomplete or wrong about at least one
path, and it cannot be used to establish that a *different* path behaves better. Neither page on the
deletion side (*"Deleting and restoring branches in a pull request"*, *"Managing the automatic
deletion of branches"*) mentions base-branch dependents at all.

**Therefore: undecided, and deliberately not guessed.** There is a plausible mechanism — that
GitHub's own post-merge deletion flow runs the retarget while a plain ref DELETE does not — and it is
**recorded here as a hypothesis with no evidence behind it, not as a finding.** Establishing it would
mean deliberately deleting a branch under an open stacked PR to watch what happens, which is the act
this whole document exists to prevent.

**What follows from leaving it undecided:**

* Keep `delete_branch_on_merge` **`false`**. Not because it is known to be worse — because it is
  unknown, and the guard cannot see a server-side deletion at all (§6.2). Turning it on would move
  the act out of the guard's view and rest on the documentation sentence above.
* If it is ever turned on, that is the owner's decision with this section in front of them, and the
  guard does not change: it never had an opinion about the setting.

## 6. What was built

| Artifact | What it is |
|---|---|
| `seneschal/scripts/branch_delete_guard.py` | The `PreToolUse` hook. Refuses `gh pr merge --delete-branch`/`-d`, `gh api … -X DELETE …/git/refs/heads/<b>`, `git push <remote> --delete <b>` and `git push <remote> :<b>` when the predicate is non-empty, and **names** the pull requests that would have closed. Refuses a protected branch outright. |
| `seneschal/scripts/branch_sweep.py` | The sweep. Merged-PR head branches with no open dependent, dry-run by default, capped, predicate re-read per branch immediately before each deletion; never a protected branch or the base branch. |
| `seneschal/scripts/test_branch_delete_guard.py` | Replays the failure both ways (merge with `--delete-branch`, bare ref deletion), plus the control (a merged branch with no dependents is **allowed**). |
| `seneschal/scripts/test_branch_sweep.py` | Asserts non-deletions, including a PR opened *between* the candidate list and the deletion. |
| `seneschal/scripts/BRANCH_DELETE_GUARD_SETUP.md` | The host-side install (manual, or `settings_merge.py --guard branch-delete`). **Until the owner installs it, the hook is inert** — `~/.claude/settings.json` is not in version control and no PR can write it. |

`merge_guard.command_segments(command, heads)` is the shared lexer: the guard walks `git` segments
with **the same** command-position detection, `.exe` stripping, redirection handling and `bash -c` /
`pwsh -Command` walk that `merge_guard` uses for `gh`. `merge_guard.gh_segments` is that function at
its default, so sharing it cannot widen what `merge_guard` detects; a test pins that equivalence.

**Protected branches and the base branch are configuration** (`seneschal/scripts/repo_config.py`):
the fixed floor `develop`, `master`, `main`, `trunk`, `HEAD`, plus any `protected_branches` the owner
adds in `seneschal/references/pr-guard.json` (additions only — a typo in a JSON file must not be what
un-protects `main`). The sweep additionally never deletes `repo_config.base_branch()`.

### 6.1 Three outcomes, not two

| Situation | Exit | Why |
|---|---|---|
| Not a branch deletion, or the predicate is empty | `0`, silent | Stage 1 is pure string work and fails **open** — it runs on every shell call on the machine. |
| A parse the guard cannot read; `gh` missing or unauthenticated; an unclassifiable failure | `2`, refuses | *"Cannot tell"* is never *"allow"*. This is `merge_guard.parse_gh_pr_merge`'s unrecognised-flag argument in a second place. |
| **GitHub itself unreachable** — DNS, refused, timeout, 5xx | `1`, **allows and says so** | An offline machine cannot answer, and walling off `git push` for the length of an outage buys nothing. Exit 1 is the hook contract's *non-blocking error*: stderr reaches the owner, the command proceeds. |
| Not a GitHub repository at all | `0`, silent | A correct negative, not an unknown: no GitHub repo, no GitHub pull requests, nothing that can close. |

**The third row is a deliberate asymmetry.** `merge_guard` fails closed on every stage-2 path because
a merge is rare and deferring one is free; this fires on ordinary `git push` in every session on the
box. **Exit 1 rather than 0 is the load-bearing part**: an outage that allowed *silently* would be
indistinguishable from a clean verdict, and the whole point of a guard is that its silence means
something.

### 6.2 What it does not stop

* **Anything not going through Bash or PowerShell** — the web UI, a browser, an MCP tool. This is the
  other half of §5: the repository setting acts server-side, where the hook cannot see it.
* **`git branch -d`/`-D`** — a local ref deletion cannot close a pull request.
* **A race.** The predicate is read once, before the command runs.
* **Forgery.** Same trust boundary as `merge_guard`: none. What it buys is that routing around it
  stops being a judgment call inside a normal loop.

## 7. The sweep's bounds, and why they are not knobs

1. **Dry-run is the default**; `--apply` is required. This does not contradict act-low — a deletion
   that passes the predicate is the assistant's to make — it means the acting invocation is one
   somebody typed. (`pr_sweep.py` can afford the opposite default because its act is *sending a
   question*.)
2. **`--limit` caps deletions per run** (default 25) and a capped run **names what it held over**.
   §4.2's backlog is why this is not theoretical.
3. **No default `--merged-within` window.** A window would make the backlog invisible rather than
   bounded, and how many old branches sit on the remote is a fact the owner should see once.
4. **Unknown is never delete.** A branch whose predicate could not be evaluated is skipped and
   reported. This is the one place the hook's fail-open-on-outage does *not* carry over: the hook
   allows an outage because a human is watching the command it allowed; the sweep runs alone.

## 8. Testing the guard: mutation checks need line-ending awareness

The guard's tests were mutation-checked — each mutation applied alone and reverted before the next —
and one early survivor (removing `strip_redirections` from the `git push` path, so
`git push origin --delete x 2>&1` read `2>&1` as a second branch name) became two tests rather than a
retired mutation.

**A mutation harness has to be line-ending-aware.** On a Windows checkout with `core.autocrlf=true`
the files are **CRLF on disk** while the object store holds LF. An anchor written with `\n` matches
**zero** times, the mutation is silently never applied, the suite stays green, and the result reads
exactly like a **surviving mutation** — the tool manufactures a false finding about the thing it is
checking. So translate every anchor into the file's own terminator **and assert the match count is
exactly 1**, aborting rather than reporting a survivor. Anything that mutation-checks a guard in this
repo needs both halves.

## 9. Open questions

1. **The repository setting** (§5) — undecided by design; the evidence that would settle it is the
   act the guard prevents.
2. **Scheduling the sweep.** It is a hand-run tool today. Making it a supervised daemon task is a
   small addition and the predicate does not change; it waits until a few `--apply` runs have been
   watched.
