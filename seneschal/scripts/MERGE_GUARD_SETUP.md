# Merge guard — install

A `PreToolUse` hook that **refuses `gh pr merge` (and the `gh api` merge endpoints) on any pull
request whose CI is not green, and on any pull request that is not docs-only unless the owner has
approved that exact PR at that exact head SHA.** Script: `merge_guard.py`. A second, optional
`PostToolUseFailure` hook (the same script with `--post-tool-use`) gives an approval back when GitHub
refuses a merge it was spent on.

**This is a user-level install, and until you make it the guard does nothing at all.** The hook goes
in **your** `~/.claude/settings.json` — *not* this repo's `.claude/settings.json*` (a repo-shipped hook
would impose it on every install; personal hook config never ships). No PR can write that file.
Pulling the script into your checkout ships the script, the tests and this page; it changes no
behaviour until you add the blocks in step 2 (or run the automated path in §2c).

---

## Why it exists

`../references/autonomy-policy.md` grants one standing merge lane — **merge a docs-only pull request
once CI is green** — and makes **merging a pull request that changes functionality** ask-high, every
time. A rule that is only written down does not hold: the failure it has to survive is an agent that
generalizes a few one-off *"go ahead and merge that"* instructions into a standing authorization that
does not exist, inside its normal loop. A note in a carry-over file does nothing to a future session.

So this is not another sentence. It is a stop — the same shape as `jobs.py`'s preflight refusal: fail
**open** on "is this even a merge?" (it runs on every shell call and may not brick the machine), fail
**closed** on everything after a merge has been positively identified.

## Which repositories — configuration, not code

Nothing about your repositories is hard-coded. The PR tooling reads one config file through
`repo_config.py`:

- **`seneschal/references/pr-guard.json`** — your per-install config, **gitignored**. Copy the shipped
  **`seneschal/references/pr-guard.example.json`** to it to change anything; while it does not exist,
  the example is what is read. (An owner file that exists but will not parse reads as `{}` — every key
  falls back to git — never as the example.)
- Keys (every one may stay empty):
  - `watched_repos` — `["owner/name", …]`, the repositories the PR sweep asks about. Empty means
    *this checkout's own `origin` repository* and nothing else.
  - `base_branch` — the integration branch PRs target. `null` derives it (below).
  - `protected_branches` — extra branch names no guard ever deletes, **added** to the fixed floor
    `develop`, `master`, `main`, `trunk`, `HEAD`. The config can never remove one.
  - `deploy_on_merge` — `{"owner/name": "branch"}`: repositories where merging to that branch
    redeploys the running assistant. Empty means *this checkout's `origin` redeploys on `main`* —
    the branch the `seneschald-update` merge-detector pulls (`PATH_A_CUTOVER.md`).
- **What git derives when a key is unset:** the repository is `git remote get-url origin`, parsed into
  `owner/name` (https, `git@host:owner/name.git` and `ssh://` forms); the base branch is **`develop`
  when `origin/develop` exists** (Git Flow — which seneschal itself uses: `develop` integrates, `main`
  releases), else the branch `origin/HEAD` points at, else `main`.

Print what your install resolves to:

```bash
python seneschal/scripts/repo_config.py
```

**What the merge guard reads from it is deliberately small.** It judges whatever repository a merge
names — the config never tells it *which* repository a typed command meant (§4f). It reads only
`deploy_on_merge`, and only for two sentences: whether the Approve option may say the merge redeploys
the assistant, and whether a prompt-shaped blocker is described as *"what the assistant executes"*
(only in the assistant's own repository — a foreign repo's `CLAUDE.md` is what *that* repo's agent
runs). Neither moves a verdict.

## 1. Check the script is there

