#!/usr/bin/env python3
"""**A `PreToolUse` hook: refuse `gh pr merge` on any pull request whose CI is not green, and on any
pull request that is not docs-only unless the owner has approved that exact PR at that exact head
SHA.** Standard library only.

## Why this is code and not a sentence

The rule is already written down: `../references/autonomy-policy.md` grants merge-on-green to a
docs-only PR and makes **merging a pull request that changes functionality** ask-high. A written rule
does not hold on its own. The failure it has to survive is an agent that generalizes a few one-off
*"go ahead and merge that"* instructions into a standing authorization that does not exist — and a
note in carry-over does nothing to a future session. So this is a **stop**, in the shape this repo
already uses elsewhere (`jobs.py`'s `preflight_refusal`: *a prose rule was already in place and
prevented neither*).

## Never merge red or pending — the precondition under everything else

Every merge — docs-only, approved, any repository — is refused unless every CI check on the PR's
head has **finished and passed** (:func:`ci_refusal`). There is **no carve-out**: not for a failure
that looks pre-existing, flaky or unrelated, not for an approved PR, not for a docs-only one. Deciding
that a red check is safe to waive is exactly the self-granted authorization this module exists to
stop; the only doors past a red check are fixing it or the owner merging by hand. A rollup that
cannot be read, and a head with no checks at all, fail closed like every other stage-2 unknown.

## The two-stage failure direction, and why it is not one rule

`bash_path_guard.py` — another hook in this directory — **fails open on every path**, because it
fires on every Bash call on the machine and a guard that breaks may never be the thing that stops the
owner's work. This guard's whole purpose is the opposite: *"cannot tell"* must never be *"allow"*.

Both are true at once, so the guard is **two stages with opposite polarities**, exactly `jobs.py`'s
*fail-open on unknown, fail-closed on known-bad*:

1. **"Is this even a merge?"** — cheap, local, no subprocess. **Fails OPEN.** A crash here allows the
   command, because this stage runs on `ls`, on `git status`, on every shell call in every session,
   and a bug in it may not brick the machine.
2. **"May this merge proceed?"** — everything after a merge has been positively identified.
   **Fails CLOSED, absolutely.** No `gh`, network error, unparseable JSON, an unrecognised flag, a PR
   number that doesn't match, an empty file list, an unreadable CI rollup, an exception anywhere in
   this module — all DENY, and the message says which check failed.

The seam matters: stage 1 is a *string* question with no I/O, so its failure surface is nearly empty,
and stage 2 never runs unless stage 1 already said "this is a merge". That is what lets the strict
half be strict.

**Detection is at COMMAND POSITION, deliberately.** A segment counts only if its first token is `gh`,
so `grep "gh pr merge" notes.md` and `echo "run gh pr merge 5"` are untouched — a guard that refuses
the commands used to write about it is a guard that gets uninstalled. The cost is that a `gh` inside a
quoted string carrying a shell separator can still be seen as a segment; that direction is safe and
the rejection says what to do.

## `EXIT 2 + STDERR`, never the JSON `permissionDecision`

Same mechanism, same reason, as `bash_path_guard.py`: the JSON decision rides on a well-formed object
reaching stdout, and the contract resolves a malformed one **by letting the action proceed**. For a
guard whose entire job is to refuse, a writer bug that silently disables it is the one unacceptable
failure. Exit 2 is a code, which cannot be half-written.

## What "docs-only" means here

Every changed path is `*.md`, **or** is in :data:`DOCS_ONLY_ALLOWLIST` — which has exactly one entry
and is not a place to add things — **and none of them is prose that RUNS**.

The grant covers a PR that touches only prose *and changes no executable behavior*; a classifier that
implemented only the first half would let `seneschal/modes/chat.md`, a persona file, or
`seneschal/references/autonomy-policy.md` — *the file that IS the gate* — merge on green with no tap.
:data:`PROMPT_PATHS` and :data:`PROMPT_DIRS` are the second half, and :data:`PROMPT_PATH_EXEMPT` is
the one hole in them: the docs router (`seneschal/docs/CLAUDE.md`), an index every documentation PR
touches. **A subagent's `references/` is prose that runs too**, which is why :data:`PROMPT_DIRS`
matches a directory name rather than a place. **Any other tool's instruction file is prose that runs
too** (:data:`AGENT_INSTRUCTION_GLOBS`): a rule keyed on the filename `CLAUDE.md` alone would let an
`AGENTS.md`-only PR through as docs-only.

## The approval record, and the honest limit of it

An approval is `../state/merge-approvals/<owner>__<repo>--<pr>.json`, bound to the PR's **head SHA**:
new commits move the SHA, the record stops matching, and the guard refuses again. An approval
approves an artifact, not a name. It is single-use and expires in :data:`APPROVAL_TTL_HOURS`.

**A SHORT `--match-head-commit` IS REFUSED BEFORE THE SPEND.** The record holds the full 40-hex head.
GitHub rejects a prefix, so a merge carrying one fails AFTER the guard has spent the tap on the
attempt, and the corrected retry is then refused as already spent. :func:`match_head_refusal` blocks
a prefix (printing the full head to copy) or a wrong SHA before :func:`consume_approval` runs, and
every place the head is printed prints all of it. *Spent on an attempt* is unchanged; what this adds
is that an attempt the guard can see will fail is not allowed to be one.

**AND A REFUSED ATTEMPT IS GIVEN BACK.** A merge can pass the guard, spend the tap, and be refused by
GitHub outright — a stacked PR, for instance, must be merged through the asynchronous REST endpoint.
Nothing merged, yet the retry is blocked as already spent. The guard still spends BEFORE the merge (it
cannot see the outcome, and a live approval must never sit beside a merge that might have landed).
The way back is a `PostToolUseFailure` hook (`merge_guard.py --post-tool-use`, or the `refund`
subcommand by hand) running :func:`refund_approval`, which restores the approval **only** when the
command was the lone merge in its shell segment, exited non-zero, was not `--auto` and did not fail
as "already enqueued", the spend is under :data:`REFUND_WINDOW_MINUTES` old and has been refunded
fewer than :data:`MAX_REFUNDS` times, and `gh` — asked again, right then — says the PR is **still OPEN
at the SAME head** the approval is bound to. What it restores is the approval the owner gave, for the
artifact they gave it for. Every spend, refund and declined refund is a row in
:data:`APPROVAL_EVENTS_FILE`.

**`gh api` merges are read off their own path.** `gh api` takes no `-R`, so a reader that looked for
one and fell back to the directory's `origin` would judge `repos/<other>/pulls/26/merge`, typed in
this checkout, against this checkout's #26. :func:`parse_gh_api_merge` reads owner, repo and number
out of `repos/<o>/<r>/pulls/<n>/merge` — or `…/merge-async`, GitHub's required door for a stacked PR
— and the REST door must carry `-f sha=<full approved head>` and `-f merge_method=merge`
(:func:`api_merge_refusal`). An async merge also merges every PR below the requested one in its
stack, so it is allowed only for the bottom of a stack (:func:`stack_refusal`).

**A PR NUMBER IS NOT AN IDENTITY.** A record named `<pr>.json` carries no repository, so an approval
for #45 in one repository and an ask about #45 in another would share a file. The SHA binding keeps
the *merge* correct across repositories — the other repo's head does not match — so **the risk is
CLOBBERING, not authorization**: a live approval silently overwritten by an ask about a
same-numbered PR elsewhere. The key is `(repo, pr)`, and the repo comes from `gh`'s own answer
(`url`), never from the command. Legacy bare-number records are **tolerated rather than migrated**,
because nothing in one says which repo it was for and a guess would make the collision permanent;
:func:`load_approval` has the full argument.

**A record any agent can mint is worthless** — it is the same trust with extra steps, and the failure
being fixed is an agent deciding for itself. So :func:`record_approval` is called from **exactly one
place**: `presence.py`'s Telegram callback path, the daemon's inbound side, when a `callback_query`
carries a `merge-approval` question the owner tapped Approve on. `request` — which an agent may run
freely, because *asking* is act-low — sends the question and writes no approval.
:func:`verify_approval` additionally cross-checks the record against
`../state/telegram-questions.json`: the named question must exist, be answered, carry matching `meta`,
and have the Approve option selected. A sloppy forgery fails that.

**AND IT IS STILL FORGEABLE, WHICH IS SAID HERE RATHER THAN HIDDEN.** There is no trust boundary
between an agent and the daemon on this host — same user, same disk, same readable `telegram.env`.
Any secret the guard could check, the agent could read. What this buys is not impossibility; it is
that **routing around the guard stops being a judgment call and becomes a forgery.** The failure this
exists for is an agent reasoning itself into an authorization inside its normal loop; writing a file
it knows is a lie is a different act, and it is the most a same-user process can be held to. Every
remaining bypass is listed in `MERGE_GUARD_SETUP.md` → "What this does not stop".

## Asking has to work with NO ARGUMENTS beyond the PR, because the caller is a hook

**This module is a HOOK**: the harness spawns it with no argv, and the refusal it prints tells the
refused agent to run `request --pr N --repo R` with no mention of credentials. A door that only opens
for a caller who knows a flag the doorway never mentions is a wall. So credentials resolve with no
arguments — the sibling `telegram.env`, this directory's standing convention (`sentinel.py` does the
same) — and an explicit `--env-file` still wins. The argv is **built in order** by
:func:`request_argv` rather than spliced (splicing `--env-file` in at a fixed index once landed it
between `--state-dir` and its value); a test round-trips that argv through `telegram_ask`'s own
parser.

## A CLOSED PR IS A QUESTION WITH NO VALID ANSWER, so `request` refuses to ask it

A `request --pr 90` that resolves against the wrong repository can land on a real pull request that
merged weeks ago — and an owner tapping through a picker shaped exactly like a correct one spends a
real approval on a question that should never have been asked. :func:`not_open_refusal` is the stop,
shared by `request` and :func:`ask_on_green` so the two cannot drift. **Nothing about it lowers the
gate — it can only ever make the guard refuse MORE**, and every path it touches either sends a
QUESTION or declines to send one:

  * **The message is the diagnostic, and it leads with the REPOSITORY.** Seeing *"<owner>/<repo> #90
    is MERGED"* under a title the caller does not recognise is what tells them they named the wrong
    repo; *"#90 is merged"* would not.
  * **It is not the only stop.** The CLI requires `--repo` (below), so this message is for the caller
    who named the *wrong* repository rather than none.
  * **It fails OPEN on anything that is not a known-not-open state** — the opposite polarity from the
    rest of stage 2, because this can only suppress a *question*, and an un-asked merge is worse than
    an extra ask.

## THE REPOSITORY IS REQUIRED, NEVER DEFAULTED

A default is what the wrong-repository ask looks like from the inside: nothing fails, the wrong thing
quietly happens somewhere else, and the picker is shaped exactly like a correct one. It matters here
more than elsewhere because a checkout of this framework routinely has many worktrees live at once;
reading a repository off the current directory in that world is a coin toss reported as a fact.
**There is no repository to be wrong about unless somebody names it.**

**The requirement SPLITS, because the two doors are not the same kind of caller:**

  * **The CLI** — `check`, `judge`, `request`, `render`, `ask-on-green`, `list`, `asks`, `refund` —
    demands `--repo` and exits :data:`EXIT_NO_REPO` naming the flag when it is absent. No fallback to
    the working directory, no fallback to a constant (not even `repo_config`'s), no helpful read of
    `origin`. A CLI caller is *composing* a command, and composing it from the wrong repository is
    the failure. `prune-asks` is the one exemption and it is argued at
    :data:`REPO_REQUIRED_COMMANDS`.
  * **The hook** cannot demand anything. It is handed a shell command a human typed — `gh pr merge
    566` — and a flag nobody passed is not available to it. So it **DERIVES**, explicitly and out
    loud: :func:`derive_repo` reads the command's own `-R`/`--repo` first and the invoking
    directory's `origin` second, and **when neither answers it BLOCKS**. An unknown repository is the
    case where allowing is most dangerous, not least. Which rung answered rides on the
    :class:`Decision` and is printed, so the refusal, the `judge` output and the allow line all say
    *which repository this was decided about*.

This changes only how the guard establishes WHICH repository it is deciding about — and the approval
key still comes from `gh`'s own answer (`url`), never from the flag, because the flag says which
question to ask and the answer says what was asked about. Legacy bare-number approvals are still
tolerated; `list` reports them beside the repo-keyed records rather than filtering them out of view
(:func:`_cmd_list`).

## Which repositories are watched, and which one the assistant runs from, is CONFIG

Nothing about the owner's repositories is code. :data:`DEPLOY_ON_MERGE` — *which repositories a merge
redeploys the running assistant from, and on which branch* — resolves through
`repo_config.deploy_on_merge()` (`../references/pr-guard.json`, else the shipped example, else this
checkout's `origin` on `main`). It drives two sentences only: the Approve option's deploy claim, and
whether a prompt-shaped blocker is narrated as *what the assistant executes*. It never moves a
verdict. The PR sweep's list of repositories (`repo_config.watched_repos()`) lives with the sweep,
not here: this guard judges whatever repository a merge names.

## The picker goes out BY ITSELF when CI turns green

An opt-in *"remember to ask"* is the same class of failure as an agent having to remember to ask, so
:func:`ask_on_green` sends the picker on a terminal green verdict — called by `watch_pr.py` (a
hand-started watcher) and by `pr_sweep.py` (the resident daemon's sweep, the caller that makes
"auto-send on green" true without a watcher happening to run).

**Five refusals keep it from becoming a buzzer.** It sends only for a PR **this guard's own
classifier** would block (`non_docs_paths` — never a second classifier that could drift from it), only
while the PR is **OPEN**, only when :func:`verify_approval` finds **no live approval**, only when the
**ask log** has no row for that PR at that head SHA, and only **outside the quiet window**. The ask log
is keyed exactly as the approval record is — `(repo, pr, head_sha)` — so new commits mean a new picker
and a CI re-run on the same head means silence.

**It writes no approval, on any path, and a failed send records nothing** — so blocked-and-couldn't-ask
stays the outcome, and the next watcher tries again rather than the ask being silently spent.

## The ask ledger is a HISTORY

Two doors that each ask once (the watcher and a hand-run `request`) can each send a picker for the same
PR at the same commit seconds apart. A ledger that is a dict keyed by PR then holds one entry for two
live pickers, so the file that exists to prevent the duplicate is also the file that hides it.
:func:`record_ask` therefore **appends** to `../state/merge-ask-log.jsonl`, `state/`'s house shape:
every send gets a row, no row is ever overwritten, and `prune_asks` ages them at
:data:`ASK_LOG_RETENTION_DAYS` by building a new file and `os.replace`-ing it. And `request` is
idempotent on `(repo, pr, head_sha)` with `--resend` as an unconditional escape hatch. **It does not
refuse**: being unable to ask is strictly worse than a duplicate buzz, so a skip is `ok: true`, exit 0,
and a spent approval re-opens it with no flag at all. See :func:`duplicate_ask_reason`.

## `check` — asking the question without spending the answer

A hook reads command TEXT, so an allow spends the approval whether or not a merge follows: a
`gh pr merge` inside an `if` that evaluates false, or a diagnostic replay of the hook, each burn a tap.
:func:`check_pr` is the way to ask. It runs **the same classification and the same approval lookup
the hook runs** — :func:`evaluate_pr` is one function called by both, so a `check` verdict cannot
drift from the hook's — and reports: would it allow, in which repository, at which head, is CI green,
is there an approval, its `question_id`, its `consumed_at`, whether it matches the newest ask, and if
blocked, why.

**It resolves the record and the ledger through the SAME functions the hook does.**
:func:`load_approval` (including its legacy bare-number fallback) and :func:`asks_for` are the only
doors, so a `check` can never report on a different file than the one a merge would spend.

**The write lives on the hook path and nowhere else.** :func:`evaluate_pr` is pure — it reads and
returns — and the single `consume_approval` call sits in :func:`decide_command`, past it. There is no
argument through which `check` could spend a token, so making it able to is an edit to the seam,
which is reviewable. Tests assert it against the source and against the state directory's bytes.

**It shortens nothing.** `check` reports; it does not merge, does not offer to, and prints no command
that would. And **a check is a SNAPSHOT, said in the output on every path**: CI can go red, the head
can move, the approval can expire between the answer and the merge.

The command-judging CLI is `judge --command`, named so because it **still spends on an allow** — it
is the hook by hand. `check` took the obvious name deliberately: the safe door is the one you reach
for without thinking.

## The picker carries facts about the CHANGE, not only the artifact

A picker with the title, the verdict, the path count and the head SHA is all true and all about the
object; answering it properly means leaving Telegram and opening GitHub by hand, which is the
one-tap affordance defeating itself. So the question carries **the PR description**
(:func:`summarize_pr_body`) and **a tappable link** (:func:`pr_link_line`), in three bands whose ORDER
is a defence: the guard's own facts, then the description under a header naming where it came from,
then the link. The description is the only part that comes from outside this process, and it cannot
forge an option (one argv element, no shell), cannot reach the `--meta` (nothing from it goes there),
and cannot wear the picker's clothes (`1. ` at a line start is rewritten). **Nothing about it can make
the picker fail to send**: it is cut to whatever room :data:`QUESTION_CHARS_MAX` leaves and dropped
outright when that is too little.

**And the Approve option claims a deploy only where one happens** — conditional on
:data:`DEPLOY_ON_MERGE`, keyed on repo **and** base branch, unknown ⇒ no claim.

Install (the owner's to make — no PR can write `~/.claude/settings.json`): `MERGE_GUARD_SETUP.md`.
**Until that host-side edit exists this module is INERT.**

## Detection niceties: what stage 1 refuses to guess

**A bare `gh pr merge` or a branch name in its place is REFUSED, not resolved** — which PR either one
means depends on state outside the command being judged, and guessing would judge a different pull
request than the one on screen.

**A shell redirection is not a second positional argument.** `shlex` lexes words, not shell grammar,
so `gh pr merge 417 2>&1` would count `2>&1` as a second positional and refuse an APPROVED merge —
which is exactly how a guard teaches people to route around it. `strip_redirections` drops what the
shell would before parsing. **The gate does not move**: `gh pr merge 5 6` (a genuine second number)
and an unrecognised redirect dialect (`{fd}>`) still block, and the strip runs INSIDE
`parse_gh_pr_merge`, downstream of segmentation, so a redirect between two chained merges hides
neither.

## `prompt_paths` names what changed, not what to feel about it

`prompt_paths` filters `non_docs_paths`'s own output rather than re-deriving a second answer, and it
moves no verdict — it exists only so the refusal can say *"It changes what the assistant executes"*
rather than *"changes functionality"*, which is true and useless about a `modes/chat.md` PR. One
phrase serves the picker and the refusal both, so the wall and the phone can never disagree. **It is
said only about the assistant's own repository** (:func:`is_assistant_repo`): a foreign repo's
`CLAUDE.md` / `SKILL.md` / modes files are what THAT repo's agent executes, and anywhere else a
prompt-shaped blocker is still a blocker, described as code.

## `ask_identity` and `twin_settlements` — one definition of *the same question*

:func:`ask_identity` (given a question's `meta`) returns the exact `(repo_key, pr, head_sha)` tuple
that :func:`record_ask` keys the ask log on. `twin_settlements(store, qid)` is the pure list of *other*
records asking the same question — the mechanism behind `telegram_ask.resolve` settling twins on the
owner's tap, i.e. the double-picker closed at the settlement layer as well as at the ledger layer.

**`_question_confirms` is deliberately left separate.** It answers a different question than
`ask_identity` does, it must name which field disagreed, and it keeps a legacy repo tolerance that the
stricter tuple refuses. Rewriting it onto `ask_identity` would newly DENY approvals that verify today —
a behaviour change wearing a refactor. Instead a test binds them one-directionally: wherever
`ask_identity` says two records are the same question, the floor under every approval still agrees.

## The hook's own timeout has to be almost the whole budget

`request`/`ask_on_green` make a real network call, and the hook path reads `gh` — and **a hook timeout
IS an allow** — so its configured `"timeout"` must be **90, not the default 10**. Getting this wrong
doesn't fail loud; it fails as an allow that looks identical to a correct one.

## The picker lands in its own Telegram thread

`request_argv` appends `--topic` using `telegram_topics.TOPIC_PULL_REQUESTS` — that module's own
constant, so the routing table keeps one spelling — imported lazily like `telegram_ask` and
`telegram_format`, because this is a hook running on every shell call. It changes only WHERE the
picker lands, never WHETHER: an import that fails costs the topic, not the ask.

## The picker names the other PR fighting over the same files (`pr_overlap.py`)

Spec: `../docs/concurrent-pr-collisions-spec.md` phase 1. :mod:`pr_overlap` is imported **lazily**,
exactly like :mod:`pr_digest` — `ImportWeightTest` pins the module scope, so the hook's own decision
path never reaches it.

The block is **body text on the EXISTING question, never a second question, never a third option** —
the overlap is context, not something the owner can answer, and the option list is byte-identical
with and without it. It sits INSIDE the header band, under `Head:` and above the fenced description,
because it is one of the guard's own facts, so its characters come out of the summary's budget, never
out of the API limit.

`overlap=` defaults to `None` so `request_argv` stays network-free for its own callers and tests —
safe only because no production door relies on that default (`EveryAskDoorLooksForOverlapTest`): a
block wired into only one door would be silently absent from the one that actually asks.

**The entry lines ride `--quote`, which is the one way this feature could cost a picker.** An entry
relays another PR's title and changed paths, and `ask_citations.py` refuses to send anything it cannot
resolve — so an overlapping PR that adds, renames or deletes a file would otherwise make
`telegram_ask` exit 2 `refused: citation` with the picker never sent at all. The header and the
`(+N more)` line are the assistant's own sentences and face the citation gate like the rest of its
framing, and a span that doesn't occur in the actual built question is refused rather than silently
ignored.

`OVERLAP_CHARS` is derived from a measured worst case and is a backstop the existing caps already
satisfy — if it ever binds, the WHOLE block is dropped rather than half of it, since a truncated list
of overlapping PRs reads as a complete one. `--overlap-from <file>` replays a captured `gh pr list`
through the same seam the tests use, because the block is a claim about which PRs were open AT THAT
MOMENT, and that world is gone within the hour.

## The lead-in gets a section-by-section outline (`pr_digest.py`)

A lead-in made only of pointers (*"implements phases 2 and 3 of the spec"*) is not a description. The
header band carries a section-by-section outline beside it. **A citation gate misses this failure mode
because a phase number is a citation without the syntax.**

**An undefinable reference here is NAMED, never refused (`UNEXPLAINED_HEADER`) — the opposite polarity
to `ask_citations.py` in the very same message.** There, a refusal costs one edit to a sentence the
assistant wrote itself; here it costs the MERGE, and the repair is editing someone else's PR body — so
the guard notes the problem instead of blocking on it. A PR body with no headings at all is left
untouched, and an unimportable `pr_digest` falls back to the lead-in alone.

`blocker_line` folds a shared directory out of the changed-path list to make room, and **the "all in
X" claim is computed over EVERY blocker, never only the four shown** — truncation must never be the
source of a false sentence. It never folds a `.md` path, because folding prints a bare basename and a
bare `CLAUDE.md` is an ambiguous document reference that `ask_citations.py` refuses. And a root path is
*qualified*, not merely left unfolded (`cite_path`): a path with no directory is already a bare
basename, so the picker spells it `./CLAUDE.md` — asserted against `citation_block` itself plus a
control that the bare spelling still refuses.

**The header's own citations are paid for BEFORE the summary is cut.** Every `.md` the blocker line
names is a citation the gate will excerpt at `MIN_EXCERPT_CHARS` or refuse the whole picker over. A
summary whose room is computed from `QUESTION_CHARS_MAX` with no term for those excerpts can leave the
gate under its floor — a refusal on the assistant's own sentence, where shortening the PR's title and
body changes nothing and `--quote` is not an honest answer (that exemption is for *relayed* text).
`citation_reserve` — `ask_citations.min_room`, the gate's own refusal arithmetic exported rather than
copied, plus its safety margin — comes off the ceiling first and the summary takes what is left. If
even an empty summary cannot pay for four cited paths, `request_argv` narrows the blocker line
(`BLOCKER_SHOWN_MAX` → 3 → 2 → 1) and the rest fold into its *(+N more)*, so a shown `.md` is always
described and an undescribed one is never shown (`TheHeadersOwnCitationsAreReservedForTest`;
`min_room` is pinned to `citation_block`'s boundary in `test_ask_citations.py`).

## `render --pr N` — the read-only preview

Writes nothing, spends nothing, refuses nothing. It renders MERGED/CLOSED PRs exactly the way
`request --dry-run` correctly declines to — the only pickers anyone ever argues about after the fact.
See `MERGE_GUARD_SETUP.md`.

**The PR body is text from outside this process, handled accordingly.** It goes in as ONE argv
element into a list-form `subprocess.run` (no shell, so no forged option), never into `--meta`, and a
line wearing `render_body`'s `1. ` option shape gets rewritten. Nothing in it may cost the send:
`_send_text` does not chunk, so an over-long body is a bare 400 and the picker is simply gone — an
editorial cap sits under the structural one, measured against the real `render_body`, with visible
truncation, and the summary is dropped WHOLE before the link or the guard's own facts ever are.

## Docs-only PRs get a picker too — a NOTICE, never a gate

A docs-only PR skips `verify_approval` entirely, so without its own notice a green docs-only PR sits
with nothing for the owner to tap. The same refusals run, minus `verify_approval` (a docs-only PR has
nothing to approve), and `request_argv`'s `docs_only=True` branch builds a **different picker** — a
different header, different two options, and `meta.kind = DOCS_ONLY_META_KIND`, **never**
`APPROVAL_META_KIND` (a tap reaching the daemon's approval clause would mint an approval nothing
needed, and tell the owner one landed that never mattered). **Merging never waits on it, before,
during or after the tap** — the docs-only grant is untouched by this notice existing.
`picker_retire.RETIRABLE_META_KINDS` carries both kinds, so retire-on-merge settles a docs-only notice
exactly as it settles a real approval ask.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

#: Where Telegram credentials live when nobody passes `--env-file`. **This directory's standing
#: convention, reused rather than reinvented** — `sentinel.DEFAULT_TELEGRAM_ENV` resolves the
#: sibling `telegram.env` exactly this way. It matters here more than there: this module is a hook,
#: spawned with no argv, whose refusal tells an agent to run `request` with no flags at all.
DEFAULT_TELEGRAM_ENV = os.path.join(SCRIPT_DIR, "telegram.env")

#: The tools whose commands are inspected. Unlike `bash_path_guard`, BOTH shells are guarded — a
#: merge is a merge in either, and there is no lexing asymmetry here to make one of them a false
#: positive by construction.
GUARDED_TOOLS = ("Bash", "PowerShell")

APPROVALS_DIR = "merge-approvals"
APPROVAL_SCHEMA = "seneschal.merge-approval/1"
#: The `meta.kind` a question must carry for a tap on it to mint an approval.
APPROVAL_META_KIND = "merge-approval"

#: **The `meta.kind` for a docs-only notice — deliberately NOT `APPROVAL_META_KIND`.** A docs-only PR
#: needs no approval (the standing docs-only grant already allows the merge on green), so a tap on
#: this picker must never reach the daemon's approval clause and mint one — that would tell the owner
#: "approval recorded" for a merge that was never gated on their approval in the first place, which
#: is exactly the false claim `request_argv`'s docstring on citations refuses to let happen to a
#: *document* reference, pointed at a *gate* claim instead. `picker_retire.RETIRABLE_META_KINDS` carries this
#: alongside `APPROVAL_META_KIND` on purpose — both are still pull-request questions, and the retire-
#: on-merge path (§1/§2 of that module) must settle this one exactly as it settles an approval ask.
DOCS_ONLY_META_KIND = "docs-only-notice"

#: How long an approval stays good. The SHA binding already covers *the PR changed*; this covers
#: *the day changed*. A merge approved yesterday morning and never acted on is a decision whose
#: context has moved, and re-asking costs one tap. Shorter than `telegram_ask.QUESTION_TTL_DAYS` (7)
#: on purpose: that TTL is about whether a question is still answerable, this one is about whether an
#: answer is still current.
APPROVAL_TTL_HOURS = 24

#: The ask ledger: **every** picker that has gone out, one append-only row per send, keyed on
#: `(repo, pr, head_sha)` exactly as the approval record is — the two answer the same question about
#: the same artifact and a second key would let them disagree. It is the only thing standing between
#: "CI re-ran on the same commit" and a second buzz.
#:
#: **JSONL, because a dict keyed by PR can only hold the LAST ask** — two live pickers for one PR
#: would leave one entry behind. `state/`'s house shape, the same one `turns.jsonl` uses: append a
#: line, never rewrite one; prune builds a new file and `os.replace`s it.
ASK_LOG_FILE = "merge-ask-log.jsonl"
ASK_LOG_SCHEMA = "seneschal.merge-ask/2"
#: The clobbering predecessor. **Read, folded in, and never written or deleted** — an install that
#: ran the dict-shaped ledger may still hold real rows in it, and re-asking about a PR the owner has
#: already been asked about is exactly the buzz this ledger exists to prevent. See
#: :func:`_legacy_asks`.
LEGACY_ASK_LOG_FILE = "merge-ask-log.json"
#: 30 days: this is a delivery record, not a finding — and 30 is already 30× the approval TTL, so
#: ageing a row out can never resurrect a question. Swept by Dream beside the other delivery logs.
#: See :func:`prune_asks`.
ASK_LOG_RETENTION_DAYS = 30

#: Every spend of an approval and every attempt to give one back — refunded or declined, with the
#: reason. Append-only, `state/`'s JSONL house shape; see :func:`refund_approval`.
APPROVAL_EVENTS_FILE = "merge-approval-events.jsonl"
APPROVAL_EVENTS_SCHEMA = "seneschal.merge-approval-event/1"
#: The argv that selects the POST-tool hook (`PostToolUseFailure`). The pre-tool hook is still the
#: no-argv form; this one needs its own spelling because it is a different hook reading a
#: different event, and a flag rather than a subcommand because, like the pre-tool hook, it takes
#: no `--repo` — it reads the repository off the command it is handed.
POST_HOOK_ARG = "--post-tool-use"

#: The shape :func:`check_pr` returns. Versioned because a script will read it — the whole point of
#: `--json` is that something other than a human can consume the answer.
CHECK_SCHEMA = "seneschal.merge-check/1"

#: **Printed on every `check`, in text and in JSON, including the ones that say ALLOW.** A verdict
#: about a live PR is true for the instant it was computed and no longer: CI can go red, a push can
#: move the head, the TTL can run out. The wording matters because the failure mode is social — a
#: tool that reads like a guarantee gets trusted like one, and then a stale ALLOW reads as permission
#: rather than as an observation that has since expired.
SNAPSHOT_NOTE = (
    "This is a SNAPSHOT, not a guarantee. Between now and the merge, CI can go red, a push can move "
    "the head SHA, and the approval can be spent or expire — each of those flips this verdict. It "
    "tells you whether a tap would be wasted right now. It does not promise the merge will be "
    "allowed, and CI read green now can be re-run red before the merge."
)

#: **Whether auto-sent pickers hold for the small hours.** The nearest precedent says the small hours
#: are not a delivery window — `sentinel`'s night curfew, whose window this reuses rather than copies
#: (the owner's `owner.nightCurfew`, default 01:00–07:00) — and a code change is not worth waking the
#: owner for.
#:
#: A merge approval is not a Critical or a Call Me, so it does not pierce. `False` here sends at any
#: hour and nothing else changes. **A quiet-hours skip writes NOTHING to the ask log**, so it is a
#: deferral rather than a loss: the next watcher on that PR asks. See :func:`in_quiet_hours`.
ASK_ON_GREEN_QUIET_HOURS = True

#: **The awake override: the quiet window stands down while the owner is demonstrably awake.** The
#: window exists so a picker never WAKES the owner; it has no business holding one back from someone
#: who is already up and chatting while their PRs go green.
#:
#: "Demonstrably awake" means **the owner personally** touched a channel within
#: :data:`AWAKE_OVERRIDE_WINDOW` — a message, a picker tap, a reaction, an attachment
#: (`turns.is_owner_presence`) — and NOT a job notice or any other system line enqueued on their
#: channel. **Sleep data is deliberately not consulted:** a recent message is stronger evidence than
#: any sensor. Fails toward QUIET — an unreadable turns file means no evidence, and no evidence means
#: the window holds.
#:
#: **Scoped to the PR pickers and the red-CI notices riding the same pass** (`picker_quiet_hours`).
#: `in_quiet_hours` itself — the window — is unchanged, and reminders never read it: `sentinel`'s
#: curfew is its own gate, and this override does not reach it. `False` here restores the window as
#: an unconditional hold.
AWAKE_OVERRIDE = True
AWAKE_OVERRIDE_WINDOW = timedelta(minutes=30)

#: **The docs-only allowlist. ONE ENTRY, AND IT IS NOT A PLACE TO ADD THINGS.**
#:
#: `seneschal/context-budget.json` is the byte ledger. It is data read by `check_context_budget.py`,
#: it changes no executable behaviour, and nearly every prose PR in this repo has to touch it —
#: a docs PR that raises a budget would otherwise be reclassified as functional and stopped, which
#: turns the one standing grant into friction and teaches everyone to route around the guard. That
#: is the whole test for membership: **a path belongs here only if changing it cannot change what
#: runs.** A `.py`, a workflow, a lockfile, a `.json` that any running code branches on — none of
#: those qualify, however docs-adjacent the PR feels. When in doubt it is not in the list; the cost
#: of being wrong on this side is one Telegram tap.
DOCS_ONLY_ALLOWLIST = frozenset({"seneschal/context-budget.json"})

#: **THE PROMPT-PATH DENYLIST — THE ALLOWLIST'S MIRROR IMAGE, AND IT IS NOT A NEW POLICY.**
#:
#: `../references/autonomy-policy.md` grants merge-on-green to a PR whose diff *"touches only prose …
#: and changes no executable behavior."* A classifier that implements the first half — `.md` ⇒ docs —
#: and not the second would wave through the prompt the daemon executes. So these paths are `.md` and
#: are **not** documentation: the mode bodies are read imperatively on dispatch, so an edit there
#: edits a live run, and `seneschal/references/autonomy-policy.md` **is the gate itself** — without
#: this, the one file that says what may merge unasked would be a file that could merge unasked.
#:
#: **There are two match kinds, and the second is a directory NAME rather than a place.** Each
#: entry here is `(directory prefix, filename)`: an empty prefix means anywhere in the tree, an
#: empty filename means every file under the prefix. :data:`PROMPT_DIRS` is the other kind — a
#: directory *name* that makes everything below it a prompt wherever in the tree it appears, which
#: is how `seneschal/references/**` is covered now that it is not the only `references/` that runs.
#: Matching is on the lowercased, forward-slashed path — **case-insensitively, which is the
#: widening direction**, the same way :func:`non_docs_paths` already reads the `.md` extension.
PROMPT_PATHS = (
    ("seneschal/modes/", ""),    # the mode bodies — read imperatively on dispatch
    ("persona/", ""),            # who the assistant is, mirrored into the phone screener's persona
    ("", "skill.md"),            # seneschal/SKILL.md (the orchestrator) + every subagents/**/SKILL.md
    ("", "claude.md"),           # every per-directory instruction file, minus the exemption below
)   # `seneschal/references/**` is NOT missing from this tuple — PROMPT_DIRS subsumes it, see below.

#: **A `references/` DIRECTORY IS A PROMPT TREE WHEREVER IT SITS.** This is the entry that
#: *subsumes* `seneschal/references/` rather than sitting beside it — same rule, stated about the thing
#: that is actually load-bearing.
#:
#: A subagent's `references/` (e.g. under `subagents/journal-steward/`) is read by that subagent
#: exactly the way the orchestrator reads `seneschal/references/` — a change there can alter what a
#: nightly run writes into the owner's store. A denylist written only against `seneschal/`'s prompt
#: tree classifies such a PR docs-only: no approval, and therefore no picker either.
#:
#: **THE OBVIOUS PATCH IS `("subagents/", "")` AND IT IS WRONG.** Most `.md` files under
#: `subagents/` are a `SKILL.md` (already covered above) or live under a `references/` directory —
#: but some are ordinary human-facing documentation (a README, a conversion-pattern note, a setup
#: runbook). A prefix rule blocks those for changing nothing that runs: cost with no gate behind it,
#: the mistake the exemption below exists to remember.
#:
#: **NO EXEMPTION IS WARRANTED HERE.** The exemption test is traffic, not inconvenience: the docs
#: router is touched by most prose PRs because every one adds an entry, while a given file under a
#: `references/` directory is touched rarely. Nothing there has the router's high-traffic-index
#: character, so a hole here would buy friction relief that does not exist.
#:
#: Matching is on **path segments**, so a top-level `references/` or one nested five deep reads the
#: same, and a file merely *named* `references.md` does not match.
PROMPT_DIRS = frozenset({"references"})

#: **EVERY FILE AN AGENT HARNESS LOADS AS INSTRUCTIONS IS A PROMPT, WHATEVER TOOL IT IS NAMED FOR.**
#:
#: :data:`PROMPT_PATHS` keys on the filename `CLAUDE.md`, so without this a PR touching only an
#: `AGENTS.md` would be docs-only and merge on green with no picker — yet Claude Code loads
#: `AGENTS.md` as instructions where no `CLAUDE.md` exists at that level or above, and Codex,
#: Copilot, Gemini and Cursor read their own files unconditionally. The gap is closed here whether or
#: not a repository adopts any of those files.
#:
#: What is already covered is not repeated: `CLAUDE.md` and `SKILL.md` are in :data:`PROMPT_PATHS`.
#: The non-`.md` entries (`.claude/settings.json`, `.cursorrules`, a `.github/agents/*.yml`) were
#: blockers already, as every non-`.md` is; listing them makes the picker call them *prompt* rather
#: than code, which is what they are.
#:
#: Globs over the lowercased, forward-slashed path: `**` is any number of whole segments (zero
#: included), `*` stays inside one segment. Every entry starts `**/` — a nested package's file is
#: loaded by the tools that walk the tree, and widening is the safe direction for a denylist.
AGENT_INSTRUCTION_GLOBS = (
    "**/agents.md",                          # Codex, Copilot, Cursor, … and Claude Code's fallback
    "**/claude.local.md",                    # Claude Code's personal per-project instructions
    "**/gemini.md",                          # Gemini CLI
    "**/.github/copilot-instructions.md",    # Copilot repository instructions
    "**/*.instructions.md",                  # Copilot path-scoped instructions
    "**/.github/agents/**",                  # Copilot custom agents
    "**/.claude/**",                         # Claude Code commands / agents / skills / settings / hooks
    "**/.cursorrules",                       # Cursor, legacy single file
    "**/.cursor/rules/**",                   # Cursor project rules
)


def _glob_regex(glob: str):
    """One :data:`AGENT_INSTRUCTION_GLOBS` entry -> an anchored regex. `**` spans whole segments
    (possibly empty ones, so a leading `/` still matches); `*` never crosses a `/`."""
    parts = glob.split("/")
    out = ""
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            out += ".+" if last else "(?:[^/]*/)*"
        else:
            out += "[^/]*".join(re.escape(s) for s in part.split("*")) + ("" if last else "/")
    return re.compile(out + r"\Z")


_AGENT_INSTRUCTION_RES = tuple(_glob_regex(g) for g in AGENT_INSTRUCTION_GLOBS)

#: **THE ONE EXEMPTION — DO NOT TIDY IT AWAY.**
#:
#: `seneschal/docs/CLAUDE.md` is the **router**: an index of documents with a one-line status each, not
#: an instruction. Every documentation PR touches it to add its entry, which is exactly what makes it
#: different from its siblings. Without this hole, *every `**/CLAUDE.md` is a prompt* would flip
#: nearly every docs-only PR to ask-high on the router alone — it would not narrow the docs-only
#: grant, it would **repeal** it. A grant that fires the picker every time is a grant that has been
#: withdrawn, and it teaches everyone to route around the guard.
#:
#: Lowercased, because the membership test above is.
PROMPT_PATH_EXEMPT = frozenset({"seneschal/docs/claude.md"})

#: **WHICH REPOSITORIES A MERGE ACTUALLY DEPLOYS — AND WHY THAT IS A LOOKUP RATHER THAN A SENTENCE.**
#:
#: The Approve option could say, unconditionally, that merging *"redeploys the running assistant."*
#: That is true only of a repository the resident daemon runs from — where `seneschald-update`
#: follows a branch and merging into it **is** deploying (`PATH_A_CUTOVER.md`). For every other
#: repository the owner approves PRs in, the sentence is false. Overstating the stakes is not a safe
#: error: it teaches the owner that the gate's own words are decoration, and a gate that is skimmed
#: has stopped being a gate.
#:
#: Keyed on the **branch as well as the repo**, because the claim is false *within* the deploying
#: repo too — a merge into the integration branch of a Git Flow repo deploys nothing when the daemon
#: runs off `main`. An absent or unreadable base branch ⇒ **NO CLAIM**, which is the safe direction for
#: a sentence: omitting something true costs nothing, asserting something false costs the gate's
#: credibility.
#:
#: **The map is CONFIG, not code**: `repo_config.deploy_on_merge()` — the `deploy_on_merge` key of
#: `../references/pr-guard.json` (else the shipped `pr-guard.example.json`), and when that is empty,
#: this checkout's own `origin` on `main`. This name is the one override: `None` (the shipped value)
#: means *ask `repo_config` at call time*; a test, or an embedding caller, may set it to a
#: `{"owner/name": "branch"}` dict and every reader below sees exactly that. Read only through
#: :func:`deploy_on_merge_map`, so the claim is repo-conditional in one place instead of spelled in
#: three.
DEPLOY_ON_MERGE = None


def deploy_on_merge_map() -> dict:
    """``{"owner/name" (lowercased): branch}`` — :data:`DEPLOY_ON_MERGE` when set, else
    `repo_config.deploy_on_merge()`. **Never raises**: `repo_config` is imported lazily (this module
    is a hook on every shell call and `ImportWeightTest` pins its module scope to the standard
    library), and any failure answers `{}` — no repository deploys, so no deploy claim is made and
    no prompt path is narrated as the assistant's. That is the safe direction for both readers."""
    try:
        mapping = DEPLOY_ON_MERGE
        if mapping is None:
            import repo_config
            mapping = repo_config.deploy_on_merge()
        return {str(k).strip().lower(): str(v).strip() for k, v in dict(mapping).items()
                if str(k).strip() and str(v).strip()}
    except Exception:  # noqa: BLE001 — a config read may never cost the picker or the verdict
        return {}

