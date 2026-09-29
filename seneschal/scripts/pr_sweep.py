#!/usr/bin/env python3
"""**Find the green PRs nobody is watching, and hand them to the merge guard.** Stdlib + the `gh` CLI.

## The gap this closes

A sentence in chat is not sufficient authorization for a merge, so every functionality merge waits
on a merge approval picker — and **the picker has to arrive without anyone remembering to ask for
it**, or every merge stalls waiting for an agent to think of it.

`merge_guard.ask_on_green` is that rule in code. But its only other caller is `watch_pr.py`, and
`watch_pr.py` only runs when somebody starts it — so without this module what actually ships is
*"auto-send when a watcher happens to be running."* A PR that goes green with nothing watching
produces nothing at all, and a picker that did go out can sit unanswered for hours because nothing
is following up on it. Someone still has to remember, which is the thing the rule exists to remove.

This module is the **finder**. The daemon's PR-watch supervised task runs :func:`sweep` on a
cadence, so the asker is resident rather than hand-started.

**The same pass takes the dead pickers down.** A picker whose PR has merged or closed is never
withdrawn by anything else, so it sits on the owner's phone indistinguishable from a live question
until they tap it — and a tap on it buys nothing. The detector costs nothing extra: the `gh pr list`
this file already runs **is** the evidence, so a pending merge picker absent from a list that was
read whole is a candidate. `picker_retire.py` owns what happens next; this file owns only the index,
because the list is already here. Its own docstring carries the ways it declines to act.

**A picker also dies without its PR going anywhere.** An approval is bound to one exact commit, so
the moment a head moves — a rebase, a fixup, a force-push — the question pinned to the old commit
buys nothing. That second class needs a second field and no second call: :data:`LIST_FIELDS` carries
`headRefOid` for the pre-filter anyway, so :func:`open_heads` reads the head off the rows this pass
already fetched and hands it over beside the numbers. The re-ask is free for the same reason — the
guard's ask log is keyed `(repo, pr, head_sha)`, so a moved head is already a question nobody has
asked.

**It does not ask about a pull request GitHub says cannot merge.** An approval is bound to one exact
commit and a conflicted commit cannot be merged, so a tap on a `CONFLICTING` head is a decision that
buys nothing — even when CI is fully green at that SHA. Green was never the whole question; whether
the pull request can merge **at all** is the other half. That is one more field on a call already
being made: `mergeable` rides the same `gh pr list` as `headRefOid`, and :func:`unmergeable_reason`
reads it off rows this pass already fetched.

**The same pass puts each picker's STATE on the picker.** A warning that a PR cannot merge, arriving
in prose after the tap, arrives in the wrong place: the owner is looking at the picker when they
decide. A reaction is in the same place they are looking. `picker_mark.sweep` runs between the
asking and the retiring, rides this pass's own list a third time (:func:`open_rows`), and is the
fast half of a pair: it costs no `gh` call and has no cap, where `picker_retire` costs a
`gh pr view` per pull request and is capped. Its rules, and why 😴 is derived rather than latched:
its docstring + `../docs/picker-state-marking-spec.md`.

**What the ask-time check supports is narrower than the problem, and the difference is stated
rather than blurred.** A PR that was CLEAN when its picker went out and went conflicted afterwards
(another merge dirtied it) cannot be caught by any ask-time check. This suppression closes the
**asked-while-conflicted** class and only that class. **The went-conflicted-after-the-ask class is
`picker_mark`'s, and it is a MARKING rather than a retirement** — a picker whose PR turned
`CONFLICTING` under it gets 😴 and stays where it is. Retiring it instead was considered and refused:
retirement is irreversible and `mergeable` is a field GitHub serves stale, so a retirement on it
could withdraw a live decision, which is the one thing `picker_retire`'s fail-open rules exist to
prevent. A second opinion about which questions may be *withdrawn* is still the thing this file
refuses to grow; a reaction withdraws nothing.

**The tidyings reach every repository with a pending picker — the asking does not.** A picker for a
repository outside the watched set (sent by another session, or by a hand-run `watch_pr.py`) would
otherwise stand after its PR merged, because the pass only read the watched set, and the owner would
have to tap *Not now* just to make it stop looking undecided. So :func:`sweep` also lists each
repository named by a pending picker (:func:`picker_retire.pending_repos`, the CLI's own
enumeration, capped at :data:`picker_retire.MAX_EXTRA_REPOS_PER_PASS` per pass with the remainder
named) and hands those rows to `picker_mark` and `picker_retire` **only** — they never enter
:func:`candidates` and never reach the red arm. What widens is which stale pickers come down, not
which repositories get asked about (`../docs/picker-state-marking-spec.md` §10.5 item 1).

**It never asks about a PR that is behind its base.** Where the base branch requires branches to be
up to date before merging (`required_status_checks.strict`), a branch merely out of date with its
base — no textual conflict, `mergeable: MERGEABLE` — is something GitHub itself refuses to merge.
Left unhandled that reads exactly like a clean, green PR and keeps getting asked about, spending a
tap GitHub was always going to refuse. `mergeStateStatus` joins the same `gh pr list` call
`mergeable` already rides, and :func:`behind_reason` is `unmergeable_reason`'s allow-list shape one
field over — kept as a **separate** dict and a separate `skipped` flag (`behind`, not `conflicted`)
because the two have different fixes: a conflict needs a hand resolution, BEHIND needs a plain
merge-up, and folding them into one reason would hide which repair applies.

## Which repositories it watches — configuration, never code

The watched set is :func:`default_repos`, which is `repo_config.watched_repos()`: the owner's
`watched_repos` list in `seneschal/references/pr-guard.json`, else this checkout's own `origin`
repository and nothing else. It is resolved **lazily, on every call** — never frozen at import — so
a config edit needs no daemon restart and a test can inject its own set. `pr_sweep.DEFAULT_REPOS`
still exists for callers that read it as a name (the daemon's boot line, the diagnostic CLIs): it is
a module attribute resolved through :func:`default_repos` on access (PEP 562 `__getattr__`), so it
always answers the current config.

**Discovering repositories instead was considered and is worse, not merely harder.** An account's
full repository list includes vendored mirrors, forks, and archived experiments; watching them all
means buzzing the owner about pull requests in repositories they have no intention of merging from
here, and the set would change under them without any diff to review. A configured list is
reviewable, greppable, and bounded by something a human wrote down. A mirror the daemon pushes to
(a merge there is not a deploy and not a decision the owner makes on Telegram) simply stays out of
the list.

## It decides what is worth ASKING ABOUT, and hands over everything else

**Is CI terminally green on this PR** — and that verdict is `watch_pr.classify`, imported rather than
re-derived, because the rule that an empty rollup is *pending* and never *green* is exactly what
stops a watcher reporting ✅ in one second on a PR whose CI had not started. **And whether GitHub
positively says it cannot merge** — which is not a classifier at all, but GitHub's own verdict
copied verbatim with no vocabulary of this file's laid over it.

**Neither is a second opinion, and that is the test to apply to anything else that wants to live
here.** The guard decides *refusal* — may this merge, may this even be asked about — from the pull
request itself. This file decides *candidacy* — is a question worth sending right now — from
transient facts on a list it already reads. The guard's own CI refusal (`merge_guard.ci_refusal`)
reads the rollup through the same `watch_pr.classify`, so the two cannot drift: a PR this file
never asks about because CI is red is a PR the guard would refuse to merge anyway. `mergeable` the
guard does not read at all.

Every other question — is this PR docs-only, is it still open, is there already a live approval, was a
picker already sent for this commit, is it the middle of the night — belongs to
:func:`merge_guard.ask_on_green` and is asked there. **A second classifier or a second dedupe here
is the recurring bug** (`watch_pr.ask_on_green`'s docstring makes the same refusal for the same
reason): a second opinion about what "changes functionality" means is a second opinion that can
drift from the one doing the blocking.

## What it CANNOT do

**It finds and it asks. It does not merge, does not approve, and does not record an approval.**
`merge_guard.record_approval`'s single-caller property — the daemon's Telegram callback path and
nothing else — is frozen, and a test asserts this file does not so much as name it. Nothing here
widens what may merge; it widens only *what gets asked about*, which was already act-low.

## The burst, which is the part that would bite

A resident sweep sees **every open PR at once** on its first pass, after a daemon restart, and after
an outage. Four bounds, in the order they apply:

1. **Quiet hours suppress the ASKING** — :func:`merge_guard.in_quiet_hours`, the guard's own
   predicate over `sentinel`'s overnight window in the owner's local time, not a second copy. Zero
   pickers inside the window, and **nothing recorded for one**, so a PR that goes green at 03:00 is
   deferred rather than dropped and the first pass after the window lifts asks about it. The guard
   re-checks the window anyway on its own path, so this is the cost half of a rule enforced
   elsewhere. **Both readings are of the SAME instant**, pinned once per pass in :func:`sweep`; two
   clocks would let the two disagree.

   **The window suppresses the asking, not the pass.** An early return here would be free while
   asking was the only thing behind it, and false for a marker, which would be withheld for a
   quarter of every day — including the hours an owner who is up late actually taps in. So the pass
   still looks, and the two silent tidyings — marking and retirement — run; neither raises a
   notification, so neither can be the reason anyone wakes. The cost is one `gh pr list` per repo
   per pass overnight.

   **The window stands down while the owner is demonstrably awake.** `merge_guard.awake_evidence`
   reads the tail of `state/turns.jsonl` for an owner row within the awake window — a message, a
   picker tap, a reaction, an attachment; never a `[job finished:` notice — and when it finds one
   the pass asks (and the red arm notifies) as if it were daytime, with `awake_override` in the
   report naming the instant. It fails toward QUIET: no file, an unreadable one, or no such row, and
   the window holds exactly as before.
2. **The ask log makes a restart a no-op.** `already_asked` is keyed on `(repo, pr, head_sha)` and
   lives on disk, so the second boot of the day re-sees the same PRs and asks about **none** of them.
   This is reuse doing the work: the restart case needed no new mechanism at all.
3. **At most :data:`MAX_ASKS_PER_PASS` pickers actually go out per pass** — one. Five PRs going green
   together become five pickers spread across five passes, not five buzzes in one second. It is the
   stagger-don't-batch reminder rule applied to a different queue: a merge approval is a decision
   made one at a time, and five arriving at once is the prose list the picker exists to replace,
   wearing a keyboard.
4. **A capped pass says what it left behind.** `deferred` names every candidate the cap held over and
   the task logs it, because a silent truncation reads as "covered everything" when it didn't.

**The first pass deliberately does NOT seed.** A feed watcher seeds its first run so a new machine
can never bulk-pull a back catalogue, and that is right *there* — a missed post costs nothing. Here
it would be a lie in the ledger: seeding writes `already_asked` rows for pickers that never went
out, so the PR would never get its question and the failure would be invisible. That is precisely the
blocked-and-couldn't-ask → never-asked collapse `ask_on_green` refuses on its failed-send path. The
per-pass cap converts a burst into a **queue**; seeding would convert it into a **drop**.

## The open index, and why an unreadable page answers None rather than empty

:func:`open_numbers` (rows, truncated) folds a repo's rows into a set of open PR numbers, or **`None`
when the answer cannot be trusted COMPLETE** (unreadable, or at the page limit). That `None` is the
point: `picker_retire.py` reasons from ABSENCE, and absence read off a truncated list would retire a
live picker on a paging artifact rather than a real merge/close. Retirement runs LAST, after the
asking, and rides the `retirer=` seam.

## Green-only was a gap too: a RED pull request needs telling about

`candidates` is green-only by construction (`is_green` requires terminal-and-passing), so a red PR
never becomes a candidate — no picker, correctly, because a picker asks for a merge and a red PR must
not merge; but without a second arm also **no notification of any kind**, because nothing else in
this resident pass is looking. Only a hand-started `watch_pr.py` computes a red verdict at all, and
its red summary reaches the owner only if somebody started it as a job.

:func:`is_red` (`is_green`'s mirror, same `watch_pr.classify` import, same pending-is-neither rule)
and `pr_red_notify.py` (the ledger, the message text, the send) close it. **It is a NOTIFICATION,
never a picker** — a red PR needs fixing, not approving, so this arm has no option list, mints no
approval, and cannot be routed through `merge_guard` by construction (there is no path through which
it could). It rides this pass's own rows, so the red arm costs no second `gh` call; it is bound to
`(repo, pr, head_sha)` exactly as the ask ledger is, so a new head (a fixup, a rebase, a force-push)
is a new question and notifies again; it defers rather than sends inside the same quiet window
:func:`merge_guard.in_quiet_hours` already pins for the ask arm, and is capped at
:data:`MAX_RED_NOTIFIES_PER_PASS` per pass with the remainder named in `red_deferred`, not dropped —
the same staggering shape `sent`/`max_asks` already use. A failed, timed-out or ambiguous send
records nothing, so the next pass retries rather than silently losing the notice.

## Fail-open, quietly, on every path

`gh` missing, unauthenticated, rate-limited, offline, a malformed response, one repo broken while the
other is fine, a Telegram send that fails for the red arm: each costs **this pass** (or, for the red
arm, this one notification) and nothing else. :func:`sweep` never raises, per-repo errors are
isolated from each other, and the caller is one of the daemon's supervised tasks — it must not be
able to take the others down. The daemon's task is also off under `--stub-send`, unlike its
siblings: they never send, while a sweep's whole job is to reach the owner's phone and
`telegram_ask.py` has never heard of the flag.

USAGE (diagnostics; the daemon is the production caller):
  python pr_sweep.py --dry-run            # what WOULD be asked/notified about, sending nothing
  python pr_sweep.py --repo owner/name    # override the watched set for one run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling imports work when imported, not only when run

import job_pr_draft  # noqa: E402 — a PR whose branch is a running job's worktree is not a candidate
import merge_guard as mg  # noqa: E402 — the asker, the classifier, the ledgers and the quiet window
import picker_mark  # noqa: E402 — puts each picker's STATE on it as a reaction; same open list again
import picker_retire  # noqa: E402 — takes the DEAD pickers down; rides this pass's own open list
import pr_red_notify  # noqa: E402 — tells the owner when CI goes RED; a notice, never a picker
import repo_config  # noqa: E402 — the watched set is the owner's config, never a constant here
import watch_pr  # noqa: E402 — `classify`: the ONE rollup->verdict rule, imported rather than copied


def default_repos() -> tuple:
    """**The watched set: the repositories this pass may ASK about.** `repo_config.watched_repos()`
    — the owner's configured list, else this checkout's own `origin` repository — resolved at call
    time, never cached, so a config edit needs no restart and a test can inject its own set.

    `()` when neither the config nor git names a repository: a pass over nothing asks about nothing,
    which is the honest answer for a checkout with no `origin`. Never raises (`repo_config`'s
    readers never do)."""
    return tuple(repo_config.watched_repos())


def __getattr__(name):
    """**`DEFAULT_REPOS` stays a readable name, resolved on access.** Callers that read
    `pr_sweep.DEFAULT_REPOS` (the daemon's boot line, the diagnostic CLIs) get :func:`default_repos`'
    current answer rather than a tuple frozen at import. Inside this module, call
    :func:`default_repos` — a bare name never reaches a module `__getattr__`."""
    if name == "DEFAULT_REPOS":
        return default_repos()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: How many pull requests one `gh pr list` call may return. Generous — the sweep must not silently
#: stop seeing PRs past some boundary — but finite, so a repo in a strange state cannot make one pass
#: unbounded. :func:`sweep` reports when a repo hits it.
PR_LIST_LIMIT = 50

#: **The burst bound: pickers actually SENT per pass.** One. See the module docstring's bound 3 — this
#: is a staggering rule, not a throttle: nothing is dropped, the remainder is named in `deferred` and
#: asked about on the following pass. Raising it to N makes N pickers arrive in the same second,
#: which is the thing being prevented; lowering it to 0 disables asking without disabling the sweep.
MAX_ASKS_PER_PASS = 1

#: **The red-notify burst bound, `MAX_ASKS_PER_PASS`'s shape, one arm over.** A resident sweep sees
#: every open PR at once on its first pass, after a restart, and after an outage, and a red PR is no
#: different from a green one in that respect — a queue of failures going red together must not
#: become a queue of buzzes in one second either. Nothing is dropped: the remainder is named in
#: `red_deferred` and notified about on the following pass. See `pr_red_notify.py`'s module docstring
#: for the gap this arm closes.
MAX_RED_NOTIFIES_PER_PASS = 1

#: What `gh pr list` is asked for. `headRefOid` + `url` let the cheap pre-filter below consult the
#: guard's own ask log before spending a subprocess per candidate — and let :func:`open_heads` answer
#: *"is this still the commit the picker was pinned to?"* off the same rows; `isDraft` because a draft
#: is not a merge candidate; `mergeable`, because a picker for a pull request that **cannot merge**
#: spends a tap on a decision that cannot be carried out (:data:`CANNOT_MERGE_STATES`); `baseRefName`,
#: because :func:`picker_mark.local_conflict` merges a head against `origin/<base>` and a pull
#: request's base is not something this file may assume — watched repositories need not share one.
#:
#: **Every one of these is a field on a call this pass already makes, and that is the whole design.**
#: `gh pr list --json number,mergeable,headRefOid,isDraft` returns mergeability in the SAME single
#: response. A `gh pr view` per candidate would buy the same fact for one subprocess per pull request
#: per pass, forever.
#:
#: **`mergeStateStatus`** — where the base requires up-to-date branches, a PR that is merely behind
#: reports `MERGEABLE` (there is no textual conflict) with `mergeStateStatus: BEHIND`, so without this
#: field a BEHIND PR reads exactly like a clean one and keeps getting asked about — GitHub then refuses
#: the merge the tap was spent on. See :data:`BEHIND_STATES`.
#:
#: **`headRefName`** is what `job_pr_draft.inflight_job_for` matches against a running job's worktree
#: branch, so a PR still being written by that job is skipped here on the very first pass, before it
#: can ever be asked about or notified red — the same "already on a call this pass makes" argument.
LIST_FIELDS = ("number,statusCheckRollup,headRefOid,headRefName,url,isDraft,mergeable,title,"
              "baseRefName,mergeStateStatus")

#: **The `mergeable` values that stop a question going out — an ALLOW-LIST of refusals, never a test
#: for anything-that-is-not-MERGEABLE.** The shape is `merge_guard.NOT_OPEN_STATES`' and so is the
#: reason: the negative spelling silently swallows `UNKNOWN` and every value GitHub adds after this
#: was written, and this predicate can only ever suppress **a question**, where an un-asked merge
#: beats an extra ask. `test_the_refusal_is_an_allow_list_never_a_negation` asserts the negative
#: spelling is absent from this file, which is why the sentence above does not spell it.
#:
#: **`UNKNOWN` therefore ASKS, deliberately, and it is the decision here most likely to be argued
#: with.** It is GitHub's lazy-computation state — the absence of an answer rather than a verdict —
#: and it is transient by construction, since the query itself schedules the computation: a PR can
#: read `UNKNOWN` one minute and `CONFLICTING` an hour later. Three reasons, descending:
#:
#: 1. **Suppressing on it could withhold a picker forever.** A pull request whose mergeability never
#:    computed would never be asked about, silently and permanently — the exact failure this module
#:    exists to remove (a green PR with nothing watching it produces nothing).
#:    Bounding a deferral instead would take a new on-disk counter; asking takes none.
#: 2. **It keeps the change a pure NARROWING.** The only pull requests that stop being asked about
#:    are the ones GitHub *positively says* cannot merge, so this cannot introduce a drop that did
#:    not exist before it. It is :func:`is_green`'s rule read in a mirror — absence of evidence is
#:    not a pass, and it is not a failure either.
#: 3. **The cost of being wrong is asymmetric, and not close.** Asking on an `UNKNOWN` that was
#:    really `CONFLICTING` costs **one visible tap**, recovered on the next pass. Withholding costs a
#:    merge that never happens, and says nothing at all while it doesn't.
#:
#: **The price is real and is stated rather than argued away:** the `UNKNOWN` window is precisely the
#: minutes after a base moves, which is precisely when a pull request goes conflicted — so a PR
#: dirtied two minutes ago may still be asked about. The suppression is partial by construction, and
#: what closes the remainder is marking and retirement, not a stricter read here.
CANNOT_MERGE_STATES = {
    "CONFLICTING": "GitHub reports it CONFLICTING against its base, so an approval given at this "
                   "commit could not be spent",
}

#: `gh pr list` gets less time than `gh pr view` does (`merge_guard._run` allows 60 s). This runs
#: every few minutes forever, so a hung call must give up well inside one pass rather than pile up.
LIST_TIMEOUT_SEC = 30


def _run(argv: list) -> tuple:
    """`(returncode, stdout, stderr)`. The one subprocess seam, so a test replaces one thing and no
    test can reach a real `gh`."""
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=LIST_TIMEOUT_SEC)
    return proc.returncode, proc.stdout, proc.stderr


def list_open_prs(repo: str, runner=None, limit: int = PR_LIST_LIMIT) -> tuple:
    """`(rows, error)` for one repository's open pull requests.

    `rows is None` means **the repo could not be read at all** — no `gh`, not logged in, rate-limited,
    offline, unparseable output — and is deliberately distinct from `[]`, which means the repo was
    read and has no open PRs. The caller reports the first and does nothing about the second.

    Never raises. Every failure of every kind becomes `(None, "why")`, because this runs inside a
    supervised daemon task and a flaky GitHub may not cost the other tasks."""
    argv = ["gh", "pr", "list", "--repo", str(repo), "--state", "open",
            "--json", LIST_FIELDS, "--limit", str(int(limit))]
    try:
        code, out, err = (runner or _run)(argv)
    except FileNotFoundError:
        return None, "`gh` is not on PATH"
    except subprocess.TimeoutExpired:
        return None, f"`gh pr list` timed out after {LIST_TIMEOUT_SEC}s"
    except Exception as e:  # noqa: BLE001 — any transport failure is just a failure to look
        return None, f"`gh pr list` failed: {e!r}"
    if code != 0:
        return None, (err or out or "gh returned non-zero").strip()[:300]
    try:
        rows = json.loads(out)
    except (ValueError, TypeError) as e:
        return None, f"unreadable gh output: {e}"
    if not isinstance(rows, list):
        return None, f"`gh pr list` returned {type(rows).__name__}, not a list"
    return [r for r in rows if isinstance(r, dict)], ""


def is_green(row: dict) -> bool:
    """Has this PR's CI reached a terminal, wholly-passing verdict?

    **`watch_pr.classify` decides, not this function.** The rollup shape `gh pr list` returns is
    byte-identical to `gh pr view`'s, so the same fold applies — and the two rules that matter here
    are its rules, not a restatement of them: **pending is never green** (unknown fails closed), and
    **an empty rollup is never green** (a PR with no checks attached is indistinguishable from a repo
    with no CI, and absence of evidence is not a pass). `null` — which `gh` returns for a PR with no
    rollup at all — folds into the same empty case."""
    nodes = row.get("statusCheckRollup")
    if not isinstance(nodes, list):
        return False
    verdict = watch_pr.classify(nodes)
    return bool(verdict["done"] and not verdict["failed"] and not verdict["empty"])


def is_red(row: dict) -> tuple:
    """`(True, verdict)` when this PR's CI has reached a terminal verdict with at least one failure,
    `(False, None)` otherwise — **pending is never red, exactly as pending is never green**
    (`watch_pr.classify` decides both, imported rather than re-derived, so the two verdicts can never
    disagree about the same rollup). An empty rollup is `False` here too: it is indistinguishable from
    a repo with no CI at all, and *nothing ran* is not the same claim as *something ran and failed*."""
    nodes = row.get("statusCheckRollup")
    if not isinstance(nodes, list):
        return False, None
    verdict = watch_pr.classify(nodes)
    if verdict["done"] and verdict["failed"] and not verdict["empty"]:
        return True, verdict
    return False, None


def unmergeable_reason(row: dict) -> str:
    """Why GitHub says this pull request cannot merge as it stands, or `""` when nothing says so.

    **A non-empty answer is positive evidence, never an absence.** The value has to be a string that
    is a key of :data:`CANNOT_MERGE_STATES`; a missing field, a `None`, a non-string, `UNKNOWN`, and
    any value GitHub invents later all return `""` and the question goes out. That direction is the
    constant's and is argued there — it is the polarity `merge_guard.not_open_refusal` already uses
    for `state`, for the same reason and with the same consequence.

    Returning the REASON rather than a bool is what lets the skip say why in the pass report. A
    suppression that reports nothing reads as *there was nothing there*, which is this module's own
    complaint about a silent truncation."""
    value = row.get("mergeable")
    if not isinstance(value, str):
        return ""
    return CANNOT_MERGE_STATES.get(value.strip().upper(), "")


#: **The `mergeStateStatus` values that stop a question going out — an ALLOW-LIST, `CANNOT_MERGE_STATES`'s
#: shape and its reason.** `BEHIND` is a DIFFERENT fact from `CONFLICTING`: the PR has no textual
#: conflict at all (`mergeable` reads `MERGEABLE`), it is merely not up to date with a base that
#: requires it (`required_status_checks.strict`). The fix is mechanical — merge the base into the
#: branch and push — where a conflict needs a hand resolution, so the two are kept in separate dicts
#: rather than folded into one "cannot merge" reason.
#:
#: **Every other value ASKS, for the same reason `CANNOT_MERGE_STATES` gives**: `CLEAN`/`UNSTABLE`/
#: `DIRTY`/`BLOCKED`/`DRAFT`/`HAS_HOOKS`/`UNKNOWN`, and anything GitHub invents after this was written,
#: all suppress nothing here — only a positively-known-BEHIND head withholds the question.
BEHIND_STATES = {
    "BEHIND": "GitHub reports it BEHIND its base branch, so an approval given at this commit could "
              "not be spent — merge the base into this branch and push, then it can be asked about "
              "again",
}


def behind_reason(row: dict) -> str:
    """Why this pull request's head is behind its base and cannot merge as it stands, or `""` when
    nothing says so. Mirrors :func:`unmergeable_reason` exactly, one field over."""
    value = row.get("mergeStateStatus")
    if not isinstance(value, str):
        return ""
    return BEHIND_STATES.get(value.strip().upper(), "")


def open_numbers(rows, truncated: bool = False):
    """The set of open PR numbers in one repo's rows, or **`None` where the answer cannot be trusted
    to be COMPLETE** — the repo could not be read at all, or the page limit was hit.

    `None` is the whole point of this function. It feeds `picker_retire`, which reasons from
    *absence*, and absence from an incomplete list means nothing at all: an open PR past the page
    boundary would look merged, and a live picker would be retired on the strength of a truncation.
    So the two unknowns collapse into one value that reads as *don't conclude anything*."""
    if rows is None or truncated:
        return None
    return {r["number"] for r in rows if isinstance(r, dict) and isinstance(r.get("number"), int)}


def open_heads(rows, truncated: bool = False):
    """`{pr_number: head_sha}` for one repo's rows, or **`None` where the answer cannot be trusted**
    — the same two unknowns as :func:`open_numbers`, collapsed the same way and for the same reason.

    **This is the join, and it costs nothing.** `headRefOid` is in :data:`LIST_FIELDS` for the
    pre-filter anyway, and `picker_retire` records a `head_sha` on every merge picker because the
    guard binds an approval to a commit; this function puts the two facts beside each other. Reading
    the head off rows this pass already fetched is what makes a moved-head retirement free — a
    `gh pr view` here would spend a call to be told what the list already said.

    **A row whose head cannot be read is ABSENT from the mapping, never present with a `None`.**
    Absence reads as *I do not know this pull request's head*, which is the direction that declines
    to retire; a `None` sitting in the mapping is a value something downstream could compare
    against, and *cannot tell* must never be comparable to a SHA."""
    if rows is None or truncated:
        return None
    heads = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        pr, head = r.get("number"), r.get("headRefOid")
        if not isinstance(pr, int) or isinstance(pr, bool):
            continue  # `isinstance(True, int)` is True, and `#True` is not a pull request
        if not isinstance(head, str) or not head.strip():
            continue
        heads[pr] = head.strip()
    return heads


def open_rows(rows, truncated: bool = False):
    """`{pr_number: row}` for one repo's rows, or **`None` where the answer cannot be trusted** — the
    same two unknowns as :func:`open_numbers`, collapsed the same way and for the same reason.

    **The third view of one list, and a third view rather than a replacement.** `open_numbers` and
    `open_heads` answer `picker_retire`'s two questions and are left exactly as they are;
    `picker_mark` needs the whole row — the head, `mergeable`, the rollup and `baseRefName` — so it
    gets the row rather than a fourth projection per field it grows an interest in. All three are
    built in one loop off one set of rows in :func:`sweep`, so they can never describe two different
    reads of GitHub.

    **A row with no readable `number` is ABSENT, never present under a guessed key**, which is
    :func:`open_heads`' rule: absence reads as *I do not know this pull request*, and that is the
    direction every consumer of this index declines on."""
    if rows is None or truncated:
        return None
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        pr = r.get("number")
        if not isinstance(pr, int) or isinstance(pr, bool):
            continue  # `isinstance(True, int)` is True, and `#True` is not a pull request
        out[pr] = r
    return out


def candidates(rows: list, inflight: dict | None = None) -> list:
    """The rows worth handing to the guard, in ascending PR order so a capped pass is deterministic
    rather than dependent on whatever order GitHub answered in.

    Three filters, and none is a policy the guard also holds. **Green** is `watch_pr`'s verdict
    (above). **Draft** is dropped because a draft is not a merge candidate — and that skip is free
    rather than a silent miss: nothing is recorded for it, so the pass after it is marked ready asks
    about it normally, even though un-drafting moves no commit and therefore no head SHA.
    **In-flight** (`job_pr_draft.py`) is dropped the identical way: a PR whose branch is checked out
    in a running job's worktree is not a candidate either, because the job may still push more
    commits — asking about it, or `pr_repair.py` treating a head it is about to move as CONFLICTING,
    is exactly what this check prevents. `inflight` defaults to `{}` (nothing in flight) so every
    direct call to this function with rows alone keeps its meaning."""
    inflight = inflight or {}
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue  # `list_open_prs` already filters these; `candidates` is also called directly
        pr = row.get("number")
        if not isinstance(pr, int):
            continue
        if row.get("isDraft"):
            continue
        if job_pr_draft.inflight_job_for(row, inflight):
            continue
        if not is_green(row):
            continue
        out.append(row)
    return sorted(out, key=lambda r: r["number"])


def already_handled(state_dir: str, row: dict) -> bool:
    """Cheap pre-filter: has a picker already gone out for this exact commit?

    **This is `merge_guard.already_asked`, called with the key `record_ask` wrote** — not a second
    dedupe. It exists only to save a `gh pr view` subprocess per candidate per pass on the steady
    state, where almost every green PR has already been asked about.

    **It can only ever cause a SKIP, never a send**, and that direction is the whole safety argument:
    `ask_on_green` re-asks the same question authoritatively against the same ledger a moment later,
    so a pre-filter that is wrong costs one subprocess and changes no outcome. An unreadable
    `url` (hence no repo key) or a missing head SHA declines to pre-filter and falls through to the
    guard, which is the side that can deny."""
    head = row.get("headRefOid")
    slug = mg.repo_from_url(row.get("url"))
    if not isinstance(head, str) or not head.strip() or not slug:
        return False
    try:
        return mg.already_asked(state_dir, int(row["number"]), head.strip(), repo=slug)
    except Exception:  # noqa: BLE001 — an unreadable ledger means "not asked", the safe direction
        return False


def extra_retire_repos(state_dir: str, watched) -> list:
    """The repositories a pending picker names that are NOT in `watched` — sorted, keyed the way
    the guard keys a repository, and read for RETIREMENT ONLY (the comment block in :func:`sweep`
    carries the argument). `picker_retire.pending_repos` is the enumeration, imported rather than
    re-derived, so this pass and the hand-run CLI cannot disagree about which repositories hold a
    stale picker — and the store read stays on `picker_retire`'s side, so this file grows no reading
    of the question store of its own. Raises on an unreadable store; :func:`sweep` catches that and
    falls back to the watched set alone."""
    watched_keys = {mg.repo_key(r) for r in watched or ()}
    return [r for r in picker_retire.pending_repos_in(state_dir)
            if mg.repo_key(r) not in watched_keys]


def sweep(repos=None, state_dir: str = mg.DEFAULT_STATE_DIR, asker=None, lister=None,
          max_asks: int = MAX_ASKS_PER_PASS, now=None, dry_run: bool = False, retirer=None,
          marker=None, notifier=None, max_red_notifies: int = MAX_RED_NOTIFIES_PER_PASS,
          max_extra_repos: int = picker_retire.MAX_EXTRA_REPOS_PER_PASS) -> dict:
    """One pass: find green PRs the guard would block, hand at most `max_asks` of them over, and tell
    the owner about at most `max_red_notifies` PRs whose CI just went red.

    Returns a report — `{"quiet_hours", "awake_override" (only when it stood the window down),
    "asked", "skipped", "deferred", "errors", "looked_at", "notified", "red_skipped",
    "red_deferred", "red_looked_at", "retire_repos", "retire_repos_deferred"}` — and **never
    raises**. The caller is a supervised daemon task; an exception escaping here would take the
    daemon's chat and reminders down with it.

    **`repos` is the set this pass may ASK about**; `None` (the default, and the daemon's call shape)
    means :func:`default_repos` — the owner's configured watched set, resolved now. It is not the
    whole set the pass reads: every repository a pending picker names is also listed, up to
    `max_extra_repos` per pass, and those extra rows feed only the marking and the retirement —
    never `candidates`, never the red arm (the argument is at the call site below). `retire_repos`
    names the extras this pass read and `retire_repos_deferred` the ones the cap held over.

    `asker` and `lister` are the test seams. With neither replaced this shells out to `gh` and, on a
    genuine candidate, to `telegram_ask.py` — so every test passes both, and no test can reach the
    network or the owner's phone. `notifier` is the same seam for the red arm — with it unreplaced
    this shells out to `telegram_send.py` via `pr_red_notify.send`.

    **EVERY ASK NAMES ITS REPOSITORY, and that is a requirement rather than a tidiness.** The guard
    does not infer a repository, so `ask_on_green(pr, repo=None)` would be an ask about a repository
    nobody established. This sweep iterates a **named** watched set and passes each row's own repo
    through, and `test_every_ask_names_its_repository` pins it rather than leaving it true by
    accident. :func:`default_repos` is not the kind of default that rule forbids: it is an explicit,
    configured list of repositories, not a repository read off wherever the daemon happens to be
    standing — and even its git fallback names the repository before any ask is made.

    **`dry_run` is a diagnostic, not a mode**: it forwards to `ask_on_green`'s own dry run, which
    renders the picker and records nothing. It cannot approve anything, because nothing here can. The
    red arm honours it the same way: a dry run counts against `max_red_notifies` and reports what it
    WOULD send, and sends and records nothing.

    **A red PR gets a notification, never a picker — `pr_red_notify.py` carries the full argument.**
    It rides this pass's own rows (no second `gh` call), is bound to `(repo, pr, head_sha)` exactly as
    the ask ledger is (a new head is a new question), defers rather than sends inside the quiet
    window, and marks a PR notified only when the send positively confirms delivery — a failed send
    costs the notice this pass and nothing else, and the next pass retries."""
    report = {"quiet_hours": False, "asked": [], "skipped": [], "deferred": [], "errors": [],
              "looked_at": 0, "retired": [], "marked": [],
              "notified": [], "red_skipped": [], "red_deferred": [], "red_looked_at": 0,
              "retire_repos": [], "retire_repos_deferred": []}
    # **Locals, deliberately NOT report fields.** `open_index`'s values are sets, and `main` prints
    # this report as JSON; more to the point all three are by-products of the listing above rather
    # than findings, and the retirement's and the marking's own sub-reports already say what they did
    # with them. They are built in one loop off one set of rows so they can never describe two
    # different reads.
    open_index, head_index, row_index = {}, {}, {}

    # **The watched set, resolved once per pass** — like `now` below, so every decision in the pass
    # reads the same answer. An explicit `repos` (a test, `--repo`) wins; `None` asks the config.
    if repos is None:
        try:
            repos = default_repos()
        except Exception as e:  # noqa: BLE001 — repo_config never raises; trust nothing
            repos = ()
            report["errors"].append(f"the watched repositories could not be resolved: {e!r}")

    # **ONE instant for the whole pass**, pinned before the first decision that reads it. Three
    # places downstream ask what time it is — this function's quiet-window check below,
    # `ask_on_green`'s own re-check of the same window, and `picker_retire` — and if each read its
    # own clock **they could answer differently within a single pass**: a pass beginning one second
    # before the window opens clears the check here and is then declined by an asker that has ticked
    # past it — the picker deferred by a window the pass believed it was outside of, silently, and
    # the suite made time-of-day dependent. Threading follows the shape `merge_guard` already uses
    # for `_stamp` and `record_ask` — the caller's instant wins, and its absence means *this*
    # moment, once.
    now = now or datetime.now(timezone.utc)

    # **A running job's own branch is not a candidate, on the very first pass it exists.** Computed
    # once per pass, like `now`, and threaded into `candidates()` and the red arm below — the same
    # in-flight map either would otherwise recompute per repository for no reason. Fail-open to `{}`
    # (`job_pr_draft`'s own contract), which means "nothing known to be in flight" and changes nothing
    # about this pass's other behaviour.
    inflight = job_pr_draft.list_running_worktree_branches(state_dir)

    # **Bound 1 — the quiet window SUPPRESSES THE ASKING AND NOT THE PASS.** An early return here
    # would cost no `gh` call overnight, and would be harmless while asking was the only thing behind
    # it — `ask_on_green` re-checks the same window independently. **Hanging `picker_mark` off an
    # early return would make it harmful**: a cost optimisation would become the thing deciding
    # whether pickers get marked, for a quarter of every day — the quarter an owner who is up late
    # can still tap in, on a picker that went dirty after it was sent.
    #
    # **What the window protects is unchanged: nothing reaches the owner's phone inside it.** No
    # picker is sent — and nothing is recorded for one either, so it is deferred rather than
    # spent. What the pass still does is *look*, and the two silent tidyings still run: a reaction
    # raises no notification and an edit to an existing message raises none either, so neither can
    # wake anyone, and both leave the morning queue honest instead of hours stale. The cost is one
    # `gh pr list` per repo per pass overnight, read-only, far inside GitHub's hourly budget.
    #
    # **And it stands down while the owner is demonstrably awake.** `mg.awake_evidence` is the
    # guard's own reading of `turns.jsonl` — an owner message, tap, reaction or attachment within the
    # awake window, never a job notice — and it fails toward QUIET. `quiet_hours` then reads False,
    # because it means *the window is holding this pass*, and `awake_override` names the instant
    # that stood it down. The red arm below reads the same flag, so it stands down too: a red notice
    # is quieter than a picker, and it shares the window.
    try:
        window = bool(mg.in_quiet_hours(now))
        awake = mg.awake_evidence(state_dir, now) if window else None
        if awake is not None:
            report["awake_override"] = awake.isoformat().replace("+00:00", "Z")
        report["quiet_hours"] = window and awake is None
    except Exception as e:  # noqa: BLE001 — cannot tell the hour ⇒ carry on, exactly as the guard does
        report["errors"].append(f"quiet-hours check failed (sweeping anyway): {e!r}")

    ask = asker or mg.ask_on_green
    notify = notifier or pr_red_notify.send
    sent = 0
    red_sent = 0
    for repo in repos or ():
        try:
            rows, err = list_open_prs(repo, runner=lister)
        except Exception as e:  # noqa: BLE001 — belt and braces; list_open_prs already swallows
            rows, err = None, f"{e!r}"
        # All three indexes are stamped for EVERY repo including the broken ones, and `None` is what
        # an unreadable or truncated repo contributes — see `open_numbers` / `open_heads`. Recording
        # the failure rather than omitting it is what stops "I could not look" from being read
        # downstream as "there is nothing there".
        truncated = bool(rows is not None and len(rows) >= PR_LIST_LIMIT)
        open_index[mg.repo_key(repo)] = open_numbers(rows, truncated=truncated)
        head_index[mg.repo_key(repo)] = open_heads(rows, truncated=truncated)
        row_index[mg.repo_key(repo)] = open_rows(rows, truncated=truncated)
        if rows is None:
            report["errors"].append(f"{repo}: {err}")
            continue  # one unreadable repo may not stop the others
        if truncated:
            report["errors"].append(
                f"{repo}: {PR_LIST_LIMIT} open PRs came back, which is the page limit — there may "
                f"be more this pass never looked at")
        for row in candidates(rows, inflight):
            report["looked_at"] += 1
            pr = row["number"]
            # **Before anything is asked, and before the cap is consulted.** A pull request that
            # cannot merge is not a question being held over: it must not occupy a cap slot, and it
            # must not land in `deferred`, which is the field that promises the next pass will ask.
            # It is reported here rather than filtered out inside `candidates` so the suppression is
            # visible in the pass — and it is checked ahead of `already_handled` because *this one
            # cannot merge right now* is the truer thing to say about a pull request than *a picker
            # went out for this commit at some point*.
            blocked = unmergeable_reason(row)
            if blocked:
                report["skipped"].append({"repo": repo, "pr": pr, "reason": blocked,
                                          "conflicted": True})
                continue
            # **Checked right beside `blocked`, same reasoning, different fact.** A behind head is
            # not a question being held over either: it must not occupy a cap slot and must not land
            # in `deferred`, which promises the next pass will ask. `conflicted` stays reserved for
            # `mergeable`'s verdict — `behind` is its own flag so a report reader can tell a textual
            # conflict from a stale branch without parsing the reason string.
            behind = behind_reason(row)
            if behind:
                report["skipped"].append({"repo": repo, "pr": pr, "reason": behind, "behind": True})
                continue
            if already_handled(state_dir, row):
                report["skipped"].append({"repo": repo, "pr": pr, "reason": "already asked at this head"})
                continue
            if report["quiet_hours"]:
                # **Bound 1's remaining half, and it is a DEFERRAL rather than a drop** — nothing is
                # recorded, so the first pass after the window lifts asks normally. `ask_on_green`
                # would refuse this anyway on its own window; skipping here is what keeps the pass
                # free of one `gh pr view` per green PR all night.
                report["deferred"].append({"repo": repo, "pr": pr,
                                           "reason": "inside the quiet window — asked on the first "
                                                     "pass after it lifts"})
                continue
            if sent >= max_asks:
                # Bound 3/4: held over, NOT dropped, and named so the log can say so.
                report["deferred"].append({"repo": repo, "pr": pr,
                                           "reason": f"{max_asks} picker(s) already sent this pass"})
                continue
            try:
                res = ask(int(pr), repo=repo, state_dir=state_dir, now=now, dry_run=dry_run)
            except Exception as e:  # noqa: BLE001 — the guard promises not to raise; trust nothing
                report["errors"].append(f"{repo}#{pr}: the ask raised: {e!r}")
                continue
            if not isinstance(res, dict):
                report["errors"].append(f"{repo}#{pr}: the ask returned {type(res).__name__}")
                continue
            reason = res.get("reason") or ""
            # A dry run counts against the cap exactly as a real send does, so `--dry-run` previews
            # the pass that WOULD happen rather than a differently-shaped one. It still sends and
            # records nothing — that is `ask_on_green`'s guarantee, not a second one made here.
            if res.get("sent") or res.get("dry_run"):
                sent += 1
                report["asked"].append({"repo": repo, "pr": pr, "reason": reason,
                                        "question_id": res.get("question_id"),
                                        "dry_run": bool(res.get("dry_run"))})
            elif res.get("ok"):
                report["skipped"].append({"repo": repo, "pr": pr, "reason": reason})
            else:
                report["errors"].append(f"{repo}#{pr}: {reason}")

        # **The red arm — same rows, no second `gh` call.** `candidates(rows)` above already dropped
        # every draft and every non-green row; this loop looks at the SAME `rows` for the opposite
        # fact. A draft is skipped here too: its CI failing is still work in progress by definition,
        # and the pass after it is marked ready notifies normally if it is still red then.
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            pr = row.get("number")
            if not isinstance(pr, int) or isinstance(pr, bool):
                continue
            if row.get("isDraft"):
                continue
            if job_pr_draft.inflight_job_for(row, inflight):
                continue  # still being written; its CI going red is work in progress, not a finding
            red, verdict = is_red(row)
            if not red:
                continue
            report["red_looked_at"] += 1
            head = row.get("headRefOid")
            if not isinstance(head, str) or not head.strip():
                report["red_skipped"].append({"repo": repo, "pr": pr,
                                              "reason": "no readable head SHA"})
                continue
            head = head.strip()
            if pr_red_notify.already_notified(state_dir, pr, head, repo=repo):
                report["red_skipped"].append({"repo": repo, "pr": pr,
                                              "reason": "already notified at this head"})
                continue
            if report["quiet_hours"]:
                # Same polarity as bound 1 above: a deferral, not a drop. Nothing is recorded, so the
                # first pass after the window lifts notifies normally if this PR is still red then.
                report["red_deferred"].append(
                    {"repo": repo, "pr": pr,
                     "reason": "inside the quiet window — notified on the first pass after it lifts"})
                continue
            if red_sent >= max_red_notifies:
                report["red_deferred"].append(
                    {"repo": repo, "pr": pr,
                     "reason": f"{max_red_notifies} red notice(s) already sent this pass"})
                continue
            if dry_run:
                red_sent += 1
                report["notified"].append({"repo": repo, "pr": pr, "dry_run": True})
                continue
            text = pr_red_notify.notify_text(repo, row, verdict)
            try:
                res = notify(text, state_dir=state_dir)
            except Exception as e:  # noqa: BLE001 — a notice may not take the pass down with it
                report["errors"].append(f"{repo}#{pr}: the red notify raised: {e!r}")
                continue
            if not isinstance(res, dict):
                report["errors"].append(f"{repo}#{pr}: the red notify returned {type(res).__name__}")
                continue
            if res.get("ok"):
                # **Marked notified ONLY on a positive confirmation.** A send that failed, timed out,
                # or came back ambiguous records nothing, so the next pass retries — the same
                # asymmetry `merge_guard.record_ask` uses: marking-then-failing loses a notification
                # forever, where a duplicate next pass costs one extra buzz.
                pr_red_notify.record_notified(state_dir, pr, head, repo=repo, now=now)
                red_sent += 1
                report["notified"].append({"repo": repo, "pr": pr})
            else:
                report["errors"].append(
                    f"{repo}#{pr}: the red notify was not sent: {res.get('error') or 'unknown error'}")

    # **The retire-only widening: every repository a pending picker names gets the READ, and only
    # the read.** A picker for a repository outside the watched set (sent by another session, or by
    # a hand-run `watch_pr.py`) would otherwise stand after its PR merged, because only the hand-run
    # `picker_retire.py` CLI ever covered such repositories. So the three indexes gain one entry per
    # extra repository, off one `gh pr list` each, and the extra rows go NOWHERE ELSE: not through
    # `candidates`, so no ask; not through the red loop, so no notice. That is deliberate and not an
    # omission — the WATCHED set is the owner's configuration (`picker-state-marking-spec.md` §10.5
    # item 1), and `repos` is untouched. The two tidyings below then see the extra repositories
    # exactly as they see a watched one: a reaction on an existing picker and an edit to an existing
    # picker, neither of which can buzz anyone. Bounded by `max_extra_repos`, held-over named, never
    # dropped; an unreadable store means no extras, never a raise.
    extra = []
    try:
        extra = extra_retire_repos(state_dir, repos)
    except Exception as e:  # noqa: BLE001 — cannot enumerate ⇒ the watched set alone
        report["errors"].append(f"the pending-picker repositories could not be read: {e!r}")
    cap = max(0, int(max_extra_repos))
    report["retire_repos"] = extra[:cap]
    report["retire_repos_deferred"] = extra[cap:]
    for repo in report["retire_repos"]:
        try:
            rows, err = list_open_prs(repo, runner=lister)
        except Exception as e:  # noqa: BLE001 — belt and braces; list_open_prs already swallows
            rows, err = None, f"{e!r}"
        truncated = bool(rows is not None and len(rows) >= PR_LIST_LIMIT)
        open_index[mg.repo_key(repo)] = open_numbers(rows, truncated=truncated)
        head_index[mg.repo_key(repo)] = open_heads(rows, truncated=truncated)
        row_index[mg.repo_key(repo)] = open_rows(rows, truncated=truncated)
        if rows is None:
            report["errors"].append(f"{repo} (retire-only read): {err}")
        elif truncated:
            report["errors"].append(
                f"{repo} (retire-only read): {PR_LIST_LIMIT} open PRs came back, which is the page "
                f"limit — nothing about its pickers can be concluded this pass")

    # **The pickers get their state marked BETWEEN the asking and the retiring**
    # (`picker_mark.py` + `../docs/picker-state-marking-spec.md`). Asking stays first and is
    # untouched. Marking is second because it is the cheap, time-sensitive half — no `gh` call, no
    # cap, one reaction per changed picker — and because a picker this pass will ALSO retire should
    # carry 😴 before it is settled, so the settled message ends up wearing the marker. A raise here
    # is recorded and **the retirement below still runs**: the two are independent tidyings and one
    # failing may not cancel the other.
    try:
        markings = (marker or picker_mark.sweep)(
            state_dir=state_dir, row_index=row_index, now=now, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 — picker_mark promises not to raise; trust nothing
        report["errors"].append(f"the picker-marking pass raised: {e!r}")
        markings = None
    if isinstance(markings, dict):
        report["marked"] = markings.get("marked") or []
        report["markings"] = markings
        report["errors"].extend(f"picker-mark: {e}" for e in markings.get("errors") or [])

    # **The dead pickers come down LAST, on the list this pass already paid for.** After the asking,
    # deliberately: asking is the thing that must not be delayed by tidying, and a retirement is
    # silent (an edited message raises no notification), so it can wait for the pass that buzzes.
    # Ask-then-retire is also the right order for the moved-head class specifically: the fresh
    # question at the new head goes out first, so the PR is never momentarily without one.
    # `retirer` is the seam; the whole thing is wrapped because a supervised daemon task may not be
    # taken down by a failure to tidy up after itself.
    try:
        retirements = (retirer or picker_retire.sweep)(
            state_dir=state_dir, open_index=open_index, head_index=head_index, now=now,
            dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 — picker_retire promises not to raise; trust nothing
        report["errors"].append(f"the stale-picker pass raised: {e!r}")
        return report
    if isinstance(retirements, dict):
        report["retired"] = retirements.get("retired") or []
        report["retirements"] = retirements
        report["errors"].extend(f"stale-picker: {e}" for e in retirements.get("errors") or [])
    return report


def summary_line(report: dict) -> str:
    """One line for `presence.log`, and it says what a bounded pass left behind — a cap that reports
    nothing reads as "covered everything" when it didn't."""
    # **The quiet window is a CLAUSE, not a whole line.** It suppresses only the asking, so the line
    # has to keep saying what the pass DID do — a marker set at 3 AM that the log renders as "no
    # sweep" is the kind of quiet lie the rest of this function exists to avoid.
    bits = []
    if report.get("quiet_hours"):
        bits.append("inside the quiet window — nothing asked, nothing recorded, next pass asks")
    elif report.get("awake_override"):
        bits.append(f"inside the quiet window but the owner is awake (heard from at "
                    f"{report['awake_override']}) — asking normally")
    bits.append(f"looked at {report.get('looked_at', 0)} green PR(s)")
    if report.get("asked"):
        bits.append("asked about " + ", ".join(f"{a['repo']}#{a['pr']}" for a in report["asked"]))
    if report.get("deferred"):
        bits.append(f"{len(report['deferred'])} held for the next pass ("
                    + ", ".join(f"{d['repo']}#{d['pr']}" for d in report["deferred"][:4]) + ")")
    # Named, never counted silently: a suppressed question is the one thing in this report that looks
    # exactly like nothing having been there. Keyed on the skip's own flag rather than on its prose,
    # so re-wording the reason cannot quietly empty this clause.
    conflicted = [s for s in report.get("skipped") or []
                  if isinstance(s, dict) and s.get("conflicted")]
    if conflicted:
        bits.append(f"{len(conflicted)} not asked about, cannot merge as they stand ("
                    + ", ".join(f"{c['repo']}#{c['pr']}" for c in conflicted[:4]) + ")")
    behind = [s for s in report.get("skipped") or []
              if isinstance(s, dict) and s.get("behind")]
    if behind:
        bits.append(f"{len(behind)} not asked about, behind their base ("
                    + ", ".join(f"{b['repo']}#{b['pr']}" for b in behind[:4]) + ")")
    # **The red arm — a NOTIFICATION, never a picker, so it gets its own clause rather than folding
    # into `asked`.** Same "a cap that reports nothing reads as covered everything" rule as the rest
    # of this function.
    if report.get("red_looked_at"):
        bits.append(f"looked at {report['red_looked_at']} red PR(s)")
    if report.get("notified"):
        bits.append("told the owner about "
                    + ", ".join(f"{n['repo']}#{n['pr']}" for n in report["notified"]))
    if report.get("red_deferred"):
        bits.append(f"{len(report['red_deferred'])} red PR(s) held for the next pass ("
                    + ", ".join(f"{d['repo']}#{d['pr']}" for d in report["red_deferred"][:4]) + ")")
    # **The retire-only extras, named when they cost something.** The read itself is silent in the
    # steady state; what has to be said is a repository the cap held over, because a held one is
    # exactly the stale picker this widening exists to take down and nobody would otherwise know it
    # was being skipped.
    if report.get("retire_repos_deferred"):
        held = report["retire_repos_deferred"]
        bits.append(f"{len(held)} pending-picker repo(s) past the retire-read cap, held for the "
                    f"next pass (" + ", ".join(held[:4]) + ")")
    marked = picker_mark.summary_line(report.get("markings") or {})
    if marked:
        bits.append(marked)
    retired = picker_retire.summary_line(report.get("retirements") or {})
    if retired:
        bits.append(retired)
    if report.get("errors"):
        bits.append(f"{len(report['errors'])} error(s)")
    return "pr-watch: " + "; ".join(bits)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Find green PRs the merge guard would block and hand them to it (never merges)")
    p.add_argument("--repo", action="append", dest="repos", default=None,
                   help="repeatable; overrides the watched set for this run (default: "
                        "repo_config's watched_repos)")
    p.add_argument("--state-dir", default=mg.DEFAULT_STATE_DIR,
                   help="seneschal/state holding the approval + ask ledgers (default: the guard's own)")
    p.add_argument("--max-asks", type=int, default=MAX_ASKS_PER_PASS,
                   help="pickers actually sent this pass; the rest are held, never dropped")
    p.add_argument("--max-red-notifies", type=int, default=MAX_RED_NOTIFIES_PER_PASS,
                   help="red-CI notices actually sent this pass; the rest are held, never dropped")
    p.add_argument("--max-extra-repos", type=int, default=picker_retire.MAX_EXTRA_REPOS_PER_PASS,
                   help="unwatched repos with a pending picker read this pass, for retirement only; "
                        "the rest are held, never dropped (0 = watched repos only)")
    p.add_argument("--dry-run", action="store_true",
                   help="render what would be asked; sends nothing and records nothing")
    args = p.parse_args(argv)
    report = sweep(repos=tuple(args.repos) if args.repos else None,
                   state_dir=args.state_dir, max_asks=args.max_asks,
                   max_red_notifies=args.max_red_notifies, max_extra_repos=args.max_extra_repos,
                   dry_run=args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
