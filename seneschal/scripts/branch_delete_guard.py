#!/usr/bin/env python3
"""**A `PreToolUse` hook: refuse to delete a remote branch while an OPEN pull request has it as
`base`.** Standard library only, plus the `gh` CLI.

## The failure

Merging with `--delete-branch` deletes the head branch. If another open PR is *stacked* on that
branch — has it as its `base` — GitHub does not retarget the dependent. **It closes it**: the
dependent's timeline shows a `BaseRefDeleted` event and a close in the same second. Recovery is a
three-step hand job — recreate the deleted ref, reopen the pull request, retarget it — and it is
easy to miss entirely, because **a reopened PR does not show up in the repo's PR history as a
failure**. The way to count past occurrences is the `BaseRefDeletedEvent` in the GraphQL timeline;
`../docs/stacked-pr-branch-deletion-spec.md` has the query.

**GitHub's documentation says the opposite of what happens, and that is left undecided here rather
than explained.** The `github/docs` reusable `retargeted-on-branch-deletion` reads,
unconditionally: *"If you delete a head branch after its pull request has been merged, GitHub
checks for any open pull requests in the same repository that specify the deleted branch as their
base branch. GitHub automatically updates any such pull requests, changing their base branch to the
merged pull request's base branch."* The docs draw **no distinction** between the repository setting
*"Automatically delete head branches"* and a `--delete-branch` / API deletion, so they cannot be used
to establish that one retargets where the other closes. This guard therefore takes no position on
the setting; it is keyed on **the observed behaviour of the deletion path it can see**. See the spec.

## Why this is code, and why a rule was not offered instead

A written rule ("never pass `--delete-branch` when something is stacked") has to be remembered at
exactly the moment the author is thinking about something else — the merge. Written countermeasures
of that kind recur; the mechanical guards (`bash_path_guard.py`, `merge_guard.py`) are the ones that
hold. So this is a stop, not a sentence.

## Why this is NOT `merge_guard.py`

They judge different acts and would answer differently on the same command. `merge_guard` asks *"may
this pull request merge?"* — and an **approved** `gh pr merge 566 --delete-branch` is a *yes*, which
is exactly the command that closes the PR stacked on 566's head. Deleting the branch is a second act riding on the first,
with its own predicate and its own casualty. Two of the three shapes this file refuses
(`gh api -X DELETE`, `git push origin --delete`) are not merges at all and `merge_guard` never sees
them. What *is* shared is the lexer: :func:`merge_guard.command_segments`, imported rather than
copied, so command-position detection, `.exe` stripping, redirection handling and the `bash -c` /
`pwsh -Command` walk have one implementation and cannot drift apart.

## The predicate IS the fix, and the scheduling is not

The obvious alternative is to stop passing `--delete-branch` and run a periodic sweep instead.
**A sweep without this predicate automates the bug** — it deletes a base branch unattended, closing a
stacked PR with nobody watching, which is strictly worse than doing it by hand ninety seconds before
someone notices. So the predicate lives here, `branch_sweep.py` imports it, and there is no second
copy that could disagree:

    gh pr list --state open --base <branch> --json number   ->  must be empty

One call, cheap, and the same one a person would run by hand to check.

## Failure directions — THREE outcomes, not two, and the asymmetry is deliberate

`merge_guard` fails closed on every stage-2 path. This guard does not, because it fires on ordinary
`git push` in every session on the machine and a walled-off `git push` during an outage is a cost
`merge_guard` never pays. So:

1. **Stage 1, "is this even a branch deletion?"** — pure string work, no I/O. **Fails OPEN, silent**
   (:data:`EXIT_ALLOW`), for the same reason as `bash_path_guard`: it runs on `ls`, on `git status`,
   on everything, and a bug in it may not brick the machine.
2. **A parse this module cannot understand** — an unrecognised flag, a `deleteRef` GraphQL body, an
   unreadable remote, `gh` missing or unauthenticated. **BLOCKS** (:data:`EXIT_BLOCK`). *"Cannot
   tell"* is never *"allow"*, and the message says which check could not be satisfied. This is
   `merge_guard.parse_gh_pr_merge`'s unrecognised-flag argument in a second place: a guard that
   guesses at a flag reads the branch name off the wrong token and clears a deletion it never
   evaluated.
3. **GitHub itself unreachable** — DNS, refused connection, timeout, 5xx. **ALLOWS, LOUDLY**
   (:data:`EXIT_ADVISORY` — exit 1 is the hook contract's *non-blocking error*: stderr reaches
   the owner, the command proceeds). An offline machine cannot answer the question, and refusing every
   branch deletion for the length of an outage buys nothing — the PR that would be closed is on a
   server nobody can reach either. **The message names which of the two happened**, because
   "allowed because offline" and "allowed because safe" are the same silence otherwise.

There is a **fourth** outcome that is not a failure at all: *this is not a GitHub repository*
(`gh` reports no GitHub remote). No GitHub repo means no GitHub pull requests means nothing can be
closed, so that is a correct negative — :data:`EXIT_ALLOW`, silent. Treating it as an unknown would
make this hook refuse `git push origin --delete` in every unrelated repo on the box, which is how a
guard gets uninstalled.

## `EXIT 2 + STDERR`, never the JSON `permissionDecision`

Same mechanism and same reason as the other two hooks: the JSON decision rides on a well-formed
object reaching stdout, and the contract resolves a malformed one **by letting the action proceed**.
For a guard whose job is to refuse, a writer bug that silently disables it is the one unacceptable
failure. An exit code cannot be half-written.

## What it does not stop

* **Anything that does not go through Bash or PowerShell.** A deletion from the GitHub web UI, from
  a browser, or from an MCP tool is invisible here. The web UI is where the repository *setting*
  would act, which is the other half of why that setting is left undecided.
* **`git branch -d` / `-D`.** A local branch deletion cannot close a pull request, so refusing it
  would be a false positive with no failure behind it.
* **A race.** The predicate is read once, before the command runs. A PR opened against the branch in
  the intervening seconds is not seen. `branch_sweep.py` re-reads it per branch immediately before
  each deletion for the same reason, which is the most a check outside the server can do.
* **Forgery.** Same trust boundary as `merge_guard`: no boundary. What this buys is that routing
  around it stops being a judgment call inside a normal loop and becomes a deliberate act.

## Which branches are protected is configuration

:func:`protected_branches` is `repo_config.protected_branches()`: the fixed floor
(`repo_config.PROTECTED_FLOOR` — develop, master, main, trunk, HEAD) plus any names the owner adds in
`../references/pr-guard.json`. The config can ADD names, never remove the floor. The floor alone is
also exported as :data:`PROTECTED_BRANCHES`, a plain frozenset that needs no disk read, for callers
and tests that only want the part no config can change.

Install (the owner's to make — no PR can write `~/.claude/settings.json`):
`BRANCH_DELETE_GUARD_SETUP.md`, or `settings_merge.py --guard branch-delete`. **Until that host-side
edit exists this module is INERT.**

USAGE (diagnostics; the hook is the production caller and takes no argv):
  python branch_delete_guard.py check --branch docs/some-spec
  python branch_delete_guard.py check --branch develop --repo example/repo
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling imports work when imported, not only when run

# The LEXER only — command-position detection, `.exe` stripping, the nested-shell walk and
# `strip_redirections`. Deliberately not the classifier: what counts as a *merge* is that module's
# question and stays there. See the module docstring, "Why this is NOT merge_guard.py".
import merge_guard as mg  # noqa: E402
import repo_config  # noqa: E402 — the protected-branch set is owner config, not code

#: The tools whose commands are inspected. Both shells, like `merge_guard` and unlike
#: `bash_path_guard`: a branch deletion is a branch deletion in either, and there is no lexing
#: asymmetry here to make one of them a false positive by construction.
GUARDED_TOOLS = ("Bash", "PowerShell")

#: The hook contract's three exit codes, named because the whole design is which one is returned.
#: `0` — allow, silent. `2` — block, reason on stderr, fed back to the model. Anything else is a
#: *non-blocking error*: stderr is shown to the owner and the command proceeds, which is exactly the
#: shape the "GitHub unreachable" outcome needs and the reason it is `1` rather than a silent `0`.
EXIT_ALLOW = 0
EXIT_ADVISORY = 1
EXIT_BLOCK = 2

#: `gh pr list` is one indexed query. Shorter than `merge_guard`'s 60 s because this can fire on an
#: ordinary `git push` and a hung call must give up well inside the hook's own timeout, where it
#: becomes an advisory rather than a wall.
GH_TIMEOUT_SEC = 30

#: `git remote get-url` and friends: local, no network, so a second here is already pathological.
GIT_TIMEOUT_SEC = 10

#: The one query. `number` is what the predicate needs; the other three exist so the refusal can
#: **name** what it saved rather than assert a count — a message that says "1 open PR" sends
#: whoever reads it back to GitHub to find out which, and that round trip is where the guard's
#: credibility is spent.
PR_LIST_FIELDS = "number,title,headRefName,url"

#: Branches this guard will not evaluate away: deleting one is never routine, and the predicate is
#: the wrong question to ask about it — the integration branch is what PRs land on (and, where the
#: daemon runs off it, what is deployed), and the release branch is the rollback point. A deletion
#: aimed at one of these is refused outright.
#:
#: This is the FLOOR only (`repo_config.PROTECTED_FLOOR`), kept under its historical name as a
#: constant that never touches the disk. The live set — floor plus the owner's configured additions —
#: is :func:`protected_branches`, and every decision in this module and in `branch_sweep.py` reads
#: that.
PROTECTED_BRANCHES = repo_config.PROTECTED_FLOOR


def protected_branches() -> frozenset:
    """The branches no guard deletes: :data:`PROTECTED_BRANCHES` plus the owner's
    `pr-guard.json` `protected_branches` additions. Never raises — an unreadable config falls back to
    the floor, which can only protect more than a working config would remove."""
    try:
        return frozenset(repo_config.protected_branches()) | PROTECTED_BRANCHES
    except Exception:  # noqa: BLE001 — a config read in a hook never raises
        return PROTECTED_BRANCHES


#: `gh api repos/{owner}/{repo}/git/refs/heads/{branch}` — the REST spelling of a ref deletion, with
#: or without an `https://api.github.com/` prefix. The branch is the whole remainder because branch
#: names contain slashes (`docs/some-spec`).
_API_REF_RE = re.compile(
    r"(?:^|/)repos/([^/\s]+)/([^/\s]+)/git/refs/heads/(\S+?)/*$", re.IGNORECASE)

#: The GraphQL mutation that deletes a ref. A ref is named by node ID in a GraphQL body, not by
#: branch name, so **no branch is parseable out of one** — this is a refusal on sight, exactly as
#: `merge_guard` treats `mergePullRequest`.
_GRAPHQL_DELETE_REF_RE = re.compile(r"\bdeleteRef\b")

#: `owner/repo` out of a remote URL, both spellings git uses for GitHub.
_REMOTE_URL_RE = re.compile(
    r"^(?:https?://|git@|ssh://git@)github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/*$",
    re.IGNORECASE)

#: `gh api`'s method flag, glued or split. `-XDELETE` is the spelling that would slip past a naive
#: equality check, which is why this is a regex and not a set membership.
_METHOD_GLUED_RE = re.compile(r"^(?:-X|--method)=?(.+)$")
_METHOD_BARE = frozenset({"-X", "--method"})

#: `gh pr merge`'s branch-deleting flag. Go's pflag accepts `--flag=false` on a boolean, so the
#: `=value` form is read rather than assumed — `--delete-branch=false` deletes nothing and must not
#: be refused, and a value that is neither true nor false is a parse this module does not understand.
_DELETE_BRANCH_FLAGS = frozenset({"-d", "--delete-branch"})
_BOOL_TRUE = frozenset({"", "1", "t", "true", "y", "yes"})
_BOOL_FALSE = frozenset({"0", "f", "false", "n", "no"})

#: `git push` flags that take no value. Everything absent from this set and from
#: :data:`_GIT_PUSH_VALUE_FLAGS` is a **refusal**, not a shrug: an unknown flag that silently ate no
#: value would let the next token be read as a branch name while git read it as the flag's argument,
#: and the guard would then evaluate a branch nobody was deleting.
_GIT_PUSH_BOOLEAN_FLAGS = frozenset({
    "-d", "--delete", "-f", "--force", "--force-with-lease", "--force-if-includes",
    "-n", "--dry-run", "-q", "--quiet", "-v", "--verbose", "--progress", "--no-progress",
    "--atomic", "--no-atomic", "--verify", "--no-verify", "--porcelain", "--prune",
    "--mirror", "--all", "--tags", "--follow-tags", "--thin", "--no-thin",
    "-u", "--set-upstream", "--signed", "--no-signed", "--ipv4", "--ipv6", "-4", "-6",
    "-h", "--help",
})
_GIT_PUSH_VALUE_FLAGS = frozenset({
    "--repo", "-o", "--push-option", "--receive-pack", "--exec",
})

#: Substrings that mean *the network could not be crossed*. Deliberately explicit rather than
#: "anything that is not recognised": widening this list widens the ALLOW path, so every entry
#: should be a phrase somebody has actually seen from `gh`.
_UNREACHABLE_MARKERS = (
    "dial tcp", "no such host", "network is unreachable", "connection refused",
    "connection reset", "context deadline exceeded", "i/o timeout", "timeout awaiting",
    "tls handshake timeout", "temporary failure in name resolution",
    "could not resolve host", "eof (github.com)", "502 bad gateway", "503 service",
    "504 gateway", "service unavailable", "server error", "bad gateway",
)

#: Substrings that mean *there is no GitHub repository here*. A correct negative, not an unknown.
_NOT_GITHUB_MARKERS = (
    "none of the git remotes configured for this repository",
    "no git remotes found", "not a git repository", "could not determine base repository",
)

#: Substrings that mean *credentials*. Named separately from the unknown bucket only so the refusal
#: can say to run `gh auth login` instead of leaving the reader to guess.
_AUTH_MARKERS = (
    "gh auth login", "authentication required", "bad credentials", "requires authentication",
    "http 401", "must be logged in", "no oauth token",
)


class GuardError(Exception):
    """A check that could not be satisfied. Always a BLOCK; the text is the reason."""


class Unreachable(Exception):
    """GitHub could not be reached. ALLOW, with the reason on stderr — see the module docstring's
    outcome 3. This is the one exception type in this module that does not refuse."""


class NotGitHub(Exception):
    """No GitHub repository is in play, so no pull request can be closed. A correct negative:
    ALLOW, silently. Outcome 4."""


# --------------------------------------------------------------------------- stage 1: detection

def _flag_name(token: str) -> str:
    """`--repo=x` -> `--repo`. The name is what gets looked up in the flag sets; the value, if any,
    came glued to it and is not a positional."""
    return (token or "").partition("=")[0]


def _bool_flag_value(token: str):
    """`--delete-branch` -> True, `--delete-branch=false` -> False, `--delete-branch=maybe` -> None.

    ``None`` is *"this module cannot read it"* and its caller turns that into a refusal. Guessing
    `True` would refuse a correct command; guessing `False` would clear a deletion nobody evaluated.
    """
    name, eq, value = (token or "").partition("=")
    if name not in _DELETE_BRANCH_FLAGS:
        return None
    if not eq:
        return True
    low = value.strip().lower()
    if low in _BOOL_TRUE:
        return True
    if low in _BOOL_FALSE:
        return False
    return None


def _strip_ref_prefix(ref: str) -> str:
    """`refs/heads/x` -> `x`. Both spellings reach the same branch, so both must reach the same
    predicate; a guard keyed on the short form is one `refs/heads/` away from not firing."""
    ref = (ref or "").strip().strip("'\"")
    for prefix in ("refs/heads/", "heads/"):
        if ref.lower().startswith(prefix):
            return ref[len(prefix):]
    return ref


def _repo_from_remote_token(token: str):
    """A `git push` remote that is a URL rather than a name -> `owner/repo`, else ``None``."""
    m = _REMOTE_URL_RE.match((token or "").strip().strip("'\""))
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _parse_gh_pr_merge_delete(tokens: list) -> list:
    """`gh pr merge …` tokens -> `[{kind, pr, repo}]` if it would delete the branch, else `[]`.

    The PR number and `--repo` come from :func:`merge_guard.parse_gh_pr_merge`, which already refuses
    a bare `gh pr merge`, a branch name and an unrecognised flag. Its :class:`merge_guard.GuardError`
    is re-raised as this module's, because the two mean the same thing here — *the command could not
    be read, so it is refused* — and a merge that module blocks is one this module has no business
    silently clearing on a parse it could not do either."""
    after = _tokens_after_merge(tokens)
    if after is None:
        return []
    deletes = False
    i = 0
    while i < len(after):
        tok = after[i]
        if tok == "--":
            break
        name = _flag_name(tok)
        if name in _DELETE_BRANCH_FLAGS:
            value = _bool_flag_value(tok)
            if value is None:
                raise GuardError(
                    f"cannot read {tok!r} as a true/false flag on `gh pr merge`. The guard refuses "
                    f"rather than guess: guessing 'off' would clear a branch deletion it never "
                    f"evaluated. Write it as a bare --delete-branch, or drop the flag.")
            deletes = deletes or value
        elif name in mg.VALUE_FLAGS and "=" not in tok:
            i += 1  # the next token is this flag's value, not a flag of its own
        i += 1
    if not deletes:
        return []
    parsed = _parse_or_raise(tokens)
    return [{"kind": "gh-pr-merge", "pr": parsed["pr"], "repo": parsed.get("repo")}]


def _tokens_after_merge(tokens: list):
    """The argument tokens of a `gh pr merge` segment, redirections removed, or ``None`` if this
    segment is not one. Mirrors :func:`merge_guard.parse_gh_pr_merge`'s own slicing so the two read
    the same token stream."""
    rest = tokens[1:]
    if "pr" not in rest:
        return None
    after = rest[rest.index("pr") + 1:]
    if "merge" not in after:
        return None
    return mg.strip_redirections(after[after.index("merge") + 1:])


def _parse_or_raise(tokens: list) -> dict:
    try:
        return mg.parse_gh_pr_merge(tokens)
    except mg.GuardError as exc:
        raise GuardError(f"{exc} (read while checking whether this merge deletes a branch)")


def _parse_gh_api_delete(tokens: list) -> list:
    """`gh api …` tokens -> `[{kind, branch, repo}]` for a ref deletion, else `[]`.

    Both halves must be present: the method must be DELETE **and** a token must be the
    `git/refs/heads/…` path. A `gh api` GET against that path is a read and is left alone."""
    rest = tokens[1:]
    if "api" not in rest:
        return []
    args = mg.strip_redirections(rest[rest.index("api") + 1:])
    joined = " ".join(args)
    if _GRAPHQL_DELETE_REF_RE.search(joined):
        raise GuardError(
            "this is a GraphQL `deleteRef` mutation. A ref is named there by node ID, not by branch "
            "name, so no branch can be read out of it and the predicate cannot be evaluated at all. "
            "Use `gh api repos/<owner>/<repo>/git/refs/heads/<branch> -X DELETE`, which names the "
            "branch, and the guard will check it.")

    method, i = None, 0
    while i < len(args):
        tok = args[i]
        if tok in _METHOD_BARE:
            method = args[i + 1] if i + 1 < len(args) else None
            i += 1
        else:
            m = _METHOD_GLUED_RE.match(tok)
            if m:
                method = m.group(1)
        i += 1
    if (method or "").strip().strip("'\"").upper() != "DELETE":
        return []

    out = []
    for tok in args:
        m = _API_REF_RE.search((tok or "").strip().strip("'\""))
        if m:
            out.append({"kind": "gh-api-ref",
                        "branch": _strip_ref_prefix(m.group(3)),
                        "repo": f"{m.group(1)}/{m.group(2)}"})
    if not out:
        raise GuardError(
            "this is a `gh api` DELETE whose path the guard could not read as "
            "`repos/<owner>/<repo>/git/refs/heads/<branch>`. It refuses rather than assume the "
            "request deletes nothing: if this DELETE does remove a branch, the guard would be "
            "clearing a deletion it never evaluated. Spell the path out, or use "
            "`git push <remote> --delete <branch>`.")
    return out


def _parse_git_push_delete(tokens: list) -> list:
    """`git push …` tokens -> `[{kind, branch, remote}]` for each branch it would delete, else `[]`.

    Two spellings, and the second is a straight bypass of the first: `--delete`/`-d` with refspecs,
    and the colon refspec `:<branch>` whose empty source side means *push nothing onto it*, which is
    how git spells a deletion without ever using the word.

    **An unrecognised flag refuses.** See :data:`_GIT_PUSH_BOOLEAN_FLAGS` — this is
    `merge_guard.parse_gh_pr_merge`'s argument in a second place, and it is soundness rather than
    pedantry: a flag wrongly assumed to take no value shifts every positional after it by one."""
    rest = tokens[1:]
    if "push" not in rest:
        return []
    args = mg.strip_redirections(rest[rest.index("push") + 1:])

    delete_mode, positionals, i = False, [], 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            positionals.extend(args[i + 1:])
            break
        if tok.startswith("-") and tok != "-":
            name = _flag_name(tok)
            if name in ("-d", "--delete"):
                delete_mode = True
            elif name in _GIT_PUSH_VALUE_FLAGS:
                if "=" not in tok:
                    i += 1
            elif name not in _GIT_PUSH_BOOLEAN_FLAGS:
                raise GuardError(
                    f"unrecognised flag {name!r} on `git push`. The guard refuses rather than guess "
                    f"whether it takes a value: guessing wrong shifts every positional after it by "
                    f"one, so it would read the wrong token as the branch name and clear a deletion "
                    f"it never evaluated. Re-run without it, or delete the branch with "
                    f"`git push <remote> --delete <branch>`.")
        else:
            positionals.append(tok)
        i += 1

    # `git push` with nothing but flags names no remote and no ref: git errors on it, and there is
    # no branch here to ask about.
    if len(positionals) < 2:
        return []
    remote, refspecs = positionals[0], positionals[1:]

    out = []
    for spec in refspecs:
        spec = (spec or "").strip().strip("'\"")
        if spec.startswith(":"):
            branch = _strip_ref_prefix(spec[1:])
        elif delete_mode and ":" not in spec:
            branch = _strip_ref_prefix(spec)
        elif delete_mode and spec.endswith(":"):
            # `--delete src:` is not a deletion git accepts; refusing beats reading half of it.
            raise GuardError(f"cannot read the refspec {spec!r} on a deleting `git push`")
        else:
            continue
        if branch:
            out.append({"kind": "git-push", "branch": branch, "remote": remote})
    return out


def deletion_invocations(command: str) -> list:
    """Every remote-branch deletion in `command`. `[]` means stage 2 never runs.

    Raises :class:`GuardError` for a shape it identified but could not read — a refusal, not an
    error — which is why this is the boundary between the fail-open half and the fail-closed half
    rather than a place to be tolerant."""
    out = []
    for tokens in mg.command_segments(command, ("gh", "git")):
        head = mg._strip_exe(tokens[0])
        if head == "gh":
            out.extend(_parse_gh_pr_merge_delete(tokens))
            out.extend(_parse_gh_api_delete(tokens))
        elif head == "git":
            out.extend(_parse_git_push_delete(tokens))
    return out


def looks_like_deletion(command: str) -> bool:
    """Stage 1's whole question, for callers that only want the boolean. Never raises: a
    :class:`GuardError` here means *"a deletion, which I could not read"*, and that is still a
    deletion."""
    try:
        return bool(deletion_invocations(command))
    except GuardError:
        return True


# ------------------------------------------------------------------- stage 2: THE PREDICATE

def _run(argv: list, cwd=None, timeout: int = GH_TIMEOUT_SEC) -> tuple:
    """`(returncode, stdout, stderr)`. The one subprocess call, so tests replace one seam."""
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=cwd or None)
    return proc.returncode, proc.stdout, proc.stderr


def classify_gh_failure(text: str) -> Exception:
    """A failed `gh` invocation's output -> the exception that says what to do about it.

    This function is the whole fail-open/fail-closed seam, so it is written as a **whitelist in the
    allowing direction**: only a phrase that positively means *the network could not be crossed*
    reaches :class:`Unreachable`. Everything unrecognised is a :class:`GuardError`, i.e. a block."""
    low = (text or "").lower()
    for marker in _NOT_GITHUB_MARKERS:
        if marker in low:
            return NotGitHub(text.strip())
    for marker in _UNREACHABLE_MARKERS:
        if marker in low:
            return Unreachable(text.strip())
    for marker in _AUTH_MARKERS:
        if marker in low:
            return GuardError(
                "`gh` is not authenticated, so the guard could not ask GitHub whether an open pull "
                "request is stacked on this branch. That is NOT GitHub being unreachable — it is "
                "this machine being unable to ask — so the deletion is refused rather than waved "
                "through. Run `gh auth login` and retry.\n\n  gh said: " + text.strip())
    return GuardError(
        "`gh` failed and the guard could not classify the failure as a network outage, so it "
        "refuses rather than assume the branch is safe to delete.\n\n  gh said: " + text.strip())


def _gh_json(argv: list, cwd=None, runner=None):
    """Run `gh`, parse its JSON, or raise one of the three outcome exceptions."""
    run = runner or _run
    try:
        rc, out, err = run(argv, cwd)
    except FileNotFoundError:
        raise GuardError(
            "the `gh` CLI is not on PATH, so the guard could not ask GitHub whether an open pull "
            "request is stacked on this branch. That is NOT GitHub being unreachable — it is this "
            "machine missing the only tool that can answer — so the deletion is refused.")
    except subprocess.TimeoutExpired:
        raise Unreachable(f"`{' '.join(argv[:3])} …` did not answer within {GH_TIMEOUT_SEC}s")
    except OSError as exc:
        raise GuardError(f"could not run `gh`: {exc!r}")
    if rc != 0:
        raise classify_gh_failure(err or out)
    try:
        parsed = json.loads(out or "[]")
    except (TypeError, ValueError):
        raise GuardError(
            "`gh` exited 0 but its output was not JSON the guard could read. It refuses rather "
            "than read an unparseable answer as 'no dependent pull requests'.")
    if not isinstance(parsed, list):
        raise GuardError(f"`gh` returned {type(parsed).__name__}, not a list of pull requests")
    return parsed


def open_prs_based_on(branch: str, repo=None, cwd=None, runner=None) -> list:
    """**THE PREDICATE.** Every OPEN pull request whose `base` is `branch`. Empty list = safe.

    One `gh` call, and the same one a person would run by hand to check. It is defined here and imported by `branch_sweep.py`; there is no second implementation, because a
    sweep whose idea of "safe" can drift from the hook's is the automated version of the bug.

    `repo=None` lets `gh` resolve the repository from `cwd` — which is the same resolution the
    command being judged would use, so this is not the guess `merge_guard` refuses to make about a
    branch name. Where the command names a repository (`gh api …/repos/o/r/…`, a `git push` to a
    remote whose URL is readable) it is passed explicitly and nothing is inferred."""
    argv = ["gh", "pr", "list", "--state", "open", "--base", branch,
            "--json", PR_LIST_FIELDS, "--limit", "100"]
    if repo:
        argv += ["--repo", repo]
    return _gh_json(argv, cwd=cwd, runner=runner)


def repo_for_remote(remote: str, cwd=None, runner=None):
    """A `git push` remote name -> `owner/repo`, or ``None`` for a remote that is not on GitHub.

    Local, deterministic, and derived from something the command actually named — which is what
    keeps the git-push path from asking `gh` about whatever repository the shell happens to sit in.
    A remote that cannot be resolved raises: `git push` to it would have failed too, so refusing
    costs a command that was not going to work."""
    direct = _repo_from_remote_token(remote)
    if direct:
        return direct
    run = runner or _run
    try:
        rc, out, err = run(["git", "remote", "get-url", remote], cwd, GIT_TIMEOUT_SEC)
    except FileNotFoundError:
        raise GuardError("`git` is not on PATH, so the push target could not be resolved")
    except subprocess.TimeoutExpired:
        raise GuardError(f"`git remote get-url {remote}` did not answer")
    except OSError as exc:
        raise GuardError(f"could not run `git remote get-url`: {exc!r}")
    if rc != 0:
        raise GuardError(
            f"could not resolve the remote {remote!r} to a URL, so the guard cannot tell which "
            f"repository this deletion targets or which pull requests it would close. It refuses "
            f"rather than check the wrong repository.\n\n  git said: {(err or out).strip()}")
    return _repo_from_remote_token((out or "").strip())


# --------------------------------------------------------------------------- the decision

def _describe(pr: dict) -> str:
    number = pr.get("number")
    title = (pr.get("title") or "").strip()
    head = (pr.get("headRefName") or "").strip()
    url = (pr.get("url") or "").strip()
    line = f"  * #{number}"
    if head:
        line += f"  ({head})"
    if title:
        line += f"  {title[:80]}"
    if url:
        line += f"\n      {url}"
    return line


def refusal(branch: str, prs: list, how: str) -> str:
    """The message. It **names** the pull requests rather than counting them, because the whole cost
    of this failure is working out afterwards which PR was closed and what its base had been — and
    because a refusal whose reader has to go to GitHub to understand it is a refusal that
    gets argued with."""
    plural = "s" if len(prs) != 1 else ""
    return (
        f"Blocked: deleting the branch `{branch}` would CLOSE {len(prs)} open pull request{plural}.\n"
        f"\n"
        f"GitHub does not reliably retarget a pull request whose base branch is deleted. It closes "
        f"it, and recovery means recreating the deleted ref, reopening the pull request and "
        f"retargeting it by hand.\n"
        f"\n"
        f"Stacked on `{branch}` right now:\n"
        f"{chr(10).join(_describe(p) for p in prs)}\n"
        f"\n"
        f"Do this instead, in this order:\n"
        f"  1. retarget each one first:  gh pr edit <number> --base <the base you want>\n"
        f"  2. confirm nothing is left:  gh pr list --state open --base {branch} --json number\n"
        f"  3. then delete the branch.\n"
        f"\n"
        f"If the merge is what you wanted, run it WITHOUT --delete-branch. The branch is then swept "
        f"later by seneschal/scripts/branch_sweep.py, which applies this same predicate per branch "
        f"immediately before each deletion.\n"
        f"\n"
        f"(refused on: {how})")


def protected_refusal(branch: str, how: str) -> str:
    return (
        f"Blocked: `{branch}` is a protected branch and this command would delete it.\n"
        f"\n"
        f"The integration branch is what pull requests land on — and, where the daemon runs off "
        f"it, what is deployed — and the release branch is the rollback point. Deleting one is never "
        f"routine, and 'no open pull request is stacked on it' is the wrong question to ask about "
        f"it. (Protected: the fixed floor plus references/pr-guard.json's protected_branches.)\n"
        f"\n"
        f"If this is genuinely intended, do it from the GitHub web UI where it is a deliberate act "
        f"with a confirmation, not a flag on another command.\n"
        f"\n"
        f"(refused on: {how})")


def _how(inv: dict) -> str:
    kind = inv.get("kind")
    if kind == "gh-pr-merge":
        return f"gh pr merge {inv.get('pr')} --delete-branch"
    if kind == "gh-api-ref":
        return f"gh api …/git/refs/heads/{inv.get('branch')} -X DELETE"
    return f"git push {inv.get('remote')} --delete {inv.get('branch')}"


def _pr_head_branch(inv: dict, cwd=None, runner=None):
    """The `gh pr merge` case's own resolver — an object response, so it cannot share
    :func:`_gh_json`'s list contract."""
    run = runner or _run
    argv = ["gh", "pr", "view", str(inv["pr"]), "--json", "headRefName"]
    if inv.get("repo"):
        argv += ["--repo", inv["repo"]]
    try:
        rc, out, err = run(argv, cwd)
    except FileNotFoundError:
        raise GuardError(
            "the `gh` CLI is not on PATH, so the guard could not learn which branch "
            f"`gh pr merge {inv['pr']} --delete-branch` would delete. Refused rather than assumed.")
    except subprocess.TimeoutExpired:
        raise Unreachable(f"`gh pr view {inv['pr']}` did not answer within {GH_TIMEOUT_SEC}s")
    except OSError as exc:
        raise GuardError(f"could not run `gh`: {exc!r}")
    if rc != 0:
        raise classify_gh_failure(err or out)
    try:
        branch = (json.loads(out or "{}") or {}).get("headRefName")
    except (TypeError, ValueError):
        branch = None
    if not branch:
        raise GuardError(
            f"`gh` did not report a head branch for PR #{inv['pr']}, so the guard cannot tell which "
            f"branch --delete-branch would remove, and refuses rather than clear an unknown one.")
    return branch