Point the hooks at the **daemon's** checkout — the one `seneschald-update` keeps current — so they
pick up fixes on the next pull without you touching settings again. Below, `$REPO` stands for that
checkout's absolute path (e.g. `C:/Users/you/workspace/seneschal` or `/home/you/seneschal`); for the
shell commands below, set it first (`REPO=/home/you/seneschal` in bash,
`$REPO = "C:/Users/you/workspace/seneschal"` in PowerShell).

```bash
# POSIX
test -f "$REPO/seneschal/scripts/merge_guard.py" && echo present
```

```powershell
# Windows
Test-Path "$REPO/seneschal/scripts/merge_guard.py"
```

`gh` must be on `PATH` and logged in for the hook to classify anything — without it every merge is
refused (that is the fail-closed direction, not a bug).

## 2. Add it to `~/.claude/settings.json`

Add a **`"PreToolUse"`** entry inside the top-level `"hooks"` object. `"PreToolUse"` is a *list*: if
another `PreToolUse` hook is already installed (the bash path guard, the script-file guard), add this
as a separate sibling object with its own `"matcher"`. Order does not matter. With no `PreToolUse`
hooks yet:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|PowerShell",
        "hooks": [
          { "type": "command", "command": "python $REPO/seneschal/scripts/merge_guard.py", "timeout": 90 }
        ]
      }
    ]
  }
}
```

Replace `$REPO` with the absolute path of your checkout (the daemon's, the one that runs off `main`) —
forward slashes work on Windows too and avoid JSON backslash-escaping. Use `python3` instead of
`python` if that is how Python is invoked on your machine. **Mind JSON's no-trailing-comma rule** when
adding the entry beside existing ones.

**`"timeout": 90`, not the default 10.** The hook shells out to `gh pr view`, a network call. If a
hook times out the tool call **proceeds** — so a timeout is an *allow*, which is the one direction this
guard must not fail in, and it fails silently: an allow by timeout looks identical to a correct one.

`"matcher": "Bash|PowerShell"` means the hook process only spawns for shell calls. The script checks
the tool name **again** internally, so the matcher is an optimisation, not the safety mechanism.

### 2b. The give-back hook — `PostToolUseFailure`

A second, **separate** entry, so a merge GitHub refuses stops costing a tap (§4g). Add it as a
sibling of `"PreToolUse"` inside `"hooks"`:

```json
    "PostToolUseFailure": [
      {
        "matcher": "Bash|PowerShell",
        "hooks": [
          { "type": "command", "command": "python $REPO/seneschal/scripts/merge_guard.py --post-tool-use", "timeout": 90 }
        ]
      }
    ]
```

It does nothing for any command that is not a merge, never blocks anything, and exits 0 on every
path — a post-tool hook cannot un-run a command, and one that breaks costs a refund, never a session.
Until you add it, the give-back still exists by hand (`refund`, §4g), with exactly the same conditions.

Check the file still parses:

```bash
# POSIX
python3 -c "import json, os; json.load(open(os.path.expanduser('~/.claude/settings.json'))); print('settings.json parses')"
```

```powershell
# Windows
Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw | ConvertFrom-Json | Out-Null; "settings.json parses"
```

Settings are re-read for **new** sessions. Start a fresh `claude` session (or `/hooks` in a running
one) to pick them up.

### 2c. The automated path

`settings_merge.py` writes both blocks for you — diff-first, append-only, backup-first, and it refuses a
settings file that does not parse:

```bash
python seneschal/scripts/settings_merge.py --guard merge            # dry run: prints the diff
python seneschal/scripts/settings_merge.py --guard merge --apply    # backs up, then writes
```

`--guard all` installs every guard hook (`bash-path`, `script-file`, `merge`, `branch-delete`);
`query-shape` is Notion-only and is never part of `all`.

## 3. Verify it fired

**The hook, without a network call and without spending anything.** A bare `gh pr merge` with no PR
number is refused at the parser, before `gh` is asked anything; an ordinary command is allowed
silently:

```bash
# POSIX
echo '{"tool_name":"Bash","tool_input":{"command":"gh pr merge --merge"},"cwd":"/tmp"}' |
  python3 "$REPO/seneschal/scripts/merge_guard.py"; echo "exit=$?"
echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' |
  python3 "$REPO/seneschal/scripts/merge_guard.py"; echo "exit=$?"
```

```powershell
# Windows
'{"tool_name":"PowerShell","tool_input":{"command":"gh pr merge --merge"},"cwd":"C:/"}' |
  python "$REPO/seneschal/scripts/merge_guard.py"; "exit=$LASTEXITCODE"
'{"tool_name":"PowerShell","tool_input":{"command":"Get-ChildItem"}}' |
  python "$REPO/seneschal/scripts/merge_guard.py"; "exit=$LASTEXITCODE"
```

The first must print `BLOCKED by the merge guard: no PR number in the command…` on stderr and
`exit=2`; the second must print nothing and `exit=0`.

**A real PR, read-only.** `check` consumes nothing, records nothing and merges nothing, so you can run
it as often as you like on anything, including a PR you have just approved:

```bash
python seneschal/scripts/merge_guard.py check --pr 123 --repo <owner>/<name>
```

A functionality PR with no approval prints `verdict: BLOCKED`, the non-docs paths that make it one,
and `approval: none on file for this PR` (exit 2). A green docs-only PR prints
`verdict: ALLOW — docs-only and green, under the standing docs-only grant` (exit 0) — the grant still
working with no friction, which matters as much as the refusal does. A PR with red or pending CI prints
`BLOCKED` with the CI reason, docs-only or not (§4h).

> **Do not verify by piping a real merge payload into the hook for a PR you have just approved.**
> That runs the **hook**, and the hook **spends the approval on an allow** — the diagnostic would
> destroy the tap it was checking. `check` exists so you never have to. The hook's own decision on a
> command string is `judge --command "…"` — same behaviour, **including the spend**, under a name
> that says so (`--dry-run` holds the token).

**The door opens.** `--dry-run` renders the picker without sending anything:

```bash
python seneschal/scripts/merge_guard.py request --pr 123 --repo <owner>/<name> --dry-run
```

That prints a JSON blob containing `"dry_run": true` and a `meta` carrying the head SHA. If it prints
`no chat id` or an argparse error, the delivery path is broken — see §4a.

**The wiring, in a real session.** Type a `gh pr merge <n> --merge` for a functionality PR with **no
approval on file** into a fresh `claude` session: you should get the refusal instead of a merge. A DENY
spends nothing.

## 4. What happens when the assistant hits the wall

It gets the refusal on stderr (exit 2), which quotes the policy, names the file it lives in, names the
repository and how it was established, lists the paths that make the PR ask-high (a prompt path is
tagged `(prompt — this is what the assistant executes)` in the assistant's own repository), and gives
the one way forward:

```
TO PROCEED, ASK THE OWNER — do not work around this:
  python seneschal/scripts/merge_guard.py request --pr 406 --repo <owner>/<name>
