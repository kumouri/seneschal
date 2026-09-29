#!/usr/bin/env python3
"""**When a merge picker stops being answerable, the picker retires itself.** Stdlib only.

Two ways a question dies. Its pull request **stops being open** (§1 below), or the pull request is
still open and its **head has moved** off the commit the question was pinned to (§2). Both leave the
same artifact — a live keyboard on the owner's phone attached to a decision that no longer exists —
and both are detected off the `gh pr list` `pr_sweep` already runs.

## §1 — the PR stopped being open

The guard has always known to *refuse* a merge on a merged or closed PR — `merge_guard
.not_open_refusal` exists for exactly that — but without this module nothing ever goes back for the
**question**. So the picker stays live on the owner's phone forever, and **a dead picker is
indistinguishable from a live one until it is tapped.** An Approve tap on a PR that merged hours
earlier costs attention and buys nothing, and a pending list that fills with questions about PRs
that already merged is a list nobody can trust. Deleting those messages by hand is worse still: it
leaves store records with no message at all, which is the orphan case §*Already settled* below
exists to reconcile.

## §2 — the PR is still open and the head has moved

An approval is bound to one exact commit: `merge_guard.verify_approval` refuses a merge whose head
is not the approved head, and `merge_guard.record_ask` keys the ask log on `(repo, pr, head_sha)`
for the same reason. So the instant a branch is rebased, amended or force-pushed, the picker pinned
to the old commit is a **dead question still sitting in the thread** — it can be tapped, and the tap
buys nothing. On a busy repository that happens several times a day.

`confirm_not_open` has no opinion about this case because it cannot have one: `not_open_refusal`
allow-lists `MERGED`/`CLOSED`, so a still-open PR is skipped as *"#N is still open"* — true, and not
the question being asked. The missing fact was never missing, only unjoined: `pending_pr_pickers`
carries the picker's `head_sha`, and `pr_sweep.LIST_FIELDS` fetches `headRefOid`.
:func:`pr_sweep.open_heads` puts them beside each other, so **this class costs zero additional API
calls**, and :func:`moved_head` compares them.

**The re-ask is free too, and needed nothing built.** The ask log is keyed on the head SHA, so at
the new commit `merge_guard.already_asked` is already `False` and the very next green sweep asks
normally. `pr_sweep` asks *before* it retires, so the fresh question goes out ahead of the stale one
coming down and the pull request is never momentarily unrepresented.

**What the retirement line may NOT say.** It states the fact — the commit this question was pinned
to is no longer the head — and stops. Whether an approval *ought* to survive a rebase is an open
question in `../docs/concurrent-pr-collisions-spec.md` (§7), and a sentence on the owner's phone may
not pre-empt a decision they have not made. :func:`moved_head_text` therefore never says the
approval "should have" carried, never calls the binding right or wrong, and never suggests anything
was lost by not tapping sooner.

## It costs nothing to notice

**`pr_sweep` already lists every open PR in every watched repository, every few minutes.** That
list is the detector: a pending merge picker whose `(repo, pr)` is **not in a list that was read
whole** is a picker whose PR is not open. There is no second poller, no `gh` call per pending
question per tick, and in the steady state — nothing merged since the last pass — **zero** added API
calls for the watched set. The same pass also lists each *unwatched* repository a pending picker
names (decline case 2 below) — one `gh pr list` per such repository per pass, and only while a
picker for it is pending, so that cost is bounded by the pickers on the owner's phone rather than by
the number of repositories they own, and it goes back to zero the moment they settle. Only when a
candidate appears is one `gh pr view` spent, and it is spent per **pull request** rather than per
picker (two live pickers for one PR confirm once), capped at :data:`MAX_PRS_CONFIRMED_PER_PASS` per
pass.

That confirmation is not ceremony. Absence from a list is an *inference*; `merge_guard.pr_facts` +
`merge_guard.not_open_refusal` is the repo's own authoritative predicate, and it is the one the
merge door already refuses on. Reusing it means a retirement and a refusal can never disagree about
the same PR — the no-second-classifier rule this directory keeps to.

## Six ways it declines to act, and every one of them leaves the picker alone

**FAIL OPEN AND FAIL SILENT.** A wrongly-retired live question is far worse than a stale one: the
owner can ignore noise, they cannot recover a decision they were never shown. So retirement happens
only from positive evidence, and *cannot tell* is always *leave it*:

1. **Not a pull-request question.** Eligibility is `meta.kind` in `RETIRABLE_META_KINDS`
   (`"merge-approval"` or `"docs-only-notice"`) with an integer `pr` and a readable `repo` — a
   positive allow-list, never *"everything except…"*. A held outbound approval of another kind, or
   a picker with `meta: null` whose origin nobody can attribute, is out of scope **by shape**, and
   tests pin both.
2. **A repository this pass did not read.** A picker whose repository is in neither index is left
   exactly as it is — *"I could not look"* is never *"there is nothing there"*. The resident pass
   reads the watched set (`pr_sweep.default_repos`, the owner's configuration) **∪ every repository
   named by a pending retirable picker** (:func:`pending_repos`, the same enumeration the CLI below
   makes), so a picker for an unwatched repository is retired within minutes of its PR merging
   exactly as a watched one is. **The widening is RETIRE-ONLY.** The extra repositories get the
   read — `gh pr list` for state and head — and the two silent tidyings (this module and
   `picker_mark`, a reaction on an existing message); they are never fed to the ask path or the
   red-CI notice, so the *watched* set stays exactly what the owner configured
   (`../docs/picker-state-marking-spec.md` §10.5 item 1). The extra reads are bounded by
   :data:`MAX_EXTRA_REPOS_PER_PASS`; a repository past the cap is held for the next pass, named,
   never dropped.
3. **A list that might be truncated.** At the page limit, absence stops meaning anything, so the
   whole repository is skipped for the pass.
4. **A PR whose state is not a KNOWN not-open state.** `not_open_refusal` is an allow-list of
   refusals (`MERGED`/`CLOSED`); an unreadable `gh`, a missing state, an unrecognised one — all
   leave the picker up.
5. **A head that cannot be compared, and — the invariant — a head that has NOT moved.** §2 retires
   only on two SHAs that are both readable and demonstrably *different*. A picker naming no
   `head_sha` (the pre-binding legacy shape), a repository this pass built no head map for, a PR
   missing from that map, a row whose `headRefOid` was unreadable: every one of them is *cannot
   tell*, and every one leaves the picker exactly as it is. **An unchanged head is never retired**,
   which is the property the whole class stands on — get it wrong and the pass eats the live
   question of every open PR on every tick.
6. **An edit Telegram did not answer.** Nothing is written until the message is provably settled.

## Edit, never delete

**Checked against the Bot API rather than assumed:** `deleteMessage` says a message can only be
deleted if it was sent **less than 48 hours ago**, after which it *"can't be deleted"* by any bot
regardless of rights — while editing a message the bot itself sent carries **no documented time
limit** (the only edit refusal of that shape is *"message can't be edited"*, for a message the bot
did not send). Deleting also erases evidence the owner may want. So the message becomes one settled
line — *"owner/name #123 merged at 17:24 — nothing to approve."* — with an empty keyboard, and the
thread stays readable. `telegram_ask.retire` does the edit and the stamp; this module decides
*which* pickers and *what the line says*.

**Already settled counts as settled.** A message deleted by hand answers *"message to edit not
found"*; a message this pass already edited before it crashed answers *"message is not modified"*.
Both retire the record without re-sending anything, which is what makes the pass convergent instead
of a loop against a message that is never coming back. That read of the Bot API's `description`
field is the only one in this tree — these 400s carry no error code of their own — and it is
deliberately locked behind `isinstance(exc, TelegramAPIError)` so a different failure shape never
gets mistaken for an already-settled message.

## The store says RETIRED, which is not ANSWERED

`retired_at` + `retired_reason` + `retired_edit` are added; `answered_at` stays null and `selected`
stays empty **forever**. No `selected` value is ever fabricated, so the ledger can never later read
as though the owner answered — and `merge_guard._question_confirms`, the floor under every approval,
reads exactly those two untouched fields.

USAGE (diagnostics; `pr_sweep.sweep` is the production caller):
  python picker_retire.py --dry-run          # what WOULD retire; edits nothing, writes nothing
  python picker_retire.py                    # retire, bounded by --max-prs
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling imports work when imported, not only when run

import merge_guard as mg  # noqa: E402 — the PR reader, the not-open predicate, the repo key
import telegram_ask as ta  # noqa: E402 — the store, the edit, and the retirement stamp

#: **The ONLY `meta.kind`s a retirement may touch.** Spelled as a set because the eligible thing is a
#: *kind of subject* (a pull request) and a future kind that is also a PR belongs here — whereas a
#: bare `== APPROVAL_META_KIND` invites someone to widen it to `!=` something instead. Both members
#: are `merge_guard`'s own constants, so the picker and the retirement cannot disagree about which
#: questions are pull-request questions. A docs-only notice is a picker about a pull request exactly
#: as an approval ask is — its PR merging or closing makes the notice exactly as stale — and the two
#: share this module's whole reasoning (§1/§2 above) with nothing PR-specific keyed on which kind it
#: was.
RETIRABLE_META_KINDS = frozenset({mg.APPROVAL_META_KIND, mg.DOCS_ONLY_META_KIND})

#: **The API bound: distinct pull requests confirmed per pass.** Each costs one `gh pr view`; every
#: picker for that PR then rides the same answer, so six stale records across four PRs cost four
#: calls, not six. Deliberately small — the remainder is held for the next pass a few minutes later,
#: and a retirement is silent (an edited message raises no notification), so nothing is lost by being
#: slow. **Setting it to 0 disables retiring without disabling the sweep.**
MAX_PRS_CONFIRMED_PER_PASS = 3

#: **The read bound: repositories OUTSIDE the watched set listed per pass, for retirement only.**
#: `pr_sweep.sweep` also lists every repository a pending retirable picker names, so a stale picker
#: for an unwatched repository comes down within minutes instead of waiting for a human to run the
#: CLI. The set is naturally a handful — the distinct repositories across the pending pickers — but a
#: store full of forgotten questions across many repositories must not turn one pass into a page of
#: `gh pr list` calls, so the extras are read in sorted order up to this many and the rest are named
#: in the report as held for the next pass. Never a drop: the pickers themselves stay pending, and a
#: held repository is reached once a repository ahead of it has no pending picker left — and the
#: pass says which ones it held every time, so a repository stuck behind the cap is visible in the
#: log rather than silently unread. **Setting it to 0 reads the watched repositories only.**
MAX_EXTRA_REPOS_PER_PASS = 10

#: **Why a candidate is a candidate**, carried on the record rather than re-derived downstream. The
#: two classes are genuinely different facts — one needs a `gh pr view` to confirm an inference from
#: absence, the other is already-positive evidence — and a report that renders them the same is how
#: a broken watcher looks healthy.
NOT_OPEN = "not-open"
MOVED_HEAD = "moved-head"


def _utcnow(now=None) -> datetime:
    return now or datetime.now(timezone.utc)


def pending_pr_pickers(store: dict) -> list:
    """Every **never-answered, never-retired** question whose subject is a pull request, oldest id
    first for determinism.

    **A positive allow-list, and that is rule 1 of the module docstring.** A question qualifies only
    by carrying `meta.kind` in :data:`RETIRABLE_META_KINDS` *and* an integer `pr` *and* a repository
    the key function can read. Everything else — a `meta` of `null`, a question of another kind, a
    legacy merge picker that names no repository — is not a candidate, and the failure mode of the
    omission is a stale picker rather than a withdrawn live decision.

    `bool` is excluded explicitly because `isinstance(True, int)` is `True`, and `#True` is not a
    pull request."""
    out = []
    for qid, q in sorted((store.get("questions") or {}).items()):
        if not isinstance(q, dict):
            continue
        if q.get("answered_at") or q.get("retired_at"):
            continue
        meta = q.get("meta")
        if not isinstance(meta, dict) or meta.get("kind") not in RETIRABLE_META_KINDS:
            continue
        pr, repo = meta.get("pr"), meta.get("repo")
        if not isinstance(pr, int) or isinstance(pr, bool):
            continue
        if not isinstance(repo, str) or not mg.repo_key(repo):
            continue
        out.append({"question_id": qid, "pr": pr, "repo": repo,
                    "head_sha": meta.get("head_sha"), "message_id": q.get("message_id"),
                    "asked_at": q.get("asked_at")})
    return out


def pending_repos(store: dict) -> list:
    """Every repository named by a pending retirable picker — the `owner/name` as the picker spells
    it, one entry per distinct `merge_guard.repo_key`, **sorted** so a capped caller reads the same
    repositories in the same order every pass.

    **This is the one enumeration, and both doors use it.** :func:`main` covers every repository
    with a pending picker rather than only the watched set, and `pr_sweep.sweep` reads the same set
    for its retire-only widening (decline case 2 above). One function rather than two
    comprehensions, so the resident pass and the hand-run CLI cannot disagree about which
    repositories have a stale picker to look for.

    Nothing here is a candidate for an ask: it derives from :func:`pending_pr_pickers`, which is
    itself the positive allow-list, so a repository only appears here because a pull-request
    question about it is already on the owner's phone."""
    seen = {}
    for p in pending_pr_pickers(store):
        seen.setdefault(mg.repo_key(p["repo"]), p["repo"])
    return [seen[k] for k in sorted(seen)]


