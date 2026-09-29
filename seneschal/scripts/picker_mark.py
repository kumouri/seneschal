#!/usr/bin/env python3
"""**Put the state of a merge picker ON the picker, as a reaction.** Stdlib + `git`.

## Why a reaction

A merge picker can go dead in the owner's thread — its PR merged, its head moved, it went
conflicted — and nothing about the message says so. The assistant may well know, and say so in
**prose, after the tap**; by then the tap has been spent on a decision that buys nothing.

**The instrument is wrong, not the content.** Prose arrives in the conversation; the tap happens on
the picker. Those are two different places, and the owner is looking at the second one when they
decide. A reaction is in the same place they are looking when they tap — the picker rule itself
(*a picker is one tap; a prose list is a writing assignment*) applied to the picker.

An ordering glyph (keycap numbers) and a bed glyph for "put to bed" would be the natural vocabulary,
and both are impossible: Telegram restricts a bot's reactions to a fixed server-side set and neither
is in it. The constraint improved the design rather than compromising it.
`../docs/picker-state-marking-spec.md` carries the measurement and the argument; this file carries
the rules.

The dead pickers are not one class, which is why :func:`classify` has several death tests rather
than one: a picker whose PR merged, and a picker whose PR is still open but `CONFLICTING`, are both
unspendable for different reasons.

## Three states, one glyph each

Because a bot may set **exactly one reaction per message**, a picker carries one *state*, never a
rank *and* a state:

* **🔥 `next`** — spendable right now. Every alive-and-green picker carries it; there is no
  one-at-a-time winner (see below).
* **👀 `queued`** — not yet: no terminal green verdict.
* **😴 `asleep`** — put to bed; this picker cannot be spent.

## Every alive-and-green picker carries 🔥 — no uniqueness

**🔥 means spendable now, not "spend this one first."** Green is required for 🔥 and is not sufficient
for 😴: required, because the standing rule is *never merge a PR with red or pending CI*, so 🔥 on a
red PR would point the owner at a merge they must not make; not sufficient for 😴, because pending CI
is temporary and red CI is fixed by a commit, which moves the head, which is the moved-head class
anyway.

An earlier design picked one alive-and-green picker to hold 🔥 (the current holder if still alive,
otherwise the oldest `asked_at`) and demoted every other to 👀, to suggest a merge order that would
dodge conflicts. It broke as soon as two genuinely spendable, disjoint pickers were green at once:
when most open PRs conflict with each other anyway, no ordering can dodge a conflict, so a single
winner buys nothing over marking every spendable picker. The tie-break is gone — not left
unreachable, since dead code implementing a rule judged a defect is not neutral to leave lying
around. Full argument on both sides: `../docs/picker-state-marking-spec.md` §6.1.

## What makes a picker 😴 — mechanical, no judgment

A picker is bound to `(repo, pr, head_sha)`. It is dead when its head is no longer the pull request's
head, or the pull request is not open, or the pull request cannot merge as it stands, or it is
**BEHIND its base** where the base requires branches to be up to date before merging
(`required_status_checks.strict`) — a BEHIND head reports `mergeable: MERGEABLE` with no textual
conflict, so it would otherwise pass test 3 unnoticed. All four are read off the rows `pr_sweep`
already fetched, plus — for the "cannot merge" one — one local `git merge-tree`.

**`git merge-tree` is in the loop because GitHub's `mergeable` is stale-optimistic**: it can serve
`MERGEABLE` for minutes after a merge has dirtied the dependents, which is exactly the window a tap
gets spent in. The verdict is the union of the two sources and **the local arm may only ever ADD
deadness, never remove it** — a clean `merge-tree` never clears a GitHub `CONFLICTING`. A stale local
base could otherwise resurrect a picker that genuinely cannot merge, and the two errors do not cost
the same: a missed 😴 costs one tap, a wrongly-cleared 😴 costs a tap *and* teaches the owner the
marker is unreliable.

**😴 IS DERIVED EVERY PASS AND IS NEVER LATCHED.** For the not-open and moved-head classes the
evidence never reverses, so terminality *falls out* and needs no rule. For the conflicted class it
can reverse — and a latched 😴 on a picker that came back to life is a picker nobody will ever tap,
while `merge_guard`'s ask log is keyed on `(repo, pr, head_sha)`, so **no replacement picker would
ever be sent for that commit.** That is the silent never-asked failure `pr_sweep` exists to remove,
wearing a reaction. Deriving fresh costs nothing, so there is nothing to buy by latching. **BEHIND
reverses too, and by the same argument**: a plain merge-up clears it at the identical head SHA a
moved base dirtied, so a latch here would be the same never-replaced picker one field over.

## What makes a picker 🔥, today

Every picker that is alive **and green** carries 🔥 — no tie-break, no winner, no stability rule.
`classify` decides alive/dead; `plan` marks every alive-and-green picker `next` and every
alive-but-not-green one `queued`, and that is the whole rule. The single-winner design this replaced
is kept as history, with both sides of the argument, in `../docs/picker-state-marking-spec.md` §6
(original) and §6.1 (the amendment) — not repeated here, because the code no longer implements it.

## What it may not do

**It marks. It does not send, merge, approve, or record an approval**, and it never so much as names
`merge_guard.record_approval` in code. It **never clears a reaction** — every pending PR picker
resolves to one of three glyphs, so an empty reaction list is never needed. It **never touches a
non-PR question**: eligibility is `picker_retire.pending_pr_pickers`, imported rather than
re-spelled, so a held outbound approval of another kind and a picker with no `meta` stay out of
scope by shape and a second eligibility rule cannot drift from the first. **Nothing here changes the
approval gate** — a reaction is decoration on a question.

## Fail soft on every path

A marker is a convenience; the sweep's job is asking. `REACTION_INVALID`, a network error, a deleted
message, a 429, missing credentials, an unreadable store, an absent `git` — every one costs **the
mark** and nothing else. :func:`sweep` never raises, because its caller is a supervised daemon task
and a failure to decorate may not cost the pass that asks.

USAGE (diagnostics; `pr_sweep.sweep` is the production caller):
  python picker_mark.py --dry-run     # what WOULD be marked; reacts to nothing, writes nothing
  python picker_mark.py               # mark
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling imports work when imported, not only when run

import merge_guard as mg  # noqa: E402 — the repo key, the remote reader, the state dir, the env file
import picker_retire as pkr  # noqa: E402 — `pending_pr_pickers`: THE eligibility rule, not a copy
import telegram_ask as ta  # noqa: E402 — the store, the reaction call, and the stamp

# **`pr_sweep` is imported LAZILY, inside the three functions that use it, and that is structural
# rather than stylistic.** `pr_sweep` imports this module at module scope — the direction
# `picker_retire` already established, where the sweep owns the helpers and the helpers do not own
# the sweep — so a module-scope import here would close a cycle. It would happen to work, because
# nothing at module scope in either file touches the other's attributes; the first person to write
# `X = pr_sweep.SOMETHING` up here would break it, and the failure would depend on which module the
# process imported first. A function-local import cannot be broken that way and costs a dict hit.

#: **The repository root the local `git` calls run in.** Derived from this file's own location rather
#: than inherited from the caller's cwd, so a diagnostic run from anywhere answers about the same
#: checkout the daemon does — and so a `--worktree` job's private tree answers about *itself*, which
#: is the honest answer there (worktrees share an object store, so the heads are the same objects).
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))

#: **The three glyphs, as escapes with the character in the comment.** Escapes rather than literals
#: because these are non-BMP codepoints on a Windows host: an editor or a tool that re-saves this file
#: as cp1252 destroys a literal silently, and a marker that stops matching the one already on the
#: message would re-set a reaction every pass forever. Every one is in Telegram's fixed
#: `ReactionTypeEmoji` allow-list — pinned against this repo's own tracked copy of the set in
#: `../docs/telegram-inbound-spec.md` §3.4b. **Keycap digits and 🛏 are NOT in it and cannot be sent
#: by any bot.**
MARK_NEXT = "\U0001F525"     # 🔥 — spendable now
MARK_QUEUED = "\U0001F440"   # 👀 — queued, not yet
MARK_ASLEEP = "\U0001F634"   # 😴 — put to bed; cannot be spent

#: The state names, which are what the report and the log say. **The glyph never reaches stdout.**
#: `pr_sweep.main` prints its report with `ensure_ascii=False`, and a Windows console is cp1252 — a
#: non-BMP glyph there is a `UnicodeEncodeError` in a diagnostic. Names are also greppable in
#: `presence.log`, which a glyph is not.
STATE_NEXT = "next"
STATE_QUEUED = "queued"
STATE_ASLEEP = "asleep"

#: state name -> glyph. The ONE mapping; nothing else in this tree may spell one of these three.
MARKS = {STATE_NEXT: MARK_NEXT, STATE_QUEUED: MARK_QUEUED, STATE_ASLEEP: MARK_ASLEEP}

#: The intermediate verdict :func:`classify` returns for a picker that is still spendable. It is not
#: a state: the 🔥/👀 split depends on the CI verdict, which :func:`plan` reads off the row after
#: the local arm has had its say.
ALIVE = "alive"

#: **Local `git merge-tree` checks per pass.** Each is three git calls of tens of milliseconds, so
#: the cap is a backstop against a pathological queue rather than a cost that binds: a typical pass
#: has one or two alive pickers. The remainder simply fall back to GitHub's own `mergeable`, which
#: is a *weaker* answer and never a wrong one — so a capped pass loses freshness, never correctness,
#: and nothing needs to be held over or reported as deferred.
MAX_LOCAL_MERGE_CHECKS = 8

#: Local git gets less time than `gh` does. This runs every few minutes forever inside a supervised
#: task, so a hung call must give up well inside one pass rather than pile up.
LOCAL_GIT_TIMEOUT_SEC = 20

#: `git` with fsmonitor OFF, which is not a preference: a checkout with an fsmonitor daemon attached
#: can hang a background git call waiting on it (`job_worktree.py` passes it on every call for the
#: same reason). One spelling, so no call site can forget.
GIT = ("git", "-c", "core.fsmonitor=false")


def _run(argv: list, cwd=None) -> tuple:
    """`(returncode, stdout, stderr)`. **The one subprocess seam**, so a test replaces one thing and
    no test can reach a real `git`."""
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=cwd or REPO_ROOT, timeout=LOCAL_GIT_TIMEOUT_SEC)
    return proc.returncode, proc.stdout, proc.stderr


def local_repo(runner=None, cwd=None) -> str:
    """The `owner/name` of the checkout this process is standing in, or `""` when it cannot be read.

    **This is what scopes the local merge check to the right repository.** `pr_sweep` may watch
    several, and a `merge-tree` run in this checkout against another repository's pull request head
    would either fail to resolve the ref (the safe outcome) or — if the SHAs ever collided — answer
    about the wrong thing entirely. The derivation is `merge_guard`'s own: its `ORIGIN_ARGV` and its
    `repo_from_remote`, which is the pair the merge hook already trusts to say which repository a
    directory is.

    Never raises. Every failure — no `git`, not a repository, no `origin`, a non-GitHub remote — is
    `""`, and `""` means *use GitHub's field for everything*."""
    try:
        code, out, _err = (runner or _run)(list(mg.ORIGIN_ARGV), cwd)
    except Exception:  # noqa: BLE001 — any transport failure is just a failure to look
        return ""
    if code != 0:
        return ""
    return mg.repo_from_remote(out)


