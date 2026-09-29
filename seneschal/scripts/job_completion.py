#!/usr/bin/env python3
"""The job-completion guarantee: detect, resume, rescue. Stdlib only.

**The problem this exists for.** `jobs.py` guarantees a job reaches a terminal state and notifies
about it exactly once — but "terminal" was never the same claim as "the work actually landed." A
job's agent can launch the test suite (or anything else) with `run_in_background` and then end its
turn to wait for a completion notification that a single-prompt `claude -p` process can never
receive, because the process ends when the turn does. The job exits 0, `jobs.py` reports `done`, and
the log holds only the agent's last sentence. The stranded work takes every shape: a PR opened whose
report never reached the log; a commit never pushed; hundreds of lines staged and never committed.
Left alone, each is recovered only if a human happens to notice — and noticing is not a mechanism.

**The fix is not "keep the process alive."** `claude -p` is meant for a single prompt, and nothing
here tries to hold a `-p` process open past its turn. Instead something OUTSIDE the process —
`jobs.reconcile`'s existing tick — notices what the process left behind: a mechanism outside the
turn, not a rule inside the brief. A prose instruction telling an agent "commit before you finish"
is exactly the kind of rule that fails silently and repeatedly.

**Three layers, run in order, each bounded:**

1. **DETECT** (`detect_incomplete`). Before a job that reached a success-like terminal status
   (`DONE`/`ended-unknown`) is allowed to notify as such, its working directory is inspected for:
   staged-but-uncommitted changes, unstaged changes to tracked files, commits on the current branch
   that were never pushed to `origin`, and a fully-pushed branch with no open pull request. **A job
   that legitimately produced no changes is COMPLETE, not incomplete** — every check is additive
   evidence, never a default suspicion, and an unreadable git/gh call fails OPEN (skips that one
   condition) rather than flagging a job on ambiguous evidence. **"Unpushed" means unreachable from
   every remote-tracking ref** (`remote_refs_containing_head`, the plumbing form of `git branch -r
   --contains HEAD`), detached or not — never merely "ahead of `origin/develop`". A job run with
   `--cwd` in a worktree detached at a commit that is already some remote branch's tip touches
   nothing and exits 0; reading its ahead-of-base count as orphan work would have RESCUE push
   `rescue/<id>` at a commit origin already has and mark the job FAILED. Both halves ask the same
   question first.

2. **RESUME** (`resume_argv` / `can_resume` / `build_resume_prompt`). If the job's `claude -p`
   invocation had a session id captured up front (`prepare_argv`, called from `jobs.start_job`), an
   incomplete job is handed back to that exact session with `claude --resume <id> <carried flags...>
   -p "<prompt>"` — a verified mechanism (`claude --help`: `--resume` continues a session set up front
   by `--session-id`, and both compose with `-p`), not a guess. **The carried flags matter as much as
   the resume itself** (`_carried_flags`, checked against `claude --help`): `--permission-mode`,
   `--model`, `--allowedTools`/`--disallowedTools`, `--dangerously-skip-permissions`, `--mcp-config`,
   `--add-dir`, `--settings`, `--append-system-prompt`, read from `rec["original_argv"]` if present
   else `rec["argv"]` — never from a caller-picked default. Without them a resumed session silently
   reverts to the CLI's default permission mode, and a job launched under
   `--permission-mode bypassPermissions` has its `git push`/`gh`/`python` refused with no human
   present — failing with its work stranded committed-but-unpushed, the very outcome RESUME exists to
   prevent. The prompt is a **module constant** (`RESUME_PROMPT_TEMPLATE`), never caller-supplied
   text, and it says exactly what was found and what to do — commit, push, open the PR, and never
   background anything, because the agent that just lost a push to that exact mistake is the one
   being resumed. Bounded at `MAX_RESUME_ATTEMPTS` (2): a job with no captured session id, or one
   that has already exhausted its attempts, cannot be resumed and falls straight to rescue.
   `jobs.reconcile` owns the actual respawn (via its existing `_spawn_attempt` — a resume is spawned
   exactly like a fresh attempt, so the whole detached-survival/deadline/kill machinery applies to it
   unmodified) and the actual record write; this module only decides and never touches disk.

3. **RESCUE** (`rescue`). Resume exhausted (or impossible) and the job is still incomplete: commit
   whatever is staged/dirty onto `rescue/<job-id>` and push it — **never force**, so a push that would
   overwrite existing history on that name is refused rather than silently winning a race; **never a
   tip that is already on a remote ref** (the same `remote_refs_containing_head` read DETECT uses —
   a rescue push there would only duplicate a commit origin has, under a second name); **never a
   PR** from a rescue branch, because inventing a merge is not what this exists to do; **never on
   `develop`/`master`** — a job that ran directly in a *shared* checkout (no `--worktree`) on the
   protected branch is refused rescue outright rather than auto-committing onto the branch the live
   daemon deploys from (`jobs.REPO_ROOT`). The job is then reported `FAILED`, naming the rescue
   branch (or the reason rescue itself couldn't run).

**Scoped to jobs with a job-specific working directory, deliberately** (`is_scoped_cwd`). The default
`cwd` for a job with neither `--worktree` nor `--cwd` is the daemon's own live checkout
(`jobs.REPO_ROOT`) — shared by every such job. Running DETECT there unconditionally would attribute
whatever happened to be dirty in that shared tree, at that exact instant, to whichever unrelated job
(say, a PR watcher) happened to finish next — a false positive with no upside, since a `--worktree`
job (the recommended shape for every delegated coding task) already gets a directory nothing else
touches. So DETECT only ever runs for a job that has its own worktree, or an explicit `--cwd`
different from the shared root — "a known repo cwd" is read as *a cwd this job does not share*, not
merely one that happens to be a git repository.

**Honesty about what is tested, since a fake that proves nothing is worse than an honest gap.**
DETECT and RESCUE are exercised in `test_job_completion.py` against **real temporary git
repositories** with a local bare remote as `origin` — genuine git plumbing, no mocks standing in for
behaviour this module needs to get right (the `git worktree remove`-survives-a-committed-but-unpushed-
branch fact this design leans on was verified against real git; a canned `CompletedProcess` would
have needed to already know the answer). The `gh pr list` call is behind its own injectable seam
(`gh_runner`) and is always faked in tests, since a real one needs network and auth CI does not have.
**RESUME cannot be exercised end to end** — spawning a real `claude` process from a test is not
feasible here — so only its DECISION logic (`can_resume`, bounding, prompt/argv construction) is
covered; the actual respawn is `jobs.reconcile`'s existing, separately-tested `_spawn_attempt` path,
unmodified by this feature.

**Named residuals, not papered over:**

  * A job that both opts into `--retry` AND needs a resume will get `_run_shim`'s retry bookkeeping
    re-evaluated on the resumed attempt (its `retry` config is still on the record) — a resumed success
    can overwrite `retry_outcome` with a value describing the SAME outcome a second time, and a resumed
    failure can be scheduled for a further transient retry. Neither is incorrect, both are just two
    independently-designed safety nets composing rather than one subordinating the other; not worth
    entangling further given how rare a retry-configured coding job is.
  * `_run_shim`'s per-attempt numbering (`rec["attempt"]`) is gated on `rec.get("retry")`, so a resumed
    attempt on a plain (non-`--retry`) job still stamps `"attempt": 1` in every `attempts[]` entry it
    appends — `completion.resume_attempts` on the record, not `attempts[]`, is the authoritative count
    of how many times this feature respawned the job.
  * `_open_attempt_log`'s per-attempt delimiter is the same `retry`-gated shape, so a resumed attempt's
    output is appended to the log with no separator from the original run's. Cosmetic only.
  * `_carried_flags`'s variadic flags (`--add-dir`/`--allowedTools`/`--disallowedTools`/`--mcp-config`)
    read their values greedily up to the next `-`-prefixed token or the end of the source argv — the
    same boundary a real `-`-prefixed flag always gives it. If one of those flags were the LAST flag on
    the original command line and the bare positional prompt trailed directly after it with no flag in
    between, that prompt text would be read as one more value for the flag. The framework's own argv
    construction (`presence.py`, the `jobs.py start --worktree -- claude -p "<brief>"` shape in
    `background-jobs-spec.md`) always places the prompt immediately next to `-p`, but nothing enforces
    that shape on a caller, so it is named rather than assumed impossible.

**This is the reactive half of a two-part fix — `job_background_guard.py` is the preventive half.**
This module cleans up after a job backgrounds a step and ends its turn before the result can land;
the hook refuses that exact Bash call before it ever runs, scoped to a job process via the
`SENESCHAL_JOB_ID` `jobs.child_env` stamps. Neither retires the other: the hook cannot help a job
launched without it installed (or one whose brief finds some other way to strand work), and this
module's git-level DETECT has no opinion about *why* work was left uncommitted.

Design + rationale: `seneschal/docs/background-jobs-spec.md` §3.13-§3.14. Schema: `seneschal/state/README.md`.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import job_worktree  # BASE_REMOTE / BASE_BRANCH ("origin/develop", spelled in full) + the Runner seam

# Bounded, deliberately small: a resume is a real `claude` turn (real spend, real time), and the point
# is to close the specific "forgot to land it" gap, not to retry indefinitely against a job that keeps
# declining to finish.
MAX_RESUME_ATTEMPTS = 2

STAGED_UNCOMMITTED = "staged-uncommitted"
UNSTAGED_TRACKED = "unstaged-tracked"
UNPUSHED_COMMITS = "unpushed-commits"
PUSHED_NO_PR = "pushed-no-pr"

REASON_TEXT = {
    STAGED_UNCOMMITTED: "changes are staged (`git add`) but never committed",
    UNSTAGED_TRACKED: "tracked files have uncommitted modifications",
    UNPUSHED_COMMITS: "there are commits on this branch that were never pushed to origin",
    PUSHED_NO_PR: "the branch is pushed but no open pull request targets it",
}

# A job that ran directly in a SHARED checkout (no `--worktree`) must never be auto-committed onto the
# branch that checkout deploys from or releases from. See the module docstring's RESCUE section.
PROTECTED_BRANCHES = ("develop", "master")

RESCUE_BRANCH_PREFIX = "rescue/"

# The fixed continuation prompt. A CONSTANT, not caller-supplied text, so what gets said to a resumed
# session cannot drift per-caller into yet another "please remember to commit" prose rule — the shape
# that fails silently and repeatedly.
RESUME_PROMPT_TEMPLATE = (
    "STOP. Your previous turn ended before this delegated job's work actually landed. An automated "
    "check of this job's working directory (branch: {branch}) found:\n"
    "{reasons_block}\n\n"
    "Do the following now, in the foreground, in order:\n"
    "1. Review any uncommitted changes and, if they are correct, stage and commit them with a "
    "Conventional Commits message.\n"
    "2. Push the current branch to origin.\n"
    "3. If no open pull request targets origin/develop from this branch yet, open one with "
    "`gh pr create`.\n\n"
    "Do not launch the test suite, or anything else, in the background and end your turn to wait for a "
    "result — your process ends the instant your turn ends, so a backgrounded command can never report "
    "back to you. That is exactly the mistake that left this job's work stranded the first time. Run "
    "verification in the FOREGROUND before you finish, and end your turn only once the work is "
    "committed, pushed, and — if applicable — the pull request is open."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _cmd_name(token) -> str:
    """The bare command a token names, lowercased and de-`.exe`'d. Deliberately the same tiny logic as
    `jobs._cmd_name` — duplicated rather than imported, because `jobs.py` imports this module and an
    import the other way would be circular (the same posture `job_worktree.py` documents for itself)."""
    try:
        text = str(token).replace("\\", "/").rstrip("/")
    except Exception:  # noqa: BLE001 — a weird argv element is unknown, and unknown is fail-open
        return ""
    name = text.rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _result_message(res) -> str:
    """git's/gh's own words for a failure: stderr, falling back to stdout, then the exit code. Mirrors
    `job_worktree._git_message` (not imported, same reasoning as `_cmd_name`)."""
    for stream in ("stderr", "stdout"):
        text = getattr(res, stream, "") or ""
        text = " ".join(str(text).split())
        if text:
            return text[:200]
    return f"exit {getattr(res, 'returncode', '?')}"


def _git(runner, cwd: str, *args) -> "subprocess.CompletedProcess":
    return runner.run(["git", "-c", "core.fsmonitor=false", *args], cwd=cwd)


def _gh(runner, cwd: str, *args) -> "subprocess.CompletedProcess":
    return runner.run(["gh", *args], cwd=cwd)


def _ok(res) -> bool:
    return getattr(res, "returncode", 1) == 0


# --------------------------------------------------------------------------- scope

def is_scoped_cwd(rec: dict, repo_root: str) -> bool:
    """Does this job have a working directory nothing else shares? True for any `--worktree` job
    (the record carries a `worktree` block regardless of whether teardown later leaks or clears it),
    or a job whose `cwd` is not the shared default. False for the common case of a job that ran with
    neither flag — see the module docstring for why that case must be excluded rather than merely
    tolerated."""
    if not isinstance(rec, dict):
        return False
    if rec.get("worktree"):
        return True
    cwd = rec.get("cwd")
    if not cwd or not repo_root:
        return False
    try:
        return os.path.normcase(os.path.abspath(cwd)) != os.path.normcase(os.path.abspath(repo_root))
    except Exception:  # noqa: BLE001 — an unresolvable path is not evidence of a private cwd
        return False


# --------------------------------------------------------------------------- git reads

def is_repo(cwd, *, runner) -> bool:
    if not cwd or not os.path.isdir(cwd):
        return False
    res = _git(runner, cwd, "rev-parse", "--is-inside-work-tree")
    return _ok(res) and (getattr(res, "stdout", "") or "").strip() == "true"


def current_branch(cwd, *, runner) -> str | None:
    """The checked-out branch name, or `None` on detached HEAD (or any read failure — a detached
    worktree is exactly what `job_worktree.create` hands the agent before it runs its own `checkout
    -b`, so `None` here is a real, common, non-error state)."""
    res = _git(runner, cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if not _ok(res):
        return None
    name = (getattr(res, "stdout", "") or "").strip()
    return None if name in ("", "HEAD") else name


def has_staged_changes(cwd, *, runner) -> bool:
    return not _ok(_git(runner, cwd, "diff", "--cached", "--quiet"))


def has_unstaged_changes(cwd, *, runner) -> bool:
    """Tracked files only — `git diff --quiet` never looks at untracked files, which is deliberate:
    a job's cwd routinely holds gitignored build/cache output, and flagging that would make every job
    read as dirty."""
    return not _ok(_git(runner, cwd, "diff", "--quiet"))


def _rev_parse(cwd, ref, *, runner) -> str | None:
    res = _git(runner, cwd, "rev-parse", "--verify", "--quiet", ref)
    if not _ok(res):
        return None
    return (getattr(res, "stdout", "") or "").strip() or None


def _commit_count(cwd, range_expr, *, runner) -> int | None:
    res = _git(runner, cwd, "rev-list", "--count", range_expr)
    if not _ok(res):
        return None
    try:
        return int((getattr(res, "stdout", "") or "").strip())
    except ValueError:
        return None


def _remote_branch_sha(cwd, remote, branch, *, runner) -> str | None:
    """The SHA `<branch>` is at on `remote`, straight from the remote via `ls-remote` — never a local
    remote-tracking ref, which can be stale the instant the agent pushed a branch nobody has fetched
    since. `None` when the branch does not exist there at all, which is the common "never pushed" case
    and is read as exactly that by the caller."""
    res = _git(runner, cwd, "ls-remote", remote, f"refs/heads/{branch}")
    if not _ok(res):
        return None
    line = (getattr(res, "stdout", "") or "").strip().splitlines()
    if not line or not line[0].split():
        return None
    return line[0].split()[0]


def remote_refs_containing_head(cwd, *, runner) -> list:
    """Every remote-tracking ref (`refs/remotes/*`) that already contains HEAD — the plumbing form of
    `git branch -r --contains HEAD`. Non-empty means HEAD is on the remote SOMEWHERE, whatever the
    current branch is called and whether there even is one: a commit reachable from any remote ref is
    never "unpushed". A worktree DETACHED at a commit that is already some remote branch's tip is
    `ahead` of `origin/develop` without holding any orphan work; reading it as unpushed would have
    RESCUE push `rescue/<id>` at a commit origin already has, failing a job that touched nothing.
    Empty on any read failure — fail-open here means "no evidence
    it's remote," which just falls through to the finer per-branch checks, never to a new flag."""
    res = _git(runner, cwd, "for-each-ref", "--format=%(refname:short)", "--contains", "HEAD",
               "refs/remotes/")
    if not _ok(res):
        return []
    refs = []
    for line in (getattr(res, "stdout", "") or "").splitlines():
        name = line.strip()
        # `origin/HEAD` is a symref alias for the default branch, not a branch of its own.
        if name and not name.endswith("/HEAD"):
            refs.append(name)
    return refs