def pending_repos_in(state_dir: str = mg.DEFAULT_STATE_DIR) -> list:
    """:func:`pending_repos` over the question store in `state_dir`. The one-line door `pr_sweep`
    uses, so the store read stays here beside the eligibility rule rather than being spelled a
    second time in the caller. Raises exactly as `telegram_ask.load_store` would; the caller
    decides what an unreadable store means (for the resident pass: the watched set alone)."""
    return pending_repos(ta.load_store(ta.store_path(state_dir)))


def moved_head(picker: dict, heads) -> tuple:
    """`(new_head, "")` when this **still-open** PR's head is demonstrably not the commit the picker
    was pinned to; `(None, "why not")` on every other answer, including *cannot tell*.

    `heads` is one repository's slice of `pr_sweep.open_heads` — `{pr_number: head_sha}`, or `None`
    where the list could not be trusted whole. **Four ways to be unsure and one way to be sure**, and
    only the sure one retires:

    * the picker names no commit (a pre-binding legacy record) — nothing to compare;
    * this pass built no head map for the repository, or the map is `None` — nothing to compare
      *against*;
    * the pull request is not in the map, or its `headRefOid` was unreadable — same;
    * **the heads match** — the question is still exactly the question it was, and retiring it would
      withdraw a live decision. This is the invariant; everything else here is a guard around it.

    Compared as full SHAs, not prefixes. Both sides come from the same `headRefOid` field of the same
    `gh` schema — `pr_facts` reads it on the ask and `pr_sweep.open_heads` reads it on the sweep — so
    an exact match is the honest test, and a prefix comparison would be inventing a tolerance for a
    disagreement that cannot arise."""
    repo, pr = picker.get("repo"), picker.get("pr")
    pinned = picker.get("head_sha")
    if not isinstance(pinned, str) or not pinned.strip():
        return None, (f"{repo} #{pr} is still open and its picker names no commit, so there is "
                      f"nothing to compare a head against")
    if not isinstance(heads, dict):
        return None, (f"{repo} #{pr} is still open and this pass read no head for {repo}, so "
                      f"whether the commit moved is unknown")
    current = heads.get(pr)
    if not isinstance(current, str) or not current.strip():
        return None, (f"{repo} #{pr} is still open and came back with no readable head, so whether "
                      f"the commit moved is unknown")
    if current.strip() == pinned.strip():
        return None, (f"{repo} #{pr} is still open at {pinned.strip()[:12]}, the commit this picker "
                      f"was pinned to")
    return current.strip(), ""