def local_conflict(base_ref: str, head_sha: str, runner=None, cwd=None) -> tuple:
    """`(True, "")` conflicted · `(False, "")` clean · `(None, "why")` **no answer at all**.

    **A MISSING OBJECT EXITS 1, EXACTLY LIKE A CONFLICT — that is the trap this function exists
    around.** `git merge-tree --write-tree origin/<base> <unfetched sha>` exits **1** with `not
    something we can merge` on stderr and **empty stdout**, while a real conflict exits **1** with
    the merged tree's OID on stdout. So an unfetched head reads as *conflicted* to anything that
    trusts the exit code alone, and a false 😴 is a picker the owner is told not to tap.

    **Two independent locks, and both are load-bearing.** Both refs are proved to resolve to a commit
    *before* the merge is attempted (`cat-file -e <ref>^{commit}`), which is the primary guard; and
    exit 1 is only read as a conflict when **stdout is non-empty**, which catches any other way git
    might decline that nobody has thought of yet. Either alone would probably do; together the
    false-😴 direction needs two independent things to go wrong.

    **It never fetches.** A head pushed since the last `seneschald-update` fetch is simply not local,
    the `cat-file` says so, and the caller falls back to GitHub's field. Fetching here would put a
    new side effect on the daemon's own checkout every pass, which is the checkout that must stay
    clean for the merge-detector's fast-forward pull — and the fallback is a weaker answer, never a
    wrong one."""
    run = runner or _run
    for ref in (base_ref, head_sha):
        if not isinstance(ref, str) or not ref.strip():
            return None, "a ref to merge was missing"
        try:
            code, _out, _err = run([*GIT, "cat-file", "-e", f"{ref.strip()}^{{commit}}"], cwd)
        except Exception as e:  # noqa: BLE001 — no git, hung, unreadable checkout
            return None, f"git could not be run here: {e!r}"
        if code != 0:
            return None, f"{ref.strip()} is not a commit in this checkout"
    try:
        code, out, _err = run([*GIT, "merge-tree", "--write-tree", base_ref.strip(), head_sha.strip()],
                              cwd)
    except Exception as e:  # noqa: BLE001
        return None, f"git merge-tree could not be run: {e!r}"
    if code == 0:
        return False, ""
    if code == 1 and (out or "").strip():
        return True, ""
    return None, f"git merge-tree gave no usable answer (exit {code})"