def pr_open_for_branch(cwd, branch, *, gh_runner) -> bool | None:
    """Is there an open PR for `branch`? `None` — unknown, never flagged — when `gh_runner` is absent
    or the call fails for any reason (missing binary, no auth, no network): this one sub-check is
    fail-OPEN by design, the same posture the rest of `jobs.py` takes toward an ambiguous read."""
    if gh_runner is None:
        return None
    try:
        res = _gh(gh_runner, cwd, "pr", "list", "--head", branch, "--state", "open", "--json", "number")
    except Exception:  # noqa: BLE001 — a broken seam is unknown, not a finding
        return None
    if not _ok(res):
        return None
    try:
        data = json.loads(getattr(res, "stdout", "") or "[]")
    except (ValueError, TypeError):
        return None
    return bool(data)


# --------------------------------------------------------------------------- DETECT

def detect_incomplete(rec: dict, *, runner=None, gh_runner=None,
                      base_remote: str | None = None, base_branch: str | None = None) -> dict | None:
    """Layer 1. Returns `None` when the job's cwd shows nothing wrong (or is not a readable git repo
    at all — no evidence is not evidence of incompleteness), else `{"reasons": [...], "branch": ...,
    "detail": {...}}`. Every condition is additive evidence; see the module docstring for what each
    reason means and why an ambiguous git/gh read is skipped rather than flagged."""
    runner = runner or job_worktree.Runner()
    remote = base_remote or job_worktree.BASE_REMOTE
    base = base_branch or job_worktree.BASE_BRANCH
    cwd = (rec or {}).get("cwd")
    if not is_repo(cwd, runner=runner):
        return None

    reasons: list = []
    detail: dict = {}
    if has_staged_changes(cwd, runner=runner):
        reasons.append(STAGED_UNCOMMITTED)
    if has_unstaged_changes(cwd, runner=runner):
        reasons.append(UNSTAGED_TRACKED)

    branch = current_branch(cwd, runner=runner)
    head_sha = _rev_parse(cwd, "HEAD", runner=runner)
    ahead = _commit_count(cwd, f"{remote}/{base}..HEAD", runner=runner) if head_sha else None
    if ahead:
        if branch is None:
            detail["detached"] = True
        # "Ahead of origin/develop" is not "unpushed": first ask whether HEAD is already reachable
        # from ANY remote-tracking ref (detached or not) — if it is, nothing here needs pushing.
        on_remote = remote_refs_containing_head(cwd, runner=runner)
        remote_sha = _remote_branch_sha(cwd, remote, branch, runner=runner) if branch else None
        if on_remote:
            detail["remote_refs"] = on_remote
        elif branch is None:
            # Detached HEAD with real commits beyond the base AND on no remote ref: the agent
            # committed something and never made a branch for it, so nothing could ever be pushed
            # or PR'd from it.
            reasons.append(UNPUSHED_COMMITS)
            detail["unpushed_commits"] = ahead
        elif remote_sha is None:
            reasons.append(UNPUSHED_COMMITS)
            detail["unpushed_commits"] = ahead
        elif remote_sha != head_sha:
            unpushed = _commit_count(cwd, f"{remote_sha}..HEAD", runner=runner)
            if unpushed:
                reasons.append(UNPUSHED_COMMITS)
                detail["unpushed_commits"] = unpushed
        # The PR check applies only to a named branch whose OWN remote tip is exactly HEAD — a
        # commit that merely sits on some other remote ref has nothing this branch could PR.
        if (branch and branch not in PROTECTED_BRANCHES and remote_sha == head_sha
                and UNPUSHED_COMMITS not in reasons):
            if pr_open_for_branch(cwd, branch, gh_runner=gh_runner) is False:
                reasons.append(PUSHED_NO_PR)

    if not reasons:
        return None
    return {"reasons": reasons, "branch": branch, "detail": detail, "cwd": cwd}