def stale_candidates(pickers: list, open_index: dict, head_index=None) -> tuple:
    """`(candidates, skipped)` — the pickers whose PR is **absent from a list read whole** (§1), or
    **present in it under a different commit** (§2). Each candidate carries `why`
    (:data:`NOT_OPEN` / :data:`MOVED_HEAD`) and, for §2, the `open_head` it moved to.

    `open_index` maps `merge_guard.repo_key(repo)` -> the set of open PR numbers, or `None` where
    the answer cannot be trusted to be complete (the repo could not be read, or the page limit was
    hit). A repository not in the index at all was not swept this pass. All three of those decline,
    and each says which one it was, because *"nothing to retire"* and *"I couldn't look"* are
    different facts and a report that renders them the same is how a broken watcher looks healthy.

    `head_index` is the parallel `{repo_key: {pr: head_sha}}` from `pr_sweep.open_heads`, and it is
    **optional on purpose**: a caller that supplies no heads gets exactly the §1 behaviour, because
    every §2 decision then falls through :func:`moved_head`'s *cannot tell* arm. A missing index may
    not be able to turn into a retirement — only into a skip."""
    candidates, skipped = [], []
    index = open_index or {}
    heads = head_index or {}
    for p in pickers:
        key = mg.repo_key(p["repo"])
        if key not in index:
            skipped.append({**p, "reason": f"{p['repo']} was not swept this pass"})
            continue
        numbers = index.get(key)
        if numbers is None:
            skipped.append({**p, "reason": f"the open-PR list for {p['repo']} could not be read "
                                           f"whole, so absence from it means nothing"})
            continue
        if p["pr"] not in numbers:
            candidates.append({**p, "why": NOT_OPEN})
            continue
        # Still open — which settles §1 and says nothing at all about §2.
        current, why_not = moved_head(p, heads.get(key))
        if current:
            candidates.append({**p, "why": MOVED_HEAD, "open_head": current})
        else:
            skipped.append({**p, "reason": why_not})
    return candidates, skipped