def evaluate(command: str, cwd=None, runner=None) -> tuple:
    """`command` -> `(exit_code, message)`. The whole decision, with no I/O of its own.

    Order matters and is the design: a BLOCK anywhere in a chained command wins over an advisory
    anywhere else in it, because `a && b` runs both and the guard refuses the pair or neither."""
    try:
        invocations = deletion_invocations(command)
    except GuardError as exc:
        return EXIT_BLOCK, (
            f"Blocked: this command deletes a branch, and the guard could not read it well enough "
            f"to check whether an open pull request is stacked on that branch.\n\n{exc}")
    if not invocations:
        return EXIT_ALLOW, ""

    blocks, advisories = [], []
    for inv in invocations:
        how = _how(inv)
        try:
            if inv["kind"] == "gh-pr-merge":
                branch, repo = _pr_head_branch(inv, cwd=cwd, runner=runner), inv.get("repo")
            else:
                branch, repo = _branch_for(inv, cwd=cwd, runner=runner)
            if branch in protected_branches():
                blocks.append(protected_refusal(branch, how))
                continue
            prs = open_prs_based_on(branch, repo=repo, cwd=cwd, runner=runner)
            if prs:
                blocks.append(refusal(branch, prs, how))
        except NotGitHub:
            continue                      # outcome 4: nothing here can close a pull request
        except Unreachable as exc:
            advisories.append(f"{how}: {exc}")
        except GuardError as exc:
            blocks.append(f"Blocked: {how}\n\n{exc}")
    if blocks:
        return EXIT_BLOCK, "\n\n----\n\n".join(blocks)
    if advisories:
        return EXIT_ADVISORY, (
            "branch-delete guard: ALLOWED WITHOUT CHECKING — GitHub could not be reached, so it is "
            "unknown whether an open pull request is stacked on the branch being deleted. This is "
            "the guard's fail-open path, not a clean verdict.\n  "
            + "\n  ".join(advisories)
            + "\nIf a pull request disappears, look for it in `gh pr list --state closed` first: it "
              "was closed, not lost, and `gh pr reopen` after recreating the ref recovers it.")
    return EXIT_ALLOW, ""