# --------------------------------------------------------------------------- RESUME

def resume_attempts_used(rec: dict) -> int:
    attempts = ((rec or {}).get("completion") or {}).get("resume_attempts")
    return len(attempts) if isinstance(attempts, list) else 0


def can_resume(rec: dict) -> bool:
    """Bounded, and never for a job the caller cancelled — a cancel is a deliberate stop, and resuming
    it would be the module reversing a decision that was never its to make. In practice a cancelled
    job never reaches this feature at all (`jobs.reconcile` only calls it for a success-like terminal
    status), so this check is a second, cheap belt rather than the only one."""
    if not isinstance(rec, dict):
        return False
    if rec.get("status") == "cancelled":
        return False
    if not rec.get("claude_session_id"):
        return False
    return resume_attempts_used(rec) < MAX_RESUME_ATTEMPTS


def build_resume_prompt(detect_result: dict) -> str:
    reasons = (detect_result or {}).get("reasons") or []
    lines = "\n".join(f"  - {REASON_TEXT.get(r, r)}" for r in reasons) or "  - (no reason recorded)"
    branch = (detect_result or {}).get("branch") or "(none — HEAD is detached)"
    return RESUME_PROMPT_TEMPLATE.format(branch=branch, reasons_block=lines)


# Flags that shape a session and remain legal alongside `--resume` (each verified present in
# `claude --help` in this environment before being added here) — `resume_argv` carries these forward
# from the ORIGINAL launch so the resumed session runs under the same constraints as the one it
# replaces. Everything else on the original command line is dropped on purpose: `-p`/`--print` and
# its prompt and `--session-id` do not survive a resume (the resume supplies its own), and an
# unrecognized flag was never verified safe to repeat here.
_CARRY_SINGLE_VALUE = ("--permission-mode", "--model", "--settings", "--append-system-prompt")
_CARRY_VARIADIC = ("--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
                   "--mcp-config", "--add-dir")