def when_phrase(raw, now=None) -> str:
    """`"at 17:24"` for today, `"on Aug 25 at 17:24"` for any other day, `""` for anything
    unreadable — on the owner's wall clock.

    The date appears only when it is not today's, because *"merged at 17:24"* on a picker scrolled
    past next week is a sentence that quietly lies about which day it means. The owner's wall clock
    via `clock.to_local` — the one owner-timezone conversion in this tree (`tz_common` underneath),
    never a second copy — and an unavailable clock costs the time and not the line. No zone label:
    the owner reads their own clock, and a label from the machine-local fallback could name a zone
    they are not in. The stamp is parsed with the guard's own `_parse_stamp` for the same reason: a
    third spelling of the `Z`-suffix dance is a third thing that can disagree about what a
    timestamp means."""
    at = mg._parse_stamp(raw)
    if at is None:
        return ""
    try:
        import clock
        local = clock.to_local(at)
        today = clock.to_local(_utcnow(now))
    except Exception:  # noqa: BLE001 — cannot tell the hour ⇒ say nothing about the hour
        return ""
    hhmm = f"{local:%H:%M}"
    if local.date() == today.date():
        return f"at {hhmm}"
    return f"on {local:%b} {local.day} at {hhmm}"


def settled_text(facts: dict, now=None) -> str:
    """**The exact words a retired picker becomes.** One line, and it is a record rather than an
    apology: it names the repository (a PR number is not an identity across repositories), says what
    happened and when, and closes with the only thing the owner needs to know — there is nothing to
    do.

    It never says the owner was late, never says they missed anything, and never implies a tap would
    have helped. The picker went stale on the assistant's side."""
    repo = str(facts.get("repo") or "").strip()
    pr = facts.get("pr")
    state = str(facts.get("state") or "").strip().upper()
    if state == "MERGED":
        when = when_phrase(facts.get("merged_at"), now)
        what = f"merged {when}" if when else "has merged"
    else:
        when = when_phrase(facts.get("closed_at"), now)
        what = f"was closed without merging {when}" if when else "was closed without merging"
    where = f"{repo} " if repo else ""
    return f"{where}#{pr} {what} — nothing to approve."