def pending(store: dict) -> list:
    """Every markable picker, with the reaction it is already carrying.

    **`picker_retire.pending_pr_pickers` IS the eligibility rule and is imported, never re-spelled.**
    It is the positive allow-list — never answered, never retired, a pull-request `meta.kind`, an
    integer `pr`, a readable `repo` — that keeps every non-PR question out of scope *by shape*. A
    second copy here is the second-classifier bug this directory keeps to one of, and the two would
    drift the first time either was widened.

    The one thing added is `reaction`, read off the same store record: :func:`plan` needs it for the
    idempotence skip, which is not a fact about eligibility."""
    questions = (store or {}).get("questions") or {}
    out = []
    for p in pkr.pending_pr_pickers(store or {}):
        q = questions.get(p["question_id"])
        current = (q or {}).get("reaction") if isinstance(q, dict) else None
        out.append({**p, "reaction": current if isinstance(current, str) else None})
    return out


def classify(picker: dict, row_index) -> tuple:
    """`(ALIVE | STATE_ASLEEP, reason)` for a picker this pass can judge, `(None, "why not")` when it
    cannot.

    `row_index` maps `merge_guard.repo_key(repo)` -> `{pr_number: row}`, or `None` where the list
    could not be trusted whole (`pr_sweep.open_rows`). **Three ways to be unsure and every one of
    them leaves the picker alone**, which is `picker_retire`'s polarity and its reason: a wrongly
    marked live question is worse than an unmarked one.

    The death tests, in the order they can be answered:

    1. **Absent from a list read whole** — the pull request is not open. Same inference
       `picker_retire` makes from the same evidence.
    2. **A head that moved** — the commit this question was pinned to is no longer the head, so the
       approval it would mint could not be spent.
    3. **GitHub says it cannot merge** — `pr_sweep.unmergeable_reason`, which reads the `mergeable`
       field off this row and is an ALLOW-LIST of refusals rather than a test for
       anything-that-is-not-mergeable. `UNKNOWN` therefore does **not** mark, exactly as it does not
       suppress an ask, and for the same reason: it is the absence of an answer rather than a verdict.
    4. **GitHub says it is BEHIND its base** — `pr_sweep.behind_reason`, imported the same way and
       for the same reason, reading `mergeStateStatus` off this row. Where the base requires
       up-to-date branches, a BEHIND head reports `mergeable: MERGEABLE` (no textual conflict) and
       would otherwise slip past test 3 while carrying 🔥 or 👀 into a merge GitHub is going to
       refuse. Checked after 3, never folded into it — the reason string has to say *merge up*, not
       *resolve a conflict*, or the picker points the owner at the wrong repair.

    **The local `git merge-tree` arm is NOT here**, deliberately: this function is pure so that every
    death rule is testable without a subprocess, and the local check is a *narrowing* :func:`plan`
    applies afterwards to the survivors. It has no opinion about BEHIND either — a local merge-tree
    check answers *would this conflict*, not *is the branch up to date*, so it could never confirm or
    refute test 4."""
    repo, pr = picker.get("repo"), picker.get("pr")
    key = mg.repo_key(repo)
    if key not in (row_index or {}):
        return None, f"{repo} was not swept this pass"
    rows = (row_index or {}).get(key)
    if rows is None:
        return None, (f"the open-PR list for {repo} could not be read whole, so nothing about "
                      f"#{pr} can be concluded from it")
    row = rows.get(pr)
    if row is None:
        return STATE_ASLEEP, f"{repo} #{pr} is no longer open"
    pinned = picker.get("head_sha")
    current = row.get("headRefOid")
    if isinstance(pinned, str) and pinned.strip() and isinstance(current, str) and current.strip():
        if pinned.strip() != current.strip():
            return STATE_ASLEEP, (f"{repo} #{pr} has moved past {pinned.strip()[:12]}, the commit "
                                  f"this question was pinned to")
    import pr_sweep  # noqa: PLC0415 — lazy by design; see the import block at the top of this file
    blocked = pr_sweep.unmergeable_reason(row)
    if blocked:
        return STATE_ASLEEP, f"{repo} #{pr} {blocked}"
    behind = pr_sweep.behind_reason(row)
    if behind:
        return STATE_ASLEEP, f"{repo} #{pr} {behind}"
    return ALIVE, ""