def _branch_for(inv: dict, cwd=None, runner=None):
    """`(branch, repo)` for the two invocation kinds that name their branch outright."""
    if inv["kind"] == "gh-api-ref":
        return inv["branch"], inv.get("repo")
    repo = repo_for_remote(inv["remote"], cwd=cwd, runner=runner)
    if repo is None:
        raise NotGitHub(f"remote {inv['remote']!r} is not on github.com")
    return inv["branch"], repo


def decide(event) -> tuple:
    """One `PreToolUse` event -> `(exit_code, message)`. Never raises on a hostile shape; a payload
    this hook cannot understand is a payload it has no opinion about, and **that** unknown fails
    open — it is stage 1's unknown, not stage 2's."""
    if not isinstance(event, dict):
        return EXIT_ALLOW, ""
    if event.get("tool_name") not in GUARDED_TOOLS:
        return EXIT_ALLOW, ""
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return EXIT_ALLOW, ""
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return EXIT_ALLOW, ""
    cwd = event.get("cwd")
    return evaluate(command, cwd=cwd if isinstance(cwd, str) else None)


# --------------------------------------------------------------------------- entrypoints

def hook_main(stdin=None, stderr=None) -> int:
    """Hook entrypoint. Spawned with no argv; reads the event on stdin.

    **The wrapper is around stage 1's reading, and nothing else.** An unreadable stdin or a payload
    that is not JSON allows silently, because that is a hook the harness called wrong rather than a
    branch deletion. Once :func:`decide` has run, its verdict is returned as-is: a refusal computed
    by stage 2 may not be swallowed by an except clause."""
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        if not raw or not raw.strip():
            return EXIT_ALLOW
        event = json.loads(raw)
    except Exception:  # noqa: BLE001 -- stage 1 fails open; see the module docstring
        return EXIT_ALLOW
    try:
        code, message = decide(event)
    except Exception as exc:  # noqa: BLE001 -- a raise INSIDE the decision is an unknown, and
        code, message = EXIT_BLOCK, (   # stage 2's unknowns block. See merge_guard's FailClosedTest.
            "Blocked: the branch-delete guard raised while deciding whether this command would "
            f"close an open pull request, so it does not know. It refuses rather than guess.\n\n"
            f"  {type(exc).__name__}: {exc}")
    if message:
        try:
            (stderr if stderr is not None else sys.stderr).write(message + "\n")
        except Exception:  # noqa: BLE001 -- a block we cannot explain is still a correct block
            pass
    return code


