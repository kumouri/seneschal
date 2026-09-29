#!/usr/bin/env python3
"""Come back later for the worktrees a job's teardown could not remove.

**Why a periodic sweeper is the second half of the design, not a workaround for a bug.**
`jobs.py --worktree` cuts a private checkout per delegated job and tears it down when the job ends.
That teardown runs `git worktree remove` and **never `--force`** — `job_worktree.py`'s fourth
refusal — so a removal git will not complete leaves the directory on disk, records
`worktree_leaked: true`, and names it in the completion push. **That refusal is correct and this
module does not weaken it.** But nothing else ever comes back for the leavings, and the leak is
usually a Windows file handle still open at the instant teardown ran: seconds or minutes later the
identical removal succeeds. A sweeper that runs *later* is therefore the natural complement to a
teardown that correctly refuses to force.

A real pile holds every shape the decision tree below handles: directories on disk that are no longer
registered as worktrees, half-finished removals holding thousands of files and no `.git`, and — the
one that matters most — a registered worktree holding a commit that exists nowhere else.

**REMOVAL IS OPT-IN PER DIRECTORY. A directory is removed only if it is PROVABLY safe, and anything
this module cannot prove is safe is LEFT ALONE AND REPORTED. There is no "probably fine" branch.**
That sentence is the whole module; everything below is its mechanism. The asymmetry behind it is
that a leaked directory costs a few megabytes and lost work is not recoverable — `job_worktree.py`'s
line, inherited here because this is the same trade one step further out.

**The two proofs, and nothing else counts as one.**

1. **An empty husk.** No `.git`, not registered as a worktree, and `os.listdir` returns **nothing**.
   Such a directory demonstrably holds no file and no git state; there is nothing left to lose.
   **"Near-empty" is deliberately not a category** — a directory with no `.git` and a few hundred
   files in it is a removal that stopped halfway, and which half survived is exactly what is not
   provable from here. Such directories are refused, loudly, forever, until a human looks.
2. **A registered worktree that is clean AND fully pushed.** `git status --porcelain` empty (tracked
   *and* untracked), and `git rev-list --count HEAD --not --remotes` == 0, i.e. every commit reachable
   from its HEAD is also reachable from some remote-tracking ref. **The second check is the one that
   earns this module's existence**: plain `git worktree remove` enforces the first and *not* the
   second, so it would happily remove a clean tree whose single commit exists in no other ref.

**Everything else is refused, and an error is a refusal.** A git call that fails, times out, raises,
or answers in a shape this module does not recognise resolves to *keep*, never to *remove*: the cheap
error is a directory that survives a night too long.

**Ignored files are the one residual, and it is named rather than hidden.** `status --porcelain`
does not report `.gitignore`d paths, so a clean-and-pushed worktree is removed with its
`__pycache__`, its per-job `seneschal/state/` and any `.venv` inside it. That is **exactly** what plain
`git worktree remove` already does and exactly what `job_worktree.teardown` has always done; this
module does not widen it, and widening the check to `--ignored` would refuse every worktree that ever
ran Python, which is a sweeper that never sweeps.

**Protected paths are refused before any git call.** The daemon's checkout, a `<checkout>-dev`
sibling, the harness's `.claude/worktrees/`, and **the tree this process is itself running from** — a
sweeper that can delete its own working directory is one `--root` typo from disaster. Four
independent rules cover them (`protection_refusal`); the structural one is that anything not strictly
*inside* the configured root is out of scope, and the derived one is that a `.git` **directory** means
a clone, never a job worktree.

**A running job's worktree is never touched.** The record is cross-checked in `state/jobs/`, and the
condition to proceed is `jobs.prune`'s own — terminal **and** already notified — for `jobs.prune`'s
own reason: the GC may never be what makes a job go silent, and the completion push quotes the leaked
path, so reclaiming it before that push lands would have the push name a directory that is gone.

**Held is not refused, and that distinction is what keeps this quiet.** A running job and a
three-hour-old directory are the *expected* state on any given night; reporting them would ping the
owner nightly about nothing, and a worker that does that is worse than no worker. So the verdicts
split three ways — `remove`, `hold` (routine, logged, silent) and `refuse` (noteworthy) — and only
`remove` and `refuse` are worth speaking about. Dream folds the report into the digest only when one
of those two is non-empty.

**Dry run is the default.** `--apply` is required to delete anything: a swept checkout is not
regenerable from anything.

Scheduling: **Dream step 2**, beside every other GC in this repo — a new scheduled task for this
would be a second thing to notice being dead. No resident daemon task; leaked husks accumulate at a
handful a week and nothing degrades while one sits.

Design + rationale: `seneschal/docs/delegated-work-isolation-spec.md` (phase 3).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import job_worktree as jw

# The floor. Six hours is comfortably past the file-handle window the leak comes from, and far short
# of the 14-day `LEAK_RETENTION_DAYS` that leaves `job_worktree.sweep` unable to reclaim most of a
# real pile. It is a *floor*, not a
# retention: a directory is not removed BECAUSE it is old, it is removed because it is provably empty
# or provably clean, and the age only buys certainty that nothing is still writing into it.
DEFAULT_MIN_AGE_HOURS = 6.0

# Refused before any git call, by name, in addition to the structural rules. Derived from the
# checkout these modules were loaded from — never a hardcoded name, since an install can be cloned
# under any directory name: the checkout itself is the live daemon's tree (a dirty tracked file there
# blocks the updater's `pull --ff-only`, leaving the daemon deploy-blind), and a `<checkout>-dev`
# sibling is the conventional hand-work tree, holding in-flight work that exists nowhere else.
# Neither can be a job id, so this can never refuse a real worktree. (Run from inside a job worktree
# the derived names are that job's own — already covered by the self rule — and the containment and
# `.git`-directory rules still keep the daemon's checkout out of reach.)
_HOST_BASENAME = os.path.basename(os.path.normpath(jw.REPO_ROOT))
PROTECTED_BASENAMES = (_HOST_BASENAME, _HOST_BASENAME + "-dev")

# The harness's own worktrees. Different lifecycle, different owner, not ours to reclaim.
FOREIGN_WORKTREE_MARKER = os.path.join(".claude", "worktrees")

REMOVE = "remove"
HOLD = "hold"
REFUSE = "refuse"


def _norm(path: str) -> str:
    """One spelling for a path, so a comparison can't fail on `/` vs `\\` or on case. Every
    path-equality test in this module goes through it — git prints forward slashes on Windows even
    when `os.listdir` does not."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _is_within(child: str, parent: str) -> bool:
    """Is `child` strictly inside `parent`? `commonpath` rather than `startswith`, which would call
    `seneschal-worktrees-old` a child of `seneschal-worktrees`."""
    c, p = _norm(child), _norm(parent)
    if c == p:
        return False
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:      # different drives — not a containment question, just a no
        return False