#: The Bot API's own per-message cap — `telegram_format.TELEGRAM_MESSAGE_LIMIT`, **copied rather than
#: imported**. This module is a `PreToolUse` hook that runs on every Bash and PowerShell call on this
#: machine under a bare `python`, and `ImportWeightTest` pins its module scope to the standard library
#: for that reason. `PickerFitsTheApiLimitTest` asserts the copy against the original, so a drift is a
#: red suite rather than a silently rejected message.
TELEGRAM_MESSAGE_LIMIT = 4096

#: Head-room for everything `telegram_ask.render_body` adds *around* the question: the "Tap one."
#: line, the option numbering, and both option descriptions. That comes to ~270 characters for the
#: picker this module builds; 512 is that with room for the descriptions to grow a sentence. The
#: suite renders a maximal question through the real `render_body` and asserts the result fits, so
#: this number cannot rot into a guess.
PICKER_SCAFFOLD_RESERVE = 512

#: The hard structural ceiling on the `--question` string. **A picker that does not arrive is far
#: worse than a terse one** — the whole feature is a one-tap affordance, and a 400 from the Bot API
#: turns it into silence. Over-long *HTML* degrades safely (`telegram_format.render_for_api` returns
#: `None` and the unconverted source goes out instead); an over-long *source* does not, so this is
#: the number that actually has to hold.
QUESTION_CHARS_MAX = TELEGRAM_MESSAGE_LIMIT - PICKER_SCAFFOLD_RESERVE

#: The editorial cap on the whole description band — lead-in **plus** outline, applied before the
#: structural clamp above and independently of it. The clamp is the backstop; this is the reading
#: experience.
#:
#: **The characters are spent on a SHAPE rather than on more paragraph.** A reader who needs *a
#: sentence on each part* is not helped by *more prose* — a picker that scrolls is a picker the owner
#: taps without reading, which is worse than the terse one. So the band is a short lead plus a
#: **bulleted outline**, which is skimmed rather than read: ~1,600 characters where roughly half of
#: them are section titles at a line start. See :data:`LEAD_SUMMARY_CHARS` for the split.
BODY_SUMMARY_CHARS = 1600

#: The lead-in's own share. That is the trade, said plainly: past about 600 characters a lead-in is
#: the body restating itself, while the outline buys a whole section per ~200 characters. Truncation
#: stays visible (:data:`TRUNCATION_MARK`) and the link is still one tap away.
LEAD_SUMMARY_CHARS = 600

#: The outline's share of :data:`BODY_SUMMARY_CHARS`, and the per-line cap inside it. ~220 characters
#: is a heading plus one sentence. Six entries is what fits on a phone screen
#: without becoming a table of contents; a body with more says so (:data:`OUTLINE_MORE`).
OUTLINE_CHARS = 900
OUTLINE_ENTRY_CHARS = 220
OUTLINE_MAX_SECTIONS = 6
#: Below this there is no outline at all. One heading clipped to forty characters is a table of
#: contents with one entry — it costs a line and answers nothing, which is the miniature of the
#: failure this band exists to fix.
OUTLINE_MIN_ROOM = 80

#: A PR title is unbounded as far as this module is concerned (GitHub caps it at 256). Truncating it
#: keeps a pathological title from crowding out the summary it is supposed to introduce.
TITLE_CHARS = 160

#: How many blocker paths :func:`blocker_line` names before "(+N more)". Four fits a phone line;
#: **and it is a ceiling, not a promise** — :func:`request_argv` shows fewer when the `.md` paths
#: among them cannot all be described at `ask_citations.MIN_EXCERPT_CHARS`, because every `.md` it
#: prints is a citation the gate will refuse the whole picker over. Whatever is not shown is still
#: counted, so the line stays honest at any width.
BLOCKER_SHOWN_MAX = 4

#: Truncation is always VISIBLE and always says where the rest is. A summary that silently stops is
#: worse than no summary, because it reads as complete.
TRUNCATION_MARK = "… (truncated — full description on GitHub)"

#: The label above the extracted body. It is there so the half of the message that came from OUTSIDE
#: this process is fenced off from the guard's own facts by a line the owner can see.
SUMMARY_HEADER = "What it changes, from the PR description:"

#: The label above the outline. Still the PR's own words, so it sits INSIDE the fenced band rather
#: than starting a new one.
OUTLINE_HEADER = "Section by section:"
#: Sections that did not fit. Visible truncation, the same rule as :data:`TRUNCATION_MARK`: an
#: outline that silently stops reads as the whole change.
OUTLINE_MORE = "• (+%d more section%s — full description on GitHub)"

#: **The unexplained-reference line, and it is the guard's own sentence, not the PR's.**
#:
#: `pr_digest` resolves an ordinal reference by pulling the section it names into the outline. When
#: the body never defines one, this says so in one line. It is a SUBSTITUTION and never a refusal:
#: `ask_citations` may exit 2 on a `§` it cannot show because the fix is one edit to a sentence the
#: assistant wrote, but this reference lives in someone else's PR body — refusing would cost the merge and the
#: only repair is editing another author's description. **A merge picker that fails to send is a
#: green functionality PR that never gets approved.** `pr_digest`'s docstring carries the full
#: argument, including why only the worded family is ever named here.
UNEXPLAINED_HEADER = "Named but not defined anywhere in the description:"
#: How many are named before it stops. Two is enough to show the shape of the gap; a list of eight
#: is annotation noise.
UNEXPLAINED_MAX = 2

#: **THE OVERLAP BLOCK — WHICH OTHER OPEN PR CHANGES THE SAME FILES, AND NOTHING MORE THAN THAT.**
#:
#: `../docs/concurrent-pr-collisions-spec.md` phase 1. When one PR merges and another that touches
#: the same files goes `CONFLICTING` seconds later, a picker that named only its own blockers gave the
#: owner no way to know the other existed — and an approval taken just before the sibling merged is
#: spent on a head the required rebase then destroys, even when the change itself did not change.
#:
#: **It states the overlap. It does not predict the conflict**, and that wording is the whole design
#: rather than a caution: a shared path frequently auto-merges on its own (a router index is the
#: common case), so a block that predicted conflicts would be wrong about exactly the files it names.
#:
#: **And it says nothing about what the owner should do.** No ordering suggestion, no *"merge this
#: one first"*, no third option, no second question. Ranking by diff size was measured and made no
#: difference; a suggestion resting on a negative result does not get promoted to advice by being
#: implemented.
OVERLAP_HEADER = "Also open, changing some of the same files:"
#: How many other PRs are named, and how many shared paths each. Three and four — the same order as
#: :func:`blocker_line`'s cap, for the same reason: a picker that scrolls is a picker tapped without
#: reading. **Whatever is dropped is named**, per this directory's no-silent-caps habit.
OVERLAP_MAX_PRS = 3
OVERLAP_MAX_PATHS = 4
#: A character budget on one entry's path list, applied *before* the count cap so four very long
#: paths cannot spend the block. Overflow is named by :data:`OVERLAP_PATHS_MORE` either way, so this
#: never truncates silently. One path is always shown: a shared-file line naming no file is noise.
OVERLAP_PATHS_CHARS = 150
#: The other PR's title, clipped with :func:`_clip`'s visible ellipsis. Shorter than
#: :data:`TITLE_CHARS` — this is the PR the owner is *not* being asked about.
OVERLAP_TITLE_CHARS = 72
OVERLAP_PATHS_MORE = " (+%d more)"
OVERLAP_MORE = "• (+%d more open PR%s overlap%s)"
#: The whole block's ceiling, and it is a **backstop the caps above already satisfy** rather than the
#: thing that shapes the block. **Derived from a measurement, not chosen**: the structural worst case
#: — three seven-digit PR numbers, all drafts, titles past :data:`OVERLAP_TITLE_CHARS`, first shared
#: paths long enough to be clipped at :data:`OVERLAP_PATHS_CHARS` with more still named, and a
#: `(+96 more open PRs overlap)` line — renders at **840 characters**. 900 is that with headroom for
#: one of the caps above to grow a little without silently turning the block off.
#: `OverlapBandIsBoundedTest` re-derives the 840 every run, so this cannot rot into a guess.
OVERLAP_CHARS = 900

#: The label above the link. See :func:`pr_link_line` for why the URL goes out bare.
LINK_HEADER = "Read it in full:"

POLICY_FILE = "seneschal/references/autonomy-policy.md"
#: A verbatim phrase from :data:`POLICY_FILE` — `PolicyQuoteIsVerbatimTest` asserts it occurs there,
#: so the refusal can never quote a rule the policy no longer states.
POLICY_QUOTE = '"Merge a DOCS-ONLY pull request once CI is green"'

#: `gh pr merge` flags that consume the NEXT token. Anything not in here and not in
#: :data:`BOOLEAN_FLAGS` is unrecognised, and an unrecognised flag is a DENY — see
#: :func:`parse_gh_pr_merge`.
VALUE_FLAGS = frozenset({
    "-b", "--body", "-F", "--body-file", "-t", "--subject", "-A", "--author-email",
    "--match-head-commit", "-R", "--repo",
})
BOOLEAN_FLAGS = frozenset({
    "--admin", "--auto", "--disable-auto", "-d", "--delete-branch",
    "-m", "--merge", "-r", "--rebase", "-s", "--squash", "-h", "--help",
})

_PULL_URL_RE = re.compile(r"github\.com/[^/\s]+/[^/\s]+/pull/(\d+)", re.IGNORECASE)
#: The same URL, read for the OTHER half — which repository the number belongs to.
_PULL_REPO_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/pull/\d+", re.IGNORECASE)
#: The REST merge endpoints, as `gh api` spells them — stage 1's cheap "is this one?" test. Both the
#: synchronous `…/pulls/N/merge` and the asynchronous `…/pulls/N/merge-async` (GitHub's REQUIRED
#: door for a stacked PR — see :func:`parse_gh_api_merge`).
_API_MERGE_RE = re.compile(r"/pulls/(\d+)/merge(?:-async)?\b", re.IGNORECASE)
#: The same endpoint read for ALL of its parts — owner, repo, number and which verb. Anchored on the
#: whole token: `repos/o/r/pulls/N/merge`, with or without a leading slash, or a full
#: `https://api.github.com/…` URL. **The repository comes off this path, never off the directory the
#: command was typed in** — otherwise a `gh api` merge of another repository's #26, typed in this
#: checkout, would be judged as this checkout's #26.
_API_MERGE_PATH_RE = re.compile(
    r"^(?:https?://[^/\s]+/)?/?repos/([^/\s]+)/([^/\s]+)/pulls/(\d+)/(merge|merge-async)/?$",
    re.IGNORECASE)
#: `gh api` flags that consume the next token. Anything not here or in :data:`API_BOOLEAN_FLAGS` is
#: refused, for :func:`parse_gh_pr_merge`'s reason: a guessed arity reads the endpoint off the wrong
#: token. `-f`/`-F` and `--input` are handled on their own because their VALUES are read.
API_VALUE_FLAGS = frozenset({
    "-X", "--method", "-H", "--header", "-q", "--jq", "-t", "--template", "--hostname",
    "--cache", "-p", "--preview", "-R", "--repo",
})
API_BOOLEAN_FLAGS = frozenset({"-i", "--include", "--paginate", "--silent", "--verbose",
                               "--slurp", "-h", "--help"})
API_FIELD_FLAGS = frozenset({"-f", "--raw-field", "-F", "--field"})
#: The only `merge_method` the REST door may carry. The framework's rule is merge commits, never
#: squash or rebase (`autonomy-policy.md`); the REST endpoint is one more door, so it holds that rule.
API_MERGE_METHOD = "merge"
#: The GraphQL mutation. No number is reliably parseable out of a GraphQL body, so this is a
#: DENY on sight rather than something the guard tries to classify.
_GRAPHQL_MERGE_RE = re.compile(r"\bmergePullRequest\b")

#: Shell separators a segment cannot cross. Naive by design: a mis-split can only ever create an
#: extra candidate segment, which is the safe direction.
_SEGMENT_SPLIT_RE = re.compile(r"(?:\|\||&&|[;\n|])")

#: **A redirection is the SHELL's, not `gh`'s — it never reaches argv.** `shlex` has no idea what a
#: redirection operator is, so `gh pr merge 454 --merge 2>&1` lexes `2>&1` as an ordinary token and
#: the positional extractor counted it as a second PR argument. See :func:`strip_redirections`.
#: The POSIX operator set (`<`, `>`, `>|`, `>>`, `<>`, `<&`, `>&`, `<<`, `<<-`), each with an
#: optional leading file-descriptor number; plus bash's `&>` / `&>>` and PowerShell's `*>` / `*>>`,
#: because :data:`GUARDED_TOOLS` covers both shells. Longest spelling first within each family.
_REDIRECT_TOKEN_RE = re.compile(r"""
    ^(?:
        &>> | &>                                        # bash: both streams at once
      | (?:\d+|\*)?                                     # fd number; `*` = PowerShell all-streams
        (?: >> | >\| | >& | > | <<- | << | <> | <& | < )
    )
""", re.VERBOSE)

# ---- the PR body, which is TEXT FROM OUTSIDE THIS PROCESS. See :func:`summarize_pr_body`. ----

#: Everything from here down is machine-appended noise rather than description. Matched at a line
#: start; the FIRST match ends the body, because a footer never has content after it.
_BODY_FOOTER_RE = re.compile(
    r"^\s*(?:\**(?:\U0001F916\s*)?Generated with\b|Co-[Aa]uthored-[Bb]y:)", re.MULTILINE)