```

`request` sends you a tappable **Approve / Not now** picker on Telegram and **writes no approval**.
When you tap Approve, the daemon's Telegram callback path writes
`state/merge-approvals/<owner>__<repo>--<pr>.json`, bound to the head SHA the picker was asked about.
The approval is **single-use** (spent by the merge it allows) and **expires in 24 h**. New commits move
the head SHA and the approval stops matching — the guard refuses again and asks again.

The picker carries three bands, in an order that is a defence: the guard's own facts (the bold,
linked *"Merge <repo> PR #N"* title line, the change-kind sentence, the blocker paths, the full
40-character head SHA, and any other open PR changing the same files); then the PR description under
a header naming where it came from (a lead-in plus a section-by-section outline — §4i); then the bare
link. The title line is repeated under the last option, so a long picker reads from either end. The
Approve option says it redeploys the assistant **only** for a repository and base branch in
`deploy_on_merge`. It lands in the Telegram **pull-requests** topic.

### 4a. Where the picker gets its credentials

The hook is spawned with **no argv**, and the refusal it prints tells the agent to run `request` with
no credential flags. So credentials resolve with no arguments: the sibling `seneschal/scripts/telegram.env`
(this directory's standing convention), else whatever `TELEGRAM_*` the environment carries. An explicit
`--env-file` still wins. The argv handed to `telegram_ask.py` is built in order, never spliced, and the
suite round-trips it through `telegram_ask`'s own parser.

### 4b. The picker sends itself when CI turns green

`merge_guard.ask_on_green` sends the picker on a terminal green CI verdict, so nobody has to remember
to ask. Two callers: `watch_pr.py` (a hand-started watcher, on by default; `--no-ask-on-green` turns it
off) and `pr_sweep.py`, the resident daemon's sweep over `watched_repos`. It stays quiet unless **all
five** hold: the PR is one this guard would block (its own classifier, never a second one), it is
**OPEN**, there is **no live approval** already, the **ask log** has no row for that PR at that head
SHA, and it is **outside the quiet window**.

- **The quiet window** is the owner's night curfew (`owner.nightCurfew` in `persona/identity.json`,
  default 01:00–07:00 on the owner's configured timezone), read through `sentinel` rather than copied.
  A quiet-hours skip records nothing, so it is a deferral: the next pass asks.
- **It stands down while you are demonstrably awake** — a message, tap, reaction or attachment from
  you within the last 30 min (`turns.is_owner_presence`); a job notice does not count. An unreadable
  turns file keeps the window.
- **A docs-only PR gets a notice too — never a gate.** A different picker (*Merge it now / No need*,
  `meta.kind = docs-only-notice`) so you have something to tap; the merge never waits on it.

It writes no approval on any path, and a failed send records nothing — the next pass retries.

### 4c. `check` — asking whether it would work, without spending the answer

```bash
python seneschal/scripts/merge_guard.py check --pr 406 --repo <owner>/<name> [--json]
```

It runs **the same classification and the same approval lookup the hook runs** (one shared pure
function, `evaluate_pr`) and reports: would it allow, in which repository, at which head, is CI green,
whether an approval is on file, its `question_id`, its `consumed_at`, whether it matches the newest
ask, and if blocked, why. Exit 0 = would allow, 2 = would not (including *could not tell*). It prints
no merge command. **It is a snapshot, and says so on every path**: CI, the head SHA and the TTL can
all move between the answer and the merge.

Two rules it does not remove: the hook reads command **text**, so put `gh pr merge` in a bare command
— never inside an `if` or after `&&` that might not reach it, or the tap is spent for a merge the shell
never ran; and `judge --command` still spends, on purpose.

### 4d. One question, asked twice — the ask log is a history

`state/merge-ask-log.jsonl` gets **one appended row per picker sent** (repo, PR, head SHA, question id,
which door). `request` is idempotent on `(repo, PR, head SHA)`: a second identical request is a skip
(`ok: true`, exit 0, `already_asked: true`) that names `--resend` as the escape hatch; new commits, a
different repository, or a **spent** approval re-open asking with no flag. `asks --repo <owner>/<name>`
prints the history; `prune-asks` (run by Dream) ages rows out after 30 days. A rows-keyed-by-PR
predecessor `merge-ask-log.json`, if present, is read and never rewritten.

**The one thing `request` refuses** is a PR that is not **OPEN** — a merge approval for a merged or
closed PR is a question with no valid answer. It exits **3** (`EXIT_PR_NOT_OPEN`), leads the message
with the **repository** (*"<owner>/<name> #90 is MERGED"* is what tells a caller they named the wrong
repo), and sends nothing. Anything that is not a known not-open state (a missing or new `state` value,
a draft) asks anyway: this check can only suppress a question.

### 4e. A PR number is not an identity — the approval is filed under the repo too

The approval key is `(repo, pr)`, and the repository comes from **`gh`'s own answer** (the PR's `url`),
never from the command. Two repositories that each have a #45 therefore get two files and cannot
clobber each other's live approval; the head-SHA binding keeps the *merge* correct across
repositories either way. A legacy bare-number record (`<pr>.json`) is still read — nothing in it says
which repository it was for, so it is tolerated rather than migrated, and `list` reports such records
under `legacy` in every repository's listing.

### 4f. The repository is REQUIRED — no defaulting

- **The CLI** — `check`, `judge`, `request`, `render`, `ask-on-green`, `list`, `asks`, `refund` —
  refuses without `--repo <owner>/<name>` (a GitHub URL is accepted and normalized) and exits **4**
  (`EXIT_NO_REPO`) naming the flag. There is no fallback to the working directory, to `origin`, or to
  the configured repositories. `prune-asks` is the one exemption: it ages a log that spans every
  repository by date.
- **The hook** cannot demand a flag nobody passed, so it **derives**, out loud: the command's own
  `-R`/`--repo` first, the invoking directory's `origin` second, and **when neither answers it blocks**.
  A `gh api` merge's repository is read off its own endpoint path. The refusal, the `judge` output and
  the allow line all say which repository was decided about and how it was established.

This changes only *which repository* the guard decides about, never *what* it decides.

### 4g. A merge GitHub refuses gives the tap back — and `gh api` merges name their own repository

The guard spends the approval **before** the merge (it cannot see the outcome, and a live approval
must never sit beside a merge that might have landed). When GitHub then refuses the merge — a stacked
PR must use the asynchronous REST endpoint, a base branch moved — the `PostToolUseFailure` hook (§2b)
or `merge_guard.py refund --pr N --repo R --exit-status N --command "…"` runs `refund_approval`, which
restores the approval **only** when: the command was the lone merge in its shell segment (a chained
`&&`/`|` exit status is not the merge's own), it exited non-zero, it was not `--auto` and did not fail
as "already enqueued", the spend is under 15 minutes old and has been refunded fewer than 3 times, and
`gh` — asked again, right then — says the PR is **still OPEN at the same head** the approval is bound
to. Every spend, refund and declined refund is a row in `state/merge-approval-events.jsonl`.

`gh api` merges are recognised too: `…/pulls/<n>/merge` and `…/pulls/<n>/merge-async` (and a GraphQL
`mergePullRequest`, which is refused on sight). The REST door must carry `-f sha=<full approved head>`
and `-f merge_method=merge` (merge commits only — never squash or rebase). An async merge also merges
every PR below the requested one in its stack, so it is allowed only for the **bottom** of a stack;
merge from the bottom up, each on its own approval.

A `--match-head-commit` that is a 12-character prefix or the wrong SHA is refused **before** the spend
(GitHub rejects a prefix, which would waste the tap); the refusal prints the full head to copy, and the
picker and relay line print all 40 characters.

### 4h. Never merge red or pending

Every merge — docs-only, approved, any repository — is refused unless every CI check on the PR's head
has **finished and passed**. There is **no carve-out**: not for a failure that looks pre-existing,
flaky or unrelated, not for an approved PR, not for a docs-only one. A head with **no checks at all** is
not green (if a repository has no CI, it needs CI), and a rollup that cannot be read, or a verdict that
cannot be computed, is a refusal. The rollup is read with `watch_pr.classify` — the same fold the
watcher uses to announce *green* — so the two cannot disagree. The only doors past a red check are
fixing it or the owner deciding what to do; the guard never decides a red check is safe to waive.

A base branch that requires branches be up to date (`required_status_checks.strict`) makes a
**BEHIND** head a separate refusal, with its own fix: merge the base into the branch and push, then
re-ask. A BEHIND head is not a conflict.

### 4i. What the description band is, and why it cannot cost the picker

- **The lead-in and outline.** A lead-in made only of pointers (*"phases 2 and 3 of the spec"*) is not
  a description, so the band carries the lead-in plus a section-by-section outline (`pr_digest.py`).
  A reference the body never defines is *named* in one line, never refused — refusing would cost the
  merge, and the only fix would be editing someone else's PR body.
- **Relayed text rides `--quote`.** `telegram_ask` resolves any document or section reference in a
  picker and refuses one it cannot show (`ask_citations.py`). A PR title naming a file the PR is
  DELETING would otherwise make the merge picker un-sendable — the gate failing hardest on the PRs that
  need it. So the title, the description band and the overlap block's entry lines are passed as
  `--quote` spans; the guard's own sentences are not.
- **The header's own citations are paid for first.** Every `.md` blocker path the header names is a
  citation the gate will excerpt or refuse over, so its room is reserved before the description is
  cut, and the blocker line names fewer paths (four → one) rather than name one it cannot describe. A
  path the PR creates resolves from the PR's own head (`--cite-head-*`), restricted to exactly the paths
  the header prints.
- **Nothing in it can make the picker fail to send**: it is cut to the room left under the Bot API
  limit, with visible truncation, and dropped whole before the link or the guard's own facts ever are.

`render --pr N --repo <owner>/<name>` prints the picker a PR would get — sending nothing, recording
nothing, refusing nothing — including for a merged or closed PR. `--overlap-from <file>` (on `request`
and `render`) replays a captured `gh pr list --json number,title,files,isDraft` so the overlap block
can be reproduced after the other PRs have merged.

## 5. What this does **not** stop — read this, it is the honest part

**There is no trust boundary between an agent and this guard.** They run as the same OS user, on the
same disk, and every file either can read the other can read. Any secret the guard could check for, an
agent could read. What the guard buys is not impossibility — it is that **routing around it stops being
a judgment call and becomes a deliberate act.** The failure it exists for is an agent reasoning itself
into an authorization inside its normal loop. That path is closed. These are not:

| Bypass | Guarded? |
|---|---|
| `git push origin HEAD:<branch>` — a direct push to the deploy or integration branch | **No.** Not a merge command at all, and it deploys just as hard. Branch protection is the only real fix, and it is GitHub-side, not something a PR can set. |
| Merging from the GitHub **web UI**, the mobile app, or another machine | **No.** Nothing local is involved. |
| `curl` / any HTTP client with a token against `PUT /pulls/N/merge` | **No.** Only `gh` is recognised. |
| `gh api …/repos/o/r/merges` (the merge-**branch** endpoint) | **No.** Only `/pulls/N/merge`, `/pulls/N/merge-async` and `mergePullRequest` are matched. |
| Lying about a failed merge to `refund` | **No.** Same class as forging the record. The PR must still be OPEN at the approved head, so the most it restores is the owner's own approval for an unmerged PR. |
| A merge inside a **script file** — `pwsh -File deploy.ps1` | **No.** The hook sees the outer command only. `bash -c "…"` and `pwsh -Command "…"` **are** followed, three levels deep. |
| `powershell -EncodedCommand <base64>` | **No, deliberately.** Decoding obfuscation is not the threat model. |
| Writing `state/merge-approvals/<owner>__<repo>--<pr>.json` and a matching `telegram-questions.json` entry by hand | **No.** Both files would have to be forged consistently — the guard cross-checks them — but a determined process can do it. |
| Editing `merge_guard.py` in the daemon's checkout, or its `pr-guard.json` | **No.** Same user, same disk. (The config moves only two sentences, never a verdict.) |
| Uninstalling the hook from `~/.claude/settings.json` | **No.** |
| A merge through an **MCP tool** (e.g. a GitHub MCP server) rather than a shell | **No.** Only the `Bash` and `PowerShell` tools are inspected. |
| Any session where the hook is not installed | **No** — see the top of this page. |

**Prose that runs is not docs-only.** In this repo a lot of Markdown *is* behaviour, so these paths are
`.md` and still ask-high — the picker fires (`merge_guard.PROMPT_PATHS`, `PROMPT_DIRS`,
`AGENT_INSTRUCTION_GLOBS`):

| Denied | Why |
|---|---|
| `seneschal/modes/**` | the mode bodies, read imperatively on dispatch — an edit there edits a live run |
| `seneschal/SKILL.md` and every `subagents/**/SKILL.md` | the orchestrator and its subagents |
| `persona/**` | who the assistant is, mirrored into the phone screener |
| any `references/` directory, at any depth | `seneschal/references/**` — including `autonomy-policy.md`, **the gate itself** — and every subagent's own reference files |
| every other `**/CLAUDE.md` | the per-directory instruction files |
| `**/AGENTS.md`, `**/CLAUDE.local.md`, `**/GEMINI.md`, `.github/copilot-instructions.md`, `**/*.instructions.md`, `.github/agents/**`, `.claude/**`, `.cursorrules`, `.cursor/rules/**` (any depth) | every other harness's instruction file |

**One exemption, `seneschal/docs/CLAUDE.md`, which should not be tidied away.** It is the docs *router*
— an index of documents with a one-line status each, not an instruction — and every documentation PR
touches it to add its entry. Without the exemption, *every `**/CLAUDE.md` is a prompt* would flip
nearly every docs-only PR to ask-high on the router alone: it would not narrow the docs-only grant, it
would repeal it. The docs-only allowlist likewise holds exactly one non-`.md` path,
`seneschal/context-budget.json` (the byte ledger).

**Still on the docs-only side, named here rather than quietly widened:** archon charters and profile
prose (`archons/**`), which are prose their sessions run under but sit outside a `references/`
directory. Covering them is one line in `PROMPT_PATHS` — the owner's decision to make.

## 6. What it deliberately does not do

- **It never resolves a branch name or a bare `gh pr merge`.** Which PR those point at depends on the
  shell's working directory and current branch, neither of which is in the command being judged.
  Requiring the number typed out costs one word and makes every merge self-documenting.
- **It refuses an unrecognised `gh pr merge` flag.** If a future flag takes a value, a guard that
  guessed wrong would read the PR number off the flag's argument and judge a different pull request —
  possibly allowing on *its* diff. There is no safe guess, so there is no guess.
- **It ignores shell redirections, because the shell eats them and `gh` never sees one.** Covered: the
  POSIX operators `<` `>` `>|` `>>` `<>` `<&` `>&` `<<` `<<-`, each with an optional leading fd number,
  glued (`2>&1`, `>out.log`) or separated (`2> err.txt` — the filename goes too); plus bash's `&>` /
  `&>>` and PowerShell's `*>` / `*>>`. **Not** covered, deliberately: bash's `{fd}>file`, which stays a
  positional and therefore still *blocks* — an unknown dialect fails closed. `gh pr merge 5 6` is still
  ambiguous and still refused, and a redirect between two chained merges hides neither.
- **It never says "allow" when it cannot tell.** No `gh`, network error, bad JSON, PR number mismatch,
  empty file list, unreadable CI rollup, a crash in the guard itself — all refuse, and the message says
  which check failed. This is the exact inverse of `bash_path_guard.py`, and the two live side by side
  on purpose: that one fires on every shell call and must never block your work; this one has one job.
- **It never decides a red check is waivable** (§4h), and never mints an approval — only the daemon's
  Telegram callback path writes one, on your tap.

## 7. Remove it

Delete the objects you added from `~/.claude/settings.json` — the `PreToolUse` one and, if you added
it, the `PostToolUseFailure` one (mind the commas) — and start a new session.
`seneschal/state/merge-approvals/`, `seneschal/state/merge-ask-log.jsonl` and
`seneschal/state/merge-approval-events.jsonl` can be deleted too; nothing else reads them.