_CARRY_BOOLEAN = ("--dangerously-skip-permissions",)


def _carried_flags(source_argv: list) -> list:
    """The subset of `source_argv` (a full original `claude ...` command line, `argv[0]` included)
    that `resume_argv` carries forward onto the resume command. Walks the argv once, keeping only the
    flags named in `_CARRY_SINGLE_VALUE`/`_CARRY_VARIADIC`/`_CARRY_BOOLEAN` above — a single-value
    flag takes exactly the one token after it, a variadic flag takes every following token up to the
    next `-`-prefixed one (the same greedy shape `claude`'s own option parser uses for `--add-dir`
    etc.), a boolean flag takes nothing. Everything unrecognized — the command name, `-p`/`--print`
    and the prompt after it, `--session-id`, any other flag and its value(s) — is silently skipped."""
    argv = list(source_argv or [])
    out: list = []
    i = 1  # argv[0] is the command name itself, never carried
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok in _CARRY_BOOLEAN:
            out.append(tok)
            i += 1
        elif tok in _CARRY_SINGLE_VALUE:
            out.append(tok)
            if i + 1 < n:
                out.append(argv[i + 1])
            i += 2
        elif tok in _CARRY_VARIADIC:
            out.append(tok)
            i += 1
            while i < n and not str(argv[i]).startswith("-"):
                out.append(argv[i])
                i += 1
        else:
            i += 1
    return out