def resolve_root(root: str | None = None) -> str:
    """Which directory to sweep: `--root`, else `$SENESCHAL_WORKTREE_ROOT`, else the derived default.

    **The last case needs one correction `job_worktree.worktree_root` cannot make**, and it is why
    this function exists. That derivation is `dirname(REPO_ROOT)/seneschal-worktrees`, which is right
    for its own callers — `jobs.py` runs from the daemon's checkout — and wrong for this one, because a
    sweeper is the tool most likely to be run from *inside* a job worktree. There `REPO_ROOT` is
    `…/seneschal-worktrees/<job-id>`, so the derivation yields
    `…/seneschal-worktrees/seneschal-worktrees`, which does not exist, and the sweep reports a clean
    night over a directory that was never the pile. **A silent no-op that looks exactly like
    success.**

    So: when this checkout's own parent IS the worktree root, that parent is the answer."""
    if root:
        return os.path.abspath(root)
    if (os.environ.get(jw.WORKTREE_ROOT_ENV) or "").strip():
        return jw.worktree_root()
    parent = os.path.dirname(jw.REPO_ROOT)
    if os.path.basename(parent).lower() == jw.WORKTREE_DIRNAME.lower():
        return parent
    return jw.worktree_root()


def self_paths() -> list:
    """The trees this process must never delete: the checkout these modules were loaded from, and the
    current working directory. Derived, never configured — `job_worktree.REPO_ROOT` is computed from
    the file's own location, so when the sweeper runs from inside a job worktree this is that
    worktree, and the self-delete is refused by construction."""
    out = [jw.REPO_ROOT]
    try:
        out.append(os.getcwd())
    except OSError:         # a cwd that no longer exists is not a path we need to protect
        pass
    return out