def moved_head_text(cand: dict) -> str:
    """**The exact words a §2 retirement becomes**, and the shorter half of this function is what it
    refuses to say.

    It states one fact — the commit this question was pinned to is no longer the head, and here is
    the one that is — and closes on what that means for the tap in front of the owner: nothing to
    approve *here*, with the question returning when the new head is green. That last clause is a
    promise the code keeps: the ask log is keyed on the head SHA, so the new commit is already an
    unasked question, and the next green sweep sends it.

    **It says nothing about whether the approval SHOULD have survived the move.** That is §7 of
    `concurrent-pr-collisions-spec.md`, an open decision, and a line on the owner's phone may not
    pre-empt it — not by arguing the binding is right, not by apologising for it, not by implying it
    is a defect being worked around. It also never says the owner was slow: the question went stale
    on the assistant's side, exactly as :func:`settled_text` refuses the same insinuation for §1."""
    repo = str(cand.get("repo") or "").strip()
    pr = cand.get("pr")
    pinned = str(cand.get("head_sha") or "").strip()[:12]
    current = str(cand.get("open_head") or "").strip()[:12]
    where = f"{repo} " if repo else ""
    return (f"{where}#{pr} has moved past the commit this question was pinned to — {pinned} is no "
            f"longer the head, {current} is. Nothing to approve here; the question comes back when "
            f"the new head is green.")