def flag_value(argv: list, flag: str) -> str | None:
    """The token immediately following `flag` in `argv`, or `None` when `flag` is absent (or is the
    last token). Used to compare a single-value flag — today just `--permission-mode`
    (`permission_mode_of`) — across two argvs without re-deriving `_carried_flags`'s full parse."""
    argv = list(argv or [])
    for i, tok in enumerate(argv):
        if tok == flag:
            return str(argv[i + 1]) if i + 1 < len(argv) else None
    return None


def permission_mode_of(argv: list) -> str | None:
    """The `--permission-mode` value on `argv`, or `None` when it isn't there (the CLI's own default
    mode). `jobs.resume_permission_mismatch` uses this to compare a resumed attempt's mode against the
    original launch's."""
    return flag_value(argv, "--permission-mode")


def resume_argv(rec: dict, prompt: str) -> list | None:
    """`claude --resume <session-id> <carried-forward flags> -p "<prompt>"`, or `None` when this job
    has no captured session id at all — an older job, or one whose command was never `claude -p` in
    the first place. `None` here is the signal `evaluate()` reads as "cannot resume, go straight to
    rescue."

    The carried-forward flags come from `rec["original_argv"]` if present, else `rec["argv"]`, run
    through `_carried_flags`. `original_argv` is what `jobs.reconcile` stamps the FIRST time a resume
    overwrites `rec["argv"]` with the resume command itself (`jobs.py`, `rec.setdefault(
    "original_argv", list(rec.get("argv") or []))` runs immediately before `rec["argv"] =
    outcome["argv"]`) — reading `original_argv` here means a SECOND resume still sees the true
    original launch, not the first resume's already-stripped argv.

    **Why the flags carry.** A job launched as `claude -p --permission-mode bypassPermissions
    <prompt>` and resumed as bare `["claude", "--resume", sid, "-p", prompt]` loses its permission
    mode: the resumed session runs under the CLI's default, and its `git push`/`gh`/`python` calls are
    refused with no human present to approve them. Every resume attempt then fails identically and the
    job ends `failed` with its work committed-but-unpushed — the outcome RESUME exists to prevent, on
    the one class of job (anything that touches git) that most needs a non-default permission mode to
    ever land its work."""
    sid = (rec or {}).get("claude_session_id")
    if not sid:
        return None
    source = (rec or {}).get("original_argv") or (rec or {}).get("argv") or []
    return ["claude", "--resume", str(sid), *_carried_flags(source), "-p", prompt]