def protection_refusal(path: str, root: str, mine: list | None = None) -> str:
    """Why this path may not be touched at all, or `""` if it is in scope.

    Four independent rules, deliberately overlapping. Any one of them alone would cover the daemon's
    checkout today; all four are here because they fail differently — the containment rule survives a
    renamed directory, the `.git`-kind rule survives a `--root` pointed one level too high, the name
    rule survives both being wrong, and the self rule survives the sweeper being run from anywhere."""
    mine = self_paths() if mine is None else mine

    if not _is_within(path, root):
        return f"outside the configured worktree root ({root})"

    for own in mine:
        if _norm(path) == _norm(own) or _is_within(own, path):
            return "this is the checkout the sweeper is running from"

    parts = [p.lower() for p in _norm(path).split(os.sep)]
    marker = [p.lower() for p in FOREIGN_WORKTREE_MARKER.split(os.sep)]
    for i in range(len(parts) - len(marker) + 1):
        if parts[i:i + len(marker)] == marker:
            return "under .claude/worktrees — the harness's, not ours"

    if os.path.basename(_norm(path)) in PROTECTED_BASENAMES:
        return "a protected checkout (the daemon's tree or its -dev sibling)"

    # A worktree's `.git` is a FILE holding `gitdir:`; a clone's is a directory. A directory here
    # means a whole repository with its own object store, which no teardown of ours ever created.
    if os.path.isdir(os.path.join(path, ".git")):
        return "holds a .git directory — a clone, not a job worktree"

    return ""


def registered_worktrees(runner, host: str) -> tuple:
    """`(set_of_normalised_paths, error)` from one `git worktree list --porcelain` in the host repo.

    Returns the error rather than raising, and the caller turns it into a refusal of the whole run:
    without this list the module cannot tell an unregistered husk from a registered worktree, and
    guessing that difference is exactly the judgement it exists to avoid making."""
    try:
        res = jw._git(runner, host, "worktree", "list", "--porcelain")
    except Exception as e:  # noqa: BLE001 — a broken seam is an error, never a green light
        return (set(), jw.one_line(f"could not run git: {e}"))
    if getattr(res, "returncode", 1) != 0:
        return (set(), jw._git_message(res))
    found = set()
    for line in (getattr(res, "stdout", "") or "").splitlines():
        if line.startswith("worktree "):
            found.add(_norm(line[len("worktree "):].strip()))
    return (found, "")


def _git_in(runner, path: str, *args):
    """A git call **inside** the worktree being judged. `-C` rather than `cwd=`, so a directory that
    vanished mid-sweep fails as a git error we can report rather than an OSError from the spawn."""
    return runner.run(["git", *jw.GIT_FLAGS, "-C", path, *args])


def worktree_is_clean(runner, path: str) -> tuple:
    """`(clean, detail)` — tracked *and* untracked, exactly `git worktree remove`'s own test.

    Any failure answers `(False, why)`. `--porcelain` is stable across git versions in a way the
    human `status` output is not, and it is empty on a clean tree, so "clean" is the absence of
    output rather than the presence of a phrase to match."""
    try:
        res = _git_in(runner, path, "status", "--porcelain")
    except Exception as e:  # noqa: BLE001
        return (False, jw.one_line(f"could not run git status: {e}"))
    if getattr(res, "returncode", 1) != 0:
        return (False, f"git status failed: {jw._git_message(res)}")
    lines = [ln for ln in (getattr(res, "stdout", "") or "").splitlines() if ln.strip()]
    if lines:
        return (False, f"{len(lines)} uncommitted change(s), e.g. {jw.one_line(lines[0], 60)}")
    return (True, "")