def _row(picker: dict, row_index) -> dict:
    """The row :func:`classify` judged, for the callers that also need `baseRefName` or the rollup.
    Only ever consulted after an `ALIVE` verdict, where it provably exists."""
    rows = (row_index or {}).get(mg.repo_key(picker.get("repo"))) or {}
    return rows.get(picker.get("pr")) or {}


def _entry(picker: dict, **extra) -> dict:
    """One report row, and **it carries no glyph** — :data:`STATE_NEXT`'s rule enforced at the one
    place a picker's own `reaction` field could leak into `pr_sweep`'s JSON output and through it
    into a cp1252 console. Naming the kept fields rather than filtering the dropped ones means a
    field added to a picker later cannot escape by default.

    `already` is the glyph comparison rendered as a **boolean** for exactly that reason: the
    idempotence skip needs to know whether the message is already correct, and the honest way to
    carry that out of here is the answer rather than the two strings it was computed from."""
    row = {k: picker.get(k) for k in ("repo", "pr", "question_id", "head_sha", "message_id")}
    row.update(extra)
    if "state" in row:
        row["already"] = picker.get("reaction") == MARKS.get(row["state"])
    return row


def plan(pickers: list, row_index, conflict_check=None, local_repo_key: str = "",
         max_local_checks: int = MAX_LOCAL_MERGE_CHECKS) -> tuple:
    """`(marks, skipped, local_checks)` — the state every markable picker should be in right now.

    `marks` carries one entry per picker with a `state`; `skipped` carries the ones this pass could
    not judge, each saying which unknown it hit. **A picker never appears in both**, and a picker in
    `skipped` is left exactly as it is on the owner's phone.

    `conflict_check(repo, base_ref, head_sha) -> True | False | None` is the local `git merge-tree`
    arm, injected so this function stays free of subprocesses. **It may only ever turn ALIVE into
    ASLEEP** — a `False` changes nothing and a `None` changes nothing — which is the polarity argued
    in the module docstring and asserted by a test. It is consulted only for pickers GitHub has not
    already condemned, because a second opinion on a settled verdict buys nothing.

    **`local_repo_key` scopes it, and it lives here rather than inside the callable so the cap can
    be honest.** A picker for a repository this checkout is not is skipped *before* the budget is
    charged; otherwise another repository's backlog could spend the whole allowance on calls that
    were never going to be made, and `max_local_checks` would bound something other than git calls.

    Nothing here writes anything or talks to anything: :func:`sweep` owns the effects."""
    import pr_sweep as ps  # noqa: PLC0415 — lazy by design; see the import block at the top
    marks, skipped, alive, checks = [], [], [], 0
    for p in pickers or []:
        state, reason = classify(p, row_index)
        if state is None:
            skipped.append(_entry(p, reason=reason))
            continue
        if state == STATE_ASLEEP:
            marks.append(_entry(p, state=STATE_ASLEEP, reason=reason))
            continue
        row = _row(p, row_index)
        alive.append({**p, "green": ps.is_green(row), "base": row.get("baseRefName")})

    for p in alive:
        if conflict_check is None or not local_repo_key:
            continue
        if mg.repo_key(p.get("repo")) != local_repo_key:
            continue
        base = p.get("base")
        if not isinstance(base, str) or not base.strip():
            continue
        if checks >= max(0, int(max_local_checks)):
            continue
        checks += 1
        try:
            hit = conflict_check(p.get("repo"), base.strip(), p.get("head_sha"))
        except Exception:  # noqa: BLE001 — a local check that raises is simply no answer
            hit = None
        if hit is True:
            p["dead_locally"] = True

    still = []
    for p in alive:
        if p.pop("dead_locally", False):
            marks.append(_entry(p, state=STATE_ASLEEP,
                                reason=(f"{p['repo']} #{p['pr']} conflicts with {p.get('base')} in "
                                        f"this checkout, so an approval here could not be spent")))
        else:
            still.append(p)

    # **No winner-take-all.** Every alive-and-green picker is spendable now and gets 🔥; every other
    # alive picker just has no terminal green verdict yet. 🔥 means spendable now, no uniqueness —
    # see the module docstring and `../docs/picker-state-marking-spec.md` §6.1 for the argument.
    for p in still:
        if p.get("green"):
            marks.append(_entry(p, state=STATE_NEXT,
                                reason=f"{p['repo']} #{p['pr']} is green and can merge as it stands"))
        else:
            marks.append(_entry(p, state=STATE_QUEUED,
                                reason=f"{p['repo']} #{p['pr']} has no terminal green verdict yet"))
    return marks, skipped, checks