def prepare_argv(argv: list) -> tuple:
    """Called once, from `jobs.start_job`, before a job is ever spawned. Injects a fresh
    `--session-id <uuid>` into a bare single-prompt `claude` invocation so this feature can
    `--resume` it later — a verified mechanism (`claude --help`), set up front rather than
    discovered after the fact, because there is no way to learn a session's id from outside it once
    it has already started.

    Returns `(argv, session_id_or_None)`. `argv` is returned UNCHANGED — not even copied differently —
    for anything that is not `claude`, that already carries `--resume`/`--session-id` (the caller is
    managing its own session identity; this must never override that), or that has no `-p`/`--print`
    at all (a job with no single prompt is not the shape this feature resumes)."""
    argv = list(argv or [])
    if not argv or _cmd_name(argv[0]) != "claude":
        return argv, None
    rest = argv[1:]
    if "--resume" in rest or "--session-id" in rest:
        return argv, None
    if "-p" not in rest and "--print" not in rest:
        return argv, None
    sid = str(uuid.uuid4())
    return [argv[0], "--session-id", sid, *rest], sid


# --------------------------------------------------------------------------- RESCUE

def _rescue_commit_message(job_id: str, detect_result: dict | None) -> str:
    reasons = (detect_result or {}).get("reasons") or []
    words = ", ".join(REASON_TEXT.get(r, r) for r in reasons) or "unfinished work"
    return (f"rescue: unfinished work from job {job_id}\n\n"
            f"Auto-committed by the job-completion guarantee after resume was exhausted or "
            f"unavailable. Detected: {words}.")