def confirm_not_open(pr: int, repo: str, runner=None) -> tuple:
    """`(facts, refusal)` — is this PR **known** not to be open?

    Both halves are `merge_guard`'s: :func:`merge_guard.pr_facts` reads it and
    :func:`merge_guard.not_open_refusal` judges it, so the retirement and the merge door share one
    answer to *"is it still open?"* rather than two that can drift.

    Never raises. `(None, "")` is *could not look* and `(facts, "")` is *open, or a state nobody
    recognises* — both leave the picker exactly as it is."""
    try:
        facts = mg.pr_facts(int(pr), repo=repo, runner=runner)
    except Exception:  # noqa: BLE001 — gh absent, offline, rate-limited, a PR that will not read
        return None, ""
    return facts, mg.not_open_refusal(int(pr), facts)


def _config(env_file=None):
    """The bot credentials, loaded **only once there is something to retire** — so a pass with no
    candidates touches no env file, opens no socket and can reach nothing. Returns `(cfg, error)`."""
    try:
        c = ta.cfg(ta.load_env(mg.resolve_env_file(env_file, mg.DEFAULT_TELEGRAM_ENV)))
    except Exception as e:  # noqa: BLE001
        return None, f"the Telegram credentials could not be loaded: {e!r}"
    if not c.get("token"):
        return None, "no Telegram bot token, so no picker could be edited"
    return c, ""


def sweep(state_dir: str = mg.DEFAULT_STATE_DIR, open_index=None, env_file=None, api=None,
          runner=None, now=None, max_prs: int = MAX_PRS_CONFIRMED_PER_PASS,
          dry_run: bool = False, config=None, head_index=None) -> dict:
    """One retirement pass over the pending pickers. **Never raises** — its caller is
    `pr_sweep.sweep`, which is a supervised daemon task, and a failure to tidy may not cost the
    sweep that asks.

    `api`, `runner` and `config` are the test seams: with none of them replaced this shells out to
    `gh` and talks to the Bot API, so every test passes them and no test can reach the wire.

    `dry_run` **still confirms** — it spends the `gh pr view` and reports the exact line each picker
    would become — but edits nothing and writes nothing. That is what makes it useful for answering
    *"what is about to happen to my phone?"* rather than a differently-shaped preview."""
    report = {"pending": 0, "candidates": 0, "retired": [], "skipped": [], "deferred": [],
              "errors": [], "confirms": 0}
    try:
        store = ta.load_store(ta.store_path(state_dir))
    except Exception as e:  # noqa: BLE001 — load_store is contracted not to raise; trust nothing
        report["errors"].append(f"the question store could not be read: {e!r}")
        return report

    try:
        pickers = pending_pr_pickers(store)
        report["pending"] = len(pickers)
        candidates, skipped = stale_candidates(pickers, open_index or {}, head_index or {})
        report["skipped"] = skipped
        report["candidates"] = len(candidates)
    except Exception as e:  # noqa: BLE001
        report["errors"].append(f"the pending pickers could not be read: {e!r}")
        return report
    if not candidates:
        return report

    # **A DRY RUN NEEDS NO CREDENTIALS**, because it edits nothing — and demanding them anyway is
    # not a harmless extra check: `telegram.env` is gitignored and lives only in the daemon's own
    # checkout, so `--dry-run` from a worktree would answer *"no bot token"* and preview nothing,
    # which is exactly when someone wants to know what is about to happen to the owner's phone.
    c = config
    if c is None and not dry_run:
        c, err = _config(env_file)
        if err:
            report["errors"].append(err)
            return report

    # One confirmation per PULL REQUEST, reused by every picker for it. `verdicts` doubles as the
    # budget: its size is what the cap bounds, so a second picker for an already-confirmed PR is
    # free and can never be the thing that pushes the pass over.
    verdicts = {}
    for cand in candidates:
        if cand.get("why") == MOVED_HEAD:
            # **No `gh pr view`, and that is not a relaxation of rule 4.** Absence from a list is an
            # *inference*, which is why §1 pays for confirmation; a row that is PRESENT and carries
            # a different `headRefOid` is direct positive evidence, read whole from the same
            # `gh pr list` this pass already spent. Confirming it would buy a second copy of the
            # answer the list gave. It is outside `max_prs` for the same reason — that cap bounds
            # API calls, this branch makes none, and letting three unrelated merged PRs defer a
            # free retirement would be a budget spent on nothing.
            text = moved_head_text(cand)
        else:
            key = (mg.repo_key(cand["repo"]), cand["pr"])
            if key not in verdicts:
                if len(verdicts) >= max(0, int(max_prs)):
                    report["deferred"].append({**cand, "reason": f"{max_prs} pull request(s) "
                                                                 f"already confirmed this pass"})
                    continue
                verdicts[key] = confirm_not_open(cand["pr"], cand["repo"], runner=runner)
                report["confirms"] += 1
            facts, refusal = verdicts[key]
            if not refusal:
                report["skipped"].append({**cand, "reason": (
                    f"{cand['repo']} #{cand['pr']} is not in a state a picker may be retired on "
                    f"({(facts or {}).get('state') if facts else 'it could not be read'}) — left "
                    f"alone")})
                continue
            text = settled_text(facts, now)

        if dry_run:
            report["retired"].append({**cand, "text": text, "edit": "dry-run", "dry_run": True})
            continue
        try:
            res = ta.retire(c, state_dir, cand["question_id"], text, api=api, now=now)
        except Exception as e:  # noqa: BLE001 — `retire` promises not to raise; trust nothing
            report["errors"].append(f"{cand['repo']}#{cand['pr']}: retiring "
                                    f"{cand['question_id']} raised: {e!r}")
            continue
        if res.get("retired"):
            report["retired"].append({**cand, "text": text, "edit": res.get("edit")})
        elif res.get("ok"):
            report["skipped"].append({**cand, "reason": res.get("reason") or "not retired"})
        else:
            report["errors"].append(f"{cand['repo']}#{cand['pr']}: {res.get('reason')}")
    return report