def _config(env_file=None):
    """The bot credentials, loaded **only once there is something to mark** — so a pass with nothing
    to say touches no env file and opens no socket. `picker_retire._config`'s shape and its reason,
    spelled again here rather than imported, because importing it would make a change to one module's
    credential handling silently change the other's."""
    try:
        c = ta.cfg(ta.load_env(mg.resolve_env_file(env_file, mg.DEFAULT_TELEGRAM_ENV)))
    except Exception as e:  # noqa: BLE001
        return None, f"the Telegram credentials could not be loaded: {e!r}"
    if not c.get("token"):
        return None, "no Telegram bot token, so no picker could be marked"
    return c, ""


def sweep(state_dir: str = mg.DEFAULT_STATE_DIR, row_index=None, env_file=None, api=None,
          runner=None, now=None, dry_run: bool = False, config=None,
          max_local_checks: int = MAX_LOCAL_MERGE_CHECKS) -> dict:
    """One marking pass. **Never raises** — its caller is `pr_sweep.sweep`, a supervised daemon task,
    and a failure to decorate may not cost the pass that asks.

    Returns `{"pending", "marked", "unchanged", "skipped", "errors", "local_checks"}`. `marked` is
    what actually went on the wire this pass; `unchanged` is what was already correct, counted rather
    than listed because in the steady state it is everything.

    `api`, `runner` and `config` are the test seams: with none of them replaced this talks to the Bot
    API and shells out to `git`, so every test passes them and no test can reach the wire.

    `dry_run` reports the exact state each picker would be set to, spends the local `git` checks so
    the preview is the real verdict, and **reacts to nothing and writes nothing**."""
    report = {"pending": 0, "marked": [], "unchanged": 0, "skipped": [], "errors": [],
              "local_checks": 0}
    try:
        store = ta.load_store(ta.store_path(state_dir))
        pickers = pending(store)
        report["pending"] = len(pickers)
    except Exception as e:  # noqa: BLE001 — load_store is contracted not to raise; trust nothing
        report["errors"].append(f"the pending pickers could not be read: {e!r}")
        return report
    if not pickers:
        return report

    # The local arm, scoped to the one repository this checkout can answer for. Resolved ONCE per
    # pass: it is a fact about the process, not about a picker, and asking git per picker would spend
    # a subprocess to be told the same thing every time. An unreadable answer is `""`, which turns
    # the whole local arm off for the pass and leaves GitHub's own field doing the work.
    try:
        here = local_repo(runner=runner)
    except Exception as e:  # noqa: BLE001 — local_repo is contracted not to raise; trust nothing
        here = ""
        report["errors"].append(f"the local repository could not be read: {e!r}")
    report["local_repo"] = here

    def check(_repo, base, head):
        hit, _why = local_conflict(f"origin/{base}", head, runner=runner)
        return hit

    try:
        marks, skipped, checks = plan(pickers, row_index or {},
                                      conflict_check=check if here else None,
                                      local_repo_key=mg.repo_key(here),
                                      max_local_checks=max_local_checks)
    except Exception as e:  # noqa: BLE001
        report["errors"].append(f"the marking plan could not be built: {e!r}")
        return report
    report["skipped"] = skipped
    report["local_checks"] = checks

    # **The idempotence skip, and it is the first of two.** A pass runs every few minutes and
    # re-derives every state, so almost every picker is already correct; `telegram_ask.mark` refuses
    # a no-op again on its own, so an in-process caller cannot hammer the API by going round this.
    todo = [m for m in marks if not m.get("already")]
    report["unchanged"] = len(marks) - len(todo)
    if not todo:
        return report

    c = config
    if c is None and not dry_run:
        c, err = _config(env_file)
        if err:
            report["errors"].append(err)
            return report

    for m in todo:
        emoji = MARKS[m["state"]]
        if dry_run:
            report["marked"].append({**m, "dry_run": True})
            continue
        try:
            res = ta.mark(c, state_dir, m["question_id"], emoji, api=api, now=now)
        except Exception as e:  # noqa: BLE001 — `mark` promises not to raise; trust nothing
            report["errors"].append(f"{m['repo']}#{m['pr']}: marking {m['question_id']} "
                                    f"{m['state']} raised: {e!r}")
            continue
        if res.get("marked"):
            report["marked"].append(dict(m))
        elif res.get("ok"):
            report["unchanged"] += 1
        else:
            report["errors"].append(f"{m['repo']}#{m['pr']}: {res.get('reason')}")
    return report