def rescue(rec: dict, detect_result: dict | None, *, runner=None, now: datetime | None = None) -> dict:
    """Layer 3. Stages and commits whatever is dirty, then pushes `HEAD` to `origin` under a new,
    job-specific ref name (`rescue/<job-id>`) — never the branch's own name, never `--force`, never
    followed by a PR. Refuses outright (and says why) rather than committing when the job's cwd sits
    on a protected branch in a SHARED checkout — see the module docstring.

    Never raises: this runs from inside `jobs.reconcile`'s per-record try, and a raise there costs the
    job its notification entirely (`jobs.notify_text`'s own docstring makes the identical argument
    about `worktree_note`/`contradiction_note`)."""
    runner = runner or job_worktree.Runner()
    now = now or _utc_now()
    rec = rec or {}
    job_id = rec.get("id") or "unknown"
    branch_name = f"{RESCUE_BRANCH_PREFIX}{job_id}"
    out = {"branch": branch_name, "committed": False, "pushed": False,
           "at": _stamp(now), "skipped_reason": None}
    try:
        cwd = rec.get("cwd")
        if not is_repo(cwd, runner=runner):
            out["skipped_reason"] = "job cwd is not a readable git repository — nothing to rescue"
            return out
        current = current_branch(cwd, runner=runner)
        if current in PROTECTED_BRANCHES:
            out["skipped_reason"] = (
                f"job ran directly on {current!r} in a shared checkout — refusing to auto-commit "
                "onto the deploy/release branch; recover by hand")
            return out
        add_res = _git(runner, cwd, "add", "-A")
        if not _ok(add_res):
            out["skipped_reason"] = f"git add -A failed: {_result_message(add_res)}"
            return out
        if has_staged_changes(cwd, runner=runner):
            msg = _rescue_commit_message(job_id, detect_result)
            commit_res = _git(runner, cwd, "commit", "-q", "-m", msg)
            if not _ok(commit_res):
                out["skipped_reason"] = f"git commit failed: {_result_message(commit_res)}"
                return out
            out["committed"] = True
        already = remote_refs_containing_head(cwd, runner=runner)
        if already:
            # HEAD (after any commit above) is already on the remote under some ref — a rescue push
            # would only duplicate it under a second name.
            out["already_remote"] = already
            out["skipped_reason"] = (
                f"nothing to rescue — HEAD is already on {', '.join(already)}; a rescue push would "
                "only duplicate a commit origin has")
            return out
        remote = job_worktree.BASE_REMOTE
        push_res = _git(runner, cwd, "push", remote, f"HEAD:refs/heads/{branch_name}")
        if _ok(push_res):
            out["pushed"] = True
        else:
            out["skipped_reason"] = f"push failed: {_result_message(push_res)}"
        return out
    except Exception as e:  # noqa: BLE001 — a broken seam must not strand the job unreported
        out["skipped_reason"] = f"rescue raised: {e}"
        return out