def _cmd_check(args) -> int:
    """`check --branch X` — the read-only diagnostic the setup doc verifies with. Runs the predicate
    and nothing else: it deletes nothing, records nothing, and prints the command that would be
    safe rather than performing it."""
    try:
        prs = open_prs_based_on(args.branch, repo=args.repo, cwd=args.cwd)
    except NotGitHub as exc:
        print(f"not a GitHub repository here ({exc}) — nothing to check")
        return EXIT_ALLOW
    except Unreachable as exc:
        print(f"UNREACHABLE: {exc}")
        print("the guard would ALLOW and say so (exit 1); this is not a clean verdict")
        return EXIT_ADVISORY
    except GuardError as exc:
        print(f"REFUSED: {exc}")
        return EXIT_BLOCK
    if args.branch in protected_branches():
        print(protected_refusal(args.branch, "check"))
        return EXIT_BLOCK
    if not prs:
        print(f"safe: no open pull request has `{args.branch}` as its base")
        return EXIT_ALLOW
    print(refusal(args.branch, prs, "check"))
    return EXIT_BLOCK


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return hook_main()
    parser = argparse.ArgumentParser(
        description="Refuse to delete a remote branch that an open pull request is stacked on.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="run the predicate against one branch (read-only)")
    check.add_argument("--branch", required=True)
    check.add_argument("--repo", default=None, help="owner/name; default: resolved from --cwd")
    check.add_argument("--cwd", default=None)
    check.set_defaults(func=_cmd_check)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