def summary_line(report: dict) -> str:
    """One clause for `presence.log`, or `""` when a pass changed nothing worth a line.

    **State NAMES, never the glyphs** — see :data:`STATE_NEXT`'s note: this string reaches a Windows
    console through `pr_sweep.main` and a cp1252 encode of a non-BMP codepoint is a crash in a
    diagnostic. Names are also greppable."""
    marked = report.get("marked") or []
    if not marked:
        return ""
    by_state = {}
    for m in marked:
        by_state.setdefault(m.get("state"), []).append(f"{m.get('repo')}#{m.get('pr')}")
    parts = [f"{state} {', '.join(prs)}" for state, prs in sorted(by_state.items())]
    return "marked " + "; ".join(parts)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Mark each pending merge picker with the one reaction that says what it is — "
                    "spendable now, queued, or put to bed (never sends, never merges)")
    p.add_argument("--state-dir", default=mg.DEFAULT_STATE_DIR,
                   help="seneschal/state holding telegram-questions.json (default: the guard's own)")
    p.add_argument("--env-file", help="KEY=VALUE file with TELEGRAM_* settings (kept untracked)")
    p.add_argument("--max-local-checks", type=int, default=MAX_LOCAL_MERGE_CHECKS,
                   help="local `git merge-tree` checks this pass; the rest use GitHub's own field")
    p.add_argument("--dry-run", action="store_true",
                   help="report the state each picker would be set to; react to nothing")
    args = p.parse_args(argv)

    # The row index, built through `pr_sweep`'s own lister rather than a second one. Imported HERE
    # because `pr_sweep` imports this module at module scope: the production caller hands the index
    # in, and only this diagnostic door ever needs to go and get one. **It covers every repository
    # with a pending picker**, not just the watched set (`pr_sweep.default_repos`) —
    # `picker_retire.main` makes the same widening for the same reason: a human ran this, and it is
    # one shot rather than a poller.
    import pr_sweep as ps
    index, errors = {}, []
    for repo in {p_["repo"] for p_ in pending(ta.load_store(ta.store_path(args.state_dir)))}:
        rows, err = ps.list_open_prs(repo)
        if err:
            errors.append(f"{repo}: {err}")
        truncated = bool(rows is not None and len(rows) >= ps.PR_LIST_LIMIT)
        index[mg.repo_key(repo)] = ps.open_rows(rows, truncated=truncated)
    report = sweep(state_dir=args.state_dir, row_index=index, env_file=args.env_file,
                   max_local_checks=args.max_local_checks, dry_run=args.dry_run)
    report["errors"] = list(report.get("errors") or []) + errors
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