#: HTML comments. A PR template hides its instructions in these, and they are never a description.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
#: A fenced code block, dropped whole. A pasted diff or log is the largest thing a body can carry and
#: the least likely to answer *"what is this?"*. An UNCLOSED fence eats to the end deliberately: half
#: a fence is exactly what would otherwise survive truncation.
_FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,}).*?(?:^[ ]{0,3}\1`*[ \t]*$|\Z)",
                       re.MULTILINE | re.DOTALL)
#: `- [ ]` / `* [x]` — checklist boilerplate from a PR template, never a description of the change.
_CHECKBOX_RE = re.compile(r"^[ \t]*[-*+][ \t]+\[[ xX]\][ \t]*.*$", re.MULTILINE)
#: An ATX heading or a horizontal rule — where the lead-in section ends.
_SECTION_BREAK_RE = re.compile(r"^(?:[ ]{0,3}#{1,6}[ \t]|[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$)")
#: **`1. ` AT A LINE START IS THE EXACT SHAPE `telegram_ask.render_body` GIVES AN OPTION.** A PR body
#: may not wear the picker's own clothes. Rewritten to a bullet rather than dropped — the content is
#: fine, the costume is not.
_OPTION_SHAPED_RE = re.compile(r"^([ \t]*)\d{1,2}[.)][ \t]+", re.MULTILINE)
#: Control characters, which a body has no business carrying into a Telegram message.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: Three or more blank lines collapse to one. Vertical whitespace is budget spent on nothing.
_BLANK_RUN_RE = re.compile(r"\n{3,}")
#: The canonical GitHub pull-request URL this module is willing to put in front of the owner. Rebuilt from
#: `(repo, number)` — both already validated — rather than forwarded from `gh`'s string.
_SAFE_PR_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/\d+$")


class GuardError(Exception):
    """A stage-2 check that could not be satisfied. Always a DENY; the text is the reason."""


# --------------------------------------------------------------------------- stage 1: detection

def _strip_exe(token: str) -> str:
    """`C:/Program Files/GitHub CLI/gh.exe` -> `gh`. A guard keyed on the literal string `gh` is one
    absolute path away from not firing."""
    base = os.path.basename((token or "").strip().strip("'\"").replace("\\", "/"))
    return base[:-4].lower() if base.lower().endswith(".exe") else base.lower()


def _tokens(segment: str) -> list:
    """One shell segment -> tokens, tolerantly. `shlex` first (it understands quoting); a whitespace
    split as the fallback, because a segment we cannot lex is still a segment we must look at."""
    try:
        toks = shlex.split(segment, posix=True)
    except ValueError:
        toks = [t.strip("'\"") for t in segment.split()]
    # PowerShell's call operator, and a bare `&` from a mis-split chain.
    while toks and toks[0] in ("&", "&&"):
        toks = toks[1:]
    return toks


#: Shells whose `-c` / `-Command` argument is another command, not data. A merge one level down is
#: still a merge, and `bash -c "gh pr merge 406"` is a spelling an agent reaches for by habit rather
#: than by evasion — which is exactly the class this guard exists to catch.
_NESTED_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "pwsh", "powershell", "cmd"})
_NESTED_FLAGS = frozenset({"-c", "-Command", "-command", "/c", "/C", "-EncodedCommand"})
#: How far the nesting is followed. `bash -c "pwsh -Command '…'"` is two; past a few levels the
#: intent is no longer habit, and the depth cap keeps a pathological string from spinning.
_MAX_NEST_DEPTH = 3

#: **Public aliases, for the other hooks that must not keep a second copy of this vocabulary.**
#: `script_file_guard.py` normalises a program name the same way and needs the same idea of which
#: flag carries a command; a second spelling of either drifts, and the drift shows up as one guard
#: firing on a form the other misses — the argument that made :func:`command_segments`' `heads` a
#: parameter, one level down. Aliases rather than a rename so nothing in this module moves.
#: `script_file_guard`'s own sets are deliberately **wider** (it needs `-lc`, which no `gh` or `git`
#: invocation uses), and its `VocabularyCoverageTest` asserts they stay supersets of these.
strip_exe = _strip_exe
NESTED_SHELLS = _NESTED_SHELLS
NESTED_FLAGS = _NESTED_FLAGS


def command_segments(command: str, heads=("gh",), _depth: int = 0) -> list:
    """Every segment of `command` whose FIRST token is one of `heads`, tokenized — following
    `bash -c` and `pwsh -Command` into the command they carry.

    Command position is the point: `grep "gh pr merge" notes.md` and `echo "... gh pr merge ..."`
    have `grep`/`echo` first and are not merges. This is the same self-immunity
    `bash_path_guard`'s `test_the_message_does_not_match_its_own_rule` pins — a guard that refuses
    the commands used to write about it is a guard that gets uninstalled.

    **`-EncodedCommand` is deliberately NOT decoded.** Base64-decoding it to look for `gh` would be
    the guard learning to read obfuscation, which is a losing game and not the threat model — the
    thing being prevented is an agent's misjudgment, not an adversary. It is listed here only so the
    nesting walk does not mistake the payload for a positional.

    **`heads` is a parameter because there is a SECOND hook, not because it is configurable.**
    `branch_delete_guard.py` needs the identical walk over `git` segments (`git push origin --delete`
    is a branch deletion with no `gh` anywhere in it), and the alternative was a second copy of the
    nesting walk, the `.exe` stripping and the segment split. Those would then drift, and the drift
    would show up as one guard firing on a spelling the other misses — which is the failure mode both
    of them exist to remove. :func:`gh_segments` below is this function at its default and stays the
    only spelling `merge_guard`'s own paths use."""
    wanted = frozenset(heads)
    out = []
    for segment in _SEGMENT_SPLIT_RE.split(command or ""):
        toks = _tokens(segment)
        if not toks:
            continue
        head = _strip_exe(toks[0])
        if head in wanted:
            out.append(toks)
        elif head in _NESTED_SHELLS and _depth < _MAX_NEST_DEPTH:
            for i, tok in enumerate(toks[1:], start=1):
                if tok in _NESTED_FLAGS and i + 1 < len(toks):
                    out.extend(command_segments(toks[i + 1], heads, _depth + 1))
    return out


def gh_segments(command: str, _depth: int = 0) -> list:
    """:func:`command_segments` restricted to `gh`. Every `merge_guard` path goes through this
    spelling, so widening the walk for another caller cannot widen what this module detects."""
    return command_segments(command, ("gh",), _depth)


def merge_invocations(command: str) -> list:
    """The `gh` segments in `command` that are merges. `[]` means stage 2 never runs.

    Three shapes, because two of them are otherwise straight bypasses of the first:
      * `gh pr merge …`      — the ordinary one.
      * `gh api …/pulls/N/merge …` — the REST endpoint. Same act, different spelling.
      * `gh api graphql … mergePullRequest …` — likewise, and unparseable, so it is DENY on sight.
    """
    found = []
    for toks in gh_segments(command):
        rest = toks[1:]
        joined = " ".join(rest)
        if _GRAPHQL_MERGE_RE.search(joined):
            found.append({"kind": "graphql", "tokens": toks})
            continue
        if "api" in rest and _API_MERGE_RE.search(joined):
            if not _explicit_api_read(rest):
                found.append({"kind": "api", "tokens": toks})
            continue
        try:
            pr_at = rest.index("pr")
        except ValueError:
            continue
        if "merge" in rest[pr_at + 1:]:
            found.append({"kind": "pr-merge", "tokens": toks})
    return found


def _explicit_api_read(rest: list) -> bool:
    """`gh api -X GET …/pulls/N/merge` with no body — GitHub's read-only *"is this PR merged?"* — is
    not a merge. **Only an EXPLICIT `GET` counts, and any field or `--input` voids it**: stage 1 fails
    open, so this may only ever recognise the one unmistakable read, never infer `gh`'s implied
    method (which flips to POST the moment a field appears)."""
    method = None
    for i, tok in enumerate(rest):
        if tok in ("-X", "--method") and i + 1 < len(rest):
            method = rest[i + 1]
        elif tok.startswith("--method="):
            method = tok.split("=", 1)[1]
        elif tok.startswith("-X") and len(tok) > 2:
            method = tok[2:]
        elif tok.split("=", 1)[0] in API_FIELD_FLAGS or tok.split("=", 1)[0] == "--input" \
                or (tok[:2] in ("-f", "-F") and len(tok) > 2 and not tok.startswith("--")):
            return False
    return str(method or "").upper() == "GET"


def looks_like_merge(command: str) -> bool:
    """Stage 1's whole question. No I/O, no subprocess, no disk — so the fail-open wrapper around it
    has almost nothing to catch, which is what makes stage 2 safe to make strict."""
    return bool(merge_invocations(command))


# --------------------------------------------------------------------------- stage 1.5: parsing

def strip_redirections(tokens: list) -> list:
    """Drop the shell's redirections from one segment's argument tokens.

    Without this, `gh pr merge 454 --repo <this repo> --merge 2>&1` is refused with *"more than one
    PR argument: ['454', '2>&1']"* — a legitimate, approved merge blocked until the command is
    retyped without the redirect. **A guard that blocks correct merges teaches everyone to route
    around it, and nobody routing around it is this guard's entire value** — so this is a
    correctness property, not polish.

    The cause is a seam, not a typo. This module reads command **TEXT** (deliberately — see the
    module docstring), and `shlex` lexes words, not shell grammar: it has no notion of a redirection
    operator, so `2>&1` arrives as an ordinary token and lands in the positional list. But a
    redirection is consumed by the *shell*; `gh` never sees one in `argv`. Removing them here makes
    the guard's view of the arguments match `gh`'s.

    **This may not lower the gate, and it does not.** Two properties are load-bearing:

      * **Genuine ambiguity still fails CLOSED.** Stripping only ever removes tokens a shell would
        also have removed. `gh pr merge 5 6` has no redirections, so it is untouched and still
        refused; `gh pr merge 5 6 2>&1` still leaves two PR arguments.
      * **A redirect cannot swallow a second merge.** This runs inside :func:`parse_gh_pr_merge`, on
        the tokens of ONE already-identified segment. Segmentation and detection
        (:func:`gh_segments`, :func:`merge_invocations`) are upstream and untouched, so
        `gh pr merge 5 2>&1 && gh pr merge 6` is still two merge invocations, both judged.
        `RedirectionTokensTest` pins that explicitly.

    A **bare** operator (`2>`, `>`, `>&`) takes the FOLLOWING token as its target, so both are
    dropped — a filename is not a PR argument either. A **glued** one (`2>&1`, `>out.log`) carries
    its own target and drops alone. That is the shell's own rule, which is why
    `gh pr merge 5 > 6` and `gh pr merge 5 >6` both read as PR 5 here exactly as they do in `sh`."""
    out, i = [], 0
    while i < len(tokens):
        m = _REDIRECT_TOKEN_RE.match(tokens[i] or "")
        if not m:
            out.append(tokens[i])
            i += 1
        elif m.end() == len(tokens[i]):
            i += 2      # bare operator — the next token is the target it redirects to
        else:
            i += 1      # glued operator — `2>&1`, `>out.log`; the target came with it
    return out


def _positional_pr(token: str):
    """A `gh pr merge` positional -> a PR number, or None for a branch name.

    A branch name is deliberately *not* resolved by asking `gh` what PR is on it. The guard's answer
    then depends on the shell's working directory and on which branch happens to be checked out,
    neither of which is in the command it is judging. Requiring the number typed out costs one word,
    makes every merge in this repo self-documenting, and is the difference between a guard that reads
    the command and a guard that guesses the intent."""
    token = (token or "").strip().strip("'\"")
    if token.isdigit():
        return int(token)
    m = _PULL_URL_RE.search(token)
    return int(m.group(1)) if m else None


def parse_gh_pr_merge(tokens: list) -> dict:
    """`gh pr merge` tokens -> `{"pr": int, "repo": str|None, "match_head": str|None}`. Raises
    :class:`GuardError` — i.e. DENY — for anything it cannot read with certainty.

    `match_head` is the value of `--match-head-commit`, read but not judged here: whether it is a
    SHA GitHub would accept is :func:`match_head_refusal`'s question, and it needs the PR's real
    head to answer it.

    **An unrecognised flag is a refusal, and that is soundness rather than pedantry.** If an unknown
    flag silently consumed no value, `gh pr merge --some-new-flag 406` would read `406` as the PR
    while `gh` read it as the flag's argument and merged the *current branch's* PR instead. The guard
    would then classify a completely different pull request — and could ALLOW on its diff. There is
    no safe guess available here, so there is no guess.

    Shell redirections are removed before anything is read, because the shell removes them before
    `gh` is ever executed — :func:`strip_redirections` has the argument."""
    rest = tokens[1:]
    try:
        after = rest[rest.index("pr") + 1:]
    except ValueError:  # unreachable via merge_invocations; belt and braces
        raise GuardError("this looked like a merge but has no `pr` subcommand")
    after = after[after.index("merge") + 1:] if "merge" in after else []
    # Before flags OR positionals are read: a redirection is never either one. Doing it here rather
    # than in the positional branch also means a value-flag can never eat an operator as its value.
    after = strip_redirections(after)

    positionals, repo, match_head, i = [], None, None, 0
    while i < len(after):
        tok = after[i]
        if tok == "--":
            positionals.extend(after[i + 1:])
            break
        if tok.startswith("-") and tok != "-":
            name, eq, value = tok.partition("=")
            if name in VALUE_FLAGS:
                if not eq:
                    if i + 1 >= len(after):
                        raise GuardError(f"flag {name} is missing its value")
                    value = after[i + 1]
                    i += 1
                if name in ("-R", "--repo"):
                    repo = value
                elif name == "--match-head-commit":
                    match_head = value
            elif name not in BOOLEAN_FLAGS:
                raise GuardError(
                    f"unrecognised flag {name!r} on `gh pr merge`. The guard refuses rather than "
                    f"guess whether it takes a value: guessing wrong would make it read the PR "
                    f"number off the wrong token and judge a different pull request entirely. "
                    f"Re-run naming the PR number explicitly and without this flag, or ask the owner.")
        else:
            positionals.append(tok)
        i += 1

    if not positionals:
        raise GuardError(
            "no PR number in the command. A bare `gh pr merge` means \"whatever PR is on the "
            "branch this shell happens to have checked out\", which is not in the command and is "
            "not something the guard will infer. Name it: `gh pr merge 407 --merge`.")
    if len(positionals) > 1:
        raise GuardError(f"more than one PR argument: {positionals!r}")
    pr = _positional_pr(positionals[0])
    if pr is None:
        raise GuardError(
            f"{positionals[0]!r} is not a PR number or pull-request URL. A branch name is not "
            f"resolved here — which PR it points at depends on state outside the command. "
            f"Name the number: `gh pr merge 407 --merge`.")
    return {"pr": pr, "repo": repo, "match_head": match_head}


def parse_invocation(inv: dict) -> dict:
    """One entry from :func:`merge_invocations` -> `{"pr", "repo", "match_head"}`, or raise
    (DENY)."""
    kind = inv.get("kind")
    if kind == "graphql":
        raise GuardError(
            "this is a GraphQL `mergePullRequest` mutation. No PR number can be read out of a "
            "GraphQL body with confidence, so the guard refuses it outright rather than judging a "
            "PR it may have mis-identified. Use `gh pr merge <number>` and it will be classified.")
    tokens = inv.get("tokens") or []
    if kind == "api":
        return parse_gh_api_merge(tokens)
    return parse_gh_pr_merge(tokens)


def _api_flag_value(after: list, i: int, name: str, eq: str, value: str, glued: str) -> tuple:
    """`(value, next_i)` for a `gh api` flag at `after[i]`: `--f=v`, `-fv` (glued) or `-f v`."""
    if eq:
        return value, i + 1
    if glued:
        return glued, i + 1
    if i + 1 >= len(after):
        raise GuardError(f"`gh api` flag {name} is missing its value")
    return after[i + 1], i + 2


def parse_gh_api_merge(tokens: list) -> dict:
    """`gh api … repos/<o>/<r>/pulls/<n>/merge[-async] …` -> the same shape
    :func:`parse_gh_pr_merge` returns, plus what the REST door needs judged. Raises
    :class:`GuardError` (DENY) on anything it cannot read with certainty.

    `gh api -X PUT repos/<other>/<repo>/pulls/26/merge -f merge_method=merge -f sha=…` typed in this
    checkout names no `-R` (which `gh api` does not even take); a reader that fell through to
    `origin` would fetch **this** checkout's #26 and refuse on its paths. The endpoint names its
    repository in the path, as `gh` itself reads it; so does this, and a path the guard cannot read
    (a `{owner}`
    placeholder `gh` fills from the current directory, a second positional, a body from `--input`)
    is a refusal rather than a fallback to the directory.

    What it returns beyond `pr`/`repo`:

      * `match_head` — the `sha` field. **Required on this door** (`require_match_head`): the REST
        call's only binding to the approved artifact is that field, and :func:`decide_command`
        refuses its absence with the full head printed, before anything is spent.
      * `endpoint` — `"merge"` or `"merge-async"`. The async one merges every PR *below* this one in
        a stack too, which :func:`stack_refusal` guards.
      * `merge_method` — must be exactly :data:`API_MERGE_METHOD`; squash, rebase and absent are
        refused here (the framework's merge-commit rule, which the REST door holds too).
      * `auto` — always False; kept so the refund path reads one shape for both doors."""
    rest = tokens[1:]
    try:
        after = strip_redirections(rest[rest.index("api") + 1:])
    except ValueError:  # unreachable via merge_invocations; belt and braces
        raise GuardError("this looked like a `gh api` merge but has no `api` subcommand")

    positionals, fields, repo_flag, i = [], [], None, 0
    while i < len(after):
        tok = after[i]
        if tok == "--":
            positionals.extend(after[i + 1:])
            break
        if tok.startswith("-") and tok != "-":
            name, eq, value = tok.partition("=")
            glued = ""
            if not name.startswith("--") and len(name) > 2:
                # `-XPUT`, `-fsha=…` — a short flag with its value glued on.
                name, glued, eq, value = name[:2], tok[2:], "", ""
            if name == "--input":
                raise GuardError(
                    "this `gh api` merge reads its body from `--input`, which the guard cannot see, "
                    "so it cannot tell which head the merge is bound to. Pass the fields with -f: "
                    "`-f merge_method=merge -f sha=<full head>`.")
            if name in API_FIELD_FLAGS:
                raw, i = _api_flag_value(after, i, name, eq, value, glued)
                key, sep, val = raw.partition("=")
                if not sep:
                    raise GuardError(f"`gh api {name} {raw}` is not key=value")
                if name in ("-F", "--field") and val.startswith("@"):
                    raise GuardError(f"`{name} {raw}` reads its value from a file the guard cannot "
                                     f"see. Spell the value out with -f.")
                fields.append((key.strip(), val.strip()))
                continue
            if name in API_VALUE_FLAGS:
                val, i = _api_flag_value(after, i, name, eq, value, glued)
                if name in ("-R", "--repo"):
                    repo_flag = val
                continue
            if name in API_BOOLEAN_FLAGS and not glued:
                i += 1
                continue
            raise GuardError(
                f"unrecognised flag {name!r} on a `gh api` merge. The guard refuses rather than "
                f"guess whether it takes a value, because a wrong guess reads the endpoint off the "
                f"wrong token. Re-run without it, or ask the owner.")
        positionals.append(tok)
        i += 1

    if len(positionals) != 1:
        raise GuardError(f"a `gh api` merge needs exactly one endpoint; this has {positionals!r}")
    endpoint = positionals[0].strip().strip("'\"")
    if "{" in endpoint:
        raise GuardError(
            f"the endpoint {endpoint!r} uses a placeholder, which `gh` fills from the directory "
            f"the command runs in. A PR number is not an identity, so the guard does not guess: "
            f"spell the repository out — repos/<owner>/<name>/pulls/<n>/merge.")
    m = _API_MERGE_PATH_RE.match(endpoint)
    if not m:
        raise GuardError(f"{endpoint!r} is not a readable repos/<owner>/<name>/pulls/<n>/merge "
                         f"path, so the guard cannot tell which pull request this merges")
    repo = f"{m.group(1)}/{m.group(2)}"
    if repo_flag and normalize_repo(repo_flag).lower() != repo.lower():
        raise GuardError(f"the command names two repositories ({repo_flag!r} and the path's "
                         f"{repo!r}); refusing rather than picking one")

    shas = [v for k, v in fields if k == "sha"]
    methods = [v for k, v in fields if k == "merge_method"]
    if len(shas) > 1 or len(methods) > 1:
        raise GuardError("a `sha` or `merge_method` field is given twice; `gh` sends one of them "
                         "and the guard cannot tell which")
    method = methods[0] if methods else None
    return {"pr": int(m.group(3)), "repo": repo, "match_head": shas[0] if shas else None,
            "require_match_head": True, "endpoint": m.group(4).lower(),
            "merge_method": method, "auto": False}


def api_merge_refusal(parsed: dict, facts: dict) -> str:
    """`""` unless this is a `gh api` merge whose FIELDS would spend an approval on the wrong thing.
    Judged after `gh` has answered, like :func:`match_head_refusal`, so the refusal can print the
    full head to copy — and, like it, **above the spend**.

    Two refusals: a missing `sha` (the REST call's only binding to the approved artifact; the async
    endpoint otherwise binds to *whatever the head is when the merge runs*), and a `merge_method`
    other than `merge` (the framework's merge-commit rule)."""
    if not parsed.get("require_match_head"):
        return ""
    method = parsed.get("merge_method")
    if method != API_MERGE_METHOD:
        said = f"`merge_method={method}`" if method else "no `merge_method`"
        return (f"this `gh api` merge carries {said}. The standing rule is merge commits only "
                f"— never squash, never rebase — so the REST door requires `-f merge_method=merge`")
    if parsed.get("match_head") is None:
        return (f"a `gh api` merge must carry `-f sha=<full head>` — it is the only thing binding "
                f"the REST call to the head the owner approved — full head: {facts.get('head_sha')}")
    return ""


# --------------------------------------------------------------------------- stage 2: the PR

def _run(argv: list, cwd=None) -> tuple:
    """`(returncode, stdout, stderr)`. The one subprocess call, so tests replace one seam."""
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, cwd=cwd or None)
    return proc.returncode, proc.stdout, proc.stderr


#: **`body` and `baseRefName` are here for the PICKER, not for the decision.** Nothing the guard
#: allows or denies turns on either — they exist so the question the owner is asked says *what the
#: change is* and stops claiming a deploy in repositories that have none. Keep that separation: a
#: field that only the message reads may never grow into a field the verdict reads.
#:
#: **`mergedAt`/`closedAt` are the same kind of field**: they exist so a retired picker can say
#: *when* the thing it was asking about stopped being askable, and nothing decides anything on them.
#:
#: **`mergeStateStatus` is NOT the same kind of field — it IS read by the decision.** A base branch
#: protected with `required_status_checks.strict` requires a branch be up to date with its base
#: before GitHub will merge it, and a BEHIND head has no textual conflict — `gh` would answer this PR
#: mergeable with nothing else here saying otherwise. See :func:`behind_refusal`.
#:
#: **`statusCheckRollup` is read by the decision too — never merge red or pending.** See
#: :func:`ci_refusal`: a merge (docs-only included) is refused unless every check on the head has
#: finished and passed.
PR_FIELDS = ("number,files,headRefOid,state,title,url,body,baseRefName,mergedAt,closedAt,"
             "mergeStateStatus,statusCheckRollup")


def pr_facts(pr: int, repo=None, cwd=None, runner=None) -> dict:
    """`gh pr view <pr> --json …` ->
    `{"pr", "repo", "paths", "head_sha", "state", "title", "url", "base", "body"}`.

    **Every unhappy path raises**, which is every unhappy path DENYing: `gh` absent, `gh` non-zero
    (not logged in, no network, no such PR), output that is not JSON, a `number` that is not the one
    we asked about, a missing `headRefOid`, a `url` no repository can be read out of, or an empty
    file list. A pull request with no files is not a docs-only pull request; it is a response the
    guard does not understand.

    **`repo` comes from `gh`'s ANSWER, never from the command**, and that is the whole reason it is
    here rather than in :func:`parse_gh_pr_merge`. `gh pr merge 45` carries no repository at all —
    which repo it means depends on the shell's working directory, exactly the state-outside-the-
    command this guard refuses to infer everywhere else. The `url` on the response is the base repo
    of the PR whose head SHA and file list are in the same object, so keying an approval on it can
    never file the record under a different pull request than the one that was classified.

    **`url`, `base` and `body` are the exception to "every unhappy path raises", deliberately.** They
    feed the *message* — the tappable link, the deploy sentence, the summary — and not the verdict.
    A PR with an empty description is an ordinary PR; refusing to classify it because the picker
    would be terser is the guard failing closed on the wrong question. So they degrade to `""` and
    the picker degrades with them. (`url` still raises via :func:`repo_from_url` below, because the
    *repository* is read out of it and that one is load-bearing.)"""
    runner = runner or _run
    argv = ["gh", "pr", "view", str(pr), "--json", PR_FIELDS]
    if repo:
        argv += ["--repo", repo]
    try:
        code, out, err = runner(argv, cwd)
    except FileNotFoundError:
        raise GuardError("`gh` is not on PATH, so the diff cannot be classified")
    except subprocess.TimeoutExpired:
        raise GuardError("`gh pr view` timed out, so the diff cannot be classified")
    except Exception as e:  # noqa: BLE001 — any transport failure is a failure to classify
        raise GuardError(f"`gh pr view` failed: {e}")
    if code != 0:
        raise GuardError(f"`gh pr view {pr}` exited {code}: {(err or out or '').strip()[:300]}")
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        raise GuardError(f"`gh pr view {pr}` did not return JSON: {(out or '').strip()[:200]!r}")
    if not isinstance(data, dict):
        raise GuardError(f"`gh pr view {pr}` returned {type(data).__name__}, not an object")

    number = data.get("number")
    if number != pr:
        raise GuardError(
            f"PR number mismatch: asked about #{pr}, `gh` answered about #{number!r}. Refusing "
            f"rather than judging one pull request and merging another.")
    head = data.get("headRefOid")
    if not isinstance(head, str) or not head.strip():
        raise GuardError(f"PR #{pr} has no headRefOid, so an approval could not be bound to it")
    slug = repo_from_url(data.get("url"))
    if not slug:
        raise GuardError(
            f"PR #{pr} came back with no readable repository URL ({data.get('url')!r}), so an "
            f"approval could not be filed against a repository. #{pr} is a number in some repo, "
            f"not an identity — refusing rather than guessing which one.")

    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise GuardError(
            f"PR #{pr} reports no changed files. That is not a docs-only PR — it is a response "
            f"this guard does not understand, and 'cannot tell' is never 'allow'.")
    paths = []
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else entry
        if not isinstance(path, str) or not path.strip():
            raise GuardError(f"PR #{pr} has a changed file with no readable path: {entry!r}")
        paths.append(path.strip())
    return {"pr": pr, "repo": slug, "paths": paths, "head_sha": head.strip(),
            "state": data.get("state"), "title": data.get("title") or "",
            "url": data.get("url") or "", "base": data.get("baseRefName") or "",
            "body": data.get("body") or "",
            # Message-only, and degrading to "" exactly as `url`/`base`/`body` do. `gh` answers
            # `null` for whichever of the two does not apply, and both are null on an OPEN PR.
            "merged_at": data.get("mergedAt") or "", "closed_at": data.get("closedAt") or "",
            # NOT message-only — :func:`behind_refusal` reads this one. Degrades to "" like the
            # message-only fields above rather than raising, because an old `gh` or a transient
            # GraphQL miss on this one field must not turn every PR classification into a DENY.
            "merge_state_status": data.get("mergeStateStatus") or "",
            # Read by :func:`ci_refusal`. ABSENT (an old `gh`, a response without the field) is
            # `None` — *unreadable*, which that function refuses; `null` is `gh`'s spelling of "no
            # checks attached" and folds into the empty list, which it also refuses. Never raises
            # here, so a PR with no CI still classifies and `check` can say why it would be blocked.
            "ci_nodes": (list(data.get("statusCheckRollup") or [])
                         if "statusCheckRollup" in data
                         and isinstance(data.get("statusCheckRollup") or [], list) else None)}


def is_prompt_path(path: str) -> bool:
    """**Is this `.md` prose that RUNS?** :data:`PROMPT_PATHS`, :data:`PROMPT_DIRS` **or**
    :data:`AGENT_INSTRUCTION_GLOBS`, minus :data:`PROMPT_PATH_EXEMPT`.

    Lowercased and forward-slashed first, so `SENESCHAL\\MODES\\Chat.md` classifies the same as
    `seneschal/modes/chat.md` — the widening direction, which is the safe one here.

    The two match kinds are read in one pass and neither can raise: a split of a string is total,
    and the exemption is checked before both so a hole stays a hole."""
    norm = (path or "").replace("\\", "/").strip().lower()
    if not norm or norm in PROMPT_PATH_EXEMPT:
        return False
    segments = norm.split("/")
    # A directory NAME, at any depth — the file's own name is never a directory, so `references.md`
    # in an ordinary folder does not match while `a/b/references/c.md` does.
    for segment in segments[:-1]:
        if segment in PROMPT_DIRS:
            return True
    base = segments[-1]
    for prefix, name in PROMPT_PATHS:
        if norm.startswith(prefix) and (not name or base == name):
            return True
    return any(rx.match(norm) for rx in _AGENT_INSTRUCTION_RES)


def _classify_path(path) -> tuple:
    """`(normalised path, is it prose that does NOT run?)`.

    **THIS CANNOT RAISE, AND WHAT IT CANNOT CLASSIFY IS NOT DOCS.** Stage 2 fails closed absolutely,
    and this module is also a hook on every shell call on the machine — so the one thing a
    classification bug may never do is turn into an allow. A path this cannot read is a blocker."""
    try:
        norm = (path or "").replace("\\", "/").strip()
    except Exception:  # noqa: BLE001 — a path that is not even a string is not a docs path
        return repr(path), False
    try:
        if norm in DOCS_ONLY_ALLOWLIST:
            return norm, True
        return norm, norm.lower().endswith(".md") and not is_prompt_path(norm)
    except Exception:  # noqa: BLE001
        return norm, False


def non_docs_paths(paths) -> list:
    """The paths that make a PR ask-high. Empty list == docs-only.

    `*.md` or the one allowlist entry — **except an `.md` that is itself the prompt**
    (:data:`PROMPT_PATHS` + :data:`PROMPT_DIRS` + :data:`AGENT_INSTRUCTION_GLOBS`). Extension match
    is case-insensitive
    (`README.MD`), and the comparison is on the forward-slashed path `gh` returns."""
    return [norm for norm, docs in (_classify_path(p) for p in paths) if not docs]


def prompt_paths(paths) -> list:
    """The blockers that are **prose that runs**, in blocker order. Always a subset of
    :func:`non_docs_paths`, because it filters that function's own output rather than re-deriving
    one — the same no-second-classifier rule the rest of this module lives by.

    It exists only to make the picker's sentence true (:func:`change_kind_phrase`); **it changes no
    verdict**. A path it cannot sub-classify stays a blocker and is simply described as code, so the
    worst it can do is understate what is being approved by one word."""
    out = []
    for path in non_docs_paths(paths):
        try:
            if is_prompt_path(path):
                out.append(path)
        except Exception:  # noqa: BLE001 — labelling may never affect the gate
            pass
    return out


def change_kind_phrase(blockers, prompt, repo) -> str:
    """**What kind of change this is, as a verb phrase, in the words that are actually true.**

    *"It changes functionality"* about every blocked PR is true and useless for one whose only
    blocker is `seneschal/modes/chat.md` — it reads like a build change, and the owner would go
    looking for code that is not there. It changes what the assistant **executes**, which is a
    different thing to weigh in the eight seconds a picker gets.

    One phrase, used by the picker **and** by the refusal, so the sentence an agent reads at the wall
    and the sentence the owner reads on their phone cannot disagree about the same PR.

    **"What the assistant executes" is a claim about the assistant's OWN repository, and is said only
    there.** The denylist behind `prompt` is written against this framework's tree: `persona/`,
    `seneschal/modes/`, a `references/` directory, a `CLAUDE.md`. A `CLAUDE.md` in some other
    repository the owner approves PRs in is prose *that* repo's agent runs, and narrating it as the
    running assistant's would be a false clause in the one sentence meant to be true. So for any
    repository :func:`is_assistant_repo` does not recognise (read off
    :func:`deploy_on_merge_map` — the module's one source for which repo is the assistant's) a
    prompt-shaped blocker is described as code and the phrase is *"changes functionality"*, full
    stop. **Not** *"changes functionality (incl. that repo's agent instructions)"*: the guard cannot
    vouch for what a `references/` or a `CLAUDE.md` IS in a tree it has never read, and a guess
    dressed as a fact is the same defect one word shorter. **Unknown repo ⇒ the plain phrase**, the
    same direction :func:`deploys_on_merge` takes. The verdict does not move: a foreign `CLAUDE.md`
    is still a blocker, it is just not narrated as the assistant's.

    The assistant is named via :func:`assistant_label` (the persona's configured name, else *"the
    assistant"*).

    This function checks the repo for itself rather than trusting its caller to have filtered
    `prompt`, because it is the sentence the owner reads: a caller that forgets the filter gets the
    true phrase anyway, and the callers filter too so their *count* wording agrees with the verb.

    Deliberately not louder for the prose case. :data:`DEPLOY_ON_MERGE` records the cost of the
    opposite error: overstating the stakes teaches the owner that the gate's own words are
    decoration, and a gate that is skimmed has stopped being a gate."""
    if not is_assistant_repo(repo):
        prompt = []
    if prompt and len(prompt) < len(blockers):
        return f"changes functionality and what {assistant_label()} executes"
    return f"changes what {assistant_label()} executes" if prompt else "changes functionality"


#: **The only PR states an approval can ever be spent on.** `gh pr view --json state` answers exactly
#: `OPEN` / `CLOSED` / `MERGED`, so this maps the two that are not OPEN to the sentence that says what
#: to do about it. **An unrecognised value is not in here on purpose** — see :func:`not_open_refusal`.
NOT_OPEN_STATES = {
    "MERGED": "It has already merged, so there is no merge left to approve.",
    "CLOSED": "It was closed without merging, so there is nothing to approve; reopen it first if it "
              "should merge.",
}

#: `request`'s exit code for *"this PR was read fine and cannot be asked about"*. **Distinct from 2**,
#: which every other `request` failure uses, because the two want opposite responses: a 2 is usually
#: transient (`gh` not logged in, no network) and re-running is reasonable, while a 3 will be a 3
#: forever until the caller names a different repository. Same reason `jobs.py` keeps its refusal exit
#: apart from its failures. The JSON on stdout carries `pr_state` either way, so nothing has to read
#: the code to know which happened.
EXIT_PR_NOT_OPEN = 3


def not_open_refusal(pr: int, facts: dict) -> str:
    """`""` if this PR can still be asked about, else the sentence saying why it cannot.

    **A merge approval for a merged or closed pull request is a question with no valid answer.** A
    picker for a PR that merged weeks ago — reached because a PR number was resolved against the
    wrong repository — can be approved in seconds by an owner tapping through a question shaped
    exactly like a correct one (module docstring).

    **THE MESSAGE IS THE DIAGNOSTIC, WHICH IS WHY IT LEADS WITH THE REPOSITORY.** The failure being
    caught is a caller who is wrong about *which repo*, and they will not discover that from
    *"#90 is merged"* — they discover it from *"<owner>/<repo> #90 is MERGED"*, against a title they
    do not recognise. So repo, number, state and title all appear, and the tail echoes the `--repo`
    that resolved to it (the CLI requires one, so it was named rather than inferred).

    **FAIL OPEN ON ANYTHING THAT IS NOT A KNOWN-NOT-OPEN STATE.** A missing, `None`, empty or
    unrecognised `state` asks anyway. This is the opposite polarity from the rest of stage 2, and
    deliberately so: stage 2 fails closed because *"cannot tell"* must never be *"allow a merge"*,
    whereas everything here can only ever suppress a **question**, and an un-asked merge is worse than
    an extra ask. `jobs.py`'s `preflight_refusal` line exactly — **fail-open on unknown, fail-closed on
    known-bad** — and it is why :data:`NOT_OPEN_STATES` is an allow-list of refusals rather than a
    `!= "OPEN"` comparison.

    **DRAFTS ARE NOT REFUSED, and that is a decision rather than an oversight.** `state` never says
    `DRAFT` (draft-ness is a separate `isDraft` field, which is not fetched), and a draft ask has a
    genuinely valid answer: marking a PR ready for review does **not** move its head SHA, so an
    approval taken on a draft is still spendable the moment it is ready, well inside
    :data:`APPROVAL_TTL_HOURS`. Refusing it would swallow a legitimate ask to catch a case nobody has
    made."""
    state = str((facts or {}).get("state") or "").strip().upper()
    why = NOT_OPEN_STATES.get(state)
    if not why:
        return ""
    slug = (facts or {}).get("repo") or ""
    repo = slug or "the resolved repository"
    title = _clip(str((facts or {}).get("title") or ""), TITLE_CHARS)
    named = f' — "{title}"' if title else ""
    # The echoed command is built from the SLUG, never from the "the resolved repository" stand-in:
    # a refusal that prints `--repo the resolved repository` is a refusal offering a command that
    # cannot be run — a door that does not open.
    # `pr_facts` raises on an unreadable URL so the slug is always there in practice, and the tail
    # simply drops rather than degrading if that ever stops being true.
    echo = (f" — `--pr {pr} --repo {slug}` is what resolved to this. If that is not the pull request "
            f"you meant, the number is right and the repository is not.") if slug else ""
    return (f"{repo} #{pr} is {state}, not OPEN{named}. {why} Nothing was asked, so no tap was "
            f"spent. A PR number is not an identity, so check the repository you named before "
            f"re-running: --repo is required, which means this one was named rather than "
            f"inferred{echo}")


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def match_head_refusal(match_head, facts: dict) -> str:
    """`""` if `--match-head-commit` is absent or is the PR's full head, else why the merge must not
    run — **decided BEFORE the approval is spent**, which is the whole point of it.

    `gh pr merge 18 --repo <owner>/<name> --merge --match-head-commit ce1477cdd6f9` — a 12-character
    prefix copied from a relay line that printed only that much of the head — is rejected by
    GitHub's GraphQL (*Could not coerce value … to GitObjectID*: `expectedHeadOid` is a full 40-hex
    OID), so **no merge happens**, but a guard that allowed the command has already let
    :func:`consume_approval` spend the tap on the attempt. The corrected retry is then refused as
    *"already spent"*, and the owner has to tap a fresh picker for a change they already approved.

    `consume_approval`'s *spent on an ATTEMPT* rule is deliberate and unchanged. What was wrong is
    that the guard allowed an attempt it could see would fail: the command's own text said which
    head `gh` would send, and the guard already held the real one. So this is a **refusal, not a
    repair** — the guard never rewrites a command — and it spends nothing, because nothing was tried.
    Both failures are the fail-closed direction: a value GitHub cannot coerce, and a value that names
    a different commit than the one an approval would be bound to, are each a merge that must not
    run rather than one to let GitHub sort out."""
    if match_head is None:
        return ""
    head = str(facts.get("head_sha") or "")
    given = str(match_head).strip()
    if not _FULL_SHA_RE.match(given.lower()):
        # The full head is printed HERE so the corrected command needs no second lookup — the
        # prefix was copied from a message; the fix is copied from this one.
        return (f"`--match-head-commit` needs the full 40-character SHA; GitHub rejects a prefix "
                f"and the approval would be wasted — full head: {head}")
    if given.lower() != head.lower():
        return (f"`--match-head-commit {given}` is not this PR's head ({head}) — GitHub would "
                f"refuse the merge and the approval, which is bound to the head, would be wasted")
    return ""


#: **The `mergeStateStatus` values a merge may not spend a tap on — an ALLOW-LIST, `NOT_OPEN_STATES`'
#: shape and reason, one field over.** A base branch protected with
#: `required_status_checks.strict` requires a branch be up to date with its base, so a PR that is
#: `BEHIND` has no textual conflict — `mergeable` reads `MERGEABLE` — and would otherwise read as an
#: ordinary approved merge right up until GitHub refuses it. **Every other value is silent here**: `CLEAN`/`UNSTABLE`/`DIRTY`/
#: `BLOCKED`/`DRAFT`/`HAS_HOOKS`/`UNKNOWN`, and anything GitHub adds later, ask/allow exactly as
#: before — this predicate can only ever ADD a refusal, never remove one that already exists.
BEHIND_STATES = {
    "BEHIND": "It is BEHIND its base branch, and the base branch requires a branch be up to date "
              "before it can merge. Merge the base into this branch and push, then re-ask — no hand "
              "resolution is needed, this is not a conflict.",
}


def behind_refusal(pr: int, facts: dict) -> str:
    """`""` if this PR's head is not known to be BEHIND its base, else the sentence saying so and
    what to do about it.

    **Read by `evaluate_pr`, so it reaches `check`, `judge` and the hook itself in one place** — the
    same sharing argument `evaluate_pr`'s own docstring makes for the docs-only classifier: a second
    "can this actually merge" opinion written for one door could drift from what the others decide.

    **Distinct from a `CONFLICTING` refusal, on purpose.** GitHub reports NO textual conflict for a
    BEHIND head — the two facts come from different fields (`mergeStateStatus` here,
    `mergeable` for a conflict) and have different fixes, so the message says *merge up*, never
    *resolve a conflict*.

    **Fails open on anything that is not a known-BEHIND state**, the same polarity `not_open_refusal`
    argues for `NOT_OPEN_STATES`: a missing, `None`, empty or unrecognised `merge_state_status`
    changes nothing here, because this predicate can only ever REFUSE a merge that would otherwise be
    allowed, and refusing on an absent answer would deny a merge the guard has no evidence against."""
    status = str((facts or {}).get("merge_state_status") or "").strip().upper()
    return BEHIND_STATES.get(status, "")


def ci_refusal(pr: int, facts: dict) -> str:
    """`""` only when every CI check on this PR's head has FINISHED and PASSED; otherwise the
    sentence saying why the merge may not run.

    **NEVER MERGE RED OR PENDING, AND THERE IS NO CARVE-OUT.** Not for a docs-only PR (its standing
    grant is *merge once CI is green*, and green is the precondition, not a formality), not for a
    failure that looks pre-existing, flaky or unrelated, and not for an approved PR — an approval
    covers *what* merges, never *when CI has not said yes*. Deciding that a red check is safe to
    waive is exactly the kind of self-granted authorization this module exists to stop, so the
    only door past a red check is the owner fixing it or merging by hand.

    **Fails CLOSED on every doubt**, the stage-2 polarity: a rollup that could not be read (the
    field absent), one with no checks at all (*nothing ran* is not *passed* — a repository with no
    CI gets CI, it does not get a waiver), a pending check, and a verdict that could not be
    computed are all refusals. The fold is `watch_pr.classify` — the repository's one reading of a
    rollup, imported lazily rather than re-derived, so the watcher that announces *green* and the
    guard that permits the merge can never disagree about the same checks. Red wins over pending:
    a failed check is reported even while others still run.

    Read by :func:`evaluate_pr` (so `check` reports it) and by :func:`decide_command` before the
    approval is spent."""
    nodes = (facts or {}).get("ci_nodes")
    if not isinstance(nodes, list):
        return (f"its CI status could not be read, so it cannot be shown green — never merge red or "
                f"pending, and 'cannot tell' is not green")
    try:
        import watch_pr
        verdict = watch_pr.classify(nodes)
    except Exception as e:  # noqa: BLE001 — a verdict we cannot compute is not a pass
        return f"its CI verdict could not be computed ({e!r}), and 'cannot tell' is not green"
    if verdict.get("failed"):
        names = ", ".join((verdict.get("failing_names") or [])[:4]) or "a check"
        return (f"its CI is RED ({names}). Never merge red — not for a pre-existing, flaky or "
                f"seemingly unrelated failure either. Fix it, or leave the decision to the owner")
    if verdict.get("empty"):
        return ("no CI checks have reported on its head. Nothing ran is not the same as passed; "
                "wait for CI to report, and if this repository has no CI at all, add some")
    if not verdict.get("done"):
        return (f"its CI is still PENDING ({verdict.get('pending')} check(s) running). Never merge "
                f"pending — wait for every check to finish green")
    return ""


def stack_refusal(pr: int, facts: dict, cwd=None, runner=None) -> str:
    """`""` unless an ASYNC merge of `pr` would also merge pull requests nobody approved.

    GitHub, on `PUT …/pulls/{n}/merge-async`: *"When using this endpoint to merge a stacked pull
    request, all pull requests in the stack up to and including the requested PR will be merged
    into the base branch."* An approval covers one PR at one head; the guard classifies that PR's
    own diff. So an async merge is allowed only for the **bottom** of a stack — a PR whose base
    branch is not the head branch of another open PR. Merge from the bottom up; each PR gets its own
    approval, as it always did. (The bottom PR of a stack — base `develop`, with another PR stacked
    on top of it — is exactly the shape this allows.)

    **Fails CLOSED**: no base branch, `gh pr list` absent, non-zero, or unreadable are all refusals,
    because "cannot tell what else would merge" is not "nothing else would"."""
    base = str((facts or {}).get("base") or "").strip()
    repo = (facts or {}).get("repo")
    if not base or not repo:
        return ("an async merge also merges every PR below this one in its stack, and this PR's base "
                "branch could not be read, so what else would merge is unknown")
    argv = ["gh", "pr", "list", "--repo", repo, "--head", base, "--state", "open",
            "--json", "number,headRefName"]
    try:
        code, out, err = (runner or _run)(argv, cwd)
        rows = json.loads(out) if code == 0 else None
    except Exception as e:  # noqa: BLE001 — cannot tell is not "nothing below"
        return f"could not list the PRs below #{pr} in its stack (`gh pr list` failed: {e})"
    if not isinstance(rows, list):
        return (f"could not list the PRs below #{pr} in its stack (`gh pr list` exited {code}: "
                f"{str(err or out or '').strip()[:200]})")
    below = sorted(r.get("number") for r in rows if isinstance(r, dict) and r.get("number") != pr)
    if not below:
        return ""
    names = ", ".join(f"#{n}" for n in below)
    return (f"#{pr} is stacked on {names} (its base `{base}` is that PR's head), and an async merge "
            f"merges the whole stack below it too — which the owner did not approve. Merge the bottom "
            f"of the stack first, each on its own approval")


# --------------------------------------------------------------------------- the approval record

def approvals_dir(state_dir: str) -> str:
    return os.path.join(state_dir, APPROVALS_DIR)


def repo_from_url(url) -> str:
    """`https://github.com/example/other/pull/45` -> `example/other`. `""` when it cannot be read,
    and every caller treats that as a DENY."""
    m = _PULL_REPO_RE.search(str(url or ""))
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def repo_key(repo) -> str:
    """`example/other` -> `example__other`, a filename-safe, case-folded key. `""` for no repo,
    which is what selects the legacy bare-number path below.

    Case-folded because GitHub slugs are case-insensitive and NTFS filenames are too: without it
    `Example/Repo` and `example/repo` are one file on a Windows host and two keys in the code, which
    is the same disagreement between the name and the artifact the repo-keying exists to remove."""
    slug = str(repo or "").strip().strip("/").lower()
    if not slug:
        return ""
    return re.sub(r"[^a-z0-9._-]+", "_", slug.replace("/", "__"))


# ------------------------------------------------------- WHICH repository (required, never defaulted)

#: A git remote URL -> `owner/name`. Both spellings `origin` commonly holds —
#: `https://github.com/example/repo` and `git@github.com:example/other.git` — plus the `ssh://`
#: form, an optional `.git`, and an optional trailing slash. **GitHub only, and that is the point
#: rather than a limitation**: the slug keys an approval and is what `gh` is asked about, so a remote
#: that is not GitHub establishes nothing and must read as *unknown* rather than as a near miss.
_REMOTE_REPO_RE = re.compile(
    r"^(?:(?:https?|ssh|git)://)?(?:[^@/\s]+@)?github\.com[:/]+"
    r"([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?$",
    re.IGNORECASE)

#: `owner/name`, as a caller types it for `-R`/`--repo`. Deliberately not a loose "contains a slash"
#: test, and deliberately **not** accepting a bare name: `gh -R repo` resolves against the logged-in
#: user, which is state outside the command, and reading a repository out of state outside the
#: command is the entire defect this file refuses to have.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

#: How the invoking directory's repository is read on the hook path. `git config --get` rather than
#: `git remote get-url` because it is the older, stabler spelling and answers **non-zero** on a
#: missing `origin` instead of writing prose to stderr. It goes through :func:`_run` — the module's
#: one subprocess seam — so a test that can reach `gh` can reach this too and there is no second seam
#: to forget to stub.
ORIGIN_ARGV = ("git", "config", "--get", "remote.origin.url")

#: The subcommands that REQUIRE `--repo` — a PR number is not an identity, and a default is a guess
#: wearing a fact's clothes (module docstring, "THE REPOSITORY IS REQUIRED").
#:
#: `prune-asks` is the single absence, and it is a classification rather than an oversight: it ages
#: rows out of `merge-ask-log.jsonl` by **date**, that log is one file spanning every repository, and
#: there is no per-repository answer for it to give. A flag that changes nothing is worse than no
#: flag — it teaches the caller that naming a repo is ceremony. A test asserts this set is exactly
#: the parser's subcommands minus that one, so a NEW subcommand has to be classified deliberately
#: rather than defaulted into silence.
REPO_REQUIRED_COMMANDS = frozenset({
    "check", "judge", "request", "render", "ask-on-green", "list", "asks", "refund",
})

#: One spelling of the flag's help, shared by every parser that declares it, so the subcommands cannot
#: disagree about whether it is optional. It says REQUIRED first because that is the line a caller
#: reads when `--help` tells them why their command exited 4.
REPO_HELP = ("REQUIRED — owner/name (or a GitHub URL). There is no default and no fallback to the "
             "working directory or to the configured repositories: a PR number is not an identity, "
             "and #45 can exist in more than one repository the owner works in.")

#: A CLI subcommand run with no `--repo`. **Distinct from 2**, for :data:`EXIT_PR_NOT_OPEN`'s exact
#: reason: a 2 is usually transient (`gh` not logged in, no network) and re-running is reasonable,
#: while this one is identical on every re-run until the caller names a repository.
EXIT_NO_REPO = 4


def repo_from_remote(url) -> str:
    """A git remote URL -> `owner/name`, or `""` when it is not a readable GitHub remote.

    Separate from :func:`repo_from_url`, which reads a **pull request** URL, because they are
    different artifacts that happen to share a host: one is what `gh` answered about a PR, the other
    is what a checkout says about itself. Collapsing them would let a `/pull/` URL satisfy an
    `origin` lookup and vice versa."""
    m = _REMOTE_REPO_RE.match(str(url or "").strip().rstrip("/"))
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def normalize_repo(value) -> str:
    """What a caller typed for `--repo`/`-R` -> `owner/name`, or `""` when it is not one.

    `gh` accepts a slug **or** a full URL, so both do here. Anything else — a bare name, a branch, a
    path, whitespace — is `""`, and every caller reads `""` as *unknown*. Unknown blocks."""
    text = str(value or "").strip()
    if not text:
        return ""
    slug = repo_from_remote(text)
    if slug:
        return slug
    bare = text.strip("/")
    if bare.lower().endswith(".git"):
        bare = bare[:-4]   # `o/n.git` and `o/n` are one repository and must not be two keys
    return bare if _SLUG_RE.match(bare) else ""


def origin_repo(cwd, runner=None) -> str:
    """The `owner/name` of `origin` in the directory a command was typed in, or `""`.

    **Never raises, and every failure is the same `""`:** no `cwd`, `git` not on PATH, a directory
    that is not a checkout, no `origin`, a remote that is not GitHub, a timeout, output that is not a
    URL. `""` means *could not establish*, and at the one caller that blocks — this is stage 2, where
    "cannot tell" is never "allow".

    Deliberately NOT `repo_config.origin_repo`: that reader answers about *this framework's own
    checkout* for the PR sweep's defaults, while this one answers about *the directory the command was
    typed in*, through this module's single subprocess seam."""
    if not cwd:
        return ""
    try:
        code, out, _err = (runner or _run)(list(ORIGIN_ARGV), cwd)
    except Exception:  # noqa: BLE001 — any transport failure is a repository we could not establish
        return ""
    if code != 0:
        return ""
    lines = str(out or "").strip().splitlines()
    return repo_from_remote(lines[0]) if lines else ""


def derive_repo(command_repo=None, context_repo=None, cwd=None, runner=None) -> tuple:
    """**Which repository is this command about?** -> `(slug, source)`. `slug == ""` means BLOCK.

    The CLI requires `--repo`. **The hook cannot be handed that rule directly**: it is given a shell
    command a human typed — `gh pr merge 566` — and it cannot demand a flag nobody passed. So the
    requirement splits, and this is the hook's half. The repository is still established
    *explicitly* and *said out loud*; it is derived rather than declared.

    The order is not a preference list. Each rung is strictly better evidence than the one below it:

      1. **The command's own `-R`/`--repo`** — named by the caller, inside the artifact being judged.
      2. **`context_repo`** — the repository the caller states they are replaying in. Only `judge`
         passes this: it is that subcommand's `--repo`, standing exactly where the hook's cwd stands.
      3. **`origin` in the invoking directory** — the weakest rung that is still a fact about
         something. Handing `cwd` to `gh pr view` with no `--repo` would make `gh` do this lookup
         silently; doing it here is what makes the answer sayable.

    **When none of them answers, this blocks.** A guard that infers its repository from the current
    directory will cheerfully answer about a repository the caller did not mean, in a shape
    indistinguishable from a correct answer — and a checkout with many worktrees live at once makes
    that likely rather than rare. An unknown repository is where allowing is *most* dangerous: #45
    in one repository and #45 in another are one number and two artifacts. **The configured
    repositories (`repo_config`) are never a rung** — config says what the sweep watches, not which
    repository a typed command meant.

    **It establishes which repository to ASK ABOUT; it does not key anything.** The approval key
    still comes from `gh`'s own answer (:func:`pr_facts`, the `url` field), and that separation is
    load-bearing: the flag says which question to ask, the response says what was actually asked
    about, and a mismatch between them is caught by `pr_facts`' own number check rather than assumed
    away here.

    `source` names the rung that answered and is carried on the :class:`Decision` — so the refusal,
    the `judge` line and the allow line all say which repository this was decided about. When `slug`
    is `""`, `source` is the refusal's headline instead."""
    named = normalize_repo(command_repo)
    if named:
        return named, f"named on the command itself (--repo {named})"
    if command_repo:
        return "", (f"the command names a repository this guard cannot read as owner/name "
                    f"({str(command_repo)[:120]!r}). A PR number is not an identity, so this is a "
                    f"refusal rather than a guess about which repository was meant.")
    supplied = normalize_repo(context_repo)
    if supplied:
        return supplied, f"named by the caller (--repo {supplied})"
    if context_repo:
        return "", (f"--repo was given as {str(context_repo)[:120]!r}, which is neither owner/name "
                    f"nor a GitHub URL, so no repository was established.")
    from_origin = origin_repo(cwd, runner=runner)
    if from_origin:
        return from_origin, f"derived from origin in the invoking directory {cwd}"
    where = f" ({cwd})" if cwd else " — the event carried no working directory"
    return "", (f"this command names no repository, and none could be read from origin in the "
                f"directory it was typed in{where}. A PR number is not an identity: #45 can exist "
                f"in more than one repository, and a guard that guesses which one answers a "
                f"question nobody asked, in a shape that looks exactly like a correct answer. "
                f"Name it on the command — --repo <owner>/<name>.")


def missing_repo_refusal(cmd: str) -> str:
    """The CLI's refusal when `--repo` is absent. **It names the flag**, because typing it is the
    caller's next action and a refusal they have to go read a doc to act on is a wall.

    There is no fallback to the working directory, none to a constant or to the configured
    repositories, and no helpful read of `origin` — the hook derives because it has no choice
    (:func:`derive_repo`), and a CLI caller has every choice. A caller composing a command composes
    it from the repository they are *thinking* about, which is not always the one they are standing
    in."""
    return "\n".join([
        f"merge_guard.py {cmd}: --repo is REQUIRED and has no default.",
        "",
        f"  python seneschal/scripts/merge_guard.py {cmd} --repo <owner>/<name> ...",
        "",
        "A PR number is not an identity. #45 can be a real pull request in more than one repository",
        "the owner works in. A `request --pr 90` resolved against the wrong repository can land on a",
        # **No repository is named here.** `DeployClaimIsRepoConditionalTest` pins that this module
        # spells no concrete repository slug in its code — which repositories deploy is config
        # (`DEPLOY_ON_MERGE` → `repo_config`), and a narrative sentence naming one is
        # indistinguishable from a hardcode to the test that keeps that true.
        "PR that merged weeks ago; the owner gets a picker shaped exactly like a correct one, and a",
        "real approval is spent on a question that should never have been asked.",
        "",
        "So this falls back to nothing: not the working directory, not a constant, not origin, not",
        "the configured repositories. A default is a guess wearing a fact's clothes, and the answer",
        "it produces is shaped exactly like a correct one.",
    ])


def approval_path(state_dir: str, pr: int, repo=None) -> str:
    """Where an approval for `(repo, pr)` is written. **With no repo this is the LEGACY path**, and
    that fallback is load-bearing rather than tidy — see :func:`load_approval`."""
    key = repo_key(repo)
    name = f"{key}--{int(pr)}.json" if key else f"{int(pr)}.json"
    return os.path.join(approvals_dir(state_dir), name)


def _existing_approval_path(state_dir: str, pr: int, repo=None) -> str:
    """The file an approval for `(repo, pr)` actually lives in *today*: the repo-keyed one if it is
    there, else the legacy bare-number one if THAT is there, else the repo-keyed one (so a write
    always lands on the new shape). One function, so read and write can never disagree about which
    file they mean and `consume_approval` can never spend a record it did not read."""
    keyed = approval_path(state_dir, pr, repo)
    if repo_key(repo) and not os.path.exists(keyed):
        legacy = approval_path(state_dir, pr, None)
        if os.path.exists(legacy):
            return legacy
    return keyed


def _stamp(now=None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _parse_stamp(raw):
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _save_json(path: str, payload: dict) -> None:
    """Build-then-`os.replace`, never a truncate-write — the `state/` house rule: these files are
    gitignored, so a half-written one exists nowhere else."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def load_approval(state_dir: str, pr: int, repo=None):
    """The record for `(repo, pr)`, or None. Never raises: an unreadable record is *no approval*,
    which denies.

    **A LEGACY bare-number record still resolves, deliberately — the tolerate half of the repo
    keying.** A bare `<pr>.json` carries no `repo` field and none can be inferred from it, so
    migrating one means *guessing* which repository it was for; a wrong guess files one repo's
    approval under another's, which is the collision the keying exists to remove, made permanent and
    invisible. Tolerating them costs nothing that was not already the case: what keeps a
    cross-repository number collision safe is the **head-SHA binding**, which is checked here too and
    holds across repositories, and every legacy record expires within :data:`APPROVAL_TTL_HOURS`. So the
    tolerated set is closed, self-liquidating, and no weaker than the day it was written — while
    every NEW record is keyed on `(repo, pr)` and cannot be clobbered at all."""
    try:
        with open(_existing_approval_path(state_dir, pr, repo), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def record_approval(state_dir: str, pr: int, head_sha: str, question_id: str, repo=None,
                    approved_by: str = "telegram-tap", now=None) -> dict:
    """Write the approval for `(repo, pr)` at `head_sha`. **Called from exactly one place** —
    the daemon's Telegram callback path in `presence.py`, on a real `callback_query` from Telegram.

    Nothing else in this repo calls it, and a test asserts that. It is not private (a `_` prefix
    would be theatre against a same-user process), but it has one caller by construction so that
    "who can approve a merge" is a question with a code answer instead of a prose one.

    `repo` comes off the question's own `meta`, so it is whatever the picker was asked about. **An
    absent one writes the legacy path rather than refusing**: a question sent by an older version may
    still be in flight on the owner's phone, and a tap on it must still mint the approval they think
    they are giving. That record then reads back exactly as a legacy record does."""
    record = {"schema": APPROVAL_SCHEMA, "pr": int(pr), "repo": str(repo) if repo else None,
              "head_sha": str(head_sha), "question_id": str(question_id),
              "approved_by": approved_by, "approved_at": _stamp(now), "consumed_at": None}
    _save_json(_existing_approval_path(state_dir, pr, repo), record)
    return record


def approval_events_path(state_dir: str) -> str:
    return os.path.join(state_dir, APPROVAL_EVENTS_FILE)


def record_approval_event(state_dir: str, row: dict, now=None) -> None:
    """Append one row to :data:`APPROVAL_EVENTS_FILE` — every spend, every refund, every refund
    DECLINED. Append-only, the ask log's shape (:func:`record_ask`), and fail-open for the same
    reason: a row we cannot write costs the history, never the decision. The approval record itself
    also carries `spends`/`refunds` lists; this log is the one that survives the record being
    overwritten by the next tap."""
    row = {"schema": APPROVAL_EVENTS_SCHEMA, "at": _stamp(now), **row}
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(approval_events_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError):
        pass


def consume_approval(state_dir: str, pr: int, now=None, repo=None, command=None) -> bool:
    """Spend the approval. Best-effort: a failure here cannot un-allow a merge already permitted, so
    it must not raise into the decision path.

    **Spent means "spent on an ATTEMPT"** — the guard sits *before* the merge and cannot see whether
    it worked, and that direction is still deliberate: a live approval must never lie around while a
    merge might have landed. **But a refused attempt can be given back** — a tap spent on a merge
    GitHub refuses outright (a stacked PR, *"must be merged using the asynchronous merge REST
    API"*) merged nothing, and the retry would otherwise be blocked as already spent.
    :func:`refund_approval` is the only way back, and it demands proof
    that the merge did not happen. The spend is recorded in the record's `spends` list and in the
    event log, so a refund can be matched to the spend it reverses."""
    record = load_approval(state_dir, pr, repo)
    if not record:
        return False
    record["consumed_at"] = _stamp(now)
    spend = {"at": record["consumed_at"], "head_sha": record.get("head_sha"),
             "command": _clip(str(command or ""), 300)}
    record["spends"] = list(record.get("spends") or []) + [spend]
    try:
        _save_json(_existing_approval_path(state_dir, pr, repo), record)
    except OSError:
        return False
    record_approval_event(state_dir, {"event": "spent", "pr": int(pr), "repo": record.get("repo"),
                                      "question_id": record.get("question_id"), **spend}, now=now)
    return True


#: A refund reverses the attempt that JUST happened. Past this, a spent approval stays spent —
#: the refused merge is not the thing being retried any more, and a fresh tap is cheap.
REFUND_WINDOW_MINUTES = 15
#: How many times one approval may be given back. A merge that GitHub refuses three times at the
#: same head is not going to succeed on the fourth; that is a question for the owner, not a loop.
MAX_REFUNDS = 3
#: Failure text meaning the merge IS in flight even though the command exited non-zero — the async
#: endpoint's 409 (*"an existing merge request already enqueued"*) and the merge queue. A refund
#: here would hand back an approval while a merge of that head is still going to land.
_IN_FLIGHT_RE = re.compile(r"\b409\b|already\s+enqueued|merge\s+queue|already\s+in\s+a\s+merge",
                           re.IGNORECASE)
_EXIT_CODE_RE = re.compile(r"Exit code (-?\d+)")


def _single_segment(command: str) -> bool:
    """Is `command` ONE shell segment — so its exit status is the merge's own? `gh pr merge 5 && x`
    exits with `x`'s status, `gh pr merge 5 | tail` with `tail`'s; either can be non-zero after a
    merge that landed (or was queued), so neither can prove a refusal."""
    return len([s for s in _SEGMENT_SPLIT_RE.split(command or "") if s.strip()]) == 1


def refund_approval(state_dir: str, pr: int, repo, exit_status, command: str, error_text: str = "",
                    via: str = "hook", cwd=None, runner=None, now=None) -> tuple:
    """Give back an approval that was spent on a merge GitHub REFUSED. -> `(refunded, reason)`.

    **The un-spend may not open a replay hole, so every condition below is required, and each is a
    fact read in this call rather than a claim accepted from the caller:**

      1. **The command failed**: `exit_status` is an integer and not 0. A success — including
         `--auto` or an async 202, where GitHub accepted and the PR is still OPEN — never refunds.
      2. **The exit status is the merge's own**: `command` is one shell segment with exactly one
         merge invocation in it, for this `(repo, pr)`, and it is not `--auto`.
      3. **The failure text does not say the merge is in flight** (:data:`_IN_FLIGHT_RE`).
      4. **An approval for `(repo, pr)` is on file, spent, within :data:`REFUND_WINDOW_MINUTES` of
         the spend, and refunded fewer than :data:`MAX_REFUNDS` times.**
      5. **GitHub, asked NOW, says the PR is still OPEN at the SAME head the approval is bound to.**
         A merged PR is not OPEN; a pushed PR has a new head — and either way nothing is restored.

    What a refund restores is exactly the approval the owner gave: that PR, that head, the original
    TTL counted from their tap. It can never let anything merge they did not approve. **Every
    outcome — refunded or declined, and why — is written to :data:`APPROVAL_EVENTS_FILE`**, and a
    refund is also appended to the record's own `refunds` list. Never raises."""
    base = {"pr": int(pr), "repo": repo, "via": via, "exit_status": exit_status,
            "command": _clip(str(command or ""), 300), "error": _clip(str(error_text or ""), 300)}

    def decline(why: str) -> tuple:
        record_approval_event(state_dir, {"event": "refund-declined", "why": why, **base}, now=now)
        return False, why

    try:
        if not isinstance(exit_status, int) or isinstance(exit_status, bool):
            return decline("no exit status was available, so a refusal cannot be told from a merge")
        if exit_status == 0:
            return decline("the command exited 0 — the merge (or its enqueue) was accepted")
        if not _single_segment(command):
            return decline("the command is more than one shell segment, so its exit status is not "
                           "the merge's own")
        invs = merge_invocations(command)
        if len(invs) != 1:
            return decline(f"the command holds {len(invs)} merge invocations, not one")
        try:
            parsed = parse_invocation(invs[0])
        except GuardError as e:
            return decline(f"the merge in the command could not be read: {e}")
        if parsed.get("pr") != int(pr):
            return decline(f"the command merges #{parsed.get('pr')}, not #{pr}")
        named = normalize_repo(parsed.get("repo"))
        if named and repo_key(named) != repo_key(repo):
            return decline(f"the command merges in {named}, not {repo}")
        if "--auto" in (invs[0].get("tokens") or []):
            return decline("`--auto` enables auto-merge; the merge may still land")
        if _IN_FLIGHT_RE.search(str(error_text or "")):
            return decline("the failure says a merge is already enqueued or queued — it may land")

        record = load_approval(state_dir, pr, repo)
        if not record or record.get("schema") != APPROVAL_SCHEMA:
            return decline("no approval on file for this PR")
        on_file = record.get("repo")
        if on_file and repo_key(on_file) != repo_key(repo):
            return decline(f"the approval on file is for {on_file}, not {repo}")
        spent = _parse_stamp(record.get("consumed_at"))
        if spent is None:
            return decline("the approval on file is not spent — nothing to give back")
        current = now or datetime.now(timezone.utc)
        if current - spent > timedelta(minutes=REFUND_WINDOW_MINUTES):
            return decline(f"it was spent more than {REFUND_WINDOW_MINUTES} min ago")
        refunds = list(record.get("refunds") or [])
        if len(refunds) >= MAX_REFUNDS:
            return decline(f"it has already been given back {len(refunds)} times; ask the owner again")

        try:
            facts = pr_facts(int(pr), repo=repo, cwd=cwd, runner=runner)
        except GuardError as e:
            return decline(f"the PR could not be re-read, so it cannot be shown unmerged: {e}")
        if repo_key(facts.get("repo")) != repo_key(repo):
            return decline(f"`gh` answered about {facts.get('repo')}, not {repo}")
        if str(facts.get("state") or "").upper() != "OPEN":
            return decline(f"the PR is {facts.get('state')!r} now, not OPEN")
        if facts.get("head_sha") != record.get("head_sha"):
            return decline(f"the PR's head is {facts.get('head_sha')}, not the approved "
                           f"{record.get('head_sha')}")

        refund = {"at": _stamp(now), "spent_at": record.get("consumed_at"),
                  "head_sha": facts["head_sha"], "pr_state": "OPEN", **base}
        record["refunds"] = refunds + [refund]
        record["consumed_at"] = None
        _save_json(_existing_approval_path(state_dir, pr, repo), record)
    except Exception as e:  # noqa: BLE001 — a refund we cannot complete leaves it SPENT
        return decline(f"the refund failed: {e!r}")
    record_approval_event(state_dir, {"event": "refunded", "question_id": record.get("question_id"),
                                      **refund}, now=now)
    return True, (f"approval for {repo} #{pr} at {facts['head_sha']} restored — GitHub refused the "
                  f"merge (exit {exit_status}) and the PR is still OPEN at the approved head")


def _question_confirms(state_dir: str, record: dict, pr: int, head_sha: str, repo=None) -> str:
    """`""` if `../state/telegram-questions.json` corroborates the record, else why it does not.

    The cross-check exists because the approval file and the question store have *different
    producers*: the store is written by the send path, which only gets a question id after the Bot
    API accepted a real message, and stamped `answered_at` by the resolve path, which runs on a real
    `callback_query`. Consistently forging both is a deliberate act; forging one is a slip. This is
    not a trust boundary — see the module docstring, which says so plainly — it is a raised floor.

    `telegram_ask` is imported HERE rather than at module scope so the hook's ordinary path (every
    Bash call on the machine, almost none of them merges) pays nothing for it."""
    qid = record.get("question_id")
    if not isinstance(qid, str) or not qid:
        return "the approval names no question id"
    try:
        import telegram_ask as ta
        store = ta.load_store(ta.store_path(state_dir))
    except Exception as e:  # noqa: BLE001 — cannot corroborate is not corroborated
        return f"the question store could not be read ({e})"
    q = (store.get("questions") or {}).get(qid)
    if not isinstance(q, dict):
        return f"question {qid} is not in the question store"
    if not q.get("answered_at"):
        return f"question {qid} was never answered"
    meta = q.get("meta")
    if not isinstance(meta, dict) or meta.get("kind") != APPROVAL_META_KIND:
        return f"question {qid} is not a merge-approval question"
    if meta.get("pr") != pr:
        return f"question {qid} approves PR #{meta.get('pr')!r}, not #{pr}"
    # A question sent before the keying fix carries no `repo`; absent is tolerated exactly as the
    # legacy approval record is, and for the same reason. A PRESENT one that disagrees is a DENY.
    asked_repo = meta.get("repo")
    if asked_repo and repo and repo_key(asked_repo) != repo_key(repo):
        return f"question {qid} approves #{pr} in {asked_repo}, not in {repo}"
    if meta.get("head_sha") != head_sha:
        return f"question {qid} was asked at a different head SHA"
    approve_index = meta.get("approve_index", 0)
    if list(q.get("selected") or []) != [approve_index]:
        return f"question {qid} was not answered with Approve"
    return ""


def verify_approval(state_dir: str, pr: int, head_sha: str, repo=None, now=None) -> str:
    """`""` if a live approval covers `(repo, pr)` at `head_sha`, else the reason it does not.

    Seven ways to fail, each of them a DENY: no record, a malformed one, one for another PR, one for
    another REPOSITORY, one bound to a head SHA that has since moved, one already spent, one past its
    TTL — plus the question-store cross-check above."""
    record = load_approval(state_dir, pr, repo)
    if record is None:
        return "no approval on file for this PR"
    if record.get("schema") != APPROVAL_SCHEMA:
        return f"the approval on file has schema {record.get('schema')!r}, not {APPROVAL_SCHEMA}"
    if record.get("pr") != pr:
        return f"the approval on file is for PR #{record.get('pr')!r}, not #{pr}"
    on_file = record.get("repo")
    if on_file and repo and repo_key(on_file) != repo_key(repo):
        return (f"the approval on file is for #{pr} in {on_file}, and this merge is #{pr} in "
                f"{repo} — a PR number is not an identity across repositories")
    if record.get("consumed_at"):
        return (f"that approval was already spent at {record['consumed_at']} — approvals are "
                f"single-use. If the merge failed and you are retrying, ask again")
    recorded_sha = record.get("head_sha")
    if not isinstance(recorded_sha, str) or recorded_sha != head_sha:
        return (f"the approval was given at {str(recorded_sha)[:12]} but the PR's head is now "
                f"{head_sha[:12]} — new commits have landed since the owner approved, so what "
                f"they approved is not what would merge")
    at = _parse_stamp(record.get("approved_at"))
    if at is None:
        return "the approval has no readable timestamp"
    if (now or datetime.now(timezone.utc)) - at > timedelta(hours=APPROVAL_TTL_HOURS):
        return (f"the approval is older than {APPROVAL_TTL_HOURS} h (given {record['approved_at']})")
    return _question_confirms(state_dir, record, pr, head_sha, repo)


# --------------------------------------------------------------------------- asking the owner

def resolve_env_file(explicit=None, default_path: str | None = None):
    """Which `telegram.env` to hand `telegram_ask.py`, or `None` for "use the environment".

    **An explicit `--env-file` always wins** — a caller who named a file meant that file, and a test
    or a throwaway bot depends on it (`ping-progress`/`presence-offline` style: a live `telegram.env`
    picked up by accident messages the owner for real). Otherwise the sibling `telegram.env`, if it
    exists. If it does not, `None`: the environment may still carry `TELEGRAM_*`, and
    `telegram_send.load_env` reads it either way — so an absent file degrades to *absent*, exactly
    like every other optional integration here, rather than to a traceback.

    `default_path` is a test seam, so the suite's verdict never depends on whether this host happens
    to have credentials sitting beside the script."""
    if explicit:
        return explicit
    path = DEFAULT_TELEGRAM_ENV if default_path is None else default_path
    return path if path and os.path.exists(path) else None


def in_quiet_hours(now=None) -> bool:
    """Is it the small hours on the owner's wall clock? See :data:`ASK_ON_GREEN_QUIET_HOURS`.

    **The window is `sentinel`'s, imported rather than copied** — `sentinel.curfew_window()`, the
    owner's configured `owner.nightCurfew` (default 01:00–07:00). It asks the same question this does
    — *"is 2 AM a reasonable hour to buzz the owner?"* — and a second pair of numbers here is a rule
    that drifts from its twin the first time one of them is edited. The hour is read through
    `clock.to_local` (the owner's configured zone, DST-correct; machine-local when unconfigured). A
    window whose start is after its end wraps midnight, and start == end disables it, exactly as the
    curfew reads it. All imports are lazy: `ImportWeightTest` pins this module's import surface to
    stdlib, because the hook pays it on every shell call and this path runs once per green watch.

    **Fails toward SENDING.** If the hour cannot be determined the picker goes out — a mistimed
    message is loud where a swallowed one is silent."""
    if not ASK_ON_GREEN_QUIET_HOURS:
        return False
    try:
        import clock
        import sentinel
        start, end = sentinel.curfew_window()
        if start == end:
            return False
        t = clock.to_local(now or datetime.now(timezone.utc)).time()
        return (start <= t < end) if start < end else (t >= start or t < end)
    except Exception:  # noqa: BLE001 — cannot tell the hour ⇒ send, and say nothing about the hour
        return False


def awake_evidence(state_dir: str = DEFAULT_STATE_DIR, now=None):
    """The newest instant within :data:`AWAKE_OVERRIDE_WINDOW` at which the owner personally touched
    a channel (`turns.awake_since`), or `None` — including when :data:`AWAKE_OVERRIDE` is off and on
    any failure at all. **Fails toward QUIET**, the opposite polarity to :func:`in_quiet_hours`, and
    deliberately: that one fails toward sending because a missing hour is loud either way, while this
    one can only ever RELAX the window, so it relaxes it on positive evidence and never on doubt.
    `turns` is imported lazily for the same `ImportWeightTest` reason `clock` and `sentinel` are."""
    if not AWAKE_OVERRIDE:
        return None
    try:
        import turns
        return turns.awake_since(state_dir, now=now or datetime.now(timezone.utc),
                                 window=AWAKE_OVERRIDE_WINDOW)
    except Exception:  # noqa: BLE001 — no evidence ⇒ the window stands
        return None


def picker_quiet_hours(state_dir: str = DEFAULT_STATE_DIR, now=None) -> bool:
    """Should a PR picker (or a red-CI notice) be HELD right now? :func:`in_quiet_hours`, unless
    :func:`awake_evidence` says the owner is up (:data:`AWAKE_OVERRIDE`).

    The turns file is only read inside the window, so the rest of the day costs nothing. **These
    two functions are the only definition of "hold the picker"**: `ask_on_green` below calls this one,
    and `pr_sweep.sweep` composes the same two calls itself for its short-circuit and its red arm,
    only so its report can name the instant that stood the window down. Two reads within one pass
    can only disagree in one direction — the file is append-only and `now` is pinned, so a second
    read may find the owner awake where the first did not, never the reverse — except when the file
    turns unreadable between them, and then the guard's refusal records nothing, so it is a
    deferral, exactly as before."""
    if not in_quiet_hours(now):
        return False
    return awake_evidence(state_dir, now) is None


def ask_log_path(state_dir: str) -> str:
    return os.path.join(state_dir, ASK_LOG_FILE)


def legacy_ask_log_path(state_dir: str) -> str:
    return os.path.join(state_dir, LEGACY_ASK_LOG_FILE)


def _legacy_asks(state_dir: str) -> list:
    """The older dict-shaped `merge-ask-log.json`, read as rows. **Read-only and never rewritten**
    — it is runtime state on an install that ran it, and this module has no business deleting it.
    Folding it in is what stops a cutover from re-asking about a PR the owner was already asked
    about; it carries no repo, so those rows match tolerantly, exactly as a legacy approval does."""
    try:
        with open(legacy_ask_log_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    asks = data.get("asks") if isinstance(data, dict) else None
    rows = []
    for key, entry in (asks or {}).items() if isinstance(asks, dict) else ():
        if not isinstance(entry, dict):
            continue
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        rows.append({"schema": ASK_LOG_SCHEMA, "pr": number, "repo": None,
                     "head_sha": entry.get("head_sha"), "asked_at": entry.get("asked_at"),
                     "question_id": entry.get("question_id"), "via": entry.get("via"),
                     "legacy": True})
    return rows


def read_asks(state_dir: str) -> list:
    """**Every picker that has gone out**, oldest first. Never raises: an unreadable log means
    *nothing has been asked*, which costs at worst one duplicate picker — the safe direction, since
    the alternative is a PR that silently never gets its question.

    A torn or unparseable line is skipped rather than aborting the read, `turns.py`'s rule: one bad
    row may not hide every good one."""
    rows = _legacy_asks(state_dir)
    try:
        with open(ask_log_path(state_dir), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def asks_for(state_dir: str, pr: int, head_sha: str, repo=None) -> list:
    """Every ask row for **this PR in this repo at this commit**, oldest first.

    A row with no `repo` is a legacy one and matches tolerantly — the head SHA is what actually
    tells two same-numbered PRs apart, and two repositories sharing a commit SHA does not happen."""
    out = []
    for row in read_asks(state_dir):
        if row.get("pr") != int(pr) or row.get("head_sha") != head_sha:
            continue
        row_repo = row.get("repo")
        if row_repo and repo and repo_key(row_repo) != repo_key(repo):
            continue
        out.append(row)
    return out


def already_asked(state_dir: str, pr: int, head_sha: str, repo=None) -> bool:
    """Has a picker already gone out for **this PR at this commit**? New commits move the SHA and the
    answer flips back to no, which is the same binding the approval record uses and for the same
    reason: a question about a different artifact is a different question."""
    return bool(asks_for(state_dir, pr, head_sha, repo))


# ------------------------------------------- the SAME question, asked twice

#: **What `retired_by.how` says when one picker's answer settled another.** Spelled HERE rather than
#: in `telegram_ask` because that module treats `retired_by` exactly the way it treats `meta` — it
#: carries it and never reads it. The module that decides two records are the same question is the
#: module that owns the word for what it did about it.
SETTLED_BY_TWIN = "twin-answer"


def ask_identity(meta):
    """**`(repo_key, pr, head_sha)` — WHICH QUESTION a merge-approval picker asked**, read off a
    question record's own `meta`; `None` when the record does not carry a whole one.

    This is the tuple :func:`record_ask` keys the ask log on and :func:`record_approval` binds an
    approval to, in a form a second caller can reuse. **It exists so there is exactly one definition
    of *the same question* in this tree**: two pickers that match here are the same ask, about the
    same commit, in the same repository — and nothing weaker than that ever counts as sameness.

    **A missing part is `None`, not a tolerance — deliberately stricter than :func:`asks_for`, whose
    legacy rows match on an absent repository.** The two polarities are opposite and that is the
    whole reason. There, a wrong answer costs at worst a duplicate picker; here it would settle a
    question the owner is still owed, and `picker_retire`'s standing rule applies with full force: *a
    wrongly-retired live question is far worse than a stale one.* `head_sha` is not optional for the
    same reason — a different commit is a different question, which is
    `../docs/concurrent-pr-collisions-spec.md`'s re-ask-at-a-new-head binding, and nothing built on
    this tuple may be the thing that defeats it.

    **It is not :func:`_question_confirms`, and does not replace it.** That function answers a
    different question — *does the store corroborate this approval?* — and has to name which field
    disagreed, in a sentence a denied merge prints. It also keeps the legacy repo tolerance, which
    it must: rewriting it onto this stricter tuple would newly deny approvals that verify today. A
    test binds the two, so a change here that made them disagree goes red.

    `bool` is excluded explicitly because `isinstance(True, int)` is `True`, and `#True` is not a
    pull request — `picker_retire.pending_pr_pickers`'s guard, for its own reason."""
    if not isinstance(meta, dict) or meta.get("kind") != APPROVAL_META_KIND:
        return None
    pr = meta.get("pr")
    if not isinstance(pr, int) or isinstance(pr, bool):
        return None
    repo = meta.get("repo")
    key = repo_key(repo) if isinstance(repo, str) else ""
    if not key:
        return None
    head = meta.get("head_sha")
    if not isinstance(head, str) or not head.strip():
        return None
    return (key, pr, head.strip())


def twin_settlements(store, qid) -> list:
    """**The records that asked the same question as `qid` and are still waiting for an answer the
    owner has already given.** One `{question_id, reason, by}` per record — the argument list for
    `telegram_ask.mark_settled` — and `[]` for every case that is not certain.

    ## The case this exists for

    A picker whose send failed (its record carries `message_id: null`) never reaches the owner's
    phone; a second picker for the identical question at the identical head goes out later and is
    answered, and the PR merges. The gate worked — but the failed send survives as an unanswered
    record for the same `(repo, pr, head_sha)`, the oldest in the store, and is eventually shown to
    the owner again so they answer the same question a second time. This function settles it the
    moment the twin is answered.

    ## There is no inference here, and that is the design

    A twin is **literally the same question at the same commit** — :func:`ask_identity` on both
    records, compared whole. A different head SHA is a different question and is left alone; an
    absent repository or head yields no identity and settles nothing; a record of any other
    `meta.kind` is not a candidate. **Only an ANSWER propagates**: `qid` must itself carry
    `answered_at`, so a retirement can never cascade and neither can a settle (nothing here reads
    `retired_at` as evidence of anything except *already handled, skip it*).

    Pure and side-effect free: a store dict in, a list of arguments out. The write is
    `telegram_ask`'s, in one save, and the ordering guarantee that matters — the owner's answer is
    on disk before any of this runs — belongs to its caller."""
    questions = store.get("questions") if isinstance(store, dict) else None
    if not isinstance(questions, dict):
        return []
    source = questions.get(qid)
    if not isinstance(source, dict) or not source.get("answered_at"):
        return []
    want = ask_identity(source.get("meta"))
    if want is None:
        return []
    out = []
    for other, q in sorted(questions.items()):
        if other == qid or not isinstance(q, dict):
            continue
        if q.get("answered_at") or q.get("retired_at"):
            continue
        if ask_identity(q.get("meta")) != want:
            continue
        meta = q.get("meta") or {}
        # The record's OWN repository string, not the folded key: this sentence is what a tap on the
        # settled picker shows the owner, and `example__repo` is a filename, not a repository.
        where = f"{meta.get('repo')} " if meta.get("repo") else ""
        out.append({
            "question_id": other,
            # It states what happened and stops, `picker_retire.settled_text`'s rule: it never says
            # the owner was slow, never says they missed anything, and never implies the duplicate
            # was theirs to notice. The picker went stale on the assistant's side.
            "reason": (f"{where}#{meta.get('pr')} at {want[2][:12]} — you already answered this "
                       f"question on another picker for the same commit."),
            "by": {"how": SETTLED_BY_TWIN, "by": "merge_guard.twin_settlements",
                   "question_id": qid, "answered_at": source.get("answered_at")},
        })
    return out


def record_ask(state_dir: str, pr: int, head_sha: str, question_id: str, via: str,
               repo=None, now=None) -> None:
    """Note that a picker landed. **Writes no approval and touches nothing under
    :data:`APPROVALS_DIR`** — this records that the owner was *asked*, which is the opposite of a
    decision.

    **APPENDS. Every send gets a row, and no row is ever overwritten.** A ledger keyed by PR lets a
    hand-sent picker, seconds after the watcher auto-sent one, clobber the earlier row — **one**
    entry for **two** live pickers: the owner gets both buzzes and the file says one. A record that
    can be silently reduced to one row cannot be used to find out that two things happened, which
    is the only thing anyone would read it for.

    Fail-open, this directory's house rule for `state/` writers: a log we cannot write costs at worst
    a duplicate question, never the question. Only ever called after a send the Bot API accepted."""
    row = {"schema": ASK_LOG_SCHEMA, "pr": int(pr), "repo": str(repo) if repo else None,
           "head_sha": str(head_sha), "asked_at": _stamp(now),
           "question_id": str(question_id or ""), "via": via}
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(ask_log_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError):
        pass


def prune_asks(state_dir: str, days: int = ASK_LOG_RETENTION_DAYS, now=None) -> int:
    """Drop ask rows older than `days` and rewrite the log. Returns how many were dropped.

    Aged the way the other delivery logs are aged (Dream is where it runs): **a row whose `asked_at`
    will not parse is KEPT**, because a GC must never be the thing that loses the record of a
    question the owner was actually asked, and `days <= 0` keeps everything.
    Build-then-`os.replace`, so a crash mid-prune leaves the old file intact. **The legacy
    `merge-ask-log.json` is not touched on any path.**

    30 days is far beyond the 24 h approval TTL, so a pruned row can never be the thing that
    suppresses a picker: by the time a row ages out, anything it was gating expired weeks ago."""
    if not days or days <= 0:
        return 0
    path = ask_log_path(state_dir)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return 0

    kept, dropped = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            at = _parse_stamp(row.get("asked_at")) if isinstance(row, dict) else None
        except ValueError:
            at = None
        if at is not None and at < cutoff:
            dropped += 1
            continue
        kept.append(line if line.endswith("\n") else line + "\n")
    if not dropped:
        return 0
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(kept)
    os.replace(tmp, path)
    return dropped


# --------------------------------------------------------------------------- what the picker SAYS

def deploys_on_merge(repo, base) -> bool:
    """Does merging this PR actually deploy anything? See :data:`DEPLOY_ON_MERGE`.

    **Unknown is NO**, on both halves. The sentence is a claim about consequences, and a claim the
    owner cannot act on is worth less than silence."""
    want = deploy_on_merge_map().get((repo or "").strip().lower())
    return bool(want) and (base or "").strip() == want


def is_assistant_repo(repo) -> bool:
    """Is this the running assistant's OWN repository — the one whose prompt files are what it
    executes?

    Read off :func:`deploy_on_merge_map`'s keys rather than a second setting, so "which repository
    is the assistant's" has exactly one source (`DeployClaimIsRepoConditionalTest` pins this).
    Repo-only, not branch-keyed like :func:`deploys_on_merge`: a `seneschal/modes/chat.md` merged into
    the integration branch deploys nothing yet, but it is still the assistant's prompt and not some
    other agent's. **Unknown is NO** — the phrase that depends on this says less when it cannot tell."""
    return (repo or "").strip().lower() in deploy_on_merge_map()


def assistant_label() -> str:
    """How the picker names the running assistant: `identity_common.assistant_name` (the persona's
    configured name, else *"the assistant"*). Lazy and never raises — a name is a courtesy, and a
    failure to read one costs the name, never the sentence."""
    try:
        import identity_common
        return identity_common.assistant_name(identity_common.load_identity())
    except Exception:  # noqa: BLE001
        return "the assistant"


def pr_link_line(repo, pr, url="") -> str:
    """The tappable link, or `""` when one cannot be vouched for.

    **The URL is REBUILT from `(repo, number)`, not forwarded from `gh`'s string.** Both of those are
    already load-bearing and already validated — `repo` came out of :func:`repo_from_url`, `pr` was
    checked against the number `gh` answered about — so reconstructing costs nothing and means no
    text from the response can reach the owner as a link. The result is then matched against
    :data:`_SAFE_PR_URL_RE` and dropped if it does not fit, which is the same *cannot tell ⇒ do not*
    polarity as the rest of stage 2.

    **It goes out BARE, on its own line, and that is a decision about BOTH format modes** rather than
    an assumption about either. `TELEGRAM_FORMAT` is `plain` by default and `markdown` where the
    untracked `telegram.env` opts in, and no PR can change which:

      * **plain** — Telegram's client auto-detects a bare URL and links it. A Markdown `[text](url)`
        would arrive as literal brackets.
      * **markdown** — `telegram_format.to_html` leaves a bare URL untouched (verified against the
        converter, not assumed: `_` opens emphasis only off a non-word character, and every `_` in a
        GitHub path follows one), and Telegram auto-detects it in HTML mode too.

    A repository literally named `_x_` is the one shape where that reasoning fails — there the
    leading `_` *does* follow a non-word character and the emphasis fires. So the test is **the
    converter's own answer, not the argument about it** (:func:`_survives_markdown`), and the rare
    failing URL falls back to `[url](url)`, which renders as a real link under markdown and as a
    still-auto-detected URL in brackets under plain. Ugly beats broken; neither is silent.
    """
    slug, number = (repo or "").strip(), int(pr)
    built = f"https://github.com/{slug}/pull/{number}" if slug else str(url or "").strip()
    if not _SAFE_PR_URL_RE.match(built):
        return ""
    return built if _survives_markdown(built) else f"[{built}]({built})"


_SAFE_REPO_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def picker_title(pr: int, repo, title: str) -> str:
    """The approval picker's title line — **the same string at the top and at the bottom.**

    The leading *"Merge <repo> PR #<n>"* is **bold**, the repository is a link to the repository
    and *"PR #<n>"* is a link to the pull request; the rest of the line (*" — <title>?"*) stays
    unformatted. And the whole line is repeated under the last option (:func:`request_argv` passes
    it as `--footer`), so a long picker can be read from either end without scrolling. One
    function, so the two copies cannot drift.

    **The URLs are rebuilt from `(repo, number)` and matched against a closed shape**, exactly as
    :func:`pr_link_line` does for the bare link — no string off the `gh` response reaches the owner
    as an anchor. A slug that does not fit keeps the plain words: the line loses its links, never its
    facts. No repo at all (the `gh` read could not name one) is *"Merge PR #N"*, bold, unlinked.

    **Format modes.** Under `TELEGRAM_FORMAT=markdown`, `telegram_format`
    renders `**…**` as `<b>` and `[text](url)` as `<a>`, nested, verified against the converter
    (`PickerTitleIsBoldAndLinkedTest`). Under plain, and on the once-only plain re-send after a
    rejected conversion, the markup goes out **literally**: `telegram_ask._send_text` re-sends the
    unconverted source, stripping nothing. That is the accepted trade — the line still reads as
    *Merge <owner>/<repo> PR #717 — title?* with brackets and URLs around the two names, and the
    bare *"Read it in full"* URL below is untouched either way."""
    slug, number = (repo or "").strip(), int(pr)
    repo_url = f"https://github.com/{slug}" if slug else ""
    pr_url = f"{repo_url}/pull/{number}" if slug else ""
    if slug and _SAFE_REPO_URL_RE.match(repo_url) and _SAFE_PR_URL_RE.match(pr_url):
        where, number_text = f"[{slug}]({repo_url}) ", f"[PR #{number}]({pr_url})"
    else:
        where, number_text = (f"{slug} " if slug else ""), f"PR #{number}"
    return f"**Merge {where}{number_text}** — {title}?"


def _survives_markdown(url: str) -> bool:
    """Does `telegram_format.to_html` leave `url` byte-identical?

    **Imported lazily**, exactly as `telegram_ask` is: this module is a hook on every shell call and
    `ImportWeightTest` pins its module scope to the standard library. An import that fails answers
    `True` — the plain path is the default and does not convert at all, so a converter we cannot
    consult is a converter that is not running."""
    try:
        import telegram_format as tf
        return tf.to_html(url) == url
    except Exception:  # noqa: BLE001 — a formatting question may never cost the picker
        return True


def _cut_at_footer(text: str) -> str:
    m = _BODY_FOOTER_RE.search(text)
    return text[:m.start()] if m else text


def _lead_section(text: str) -> str:
    """The lead-in — everything before the first heading or horizontal rule.

    That is where a PR body puts its *what this is*, and it is what a reader skims. **When the body
    opens with a heading the lead-in is empty**, so leading headings are stepped over and the first
    section that has content wins; otherwise a body shaped `## Summary\\n…` would summarize as
    nothing at all."""
    lines = text.split("\n")
    out: list = []
    started = False
    for line in lines:
        if _SECTION_BREAK_RE.match(line):
            if started and any(l.strip() for l in out):
                break
            out = []           # a heading before any content: start again underneath it
            continue
        out.append(line)
        started = started or bool(line.strip())
    return "\n".join(out)


def summarize_pr_body(body, limit: int = BODY_SUMMARY_CHARS, title: str = "") -> str:
    """The PR description, reduced to what answers *"what is this change?"* — or `""`.

    **Two bands: the lead-in, then a section-by-section outline.** The lead-in is where a body says
    what it is **only when its author wrote one**. An author who wrote a pointer instead (*"implements
    phases 2 and 3 of the spec"*) gets that pointer faithfully relayed, while the sentences that
    explain it sit three headings further down the same body, unread. `pr_digest` reads them, and
    resolves the ordinals in the lead-in against them so the explanation lands under the reference.
    `title` is passed in for that resolution only — a title that says *"phases 2-3"* is a promise the
    outline then has to keep — and never reaches the returned text.

    A picker that gives the title, the path count and the head SHA — every fact about the artifact
    and not one about the change — can only be answered properly by leaving Telegram and opening
    GitHub. That is the one-tap affordance defeating itself.

    **What is kept:** the lead-in section, which is where a body says what it did, and then every
    heading it has, each with its first sentence, until :data:`OUTLINE_CHARS` runs out. A body with
    no headings gets the lead-in alone.

    **What is dropped, and why each is noise rather than content:** the `Generated with Claude Code`
    / `Co-Authored-By` footer (machine-appended), HTML comments (a PR template's instructions to the
    author), checklist boilerplate, and fenced code blocks — a pasted diff or log is the largest
    thing a body can hold and the least likely to be the sentence the owner needs, and dropping a fence
    *whole* is also what stops half of one surviving truncation.

    **THE BODY IS TEXT FROM OUTSIDE THIS PROCESS AND IS TREATED AS SUCH.** Four defences, at four
    different layers, because they fail differently:

      * *It cannot forge an option.* The summary is interpolated into the ONE `--question` argv
        element. :func:`request_argv` builds a list and `subprocess.run` is handed that list, so
        there is no shell and no re-lexing: a body line reading `--option Approve|Merge everything`
        is a line of the question's text and can be nothing else. `RequestArgvTest` round-trips the
        argv through `telegram_ask`'s own parser, so the option count is asserted rather than
        assumed.
      * *It cannot reach the `--meta` JSON.* Nothing here is put in `meta`; the approval's meaning
        stays `(kind, pr, repo, head_sha, approve_index)`, all of them the guard's own facts.
      * *It may not wear the picker's clothes.* `1. ` at a line start is exactly what
        `telegram_ask.render_body` prints for an option, so those lines are rewritten to bullets.
        The content survives; the costume does not. Control characters go the same way.
      * *It may not spend the assistant's citations.* `telegram_ask` **resolves** any
        document or section reference in a picker and refuses to send one it cannot show
        (`ask_citations.py`). A PR title and a PR description routinely cite `.md` files and
        §-numbers, and one that names a file the PR is itself DELETING would otherwise make the
        merge picker un-sendable — the gate failing hardest on exactly the PRs that need it. So
        :func:`request_argv` passes both bands as `--quote` spans: **relayed text is exempt, the
        assistant's own framing is not.** It is an exemption for a SPAN, so nothing about the layout, the
        ordering or the byte arithmetic below changes.

    Markup needs no defence here and is deliberately given none: `telegram_format.to_html` escapes
    `<`, `>` and `&` in text before it emits a tag, refuses a link whose scheme it cannot vouch for,
    and `render_for_api` returns `None` — sending the original as plain text — on any conversion that
    raises or overflows. A body that breaks the converter costs the *formatting*, never the message.
    """
    scrubbed = scrub_pr_body(body)
    if not scrubbed:
        return ""
    lead = _truncate(_tidy(_lead_section(scrubbed)), min(LEAD_SUMMARY_CHARS, limit))
    room = limit - len(lead) - len(OUTLINE_HEADER) - (4 if lead else 2)
    wanted = references_in((title or "") + "\n" + lead)
    lines, dropped = _outline_lines(scrubbed, min(OUTLINE_CHARS, room), wanted=wanted, avoid=lead)
    if lines and dropped > 0:
        more = OUTLINE_MORE % (dropped, "" if dropped == 1 else "s")
        if sum(len(l) + 1 for l in lines) + len(more) <= min(OUTLINE_CHARS, room):
            lines = lines + [more]
    band = "\n".join(lines)
    if lead and band:
        return "%s\n\n%s\n%s" % (lead, OUTLINE_HEADER, band)
    if band:
        return "%s\n%s" % (OUTLINE_HEADER, band)
    return lead


def scrub_pr_body(body) -> str:
    """The body with everything that is noise rather than description removed — and **nothing else
    done to it**. Split out of :func:`summarize_pr_body` so the lead-in and the outline read the
    same scrubbed text; a body scrubbed twice by two rules is two descriptions that can disagree."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""
    text = _CONTROL_RE.sub("", text)
    text = _HTML_COMMENT_RE.sub("", text)
    text = _cut_at_footer(text)
    text = _FENCE_RE.sub("", text)
    text = _CHECKBOX_RE.sub("", text)
    # **AHEAD of the lead-in split, not after it.** The outline reads the whole body, so a `1. ` at
    # a line start below the first heading would otherwise reach the message still wearing an
    # option's clothes.
    return _OPTION_SHAPED_RE.sub(r"\1- ", text)


def _tidy(text: str) -> str:
    text = "\n".join(line.rstrip() for line in (text or "").split("\n"))
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


def _digest():
    """`pr_digest`, or None. **Imported here and not at module scope** — `ImportWeightTest` pins this
    module's imports to the standard library because it is a hook on every shell call on this
    machine, and `pr_digest` reaches `ask_citations` in turn.

    A failure answers None and the picker degrades to the lead-in alone, which is exactly the message
    that shipped the day before. Same polarity as everything else below the header: **a picker that
    does not arrive is far worse than a terse one.**"""
    try:
        import pr_digest
        return pr_digest
    except Exception:  # noqa: BLE001 — a missing sibling may never cost the question
        return None


def references_in(text: str) -> list:
    """The ordinal labels a piece of picker text promises to explain. `[]` when `pr_digest` is
    unavailable, which makes the outline fall back to plain document order."""
    mod = _digest()
    return [r["label"] for r in mod.ordinal_references(text)] if mod else []


def _outline_lines(scrubbed: str, room: int, wanted=None, avoid: str = ""):
    """The outline band. `room` under :data:`OUTLINE_MIN_ROOM` yields nothing rather than one
    stub — a single truncated heading is the tease this whole change is against."""
    mod = _digest()
    if not mod or room < OUTLINE_MIN_ROOM:
        return [], 0
    return mod.outline(scrubbed, room=room, entry_chars=OUTLINE_ENTRY_CHARS,
                       max_entries=OUTLINE_MAX_SECTIONS, wanted=wanted, avoid=avoid)


def unexplained_line(question_text: str, summary: str) -> str:
    """The `Named but not defined` line, or `""`. See :data:`UNEXPLAINED_HEADER` for the polarity
    argument and `pr_digest`'s docstring for why only the worded family is ever named."""
    mod = _digest()
    if not mod or not summary:
        return ""
    lines = [l for l in summary.split("\n") if l.startswith("• ")]
    missing = mod.unexplained(question_text, lines)
    if not missing:
        return ""
    shown = ", ".join('"%s"' % m for m in missing[:UNEXPLAINED_MAX])
    extra = len(missing) - UNEXPLAINED_MAX
    return "%s %s%s." % (UNEXPLAINED_HEADER, shown, " (+%d more)" % extra if extra > 0 else "")


def overlapping_prs(pr: int, facts: dict, lister=None, now=None) -> list:
    """The other open PRs that change some of the same files. **`[]` on every failure there is.**

    **Imported lazily, exactly as `pr_digest` is** — this module is a `PreToolUse` hook spawned with
    no argv on every Bash and PowerShell call on this machine, `ImportWeightTest` pins its module
    scope to the standard library, and an `ImportError` at module scope would break the hook before
    `main()` could apply either failure polarity. The hook's own decision path never reaches here.

    **Failure means saying nothing, and that direction is the design.** An overlap block is
    information; a picker is a merge that can happen. `gh` missing, rate-limited, slow, offline or
    unparseable therefore costs the block and never the question — the picker goes out unwarned,
    exactly as it did before this shipped. `watch_pr.py`'s rule unchanged: *"asking is not merging
    and may not cost the verdict."*

    `lister` is the one seam, handed straight to `pr_overlap`'s own `gh` runner, so no test here can
    reach the network and `request --overlap-from` can replay a captured list."""
    try:
        import pr_overlap
    except Exception:  # noqa: BLE001 — a missing sibling may never cost the question
        return []
    try:
        return pr_overlap.find(int(pr), facts.get("repo"), facts.get("paths") or [],
                               runner=lister, now=now)
    except Exception:  # noqa: BLE001 — belt and braces; `find` already promises not to raise
        return []


def _overlap_paths(shared: list) -> str:
    """The shared paths on one line — capped by count **and** by characters, with the remainder named
    either way. Never a silent cut: `pr_sweep`'s `deferred` rule, applied to a shorter list."""
    shown, used = [], 0
    for path in shared[:OVERLAP_MAX_PATHS]:
        if shown and used + len(path) + 2 > OVERLAP_PATHS_CHARS:
            break
        shown.append(path)
        used += len(path) + 2
    text = ", ".join(_clip(p, OVERLAP_PATHS_CHARS) for p in shown)
    dropped = len(shared) - len(shown)
    return text + (OVERLAP_PATHS_MORE % dropped if dropped > 0 else "")


def overlap_band(rows) -> str:
    """The overlap block, or `""`. See :data:`OVERLAP_HEADER` for why it says what it says and,
    more importantly, why it says nothing else.

    Sits directly under the `Head:` line — **above** the fenced band carrying the PR's own
    description — because it is one of the guard's own facts, and text from outside this process may
    never displace the facts it is being read against.

    Formatting follows `pr_digest`'s: the `• ` bullet is the guard's own marker, a title is joined to
    its detail rather than restated, and every cap names what it dropped."""
    lines = []
    for row in (rows or [])[:OVERLAP_MAX_PRS]:
        number = row.get("pr")
        if not isinstance(number, int):
            continue
        title = _clip(str(row.get("title") or ""), OVERLAP_TITLE_CHARS)
        draft = " (draft)" if row.get("draft") else ""
        paths = _overlap_paths([p for p in (row.get("shared") or []) if isinstance(p, str)])
        if not paths:
            continue
        head = f"• #{number}{draft}" + (f" {title}" if title else "")
        lines.append(f"{head} — {paths}")
    if not lines:
        return ""
    dropped = len(rows) - len(lines)
    if dropped > 0:
        lines.append(OVERLAP_MORE % (dropped, "s" if dropped != 1 else "",
                                     "" if dropped != 1 else "s"))
    band = "\n\n%s\n%s" % (OVERLAP_HEADER, "\n".join(lines))
    # The ceiling is a backstop the caps above already satisfy (`OverlapBandIsBoundedTest`). If it
    # ever binds, the whole block goes rather than half of it: a truncated list of which PRs overlap
    # reads as complete, which is the one thing this block may not be.
    return band if len(band) <= OVERLAP_CHARS else ""


def overlap_quotes(band: str) -> list:
    """The block's entry lines, for `ask_citations`' `--quote` exemption. **Load-bearing, and it is
    the one place this feature could have cost a picker.**

    An entry names **another pull request's changed paths and its title**, and `ask_citations`
    refuses a send it cannot resolve — so an overlapping PR that *adds* a file (every PR that adds a
    spec), renames one, or deletes one would name a path that is not in this checkout, and the
    picker would not go out at all. Verified before it shipped: a fabricated
    `seneschal/docs/some-retired-memo.md` in an overlap row made `telegram_ask` exit 2, `refused:
    citation`. **The block exists to prevent a wasted tap; costing the whole question would be
    strictly worse than the problem**, and it would fail hardest on exactly the PRs that need it.

    `--quote` is the escape hatch `MERGE_GUARD_SETUP.md` already documents for this, in these words:
    a PR title *"naming a file the PR is DELETING would otherwise make the merge picker
    un-sendable — the gate failing hardest on the PRs that need it."* Same argument, one PR over.

    **The entry lines only.** :data:`OVERLAP_HEADER` and :data:`OVERLAP_MORE` are the assistant's
    own sentences and face the citation gate like the rest of its framing — the same line the
    unexplained-reference note is on. And a quoted span is an exemption for a **span**, not a
    layout change: nothing about the message moves."""
    return [line for line in (band or "").split("\n") if line.startswith("• #")]


def _truncate(text: str, limit: int) -> str:
    """`text` within `limit` characters, **including** :data:`TRUNCATION_MARK` when it had to cut.

    Cuts at the last line break, else the last space, else mid-word — in that order, so the common
    case keeps whole bullets. A limit too small to hold the marker yields `""`: a message consisting
    only of *"(truncated)"* tells the owner nothing and still costs the budget."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    room = limit - len(TRUNCATION_MARK) - 1
    if room <= 0:
        return ""
    head = text[:room]
    for sep in ("\n", " "):
        cut = head.rfind(sep)
        if cut > room // 2:
            head = head[:cut]
            break
    return head.rstrip() + "\n" + TRUNCATION_MARK


def _clip(text: str, limit: int) -> str:
    """A single line, clipped with a bare ellipsis. The summary's marker says *"full description on
    GitHub"*, which is the right thing to say about a description and the wrong thing about a title."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


#: **The spelling that qualifies a file at the repo ROOT**, for `ask_citations.resolve_doc`.
#: Deliberately a literal here rather than an import: this module is a `PreToolUse` hook on every
#: shell call and `ImportWeightTest` pins what it drags in at module scope. The two spellings are
#: held together by a test that runs a real root-path picker through `citation_block` ITSELF, which
#: is the thing that actually refuses the send — the same reason `blocker_line`'s `.md` rule is
#: asserted against that module rather than against a predicate copied over here.
ROOT_PATH_PREFIX = "./"


def cite_path(path: str) -> str:
    """A changed path, spelled so the citation gate can resolve it.

    A path with a directory in it qualifies itself. **A path at the repo ROOT does not** — and the
    picker is where that stops being a curiosity. The root `CLAUDE.md` is a prompt path, so a PR
    touching it is ask-high, and the picker is the ONLY route to the owner's tap; but the picker
    names its blockers, `ask_citations.resolve_doc` matches that bare basename against every
    `CLAUDE.md` in the tree, and the send is REFUSED. Such a PR could then be neither merged nor
    asked about.

    `./` is the qualification that resolver honours: one place to look, exactly like a token with a
    `/` in it. It is **not** a softening of the ambiguity refusal — a bare `CLAUDE.md` is still
    refused with its candidate list intact, because the dot-slash is this function SAYING which of
    the candidates it means, and a picker that guessed would be citing a file it had not read.

    Applied to every root path and not only the `.md` ones. Only a `.md` is a citation today, but a
    line reading `./CLAUDE.md, pyproject.toml` invites its next reader to conclude the prefix says
    something about the FILE rather than about where it sits.

    The separator is normalised first even though :func:`_classify_path` already forward-slashes
    everything that reaches :func:`blocker_line`: this takes a path, and a helper that silently
    prefixed a Windows-spelled `seneschal\\scripts\\x.py` would be worse than the bug it fixes."""
    text = (path or "").replace("\\", "/")
    if not text or "/" in text:
        return text
    return ROOT_PATH_PREFIX + text


def blocker_line(blockers: list, counted: str = "", ordered=None,
                 shown_max: int = BLOCKER_SHOWN_MAX) -> str:
    """*"5 non-docs paths, all in seneschal/scripts — ask_no_access.py, mouth.py, …"*.

    **The one thing cut to pay for the outline**, and it is redundancy rather than content: a line
    that spells `seneschal/scripts/` four times spends ~56 characters telling the owner nothing the
    first one had not. Folding a shared directory out of the front buys the outline roughly a
    third of a section at no loss — the paths are still named, still counted, still capped at four
    with the overflow said out loud.

    A mixed-directory PR renders exactly as it did before. There is no partial-prefix cleverness
    here on purpose: a common prefix that is not a whole directory (`seneschal/sc`) reads as a typo.

    **The count phrase is the caller's, not this function's.** With the prompt-path denylist,
    *how many* is no longer always *"N non-docs paths"* — it may be *"2 prompt
    paths"* or *"5 paths, 3 of them prompt"*, and that wording is derived beside
    :func:`change_kind_phrase` so the count and the verb cannot describe different PRs. `counted`
    is that phrase and `ordered` is the same list in the order the picker shows it (prompt paths
    lead). Both default to this function's own pre-denylist behaviour, which is what the direct
    unit tests below call and what keeps the compression rule readable on its own.

    **`blockers` stays the whole set even when `ordered` reorders it**, because every claim here
    — the count, the overflow, and *"all in"* most of all — is about the change and not
    about the four paths that happened to fit.

    **`shown_max` is how many are named before "(+N more)"**, :data:`BLOCKER_SHOWN_MAX` by default.
    :func:`request_argv` lowers it when the `.md` paths it would name cannot all be described
    within the message — the count and the overflow are computed from the SAME number,
    so a narrower line is a shorter one, never a less true one."""
    count = len(blockers)
    plural = "s" if count != 1 else ""
    counted = counted or f"{count} non-docs path{plural}"
    shown_max = max(1, int(shown_max))
    shown = (list(ordered) if ordered is not None else list(blockers))[:shown_max]
    extra = count - shown_max
    more = f" (+{extra} more)" if extra > 0 else ""
    # **Computed over EVERY blocker, never the four shown.** "all in seneschal/scripts" is a claim about
    # the whole change; deriving it from the visible slice would make the truncation itself the
    # source of a false sentence, and an overstated picker is how a gate stops being read.
    dirs = {p.rsplit("/", 1)[0] for p in blockers if "/" in p}
    # **A `.md` is never folded, and that is the seam between this and the prompt-path
    # denylist**. Folding prints a bare basename, and in this repo a bare `.md` basename is an
    # ambiguous document reference: `ask_citations.resolve_doc` matches `CLAUDE.md` against every
    # copy in the tree and REFUSES the send. Without the denylist no `**/CLAUDE.md` could be a
    # blocker, so the two rules never met; with it, a folded `.md` would kill the picker. **A merge picker that fails to send is a green functionality PR that
    # never gets approved**, so the compression yields — a `.md` blocker keeps the qualified
    # path this repo asks of every other pointer, and the line is merely as long as it was
    # before. Tested against `ask_citations` itself rather than against this predicate.
    #
    # **The root `CLAUDE.md` is the case this rule could not reach**, and it is why
    # `cite_path` exists above: folding was never the problem there, because a path with
    # no directory is ALREADY a bare basename and there is nothing to fold out of it. The
    # unfolded branch below therefore qualifies it explicitly rather than merely
    # declining to shorten it.
    if (len(dirs) == 1 and len(shown) > 1 and all("/" in p for p in blockers)
            and not any(p.lower().endswith(".md") for p in shown)):
        where = dirs.pop()
        names = ", ".join(p.rsplit("/", 1)[1] for p in shown)
        return f"{counted}, all in {where} — {names}{more}."
    return f"{counted} — {', '.join(cite_path(p) for p in shown)}{more}."


def approve_option_text(pr: int, facts: dict) -> str:
    """The Approve option's *what it means and what it costs* — an option always says both.

    The deploy sentence is present only where merging genuinely deploys (:data:`DEPLOY_ON_MERGE`).
    Everything else about the option is unconditional, because everything else is true everywhere:
    the approval is single-use and it is bound to this exact commit."""
    # FULL, not `[:12]`: this and the daemon's relay line are what the merging agent copies into
    # `--match-head-commit`, and GitHub rejects a prefix — :func:`match_head_refusal`.
    head = facts["head_sha"]
    deploy = (f" It redeploys {assistant_label()} on the next update cycle (~10 min)."
              if deploys_on_merge(facts.get("repo"), facts.get("base")) else "")
    return (f"Approve|Merge #{pr} at {head}.{deploy} Single-use, and only this exact commit.")


def citation_reserve(cite_paths: list) -> int:
    """Characters :func:`request_argv` must NOT spend on the summary, so that `ask_citations` can
    describe every `.md` path the header names.

    **The refusal this reserves against.** A header naming four prompt `.md` blockers, beside an
    overlap band naming two other PRs, with the summary then cut to whatever fitted inside
    :data:`QUESTION_CHARS_MAX`, can leave `ask_citations` under 200 characters per excerpt against its
    200 floor. It refuses, correctly: an excerpt that short teases a section instead of describing
    it. But the refusal lands on the assistant's own header, where the only "fix" is to stop naming
    the blockers, and a picker that fails to send is a green functionality PR that never gets
    approved. Shortening the title and body does not help, because the arithmetic had no term for
    the excerpts at all.

    So the summary's room now starts from the citations' floor, not from the API limit. The sum is
    `ask_citations.min_room` — the gate's own refusal arithmetic, exported rather than copied, so
    the reserve and the refusal cannot drift apart — plus `CITATION_SAFETY_MARGIN`, which
    `telegram_ask.ask` subtracts before it hands the gate its `room`. With :data:`QUESTION_CHARS_MAX`
    already under the limit by :data:`PICKER_SCAFFOLD_RESERVE` (measured against the real
    `render_body` in `PickerFitsTheApiLimitTest`), a question that fits inside
    `QUESTION_CHARS_MAX - citation_reserve(...)` leaves the gate at least `min_room`.

    Imported here rather than at module scope for the usual reason: this module is a hook on every
    shell call, and `request_argv` is on the ask path only. If `ask_citations` cannot load, neither
    can `telegram_ask` — the gate that would need the reserve is the one that is absent — so `0` is
    the honest answer rather than a guess."""
    if not cite_paths:
        return 0
    try:
        import ask_citations as ac
    except Exception:  # noqa: BLE001 — no gate to reserve for
        return 0
    return ac.CITATION_SAFETY_MARGIN + ac.min_room(cite_path(p) for p in cite_paths)


#: **A job brief's own words, leaked into the PR they describe.** A job brief commonly ends with
#: "PR against develop. **DO NOT MERGE**" — an instruction to the AGENT (it may not merge the PR
#: itself), not a verdict about the pull request. A job that echoes that literal phrase into the PR's
#: own title instead of obeying it would, with :func:`request_argv` spelling the picker straight off
#: `gh`'s title, ask the owner to merge a PR whose own title tells them not to — and, reasonably,
#: they never tap it.
#:
#: Anchored to the START or the END of the title, never the middle — an ordinary title that happens
#: to discuss the phrase in its own sentence (*"refactor: do not merge conflicting branches
#: automatically"*) is never touched. A real hold is a GitHub DRAFT, which never reaches this
#: function at all (`pr_sweep.candidates` drops a draft before ever calling :func:`ask_on_green`), so
#: a marker seen here can only be the leaked instruction, never a decision anyone made about merging
#: this PR.
_DNM_LEAD_RE = re.compile(
    r"^\s*\[?\s*do\s+not\s+merge\s*\]?\s*:?\s*[-–—]?\s*", re.IGNORECASE)
_DNM_TAIL_RE = re.compile(
    r"\s*[-–—]?\s*\[?\s*do\s+not\s+merge\s*\]?\s*:?\s*$", re.IGNORECASE)

#: The picker's own sentence when :func:`strip_leaked_dnm_marker` found something to strip —
#: **display-only, never a GitHub edit** (`pr_sweep.py`'s house rule: this module never writes back
#: to the PR it is describing).
DNM_NOTE = ('The title carried "DO NOT MERGE" — read as a leaked job instruction (the agent may not '
            "merge it, not that the pull request itself shouldn't be), so it's shown above with that "
            "phrase removed.")


def strip_leaked_dnm_marker(title: str) -> tuple:
    """`(display_title, found)` — `title` with a leaked "DO NOT MERGE" job instruction stripped from
    its front or back, and whether there was one to strip. Case-insensitive; tolerates a trailing
    colon, a wrapping `[...]`, and a leading dash or em dash, because those are the shapes actually
    seen (`"DO NOT MERGE: …"`, `"… — DO NOT MERGE"`, `"[DO NOT MERGE] …"`). Never touches the middle
    of a title — the anchor is the whole defence, not a keyword scan.

    **A title with nothing to strip comes back byte-identical**, not merely equal after a stray
    `.strip()` — the `found` flag is `False` only when `title` itself is returned unchanged."""
    text = title or ""
    lead = _DNM_LEAD_RE.sub("", text, count=1)
    tail = _DNM_TAIL_RE.sub("", lead, count=1)
    if tail == text:
        return text, False
    return tail.strip(), True


def request_argv(pr: int, facts: dict, blockers: list, state_dir: str, env_file=None,
                 dry_run: bool = False, overlap=None, docs_only: bool = False) -> list:
    """**The one place the approval picker is spelled — and the docs-only notice too.** `request`
    and :func:`ask_on_green` both come through here, so the question the owner reads cannot depend on
    which door asked it.

    **`docs_only=True` builds a DIFFERENT PICKER, NOT A REWORDED ONE.** A docs-only PR is already
    allowed to merge on green under the standing docs-only grant — there is no approval to ask for,
    so this branch may not say "Approve", may not imply a tap is required, and may not claim merging
    waits on one. **It is an affordance, not a gate**: the picker exists so the owner has something
    to tap that speeds the merge along, rather than being told in prose that a PR is ready. Reusing
    this function's shared machinery (title clip, overlap block, PR-body summary, citation
    resolution, `--topic`) is what keeps the docs-only notice honest about the same facts the
    approval picker is. Its `meta.kind` is :data:`DOCS_ONLY_META_KIND`, never
    :data:`APPROVAL_META_KIND` — "Approve" reaching the daemon's approval clause would mint an
    approval record nothing needed, and would tell the owner one was.

    **Built in order, never spliced.** Splicing `argv[3:3] = ["--env-file", value]` lands the flag
    between `--state-dir` and its value and makes every `--env-file` invocation an argparse error.
    Appending in order is what makes that class of mistake unspellable, and `RequestArgvTest`
    round-trips the result through `telegram_ask`'s own parser so a malformed argv fails in the
    suite instead of on a live merge.

    **The question is assembled in three bands, and the order is a defence, not a layout.** The
    guard's own facts come first (repo, number, title, the non-docs paths, the head SHA); the PR
    description — the only part that came from outside this process — sits under a labelled header
    beneath them; the link closes it, immediately above the buttons. Untrusted text can therefore
    never displace the facts it is being read against, and the owner never has to scroll past it to
    find the thing they are approving. See :func:`summarize_pr_body` for the rest of that argument,
    and :func:`pr_link_line` for why the URL goes out bare.

    **The header's own blocker paths get a second place to resolve.** A path the PR CREATES cannot be
    citation-resolved against the checkout `ask_citations` reads by default — it does not exist on
    the base branch yet — so a header naming a new `.md` under a prompt-path directory would make the
    whole picker un-sendable, forever, with no wording fix. `--cite-head-repo`/`--cite-head-sha`/
    `--cite-head-path`, appended after `--quote`, are a closed allow-list: exactly the paths this
    header already prints (`cite_paths`, sliced to :func:`blocker_line`'s own display cap), never the
    PR's full changed-file list. See `ask_citations.PRHead` for where the content is actually read
    from and every way it fails closed instead of showing something unverified.

    **A PICKER THAT DOES NOT ARRIVE IS FAR WORSE THAN A TERSE ONE.** Everything below the header is
    therefore droppable and nothing above it is: the summary is cut to whatever room is left inside
    :data:`QUESTION_CHARS_MAX` and dropped entirely if that is too little to be worth reading, and
    the whole question is rebuilt without it in the case that cannot happen but must not be fatal
    if it does.

    **And the citations the header itself incurs are reserved for BEFORE the summary is cut.** Every
    `.md` path the blocker line names is a citation `ask_citations` will excerpt at no less than its
    floor or refuse outright — and the refusal lands on this function's own sentence, where `--quote`
    is not the honest answer (that exemption is for text the assistant is *relaying*, and the
    blocker line is its own framing). So :func:`citation_reserve` is taken off the top, and the
    summary gets what is left. If even an empty summary cannot pay for the `.md` paths four-wide, the
    blocker line is narrowed — three named, then two, then one — with the rest folded into its
    "(+N more)", which is the one truncation the line has always been honest about. A path not shown
    is not cited and costs nothing; a path shown is always described. A `.md` blocker is never
    spelled so the gate cannot see it: that would be a picker citing a file it does not show, which
    is the exact thing the gate exists to refuse.

    **`overlap` is data, not text, and every door must compute it.** It is the rows
    :func:`overlapping_prs` returns; :func:`overlap_band` renders them here, so the block the owner
    reads is spelled in one place like the rest of the question. `None` — the default — means *no
    block*, which is also the answer on every `gh` failure, so a caller that cannot look is
    indistinguishable from one that looked and found nothing. That default is what keeps this
    function network-free for its callers and its tests; the property that no production door
    relies on it is asserted against this file by `EveryAskDoorLooksForOverlapTest`."""
    if docs_only:
        meta = {"kind": DOCS_ONLY_META_KIND, "pr": int(pr), "repo": facts.get("repo") or None,
                "head_sha": facts["head_sha"]}
    else:
        meta = {"kind": APPROVAL_META_KIND, "pr": int(pr), "repo": facts.get("repo") or None,
                "head_sha": facts["head_sha"], "approve_index": 0}
    # The repo is NAMED in the question, not only carried in the meta. The owner is asked about PR
    # numbers from more than one repository on the same phone, and "#45" alone is not an identity.
    where = f"{facts.get('repo')} " if facts.get("repo") else ""
    display_title, dnm_found = strip_leaked_dnm_marker(str(facts.get("title") or ""))
    title = _clip(display_title, TITLE_CHARS)
    dnm_line = f"\n{DNM_NOTE}" if dnm_found else ""
    overlap_text = overlap_band(overlap)
    # The closed allow-list for `ask_citations.PRHead` (see the flags appended near the bottom of
    # this function). Stays `[]` for a docs-only notice: that header names no
    # non-docs path at all, so there is nothing here that could ever need the PR's own head.
    cite_paths: list = []
    # What the summary must leave untouched for the header's own citations — `0` until the
    # approval branch below names a `.md` path. See :func:`citation_reserve`.
    cite_reserve = 0
    link = pr_link_line(facts.get("repo"), pr, facts.get("url"))
    tail = f"\n\n{LINK_HEADER}\n{link}" if link else ""
    if docs_only:
        # **NO "?"** — the approval header ends on a question because there is a decision to make;
        # this one states a fact. **NO "ask-high"** — the whole point is that it is not.
        header = (f"{where}PR #{pr} — {title} — is docs-only and green.\n\n"
                  f"It already merges on green under the standing docs-only grant, so this is a "
                  f"heads-up, not an approval ask — it can merge with or without a tap here.\n"
                  f"Head: {facts['head_sha']}"
                  f"{overlap_text}"
                  f"{dnm_line}")
        footer = ""
    else:
        # **Prompt paths lead the shown list.** Only four fit, and a `seneschal/modes/*.md` sitting
        # behind ten `.py` files is the one blocker the owner cannot guess from the title.
        # **Only in the assistant's own repository.** In any other, a prompt-shaped path is counted
        # and listed as code, so *"N prompt paths"* below can never disagree with the verb
        # :func:`change_kind_phrase` picks.
        prompt = prompt_paths(blockers) if is_assistant_repo(facts.get("repo")) else []
        ordered = prompt + [p for p in blockers if p not in prompt]
        if prompt and len(prompt) < len(blockers):
            counted = (f"{len(blockers)} path{'s' if len(blockers) != 1 else ''}, {len(prompt)} of "
                       f"them prompt")
        elif prompt:
            counted = f"{len(prompt)} prompt path{'s' if len(prompt) != 1 else ''}"
        else:
            counted = f"{len(blockers)} non-docs path{'s' if len(blockers) != 1 else ''}"
        # **Bold, linked, and repeated at the foot** — see :func:`picker_title`. The
        # footer is passed to `telegram_ask` separately (`--footer`) because it has to land UNDER
        # the options, which this function never renders; `footer_band` below is counted against
        # the same budget as everything else so the reserve still holds. It is never truncated: a
        # title line is already clipped at `TITLE_CHARS`, and a half-footer would read as a
        # different PR.
        footer = picker_title(pr, facts.get("repo"), title)
        # **Four blockers named, or as many as the message can also DESCRIBE.** Each `.md` in the
        # shown slice is a citation the gate will excerpt at its floor or refuse the picker over,
        # and the refusal cannot be quoted away — the blocker line is the assistant's own
        # sentence. So the width is settled here, against the reserve those citations need, before
        # the summary is cut: four if the header, link, footer and reserve leave the summary any
        # room at all (even none), else three, then two, then one. Narrowing moves a path into
        # "(+N more)", the overflow the line has always named, so nothing shown is ever uncited
        # and nothing uncited is ever shown.
        for shown_max in range(BLOCKER_SHOWN_MAX, 0, -1):
            # **The exact paths `blocker_line` is about to print, and nothing wider.** The slice
            # matches its display width — a path named only inside "(+N more)" is not "already
            # listed in the header", and letting IT resolve from head would widen what is citable
            # rather than fix the refusal the head-citation allow-list is scoped to. Filtered to `.md`
            # because that is the only extension `ask_citations.DOC_RE` ever scans for; a
            # `.py`/`.json` entry here would just never be looked up, so leaving it in would be
            # inert, not unsafe — filtered anyway for honesty about what this list is for.
            cite_paths = [p for p in ordered[:shown_max] if p.lower().endswith(".md")]
            cite_reserve = citation_reserve(cite_paths)
            line = blocker_line(blockers, counted=counted, ordered=ordered, shown_max=shown_max)
            header = (f"{footer}\n\n"
                      f"It {change_kind_phrase(blockers, prompt, facts.get('repo'))}, so it is "
                      f"ask-high: {line}\n"
                      f"Head: {facts['head_sha']}"
                      # The overlap block is one of the guard's OWN facts, so it goes inside the
                      # header band — above the fenced description, and undroppable the way the
                      # rest of the header is. The summary's `room` below is computed off
                      # `len(header)`, so the block automatically takes its characters from the
                      # description rather than from the API limit.
                      f"{overlap_text}"
                      f"{dnm_line}")
            if (QUESTION_CHARS_MAX - cite_reserve - len(header) - len(tail)
                    - len(f"\n\n{footer}") - len(SUMMARY_HEADER) - 2) >= 0:
                break

    # The approval picker's footer: the title line again, under the last option. NOT a band of the
    # question — `telegram_ask` places it after the options and after any citation block — but its
    # characters come out of the same budget, so the summary's room shrinks by it and the API
    # reserve measured in `PickerFitsTheApiLimitTest` still covers the scaffolding.
    footer_band = f"\n\n{footer}" if footer else ""
    # **The ceiling on this question, and it is lower than the API's by exactly what the header's
    # own citations will need** — see :func:`citation_reserve`. Every clamp below reads this, not
    # `QUESTION_CHARS_MAX`, so no band can spend the excerpts' characters.
    question_max = QUESTION_CHARS_MAX - cite_reserve
    # The summary gets what is left after everything that may not be dropped. `- 2` is the blank
    # line between the header and the summary's own header.
    room = question_max - len(header) - len(tail) - len(footer_band) - len(SUMMARY_HEADER) - 2
    summary = summarize_pr_body(facts.get("body"), min(BODY_SUMMARY_CHARS, room), title=title)
    body_band = f"\n\n{SUMMARY_HEADER}\n{summary}" if summary else ""
    # The note reads the picker's OWN words — the title and the summary as they will actually be
    # sent — rather than the PR body, because the promise the owner is owed is about the sentence in
    # front of them and not about a paragraph that got truncated away.
    note = unexplained_line(f"{title}\n{summary}", summary)
    note_band = f"\n\n{note}" if note and len(header) + len(body_band) + len(tail) + len(note) + 2 \
        + len(footer_band) <= question_max else ""

    question = f"{header}{body_band}{note_band}{tail}"
    if len(question) + len(footer_band) > question_max:  # unreachable by the arithmetic above
        question = f"{header}{tail}"[:question_max - len(footer_band)]

    argv = [sys.executable, os.path.join(SCRIPT_DIR, "telegram_ask.py")]
    if env_file:
        argv += ["--env-file", env_file]
    # The two spans that came from outside this process, named for the citation gate. `title` and
    # `summary` are the POST-clip strings — the ones actually interpolated above — because a span
    # that does not occur in the question is refused rather than ignored, and handing over the
    # pre-clip title would turn every long-titled PR into an un-sendable picker.
    #
    # **Filtered against the question that was actually built**, for the same reason one step
    # further on: the rebuild branch above drops the summary band and slices the header, so a span
    # named unconditionally could be one the message no longer contains — and `ask_citations`
    # refuses a `--quote` it cannot find. That path is unreachable today; it would have cost the
    # picker if it ever became reachable, which is the wrong way for this to fail.
    #
    # The unexplained-reference note is deliberately NOT quoted, and neither is the overlap block's
    # header. Both are the assistant's own sentences, so they face the citation gate like the rest
    # of its framing. The block's ENTRY lines are — they relay another pull request's title and changed
    # paths, and a PR that adds or deletes a file would otherwise make this picker un-sendable. See
    # :func:`overlap_quotes`.
    quoted = [s for s in (title, summary, *overlap_quotes(overlap_text)) if s and s in question]
    if docs_only:
        # **THE RECOMMENDATION STAYS ON, UNLIKE THE APPROVAL PICKER'S.** There the first-option mark
        # would be the assistant recommending its OWN merge of a change that needed the owner's
        # judgment — the thing the gate exists to keep out. Here there is no judgment to keep out: the merge is already
        # allowed, so "merge it now" is an honest recommendation rather than a smuggled decision.
        argv += [
            "--state-dir", state_dir, "ask", "--question", question,
            "--option", "Merge it now|Say the word and I'll run it — it already qualifies to merge "
                        "on green with no approval needed, so this only saves the wait until I "
                        "notice on my own.",
            "--option", "No need|Nothing changes either way — it stays green and queued, and merges "
                        "with or without a tap here.",
            "--meta", json.dumps(meta),
        ]
    else:
        argv += [
            "--state-dir", state_dir, "ask", "--question", question,
            # NOT the first-option-is-the-recommendation shape. The picker convention marks the
            # first option `(Recommended)`; here that would be the assistant recommending its own
            # merge, which is the judgment the gate exists to keep out. `--no-recommendation` is the
            # convention's escape hatch for a genuinely open pick, and this is one.
            "--no-recommendation",
            "--option", approve_option_text(int(pr), facts),
            "--option", "Not now|Nothing merges. The PR stays open and I'll hold it until you say "
                        "otherwise.",
            "--meta", json.dumps(meta),
            # The title line again, under the last option (`picker_title`). It contains the relayed
            # `title`, and `ask_citations._mask_spans` masks EVERY occurrence of a quoted span, so
            # the one `--quote` below covers both copies.
            "--footer", footer,
        ]
    for span in quoted:
        argv += ["--quote", span]
    if cite_paths and facts.get("repo") and facts.get("head_sha"):
        # A cited path is looked up in the PR's tree, not the base branch, so a file the PR creates
        # resolves. That keeps the guard's actual promise — never show a citation it cannot open —
        # because it CAN open it, from the head. `cite_paths` is already the closed list — exactly
        # what the header above just printed, never the PR's full changed-file list — so this cannot
        # widen what `ask_citations` will resolve, only give it a second place to look for paths it
        # already named.
        argv += ["--cite-head-repo", facts["repo"], "--cite-head-sha", facts["head_sha"]]
        for p in cite_paths:
            argv += ["--cite-head-path", p]
    # **THE TOPIC.** Pickers buried in one day's chat are hard to scroll back to, and merging is the
    # owner's alone — a picker they cannot find is a merge that cannot happen. The purpose string is
    # `telegram_topics`' own constant rather than a literal, so the routing table has ONE spelling;
    # `telegram_ask.py` does the resolving and is contracted to fall back to the main chat for every
    # failure there is, so this flag can only ever change WHERE the picker lands, never WHETHER.
    #
    # Imported HERE, exactly as `telegram_ask` and `telegram_format` are: this module is a hook on
    # every shell call and `request_argv` is on the ask path only. An import that fails costs the
    # topic and not the picker.
    #
    # **THE FLAG STAYS EXPLICIT EVEN THOUGH `decisions` IS THE DEFAULT TOPIC**, and that is the point
    # rather than an oversight: a merge approval is the one picker that has its own home, and the
    # default exists for everything that does not. Reading this as redundancy and deleting the flag
    # would silently move every merge ask into the decisions topic.
    try:
        import telegram_topics as tt
        argv += ["--topic", tt.TOPIC_PULL_REQUESTS]
    except Exception as e:  # noqa: BLE001 — a picker in the wrong topic beats no picker
        # Without the flag `telegram_ask` applies its own default, so the ask lands in `decisions`
        # rather than in the main chat — which is why this line says *not in its own topic* rather
        # than naming a destination this module can no longer predict.
        print(f"! telegram_topics unavailable ({e}); the picker goes out without its own topic",
              file=sys.stderr)
    if dry_run:
        argv.append("--dry-run")
    return argv


def _send_question(argv: list):
    """Run the picker. The one subprocess seam for the send, so a test replaces one thing."""
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout, proc.stderr


def _sent_question_id(stdout: str):
    """`telegram_ask.py`'s one-line JSON -> `(ok, question_id, payload)`. Unreadable output is not a
    landed send: we would be recording an ask we cannot prove happened."""
    try:
        payload = json.loads((stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError, TypeError):
        return False, None, {}
    if not isinstance(payload, dict):
        return False, None, {}
    return bool(payload.get("ok")), payload.get("question_id"), payload


def ask_on_green(pr: int, repo=None, state_dir: str = DEFAULT_STATE_DIR, env_file=None,
                 runner=None, now=None, dry_run: bool = False, sender=None, lister=None) -> dict:
    """When a PR the guard would block reaches a terminal green CI verdict, the approval picker goes
    out **without an agent remembering to ask**.

    **And a DOCS-ONLY PR gets a picker too — a notice, never a gate.** The standing grant already
    lets a docs-only PR merge on green with no approval; reading that as "nothing to ask" and
    stopping silently would leave the owner with no affordance at all, only the assistant saying in
    prose that a PR is ready — prose they cannot tap. **THE GRANT IS UNTOUCHED — a docs-only merge
    still never waits on this picker**, on the tap, or on this function running at all; see
    :func:`request_argv`'s `docs_only=True` branch for why the wording, options and `meta.kind` all
    differ from the approval picker's rather than merely being reworded.

    **It asks; it does not re-judge CI.** Its callers (`watch_pr.py`, `pr_sweep.py`) call it only on
    a terminal green verdict, and the merge itself is refused on red or pending by
    :func:`ci_refusal` whatever this function did — so a second CI reading here would be a second
    opinion that could only drift.

    Returns `{"ok", "sent", "pr", "reason", "docs_only", …}` and **never raises** — its caller is a CI
    watcher whose verdict is the deliverable, and an ask that fails may not turn a green report red.

    **Nothing here can lower the gate.** It writes no approval on any path — :func:`verify_approval`
    is consulted only on the non-docs-only branch, to stay quiet when one already exists, since a
    docs-only PR has no approval to already have on file — and reuses :func:`non_docs_paths` — the
    guard's own classifier — rather than a second one that could drift into calling a functional PR
    docs-only. A send that fails records nothing, so the outcome is blocked-and-couldn't-ask, exactly
    as it is today, and the next watcher retries.

    `lister` is the overlap lookup's seam, threaded rather than resolved here so a test that reaches
    this door cannot reach `gh` either. The lookup itself may fail freely: it costs the block and
    never the picker (:func:`overlapping_prs`)."""
    try:
        facts = pr_facts(int(pr), repo=repo, runner=runner)
    except GuardError as e:
        return {"ok": False, "sent": False, "pr": pr, "reason": f"could not read PR #{pr}: {e}"}
    except Exception as e:  # noqa: BLE001 — a watcher's verdict may not die on the ask
        return {"ok": False, "sent": False, "pr": pr, "reason": f"could not read PR #{pr}: {e!r}"}

    head, slug = facts["head_sha"], facts.get("repo")
    base = {"ok": True, "sent": False, "pr": int(pr), "repo": slug, "head_sha": head}

    # **`not_open_refusal`, not a second spelling of it.** `request` refuses on exactly this
    # predicate, and two copies of "is it still open?" are the drift this module already refuses to
    # accept for the docs-only classifier one line below.
    not_open = not_open_refusal(int(pr), facts)
    if not_open:
        return {**base, "pr_state": facts.get("state"), "reason": not_open}

    # **`behind_refusal`, checked before the docs-only split — `pr_sweep`'s own suppression reached a
    # second way, since `watch_pr.py` calls this function directly and never goes through the
    # sweep's row filter.** `pr_sweep.behind_reason` runs on every candidate regardless of what kind
    # of picker would follow, so this stays at the same rung rather than only guarding the
    # approval-gated half: a docs-only notice that says "green, merge whenever" about a PR GitHub is
    # about to refuse is a stale claim even though no tap is at stake.
    behind = behind_refusal(int(pr), facts)
    if behind:
        return {**base, "reason": behind}

    blockers = non_docs_paths(facts["paths"])
    docs_only = not blockers
    base = {**base, "docs_only": docs_only}

    # **Only a non-docs-only PR can have a live approval on file** — a docs-only one was never gated
    # on one, so asking `verify_approval` about it would be a question with no meaning, not a check.
    if not docs_only and not verify_approval(state_dir, int(pr), head, repo=slug, now=now):
        return {**base, "reason": f"a live approval is already on file for #{pr} at {head[:12]}"}

    if already_asked(state_dir, int(pr), head, repo=slug):
        return {**base, "reason": f"already asked about #{pr} at {head[:12]} — CI re-running on the "
                                  f"same commit is not a new question"}

    if picker_quiet_hours(state_dir, now):
        # Deliberately NOT recorded: the skip has to be recoverable, or a PR that happens to go green
        # at 03:00 loses its picker permanently and silently. `picker_quiet_hours`, not the bare
        # window: it stands down while the owner is demonstrably awake (:data:`AWAKE_OVERRIDE`).
        return {**base, "quiet_hours": True,
                "reason": f"inside the quiet window, so #{pr} was not asked about now — nothing is "
                          f"recorded, so the next watch on this commit will ask"}

    argv = request_argv(int(pr), facts, blockers, state_dir,
                        env_file=resolve_env_file(env_file), dry_run=dry_run,
                        overlap=overlapping_prs(int(pr), facts, lister=lister),
                        docs_only=docs_only)
    try:
        code, out, err = (sender or _send_question)(argv)
    except Exception as e:  # noqa: BLE001
        return {**base, "ok": False, "reason": f"the picker could not be sent: {e!r}"}

    ok, qid, payload = _sent_question_id(out)
    if code != 0 or not ok:
        return {**base, "ok": False,
                "reason": f"the picker was not sent: {payload.get('error') or (err or out or '').strip()[:200]}"}
    if dry_run:
        return {**base, "sent": False, "dry_run": True, "argv": argv,
                "reason": "dry run — the picker was rendered, not sent, and nothing was recorded"}

    record_ask(state_dir, int(pr), head, qid or "", via="auto-green", repo=slug, now=now)
    reason = (f"let the owner know #{pr} is docs-only and green at {head[:12]} — merging never "
              f"waited on this" if docs_only else f"asked the owner to approve #{pr} at {head[:12]}")
    return {**base, "sent": True, "question_id": qid, "reason": reason}


# --------------------------------------------------------------------------- the decision

def _approval_snapshot(state_dir: str, pr: int, head_sha=None, repo=None) -> dict:
    """What is on file for `(repo, pr)`, as data. **Reads only** — the fields a by-hand verification
    would otherwise read with inline Python before every merge, in one place so both the hook's
    decision and :func:`check_pr` describe the same record the same way.

    **Both lookups go through the functions the hook itself uses**, which is the load-bearing part
    of the repo keying: :func:`load_approval` resolves `(repo, pr)` — *including the legacy
    bare-number fallback*, so a legacy record is reported rather than read as absent — and
    :func:`asks_for` filters the ledger. A second resolver here would report on a
    different file than a merge would spend, and would do it confidently.

    **The ask ledger is a HISTORY now, so "the newest ask" is a pick rather than a key.**
    `merge-ask-log.jsonl` holds one appended row per send; :func:`asks_for` returns every row for this
    PR in this repo *at this commit*, oldest first, so the last one is the picker currently on the
    owner's phone. `head_sha` is part of that key: with no head to bind to (an unreadable PR) there is no
    question to be asked about *this artifact*, so the lookup is skipped rather than answered
    loosely — see :func:`render_check`, which says so instead of printing "none recorded".

    `matches_newest_ask` is `None` when either side is absent, never `False`: "there is no ask
    logged" and "the approval belongs to a different question" are different answers, and collapsing
    them is how a missing ledger would read as a forged approval."""
    record = load_approval(state_dir, pr, repo)
    rows = asks_for(state_dir, int(pr), head_sha, repo) if head_sha else []
    ask = rows[-1] if rows else None

    matches = None
    if record is not None and ask is not None:
        qid = ask.get("question_id")
        matches = bool(qid) and qid == record.get("question_id") \
            and ask.get("head_sha") == record.get("head_sha")
    approval = {
        "exists": record is not None,
        "repo": record.get("repo") if record else None,
        "question_id": record.get("question_id") if record else None,
        "approved_at": record.get("approved_at") if record else None,
        "consumed_at": record.get("consumed_at") if record else None,
        "head_sha": record.get("head_sha") if record else None,
        "matches_newest_ask": matches,
    }
    newest = None if ask is None else {
        "question_id": ask.get("question_id"), "head_sha": ask.get("head_sha"),
        "asked_at": ask.get("asked_at"), "via": ask.get("via")}
    return {"approval": approval, "newest_ask": newest, "ask_count": len(rows)}


def evaluate_pr(pr: int, facts: dict, state_dir: str, now=None) -> dict:
    """**The one classification, shared by the hook and by `check`.** Given a PR's facts, is this a
    merge the guard would allow — and everything a human needs to see why.

    **PURE: it reads and returns. It writes nothing, on any path**, and that is the load-bearing
    property of this whole module's read-only half. `decide_command` calls it and then, separately,
    spends the approval; `check_pr` calls it and stops. The `consume` lives past this function rather
    than inside it with a flag, because a flag is one wrong default away from a read-only diagnostic
    spending the owner's tap.

    Sharing it is the other half: a second classifier written for the read-only path could drift into
    reassuring an agent that a merge would be allowed when the hook would refuse it, which is worse
    than having no check at all. Same reason :func:`ask_on_green` reuses :func:`non_docs_paths`.

    **The repository comes off `facts`, never off the caller**, exactly as it does in
    :func:`decide_command` — it is what `gh` answered about the PR whose head SHA and file list are
    in the same object, so the record this consults can never be filed under a different pull
    request than the one just classified. There is no `repo` parameter here for the same reason
    there is no `consume` one: it would be a way to get the answer wrong."""
    pr = int(pr)
    head = facts["head_sha"]
    slug = facts.get("repo")
    blockers = non_docs_paths(facts["paths"])
    # **BEHIND is checked before the approval lookup, and it wins over a live approval.** A docs-only
    # PR skips this branch entirely, exactly as it skips `verify_approval` — a docs-only merge is
    # never gated on a tap, so a BEHIND docs-only PR just fails at GitHub's own merge call, which
    # costs nothing here. For an approval-gated PR, an approval bound to a head that is behind its
    # base could not be spent right now regardless of when it was granted, so `behind_refusal` is
    # tried FIRST and only falls through to `verify_approval` when it has nothing to say.
    behind = "" if not blockers else behind_refusal(pr, facts)
    # **CI is checked for EVERY merge, docs-only included, and it wins over everything else** — a
    # red or pending head is refused whatever the approval says (:func:`ci_refusal`).
    ci = ci_refusal(pr, facts)
    why = ci or ("" if not blockers
                 else (behind or verify_approval(state_dir, pr, head, repo=slug, now=now)))
    return {
        "schema": CHECK_SCHEMA,
        "pr": pr,
        "repo": slug,
        "head_sha": head,
        "title": facts.get("title") or "",
        "pr_state": facts.get("state"),
        "readable": True,
        "docs_only": not blockers,
        "blockers": blockers,
        "behind": bool(behind),
        "ci_green": not ci,
        "would_allow": not why,
        "why": why,
        "snapshot": SNAPSHOT_NOTE,
        **_approval_snapshot(state_dir, pr, head, slug),
    }


class Decision:
    """`allow` plus the human-readable `reason`. `pr`/`docs_only` are for the CLI and the tests.

    **`repo`/`repo_source` say which repository this was decided about, and how that was
    established.** They are carried on the decision rather than recomputed by whoever wants to print
    them, because the point of requiring a repository is that the answer stops being implicit — and
    a decision that cannot say which repository it is about is precisely the ambiguity removed."""

    def __init__(self, allow: bool, reason: str, pr=None, docs_only=None, repo=None,
                 repo_source=None):
        self.allow, self.reason, self.pr, self.docs_only = allow, reason, pr, docs_only
        self.repo, self.repo_source = repo, repo_source

    def as_dict(self) -> dict:
        return {"allow": self.allow, "reason": self.reason, "pr": self.pr,
                "docs_only": self.docs_only, "repo": self.repo,
                "repo_source": self.repo_source}


def deny_text(headline: str, pr=None, head_sha=None, blockers=None, repo=None,
              repo_source=None) -> str:
    """The refusal. It has three jobs and all three are load-bearing.

    It **quotes the policy and names the file it lives in**, so the agent that hit the wall can read
    the rule rather than infer one. It **says how to get approval**, because the failure mode this
    whole module exists to stop is an agent deciding for itself what it is allowed to do — an
    unexplained wall is an invitation to route around it, and a wall with a door is not. And it says
    plainly that **only the owner can open it**, so the door is not mistaken for a formality.

    **It also says WHICH REPOSITORY, and how that was established.** A refusal that names a number
    and not a repository is the same ambiguity the required `--repo` removes, one layer out: the
    agent reading this wall is about to retype the command, and `--pr 90` retyped in the wrong
    repository is how a long-closed PR gets a picker. `repo_source` is printed beside it rather
    than dropped, because *"derived from `origin` in this directory"* and *"you named it"* are
    different claims and only one of them is worth double-checking."""
    lines = [f"BLOCKED by the merge guard: {headline}", ""]
    if pr is not None:
        lines.append(f"PR: #{pr}" + (f"   head: {head_sha[:12]}" if head_sha else ""))
    if repo or repo_source:
        lines.append(f"Repository: {repo or 'NOT ESTABLISHED'}"
                     + (f"   ({repo_source})" if repo_source else ""))
    if blockers:
        shown = blockers[:12]
        # The tag is a claim about the ASSISTANT, so it is only made in its own repository — the
        # same rule as :func:`change_kind_phrase`, which this wall shares its sentence with.
        prompt = set(prompt_paths(blockers)) if is_assistant_repo(repo) else set()
        lines.append("Paths that make this ask-high:")
        # A `(prompt)` tag rather than a second list: the agent needs to know WHICH path is the
        # blocker, and splitting them into two blocks buries a lone `.py` under a heading.
        tag = f"   (prompt — this is what {assistant_label()} executes)"
        lines.extend(f"  - {p}" + (tag if p in prompt else "") for p in shown)
        if len(blockers) > len(shown):
            lines.append(f"  ... and {len(blockers) - len(shown)} more")
        lines.append("")
    lines += [
        f"The owner's standing merge policy, in {POLICY_FILE}:",
        f"  {POLICY_QUOTE}",
        "",
        "A docs-only PR may be merged once CI is green; anything that changes what runs is ask-high,",
        "every time. Green CI does not graduate it, and red or pending CI blocks every merge. This",
        "guard exists because a rule that is only written down does not stop an agent that has",
        "talked itself into an authorization.",
        "",
        "TO PROCEED, ASK THE OWNER — do not work around this:",
        # **`--repo` is spelled here because `request` REQUIRES it.** The refusal an agent reads has
        # to be the command that works: a door whose printed invocation exits 4 is a wall.
        f"  python seneschal/scripts/merge_guard.py request "
        f"--pr {pr if pr is not None else '<number>'} --repo {repo or '<owner>/<name>'}",
        "",
        "That sends the owner a tappable Approve / Not now picker on Telegram. The approval is",
        "written by the daemon when they tap, is bound to this exact head SHA, is single-use, and",
        f"expires in {APPROVAL_TTL_HOURS} h. There is no way for you to mint one, and that is the point:",
        "if you are reading this and considering another route to the same merge, the answer is to",
        "ask the owner.",
    ]
    return "\n".join(lines)


def decide_command(command: str, state_dir: str = DEFAULT_STATE_DIR, cwd=None, runner=None,
                   now=None, consume: bool = True, context_repo=None) -> Decision:
    """Stage 2 for one command. **Every path out of here that is not a positive ALLOW is a DENY.**

    Assumes stage 1 already matched; calling it on a non-merge returns ALLOW with "not a merge".

    `context_repo` is the repository the caller states they are judging *in*, and only `judge` passes
    it — it is that subcommand's `--repo`, standing where the hook's `cwd` stands. The hook passes
    `cwd` and nothing else, exactly as before."""
    invocations = merge_invocations(command)
    if not invocations:
        return Decision(True, "not a merge command")

    slug = source = None
    for inv in invocations:
        try:
            parsed = parse_invocation(inv)
        except GuardError as e:
            return Decision(False, deny_text(str(e)))
        pr, named = parsed["pr"], parsed.get("repo")

        # **THE REPOSITORY IS ESTABLISHED BEFORE `gh` IS ASKED ANYTHING, AND FAILING TO IS A DENY.**
        # :func:`derive_repo` carries the argument. Handing `gh` a `None` repo would make it resolve
        # the number against the shell's working directory — the same lookup, done silently, by a
        # process that cannot be asked afterwards which repository it picked. Deriving it here is what makes the answer
        # sayable, and an unestablishable repository blocks like every other stage-2 unknown.
        asked, source = derive_repo(named, context_repo=context_repo, cwd=cwd, runner=runner)
        if not asked:
            return Decision(False, deny_text(source, pr=pr, repo_source=source), pr=pr,
                            repo_source=source)
        try:
            facts = pr_facts(pr, repo=asked, cwd=cwd, runner=runner)
        except GuardError as e:
            return Decision(False, deny_text(str(e), pr=pr, repo=asked, repo_source=source),
                            pr=pr, repo=asked, repo_source=source)

        # **The repo the approval is KEYED on comes from the PR gh just answered about, never from
        # the command** — requiring `--repo` decides which question to ask, not what the answer is.
        # An approval keyed on the number alone is one `45.json` for every repo on the disk.
        # `evaluate_pr` reads it off `facts` for that reason.
        slug = facts.get("repo")
        # **A `--match-head-commit` GitHub would reject is refused HERE, above the spend** —
        # :func:`match_head_refusal`: otherwise the command fails at GitHub AFTER the guard has
        # spent the approval on the attempt. Checked before `evaluate_pr` so a
        # docs-only merge gets the same correction instead of GitHub's coercion error.
        # The REST door's own two (a `sha` it must carry, a merge method it must name), then the
        # async door's stack check — all above the spend, and all for docs-only PRs too: a missing
        # binding or an unapproved PR riding in underneath is not made safe by this one's diff.
        bad_head = (api_merge_refusal(parsed, facts)
                    or match_head_refusal(parsed.get("match_head"), facts)
                    or (stack_refusal(pr, facts, cwd=cwd, runner=runner)
                        if parsed.get("endpoint") == "merge-async" else ""))
        if bad_head:
            return Decision(False, deny_text(bad_head, pr=pr, head_sha=facts["head_sha"],
                                             repo=slug, repo_source=source),
                            pr=pr, repo=slug, repo_source=source)
        verdict = evaluate_pr(pr, facts, state_dir, now=now)
        if not verdict["ci_green"]:
            # **Never merge red or pending — docs-only and approved PRs included.** Refused above
            # the spend, so an approval is never wasted on a merge that may not run.
            return Decision(False, deny_text(f"PR #{pr}: {verdict['why']}.", pr=pr,
                                             head_sha=facts["head_sha"], repo=slug,
                                             repo_source=source),
                            pr=pr, docs_only=verdict["docs_only"], repo=slug, repo_source=source)
        if verdict["docs_only"]:
            continue  # docs-only on green: the owner's standing grant. No friction, by design.

        if not verdict["would_allow"]:
            blockers = verdict["blockers"]
            kind = change_kind_phrase(blockers, prompt_paths(blockers), slug)
            return Decision(
                False,
                deny_text(f"PR #{pr} {kind} and {verdict['why']}.", pr=pr,
                          head_sha=facts["head_sha"], blockers=blockers, repo=slug,
                          repo_source=source),
                pr=pr, docs_only=False, repo=slug, repo_source=source)
        # **THE ONE WRITE ON THE DECISION PATH, AND IT LIVES HERE RATHER THAN IN `evaluate_pr`.**
        # Everything above this line is read-only and is what `check` runs; the spend is what makes
        # this the hook and that a diagnostic. Moving it up, or giving `evaluate_pr` a `consume=`
        # flag, is how the read-only half stops being read-only.
        if consume:
            consume_approval(state_dir, pr, now=now, repo=slug, command=command)
        return Decision(True, f"{slug} PR #{pr} is approved by the owner at {facts['head_sha'][:12]} "
                              f"and CI is green (approval spent; repository {source}. If GitHub "
                              f"refuses the merge, the post-tool hook restores it — see "
                              f"refund_approval)",
                        pr=pr, docs_only=False, repo=slug, repo_source=source)

    last = invocations[-1]
    pr = None
    try:
        pr = parse_invocation(last).get("pr")
    except GuardError:
        pass
    return Decision(True, f"{slug + ' ' if slug else ''}PR #{pr} is docs-only and CI is green "
                          f"(the owner's standing docs-only grant)",
                    pr=pr, docs_only=True, repo=slug, repo_source=source)


# ------------------------------------------------------------------- the read-only check

def check_pr(pr: int, repo=None, state_dir: str = DEFAULT_STATE_DIR, runner=None, now=None) -> dict:
    """**Would the guard allow a merge of `pr` right now — asked without spending the answer.**

    Runs :func:`pr_facts` and :func:`evaluate_pr`, the same
    two the hook runs, and returns their verdict. **It writes nothing anywhere, on any path** —
    no approval, no ask log, no state file at all — and it is not reachable from :func:`main`.

    `repo` is the same optional hint `gh pr view` takes, and on the readable path it is *not* what
    the verdict is keyed on: :func:`evaluate_pr` uses the repository `gh` answered with. On the
    unreadable path it is all there is, and the record it finds is reported as context that decides
    nothing — `would_allow` is already `False` there.

    **Never raises**, and an unreadable PR is reported as `would_allow: False`, not as an error to be
    interpreted. The polarity matches the hook's: *cannot tell* is never *allow*, on the read-only
    side too, because a check that says nothing when `gh` is down is a check whose silence someone
    will read as fine."""
    pr = int(pr)
    try:
        facts = pr_facts(pr, repo=repo, runner=runner)
    except GuardError as e:
        reason = str(e)
    except Exception as e:  # noqa: BLE001 — a diagnostic may not die on the thing it is diagnosing
        reason = f"`gh pr view` failed: {e!r}"
    else:
        return evaluate_pr(pr, facts, state_dir, now=now)

    # The PR could not be read, so nothing can be bound to a head SHA. The record on file is still
    # reported — it is what a human would look at next — but it decides nothing here, and the ask
    # ledger is not consulted at all: its key includes the head SHA we just failed to read.
    return {
        "schema": CHECK_SCHEMA, "pr": pr, "repo": repo or None, "head_sha": None, "title": "",
        "pr_state": None, "readable": False, "docs_only": None, "blockers": [],
        "ci_green": None, "would_allow": False, "why": reason, "snapshot": SNAPSHOT_NOTE,
        **_approval_snapshot(state_dir, pr, None, repo),
    }


def _yes_no(value) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def _json_ish(value) -> str:
    """`None` -> `null`. The reader of this report is checking a field in a JSON file on disk, and
    Python's `None` is not what the approval record says — printing it invites someone to grep for
    the wrong string in the file this report is standing in for."""
    return "null" if value is None else str(value)


def render_check(verdict: dict) -> str:
    """The human form of :func:`check_pr`. **It reports and stops.**

    It prints no merge command — not as a courtesy but because printing one is how a read-only tool
    becomes a shortcut. `merge_guard.py check --pr N && gh pr merge N` is the ask-then-merge-yourself
    loop wearing a diagnostic's clothes, and a test asserts this output does not itself parse as a
    merge (`looks_like_merge`), the same self-immunity :func:`deny_text` holds. When the answer is
    *blocked*, the only next step it offers is **asking the owner**.

    **The repository is named on the header line**, because a PR number is not an identity: the
    owner is asked about `#45` in more than one repo on the same phone, and a report that says only
    `#45` is a report about whichever repo the reader assumes."""
    pr, head, slug = verdict["pr"], verdict.get("head_sha"), verdict.get("repo")
    approval, ask = verdict["approval"], verdict.get("newest_ask")

    lines = [f"merge check — {slug + ' ' if slug else ''}PR #{pr}"
             + (f" at {head[:12]}" if head else " (head unreadable)")]
    if verdict.get("title"):
        lines.append(f"  {verdict['title']}")
    lines.append("")

    if not verdict.get("readable", True):
        lines += ["  verdict:   CANNOT TELL, so: BLOCKED",
                  f"  why:       {verdict['why']}"]
    elif verdict["docs_only"]:
        if verdict["would_allow"]:
            lines.append("  verdict:   ALLOW — docs-only and green, under the standing docs-only grant")
        else:
            lines += ["  verdict:   BLOCKED — docs-only, but CI is not green",
                      f"  why:       {verdict['why']}"]
        lines.append("  changes:   docs-only (no approval is needed, and none would be spent)")
    else:
        lines.append("  verdict:   " + ("ALLOW — a live approval covers this exact head"
                                        if verdict["would_allow"] else "BLOCKED"))
        blockers = verdict["blockers"]
        lines.append(f"  changes:   functionality — {len(blockers)} non-docs "
                     f"path{'s' if len(blockers) != 1 else ''}")
        for path in blockers[:12]:
            lines.append(f"               - {path}")
        if len(blockers) > 12:
            lines.append(f"               ... and {len(blockers) - 12} more")
        if not verdict["would_allow"]:
            lines.append(f"  why:       {verdict['why']}")

    if verdict.get("pr_state"):
        lines.append(f"  pr state:  {verdict['pr_state']}")

    lines.append("")
    if not approval["exists"]:
        lines.append("  approval:  none on file for this PR")
    else:
        lines += [
            f"  approval:  on file — question {approval['question_id'] or '(none named)'}",
            f"               repo:        {_json_ish(approval.get('repo'))}",
            f"               approved_at: {_json_ish(approval['approved_at'])}",
            f"               consumed_at: {_json_ish(approval['consumed_at'])}"
            + ("   (unspent)" if not approval["consumed_at"] else "   (SPENT — approvals are "
                                                                 "single-use)"),
            f"               head_sha:    {_json_ish(approval['head_sha'])}",
            f"               matches the newest ask: {_yes_no(approval['matches_newest_ask'])}",
        ]
    if not verdict.get("readable", True):
        # NOT "none recorded". Since the ledger became a history keyed on (repo, PR, head SHA), an
        # ask is a question about one artifact — and the artifact is exactly what could not be read.
        # Reporting an absence we never looked for is the kind of confidently-wrong line this
        # rebase existed to prevent.
        lines.append("  newest ask: not looked up — an ask is keyed to a head SHA, and this PR's "
                     "head could not be read")
    elif ask is None:
        lines.append("  newest ask: none recorded for this PR at this head")
    else:
        count = verdict.get("ask_count") or 1
        lines.append(f"  newest ask: {ask['question_id'] or '(none named)'} at "
                     f"{str(ask['head_sha'] or '')[:12]}, {ask['asked_at']} (via {ask['via']})"
                     + (f"   ({count} pickers have gone out for this head)" if count > 1 else ""))

    if not verdict.get("readable", True):
        # The record above is reported because it is what a human looks at next — but with no head
        # SHA to bind it to, it decides nothing, and saying so is the difference between a diagnostic
        # and a false reassurance. Asking the owner is not the fix either: `request` reads the PR too.
        lines += ["", "  The PR could not be read, so none of the above is a verdict about it. Fix "
                      "the read and check again."]
    elif not verdict["would_allow"]:
        # `--repo` is spelled because `request` REQUIRES it: a printed invocation that exits 4 is a
        # door that does not open.
        lines += ["", "  TO PROCEED, ASK THE OWNER — that is the whole route, and there is no other:",
                  f"    python seneschal/scripts/merge_guard.py request --pr {pr} "
                  f"--repo {slug or '<owner>/<name>'}"]

    lines += ["", "  This check spent nothing: no approval consumed, no ask recorded, nothing "
                  "written.", f"  {verdict['snapshot']}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- the hook

def extract_command(event):
    """A `PreToolUse` payload -> the command string, or None when this event is not our business.
    Never raises: a shape we do not understand has no command in it, and stage 1 allows."""
    if not isinstance(event, dict):
        return None
    if event.get("tool_name") not in GUARDED_TOOLS:
        return None
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    return command if isinstance(command, str) and command else None


def main(stdin=None, stderr=None, state_dir: str = DEFAULT_STATE_DIR, runner=None, now=None) -> int:
    """Hook entrypoint. `0` = allow, `2` = block with the reason on stderr.

    The two stages, with their opposite polarities, are the whole design — see the module docstring.
    Stage 1 is wrapped in a fail-OPEN try because it runs on every shell call on this machine; stage
    2 is wrapped in a fail-CLOSED one because by then a merge has been positively identified and
    "cannot tell" is never "allow"."""
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        event = json.loads(raw) if raw and raw.strip() else None
        command = extract_command(event)
        if command is None or not looks_like_merge(command):
            return 0
        cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else None
    except Exception:  # noqa: BLE001 — stage 1 fails OPEN: a bug here may not brick every session
        return 0

    try:
        decision = decide_command(command, state_dir=state_dir, cwd=cwd, runner=runner, now=now)
    except Exception as e:  # noqa: BLE001 — stage 2 fails CLOSED, including on its own bugs
        decision = Decision(False, deny_text(f"the guard itself failed while checking: {e!r}"))
    if decision.allow:
        return 0
    try:
        (stderr if stderr is not None else sys.stderr).write(decision.reason + "\n")
    except Exception:  # noqa: BLE001 — a block we cannot explain is still a correct block
        pass
    return 2


def exit_status_from_event(event) -> "int | None":
    """A post-tool event -> the command's exit status, or None when it cannot be read.

    `PostToolUse` fires only on success, so it is 0 unless the response names another code.
    `PostToolUseFailure` carries the harness's failure text — for a non-zero Bash exit that reads
    `Exit code N` (as the harness reports it: `Error: Exit code 1\\nGraphQL: This pull request is
    part of a stack …`) — so N is read out of it. No readable N is None, and None never
    refunds."""
    if not isinstance(event, dict):
        return None
    response = event.get("tool_response")
    if isinstance(response, dict):
        for key in ("exit_code", "exitCode", "returnCode", "return_code"):
            if isinstance(response.get(key), int) and not isinstance(response.get(key), bool):
                return response[key]
    if event.get("hook_event_name") == "PostToolUse":
        return 0
    for text in _event_texts(event):
        m = _EXIT_CODE_RE.search(text)
        if m:
            return int(m.group(1))
    return None


def _event_texts(event: dict) -> list:
    """Every string a post-tool event carries about the outcome: `error`, and `tool_response` as a
    string or as a dict's string values (stdout/stderr)."""
    out = []
    for value in (event.get("error"), event.get("tool_response")):
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            out.extend(v for v in value.values() if isinstance(v, str))
    return out


def post_main(stdin=None, stdout=None, state_dir: str = DEFAULT_STATE_DIR, runner=None,
              now=None) -> int:
    """The `PostToolUseFailure` hook: when a merge the guard allowed FAILED, try to give the approval
    back (:func:`refund_approval`, which decides everything and logs every outcome).

    **Always exits 0 and never raises** — a post-tool hook cannot un-run anything, and one that
    breaks costs a refund, never a session. A success event is ignored outright: a merge that
    exited 0 happened (or was enqueued), and its spend stands. The repository is read off the
    command the same way the pre-tool hook reads it (:func:`derive_repo`), so both hooks decide about
    the same repository. What happened is reported back as `additionalContext`, so the agent that
    ran the merge learns whether the tap it spent is live again."""
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        event = json.loads(raw) if raw and raw.strip() else None
        if event is None or event.get("hook_event_name") == "PostToolUse":
            return 0
        command = extract_command(event)
        if command is None or not looks_like_merge(command):
            return 0
        cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else None
        status = exit_status_from_event(event)
        error_text = "\n".join(_event_texts(event))
        notes = []
        for inv in merge_invocations(command):
            try:
                parsed = parse_invocation(inv)
            except GuardError:
                continue   # the pre-tool hook refused it, so nothing was spent
            repo, _source = derive_repo(parsed.get("repo"), cwd=cwd, runner=runner)
            if not repo:
                continue
            _ok, why = refund_approval(state_dir, parsed["pr"], repo, status, command,
                                       error_text=error_text, via="hook", cwd=cwd,
                                       runner=runner, now=now)
            notes.append(f"merge_guard: {repo} #{parsed['pr']}: "
                         + (why if _ok else f"approval NOT restored — {why}"))
        if notes:
            (stdout if stdout is not None else sys.stdout).write(json.dumps({
                "hookSpecificOutput": {"hookEventName": event.get("hook_event_name"),
                                       "additionalContext": "\n".join(notes)}}) + "\n")
    except Exception:  # noqa: BLE001 — a post-tool hook that breaks costs a refund, nothing more
        return 0
    return 0


# --------------------------------------------------------------------------- CLI

def _cmd_refund(args) -> int:
    """The hand door to :func:`refund_approval`, for a session where the post-tool hook is not
    installed yet. **Same function, same conditions, nothing looser**: GitHub is re-read here, the
    PR must still be OPEN at the approved head, the command must be the single failed merge, and
    the outcome is logged with `via: "cli"`. Stating a false exit status is a forgery, the same
    class `MERGE_GUARD_SETUP.md` already names — and even then the most it restores is the exact
    approval the owner gave, for a PR that GitHub says has not merged."""
    ok, why = refund_approval(args.state_dir, args.pr, args.repo, args.exit_status, args.command,
                              error_text=args.error or "", via="cli")
    print(json.dumps({"refunded": ok, "why": why, "pr": args.pr, "repo": args.repo}))
    return 0 if ok else 2


def _cmd_check(args) -> int:
    """**READ-ONLY. It cannot authorize anything, and that is structural rather than promised.**

    Everything it can reach — :func:`check_pr`, :func:`evaluate_pr`, :func:`render_check` — reads and
    returns. There is no `consume` argument to get wrong, no `--force` to add later; spending lives on
    :func:`decide_command`'s side of the seam. Exit 0 = would allow, 2 = would not, including
    *could not tell*."""
    verdict = check_pr(args.pr, repo=args.repo, state_dir=args.state_dir)
    print(json.dumps(verdict, ensure_ascii=False) if args.json else render_check(verdict))
    return 0 if verdict["would_allow"] else 2


def _cmd_judge(args) -> int:
    """The hook's decision on one command string, by hand — **and it SPENDS the approval on an allow,
    exactly as the hook does.**

    That is not a defect to fix: this is the hook, and a faithful replay of the hook has to include
    the spend or it is not a replay. It is deliberately NOT named `check`: a name that reads as a
    diagnostic invites a diagnostic run that burns the owner's tap. Use `check --pr N` to ask the same
    question without paying for the answer; `--dry-run` still holds the token here.

    **`--repo` is the invoking directory, by hand.** The hook derives its repository from the cwd the
    `PreToolUse` event carried (:func:`derive_repo`, rung 3); a replay has no such directory, so the
    caller states it instead and it stands in exactly the same place. A `-R` *inside* the command
    still wins, as it does for the hook — and the replay says which one it used, so the two being
    different is visible rather than silently resolved."""
    decision = decide_command(args.command, state_dir=args.state_dir, consume=not args.dry_run,
                              context_repo=args.repo)
    if args.json:
        print(json.dumps(decision.as_dict(), ensure_ascii=False))
    else:
        print(("ALLOW: " + decision.reason) if decision.allow else decision.reason)
    return 0 if decision.allow else 2


def duplicate_ask_reason(state_dir: str, pr: int, head_sha: str, repo=None) -> str:
    """`""` if `request` should send, else why the question it would send is already on the owner's
    phone.

    The watcher auto-sends a picker and an agent, not knowing, hand-sends another seconds later: **two
    live pickers for one PR at one commit**, both correct in isolation — `ask_on_green` is idempotent
    on (PR, head SHA), and a `request` idempotent on nothing is the other half.

    **The judgement, stated because it is a judgement:** an explicit `request` genuinely IS different
    from a watcher firing — an agent asking on purpose is acting, and being unable to ask is strictly
    worse than a duplicate buzz. So this does not make `request` *refuse*; it makes it **idempotent
    on (repo, PR, head SHA)**, exactly as the automatic path already is, and leaves `--resend` as an
    unconditional escape hatch. Two questions with identical text about identical bytes, seconds
    apart, are not two asks; they are one ask sent twice.

    **A SPENT approval re-opens it with no flag at all.** That is the case where re-asking is
    obviously right — the owner tapped Approve, the token was consumed on a merge attempt, and the merge
    still has not landed — and it is also the case where an agent is most likely to be stuck. The
    outstanding picker is provably dead, so this is not a duplicate. Nothing here can lower the gate:
    every path either sends a QUESTION or declines to send one."""
    if not asks_for(state_dir, pr, head_sha, repo):
        return ""
    record = load_approval(state_dir, pr, repo)
    if isinstance(record, dict) and record.get("head_sha") == head_sha and record.get("consumed_at"):
        return ""  # the approval it produced has been spent; that picker is finished business
    return (f"a picker for #{pr} at {head_sha[:12]} has already gone out and is still live — "
            f"a second identical question is a duplicate buzz, not a second ask. If it was lost, "
            f"or you need the owner to answer again, re-run with --resend.")


def _canned_lister(path):
    """A `pr_overlap` runner that answers from a **file** instead of from `gh` — `--overlap-from`.

    `render` exists because *"the pickers are the evidence"* and the PRs that produced them have all
    merged by the time anyone asks. The overlap block has the same problem one level worse: it is a
    statement about **which other pull requests were open at that moment**, and that world is gone
    within the hour. So the same seam the tests use is reachable from the CLI, and a picker can be
    reproduced from a captured `gh pr list --json number,title,files,isDraft` payload.

    Read with `getattr` at both call sites, deliberately: `RenderIsReadOnlyAndRefusesNothingTest`
    builds its own `Namespace`, and a flag whose absence is a crash makes every hand-built caller a
    caller of the crash. Absent means the same thing as unset — look at `gh`.

    `None` — the ordinary case — means the real `gh` call. It changes nothing about the gate: the
    file only supplies the *other* PRs' numbers, titles and file lists, and the block it produces is
    still rendered by :func:`overlap_band` under every cap. A read failure answers *"could not
    look"*, which is the same `[]` every other failure answers."""
    if not path:
        return None

    def lister(_argv):
        with open(path, "r", encoding="utf-8") as fh:
            return 0, fh.read(), ""
    return lister


def _cmd_request(args) -> int:
    """Ask the owner. **This writes no approval** — it sends the picker and stops. Agent-callable on
    purpose: asking is act-low, and a guard whose only escape is "ask the owner" has to make asking
    the single easiest thing to do.

    **It takes no required arguments beyond the PR and its repository, and that is the whole
    point**: the refusal an agent reads spells exactly `request --pr N --repo R`, so anything this
    needs and does not resolve for itself is a door that does not open. Credentials come from
    :func:`resolve_env_file`.

    **It refuses one thing, and only one: a PR that is not OPEN** (:func:`not_open_refusal`, exit
    :data:`EXIT_PR_NOT_OPEN`). That refusal is checked before everything else here, because the
    caller's real error is almost always the *repository* and the message is the diagnostic.

    It is **idempotent on (repo, PR, head SHA)** rather than ungated, with `--resend` as the escape
    hatch — see :func:`duplicate_ask_reason` for the reasoning and for why a skip is `ok: true` and
    exit **0**: the caller's goal is that the owner has a live picker for this commit, and they do. An agent that reads this as a failure would re-run it, which is the buzz."""
    try:
        facts = pr_facts(args.pr, repo=args.repo)
    except GuardError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    # **BEFORE the docs-only branch, deliberately.** A merged PR that happens to be docs-only would
    # otherwise be answered *"docs-only, no approval needed"* — true, useless, and silent about the
    # thing the caller actually got wrong. The repository is the diagnosis; it goes first.
    not_open = not_open_refusal(args.pr, facts)
    if not_open:
        print(json.dumps({"ok": False, "sent": False, "pr": args.pr, "repo": facts.get("repo"),
                          "pr_state": facts.get("state"), "error": not_open}, ensure_ascii=False))
        return EXIT_PR_NOT_OPEN
    blockers = non_docs_paths(facts["paths"])
    if not blockers:
        print(json.dumps({"ok": True, "docs_only": True, "pr": args.pr,
                          "repo": facts.get("repo"),
                          "note": "docs-only — already act-low under the standing docs-only "
                                  "grant (merge once CI is green); no approval needed"},
                         ensure_ascii=False))
        return 0
    slug = facts.get("repo")
    duplicate = "" if args.resend else duplicate_ask_reason(
        args.state_dir, args.pr, facts["head_sha"], repo=slug)
    if duplicate:
        prior = asks_for(args.state_dir, args.pr, facts["head_sha"], repo=slug)
        print(json.dumps({"ok": True, "sent": False, "already_asked": True, "pr": args.pr,
                          "repo": slug, "head_sha": facts["head_sha"], "note": duplicate,
                          "asked_at": [row.get("asked_at") for row in prior]},
                         ensure_ascii=False))
        return 0
    argv = request_argv(args.pr, facts, blockers, args.state_dir,
                        env_file=resolve_env_file(args.env_file), dry_run=args.dry_run,
                        overlap=overlapping_prs(args.pr, facts,
                                                lister=_canned_lister(
                                                    getattr(args, "overlap_from", None))))
    code, out, err = _send_question(argv)
    sys.stdout.write(out)
    if code != 0:
        sys.stderr.write(err)
        return code
    ok, qid, _payload = _sent_question_id(out)
    if ok and not args.dry_run:
        record_ask(args.state_dir, args.pr, facts["head_sha"], qid or "", via="request",
                   repo=slug, now=None)
    return code


def _cmd_render(args) -> int:
    """**Print the picker this PR would get. Sends nothing, records nothing, refuses nothing.**

    `request --dry-run` already renders, but it is the *send* path minus the send: it declines a PR
    that is not OPEN and it short-circuits a docs-only one, both correctly. That makes it unable to
    answer the question this exists for — *"what would the owner have read?"* — about the PRs whose
    pickers are the evidence, because those have all merged by the time anyone asks.

    So this is the same argv through the same builder, always `--dry-run`, with the two refusals
    absent and the ask ledger untouched. It is read-only in the strong sense `check` is: it can
    reach nothing that writes. **It also does not print a merge command** — `CheckDoesNotShortenTheLoopTest`'s
    rule holds here too, a rendering tool that ends in a copy-pasteable merge is a bypass wearing a
    preview's clothes."""
    try:
        facts = pr_facts(args.pr, repo=args.repo)
    except GuardError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    blockers = non_docs_paths(facts["paths"]) or ["(docs-only — this picker would not be sent)"]
    argv = request_argv(args.pr, facts, blockers, args.state_dir, env_file=None, dry_run=True,
                        overlap=overlapping_prs(args.pr, facts,
                                                lister=_canned_lister(
                                                    getattr(args, "overlap_from", None))))
    code, out, err = _send_question(argv)
    if code != 0:
        # A citation refusal is `telegram_ask`'s JSON on STDOUT (exit 2), not stderr — printing only
        # stderr would render a refused picker as nothing at all.
        sys.stderr.write(err)
        sys.stderr.write(out)
        return code
    ok, _qid, payload = _sent_question_id(out)
    if args.json or not ok:
        sys.stdout.write(out)
        return 0 if ok else 1
    print(payload.get("body", ""))
    return 0


def _cmd_ask_on_green(args) -> int:
    """The auto-send, by hand. `watch_pr.py` is its production caller; this exists so the behaviour is
    runnable and testable on its own rather than only reachable through a poller."""
    res = ask_on_green(args.pr, repo=args.repo, state_dir=args.state_dir, env_file=args.env_file,
                       dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


def _cmd_list(args) -> int:
    """Approvals on file **for one repository**, plus the legacy bare-number records that a lookup in
    that repository would still fall back to.

    Reads BOTH filename shapes — `<repo-key>--<pr>.json` and the legacy bare `<pr>.json` — and
    reports each record's own `repo`. The filter is on the **filename key**, not on the record's
    `repo` field, because the filename is what :func:`approval_path` writes and what
    :func:`_existing_approval_path` resolves; filtering on the field would report a different set
    than a merge would spend.

    **THE LEGACY RECORDS ARE LISTED SEPARATELY, NEVER FILTERED AWAY, AND THAT IS THE WHOLE CARE
    HERE.** A bare `<pr>.json` predates the repo keying and says nothing about which
    repository it was for. :func:`load_approval` still falls back to one, so it is still reachable
    from *every* repository — which means dropping it from this listing for failing to match
    `--repo` would hide precisely the records whose ambiguity is why the flag now exists, and would
    hide them on the day somebody went looking. They come back under `legacy` with `repo: null`,
    which is the honest answer: *this exists, it applies here, and nothing can say whether it
    should.* In practice every such record is consumed or long past the 24 h TTL, so the tolerated
    set is as closed and self-liquidating as :func:`load_approval` claims. This reports them
    regardless; the count is context, not the argument."""
    rows, legacy = [], []
    directory = approvals_dir(args.state_dir)
    want = repo_key(args.repo)
    for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
        if not name.endswith(".json"):
            continue
        stem = name[:-5]
        try:
            pr = int(stem.rsplit("--", 1)[-1])
        except ValueError:
            continue
        key = stem.rsplit("--", 1)[0] if "--" in stem else ""
        if key and key != want:
            continue
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            (rows if key else legacy).append(
                {"pr": pr, "repo": record.get("repo"), "file": name,
                 "head_sha": record.get("head_sha"),
                 "approved_at": record.get("approved_at"),
                 "consumed_at": record.get("consumed_at")})
    print(json.dumps({"ok": True, "repo": args.repo, "approvals": rows, "legacy": legacy},
                     ensure_ascii=False))
    return 0


def _cmd_asks(args) -> int:
    """The ask history — every picker that has gone out, oldest first. Read-only.

    This is the subcommand the clobbering ledger could not have had: with one entry per PR there was
    no history to print, which is precisely why a duplicate send could leave no trace.

    **The repository filter is :func:`asks_for`'s own match, not a stricter one.** A row carrying no
    `repo` is a legacy row from before the keying fix, and it matches every repository here for the
    same reason it does at the resolver: the head SHA is what actually tells two same-numbered PRs
    apart. A stricter filter would print a history the guard itself does not have, which is worse
    than an over-inclusive one — so `legacy` counts the unattributable rows in the answer instead of
    leaving the reader to infer that some of it is."""
    rows = read_asks(args.state_dir)
    want = repo_key(args.repo)
    rows = [row for row in rows
            if not row.get("repo") or repo_key(row.get("repo")) == want]
    if args.pr is not None:
        rows = [row for row in rows if row.get("pr") == args.pr]
    shown = rows[-args.limit:] if args.limit else rows
    print(json.dumps({"ok": True, "repo": args.repo, "asks": shown,
                      "legacy": sum(1 for row in shown if not row.get("repo"))},
                     ensure_ascii=False))
    return 0


def _cmd_prune_asks(args) -> int:
    dropped = prune_asks(args.state_dir, days=args.days)
    print(json.dumps({"ok": True, "dropped": dropped, "days": args.days}, ensure_ascii=False))
    return 0


def cli(argv=None) -> int:
    p = argparse.ArgumentParser(description="Refuse a merge that is neither docs-only nor approved.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="READ-ONLY: would a merge of this PR be allowed right now? "
                                     "Consumes nothing, records nothing, merges nothing")
    c.add_argument("--pr", type=int, required=True)
    c.add_argument("--repo", default=None, help=REPO_HELP)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=_cmd_check)

    j = sub.add_parser("judge", help="replay the hook's decision on one shell command. SPENDS the "
                                     "approval on an allow, exactly as the hook does — use `check` "
                                     "to ask without spending")
    j.add_argument("--command", required=True)
    j.add_argument("--repo", default=None,
                   help=REPO_HELP + " Here it stands where the hook's working directory stands: a "
                        "-R inside --command still wins, and the replay says which one it used.")
    j.add_argument("--json", action="store_true")
    j.add_argument("--dry-run", action="store_true", help="do not spend an approval on an ALLOW")
    j.set_defaults(func=_cmd_judge)

    r = sub.add_parser("request", help="ask the owner to approve one PR (writes NO approval)")
    r.add_argument("--pr", type=int, required=True)
    r.add_argument("--repo", default=None, help=REPO_HELP)
    r.add_argument("--env-file", default=None,
                   help="KEY=VALUE file with TELEGRAM_* settings. Defaults to the sibling "
                        "telegram.env when it exists — a hook is spawned with no argv, so this "
                        "must work unset.")
    r.add_argument("--dry-run", action="store_true", help="render the picker; no network, no record")
    r.add_argument("--resend", "--force", dest="resend", action="store_true",
                   help="send even if a picker for this PR at this head SHA already went out. For "
                        "the genuine re-ask: a message that got lost, or an answer the owner "
                        "needs to give again. Asking is act-low, so this gate is never a wall.")
    r.add_argument("--overlap-from", default=None,
                   help="DIAGNOSTIC: read the other open PRs from a captured `gh pr list --json "
                        "number,title,files,isDraft` file instead of calling `gh`, so a picker can "
                        "be reproduced after the PRs that overlapped it have merged")
    r.set_defaults(func=_cmd_request)

    rd = sub.add_parser("render", help="READ-ONLY: print the picker this PR would get. Sends "
                                       "nothing, records nothing, and unlike `request --dry-run` "
                                       "renders a merged or closed PR too")
    rd.add_argument("--pr", type=int, required=True)
    rd.add_argument("--repo", default=None, help=REPO_HELP)
    rd.add_argument("--json", action="store_true", help="the whole dry-run payload, not just the body")
    rd.add_argument("--overlap-from", default=None,
                    help="DIAGNOSTIC: the other open PRs from a captured `gh pr list` file rather "
                         "than from `gh` — see `request --overlap-from`")
    rd.set_defaults(func=_cmd_render)

    g = sub.add_parser("ask-on-green", help="send the picker if this PR would be blocked and is not "
                                            "already asked/approved (watch_pr.py's hook, by hand)")
    g.add_argument("--pr", type=int, required=True)
    g.add_argument("--repo", default=None, help=REPO_HELP)
    g.add_argument("--env-file", default=None)
    g.add_argument("--dry-run", action="store_true", help="decide and render; no network, no record")
    g.set_defaults(func=_cmd_ask_on_green)

    ls = sub.add_parser("list", help="approvals on file for one repository (plus the legacy "
                                     "bare-number records that still resolve there)")
    ls.add_argument("--repo", default=None, help=REPO_HELP)
    ls.set_defaults(func=_cmd_list)

    a = sub.add_parser("asks", help="every picker that has gone out for one repository, oldest "
                                    "first (read-only)")
    a.add_argument("--repo", default=None, help=REPO_HELP)
    a.add_argument("--pr", type=int, default=None)
    a.add_argument("--limit", type=int, default=50)
    a.set_defaults(func=_cmd_asks)

    rf = sub.add_parser("refund", help="give back an approval spent on a merge GitHub REFUSED — "
                                       "only if the PR is still OPEN at the approved head (the "
                                       "post-tool hook's door, by hand)")
    rf.add_argument("--pr", type=int, required=True)
    rf.add_argument("--repo", default=None, help=REPO_HELP)
    rf.add_argument("--exit-status", type=int, required=True,
                    help="the failed merge command's exit status (non-zero)")
    rf.add_argument("--command", required=True, help="the exact merge command that failed")
    rf.add_argument("--error", default=None, help="the failure text GitHub/`gh` printed")
    rf.set_defaults(func=_cmd_refund)

    # **NO `--repo`, and it is the one subcommand without one** — see
    # :data:`REPO_REQUIRED_COMMANDS`, which a test pins against this parser: it ages rows out by
    # DATE, from a log that spans every repository, so a repo flag here would change nothing and
    # teach the caller that naming one is ceremony.
    pa = sub.add_parser("prune-asks", help="age out ask rows across every repository (Dream step 2)")
    pa.add_argument("--days", type=int, default=ASK_LOG_RETENTION_DAYS)
    pa.set_defaults(func=_cmd_prune_asks)

    args = p.parse_args(argv)
    # **THE ONE GATE, here rather than repeated in each `_cmd_*`, deliberately.** "`--repo` is
    # required" is a property of the command line, so it is enforced where the command line is
    # parsed: one refusal, one message, one place a future subcommand must be classified. Nothing
    # below `args.func` can be reached without a repository, which is what makes "no defaulting"
    # structural rather than promised — there is no per-command default left to get wrong.
    #
    # It also NORMALISES, so `--repo` accepts what `gh -R` accepts (a slug or a URL) and everything
    # downstream sees one canonical `owner/name`. A value that is neither establishes no repository
    # and is refused with the same exit code: a bare name resolves against the logged-in user, which
    # is exactly the state-outside-the-command this requirement removes.
    if args.cmd in REPO_REQUIRED_COMMANDS:
        if not getattr(args, "repo", None):
            sys.stderr.write(missing_repo_refusal(args.cmd) + "\n")
            return EXIT_NO_REPO
        slug = normalize_repo(args.repo)
        if not slug:
            sys.stderr.write(
                f"merge_guard.py {args.cmd}: --repo {args.repo!r} is neither owner/name nor a "
                f"GitHub URL, so no repository was established. Nothing was read, nothing was "
                f"sent, and nothing was written.\n")
            return EXIT_NO_REPO
        args.repo = slug
    return args.func(args)


if __name__ == "__main__":
    # No argv means the pre-tool hook; `--post-tool-use` alone means the post-tool hook (both read
    # their event from stdin). Any other argv is the CLI.
    if len(sys.argv) == 1:
        sys.exit(main())
    if sys.argv[1:] == [POST_HOOK_ARG]:
        sys.exit(post_main())
    sys.exit(cli())