def summary_line(report: dict) -> str:
    """One clause for `presence.log`, or `""` when a pass did nothing worth a line. A bounded pass
    names what it held over — a cap that reports nothing reads as *covered everything*."""
    bits = []
    if report.get("retired"):
        # The class rides along, because the two mean different things to whoever reads the log: a
        # §1 retirement is the end of that pull request's story, a §2 one says another picker is
        # coming for the same PR at a new commit.
        bits.append("retired " + ", ".join(
            f"{r['repo']}#{r['pr']}" + (" (head moved)" if r.get("why") == MOVED_HEAD else "")
            for r in report["retired"]))
    if report.get("deferred"):
        bits.append(f"{len(report['deferred'])} stale picker(s) held for the next pass")
    return "; ".join(bits)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Retire the merge pickers whose pull requests are no longer open, or whose "
                    "head has moved off the commit the question was pinned to "
                    "(edits the message, never deletes it; never touches a non-PR question)")
    p.add_argument("--state-dir", default=mg.DEFAULT_STATE_DIR,
                   help="seneschal/state holding telegram-questions.json (default: the guard's own)")
    p.add_argument("--env-file", help="KEY=VALUE file with TELEGRAM_* settings (kept untracked)")
    p.add_argument("--max-prs", type=int, default=MAX_PRS_CONFIRMED_PER_PASS,
                   help="distinct pull requests confirmed this pass; the rest are held, never dropped")
    p.add_argument("--dry-run", action="store_true",
                   help="confirm and report the exact line each picker would become; edit nothing")
    args = p.parse_args(argv)

    # The open index, built through `pr_sweep`'s own lister rather than a second one. Imported HERE
    # because `pr_sweep` imports this module at module scope: the production caller hands the index
    # in, and only this diagnostic door ever needs to go and get one. **Every repository with a
    # pending picker, uncapped** — a human ran this, one shot; the resident pass reads the same
    # `pending_repos` under `MAX_EXTRA_REPOS_PER_PASS`.
    import pr_sweep
    index, heads, errors = {}, {}, []
    for repo in pending_repos(ta.load_store(ta.store_path(args.state_dir))):
        rows, err = pr_sweep.list_open_prs(repo)
        if err:
            errors.append(f"{repo}: {err}")
        truncated = bool(rows is not None and len(rows) >= pr_sweep.PR_LIST_LIMIT)
        index[mg.repo_key(repo)] = pr_sweep.open_numbers(rows, truncated=truncated)
        # Off the SAME rows as the numbers above — one list, two views of it. Building the heads
        # from a second call would let this door disagree with itself about one pull request.
        heads[mg.repo_key(repo)] = pr_sweep.open_heads(rows, truncated=truncated)
    report = sweep(state_dir=args.state_dir, open_index=index, head_index=heads,
                   env_file=args.env_file, max_prs=args.max_prs, dry_run=args.dry_run)
    report["errors"] = list(report.get("errors") or []) + errors
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