def unpushed_commits(runner, path: str) -> tuple:
    """`(count, detail)` — commits reachable from HEAD and from **no** remote-tracking ref.

    **This is the check `git worktree remove` does not do**, and the one that would actually lose
    something. `--not --remotes` is what makes it honest about a checkout with more than one remote
    (a fork, a vendored subtree): "pushed" cannot mean "on origin/develop" — a commit pushed to any
    remote is safe, and only a commit on none is not.

    A failure answers a **non-zero count with the reason**, so an unreadable HEAD refuses exactly as
    an unpushed commit does. A count that will not parse is the same: no evidence is not evidence of
    safety."""
    try:
        res = _git_in(runner, path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    except Exception as e:  # noqa: BLE001
        return (-1, jw.one_line(f"could not run git rev-list: {e}"))
    if getattr(res, "returncode", 1) != 0:
        return (-1, f"git rev-list failed: {jw._git_message(res)}")
    raw = (getattr(res, "stdout", "") or "").strip()
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return (-1, f"could not read a commit count from git ({jw.one_line(raw, 40)!r})")
    if n > 0:
        return (n, f"{n} commit(s) exist here and nowhere else")
    return (0, "")


def _age_hours(path: str, rec: dict | None, now: float) -> float:
    """The age of the YOUNGEST evidence about this directory, in hours.

    Youngest, not oldest, and not the mean: the floor exists to be sure nothing is still writing
    here, so a directory whose record was notified eleven days ago but whose mtime moved ten minutes
    ago is ten minutes old. An unreadable signal contributes `0.0` — unknown age is young."""
    ages = []
    try:
        ages.append((now - os.stat(path).st_mtime) / 3600.0)
    except OSError:
        ages.append(0.0)
    stamp = (rec or {}).get("notified_at")
    if stamp:
        try:
            import jobs
            ages.append((jw._utc_now() - jobs.parse_iso(stamp)).total_seconds() / 3600.0)
        except Exception:   # noqa: BLE001 — an unparseable stamp is no evidence of age
            ages.append(0.0)
    return max(0.0, min(ages)) if ages else 0.0


def _entry_count(path: str) -> int:
    """How many entries the directory holds. `-1` when it cannot be read — which is not zero, and the
    difference is the whole point: an unreadable directory must never satisfy the empty-husk proof."""
    try:
        return len(os.listdir(path))
    except OSError:
        return -1


def classify(path: str, *, root: str, registered: set, rec: dict | None, runner,
             min_age_hours: float, now: float, mine: list | None = None,
             terminal=None) -> dict:
    """One directory's verdict: `{name, path, verdict, reason}`.

    Pure decision, no removal — which is what lets every rule below be pinned by a test that never deletes anything and never spawns git.

    **Order is load-bearing.** Protection first, so a protected path is never so much as `stat`ed for
    an age. Then the job record, so "the sweeper raced a live job" is impossible before any proof is
    attempted. Then age. Only then the two proofs, which are the only paths to `remove`."""
    name = os.path.basename(os.path.normpath(path))
    out = {"name": name, "path": path, "verdict": REFUSE, "reason": "", "how": ""}

    guard = protection_refusal(path, root, mine)
    if guard:
        return {**out, "verdict": REFUSE, "reason": guard}

    is_registered = _norm(path) in registered

    # `jobs.TERMINAL` is passed in rather than imported here so a test can pin the running-job rule
    # without a jobs module on the path; the CLI always passes the real one. An empty tuple means
    # "no state counts as terminal", so a record present with no known terminal set HOLDS — the safe
    # direction, and the one a mis-wired caller lands in.
    terminal_states = terminal or ()
    if rec is not None:
        status = rec.get("status")
        if status not in terminal_states:
            return {**out, "verdict": HOLD,
                    "reason": f"job is still {status or 'unrecorded'} — never race a live job"}
        if not rec.get("notified_at"):
            return {**out, "verdict": HOLD,
                    "reason": "job is terminal but its completion push has not landed"}

    age = _age_hours(path, rec, now)
    if age < min_age_hours:
        return {**out, "verdict": HOLD,
                "reason": f"only {age:.1f} h old (floor {min_age_hours:g} h)"}

    entries = _entry_count(path)

    if not is_registered:
        # Proof 1 — the empty husk. Note the three conjuncts: unregistered, no `.git` of any kind,
        # and literally zero entries. `entries == -1` (unreadable) fails this and lands in the
        # refusal below, which is the direction that costs nothing.
        if entries == 0 and not os.path.exists(os.path.join(path, ".git")):
            return {**out, "verdict": REMOVE, "how": "rmdir",
                    "reason": f"empty husk — no .git, not registered, {age / 24:.1f} days old"}
        if entries < 0:
            return {**out, "verdict": REFUSE, "reason": "directory could not be read"}
        if os.path.exists(os.path.join(path, ".git")):
            return {**out, "verdict": REFUSE,
                    "reason": f"has a .git but git does not list it as a worktree "
                              f"({entries} top-level entries)"}
        return {**out, "verdict": REFUSE,
                "reason": f"not registered as a worktree, no .git, but {entries} top-level entr"
                          f"{'y' if entries == 1 else 'ies'} remain — a removal that stopped "
                          f"halfway; cannot prove it holds nothing"}

    if rec is None:
        # A REGISTERED worktree with no job record is an **orphan**, and "reclaim, don't guess"
        # has no reading under which a missing record proves a job is over. It is also defence in
        # depth for the hazard below: with an unreadable ledger a *running* job's tree looks exactly
        # like this, and a running job's tree is usually clean and fully pushed — i.e. it would
        # otherwise satisfy proof 2 and be deleted out from under a live agent. A state dir passed one
        # level too deep makes every record read as None, leaving only the age floor between a
        # running job and its checkout.
        #
        # An unregistered EMPTY husk with no record is still removed, deliberately — that is the one
        # place this goes further than `job_worktree.sweep`, which counts every orphan and reclaims
        # none. The difference is that proof 1 does not rest on the record at all: a directory with no
        # git state and no files holds nothing, whoever it once belonged to.
        return {**out, "verdict": REFUSE,
                "reason": "registered worktree with no job record — nothing says that job is over"}

    # Proof 2 — registered, clean, fully pushed. Both halves must answer, and either one failing to
    # answer is a refusal carrying git's own words.
    clean, why = worktree_is_clean(runner, path)
    if not clean:
        return {**out, "verdict": REFUSE, "reason": why}
    ahead, why = unpushed_commits(runner, path)
    if ahead != 0:
        return {**out, "verdict": REFUSE, "reason": why}
    return {**out, "verdict": REMOVE, "how": "git",
            "reason": f"registered, clean, every commit pushed ({age / 24:.1f} days old)"}


def remove_one(verdict: dict, *, runner, host: str) -> tuple:
    """Carry out one proved-safe removal. `(ok, why_not)`. **Two mechanisms, one per proof.**

    * **`how == "rmdir"` — the empty husk.** `os.rmdir`, never `shutil.rmtree`. A husk is not a
      registered worktree, so `git worktree remove` would simply fail on it and the directory would
      sit there forever being re-refused every night. But the deeper reason is that **`os.rmdir`
      refuses a non-empty directory**: the operating system re-proves emptiness atomically at the
      instant of deletion, which closes the window between `classify`'s `listdir` and this call. The
      proof and the act check the same fact, and `rmtree` would be the version that doesn't.
    * **`how == "git"` — the registered worktree.** `job_worktree.teardown`, which is **one**
      implementation of "`git worktree remove`, never `--force`, leak loudly", shared with the
      per-job path. A second copy here is the class of bug this repo keeps re-learning, and it would
      be the copy that could forget the rule.

    Never raises: a removal that fails is reported as a refusal, exactly like one that was never
    attempted."""
    path = verdict.get("path") or ""
    if verdict.get("how") == "rmdir":
        try:
            os.rmdir(path)
        except OSError as e:
            return (False, jw.one_line(f"{e}"))
        return (True, "")
    res = jw.teardown({"worktree": {"path": path, "host": host}}, runner=runner, host=host) or {}
    if res.get("removed"):
        return (True, "")
    return (False, jw.one_line(res.get("reason") or "git refused the removal"))


def sweep(root: str | None = None, *, state_dir: str | None = None, apply: bool = False,
          min_age_hours: float = DEFAULT_MIN_AGE_HOURS, host: str | None = None,
          runner=None, now: float | None = None, out=None) -> dict:
    """Judge every directory under the worktree root, then remove only the provable ones.

    Returns `{root, removed, refused, held, error}` — lists of verdict dicts, so a caller reports a
    running count without re-deriving it (`job_worktree.sweep`'s shape, and `jobs.py list`'s reason
    for showing leaks at all: a pile-up has to be visible rather than merely present).

    A missing root is the normal state on a machine that has never run a `--worktree` job, and is not
    an error. A root that exists and cannot be listed **is**."""
    runner = runner or jw.Runner()
    host = os.path.abspath(host or jw.REPO_ROOT)
    now = time.time() if now is None else now
    # Resolved HERE, not as a default argument: `out=sys.stdout` in the signature binds at import
    # time, so a caller that redirects `sys.stdout` afterwards captures nothing. Caught by
    # `CliTests.test_default_is_a_dry_run`, which is exactly the shape a Dream step uses.
    out = sys.stdout if out is None else out
    base = resolve_root(root)
    result = {"root": base, "removed": [], "refused": [], "held": [], "error": ""}

    if not os.path.isdir(base):
        print(f"worktree-gc: nothing to sweep — {base} does not exist", file=out)
        return result
    try:
        names = sorted(os.listdir(base))
    except OSError as e:
        result["error"] = f"cannot list {base}: {e}"
        print(f"worktree-gc: {result['error']}", file=out)
        return result

    listed, err = registered_worktrees(runner, host)
    if err:
        # The registration list is the input that tells a husk from a worktree. Without it every
        # directory would be judged by the weaker of the two proofs, so the run refuses as a whole
        # rather than proceeding on a guess.
        result["error"] = f"could not read the worktree list: {err}"
        print(f"worktree-gc: REFUSING THE WHOLE RUN — {result['error']}", file=out)
        return result

    try:
        import jobs
        terminal = jobs.TERMINAL
        state = state_dir or jobs.DEFAULT_STATE_DIR
    except Exception as e:  # noqa: BLE001
        result["error"] = f"could not load the job ledger: {e}"
        print(f"worktree-gc: REFUSING THE WHOLE RUN — {result['error']}", file=out)
        return result

    # **A ledger that isn't there is not a ledger saying "no jobs are running".** Without it every
    # live job's worktree reads as an orphan, and the orphan rule above is the only thing left
    # standing between a running agent and its checkout. One rule should never be load-bearing alone,
    # so the absent ledger refuses the run outright — the same fail-closed direction, one level up.
    # A state dir passed one level too deep is the way this actually happens, and it is silent: `load_job` returns None for a missing file exactly as it does for a job that never was.
    if not os.path.isdir(jobs.jobs_dir(state)):
        result["error"] = (f"no job ledger at {jobs.jobs_dir(state)} — cannot tell a finished job "
                           f"from a running one (pass --state-dir <…/seneschal/state>)")
        print(f"worktree-gc: REFUSING THE WHOLE RUN — {result['error']}", file=out)
        return result

    mine = self_paths()
    for name in names:
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        try:
            rec = jobs.load_job(state, name)
        except Exception:   # noqa: BLE001 — an unreadable record is not permission to remove
            result["refused"].append({"name": name, "path": path, "verdict": REFUSE,
                                      "reason": "its job record could not be read"})
            continue
        v = classify(path, root=base, registered=listed, rec=rec, runner=runner,
                     min_age_hours=min_age_hours, now=now, mine=mine, terminal=terminal)

        if v["verdict"] == HOLD:
            result["held"].append(v)
            print(f"worktree-gc: held {name} — {v['reason']}", file=out)
            continue
        if v["verdict"] == REFUSE:
            result["refused"].append(v)
            print(f"worktree-gc: REFUSED {name} — {v['reason']}", file=out)
            continue

        if not apply:
            result["removed"].append(v)
            print(f"worktree-gc: would remove {name} — {v['reason']}", file=out)
            continue

        ok, why = remove_one(v, runner=runner, host=host)
        if ok:
            result["removed"].append(v)
            print(f"worktree-gc: removed {name} — {v['reason']}", file=out)
        else:
            failed = {**v, "verdict": REFUSE, "reason": f"removal refused: {why}"}
            result["refused"].append(failed)
            print(f"worktree-gc: REFUSED {name} — {failed['reason']}", file=out)

    if apply and result["removed"]:
        # Last, once: `prune` tidies admin records whose directory is already gone. It reclaims
        # nothing by itself — running it first would tidy away the very registrations the
        # judgements above are read from.
        jw._git(runner, host, "worktree", "prune")

    verb = "would be removed" if not apply else "removed"
    if result["removed"] or result["refused"]:
        print(f"worktree-gc: {len(result['removed'])} {verb}, {len(result['refused'])} refused, "
              f"{len(result['held'])} held under {base}", file=out)
    else:
        # THE QUIET PATH. Nothing was reclaimed and nothing needs a human — one line for the log and
        # not a word to the owner. A nightly worker that pings about routine holds is worse than no
        # worker, so `held` deliberately does not make this line speak.
        print(f"worktree-gc: nothing to report ({len(result['held'])} held) under {base}", file=out)
    return result


def has_news(result: dict) -> bool:
    """Is there anything worth telling the owner? Removed something, refused something, or failed. The
    one predicate the Dream step reads, so "speak only when there is something to say" is a function
    rather than a judgement made afresh each night."""
    return bool(result.get("removed") or result.get("refused") or result.get("error"))


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Sweep the worktrees a delegated job's teardown could not remove.",
        epilog="Dry run by DEFAULT — pass --apply to remove anything. `--force` appears nowhere in "
               "this script and a test asserts it.")
    ap.add_argument("--root", default=None,
                    help="worktree root to sweep (default: the standard "
                         f"<repos>/{jw.WORKTREE_DIRNAME}, or ${jw.WORKTREE_ROOT_ENV})")
    ap.add_argument("--state-dir", default=None,
                    help="job ledger to cross-check against (default: jobs.DEFAULT_STATE_DIR)")
    ap.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS,
                    help=f"leave anything younger than this alone (default {DEFAULT_MIN_AGE_HOURS:g})")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, change nothing — THE DEFAULT; accepted so it can be said out loud")
    ap.add_argument("--apply", action="store_true",
                    help="actually remove the directories proved safe (without this, nothing is deleted)")
    ap.add_argument("--json", action="store_true", help="also print the result as JSON")
    args = ap.parse_args(argv)

    if args.min_age_hours < 0:
        print("worktree-gc: --min-age-hours must not be negative", file=sys.stderr)
        return 2
    if args.apply and args.dry_run:
        # Refused rather than ranked. Which one wins is a guess about whether the caller meant to
        # delete, and that is not a guess this module makes.
        print("worktree-gc: --apply and --dry-run contradict each other", file=sys.stderr)
        return 2

    result = sweep(args.root, state_dir=args.state_dir, apply=args.apply,
                   min_age_hours=args.min_age_hours)
    if args.json:
        print(json.dumps(result, indent=2))
    # "Found nothing" is exit 0 — a GC that exits non-zero on a quiet night trains everyone to ignore
    # its exit code. Only a real error is non-zero.
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