# --------------------------------------------------------------------------- the decision

def evaluate(rec: dict, *, repo_root: str, runner=None, gh_runner=None, now: datetime | None = None) -> dict:
    """The single entry point `jobs.reconcile` calls for a job that just reached `done` or
    `ended-unknown`. Reads only — it never writes `rec` or touches disk; `jobs.reconcile` owns every
    write to a job record and is the only caller that may act on the returned `action`.

    Returns `{"action": "proceed", "completion": {...}}` when DETECT found nothing (the common case);
    `{"action": "resume", "argv": [...], "completion": {...}}` when a bounded resume is available;
    or `{"action": "rescue", "completion": {...}, "detected": {...}}` when resume is exhausted or was
    never possible and the caller must call `rescue()` itself.

    `completion["reasons"]` is `[]` on the proceed path — the record still gets a `checked_at` stamp
    even when clean, so "did DETECT actually run for this job" is answerable from the record alone."""
    now = now or _utc_now()
    prior = (rec or {}).get("completion") if isinstance((rec or {}).get("completion"), dict) else {}
    completion = {"checked_at": _stamp(now), "reasons": [],
                  "resume_attempts": list(prior.get("resume_attempts") or [])}
    if not is_scoped_cwd(rec, repo_root):
        completion["skipped"] = "shared-repo-root"
        return {"action": "proceed", "completion": completion}

    detected = detect_incomplete(rec, runner=runner, gh_runner=gh_runner)
    if detected is None:
        return {"action": "proceed", "completion": completion}

    completion["reasons"] = detected["reasons"]
    completion["branch"] = detected.get("branch")
    if can_resume(rec):
        prompt = build_resume_prompt(detected)
        argv = resume_argv(rec, prompt)
        if argv is not None:
            return {"action": "resume", "argv": argv, "prompt": prompt,
                    "completion": completion, "detected": detected}
    return {"action": "rescue", "completion": completion, "detected": detected}
